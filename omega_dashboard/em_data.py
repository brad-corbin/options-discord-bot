"""EM brief read-side data layer for the Market View dashboard.

# v11.7 (Patch M.4): pure read access to the existing _compute_em_brief_data
# helper in app.py, plus the all-35 refresh job orchestration.
# Never imports private helpers from telegram_commands or the recorder.

Three public functions:
  - get_em_brief(ticker, session=None) -> dict
        Wraps app._compute_em_brief_data with dashboard-specific shape
        (CT-formatted timestamps, partial-brief warning detection).
        Never raises — returns a dict with 'available'=False on any
        failure path.
  - start_refresh_all() -> dict
        Idempotent — if a refresh job is in flight, returns its job_id
        rather than starting a new one. Spawns daemon thread that calls
        _generate_silent_thesis serially for FLOW_TICKERS with
        time.sleep(2.0) between tickers (protects Schwab rate limiter).
  - get_refresh_progress(job_id) -> dict
        Returns counters + slow_caption flag when elapsed > 60s.

Kill switch: EM_BRIEF_DASHBOARD_ENABLED=false → all entry points
return friendly disabled responses.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CHICAGO_TZ = ZoneInfo("America/Chicago")

# Status-key prefix in Redis for in-flight all-35 refresh jobs.
REFRESH_KEY_PREFIX = "em_refresh:"
REFRESH_STATUS_TTL_SEC = 30 * 60  # 30 min — long enough that mid-refresh
                                  # page reloads still see the status,
                                  # short enough that stale jobs expire.

# Daemon throttle (per QC fix #1 in spec) — explicit serialization between
# tickers to protect the Schwab rate limiter during market hours.
INTER_TICKER_SLEEP_SEC = 2.0

# After this elapsed seconds, the UI prepends a "this can take several
# minutes during market hours" caption to the progress text.
SLOW_CAPTION_THRESHOLD_SEC = 60

# Sections the partial-brief detector treats as REQUIRED — if any are
# missing from data["available_sections"], surface a warning banner.
REQUIRED_SECTIONS = {"header", "em_range", "walls", "bias"}


def _kill_switch_off() -> bool:
    """Return True if EM_BRIEF_DASHBOARD_ENABLED is explicitly set to false.
    Defaults to enabled (env unset → not killed)."""
    return os.getenv("EM_BRIEF_DASHBOARD_ENABLED", "true").strip().lower() in ("0", "false", "no")


def get_em_brief(ticker: str, session: Optional[str] = None) -> Dict[str, Any]:
    """Compute the EM brief for a single ticker, shaped for the dashboard.

    Wraps app._compute_em_brief_data. Adds dashboard-specific shape
    (CT-formatted timestamps, partial-brief warning flags). Returns
    a dict the _em_brief_panel.html template renders. NEVER raises —
    returns a dict with 'available'=False + friendly error message on
    any failure (route renders the error state)."""
    if _kill_switch_off():
        return {
            "available": False,
            "error": "EM brief panel disabled (EM_BRIEF_DASHBOARD_ENABLED=false).",
            "ticker": ticker,
        }
    try:
        from app import _compute_em_brief_data
        data = _compute_em_brief_data(ticker, session)
    except Exception as e:
        log.warning(f"em_data.get_em_brief({ticker}): {e}", exc_info=True)
        return {
            "available": False,
            "error": f"Couldn't compute brief for {ticker}: {type(e).__name__}",
            "ticker": ticker,
        }
    if data is None:
        return {
            "available": False,
            "error": f"Couldn't compute brief for {ticker}: no option chain available.",
            "ticker": ticker,
        }
    # Partial-brief detection — drives the warning banner.
    missing = REQUIRED_SECTIONS - set(data.get("available_sections", []))
    partial_warning = None
    if missing:
        partial_warning = (
            f"Partial brief — sections unavailable: {', '.join(sorted(missing))}. "
            f"Underlying data may be incomplete."
        )
    return {
        "available": True,
        "error": None,
        "partial_warning": partial_warning,
        "ticker": ticker,
        "data": data,
        "computed_at_ct": datetime.now(timezone.utc)
            .astimezone(CHICAGO_TZ).strftime("%H:%M:%S CT"),
    }


# ─────────────────────────────────────────────────────────────────────
# All-35 refresh job orchestration
# ─────────────────────────────────────────────────────────────────────

def _redis():
    """Get the app's Redis client. Returns None if Redis is unavailable."""
    try:
        from app import _get_redis
        return _get_redis()
    except Exception as e:
        log.warning(f"em_data._redis(): {e}")
        return None


def _refresh_key(job_id: str) -> str:
    return f"{REFRESH_KEY_PREFIX}{job_id}"


