"""Dashboard Blueprint routes.

Phase 1 scope:
  - Auth: /login + /logout, single-password, Flask session, 30-day persistence
  - Nav: header with logo, page tabs, account switcher
  - Pages: /dashboard, /trading, /portfolio, /diagnostic — placeholder content
  - Account context: cookie-persisted across navigation

No data layer yet. No Redis/Sheets reads. No writes. Just the framework.

────────────────────────────────────────────────────────────────────
Patches applied:
  legacy-v1: Visual rebrand to The Legacy Desk
    - PAGE_TABS: rename Command → Desk, Trading → Market View
    - load_family_roster() loads data/family_roster.yaml on demand
    - render_page() now passes family_roster context to all templates

  legacy-v1.1: Sync Portfolio account picker to top-bar selection
    - portfolio_section() defaults active_underlying from top-bar cookie
      when no ?acct= is supplied, instead of hardcoding "brad"
    - set_account() strips stale ?acct= from referer URL on redirect, so
      clicking a top-bar chip while on Portfolio actually re-defaults
────────────────────────────────────────────────────────────────────
"""
import os
import logging
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, make_response, flash, jsonify
)

# legacy-v1: YAML for family roster. Optional — falls back to empty list if
# either pyyaml or the file is missing, so The Family panel just doesn't render.
try:
    import yaml as _yaml  # noqa: F401
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    _yaml = None

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
# legacy-v1: renamed Command → Desk, Trading → Market View (endpoints unchanged)
# v11.7 (Patch H.1): Alerts tab between Market View and Portfolio.
PAGE_TABS = [
    {"key": "dashboard",  "label": "Desk",         "endpoint": "dashboard.command_center"},
    {"key": "trading",    "label": "Market View",  "endpoint": "dashboard.trading"},
    {"key": "alerts",     "label": "Alerts",       "endpoint": "dashboard.alerts"},
    {"key": "portfolio",  "label": "Portfolio",    "endpoint": "dashboard.portfolio"},
    {"key": "research",   "label": "Research",     "endpoint": "dashboard.research"},
    {"key": "restore",    "label": "Durability",   "endpoint": "dashboard.restore"},
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


# legacy-v1: family roster loader
_FAMILY_ROSTER_PATH = Path(__file__).parent / "data" / "family_roster.yaml"
_FAMILY_ROSTER_CACHE = {"mtime": None, "data": None}

def load_family_roster():
    """Load the family roster from data/family_roster.yaml.

    Cached by file mtime so editing the YAML picks up on the next request
    without a restart. Any failure (missing pyyaml, missing file, parse error)
    is logged and returns [] — The Family panel will just not render.
    """
    if not _HAS_YAML:
        return []
    try:
        if not _FAMILY_ROSTER_PATH.exists():
            return []
        mtime = _FAMILY_ROSTER_PATH.stat().st_mtime
        if _FAMILY_ROSTER_CACHE["mtime"] == mtime and _FAMILY_ROSTER_CACHE["data"] is not None:
            return _FAMILY_ROSTER_CACHE["data"]
        with open(_FAMILY_ROSTER_PATH, "r", encoding="utf-8") as f:
            doc = _yaml.safe_load(f) or {}
        roster = doc.get("family", []) or []
        # Normalize: ensure each entry is a dict with at least name + relation
        clean = []
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            if not entry.get("name") or not entry.get("relation"):
                continue
            clean.append({
                "name":     str(entry["name"]),
                "relation": str(entry["relation"]),
                "nickname": str(entry["nickname"]) if entry.get("nickname") else None,
            })
        _FAMILY_ROSTER_CACHE["mtime"] = mtime
        _FAMILY_ROSTER_CACHE["data"]  = clean
        return clean
    except Exception:
        log.warning("legacy-v1: failed to load family_roster.yaml", exc_info=True)
        return []


def render_page(template_name, page_key, **context):
    """Centralized render with all the bits every page needs."""
    active_account = get_active_account()
    return render_template(
        template_name,
        active_page=page_key,
        active_account=active_account,
        accounts=ACCOUNTS,
        page_tabs=PAGE_TABS,
        family_roster=load_family_roster(),  # legacy-v1
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

    # legacy-v1.1: strip stale ?acct= from referer URL if present, so the
    # newly-set cookie drives the Portfolio default rather than the URL param
    # winning. Other query params (show_closed, since, ticker) are preserved.
    try:
        from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
        parsed = urlparse(referer)
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            if "acct" in qs:
                qs.pop("acct", None)
                referer = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        log.debug("legacy-v1.1: failed to strip ?acct= from referer", exc_info=True)

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
    """Trading tab — live row table. Initial render carries the first
    snapshot so the page is useful on first paint; the JS layer then
    polls /trading/data every 5s for updates."""
    from . import data
    active_account = get_active_account()
    page_data = data.trading_data(active_account["key"])
    return render_page(
        "dashboard/trading.html",
        page_key="trading",
        page_data=page_data,
    )

@dashboard_bp.route("/trading/data", methods=["GET"])
@login_required
def trading_data_json():
    """JSON-only feed for the Trading tab's polling JS. Same payload as
    /trading's initial render but as application/json."""
    from . import data
    from flask import jsonify
    active_account = get_active_account()
    payload = data.trading_data(active_account["key"])
    resp = jsonify(payload)
    # No caching at the HTTP layer — the data layer caches for 1s, which
    # is the right place. Browsers and CDNs should always re-fetch.
    resp.headers["Cache-Control"] = "no-store"
    return resp


# v11.7 (Patch H.3): alerts feed routes. Read-only against the recorder
# DB at /var/backtest/desk.db; mirrors the /trading + /trading/data
# pattern above. Three login-required routes:
#   /alerts                 — page render + initial payload
#   /alerts/data            — JSON payload for the 10s polling loop
#   /alerts/<alert_id>      — detail page (UUID v4 enforced inside)
#
# alerts_data.py owns its own read-only sqlite3 connection (mode=ro
# URI form). It does NOT import alert_recorder write internals — the
# read/write boundary is intentional so future recorder refactors don't
# break the dashboard.

@dashboard_bp.route("/alerts", methods=["GET"])
@login_required
def alerts():
    from . import alerts_data
    page_data = alerts_data.list_alerts()
    return render_page(
        "dashboard/alerts.html",
        page_key="alerts",
        page_data=page_data,
    )


@dashboard_bp.route("/alerts/data", methods=["GET"])
@login_required
def alerts_data_json():
    from . import alerts_data
    payload = alerts_data.list_alerts()
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard_bp.route("/alerts/<string:alert_id>", methods=["GET"])
@login_required
def alerts_detail(alert_id):
    from . import alerts_data
    detail = alerts_data.get_alert_detail(alert_id)
    if detail is None:
        # 404 with friendly empty-state, not raw Flask 404. The UUID v4
        # check inside get_alert_detail also returns None for malformed
        # ids — same friendly path.
        return render_page(
            "dashboard/alerts_detail.html",
            page_key="alerts",
            page_data={"available": False,
                       "error": f"Alert not found: {alert_id}",
                       "detail": None},
        ), 404
    return render_page(
        "dashboard/alerts_detail.html",
        page_key="alerts",
        page_data={"available": True, "error": None, "detail": detail},
    )


# v11.7 (Patch M.5): EM brief routes for Market View. Three routes:
#   GET  /em/brief/<ticker>          — synchronous, returns JSON for the panel
#   POST /em/refresh                 — starts the all-35 refresh job
#   GET  /em/refresh/status/<job_id> — progress poll (every 2s from JS)
#
# All login-required. All read-only against the existing trading path
# except /em/refresh which writes ThesisContext via _generate_silent_thesis
# (same write path the periodic loop uses). EM_BRIEF_DASHBOARD_ENABLED
# kill switch handled inside em_data — routes return 410 when disabled.

import re as _re

_TICKER_RE = _re.compile(r"^[A-Z]{1,8}$")


@dashboard_bp.route("/em/brief/<string:ticker>", methods=["GET"])
@login_required
def em_brief(ticker):
    from . import em_data
    ticker_upper = (ticker or "").upper().strip()
    if not _TICKER_RE.match(ticker_upper):
        return jsonify({"available": False,
                        "error": f"Invalid ticker: {ticker}",
                        "ticker": ticker}), 400
    payload = em_data.get_em_brief(ticker_upper)
    if payload.get("error") and "disabled" in (payload.get("error") or ""):
        return jsonify(payload), 410
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard_bp.route("/em/refresh", methods=["POST"])
@login_required
def em_refresh():
    from . import em_data
    payload = em_data.start_refresh_all()
    if payload.get("error") and "disabled" in (payload.get("error") or ""):
        return jsonify(payload), 410
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard_bp.route("/em/refresh/status/<string:job_id>", methods=["GET"])
@login_required
def em_refresh_status(job_id):
    from . import em_data
    # Reject anything that isn't a UUID-shaped string before hitting Redis.
    if not _re.match(r"^[0-9a-f-]{8,40}$", job_id, _re.IGNORECASE):
        return jsonify({"found": False, "error": "Invalid job_id"}), 400
    payload = em_data.get_refresh_progress(job_id)
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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

    # Active underlying account for entry — resolved from query param,
    # then top-bar cookie (legacy-v1.1), then "brad" default.
    from . import writes
    active_underlying = (request.args.get("acct") or "").strip().lower()
    if active_underlying not in writes.ALL_UNDERLYING_ACCOUNTS:
        # legacy-v1.1: take the top-bar selection as default before falling
        # back to brad. Top-bar uses keys (combined/mine/mom/partner/kyleigh/clay);
        # Portfolio entry uses underlying keys (brad/mom/partner/kyleigh/clay).
        # combined and mine both collapse to brad since Combined isn't a valid
        # entry account.
        _TOP_TO_UNDERLYING = {
            "combined": "brad",
            "mine":     "brad",
            "mom":      "mom",
            "partner":  "partner",
            "kyleigh":  "kyleigh",
            "clay":     "clay",
        }
        try:
            top_key = (get_active_account() or {}).get("key", "combined")
        except Exception:
            top_key = "combined"
        active_underlying = _TOP_TO_UNDERLYING.get(top_key, "brad")
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
    funded = request.form.get("funded_from_cash") == "1"
    cb_raw = (request.form.get("cost_basis") or "").strip()
    cost_basis = cb_raw if cb_raw else None
    result = writes.add_lumpsum(
        account=acct,
        label=request.form.get("label"),
        value=request.form.get("value"),
        subaccount=request.form.get("subaccount"),
        as_of=request.form.get("as_of"),
        note=request.form.get("note"),
        funded_from_cash=funded,
        cost_basis=cost_basis,
    )
    if result.get("ok"):
        e = result["entry"]
        msg = f"Added lump-sum '{e['label']}' = ${e['value']:,.2f}"
        if funded:
            cb_used = e.get("cost_basis") or e["value"]
            msg += f" · cash debited ${cb_used:,.2f}"
            if abs(cb_used - e["value"]) > 0.01:
                gain = e["value"] - cb_used
                msg += f" (unrealized gain ${gain:,.2f})"
        _flash(msg, "success")
    else:
        _flash(f"Add failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/lumpsum/update/<entry_id>", methods=["POST"])
@login_required
def portfolio_lumpsum_update(entry_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    cash_impact = request.form.get("cash_impact") or "market"
    result = writes.update_lumpsum(
        account=acct,
        entry_id=entry_id,
        value=request.form.get("value") or None,
        as_of=request.form.get("as_of") or None,
        label=request.form.get("label") or None,
        note=request.form.get("note") or None,
        cash_impact=cash_impact,
    )
    if result.get("ok"):
        msg = f"Updated '{result['entry']['label']}'"
        if cash_impact == "buy":
            msg += " · cash debited"
        elif cash_impact == "sell":
            msg += " · cash credited"
        _flash(msg, "success")
    else:
        _flash(f"Update failed: {result.get('error')}", "error")
    return _bounce("holdings", acct)


@dashboard_bp.route("/portfolio/lumpsum/delete/<entry_id>", methods=["POST"])
@login_required
def portfolio_lumpsum_delete(entry_id):
    from . import writes
    acct = request.form.get("acct", "brad")
    credit_cash = request.form.get("credit_cash") == "1"
    result = writes.delete_lumpsum(acct, entry_id, credit_cash=credit_cash)
    if result.get("ok"):
        msg = "Lump-sum deleted"
        if credit_cash:
            msg += " · cash credited"
        _flash(msg, "success")
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
    sub = request.form.get("subaccount") or None
    result = writes.delete_holding(acct, ticker, also_delete_cash=also_cash, subaccount=sub)
    if result.get("ok"):
        msg = f"{ticker.upper()} removed"
        if sub:
            msg += f" from {sub}"
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
        per_acc = result.get("per_account", {}) or {}
        if per_acc:
            parts = []
            for acc, info in per_acc.items():
                bit = f"{acc}: {info['campaigns']} camp"
                if info.get("discovered"):
                    bit += f" ({info['discovered']} new)"
                parts.append(bit)
            breakdown = " · ".join(parts)
            msg = (f"Backfill complete · {breakdown} · +${result.get('premium_recovered', 0):,.2f} premium history")
        else:
            msg = "Backfill complete — nothing to update (all campaigns already up-to-date)"
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


# ─── PHASE 4.5+: CSV EXPORTS ────────────────
# So Brad can pull down the actual stored data for reconciliation against
# his Schwab statements and spreadsheets.

def _csv_response(filename: str, rows: list) -> "Response":
    """Build a CSV download response. rows = list of lists (first row = header)."""
    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    for r in rows:
        writer.writerow(r)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@dashboard_bp.route("/portfolio/export/cash/<account>.csv", methods=["GET"])
@login_required
def portfolio_export_cash(account):
    """Export cash ledger for an account as CSV."""
    from . import writes
    if account not in ("brad", "mom", "partner", "kyleigh", "clay"):
        return "Invalid account", 400
    ledger = writes.get_cash_ledger(account)
    rows = [["date", "type", "amount", "subaccount", "note", "ref_id", "ref_type", "id"]]
    for ev in sorted(ledger, key=lambda e: (e.get("date", ""), e.get("id", ""))):
        if not isinstance(ev, dict):
            continue
        rows.append([
            ev.get("date", ""),
            ev.get("type", ""),
            f"{float(ev.get('amount') or 0):.2f}",
            ev.get("subaccount", ""),
            ev.get("note", ""),
            ev.get("ref_id", ""),
            ev.get("ref_type", ""),
            ev.get("id", ""),
        ])
    from datetime import date as _date
    return _csv_response(
        f"omega_cash_ledger_{account}_{_date.today().isoformat()}.csv", rows
    )


@dashboard_bp.route("/portfolio/export/options/<account>.csv", methods=["GET"])
@login_required
def portfolio_export_options(account):
    """Export all options (open + closed) for an account as CSV."""
    from . import writes
    if account not in ("brad", "mom", "partner", "kyleigh", "clay"):
        return "Invalid account", 400
    options = writes.get_options(account)
    rows = [["id", "ticker", "type", "direction", "strike", "exp", "premium",
             "contracts", "subaccount", "category", "tag", "open_date",
             "status", "close_date", "close_premium", "note"]]
    for o in sorted(options, key=lambda x: (x.get("open_date", ""), x.get("id", ""))):
        if not isinstance(o, dict):
            continue
        rows.append([
            o.get("id", ""),
            o.get("ticker", ""),
            o.get("type", ""),
            o.get("direction", ""),
            o.get("strike", ""),
            o.get("exp", ""),
            o.get("premium", ""),
            o.get("contracts", ""),
            o.get("subaccount", ""),
            o.get("category", ""),
            o.get("tag", ""),
            o.get("open_date", ""),
            o.get("status", ""),
            o.get("close_date", ""),
            o.get("close_premium", ""),
            o.get("note", ""),
        ])
    from datetime import date as _date
    return _csv_response(
        f"omega_options_{account}_{_date.today().isoformat()}.csv", rows
    )


@dashboard_bp.route("/portfolio/export/holdings/<account>.csv", methods=["GET"])
@login_required
def portfolio_export_holdings(account):
    """Export share holdings for an account as CSV."""
    from . import writes
    if account not in ("brad", "mom", "partner", "kyleigh", "clay"):
        return "Invalid account", 400
    holdings = writes.get_holdings(account)
    rows = [["ticker", "shares", "cost_basis", "lot_value", "subaccount",
             "tag", "first_added", "last_updated"]]
    for ticker, h in sorted(holdings.items()):
        if not isinstance(h, dict):
            continue
        shares = float(h.get("shares") or 0)
        cb = float(h.get("cost_basis") or 0)
        rows.append([
            ticker,
            shares,
            f"{cb:.4f}",
            f"{shares * cb:.2f}",
            h.get("subaccount", ""),
            h.get("tag", ""),
            h.get("first_added", ""),
            h.get("last_updated", ""),
        ])
    from datetime import date as _date
    return _csv_response(
        f"omega_holdings_{account}_{_date.today().isoformat()}.csv", rows
    )


@dashboard_bp.route("/portfolio/export/spreads/<account>.csv", methods=["GET"])
@login_required
def portfolio_export_spreads(account):
    """Export spreads for an account as CSV."""
    from . import writes
    if account not in ("brad", "mom", "partner", "kyleigh", "clay"):
        return "Invalid account", 400
    spreads = writes.get_spreads(account)
    rows = [["id", "ticker", "spread_type", "long_strike", "short_strike",
             "exp", "net", "contracts", "is_credit", "subaccount", "tag",
             "open_date", "status", "close_date", "close_value", "note"]]
    for s in sorted(spreads, key=lambda x: (x.get("open_date", ""), x.get("id", ""))):
        if not isinstance(s, dict):
            continue
        rows.append([
            s.get("id", ""),
            s.get("ticker", ""),
            s.get("spread_type", ""),
            s.get("long_strike", ""),
            s.get("short_strike", ""),
            s.get("exp", ""),
            s.get("net", ""),
            s.get("contracts", ""),
            s.get("is_credit", ""),
            s.get("subaccount", ""),
            s.get("tag", ""),
            s.get("open_date", ""),
            s.get("status", ""),
            s.get("close_date", ""),
            s.get("close_value", ""),
            s.get("note", ""),
        ])
    from datetime import date as _date
    return _csv_response(
        f"omega_spreads_{account}_{_date.today().isoformat()}.csv", rows
    )


@dashboard_bp.route("/portfolio/export/all/<account>.zip", methods=["GET"])
@login_required
def portfolio_export_all(account):
    """Export everything for an account as a single ZIP."""
    import io, zipfile, csv
    from datetime import date as _date
    from . import writes
    if account not in ("brad", "mom", "partner", "kyleigh", "clay"):
        return "Invalid account", 400

    def _write_csv(rows: list) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        for r in rows:
            w.writerow(r)
        return buf.getvalue().encode("utf-8")

    today = _date.today().isoformat()
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Cash ledger
        ledger = writes.get_cash_ledger(account)
        rows = [["date", "type", "amount", "subaccount", "note", "ref_id", "ref_type", "id"]]
        for ev in sorted(ledger, key=lambda e: (e.get("date", ""), e.get("id", ""))):
            if isinstance(ev, dict):
                rows.append([
                    ev.get("date", ""), ev.get("type", ""),
                    f"{float(ev.get('amount') or 0):.2f}",
                    ev.get("subaccount", ""), ev.get("note", ""),
                    ev.get("ref_id", ""), ev.get("ref_type", ""),
                    ev.get("id", ""),
                ])
        zf.writestr(f"cash_ledger_{account}.csv", _write_csv(rows))

        # Options
        options = writes.get_options(account)
        rows = [["id", "ticker", "type", "direction", "strike", "exp", "premium",
                 "contracts", "subaccount", "category", "tag", "open_date",
                 "status", "close_date", "close_premium", "note"]]
        for o in sorted(options, key=lambda x: (x.get("open_date", ""), x.get("id", ""))):
            if isinstance(o, dict):
                rows.append([
                    o.get("id", ""), o.get("ticker", ""), o.get("type", ""),
                    o.get("direction", ""), o.get("strike", ""), o.get("exp", ""),
                    o.get("premium", ""), o.get("contracts", ""),
                    o.get("subaccount", ""), o.get("category", ""), o.get("tag", ""),
                    o.get("open_date", ""), o.get("status", ""),
                    o.get("close_date", ""), o.get("close_premium", ""),
                    o.get("note", ""),
                ])
        zf.writestr(f"options_{account}.csv", _write_csv(rows))

        # Holdings
        holdings = writes.get_holdings(account)
        rows = [["ticker", "shares", "cost_basis", "lot_value", "subaccount",
                 "tag", "first_added", "last_updated"]]
        for ticker, h in sorted(holdings.items()):
            if isinstance(h, dict):
                shares = float(h.get("shares") or 0)
                cb = float(h.get("cost_basis") or 0)
                rows.append([
                    ticker, shares, f"{cb:.4f}", f"{shares * cb:.2f}",
                    h.get("subaccount", ""), h.get("tag", ""),
                    h.get("first_added", ""), h.get("last_updated", ""),
                ])
        zf.writestr(f"holdings_{account}.csv", _write_csv(rows))

        # Spreads
        spreads = writes.get_spreads(account)
        rows = [["id", "ticker", "spread_type", "long_strike", "short_strike",
                 "exp", "net", "contracts", "is_credit", "subaccount", "tag",
                 "open_date", "status", "close_date", "close_value", "note"]]
        for s in sorted(spreads, key=lambda x: (x.get("open_date", ""), x.get("id", ""))):
            if isinstance(s, dict):
                rows.append([
                    s.get("id", ""), s.get("ticker", ""), s.get("spread_type", ""),
                    s.get("long_strike", ""), s.get("short_strike", ""),
                    s.get("exp", ""), s.get("net", ""), s.get("contracts", ""),
                    s.get("is_credit", ""), s.get("subaccount", ""), s.get("tag", ""),
                    s.get("open_date", ""), s.get("status", ""),
                    s.get("close_date", ""), s.get("close_value", ""),
                    s.get("note", ""),
                ])
        zf.writestr(f"spreads_{account}.csv", _write_csv(rows))

        # Active campaigns (wheels) summary
        try:
            from . import campaigns as _campaigns
            camps = _campaigns.get_campaigns(account)
            rows = [["ticker", "subaccount", "status", "opened_at", "closed_at",
                     "total_premium", "shares_held", "weighted_cost_basis",
                     "effective_basis", "csp_open_count", "cc_open_count",
                     "duration_days", "events_count"]]
            for c in camps:
                rollup = c.get("rollup") or {}
                rows.append([
                    c.get("ticker", ""), c.get("subaccount", ""),
                    c.get("status", ""), c.get("opened_at", ""),
                    c.get("closed_at", ""),
                    rollup.get("total_premium", 0),
                    rollup.get("shares_held", 0),
                    rollup.get("weighted_cost_basis", 0),
                    rollup.get("effective_basis", 0),
                    rollup.get("csp_open_count", 0),
                    rollup.get("cc_open_count", 0),
                    rollup.get("duration_days", 0),
                    len(c.get("events", []) or []),
                ])
            zf.writestr(f"campaigns_{account}.csv", _write_csv(rows))
        except Exception as e:
            log.warning(f"campaigns export skipped: {e}")

    zip_buf.seek(0)
    resp = make_response(zip_buf.getvalue())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="omega_{account}_full_{today}.zip"'
    )
    return resp


# ─── PHASE 4.5: EDIT CLOSED POSITION META ────────────────

@dashboard_bp.route("/portfolio/options/edit-closed/<opt_id>", methods=["POST"])
@login_required
def portfolio_option_edit_closed(opt_id):
    """Edit metadata on a closed option. Supports sub-account, note, and
    (Phase 4.5+) premium / contracts / close_premium with cash-event reconciliation."""
    from . import writes
    acct = request.form.get("acct", "brad")
    sub = request.form.get("subaccount")
    note = request.form.get("note")
    premium = request.form.get("premium")
    contracts = request.form.get("contracts")
    close_premium = request.form.get("close_premium")
    result = writes.edit_closed_option_meta(
        acct, opt_id,
        subaccount=sub if sub is not None else None,
        note=note if note is not None else None,
        premium=premium if premium not in (None, "") else None,
        contracts=contracts if contracts not in (None, "") else None,
        close_premium=close_premium if close_premium not in (None, "") else None,
    )
    if result.get("ok"):
        msg = "Updated closed option"
        if result.get("cash_adjusted"):
            msg += " (cash events adjusted to match new values)"
        _flash(msg, "success")
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


@dashboard_bp.route("/research", methods=["GET"])
@login_required
def research():
    """Research tab — rebuild progress + per-ticker BotState (Patch 11.3).

    Shows what's been migrated to the canonical rebuild and the live state
    of each ticker through the new compute path. As canonical functions
    land in subsequent patches, more fields go from 'pending' to lit
    values automatically.
    """
    try:
        from . import research_data as rd
    except ImportError as e:
        log.warning("research_data module not available: %s", e)
        return render_page(
            "dashboard/research.html",
            page_key="research",
            page_data=_empty_research_payload(str(e)),
        )

    data_router = _get_bot_data_router()
    # Patch C: pass the Redis client so the consumer path is reachable.
    # When RESEARCH_USE_REDIS env var is off, redis_client is ignored.
    from app import _get_redis
    payload = rd.research_data(
        data_router=data_router,
        redis_client=_get_redis(),
    )
    return render_page(
        "dashboard/research.html",
        page_key="research",
        page_data=payload,
    )


@dashboard_bp.route("/research/data", methods=["GET"])
@login_required
def research_data_json():
    """JSON feed for any future polling JS on the Research page.

    Right now the page server-renders on each visit (60s in-memory cache
    in research_data.py keeps Schwab cost flat).
    """
    from flask import jsonify
    try:
        from . import research_data as rd
    except ImportError:
        return jsonify({"available": False, "error": "research_data unavailable"}), 503

    data_router = _get_bot_data_router()
    # Patch C: pass the Redis client so the consumer path is reachable.
    # When RESEARCH_USE_REDIS env var is off, redis_client is ignored.
    from app import _get_redis
    payload = rd.research_data(
        data_router=data_router,
        redis_client=_get_redis(),
    )
    from dataclasses import asdict, is_dataclass
    body = asdict(payload) if is_dataclass(payload) else payload
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Backward-compat: old /diagnostic URL → /research.
# Remove after a release or two once external bookmarks have updated.
@dashboard_bp.route("/diagnostic", methods=["GET"])
@login_required
def diagnostic_redirect():
    return redirect(url_for("dashboard.research"))


def _get_bot_data_router():
    """Locate the bot's DataRouter instance.

    The bot exposes it as `_cached_md` in app.py, set up by
    `build_data_router()`. We import lazily to avoid module-load
    circular dependencies. Returns None if not available — caller
    renders a graceful 'unavailable' state.
    """
    try:
        import app
        return getattr(app, "_cached_md", None)
    except Exception as e:
        log.warning("Could not locate bot data_router: %s", e)
        return None


def _empty_research_payload(error_msg: str):
    """Fallback payload when research_data module is unavailable."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    return SimpleNamespace(
        fetched_at_utc=datetime.now(timezone.utc),
        tickers_total=0,
        tickers_with_data=0,
        tickers_errored=0,
        fields_lit_avg=0.0,
        fields_total=0,
        canonical_status_summary={},
        snapshots=[],
        available=False,
        error=error_msg,
    )


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


# ─── S.4: TICKER REGISTRATION ───────────────────────────────
@dashboard_bp.route("/api/register-tickers", methods=["POST"])
@login_required
def api_register_tickers():
    """Register a set of tickers for streaming spot subscription.

    Called by the dashboard pages on load, BEFORE the first /api/spot-prices
    request. Hands the ticker list to schwab_stream's add_equity_symbols
    so the WebSocket sub catches up by the time the user's spot-prices
    poll arrives. Idempotent — repeated calls with the same tickers are
    silently de-duped inside add_equity_symbols.

    Body: {"tickers": ["AAPL", "MSFT", ...]}
    Returns: {"registered": N, "active": <streaming sub count>}

    No-ops cleanly when DASHBOARD_SPOT_USE_STREAMING is off — the streamer
    still subscribes; the dashboard just won't read from it.
    """
    from flask import jsonify, request
    body = request.get_json(silent=True) or {}
    raw = body.get("tickers") or []
    tickers = [str(t).strip().upper() for t in raw if t and str(t).strip()]
    if not tickers:
        return jsonify({"registered": 0, "active": 0})

    try:
        from schwab_stream import _stream_manager
        if _stream_manager is None:
            log.debug("register-tickers: stream manager not running; skipping")
            return jsonify({"registered": 0, "active": 0, "note": "stream offline"})
        _stream_manager.add_equity_symbols(tickers)
        active = _stream_manager.status.get("equity_symbols_subscribed", 0)
        return jsonify({"registered": len(tickers), "active": active})
    except Exception as e:
        log.warning(f"register-tickers failed: {e}")
        return jsonify({"registered": 0, "active": 0, "error": str(e)}), 200


# ─── PHASE 4.5+ — LIVE SPOT PRICES ─────────────────────────
@dashboard_bp.route("/api/spot-prices", methods=["GET"])
@login_required
def api_spot_prices():
    """Return current spot prices for the requested tickers.

    GET /api/spot-prices?tickers=AAPL,MSFT,SPY

    Cached server-side for 60 seconds. Returns whatever it can fetch; tickers
    that error are simply omitted from the response (the frontend then shows
    "—" for them, no error noise).
    """
    from . import spot_prices
    raw = (request.args.get("tickers") or "").strip()
    if not raw:
        return jsonify({"prices": {}})
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    prices = spot_prices.get_spot_prices(tickers)
    return jsonify({"prices": prices, "count": len(prices)})