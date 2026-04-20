# conviction_scorer.py
# ═══════════════════════════════════════════════════════════════════
# CONVICTION SCORER v8.3.0 — scanner-first confluence scoring
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Purpose:
#   Gate every active-scanner signal with a confluence score (0-100) derived
#   from the v3 backtest of 621,007 signals over 20 months. Score determines
#   whether the signal posts to Telegram, logs only, or is discarded.
#
# Public API:
#   score_signal(scanner_event, context_snapshot) -> ConvictionResult
#
# Integration point (app.py):
#   Called at the TOP of `_enqueue_signal` (line 3327) before the Redis push.
#   If result.decision == "discard" or "log_only", the enqueue short-circuits
#   and no Telegram post happens. If result.decision == "post", the signal
#   continues to the worker queue as before.
#
# Rollback:
#   Set CONVICTION_SCORER_ENABLED=false in Render env → scanner path bypasses
#   this module entirely. Redeploy. < 5 min.
#
# All thresholds, weights, and ticker lists are backtest-derived. Full
# provenance in CONVICTION_GATE_SPEC_v0_6.md.
# ═══════════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Literal

log = logging.getLogger("conviction_scorer")


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

# Base score for a scanner signal with no other evidence either way.
# Rationale: CB-aligned scanner signals run ~65% WR baseline in the backtest.
BASE_SCORE = 65

# Hard floor (below = discard silently) and ceiling (above = clamped).
MIN_SCORE = 0
MAX_SCORE = 100

# Default thresholds; overridable via env.
# NOTE: Validated against full 621K backtest sample on 2026-04-19.
# At threshold=70, POST_WR=73.5% vs LOG_ONLY_WR=64.7% (+8.8 WR lift) on n=55K.
# At threshold=75, WR lift drops to +2.5 because extreme-stack signals are
# rarer but not meaningfully better than high-confluence signals at 70-74.
DEFAULT_POST_THRESHOLD = 70    # ≥ this → Telegram post
DEFAULT_LOG_THRESHOLD  = 60    # ≥ this but < post → log only, no post
                                # < this → silent discard, no log

# Strictness modes affect WHICH rules apply.
STRICTNESS_LOOSE   = "loose"   # hard gates only
STRICTNESS_MEDIUM  = "medium"  # hard gates + tier gating + CB-related rules (default)
STRICTNESS_TIGHT   = "tight"   # all rules


# ═══════════════════════════════════════════════════════════════════
# TICKER TIER LISTS — locked from v8.2, validated batch 3a
# ═══════════════════════════════════════════════════════════════════

TIER_1_QUALITY = frozenset({
    "SPY", "XLF", "DIA", "GLD", "XLV", "QQQ", "CAT",
    "XLE", "GS", "TLT", "SOXX", "IWM",
    "JPM",    # bull only — bear handled via P13
    "GOOGL",  # bull only — bear handled via P13
})

TIER_2_MARGINAL_BULL = frozenset({
    "MSFT", "AMZN", "NVDA", "NFLX", "META", "UNH", "CRM", "LLY",
    # ORCL removed — bull at_edge fails at 25.6% WR (n=39)
})

TIER_3_CREDIT_ONLY = frozenset({
    "ARM", "BA", "COIN", "MRNA", "MSTR", "PLTR", "SMCI",
    "SOFI", "TSLA", "AMD",
    "ORCL",  # moved here from Tier-2 in v0.4
})

# Tickers that are debit-bear candidates. Others get P13 penalty on bear.
DEBIT_BEAR_CANDIDATES = frozenset({
    "TLT", "DIA", "XLV", "AAPL", "IWM", "SPY",
    "MSFT", "XLF", "UNH", "QQQ", "XLE",
})

# Tickers in Tier-1 for bull direction only (bear gets P13 penalty)
BULL_ONLY_TIER_1 = frozenset({"JPM", "GOOGL"})


# ═══════════════════════════════════════════════════════════════════
# RESULT TYPE
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ConvictionResult:
    """Output of score_signal().

    Fields:
        score: final 0-100 conviction score (after caps/floor/ceiling)
        decision: "post" | "log_only" | "discard"
        breakdown: rule-by-rule point contributions (for Sheets logging)
        hard_gate_triggered: the first hard gate that fired, or None
        tier_action: "tier3_credit_only" | "tier2_capped_74" | None
    """
    score: int = BASE_SCORE
    decision: Literal["post", "log_only", "discard"] = "log_only"
    breakdown: Dict[str, int] = field(default_factory=dict)
    hard_gate_triggered: Optional[str] = None
    tier_action: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "decision": self.decision,
            "breakdown": self.breakdown,
            "hard_gate_triggered": self.hard_gate_triggered,
            "tier_action": self.tier_action,
        }


