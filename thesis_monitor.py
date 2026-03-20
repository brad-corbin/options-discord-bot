# thesis_monitor.py
# ═══════════════════════════════════════════════════════════════════
# v1.1 Thesis Monitor — Continuous Context-Aware Price Monitoring
#
# v1.1: IntradayLevelTracker — builds support/resistance from the
#   prices the daemon already collects. Zero additional API calls.
#   Detects: rejection zones, session high/low, consolidation edges,
#   sharp-move origins (order blocks). Feeds into failed move engine.
#
# NOTE: Educational/demo code. Not financial advice.
# ═══════════════════════════════════════════════════════════════════

import logging
import threading
import time
import json
from datetime import datetime, timezone
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass, field, asdict

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────

MONITOR_POLL_INTERVAL_SEC = 300          # 5 minutes between checks
MONITOR_ALERT_COOLDOWN_SEC = 600         # min 10 min between same-type alerts
MONITOR_MAX_BREAK_AGE_SEC = 900          # 15 min window for failed move detection
MONITOR_MOMENTUM_LOOKBACK = 5            # number of price points for momentum calc
MONITOR_RECLAIM_THRESHOLD_PCT = 0.015    # 0.015% above/below level = reclaim
MONITOR_STALL_THRESHOLD_PCT = 0.008      # moves < this = stalling
MONITOR_ENABLED_TICKERS = ["SPY", "QQQ"]

# Intraday level detection config
INTRADAY_MIN_TOUCHES = 2                 # min times price must touch a zone to be a level
INTRADAY_ZONE_TOLERANCE_PCT = 0.04       # 0.04% = ~$0.26 on SPY — how close counts as "same level"
INTRADAY_MIN_PRICES_FOR_LEVELS = 6       # need ~30 min of data before detecting levels
INTRADAY_SHARP_MOVE_THRESHOLD = 0.12     # 0.12% in one 5m candle = sharp move
INTRADAY_CONSOLIDATION_CANDLES = 4       # 4 candles in tight range = consolidation
INTRADAY_CONSOLIDATION_RANGE_PCT = 0.06  # range < 0.06% of price = tight


# ─────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────

@dataclass
class ThesisLevels:
    """Key levels from an EM card — set once, referenced all day."""
    gamma_flip: Optional[float] = None
    local_resistance: Optional[float] = None
    local_support: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gamma_wall: Optional[float] = None
    pin_zone_low: Optional[float] = None
    pin_zone_high: Optional[float] = None
    micro_trigger_up: Optional[float] = None
    micro_trigger_down: Optional[float] = None
    range_break_up: Optional[float] = None
    range_break_down: Optional[float] = None
    pivot: Optional[float] = None
    r1: Optional[float] = None
    s1: Optional[float] = None
    fib_support: Optional[float] = None
    fib_resistance: Optional[float] = None
    vpoc: Optional[float] = None
    max_pain: Optional[float] = None
    em_high: Optional[float] = None   # 1σ upper
    em_low: Optional[float] = None    # 1σ lower
    em_2sd_high: Optional[float] = None
    em_2sd_low: Optional[float] = None


@dataclass
class ThesisContext:
    """Full thesis context — set from EM card, enriched over the day."""
    ticker: str = ""
    bias: str = "NEUTRAL"
    bias_score: int = 0
    gex_sign: str = "positive"             # positive or negative
    gex_value: float = 0.0
    dex_value: float = 0.0
    vanna_value: float = 0.0
    charm_value: float = 0.0
    regime: str = "UNKNOWN"                # TRENDING, SUPPRESSING, MIXED
    volatility_regime: str = "NORMAL"
    vix: float = 20.0
    iv: float = 0.20
    prior_day_close: Optional[float] = None
    prior_day_context: str = "NORMAL"      # SQUEEZE_INTO_CLOSE, BREAKDOWN_INTO_CLOSE, etc.
    session_label: str = ""
    levels: ThesisLevels = field(default_factory=ThesisLevels)
    created_at: str = ""
    spot_at_creation: float = 0.0


@dataclass
class BreakAttempt:
    """Record of a price break at a level."""
    level: float
    level_name: str          # e.g. "local_support", "intraday_support"
    direction: str           # "UP" or "DOWN"
    break_price: float
    break_time: float        # monotonic timestamp
    detected_as_failed: bool = False
    candles_since: int = 0


@dataclass
class IntradayLevel:
    """A support or resistance level discovered from intraday price action."""
    price: float
    kind: str                # "support" or "resistance"
    source: str              # "rejection_zone", "session_high", "session_low",
                             # "sharp_move_origin", "consolidation_edge"
    touches: int = 0
    first_seen_ts: float = 0.0
    last_touched_ts: float = 0.0
    active: bool = True


@dataclass
class MonitorState:
    """Evolving state tracked throughout the day."""
    status: str = "FRESH"
    momentum: str = "NEUTRAL"
    above_gamma_flip: Optional[bool] = None
    price_history: list = field(default_factory=list)
    break_attempts: list = field(default_factory=list)
    failed_moves: list = field(default_factory=list)
    alert_history: dict = field(default_factory=dict)
    last_guidance_ts: float = 0.0
    check_count: int = 0
    prior_day_applied: bool = False
    # v1.1: Intraday levels
    intraday_levels: list = field(default_factory=list)
    session_high: Optional[float] = None
    session_low: Optional[float] = None
    session_high_time: str = ""
    session_low_time: str = ""


# ─────────────────────────────────────────────────────────
# TIME-OF-DAY LOGIC
# ─────────────────────────────────────────────────────────

