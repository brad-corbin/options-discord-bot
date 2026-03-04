from options_engine import recommend_from_marketdata
import os
import time
import math
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------- HELPERS ----------
def first_val(x, default=None):
    """MarketData often returns [value]. Unwrap lists safely."""
    if x is None:
        return default
    if isinstance(x, list):
        if len(x) == 0:
            return default
        return x[0]
    return x

def as_float(x, default=0.0):
    v = first_val(x, default)
    try:
        return float(v)
    except Exception:
        return float(default)

def as_int(x, default=0):
    v = first_val(x, default)
    try:
        return int(v)
    except Exception:
        return int(default)

# ---------- ENV VARS (set in Render) ----------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "").strip()

MARKETDATA_TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
WATCHLIST = os.getenv("WATCHLIST", "").strip()
SCAN_SECRET = os.getenv("SCAN_SECRET", "").strip()

BOT_URL = os.getenv("BOT_URL", "").strip()

# In-memory snapshots (Render free can restart and forget these)
prev_oi_snapshot = {}  # key: (ticker, exp, right, strike) -> oi


# ---------- DEBUG ----------
@app.route("/debug", methods=["GET"])
def debug():
    return jsonify({
        "DISCORD_WEBHOOK_set": bool(DISCORD_WEBHOOK_URL),
        "MARKETDATA_TOKEN_set": bool(MARKETDATA_TOKEN),
        "WATCHLIST_len": len([t for t in (WATCHLIST.split(",") if WATCHLIST else []) if t.strip()]),
        "BOT_URL_set": bool(BOT_URL),
        "SCAN_SECRET_set": bool(SCAN_SECRET),
    })


# ---------- DISCORD ----------
def post_to_discord(payload, max_retries=5):
    """
    Discord webhooks:
      - Success: 204 No Content (sometimes 200)
      - 429: response JSON may contain retry_after
    """
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

                # small cushion + mild backoff
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


# ---------- MARKETDATA ----------
def md_headers():
    return {"Authorization": f"Bearer {MARKETDATA_TOKEN}"}

def md_get(url, params=None):
    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")

    r = requests.get(url, headers=md_headers(), params=params or {}, timeout=25)
    r.raise_for_status()
    data = r.json()

    # Debug to Render logs
    print("MARKETDATA URL:", url, flush=True)
    # NOTE: This can be big — keep it on while debugging, then you can remove
    print("MARKETDATA RESPONSE:", data, flush=True)

    return data


