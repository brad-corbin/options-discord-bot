import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ====== ENV VARS (set these in Render + GitHub secrets) ======
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TV_SECRET = os.getenv("TV_WEBHOOK_SECRET", "").strip()
SCAN_SECRET = os.getenv("SCAN_SECRET", "").strip()

# Optional controls
POST_MODE = os.getenv("POST_MODE", "summary_then_cards").strip().lower()
# modes: "summary_only", "cards_only", "summary_then_cards"

MAX_CARDS_PER_SCAN = int(os.getenv("MAX_CARDS_PER_SCAN", "3"))  # limit spam
WATCHLIST = os.getenv("WATCHLIST", "SPY,QQQ,NVDA,AAPL,MSFT,META,GOOGL,CAT,BE,XOM,GLD,SLV,NEM,FDX,PLTR,IREN,LMND,SOFI,ALAB,NBIS,GTLB,DBX,FRSH,KLAR,PFE,NOC,LMT,HAL,RTX,XLE,HII,IWM,HD,LOW,IAU,ITA,TSLA,TE,ONDS,BBAI,MRNA,OXY,TSLA,MSFT,CIFR,INOD,DUOL,CORT,ALZN,RXRX,RBLX,EOSE,HOOD").split(",")

# Store “open watches” for TP monitoring (mock for now)
watches = {}


# ============================================================
# Discord posting with rate limit handling
# ============================================================
def post_to_discord(payload, max_retries=3):
    """
    Discord webhooks return:
      - 204 No Content on success
      - 429 if rate limited with 'retry_after' in JSON
    """
    if not DISCORD_WEBHOOK:
        return 400, "DISCORD_WEBHOOK_URL not set"

    for attempt in range(max_retries + 1):
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

            # success
            if r.status_code in (200, 204):
                return r.status_code, ""

            # rate limited
            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("retry_after", 2))
                except Exception:
                    retry_after = 2.0

                # small cushion
                time.sleep(retry_after + 0.25)
                continue

            # other error
            return r.status_code, (r.text[:500] if r.text else "")

        except Exception as e:
            # backoff for network blips
            time.sleep(1.5 * (attempt + 1))
            last_err = str(e)

    return 500, f"Discord post failed after retries: {last_err}"


# ============================================================
# Helpers: formatting + “next-level” scoring (mock data for now)
# ============================================================
def now_ct_str():
    # Render server time may be UTC; we print UTC to keep consistent
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def round_to_increment(x, inc=0.5):
    return round(round(x / inc) * inc, 2)


def approx_expected_move(spot, days=7):
    """
    Placeholder expected move until real IV:
    - uses simple 4% 7D move as a baseline (tweak later).
    When you connect IV, replace this with:
      expected_move = spot * iv * sqrt(days/365)
    """
    base_pct_7d = 0.04
    return round(spot * base_pct_7d, 2)


def approx_walls(spot, expected_move):
    """
    Placeholder walls:
    - call wall near +0.8 * expected_move
    - put wall near -0.8 * expected_move
    - zero gamma near spot rounded
    """
    call_wall = round_to_increment(spot + expected_move * 0.8, 1.0)
    put_wall = round_to_increment(spot - expected_move * 0.8, 1.0)
    zero_g = round_to_increment(spot, 1.0)
    return call_wall, put_wall, zero_g


def risk_rating(spot, expected_move, call_wall, put_wall, regime):
    """
    Simple, readable rating:
    - Positive gamma / range favored generally lower risk
    - Trend favored generally higher risk for spreads unless aligned
    """
    # distance to walls
    dist_call = abs(call_wall - spot)
    dist_put = abs(spot - put_wall)

    # base score
    score = 0

    # regime effect
    if regime == "Positive Gamma / Range favored":
        score += 0
    else:
        score += 2

    # closer to walls = higher risk (chop / rejection zones)
    if dist_call < expected_move * 0.25:
        score += 2
    if dist_put < expected_move * 0.25:
        score += 2

    # very small expected move = tighter range = okay, but can be whippy
    if expected_move < spot * 0.02:
        score += 1

    if score <= 2:
        return "🟢 LOW"
    if score <= 5:
        return "🟡 MOD"
    return "🔴 HIGH"


