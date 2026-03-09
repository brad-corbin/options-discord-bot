# holdings_commands.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Phase 2A — Telegram Command Handlers for Portfolio
#   /hold, /sell, /close, /expire, /assign, /options, /wheel
#
# v3.2 — Multi-account support
# v3.4 — /spread command for debit spread tracking
#
# Each handler receives (args, send_fn, [extra], account="brad")

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
    roll_option,
    calc_holding_pnl,
    calc_option_pnl,
    calc_ticker_options_income,
    calc_portfolio_summary,
    calc_wheel_pnl,
    get_cash_data,
    set_total_deposited,
    update_cash_balance,
    calc_account_pnl,
    get_mutual_fund,
    set_mutual_fund_basis,
    update_mutual_fund_value,
    calc_mutual_fund_pnl,
    # v3.4 — Spread tracking
    get_all_spreads,
    get_open_spreads,
    get_spread_by_id,
    add_spread,
    close_spread,
    stop_spread,
    expire_spread as expire_spread_fn,
    calc_spread_pnl,
    calc_spread_summary,
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
        "rolled":   "🔁",
        "stopped":  "🛑",
    }.get(status, "❓")


def _acct_label(account: str) -> str:
    """Short label for message headers so you know which account you're looking at."""
    if account == "mom":
        return "👩 Mom"
    return "📁 Brad"


# ═══════════════════════════════════════════════════════════
# /hold COMMANDS
# ═══════════════════════════════════════════════════════════

def handle_hold(args: list, send_fn, get_spot_fn, account: str = "brad"):
    if not args:
        send_fn("Usage: /hold add|remove|list [--mom]")
        return

    sub = args[0].lower()

    if sub == "add":
        _hold_add(args[1:], send_fn, account)
    elif sub == "remove":
        _hold_remove(args[1:], send_fn, account)
    elif sub == "list":
        _hold_list(send_fn, get_spot_fn, account)
    else:
        send_fn(f"Unknown: /hold {sub}\nUse: /hold add|remove|list")


def _hold_add(args: list, send_fn, account: str = "brad"):
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

    for r in args[3:]:
        if r.startswith("#"):
            tag = r[1:]
        else:
            notes = (notes + " " + r).strip() if notes else r

    holding = add_holding(ticker, shares, cost_basis, tag=tag, notes=notes, account=account)
    invested = holding["shares"] * holding["cost_basis"]

    msg = (
        f"✅ {_acct_label(account)} — {ticker} — {holding['shares']}sh @${holding['cost_basis']:.2f}\n"
        f"Total invested: ${invested:,.0f}"
    )
    if holding.get("tags"):
        msg += f"\nTags: {' '.join('#' + t for t in holding['tags'])}"
    send_fn(msg)


def _hold_remove(args: list, send_fn, account: str = "brad"):
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

    result = remove_holding(ticker, shares, account=account)

    if not result.get("removed"):
        send_fn(f"❌ {result.get('error', 'Unknown error')}")
        return

    if result["remaining"] > 0:
        send_fn(f"✅ {_acct_label(account)} — Removed {shares}sh {ticker} — {result['remaining']}sh remaining")
    else:
        send_fn(f"✅ {_acct_label(account)} — Removed ALL {ticker} from holdings")


def _hold_list(send_fn, get_spot_fn, account: str = "brad"):
    holdings = get_all_holdings(account=account)
    if not holdings:
        send_fn(f"📊 {_acct_label(account)} — No holdings yet. Use /hold add TICKER SHARES @PRICE")
        return

    price_map = {}
    for ticker in holdings:
        try:
            price_map[ticker] = get_spot_fn(ticker)
        except Exception as e:
            log.warning(f"Price fetch failed for {ticker}: {e}")

    summary = calc_portfolio_summary(price_map, account=account)
    details = summary["holdings"]

    lines = [f"📊 {_acct_label(account)} — HOLDINGS ({summary['num_holdings']} positions)\n"]

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

def handle_sell(args: list, send_fn, account: str = "brad"):
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

    exp = args[3]

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
        ticker=ticker, opt_type=opt_type, direction="sell",
        strike=strike, exp=exp, premium=premium,
        contracts=contracts, account=account,
    )

    total_credit = premium * contracts * 100
    label = "CSP" if opt_side == "put" else "CC"

    lines = [
        f"✅ {_acct_label(account)} — Opened {opt['id']}",
        f"SELL {label} {ticker} ${strike} exp {exp}",
        f"Premium: ${premium} × {contracts} = ${total_credit:,.0f} credit",
    ]

    if opt_side == "put":
        cash_secured = strike * contracts * 100
        breakeven = round(strike - premium, 2)
        lines.append(f"Cash secured: ${cash_secured:,.0f}")
        lines.append(f"Break-even: ${breakeven}")
    else:
        holding = get_holding(ticker, account=account)
        if holding:
            lines.append(f"Covered by: {holding['shares']}sh")

    wheel = calc_wheel_pnl(ticker, account=account)
    lines.append(f"Wheel: {wheel['stage_emoji']} {wheel['stage']}")
    if wheel["realized_premium"] != 0:
        lines.append(f"Total premium on {ticker}: {_fmt_money(wheel['total_premium'])}")
    if wheel["adjusted_basis"] is not None:
        lines.append(f"Adjusted basis: ${wheel['adjusted_basis']}")

    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /close, /expire, /assign COMMANDS
