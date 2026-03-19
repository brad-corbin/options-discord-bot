# app.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v4.2 UPGRADE (2026-03-17):
#   - CHECK_TICKER_TIMEOUT_SEC imported from trading_rules (45s, was hardcoded 75s)
#   - PREFETCH_WORKER_WAIT_SEC used consistently (was hardcoded 60s in one path)
#   - PIN regime gate: blocks directional spreads when v4 prefilter reports *PIN
#   - CHOP regime gate: raises confidence threshold + reduces size in choppy tape
#   - _estimate_iv_rank: IV clamped to SWING_IV_MIN/MAX (same fix as swing_engine)
#   - _get_ohlc_bars: OHLC_WARN_ONCE_PER_CYCLE dedup (32 duplicate MA warnings today)
#   - CHOP_REGIME_SIZE_MULT applied in _post_trade_card size block
#
# v4.0/v4.1 preserved:
#   - v4 institutional engine integration via engine_bridge.py
#   - Confidence scoring on all cards (HIGH/MODERATE/LOW)
#   - Wave prefetch cache, Redis-backed signal queue
#   - CAGF regime gate for SPY/QQQ/SPX
#   - EM-aware strike placement and width clamping
#   - EM accuracy logger + auto-reconciler

from telegram_commands import (
    handle_command,
    register_webhook,
    is_paused,
    get_confidence_gate,
    set_last_scan,
    get_state,
    send_reply,
)

import jwt
import os
import time
import math
import json
import hashlib
import base64
import logging
import csv
import threading
import queue
import portfolio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
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
    IMMEDIATE_POST_TIER, IMMEDIATE_POST_MIN_CONF, IMMEDIATE_POST_0DTE,
    DIGEST_CARD_CACHE_TTL_SEC,
    # v4.2
    CHECK_TICKER_TIMEOUT_SEC,
    CHOP_REGIME_CONF_GATE, CHOP_REGIME_SIZE_MULT,
    PIN_REGIME_BLOCK_BEAR_PUTS, PIN_REGIME_BLOCK_BULL_CALLS,
    SWING_IV_MIN, SWING_IV_MAX, SWING_IV_ATM_BAND_PCT,
    OHLC_WARN_ONCE_PER_CYCLE,
)
# ── v4 engine bridge ──
from engine_bridge import (
    run_institutional_snapshot,
    build_option_rows,
    format_confidence_header,
    format_trade_sign_line,
    format_vol_regime_line,
)
from options_exposure import SCHEMA_VERSION
from oi_cache import OICache
from api_cache import CachedMarketData
from institutional_flow import compute_cagf, recommend_dte, format_cagf_block, format_dte_block
from card_formatters import (
    format_plain_english_card,
    format_decision_card,
    resolve_unified_regime,
    regime_gate,
)
from em_reconciler import (
    reconcile_em_predictions,
    compute_accuracy_stats,
    format_accuracy_report,
    fetch_eod_close_marketdata,
)

import risk_manager
import trade_journal

# OI cache will be initialized after store_get/store_set are defined
_oi_cache = None

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

# Live validation: TradingView is the trigger, bot data is the source of truth
SCALP_SIGNAL_WARN_DRIFT_PCT   = float(os.getenv("SCALP_SIGNAL_WARN_DRIFT_PCT", "0.20") or 0.20)
SCALP_SIGNAL_REJECT_DRIFT_PCT = float(os.getenv("SCALP_SIGNAL_REJECT_DRIFT_PCT", "0.35") or 0.35)
SWING_SIGNAL_WARN_DRIFT_PCT   = float(os.getenv("SWING_SIGNAL_WARN_DRIFT_PCT", "0.45") or 0.45)
SWING_SIGNAL_REJECT_DRIFT_PCT = float(os.getenv("SWING_SIGNAL_REJECT_DRIFT_PCT", "0.75") or 0.75)
SIGNAL_WARN_CONF_PENALTY      = int(os.getenv("SIGNAL_WARN_CONF_PENALTY", "6") or 6)
SIGNAL_MODERATE_CONF_PENALTY  = int(os.getenv("SIGNAL_MODERATE_CONF_PENALTY", "12") or 12)
SIGNAL_STALE_AFTER_SEC        = int(os.getenv("SIGNAL_STALE_AFTER_SEC", "900") or 900)

TELEGRAM_PORTFOLIO_CHAT_ID     = os.getenv("TELEGRAM_PORTFOLIO_CHAT_ID",     "").strip()
TELEGRAM_MOM_PORTFOLIO_CHAT_ID = os.getenv("TELEGRAM_MOM_PORTFOLIO_CHAT_ID", "").strip()

ACCOUNT_CHAT_IDS = {
    "brad": TELEGRAM_PORTFOLIO_CHAT_ID,
    "mom":  TELEGRAM_MOM_PORTFOLIO_CHAT_ID,
}

# ─────────────────────────────────────────────────────────
# DATASET / DIAGNOSTICS AUT0-LOGGING
# ─────────────────────────────────────────────────────────
DIAGNOSTIC_CHAT_ID = os.getenv("DIAGNOSTIC_CHAT_ID", "").strip()
AUTO_LOG_DIR = os.getenv("AUTO_LOG_DIR", "/mnt/data/bot_logs").strip() or "/mnt/data/bot_logs"
AUTO_LOG_ENABLE = os.getenv("AUTO_LOG_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
AUTO_LOG_DIAGNOSTICS = os.getenv("AUTO_LOG_DIAGNOSTICS", "0").strip().lower() in ("1", "true", "yes", "on")
GOOGLE_SHEETS_ENABLE = os.getenv("GOOGLE_SHEETS_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1bXyAVQ8dB-dTyFVN6uv6PVtuphwRZ6VjIp2W9dcm9iw").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_SIGNAL_TAB = os.getenv("GOOGLE_SHEET_SIGNAL_TAB", "signal_decisions").strip() or "signal_decisions"
GOOGLE_SHEET_EM_TAB = os.getenv("GOOGLE_SHEET_EM_TAB", "em_predictions").strip() or "em_predictions"
GOOGLE_SHEET_RECON_TAB = os.getenv("GOOGLE_SHEET_RECON_TAB", "em_reconciliation").strip() or "em_reconciliation"

# ─────────────────────────────────────────────────────────
# REDIS
# ─────────────────────────────────────────────────────────
_redis_client = None


class _MemStore:
    """
    TTL-aware in-memory fallback store.
    Mirrors Redis setex/get/exists interface.
    """
    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def _prune(self):
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
_csv_lock = threading.Lock()


def _ensure_log_dir():
    try:
        os.makedirs(AUTO_LOG_DIR, exist_ok=True)
    except Exception as e:
        log.warning(f"Auto-log dir unavailable ({AUTO_LOG_DIR}): {e}")


def _append_jsonl(filename: str, row: dict):
    if not AUTO_LOG_ENABLE:
        return
    try:
        _ensure_log_dir()
        path = os.path.join(AUTO_LOG_DIR, filename)
        payload = dict(row or {})
        payload.setdefault("logged_at_utc", datetime.now(timezone.utc).isoformat())
        with _csv_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"JSONL auto-log failed ({filename}): {e}")


def _append_csv_row(filename: str, fieldnames: list, row: dict):
    if not AUTO_LOG_ENABLE:
        return
    try:
        _ensure_log_dir()
        path = os.path.join(AUTO_LOG_DIR, filename)
        safe_row = {k: row.get(k) for k in fieldnames}
        exists = os.path.exists(path)
        with _csv_lock:
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not exists or os.path.getsize(path) == 0:
                    writer.writeheader()
                writer.writerow(safe_row)
        log.info(f"CSV auto-log wrote: {filename} -> {path}")
        _append_google_sheet_row(filename, fieldnames, safe_row)
    except Exception as e:
        log.warning(f"CSV auto-log failed ({filename}): {e}")


_google_sheets_lock = threading.Lock()
_google_sheets_token_cache = {"token": None, "exp": 0}
_google_sheets_header_tabs = set()
_google_sheets_sa_cache = None


def _tab_for_filename(filename: str) -> str:
    mapping = {
        "signal_decisions.csv": GOOGLE_SHEET_SIGNAL_TAB,
        "em_predictions.csv": GOOGLE_SHEET_EM_TAB,
        "em_reconciliation.csv": GOOGLE_SHEET_RECON_TAB,
    }
    return mapping.get(filename, "")


def _load_google_service_account() -> dict | None:
    global _google_sheets_sa_cache
    if _google_sheets_sa_cache is not None:
        return _google_sheets_sa_cache
    raw = GOOGLE_SERVICE_ACCOUNT_JSON
    try:
        if raw:
            _google_sheets_sa_cache = json.loads(raw)
            log.info(f"Google Sheets service account loaded from env: {_google_sheets_sa_cache.get('client_email','?')}")
            return _google_sheets_sa_cache
        if GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
                _google_sheets_sa_cache = json.load(f)
                log.info(f"Google Sheets service account loaded from file: {_google_sheets_sa_cache.get('client_email','?')}")
                return _google_sheets_sa_cache
        default_path = "/mnt/data/corbin-bot-tracking-0249b119c63f.json"
        if os.path.exists(default_path):
            with open(default_path, "r", encoding="utf-8") as f:
                _google_sheets_sa_cache = json.load(f)
                log.info(f"Google Sheets service account loaded from default file: {_google_sheets_sa_cache.get('client_email','?')}")
                return _google_sheets_sa_cache
        log.warning("Google Sheets credentials not found in env or file.")
    except Exception as e:
        log.warning(f"Google Sheets credentials load failed: {e}")
    return None


