# market_regime.py
# Self-contained v3.1 — no external dependency required
# If regime_config.py exists, feature flags are loaded from it.
# If not, this file works standalone with inline defaults.

import logging
import math
import threading
from datetime import datetime, date
from typing import Optional, Callable, List, Dict

log = logging.getLogger(__name__)

# ── Inline defaults (no external file needed) ──
ENABLE_CORE_REGIME_V2    = True
ENABLE_EVENT_OVERLAYS    = False
ENABLE_SECTOR_OVERLAYS   = False
LOG_ONLY_EVENT_OVERLAYS  = True
LOG_ONLY_SECTOR_OVERLAYS = True

BULL_BASE       = "BULL_BASE"
BULL_TRANSITION = "BULL_TRANSITION"
CHOP            = "CHOP"
BEAR_TRANSITION = "BEAR_TRANSITION"
BEAR_CRISIS     = "BEAR_CRISIS"

REGIME_SCORE_THRESHOLDS = {BULL_BASE: 6, BULL_TRANSITION: 2, CHOP: -1, BEAR_TRANSITION: -5, BEAR_CRISIS: -99}
REGIME_HYSTERESIS_DAYS = 2
V2_TO_V1 = {BULL_BASE: "BULL", BULL_TRANSITION: "TRANSITION", CHOP: "TRANSITION", BEAR_TRANSITION: "BEAR", BEAR_CRISIS: "BEAR"}

EVENT_NONE = "NONE"
EVENT_MACRO_SHOCK = "MACRO_SHOCK"
EVENT_WAR_CRISIS = "WAR_CRISIS"
EVENT_DECAY_SESSIONS = {EVENT_MACRO_SHOCK: 3, EVENT_WAR_CRISIS: 5}
EVENT_MODIFIERS = {EVENT_MACRO_SHOCK: {}, EVENT_WAR_CRISIS: {"force_regime_floor": BEAR_CRISIS}}
VIX_SHOCK_THRESHOLD = 25.0
VIX_SPIKE_PCT = 25.0
VIX_CRISIS_THRESHOLD = 35.0
VIX_CRISIS_DAYS = 2

SECTOR_AI_STRONG = "AI_STRONG"
SECTOR_AI_WEAK = "AI_WEAK"
SECTOR_COMMODITIES_STRONG = "COMMODITIES_STRONG"
SECTOR_DEFENSIVES_STRONG = "DEFENSIVES_STRONG"
SECTOR_BREADTH_WEAK = "INDEX_BREADTH_WEAK"
SECTOR_BREADTH_HEALTHY = "BREADTH_HEALTHY"
SECTOR_ETFS = {"tech": ["XLK", "SMH"], "commodity": ["XLE", "GLD"], "defensive": ["XLP", "XLU"]}
SECTOR_OUTPERFORM_THRESHOLD = 1.5
SECTOR_UNDERPERFORM_THRESHOLD = -1.5

CORE_TICKERS = ["SPY", "QQQ", "IWM"]
VIX_TICKER = "^VIX"
SECTOR_TICKERS = ["XLK", "SMH", "XLE", "XLP", "XLU", "GLD", "TLT"]
DAILY_LOOKBACK = 70

REGIME_DEFAULTS = {
    BULL_BASE:       {"direction_bias": "bull",    "long_min_score": 55, "short_min_score": 75, "default_hold_long": 5, "default_hold_short": 1},
    BULL_TRANSITION: {"direction_bias": "bull",    "long_min_score": 60, "short_min_score": 80, "default_hold_long": 5, "default_hold_short": 1},
    CHOP:            {"direction_bias": "neutral", "long_min_score": 70, "short_min_score": 70, "default_hold_long": 1, "default_hold_short": 1},
    BEAR_TRANSITION: {"direction_bias": "bear",    "long_min_score": 72, "short_min_score": 60, "default_hold_long": 1, "default_hold_short": 3},
    BEAR_CRISIS:     {"direction_bias": "bear",    "long_min_score": 80, "short_min_score": 55, "default_hold_long": 1, "default_hold_short": 5},
}