# ═══════════════════════════════════════════════════════════

def handle_close(args: list, send_fn, account: str = "brad"):
    if len(args) < 2:
        send_fn("Usage: /close OPT_ID CLOSE_PRICE\nExample: /close opt_001 0.15")
        return

    opt_id = args[0]
    try:
        close_premium = float(args[1])
    except ValueError:
        send_fn(f"Bad price: {args[1]}")
        return

    result = close_option(opt_id, close_premium, account=account)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = calc_option_pnl(result)
    label = _opt_type_label(result)

    send_fn(
        f"✅ {_acct_label(account)} — Closed {opt_id}\n"
        f"{label} {result['ticker']} ${result['strike']} → closed @${close_premium}\n"
        f"P/L: {_fmt_money(pnl)} {_pnl_emoji(pnl)}"
    )


def handle_expire(args: list, send_fn, account: str = "brad"):
    if not args:
        send_fn("Usage: /expire OPT_ID\nExample: /expire opt_001")
        return

    opt_id = args[0]
    result = expire_option(opt_id, account=account)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = calc_option_pnl(result)
    label = _opt_type_label(result)

    send_fn(
        f"💀 {_acct_label(account)} — Expired {opt_id}\n"
        f"{label} {result['ticker']} ${result['strike']} — expired worthless\n"
        f"Premium kept: {_fmt_money(pnl)} {_pnl_emoji(pnl)}"
    )


def handle_assign(args: list, send_fn, account: str = "brad"):
    if not args:
        send_fn("Usage: /assign OPT_ID\nExample: /assign opt_001")
        return

    opt_id = args[0]
    result = assign_option(opt_id, account=account)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    opt = result["option"]
    action = result.get("action", "—")
    label = _opt_type_label(opt)
    ticker = opt["ticker"]

    lines = [
        f"📌 {_acct_label(account)} — Assigned {opt_id}",
        f"{label} {opt['ticker']} ${opt['strike']}",
        f"→ {action}",
    ]

    wheel = calc_wheel_pnl(ticker, account=account)

    if opt["type"] == "csp" and wheel["has_shares"]:
        lines.append("")
        lines.append(f"Premium collected on {ticker}: {_fmt_money(wheel['realized_premium'])}")
        if wheel["adjusted_basis"] is not None:
            lines.append(f"Adjusted cost basis: ${wheel['adjusted_basis']}")
        lines.append(f"Wheel: {wheel['stage_emoji']} {wheel['stage']}")
        lines.append(f"\n💡 Next: sell a covered call above ${wheel['adjusted_basis'] or opt['strike']}")
    elif opt["type"] == "covered_call":
        lines.append(f"\nPremium collected on {ticker}: {_fmt_money(wheel['realized_premium'])}")
        lines.append(f"Wheel: {wheel['stage_emoji']} {wheel['stage']}")

    send_fn("\n".join(lines))


def handle_roll(args: list, send_fn, account: str = "brad"):
    if len(args) < 4:
        send_fn(
            "Usage:\n"
            "  /roll OPT_ID NEW_EXP NEW_STRIKE NEW_PREMIUM [CLOSE_PRICE]\n"
            "  /roll opt_016 2026-03-20 13 4.32\n"
            "  /roll opt_016 2026-03-20 13 4.32 3.77\n\n"
            "Closes the old position and opens a new one,\n"
            "tracking the roll chain and net credit."
        )
        return

    idx = 0
    direction_hint = None
    if args[0].lower() in ("out", "up", "down"):
        direction_hint = args[0].lower()
        idx = 1

    opt_id = args[idx]
    idx += 1

    remaining = args[idx:]
    if len(remaining) < 3:
        send_fn("Need at least: NEW_EXP NEW_STRIKE NEW_PREMIUM")
        return

    new_exp = remaining[0]

    try:
        new_strike = float(remaining[1])
    except ValueError:
        send_fn(f"Bad strike: {remaining[1]}")
        return

    try:
        new_premium = float(remaining[2])
    except ValueError:
        send_fn(f"Bad premium: {remaining[2]}")
        return

    close_premium = None
    if len(remaining) >= 4:
        try:
            close_premium = float(remaining[3])
        except ValueError:
            send_fn(f"Bad close price: {remaining[3]}")
            return

    result = roll_option(
        opt_id, new_exp, new_strike, new_premium,
        close_premium=close_premium, account=account,
    )

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    old = result["old_opt"]
    new = result["new_opt"]
    ticker = old["ticker"]
    label = _opt_type_label(old)

    lines = [
        f"🔁 {_acct_label(account)} — Rolled {opt_id} → {new['id']}",
        f"{label} {ticker}",
        f"Old: ${old['strike']} exp {old['exp']} @${old['premium']}",
        f"New: ${new['strike']} exp {new['exp']} @${new['premium']}",
        "",
        f"Net roll credit: {_fmt_money(result['net_credit'])}",
        f"Total premium on {ticker}: {_fmt_money(result['total_ticker_premium'])}",
    ]

    wheel = calc_wheel_pnl(ticker, account=account)
    if wheel["adjusted_basis"] is not None:
        lines.append(f"Adjusted basis: ${wheel['adjusted_basis']}")
    lines.append(f"Wheel: {wheel['stage_emoji']} {wheel['stage']}")

    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /options COMMAND
