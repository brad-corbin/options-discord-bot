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
    {"key": "mine",      "label": "Mine",        "color": "mine"},
    {"key": "mom",       "label": "Mom",         "color": "mom"},
    {"key": "partner",   "label": "Partnership", "color": "partner"},
    {"key": "kyleigh",   "label": "Kyleigh",     "color": "kyleigh"},
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
    from . import data
    active_account = get_active_account()
    # Pulled tickers: comma-separated cookie, e.g. "AMD,SOFI"
    pulled_raw = request.cookies.get("omega_pulled", "").strip()
    pulled = [t.strip().upper() for t in pulled_raw.split(",") if t.strip()] if pulled_raw else []
    page_data = data.trading_data(active_account["key"], pulled_tickers=pulled)
    return render_page(
        "dashboard/trading.html",
        page_key="trading",
        page_data=page_data,
    )


@dashboard_bp.route("/trading/pull/<ticker>", methods=["POST"])
@login_required
def pull_ticker(ticker):
    """Add a ticker to the pulled set (cookie-persisted for the session)."""
    ticker = (ticker or "").strip().upper()
    import re as _re
    if not _re.match(r"^[A-Z][A-Z0-9.\-]{0,10}$", ticker):
        return redirect(url_for("dashboard.trading"))

    pulled_raw = request.cookies.get("omega_pulled", "").strip()
    pulled = [t.strip().upper() for t in pulled_raw.split(",") if t.strip()] if pulled_raw else []
    # Newest first, dedup
    pulled = [ticker] + [t for t in pulled if t != ticker]
    pulled = pulled[:12]  # cap

    resp = make_response(redirect(url_for("dashboard.trading")))
    resp.set_cookie("omega_pulled", ",".join(pulled),
                    max_age=60 * 60 * 24, samesite="Lax")
    return resp


@dashboard_bp.route("/trading/dismiss/<ticker>", methods=["POST"])
@login_required
def dismiss_pulled(ticker):
    """Remove a single ticker from the pulled set."""
    ticker = (ticker or "").strip().upper()
    pulled_raw = request.cookies.get("omega_pulled", "").strip()
    pulled = [t.strip().upper() for t in pulled_raw.split(",") if t.strip()] if pulled_raw else []
    pulled = [t for t in pulled if t != ticker]

    resp = make_response(redirect(url_for("dashboard.trading")))
    resp.set_cookie("omega_pulled", ",".join(pulled),
                    max_age=60 * 60 * 24, samesite="Lax")
    return resp


@dashboard_bp.route("/trading/clear-pulls", methods=["POST"])
@login_required
def clear_pulls():
    resp = make_response(redirect(url_for("dashboard.trading")))
    resp.set_cookie("omega_pulled", "", max_age=0, samesite="Lax")
    return resp


@dashboard_bp.route("/portfolio", methods=["GET"])
@login_required
def portfolio():
    return render_page("dashboard/portfolio.html", page_key="portfolio")


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
        "phase": 3,
        "auth_configured": bool(DASHBOARD_PASSWORD),
        "durability": {
            "sheets_available": dstatus.get("sheets_available", False),
            "portfolio_available": dstatus.get("portfolio_available", False),
            "store_initialized": dstatus.get("store_initialized", False),
            "snapshot_count": dstatus.get("snapshot_count", 0),
            "retention_days": dstatus.get("retention_days"),
        },
    }
