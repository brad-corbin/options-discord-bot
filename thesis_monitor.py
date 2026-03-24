# thesis_monitor.py
# v2.0 Thesis Monitor — Bar-Aware + Confluence Scoring + Policy Engine
# v1.5 base: entry detection, exit monitor, ActiveTrade, persistence
# v2.0 adds: OHLCV bars, level registry, entry validator, exit policies
import logging, threading, time, json, uuid
from datetime import datetime, timezone
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass, field, asdict

from bar_state import BarStateManager, Bar, ExecutionState
from level_registry import LevelRegistry, Level
from entry_validator import EntryValidator, ValidationResult
from exit_policy import ExitPolicy, ExitSignal, select_exit_policy, evaluate_exit

log = logging.getLogger(__name__)

MONITOR_POLL_INTERVAL_SEC = 300
MONITOR_POLL_INTERVAL_FAST_SEC = 60
MONITOR_FAST_POLL_TICKERS = ["SPY"]
MONITOR_ALERT_COOLDOWN_SEC = 600
MONITOR_ZONE_CLUSTER_PCT   = 0.08   # break attempts / level alerts within 0.08% of each other are treated as the same zone
MONITOR_MAX_BREAK_AGE_SEC = 900
MONITOR_MOMENTUM_LOOKBACK = 5
MONITOR_RECLAIM_THRESHOLD_PCT = 0.015
MONITOR_STALL_THRESHOLD_PCT = 0.008
MONITOR_CONFIRM_BUFFER_PCT = 0.08
MONITOR_EXTENSION_LIMIT_PCT = 0.25
MONITOR_MIN_HOLD_POLLS_AFTER_RECLAIM = 1
MONITOR_RETEST_TOLERANCE_PCT = 0.04
MONITOR_DEFAULT_TICKERS = ["SPY", "QQQ"]

# ── Hairline stop gate ───────────────────────────────────────────────────────
# When the distance between entry price and stop level is below this threshold
# (expressed as % of price), the setup is considered a hairline stop.
# Hairline setups DO still alert — but they require MONITOR_HAIRLINE_EXTRA_HOLDS
# additional confirmation polls of holding structure before the trade card fires.
# This prevents auto-entering stops that are at noise level (e.g. $0.10 on SPY).
MONITOR_HAIRLINE_STOP_PCT    = 0.03   # 0.03% = ~$0.20 on SPY $660, ~$0.18 on QQQ $590
MONITOR_HAIRLINE_EXTRA_HOLDS = 2      # extra candles of structure hold required
INTRADAY_MIN_TOUCHES = 3
INTRADAY_ZONE_TOLERANCE_PCT = 0.04
INTRADAY_MIN_PRICES_FOR_LEVELS = 6
INTRADAY_SHARP_MOVE_THRESHOLD = 0.12
INTRADAY_CONSOLIDATION_CANDLES = 4
INTRADAY_CONSOLIDATION_RANGE_PCT = 0.06

# ── Exit Monitor Constants ──
EXIT_INVALIDATE_BUFFER_PCT = 0.06     # price must reclaim past stop by this % to invalidate
EXIT_MOMENTUM_FADE_LOOKBACK = 4       # polls to check momentum against position
EXIT_SCALE_PROXIMITY_PCT = 0.08       # 0.08% — how close to target counts as "reached"
EXIT_TRAIL_ACCELERATION_MULT = 1.8    # momentum must be 1.8x avg to trail
EXIT_EXHAUSTION_DECEL_MULT = 0.4      # momentum below 0.4x avg after scaling = exhaustion
EXIT_MAX_TRADE_AGE_SEC = 14400        # 4 hours — auto-expire stale trades
EXIT_ALERT_COOLDOWN_SEC = 300         # 5 min between same exit alert type per trade

@dataclass
class ThesisLevels:
    gamma_flip: Optional[float] = None; local_resistance: Optional[float] = None
    local_support: Optional[float] = None; call_wall: Optional[float] = None
    put_wall: Optional[float] = None; gamma_wall: Optional[float] = None
    pin_zone_low: Optional[float] = None; pin_zone_high: Optional[float] = None
    micro_trigger_up: Optional[float] = None; micro_trigger_down: Optional[float] = None
    range_break_up: Optional[float] = None; range_break_down: Optional[float] = None
    pivot: Optional[float] = None; r1: Optional[float] = None; s1: Optional[float] = None
    fib_support: Optional[float] = None; fib_resistance: Optional[float] = None
    vpoc: Optional[float] = None; max_pain: Optional[float] = None
    em_high: Optional[float] = None; em_low: Optional[float] = None
    em_2sd_high: Optional[float] = None; em_2sd_low: Optional[float] = None

@dataclass
class ThesisContext:
    ticker: str = ""; bias: str = "NEUTRAL"; bias_score: int = 0
    gex_sign: str = "positive"; gex_value: float = 0.0
    dex_value: float = 0.0; vanna_value: float = 0.0; charm_value: float = 0.0
    regime: str = "UNKNOWN"; volatility_regime: str = "NORMAL"
    vix: float = 20.0; iv: float = 0.20
    prior_day_close: Optional[float] = None; prior_day_context: str = "NORMAL"
    session_label: str = ""; levels: ThesisLevels = field(default_factory=ThesisLevels)
    created_at: str = ""; spot_at_creation: float = 0.0
    # ── ATM option snapshot from pre-market chain ────────────────────────────
    # Populated once at em card time. Used to approximate the 20% premium stop
    # during live monitoring without needing live option quote fetches.
    atm_call_delta:   float = 0.0   # delta of the nearest ATM call at em time
    atm_call_premium: float = 0.0   # mid price of the nearest ATM call ($)
    atm_put_delta:    float = 0.0   # delta of the nearest ATM put at em time
    atm_put_premium:  float = 0.0   # mid price of the nearest ATM put ($)

@dataclass
class BreakAttempt:
    level: float; level_name: str; direction: str; break_price: float; break_time: float
    detected_as_failed: bool = False; detected_as_confirmed: bool = False; break_bar_index: int = -1
    reclaim_seen: bool = False; reclaim_price: Optional[float] = None
    reclaim_time: float = 0.0; reclaim_holds: int = 0
    retest_armed: bool = False; retest_fired: bool = False
    hairline_holds: int = 0   # extra confirmation polls accumulated on hairline setups

@dataclass
class IntradayLevel:
    price: float; kind: str; source: str; touches: int = 0
    first_seen_ts: float = 0.0; last_touched_ts: float = 0.0; active: bool = True

@dataclass
class ActiveTrade:
    ticker: str
    direction: str  # LONG / SHORT
    entry_type: str  # BREAK / FAILED / RETEST
    entry_price: float
    stop_level: float
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    targets: list = field(default_factory=list)  # [float] — next S/R levels
    status: str = "OPEN"  # OPEN / SCALED / TRAILED / CLOSED / INVALIDATED
    entry_time: float = 0.0       # monotonic — in-process only
    entry_epoch: float = 0.0      # time.time() — survives restart
    entry_time_str: str = ""
    level_name: str = ""  # what level triggered the entry
    gex_at_entry: str = "positive"
    regime: str = ""
    time_phase: str = ""
    prior_day_context: str = "NORMAL"
    bias_at_entry: str = ""
    volatility_regime: str = ""
    trade_type_label: str = ""  # "Naked puts", "Call debit spread", etc.
    # v2.0: setup quality + exit policy
    setup_score: int = 0          # 1-5 from EntryValidator
    setup_label: str = ""         # "HIGH CONVICTION", etc.
    exit_policy_name: str = ""    # "TREND_CONTINUATION", etc.
    scale_advice: str = ""        # "1/3 at T1", etc.
    level_tier: str = "C"         # A/B/C from LevelRegistry
    validation_summary: str = ""  # gate summary from validator
    policy_config: dict = field(default_factory=dict)  # persisted policy params for restart
    # tracking
    max_favorable: float = 0.0  # max move in trade direction from entry
    min_favorable: float = 0.0  # worst adverse excursion from entry
    scaled_at_price: Optional[float] = None
    trail_stop: Optional[float] = None
    close_price: Optional[float] = None
    close_reason: str = ""
    close_time: float = 0.0      # monotonic — in-process only
    close_epoch: float = 0.0     # time.time() — survives restart
    exit_alert_history: dict = field(default_factory=dict)  # cooldown tracking
    last_exit_bar_ts: float = 0.0  # timestamp of last bar used for policy eval
    # ── Option premium tracking (delta approximation) ────────────────────────
    # Set at entry if chain data is available from the pre-market em fetch.
    # Used for 20% premium stop without live quote fetching.
    entry_premium: float = 0.0   # estimated option mid price at entry (dollars)
    entry_delta: float = 0.0     # option delta at entry (0–1 for calls, 0–1 for puts)

@dataclass
class MonitorState:
    status: str = "FRESH"; momentum: str = "NEUTRAL"
    above_gamma_flip: Optional[bool] = None
    price_history: list = field(default_factory=list)
    break_attempts: list = field(default_factory=list)
    failed_moves: list = field(default_factory=list)
    confirmed_breaks: list = field(default_factory=list)
    active_trend_direction: Optional[str] = None
    alert_history: dict = field(default_factory=dict)
    last_guidance_ts: float = 0.0; check_count: int = 0; prior_day_applied: bool = False
    intraday_levels: list = field(default_factory=list)
    session_high: Optional[float] = None; session_low: Optional[float] = None
    session_high_time: str = ""; session_low_time: str = ""
    active_trades: list = field(default_factory=list)  # List[ActiveTrade]

def _get_time_phase_ct() -> dict:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        try:
            import pytz; now = datetime.now(pytz.timezone("America/Chicago"))
        except Exception:
            now = datetime.utcnow()
    mins = now.hour * 60 + now.minute
    if mins < 510: return {"phase": "PRE_MARKET", "label": "Pre-Market", "favor": "wait", "note": "Wait for open to confirm."}
    if mins < 540: return {"phase": "OPEN", "label": "Open (first 30 min)", "favor": "caution", "note": "Expansion window — let levels prove."}
    if mins < 600: return {"phase": "MORNING", "label": "Morning Session", "favor": "breakout", "note": "Breakouts more reliable. Watch for trend."}
    if mins < 720: return {"phase": "MIDDAY", "label": "Midday", "favor": "failed_move", "note": "Chop zone — failed moves are best."}
    if mins < 810: return {"phase": "AFTERNOON", "label": "Afternoon", "favor": "trend_resumption", "note": "Trend resumption or reversal."}
    if mins < 870: return {"phase": "POWER_HOUR", "label": "Power Hour", "favor": "pin_or_expand", "note": "GEX+ = pinning. GEX- = expansion."}
    if mins < 915: return {"phase": "CLOSE", "label": "Into Close", "favor": "pin", "note": "Favor pin / mean reversion. Reduce size."}
    return {"phase": "AFTER_HOURS", "label": "After Hours", "favor": "wait", "note": "Session over."}

def _dte_guidance(phase: str) -> str:
    """Return explicit DTE instruction based on session phase."""
    if phase in ("POWER_HOUR", "CLOSE"):
        from datetime import date, timedelta
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/Chicago")).date()
        except Exception:
            today = datetime.utcnow().date()
        # Next trading day (skip Saturday=5, Sunday=6)
        next_day = today + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return f"📅 DTE: Use TOMORROW's expiry ({next_day.strftime('%m/%d')}) — 1DTE. Session ends soon. DO NOT buy 0DTE."
    return "📅 DTE: Use TODAY's expiry (0DTE)."


# ── Tickers that use the spread ladder ──────────────────────────────────────
# All others fall back to simple ATM+2 naked suggestion.
_SPREAD_TICKERS = {"SPY", "QQQ"}

# Setup type constants — passed into _contract_suggestion
_SETUP_FAILED_BREAKDOWN    = "failed_breakdown"      # reclaim after false break down → LONG
_SETUP_FAILED_BREAKOUT     = "failed_breakout"       # lost + held after false break up → SHORT
_SETUP_BREAKOUT_FOLLOW     = "breakout_followthrough" # true breakout with continuation → LONG
_SETUP_BREAKDOWN_FOLLOW    = "breakdown_followthrough" # true breakdown with continuation → SHORT
_SETUP_RETEST_LONG         = "retest_long"            # retest of confirmed/failed level → LONG
_SETUP_RETEST_SHORT        = "retest_short"           # retest of confirmed/failed level → SHORT


def _pick_instrument(
    setup_type: str,
    direction: str,
    gex: str,
    vol_regime: str,
    bias_score: int,
    momentum: str,
    phase: str,
    d1: Optional[float],
) -> str:
    """Decide between naked option and debit spread.

    Returns one of: "naked_call", "naked_put", "call_spread", "put_spread"

    Momentum vocabulary matches _evaluate_momentum output exactly:
      Strong:  ACCELERATING_UP, ACCELERATING_DOWN
      Drifting: DRIFTING_UP, DRIFTING_DOWN   (treated as moderate — neither strong nor weak)
      Weak:    LOSING_UPSIDE_MOMENTUM, LOSING_DOWNSIDE_MOMENTUM, STALLING
    """
    is_continuation = setup_type in (_SETUP_BREAKOUT_FOLLOW, _SETUP_BREAKDOWN_FOLLOW)
    is_failed_move  = setup_type in (_SETUP_FAILED_BREAKDOWN, _SETUP_FAILED_BREAKOUT)
    is_retest       = setup_type in (_SETUP_RETEST_LONG, _SETUP_RETEST_SHORT)
    is_overnight    = phase in ("POWER_HOUR", "CLOSE")
    is_crisis_vol   = vol_regime in ("CRISIS", "ELEVATED")
    has_room        = d1 is not None and d1 > 2.0
    # Strong = confirmed acceleration in either direction
    momentum_strong = momentum in ("ACCELERATING_UP", "ACCELERATING_DOWN")
    # Weak = fading, stalling, or lost direction
    momentum_weak   = momentum in ("LOSING_UPSIDE_MOMENTUM", "LOSING_DOWNSIDE_MOMENTUM", "STALLING")
    # Drifting = mild directional bias, treated as neutral for instrument selection

    # ── Always spread ────────────────────────────────────────────────────────
    if gex == "positive":
        return "call_spread" if direction == "LONG" else "put_spread"

    if momentum_weak and not is_overnight:
        return "call_spread" if direction == "LONG" else "put_spread"

    if not has_room and is_failed_move:
        return "call_spread" if direction == "LONG" else "put_spread"

    if is_crisis_vol and abs(bias_score) <= 2:
        return "call_spread" if direction == "LONG" else "put_spread"

    # ── Naked for overnight (Power Hour / Close → 1DTE) ─────────────────────
    if is_overnight and gex == "negative" and abs(bias_score) >= 4:
        return "naked_call" if direction == "LONG" else "naked_put"

    # ── Naked for GEX- continuation with room and confirmed momentum ─────────
    if is_continuation and gex == "negative" and has_room and momentum_strong:
        return "naked_call" if direction == "LONG" else "naked_put"

    # ── Naked for GEX- failed moves with confirmed acceleration and room ─────
    if is_failed_move and gex == "negative" and momentum_strong and has_room:
        return "naked_call" if direction == "LONG" else "naked_put"

    # ── Naked for GEX- retests with confirmed acceleration ───────────────────
    if is_retest and gex == "negative" and momentum_strong:
        return "naked_call" if direction == "LONG" else "naked_put"

    # ── Default: spread ──────────────────────────────────────────────────────
    return "call_spread" if direction == "LONG" else "put_spread"