def _get_time_phase_ct() -> dict:
    """Get current Central Time trading phase."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        try:
            import pytz
            now = datetime.now(pytz.timezone("America/Chicago"))
        except Exception:
            now = datetime.utcnow()

    h, m = now.hour, now.minute
    mins = h * 60 + m

    if mins < 510:    # before 8:30
        return {"phase": "PRE_MARKET", "label": "Pre-Market", "favor": "wait",
                "note": "Levels are set — wait for open to confirm."}
    if mins < 540:    # 8:30 – 9:00
        return {"phase": "OPEN", "label": "Open (first 30 min)", "favor": "caution",
                "note": "Expansion / discovery window — let levels prove before acting."}
    if mins < 600:    # 9:00 – 10:00
        return {"phase": "MORNING", "label": "Morning Session", "favor": "breakout",
                "note": "Breakouts are more reliable now. Watch for trend establishment."}
    if mins < 720:    # 10:00 – 12:00
        return {"phase": "MIDDAY", "label": "Midday", "favor": "failed_move",
                "note": "Chop zone — failed moves are the best play here."}
    if mins < 810:    # 12:00 – 1:30
        return {"phase": "AFTERNOON", "label": "Afternoon", "favor": "trend_resumption",
                "note": "Trend resumption or reversal — momentum matters now."}
    if mins < 870:    # 1:30 – 2:30
        return {"phase": "POWER_HOUR", "label": "Power Hour", "favor": "pin_or_expand",
                "note": "GEX+ = pinning. GEX- = expansion. Adjust accordingly."}
    if mins < 960:    # 2:30 – 4:00
        return {"phase": "CLOSE", "label": "Into Close", "favor": "pin",
                "note": "Favor pin / mean reversion. Reduce size on new entries."}
    return {"phase": "AFTER_HOURS", "label": "After Hours", "favor": "wait",
            "note": "Session over — review and plan for tomorrow."}


# ─────────────────────────────────────────────────────────
# INTRADAY LEVEL TRACKER — Zero API calls
# ─────────────────────────────────────────────────────────

class IntradayLevelTracker:
    """Discovers intraday S/R from prices already being collected."""

    @staticmethod
    def update(state: MonitorState, price: float, now: float) -> List[dict]:
        events = []
        prices = [p["price"] for p in state.price_history]
        n = len(prices)
        if n < 2:
            return events
        spot = price
        tol = spot * INTRADAY_ZONE_TOLERANCE_PCT / 100

        # 1. Session high/low
        current_high = max(prices)
        current_low = min(prices)
        if state.session_high is None or current_high > state.session_high:
            state.session_high = current_high
            for p in state.price_history:
                if p["price"] == current_high:
                    state.session_high_time = p.get("time_str", "")
                    break
            IntradayLevelTracker._upsert_level(state, current_high, "resistance", "session_high", now)
        if state.session_low is None or current_low < state.session_low:
            state.session_low = current_low
            for p in state.price_history:
                if p["price"] == current_low:
                    state.session_low_time = p.get("time_str", "")
                    break
            IntradayLevelTracker._upsert_level(state, current_low, "support", "session_low", now)

        if n < INTRADAY_MIN_PRICES_FOR_LEVELS:
            return events

        # 2. Rejection zones — prices touched 2+ times then moved away
        zone_counts: Dict[float, list] = {}
        for i, p in enumerate(prices):
            placed = False
            for rep in zone_counts:
                if abs(p - rep) <= tol:
                    zone_counts[rep].append(i)
                    placed = True
                    break
            if not placed:
                zone_counts[p] = [i]

        for rep_price, indices in zone_counts.items():
            if len(indices) < INTRADAY_MIN_TOUCHES:
                continue
            has_departure = False
            for idx in indices:
                subsequent = prices[idx + 1:idx + 4] if idx + 1 < n else []
                for sp in subsequent:
                    if abs(sp - rep_price) > tol * 2.5:
                        has_departure = True
                        break
                if has_departure:
                    break
            if not has_departure:
                continue

            bounces_up = sum(1 for idx in indices if idx + 1 < n and prices[idx + 1] > rep_price + tol * 0.5)
            bounces_down = sum(1 for idx in indices if idx + 1 < n and prices[idx + 1] < rep_price - tol * 0.5)

            if bounces_up >= INTRADAY_MIN_TOUCHES:
                if IntradayLevelTracker._upsert_level(state, rep_price, "support", "rejection_zone", now, touches=len(indices)):
                    events.append({
                        "msg": f"📍 NEW 5m SUPPORT at ${rep_price:.2f} ({len(indices)} touches, bounced up each time). "
                               f"Watch for failed break below.",
                        "type": "info", "priority": 3,
                        "alert_key": f"id_sup_{rep_price:.2f}",
                    })
            if bounces_down >= INTRADAY_MIN_TOUCHES:
                if IntradayLevelTracker._upsert_level(state, rep_price, "resistance", "rejection_zone", now, touches=len(indices)):
                    events.append({
                        "msg": f"📍 NEW 5m RESISTANCE at ${rep_price:.2f} ({len(indices)} touches, rejected down each time). "
                               f"Watch for failed break above.",
                        "type": "info", "priority": 3,
                        "alert_key": f"id_res_{rep_price:.2f}",
                    })

        # 3. Sharp move origins (order block concept)
        if n >= 3:
            for i in range(max(n - 12, 1), n):
                move = prices[i] - prices[i - 1]
                move_pct = abs(move) / prices[i - 1] * 100
                if move_pct >= INTRADAY_SHARP_MOVE_THRESHOLD:
                    origin = prices[i - 1]
                    if move > 0:
                        if IntradayLevelTracker._upsert_level(state, origin, "support", "sharp_move_origin", now):
                            events.append({
                                "msg": f"📍 SHARP MOVE ORIGIN: Price rocketed up from ${origin:.2f}. "
                                       f"Now intraday support — trapped shorts sit here.",
                                "type": "info", "priority": 3,
                                "alert_key": f"sharp_sup_{origin:.2f}",
                            })
                    else:
                        if IntradayLevelTracker._upsert_level(state, origin, "resistance", "sharp_move_origin", now):
                            events.append({
                                "msg": f"📍 SHARP MOVE ORIGIN: Price dumped from ${origin:.2f}. "
                                       f"Now intraday resistance — trapped longs sit here.",
                                "type": "info", "priority": 3,
                                "alert_key": f"sharp_res_{origin:.2f}",
                            })

        # 4. Consolidation edges
        if n >= INTRADAY_CONSOLIDATION_CANDLES:
            recent = prices[-INTRADAY_CONSOLIDATION_CANDLES:]
            rng = max(recent) - min(recent)
            rng_pct = rng / spot * 100
            if rng_pct <= INTRADAY_CONSOLIDATION_RANGE_PCT and rng > 0:
                cons_high = max(recent)
                cons_low = min(recent)
                IntradayLevelTracker._upsert_level(state, cons_high, "resistance", "consolidation_edge", now)
                IntradayLevelTracker._upsert_level(state, cons_low, "support", "consolidation_edge", now)
                events.append({
                    "msg": f"📍 CONSOLIDATION: Flat between ${cons_low:.2f}–${cons_high:.2f} "
                           f"for {INTRADAY_CONSOLIDATION_CANDLES * 5} min. Break with momentum is actionable.",
                    "type": "info", "priority": 2,
                    "alert_key": f"cons_{cons_low:.1f}_{cons_high:.1f}",
                })

        # Deactivate stale levels
        for lvl in state.intraday_levels:
            if not lvl.active:
                continue
            if abs(spot - lvl.price) > tol * 8 and (now - lvl.last_touched_ts) > 2700:
                lvl.active = False

        return events

    @staticmethod
    def _upsert_level(state, price, kind, source, now, touches=1):
        tol = price * INTRADAY_ZONE_TOLERANCE_PCT / 100
        for lvl in state.intraday_levels:
            if abs(lvl.price - price) <= tol and lvl.kind == kind:
                lvl.touches = max(lvl.touches, touches)
                lvl.last_touched_ts = now
                lvl.active = True
                return False
        state.intraday_levels.append(IntradayLevel(
            price=round(price, 2), kind=kind, source=source,
            touches=touches, first_seen_ts=now, last_touched_ts=now, active=True,
        ))
        return True

    @staticmethod
    def get_active_levels(state: MonitorState, price: float) -> dict:
        active = [l for l in state.intraday_levels if l.active]
        supports = sorted([l for l in active if l.kind == "support" and l.price < price], key=lambda l: price - l.price)
        resistances = sorted([l for l in active if l.kind == "resistance" and l.price > price], key=lambda l: l.price - price)
        return {
            "support": supports[0] if supports else None,
            "resistance": resistances[0] if resistances else None,
            "all_support": supports[:3],
            "all_resistance": resistances[:3],
        }


# ─────────────────────────────────────────────────────────
# THESIS MONITOR ENGINE
# ─────────────────────────────────────────────────────────

class ThesisMonitorEngine:
    """
    Core engine that evaluates price action against the thesis.

    NOT a standalone thread — the ThesisMonitorDaemon (below) handles
    scheduling and Telegram posting. This class is pure logic.
    """

    def __init__(self):
        self._theses: Dict[str, ThesisContext] = {}
        self._states: Dict[str, MonitorState] = {}
        self._lock = threading.Lock()

    def store_thesis(self, ticker: str, thesis: ThesisContext):
        """Called by app.py after each EM card. Resets monitoring state."""
        with self._lock:
            self._theses[ticker] = thesis
            # Preserve failed moves from earlier session if still relevant
            old_state = self._states.get(ticker)
            new_state = MonitorState()
            if old_state and old_state.failed_moves:
                # Keep failed moves < 30 min old
                now = time.monotonic()
                new_state.failed_moves = [
                    fm for fm in old_state.failed_moves
                    if (now - fm.break_time) < 1800
                ]
            # Preserve intraday levels from earlier in the session
            if old_state and old_state.intraday_levels:
                new_state.intraday_levels = [l for l in old_state.intraday_levels if l.active]
                new_state.session_high = old_state.session_high
                new_state.session_low = old_state.session_low
                new_state.session_high_time = old_state.session_high_time
                new_state.session_low_time = old_state.session_low_time
            self._states[ticker] = new_state
            log.info(f"Thesis stored: {ticker} | bias={thesis.bias} score={thesis.bias_score} "
                     f"gex={thesis.gex_sign} regime={thesis.regime}")

    def get_thesis(self, ticker: str) -> Optional[ThesisContext]:
        return self._theses.get(ticker)

    def get_state(self, ticker: str) -> Optional[MonitorState]:
        return self._states.get(ticker)

    def evaluate(self, ticker: str, price: float) -> List[dict]:
        """
        Evaluate a new price against the thesis.

        Returns a list of events: [{msg, type, priority}]
          type: info, warning, alert, critical
          priority: 1 (low) to 5 (high)
        """
        with self._lock:
            thesis = self._theses.get(ticker)
            state = self._states.get(ticker)
            if not thesis or not state:
                return []

            events = []
            now = time.monotonic()
            lvl = thesis.levels
            prev_price = state.price_history[-1]["price"] if state.price_history else None

            # Record price
            try:
                from zoneinfo import ZoneInfo
                ts = datetime.now(ZoneInfo("America/Chicago")).strftime("%I:%M %p")
            except Exception:
                ts = datetime.utcnow().strftime("%H:%M")

            state.price_history.append({"price": price, "time_str": ts, "ts_mono": now})
            # Keep last 60 data points (~5 hours at 5 min intervals)
            if len(state.price_history) > 60:
                state.price_history = state.price_history[-60:]
            state.check_count += 1

            # ── Prior day context (first check only) ──
            if not state.prior_day_applied and thesis.prior_day_context != "NORMAL":
                state.prior_day_applied = True
                if thesis.prior_day_context == "SQUEEZE_INTO_CLOSE":
                    events.append({
                        "msg": f"📅 PRIOR DAY CONTEXT: Yesterday ended with a squeeze into close. "
                               f"Expect continuation pressure early OR a fade. Watch the first 15 min.",
                        "type": "info", "priority": 2,
                    })
                elif thesis.prior_day_context == "BREAKDOWN_INTO_CLOSE":
                    events.append({
                        "msg": f"📅 PRIOR DAY CONTEXT: Yesterday ended with a breakdown into close. "
                               f"Gap-down risk or early selling possible.",
                        "type": "warning", "priority": 3,
                    })

            # ── Momentum tracking ──
            events.extend(self._evaluate_momentum(state, price))

            # ── v1.1: Update intraday levels from price history ──
            intraday_events = IntradayLevelTracker.update(state, price, now)
            events.extend(intraday_events)

            # ── Break attempt detection ──
            if prev_price is not None:
                events.extend(self._detect_breaks(thesis, state, price, prev_price, now))

            # ── Failed move detection ──
            events.extend(self._detect_failed_moves(thesis, state, price, now))

            # ── Gamma flip crossover ──
            events.extend(self._check_gamma_flip(thesis, state, price))

            # ── Status evolution ──
            if state.status == "FRESH" and state.check_count >= 2:
                state.status = "DEVELOPING"

            # ── Apply cooldowns ──
            events = self._apply_cooldowns(state, events, now)

            return events

    def build_guidance(self, ticker: str, price: float) -> List[dict]:
        """
        Build full plain English guidance for current price.

        Returns list of {text, type} items.
        type: context, time, bullish, bearish, warning, critical, neutral, divider, info
        """
        thesis = self._theses.get(ticker)
        state = self._states.get(ticker)
        if not thesis or not state:
            return [{"text": f"No thesis loaded for {ticker}. Run /em first.", "type": "neutral"}]

        guidance = []
        lvl = thesis.levels
        tp = _get_time_phase_ct()

        # ── Thesis header ──
        guidance.append({
            "text": f"THESIS: {thesis.bias} (score {thesis.bias_score}/14) | "
                    f"GEX {thesis.gex_sign} ({thesis.gex_value:+.1f}M) | Regime: {thesis.regime}",
            "type": "context",
        })

        # ── Time of day ──
        guidance.append({"text": f"{tp['label']}: {tp['note']}", "type": "time"})

        # ── Gamma flip position ──
        if lvl.gamma_flip is not None:
            if price > lvl.gamma_flip:
                guidance.append({
                    "text": f"Price is ABOVE gamma flip (${lvl.gamma_flip:.2f}) — bullish structure. "
                            f"Dealers are buying dips. Favor long setups.",
                    "type": "bullish",
                })
            else:
                guidance.append({
                    "text": f"Price is BELOW gamma flip (${lvl.gamma_flip:.2f}) — bearish/trending structure. "
                            f"Breakdowns can accelerate.",
                    "type": "bearish",
                })

        # ── Pin zone check ──
        if lvl.pin_zone_low is not None and lvl.pin_zone_high is not None:
            if lvl.pin_zone_low <= price <= lvl.pin_zone_high:
                if thesis.gex_sign == "positive":
                    guidance.append({
                        "text": f"INSIDE PIN ZONE (${lvl.pin_zone_low:.2f}–${lvl.pin_zone_high:.2f}) "
                                f"with positive GEX — price wants to stay here. "
                                f"Don't chase breakouts — TRADE FAILURES instead.",
                        "type": "warning",
                    })

        # ── Micro trigger position ──
        if lvl.micro_trigger_up is not None and lvl.micro_trigger_down is not None:
            if lvl.micro_trigger_down < price < lvl.micro_trigger_up:
                guidance.append({
                    "text": f"IN NO-MAN'S LAND between micro triggers "
                            f"(${lvl.micro_trigger_down:.2f}–${lvl.micro_trigger_up:.2f}). "
                            f"Wait for a trigger to fire before acting.",
                    "type": "neutral",
                })
            elif price >= lvl.micro_trigger_up:
                guidance.append({
                    "text": f"ABOVE micro trigger ${lvl.micro_trigger_up:.2f} — bullish bias active. "
                            f"Look for pullbacks to this level as re-entry.",
                    "type": "bullish",
                })
            elif price <= lvl.micro_trigger_down:
                guidance.append({
                    "text": f"BELOW micro trigger ${lvl.micro_trigger_down:.2f} — bearish bias active. "
                            f"Look for bounces to this level as short re-entry.",
                    "type": "bearish",
                })

        # ── Momentum state ──
        momentum_map = {
            "ACCELERATING_UP": ("🚀 Momentum ACCELERATING UP — 5m candles getting bigger. Let winners run, trail stops.", "bullish"),
            "ACCELERATING_DOWN": ("🚀 Momentum ACCELERATING DOWN — selling intensifying on 5m. Don't catch the knife.", "bearish"),
            "LOSING_UPSIDE_MOMENTUM": ("⚠️ MOMENTUM WARNING: Last 5m candle went against the uptrend. If long, tighten stops.", "warning"),
            "LOSING_DOWNSIDE_MOMENTUM": ("⚠️ MOMENTUM WARNING: Last 5m candle went against the downtrend. If short, tighten stops.", "warning"),
            "STALLING": ("Price is STALLING on 5m — no clear direction. This is where traps happen. Wait for decisive candle.", "neutral"),
            "DRIFTING_UP": ("Drifting higher on 5m — mild bullish pressure.", "info"),
            "DRIFTING_DOWN": ("Drifting lower on 5m — mild bearish pressure.", "info"),
        }
        if state.momentum in momentum_map:
            text, mtype = momentum_map[state.momentum]
            guidance.append({"text": text, "type": mtype})

        # ── v1.1: Intraday levels section ──
        intraday = IntradayLevelTracker.get_active_levels(state, price)
        id_sup = intraday["support"]
        id_res = intraday["resistance"]

        if id_sup or id_res:
            guidance.append({"text": "— INTRADAY LEVELS (formed today from 5m price action) —", "type": "divider"})

            if id_sup:
                action = "Failed break below = squeeze long." if thesis.gex_sign == "positive" else "Break with momentum = valid short."
                guidance.append({
                    "text": f"Intraday support: ${id_sup.price:.2f} "
                            f"({id_sup.source.replace('_', ' ')}, {id_sup.touches} touches). {action}",
                    "type": "info",
                })
            if id_res:
                action = "Failed break above = fade short." if thesis.gex_sign == "positive" else "Break with momentum = valid long."
                guidance.append({
                    "text": f"Intraday resistance: ${id_res.price:.2f} "
                            f"({id_res.source.replace('_', ' ')}, {id_res.touches} touches). {action}",
                    "type": "info",
                })

        # ── Session range ──
        if state.session_high is not None and state.session_low is not None:
            rng = state.session_high - state.session_low
            guidance.append({
                "text": f"Session range: ${state.session_low:.2f} ({state.session_low_time}) → "
                        f"${state.session_high:.2f} ({state.session_high_time}) = ${rng:.2f} wide",
                "type": "context",
            })

        # ── Failed move setups (the key differentiator) ──
        if state.failed_moves:
            last_fail = state.failed_moves[-1]
            age_min = (time.monotonic() - last_fail.break_time) / 60
            if age_min < 30:  # still relevant
                src_note = " (intraday level)" if "intraday" in last_fail.level_name else ""
                if last_fail.direction == "DOWN":
                    guidance.append({
                        "text": f"🔥 ACTIVE SQUEEZE SETUP: Failed breakdown at ${last_fail.level:.2f} "
                                f"({last_fail.level_name}{src_note}) was reclaimed. Shorts are trapped. "
                                f"Bias flips LONG. Stop: below ${last_fail.level:.2f}.",
                        "type": "critical",
                    })
                else:
                    guidance.append({
                        "text": f"🔥 ACTIVE FADE SETUP: Failed breakout at ${last_fail.level:.2f} "
                                f"({last_fail.level_name}{src_note}) was lost. Longs are trapped. "
                                f"Bias flips SHORT. Stop: above ${last_fail.level:.2f}.",
                        "type": "critical",
                    })
        elif state.status == "BREAK_IN_PROGRESS":
            guidance.append({
                "text": "⏳ BREAK IN PROGRESS: A level was just broken. Do NOT trade yet — "
                        "wait 2-3 five-minute candles for follow-through. If it fails and reclaims, that's the trade.",
                "type": "warning",
            })

        # ── What to watch next ──
        guidance.append({"text": "— WHAT TO WATCH NEXT —", "type": "divider"})

        # Use intraday levels if available, fall back to daily
        nearest_sup = id_sup.price if id_sup else lvl.local_support
        nearest_res = id_res.price if id_res else lvl.local_resistance
        sup_label = "intraday support" if id_sup else "daily support"
        res_label = "intraday resistance" if id_res else "daily resistance"

        if nearest_res is not None and nearest_sup is not None:
            dist_res = nearest_res - price
            dist_sup = price - nearest_sup

            if dist_res < dist_sup:
                guidance.append({
                    "text": f"Nearest test: {res_label} at ${nearest_res:.2f} ({dist_res:.2f} away). "
                            f"If it rejects → short toward ${nearest_sup:.2f}. "
                            f"If it breaks AND holds on 5m → long.",
                    "type": "info",
                })
            else:
                guidance.append({
                    "text": f"Nearest test: {sup_label} at ${nearest_sup:.2f} ({dist_sup:.2f} away). "
                            f"If it bounces → long toward ${nearest_res:.2f}. "
                            f"If it breaks AND fails on 5m → squeeze long (the best trade).",
                    "type": "info",
                })

        # ── GEX-specific behavior ──
        if thesis.gex_sign == "positive":
            if tp["phase"] in ("POWER_HOUR", "CLOSE"):
                guidance.append({
                    "text": "GEX+ near close = maximum pinning. Expect price pulled toward max pain / pin zone. Fade extremes.",
                    "type": "time",
                })
            guidance.append({
                "text": "GEX+ reminder: Failed moves and mean reversion are MORE likely than continuation.",
                "type": "context",
            })
        else:
            guidance.append({
                "text": "GEX- reminder: Moves can ACCELERATE. Breakdowns are more dangerous. Wider stops or smaller size.",
                "type": "context",
            })

        return guidance

    # ── Internal analysis methods ──

    def _evaluate_momentum(self, state: MonitorState, price: float) -> List[dict]:
        """Track momentum using recent price history."""
        events = []
        recent = [p["price"] for p in state.price_history[-MONITOR_MOMENTUM_LOOKBACK:]]
        if len(recent) < 3:
            return events

        diffs = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        avg_diff = sum(diffs) / len(diffs)
        last_diff = diffs[-1]
        old_momentum = state.momentum

        spot = recent[-1] if recent else price
        threshold = spot * MONITOR_STALL_THRESHOLD_PCT

        if abs(avg_diff) < threshold:
            state.momentum = "STALLING"
        elif avg_diff > 0 and last_diff > 0:
            state.momentum = "ACCELERATING_UP" if last_diff > avg_diff * 1.5 else "DRIFTING_UP"
        elif avg_diff < 0 and last_diff < 0:
            state.momentum = "ACCELERATING_DOWN" if last_diff < avg_diff * 1.5 else "DRIFTING_DOWN"
        elif avg_diff > 0 and last_diff <= 0:
            state.momentum = "LOSING_UPSIDE_MOMENTUM"
        elif avg_diff < 0 and last_diff >= 0:
            state.momentum = "LOSING_DOWNSIDE_MOMENTUM"

        # Alert on momentum transitions that matter
        if state.momentum != old_momentum:
            if state.momentum == "LOSING_UPSIDE_MOMENTUM" and old_momentum in ("ACCELERATING_UP", "DRIFTING_UP"):
                events.append({
                    "msg": f"⚠️ {state.price_history[-1].get('time_str', '')} — Upside momentum fading. "
                           f"Last move was against the trend. If long, tighten stops.",
                    "type": "warning", "priority": 4,
                    "alert_key": "momentum_fade_up",
                })
            elif state.momentum == "LOSING_DOWNSIDE_MOMENTUM" and old_momentum in ("ACCELERATING_DOWN", "DRIFTING_DOWN"):
                events.append({
                    "msg": f"⚠️ {state.price_history[-1].get('time_str', '')} — Downside momentum fading. "
                           f"Bounce attempt forming. If short, tighten stops.",
                    "type": "warning", "priority": 4,
                    "alert_key": "momentum_fade_down",
                })

        return events

    def _detect_breaks(self, thesis: ThesisContext, state: MonitorState,
                       price: float, prev_price: float, now: float) -> List[dict]:
        """Detect when price crosses a key level (daily OR intraday)."""
        events = []
        lvl = thesis.levels

        # Define level-pairs to watch: (level_value, level_name, is_support)
        watch_levels = []
        if lvl.local_support is not None:
            watch_levels.append((lvl.local_support, "daily_support", True))
        if lvl.local_resistance is not None:
            watch_levels.append((lvl.local_resistance, "daily_resistance", False))
        if lvl.range_break_down is not None and lvl.range_break_down != lvl.local_support:
            watch_levels.append((lvl.range_break_down, "range_break_down", True))
        if lvl.range_break_up is not None and lvl.range_break_up != lvl.local_resistance:
            watch_levels.append((lvl.range_break_up, "range_break_up", False))
        if lvl.put_wall is not None and lvl.put_wall != lvl.local_support:
            watch_levels.append((lvl.put_wall, "put_wall", True))
        if lvl.call_wall is not None and lvl.call_wall != lvl.local_resistance:
            watch_levels.append((lvl.call_wall, "call_wall", False))

        # v1.1: Add active intraday levels (skip if too close to a daily level)
        tol = price * INTRADAY_ZONE_TOLERANCE_PCT / 100
        for il in state.intraday_levels:
            if not il.active:
                continue
            too_close = any(abs(il.price - dlvl) <= tol for (dlvl, _, _) in watch_levels)
            if too_close:
                continue
            if il.kind == "support":
                watch_levels.append((il.price, f"intraday_support ({il.source.replace('_', ' ')})", True))
            else:
                watch_levels.append((il.price, f"intraday_resistance ({il.source.replace('_', ' ')})", False))

        for level, name, is_support in watch_levels:
            if is_support:
                if prev_price >= level and price < level:
                    ba = BreakAttempt(level=level, level_name=name, direction="DOWN",
                                     break_price=price, break_time=now)
                    state.break_attempts.append(ba)
                    state.status = "BREAK_IN_PROGRESS"
                    is_range = "range" in name
                    is_intraday = "intraday" in name
                    if is_range:
                        events.append({
                            "msg": f"🚨 RANGE BREAK DOWN: Below ${level:.2f} ({name}). "
                                   f"If this fails to continue → SQUEEZE potential is high (GEX={thesis.gex_sign}).",
                            "type": "critical", "priority": 5,
                            "alert_key": f"break_down_{name}_{level:.2f}",
                        })
                    elif is_intraday:
                        events.append({
                            "msg": f"🔻 INTRADAY BREAK: Price broke below ${level:.2f} ({name}). "
                                   f"Watching 5m candles for follow-through or failure.",
                            "type": "alert", "priority": 4,
                            "alert_key": f"break_down_id_{level:.2f}",
                        })
                    else:
                        events.append({
                            "msg": f"🔻 BREAK ATTEMPT: Price broke below {name} ${level:.2f}. "
                                   f"Watching 5m candles for follow-through or failure.",
                            "type": "alert", "priority": 4,
                            "alert_key": f"break_down_{name}",
                        })
            else:
                if prev_price <= level and price > level:
                    ba = BreakAttempt(level=level, level_name=name, direction="UP",
                                     break_price=price, break_time=now)
                    state.break_attempts.append(ba)
                    state.status = "BREAK_IN_PROGRESS"
                    is_range = "range" in name
                    is_intraday = "intraday" in name
                    if is_range:
                        events.append({
                            "msg": f"🚨 RANGE BREAK UP: Above ${level:.2f} ({name}). "
                                   f"Watching for continuation or trap.",
                            "type": "critical", "priority": 5,
                            "alert_key": f"break_up_{name}_{level:.2f}",
                        })
                    elif is_intraday:
                        events.append({
                            "msg": f"🔺 INTRADAY BREAK: Price broke above ${level:.2f} ({name}). "
                                   f"Watching 5m candles for follow-through or failure.",
                            "type": "alert", "priority": 4,
                            "alert_key": f"break_up_id_{level:.2f}",
                        })
                    else:
                        events.append({
                            "msg": f"🔺 BREAK ATTEMPT: Price broke above {name} ${level:.2f}. "
                                   f"Watching 5m candles for follow-through or failure.",
                            "type": "alert", "priority": 4,
                            "alert_key": f"break_up_{name}",
                        })

        return events

    def _detect_failed_moves(self, thesis: ThesisContext, state: MonitorState,
                             price: float, now: float) -> List[dict]:
        """Detect when a break attempt fails and price reclaims the level."""
        events = []
        reclaim_buffer = price * MONITOR_RECLAIM_THRESHOLD_PCT

        for ba in state.break_attempts:
            if ba.detected_as_failed:
                continue
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC:
                continue  # too old

            ba.candles_since += 1

            # Need at least 2 candles (checks) for the break to have had time to continue or fail
            if ba.candles_since < 2:
                continue

            if ba.direction == "DOWN" and price > ba.level + reclaim_buffer:
                # Failed breakdown → SQUEEZE
                ba.detected_as_failed = True
                state.failed_moves.append(ba)
                state.status = "FAILED_MOVE_ACTIVE"

                gex_note = ""
                if thesis.gex_sign == "positive":
                    gex_note = " GEX+ amplifies mean reversion — squeeze probability is HIGH."

                source_note = " (intraday level)" if "intraday" in ba.level_name else ""

                events.append({
                    "msg": f"🔥 FAILED BREAKDOWN at ${ba.level:.2f} ({ba.level_name}{source_note}) — "
                           f"price reclaimed to ${price:.2f}. "
                           f"Shorts are trapped below. SQUEEZE SETUP LONG. "
                           f"Stop: below ${ba.level:.2f}.{gex_note}",
                    "type": "critical", "priority": 5,
                    "alert_key": f"failed_break_{ba.level:.2f}",
                })

            elif ba.direction == "UP" and price < ba.level - reclaim_buffer:
                # Failed breakout → FADE
                ba.detected_as_failed = True
                state.failed_moves.append(ba)
                state.status = "FAILED_MOVE_ACTIVE"

                gex_note = ""
                if thesis.gex_sign == "positive":
                    gex_note = " GEX+ amplifies mean reversion — fade probability is HIGH."

                source_note = " (intraday level)" if "intraday" in ba.level_name else ""

                events.append({
                    "msg": f"🔥 FAILED BREAKOUT at ${ba.level:.2f} ({ba.level_name}{source_note}) — "
                           f"price fell back to ${price:.2f}. "
                           f"Longs are trapped above. FADE SETUP SHORT. "
                           f"Stop: above ${ba.level:.2f}.{gex_note}",
                    "type": "critical", "priority": 5,
                    "alert_key": f"failed_break_{ba.level_name}",
                })

        return events

    def _check_gamma_flip(self, thesis: ThesisContext, state: MonitorState,
                          price: float) -> List[dict]:
        """Track gamma flip crossovers."""
        events = []
        flip = thesis.levels.gamma_flip
        if flip is None:
            return events

        above = price > flip
        if state.above_gamma_flip is not None and above != state.above_gamma_flip:
            if above:
                events.append({
                    "msg": f"📈 RECLAIMED GAMMA FLIP (${flip:.2f}) — "
                           f"bullish structure improving, dealers shift to buying dips.",
                    "type": "critical", "priority": 5,
                    "alert_key": "gamma_flip_reclaim",
                })
            else:
                events.append({
                    "msg": f"📉 LOST GAMMA FLIP (${flip:.2f}) — "
                           f"bearish pressure increasing, dealers amplify selling.",
                    "type": "critical", "priority": 5,
                    "alert_key": "gamma_flip_lost",
                })
        state.above_gamma_flip = above
        return events

    def _apply_cooldowns(self, state: MonitorState, events: List[dict],
                         now: float) -> List[dict]:
        """Filter events by cooldown to prevent alert spam."""
        filtered = []
        for e in events:
            key = e.get("alert_key", e.get("msg", "")[:40])
            last = state.alert_history.get(key, 0)
            if (now - last) >= MONITOR_ALERT_COOLDOWN_SEC:
                state.alert_history[key] = now
                filtered.append(e)
            else:
                log.debug(f"Alert cooled down: {key}")
        return filtered

    def format_status(self, ticker: str) -> str:
        """Format current monitoring status for /monitor command."""
        thesis = self._theses.get(ticker)
        state = self._states.get(ticker)
        if not thesis:
            return f"📡 {ticker}: No thesis loaded. Run /em to generate one."

        price = state.price_history[-1]["price"] if state and state.price_history else thesis.spot_at_creation
        lines = [
            f"📡 {ticker} — THESIS MONITOR",
            f"Status: {state.status if state else 'INACTIVE'}",
            f"Bias: {thesis.bias} ({thesis.bias_score:+d}/14)",
            f"GEX: {thesis.gex_sign} ({thesis.gex_value:+.1f}M) | Regime: {thesis.regime}",
            f"Last Price: ${price:.2f}",
        ]
        if state:
            lines.append(f"Momentum: {state.momentum}")
            lines.append(f"Checks: {state.check_count}")

            if state.session_high is not None and state.session_low is not None:
                lines.append(f"Session: ${state.session_low:.2f} – ${state.session_high:.2f}")

            active_levels = [l for l in state.intraday_levels if l.active]
            if active_levels:
                sup_levels = [l for l in active_levels if l.kind == "support"]
                res_levels = [l for l in active_levels if l.kind == "resistance"]
                lines.append(f"Intraday levels: {len(sup_levels)} support, {len(res_levels)} resistance")
                for l in sorted(sup_levels, key=lambda x: x.price, reverse=True)[:2]:
                    lines.append(f"  🟢 ${l.price:.2f} support ({l.source}, {l.touches}x)")
                for l in sorted(res_levels, key=lambda x: x.price)[:2]:
                    lines.append(f"  🔴 ${l.price:.2f} resistance ({l.source}, {l.touches}x)")

            if state.break_attempts:
                active = [ba for ba in state.break_attempts
                          if not ba.detected_as_failed and (time.monotonic() - ba.break_time) < MONITOR_MAX_BREAK_AGE_SEC]
                if active:
                    lines.append(f"Active breaks: {len(active)}")
            if state.failed_moves:
                lines.append(f"Failed moves detected: {len(state.failed_moves)}")

        tp = _get_time_phase_ct()
        lines.append(f"Phase: {tp['label']}")
        lines.append(f"Polling: every {MONITOR_POLL_INTERVAL_SEC}s (5m candles, intraday levels active)")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# BACKGROUND DAEMON
# ─────────────────────────────────────────────────────────

class ThesisMonitorDaemon:
    """
    Background thread that polls prices and sends alerts.

    Wired into app.py — needs:
      - get_spot_fn(ticker) -> float
      - post_fn(text) -> None (Telegram poster)
    """

    def __init__(self, engine: ThesisMonitorEngine,
                 get_spot_fn: Callable, post_fn: Callable):
        self.engine = engine
        self.get_spot = get_spot_fn
        self.post_fn = post_fn
        self._enabled = True
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            log.info("Thesis monitor daemon already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="thesis-monitor")
        self._thread.start()
        log.info("Thesis monitor daemon started")

    def stop(self):
        self._stop_event.set()
        log.info("Thesis monitor daemon stop requested")

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        log.info(f"Thesis monitor polling loop started (interval={MONITOR_POLL_INTERVAL_SEC}s)")
        while not self._stop_event.is_set():
            try:
                self._poll_cycle()
            except Exception as e:
                log.error(f"Thesis monitor poll error: {e}", exc_info=True)
            self._stop_event.wait(MONITOR_POLL_INTERVAL_SEC)
        log.info("Thesis monitor polling loop stopped")

    def _poll_cycle(self):
        """One polling cycle — check all monitored tickers."""
        if not self._enabled:
            return

        # Only poll during market hours
        tp = _get_time_phase_ct()
        if tp["phase"] in ("PRE_MARKET", "AFTER_HOURS"):
            return

        for ticker in MONITOR_ENABLED_TICKERS:
            thesis = self.engine.get_thesis(ticker)
            if not thesis:
                continue  # No thesis loaded for this ticker

            try:
                price = self.get_spot(ticker)
                if not price or price <= 0:
                    log.warning(f"Thesis monitor: bad price for {ticker}: {price}")
                    continue

                # Evaluate price against thesis
                events = self.engine.evaluate(ticker, price)

                # Post any events to Telegram
                for event in events:
                    priority = event.get("priority", 1)
                    if priority >= 3:
                        # High-priority events get their own message
                        self._post_alert(ticker, price, event)
                    else:
                        log.info(f"Thesis monitor [{ticker}]: {event.get('msg', '')}")

            except Exception as e:
                log.warning(f"Thesis monitor price check failed for {ticker}: {e}")

    def _post_alert(self, ticker: str, price: float, event: dict):
        """Format and send an alert to Telegram."""
        tp = _get_time_phase_ct()
        state = self.engine.get_state(ticker)
        thesis = self.engine.get_thesis(ticker)

        lines = [
            f"📡 {ticker} THESIS ALERT — ${price:.2f}",
            f"",
            event["msg"],
        ]

        # Add momentum context if relevant
        if state and state.momentum not in ("NEUTRAL", "STALLING"):
            lines.append(f"Momentum: {state.momentum.replace('_', ' ')}")

        # Add nearest intraday levels for context
        if state:
            intraday = IntradayLevelTracker.get_active_levels(state, price)
            if intraday["support"]:
                lines.append(f"Nearest intraday support: ${intraday['support'].price:.2f}")
            if intraday["resistance"]:
                lines.append(f"Nearest intraday resistance: ${intraday['resistance'].price:.2f}")

        lines.append(f"")
        lines.append(f"Phase: {tp['label']} | Thesis: {thesis.bias} ({thesis.bias_score:+d}/14)")
        lines.append(f"— Not financial advice —")

        try:
            self.post_fn("\n".join(lines))
            log.info(f"Thesis alert posted: {ticker} | {event.get('type', '?')} | {event.get('msg', '')[:80]}")
        except Exception as e:
            log.error(f"Thesis alert post failed: {e}")


# ─────────────────────────────────────────────────────────
# THESIS BUILDER (from EM card data)
# ─────────────────────────────────────────────────────────

def build_thesis_from_em_card(
    ticker: str,
    spot: float,
    bias: dict,
    eng: dict,
    em: dict,
    walls: dict,
    cagf: dict = None,
    vix: dict = None,
    v4_result: dict = None,
    session_label: str = "",
    local_walls: dict = None,
    prior_day_close: float = None,
) -> ThesisContext:
    """
    Convert EM card data into a ThesisContext for monitoring.

    Called by app.py after _post_em_card generates its data.
    """
    eng = eng or {}
    walls = walls or {}
    em = em or {}
    local_w = local_walls or walls or {}
    cagf = cagf or {}
    vix = vix or {}

    # Build levels
    levels = ThesisLevels(
        gamma_flip=eng.get("flip_price"),
        local_resistance=local_w.get("local_resistance_1") or walls.get("call_wall"),
        local_support=local_w.get("local_support_1") or walls.get("put_wall"),
        call_wall=walls.get("call_wall"),
        put_wall=walls.get("put_wall"),
        gamma_wall=walls.get("gamma_wall"),
        pin_zone_low=local_w.get("pin_zone_low"),
        pin_zone_high=local_w.get("pin_zone_high"),
        micro_trigger_up=local_w.get("local_resistance_1") or walls.get("call_wall"),
        micro_trigger_down=local_w.get("local_support_1") or walls.get("put_wall"),
        range_break_up=local_w.get("pin_zone_high") or walls.get("call_wall"),
        range_break_down=local_w.get("pin_zone_low") or walls.get("put_wall"),
        pivot=local_w.get("pivot"),
        r1=local_w.get("r1"),
        s1=local_w.get("s1"),
        fib_support=local_w.get("fib_support"),
        fib_resistance=local_w.get("fib_resistance"),
        vpoc=local_w.get("vpoc"),
        max_pain=local_w.get("max_pain") or eng.get("max_pain"),
        em_high=em.get("bull_1sd"),
        em_low=em.get("bear_1sd"),
        em_2sd_high=em.get("bull_2sd"),
        em_2sd_low=em.get("bear_2sd"),
    )

    # Determine prior day context
    prior_ctx = "NORMAL"
    if prior_day_close is not None and spot > 0:
        gap_pct = abs(spot - prior_day_close) / prior_day_close * 100
        if gap_pct > 0.5:
            prior_ctx = "GAP_UP" if spot > prior_day_close else "GAP_DOWN"

    gex_val = eng.get("gex", 0)
    gex_sign = "positive" if gex_val >= 0 else "negative"

    # v1.1: Reconcile GEX sign with gamma flip position.
    # Raw GEX can be positive, but if spot is far below the flip,
    # effective dealer positioning is amplifying, not suppressing.
    flip = eng.get("flip_price")
    if flip is not None and spot > 0:
        dist_from_flip_pct = (flip - spot) / spot * 100
        if dist_from_flip_pct > 1.5:
            gex_sign = "negative"
            log.info(f"Thesis GEX sign overridden: raw GEX {gex_val:+.1f}M but spot {dist_from_flip_pct:.1f}% below flip → negative")
        elif dist_from_flip_pct < -1.5:
            gex_sign = "positive"

    # Determine regime
    regime = "UNKNOWN"
    if cagf and cagf.get("regime"):
        regime = cagf["regime"]
    elif eng.get("is_positive_gex") is not None:
        regime = "SUPPRESSING" if eng["is_positive_gex"] else "TRENDING"

    try:
        from zoneinfo import ZoneInfo
        ts = datetime.now(ZoneInfo("America/Chicago")).isoformat()
    except Exception:
        ts = datetime.now(timezone.utc).isoformat()

    return ThesisContext(
        ticker=ticker,
        bias=bias.get("direction", "NEUTRAL"),
        bias_score=bias.get("score", 0),
        gex_sign=gex_sign,
        gex_value=round(gex_val, 2),
        dex_value=round(eng.get("dex", 0), 2),
        vanna_value=round(eng.get("vanna", 0), 2),
        charm_value=round(eng.get("charm", 0), 2),
        regime=regime,
        volatility_regime=v4_result.get("vol_regime", {}).get("label", "NORMAL") if v4_result else "NORMAL",
        vix=vix.get("vix", 20) if isinstance(vix, dict) else 20,
        iv=v4_result.get("iv", 0.20) if v4_result else 0.20,
        prior_day_close=prior_day_close,
        prior_day_context=prior_ctx,
        session_label=session_label,
        levels=levels,
        created_at=ts,
        spot_at_creation=spot,
    )


# ─────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ─────────────────────────────────────────────────────────
# Created once, wired up by app.py at startup.

_monitor_engine = ThesisMonitorEngine()
_monitor_daemon: Optional[ThesisMonitorDaemon] = None


def get_engine() -> ThesisMonitorEngine:
    return _monitor_engine


def get_daemon() -> Optional[ThesisMonitorDaemon]:
    return _monitor_daemon


def init_daemon(get_spot_fn: Callable, post_fn: Callable) -> ThesisMonitorDaemon:
    global _monitor_daemon
    _monitor_daemon = ThesisMonitorDaemon(_monitor_engine, get_spot_fn, post_fn)
    _monitor_daemon.start()
    log.info("Thesis monitor daemon initialized and started")
    return _monitor_daemon