# ═══════════════════════════════════════════════════════════

def handle_options(args: list, send_fn, account: str = "brad"):
    show_history = args and args[0].lower() == "history"

    if show_history:
        _options_history(send_fn, account)
    else:
        _options_open(send_fn, account)


def _options_open(send_fn, account: str = "brad"):
    positions = get_open_options(account=account)

    if not positions:
        send_fn(f"📋 {_acct_label(account)} — No open options. Use /sell put|call to open one.")
        return

    lines = [f"📋 {_acct_label(account)} — OPEN OPTIONS ({len(positions)} positions)\n"]

    for o in positions:
        label = _opt_type_label(o)
        exp_short = o["exp"][5:] if len(o["exp"]) >= 10 else o["exp"]

        lines.append(
            f"{o['id']}  {o['ticker']}  {label}  "
            f"${o['strike']}  {exp_short}  "
            f"sold@${o['premium']}"
            f"{'  x' + str(o['contracts']) if o['contracts'] > 1 else ''}"
        )

    lines.append("\nUse /close ID PRICE or /expire ID or /assign ID")
    send_fn("\n".join(lines))


def _options_history(send_fn, account: str = "brad"):
    all_opts = get_all_options(account=account)
    closed = [o for o in all_opts if o.get("status") in ("closed", "expired", "assigned", "rolled")]

    if not closed:
        send_fn(f"📋 {_acct_label(account)} — No options history yet.")
        return

    closed.sort(key=lambda o: o.get("close_date", ""), reverse=True)

    lines = [f"📋 {_acct_label(account)} — OPTIONS HISTORY ({len(closed)} closed)\n"]
    total_pnl = 0.0

    for o in closed[:20]:
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

def handle_wheel(args: list, send_fn, account: str = "brad"):
    if args:
        _wheel_ticker(args[0].upper(), send_fn, account)
    else:
        _wheel_summary(send_fn, account)


def _wheel_ticker(ticker: str, send_fn, account: str = "brad"):
    result = calc_wheel_pnl(ticker, account=account)
    history = result["history"]

    if not history:
        send_fn(f"🔄 {_acct_label(account)} — No wheel history for {ticker}")
        return

    lines = [
        f"🔄 {_acct_label(account)} — WHEEL — {ticker}",
        f"Status: {result['stage_emoji']} {result['stage']}",
    ]

    if result["has_shares"]:
        lines.append(f"Shares: {result['shares']} @ ${result['entry_price']:.2f}")
        if result["adjusted_basis"] is not None:
            lines.append(f"Adjusted Basis: ${result['adjusted_basis']}")

    lines.append("")

    lines.append("Premium History:")
    for o in history:
        label = _opt_type_label(o)
        status = _opt_status_emoji(o["status"])
        prem = o.get("premium", 0) * o.get("contracts", 1) * 100
        prem_str = f"${prem:,.0f}"

        extra = ""
        if o["status"] == "rolled":
            extra = f" → {o.get('rolled_to', '?')}"
        elif o["status"] in ("closed",):
            close_p = o.get("close_premium", 0)
            extra = f" closed@${close_p}"

        contracts_str = f" x{o['contracts']}" if o.get("contracts", 1) > 1 else ""
        exp_short = o["exp"][5:] if len(o["exp"]) >= 10 else o["exp"]

        pnl_str = ""
        if o["status"] != "open":
            pnl = calc_option_pnl(o)
            pnl_str = f"  {_fmt_money(pnl)}"

        lines.append(
            f"  {status} {o['id']} {label} ${o['strike']} {exp_short} "
            f"@${o['premium']}{contracts_str}{pnl_str}{extra}"
        )

    lines.append("")
    lines.append(f"Realized Premium: {_fmt_money(result['realized_premium'])}")
    if result["open_premium"] > 0:
        lines.append(f"Open Premium: {_fmt_money(result['open_premium'])}")
    lines.append(f"Total Premium: {_fmt_money(result['total_premium'])}")

    if result["has_shares"] and result["adjusted_basis"] is not None:
        lines.append("")
        lines.append("Adjusted Cost Basis:")
        prem_per_share = round(result["realized_premium"] / result["shares"], 2) if result["shares"] > 0 else 0
        lines.append(f"  ${result['entry_price']:.2f} - ${prem_per_share:.2f} = ${result['adjusted_basis']}")

    if result["open_opts"]:
        lines.append("")
        lines.append("Open Position(s):")
        for o in result["open_opts"]:
            label = _opt_type_label(o)
            contracts_str = f" x{o['contracts']}" if o.get("contracts", 1) > 1 else ""
            lines.append(f"  {label} ${o['strike']} exp {o['exp']} @${o['premium']}{contracts_str}")

    send_fn("\n".join(lines))


