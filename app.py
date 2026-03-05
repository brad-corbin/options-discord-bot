# app.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# UPGRADED APP v2 — Full rewrite
# Improvements:
#   - Redis-backed OI snapshot persistence (falls back to in-memory gracefully)
#   - IV rank passed into options_engine for smarter spread selection
#   - Skew computed and surfaced in messages
#   - Direction bias uses multi-factor scoring (not naïve midpoint)
#   - Confidence gating: low-confidence trades are suppressed, not posted
#   - Position sizing surfaced in trade messages
#   - Async-style parallel scanning via ThreadPoolExecutor
#   - Duplicate trade prevention via Redis (or in-memory set)
#   - Cleaner message formatting with structured sections
#   - net_gex passed through to options_engine for regime-aware selection
from telegram_commands import (
    handle_command,
    register_webhook,
    is_paused,
    get_confidence_gate,
    set_last_scan,
    get_state,
)

import os
import time
import math
import json
import hashlib
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from data_providers import enrich_ticker

import requests
from flask import Flask, request, jsonify

from options_engine import (
    recommend_from_marketdata,
    compute_iv_rank,
    skew_score,
    pick_atm_iv,
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
# Register Telegram webhook on startup
_tg_webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

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
REDIS_URL           = os.getenv("REDIS_URL",           "").strip()   # Render Redis add-on

# ─────────────────────────────────────────────────────────
# TUNABLES
# ─────────────────────────────────────────────────────────
MAX_SPREAD_WIDTH        = float(os.getenv("MAX_SPREAD_WIDTH",        "10")   or 10)
MAX_DEBIT_PCT_WIDTH     = float(os.getenv("MAX_DEBIT_PCT_WIDTH",     "0.60") or 0.60)
LIQ_WARN_MIN_OI         = int(  os.getenv("LIQ_WARN_MIN_OI",         "500")  or 500)
LIQ_WARN_BA             = float(os.getenv("LIQ_WARN_BA",             "0.30") or 0.30)
SCAN_MAX_DTE            = int(  os.getenv("SCAN_MAX_DTE",             "7")   or 7)
TRADE_TARGET_DTE        = int(  os.getenv("TRADE_TARGET_DTE",         "5")   or 5)
EXPECTED_MOVE_DTE       = int(  os.getenv("EXPECTED_MOVE_DTE",        "5")   or 5)
DEFAULT_MAX_POSTS       = int(  os.getenv("MAX_POSTS_PER_SCAN",       "6")   or 6)
MIN_ALPHA_TO_POST       = int(  os.getenv("MIN_ALPHA_TO_POST",        "45")  or 45)  # confidence gate
MIN_CONFIDENCE_TO_POST  = int(  os.getenv("MIN_CONFIDENCE_TO_POST",   "40")  or 40)
ACCOUNT_SIZE            = float(os.getenv("ACCOUNT_SIZE",       "100000")    or 100000)
MAX_RISK_PCT            = float(os.getenv("MAX_RISK_PCT",          "0.02")   or 0.02)
MAX_RISK_USD            = float(os.getenv("MAX_RISK_USD",           "500")   or 500)
SCAN_WORKERS            = int(  os.getenv("SCAN_WORKERS",              "4")  or 4)   # parallel threads
DEDUP_TTL_SECONDS       = int(  os.getenv("DEDUP_TTL_SECONDS",      "3600")  or 3600) # 1hr dedup window

# ─────────────────────────────────────────────────────────
# REDIS  (graceful fallback to in-memory)
# ─────────────────────────────────────────────────────────
_redis_client = None
_mem_store: dict = {}   # fallback

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


def store_get(key: str) -> str | None:
    r = _get_redis()
    if r:
        try:
            return r.get(key)
        except Exception as e:
            log.warning(f"Redis get failed: {e}")
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
# OI SNAPSHOT  (Redis-backed)
# ─────────────────────────────────────────────────────────

def _oi_key(ticker, exp, right, strike):
    return f"oi:{ticker}:{exp}:{right}:{strike}"


def get_prev_oi(ticker, exp, right, strike) -> int | None:
    v = store_get(_oi_key(ticker, exp, right, strike))
    return int(v) if v is not None else None


def set_prev_oi(ticker, exp, right, strike, oi: int):
    store_set(_oi_key(ticker, exp, right, strike), str(oi), ttl=86400)  # 24hr TTL


# ─────────────────────────────────────────────────────────
# DEDUP  (prevent same ticker+direction+strikes posting twice in TTL window)
# ─────────────────────────────────────────────────────────

def trade_dedup_key(ticker, direction, short_k, long_k) -> str:
    raw = f"{ticker}:{direction}:{short_k}:{long_k}"
    return "dedup:" + hashlib.md5(raw.encode()).hexdigest()


def is_duplicate_trade(ticker, direction, short_k, long_k) -> bool:
    return store_exists(trade_dedup_key(ticker, direction, short_k, long_k))


def mark_trade_sent(ticker, direction, short_k, long_k):
    k = trade_dedup_key(ticker, direction, short_k, long_k)
    store_set(k, "1", ttl=DEDUP_TTL_SECONDS)


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def first_val(x, default=None):
    if x is None:
        return default
    return x[0] if isinstance(x, list) else x


def as_float(x, default=0.0):
    v = first_val(x, default)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default

def as_int(x, default=0):
    v = first_val(x, default)
    try:
        return int(v) if v is not None else int(default)
    except Exception:
        return int(default)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def gex_summary(net_gex: float, contracts: list, spot: float) -> tuple[str, str, float | None]:
    """
    Returns (regime_line, magnitude_str, flip_level).
    
    GEX GUIDE FOR TRADERS:
    - Positive GEX + High magnitude = dealers are LONG gamma = they BUY dips, SELL rips
      → Market gets pinned, low volatility, range-bound. Sell spreads / iron condors.
    - Negative GEX + High magnitude = dealers are SHORT gamma = they SELL dips, BUY rips  
      → Market trends / whips violently. Directional spreads, wider stops, expect moves.
    - Gamma Flip Level = price where regime CHANGES. Crossing it = behavior shift.
      → Above flip = positive gamma (calm). Below flip = negative gamma (volatile).
    - Low magnitude (< $500M) = GEX signal is weak, don't weight it heavily.
    """
    # Magnitude label
    mag = abs(net_gex)
    if mag >= 1e9:
        mag_str = f"${mag/1e9:.1f}B"
    elif mag >= 1e6:
        mag_str = f"${mag/1e6:.0f}M"
    else:
        mag_str = f"${mag:.0f}"

    # Signal strength
    if mag >= 2e9:
        strength = "Strong"
    elif mag >= 500e6:
        strength = "Moderate"
    else:
        strength = "Weak"

    # Regime
    regime = "Positive" if net_gex >= 0 else "Negative"
    behavior = "range-bound / pin risk" if net_gex >= 0 else "trending / volatile"

    # Gamma flip: strike where net GEX crosses zero
    # Approximate by finding strike with smallest absolute net GEX contribution
    strike_gex = {}
    for c in contracts:
        right  = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), None)
        if strike is None:
            continue
        oi    = as_int(c.get("openInterest"), 0)
        gamma = as_float(c.get("gamma"), 0.0)
        contrib = oi * gamma * (spot ** 2) * 100
        if right == "call":
            strike_gex[strike] = strike_gex.get(strike, 0) + contrib
        elif right == "put":
            strike_gex[strike] = strike_gex.get(strike, 0) - contrib

    flip_level = None
    if strike_gex and len(strike_gex) >= 3:
        # Find the strike where cumulative GEX crosses zero
        # Sort strikes and walk cumulative sum to find sign change
        sorted_strikes = sorted(strike_gex.keys())
        cumulative = 0.0
        prev_strike = None
        for k in sorted_strikes:
            cumulative += strike_gex[k]
            if prev_strike is not None:
                # Sign change detected → flip is between prev and current
                prev_cum = cumulative - strike_gex[k]
                if (prev_cum >= 0 and cumulative < 0) or (prev_cum < 0 and cumulative >= 0):
                    # Interpolate: weight by magnitude
                    total = abs(prev_cum) + abs(cumulative)
                    if total > 0:
                        flip_level = round(
                            (prev_strike * abs(cumulative) + k * abs(prev_cum)) / total, 2
                        )
                    else:
                        flip_level = k
                    break
            prev_strike = k

        # Fallback: if no zero crossing found, use strike nearest to spot
        # with smallest absolute cumulative GEX
        if flip_level is None:
            running = 0.0
            best_dist = float("inf")
            for k in sorted_strikes:
                running += strike_gex[k]
                dist = abs(running)
                if dist < best_dist:
                    best_dist = dist
                    flip_level = k
    flip_str = f"{flip_level:.2f}" if flip_level is not None else "—"
    regime_line = (
        f"GEX: {regime} ({strength}) | {mag_str} | Flip: {flip_str}\n"
        f"  → {behavior}"
    )

    return regime_line, mag_str, flip_level