def _get_google_access_token() -> str | None:
    if not GOOGLE_SHEETS_ENABLE or not GOOGLE_SHEET_ID:
        log.info("Google Sheets token fetch skipped: disabled or sheet id missing")
        return None
    now = int(time.time())
    cached = _google_sheets_token_cache
    if cached.get("token") and now < int(cached.get("exp", 0)) - 60:
        return cached["token"]
    sa = _load_google_service_account()
    if not sa:
        return None
    try:
        issued = int(time.time())
        payload = {
            "iss": sa["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file",
            "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
            "iat": issued,
            "exp": issued + 3600,
        }
        assertion = jwt.encode(payload, sa["private_key"], algorithm="RS256")
        resp = requests.post(
            sa.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        exp = issued + int(data.get("expires_in", 3600))
        if token:
            _google_sheets_token_cache.update({"token": token, "exp": exp})
            log.info("Google Sheets token acquired successfully")
            return token
    except Exception as e:
        log.warning(f"Google Sheets token fetch failed: {e}")
    return None


def _sheet_headers_exist(tab: str, token: str) -> bool:
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{requests.utils.quote(tab + '!1:1', safe='') }"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        values = resp.json().get("values", [])
        return bool(values and any((str(x).strip() for x in values[0])))
    except Exception as e:
        log.warning(f"Google Sheets header check failed for {tab}: {e}")
        return True


def _append_google_sheet_values(tab: str, values: list, token: str) -> bool:
    try:
        rng = requests.utils.quote(f"{tab}!A:A", safe="!")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{rng}:append"
        resp = requests.post(
            url,
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": values},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"Google Sheets append failed for {tab}: {e}")
        return False


def _append_google_sheet_row(filename: str, fieldnames: list, row: dict):
    tab = _tab_for_filename(filename)
    if not (GOOGLE_SHEETS_ENABLE and tab and GOOGLE_SHEET_ID):
        return
    token = _get_google_access_token()
    if not token:
        return
    values = [[row.get(k) for k in fieldnames]]
    try:
        with _google_sheets_lock:
            if tab not in _google_sheets_header_tabs:
                if not _sheet_headers_exist(tab, token):
                    if _append_google_sheet_values(tab, [fieldnames], token):
                        log.info(f"Google Sheets header row written for tab '{tab}'")
                    else:
                        log.warning(f"Google Sheets header write failed for tab '{tab}'")
                _google_sheets_header_tabs.add(tab)
            ok = _append_google_sheet_values(tab, values, token)
            if ok:
                log.info(f"Google Sheets append OK for tab '{tab}' (1 row)")
            else:
                log.warning(f"Google Sheets append returned False for tab '{tab}'")
    except Exception as e:
        log.warning(f"Google Sheets row sync failed for {filename}: {e}")


def _post_diagnostic(text: str):
    if not (AUTO_LOG_DIAGNOSTICS and DIAGNOSTIC_CHAT_ID and text):
        return
    try:
        post_to_telegram(text, chat_id=DIAGNOSTIC_CHAT_ID)
    except Exception as e:
        log.warning(f"Diagnostic post failed: {e}")


def _log_signal_dataset_event(ticker: str, webhook_data: dict, outcome: str, reason: str = "", best_rec: dict = None,
                              signal_validation: dict = None, regime: dict = None, v4_flow: dict = None,
                              spot: float = None, expirations_checked: int = None):
    try:
        wd = dict(webhook_data or {})
        trade = (best_rec or {}).get("trade", {}) if best_rec else {}
        row = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": "signal_decision",
            "ticker": ticker,
            "mode": wd.get("type") or "scalp",
            "source": wd.get("source") or "tv",
            "bias": wd.get("bias"),
            "tier": wd.get("tier"),
            "outcome": outcome,
            "reason": (reason or "")[:300],
            "signal_time": wd.get("time"),
            "signal_price": wd.get("close"),
            "live_spot": spot if spot is not None else wd.get("live_spot"),
            "signal_age_sec": (signal_validation or {}).get("signal_age_sec"),
            "drift_pct": (signal_validation or {}).get("drift_pct"),
            "validation_ok": (signal_validation or {}).get("ok"),
            "validation_penalty": (signal_validation or {}).get("confidence_penalty"),
            "expiration": (best_rec or {}).get("exp"),
            "dte": (best_rec or {}).get("dte"),
            "long_strike": trade.get("long"),
            "short_strike": trade.get("short"),
            "debit": trade.get("debit"),
            "width": trade.get("width"),
            "ror": trade.get("ror"),
            "win_prob": trade.get("win_prob"),
            "ev_per_contract": trade.get("ev_per_contract"),
            "confidence": (best_rec or {}).get("confidence"),
            "confidence_pre_validation": (best_rec or {}).get("confidence_pre_validation"),
            "contracts": (best_rec or {}).get("contracts"),
            "expirations_checked": expirations_checked,
            "regime": (regime or {}).get("regime") if isinstance(regime, dict) else None,
            "vix": (regime or {}).get("vix") if isinstance(regime, dict) else None,
            "adx": (regime or {}).get("adx") if isinstance(regime, dict) else None,
            "v4_composite_regime": (v4_flow or {}).get("composite_regime") if isinstance(v4_flow, dict) else None,
            "v4_confidence_label": (v4_flow or {}).get("confidence_label") if isinstance(v4_flow, dict) else None,
            "log_schema": "v1",
        }
        fieldnames = list(row.keys())
        _append_csv_row("signal_decisions.csv", fieldnames, row)
        _append_jsonl("signal_decisions.jsonl", row)

        diag = (
            f"🧪 {ticker} {str(wd.get('bias','')).upper()} T{wd.get('tier','?')} | {outcome}\n"
            f"spot ${row['live_spot']} | drift {row['drift_pct']}% | conf {row['confidence']}\n"
            f"reason: {row['reason'] or '—'}"
        )
        _post_diagnostic(diag)
    except Exception as e:
        log.warning(f"Signal dataset log failed for {ticker}: {e}")


def _get_redis():
    global _redis_client
    if _redis_client is not None:
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
            REDIS_URL, decode_responses=True,
            socket_timeout=10, socket_connect_timeout=5, retry_on_timeout=True,
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
            if ttl: r.setex(key, ttl, value)
            else: r.set(key, value)
            return
        except Exception as e:
            log.warning(f"Redis set failed ({e}) — writing to mem fallback")
    _mem_store.set(key, value, ttl)


def store_get(key: str):
    r = _get_redis()
    if r:
        try: return r.get(key)
        except Exception: pass
    return _mem_store.get(key)


def store_exists(key: str) -> bool:
    r = _get_redis()
    if r:
        try: return bool(r.exists(key))
        except Exception: pass
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

QUEUE_MAX        = 80
QUEUE_WORKERS    = 6
SIGNAL_TTL_SEC   = 480
TG_MIN_GAP_SEC   = 0.8
WAVE_COLLECT_SEC = 90

REDIS_QUEUE_KEY  = "signal_queue"

_mem_signal_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)

_tg_last_post_time: float = 0.0
_tg_lock = threading.Lock()

# ─────────────────────────────────────────────────────────
# WAVE PREFETCH CACHE — v4.3
# Pre-fetches spot, chains, candles, regime, v4 prefilter
# for all unique tickers in a signal wave BEFORE workers start.
# Workers then pull from cache instead of hitting APIs.
# ─────────────────────────────────────────────────────────

_prefetch_cache = {}
_prefetch_lock = threading.Lock()
_prefetch_ttl = 120  # seconds — data is fresh enough for 2 minutes


def _prefetch_get(ticker: str) -> dict:
    """Get prefetched data for a ticker, or None if expired/missing."""
    with _prefetch_lock:
        entry = _prefetch_cache.get(ticker)
        if entry and (time.time() - entry["ts"]) < _prefetch_ttl:
            return entry
    return None


def _prefetch_set(ticker: str, data: dict):
    """Store prefetched data for a ticker."""
    with _prefetch_lock:
        _prefetch_cache[ticker] = {**data, "ts": time.time()}


def _prefetch_ticker(ticker: str):
    """
    Fetch all data needed by check_ticker for one ticker.
    Stores in prefetch cache. Called once per unique ticker in a wave.
    """
    ticker = ticker.strip().upper()

    # Skip if already fresh in cache
    if _prefetch_get(ticker):
        return

    try:
        t0 = time.time()

        # 1. Spot price
        spot = get_spot(ticker)

        # 2. Options chains (the slowest part — multiple expirations)
        chains = get_options_chain(ticker)

        # 3. Enrichment (earnings check) — with short timeout to avoid Finnhub stalls
        try:
            enrichment = enrich_ticker(ticker)
        except Exception:
            enrichment = {"has_earnings": False, "earnings_warn": None}

        # 4. Daily candles for RV
        candle_closes = get_daily_candles(ticker, days=RV_LOOKBACK_DAYS + 5)

        # 5. Regime (cached globally, so this is fast after first call)
        regime = get_current_regime()

        # 6. V4 prefilter (institutional snapshot)
        v4_flow = _run_v4_prefilter(ticker, spot, chains, candle_closes)

        elapsed = time.time() - t0

        _prefetch_set(ticker, {
            "spot": spot,
            "chains": chains,
            "enrichment": enrichment,
            "candle_closes": candle_closes,
            "regime": regime,
            "v4_flow": v4_flow,
        })

        log.info(f"Prefetch {ticker}: {len(chains)} exps, "
                 f"v4={v4_flow.get('composite_regime', '?') if v4_flow else 'N/A'}, "
                 f"{elapsed:.1f}s")

    except Exception as e:
        log.warning(f"Prefetch failed for {ticker}: {e}")
        # Store a minimal entry so workers don't re-fetch and also timeout
        _prefetch_set(ticker, {
            "spot": None, "chains": None, "enrichment": {},
            "candle_closes": [], "regime": {}, "v4_flow": {},
            "error": str(e),
        })


def _prefetch_wave(tickers: list):
    """
    Pre-fetch data for all unique tickers in a signal wave.
    Uses a thread pool for parallel fetching with controlled concurrency.
    """
    unique = list(dict.fromkeys(t.strip().upper() for t in tickers))

    # Filter out tickers already in cache
    to_fetch = [t for t in unique if not _prefetch_get(t)]

    if not to_fetch:
        log.info(f"Wave prefetch: all {len(unique)} tickers already cached")
        return

    log.info(f"Wave prefetch: fetching {len(to_fetch)} tickers "
             f"({len(unique) - len(to_fetch)} already cached)")

    # Use max 4 threads to avoid API rate limits, but still parallel
    with ThreadPoolExecutor(max_workers=min(4, len(to_fetch))) as pool:
        pool.map(_prefetch_ticker, to_fetch)

    log.info(f"Wave prefetch complete: {len(to_fetch)} tickers ready")

_wave_lock         = threading.Lock()
_wave_results: list = []
_wave_last_arrival: float = 0.0


def _record_wave_result(result: dict):
    global _wave_last_arrival
    with _wave_lock:
        _wave_results.append(result)
        _wave_last_arrival = time.time()


def _flush_wave_digest():
    """
    v4.1: Compact digest with /tradecard retrieval.
    - Immediate post: T1 + conf >= 75 (or 0DTE)
    - Digest: one-liner per signal, full card cached for /tradecard TICKER
    """
    global _wave_results
    with _wave_lock:
        if not _wave_results:
            return
        results = list(_wave_results)
        _wave_results = []

    immediate_cards = []   # full cards to post now
    digest_lines = []      # compact one-liners
    skipped_lines = []     # rejected signals

    for r in results:
        job_type = r.get("job_type", "tv")
        ticker = r.get("ticker", "?")
        bias = r.get("bias", "bull")
        tier = r.get("tier", "?")
        conf = r.get("confidence")
        card = r.get("card")
        won = r.get("outcome") == "trade"

        dir_emoji = "🐻" if bias == "bear" else "🐂"
        conf_str = f"{conf}/100" if conf is not None else "—"
        type_label = "SWING" if job_type == "swing" else "TV"

        if won and card:
            # Cache the full card for /tradecard retrieval
            cache_key = f"tradecard:{ticker.upper()}"
            store_set(cache_key, card, ttl=DIGEST_CARD_CACHE_TTL_SEC)

            # Decide: immediate post or digest-only?
            is_immediate = (
                (tier in IMMEDIATE_POST_TIER and conf is not None and conf >= IMMEDIATE_POST_MIN_CONF)
            )

            if is_immediate:
                immediate_cards.append(card)
                digest_lines.append(f"  ✅ {ticker} T{tier} {dir_emoji} {conf_str} — POSTED ⬆️")
            else:
                digest_lines.append(f"  📋 {ticker} T{tier} {dir_emoji} {conf_str} — /tradecard {ticker}")
        else:
            reason = r.get("reason", "no setup")[:40]
            skipped_lines.append(f"  ❌ {ticker} T{tier} {dir_emoji} {conf_str} — {reason}")

    lines = []
    if digest_lines or skipped_lines:
        lines.append(f"📊 SIGNAL DIGEST ({len(digest_lines)} trades, {len(skipped_lines)} skipped)")
        lines.append("")
        if digest_lines:
            lines.append("── Trades ──")
            lines.extend(digest_lines)
        if skipped_lines:
            lines.append("")
            lines.append("── Skipped ──")
            lines.extend(skipped_lines)
        lines.append("")
        lines.append("💡 Use /tradecard TICKER for full card")

    if not lines and not immediate_cards:
        return

    log.info(f"Wave digest: {len(immediate_cards)} immediate, "
             f"{len(digest_lines)} digest, {len(skipped_lines)} skipped")

    if lines:
        _tg_rate_limited_post("\n".join(lines))

    for card_text in immediate_cards:
        _tg_rate_limited_post(card_text)


def _digest_poster_thread():
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
    job = {
        "job_type": job_type, "ticker": ticker, "bias": bias,
        "webhook_data": webhook_data, "signal_msg": signal_msg,
        "enqueued_at": time.time(),
    }

    # v4.3: Track tickers for wave prefetch
    _record_prefetch_ticker(ticker)

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
                log.warning(f"Redis signal queue at {qsize}/{QUEUE_MAX}")
            return
        except Exception as e:
            log.warning(f"Redis enqueue failed ({e}), falling back to memory queue")

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


# ─────────────────────────────────────────────────────────
# PREFETCH COLLECTOR — v4.3
# Collects tickers from incoming signal burst, then prefetches
# all data in parallel before workers start processing.
# ─────────────────────────────────────────────────────────

_prefetch_pending_lock = threading.Lock()
_prefetch_pending: list = []
_prefetch_last_signal: float = 0.0
_prefetch_thread_active = False
_prefetch_wave_done = threading.Event()  # Workers wait on this
_prefetch_wave_done.set()  # Start in "done" state (no wave pending)
PREFETCH_SETTLE_SEC = 3  # wait this long after last signal before prefetching
PREFETCH_WORKER_WAIT_SEC = 45  # max time workers wait for prefetch


def _record_prefetch_ticker(ticker: str):
    """Called from _enqueue_signal to track tickers needing prefetch."""
    global _prefetch_last_signal
    with _prefetch_pending_lock:
        _prefetch_pending.append(ticker.strip().upper())
        _prefetch_last_signal = time.time()
        _prefetch_wave_done.clear()  # Signal workers to wait
        _maybe_start_prefetch_thread()


def _maybe_start_prefetch_thread():
    """Start the prefetch thread if not already running."""
    global _prefetch_thread_active
    if _prefetch_thread_active:
        return
    _prefetch_thread_active = True
    threading.Thread(target=_prefetch_collector_thread, daemon=True, name="wave-prefetch").start()


def _prefetch_collector_thread():
    """
    Wait for signal burst to settle, then prefetch all unique tickers.
    This runs once per burst, then exits.
    """
    global _prefetch_thread_active
    try:
        # Wait for signals to stop arriving
        while True:
            time.sleep(1)
            with _prefetch_pending_lock:
                elapsed = time.time() - _prefetch_last_signal
                if elapsed >= PREFETCH_SETTLE_SEC and _prefetch_pending:
                    tickers = list(_prefetch_pending)
                    _prefetch_pending.clear()
                    break
                elif not _prefetch_pending:
                    # No signals pending
                    _prefetch_wave_done.set()
                    return

        # Dedupe and prefetch
        unique = list(dict.fromkeys(tickers))
        log.info(f"Wave prefetch triggered: {len(unique)} unique tickers from {len(tickers)} signals")
        _prefetch_wave(unique)

    except Exception as e:
        log.error(f"Prefetch collector error: {e}", exc_info=True)
    finally:
        _prefetch_wave_done.set()  # Unblock workers even on error
        _prefetch_thread_active = False
        log.info("Prefetch wave complete — workers unblocked")


def _process_job(worker_id: int, job: dict):
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
        "job_type": job_type, "ticker": ticker, "bias": bias,
        "tier": tier_label, "outcome": "skip", "card": None,
        "confidence": None, "reason": "",
    }

    if job_type == "tv":
        check_spread_exit_warning(ticker, bias, webhook_data)

        if bias not in ALLOWED_DIRECTIONS:
            base["reason"] = f"{bias} not in allowed directions"
            _record_wave_result(base)
            return

        # ── v4.1: CAGF regime gate for liquid index tickers ──
        # Pine = timing trigger, Python CAGF = regime context
        # Only gate SPY/QQQ/SPX — other tickers skip this check
        if ticker.upper() in ("SPY", "QQQ", "SPX"):
            try:
                _spot = get_spot(ticker)
                _bars = _get_ohlc_bars(ticker, days=65)
                _adv, _ = _estimate_liquidity(ticker, _spot)
                _closes = get_daily_candles(ticker, days=60)
                _vix_data = _get_vix_data()
                _vix_val = _vix_data.get("vix", 20) if _vix_data else 20

                # Run a quick v4 snapshot for dealer flows
                from datetime import date as _date
                _today = _date.today().strftime("%Y-%m-%d")
                _chain_data, _c_spot, _c_exp = _get_0dte_chain(ticker, _today)
                if _chain_data and _c_spot:
                    _oi_cache.apply_oi_changes_to_chain(ticker, _c_exp, _chain_data)
                    _v4 = run_institutional_snapshot(
                        chain_data=_chain_data, spot=_c_spot, dte=0.5,
                        recent_bars=_bars, is_0dte=True,
                        avg_daily_dollar_volume=_adv,
                        liquid_index=True,
                    )
                    if not _v4.get("error"):
                        _eng = _v4.get("engine_result", {})
                        _rv = _v4.get("vol_regime", {}).get("realized_vol_20d", 0) or 0
                        _iv = _v4.get("iv", 0) or 0

                        import pytz as _pytz
                        _ct = _pytz.timezone("America/Chicago")
                        _now_ct = datetime.now(_ct)
                        _mkt_open = _now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
                        _mkt_close = _now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
                        _sess_secs = (_mkt_close - _mkt_open).total_seconds()
                        _elapsed = max(0, (_now_ct - _mkt_open).total_seconds())
                        _sess_prog = min(1.0, _elapsed / _sess_secs) if _sess_secs > 0 else 0.5

                        _cagf = compute_cagf(
                            dealer_flows={
                                "gex": _eng.get("gex", 0), "dex": _eng.get("dex", 0),
                                "vanna": _eng.get("vanna", 0), "charm": _eng.get("charm", 0),
                                "gamma_flip": _eng.get("flip_price"),
                            },
                            iv=_iv, rv=_rv, spot=_c_spot, vix=_vix_val,
                            session_progress=_sess_prog, adv=_adv,
                            candle_closes=_closes, ticker=ticker,
                        )

                        _trend_prob = _cagf.get("trend_day_probability", 0)
                        _regime = _cagf.get("regime", "UNKNOWN")
                        _prob = _cagf.get("probability", 50)

                        # Gate: T1 requires trend_prob >= 0.55, T2 >= 0.40
                        min_trend = 0.55 if tier_label == "1" else 0.40
                        if _trend_prob < min_trend:
                            base["reason"] = (
                                f"CAGF regime gate: trend prob {_trend_prob:.0%} < {min_trend:.0%} "
                                f"({_regime}, {_prob:.0f}% directional)"
                            )
                            log.info(f"[worker-{worker_id}] {ticker} T{tier_label} blocked by CAGF: "
                                     f"trend={_trend_prob:.0%} regime={_regime} prob={_prob:.0f}%")
                            _record_wave_result(base)
                            return

                        log.info(f"[worker-{worker_id}] {ticker} CAGF passed: "
                                 f"trend={_trend_prob:.0%} regime={_regime} prob={_prob:.0f}%")
            except Exception as e:
                log.warning(f"[worker-{worker_id}] CAGF gate error for {ticker}: {e} — proceeding without gate")

        # ── v4.2: PIN regime gate (all tickers) ──
        # Uses prefetch v4_flow if available (no extra API call).
        # Blocks directional debit spreads when v4 regime contains PIN —
        # same logic check_ticker enforces via "not enough ITM strikes",
        # but surfaced here earlier to save a full timeout cycle.
        _cached_prefetch = _prefetch_get(ticker)
        if _cached_prefetch and _cached_prefetch.get("v4_flow"):
            _v4f = _cached_prefetch["v4_flow"]
            _composite = (_v4f.get("composite_regime") or "").upper()
            if "PIN" in _composite:
                if bias == "bear" and PIN_REGIME_BLOCK_BEAR_PUTS:
                    base["reason"] = f"PIN regime ({_composite}) — bear puts blocked"
                    log.info(f"[worker-{worker_id}] {ticker} blocked: {base['reason']}")
                    _record_wave_result(base)
                    return
                if bias == "bull" and PIN_REGIME_BLOCK_BULL_CALLS:
                    base["reason"] = f"PIN regime ({_composite}) — bull calls blocked"
                    log.info(f"[worker-{worker_id}] {ticker} blocked: {base['reason']}")
                    _record_wave_result(base)
                    return

        # ── v4.2: CHOP regime confidence gate (all tickers) ──
        # LOW VOL CHOP: only take trades that clear the tighter confidence threshold.
        # Regime is cheap to fetch — already cached by prefetch or get_current_regime().
        _current_regime = (_cached_prefetch.get("regime") if _cached_prefetch else None) or get_current_regime()
        _regime_label = (_current_regime.get("label") or "").upper()
        if "CHOP" in _regime_label:
            # Pre-check: if we have a prefetch confidence score, gate early
            if _cached_prefetch and _cached_prefetch.get("v4_flow"):
                _conf_score = _cached_prefetch["v4_flow"].get("confidence_score", 1.0)
                # confidence_score is 0.0–1.0; CHOP_REGIME_CONF_GATE is 0–100
                if _conf_score * 100 < CHOP_REGIME_CONF_GATE:
                    base["reason"] = (
                        f"CHOP regime gate: v4 conf {_conf_score*100:.0f}/100 "
                        f"< {CHOP_REGIME_CONF_GATE} (LOW VOL CHOP)"
                    )
                    log.info(f"[worker-{worker_id}] {ticker} blocked: {base['reason']}")
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
    log.info(f"Redis signal worker-{worker_id} started")
    last_heartbeat = time.time()

    while True:
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
            result = r.brpop(REDIS_QUEUE_KEY, timeout=5)
            if not result:
                continue
            _, raw = result
            job = json.loads(raw)
            try:
                _process_job(worker_id, job)
            except Exception as e:
                log.error(f"[worker-{worker_id}] Job error for {job.get('ticker')}: {e}", exc_info=True)
        except Exception as e:
            log.error(f"[worker-{worker_id}] Redis worker error: {e}", exc_info=True)
            global _redis_client
            _redis_client = None
            time.sleep(3)


def _signal_queue_worker_memory(worker_id: int):
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
                "job_type": job_type, "ticker": ticker, "bias": bias,
                "webhook_data": webhook_data, "signal_msg": signal_msg,
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
    r = _get_redis()
    worker_fn = _signal_queue_worker_redis if r else _signal_queue_worker_memory
    mode = "Redis" if r else "memory"
    log.info(f"Starting {QUEUE_WORKERS} {mode} signal workers (TTL {SIGNAL_TTL_SEC}s)")
    threads = []
    for i in range(QUEUE_WORKERS):
        t = threading.Thread(target=worker_fn, args=(i + 1,), daemon=True, name=f"signal-worker-{i + 1}")
        t.start()
        threads.append(t)
    return threads


_queue_worker_threads = _start_workers()


# ─────────────────────────────────────────────────────────
# check_ticker WITH HARD TIMEOUT (v3.9)
# ─────────────────────────────────────────────────────────

def check_ticker_with_timeout(ticker, direction="bull", webhook_data=None, timeout_sec=None):
    timeout_sec = timeout_sec if timeout_sec is not None else CHECK_TICKER_TIMEOUT_SEC
    result = {}
    exc_holder = []

    def _run():
        try:
            result.update(check_ticker(ticker, direction=direction, webhook_data=webhook_data))
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        log.error(f"check_ticker({ticker}) TIMED OUT after {timeout_sec}s")
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
                try: retry_after = r.json().get("parameters", {}).get("retry_after", 15)
                except Exception: retry_after = 15
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
# MARKETDATA API
# ─────────────────────────────────────────────────────────

def md_get(url, params=None):
    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")
    r = requests.get(url, headers={"Authorization": f"Bearer {MARKETDATA_TOKEN}"},
                     params=params or {}, timeout=8)
    r.raise_for_status()
    return r.json()

# v4.1: Cached API layer — reduces duplicate API calls by 70-90%
_cached_md = CachedMarketData(md_get)

def get_spot(ticker: str) -> float:
    return _cached_md.get_spot(ticker, as_float_fn=as_float)

def get_expirations(ticker: str) -> list:
    return _cached_md.get_expirations(ticker)

def get_daily_candles(ticker: str, days: int = 30) -> list:
    return _cached_md.get_daily_candles(ticker, days)

def get_vix() -> float:
    try:
        resp = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                           params={"interval": "1d", "range": "1d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
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
    now = time.time()
    if _regime_cache["data"] and (now - _regime_cache["ts"]) < 300:
        return _regime_cache["data"]
    try:
        vix = get_vix()
        spy_candles = get_daily_candles("SPY", days=30)
        regime = risk_manager.classify_regime(vix=vix, spy_candles=spy_candles)
        _regime_cache["data"] = regime
        _regime_cache["ts"] = now
        log.info(f"Regime: {regime.get('label')} (VIX {vix:.1f}, ADX {regime.get('adx', 0):.0f})")
        return regime
    except Exception as e:
        log.warning(f"Regime detection failed: {e}")
        return {"label": "UNKNOWN", "emoji": "❓", "vix": 0, "adx": 0,
                "vix_regime": "UNKNOWN", "adx_regime": "UNKNOWN", "size_mult": 1.0}


def _market_today_date():
    """Use America/Chicago so 0DTE/1DTE align with your trading day, not UTC midnight."""
    try:
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _dedupe_expirations_by_dte(valid_exps: list, max_per_dte: int = 1) -> list:
    """
    When provider expirations straddle UTC midnight, two expirations can both appear as DTE 0.
    Keep only the earliest expiration per DTE bucket to save API calls.
    """
    buckets = {}
    for dte, exp in valid_exps:
        buckets.setdefault(dte, []).append(exp)

    deduped = []
    for dte in sorted(buckets.keys()):
        # earliest expiry first; if caller wants more than one per DTE, keep that many
        for exp in sorted(buckets[dte])[:max_per_dte]:
            deduped.append((dte, exp))
    return deduped


def _is_http_429(exc: Exception) -> bool:
    try:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status == 429 or "429" in str(exc)
    except Exception:
        return "429" in str(exc)



# ─────────────────────────────────────────────────────────
# OPTIONS CHAIN — SCALP (0-10 DTE)
# ─────────────────────────────────────────────────────────

def get_options_chain(ticker: str) -> list:
    from trading_rules import MAX_EXPIRATIONS_TO_PULL
    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = _market_today_date()

    valid_exps = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if MIN_DTE <= dte <= MAX_DTE:
                valid_exps.append((dte, exp))
        except Exception:
            continue
    valid_exps.sort(key=lambda x: x[0])
    valid_exps = _dedupe_expirations_by_dte(valid_exps, max_per_dte=1)

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
    saw_rate_limit = False

    for dte, exp in exps_to_fetch:
        try:
            data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", {"expiration": exp})
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
                    "right": right, "strike": as_float(col("strike", None)[i], None),
                    "expiration": exp, "dte": dte,
                    "openInterest": as_int(col("openInterest", 0)[i], 0),
                    "volume": as_int(col("volume", 0)[i], 0),
                    "iv": as_float(col("iv", None)[i], None),
                    "gamma": as_float(col("gamma", 0.0)[i], 0.0),
                    "delta": as_float(col("delta", None)[i], None),
                    "theta": as_float(col("theta", None)[i], None),
                    "vega": as_float(col("vega", None)[i], None),
                    "bid": as_float(col("bid", None)[i], None),
                    "ask": as_float(col("ask", None)[i], None),
                    "mid": as_float(col("mid", None)[i], None),
                })
            if contracts:
                results.append((exp, dte, contracts))
                log.info(f"{ticker}: fetched {len(contracts)} contracts for {exp} (DTE {dte})")
        except Exception as e:
            if _is_http_429(e):
                saw_rate_limit = True
                log.warning(f"{ticker}: rate limit while fetching chain for {exp}: {e}")
                break
            log.warning(f"{ticker}: failed to fetch chain for {exp}: {e}")
            continue

    if not results:
        if saw_rate_limit:
            raise RuntimeError(f"Rate limit hit while fetching options chain for {ticker}. Try again shortly.")
        raise RuntimeError(f"No valid chains fetched for {ticker}")
    return results