def _wheel_summary(send_fn, account: str = "brad"):
    all_opts = get_all_options(account=account)
    wheel_tickers = set()

    for o in all_opts:
        if o.get("type") in ("csp", "covered_call"):
            wheel_tickers.add(o["ticker"])

    if not wheel_tickers:
        send_fn(f"🔄 {_acct_label(account)} — No wheel positions yet. Use /sell put to start.")
        return

    lines = [f"🔄 {_acct_label(account)} — WHEEL SUMMARY\n"]
    grand_total = 0.0

    for ticker in sorted(wheel_tickers):
        result = calc_wheel_pnl(ticker, account=account)
        grand_total += result["total_premium"]

        basis_str = f"  basis ${result['adjusted_basis']}" if result["adjusted_basis"] is not None else ""
        stage_str = f"{result['stage_emoji']}{result['stage'][:3]}"

        lines.append(
            f"{ticker}  {stage_str}  "
            f"prem {_fmt_money(result['total_premium'])}{basis_str}"
        )

    lines.append(f"\nTotal Premium: {_fmt_money(grand_total)}")
    lines.append("\nUse /wheel TICKER for full history")
    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /holdings COMMAND (Phase 2B — Sentiment Report)
# ═══════════════════════════════════════════════════════════

def handle_holdings(args: list, send_fn, md_get_fn, account: str = "brad"):
    from sentiment_report import generate_sentiment_report

    send_fn(f"🔍 {_acct_label(account)} — Running sentiment scan...")

    try:
        report = generate_sentiment_report(md_get_fn, account=account)
        send_fn(report)
    except Exception as e:
        log.error(f"/holdings error: {type(e).__name__}: {e}")
        send_fn(f"⚠️ Sentiment scan failed: {type(e).__name__}: {str(e)[:120]}")


# ═══════════════════════════════════════════════════════════
# /portfolio COMMAND (Phase 2C — Full Dashboard)
# ═══════════════════════════════════════════════════════════

def handle_portfolio(args: list, send_fn, md_get_fn, account: str = "brad"):
    from portfolio_dashboard import generate_dashboard

    send_fn(f"📊 {_acct_label(account)} — Building dashboard...")

    try:
        messages = generate_dashboard(md_get_fn, account=account)
        for msg in messages:
            send_fn(msg)
    except Exception as e:
        log.error(f"/portfolio error: {type(e).__name__}: {e}")
        send_fn(f"⚠️ Dashboard failed: {type(e).__name__}: {str(e)[:120]}")


# ═══════════════════════════════════════════════════════════
# /cash COMMAND (v3.3)
# ═══════════════════════════════════════════════════════════

def handle_cash(args: list, send_fn, get_spot_fn, account: str = "brad"):
    if not args:
        _cash_show(send_fn, get_spot_fn, account)
        return

    sub = args[0].lower()

    if sub == "deposit":
        if len(args) < 2:
            send_fn(
                "Usage:\n"
                "  /cash deposit 50000 — set total amount deposited\n"
                "  /cash deposit +5000 — add a new deposit"
            )
            return
        raw = args[1].replace(",", "").replace("$", "")
        is_add = raw.startswith("+")
        try:
            amount = float(raw)
        except ValueError:
            send_fn(f"Bad amount: {args[1]}")
            return

        if is_add:
            data = set_total_deposited(amount, add=True, account=account)
            send_fn(
                f"✅ {_acct_label(account)} — Deposit added: {_fmt_money(amount)}\n"
                f"Total deposited: ${data['total_deposited']:,.2f}"
            )
        else:
            data = set_total_deposited(amount, add=False, account=account)
            send_fn(
                f"✅ {_acct_label(account)} — Total deposited set: ${data['total_deposited']:,.2f}"
            )
        return

    if sub == "history":
        _cash_history(send_fn, account)
        return

    raw = args[0].replace(",", "").replace("$", "")
    try:
        balance = float(raw)
    except ValueError:
        send_fn(
            "Usage:\n"
            "  /cash 12345 — update cash balance\n"
            "  /cash deposit 50000 — set total deposited\n"
            "  /cash deposit +5000 — add a deposit\n"
            "  /cash history — balance snapshots\n"
            "  /cash — show full account P/L"
        )
        return

    data = update_cash_balance(balance, account=account)
    send_fn(
        f"✅ {_acct_label(account)} — Cash balance updated: ${balance:,.2f}\n"
        f"Use /cash to see full account P/L breakdown"
    )


