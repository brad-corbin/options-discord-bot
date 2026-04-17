# thesis_monitor.py
# v2.0 Thesis Monitor — Bar-Aware + Confluence Scoring + Policy Engine
# v2.1 (v5.0): Alert hierarchy fixes, VIX-scaled stops, EM guide matching
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

# v5.0: Alert hierarchy + VIX-scaled stops
# v5.1: CRISIS exit framework, circuit breaker, multi-touch break
try:
    from trading_rules import (
        ALERT_SUPPRESS_ON_VALIDATOR_REJECT,
        ALERT_OPPOSITE_DIRECTION_COOLDOWN_SEC,
        ALERT_MOMENTUM_FADE_COOLDOWN_SEC,
        ALERT_DEMOTE_REPEAT_FAILED_MOVE,
        VIX_STOP_SCALE_ENABLED,
        EM_GUIDE_MATCHING_ENABLED,
        get_vix_scaled_min_stop,
        # v5.1 Change 6: Circuit breaker
        CIRCUIT_BREAKER_ENABLED,
        CIRCUIT_BREAKER_MAX_CONSEC_STOPS,
        CIRCUIT_BREAKER_PAUSE_MIN,
        # v5.1 Change 8: CRISIS long option exit framework
        CRISIS_LONG_OPTION_PRIMARY,
        CRISIS_HOLD_WINDOW_MIN,
        CRISIS_PREMIUM_STOP_PCT,
        CRISIS_SCALE_1_PCT,
        CRISIS_SCALE_2_PCT,
        CRISIS_TRAIL_GIVEBACK_PCT,
        CRISIS_FINAL_HOUR_GIVEBACK_PCT,
        CRISIS_FINAL_HOUR_MINUTES,
        CRISIS_0DTE_DELTA_TARGET,
        CRISIS_0DTE_DELTA_RANGE,
        CRISIS_PUTS_ALWAYS_PRIMARY,
        CRISIS_CALLS_REQUIRE_GEX_NEG,
        # v5.1 Change 9: Multi-touch level break
        MULTI_TOUCH_BREAK_ENABLED,
        MULTI_TOUCH_MIN_TOUCHES,
        MULTI_TOUCH_LOOKBACK_MIN,
        MULTI_TOUCH_STOP_ZONE_BUFFER,
        MULTI_TOUCH_CONFIRM_POLLS,
        MULTI_TOUCH_MAX_ACTIVE,
        MULTI_TOUCH_MAX_TOUCHES,
        MULTI_TOUCH_MAX_AGE_MIN,
        MULTI_TOUCH_RECENT_TOUCH_MIN,
        # v5.1 Change 3: Spot-poll confirmation
        BREAK_CONFIRM_POLLS,
    )
except ImportError:
    ALERT_SUPPRESS_ON_VALIDATOR_REJECT = True
    ALERT_OPPOSITE_DIRECTION_COOLDOWN_SEC = 300
    ALERT_MOMENTUM_FADE_COOLDOWN_SEC = 600
    ALERT_DEMOTE_REPEAT_FAILED_MOVE = True
    VIX_STOP_SCALE_ENABLED = False
    EM_GUIDE_MATCHING_ENABLED = False
    def get_vix_scaled_min_stop(ticker, vix=20): return 0.40
    CIRCUIT_BREAKER_ENABLED = False
    CIRCUIT_BREAKER_MAX_CONSEC_STOPS = 2
    CIRCUIT_BREAKER_PAUSE_MIN = 30
    CRISIS_LONG_OPTION_PRIMARY = False
    CRISIS_HOLD_WINDOW_MIN = 15
    CRISIS_PREMIUM_STOP_PCT = 0.45
    CRISIS_SCALE_1_PCT = 0.50
    CRISIS_SCALE_2_PCT = 1.00
    CRISIS_TRAIL_GIVEBACK_PCT = 0.30
    CRISIS_FINAL_HOUR_GIVEBACK_PCT = 0.15
    CRISIS_FINAL_HOUR_MINUTES = 60
    CRISIS_0DTE_DELTA_TARGET = 0.30
    CRISIS_0DTE_DELTA_RANGE = (0.25, 0.35)
    CRISIS_PUTS_ALWAYS_PRIMARY = True
    CRISIS_CALLS_REQUIRE_GEX_NEG = True
    MULTI_TOUCH_BREAK_ENABLED = False
    MULTI_TOUCH_MIN_TOUCHES = 3
    MULTI_TOUCH_LOOKBACK_MIN = 30
    MULTI_TOUCH_STOP_ZONE_BUFFER = 0.10
    MULTI_TOUCH_CONFIRM_POLLS = 3
    MULTI_TOUCH_MAX_ACTIVE = 1
    MULTI_TOUCH_MAX_TOUCHES = 10
    MULTI_TOUCH_MAX_AGE_MIN = 180
    MULTI_TOUCH_RECENT_TOUCH_MIN = 60
    BREAK_CONFIRM_POLLS = 3

MONITOR_POLL_INTERVAL_SEC = 60             # v7.0: was 300 — streaming spots are free, evaluate all tickers every cycle
MONITOR_POLL_INTERVAL_FAST_SEC = 30        # v7.0: was 60 — streaming spots are instant, catch breaks 2x faster
MONITOR_FAST_POLL_TICKERS = [              # v7.0: was just SPY/QQQ — streaming covers all flow tickers for free
    # Indexes
    "SPY", "QQQ", "IWM", "DIA",
    # Mega-cap tech (highest flow volume)
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "TSLA", "GOOGL",
    # Large-cap with heavy options flow
    "NFLX", "COIN", "AVGO", "PLTR", "CRM", "ORCL", "ARM", "SMCI",
    # Financials
    "JPM", "GS",
    # Industrials / Health
    "BA", "CAT", "LLY", "UNH", "MRNA",
    # ETFs
    "GLD", "TLT", "XLF", "XLE", "XLV", "SOXX",
    # Additional active
    "MSTR", "SOFI",
]
MONITOR_ALERT_COOLDOWN_SEC = 300       # v4.3: was 600 (10 min) — 5 min is enough to prevent spam
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
MONITOR_ENTRY_COOLDOWN_SEC   = 300    # minimum 5 minutes between new trade entries per ticker

# ── Gamma Flip Oscillation Gate ───────────────────────────────────────────
# When price chops around the gamma flip, the bot fires alternating
# LONG/SHORT signals that bleed capital.  If the flip has been crossed
# MONITOR_GF_OSCILLATION_MAX times within MONITOR_GF_OSCILLATION_WINDOW_SEC,
# all new entries within MONITOR_GF_OSCILLATION_BLOCK_PCT of the flip are
# blocked until price establishes outside the zone for the full window.
# v4.3: Raised max from 3→5 and narrowed block zone from 0.50→0.35%.
# At 3 crossings / 0.50%, the gate was blocking nearly ALL entries in
# consolidation sessions where SPY oscillates near the flip for hours.
MONITOR_GF_OSCILLATION_MAX       = 5     # crossings to trigger the gate
MONITOR_GF_OSCILLATION_WINDOW_SEC = 1800  # 30 minutes
MONITOR_GF_OSCILLATION_BLOCK_PCT = 0.35  # 0.35% of price (~$2.30 on SPY)

# ── Minimum Hold Bars ─────────────────────────────────────────────────────
# Exit policy Layer 2 (scale / trail / giveback) is suppressed until the
# trade has been held for at least this many bar closes.  Hard stop (Layer 1)
# always remains active.  This prevents the giveback threshold from killing
# trades that need 5-10 minutes to develop.
MONITOR_MIN_HOLD_BARS = 5   # 5 × 1m bars for SPY (5 min); 5 × 5m = 25 min for others

# ── Minimum Stop Distance ────────────────────────────────────────────────
# Hard floor on stop distance.  Any setup whose stop is closer than this
# (in dollars) is rejected outright — the risk/reward is noise-level.
MONITOR_MIN_STOP_DISTANCE = {
    "SPY": 0.40,   # ~$0.40 = 0.06% on SPY ~$655
    "QQQ": 0.40,
    "DEFAULT": 0.30,
}

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
    # Phase 3: live option tracking via streaming
    option_symbol: str = ""      # OCC symbol for streaming subscription
    option_strike: float = 0.0   # strike price
    option_expiry: str = ""      # expiry date YYYY-MM-DD
    # ── v5.1 Change 8: CRISIS long option tracking ────────────────────────
    is_crisis_long_option: bool = False   # True = use CRISIS exit framework
    crisis_phase: str = "HOLD"           # HOLD → SCALE_1 → SCALE_2 → TRAIL
    est_premium: float = 0.0             # current estimated premium
    peak_premium: float = 0.0            # highest est premium (for trailing)
    contracts_remaining: int = 20        # contracts still held (scale 1/3 each time)
    crisis_hold_until: float = 0.0       # epoch — no exit before this (except premium stop)
    crisis_scale1_done: bool = False
    crisis_scale2_done: bool = False
    # ── v5.1 Change 9: Multi-touch level break ────────────────────────────
    is_multi_touch_entry: bool = False    # True = Change 9 structural entry
    consolidation_zone_high: float = 0.0 # top of the range for wide stop
    consolidation_zone_low: float = 0.0  # bottom of the range for wide stop
    touch_count: int = 0                 # how many same-side touches triggered this
    # ── v5.1 Change 6: Bias tracking for circuit breaker ──────────────────
    bias_direction: str = ""             # "bearish" or "bullish" for circuit breaker
    # ── v5.1: Scale-in entry framework ────────────────────────────────
    scale_in_stage: int = 1              # 1=initial 1/3, 2=added on confirm, 3=full
    initial_contracts: int = 7           # 1/3 of 20 — initial position size
    # ── v7: Swing trail monitor (alert-only, no auto-execution) ───
    is_swing_trade: bool = False
    swing_trail_active: bool = False
    swing_mfe_peak_pct: float = 0.0       # highest % move from entry
    swing_trail_level: float = 0.0        # current trail stop price
    swing_giveback_pct: float = 0.40      # 40% giveback default
    swing_min_profit_pct: float = 0.005   # 0.5% activation threshold
    swing_last_alert: str = ""            # last alert type sent
    swing_last_alert_time: float = 0.0    # cooldown tracking

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
    last_entry_time: float = 0.0   # monotonic — time of most recent trade entry
    gamma_flip_crossings: list = field(default_factory=list)  # List[float] — epoch timestamps of flip crosses
    # ── v5.1 Change 1: Real-time ORB from SmartMid ────────────────────────
    orb_high: Optional[float] = None     # 15-min opening range high
    orb_low: Optional[float] = None      # 15-min opening range low
    orb_ready: bool = False              # True after 15 min of data
    orb_prices: list = field(default_factory=list)  # spot polls during ORB window
    # ── v5.1 Change 3: Spot-poll confirmation ─────────────────────────────
    break_confirm_polls: dict = field(default_factory=dict)  # {level_key: consecutive_count}
    # ── v5.1 Change 6: Circuit breaker state ──────────────────────────────
    consec_stops: dict = field(default_factory=dict)    # {"bearish": 0, "bullish": 0}
    cb_paused_until: dict = field(default_factory=dict) # {"bearish": epoch, "bullish": epoch}
    # ── v5.1 Change 9: Multi-touch level break tracking ───────────────────
    multi_touch_confirm_polls: dict = field(default_factory=dict)  # {level_price_str: count}
    multi_touch_consumed_levels: set = field(default_factory=set)  # {price_str} — levels already traded

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

