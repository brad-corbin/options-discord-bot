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
_mem_store: dict = {}

def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        return None
    try:
        import redis
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
        _redis_client.ping()
        log.info("Redis connected")
        return _redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}), using in-memory fallback")
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
            log.warning(f"Redis set failed: {e}")
    _mem_store[key] = value

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
    return key in _mem_store

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
# TV SIGNAL QUEUE
# ─────────────────────────────────────────────────────────
# Placed here — after Redis, store_*, and dedup helpers are defined.
# Worker calls post_to_telegram, check_spread_exit_warning, check_ticker
# which are defined further below; Python resolves these at call time
# not at definition time, so forward references are fine.

TV_QUEUE_MAX = 50
_tv_queue: queue.Queue = queue.Queue(maxsize=TV_QUEUE_MAX)

def _tv_queue_worker():
    """Single worker thread — drains the TV signal queue one by one."""
    log.info("TV queue worker started")
    while True:
        try:
            job = _tv_queue.get(block=True, timeout=5)
            if job is None:
                break
            ticker, bias, webhook_data, signal_msg = job
            try:
                post_to_telegram(signal_msg)
                check_spread_exit_warning(ticker, bias, webhook_data)
                if bias in ALLOWED_DIRECTIONS:
                    check_ticker(ticker, direction=bias, webhook_data=webhook_data)
                else:
                    post_to_telegram(f"ℹ️ {ticker}: {bias} signal skipped — not in allowed directions")
            except Exception as e:
                log.error(f"TV queue worker error for {ticker}: {e}", exc_info=True)
            finally:
                _tv_queue.task_done()
        except queue.Empty:
            continue

_tv_worker_thread = threading.Thread(target=_tv_queue_worker, daemon=True, name="tv-queue-worker")
_tv_worker_thread.start()

# ─────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────

def post_to_telegram(text: str, max_retries: int = 4, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not cid:
        return 400, "TELEGRAM tokens not set"
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return 200, ""
            last_err = r.text[:300] if r.text else f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(min(1.5 * (attempt + 1), 6.0))
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
    r = requests.get(url,
                     headers={"Authorization": f"Bearer {MARKETDATA_TOKEN}"},
                     params=params or {},
                     timeout=25)
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
    """Fetch current VIX spot level.
    MarketData.app does not serve VIX. Use Yahoo Finance instead,
    with SPY IV as fallback, and 20.0 as last resort.
    """
    # Primary: Yahoo Finance (^VIX) — no API key required
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

    # Fallback: SPY options chain implied vol as VIX proxy
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
    import time as _time
    now = _time.time()
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
    """
    Estimate IV rank (0-100) from current IV vs realized vol.
    Higher rank = IV expensive relative to recent realized vol.
    """
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

        # Where is current IV relative to realized vol?
        # IV == RV → rank 50, IV 2x RV → rank 90, IV 0.5x RV → rank 10
        ratio = current_iv / rv
        rank  = min(100, max(0, (ratio - 0.5) / 1.5 * 100))
        return round(rank, 1)

    except Exception:
        return 50.0


# ─────────────────────────────────────────────────────────
# EXIT WARNING — warns on opposite open spreads
# ─────────────────────────────────────────────────────────

def check_spread_exit_warning(ticker: str, signal_bias: str, webhook_data: dict):
    """
    Called when a TV signal comes in. Warns about open spreads
    that are OPPOSITE to the signal direction.
    """
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

        st, body = post_to_telegram(card)

        if st == 200:
            mark_trade_sent(ticker, direction, trade.get("short"), trade.get("long"))

        trade_journal.log_signal(
            ticker, webhook_data,
            outcome="trade_opened" if risk_result["allowed"] else "risk_blocked",
            confidence=best_rec.get("confidence"),
        )

        return {
            "ticker":              ticker,
            "ok":                  True,
            "posted":              st == 200,
            "tg_status":           st,
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
    return "OK"


@app.route("/debug", methods=["GET"])
def debug():
    r = _get_redis()
    return jsonify({
        "TELEGRAM_set":          bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "MARKETDATA_set":        bool(MARKETDATA_TOKEN),
        "REDIS_connected":       r is not None,
        "WATCHLIST_len":         len([t for t in WATCHLIST.split(",") if t.strip()]),
        "ENGINE_VERSION":        "v3.7",
        "SCAN_WORKERS":          SCAN_WORKERS,
        "DEDUP_TTL_S":           DEDUP_TTL_SECONDS,
        "ALLOWED_DIRECTIONS":    ALLOWED_DIRECTIONS,
        "PORTFOLIO_CHANNEL_set": bool(TELEGRAM_PORTFOLIO_CHAT_ID),
        "MOM_CHANNEL_set":       bool(TELEGRAM_MOM_PORTFOLIO_CHAT_ID),
    })


@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram("✅ Telegram test OK (v3.7 scalp + swing)")
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
            get_regime_fn = get_current_regime,
        )

    threading.Thread(target=run_command, daemon=True).start()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────
# TRADINGVIEW WEBHOOK — scalp signals (/tv)
# Receives signals from BUS v1.0 Pine indicator (15M chart)
# ─────────────────────────────────────────────────────────

@app.route("/tv", methods=["POST"])
def tv_webhook():
    data = request.get_json(silent=True) or {}
    raw  = (request.get_data(as_text=True) or "").strip()

    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "bull").strip().lower()
    tier   = (data.get("tier")   or "2").strip()
    close  = as_float(data.get("close"), 0.0)

    if not ticker:
        st, _ = post_to_telegram("📢 TV signal received (no ticker)")
        return jsonify({"status": "received_raw", "tg_status": st})

    log.info(f"TV signal: {ticker} bias={bias} tier={tier} close={close}")

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

    # Enqueue — returns immediately so TradingView doesn't time out
    try:
        _tv_queue.put_nowait((ticker, bias, webhook_data, signal_msg))
        qsize = _tv_queue.qsize()
        log.info(f"TV signal queued: {ticker} (queue depth: {qsize})")
        if qsize > TV_QUEUE_MAX * 0.8:
            log.warning(f"TV queue at {qsize}/{TV_QUEUE_MAX} — approaching capacity")
    except queue.Full:
        # Queue is full — drop oldest, enqueue new
        try:
            dropped = _tv_queue.get_nowait()
            log.warning(f"TV queue full — dropped oldest signal: {dropped[0]}")
            _tv_queue.task_done()
        except queue.Empty:
            pass
        try:
            _tv_queue.put_nowait((ticker, bias, webhook_data, signal_msg))
        except queue.Full:
            log.error(f"TV queue still full after drop — signal for {ticker} lost")

    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# SWING WEBHOOK — Fibonacci swing signals (/swing)