def _cash_show(send_fn, get_spot_fn, account: str = "brad"):
    cash_data = get_cash_data(account=account)

    if cash_data.get("total_deposited", 0) == 0 and cash_data.get("cash_balance", 0) == 0:
        send_fn(
            f"💰 {_acct_label(account)} — No cash data yet.\n\n"
            f"Step 1: /cash deposit 50000\n"
            f"  (total cash you've put into the account)\n\n"
            f"Step 2: /cash 12345\n"
            f"  (current cash balance from your broker)\n\n"
            f"The bot computes realized P/L automatically\n"
            f"from the difference."
        )
        return

    holdings = get_all_holdings(account=account)
    price_map = {}
    for ticker in holdings:
        try:
            price_map[ticker] = get_spot_fn(ticker)
        except Exception as e:
            log.warning(f"Cash P/L: price fetch failed for {ticker}: {e}")

    pnl = calc_account_pnl(price_map, account=account)

    dep_emoji = "💰"
    total_emoji = "🟢" if pnl["total_pnl"] >= 0 else "🔴"
    real_emoji = "🟢" if pnl["realized_pnl"] >= 0 else "🔴"
    unreal_emoji = "🟢" if pnl["unrealized_pnl"] >= 0 else "🔴"
    last = pnl["last_updated"] or "never"

    missing = [t for t in holdings if t not in price_map]

    lines = [
        f"{dep_emoji} {_acct_label(account)} — ACCOUNT P/L\n",
        f"Total Deposited:  ${pnl['total_deposited']:,.2f}",
        f"Cash Balance:     ${pnl['cash_balance']:,.2f}",
        f"Holdings Cost:    ${pnl['holdings_cost']:,.2f}",
        f"Holdings Value:   ${pnl['holdings_value']:,.2f}",
    ]

    if pnl.get("fund_value", 0) > 0 or pnl.get("fund_cost", 0) > 0:
        lines.append(f"Funds Cost:       ${pnl['fund_cost']:,.2f}")
        lines.append(f"Funds Value:      ${pnl['fund_value']:,.2f}")

    lines.extend([
        f"Account Value:    ${pnl['account_value']:,.2f}",
        "",
        f"{unreal_emoji} Unrealized P/L:  {_fmt_money(pnl['unrealized_pnl'])}",
        f"  (shares + funds vs purchase prices)",
        f"{real_emoji} Realized P/L:    {_fmt_money(pnl['realized_pnl'])}",
        f"  (day trades, closed options, dividends, etc.)",
        f"{total_emoji} Total P/L:       {_fmt_money(pnl['total_pnl'])} ({_fmt_pct(pnl['return_pct'])})",
        "",
        f"Last cash update: {last}",
    ])

    if missing:
        lines.append(f"⚠️ No price data: {', '.join(missing)}")

    send_fn("\n".join(lines))


def _cash_history(send_fn, account: str = "brad"):
    data = get_cash_data(account=account)
    history = data.get("history", [])
    deposited = data.get("total_deposited", 0)

    if not history:
        send_fn(f"💰 {_acct_label(account)} — No cash history yet. Use /cash BALANCE to start tracking.")
        return

    recent = history[-20:]

    lines = [
        f"💰 {_acct_label(account)} — CASH BALANCE HISTORY\n",
        f"Total deposited: ${deposited:,.2f}\n",
    ]

    for snap in recent:
        cash = snap["cash"]
        lines.append(f"{snap['date']}  ${cash:,.2f}")

    if len(history) > 20:
        lines.append(f"\n(showing last 20 of {len(history)} snapshots)")

    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /fund COMMAND (v3.2)
# ═══════════════════════════════════════════════════════════