def _existing_inflight_job(rc) -> Optional[str]:
    """Return job_id of an in-flight refresh job, or None.

    A job is "in flight" if its Redis key exists AND has no `finished_at`
    field. This is the idempotency check for concurrent refresh-all
    requests."""
    if rc is None:
        return None
    try:
        for key in rc.scan_iter(match=f"{REFRESH_KEY_PREFIX}*", count=50):
            key_str = key.decode() if isinstance(key, bytes) else key
            data = rc.hgetall(key_str)
            if not data:
                continue
            decoded = {(k.decode() if isinstance(k, bytes) else k):
                       (v.decode() if isinstance(v, bytes) else v)
                       for k, v in data.items()}
            if "finished_at" not in decoded:
                # Strip the prefix to get the job_id.
                return key_str.split(":", 1)[1]
    except Exception as e:
        log.warning(f"em_data._existing_inflight_job: {e}")
    return None


def start_refresh_all() -> Dict[str, Any]:
    """Start an all-35 refresh job. Idempotent — if a job is in flight,
    returns its job_id rather than starting a new one.

    Returns: {job_id, started_now: bool, total: int, error: str (optional)}
    """
    if _kill_switch_off():
        return {"job_id": None, "started_now": False, "total": 0,
                "error": "EM brief dashboard disabled."}
    rc = _redis()
    if rc is None:
        return {"job_id": None, "started_now": False, "total": 0,
                "error": "Redis unavailable."}
    existing = _existing_inflight_job(rc)
    if existing:
        return {"job_id": existing, "started_now": False, "total": 0}

    from oi_flow import FLOW_TICKERS
    tickers = list(FLOW_TICKERS)
    job_id = str(uuid.uuid4())
    key = _refresh_key(job_id)
    started_at = int(time.time() * 1000)

    try:
        rc.hset(key, mapping={
            "started_at": started_at,
            "total": len(tickers),
            "completed": 0,
            "errors": 0,
        })
        rc.expire(key, REFRESH_STATUS_TTL_SEC)
    except Exception as e:
        log.warning(f"em_data.start_refresh_all: redis init failed: {e}")
        return {"job_id": None, "started_now": False, "total": 0,
                "error": str(e)}

    def _run():
        # v11.7 (Patch M.4): refresh daemon. Serialized per-ticker with
        # explicit time.sleep(2.0) between calls — protects the global
        # Schwab rate limiter from competing with the live trading
        # path during market hours (QC fix #1 in spec).
        from app import _generate_silent_thesis
        for i, ticker in enumerate(tickers):
            try:
                _generate_silent_thesis(ticker)
            except Exception as e:
                log.warning(f"em refresh: {ticker} failed: {e}")
                try:
                    rc.hincrby(key, "errors", 1)
                except Exception:
                    pass
            try:
                rc.hincrby(key, "completed", 1)
            except Exception:
                pass
            # Don't sleep after the last ticker.
            if i < len(tickers) - 1:
                time.sleep(INTER_TICKER_SLEEP_SEC)
        try:
            rc.hset(key, "finished_at", int(time.time() * 1000))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name=f"em_refresh_{job_id[:8]}").start()
    return {"job_id": job_id, "started_now": True, "total": len(tickers)}


def get_refresh_progress(job_id: str) -> Dict[str, Any]:
    """Return the current status of a refresh job.

    Shape: {found: bool, started_at, total, completed, errors,
            finished_at, elapsed_seconds, slow_caption: bool, error?}
    """
    rc = _redis()
    if rc is None:
        return {"found": False, "error": "Redis unavailable."}
    try:
        raw = rc.hgetall(_refresh_key(job_id))
    except Exception as e:
        return {"found": False, "error": str(e)}
    if not raw:
        return {"found": False}
    decoded = {(k.decode() if isinstance(k, bytes) else k):
               (v.decode() if isinstance(v, bytes) else v)
               for k, v in raw.items()}
    started_at = int(decoded.get("started_at", 0))
    finished_at = decoded.get("finished_at")
    finished_at_int = int(finished_at) if finished_at else None
    now_ms = int(time.time() * 1000)
    elapsed_seconds = (now_ms - started_at) // 1000 if started_at else 0
    return {
        "found": True,
        "started_at": started_at,
        "total": int(decoded.get("total", 0)),
        "completed": int(decoded.get("completed", 0)),
        "errors": int(decoded.get("errors", 0)),
        "finished_at": finished_at_int,
        "elapsed_seconds": elapsed_seconds,
        "slow_caption": elapsed_seconds > SLOW_CAPTION_THRESHOLD_SEC and finished_at_int is None,
    }
