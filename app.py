# app.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.

from options_engine import recommend_from_marketdata

import os
import time
import math
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ----------------------------
# TELEGRAM (Render ENV VARS)
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ----------------------------
# ENV VARS (set in Render)
# ----------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()  # optional, unused by default
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "").strip()

MARKETDATA_TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
WATCHLIST = os.getenv("WATCHLIST", "").strip()
SCAN_SECRET = os.getenv("SCAN_SECRET", "").strip()

BOT_URL = os.getenv("BOT_URL", "").strip()

# User prefs / rules
MAX_SPREAD_WIDTH = float(os.getenv("MAX_SPREAD_WIDTH", "5") or 5)               # $5 max width
MAX_DEBIT_PCT_WIDTH = float(os.getenv("MAX_DEBIT_PCT_WIDTH", "0.70") or 0.70)   # debit <= 70% width
LIQ_WARN_MIN_OI = int(os.getenv("LIQ_WARN_MIN_OI", "500") or 500)               # warn if OI < 500
LIQ_WARN_BA = float(os.getenv("LIQ_WARN_BA", "0.30") or 0.30)                   # warn if bid/ask > .30
SCAN_MAX_DTE = int(os.getenv("SCAN_MAX_DTE", "7") or 7)                         # scan out to 7 DTE
DEFAULT_MAX_POSTS_PER_SCAN = int(os.getenv("MAX_POSTS_PER_SCAN", "6") or 6)     # cap messages per scan

# In-memory snapshots (Render can restart & forget these)
prev_oi_snapshot = {}  # key: (ticker, exp, right, strike) -> oi


# ----------------------------
# HELPERS
# ----------------------------
def first_val(x, default=None):
    """MarketData sometimes returns [value]. Unwrap lists safely."""
    if x is None:
        return default
    if isinstance(x, list):
        return x[0] if x else default
    return x


def as_float(x, default=0.0):
    v = first_val(x, default)
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return float(default)


def as_int(x, default=0):
    v = first_val(x, default)
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return int(default)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ----------------------------
# TELEGRAM SENDER (FIXED)
# - returns (status_code, body_text)
# - NO parse_mode to avoid Markdown failures
# ----------------------------
def post_to_telegram(text: str, max_retries: int = 4):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return 400, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return 200, ""
            last_err = r.text[:300] if r.text else f"HTTP {r.status_code}"
            time.sleep(min(1.5 * (attempt + 1), 6.0))
        except Exception as e:
            last_err = str(e)
            time.sleep(min(1.5 * (attempt + 1), 6.0))

    return 500, f"Telegram post failed after retries: {last_err}"


# ----------------------------
# (Optional) DISCORD webhook (kept for reference)
# ----------------------------
def post_to_discord(payload, max_retries=5):
    if not DISCORD_WEBHOOK_URL:
        return 400, "DISCORD_WEBHOOK_URL not set"

    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)

            if r.status_code in (200, 204):
                return r.status_code, ""

            if r.status_code == 429:
                try:
                    retry_after = as_float(r.json().get("retry_after"), 2.0)
                except Exception:
                    retry_after = 2.0

                sleep_s = retry_after + min(2.0 * attempt, 6.0)
                time.sleep(sleep_s)
                last_err = f"429 rate limited; slept {sleep_s:.2f}s"
                continue

            last_err = (r.text[:300] if r.text else f"HTTP {r.status_code}")
            time.sleep(min(1.5 * (attempt + 1), 6.0))

        except Exception as e:
            last_err = str(e)
            time.sleep(min(1.5 * (attempt + 1), 6.0))

    return 500, f"Discord post failed after retries: {last_err}"


# ----------------------------
# MARKETDATA
# ----------------------------
def md_headers():
    return {"Authorization": f"Bearer {MARKETDATA_TOKEN}"}


def md_get(url, params=None):
    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")

    r = requests.get(url, headers=md_headers(), params=params or {}, timeout=25)
    r.raise_for_status()
    return r.json()


