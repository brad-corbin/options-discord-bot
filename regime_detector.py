# regime_detector.py
# ═══════════════════════════════════════════════════════════════════
# Regime Transition Detector
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Detects TRANSITIONS between volatility regimes — not the regime itself,
# but the moment of change. The most profitable trades happen at regime
# shifts, not once you're already deep inside one.
#
# Four transition types:
#
#   ELEVATED → CRISIS (crash developing)
#     VIX accelerating upward, term structure inverting,
#     GEX flipping negative. Pre-position puts BEFORE the crowd.
#
#   CRISIS → NORMALIZING (snap-back coming)
#     VIX decelerating after spike, term structure re-steepening,
#     GEX flipping positive. Snap-back long calls become valid.
#
#   LOW_VOL → BREAKOUT (expansion starting)
#     VIX rising from low base with directional price move,
#     volume expanding after contraction. Long calls at cheap premium.
#
#   TRENDING → EXHAUSTION (mean reversion coming)
#     VIX declining while price still trending (complacency),
#     volume fading, RSI divergence. Fade setups start working.
#
# Consumes data already flowing through the system:
#   - VIX from CBOE (cached)
#   - VIX9D from CBOE
#   - Term structure from vix_term_structure.py
#   - GEX from v4 institutional flow
#
# Usage:
#   from regime_detector import RegimeDetector
#   detector = RegimeDetector()
#   detector.update(vix=27.4, vix9d=30.1, gex_sign="negative", ...)
#   transition = detector.current_transition
# ═══════════════════════════════════════════════════════════════════

import time
import logging
import threading
from datetime import datetime
from typing import Dict, Optional, List
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

# VIX velocity thresholds (points per observation)
VIX_ACCEL_THRESHOLD      = 1.5     # VIX rising >1.5 pts between observations = accelerating
VIX_DECEL_THRESHOLD      = -1.0    # VIX falling >1.0 pt = decelerating
VIX_SPIKE_THRESHOLD      = 3.0     # VIX up >3 pts = spike (immediate alert)

# Term structure
TERM_INVERSION_THRESHOLD = 0.5     # VIX9D > VIX by this much = inverted (panic)
TERM_STEEP_THRESHOLD     = -2.0    # VIX9D < VIX by this much = steep contango (calm)

# Regime boundaries
VIX_LOW_CEILING          = 16.0    # below this = low vol environment
VIX_ELEVATED_FLOOR       = 20.0    # above this = elevated
VIX_CRISIS_FLOOR         = 28.0    # above this = crisis territory

# Transition detection requires N consecutive confirming observations
CONFIRMATION_COUNT       = 2       # need 2 consecutive signals to confirm transition

# Rolling window for VIX history
VIX_HISTORY_SIZE         = 50      # store last 50 observations

# How often to check (seconds) — called externally, this is just for logging
CHECK_INTERVAL_SEC       = 1800    # 30 minutes


# ═══════════════════════════════════════════════════════════
# TRANSITION TYPES
# ═══════════════════════════════════════════════════════════