# ─────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────

def post_to_telegram(text: str, max_retries: int = 4) -> tuple[int, str]:
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

@app.route("/telegram_webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    # Verify secret
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
            scan_fn      = scan_ticker,
            full_scan_fn = lambda: scan_watchlist_internal(tickers),
            watchlist    = tickers,
        )

    threading.Thread(target=run_command, daemon=True).start()
    return jsonify({"ok": True})
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
    params = {}
    if os.getenv("MD_WEEKLY_ONLY", "0").strip().lower() in ("1", "true", "yes"):
        params["weekly"] = "true"
    data = md_get(f"https://api.marketdata.app/v1/options/expirations/{ticker}/", params)
    if not isinstance(data, dict) or data.get("s") != "ok":
        raise RuntimeError(f"Bad expirations for {ticker}")
    return sorted(set(str(e)[:10] for e in (data.get("expirations") or []) if e))


def get_options_chain(ticker: str, max_dte: int = 7) -> tuple:
    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = datetime.now(timezone.utc).date()

    scored = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            scored.append((abs(dte - TRADE_TARGET_DTE), dte, exp))
        except Exception:
            continue

    if not scored:
        raise RuntimeError(f"No usable expirations for {ticker}")

    within = [x for x in scored if x[1] <= max_dte]
    scored = sorted(within or scored)
    chosen_exp, chosen_dte = scored[0][2], scored[0][1]

    params = {"expiration": chosen_exp}
    if os.getenv("MD_WEEKLY_ONLY", "0").strip().lower() in ("1", "true", "yes"):
        params["weekly"] = "true"

    data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/", params)
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
            "bid":          as_float(col("bid",         None)[i], None),
            "ask":          as_float(col("ask",         None)[i], None),
            "mid":          as_float(col("mid",         None)[i], None),
        })

    return chosen_exp, contracts


# ─────────────────────────────────────────────────────────
# CORE METRICS
# ─────────────────────────────────────────────────────────