def handle_fund(args: list, send_fn, account: str = "brad"):
    if not args:
        _fund_show(send_fn, account)
        return

    sub = args[0].lower()

    if sub == "set" or sub == "basis":
        if len(args) < 2:
            send_fn("Usage: /fund set 50000 — set total amount invested")
            return
        try:
            amount = float(args[1].replace(",", "").replace("$", ""))
        except ValueError:
            send_fn(f"Bad amount: {args[1]}")
            return

        fund = set_mutual_fund_basis(amount, account=account)
        send_fn(
            f"✅ {_acct_label(account)} — Mutual Fund cost basis set\n"
            f"Total invested: ${fund['cost_basis']:,.2f}"
        )

    elif sub == "update":
        if len(args) < 2:
            send_fn("Usage: /fund update 54200 — update current market value")
            return
        try:
            amount = float(args[1].replace(",", "").replace("$", ""))
        except ValueError:
            send_fn(f"Bad amount: {args[1]}")
            return

        fund = update_mutual_fund_value(amount, account=account)
        pnl = calc_mutual_fund_pnl(account=account)

        emoji = "🟢" if pnl["pnl"] >= 0 else "🔴"
        send_fn(
            f"{emoji} {_acct_label(account)} — Mutual Fund updated\n"
            f"Cost basis: ${pnl['cost_basis']:,.2f}\n"
            f"Current value: ${pnl['current_value']:,.2f}\n"
            f"P/L: {_fmt_money(pnl['pnl'])} ({_fmt_pct(pnl['return_pct'])})"
        )

    elif sub == "history":
        _fund_history(send_fn, account)

    else:
        send_fn(
            "Usage:\n"
            "  /fund — show current P/L\n"
            "  /fund set 50000 — set total invested\n"
            "  /fund update 54200 — update current value\n"
            "  /fund basis 52000 — adjust cost basis\n"
            "  /fund history — show snapshots over time"
        )


def _fund_show(send_fn, account: str = "brad"):
    pnl = calc_mutual_fund_pnl(account=account)

    if pnl["cost_basis"] == 0 and pnl["current_value"] == 0:
        send_fn(
            f"💼 {_acct_label(account)} — No mutual fund data yet.\n"
            f"Use /fund set 50000 to set your total invested,\n"
            f"then /fund update 54200 to log current value."
        )
        return

    emoji = "🟢" if pnl["pnl"] >= 0 else "🔴"
    last = pnl["last_updated"] or "never"

    lines = [
        f"💼 {_acct_label(account)} — MUTUAL FUNDS / ETFs\n",
        f"Cost basis: ${pnl['cost_basis']:,.2f}",
        f"Current value: ${pnl['current_value']:,.2f}",
        f"{emoji} P/L: {_fmt_money(pnl['pnl'])} ({_fmt_pct(pnl['return_pct'])})",
        f"Last updated: {last}",
        f"Snapshots: {pnl['num_snapshots']}",
    ]
    send_fn("\n".join(lines))


def _fund_history(send_fn, account: str = "brad"):
    fund = get_mutual_fund(account=account)
    history = fund.get("history", [])
    cost = fund.get("cost_basis", 0)

    if not history:
        send_fn(f"💼 {_acct_label(account)} — No fund history yet. Use /fund update VALUE to start tracking.")
        return

    recent = history[-20:]

    lines = [f"💼 {_acct_label(account)} — FUND VALUE HISTORY\n"]
    lines.append(f"Cost basis: ${cost:,.2f}\n")

    for snap in recent:
        val = snap["value"]
        pnl = val - cost
        pct = (pnl / cost * 100) if cost > 0 else 0
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{snap['date']}  ${val:,.2f}  "
            f"{emoji} {_fmt_money(pnl)} ({_fmt_pct(pct)})"
        )

    if len(history) > 20:
        lines.append(f"\n(showing last 20 of {len(history)} snapshots)")

    send_fn("\n".join(lines))


# ═══════════════════════════════════════════════════════════
# /spread COMMAND (v3.4 — Debit Spread Tracking)
# ═══════════════════════════════════════════════════════════
#
# /spread add AAPL 570/571 0.65 2026-03-14           → 1 contract
# /spread add AAPL 570/571 0.65 2026-03-14 x3        → 3 contracts
# /spread close sp_001 0.91                           → closed at $0.91
# /spread stop sp_001                                 → stopped out (total loss)
# /spread expire sp_001                               → expired ITM (max profit)
# /spread expire sp_001 otm                           → expired OTM (total loss)
# /spread list                                        → show open spreads
# /spread history                                     → show closed spreads + P/L
# /spread summary                                     → win rate + totals