def get_spot(ticker: str) -> float:
    data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")

    last_ = as_float(data.get("last"), 0.0)
    if last_ > 0:
        return last_

    mid_ = as_float(data.get("mid"), 0.0)
    if mid_ > 0:
        return mid_

    bid_ = as_float(data.get("bid"), 0.0)
    if bid_ > 0:
        return bid_

    ask_ = as_float(data.get("ask"), 0.0)
    if ask_ > 0:
        return ask_

    raise RuntimeError(f"Could not parse spot quote for {ticker}")


def get_options_chain(ticker: str, max_dte: int = 7):
    """
    MarketData chain response is columnar arrays.
    Normalize into list[dict] and choose nearest expiration <= max_dte if possible.
    """
    data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/")

    if not isinstance(data, dict) or data.get("s") != "ok":
        raise RuntimeError(f"Bad options chain response for {ticker}: {str(data)[:180]}")

    sym_list = data.get("optionSymbol") or []
    if not isinstance(sym_list, list) or len(sym_list) == 0:
        raise RuntimeError("Unexpected chain format: optionSymbol missing/empty")

    n = len(sym_list)

    def col(name, default=None):
        v = data.get(name, default)
        if isinstance(v, list):
            return v
        return [default] * n

    optionSymbol = col("optionSymbol", "")
    side = col("side", "")
    strike = col("strike", None)
    expiration = col("expiration", None)  # epoch seconds
    dte = col("dte", None)

    openInterest = col("openInterest", 0)
    volume = col("volume", 0)
    iv = col("iv", None)
    gamma = col("gamma", None)
    delta = col("delta", None)
    bid = col("bid", None)
    ask = col("ask", None)
    mid = col("mid", None)

    contracts = []
    for i in range(n):
        exp_epoch = as_int(expiration[i], 0)
        exp_date = (
            datetime.fromtimestamp(exp_epoch, tz=timezone.utc).date().isoformat()
            if exp_epoch else None
        )

        right = (side[i] or "").lower().strip()
        if right in ("c", "call"):
            right = "call"
        elif right in ("p", "put"):
            right = "put"

        contracts.append({
            "optionSymbol": optionSymbol[i],
            "right": right,
            "strike": as_float(strike[i], None),
            "expiration": exp_date,
            "dte": as_int(dte[i], None),

            "openInterest": as_int(openInterest[i], 0),
            "volume": as_int(volume[i], 0),

            "iv": as_float(iv[i], None),
            "gamma": as_float(gamma[i], 0.0),
            "delta": as_float(delta[i], None),

            "bid": as_float(bid[i], None),
            "ask": as_float(ask[i], None),
            "mid": as_float(mid[i], None),
        })

    exp_map = {}
    for c in contracts:
        exp = c.get("expiration")
        if exp:
            exp_map.setdefault(exp, []).append(c)

    if not exp_map:
        raise RuntimeError("No expirations found in chain response")

    # pick closest by min dte
    exp_candidates = []
    for exp, rows in exp_map.items():
        dtes = [r.get("dte") for r in rows if isinstance(r.get("dte"), int)]
        min_d = min(dtes) if dtes else 999999
        exp_candidates.append((min_d, exp))

    exp_candidates.sort(key=lambda x: x[0])

    chosen_exp = None
    for min_d, exp in exp_candidates:
        if min_d <= max_dte:
            chosen_exp = exp
            break
    if chosen_exp is None:
        chosen_exp = exp_candidates[0][1]

    return chosen_exp, exp_map[chosen_exp]


# ----------------------------
# CORE METRICS
# ----------------------------
def strike_increment(strikes):
    strikes = sorted(set(float(x) for x in strikes if x is not None))
    if len(strikes) < 2:
        return 1.0
    diffs = [round(strikes[i + 1] - strikes[i], 4) for i in range(len(strikes) - 1)]
    diffs = [d for d in diffs if d > 0]
    return min(diffs) if diffs else 1.0


