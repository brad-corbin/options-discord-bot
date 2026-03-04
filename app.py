import os
import time
import math
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
@app.route("/debug", methods=["GET"])
def debug():
    # DON'T print the token; only whether it exists
    return jsonify({
        "MARKETDATA_TOKEN_set": bool(os.getenv("MARKETDATA_TOKEN")),
        "DISCORD_WEBHOOK_set": bool(os.getenv("DISCORD_WEBHOOK_URL")),
        "WATCHLIST_len": len((os.getenv("WATCHLIST") or "").split(",")) if os.getenv("WATCHLIST") else 0
    })
# ---------- ENV VARS (set these in Render) ----------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "").strip()

MARKETDATA_TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()  # from marketdata.app
WATCHLIST = os.getenv("WATCHLIST", "").strip()  # comma-separated tickers
SCAN_SECRET = os.getenv("SCAN_SECRET", "").strip()  # secret you call /scan with

# Optional: base url for your own service (nice for debugging / actions)
BOT_URL = os.getenv("BOT_URL", "").strip()

# In-memory snapshots (note: free Render can restart and forget these)
prev_oi_snapshot = {}  # key: (ticker, exp, right, strike) -> oi


# ---------- DISCORD ----------
def post_to_discord(payload, max_retries=5):
    """
    Discord webhook:
      - Success is 204 No Content (sometimes 200)
      - On 429, response JSON includes retry_after (seconds)
    """
    if not DISCORD_WEBHOOK_URL:
        return 400, "DISCORD_WEBHOOK_URL not set"

    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)

            if r.status_code in (200, 204):
                return r.status_code, ""

            # Rate limited
            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("retry_after", 2))
                except Exception:
                    retry_after = 2.0

                # Add small cushion + exponential backoff
                sleep_s = retry_after + min(2.0 * attempt, 6.0)
                time.sleep(sleep_s)
                last_err = f"429 rate limited; slept {sleep_s:.2f}s"
                continue

            # Other error
            last_err = (r.text[:300] if r.text else f"HTTP {r.status_code}")
            # brief backoff to avoid hammering
            time.sleep(min(1.5 * (attempt + 1), 6))
        except Exception as e:
            last_err = str(e)
            time.sleep(min(1.5 * (attempt + 1), 6))

    return 500, f"Discord post failed after retries: {last_err}"


# ---------- MARKETDATA (REST) ----------
def md_headers():
    # MarketData supports Bearer tokens via Authorization header
    # If this ever fails, you can fall back to token= query param (less secure).
    return {"Authorization": f"Bearer {MARKETDATA_TOKEN}"}


def md_get(url, params=None):
    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")

    r = requests.get(url, headers=md_headers(), params=params or {}, timeout=25)
    r.raise_for_status()

    data = r.json()

    # Debug output to Render logs
    print("MARKETDATA URL:", url, flush=True)
    print("MARKETDATA RESPONSE:", data, flush=True)

    return data


def get_spot(ticker: str) -> float:
    # Quotes endpoint (stocks)
    # Example: https://api.marketdata.app/v1/stocks/quotes/SPY/
    data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")
    # MarketData responses often wrap arrays; handle common shapes safely
    # Try the common fields:
    for key in ("last", "price", "mid"):
        if isinstance(data, dict) and key in data and data[key] is not None:
            return float(data[key])
    # Fallback: try typical array response format { "last": [..] }
    if isinstance(data, dict):
        if "last" in data and isinstance(data["last"], list) and data["last"]:
            return float(data["last"][0])
    raise RuntimeError(f"Could not parse spot quote for {ticker}")


def get_options_chain(ticker: str, max_dte: int = 7):
    """
    Pull an options chain and pick the nearest expiration <= max_dte.
    NOTE: MarketData has multiple options endpoints. This function expects
    that the chain payload includes: expiration, strike, right (call/put),
    openInterest, volume, iv, gamma, delta, bid, ask.
    """
    # Many MarketData installs provide an "options chain" endpoint.
    # If your account uses a different path, we’ll adapt quickly after you confirm.
    #
    # Try a commonly used chain endpoint:
    #   https://api.marketdata.app/v1/options/chain/{ticker}/
    data = md_get(f"https://api.marketdata.app/v1/options/chain/{ticker}/")

    # Normalize into list of contracts (best-effort)
    contracts = data.get("contracts") if isinstance(data, dict) else None
    if contracts is None and isinstance(data, dict) and "data" in data:
        contracts = data["data"]
    if not contracts or not isinstance(contracts, list):
        raise RuntimeError("Unexpected chain response format (no contracts list).")

    # Pick expirations within DTE
    now = datetime.now(timezone.utc)
    exp_to_contracts = {}
    for c in contracts:
        exp = c.get("expiration") or c.get("exp")
        if not exp:
            continue
        # exp might be "YYYY-MM-DD"
        try:
            exp_dt = datetime.fromisoformat(exp).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        dte = (exp_dt.date() - now.date()).days
        if 0 <= dte <= max_dte:
            exp_to_contracts.setdefault(exp, []).append(c)

    if not exp_to_contracts:
        # If none in 0-7, just take soonest available
        for c in contracts:
            exp = c.get("expiration") or c.get("exp")
            if exp:
                exp_to_contracts.setdefault(exp, []).append(c)

    chosen_exp = sorted(exp_to_contracts.keys())[0]
    return chosen_exp, exp_to_contracts[chosen_exp]