# ═══════════════════════════════════════════════════════════════════
# CONFIG HELPERS — read env lazily so tests can monkey-patch
# ═══════════════════════════════════════════════════════════════════

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_str(key: str, default: str) -> str:
    return (os.getenv(key, default) or default).strip().lower()


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "").strip().lower()
    if not v:
        return default
    return v in ("true", "1", "yes", "on")


def _get_post_threshold() -> int:
    return _env_int("CONVICTION_POST_THRESHOLD", DEFAULT_POST_THRESHOLD)


def _get_log_threshold() -> int:
    return _env_int("CONVICTION_LOG_THRESHOLD", DEFAULT_LOG_THRESHOLD)


def _get_strictness() -> str:
    s = _env_str("CONVICTION_STRICTNESS", STRICTNESS_MEDIUM)
    if s not in (STRICTNESS_LOOSE, STRICTNESS_MEDIUM, STRICTNESS_TIGHT):
        return STRICTNESS_MEDIUM
    return s


def _flow_boost_enabled() -> bool:
    return _env_bool("FLOW_BOOST_ENABLED", False)


# ═══════════════════════════════════════════════════════════════════
# SAFE FIELD ACCESS
# Context snapshot comes from app.py's _build_context_snapshot helper.
# Some fields may be missing if upstream data is stale. Every lookup
# is defensive.
# ═══════════════════════════════════════════════════════════════════

def _safe_get(ctx: Dict, key: str, default=None):
    if not isinstance(ctx, dict):
        return default
    v = ctx.get(key)
    if v is None:
        return default
    return v


def _safe_str(ctx: Dict, key: str, default: str = "") -> str:
    v = _safe_get(ctx, key, default)
    return str(v).strip().lower() if v is not None else default


def _safe_float(ctx: Dict, key: str, default: float = 0.0) -> float:
    v = _safe_get(ctx, key, default)
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _safe_bool(ctx: Dict, key: str, default: bool = False) -> bool:
    v = _safe_get(ctx, key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "t")
    return default


# ═══════════════════════════════════════════════════════════════════
# HARD GATES (§4a)
# Return a tuple (gate_name, reason) if triggered, else None.
# ═══════════════════════════════════════════════════════════════════

def _check_g1_cb_misalignment(ctx: Dict) -> Optional[tuple]:
    """G1: Direction contradicts CB side.

    bull + above_cb (signal wants up, but price already above cost basis) OR
    bear + below_cb (signal wants down, but price already below cost basis)

    Backtest: -9 to -12 WR at n>15K. Batch 1 finding.
    """
    direction = _safe_str(ctx, "direction")
    cb_side = _safe_str(ctx, "cb_side")

    if not direction or not cb_side:
        return None  # missing data → don't gate

    if direction == "bull" and cb_side == "above_cb":
        return ("G1", "bull signal but price already above CB")
    if direction == "bear" and cb_side == "below_cb":
        return ("G1", "bear signal but price already below CB")

    return None


def _check_g2_no_box_bear(ctx: Dict) -> Optional[tuple]:
    """G2: Bear signal with no Potter Box structure.

    Backtest: -9.5 to -10.5 WR. Bear signals without defined resistance/support
    from Potter Box perform significantly worse. Batch 1 finding.
    """
    direction = _safe_str(ctx, "direction")
    pb_state = _safe_str(ctx, "pb_state")

    if direction == "bear" and pb_state == "no_box":
        return ("G2", "bear direction with no_box state")

    return None


def _check_g3_bear_far_below_resistance(ctx: Dict) -> Optional[tuple]:
    """G3: Bear signal with fractal resistance >= 3% above current price.

    Backtest: -14 to -20 WR at n~3K. If price has lots of room to bounce
    back up before hitting resistance, bear signals fail more often.
    Batch 3b finding.
    """
    direction = _safe_str(ctx, "direction")
    if direction != "bear":
        return None

    frac_dist = _safe_float(ctx, "fractal_resistance_above_spot_pct", -1)
    if frac_dist >= 3.0:
        return ("G3", f"bear with fractal resistance {frac_dist:.1f}% above")

    return None


def _run_hard_gates(ctx: Dict) -> Optional[tuple]:
    """Run gates G1-G3 in order. Return first gate that fires, else None."""
    for gate_fn in (_check_g1_cb_misalignment,
                    _check_g2_no_box_bear,
                    _check_g3_bear_far_below_resistance):
        result = gate_fn(ctx)
        if result:
            return result
    return None


# ═══════════════════════════════════════════════════════════════════
# PENALTIES (§4b heavy, §4c moderate)
# Each returns points (negative int) or 0. Appended to breakdown.
# ═══════════════════════════════════════════════════════════════════

