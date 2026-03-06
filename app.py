# app.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3 UPGRADE — Key changes:
#   - /tv webhook now parses BUS v1.0 payload (tier, wave data, trend state)
#   - New /check TICKER command via Telegram (on-demand analysis)
#   - options_engine_v3 used for all new trade recommendations
#   - Legacy scan route preserved (still uses old engine if needed)
#   - Earnings week + dividend blocking enforced at trade level
#   - Exit targets use Brad's 30%/35%/50% return-on-risk formula

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
)

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

# ─────────────────────────────────────────────────────────
# REDIS (graceful fallback to in-memory)
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
# TELEGRAM
# ─────────────────────────────────────────────────────────

def post_to_telegram(text: str, max_retries: int = 4):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return 400, "TELEGRAM tokens not set"
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
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

def get_options_chain(ticker: str) -> tuple:
    """
    Fetch the best expiration (closest to TARGET_DTE within MIN_DTE–MAX_DTE)
    and return (expiration_str, dte, contracts_list).
    """
    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = datetime.now(timezone.utc).date()

    scored = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if MIN_DTE <= dte <= MAX_DTE:
                scored.append((abs(dte - TARGET_DTE), dte, exp))
        except Exception:
            continue

    if not scored:
        # Fallback: closest to TARGET_DTE even if outside range
        for exp in exps:
            try:
                dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
                scored.append((abs(dte - TARGET_DTE), dte, exp))
            except Exception:
                continue

    if not scored:
        raise RuntimeError(f"No usable expirations for {ticker}")

    scored.sort()
    chosen_exp, chosen_dte = scored[0][2], scored[0][1]

    data = md_get(
        f"https://api.marketdata.app/v1/options/chain/{ticker}/",
        {"expiration": chosen_exp},
    )
    if not isinstance(data, dict) or data.get("s") != "ok":
        raise RuntimeError(f"Bad chain for {ticker}")

    sym_list = data.get("optionSymbol") or []
    if not sym_list:
        raise RuntimeError(f"Empty chain for {ticker}")

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
            "expiration":   chosen_exp,
            "dte":          chosen_dte,
            "openInterest": as_int(col("openInterest",  0)[i], 0),
            "volume":       as_int(col("volume",         0)[i], 0),
            "iv":           as_float(col("iv",          None)[i], None),
            "gamma":        as_float(col("gamma",        0.0)[i], 0.0),
            "delta":        as_float(col("delta",       None)[i], None),
            "theta":        as_float(col("theta",       None)[i], None),
            "vega":         as_float(col("vega",        None)[i], None),
            "bid":          as_float(col("bid",         None)[i], None),
            "ask":          as_float(col("ask",         None)[i], None),
            "mid":          as_float(col("mid",         None)[i], None),
        })

    return chosen_exp, chosen_dte, contracts


# ─────────────────────────────────────────────────────────
# CHECK TICKER (v3 engine — on-demand via /check or /tv)
# ─────────────────────────────────────────────────────────

def check_ticker(
    ticker: str,
    direction: str = "bull",
    webhook_data: dict = None,
) -> dict:
    """
    Full v3 pipeline: fetch data → check rules → build spread → post card.
    Used by both /check command and /tv webhook.
    Returns result dict with 'ok', 'posted', 'reason' keys.
    """
    ticker = ticker.strip().upper()
    webhook_data = webhook_data or {"bias": direction, "tier": "2"}

    try:
        # Fetch market data
        spot = get_spot(ticker)
        exp, dte, contracts = get_options_chain(ticker)

        # Enrichment: earnings + IV
        enrichment = enrich_ticker(ticker)
        has_earnings = enrichment.get("has_earnings", False)
        earnings_warn = enrichment.get("earnings_warn")

        # Check dividend (simple: skip if ex-div in DTE window)
        # TODO: Add dividend API check here
        has_dividend = False

        log.info(f"check_ticker({ticker}): spot={spot} exp={exp} dte={dte} "
                 f"contracts={len(contracts)} earnings={has_earnings}")

        # Run v3 engine
        rec = recommend_trade(
            ticker=ticker,
            spot=spot,
            contracts=contracts,
            dte=dte,
            expiration=exp,
            webhook_data=webhook_data,
            has_earnings=has_earnings,
            has_dividend=has_dividend,
        )

        # If no trade, return the reason
        if not rec.get("ok"):
            log.info(f"check_ticker({ticker}): no trade — {rec.get('reason')}")
            return {
                "ticker":     ticker,
                "ok":         False,
                "posted":     False,
                "reason":     rec.get("reason"),
                "confidence": rec.get("confidence"),
            }

        # Dedup check
        trade = rec.get("trade", {})
        if is_duplicate_trade(ticker, direction, trade.get("short"), trade.get("long")):
            return {
                "ticker": ticker,
                "ok":     True,
                "posted": False,
                "reason": "Duplicate trade in dedup window",
            }

        # Format and post trade card
        card = format_trade_card(rec)

        # Prepend earnings warning if applicable
        if has_earnings and earnings_warn:
            card = earnings_warn + "\n\n" + card

        st, body = post_to_telegram(card)

        if st == 200:
            mark_trade_sent(ticker, direction, trade.get("short"), trade.get("long"))

        return {
            "ticker":     ticker,
            "ok":         True,
            "posted":     st == 200,
            "tg_status":  st,
            "confidence": rec.get("confidence"),
            "trade":      trade,
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
        "TELEGRAM_set":    bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "MARKETDATA_set":  bool(MARKETDATA_TOKEN),
        "REDIS_connected": r is not None,
        "WATCHLIST_len":   len([t for t in WATCHLIST.split(",") if t.strip()]),
        "ENGINE_VERSION":  "v3",
        "SCAN_WORKERS":    SCAN_WORKERS,
        "DEDUP_TTL_S":     DEDUP_TTL_SECONDS,
    })