def compute_walls_and_gex(spot: float, contracts: list) -> tuple:
    call_oi, put_oi = {}, {}
    strikes         = []
    net_gex         = 0.0

    for c in contracts:
        right  = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), None)
        if strike is None:
            continue
        strikes.append(strike)
        oi    = as_int(c.get("openInterest"), 0)
        gamma = as_float(c.get("gamma"), 0.0)

        if right == "call":
            call_oi[strike] = call_oi.get(strike, 0) + oi
            net_gex += oi * gamma * (spot ** 2) * 100
        elif right == "put":
            put_oi[strike]  = put_oi.get(strike, 0) + oi
            net_gex -= oi * gamma * (spot ** 2) * 100

    call_wall = max(call_oi.items(), key=lambda kv: kv[1])[0] if call_oi else None
    put_wall  = max(put_oi.items(),  key=lambda kv: kv[1])[0] if put_oi  else None

    s = sorted(set(strikes))
    diffs = [s[i+1] - s[i] for i in range(len(s)-1) if s[i+1] > s[i]]
    inc = min(diffs) if diffs else 1.0

    return call_wall, put_wall, net_gex, inc


def oi_change_score(ticker: str, exp: str, contracts: list) -> tuple:
    total, biggest_abs, biggest_label = 0, 0, "—"

    for c in contracts:
        right  = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), None)
        if strike is None:
            continue
        oi   = as_int(c.get("openInterest"), 0)
        prev = get_prev_oi(ticker, exp, right, strike)
        set_prev_oi(ticker, exp, right, strike, oi)

        if prev is not None:
            delta = oi - prev
            absd  = abs(delta)
            total += absd
            if absd > biggest_abs:
                biggest_abs   = absd
                biggest_label = f"{right.upper()} {strike:g} ΔOI {delta:+d}"

    return total, biggest_label


def atm_iv_from_contracts(contracts: list, spot: float) -> float:
    near = sorted(
        [c for c in contracts if c.get("strike") is not None],
        key=lambda c: abs(as_float(c.get("strike"), 0) - spot)
    )[:10]
    ivs = [as_float(c.get("iv"), None) for c in near if c.get("iv") is not None and as_float(c.get("iv"), 0) > 0]
    return sum(ivs) / len(ivs) if ivs else 0.30

def compute_flow_signal(contracts: list, trade: dict, spot: float) -> list:
    """
    Analyzes options volume vs open interest for the specific spread legs
    and overall chain to detect unusual flow.

    Returns list of strings to append to trade card.
    """
    if not trade or not contracts:
        return []

    short_k    = trade.get("short")
    long_k     = trade.get("long")
    trade_side = (trade.get("side") or "").lower()  # call or put

    # Aggregate chain-level volume
    total_call_vol = 0
    total_put_vol  = 0
    total_call_oi  = 0
    total_put_oi   = 0

    leg_data = {}  # strike → {vol, oi, ratio}

    for c in contracts:
        right  = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), None)
        if strike is None:
            continue

        vol = as_int(c.get("volume"), 0)
        oi  = as_int(c.get("openInterest"), 0)

        if right == "call":
            total_call_vol += vol
            total_call_oi  += oi
        elif right == "put":
            total_put_vol += vol
            total_put_oi  += oi

        if strike in (short_k, long_k) and right == trade_side:
            ratio = round(vol / oi, 3) if oi > 0 else 0
            leg_data[strike] = {
                "vol":   vol,
                "oi":    oi,
                "ratio": ratio,
            }

    lines = ["📊 Flow Signal:"]

    # Leg-level analysis
    for label, k in [("Long", long_k), ("Short", short_k)]:
        d = leg_data.get(k)
        if not d:
            continue

        vol, oi, ratio = d["vol"], d["oi"], d["ratio"]

        if ratio >= 1.0:
            flag = "🔥🔥 VERY unusual"
        elif ratio >= 0.50:
            flag = "🔥 Unusual"
        elif ratio >= 0.15:
            flag = "⚡ Notable"
        else:
            flag = ""

        vol_str = f"{vol:,}" if vol else "—"
        oi_str  = f"{oi:,}"  if oi  else "—"
        ratio_str = f"{ratio:.2f}" if ratio else "—"

        flag_str = f" {flag}" if flag else ""
        lines.append(
            f"  {label} {k} {trade_side.upper()}: "
            f"Vol {vol_str} | OI {oi_str} | V/OI {ratio_str}{flag_str}"
        )

    # Chain-level put/call volume ratio
    if total_call_vol > 0 and total_put_vol > 0:
        pc_ratio = round(total_put_vol / total_call_vol, 2)
        if pc_ratio >= 1.5:
            chain_note = f"P/C Vol Ratio: {pc_ratio} 🐻 Heavy put flow"
        elif pc_ratio <= 0.67:
            chain_note = f"P/C Vol Ratio: {pc_ratio} 🐂 Heavy call flow"
        else:
            chain_note = f"P/C Vol Ratio: {pc_ratio} — balanced"
        lines.append(f"  Chain: {chain_note}")

    # Unusual flow summary
    unusual_legs = [
        k for k, d in leg_data.items()
        if d["ratio"] >= 0.50
    ]
    if unusual_legs:
        lines.append(f"  ⚠️ Unusual flow on: {', '.join(str(k) for k in unusual_legs)}")

    return lines if len(lines) > 1 else []

def risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score) -> tuple:
    if call_wall is None or put_wall is None:
        return "⚪ Unknown", "Walls unavailable"

    near = min(abs(call_wall - spot), abs(spot - put_wall))
    notes, score = [], 0

    if net_gex >= 0:
        score += 1; notes.append("Positive Gamma")
    else:
        score -= 1; notes.append("Negative Gamma")

    if near <= 2 * inc:
        score -= 1; notes.append("Near wall")
    else:
        score += 1; notes.append("Clear of walls")

    if oi_score > 50000:
        score -= 1; notes.append("Large OI shift")
    elif oi_score > 15000:
        notes.append("Moderate OI shift")
    else:
        score += 1; notes.append("OI stable")

    label = "🟢 Low" if score >= 2 else "🟡 Medium" if score == 1 else "🔴 High"
    return label, " | ".join(notes)