def regime_guess(call_wall, put_wall, zero_g, spot):
    """
    Placeholder: if spot is near zero_g and walls are balanced, call it positive gamma.
    Replace with real GEX sign once you compute dealer gamma.
    """
    if abs(spot - zero_g) <= max(1.0, spot * 0.002):
        return "Positive Gamma / Range favored"
    return "Neutral/Negative Gamma / Trend favored"


def choose_spread_candidate(ticker, spot, expected_move, call_wall, put_wall, regime):
    """
    Pick a “suggested trade” template in the style you like.
    For now:
      - If positive gamma (range), suggest credit spread outside expected move.
      - Else suggest bull call debit spread for calls (your preference).
    """
    days = 7  # placeholder; later you’ll choose 0–7 DTE based on chain
    if regime.startswith("Positive Gamma"):
        # Call credit spread above call wall / expected move
        short = int(round(call_wall))  # simple
        long = short + 5
        credit = 0.85  # placeholder
        max_loss = round((long - short) - credit, 2)
        pop = 81  # placeholder
        roi = round((credit / max_loss) * 100, 1) if max_loss > 0 else 0
        return {
            "label": f"{days}D Call Credit Spread",
            "legs": f"Sell {short}C / Buy {long}C",
            "price_label": f"Credit: {credit:.2f} | Max Loss: {max_loss:.2f}",
            "pop": f"{pop}%",
            "roi": f"{roi}%",
            "why": [
                "Short strike outside expected move",
                "Above call wall",
                "Liquidity OK (placeholder)",
            ],
            "risk_capital": max_loss,  # for TP math conceptually
            "max_profit": credit,
            "direction": "credit",
        }

    # Trend favored → bull call debit spread (your preference)
    lower = int(round(spot - 2))
    upper = lower + 1
    debit = 0.52  # placeholder
    max_profit = round((upper - lower) - debit, 2)
    return {
        "label": f"{days}D Bull Call Spread",
        "legs": f"Buy {lower}C / Sell {upper}C",
        "price_label": f"Debit: {debit:.2f} | Max Profit: {max_profit:.2f}",
        "pop": "—",
        "roi": "—",
        "why": [
            "Calls aligned with trend regime (placeholder)",
            "Tight width for 0–7DTE style",
            "Liquidity OK (placeholder)",
        ],
        "risk_capital": debit,   # risk = debit paid
        "max_profit": max_profit,
        "direction": "debit",
    }


def tp_levels_from_risk(entry_mid, risk_capital, max_profit):
    """
    Your rule: TP1/TP2 are % of RISK CAPITAL (not max profit)
      TP1: +30% of risk
      TP2: +50% of risk
      TP3: max profit (cap)
    For debit spreads: entry_mid = debit, risk_capital = debit, max_profit known.
    """
    tp1 = round(entry_mid + (risk_capital * 0.30), 2)
    tp2 = round(entry_mid + (risk_capital * 0.50), 2)

    # For TP3, target “near max profit”
    # Debit spread max value ≈ width; profit cap = max_profit, so price cap ≈ entry + max_profit
    tp3 = round(entry_mid + max_profit, 2) if max_profit is not None else round(entry_mid * 2, 2)
    return tp1, tp2, tp3


