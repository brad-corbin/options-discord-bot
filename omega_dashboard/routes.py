"""Dashboard Blueprint routes.

Phase 1 scope:
  - Auth: /login + /logout, single-password, Flask session, 30-day persistence
  - Nav: header with logo, page tabs, account switcher
  - Pages: /dashboard, /trading, /portfolio, /diagnostic — placeholder content
  - Account context: cookie-persisted across navigation

No data layer yet. No Redis/Sheets reads. No writes. Just the framework.
"""
import os
import logging
from datetime import timedelta
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, make_response, flash
)

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()

# Account UI views — maps to underlying account keys defined in the bot.
# Phase 1 uses these as labels only; real account-keyed reads come in phase 3.
ACCOUNTS = [
    {"key": "combined",  "label": "Combined",    "color": "combined"},
    {"key": "mine",      "label": "Corbin",      "color": "mine"},
    {"key": "mom",       "label": "Volkman",     "color": "mom"},
    {"key": "partner",   "label": "Partnership", "color": "partner"},
    {"key": "kyleigh",   "label": "Kyleigh",     "color": "kyleigh"},
    {"key": "clay",      "label": "Clay",        "color": "clay"},
]
ACCOUNT_KEYS = {a["key"] for a in ACCOUNTS}

# Top-nav page tabs
PAGE_TABS = [
    {"key": "dashboard",  "label": "Command",    "endpoint": "dashboard.command_center"},
    {"key": "trading",    "label": "Trading",    "endpoint": "dashboard.trading"},
    {"key": "portfolio",  "label": "Portfolio",  "endpoint": "dashboard.portfolio"},
    {"key": "diagnostic", "label": "Diagnostic", "endpoint": "dashboard.diagnostic"},
    {"key": "restore",    "label": "Durability", "endpoint": "dashboard.restore"},
]

# Session lifetime — 30 days, matching the spec
SESSION_LIFETIME_DAYS = 30

# ──────────────────────────────────────────────────────────
# Blueprint
# ──────────────────────────────────────────────────────────

dashboard_bp = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/dashboard/static",
)


