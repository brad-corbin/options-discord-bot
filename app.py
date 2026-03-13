# app.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.6 UPGRADE:
#   - Bear put debit spread support (bear signals now generate trade cards)
#   - Scheduled scan disabled — TV webhooks are the only signal source
#   - Direction-aware confidence scoring
#
# v3.7 UPGRADE:
#   - Fibonacci swing trade support (/swing endpoint)
#   - Black-Scholes fair value validation for swing spreads
#   - 7-60 DTE swing engine with auto DTE selection
#   - Weekly + daily trend confirmation for swing entries
#
# v3.9 UPGRADE:
#   - Redis-backed signal queue (survives deploys)
#   - Faster md_get timeout (8s vs 25s) to unblock workers faster
#   - check_ticker wrapped with 45s hard timeout
#   - QUEUE_WORKERS raised to 6
#   - Worker heartbeat logging every 60s

from telegram_commands import (
    handle_command,
    register_webhook,
    is_paused,
    get_confidence_gate,
    set_last_scan,
    get_state,
    send_reply,
)

import os
import time
import math
import json
import hashlib
import logging
import threading
import queue
import portfolio
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify

from options_engine_v3 import (
    recommend_trade,
    format_trade_card,
    as_float,
    as_int,
)
from data_providers import (
    enrich_ticker,
    get_earnings_warning,
    get_iv_rank_from_candles,
)
from trading_rules import (
    MIN_DTE, MAX_DTE, TARGET_DTE,
    MIN_CONFIDENCE_TO_TRADE,
    ALLOWED_DIRECTIONS,
    NO_EARNINGS_WEEK,
    RV_LOOKBACK_DAYS,
    JOURNAL_LOG_ALL_SIGNALS,
)
import risk_manager
import trade_journal

# ─────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────
# ENV VARS
# ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN",  "").strip()
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID",    "").strip()
TV_WEBHOOK_SECRET   = os.getenv("TV_WEBHOOK_SECRET",   "").strip()
MARKETDATA_TOKEN    = os.getenv("MARKETDATA_TOKEN",    "").strip()
WATCHLIST           = os.getenv("WATCHLIST",           "").strip()
SCAN_SECRET         = os.getenv("SCAN_SECRET",         "").strip()
BOT_URL             = os.getenv("BOT_URL",             "").strip()
REDIS_URL           = os.getenv("REDIS_URL",           "").strip()
SCAN_WORKERS        = int(os.getenv("SCAN_WORKERS", "4") or 4)
DEDUP_TTL_SECONDS   = int(os.getenv("DEDUP_TTL_SECONDS", "3600") or 3600)

TELEGRAM_PORTFOLIO_CHAT_ID     = os.getenv("TELEGRAM_PORTFOLIO_CHAT_ID",     "").strip()
TELEGRAM_MOM_PORTFOLIO_CHAT_ID = os.getenv("TELEGRAM_MOM_PORTFOLIO_CHAT_ID", "").strip()

ACCOUNT_CHAT_IDS = {
    "brad": TELEGRAM_PORTFOLIO_CHAT_ID,
    "mom":  TELEGRAM_MOM_PORTFOLIO_CHAT_ID,
}

# ─────────────────────────────────────────────────────────
# REDIS
# ─────────────────────────────────────────────────────────
_redis_client = None


class _MemStore:
    """
    TTL-aware in-memory fallback store.

    Mirrors the Redis setex / get / exists interface so store_set / store_get /
    store_exists behave identically whether Redis is up or down.

    Keys written without a TTL live forever (same semantics as Redis SET with no EX).
    Keys written with TTL > 0 expire at `time.monotonic() + ttl` and are pruned
    lazily on every read/write — no background thread needed.

    Without this, dedup keys written during a Redis outage would live forever in
    the process, silently suppressing valid future alerts for the remainder of the
    Render dyno's uptime.
    """
    def __init__(self):
        self._data: dict = {}   # key → (value, expires_at_monotonic | None)
        self._lock = threading.Lock()

    def _prune(self):
        """Remove all expired keys. Called inside the lock on every read/write."""
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._data.items() if exp is not None and now >= exp]
        for k in expired:
            del self._data[k]

    def set(self, key: str, value: str, ttl: int = 0):
        exp = (time.monotonic() + ttl) if ttl > 0 else None
        with self._lock:
            self._prune()
            self._data[key] = (value, exp)

    def get(self, key: str):
        with self._lock:
            self._prune()
            entry = self._data.get(key)
            return entry[0] if entry is not None else None

    def exists(self, key: str) -> bool:
        with self._lock:
            self._prune()
            return key in self._data


_mem_store = _MemStore()


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        # Verify the cached connection is still alive
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            log.warning("Redis cached connection dead — reconnecting")
            _redis_client = None

    if not REDIS_URL:
        return None
    try:
        import redis
        _redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=10,         # longer for BRPOP blocking calls
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        _redis_client.ping()
        log.info("Redis connected")
        return _redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}), using in-memory fallback")
        _redis_client = None
        return None


def store_set(key: str, value: str, ttl: int = 0):
    r = _get_redis()
    if r:
        try:
            if ttl:
                r.setex(key, ttl, value)
            else:
                r.set(key, value)
            return
        except Exception as e:
            log.warning(f"Redis set failed ({e}) — writing to mem fallback")
    _mem_store.set(key, value, ttl)


def store_get(key: str):
    r = _get_redis()
    if r:
        try:
            return r.get(key)
        except Exception:
            pass
    return _mem_store.get(key)


def store_exists(key: str) -> bool:
    r = _get_redis()
    if r:
        try:
            return bool(r.exists(key))
        except Exception:
            pass
    return _mem_store.exists(key)

# ─────────────────────────────────────────────────────────
# DEDUP
# ─────────────────────────────────────────────────────────

def trade_dedup_key(ticker, direction, short_k, long_k) -> str:
    raw = f"{ticker}:{direction}:{short_k}:{long_k}"
    return "dedup:" + hashlib.md5(raw.encode()).hexdigest()

def is_duplicate_trade(ticker, direction, short_k, long_k) -> bool:
    return store_exists(trade_dedup_key(ticker, direction, short_k, long_k))

def mark_trade_sent(ticker, direction, short_k, long_k):
    store_set(trade_dedup_key(ticker, direction, short_k, long_k), "1", ttl=DEDUP_TTL_SECONDS)

# ─────────────────────────────────────────────────────────
# UNIFIED SIGNAL QUEUE — Redis-backed (v3.9)
# ─────────────────────────────────────────────────────────
# Signals are pushed to a Redis list ("signal_queue") so they survive
# deploys and process restarts.  Workers use BRPOP (blocking pop).
# Falls back to in-memory queue if Redis is unavailable.
#
# Job dict keys: job_type, ticker, bias, webhook_data, signal_msg, enqueued_at

QUEUE_MAX        = 80
QUEUE_WORKERS    = 6      # v3.9: raised from 3
SIGNAL_TTL_SEC   = 480    # 8 min — drop stale signals
TG_MIN_GAP_SEC   = 0.8    # v3.9: lowered from 1.5
WAVE_COLLECT_SEC = 90     # v4.0: seconds to wait for bar-close flood to settle before digest

REDIS_QUEUE_KEY  = "signal_queue"

# In-memory fallback queue (used only when Redis is unavailable)
_mem_signal_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)

_tg_last_post_time: float = 0.0
_tg_lock = threading.Lock()

# ─────────────────────────────────────────────────────────
# WAVE COLLECTOR — v4.0
# ─────────────────────────────────────────────────────────
# Workers silently accumulate results during a bar-close flood.
# After WAVE_COLLECT_SEC of quiet, digest-poster fires one summary
# message + only the winning trade cards.

_wave_lock         = threading.Lock()
_wave_results: list = []
_wave_last_arrival: float = 0.0


def _record_wave_result(result: dict):
    """Add a processed signal result to the current wave buffer."""
    global _wave_last_arrival
    with _wave_lock:
        _wave_results.append(result)
        _wave_last_arrival = time.time()


def _flush_wave_digest():
    """
    Post one digest summary + trade cards for winners only.
    Clears the wave buffer.
    """
    global _wave_results
    with _wave_lock:
        if not _wave_results:
            return
        results = list(_wave_results)
        _wave_results = []

    tv_winners, tv_skipped, swing_winners, swing_skipped = [], [], [], []

    for r in results:
        job_type = r.get("job_type", "tv")
        entry = {
            "ticker": r.get("ticker", "?"),
            "bias":   r.get("bias",   "bull"),
            "tier":   r.get("tier",   "?"),
            "conf":   r.get("confidence"),
            "card":   r.get("card"),
        }
        won = r.get("outcome") == "trade"
        if job_type == "swing":
            (swing_winners if won else swing_skipped).append(entry)
        else:
            (tv_winners if won else tv_skipped).append(entry)

    lines = []

    if tv_winners or tv_skipped:
        lines.append("📊 TV SIGNAL DIGEST")
        if tv_winners:
            lines.append(f"✅ {len(tv_winners)} trade card(s) below")
        if tv_skipped:
            t1b, t1bear, t2b, t2bear, other = [], [], [], [], []
            for e in tv_skipped:
                t, b = e["tier"], e["bias"]
                if   t == "1" and b == "bull": t1b.append(e["ticker"])
                elif t == "1" and b == "bear": t1bear.append(e["ticker"])
                elif t == "2" and b == "bull": t2b.append(e["ticker"])
                elif t == "2" and b == "bear": t2bear.append(e["ticker"])
                else: other.append(e["ticker"])
            lines.append("❌ No trade:")
            if t1b:    lines.append("  T1 🐂: " + " · ".join(t1b))
            if t1bear: lines.append("  T1 🐻: " + " · ".join(t1bear))
            if t2b:    lines.append("  T2 🐂: " + " · ".join(t2b))
            if t2bear: lines.append("  T2 🐻: " + " · ".join(t2bear))
            if other:  lines.append("  Other: " + " · ".join(other))

    if swing_winners or swing_skipped:
        if lines:
            lines.append("")
        lines.append("🔄 SWING SIGNAL DIGEST")
        if swing_winners:
            lines.append(f"✅ {len(swing_winners)} swing card(s) below")
        if swing_skipped:
            bull_s = [e["ticker"] for e in swing_skipped if e["bias"] == "bull"]
            bear_s = [e["ticker"] for e in swing_skipped if e["bias"] == "bear"]
            lines.append("❌ No swing trade:")
            if bull_s: lines.append("  🐂: " + " · ".join(bull_s))
            if bear_s: lines.append("  🐻: " + " · ".join(bear_s))

    if not lines:
        return

    log.info(f"Wave digest: {len(tv_winners)} TV wins, {len(tv_skipped)} skipped, "
             f"{len(swing_winners)} swing wins, {len(swing_skipped)} swing skipped")
    _tg_rate_limited_post("\n".join(lines))

    for entry in tv_winners + swing_winners:
        if entry.get("card"):
            _tg_rate_limited_post(entry["card"])


def _digest_poster_thread():
    """Flush wave digest after WAVE_COLLECT_SEC of silence."""
    log.info("Wave digest poster started")
    while True:
        time.sleep(5)
        with _wave_lock:
            has_results  = bool(_wave_results)
            last_arrival = _wave_last_arrival
        if has_results and (time.time() - last_arrival) >= WAVE_COLLECT_SEC:
            try:
                _flush_wave_digest()
            except Exception as e:
                log.error(f"Digest flush error: {e}", exc_info=True)


threading.Thread(target=_digest_poster_thread, daemon=True, name="digest-poster").start()


def _tg_rate_limited_post(text: str, max_retries: int = 6, chat_id: str = None):
    """Post to Telegram with global rate limiting across all workers."""
    global _tg_last_post_time
    with _tg_lock:
        now = time.time()
        gap = now - _tg_last_post_time
        if gap < TG_MIN_GAP_SEC:
            time.sleep(TG_MIN_GAP_SEC - gap)
        _tg_last_post_time = time.time()
    return post_to_telegram(text, max_retries=max_retries, chat_id=chat_id)


def _enqueue_signal(job_type: str, ticker: str, bias: str,
                    webhook_data: dict, signal_msg: str):
    """
    Push a signal job to Redis list.
    Falls back to in-memory queue if Redis is unavailable.
    """
    job = {
        "job_type":    job_type,
        "ticker":      ticker,
        "bias":        bias,
        "webhook_data": webhook_data,
        "signal_msg":  signal_msg,
        "enqueued_at": time.time(),
    }

    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            pipe.lpush(REDIS_QUEUE_KEY, json.dumps(job))
            pipe.ltrim(REDIS_QUEUE_KEY, 0, QUEUE_MAX - 1)
            pipe.execute()
            qsize = r.llen(REDIS_QUEUE_KEY)
            log.info(f"{job_type.upper()} signal pushed to Redis: {ticker} (depth: {qsize})")
            if qsize > QUEUE_MAX * 0.7:
                log.warning(f"Redis signal queue at {qsize}/{QUEUE_MAX} — approaching capacity")
            return
        except Exception as e:
            log.warning(f"Redis enqueue failed ({e}), falling back to memory queue")

    # In-memory fallback
    mem_job = (job_type, ticker, bias, webhook_data, signal_msg, job["enqueued_at"])
    try:
        _mem_signal_queue.put_nowait(mem_job)
        log.info(f"{job_type.upper()} signal queued (memory fallback): {ticker}")
    except queue.Full:
        try:
            dropped = _mem_signal_queue.get_nowait()
            log.warning(f"Memory queue full — dropped: {dropped[1]} ({dropped[0]})")
            _mem_signal_queue.task_done()
        except queue.Empty:
            pass
        try:
            _mem_signal_queue.put_nowait(mem_job)
        except queue.Full:
            log.error(f"Memory queue still full — signal lost: {ticker} ({job_type})")


def _process_job(worker_id: int, job: dict):
    """
    Process a single signal job silently — accumulate result in wave buffer.
    The digest-poster thread fires one summary + winning cards after the wave
    settles (WAVE_COLLECT_SEC quiet period). v4.0
    """
    job_type     = job["job_type"]
    ticker       = job["ticker"]
    bias         = job["bias"]
    webhook_data = job["webhook_data"]
    enqueued_at  = job["enqueued_at"]
    tier_label   = webhook_data.get("tier", "?")

    age_sec = time.time() - enqueued_at
    if age_sec > SIGNAL_TTL_SEC:
        log.warning(f"[worker-{worker_id}] Stale {job_type} signal dropped: {ticker} ({age_sec:.0f}s)")
        return

    log.info(f"[worker-{worker_id}] Processing {job_type}: {ticker} {bias} T{tier_label} ({age_sec:.0f}s ago)")

    base = {
        "job_type":   job_type,
        "ticker":     ticker,
        "bias":       bias,
        "tier":       tier_label,
        "outcome":    "skip",
        "card":       None,
        "confidence": None,
        "reason":     "",
    }

    if job_type == "tv":
        # Exit warning is time-sensitive — still posts immediately
        check_spread_exit_warning(ticker, bias, webhook_data)

        if bias not in ALLOWED_DIRECTIONS:
            base["reason"] = f"{bias} not in allowed directions"
            _record_wave_result(base)
            return

        rec = check_ticker_with_timeout(ticker, direction=bias, webhook_data=webhook_data)
        base["confidence"] = rec.get("confidence")

        if rec.get("error") == "timeout":
            base["reason"] = "timeout"
            _record_wave_result(base)
            return

        if not rec.get("ok"):
            base["reason"] = rec.get("reason", "no valid spread")
            _record_wave_result(base)
            return

        if not rec.get("posted") and rec.get("reason") == "Duplicate trade in dedup window":
            base["reason"] = "duplicate"
            _record_wave_result(base)
            return

        # Trade winner — card was already built in check_ticker
        card_text = rec.get("card")
        base["outcome"] = "trade"
        base["card"]    = card_text
        _record_wave_result(base)
        log.info(f"TV winner queued for digest: {ticker} {bias} T{tier_label} conf={rec.get('confidence')}/100")

    elif job_type == "swing":
        from swing_engine import recommend_swing_trade, format_swing_card
        try:
            spot   = get_spot(ticker)
            chains = get_options_chain_swing(ticker)
        except Exception as e:
            base["reason"] = f"data fetch error: {e}"
            _record_wave_result(base)
            return

        if not chains:
            base["reason"] = "no options chain data in 7-60 DTE range"
            _record_wave_result(base)
            return

        candles = get_daily_candles(ticker, days=252)
        iv_rank = _estimate_iv_rank(chains, candles)
        rec = recommend_swing_trade(
            ticker=ticker, spot=spot, chains=chains,
            webhook_data=webhook_data, iv_rank=iv_rank,
        )
        base["confidence"] = rec.get("confidence")

        if not rec.get("ok"):
            base["reason"] = rec.get("reason", "no valid setup")
            _record_wave_result(base)
        else:
            base["outcome"] = "trade"
            base["card"]    = format_swing_card(rec)
            _record_wave_result(base)
            log.info(f"Swing winner queued for digest: {ticker} {bias} conf={rec.get('confidence')}/100")