def alpha_score(spot, call_wall, put_wall, net_gex, oi_score, risk_label, emove, inc) -> int:
    score = 50
    if "Low"    in risk_label: score += 12
    elif "Medium" in risk_label: score += 4
    elif "High"  in risk_label: score -= 10

    score += 6 if net_gex >= 0 else -6

    if oi_score > 50000:
        score += 6
    elif oi_score > 15000:
        score += 3

    if call_wall and put_wall:
        near = min(abs(call_wall - spot), abs(spot - put_wall))
        score += 8 if near <= 2 * inc else 3 if near <= 5 * inc else 0

    score += 2 if emove > 0 else -10
    return int(clamp(score, 0, 100))


# ─────────────────────────────────────────────────────────
# MESSAGE BUILDERS
# ─────────────────────────────────────────────────────────

def build_scan_message(
    ticker, spot, exp, dte, call_wall, put_wall, net_gex, emove,
    oi_note, risk_label, risk_notes, a_score, direction,
    iv_rank, skew_bias, skew_val, trade_lines, contracts,
    hv20=None, atm_iv=None,
) -> str:

    walls = (f"Walls: Call {call_wall:g} | Put {put_wall:g} | ZeroG ~{(call_wall+put_wall)/2:.0f}"
             if call_wall and put_wall else "Walls: —")

    ivr_str  = f"{iv_rank:.0f}/100" if iv_rank is not None else "— (not yet available)"

    ivr_str = f"{iv_rank:.0f}/100" if iv_rank is not None else "— (not yet available)"

    # HV20 vs IV analysis
    hv_line = None
    if hv20 is not None and atm_iv is not None and hv20 > 0:
        ratio = round(atm_iv / hv20, 2)
        if ratio >= 1.30:
            hv_signal = "🔴 IV rich — credit favored"
        elif ratio <= 0.90:
            hv_signal = "🟢 IV cheap — debit favored"
        else:
            hv_signal = "⚪ IV fair — neutral"
        hv_line = (
            f"IV vs HV20: IV {atm_iv:.2f} | "
            f"HV20 {hv20:.2f} | "
            f"Ratio {ratio} {hv_signal}"
        )
        
    # HV20 vs IV ratio
    hv20 = locals().get("hv20")  # passed via extra context

    # Skew interpretation
    if skew_bias == "bull_skew":
        skew_note = "calls pricier → bullish flow"
    elif skew_bias == "bear_skew":
        skew_note = "puts pricier → protective demand"
    else:
        skew_note = "balanced"
    skew_str = f"{skew_bias} ({skew_val:+.3f}) — {skew_note}"

    # GEX summary (replaces broken ASCII bar)
    gex_line, _, flip_level = gex_summary(net_gex, contracts, spot)

    # Flip level warning
    flip_warn = ""
    if flip_level is not None and call_wall is not None and put_wall is not None:
        dist_to_flip = abs(spot - flip_level)
        inc = abs(call_wall - put_wall) / 10 or 1.0
        if dist_to_flip <= 2 * inc:
            flip_warn = f"⚠️ Spot near Gamma Flip ({flip_level:g}) — regime change risk"

    lines = [
        f"🚨 SCAN — {ticker}",
        f"Direction: {direction.upper()} | DTE: {max(dte,1)} ({exp})",
        f"Spot: {spot:.2f} | E-Move ±{emove:.2f}",
        f"IV Rank: {ivr_str}",
        hv_line if hv_line else None,
        f"Skew: {skew_str}",
        gex_line,
        flip_warn if flip_warn else None,
        walls,
        f"OI Signal: {oi_note}",
        f"Risk: {risk_label} — {risk_notes}",
        f"Big Alpha: {a_score}/100",
        "",
    ]
    lines += trade_lines
    lines += ["", "— Not financial advice —"]
    return "\n".join(l for l in lines if l is not None)