def _penalty_p1_bear_near_resistance(ctx: Dict) -> int:
    """P1: Bear signal 2-3% below resistance. Batch 3b."""
    if _safe_str(ctx, "direction") != "bear":
        return 0
    frac_dist = _safe_float(ctx, "fractal_resistance_above_spot_pct", -1)
    if 2.0 <= frac_dist < 3.0:
        return -8
    return 0


def _penalty_p2_p3_established(ctx: Dict) -> int:
    """P2 and P3: wave_label=established, split by direction and source.

    v0.4 data:
      Bull + established + pinescript:      -8 (was -7 in v0.3)
      Bear + established + pinescript:      -4 (new split)
      Bull + established + active_scanner:  -6 (new split)
      Bear + established + active_scanner:  -2 (new split)
    """
    wave_label = _safe_str(ctx, "wave_label")
    if wave_label != "established":
        return 0
    direction = _safe_str(ctx, "direction")
    source = _safe_str(ctx, "scoring_source")

    if direction == "bull":
        if source == "pinescript":
            return -8
        else:  # active_scanner or default
            return -6
    else:  # bear
        if source == "pinescript":
            return -4
        else:
            return -2


def _penalty_p4_xle_bear_active(ctx: Dict) -> int:
    """P4: XLE + active_scanner + bear → -5. Backtest: WR=43.6% at n=1,524."""
    if (_safe_str(ctx, "ticker").upper() == "XLE"
            and _safe_str(ctx, "scoring_source") == "active_scanner"
            and _safe_str(ctx, "direction") == "bear"):
        return -5
    return 0


def _penalty_p5_bull_ema_q5(ctx: Dict) -> int:
    """P5: Bull + ema_diff quintile 5 (overextended). Batch 3c/3d."""
    if _safe_str(ctx, "direction") == "bull" and _safe_get(ctx, "ema_diff_quintile") == 5:
        return -4
    return 0


def _penalty_p6_bull_macd_q5(ctx: Dict) -> int:
    """P6: Bull + macd_hist Q5. Batch 3c/3d."""
    if _safe_str(ctx, "direction") == "bull" and _safe_get(ctx, "macd_hist_quintile") == 5:
        return -4
    return 0


def _penalty_p7_bear_ema_q5(ctx: Dict) -> int:
    """P7: Bear + ema_diff Q5. Batch 3c."""
    if _safe_str(ctx, "direction") == "bear" and _safe_get(ctx, "ema_diff_quintile") == 5:
        return -4
    return 0


def _penalty_p8_bear_macd_q5_30m(ctx: Dict) -> int:
    """P8: Bear + macd_hist Q5 + timeframe=30m. Batch 3d (narrowed from v8.2)."""
    if (_safe_str(ctx, "direction") == "bear"
            and _safe_get(ctx, "macd_hist_quintile") == 5
            and _safe_str(ctx, "timeframe") in ("30m", "30")):
        return -3
    return 0


def _penalty_p9_bear_rsi_q5(ctx: Dict) -> int:
    """P9: Bear + RSI Q5 (shorting into overbought — can squeeze). Batch 3c."""
    if _safe_str(ctx, "direction") == "bear" and _safe_get(ctx, "rsi_quintile") == 5:
        return -3
    return 0


def _penalty_p10_bull_30m_ema_q1(ctx: Dict) -> int:
    """P10: Bull + 30m + ema_diff Q1 (far below EMA). Batch 3c."""
    if (_safe_str(ctx, "direction") == "bull"
            and _safe_str(ctx, "timeframe") in ("30m", "30")
            and _safe_get(ctx, "ema_diff_quintile") == 1):
        return -3
    return 0


def _penalty_p11_bull_adx_q5(ctx: Dict) -> int:
    """P11: Bull + ADX Q5 (trend exhaustion). Batch 3c."""
    if _safe_str(ctx, "direction") == "bull" and _safe_get(ctx, "adx_quintile") == 5:
        return -2
    return 0


def _penalty_p12_below_floor_bull(ctx: Dict) -> int:
    """P12: pb_state=below_floor + bull (buying against structure). Batch 1."""
    if _safe_str(ctx, "pb_state") == "below_floor" and _safe_str(ctx, "direction") == "bull":
        return -2
    return 0


def _penalty_p13_weak_debit_bear_ticker(ctx: Dict) -> int:
    """P13: Weak debit-bear ticker (GOOGL, JPM) + active_scanner + bear.

    Batch 3a: 54% WR — below Tier-1 threshold. Not hard-blocked because
    sometimes works, but penalized.
    """
    if (_safe_str(ctx, "ticker").upper() in ("GOOGL", "JPM")
            and _safe_str(ctx, "direction") == "bear"
            and _safe_str(ctx, "scoring_source") == "active_scanner"):
        return -3
    return 0