# Receives signals from BFS v1.0 Pine indicator (daily chart)
# ─────────────────────────────────────────────────────────

@app.route("/swing", methods=["POST"])
def swing_webhook():
    data = request.get_json(silent=True) or {}

    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "bull").strip().lower()
    tier   = (data.get("tier")   or "2").strip()
    close  = as_float(data.get("close"), 0.0)

    if not ticker:
        return jsonify({"status": "ignored", "reason": "no ticker"}), 200

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

    # Signal summary
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

    def run_swing_check():
        try:
            from swing_engine import recommend_swing_trade, format_swing_card

            post_to_telegram(signal_msg)

            spot   = get_spot(ticker)
            chains = get_options_chain_swing(ticker)

            if not chains:
                post_to_telegram(f"❌ {ticker} swing: no options chain data in 7-60 DTE range")
                return

            candles = get_daily_candles(ticker, days=252)
            iv_rank = _estimate_iv_rank(chains, candles)

            rec = recommend_swing_trade(
                ticker=ticker,
                spot=spot,
                chains=chains,
                webhook_data=webhook_data,
                iv_rank=iv_rank,
            )

            if not rec.get("ok"):
                reason = rec.get("reason", "no valid setup")
                conf   = rec.get("confidence")
                msg    = f"❌ {ticker} swing — {reason}"
                if conf:
                    msg += f"\nConfidence: {conf}/100"
                post_to_telegram(msg)
                return

            card = format_swing_card(rec)
            post_to_telegram(card)
            log.info(f"Swing card posted: {ticker} {bias} T{tier} "
                     f"fib={fib_level}% conf={rec.get('confidence')}/100")

        except Exception as e:
            log.error(f"Swing check error for {ticker}: {type(e).__name__}: {e}")
            post_to_telegram(f"⚠️ Swing error for {ticker}: {type(e).__name__}")

    threading.Thread(target=run_swing_check, daemon=True).start()
    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# WATCHLIST SCAN — DISABLED
# Scheduled scan removed. TradingView webhooks are the only signal source.
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