def expected_move_from_iv(spot: float, iv: float, dte: int) -> float:
    T = max(dte, 1) / 365.0
    return spot * iv * math.sqrt(T)


def compute_walls_and_gex(spot: float, contracts: list):
    call_oi = {}
    put_oi = {}
    strikes = []
    net_gex = 0.0

    for c in contracts:
        right = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), None)
        if strike is None:
            continue

        strikes.append(strike)
        oi = as_int(c.get("openInterest"), 0)
        gamma = as_float(c.get("gamma"), 0.0)

        if right == "call":
            call_oi[strike] = call_oi.get(strike, 0) + oi
            net_gex += oi * gamma * (spot ** 2) * 100.0
        elif right == "put":
            put_oi[strike] = put_oi.get(strike, 0) + oi
            net_gex -= oi * gamma * (spot ** 2) * 100.0

    call_wall = max(call_oi.items(), key=lambda kv: kv[1])[0] if call_oi else None
    put_wall = max(put_oi.items(), key=lambda kv: kv[1])[0] if put_oi else None
    inc = strike_increment(strikes) if strikes else 1.0

    return call_wall, put_wall, net_gex, inc


def oi_change_score(ticker: str, exp: str, contracts: list):
    global prev_oi_snapshot

    total_abs_change = 0
    biggest_abs = 0
    biggest_label = "—"

    for c in contracts:
        right = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), None)
        if strike is None:
            continue

        oi = as_int(c.get("openInterest"), 0)
        k = (ticker, exp, right, strike)
        prev = prev_oi_snapshot.get(k)

        if prev is not None:
            delta = oi - prev
            absd = abs(delta)
            total_abs_change += absd
            if absd > biggest_abs:
                biggest_abs = absd
                biggest_label = f"{right.upper()} {strike:g} ΔOI {delta:+d}"

        prev_oi_snapshot[k] = oi

    return total_abs_change, biggest_label


def risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score):
    if call_wall is None or put_wall is None:
        return "⚪ Unknown", "Walls not available"

    dist_call = abs(call_wall - spot)
    dist_put = abs(spot - put_wall)
    near_wall_dist = min(dist_call, dist_put)

    notes = []
    score = 0

    if net_gex >= 0:
        score += 1
        notes.append("Positive Gamma (range-favored)")
    else:
        score -= 1
        notes.append("Negative Gamma (trend/vol risk)")

    if near_wall_dist <= (2 * inc):
        score -= 1
        notes.append("Near a major wall")
    else:
        score += 1
        notes.append("Not hugging walls")

    if oi_score > 50000:
        score -= 1
        notes.append("Large OI shift")
    elif oi_score > 15000:
        notes.append("Moderate OI shift")
    else:
        score += 1
        notes.append("OI stable")

    if score >= 2:
        return "🟢 Low", " | ".join(notes)
    if score == 1:
        return "🟡 Medium", " | ".join(notes)
    return "🔴 High", " | ".join(notes)


# ----------------------------
# UPGRADE 3: BIG ALPHA
# ----------------------------
def big_alpha_score(spot, call_wall, put_wall, net_gex, oi_score, risk_label, emove, inc):
    score = 50

    if "Low" in risk_label:
        score += 12
    elif "Medium" in risk_label:
        score += 4
    elif "High" in risk_label:
        score -= 10

    score += 6 if net_gex >= 0 else -6

    if oi_score > 50000:
        score += 6
    elif oi_score > 15000:
        score += 3

    if call_wall is not None and put_wall is not None:
        near_wall = min(abs(call_wall - spot), abs(spot - put_wall))
        if near_wall <= (2 * inc):
            score += 8
        elif near_wall <= (5 * inc):
            score += 3

    if emove <= 0:
        score -= 10
    else:
        score += 2

    return int(clamp(score, 0, 100))