def build_watchlist_card(ticker, spot, expected_move, call_wall, put_wall, zero_g, regime, trade, risk, tp1, tp2, tp3):
    """
    Builds the exact “clean card” look using a Discord code block.
    """
    why_lines = "\n".join([f"• {w}" for w in trade["why"]])

    text = (
        f"🚨 WATCHLIST SETUP — {ticker}\n\n"
        f"Spot: {spot:.2f}\n"
        f"Expected Move (7D): ±{expected_move:.2f}\n"
        f"Walls: Call {call_wall} | Put {put_wall} | ZeroG {zero_g}\n"
        f"Regime: {regime}\n\n"
        f"✅ Suggested: {trade['label']}\n"
        f"{trade['legs']}\n"
        f"{trade['price_label']}\n"
        f"POP: {trade['pop']} | ROI: {trade['roi']}\n"
        f"Risk Rating: {risk}\n\n"
        f"Targets (MID): TP1 {tp1:.2f} | TP2 {tp2:.2f} | TP3 {tp3:.2f}\n\n"
        f"Why:\n"
        f"{why_lines}\n"
    )

    # Discord monospace card
    return {"content": f"```{text}```"}


def is_trade_worthy(risk, trade):
    """
    Simple v1 gate:
      - allow 🟢 always
      - allow 🟡 only for credit spreads (range setups), otherwise skip
    """
    if risk.startswith("🟢"):
        return True
    if risk.startswith("🟡") and trade["direction"] == "credit":
        return True
    return False


# ============================================================
# TP Checker (mock) — posts only the highest TP hit
# ============================================================
def tp_checker(watch_id):
    while True:
        time.sleep(3600)

        watch = watches.get(watch_id)
        if not watch:
            return

        # MOCK movement (replace with live options mid later)
        watch["current_mid"] *= 1.08

        current = watch["current_mid"]
        entry = watch["entry_mid"]

        tp_levels = [watch["tp1"], watch["tp2"], watch["tp3"]]
        highest_hit = 0

        for i, level in enumerate(tp_levels, start=1):
            if current >= level:
                highest_hit = i

        if highest_hit > watch["max_tp_hit"]:
            watch["max_tp_hit"] = highest_hit
            gain_pct = ((current - entry) / entry) * 100

            payload = {
                "content": f"✅ TP{highest_hit} HIT — {watch['ticker']} ({watch['label']})",
                "embeds": [
                    {
                        "fields": [
                            {"name": "Legs", "value": watch["legs"], "inline": False},
                            {"name": "Entry MID", "value": f"{entry:.2f}", "inline": True},
                            {"name": "Current MID", "value": f"{current:.2f}", "inline": True},
                            {"name": "Gain %", "value": f"{gain_pct:.1f}%", "inline": True},
                        ]
                    }
                ],
            }
            post_to_discord(payload)


# ============================================================
# Routes
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/tv", methods=["POST"])
def tv_webhook():
    """
    TradingView webhook expects JSON like:
    {
      "secret": "...",
      "ticker": "{{ticker}}",
      "close": {{close}}
    }
    """
    data = request.get_json(force=True) or {}

    if data.get("secret") != TV_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    ticker = str(data.get("ticker", "UNKNOWN")).upper()
    close = float(data.get("close", 0))

    # Next-level “setup” using same engine (still mock until real chain)
    spot = close if close > 0 else round(400 + (hash(ticker) % 100), 2)
    expected_move = approx_expected_move(spot, days=7)
    call_wall, put_wall, zero_g = approx_walls(spot, expected_move)
    regime = regime_guess(call_wall, put_wall, zero_g, spot)

    trade = choose_spread_candidate(ticker, spot, expected_move, call_wall, put_wall, regime)

    # Entry mid (mock) — replace with live mid from options chain later
    # For debit spreads: entry_mid ≈ debit, for credit spreads: entry_mid ≈ credit
    entry_mid = 0.52 if trade["direction"] == "debit" else 0.85

    risk_capital = float(trade["risk_capital"]) if trade["risk_capital"] is not None else entry_mid
    max_profit = float(trade["max_profit"]) if trade["max_profit"] is not None else entry_mid

    tp1, tp2, tp3 = tp_levels_from_risk(entry_mid, risk_capital, max_profit)
    risk = risk_rating(spot, expected_move, call_wall, put_wall, regime)

    # Post clean card
    payload = build_watchlist_card(
        ticker=ticker,
        spot=spot,
        expected_move=expected_move,
        call_wall=call_wall,
        put_wall=put_wall,
        zero_g=zero_g,
        regime=regime,
        trade=trade,
        risk=risk,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
    )
    discord_status, discord_body = post_to_discord(payload)

    # Start TP monitoring (mock) so you get “highest TP hit”
    watch_id = f"{ticker}_{int(time.time())}"
    watches[watch_id] = {
        "ticker": ticker,
        "label": trade["label"],
        "legs": trade["legs"],
        "entry_mid": entry_mid,
        "current_mid": entry_mid,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "max_tp_hit": 0,
    }
    threading.Thread(target=tp_checker, args=(watch_id,), daemon=True).start()

    return jsonify(
        {
            "status": "received",
            "ticker": ticker,
            "spot": spot,
            "discord_status": discord_status,
            "discord_body": discord_body,
            "timestamp": now_ct_str(),
        }
    )


