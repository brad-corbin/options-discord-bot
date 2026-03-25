# entry_validator.py
# ═══════════════════════════════════════════════════════════════════
# Entry Validator v2 + Setup Confidence Score
#
# No trade unless all gates pass. Single deterministic validator
# that replaces the scattered if/else entry logic.
#
# Gates:
#   1. Location   — level quality score ≥ threshold
#   2. Structure  — bar close through level, not wick-only
#   3. Momentum   — 5m expansion confirms direction
#   4. Extension  — not too far from trigger / VWAP / OR / EM
#   5. Regime     — GEX+/pin favors failures; GEX-/trend favors continuation
#   6. Time       — morning continuation ≠ midday fade
#   7. Drift      — signal freshness, TV-vs-live consistency
#
# Setup Score 1-5:
#   5/5  A-tier confluence, clean momentum, non-extended, regime aligned
#   4/5  Good structure, one minor defect
#   3/5  Tradeable with reduced size / defined risk only
#   2/5  Watchlist / retest only
#   1/5  No trade
# ═══════════════════════════════════════════════════════════════════

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict

log = logging.getLogger(__name__)

# ── Gate Thresholds ──
LOCATION_MIN_SCORE = 20          # minimum level quality score
EXTENSION_MAX_PCT_VWAP = 0.5     # max distance from VWAP to enter (%)
EXTENSION_MAX_PCT_TRIGGER = 0.25 # max distance past trigger level (%)
EXTENSION_MAX_PCT_OR = 0.8       # max distance from OR boundaries (%)
EXTENSION_MAX_PCT_EM = 0.15      # max distance past EM 1σ as fraction of EM width
MOMENTUM_MIN_BODY_PCT = 0.4      # bar body must be ≥ 40% of range
MOMENTUM_MIN_EXPANSION = 0.8     # bar range must be ≥ 80% of avg range


@dataclass
class GateResult:
    """Result of a single validation gate."""
    name: str
    passed: bool
    score_contribution: int = 0
    reason: str = ""


@dataclass
class ValidationResult:
    """Complete entry validation output."""
    valid: bool = False
    setup_score: int = 1
    setup_label: str = "NO TRADE"
    gates: List[GateResult] = field(default_factory=list)
    trade_type: str = ""         # "naked_puts", "call_debit_spread", etc.
    scale_advice: str = ""       # "1/3 at T1", "2/3 at T1", etc.
    confidence_factors: Dict[str, int] = field(default_factory=dict)

    @property
    def gate_summary(self) -> str:
        return " | ".join(f"{'✓' if g.passed else '✗'} {g.name}" for g in self.gates)

    @property
    def failed_gates(self) -> List[GateResult]:
        return [g for g in self.gates if not g.passed]