def _penalty_p14_weak_marginal_bull(ctx: Dict) -> int:
    """P14: CRM, LLY marginal bulls — -3 unless at_edge recovers.

    ORCL was removed from this rule (moved to Tier-3 credit-only)
    because at_edge makes ORCL *worse* (25.6% WR at_edge vs baseline).
    """
    if (_safe_str(ctx, "ticker").upper() in ("CRM", "LLY")
            and _safe_str(ctx, "direction") == "bull"):
        if not _safe_bool(ctx, "at_edge", False):
            return -3
    return 0


def _penalty_p15_bull_late_maturity(ctx: Dict) -> int:
    """P15 (NEW in v0.4): bull + maturity=late → -3.

    Batch 1 data showed bull + late = -5.0 WR at active_scanner T2 15m.
    Symmetric counterweight to B6a (bear + late = +4).
    """
    if (_safe_str(ctx, "direction") == "bull"
            and _safe_str(ctx, "maturity") == "late"):
        return -3
    return 0


# ═══════════════════════════════════════════════════════════════════
# BOOSTS (§4d moderate, §4e heavy)
# ═══════════════════════════════════════════════════════════════════

def _boost_b1_breakout_imminent(ctx: Dict) -> int:
    """B1: wave_label=breakout_imminent → +3. Batch 1 (+3.5 to +5.6 WR)."""
    if _safe_str(ctx, "wave_label") == "breakout_imminent":
        return 3
    return 0


def _boost_b2_above_roof_bull(ctx: Dict) -> int:
    """B2: pb_state=above_roof + bull → +3. Batch 1 (+3.5 to +4.7 WR)."""
    if _safe_str(ctx, "pb_state") == "above_roof" and _safe_str(ctx, "direction") == "bull":
        return 3
    return 0


def _boost_b3_wave_aligned(ctx: Dict) -> int:
    """B3: wave direction aligned with signal direction → +3.

    v0.4 bump from +2 to +3 to match observed +3.2 to +4.9 WR lift.
    Uses 'wave_dir_original' interpretation (rt>ft=bullish, ft>rt=bearish).
    """
    direction = _safe_str(ctx, "direction")
    wave_dir = _safe_str(ctx, "wave_dir_original")  # "bullish" or "bearish"

    if (direction == "bull" and wave_dir == "bullish"):
        return 3
    if (direction == "bear" and wave_dir == "bearish"):
        return 3
    return 0


def _boost_b4_diamond(ctx: Dict) -> int:
    """B4: diamond=True → +2. Phase 2g revision.

    Originally gated on scoring_source=='pinescript' because diamond was
    a pinescript-computed field. In v8.3 Phase 2c, diamond becomes a native
    bot computation (ema_diff_quintile and macd_hist_quintile both in
    {Q2,Q3,Q4}) via diamond_detector.compute_diamond_live. With TV being
    deprecated in Phase 2f, the pinescript gate is removed.

    Weight (+2) retained as a starting point pending Phase 3 re-backtest
    validation. Adjust here once Phase 4 completes.
    """
    if _safe_bool(ctx, "diamond", False):
        return 2
    return 0


def _boost_b5_at_edge_tier2(ctx: Dict) -> int:
    """B5: at_edge + Tier-2 ticker → +2. Batch 2."""
    ticker = _safe_str(ctx, "ticker").upper()
    if _safe_bool(ctx, "at_edge", False) and ticker in TIER_2_MARGINAL_BULL:
        return 2
    return 0


def _boost_b6_bear_late_maturity(ctx: Dict) -> int:
    """B6a/B6b: Bear + late maturity, split by source.

    B6a: active_scanner → +4 (batch 1: +5.6 WR)
    B6b: pinescript → +2 (batch 1: +2.5 WR)
    """
    if (_safe_str(ctx, "direction") == "bear"
            and _safe_str(ctx, "maturity") == "late"):
        source = _safe_str(ctx, "scoring_source")
        if source == "active_scanner":
            return 4  # B6a
        elif source == "pinescript":
            return 2  # B6b
    return 0


def _boost_b7_bull_near_pivot_resistance(ctx: Dict) -> int:
    """B7: Bull + pivot resistance <2% above spot → +3. Batch 3b."""
    if _safe_str(ctx, "direction") != "bull":
        return 0
    pivot_dist = _safe_float(ctx, "pivot_resistance_above_spot_pct", 100)
    if 0 < pivot_dist < 2.0:
        return 3
    return 0


