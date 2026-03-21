# thesis_monitor.py
# v1.5 Thesis Monitor — Merged: v1.4 Live Entry + v1.2 Best Features
# v1.4: reclaim-hold, don't-chase, retest, time-phase candles, momentum validation
# v1.2: trade types, session-low suppression, build_guidance, GEX reconciliation, Redis, any-ticker
import logging, threading, time, json
from datetime import datetime, timezone
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass, field, asdict

log = logging.getLogger(__name__)

MONITOR_POLL_INTERVAL_SEC = 300
MONITOR_POLL_INTERVAL_FAST_SEC = 60
MONITOR_FAST_POLL_TICKERS = ["SPY"]
MONITOR_ALERT_COOLDOWN_SEC = 600
MONITOR_MAX_BREAK_AGE_SEC = 900
MONITOR_MOMENTUM_LOOKBACK = 5
MONITOR_RECLAIM_THRESHOLD_PCT = 0.015
MONITOR_STALL_THRESHOLD_PCT = 0.008
MONITOR_CONFIRM_BUFFER_PCT = 0.08
MONITOR_EXTENSION_LIMIT_PCT = 0.25
MONITOR_MIN_HOLD_POLLS_AFTER_RECLAIM = 1
MONITOR_RETEST_TOLERANCE_PCT = 0.04
MONITOR_DEFAULT_TICKERS = ["SPY", "QQQ"]
INTRADAY_MIN_TOUCHES = 2
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

@dataclass
class BreakAttempt:
    level: float; level_name: str; direction: str; break_price: float; break_time: float
    detected_as_failed: bool = False; detected_as_confirmed: bool = False; candles_since: int = 0
    reclaim_seen: bool = False; reclaim_price: Optional[float] = None
    reclaim_time: float = 0.0; reclaim_holds: int = 0
    retest_armed: bool = False; retest_fired: bool = False

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
    targets: list = field(default_factory=list)  # [float] — next S/R levels
    status: str = "OPEN"  # OPEN / SCALED / TRAILED / CLOSED / INVALIDATED
    entry_time: float = 0.0
    entry_time_str: str = ""
    level_name: str = ""  # what level triggered the entry
    gex_at_entry: str = "positive"
    trade_type_label: str = ""  # "Naked puts", "Call debit spread", etc.
    # tracking
    max_favorable: float = 0.0  # max move in trade direction from entry
    min_favorable: float = 0.0  # worst adverse excursion from entry
    scaled_at_price: Optional[float] = None
    trail_stop: Optional[float] = None
    close_price: Optional[float] = None
    close_reason: str = ""
    close_time: float = 0.0
    exit_alert_history: dict = field(default_factory=dict)  # cooldown tracking

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