def build_trade_lines(rec: dict, ticker: str) -> list:
    if not rec.get("ok"):
        reason = rec.get("reason", "—")
        conf   = rec.get("confidence")
        lines  = [f"🧠 Trade: suppressed — {reason}"]
        if conf is not None:
            lines.append(f"Confidence: {conf}/100")
        return lines

    trade      = rec.get("trade") or {}
    stype      = (trade.get("type") or "").upper()
    side       = (trade.get("side") or rec.get("side") or "").upper()
    all_cands  = rec.get("all_candidates") or []
    spot       = rec.get("spot") or 0

    conf       = rec.get("confidence", "—")
    conf_r     = rec.get("conf_reasons") or []
    contracts  = rec.get("contracts_suggested", 1)
    dollar_r   = rec.get("dollar_risk", 0)

    # Filter candidates to same type and side as best trade
    best_type = (trade.get("type") or "").lower()
    best_side = (trade.get("side") or "").lower()
    same_type_cands = [
        c for c in all_cands
        if (c.get("type") or "").lower() == best_type
        and (c.get("side") or "").lower() == best_side
        and c.get("RoR") is not None
    ]

    # Sort by width, deduplicate widths, take top 3
    seen_widths = set()
    ladder = []
    for c in sorted(same_type_cands, key=lambda x: x.get("width", 0)):
        w = c.get("width")
        if w not in seen_widths:
            seen_widths.add(w)
            ladder.append(c)
        if len(ladder) >= 3:
            break

    # If we have less than 2 candidates just show single trade
    show_ladder = len(ladder) >= 2

    # Header
    best_long  = trade.get("long")
    best_short = trade.get("short")
    long_delta = trade.get("long_delta")
    itm_amount = trade.get("itm_amount")
    cost_pct   = trade.get("cost_pct")
    pop        = trade.get("pop")
    price      = trade.get("price")
    ml         = trade.get("maxLoss")
    mp         = trade.get("maxProfit")
    ror        = trade.get("RoR")
    warns      = trade.get("warnings") or []

    delta_str  = f"δ{long_delta:.2f}" if isinstance(long_delta, float) else ""
    itm_str    = f"${itm_amount:.2f} ITM" if isinstance(itm_amount, float) else ""
    itm_label  = f" ({delta_str}, {itm_str})" if (delta_str or itm_str) else ""
    short_itm  = trade.get("short_itm")
    short_label = f" (${short_itm:.2f} ITM)" if isinstance(short_itm, float) else ""
    cost_ok   = isinstance(cost_pct, float) and cost_pct <= 70
    cost_str  = f"{cost_pct:.0f}% {'✅' if cost_ok else '⚠️'}" if isinstance(cost_pct, float) else ""

    # Profit targets for best trade
    dollar_risked = (price or 0) * 100 * contracts
    pt_lines = []
    if isinstance(price, float) and isinstance(ml, float) and dollar_risked > 0:
        t25 = round(price + ml * 0.25, 2)
        t35 = round(price + ml * 0.35, 2)
        t50 = round(price + ml * 0.50, 2)
        g25 = round(dollar_risked * 0.25, 0)
        g35 = round(dollar_risked * 0.35, 0)
        g50 = round(dollar_risked * 0.50, 0)
        pt_lines = [
            "📊 Profit Targets (off $ risked):",
            f"  Same Day  → 25%: +${g25:.0f} (sell at {t25:.2f})",
            f"  Next Day  → 35%: +${g35:.0f} (sell at {t35:.2f})",
            f"  Deep ITM  → 50%: +${g50:.0f} (sell at {t50:.2f})",
        ]

    lines = [
        f"🧠 Trade: {stype} {side} SPREAD",
        f"Long: {best_long}{itm_label} | Short: {best_short}{short_label}",
        f"Width: ${trade.get('width','?')} | Cost: ${price:.2f} ({cost_str})" if isinstance(price, float) else "",
        f"Max Profit: ${mp:.2f} | Max Loss: ${ml:.2f}" if isinstance(mp, float) else "",
        f"POP: {pop:.0%}" if isinstance(pop, float) else "",
        f"Confidence: {conf}/100 ({', '.join(conf_r[:2])})" if conf_r else f"Confidence: {conf}/100",
        f"Size: {contracts} contract(s) | ${dollar_r:.0f} risk",
        "",
        *pt_lines,
    ]

    if warns:
        lines.append("⚠️ " + "; ".join(str(w) for w in warns[:3]))

    # Multi-strike ladder
    if show_ladder:
        lines.append("")
        lines.append("📐 Width Ladder:")
        lines.append("─────────────────")
        for c in ladder:
            w      = c.get("width")
            cp     = c.get("price")
            cr     = c.get("RoR")
            cpop   = c.get("pop")
            clong  = c.get("long")
            cshort = c.get("short")
            cml    = c.get("maxLoss")
            cpct   = round(cp / w * 100, 0) if w and cp else None

            is_best = (clong == best_long and cshort == best_short)
            star    = " ⭐ BEST" if is_best else ""

            pct_str  = f"{cpct:.0f}% {'✅' if cpct and cpct <= 70 else '⚠️'}" if cpct else ""
            ror_str  = f"{cr:.2f}" if isinstance(cr, float) else "—"
            pop_str  = f"{cpop:.0%}" if isinstance(cpop, float) else "—"

            # Per-width profit targets
            w_risked = (cp or 0) * 100 * contracts
            pt25 = round(w_risked * 0.25, 0) if w_risked else 0
            pt35 = round(w_risked * 0.35, 0) if w_risked else 0
            sell25 = round((cp or 0) + (cml or 0) * 0.25, 2)
            sell35 = round((cp or 0) + (cml or 0) * 0.35, 2)

            lines.append(
                f"${w:.0f} wide | ${cp:.2f} ({pct_str}) | "
                f"RoR {ror_str} | POP {pop_str}{star}"
            )
            lines.append(
                f"  {clong} / {cshort} | "
                f"PT25: +${pt25:.0f} | PT35: +${pt35:.0f} "
                f"(sell {sell25:.2f} / {sell35:.2f})"
            )
        lines.append("─────────────────")

    return [l for l in lines if l is not None]

