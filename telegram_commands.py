# telegram_commands.py
# Telegram command interface for Omega 3000 Bot
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.

import os
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN",  "").strip()
TELEGRAM_ADMIN_IDS  = [
    x.strip() for x in
    os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip()
]

# Runtime state (stored in memory, survives within a session)
_state = {
    "paused":          False,
    "confidence_gate": int(os.getenv("MIN_CONFIDENCE_TO_POST", "40") or 40),
    "last_scan_time":  None,
    "scan_count":      0,
    "start_time":      datetime.now(timezone.utc),
}


def get_state() -> dict:
    return _state


def set_last_scan(posted: int, total: int):
    _state["last_scan_time"] = datetime.now(timezone.utc)
    _state["scan_count"] += 1
    _state["last_scan_posted"] = posted
    _state["last_scan_total"]  = total


def is_paused() -> bool:
    return _state.get("paused", False)


def get_confidence_gate() -> int:
    return _state.get("confidence_gate", 40)


def is_authorized(user_id: str) -> bool:
    if not TELEGRAM_ADMIN_IDS:
        return True   # no whitelist = anyone can use (open mode)
    return str(user_id) in TELEGRAM_ADMIN_IDS


def send_reply(chat_id: str, text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"send_reply error: {e}")


def register_webhook(bot_url: str, webhook_secret: str):
    """
    Registers this bot's /telegram_webhook endpoint with Telegram.
    Call once on startup.
    """
    if not TELEGRAM_BOT_TOKEN or not bot_url:
        log.warning("Cannot register webhook — BOT_URL or TOKEN missing")
        return

    webhook_url = f"{bot_url.rstrip('/')}/telegram_webhook/{webhook_secret}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            log.info(f"Telegram webhook registered: {webhook_url}")
        else:
            log.warning(f"Webhook registration failed: {data}")
    except Exception as e:
        log.warning(f"Webhook registration error: {e}")


def handle_command(
    user_id:   str,
    chat_id:   str,
    text:      str,
    scan_fn,          # scan_ticker function from app.py
    full_scan_fn,     # scan_watchlist logic from app.py
    watchlist: list,
) -> None:
    """
    Parse and execute a Telegram command.
    Runs in a background thread to avoid blocking the webhook response.
    """
    if not is_authorized(user_id):
        send_reply(chat_id, "⛔ You are not authorized to use this bot.")
        return

    text  = (text or "").strip()
    parts = text.split()
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1:] if len(parts) > 1 else []

    # ─────────────────────────────────────
    # /scan [TICKER]
    # ─────────────────────────────────────
    if cmd in ("/scan", "/scan@omegabot"):
        if args:
            ticker = args[0].upper()
            send_reply(chat_id, f"🔍 Scanning {ticker}...")
            result = scan_fn(ticker)
            if result.get("posted"):
                send_reply(chat_id, f"✅ {ticker} scan card posted above.")
            else:
                reason = result.get("skipped") or result.get("error") or "no setup found"
                send_reply(chat_id, f"ℹ️ {ticker}: {reason}")
        else:
            if is_paused():
                send_reply(chat_id, "⏸ Bot is paused. Use /resume first.")
                return
            send_reply(chat_id, f"🔍 Scanning full watchlist ({len(watchlist)} tickers)...")
            def run_scan():
                full_scan_fn()
            threading.Thread(target=run_scan, daemon=True).start()

    # ─────────────────────────────────────
    # /status
    # ─────────────────────────────────────
    elif cmd in ("/status", "/status@omegabot"):
        uptime     = datetime.now(timezone.utc) - _state["start_time"]
        uptime_str = f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m"

        last_scan = _state.get("last_scan_time")
        if last_scan:
            mins_ago  = int((datetime.now(timezone.utc) - last_scan).total_seconds() / 60)
            scan_str  = f"{mins_ago}m ago ({_state.get('last_scan_posted',0)}/{_state.get('last_scan_total',0)} posted)"
        else:
            scan_str = "No scan run yet"

        paused_str = "⏸ PAUSED" if _state["paused"] else "▶️ Running"
        conf_str   = str(_state["confidence_gate"])

        msg = (
            f"🤖 Omega 3000 Status\n"
            f"State: {paused_str}\n"
            f"Uptime: {uptime_str}\n"
            f"Last Scan: {scan_str}\n"
            f"Total Scans: {_state['scan_count']}\n"
            f"Confidence Gate: {conf_str}/100\n"
            f"Watchlist: {len(watchlist)} tickers\n"
            f"Admins: {len(TELEGRAM_ADMIN_IDS)} authorized"
        )
        send_reply(chat_id, msg)

    # ─────────────────────────────────────
    # /watchlist
    # ─────────────────────────────────────
    elif cmd in ("/watchlist", "/watchlist@omegabot"):
        if not watchlist:
            send_reply(chat_id, "⚠️ Watchlist is empty.")
            return
        chunks = [watchlist[i:i+20] for i in range(0, len(watchlist), 20)]
        for i, chunk in enumerate(chunks):
            msg = f"📋 Watchlist ({i*20+1}–{i*20+len(chunk)}):\n"
            msg += ", ".join(chunk)
            send_reply(chat_id, msg)

    # ─────────────────────────────────────
    # /confidence [value]
    # ─────────────────────────────────────
    elif cmd in ("/confidence", "/confidence@omegabot"):
        if not args:
            send_reply(chat_id,
                f"Current confidence gate: {_state['confidence_gate']}/100\n"
                f"Usage: /confidence 60"
            )
            return
        try:
            val = int(args[0])
            if not 0 <= val <= 100:
                raise ValueError
            old = _state["confidence_gate"]
            _state["confidence_gate"] = val
            send_reply(chat_id,
                f"✅ Confidence gate updated: {old} → {val}/100\n"
                f"Trades below {val}/100 will be suppressed."
            )
        except ValueError:
            send_reply(chat_id, "⚠️ Usage: /confidence 60 (must be 0–100)")

    # ─────────────────────────────────────
    # /pause
    # ─────────────────────────────────────
    elif cmd in ("/pause", "/pause@omegabot"):
        _state["paused"] = True
        send_reply(chat_id, "⏸ Bot paused. Scheduled scans will be skipped. Use /resume to restart.")

    # ─────────────────────────────────────
    # /resume
    # ─────────────────────────────────────
    elif cmd in ("/resume", "/resume@omegabot"):
        _state["paused"] = False
        send_reply(chat_id, "▶️ Bot resumed. Scheduled scans will run normally.")

    # ─────────────────────────────────────
    # /help
    # ─────────────────────────────────────
    elif cmd in ("/help", "/help@omegabot", "/start"):
        msg = (
            "🤖 Omega 3000 Commands:\n\n"
            "/scan AAPL — scan single ticker\n"
            "/scan — run full watchlist scan\n"
            "/status — bot health and last scan info\n"
            "/watchlist — show all tickers\n"
            "/confidence 60 — set minimum confidence gate\n"
            "/pause — pause scheduled scans\n"
            "/resume — resume scheduled scans\n"
            "/help — show this message\n\n"
            "— Not financial advice —"
        )
        send_reply(chat_id, msg)

    else:
        send_reply(chat_id,
            f"❓ Unknown command: {cmd}\nType /help for available commands."
        )