# Try to load overrides — if this fails for ANY reason, bot still starts
try:
    import regime_config as _rc
    ENABLE_CORE_REGIME_V2    = getattr(_rc, "ENABLE_CORE_REGIME_V2", ENABLE_CORE_REGIME_V2)
    ENABLE_EVENT_OVERLAYS    = getattr(_rc, "ENABLE_EVENT_OVERLAYS", ENABLE_EVENT_OVERLAYS)
    ENABLE_SECTOR_OVERLAYS   = getattr(_rc, "ENABLE_SECTOR_OVERLAYS", ENABLE_SECTOR_OVERLAYS)
    LOG_ONLY_EVENT_OVERLAYS  = getattr(_rc, "LOG_ONLY_EVENT_OVERLAYS", LOG_ONLY_EVENT_OVERLAYS)
    LOG_ONLY_SECTOR_OVERLAYS = getattr(_rc, "LOG_ONLY_SECTOR_OVERLAYS", LOG_ONLY_SECTOR_OVERLAYS)
    log.info("market_regime: loaded feature flags from regime_config.py")
except Exception:
    log.info("market_regime: using inline defaults (regime_config.py not available)")

# ── Legacy exports (active_scanner.py imports these) ──
REGIME_BEAR       = "BEAR"
REGIME_TRANSITION = "TRANSITION"
REGIME_BULL       = "BULL"
REGIME_UNKNOWN    = "UNKNOWN"
SUB_HEALTHY  = "HEALTHY"
SUB_PULLBACK = "PULLBACK"
SUB_NONE     = "NONE"
DEFAULT_REGIME = REGIME_BEAR
MA20 = 20
MA50 = 50

def empty_regime_package():
    return {
        "core_regime": BEAR_CRISIS, "core_score": 0, "core_score_details": {},
        "days_in_regime": 0, "regime_confidence": 0.0,
        "event_overlay": EVENT_NONE, "event_days_remaining": 0, "event_source": "",
        "sector_overlays": [], "sector_details": {},
        "effective_regime": BEAR_CRISIS, "v1_regime": "BEAR",
        "v2_active": ENABLE_CORE_REGIME_V2,
        "events_active": ENABLE_EVENT_OVERLAYS,
        "sectors_active": ENABLE_SECTOR_OVERLAYS,
    }

# ── Math helpers ──
def _sma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def _slope_positive(closes, p):
    if len(closes) < p + 5: return False
    now = sum(closes[-p:]) / p
    prev_slice = closes[:-5]
    if len(prev_slice) < p: return False
    return now > sum(prev_slice[-p:]) / p

def _consecutive_above(closes, p):
    if len(closes) < p + 1: return 0
    c = 0
    for i in range(len(closes)-1, p-2, -1):
        if closes[i] > sum(closes[:i+1][-p:]) / p: c += 1
        else: break
    return c

def _consecutive_below(closes, p):
    if len(closes) < p + 1: return 0
    c = 0
    for i in range(len(closes)-1, p-2, -1):
        if closes[i] < sum(closes[:i+1][-p:]) / p: c += 1
        else: break
    return c

def _pct_return(closes, days):
    if len(closes) < days + 1: return 0.0
    old = closes[-(days+1)]
    return ((closes[-1] - old) / old) * 100 if old else 0.0

def _std_dev(vals):
    if len(vals) < 2: return 0.0
    m = sum(vals)/len(vals)
    return math.sqrt(sum((x-m)**2 for x in vals)/(len(vals)-1))


# ═══════════════════════════════════════════════════════════
# DETECTOR CLASS
# ═══════════════════════════════════════════════════════════

