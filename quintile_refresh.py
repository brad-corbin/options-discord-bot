# quintile_refresh.py
# ═══════════════════════════════════════════════════════════════════
# v8.3 Phase 2d: Nightly refresh of per-combo quintile boundaries.
#
# Reads the last 30 days of signal_decisions from Google Sheets (or the
# local CSV backup), computes percentile boundaries per (scoring_source,
# timeframe, tier, direction, indicator), and writes them to Redis via
# quintile_store.write_bounds.
#
# Scheduling
# ----------
# App scheduler calls run_refresh() at 03:00 CT daily. On success, Redis
# entry scorer:quintile_bounds:v1 is replaced with a 48h TTL. On failure,
# old boundaries stay valid until their TTL expires, then quintile_store
# falls back to FALLBACK_BOUNDS.
#
# Design notes
# ------------
# - Only refreshes from scanner-sourced signals. Pinescript signals are
#   excluded going forward (TV deprecated in Phase 2f).
# - Requires minimum 100 signals per combo to compute reliable boundaries.
#   Combos below threshold retain fallback or previous values.
# - Fails gracefully: a partial refresh is better than none, so we merge
#   new combos with existing Redis data rather than replacing wholesale.
# - Never raises to caller; logs all failures.
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Window for boundary computation
LOOKBACK_DAYS      = 30
MIN_SIGNALS_PER_COMBO = 100  # below this, don't recompute — keep existing

# Indicator → signal_decisions column name mapping
INDICATOR_COLUMN_MAP = {
    "ema_diff_pct": "ema_dist_pct",   # live scanner uses ema_dist_pct
    "macd_hist":    "macd_hist",
    "rsi":          "rsi_mfi",
    "wt2":          "wt2",
    "adx":          "adx",             # may not be in scanner output yet
}


def _quintile_bounds(values: List[float]) -> Optional[List[float]]:
    """Compute [q20, q40, q60, q80] breakpoints. Matches backtest function.

    From backtest/analyze_combined_v1.py:289-295.
    """
    if not values:
        return None
    sv = sorted(values)
    n = len(sv)
    if n < MIN_SIGNALS_PER_COMBO:
        return None
    return [
        sv[int(n * 0.2)],
        sv[int(n * 0.4)],
        sv[int(n * 0.6)],
        sv[int(n * 0.8)],
    ]


# ─────────────────────────────────────────────────────────────────
# v8.3 Phase 2f fix: self-contained Sheets reader.
# Previously this module imported a nonexistent `sheets_writer.fetch_rows`;
# replaced with direct gspread usage modeled on app.py's
# _append_google_sheet_values pattern (service account auth via
# GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS, sheet ID
# via GOOGLE_SHEET_ID, tab via GOOGLE_SHEET_SIGNAL_TAB).
# Any gspread, auth, or network failure falls through to CSV fallback.
# ─────────────────────────────────────────────────────────────────
def _build_gspread_client():
    """Lazily construct an authorized gspread client. Returns None on any failure."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:
        log.debug(f"quintile_refresh: gspread/google-auth not importable: {e}")
        return None

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # Auth path A: JSON string in env (Render pattern)
    try:
        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if sa_json:
            info = json.loads(sa_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            return gspread.authorize(creds)
    except Exception as e:
        log.debug(f"quintile_refresh: SA JSON auth failed: {e}")

    # Auth path B: filesystem path
    try:
        sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if sa_path and os.path.exists(sa_path):
            creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
            return gspread.authorize(creds)
    except Exception as e:
        log.debug(f"quintile_refresh: SA file auth failed: {e}")

    log.debug("quintile_refresh: no service account credentials in env")
    return None


def _fetch_rows_from_sheets(tab_name: str, days_back: int) -> List[dict]:
    """Read rows from a signal_decisions-shape tab, filtered to last N days.

    Returns [] on any failure. Never raises.
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        log.debug("quintile_refresh: GOOGLE_SHEET_ID not set")
        return []

    client = _build_gspread_client()
    if client is None:
        return []

    try:
        sh = client.open_by_key(sheet_id)
        ws = sh.worksheet(tab_name)
    except Exception as e:
        log.warning(f"quintile_refresh: open worksheet '{tab_name}' failed: {e}")
        return []

    try:
        # get_all_records returns list[dict] using header row as keys
        records = ws.get_all_records() or []
    except Exception as e:
        log.warning(f"quintile_refresh: get_all_records failed: {e}")
        return []

    # Filter by timestamp_utc
    cutoff = time.time() - days_back * 86400
    out = []
    for r in records:
        try:
            ts = float(r.get("timestamp_utc", 0) or 0)
            if ts >= cutoff:
                out.append(r)
        except (TypeError, ValueError):
            continue
    return out