def handle_spread(args: list, send_fn, account: str = "brad"):
    """Router for /spread subcommands."""
    if not args:
        send_fn(
            "Usage:\n"
            "  /spread add TICKER LONG/SHORT DEBIT EXP [xN]\n"
            "  /spread close SP_ID PRICE\n"
            "  /spread stop SP_ID\n"
            "  /spread expire SP_ID [otm]\n"
            "  /spread list\n"
            "  /spread history\n"
            "  /spread summary\n\n"
            "Example: /spread add AAPL 570/571 0.65 2026-03-14 x3"
        )
        return

    sub = args[0].lower()

    if sub == "add":
        _spread_add(args[1:], send_fn, account)
    elif sub == "close":
        _spread_close(args[1:], send_fn, account)
    elif sub == "stop":
        _spread_stop(args[1:], send_fn, account)
    elif sub == "expire":
        _spread_expire(args[1:], send_fn, account)
    elif sub == "list":
        _spread_list(send_fn, account)
    elif sub == "history":
        _spread_history(send_fn, account)
    elif sub == "summary":
        _spread_summary_cmd(send_fn, account)
    else:
        send_fn(f"Unknown: /spread {sub}\nUse: /spread add|close|stop|expire|list|history|summary")


def _spread_add(args: list, send_fn, account: str = "brad"):
    """
    /spread add AAPL 570/571 0.65 2026-03-14
    /spread add AAPL 570/571 0.65 2026-03-14 x3
    """
    if len(args) < 4:
        send_fn(
            "Usage: /spread add TICKER LONG/SHORT DEBIT EXP [xN]\n"
            "Example: /spread add AAPL 570/571 0.65 2026-03-14 x3"
        )
        return

    ticker = args[0].upper()

    # Parse strikes: "570/571"
    strikes_str = args[1]
    if "/" not in strikes_str:
        send_fn(f"Bad strikes: {strikes_str} — use LONG/SHORT (e.g. 570/571)")
        return

    parts = strikes_str.split("/")
    try:
        long_strike = float(parts[0])
        short_strike = float(parts[1])
    except (ValueError, IndexError):
        send_fn(f"Bad strikes: {strikes_str} — use LONG/SHORT (e.g. 570/571)")
        return

    try:
        debit = float(args[2])
    except ValueError:
        send_fn(f"Bad debit: {args[2]}")
        return

    exp = args[3]

    contracts = 1
    if len(args) >= 5:
        c_str = args[4].lower()
        if c_str.startswith("x"):
            c_str = c_str[1:]
        try:
            contracts = int(c_str)
        except ValueError:
            send_fn(f"Bad contract count: {args[4]} — use x3")
            return

    spread = add_spread(
        ticker=ticker,
        long_strike=long_strike,
        short_strike=short_strike,
        debit=debit,
        exp=exp,
        contracts=contracts,
        account=account,
    )

    total_risk = debit * contracts * 100
    targets = spread["targets"]

    lines = [
        f"✅ {_acct_label(account)} — Opened {spread['id']}",
        f"BULL CALL {ticker} ${long_strike}/{short_strike}",
        f"Width: ${spread['width']:.2f} | Debit: ${debit:.2f} x{contracts} = ${total_risk:,.0f} risk",
        f"Exp: {exp}",
        "",
        "📊 Exit Targets:",
        f"  Same Day (30%): ${targets['same_day']:.2f}",
        f"  Next Day (35%): ${targets['next_day']:.2f}",
        f"  Extended (50%): ${targets['extended']:.2f}",
        f"  Stop Loss:      ${targets['stop']:.2f}",
    ]

    send_fn("\n".join(lines))


def _spread_close(args: list, send_fn, account: str = "brad"):
    """
    /spread close sp_001 0.91
    """
    if len(args) < 2:
        send_fn("Usage: /spread close SP_ID PRICE\nExample: /spread close sp_001 0.91")
        return

    sp_id = args[0]
    try:
        close_price = float(args[1])
    except ValueError:
        send_fn(f"Bad price: {args[1]}")
        return

    result = close_spread(sp_id, close_price, account=account)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = result.get("pnl", 0)
    emoji = _pnl_emoji(pnl)
    total_risk = result["debit"] * result["contracts"] * 100
    ror_pct = round(pnl / total_risk * 100, 1) if total_risk > 0 else 0

    send_fn(
        f"✅ {_acct_label(account)} — Closed {sp_id}\n"
        f"{result['ticker']} ${result['long']}/{result['short']}\n"
        f"${result['debit']:.2f} → ${close_price:.2f} x{result['contracts']}\n"
        f"P/L: {_fmt_money(pnl)} ({_fmt_pct(ror_pct)} RoR) {emoji}"
    )


def _spread_stop(args: list, send_fn, account: str = "brad"):
    """
    /spread stop sp_001
    """
    if not args:
        send_fn("Usage: /spread stop SP_ID\nExample: /spread stop sp_001")
        return

    sp_id = args[0]
    result = stop_spread(sp_id, account=account)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = result.get("pnl", 0)
    total_risk = result["debit"] * result["contracts"] * 100

    send_fn(
        f"🛑 {_acct_label(account)} — Stopped {sp_id}\n"
        f"{result['ticker']} ${result['long']}/{result['short']}\n"
        f"Total loss: {_fmt_money(pnl)} (${total_risk:,.0f} at risk)"
    )