def _signal_queue_worker_redis(worker_id: int):
    """
    Redis-backed worker. Uses BRPOP so it blocks efficiently.
    Reconnects automatically if Redis drops.
    Survives deploys — signals persist in Redis list.
    """
    log.info(f"Redis signal worker-{worker_id} started")
    last_heartbeat = time.time()

    while True:
        # Heartbeat
        now = time.time()
        if now - last_heartbeat > 60:
            r_hb = _get_redis()
            depth = r_hb.llen(REDIS_QUEUE_KEY) if r_hb else "?"
            log.info(f"[worker-{worker_id}] heartbeat — Redis queue depth: {depth}")
            last_heartbeat = now

        r = _get_redis()
        if not r:
            log.warning(f"[worker-{worker_id}] Redis unavailable — sleeping 5s")
            time.sleep(5)
            continue

        try:
            # Blocking pop with 5s timeout so heartbeat fires regularly
            result = r.brpop(REDIS_QUEUE_KEY, timeout=5)
            if not result:
                continue

            _, raw = result
            job = json.loads(raw)

            try:
                _process_job(worker_id, job)
            except Exception as e:
                log.error(
                    f"[worker-{worker_id}] Job error for {job.get('ticker')} "
                    f"({job.get('job_type')}): {e}",
                    exc_info=True,
                )

        except Exception as e:
            log.error(f"[worker-{worker_id}] Redis worker error: {e}", exc_info=True)
            # Null out cached client so _get_redis() reconnects on next iteration
            global _redis_client
            _redis_client = None
            time.sleep(3)


def _signal_queue_worker_memory(worker_id: int):
    """
    In-memory fallback worker (used when Redis is unavailable).
    Same logic as original worker.
    """
    log.info(f"Memory signal worker-{worker_id} started")
    last_heartbeat = time.time()

    while True:
        now = time.time()
        if now - last_heartbeat > 60:
            log.info(f"[worker-{worker_id}] heartbeat — memory queue depth: {_mem_signal_queue.qsize()}")
            last_heartbeat = now

        try:
            try:
                job_tuple = _mem_signal_queue.get(block=True, timeout=5)
            except queue.Empty:
                continue

            if job_tuple is None:
                break

            job_type, ticker, bias, webhook_data, signal_msg, enqueued_at = job_tuple
            job = {
                "job_type":    job_type,
                "ticker":      ticker,
                "bias":        bias,
                "webhook_data": webhook_data,
                "signal_msg":  signal_msg,
                "enqueued_at": enqueued_at,
            }

            try:
                _process_job(worker_id, job)
            except Exception as e:
                log.error(f"[worker-{worker_id}] Memory job error for {ticker}: {e}", exc_info=True)
            finally:
                _mem_signal_queue.task_done()

        except Exception as e:
            log.error(f"[worker-{worker_id}] Memory worker error: {e}", exc_info=True)
            time.sleep(2)


def _start_workers():
    """Start worker pool — Redis workers if Redis is available, memory otherwise."""
    r = _get_redis()
    worker_fn = _signal_queue_worker_redis if r else _signal_queue_worker_memory
    mode = "Redis" if r else "memory"
    log.info(f"Starting {QUEUE_WORKERS} {mode} signal workers (TTL {SIGNAL_TTL_SEC}s)")

    threads = []
    for i in range(QUEUE_WORKERS):
        t = threading.Thread(
            target=worker_fn,
            args=(i + 1,),
            daemon=True,
            name=f"signal-worker-{i + 1}",
        )
        t.start()
        threads.append(t)
    return threads


_queue_worker_threads = _start_workers()


# ─────────────────────────────────────────────────────────
# check_ticker WITH HARD TIMEOUT (v3.9)
# ─────────────────────────────────────────────────────────

def check_ticker_with_timeout(
    ticker: str,
    direction: str = "bull",
    webhook_data: dict = None,
    timeout_sec: int = 45,
) -> dict:
    """
    Run check_ticker with a hard wall-clock timeout.
    Prevents a hung MarketData request from blocking a worker indefinitely.
    """
    result = {}
    exc_holder = []

    def _run():
        try:
            result.update(
                check_ticker(ticker, direction=direction, webhook_data=webhook_data)
            )
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        log.error(f"check_ticker({ticker}) TIMED OUT after {timeout_sec}s — worker unblocked")
        # Don't post directly — let wave digest handle it as a skip
        return {"ticker": ticker, "ok": False, "posted": False, "error": "timeout", "reason": "timeout"}

    if exc_holder:
        raise exc_holder[0]

    return result


# ─────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────

def post_to_telegram(text: str, max_retries: int = 4, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not cid:
        log.error("post_to_telegram: TELEGRAM_BOT_TOKEN or CHAT_ID not set")
        return 400, "TELEGRAM tokens not set"
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                preview = text[:60].replace("\n", " ")
                log.info(f"Telegram OK (chat={cid}): {preview}...")
                return 200, ""
            if r.status_code == 429:
                try:
                    retry_after = r.json().get("parameters", {}).get("retry_after", 15)
                except Exception:
                    retry_after = 15
                log.warning(f"Telegram rate limited — waiting {retry_after}s (attempt {attempt+1})")
                time.sleep(retry_after + 1)
                continue
            last_err = r.text[:300] if r.text else f"HTTP {r.status_code}"
            log.warning(f"Telegram attempt {attempt+1} failed: {r.status_code} — {last_err}")
        except Exception as e:
            last_err = str(e)
            log.warning(f"Telegram attempt {attempt+1} exception: {last_err}")
        time.sleep(min(1.5 * (attempt + 1), 6.0))
    log.error(f"Telegram FAILED after {max_retries} attempts: {last_err}")
    return 500, f"Telegram failed: {last_err}"


def get_portfolio_chat_id(account: str) -> str:
    cid = ACCOUNT_CHAT_IDS.get(account, "")
    return cid if cid else TELEGRAM_CHAT_ID

# ─────────────────────────────────────────────────────────
# MARKETDATA API — timeout reduced to 8s (v3.9)
# ─────────────────────────────────────────────────────────

def md_get(url, params=None):
    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {MARKETDATA_TOKEN}"},
        params=params or {},
        timeout=8,   # v3.9: reduced from 25s — fail fast, don't block workers
    )
    r.raise_for_status()
    return r.json()

def get_spot(ticker: str) -> float:
    data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")
    for field in ("last", "mid", "bid", "ask"):
        v = as_float(data.get(field), 0.0)
        if v > 0:
            return v
    raise RuntimeError(f"Cannot parse spot for {ticker}")

def get_expirations(ticker: str) -> list:
    data = md_get(f"https://api.marketdata.app/v1/options/expirations/{ticker}/")
    if not isinstance(data, dict) or data.get("s") != "ok":
        raise RuntimeError(f"Bad expirations for {ticker}")
    return sorted(set(str(e)[:10] for e in (data.get("expirations") or []) if e))

def get_daily_candles(ticker: str, days: int = 30) -> list:
    """Fetch recent daily candles for RV / IV rank calculation."""
    try:
        from_date = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/daily/{ticker}/",
            {"from": from_date, "countback": days + 5},
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return []
        closes = data.get("c", [])
        if isinstance(closes, list):
            return [float(c) for c in closes if c is not None]
        return []
    except Exception as e:
        log.warning(f"Daily candles fetch failed for {ticker}: {e}")
        return []

def get_vix() -> float:
    """Fetch current VIX spot level."""
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        resp.raise_for_status()
        result = resp.json().get("chart", {}).get("result", [])
        if result:
            meta = result[0].get("meta", {})
            for field in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
                v = as_float(meta.get(field), 0.0)
                if v > 0:
                    log.info(f"VIX from Yahoo Finance: {v:.2f}")
                    return v
    except Exception as e:
        log.warning(f"VIX Yahoo fetch failed: {e}")

    try:
        spy_chains = get_options_chain_swing("SPY")
        ivs = []
        for exp, dte, contracts in spy_chains[:3]:
            for c in contracts:
                iv = as_float(c.get("iv") or c.get("impliedVolatility"), 0.0)
                if 0.05 < iv < 2.0:
                    ivs.append(iv * 100)
        if ivs:
            proxy = round(sum(ivs) / len(ivs), 1)
            log.info(f"VIX proxy from SPY IV: {proxy:.1f}")
            return proxy
    except Exception as e:
        log.warning(f"VIX SPY-IV fallback failed: {e}")

    log.warning("VIX unavailable — returning 20.0 as neutral default")
    return 20.0


_regime_cache = {"data": None, "ts": 0}

def get_current_regime() -> dict:
    """Get current market regime (VIX + ADX). Cached 5 minutes."""
    now = time.time()
    if _regime_cache["data"] and (now - _regime_cache["ts"]) < 300:
        return _regime_cache["data"]

    try:
        vix = get_vix()
        spy_candles = get_daily_candles("SPY", days=30)
        regime = risk_manager.classify_regime(
            vix=vix,
            spy_candles=spy_candles,
        )
        _regime_cache["data"] = regime
        _regime_cache["ts"] = now
        log.info(f"Regime: {regime.get('label')} (VIX {vix:.1f}, ADX {regime.get('adx', 0):.0f})")
        return regime
    except Exception as e:
        log.warning(f"Regime detection failed: {e}")
        return {"label": "UNKNOWN", "emoji": "❓", "vix": 0, "adx": 0,
                "vix_regime": "UNKNOWN", "adx_regime": "UNKNOWN", "size_mult": 1.0}


# ─────────────────────────────────────────────────────────
# OPTIONS CHAIN — SCALP (0-10 DTE)
# ─────────────────────────────────────────────────────────

def get_options_chain(ticker: str) -> list:
    """Fetch options chain for scalp trades (MIN_DTE to MAX_DTE)."""
    from trading_rules import MAX_EXPIRATIONS_TO_PULL

    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = datetime.now(timezone.utc).date()

    valid_exps = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if MIN_DTE <= dte <= MAX_DTE:
                valid_exps.append((dte, exp))
        except Exception:
            continue

    valid_exps.sort(key=lambda x: x[0])

    if not valid_exps:
        for exp in exps:
            try:
                dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
                valid_exps.append((dte, exp))
            except Exception:
                continue
        valid_exps.sort(key=lambda x: x[0])

    if not valid_exps:
        raise RuntimeError(f"No usable expirations for {ticker}")

    exps_to_fetch = valid_exps[:MAX_EXPIRATIONS_TO_PULL]
    results = []

    for dte, exp in exps_to_fetch:
        try:
            data = md_get(
                f"https://api.marketdata.app/v1/options/chain/{ticker}/",
                {"expiration": exp},
            )
            if not isinstance(data, dict) or data.get("s") != "ok":
                continue

            sym_list = data.get("optionSymbol") or []
            if not sym_list:
                continue

            n = len(sym_list)

            def col(name, default=None):
                v = data.get(name, default)
                return v if isinstance(v, list) else [default] * n

            contracts = []
            sides = col("side", "")
            for i in range(n):
                right = (sides[i] or "").lower().strip()
                right = "call" if right in ("c", "call") else "put" if right in ("p", "put") else right
                contracts.append({
                    "optionSymbol": col("optionSymbol", "")[i],
                    "right":        right,
                    "strike":       as_float(col("strike",       None)[i], None),
                    "expiration":   exp,
                    "dte":          dte,
                    "openInterest": as_int(col("openInterest",   0)[i], 0),
                    "volume":       as_int(col("volume",          0)[i], 0),
                    "iv":           as_float(col("iv",           None)[i], None),
                    "gamma":        as_float(col("gamma",         0.0)[i], 0.0),
                    "delta":        as_float(col("delta",        None)[i], None),
                    "theta":        as_float(col("theta",        None)[i], None),
                    "vega":         as_float(col("vega",         None)[i], None),
                    "bid":          as_float(col("bid",          None)[i], None),
                    "ask":          as_float(col("ask",          None)[i], None),
                    "mid":          as_float(col("mid",          None)[i], None),
                })

            if contracts:
                results.append((exp, dte, contracts))
                log.info(f"{ticker}: fetched {len(contracts)} contracts for {exp} (DTE {dte})")

        except Exception as e:
            log.warning(f"{ticker}: failed to fetch chain for {exp}: {e}")
            continue

    if not results:
        raise RuntimeError(f"No valid chains fetched for {ticker}")

    return results


# ─────────────────────────────────────────────────────────
# OPTIONS CHAIN — SWING (7-60 DTE)
# ─────────────────────────────────────────────────────────

def get_options_chain_swing(ticker: str) -> list:
    """Fetch options chain for swing trades (7-60 DTE)."""
    from swing_engine import SWING_MIN_DTE, SWING_MAX_DTE, SWING_MAX_EXPIRATIONS

    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = datetime.now(timezone.utc).date()

    valid_exps = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if SWING_MIN_DTE <= dte <= SWING_MAX_DTE:
                valid_exps.append((dte, exp))
        except Exception:
            continue

    valid_exps.sort(key=lambda x: x[0])

    if not valid_exps:
        raise RuntimeError(f"No swing expirations ({SWING_MIN_DTE}-{SWING_MAX_DTE} DTE) for {ticker}")

    results = []

    for dte, exp in valid_exps[:SWING_MAX_EXPIRATIONS]:
        try:
            data = md_get(
                f"https://api.marketdata.app/v1/options/chain/{ticker}/",
                {"expiration": exp},
            )
            if not isinstance(data, dict) or data.get("s") != "ok":
                continue

            sym_list = data.get("optionSymbol") or []
            if not sym_list:
                continue

            n = len(sym_list)

            def col(name, default=None):
                v = data.get(name, default)
                return v if isinstance(v, list) else [default] * n

            contracts = []
            sides = col("side", "")
            for i in range(n):
                right = (sides[i] or "").lower().strip()
                right = "call" if right in ("c", "call") else "put" if right in ("p", "put") else right
                contracts.append({
                    "optionSymbol": col("optionSymbol", "")[i],
                    "right":        right,
                    "strike":       as_float(col("strike",       None)[i], None),
                    "expiration":   exp,
                    "dte":          dte,
                    "openInterest": as_int(col("openInterest",   0)[i], 0),
                    "volume":       as_int(col("volume",          0)[i], 0),
                    "iv":           as_float(col("iv",           None)[i], None),
                    "gamma":        as_float(col("gamma",         0.0)[i], 0.0),
                    "delta":        as_float(col("delta",        None)[i], None),
                    "theta":        as_float(col("theta",        None)[i], None),
                    "vega":         as_float(col("vega",         None)[i], None),
                    "bid":          as_float(col("bid",          None)[i], None),
                    "ask":          as_float(col("ask",          None)[i], None),
                    "mid":          as_float(col("mid",          None)[i], None),
                })

            if contracts:
                results.append((exp, dte, contracts))
                log.info(f"Swing {ticker}: fetched {len(contracts)} contracts for {exp} (DTE {dte})")

        except Exception as e:
            log.warning(f"Swing {ticker}: failed chain for {exp}: {e}")
            continue

    return results


# ─────────────────────────────────────────────────────────
# IV RANK HELPER
# ─────────────────────────────────────────────────────────

def _estimate_iv_rank(chains: list, candle_closes: list) -> float:
    try:
        from options_engine_v3 import calc_realized_vol

        current_ivs = []
        for exp, dte, contracts in chains:
            for c in contracts:
                iv = c.get("iv")
                if iv and iv > 0:
                    current_ivs.append(iv)
            if current_ivs:
                break

        if not current_ivs:
            return 50.0

        current_iv = sum(current_ivs) / len(current_ivs)
        rv = calc_realized_vol(candle_closes) if candle_closes else 0
        if rv <= 0:
            return 50.0

        ratio = current_iv / rv
        rank  = min(100, max(0, (ratio - 0.5) / 1.5 * 100))
        return round(rank, 1)

    except Exception:
        return 50.0


# ─────────────────────────────────────────────────────────
# EXIT WARNING
# ─────────────────────────────────────────────────────────

