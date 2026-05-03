"""Omega dashboard module.

Self-contained Flask Blueprint. Drop into the bot directory, register on
the existing Flask app:

    from dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp)
    app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "change-me")

Required env vars:
    DASHBOARD_PASSWORD     — single password protecting all dashboard routes
    DASHBOARD_SECRET_KEY   — used to sign session cookies (any random string)

Optional env vars (Phase 2):
    OMEGA_SNAPSHOT_TIME_UTC            — nightly snapshot time HH:MM (default "06:00")
    OMEGA_SNAPSHOT_RETENTION_DAYS      — snapshot retention (default 30)
    OMEGA_SNAPSHOT_SCHEDULER_ENABLED   — "0" disables auto-snapshot (default "1")
"""
from .routes import dashboard_bp
from . import durability  # noqa: F401  (exposed for late-binding writers in phase 4)

__all__ = ["dashboard_bp", "durability"]