def get_spot(ticker: str) -> float:
    """
    MarketData quotes often return arrays like:
      last: [680.33], mid: [679.75], bid: [679.71], ask: [679.79]
    """
    data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")

    # prefer last, else mid, else bid, else ask
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
    MarketData chain response is columnar arrays, like:
      strike: [...],
      side: [...],
      openInterest: [...],
      iv: [...],
      gamma: [...],
      expiration: [epoch,...],
      dte: [...]
    We normalize into a list of dict contracts.
    Then we pick the nearest expiration <= max_dte if possible,
    otherwise we pick the smallest dte available.
    """
    data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/")

    if not isinstance(data, dict) or data.get("s") != "ok":
        raise RuntimeError(f"Bad options chain response for {ticker}: {str(data)[:120]}")

    # How many rows?
    sym_list = data.get("optionSymbol") or []
    if not isinstance(sym_list, list) or len(sym_list) == 0:
        raise RuntimeError("Unexpected chain format: optionSymbol missing/empty")

    n = len(sym_list)

    def col(name, default=None):
        v = data.get(name, default)
        # columns are lists; if missing, create a list of defaults
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

    # Build normalized contracts
    contracts = []
    for i in range(n):
        exp_epoch = expiration[i]
        exp_epoch = as_int(exp_epoch, 0)

        # Convert expiration epoch -> YYYY-MM-DD
        # (epoch is in seconds, UTC)
        exp_date = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).date().isoformat() if exp_epoch else None

        contracts.append({
            "optionSymbol": optionSymbol[i],
            "right": (side[i] or "").lower(),     # 'call' / 'put'
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

    # Group by expiration
    exp_map = {}
    for c in contracts:
        exp = c.get("expiration")
        if not exp:
            continue
        exp_map.setdefault(exp, []).append(c)

    if not exp_map:
        raise RuntimeError("No expirations found in chain response")

    # Choose expiration:
    # 1) any exp where min dte <= max_dte
    # 2) otherwise, smallest dte overall
    exp_candidates = []
    for exp, rows in exp_map.items():
        dtes = [r.get("dte") for r in rows if isinstance(r.get("dte"), int)]
        min_d = min(dtes) if dtes else 999999
        exp_candidates.append((min_d, exp))

    exp_candidates.sort(key=lambda x: x[0])  # smallest dte first

    chosen_exp = None
    for min_d, exp in exp_candidates:
        if min_d <= max_dte:
            chosen_exp = exp
            break
    if chosen_exp is None:
        chosen_exp = exp_candidates[0][1]

    return chosen_exp, exp_map[chosen_exp]


def strike_increment(strikes):
    strikes = sorted(set(float(x) for x in strikes))
    if len(strikes) < 2:
        return 1.0
    diffs = [round(strikes[i + 1] - strikes[i], 4) for i in range(len(strikes) - 1)]
    diffs = [d for d in diffs if d > 0]
    return min(diffs) if diffs else 1.0


def expected_move_from_iv(spot: float, iv: float, dte: int) -> float:
    T = max(dte, 1) / 365.0
    return spot * iv * math.sqrt(T)


def compute_walls_and_gex(ticker: str, spot: float, exp: str, contracts: list):
    """
    Walls:
      - Call Wall = strike with max call OI
      - Put Wall  = strike with max put OI
    Net GEX (very rough):
      oi * gamma * S^2 * 100; calls positive, puts negative
    """
    call_oi = {}
    put_oi = {}
    strikes = []
    net_gex = 0.0

    for c in contracts:
        right = (c.get("right") or c.get("type") or "").lower()
        strike = c.get("strike")
        if strike is None:
            continue

        strike = as_float(strike, None)
        if strike is None:
            continue
        strikes.append(strike)

        oi = as_int(c.get("openInterest") or c.get("open_interest"), 0)
        gamma = as_float(c.get("gamma"), 0.0)

        if right in ("call", "c"):
            call_oi[strike] = call_oi.get(strike, 0) + oi
            net_gex += oi * gamma * (spot ** 2) * 100.0
        elif right in ("put", "p"):
            put_oi[strike] = put_oi.get(strike, 0) + oi
            net_gex -= oi * gamma * (spot ** 2) * 100.0

    call_wall = max(call_oi.items(), key=lambda kv: kv[1])[0] if call_oi else None
    put_wall = max(put_oi.items(), key=lambda kv: kv[1])[0] if put_oi else None
    inc = strike_increment(strikes) if strikes else 1.0

    return call_wall, put_wall, net_gex, inc


def oi_change_score(ticker: str, exp: str, contracts: list):
    """
    Simple OI delta score vs in-memory snapshot.
    Returns: (score, biggest_change_str)
    """
    global prev_oi_snapshot

    total_abs_change = 0
    biggest_abs = 0
    biggest_label = "—"

    for c in contracts:
        right = (c.get("right") or c.get("type") or "").lower()
        strike = c.get("strike")
        if strike is None:
            continue

        strike = as_float(strike, None)
        if strike is None:
            continue

        oi = as_int(c.get("openInterest") or c.get("open_interest"), 0)

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


def build_discord_card(ticker, spot, exp, dte, call_wall, put_wall, net_gex, emove, oi_note, risk_label, risk_notes):
    gex_regime = "Positive Gamma / Range Favored" if net_gex >= 0 else "Negative Gamma / Trend Favored"

    if call_wall is not None and put_wall is not None:
        zero_g = f"{(call_wall + put_wall) / 2:.0f}"
        walls_line = f"Walls: Call {call_wall:g} | Put {put_wall:g} | ZeroG {zero_g} (est.)"
    else:
        walls_line = "Walls: —"

    lines = [
        f"🚨 WATCHLIST SCAN — {ticker}",
        "",
        f"Spot: {spot:.2f}",
        f"Expected Move ({max(dte,1)}D): ±{emove:.2f}",
        walls_line,
        f"Regime: {gex_regime}",
        f"OI Signal: {oi_note}",
        f"Risk: {risk_label}",
        f"Notes: {risk_notes}",
    ]

    return {"content": "```" + "\n".join(lines) + "```"}


# ---------- ROUTES ----------
@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/tv", methods=["POST"])
def tv_webhook():
    data = request.get_json(force=True, silent=True) or {}

    if TV_WEBHOOK_SECRET and data.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "UNKNOWN").upper()
    close = as_float(data.get("close"), 0.0)

    payload = {
        "content": (
            "```📢 TradingView Signal\n"
            f"Ticker: {ticker}\n"
            f"Close: {close:.2f}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "```"
        )
    }

    st, body = post_to_discord(payload)
    return jsonify({"status": "received", "discord_status": st, "discord_body": body})


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()

    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]
    if not tickers:
        return jsonify({"error": "WATCHLIST env var empty"}), 400

    # Prevent Discord spam (and 429s)
    MAX_POSTS_PER_SCAN = as_int(data.get("max_posts"), 6)

    results_posted = 0
    debug_lines = []

    for ticker in tickers:
        if results_posted >= MAX_POSTS_PER_SCAN:
            debug_lines.append(f"Stopped early (max_posts={MAX_POSTS_PER_SCAN})")
            break

        try:
            spot = get_spot(ticker)
            exp, contracts = get_options_chain(ticker, max_dte=7)

            exp_dt = datetime.fromisoformat(exp).date()
            dte = (exp_dt - datetime.now(timezone.utc).date()).days
            dte = max(dte, 0)

            call_wall, put_wall, net_gex, inc = compute_walls_and_gex(ticker, spot, exp, contracts)
            oi_score, oi_note = oi_change_score(ticker, exp, contracts)

            # ATM IV estimate (unwrap lists!)
            near = sorted(
                [c for c in contracts if c.get("strike") is not None],
                key=lambda c: abs(as_float(c.get("strike"), 0.0) - spot),
            )[:10]

            ivs = []
            for c in near:
                iv = c.get("iv")
                iv_f = as_float(iv, None)
                if iv_f is not None and iv_f > 0:
                    ivs.append(iv_f)

            atm_iv = (sum(ivs) / len(ivs)) if ivs else 0.30
            emove = expected_move_from_iv(spot, atm_iv, max(dte, 1))
# ----- TRADE ENGINE -----

options_data = {
    "strike": [c.get("strike") for c in contracts],
    "side": [c.get("right") for c in contracts],
    "bid": [c.get("bid") for c in contracts],
    "ask": [c.get("ask") for c in contracts],
    "openInterest": [c.get("openInterest") for c in contracts],
    "iv": [c.get("iv") for c in contracts],
    "dte": [dte for _ in contracts]
}

rec = recommend_from_marketdata(
    marketdata_json=options_data,
    direction="bull",
    dte=dte,
    spot=spot
)

if rec["ok"]:
    trade = rec["trade"]

    trade_message = f"""