# ─────────────────────────────────────────────────────────
# OPTIONS CHAIN — SWING (7-60 DTE)
# ─────────────────────────────────────────────────────────

def get_options_chain_swing(ticker: str) -> list:
    from swing_engine import SWING_MIN_DTE, SWING_MAX_DTE, SWING_MAX_EXPIRATIONS
    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = _market_today_date()

    valid_exps = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if SWING_MIN_DTE <= dte <= SWING_MAX_DTE:
                valid_exps.append((dte, exp))
        except Exception:
            continue
    valid_exps.sort(key=lambda x: x[0])
    valid_exps = _dedupe_expirations_by_dte(valid_exps, max_per_dte=1)

    if not valid_exps:
        raise RuntimeError(f"No swing expirations ({SWING_MIN_DTE}-{SWING_MAX_DTE} DTE) for {ticker}")

    results = []
    saw_rate_limit = False
    for dte, exp in valid_exps[:SWING_MAX_EXPIRATIONS]:
        try:
            data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", {"expiration": exp})
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
                    "right": right, "strike": as_float(col("strike", None)[i], None),
                    "expiration": exp, "dte": dte,
                    "openInterest": as_int(col("openInterest", 0)[i], 0),
                    "volume": as_int(col("volume", 0)[i], 0),
                    "iv": as_float(col("iv", None)[i], None),
                    "gamma": as_float(col("gamma", 0.0)[i], 0.0),
                    "delta": as_float(col("delta", None)[i], None),
                    "theta": as_float(col("theta", None)[i], None),
                    "vega": as_float(col("vega", None)[i], None),
                    "bid": as_float(col("bid", None)[i], None),
                    "ask": as_float(col("ask", None)[i], None),
                    "mid": as_float(col("mid", None)[i], None),
                })
            if contracts:
                results.append((exp, dte, contracts))
                log.info(f"Swing {ticker}: fetched {len(contracts)} contracts for {exp} (DTE {dte})")
        except Exception as e:
            log.warning(f"Swing {ticker}: failed chain for {exp}: {e}")
            continue
    return results


def _get_contracts_for_expiry(ticker: str, exp: str) -> list:
    """Fetch a single-expiry chain snapshot for wheel/monitor suggestions."""
    data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", {"expiration": exp})
    if not isinstance(data, dict) or data.get("s") != "ok":
        return []
    sym_list = data.get("optionSymbol") or []
    if not sym_list:
        return []
    n = len(sym_list)

    def col(name, default=None):
        v = data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    contracts = []
    sides = col("side", "")
    for i in range(n):
        right = (sides[i] or "").lower().strip()
        right = "call" if right in ("c", "call") else "put" if right in ("p", "put") else right
        bid = as_float(col("bid", None)[i], None)
        ask = as_float(col("ask", None)[i], None)
        mid = as_float(col("mid", None)[i], None)
        strike = as_float(col("strike", None)[i], None)
        if strike is None:
            continue
        contracts.append({
            "optionSymbol": col("optionSymbol", "")[i],
            "right": right,
            "strike": strike,
            "expiration": exp,
            "openInterest": as_int(col("openInterest", 0)[i], 0),
            "volume": as_int(col("volume", 0)[i], 0),
            "iv": as_float(col("iv", None)[i], None),
            "delta": as_float(col("delta", None)[i], None),
            "bid": bid,
            "ask": ask,
            "mid": mid,
        })
    return contracts


