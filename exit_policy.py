# exit_policy.py
# ═══════════════════════════════════════════════════════════════════
# Exit Policy Engine
#
# Replaces fixed exit rules with regime-aware, time-decayed policies.
# Selected at entry based on setup score + regime + DTE.
#
# Policies:
#   TREND_CONTINUATION  — GEX-, confirmed break, let it run
#   MEAN_REVERSION      — GEX+, failed move, monetize faster
#   SCALP               — low score or late-day, tight management
#
# Features:
#   - Regime-aware trail widths
#   - Time-decay urgency curve
#   - Partial scale sizing tied to setup quality
#   - GEX flip mid-trade recalibration
#   - MFE-based giveback limits
# ═══════════════════════════════════════════════════════════════════

import logging
import time as _time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ── Time-of-Day Urgency (minutes since 8:30 CT open) ──
# Returns urgency multiplier 1.0-3.0 that tightens everything late
def _time_urgency(minutes_since_open: int = 0, is_0dte: bool = True) -> float:
    """
    Urgency multiplier based on time of day.
    Higher = tighter stops, faster scales, less patience.
    """
    if not is_0dte:
        return 1.0  # multi-day: no intraday decay pressure
    if minutes_since_open < 120:       # before 10:30
        return 1.0
    elif minutes_since_open < 210:     # 10:30–12:00
        return 1.2
    elif minutes_since_open < 300:     # 12:00–1:30
        return 1.5
    elif minutes_since_open < 345:     # 1:30–2:15
        return 2.0
    else:                              # after 2:15
        return 3.0