def _boost_b8_bull_blue_sky(ctx: Dict) -> int:
    """B8: Bull + fractal resistance 3%+ above (blue sky). Batch 3b (+5 to +8 WR)."""
    if _safe_str(ctx, "direction") != "bull":
        return 0
    frac_dist = _safe_float(ctx, "fractal_resistance_above_spot_pct", -1)
    if frac_dist >= 3.0:
        return 4
    return 0


def _boost_b9_bear_rejection_zone(ctx: Dict) -> int:
    """B9: Bear + pivot resistance <1% above (rejection zone). Batch 3b (+4 to +9 WR)."""
    if _safe_str(ctx, "direction") != "bear":
        return 0
    pivot_dist = _safe_float(ctx, "pivot_resistance_above_spot_pct", 100)
    if 0 < pivot_dist < 1.0:
        return 4
    return 0


def _boost_b10_bear_rsi_q1(ctx: Dict) -> int:
    """B10: Bear + RSI Q1 (oversold continuation). Batch 3c."""
    if _safe_str(ctx, "direction") == "bear" and _safe_get(ctx, "rsi_quintile") == 1:
        return 4
    return 0


def _boost_b11_bear_wt2_q1(ctx: Dict) -> int:
    """B11: Bear + WT2 Q1 (low wavetrend continuation). Batch 3c."""
    if _safe_str(ctx, "direction") == "bear" and _safe_get(ctx, "wt2_quintile") == 1:
        return 4
    return 0


def _boost_b12_flow_confirmation(ctx: Dict) -> int:
    """B12: Flow event on same ticker+direction in prior 15 min → +2.

    DEFAULT OFF via FLOW_BOOST_ENABLED=false. No backtest validation yet.
    Turn on only after §2e flow_events tab has 2+ weeks of data.
    """
    if not _flow_boost_enabled():
        return 0

    recent_flow = _safe_get(ctx, "recent_flow")
    if recent_flow is None:
        return 0
    # recent_flow is expected to be a dict or None; presence = match
    if isinstance(recent_flow, dict) and recent_flow.get("ticker"):
        # Confirm same direction
        flow_dir = str(recent_flow.get("direction", "")).lower()
        if flow_dir == _safe_str(ctx, "direction"):
            return 2
    return 0


def _boost_b13_post_box_bull_active(ctx: Dict) -> int:
    """B13 (NEW in v0.4): pb_state=post_box + bull + active_scanner → +3.

    Batch 1: +9.1 WR at n=299. Small but strong edge. Was completely
    omitted in v0.3.
    """
    if (_safe_str(ctx, "pb_state") == "post_box"
            and _safe_str(ctx, "direction") == "bull"
            and _safe_str(ctx, "scoring_source") == "active_scanner"):
        return 3
    return 0


# ═══════════════════════════════════════════════════════════════════
# CAP RULES (§4f correlation adjustment)
# Applied AFTER all boosts/penalties computed. Adjust stacks where rules
# co-occur and would otherwise double-count.
# ═══════════════════════════════════════════════════════════════════

def _apply_cap_rules(breakdown: Dict[str, int]) -> Dict[str, int]:
    """Modify the breakdown in-place to enforce stacking caps.

    Returns a NEW dict reflecting the capped contributions. Also logs the
    cap reasons as synthetic breakdown entries for audit.
    """
    out = dict(breakdown)

    # Cap: B10 + B11 at +5 (bear RSI Q1 AND WT2 Q1)
    b10 = out.get("B10", 0)
    b11 = out.get("B11", 0)
    if b10 + b11 > 5:
        excess = (b10 + b11) - 5
        # Split cap evenly; for audit, record "B10_B11_cap" as a negative
        out["B10_B11_cap"] = -excess
        log.debug(f"Cap B10+B11: {b10}+{b11}→5 (excess -{excess})")

    # Cap: P5 + P6 at -6 (bull ema_diff Q5 AND macd_hist Q5)
    p5 = out.get("P5", 0)
    p6 = out.get("P6", 0)
    if p5 + p6 < -6:
        excess = (p5 + p6) - (-6)  # excess will be negative
        out["P5_P6_cap"] = -excess  # subtract negative → add positive
        log.debug(f"Cap P5+P6: {p5}+{p6}→-6 (relief +{-excess})")

    # Cap: P7 + P8 at -6 (bear ema_diff Q5 AND macd_hist Q5 on 30m)
    p7 = out.get("P7", 0)
    p8 = out.get("P8", 0)
    if p7 + p8 < -6:
        excess = (p7 + p8) - (-6)
        out["P7_P8_cap"] = -excess
        log.debug(f"Cap P7+P8: {p7}+{p8}→-6 (relief +{-excess})")

    return out