def _fetch_signal_decisions_last_n_days(days: int) -> List[dict]:
    """Return list of signal_decisions rows from the last N days.

    Tries Google Sheets first, falls back to local CSV if Sheets unavailable.
    """
    # Option 1: Google Sheets via self-contained reader
    try:
        tab = os.getenv("GOOGLE_SHEET_SIGNAL_TAB", "signal_decisions").strip() or "signal_decisions"
        rows = _fetch_rows_from_sheets(tab, days) or []
        if rows:
            log.info(f"quintile_refresh: fetched {len(rows)} rows from Sheets")
            return rows
    except Exception as e:
        log.debug(f"quintile_refresh: Sheets fetch unavailable: {e}")

    # Option 2: Local CSV fallback
    # v8.3 Phase 2f fix: use AUTO_LOG_DIR (the app's canonical CSV location)
    # instead of hardcoded /var/data/. Falls back to /tmp only if env unset.
    rows = []
    try:
        import csv
        log_dir = os.getenv("AUTO_LOG_DIR", "/tmp").strip() or "/tmp"
        path = os.getenv("SIGNAL_DECISIONS_CSV_PATH",
                         os.path.join(log_dir, "signal_decisions.csv"))
        if os.path.exists(path):
            cutoff = time.time() - days * 86400
            with open(path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    try:
                        ts = float(r.get("timestamp_utc", 0) or 0)
                        if ts >= cutoff:
                            rows.append(r)
                    except Exception:
                        continue
            log.info(f"quintile_refresh: fetched {len(rows)} rows from CSV fallback ({path})")
            return rows
        else:
            log.debug(f"quintile_refresh: CSV fallback path does not exist: {path}")
    except Exception as e:
        log.warning(f"quintile_refresh: CSV fetch failed: {e}")

    return rows


def _group_signals(rows: List[dict]) -> Dict[tuple, List[dict]]:
    """Group signal rows by (scoring_source, timeframe, tier, direction)."""
    groups = defaultdict(list)
    for r in rows:
        ss = str(r.get("source", r.get("scoring_source", "")) or "").strip().lower()
        # Skip non-scanner sources (TV deprecated)
        if ss != "active_scanner":
            continue
        tf = str(r.get("timeframe", "5") or "5").strip().lower()
        if not tf.endswith("m"):
            tf = tf + "m"
        tier = str(r.get("tier", "") or "").strip().upper()
        if not tier.startswith("T"):
            tier = "T" + tier
        direction = str(r.get("bias", r.get("direction", "")) or "").strip().lower()
        if direction not in ("bull", "bear"):
            continue
        groups[(ss, tf, tier, direction)].append(r)
    return dict(groups)


def compute_bounds_for_rows(rows: List[dict]) -> Dict[str, Dict[str, List[float]]]:
    """Compute per-combo per-indicator bounds from a list of signal rows.

    Returns {combo_key: {indicator: [q20, q40, q60, q80]}}.
    Combos/indicators with insufficient data are omitted.
    """
    groups = _group_signals(rows)
    log.info(f"quintile_refresh: {len(groups)} combos from {len(rows)} rows")

    out: Dict[str, Dict[str, List[float]]] = {}
    for (ss, tf, tier, direction), combo_rows in groups.items():
        combo_key = f"{ss}:{tf}:{tier}:{direction}"
        indicator_bounds: Dict[str, List[float]] = {}

        for indicator, col in INDICATOR_COLUMN_MAP.items():
            values = []
            for r in combo_rows:
                raw = r.get(col)
                if raw is None or raw == "":
                    continue
                try:
                    v = float(raw)
                    values.append(v)
                except (ValueError, TypeError):
                    continue

            bounds = _quintile_bounds(values)
            if bounds:
                indicator_bounds[indicator] = bounds
                log.debug(f"  {combo_key}.{indicator}: n={len(values)} bounds={bounds}")

        if indicator_bounds:
            out[combo_key] = indicator_bounds

    return out


def run_refresh() -> bool:
    """One-shot refresh: fetch rows → compute bounds → write to Redis.

    Returns True on success (even partial), False if no data at all.
    """
    try:
        rows = _fetch_signal_decisions_last_n_days(LOOKBACK_DAYS)
        if not rows:
            log.warning(f"quintile_refresh: no rows in last {LOOKBACK_DAYS} days")
            return False

        bounds = compute_bounds_for_rows(rows)
        if not bounds:
            log.warning("quintile_refresh: no valid combos computed — skipping write")
            return False

        # Merge with existing Redis data so partial refreshes don't nuke
        # combos that had temporarily insufficient data
        from quintile_store import _get_redis, REDIS_KEY_BOUNDS, write_bounds
        r = _get_redis()
        if r is not None:
            try:
                existing_raw = r.get(REDIS_KEY_BOUNDS)
                if existing_raw:
                    existing = json.loads(existing_raw)
                    # New bounds override existing per-combo
                    merged = dict(existing)
                    merged.update(bounds)
                    bounds = merged
                    log.info(f"quintile_refresh: merged {len(existing)} existing + {len(bounds) - len(existing)} new")
            except Exception as e:
                log.warning(f"quintile_refresh: merge with existing failed, using new-only: {e}")

        ok = write_bounds(bounds)
        log.info(f"quintile_refresh: {'success' if ok else 'write failed'} — {len(bounds)} combos")
        return ok
    except Exception as e:
        log.error(f"quintile_refresh: unhandled failure: {e}", exc_info=True)
        return False


def schedule_in_app(scheduler_fn):
    """Register the refresh job with the app's scheduler.

    scheduler_fn is a callable that registers a cron-like task. Example usage
    from app.py startup:
      from quintile_refresh import schedule_in_app
      schedule_in_app(lambda hhmm_ct, cb: _scheduler.at(hhmm_ct, cb))
    """
    try:
        scheduler_fn("03:00", run_refresh)
        log.info("quintile_refresh: scheduled for 03:00 CT daily")
    except Exception as e:
        log.error(f"quintile_refresh: schedule failed: {e}")


if __name__ == "__main__":
    # Allow manual invocation: `python3 quintile_refresh.py`
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ok = run_refresh()
    exit(0 if ok else 1)