def scan_watchlist_internal(tickers: list, max_posts: int = 6):
    """
    Internal scan runner — same logic as /scan route
    but callable directly from Telegram commands.
    """
    if is_paused():
        post_to_telegram("⏸ Scan skipped — bot is paused.")
        return

    results  = []
    posted   = 0

    bull_no_trade    = []
    bear_no_trade    = []
    neutral_skipped  = []
    suppressed       = []
    duplicates       = []
    earnings_flagged = []

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {executor.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            if posted >= max_posts:
                future.cancel()
                continue
            res = future.result()
            results.append(res)

            ticker  = res.get("ticker", "?")
            skipped = res.get("skipped", "")
            error   = res.get("error", "")

            if res.get("posted"):
                posted += 1
            elif "duplicate" in skipped.lower():
                duplicates.append(ticker)
            elif "not trade-worthy" in skipped.lower():
                direction = res.get("direction", "neutral")
                if direction == "bull":
                    bull_no_trade.append(ticker)
                elif direction == "bear":
                    bear_no_trade.append(ticker)
                else:
                    neutral_skipped.append(ticker)
            elif "suppressed" in skipped.lower() or "confidence" in error.lower():
                suppressed.append(ticker)
            elif error:
                neutral_skipped.append(ticker)

            if res.get("has_earnings"):
                earnings_flagged.append(ticker)

    # Summary message
    summary_lines = [
        f"📋 WATCHLIST SUMMARY — {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        f"Scanned: {len(tickers)} tickers | Trade cards sent: {posted}",
        "",
    ]
    if earnings_flagged:
        summary_lines.append(f"🚨 EARNINGS THIS WEEK: {', '.join(sorted(earnings_flagged))}")
    if bull_no_trade:
        summary_lines.append(f"🟢 BULL (no setup): {', '.join(sorted(bull_no_trade))}")
    if bear_no_trade:
        summary_lines.append(f"🔴 BEAR (no setup): {', '.join(sorted(bear_no_trade))}")
    if neutral_skipped:
        summary_lines.append(f"⚪ NEUTRAL / ERROR: {', '.join(sorted(neutral_skipped))}")
    if suppressed:
        summary_lines.append(f"🟡 LOW CONFIDENCE: {', '.join(sorted(suppressed))}")
    if duplicates:
        summary_lines.append(f"🔁 DUPLICATE (skipped): {', '.join(sorted(duplicates))}")

    summary_lines += ["", "— Not financial advice —"]
    post_to_telegram("\n".join(summary_lines))
    set_last_scan(posted, len(tickers))
    
# ─────────────────────────────────────────────────────────
# SINGLE-TICKER SCAN WORKER  (called in parallel)
# ─────────────────────────────────────────────────────────

def scan_ticker(ticker: str, force_direction: str = None) -> dict:
    """
    Full pipeline for a single ticker.
    Returns a result dict with 'posted', 'skipped', 'error' keys.
    """
    try:
        spot             = get_spot(ticker)

        # Enrich with Finnhub data (IV rank + earnings check)
        enrichment   = enrich_ticker(ticker)
        iv_rank      = enrichment.get("iv_rank")
        has_earnings = enrichment.get("has_earnings", False)
        earnings_warn = enrichment.get("earnings_warn")

        exp, contracts   = get_options_chain(ticker, max_dte=SCAN_MAX_DTE)
        exp_dt           = datetime.fromisoformat(exp).date()
        dte              = max((exp_dt - datetime.now(timezone.utc).date()).days, 0)

        call_wall, put_wall, net_gex, inc = compute_walls_and_gex(spot, contracts)
        oi_score, oi_note                 = oi_change_score(ticker, exp, contracts)

        # IV
        atm_iv = atm_iv_from_contracts(contracts, spot)
        emove  = spot * atm_iv * math.sqrt(max(EXPECTED_MOVE_DTE, 1) / 365.0)

        # Compute IV rank + HV20 using MarketData candles
        from data_providers import get_iv_rank_from_candles
        iv_rank, iv_pct, hv20 = get_iv_rank_from_candles(ticker, atm_iv)
        
        # Skew
        md_payload = {
            "strike":        [c.get("strike")       for c in contracts],
            "side":          [c.get("right")         for c in contracts],
            "bid":           [c.get("bid")           for c in contracts],
            "ask":           [c.get("ask")           for c in contracts],
            "mid":           [c.get("mid")           for c in contracts],
            "openInterest":  [c.get("openInterest")  for c in contracts],
            "iv":            [c.get("iv")            for c in contracts],
            "delta":         [c.get("delta")         for c in contracts],
            "dte":           [dte for _ in contracts],
            "_call_wall":    call_wall,
            "_put_wall":     put_wall,
        }

        call_iv_atm = pick_atm_iv(md_payload, spot, "call")
        put_iv_atm  = pick_atm_iv(md_payload, spot, "put")
        skew_val, skew_bias = skew_score(call_iv_atm, put_iv_atm)

        # Direction (multi-factor)
        from options_engine import compute_direction_bias
        direction, dir_conf, dir_notes = compute_direction_bias(
            spot, call_wall, put_wall, net_gex, skew_bias, iv_rank
        )

        # Override direction if forced by TV alert
        if force_direction and force_direction in ("bull", "bear"):
            direction = force_direction
            dir_conf  = max(dir_conf, 60)  # boost confidence since TV signal confirmed
            log.info(f"{ticker}: direction forced to {direction} by TV alert")

        # Risk / alpha
        risk_label, risk_notes = risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score)
        a_score = alpha_score(spot, call_wall, put_wall, net_gex, oi_score, risk_label, emove, inc)

        # Trade-worthy filter (use confidence as primary gate)
        near_wall   = call_wall and put_wall and (min(abs(call_wall - spot), abs(spot - put_wall)) <= 2 * inc)
        big_oi      = oi_score > 15000
        notable_gex = abs(net_gex) > 1e9

        if not (near_wall or big_oi or notable_gex or dir_conf >= 55):
            return {
                "ticker":    ticker,
                "skipped":   "not trade-worthy",
                "posted":    False,
                "direction": direction,
            }

        # Engine
        rec = recommend_from_marketdata(
            marketdata_json = md_payload,
            direction       = direction,
            dte             = dte,
            spot            = spot,
            net_gex         = net_gex,
            iv_rank         = iv_rank,
            prefer          = "debit",
            account_size    = ACCOUNT_SIZE,
            max_risk_pct    = MAX_RISK_PCT,
            max_risk_usd    = MAX_RISK_USD,
            min_confidence  = MIN_CONFIDENCE_TO_POST,
            )

        # Dedup check
        if rec.get("ok"):
            trade = rec.get("trade") or {}
            if is_duplicate_trade(ticker, direction, trade.get("short"), trade.get("long")):
                side      = (trade.get("side") or "").upper()
                short_k   = trade.get("short")
                long_k    = trade.get("long")
                stype     = (trade.get("type") or "debit").upper()
                dup_label = f"{ticker}: {stype} {direction.upper()} {side} {long_k}/{short_k}"
                return {
                    "ticker":     ticker,
                    "skipped":    "duplicate trade in TTL window",
                    "posted":     False,
                    "dup_detail": dup_label,
                }

       # Only post a card if the engine found a valid trade
        if not rec.get("ok"):
            reason = rec.get("reason", "no valid trade")
            return {
                "ticker":    ticker,
                "posted":    False,
                "skipped":   f"suppressed — {reason}",
                "direction": direction,
            }

        trade_lines = build_trade_lines(rec, ticker)

        # Add volume/flow signal
        if rec.get("ok"):
            flow_lines = compute_flow_signal(contracts, rec.get("trade") or {}, spot)
            if flow_lines:
                trade_lines = trade_lines + [""] + flow_lines

        # Prepend earnings warning to trade lines if applicable
        if has_earnings and earnings_warn:
            trade_lines = [earnings_warn, ""] + trade_lines

        msg = build_scan_message(
            ticker     = ticker,
            spot       = spot,
            exp        = exp,
            dte        = dte,
            call_wall  = call_wall,
            put_wall   = put_wall,
            net_gex    = net_gex,
            emove      = emove,
            oi_note    = oi_note,
            risk_label = risk_label,
            risk_notes = risk_notes,
            a_score    = a_score,
            direction  = direction,
            iv_rank    = iv_rank,
            skew_bias  = skew_bias,
            skew_val   = skew_val,
            trade_lines= trade_lines,
            contracts  = contracts,
            hv20       = hv20,
            atm_iv     = atm_iv,
        )

        st, body = post_to_telegram(msg)
        if st == 200:
            trade = rec.get("trade") or {}
            mark_trade_sent(ticker, direction, trade.get("short"), trade.get("long"))

        return {"ticker": ticker, "posted": st == 200, "tg_status": st, "tg_body": body[:120]}


    except Exception as e:
        log.error(f"scan_ticker({ticker}): {type(e).__name__}: {e}")
        return {"ticker": ticker, "posted": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


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
        "ACCOUNT_SIZE":    ACCOUNT_SIZE,
        "MAX_RISK_PCT":    MAX_RISK_PCT,
        "MAX_RISK_USD":    MAX_RISK_USD,
        "SCAN_WORKERS":    SCAN_WORKERS,
        "MIN_CONFIDENCE":  MIN_CONFIDENCE_TO_POST,
        "DEDUP_TTL_S":     DEDUP_TTL_SECONDS,
    })