# ═══════════════════════════════════════════════════════════════════
# TIER GATING (§5)
# Applied AFTER score is computed. Can CEILING the score or block post.
# ═══════════════════════════════════════════════════════════════════

def _apply_tier_gating(ctx: Dict, score: int,
                       result: ConvictionResult) -> int:
    """Apply tier-based gating.

    Tier-2 marginal bulls: require at_edge OR diamond to exceed 74.
    Tier-3 credit-only: score can compute, but decision forced to log_only.

    Returns the (possibly adjusted) score.
    """
    ticker = _safe_str(ctx, "ticker").upper()
    direction = _safe_str(ctx, "direction")

    # Tier-3: never post debit to main. Decision handled downstream.
    if ticker in TIER_3_CREDIT_ONLY:
        result.tier_action = "tier3_credit_only"
        log.debug(f"Tier-3 {ticker}: will force log_only")
        return score  # score unchanged; decision overridden at end

    # Tier-2: bull requires at_edge OR diamond, else ceiling at 74
    if ticker in TIER_2_MARGINAL_BULL and direction == "bull":
        at_edge = _safe_bool(ctx, "at_edge", False)
        diamond = _safe_bool(ctx, "diamond", False)
        if not (at_edge or diamond):
            if score > 74:
                log.debug(f"Tier-2 {ticker} bull: no at_edge/diamond, "
                          f"ceiling {score}→74")
                result.tier_action = "tier2_capped_74"
                return 74
    # Bull_only tickers: bear signal is handled via P13 penalty, no gate here
    return score


# ═══════════════════════════════════════════════════════════════════
# STRICTNESS FILTER
# ═══════════════════════════════════════════════════════════════════

# Rules allowed in MEDIUM strictness (CB-related + tier-related).
_MEDIUM_ALLOWED_RULES = {
    "P1", "P2_P3", "P4", "P12", "P13", "P14",
    "B1", "B2", "B7", "B8", "B9", "B13",
}
# Note: P2_P3 is one function returning combined; we treat as one key.

# LOOSE strictness = no point rules, only hard gates.


def _filter_by_strictness(breakdown: Dict[str, int],
                          strictness: str) -> Dict[str, int]:
    """Filter breakdown to only rules allowed at the given strictness level.

    loose: drop all point rules
    medium: keep only CB/tier/SR proximity rules (no indicator quintiles)
    tight: all rules kept
    """
    if strictness == STRICTNESS_TIGHT:
        return dict(breakdown)
    if strictness == STRICTNESS_LOOSE:
        # Drop everything except cap rules (which are artifacts of other rules anyway)
        return {}
    # MEDIUM: keep only allowed
    out = {}
    for key, val in breakdown.items():
        if key in _MEDIUM_ALLOWED_RULES:
            out[key] = val
        elif key.endswith("_cap"):
            # Caps only matter if both underlying rules are allowed
            pass  # drop caps in medium
    return out


# ═══════════════════════════════════════════════════════════════════
# MAIN SCORING PIPELINE
# ═══════════════════════════════════════════════════════════════════

# Registry: (rule_name, function, is_hard_gate)
_PENALTY_REGISTRY = [
    ("P1",    _penalty_p1_bear_near_resistance),
    ("P2_P3", _penalty_p2_p3_established),
    ("P4",    _penalty_p4_xle_bear_active),
    ("P5",    _penalty_p5_bull_ema_q5),
    ("P6",    _penalty_p6_bull_macd_q5),
    ("P7",    _penalty_p7_bear_ema_q5),
    ("P8",    _penalty_p8_bear_macd_q5_30m),
    ("P9",    _penalty_p9_bear_rsi_q5),
    ("P10",   _penalty_p10_bull_30m_ema_q1),
    ("P11",   _penalty_p11_bull_adx_q5),
    ("P12",   _penalty_p12_below_floor_bull),
    ("P13",   _penalty_p13_weak_debit_bear_ticker),
    ("P14",   _penalty_p14_weak_marginal_bull),
    ("P15",   _penalty_p15_bull_late_maturity),
]

_BOOST_REGISTRY = [
    ("B1",  _boost_b1_breakout_imminent),
    ("B2",  _boost_b2_above_roof_bull),
    ("B3",  _boost_b3_wave_aligned),
    ("B4",  _boost_b4_diamond),
    ("B5",  _boost_b5_at_edge_tier2),
    ("B6",  _boost_b6_bear_late_maturity),
    ("B7",  _boost_b7_bull_near_pivot_resistance),
    ("B8",  _boost_b8_bull_blue_sky),
    ("B9",  _boost_b9_bear_rejection_zone),
    ("B10", _boost_b10_bear_rsi_q1),
    ("B11", _boost_b11_bear_wt2_q1),
    ("B12", _boost_b12_flow_confirmation),
    ("B13", _boost_b13_post_box_bull_active),
]