TRADE ENGINE SIGNAL — {ticker}

Suggested {trade['type'].upper()} SPREAD

Short Strike: {trade['short']}
Long Strike: {trade['long']}

Width: {trade['width']}
Price: {trade['price']:.2f}

Max Profit: {trade['maxProfit']:.2f}
Max Loss: {trade['maxLoss']:.2f}

Return on Risk: {trade['RoR']:.2f}

Warnings: {", ".join(trade['warnings']) if trade['warnings'] else "None"}
"""

    post_to_discord({"content": f"```{trade_message}```"})
            risk_label, risk_notes = risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score)

            # Trade-worthy filters
            near_wall = False
            if call_wall is not None and put_wall is not None:
                near_wall = min(abs(call_wall - spot), abs(spot - put_wall)) <= (2 * inc)

            big_oi = oi_score > 15000
            notable_gex = abs(net_gex) > 1e9  # threshold to avoid "always true"; tune later

            trade_worthy = near_wall or big_oi or notable_gex

            if trade_worthy:
                payload = build_discord_card(
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
                )
                st, body = post_to_discord(payload)
                results_posted += 1
                debug_lines.append(f"{ticker}: posted ({st})")
                if body:
                    debug_lines.append(f"{ticker}: discord_body {body[:120]}")
            else:
                debug_lines.append(f"{ticker}: skipped")

            # light pacing
            time.sleep(0.25)

        except Exception as e:
            debug_lines.append(f"{ticker}: error {str(e)[:140]}")

    return jsonify({
        "status": "ok",
        "posted": results_posted,
        "tickers": len(tickers),
        "debug": debug_lines[:100],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