def check_spread_exit_warning(ticker: str, signal_bias: str, webhook_data: dict):
    ticker = ticker.upper()

    if signal_bias == "bear":
        warn_direction = "bull"
        warn_side = "call"
    elif signal_bias == "bull":
        warn_direction = "bear"
        warn_side = "put"
    else:
        return

    for account in ("brad", "mom"):
        try:
            open_spreads = portfolio.get_open_spreads_for_ticker(ticker, account=account)
            if not open_spreads:
                continue

            at_risk = [
                sp for sp in open_spreads
                if sp.get("direction", "bull") == warn_direction
                or sp.get("side", "call") == warn_side
            ]
            if not at_risk:
                continue

            tier        = webhook_data.get("tier", "?")
            wt2         = as_float(webhook_data.get("wt2"), 0)
            close_price = as_float(webhook_data.get("close"), 0)
            wave_zone   = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
            trend_str   = ("✅ Confirmed" if webhook_data.get("htf_confirmed")
                           else "🟡 Converging" if webhook_data.get("htf_converging")
                           else "❌ Diverging")
            acct_label  = "👩 Mom" if account == "mom" else "📁 Brad"

            for sp in at_risk:
                sp_debit     = sp.get("debit", 0)
                sp_contracts = sp.get("contracts", 1)
                total_risk   = sp_debit * sp_contracts * 100
                targets      = sp.get("targets", {})
                short_strike = sp.get("short", 0)
                long_strike  = sp.get("long", 0)
                sp_side      = sp.get("side", "call")
                side_label   = "BULL CALL" if sp_side == "call" else "BEAR PUT"

                urgency      = "⚠️"
                urgency_note = "Monitor position"

                if close_price > 0 and sp_side == "call":
                    if short_strike > 0 and close_price <= short_strike:
                        urgency      = "🚨"
                        urgency_note = "PRICE AT/BELOW SHORT STRIKE — spread losing value"
                    elif long_strike > 0 and close_price <= long_strike:
                        urgency      = "🔴"
                        urgency_note = "Price between strikes — partial loss territory"
                elif close_price > 0 and sp_side == "put":
                    if short_strike > 0 and close_price >= short_strike:
                        urgency      = "🚨"
                        urgency_note = "PRICE AT/ABOVE SHORT STRIKE — spread losing value"
                    elif long_strike > 0 and close_price >= long_strike:
                        urgency      = "🔴"
                        urgency_note = "Price between strikes — partial loss territory"

                stop_level = targets.get("stop", 0)

                lines = [
                    f"{urgency} EXIT WARNING — {ticker} {side_label} ({acct_label})",
                    f"TV Signal: T{tier} {signal_bias.upper()} | Close: ${close_price:.2f}",
                    f"1H Trend: {trend_str} | Wave: {wave_zone}",
                    "",
                    f"Open Spread: {sp['id']}",
                    f"  ${long_strike}/{short_strike} @${sp_debit:.2f} x{sp_contracts}",
                    f"  Risk: ${total_risk:,.0f} | Exp: {sp.get('exp', '?')}",
                    f"  {urgency_note}",
                    "",
                    f"Targets: sell@${targets.get('same_day', 0):.2f} (30%) | "
                    f"${targets.get('next_day', 0):.2f} (35%) | "
                    f"${targets.get('extended', 0):.2f} (50%)",
                ]

                if stop_level:
                    lines.append(f"Stop: ${stop_level:.2f}")

                lines.extend([
                    "",
                    "Action: Consider closing or tightening stop",
                    "— Not financial advice —",
                ])

                post_to_telegram("\n".join(lines))
                log.info(f"Exit warning posted for {ticker} {side_label} spread {sp['id']} ({account})")

        except Exception as e:
            log.error(f"Exit warning check failed for {ticker}/{account}: {e}")


# ─────────────────────────────────────────────────────────
# CHECK TICKER — scalp engine (0-10 DTE)
# ─────────────────────────────────────────────────────────

def check_ticker(
    ticker: str,
    direction: str = "bull",
    webhook_data: dict = None,
) -> dict:
    ticker       = ticker.strip().upper()
    webhook_data = webhook_data or {"bias": direction, "tier": "2"}

    try:
        spot        = get_spot(ticker)
        chains      = get_options_chain(ticker)
        enrichment  = enrich_ticker(ticker)
        has_earnings = enrichment.get("has_earnings", False)
        earnings_warn = enrichment.get("earnings_warn")
        has_dividend  = False

        candle_closes = get_daily_candles(ticker, days=RV_LOOKBACK_DAYS + 5)
        regime        = get_current_regime()

        log.info(f"check_ticker({ticker}): spot={spot} expirations={len(chains)} "
                 f"earnings={has_earnings} candles={len(candle_closes)} "
                 f"direction={direction} regime={regime.get('label', '?')}")

        all_recs    = []
        all_reasons = []

        for exp, dte, contracts in chains:
            rec = recommend_trade(
                ticker=ticker,
                spot=spot,
                contracts=contracts,
                dte=dte,
                expiration=exp,
                webhook_data=webhook_data,
                has_earnings=has_earnings,
                has_dividend=has_dividend,
                candle_closes=candle_closes,
                regime=regime,
            )

            if rec.get("ok"):
                all_recs.append(rec)
                log.info(f"  {exp} (DTE {dte}): ✅ {direction} trade found — "
                         f"${rec['trade']['debit']:.2f} on ${rec['trade']['width']} wide, "
                         f"RoR {rec['trade']['ror']:.0%}")
            else:
                reason = rec.get("reason", "unknown")
                all_reasons.append(f"DTE {dte} ({exp}): {reason}")
                log.info(f"  {exp} (DTE {dte}): ❌ {reason}")

        if not all_recs:
            combined_reason = "No valid spreads across any expiration"
            if all_reasons:
                combined_reason += "\n" + "\n".join(all_reasons[:4])

            trade_journal.log_signal(
                ticker, webhook_data, outcome="rejected",
                reason=combined_reason[:200],
            )

            return {
                "ticker":     ticker,
                "ok":         False,
                "posted":     False,
                "reason":     combined_reason,
                "confidence": None,
            }

        def rec_score(r):
            trade      = r.get("trade", {})
            ror        = trade.get("ror", 0)
            width      = trade.get("width", 5)
            dte_val    = r.get("dte", 5)
            width_bonus = 0.3 if width <= 1.0 else 0.1 if width <= 2.5 else 0
            dte_bonus   = 0.1 * (1.0 / (1 + abs(dte_val - TARGET_DTE)))
            return ror + width_bonus + dte_bonus

        all_recs.sort(key=rec_score, reverse=True)
        best_rec = all_recs[0]

        other_exps = []
        seen_exps  = {best_rec.get("exp")}
        for r in all_recs[1:]:
            if r.get("exp") not in seen_exps:
                seen_exps.add(r.get("exp"))
                other_exps.append(r)

        trade = best_rec.get("trade", {})

        if is_duplicate_trade(ticker, direction, trade.get("short"), trade.get("long")):
            trade_journal.log_signal(
                ticker, webhook_data, outcome="duplicate",
                confidence=best_rec.get("confidence"),
            )
            return {
                "ticker": ticker,
                "ok":     True,
                "posted": False,
                "reason": "Duplicate trade in dedup window",
            }

        risk_result = risk_manager.check_risk_limits(
            ticker=ticker,
            debit=trade.get("debit", 0),
            contracts=best_rec.get("contracts", 1),
            regime=regime,
            direction=direction,
        )

        card = format_trade_card(best_rec)

        if not risk_result["allowed"]:
            block_reasons = "; ".join(risk_result["blocks"])
            risk_warning  = "🚫 RISK LIMIT HIT — DO NOT ENTER\n" + block_reasons + "\n\n"
            card = risk_warning + card

        if risk_result.get("warnings"):
            card += "\n⚠️ Risk: " + " | ".join(risk_result["warnings"][:3])

        if other_exps:
            alt_lines = ["\n📅 Other Expirations:"]
            for r in other_exps[:3]:
                t = r.get("trade", {})
                alt_lines.append(
                    f"  DTE {r['dte']} ({r['exp']}): "
                    f"${t['debit']:.2f} on ${t['width']} wide | "
                    f"RoR {t['ror']:.0%} | {t['long']}/{t['short']}"
                )
            card += "\n".join(alt_lines)

        if has_earnings and earnings_warn:
            card = earnings_warn + "\n\n" + card

        conf = best_rec.get("confidence", 0)
        log.info(f"Trade card built: {ticker} {direction} conf={conf}/100 — queued for wave digest")

        # Mark dedup now so duplicate signals in the same wave are suppressed
        mark_trade_sent(ticker, direction, trade.get("short"), trade.get("long"))

        trade_journal.log_signal(
            ticker, webhook_data,
            outcome="trade_opened" if risk_result["allowed"] else "risk_blocked",
            confidence=best_rec.get("confidence"),
        )

        return {
            "ticker":              ticker,
            "ok":                  True,
            "posted":              True,   # will be posted via digest
            "card":                card,   # returned so digest poster can send it
            "confidence":          best_rec.get("confidence"),
            "trade":               trade,
            "expirations_checked": len(chains),
        }

    except Exception as e:
        log.error(f"check_ticker({ticker}): {type(e).__name__}: {e}")
        return {
            "ticker": ticker,
            "ok":     False,
            "posted": False,
            "error":  f"{type(e).__name__}: {str(e)[:160]}",
        }


# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    r = _get_redis()
    queue_depth = r.llen(REDIS_QUEUE_KEY) if r else _mem_signal_queue.qsize()
    return jsonify({
        "status": "ok",
        "redis": r is not None,
        "queue_depth": queue_depth,
        "workers": QUEUE_WORKERS,
    })


@app.route("/debug", methods=["GET"])
def debug():
    r = _get_redis()
    queue_depth = r.llen(REDIS_QUEUE_KEY) if r else _mem_signal_queue.qsize()
    return jsonify({
        "TELEGRAM_set":          bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "MARKETDATA_set":        bool(MARKETDATA_TOKEN),
        "REDIS_connected":       r is not None,
        "REDIS_queue_depth":     queue_depth,
        "WATCHLIST_len":         len([t for t in WATCHLIST.split(",") if t.strip()]),
        "ENGINE_VERSION":        "v3.9",
        "QUEUE_WORKERS":         QUEUE_WORKERS,
        "SIGNAL_TTL_S":          SIGNAL_TTL_SEC,
        "DEDUP_TTL_S":           DEDUP_TTL_SECONDS,
        "ALLOWED_DIRECTIONS":    ALLOWED_DIRECTIONS,
        "PORTFOLIO_CHANNEL_set": bool(TELEGRAM_PORTFOLIO_CHAT_ID),
        "MOM_CHANNEL_set":       bool(TELEGRAM_MOM_PORTFOLIO_CHAT_ID),
    })


@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram("✅ Telegram test OK (v3.9 Redis queue + swing)")
    return jsonify({"status": st, "body": body})


# ─────────────────────────────────────────────────────────
# TELEGRAM WEBHOOK
# ─────────────────────────────────────────────────────────

@app.route("/telegram_webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    if secret != os.getenv("TELEGRAM_WEBHOOK_SECRET", ""):
        return jsonify({"error": "Unauthorized"}), 403

    data    = request.get_json(silent=True) or {}
    message = data.get("message") or data.get("edited_message") or {}
    if not message:
        return jsonify({"ok": True})

    text    = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str(message.get("from", {}).get("id", ""))

    if not text.startswith("/"):
        return jsonify({"ok": True})

    # ── Command dedup guard ──
    # A fresh daemon thread is spawned per command. Under a burst (network
    # retry, double-tap, or Telegram re-delivery) the same command can arrive
    # 2-3 times within seconds, triggering duplicate chain fetches and double
    # Telegram posts. Suppress identical commands from the same user within a
    # 8-second window using the TTL store (works in both Redis and mem-fallback
    # mode since _MemStore now honours TTL correctly).
    cmd_word  = text.split()[0].lower() if text.split() else text.lower()
    dedup_key = f"cmd_dedup:{user_id}:{cmd_word}"
    if store_exists(dedup_key):
        log.info(f"Command dedup hit — suppressing repeat: {cmd_word} from user {user_id}")
        return jsonify({"ok": True})
    store_set(dedup_key, "1", ttl=8)

    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]

    def run_command():
        handle_command(
            user_id      = user_id,
            chat_id      = chat_id,
            text         = text,
            scan_fn      = lambda t: check_ticker(t),
            full_scan_fn = lambda: scan_watchlist_internal(tickers),
            check_fn     = check_ticker,
            watchlist    = tickers,
            get_spot_fn  = get_spot,
            md_get_fn    = md_get,
            post_fn      = post_to_telegram,
            get_portfolio_chat_id_fn = get_portfolio_chat_id,
            get_regime_fn    = get_current_regime,
            post_em_card_fn  = _post_em_card,
            post_monitor_card_fn = _post_monitor_card,
        )

    threading.Thread(target=run_command, daemon=True).start()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────
# TRADINGVIEW WEBHOOK — scalp signals (/tv)
# ─────────────────────────────────────────────────────────

@app.route("/tv", methods=["POST"])
def tv_webhook():
    data = request.get_json(silent=True) or {}

    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    # ── Payload validation ──
    # Reject early with a clear reason rather than letting bad data
    # propagate through the engine and produce silent garbage output.
    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "").strip().lower()
    tier   = str(data.get("tier") or "").strip()
    close  = as_float(data.get("close"), None)

    validation_errors = []
    if not ticker:
        validation_errors.append("missing 'ticker'")
    elif not ticker.replace(".", "").isalpha():
        validation_errors.append(f"invalid ticker '{ticker}' — must be alphabetic")
    if bias not in ("bull", "bear"):
        validation_errors.append(f"invalid bias '{bias}' — must be 'bull' or 'bear'")
    if tier not in ("1", "2", "3"):
        validation_errors.append(f"invalid tier '{tier}' — must be '1', '2', or '3'")
    if close is None or close <= 0:
        validation_errors.append(f"invalid close '{data.get('close')}' — must be a positive number")

    if validation_errors:
        reason = "; ".join(validation_errors)
        log.warning(f"TV webhook rejected — {reason} | raw={data}")
        return jsonify({"error": "invalid_payload", "reason": reason}), 400

    log.info(f"TV signal: {ticker} bias={bias} tier={tier} close={close}")
    log.info(f"TV webhook_data: htf_confirmed={data.get('htf_confirmed')} htf_converging={data.get('htf_converging')} daily_bull={data.get('daily_bull')} wt2={data.get('wt2')} rsi_mfi_bull={data.get('rsi_mfi_bull')} above_vwap={data.get('above_vwap')}")

    webhook_data = {
        "tier":           tier,
        "bias":           bias,
        "close":          close,
        "time":           data.get("time", ""),
        "ema5":           as_float(data.get("ema5")),
        "ema12":          as_float(data.get("ema12")),
        "ema_dist_pct":   as_float(data.get("ema_dist_pct")),
        "macd_hist":      as_float(data.get("macd_hist")),
        "macd_line":      as_float(data.get("macd_line")),
        "signal_line":    as_float(data.get("signal_line")),
        "wt1":            as_float(data.get("wt1")),
        "wt2":            as_float(data.get("wt2")),
        "rsi_mfi":        as_float(data.get("rsi_mfi")),
        "rsi_mfi_bull":   data.get("rsi_mfi_bull") in (True, "true"),
        "stoch_k":        as_float(data.get("stoch_k")),
        "stoch_d":        as_float(data.get("stoch_d")),
        "vwap":           as_float(data.get("vwap")),
        "above_vwap":     data.get("above_vwap") in (True, "true"),
        "htf_confirmed":  data.get("htf_confirmed") in (True, "true"),
        "htf_converging": data.get("htf_converging") in (True, "true"),
        "daily_bull":     data.get("daily_bull") in (True, "true"),
        "volume":         as_float(data.get("volume")),
        "timeframe":      data.get("timeframe", ""),
    }

    def _build_signal_msg():
        tier_emoji = "🥇" if tier == "1" else "🥈" if tier == "2" else "📢"
        wt2        = webhook_data.get("wt2") or 0
        wave_zone  = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
        trend_str  = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                      else "🟡 Converging" if webhook_data["htf_converging"]
                      else "❌ Diverging")
        dir_emoji  = "🐻" if bias == "bear" else "🐂"
        return "\n".join([
            f"{tier_emoji} TV Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
            f"Close: ${close:.2f} | {data.get('timeframe', '')} timeframe",
            f"1H Trend: {trend_str} | Daily: {'🟢' if webhook_data['daily_bull'] else '🔴'}",
            f"Wave: {wave_zone} (wt2={wt2:.1f})",
            f"VWAP: {'Above ✅' if webhook_data['above_vwap'] else 'Below'} | "
            f"RSI+MFI: {'Buying ✅' if webhook_data['rsi_mfi_bull'] else 'Selling'}",
            "",
        ])

    signal_msg = _build_signal_msg()
    _enqueue_signal("tv", ticker, bias, webhook_data, signal_msg)

    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# SWING WEBHOOK (/swing)
# ─────────────────────────────────────────────────────────

