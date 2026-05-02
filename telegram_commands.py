# telegram_commands.py
# Telegram command interface for Omega 3000 Bot
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.2 — Multi-account portfolio support
# v3.4 — /spread command for debit spread tracking
# v3.8 — /check runs both bull + bear, engine decides
# v3.9 — /em command for 0DTE Expected Move (SPY & QQQ)

import os
import json
import logging
import threading
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = [
    x.strip() for x in
    os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip()
]

_STATE_KEY = "omega:telegram_state"
_state_lock = threading.Lock()
_state_store_get = None
_state_store_set = None

_state = {
    "paused": False,
    "confidence_gate": int(os.getenv("MIN_CONFIDENCE_TO_POST", "40") or 40),
    "last_scan_time": None,
    "scan_count": 0,
    "last_scan_posted": 0,
    "last_scan_total": 0,
    "start_time": datetime.now(timezone.utc).isoformat(),
}

# Reference to get_regime_fn — set by handle_command on each call
_get_regime_ref = None


def init_shared_state(store_get_fn, store_set_fn):
    global _state_store_get, _state_store_set
    _state_store_get = store_get_fn
    _state_store_set = store_set_fn
    with _state_lock:
        loaded = _load_state_unlocked()
        _state.update(loaded)
        _save_state_unlocked()


def _load_state_unlocked() -> dict:
    if not _state_store_get:
        return dict(_state)
    try:
        raw = _state_store_get(_STATE_KEY)
        if not raw:
            return dict(_state)
        data = json.loads(raw)
        if isinstance(data, dict):
            merged = dict(_state)
            merged.update(data)
            return merged
    except Exception as e:
        log.warning(f"state load error: {e}")
    return dict(_state)


def _save_state_unlocked():
    if not _state_store_set:
        return
    try:
        _state_store_set(_STATE_KEY, json.dumps(_state))
    except Exception as e:
        log.warning(f"state save error: {e}")


def _mutate_state(mutator):
    with _state_lock:
        latest = _load_state_unlocked()
        _state.update(latest)
        mutator(_state)
        _save_state_unlocked()
        return dict(_state)


def get_state() -> dict:
    with _state_lock:
        latest = _load_state_unlocked()
        _state.update(latest)
        return dict(_state)


def set_last_scan(posted: int, total: int):
    def _update(s):
        s["last_scan_time"] = datetime.now(timezone.utc).isoformat()
        s["scan_count"] = int(s.get("scan_count", 0) or 0) + 1
        s["last_scan_posted"] = posted
        s["last_scan_total"] = total
    _mutate_state(_update)


def is_paused() -> bool:
    return bool(get_state().get("paused", False))


def get_confidence_gate() -> int:
    try:
        return int(get_state().get("confidence_gate", 40) or 40)
    except Exception:
        return 40


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