def _contract_suggestion(
    setup_type: str,
    direction: str,
    price: float,
    thesis=None,
    state=None,
    phase: str = "",
) -> str:
    """Return a formatted TRADE CARD contract block.

    Step 0  Decide instrument: naked vs debit spread (via _pick_instrument)
    Step 1  Classify setup type
    Step 2  Compute usable EM fraction — scaled by 1.5× for 1DTE phase
    Step 3  Nearest opposing level distance (d1)
    Step 4  Target move = min(d1, d3)
    Step 5  Raw width = 80% of target move  [spread path only]
    Step 6  Snap to 1-pt increments + hard caps  [spread path only]
    Step 7  Quality modifier  [spread path only]
    Step 8  Strike placement
    Step 9  Format with inline reasoning

    Tickers not in _SPREAD_TICKERS get a simple naked ATM suggestion.
    Always returns a string safe to embed in the TRADE CARD block.
    """
    ticker     = getattr(thesis, "ticker",           "").upper() if thesis else ""
    gex        = getattr(thesis, "gex_sign",         "positive") if thesis else "positive"
    vol_regime = getattr(thesis, "volatility_regime", "NORMAL")  if thesis else "NORMAL"
    bias_score = getattr(thesis, "bias_score",        0)         if thesis else 0
    momentum   = getattr(state,  "momentum",          "NEUTRAL") if state  else "NEUTRAL"

    # ── Non-spread tickers: always naked ATM ────────────────────────────────
    if ticker not in _SPREAD_TICKERS:
        atm = int(price)
        if direction == "LONG":
            return (f"💰 INSTRUMENT: Naked call\n"
                    f"🎯 STRIKE: {atm} call (ATM)\n"
                    f"📐 SIZE: min(floor($1,500 ÷ premium × 100), 20 contracts).")
        else:
            return (f"💰 INSTRUMENT: Naked put\n"
                    f"🎯 STRIKE: {atm + 1} put (ATM)\n"
                    f"📐 SIZE: min(floor($1,500 ÷ premium × 100), 20 contracts).")

    # ── Step 2: usable EM fraction by setup type ─────────────────────────────
    usable_fracs = {
        _SETUP_FAILED_BREAKDOWN:  0.30,
        _SETUP_FAILED_BREAKOUT:   0.30,
        _SETUP_RETEST_LONG:       0.35,
        _SETUP_RETEST_SHORT:      0.35,
        _SETUP_BREAKOUT_FOLLOW:   0.50,
        _SETUP_BREAKDOWN_FOLLOW:  0.50,
    }
    usable_frac = usable_fracs.get(setup_type, 0.30)

    # 1DTE EM scale: overnight expected move is ~1.5× the same-day EM.
    # Scaling prevents the system from recommending too-tight spreads on
    # Power Hour entries where the move can continue into next session.
    is_overnight = phase in ("POWER_HOUR", "CLOSE")
    if is_overnight:
        usable_frac *= 1.5

    # ── Step 2b: EM 1σ budget (d3) ──────────────────────────────────────────
    d3 = None
    if thesis and thesis.levels.em_high and thesis.levels.em_low:
        em_1sd = (thesis.levels.em_high - thesis.levels.em_low) / 2.0
        if em_1sd > 0:
            d3 = em_1sd * usable_frac

    # ── Step 3: nearest opposing level distance (d1) ─────────────────────────
    d1 = None
    if state:
        lvls = IntradayLevelTracker.get_active_levels(state, price)
        if direction == "LONG" and lvls.get("resistance"):
            d1 = lvls["resistance"].price - price
        elif direction == "SHORT" and lvls.get("support"):
            d1 = price - lvls["support"].price
    if d1 is None and thesis:
        if direction == "LONG" and thesis.levels.local_resistance:
            d1 = max(thesis.levels.local_resistance - price, 0) or None
        elif direction == "SHORT" and thesis.levels.local_support:
            d1 = max(price - thesis.levels.local_support, 0) or None

    # ── Step 0: instrument decision ──────────────────────────────────────────
    instrument = _pick_instrument(
        setup_type=setup_type, direction=direction,
        gex=gex, vol_regime=vol_regime, bias_score=bias_score,
        momentum=momentum, phase=phase, d1=d1,
    )
    is_naked = instrument in ("naked_call", "naked_put")

    # ── Step 4: target move ──────────────────────────────────────────────────
    candidates = [x for x in [d1, d3] if x is not None and x > 0]
    target_move = min(candidates) if candidates else 1.0

    atm = int(price)

    # ══════════════════════════════════════════════════════════════════════════
    # NAKED PATH
    # ══════════════════════════════════════════════════════════════════════════
    if is_naked:
        # Strike: ATM for calls, ATM+1 for puts (slight ITM for better delta)
        if direction == "LONG":
            strike       = atm
            option_label = f"{strike} call (ATM)"
            instr_label  = "Naked call"
        else:
            strike       = atm + 1
            option_label = f"{strike} put (ATM)"
            instr_label  = "Naked put"

        # Reasoning for the trader
        reasons = []
        if gex == "negative":
            reasons.append("GEX- amplifies move")
        if is_overnight:
            reasons.append("1DTE — overnight theta is slow")
        if d1 is not None and d1 > 2.0:
            reasons.append(f"{d1:.1f}pt room — spread would cap you early")
        if momentum in ("ACCELERATING_UP", "ACCELERATING_DOWN"):
            reasons.append("momentum accelerating")
        why = " | ".join(reasons) if reasons else "GEX- continuation"

        return (
            f"💰 INSTRUMENT: {instr_label}  [{why}]\n"
            f"🎯 STRIKE: {option_label}\n"
            f"📐 SIZE: min(floor($1,500 ÷ premium × 100), 20 contracts).\n"
            f"   Exit at 50% of premium paid (stop) or 100% gain (target)."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # SPREAD PATH
    # ══════════════════════════════════════════════════════════════════════════

    # ── Step 5: raw width ────────────────────────────────────────────────────
    raw_width = 0.80 * target_move

    # ── Step 6: snap + hard caps ─────────────────────────────────────────────
    if raw_width < 1.125:
        width = 1
    elif raw_width < 2.125:
        width = 2
    elif raw_width < 3.125:
        width = 3
    else:
        width = 4

    max_widths = {
        _SETUP_FAILED_BREAKDOWN:  2,
        _SETUP_FAILED_BREAKOUT:   2,
        _SETUP_RETEST_LONG:       3,
        _SETUP_RETEST_SHORT:      3,
        _SETUP_BREAKOUT_FOLLOW:   4,
        _SETUP_BREAKDOWN_FOLLOW:  4,
    }
    width = min(width, max_widths.get(setup_type, 2))

    # ── Step 7: quality modifier ─────────────────────────────────────────────
    # Uses exact vocabulary from _evaluate_momentum:
    #   Strong:   ACCELERATING_UP, ACCELERATING_DOWN
    #   Drifting: DRIFTING_UP, DRIFTING_DOWN  (no modifier — let width stand)
    #   Weak:     LOSING_UPSIDE_MOMENTUM, LOSING_DOWNSIDE_MOMENTUM, STALLING
    narrow = (
        (gex == "positive" and
         setup_type in (_SETUP_BREAKOUT_FOLLOW, _SETUP_BREAKDOWN_FOLLOW)) or
        abs(bias_score) <= 2 or
        momentum in ("LOSING_UPSIDE_MOMENTUM", "LOSING_DOWNSIDE_MOMENTUM", "STALLING") or
        (d1 is not None and d1 < 0.75)
    )
    widen = (
        setup_type in (_SETUP_BREAKOUT_FOLLOW, _SETUP_BREAKDOWN_FOLLOW) and
        momentum in ("ACCELERATING_UP", "ACCELERATING_DOWN") and
        (d1 is not None and d1 > 2.0) and
        abs(bias_score) >= 4 and
        gex == "negative"
    )
    if narrow:
        width = max(1, width - 1)
    elif widen:
        width = min(max_widths.get(setup_type, 2), width + 1)

    # ── Step 8: strike placement ─────────────────────────────────────────────
    if direction == "LONG":
        long_strike  = atm
        short_strike = atm + width
        spread_label = f"{long_strike}/{short_strike} call debit spread"
        instr_label  = "Call debit spread"
    else:
        long_strike  = atm + 1
        short_strike = long_strike - width
        spread_label = f"{long_strike}/{short_strike} put debit spread"
        instr_label  = "Put debit spread"

    # Reasoning
    basis_parts = []
    if d1 is not None:
        basis_parts.append(f"level {d1:.2f}pt away")
    if d3 is not None:
        basis_parts.append(f"EM budget {d3:.2f}pt")
    if gex == "positive":
        basis_parts.append("GEX+ bounds move")
    basis = f"  [{' | '.join(basis_parts)} → {width}-pt]" if basis_parts else ""

    return (
        f"💰 INSTRUMENT: {instr_label}{basis}\n"
        f"🎯 SPREAD: {spread_label} ({width}-pt)\n"
        f"📐 SIZE: min(floor($2,000 ÷ premium × 100), 40 contracts).\n"
        f"   Exit at 75-80% of max profit (${width * 100 - int(width * 25)} target on ${width * 100} max)."
    )


class IntradayLevelTracker:
    @staticmethod
    def update(state: MonitorState, price: float, now: float, bm=None) -> List[dict]:
        """Discover intraday S/R from 5m bar structure. Bars are the single truth source.
        No bars = deactivate stale levels only, produce no new levels."""
        events = []
        tol = price * INTRADAY_ZONE_TOLERANCE_PCT / 100

        # Deactivate stale levels regardless of bar availability
        for lvl in state.intraday_levels:
            if lvl.active and abs(price - lvl.price) > tol * 8 and (now - lvl.last_touched_ts) > 2700:
                lvl.active = False

        # No bars = no level discovery. Don't fake 5m levels from spot samples.
        if not bm or len(bm.state.bars_5m) < 2:
            return events
        if price <= 0:
            return events

        bars = bm.state.bars_5m
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        n = len(bars)

        # ── Session extremes from bar highs/lows ──
        current_high = max(highs)
        current_low = min(lows)
        if state.session_high is None or current_high > state.session_high:
            state.session_high = current_high
            for b in bars:
                if b.high == current_high:
                    try:
                        from zoneinfo import ZoneInfo
                        from datetime import datetime
                        state.session_high_time = datetime.fromtimestamp(b.timestamp, ZoneInfo("America/Chicago")).strftime("%I:%M %p")
                    except Exception:
                        state.session_high_time = ""
                    break
            IntradayLevelTracker._upsert_level(state, current_high, "resistance", "session_high", now)
        if state.session_low is None or current_low < state.session_low:
            state.session_low = current_low
            for b in bars:
                if b.low == current_low:
                    try:
                        from zoneinfo import ZoneInfo
                        from datetime import datetime
                        state.session_low_time = datetime.fromtimestamp(b.timestamp, ZoneInfo("America/Chicago")).strftime("%I:%M %p")
                    except Exception:
                        state.session_low_time = ""
                    break
            IntradayLevelTracker._upsert_level(state, current_low, "support", "session_low", now)
        if n < INTRADAY_MIN_PRICES_FOR_LEVELS:
            return events

        # ── Zone detection from bar closes + bar range departure ──
        zone_counts: Dict[float, list] = {}
        for i, c in enumerate(closes):
            placed = False
            for rep in zone_counts:
                if abs(c - rep) <= tol: zone_counts[rep].append(i); placed = True; break
            if not placed: zone_counts[c] = [i]
        for rep_price, indices in zone_counts.items():
            if len(indices) < INTRADAY_MIN_TOUCHES: continue
            has_dep = False
            for idx in indices:
                for j in range(idx+1, min(idx+4, n)):
                    bar_dist = max(abs(highs[j] - rep_price), abs(lows[j] - rep_price))
                    if bar_dist > tol * 2.5: has_dep = True; break
                if has_dep: break
            if not has_dep: continue
            bu = sum(1 for idx in indices if idx+1 < n and closes[idx+1] > rep_price + tol*0.5)
            bd = sum(1 for idx in indices if idx+1 < n and closes[idx+1] < rep_price - tol*0.5)
            if bu >= INTRADAY_MIN_TOUCHES:
                if IntradayLevelTracker._upsert_level(state, rep_price, "support", "rejection_zone", now, touches=len(indices)):
                    events.append({"msg": f"📍 NEW 5m SUPPORT at ${rep_price:.2f} ({len(indices)} touches).", "type": "info", "priority": 3, "alert_key": f"id_sup_{rep_price:.2f}"})
            if bd >= INTRADAY_MIN_TOUCHES:
                if IntradayLevelTracker._upsert_level(state, rep_price, "resistance", "rejection_zone", now, touches=len(indices)):
                    events.append({"msg": f"📍 NEW 5m RESISTANCE at ${rep_price:.2f} ({len(indices)} touches).", "type": "info", "priority": 3, "alert_key": f"id_res_{rep_price:.2f}"})

        # ── Sharp move detection from bar structure ──
        if n >= 3:
            for i in range(max(n-12, 1), n):
                bar_i = bars[i]
                move_pct = bar_i.range / bar_i.open * 100 if bar_i.open > 0 else 0
                if move_pct >= INTRADAY_SHARP_MOVE_THRESHOLD and bar_i.body_pct > 0.5:
                    origin = bar_i.low if bar_i.is_bullish else bar_i.high
                    kind = "support" if bar_i.is_bullish else "resistance"
                    verb = "launched from" if bar_i.is_bullish else "dumped from"
                    if IntradayLevelTracker._upsert_level(state, origin, kind, "sharp_move_origin", now):
                        events.append({"msg": f"📍 SHARP MOVE ORIGIN: price {verb} ${origin:.2f}. Now intraday {kind}.", "type": "info", "priority": 3, "alert_key": f"sharp_{kind[:3]}_{origin:.2f}"})

        # ── Consolidation detection from bar ranges ──
        if n >= INTRADAY_CONSOLIDATION_CANDLES:
            recent_bars = bars[-INTRADAY_CONSOLIDATION_CANDLES:]
            ch = max(b.high for b in recent_bars)
            cl = min(b.low for b in recent_bars)
            rng = ch - cl; rng_pct = (rng / price * 100) if price > 0 else 0
            if rng_pct <= INTRADAY_CONSOLIDATION_RANGE_PCT and rng > 0:
                IntradayLevelTracker._upsert_level(state, ch, "resistance", "consolidation_edge", now)
                IntradayLevelTracker._upsert_level(state, cl, "support", "consolidation_edge", now)
                events.append({"msg": f"📍 CONSOLIDATION: ${cl:.2f}-${ch:.2f} ({INTRADAY_CONSOLIDATION_CANDLES*5} min). Break with momentum is actionable.", "type": "info", "priority": 2, "alert_key": f"cons_{cl:.1f}_{ch:.1f}"})

        return events

    @staticmethod
    def _upsert_level(state, price, kind, source, now, touches=1):
        tol = price * INTRADAY_ZONE_TOLERANCE_PCT / 100
        for lvl in state.intraday_levels:
            if abs(lvl.price - price) <= tol and lvl.kind == kind:
                lvl.touches = max(lvl.touches, touches); lvl.last_touched_ts = now; lvl.active = True; return False
        state.intraday_levels.append(IntradayLevel(price=round(price, 2), kind=kind, source=source, touches=touches, first_seen_ts=now, last_touched_ts=now, active=True))
        return True

    @staticmethod
    def get_active_levels(state: MonitorState, price: float) -> dict:
        active = [l for l in state.intraday_levels if l.active]
        sups = sorted([l for l in active if l.kind == "support" and l.price < price], key=lambda l: price - l.price)
        ress = sorted([l for l in active if l.kind == "resistance" and l.price > price], key=lambda l: l.price - price)
        return {"support": sups[0] if sups else None, "resistance": ress[0] if ress else None, "all_support": sups[:3], "all_resistance": ress[:3]}

class ThesisMonitorEngine:
    def __init__(self, store_get_fn=None, store_set_fn=None, get_bars_fn=None):
        self._theses: Dict[str, ThesisContext] = {}; self._states: Dict[str, MonitorState] = {}
        self._lock = threading.Lock(); self._store_get = store_get_fn; self._store_set = store_set_fn
        self._get_bars_fn = get_bars_fn  # callable(ticker, resolution, countback) -> dict
        # v2.0: bar managers, level registries, exit policies, entry validator
        self._bar_managers: Dict[str, BarStateManager] = {}
        self._level_registries: Dict[str, LevelRegistry] = {}
        self._level_first_seen: Dict[str, float] = {}  # "ticker:price_rounded" → epoch
        self._entry_validator = EntryValidator()

    # Tickers that use 1-minute bars for lower latency.
    # All others use 5-minute bars.
    _HIGH_RES_TICKERS = {"SPY"}

    def _get_or_create_bar_manager(self, ticker: str) -> Optional[BarStateManager]:
        """Lazy-init bar manager for a ticker.
        SPY uses 1-minute bars to reduce alert lag from ~5min to ~1min.
        All other tickers use 5-minute bars.
        """
        if ticker in self._bar_managers:
            return self._bar_managers[ticker]
        if not self._get_bars_fn:
            return None
        resolution = 1 if ticker.upper() in self._HIGH_RES_TICKERS else 5
        bm = BarStateManager(ticker, self._get_bars_fn, resolution=resolution)
        if bm.initialize():
            self._bar_managers[ticker] = bm
            return bm
        return None

    def _build_level_registry(self, ticker: str, thesis: ThesisContext,
                              state: MonitorState, spot: float) -> LevelRegistry:
        """Build or rebuild level registry from all sources.
        Uses cached first_seen timestamps so thesis levels don't get freshness-penalized every cycle."""
        reg = LevelRegistry()
        now_epoch = time.time()
        # Thesis levels — use cached first_seen (these are stable across cycles)
        thesis_epoch = self._level_first_seen_for_thesis(ticker, thesis, now_epoch)
        reg.ingest_thesis_levels(thesis.levels, spot=spot, epoch=thesis_epoch)
        # Opening range — use OR completion time, not now
        bm = self._bar_managers.get(ticker)
        if bm:
            or30_epoch = self._level_first_seen.get(f"{ticker}:OR30", now_epoch)
            if bm.state.or_30.is_complete and f"{ticker}:OR30" not in self._level_first_seen:
                self._level_first_seen[f"{ticker}:OR30"] = now_epoch
                or30_epoch = now_epoch
            reg.ingest_opening_range(bm.state.or_30, epoch=or30_epoch)
            or15_epoch = self._level_first_seen.get(f"{ticker}:OR15", now_epoch)
            if bm.state.or_15.is_complete and f"{ticker}:OR15" not in self._level_first_seen:
                self._level_first_seen[f"{ticker}:OR15"] = now_epoch
                or15_epoch = now_epoch
            reg.ingest_opening_range(bm.state.or_15, epoch=or15_epoch)
            # Also ingest OR5
            or5_epoch = self._level_first_seen.get(f"{ticker}:OR5", now_epoch)
            if bm.state.or_5.is_complete and f"{ticker}:OR5" not in self._level_first_seen:
                self._level_first_seen[f"{ticker}:OR5"] = now_epoch
                or5_epoch = now_epoch
            reg.ingest_opening_range(bm.state.or_5, epoch=or5_epoch)
        # Intraday levels — use their actual first_seen_ts (already tracked)
        if state.intraday_levels:
            reg.ingest_intraday_levels(state.intraday_levels, epoch=0)  # epoch=0 means use level's own timestamp
        # Score everything
        session_range = 0
        if state.session_high is not None and state.session_low is not None:
            session_range = (state.session_high or 0) - (state.session_low or 999999)
            session_range = max(session_range, 0)
        reg.score_all(
            spot=spot,
            session_range=session_range,
            pin_low=thesis.levels.pin_zone_low,
            pin_high=thesis.levels.pin_zone_high,
            now_epoch=now_epoch,
        )
        self._level_registries[ticker] = reg
        return reg

    def _level_first_seen_for_thesis(self, ticker, thesis, now_epoch):
        """Get or set first_seen epoch for thesis levels.
        Keyed by ticker + session date, NOT by created_at — because /em refreshes
        create new timestamps but the structural levels (daily S/R, OI walls) don't
        actually become "fresh" just because a new card was generated."""
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            session_date = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
        except Exception:
            session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{ticker}:thesis:{session_date}"
        if key not in self._level_first_seen:
            self._level_first_seen[key] = now_epoch
        return self._level_first_seen[key]

    def store_thesis(self, ticker: str, thesis: ThesisContext):
        with self._lock:
            self._theses[ticker] = thesis
            old = self._states.get(ticker); ns = MonitorState()
            now_mono = time.monotonic(); now_epoch = time.time()
            if old and old.failed_moves:
                ns.failed_moves = [fm for fm in old.failed_moves if (now_mono - fm.break_time) < 1800]
            if old and old.intraday_levels:
                ns.intraday_levels = [l for l in old.intraday_levels if l.active]
                ns.session_high = old.session_high; ns.session_low = old.session_low
                ns.session_high_time = old.session_high_time; ns.session_low_time = old.session_low_time
            if old and old.active_trades:
                ns.active_trades = [
                    t for t in old.active_trades
                    if t.status in ("OPEN", "SCALED", "TRAILED")
                    or (now_epoch - t.entry_epoch if t.entry_epoch > 0 else 0) < 28800
                ]
            if old and old.price_history:
                ns.price_history = old.price_history[-60:]
            if old and old.break_attempts:
                ns.break_attempts = [
                    ba for ba in old.break_attempts
                    if ba.detected_as_failed or ba.detected_as_confirmed
                    or (now_mono - ba.break_time) <= MONITOR_MAX_BREAK_AGE_SEC
                ]
                ns.confirmed_breaks = old.confirmed_breaks[-10:]
            if old:
                ns.active_trend_direction = old.active_trend_direction
                ns.alert_history = old.alert_history
            self._states[ticker] = ns
            log.info(f"Thesis stored: {ticker} | bias={thesis.bias} score={thesis.bias_score} gex={thesis.gex_sign} regime={thesis.regime}")
            self._persist_thesis(ticker, thesis)

    def _persist_thesis(self, ticker, thesis):
        if not self._store_set: return
        try:
            data = {"ticker": thesis.ticker, "bias": thesis.bias, "bias_score": thesis.bias_score, "gex_sign": thesis.gex_sign, "gex_value": thesis.gex_value, "dex_value": thesis.dex_value, "vanna_value": thesis.vanna_value, "charm_value": thesis.charm_value, "regime": thesis.regime, "volatility_regime": thesis.volatility_regime, "vix": thesis.vix, "iv": thesis.iv, "prior_day_close": thesis.prior_day_close, "prior_day_context": thesis.prior_day_context, "session_label": thesis.session_label, "created_at": thesis.created_at, "spot_at_creation": thesis.spot_at_creation, "levels": asdict(thesis.levels)}
            self._store_set(f"thesis_monitor:{ticker}", json.dumps(data), ttl=86400)
            # Maintain monitored ticker list in store
            self._update_ticker_list(ticker)
        except Exception as e: log.warning(f"Thesis persist failed for {ticker}: {e}")

    def _update_ticker_list(self, ticker):
        """Add ticker to persisted monitored list if not already present."""
        if not self._store_set or not self._store_get:
            return
        try:
            raw = self._store_get("thesis_monitor:tickers")
            tickers = json.loads(raw) if raw else []
            if ticker not in tickers:
                tickers.append(ticker)
                self._store_set("thesis_monitor:tickers", json.dumps(tickers), ttl=86400)
        except Exception as e:
            log.warning(f"Ticker list update failed: {e}")

    def _load_thesis_from_store(self, ticker):
        if not self._store_get: return None
        try:
            raw = self._store_get(f"thesis_monitor:{ticker}")
            if not raw: return None
            d = json.loads(raw)
            levels = ThesisLevels(**{k: v for k, v in d.get("levels", {}).items() if k in ThesisLevels.__dataclass_fields__})
            t = ThesisContext(ticker=d.get("ticker", ticker), bias=d.get("bias", "NEUTRAL"), bias_score=d.get("bias_score", 0), gex_sign=d.get("gex_sign", "positive"), gex_value=d.get("gex_value", 0), dex_value=d.get("dex_value", 0), vanna_value=d.get("vanna_value", 0), charm_value=d.get("charm_value", 0), regime=d.get("regime", "UNKNOWN"), volatility_regime=d.get("volatility_regime", "NORMAL"), vix=d.get("vix", 20), iv=d.get("iv", 0.20), prior_day_close=d.get("prior_day_close"), prior_day_context=d.get("prior_day_context", "NORMAL"), session_label=d.get("session_label", ""), created_at=d.get("created_at", ""), spot_at_creation=d.get("spot_at_creation", 0), levels=levels)
            log.info(f"Thesis loaded from store: {ticker} | bias={t.bias} gex={t.gex_sign}")
            return t
        except Exception as e: log.warning(f"Thesis load failed for {ticker}: {e}"); return None

    def get_thesis(self, ticker):
        t = self._theses.get(ticker)
        if t: return t
        t = self._load_thesis_from_store(ticker)
        if t:
            self._theses[ticker] = t
            state = self._states.setdefault(ticker, MonitorState())
            # Recover active trades from store
            if not state.active_trades:
                self._load_trades_from_store(ticker, state)
        return t

    def get_state(self, ticker): return self._states.get(ticker)

    def get_monitored_tickers(self) -> List[str]:
        tickers = set(self._theses.keys())
        # Load persisted ticker list on restart
        if not tickers and self._store_get:
            try:
                raw = self._store_get("thesis_monitor:tickers")
                if raw:
                    stored = json.loads(raw)
                    for t in stored:
                        if self.get_thesis(t):
                            tickers.add(t)
            except Exception as e:
                log.warning(f"Ticker list load failed: {e}")
        for d in MONITOR_DEFAULT_TICKERS:
            if d not in tickers and self.get_thesis(d): tickers.add(d)
        return sorted(tickers)

    def _recent_net_move(self, state, lookback=3, bm=None):
        """Net price move over last N closed bars. No bars = 0."""
        if bm and len(bm.state.bars_5m) >= lookback:
            bars = bm.state.bars_5m[-lookback:]
            return bars[-1].close - bars[0].close
        return 0.0

    def evaluate(self, ticker, price):
        with self._lock:
            thesis = self._theses.get(ticker); state = self._states.get(ticker)
            if not thesis or not state: return []
            events = []; now = time.monotonic()
            try:
                from zoneinfo import ZoneInfo; ts = datetime.now(ZoneInfo("America/Chicago")).strftime("%I:%M %p")
            except Exception: ts = datetime.utcnow().strftime("%H:%M")
            # ── v2.0: Bar-aware price source ──
            bar = None
            bm = self._get_or_create_bar_manager(ticker)
            if bm:
                bar = bm.update()
                if bar:
                    price = bar.close  # canonical price = bar close, not spot sample
            prev_price = state.price_history[-1]["price"] if state.price_history else None
            state.price_history.append({"price": price, "time_str": ts, "ts_mono": now})
            if len(state.price_history) > 240: state.price_history = state.price_history[-240:]
            state.check_count += 1
            # ── v2.0: Build level registry with confluence scoring ──
            registry = self._build_level_registry(ticker, thesis, state, price)
            events.extend(self._evaluate_momentum(state, price, bm=bm))
            events.extend(IntradayLevelTracker.update(state, price, now, bm=bm))
            # Prior-day gap context — fire once early in session
            if state.check_count <= 3 and thesis.prior_day_context in ("GAP_UP", "GAP_DOWN") and not state.prior_day_applied:
                state.prior_day_applied = True
                if thesis.prior_day_context == "GAP_UP" and thesis.gex_sign == "positive":
                    events.append({"msg": "📊 GAP UP into GEX+ — fade probability HIGH. Watch for failed breakout.", "type": "info", "priority": 3, "alert_key": "gap_up_ctx"})
                elif thesis.prior_day_context == "GAP_DOWN" and thesis.gex_sign == "negative":
                    events.append({"msg": "📊 GAP DOWN into GEX- — trend can extend. Respect the gap.", "type": "info", "priority": 3, "alert_key": "gap_dn_ctx"})
                elif thesis.prior_day_context == "GAP_UP" and thesis.gex_sign == "negative":
                    events.append({"msg": "📊 GAP UP into GEX- — breakout can accelerate. Trail tight if fading.", "type": "info", "priority": 3, "alert_key": "gap_up_neg_ctx"})
                elif thesis.prior_day_context == "GAP_DOWN" and thesis.gex_sign == "positive":
                    events.append({"msg": "📊 GAP DOWN into GEX+ — bounce probability HIGH. Watch for failed breakdown.", "type": "info", "priority": 3, "alert_key": "gap_dn_pos_ctx"})
            # Prune stale break attempts — keep resolved ones and those still within age window
            state.break_attempts = [
                ba for ba in state.break_attempts
                if ba.detected_as_failed
                or ba.detected_as_confirmed
                or (now - ba.break_time) <= MONITOR_MAX_BREAK_AGE_SEC
            ]
            # Break attempts age by bar index, not poll count — no increment needed here
            if prev_price is not None: events.extend(self._detect_breaks(thesis, state, price, prev_price, now, bar=bar, bm=bm))
            entry_events = []
            entry_events.extend(self._detect_confirmed_breaks(thesis, state, price, now, bar=bar, bm=bm))
            entry_events.extend(self._detect_failed_moves(thesis, state, price, now, bm=bm))
            entry_events.extend(self._detect_retests(thesis, state, price, now, bm=bm))
            # Auto-create ActiveTrade — runs through EntryValidator + ExitPolicy
            for ev in entry_events:
                if ev.get("type") in ("trade_confirmed", "critical") and ev.get("priority", 0) >= 5:
                    ak = ev.get("alert_key", "")
                    if "wait" not in ak and "late" not in ak:
                        self._create_trade_from_event(ticker, thesis, state, price, ev, now, ts,
                                                       registry=registry, bar=bar, bm=bm)
            events.extend(entry_events)
            events.extend(self._check_gamma_flip(thesis, state, price))
            # ── v2.0: Exit monitoring with policy engine ──
            events.extend(self._monitor_exits(ticker, thesis, state, price, now, bar=bar, bm=bm))
            # Expire stale trades
            if self._expire_stale_trades(state, now):
                self._persist_trades(ticker, state)
            if state.status == "FRESH" and state.check_count >= 2: state.status = "DEVELOPING"
            return self._apply_cooldowns(state, events, now)

    # ── Exit Monitor: Target Discovery ──
    def _find_targets(self, ticker, thesis, state, price, direction):
        """Find next S/R levels in trade direction as targets. Zero API calls."""
        targets = []
        lvl = thesis.levels
        # Gather all known levels
        all_levels = []
        for attr, name in [
            ("local_support", "daily_support"), ("local_resistance", "daily_resistance"),
            ("put_wall", "put_wall"), ("call_wall", "call_wall"), ("gamma_wall", "gamma_wall"),
            ("gamma_flip", "gamma_flip"), ("s1", "S1"), ("r1", "R1"), ("pivot", "pivot"),
            ("em_low", "EM_low"), ("em_high", "EM_high"), ("em_2sd_low", "EM_2sd_low"),
            ("em_2sd_high", "EM_2sd_high"), ("fib_support", "fib_support"),
            ("fib_resistance", "fib_resistance"), ("vpoc", "vpoc"), ("max_pain", "max_pain"),
        ]:
            v = getattr(lvl, attr, None)
            if v is not None and v > 0:
                all_levels.append(v)
        # Add active intraday levels
        for il in state.intraday_levels:
            if il.active:
                all_levels.append(il.price)
        # Deduplicate within tolerance
        tol = price * 0.001  # 0.1%
        unique = []
        for lv in sorted(set(all_levels)):
            if not unique or abs(lv - unique[-1]) > tol:
                unique.append(lv)
        if direction == "SHORT":
            targets = sorted([l for l in unique if l < price - tol])
            targets = targets[-3:][::-1]  # nearest 3 below, closest first
        else:  # LONG
            targets = sorted([l for l in unique if l > price + tol])
            targets = targets[:3]  # nearest 3 above, closest first
        return targets

    # ── Exit Monitor: Auto-Create Trade ──
    def _create_trade_from_event(self, ticker, thesis, state, price, event, now, ts,
                                   registry=None, bar=None, bm=None):
        """Create an ActiveTrade from a clean entry signal. v2.0: validates through EntryValidator."""
        ak = event.get("alert_key", "")
        msg = event.get("msg", "")
        # Parse direction and entry_type from alert_key
        if "conf_short" in ak or "fbo_now" in ak or "rt_short" in ak or "rt_fs" in ak:
            direction = "SHORT"
        elif "conf_long" in ak or "fb_now" in ak or "rt_long" in ak or "rt_fl" in ak:
            direction = "LONG"
        else:
            log.info(f"Cannot parse direction from alert_key={ak}, skipping trade creation")
            return
        if "conf_" in ak:
            entry_type = "BREAK"
        elif "fb_" in ak or "fbo_" in ak:
            entry_type = "FAILED"
        elif "rt_" in ak:
            entry_type = "RETEST"
        else:
            entry_type = "BREAK"
        # Find the stop level
        stop = 0.0
        level_name = ""
        for ba in state.break_attempts + state.failed_moves + state.confirmed_breaks:
            bak = ak.split("_")[-1]
            try:
                ba_price_str = f"{ba.level:.2f}"
                if ba_price_str == bak or ba_price_str in ak:
                    stop = ba.level
                    level_name = ba.level_name
                    break
            except Exception:
                continue
        if stop == 0.0:
            if direction == "LONG":
                stop = price * 0.995
            else:
                stop = price * 1.005
        # Dedup
        for t in state.active_trades:
            if t.status not in ("OPEN", "SCALED", "TRAILED"):
                continue
            if t.direction != direction:
                continue
            if abs(t.stop_level - stop) / stop < 0.005 or abs(t.entry_price - price) / price < 0.005:
                log.info(f"Trade already open for {ticker} {direction} stop=${t.stop_level:.2f} near new stop=${stop:.2f}, skipping")
                return

        # ── v2.0: Level quality from registry ──
        level_quality = 0
        level_tier = "C"
        level_sources = set()
        if registry:
            reg_level = registry.get_level_at(stop)
            if reg_level:
                level_quality = reg_level.quality_score
                level_tier = reg_level.quality_tier
                level_sources = reg_level.sources

        # ── v2.0: Bar metrics for validator ──
        bar_closed = True
        bar_wicked = False
        bar_body_pct = 0.5
        bar_expansion = 1.0
        consecutive = 0
        if bar:
            bar_closed = bar.closed_through(stop, "DOWN" if direction == "SHORT" else "UP")
            bar_wicked = bar.wicked_through(stop, "DOWN" if direction == "SHORT" else "UP")
            bar_body_pct = bar.body_pct
        if bm:
            bar_expansion = bm.state.metrics.expansion_ratio
            consecutive = bm.state.metrics.consecutive_direction

        # ── v2.0: Extension metrics ──
        ext_trigger = abs(price - stop) / stop * 100 if stop > 0 else 0
        ext_vwap = bm.distance_from_vwap(price) if bm else 0
        or_dist = bm.distance_from_or(price) if bm else {}
        ext_or = or_dist.get("dist_high", 0) if direction == "LONG" else or_dist.get("dist_low", 0)
        ext_em = 0
        if thesis.levels.em_high and thesis.levels.em_low:
            em_width = thesis.levels.em_high - thesis.levels.em_low
            if em_width > 0:
                if direction == "LONG" and thesis.levels.em_high:
                    ext_em = (price - thesis.levels.em_high) / em_width if price > thesis.levels.em_high else 0
                elif direction == "SHORT" and thesis.levels.em_low:
                    ext_em = (thesis.levels.em_low - price) / em_width if price < thesis.levels.em_low else 0

        tp = _get_time_phase_ct()
        is_above_flip = None
        if thesis.levels.gamma_flip is not None:
            is_above_flip = price > thesis.levels.gamma_flip
        or_type = bm.state.or_30.range_type if bm and bm.state.or_30.is_complete else "NORMAL"

        # ── v2.0: Run EntryValidator ──
        validation = self._entry_validator.validate(
            level_quality=level_quality, level_tier=level_tier, level_sources=level_sources,
            bar_closed_through=bar_closed, bar_wicked_only=bar_wicked,
            bar_body_pct=bar_body_pct, bar_expansion=bar_expansion,
            consecutive_bars=consecutive,
            extension_from_trigger_pct=ext_trigger,
            extension_from_vwap_pct=ext_vwap,
            extension_from_or_pct=ext_or,
            extension_from_em_pct=ext_em,
            gex_sign=thesis.gex_sign, regime=thesis.regime,
            setup_type=entry_type, time_phase=tp["phase"],
            direction=direction, is_above_gamma_flip=is_above_flip,
            signal_age_sec=0, vix=thesis.vix, or_type=or_type,
        )

        if not validation.valid:
            log.info(f"EntryValidator REJECTED {ticker} {direction} {entry_type}: "
                     f"score={validation.setup_score} [{validation.gate_summary}]")
            stop_dist_pct = abs(price - stop) / price * 100 if price > 0 else 0
            _log_filtered_trade(
                ticker=ticker, filter_reason="VALIDATOR_REJECTED",
                direction=direction, entry_type=entry_type,
                setup_score=validation.setup_score, gate_summary=validation.gate_summary,
                level_name=level_name, level_tier=level_tier,
                time_phase=tp["phase"], regime=thesis.regime,
                gex_sign=thesis.gex_sign, volatility_regime=thesis.volatility_regime,
                prior_day_context=thesis.prior_day_context, bias=thesis.bias,
                price=price, stop_level=stop, stop_dist_pct=stop_dist_pct,
                badge="",  # no badge — never reached badging
            )
            return

        # ── v2.0: Select ExitPolicy ──
        policy = select_exit_policy(
            setup_score=validation.setup_score,
            gex_sign=thesis.gex_sign,
            setup_type=entry_type,
            is_0dte=(tp["phase"] not in ("POWER_HOUR", "CLOSE")),  # use 1DTE during power hour/close
            vix=thesis.vix,
            time_phase=tp["phase"],
        )

        # ── v2.0: Targets from registry (quality-ranked) ──
        if registry:
            target_levels = registry.get_targets(price, direction, count=3, min_score=10)
            targets = [l.price for l in target_levels]
        else:
            targets = self._find_targets(ticker, thesis, state, price, direction)

        trade = ActiveTrade(
            ticker=ticker, direction=direction, entry_type=entry_type,
            entry_price=price, stop_level=stop, targets=targets,
            status="OPEN", entry_time=now, entry_epoch=time.time(),
            entry_time_str=ts, level_name=level_name, gex_at_entry=thesis.gex_sign,
            regime=thesis.regime, time_phase=tp["phase"],
            prior_day_context=thesis.prior_day_context,
            bias_at_entry=thesis.bias, volatility_regime=thesis.volatility_regime,
            trade_type_label=validation.trade_type,
            setup_score=validation.setup_score, setup_label=validation.setup_label,
            exit_policy_name=policy.name, scale_advice=validation.scale_advice,
            level_tier=level_tier, validation_summary=validation.gate_summary,
            policy_config=policy.to_config(),
            max_favorable=0.0, min_favorable=0.0,
        )

        # ── Wire ATM delta/premium from thesis snapshot ───────────────────
        # LONG trades use call delta/premium; SHORT trades use put delta/premium.
        # These were captured at em card time from the live chain and stored on
        # the thesis so we can approximate the 20% premium stop each poll
        # without fetching a live option quote.
        if direction == "LONG":
            trade.entry_delta   = getattr(thesis, "atm_call_delta",   0.0) or 0.0
            trade.entry_premium = getattr(thesis, "atm_call_premium", 0.0) or 0.0
        else:
            trade.entry_delta   = getattr(thesis, "atm_put_delta",   0.0) or 0.0
            trade.entry_premium = getattr(thesis, "atm_put_premium", 0.0) or 0.0
        if trade.entry_delta > 0 and trade.entry_premium > 0:
            log.info(f"Premium stop armed: {ticker} {direction} δ={trade.entry_delta:.2f} "
                     f"prem=${trade.entry_premium:.2f} — 20% stop at ~${trade.entry_premium * 0.20:.2f} adverse")
        else:
            log.info(f"Premium stop not armed for {ticker} {direction} — no ATM data on thesis "
                     f"(delta={trade.entry_delta}, premium={trade.entry_premium})")

        state.active_trades.append(trade)
        self._persist_trades(ticker, state)
        badge = _trade_quality_badge(trade)
        _log_trade_event(trade, event="OPEN", badge=badge)
        log.info(f"ActiveTrade created: {ticker} {direction} {entry_type} @ ${price:.2f} | "
                 f"score={validation.setup_score}/5 [{validation.setup_label}] | "
                 f"policy={policy.name} | scale={validation.scale_advice} | "
                 f"stop=${stop:.2f} | targets={[f'${t:.2f}' for t in targets]} | "
                 f"gates=[{validation.gate_summary}]")

    # ── Exit Monitor: Core Loop ──
    def _monitor_exits(self, ticker, thesis, state, price, now, bar=None, bm=None):
        """Two-layer exit engine:
        Layer 1 (every poll): Spot-risk overlay — hard stop breach only.
        Layer 2 (bar-close only): Policy evaluation — scale, trail, reduce, exit, exhaustion.
        One engine, one truth source. No legacy fallback."""
        events = []
        latest_bar = bar or (bm.get_latest_bar() if bm else None)
        recent_bars = bm.get_recent_bars(5) if bm else []

        for trade in state.active_trades:
            if trade.status in ("CLOSED", "INVALIDATED"):
                continue

            # Update max favorable excursion (always, every poll)
            if trade.direction == "LONG":
                fav = price - trade.entry_price
                trade.max_favorable = max(trade.max_favorable, fav)
                trade.min_favorable = min(trade.min_favorable, fav)
            else:
                fav = trade.entry_price - price
                trade.max_favorable = max(trade.max_favorable, fav)
                trade.min_favorable = min(trade.min_favorable, fav)

            # ── Auto-migrate legacy trades without policy_config ──
            if not trade.policy_config:
                from exit_policy import select_exit_policy as _sel
                default_policy = _sel(
                    setup_score=max(trade.setup_score, 3),
                    gex_sign=trade.gex_at_entry,
                    setup_type=trade.entry_type,
                    is_0dte=True, vix=thesis.vix,
                )
                trade.policy_config = default_policy.to_config()
                trade.exit_policy_name = default_policy.name
                self._persist_trades(ticker, state)
                log.info(f"Auto-migrated trade {trade.trade_id} to policy {default_policy.name}")

            # ── LAYER 1: Spot-risk overlay (every poll) ──
            # Hard stop breach — emergency protection, no bar required
            stop_ref = trade.trail_stop if trade.trail_stop else trade.stop_level
            breached = False
            if trade.direction == "LONG" and price < stop_ref:
                breached = True
            elif trade.direction == "SHORT" and price > stop_ref:
                breached = True
            if breached:
                if self._exit_alert_ok(trade, "invalidate", now):
                    pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
                    trade.status = "INVALIDATED"; trade.close_price = price
                    trade.close_time = now; trade.close_epoch = time.time()
                    trade.close_reason = f"Hard stop ${stop_ref:.2f} breached"
                    self._persist_trades(ticker, state)
                    events.append({"msg": f"\U0001f6d1 TRADE INVALIDATED — {trade.direction}\n\nHard stop ${stop_ref:.2f} breached.\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\nDon't hope. Close it.", "type": "exit", "priority": 5, "alert_key": f"exit_inv_{trade.trade_id}"})
                    log.info(f"Trade {trade.trade_id} INVALIDATED: hard stop ${stop_ref:.2f} breached at ${price:.2f}")
                    _log_trade_event(trade, event="CLOSE", close_price=price,
                                      close_reason=f"Hard stop ${stop_ref:.2f} breached",
                                      badge=_trade_quality_badge(trade))
                continue

            # ── LAYER 1b: Delta-based 20% premium stop ───────────────────────
            # No live quote needed. Uses entry_delta (from em chain at session
            # start) and min_favorable (worst spot excursion, tracked every poll)
            # to estimate current option loss percentage.
            # Formula: est_loss = adverse_spot_move × entry_delta / entry_premium
            # Fires when estimated premium loss ≥ 20%.
            # Approximation degrades for large moves (delta shifts) but is
            # accurate enough for the tight intraday stops we run.
            if (trade.entry_premium > 0 and trade.entry_delta > 0
                    and trade.status in ("OPEN",)  # only before scaling
                    and trade.max_favorable <= 0):  # no MFE yet — still a pure loser
                adverse_move = abs(trade.min_favorable)
                if adverse_move > 0:
                    est_loss_pct = (adverse_move * trade.entry_delta) / trade.entry_premium
                    if est_loss_pct >= 0.20:
                        if self._exit_alert_ok(trade, "prem_stop", now):
                            pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
                            est_loss_dollars = round(adverse_move * trade.entry_delta, 2)
                            trade.status = "INVALIDATED"; trade.close_price = price
                            trade.close_time = now; trade.close_epoch = time.time()
                            trade.close_reason = (f"Premium stop: est. {est_loss_pct:.0%} loss "
                                                   f"(${adverse_move:.2f} adverse × δ{trade.entry_delta:.2f})")
                            self._persist_trades(ticker, state)
                            events.append({"msg": (
                                f"🛑 PREMIUM STOP — {trade.direction}\n\n"
                                f"Estimated option loss ~{est_loss_pct:.0%} of entry premium.\n"
                                f"Adverse move: ${adverse_move:.2f} × delta {trade.entry_delta:.2f} "
                                f"≈ ${est_loss_dollars:.2f} estimated loss.\n"
                                f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                                f"Cut it. Don't let a small loss compound."
                            ), "type": "exit", "priority": 5, "alert_key": f"exit_prem_{trade.trade_id}"})
                            log.info(f"Trade {trade.trade_id} PREMIUM STOP: est {est_loss_pct:.0%} loss at ${price:.2f}")
                            _log_trade_event(trade, event="CLOSE", close_price=price,
                                              close_reason=trade.close_reason,
                                              badge=_trade_quality_badge(trade))
                            continue

            # ── LAYER 2: Policy evaluation (bar-close clock only) ──
            if not latest_bar:
                continue  # no bar data = no strategy decisions

            # Skip if we already evaluated this bar
            if latest_bar.timestamp <= trade.last_exit_bar_ts:
                continue
            trade.last_exit_bar_ts = latest_bar.timestamp

            policy = ExitPolicy.from_config(trade.policy_config)
            signal = evaluate_exit(policy, trade, latest_bar, recent_bars,
                                   current_gex=thesis.gex_sign)

            if signal.action == "SCALE":
                if self._exit_alert_ok(trade, "scale", now):
                    trade.status = "SCALED"; trade.scaled_at_price = price
                    trade.trail_stop = trade.entry_price
                    self._persist_trades(ticker, state)
                    events.append({"msg": f"\U0001f4b0 SCALE {signal.scale_fraction} — {trade.direction}\n\n{signal.reason}\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({signal.pnl_pct:+.2f}%)\nMove stop to breakeven. {trade.scale_advice}", "type": "exit", "priority": 5, "alert_key": f"exit_scale_{trade.trade_id}"})
            elif signal.action == "TRAIL" and signal.new_stop:
                if self._exit_alert_ok(trade, "trail", now):
                    trade.trail_stop = signal.new_stop; trade.status = "TRAILED"
                    self._persist_trades(ticker, state)
                    events.append({"msg": f"\U0001f3c3 TRAIL — {trade.direction}\n\n{signal.reason}\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({signal.pnl_pct:+.2f}%)\nPolicy: {policy.name}", "type": "exit", "priority": 5, "alert_key": f"exit_trail_{trade.trade_id}"})
            elif signal.action == "REDUCE":
                if self._exit_alert_ok(trade, "fade", now):
                    # Latch GEX flip
                    if thesis.gex_sign != trade.policy_config.get("gex_at_entry"):
                        adjusted = policy.recalibrate_for_gex_flip(thesis.gex_sign)
                        trade.policy_config = adjusted.to_config()
                        trade.policy_config["gex_at_entry"] = thesis.gex_sign
                        trade.gex_at_entry = thesis.gex_sign
                        trade.exit_policy_name = adjusted.name
                        self._persist_trades(ticker, state)
                        log.info(f"Policy latched: {trade.trade_id} → {adjusted.name}")
                    if signal.new_stop:
                        trade.trail_stop = signal.new_stop
                    events.append({"msg": f"\u26a0\ufe0f REDUCE — {trade.direction}\n\n{signal.reason}\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({signal.pnl_pct:+.2f}%)\n{signal.urgency_label}", "type": "exit", "priority": 4, "alert_key": f"exit_fade_{trade.trade_id}"})
            elif signal.action == "EXIT":
                if self._exit_alert_ok(trade, "exit", now):
                    trade.status = "CLOSED"; trade.close_price = price
                    trade.close_time = now; trade.close_epoch = time.time()
                    trade.close_reason = signal.reason
                    _log_trade_event(trade, event="CLOSE", close_price=price,
                                      close_reason=signal.reason,
                                      badge=_trade_quality_badge(trade))
                    self._persist_trades(ticker, state)
                    events.append({"msg": f"\u23f9 EXIT — {trade.direction}\n\n{signal.reason}\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({signal.pnl_pct:+.2f}%)\nPay yourself.", "type": "exit", "priority": 5, "alert_key": f"exit_done_{trade.trade_id}"})
            elif signal.action == "INVALIDATE":
                # Bar-close invalidation (policy detected, distinct from hard-stop)
                if self._exit_alert_ok(trade, "invalidate", now):
                    pnl_pct = signal.pnl_pct
                    trade.status = "INVALIDATED"; trade.close_price = price
                    trade.close_time = now; trade.close_epoch = time.time()
                    trade.close_reason = signal.reason
                    self._persist_trades(ticker, state)
                    events.append({"msg": f"\U0001f6d1 TRADE INVALIDATED — {trade.direction}\n\n{signal.reason}\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\nDon't hope. Close it.", "type": "exit", "priority": 5, "alert_key": f"exit_inv_{trade.trade_id}"})
            # HOLD = do nothing
        return events

    def _exit_alert_ok(self, trade, key, now):
        """Check and set cooldown for exit alerts."""
        last = trade.exit_alert_history.get(key)
        if last is not None and (now - last) < EXIT_ALERT_COOLDOWN_SEC:
            return False
        trade.exit_alert_history[key] = now
        return True

    # ── Expire Stale Trades ──
    def _expire_stale_trades(self, state, now):
        changed = False
        now_epoch = time.time()
        for trade in state.active_trades:
            if trade.status in ("CLOSED", "INVALIDATED"):
                continue
            # Use epoch for age — survives restarts
            age = now_epoch - trade.entry_epoch if trade.entry_epoch > 0 else (now - trade.entry_time)
            if age > EXIT_MAX_TRADE_AGE_SEC:
                trade.status = "CLOSED"; trade.close_time = now; trade.close_epoch = now_epoch
                trade.close_reason = "Session expired (4hr max)"
                # close_price: use last known price from trade entry — current spot is not
                # available in this scope. _monitor_exits() handles price-aware closes;
                # this path is purely a safety expiry for genuinely abandoned trades.
                close_px = trade.entry_price
                trade.close_price = close_px
                _log_trade_event(trade, event="CLOSE", close_price=close_px,
                                  close_reason="Session expired (4hr max)",
                                  badge=_trade_quality_badge(trade))
                log.info(f"Trade expired: {trade.ticker} {trade.direction} @ ${trade.entry_price:.2f}")
                changed = True
        # Prune trades older than 8 hours (keep history but don't bloat)
        before = len(state.active_trades)
        state.active_trades = [
            t for t in state.active_trades
            if (now_epoch - t.entry_epoch if t.entry_epoch > 0 else (now - t.entry_time)) < 28800
        ]
        if len(state.active_trades) < before:
            changed = True
        return changed

    # ── Trade Persistence ──
    def _persist_trades(self, ticker, state):
        if not self._store_set:
            return
        try:
            now_mono = time.monotonic()
            trades_data = []
            for t in state.active_trades:
                # Convert exit_alert_history monotonic timestamps to seconds-ago
                eah_relative = {}
                for k, v in t.exit_alert_history.items():
                    eah_relative[k] = round(now_mono - v, 1) if v is not None else None
                trades_data.append({
                    "trade_id": t.trade_id,
                    "ticker": t.ticker, "direction": t.direction, "entry_type": t.entry_type,
                    "entry_price": t.entry_price, "stop_level": t.stop_level,
                    "targets": t.targets, "status": t.status,
                    "entry_epoch": t.entry_epoch, "entry_time_str": t.entry_time_str,
                    "level_name": t.level_name, "gex_at_entry": t.gex_at_entry,
                    "regime": t.regime, "time_phase": t.time_phase,
                    "prior_day_context": t.prior_day_context,
                    "bias_at_entry": t.bias_at_entry,
                    "volatility_regime": t.volatility_regime,
                    "trade_type_label": t.trade_type_label,
                    "setup_score": t.setup_score, "setup_label": t.setup_label,
                    "exit_policy_name": t.exit_policy_name, "scale_advice": t.scale_advice,
                    "level_tier": t.level_tier, "validation_summary": t.validation_summary,
                    "policy_config": t.policy_config,
                    "max_favorable": t.max_favorable, "min_favorable": t.min_favorable,
                    "scaled_at_price": t.scaled_at_price,
                    "trail_stop": t.trail_stop, "close_price": t.close_price,
                    "close_reason": t.close_reason, "close_epoch": t.close_epoch,
                    "last_exit_bar_ts": t.last_exit_bar_ts,
                    "exit_alert_history_rel": eah_relative,
                })
            self._store_set(f"active_trades:{ticker}", json.dumps(trades_data), ttl=86400)
        except Exception as e:
            log.warning(f"Trade persist failed for {ticker}: {e}")

    def _load_trades_from_store(self, ticker, state):
        if not self._store_get:
            return
        try:
            raw = self._store_get(f"active_trades:{ticker}")
            if not raw:
                return
            now_mono = time.monotonic()
            now_epoch = time.time()
            trades_data = json.loads(raw)
            for td in trades_data:
                # Reconstruct monotonic entry_time from epoch for in-process age math
                entry_epoch = td.get("entry_epoch", 0)
                if entry_epoch > 0:
                    entry_time = now_mono - (now_epoch - entry_epoch)
                else:
                    entry_time = td.get("entry_time", 0)  # legacy fallback
                close_epoch = td.get("close_epoch", 0)
                if close_epoch > 0:
                    close_time = now_mono - (now_epoch - close_epoch)
                else:
                    close_time = td.get("close_time", 0)
                trade = ActiveTrade(
                    ticker=td.get("ticker", ticker),
                    direction=td.get("direction", "LONG"),
                    entry_type=td.get("entry_type", "BREAK"),
                    entry_price=td.get("entry_price", 0),
                    stop_level=td.get("stop_level", 0),
                    trade_id=td.get("trade_id", str(uuid.uuid4())[:12]),
                    targets=td.get("targets", []),
                    status=td.get("status", "OPEN"),
                    entry_time=entry_time,
                    entry_epoch=entry_epoch,
                    entry_time_str=td.get("entry_time_str", ""),
                    level_name=td.get("level_name", ""),
                    gex_at_entry=td.get("gex_at_entry", "positive"),
                    regime=td.get("regime", ""),
                    time_phase=td.get("time_phase", ""),
                    prior_day_context=td.get("prior_day_context", "NORMAL"),
                    bias_at_entry=td.get("bias_at_entry", ""),
                    volatility_regime=td.get("volatility_regime", ""),
                    trade_type_label=td.get("trade_type_label", ""),
                    setup_score=td.get("setup_score", 0),
                    setup_label=td.get("setup_label", ""),
                    exit_policy_name=td.get("exit_policy_name", ""),
                    scale_advice=td.get("scale_advice", ""),
                    level_tier=td.get("level_tier", "C"),
                    validation_summary=td.get("validation_summary", ""),
                    policy_config=td.get("policy_config", {}),
                    max_favorable=td.get("max_favorable", 0),
                    min_favorable=td.get("min_favorable", 0),
                    scaled_at_price=td.get("scaled_at_price"),
                    trail_stop=td.get("trail_stop"),
                    close_price=td.get("close_price"),
                    close_reason=td.get("close_reason", ""),
                    close_time=close_time,
                    close_epoch=close_epoch,
                    last_exit_bar_ts=td.get("last_exit_bar_ts", 0),
                )
                # Reconstruct exit_alert_history from relative offsets
                eah_rel = td.get("exit_alert_history_rel", {})
                for k, secs_ago in eah_rel.items():
                    if secs_ago is not None:
                        trade.exit_alert_history[k] = now_mono - secs_ago
                state.active_trades.append(trade)
            log.info(f"Loaded {len(trades_data)} trades from store for {ticker}")
        except Exception as e:
            log.warning(f"Trade load failed for {ticker}: {e}")

    # ── Manual Trade Management ──
    def close_trade(self, ticker, price=None, reason="Manual close"):
        """Close the most recent open trade for a ticker."""
        with self._lock:
            self.get_thesis(ticker)  # trigger lazy-load
            state = self._states.setdefault(ticker, MonitorState())
            if not state.active_trades:
                self._load_trades_from_store(ticker, state)
            open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
            if not open_trades:
                return "No open trades."
            trade = open_trades[-1]
            trade.status = "CLOSED"; trade.close_reason = reason
            trade.close_price = price; trade.close_time = time.monotonic(); trade.close_epoch = time.time()
            _log_trade_event(trade, event="CLOSE", close_price=price,
                              close_reason=reason,
                              badge=_trade_quality_badge(trade))
            self._persist_trades(ticker, state)
            pnl = ""
            if price and trade.entry_price:
                pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
                pnl = f" ({pnl_pct:+.2f}%)"
            return f"Closed {trade.direction} {trade.entry_type} @ ${trade.entry_price:.2f}{pnl}. Reason: {reason}"

    # ── Format Active Trades for Display ──
    def format_trades(self, ticker, price=None):
        thesis = self.get_thesis(ticker)  # triggers lazy-load + trade recovery
        if not thesis:
            return f"📊 {ticker}: No thesis. Run /em."
        state = self._states.setdefault(ticker, MonitorState())
        # Load from store if empty
        if not state.active_trades:
            self._load_trades_from_store(ticker, state)
        open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
        closed_trades = [t for t in state.active_trades if t.status in ("CLOSED", "INVALIDATED")]
        lines = [f"📊 {ticker} ACTIVE TRADES"]
        if not open_trades and not closed_trades:
            lines.append("No trades. Waiting for entry signals.")
            return "\n".join(lines)
        for t in open_trades:
            status_emoji = {"OPEN": "🟢", "SCALED": "💰", "TRAILED": "🏃"}.get(t.status, "⚪")
            pnl = ""
            if price and t.entry_price:
                pnl_pct = ((price - t.entry_price) / t.entry_price * 100) if t.direction == "LONG" else ((t.entry_price - price) / t.entry_price * 100)
                pnl = f" | P&L: {pnl_pct:+.2f}%"
            stop_ref = t.trail_stop if t.trail_stop else t.stop_level
            score_str = f" [{t.setup_score}/5 {t.level_tier}]" if t.setup_score else ""
            lines.append(f"\n{status_emoji} {t.direction} {t.entry_type} — {t.status}{score_str}")
            lines.append(f"  Entry: ${t.entry_price:.2f} ({t.entry_time_str})")
            lines.append(f"  Stop: ${stop_ref:.2f}{pnl}")
            if t.targets:
                tgt_str = " → ".join([f"${tg:.2f}" for tg in t.targets[:3]])
                lines.append(f"  Targets: {tgt_str}")
            if t.exit_policy_name:
                lines.append(f"  Policy: {t.exit_policy_name} | Scale: {t.scale_advice}")
            elif t.trade_type_label:
                lines.append(f"  Type: {t.trade_type_label}")
            context_bits = [b for b in [t.time_phase, t.gex_at_entry, t.prior_day_context, t.regime] if b]
            if context_bits:
                lines.append(f"  Context: {' | '.join(context_bits)}")
            if t.max_favorable > 0:
                mfe_pct = t.max_favorable / t.entry_price * 100
                lines.append(f"  Best: +{mfe_pct:.2f}%")
        if closed_trades:
            recent = closed_trades[-3:][::-1]
            lines.append(f"\n— Recent Closed ({len(closed_trades)} total) —")
            for t in recent:
                status_emoji = "🛑" if t.status == "INVALIDATED" else "⏹"
                pnl = ""
                if t.close_price and t.entry_price:
                    pnl_pct = ((t.close_price - t.entry_price) / t.entry_price * 100) if t.direction == "LONG" else ((t.entry_price - t.close_price) / t.entry_price * 100)
                    pnl = f" ({pnl_pct:+.2f}%)"
                cp = t.close_price if t.close_price else 0
                lines.append(f"  {status_emoji} {t.direction} ${t.entry_price:.2f}→${cp:.2f}{pnl} [{t.close_reason}]")
        return "\n".join(lines)

    def build_guidance(self, ticker, price):
        thesis = self.get_thesis(ticker)
        if not thesis: return [{"text": f"No thesis for {ticker}. Run /em first.", "type": "neutral"}]
        state = self._states.setdefault(ticker, MonitorState())
        g = []; lvl = thesis.levels; tp = _get_time_phase_ct()
        g.append({"text": f"THESIS: {thesis.bias} ({thesis.bias_score}/14) | GEX {thesis.gex_sign} ({thesis.gex_value:+.1f}M) | {thesis.regime}", "type": "context"})
        g.append({"text": f"{tp['label']}: {tp['note']}", "type": "time"})
        if lvl.gamma_flip is not None:
            if price > lvl.gamma_flip: g.append({"text": f"ABOVE gamma flip ${lvl.gamma_flip:.2f} — bullish. Dealers buy dips.", "type": "bullish"})
            else: g.append({"text": f"BELOW gamma flip ${lvl.gamma_flip:.2f} — bearish/trending. Breakdowns accelerate.", "type": "bearish"})
        if lvl.pin_zone_low is not None and lvl.pin_zone_high is not None and lvl.pin_zone_low <= price <= lvl.pin_zone_high and thesis.gex_sign == "positive":
            g.append({"text": f"INSIDE PIN ZONE ${lvl.pin_zone_low:.2f}-${lvl.pin_zone_high:.2f} with GEX+. Trade failures, not breakouts.", "type": "warning"})
        if lvl.micro_trigger_up is not None and lvl.micro_trigger_down is not None:
            if lvl.micro_trigger_down < price < lvl.micro_trigger_up:
                g.append({"text": f"NO-MAN'S LAND ${lvl.micro_trigger_down:.2f}-${lvl.micro_trigger_up:.2f}. Wait for trigger.", "type": "neutral"})
        mm = {"ACCELERATING_UP": ("Momentum ACCELERATING UP — trail stops.", "bullish"), "ACCELERATING_DOWN": ("Momentum ACCELERATING DOWN — don't catch knife.", "bearish"), "LOSING_UPSIDE_MOMENTUM": ("Upside momentum fading. Tighten if long.", "warning"), "LOSING_DOWNSIDE_MOMENTUM": ("Downside momentum fading. Tighten if short.", "warning"), "STALLING": ("Price STALLING — traps happen here.", "neutral")}
        if state.momentum in mm: t, ty = mm[state.momentum]; g.append({"text": t, "type": ty})
        intraday = IntradayLevelTracker.get_active_levels(state, price)
        ids, idr = intraday["support"], intraday["resistance"]
        if ids or idr:
            g.append({"text": "— INTRADAY LEVELS —", "type": "divider"})
            if ids:
                act = "Failed break = squeeze." if thesis.gex_sign == "positive" else "Break with momentum = short."
                g.append({"text": f"Support: ${ids.price:.2f} ({ids.source.replace('_',' ')}, {ids.touches}x). {act}", "type": "info"})
            if idr:
                act = "Failed break = fade." if thesis.gex_sign == "positive" else "Break with momentum = long."
                g.append({"text": f"Resistance: ${idr.price:.2f} ({idr.source.replace('_',' ')}, {idr.touches}x). {act}", "type": "info"})
        if state.session_high is not None and state.session_low is not None:
            g.append({"text": f"Session: ${state.session_low:.2f}-${state.session_high:.2f} (${state.session_high - state.session_low:.2f} wide)", "type": "context"})
        # Show active trade status in guidance
        open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
        if open_trades:
            g.append({"text": "— ACTIVE TRADES —", "type": "divider"})
            for t in open_trades:
                pnl_pct = ((price - t.entry_price) / t.entry_price * 100) if t.direction == "LONG" else ((t.entry_price - price) / t.entry_price * 100)
                stop_ref = t.trail_stop if t.trail_stop else t.stop_level
                ty = "bullish" if pnl_pct > 0 else "bearish"
                g.append({"text": f"{t.direction} {t.entry_type} @ ${t.entry_price:.2f} ({pnl_pct:+.2f}%) | stop ${stop_ref:.2f} | {t.status}", "type": ty})
                if t.targets:
                    g.append({"text": f"  Targets: {' → '.join([f'${tg:.2f}' for tg in t.targets[:3]])}", "type": "info"})
        if state.failed_moves:
            lf = state.failed_moves[-1]; age = (time.monotonic() - lf.break_time) / 60
            if age < 30:
                if lf.direction == "DOWN": g.append({"text": f"🔥 ACTIVE SQUEEZE at ${lf.level:.2f}. Shorts trapped. Bias LONG. Stop below.", "type": "critical"})
                else: g.append({"text": f"🔥 ACTIVE FADE at ${lf.level:.2f}. Longs trapped. Bias SHORT. Stop above.", "type": "critical"})
        elif state.status == "BREAK_IN_PROGRESS":
            g.append({"text": "⏳ BREAK IN PROGRESS — wait 2-3 candles for confirm or failure.", "type": "warning"})
        g.append({"text": "— WHAT TO WATCH —", "type": "divider"})
        ns = ids.price if ids else lvl.local_support; nr = idr.price if idr else lvl.local_resistance
        if nr is not None and ns is not None:
            dr, ds = nr - price, price - ns
            if dr < ds: g.append({"text": f"Nearest: resistance ${nr:.2f} ({dr:.2f} away). Reject → short. Break+hold → long.", "type": "info"})
            else: g.append({"text": f"Nearest: support ${ns:.2f} ({ds:.2f} away). Bounce → long. Break+fail → squeeze.", "type": "info"})
        if thesis.gex_sign == "positive":
            if tp["phase"] in ("POWER_HOUR", "CLOSE"): g.append({"text": "GEX+ near close = max pinning. Fade extremes.", "type": "time"})
            g.append({"text": "GEX+ reminder: Failed moves > continuation.", "type": "context"})
        else: g.append({"text": "GEX- reminder: Moves ACCELERATE. Respect breaks. Wider stops.", "type": "context"})
        return g

    def _evaluate_momentum(self, state, price, bm=None):
        events = []
        # v2.0: Bars are the single truth source for momentum.
        # No bars = stay at current momentum state, produce no events.
        if not bm or len(bm.state.bars_5m) < 3:
            return events
        bars = bm.state.bars_5m[-MONITOR_MOMENTUM_LOOKBACK:]
        recent = [b.close for b in bars]
        if len(recent) < 3: return events
        diffs = [recent[i] - recent[i-1] for i in range(1, len(recent))]
        avg_d = sum(diffs)/len(diffs); last_d = diffs[-1]; old = state.momentum
        thr = recent[-1] * (MONITOR_STALL_THRESHOLD_PCT / 100)
        consec = bm.state.metrics.consecutive_direction
        expansion = bm.state.metrics.expansion_ratio
        if abs(avg_d) < thr and expansion < 0.8:
            state.momentum = "STALLING"
        elif consec >= 2 and expansion > 1.3:
            state.momentum = "ACCELERATING_UP"
        elif consec <= -2 and expansion > 1.3:
            state.momentum = "ACCELERATING_DOWN"
        elif consec >= 1 and avg_d > 0:
            state.momentum = "DRIFTING_UP"
        elif consec <= -1 and avg_d < 0:
            state.momentum = "DRIFTING_DOWN"
        elif avg_d > 0 and last_d <= 0:
            state.momentum = "LOSING_UPSIDE_MOMENTUM"
        elif avg_d < 0 and last_d >= 0:
            state.momentum = "LOSING_DOWNSIDE_MOMENTUM"
        else:
            state.momentum = "STALLING"
        if state.momentum != old:
            if state.momentum == "LOSING_UPSIDE_MOMENTUM" and old in ("ACCELERATING_UP", "DRIFTING_UP"):
                events.append({"msg": "⚠️ Upside momentum fading. Tighten if long.", "type": "warning", "priority": 4, "alert_key": "mom_fade_up"})
            elif state.momentum == "LOSING_DOWNSIDE_MOMENTUM" and old in ("ACCELERATING_DOWN", "DRIFTING_DOWN"):
                events.append({"msg": "⚠️ Downside momentum fading. Tighten if short.", "type": "warning", "priority": 4, "alert_key": "mom_fade_dn"})
        return events

    def _detect_breaks(self, thesis, state, price, prev_price, now, bar=None, bm=None):
        events = []; lvl = thesis.levels; wl = []
        if lvl.local_support is not None: wl.append((lvl.local_support, "daily_support", True))
        if lvl.local_resistance is not None: wl.append((lvl.local_resistance, "daily_resistance", False))
        if lvl.range_break_down is not None and lvl.range_break_down != lvl.local_support: wl.append((lvl.range_break_down, "range_break_down", True))
        if lvl.range_break_up is not None and lvl.range_break_up != lvl.local_resistance: wl.append((lvl.range_break_up, "range_break_up", False))
        if lvl.put_wall is not None and lvl.put_wall != lvl.local_support: wl.append((lvl.put_wall, "put_wall", True))
        if lvl.call_wall is not None and lvl.call_wall != lvl.local_resistance: wl.append((lvl.call_wall, "call_wall", False))
        tol = price * INTRADAY_ZONE_TOLERANCE_PCT / 100
        for il in state.intraday_levels:
            if not il.active: continue
            if any(abs(il.price - d) <= tol for d, _, _ in wl): continue
            # Suppress session extremes during confirmed trends
            if il.source == "session_low" and state.active_trend_direction == "SHORT": continue
            if il.source == "session_high" and state.active_trend_direction == "LONG": continue
            # Suppress fresh session extremes — a level is only meaningful S/R
            # if price has moved away from it. Require that the level hasn't been
            # updated (retouched) in the last 120s, proving price departed and the
            # extreme held.
            if il.source in ("session_low", "session_high"):
                stale_time = now - il.last_touched_ts
                if stale_time < 120: continue  # still being actively set/updated
            # Suppress session extremes during consistent directional momentum
            if il.source == "session_low" and state.momentum in ("ACCELERATING_DOWN", "DRIFTING_DOWN"): continue
            if il.source == "session_high" and state.momentum in ("ACCELERATING_UP", "DRIFTING_UP"): continue
            wl.append((il.price, f"intraday_{il.kind} ({il.source.replace('_',' ')})", il.kind == "support"))
        for level, name, is_sup in wl:
            if is_sup and prev_price >= level and price < level:
                # v2.0: If bar available, require bar CLOSE through, not just wick
                if bar and bar.wicked_through(level, "DOWN"):
                    log.info(f"Break [{name}] ${level:.2f}: wick-only, not bar close — skipping")
                    continue
                # Skip if a pending (non-expired) break already exists at this level+direction
                if any(
                    abs(ba.level - level) <= tol
                    and ba.direction == "DOWN"
                    and not ba.detected_as_failed
                    and not ba.detected_as_confirmed
                    and (now - ba.break_time) <= MONITOR_MAX_BREAK_AGE_SEC
                    for ba in state.break_attempts
                ):
                    continue
                state.break_attempts.append(BreakAttempt(level=level, level_name=name, direction="DOWN", break_price=price, break_time=now, break_bar_index=bm.state.bars_since_open if bm else -1))
                state.status = "BREAK_IN_PROGRESS"
                events.append({"msg": f"🔻 BREAK ATTEMPT: below ${level:.2f} ({name}). Watching follow-through or reclaim.", "type": "alert", "priority": 4, "alert_key": f"brk_dn_{name}_{level:.2f}"})
            elif not is_sup and prev_price <= level and price > level:
                # v2.0: If bar available, require bar CLOSE through, not just wick
                if bar and bar.wicked_through(level, "UP"):
                    log.info(f"Break [{name}] ${level:.2f}: wick-only, not bar close — skipping")
                    continue
                if any(
                    abs(ba.level - level) <= tol
                    and ba.direction == "UP"
                    and not ba.detected_as_failed
                    and not ba.detected_as_confirmed
                    and (now - ba.break_time) <= MONITOR_MAX_BREAK_AGE_SEC
                    for ba in state.break_attempts
                ):
                    continue
                state.break_attempts.append(BreakAttempt(level=level, level_name=name, direction="UP", break_price=price, break_time=now, break_bar_index=bm.state.bars_since_open if bm else -1))
                state.status = "BREAK_IN_PROGRESS"
                events.append({"msg": f"🔺 BREAK ATTEMPT: above ${level:.2f} ({name}). Watching follow-through or failure.", "type": "alert", "priority": 4, "alert_key": f"brk_up_{name}_{level:.2f}"})
        return events

    def _detect_confirmed_breaks(self, thesis, state, price, now, bar=None, bm=None):
        events = []; tp = _get_time_phase_ct()
        for ba in state.break_attempts:
            if ba.detected_as_failed or ba.detected_as_confirmed: continue
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC: continue
            req = 2 if tp["phase"] in ("OPEN", "MORNING", "AFTERNOON") else 3
            bars_since_break = (bm.state.bars_since_open - ba.break_bar_index) if (bm and ba.break_bar_index >= 0) else 999
            if bars_since_break < req: continue
            buf = ba.level * (MONITOR_CONFIRM_BUFFER_PCT / 100); net = self._recent_net_move(state, 3, bm=bm)
            if ba.direction == "DOWN":
                ok = price < (ba.level - buf) and net < -(ba.level * 0.0012)
                # v2.0: If bars available, require bar close through, not just spot
                if ok and bm and not bm.bar_closed_through(ba.level, "DOWN", lookback=2):
                    ok = False
                    log.info(f"Confirm [{ba.level_name}] ${ba.level:.2f}: spot below but no bar close — waiting")
            else:
                ok = price > (ba.level + buf) and net > (ba.level * 0.0012)
                if ok and bm and not bm.bar_closed_through(ba.level, "UP", lookback=2):
                    ok = False
                    log.info(f"Confirm [{ba.level_name}] ${ba.level:.2f}: spot above but no bar close — waiting")
            if not ok: continue
            ba.detected_as_confirmed = True; ba.retest_armed = True
            state.confirmed_breaks.append(ba); state.status = "BREAK_CONFIRMED"
            ext = abs(price - ba.level) / ba.level * 100; chase = ext > MONITOR_EXTENSION_LIMIT_PCT
            tp_now = _get_time_phase_ct()

            # ── Hairline stop gate (confirmed breaks) ─────────────────────────
            # On a confirmed break the stop is the broken level (reclaim above/below).
            # If price has only just cleared the level, the stop may be hairline.
            stop_dist_pct = abs(price - ba.level) / price * 100
            is_hairline = stop_dist_pct < MONITOR_HAIRLINE_STOP_PCT
            if is_hairline:
                ba.hairline_holds += 1
                if ba.hairline_holds == 1:
                    events.append({
                        "msg": (
                            f"👁 HAIRLINE STOP — WATCHING STRUCTURE\n\n"
                            f"${ba.level:.2f} broke but stop is only "
                            f"${abs(price - ba.level):.2f} away ({stop_dist_pct:.3f}%).\n"
                            f"Confirming {MONITOR_HAIRLINE_EXTRA_HOLDS} more candles of hold "
                            f"before entry fires.\n\n"
                            f"Watch: price must hold away from ${ba.level:.2f}."
                        ),
                        "type": "warning",
                        "priority": 4,
                        "alert_key": f"hairline_{ba.level:.2f}",
                    })
                    _log_filtered_trade(
                        ticker=ticker, filter_reason="HAIRLINE_GATE",
                        direction="SHORT" if ba.direction == "DOWN" else "LONG",
                        entry_type="BREAK",
                        setup_score=0, gate_summary=f"stop_dist={stop_dist_pct:.4f}%",
                        level_name=ba.level_name, level_tier="C",
                        time_phase=tp_now["phase"], regime=thesis.regime,
                        gex_sign=thesis.gex_sign,
                        volatility_regime=thesis.volatility_regime,
                        prior_day_context=thesis.prior_day_context,
                        bias=thesis.bias,
                        price=price, stop_level=ba.level,
                        stop_dist_pct=stop_dist_pct,
                        badge="",
                    )
                if ba.hairline_holds <= MONITOR_HAIRLINE_EXTRA_HOLDS:
                    ba.detected_as_confirmed = False
                    continue
            # ── End hairline gate ─────────────────────────────────────────────

            dte_line = _dte_guidance(tp_now["phase"])
            if ba.direction == "DOWN":
                state.active_trend_direction = "SHORT"
                if thesis.gex_sign == "negative":
                    tt = "💰 TRADE TYPE: Naked puts — GEX- trend can run."
                    contract_line = _contract_suggestion(_SETUP_BREAKDOWN_FOLLOW, "SHORT", price, thesis, state, phase=tp_now["phase"])
                    if chase:
                        events.append({"msg": f"🟥 BREAKDOWN CONFIRMED — EXTENDED\n\n${ba.level:.2f} ({ba.level_name}) broke. Price {ext:.2f}% past — DON'T CHASE.\nWait for retest of ${ba.level:.2f}.\nSTOP: above ${ba.level:.2f}", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_short_wait_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🟥🟥🟥 BREAKDOWN CONFIRMED — PUTS / SHORT 🟥🟥🟥\n\n${ba.level:.2f} ({ba.level_name}) broke with follow-through.\nGEX NEGATIVE — dealers amplify.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: Reclaim above ${ba.level:.2f}\nTARGET: Next support — let trend work\n\n— TRADE CARD —\n{dte_line}\n{contract_line}", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_short_{ba.level:.2f}"})
                else:
                    events.append({"msg": f"⚠️ BREAKDOWN + FOLLOW-THROUGH at ${ba.level:.2f}\nGEX+ — mean reversion possible. Reclaim = squeeze long.", "type": "warning", "priority": 4, "alert_key": f"ft_dn_{ba.level:.2f}"})
            else:
                state.active_trend_direction = "LONG"
                if thesis.gex_sign == "negative":
                    tt = "💰 TRADE TYPE: Naked calls — GEX- trend can run."
                    contract_line = _contract_suggestion(_SETUP_BREAKOUT_FOLLOW, "LONG", price, thesis, state, phase=tp_now["phase"])
                    if chase:
                        events.append({"msg": f"🟩 BREAKOUT CONFIRMED — EXTENDED\n\n${ba.level:.2f} ({ba.level_name}) broke. Price {ext:.2f}% past — DON'T CHASE.\nWait for retest of ${ba.level:.2f}.\nSTOP: below ${ba.level:.2f}", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_long_wait_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🟩🟩🟩 BREAKOUT CONFIRMED — CALLS / LONG 🟩🟩🟩\n\n${ba.level:.2f} ({ba.level_name}) broke with follow-through.\nGEX NEGATIVE — dealers amplify.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: Lose ${ba.level:.2f}\nTARGET: Next resistance — let trend work\n\n— TRADE CARD —\n{dte_line}\n{contract_line}", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_long_{ba.level:.2f}"})
                else:
                    events.append({"msg": f"⚠️ BREAKOUT + FOLLOW-THROUGH at ${ba.level:.2f}\nGEX+ — mean reversion possible. Lose level = fade short.", "type": "warning", "priority": 4, "alert_key": f"ft_up_{ba.level:.2f}"})
        return events

    def _detect_failed_moves(self, thesis, state, price, now, bm=None):
        events = []; rb = price * (MONITOR_RECLAIM_THRESHOLD_PCT / 100)
        tp = _get_time_phase_ct()
        for ba in state.break_attempts:
            if ba.detected_as_failed or ba.detected_as_confirmed: continue
            bars_since = (bm.state.bars_since_open - ba.break_bar_index) if (bm and ba.break_bar_index >= 0) else 999
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC or bars_since < 2: continue
            reclaimed = (ba.direction == "DOWN" and price > ba.level + rb) or (ba.direction == "UP" and price < ba.level - rb)
            if reclaimed:
                if not ba.reclaim_seen:
                    ba.reclaim_seen = True; ba.reclaim_price = price; ba.reclaim_time = now; ba.reclaim_holds = 0; continue
                ba.reclaim_holds += 1
                if ba.reclaim_holds < MONITOR_MIN_HOLD_POLLS_AFTER_RECLAIM: continue
                ba.detected_as_failed = True; ba.retest_armed = True
                state.failed_moves.append(ba); state.status = "FAILED_MOVE_ACTIVE"
                ext = abs(price - ba.level) / ba.level * 100; late = ext > MONITOR_EXTENSION_LIMIT_PCT

                # ── Hairline stop gate ────────────────────────────────────────
                # If the stop (ba.level) is within MONITOR_HAIRLINE_STOP_PCT of
                # entry price, the risk/reward is noise-level. Require
                # MONITOR_HAIRLINE_EXTRA_HOLDS more polls of structure hold
                # before firing the trade card. The alert still posts immediately
                # with a "HAIRLINE — waiting for confirmation" note so the trader
                # can watch the level. Once holds are satisfied the normal
                # entry message fires with the full trade card.
                stop_dist_pct = abs(price - ba.level) / price * 100
                is_hairline = stop_dist_pct < MONITOR_HAIRLINE_STOP_PCT
                if is_hairline:
                    ba.hairline_holds += 1
                    if ba.hairline_holds == 1:
                        # First time we detect hairline — fire the watch alert only
                        events.append({
                            "msg": (
                                f"👁 HAIRLINE STOP — WATCHING STRUCTURE\n\n"
                                f"${ba.level:.2f} reclaimed but stop is only "
                                f"${abs(price - ba.level):.2f} away ({stop_dist_pct:.3f}%).\n"
                                f"Too tight to enter now — confirming {MONITOR_HAIRLINE_EXTRA_HOLDS} "
                                f"more candles of hold.\n\n"
                                f"Watch: price must stay above ${ba.level:.2f}. "
                                f"If it does, trade card fires next poll."
                            ),
                            "type": "warning",
                            "priority": 4,
                            "alert_key": f"hairline_{ba.level:.2f}",
                        })
                        # Log to filtered CSV on first detection so we capture
                        # the full setup context before we know the outcome.
                        _log_filtered_trade(
                            ticker=ticker, filter_reason="HAIRLINE_GATE",
                            direction=direction, entry_type=entry_type,
                            setup_score=0, gate_summary=f"stop_dist={stop_dist_pct:.4f}%",
                            level_name=ba.level_name, level_tier="C",
                            time_phase=tp["phase"], regime=thesis.regime,
                            gex_sign=thesis.gex_sign,
                            volatility_regime=thesis.volatility_regime,
                            prior_day_context=thesis.prior_day_context,
                            bias=thesis.bias,
                            price=price, stop_level=ba.level,
                            stop_dist_pct=stop_dist_pct,
                            badge="",
                        )
                    if ba.hairline_holds <= MONITOR_HAIRLINE_EXTRA_HOLDS:
                        # Still accumulating confirmation — don't fire entry yet.
                        # Reset detected_as_failed so we re-evaluate next poll.
                        ba.detected_as_failed = False
                        continue
                    # Enough holds accumulated — fall through to normal entry below
                # ── End hairline gate ─────────────────────────────────────────

                dte_line = _dte_guidance(tp["phase"])
                if ba.direction == "DOWN":
                    direction = "LONG"
                    rn = ("GEX+ — squeeze probability HIGH." if thesis.gex_sign == "positive" else "GEX- squeeze can run hard.")
                    contract_line = _contract_suggestion(_SETUP_FAILED_BREAKDOWN, direction, price, thesis, state, phase=tp["phase"])
                    if late:
                        events.append({"msg": f"🟩🚀🔥 FAILED BREAKDOWN — SQUEEZE LONG 🟩🚀🔥\n\n${ba.level:.2f} held after reclaim. Shorts trapped.\n⚠️ Extended {ext:.2f}% — DON'T CHASE.\nWait for retest.\nSTOP: below ${ba.level:.2f}\n{rn}\n\n— TRADE CARD —\n{dte_line}\n{contract_line}", "type": "critical", "priority": 5, "alert_key": f"fb_late_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🟩🚀🔥 FAILED BREAKDOWN — SQUEEZE LONG 🟩🚀🔥\n\n${ba.level:.2f} held after reclaim. Shorts trapped.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: Below ${ba.level:.2f}\n{rn}\n\n— TRADE CARD —\n{dte_line}\n{contract_line}", "type": "critical", "priority": 5, "alert_key": f"fb_now_{ba.level:.2f}"})
                else:
                    direction = "SHORT"
                    rn = ("GEX+ — fade probability HIGH." if thesis.gex_sign == "positive" else "GEX- downside can accelerate.")
                    contract_line = _contract_suggestion(_SETUP_FAILED_BREAKOUT, direction, price, thesis, state, phase=tp["phase"])
                    if late:
                        events.append({"msg": f"🟩🚀🔥 FAILED BREAKOUT — FADE SHORT 🟩🚀🔥\n\nLost + held. Longs trapped.\n⚠️ Extended {ext:.2f}% — DON'T CHASE.\nWait for retest.\nSTOP: above ${ba.level:.2f}\n{rn}\n\n— TRADE CARD —\n{dte_line}\n{contract_line}", "type": "critical", "priority": 5, "alert_key": f"fbo_late_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🟩🚀🔥 FAILED BREAKOUT — FADE SHORT 🟩🚀🔥\n\n${ba.level:.2f} lost and held. Longs trapped.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: Above ${ba.level:.2f}\n{rn}\n\n— TRADE CARD —\n{dte_line}\n{contract_line}", "type": "critical", "priority": 5, "alert_key": f"fbo_now_{ba.level:.2f}"})
            else:
                if ba.reclaim_seen and not ba.detected_as_failed:
                    ba.reclaim_seen = False; ba.reclaim_price = None; ba.reclaim_time = 0.0; ba.reclaim_holds = 0
        return events

    def _detect_retests(self, thesis, state, price, now, bm=None):
        events = []; net = self._recent_net_move(state, 3, bm=bm)
        tp = _get_time_phase_ct()
        dte_line = _dte_guidance(tp["phase"])
        for ba in state.break_attempts:
            if not ba.retest_armed or ba.retest_fired: continue
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC: continue
            tol = ba.level * (MONITOR_RETEST_TOLERANCE_PCT / 100)
            if abs(price - ba.level) > tol: continue
            # Side-of-level validation: price must be on correct side for the retest type
            if ba.detected_as_confirmed and ba.direction == "DOWN" and price > ba.level: continue  # short retest must be at/below
            if ba.detected_as_confirmed and ba.direction == "UP" and price < ba.level: continue    # long retest must be at/above
            if ba.detected_as_failed and ba.direction == "DOWN" and price < ba.level: continue     # squeeze retest must be at/above
            if ba.detected_as_failed and ba.direction == "UP" and price > ba.level: continue       # fade retest must be at/below
            if ba.detected_as_confirmed and ba.direction == "DOWN" and net < -(ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Naked puts" if thesis.gex_sign == "negative" else "💰 Put debit spread"
                events.append({"msg": f"🎯 RETEST SHORT ENTRY\n\nBreakdown retested ${ba.level:.2f} and rejecting.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: above ${ba.level:.2f}\n\n— TRADE CARD —\n{dte_line}\n{_contract_suggestion(_SETUP_RETEST_SHORT, 'SHORT', price, thesis, state, phase=tp['phase'])}", "type": "critical", "priority": 5, "alert_key": f"rt_short_{ba.level:.2f}"})
            elif ba.detected_as_confirmed and ba.direction == "UP" and net > (ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Naked calls" if thesis.gex_sign == "negative" else "💰 Call debit spread"
                events.append({"msg": f"🎯 RETEST LONG ENTRY\n\nBreakout retested ${ba.level:.2f} and holding.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: below ${ba.level:.2f}\n\n— TRADE CARD —\n{dte_line}\n{_contract_suggestion(_SETUP_RETEST_LONG, 'LONG', price, thesis, state, phase=tp['phase'])}", "type": "critical", "priority": 5, "alert_key": f"rt_long_{ba.level:.2f}"})
            elif ba.detected_as_failed and ba.direction == "DOWN" and net > (ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Call debit spread" if thesis.gex_sign == "positive" else "💰 Naked calls"
                events.append({"msg": f"🎯 RETEST LONG (squeeze)\n\nFailed breakdown retested ${ba.level:.2f} and holding.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: below ${ba.level:.2f}\n\n— TRADE CARD —\n{dte_line}\n{_contract_suggestion(_SETUP_RETEST_LONG, 'LONG', price, thesis, state, phase=tp['phase'])}", "type": "critical", "priority": 5, "alert_key": f"rt_fl_{ba.level:.2f}"})
            elif ba.detected_as_failed and ba.direction == "UP" and net < -(ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Put debit spread" if thesis.gex_sign == "positive" else "💰 Naked puts"
                events.append({"msg": f"🎯 RETEST SHORT (fade)\n\nFailed breakout retested ${ba.level:.2f} and rejecting.\n\nENTRY: Now @ ~${price:.2f}\nSTOP: above ${ba.level:.2f}\n\n— TRADE CARD —\n{dte_line}\n{_contract_suggestion(_SETUP_RETEST_SHORT, 'SHORT', price, thesis, state, phase=tp['phase'])}", "type": "critical", "priority": 5, "alert_key": f"rt_fs_{ba.level:.2f}"})
        return events

    def _check_gamma_flip(self, thesis, state, price):
        events = []; flip = thesis.levels.gamma_flip
        if flip is None: return events
        above = price > flip
        if state.above_gamma_flip is not None and above != state.above_gamma_flip:
            if above: events.append({"msg": f"📈 RECLAIMED GAMMA FLIP ${flip:.2f} — bullish improving.", "type": "critical", "priority": 5, "alert_key": "gf_reclaim"})
            else: events.append({"msg": f"📉 LOST GAMMA FLIP ${flip:.2f} — bearish pressure.", "type": "critical", "priority": 5, "alert_key": "gf_lost"})
        state.above_gamma_flip = above; return events

    def _apply_cooldowns(self, state, events, now):
        """Suppress duplicate alerts using two layers:
        1. Exact key match — same alert_key within cooldown window.
        2. Zone-family match — for break attempts and level alerts, any alert
           whose price falls within MONITOR_ZONE_CLUSTER_PCT of a recently-fired
           alert of the same family ('brk_dn', 'brk_up', 'id_sup', 'id_res',
           'sharp_sup', 'sharp_res') is treated as a cluster duplicate and skipped.
           This is what eliminates the burst of 4 alerts in 3 seconds on nearby levels.
        """
        # Build a quick lookup: family_prefix → list of (price, last_fired_ts)
        # We store zone fires in alert_history under a synthetic key "zone:{family}:{price:.2f}"
        out = []
        for e in events:
            k = e.get("alert_key", e.get("msg", "")[:40])
            last = state.alert_history.get(k)
            if last is not None and (now - last) < MONITOR_ALERT_COOLDOWN_SEC:
                continue  # exact key cooldown

            # ── Zone-family cluster suppression ──
            # Extract family and price from alert_key patterns like:
            #   brk_dn_{name}_{price}  brk_up_{name}_{price}
            #   id_sup_{price}         id_res_{price}
            #   sharp_sup_{price}      sharp_res_{price}
            suppressed = False
            try:
                parts = k.split("_")
                family = None; level_price = None
                if k.startswith("brk_dn_"):
                    family = "brk_dn"; level_price = float(parts[-1])
                elif k.startswith("brk_up_"):
                    family = "brk_up"; level_price = float(parts[-1])
                elif k.startswith("id_sup_"):
                    family = "id_sup"; level_price = float(parts[-1])
                elif k.startswith("id_res_"):
                    family = "id_res"; level_price = float(parts[-1])
                elif k.startswith("sharp_sup_"):
                    family = "sharp_sup"; level_price = float(parts[-1])
                elif k.startswith("sharp_res_"):
                    family = "sharp_res"; level_price = float(parts[-1])

                if family and level_price:
                    tol = level_price * MONITOR_ZONE_CLUSTER_PCT / 100
                    # Scan alert_history for nearby fires in the same family
                    zone_prefix = f"zone:{family}:"
                    for hist_key, hist_ts in state.alert_history.items():
                        if not hist_key.startswith(zone_prefix):
                            continue
                        if (now - hist_ts) >= MONITOR_ALERT_COOLDOWN_SEC:
                            continue
                        try:
                            hist_price = float(hist_key[len(zone_prefix):])
                            if abs(hist_price - level_price) <= tol:
                                suppressed = True
                                log.debug(f"Zone cluster suppressed: {k} within {MONITOR_ZONE_CLUSTER_PCT}% of {hist_key}")
                                break
                        except ValueError:
                            continue
                    if not suppressed and family:
                        # Register this fire as the zone anchor
                        state.alert_history[f"zone:{family}:{level_price:.2f}"] = now
            except (ValueError, IndexError):
                pass  # non-price alert keys pass through unaffected

            if suppressed:
                continue

            state.alert_history[k] = now
            out.append(e)
        return out

    def format_status(self, ticker):
        thesis = self.get_thesis(ticker)
        if not thesis: return f"📡 {ticker}: No thesis. Run /em."
        state = self._states.setdefault(ticker, MonitorState())
        p = state.price_history[-1]["price"] if state and state.price_history else thesis.spot_at_creation
        lines = [f"📡 {ticker} MONITOR", f"Status: {state.status if state else 'INACTIVE'}", f"Bias: {thesis.bias} ({thesis.bias_score:+d}/14)", f"GEX: {thesis.gex_sign} ({thesis.gex_value:+.1f}M) | {thesis.regime}", f"Price: ${p:.2f}"]
        if state:
            lines.append(f"Momentum: {state.momentum}"); lines.append(f"Checks: {state.check_count}")
            if state.session_high is not None and state.session_low is not None: lines.append(f"Session: ${state.session_low:.2f}-${state.session_high:.2f}")
            al = [l for l in state.intraday_levels if l.active]
            if al: lines.append(f"Levels: {sum(1 for l in al if l.kind=='support')}S / {sum(1 for l in al if l.kind=='resistance')}R")
            if state.confirmed_breaks: lines.append(f"Confirmed: {len(state.confirmed_breaks)}")
            if state.failed_moves: lines.append(f"Failed moves: {len(state.failed_moves)}")
            # Active trades summary
            open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
            if open_trades:
                for t in open_trades:
                    emoji = {"OPEN": "🟢", "SCALED": "💰", "TRAILED": "🏃"}.get(t.status, "⚪")
                    pnl = ""
                    if t.entry_price > 0:
                        pnl_pct = ((p - t.entry_price) / t.entry_price * 100) if t.direction == "LONG" else ((t.entry_price - p) / t.entry_price * 100)
                        pnl = f" ({pnl_pct:+.2f}%)"
                    stop_ref = t.trail_stop if t.trail_stop else t.stop_level
                    score_tag = f" {t.setup_score}/5{t.level_tier}" if t.setup_score else ""
                    lines.append(f"{emoji} {t.direction}{score_tag} @ ${t.entry_price:.2f}{pnl} stop=${stop_ref:.2f}")
        tp = _get_time_phase_ct(); lines.append(f"Phase: {tp['label']}")
        fast = ticker.upper() in MONITOR_FAST_POLL_TICKERS
        lines.append(f"Poll: {MONITOR_POLL_INTERVAL_FAST_SEC if fast else MONITOR_POLL_INTERVAL_SEC}s")
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# PER-TICKER TRADE LOGGER
# ─────────────────────────────────────────────────────────────────────────────
# Appends one CSV row per trade event (OPEN, SCALE, CLOSE) to a per-ticker
# log file. Accumulates live data in the same column format as backtest CSVs
# so live vs backtest can be compared directly.
# ═════════════════════════════════════════════════════════════════════════════

import csv as _csv_mod
import os as _os

_TRADE_LOG_DIR = _os.environ.get("TRADE_LOG_DIR", "/tmp/trade_logs")

def _ensure_log_dir():
    _os.makedirs(_TRADE_LOG_DIR, exist_ok=True)

def _trade_log_path(ticker: str) -> str:
    _ensure_log_dir()
    return _os.path.join(_TRADE_LOG_DIR, f"trades_{ticker.upper()}.csv")

_TRADE_LOG_FIELDS = [
    "ts_utc", "ticker", "event",
    "direction", "entry_type", "setup_score", "setup_label",
    "level_name", "level_tier", "time_phase",
    "regime", "gex_sign", "volatility_regime", "prior_day_context", "bias",
    "entry_price", "stop_level", "close_price", "pnl_pts",
    "close_reason", "exit_policy", "badge",
    "entry_bar_time", "close_bar_time",
    "mfe_pts", "mae_pts",
    "entry_premium", "entry_delta",  # option tracking for premium stop analysis
]

def _log_trade_event(trade, event: str, close_price: float = None,
                     close_reason: str = "", badge: str = ""):
    """Append one row to the per-ticker CSV log file."""
    try:
        path = _trade_log_path(getattr(trade, "ticker", "UNKNOWN"))
        write_header = not _os.path.exists(path)

        pnl = ""
        if close_price is not None and hasattr(trade, "entry_price"):
            raw = close_price - trade.entry_price
            pnl = round(raw if trade.direction == "LONG" else -raw, 4)

        from datetime import datetime, timezone
        row = {
            "ts_utc":           datetime.now(timezone.utc).isoformat(),
            "ticker":           getattr(trade, "ticker",            ""),
            "event":            event,
            "direction":        getattr(trade, "direction",         ""),
            "entry_type":       getattr(trade, "entry_type",        ""),
            "setup_score":      getattr(trade, "setup_score",       ""),
            "setup_label":      getattr(trade, "setup_label",       ""),
            "level_name":       getattr(trade, "level_name",        ""),
            "level_tier":       getattr(trade, "level_tier",        ""),
            "time_phase":       getattr(trade, "time_phase",        ""),
            "regime":           getattr(trade, "regime",            ""),
            "gex_sign":         getattr(trade, "gex_at_entry",      ""),
            "volatility_regime":getattr(trade, "volatility_regime", ""),
            "prior_day_context":getattr(trade, "prior_day_context", ""),
            "bias":             getattr(trade, "bias_at_entry",     ""),
            "entry_price":      getattr(trade, "entry_price",       ""),
            "stop_level":       getattr(trade, "stop_level",        ""),
            "close_price":      close_price if close_price is not None else "",
            "pnl_pts":          pnl,
            "close_reason":     close_reason,
            "exit_policy":      getattr(trade, "exit_policy_name",  ""),
            "badge":            badge,
            "entry_bar_time":   getattr(trade, "entry_time_str",    ""),
            "close_bar_time":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") if close_price else "",
            "mfe_pts":          getattr(trade, "max_favorable",     ""),
            "mae_pts":          getattr(trade, "min_favorable",     ""),
            "entry_premium":    getattr(trade, "entry_premium",     ""),
            "entry_delta":      getattr(trade, "entry_delta",       ""),
        }

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = _csv_mod.DictWriter(f, fieldnames=_TRADE_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    except Exception as e:
        log.warning(f"Trade log write failed: {e}")


# ── Filtered trade log ────────────────────────────────────────────────────────
# Every setup that gets blocked — EntryValidator rejection, hairline gate,
# or any future filter — is written here so we can analyse missed opportunities
# as live data accumulates. Separate file per ticker: filtered_{TICKER}.csv
# Columns intentionally match _TRADE_LOG_FIELDS where possible so the two
# files can be joined/compared directly.

_FILTERED_LOG_FIELDS = [
    "ts_utc", "ticker", "filter_reason",
    "direction", "entry_type", "setup_score", "gate_summary",
    "level_name", "level_tier", "time_phase",
    "regime", "gex_sign", "volatility_regime", "prior_day_context", "bias",
    "price_at_filter", "stop_level", "stop_dist_pct",
    "badge_would_have_been",
]

def _filtered_log_path(ticker: str) -> str:
    _ensure_log_dir()
    return _os.path.join(_TRADE_LOG_DIR, f"filtered_{ticker.upper()}.csv")

def _log_filtered_trade(
    ticker: str,
    filter_reason: str,          # VALIDATOR_REJECTED | HAIRLINE_GATE
    direction: str,
    entry_type: str,
    setup_score,
    gate_summary: str,
    level_name: str,
    level_tier: str,
    time_phase: str,
    regime: str,
    gex_sign: str,
    volatility_regime: str,
    prior_day_context: str,
    bias: str,
    price: float,
    stop_level: float,
    stop_dist_pct: float,
    badge: str = "",
):
    """Append one row to the per-ticker filtered trade CSV.

    Call at every point where a valid setup signal is blocked before entry.
    Does NOT cover trades that enter and then lose — those are in the main CSV.
    """
    try:
        path = _filtered_log_path(ticker)
        write_header = not _os.path.exists(path)

        from datetime import datetime, timezone
        row = {
            "ts_utc":               datetime.now(timezone.utc).isoformat(),
            "ticker":               ticker,
            "filter_reason":        filter_reason,
            "direction":            direction,
            "entry_type":           entry_type,
            "setup_score":          setup_score,
            "gate_summary":         gate_summary,
            "level_name":           level_name,
            "level_tier":           level_tier,
            "time_phase":           time_phase,
            "regime":               regime,
            "gex_sign":             gex_sign,
            "volatility_regime":    volatility_regime,
            "prior_day_context":    prior_day_context,
            "bias":                 bias,
            "price_at_filter":      round(price, 2),
            "stop_level":           round(stop_level, 2),
            "stop_dist_pct":        round(stop_dist_pct, 4),
            "badge_would_have_been": badge,
        }

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = _csv_mod.DictWriter(f, fieldnames=_FILTERED_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    except Exception as e:
        log.warning(f"Filtered trade log write failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# QUALITY BADGE — Module-level functions (callable from engine AND daemon)
# ═════════════════════════════════════════════════════════════════════════════

def _ticker_backtest_profile(ticker: str) -> dict:
    """Ticker-aware quality preferences derived from recent backtests.
    This is used for alert badging only — it does not block trade creation."""
    t = (ticker or "").upper()
    profiles = {
        "QQQ": {
            "preferred_directions": {"SHORT"},
            "preferred_entry_types": {"FAILED"},
            "preferred_gex": {"positive"},
            "preferred_phases": {"MORNING", "POWER_HOUR"},
            "preferred_levels": {
                "daily_resistance",
                "intraday_resistance (rejection zone)",
                "intraday_resistance (sharp move origin)",
            },
            "preferred_prior_context": {"GAP_DOWN"},
            "preferred_biases": set(),
            "preferred_regimes": set(),
            "preferred_tiers": {"A", "B"},
            "score4_ok": False,
            "score4_levels": {"intraday_resistance (rejection zone)", "daily_resistance"},
            "rocket_hits": 5,
            "good_hits": 3,
            "min_score_rocket": 5,
            "min_score_good": 5,
        },
        "GLD": {
            "preferred_directions": {"LONG"},
            "preferred_entry_types": {"BREAK"},
            "preferred_gex": set(),
            "preferred_phases": {"MIDDAY", "AFTERNOON"},
            "preferred_levels": {
                "intraday_support (sharp move origin)",
                "intraday_resistance (sharp move origin)",
                "daily_resistance",
            },
            "preferred_prior_context": {"NORMAL"},
            "preferred_biases": {"BULLISH"},
            "preferred_regimes": {"HIGH_VOL_TREND"},
            "preferred_tiers": {"A", "B"},
            "score4_ok": False,
            "score4_levels": {"intraday_support (sharp move origin)", "daily_support"},
            "rocket_hits": 5,
            "good_hits": 3,
            "min_score_rocket": 5,
            "min_score_good": 5,
        },
        "SLV": {
            "preferred_directions": {"LONG"},
            "preferred_entry_types": {"FAILED"},
            "preferred_gex": {"negative"},
            "preferred_phases": {"MIDDAY", "AFTERNOON"},
            "preferred_levels": {"intraday_support (sharp move origin)"},
            "preferred_prior_context": {"GAP_UP"},
            "preferred_biases": {"BULLISH"},
            "preferred_regimes": set(),
            "preferred_tiers": {"A", "B", "C"},
            "score4_ok": True,
            "score4_levels": set(),
            "rocket_hits": 5,
            "good_hits": 3,
            "min_score_rocket": 4,
            "min_score_good": 4,
        },
        "IWM": {
            "preferred_directions": {"LONG"},
            "preferred_entry_types": {"FAILED"},
            "preferred_gex": {"negative"},
            "preferred_phases": {"MIDDAY", "POWER_HOUR", "CLOSE"},
            "preferred_levels": {"intraday_support (sharp move origin)"},
            "preferred_prior_context": {"NORMAL", "GAP_UP"},
            "preferred_biases": {"BULLISH"},
            "preferred_regimes": set(),
            "preferred_tiers": {"A", "B"},
            "score4_ok": False,
            "score4_levels": {"intraday_support (sharp move origin)", "daily_support"},
            "rocket_hits": 5,
            "good_hits": 3,
            "min_score_rocket": 5,
            "min_score_good": 4,
        },
    }
    default_profile = {
        "preferred_directions": set(),
        "preferred_entry_types": {"FAILED"},
        "preferred_gex": set(),
        "preferred_phases": {"MIDDAY", "POWER_HOUR", "CLOSE"},
        "preferred_levels": {
            "intraday_support (sharp move origin)",
            "daily_support",
            "intraday_resistance (rejection zone)",
        },
        "preferred_prior_context": set(),
        "preferred_biases": set(),
        "preferred_regimes": set(),
        "preferred_tiers": {"A", "B"},
        "score4_ok": False,
        "score4_levels": {"intraday_support (sharp move origin)", "daily_support"},
        "rocket_hits": 4,
        "good_hits": 2,
        "min_score_rocket": 5,
        "min_score_good": 4,
    }
    return profiles.get(t, default_profile)


def _trade_quality_badge(trade) -> str:
    """Ticker-aware quality badge aligned to the recent backtest findings.
    🚀 = elite fit to the ticker profile, ✅ = decent fit, ⚠️ = usable but not preferred."""
    if trade is None:
        return "⚠️"

    profile = _ticker_backtest_profile(getattr(trade, "ticker", ""))

    score = getattr(trade, "setup_score", 0) or 0
    regime = getattr(trade, "regime", "") or ""
    gex = getattr(trade, "gex_at_entry", "") or ""
    phase = getattr(trade, "time_phase", "") or ""
    entry_type = getattr(trade, "entry_type", "") or ""
    level = getattr(trade, "level_name", "") or ""
    tier = getattr(trade, "level_tier", "") or ""
    direction = getattr(trade, "direction", "") or ""
    prior_ctx = getattr(trade, "prior_day_context", "") or ""
    bias = getattr(trade, "bias_at_entry", "") or ""

    # Universal caution rules
    if phase == "OPEN":
        return "⚠️"
    if score <= 3:
        return "⚠️"
    if score == 4 and not profile.get("score4_ok"):
        allowed = profile.get("score4_levels") or set()
        if level not in allowed:
            return "⚠️"

    hits = 0
    critical_misses = 0

    if score >= profile.get("min_score_rocket", 5):
        hits += 1
    elif score >= profile.get("min_score_good", 4):
        hits += 1

    checks = [
        ("preferred_entry_types", entry_type, True),
        ("preferred_directions", direction, True),
        ("preferred_gex", gex, False),
        ("preferred_phases", phase, False),
        ("preferred_levels", level, False),
        ("preferred_prior_context", prior_ctx, False),
        ("preferred_biases", bias, False),
        ("preferred_regimes", regime, False),
        ("preferred_tiers", tier, False),
    ]

    for key, value, critical in checks:
        preferred = profile.get(key) or set()
        if not preferred:
            continue
        if value in preferred:
            hits += 1
        elif critical:
            critical_misses += 1

    if critical_misses == 0 and hits >= profile.get("rocket_hits", 5):
        return "🚀"
    if critical_misses <= 1 and hits >= profile.get("good_hits", 3):
        return "✅"
    return "⚠️"

class ThesisMonitorDaemon:
    def __init__(self, engine, get_spot_fn, post_fn):
        self.engine = engine; self.get_spot = get_spot_fn; self.post_fn = post_fn
        self._enabled = True; self._thread = None; self._stop_event = threading.Event()
        self._last_phase = ""
        self._digest_fired: set = set()  # "PHASE:YYYY-MM-DD" keys — prevents re-firing same phase same day
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="thesis-monitor"); self._thread.start()
        log.info("Thesis monitor daemon started")
    def stop(self): self._stop_event.set(); log.info("Thesis monitor stop requested")
    @property
    def is_running(self): return self._thread is not None and self._thread.is_alive()
    def _run(self):
        log.info(f"Thesis monitor: fast={MONITOR_POLL_INTERVAL_FAST_SEC}s for {MONITOR_FAST_POLL_TICKERS}, normal={MONITOR_POLL_INTERVAL_SEC}s")
        self._cycle_count = 0; self._slow_n = max(1, MONITOR_POLL_INTERVAL_SEC // MONITOR_POLL_INTERVAL_FAST_SEC)
        while not self._stop_event.is_set():
            try: self._poll_cycle()
            except Exception as e: log.error(f"Thesis monitor poll error: {e}", exc_info=True)
            self._cycle_count += 1; self._stop_event.wait(MONITOR_POLL_INTERVAL_FAST_SEC)
    def _poll_cycle(self):
        if not self._enabled: return
        tp = _get_time_phase_ct()
        if tp["phase"] in ("PRE_MARKET", "AFTER_HOURS"): return
        # ── Phase transition digest ──
        current_phase = tp["phase"]
        if current_phase != self._last_phase:
            if current_phase in ("MIDDAY", "AFTERNOON", "POWER_HOUR", "CLOSE"):
                self._fire_phase_digest(current_phase, tp["label"])
            self._last_phase = current_phase
        slow = (self._cycle_count % self._slow_n == 0)
        for ticker in self.engine.get_monitored_tickers():
            fast = ticker.upper() in MONITOR_FAST_POLL_TICKERS
            if not fast and not slow: continue
            thesis = self.engine.get_thesis(ticker)
            if not thesis: continue
            try:
                price = self.get_spot(ticker)
                if not price or price <= 0: continue
                for ev in self.engine.evaluate(ticker, price):
                    if ev.get("priority", 1) >= 4: self._post_alert(ticker, price, ev)
                    else: log.info(f"Monitor [{ticker}]: {ev.get('msg','')}")
            except Exception as e: log.warning(f"Monitor {ticker} failed: {e}")

    def _fire_phase_digest(self, phase: str, label: str):
        """Post a pre-phase digest to Telegram summarising all tickers + key levels + open trades.
        Fires once per phase per calendar day. Power Hour digest includes explicit 1DTE reminder."""
        try:
            from zoneinfo import ZoneInfo
            today_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
        except Exception:
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
        digest_key = f"{phase}:{today_str}"
        if digest_key in self._digest_fired:
            return
        self._digest_fired.add(digest_key)

        lines = [f"📋 ── {label.upper()} DIGEST ──", ""]

        # Power Hour / Close: prominent 1DTE warning goes at the top
        if phase in ("POWER_HOUR", "CLOSE"):
            from datetime import date, timedelta
            try:
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo("America/Chicago")).date()
            except Exception:
                today = datetime.utcnow().date()
            next_day = today + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            lines.append("🚨 DTE REMINDER 🚨")
            lines.append(f"Use TOMORROW's expiry ({next_day.strftime('%m/%d')}) for ALL new entries.")
            lines.append("0DTE contracts will expire worthless at close. DO NOT buy 0DTE now.")
            lines.append("")

        for ticker in self.engine.get_monitored_tickers():
            thesis = self.engine.get_thesis(ticker)
            state = self.engine.get_state(ticker)
            if not thesis:
                continue
            # Current spot (best effort)
            try:
                spot = self.get_spot(ticker) or thesis.spot_at_creation
            except Exception:
                spot = thesis.spot_at_creation

            bias_str = f"{thesis.bias} ({thesis.bias_score:+d}/14)"
            gex_str = f"GEX {'➕' if thesis.gex_sign == 'positive' else '➖'} {thesis.gex_sign.upper()} ({thesis.gex_value:+.1f}M)"
            lines.append(f"── {ticker} @ ${spot:.2f} ──")
            lines.append(f"Bias: {bias_str} | {gex_str}")
            lines.append(f"Regime: {thesis.regime} | Vol: {thesis.volatility_regime}")

            # Key levels
            if state:
                il = IntradayLevelTracker.get_active_levels(state, spot)
                sups = il.get("all_support", [])[:3]
                ress = il.get("all_resistance", [])[:3]
                if sups:
                    lines.append("Support: " + " | ".join(f"${l.price:.2f}({l.source})" for l in sups))
                if ress:
                    lines.append("Resistance: " + " | ".join(f"${l.price:.2f}({l.source})" for l in ress))
                # Open trades
                open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
                if open_trades:
                    lines.append("📌 OPEN TRADES:")
                    for t in open_trades:
                        pnl = ((spot - t.entry_price) if t.direction == "LONG" else (t.entry_price - spot))
                        stop_ref = t.trail_stop if t.trail_stop else t.stop_level
                        lines.append(f"  {t.direction} @ ${t.entry_price:.2f} | Stop ${stop_ref:.2f} | P&L {pnl:+.2f} pts | {t.status}")
            lines.append("")

        lines.append(f"Approach: {_get_time_phase_ct()['note']}")
        lines.append("— Not financial advice —")
        msg = "\n".join(lines)
        try:
            self.post_fn(msg)
            log.info(f"Phase digest posted: {phase}")
        except Exception as e:
            log.error(f"Phase digest post failed: {e}")

    def _ticker_backtest_profile(self, ticker: str) -> dict:
        """Ticker-aware quality preferences derived from recent backtests.
        This is used for alert badging only — it does not block trade creation."""
        t = (ticker or "").upper()
        profiles = {
            "QQQ": {
                "preferred_directions": {"SHORT"},
                "preferred_entry_types": {"FAILED"},
                "preferred_gex": {"positive"},
                "preferred_phases": {"MORNING", "POWER_HOUR"},
                "preferred_levels": {
                    "daily_resistance",
                    "intraday_resistance (rejection zone)",
                    "intraday_resistance (sharp move origin)",
                },
                "preferred_prior_context": {"GAP_DOWN"},
                "preferred_biases": set(),
                "preferred_regimes": set(),
                "preferred_tiers": {"A", "B"},
                "score4_ok": False,
                "score4_levels": {"intraday_resistance (rejection zone)", "daily_resistance"},
                "rocket_hits": 5,
                "good_hits": 3,
                "min_score_rocket": 5,
                "min_score_good": 5,
            },
            "GLD": {
                "preferred_directions": {"LONG"},
                "preferred_entry_types": {"BREAK"},
                "preferred_gex": set(),
                "preferred_phases": {"MIDDAY", "AFTERNOON"},
                "preferred_levels": {
                    "intraday_support (sharp move origin)",
                    "intraday_resistance (sharp move origin)",
                    "daily_resistance",
                },
                "preferred_prior_context": {"NORMAL"},
                "preferred_biases": {"BULLISH"},
                "preferred_regimes": {"HIGH_VOL_TREND"},
                "preferred_tiers": {"A", "B"},
                "score4_ok": False,
                "score4_levels": {"intraday_support (sharp move origin)", "daily_support"},
                "rocket_hits": 5,
                "good_hits": 3,
                "min_score_rocket": 5,
                "min_score_good": 5,
            },
            "SLV": {
                "preferred_directions": {"LONG"},
                "preferred_entry_types": {"FAILED"},
                "preferred_gex": {"negative"},
                "preferred_phases": {"MIDDAY", "AFTERNOON"},
                "preferred_levels": {"intraday_support (sharp move origin)"},
                "preferred_prior_context": {"GAP_UP"},
                "preferred_biases": {"BULLISH"},
                "preferred_regimes": set(),
                "preferred_tiers": {"A", "B", "C"},
                "score4_ok": True,
                "score4_levels": set(),
                "rocket_hits": 5,
                "good_hits": 3,
                "min_score_rocket": 4,
                "min_score_good": 4,
            },
            "IWM": {
                "preferred_directions": {"LONG"},
                "preferred_entry_types": {"FAILED"},
                "preferred_gex": {"negative"},
                "preferred_phases": {"MIDDAY", "POWER_HOUR", "CLOSE"},
                "preferred_levels": {"intraday_support (sharp move origin)"},
                "preferred_prior_context": {"NORMAL", "GAP_UP"},
                "preferred_biases": {"BULLISH"},
                "preferred_regimes": set(),
                "preferred_tiers": {"A", "B"},
                "score4_ok": False,
                "score4_levels": {"intraday_support (sharp move origin)", "daily_support"},
                "rocket_hits": 5,
                "good_hits": 3,
                "min_score_rocket": 5,
                "min_score_good": 4,
            },
        }
        default_profile = {
            "preferred_directions": set(),
            "preferred_entry_types": {"FAILED"},
            "preferred_gex": set(),
            "preferred_phases": {"MIDDAY", "POWER_HOUR", "CLOSE"},
            "preferred_levels": {
                "intraday_support (sharp move origin)",
                "daily_support",
                "intraday_resistance (rejection zone)",
            },
            "preferred_prior_context": set(),
            "preferred_biases": set(),
            "preferred_regimes": set(),
            "preferred_tiers": {"A", "B"},
            "score4_ok": False,
            "score4_levels": {"intraday_support (sharp move origin)", "daily_support"},
            "rocket_hits": 4,
            "good_hits": 2,
            "min_score_rocket": 5,
            "min_score_good": 4,
        }
        return profiles.get(t, default_profile)

    def _trade_quality_badge(self, trade) -> str:
        """Ticker-aware quality badge aligned to the recent backtest findings.
        🚀 = elite fit to the ticker profile, ✅ = decent fit, ⚠️ = usable but not preferred."""
        if trade is None:
            return "⚠️"

        profile = self._ticker_backtest_profile(getattr(trade, "ticker", ""))

        score = getattr(trade, "setup_score", 0) or 0
        regime = getattr(trade, "regime", "") or ""
        gex = getattr(trade, "gex_at_entry", "") or ""
        phase = getattr(trade, "time_phase", "") or ""
        entry_type = getattr(trade, "entry_type", "") or ""
        level = getattr(trade, "level_name", "") or ""
        tier = getattr(trade, "level_tier", "") or ""
        direction = getattr(trade, "direction", "") or ""
        prior_ctx = getattr(trade, "prior_day_context", "") or ""
        bias = getattr(trade, "bias_at_entry", "") or ""

        # Universal caution rules
        if phase == "OPEN":
            return "⚠️"
        if score <= 3:
            return "⚠️"
        if score == 4 and not profile.get("score4_ok"):
            allowed = profile.get("score4_levels") or set()
            if level not in allowed:
                return "⚠️"

        hits = 0
        critical_misses = 0

        if score >= profile.get("min_score_rocket", 5):
            hits += 1
        elif score >= profile.get("min_score_good", 4):
            hits += 1

        checks = [
            ("preferred_entry_types", entry_type, True),
            ("preferred_directions", direction, True),
            ("preferred_gex", gex, False),
            ("preferred_phases", phase, False),
            ("preferred_levels", level, False),
            ("preferred_prior_context", prior_ctx, False),
            ("preferred_biases", bias, False),
            ("preferred_regimes", regime, False),
            ("preferred_tiers", tier, False),
        ]

        for key, value, critical in checks:
            preferred = profile.get(key) or set()
            if not preferred:
                continue
            if value in preferred:
                hits += 1
            elif critical:
                critical_misses += 1

        if critical_misses == 0 and hits >= profile.get("rocket_hits", 5):
            return "🚀"
        if critical_misses <= 1 and hits >= profile.get("good_hits", 3):
            return "✅"
        return "⚠️"

    def _post_alert(self, ticker, price, event):
        tp = _get_time_phase_ct(); state = self.engine.get_state(ticker); thesis = self.engine.get_thesis(ticker)
        is_exit  = event.get("type") == "exit"
        is_entry = event.get("type") in ("critical", "trade_confirmed")
        ev_priority = event.get("priority", 3)

        # Find the most recently opened trade to evaluate quality badge.
        # IMPORTANT: badge is only meaningful when a trade exists to evaluate.
        # If no trade exists (e.g. entry event fired but validator rejected it,
        # or badge is being shown before the trade is persisted), do not default
        # to ⚠️ — that falsely implies a quality judgement on a non-existent trade.
        active_trade = None
        if state and state.active_trades:
            open_trades = [t for t in state.active_trades if t.status in ("OPEN","SCALED","TRAILED")]
            if open_trades:
                active_trade = max(open_trades, key=lambda t: getattr(t, "entry_epoch", 0))

        if is_exit:
            badge = ""
        elif active_trade is not None:
            badge = self._trade_quality_badge(active_trade)
        else:
            # No active trade to evaluate — no quality badge.
            # Show a neutral signal indicator instead of ⚠️ (which would
            # falsely imply the setup is weak rather than unevaluated).
            badge = "📡" if not is_entry else ""

        # ── Star rating line ──
        # P5 entry → 5 filled stars  |  P4 contextual → 4 filled + 1 empty
        if is_entry and ev_priority >= 5:
            star_line = "⭐⭐⭐⭐⭐"
        elif ev_priority == 4:
            star_line = "⭐⭐⭐⭐☆"
        else:
            star_line = None

        # ── Badge prefix ──
        badge_prefix = f"{badge} " if badge else ""

        # ── Header ──
        # Entries get "TRADE ALERT" — an actual trade is being called.
        # Everything else (momentum, break attempts, gamma flip) stays "THESIS ALERT".
        if is_exit:
            header = f"📊 {ticker} TRADE MGMT — ${price:.2f}"
        elif is_entry:
            header = f"{badge_prefix}{ticker} TRADE ALERT — ${price:.2f}"
        else:
            header = f"{badge_prefix}📡 {ticker} THESIS ALERT — ${price:.2f}"

        # Stars go first, then header
        lines = []
        if star_line:
            lines.append(star_line)
        lines += [header, "", event["msg"]]

        # ── Explicit ⚠️ caution block ──
        # Only fires when an actual trade was created AND scored as ⚠️.
        # active_trade being None means no trade to evaluate — no caution block.
        if is_entry and badge == "⚠️" and active_trade is not None:
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("⚠️ CAUTION ⚠️ — REDUCED SIZE")
            lines.append("This setup does NOT match backtest sweet spot.")
            lines.append("• Use HALF normal size or PAPER TRADE only")
            lines.append("• Do not add to position if it goes against you")
            lines.append("• Treat as data collection, not conviction trade")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        if not is_exit and active_trade:
            ctx = [x for x in [active_trade.time_phase, active_trade.gex_at_entry, active_trade.prior_day_context] if x]
            if ctx:
                lines.append(f"Setup context: {' | '.join(ctx)}")
        if not is_exit:
            if state and state.momentum not in ("NEUTRAL", "STALLING"): lines.append(f"Momentum: {state.momentum.replace('_',' ')}")
            if state:
                il = IntradayLevelTracker.get_active_levels(state, price)
                if il["support"]: lines.append(f"Nearest support: ${il['support'].price:.2f}")
                if il["resistance"]: lines.append(f"Nearest resistance: ${il['resistance'].price:.2f}")
        lines.append(""); lines.append(f"Phase: {tp['label']} | {thesis.bias} ({thesis.bias_score:+d}/14)")
        lines.append("— Not financial advice —")
        try: self.post_fn("\n".join(lines)); log.info(f"Alert: {ticker} | {event.get('type','')} | {event.get('msg','')[:80]}")
        except Exception as e: log.error(f"Alert post failed: {e}")

def build_thesis_from_em_card(ticker, spot, bias, eng, em, walls, cagf=None, vix=None, v4_result=None, session_label="", local_walls=None, prior_day_close=None,
                              atm_call_delta=0.0, atm_call_premium=0.0,
                              atm_put_delta=0.0, atm_put_premium=0.0):
    eng = eng or {}; walls = walls or {}; em = em or {}; lw = local_walls or walls or {}; cagf = cagf or {}; vix = vix or {}
    levels = ThesisLevels(gamma_flip=eng.get("flip_price"), local_resistance=lw.get("local_resistance_1") or walls.get("call_wall"), local_support=lw.get("local_support_1") or walls.get("put_wall"), call_wall=walls.get("call_wall"), put_wall=walls.get("put_wall"), gamma_wall=walls.get("gamma_wall"), pin_zone_low=lw.get("pin_zone_low"), pin_zone_high=lw.get("pin_zone_high"), micro_trigger_up=lw.get("local_resistance_1") or walls.get("call_wall"), micro_trigger_down=lw.get("local_support_1") or walls.get("put_wall"), range_break_up=lw.get("pin_zone_high") or walls.get("call_wall"), range_break_down=lw.get("pin_zone_low") or walls.get("put_wall"), pivot=lw.get("pivot"), r1=lw.get("r1"), s1=lw.get("s1"), fib_support=lw.get("fib_support"), fib_resistance=lw.get("fib_resistance"), vpoc=lw.get("vpoc"), max_pain=lw.get("max_pain") or eng.get("max_pain"), em_high=em.get("bull_1sd"), em_low=em.get("bear_1sd"), em_2sd_high=em.get("bull_2sd"), em_2sd_low=em.get("bear_2sd"))
    prior_ctx = "NORMAL"
    if prior_day_close is not None and spot > 0:
        gap = abs(spot - prior_day_close) / prior_day_close * 100
        if gap > 0.5: prior_ctx = "GAP_UP" if spot > prior_day_close else "GAP_DOWN"
    gex_val = eng.get("gex", 0); gex_sign = "positive" if gex_val >= 0 else "negative"
    flip = eng.get("flip_price")
    if flip is not None and spot > 0:
        d = (flip - spot) / spot * 100
        if d > 1.5: gex_sign = "negative"; log.info(f"Thesis GEX overridden: raw {gex_val:+.1f}M but spot {d:.1f}% below flip → negative")
        elif d < -1.5: gex_sign = "positive"
    regime = "UNKNOWN"
    if cagf and cagf.get("regime"): regime = cagf["regime"]
    elif eng.get("is_positive_gex") is not None: regime = "SUPPRESSING" if eng["is_positive_gex"] else "TRENDING"
    try:
        from zoneinfo import ZoneInfo; ts = datetime.now(ZoneInfo("America/Chicago")).isoformat()
    except Exception: ts = datetime.now(timezone.utc).isoformat()
    ctx = ThesisContext(ticker=ticker, bias=bias.get("direction", "NEUTRAL"), bias_score=bias.get("score", 0), gex_sign=gex_sign, gex_value=round(gex_val, 2), dex_value=round(eng.get("dex", 0), 2), vanna_value=round(eng.get("vanna", 0), 2), charm_value=round(eng.get("charm", 0), 2), regime=regime, volatility_regime=v4_result.get("vol_regime", {}).get("label", "NORMAL") if v4_result else "NORMAL", vix=vix.get("vix", 20) if isinstance(vix, dict) else 20, iv=v4_result.get("iv", 0.20) if v4_result else 0.20, prior_day_close=prior_day_close, prior_day_context=prior_ctx, session_label=session_label, levels=levels, created_at=ts, spot_at_creation=spot)
    ctx.atm_call_delta   = float(atm_call_delta   or 0.0)
    ctx.atm_call_premium = float(atm_call_premium or 0.0)
    ctx.atm_put_delta    = float(atm_put_delta    or 0.0)
    ctx.atm_put_premium  = float(atm_put_premium  or 0.0)
    return ctx

_monitor_engine = ThesisMonitorEngine()
_monitor_daemon: Optional[ThesisMonitorDaemon] = None
def get_engine(): return _monitor_engine
def get_daemon(): return _monitor_daemon

# ── Channel routing ──────────────────────────────────────────────────────────
#
#  TWO-CHANNEL DESIGN
#  ──────────────────
#  MAIN channel    (TELEGRAM_CHAT_ID)      → dealer briefs, action guides,
#                                            /em cards, swing + spread trades.
#                                            Posted by app.py — not this module.
#
#  INTRADAY channel (TELEGRAM_CHAT_INTRADAY) → everything ThesisMonitorDaemon
#                                              posts: P4/P5 alerts, trade mgmt,
#                                              phase digests, for ALL tickers.
#
#  To wire this up in app.py:
#
#    import os
#    CHAT_MAIN     = os.environ["TELEGRAM_CHAT_ID"]
#    CHAT_INTRADAY = os.environ["TELEGRAM_CHAT_INTRADAY"]
#
#    def post_main(msg):     send_telegram(msg, chat_id=CHAT_MAIN)
#    def post_intraday(msg): send_telegram(msg, chat_id=CHAT_INTRADAY)
#
#    # Dealer briefs / action guides — use post_main directly in your handlers
#    # Monitor daemon — pass post_intraday
#    init_daemon(get_spot_fn=get_spot, intraday_post_fn=post_intraday, ...)
#
# ────────────────────────────────────────────────────────────────────────────

def init_daemon(get_spot_fn, post_fn=None, store_get_fn=None, store_set_fn=None,
                get_bars_fn=None, intraday_post_fn=None):
    """Initialise and start the thesis monitor daemon.

    Args:
        get_spot_fn:       callable(ticker) -> float
        post_fn:           fallback post callable (used if intraday_post_fn is None).
                           Points at main Telegram channel. Kept for backwards compat.
        intraday_post_fn:  callable(msg) -> None posting to the dedicated intraday
                           channel (TELEGRAM_CHAT_INTRADAY). If supplied, the daemon
                           uses this exclusively. Main-channel posts (dealer briefs,
                           action guides) remain in app.py and are unaffected.
        store_get_fn:      Redis get callable
        store_set_fn:      Redis set callable
        get_bars_fn:       callable(ticker, resolution, countback) -> dict
    """
    global _monitor_daemon
    _monitor_engine._store_get = store_get_fn; _monitor_engine._store_set = store_set_fn
    _monitor_engine._get_bars_fn = get_bars_fn
    # Prefer the explicit intraday channel; fall back to post_fn for backwards compat
    effective_post_fn = intraday_post_fn if intraday_post_fn is not None else post_fn
    if effective_post_fn is None:
        raise ValueError("init_daemon requires either intraday_post_fn or post_fn")
    if intraday_post_fn is not None:
        log.info("Thesis monitor: routing all alerts to INTRADAY channel")
    else:
        log.warning("Thesis monitor: intraday_post_fn not set — posting to main channel (legacy mode)")
    _monitor_daemon = ThesisMonitorDaemon(_monitor_engine, get_spot_fn, effective_post_fn)
    _monitor_daemon.start(); log.info("Thesis monitor daemon initialized"); return _monitor_daemon