def strike_increment(strikes):
    strikes = sorted(set(float(x) for x in strikes))
    if len(strikes) < 2:
        return 1.0
    diffs = [round(strikes[i+1] - strikes[i], 4) for i in range(len(strikes)-1)]
    diffs = [d for d in diffs if d > 0]
    return min(diffs) if diffs else 1.0


def expected_move_from_iv(spot: float, iv: float, dte: int) -> float:
    # Expected move ~ S * IV * sqrt(T)
    T = max(dte, 1) / 365.0
    return spot * iv * math.sqrt(T)


def compute_walls_and_gex(ticker: str, spot: float, exp: str, contracts: list):
    """
    Walls:
      - Call Wall: strike with max call OI
      - Put Wall: strike with max put OI
    GEX:
      - Approx net gamma exposure using OI*gamma*S^2*100; calls positive, puts negative
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
        strike = float(strike)
        strikes.append(strike)

        oi = c.get("openInterest") or c.get("open_interest") or 0
        try:
            oi = int(oi)
        except Exception:
            oi = 0

        gamma = c.get("gamma")
        try:
            gamma = float(gamma) if gamma is not None else 0.0
        except Exception:
            gamma = 0.0

        # Wall calc
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
    Computes a simple OI delta score vs the previous snapshot stored in memory.
    Returns: (score, biggest_change_str)
    """
    global prev_oi_snapshot

    total_abs_change = 0
    biggest = (0, "")  # (abs_change, label)

    for c in contracts:
        right = (c.get("right") or c.get("type") or "").lower()
        strike = c.get("strike")
        if strike is None:
            continue
        strike = float(strike)

        oi = c.get("openInterest") or c.get("open_interest") or 0
        try:
            oi = int(oi)
        except Exception:
            oi = 0

        k = (ticker, exp, right, strike)
        prev = prev_oi_snapshot.get(k, None)
        if prev is not None:
            delta = oi - prev
            absd = abs(delta)
            total_abs_change += absd
            if absd > biggest[0]:
                biggest = (absd, f"{right.upper()} {strike:g} ΔOI {delta:+d}")
        prev_oi_snapshot[k] = oi

    return total_abs_change, (biggest[1] if biggest[0] else "—")


def risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score):
    """
    Simple heuristic (you can tune later):
      - Lower risk if positive gamma AND price is inside walls
      - Higher risk if near/through walls or negative gamma
      - Higher risk if big OI shifts (can mean positioning change)
    """
    if call_wall is None or put_wall is None:
        return "⚪ Unknown", "Walls not available"

    dist_call = abs(call_wall - spot)
    dist_put = abs(spot - put_wall)
    near_wall = min(dist_call, dist_put)

    notes = []
    score = 0

    # Gamma regime
    if net_gex >= 0:
        score += 1
        notes.append("Positive Gamma (range-favored)")
    else:
        score -= 1
        notes.append("Negative Gamma (trend/vol risk)")

    # Near wall?
    if near_wall <= (2 * inc):
        score -= 1
        notes.append("Near a major wall")
    else:
        score += 1
        notes.append("Not hugging walls")

    # OI shifts
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


def choose_spread_width(inc):
    """
    Your preference:
      - Suggest $1 spreads when available
      - Show $2.50 when it exists
    """
    # If chain increments show $1, use $1 by default
    if inc <= 1.01:
        return 1.0, 2.5
    # If $2.50 increments exist, use it
    if abs(inc - 2.5) < 0.01:
        return 2.5, None
    # Fallback: use the detected increment
    return inc, None