# ----------------------------
# GEX GRAPHIC (text bar)
# ----------------------------
def gex_graphic(net_gex: float, width: int = 21) -> str:
    if width < 11:
        width = 11
    center = width // 2

    mag = abs(net_gex)
    if mag <= 0:
        steps = 0
    else:
        scaled = math.log10(1.0 + (mag / 1e8))
        steps = int(clamp(round(scaled * 3.0), 0, center))

    bar = ["·"] * width
    bar[center] = "|"

    if net_gex > 0 and steps > 0:
        for i in range(center + 1, min(width, center + 1 + steps)):
            bar[i] = "█"
        bar[min(width - 1, center + steps)] = "▶"
    elif net_gex < 0 and steps > 0:
        for i in range(center - 1, max(-1, center - 1 - steps), -1):
            bar[i] = "█"
        bar[max(0, center - steps)] = "◀"

    return "".join(bar)


# ----------------------------
# Confidence / Zones
# ----------------------------
def strike_targets_and_confidence(spot, emove, call_wall, put_wall, net_gex, inc, oi_score):
    bull_upper = spot + emove
    bear_lower = spot - emove

    score = 50
    notes = []

    if net_gex >= 0:
        score += 10
        regime = "+Gamma / Range"
    else:
        score -= 10
        regime = "-Gamma / Trend"

    if call_wall and put_wall:
        near = min(abs(call_wall - spot), abs(spot - put_wall))
        if near <= 2 * inc:
            score -= 10
            notes.append("Near wall")
        else:
            score += 5
            notes.append("Not pinned")
    else:
        regime = regime + " (no walls)"

    if oi_score > 50000:
        score -= 15
        notes.append("Huge OI shift")
    elif oi_score > 15000:
        score -= 5
        notes.append("Moderate OI shift")
    else:
        score += 5
        notes.append("OI stable")

    score = int(clamp(score, 0, 100))

    return {
        "bull_upper": bull_upper,
        "bear_lower": bear_lower,
        "regime": regime,
        "confidence": score,
        "notes": ", ".join(notes) if notes else "—",
    }


# ----------------------------
# LIQUIDITY WARNINGS (Upgrade 2)
# ----------------------------
def find_contract(contracts, right, strike):
    right = (right or "").lower()
    if right in ("c", "call"):
        right = "call"
    if right in ("p", "put"):
        right = "put"

    s = as_float(strike, None)
    if s is None:
        return None

    for c in contracts:
        if (c.get("right") or "").lower() != right:
            continue
        cs = as_float(c.get("strike"), None)
        if cs is None:
            continue
        if abs(cs - s) < 1e-6:
            return c
    return None


def liquidity_warnings_for_trade(contracts, trade):
    warnings = []

    ttype = (trade.get("type") or "").lower()
    right_guess = None
    if "call" in ttype:
        right_guess = "call"
    elif "put" in ttype:
        right_guess = "put"

    short_k = trade.get("short")
    long_k = trade.get("long")

    c_short = find_contract(contracts, right_guess, short_k) if right_guess else None
    c_long = find_contract(contracts, right_guess, long_k) if right_guess else None

    if (c_short is None or c_long is None) and (short_k is not None and long_k is not None):
        for rg in ("call", "put"):
            cs = find_contract(contracts, rg, short_k)
            cl = find_contract(contracts, rg, long_k)
            if cs and cl:
                c_short, c_long = cs, cl
                break

    def leg_warn(label, c):
        if not c:
            warnings.append(f"{label}: contract not found (check strikes)")
            return
        oi = as_int(c.get("openInterest"), 0)
        bid = as_float(c.get("bid"), None)
        ask = as_float(c.get("ask"), None)
        if oi < LIQ_WARN_MIN_OI:
            warnings.append(f"Low OI: {label} OI {oi} < {LIQ_WARN_MIN_OI}")
        if bid is not None and ask is not None and (ask - bid) > LIQ_WARN_BA:
            warnings.append(f"Wide bid/ask: {label} ({bid:.2f}/{ask:.2f}) spread {(ask-bid):.2f} > {LIQ_WARN_BA:.2f}")

    leg_warn("SHORT", c_short)
    leg_warn("LONG", c_long)

    return warnings