def _wheel_est_credit(contract: dict) -> float | None:
    bid = contract.get("bid")
    ask = contract.get("ask")
    mid = contract.get("mid")
    vals = [v for v in (bid, ask, mid) if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    if bid and mid:
        return round((bid + mid) / 2.0, 2)
    if mid:
        return round(mid, 2)
    return round(bid or ask or 0.0, 2)


def _score_wheel_candidate(c: dict, target_delta: float, liquidity_floor: int = 50) -> float:
    oi = c.get("openInterest") or 0
    vol = c.get("volume") or 0
    delta = abs(c.get("delta") or 0.0)
    bid = c.get("bid") or 0.0
    ask = c.get("ask") or 0.0
    spread_pen = 0.0
    if bid > 0 and ask >= bid:
        mid = max((bid + ask) / 2.0, 0.01)
        spread_pen = max(0.0, (ask - bid) / mid)
    liq_bonus = min((oi + vol) / max(liquidity_floor, 1), 2.0)
    return -abs(delta - target_delta) + 0.20 * liq_bonus - 0.15 * spread_pen


def _pick_wheel_short(contract_rows: list, side: str, spot: float, em: dict, walls: dict, adjusted_basis=None):
    side = side.lower().strip()
    candidates = []
    target_delta = 0.22
    for c in contract_rows:
        if c.get("right") != side:
            continue
        strike = c.get("strike")
        if strike is None:
            continue
        credit = _wheel_est_credit(c)
        if credit is None or credit < 0.05:
            continue
        oi = c.get("openInterest") or 0
        delta = abs(c.get("delta") or 0.0)
        if side == "call":
            if strike <= spot:
                continue
            if adjusted_basis is not None and strike < adjusted_basis:
                continue
            structural_ok = strike >= max((adjusted_basis or 0), spot + 0.35 * (em.get("em_1sd") or 0))
            if walls.get("call_wall"):
                structural_ok = structural_ok or strike >= walls.get("call_wall")
        else:
            if strike >= spot:
                continue
            structural_ok = strike <= spot - 0.30 * (em.get("em_1sd") or 0)
            if walls.get("put_wall"):
                structural_ok = structural_ok or strike <= walls.get("put_wall")
        if delta and not (0.10 <= delta <= 0.40):
            continue
        score = _score_wheel_candidate(c, target_delta)
        if structural_ok:
            score += 0.30
        if oi < 25:
            score -= 0.40
        candidates.append((score, c, credit))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, c, credit = candidates[0]
    strike = c.get("strike")
    delta = abs(c.get("delta") or 0.0)
    oi = c.get("openInterest") or 0
    bid = c.get("bid") or 0.0
    ask = c.get("ask") or 0.0
    mid = c.get("mid") or 0.0
    return {
        "strike": strike,
        "credit": credit,
        "delta": delta,
        "oi": oi,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "expiration": c.get("expiration"),
    }


def _wheel_label_from_delta(delta: float | None, option_type: str) -> tuple[str, str]:
    d = abs(delta or 0.0)
    if d <= 0.18:
        return "Conservative", "🟢"
    if d <= 0.30:
        return "Balanced", "🟡"
    return "Aggressive", "🔴"


def _wheel_premium_quality(credit: float | None, spot: float | None) -> tuple[str, str]:
    try:
        ratio = (credit or 0.0) / max(spot or 0.0, 0.01)
    except Exception:
        ratio = 0.0
    if ratio >= 0.035:
        return "Strong", "🟢"
    if ratio >= 0.015:
        return "Fair", "🟡"
    return "Light", "⚪"


def _build_wheel_focus_block(ticker: str, expiry: str, spot: float, em: dict, walls: dict):
    """Return wheel-focused lines for /monitorlong using the selected buffered expiry."""
    try:
        wheel = risk_manager.calc_wheel_pnl(ticker, account="brad")
    except Exception:
        wheel = {"has_shares": False, "adjusted_basis": None, "stage": "ACTIVE", "stage_emoji": "🔄", "shares": 0}
    contracts = _get_contracts_for_expiry(ticker, expiry)
    if not contracts:
        return []

    adjusted_basis = wheel.get("adjusted_basis")
    cc = _pick_wheel_short(contracts, "call", spot, em, walls or {}, adjusted_basis=adjusted_basis)
    csp = _pick_wheel_short(contracts, "put", spot, em, walls or {}, adjusted_basis=None)

    lines = ["", "🔄 Wheel Focus (30 DTE style):"]
    stage = wheel.get("stage", "ACTIVE")
    stage_emoji = wheel.get("stage_emoji", "🔄")
    basis_txt = f" | Adjusted basis: ${adjusted_basis:.2f}" if adjusted_basis is not None else ""
    lines.append(f"  {stage_emoji} Stage: {stage}{basis_txt}")

    if wheel.get("has_shares") and cc:
        fit, fit_emoji = _wheel_label_from_delta(cc.get("delta"), "call")
        prem, prem_emoji = _wheel_premium_quality(cc.get("credit"), spot)
        basis_guard = f" above basis ${adjusted_basis:.2f}" if adjusted_basis is not None else " above spot"
        lines.append(
            f"  📞 Preferred CC: Sell {ticker} {expiry} ${cc['strike']:.1f}C for ~${cc['credit']:.2f} credit "
            f"(Δ {cc['delta']:.2f}, OI {cc['oi']})"
        )
        lines.append(f"     Why: keeps the call{basis_guard} and nearer the upper expected-move / resistance zone without capping too early.")
        lines.append(f"     ⚖️ Wheel fit: {fit_emoji} {fit} | 💵 Premium quality: {prem_emoji} {prem}")
    elif cc:
        fit, fit_emoji = _wheel_label_from_delta(cc.get("delta"), "call")
        prem, prem_emoji = _wheel_premium_quality(cc.get("credit"), spot)
        lines.append(
            f"  📞 CC watch: {ticker} {expiry} ${cc['strike']:.1f}C ~${cc['credit']:.2f} credit "
            f"(Δ {cc['delta']:.2f}, OI {cc['oi']})"
        )
        lines.append("     Why: sits above spot and closer to the upper expected-move / resistance area, which gives upside room before assignment risk climbs.")
        lines.append(f"     ⚖️ Wheel fit: {fit_emoji} {fit} | 💵 Premium quality: {prem_emoji} {prem}")

    if csp:
        fit, fit_emoji = _wheel_label_from_delta(csp.get("delta"), "put")
        prem, prem_emoji = _wheel_premium_quality(csp.get("credit"), spot)
        lines.append(
            f"  🔻 Preferred CSP: Sell {ticker} {expiry} ${csp['strike']:.1f}P for ~${csp['credit']:.2f} credit "
            f"(Δ {csp['delta']:.2f}, OI {csp['oi']})"
        )
        lines.append("     Why: places the strike under spot and closer to support / lower expected-move territory.")
        lines.append(f"     ⚖️ Wheel fit: {fit_emoji} {fit} | 💵 Premium quality: {prem_emoji} {prem}")

    if not cc and not csp:
        lines.append("  No clean 30 DTE wheel strikes found on this chain.")
    return lines


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
                # v4.2: clamp to sane range — same fix as swing_engine avg_iv computation.
                # Deep OTM / near-expiry contracts return IV > 100 from MarketData.app
                # and would inflate the rank to 100 for every ticker.
                if iv and SWING_IV_MIN < iv < SWING_IV_MAX:
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
        warn_direction = "bull"; warn_side = "call"
    elif signal_bias == "bull":
        warn_direction = "bear"; warn_side = "put"
    else:
        return

    for account in ("brad", "mom"):
        try:
            open_spreads = portfolio.get_open_spreads_for_ticker(ticker, account=account)
            if not open_spreads:
                continue
            at_risk = [sp for sp in open_spreads
                       if sp.get("direction", "bull") == warn_direction or sp.get("side", "call") == warn_side]
            if not at_risk:
                continue

            tier = webhook_data.get("tier", "?")
            wt2 = as_float(webhook_data.get("wt2"), 0)
            close_price = as_float(webhook_data.get("close"), 0)
            wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
            trend_str = ("✅ Confirmed" if webhook_data.get("htf_confirmed")
                         else "🟡 Converging" if webhook_data.get("htf_converging")
                         else "❌ Diverging")
            acct_label = "👩 Mom" if account == "mom" else "📁 Brad"

            for sp in at_risk:
                sp_debit = sp.get("debit", 0); sp_contracts = sp.get("contracts", 1)
                total_risk = sp_debit * sp_contracts * 100
                targets = sp.get("targets", {}); short_strike = sp.get("short", 0)
                long_strike = sp.get("long", 0); sp_side = sp.get("side", "call")
                side_label = "BULL CALL" if sp_side == "call" else "BEAR PUT"
                urgency = "⚠️"; urgency_note = "Monitor position"

                if close_price > 0 and sp_side == "call":
                    if short_strike > 0 and close_price <= short_strike:
                        urgency = "🚨"; urgency_note = "PRICE AT/BELOW SHORT STRIKE"
                    elif long_strike > 0 and close_price <= long_strike:
                        urgency = "🔴"; urgency_note = "Price between strikes — partial loss"
                elif close_price > 0 and sp_side == "put":
                    if short_strike > 0 and close_price >= short_strike:
                        urgency = "🚨"; urgency_note = "PRICE AT/ABOVE SHORT STRIKE"
                    elif long_strike > 0 and close_price >= long_strike:
                        urgency = "🔴"; urgency_note = "Price between strikes — partial loss"

                stop_level = targets.get("stop", 0)

                lines = [
                    f"{urgency} EXIT WARNING — {ticker} {side_label} ({acct_label})",
                    f"TV Signal: T{tier} {signal_bias.upper()} | Close: ${close_price:.2f}",
                    f"1H Trend: {trend_str} | Wave: {wave_zone}", "",
                    f"Open Spread: {sp['id']}",
                    f"  ${long_strike}/{short_strike} @${sp_debit:.2f} x{sp_contracts}",
                    f"  Risk: ${total_risk:,.0f} | Exp: {sp.get('exp', '?')}",
                    f"  {urgency_note}", "",
                    f"Targets: sell@${targets.get('same_day', 0):.2f} (30%) | "
                    f"${targets.get('next_day', 0):.2f} (35%) | ${targets.get('extended', 0):.2f} (50%)",
                ]
                if stop_level:
                    lines.append(f"Stop: ${stop_level:.2f}")
                lines.extend(["", "Action: Consider closing or tightening stop", "— Not financial advice —"])
                post_to_telegram("\n".join(lines))
                log.info(f"Exit warning posted for {ticker} {side_label} spread {sp['id']} ({account})")
        except Exception as e:
            log.error(f"Exit warning check failed for {ticker}/{account}: {e}")


# ─────────────────────────────────────────────────────────
# V4 PREFILTER — institutional flow quality gate for scalp engine
# Runs v4 snapshot on nearest-DTE chain before spread selection.
# Returns a v4_flow dict that compute_confidence can score.
# ─────────────────────────────────────────────────────────

def _contracts_to_chain_data(contracts: list) -> dict:
    """
    Convert v3 contract dicts back to MarketData.app columnar format
    so engine_bridge can consume it.
    """
    if not contracts:
        return {}
    keys = {
        "optionSymbol": "optionSymbol", "strike": "strike", "side": "right",
        "iv": "iv", "openInterest": "openInterest", "volume": "volume",
        "delta": "delta", "gamma": "gamma", "theta": "theta", "vega": "vega",
        "bid": "bid", "ask": "ask",
    }
    result = {"s": "ok"}
    for md_key, contract_key in keys.items():
        result[md_key] = [c.get(contract_key) for c in contracts]
    return result


def _run_v4_prefilter(ticker: str, spot: float, chains: list, candle_closes: list) -> dict:
    """
    Run v4 institutional snapshot on the nearest-DTE chain.
    Returns a v4_flow dict for compute_confidence, or empty dict on failure.

    The v4_flow dict contains:
      - confidence_label: HIGH/MODERATE/LOW
      - gex: net GEX value
      - bias: UPSIDE/DOWNSIDE/NEUTRAL
      - gamma_flip: flip price or None
      - spot: current spot
      - composite_regime: regime label string
      - downgrades: list of downgrade flags
    """
    try:
        if not chains:
            return {}

        # Use nearest-DTE chain (first in list, already sorted by DTE)
        exp, dte, contracts = chains[0]

        # Convert to MarketData.app format for the v4 engine
        chain_data = _contracts_to_chain_data(contracts)
        if not chain_data.get("optionSymbol"):
            return {}

        # Apply OI cache
        _oi_cache.apply_oi_changes_to_chain(ticker, exp, chain_data)

        # Build OHLC bars from candle closes for RV
        bars = _get_ohlc_bars(ticker, days=65)

        # Estimate liquidity
        adv, spread_pct = _estimate_liquidity(ticker, spot)

        # Run v4 snapshot
        v4 = run_institutional_snapshot(
            chain_data=chain_data, spot=spot, dte=max(dte, 0.5),
            recent_bars=bars, is_0dte=(dte == 0),
            avg_daily_dollar_volume=adv,
            bid_ask_spread_pct=spread_pct,
            liquid_index=_is_liquid(ticker),
        )

        # Save OI snapshot for next run
        _oi_cache.save_snapshot(ticker, exp, chain_data)

        if v4.get("error"):
            log.warning(f"v4 prefilter failed for {ticker}: {v4['error']}")
            return {}

        snap = v4.get("snapshot", {})
        confidence = snap.get("confidence", {})
        dealer = snap.get("dealer_flows", {})
        regime = snap.get("regime", {})
        adj = snap.get("adjusted_expectation", {})

        v4_flow = {
            "confidence_label": confidence.get("label", ""),
            "confidence_score": confidence.get("composite", 0),
            "gex": dealer.get("gex", 0),
            "dex": dealer.get("dex", 0),
            "vanna": dealer.get("vanna", 0),
            "charm": dealer.get("charm", 0),
            "bias": adj.get("bias", "NEUTRAL"),
            "bias_score": adj.get("bias_score", 0),
            "gamma_flip": dealer.get("gamma_flip"),
            "spot": spot,
            "composite_regime": regime.get("regime", ""),
            "downgrades": snap.get("downgrades", []),
            "vol_regime_label": snap.get("volatility_regime", {}).get("label", ""),
        }

        log.info(f"v4 prefilter {ticker}: conf={confidence.get('label')} "
                 f"gex={dealer.get('gex', 0):.0f} bias={adj.get('bias')} "
                 f"regime={regime.get('regime', '?')} flip={dealer.get('gamma_flip')}")

        return v4_flow

    except Exception as e:
        log.warning(f"v4 prefilter error for {ticker}: {e}")
        return {}


# ─────────────────────────────────────────────────────────
# CHECK TICKER — scalp engine (0-10 DTE) + v4 flow quality gate
# ─────────────────────────────────────────────────────────

def _signal_validation_thresholds(webhook_data: dict) -> tuple[float, float]:
    timeframe = str((webhook_data or {}).get("timeframe") or "").lower()
    is_swing = any(tag in timeframe for tag in ("d", "w", "day", "week")) or bool((webhook_data or {}).get("is_swing"))
    if is_swing:
        return SWING_SIGNAL_WARN_DRIFT_PCT, SWING_SIGNAL_REJECT_DRIFT_PCT
    return SCALP_SIGNAL_WARN_DRIFT_PCT, SCALP_SIGNAL_REJECT_DRIFT_PCT


def _validate_live_signal(ticker: str, live_spot: float, webhook_data: dict | None) -> dict:
    webhook_data = webhook_data or {}
    alert_close = as_float(webhook_data.get("close"), 0.0)
    received_at = as_float(webhook_data.get("received_at_epoch"), 0.0)
    now_ts = time.time()
    signal_age_sec = max(0, int(now_ts - received_at)) if received_at else None
    warn_pct, reject_pct = _signal_validation_thresholds(webhook_data)

    result = {
        "ok": True,
        "label": "LIVE_OK",
        "reason": "",
        "signal_age_sec": signal_age_sec,
        "alert_close": alert_close if alert_close > 0 else None,
        "live_spot": live_spot,
        "drift_pct": None,
        "warn_threshold_pct": warn_pct,
        "reject_threshold_pct": reject_pct,
        "confidence_penalty": 0,
        "card_note": "",
    }

    if alert_close <= 0 or live_spot <= 0:
        result["label"] = "NO_REFERENCE"
        result["reason"] = "Missing alert close or live spot for validation"
        result["card_note"] = "🟡 Signal validation: alert close unavailable — trade priced from live data only."
        return result

    drift_pct = abs((live_spot - alert_close) / alert_close) * 100.0
    result["drift_pct"] = round(drift_pct, 3)

    if signal_age_sec is not None and signal_age_sec > SIGNAL_STALE_AFTER_SEC:
        result.update({
            "ok": False,
            "label": "STALE_SIGNAL",
            "reason": f"Signal stale ({signal_age_sec}s old > {SIGNAL_STALE_AFTER_SEC}s)",
            "card_note": f"🚫 Signal validation: stale alert ({signal_age_sec}s old).",
        })
        return result

    if drift_pct >= reject_pct:
        result.update({
            "ok": False,
            "label": "DRIFT_REJECT",
            "reason": f"Live spot drift {drift_pct:.2f}% exceeded reject threshold {reject_pct:.2f}%",
            "card_note": f"🚫 Signal validation: live spot moved {drift_pct:.2f}% from TV alert (${alert_close:.2f} → ${live_spot:.2f}).",
        })
        return result

    if drift_pct >= warn_pct:
        result.update({
            "label": "DRIFT_WARN",
            "reason": f"Live spot drift {drift_pct:.2f}% exceeded warn threshold {warn_pct:.2f}%",
            "confidence_penalty": SIGNAL_MODERATE_CONF_PENALTY,
            "card_note": f"🟡 Signal validation: live spot drift {drift_pct:.2f}% vs TV alert (${alert_close:.2f} → ${live_spot:.2f}); confidence reduced.",
        })
        return result

    if drift_pct >= (warn_pct * 0.5):
        result.update({
            "label": "DRIFT_LIGHT",
            "reason": f"Live spot drift {drift_pct:.2f}% is elevated but within limits",
            "confidence_penalty": SIGNAL_WARN_CONF_PENALTY,
            "card_note": f"🟢 Signal validation: live spot drift only {drift_pct:.2f}% from TV alert (${alert_close:.2f} → ${live_spot:.2f}).",
        })
        return result

    result["card_note"] = f"🟢 Signal validation: TV ${alert_close:.2f} vs live ${live_spot:.2f} ({drift_pct:.2f}% drift)."
    return result


def check_ticker(ticker, direction="bull", webhook_data=None):
    ticker = ticker.strip().upper()
    webhook_data = webhook_data or {"bias": direction, "tier": "2"}

    try:
        # v4.3: Smart prefetch-aware data loading
        # If a prefetch wave is in progress, wait for this ticker's data
        # rather than hitting APIs independently and causing rate limit cascades
        cached = _prefetch_get(ticker)
        if not cached and _prefetch_thread_active:
            wait_start = time.time()
            while (time.time() - wait_start) < PREFETCH_WORKER_WAIT_SEC:
                time.sleep(2)
                cached = _prefetch_get(ticker)
                if cached:
                    log.info(f"check_ticker({ticker}): prefetch ready ({time.time()-wait_start:.0f}s wait)")
                    break
                if not _prefetch_thread_active:
                    break
            if not cached:
                log.info(f"check_ticker({ticker}): prefetch wait expired — live fetch")

        if cached and cached.get("spot") and cached.get("chains"):
            spot = cached["spot"]
            chains = cached["chains"]
            enrichment = cached.get("enrichment", {})
            candle_closes = cached.get("candle_closes", [])
            regime = cached.get("regime") or get_current_regime()
            v4_flow = cached.get("v4_flow", {})
            has_earnings = enrichment.get("has_earnings", False)
            earnings_warn = enrichment.get("earnings_warn")
            log.info(f"check_ticker({ticker}): using prefetch cache")
        else:
            # No cache — fetch live once, then store so the opposite side of /check can reuse it.
            spot = get_spot(ticker); chains = get_options_chain(ticker)
            try:
                enrichment = enrich_ticker(ticker)
            except Exception:
                enrichment = {"has_earnings": False, "earnings_warn": None}
            has_earnings = enrichment.get("has_earnings", False)
            earnings_warn = enrichment.get("earnings_warn")
            candle_closes = get_daily_candles(ticker, days=RV_LOOKBACK_DAYS + 5)
            regime = get_current_regime()
            v4_flow = _run_v4_prefilter(ticker, spot, chains, candle_closes)
            _prefetch_set(ticker, {
                "spot": spot,
                "chains": chains,
                "enrichment": enrichment,
                "candle_closes": candle_closes,
                "regime": regime,
                "v4_flow": v4_flow,
            })
            log.info(f"check_ticker({ticker}): live fetch cached for reuse")
        has_dividend = False

        log.info(f"check_ticker({ticker}): spot={spot} expirations={len(chains)} "
                 f"earnings={has_earnings} candles={len(candle_closes)} direction={direction}"
                 f"{' v4=' + v4_flow.get('composite_regime', '?') if v4_flow else ''}")

        signal_validation = _validate_live_signal(ticker, spot, webhook_data)
        webhook_data = dict(webhook_data or {})
        webhook_data["live_spot"] = spot
        webhook_data["signal_validation"] = signal_validation

        if not signal_validation.get("ok", True):
            reason = signal_validation.get("reason") or "signal validation failed"
            trade_journal.log_signal(ticker, webhook_data, outcome="rejected", reason=reason[:200])
            _log_signal_dataset_event(ticker, webhook_data, outcome="rejected_validation", reason=reason, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot, expirations_checked=len(chains) if chains else 0)
            log.info(f"check_ticker({ticker}): rejected by validation — {reason}")
            return {
                "ticker": ticker, "ok": False, "posted": False,
                "reason": reason,
                "confidence": None,
                "signal_validation": signal_validation,
            }

        all_recs = []; all_reasons = []
        for exp, dte, contracts in chains:
            rec = recommend_trade(
                ticker=ticker, spot=spot, contracts=contracts, dte=dte,
                expiration=exp, webhook_data=webhook_data,
                has_earnings=has_earnings, has_dividend=has_dividend,
                candle_closes=candle_closes, regime=regime,
                v4_flow=v4_flow,
            )
            if rec.get("ok"):
                all_recs.append(rec)
                log.info(f"  {exp} (DTE {dte}): ✅ {direction} trade found")
            else:
                reason = rec.get("reason", "unknown")
                all_reasons.append(f"DTE {dte} ({exp}): {reason}")
                log.info(f"  {exp} (DTE {dte}): ❌ {reason}")

        if not all_recs:
            combined_reason = "No valid spreads across any expiration"
            if all_reasons:
                combined_reason += "\n" + "\n".join(all_reasons[:4])
            trade_journal.log_signal(ticker, webhook_data, outcome="rejected", reason=combined_reason[:200])
            _log_signal_dataset_event(ticker, webhook_data, outcome="rejected_no_setup", reason=combined_reason, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot, expirations_checked=len(chains))
            return {"ticker": ticker, "ok": False, "posted": False,
                    "reason": combined_reason, "confidence": None}

        def rec_score(r):
            trade = r.get("trade", {}); ror = trade.get("ror", 0)
            width = trade.get("width", 5); dte_val = r.get("dte", 5)
            width_bonus = 0.3 if width <= 1.0 else 0.1 if width <= 2.5 else 0
            dte_bonus = 0.1 * (1.0 / (1 + abs(dte_val - TARGET_DTE)))
            return ror + width_bonus + dte_bonus

        all_recs.sort(key=rec_score, reverse=True)
        best_rec = all_recs[0]

        other_exps = []; seen_exps = {best_rec.get("exp")}
        for r in all_recs[1:]:
            if r.get("exp") not in seen_exps:
                seen_exps.add(r.get("exp")); other_exps.append(r)

        penalty = int((signal_validation or {}).get("confidence_penalty") or 0)
        if penalty > 0:
            base_conf = int(best_rec.get("confidence", 0) or 0)
            best_rec["confidence_pre_validation"] = base_conf
            best_rec["confidence"] = max(0, base_conf - penalty)
            best_rec["signal_validation_penalty"] = penalty

        trade = best_rec.get("trade", {})

        if is_duplicate_trade(ticker, direction, trade.get("short"), trade.get("long")):
            trade_journal.log_signal(ticker, webhook_data, outcome="duplicate", confidence=best_rec.get("confidence"))
            _log_signal_dataset_event(ticker, webhook_data, outcome="duplicate", reason="Duplicate trade in dedup window", best_rec=best_rec, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot, expirations_checked=len(chains))
            return {"ticker": ticker, "ok": True, "posted": False, "reason": "Duplicate trade in dedup window"}

        risk_result = risk_manager.check_risk_limits(
            ticker=ticker, debit=trade.get("debit", 0),
            contracts=best_rec.get("contracts", 1), regime=regime, direction=direction,
        )

        card = format_trade_card(best_rec)
        validation_note = (signal_validation or {}).get("card_note")
        if validation_note:
            card = validation_note + "\n\n" + card

        if not risk_result["allowed"]:
            block_reasons = "; ".join(risk_result["blocks"])
            card = "🚫 RISK LIMIT HIT — DO NOT ENTER\n" + block_reasons + "\n\n" + card
        if risk_result.get("warnings"):
            card += "\n⚠️ Risk: " + " | ".join(risk_result["warnings"][:3])
        if other_exps:
            alt_lines = ["\n📅 Other Expirations:"]
            for r in other_exps[:3]:
                t = r.get("trade", {})
                alt_lines.append(f"  DTE {r['dte']} ({r['exp']}): ${t['debit']:.2f} on ${t['width']} wide | RoR {t['ror']:.0%} | {t['long']}/{t['short']}")
            card += "\n".join(alt_lines)
        if has_earnings and earnings_warn:
            card = earnings_warn + "\n\n" + card

        conf = best_rec.get("confidence", 0)
        log.info(f"Trade card built: {ticker} {direction} conf={conf}/100")
        mark_trade_sent(ticker, direction, trade.get("short"), trade.get("long"))
        trade_journal.log_signal(ticker, webhook_data,
            outcome="trade_opened" if risk_result["allowed"] else "risk_blocked",
            confidence=best_rec.get("confidence"))
        _log_signal_dataset_event(
            ticker, webhook_data,
            outcome="trade_opened" if risk_result["allowed"] else "risk_blocked",
            reason=("; ".join(risk_result.get("blocks", [])) if not risk_result["allowed"] else ""),
            best_rec=best_rec, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow,
            spot=spot, expirations_checked=len(chains)
        )

        # v4.3: Cache card for /tradecard retrieval (same as wave digest path)
        try:
            cache_key = f"tradecard:{ticker.upper()}"
            store_set(cache_key, card, ttl=DIGEST_CARD_CACHE_TTL_SEC)
            log.debug(f"Trade card cached: {cache_key}")
        except Exception:
            pass  # non-critical

        return {
            "ticker": ticker, "ok": True, "posted": True, "card": card,
            "confidence": best_rec.get("confidence"), "trade": trade,
            "expirations_checked": len(chains),
            "signal_validation": signal_validation,
        }
    except Exception as e:
        log.error(f"check_ticker({ticker}): {type(e).__name__}: {e}")
        if _is_http_429(e) or "rate limit" in str(e).lower():
            return {"ticker": ticker, "ok": False, "posted": False,
                    "reason": f"Rate limit hit while checking {ticker}. Try again shortly.",
                    "error": f"{type(e).__name__}: {str(e)[:160]}"}
        return {"ticker": ticker, "ok": False, "posted": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    r = _get_redis()
    queue_depth = r.llen(REDIS_QUEUE_KEY) if r else _mem_signal_queue.qsize()
    return jsonify({"status": "ok", "redis": r is not None, "queue_depth": queue_depth,
                    "workers": QUEUE_WORKERS, "engine_version": SCHEMA_VERSION})

@app.route("/debug", methods=["GET"])
def debug():
    r = _get_redis()
    queue_depth = r.llen(REDIS_QUEUE_KEY) if r else _mem_signal_queue.qsize()
    return jsonify({
        "TELEGRAM_set": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "MARKETDATA_set": bool(MARKETDATA_TOKEN),
        "REDIS_connected": r is not None,
        "REDIS_queue_depth": queue_depth,
        "WATCHLIST_len": len([t for t in WATCHLIST.split(",") if t.strip()]),
        "ENGINE_VERSION": SCHEMA_VERSION,
        "QUEUE_WORKERS": QUEUE_WORKERS,
        "SIGNAL_TTL_S": SIGNAL_TTL_SEC,
        "DEDUP_TTL_S": DEDUP_TTL_SECONDS,
        "ALLOWED_DIRECTIONS": ALLOWED_DIRECTIONS,
        "PORTFOLIO_CHANNEL_set": bool(TELEGRAM_PORTFOLIO_CHAT_ID),
        "MOM_CHANNEL_set": bool(TELEGRAM_MOM_PORTFOLIO_CHAT_ID),
        "API_CACHE": _cached_md.get_stats(),
        "SIGNAL_VALIDATION": {
            "SCALP_WARN_PCT": SCALP_SIGNAL_WARN_DRIFT_PCT,
            "SCALP_REJECT_PCT": SCALP_SIGNAL_REJECT_DRIFT_PCT,
            "SWING_WARN_PCT": SWING_SIGNAL_WARN_DRIFT_PCT,
            "SWING_REJECT_PCT": SWING_SIGNAL_REJECT_DRIFT_PCT,
            "STALE_AFTER_SEC": SIGNAL_STALE_AFTER_SEC,
        },
    })

@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram(f"✅ Telegram test OK (v4.0 engine {SCHEMA_VERSION})")
    return jsonify({"status": st, "body": body})


# ─────────────────────────────────────────────────────────
# TELEGRAM WEBHOOK
# ─────────────────────────────────────────────────────────

@app.route("/telegram_webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    if secret != os.getenv("TELEGRAM_WEBHOOK_SECRET", ""):
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    message = data.get("message") or data.get("edited_message") or {}
    if not message:
        return jsonify({"ok": True})
    text = message.get("text", ""); chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str(message.get("from", {}).get("id", ""))
    if not text.startswith("/"):
        return jsonify({"ok": True})

    cmd_word = text.split()[0].lower() if text.split() else text.lower()
    dedup_key = f"cmd_dedup:{user_id}:{cmd_word}"
    if store_exists(dedup_key):
        log.info(f"Command dedup hit — suppressing: {cmd_word} from {user_id}")
        return jsonify({"ok": True})
    store_set(dedup_key, "1", ttl=8)
    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]

    def run_command():
        handle_command(
            user_id=user_id, chat_id=chat_id, text=text,
            scan_fn=lambda t: check_ticker(t),
            full_scan_fn=lambda: scan_watchlist_internal(tickers),
            check_fn=check_ticker, watchlist=tickers,
            get_spot_fn=get_spot, md_get_fn=md_get, post_fn=post_to_telegram,
            get_portfolio_chat_id_fn=get_portfolio_chat_id,
            get_regime_fn=get_current_regime,
            post_em_card_fn=_post_em_card, post_monitor_card_fn=_post_monitor_card,
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

    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "").strip().lower()
    tier   = str(data.get("tier") or "").strip()
    close  = as_float(data.get("close"), None)

    validation_errors = []
    if not ticker: validation_errors.append("missing 'ticker'")
    elif not ticker.replace(".", "").isalpha(): validation_errors.append(f"invalid ticker '{ticker}'")
    if bias not in ("bull", "bear"): validation_errors.append(f"invalid bias '{bias}'")
    if tier not in ("1", "2", "3"): validation_errors.append(f"invalid tier '{tier}'")
    if close is None or close <= 0: validation_errors.append(f"invalid close '{data.get('close')}'")

    if validation_errors:
        reason = "; ".join(validation_errors)
        log.warning(f"TV webhook rejected — {reason}")
        return jsonify({"error": "invalid_payload", "reason": reason}), 400

    log.info(f"TV signal: {ticker} bias={bias} tier={tier} close={close}")

    webhook_data = {
        "tier": tier, "bias": bias, "close": close, "time": data.get("time", ""),
        "received_at_epoch": time.time(),
        "ema5": as_float(data.get("ema5")), "ema12": as_float(data.get("ema12")),
        "ema_dist_pct": as_float(data.get("ema_dist_pct")),
        "macd_hist": as_float(data.get("macd_hist")),
        "macd_line": as_float(data.get("macd_line")),
        "signal_line": as_float(data.get("signal_line")),
        "wt1": as_float(data.get("wt1")), "wt2": as_float(data.get("wt2")),
        "rsi_mfi": as_float(data.get("rsi_mfi")),
        "rsi_mfi_bull": data.get("rsi_mfi_bull") in (True, "true"),
        "stoch_k": as_float(data.get("stoch_k")), "stoch_d": as_float(data.get("stoch_d")),
        "vwap": as_float(data.get("vwap")),
        "above_vwap": data.get("above_vwap") in (True, "true"),
        "htf_confirmed": data.get("htf_confirmed") in (True, "true"),
        "htf_converging": data.get("htf_converging") in (True, "true"),
        "daily_bull": data.get("daily_bull") in (True, "true"),
        "volume": as_float(data.get("volume")), "timeframe": data.get("timeframe", ""),
    }

    def _build_signal_msg():
        tier_emoji = "🥇" if tier == "1" else "🥈" if tier == "2" else "📢"
        wt2 = webhook_data.get("wt2") or 0
        wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
        trend_str = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                     else "🟡 Converging" if webhook_data["htf_converging"] else "❌ Diverging")
        dir_emoji = "🐻" if bias == "bear" else "🐂"
        return "\n".join([
            f"{tier_emoji} TV Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
            f"Close: ${close:.2f} | {data.get('timeframe', '')} timeframe",
            f"1H Trend: {trend_str} | Daily: {'🟢' if webhook_data['daily_bull'] else '🔴'}",
            f"Wave: {wave_zone} (wt2={wt2:.1f})",
            f"VWAP: {'Above ✅' if webhook_data['above_vwap'] else 'Below'} | "
            f"RSI+MFI: {'Buying ✅' if webhook_data['rsi_mfi_bull'] else 'Selling'}", "",
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

    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "").strip().lower()
    tier   = str(data.get("tier") or "").strip()
    close  = as_float(data.get("close"), None)

    validation_errors = []
    if not ticker: validation_errors.append("missing 'ticker'")
    elif not ticker.replace(".", "").isalpha(): validation_errors.append(f"invalid ticker '{ticker}'")
    if bias not in ("bull", "bear"): validation_errors.append(f"invalid bias '{bias}'")
    if tier not in ("1", "2", "3"): validation_errors.append(f"invalid tier '{tier}'")
    if close is None or close <= 0: validation_errors.append(f"invalid close '{data.get('close')}'")

    if validation_errors:
        reason = "; ".join(validation_errors)
        log.warning(f"Swing webhook rejected — {reason}")
        return jsonify({"error": "invalid_payload", "reason": reason}), 400

    log.info(f"Swing signal: {ticker} bias={bias} tier={tier}")

    webhook_data = {
        "tier": tier, "bias": bias, "close": close, "time": data.get("time", ""),
        "received_at_epoch": time.time(),
        "fib_level": data.get("fib_level", "61.8"),
        "fib_distance_pct": as_float(data.get("fib_distance_pct"), 2.0),
        "fib_high": as_float(data.get("fib_high")), "fib_low": as_float(data.get("fib_low")),
        "fib_range": as_float(data.get("fib_range")),
        "fib_ext_127": as_float(data.get("fib_ext_127")),
        "fib_ext_162": as_float(data.get("fib_ext_162")),
        "weekly_bull": data.get("weekly_bull") in (True, "true"),
        "weekly_bear": data.get("weekly_bear") in (True, "true"),
        "htf_confirmed": data.get("htf_confirmed") in (True, "true"),
        "htf_converging": data.get("htf_converging") in (True, "true"),
        "daily_bull": data.get("daily_bull") in (True, "true"),
        "rsi": as_float(data.get("rsi")),
        "rsi_mfi_bull": data.get("rsi_mfi_bull") in (True, "true"),
        "vol_contracting": data.get("vol_contracting") in (True, "true"),
        "vol_expanding": data.get("vol_expanding") in (True, "true"),
        "volume": as_float(data.get("volume")), "timeframe": data.get("timeframe", "D"),
    }

    tier_emoji = "🥇" if tier == "1" else "🥈"
    dir_emoji  = "🐻" if bias == "bear" else "🐂"
    fib_level  = webhook_data["fib_level"]
    fib_emojis = {"61.8": "🌟", "50.0": "⭐", "38.2": "✨", "78.6": "💫"}
    fib_emoji  = fib_emojis.get(str(fib_level), "📐")
    weekly_str = "🟢 Bull" if webhook_data["weekly_bull"] else "🔴 Bear"
    daily_str  = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                  else "🟡 Converging" if webhook_data["htf_converging"] else "❌ Diverging")
    vol_str    = "🟢 Contracting" if webhook_data["vol_contracting"] else "📊 Normal"

    signal_msg = "\n".join([
        f"{tier_emoji} SWING Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
        f"Fib: {fib_emoji} {fib_level}% level ({webhook_data['fib_distance_pct']:.1f}% away)",
        f"Close: ${close:.2f} | {webhook_data['timeframe']} chart",
        f"Weekly: {weekly_str} | Daily: {daily_str}", f"Volume: {vol_str}", "",
    ])
    _enqueue_signal("swing", ticker, bias, webhook_data, signal_msg)
    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# 0DTE EM TRIGGER
# ─────────────────────────────────────────────────────────

EM_SCHEDULE_TIMES_CT = [(8, 45), (14, 45)]
EM_TICKERS = ["SPY", "QQQ"]

@app.route("/em", methods=["POST", "GET"])
def em_trigger():
    data = request.get_json(force=True, silent=True) or {}
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
    log.info("scan_watchlist_internal called but scan is disabled")
    return

@app.route("/scan", methods=["POST"])
def scan_watchlist():
    return jsonify({"status": "disabled", "reason": "Use TradingView webhooks only"}), 200


# ─────────────────────────────────────────────────────────
# EM RECONCILER — compares predictions to actual EOD prices
# ─────────────────────────────────────────────────────────

@app.route("/reconcile", methods=["POST", "GET"])
def reconcile_route():
    """
    Trigger EM prediction reconciliation.
    Compares past EM cards against actual EOD closes.
    POST/GET with secret param for auth.
    Optional params:
      - lookback: days to scan (default 5)
      - post: "true" to post summary to Telegram
    """
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    lookback = int(data.get("lookback", request.args.get("lookback", 5)))
    post_summary = str(data.get("post", request.args.get("post", "true"))).lower() == "true"

    def run():
        try:
            r = _get_redis()
            if not r:
                log.warning("Reconciler: no Redis — cannot scan")
                return

            # Build the EOD fetcher using app.py's md_get
            def eod_fetcher(ticker, date_str):
                return fetch_eod_close_marketdata(ticker, date_str, md_get)

            entries = reconcile_em_predictions(
                redis_client=r,
                fetch_eod_close=eod_fetcher,
                store_set=store_set,
                lookback_days=lookback,
            )

            if not entries:
                log.info("Reconciler: no entries to process")
                return

            stats = compute_accuracy_stats(entries)
            report = format_accuracy_report(stats)

            log.info(f"Reconciler stats: n={stats.get('n')} "
                     f"1σ={stats.get('in_1_sigma_pct')}% "
                     f"dir={stats.get('direction_correct_pct')}%")

            if post_summary and stats.get("n", 0) > 0:
                post_to_telegram(report)
                log.info("Reconciler: accuracy report posted to Telegram")

        except Exception as e:
            log.error(f"Reconciler error: {e}", exc_info=True)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "accepted", "lookback_days": lookback})