@app.route("/swing", methods=["POST"])
def swing_webhook():
    data = request.get_json(silent=True) or {}

    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    # ── Payload validation ──
    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "").strip().lower()
    tier   = str(data.get("tier") or "").strip()
    close  = as_float(data.get("close"), None)

    validation_errors = []
    if not ticker:
        validation_errors.append("missing 'ticker'")
    elif not ticker.replace(".", "").isalpha():
        validation_errors.append(f"invalid ticker '{ticker}' — must be alphabetic")
    if bias not in ("bull", "bear"):
        validation_errors.append(f"invalid bias '{bias}' — must be 'bull' or 'bear'")
    if tier not in ("1", "2", "3"):
        validation_errors.append(f"invalid tier '{tier}' — must be '1', '2', or '3'")
    if close is None or close <= 0:
        validation_errors.append(f"invalid close '{data.get('close')}' — must be a positive number")

    if validation_errors:
        reason = "; ".join(validation_errors)
        log.warning(f"Swing webhook rejected — {reason} | raw={data}")
        return jsonify({"error": "invalid_payload", "reason": reason}), 400

    log.info(f"Swing signal: {ticker} bias={bias} tier={tier} "
             f"fib={data.get('fib_level')} dist={data.get('fib_distance_pct')}%")

    webhook_data = {
        "tier":             tier,
        "bias":             bias,
        "close":            close,
        "time":             data.get("time", ""),
        "fib_level":        data.get("fib_level", "61.8"),
        "fib_distance_pct": as_float(data.get("fib_distance_pct"), 2.0),
        "fib_high":         as_float(data.get("fib_high")),
        "fib_low":          as_float(data.get("fib_low")),
        "fib_range":        as_float(data.get("fib_range")),
        "fib_ext_127":      as_float(data.get("fib_ext_127")),
        "fib_ext_162":      as_float(data.get("fib_ext_162")),
        "weekly_bull":      data.get("weekly_bull") in (True, "true"),
        "weekly_bear":      data.get("weekly_bear") in (True, "true"),
        "htf_confirmed":    data.get("htf_confirmed") in (True, "true"),
        "htf_converging":   data.get("htf_converging") in (True, "true"),
        "daily_bull":       data.get("daily_bull") in (True, "true"),
        "rsi":              as_float(data.get("rsi")),
        "rsi_mfi_bull":     data.get("rsi_mfi_bull") in (True, "true"),
        "vol_contracting":  data.get("vol_contracting") in (True, "true"),
        "vol_expanding":    data.get("vol_expanding") in (True, "true"),
        "volume":           as_float(data.get("volume")),
        "timeframe":        data.get("timeframe", "D"),
    }

    tier_emoji = "🥇" if tier == "1" else "🥈"
    dir_emoji  = "🐻" if bias == "bear" else "🐂"
    fib_level  = webhook_data["fib_level"]
    fib_emojis = {"61.8": "🌟", "50.0": "⭐", "38.2": "✨", "78.6": "💫"}
    fib_emoji  = fib_emojis.get(str(fib_level), "📐")
    weekly_str = "🟢 Bull" if webhook_data["weekly_bull"] else "🔴 Bear"
    daily_str  = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                  else "🟡 Converging" if webhook_data["htf_converging"]
                  else "❌ Diverging")
    vol_str    = "🟢 Contracting" if webhook_data["vol_contracting"] else "📊 Normal"

    signal_lines = [
        f"{tier_emoji} SWING Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
        f"Fib: {fib_emoji} {fib_level}% level ({webhook_data['fib_distance_pct']:.1f}% away)",
        f"Close: ${close:.2f} | {webhook_data['timeframe']} chart",
        f"Weekly: {weekly_str} | Daily: {daily_str}",
        f"Volume: {vol_str}",
        "",
    ]
    signal_msg = "\n".join(signal_lines)

    _enqueue_signal("swing", ticker, bias, webhook_data, signal_msg)

    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# 0DTE EM — MANUAL TRIGGER ROUTE
# ─────────────────────────────────────────────────────────

@app.route("/em", methods=["POST", "GET"])
def em_trigger():
    """Manual trigger for 0DTE EM cards. Useful for testing."""
    data     = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()

    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in
               (data.get("tickers") or ",".join(EM_TICKERS)).split(",") if t.strip()]
    session = data.get("session", "manual")

    def run():
        for ticker in tickers:
            _post_em_card(ticker, session)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "accepted", "tickers": tickers, "session": session})


# ─────────────────────────────────────────────────────────
# WATCHLIST SCAN — DISABLED
# ─────────────────────────────────────────────────────────

def scan_watchlist_internal(tickers: list, max_posts: int = 6):
    log.info("scan_watchlist_internal called but scan is disabled — skipping")
    return


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    return jsonify({
        "status": "disabled",
        "reason": "Scheduled scan disabled — use TradingView webhooks only",
    }), 200


# ─────────────────────────────────────────────────────────
# HOLDINGS SENTIMENT SCAN
# ─────────────────────────────────────────────────────────

@app.route("/holdings_scan", methods=["POST"])
def holdings_scan():
    data     = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()

    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    def run_scan():
        from sentiment_report import generate_sentiment_report

        for account in ("brad", "mom"):
            try:
                report      = generate_sentiment_report(md_get, account=account)
                target_chat = get_portfolio_chat_id(account)
                post_to_telegram(report, chat_id=target_chat)
            except Exception as e:
                log.error(f"Holdings scan error ({account}): {e}")
                target_chat = get_portfolio_chat_id(account)
                post_to_telegram(
                    f"⚠️ Holdings scan failed ({account}): {type(e).__name__}",
                    chat_id=target_chat,
                )

    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "accepted"})


# ─────────────────────────────────────────────────────────
# 0DTE EXPECTED MOVE SCHEDULER — SPY & QQQ
# ─────────────────────────────────────────────────────────
# Fires at 8:45 AM and 2:45 PM Central Time on weekdays.
# Fetches ATM IV from the 0DTE options chain, calculates
# the expected move for the remainder of the day, and posts
# a clean card to Telegram.

EM_SCHEDULE_TIMES_CT = [
    (8,  45),   # 8:45 AM Central — pre-open EM
    (14, 45),   # 2:45 PM Central — afternoon EM check
]
EM_TICKERS = ["SPY", "QQQ"]


def _get_next_trading_day(from_date) -> str:
    """Return the next weekday date string after from_date (a date object)."""
    from datetime import timedelta as _td
    d = from_date + _td(days=1)
    while d.weekday() >= 5:
        d += _td(days=1)
    return d.strftime("%Y-%m-%d")


def _build_option_rows(data: dict, spot: float, days_to_exp: float) -> list:
    """
    Convert a MarketData.app chain response into a list of OptionRow objects
    for the ExposureEngine. Falls back to BS greeks if API fields are missing.
    """
    from options_exposure import OptionRow

    sym_list = data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return []

    def col(name, default=None):
        v = data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes = col("strike",       None)
    iv_list = col("iv",           None)
    oi_list = col("openInterest",  0)
    vol_l   = col("volume",        0)
    delta_l = col("delta",         None)
    gamma_l = col("gamma",         None)
    sides   = col("side",         "")

    rows = []
    for i in range(n):
        strike = as_float(strikes[i], 0)
        iv     = as_float(iv_list[i], 0)
        oi     = int(as_float(oi_list[i], 0))
        side   = str(sides[i] or "").lower()
        vol    = int(as_float(vol_l[i], 0))

        if strike <= 0 or iv <= 0 or side not in ("call", "put"):
            continue

        delta = as_float(delta_l[i], None) if delta_l[i] is not None else None
        gamma = as_float(gamma_l[i], None) if gamma_l[i] is not None else None

        rows.append(OptionRow(
            option_type      = side,
            strike           = strike,
            days_to_exp      = max(days_to_exp, 0.5),   # min 0.5 day for 0DTE BS stability
            iv               = iv,
            open_interest    = oi,
            underlying_price = spot,
            volume           = vol,
            delta            = delta,
            gamma            = gamma,
        ))
    return rows


# ─────────────────────────────────────────────────────────────────────
# CHAIN DATA CACHE
# ─────────────────────────────────────────────────────────────────────
# Options chain fetches are expensive: ~300–800 ms each, and they count
# against the MarketData.app rate limit. Without caching, firing /em SPY
# then /monitorshort SPY within seconds fetches the same chain twice.
# The scheduled 8:45 AM card for SPY + QQQ fires two concurrent fetches
# already — add any user monitor commands on top and quota burns fast.
#
# Cache keyed by (ticker, expiry_date) → (data, spot, expiry, fetched_at).
# TTL: 60 seconds — chain OI/IV moves slowly intraday; a 60s window is
# safe and eliminates nearly all redundant fetches during burst usage.
# Thread-safe via a simple lock.
# ─────────────────────────────────────────────────────────────────────

_CHAIN_CACHE_TTL   = 60           # seconds
_chain_cache: dict = {}           # (ticker, expiry) → (data, spot, expiry, fetched_at)
_chain_cache_lock  = threading.Lock()


def _chain_cache_get(ticker: str, expiry: str):
    """Return cached (data, spot, expiry) if fresh, else None."""
    key = (ticker.upper(), expiry)
    with _chain_cache_lock:
        entry = _chain_cache.get(key)
        if entry and (time.monotonic() - entry[3]) < _CHAIN_CACHE_TTL:
            log.debug(f"Chain cache HIT: {ticker} {expiry}")
            return entry[0], entry[1], entry[2]
        if entry:
            del _chain_cache[key]   # stale — evict immediately
    return None


def _chain_cache_set(ticker: str, expiry: str, data, spot: float):
    """Store a fresh chain fetch in the cache."""
    key = (ticker.upper(), expiry)
    with _chain_cache_lock:
        # Prune any stale entries while we have the lock (bounded memory)
        now = time.monotonic()
        stale = [k for k, v in _chain_cache.items() if (now - v[3]) >= _CHAIN_CACHE_TTL]
        for k in stale:
            del _chain_cache[k]
        _chain_cache[key] = (data, spot, expiry, now)
        log.debug(f"Chain cache SET: {ticker} {expiry} ({len(_chain_cache)} entries)")


def _get_0dte_chain(ticker: str, target_date_str: str = None) -> tuple:
    """
    Fetch the full options chain for a target expiration.
    Returns (data_dict, spot, resolved_expiration) or (None, None, None).
    Results are cached for _CHAIN_CACHE_TTL seconds to avoid redundant
    API calls when multiple commands touch the same ticker concurrently.
    """
    try:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target    = target_date_str or today_utc

        # ── Cache check (uses pre-resolved target so hit rate is high) ──
        cached = _chain_cache_get(ticker, target)
        if cached:
            return cached   # (data, spot, expiry)

        spot = get_spot(ticker)

        data = md_get(
            f"https://api.marketdata.app/v1/options/chain/{ticker}/",
            {"expiration": target},
        )
        if not isinstance(data, dict) or data.get("s") != "ok" or not data.get("optionSymbol"):
            exps        = get_expirations(ticker)
            future_exps = [e for e in exps if e >= target]
            if not future_exps:
                return None, None, None
            target = future_exps[0]

            # Check cache again with resolved target
            cached = _chain_cache_get(ticker, target)
            if cached:
                return cached

            data = md_get(
                f"https://api.marketdata.app/v1/options/chain/{ticker}/",
                {"expiration": target},
            )
            if not isinstance(data, dict) or data.get("s") != "ok":
                return None, None, None

        if not data.get("optionSymbol"):
            return None, None, None

        _chain_cache_set(ticker, target, data, spot)
        return data, spot, target

    except Exception as e:
        log.warning(f"Chain fetch failed for {ticker} (target={target_date_str}): {e}")
        return None, None, None


def _get_0dte_iv(ticker: str, target_date_str: str = None) -> tuple:
    """
    Full institutional-grade chain analysis.
    Returns (iv, spot, expiration, engine_result, skew, pcr, vix_data)
    or (None, None, None, None, {}, {}, {}) on failure.

    engine_result contains:
      - net.gex, net.dex, net.vanna, net.charm
      - walls.call_wall, walls.put_wall, walls.gamma_wall
      - by_strike map
      - flip_price (from proper grid sweep)
      - regime (plain English)
      - vanna_charm (plain English)
    """
    empty = (None, None, None, None, {}, {}, {})
    try:
        from options_exposure import ExposureEngine, gex_regime, vanna_charm_context

        data, spot, target = _get_0dte_chain(ticker, target_date_str)
        if data is None:
            return empty

        # Days to expiration — 0DTE = 0 days but we need > 0 for BS; use 0.5
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        from datetime import date as _date
        try:
            exp_date  = _date.fromisoformat(target)
            today_d   = _date.fromisoformat(today_utc)
            dte       = max((exp_date - today_d).days, 0)
        except Exception:
            dte = 0

        rows = _build_option_rows(data, spot, max(dte, 0.5))
        if not rows:
            return empty

        # ── ExposureEngine (proper BS + dealer sign conventions) ──
        engine = ExposureEngine(r=0.04)
        result = engine.compute(rows)

        # Gamma flip via price grid sweep (±10% of spot, $1 steps)
        lo   = int(spot * 0.90)
        hi   = int(spot * 1.10) + 1
        grid = [float(p) for p in range(lo, hi, 1)]
        flip = engine.gamma_flip(rows, grid)

        net          = result["net"]
        regime_info  = gex_regime(net["gex"])
        vc_info      = vanna_charm_context(net["vanna"], net["charm"])
        walls_raw    = result["walls"]

        # Build walls dict in same shape the rest of the code expects
        by_strike = result["by_strike"]
        walls = {}
        if walls_raw["call_wall"]:
            cw = walls_raw["call_wall"]
            walls["call_wall"]    = cw
            walls["call_wall_oi"] = by_strike[cw]["call_oi"]
            walls["call_top3"]    = sorted(by_strike, key=lambda k: by_strike[k]["call_oi"], reverse=True)[:3]
        if walls_raw["put_wall"]:
            pw = walls_raw["put_wall"]
            walls["put_wall"]    = pw
            walls["put_wall_oi"] = by_strike[pw]["put_oi"]
            walls["put_top3"]    = sorted(by_strike, key=lambda k: by_strike[k]["put_oi"], reverse=True)[:3]
        if walls_raw["gamma_wall"]:
            gw = walls_raw["gamma_wall"]
            walls["gamma_wall"]     = gw
            walls["gamma_wall_gex"] = by_strike[gw]["gex"]

        engine_result = {
            "gex":        round(net["gex"]   / 1_000_000, 2),   # $M
            "dex":        round(net["dex"]   / 1_000_000, 2),
            "vanna":      round(net["vanna"] / 1_000_000, 2),
            "charm":      round(net["charm"] / 1_000_000, 2),
            "flip_price": flip,
            "is_positive_gex": net["gex"] >= 0,
            "regime":     regime_info,
            "vc":         vc_info,
        }

        # ── ATM IV (average of options within 1% of spot) ──
        n     = len(data.get("optionSymbol") or [])
        def col(name, default=None):
            v = data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        iv_list = col("iv",     None)
        iv_sides= col("side",   "")
        oi_list = col("openInterest", 0)
        vol_l   = col("volume", 0)

        atm_ivs = []
        for pct in (0.005, 0.01, 0.02):
            atm_range = spot * pct
            for i in range(n):
                s  = as_float(strikes[i], 0)
                iv = as_float(iv_list[i],  0)
                if iv > 0 and abs(s - spot) <= atm_range:
                    atm_ivs.append(iv)
            if atm_ivs:
                break

        if not atm_ivs:
            return empty

        avg_iv = round(sum(atm_ivs) / len(atm_ivs), 4)
        skew   = _get_atm_skew(strikes, iv_list, iv_sides, n, spot)
        pcr    = _calc_pcr(oi_list, vol_l, iv_sides, n)
        vix    = _get_vix_data()

        return avg_iv, spot, target, engine_result, walls, skew, pcr, vix

    except Exception as e:
        log.warning(f"0DTE IV fetch failed for {ticker} (target={target_date_str}): {e}")
        return empty


def _get_atm_skew(strikes, iv_list, sides, n: int, spot: float) -> dict:
    """
    Compare ATM call IV vs ATM put IV within 2% of spot.
    Put IV > Call IV = fear premium (bearish lean).
    Call IV > Put IV = greed/momentum premium (bullish lean).
    """
    call_ivs = []
    put_ivs  = []
    atm_range = spot * 0.02

    for i in range(n):
        strike = as_float(strikes[i], 0)
        iv     = as_float(iv_list[i],  0)
        side   = str(sides[i] or "").lower()
        if iv <= 0 or abs(strike - spot) > atm_range:
            continue
        if side == "call":
            call_ivs.append(iv)
        elif side == "put":
            put_ivs.append(iv)

    result = {}
    if call_ivs:
        result["call_iv"] = round(sum(call_ivs) / len(call_ivs) * 100, 1)
    if put_ivs:
        result["put_iv"]  = round(sum(put_ivs)  / len(put_ivs)  * 100, 1)
    return result


def _calc_pcr(oi_list, vol_l, sides, n: int) -> dict:
    """
    Put/Call Ratio by OI and Volume.
    PCR > 1.2 = fear/bearish.  PCR < 0.7 = greed/bullish.
    """
    call_oi = call_vol = put_oi = put_vol = 0
    for i in range(n):
        oi   = int(as_float(oi_list[i], 0))
        vol  = int(as_float(vol_l[i],   0))
        side = str(sides[i] or "").lower()
        if side == "call":
            call_oi  += oi
            call_vol += vol
        elif side == "put":
            put_oi  += oi
            put_vol += vol

    return {
        "put_oi":   put_oi,
        "call_oi":  call_oi,
        "put_vol":  put_vol,
        "call_vol": call_vol,
        "pcr_oi":   round(put_oi  / call_oi,  2) if call_oi  > 0 else None,
        "pcr_vol":  round(put_vol / call_vol, 2) if call_vol > 0 else None,
    }


def _get_vix_data() -> dict:
    """
    Fetch VIX and VIX9D.
    VIX9D > VIX = near-term fear spike (move happening NOW).
    VIX9D < VIX = near-term calm, longer-dated concern.
    """
    try:
        def _parse_quote(data):
            for field in ("last", "mid", "bid"):
                v = data.get(field)
                if isinstance(v, list):
                    v = v[0] if v else None
                val = as_float(v, 0)
                if val > 0:
                    return val
            return 0.0

        vix   = _parse_quote(md_get("https://api.marketdata.app/v1/stocks/quotes/VIX/"))
        vix9d = _parse_quote(md_get("https://api.marketdata.app/v1/stocks/quotes/VIX9D/"))

        if vix <= 0:
            return {}

        if vix9d > 0:
            if vix9d > vix * 1.05:
                term = "inverted"
            elif vix9d < vix * 0.95:
                term = "normal"
            else:
                term = "flat"
        else:
            term = "unknown"

        return {
            "vix":  round(vix,  1),
            "vix9d": round(vix9d, 1) if vix9d > 0 else None,
            "term": term,
        }

    except Exception as e:
        log.debug(f"VIX fetch failed: {e}")
        return {}