@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram("✅ Telegram test OK")
    return jsonify({"status": st, "body": body})


@app.route("/exp_debug/<ticker>", methods=["GET"])
def exp_debug(ticker):
    try:
        ticker = ticker.strip().upper()
        exps   = get_expirations(ticker)
        today  = datetime.now(timezone.utc).date()
        rows   = []
        for e in exps[:50]:
            try:
                d = max((datetime.fromisoformat(e).date() - today).days, 0)
            except Exception:
                d = None
            rows.append({"exp": e, "dte": d})
        return jsonify({"ticker": ticker, "count": len(exps), "first_50": rows})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 200


# TradingView webhook — autonomous + TV-triggered running in parallel
@app.route("/tv", methods=["POST"])
def tv_webhook():
    data    = request.get_json(silent=True) or {}
    raw     = (request.get_data(as_text=True) or "").strip()

    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    ticker   = (data.get("ticker") or "").strip().upper()
    close    = as_float(data.get("close"),    0.0)
    tv_time  = (data.get("time") or "").strip()
    bias     = (data.get("bias") or "bull").strip().lower()

    # Enriched indicator values from Pine Script
    rsi       = as_float(data.get("rsi"),      None)
    vwap      = as_float(data.get("vwap"),     None)
    ema_fast  = as_float(data.get("ema_fast"), None)
    ema_slow  = as_float(data.get("ema_slow"), None)
    vol_ratio = as_float(data.get("vol_ratio"),None)
    high      = as_float(data.get("high"),     None)
    low       = as_float(data.get("low"),      None)
    volume    = as_float(data.get("volume"),   None)

    if not ticker:
        st, body = post_to_telegram("📢 TV signal received (no ticker)")
        return jsonify({"status": "received_raw", "tg_status": st})

    log.info(f"TV signal: {ticker} close={close} bias={bias} rsi={rsi}")

    # Build indicator context string for Telegram
    indicator_lines = []
    if rsi is not None:
        rsi_note = "oversold 🟢" if rsi < 35 else "overbought 🔴" if rsi > 65 else "neutral"
        indicator_lines.append(f"RSI: {rsi:.1f} ({rsi_note})")
    if vwap is not None and close > 0:
        vwap_note = "above VWAP 🟢" if close > vwap else "below VWAP 🔴"
        indicator_lines.append(f"VWAP: {vwap:.2f} — {vwap_note}")
    if ema_fast is not None and ema_slow is not None:
        ema_note = "bullish cross 🟢" if ema_fast > ema_slow else "bearish cross 🔴"
        indicator_lines.append(f"EMA 9/21: {ema_fast:.2f} / {ema_slow:.2f} — {ema_note}")
    if vol_ratio is not None:
        vol_note = "🔥 spike" if vol_ratio >= 2.0 else "elevated" if vol_ratio >= 1.5 else "normal"
        indicator_lines.append(f"Volume: {vol_ratio:.1f}x avg ({vol_note})")

    # Signal strength score (0-4 indicators aligned)
    aligned = 0
    if rsi is not None:
        aligned += 1 if (bias == "bull" and rsi > 50) or (bias == "bear" and rsi < 50) else 0
    if vwap is not None and close > 0:
        aligned += 1 if (bias == "bull" and close > vwap) or (bias == "bear" and close < vwap) else 0
    if ema_fast is not None and ema_slow is not None:
        aligned += 1 if (bias == "bull" and ema_fast > ema_slow) or (bias == "bear" and ema_fast < ema_slow) else 0
    if vol_ratio is not None:
        aligned += 1 if vol_ratio >= 1.5 else 0

    max_aligned   = sum([rsi is not None, vwap is not None,
                         ema_fast is not None, vol_ratio is not None])
    strength_pct  = int(aligned / max_aligned * 100) if max_aligned > 0 else 75
    # If no indicators sent (Marcipher alert), assume strong signal
    # since Marcipher dual-condition is already high quality
    if max_aligned == 0:
       # Marcipher alert — no indicator data but high quality signal
        strength_label = "💪 Strong (Marcipher dual-condition)"
    elif strength_pct >= 75:
        strength_label = "💪 Strong"
    elif strength_pct >= 50:
        strength_label = "👍 Moderate"
    else:
        strength_label = "⚠️ Weak — indicators mixed"

    # Build TV signal context message
    prefix_lines = [
        f"📢 TV Signal — {ticker} ({bias.upper()})",
        f"Close: {close:.2f} | Time: {tv_time}" if tv_time else f"Close: {close:.2f}",
        f"Signal Strength: {strength_label} ({aligned}/{max_aligned} aligned)",
        "",
        *indicator_lines,
        "",
    ]
    prefix = "\n".join(prefix_lines)

    # Run everything in background — respond to TradingView immediately
    def run_tv_scan():
        try:
            post_to_telegram(prefix)
            scan_ticker(ticker, force_direction=bias)
        except Exception as e:
            log.error(f"TV scan error for {ticker}: {e}")

    threading.Thread(target=run_tv_scan, daemon=True).start()

    # Return immediately to TradingView (within 3 seconds)
    return jsonify({
        "status":  "accepted",
        "ticker":  ticker,
        "bias":    bias,
        "aligned": f"{aligned}/{max_aligned}",
    }), 200

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
        return jsonify({"status": "paused", "message": "Bot is paused — use /resume in Telegram"})

    max_posts = as_int(data.get("max_posts"), DEFAULT_MAX_POSTS)
    results   = []
    posted    = 0

    # Buckets for summary message
    bull_no_trade   = []
    bear_no_trade   = []
    neutral_skipped = []
    suppressed      = []
    duplicates      = []
    earnings_flagged = []

    log.info(f"Scan started: {len(tickers)} tickers, {SCAN_WORKERS} workers")

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {executor.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            res = future.result()
            results.append(res)

            ticker  = res.get("ticker", "?")
            skipped = res.get("skipped", "")
            error   = res.get("error", "")

            if res.get("posted"):
                posted += 1

            elif "duplicate" in skipped.lower():
                dup_detail = res.get("dup_detail", ticker)
                duplicates.append(dup_detail)

            elif "not trade-worthy" in skipped.lower():
                # Still want to show direction
                direction = res.get("direction", "neutral")
                if direction == "bull":
                    bull_no_trade.append(ticker)
                elif direction == "bear":
                    bear_no_trade.append(ticker)
                else:
                    neutral_skipped.append(ticker)

            elif "suppressed" in skipped.lower() or "confidence" in error.lower():
                suppressed.append(ticker)

            elif error:
                neutral_skipped.append(ticker)
            # Track earnings flags
            if res.get("has_earnings"):
                earnings_flagged.append(ticker)
                
    # Build and send summary message
    summary_lines = [
        f"📋 WATCHLIST SUMMARY — {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        f"Scanned: {len(tickers)} tickers | Trade cards sent: {posted}",
        "",
    ]

    if earnings_flagged:
        summary_lines.append(f"🚨 EARNINGS THIS WEEK: {', '.join(sorted(earnings_flagged))}")

    if bull_no_trade:
        summary_lines.append(f"🟢 BULL (no setup): {', '.join(sorted(bull_no_trade))}")
    if bear_no_trade:
        summary_lines.append(f"🔴 BEAR (no setup): {', '.join(sorted(bear_no_trade))}")
    if neutral_skipped:
        summary_lines.append(f"⚪ NEUTRAL / ERROR: {', '.join(sorted(neutral_skipped))}")
    if suppressed:
        summary_lines.append(f"🟡 LOW CONFIDENCE: {', '.join(sorted(suppressed))}")
    if duplicates:
        summary_lines.append(f"🔁 DUPLICATE (skipped): {', '.join(sorted(duplicates))}")
    if earnings_flagged:
        summary_lines.append(f"🚨 EARNINGS THIS WEEK: {', '.join(sorted(earnings_flagged))}")
        
    summary_lines += ["", "— Not financial advice —"]
    summary_text = "\n".join(summary_lines)

    st, body = post_to_telegram(summary_text)
    log.info(f"Scan complete: {posted}/{len(tickers)} trade cards posted")
    set_last_scan(posted, len(tickers))
    log.info(f"Scan complete: {posted}/{len(tickers)} posted")

    return jsonify({
        "status":  "ok",
        "posted":  posted,
        "tickers": len(tickers),
        "results": results,
    })

# Register Telegram webhook on startup
with app.app_context():
    _tg_ws = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    _bot_url = BOT_URL
    if _tg_ws and _bot_url:
        register_webhook(_bot_url, _tg_ws)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