def _run_reconciler_auto():
    """Called by the EM scheduler at 4:15 PM CT to auto-reconcile."""
    try:
        r = _get_redis()
        if not r:
            return

        def eod_fetcher(ticker, date_str):
            return fetch_eod_close_marketdata(ticker, date_str, md_get)

        entries = reconcile_em_predictions(
            redis_client=r,
            fetch_eod_close=eod_fetcher,
            store_set=store_set,
            lookback_days=3,
        )

        if entries:
            stats = compute_accuracy_stats(entries)
            if stats.get("n", 0) >= 5:
                # Only post summary once we have enough data
                report = format_accuracy_report(stats)
                post_to_telegram(report)
                log.info(f"Auto-reconciler: posted accuracy report ({stats['n']} entries)")
            else:
                log.info(f"Auto-reconciler: {stats.get('n', 0)} entries — waiting for more data before posting")
    except Exception as e:
        log.error(f"Auto-reconciler error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────
# HOLDINGS SENTIMENT SCAN
# ─────────────────────────────────────────────────────────

@app.route("/holdings_scan", methods=["POST"])
def holdings_scan():
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    def run_scan():
        from sentiment_report import generate_sentiment_report
        for account in ("brad", "mom"):
            try:
                report = generate_sentiment_report(md_get, account=account)
                target_chat = get_portfolio_chat_id(account)
                post_to_telegram(report, chat_id=target_chat)
            except Exception as e:
                log.error(f"Holdings scan error ({account}): {e}")
                post_to_telegram(f"⚠️ Holdings scan failed ({account}): {type(e).__name__}",
                                chat_id=get_portfolio_chat_id(account))

    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "accepted"})


# ─────────────────────────────────────────────────────────
# CHAIN DATA CACHE
# ─────────────────────────────────────────────────────────

_CHAIN_CACHE_TTL = 60
_chain_cache: dict = {}
_chain_cache_lock = threading.Lock()

def _chain_cache_get(ticker: str, expiry: str):
    key = (ticker.upper(), expiry)
    with _chain_cache_lock:
        entry = _chain_cache.get(key)
        if entry and (time.monotonic() - entry[3]) < _CHAIN_CACHE_TTL:
            return entry[0], entry[1], entry[2]
        if entry:
            del _chain_cache[key]
    return None

def _chain_cache_set(ticker: str, expiry: str, data, spot: float):
    key = (ticker.upper(), expiry)
    with _chain_cache_lock:
        now = time.monotonic()
        stale = [k for k, v in _chain_cache.items() if (now - v[3]) >= _CHAIN_CACHE_TTL]
        for k in stale:
            del _chain_cache[k]
        _chain_cache[key] = (data, spot, expiry, now)


def _get_0dte_chain(ticker: str, target_date_str: str = None) -> tuple:
    try:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = target_date_str or today_utc
        cached = _chain_cache_get(ticker, target)
        if cached:
            return cached
        spot = get_spot(ticker)
        data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", {"expiration": target})
        if not isinstance(data, dict) or data.get("s") != "ok" or not data.get("optionSymbol"):
            exps = get_expirations(ticker)
            future_exps = [e for e in exps if e >= target]
            if not future_exps:
                return None, None, None
            target = future_exps[0]
            cached = _chain_cache_get(ticker, target)
            if cached:
                return cached
            data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", {"expiration": target})
            if not isinstance(data, dict) or data.get("s") != "ok":
                return None, None, None
        if not data.get("optionSymbol"):
            return None, None, None
        _chain_cache_set(ticker, target, data, spot)
        return data, spot, target
    except Exception as e:
        log.warning(f"Chain fetch failed for {ticker}: {e}")
        return None, None, None


def _get_next_trading_day(from_date) -> str:
    d = from_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────
# V4 ENGINE INTEGRATION — _get_0dte_iv
# Uses OI cache for oi_change, stock data for liquidity, liquid_index for spread detection
# ─────────────────────────────────────────────────────────

def _get_0dte_iv(ticker: str, target_date_str: str = None) -> tuple:
    """
    Full v4 institutional chain analysis.
    Returns: (iv, spot, expiration, engine_result, walls, skew, pcr, vix, v4_result)
    The 9th element (v4_result) contains confidence, downgrades, trade_sign, audit, vol_regime.
    """
    empty = (None, None, None, None, {}, {}, {}, {}, {})
    try:
        data, spot, target = _get_0dte_chain(ticker, target_date_str)
        if data is None:
            return empty

        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        from datetime import date as _date
        try:
            dte = max((_date.fromisoformat(target) - _date.fromisoformat(today_utc)).days, 0)
        except Exception:
            dte = 0

        # ── OI change: diff current OI against cached prior snapshot ──
        _oi_cache.apply_oi_changes_to_chain(ticker, target, data)

        # ── OHLC bars for RV ──
        bars = _get_ohlc_bars(ticker, days=65)

        # ── Estimate liquidity from stock data ──
        adv, spread = _estimate_liquidity(ticker, spot)

        v4 = run_institutional_snapshot(
            chain_data=data, spot=spot, dte=max(dte, 0.5),
            recent_bars=bars, is_0dte=(dte == 0),
            avg_daily_dollar_volume=adv,
            bid_ask_spread_pct=spread,
            liquid_index=_is_liquid(ticker),
        )

        # ── Save current OI as reference for next run ──
        _oi_cache.save_snapshot(ticker, target, data)

        if v4.get("error") or v4.get("iv") is None:
            return empty

        vix = _get_vix_data()

        return (
            v4["iv"], spot, target,
            v4["engine_result"], v4["walls"],
            v4["skew"], v4["pcr"], vix,
            v4,
        )

    except Exception as e:
        log.warning(f"0DTE IV fetch failed for {ticker}: {e}")
        return empty


def _get_chain_for_expiry(ticker: str, target_date_str: str) -> tuple:
    """Fetch chain for specific expiry. Cached."""
    try:
        cached = _chain_cache_get(ticker, target_date_str)
        if cached:
            return cached
        spot = get_spot(ticker)
        data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", {"expiration": target_date_str})
        if not isinstance(data, dict) or data.get("s") != "ok" or not data.get("optionSymbol"):
            return None, None, None
        _chain_cache_set(ticker, target_date_str, data, spot)
        return data, spot, target_date_str
    except Exception as e:
        log.warning(f"Chain fetch failed for {ticker} exp={target_date_str}: {e}")
        return None, None, None


def _get_chain_iv_for_expiry(ticker: str, target_date_str: str, dte: float) -> tuple:
    """Full v4 chain analysis for any expiry (monitor cards)."""
    empty = (None, None, None, None, {}, {}, {}, {}, {})
    try:
        data, spot, target = _get_chain_for_expiry(ticker, target_date_str)
        if data is None:
            return empty

        # ── OI change ──
        _oi_cache.apply_oi_changes_to_chain(ticker, target, data)

        bars = _get_ohlc_bars(ticker, days=65)
        adv, spread = _estimate_liquidity(ticker, spot)

        v4 = run_institutional_snapshot(
            chain_data=data, spot=spot, dte=max(dte, 0.5),
            recent_bars=bars, is_0dte=(dte <= 1),
            avg_daily_dollar_volume=adv,
            bid_ask_spread_pct=spread,
            liquid_index=_is_liquid(ticker),
        )

        # ── Save OI snapshot ──
        _oi_cache.save_snapshot(ticker, target, data)

        if v4.get("error") or v4.get("iv") is None:
            return empty

        vix = _get_vix_data()
        return (v4["iv"], spot, target, v4["engine_result"], v4["walls"],
                v4["skew"], v4["pcr"], vix, v4)
    except Exception as e:
        log.warning(f"Chain IV fetch failed for {ticker} exp={target_date_str}: {e}")
        return empty


def _estimate_liquidity(ticker: str, spot: float) -> tuple:
    """
    Estimate ADV and bid-ask spread from stock candles + quote.
    Returns (avg_daily_dollar_volume, bid_ask_spread_pct) or (None, None).
    """
    adv = None
    spread = None
    try:
        # ADV: average of last 20 days of (close * volume)
        candles = get_daily_candles(ticker, days=25)
        if candles and len(candles) >= 5:
            # candles is a list of close prices from get_daily_candles
            # We need volume too — fetch raw candles
            from_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            raw = md_get(f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",
                        {"from": from_date})
            if raw and raw.get("s") == "ok":
                closes = raw.get("c") or []
                volumes = raw.get("v") or []
                n = min(len(closes), len(volumes))
                if n >= 5:
                    dvols = []
                    for i in range(max(n - 20, 0), n):
                        c = as_float(closes[i], 0)
                        v = as_float(volumes[i], 0)
                        if c > 0 and v > 0:
                            dvols.append(c * v)
                    if dvols:
                        adv = sum(dvols) / len(dvols)
    except Exception as e:
        log.debug(f"ADV estimate failed for {ticker}: {e}")

    try:
        # Spread from stock quote
        stock_data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")
        if stock_data and stock_data.get("s") == "ok":
            bid = as_float((stock_data.get("bid") or [None])[0], 0)
            ask = as_float((stock_data.get("ask") or [None])[0], 0)
            if bid > 0 and ask > 0 and ask > bid:
                spread = (ask - bid) / ((ask + bid) / 2)
    except Exception as e:
        log.debug(f"Spread estimate failed for {ticker}: {e}")

    return adv, spread


# v4.2: Per-cycle OHLC warning dedup.
# api_cache.get_ohlc_bars logs a WARNING on every timeout, which produced
# 32 identical lines for MA in today's run. We gate to one warning per
# ticker per process lifetime using a simple set. The set stays small
# (one entry per unique ticker that has ever timed out) and never needs
# to be cleared — the underlying cache TTL handles re-fetching.
_ohlc_warned_tickers: set = set()


def _get_ohlc_bars(ticker: str, days: int = 65) -> list:
    """Fetch daily OHLC and convert to options_exposure.OHLC objects (cached).
    v4.2: Suppresses repeated timeout warnings for the same ticker (OHLC_WARN_ONCE_PER_CYCLE).
    """
    if OHLC_WARN_ONCE_PER_CYCLE and ticker.upper() in _ohlc_warned_tickers:
        # Warning already emitted for this ticker — fetch silently
        import logging as _logging
        _orig_level = logging.getLogger("api_cache").level
        logging.getLogger("api_cache").setLevel(_logging.ERROR)
        try:
            return _cached_md.get_ohlc_bars(ticker, days)
        finally:
            logging.getLogger("api_cache").setLevel(_orig_level)

    result = _cached_md.get_ohlc_bars(ticker, days)
    if not result and OHLC_WARN_ONCE_PER_CYCLE:
        _ohlc_warned_tickers.add(ticker.upper())
    return result


def _get_atm_skew(strikes, iv_list, sides, n, spot):
    call_ivs, put_ivs = [], []
    atm_range = spot * 0.02
    for i in range(n):
        strike = as_float(strikes[i], 0); iv = as_float(iv_list[i], 0)
        side = str(sides[i] or "").lower()
        if iv <= 0 or abs(strike - spot) > atm_range: continue
        if side == "call": call_ivs.append(iv)
        elif side == "put": put_ivs.append(iv)
    result = {}
    if call_ivs: result["call_iv"] = round(sum(call_ivs) / len(call_ivs) * 100, 1)
    if put_ivs: result["put_iv"] = round(sum(put_ivs) / len(put_ivs) * 100, 1)
    return result