def score_signal(scanner_event: Dict,
                 context_snapshot: Dict) -> ConvictionResult:
    """Main entry point. Compute conviction score for a scanner signal.

    This function is FAIL-OPEN at the caller level: if it raises, the
    caller should default to posting the signal. Inside this function we
    try to handle every bad input gracefully.

    Args:
        scanner_event: dict with keys: job_type, ticker, bias, webhook_data,
                       signal_msg. Built by active_scanner/_enqueue_signal.
        context_snapshot: dict with enriched context (CB side, PB state,
                          wave label, maturity, quintiles, etc.). Built by
                          app.py's _build_context_snapshot helper.

    Returns:
        ConvictionResult with score, decision, breakdown, and audit fields.
    """
    # Merge event and context so rules can read either
    ctx = {}
    if isinstance(context_snapshot, dict):
        ctx.update(context_snapshot)
    if isinstance(scanner_event, dict):
        # Event's ticker/bias override (source of truth)
        ctx.setdefault("ticker", scanner_event.get("ticker"))
        # Convert bias to direction ("bull"/"bear")
        bias = str(scanner_event.get("bias", "")).lower()
        if bias in ("bull", "bullish", "call"):
            ctx.setdefault("direction", "bull")
        elif bias in ("bear", "bearish", "put"):
            ctx.setdefault("direction", "bear")

    result = ConvictionResult()
    result.breakdown = {}

    # ─── Hard gates (always run, independent of strictness) ───
    gate_hit = _run_hard_gates(ctx)
    if gate_hit:
        gate_name, reason = gate_hit
        result.score = 0
        result.decision = "discard"
        result.hard_gate_triggered = gate_name
        result.breakdown = {gate_name: -999, "_reason": 0}
        log.info(f"Scorer GATE {gate_name}: {_safe_str(ctx, 'ticker')} "
                 f"{_safe_str(ctx, 'direction')} — {reason}")
        return result

    strictness = _get_strictness()

    # ─── LOOSE strictness: only hard gates, everything else is baseline ───
    if strictness == STRICTNESS_LOOSE:
        result.score = BASE_SCORE
        # Apply tier gating still (tier-3 etc.)
        result.score = _apply_tier_gating(ctx, result.score, result)
        _finalize_decision(ctx, result)
        return result

    # ─── MEDIUM or TIGHT: run all point rules ───
    breakdown: Dict[str, int] = {}
    for rule_name, rule_fn in _PENALTY_REGISTRY:
        try:
            points = rule_fn(ctx)
            if points != 0:
                breakdown[rule_name] = points
        except Exception as e:
            log.warning(f"Penalty {rule_name} raised: {e}")

    for rule_name, rule_fn in _BOOST_REGISTRY:
        try:
            points = rule_fn(ctx)
            if points != 0:
                breakdown[rule_name] = points
        except Exception as e:
            log.warning(f"Boost {rule_name} raised: {e}")

    # Apply correlation caps
    breakdown = _apply_cap_rules(breakdown)

    # Filter by strictness
    breakdown = _filter_by_strictness(breakdown, strictness)

    # Sum
    delta = sum(breakdown.values())
    score = BASE_SCORE + delta

    # Clamp
    score = max(MIN_SCORE, min(MAX_SCORE, score))

    # Tier gating (may cap bull scores, mark tier-3)
    score = _apply_tier_gating(ctx, score, result)

    result.score = int(score)
    result.breakdown = breakdown

    _finalize_decision(ctx, result)
    return result


def _finalize_decision(ctx: Dict, result: ConvictionResult) -> None:
    """Set result.decision based on score, thresholds, and tier action."""
    post_t = _get_post_threshold()
    log_t = _get_log_threshold()

    # Tier-3 override: never post, always at least log
    if result.tier_action == "tier3_credit_only":
        if result.score >= log_t:
            result.decision = "log_only"
        else:
            result.decision = "discard"
        return

    # Normal path
    if result.score >= post_t:
        result.decision = "post"
    elif result.score >= log_t:
        result.decision = "log_only"
    else:
        result.decision = "discard"


