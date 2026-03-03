import os
import threading
import time

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
TV_SECRET = os.getenv("TV_WEBHOOK_SECRET")

watches = {}


def post_to_discord(payload):
    """Send message to Discord and return status for debugging."""
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    return r.status_code, (r.text[:200] if r.text else "")


def tp_checker(watch_id):
    """Hourly TP checks (mock pricing for now)."""
    while True:
        time.sleep(3600)

        watch = watches.get(watch_id)
        if not watch:
            return

        # MOCK movement (replace with Schwab later)
        watch["current_mid"] *= 1.08

        current = watch["current_mid"]
        entry = watch["entry_mid"]

        tp_levels = [watch["tp1"], watch["tp2"], watch["tp3"]]

        highest_hit = 0
        for i, level in enumerate(tp_levels, start=1):
            if current >= level:
                highest_hit = i

        # Post only the highest newly-hit TP
        if highest_hit > watch["max_tp_hit"]:
            watch["max_tp_hit"] = highest_hit
            gain_pct = ((current - entry) / entry) * 100

            payload = {
                "content": f"✅ TP{highest_hit} HIT — {watch['ticker']} Bull Call Spread",
                "embeds": [
                    {
                        "fields": [
                            {"name": "Strikes", "value": watch["strikes"], "inline": True},
                            {"name": "Entry MID", "value": f"{entry:.2f}", "inline": True},
                            {"name": "Current MID", "value": f"{current:.2f}", "inline": True},
                            {"name": "Gain %", "value": f"{gain_pct:.1f}%", "inline": True},
                        ]
                    }
                ],
            }

            post_to_discord(payload)


@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/tv", methods=["POST"])
def tv_webhook():
    # Force JSON parsing because TradingView sometimes sends text/plain
    data = request.get_json(force=True) or {}

    if data.get("secret") != TV_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    ticker = data.get("ticker", "UNKNOWN")
    close = float(data.get("close", 0))

    # Mock entry pricing logic for V1 (replace with real options later)
    entry_mid = round(close * 0.01, 2)  # mock pricing
    width = 1.0

    tp1 = entry_mid * 1.30
    tp2 = entry_mid * 1.50
    tp3 = width * 0.95

    watch_id = f"{ticker}_{int(time.time())}"

    watches[watch_id] = {
        "ticker": ticker,
        "strikes": "Mock 1w Spread",
        "entry_mid": entry_mid,
        "current_mid": entry_mid,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "max_tp_hit": 0,
    }

    threading.Thread(target=tp_checker, args=(watch_id,), daemon=True).start()

    payload = {
        "content": f"📣 {ticker} Bull Call Spread Signal",
        "embeds": [
            {
                "fields": [
                    {"name": "Close", "value": str(close), "inline": True},
                    {"name": "Entry MID (Mock)", "value": f"{entry_mid:.2f}", "inline": True},
                    {"name": "TP1 (+30%)", "value": f"{tp1:.2f}", "inline": True},
                    {"name": "TP2 (+50%)", "value": f"{tp2:.2f}", "inline": True},
                    {"name": "TP3 (~Max)", "value": f"{tp3:.2f}", "inline": True},
                ]
            }
        ],
    }

    discord_status, discord_body = post_to_discord(payload)

    # Return Discord status so you can confirm it worked
    return jsonify({"status": "received", "discord_status": discord_status, "discord_body": discord_body})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
