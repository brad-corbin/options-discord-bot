"""Outcome computer daemon.

# v11.7 (Patch G.8): reads alert_price_track for each alert and writes
# alert_outcomes rows at every standard horizon (5min, 15min, 30min, 1h,
# 4h, 1d, 2d, 3d, 5d, expiry) once the elapsed time crosses that horizon.
# Idempotent — INSERT OR REPLACE.

Pure compute — no market data fetching, no Schwab calls. Operates entirely
on data already in the recorder DB.

Hit-PT flags use ANY-touch semantics: hit_pt1=1 if the track path touched
PT1 anywhere within the window from fire to this horizon (not just the
closing value). This lets queries answer "the trade got to +50% mid-day,
would I have won at exit?"

Gated by:
  RECORDER_ENABLED=true          (master gate, default off)
  RECORDER_OUTCOMES_ENABLED=true (outcome-specific gate, default off)

The daemon spawns unconditionally at bot startup; the inner loop checks
both gates each pass and no-ops if either is off. Flipping the env var on
and redeploying starts computing within one loop interval (60s).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from alert_recorder import (
    HORIZONS_SECONDS,
    _master_enabled,
    _db_path,
    _utc_micros,
    record_outcome,
)

log = logging.getLogger(__name__)

DEFAULT_LOOP_INTERVAL_S = 60

# PT levels for long structures (percentage of entry mark)
PT_LEVELS_LONG = (0.20, 0.50, 1.00)
# PT levels for credit spreads (percentage of max profit / risk)
PT_LEVELS_CREDIT = (0.50, 0.75, 0.90)

# v11.7 (Patch G.15): v2_5d evaluation alerts have no option structure
# (suggested_structure = {"type": "evaluation"}). We measure outcomes via
# underlying % movement direction-adjusted by the alert's bias. Thresholds
# are absolute % moves; V1.1 may scale-normalize by ticker ATR if data
# shows cross-ticker noise hides signal.
V25D_PT1_PCT = 0.5
V25D_PT2_PCT = 1.0
V25D_PT3_PCT = 2.0


# ─────────────────────────────────────────────────────────────
# Gate helpers
# ─────────────────────────────────────────────────────────────

def _outcomes_enabled() -> bool:
    """Both master gate AND outcomes-specific gate must be on."""
    if not _master_enabled():
        return False
    return os.getenv("RECORDER_OUTCOMES_ENABLED", "false").lower() in (
        "1", "true", "yes")


def _pt_levels(structure: dict) -> Tuple[float, float, float]:
    """Return the appropriate PT levels tuple for the structure type."""
    if structure.get("type") in ("bull_put", "bear_call"):
        return PT_LEVELS_CREDIT
    return PT_LEVELS_LONG


# ─────────────────────────────────────────────────────────────
# Core outcome computation (pure function, testable standalone)
# ─────────────────────────────────────────────────────────────

def _compute_outcome_for_horizon(
    *,
    structure: dict,
    horizon_seconds: int,
    track: List[Tuple[int, float, float]],
    pt_levels: Tuple[float, float, float],
) -> Dict:
    """Compute pnl/MFE/MAE/hit_pt at this horizon from track samples
    in [0, horizon_seconds].

    Args:
        structure:        suggested_structure dict (type, entry_mark, etc.)
        horizon_seconds:  upper boundary of the window (inclusive)
        track:            list of (elapsed_seconds, structure_mark, structure_pnl_pct)
                          tuples, sorted ascending by elapsed_seconds
        pt_levels:        (pt1, pt2, pt3) as fractional gain targets
                          e.g. (0.20, 0.50, 1.00) = 20%, 50%, 100%

    Returns:
        dict with keys: outcome_at, underlying_price, structure_mark,
        pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3,
        max_favorable_pct, max_adverse_pct.
        outcome_at and underlying_price are None here (caller fills them in).
    """
    in_window = [(e, m, p) for (e, m, p) in track if e <= horizon_seconds]
    if not in_window:
        return dict(
            outcome_at=None,
            underlying_price=None,
            structure_mark=None,
            pnl_pct=None,
            pnl_abs=None,
            hit_pt1=0,
            hit_pt2=0,
            hit_pt3=0,
            max_favorable_pct=None,
            max_adverse_pct=None,
        )

    # Closing values: last sample at-or-before the horizon boundary
    elapsed_at_close, mark_at_close, pnl_at_close = in_window[-1]

    # MFE / MAE across the entire window (ANY-touch semantics)
    pcts = [p for (_, _, p) in in_window if p is not None]
    mfe = max(pcts) if pcts else None
    mae = min(pcts) if pcts else None

    # hit_pt flags: ANY-touch — did the path reach this level at any point?
    pt1_threshold = pt_levels[0] * 100
    pt2_threshold = pt_levels[1] * 100
    pt3_threshold = pt_levels[2] * 100
    pt1 = int(any(p >= pt1_threshold for p in pcts)) if pcts else 0
    pt2 = int(any(p >= pt2_threshold for p in pcts)) if pcts else 0
    pt3 = int(any(p >= pt3_threshold for p in pcts)) if pcts else 0

    return dict(
        outcome_at=None,         # filled in by caller (fired_at + h_seconds * 1e6)
        underlying_price=None,   # not available from track table; caller leaves None
        structure_mark=mark_at_close,
        pnl_pct=pnl_at_close,
        pnl_abs=None,            # pnl_abs not stored in track; outcomes use pnl_pct
        hit_pt1=pt1,
        hit_pt2=pt2,
        hit_pt3=pt3,
        max_favorable_pct=mfe,
        max_adverse_pct=mae,
    )


# v11.7 (Patch G.15): outcome compute for v2_5d evaluation alerts.
# Unlike structure-based outcomes (long_call_burst, credit_v84),
# evaluation alerts have no option structure. We measure the
# underlying's % move from spot_at_fire, direction-adjusted by the
# alert's bias (bull = positive moves count, bear = negative moves
# count as wins after sign flip).
def _compute_evaluation_outcome_for_horizon(
    *,
    spot_at_fire: Optional[float],
    direction: Optional[str],
    horizon_seconds: int,
    track: List[Tuple[int, float]],
) -> Dict:
    """Compute pnl/MFE/MAE/hit_pt for a v2_5d evaluation alert at this horizon.

    Args:
        spot_at_fire:     underlying price at alert fire time
        direction:        'bull' or 'bear' — alert's predicted bias.
                          Anything else (None, 'neutral') defaults to bull.
        horizon_seconds:  upper boundary of the window (inclusive)
        track:            list of (elapsed_seconds, underlying_price)
                          tuples sorted ascending. Samples with NULL
                          underlying_price must be filtered out by caller.

    Returns:
        dict with the same keys as _compute_outcome_for_horizon.
        structure_mark is always None (no structure to mark). pnl_pct
        is the direction-adjusted % move of the underlying at horizon
        close. PT-hit flags use ANY-touch semantics against the V25D_PT*
        thresholds.
    """
    in_window = [(e, u) for (e, u) in track
                 if e <= horizon_seconds and u is not None]
    if not in_window or spot_at_fire is None or spot_at_fire <= 0:
        return dict(
            outcome_at=None,
            underlying_price=None,
            structure_mark=None,
            pnl_pct=None,
            pnl_abs=None,
            hit_pt1=0,
            hit_pt2=0,
            hit_pt3=0,
            max_favorable_pct=None,
            max_adverse_pct=None,
        )

    sign = -1.0 if (direction or "").lower() == "bear" else 1.0

    pct_moves = [
        sign * ((u - spot_at_fire) / spot_at_fire) * 100.0
        for (_, u) in in_window
    ]
    _, close_underlying = in_window[-1]
    close_pct = sign * ((close_underlying - spot_at_fire) / spot_at_fire) * 100.0

    mfe = max(pct_moves)
    mae = min(pct_moves)

    pt1 = int(any(p >= V25D_PT1_PCT for p in pct_moves))
    pt2 = int(any(p >= V25D_PT2_PCT for p in pct_moves))
    pt3 = int(any(p >= V25D_PT3_PCT for p in pct_moves))

    return dict(
        outcome_at=None,            # filled in by caller
        underlying_price=close_underlying,
        structure_mark=None,        # no structure for evaluation alerts
        pnl_pct=close_pct,
        pnl_abs=None,
        hit_pt1=pt1,
        hit_pt2=pt2,
        hit_pt3=pt3,
        max_favorable_pct=mfe,
        max_adverse_pct=mae,
    )


# ─────────────────────────────────────────────────────────────
# DB read helpers
# ─────────────────────────────────────────────────────────────

def _all_alerts(conn: sqlite3.Connection) -> List[dict]:
    """Load all alerts from DB. Returns list of dicts.

    Patch G.15: direction added so v2_5d evaluation outcomes can
    sign-flip for bear alerts.
    """
    rows = conn.execute(
        "SELECT alert_id, fired_at, engine, direction, suggested_structure, "
        "suggested_dte, spot_at_fire FROM alerts"
    ).fetchall()
    out = []
    for (alert_id, fired_at, engine, direction, struct, dte, spot) in rows:
        try:
            structure = json.loads(struct) if struct else {}
        except Exception:
            structure = {}
        out.append({
            "alert_id": alert_id,
            "fired_at": fired_at,
            "engine": engine,
            "direction": direction,
            "structure": structure,
            "suggested_dte": dte,
            "spot": spot,
        })
    return out


def _track_for(conn: sqlite3.Connection, alert_id: str,
               ) -> List[Tuple[int, Optional[float], Optional[float],
                              Optional[float]]]:
    """Load price track for one alert.

    Patch G.15: returns 4-tuples (elapsed_seconds, structure_mark,
    structure_pnl_pct, underlying_price). underlying_price added so
    v2_5d outcome compute can use it. Structure-based callers
    destructure positions [0:3] and ignore position [3].
    """
    rows = conn.execute(
        "SELECT elapsed_seconds, structure_mark, structure_pnl_pct, "
        "underlying_price "
        "FROM alert_price_track WHERE alert_id = ? ORDER BY elapsed_seconds",
        (alert_id,)
    ).fetchall()
    return [
        (int(e), m, p, u)
        for (e, m, p, u) in rows
        if e is not None
    ]


# ─────────────────────────────────────────────────────────────
# Single-pass logic (testable without threads)
# ─────────────────────────────────────────────────────────────

def run_single_pass() -> None:
    """One pass of the outcome computer.

    Pure compute on existing track data — no market data fetches.
    For each alert:
      1. Load its price track from alert_price_track.
      2. Skip if no track samples exist.
      3. For each standard horizon in HORIZONS_SECONDS: if elapsed time
         since fire >= horizon, compute and INSERT OR REPLACE a row in
         alert_outcomes.
      4. Handle the per-alert 'expiry' horizon using suggested_dte.

    Idempotent — calling this multiple times over the same data produces
    identical rows in alert_outcomes (INSERT OR REPLACE).
    """
    if not _master_enabled():
        return
    try:
        conn = sqlite3.connect(_db_path(), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        alerts = _all_alerts(conn)
    except Exception as e:
        log.warning(f"outcomes: load alerts failed: {e}")
        return

    now_micros = _utc_micros()
    for alert in alerts:
        try:
            alert_id = alert["alert_id"]
            fired_at = alert["fired_at"]
            track = _track_for(conn, alert_id)
            if not track:
                # No samples yet — nothing to compute
                continue

            elapsed_now = (now_micros - fired_at) // 1_000_000
            pt = _pt_levels(alert["structure"])

            # Patch G.15: branch on engine. v2_5d evaluation alerts have
            # no option structure — outcomes are computed from underlying
            # % movement direction-adjusted by alert bias.
            is_evaluation = alert["engine"] == "v2_5d"
            if is_evaluation:
                eval_track = [(e, u) for (e, _m, _p, u) in track]
            else:
                structure_track = [(e, m, p) for (e, m, p, _u) in track]

            def _compute(h_seconds: int) -> Dict:
                if is_evaluation:
                    return _compute_evaluation_outcome_for_horizon(
                        spot_at_fire=alert["spot"],
                        direction=alert.get("direction"),
                        horizon_seconds=h_seconds,
                        track=eval_track,
                    )
                return _compute_outcome_for_horizon(
                    structure=alert["structure"],
                    horizon_seconds=h_seconds,
                    track=structure_track,
                    pt_levels=pt,
                )

            # Standard horizons (5min, 15min, 30min, 1h, 4h, 1d, 2d, 3d, 5d)
            for horizon_label, h_seconds in HORIZONS_SECONDS.items():
                if elapsed_now < h_seconds:
                    # Horizon not yet reached — skip
                    continue
                computed = _compute(h_seconds)
                record_outcome(
                    alert_id=alert_id,
                    horizon=horizon_label,
                    outcome_at=fired_at + h_seconds * 1_000_000,
                    underlying_price=computed["underlying_price"],
                    structure_mark=computed["structure_mark"],
                    pnl_pct=computed["pnl_pct"],
                    pnl_abs=computed["pnl_abs"],
                    hit_pt1=computed["hit_pt1"],
                    hit_pt2=computed["hit_pt2"],
                    hit_pt3=computed["hit_pt3"],
                    max_favorable_pct=computed["max_favorable_pct"],
                    max_adverse_pct=computed["max_adverse_pct"],
                )

            # Per-alert 'expiry' horizon: derived from suggested_dte
            dte = alert.get("suggested_dte")
            if dte:
                expiry_seconds = int(dte) * 24 * 60 * 60
                if elapsed_now >= expiry_seconds:
                    computed = _compute(expiry_seconds)
                    record_outcome(
                        alert_id=alert_id,
                        horizon="expiry",
                        outcome_at=fired_at + expiry_seconds * 1_000_000,
                        underlying_price=computed["underlying_price"],
                        structure_mark=computed["structure_mark"],
                        pnl_pct=computed["pnl_pct"],
                        pnl_abs=computed["pnl_abs"],
                        hit_pt1=computed["hit_pt1"],
                        hit_pt2=computed["hit_pt2"],
                        hit_pt3=computed["hit_pt3"],
                        max_favorable_pct=computed["max_favorable_pct"],
                        max_adverse_pct=computed["max_adverse_pct"],
                    )

        except Exception as e:
            log.debug(f"outcomes: alert {alert.get('alert_id', '?')} failed: {e}")

    try:
        conn.close()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Daemon thread
# ─────────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


def _loop(stop_event: threading.Event) -> None:
    """Inner daemon loop. Runs until stop() sets the stop event."""
    log.info("outcome_computer_daemon: loop started")
    while not stop_event.is_set():
        try:
            if _outcomes_enabled():
                run_single_pass()
        except Exception as e:
            # Outer try/except: daemon never crashes
            log.warning(f"outcomes: outer loop caught: {e}")
        stop_event.wait(DEFAULT_LOOP_INTERVAL_S)
    log.info("outcome_computer_daemon: loop stopped")


def start() -> None:
    """Spawn the daemon thread. Idempotent — second call is a no-op if
    the thread is already alive. The inner loop checks _outcomes_enabled()
    each pass and no-ops when the env var is off."""
    global _thread, _stop_event
    if _thread is not None and _thread.is_alive():
        return
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_loop,
        args=(_stop_event,),
        name="outcome-computer-daemon",
        daemon=True,
    )
    _thread.start()
    log.info("outcome_computer_daemon: started")


def stop() -> None:
    """Signal the daemon to stop and wait for it to exit (up to 5s)."""
    if _stop_event is not None:
        _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
    log.info("outcome_computer_daemon: stop requested")