@dashboard_bp.before_app_request
def _make_session_permanent():
    """Apply session lifetime once per app request, and start the snapshot
    scheduler the first time a request lands."""
    session.permanent = True
    from flask import current_app
    if "_omega_session_set" not in current_app.config:
        current_app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)
        current_app.config["_omega_session_set"] = True

    # Phase 2: start the daily snapshot scheduler on first request
    if "_omega_scheduler_started" not in current_app.config:
        try:
            from .scheduler import start_snapshot_scheduler
            start_snapshot_scheduler()
        except Exception as e:
            log.warning(f"Snapshot scheduler start failed: {e}")
        current_app.config["_omega_scheduler_started"] = True


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def login_required(view):
    """Redirect unauthenticated requests to /login."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("dashboard.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_active_account():
    """Resolve the active account from cookie, default to combined."""
    raw = request.cookies.get("omega_account", "combined")
    if raw not in ACCOUNT_KEYS:
        raw = "combined"
    for acc in ACCOUNTS:
        if acc["key"] == raw:
            return acc
    return ACCOUNTS[0]


def render_page(template_name, page_key, **context):
    """Centralized render with all the bits every page needs."""
    active_account = get_active_account()
    return render_template(
        template_name,
        active_page=page_key,
        active_account=active_account,
        accounts=ACCOUNTS,
        page_tabs=PAGE_TABS,
        **context,
    )


# ──────────────────────────────────────────────────────────
# Auth routes
# ──────────────────────────────────────────────────────────

@dashboard_bp.route("/", methods=["GET"])
def root():
    """Landing — redirect to dashboard if logged in, login otherwise."""
    if session.get("auth"):
        return redirect(url_for("dashboard.command_center"))
    return redirect(url_for("dashboard.login"))


@dashboard_bp.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        # Fail loud rather than open the door silently.
        return (
            "DASHBOARD_PASSWORD env var not set. "
            "Set it on Render and redeploy.",
            500,
        )

    error = None
    if request.method == "POST":
        attempt = (request.form.get("password") or "").strip()
        if attempt and attempt == DASHBOARD_PASSWORD:
            session["auth"] = True
            log.info("Dashboard login successful")
            next_url = request.args.get("next") or url_for("dashboard.command_center")
            # Only allow same-origin redirects
            if not next_url.startswith("/"):
                next_url = url_for("dashboard.command_center")
            return redirect(next_url)
        else:
            error = "Incorrect password."
            log.warning("Dashboard login attempt failed")

    return render_template("dashboard/login.html", error=error)


@dashboard_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.pop("auth", None)
    return redirect(url_for("dashboard.login"))


# ──────────────────────────────────────────────────────────
# Account switching
# ──────────────────────────────────────────────────────────

@dashboard_bp.route("/account/<account_key>", methods=["POST", "GET"])
@login_required
def set_account(account_key):
    """Set the active account cookie, then bounce back to the referring page."""
    if account_key not in ACCOUNT_KEYS:
        account_key = "combined"

    # Bounce back to whichever page the user came from
    referer = request.referrer or url_for("dashboard.command_center")
    if not referer.startswith(request.host_url):
        referer = url_for("dashboard.command_center")

    resp = make_response(redirect(referer))
    # Cookie persists for the session lifetime
    resp.set_cookie(
        "omega_account",
        account_key,
        max_age=60 * 60 * 24 * SESSION_LIFETIME_DAYS,
        httponly=False,  # Read-only cookie, no sensitive data
        samesite="Lax",
    )
    return resp


# ──────────────────────────────────────────────────────────
# Page routes
# ──────────────────────────────────────────────────────────

@dashboard_bp.route("/dashboard", methods=["GET"])
@login_required
def command_center():
    from . import data
    active_account = get_active_account()
    page_data = data.command_center_data(active_account["key"])
    return render_page(
        "dashboard/command_center.html",
        page_key="dashboard",
        page_data=page_data,
    )


@dashboard_bp.route("/trading", methods=["GET"])
@login_required
def trading():
    return render_page("dashboard/trading.html", page_key="trading")


@dashboard_bp.route("/portfolio", methods=["GET"])
@login_required
def portfolio():
    """Portfolio entry page — defaults to cash sub-tab."""
    return redirect(url_for("dashboard.portfolio_section", section="cash"))


@dashboard_bp.route("/portfolio/<section>", methods=["GET"])
@login_required
def portfolio_section(section):
    """Sub-tabbed portfolio entry pages."""
    valid_sections = {"cash", "holdings", "options", "spreads", "rolls", "transfers", "settings"}
    if section not in valid_sections:
        return redirect(url_for("dashboard.portfolio_section", section="cash"))

    # Active underlying account for entry — resolved from query param or default
    from . import writes
    active_underlying = (request.args.get("acct") or "").strip().lower()
    if active_underlying not in writes.ALL_UNDERLYING_ACCOUNTS:
        active_underlying = "brad"

    page_data = writes.portfolio_page_data(
        active_underlying,
        show_closed=(request.args.get("show_closed") == "1"),
        since_date=(request.args.get("since") or "").strip() or None,
        ticker_filter=(request.args.get("ticker") or "").strip() or None,
    )
    page_data["active_section"] = section

    # Date helpers for history filter dropdowns
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    page_data["today_30"] = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    page_data["today_90"] = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    page_data["year_start"] = f"{now.year}-01-01"

    # Settings tab needs audit log for undo UI
    if section == "settings":
        try:
            page_data["recent_audits"] = writes.get_recent_audit_entries(limit=15)
        except Exception:
            page_data["recent_audits"] = []

    flash_msg = session.pop("_portfolio_flash", None)
    flash_kind = session.pop("_portfolio_flash_kind", "info")

    return render_page(
        "dashboard/portfolio.html",
        page_key="portfolio",
        page_data=page_data,
        flash_msg=flash_msg,
        flash_kind=flash_kind,
    )


def _flash(msg: str, kind: str = "info"):
    session["_portfolio_flash"] = msg
    session["_portfolio_flash_kind"] = kind


def _bounce(section: str, acct: str = None):
    url = url_for("dashboard.portfolio_section", section=section)
    if acct:
        url += f"?acct={acct}"
    return redirect(url)


# ─── CASH ROUTES ─────────────────────────────────────────

@dashboard_bp.route("/portfolio/cash/add", methods=["POST"])
@login_required
def portfolio_cash_add():
    from . import writes
    acct = request.form.get("acct", "brad")
    event_type = request.form.get("event_type", "deposit")
    raw_amount = (request.form.get("amount") or "").strip()

    # Sanity check before calling writes
    if not raw_amount:
        _flash("Cash add failed: Please enter an amount before submitting.", "error")
        return _bounce("cash", acct)

    # Phase 4.5 — "Set balance to" pseudo-type. The user enters a target
    # balance; we compute the delta against the current sub-account balance
    # and create a manual_set for that delta (so the user doesn't have to
    # do the math). Only acts on the chosen sub-account, not the whole acct.
    if event_type == "set_balance":
        target_str = raw_amount.replace("$", "").replace(",", "").replace(" ", "")
        try:
            target = float(target_str) if target_str else None
        except Exception:
            target = None
        if target is None:
            _flash(f"Set balance failed: '{raw_amount}' isn't a valid number.", "error")
            return _bounce("cash", acct)
        sub = (request.form.get("subaccount") or "").strip() or "Brokerage"
        breakdown = writes.calc_cash_breakdown(acct)
        current_sub = float(breakdown.get(sub, 0.0))
        delta = round(target - current_sub, 2)
        if abs(delta) < 0.005:
            _flash(f"Already at ${target:,.2f} in {sub} — no adjustment needed.", "info")
            return _bounce("cash", acct)
        result = writes.add_cash_event(
            account=acct,
            event_type="manual_set",
            amount=delta,
            subaccount=sub,
            date=request.form.get("date"),
            note=(request.form.get("note") or f"Set {sub} to ${target:,.2f}").strip(),
        )
        if result.get("ok"):
            _flash(f"{sub} adjusted by ${delta:+,.2f} → balance now ${target:,.2f}", "success")
        else:
            _flash(f"Set balance failed: {result.get('error')}", "error")
        return _bounce("cash", acct)

    result = writes.add_cash_event(
        account=acct,
        event_type=event_type,
        amount=raw_amount,
        subaccount=request.form.get("subaccount"),
        date=request.form.get("date"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        _flash(f"Cash {event_type}: ${result['entry']['amount']:.2f} · balance now ${result['new_balance']:,.2f}", "success")
    else:
        err = result.get("error", "unknown error")
        _flash(f"Cash add failed: {err} (you entered: '{raw_amount}')", "error")
    return _bounce("cash", acct)


@dashboard_bp.route("/portfolio/cash/delete/<entry_id>", methods=["POST"])
@login_required
def portfolio_cash_delete(entry_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.delete_cash_event(acct, entry_id)
    if result.get("ok"):
        _flash(f"Deleted cash event · balance now ${result['new_balance']:,.2f}", "success")
    else:
        _flash(f"Delete failed: {result.get('error')}", "error")
    return _bounce("cash", acct)


# ─── HOLDINGS ROUTES ─────────────────────────────────────

@dashboard_bp.route("/portfolio/holdings/add", methods=["POST"])
@login_required
def portfolio_holding_add():
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.add_holding(
        account=acct,
        ticker=request.form.get("ticker"),
        shares=request.form.get("shares"),
        cost_basis=request.form.get("cost_basis"),
        subaccount=request.form.get("subaccount"),
        tag=request.form.get("tag"),
        date=request.form.get("date"),
    )
    if result.get("ok"):
        _flash(f"Added {result['holding']['shares']} {result['ticker']} @ ${result['holding']['cost_basis']:.2f}", "success")
    else:
        _flash(f"Add failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/holdings/sell", methods=["POST"])
@login_required
def portfolio_holding_sell():
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.sell_holding(
        account=acct,
        ticker=request.form.get("ticker"),
        shares=request.form.get("shares"),
        sell_price=request.form.get("sell_price"),
        date=request.form.get("date"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        _flash(f"Sold {result['ticker']} for ${result['proceeds']:,.2f}", "success")
    else:
        _flash(f"Sell failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/holdings/edit", methods=["POST"])
@login_required
def portfolio_holding_edit():
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.edit_holding(
        account=acct,
        ticker=request.form.get("ticker"),
        shares=request.form.get("shares") or None,
        cost_basis=request.form.get("cost_basis") or None,
        subaccount=request.form.get("subaccount") or None,
        tag=request.form.get("tag") or None,
    )
    if result.get("ok"):
        _flash(f"Edited {result['ticker']}", "success")
    else:
        _flash(f"Edit failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


# Lump-sum routes
@dashboard_bp.route("/portfolio/lumpsum/add", methods=["POST"])
@login_required
def portfolio_lumpsum_add():
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.add_lumpsum(
        account=acct,
        label=request.form.get("label"),
        value=request.form.get("value"),
        subaccount=request.form.get("subaccount"),
        as_of=request.form.get("as_of"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        _flash(f"Added lump-sum '{result['entry']['label']}' = ${result['entry']['value']:,.2f}", "success")
    else:
        _flash(f"Add failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/lumpsum/update/<entry_id>", methods=["POST"])
@login_required
def portfolio_lumpsum_update(entry_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.update_lumpsum(
        account=acct,
        entry_id=entry_id,
        value=request.form.get("value") or None,
        as_of=request.form.get("as_of") or None,
        label=request.form.get("label") or None,
        note=request.form.get("note") or None,
    )
    if result.get("ok"):
        _flash(f"Updated '{result['entry']['label']}'", "success")
    else:
        _flash(f"Update failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/lumpsum/delete/<entry_id>", methods=["POST"])
@login_required
def portfolio_lumpsum_delete(entry_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.delete_lumpsum(acct, entry_id)
    if result.get("ok"):
        _flash("Lump-sum deleted", "success")
    else:
        _flash(f"Delete failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


# ─── OPTIONS ROUTES ─────────────────────────────────────

@dashboard_bp.route("/portfolio/options/add", methods=["POST"])
@login_required
def portfolio_option_add():
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.add_option(
        account=acct,
        ticker=request.form.get("ticker"),
        opt_type=request.form.get("opt_type"),
        strike=request.form.get("strike"),
        exp=request.form.get("exp"),
        premium=request.form.get("premium"),
        contracts=request.form.get("contracts", 1),
        direction=request.form.get("direction"),
        subaccount=request.form.get("subaccount"),
        category=request.form.get("category"),
        open_date=request.form.get("open_date"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        o = result['option']
        _flash(f"Opened {o['type']} {o['ticker']} ${o['strike']} {o['exp']} @ ${o['premium']}", "success")
    else:
        _flash(f"Add failed: {result.get('error')}", "error")
    return _bounce("options", acct)


@dashboard_bp.route("/portfolio/options/close/<opt_id>", methods=["POST"])
@login_required
def portfolio_option_close(opt_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    # Phase 4.5 — auto-handle assignment (defaults to ON)
    auto_handle_raw = request.form.get("auto_handle_shares", "1")
    auto_handle = (auto_handle_raw or "1").strip() not in ("0", "false", "no", "")
    actual_fill_raw = request.form.get("actual_fill_price", "").strip()
    actual_fill = actual_fill_raw if actual_fill_raw else None

    result = writes.close_option(
        account=acct,
        opt_id=opt_id,
        status=request.form.get("status"),
        close_premium=request.form.get("close_premium"),
        close_date=request.form.get("close_date"),
        note=request.form.get("note"),
        contracts_to_close=request.form.get("contracts_to_close"),
        auto_handle_shares=auto_handle,
        actual_fill_price=actual_fill,
    )
    if result.get("ok"):
        if result.get("partial"):
            n_closed = result["closed_portion"]["contracts"]
            n_remaining = result["remaining"]["contracts"]
            msg = f"Closed {n_closed} contracts ({n_remaining} still open)"
        else:
            msg = f"Option marked {result['option']['status']}"
        # Phase 4.5 — note the auto-handle outcome if it ran
        if result.get("auto_handled"):
            ar = result["auto_handled"]
            if ar.get("kind") == "csp_assignment":
                msg += f" · bought {ar['shares_acquired']} {ar['ticker']} @ ${ar['fill_price']}"
            elif ar.get("kind") == "cc_called_away":
                msg += f" · sold {ar['shares_sold']} {ar['ticker']} @ ${ar['fill_price']}"
        _flash(msg, "success")
    else:
        _flash(f"Close failed: {result.get('error')}", "error")
    return _bounce("options", acct)


@dashboard_bp.route("/portfolio/options/delete/<opt_id>", methods=["POST"])
@login_required
def portfolio_option_delete(opt_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    also_cash = request.form.get("also_cash") == "1"
    result = writes.delete_option(acct, opt_id, also_delete_cash=also_cash)
    if result.get("ok"):
        msg = "Option deleted"
        if result.get("linked_cash_deleted"):
            msg += f" + {result['linked_cash_deleted']} cash event(s)"
        elif result.get("linked_cash_count"):
            msg += f" (kept {result['linked_cash_count']} cash event(s))"
        _flash(msg, "success")
    else:
        _flash(f"Delete failed: {result.get('error')}", "error")
    return _bounce("options", acct)


@dashboard_bp.route("/portfolio/options/edit/<opt_id>", methods=["POST"])
@login_required
def portfolio_option_edit(opt_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    fields = {}
    for k in ("strike", "exp", "premium", "contracts", "subaccount", "category", "tag", "open_date", "note"):
        v = request.form.get(k)
        if v not in (None, ""):
            fields[k] = v
    result = writes.edit_option(acct, opt_id, **fields)
    if result.get("ok"):
        _flash("Option edited", "success")
    else:
        _flash(f"Edit failed: {result.get('error')}", "error")
    return _bounce("options", acct)


# ─── ROLL ROUTE ──────────────────────────────────────────

@dashboard_bp.route("/portfolio/rolls/execute", methods=["POST"])
@login_required
def portfolio_roll_execute():
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.roll_option(
        account=acct,
        opt_id=request.form.get("opt_id"),
        new_strike=request.form.get("new_strike"),
        new_exp=request.form.get("new_exp"),
        new_premium=request.form.get("new_premium"),
        close_premium=request.form.get("close_premium"),
        roll_date=request.form.get("roll_date"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        _flash(f"Rolled · net ${result['net_credit']:+,.2f}", "success")
    else:
        _flash(f"Roll failed: {result.get('error')}", "error")
    return _bounce("rolls", acct)


# ─── SPREADS ROUTES ─────────────────────────────────────

@dashboard_bp.route("/portfolio/spreads/add", methods=["POST"])
@login_required
def portfolio_spread_add():
    from . import writes
    acct = request.form.get("acct", "brad")
    is_credit = request.form.get("is_credit", "true").lower() == "true"
    result = writes.add_spread(
        account=acct,
        ticker=request.form.get("ticker"),
        spread_type=request.form.get("spread_type"),
        long_strike=request.form.get("long_strike"),
        short_strike=request.form.get("short_strike"),
        exp=request.form.get("exp"),
        net=request.form.get("net"),
        contracts=request.form.get("contracts", 1),
        is_credit=is_credit,
        subaccount=request.form.get("subaccount"),
        open_date=request.form.get("open_date"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        _flash(f"Added {result['spread']['type']} spread", "success")
    else:
        _flash(f"Add failed: {result.get('error')}", "error")
    return _bounce("spreads", acct)


@dashboard_bp.route("/portfolio/spreads/close/<spread_id>", methods=["POST"])
@login_required
def portfolio_spread_close(spread_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    result = writes.close_spread(
        account=acct,
        spread_id=spread_id,
        status=request.form.get("status"),
        close_value=request.form.get("close_value"),
        close_date=request.form.get("close_date"),
        note=request.form.get("note"),
        contracts_to_close=request.form.get("contracts_to_close"),
    )
    if result.get("ok"):
        if result.get("partial"):
            n_closed = result["closed_portion"]["contracts"]
            n_remaining = result["remaining"]["contracts"]
            _flash(f"Closed {n_closed} contracts ({n_remaining} still open)", "success")
        else:
            _flash(f"Spread {result['spread']['status']}", "success")
    else:
        _flash(f"Close failed: {result.get('error')}", "error")
    return _bounce("spreads", acct)


@dashboard_bp.route("/portfolio/spreads/edit/<spread_id>", methods=["POST"])
@login_required
def portfolio_spread_edit(spread_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    fields = {}
    for k in ("long_strike", "short_strike", "exp", "net", "is_credit",
              "contracts", "subaccount", "open_date", "note"):
        v = request.form.get(k)
        if v not in (None, ""):
            fields[k] = v
    result = writes.edit_spread(acct, spread_id, **fields)
    if result.get("ok"):
        _flash("Spread edited", "success")
    else:
        _flash(f"Edit failed: {result.get('error')}", "error")
    return _bounce("spreads", acct)


@dashboard_bp.route("/portfolio/spreads/delete/<spread_id>", methods=["POST"])
@login_required
def portfolio_spread_delete(spread_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    also_cash = request.form.get("also_cash") == "1"
    result = writes.delete_spread(acct, spread_id, also_delete_cash=also_cash)
    if result.get("ok"):
        msg = "Spread deleted"
        if result.get("linked_cash_deleted"):
            msg += f" + {result['linked_cash_deleted']} cash event(s)"
        _flash(msg, "success")
    else:
        _flash(f"Delete failed: {result.get('error')}", "error")
    return _bounce("spreads", acct)


@dashboard_bp.route("/portfolio/holdings/delete/<ticker>", methods=["POST"])
@login_required
def portfolio_holding_delete(ticker):
    from . import writes
    acct = request.form.get("acct", "brad")
    also_cash = request.form.get("also_cash") == "1"
    result = writes.delete_holding(acct, ticker, also_delete_cash=also_cash)
    if result.get("ok"):
        msg = f"{ticker.upper()} removed"
        if result.get("linked_cash_deleted"):
            msg += f" + {result['linked_cash_deleted']} cash event(s) reversed"
        _flash(msg, "success")
    else:
        _flash(f"Delete failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/inspect/<kind>/<item_id>", methods=["GET"])
@login_required
def portfolio_inspect(kind, item_id):
    """JSON endpoint — returns linked cash events for a given position so
    the delete confirmation modal can show what will be affected."""
    from . import writes
    acct = request.args.get("acct", "brad")
    if kind == "option":
        info = writes.inspect_option_links(acct, item_id)
    elif kind == "spread":
        info = writes.inspect_spread_links(acct, item_id)
    elif kind == "holding":
        info = writes.inspect_holding_links(acct, item_id)
    else:
        return {"error": "unknown kind"}, 400
    return info


@dashboard_bp.route("/portfolio/audit", methods=["GET"])
@login_required
def portfolio_audit():
    """Recent audit entries for the undo UI."""
    from . import writes
    entries = writes.get_recent_audit_entries(limit=20)
    return {"entries": entries}


@dashboard_bp.route("/portfolio/audit/undo", methods=["POST"])
@login_required
def portfolio_audit_undo():
    from . import writes
    result = writes.undo_audit_entry(
        timestamp=request.form.get("timestamp", ""),
        op=request.form.get("op", ""),
        account=request.form.get("account", ""),
        target=request.form.get("target", ""),
    )
    if result.get("ok"):
        _flash(result.get("msg") or "Undone", "success")
    else:
        _flash(f"Undo failed: {result.get('error')}", "error")
    return _bounce("settings")


# ─── TRANSFERS ROUTES ───────────────────────────────────

@dashboard_bp.route("/portfolio/transfers/add", methods=["POST"])
@login_required
def portfolio_transfer_add():
    from . import writes
    recipient = request.form.get("recipient", "kyleigh")
    src_acct = request.form.get("acct", "brad")

    # Phase 4.5 — direction toggle replaces "negative amount = reverse" magic
    direction = request.form.get("direction", "pay")  # "pay" or "receive"
    raw_amount = (request.form.get("amount") or "").strip()

    # Always strip any sign the user typed; we'll apply direction
    # ourselves so the form is unambiguous.
    raw_clean = raw_amount.replace("$", "").replace(",", "").replace(" ", "").lstrip("-+")
    try:
        magnitude = float(raw_clean) if raw_clean else 0
    except Exception:
        magnitude = 0

    if magnitude <= 0:
        _flash("Transfer failed: enter a positive amount", "error")
        return _bounce("transfers", src_acct)

    # pay = positive (mom paying recipient); receive = negative (recipient sending in)
    signed = magnitude if direction == "pay" else -magnitude

    result = writes.add_transfer(
        recipient=recipient,
        account=src_acct,
        amount=signed,
        subaccount=request.form.get("subaccount"),
        date=request.form.get("date"),
        note=request.form.get("note"),
    )
    if result.get("ok"):
        verb = "Paid" if direction == "pay" else "Received from"
        _flash(f"{verb} {recipient.title()} ${magnitude:,.2f} · their balance now ${result['recipient_balance']:,.2f}", "success")
    else:
        _flash(f"Transfer failed: {result.get('error')}", "error")
    return _bounce("transfers", src_acct)


@dashboard_bp.route("/portfolio/transfers/delete/<recipient>/<transfer_id>", methods=["POST"])
@login_required
def portfolio_transfer_delete(recipient, transfer_id):
    from . import writes
    src_acct = request.form.get("acct", "brad")
    result = writes.delete_transfer(recipient, transfer_id)
    if result.get("ok"):
        _flash("Transfer entry deleted (cash event NOT auto-reversed)", "success")
    else:
        _flash(f"Delete failed: {result.get('error')}", "error")
    return _bounce("transfers", src_acct)


# ─── SETTINGS ROUTES ────────────────────────────────────

@dashboard_bp.route("/portfolio/settings/wipe", methods=["POST"])
@login_required
def portfolio_wipe():
    from . import writes
    acct = request.form.get("acct", "")
    confirm = request.form.get("confirm", "")
    if acct == "ALL":
        result = writes.wipe_all(confirm)
    else:
        result = writes.wipe_account(acct, confirm)
    if result.get("ok"):
        _flash(f"Wipe complete · pre-wipe snapshot: {result.get('snapshot_tab') or 'see Durability tab'}", "success")
    else:
        _flash(f"Wipe failed: {result.get('error')}", "error")
    return _bounce("settings", acct if acct != "ALL" else None)


# ─── PHASE 4.5: RETRO-FIX WHEEL ASSIGNMENTS ──────────────

@dashboard_bp.route("/portfolio/retrofix/wheel-assignments", methods=["GET"])
@login_required
def portfolio_retrofix_preview():
    """Preview page for retro-fixing legacy assignments without share lots."""
    from . import writes
    scan = writes.retrofix_scan_assignments()
    return render_page(
        "dashboard/retrofix.html",
        page_key="portfolio",
        scan=scan,
        underlying_labels=writes.UNDERLYING_LABELS,
    )


@dashboard_bp.route("/portfolio/retrofix/wheel-assignments", methods=["POST"])
@login_required
def portfolio_retrofix_apply():
    """Apply selected retrofix candidates."""
    from . import writes

    selections = []
    # Form fields are named: select_{i}, account_{i}, ticker_{i}, etc.
    # We iterate through indices until we run out.
    i = 0
    while True:
        if request.form.get(f"select_{i}") is None and \
           request.form.get(f"account_{i}") is None:
            break
        if request.form.get(f"select_{i}") == "1":
            selections.append({
                "account": request.form.get(f"account_{i}", ""),
                "ticker": request.form.get(f"ticker_{i}", ""),
                "strike": request.form.get(f"strike_{i}", "0"),
                "contracts": request.form.get(f"contracts_{i}", "1"),
                "subaccount": request.form.get(f"subaccount_{i}", ""),
                "close_date": request.form.get(f"close_date_{i}", ""),
                "opt_id": request.form.get(f"opt_id_{i}", ""),
            })
        i += 1
        if i > 500:
            break  # safety

    if not selections:
        _flash("No assignments selected — nothing applied.", "info")
        return redirect(url_for("dashboard.portfolio_section", section="settings"))

    result = writes.retrofix_apply(selections)
    msg = f"Applied {result.get('applied', 0)} fix(es)"
    if result.get("skipped"):
        msg += f" · {result['skipped']} skipped"
    _flash(msg, "success" if result.get("applied") else "info")
    return redirect(url_for("dashboard.portfolio_section", section="settings"))


@dashboard_bp.route("/portfolio/backfill-campaign-history", methods=["POST"])
@login_required
def portfolio_backfill_campaigns():
    """Phase 4.5 — Reconstruct campaign event history from the audit log.

    Walks the audit log and rebuilds csp_open / csp_rolled / csp_assigned /
    cc_open / cc_closed / cc_called_away events for every campaign so the
    rollup cards show accurate premium totals and timelines.

    Idempotent — safe to run multiple times.
    """
    from . import writes
    result = writes.backfill_campaign_history()
    if result.get("ok"):
        msg = (f"Backfilled {result.get('campaigns_modified', 0)} campaign(s) · "
               f"+{result.get('events_added', 0)} events · "
               f"+${result.get('premium_recovered', 0):,.2f} in premium history")
        _flash(msg, "success" if result.get("campaigns_modified") else "info")
    else:
        _flash(f"Backfill failed: {result.get('error')}", "error")
    return redirect(url_for("dashboard.portfolio_section", section="settings"))


@dashboard_bp.route("/portfolio/repair-option-data", methods=["POST"])
@login_required
def portfolio_repair_option_data():
    """Phase 4.5 — Patch missing fields on closed/expired/assigned options
    from the audit log. Idempotent."""
    from . import writes
    result = writes.repair_option_data()
    if result.get("ok"):
        msg = (f"Repaired {result.get('options_repaired', 0)} option(s) · "
               f"filled {result.get('fields_filled', 0)} missing field(s)")
        _flash(msg, "success" if result.get("options_repaired") else "info")
    else:
        _flash(f"Repair failed: {result.get('error')}", "error")
    return redirect(url_for("dashboard.portfolio_section", section="settings"))


# ─── PHASE 4.5: EDIT CLOSED POSITION META ────────────────

@dashboard_bp.route("/portfolio/options/edit-closed/<opt_id>", methods=["POST"])
@login_required
def portfolio_option_edit_closed(opt_id):
    """Edit sub-account / note on a closed option (does not touch cash math)."""
    from . import writes
    acct = request.form.get("acct", "brad")
    sub = request.form.get("subaccount")
    note = request.form.get("note")
    result = writes.edit_closed_option_meta(
        acct, opt_id,
        subaccount=sub if sub is not None else None,
        note=note if note is not None else None,
    )
    if result.get("ok"):
        _flash(f"Updated closed option metadata", "success")
    else:
        _flash(f"Edit failed: {result.get('error')}", "error")
    return _bounce("options", acct)


@dashboard_bp.route("/portfolio/spreads/edit-closed/<spr_id>", methods=["POST"])
@login_required
def portfolio_spread_edit_closed(spr_id):
    """Edit sub-account / note on a closed spread (does not touch cash math)."""
    from . import writes
    acct = request.form.get("acct", "brad")
    sub = request.form.get("subaccount")
    note = request.form.get("note")
    result = writes.edit_closed_spread_meta(
        acct, spr_id,
        subaccount=sub if sub is not None else None,
        note=note if note is not None else None,
    )
    if result.get("ok"):
        _flash(f"Updated closed spread metadata", "success")
    else:
        _flash(f"Edit failed: {result.get('error')}", "error")
    return _bounce("spreads", acct)


@dashboard_bp.route("/portfolio/holdings/edit-sold-lot/<lot_id>", methods=["POST"])
@login_required
def portfolio_sold_lot_edit(lot_id):
    """Edit sub-account / note on a sold share lot (does not touch P&L)."""
    from . import writes
    acct = request.form.get("acct", "brad")
    sub = request.form.get("subaccount")
    note = request.form.get("note")
    result = writes.edit_sold_lot_meta(
        acct, lot_id,
        subaccount=sub if sub is not None else None,
        note=note if note is not None else None,
    )
    if result.get("ok"):
        _flash(f"Updated sold lot metadata", "success")
    else:
        _flash(f"Edit failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/settings/subaccount/add", methods=["POST"])
@login_required
def portfolio_sub_add():
    from . import writes
    name = request.form.get("name", "").strip()
    result = writes.add_subaccount(name)
    if result.get("ok"):
        _flash(f"Added sub-account '{name}'", "success")
    else:
        _flash(f"Add failed: {result.get('error')}", "error")
    return _bounce("settings")


@dashboard_bp.route("/portfolio/settings/subaccount/remove", methods=["POST"])
@login_required
def portfolio_sub_remove():
    from . import writes
    name = request.form.get("name", "").strip()
    result = writes.remove_subaccount(name)
    if result.get("ok"):
        _flash(f"Removed sub-account '{name}'", "success")
    else:
        _flash(f"Remove failed: {result.get('error')}", "error")
    return _bounce("settings")


@dashboard_bp.route("/portfolio/partner-host/set", methods=["POST"])
@login_required
def portfolio_partner_host_set():
    """Set the host trading account for a partner (kyleigh, clay).
    Pass empty host to disable exclusion."""
    from . import writes
    partner = (request.form.get("partner") or "").strip().lower()
    host = (request.form.get("host") or "").strip().lower()
    result = writes.set_partner_host(partner, host)
    if result.get("ok"):
        if host:
            _flash(f"{partner.title()}'s capital is now hosted in {host.title()}'s account · their balance will be excluded from {host.title()}'s capital tracking", "success")
        else:
            _flash(f"{partner.title()}'s capital is no longer attributed to any host account", "success")
    else:
        _flash(f"Set host failed: {result.get('error')}", "error")
    # Bounce back to the partner's portfolio cash page
    return redirect(url_for("dashboard.portfolio_section", section="cash") + f"?acct={partner}")


@dashboard_bp.route("/diagnostic", methods=["GET"])
@login_required
def diagnostic():
    return render_page("dashboard/diagnostic.html", page_key="diagnostic")


# ──────────────────────────────────────────────────────────
# Phase 2 — Durability routes
# ──────────────────────────────────────────────────────────

@dashboard_bp.route("/restore", methods=["GET"])
@login_required
def restore():
    """Restore page — list snapshots, recent audit log entries, manual snapshot."""
    from . import durability
    snapshots = durability.list_snapshots()
    audit_entries = durability.list_audit_entries(limit=25)
    status = durability.get_status()
    flash_msg = session.pop("_durability_flash", None)
    flash_kind = session.pop("_durability_flash_kind", "info")
    return render_page(
        "dashboard/restore.html",
        page_key="restore",
        snapshots=snapshots,
        audit_entries=audit_entries,
        durability_status=status,
        flash_msg=flash_msg,
        flash_kind=flash_kind,
    )


@dashboard_bp.route("/restore/<date_iso>", methods=["POST"])
@login_required
def do_restore(date_iso):
    """Perform a restore. Requires confirmation token in form post."""
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_iso or ""):
        session["_durability_flash"] = "Invalid snapshot date format"
        session["_durability_flash_kind"] = "error"
        return redirect(url_for("dashboard.restore"))

    confirm = (request.form.get("confirm") or "").strip().upper()
    if confirm != "RESTORE":
        session["_durability_flash"] = "Restore requires typing RESTORE in the confirmation field"
        session["_durability_flash_kind"] = "error"
        return redirect(url_for("dashboard.restore"))

    from . import durability
    result = durability.restore_from_snapshot(date_iso)

    if result.get("ok"):
        accounts = list((result.get("restored") or {}).keys())
        session["_durability_flash"] = (
            f"Restored from {date_iso} · accounts: {', '.join(accounts) or 'none'}"
        )
        session["_durability_flash_kind"] = "success"
    else:
        session["_durability_flash"] = f"Restore failed: {result.get('error', 'unknown')}"
        session["_durability_flash_kind"] = "error"

    return redirect(url_for("dashboard.restore"))


@dashboard_bp.route("/snapshot/now", methods=["POST"])
@login_required
def snapshot_now():
    """Manually trigger a snapshot."""
    from . import durability
    result = durability.take_snapshot()

    if result.get("ok"):
        session["_durability_flash"] = (
            f"Snapshot saved to {result.get('tab')} · {result.get('rows')} rows"
        )
        session["_durability_flash_kind"] = "success"
    else:
        session["_durability_flash"] = f"Snapshot failed: {result.get('error', 'unknown')}"
        session["_durability_flash_kind"] = "error"

    return redirect(url_for("dashboard.restore"))


# ──────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────

@dashboard_bp.route("/dashboard/health", methods=["GET"])
def health():
    """Used to verify the Blueprint is registered."""
    try:
        from . import durability
        dstatus = durability.get_status()
    except Exception:
        dstatus = {}

    return {
        "status": "ok",
        "module": "omega-dashboard",
        "phase": 4,
        "auth_configured": bool(DASHBOARD_PASSWORD),
        "durability": {
            "sheets_available": dstatus.get("sheets_available", False),
            "portfolio_available": dstatus.get("portfolio_available", False),
            "store_initialized": dstatus.get("store_initialized", False),
            "snapshot_count": dstatus.get("snapshot_count", 0),
            "retention_days": dstatus.get("retention_days"),
        },
    }