@app.route("/scan", methods=["POST"])
def scan_watchlist():
    """
    GitHub Actions calls:
      POST /scan
      {"secret": "<SCAN_SECRET>"}
    """
    data = request.get_json(force=True) or {}

    if data.get("secret") != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    tickers = [t.strip().upper() for t in WATCHLIST if t.strip()]
    if not tickers:
        return jsonify({"error": "WATCHLIST empty"}), 400

    tradeworthy = []
    cards = []

    for ticker in tickers:
        # MOCK spot — replace with real spot (provider) later
        spot = round(400 + (hash(ticker) % 500), 2)

        expected_move = approx_expected_move(spot, days=7)
        call_wall, put_wall, zero_g = approx_walls(spot, expected_move)
        regime = regime_guess(call_wall, put_wall, zero_g, spot)

        trade = choose_spread_candidate(ticker, spot, expected_move, call_wall, put_wall, regime)

        # mock entry mid
        entry_mid = 0.52 if trade["direction"] == "debit" else 0.85
        risk_capital = float(trade["risk_capital"]) if trade["risk_capital"] is not None else entry_mid
        max_profit = float(trade["max_profit"]) if trade["max_profit"] is not None else entry_mid

        tp1, tp2, tp3 = tp_levels_from_risk(entry_mid, risk_capital, max_profit)
        risk = risk_rating(spot, expected_move, call_wall, put_wall, regime)

        if is_trade_worthy(risk, trade):
            tradeworthy.append((ticker, risk, trade["label"], trade["legs"]))
            cards.append(
                build_watchlist_card(
                    ticker=ticker,
                    spot=spot,
                    expected_move=expected_move,
                    call_wall=call_wall,
                    put_wall=put_wall,
                    zero_g=zero_g,
                    regime=regime,
                    trade=trade,
                    risk=risk,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                )
            )

    # Summary message
    if POST_MODE in ("summary_only", "summary_then_cards"):
        if tradeworthy:
            lines = "\n".join([f"• {t} — {r} — {lbl} — {legs}" for t, r, lbl, legs in tradeworthy])
            summary = (
                f"```📋 WATCHLIST SCAN — {now_ct_str()}\n\n"
                f"✅ Trade-worthy ({len(tradeworthy)}):\n{lines}\n```"
            )
        else:
            summary = f"```📋 WATCHLIST SCAN — {now_ct_str()}\n\nNo trade-worthy setups right now.```"

        post_to_discord({"content": summary})

    # Post cards (limited)
    if POST_MODE in ("cards_only", "summary_then_cards"):
        for payload in cards[:MAX_CARDS_PER_SCAN]:
            post_to_discord(payload)
            time.sleep(0.8)  # tiny spacing helps avoid webhook bursts

    return jsonify(
        {
            "status": "scan_complete",
            "tickers": tickers,
            "tradeworthy_count": len(tradeworthy),
            "posted_cards": min(len(cards), MAX_CARDS_PER_SCAN) if POST_MODE != "summary_only" else 0,
            "timestamp": now_ct_str(),
        }
    )


if __name__ == "__main__":
    # Render sets PORT; fallback to 10000
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