def build_discord_card(ticker, spot, exp, dte, call_wall, put_wall, net_gex, emove, oi_note, risk_label, risk_notes):
    gex_regime = "Positive Gamma / Range Favored" if net_gex >= 0 else "Negative Gamma / Trend Favored"
    zero_g = "—"
    if call_wall and put_wall:
        zero_g = f"{(call_wall + put_wall)/2:.0f}"

    lines = [
        f"🚨 WATCHLIST SCAN — {ticker}",
        "",
        f"Spot: {spot:.2f}",
        f"Expected Move ({dte}D): ±{emove:.2f}",
        f"Walls: Call {call_wall:g} | Put {put_wall:g} | ZeroG {zero_g} (est.)",
        f"Regime: {gex_regime}",
        f"OI Signal: {oi_note}",
        f"Risk: {risk_label}",
        f"Notes: {risk_notes}",
    ]

    return {
        "content": "```" + "\n".join(lines) + "```"
    }


# ---------- ROUTES ----------
@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/tv", methods=["POST"])
def tv_webhook():
    # TradingView sometimes sends text/plain
    data = request.get_json(force=True, silent=True) or {}

    if TV_WEBHOOK_SECRET and data.get("secret") != TV_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "UNKNOWN").upper()
    close = float(data.get("close") or 0)

    # V1: just post the raw signal cleanly. (We’ll swap to real option pricing once Schwab/MarketData logic is ready.)
    payload = {
        "content": f"```📢 TradingView Signal\nTicker: {ticker}\nClose: {close:.2f}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n```"
    }
    st, body = post_to_discord(payload)
    return jsonify({"status": "received", "discord_status": st, "discord_body": body})


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    # Secure it
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]
    if not tickers:
        return jsonify({"error": "WATCHLIST env var empty"}), 400

    results_posted = 0
    debug = []

    for ticker in tickers:
        try:
            spot = get_spot(ticker)
            exp, contracts = get_options_chain(ticker, max_dte=7)

            # DTE
            exp_dt = datetime.fromisoformat(exp).date()
            dte = (exp_dt - datetime.now(timezone.utc).date()).days
            dte = max(dte, 0)

            call_wall, put_wall, net_gex, inc = compute_walls_and_gex(ticker, spot, exp, contracts)
            oi_score, oi_note = oi_change_score(ticker, exp, contracts)

            # Expected move: try ATM IV from nearest-to-spot options (fallback to 0.30 if missing)
            # Pick a few contracts nearest spot and average their IV
            near = sorted(
                [c for c in contracts if c.get("strike") is not None],
                key=lambda c: abs(float(c.get("strike")) - spot),
            )[:10]
            ivs = []
            for c in near:
                iv = c.get("iv")
                try:
                    if iv is not None:
                        ivs.append(float(iv))
                except Exception:
                    pass
            atm_iv = sum(ivs) / len(ivs) if ivs else 0.30
            emove = expected_move_from_iv(spot, atm_iv, dte if dte else 1)

            risk_label, risk_notes = risk_rating(spot, call_wall, put_wall, net_gex, inc, oi_score)

            # “Trade worthy” logic (your request):
            # - price near call/put wall OR big OI changes OR GEX regime noteworthy
            near_wall = False
            if call_wall is not None and put_wall is not None:
                near_wall = min(abs(call_wall - spot), abs(spot - put_wall)) <= (2 * inc)

            big_oi = oi_score > 15000
            notable_gex = abs(net_gex) > 0  # always present; you can tighten later

            trade_worthy = near_wall or big_oi or notable_gex

            if trade_worthy:
                payload = build_discord_card(
                    ticker=ticker,
                    spot=spot,
                    exp=exp,
                    dte=dte if dte else 1,
                    call_wall=call_wall or 0,
                    put_wall=put_wall or 0,
                    net_gex=net_gex,
                    emove=emove,
                    oi_note=oi_note,
                    risk_label=risk_label,
                    risk_notes=risk_notes,
                )
                st, body = post_to_discord(payload)
                results_posted += 1
                debug.append(f"{ticker}: posted ({st})")
            else:
                debug.append(f"{ticker}: skipped (not trade-worthy)")

            # Gentle pacing to avoid Discord/global limits
            time.sleep(0.4)

        except Exception as e:
            debug.append(f"{ticker}: error {str(e)[:120]}")
            # keep going

    summary = {
        "status": "ok",
        "posted": results_posted,
        "tickers": len(tickers),
        "debug": debug[:50],
    }
    return jsonify(summary)


if __name__ == "__main__":
    # Render binds externally; keep 10000 since you used it
    app.run(host="0.0.0.0", port=10000)
