"""Omega dashboard module — phase 1.

Self-contained Flask Blueprint providing auth + nav + visual scaffolding.
Drop into the bot directory, register on the existing Flask app:

    from dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp)
    app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "change-me")

Required env vars:
    DASHBOARD_PASSWORD  — the single password protecting all dashboard routes
    DASHBOARD_SECRET_KEY — used to sign session cookies (any random string)
"""
from .routes import dashboard_bp

__all__ = ["dashboard_bp"]