def _calc_bias(spot: float, em: dict, walls: dict, skew: dict,
               eng: dict, pcr: dict, vix: dict) -> dict:
    """
    Institutional dealer-flow bias engine — v3.

    PHILOSOPHY: Every calculated value gets used. Missing data skips that
    signal rather than diluting the read with a neutral score. The result
    biases toward giving traders a clear directional verdict — NEUTRAL only
    means the data is genuinely split, not that we gave up.

    SIGNAL MAP (max possible score: ±14)
    ─────────────────────────────────────────────────────────────────────
    GROUP 1 — DEALER MECHANICS (highest reliability, price-forcing flows)
      S1  GEX flip position       ±2   are dealers amplifying or suppressing?
      S2  DEX direction           ±2   where must dealers trade if price moves?
      S3  Vanna flow              ±1   IV-change forced dealer buying/selling
      S4  Charm flow              +-1  time-decay forced dealer hedging today
      S5  GEX regime context       0   informational only — no score
    GROUP 2 — OPTIONS POSITIONING (institutional OI commitment)
      S6  OI wall asymmetry       ±1   which side has more big-money hedging?
      S7  Spot vs wall midpoint   ±1   where are we inside the range?
      S8  Gamma wall magnet       ±1   pin or drift tendency
      S9  Secondary wall cluster   0   informational only — no score
    GROUP 3 — SENTIMENT FLOW (real-time conviction)
      S10 IV skew                 ±1   what are people paying to protect?
      S11 PCR by OI               ±1   net options positioning
      S12 PCR by Volume           ±1   today's live flow vs yesterday's OI
    GROUP 4 — MACRO BACKDROP (context, size management)
      S13 VIX level               ±2   absolute fear gauge
      S14 VIX term structure      ±1   near-term vs 30-day fear relationship
    ─────────────────────────────────────────────────────────────────────
    VERDICT THRESHOLDS (out of ±14):
      ≥ +7  STRONG BULLISH   (high conviction, size normally)
      ≥ +3  BULLISH          (moderate conviction, normal entries)
      ≥ +1  SLIGHT BULLISH   (weak edge, tighter stops)
       = 0  NEUTRAL          (no edge, reduce size or wait)
      ≤ -1  SLIGHT BEARISH   (weak edge, tighter stops)
      ≤ -3  BEARISH          (moderate conviction, normal entries)
      ≤ -7  STRONG BEARISH   (high conviction, size normally)
    """
    score   = 0
    signals = []

    # ══════════════════════════════════════════════════════════════════
    # GROUP 1 — DEALER MECHANICS
    # These are the highest-reliability signals because they represent
    # flows that MUST happen — dealers are not discretionary.
    # ══════════════════════════════════════════════════════════════════

    # S1 — GEX Flip Position (weight ±2)
    # This is the single most important signal on the card.
    # The gamma flip is the price where dealers switch from suppressing
    # volatility to amplifying it. Being above = vol suppression regime.
    # Being below = every move gets amplified and chased by dealers.
    if eng and eng.get("flip_price"):
        fp       = eng["flip_price"]
        dist_pct = ((spot - fp) / fp) * 100
        tgex     = eng.get("gex", 0)
        if spot > fp:
            score += 2
            ctx = "range-bound bias" if tgex > 0 else "above flip but still negative GEX — trending likely"
            signals.append(("▲▲", f"[FLIP +2] Price ${spot:.2f} is {abs(dist_pct):.1f}% ABOVE gamma flip ${fp:.2f} — {ctx}. Dealers suppress volatility above this level."))
        else:
            score -= 2
            signals.append(("▼▼", f"[FLIP -2] Price ${spot:.2f} is {abs(dist_pct):.1f}% BELOW gamma flip ${fp:.2f} — dealers AMPLIFY every move from here. Momentum and breakout setups favored."))
    elif eng and "gex" in eng:
        tgex = eng["gex"]
        if tgex > 0:
            score += 1
            signals.append(("▲", f"[GEX +1] No flip found but net GEX is positive (${tgex:.1f}M) — dealers are net long gamma, suppressing moves."))
        else:
            score -= 1
            signals.append(("▼", f"[GEX -1] No flip found and net GEX is negative (-${abs(tgex):.1f}M) — dealers are net short gamma, amplifying moves."))

    # S2 — DEX Direction (weight ±2)
    # DEX = Dollar Delta Exposure. This tells you the SIZE and DIRECTION
    # of dealer delta hedges that are outstanding. When price moves, dealers
    # must re-hedge — and that re-hedging CREATES price pressure.
    # Negative DEX = dealers short delta = they BUY as price rises (fuel for rallies).
    # Positive DEX = dealers long delta = they SELL as price falls (fuel for drops).
    if eng and "dex" in eng:
        dex  = eng["dex"]
        adex = abs(dex)
        if dex < -1.0:
            score += 2
            signals.append(("▲▲", f"[DEX +2] Dealers are net SHORT delta (DEX -${adex:.1f}M) — they MUST BUY shares as price rises. Every rally gets mechanical buying fuel added."))
        elif dex < -0.25:
            score += 1
            signals.append(("▲", f"[DEX +1] Dealers mildly short delta (DEX -${adex:.1f}M) — some buying fuel on upside moves."))
        elif dex > 1.0:
            score -= 2
            signals.append(("▼▼", f"[DEX -2] Dealers are net LONG delta (DEX +${dex:.1f}M) — they MUST SELL shares as price falls. Every drop gets mechanical selling added."))
        elif dex > 0.25:
            score -= 1
            signals.append(("▼", f"[DEX -1] Dealers mildly long delta (DEX +${dex:.1f}M) — some selling pressure on downside moves."))
        else:
            signals.append(("◆", f"[DEX  0] Dealers near delta-neutral (DEX ${dex:+.1f}M) — no strong forced re-hedging flow in either direction."))

    # S3 — Vanna Flow (weight ±1)
    # Vanna = d(delta)/d(vol). When IV changes, Vanna drives dealer re-hedging.
    # On high-IV days or vol spikes this can overwhelm price action.
    # Vanna tailwind = rising IV forces dealers to BUY (bullish pressure from vol).
    # Vanna headwind = rising IV forces dealers to SELL (bearish pressure from vol).
    if eng and "vanna" in eng:
        vanna_m = eng["vanna"]
        if vanna_m > 0.5:
            score += 1
            signals.append(("▲", f"[VANNA +1] Net Vanna ${vanna_m:+.1f}M — if IV rises today, dealer re-hedging adds BUYING pressure. Vol spikes will support price."))
        elif vanna_m < -0.5:
            score -= 1
            signals.append(("▼", f"[VANNA -1] Net Vanna ${vanna_m:+.1f}M — if IV rises today, dealer re-hedging adds SELLING pressure. Vol spikes will pressure price."))
        else:
            signals.append(("◆", f"[VANNA  0] Net Vanna ${vanna_m:+.1f}M — minimal IV-driven dealer flow expected."))

    # S4 — Charm Flow (weight ±1)
    # Charm = d(delta)/d(time). Dealer hedges decay with time.
    # Charm headwind is the reason for the famous "3:30 PM drift" — as time
    # passes on a 0DTE day, charm unwinds either ADD or REMOVE selling pressure.
    # On 0DTE days this can be the dominant afternoon force.
    if eng and "charm" in eng:
        charm_m = eng["charm"]
        if charm_m > 0.5:
            score += 1
            signals.append(("▲", f"[CHARM +1] Net Charm ${charm_m:+.1f}M — as today's session progresses, time-decay removes dealer short hedges. Watch for afternoon drift UP (classic 3:30 PM move)."))
        elif charm_m < -0.5:
            score -= 1
            signals.append(("▼", f"[CHARM -1] Net Charm ${charm_m:+.1f}M — as today's session progresses, time-decay ADDS dealer short hedges. Watch for afternoon drift DOWN (charm headwind into close)."))
        else:
            signals.append(("◆", f"[CHARM  0] Net Charm ${charm_m:+.1f}M — time decay has minimal directional effect on dealer hedges today."))

    # S5 — GEX Regime Context (informational, no score)
    if eng and "gex" in eng:
        tgex = eng["gex"]
        regime = eng.get("regime", {})
        preferred = regime.get("preferred", "")
        avoid     = regime.get("avoid", "")
        if tgex >= 0:
            signals.append(("◆", f"[GEX REGIME] POSITIVE ${tgex:.1f}M — MM suppress moves. Favors: {preferred}. Avoid: {avoid}."))
        else:
            signals.append(("⚡", f"[GEX REGIME] NEGATIVE -${abs(tgex):.1f}M — MM amplify moves. Favors: {preferred}. Avoid: {avoid}."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 2 — OPTIONS POSITIONING
    # These signals reflect where institutional money has committed
    # hedges — less real-time than dealer mechanics but very sticky.
    # ══════════════════════════════════════════════════════════════════

    # S6 — OI Wall Asymmetry (weight ±1)
    # Which side has more institutional protection committed?
    # A dominant put wall means big money paid to protect the downside —
    # that creates a floor. A dominant call wall creates a ceiling.
    if walls and "call_wall_oi" in walls and "put_wall_oi" in walls:
        cw_oi = walls["call_wall_oi"]
        pw_oi = walls["put_wall_oi"]
        ratio = pw_oi / cw_oi if cw_oi > 0 else 1.0
        if ratio >= 1.25:
            score += 1
            signals.append(("▲", f"[OI ASYM +1] Put wall OI ({pw_oi:,}) dominates call wall OI ({cw_oi:,}) by {ratio:.1f}x — heavy downside protection paid for. Strong buy-the-dip positioning."))
        elif ratio >= 1.10:
            signals.append(("◆", f"[OI ASYM  0] Slight put OI edge ({pw_oi:,} vs {cw_oi:,}, {ratio:.1f}x) — mild downside protection lean. Not conclusive."))
        elif ratio <= 0.80:
            score -= 1
            signals.append(("▼", f"[OI ASYM -1] Call wall OI ({cw_oi:,}) dominates put wall OI ({pw_oi:,}) by {1/ratio:.1f}x — heavy upside hedging. Strong sell-the-rip positioning."))
        elif ratio <= 0.90:
            signals.append(("◆", f"[OI ASYM  0] Slight call OI edge ({cw_oi:,} vs {pw_oi:,}, {1/ratio:.1f}x) — mild upside hedging lean. Not conclusive."))
        else:
            signals.append(("◆", f"[OI ASYM  0] OI is balanced (put {pw_oi:,} vs call {cw_oi:,}, {ratio:.2f}x) — no dominant institutional lean."))

    # S7 — Spot vs Wall Midpoint (weight ±1)
    # Simple positional bias: are we closer to resistance or support?
    if walls and "call_wall" in walls and "put_wall" in walls:
        mid      = (walls["call_wall"] + walls["put_wall"]) / 2
        dist_pct = ((spot - mid) / mid) * 100
        if dist_pct >= 0.30:
            score += 1
            signals.append(("▲", f"[MIDPOINT +1] Price ${spot:.2f} is {dist_pct:.1f}% above midpoint ${mid:.2f} — positioned in upper half of the dealer range. Bullish bias within structure."))
        elif dist_pct <= -0.30:
            score -= 1
            signals.append(("▼", f"[MIDPOINT -1] Price ${spot:.2f} is {abs(dist_pct):.1f}% below midpoint ${mid:.2f} — positioned in lower half of the dealer range. Bearish bias within structure."))
        else:
            signals.append(("◆", f"[MIDPOINT  0] Price ${spot:.2f} near midpoint ${mid:.2f} ({dist_pct:+.1f}%) — no positional edge within the range."))

    # S8 — Gamma Wall as Price Magnet (weight ±1)
    # The strike with the highest absolute GEX is the strongest dealer
    # hedging concentration. Price tends to gravitate toward it intraday.
    if walls and "gamma_wall" in walls:
        gw           = walls["gamma_wall"]
        gw_dist_pct  = ((gw - spot) / spot) * 100
        if abs(gw_dist_pct) <= 0.30:
            signals.append(("◆", f"[GAMMA WALL  0] Gamma wall ${gw:.0f} is very close to spot ({gw_dist_pct:+.1f}%) — price is PINNED. Expect tight chop around this strike today."))
        elif gw > spot:
            score += 1
            signals.append(("▲", f"[GAMMA WALL +1] Gamma wall ${gw:.0f} is {gw_dist_pct:.1f}% ABOVE spot — acts as upside magnet. Price may drift toward it during the session."))
        else:
            score -= 1
            signals.append(("▼", f"[GAMMA WALL -1] Gamma wall ${gw:.0f} is {abs(gw_dist_pct):.1f}% BELOW spot — acts as downside magnet. Price may drift toward it during the session."))

    # S9 — Secondary Wall Clusters (informational, no score)
    # Shows the full OI cluster on each side — not just the top wall.
    if walls and "call_top3" in walls and "put_top3" in walls:
        ct3 = " → ".join(f"${x:.0f}" for x in walls["call_top3"])
        pt3 = " → ".join(f"${x:.0f}" for x in walls["put_top3"])
        signals.append(("◆", f"[CLUSTERS] Resistance stack: {ct3} | Support stack: {pt3}"))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 3 — SENTIMENT FLOW
    # These signals reflect what the market is actually paying for
    # and doing with options — real-time conviction indicators.
    # ══════════════════════════════════════════════════════════════════

    # S10 — IV Skew (weight ±1)
    # The premium difference between ATM calls and ATM puts.
    # When puts cost significantly more, the market is paying a fear premium.
    # When calls cost more, it signals upside chase / FOMO.
    if skew and "call_iv" in skew and "put_iv" in skew:
        diff = skew["put_iv"] - skew["call_iv"]
        if diff >= 2.5:
            score -= 1
            signals.append(("▼", f"[SKEW -1] Strong fear skew: puts {skew['put_iv']}% vs calls {skew['call_iv']}% (+{diff:.1f}pp) — market paying heavy premium to hedge downside. Genuine fear."))
        elif diff >= 1.0:
            signals.append(("◆", f"[SKEW  0] Mild fear skew: puts {skew['put_iv']}% vs calls {skew['call_iv']}% (+{diff:.1f}pp) — normal put premium, no strong signal."))
        elif diff <= -2.5:
            score += 1
            signals.append(("▲", f"[SKEW +1] Greed skew: calls {skew['call_iv']}% vs puts {skew['put_iv']}% ({abs(diff):.1f}pp) — market paying heavy premium to chase upside. Genuine momentum."))
        elif diff <= -1.0:
            signals.append(("◆", f"[SKEW  0] Mild greed skew: calls {skew['call_iv']}% vs puts {skew['put_iv']}% ({abs(diff):.1f}pp) — slight call premium, no strong signal."))
        else:
            signals.append(("◆", f"[SKEW  0] IV balanced: calls {skew.get('call_iv','?')}% / puts {skew.get('put_iv','?')}% ({diff:+.1f}pp) — no directional conviction from skew."))

    # S11 — PCR by OI (weight ±1)
    # Put/Call ratio by open interest = cumulative positioning from prior days.
    # Represents the structural lean of the market's existing hedges.
    if pcr and pcr.get("pcr_oi") is not None:
        p = pcr["pcr_oi"]
        if p > 1.35:
            score -= 1
            signals.append(("▼", f"[PCR OI -1] PCR(OI) {p:.2f} — very high put skew. Market is structurally positioned defensively. Bearish sentiment is baked into existing positions."))
        elif p > 1.1:
            signals.append(("◆", f"[PCR OI  0] PCR(OI) {p:.2f} — mildly elevated puts. Defensive lean but not extreme."))
        elif p < 0.65:
            score += 1
            signals.append(("▲", f"[PCR OI +1] PCR(OI) {p:.2f} — very low, call-dominant positioning. Market is structurally bullish in existing positions."))
        elif p < 0.85:
            signals.append(("◆", f"[PCR OI  0] PCR(OI) {p:.2f} — mildly elevated calls. Bullish lean but not extreme."))
        else:
            signals.append(("◆", f"[PCR OI  0] PCR(OI) {p:.2f} — balanced positioning. No strong structural sentiment."))

    # S12 — PCR by Volume (weight ±1)
    # TODAY's live options flow — more current than OI which is yesterday's data.
    # This tells you what traders are doing right NOW, not what they did before.
    if pcr and pcr.get("pcr_vol") is not None:
        pv = pcr["pcr_vol"]
        if pv > 1.35:
            score -= 1
            signals.append(("▼", f"[PCR VOL -1] PCR(Vol) {pv:.2f} — traders are ACTIVELY buying puts today. Real-time bearish flow — more urgent signal than OI."))
        elif pv > 1.1:
            signals.append(("◆", f"[PCR VOL  0] PCR(Vol) {pv:.2f} — slightly more put volume today. Mild defensive flow."))
        elif pv < 0.65:
            score += 1
            signals.append(("▲", f"[PCR VOL +1] PCR(Vol) {pv:.2f} — traders are ACTIVELY buying calls today. Real-time bullish flow — more urgent signal than OI."))
        elif pv < 0.85:
            signals.append(("◆", f"[PCR VOL  0] PCR(Vol) {pv:.2f} — slightly more call volume today. Mild bullish flow."))
        else:
            signals.append(("◆", f"[PCR VOL  0] PCR(Vol) {pv:.2f} — balanced today's flow. No real-time directional conviction."))

    # ══════════════════════════════════════════════════════════════════
    # GROUP 4 — MACRO BACKDROP
    # These signals set context for how much to trust the other signals
    # and how to size. High VIX = wider EM, less predictable pinning.
    # ══════════════════════════════════════════════════════════════════

    # S13 — VIX Level (weight ±2)
    # VIX is the market's official fear gauge. At extremes it directly
    # impacts how reliable the other signals are and what size to use.
    if vix and vix.get("vix"):
        v    = vix["vix"]
        v9d  = vix.get("vix9d")
        term = vix.get("term", "unknown")

        if v >= 40:
            score -= 2
            signals.append(("▼▼", f"[VIX -2] VIX {v} — EXTREME fear/crisis. EM ranges may be too small. Dealer hedging breaks down at these levels. Consider sitting out or minimum size."))
        elif v >= 28:
            score -= 1
            signals.append(("▼", f"[VIX -1] VIX {v} — elevated fear. Market is unstable. Use wider stops, smaller size. EM may understate risk."))
        elif v >= 18:
            signals.append(("◆", f"[VIX  0] VIX {v} — above-average uncertainty. Normal risk management. EM ranges are appropriate."))
        elif v >= 12:
            score += 1
            signals.append(("▲", f"[VIX +1] VIX {v} — calm environment. Low fear, orderly market. Dealer hedging is predictable. EM ranges are reliable."))
        else:
            score += 2
            signals.append(("▲▲", f"[VIX +2] VIX {v} — extremely low fear. Market is complacent. Dealer flows are very predictable. EM ranges highly reliable."))

        # S14 — VIX Term Structure (weight ±1)
        # The relationship between near-term (VIX9D) and 30-day (VIX) fear.
        # Inverted = near-term fear exceeds long-term = something breaking NOW.
        # Normal = near-term calmer = today should be more stable than recent history.
        if v9d and term == "inverted":
            score -= 1
            delta_v = round(v9d - v, 1)
            signals.append(("▼", f"[TERM -1] VIX term INVERTED — VIX9D {v9d} is {delta_v}pt ABOVE VIX {v}. Near-term fear exceeds 30-day average. Something is breaking down RIGHT NOW. High urgency warning."))
        elif v9d and term == "normal":
            score += 1
            delta_v = round(v - v9d, 1)
            signals.append(("▲", f"[TERM +1] VIX term normal — VIX9D {v9d} is {delta_v}pt BELOW VIX {v}. Near-term is calmer than the 30-day average. Today should be relatively stable."))
        elif v9d and term == "flat":
            signals.append(("◆", f"[TERM  0] VIX term flat — VIX9D {v9d} ≈ VIX {v}. Consistent fear across timeframes, no term structure edge."))

    # ══════════════════════════════════════════════════════════════════
    # VERDICT — bias toward giving a clear read
    # Thresholds calibrated against max score of ±14.
    # Only NEUTRAL means we genuinely cannot pick a side.
    # ══════════════════════════════════════════════════════════════════
    up_count   = sum(1 for e, _ in signals if e in ("▲", "▲▲"))
    down_count = sum(1 for e, _ in signals if e in ("▼", "▼▼"))
    neu_count  = len(signals) - up_count - down_count

    if score >= 7:
        direction = "STRONG BULLISH"
        strength  = "High Conviction"
        verdict   = (
            "High-conviction bullish setup. Dealer mechanics, positioning, and sentiment all align. "
            "Favor ITM call debit spreads or bull setups. Size normally with standard stops."
        )
    elif score >= 3:
        direction = "BULLISH"
        strength  = "Moderate"
        verdict   = (
            "Solid bullish lean. Multiple independent signals favor upside. "
            "Prefer bull setups. Size normally but confirm with price action before entry."
        )
    elif score >= 1:
        direction = "SLIGHT BULLISH"
        strength  = "Weak"
        verdict   = (
            "Marginal bullish edge. More signals favor upside than down but conviction is low. "
            "Take bull setups only on clean entries. Tighter stops than normal."
        )
    elif score == 0:
        direction = "NEUTRAL"
        strength  = ""
        verdict   = (
            "Signals are genuinely split. No structural edge in either direction. "
            "Range-bound or unpredictable chop likely. Reduce size significantly or wait for a cleaner setup."
        )
    elif score >= -2:
        direction = "SLIGHT BEARISH"
        strength  = "Weak"
        verdict   = (
            "Marginal bearish edge. More signals favor downside than up but conviction is low. "
            "Take bear setups only on clean entries. Tighter stops than normal."
        )
    elif score >= -6:
        direction = "BEARISH"
        strength  = "Moderate"
        verdict   = (
            "Solid bearish lean. Multiple independent signals favor downside. "
            "Prefer bear setups. Size normally but confirm with price action before entry."
        )
    else:
        direction = "STRONG BEARISH"
        strength  = "High Conviction"
        verdict   = (
            "High-conviction bearish setup. Dealer mechanics, positioning, and sentiment all align. "
            "Favor ITM put debit spreads or bear setups. Size normally with standard stops."
        )

    return {
        "direction":  direction,
        "strength":   strength,
        "score":      score,
        "max_score":  14,
        "up_count":   up_count,
        "down_count": down_count,
        "neu_count":  neu_count,
        "n_signals":  len(signals),
        "signals":    signals,
        "verdict":    verdict,
    }


def _calc_intraday_em(spot: float, iv: float, hours_remaining: float) -> dict:
    """
    EM = Spot × IV × sqrt(hours / trading_hours_per_year)
    Trading year = 252 days × 6.5h = 1,638h
    1σ = 68% probability price stays inside range
    2σ = 95% probability price stays inside range
    """
    if iv <= 0 or hours_remaining <= 0:
        return {}

    hours_in_year = 252 * 6.5
    em_1sd = round(spot * iv * math.sqrt(hours_remaining / hours_in_year), 2)
    em_2sd = round(em_1sd * 2, 2)

    return {
        "em_1sd":      em_1sd,
        "em_2sd":      em_2sd,
        "bull_1sd":    round(spot + em_1sd, 2),
        "bear_1sd":    round(spot - em_1sd, 2),
        "bull_2sd":    round(spot + em_2sd, 2),
        "bear_2sd":    round(spot - em_2sd, 2),
        "em_pct_1sd":  round((em_1sd / spot) * 100, 2),
        "hours_used":  round(hours_remaining, 2),
    }


def _post_em_card(ticker: str, session: str):
    """
    Institutional-grade EM card — v3.
    Every calculated field is displayed. Nothing wasted.
    Morning  (8:45 AM CT): TODAY's expiration, hours remaining.
    Afternoon (2:45 PM CT): NEXT trading day, full 6.5h session.
    """
    try:
        import pytz
        ct       = pytz.timezone("America/Chicago")
        now_ct   = datetime.now(ct)
        today_dt = now_ct.date()

        is_afternoon = (session == "afternoon")

        if is_afternoon:
            target_date_str = _get_next_trading_day(today_dt)
            hours_for_em    = 6.5
            session_emoji   = "🌆"
            session_label   = "Next Day Preview"
            horizon_note    = f"Full session EM for {target_date_str}"
        else:
            target_date_str = today_dt.strftime("%Y-%m-%d")
            market_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
            hours_for_em    = max((market_close_ct - now_ct).total_seconds() / 3600, 0.25)
            session_emoji   = "🌅"
            session_label   = "Today (Pre-Open)"
            horizon_note    = f"{hours_for_em:.1f}h remaining today"

        iv, spot, expiration, eng, walls, skew, pcr, vix = _get_0dte_iv(ticker, target_date_str)

        if iv is None or spot is None:
            log.warning(f"EM card skipped for {ticker}: IV unavailable (target={target_date_str})")
            post_to_telegram(f"⚠️ {ticker} EM: could not fetch IV for {target_date_str}")
            return

        em   = _calc_intraday_em(spot, iv, hours_for_em)
        if not em:
            return

        bias = _calc_bias(spot, em, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        iv_pct = iv * 100
        if iv_pct < 10:
            iv_emoji = "🟢"
            iv_note  = "Low IV — tight range expected. EM may be conservative."
        elif iv_pct < 20:
            iv_emoji = "🟡"
            iv_note  = "Moderate IV — normal session. EM ranges are reliable."
        elif iv_pct < 35:
            iv_emoji = "🔴"
            iv_note  = "Elevated IV — wider swings. Respect your stops and size down."
        else:
            iv_emoji = "🚨"
            iv_note  = "EXTREME IV — EM may understate true range. Minimum size or stand aside."

        # ════════════════════════════════════════
        # HEADER
        # ════════════════════════════════════════
        lines = [
            f"{session_emoji} {ticker} — Institutional EM Brief ({session_label})",
            f"Spot: ${spot:.2f}  |  IV: {iv_emoji} {iv_pct:.1f}%  |  Exp: {expiration}",
            f"Session: {horizon_note}",
        ]
        if vix and vix.get("vix"):
            v    = vix["vix"]
            v9d  = vix.get("vix9d")
            term = vix.get("term", "")
            term_str = {"inverted": " 🚨 INVERTED", "normal": " ✅ normal", "flat": " flat"}.get(term, "")
            v9d_str  = f"  VIX9D: {v9d}{term_str}" if v9d else ""
            lines.append(f"VIX: {v}{v9d_str}")

        # ════════════════════════════════════════
        # EXPECTED MOVE RANGES
        # ════════════════════════════════════════
        lines += [
            "",
            "─" * 32,
            "📐 EXPECTED MOVE",
            f"  1σ (68%):  🐂 ${em['bull_1sd']:.2f}  ←  ${spot:.2f}  →  🐻 ${em['bear_1sd']:.2f}",
            f"             ±${em['em_1sd']:.2f}  ({em['em_pct_1sd']:.2f}%)",
            f"  2σ (95%):  🐂 ${em['bull_2sd']:.2f}  ←→  🐻 ${em['bear_2sd']:.2f}",
            f"             ±${em['em_2sd']:.2f}",
        ]

        # ════════════════════════════════════════
        # DEALER MECHANICS BLOCK
        # Every single exposure number displayed.
        # ════════════════════════════════════════
        if eng:
            tgex     = eng.get("gex", 0)
            dex      = eng.get("dex", 0)
            vanna_m  = eng.get("vanna", 0)
            charm_m  = eng.get("charm", 0)
            flip     = eng.get("flip_price")
            regime   = eng.get("regime", {})

            gex_icon = "🧲" if tgex >= 0 else "⚡"
            gex_sign = f"+${tgex:.1f}M" if tgex >= 0 else f"-${abs(tgex):.1f}M"
            gex_mode = "SUPPRESSING moves (range-bound)" if tgex >= 0 else "AMPLIFYING moves (trending)"

            dex_sign = f"+${dex:.1f}M" if dex >= 0 else f"-${abs(dex):.1f}M"
            dex_note = "dealers LONG delta → must SELL on drops (adds to selling)" if dex >= 0 else "dealers SHORT delta → must BUY on rallies (adds fuel to upside)"

            vanna_sign = f"+${vanna_m:.1f}M" if vanna_m >= 0 else f"-${abs(vanna_m):.1f}M"
            vanna_note = "IV spike → dealer BUYING (bullish)" if vanna_m >= 0 else "IV spike → dealer SELLING (bearish)"

            charm_sign = f"+${charm_m:.1f}M" if charm_m >= 0 else f"-${abs(charm_m):.1f}M"
            charm_note = "time passes → removes sell hedges (bullish drift)" if charm_m >= 0 else "time passes → adds sell hedges (bearish drift into close)"

            lines += [
                "",
                "─" * 32,
                "⚙️ DEALER FLOW",
                f"  {gex_icon} GEX:   {gex_sign}  —  dealers are {gex_mode}",
                f"  📍 Flip:  ${flip:.2f}  ({'above' if spot > flip else 'BELOW'} — {'suppression' if spot > flip else 'amplification'} regime)" if flip else "  📍 Flip:  not found in ±10% range",
                f"  📊 DEX:   {dex_sign}  —  {dex_note}",
                f"  🌊 Vanna: {vanna_sign}  —  {vanna_note}",
                f"  ⏱️ Charm: {charm_sign}  —  {charm_note}",
            ]
            if regime.get("preferred"):
                lines.append(f"  ✅ Favors: {regime['preferred']}")
            if regime.get("avoid"):
                lines.append(f"  ❌ Avoid:  {regime['avoid']}")

        # ════════════════════════════════════════
        # KEY LEVELS — OI walls + gamma wall + clusters + pin zone
        # ════════════════════════════════════════
        if walls:
            lines += ["", "─" * 32, "🧱 KEY LEVELS"]

            if "call_wall" in walls:
                cw    = walls["call_wall"]
                cw_oi = walls["call_wall_oi"]
                dist  = ((cw - spot) / spot) * 100
                if cw <= em["bull_1sd"]:
                    cw_tag = "⚠️ INSIDE 1σ — will cap the rally"
                elif cw <= em["bull_2sd"]:
                    cw_tag = "within 2σ — reachable on a strong move"
                else:
                    cw_tag = "outside 2σ — upside is clear today"
                lines.append(f"  📵 Resistance: ${cw:.0f}  ({cw_oi:,} OI, +{dist:.1f}%) — {cw_tag}")

            if "put_wall" in walls:
                pw    = walls["put_wall"]
                pw_oi = walls["put_wall_oi"]
                dist  = ((spot - pw) / spot) * 100
                if pw >= em["bear_1sd"]:
                    pw_tag = "⚠️ INSIDE 1σ — will stop the drop"
                elif pw >= em["bear_2sd"]:
                    pw_tag = "within 2σ — reachable on a strong selloff"
                else:
                    pw_tag = "outside 2σ — limited floor below"
                lines.append(f"  🛡️ Support:    ${pw:.0f}  ({pw_oi:,} OI, -{dist:.1f}%) — {pw_tag}")

            if "gamma_wall" in walls:
                gw       = walls["gamma_wall"]
                gw_dist  = ((gw - spot) / spot) * 100
                gw_label = "ABOVE" if gw > spot else "BELOW"
                lines.append(f"  🎯 Gamma Wall: ${gw:.0f}  ({gw_dist:+.1f}% {gw_label} spot) — strongest dealer magnet, price gravitates here")

            if "call_top3" in walls:
                ct3 = "  →  ".join(f"${x:.0f}" for x in walls["call_top3"])
                lines.append(f"  📵 Resistance stack: {ct3}")
            if "put_top3" in walls:
                pt3 = "  →  ".join(f"${x:.0f}" for x in walls["put_top3"])
                lines.append(f"  🛡️ Support stack:    {pt3}")

            if (
                "call_wall" in walls and "put_wall" in walls
                and walls["call_wall"] <= em["bull_1sd"]
                and walls["put_wall"]  >= em["bear_1sd"]
            ):
                pin_w = walls["call_wall"] - walls["put_wall"]
                lines.append(f"  📌 PIN ZONE: ${walls['put_wall']:.0f} – ${walls['call_wall']:.0f}  (${pin_w:.0f} wide) — both walls inside 1σ, gravitational pull all day")

        # ════════════════════════════════════════
        # SENTIMENT — skew + both PCR values
        # ════════════════════════════════════════
        skew_str = ""
        if skew and "call_iv" in skew and "put_iv" in skew:
            diff = skew["put_iv"] - skew["call_iv"]
            skew_str = f"  IV Skew: Calls {skew['call_iv']}%  /  Puts {skew['put_iv']}%  ({diff:+.1f}pp {'fear' if diff > 0 else 'greed'})"

        pcr_oi_str  = f"OI {pcr['pcr_oi']:.2f}"   if pcr and pcr.get("pcr_oi")  is not None else "OI n/a"
        pcr_vol_str = f"Vol {pcr['pcr_vol']:.2f}"  if pcr and pcr.get("pcr_vol") is not None else "Vol n/a"

        lines += [
            "",
            "─" * 32,
            "📊 SENTIMENT",
            f"  PCR:  {pcr_oi_str}  |  {pcr_vol_str}  (>1.2 fear · <0.8 greed)",
        ]
        if skew_str:
            lines.append(skew_str)

        # ════════════════════════════════════════
        # DIRECTIONAL LEAN — full signal breakdown
        # ════════════════════════════════════════
        dir_emoji = {
            "STRONG BULLISH": "🟢🟢",
            "BULLISH":        "🟢",
            "SLIGHT BULLISH": "🟡",
            "NEUTRAL":        "⚪",
            "SLIGHT BEARISH": "🟠",
            "BEARISH":        "🔴",
            "STRONG BEARISH": "🔴🔴",
        }.get(bias["direction"], "⚪")

        strength_str = f"  [{bias['strength']}]" if bias.get("strength") else ""
        score_str    = f"{bias['score']:+d}/{bias['max_score']}"
        dot_bar      = ("▲" * bias["up_count"]) + ("▼" * bias["down_count"]) + ("◆" * bias["neu_count"])

        lines += [
            "",
            "═" * 32,
            f"{dir_emoji}  LEAN: {bias['direction']}{strength_str}",
            f"Score: {score_str}  |  {dot_bar}",
            f"  ▲ {bias['up_count']} bullish  ·  ▼ {bias['down_count']} bearish  ·  ◆ {bias['neu_count']} neutral",
            "",
            f"📋 {bias['verdict']}",
            "",
            "── Signal Breakdown ──",
        ]
        for arrow, text in bias["signals"]:
            lines.append(f"  {arrow}  {text}")
        lines += [
            "═" * 32,
            "",
            f"💡 {iv_note}",
            "— Not financial advice —",
        ]

        post_to_telegram("\n".join(lines))
        log.info(
            f"EM card posted: {ticker} | {session_label} | spot={spot} | IV={iv_pct:.1f}% | "
            f"EM=±${em['em_1sd']} | GEX={eng.get('gex') if eng else 'N/A'} | "
            f"DEX={eng.get('dex') if eng else 'N/A'} | "
            f"flip={eng.get('flip_price') if eng else 'N/A'} | "
            f"score={bias['score']} | lean={bias['direction']} | exp={expiration}"
        )

        # ── Trade card posted immediately after EM card (same data, no new API call) ──
        _post_trade_card(
            ticker     = ticker,
            spot       = spot,
            expiration = expiration,
            eng        = eng or {},
            walls      = walls or {},
            bias       = bias,
            em         = em,
            vix        = vix or {},
            pcr        = pcr or {},
            is_0dte    = True,
        )

    except Exception as e:
        log.error(f"EM card error for {ticker} ({session}): {e}", exc_info=True)





# ─────────────────────────────────────────────────────────────────────
# LIQUID SYMBOL DETECTION
# Full institutional card (GEX/DEX/Vanna/Charm) on these — they have
# deep enough chains for the exposure engine to produce reliable reads.
# Everything else gets a simplified card.
# ─────────────────────────────────────────────────────────────────────

LIQUID_SYMBOLS = {
    "SPY", "QQQ", "SPX", "NDX",
    "AAPL", "NVDA", "TSLA", "AMZN", "META", "MSFT", "GOOGL", "GOOG",
    "AMD", "NFLX", "COIN", "MSTR", "ARM", "PLTR", "SMCI",
}

def _is_liquid(ticker: str) -> bool:
    return ticker.upper() in LIQUID_SYMBOLS


# ─────────────────────────────────────────────────────────────────────
# EXPIRY SELECTION
# ─────────────────────────────────────────────────────────────────────

def _find_expiry_nearest(ticker: str) -> tuple:
    """
    /monitorshort — find the nearest available expiration regardless of DTE.
    Returns (expiry_str, dte_int) or (None, None).
    """
    try:
        from datetime import date as _date
        today      = _date.today()
        today_str  = today.strftime("%Y-%m-%d")
        exps       = get_expirations(ticker)
        future     = [e for e in exps if e >= today_str]
        if not future:
            return None, None
        exp = future[0]
        dte = (_date.fromisoformat(exp) - today).days
        return exp, dte
    except Exception as e:
        log.warning(f"_find_expiry_nearest failed for {ticker}: {e}")
        return None, None


def _find_expiry_closest_to_21(ticker: str) -> tuple:
    """
    /monitorlong — find the expiration closest to 21 days (standard monthly).
    Must be at least 15 days out. Returns (expiry_str, dte_int) or (None, None).
    """
    try:
        from datetime import date as _date
        today     = _date.today()
        today_str = today.strftime("%Y-%m-%d")
        exps      = get_expirations(ticker)
        candidates = []
        for e in exps:
            dte = (_date.fromisoformat(e) - today).days
            if dte >= 15:
                candidates.append((abs(dte - 21), dte, e))
        if not candidates:
            return None, None
        candidates.sort()
        _, dte, exp = candidates[0]
        return exp, dte
    except Exception as e:
        log.warning(f"_find_expiry_closest_to_21 failed for {ticker}: {e}")
        return None, None


# ─────────────────────────────────────────────────────────────────────
# CHAIN FETCH FOR ANY EXPIRY (not 0DTE-specific)
# ─────────────────────────────────────────────────────────────────────

def _get_chain_for_expiry(ticker: str, target_date_str: str) -> tuple:
    """
    Fetch options chain for a specific expiration date.
    Returns (data_dict, spot, resolved_expiration) or (None, None, None).
    Works for any DTE — 0DTE, weekly, monthly.
    Results are cached for _CHAIN_CACHE_TTL seconds.
    """
    try:
        cached = _chain_cache_get(ticker, target_date_str)
        if cached:
            return cached

        spot = get_spot(ticker)
        data = md_get(
            f"https://api.marketdata.app/v1/options/chain/{ticker}/",
            {"expiration": target_date_str},
        )
        if not isinstance(data, dict) or data.get("s") != "ok" or not data.get("optionSymbol"):
            return None, None, None

        _chain_cache_set(ticker, target_date_str, data, spot)
        return data, spot, target_date_str
    except Exception as e:
        log.warning(f"Chain fetch failed for {ticker} exp={target_date_str}: {e}")
        return None, None, None


def _get_chain_iv_for_expiry(ticker: str, target_date_str: str, dte: float) -> tuple:
    """
    Full chain analysis for any expiry. Same pipeline as _get_0dte_iv but
    accepts pre-resolved expiry date and DTE. Returns the same 8-tuple.
    Returns (iv, spot, expiration, engine_result, walls, skew, pcr, vix) or empties.
    """
    empty = (None, None, None, None, {}, {}, {}, {})
    try:
        from options_exposure import ExposureEngine, gex_regime, vanna_charm_context

        data, spot, target = _get_chain_for_expiry(ticker, target_date_str)
        if data is None:
            return empty

        rows = _build_option_rows(data, spot, max(dte, 0.5))
        if not rows:
            return empty

        engine = ExposureEngine(r=0.04)
        result = engine.compute(rows)

        lo   = int(spot * 0.90)
        hi   = int(spot * 1.10) + 1
        grid = [float(p) for p in range(lo, hi, 1)]
        flip = engine.gamma_flip(rows, grid)

        net         = result["net"]
        regime_info = gex_regime(net["gex"])
        vc_info     = vanna_charm_context(net["vanna"], net["charm"])
        walls_raw   = result["walls"]
        by_strike   = result["by_strike"]

        walls = {}
        if walls_raw["call_wall"]:
            cw = walls_raw["call_wall"]
            walls["call_wall"]    = cw
            walls["call_wall_oi"] = by_strike[cw]["call_oi"]
            walls["call_top3"]    = sorted(by_strike, key=lambda k: by_strike[k]["call_oi"], reverse=True)[:3]
        if walls_raw["put_wall"]:
            pw = walls_raw["put_wall"]
            walls["put_wall"]    = pw
            walls["put_wall_oi"] = by_strike[pw]["put_oi"]
            walls["put_top3"]    = sorted(by_strike, key=lambda k: by_strike[k]["put_oi"], reverse=True)[:3]
        if walls_raw["gamma_wall"]:
            gw = walls_raw["gamma_wall"]
            walls["gamma_wall"]     = gw
            walls["gamma_wall_gex"] = by_strike[gw]["gex"]

        engine_result = {
            "gex":        round(net["gex"]   / 1_000_000, 2),
            "dex":        round(net["dex"]   / 1_000_000, 2),
            "vanna":      round(net["vanna"] / 1_000_000, 2),
            "charm":      round(net["charm"] / 1_000_000, 2),
            "flip_price": flip,
            "is_positive_gex": net["gex"] >= 0,
            "regime":     regime_info,
            "vc":         vc_info,
        }

        # ATM IV
        n       = len(data.get("optionSymbol") or [])
        def col(name, default=None):
            v = data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes  = col("strike", None)
        iv_list  = col("iv",     None)
        iv_sides = col("side",   "")
        oi_list  = col("openInterest", 0)
        vol_l    = col("volume", 0)

        atm_ivs = []
        for pct in (0.01, 0.02, 0.03):
            atm_range = spot * pct
            for i in range(n):
                s  = as_float(strikes[i], 0)
                iv = as_float(iv_list[i],  0)
                if iv > 0 and abs(s - spot) <= atm_range:
                    atm_ivs.append(iv)
            if atm_ivs:
                break

        if not atm_ivs:
            return empty

        avg_iv = round(sum(atm_ivs) / len(atm_ivs), 4)
        skew   = _get_atm_skew(strikes, iv_list, iv_sides, n, spot)
        pcr    = _calc_pcr(oi_list, vol_l, iv_sides, n)
        vix    = _get_vix_data()

        return avg_iv, spot, target, engine_result, walls, skew, pcr, vix

    except Exception as e:
        log.warning(f"Chain IV fetch failed for {ticker} exp={target_date_str}: {e}")
        return empty


# ─────────────────────────────────────────────────────────────────────
# TRADE CARD — posted immediately after the EM card
# Takes already-fetched data — zero extra API calls.
# ─────────────────────────────────────────────────────────────────────

def _post_trade_card(
    ticker: str,
    spot: float,
    expiration: str,
    eng: dict,
    walls: dict,
    bias: dict,
    em: dict,
    vix: dict,
    pcr: dict,
    is_0dte: bool = True,
):
    """
    0DTE trade recommendation card — v2.

    WIDTH LOGIC (fixed):
      Width is determined by the underlying price, NOT by wall distance.
      Walls are targets, not spread legs. This prevents absurdly wide spreads.

      SPX/NDX:          $15–$25 wide
      SPY/QQQ (>$400):  $4–$6 wide
      High-price stocks (>$300): $5–$10 wide
      Mid-price stocks ($50–$300): $3–$5 wide
      Low-price stocks (<$50):    $1–$2 wide

      Width is then tightened by 50% when GEX is positive (range-bound day)
      because the expected move is being suppressed.

    GATE LOGIC (blocks the trade before posting):
      G1 — NEUTRAL bias:               no trade, sit out
      G2 — SLIGHT with score < 2:      no trade, wait for setup
      G3 — Flip mismatch (bull below flip OR bear above flip): no trade, wrong side
      G4 — VIX >= 40:                  no trade, stand aside
      G5 — GEX positive + score < 5:   downgrade to monitoring only (not enough edge
                                        to fight dealer suppression on a 0DTE spread)

    STRIKE LOGIC (fixed):
      Long leg: ATM or first ITM strike (~0.55–0.65Δ proxy)
        - Round to nearest $1 strike for stocks < $100
        - Round to nearest $5 strike for ETFs and stocks $100–$500
        - Round to nearest $25 for SPX/NDX
      Short leg: long_strike + width (bull) or long_strike - width (bear)
      Wall is a TARGET, not the short leg.
    """
    try:
        direction = bias.get("direction", "NEUTRAL")
        score     = bias.get("score", 0)
        is_bull   = "BULL" in direction

        tgex      = eng.get("gex", 0)      if eng else 0
        dex       = eng.get("dex", 0)      if eng else 0
        flip      = eng.get("flip_price")  if eng else None
        charm_m   = eng.get("charm", 0)    if eng else 0
        neg_gex   = tgex < 0
        v         = vix.get("vix", 20)     if vix else 20

        exp_short = expiration[5:] if expiration else "?"

        def no_trade(reason: str, emoji: str = "⚪"):
            post_to_telegram(
                f"🎯 {ticker} — 0DTE TRADE SETUP  |  Exp: {exp_short}\n"
                f"{emoji} NO TRADE — {reason}\n"
                f"— Not financial advice —"
            )
            log.info(f"Trade card blocked: {ticker} | {reason}")

        # ════════════════════════════
        # GATE CHECKS — block before any strike math
        # ════════════════════════════

        # G1 — No directional edge
        if direction == "NEUTRAL":
            no_trade(
                f"Bias NEUTRAL (score {score:+d}/14). No directional edge.\n"
                f"If GEX is positive and walls are tight, a small iron condor between\n"
                f"${walls.get('put_wall','?')} and ${walls.get('call_wall','?')} could work — but no debit spread today.",
                "⚪"
            )
            return

        # G2 — Edge too thin
        if direction in ("SLIGHT BULLISH", "SLIGHT BEARISH") and abs(score) < 2:
            no_trade(
                f"Lean {direction} but score is only {score:+d}/14 — edge too thin.\n"
                f"Wait for a price action confirmation or a score of at least ±3 before entering.",
                "🟡" if is_bull else "🟠"
            )
            return

        # G3 — Flip mismatch: spot is on the wrong side of the flip for this direction
        if flip:
            flip_wrong = (is_bull and spot < flip) or (not is_bull and spot > flip)
            if flip_wrong:
                side_word = "BELOW" if is_bull else "ABOVE"
                no_trade(
                    f"Spot ${spot:.2f} is {side_word} the gamma flip ${flip:.2f}.\n"
                    f"This means dealers are in AMPLIFICATION mode against your direction.\n"
                    f"Do not enter a {'bull' if is_bull else 'bear'} spread while spot is {side_word} the flip.\n"
                    f"Wait for spot to reclaim ${flip:.2f} before entering.",
                    "🔴" if is_bull else "🔴"
                )
                return

        # G4 — VIX extreme
        if v >= 40:
            no_trade(
                f"VIX {v} is extreme — EM ranges are unreliable at these levels.\n"
                f"Stand aside until VIX drops below 35.",
                "🚨"
            )
            return

        # G5 — Positive GEX + weak score = suppressed moves, not enough edge
        if not neg_gex and abs(score) < 5:
            no_trade(
                f"GEX is POSITIVE (+${tgex:.1f}M) and score is only {score:+d}/14.\n"
                f"Positive GEX suppresses intraday moves — debit spreads need momentum.\n"
                f"This setup doesn't have enough conviction to fight dealer suppression.\n"
                f"Either wait for score ≥ ±5, or wait for GEX to flip negative.",
                "🧲"
            )
            return

        # ════════════════════════════
        # WIDTH — price-based, tightened on positive GEX
        # ════════════════════════════
        ticker_up = ticker.upper()
        if ticker_up in ("SPX", "NDX"):
            base_width = 20
            step       = 5
        elif spot >= 400:        # SPY, QQQ, high-price ETFs
            base_width = 5
            step       = 1
        elif spot >= 200:
            base_width = 5
            step       = 1
        elif spot >= 100:
            base_width = 3
            step       = 1
        elif spot >= 50:
            base_width = 2
            step       = 1
        else:
            base_width = 1
            step       = 1

        # Tighten on positive GEX (suppressed moves — less range to profit from)
        width = base_width if neg_gex else max(base_width // 2, step)

        # ════════════════════════════
        # STRIKE SELECTION
        # Long leg: first ITM strike at ~step intervals
        # Short leg: long ± width (never the wall — wall is a target)
        # ════════════════════════════
        if is_bull:
            # Round spot down to nearest step for long strike (ITM call)
            long_strike  = (int(spot) // step) * step
            if long_strike >= spot:
                long_strike -= step
            short_strike = long_strike + width
            spread_type  = "CALL DEBIT SPREAD"
            long_label   = f"${long_strike:.0f}C"
            short_label  = f"${short_strike:.0f}C"
            # Targets: gamma wall first, then call wall (if inside spread)
            gwall = walls.get("gamma_wall")
            cwall = walls.get("call_wall")
            target1 = gwall if (gwall and long_strike < gwall <= short_strike) else short_strike
            target2 = cwall if (cwall and cwall > short_strike) else None
            stop_level = flip if flip else round(spot * (1 - 0.007), 2)
        else:
            # Round spot up to nearest step for long strike (ITM put)
            long_strike  = (int(spot) // step) * step
            if long_strike <= spot:
                long_strike += step
            short_strike = long_strike - width
            spread_type  = "PUT DEBIT SPREAD"
            long_label   = f"${long_strike:.0f}P"
            short_label  = f"${short_strike:.0f}P"
            gwall = walls.get("gamma_wall")
            pwall = walls.get("put_wall")
            target1 = gwall if (gwall and short_strike <= gwall < long_strike) else short_strike
            target2 = pwall if (pwall and pwall < short_strike) else None
            stop_level = flip if flip else round(spot * (1 + 0.007), 2)

        actual_width = abs(short_strike - long_strike)
        # ITM 0DTE debit spread: cost ~55–70% of width (deep ITM = higher % of width)
        # Use 60% as a realistic mid estimate
        cost_est   = round(actual_width * 0.60, 2)
        max_profit = round(actual_width - cost_est, 2)
        rr         = round(max_profit / cost_est, 2) if cost_est > 0 else 0

        # ════════════════════════════
        # SIZE — VIX tier + DEX confirmation
        # ════════════════════════════
        if v >= 28:
            base_pct = 25
        elif v >= 20:
            base_pct = 50
        elif v >= 15:
            base_pct = 75
        else:
            base_pct = 100

        dex_confirms  = (is_bull and dex < -0.25) or (not is_bull and dex > 0.25)
        dex_disagrees = (is_bull and dex > 0.25)  or (not is_bull and dex < -0.25)
        if dex_confirms:
            size_pct = min(base_pct + 25, 100)
            dex_note = "DEX confirms direction → +1 tier"
        elif dex_disagrees:
            size_pct = max(base_pct - 25, 25)
            dex_note = "DEX disagrees → -1 tier"
        else:
            size_pct = base_pct
            dex_note = "DEX neutral — no adjustment"

        # ════════════════════════════
        # TIMING from Charm
        # ════════════════════════════
        charm_tail = charm_m > 0
        if charm_tail:
            timing_note = "Charm tailwind — afternoon drift works for you. Hold into 2:30 PM CT."
            timing_warn = "Exit by 2:45 PM CT to avoid final gamma acceleration."
        else:
            timing_note = "Charm headwind — do NOT hold into the close."
            timing_warn = "⚠️ EXIT by noon CT. Time decay adds selling pressure as day progresses."

        # ════════════════════════════
        # GEX regime note
        # ════════════════════════════
        if neg_gex:
            gex_note = f"⚡ Negative GEX (-${abs(tgex):.1f}M) — dealers AMPLIFY moves. Debit spreads confirmed."
        else:
            gex_note = f"🧲 Positive GEX (+${tgex:.1f}M) — dealers suppress moves. Width tightened to ${actual_width:.0f}."

        # ════════════════════════════
        # CHECKLIST — all pass by this point (gates already blocked failures)
        # ════════════════════════════
        checks = [
            f"✅ Bias: {direction} (score {score:+d}/14)",
            f"✅ GEX: {'negative — momentum confirmed' if neg_gex else f'positive but score ≥ 5 — proceeding with tight width'}",
            f"✅ Spot ${spot:.2f} {'above' if is_bull else 'below'} flip ${flip:.2f} — correct side" if flip else f"✅ No flip found — using price-based stop ${stop_level:.2f}",
            f"✅ DEX: {dex_note}",
            f"✅ VIX {v} → base size {base_pct}%",
        ]

        # ════════════════════════════
        # INVALIDATION
        # ════════════════════════════
        if flip:
            inval_price = f"gamma flip ${flip:.2f}"
            inval_note  = f"If spot crosses the gamma flip (${flip:.2f}) against you → exit immediately, no averaging"
        else:
            inval_note  = f"If spread loses 50% of cost (${round(cost_est*0.5,2):.2f}) → stop out"

        dir_emoji = {
            "STRONG BULLISH": "🟢🟢", "BULLISH": "🟢", "SLIGHT BULLISH": "🟡",
            "SLIGHT BEARISH": "🟠", "BEARISH": "🔴", "STRONG BEARISH": "🔴🔴",
        }.get(direction, "⚪")

        lines = [
            f"🎯 {ticker} — 0DTE TRADE SETUP",
            f"Generated: {datetime.now().strftime('%I:%M %p CT')}  |  Exp: {exp_short}",
            f"Lean: {dir_emoji} {direction}  [Score: {score:+d}/14]",
            "━" * 32,
            "",
            f"⚙️ REGIME: {gex_note}",
            "",
            f"📋 SETUP: ITM {spread_type}",
            f"  Buy:   {ticker} {long_label}  (ITM, ~0.60Δ)",
            f"  Sell:  {ticker} {short_label}  ({actual_width:.0f}-wide spread)",
            f"  Width: ${actual_width:.0f}  |  Est. cost: ~${cost_est:.2f}/contract",
            f"  Max profit: ~${max_profit:.2f}/contract  |  R/R: ~{rr:.1f}:1",
            f"  Cost is ~60% of width — this is normal for ITM 0DTE spreads",
            "",
            f"📍 LEVELS",
            f"  Entry zone:  ${spot:.2f} ± 0.3%",
        ]

        if target1 and target1 != short_strike:
            lines.append(f"  Target 1:    ${target1:.0f}  (gamma wall — take 50% profit here)")
        lines.append(f"  {'Target 2' if target1 != short_strike else 'Target 1'}:    ${short_strike:.0f}  (short leg — full exit at spread max)")
        if target2:
            wall_name = "call wall" if is_bull else "put wall"
            lines.append(f"  Extended T:  ${target2:.0f}  ({wall_name} — only if ITM and time permits)")
        lines += [
            f"  Hard stop:   ${stop_level:.2f}  ({'gamma flip — regime change' if flip else 'price-based stop'})",
            f"  Alt stop:    -50% of premium  (${round(cost_est*0.5,2):.2f} loss per contract)",
            "",
            f"⏱️ TIMING",
            f"  {timing_note}",
            f"  {timing_warn}",
            f"  ⚠️ No new entries after 2:30 PM CT — gamma risk zone",
            "",
            f"📊 SIZE",
            f"  VIX {v} → base: {base_pct}%  |  {dex_note}",
            f"  → FINAL: {size_pct}% of normal position size",
            "",
            "✅ ENTRY CHECKLIST",
        ]
        for c in checks:
            lines.append(f"  {c}")

        lines += [
            "",
            "⛔ INVALIDATION",
            f"  {inval_note}",
            f"  Spread loses 50% of cost → stop out regardless of time or P/L",
            "━" * 32,
            "— Not financial advice —",
        ]

        post_to_telegram("\n".join(lines))
        log.info(
            f"Trade card: {ticker} | {direction} | score={score} | "
            f"{spread_type} {long_label}/{short_label} | width=${actual_width} | "
            f"cost_est=${cost_est} | stop=${stop_level} | size={size_pct}%"
        )

    except Exception as e:
        log.error(f"Trade card error for {ticker}: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────
# MONITOR CARD — /monitorlong and /monitorshort
# Full card on liquid symbols. Simplified card on thin chains.
# ─────────────────────────────────────────────────────────────────────

def _post_monitor_card(ticker: str, mode: str):
    """
    mode = "long"  → expiry closest to 21 DTE (min 15 days)
    mode = "short" → nearest available expiry
    Posts a positional outlook card. No trade card — monitoring only.
    """
    try:
        ticker = ticker.upper()

        if mode == "long":
            expiry, dte = _find_expiry_closest_to_21(ticker)
            mode_label  = "Swing Outlook (≈21 DTE)"
            mode_emoji  = "📅"
        else:
            expiry, dte = _find_expiry_nearest(ticker)
            mode_label  = "Near-Term Outlook (Nearest Exp)"
            mode_emoji  = "⚡"

        if not expiry:
            post_to_telegram(f"⚠️ {ticker}: could not find a valid expiration for /monitor{mode}")
            return

        liquid = _is_liquid(ticker)

        if liquid:
            iv, spot, expiration, eng, walls, skew, pcr, vix = _get_chain_iv_for_expiry(ticker, expiry, dte)
        else:
            # Simplified fetch — ATM IV + basic walls only, skip heavy exposure engine
            iv, spot, expiration, eng, walls, skew, pcr, vix = _get_chain_iv_for_expiry(ticker, expiry, dte)
            # Strip the heavy dealer metrics for thin chains — walls and skew still useful
            eng = {}

        if iv is None or spot is None:
            post_to_telegram(f"⚠️ {ticker}: could not fetch IV for {expiry} (DTE={dte})")
            return

        # For non-0DTE: EM over full calendar days, not intraday hours
        # Use annualized EM: Spot × IV × sqrt(DTE/365)
        trading_days = max(dte * (252/365), 0.5)
        hours_equiv  = trading_days * 6.5
        em = _calc_intraday_em(spot, iv, hours_equiv)
        if not em:
            return

        # Bias engine — same 14-signal system
        bias = _calc_bias(spot, em or {}, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        iv_pct = iv * 100
        if iv_pct < 15:
            iv_emoji = "🟢"
            iv_note  = "Low IV — options are cheap, premium selling less attractive"
        elif iv_pct < 30:
            iv_emoji = "🟡"
            iv_note  = "Moderate IV — balanced environment for buyers and sellers"
        elif iv_pct < 50:
            iv_emoji = "🔴"
            iv_note  = "Elevated IV — options expensive, consider spreads over naked positions"
        else:
            iv_emoji = "🚨"
            iv_note  = "EXTREME IV — very wide expected ranges. Size down significantly."

        dir_emoji = {
            "STRONG BULLISH": "🟢🟢", "BULLISH": "🟢", "SLIGHT BULLISH": "🟡",
            "NEUTRAL": "⚪", "SLIGHT BEARISH": "🟠", "BEARISH": "🔴", "STRONG BEARISH": "🔴🔴",
        }.get(bias["direction"], "⚪")

        chain_note = "Full institutional analysis" if liquid else "Simplified (thin chain — dealer metrics omitted)"

        # ════════════ HEADER ════════════
        lines = [
            f"{mode_emoji} {ticker} — {mode_label}",
            f"Spot: ${spot:.2f}  |  IV: {iv_emoji} {iv_pct:.1f}%  |  DTE: {dte}",
            f"Expiry: {expiration}  |  {chain_note}",
        ]
        if vix and vix.get("vix"):
            v    = vix["vix"]
            v9d  = vix.get("vix9d")
            term = vix.get("term", "")
            t_str = {"inverted": " 🚨 INVERTED", "normal": " ✅ normal", "flat": " flat"}.get(term, "")
            v9d_s = f"  VIX9D: {v9d}{t_str}" if v9d else ""
            lines.append(f"VIX: {v}{v9d_s}")

        # ════════════ EXPECTED RANGE over DTE ════════════
        lines += [
            "",
            "─" * 32,
            f"📐 EXPECTED MOVE  ({dte} calendar days)",
            f"  1σ (68%):  🐂 ${em['bull_1sd']:.2f}  ←  ${spot:.2f}  →  🐻 ${em['bear_1sd']:.2f}",
            f"             ±${em['em_1sd']:.2f}  ({em['em_pct_1sd']:.2f}%)",
            f"  2σ (95%):  🐂 ${em['bull_2sd']:.2f}  ←→  🐻 ${em['bear_2sd']:.2f}",
            f"             ±${em['em_2sd']:.2f}",
        ]

        # ════════════ DEALER FLOW (liquid only) ════════════
        if liquid and eng:
            tgex    = eng.get("gex", 0)
            dex     = eng.get("dex", 0)
            vanna_m = eng.get("vanna", 0)
            charm_m = eng.get("charm", 0)
            flip    = eng.get("flip_price")
            regime  = eng.get("regime", {})

            gex_icon = "🧲" if tgex >= 0 else "⚡"
            gex_mode = "SUPPRESSING (range-bound)" if tgex >= 0 else "AMPLIFYING (trending)"
            dex_note = "dealers SHORT → BUY on rallies" if dex < 0 else "dealers LONG → SELL on drops"

            lines += [
                "",
                "─" * 32,
                "⚙️ DEALER FLOW",
                f"  {gex_icon} GEX:   {'+'if tgex>=0 else ''}{tgex:.1f}M  —  {gex_mode}",
                f"  📍 Flip:  ${flip:.2f}" if flip else "  📍 Flip:  not found",
                f"  📊 DEX:   {dex:+.1f}M  —  {dex_note}",
                f"  🌊 Vanna: {vanna_m:+.1f}M  |  Charm: {charm_m:+.1f}M",
            ]
            if regime.get("preferred"):
                lines.append(f"  ✅ Favors: {regime['preferred']}")
            if regime.get("avoid"):
                lines.append(f"  ❌ Avoid:  {regime['avoid']}")

        # ════════════ KEY LEVELS ════════════
        if walls:
            lines += ["", "─" * 32, "🧱 KEY LEVELS"]

            if "call_wall" in walls:
                cw    = walls["call_wall"]
                cw_oi = walls["call_wall_oi"]
                dist  = ((cw - spot) / spot) * 100
                in_1  = cw <= em["bull_1sd"]
                tag   = "inside 1σ — strong cap" if in_1 else ("within 2σ" if cw <= em["bull_2sd"] else "outside 2σ")
                lines.append(f"  📵 Resistance: ${cw:.0f}  ({cw_oi:,} OI, +{dist:.1f}%) — {tag}")

            if "put_wall" in walls:
                pw    = walls["put_wall"]
                pw_oi = walls["put_wall_oi"]
                dist  = ((spot - pw) / spot) * 100
                in_1  = pw >= em["bear_1sd"]
                tag   = "inside 1σ — strong floor" if in_1 else ("within 2σ" if pw >= em["bear_2sd"] else "outside 2σ")
                lines.append(f"  🛡️ Support:    ${pw:.0f}  ({pw_oi:,} OI, -{dist:.1f}%) — {tag}")

            if "gamma_wall" in walls and liquid:
                gw = walls["gamma_wall"]
                lines.append(f"  🎯 Gamma Wall: ${gw:.0f}  — dealer hedging magnet")

            if "call_top3" in walls:
                ct3 = " → ".join(f"${x:.0f}" for x in walls["call_top3"])
                lines.append(f"  📵 Resistance cluster: {ct3}")
            if "put_top3" in walls:
                pt3 = " → ".join(f"${x:.0f}" for x in walls["put_top3"])
                lines.append(f"  🛡️ Support cluster:    {pt3}")

        # ════════════ SENTIMENT ════════════
        skew_str = ""
        if skew and "call_iv" in skew and "put_iv" in skew:
            diff     = skew["put_iv"] - skew["call_iv"]
            skew_str = f"  IV Skew: Calls {skew['call_iv']}% / Puts {skew['put_iv']}%  ({diff:+.1f}pp {'fear' if diff > 0 else 'greed'})"

        pcr_oi_s  = f"OI {pcr['pcr_oi']:.2f}"   if pcr and pcr.get("pcr_oi")  is not None else "OI n/a"
        pcr_vol_s = f"Vol {pcr['pcr_vol']:.2f}"  if pcr and pcr.get("pcr_vol") is not None else "Vol n/a"

        lines += [
            "",
            "─" * 32,
            "📊 SENTIMENT",
            f"  PCR: {pcr_oi_s}  |  {pcr_vol_s}",
        ]
        if skew_str:
            lines.append(skew_str)

        # ════════════ DIRECTIONAL LEAN ════════════
        strength_str = f"  [{bias['strength']}]" if bias.get("strength") else ""
        score_str    = f"{bias['score']:+d}/{bias.get('max_score', 14)}"
        dot_bar      = ("▲" * bias["up_count"]) + ("▼" * bias["down_count"]) + ("◆" * bias["neu_count"])

        lines += [
            "",
            "═" * 32,
            f"{dir_emoji}  OUTLOOK: {bias['direction']}{strength_str}",
            f"Score: {score_str}  |  {dot_bar}",
            f"  ▲ {bias['up_count']} bullish  ·  ▼ {bias['down_count']} bearish  ·  ◆ {bias['neu_count']} neutral",
            "",
            f"📋 {bias['verdict']}",
            "",
            "── Signal Breakdown ──",
        ]
        for arrow, text in bias["signals"]:
            lines.append(f"  {arrow}  {text}")

        lines += [
            "═" * 32,
            "",
            f"💡 {iv_note}",
            f"📌 This is a monitoring card — no trade recommended.",
            "— Not financial advice —",
        ]

        post_to_telegram("\n".join(lines))
        log.info(
            f"Monitor card: {ticker} | mode={mode} | exp={expiration} | DTE={dte} | "
            f"IV={iv_pct:.1f}% | EM=±{em['em_1sd']} | lean={bias['direction']} | score={bias['score']}"
        )

    except Exception as e:
        log.error(f"Monitor card error for {ticker} mode={mode}: {e}", exc_info=True)
        post_to_telegram(f"⚠️ Monitor card failed for {ticker}: {type(e).__name__}")


def _em_scheduler():
    """
    Background thread. Wakes every minute, checks if it is time to fire
    an EM card for SPY and QQQ. Fires at 8:45 AM and 2:45 PM Central.
    Skips weekends.
    """
    try:
        import pytz
    except ImportError:
        log.error("pytz not installed — EM scheduler disabled. Add pytz to requirements.txt")
        return

    log.info(f"0DTE EM scheduler started — fires at {EM_SCHEDULE_TIMES_CT} CT on weekdays")
    fired_today: set = set()   # tracks (date_str, hour, minute) already fired

    while True:
        try:
            ct = pytz.timezone("America/Chicago")
            now_ct = datetime.now(ct)

            # Skip weekends
            if now_ct.weekday() >= 5:
                time.sleep(60)
                continue

            date_str = now_ct.strftime("%Y-%m-%d")

            for hour, minute in EM_SCHEDULE_TIMES_CT:
                fire_key = (date_str, hour, minute)
                if fire_key in fired_today:
                    continue

                # Fire within a 2-minute window to handle scheduler drift
                if now_ct.hour == hour and abs(now_ct.minute - minute) <= 1:
                    session = "morning" if hour < 12 else "afternoon"
                    fired_today.add(fire_key)
                    log.info(f"EM scheduler firing: {session} {date_str} {hour:02d}:{minute:02d} CT")
                    for ticker in EM_TICKERS:
                        threading.Thread(
                            target=_post_em_card,
                            args=(ticker, session),
                            daemon=True,
                            name=f"em-card-{ticker}-{session}",
                        ).start()

            # Prune fired_today to only keep today's entries
            fired_today = {k for k in fired_today if k[0] == date_str}

        except Exception as e:
            log.error(f"EM scheduler error: {e}", exc_info=True)

        time.sleep(60)   # check once per minute


# Start EM scheduler
threading.Thread(target=_em_scheduler, daemon=True, name="em-scheduler").start()


# ─────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────

with app.app_context():
    _tg_ws = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if _tg_ws and BOT_URL:
        register_webhook(BOT_URL, _tg_ws)

    portfolio.init_store(store_get, store_set)
    trade_journal.init_store(store_get, store_set)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