# ----------------------------
# MESSAGE BUILDERS (FIXED: extra_lines passed in)
# ----------------------------
def build_scan_message(
    ticker, spot, exp, dte, call_wall, put_wall, net_gex, emove,
    oi_note, risk_label, risk_notes, alpha_score, direction, extra_lines
):
    gex_regime = "Positive Gamma / Range Favored" if net_gex >= 0 else "Negative Gamma / Trend Favored"
    gex_bar = gex_graphic(net_gex)

    if call_wall is not None and put_wall is not None:
        zero_g = f"{(call_wall + put_wall) / 2:.0f}"
        walls_line = f"Walls: Call {call_wall:g} | Put {put_wall:g} | ZeroG {zero_g} (est.)"
    else:
        walls_line = "Walls: —"

    lines = [
        f"🚨 WATCHLIST SCAN — {ticker}",
        f"Exp: {exp} | DTE: {max(dte,1)}",
        f"Spot: {spot:.2f}",
        f"Auto Direction: {direction.upper()}",
        f"Expected Move ({max(dte,1)}D): ±{emove:.2f}",
        *extra_lines,
        walls_line,
        f"GEX: {gex_regime}",
        f"GEX Bar: {gex_bar}",
        f"OI Signal: {oi_note}",
        f"Risk: {risk_label}",
        f"Notes: {risk_notes}",
        f"Big Alpha: {alpha_score}/100",
        "",
        "— Not financial advice —",
    ]
    return "\n".join(lines)


def build_trade_engine_text(ticker, direction, trade, warnings_extra):
    ttype = (trade.get("type") or "").upper()
    short_k = trade.get("short")
    long_k = trade.get("long")
    width = trade.get("width")
    price = trade.get("price")
    mp = trade.get("maxProfit")
    ml = trade.get("maxLoss")
    ror = trade.get("RoR")

    warnings = []
    if isinstance(trade.get("warnings"), list):
        warnings.extend([str(x) for x in trade["warnings"] if x])
    if warnings_extra:
        warnings.extend([str(x) for x in warnings_extra if x])

    warn_line = ", ".join(warnings) if warnings else "None"

    lines = [
        f"🧠 TRADE ENGINE — {ticker}",
        f"Auto Direction: {direction.upper()}",
        "",
        f"Suggested: {ttype} SPREAD",
        f"Short Strike: {short_k}",
        f"Long Strike:  {long_k}",
        f"Width: {width}",
        f"Price: {price:.2f}" if isinstance(price, (int, float)) else f"Price: {price}",
        "",
        f"Max Profit: {mp:.2f}" if isinstance(mp, (int, float)) else f"Max Profit: {mp}",
        f"Max Loss:   {ml:.2f}" if isinstance(ml, (int, float)) else f"Max Loss:   {ml}",
        f"Return on Risk: {ror:.2f}" if isinstance(ror, (int, float)) else f"Return on Risk: {ror}",
        "",
        f"Warnings: {warn_line}",
        "",
        "— Not financial advice —",
    ]
    return "\n".join(lines)