class MarketRegimeDetector:
    def __init__(self, notify_fn=None):
        self._lock = threading.Lock()
        self._notify_fn = notify_fn
        self._last_refresh_date = None
        self._core_regime = BEAR_CRISIS
        self._core_score = 0
        self._core_score_details = {}
        self._regime_since = None
        self._days_in_regime = 0
        self._candidate_regime = None
        self._candidate_days = 0
        self._event_overlay = EVENT_NONE
        self._event_set_date = None
        self._event_source = ""
        self._manual_event = None
        self._sector_overlays = []
        self._sector_details = {}
        self._prices = {}
        self._mas = {}
        self._v1_regime = DEFAULT_REGIME
        self._sub_regime = SUB_NONE
        # Legacy state for display
        self._qqq_price = 0.0
        self._qqq_ma20 = 0.0
        self._qqq_ma50 = 0.0
        self._iwm_price = 0.0
        self._iwm_ma50 = 0.0
        self._qqq_above_ma20_days = 0
        self._qqq_below_ma20_days = 0
        self._both_above_ma50_days = 0

    def get_regime(self):
        with self._lock: return self._v1_regime

    def get_sub_regime(self):
        with self._lock: return self._sub_regime

    def get_regime_package(self):
        with self._lock:
            pkg = empty_regime_package()
            pkg["core_regime"] = self._core_regime
            pkg["core_score"] = self._core_score
            pkg["core_score_details"] = dict(self._core_score_details)
            pkg["days_in_regime"] = self._days_in_regime
            pkg["regime_confidence"] = self._compute_confidence()
            pkg["event_overlay"] = self._event_overlay
            pkg["event_days_remaining"] = self._event_days_remaining()
            pkg["event_source"] = self._event_source
            pkg["sector_overlays"] = list(self._sector_overlays)
            pkg["sector_details"] = dict(self._sector_details)
            pkg["effective_regime"] = self._effective_regime()
            pkg["v1_regime"] = self._v1_regime
            return pkg

    def needs_refresh(self):
        with self._lock: return self._last_refresh_date != date.today()

    def set_event_override(self, event, source="manual"):
        with self._lock:
            self._manual_event = event
            self._event_overlay = event
            self._event_set_date = date.today()
            self._event_source = source

    def clear_event_override(self):
        with self._lock:
            self._manual_event = None
            self._event_overlay = EVENT_NONE
            self._event_set_date = None
            self._event_source = ""

    def refresh(self, daily_candle_fn):
        try:
            data = {}
            for t in CORE_TICKERS:
                c = daily_candle_fn(t, days=DAILY_LOOKBACK)
                if c and len(c) >= MA50 + 5: data[t] = c
            try:
                vix = daily_candle_fn(VIX_TICKER, days=DAILY_LOOKBACK)
                if vix and len(vix) >= 11: data[VIX_TICKER] = vix
            except Exception: pass

            if "QQQ" not in data or "IWM" not in data:
                log.warning("MarketRegime: missing QQQ/IWM")
                return self._v1_regime

            sector_data = {}
            if ENABLE_SECTOR_OVERLAYS or LOG_ONLY_SECTOR_OVERLAYS:
                for t in SECTOR_TICKERS:
                    try:
                        c = daily_candle_fn(t, days=DAILY_LOOKBACK)
                        if c and len(c) >= 6: sector_data[t] = c
                    except Exception: pass

            new_core = self._compute_core(data)
            new_event = self._compute_event(data)
            new_sectors = self._compute_sectors(data, sector_data)

            with self._lock:
                old_v1 = self._v1_regime
                old_core = self._core_regime

                # Hysteresis
                if new_core != self._core_regime:
                    if new_core == self._candidate_regime:
                        self._candidate_days += 1
                    else:
                        self._candidate_regime = new_core
                        self._candidate_days = 1
                    if self._candidate_days >= REGIME_HYSTERESIS_DAYS:
                        self._core_regime = new_core
                        self._candidate_regime = None
                        self._candidate_days = 0
                        self._regime_since = date.today()
                        self._days_in_regime = 0
                else:
                    self._candidate_regime = None
                    self._candidate_days = 0
                    self._days_in_regime += 1

                if self._manual_event is None:
                    self._event_overlay = new_event
                self._sector_overlays = new_sectors

                if ENABLE_CORE_REGIME_V2:
                    self._v1_regime = V2_TO_V1.get(self._core_regime, REGIME_BEAR)
                    if self._v1_regime == REGIME_TRANSITION:
                        self._sub_regime = SUB_HEALTHY if self._core_regime == BULL_TRANSITION else SUB_PULLBACK
                    else:
                        self._sub_regime = SUB_NONE
                else:
                    self._v1_regime = self._legacy_v1(data)

                self._last_refresh_date = date.today()

            if self._v1_regime != old_v1 or self._core_regime != old_core:
                self._on_change(old_core, self._core_regime, old_v1, self._v1_regime)

            log.info(f"MarketRegime: {self._core_regime} (score={self._core_score}) v1={self._v1_regime} event={self._event_overlay} sectors={self._sector_overlays}")
            return self._v1_regime
        except Exception as e:
            log.error(f"MarketRegime refresh failed: {e}", exc_info=True)
            return self._v1_regime

    def _compute_core(self, data):
        score = 0
        d = {}
        spy = data.get("SPY", [])
        qqq = data.get("QQQ", [])
        iwm = data.get("IWM", [])
        vix = data.get(VIX_TICKER, [])

        # Trend
        t = 0
        spy20 = _sma(spy, MA20); spy50 = _sma(spy, MA50)
        qqq20 = _sma(qqq, MA20); qqq50 = _sma(qqq, MA50)
        if spy and spy20 and spy[-1] > spy20: t += 1
        if spy and spy50 and spy[-1] > spy50: t += 1
        if _slope_positive(spy, MA20): t += 1
        if qqq and qqq20 and qqq[-1] > qqq20: t += 1
        if qqq and qqq50 and qqq[-1] > qqq50: t += 1
        if _slope_positive(qqq, MA20): t += 1
        if spy and spy20 and spy[-1] < spy20: t -= 1
        if qqq and qqq20 and qqq[-1] < qqq20: t -= 1
        d["trend"] = t; score += t

        # Breadth
        b = 0
        iwm20 = _sma(iwm, MA20); iwm50 = _sma(iwm, MA50)
        if iwm and iwm20 and iwm[-1] > iwm20: b += 1
        if iwm and iwm50 and iwm[-1] > iwm50: b += 1
        if iwm and iwm20 and iwm50 and iwm[-1] < iwm20 and iwm[-1] < iwm50: b -= 1
        d["breadth"] = b; score += b

        # Vol
        v = 0
        if vix and len(vix) >= 11:
            vn = vix[-1]; va = sum(vix[-10:])/10; vs = _std_dev(vix[-10:])
            if vn < va: v += 1
            if vs > 0 and vn > va + vs: v -= 1
            if vs > 0 and vn > va + 2*vs: v -= 1
            d["vix"] = round(vn, 1)
        d["vol"] = v; score += v

        self._core_score = score
        self._core_score_details = d

        # Store prices
        if qqq:
            self._qqq_price = qqq[-1]
            self._qqq_ma20 = qqq20 or 0; self._qqq_ma50 = qqq50 or 0
            self._prices["QQQ"] = qqq[-1]
            self._mas.setdefault("QQQ", {})[20] = qqq20 or 0
            self._mas.setdefault("QQQ", {})[50] = qqq50 or 0
            self._qqq_above_ma20_days = _consecutive_above(qqq, MA20)
            self._qqq_below_ma20_days = _consecutive_below(qqq, MA20)
        if iwm:
            self._iwm_price = iwm[-1]
            self._iwm_ma50 = iwm50 or 0
            self._prices["IWM"] = iwm[-1]
            self._mas.setdefault("IWM", {})[50] = iwm50 or 0
        if spy:
            self._prices["SPY"] = spy[-1]
        if qqq and iwm:
            self._both_above_ma50_days = min(_consecutive_above(qqq, MA50), _consecutive_above(iwm, MA50))

        if score >= REGIME_SCORE_THRESHOLDS[BULL_BASE]: return BULL_BASE
        elif score >= REGIME_SCORE_THRESHOLDS[BULL_TRANSITION]: return BULL_TRANSITION
        elif score >= REGIME_SCORE_THRESHOLDS[CHOP]: return CHOP
        elif score >= REGIME_SCORE_THRESHOLDS[BEAR_TRANSITION]: return BEAR_TRANSITION
        else: return BEAR_CRISIS

    def _compute_event(self, data):
        vix = data.get(VIX_TICKER, [])
        if not vix or len(vix) < 3:
            return self._check_decay()
        vn = vix[-1]; vp = vix[-2]; va = sum(vix[-10:])/min(10,len(vix))

        if vn > VIX_CRISIS_THRESHOLD:
            above = sum(1 for v in vix[-VIX_CRISIS_DAYS:] if v > VIX_CRISIS_THRESHOLD)
            if above >= VIX_CRISIS_DAYS:
                self._event_source = f"VIX >{VIX_CRISIS_THRESHOLD} for {above}d"
                self._event_set_date = date.today()
                return EVENT_WAR_CRISIS

        if vp > 0:
            spike = ((vn - vp)/vp)*100
            if spike > VIX_SPIKE_PCT:
                self._event_source = f"VIX spiked {spike:.0f}%"
                self._event_set_date = date.today()
                return EVENT_MACRO_SHOCK

        if vn > VIX_SHOCK_THRESHOLD and vn > va * 1.15:
            self._event_source = f"VIX {vn:.1f} elevated"
            self._event_set_date = date.today()
            return EVENT_MACRO_SHOCK

        return self._check_decay()

    def _check_decay(self):
        if self._event_overlay != EVENT_NONE and self._event_days_remaining() > 0:
            return self._event_overlay
        return EVENT_NONE

    def _event_days_remaining(self):
        if self._event_overlay == EVENT_NONE or not self._event_set_date: return 0
        mx = EVENT_DECAY_SESSIONS.get(self._event_overlay, 3)
        return max(0, mx - (date.today() - self._event_set_date).days)

    def _compute_sectors(self, core, sector_data):
        overlays = []
        spy = core.get("SPY", [])
        if not spy or len(spy) < 6: return overlays
        spy5 = _pct_return(spy, 5)
        self._sector_details = {"SPY_5d": round(spy5, 2)}

        def _chk(grp, strong, weak=None):
            rets = []
            for t in SECTOR_ETFS.get(grp, []):
                c = sector_data.get(t, [])
                if c and len(c) >= 6:
                    r = _pct_return(c, 5); rets.append(r)
                    self._sector_details[f"{t}_5d"] = round(r, 2)
            if rets:
                diff = sum(rets)/len(rets) - spy5
                if diff > SECTOR_OUTPERFORM_THRESHOLD: overlays.append(strong)
                elif weak and diff < SECTOR_UNDERPERFORM_THRESHOLD: overlays.append(weak)

        _chk("tech", SECTOR_AI_STRONG, SECTOR_AI_WEAK)
        _chk("commodity", SECTOR_COMMODITIES_STRONG)
        _chk("defensive", SECTOR_DEFENSIVES_STRONG)

        iwm = core.get("IWM", [])
        if iwm and len(iwm) >= 6:
            d = _pct_return(iwm, 5) - spy5
            self._sector_details["IWM_5d"] = round(_pct_return(iwm, 5), 2)
            if d < SECTOR_UNDERPERFORM_THRESHOLD: overlays.append(SECTOR_BREADTH_WEAK)
            elif d > SECTOR_OUTPERFORM_THRESHOLD: overlays.append(SECTOR_BREADTH_HEALTHY)
        return overlays

    def _effective_regime(self):
        eff = self._core_regime
        if ENABLE_EVENT_OVERLAYS and self._event_overlay != EVENT_NONE:
            mods = EVENT_MODIFIERS.get(self._event_overlay, {})
            floor = mods.get("force_regime_floor")
            if floor:
                order = [BULL_BASE, BULL_TRANSITION, CHOP, BEAR_TRANSITION, BEAR_CRISIS]
                ei = order.index(eff) if eff in order else 0
                fi = order.index(floor) if floor in order else 0
                if fi > ei: eff = floor
        return eff

    def _compute_confidence(self):
        thresholds = sorted(REGIME_SCORE_THRESHOLDS.values(), reverse=True)
        return min(1.0, min(abs(self._core_score - t) for t in thresholds) / 4.0)

    def _legacy_v1(self, data):
        qqq = data.get("QQQ", []); iwm = data.get("IWM", [])
        if not qqq or not iwm: return self._v1_regime
        b50 = min(_consecutive_above(qqq, MA50), _consecutive_above(iwm, MA50))
        if b50 >= 10: return REGIME_BULL
        if _consecutive_above(qqq, MA20) >= 5: return REGIME_TRANSITION
        if _consecutive_below(qqq, MA20) >= 3: return REGIME_BEAR
        return self._v1_regime

    # ── Status ──

    def get_status(self):
        with self._lock:
            pkg = self.get_regime_package()
            pkg["regime_since"] = str(self._regime_since)
            pkg["last_refresh"] = str(self._last_refresh_date)
            pkg["prices"] = dict(self._prices)
            pkg["mas"] = {k: dict(v) for k, v in self._mas.items()}
            pkg["qqq_price"] = self._qqq_price
            pkg["qqq_ma20"] = self._qqq_ma20
            pkg["qqq_ma50"] = self._qqq_ma50
            pkg["iwm_price"] = self._iwm_price
            pkg["iwm_ma50"] = self._iwm_ma50
            pkg["qqq_below_ma20_days"] = self._qqq_below_ma20_days
            pkg["qqq_above_ma20_days"] = self._qqq_above_ma20_days
            pkg["both_above_ma50_days"] = self._both_above_ma50_days
            return pkg

    def format_status_message(self):
        s = self.get_status()
        core = s["core_regime"]; v1 = s["v1_regime"]
        emoji = {BULL_BASE: "🟢", BULL_TRANSITION: "🟡", CHOP: "⚪", BEAR_TRANSITION: "🟠", BEAR_CRISIS: "🔴"}.get(core, "⚪")
        lines = [
            f"{emoji} REGIME: {core} (score={s['core_score']}, conf={s['regime_confidence']:.0%})",
            f"V1: {v1} | Days: {s['days_in_regime']} | Since: {s['regime_since']}",
            f"Score: trend={s['core_score_details'].get('trend',0)} breadth={s['core_score_details'].get('breadth',0)} vol={s['core_score_details'].get('vol',0)}",
        ]
        if s.get("qqq_price"):
            lines.append(f"QQQ: ${s['qqq_price']:.2f} MA20=${s['qqq_ma20']:.2f} MA50=${s['qqq_ma50']:.2f}")
            lines.append(f"  Below MA20: {s['qqq_below_ma20_days']}d | Above MA20: {s['qqq_above_ma20_days']}d")
        if s.get("iwm_price"):
            lines.append(f"IWM: ${s['iwm_price']:.2f} MA50=${s['iwm_ma50']:.2f}")
        ev = s.get("event_overlay", EVENT_NONE)
        if ev != EVENT_NONE:
            a = "LIVE" if ENABLE_EVENT_OVERLAYS else "LOG"
            lines.append(f"⚡ EVENT: {ev} ({s['event_days_remaining']}d left) [{a}]")
        sectors = s.get("sector_overlays", [])
        if sectors:
            a = "LIVE" if ENABLE_SECTOR_OVERLAYS else "LOG"
            lines.append(f"📊 SECTORS [{a}]: {', '.join(sectors)}")
        return "\n".join(lines)

    def _on_change(self, old_core, new_core, old_v1, new_v1):
        emoji = {BULL_BASE: "🟢", BULL_TRANSITION: "🟡", CHOP: "⚪", BEAR_TRANSITION: "🟠", BEAR_CRISIS: "🔴"}
        lines = [
            f"⚠️ REGIME CHANGE",
            f"{emoji.get(old_core,'⚪')} {old_core} → {emoji.get(new_core,'⚪')} {new_core}",
            f"V1: {old_v1} → {new_v1} | Score: {self._core_score}",
        ]
        if self._event_overlay != EVENT_NONE:
            lines.append(f"Event: {self._event_overlay}")
        if self._sector_overlays:
            lines.append(f"Sectors: {', '.join(self._sector_overlays)}")
        defaults = REGIME_DEFAULTS.get(new_core, {})
        if defaults:
            lines.append(f"Bias: {defaults.get('direction_bias')} | Long≥{defaults.get('long_min_score')} Short≥{defaults.get('short_min_score')}")
        msg = "\n".join(lines)
        log.warning(f"REGIME CHANGE: {old_core} → {new_core}")
        if self._notify_fn:
            try: self._notify_fn(msg)
            except Exception as e: log.error(f"Regime alert failed: {e}")


# ── Module-level API ──
_detector = None

def init_regime_detector(notify_fn=None):
    global _detector
    _detector = MarketRegimeDetector(notify_fn=notify_fn)
    return _detector

def get_market_regime():
    return _detector.get_regime() if _detector else DEFAULT_REGIME

def get_market_sub_regime():
    return _detector.get_sub_regime() if _detector else SUB_NONE

def get_regime_package():
    return _detector.get_regime_package() if _detector else empty_regime_package()