def _calc_pcr(oi_list, vol_l, sides, n):
    call_oi = call_vol = put_oi = put_vol = 0
    for i in range(n):
        oi = int(as_float(oi_list[i], 0)); vol = int(as_float(vol_l[i], 0))
        side = str(sides[i] or "").lower()
        if side == "call": call_oi += oi; call_vol += vol
        elif side == "put": put_oi += oi; put_vol += vol
    return {
        "put_oi": put_oi, "call_oi": call_oi, "put_vol": put_vol, "call_vol": call_vol,
        "pcr_oi": round(put_oi / call_oi, 2) if call_oi > 0 else None,
        "pcr_vol": round(put_vol / call_vol, 2) if call_vol > 0 else None,
    }


def _get_vix_data():
    return _cached_md.get_vix_data(as_float_fn=as_float)


# ─────────────────────────────────────────────────────────
# _calc_bias — KEPT AS-IS (14-signal scoring system)
# This is better than the engine's 4-signal composite.
# The v4 engine feeds it the same engine_result/walls/skew/pcr/vix
# it always consumed, plus v4_result is available for confidence.
# ─────────────────────────────────────────────────────────

# NOTE: _calc_bias is preserved EXACTLY as in the original app.py.
# It is ~400 lines of the 14-signal scoring system.
# For brevity in this file, the function is imported or pasted from
# the original. The ONLY change is that callers now also pass v4_result
# to the card formatters, which _calc_bias doesn't need to know about.
#
# >>> PASTE YOUR EXISTING _calc_bias() FUNCTION HERE UNCHANGED <<<
# It takes: (spot, em, walls, skew, eng, pcr, vix) and returns the bias dict.
# The v4 integration does NOT modify this function at all.

from app_bias import _calc_bias  # ← or paste the full function inline


# ─────────────────────────────────────────────────────────
# DISPLAY HELPERS — same as original + v4 confidence header
# ─────────────────────────────────────────────────────────

def _format_em_block(em, spot, bias_score, label=""):
    header = f"📐 EXPECTED MOVE{(' ' + label) if label else ''}"
    bull_1 = em["bull_1sd"]; bear_1 = em["bear_1sd"]
    bull_2 = em["bull_2sd"]; bear_2 = em["bear_2sd"]
    em_1 = em["em_1sd"]; em_2 = em["em_2sd"]; em_pct = em["em_pct_1sd"]
    line_1sd = f"  1σ (68%):  ${bear_1:.2f}  ←  ${spot:.2f}  →  ${bull_1:.2f}"
    # Bias score informs lean but NOT the range — range is pure IV math
    if bias_score >= 4:
        lean = f"  📈 Lean: BULLISH  (score {bias_score:+d}/14)"
    elif bias_score >= 2:
        lean = f"  📈 Lean: slight bullish  (score {bias_score:+d}/14)"
    elif bias_score <= -4:
        lean = f"  📉 Lean: BEARISH  (score {bias_score:+d}/14)"
    elif bias_score <= -2:
        lean = f"  📉 Lean: slight bearish  (score {bias_score:+d}/14)"
    else:
        lean = f"  ↔️  No clear lean  (score {bias_score:+d}/14)"
    return ["", "─" * 32, header, line_1sd, f"             ±${em_1:.2f}  ({em_pct:.2f}%)",
            f"  2σ (95%):  ${bear_2:.2f}  ←→  ${bull_2:.2f}", f"             ±${em_2:.2f}", lean]


def _format_skew_line(skew):
    if not skew or "call_iv" not in skew or "put_iv" not in skew: return ""
    c = skew["call_iv"]; p = skew["put_iv"]; diff = abs(p - c)
    if diff < 1.0: reading = "balanced"
    elif p > c: reading = f"puts pricier by {diff:.1f}% — {'aggressive' if diff >= 5 else 'mild'} fear"
    else: reading = f"calls pricier by {diff:.1f}% — {'aggressive' if diff >= 5 else 'mild'} greed"
    return f"  IV Skew: Calls {c}% / Puts {p}% — {reading}"


def _calc_intraday_em(spot, iv, hours_remaining):
    if iv <= 0 or hours_remaining <= 0: return {}
    hours_in_year = 252 * 6.5
    em_1sd = round(spot * iv * math.sqrt(hours_remaining / hours_in_year), 2)
    em_2sd = round(em_1sd * 2, 2)
    return {
        "em_1sd": em_1sd, "em_2sd": em_2sd,
        "bull_1sd": round(spot + em_1sd, 2), "bear_1sd": round(spot - em_1sd, 2),
        "bull_2sd": round(spot + em_2sd, 2), "bear_2sd": round(spot - em_2sd, 2),
        "em_pct_1sd": round((em_1sd / spot) * 100, 2), "hours_used": round(hours_remaining, 2),
    }


# ─────────────────────────────────────────────────────────
# LIQUID SYMBOL DETECTION
# ─────────────────────────────────────────────────────────

LIQUID_SYMBOLS = {
    # Major indices
    "SPY", "QQQ", "SPX", "NDX", "IWM", "DIA",
    # Mega-cap tech
    "AAPL", "NVDA", "TSLA", "AMZN", "META", "MSFT", "GOOGL", "GOOG",
    # High-volume single names
    "AMD", "NFLX", "COIN", "MSTR", "ARM", "PLTR", "SMCI", "INTC", "MU", "SOFI",
    # Commodity & sector ETFs
    "GLD", "SLV", "GDX", "USO", "XLE", "XLF", "XLK", "XLV", "XLI", "XLP",
    # Bond & international ETFs
    "TLT", "HYG", "EEM", "EFA", "FXI",
    # Volatility
    "VXX", "UVXY", "SQQQ", "TQQQ",
}

def _is_liquid(ticker): return ticker.upper() in LIQUID_SYMBOLS


# ─────────────────────────────────────────────────────────
# EXPIRY SELECTION
# ─────────────────────────────────────────────────────────

def _find_expiry_nearest(ticker):
    try:
        from datetime import date as _date
        today = _date.today(); today_str = today.strftime("%Y-%m-%d")
        exps = get_expirations(ticker)
        future = [e for e in exps if e >= today_str]
        if not future: return None, None
        exp = future[0]; dte = (_date.fromisoformat(exp) - today).days
        return exp, dte
    except Exception as e:
        log.warning(f"_find_expiry_nearest failed for {ticker}: {e}")
        return None, None




def _find_expiry_closest_to_21(ticker):
    """
    Monitor-long selection for a ~21-day thesis, but with extra time buffer.
    Prefer 28–35 DTE when available so trades and wheel levels have grace time.
    Fall back to the nearest >=21 DTE expiry if the buffered band is unavailable.
    """
    try:
        from datetime import date as _date
        today = _date.today(); today_str = today.strftime("%Y-%m-%d")
        exps = get_expirations(ticker)
        buffered = []
        fallback = []
        for e in exps:
            dte = (_date.fromisoformat(e) - today).days
            if e < today_str or dte < 15:
                continue
            row = (abs(dte - 31), dte, e)
            if 28 <= dte <= 35:
                buffered.append(row)
            if dte >= 21:
                fallback.append((abs(dte - 28), dte, e))
        if buffered:
            buffered.sort(); _, dte, exp = buffered[0]
            return exp, dte
        if fallback:
            fallback.sort(); _, dte, exp = fallback[0]
            return exp, dte
        return None, None
    except Exception as e:
        log.warning(f"_find_expiry_closest_to_21 failed for {ticker}: {e}")
        return None, None


# ─────────────────────────────────────────────────────────────────────
# EM ACCURACY LOGGER — saves predictions for backtest analysis
# Stores each EM card's prediction in Redis so we can compare
# against actual EOD prices and compute hit rates over time.
# ─────────────────────────────────────────────────────────────────────

def _log_em_prediction(ticker: str, session: str, spot: float, em: dict, bias: dict, v4_result: dict, walls: dict = None, eng: dict = None, cagf: dict = None):
    """
    Log EM prediction to Redis and to an auto-tracked dataset for later tuning.
    Key: em_log:{date}:{ticker}:{session}
    Stored as JSON with prediction data. TTL: 90 days.
    """
    try:
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.strftime("%Y-%m-%d")
        key = f"em_log:{now_str}:{ticker}:{session}"
        walls = walls or {}
        eng = eng or {}
        max_pain = walls.get("max_pain")
        if max_pain is None:
            max_pain = eng.get("max_pain")
        entry = {
            "ticker": ticker,
            "date": now_str,
            "session": session,
            "logged_at_utc": now_dt.isoformat(),
            "spot_at_prediction": spot,
            "em_1sd": em.get("em_1sd"),
            "bull_1sd": em.get("bull_1sd"),
            "bear_1sd": em.get("bear_1sd"),
            "bull_2sd": em.get("bull_2sd"),
            "bear_2sd": em.get("bear_2sd"),
            "bias_score": bias.get("score"),
            "bias_direction": bias.get("direction"),
            "v4_confidence": v4_result.get("confidence", {}).get("label") if v4_result else None,
            "v4_confidence_composite": v4_result.get("confidence", {}).get("composite") if v4_result else None,
            "v4_bias": v4_result.get("snapshot", {}).get("adjusted_expectation", {}).get("bias") if v4_result else None,
            "v4_regime": v4_result.get("snapshot", {}).get("regime", {}).get("regime") if v4_result else None,
            "gamma_flip": walls.get("gamma_flip") or eng.get("flip_price"),
            "call_wall": walls.get("call_wall"),
            "put_wall": walls.get("put_wall"),
            "gamma_wall": walls.get("gamma_wall"),
            "max_pain": max_pain,
            "pin_zone_low": walls.get("put_wall"),
            "pin_zone_high": walls.get("call_wall"),
            "cagf_direction": (cagf or {}).get("direction"),
            "cagf_regime": (cagf or {}).get("regime"),
            "trend_day_probability": (cagf or {}).get("trend_day_probability"),
            "eod_price": None,
            "reconciled": False,
        }
        store_set(key, json.dumps(entry), ttl=90 * 86400)  # 90 day TTL

        fieldnames = [
            "logged_at_utc","date","session","ticker","spot_at_prediction","em_1sd","bull_1sd","bear_1sd",
            "bull_2sd","bear_2sd","bias_score","bias_direction","v4_confidence","v4_confidence_composite",
            "v4_bias","v4_regime","gamma_flip","call_wall","put_wall","gamma_wall","max_pain",
            "pin_zone_low","pin_zone_high","cagf_direction","cagf_regime","trend_day_probability"
        ]
        _append_csv_row("em_predictions.csv", fieldnames, entry)
        _append_jsonl("em_predictions.jsonl", entry)
        log.debug(f"EM prediction logged: {key}")
    except Exception as e:
        log.warning(f"EM prediction log failed: {e}")


# ─────────────────────────────────────────────────────────────────────
# _post_em_card — v4 INTEGRATED
# Now shows confidence, downgrades, trade sign, vol regime from v4 engine.
# _calc_bias 14-signal scoring preserved exactly.
# ─────────────────────────────────────────────────────────────────────