# ═══════════════════════════════════════════════════════════════════
# SELF-TEST: quick sanity check if run directly
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print("conviction_scorer.py self-test")

    # Test 1: High-conviction bull signal on SPY — should post
    r1 = score_signal(
        {"ticker": "SPY", "bias": "bull"},
        {"direction": "bull", "cb_side": "below_cb", "pb_state": "above_roof",
         "wave_label": "breakout_imminent", "wave_dir_original": "bullish",
         "scoring_source": "active_scanner", "maturity": "mid",
         "fractal_resistance_above_spot_pct": 5.0,  # blue sky (B8 +4)
         "pivot_resistance_above_spot_pct": 1.5}    # B7 +3
    )
    print(f"  1. SPY bull above_roof + imminent + aligned + blue_sky → score={r1.score} "
          f"decision={r1.decision}")
    print(f"     breakdown: {r1.breakdown}")
    assert r1.decision == "post", f"Expected post, got {r1.decision}"

    # Test 2: Hard gate G1 (bull + above_cb)
    r2 = score_signal(
        {"ticker": "SPY", "bias": "bull"},
        {"direction": "bull", "cb_side": "above_cb", "pb_state": "in_box"}
    )
    print(f"  2. SPY bull ABOVE CB (G1) → score={r2.score} "
          f"decision={r2.decision} gate={r2.hard_gate_triggered}")
    assert r2.decision == "discard" and r2.hard_gate_triggered == "G1"

    # Test 3: Hard gate G2 (no_box + bear)
    r3 = score_signal(
        {"ticker": "SPY", "bias": "bear"},
        {"direction": "bear", "cb_side": "above_cb", "pb_state": "no_box"}
    )
    print(f"  3. SPY bear NO_BOX (G2) → score={r3.score} "
          f"decision={r3.decision} gate={r3.hard_gate_triggered}")
    assert r3.decision == "discard" and r3.hard_gate_triggered == "G2"

    # Test 4: Tier-3 ticker (TSLA) cannot post
    r4 = score_signal(
        {"ticker": "TSLA", "bias": "bull"},
        {"direction": "bull", "cb_side": "below_cb", "pb_state": "above_roof",
         "wave_label": "breakout_imminent", "wave_dir_original": "bullish",
         "scoring_source": "active_scanner", "maturity": "mid"}
    )
    print(f"  4. TSLA bull (Tier-3) → score={r4.score} "
          f"decision={r4.decision} tier={r4.tier_action}")
    assert r4.decision != "post", f"Tier-3 should never post, got {r4.decision}"
    assert r4.tier_action == "tier3_credit_only"

    # Test 5: Tier-2 bull without at_edge capped at 74
    r5 = score_signal(
        {"ticker": "MSFT", "bias": "bull"},
        {"direction": "bull", "cb_side": "below_cb", "pb_state": "above_roof",
         "wave_label": "breakout_imminent", "wave_dir_original": "bullish",
         "scoring_source": "active_scanner", "maturity": "mid",
         "at_edge": False, "diamond": False}
    )
    print(f"  5. MSFT bull no at_edge (Tier-2) → score={r5.score} "
          f"decision={r5.decision} tier={r5.tier_action}")
    assert r5.score <= 74, f"Tier-2 bull no at_edge should cap at 74, got {r5.score}"
    assert r5.decision != "post"

    # Test 6: MSFT with at_edge can post
    r6 = score_signal(
        {"ticker": "MSFT", "bias": "bull"},
        {"direction": "bull", "cb_side": "below_cb", "pb_state": "above_roof",
         "wave_label": "breakout_imminent", "wave_dir_original": "bullish",
         "scoring_source": "active_scanner", "maturity": "mid",
         "at_edge": True}
    )
    print(f"  6. MSFT bull WITH at_edge → score={r6.score} "
          f"decision={r6.decision}")

    # Test 7: Bear far below resistance (G3)
    r7 = score_signal(
        {"ticker": "QQQ", "bias": "bear"},
        {"direction": "bear", "cb_side": "above_cb", "pb_state": "in_box",
         "fractal_resistance_above_spot_pct": 5.2}
    )
    print(f"  7. QQQ bear with 5.2% fractal above → score={r7.score} "
          f"decision={r7.decision} gate={r7.hard_gate_triggered}")
    assert r7.decision == "discard" and r7.hard_gate_triggered == "G3"

    # Test 8: Bear oversold continuation (B10 + B11 capped)
    r8 = score_signal(
        {"ticker": "SPY", "bias": "bear"},
        {"direction": "bear", "cb_side": "above_cb", "pb_state": "below_floor",
         "wave_label": "breakout_probable", "wave_dir_original": "bearish",
         "scoring_source": "active_scanner", "maturity": "late",
         "rsi_quintile": 1, "wt2_quintile": 1,
         "pivot_resistance_above_spot_pct": 0.5}
    )
    print(f"  8. SPY bear capitulation stack → score={r8.score} "
          f"decision={r8.decision}")
    print(f"     breakdown: {r8.breakdown}")

    print("\nAll self-tests passed.")