@dataclass
class RegimeTransition:
    """A detected regime transition with actionable context."""
    transition_type: str    # CRISIS_DEVELOPING, NORMALIZING, BREAKOUT, EXHAUSTION
    from_regime: str        # what we're leaving
    to_regime: str          # what we're entering
    confidence: float       # 0-1, how confident in the transition
    signals: List[str]      # what triggered the detection
    action: str             # recommended positioning
    is_snapback: bool       # flag for long call unlock
    detected_at: float      # timestamp
    vix: float = 0.0
    vix_velocity: float = 0.0
    term_structure: str = "flat"
    gex_sign: str = "positive"

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.detected_at) / 60

    def format_alert(self) -> str:
        """Format for Telegram posting."""
        emoji = {
            "CRISIS_DEVELOPING": "🔴⚡",
            "NORMALIZING": "🟢📈",
            "BREAKOUT": "🟡🚀",
            "EXHAUSTION": "🟠⚠️",
        }.get(self.transition_type, "❓")

        lines = [
            f"{emoji} REGIME SHIFT: {self.transition_type}",
            f"",
            f"VIX: {self.vix:.1f} (velocity: {self.vix_velocity:+.1f}/obs)",
            f"Term structure: {self.term_structure}",
            f"GEX: {self.gex_sign}",
            f"Confidence: {self.confidence:.0%}",
            f"",
            f"📋 {self.action}",
            f"",
            f"Signals:",
        ]
        for s in self.signals:
            lines.append(f"  • {s}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# DETECTOR
# ═══════════════════════════════════════════════════════════

class RegimeDetector:
    """
    Tracks VIX, term structure, and GEX over time.
    Detects regime transitions at the moment of change.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Rolling history
        self._vix_history: List[dict] = []  # [{vix, vix9d, gex_sign, term, ts}, ...]
        # Current state
        self._current_regime: str = "UNKNOWN"
        self._current_transition: Optional[RegimeTransition] = None
        self._transition_history: List[RegimeTransition] = []
        # Confirmation counters
        self._crisis_developing_count = 0
        self._normalizing_count = 0
        self._breakout_count = 0
        self._exhaustion_count = 0

    def update(
        self,
        vix: float,
        vix9d: float = 0.0,
        gex_sign: str = "positive",
        gex_value: float = 0.0,
        term_structure: str = "flat",
        spot_change_pct: float = 0.0,
        volume_ratio: float = 1.0,
    ) -> Optional[RegimeTransition]:
        """
        Feed new observation. Returns a RegimeTransition if one is detected.
        Call this every 30 minutes during market hours.
        """
        with self._lock:
            now = time.time()

            obs = {
                "vix": vix,
                "vix9d": vix9d,
                "gex_sign": gex_sign,
                "gex_value": gex_value,
                "term": term_structure,
                "spot_change_pct": spot_change_pct,
                "volume_ratio": volume_ratio,
                "ts": now,
            }

            self._vix_history.append(obs)
            if len(self._vix_history) > VIX_HISTORY_SIZE:
                self._vix_history = self._vix_history[-VIX_HISTORY_SIZE:]

            # Need at least 2 observations for velocity
            if len(self._vix_history) < 2:
                self._current_regime = self._classify_regime(vix)
                return None

            prev = self._vix_history[-2]
            new_regime = self._classify_regime(vix)
            vix_velocity = vix - prev["vix"]
            term_state = self._classify_term(vix, vix9d)

            # ── Check each transition type ──
            transition = None

            # 1. ELEVATED → CRISIS (crash developing)
            if self._check_crisis_developing(vix, vix_velocity, term_state,
                                              gex_sign, spot_change_pct):
                self._crisis_developing_count += 1
                if self._crisis_developing_count >= CONFIRMATION_COUNT:
                    transition = self._build_transition(
                        "CRISIS_DEVELOPING",
                        self._current_regime, "CRISIS",
                        vix, vix_velocity, term_state, gex_sign,
                    )
            else:
                self._crisis_developing_count = max(0, self._crisis_developing_count - 1)

            # 2. CRISIS → NORMALIZING (snap-back)
            if not transition and self._check_normalizing(
                    vix, vix_velocity, term_state, gex_sign):
                self._normalizing_count += 1
                if self._normalizing_count >= CONFIRMATION_COUNT:
                    transition = self._build_transition(
                        "NORMALIZING",
                        self._current_regime, "ELEVATED",
                        vix, vix_velocity, term_state, gex_sign,
                    )
            else:
                self._normalizing_count = max(0, self._normalizing_count - 1)

            # 3. LOW_VOL → BREAKOUT
            if not transition and self._check_breakout(
                    vix, vix_velocity, term_state, volume_ratio, spot_change_pct):
                self._breakout_count += 1
                if self._breakout_count >= CONFIRMATION_COUNT:
                    transition = self._build_transition(
                        "BREAKOUT",
                        self._current_regime, "TRENDING",
                        vix, vix_velocity, term_state, gex_sign,
                    )
            else:
                self._breakout_count = max(0, self._breakout_count - 1)

            # 4. TRENDING → EXHAUSTION
            if not transition and self._check_exhaustion(
                    vix, vix_velocity, volume_ratio, spot_change_pct):
                self._exhaustion_count += 1
                if self._exhaustion_count >= CONFIRMATION_COUNT:
                    transition = self._build_transition(
                        "EXHAUSTION",
                        self._current_regime, "LOW_VOL",
                        vix, vix_velocity, term_state, gex_sign,
                    )
            else:
                self._exhaustion_count = max(0, self._exhaustion_count - 1)

            # Update regime
            self._current_regime = new_regime

            if transition:
                self._current_transition = transition
                self._transition_history.append(transition)
                if len(self._transition_history) > 20:
                    self._transition_history = self._transition_history[-20:]
                log.info(f"Regime transition detected: {transition.transition_type} "
                         f"(conf={transition.confidence:.0%}, VIX={vix:.1f}, "
                         f"vel={vix_velocity:+.1f})")

            return transition

    # ── Transition Detection Logic ──

    def _check_crisis_developing(
        self, vix: float, vix_vel: float, term: str,
        gex: str, spot_pct: float,
    ) -> bool:
        """ELEVATED → CRISIS: VIX accelerating, term inverting, GEX flipping neg."""
        signals = []
        if vix >= VIX_ELEVATED_FLOOR:
            signals.append("VIX elevated")
        if vix_vel >= VIX_ACCEL_THRESHOLD:
            signals.append("VIX accelerating")
        if vix_vel >= VIX_SPIKE_THRESHOLD:
            signals.append("VIX spiking")
        if term == "inverted":
            signals.append("Term structure inverted")
        if gex == "negative":
            signals.append("GEX negative")
        if spot_pct < -0.5:
            signals.append("Market declining")
        # Need at least 3 of these signals
        return len(signals) >= 3

    def _check_normalizing(
        self, vix: float, vix_vel: float, term: str, gex: str,
    ) -> bool:
        """CRISIS → NORMALIZING: VIX decelerating after spike, term re-steepening."""
        if vix < VIX_ELEVATED_FLOOR:
            return False  # already normalized
        signals = []
        if vix_vel <= VIX_DECEL_THRESHOLD:
            signals.append("VIX declining")
        if self._current_regime == "CRISIS" and vix < VIX_CRISIS_FLOOR:
            signals.append("VIX dropped below crisis threshold")
        if term in ("flat", "contango"):
            signals.append("Term structure normalizing")
        if gex == "positive":
            signals.append("GEX flipped positive")
        # Check if VIX peaked (current < recent max)
        if len(self._vix_history) >= 5:
            recent_max = max(h["vix"] for h in self._vix_history[-5:])
            if vix < recent_max * 0.95:
                signals.append("VIX off recent highs")
        return len(signals) >= 3

    def _check_breakout(
        self, vix: float, vix_vel: float, term: str,
        vol_ratio: float, spot_pct: float,
    ) -> bool:
        """LOW_VOL → BREAKOUT: VIX rising from low base, volume expanding."""
        if self._current_regime not in ("LOW_VOL", "UNKNOWN"):
            return False
        signals = []
        if vix_vel > 0.5 and vix < VIX_ELEVATED_FLOOR:
            signals.append("VIX rising from low base")
        if vol_ratio > 1.3:
            signals.append("Volume expanding")
        if abs(spot_pct) > 0.8:
            signals.append("Large price move")
        # Check VIX was recently low
        if len(self._vix_history) >= 3:
            recent_avg = sum(h["vix"] for h in self._vix_history[-3:]) / 3
            if recent_avg < VIX_LOW_CEILING:
                signals.append("VIX emerging from low base")
        return len(signals) >= 3

    def _check_exhaustion(
        self, vix: float, vix_vel: float,
        vol_ratio: float, spot_pct: float,
    ) -> bool:
        """TRENDING → EXHAUSTION: VIX declining while price still trending."""
        if vix >= VIX_ELEVATED_FLOOR:
            return False  # too much vol for exhaustion
        signals = []
        if vix_vel < -0.3 and vix > VIX_LOW_CEILING:
            signals.append("VIX declining during trend")
        if vol_ratio < 0.8:
            signals.append("Volume fading")
        if abs(spot_pct) < 0.2 and self._current_regime == "TRENDING":
            signals.append("Price stalling")
        # Check if VIX has been declining while staying above low threshold
        if len(self._vix_history) >= 4:
            vix_slope = self._vix_history[-1]["vix"] - self._vix_history[-4]["vix"]
            if vix_slope < -1.5:
                signals.append("VIX in sustained decline")
        return len(signals) >= 3

    # ── Helpers ──

    def _classify_regime(self, vix: float) -> str:
        if vix >= VIX_CRISIS_FLOOR:
            return "CRISIS"
        elif vix >= VIX_ELEVATED_FLOOR:
            return "ELEVATED"
        elif vix <= VIX_LOW_CEILING:
            return "LOW_VOL"
        return "NORMAL"

    def _classify_term(self, vix: float, vix9d: float) -> str:
        if vix9d <= 0:
            return "unknown"
        diff = vix9d - vix
        if diff >= TERM_INVERSION_THRESHOLD:
            return "inverted"
        elif diff <= TERM_STEEP_THRESHOLD:
            return "contango"
        return "flat"

    def _build_transition(
        self, transition_type: str, from_regime: str, to_regime: str,
        vix: float, vix_vel: float, term: str, gex: str,
    ) -> RegimeTransition:
        """Build a transition object with confidence and action."""
        # Confidence based on signal strength
        confidence = 0.5  # base

        if transition_type == "CRISIS_DEVELOPING":
            if vix_vel >= VIX_SPIKE_THRESHOLD:
                confidence += 0.2
            if term == "inverted":
                confidence += 0.15
            if gex == "negative":
                confidence += 0.15
            signals = []
            if vix_vel >= VIX_ACCEL_THRESHOLD:
                signals.append(f"VIX accelerating ({vix_vel:+.1f}/obs)")
            if term == "inverted":
                signals.append("Term structure inverted (near-term fear spiking)")
            if gex == "negative":
                signals.append("GEX negative (dealers short gamma — moves accelerate)")
            action = ("🔴 PUT POSITIONING RECOMMENDED\n"
                     "Long puts favored. Delta + vega both benefit from continued decline.\n"
                     "Reduce or hedge long equity exposure.")
            is_snapback = False

        elif transition_type == "NORMALIZING":
            if vix_vel <= VIX_DECEL_THRESHOLD * 2:
                confidence += 0.15
            if gex == "positive":
                confidence += 0.2
            if term in ("flat", "contango"):
                confidence += 0.15
            signals = []
            if vix_vel < 0:
                signals.append(f"VIX declining ({vix_vel:+.1f}/obs)")
            if gex == "positive":
                signals.append("GEX flipped positive (dealers stabilizing)")
            if term != "inverted":
                signals.append("Term structure re-steepening")
            action = ("🟢 SNAP-BACK CALLS VALID\n"
                     "Failed breakdown squeeze plays unlocked.\n"
                     "Delta gain should outpace vega headwind on violent bounce.\n"
                     "Reduce put exposure, consider call spreads or long calls.")
            is_snapback = True

        elif transition_type == "BREAKOUT":
            signals = [
                f"VIX rising from low base ({vix:.1f})",
                "Volume expanding on directional move",
            ]
            action = ("🟡 BREAKOUT POSITIONING\n"
                     "Long calls at cheap premium — vol expansion + delta both work.\n"
                     "GEX negative above gamma flip = acceleration zone.")
            is_snapback = False

        elif transition_type == "EXHAUSTION":
            signals = [
                f"VIX declining during trend ({vix:.1f})",
                "Volume fading on continuation",
            ]
            action = ("🟠 FADE SETUPS DEVELOPING\n"
                     "Mean reversion probability increasing.\n"
                     "Reduce trend-following size. Watch for failed extensions.")
            is_snapback = False

        else:
            signals = []
            action = "Monitor"
            is_snapback = False

        confidence = min(confidence, 1.0)

        return RegimeTransition(
            transition_type=transition_type,
            from_regime=from_regime,
            to_regime=to_regime,
            confidence=confidence,
            signals=signals,
            action=action,
            is_snapback=is_snapback,
            detected_at=time.time(),
            vix=vix,
            vix_velocity=vix_vel,
            term_structure=term,
            gex_sign=gex,
        )

    # ── Public API ──

    @property
    def current_transition(self) -> Optional[RegimeTransition]:
        """Most recent active transition, if still fresh (< 2 hours old)."""
        with self._lock:
            if self._current_transition and self._current_transition.age_minutes < 120:
                return self._current_transition
            return None

    @property
    def current_regime(self) -> str:
        with self._lock:
            return self._current_regime

    @property
    def is_snapback_active(self) -> bool:
        """True if a NORMALIZING transition is active — unlocks long calls."""
        t = self.current_transition
        return t is not None and t.is_snapback

    @property
    def is_crisis_developing(self) -> bool:
        """True if a CRISIS_DEVELOPING transition is active."""
        t = self.current_transition
        return t is not None and t.transition_type == "CRISIS_DEVELOPING"

    def get_flags(self) -> dict:
        """Flags consumed by scanner and swing engine."""
        t = self.current_transition
        return {
            "regime": self.current_regime,
            "transition_active": t is not None,
            "transition_type": t.transition_type if t else None,
            "is_snapback": self.is_snapback_active,
            "is_crisis_developing": self.is_crisis_developing,
            "transition_confidence": t.confidence if t else 0,
            "vix_velocity": (self._vix_history[-1]["vix"] - self._vix_history[-2]["vix"]
                           if len(self._vix_history) >= 2 else 0),
        }

    def get_status(self) -> dict:
        """Full status for /regime endpoint."""
        with self._lock:
            return {
                "current_regime": self._current_regime,
                "observations": len(self._vix_history),
                "latest_vix": self._vix_history[-1]["vix"] if self._vix_history else None,
                "vix_velocity": (self._vix_history[-1]["vix"] - self._vix_history[-2]["vix"]
                               if len(self._vix_history) >= 2 else 0),
                "current_transition": {
                    "type": self._current_transition.transition_type,
                    "confidence": self._current_transition.confidence,
                    "age_minutes": self._current_transition.age_minutes,
                    "is_snapback": self._current_transition.is_snapback,
                    "action": self._current_transition.action,
                } if self._current_transition else None,
                "transition_history_count": len(self._transition_history),
                "confirmation_counters": {
                    "crisis_developing": self._crisis_developing_count,
                    "normalizing": self._normalizing_count,
                    "breakout": self._breakout_count,
                    "exhaustion": self._exhaustion_count,
                },
            }
