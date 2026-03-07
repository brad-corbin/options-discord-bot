# holdings_commands.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Phase 2A — Telegram Command Handlers for Portfolio
#   - /hold add, /hold remove, /hold list
#   - /sell put, /sell call
#   - /close, /expire, /assign
#   - /options, /options history
#   - /wheel TICKER
#
# Each handler receives (args, send_fn, get_spot_fn)
#   send_fn(text)          → posts to Telegram chat
#   get_spot_fn(ticker)    → returns current price float

import logging
from portfolio import (
    get_all_holdings,
    get_holding,
    add_holding,
    remove_holding,
    get_open_options,
    get_all_options,
    get_option_by_id,
    add_option,
    close_option,
    expire_option,
    assign_option,
    calc_holding_pnl,
    calc_option_pnl,
    calc_ticker_options_income,
    calc_portfolio_summary,
    calc_wheel_pnl,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# FORMATTERS
# ─────────────────────────────────────────────────────────

def _fmt_money(v: float) -> str:
    """Format dollar amount with sign: +$1,234 or -$567"""
    prefix = "+" if v >= 0 else ""
    return f"{prefix}${v:,.0f}"

def _fmt_pct(v: float) -> str:
    prefix = "+" if v >= 0 else ""
    return f"{prefix}{v:.1f}%"

def _pnl_emoji(v: float) -> str:
    return "🟢" if v > 0 else "🔴" if v < 0 else "⚪"

def _opt_type_label(opt: dict) -> str:
    t = opt.get("type", "")
    if t == "covered_call":
        return "CC"
    elif t == "csp":
        return "CSP"
    elif t == "debit_spread":
        return "DS"
    return t.upper()

def _opt_status_emoji(status: str) -> str:
    return {
        "open":     "🔵",
        "closed":   "✅",
        "expired":  "💀",
        "assigned": "📌",
    }.get(status, "❓")


# ═══════════════════════════════════════════════════════════
# /hold COMMANDS
# ═══════════════════════════════════════════════════════════

def handle_hold(args: list, send_fn, get_spot_fn):
    """
    Router for /hold subcommands:
      /hold add AAPL 100 @185.50
      /hold add AAPL 100 @185.50 #wheel
      /hold remove AAPL
      /hold remove AAPL 50
      /hold list
    """
    if not args:
        send_fn("Usage: /hold add|remove|list")
        return

    sub = args[0].lower()

    if sub == "add":
        _hold_add(args[1:], send_fn)
    elif sub == "remove":
        _hold_remove(args[1:], send_fn)
    elif sub == "list":
        _hold_list(send_fn, get_spot_fn)
    else:
        send_fn(f"Unknown: /hold {sub}\nUse: /hold add|remove|list")


def _hold_add(args: list, send_fn):
    """
    /hold add AAPL 100 @185.50
    /hold add AAPL 100 @185.50 #wheel
    /hold add AAPL 100 @185.50 #long-term Core position
    """
    if len(args) < 3:
        send_fn("Usage: /hold add TICKER SHARES @PRICE [#tag] [notes]")
        return

    ticker = args[0].upper()
    tag = None
    notes = ""

    try:
        shares = int(args[1])
    except ValueError:
        send_fn(f"Bad share count: {args[1]}")
        return

    price_str = args[2]
    if price_str.startswith("@"):
        price_str = price_str[1:]
    try:
        cost_basis = float(price_str)
    except ValueError:
        send_fn(f"Bad price: {args[2]} — use @185.50")
        return

    # Parse optional tag and notes from remaining args
    for r in args[3:]:
        if r.startswith("#"):
            tag = r[1:]
        else:
            notes = (notes + " " + r).strip() if notes else r

    holding = add_holding(ticker, shares, cost_basis, tag=tag, notes=notes)
    invested = holding["shares"] * holding["cost_basis"]

    msg = (
        f"✅ {ticker} — {holding['shares']}sh @${holding['cost_basis']:.2f}\n"
        f"Total invested: ${invested:,.0f}"
    )
    if holding.get("tags"):
        msg += f"\nTags: {' '.join('#' + t for t in holding['tags'])}"
    send_fn(msg)


def _hold_remove(args: list, send_fn):
    """
    /hold remove AAPL       → remove all shares
    /hold remove AAPL 50    → partial sale
    """
    if not args:
        send_fn("Usage: /hold remove TICKER [SHARES]")
        return

    ticker = args[0].upper()
    shares = None
    if len(args) >= 2:
        try:
            shares = int(args[1])
        except ValueError:
            send_fn(f"Bad share count: {args[1]}")
            return

    result = remove_holding(ticker, shares)

    if not result.get("removed"):
        send_fn(f"❌ {result.get('error', 'Unknown error')}")
        return

    if result["remaining"] > 0:
        send_fn(f"✅ Removed {shares}sh {ticker} — {result['remaining']}sh remaining")
    else:
        send_fn(f"✅ Removed ALL {ticker} from holdings")


def _hold_list(send_fn, get_spot_fn):
    """
    /hold list → show all holdings with current price & P/L
    """
    holdings = get_all_holdings()
    if not holdings:
        send_fn("📊 No holdings yet. Use /hold add TICKER SHARES @PRICE")
        return

    # Fetch current prices
    price_map = {}
    for ticker in holdings:
        try:
            price_map[ticker] = get_spot_fn(ticker)
        except Exception as e:
            log.warning(f"Price fetch failed for {ticker}: {e}")

    summary = calc_portfolio_summary(price_map)
    details = summary["holdings"]

    lines = [f"📊 HOLDINGS ({summary['num_holdings']} positions)\n"]

    for d in details:
        tags_str = ""
        h = holdings.get(d["ticker"], {})
        if h.get("tags"):
            tags_str = "  " + " ".join("#" + t for t in h["tags"])

        lines.append(
            f"{d['ticker']}  {d['shares']}sh  "
            f"${d['cost_basis']:.2f} → ${d['current']:.2f}  "
            f"{_fmt_money(d['total_pnl'])} ({_fmt_pct(d['return_pct'])})"
            f"{tags_str}"
        )

    # Tickers we couldn't price
    missing = [t for t in holdings if t not in price_map]
    if missing:
        lines.append(f"\n⚠️ No price data: {', '.join(missing)}")

    lines.append(f"\nTotal Unrealized: {_fmt_money(summary['total_unrealized'])}")
    lines.append(f"Options Income (closed): {_fmt_money(summary['total_opt_income'])}")
    lines.append(f"Combined P/L: {_fmt_money(summary['combined_pnl'])}")

    if summary["num_open_options"] > 0:
        lines.append(f"Open options: {summary['num_open_options']} — use /options to view")

    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /sell COMMANDS
# ═══════════════════════════════════════════════════════════

def handle_sell(args: list, send_fn):
    """
    /sell put AAPL 180 2026-03-21 2.35
    /sell put AAPL 180 2026-03-21 2.35 x3
    /sell call AAPL 195 2026-03-21 1.80
    """
    if len(args) < 5:
        send_fn(
            "Usage: /sell put|call TICKER STRIKE EXP PREMIUM [xN]\n"
            "Example: /sell put AAPL 180 2026-03-21 2.35\n"
            "Example: /sell call AAPL 195 2026-03-21 1.80 x2"
        )
        return

    opt_side = args[0].lower()
    if opt_side not in ("put", "call"):
        send_fn(f"Unknown option type: {opt_side} — use put or call")
        return

    ticker = args[1].upper()

    try:
        strike = float(args[2])
    except ValueError:
        send_fn(f"Bad strike: {args[2]}")
        return

    exp = args[3]  # expects YYYY-MM-DD

    try:
        premium = float(args[4])
    except ValueError:
        send_fn(f"Bad premium: {args[4]}")
        return

    contracts = 1
    if len(args) >= 6:
        c_str = args[5].lower()
        if c_str.startswith("x"):
            c_str = c_str[1:]
        try:
            contracts = int(c_str)
        except ValueError:
            send_fn(f"Bad contract count: {args[5]} — use x3")
            return

    opt_type = "csp" if opt_side == "put" else "covered_call"
    opt = add_option(
        ticker=ticker,
        opt_type=opt_type,
        direction="sell",
        strike=strike,
        exp=exp,
        premium=premium,
        contracts=contracts,
    )

    total_credit = premium * contracts * 100
    label = "CSP" if opt_side == "put" else "CC"

    send_fn(
        f"✅ Opened {opt['id']}\n"
        f"SELL {label} {ticker} ${strike} exp {exp}\n"
        f"Premium: ${premium} × {contracts} = ${total_credit:,.0f} credit\n"
        f"Use /close {opt['id']} PRICE to close"
    )


# ═══════════════════════════════════════════════════════════
# /close, /expire, /assign COMMANDS
# ═══════════════════════════════════════════════════════════

def handle_close(args: list, send_fn):
    """
    /close opt_001 0.15  → bought back at $0.15
    """
    if len(args) < 2:
        send_fn("Usage: /close OPT_ID CLOSE_PRICE\nExample: /close opt_001 0.15")
        return

    opt_id = args[0]
    try:
        close_premium = float(args[1])
    except ValueError:
        send_fn(f"Bad price: {args[1]}")
        return

    result = close_option(opt_id, close_premium)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = calc_option_pnl(result)
    label = _opt_type_label(result)

    send_fn(
        f"✅ Closed {opt_id}\n"
        f"{label} {result['ticker']} ${result['strike']} → closed @${close_premium}\n"
        f"P/L: {_fmt_money(pnl)} {_pnl_emoji(pnl)}"
    )


def handle_expire(args: list, send_fn):
    """
    /expire opt_001 → expired worthless, full premium kept
    """
    if not args:
        send_fn("Usage: /expire OPT_ID\nExample: /expire opt_001")
        return

    opt_id = args[0]
    result = expire_option(opt_id)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = calc_option_pnl(result)
    label = _opt_type_label(result)

    send_fn(
        f"💀 Expired {opt_id}\n"
        f"{label} {result['ticker']} ${result['strike']} — expired worthless\n"
        f"Premium kept: {_fmt_money(pnl)} {_pnl_emoji(pnl)}"
    )


def handle_assign(args: list, send_fn):
    """
    /assign opt_001 → CSP assigned (shares added) or CC assigned (shares removed)
    """
    if not args:
        send_fn("Usage: /assign OPT_ID\nExample: /assign opt_001")
        return

    opt_id = args[0]
    result = assign_option(opt_id)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    opt = result["option"]
    action = result.get("action", "—")
    label = _opt_type_label(opt)

    send_fn(
        f"📌 Assigned {opt_id}\n"
        f"{label} {opt['ticker']} ${opt['strike']}\n"
        f"→ {action}"
    )


# ═══════════════════════════════════════════════════════════
# /options COMMAND
# ═══════════════════════════════════════════════════════════

def handle_options(args: list, send_fn):
    """
    /options          → show all open positions
    /options history  → show closed/expired positions with realized P/L
    """
    show_history = args and args[0].lower() == "history"

    if show_history:
        _options_history(send_fn)
    else:
        _options_open(send_fn)


def _options_open(send_fn):
    """Show all open options positions."""
    positions = get_open_options()

    if not positions:
        send_fn("📋 No open options. Use /sell put|call to open one.")
        return

    lines = [f"📋 OPEN OPTIONS ({len(positions)} positions)\n"]

    for o in positions:
        label = _opt_type_label(o)
        exp_short = o["exp"][5:] if len(o["exp"]) >= 10 else o["exp"]  # MM-DD

        lines.append(
            f"{o['id']}  {o['ticker']}  {label}  "
            f"${o['strike']}  {exp_short}  "
            f"sold@${o['premium']}"
            f"{'  x' + str(o['contracts']) if o['contracts'] > 1 else ''}"
        )

    lines.append("\nUse /close ID PRICE or /expire ID or /assign ID")
    send_fn("\n".join(lines))


def _options_history(send_fn):
    """Show closed/expired/assigned options with realized P/L."""
    all_opts = get_all_options()
    closed = [o for o in all_opts if o.get("status") in ("closed", "expired", "assigned")]

    if not closed:
        send_fn("📋 No options history yet.")
        return

    # Show most recent first
    closed.sort(key=lambda o: o.get("close_date", ""), reverse=True)

    lines = [f"📋 OPTIONS HISTORY ({len(closed)} closed)\n"]
    total_pnl = 0.0

    for o in closed[:20]:  # cap at 20 to avoid huge messages
        pnl = calc_option_pnl(o)
        total_pnl += pnl
        label = _opt_type_label(o)
        status = _opt_status_emoji(o["status"])

        lines.append(
            f"{status} {o['id']}  {o['ticker']}  {label}  "
            f"${o['strike']}  "
            f"${o['premium']}→${o.get('close_premium', 0):.2f}  "
            f"{_fmt_money(pnl)} {_pnl_emoji(pnl)}"
        )

    lines.append(f"\nTotal Realized: {_fmt_money(total_pnl)} {_pnl_emoji(total_pnl)}")

    if len(closed) > 20:
        lines.append(f"(showing 20 of {len(closed)})")

    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /wheel COMMAND
# ═══════════════════════════════════════════════════════════

def handle_wheel(args: list, send_fn):
    """
    /wheel AAPL → show complete wheel history + P/L for AAPL
    /wheel      → show summary of all wheel tickers
    """
    if args:
        _wheel_ticker(args[0].upper(), send_fn)
    else:
        _wheel_summary(send_fn)


def _wheel_ticker(ticker: str, send_fn):
    """Full wheel history for one ticker."""
    result = calc_wheel_pnl(ticker)
    history = result["history"]

    if not history:
        send_fn(f"🔄 No wheel history for {ticker}")
        return

    lines = [
        f"🔄 WHEEL — {ticker}",
        f"Total Premium: {_fmt_money(result['total_premium'])} "
        f"({result['closed_rounds']} rounds closed)\n",
    ]

    for o in history:
        label = _opt_type_label(o)
        status = _opt_status_emoji(o["status"])
        pnl_str = ""

        if o["status"] != "open":
            pnl = calc_option_pnl(o)
            pnl_str = f"  {_fmt_money(pnl)}"

        lines.append(
            f"{status} {o['id']}  {label}  ${o['strike']}  "
            f"exp {o['exp']}  @${o['premium']}"
            f"{'  x' + str(o['contracts']) if o['contracts'] > 1 else ''}"
            f"{pnl_str}"
        )

    if result["open_positions"] > 0:
        lines.append(f"\n{result['open_positions']} position(s) still open")

    # Check if shares are currently held (wheel in CC phase)
    holding = get_holding(ticker)
    if holding:
        lines.append(
            f"\n📦 Currently holding {holding['shares']}sh @${holding['cost_basis']:.2f}"
        )

    send_fn("\n".join(lines))


def _wheel_summary(send_fn):
    """Summary of all tickers with wheel activity."""
    all_opts = get_all_options()
    wheel_tickers = set()

    for o in all_opts:
        if o.get("type") in ("csp", "covered_call"):
            wheel_tickers.add(o["ticker"])

    if not wheel_tickers:
        send_fn("🔄 No wheel positions yet. Use /sell put to start.")
        return

    lines = ["🔄 WHEEL SUMMARY\n"]
    grand_total = 0.0

    for ticker in sorted(wheel_tickers):
        result = calc_wheel_pnl(ticker)
        grand_total += result["total_premium"]
        open_str = f"  ({result['open_positions']} open)" if result["open_positions"] else ""

        lines.append(
            f"{ticker}  {result['closed_rounds']} rounds  "
            f"{_fmt_money(result['total_premium'])}{open_str}"
        )

    lines.append(f"\nGrand Total Premium: {_fmt_money(grand_total)}")
    lines.append("\nUse /wheel TICKER for full history")
    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /holdings COMMAND (Phase 2B — Sentiment Report)
# ═══════════════════════════════════════════════════════════

def handle_holdings(args: list, send_fn, md_get_fn):
    """
    /holdings → run sentiment scan on all holdings (EMA/VWAP/Vol + P/L)
    Delegates to sentiment_report.generate_sentiment_report()
    """
    from sentiment_report import generate_sentiment_report

    send_fn("🔍 Running sentiment scan...")

    try:
        report = generate_sentiment_report(md_get_fn)
        send_fn(report)
    except Exception as e:
        log.error(f"/holdings error: {type(e).__name__}: {e}")
        send_fn(f"⚠️ Sentiment scan failed: {type(e).__name__}: {str(e)[:120]}")