class EntryValidator:
    """
    Deterministic entry validator. Evaluates all gates, computes setup score.
    """

    def validate(self, level_quality: int, level_tier: str, level_sources: set,
                 bar_closed_through: bool, bar_wicked_only: bool,
                 bar_body_pct: float, bar_expansion: float,
                 consecutive_bars: int,
                 extension_from_trigger_pct: float,
                 extension_from_vwap_pct: float,
                 extension_from_or_pct: float,
                 extension_from_em_pct: float,
                 gex_sign: str, regime: str,
                 setup_type: str,       # BREAK / FAILED / RETEST
                 time_phase: str,
                 direction: str,        # LONG / SHORT
                 is_above_gamma_flip: bool = None,
                 signal_age_sec: float = 0,
                 vix: float = 20.0,
                 or_type: str = "NORMAL",
                 ) -> ValidationResult:
        """
        Run all gates. Returns ValidationResult with setup score.
        """
        result = ValidationResult()
        score_parts = {}

        # ── Gate 1: Location ──
        g1 = self._gate_location(level_quality, level_tier)
        result.gates.append(g1)
        score_parts["location"] = g1.score_contribution

        # ── Gate 2: Structure ──
        g2 = self._gate_structure(bar_closed_through, bar_wicked_only, setup_type)
        result.gates.append(g2)
        score_parts["structure"] = g2.score_contribution

        # ── Gate 3: Momentum ──
        g3 = self._gate_momentum(bar_body_pct, bar_expansion, consecutive_bars, direction)
        result.gates.append(g3)
        score_parts["momentum"] = g3.score_contribution

        # ── Gate 4: Extension ──
        g4 = self._gate_extension(extension_from_trigger_pct, extension_from_vwap_pct,
                                   extension_from_or_pct, extension_from_em_pct)
        result.gates.append(g4)
        score_parts["extension"] = g4.score_contribution

        # ── Gate 5: Regime ──
        g5 = self._gate_regime(gex_sign, regime, setup_type, direction, is_above_gamma_flip)
        result.gates.append(g5)
        score_parts["regime"] = g5.score_contribution

        # ── Gate 6: Time ──
        g6 = self._gate_time(time_phase, setup_type, direction, or_type)
        result.gates.append(g6)
        score_parts["time"] = g6.score_contribution

        # ── Gate 7: Drift ──
        g7 = self._gate_drift(signal_age_sec)
        result.gates.append(g7)
        score_parts["drift"] = g7.score_contribution

        # ── Compute final score ──
        result.confidence_factors = score_parts
        total = sum(score_parts.values())

        # Hard fail: if any critical gate fails, cap at 2
        hard_fail_gates = {"structure", "extension"}
        hard_failed = any(not g.passed for g in result.gates if g.name in hard_fail_gates)

        # ── v15: Critical-pair rejection ──────────────────────────────────
        # If BOTH location AND momentum fail, the setup has neither a good
        # price nor confirming direction. This combination produced 5 losers
        # on 2026-03-24 (trades #4, #9, #13, #15, #17). Hard reject.
        loc_failed = not g1.passed
        mom_failed = not g3.passed
        critical_pair_failed = loc_failed and mom_failed

        if hard_failed or critical_pair_failed:
            total = min(total, 8)  # forces score ≤ 2
            if critical_pair_failed:
                log.info(f"Critical-pair rejection: location({g1.reason}) + momentum({g3.reason}) both failed")

        # Map total to 1-5 score
        # Max possible: ~7 per gate * 7 gates = 49, realistic max ~35
        if total >= 28:
            result.setup_score = 5
            result.setup_label = "HIGH CONVICTION"
        elif total >= 21:
            result.setup_score = 4
            result.setup_label = "GOOD SETUP"
        elif total >= 14:
            result.setup_score = 3
            result.setup_label = "REDUCED SIZE"
        elif total >= 7:
            result.setup_score = 2
            result.setup_label = "WATCHLIST ONLY"
        else:
            result.setup_score = 1
            result.setup_label = "NO TRADE"

        result.valid = result.setup_score >= 3
        all_passed = all(g.passed for g in result.gates)

        # ── Trade type selection ──
        result.trade_type = self._select_trade_type(
            gex_sign, setup_type, result.setup_score, vix)

        # ── Scale advice ──
        result.scale_advice = self._select_scale_advice(
            result.setup_score, gex_sign, setup_type, time_phase)

        return result

    # ── Individual Gates ──

    def _gate_location(self, quality: int, tier: str) -> GateResult:
        # Graceful degradation: if no registry scored this level (quality=0),
        # pass with neutral score — don't block entries just because registry is unavailable
        if quality == 0 and tier == "C":
            return GateResult("location", True, 3, "no registry data — neutral")
        passed = quality >= LOCATION_MIN_SCORE
        if tier == "A":
            contrib = 7
        elif tier == "B":
            contrib = 4
        else:
            contrib = 1
        return GateResult("location", passed, contrib,
                          f"quality={quality} tier={tier}")

    def _gate_structure(self, bar_closed_through: bool, bar_wicked_only: bool,
                        setup_type: str) -> GateResult:
        if setup_type == "RETEST":
            # Retest doesn't need a bar close through — it needs rejection
            passed = True
            contrib = 4
            reason = "retest — close not required"
        elif bar_closed_through:
            passed = True
            contrib = 5
            reason = "bar closed through level"
        elif bar_wicked_only:
            passed = False
            contrib = 1
            reason = "wick-only, no close through"
        else:
            passed = False
            contrib = 0
            reason = "level not tested"
        return GateResult("structure", passed, contrib, reason)

    def _gate_momentum(self, body_pct: float, expansion: float,
                       consecutive: int, direction: str) -> GateResult:
        points = 0
        reasons = []
        # Body quality
        if body_pct >= MOMENTUM_MIN_BODY_PCT:
            points += 2
        else:
            reasons.append(f"weak body {body_pct:.0%}")
        # Expansion
        if expansion >= MOMENTUM_MIN_EXPANSION:
            points += 2
        else:
            reasons.append(f"no expansion {expansion:.1f}x")
        # Direction alignment
        if (direction == "LONG" and consecutive >= 2) or (direction == "SHORT" and consecutive <= -2):
            points += 2
        elif (direction == "LONG" and consecutive >= 1) or (direction == "SHORT" and consecutive <= -1):
            points += 1
        else:
            reasons.append("no directional follow-through")
        passed = points >= 3
        return GateResult("momentum", passed, points,
                          " + ".join(reasons) if reasons else "clean momentum")

    def _gate_extension(self, trigger_pct: float, vwap_pct: float,
                        or_pct: float, em_pct: float) -> GateResult:
        points = 5  # start full, deduct
        reasons = []
        if abs(trigger_pct) > EXTENSION_MAX_PCT_TRIGGER:
            points -= 2
            reasons.append(f"extended {trigger_pct:.2f}% past trigger")
        if abs(vwap_pct) > EXTENSION_MAX_PCT_VWAP:
            points -= 1
            reasons.append(f"far from VWAP ({vwap_pct:.2f}%)")
        if abs(or_pct) > EXTENSION_MAX_PCT_OR:
            points -= 1
            reasons.append(f"far from OR ({or_pct:.2f}%)")
        if abs(em_pct) > EXTENSION_MAX_PCT_EM:
            points -= 1
            reasons.append(f"past EM boundary ({em_pct:.2f}%)")
        points = max(0, points)
        passed = points >= 3
        return GateResult("extension", passed, points,
                          " | ".join(reasons) if reasons else "non-extended")

    def _gate_regime(self, gex_sign: str, regime: str, setup_type: str,
                     direction: str, is_above_flip: bool = None) -> GateResult:
        points = 0
        reasons = []
        # Regime alignment
        if gex_sign == "negative" and setup_type == "BREAK":
            points += 3  # GEX- continuation = high edge
            reasons.append("GEX- break = strong")
        elif gex_sign == "positive" and setup_type == "FAILED":
            points += 3  # GEX+ failed move = high edge
            reasons.append("GEX+ failed = strong")
        elif gex_sign == "negative" and setup_type == "FAILED":
            points += 2  # GEX- squeeze can run
            reasons.append("GEX- squeeze")
        elif gex_sign == "positive" and setup_type == "BREAK":
            points += 1  # GEX+ break = lower conviction
            reasons.append("GEX+ break = caution")
        else:
            points += 2  # neutral
        # Gamma flip alignment
        if is_above_flip is not None:
            if (direction == "LONG" and is_above_flip) or (direction == "SHORT" and not is_above_flip):
                points += 2
                reasons.append("aligned with flip")
            else:
                reasons.append("against flip")
        passed = points >= 2
        return GateResult("regime", passed, points, " | ".join(reasons))

    def _gate_time(self, phase: str, setup_type: str, direction: str,
                   or_type: str = "NORMAL") -> GateResult:
        points = 3  # default neutral
        reasons = []
        if phase == "OPEN":
            if setup_type == "BREAK" and or_type == "NARROW":
                points = 5  # narrow OR break at open = highest quality
                reasons.append("narrow OR break at open")
            elif setup_type == "BREAK":
                points = 4
                reasons.append("open expansion")
            else:
                points = 2
                reasons.append("open — wait for structure")
        elif phase == "MORNING":
            if setup_type == "BREAK":
                points = 5
                reasons.append("morning break = best")
            else:
                points = 4
        elif phase == "MIDDAY":
            if setup_type == "FAILED":
                points = 5
                reasons.append("midday fade = best")
            elif setup_type == "BREAK":
                points = 2
                reasons.append("midday break = low conviction")
            else:
                points = 3
        elif phase == "AFTERNOON":
            points = 4
            reasons.append("trend resumption zone")
        elif phase in ("POWER_HOUR", "CLOSE"):
            if setup_type == "FAILED":
                points = 4
                reasons.append("late pin/fade")
            else:
                points = 2
                reasons.append("late — reduce size")
        elif phase in ("PRE_MARKET", "AFTER_HOURS"):
            points = 0
            reasons.append("session closed")
        passed = points >= 2
        return GateResult("time", passed, points,
                          " | ".join(reasons) if reasons else phase)

    def _gate_drift(self, signal_age_sec: float) -> GateResult:
        if signal_age_sec <= 0:
            # Live monitor signal — freshest possible
            return GateResult("drift", True, 5, "live signal")
        elif signal_age_sec < 60:
            return GateResult("drift", True, 4, f"fresh ({signal_age_sec:.0f}s)")
        elif signal_age_sec < 180:
            return GateResult("drift", True, 3, f"recent ({signal_age_sec:.0f}s)")
        elif signal_age_sec < 300:
            return GateResult("drift", True, 2, f"aging ({signal_age_sec:.0f}s)")
        else:
            return GateResult("drift", False, 0, f"stale ({signal_age_sec:.0f}s)")

    # ── Trade Type Selection ──

    def _select_trade_type(self, gex_sign: str, setup_type: str,
                           score: int, vix: float) -> str:
        """Select trade instrument based on regime + setup quality."""
        if score <= 2:
            return "no_trade"
        # High VIX: prefer spreads to cap vega risk
        if vix > 28:
            return "debit_spread"
        if gex_sign == "negative" and setup_type == "BREAK" and score >= 4:
            return "naked_options"  # GEX- break: let it run
        if gex_sign == "positive" and setup_type == "FAILED" and score >= 4:
            return "debit_spread"  # GEX+ reversal: capped
        if gex_sign == "negative" and setup_type == "FAILED":
            return "naked_options"  # GEX- squeeze: can run hard
        if score >= 4:
            return "naked_options"
        return "debit_spread"  # default: defined risk

    # ── Scale Advice ──

    def _select_scale_advice(self, score: int, gex_sign: str,
                             setup_type: str, time_phase: str) -> str:
        """How much to take at first target."""
        if time_phase in ("POWER_HOUR", "CLOSE"):
            return "2/3 at T1 — late session, heavier first scale"
        if score >= 5 and gex_sign == "negative" and setup_type == "BREAK":
            return "1/3 at T1 — let runner work"
        if score >= 4:
            return "1/2 at T1"
        if score >= 3 or gex_sign == "positive":
            return "2/3 at T1 — defined risk, heavier scale"
        return "full exit at T1"