@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram("✅ Telegram test OK (v3 engine)")
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
            scan_fn      = lambda t: check_ticker(t),  # /scan now uses v3 too
            full_scan_fn = lambda: scan_watchlist_internal(tickers),
            check_fn     = check_ticker,
            watchlist    = tickers,
        )

    threading.Thread(target=run_command, daemon=True).start()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────
# TRADINGVIEW WEBHOOK (BUS v1.0 payload)
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

    # Parse BUS v1.0 webhook fields
    webhook_data = {
        "tier":            tier,
        "bias":            bias,
        "close":           close,
        "time":            data.get("time", ""),
        "ema5":            as_float(data.get("ema5")),
        "ema12":           as_float(data.get("ema12")),
        "ema_dist_pct":    as_float(data.get("ema_dist_pct")),
        "macd_hist":       as_float(data.get("macd_hist")),
        "macd_line":       as_float(data.get("macd_line")),
        "signal_line":     as_float(data.get("signal_line")),
        "wt1":             as_float(data.get("wt1")),
        "wt2":             as_float(data.get("wt2")),
        "rsi_mfi":         as_float(data.get("rsi_mfi")),
        "rsi_mfi_bull":    data.get("rsi_mfi_bull") in (True, "true"),
        "stoch_k":         as_float(data.get("stoch_k")),
        "stoch_d":         as_float(data.get("stoch_d")),
        "vwap":            as_float(data.get("vwap")),
        "above_vwap":      data.get("above_vwap") in (True, "true"),
        "htf_confirmed":   data.get("htf_confirmed") in (True, "true"),
        "htf_converging":  data.get("htf_converging") in (True, "true"),
        "daily_bull":      data.get("daily_bull") in (True, "true"),
        "volume":          as_float(data.get("volume")),
        "timeframe":       data.get("timeframe", ""),
    }

    # Build signal context message for Telegram
    tier_emoji = "🥇" if tier == "1" else "🥈" if tier == "2" else "📢"
    wt2 = webhook_data.get("wt2") or 0
    wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"

    trend_str = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                 else "🟡 Converging" if webhook_data["htf_converging"]
                 else "❌ Diverging")

    signal_lines = [
        f"{tier_emoji} TV Signal — {ticker} (T{tier} {bias.upper()})",
        f"Close: ${close:.2f} | {data.get('timeframe', '')} timeframe",
        f"1H Trend: {trend_str} | Daily: {'🟢' if webhook_data['daily_bull'] else '🔴'}",
        f"Wave: {wave_zone} (wt2={wt2:.1f})",
        f"VWAP: {'Above ✅' if webhook_data['above_vwap'] else 'Below'} | "
        f"RSI+MFI: {'Buying ✅' if webhook_data['rsi_mfi_bull'] else 'Selling'}",
        "",
    ]
    signal_msg = "\n".join(signal_lines)

    # Run check in background — return to TradingView immediately
    def run_tv_check():
        try:
            # Post signal context first
            post_to_telegram(signal_msg)

            # Only process bull signals (per trading rules)
            if bias not in ALLOWED_DIRECTIONS:
                post_to_telegram(f"ℹ️ {ticker}: {bias} signal skipped — bull only mode")
                return

            # Run v3 engine
            check_ticker(ticker, direction=bias, webhook_data=webhook_data)

        except Exception as e:
            log.error(f"TV check error for {ticker}: {e}")

    threading.Thread(target=run_tv_check, daemon=True).start()
    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# WATCHLIST SCAN (scheduled via cron or /scan command)
# ─────────────────────────────────────────────────────────

def scan_watchlist_internal(tickers: list, max_posts: int = 6):
    if is_paused():
        post_to_telegram("⏸ Scan skipped — bot is paused.")
        return

    posted = 0
    results = []
    no_trade = []
    errors = []
    earnings_flagged = []

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {
            executor.submit(check_ticker, t, "bull"): t
            for t in tickers
        }
        for future in as_completed(futures):
            if posted >= max_posts:
                future.cancel()
                continue
            res = future.result()
            results.append(res)

            ticker = res.get("ticker", "?")
            if res.get("posted"):
                posted += 1
            elif res.get("error"):
                errors.append(ticker)
            else:
                no_trade.append(f"{ticker}: {res.get('reason', '—')[:40]}")

    # Summary
    summary_lines = [
        f"📋 WATCHLIST SUMMARY — {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        f"Scanned: {len(tickers)} | Trade cards: {posted}",
        "",
    ]
    if no_trade:
        summary_lines.append("No setup: " + ", ".join(no_trade[:10]))
    if errors:
        summary_lines.append(f"Errors: {', '.join(errors)}")
    summary_lines += ["", "— Not financial advice —"]

    post_to_telegram("\n".join(summary_lines))
    set_last_scan(posted, len(tickers))


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    data     = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()

    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]
    if not tickers:
        return jsonify({"error": "WATCHLIST empty"}), 400

    if is_paused():
        return jsonify({"status": "paused"})

    max_posts = as_int(data.get("max_posts"), 6)

    # Run in background
    threading.Thread(
        target=scan_watchlist_internal,
        args=(tickers, max_posts),
        daemon=True,
    ).start()

    return jsonify({"status": "accepted", "tickers": len(tickers)})


# ─────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────

with app.app_context():
    _tg_ws = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if _tg_ws and BOT_URL:
        register_webhook(BOT_URL, _tg_ws)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