def send_document(chat_id: str, filepath: str, caption: str = ""):
    """Send a file as a Telegram document."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        with open(filepath, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        log.warning(f"send_document error: {e}")


def register_webhook(bot_url: str, webhook_secret: str):
    if not TELEGRAM_BOT_TOKEN or not bot_url:
        log.warning("Cannot register webhook — BOT_URL or TOKEN missing")
        return

    webhook_url = f"{bot_url.rstrip('/')}/telegram_webhook/{webhook_secret}"
    try:
        r    = requests.post(
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
# ACCOUNT FLAG PARSING
# ─────────────────────────────────────────────────────────

def _parse_account_flag(args: list) -> tuple:
    cleaned = [a for a in args if a.lower() != "-mom"]
    has_mom = len(cleaned) < len(args)
    account = "mom" if has_mom else "brad"
    return account, cleaned


# ─────────────────────────────────────────────────────────
# MAIN COMMAND HANDLER
# ─────────────────────────────────────────────────────────

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
    post_em_card_fn=None,
    post_monitor_card_fn=None,
    post_checkswing_card_fn=None,
    thesis_engine=None,
    post_income_scan_fn=None,
    post_income_score_fn=None,
    post_options_map_fn=None,
) -> None:
    if not is_authorized(user_id):
        send_reply(chat_id, "⛔ You are not authorized to use this bot.")
        return

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
    # PORTFOLIO COMMANDS
    # ─────────────────────────────────────

    if cmd in ("/hold", "/hold@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_hold
        threading.Thread(
            target=_safe_run,
            args=(handle_hold, clean_args, _portfolio_reply(account), get_spot_fn or _no_spot, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/sell", "/sell@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_sell
        threading.Thread(
            target=_safe_run,
            args=(handle_sell, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/close", "/close@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_close
        threading.Thread(
            target=_safe_run,
            args=(handle_close, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/roll", "/roll@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_roll
        threading.Thread(
            target=_safe_run,
            args=(handle_roll, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/expire", "/expire@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_expire
        threading.Thread(
            target=_safe_run,
            args=(handle_expire, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/assign", "/assign@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_assign
        threading.Thread(
            target=_safe_run,
            args=(handle_assign, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/options", "/options@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_options
        threading.Thread(
            target=_safe_run,
            args=(handle_options, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/wheel", "/wheel@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_wheel
        threading.Thread(
            target=_safe_run,
            args=(handle_wheel, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/holdings", "/holdings@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_holdings
        threading.Thread(
            target=_safe_run,
            args=(handle_holdings, clean_args, _portfolio_reply(account), md_get_fn or _no_md_get, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/portfolio", "/portfolio@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_portfolio
        threading.Thread(
            target=_safe_run,
            args=(handle_portfolio, clean_args, _portfolio_reply(account), md_get_fn or _no_md_get, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/cash", "/cash@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_cash
        threading.Thread(
            target=_safe_run,
            args=(handle_cash, clean_args, _portfolio_reply(account), get_spot_fn or _no_spot, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/fund", "/fund@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_fund
        threading.Thread(
            target=_safe_run,
            args=(handle_fund, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/spread", "/spread@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_spread
        threading.Thread(
            target=_safe_run,
            args=(handle_spread, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/risk", "/risk@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_risk
        threading.Thread(
            target=_safe_run_with_regime,
            args=(handle_risk, clean_args, _portfolio_reply(account), chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/regime", "/regime@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_regime
        threading.Thread(
            target=_safe_run_with_regime,
            args=(handle_regime, clean_args, _portfolio_reply(account), chat_id, account),
            daemon=True,
        ).start()
        return

    if cmd in ("/journal", "/journal@omegabot"):
        account, clean_args = _parse_account_flag(args)
        from holdings_commands import handle_journal
        threading.Thread(
            target=_safe_run,
            args=(handle_journal, clean_args, _portfolio_reply(account), None, chat_id, account),
            daemon=True,
        ).start()
        return

    # ─────────────────────────────────────
    # /watchmap [TICKER] [intraday|diag|both] [compact]
    # Phase 3.0-B/C — Watch Map sidecar. Context only.
    # ─────────────────────────────────────
    if cmd in ("/watchmap", "/watchmap@omegabot", "/optionsmap", "/optionsmap@omegabot", "/omap", "/omap@omegabot", "/emap", "/emap@omegabot"):
        if not post_options_map_fn:
            reply("⚠️ Watch Map function not wired — post_options_map_fn missing.")
            return
        ticker = "SPY"
        route = None
        route_words = {"intraday", "diag", "diagnosis", "both", "main"}
        compact = False
        for arg in args:
            al = arg.lower().strip()
            if al in route_words:
                route = al
            elif al in {"compact", "short", "brief"}:
                compact = True
            elif arg.upper().replace(".", "").isalpha():
                ticker = arg.upper()
        mode = "compact" if compact else "full"
        reply(f"🧭 Building Watch Map for {ticker} ({route or 'default route'}, {mode})...")

        def run_options_map():
            try:
                post_options_map_fn(ticker, route, compact=compact)
            except Exception as e:
                log.error(f"/watchmap {ticker}: {e}", exc_info=True)
                reply(f"⚠️ Watch Map error for {ticker}: {type(e).__name__}")

        threading.Thread(target=run_options_map, daemon=True).start()
        return

    # ─────────────────────────────────────
    # /em [TICKER] [morning|afternoon] — 0DTE Expected Move + Trade Card
    # ─────────────────────────────────────
    if cmd in ("/em", "/em@omegabot"):
        if not post_em_card_fn:
            reply("⚠️ EM function not wired — post_em_card_fn missing.")
            return

        from app import EM_TICKERS

        # Parse args: /em | /em SPY | /em morning | /em SPY morning
        # First arg could be a ticker (all-caps, no digits only) or a session word
        SESSION_WORDS = {"morning", "afternoon", "manual"}
        ticker_arg  = None
        session_arg = "manual"

        for arg in args:
            al = arg.lower()
            if al in SESSION_WORDS:
                session_arg = al
            elif arg.upper().replace(".", "").isalpha():
                ticker_arg = arg.upper()

        # Determine ticker list
        if ticker_arg:
            tickers = [ticker_arg]
        else:
            tickers = EM_TICKERS  # default: SPY + QQQ

        reply(f"📐 Fetching 0DTE EM + Trade setup for {', '.join(tickers)} ({session_arg})...")

        def run_em():
            for ticker in tickers:
                try:
                    post_em_card_fn(ticker, session_arg)
                except Exception as e:
                    log.error(f"/em {ticker}: {e}")
                    reply(f"⚠️ EM error for {ticker}: {type(e).__name__}")

        threading.Thread(target=run_em, daemon=True).start()
        return

    # /monitorlong TICKER — swing outlook, expiry closest to 21 DTE
    # ─────────────────────────────────────
    if cmd in ("/monitorlong", "/monitorlong@omegabot"):
        if not post_monitor_card_fn:
            reply("⚠️ Monitor function not wired — post_monitor_card_fn missing.")
            return
        if not args:
            reply("Usage: /monitorlong IREN\n15–30 day swing outlook on the nearest monthly expiration.")
            return
        ticker = args[0].upper()
        reply(f"📅 Fetching swing monitor card for {ticker} (~21 DTE)...")

        def run_monitor_long():
            try:
                post_monitor_card_fn(ticker, "long")
            except Exception as e:
                log.error(f"/monitorlong {ticker}: {e}")
                reply(f"⚠️ Monitor error for {ticker}: {type(e).__name__}")

        threading.Thread(target=run_monitor_long, daemon=True).start()
        return

    # /monitorshort TICKER — near-term outlook, nearest available expiry
    # ─────────────────────────────────────
    if cmd in ("/monitorshort", "/monitorshort@omegabot"):
        if not post_monitor_card_fn:
            reply("⚠️ Monitor function not wired — post_monitor_card_fn missing.")
            return
        if not args:
            reply("Usage: /monitorshort IREN\nNear-term outlook on the nearest available expiration.")
            return
        ticker = args[0].upper()
        reply(f"⚡ Fetching near-term monitor card for {ticker} (nearest exp)...")

        def run_monitor_short():
            try:
                post_monitor_card_fn(ticker, "short")
            except Exception as e:
                log.error(f"/monitorshort {ticker}: {e}")
                reply(f"⚠️ Monitor error for {ticker}: {type(e).__name__}")

        threading.Thread(target=run_monitor_short, daemon=True).start()
        return

    # ─────────────────────────────────────
    # /checkswing TICKER [bull|bear]
    # ─────────────────────────────────────
    if cmd in ("/checkswing", "/checkswing@omegabot"):
        if not post_checkswing_card_fn:
            reply("⚠️ Swing check function not wired — post_checkswing_card_fn missing.")
            return
        if not args:
            reply(
                "Usage: /checkswing GLD\n"
                "       /checkswing GLD bull\n"
                "       /checkswing GLD bear\n\n"
                "Direction is optional. No direction = evaluate both sides and return the best valid swing setup."
            )
            return
        ticker = args[0].upper()
        forced_direction = None
        if len(args) > 1:
            d = args[1].lower()
            if d not in ("bull", "bear"):
                reply(f"⚠️ Direction must be 'bull' or 'bear', got '{d}'")
                return
            forced_direction = d
        dir_label = forced_direction.upper() if forced_direction else "BULL + BEAR"
        reply(f"🧭 Checking swing setup for {ticker} ({dir_label})...")

        def run_check_swing():
            try:
                post_checkswing_card_fn(ticker, forced_direction)
            except Exception as e:
                log.error(f"/checkswing {ticker} {forced_direction or 'both'}: {e}")
                reply(f"⚠️ Swing check error for {ticker}: {type(e).__name__}")

        threading.Thread(target=run_check_swing, daemon=True).start()
        return

    # ─────────────────────────────────────
    # /tradecard TICKER — retrieve cached full trade card from digest
    # ─────────────────────────────────────
    if cmd in ("/tradecard", "/tradecard@omegabot"):
        if not args:
            reply(
                "Usage: /tradecard SPY\n"
                "Retrieves the full trade card for a ticker from the latest signal digest.\n"
                "Cards are cached for 1 hour after a signal fires."
            )
            return

        ticker = args[0].upper()
        # Look up cached card from store
        try:
            from app import store_get
            cache_key = f"tradecard:{ticker}"
            cached_card = store_get(cache_key)
            if cached_card:
                reply(cached_card)
                log.info(f"/tradecard {ticker}: card retrieved from cache")
            else:
                reply(
                    f"❌ No cached trade card for {ticker}.\n"
                    f"Cards are available after a signal fires and appear in the digest.\n"
                    f"Use /check {ticker} to run a fresh analysis."
                )
        except Exception as e:
            log.error(f"/tradecard {ticker}: {e}")
            reply(f"⚠️ Error retrieving card: {type(e).__name__}")
        return

    # ─────────────────────────────────────
    # /check TICKER [bull|bear]
    # ─────────────────────────────────────
    if cmd in ("/check", "/check@omegabot"):
        if not args:
            reply(
                "Usage: /check AAPL\n"
                "       /check AAPL bull  — force bull only\n"
                "       /check AAPL bear  — force bear only\n\n"
                "By default runs both directions — engine posts\n"
                "whichever setups meet your confidence gate."
            )
            return

        ticker           = args[0].upper()
        forced_direction = None

        if len(args) > 1:
            d = args[1].lower()
            if d not in ("bull", "bear"):
                reply(f"⚠️ Direction must be 'bull' or 'bear', got '{d}'")
                return
            forced_direction = d

        directions = [forced_direction] if forced_direction else ["bull", "bear"]
        dir_label  = forced_direction.upper() if forced_direction else "BULL + BEAR"
        reply(f"🔍 Checking {ticker} ({dir_label})...")

        def run_check():
            any_posted = False
            results    = []
            for direction in directions:
                try:
                    result = check_fn(ticker, direction)
                    if result.get("posted") and result.get("card"):
                        # v4.3: Actually post the card — check_fn builds it but doesn't send
                        if post_fn:
                            post_fn(result["card"])
                        else:
                            reply(result["card"])
                        any_posted = True
                    elif result.get("posted"):
                        # posted=True but no card text (shouldn't happen, but handle it)
                        any_posted = True
                    else:
                        reason = result.get("reason") or result.get("error") or "no valid setup"
                        conf   = result.get("confidence")
                        results.append((direction, reason, conf))
                except Exception as e:
                    log.error(f"/check {ticker} {direction}: {e}")
                    results.append((direction, f"{type(e).__name__}", None))

            if not any_posted:
                lines = [f"❌ {ticker} — no setups found"]
                for direction, reason, conf in results:
                    dir_emoji = "🐂" if direction == "bull" else "🐻"
                    line = f"{dir_emoji} {direction.upper()}: {reason}"
                    if conf is not None:
                        line += f" (conf {conf}/100)"
                    lines.append(line)
                reply("\n".join(lines))

        threading.Thread(target=run_check, daemon=True).start()
        return

    # ─────────────────────────────────────
    # /scan [TICKER]
    # ─────────────────────────────────────
    if cmd in ("/scan", "/scan@omegabot"):
        if args:
            ticker = args[0].upper()
            reply(f"🔍 Scanning {ticker}...")
            result = scan_fn(ticker)
            if result.get("posted"):
                reply(f"✅ {ticker} scan card posted above.")
            else:
                reason = result.get("skipped") or result.get("error") or "no setup found"
                reply(f"ℹ️ {ticker}: {reason}")
        else:
            if is_paused():
                reply("⏸ Bot is paused. Use /resume first.")
                return
            reply(f"🔍 Scanning full watchlist ({len(watchlist)} tickers)...")
            threading.Thread(target=full_scan_fn, daemon=True).start()
        return

    # ─────────────────────────────────────
    # /status
    # ─────────────────────────────────────
    if cmd in ("/status", "/status@omegabot"):
        state = get_state()
        start_time = state.get("start_time")
        if isinstance(start_time, str):
            try:
                start_time = datetime.fromisoformat(start_time)
            except Exception:
                start_time = datetime.now(timezone.utc)
        uptime = datetime.now(timezone.utc) - start_time
        uptime_str = f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m"

        last_scan = state.get("last_scan_time")
        if isinstance(last_scan, str):
            try:
                last_scan = datetime.fromisoformat(last_scan)
            except Exception:
                last_scan = None
        if last_scan:
            mins_ago = int((datetime.now(timezone.utc) - last_scan).total_seconds() / 60)
            scan_str = f"{mins_ago}m ago ({state.get('last_scan_posted', 0)}/{state.get('last_scan_total', 0)} posted)"
        else:
            scan_str = "No scan run yet"

        brad_holdings = brad_opts = brad_spreads = 0
        mom_holdings  = mom_opts  = mom_spreads  = 0
        try:
            from portfolio import get_all_holdings, get_open_options, get_open_spreads
            brad_holdings = len(get_all_holdings(account="brad"))
            brad_opts     = len(get_open_options(account="brad"))
            brad_spreads  = len(get_open_spreads(account="brad"))
            mom_holdings  = len(get_all_holdings(account="mom"))
            mom_opts      = len(get_open_options(account="mom"))
            mom_spreads   = len(get_open_spreads(account="mom"))
        except Exception:
            pass

        reply(
            f"🤖 Omega 3000 Status\n"
            f"State: {'⏸ PAUSED' if state.get('paused') else '▶️ Running'}\n"
            f"Uptime: {uptime_str}\n"
            f"Last Scan: {scan_str}\n"
            f"Total Scans: {state.get('scan_count', 0)}\n"
            f"Confidence Gate: {state.get('confidence_gate', 40)}/100\n"
            f"Watchlist: {len(watchlist)} tickers\n"
            f"Brad: {brad_holdings} hold | {brad_opts} opts | {brad_spreads} spreads\n"
            f"Mom:  {mom_holdings} hold | {mom_opts} opts | {mom_spreads} spreads\n"
            f"Admins: {len(TELEGRAM_ADMIN_IDS)} authorized"
        )
        return

    # ─────────────────────────────────────
    # /watchlist
    # ─────────────────────────────────────
    if cmd in ("/watchlist", "/watchlist@omegabot"):
        if not watchlist:
            reply("⚠️ Watchlist is empty.")
            return
        chunks = [watchlist[i:i + 20] for i in range(0, len(watchlist), 20)]
        for i, chunk in enumerate(chunks):
            reply(f"📋 Watchlist ({i * 20 + 1}–{i * 20 + len(chunk)}):\n" + ", ".join(chunk))
        return

    # ─────────────────────────────────────
    # /confidence [value]
    # ─────────────────────────────────────
    if cmd in ("/confidence", "/confidence@omegabot"):
        if not args:
            reply(
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
            reply(
                f"✅ Confidence gate updated: {old} → {val}/100\n"
                f"Trades below {val}/100 will be suppressed."
            )
        except ValueError:
            reply("⚠️ Usage: /confidence 60 (must be 0–100)")
        return

    if cmd in ("/pause", "/pause@omegabot"):
        _mutate_state(lambda s: s.__setitem__("paused", True))
        reply("⏸ Bot paused. Scheduled scans will be skipped. Use /resume to restart.")
        return

    if cmd in ("/resume", "/resume@omegabot"):
        _mutate_state(lambda s: s.__setitem__("paused", False))
        reply("▶️ Bot resumed. Scheduled scans will run normally.")
        return

    # ─────────────────────────────────────
    # /cachestats — API cache hit rates
    # ─────────────────────────────────────
    if cmd in ("/cachestats", "/cachestats@omegabot"):
        try:
            from app import _cached_md
            stats = _cached_md.get_stats()
            lines = ["📊 API Cache Stats"]
            for name, s in stats.items():
                lines.append(f"  {name}: {s['hits']} hits / {s['misses']} miss ({s['hit_rate']:.0%}) | {s['size']} cached")
            reply("\n".join(lines))
        except Exception as e:
            reply(f"⚠️ Cache stats error: {e}")
        return

    # ─────────────────────────────────────
    # /monitor — Thesis Monitor
    # ─────────────────────────────────────
    if cmd in ("/monitor", "/monitor@omegabot"):
        if not thesis_engine:
            reply("⚠️ Thesis monitor not initialized.")
            return

        if args and args[0].lower() == "stop":
            from thesis_monitor import get_daemon
            daemon = get_daemon()
            if daemon:
                daemon.stop()
                reply("⏹️ Thesis monitor stopped. Alerts paused until /monitor start.")
            else:
                reply("⚠️ No monitor daemon running.")
            return

        if args and args[0].lower() == "start":
            from thesis_monitor import get_daemon
            daemon = get_daemon()
            if daemon:
                daemon.start()
                reply("▶️ Thesis monitor started. Polling every 5 min during market hours.")
            else:
                reply("⚠️ Monitor daemon not initialized. Restart bot.")
            return

        if args and args[0].lower() == "trades":
            ticker = args[1].upper() if len(args) > 1 else "SPY"
            try:
                spot = get_spot_fn(ticker) if get_spot_fn else 0
                reply(thesis_engine.format_trades(ticker, price=spot))
            except Exception as e:
                log.error(f"Monitor trades error: {e}", exc_info=True)
                reply(f"⚠️ Trades error: {e}")
            return

        if args and args[0].lower() == "close":
            ticker = args[1].upper() if len(args) > 1 else "SPY"
            reason = " ".join(args[2:]) if len(args) > 2 else "Manual close"
            try:
                spot = get_spot_fn(ticker) if get_spot_fn else None
                result = thesis_engine.close_trade(ticker, price=spot, reason=reason)
                reply(f"📊 {ticker}: {result}")
            except Exception as e:
                log.error(f"Monitor close error: {e}", exc_info=True)
                reply(f"⚠️ Close error: {e}")
            return

        if args and args[0].lower() == "guidance":
            ticker = args[1].upper() if len(args) > 1 else "SPY"
            try:
                spot = get_spot_fn(ticker) if get_spot_fn else 0
                if not spot or spot <= 0:
                    reply(f"⚠️ Could not fetch spot price for {ticker}.")
                    return
                guidance = thesis_engine.build_guidance(ticker, spot)
                out_lines = [f"📡 {ticker} — THESIS GUIDANCE @ ${spot:.2f}", ""]
                for item in guidance:
                    if item["type"] == "divider":
                        out_lines.append(f"\n{item['text']}")
                    elif item["type"] == "critical":
                        out_lines.append(f"🔥 {item['text']}")
                    elif item["type"] == "warning":
                        out_lines.append(f"⚠️ {item['text']}")
                    elif item["type"] in ("bullish",):
                        out_lines.append(f"🟢 {item['text']}")
                    elif item["type"] in ("bearish",):
                        out_lines.append(f"🔴 {item['text']}")
                    elif item["type"] == "time":
                        out_lines.append(f"🕐 {item['text']}")
                    elif item["type"] == "context":
                        out_lines.append(f"📋 {item['text']}")
                    else:
                        out_lines.append(f"  {item['text']}")
                out_lines.append("")
                out_lines.append("— Not financial advice —")
                reply("\n".join(out_lines))
            except Exception as e:
                log.error(f"Monitor guidance error: {e}", exc_info=True)
                reply(f"⚠️ Guidance error: {e}")
            return

        # Default: show status for all monitored tickers
        if args:
            tickers_to_show = [args[0].upper()]
        else:
            tickers_to_show = thesis_engine.get_monitored_tickers()
            if not tickers_to_show:
                tickers_to_show = ["SPY", "QQQ"]
        for t in tickers_to_show:
            reply(thesis_engine.format_status(t))
        return

    # ─────────────────────────────────────
    # /income [TICKER] — Income trade scanner
    # ─────────────────────────────────────
    if cmd in ("/income", "/income@omegabot"):
        if not post_income_scan_fn:
            reply("⚠️ Income scanner not wired — post_income_scan_fn missing.")
            return
        ticker = args[0].upper() if args else None
        label = ticker or "full universe"
        reply(f"📊 Running income scan ({label})...")
        post_income_scan_fn(chat_id, ticker)
        return

    # ─────────────────────────────────────
    # /score TICKER put|call STRIKE WIDTH CREDIT [EXPIRY]
    # ─────────────────────────────────────
    if cmd in ("/score", "/score@omegabot"):
        if not post_income_score_fn:
            reply("⚠️ Income scorer not wired — post_income_score_fn missing.")
            return
        if len(args) < 5:
            reply(
                "Usage: /score MRNA put 47 2 0.45\n"
                "       /score MRNA put 47 2 0.45 2026-04-11\n\n"
                "Args: TICKER put|call SHORT_STRIKE WIDTH CREDIT [EXPIRY]\n\n"
                "Scores a specific credit spread with full automated context:\n"
                "earnings risk, chain liquidity, support quality, regime, and more."
            )
            return
        try:
            ticker = args[0].upper()
            direction = args[1].lower()
            if direction not in ("put", "call"):
                reply(f"⚠️ Direction must be 'put' or 'call', got '{direction}'")
                return
            trade_type = "bull_put" if direction == "put" else "bear_call"
            # Strip common user formatting: @ $ ,
            clean = [a.replace("@", "").replace("$", "").replace(",", "") for a in args[2:]]
            clean = [a for a in clean if a]  # remove empties from standalone "@"
            short_strike = float(clean[0])
            width = float(clean[1])
            credit = float(clean[2])
            expiry = clean[3] if len(clean) > 3 else None

            if trade_type == "bull_put":
                long_strike = short_strike - width
            else:
                long_strike = short_strike + width
            reply(f"📋 Scoring {ticker} {direction} {short_strike}/{long_strike} @ ${credit:.2f}...")
            post_income_score_fn(chat_id, ticker, trade_type, short_strike, width, credit, expiry)
        except (ValueError, IndexError) as e:
            reply(f"⚠️ Parse error: {e}\nUsage: /score MRNA put 47 2 0.45")
        return


    # ─────────────────────────────────────
    # /recresults [YYYY-MM-DD] | /recweek | /recmonth | /recopen | /shadowedge
    # Uses reply_long() to auto-split messages over 4096 chars.
    # ─────────────────────────────────────
    if cmd in ("/recresults", "/recresults@omegabot"):
        try:
            from app import _rec_tracker
            from recommendation_tracker import generate_daily_report, reply_long
            if _rec_tracker is None:
                reply("⚠️ Bot idea tracker not initialized.")
                return
            date_arg = args[0] if args else None
            report = generate_daily_report(_rec_tracker, date_str=date_arg)
            reply_long(reply, report)
        except Exception as e:
            reply(f"⚠️ Bot idea tracking report error: {e}")
        return

    if cmd in ("/recweek", "/recweek@omegabot", "/recmonth", "/recmonth@omegabot"):
        try:
            from app import _rec_tracker
            from recommendation_tracker import generate_weekly_summary, reply_long
            if _rec_tracker is None:
                reply("⚠️ Bot idea tracker not initialized.")
                return
            days = 7 if "week" in cmd else 30
            reply_long(reply, generate_weekly_summary(_rec_tracker, days=days))
        except Exception as e:
            reply(f"⚠️ Bot idea tracking summary error: {e}")
        return

    if cmd in ("/recopen", "/recopen@omegabot"):
        try:
            from app import _rec_tracker
            from recommendation_tracker import generate_open_positions_report, reply_long
            if _rec_tracker is None:
                reply("⚠️ Bot idea tracker not initialized.")
                return
            reply_long(reply, generate_open_positions_report(_rec_tracker))
        except Exception as e:
            reply(f"⚠️ Open bot idea report error: {e}")
        return

    if cmd in ("/shadowedge", "/shadowedge@omegabot"):
        try:
            from app import _rec_tracker
            from recommendation_tracker import (
                analyze_shadow_edge_from_campaigns,
                format_shadow_edge_report,
                reply_long,
            )
            if _rec_tracker is None:
                reply("⚠️ Bot idea tracker not initialized.")
                return
            lookback = int(args[0]) if args else 30
            analysis = analyze_shadow_edge_from_campaigns(
                _rec_tracker, lookback_days=lookback,
            )
            reply_long(reply, format_shadow_edge_report(analysis))
        except Exception as e:
            reply(f"⚠️ Shadow edge error: {e}")
        return

    if cmd in ("/help", "/help@omegabot", "/start"):
        reply(
            "🤖 Omega 3000 Commands:\n\n"
            "── Analysis ──\n"
            "/check AAPL — auto bull+bear, engine decides\n"
            "/check AAPL bull — force bull only\n"
            "/check AAPL bear — force bear only\n"
            "/tradecard SPY — full card from digest\n"
            "/scan AAPL — scan single ticker\n"
            "/scan — run full watchlist scan\n"
            "\n── 0DTE Expected Move ──\n"
            "/em — EM + Trade card for SPY & QQQ (0DTE)\n"
            "/em SPY — EM + Trade card for any symbol\n"
            "/em morning — today's EM (hours remaining)\n"
            "/em afternoon — next day full-session EM\n"
            "/em SPY morning — specific ticker + session\n"
            "  Auto-fires: 8:45 AM CT (today) & 2:45 PM CT (next day)\n"
            "\n── Thesis Monitor (NEW) ──\n"
            "/monitor — show thesis status for all monitored tickers\n"
            "/monitor SPY — show status for specific ticker\n"
            "/monitor trades — show active trades & P&L\n"
            "/monitor trades SPY — trades for specific ticker\n"
            "/monitor close SPY — close most recent open trade\n"
            "/monitor guidance — plain English action guidance\n"
            "/monitor guidance SPY — guidance for specific ticker\n"
            "/monitor start — resume monitoring\n"
            "/monitor stop — pause monitoring\n"
            "  Auto-monitors SPY & QQQ from scheduled cards.\n"
            "  Run /em AAPL to add any ticker to monitoring.\n"
            "  Detects: entries, exits, scale, trail, invalidation.\n"
            "\n── Position Monitor ──\n"
            "/monitorlong GLD — monitoring / wheel outlook with ~21-day thesis\n"
            "/monitorshort GLD — near-term management view for roll / close-early decisions\n"
            "/checkswing GLD — on-demand true swing setup check\n"
            "/checkswing GLD bull — force bullish swing thesis only\n"
            "  /monitorlong keeps wheel data and thesis context; /checkswing runs the actual swing engine.\n"
            "  Works on any symbol with liquid options.\n"
            "\n── Portfolio (add -mom for mom's account) ──\n"
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
            "\n── Debit Spreads ──\n"
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
            "/cachestats — API cache hit rates\n"
            "/watchlist — show tickers\n"
            "/confidence 60 — set min confidence\n"
            "/pause | /resume — control scheduled scans\n"
            "/exportlogs — download full log file\n"
            "/exportlogs SWEEP — filtered by keyword\n"
            "\n── Risk & Regime ──\n"
            "/risk — portfolio risk dashboard\n"
            "/regime — market regime (VIX + ADX)\n"
            "/journal — trade analytics + backtest data\n"
            "/journal AAPL — per-ticker stats\n"
            "/journal signals — recent signal log\n"
            "/journal trades — recent trade log\n"
            "/journal attrs — Greeks P/L attribution\n"
            "\n── Income Scanner ──\n"
            "/income — scan all income tickers for credit spread opportunities\n"
            "/income MRNA — scan single ticker\n"
            "/score MRNA put 47 2 0.45 — score a specific bull put spread\n"
            "/score MRNA put 47 2 0.45 2026-04-11 — score with explicit expiry\n"
            "/score NVDA call 195 2.50 0.35 — score a bear call spread\n"
            "/help — this message\n\n"
            "💡 -mom on any portfolio command for mom's account\n"
            "⚡ TV signals auto-warn if you have opposite spreads open\n"
            "🛡️ Risk limits auto-block trades that exceed exposure caps\n"
            "— Not financial advice —"
        )
        return

    # ─────────────────────────────────────
    # SUBSCRIPTION COMMANDS (v8.5 Phase 3)
    # ─────────────────────────────────────

    if cmd in ("/daytrade", "/daytrade@omegabot"):
        try:
            from subscriptions import get_subscription_manager
            sm = get_subscription_manager()
        except Exception:
            sm = None
        if not sm:
            reply("Subscription manager not ready. Try again in a moment.")
            return
        if len(args) < 2:
            reply("Usage: /daytrade <TICKER> <call|put|close>")
            return
        ticker = args[0].upper()
        action = args[1].lower()
        if action == "close":
            n = sm.remove(chat_id, ticker)
            reply(f"Closed {n} daytrade subscription(s) for {ticker}" if n
                  else f"No active daytrade sub for {ticker}")
            return
        if action not in ("call", "put"):
            reply("Direction must be 'call', 'put', or 'close'")
            return
        sm.add(chat_id, ticker, action, mode="daytrade", source="manual")
        reply(f"📡 Daytrade active: {ticker} {action.upper()}\n"
              f"You'll receive thesis alerts, trade cards, flow conviction, "
              f"and exits for {ticker}. Stop with /daytrade {ticker} close")
        return

    if cmd in ("/conviction", "/conviction@omegabot"):
        try:
            from subscriptions import get_subscription_manager
            sm = get_subscription_manager()
        except Exception:
            sm = None
        if not sm:
            reply("Subscription manager not ready. Try again in a moment.")
            return
        if len(args) < 2:
            reply("Usage: /conviction <TICKER> <call|put|close>")
            return
        ticker = args[0].upper()
        action = args[1].lower()
        if action == "close":
            n = sm.remove(chat_id, ticker)
            reply(f"Closed {n} conviction subscription(s) for {ticker}" if n
                  else f"No active conviction sub for {ticker}")
            return
        if action not in ("call", "put"):
            reply("Direction must be 'call', 'put', or 'close'")
            return
        sm.add(chat_id, ticker, action, mode="conviction", source="manual")
        reply(f"💎 Conviction tracking: {ticker} {action.upper()}\n"
              f"You'll receive conviction re-fires and exit signals. "
              f"Technical thesis alerts stay silent. Auto-closes on exit signal. "
              f"Stop with /conviction {ticker} close")
        return

    if cmd in ("/positions", "/positions@omegabot"):
        try:
            from subscriptions import get_subscription_manager
            sm = get_subscription_manager()
        except Exception:
            sm = None
        if not sm:
            reply("Subscription manager not ready.")
            return
        subs = sm.list_all(chat_id)
        if not subs:
            reply("No active subscriptions.\n"
                  "Use /daytrade <TICKER> <call|put> or /conviction <TICKER> <call|put>")
            return
        lines = ["📋 Active subscriptions:"]
        for s in subs:
            tag = "📡" if s["mode"] == "daytrade" else "💎"
            line = f"  {tag} {s['ticker']} {s['direction'].upper()} ({s['mode']})"
            if s.get("source_expiry"):
                line += f" — exp {s['source_expiry']} ({s.get('source_dte','?')}DTE)"
            lines.append(line)
        reply("\n".join(lines))
        return

    if cmd in ("/exportlogs", "/exportlogs@omegabot", "/logs", "/logs@omegabot"):
        try:
            import os as _os
            _diag_chat = _os.getenv("DIAGNOSTIC_CHAT_ID", "").strip() or chat_id
            log_path = "/tmp/omega3000.log"
            if not _os.path.exists(log_path):
                reply("No log file found yet. Logs accumulate after deploy.")
                return
            size_mb = _os.path.getsize(log_path) / 1_000_000
            # Optional: filter by keyword
            if args:
                keyword = " ".join(args).lower()
                filtered_path = "/tmp/omega3000_filtered.log"
                count = 0
                with open(log_path, "r", encoding="utf-8", errors="replace") as src, \
                     open(filtered_path, "w", encoding="utf-8") as dst:
                    for line in src:
                        if keyword in line.lower():
                            dst.write(line)
                            count += 1
                send_document(_diag_chat, filtered_path,
                              caption=f"Filtered logs: '{keyword}' ({count} lines)")
                reply(f"📤 Sent filtered logs to diagnostics channel ({count} lines)")
            else:
                send_document(_diag_chat, log_path,
                              caption=f"Full log ({size_mb:.1f} MB)")
                reply(f"📤 Sent full log to diagnostics channel ({size_mb:.1f} MB)")
        except Exception as e:
            reply(f"Log export failed: {e}")
        return

    reply(f"❓ Unknown command: {cmd}\nType /help for available commands.")


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
    try:
        handler_fn(args, reply_fn, get_regime_fn=_get_regime_ref, account=account)
    except Exception as e:
        log.error(f"Command error: {type(e).__name__}: {e}")
        send_reply(chat_id, f"⚠️ Error: {type(e).__name__}: {str(e)[:120]}")