def _dte_guidance(phase: str, volatility_regime: str = "NORMAL", vix: float = 20.0) -> str:
    """Return explicit DTE instruction based on session phase and vol regime.
    v5.1: In CRISIS, morning entries should use 1-2 DTE to survive theta."""
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
    # v5.1: CRISIS morning/midday entries need 1-2 DTE to survive theta
    # A 0DTE OTM option at VIX 25+ loses 15-20% per hour from theta alone.
    # Morning entries need 3-5 hours to develop — 0DTE won't survive.
    if volatility_regime == "CRISIS" and phase in ("OPEN", "MORNING", "MIDDAY"):
        return ("📅 DTE: Use 1-2 DTE — NOT 0DTE.\n"
                "   ⚠️ CRISIS vol + morning entry = theta will destroy 0DTE before the move develops.\n"
                "   1DTE gives the thesis time to work without bleeding premium.")
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
        _SETUP_FAILED_BREAKDOWN:  0.40,  # v4.3: was 0.30
        _SETUP_FAILED_BREAKOUT:   0.40,  # v4.3: was 0.30
        _SETUP_RETEST_LONG:       0.35,
        _SETUP_RETEST_SHORT:      0.35,
        _SETUP_BREAKOUT_FOLLOW:   0.50,
        _SETUP_BREAKDOWN_FOLLOW:  0.50,
    }
    usable_frac = usable_fracs.get(setup_type, 0.40)

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
    # v4.3: EM floor — nearby levels cap width, but can't go below 50% of EM
    # budget. Prevents pin zones from forcing $1-wide spreads.
    candidates = [x for x in [d1, d3] if x is not None and x > 0]
    level_capped = min(candidates) if candidates else 1.0
    em_floor = (d3 * 0.50) if d3 is not None and d3 > 0 else 1.0
    target_move = max(level_capped, em_floor)

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
        _SETUP_FAILED_BREAKDOWN:  3,    # v4.3: was 2 — too narrow for intraday theta
        _SETUP_FAILED_BREAKOUT:   3,    # v4.3: was 2
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
    _HIGH_RES_TICKERS = {                    # v7.0: expanded from SPY/QQQ — streaming makes 1-min bars free
        "SPY", "QQQ", "IWM",                # Indexes — highest priority
        "NVDA", "TSLA", "AMD", "AAPL",      # Mega-cap with heaviest options flow
        "META", "AMZN", "MSFT", "GOOGL",    # Big tech
    }

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
                ns.gamma_flip_crossings = old.gamma_flip_crossings  # v15: preserve oscillation gate state
            self._states[ticker] = ns
            log.info(f"Thesis stored: {ticker} | bias={thesis.bias} score={thesis.bias_score} gex={thesis.gex_sign} regime={thesis.regime}")
            self._persist_thesis(ticker, thesis)

    def _persist_thesis(self, ticker, thesis):
        if not self._store_set: return
        try:
            data = {"ticker": thesis.ticker, "bias": thesis.bias, "bias_score": thesis.bias_score, "gex_sign": thesis.gex_sign, "gex_value": thesis.gex_value, "dex_value": thesis.dex_value, "vanna_value": thesis.vanna_value, "charm_value": thesis.charm_value, "regime": thesis.regime, "volatility_regime": thesis.volatility_regime, "vix": thesis.vix, "iv": thesis.iv, "prior_day_close": thesis.prior_day_close, "prior_day_context": thesis.prior_day_context, "session_label": thesis.session_label, "created_at": thesis.created_at, "spot_at_creation": thesis.spot_at_creation, "levels": asdict(thesis.levels),
                    # v15: persist ATM option data for premium stop
                    "atm_call_delta": thesis.atm_call_delta,
                    "atm_call_premium": thesis.atm_call_premium,
                    "atm_put_delta": thesis.atm_put_delta,
                    "atm_put_premium": thesis.atm_put_premium,
            }
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
            # v15: restore ATM option data for premium stop
            t.atm_call_delta   = float(d.get("atm_call_delta", 0.0) or 0.0)
            t.atm_call_premium = float(d.get("atm_call_premium", 0.0) or 0.0)
            t.atm_put_delta    = float(d.get("atm_put_delta", 0.0) or 0.0)
            t.atm_put_premium  = float(d.get("atm_put_premium", 0.0) or 0.0)
            log.info(f"Thesis loaded from store: {ticker} | bias={t.bias} gex={t.gex_sign} "
                     f"atm_call_δ={t.atm_call_delta:.3f} atm_put_δ={t.atm_put_delta:.3f}")
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
            # Recover ORB from Redis (survives redeploy)
            if not state.orb_ready and _persistent_state:
                try:
                    orb = _persistent_state.get_orb(ticker)
                    if orb and orb.get("high") and orb.get("low"):
                        state.orb_high = orb["high"]
                        state.orb_low = orb["low"]
                        state.orb_ready = True
                        log.info(f"ORB15 recovered from Redis: {ticker} "
                                 f"high=${orb['high']:.2f} low=${orb['low']:.2f}")
                except Exception:
                    pass
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
        # Issue 1: Include tickers with active conviction trades (may not have thesis)
        for t, st in self._states.items():
            if any(tr.status in ("OPEN", "SCALED", "TRAILED") for tr in st.active_trades):
                tickers.add(t)
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
            if not state: return []
            # Allow exit monitoring even without thesis if we have active trades
            # (conviction trades may not have a thesis context)
            has_active_trades = any(t.status in ("OPEN", "SCALED", "TRAILED")
                                   for t in state.active_trades)
            if not thesis and not has_active_trades: return []
            if not thesis:
                # Minimal thesis for exit monitoring only — no entry logic runs
                thesis = ThesisContext(ticker=ticker)
            events = []; now = time.monotonic()
            try:
                from zoneinfo import ZoneInfo; ts = datetime.now(ZoneInfo("America/Chicago")).strftime("%I:%M %p")
            except Exception: ts = datetime.utcnow().strftime("%H:%M")
            # ── v2.0: Bar-aware price source ──
            # v5.1 fix: Do NOT override spot price with bar.close.
            # Bar candles come from UTP (15-minute delayed). Spot comes from
            # IEX (real-time). Use real-time spot for level break detection
            # and exit monitoring. Bar is still passed separately for:
            #   - Wick filtering (prevent false breaks from intrabar spikes)
            #   - Bar-count confirmation (N bars of follow-through)
            #   - Indicator computation (VWAP, volume profile)
            bar = None
            bm = self._get_or_create_bar_manager(ticker)
            if bm:
                bar = bm.update()
                # Bar updates VWAP and indicators internally.
                # Do NOT do: price = bar.close (that's 15 min delayed)
            prev_price = state.price_history[-1]["price"] if state.price_history else None
            state.price_history.append({"price": price, "time_str": ts, "ts_mono": now})
            if len(state.price_history) > 240: state.price_history = state.price_history[-240:]
            state.check_count += 1
            # ── v5.1 Change 1: Real-time ORB from SmartMid polls ──────────
            # Build 15-min opening range from live spot polls. Available at
            # 8:45 AM CT (15 min after 8:30 open). Replaces OR30 from delayed bars.
            if ticker.upper() in self._HIGH_RES_TICKERS and not state.orb_ready:
                tp_now = _get_time_phase_ct()
                _mins_ct = 0
                try:
                    from zoneinfo import ZoneInfo
                    _now_ct = datetime.now(ZoneInfo("America/Chicago"))
                    _mins_ct = _now_ct.hour * 60 + _now_ct.minute
                except Exception:
                    _mins_ct = 0
                # Market open is 8:30 CT = 510 minutes
                if 510 <= _mins_ct <= 525:  # first 15 minutes of session
                    state.orb_prices.append(price)
                    state.orb_high = max(state.orb_high or price, price)
                    state.orb_low = min(state.orb_low or price, price)
                elif _mins_ct > 525 and state.orb_prices:
                    # ORB window closed — finalize
                    state.orb_ready = True
                    orb_range = (state.orb_high or 0) - (state.orb_low or 0)
                    log.info(f"ORB15 ready: {ticker} high=${state.orb_high:.2f} "
                             f"low=${state.orb_low:.2f} range=${orb_range:.2f} "
                             f"({len(state.orb_prices)} polls)")
                    # Persist ORB to Redis (survives redeploy)
                    if _persistent_state and state.orb_high and state.orb_low:
                        try:
                            _persistent_state.save_orb(ticker, state.orb_high, state.orb_low)
                        except Exception:
                            pass
                    # Feed ORB levels into intraday level tracker
                    if state.orb_high:
                        IntradayLevelTracker._upsert_level(
                            state, state.orb_high, "resistance", "ORB15_HIGH",
                            time.time(), touches=2)
                    if state.orb_low:
                        IntradayLevelTracker._upsert_level(
                            state, state.orb_low, "support", "ORB15_LOW",
                            time.time(), touches=2)
                elif _mins_ct > 525 and not state.orb_prices:
                    # v5.1 fix: Thesis stored after ORB window — seed from bar manager
                    # The bar manager may have bars from 8:30-8:45 already.
                    state.orb_ready = True  # mark done so we don't re-check
                    if bm and hasattr(bm, 'state') and hasattr(bm.state, 'or_30'):
                        _or30 = bm.state.or_30
                        if _or30.is_complete and _or30.high > 0 and _or30.low > 0:
                            state.orb_high = _or30.high
                            state.orb_low = _or30.low
                            log.info(f"ORB15 seeded from bar manager OR30: {ticker} "
                                     f"high=${_or30.high:.2f} low=${_or30.low:.2f}")
                            IntradayLevelTracker._upsert_level(
                                state, _or30.high, "resistance", "ORB15_HIGH",
                                time.time(), touches=2)
                            IntradayLevelTracker._upsert_level(
                                state, _or30.low, "support", "ORB15_LOW",
                                time.time(), touches=2)
                        else:
                            log.info(f"ORB15 unavailable: {ticker} — thesis stored "
                                     f"after ORB window, no bar data")
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

            # ── v5.1 Change 9: Multi-touch level break detection ──────────
            if MULTI_TOUCH_BREAK_ENABLED and ticker.upper() in self._HIGH_RES_TICKERS:
                mt_events = self._detect_multi_touch_break(
                    ticker, thesis, state, price, now, registry=registry)
                entry_events.extend(mt_events)

            # ── v5.1: Confluence messaging ─────────────────────────────────
            # When a confirming signal fires while an active trade exists in
            # the same direction, convert it to a confluence annotation instead
            # of firing a new trade card. This prevents confusing double-signals
            # and reinforces the existing trade.
            _active_longs = [t for t in state.active_trades
                            if t.status in ("OPEN", "SCALED", "TRAILED") and t.direction == "LONG"]
            _active_shorts = [t for t in state.active_trades
                             if t.status in ("OPEN", "SCALED", "TRAILED") and t.direction == "SHORT"]
            for ev in entry_events:
                if ev.get("type") not in ("trade_confirmed", "critical"):
                    continue
                if ev.get("priority", 0) < 5:
                    continue
                ak = ev.get("alert_key", "")
                # Determine direction of this event
                _ev_dir = None
                if any(x in ak for x in ("conf_short", "fbo_now", "rt_short", "rt_fs", "mt_break_dn")):
                    _ev_dir = "SHORT"
                elif any(x in ak for x in ("conf_long", "fb_now", "rt_long", "rt_fl", "mt_break_up")):
                    _ev_dir = "LONG"
                if _ev_dir is None:
                    continue
                # Check for active trade in same direction
                _matching = _active_longs if _ev_dir == "LONG" else _active_shorts
                if _matching:
                    _existing = _matching[0]
                    _orig_msg = ev.get("msg", "")
                    # Extract the key info from the original message (first line)
                    _first_line = _orig_msg.split("\n")[0] if "\n" in _orig_msg else _orig_msg[:60]
                    ev["msg"] = (
                        f"📋 CONFLUENCE — adds to active {_ev_dir}\n\n"
                        f"{_first_line}\n\n"
                        f"Active trade: {_existing.direction} @ ${_existing.entry_price:.2f} "
                        f"(stop ${_existing.stop_level:.2f})\n"
                        f"Thesis strengthening — HOLD position.\n"
                        f"Consider adding 1/3 size on this confirmation if not full."
                    )
                    ev["type"] = "info"
                    ev["priority"] = 4  # still visible in Telegram
                    ev["alert_key"] = f"confluence_{ak}"
                    log.info(f"Confluence: {ticker} {_ev_dir} signal {ak} → "
                             f"reinforces active trade {_existing.trade_id}")

            # ── v5.0: Track which events the validator rejects ──
            _rejected_keys = set()

            # Auto-create ActiveTrade — runs through EntryValidator + ExitPolicy
            for ev in entry_events:
                if ev.get("type") in ("trade_confirmed", "critical") and ev.get("priority", 0) >= 5:
                    ak = ev.get("alert_key", "")
                    if "wait" not in ak and "late" not in ak:
                        _trade_created = self._create_trade_from_event(
                            ticker, thesis, state, price, ev, now, ts,
                            registry=registry, bar=bar, bm=bm)
                        # v5.0: If validator rejected, mark this key for downgrade
                        if not _trade_created and ALERT_SUPPRESS_ON_VALIDATOR_REJECT:
                            _rejected_keys.add(ak)

            # ── v5.0: Downgrade rejected critical alerts to info ──
            # This prevents "FADE SHORT 🔥" from firing when the validator
            # just said "score=2, location + momentum failed."
            for ev in entry_events:
                ak = ev.get("alert_key", "")
                if ak in _rejected_keys and ev.get("type") == "critical":
                    ev["type"] = "info"
                    ev["priority"] = 3  # below the threshold=4 for posting
                    ev["msg"] = "⚠️ [Validator rejected] " + ev.get("msg", "")
                    log.info(f"Alert downgraded (validator rejected): {ak}")

            # ── v5.0: Opposite-direction suppression ──
            # After a critical SHORT alert, suppress critical LONG alerts
            # for ALERT_OPPOSITE_DIRECTION_COOLDOWN_SEC, and vice versa.
            _last_critical_dir = getattr(state, '_last_critical_direction', None)
            _last_critical_ts = getattr(state, '_last_critical_ts', 0)
            for ev in entry_events:
                if ev.get("type") == "critical" and ev.get("priority", 0) >= 5:
                    ak = ev.get("alert_key", "")
                    # Determine direction of this alert
                    _ev_dir = None
                    if any(x in ak for x in ("fbo_", "conf_short", "rt_short", "rt_fs")):
                        _ev_dir = "SHORT"
                    elif any(x in ak for x in ("fb_", "conf_long", "rt_long", "rt_fl")):
                        _ev_dir = "LONG"

                    if (_ev_dir and _last_critical_dir and
                            _ev_dir != _last_critical_dir and
                            (now - _last_critical_ts) < ALERT_OPPOSITE_DIRECTION_COOLDOWN_SEC):
                        ev["type"] = "alert"
                        ev["priority"] = 4  # still visible but not "critical"
                        ev["msg"] = ev.get("msg", "").replace("🟩🚀🔥", "⚡")
                        log.info(f"Alert demoted (opposite direction within {ALERT_OPPOSITE_DIRECTION_COOLDOWN_SEC}s): {ak}")
                    elif _ev_dir and ev.get("type") == "critical":
                        state._last_critical_direction = _ev_dir
                        state._last_critical_ts = now

            # ── v5.0: Demote repeat failed-move alerts at same level ──
            if ALERT_DEMOTE_REPEAT_FAILED_MOVE:
                _seen_failed_levels = getattr(state, '_seen_failed_levels', {})
                for ev in entry_events:
                    ak = ev.get("alert_key", "")
                    if ev.get("type") == "critical" and ("fb_" in ak or "fbo_" in ak):
                        # Extract level price from alert key
                        try:
                            _level_str = ak.split("_")[-1]
                            if _level_str in _seen_failed_levels:
                                ev["type"] = "alert"
                                ev["priority"] = 4
                                ev["msg"] = ev.get("msg", "").replace("🟩🚀🔥", "📋").replace("TRADE ALERT", "MGMT NOTE")
                                log.info(f"Alert demoted (repeat failed move at {_level_str}): {ak}")
                            else:
                                _seen_failed_levels[_level_str] = now
                        except Exception:
                            pass
                state._seen_failed_levels = _seen_failed_levels

            # ── v5.0: EM Guide matching annotation ──
            if EM_GUIDE_MATCHING_ENABLED:
                for ev in entry_events:
                    if ev.get("type") == "critical" and ev.get("priority", 0) >= 5:
                        _em_match = self._check_em_guide_match(thesis, state, price, ev)
                        if _em_match:
                            ev["msg"] = f"📋 MATCHES EM GUIDE: {_em_match}\n\n" + ev.get("msg", "")

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

        # ── Fallback targets — never return empty ─────────────────────
        # If structural levels are too far or absent, synthesise reasonable
        # intraday targets from VWAP, gamma flip, or EM boundaries.
        if not targets:
            fallbacks = []
            if direction == "LONG":
                if lvl.gamma_flip and lvl.gamma_flip > price + tol:
                    fallbacks.append(lvl.gamma_flip)
                if lvl.em_high and lvl.em_high > price + tol:
                    fallbacks.append(lvl.em_high)
                if lvl.local_resistance and lvl.local_resistance > price + tol:
                    fallbacks.append(lvl.local_resistance)
                if lvl.call_wall and lvl.call_wall > price + tol:
                    fallbacks.append(lvl.call_wall)
            else:  # SHORT
                if lvl.gamma_flip and lvl.gamma_flip < price - tol:
                    fallbacks.append(lvl.gamma_flip)
                if lvl.em_low and lvl.em_low < price - tol:
                    fallbacks.append(lvl.em_low)
                if lvl.local_support and lvl.local_support < price - tol:
                    fallbacks.append(lvl.local_support)
                if lvl.put_wall and lvl.put_wall < price - tol:
                    fallbacks.append(lvl.put_wall)
            # Deduplicate and sort
            fallbacks = sorted(set(fallbacks))
            if direction == "SHORT":
                targets = fallbacks[-3:][::-1]
            else:
                targets = fallbacks[:3]

        return targets

    # ── Exit Monitor: Auto-Create Trade ──
    def _create_trade_from_event(self, ticker, thesis, state, price, event, now, ts,
                                   registry=None, bar=None, bm=None):
        """Create an ActiveTrade from a clean entry signal. v2.0: validates through EntryValidator.
        v5.0: Returns True if trade was created, False if rejected/blocked."""
        ak = event.get("alert_key", "")

        # ── Entry cooldown — prevent rapid re-entry ───────────────────────────
        # Gamma flip oscillation and clustered level breaks can fire multiple
        # signals within minutes. Enforce a minimum gap between entries.
        time_since_last = now - state.last_entry_time if state.last_entry_time > 0 else float("inf")
        if time_since_last < MONITOR_ENTRY_COOLDOWN_SEC:
            log.info(f"Entry cooldown: {ticker} — {time_since_last:.0f}s since last entry "
                     f"(need {MONITOR_ENTRY_COOLDOWN_SEC}s), skipping {ak}")
            return False

        msg = event.get("msg", "")
        # Parse direction and entry_type from alert_key
        if "conf_short" in ak or "fbo_now" in ak or "rt_short" in ak or "rt_fs" in ak or "mt_break_dn" in ak:
            direction = "SHORT"
        elif "conf_long" in ak or "fb_now" in ak or "rt_long" in ak or "rt_fl" in ak or "mt_break_up" in ak:
            direction = "LONG"
        else:
            log.info(f"Cannot parse direction from alert_key={ak}, skipping trade creation")
            return False
        if "conf_" in ak:
            entry_type = "BREAK"
        elif "fb_" in ak or "fbo_" in ak:
            entry_type = "FAILED"
        elif "rt_" in ak:
            entry_type = "RETEST"
        elif "mt_break_" in ak:
            entry_type = "MULTI_TOUCH"
        else:
            entry_type = "BREAK"
        # Find the stop level

        # ── v5.1 Change 6: Circuit breaker check ─────────────────────────
        # After 2 consecutive stops in the same bias direction, pause that
        # direction for 30 minutes. Tracks bias (bearish/bullish).
        if CIRCUIT_BREAKER_ENABLED:
            _bias_dir = "bearish" if direction == "SHORT" else "bullish"
            _cb_until = state.cb_paused_until.get(_bias_dir, 0)
            if _cb_until > 0 and time.time() < _cb_until:
                _remaining = int((_cb_until - time.time()) / 60)
                # v5.1: Change 9 entries bypass circuit breaker (structural confirmation)
                _is_mt = event.get("_is_multi_touch", False)
                if not _is_mt:
                    log.info(f"Circuit breaker: {ticker} {_bias_dir} paused for "
                             f"{_remaining} more min (2 consecutive stops), blocking {ak}")
                    return False
        stop = 0.0
        level_name = ""
        # v5.1 Change 9: Multi-touch events carry pre-computed stop
        if event.get("_is_multi_touch") and event.get("_mt_stop"):
            stop = event["_mt_stop"]
            level_name = f"MT_BREAK_{event.get('_touch_count', 0)}x"
        else:
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
                return False

        # ── v5.1 Change 9: Block counter-direction when multi-touch active ──
        # If a Change 9 structural trade is open, don't fire entries in the
        # opposite direction — the structural thesis takes priority.
        # Fix C: Release block if trade has been underwater for 30+ minutes.
        for t in state.active_trades:
            if t.status not in ("OPEN", "SCALED", "TRAILED"):
                continue
            if getattr(t, 'is_multi_touch_entry', False) and t.direction != direction:
                # Check if trade is underwater for 30+ min
                _trade_age_min = (time.time() - t.entry_epoch) / 60 if t.entry_epoch > 0 else 0
                _is_underwater = False
                if t.direction == "LONG" and price < t.entry_price:
                    _is_underwater = True
                elif t.direction == "SHORT" and price > t.entry_price:
                    _is_underwater = True
                if _is_underwater and _trade_age_min >= 30:
                    log.info(f"Counter-direction block RELEASED: {ticker} {direction} {ak} — "
                             f"multi-touch {t.direction} trade {t.trade_id} underwater "
                             f"{_trade_age_min:.0f}min (entry ${t.entry_price:.2f} vs spot ${price:.2f})")
                    # Don't block — let the counter-direction trade through
                else:
                    log.info(f"Counter-direction blocked: {ticker} {direction} {ak} — "
                             f"active multi-touch {t.direction} trade {t.trade_id}")
                    return False

        # ── Gamma Flip Oscillation Gate ───────────────────────────────────
        # If price has been whipsawing around gamma flip, block entries near it.
        flip = thesis.levels.gamma_flip
        if flip is not None and len(state.gamma_flip_crossings) >= MONITOR_GF_OSCILLATION_MAX:
            block_zone = price * MONITOR_GF_OSCILLATION_BLOCK_PCT / 100
            if abs(price - flip) <= block_zone:
                log.info(f"GF oscillation gate: {ticker} — {len(state.gamma_flip_crossings)} "
                         f"crossings in {MONITOR_GF_OSCILLATION_WINDOW_SEC}s, price ${price:.2f} "
                         f"within {MONITOR_GF_OSCILLATION_BLOCK_PCT}% of flip ${flip:.2f}, blocking {ak}")
                _log_filtered_trade(
                    ticker=ticker, filter_reason="GF_OSCILLATION_GATE",
                    direction=direction, entry_type=entry_type,
                    setup_score=0, gate_summary=f"gf_crossings={len(state.gamma_flip_crossings)}",
                    level_name=level_name, level_tier="C",
                    time_phase=_get_time_phase_ct()["phase"], regime=thesis.regime,
                    gex_sign=thesis.gex_sign, volatility_regime=thesis.volatility_regime,
                    prior_day_context=thesis.prior_day_context, bias=thesis.bias,
                    price=price, stop_level=stop,
                    stop_dist_pct=abs(price - stop) / price * 100 if price > 0 else 0,
                    badge="",
                )
                return False

        # ── Minimum Stop Distance Gate ────────────────────────────────────
        # Hard floor — if the stop is closer than the minimum for this ticker,
        # the setup is noise-level and gets rejected outright.
        # v5.0: Scale min stop with VIX — at VIX 25, a $0.40 SPY stop is noise.
        if VIX_STOP_SCALE_ENABLED and thesis.vix > 0:
            min_stop = get_vix_scaled_min_stop(ticker, thesis.vix)
        else:
            min_stop = MONITOR_MIN_STOP_DISTANCE.get(ticker.upper(),
                        MONITOR_MIN_STOP_DISTANCE["DEFAULT"])
        stop_dist_abs = abs(price - stop)
        if stop_dist_abs < min_stop:
            log.info(f"Min stop gate: {ticker} — stop ${stop:.2f} is ${stop_dist_abs:.2f} "
                     f"from entry ${price:.2f} (min ${min_stop:.2f}), blocking {ak}")
            _log_filtered_trade(
                ticker=ticker, filter_reason="MIN_STOP_DISTANCE",
                direction=direction, entry_type=entry_type,
                setup_score=0, gate_summary=f"stop_dist=${stop_dist_abs:.2f}<${min_stop:.2f}",
                level_name=level_name, level_tier="C",
                time_phase=_get_time_phase_ct()["phase"], regime=thesis.regime,
                gex_sign=thesis.gex_sign, volatility_regime=thesis.volatility_regime,
                prior_day_context=thesis.prior_day_context, bias=thesis.bias,
                price=price, stop_level=stop,
                stop_dist_pct=abs(price - stop) / price * 100 if price > 0 else 0,
                badge="",
            )
            return False

        # ── v2.0: Level quality from registry ──
        level_quality = 0
        level_tier = "C"
        level_sources = set()
        if event.get("_is_multi_touch"):
            # v5.1 Change 9: multi-touch entries have proven levels
            level_quality = 90
            level_tier = "A"
            level_sources = {"intraday_multi_touch", f"touch_{event.get('_touch_count', 3)}x"}
        elif registry:
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
            return False

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

        # ── Target sanity for mean-reversion setups ───────────────────────────
        # Failed moves and retests should target the first realistic intraday
        # level, not the EM 1σ boundary (which is an all-day boundary, not a
        # meaningful T1 for a 10-20 min fade). If the first target is beyond
        # the EM 1σ it will never get reached before the giveback exit fires,
        # making scale logic meaningless. Cap T1 at EM ±1σ and drop T2+ that
        # are implausibly far for an intraday mean-reversion timeframe.
        if entry_type in ("FAILED", "RETEST") and targets:
            em_high = thesis.levels.em_high
            em_low  = thesis.levels.em_low
            if direction == "SHORT" and em_low and em_low > 0:
                targets = [t for t in targets if t >= em_low]
            elif direction == "LONG" and em_high and em_high > 0:
                targets = [t for t in targets if t <= em_high]
            # If registry gave us only EM-boundary levels (no structural levels
            # closer in), fall back to nearest intraday levels from state
            if not targets or (targets and abs(targets[0] - price) > 3.0):
                intraday_targets = self._find_targets(ticker, thesis, state, price, direction)
                # Keep only intraday levels within 3 pts — realistic for a fade
                intraday_near = [t for t in intraday_targets if abs(t - price) <= 3.0]
                if intraday_near:
                    targets = intraday_near[:2]

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
            # v5.1 Change 6: bias direction for circuit breaker
            bias_direction="bearish" if direction == "SHORT" else "bullish",
            # v5.1 Change 9: multi-touch entry flag
            is_multi_touch_entry=event.get("_is_multi_touch", False),
            touch_count=event.get("_touch_count", 0),
            consolidation_zone_high=event.get("_zone_high", 0.0),
            consolidation_zone_low=event.get("_zone_low", 0.0),
        )

        # ── v5.1 Change 8: CRISIS long option detection ──────────────────
        # When vol regime is CRISIS and conditions are met, flag trade for
        # the CRISIS long option exit framework (scale in thirds, wide trail).
        _is_crisis = thesis.volatility_regime == "CRISIS"
        _is_0dte = tp["phase"] not in ("POWER_HOUR", "CLOSE")
        if CRISIS_LONG_OPTION_PRIMARY and _is_crisis and _is_0dte:
            # Puts: always primary in CRISIS (vol expands on drops)
            # Calls: require GEX negative (squeeze context)
            _use_crisis = False
            if direction == "SHORT":  # bearish = put buyer
                _use_crisis = True  # CRISIS_PUTS_ALWAYS_PRIMARY
            elif direction == "LONG" and thesis.gex_sign == "negative":
                _use_crisis = True  # squeeze/snap-back context
            if _use_crisis:
                trade.is_crisis_long_option = True
                trade.crisis_phase = "HOLD"
                trade.crisis_hold_until = time.time() + (CRISIS_HOLD_WINDOW_MIN * 60)
                # v5.1: Scale-in — start at 1/3 position (7 contracts)
                # Add remaining 2/3 on confluence confirmation signals
                trade.contracts_remaining = 7
                trade.initial_contracts = 7
                trade.scale_in_stage = 1
                # Estimate entry premium from delta × distance to strike
                # For slightly OTM (delta 0.30), premium ≈ delta × 5-6
                _delta_target = CRISIS_0DTE_DELTA_TARGET
                trade.entry_delta = _delta_target
                if trade.entry_premium <= 0:
                    # Approximate: ATM premium at VIX 30 ≈ $3-4 for SPY 0DTE
                    # OTM delta 0.30 ≈ 60-70% of ATM premium
                    trade.entry_premium = round(thesis.vix * 0.06 * 0.65, 2)
                trade.est_premium = trade.entry_premium
                trade.peak_premium = trade.entry_premium
                log.info(f"CRISIS long option armed: {ticker} {direction} "
                         f"δ={_delta_target:.2f} est_prem=${trade.entry_premium:.2f} "
                         f"hold_until={CRISIS_HOLD_WINDOW_MIN}min "
                         f"premium_stop={CRISIS_PREMIUM_STOP_PCT:.0%}")

        # ── Wire ATM delta/premium from thesis snapshot ───────────────────
        # LONG trades use call delta/premium; SHORT trades use put delta/premium.
        # These were captured at em card time from the live chain and stored on
        # the thesis so we can approximate the 20% premium stop each poll
        # without fetching a live option quote.
        if not trade.is_crisis_long_option:  # CRISIS trades use their own premium tracking
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

        # Phase 3: Compute OCC symbol and request streaming subscription
        try:
            from schwab_stream import build_occ_symbol, get_option_symbol_manager, get_live_premium
            _opt_side = "call" if direction == "LONG" else "put"
            _atm = round(price)  # ATM strike
            if _opt_side == "put":
                _atm = round(price) + 1  # slightly ITM for puts per v5.1
            # Find expiry — use today for 0DTE, tomorrow for 1DTE
            _today = date.today()
            if thesis.session_label and "1DTE" in thesis.session_label:
                _exp = _today + timedelta(days=1)
                while _exp.weekday() >= 5:
                    _exp += timedelta(days=1)
            else:
                _exp = _today
            trade.option_expiry = _exp.strftime("%Y-%m-%d")
            trade.option_strike = float(_atm)
            trade.option_symbol = build_occ_symbol(ticker, trade.option_expiry, _opt_side, _atm)
            log.info(f"Phase 3: OCC symbol for trade: {trade.option_symbol}")

            # Request streaming subscription for this symbol
            osm = get_option_symbol_manager()
            if osm:
                osm.subscribe_specific([trade.option_symbol])

            # Try to get live premium from streaming instead of estimate
            live_prem = get_live_premium(trade.option_symbol)
            if live_prem and live_prem > 0:
                trade.entry_premium = live_prem
                trade.est_premium = live_prem
                trade.peak_premium = live_prem
                log.info(f"Phase 3: Live entry premium ${live_prem:.2f} from streaming "
                         f"(replaces estimate)")
        except Exception as e:
            log.debug(f"Phase 3 OCC symbol setup: {e}")

        state.active_trades.append(trade)
        state.last_entry_time = now   # stamp cooldown timer
        self._persist_trades(ticker, state)
        badge = _trade_quality_badge(trade)
        _log_trade_event(trade, event="OPEN", badge=badge)
        _pg_register(trade)  # v5.1: register with portfolio Greeks
        log.info(f"ActiveTrade created: {ticker} {direction} {entry_type} @ ${price:.2f} | "
                 f"score={validation.setup_score}/5 [{validation.setup_label}] | "
                 f"policy={policy.name} | scale={validation.scale_advice} | "
                 f"stop=${stop:.2f} | targets={[f'${t:.2f}' for t in targets]} | "
                 f"gates=[{validation.gate_summary}]")
        return True

    # ── Issue 1: Conviction Play → ActiveTrade ──
    def create_conviction_trade(self, play: dict) -> bool:
        """Create an ActiveTrade from a conviction play, wiring it into
        the existing exit framework (premium stop, scale-out, trailing).

        This gives conviction plays the same exit logic as thesis-monitor
        generated trades: 20% premium stop, 1/3→2/3→full scale-in,
        and trailing exits.
        """
        ticker = play.get("ticker", "")
        if not ticker:
            return False

        direction = "LONG" if play.get("trade_direction") == "bullish" else "SHORT"
        spot = play.get("spot", 0)
        if spot <= 0:
            return False

        # Check for duplicate — don't create if already tracking this ticker+direction
        state = self._states.setdefault(ticker, MonitorState())
        for t in state.active_trades:
            if t.status in ("OPEN", "SCALED", "TRAILED") and t.direction == direction:
                log.info(f"Conviction trade skipped — already tracking {ticker} {direction}")
                return False

        route = play.get("route", "immediate")
        dte = play.get("dte", 0)
        strike = play.get("strike", round(spot))
        rec_strike = play.get("rec_strike", strike)

        # Compute stop: 20% of premium (handled by premium stop), but also set
        # a spot-based backstop. Use 1.5% for immediate, 3% for swing.
        if route == "immediate":
            stop_pct = 0.015
        else:
            stop_pct = 0.03
        if direction == "LONG":
            stop = spot * (1 - stop_pct)
        else:
            stop = spot * (1 + stop_pct)

        # Targets: use Potter Box levels if available, else ±1% / ±2%
        targets = []
        try:
            pb_data = _persistent_state._json_get(f"potter_box:active:{ticker}") if _persistent_state else None
            if pb_data:
                box = pb_data.get("box") or pb_data
                if direction == "LONG" and box.get("roof", 0) > spot:
                    targets.append(box["roof"])
                elif direction == "SHORT" and box.get("floor", 0) > 0 and box["floor"] < spot:
                    targets.append(box["floor"])
        except Exception:
            pass
        if not targets:
            mult = 1 if direction == "LONG" else -1
            targets = [round(spot * (1 + mult * 0.01), 2),
                       round(spot * (1 + mult * 0.02), 2)]

        now = time.monotonic()
        trade = ActiveTrade(
            ticker=ticker,
            direction=direction,
            entry_type="CONVICTION",
            entry_price=spot,
            stop_level=stop,
            targets=targets,
            status="OPEN",
            entry_time=now,
            entry_epoch=time.time(),
            entry_time_str=datetime.now().strftime("%H:%M:%S CT"),
            level_name=f"conviction_flow_{play.get('route', 'imm')}",
            regime=play.get("regime", ""),
            trade_type_label=play.get("trade_side", "LONG CALL"),
            setup_score=4 if play.get("shadow_agrees") else 3,
            setup_label="CONVICTION FLOW",
            exit_policy_name="CONVICTION_MOMENTUM",
            scale_advice="1/3 at entry, scale on confirmation",
            bias_direction=play.get("trade_direction", "bullish"),
            initial_contracts=7,
            scale_in_stage=1,
        )

        # Wire OCC symbol for live premium tracking
        opt_side = "call" if direction == "LONG" else "put"
        opt_strike = rec_strike if rec_strike > 0 else round(spot)
        try:
            from schwab_stream import build_occ_symbol, get_option_symbol_manager, get_live_premium
            exp_str = str(play.get("expiry", ""))[:10]
            if not exp_str or dte == 0:
                exp_date = date.today()
                # Use 1DTE vehicle for 0DTE
                if play.get("recommend_1dte"):
                    exp_date = date.today() + timedelta(days=1)
                    while exp_date.weekday() >= 5:
                        exp_date += timedelta(days=1)
                exp_str = exp_date.strftime("%Y-%m-%d")

            trade.option_expiry = exp_str
            trade.option_strike = float(opt_strike)
            trade.option_symbol = build_occ_symbol(ticker, exp_str, opt_side, opt_strike)
            log.info(f"Conviction trade OCC: {trade.option_symbol}")

            osm = get_option_symbol_manager()
            if osm:
                osm.subscribe_specific([trade.option_symbol])

            live_prem = get_live_premium(trade.option_symbol)
            if live_prem and live_prem > 0:
                trade.entry_premium = live_prem
                trade.est_premium = live_prem
                trade.peak_premium = live_prem
                trade.entry_delta = 0.50  # approximate ATM
                log.info(f"Conviction trade live premium: ${live_prem:.2f}")
            else:
                # Fallback: estimate from play mid/ask
                est = play.get("rec_mid") or play.get("mid") or play.get("ask", 0)
                if est and est > 0:
                    trade.entry_premium = est
                    trade.est_premium = est
                    trade.peak_premium = est
                    trade.entry_delta = 0.50
        except Exception as e:
            # v7.3 fix (Patch 3): was log.debug which swallowed early SPY/QQQ
            # conviction trades registering with empty OCC / $0.00 premium.
            # WARN with play context + traceback so we can diagnose next time.
            log.warning(
                f"Conviction OCC build failed for {ticker} "
                f"(direction={direction}, rec_strike={rec_strike}, "
                f"play_expiry={play.get('expiry','')}): {e}",
                exc_info=True
            )

        state.active_trades.append(trade)
        self._persist_trades(ticker, state)
        _log_trade_event(trade, event="OPEN")
        try:
            _pg_register(trade)
        except Exception:
            pass

        log.info(f"🎯 Conviction ActiveTrade created: {ticker} {direction} "
                 f"@ ${spot:.2f} | stop=${stop:.2f} | OCC={trade.option_symbol} | "
                 f"premium=${trade.entry_premium:.2f}")
        return True

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

            # v7: Swing trail monitor (alert-only, no auto-execution)
            if getattr(trade, 'is_swing_trade', False):
                self._swing_trail_monitor(trade, ticker, price, now, events)

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
                    # v5.1 Change 6: increment circuit breaker
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=True)
                    self._persist_trades(ticker, state)
                    events.append({"msg": f"\U0001f6d1 TRADE INVALIDATED — {trade.direction}\n\nHard stop ${stop_ref:.2f} breached.\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\nDon't hope. Close it.", "type": "exit", "priority": 5, "alert_key": f"exit_inv_{trade.trade_id}"})
                    log.info(f"Trade {trade.trade_id} INVALIDATED: hard stop ${stop_ref:.2f} breached at ${price:.2f}")
                    _log_trade_event(trade, event="CLOSE", close_price=price,
                                      close_reason=f"Hard stop ${stop_ref:.2f} breached",
                                      badge=_trade_quality_badge(trade))
                continue

            # ── v5.1 Change 8: CRISIS long option exit framework ──────────
            # Replaces standard premium stop + Layer 2 for CRISIS long option trades.
            # Scale out in thirds with premium tracking from delta × spot move.
            if getattr(trade, 'is_crisis_long_option', False):
                _crisis_exit = self._crisis_long_option_exit(
                    trade, ticker, thesis, state, price, now)
                if _crisis_exit:
                    events.extend(_crisis_exit)
                continue  # CRISIS trades skip standard Layer 1b + Layer 2

            # ── LAYER 1b: Delta-based 20% premium stop ───────────────────────
            # Phase 3: Use live streaming premium when available.
            # Fallback: uses entry_delta and min_favorable to estimate loss.
            if (trade.entry_premium > 0 and trade.entry_delta > 0
                    and trade.status in ("OPEN",)  # only before scaling
                    and trade.max_favorable <= 0):  # no MFE yet — still a pure loser

                # Phase 3: Try live premium first
                _live_loss_pct = None
                if trade.option_symbol:
                    try:
                        from schwab_stream import get_live_premium
                        _lp = get_live_premium(trade.option_symbol)
                        if _lp and _lp > 0 and trade.entry_premium > 0:
                            _live_loss_pct = (trade.entry_premium - _lp) / trade.entry_premium
                    except Exception:
                        pass

                adverse_move = abs(trade.min_favorable)
                if _live_loss_pct is not None:
                    est_loss_pct = _live_loss_pct
                    est_loss_dollars = round(trade.entry_premium - (trade.entry_premium * (1 - est_loss_pct)), 2)
                elif adverse_move > 0:
                    est_loss_pct = (adverse_move * trade.entry_delta) / trade.entry_premium
                    est_loss_dollars = round(adverse_move * trade.entry_delta, 2)
                else:
                    est_loss_pct = 0
                    est_loss_dollars = 0

                if est_loss_pct >= 0.20:
                        if self._exit_alert_ok(trade, "prem_stop", now):
                            pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
                            trade.status = "INVALIDATED"; trade.close_price = price
                            trade.close_time = now; trade.close_epoch = time.time()
                            _src = "live" if _live_loss_pct is not None else "est"
                            trade.close_reason = (f"Premium stop: {_src}. {est_loss_pct:.0%} loss "
                                                   f"(${adverse_move:.2f} adverse × δ{trade.entry_delta:.2f})")
                            # v5.1 Change 6: increment circuit breaker
                            self._update_circuit_breaker(state, trade.bias_direction, is_stop=True)
                            self._persist_trades(ticker, state)
                            _detail = (f"Live option premium ${_lp:.2f} vs entry ${trade.entry_premium:.2f}"
                                       if _live_loss_pct is not None else
                                       f"Adverse move: ${adverse_move:.2f} × delta {trade.entry_delta:.2f} "
                                       f"≈ ${est_loss_dollars:.2f} estimated loss.")
                            events.append({"msg": (
                                f"🛑 PREMIUM STOP — {trade.direction}\n\n"
                                f"Option loss ~{est_loss_pct:.0%} of entry premium.\n"
                                f"{_detail}\n"
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

            # ── Minimum hold gate — let trade develop before giveback eval ──
            # Hard stop (Layer 1) always active. Policy evaluation (scale,
            # trail, giveback) is suppressed for the first N bars to prevent
            # the giveback threshold from killing trades that need 5-10 min.
            bars_held = 0
            if bm and trade.entry_epoch > 0:
                for b in bm.state.bars_5m:
                    if b.timestamp > trade.entry_epoch:
                        bars_held += 1
            if bars_held < MONITOR_MIN_HOLD_BARS and trade.status == "OPEN":
                continue  # too early — let the trade breathe

            policy = ExitPolicy.from_config(trade.policy_config)
            signal = evaluate_exit(policy, trade, latest_bar, recent_bars,
                                   current_gex=thesis.gex_sign)

            if signal.action == "SCALE":
                if self._exit_alert_ok(trade, "scale", now):
                    trade.status = "SCALED"; trade.scaled_at_price = price
                    trade.trail_stop = trade.entry_price
                    # v5.1 Change 6: scale = trade is working, reset circuit breaker
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=False)
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
                    # v5.1 Change 6: non-stop exit resets circuit breaker
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=False)
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
                    # v5.1 Change 6: bar-close invalidation counts as stop
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=True)
                    self._persist_trades(ticker, state)
                    _pg_close(trade)  # v5.1: not covered by _log_trade_event here
                    events.append({"msg": f"\U0001f6d1 TRADE INVALIDATED — {trade.direction}\n\n{signal.reason}\nEntry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\nDon't hope. Close it.", "type": "exit", "priority": 5, "alert_key": f"exit_inv_{trade.trade_id}"})
            # HOLD = do nothing
        return events

    def _swing_trail_monitor(self, trade, ticker, price, now, events):
        """Monitor swing trade MFE and send trail alerts.
        Alert-only — does NOT auto-execute. Human decides."""
        if trade.status in ("CLOSED", "INVALIDATED"):
            return

        try:
            from trading_rules import (
                SWING_TRAIL_MIN_PROFIT_PCT, SWING_TRAIL_GIVEBACK_PCT,
                SWING_TRAIL_ALERT_COOLDOWN, SWING_TRAIL_NEW_PEAK_PCT,
                SWING_TRAIL_GIVEBACK_WARN,
            )
        except ImportError:
            SWING_TRAIL_MIN_PROFIT_PCT = 0.005
            SWING_TRAIL_GIVEBACK_PCT = 0.40
            SWING_TRAIL_ALERT_COOLDOWN = 300
            SWING_TRAIL_NEW_PEAK_PCT = 0.002
            SWING_TRAIL_GIVEBACK_WARN = 0.27

        entry = trade.entry_price
        if entry <= 0:
            return

        # Current move %
        if trade.direction == "LONG":
            move_pct = (price - entry) / entry
        else:
            move_pct = (entry - price) / entry

        # Cooldown check
        def _trail_alert_ok(alert_type):
            if trade.swing_last_alert == alert_type:
                if now - trade.swing_last_alert_time < SWING_TRAIL_ALERT_COOLDOWN:
                    return False
            return True

        def _mark_trail_alert(alert_type):
            trade.swing_last_alert = alert_type
            trade.swing_last_alert_time = now

        # ── Check if trail should activate ──
        if not trade.swing_trail_active:
            if move_pct >= SWING_TRAIL_MIN_PROFIT_PCT:
                trade.swing_trail_active = True
                trade.swing_mfe_peak_pct = move_pct
                giveback = move_pct * SWING_TRAIL_GIVEBACK_PCT
                if trade.direction == "LONG":
                    trade.swing_trail_level = entry * (1 + move_pct - giveback)
                else:
                    trade.swing_trail_level = entry * (1 - move_pct + giveback)

                if _trail_alert_ok("activated"):
                    _mark_trail_alert("activated")
                    events.append({
                        "msg": (
                            f"\U0001f7e2 SWING TRAIL ACTIVATED — {ticker} {trade.direction}\n\n"
                            f"Entry: ${entry:.2f} → Now: ${price:.2f} ({move_pct*100:+.2f}%)\n"
                            f"Trail stop: ${trade.swing_trail_level:.2f}\n"
                            f"MFE tracking started. Will alert at giveback threshold.\n"
                            f"\n\U0001f4a1 No action needed yet — let it run."
                        ),
                        "type": "swing_trail", "priority": 3,
                        "alert_key": f"swing_trail_act_{trade.trade_id}",
                    })
                    self._persist_trades(ticker, None)
            return

        # ── Trail is active — check for new peak ──
        if move_pct > trade.swing_mfe_peak_pct:
            old_peak = trade.swing_mfe_peak_pct
            trade.swing_mfe_peak_pct = move_pct
            giveback = move_pct * SWING_TRAIL_GIVEBACK_PCT
            if trade.direction == "LONG":
                trade.swing_trail_level = entry * (1 + move_pct - giveback)
            else:
                trade.swing_trail_level = entry * (1 - move_pct + giveback)

            if move_pct - old_peak >= SWING_TRAIL_NEW_PEAK_PCT:
                if _trail_alert_ok("new_peak"):
                    _mark_trail_alert("new_peak")
                    events.append({
                        "msg": (
                            f"\U0001f4c8 SWING NEW PEAK — {ticker} {trade.direction}\n\n"
                            f"Peak: ${price:.2f} ({move_pct*100:+.2f}% from ${entry:.2f})\n"
                            f"Trail stop: ${trade.swing_trail_level:.2f} "
                            f"({SWING_TRAIL_GIVEBACK_PCT*100:.0f}% giveback)\n"
                            f"Keep holding — trail protects gains."
                        ),
                        "type": "swing_trail", "priority": 2,
                        "alert_key": f"swing_trail_peak_{trade.trade_id}",
                    })
                    self._persist_trades(ticker, None)
            return

        # ── Check giveback from peak ──
        if trade.swing_mfe_peak_pct > 0:
            giveback_current = (trade.swing_mfe_peak_pct - move_pct) / trade.swing_mfe_peak_pct

            peak_price = (entry * (1 + trade.swing_mfe_peak_pct)
                          if trade.direction == "LONG"
                          else entry * (1 - trade.swing_mfe_peak_pct))

            # Warning at partial giveback
            if (giveback_current >= SWING_TRAIL_GIVEBACK_WARN
                    and giveback_current < SWING_TRAIL_GIVEBACK_PCT):
                if _trail_alert_ok("giveback_warn"):
                    _mark_trail_alert("giveback_warn")
                    events.append({
                        "msg": (
                            f"\u26a0\ufe0f SWING GIVEBACK — {ticker} {trade.direction}\n\n"
                            f"Peak: ${peak_price:.2f} ({trade.swing_mfe_peak_pct*100:+.1f}%)\n"
                            f"Now: ${price:.2f} ({move_pct*100:+.1f}%)\n"
                            f"Giveback: {giveback_current*100:.0f}% of move "
                            f"(trigger at {SWING_TRAIL_GIVEBACK_PCT*100:.0f}%)\n"
                            f"Trail stop: ${trade.swing_trail_level:.2f}\n"
                            f"\n\U0001f914 Watch closely. Not at trigger yet."
                        ),
                        "type": "swing_trail", "priority": 3,
                        "alert_key": f"swing_trail_warn_{trade.trade_id}",
                    })

            # Close signal at full giveback
            if giveback_current >= SWING_TRAIL_GIVEBACK_PCT:
                if _trail_alert_ok("close_signal"):
                    _mark_trail_alert("close_signal")
                    events.append({
                        "msg": (
                            f"\U0001f534 SWING EXIT SIGNAL — {ticker} {trade.direction}\n\n"
                            f"Hit {SWING_TRAIL_GIVEBACK_PCT*100:.0f}% giveback threshold.\n"
                            f"Peak: ${peak_price:.2f} ({trade.swing_mfe_peak_pct*100:+.1f}%)\n"
                            f"Now: ${price:.2f} ({move_pct*100:+.1f}%)\n"
                            f"Trail level: ${trade.swing_trail_level:.2f}\n"
                            f"Keeping: {move_pct*100:.1f}% of entry\n"
                            f"\n\U0001f3af Manual close recommended.\n"
                            f"Check spread bid before executing."
                        ),
                        "type": "swing_trail", "priority": 5,
                        "alert_key": f"swing_trail_exit_{trade.trade_id}",
                    })

    def _exit_alert_ok(self, trade, key, now):
        """Check and set cooldown for exit alerts."""
        last = trade.exit_alert_history.get(key)
        if last is not None and (now - last) < EXIT_ALERT_COOLDOWN_SEC:
            return False
        trade.exit_alert_history[key] = now
        return True

    # ── v5.1 Change 6: Circuit Breaker ──
    def _update_circuit_breaker(self, state, bias_direction, is_stop=True):
        """Update circuit breaker state after trade close.
        is_stop=True: hard stop or premium stop → increment counter.
        is_stop=False: non-stop exit (scale, giveback, trail) → reset counter."""
        if not CIRCUIT_BREAKER_ENABLED or not bias_direction:
            return
        if is_stop:
            count = state.consec_stops.get(bias_direction, 0) + 1
            state.consec_stops[bias_direction] = count
            if count >= CIRCUIT_BREAKER_MAX_CONSEC_STOPS:
                pause_until = time.time() + (CIRCUIT_BREAKER_PAUSE_MIN * 60)
                state.cb_paused_until[bias_direction] = pause_until
                log.info(f"Circuit breaker TRIGGERED: {bias_direction} paused for "
                         f"{CIRCUIT_BREAKER_PAUSE_MIN}min ({count} consecutive stops)")
            else:
                log.info(f"Circuit breaker: {bias_direction} consecutive stops = {count}")
        else:
            if state.consec_stops.get(bias_direction, 0) > 0:
                log.info(f"Circuit breaker RESET: {bias_direction} "
                         f"(was {state.consec_stops.get(bias_direction, 0)} consecutive)")
            state.consec_stops[bias_direction] = 0
            # Don't clear cb_paused_until — let the timer expire naturally

    # ── v5.1 Change 8: CRISIS Long Option Exit Framework ──
    def _crisis_long_option_exit(self, trade, ticker, thesis, state, price, now):
        """CRISIS long option exit: scale in thirds with premium tracking.
        Returns list of events, or empty list if HOLD."""
        events = []
        now_epoch = time.time()

        # Estimate current premium from delta × spot move
        if trade.direction == "LONG":
            spot_move = price - trade.entry_price
        else:  # SHORT direction = put buyer
            spot_move = trade.entry_price - price

        # Phase 3: Use live streaming premium if available, else delta approximation
        _used_live = False
        if trade.option_symbol:
            try:
                from schwab_stream import get_live_premium, get_live_greeks
                live_prem = get_live_premium(trade.option_symbol)
                if live_prem and live_prem > 0:
                    trade.est_premium = live_prem
                    _used_live = True
                    # Also update delta from live Greeks for better accuracy
                    live_g = get_live_greeks(trade.option_symbol)
                    if live_g and live_g.get("delta"):
                        trade.entry_delta = abs(live_g["delta"])
            except Exception:
                pass

        if not _used_live:
            # Fallback: Approximate premium: entry_premium + (spot_move × delta)
            trade.est_premium = max(0.01, trade.entry_premium + spot_move * trade.entry_delta)

        trade.peak_premium = max(trade.peak_premium, trade.est_premium)

        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
        gain_pct = (trade.est_premium - trade.entry_premium) / trade.entry_premium if trade.entry_premium > 0 else 0
        giveback_pct = (trade.peak_premium - trade.est_premium) / trade.peak_premium if trade.peak_premium > 0 else 0

        # ── Phase 1: HOLD window — no exit except premium stop ──
        if now_epoch < trade.crisis_hold_until:
            # Only 45% premium stop active during hold window
            if trade.est_premium <= trade.entry_premium * (1 - CRISIS_PREMIUM_STOP_PCT):
                if self._exit_alert_ok(trade, "crisis_prem_stop", now):
                    trade.status = "INVALIDATED"; trade.close_price = price
                    trade.close_time = now; trade.close_epoch = now_epoch
                    trade.close_reason = f"CRISIS premium stop: est. {gain_pct:.0%} loss during hold window"
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=True)
                    self._persist_trades(ticker, state)
                    events.append({"msg": (
                        f"🛑 CRISIS PREMIUM STOP — {trade.direction}\n\n"
                        f"Est. premium ${trade.est_premium:.2f} (was ${trade.entry_premium:.2f})\n"
                        f"Loss ~{abs(gain_pct):.0%} during {CRISIS_HOLD_WINDOW_MIN}min hold window.\n"
                        f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                        f"Thesis failed. Cut it."
                    ), "type": "exit", "priority": 5, "alert_key": f"exit_crisis_prem_{trade.trade_id}"})
                    _log_trade_event(trade, event="CLOSE", close_price=price,
                                      close_reason=trade.close_reason,
                                      badge=_trade_quality_badge(trade))
            return events  # hold window still active

        # ── Phase 2: Scale out in thirds ──
        # Scale 1: 50% premium gain → sell 1/3
        if not trade.crisis_scale1_done and gain_pct >= CRISIS_SCALE_1_PCT:
            if self._exit_alert_ok(trade, "crisis_scale1", now):
                trade.crisis_scale1_done = True
                trade.crisis_phase = "SCALE_1"
                contracts_sold = trade.contracts_remaining // 3
                trade.contracts_remaining -= contracts_sold
                self._update_circuit_breaker(state, trade.bias_direction, is_stop=False)
                self._persist_trades(ticker, state)
                events.append({"msg": (
                    f"💰 CRISIS SCALE 1/3 — {trade.direction}\n\n"
                    f"Est. premium ${trade.est_premium:.2f} (+{gain_pct:.0%} from ${trade.entry_premium:.2f})\n"
                    f"SELL {contracts_sold} contracts. {trade.contracts_remaining} remaining.\n"
                    f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                    f"Lock in profit. Let the rest run."
                ), "type": "exit", "priority": 5, "alert_key": f"exit_crisis_s1_{trade.trade_id}"})

        # Scale 2: 100% premium gain → sell another 1/3
        if trade.crisis_scale1_done and not trade.crisis_scale2_done and gain_pct >= CRISIS_SCALE_2_PCT:
            if self._exit_alert_ok(trade, "crisis_scale2", now):
                trade.crisis_scale2_done = True
                trade.crisis_phase = "SCALE_2"
                contracts_sold = trade.contracts_remaining // 2
                trade.contracts_remaining -= contracts_sold
                self._persist_trades(ticker, state)
                events.append({"msg": (
                    f"💰 CRISIS SCALE 2/3 — {trade.direction}\n\n"
                    f"Est. premium ${trade.est_premium:.2f} (+{gain_pct:.0%} = DOUBLED)\n"
                    f"SELL {contracts_sold} more. {trade.contracts_remaining} remaining.\n"
                    f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                    f"Trail the rest with {CRISIS_TRAIL_GIVEBACK_PCT:.0%} giveback."
                ), "type": "exit", "priority": 5, "alert_key": f"exit_crisis_s2_{trade.trade_id}"})

        # ── Phase 3: Trail last contracts ──
        if trade.crisis_scale2_done:
            trade.crisis_phase = "TRAIL"
            # Determine giveback threshold
            _mins_ct = 0
            try:
                from zoneinfo import ZoneInfo
                _now_ct = datetime.now(ZoneInfo("America/Chicago"))
                _mins_ct = _now_ct.hour * 60 + _now_ct.minute
            except Exception:
                _mins_ct = 0
            # Phase 4: Last 60 min — tighten trail
            _close_mins = 870  # 14:30 CT = market close
            _minutes_to_close = _close_mins - _mins_ct
            if _minutes_to_close <= CRISIS_FINAL_HOUR_MINUTES:
                _gb_threshold = CRISIS_FINAL_HOUR_GIVEBACK_PCT
                trade.crisis_phase = "TRAIL_TIGHT"
            else:
                _gb_threshold = CRISIS_TRAIL_GIVEBACK_PCT

            if giveback_pct >= _gb_threshold:
                if self._exit_alert_ok(trade, "crisis_trail_exit", now):
                    trade.status = "CLOSED"; trade.close_price = price
                    trade.close_time = now; trade.close_epoch = now_epoch
                    trade.close_reason = (f"CRISIS trail: {giveback_pct:.0%} giveback from "
                                          f"peak ${trade.peak_premium:.2f} (threshold {_gb_threshold:.0%})")
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=False)
                    self._persist_trades(ticker, state)
                    events.append({"msg": (
                        f"🏃 CRISIS TRAIL EXIT — {trade.direction}\n\n"
                        f"Peak premium ${trade.peak_premium:.2f} → Now ${trade.est_premium:.2f}\n"
                        f"Giveback {giveback_pct:.0%} exceeded {_gb_threshold:.0%} threshold.\n"
                        f"Remaining {trade.contracts_remaining} contracts closed.\n"
                        f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                        f"Pay yourself."
                    ), "type": "exit", "priority": 5, "alert_key": f"exit_crisis_trail_{trade.trade_id}"})
                    _log_trade_event(trade, event="CLOSE", close_price=price,
                                      close_reason=trade.close_reason,
                                      badge=_trade_quality_badge(trade))

        # ── 45% premium stop (all phases after hold window) ──
        if trade.status not in ("CLOSED", "INVALIDATED"):
            if trade.est_premium <= trade.entry_premium * (1 - CRISIS_PREMIUM_STOP_PCT):
                if self._exit_alert_ok(trade, "crisis_prem_stop", now):
                    trade.status = "INVALIDATED"; trade.close_price = price
                    trade.close_time = now; trade.close_epoch = now_epoch
                    trade.close_reason = f"CRISIS premium stop: est. {abs(gain_pct):.0%} loss"
                    self._update_circuit_breaker(state, trade.bias_direction, is_stop=True)
                    self._persist_trades(ticker, state)
                    events.append({"msg": (
                        f"🛑 CRISIS PREMIUM STOP — {trade.direction}\n\n"
                        f"Est. premium ${trade.est_premium:.2f} (was ${trade.entry_premium:.2f})\n"
                        f"Loss ~{abs(gain_pct):.0%} of entry premium.\n"
                        f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)"
                    ), "type": "exit", "priority": 5, "alert_key": f"exit_crisis_prem_{trade.trade_id}"})
                    _log_trade_event(trade, event="CLOSE", close_price=price,
                                      close_reason=trade.close_reason,
                                      badge=_trade_quality_badge(trade))

        return events

    # ── v5.1 Change 9: Multi-Touch Level Break Detection ──
    def _detect_multi_touch_break(self, ticker, thesis, state, price, now, registry=None):
        """Scan intraday levels for 3+ same-side touches and confirmed break.
        Returns entry events if a structural break is detected."""
        events = []
        if not state.intraday_levels:
            return events

        # Check for active Change 9 trades — only 1 at a time per ticker
        _active_mt = [t for t in state.active_trades
                      if t.status in ("OPEN", "SCALED", "TRAILED")
                      and getattr(t, 'is_multi_touch_entry', False)]
        if len(_active_mt) >= MULTI_TOUCH_MAX_ACTIVE:
            return events

        # Also block counter-direction entries if a Change 9 trade is active
        if _active_mt:
            return events

        now_epoch = time.time()

        for il in state.intraday_levels:
            if not il.active:
                continue
            if il.touches < MULTI_TOUCH_MIN_TOUCHES:
                continue
            # v5.1 Fix B: Cap touches — levels tested >10x are ranges, not fresh breaks
            if il.touches > MULTI_TOUCH_MAX_TOUCHES:
                continue
            # v5.1 fix: Skip levels already consumed by a previous multi-touch trade
            _level_price_str = f"{il.price:.2f}"
            if _level_price_str in state.multi_touch_consumed_levels:
                continue
            # Level must have existed for minimum lookback
            age_min = (now_epoch - il.first_seen_ts) / 60 if il.first_seen_ts > 0 else 0
            if age_min < MULTI_TOUCH_LOOKBACK_MIN:
                continue
            # v5.1 Fix B: Max age — levels older than 3 hours are stale
            if age_min > MULTI_TOUCH_MAX_AGE_MIN:
                continue
            # v5.1 Fix B: Recent touch — level must have been tested within last 60 min
            last_touch_age = (now_epoch - il.last_touched_ts) / 60 if il.last_touched_ts > 0 else 999
            if last_touch_age > MULTI_TOUCH_RECENT_TOUCH_MIN:
                continue

            level_key = f"{il.price:.2f}_{il.kind}"
            broken = False
            direction = ""

            # Support break → bearish (long put)
            if il.kind == "support" and price < il.price:
                # Price is below support level
                broken = True
                direction = "SHORT"  # bearish bias = put buyer

            # Resistance break → bullish (long call)
            elif il.kind == "resistance" and price > il.price:
                broken = True
                direction = "LONG"  # bullish bias = call buyer

            if not broken:
                # Reset confirmation count if price came back inside
                state.multi_touch_confirm_polls.pop(level_key, None)
                continue

            # ── Spot-poll confirmation (Change 3 style) ──
            confirm_count = state.multi_touch_confirm_polls.get(level_key, 0) + 1
            state.multi_touch_confirm_polls[level_key] = confirm_count

            if confirm_count < MULTI_TOUCH_CONFIRM_POLLS:
                log.debug(f"Multi-touch break polling: {ticker} {il.kind} "
                          f"${il.price:.2f} ({il.touches} touches) — "
                          f"confirm poll {confirm_count}/{MULTI_TOUCH_CONFIRM_POLLS}")
                continue

            # ── CONFIRMED — build the entry ──
            # Find the consolidation zone for stop placement
            zone_high = il.price
            zone_low = il.price
            for other in state.intraday_levels:
                if not other.active:
                    continue
                if abs(other.price - il.price) < il.price * 0.005:  # within 0.5%
                    zone_high = max(zone_high, other.price)
                    zone_low = min(zone_low, other.price)

            # Also check sharp move origins and resistance/support from thesis
            # v5.1 spec: stop = max/min(consolidation_zone, nearest_level, broken_level ± min_stop)
            # Fix: Only consider levels NEAR the consolidation zone (within 1% of broken level)
            # to prevent distant daily support/resistance from creating $7+ stops on 0DTE.
            _min_stop_val = get_vix_scaled_min_stop(ticker, thesis.vix) if (VIX_STOP_SCALE_ENABLED and thesis.vix > 0) else 0.40
            _max_zone_dist = il.price * 0.01  # 1% ≈ $6.50 on SPY — reasonable max zone

            if direction == "SHORT":
                # Put: stop above consolidation zone
                # Gather candidate stops: consolidation high, nearest resistance, broken level + min_stop
                _candidates = [zone_high + MULTI_TOUCH_STOP_ZONE_BUFFER]
                if thesis.levels.local_resistance and thesis.levels.local_resistance > il.price:
                    if (thesis.levels.local_resistance - il.price) <= _max_zone_dist:
                        _candidates.append(thesis.levels.local_resistance)
                _candidates.append(il.price + _min_stop_val)  # broken_level + min_stop
                stop = max(_candidates)
            else:
                # Call: stop below consolidation zone
                _candidates = [zone_low - MULTI_TOUCH_STOP_ZONE_BUFFER]
                if thesis.levels.local_support and thesis.levels.local_support < il.price:
                    if (il.price - thesis.levels.local_support) <= _max_zone_dist:
                        _candidates.append(thesis.levels.local_support)
                _candidates.append(il.price - _min_stop_val)  # broken_level - min_stop
                stop = min(_candidates)

            stop_dist = abs(price - stop)

            # VIX gate check — with the spec formula, stop should naturally pass,
            # but verify explicitly as a safety net
            if VIX_STOP_SCALE_ENABLED and thesis.vix > 0:
                min_stop = _min_stop_val
                if stop_dist < min_stop:
                    log.info(f"Multi-touch break VIX gate: {ticker} stop dist "
                             f"${stop_dist:.2f} < min ${min_stop:.2f}, skipping")
                    continue

            # Determine instrument label — Change 9 spec-aligned
            _vol = (thesis.volatility_regime or "NORMAL").upper()
            _is_crisis = _vol == "CRISIS"
            _is_elevated = _vol == "ELEVATED"
            _is_low_vol = _vol not in ("ELEVATED", "CRISIS")

            if direction == "SHORT":
                # Support break → long put in CRISIS/ELEVATED, spread in lower vol
                _instr = "LONG PUT" if (_is_crisis or _is_elevated) else "Put debit spread"
            else:
                # Resistance break → long call in low vol, or in CRISIS squeeze context
                _call_snapback_ok = _is_crisis and thesis.gex_sign == "negative"
                _instr = "LONG CALL" if (_is_low_vol or _call_snapback_ok) else "Call debit spread"

            # Build the entry event
            _type_label = "support break" if il.kind == "support" else "resistance break"
            _tp_now = _get_time_phase_ct()

            # v5.1: DTE guidance based on vol regime and time of day
            _dte_line = _dte_guidance(_tp_now["phase"], thesis.volatility_regime, thesis.vix)

            # v5.1: Strike guidance for long options
            _atm = int(price)
            _strike_guidance = ""
            if "LONG" in _instr.upper():
                if _is_crisis or _is_elevated:
                    _delta_tgt = 0.30
                    _delta_str = "0.25-0.35"
                    # Slightly OTM: 1-2 strikes from ATM
                    if direction == "SHORT":  # put buyer
                        _rec_strike = _atm - 1
                        _option_desc = f"${_rec_strike} put"
                    else:  # call buyer
                        _rec_strike = _atm + 2
                        _option_desc = f"${_rec_strike} call"
                    # Estimate premium from VIX
                    _prem_est = round(thesis.vix * 0.06 * 0.65, 2)
                    _contracts = 20
                    _max_risk = int(_prem_est * _contracts * 100)
                    _strike_guidance = (
                        f"\n\n— STRIKE GUIDANCE —\n"
                        f"🎯 STRIKE: {_option_desc} (delta ~{_delta_tgt}, slightly OTM)\n"
                        f"   Target delta range: {_delta_str}\n"
                        f"💵 Est. premium: ~${_prem_est:.2f} per contract\n"
                        f"📐 SIZE: {_contracts} contracts = ${_max_risk:,} max risk\n"
                        f"   Scale in: enter 1/3 now ({_contracts // 3} contracts), "
                        f"add on confirmation\n"
                        f"{_dte_line}"
                    )
                else:
                    # Low vol — ATM strikes, cheaper premium
                    if direction == "SHORT":
                        _rec_strike = _atm
                        _option_desc = f"${_rec_strike} put"
                    else:
                        _rec_strike = _atm
                        _option_desc = f"${_rec_strike} call"
                    _strike_guidance = (
                        f"\n\n— TRADE CARD —\n"
                        f"🎯 STRIKE: {_option_desc} (ATM, delta ~0.50)\n"
                        f"📐 SIZE: min(floor($2,000 ÷ premium × 100), 20 contracts)\n"
                        f"{_dte_line}"
                    )

            msg = (
                f"⭐⭐⭐⭐⭐ 🚀 {ticker} STRUCTURAL BREAK — {_instr.upper()}\n\n"
                f"📐 MULTI-TOUCH {_type_label.upper()}: "
                f"${il.price:.2f} tested {il.touches}x, CONFIRMED BROKEN\n\n"
                f"{'🟥🔥 BREAKDOWN' if direction == 'SHORT' else '🟩🔥 BREAKOUT'} "
                f"— {_instr}\n\n"
                f"ENTRY: ~${price:.2f}\n"
                f"STOP: ${stop:.2f} (above full consolidation zone)\n"
                f"Stop distance: ${stop_dist:.2f}\n"
                f"Zone: ${zone_low:.2f}-${zone_high:.2f} ({il.touches} touches)"
                f"{_strike_guidance}"
            )

            _ak = f"mt_break_{'dn' if direction == 'SHORT' else 'up'}_{il.price:.2f}"
            events.append({
                "msg": msg,
                "type": "critical",
                "priority": 5,
                "alert_key": _ak,
                # Pass multi-touch metadata for _create_trade_from_event
                "_is_multi_touch": True,
                "_touch_count": il.touches,
                "_zone_high": zone_high,
                "_zone_low": zone_low,
                "_mt_stop": stop,
            })

            log.info(f"Multi-touch break CONFIRMED: {ticker} {_type_label} "
                     f"${il.price:.2f} ({il.touches} touches) → {direction} "
                     f"stop=${stop:.2f} dist=${stop_dist:.2f}")

            # Clear confirmation counter — don't re-fire
            state.multi_touch_confirm_polls.pop(level_key, None)
            # Mark level as consumed — persistent across poll cycles
            il.active = False
            state.multi_touch_consumed_levels.add(_level_price_str)

            # Only fire one multi-touch event per evaluate cycle
            break

        return events

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
                    "option_symbol": t.option_symbol,
                    "option_strike": t.option_strike,
                    "option_expiry": t.option_expiry,
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
                trade.option_symbol = td.get("option_symbol", "")
                trade.option_strike = td.get("option_strike", 0.0)
                trade.option_expiry = td.get("option_expiry", "")
                # Phase 3: re-subscribe to streaming for restored trades
                if trade.option_symbol and trade.status in ("OPEN", "SCALED", "TRAILED"):
                    try:
                        from schwab_stream import get_option_symbol_manager
                        osm = get_option_symbol_manager()
                        if osm:
                            osm.subscribe_specific([trade.option_symbol])
                    except Exception:
                        pass
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
            # v5.0: Dedicated momentum fade cooldown (longer than general alert cooldown).
            # Prevents the "upside fading / downside fading" ping-pong every 60 seconds.
            _mom_fade_last_up = state.alert_history.get("mom_fade_up", 0)
            _mom_fade_last_dn = state.alert_history.get("mom_fade_dn", 0)
            _now_mono = time.monotonic()

            if state.momentum == "LOSING_UPSIDE_MOMENTUM" and old in ("ACCELERATING_UP", "DRIFTING_UP"):
                if (_now_mono - _mom_fade_last_up) >= ALERT_MOMENTUM_FADE_COOLDOWN_SEC:
                    events.append({"msg": "⚠️ Upside momentum fading. Tighten if long.", "type": "warning", "priority": 4, "alert_key": "mom_fade_up"})
            elif state.momentum == "LOSING_DOWNSIDE_MOMENTUM" and old in ("ACCELERATING_DOWN", "DRIFTING_DOWN"):
                if (_now_mono - _mom_fade_last_dn) >= ALERT_MOMENTUM_FADE_COOLDOWN_SEC:
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
        _ticker = getattr(thesis, 'ticker', '')
        _is_high_res = _ticker.upper() in self._HIGH_RES_TICKERS
        for ba in state.break_attempts:
            if ba.detected_as_failed or ba.detected_as_confirmed: continue
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC: continue

            # ── v5.1 Change 3: Spot-poll confirmation for high-res tickers ──
            # Instead of waiting for 2-3 bars (10-20 min with delayed data),
            # count consecutive 60-second spot polls beyond the level.
            # Confirmation in 2-3 minutes instead of 15-20 minutes.
            if _is_high_res:
                _confirm_key = f"brk_{ba.direction}_{ba.level:.2f}"
                buf = ba.level * (MONITOR_CONFIRM_BUFFER_PCT / 100)
                if ba.direction == "DOWN":
                    _beyond = price < (ba.level - buf)
                else:
                    _beyond = price > (ba.level + buf)

                if _beyond:
                    _polls = state.break_confirm_polls.get(_confirm_key, 0) + 1
                    state.break_confirm_polls[_confirm_key] = _polls
                else:
                    # Price came back — reset
                    state.break_confirm_polls[_confirm_key] = 0
                    continue

                _req_polls = BREAK_CONFIRM_POLLS  # v5.1: dedicated constant
                if _polls < _req_polls:
                    continue  # not enough consecutive polls yet

                # Confirmed via spot polls — 3 consecutive polls beyond level
                # is sufficient confirmation. No additional net move filter needed.
                # Clear the poll counter
                state.break_confirm_polls.pop(_confirm_key, None)
            else:
                # Standard bar-count confirmation for non-high-res tickers
                req = 2 if tp["phase"] in ("OPEN", "MORNING", "AFTERNOON") else 3
                bars_since_break = (bm.state.bars_since_open - ba.break_bar_index) if (bm and ba.break_bar_index >= 0) else 999
                if bars_since_break < req: continue
                buf = ba.level * (MONITOR_CONFIRM_BUFFER_PCT / 100); net = self._recent_net_move(state, 3, bm=bm)
                if ba.direction == "DOWN":
                    ok = price < (ba.level - buf) and net < -(ba.level * 0.0012)
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
                        ticker=getattr(thesis, "ticker", "UNKNOWN"),
                        filter_reason="HAIRLINE_GATE",
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

            dte_line = _dte_guidance(tp_now["phase"], thesis.volatility_regime, thesis.vix)
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
                # ── Also enforce hard minimum stop distance ───────────────
                # v5.0: VIX-scaled stop distance
                _fm_ticker = getattr(thesis, "ticker", "").upper()
                if VIX_STOP_SCALE_ENABLED and thesis.vix > 0:
                    min_stop = get_vix_scaled_min_stop(_fm_ticker, thesis.vix)
                else:
                    min_stop = MONITOR_MIN_STOP_DISTANCE.get(
                        _fm_ticker,
                        MONITOR_MIN_STOP_DISTANCE["DEFAULT"])
                if abs(price - ba.level) < min_stop:
                    log.info(f"Min stop gate (failed move): ${ba.level:.2f} is "
                             f"${abs(price - ba.level):.2f} from price (min ${min_stop:.2f}), skipping")
                    ba.detected_as_failed = False
                    continue
                # Derive direction/entry_type early — needed for hairline logging
                _fm_direction = "LONG" if ba.direction == "DOWN" else "SHORT"
                _fm_entry_type = "FAILED"
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
                            ticker=getattr(thesis, "ticker", "UNKNOWN"),
                            filter_reason="HAIRLINE_GATE",
                            direction=_fm_direction, entry_type=_fm_entry_type,
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

                dte_line = _dte_guidance(tp["phase"], thesis.volatility_regime, thesis.vix)
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
        dte_line = _dte_guidance(tp["phase"], thesis.volatility_regime, thesis.vix)
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

    # ── v5.0: EM Guide Matching ──
    def _check_em_guide_match(self, thesis, state, price, event) -> Optional[str]:
        """
        Check if a trade alert matches one of the EM guide's "SETUPS TO WATCH."

        The EM guide posts setups like:
          "IF price breaks above $654.22 AND fails → GO SHORT. Stop above $654.22."
          "IF price breaks below $647.92 AND fails → GO LONG. Stop below $647.92."

        This method checks if the current alert matches one of those patterns
        by comparing the alert direction + level against the thesis levels.

        Returns a description string if matched, None otherwise.
        """
        if not EM_GUIDE_MATCHING_ENABLED:
            return None

        ak = event.get("alert_key", "")
        lvl = thesis.levels

        # Determine alert direction
        is_short = any(x in ak for x in ("fbo_", "conf_short", "rt_short", "rt_fs"))
        is_long = any(x in ak for x in ("fb_", "conf_long", "rt_long", "rt_fl"))

        if not is_short and not is_long:
            return None

        # Extract level price from alert key
        try:
            level_price = float(ak.split("_")[-1])
        except (ValueError, IndexError):
            return None

        tol_pct = 0.15  # 0.15% tolerance for level matching
        matches = []

        # Check against thesis levels (micro triggers from EM guide)
        if is_short and lvl.local_resistance is not None:
            if abs(level_price - lvl.local_resistance) / lvl.local_resistance * 100 < tol_pct:
                matches.append(f"failed breakout at R ${lvl.local_resistance:.2f}")
        if is_short and lvl.micro_trigger_up is not None:
            if abs(level_price - lvl.micro_trigger_up) / lvl.micro_trigger_up * 100 < tol_pct:
                matches.append(f"fade above micro trigger ${lvl.micro_trigger_up:.2f}")

        if is_long and lvl.local_support is not None:
            if abs(level_price - lvl.local_support) / lvl.local_support * 100 < tol_pct:
                matches.append(f"failed breakdown at S ${lvl.local_support:.2f}")
        if is_long and lvl.micro_trigger_down is not None:
            if abs(level_price - lvl.micro_trigger_down) / lvl.micro_trigger_down * 100 < tol_pct:
                matches.append(f"squeeze below micro trigger ${lvl.micro_trigger_down:.2f}")

        # Check against gamma flip
        if lvl.gamma_flip is not None:
            if abs(level_price - lvl.gamma_flip) / lvl.gamma_flip * 100 < tol_pct:
                if is_long:
                    matches.append(f"reclaim gamma flip ${lvl.gamma_flip:.2f}")
                elif is_short:
                    matches.append(f"rejection at gamma flip ${lvl.gamma_flip:.2f}")

        # Check GEX alignment
        if thesis.gex_sign == "positive":
            if "fbo_" in ak or "fb_" in ak:
                matches.append("GEX+ confirms failed-move setup")

        if matches:
            return " + ".join(matches)
        return None

    def _check_gamma_flip(self, thesis, state, price):
        events = []; flip = thesis.levels.gamma_flip
        if flip is None: return events
        above = price > flip
        if state.above_gamma_flip is not None and above != state.above_gamma_flip:
            # ── Track crossing timestamp for oscillation gate ──
            now_epoch = time.time()
            state.gamma_flip_crossings.append(now_epoch)
            # Prune crossings older than the oscillation window
            cutoff = now_epoch - MONITOR_GF_OSCILLATION_WINDOW_SEC
            state.gamma_flip_crossings = [t for t in state.gamma_flip_crossings if t >= cutoff]
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
        # ── v5.1 Change 4: Noise reduction ────────────────────────────────
        # Demote non-essential alerts to prevent Telegram spam.
        # Target: ~75% reduction in Telegram volume.
        _has_active_trade = any(
            t.status in ("OPEN", "SCALED", "TRAILED")
            for t in state.active_trades
        )
        for e in events:
            k = e.get("alert_key", "")
            msg = e.get("msg", "")
            etype = e.get("type", "")

            # Break attempts → log only (never Telegram)
            if k.startswith("brk_dn_") or k.startswith("brk_up_"):
                if etype not in ("trade_confirmed", "critical"):
                    e["priority"] = min(e.get("priority", 0), 2)
                    e["type"] = "info"

            # Momentum warnings → Telegram only when active trade exists
            elif "momentum" in k.lower() or "momentum" in msg.lower():
                if not _has_active_trade:
                    e["priority"] = min(e.get("priority", 0), 2)
                    e["type"] = "info"

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
    "option_symbol",                 # Phase 3: OCC symbol for streaming
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
            "option_symbol":    getattr(trade, "option_symbol",     ""),
        }

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = _csv_mod.DictWriter(f, fieldnames=_TRADE_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        # v5.1: notify portfolio Greeks on trade close
        if event == "CLOSE":
            _pg_close(trade)

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
        # v7.3 fix (Patch 2): _HIGH_RES_TICKERS is defined on ThesisMonitorEngine
        # (class at line ~788), not on ThesisMonitorDaemon. Access via self.engine,
        # which is stored in __init__. Previously AttributeError on every thread
        # start, silently killing the monitor daemon after logging "started".
        log.info(f"Thesis monitor: {MONITOR_POLL_INTERVAL_FAST_SEC}s for {len(MONITOR_FAST_POLL_TICKERS)} streaming tickers, "
                 f"{MONITOR_POLL_INTERVAL_SEC}s for others, "
                 f"{len(self.engine._HIGH_RES_TICKERS)} high-res (1-min bars + ORB)")
        self._cycle_count = 0; self._slow_n = max(1, MONITOR_POLL_INTERVAL_SEC // MONITOR_POLL_INTERVAL_FAST_SEC)
        while not self._stop_event.is_set():
            try: self._poll_cycle()
            except Exception as e: log.error(f"Thesis monitor poll error: {e}", exc_info=True)
            self._cycle_count += 1; self._stop_event.wait(MONITOR_POLL_INTERVAL_FAST_SEC)
    def _poll_cycle(self):
        if not self._enabled: return
        # Weekend guard — no equity session Sat/Sun
        try:
            from zoneinfo import ZoneInfo
            _now_ct = datetime.now(ZoneInfo("America/Chicago"))
        except Exception:
            try:
                import pytz; _now_ct = datetime.now(pytz.timezone("America/Chicago"))
            except Exception:
                _now_ct = datetime.utcnow()
        if _now_ct.weekday() >= 5: return  # Saturday=5, Sunday=6
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
            # Issue 1: Don't skip tickers without thesis if they have active trades
            # (conviction trades create state but may not have thesis)
            if not thesis:
                state = self.engine.get_state(ticker)
                has_active = state and any(
                    t.status in ("OPEN", "SCALED", "TRAILED")
                    for t in state.active_trades)
                if not has_active:
                    continue
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
            # No active trade to evaluate — no quality badge on any alert type.
            # Using "📡" for non-entry was causing "📡 📡" duplication since
            # the THESIS ALERT header already contains 📡.
            badge = ""

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

        # ── Natural Targets — show where the trade SHOULD go ──────────
        # Displayed on entry alerts AND trade mgmt so you can decide hold vs exit.
        if active_trade and active_trade.targets:
            tgt_lines = []
            for i, t in enumerate(active_trade.targets):
                dist = abs(t - price)
                dist_pct = dist / price * 100 if price > 0 else 0
                label = f"T{i+1}"
                # Mark gamma flip target specially
                if thesis and thesis.levels.gamma_flip and abs(t - thesis.levels.gamma_flip) < 0.10:
                    label += " (γ flip)"
                tgt_lines.append(f"  {label}: ${t:.2f} ({dist_pct:.2f}% / ${dist:.2f})")
            if tgt_lines:
                lines.append("")
                if is_exit:
                    lines.append(f"🎯 TARGET REVIEW ({active_trade.direction}):")
                else:
                    lines.append(f"🎯 NATURAL TARGETS ({active_trade.direction}):")
                lines.extend(tgt_lines)
                # Show risk/reward ratio
                stop_ref = active_trade.trail_stop or active_trade.stop_level
                risk = abs(price - stop_ref)
                if risk > 0 and active_trade.targets:
                    reward = abs(active_trade.targets[0] - price)
                    rr = reward / risk if risk > 0 else 0
                    lines.append(f"  R:R to T1 = {rr:.1f}:1 (risk ${risk:.2f} / reward ${reward:.2f})")
                # ── Exit-specific: show MFE and how far target was missed by ──
                if is_exit and active_trade.max_favorable > 0:
                    mfe = active_trade.max_favorable
                    lines.append(f"  📈 MFE: ${mfe:.2f} (best move in your direction)")
                    if active_trade.targets:
                        t1_dist_at_entry = abs(active_trade.targets[0] - active_trade.entry_price)
                        if t1_dist_at_entry > 0:
                            t1_capture = mfe / t1_dist_at_entry * 100
                            lines.append(f"  📊 T1 capture: {t1_capture:.0f}% of ${t1_dist_at_entry:.2f} move")
        elif is_entry and not is_exit:
            # Entry event but no trade created (validator rejected) — show where
            # targets WOULD be so the trader can evaluate manually.
            if thesis and state:
                ak = event.get("alert_key", "")
                _tgt_dir = "SHORT" if any(x in ak for x in ("fbo_", "conf_short", "rt_short", "rt_fs")) else "LONG"
                _manual_targets = self.engine._find_targets(ticker, thesis, state, price, _tgt_dir)
                if _manual_targets:
                    tgt_lines = []
                    for i, t in enumerate(_manual_targets[:3]):
                        dist = abs(t - price)
                        tgt_lines.append(f"  T{i+1}: ${t:.2f} (${dist:.2f})")
                    lines.append("")
                    lines.append(f"🎯 LEVEL TARGETS ({_tgt_dir}):")
                    lines.extend(tgt_lines)

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
    # v5.1 Fix A: Force volatility_regime from VIX directly.
    # The options engine (v4_result) may label VIX 25 as "ELEVATED" while
    # the regime detector calls it "CRISIS". The CRISIS long option framework
    # depends on this label. Use VIX thresholds as the source of truth.
    _vix_val = vix.get("vix", 0) if isinstance(vix, dict) else 0
    if _vix_val >= 25:
        ctx.volatility_regime = "CRISIS"
    elif _vix_val >= 20:
        ctx.volatility_regime = "ELEVATED"
    elif _vix_val > 0:
        ctx.volatility_regime = "NORMAL"
    # else: keep whatever v4_result provided
    if _vix_val > 0:
        log.info(f"Thesis vol regime: {ctx.volatility_regime} (VIX {_vix_val:.1f})")
    ctx.atm_call_delta   = float(atm_call_delta   or 0.0)
    ctx.atm_call_premium = float(atm_call_premium or 0.0)
    ctx.atm_put_delta    = float(atm_put_delta    or 0.0)
    ctx.atm_put_premium  = float(atm_put_premium  or 0.0)
    return ctx

_monitor_engine = ThesisMonitorEngine()
_monitor_daemon: Optional[ThesisMonitorDaemon] = None
_portfolio_greeks = None  # v5.1: set by app.py via set_portfolio_greeks()
_persistent_state = None  # v6.1: set by app.py for ORB/trade persistence

def get_engine(): return _monitor_engine
def get_daemon(): return _monitor_daemon

def set_portfolio_greeks(pg):
    """Wire the portfolio Greeks aggregator from app.py."""
    global _portfolio_greeks
    _portfolio_greeks = pg
    log.info("Portfolio Greeks aggregator connected to thesis monitor")

def set_persistent_state(ps):
    """Wire PersistentState from app.py for ORB/trade persistence."""
    global _persistent_state
    _persistent_state = ps
    log.info("PersistentState connected to thesis monitor")

def _pg_register(trade):
    """Register a new trade with the portfolio Greeks aggregator."""
    if not _portfolio_greeks:
        return
    try:
        _portfolio_greeks.register_position(
            trade_id=trade.trade_id,
            ticker=trade.ticker,
            direction=trade.direction,
            trade_type=getattr(trade, "trade_type_label", "spread"),
            option_type="put" if trade.direction == "SHORT" else "call",
            contracts=1,
            entry_price=trade.entry_premium or trade.entry_price,
            total_risk=trade.entry_premium * 100 if trade.entry_premium else trade.entry_price * 100,
            delta=trade.entry_delta or (0.50 if trade.direction == "LONG" else -0.50),
            spot=trade.entry_price,
        )
    except Exception as e:
        log.debug(f"Portfolio Greeks register failed: {e}")

def _pg_close(trade):
    """Remove a closed trade from the portfolio Greeks aggregator."""
    if not _portfolio_greeks:
        return
    try:
        reason = getattr(trade, "close_reason", None) or trade.status
        _portfolio_greeks.close_position(trade.trade_id, reason)
    except Exception as e:
        log.debug(f"Portfolio Greeks close failed: {e}")

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
