# telegram_commands.py
# Telegram command interface for Omega 3000 Bot
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3 UPGRADE: Added /check TICKER command for on-demand trade analysis
# v3.1 UPGRADE (Phase 2A): Added portfolio commands:
#   /hold, /sell, /close, /expire, /assign, /options, /wheel

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
    check_fn,         # check_ticker function from app.py (v3)
    watchlist: list,
    get_spot_fn=None, # get_spot function from app.py (Phase 2A)
    md_get_fn=None,   # md_get function from app.py (Phase 2B)
) -> None:
    """
    Parse and execute a Telegram command.
    Runs in a background thread to avoid blocking the webhook response.

    Phase 2A adds get_spot_fn for live price lookups in portfolio commands.
    Phase 2B adds md_get_fn for sentiment analysis (candle/quote data).
    Both optional so existing callers don't break.
    """
    if not is_authorized(user_id):
        send_reply(chat_id, "⛔ You are not authorized to use this bot.")
        return

    text  = (text or "").strip()
    parts = text.split()
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1:] if len(parts) > 1 else []

    # Helper: send to this chat
    reply = lambda msg: send_reply(chat_id, msg)

    # ─────────────────────────────────────
    # PHASE 2A — PORTFOLIO COMMANDS
    # ─────────────────────────────────────

    if cmd in ("/hold", "/hold@omegabot"):
        from holdings_commands import handle_hold
        _spot = get_spot_fn or _no_spot
        threading.Thread(
            target=_safe_run,
            args=(handle_hold, args, reply, _spot, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/sell", "/sell@omegabot"):
        from holdings_commands import handle_sell
        threading.Thread(
            target=_safe_run,
            args=(handle_sell, args, reply, None, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/close", "/close@omegabot"):
        from holdings_commands import handle_close
        threading.Thread(
            target=_safe_run,
            args=(handle_close, args, reply, None, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/expire", "/expire@omegabot"):
        from holdings_commands import handle_expire
        threading.Thread(
            target=_safe_run,
            args=(handle_expire, args, reply, None, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/assign", "/assign@omegabot"):
        from holdings_commands import handle_assign
        threading.Thread(
            target=_safe_run,
            args=(handle_assign, args, reply, None, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/options", "/options@omegabot"):
        from holdings_commands import handle_options
        threading.Thread(
            target=_safe_run,
            args=(handle_options, args, reply, None, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/wheel", "/wheel@omegabot"):
        from holdings_commands import handle_wheel
        threading.Thread(
            target=_safe_run,
            args=(handle_wheel, args, reply, None, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/holdings", "/holdings@omegabot"):
        from holdings_commands import handle_holdings
        _md = md_get_fn or _no_md_get
        threading.Thread(
            target=_safe_run,
            args=(handle_holdings, args, reply, _md, chat_id),
            daemon=True,
        ).start()
        return

    if cmd in ("/portfolio", "/portfolio@omegabot"):
        from holdings_commands import handle_portfolio
        _md = md_get_fn or _no_md_get
        threading.Thread(
            target=_safe_run,
            args=(handle_portfolio, args, reply, _md, chat_id),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /check TICKER [bull|bear] — on-demand trade analysis (v3 engine)
    # ─────────────────────────────────────
    if cmd in ("/check", "/check@omegabot"):
        if not args:
            send_reply(chat_id,
                "Usage: /check AAPL\n"
                "       /check SPY bull\n\n"
                "Analyzes any ticker and returns a trade card\n"
                "if it meets your rules, or explains why not."
            )
            return

        ticker = args[0].upper()
        direction = args[1].lower() if len(args) > 1 else "bull"

        if direction not in ("bull", "bear"):
            send_reply(chat_id, f"⚠️ Direction must be 'bull' or 'bear', got '{direction}'")
            return

        send_reply(chat_id, f"🔍 Checking {ticker} ({direction})...")

        def run_check():
            try:
                result = check_fn(ticker, direction)
                # check_fn posts the trade card directly to telegram
                if not result.get("posted") and not result.get("ok"):
                    reason = result.get("reason") or result.get("error") or "no valid setup"
                    conf = result.get("confidence")
                    msg = f"❌ {ticker} — {reason}"
                    if conf is not None:
                        msg += f"\nConfidence: {conf}/100"
                    send_reply(chat_id, msg)
            except Exception as e:
                log.error(f"/check {ticker}: {e}")
                send_reply(chat_id, f"⚠️ Error checking {ticker}: {type(e).__name__}")

        threading.Thread(target=run_check, daemon=True).start()

    # ─────────────────────────────────────
    # /scan [TICKER]
    # ─────────────────────────────────────
    elif cmd in ("/scan", "/scan@omegabot"):
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

        # Phase 2A: Add portfolio stats to /status
        holdings_count = 0
        open_opts_count = 0
        try:
            from portfolio import get_all_holdings, get_open_options
            holdings_count = len(get_all_holdings())
            open_opts_count = len(get_open_options())
        except Exception:
            pass  # portfolio not initialized yet — no problem

        msg = (
            f"🤖 Omega 3000 Status\n"
            f"State: {paused_str}\n"
            f"Uptime: {uptime_str}\n"
            f"Last Scan: {scan_str}\n"
            f"Total Scans: {_state['scan_count']}\n"
            f"Confidence Gate: {conf_str}/100\n"
            f"Watchlist: {len(watchlist)} tickers\n"
            f"Holdings: {holdings_count} | Open Options: {open_opts_count}\n"
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
            "── Analysis ──\n"
            "/check AAPL — analyze any ticker (v3 engine)\n"
            "/check SPY bull — with direction hint\n"
            "/scan AAPL — scan single ticker\n"
            "/scan — run full watchlist scan\n"
            "\n── Portfolio ──\n"
            "/hold add AAPL 100 @185.50 — add shares\n"
            "/hold add AAPL 100 @185.50 #wheel — with tag\n"
            "/hold remove AAPL — remove all shares\n"
            "/hold remove AAPL 50 — partial sale\n"
            "/hold list — show all holdings + P/L\n"
            "/holdings — sentiment scan (EMA/VWAP/Vol)\n"
            "/portfolio — full dashboard (fundamentals + P/L)\n"
            "\n── Options ──\n"
            "/sell put AAPL 180 2026-03-21 2.35 — sell CSP\n"
            "/sell call AAPL 195 2026-03-21 1.80 — sell CC\n"
            "/close opt_001 0.15 — buy back option\n"
            "/expire opt_001 — mark expired worthless\n"
            "/assign opt_001 — mark assigned (auto-updates holdings)\n"
            "/options — show open options\n"
            "/options history — show closed P/L\n"
            "\n── Wheel ──\n"
            "/wheel AAPL — wheel history for ticker\n"
            "/wheel — all wheel tickers summary\n"
            "\n── Settings ──\n"
            "/status — bot health + portfolio stats\n"
            "/watchlist — show all tickers\n"
            "/confidence 60 — set min confidence gate\n"
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


# ─────────────────────────────────────────────────────────
# INTERNAL HELPERS (Phase 2A + 2B)
# ─────────────────────────────────────────────────────────

def _no_spot(ticker: str) -> float:
    """Placeholder when get_spot_fn not provided."""
    raise RuntimeError("Price lookup not available — get_spot_fn not wired")


def _no_md_get(url: str, params=None):
    """Placeholder when md_get_fn not provided."""
    raise RuntimeError("MarketData API not available — md_get_fn not wired")


def _safe_run(handler_fn, args, reply_fn, extra_arg, chat_id):
    """
    Wrapper to run a holdings_commands handler in a thread with error handling.
    Handlers have different signatures:
      handle_hold(args, send_fn, get_spot_fn)
      handle_sell(args, send_fn)
      handle_close(args, send_fn)
      etc.
    We pass extra_arg only if the handler accepts it (hold needs get_spot_fn).
    """
    try:
        if extra_arg is not None:
            handler_fn(args, reply_fn, extra_arg)
        else:
            handler_fn(args, reply_fn)
    except Exception as e:
        log.error(f"Portfolio command error: {type(e).__name__}: {e}")
        send_reply(chat_id, f"⚠️ Error: {type(e).__name__}: {str(e)[:120]}")