def _spread_expire(args: list, send_fn, account: str = "brad"):
    """
    /spread expire sp_001       → expired ITM (max profit)
    /spread expire sp_001 otm   → expired OTM (total loss)
    """
    if not args:
        send_fn("Usage: /spread expire SP_ID [otm]\nDefault: expires ITM (max profit)")
        return

    sp_id = args[0]
    itm = True
    if len(args) >= 2 and args[1].lower() == "otm":
        itm = False

    result = expire_spread_fn(sp_id, itm=itm, account=account)

    if "error" in result:
        send_fn(f"❌ {result['error']}")
        return

    pnl = result.get("pnl", 0)
    emoji = _pnl_emoji(pnl)
    status_label = "ITM (max profit)" if itm else "OTM (total loss)"

    send_fn(
        f"💀 {_acct_label(account)} — Expired {sp_id} {status_label}\n"
        f"{result['ticker']} ${result['long']}/{result['short']}\n"
        f"P/L: {_fmt_money(pnl)} {emoji}"
    )


def _spread_list(send_fn, account: str = "brad"):
    """Show all open spreads."""
    spreads = get_open_spreads(account=account)

    if not spreads:
        send_fn(f"📋 {_acct_label(account)} — No open spreads. Use /spread add to log one.")
        return

    total_risk = sum(s["debit"] * s["contracts"] * 100 for s in spreads)

    lines = [f"📋 {_acct_label(account)} — OPEN SPREADS ({len(spreads)} positions)\n"]

    for s in spreads:
        exp_short = s["exp"][5:] if len(s["exp"]) >= 10 else s["exp"]
        risk = s["debit"] * s["contracts"] * 100
        contracts_str = f" x{s['contracts']}" if s["contracts"] > 1 else ""

        lines.append(
            f"{s['id']}  {s['ticker']}  "
            f"${s['long']}/{s['short']}  "
            f"@${s['debit']:.2f}{contracts_str}  "
            f"{exp_short}  "
            f"${risk:,.0f} risk"
        )

    lines.append(f"\nTotal open risk: ${total_risk:,.0f}")
    lines.append("Use /spread close ID PRICE or /spread stop ID")
    send_fn("\n".join(lines))


def _spread_history(send_fn, account: str = "brad"):
    """Show closed spreads with P/L."""
    all_sp = get_all_spreads(account=account)
    closed = [s for s in all_sp if s.get("status") != "open"]

    if not closed:
        send_fn(f"📋 {_acct_label(account)} — No spread history yet.")
        return

    closed.sort(key=lambda s: s.get("close_date", ""), reverse=True)

    lines = [f"📋 {_acct_label(account)} — SPREAD HISTORY ({len(closed)} closed)\n"]
    total_pnl = 0.0

    for s in closed[:20]:
        pnl = calc_spread_pnl(s)
        total_pnl += pnl
        status = _opt_status_emoji(s["status"])
        contracts_str = f" x{s['contracts']}" if s["contracts"] > 1 else ""

        lines.append(
            f"{status} {s['id']}  {s['ticker']}  "
            f"${s['long']}/{s['short']}  "
            f"${s['debit']:.2f}→${s.get('close_price', 0):.2f}{contracts_str}  "
            f"{_fmt_money(pnl)} {_pnl_emoji(pnl)}"
        )

    lines.append(f"\nTotal Realized: {_fmt_money(total_pnl)} {_pnl_emoji(total_pnl)}")

    if len(closed) > 20:
        lines.append(f"(showing 20 of {len(closed)})")

    send_fn("\n".join(lines))


def _spread_summary_cmd(send_fn, account: str = "brad"):
    """Show win rate and totals."""
    summary = calc_spread_summary(account=account)

    if summary["total_spreads"] == 0:
        send_fn(f"📊 {_acct_label(account)} — No spread data yet.")
        return

    emoji = _pnl_emoji(summary["total_realized"])

    lines = [
        f"📊 {_acct_label(account)} — SPREAD SUMMARY\n",
        f"Total Trades:   {summary['total_spreads']}",
        f"Open:           {summary['open_count']}",
        f"Closed:         {summary['closed_count']}",
        f"Wins:           {summary['wins']}",
        f"Losses:         {summary['losses']}",
        f"Win Rate:       {summary['win_rate']:.0f}%",
        f"Open Risk:      ${summary['total_open_risk']:,.0f}",
        f"Realized P/L:   {_fmt_money(summary['total_realized'])} {emoji}",
    ]

    send_fn("\n".join(lines))
