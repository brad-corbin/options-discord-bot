# diamond_detector.py
# ═══════════════════════════════════════════════════════════════════
# v8.3 Phase 2c: Native diamond computation.
#
# Purpose
# -------
# The backtest's `diamond` flag (analyze_combined_v1.py:776-785) is derived
# as:
#
#     diamond = (ema_diff_quintile ∈ {Q2, Q3, Q4}) AND
#               (macd_hist_quintile ∈ {Q2, Q3, Q4})
#
# The intent: a setup is "diamond" when both its EMA displacement and MACD
# histogram are in the middle quintiles — neither overextended nor flat.
#
# In v8.3 we adopt this as the CANONICAL diamond definition across backtest
# and live bot. Pinescript-based diamond is deprecated with TV deprecation.
#
# Usage
# -----
# from diamond_detector import compute_diamond_live
# is_diamond = compute_diamond_live(
#     ema_diff_value=0.42,
#     macd_hist_value=0.05,
#     scoring_source="active_scanner",
#     timeframe="5m",
#     tier="T1",
#     direction="bull",
# )
#
# Integration
# -----------
# `active_scanner._analyze_ticker` adds `diamond_live: is_diamond` to the
# signal dict (see Phase 2c-integration below). Scorer's _build_context_snapshot
# reads it as `ctx['diamond']` after pinescript-path removal in 2f.
#
# The backtest is updated in Phase 3b to compute this same field per signal,
# enabling revalidation of B4 (+2 for diamond) against measured edge.
#
# Rollback
# --------
# `DIAMOND_DETECTOR_ENABLED=false` env → always returns False. Scorer B4
# then contributes 0 to every signal. Safe rollback, no other downstream
# dependencies.
# ═══════════════════════════════════════════════════════════════════

import logging
import os

log = logging.getLogger(__name__)

from quintile_store import get_quintile_bucket

# Middle quintiles — the "not overextended" range
_MIDDLE_QUINTILES = {"Q2", "Q3", "Q4"}


def compute_diamond_live(
    ema_diff_value: float,
    macd_hist_value: float,
    scoring_source: str,
    timeframe: str,
    tier: str,
    direction: str,
) -> bool:
    """Return True if the signal is a 'diamond' per the backtest definition.

    Matches analyze_combined_v1.py:776-785 exactly:
      diamond == (ema_diff_q ∈ {Q2,Q3,Q4}) AND (macd_hist_q ∈ {Q2,Q3,Q4})

    Never raises. Unknown quintile → False (i.e., conservative; we don't
    claim diamond status unless we can verify both indicator quintiles).
    """
    if os.getenv("DIAMOND_DETECTOR_ENABLED", "true").strip().lower() != "true":
        return False

    try:
        ema_q = get_quintile_bucket(
            indicator="ema_diff_pct",
            value=ema_diff_value,
            scoring_source=scoring_source,
            timeframe=timeframe,
            tier=tier,
            direction=direction,
        )
        macd_q = get_quintile_bucket(
            indicator="macd_hist",
            value=macd_hist_value,
            scoring_source=scoring_source,
            timeframe=timeframe,
            tier=tier,
            direction=direction,
        )
    except Exception as e:
        log.debug(f"diamond_detector: quintile lookup failed: {e}")
        return False

    if ema_q in _MIDDLE_QUINTILES and macd_q in _MIDDLE_QUINTILES:
        return True

    return False


def diamond_reason(
    ema_diff_value: float,
    macd_hist_value: float,
    scoring_source: str,
    timeframe: str,
    tier: str,
    direction: str,
) -> dict:
    """Return a diagnostic dict showing why diamond is True or False.

    Used for Sheets logging (conviction_decisions tab) to audit B4 firing.
    """
    try:
        ema_q = get_quintile_bucket(
            indicator="ema_diff_pct", value=ema_diff_value,
            scoring_source=scoring_source, timeframe=timeframe,
            tier=tier, direction=direction,
        )
        macd_q = get_quintile_bucket(
            indicator="macd_hist", value=macd_hist_value,
            scoring_source=scoring_source, timeframe=timeframe,
            tier=tier, direction=direction,
        )
    except Exception:
        ema_q = "error"
        macd_q = "error"

    is_diamond = (ema_q in _MIDDLE_QUINTILES) and (macd_q in _MIDDLE_QUINTILES)

    return {
        "diamond": is_diamond,
        "ema_diff_quintile": ema_q,
        "macd_hist_quintile": macd_q,
        "ema_diff_value": float(ema_diff_value) if ema_diff_value is not None else None,
        "macd_hist_value": float(macd_hist_value) if macd_hist_value is not None else None,
    }
