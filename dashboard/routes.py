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
    """Apply session lifetime once per app request."""
    session.permanent = True
    # Set the lifetime on the app config the first time we're called.
    from flask import current_app
    if "_omega_session_set" not in current_app.config:
        current_app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)
        current_app.config["_omega_session_set"] = True


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
    return render_page("dashboard/command_center.html", page_key="dashboard")


@dashboard_bp.route("/trading", methods=["GET"])
@login_required
def trading():
    return render_page("dashboard/trading.html", page_key="trading")


@dashboard_bp.route("/portfolio", methods=["GET"])
@login_required
def portfolio():
    return render_page("dashboard/portfolio.html", page_key="portfolio")


@dashboard_bp.route("/diagnostic", methods=["GET"])
@login_required
def diagnostic():
    return render_page("dashboard/diagnostic.html", page_key="diagnostic")


# ──────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────

@dashboard_bp.route("/dashboard/health", methods=["GET"])
def health():
    """Used to verify the Blueprint is registered."""
    return {
        "status": "ok",
        "module": "omega-dashboard",
        "phase": 1,
        "auth_configured": bool(DASHBOARD_PASSWORD),
    }