class IntradayLevelTracker:
    @staticmethod
    def update(state: MonitorState, price: float, now: float) -> List[dict]:
        events = []; prices = [p["price"] for p in state.price_history]; n = len(prices)
        if n < 2: return events
        tol = price * INTRADAY_ZONE_TOLERANCE_PCT / 100
        current_high, current_low = max(prices), min(prices)
        if state.session_high is None or current_high > state.session_high:
            state.session_high = current_high
            for p in state.price_history:
                if p["price"] == current_high: state.session_high_time = p.get("time_str", ""); break
            IntradayLevelTracker._upsert_level(state, current_high, "resistance", "session_high", now)
        if state.session_low is None or current_low < state.session_low:
            state.session_low = current_low
            for p in state.price_history:
                if p["price"] == current_low: state.session_low_time = p.get("time_str", ""); break
            IntradayLevelTracker._upsert_level(state, current_low, "support", "session_low", now)
        if n < INTRADAY_MIN_PRICES_FOR_LEVELS: return events
        zone_counts: Dict[float, list] = {}
        for i, p in enumerate(prices):
            placed = False
            for rep in zone_counts:
                if abs(p - rep) <= tol: zone_counts[rep].append(i); placed = True; break
            if not placed: zone_counts[p] = [i]
        for rep_price, indices in zone_counts.items():
            if len(indices) < INTRADAY_MIN_TOUCHES: continue
            has_dep = False
            for idx in indices:
                for sp in prices[idx+1:idx+4]:
                    if abs(sp - rep_price) > tol * 2.5: has_dep = True; break
                if has_dep: break
            if not has_dep: continue
            bu = sum(1 for idx in indices if idx+1 < n and prices[idx+1] > rep_price + tol*0.5)
            bd = sum(1 for idx in indices if idx+1 < n and prices[idx+1] < rep_price - tol*0.5)
            if bu >= INTRADAY_MIN_TOUCHES:
                if IntradayLevelTracker._upsert_level(state, rep_price, "support", "rejection_zone", now, touches=len(indices)):
                    events.append({"msg": f"📍 NEW 5m SUPPORT at ${rep_price:.2f} ({len(indices)} touches).", "type": "info", "priority": 3, "alert_key": f"id_sup_{rep_price:.2f}"})
            if bd >= INTRADAY_MIN_TOUCHES:
                if IntradayLevelTracker._upsert_level(state, rep_price, "resistance", "rejection_zone", now, touches=len(indices)):
                    events.append({"msg": f"📍 NEW 5m RESISTANCE at ${rep_price:.2f} ({len(indices)} touches).", "type": "info", "priority": 3, "alert_key": f"id_res_{rep_price:.2f}"})
        if n >= 3:
            for i in range(max(n-12, 1), n):
                move = prices[i] - prices[i-1]; move_pct = abs(move) / prices[i-1] * 100
                if move_pct >= INTRADAY_SHARP_MOVE_THRESHOLD:
                    origin = prices[i-1]
                    kind = "support" if move > 0 else "resistance"
                    verb = "launched from" if move > 0 else "dumped from"
                    if IntradayLevelTracker._upsert_level(state, origin, kind, "sharp_move_origin", now):
                        events.append({"msg": f"📍 SHARP MOVE ORIGIN: price {verb} ${origin:.2f}. Now intraday {kind}.", "type": "info", "priority": 3, "alert_key": f"sharp_{kind[:3]}_{origin:.2f}"})
        if n >= INTRADAY_CONSOLIDATION_CANDLES:
            recent = prices[-INTRADAY_CONSOLIDATION_CANDLES:]
            rng = max(recent) - min(recent); rng_pct = rng / price * 100
            if rng_pct <= INTRADAY_CONSOLIDATION_RANGE_PCT and rng > 0:
                ch, cl = max(recent), min(recent)
                IntradayLevelTracker._upsert_level(state, ch, "resistance", "consolidation_edge", now)
                IntradayLevelTracker._upsert_level(state, cl, "support", "consolidation_edge", now)
                events.append({"msg": f"📍 CONSOLIDATION: ${cl:.2f}-${ch:.2f} ({INTRADAY_CONSOLIDATION_CANDLES*5} min). Break with momentum is actionable.", "type": "info", "priority": 2, "alert_key": f"cons_{cl:.1f}_{ch:.1f}"})
        for lvl in state.intraday_levels:
            if lvl.active and abs(price - lvl.price) > tol * 8 and (now - lvl.last_touched_ts) > 2700: lvl.active = False
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
    def __init__(self, store_get_fn=None, store_set_fn=None):
        self._theses: Dict[str, ThesisContext] = {}; self._states: Dict[str, MonitorState] = {}
        self._lock = threading.Lock(); self._store_get = store_get_fn; self._store_set = store_set_fn

    def store_thesis(self, ticker: str, thesis: ThesisContext):
        with self._lock:
            self._theses[ticker] = thesis
            old = self._states.get(ticker); ns = MonitorState()
            if old and old.failed_moves:
                now = time.monotonic(); ns.failed_moves = [fm for fm in old.failed_moves if (now - fm.break_time) < 1800]
            if old and old.intraday_levels:
                ns.intraday_levels = [l for l in old.intraday_levels if l.active]
                ns.session_high = old.session_high; ns.session_low = old.session_low
                ns.session_high_time = old.session_high_time; ns.session_low_time = old.session_low_time
            self._states[ticker] = ns
            log.info(f"Thesis stored: {ticker} | bias={thesis.bias} score={thesis.bias_score} gex={thesis.gex_sign} regime={thesis.regime}")
            self._persist_thesis(ticker, thesis)

    def _persist_thesis(self, ticker, thesis):
        if not self._store_set: return
        try:
            data = {"ticker": thesis.ticker, "bias": thesis.bias, "bias_score": thesis.bias_score, "gex_sign": thesis.gex_sign, "gex_value": thesis.gex_value, "dex_value": thesis.dex_value, "vanna_value": thesis.vanna_value, "charm_value": thesis.charm_value, "regime": thesis.regime, "volatility_regime": thesis.volatility_regime, "vix": thesis.vix, "iv": thesis.iv, "prior_day_close": thesis.prior_day_close, "prior_day_context": thesis.prior_day_context, "session_label": thesis.session_label, "created_at": thesis.created_at, "spot_at_creation": thesis.spot_at_creation, "levels": asdict(thesis.levels)}
            self._store_set(f"thesis_monitor:{ticker}", json.dumps(data), ttl=86400)
        except Exception as e: log.warning(f"Thesis persist failed for {ticker}: {e}")

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
        for d in MONITOR_DEFAULT_TICKERS:
            if d not in tickers and self.get_thesis(d): tickers.add(d)
        return sorted(tickers)

    def _recent_net_move(self, state, lookback=3):
        recent = [p["price"] for p in state.price_history[-lookback:]]
        return (recent[-1] - recent[0]) if len(recent) >= 2 else 0.0

    def evaluate(self, ticker, price):
        with self._lock:
            thesis = self._theses.get(ticker); state = self._states.get(ticker)
            if not thesis or not state: return []
            events = []; now = time.monotonic()
            prev_price = state.price_history[-1]["price"] if state.price_history else None
            try:
                from zoneinfo import ZoneInfo; ts = datetime.now(ZoneInfo("America/Chicago")).strftime("%I:%M %p")
            except Exception: ts = datetime.utcnow().strftime("%H:%M")
            state.price_history.append({"price": price, "time_str": ts, "ts_mono": now})
            if len(state.price_history) > 240: state.price_history = state.price_history[-240:]
            state.check_count += 1
            events.extend(self._evaluate_momentum(state, price))
            events.extend(IntradayLevelTracker.update(state, price, now))
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
            for ba in state.break_attempts:
                if not ba.detected_as_failed and not ba.detected_as_confirmed and (now - ba.break_time) <= MONITOR_MAX_BREAK_AGE_SEC:
                    ba.candles_since += 1
            if prev_price is not None: events.extend(self._detect_breaks(thesis, state, price, prev_price, now))
            entry_events = []
            entry_events.extend(self._detect_confirmed_breaks(thesis, state, price, now))
            entry_events.extend(self._detect_failed_moves(thesis, state, price, now))
            entry_events.extend(self._detect_retests(thesis, state, price, now))
            # Auto-create ActiveTrade from clean entry signals
            for ev in entry_events:
                if ev.get("type") in ("trade_confirmed", "critical") and ev.get("priority", 0) >= 5:
                    ak = ev.get("alert_key", "")
                    # Only create trade on clean entries (not DON'T CHASE / extended)
                    if "wait" not in ak and "late" not in ak:
                        self._create_trade_from_event(ticker, thesis, state, price, ev, now, ts)
            events.extend(entry_events)
            events.extend(self._check_gamma_flip(thesis, state, price))
            # ── Exit monitoring for active trades ──
            events.extend(self._monitor_exits(ticker, thesis, state, price, now))
            # Expire stale trades
            self._expire_stale_trades(state, now)
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
    def _create_trade_from_event(self, ticker, thesis, state, price, event, now, ts):
        """Create an ActiveTrade from a clean entry signal."""
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
        # Don't double-create — block if open trade at same direction+type or same-level proximity
        for t in state.active_trades:
            if t.status not in ("OPEN", "SCALED", "TRAILED"):
                continue
            if t.direction != direction:
                continue
            # Same direction + same entry type = definite duplicate
            if t.entry_type == entry_type:
                log.info(f"Trade already open for {ticker} {direction} {entry_type}, skipping")
                return
            # Same direction + near same price = likely duplicate from different signal type
            if abs(t.entry_price - price) / price < 0.005:
                log.info(f"Trade already open for {ticker} {direction} near ${price:.2f}, skipping")
                return
        # Find the stop level — extract from the break attempt
        stop = 0.0
        level_name = ""
        for ba in state.break_attempts + state.failed_moves + state.confirmed_breaks:
            bak = ak.split("_")[-1]  # e.g. "656.30"
            try:
                ba_price_str = f"{ba.level:.2f}"
                if ba_price_str == bak or ba_price_str in ak:
                    stop = ba.level
                    level_name = ba.level_name
                    break
            except Exception:
                continue
        if stop == 0.0:
            # Fallback: use nearest level in opposite direction
            if direction == "LONG":
                stop = price * 0.995  # 0.5% below as safety
            else:
                stop = price * 1.005
        # Parse trade type label from message
        tt_label = ""
        for line in msg.split("\n"):
            if "TRADE TYPE:" in line or line.strip().startswith("💰"):
                tt_label = line.strip().replace("💰 TRADE TYPE: ", "").replace("💰 ", "").strip()
                break
        targets = self._find_targets(ticker, thesis, state, price, direction)
        trade = ActiveTrade(
            ticker=ticker, direction=direction, entry_type=entry_type,
            entry_price=price, stop_level=stop, targets=targets,
            status="OPEN", entry_time=now, entry_time_str=ts,
            level_name=level_name, gex_at_entry=thesis.gex_sign,
            trade_type_label=tt_label, max_favorable=0.0, min_favorable=0.0,
        )
        state.active_trades.append(trade)
        self._persist_trades(ticker, state)
        log.info(f"ActiveTrade created: {ticker} {direction} {entry_type} @ ${price:.2f} | stop=${stop:.2f} | targets={[f'${t:.2f}' for t in targets]}")

    # ── Exit Monitor: Core Loop ──
    def _monitor_exits(self, ticker, thesis, state, price, now):
        """Check all active trades for exit/scale/trail/invalidation signals."""
        events = []
        for trade in state.active_trades:
            if trade.status in ("CLOSED", "INVALIDATED"):
                continue
            # Update max favorable excursion
            if trade.direction == "LONG":
                fav = price - trade.entry_price
                trade.max_favorable = max(trade.max_favorable, fav)
                trade.min_favorable = min(trade.min_favorable, fav)
            else:
                fav = trade.entry_price - price
                trade.max_favorable = max(trade.max_favorable, fav)
                trade.min_favorable = min(trade.min_favorable, fav)
            age = now - trade.entry_time
            # 1. INVALIDATION — stop level reclaimed/lost
            inv_events = self._check_invalidation(trade, thesis, state, price, now)
            if inv_events:
                events.extend(inv_events)
                continue  # trade closed, skip other checks
            # 2. SCALE PARTIAL — first target hit
            events.extend(self._check_scale(trade, thesis, state, price, now))
            # 3. TRAIL RUNNER — momentum accelerating in position direction
            events.extend(self._check_trail(trade, thesis, state, price, now))
            # 4. REDUCE EXPOSURE — momentum fading against position
            events.extend(self._check_momentum_fade(trade, thesis, state, price, now))
            # 5. EXIT — second target or exhaustion
            events.extend(self._check_exit(trade, thesis, state, price, now))
        return events

    def _exit_alert_ok(self, trade, key, now):
        """Check and set cooldown for exit alerts."""
        last = trade.exit_alert_history.get(key)
        if last is not None and (now - last) < EXIT_ALERT_COOLDOWN_SEC:
            return False
        trade.exit_alert_history[key] = now
        return True

    # ── Invalidation Check ──
    def _check_invalidation(self, trade, thesis, state, price, now):
        events = []
        buf = trade.stop_level * (EXIT_INVALIDATE_BUFFER_PCT / 100)
        if trade.direction == "LONG":
            # Price dropped below stop
            invalidated = price < (trade.stop_level - buf)
        else:
            # Price rallied above stop
            invalidated = price > (trade.stop_level + buf)
        # Also check trail stop if set
        if trade.trail_stop is not None:
            if trade.direction == "LONG" and price < trade.trail_stop:
                invalidated = True
            elif trade.direction == "SHORT" and price > trade.trail_stop:
                invalidated = True
        if not invalidated:
            return events
        if not self._exit_alert_ok(trade, "invalidate", now):
            return events
        trade.status = "INVALIDATED"; trade.close_price = price; trade.close_time = now
        stop_ref = trade.trail_stop if trade.trail_stop else trade.stop_level
        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
        if trade.direction == "LONG":
            trade.close_reason = f"Reclaimed ${stop_ref:.2f}"
            events.append({"msg": (
                f"🛑 TRADE INVALIDATED — LONG\n\n"
                f"Lost ${stop_ref:.2f}. Exit now.\n"
                f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                f"Stop was ${stop_ref:.2f} ({trade.level_name})\n"
                f"Don't hope. Close it."
            ), "type": "exit", "priority": 5, "alert_key": f"exit_inv_L_{trade.entry_price:.2f}"})
        else:
            trade.close_reason = f"Lost ${stop_ref:.2f}"
            events.append({"msg": (
                f"🛑 TRADE INVALIDATED — SHORT\n\n"
                f"Reclaimed ${stop_ref:.2f}. Exit now.\n"
                f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                f"Stop was ${stop_ref:.2f} ({trade.level_name})\n"
                f"Don't hope. Close it."
            ), "type": "exit", "priority": 5, "alert_key": f"exit_inv_S_{trade.entry_price:.2f}"})
        self._persist_trades(trade.ticker, state)
        log.info(f"Trade INVALIDATED: {trade.ticker} {trade.direction} @ ${trade.entry_price:.2f} → ${price:.2f}")
        return events

    # ── Scale Partial Check ──
    def _check_scale(self, trade, thesis, state, price, now):
        events = []
        if trade.status != "OPEN" or not trade.targets:
            return events
        first_target = trade.targets[0]
        prox_pct = abs(price - first_target) / first_target * 100
        if prox_pct > EXIT_SCALE_PROXIMITY_PCT:
            return events
        # Target reached
        if trade.direction == "LONG" and price < first_target:
            return events  # hasn't actually reached it yet
        if trade.direction == "SHORT" and price > first_target:
            return events
        if not self._exit_alert_ok(trade, "scale", now):
            return events
        trade.status = "SCALED"; trade.scaled_at_price = price
        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
        dir_word = "long" if trade.direction == "LONG" else "short"
        remaining = trade.targets[1:] if len(trade.targets) > 1 else []
        next_tgt = f"${remaining[0]:.2f}" if remaining else "—"
        events.append({"msg": (
            f"💰 SCALE PARTIAL — {trade.direction}\n\n"
            f"First target ${first_target:.2f} reached. Take partials.\n"
            f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
            f"Move stop to breakeven (${trade.entry_price:.2f}).\n"
            f"Next target: {next_tgt}\n"
            f"Don't let winner turn into loser."
        ), "type": "exit", "priority": 5, "alert_key": f"exit_scale_{dir_word}_{trade.entry_price:.2f}"})
        # Move stop to breakeven after scaling
        trade.trail_stop = trade.entry_price
        self._persist_trades(trade.ticker, state)
        log.info(f"Trade SCALED: {trade.ticker} {trade.direction} @ ${trade.entry_price:.2f}, target ${first_target:.2f} hit, stop→BE")
        return events

    # ── Trail Runner Check ──
    def _check_trail(self, trade, thesis, state, price, now):
        events = []
        if trade.status not in ("SCALED", "TRAILED"):
            return events
        # Check momentum acceleration in trade direction
        recent = [p["price"] for p in state.price_history[-EXIT_MOMENTUM_FADE_LOOKBACK:]]
        if len(recent) < 3:
            return events
        diffs = [recent[i] - recent[i-1] for i in range(1, len(recent))]
        avg_d = sum(diffs) / len(diffs)
        last_d = diffs[-1]
        # Is momentum accelerating in our direction?
        accel = False
        if trade.direction == "LONG" and avg_d > 0 and last_d > avg_d * EXIT_TRAIL_ACCELERATION_MULT:
            accel = True
        elif trade.direction == "SHORT" and avg_d < 0 and abs(last_d) > abs(avg_d) * EXIT_TRAIL_ACCELERATION_MULT:
            accel = True
        if not accel:
            return events
        if not self._exit_alert_ok(trade, "trail", now):
            return events
        # Compute new trail stop — halfway between entry and current price
        old_stop = trade.trail_stop if trade.trail_stop else trade.stop_level
        if trade.direction == "LONG":
            new_stop = max(old_stop, price - (price - trade.entry_price) * 0.5)
            new_stop = max(new_stop, trade.entry_price)  # never below breakeven
        else:
            new_stop = min(old_stop, price + (trade.entry_price - price) * 0.5)
            new_stop = min(new_stop, trade.entry_price)  # never above breakeven
        if abs(new_stop - old_stop) / old_stop < 0.001:
            return events  # trail didn't move meaningfully
        trade.trail_stop = round(new_stop, 2)
        trade.status = "TRAILED"
        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
        events.append({"msg": (
            f"🏃 LET RUNNER WORK — {trade.direction}\n\n"
            f"Trend accelerating. Trail stop → ${trade.trail_stop:.2f}\n"
            f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
            f"Let momentum do the work."
        ), "type": "exit", "priority": 5, "alert_key": f"exit_trail_{trade.direction}_{trade.entry_price:.2f}"})
        self._persist_trades(trade.ticker, state)
        log.info(f"Trade TRAILED: {trade.ticker} {trade.direction}, trail_stop → ${trade.trail_stop:.2f}")
        return events

    # ── Momentum Fade Check ──
    def _check_momentum_fade(self, trade, thesis, state, price, now):
        events = []
        if trade.status not in ("OPEN", "SCALED", "TRAILED"):
            return events
        recent = [p["price"] for p in state.price_history[-EXIT_MOMENTUM_FADE_LOOKBACK:]]
        if len(recent) < 3:
            return events
        diffs = [recent[i] - recent[i-1] for i in range(1, len(recent))]
        avg_d = sum(diffs) / len(diffs)
        # Is momentum fading AGAINST our position?
        fading = False
        if trade.direction == "LONG" and avg_d < 0 and trade.max_favorable > 0:
            fading = True
        elif trade.direction == "SHORT" and avg_d > 0 and trade.max_favorable > 0:
            fading = True
        if not fading:
            return events
        # Only alert if we've had a meaningful move first (at least 0.15% in our favor)
        fav_pct = trade.max_favorable / trade.entry_price * 100
        if fav_pct < 0.15:
            return events
        if not self._exit_alert_ok(trade, "fade", now):
            return events
        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
        dir_word = "Upside" if trade.direction == "LONG" else "Downside"
        events.append({"msg": (
            f"⚠️ REDUCE EXPOSURE — {trade.direction}\n\n"
            f"{dir_word} stalling. Momentum fading against position.\n"
            f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
            f"Best: {'+' if trade.direction == 'LONG' else ''}{fav_pct:.2f}% | Now giving back.\n"
            f"Don't let winner turn into loser."
        ), "type": "exit", "priority": 4, "alert_key": f"exit_fade_{trade.direction}_{trade.entry_price:.2f}"})
        return events

    # ── Exit / Exhaustion Check ──
    def _check_exit(self, trade, thesis, state, price, now):
        events = []
        if trade.status not in ("SCALED", "TRAILED"):
            return events  # only exit after scaling or trailing
        # Check if second (or later) target reached — targets[0] was the scale target
        remaining = trade.targets[1:]
        second_hit = False
        for tgt in remaining:
            if trade.direction == "LONG" and price >= tgt:
                second_hit = True; break
            elif trade.direction == "SHORT" and price <= tgt:
                second_hit = True; break
        # Check for exhaustion — momentum decelerating after a run
        exhausted = False
        recent = [p["price"] for p in state.price_history[-EXIT_MOMENTUM_FADE_LOOKBACK:]]
        if len(recent) >= 3:
            diffs = [recent[i] - recent[i-1] for i in range(1, len(recent))]
            avg_d = sum(diffs) / len(diffs)
            last_d = diffs[-1]
            if trade.direction == "LONG" and trade.max_favorable > trade.entry_price * 0.003:
                if avg_d > 0 and abs(last_d) < abs(avg_d) * EXIT_EXHAUSTION_DECEL_MULT:
                    exhausted = True
                elif last_d < 0:
                    exhausted = True
            elif trade.direction == "SHORT" and trade.max_favorable > trade.entry_price * 0.003:
                if avg_d < 0 and abs(last_d) < abs(avg_d) * EXIT_EXHAUSTION_DECEL_MULT:
                    exhausted = True
                elif last_d > 0:
                    exhausted = True
        if not second_hit and not exhausted:
            return events
        if not self._exit_alert_ok(trade, "exit", now):
            return events
        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
        if second_hit:
            reason = "Second target reached"
            trade.close_reason = reason
            events.append({"msg": (
                f"⏹ EXIT — {trade.direction}\n\n"
                f"Second target hit. Pay yourself.\n"
                f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                f"Move extended. Close remaining."
            ), "type": "exit", "priority": 5, "alert_key": f"exit_done_{trade.direction}_{trade.entry_price:.2f}"})
        else:
            reason = "Exhaustion signal"
            trade.close_reason = reason
            events.append({"msg": (
                f"⏹ EXIT — {trade.direction}\n\n"
                f"Momentum exhausting after {trade.status.lower()} phase.\n"
                f"Entry: ${trade.entry_price:.2f} → Now: ${price:.2f} ({pnl_pct:+.2f}%)\n"
                f"Move extended. Pay yourself."
            ), "type": "exit", "priority": 5, "alert_key": f"exit_exh_{trade.direction}_{trade.entry_price:.2f}"})
        trade.status = "CLOSED"; trade.close_price = price; trade.close_time = now
        self._persist_trades(trade.ticker, state)
        log.info(f"Trade CLOSED: {trade.ticker} {trade.direction} @ ${trade.entry_price:.2f} → ${price:.2f} | {reason}")
        return events

    # ── Expire Stale Trades ──
    def _expire_stale_trades(self, state, now):
        for trade in state.active_trades:
            if trade.status in ("CLOSED", "INVALIDATED"):
                continue
            if (now - trade.entry_time) > EXIT_MAX_TRADE_AGE_SEC:
                trade.status = "CLOSED"; trade.close_time = now
                trade.close_reason = "Session expired (4hr max)"
                log.info(f"Trade expired: {trade.ticker} {trade.direction} @ ${trade.entry_price:.2f}")
        # Prune trades older than 8 hours (keep history but don't bloat)
        state.active_trades = [t for t in state.active_trades if (now - t.entry_time) < 28800]

    # ── Trade Persistence ──
    def _persist_trades(self, ticker, state):
        if not self._store_set:
            return
        try:
            trades_data = []
            for t in state.active_trades:
                trades_data.append({
                    "ticker": t.ticker, "direction": t.direction, "entry_type": t.entry_type,
                    "entry_price": t.entry_price, "stop_level": t.stop_level,
                    "targets": t.targets, "status": t.status,
                    "entry_time": t.entry_time, "entry_time_str": t.entry_time_str,
                    "level_name": t.level_name, "gex_at_entry": t.gex_at_entry,
                    "trade_type_label": t.trade_type_label,
                    "max_favorable": t.max_favorable, "min_favorable": t.min_favorable,
                    "scaled_at_price": t.scaled_at_price,
                    "trail_stop": t.trail_stop, "close_price": t.close_price,
                    "close_reason": t.close_reason, "close_time": t.close_time,
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
            trades_data = json.loads(raw)
            for td in trades_data:
                trade = ActiveTrade(
                    ticker=td.get("ticker", ticker),
                    direction=td.get("direction", "LONG"),
                    entry_type=td.get("entry_type", "BREAK"),
                    entry_price=td.get("entry_price", 0),
                    stop_level=td.get("stop_level", 0),
                    targets=td.get("targets", []),
                    status=td.get("status", "OPEN"),
                    entry_time=td.get("entry_time", 0),
                    entry_time_str=td.get("entry_time_str", ""),
                    level_name=td.get("level_name", ""),
                    gex_at_entry=td.get("gex_at_entry", "positive"),
                    trade_type_label=td.get("trade_type_label", ""),
                    max_favorable=td.get("max_favorable", 0),
                    min_favorable=td.get("min_favorable", 0),
                    scaled_at_price=td.get("scaled_at_price"),
                    trail_stop=td.get("trail_stop"),
                    close_price=td.get("close_price"),
                    close_reason=td.get("close_reason", ""),
                    close_time=td.get("close_time", 0),
                )
                state.active_trades.append(trade)
            log.info(f"Loaded {len(trades_data)} trades from store for {ticker}")
        except Exception as e:
            log.warning(f"Trade load failed for {ticker}: {e}")

    # ── Manual Trade Management ──
    def close_trade(self, ticker, price=None, reason="Manual close"):
        """Close the most recent open trade for a ticker."""
        with self._lock:
            state = self._states.get(ticker)
            if not state:
                return "No state for this ticker."
            open_trades = [t for t in state.active_trades if t.status in ("OPEN", "SCALED", "TRAILED")]
            if not open_trades:
                return "No open trades."
            trade = open_trades[-1]
            trade.status = "CLOSED"; trade.close_reason = reason
            trade.close_price = price; trade.close_time = time.monotonic()
            self._persist_trades(ticker, state)
            pnl = ""
            if price and trade.entry_price:
                pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100) if trade.direction == "LONG" else ((trade.entry_price - price) / trade.entry_price * 100)
                pnl = f" ({pnl_pct:+.2f}%)"
            return f"Closed {trade.direction} {trade.entry_type} @ ${trade.entry_price:.2f}{pnl}. Reason: {reason}"

    # ── Format Active Trades for Display ──
    def format_trades(self, ticker, price=None):
        state = self._states.get(ticker)
        if not state:
            return f"📊 {ticker}: No monitor state."
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
            lines.append(f"\n{status_emoji} {t.direction} {t.entry_type} — {t.status}")
            lines.append(f"  Entry: ${t.entry_price:.2f} ({t.entry_time_str})")
            lines.append(f"  Stop: ${stop_ref:.2f}{pnl}")
            if t.targets:
                tgt_str = " → ".join([f"${tg:.2f}" for tg in t.targets[:3]])
                lines.append(f"  Targets: {tgt_str}")
            if t.trade_type_label:
                lines.append(f"  Type: {t.trade_type_label}")
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
        thesis = self._theses.get(ticker); state = self._states.get(ticker)
        if not thesis or not state: return [{"text": f"No thesis for {ticker}. Run /em first.", "type": "neutral"}]
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

    def _evaluate_momentum(self, state, price):
        events = []; recent = [p["price"] for p in state.price_history[-MONITOR_MOMENTUM_LOOKBACK:]]
        if len(recent) < 3: return events
        diffs = [recent[i] - recent[i-1] for i in range(1, len(recent))]
        avg_d = sum(diffs)/len(diffs); last_d = diffs[-1]; old = state.momentum
        thr = recent[-1] * (MONITOR_STALL_THRESHOLD_PCT / 100)
        if abs(avg_d) < thr: state.momentum = "STALLING"
        elif avg_d > 0 and last_d > 0: state.momentum = "ACCELERATING_UP" if last_d > avg_d * 1.5 else "DRIFTING_UP"
        elif avg_d < 0 and last_d < 0: state.momentum = "ACCELERATING_DOWN" if abs(last_d) > abs(avg_d) * 1.5 else "DRIFTING_DOWN"
        elif avg_d > 0 and last_d <= 0: state.momentum = "LOSING_UPSIDE_MOMENTUM"
        elif avg_d < 0 and last_d >= 0: state.momentum = "LOSING_DOWNSIDE_MOMENTUM"
        if state.momentum != old:
            if state.momentum == "LOSING_UPSIDE_MOMENTUM" and old in ("ACCELERATING_UP", "DRIFTING_UP"):
                events.append({"msg": "⚠️ Upside momentum fading. Tighten if long.", "type": "warning", "priority": 4, "alert_key": "mom_fade_up"})
            elif state.momentum == "LOSING_DOWNSIDE_MOMENTUM" and old in ("ACCELERATING_DOWN", "DRIFTING_DOWN"):
                events.append({"msg": "⚠️ Downside momentum fading. Tighten if short.", "type": "warning", "priority": 4, "alert_key": "mom_fade_dn"})
        return events

    def _detect_breaks(self, thesis, state, price, prev_price, now):
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
            # Suppress fresh session extremes — a level just set at current edge
            # is not meaningful S/R yet. Require it to have held for 3+ polls.
            if il.source in ("session_low", "session_high"):
                age_polls = sum(1 for p in state.price_history if p.get("ts_mono", 0) >= il.first_seen_ts)
                if age_polls < 3: continue
            # Suppress session extremes during consistent directional momentum
            if il.source == "session_low" and state.momentum in ("ACCELERATING_DOWN", "DRIFTING_DOWN"): continue
            if il.source == "session_high" and state.momentum in ("ACCELERATING_UP", "DRIFTING_UP"): continue
            wl.append((il.price, f"intraday_{il.kind} ({il.source.replace('_',' ')})", il.kind == "support"))
        for level, name, is_sup in wl:
            if is_sup and prev_price >= level and price < level:
                # Skip if a pending break already exists at this level+direction
                if any(abs(ba.level - level) <= tol and ba.direction == "DOWN" and not ba.detected_as_failed and not ba.detected_as_confirmed for ba in state.break_attempts):
                    continue
                state.break_attempts.append(BreakAttempt(level=level, level_name=name, direction="DOWN", break_price=price, break_time=now))
                state.status = "BREAK_IN_PROGRESS"
                events.append({"msg": f"🔻 BREAK ATTEMPT: below ${level:.2f} ({name}). Watching follow-through or reclaim.", "type": "alert", "priority": 4, "alert_key": f"brk_dn_{name}_{level:.2f}"})
            elif not is_sup and prev_price <= level and price > level:
                if any(abs(ba.level - level) <= tol and ba.direction == "UP" and not ba.detected_as_failed and not ba.detected_as_confirmed for ba in state.break_attempts):
                    continue
                state.break_attempts.append(BreakAttempt(level=level, level_name=name, direction="UP", break_price=price, break_time=now))
                state.status = "BREAK_IN_PROGRESS"
                events.append({"msg": f"🔺 BREAK ATTEMPT: above ${level:.2f} ({name}). Watching follow-through or failure.", "type": "alert", "priority": 4, "alert_key": f"brk_up_{name}_{level:.2f}"})
        return events

    def _detect_confirmed_breaks(self, thesis, state, price, now):
        events = []; tp = _get_time_phase_ct()
        for ba in state.break_attempts:
            if ba.detected_as_failed or ba.detected_as_confirmed: continue
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC: continue
            req = 2 if tp["phase"] in ("OPEN", "MORNING", "AFTERNOON") else 3
            if ba.candles_since < req: continue
            buf = ba.level * (MONITOR_CONFIRM_BUFFER_PCT / 100); net = self._recent_net_move(state, 3)
            if ba.direction == "DOWN":
                ok = price < (ba.level - buf) and net < -(ba.level * 0.0012)
            else:
                ok = price > (ba.level + buf) and net > (ba.level * 0.0012)
            if not ok: continue
            ba.detected_as_confirmed = True; ba.retest_armed = True
            state.confirmed_breaks.append(ba); state.status = "BREAK_CONFIRMED"
            ext = abs(price - ba.level) / ba.level * 100; chase = ext > MONITOR_EXTENSION_LIMIT_PCT
            if ba.direction == "DOWN":
                state.active_trend_direction = "SHORT"
                if thesis.gex_sign == "negative":
                    tt = "💰 TRADE TYPE: Naked puts — GEX- trend can run."
                    if chase:
                        events.append({"msg": f"🟥 BREAKDOWN CONFIRMED — EXTENDED\n\n${ba.level:.2f} ({ba.level_name}) broke. Price {ext:.2f}% past — DON'T CHASE.\nWait for retest of ${ba.level:.2f}.\nSTOP: above ${ba.level:.2f}\n{tt}", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_short_wait_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🟥🟥🟥 BREAKDOWN CONFIRMED — PUTS / SHORT 🟥🟥🟥\n\n${ba.level:.2f} ({ba.level_name}) broke with follow-through.\nGEX NEGATIVE — dealers amplify.\n\n{tt}\nENTRY: Buy puts near the money\nSTOP: Reclaim above ${ba.level:.2f}\nTARGET: Next support — let trend work", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_short_{ba.level:.2f}"})
                else:
                    events.append({"msg": f"⚠️ BREAKDOWN + FOLLOW-THROUGH at ${ba.level:.2f}\nGEX+ — mean reversion possible. Reclaim = squeeze long.", "type": "warning", "priority": 4, "alert_key": f"ft_dn_{ba.level:.2f}"})
            else:
                state.active_trend_direction = "LONG"
                if thesis.gex_sign == "negative":
                    tt = "💰 TRADE TYPE: Naked calls — GEX- trend can run."
                    if chase:
                        events.append({"msg": f"🟩 BREAKOUT CONFIRMED — EXTENDED\n\n${ba.level:.2f} ({ba.level_name}) broke. Price {ext:.2f}% past — DON'T CHASE.\nWait for retest of ${ba.level:.2f}.\nSTOP: below ${ba.level:.2f}\n{tt}", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_long_wait_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🟩🟩🟩 BREAKOUT CONFIRMED — CALLS / LONG 🟩🟩🟩\n\n${ba.level:.2f} ({ba.level_name}) broke with follow-through.\nGEX NEGATIVE — dealers amplify.\n\n{tt}\nENTRY: Buy calls near the money\nSTOP: Lose ${ba.level:.2f}\nTARGET: Next resistance — let trend work", "type": "trade_confirmed", "priority": 5, "alert_key": f"conf_long_{ba.level:.2f}"})
                else:
                    events.append({"msg": f"⚠️ BREAKOUT + FOLLOW-THROUGH at ${ba.level:.2f}\nGEX+ — mean reversion possible. Lose level = fade short.", "type": "warning", "priority": 4, "alert_key": f"ft_up_{ba.level:.2f}"})
        return events

    def _detect_failed_moves(self, thesis, state, price, now):
        events = []; rb = price * (MONITOR_RECLAIM_THRESHOLD_PCT / 100)
        for ba in state.break_attempts:
            if ba.detected_as_failed or ba.detected_as_confirmed: continue
            if (now - ba.break_time) > MONITOR_MAX_BREAK_AGE_SEC or ba.candles_since < 2: continue
            reclaimed = (ba.direction == "DOWN" and price > ba.level + rb) or (ba.direction == "UP" and price < ba.level - rb)
            if reclaimed:
                if not ba.reclaim_seen:
                    ba.reclaim_seen = True; ba.reclaim_price = price; ba.reclaim_time = now; ba.reclaim_holds = 0; continue
                ba.reclaim_holds += 1
                if ba.reclaim_holds < MONITOR_MIN_HOLD_POLLS_AFTER_RECLAIM: continue
                ba.detected_as_failed = True; ba.retest_armed = True
                state.failed_moves.append(ba); state.status = "FAILED_MOVE_ACTIVE"
                ext = abs(price - ba.level) / ba.level * 100; late = ext > MONITOR_EXTENSION_LIMIT_PCT
                if ba.direction == "DOWN":
                    rn = ("GEX+ — squeeze probability HIGH." if thesis.gex_sign == "positive" else "GEX- squeeze can run hard.")
                    tt = ("\n💰 TRADE TYPE: Call debit spread — GEX+ reversal capped." if thesis.gex_sign == "positive" else "\n💰 TRADE TYPE: Naked calls — GEX- squeeze can run.")
                    if late:
                        events.append({"msg": f"🔥 FAILED BREAKDOWN at ${ba.level:.2f}\n\nReclaimed + held. Shorts trapped.\n⚠️ Extended {ext:.2f}% — DON'T CHASE.\nWait for retest.\nSTOP: below level\n{rn}{tt}", "type": "critical", "priority": 5, "alert_key": f"fb_late_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🔥 FAILED BREAKDOWN — SQUEEZE LONG\n\n${ba.level:.2f} held after reclaim. Shorts trapped.\n\nENTRY: Now\nSTOP: Below ${ba.level:.2f}\n{rn}{tt}", "type": "critical", "priority": 5, "alert_key": f"fb_now_{ba.level:.2f}"})
                else:
                    rn = ("GEX+ — fade probability HIGH." if thesis.gex_sign == "positive" else "GEX- downside can accelerate.")
                    tt = ("\n💰 TRADE TYPE: Put debit spread — GEX+ reversal capped." if thesis.gex_sign == "positive" else "\n💰 TRADE TYPE: Naked puts — GEX- dump can run.")
                    if late:
                        events.append({"msg": f"🔥 FAILED BREAKOUT at ${ba.level:.2f}\n\nLost + held. Longs trapped.\n⚠️ Extended {ext:.2f}% — DON'T CHASE.\nWait for retest.\nSTOP: above level\n{rn}{tt}", "type": "critical", "priority": 5, "alert_key": f"fbo_late_{ba.level:.2f}"})
                    else:
                        events.append({"msg": f"🔥 FAILED BREAKOUT — FADE SHORT\n\n${ba.level:.2f} lost and held. Longs trapped.\n\nENTRY: Now\nSTOP: Above ${ba.level:.2f}\n{rn}{tt}", "type": "critical", "priority": 5, "alert_key": f"fbo_now_{ba.level:.2f}"})
            else:
                if ba.reclaim_seen and not ba.detected_as_failed:
                    ba.reclaim_seen = False; ba.reclaim_price = None; ba.reclaim_time = 0.0; ba.reclaim_holds = 0
        return events

    def _detect_retests(self, thesis, state, price, now):
        events = []; net = self._recent_net_move(state, 3)
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
                events.append({"msg": f"🎯 RETEST SHORT ENTRY\n\nBreakdown retested ${ba.level:.2f} and rejecting.\n\nENTRY: short / puts\nSTOP: above ${ba.level:.2f}\n{tt}", "type": "critical", "priority": 5, "alert_key": f"rt_short_{ba.level:.2f}"})
            elif ba.detected_as_confirmed and ba.direction == "UP" and net > (ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Naked calls" if thesis.gex_sign == "negative" else "💰 Call debit spread"
                events.append({"msg": f"🎯 RETEST LONG ENTRY\n\nBreakout retested ${ba.level:.2f} and holding.\n\nENTRY: long / calls\nSTOP: below ${ba.level:.2f}\n{tt}", "type": "critical", "priority": 5, "alert_key": f"rt_long_{ba.level:.2f}"})
            elif ba.detected_as_failed and ba.direction == "DOWN" and net > (ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Call debit spread" if thesis.gex_sign == "positive" else "💰 Naked calls"
                events.append({"msg": f"🎯 RETEST LONG (squeeze)\n\nFailed breakdown retested ${ba.level:.2f} and holding.\n\nENTRY: long / calls\nSTOP: below ${ba.level:.2f}\n{tt}", "type": "critical", "priority": 5, "alert_key": f"rt_fl_{ba.level:.2f}"})
            elif ba.detected_as_failed and ba.direction == "UP" and net < -(ba.level * 0.0008):
                ba.retest_fired = True; tt = "💰 Put debit spread" if thesis.gex_sign == "positive" else "💰 Naked puts"
                events.append({"msg": f"🎯 RETEST SHORT (fade)\n\nFailed breakout retested ${ba.level:.2f} and rejecting.\n\nENTRY: short / puts\nSTOP: above ${ba.level:.2f}\n{tt}", "type": "critical", "priority": 5, "alert_key": f"rt_fs_{ba.level:.2f}"})
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
        out = []
        for e in events:
            k = e.get("alert_key", e.get("msg", "")[:40]); last = state.alert_history.get(k)
            if last is None or (now - last) >= MONITOR_ALERT_COOLDOWN_SEC: state.alert_history[k] = now; out.append(e)
        return out

    def format_status(self, ticker):
        thesis = self._theses.get(ticker); state = self._states.get(ticker)
        if not thesis: return f"📡 {ticker}: No thesis. Run /em."
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
                    lines.append(f"{emoji} {t.direction} @ ${t.entry_price:.2f}{pnl} stop=${stop_ref:.2f}")
        tp = _get_time_phase_ct(); lines.append(f"Phase: {tp['label']}")
        fast = ticker.upper() in MONITOR_FAST_POLL_TICKERS
        lines.append(f"Poll: {MONITOR_POLL_INTERVAL_FAST_SEC if fast else MONITOR_POLL_INTERVAL_SEC}s")
        return "\n".join(lines)

class ThesisMonitorDaemon:
    def __init__(self, engine, get_spot_fn, post_fn):
        self.engine = engine; self.get_spot = get_spot_fn; self.post_fn = post_fn
        self._enabled = True; self._thread = None; self._stop_event = threading.Event()
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
                    if ev.get("priority", 1) >= 3: self._post_alert(ticker, price, ev)
                    else: log.info(f"Monitor [{ticker}]: {ev.get('msg','')}")
            except Exception as e: log.warning(f"Monitor {ticker} failed: {e}")
    def _post_alert(self, ticker, price, event):
        tp = _get_time_phase_ct(); state = self.engine.get_state(ticker); thesis = self.engine.get_thesis(ticker)
        is_exit = event.get("type") == "exit"
        header = f"📊 {ticker} TRADE MGMT — ${price:.2f}" if is_exit else f"📡 {ticker} THESIS ALERT — ${price:.2f}"
        lines = [header, "", event["msg"]]
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

def build_thesis_from_em_card(ticker, spot, bias, eng, em, walls, cagf=None, vix=None, v4_result=None, session_label="", local_walls=None, prior_day_close=None):
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
    return ThesisContext(ticker=ticker, bias=bias.get("direction", "NEUTRAL"), bias_score=bias.get("score", 0), gex_sign=gex_sign, gex_value=round(gex_val, 2), dex_value=round(eng.get("dex", 0), 2), vanna_value=round(eng.get("vanna", 0), 2), charm_value=round(eng.get("charm", 0), 2), regime=regime, volatility_regime=v4_result.get("vol_regime", {}).get("label", "NORMAL") if v4_result else "NORMAL", vix=vix.get("vix", 20) if isinstance(vix, dict) else 20, iv=v4_result.get("iv", 0.20) if v4_result else 0.20, prior_day_close=prior_day_close, prior_day_context=prior_ctx, session_label=session_label, levels=levels, created_at=ts, spot_at_creation=spot)

_monitor_engine = ThesisMonitorEngine()
_monitor_daemon: Optional[ThesisMonitorDaemon] = None
def get_engine(): return _monitor_engine
def get_daemon(): return _monitor_daemon
def init_daemon(get_spot_fn, post_fn, store_get_fn=None, store_set_fn=None):
    global _monitor_daemon
    _monitor_engine._store_get = store_get_fn; _monitor_engine._store_set = store_set_fn
    _monitor_daemon = ThesisMonitorDaemon(_monitor_engine, get_spot_fn, post_fn)
    _monitor_daemon.start(); log.info("Thesis monitor daemon initialized"); return _monitor_daemon
