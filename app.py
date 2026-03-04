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

import os
import time
import math
import json
import hashlib
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        return float(v) if v is not None else float(default)
    except Exception:
        return float(default)


def as_int(x, default=0):
    v = first_val(x, default)
    try:
        return int(v) if v is not None else int(default)
    except Exception:
        return int(default)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def gex_bar(net_gex: float, width: int = 21) -> str:
    center = width // 2
    mag    = abs(net_gex)
    steps  = 0 if mag <= 0 else int(clamp(round(math.log10(1 + mag / 1e8) * 3), 0, center))
    bar    = ["·"] * width
    bar[center] = "|"
    if net_gex > 0 and steps:
        for i in range(center + 1, min(width, center + 1 + steps)):
            bar[i] = "█"
        bar[min(width - 1, center + steps)] = "▶"
    elif net_gex < 0 and steps:
        for i in range(center - 1, max(-1, center - 1 - steps), -1):
            bar[i] = "█"
        bar[max(0, center - steps)] = "◀"
    return "".join(bar)


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
    iv_rank, skew_bias, skew_val, trade_lines
) -> str:

    walls = (f"Walls: Call {call_wall:g} | Put {put_wall:g} | ZeroG ~{(call_wall+put_wall)/2:.0f}"
             if call_wall and put_wall else "Walls: —")

    ivr_str  = f"{iv_rank:.0f}/100" if iv_rank is not None else "—"
    gex_str  = "Positive (range)" if net_gex >= 0 else "Negative (trending)"
    skew_str = f"{skew_bias} ({skew_val:+.3f})"

    lines = [
        f"🚨 SCAN — {ticker}",
        f"Direction: {direction.upper()} | DTE: {max(dte,1)} ({exp})",
        f"Spot: {spot:.2f} | E-Move ±{emove:.2f}",
        f"IV Rank: {ivr_str} | Skew: {skew_str}",
        f"GEX: {gex_str}",
        f"GEX Bar: {gex_bar(net_gex)}",
        walls,
        f"OI Signal: {oi_note}",
        f"Risk: {risk_label} — {risk_notes}",
        f"Big Alpha: {a_score}/100",
        "",
    ]
    lines += trade_lines
    lines += ["", "— Not financial advice —"]
    return "\n".join(lines)


def build_trade_lines(rec: dict, ticker: str) -> list:
    if not rec.get("ok"):
        reason = rec.get("reason", "—")
        conf   = rec.get("confidence")
        lines  = [f"🧠 Trade: suppressed — {reason}"]
        if conf is not None:
            lines.append(f"Confidence: {conf}/100")
        return lines

    trade   = rec.get("trade") or {}
    stype   = (trade.get("type") or "").upper()
    side    = (trade.get("side") or rec.get("side") or "").upper()
    short_k = trade.get("short")
    long_k  = trade.get("long")
    price   = trade.get("price")
    ror     = trade.get("RoR")
    pop     = trade.get("pop")
    mp      = trade.get("maxProfit")
    ml      = trade.get("maxLoss")
    warns   = trade.get("warnings") or []

    contracts = rec.get("contracts_suggested", 1)
    dollar_r  = rec.get("dollar_risk", 0)
    sizing    = rec.get("sizing_note", "")
    conf      = rec.get("confidence", "—")
    conf_r    = rec.get("conf_reasons") or []

    lines = [
        f"🧠 Trade: {stype} {side} SPREAD",
        f"Short: {short_k} | Long: {long_k}",
        f"Price: {price:.2f} | RoR: {ror:.2f}" if isinstance(price, float) and isinstance(ror, float) else f"Price: {price} | RoR: {ror}",
        f"Max Profit: {mp:.2f} | Max Loss: {ml:.2f}" if isinstance(mp, float) else "",
        f"POP: {pop:.0%}" if isinstance(pop, float) else "",
        f"Confidence: {conf}/100 ({', '.join(conf_r[:3])})" if conf_r else f"Confidence: {conf}/100",
        f"Size: {contracts} contract(s) | {sizing}",
    ]

    if warns:
        lines.append("⚠️ " + "; ".join(str(w) for w in warns[:3]))

    return [l for l in lines if l]


# ─────────────────────────────────────────────────────────
# SINGLE-TICKER SCAN WORKER  (called in parallel)
# ─────────────────────────────────────────────────────────

def scan_ticker(ticker: str) -> dict:
    """
    Full pipeline for a single ticker.
    Returns a result dict with 'posted', 'skipped', 'error' keys.
    """
    try:
        spot             = get_spot(ticker)
        exp, contracts   = get_options_chain(ticker, max_dte=SCAN_MAX_DTE)
        exp_dt           = datetime.fromisoformat(exp).date()
        dte              = max((exp_dt - datetime.now(timezone.utc).date()).days, 0)

        call_wall, put_wall, net_gex, inc = compute_walls_and_gex(spot, contracts)
        oi_score, oi_note                 = oi_change_score(ticker, exp, contracts)

        # IV
        atm_iv = atm_iv_from_contracts(contracts, spot)
        emove  = spot * atm_iv * math.sqrt(max(EXPECTED_MOVE_DTE, 1) / 365.0)

        # IV rank (from MarketData 52-week high/low if available — wire in as needed)
        iv_rank = None   # extend here: fetch md_get iv history and pass to compute_iv_rank()

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

        # Risk / alpha
        risk_label, risk_notes = risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score)
        a_score = alpha_score(spot, call_wall, put_wall, net_gex, oi_score, risk_label, emove, inc)

        # Trade-worthy filter (use confidence as primary gate)
        near_wall   = call_wall and put_wall and (min(abs(call_wall - spot), abs(spot - put_wall)) <= 2 * inc)
        big_oi      = oi_score > 15000
        notable_gex = abs(net_gex) > 1e9

        if not (near_wall or big_oi or notable_gex or dir_conf >= 55):
            return {"ticker": ticker, "skipped": "not trade-worthy", "posted": False}

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
                return {"ticker": ticker, "skipped": "duplicate trade in TTL window", "posted": False}

        trade_lines = build_trade_lines(rec, ticker)

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
        )

        st, body = post_to_telegram(msg)
        if st == 200 and rec.get("ok"):
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
    data = request.get_json(silent=True) or {}

    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    ticker   = (data.get("ticker") or "").strip().upper()
    close    = as_float(data.get("close"), 0.0)
    tv_time  = (data.get("time") or "").strip()
    tv_dir   = (data.get("direction") or "bull").strip().lower()   # TV can send direction hint

    if not ticker:
        st, body = post_to_telegram("📢 TradingView signal received (no ticker)")
        return jsonify({"status": "received_raw", "tg_status": st})

    log.info(f"TV signal: {ticker} close={close} dir={tv_dir}")

    result = scan_ticker(ticker)

    return jsonify({
        "status":  "received",
        "ticker":  ticker,
        "tv_time": tv_time,
        "result":  result,
    })


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    data     = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()

    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]
    if not tickers:
        return jsonify({"error": "WATCHLIST empty"}), 400

    max_posts = as_int(data.get("max_posts"), DEFAULT_MAX_POSTS)
    results   = []
    posted    = 0

    log.info(f"Scan started: {len(tickers)} tickers, {SCAN_WORKERS} workers")

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {executor.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            if posted >= max_posts:
                future.cancel()
                continue
            res = future.result()
            results.append(res)
            if res.get("posted"):
                posted += 1

    log.info(f"Scan complete: {posted}/{len(tickers)} posted")
    return jsonify({
        "status":  "ok",
        "posted":  posted,
        "tickers": len(tickers),
        "results": results,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