def _minutes_since_open() -> int:
    """Current minutes since 8:30 AM CT."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        now = datetime.now(ZoneInfo("America/Chicago"))
        return now.hour * 60 + now.minute - 510  # 510 = 8:30 AM
    except Exception:
        return 180  # assume midday if we can't compute


@dataclass
class ExitPolicy:
    """
    Immutable exit policy selected at trade entry.
    Drives all management decisions for the trade's lifetime.
    """
    name: str = "DEFAULT"
    # Trail parameters
    trail_pct_of_profit: float = 0.45   # trail at 45% of open profit
    trail_min_profit_pct: float = 0.15  # don't trail until 0.15% in profit
    # Scale parameters
    scale_fraction: str = "1/2"         # how much at T1
    scale_at_target: bool = True
    # Giveback limits
    max_giveback_pct: float = 0.40      # max % of MFE to give back
    # Extension / exhaustion
    exhaustion_decel_mult: float = 0.4  # momentum decel = exhaustion
    # Regime context
    gex_at_entry: str = "positive"
    setup_score: int = 3
    is_0dte: bool = True
    # Time urgency
    urgency: float = 1.0

    def to_config(self) -> dict:
        """Serialize to dict for persistence on ActiveTrade."""
        return {
            "name": self.name, "trail_pct_of_profit": self.trail_pct_of_profit,
            "trail_min_profit_pct": self.trail_min_profit_pct,
            "scale_fraction": self.scale_fraction, "scale_at_target": self.scale_at_target,
            "max_giveback_pct": self.max_giveback_pct,
            "exhaustion_decel_mult": self.exhaustion_decel_mult,
            "gex_at_entry": self.gex_at_entry, "setup_score": self.setup_score,
            "is_0dte": self.is_0dte,
        }

    @classmethod
    def from_config(cls, cfg: dict) -> 'ExitPolicy':
        """Reconstruct from persisted config dict."""
        if not cfg:
            return cls()
        return cls(
            name=cfg.get("name", "DEFAULT"),
            trail_pct_of_profit=cfg.get("trail_pct_of_profit", 0.45),
            trail_min_profit_pct=cfg.get("trail_min_profit_pct", 0.15),
            scale_fraction=cfg.get("scale_fraction", "1/2"),
            scale_at_target=cfg.get("scale_at_target", True),
            max_giveback_pct=cfg.get("max_giveback_pct", 0.40),
            exhaustion_decel_mult=cfg.get("exhaustion_decel_mult", 0.4),
            gex_at_entry=cfg.get("gex_at_entry", "positive"),
            setup_score=cfg.get("setup_score", 3),
            is_0dte=cfg.get("is_0dte", True),
            urgency=_time_urgency(_minutes_since_open(), cfg.get("is_0dte", True)),
        )

    def compute_trail_stop(self, entry_price: float, current_price: float,
                           direction: str, current_stop: float = None) -> float:
        """Compute trail stop accounting for urgency and regime."""
        if direction == "LONG":
            profit = current_price - entry_price
        else:
            profit = entry_price - current_price
        if profit <= entry_price * (self.trail_min_profit_pct / 100):
            return current_stop or (entry_price * (1 - 0.005) if direction == "LONG" else entry_price * (1 + 0.005))
        # Effective trail width narrows with urgency
        eff_pct = self.trail_pct_of_profit / self.urgency
        trail_width = profit * eff_pct
        if direction == "LONG":
            new_stop = current_price - trail_width
            new_stop = max(new_stop, entry_price)  # never below BE
        else:
            new_stop = current_price + trail_width
            new_stop = min(new_stop, entry_price)  # never above BE
        # Only tighten, never loosen
        if current_stop is not None:
            if direction == "LONG":
                new_stop = max(new_stop, current_stop)
            else:
                new_stop = min(new_stop, current_stop)
        return round(new_stop, 2)

    def should_force_exit_giveback(self, entry_price: float, current_price: float,
                                    max_favorable: float, direction: str) -> bool:
        """Has the trade given back too much of its best move?"""
        if max_favorable <= 0:
            return False
        if direction == "LONG":
            current_profit = current_price - entry_price
        else:
            current_profit = entry_price - current_price
        if current_profit < 0:
            return True  # already a loss after having been profitable
        giveback = 1.0 - (current_profit / max_favorable)
        # Tighten giveback limit with urgency
        effective_max = self.max_giveback_pct / self.urgency
        return giveback > effective_max

    def should_scale(self, price: float, target: float, direction: str) -> bool:
        """Has first target been reached?"""
        if not self.scale_at_target:
            return False
        if direction == "LONG":
            return price >= target
        return price <= target

    def get_urgency_label(self) -> str:
        if self.urgency >= 3.0:
            return "CRITICAL — close everything"
        elif self.urgency >= 2.0:
            return "HIGH — monetize winners, cut laggards"
        elif self.urgency >= 1.5:
            return "MODERATE — profit-taking bias"
        elif self.urgency >= 1.2:
            return "MILD — normal but tightening"
        return "NORMAL"

    def recalibrate_for_gex_flip(self, new_gex_sign: str) -> 'ExitPolicy':
        """
        Return a new policy adjusted for a mid-trade GEX regime change.
        Does NOT modify the original.
        """
        if new_gex_sign == self.gex_at_entry:
            return self  # no change
        # GEX flipped — adjust policy
        new = ExitPolicy(
            name=f"{self.name}_FLIPPED",
            trail_pct_of_profit=self.trail_pct_of_profit,
            trail_min_profit_pct=self.trail_min_profit_pct,
            scale_fraction=self.scale_fraction,
            scale_at_target=self.scale_at_target,
            max_giveback_pct=self.max_giveback_pct,
            exhaustion_decel_mult=self.exhaustion_decel_mult,
            gex_at_entry=self.gex_at_entry,
            setup_score=self.setup_score,
            is_0dte=self.is_0dte,
            urgency=self.urgency,
        )
        if self.gex_at_entry == "negative" and new_gex_sign == "positive":
            # Entered in trend, now pinning — tighten
            new.trail_pct_of_profit = min(self.trail_pct_of_profit, 0.30)
            new.max_giveback_pct = min(self.max_giveback_pct, 0.30)
            new.name = f"{self.name}_TREND→PIN"
            log.info(f"ExitPolicy: GEX flip neg→pos, tightening trail to {new.trail_pct_of_profit:.0%}")
        elif self.gex_at_entry == "positive" and new_gex_sign == "negative":
            # Entered in pin, now trending — widen
            new.trail_pct_of_profit = max(self.trail_pct_of_profit, 0.55)
            new.max_giveback_pct = max(self.max_giveback_pct, 0.50)
            new.name = f"{self.name}_PIN→TREND"
            log.info(f"ExitPolicy: GEX flip pos→neg, widening trail to {new.trail_pct_of_profit:.0%}")
        return new


# ── Policy Factory ──

def select_exit_policy(setup_score: int, gex_sign: str, setup_type: str,
                       is_0dte: bool = True, vix: float = 20.0,
                       time_phase: str = "MORNING") -> ExitPolicy:
    """
    Select the appropriate exit policy based on entry conditions.
    Called once at trade creation.
    """
    urgency = _time_urgency(_minutes_since_open(), is_0dte)

    # ── TREND_CONTINUATION: GEX-, confirmed break, high score ──
    if gex_sign == "negative" and setup_type == "BREAK" and setup_score >= 4:
        return ExitPolicy(
            name="TREND_CONTINUATION",
            trail_pct_of_profit=0.55,    # wide: let it run
            trail_min_profit_pct=0.15,
            scale_fraction="1/3" if setup_score >= 5 else "1/2",
            scale_at_target=True,
            max_giveback_pct=0.50,
            exhaustion_decel_mult=0.35,
            gex_at_entry=gex_sign,
            setup_score=setup_score,
            is_0dte=is_0dte,
            urgency=urgency,
        )

    # ── MEAN_REVERSION: GEX+, failed move, squeeze ──
    if gex_sign == "positive" and setup_type in ("FAILED", "RETEST"):
        return ExitPolicy(
            name="MEAN_REVERSION",
            trail_pct_of_profit=0.30,    # tight: reversion caps move
            trail_min_profit_pct=0.10,
            scale_fraction="2/3",
            scale_at_target=True,
            max_giveback_pct=0.30,
            exhaustion_decel_mult=0.45,
            gex_at_entry=gex_sign,
            setup_score=setup_score,
            is_0dte=is_0dte,
            urgency=urgency,
        )

    # ── GEX- SQUEEZE: failed move in negative gamma ──
    if gex_sign == "negative" and setup_type == "FAILED":
        return ExitPolicy(
            name="GEX_NEG_SQUEEZE",
            trail_pct_of_profit=0.50,    # wide: squeeze can run hard
            trail_min_profit_pct=0.12,
            scale_fraction="1/2",
            scale_at_target=True,
            max_giveback_pct=0.45,
            exhaustion_decel_mult=0.35,
            gex_at_entry=gex_sign,
            setup_score=setup_score,
            is_0dte=is_0dte,
            urgency=urgency,
        )

    # ── GEX+ BREAK: lower conviction continuation ──
    if gex_sign == "positive" and setup_type == "BREAK":
        return ExitPolicy(
            name="GEX_POS_BREAK",
            trail_pct_of_profit=0.25,    # very tight: mean reversion likely
            trail_min_profit_pct=0.08,
            scale_fraction="2/3",
            scale_at_target=True,
            max_giveback_pct=0.25,
            exhaustion_decel_mult=0.50,
            gex_at_entry=gex_sign,
            setup_score=setup_score,
            is_0dte=is_0dte,
            urgency=urgency,
        )

    # ── SCALP: low score, late day, or unclear regime ──
    if setup_score <= 3 or time_phase in ("POWER_HOUR", "CLOSE"):
        return ExitPolicy(
            name="SCALP",
            trail_pct_of_profit=0.25,
            trail_min_profit_pct=0.08,
            scale_fraction="2/3",
            scale_at_target=True,
            max_giveback_pct=0.25,
            exhaustion_decel_mult=0.50,
            gex_at_entry=gex_sign,
            setup_score=setup_score,
            is_0dte=is_0dte,
            urgency=max(urgency, 1.5),  # always at least moderate urgency
        )

    # ── DEFAULT: moderate management ──
    return ExitPolicy(
        name="DEFAULT",
        trail_pct_of_profit=0.40,
        trail_min_profit_pct=0.12,
        scale_fraction="1/2",
        scale_at_target=True,
        max_giveback_pct=0.40,
        exhaustion_decel_mult=0.40,
        gex_at_entry=gex_sign,
        setup_score=setup_score,
        is_0dte=is_0dte,
        urgency=urgency,
    )


@dataclass
class ExitSignal:
    """Output from the exit policy evaluation."""
    action: str = "HOLD"        # HOLD / SCALE / TRAIL / REDUCE / EXIT / INVALIDATE
    reason: str = ""
    new_stop: Optional[float] = None
    scale_fraction: str = ""
    urgency_label: str = ""
    pnl_pct: float = 0.0
    mfe_pct: float = 0.0


def evaluate_exit(policy: ExitPolicy, trade, bar, bars_recent: list,
                  current_gex: str = None) -> ExitSignal:
    """
    Evaluate exit conditions for an active trade using its policy.
    Returns an ExitSignal describing what to do.

    Args:
        policy: The trade's ExitPolicy
        trade: ActiveTrade object
        bar: Current Bar (latest 5m close)
        bars_recent: Last 4-5 bars for momentum check
        current_gex: Current GEX sign (for mid-trade flip detection)
    """
    price = bar.close
    signal = ExitSignal()
    signal.urgency_label = policy.get_urgency_label()

    # P&L
    if trade.direction == "LONG":
        signal.pnl_pct = (price - trade.entry_price) / trade.entry_price * 100
        profit = price - trade.entry_price
    else:
        signal.pnl_pct = (trade.entry_price - price) / trade.entry_price * 100
        profit = trade.entry_price - price

    signal.mfe_pct = trade.max_favorable / trade.entry_price * 100 if trade.entry_price > 0 else 0

    # Update urgency based on current time
    policy.urgency = _time_urgency(_minutes_since_open(), policy.is_0dte)

    # ── 1. GEX flip detection ──
    if current_gex and current_gex != policy.gex_at_entry:
        adjusted = policy.recalibrate_for_gex_flip(current_gex)
        if adjusted.name != policy.name:
            signal.action = "REDUCE"
            signal.reason = f"GEX flipped {policy.gex_at_entry}→{current_gex}. Adjusting management."
            signal.new_stop = adjusted.compute_trail_stop(
                trade.entry_price, price, trade.direction, trade.trail_stop)
            return signal

    # ── 2. Invalidation — bar closed through stop ──
    stop_ref = trade.trail_stop if trade.trail_stop else trade.stop_level
    if trade.direction == "LONG" and bar.close < stop_ref:
        signal.action = "INVALIDATE"
        signal.reason = f"Bar closed below stop ${stop_ref:.2f}"
        signal.pnl_pct = signal.pnl_pct
        return signal
    if trade.direction == "SHORT" and bar.close > stop_ref:
        signal.action = "INVALIDATE"
        signal.reason = f"Bar closed above stop ${stop_ref:.2f}"
        return signal

    # ── 3. Giveback limit ──
    if trade.max_favorable > 0 and policy.should_force_exit_giveback(
            trade.entry_price, price, trade.max_favorable, trade.direction):
        signal.action = "EXIT"
        signal.reason = f"Giveback exceeded {policy.max_giveback_pct:.0%} of MFE"
        return signal

    # ── 4. Scale at target ──
    if trade.status == "OPEN" and trade.targets:
        first_target = trade.targets[0]
        if policy.should_scale(price, first_target, trade.direction):
            signal.action = "SCALE"
            signal.reason = f"Target ${first_target:.2f} reached"
            signal.scale_fraction = policy.scale_fraction
            signal.new_stop = trade.entry_price  # move to breakeven
            return signal

    # ── 5. Trail stop update ──
    if trade.status in ("SCALED", "TRAILED"):
        new_stop = policy.compute_trail_stop(
            trade.entry_price, price, trade.direction, trade.trail_stop)
        if trade.trail_stop is None or (
            (trade.direction == "LONG" and new_stop > trade.trail_stop) or
            (trade.direction == "SHORT" and new_stop < trade.trail_stop)
        ):
            signal.action = "TRAIL"
            signal.reason = f"Trail tightened → ${new_stop:.2f}"
            signal.new_stop = new_stop
            return signal

    # ── 6. Momentum exhaustion ──
    if trade.status in ("SCALED", "TRAILED") and len(bars_recent) >= 3:
        diffs = [bars_recent[i].close - bars_recent[i - 1].close for i in range(1, len(bars_recent))]
        avg_d = sum(diffs) / len(diffs) if diffs else 0
        last_d = diffs[-1] if diffs else 0
        decel = False
        if trade.direction == "LONG" and avg_d > 0:
            decel = abs(last_d) < abs(avg_d) * policy.exhaustion_decel_mult
        elif trade.direction == "SHORT" and avg_d < 0:
            decel = abs(last_d) < abs(avg_d) * policy.exhaustion_decel_mult
        # Also check reversal
        reversal = (trade.direction == "LONG" and last_d < 0) or (trade.direction == "SHORT" and last_d > 0)
        if (decel or reversal) and trade.max_favorable > trade.entry_price * 0.003:
            signal.action = "EXIT"
            signal.reason = "Momentum exhaustion after scaling"
            return signal

    # ── 7. Time urgency forced exit ──
    if policy.urgency >= 3.0 and profit > 0:
        signal.action = "EXIT"
        signal.reason = "Session closing — monetize remaining"
        return signal

    if policy.urgency >= 2.0 and profit <= 0 and trade.max_favorable > trade.entry_price * 0.002:
        signal.action = "EXIT"
        signal.reason = "Late session + losing after having been profitable — cut"
        return signal

    signal.action = "HOLD"
    signal.reason = "No exit trigger"
    return signal