def _post_em_card(ticker: str, session: str):
    try:
        import pytz
        ct = pytz.timezone("America/Chicago"); now_ct = datetime.now(ct); today_dt = now_ct.date()
        is_afternoon = (session == "afternoon")

        # v4.2: Auto-detect after-hours — if market is closed and session
        # is not explicitly "afternoon", switch to next-day preview mode.
        # Market hours: 8:30 AM – 3:00 PM CT
        market_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
        market_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
        is_market_closed = now_ct > market_close_ct or now_ct < market_open_ct

        if not is_afternoon and is_market_closed:
            log.info(f"EM card: {session} session at {now_ct.strftime('%I:%M %p CT')} — "
                     f"market closed, auto-switching to next-day preview")
            is_afternoon = True  # treat as afternoon/next-day

        if is_afternoon:
            target_date_str = _get_next_trading_day(today_dt)
            hours_for_em = 6.5; session_emoji = "🌆"; session_label = "Next Day Preview"
            horizon_note = f"Full session EM for {target_date_str}"
        else:
            target_date_str = today_dt.strftime("%Y-%m-%d")
            hours_for_em = max((market_close_ct - now_ct).total_seconds() / 3600, 0.25)
            session_emoji = "🌅"; session_label = "Today (Pre-Open)"
            horizon_note = f"{hours_for_em:.1f}h remaining today"

        # v4: 9-element tuple now includes v4_result
        result_tuple = _get_0dte_iv(ticker, target_date_str)
        iv, spot, expiration = result_tuple[0], result_tuple[1], result_tuple[2]
        eng, walls, skew, pcr, vix = result_tuple[3], result_tuple[4], result_tuple[5], result_tuple[6], result_tuple[7]
        v4_result = result_tuple[8] if len(result_tuple) > 8 else {}

        if iv is None or spot is None:
            log.warning(f"EM card skipped for {ticker}: IV unavailable")
            post_to_telegram(f"⚠️ {ticker} EM: could not fetch IV for {target_date_str}")
            return

        em = _calc_intraday_em(spot, iv, hours_for_em)
        if not em: return

        bias = _calc_bias(spot, em, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        iv_pct = iv * 100
        if iv_pct < 10: iv_emoji = "🟢"; iv_note = "Low IV — tight range."
        elif iv_pct < 20: iv_emoji = "🟡"; iv_note = "Moderate IV — EM ranges reliable."
        elif iv_pct < 35: iv_emoji = "🔴"; iv_note = "Elevated IV — respect stops."
        else: iv_emoji = "🚨"; iv_note = "EXTREME IV — EM may understate. Minimum size."

        # ══════ HEADER ══════
        lines = [
            f"{session_emoji} {ticker} — Institutional EM Brief ({session_label})",
            f"Spot: ${spot:.2f}  |  IV: {iv_emoji} {iv_pct:.1f}%  |  Exp: {expiration}",
            f"Session: {horizon_note}",
        ]

        # ── v4.3: Confidence (single line, no duplicate) ──
        if v4_result:
            conf = v4_result.get("confidence", {})
            dg = v4_result.get("downgrades", [])
            conf_label_str = conf.get("label", "?")
            conf_score = conf.get("composite", 0)
            conf_line = f"Confidence: {conf_label_str} ({conf_score:.0%})"
            if dg:
                short_dg = [d.split(":")[0] for d in dg[:2]]
                conf_line += " | Data note: " + ", ".join(short_dg)
            lines.append(conf_line)

        if vix and vix.get("vix"):
            v = vix["vix"]; v9d = vix.get("vix9d"); term = vix.get("term", "")
            term_str = {"inverted": " 🚨 INVERTED", "normal": " ✅ normal", "flat": " flat"}.get(term, "")
            v9d_str = f"  VIX9D: {v9d}{term_str}" if v9d else ""
            lines.append(f"VIX: {v}{v9d_str}")

        # ── v4: Vol regime from engine ──
        if v4_result:
            vr_line = format_vol_regime_line(v4_result)
            if vr_line:
                lines.append(f"Vol: {vr_line}")

        # ══════ EXPECTED MOVE ══════
        lines += _format_em_block(em, spot, bias["score"])

        # ══════ DEALER FLOW ══════
        if eng:
            tgex = eng.get("gex", 0); dex = eng.get("dex", 0)
            vanna_m = eng.get("vanna", 0); charm_m = eng.get("charm", 0)
            flip = eng.get("flip_price"); regime = eng.get("regime", {})

            gex_icon = "🧲" if tgex >= 0 else "⚡"
            gex_sign = f"+${tgex:.1f}M" if tgex >= 0 else f"-${abs(tgex):.1f}M"
            gex_mode = "SUPPRESSING moves" if tgex >= 0 else "AMPLIFYING moves"
            dex_note = "dealers LONG → SELL on drops" if dex >= 0 else "dealers SHORT → BUY on rallies"
            vanna_note = "IV spike → dealer BUY" if vanna_m >= 0 else "IV spike → dealer SELL"
            charm_note = "removes sell hedges (bullish)" if charm_m >= 0 else "adds sell hedges (bearish)"

            lines += ["", "─" * 32, "⚙️ DEALER FLOW",
                f"  {gex_icon} GEX:   {gex_sign}  —  {gex_mode}",
                f"  📍 Flip:  ${flip:.2f}  ({'above' if spot > flip else 'BELOW'})" if flip else "  📍 Flip:  not found",
                f"  📊 DEX:   {'+'if dex>=0 else ''}{dex:.1f}M  —  {dex_note}",
                f"  🌊 Vanna: {'+'if vanna_m>=0 else ''}{vanna_m:.1f}M  —  {vanna_note}",
                f"  ⏱️ Charm: {'+'if charm_m>=0 else ''}{charm_m:.1f}M  —  {charm_note}",
            ]
            if regime.get("preferred"): lines.append(f"  ✅ Favors: {regime['preferred']}")
            if regime.get("avoid"): lines.append(f"  ❌ Avoid:  {regime['avoid']}")

        # ── v4: Trade sign ──
        if v4_result:
            ts_line = format_trade_sign_line(v4_result)
            if ts_line:
                lines.append(f"  🔍 {ts_line}")

        # ══════ INSTITUTIONAL FLOW MODEL (CAGF) — SPY/QQQ ══════
        cagf = None
        dte_rec = None
        if eng and ticker.upper() in ("SPY", "QQQ", "SPX"):
            # Compute CAGF from dealer flows
            import pytz as _pytz
            _ct = _pytz.timezone("America/Chicago")
            _now_ct = datetime.now(_ct)
            _mkt_open = _now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
            _mkt_close = _now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
            _session_secs = (_mkt_close - _mkt_open).total_seconds()
            _elapsed = max(0, (_now_ct - _mkt_open).total_seconds())
            _sess_progress = min(1.0, _elapsed / _session_secs) if _session_secs > 0 else 0.5

            # Get RV from v4 result
            _rv = 0
            if v4_result and v4_result.get("vol_regime"):
                _rv = v4_result["vol_regime"].get("realized_vol_20d", 0) or 0

            _vix_val = vix.get("vix", 20) if vix else 20

            # Get ADV for normalization
            _adv, _ = _estimate_liquidity(ticker, spot)

            # Get candle closes for momentum component
            _closes = get_daily_candles(ticker, days=60)

            cagf = compute_cagf(
                dealer_flows={
                    "gex": eng.get("gex", 0),
                    "dex": eng.get("dex", 0),
                    "vanna": eng.get("vanna", 0),
                    "charm": eng.get("charm", 0),
                    "gamma_flip": eng.get("flip_price"),
                },
                iv=iv,
                rv=_rv,
                spot=spot,
                vix=_vix_val,
                session_progress=_sess_progress,
                adv=_adv,
                candle_closes=_closes,
                ticker=ticker,
            )
            lines += format_cagf_block(cagf)

            # DTE recommendation
            dte_rec = recommend_dte(
                cagf=cagf,
                iv=iv,
                vix=_vix_val,
                session_progress=_sess_progress,
            )
            lines += format_dte_block(dte_rec)

            log.info(f"CAGF {ticker}: edge={cagf['edge']:+.2f} dir={cagf['direction']} "
                     f"trend={cagf['trend_day_probability']:.0%} regime={cagf['regime']} "
                     f"dte_rec={dte_rec['primary']['label']}")

        # ══════ KEY LEVELS ══════
        if walls:
            lines += ["", "─" * 32, "🧱 KEY LEVELS"]
            if "call_wall" in walls:
                cw = walls["call_wall"]; cw_oi = walls["call_wall_oi"]
                dist = ((cw - spot) / spot) * 100
                cw_tag = "⚠️ INSIDE 1σ" if cw <= em["bull_1sd"] else ("within 2σ" if cw <= em["bull_2sd"] else "outside 2σ")
                lines.append(f"  📵 Resistance: ${cw:.0f}  ({cw_oi:,} OI, +{dist:.1f}%) — {cw_tag}")
            if "put_wall" in walls:
                pw = walls["put_wall"]; pw_oi = walls["put_wall_oi"]
                dist = ((spot - pw) / spot) * 100
                pw_tag = "⚠️ INSIDE 1σ" if pw >= em["bear_1sd"] else ("within 2σ" if pw >= em["bear_2sd"] else "outside 2σ")
                lines.append(f"  🛡️ Support:    ${pw:.0f}  ({pw_oi:,} OI, -{dist:.1f}%) — {pw_tag}")
            if "gamma_wall" in walls:
                gw = walls["gamma_wall"]; gw_dist = ((gw - spot) / spot) * 100
                lines.append(f"  🎯 Gamma Wall: ${gw:.0f}  ({gw_dist:+.1f}%)")
            if "call_top3" in walls:
                lines.append(f"  📵 Resistance stack: {'  →  '.join(f'${x:.0f}' for x in walls['call_top3'])}")
            if "put_top3" in walls:
                lines.append(f"  🛡️ Support stack:    {'  →  '.join(f'${x:.0f}' for x in walls['put_top3'])}")
            if ("call_wall" in walls and "put_wall" in walls and
                walls["call_wall"] <= em["bull_1sd"] and walls["put_wall"] >= em["bear_1sd"]):
                pin_w = walls["call_wall"] - walls["put_wall"]
                lines.append(f"  📌 PIN ZONE: ${walls['put_wall']:.0f} – ${walls['call_wall']:.0f}  (${pin_w:.0f} wide)")

        # ══════ SENTIMENT ══════
        skew_str = _format_skew_line(skew)
        pcr_oi_str = f"OI {pcr['pcr_oi']:.2f}" if pcr and pcr.get("pcr_oi") is not None else "OI n/a"
        pcr_vol_str = f"Vol {pcr['pcr_vol']:.2f}" if pcr and pcr.get("pcr_vol") is not None else "Vol n/a"
        lines += ["", "─" * 32, "📊 SENTIMENT", f"  PCR:  {pcr_oi_str}  |  {pcr_vol_str}"]
        if skew_str: lines.append(skew_str)

        # ══════ DIRECTIONAL LEAN ══════
        dir_emoji = {"STRONG BULLISH": "🟢🟢", "BULLISH": "🟢", "SLIGHT BULLISH": "🟡",
                     "NEUTRAL": "⚪", "SLIGHT BEARISH": "🟠", "BEARISH": "🔴", "STRONG BEARISH": "🔴🔴"
                    }.get(bias["direction"], "⚪")
        strength_str = f"  [{bias['strength']}]" if bias.get("strength") else ""
        score_str = f"{bias['score']:+d}/{bias['max_score']}"
        na_count = bias.get("na_count", 0)
        dot_bar = ("▲" * bias["up_count"]) + ("▼" * bias["down_count"]) + ("◆" * bias["neu_count"]) + ("—" * na_count)

        lines += ["", "═" * 32, f"{dir_emoji}  BIAS MODEL: {bias['direction']}{strength_str}",
            f"Score: {score_str}  |  {dot_bar}",
            f"  ▲ {bias['up_count']} bullish  ·  ▼ {bias['down_count']} bearish  ·  ◆ {bias['neu_count']} neutral" + (f"  ·  — {na_count} n/a" if na_count else ""),
            "", f"📋 {bias['verdict']}", "", "── Signal Breakdown ({}/14) ──".format(bias['n_signals'])]
        for arrow, text in bias["signals"]:
            lines.append(f"  {arrow}  {text}")
        lines += ["═" * 32, "", f"💡 {iv_note}", "— Not financial advice —"]

        log.info(f"EM snapshot built: {ticker} | {session_label} | spot={spot} | IV={iv_pct:.1f}% | "
                 f"EM=±${em['em_1sd']} | score={bias['score']} | lean={bias['direction']} | "
                 f"conf={v4_result.get('confidence', {}).get('label', '?')}")

        # ── EM accuracy logger: save prediction for backtest ──
        _log_em_prediction(ticker, session, spot, em, bias, v4_result, walls=walls, eng=eng, cagf=cagf)

        # ── v4.3: Resolve unified regime for trade card + plain cards ──
        unified_regime = resolve_unified_regime(eng or {}, cagf, spot)

        # Live /em output should include the richer dealer brief when no trade qualifies.

        # ── Compute session progress for trade card timing gates ──
        _mkt_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
        _mkt_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
        _session_secs = (_mkt_close_ct - _mkt_open_ct).total_seconds()
        _elapsed_secs = max(0, (now_ct - _mkt_open_ct).total_seconds())
        _sess_progress = min(1.0, _elapsed_secs / _session_secs) if _session_secs > 0 else 0.5

        _post_trade_card(
            ticker=ticker, spot=spot, expiration=expiration,
            eng=eng or {}, walls=walls or {}, bias=bias, em=em,
            vix=vix or {}, pcr=pcr or {}, is_0dte=(not is_afternoon), v4_result=v4_result,
            cagf=cagf, dte_rec=dte_rec,
            now_ct=now_ct, session_progress=_sess_progress,
            is_next_day=is_afternoon, unified_regime=unified_regime,
        )

    except Exception as e:
        log.error(f"EM card error for {ticker} ({session}): {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────
# _post_trade_card — v4 INTEGRATED
# Added v4_result parameter for confidence gating.
# ─────────────────────────────────────────────────────────────────────

def _post_trade_card(ticker, spot, expiration, eng, walls, bias, em, vix, pcr,
                     is_0dte=True, v4_result=None, cagf=None, dte_rec=None,
                     now_ct=None, session_progress=None, is_next_day=False,
                     unified_regime=None):
    """
    Trade recommendation card — v4.2.
    CAGF-integrated: respects DTE recommendation, session timing, and EM-aware strike placement.

    v4.2 fixes:
      - Timestamp uses CT timezone (not server-local/UTC)
      - Session gate: blocks 0DTE when <15 min remain or market closed
      - DTE recommendation enforced: if CAGF says avoid 0DTE, card uses recommended DTE
      - Afternoon session → next-day trade card (not 0DTE)
      - Strikes constrained to EM 1σ boundary (short strike inside expected move)
      - Width scaled relative to EM range (no wider than 1σ move)
    """
    try:
        direction = bias.get("direction", "NEUTRAL")
        score = bias.get("score", 0)
        is_bull = "BULL" in direction
        tgex = eng.get("gex", 0) if eng else 0
        dex = eng.get("dex", 0) if eng else 0
        flip = eng.get("flip_price") if eng else None
        charm_m = eng.get("charm", 0) if eng else 0
        neg_gex = tgex < 0
        v = vix.get("vix", 20) if vix else 20
        exp_short = expiration[5:] if expiration else "?"

        # ── CT timestamp (v4.2 fix) ──
        if now_ct is None:
            try:
                import pytz
                now_ct = datetime.now(pytz.timezone("America/Chicago"))
            except Exception:
                now_ct = datetime.now()
        time_str = now_ct.strftime('%I:%M %p CT')

        # ── Determine effective DTE from CAGF recommendation ──
        # Start with the raw is_0dte flag, then let CAGF override
        effective_dte_label = "0DTE" if is_0dte else "NEXT DAY"
        effective_dte = 0 if is_0dte else 1
        dte_was_upgraded = False
        dte_upgrade_reason = ""

        # v4.2: CAGF DTE enforcement — if CAGF says avoid 0DTE, use recommended DTE
        if dte_rec and dte_rec.get("primary"):
            rec_dte = dte_rec["primary"].get("dte", 0)
            rec_label = dte_rec["primary"].get("label", "0DTE")
            avoid_list = dte_rec.get("avoid", [])

            # Check if 0DTE is in the avoid list
            dte_0_avoided = any("0DTE" in a for a in avoid_list)
            dte_1_avoided = any("1DTE" in a for a in avoid_list)

            if is_0dte and dte_0_avoided:
                # CAGF says no 0DTE — upgrade to recommended DTE
                effective_dte = rec_dte
                effective_dte_label = rec_label
                dte_was_upgraded = True
                dte_upgrade_reason = avoid_list[0] if avoid_list else "0DTE conditions not met"
                log.info(f"Trade card DTE upgraded: 0DTE → {rec_label} | {dte_upgrade_reason}")

        # v4.2: Next-day session → always at least 1 DTE, prefer CAGF recommendation
        if is_next_day:
            if dte_rec and dte_rec.get("primary"):
                rec_dte = dte_rec["primary"].get("dte", 1)
                effective_dte = max(rec_dte, 1)
                effective_dte_label = dte_rec["primary"]["label"]
            else:
                effective_dte = max(effective_dte, 1)
                effective_dte_label = f"{effective_dte} DTE"
            dte_was_upgraded = True
            dte_upgrade_reason = "afternoon session → next trading day"

        # ── Session timing gate (v4.2 fix) ──
        # Block 0DTE trades when market is closed or <15 min remain
        if effective_dte == 0 and session_progress is not None:
            mkt_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
            mkt_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
            is_market_hours = mkt_open_ct <= now_ct <= mkt_close_ct
            minutes_remaining = max(0, (mkt_close_ct - now_ct).total_seconds() / 60)

            if not is_market_hours:
                # Outside market hours — upgrade to next-day DTE
                if dte_rec and dte_rec.get("primary"):
                    effective_dte = max(dte_rec["primary"].get("dte", 1), 1)
                    effective_dte_label = dte_rec["primary"].get("label", "1 DTE")
                else:
                    effective_dte = 1
                    effective_dte_label = "1 DTE"
                dte_was_upgraded = True
                dte_upgrade_reason = f"market closed ({time_str})"
                log.info(f"Trade card: 0DTE blocked — market closed at {time_str}")

            elif minutes_remaining < 15:
                # Less than 15 minutes left — too late for 0DTE
                if dte_rec and dte_rec.get("primary"):
                    effective_dte = max(dte_rec["primary"].get("dte", 1), 1)
                    effective_dte_label = dte_rec["primary"].get("label", "1 DTE")
                else:
                    effective_dte = 1
                    effective_dte_label = "1 DTE"
                dte_was_upgraded = True
                dte_upgrade_reason = f"<15 min to close ({minutes_remaining:.0f} min left)"
                log.info(f"Trade card: 0DTE blocked — only {minutes_remaining:.0f} min remaining")

        # ── Build card title based on effective DTE ──
        if effective_dte == 0:
            card_title = "0DTE TRADE SETUP"
        else:
            card_title = f"TRADE SETUP ({effective_dte_label})"

        def no_trade(reason, emoji="⚪"):
            flip = (walls or {}).get("gamma_flip") or (eng or {}).get("flip_price")
            call_wall = (walls or {}).get("call_wall")
            put_wall = (walls or {}).get("put_wall")
            gamma_wall = (walls or {}).get("gamma_wall")
            max_pain = (walls or {}).get("max_pain") or (eng or {}).get("max_pain")
            bias_line = f"{bias['direction']} (score {bias['score']}/14)"
            range_low = em.get('bear_1sd')
            range_high = em.get('bull_1sd')
            accel_up = call_wall or range_high
            accel_dn = put_wall or range_low
            lines = [
                f"🎯 {ticker} — DEALER EM BRIEF ({effective_dte_label})  |  Exp: {exp_short}",
                f"{emoji} NO TRADE — {reason}",
                f"Bias: {bias_line}",
                f"Spot: ${spot:.2f} | 1σ: ${range_low} – ${range_high}",
            ]
            if flip is not None:
                lines.append(f"Gamma Flip: ${flip}")
            if call_wall is not None or put_wall is not None:
                lines.append(f"Call Wall: ${call_wall} | Put Wall: ${put_wall}")
            if gamma_wall is not None:
                lines.append(f"Gamma Wall: ${gamma_wall}")
            if max_pain is not None:
                lines.append(f"Max Pain: ${max_pain}")
            lines += [
                f"Trigger Up: above ${accel_up}",
                f"Trigger Down: below ${accel_dn}",
                f"Regime: {unified_regime}",
                "— Not financial advice —",
            ]
            post_to_telegram("\n".join(lines))
            log.info(f"Trade card blocked: {ticker} | {reason}")

        # ══════ GATE CHECKS ══════

        # G0 — v4 confidence gate: LOW data quality = no trade
        if v4_result and v4_result.get("confidence", {}).get("label") == "LOW":
            conf_score = v4_result["confidence"].get("composite", 0)
            dg = v4_result.get("downgrades", [])
            dg_str = "; ".join(d.split(":")[0] for d in dg[:2]) if dg else "insufficient data"
            no_trade(f"Data quality LOW ({conf_score:.0%}) — {dg_str}", "⚠️")
            return

        # G1 — No directional edge
        if direction == "NEUTRAL":
            no_trade(f"Bias NEUTRAL (score {score:+d}/14). No directional edge.", "⚪")
            return

        # G2 — Edge too thin
        if direction in ("SLIGHT BULLISH", "SLIGHT BEARISH") and abs(score) < 2:
            no_trade(f"Lean {direction} but score only {score:+d}/14 — edge too thin.", "🟡")
            return

        # G3 — Flip mismatch
        if flip:
            flip_wrong = (is_bull and spot < flip) or (not is_bull and spot > flip)
            if flip_wrong:
                side_word = "BELOW" if is_bull else "ABOVE"
                no_trade(f"Spot ${spot:.2f} is {side_word} gamma flip ${flip:.2f}.", "🔴")
                return

        # G4 — VIX extreme
        if v >= 40:
            no_trade(f"VIX {v} extreme — stand aside.", "🚨")
            return

        # G5 — Positive GEX + weak score
        if not neg_gex and abs(score) < 5:
            no_trade(f"GEX positive (+${tgex:.1f}M), score only {score:+d}/14 — not enough edge.", "🧲")
            return

        # ── v4.3: Resolve unified regime if not passed from caller ──
        if unified_regime is None:
            unified_regime = resolve_unified_regime(eng, cagf, spot)

        # G6 — v4.3 REGIME GATE: strategy must fit the environment
        gate_ok, gate_reason = regime_gate(
            regime=unified_regime, bias=bias, cagf=cagf,
            v4_result=v4_result, dte_rec=dte_rec,
        )
        if not gate_ok:
            no_trade(f"Regime gate: {gate_reason}", "🚫")
            return

        # G7 — CAGF direction conflict (v4.2)
        # If CAGF has strong directional signal opposing the bias, flag it
        cagf_conflict = False
        cagf_conflict_note = ""
        if cagf and cagf.get("regime") != "UNKNOWN":
            cagf_prob = cagf.get("probability", 50)
            cagf_dir = cagf.get("direction", "NEUTRAL")
            # Bull bias but CAGF says < 40% upside = conflict
            if is_bull and cagf_prob < 40:
                cagf_conflict = True
                cagf_conflict_note = f"⚠️ CAGF conflict: bias BULL but flow {cagf_prob:.0f}% upside ({cagf_dir})"
            # Bear bias but CAGF says > 60% upside = conflict
            elif not is_bull and cagf_prob > 60:
                cagf_conflict = True
                cagf_conflict_note = f"⚠️ CAGF conflict: bias BEAR but flow {cagf_prob:.0f}% upside ({cagf_dir})"

        # ══════ EM-AWARE WIDTH (personalized ladder) ══════
        ticker_up = ticker.upper()
        step = 5 if ticker_up in ("SPX", "NDX") else 1

        em_1sd = em.get("em_1sd", 0) if em else 0
        if em_1sd > 0:
            max_em_width = int(em_1sd / step) * step
            max_em_width = max(max_em_width, step)
        else:
            max_em_width = 999

        # Brad's width ladder: never 0.50. Prefer $1 → $2.50 → $5 on shorter trades.
        preferred_widths = [1.0, 2.5, 5.0]
        if effective_dte >= 10:
            preferred_widths += [10.0, 20.0]
        available_widths = [w for w in preferred_widths if w >= step and w <= max_em_width + 1e-9]
        if not available_widths:
            width = float(step)
        else:
            width = float(available_widths[0])

        # ══════ EM-AWARE STRIKES (v4.2 fix) ══════
        bull_1sd = em.get("bull_1sd", spot + 999) if em else spot + 999
        bear_1sd = em.get("bear_1sd", spot - 999) if em else spot - 999

        if is_bull:
            long_strike = (int(spot) // step) * step
            if long_strike >= spot: long_strike -= step
            short_strike = long_strike + width

            # v4.2: Clamp short strike to EM 1σ upper boundary
            em_upper_strike = (int(bull_1sd) // step) * step
            if short_strike > em_upper_strike and em_1sd > 0:
                old_short = short_strike
                short_strike = em_upper_strike
                width = short_strike - long_strike
                if width < step:
                    # EM range is too tight for any spread at this step — use minimum
                    short_strike = long_strike + step
                    width = step
                log.info(f"Trade card: bull short strike clamped ${old_short} → ${short_strike} "
                         f"(EM 1σ upper = ${bull_1sd:.2f})")

            spread_type = "CALL DEBIT SPREAD"
            long_label = f"${long_strike:.0f}C"; short_label = f"${short_strike:.0f}C"
            stop_level = flip if flip else round(spot * (1 - 0.007), 2)
        else:
            long_strike = (int(spot) // step) * step
            if long_strike <= spot: long_strike += step
            short_strike = long_strike - width

            # v4.2: Clamp short strike to EM 1σ lower boundary
            em_lower_strike = int(math.ceil(bear_1sd / step)) * step
            if short_strike < em_lower_strike and em_1sd > 0:
                old_short = short_strike
                short_strike = em_lower_strike
                width = long_strike - short_strike
                if width < step:
                    short_strike = long_strike - step
                    width = step
                log.info(f"Trade card: bear short strike clamped ${old_short} → ${short_strike} "
                         f"(EM 1σ lower = ${bear_1sd:.2f})")

            spread_type = "PUT DEBIT SPREAD"
            long_label = f"${long_strike:.0f}P"; short_label = f"${short_strike:.0f}P"
            stop_level = flip if flip else round(spot * (1 + 0.007), 2)

        actual_width = abs(short_strike - long_strike)
        cost_est = round(actual_width * 0.60, 2)
        max_profit = round(actual_width - cost_est, 2)
        rr = round(max_profit / cost_est, 2) if cost_est > 0 else 0

        # ══════ SIZE ══════
        if v >= 28: base_pct = 25
        elif v >= 20: base_pct = 50
        elif v >= 15: base_pct = 75
        else: base_pct = 100
        dex_confirms = (is_bull and dex < -0.25) or (not is_bull and dex > 0.25)
        dex_disagrees = (is_bull and dex > 0.25) or (not is_bull and dex < -0.25)
        if dex_confirms: size_pct = min(base_pct + 25, 100); dex_note = "DEX confirms → +1 tier"
        elif dex_disagrees: size_pct = max(base_pct - 25, 25); dex_note = "DEX disagrees → -1 tier"
        else: size_pct = base_pct; dex_note = "DEX neutral"

        # v4.2: Reduce size if CAGF conflicts with bias direction
        if cagf_conflict:
            size_pct = max(int(size_pct * 0.5), 25)
            dex_note += " | CAGF conflict → halved"

        # v4.2: Reduce size in CHOP regime
        _tc_regime_label = (unified_regime.get("label") or "").upper() if unified_regime else ""
        if "CHOP" in _tc_regime_label:
            size_pct = max(int(size_pct * CHOP_REGIME_SIZE_MULT), 25)
            dex_note += f" | CHOP regime → ×{CHOP_REGIME_SIZE_MULT}"

        # ══════ TIMING ══════
        charm_tail = charm_m > 0
        if effective_dte == 0:
            timing_note = "Charm tailwind — hold into 2:30 PM CT." if charm_tail else "Charm headwind — do NOT hold into close."
            timing_warn = "Exit by 2:45 PM CT." if charm_tail else "⚠️ EXIT by noon CT."
            entry_cutoff = "⚠️ No new entries after 2:30 PM CT"
        else:
            # Multi-day trades: context-appropriate charm language
            if is_next_day:
                timing_note = ("Charm supportive — time decay favors upside drift during sessions."
                               if charm_tail else
                               "Charm headwind — time decay adds selling pressure during sessions.")
            else:
                timing_note = ("Charm tailwind — favorable into close."
                               if charm_tail else
                               "Charm headwind — consider early entry.")
            timing_warn = f"Target hold: {effective_dte} trading day{'s' if effective_dte > 1 else ''}."
            entry_cutoff = "📌 Enter at/near open for best fill" if is_next_day else "⚠️ No new entries after 2:30 PM CT"

        gex_note = f"⚡ Negative GEX (-${abs(tgex):.1f}M) — debit spreads confirmed." if neg_gex else f"🧲 Positive GEX (+${tgex:.1f}M) — width tightened."
        regime_label = unified_regime.get("label", "UNKNOWN") if unified_regime else "UNKNOWN"
        regime_desc = unified_regime.get("description", "") if unified_regime else ""

        dir_emoji = {"STRONG BULLISH": "🟢🟢", "BULLISH": "🟢", "SLIGHT BULLISH": "🟡",
                     "SLIGHT BEARISH": "🟠", "BEARISH": "🔴", "STRONG BEARISH": "🔴🔴"}.get(direction, "⚪")

        lines = [
            f"🎯 {ticker} — {card_title}",
            f"Generated: {time_str}  |  Exp: {exp_short}",
            f"Bias model: {dir_emoji} {direction}  [Score: {score:+d}/14]",
        ]

        # ── v4: Confidence line ──
        if v4_result:
            lines.append(format_confidence_header(v4_result))

        # ── v4.2: DTE upgrade notice ──
        if dte_was_upgraded:
            lines.append(f"📆 DTE: {effective_dte_label} (reason: {dte_upgrade_reason})")

        lines += [
            "━" * 32, "",
            f"⚙️ REGIME: {regime_label} ({unified_regime.get('source', 'unknown')})",
            f"  {regime_desc}",
            f"  GEX: {'negative' if neg_gex else 'positive'} ({tgex:+.1f}M)",
            "",
        ]

        # ── CAGF Institutional Edge (SPY/QQQ) ──
        if cagf and cagf.get("regime") != "UNKNOWN":
            prob = cagf.get("probability", 50)
            edge_emoji = "🟢" if prob >= 58 else "🔴" if prob <= 42 else "⚪"
            lines.append(f"🏛️ FLOW: {edge_emoji} {cagf['direction']}  ({prob:.0f}% upside prob)")
            lines.append(f"  Trend Day: {cagf['trend_day_probability']:.0%}  |  Vol: {cagf['vol_emoji']} {cagf['vol_label']}  |  {cagf['strategy_emoji']} {cagf['strategy']}")

        # ── DTE Recommendation ──
        if dte_rec and dte_rec.get("primary"):
            p = dte_rec["primary"]
            lines.append(f"📆 DTE: {p['emoji']} {p['label']} recommended  (score {p['score']})")
            if p.get("reasoning"):
                lines.append(f"  {p['reasoning']}")
            avoid_list = dte_rec.get("avoid", [])
            if avoid_list:
                lines.append(f"  ❌ {avoid_list[0]}")

        # ── v4.2: CAGF conflict warning ──
        if cagf_conflict:
            lines.append(f"  {cagf_conflict_note}")

        # ── EM zone check for short strike ──
        if em_1sd > 0:
            if is_bull:
                em_distance = bull_1sd - short_strike
                em_zone = "inside" if short_strike <= bull_1sd else "outside"
            else:
                em_distance = short_strike - bear_1sd
                em_zone = "inside" if short_strike >= bear_1sd else "outside"
            em_zone_emoji = "✅" if em_zone == "inside" else "⚠️"
            em_zone_note = f"  {em_zone_emoji} Short strike {em_zone} EM 1σ (${em_distance:+.2f} from boundary)"
        else:
            em_zone_note = ""

        lines += [
            "",
            f"📋 SETUP: ITM {spread_type}",
            f"  Buy:   {ticker} {long_label}", f"  Sell:  {ticker} {short_label}",
            f"  Width: ${actual_width:.2f}  |  Est. cost: ~${cost_est:.2f}/contract",
            f"  Max profit: ~${max_profit:.2f}/contract  |  R/R: ~{rr:.1f}:1",
        ]
        if em_zone_note:
            lines.append(em_zone_note)

        lines += [
            "",
            f"📍 LEVELS",
            f"  Entry zone:  ${spot:.2f} ± 0.3%",
            f"  Hard stop:   ${stop_level:.2f}", "",
            f"⏱️ TIMING", f"  {timing_note}", f"  {timing_warn}",
            f"  {entry_cutoff}", "",
            f"📊 SIZE",
            f"  VIX {v} → base: {base_pct}%  |  {dex_note}",
            f"  → FINAL: {size_pct}% of normal position size",
            "━" * 32, "— Not financial advice —",
        ]

        log.info(f"Trade setup built: {ticker} | {direction} | {effective_dte_label} | "
                 f"{spread_type} {long_label}/{short_label} | "
                 f"width=${actual_width} (EM 1σ=${em_1sd:.2f}) | "
                 f"size={size_pct}% | dte_upgraded={dte_was_upgraded} | "
                 f"regime={unified_regime.get('label', '?') if unified_regime else '?'} | "
                 f"conf={v4_result.get('confidence', {}).get('label', '?') if v4_result else 'N/A'}")

        # ── v4.3: Post a single decision-first card and cache it for /tradecard ──
        try:
            dc = format_decision_card(
                ticker=ticker, spot=spot, em=em, bias=bias,
                eng=eng, regime=unified_regime or {},
                cagf=cagf, dte_rec=dte_rec, v4_result=v4_result,
                spread_type=spread_type, long_label=long_label,
                short_label=short_label, stop_level=stop_level,
                effective_dte_label=effective_dte_label, size_pct=size_pct,
                walls=walls or {}, expiry_label=expiration, est_cost=cost_est,
            )
            post_to_telegram(dc)
            try:
                cache_key = f"tradecard:{ticker.upper()}"
                store_set(cache_key, dc, ttl=DIGEST_CARD_CACHE_TTL_SEC)
            except Exception:
                pass
        except Exception as e:
            log.warning(f"Decision card failed for {ticker}: {e}")

    except Exception as e:
        log.error(f"Trade card error for {ticker}: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────
# _post_monitor_card — v4 INTEGRATED
# ─────────────────────────────────────────────────────────────────────

def _post_monitor_card(ticker: str, mode: str):
    try:
        ticker = ticker.upper()
        if mode == "long":
            expiry, dte = _find_expiry_closest_to_21(ticker)
            mode_label = "Swing Outlook (~21-day thesis / buffered DTE)"
            mode_emoji = "📅"
        else:
            expiry, dte = _find_expiry_nearest(ticker)
            mode_label = "Near-Term Outlook"
            mode_emoji = "⚡"

        if not expiry:
            post_to_telegram(f"⚠️ {ticker}: no valid expiration for /monitor{mode}")
            return

        liquid = _is_liquid(ticker)
        result_tuple = _get_chain_iv_for_expiry(ticker, expiry, dte)
        iv, spot, expiration = result_tuple[0], result_tuple[1], result_tuple[2]
        eng, walls, skew, pcr, vix = result_tuple[3], result_tuple[4], result_tuple[5], result_tuple[6], result_tuple[7]
        v4_result = result_tuple[8] if len(result_tuple) > 8 else {}

        if not liquid:
            eng = {}

        if iv is None or spot is None:
            post_to_telegram(f"⚠️ {ticker}: could not fetch IV for {expiry} (DTE={dte})")
            return

        thesis_days = 21 if mode == "long" else max(dte, 1)
        em = _calc_intraday_em(spot, iv, thesis_days * 6.5)
        if not em:
            return
        bias = _calc_bias(spot, em or {}, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        iv_pct = iv * 100
        if iv_pct < 15:
            iv_emoji, iv_note = "🟢", "Low IV"
        elif iv_pct < 30:
            iv_emoji, iv_note = "🟡", "Moderate IV"
        elif iv_pct < 50:
            iv_emoji, iv_note = "🔴", "Elevated IV"
        else:
            iv_emoji, iv_note = "🚨", "Extreme IV"

        conf_label = v4_result.get("confidence", {}).get("label", "?") if v4_result else "?"
        conf_score = v4_result.get("confidence", {}).get("composite", 0) if v4_result else 0
        dg = v4_result.get("downgrades", []) if v4_result else []
        downgrade_note = ""
        if dg:
            downgrade_note = " | ⚠ " + "; ".join(d.split(":")[0] for d in dg[:2])

        direction = bias.get("direction", "NEUTRAL")
        simple_dir = "Bullish" if "BULL" in direction else "Bearish" if "BEAR" in direction else "Neutral"
        verdict = bias.get("verdict", "Monitoring only.")

        tgex = eng.get("gex", 0) if eng else 0
        dex = eng.get("dex", 0) if eng else 0
        vanna_m = eng.get("vanna", 0) if eng else 0
        charm_m = eng.get("charm", 0) if eng else 0
        flip = eng.get("flip_price") if eng else None
        call_wall = walls.get("call_wall") if walls else None
        put_wall = walls.get("put_wall") if walls else None
        gamma_wall = walls.get("gamma_wall") if walls else None

        reason_lines = []
        counter_lines = []
        dist_flip_pct = None
        if flip is not None and spot:
            dist_flip_pct = abs((flip - spot) / spot) * 100

        if simple_dir == "Bullish":
            if dex < -0.25:
                reason_lines.append("Dealers are short delta, so rallies can attract mechanical buying.")
            if charm_m > 0:
                reason_lines.append("Charm is supportive, which can help upside drift as time passes.")
            if put_wall is not None and put_wall < spot:
                reason_lines.append(f"Put wall near ${put_wall:.2f} can act as support on pullbacks.")
            if flip is not None:
                if spot >= flip:
                    reason_lines.append(f"Price is above gamma flip ${flip:.2f}; holding above it usually keeps bullish trades cleaner.")
                else:
                    counter_lines.append(f"Price is still below gamma flip ${flip:.2f}, so bullish follow-through needs more confirmation.")
            if dex > 0.25:
                counter_lines.append("Dealers are long delta, so upside can face hedging-related selling.")
            if charm_m < 0:
                counter_lines.append("Charm is a headwind, so time decay may add pressure.")
        elif simple_dir == "Bearish":
            if dex > 0.25:
                reason_lines.append("Dealers are long delta, so rallies can meet hedging-related selling pressure.")
            if charm_m < 0:
                reason_lines.append("Charm is a headwind, so time decay can add downside pressure.")
            if call_wall is not None and call_wall > spot:
                reason_lines.append(f"Call wall near ${call_wall:.2f} can act as resistance on bounces.")
            if flip is not None:
                if spot <= flip:
                    reason_lines.append(f"Price is below gamma flip ${flip:.2f}; staying below it usually keeps bearish trades cleaner.")
                else:
                    counter_lines.append(f"Price is above gamma flip ${flip:.2f}, so bearish follow-through needs more confirmation.")
            if dex < -0.25:
                counter_lines.append("Dealers are short delta, so sharp rallies can attract mechanical buying.")
            if charm_m > 0:
                counter_lines.append("Charm is supportive, which can help upside drift if price stabilizes.")
        else:
            if flip is not None:
                if spot >= flip:
                    reason_lines.append(f"Price is above gamma flip ${flip:.2f}; holding above it keeps upside conditions steadier.")
                else:
                    reason_lines.append(f"Price is below gamma flip ${flip:.2f}; staying below it can make moves more unstable.")
            if abs(dex) > 0.25:
                reason_lines.append("Dealer delta positioning is strong enough to matter on sharp moves.")
            if abs(charm_m) > 0:
                reason_lines.append("Charm flow is active, so time decay can still influence direction.")

        if not reason_lines:
            reason_lines.append(verdict)

        if flip is None:
            flip_line = "Gamma flip unavailable for this chain."
        else:
            if dist_flip_pct is not None and dist_flip_pct >= 2.5:
                if spot >= flip:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is above it now. It is fairly far from spot, so treat it more like a regime line than a tight trigger. A clean break back below can make bullish holds less reliable."
                else:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is below it now. It is fairly far from spot, so treat it more like a regime line than a tight trigger. A reclaim back above usually makes bearish holds less reliable."
            else:
                if spot >= flip:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is above it now. A break below can weaken bullish holds and increase chop."
                else:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is below it now. A reclaim above can weaken bearish holds and increase chop."

        lines = [
            f"{mode_emoji} {ticker} — {mode_label}",
            f"🎯 Spot / Exp: ${spot:.2f} | IV: {iv_emoji} {iv_pct:.1f}% | Exp: {expiration} ({dte} DTE)",
            f"📈 Bias: {simple_dir} | 💪 Confidence: {conf_label} ({conf_score:.0%}){downgrade_note}",
            "",
            "🧠 What matters:",
        ]
        for item in reason_lines[:3]:
            lines.append(f"  • {item}")
        if counter_lines:
            lines.append(f"  • Counterpoint: {counter_lines[0]}")

        lines += [
            "",
            f"☢️ Gamma note: {flip_line}",
            "",
            "📐 Expected Move (~21-day thesis):" if mode == "long" else f"📐 Expected Move ({thesis_days}-day view):",
            f"  1σ: ${em['bear_1sd']:.2f} → ${em['bull_1sd']:.2f} (±${em['em_1sd']:.2f})",
            f"  2σ: ${em['bear_2sd']:.2f} → ${em['bull_2sd']:.2f}",
            "",
            f"💡 Bottom line: {verdict}",
            "",
            "📦 Data:",
        ]

        if put_wall is not None:
            lines.append(f"  Put Wall / Support: ${put_wall:.2f}")
        if call_wall is not None:
            lines.append(f"  Call Wall / Resistance: ${call_wall:.2f}")
        if gamma_wall is not None:
            lines.append(f"  Gamma Wall: ${gamma_wall:.2f}")
        if flip is not None:
            lines.append(f"  Gamma Flip: ${flip:.2f}")
        if liquid and eng:
            lines.append(f"  GEX: {tgex:+.1f}M | DEX: {dex:+.1f}M | Vanna: {vanna_m:+.1f}M | Charm: {charm_m:+.1f}M")
        if v4_result:
            vr_line = format_vol_regime_line(v4_result)
            if vr_line:
                lines.append(f"  Vol: {vr_line}")
        lines.append(f"  IV note: {iv_note}")
        if mode == "long":
            lines += _build_wheel_focus_block(ticker, expiration, spot, em, walls or {})
        lines += ["", "📌 Monitoring only — no trade.", "— Not financial advice —"]

        post_to_telegram("\n".join(lines))
        log.info(f"Monitor card: {ticker} | mode={mode} | DTE={dte} | lean={bias['direction']} | conf={conf_label}")

    except Exception as e:
        log.error(f"Monitor card error for {ticker} mode={mode}: {e}", exc_info=True)
        post_to_telegram(f"⚠️ Monitor card failed for {ticker}: {type(e).__name__}")


# ─────────────────────────────────────────────────────────────────────
# EM SCHEDULER
# ─────────────────────────────────────────────────────────────────────

def _em_scheduler():
    try:
        import pytz
    except ImportError:
        log.error("pytz not installed — EM scheduler disabled")
        return
    log.info(f"0DTE EM scheduler started — fires at {EM_SCHEDULE_TIMES_CT} CT on weekdays")
    log.info("EM reconciler scheduled at 16:15 CT on weekdays")
    fired_today: set = set()
    while True:
        try:
            ct = pytz.timezone("America/Chicago"); now_ct = datetime.now(ct)
            if now_ct.weekday() >= 5: time.sleep(60); continue
            date_str = now_ct.strftime("%Y-%m-%d")
            for hour, minute in EM_SCHEDULE_TIMES_CT:
                fire_key = (date_str, hour, minute)
                if fire_key in fired_today: continue
                if now_ct.hour == hour and abs(now_ct.minute - minute) <= 1:
                    session = "morning" if hour < 12 else "afternoon"
                    fired_today.add(fire_key)
                    log.info(f"EM scheduler firing: {session}")
                    for ticker in EM_TICKERS:
                        threading.Thread(target=_post_em_card, args=(ticker, session), daemon=True).start()

            # ── Auto-reconciler: 4:15 PM CT (after market close) ──
            recon_key = (date_str, 16, 15)
            if recon_key not in fired_today:
                if now_ct.hour == 16 and abs(now_ct.minute - 15) <= 1:
                    fired_today.add(recon_key)
                    log.info("EM reconciler firing (4:15 PM CT)")
                    threading.Thread(target=_run_reconciler_auto, daemon=True, name="reconciler").start()

            fired_today = {k for k in fired_today if k[0] == date_str}
        except Exception as e:
            log.error(f"EM scheduler error: {e}", exc_info=True)
        time.sleep(60)


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
    _oi_cache = OICache(store_get, store_set)
    log.info(f"OI cache initialized (Redis: {_get_redis() is not None})")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
