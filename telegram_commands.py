# telegram_commands.py
# Telegram command interface for Omega 3000 Bot
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.2 — Multi-account portfolio support
# v3.4 — /spread command for debit spread tracking

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
        return True
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


# ─────────────────────────────────────────────────────────
# ACCOUNT FLAG PARSING (v3.2)
# ─────────────────────────────────────────────────────────

def _parse_account_flag(args: list) -> tuple:
    cleaned = [a for a in args if a.lower() != "--mom"]
    has_mom = len(cleaned) < len(args)
    account = "mom" if has_mom else "brad"
    return account, cleaned


def handle_command(
    user_id:   str,
    chat_id:   str,
    text:      str,
    scan_fn,
    full_scan_fn,
    check_fn,
    watchlist: list,
    get_spot_fn=None,
    md_get_fn=None,
    post_fn=None,
    get_portfolio_chat_id_fn=None,
    get_regime_fn=None,
) -> None:
    if not is_authorized(user_id):
        send_reply(chat_id, "⛔ You are not authorized to use this bot.")
        return

    # Store regime fn reference for threaded handlers
    global _get_regime_ref
    _get_regime_ref = get_regime_fn

    text  = (text or "").strip()
    parts = text.split()
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1:] if len(parts) > 1 else []

    reply = lambda msg: send_reply(chat_id, msg)

    def _portfolio_reply(account: str):
        if post_fn and get_portfolio_chat_id_fn:
            target_chat = get_portfolio_chat_id_fn(account)
            if target_chat:
                return lambda msg: post_fn(msg, chat_id=target_chat)
        return reply

    # ─────────────────────────────────────
    # PORTFOLIO COMMANDS (v3.2: with --mom support)
    # ─────────────────────────────────────

    if cmd in ("/hold", "/hold@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_hold
        _spot = get_spot_fn or _no_spot
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_hold, clean_args, p_reply, _spot, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/sell", "/sell@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_sell
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_sell, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/close", "/close@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_close
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_close, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/roll", "/roll@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_roll
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_roll, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/expire", "/expire@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_expire
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_expire, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/assign", "/assign@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_assign
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_assign, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/options", "/options@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_options
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_options, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/wheel", "/wheel@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_wheel
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_wheel, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/holdings", "/holdings@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_holdings
        _md = md_get_fn or _no_md_get
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_holdings, clean_args, p_reply, _md, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/portfolio", "/portfolio@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_portfolio
        _md = md_get_fn or _no_md_get
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_portfolio, clean_args, p_reply, _md, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /cash — Cash balance & account P/L (v3.3)
    # ─────────────────────────────────────
    if cmd in ("/cash", "/cash@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_cash
        _spot = get_spot_fn or _no_spot
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_cash, clean_args, p_reply, _spot, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /fund — Mutual Fund / ETF tracker (v3.2)
    # ─────────────────────────────────────
    if cmd in ("/fund", "/fund@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_fund
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_fund, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /spread — Debit Spread Tracking (v3.4)
    # ─────────────────────────────────────
    if cmd in ("/spread", "/spread@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_spread
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_spread, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /risk — Portfolio Risk Dashboard (v3.5)
    # ─────────────────────────────────────
    if cmd in ("/risk", "/risk@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_risk
        p_reply = _portfolio_reply(account)
        # handle_risk(args, send_fn, get_regime_fn, account)
        threading.Thread(
            target=_safe_run_with_regime,
            args=(handle_risk, clean_args, p_reply, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /regime — Market Regime Status (v3.5)
    # ─────────────────────────────────────
    if cmd in ("/regime", "/regime@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_regime
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run_with_regime,
            args=(handle_regime, clean_args, p_reply, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /journal — Trade Journal & Analytics (v3.5)
    # ─────────────────────────────────────
    if cmd in ("/journal", "/journal@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_journal
        p_reply = _portfolio_reply(account)
        threading.Thread(
            target=_safe_run,
            args=(handle_journal, clean_args, p_reply, None, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /check TICKER [bull|bear]
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

        brad_holdings = 0
        brad_opts = 0
        brad_spreads = 0
        mom_holdings = 0
        mom_opts = 0
        mom_spreads = 0
        try:
            from portfolio import get_all_holdings, get_open_options, get_open_spreads
            brad_holdings = len(get_all_holdings(account="brad"))
            brad_opts = len(get_open_options(account="brad"))
            brad_spreads = len(get_open_spreads(account="brad"))
            mom_holdings = len(get_all_holdings(account="mom"))
            mom_opts = len(get_open_options(account="mom"))
            mom_spreads = len(get_open_spreads(account="mom"))
        except Exception:
            pass

        msg = (
            f"🤖 Omega 3000 Status\n"
            f"State: {paused_str}\n"
            f"Uptime: {uptime_str}\n"
            f"Last Scan: {scan_str}\n"
            f"Total Scans: {_state['scan_count']}\n"
            f"Confidence Gate: {conf_str}/100\n"
            f"Watchlist: {len(watchlist)} tickers\n"
            f"Brad: {brad_holdings} hold | {brad_opts} opts | {brad_spreads} spreads\n"
            f"Mom:  {mom_holdings} hold | {mom_opts} opts | {mom_spreads} spreads\n"
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

    elif cmd in ("/pause", "/pause@omegabot"):
        _state["paused"] = True
        send_reply(chat_id, "⏸ Bot paused. Scheduled scans will be skipped. Use /resume to restart.")

    elif cmd in ("/resume", "/resume@omegabot"):
        _state["paused"] = False
        send_reply(chat_id, "▶️ Bot resumed. Scheduled scans will run normally.")

    elif cmd in ("/help", "/help@omegabot", "/start"):
        msg = (
            "🤖 Omega 3000 Commands:\n\n"
            "── Analysis ──\n"
            "/check AAPL — analyze any ticker (v3 engine)\n"
            "/check SPY bull — with direction hint\n"
            "/scan AAPL — scan single ticker\n"
            "/scan — run full watchlist scan\n"
            "\n── Portfolio (add --mom for mom's account) ──\n"
            "/hold add AAPL 100 @185.50 — add shares\n"
            "/hold remove AAPL — remove shares\n"
            "/hold list — show all holdings + P/L\n"
            "/holdings — sentiment scan\n"
            "/portfolio — full dashboard\n"
            "\n── Cash & Account P/L ──\n"
            "/cash deposit 50000 — set total deposited\n"
            "/cash 12345 — update cash balance\n"
            "/cash — show full account P/L\n"
            "\n── Mutual Funds / ETFs ──\n"
            "/fund set 50000 — set total invested\n"
            "/fund update 54200 — update current value\n"
            "/fund — show P/L\n"
            "\n── Debit Spreads (v3.4) ──\n"
            "/spread add call AAPL 570/571 0.65 2026-03-14 x3\n"
            "/spread add put AAPL 580/579 0.55 2026-03-14 x2\n"
            "/spread add AAPL 570/571 0.65 2026-03-14  (defaults to call)\n"
            "/spread close sp_001 0.91 — close at price\n"
            "/spread stop sp_001 — stopped out\n"
            "/spread expire sp_001 — expired ITM\n"
            "/spread expire sp_001 otm — expired OTM\n"
            "/spread list — show open spreads\n"
            "/spread history — closed P/L\n"
            "/spread summary — win rate + totals\n"
            "\n── Options (wheel) ──\n"
            "/sell put AAPL 180 2026-03-21 2.35\n"
            "/sell call AAPL 195 2026-03-21 1.80\n"
            "/roll opt_001 2026-04-17 185 2.50\n"
            "/close opt_001 0.15 — buy back\n"
            "/expire opt_001 — expired worthless\n"
            "/assign opt_001 — assigned\n"
            "/options — open options\n"
            "/options history — closed P/L\n"
            "/wheel AAPL — wheel analytics\n"
            "/wheel — all wheels summary\n"
            "\n── Settings ──\n"
            "/status — bot health + portfolio stats\n"
            "/watchlist — show tickers\n"
            "/confidence 60 — set min confidence\n"
            "/pause | /resume — control scheduled scans\n"
            "\n── Risk & Regime (v3.5) ──\n"
            "/risk — portfolio risk dashboard\n"
            "/regime — market regime (VIX + ADX)\n"
            "/journal — trade analytics + backtest data\n"
            "/journal AAPL — per-ticker stats\n"
            "/journal signals — recent signal log\n"
            "/journal trades — recent trade log\n"
            "/journal attrs — Greeks P/L attribution\n"
            "/help — this message\n\n"
            "💡 --mom on any portfolio command for mom's account\n"
            "⚡ TV signals auto-warn if you have opposite spreads open\n"
            "🛡️ Risk limits auto-block trades that exceed exposure caps\n"
            "— Not financial advice —"
        )
        send_reply(chat_id, msg)

    else:
        send_reply(chat_id,
            f"❓ Unknown command: {cmd}\nType /help for available commands."
        )


# ─────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────

def _no_spot(ticker: str) -> float:
    raise RuntimeError("Price lookup not available — get_spot_fn not wired")


def _no_md_get(url: str, params=None):
    raise RuntimeError("MarketData API not available — md_get_fn not wired")


def _safe_run(handler_fn, args, reply_fn, extra_arg, chat_id, account="brad"):
    try:
        if extra_arg is not None:
            handler_fn(args, reply_fn, extra_arg, account=account)
        else:
            handler_fn(args, reply_fn, account=account)
    except Exception as e:
        log.error(f"Portfolio command error: {type(e).__name__}: {e}")
        send_reply(chat_id, f"⚠️ Error: {type(e).__name__}: {str(e)[:120]}")


def _safe_run_with_regime(handler_fn, args, reply_fn, chat_id, account="brad"):
    """Wrapper for handlers that need get_regime_fn (handle_risk, handle_regime)."""
    try:
        handler_fn(args, reply_fn, get_regime_fn=_get_regime_ref, account=account)
    except Exception as e:
        log.error(f"Command error: {type(e).__name__}: {e}")
        send_reply(chat_id, f"⚠️ Error: {type(e).__name__}: {str(e)[:120]}")


# Reference to get_regime_fn — set by handle_command on each call
_get_regime_ref = None