# ----------------------------
# ROUTES
# ----------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/debug", methods=["GET"])
def debug():
    return jsonify({
        "TELEGRAM_BOT_TOKEN_set": bool(TELEGRAM_BOT_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "MARKETDATA_TOKEN_set": bool(MARKETDATA_TOKEN),
        "WATCHLIST_len": len([t for t in (WATCHLIST.split(",") if WATCHLIST else []) if t.strip()]),
        "BOT_URL_set": bool(BOT_URL),
        "SCAN_SECRET_set": bool(SCAN_SECRET),
        "MAX_SPREAD_WIDTH": MAX_SPREAD_WIDTH,
        "MAX_DEBIT_PCT_WIDTH": MAX_DEBIT_PCT_WIDTH,
        "LIQ_WARN_MIN_OI": LIQ_WARN_MIN_OI,
        "LIQ_WARN_BA": LIQ_WARN_BA,
        "SCAN_MAX_DTE": SCAN_MAX_DTE,
        "DEFAULT_MAX_POSTS_PER_SCAN": DEFAULT_MAX_POSTS_PER_SCAN,
    })


# QUICK TELEGRAM TEST ENDPOINT
@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram("✅ Telegram test from Render")
    return jsonify({"telegram_status": st, "telegram_body": body})


# FIXED TV ROUTE (no debug_lines, correct indentation, correct return keys)
@app.route("/tv", methods=["POST"])
def tv_webhook():
    data = request.get_json(force=True, silent=True) or {}

    if TV_WEBHOOK_SECRET and data.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "UNKNOWN").upper()
    close = as_float(data.get("close"), 0.0)

    text = (
        "📢 TradingView Signal\n"
        f"Ticker: {ticker}\n"
        f"Close: {close:.2f}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    st, body = post_to_telegram(text)
    return jsonify({"status": "received", "telegram_status": st, "telegram_body": body})


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()

    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]
    if not tickers:
        return jsonify({"error": "WATCHLIST env var empty"}), 400

    max_posts = as_int(data.get("max_posts"), DEFAULT_MAX_POSTS_PER_SCAN)
    results_posted = 0
    debug_lines = []

    for ticker in tickers:
        if results_posted >= max_posts:
            debug_lines.append(f"Stopped early (max_posts={max_posts})")
            break

        try:
            spot = get_spot(ticker)
            exp, contracts = get_options_chain(ticker, max_dte=SCAN_MAX_DTE)

            exp_dt = datetime.fromisoformat(exp).date()
            dte = (exp_dt - datetime.now(timezone.utc).date()).days
            dte = max(dte, 0)

            call_wall, put_wall, net_gex, inc = compute_walls_and_gex(spot, contracts)
            oi_score, oi_note = oi_change_score(ticker, exp, contracts)

            # ----------------------------
            # Upgrade 1: AUTO DIRECTION
            # ----------------------------
            direction = "bull"
            if call_wall is not None and put_wall is not None:
                mid = (call_wall + put_wall) / 2.0
                if spot > mid:
                    direction = "bull"
                elif spot < mid:
                    direction = "bear"

            # ----------------------------
            # ATM IV estimate
            # ----------------------------
            near = sorted(
                [c for c in contracts if c.get("strike") is not None],
                key=lambda c: abs(as_float(c.get("strike"), 0.0) - spot),
            )[:10]

            ivs = []
            for c in near:
                iv_f = as_float(c.get("iv"), None)
                if iv_f is not None and iv_f > 0:
                    ivs.append(iv_f)

            atm_iv = (sum(ivs) / len(ivs)) if ivs else 0.30
            emove = expected_move_from_iv(spot, atm_iv, max(dte, 1))

            targets = strike_targets_and_confidence(spot, emove, call_wall, put_wall, net_gex, inc, oi_score)
            bull_zone = f"{spot:.2f} → {targets['bull_upper']:.2f}"
            bear_zone = f"{spot:.2f} → {targets['bear_lower']:.2f}"

            extra_lines = [
                f"Bull Zone: {bull_zone}",
                f"Bear Zone: {bear_zone}",
                f"Regime: {targets['regime']}",
                f"Confidence: {targets['confidence']}/100",
                f"Confidence Notes: {targets['notes']}",
            ]

            risk_label, risk_notes = risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score)
            alpha = big_alpha_score(spot, call_wall, put_wall, net_gex, oi_score, risk_label, emove, inc)

            # ----------------------------
            # Trade-worthy filters
            # ----------------------------
            near_wall = False
            if call_wall is not None and put_wall is not None:
                near_wall = min(abs(call_wall - spot), abs(spot - put_wall)) <= (2 * inc)

            big_oi = oi_score > 15000
            notable_gex = abs(net_gex) > 1e9
            trade_worthy = near_wall or big_oi or notable_gex

            if not trade_worthy:
                debug_lines.append(f"{ticker}: skipped (not trade-worthy)")
                time.sleep(0.20)
                continue

            # ----------------------------
            # 1) Send scan card to Telegram (FIXED)
            # ----------------------------
            scan_text = build_scan_message(
                ticker=ticker,
                spot=spot,
                exp=exp,
                dte=max(dte, 1),
                call_wall=call_wall,
                put_wall=put_wall,
                net_gex=net_gex,
                emove=emove,
                oi_note=oi_note,
                risk_label=risk_label,
                risk_notes=risk_notes,
                alpha_score=alpha,
                direction=direction,
                extra_lines=extra_lines,
            )

            st, body = post_to_telegram(scan_text)
            if st == 200:
                results_posted += 1
                debug_lines.append(f"{ticker}: posted telegram scan card (200)")
            else:
                debug_lines.append(f"{ticker}: telegram scan failed ({st}) {body[:120]}")

            # ----------------------------
            # 2) TRADE ENGINE
            # ----------------------------
            options_data = {
                "strike": [c.get("strike") for c in contracts],
                "side": [c.get("right") for c in contracts],  # 'call'/'put'
                "bid": [c.get("bid") for c in contracts],
                "ask": [c.get("ask") for c in contracts],
                "openInterest": [c.get("openInterest") for c in contracts],
                "iv": [c.get("iv") for c in contracts],
                "dte": [dte for _ in contracts],
            }

            try:
                rec = recommend_from_marketdata(
                    marketdata_json=options_data,
                    direction=direction,   # variable, not string
                    dte=dte,
                    spot=spot,
                )

                if isinstance(rec, dict) and rec.get("ok"):
                    trade = rec.get("trade") or {}

                    # Upgrade 2: Liquidity warnings
                    liq_warns = liquidity_warnings_for_trade(contracts, trade)

                    # Safety checks: width/debit rules
                    width = as_float(trade.get("width"), None)
                    price = as_float(trade.get("price"), None)

                    if width is not None and width > MAX_SPREAD_WIDTH:
                        liq_warns.append(f"Rule warn: width {width:g} > max {MAX_SPREAD_WIDTH:g}")

                    # Only applies to debit-ish pricing, but we warn anyway if it violates
                    if width is not None and price is not None and price > (MAX_DEBIT_PCT_WIDTH * width):
                        liq_warns.append(
                            f"Rule warn: price {price:.2f} > {MAX_DEBIT_PCT_WIDTH:.0%} of width ({(MAX_DEBIT_PCT_WIDTH*width):.2f})"
                        )

                    trade_text = build_trade_engine_text(ticker, direction, trade, liq_warns)
                    st2, body2 = post_to_telegram(trade_text)
                    if st2 == 200:
                        debug_lines.append(f"{ticker}: posted trade engine (200)")
                    else:
                        debug_lines.append(f"{ticker}: telegram trade failed ({st2}) {body2[:120]}")

                else:
                    debug_lines.append(f"{ticker}: trade engine no rec (ok=false)")

            except Exception as e:
                debug_lines.append(f"{ticker}: trade engine error {type(e).__name__}: {str(e)[:140]}")

            time.sleep(0.25)

        except Exception as e:
            debug_lines.append(f"{ticker}: error {type(e).__name__}: {str(e)[:160]}")

    return jsonify({
        "status": "ok",
        "posted": results_posted,
        "tickers": len(tickers),
        "debug": debug_lines[:200],
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
