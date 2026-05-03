"""Phase 2 — Background scheduler for nightly snapshots.

Spawns a daemon thread on first request that wakes once per day at the
configured UTC time and fires take_snapshot(). Disabled via env var if
the user prefers external cron control.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Default 06:00 UTC = 1am Central. After market close, before next-day prep.
SNAPSHOT_TIME_UTC = os.getenv("OMEGA_SNAPSHOT_TIME_UTC", "06:00").strip()
SCHEDULER_ENABLED = os.getenv(
    "OMEGA_SNAPSHOT_SCHEDULER_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_thread_started = False
_thread_lock = threading.Lock()


def _parse_hhmm(s: str) -> tuple:
    try:
        h, m = s.split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return 6, 0


def _seconds_until_next(target_h: int, target_m: int) -> int:
    """Seconds until the next occurrence of HH:MM UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, int((target - now).total_seconds()))


def _scheduler_loop():
    target_h, target_m = _parse_hhmm(SNAPSHOT_TIME_UTC)
    log.info(f"Omega snapshot scheduler online (target {target_h:02d}:{target_m:02d} UTC daily)")

    while True:
        try:
            sleep_sec = _seconds_until_next(target_h, target_m)
            hours = sleep_sec / 3600
            log.info(f"Next snapshot fire in {sleep_sec}s ({hours:.1f}h)")
            time.sleep(sleep_sec)

            log.info("Snapshot scheduler firing daily snapshot")
            try:
                from .durability import take_snapshot
                result = take_snapshot()
                if result.get("ok"):
                    log.info(
                        f"Snapshot OK: tab={result.get('tab')} "
                        f"rows={result.get('rows')} "
                        f"accounts={list((result.get('summary') or {}).keys())}"
                    )
                else:
                    log.warning(f"Snapshot failed: {result.get('error')}")
            except Exception as e:
                log.exception(f"Snapshot fire raised: {e}")

            # Sleep 60s to avoid double-firing in the same minute
            time.sleep(60)
        except Exception as e:
            log.exception(f"Snapshot scheduler loop error: {e}")
            # Back off 5 min and retry
            time.sleep(300)


def start_snapshot_scheduler():
    """Start the daemon thread once. Idempotent — safe to call repeatedly."""
    global _thread_started
    with _thread_lock:
        if _thread_started:
            return
        if not SCHEDULER_ENABLED:
            log.info("Omega snapshot scheduler disabled by env var")
            _thread_started = True  # prevent repeated log messages
            return

        t = threading.Thread(
            target=_scheduler_loop,
            daemon=True,
            name="omega-snapshot",
        )
        t.start()
        _thread_started = True
