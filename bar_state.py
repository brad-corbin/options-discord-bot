# bar_state.py
# ═══════════════════════════════════════════════════════════════════
# Bar-Aware Market State Layer
#
# Replaces point-to-point spot sampling with true OHLCV bar structure.
# Manages 1m and 5m bar buffers, rolling VWAP, opening range, and
# per-bar derived metrics (body %, wick %, expansion, distance from
# key levels). Everything downstream validates from bar closes, not
# sampled spot prices.
#
# API cost: one 5m candle fetch per poll cycle (~60s cache).
# Initial fill: one call fetching today's bars since open.
# Incremental: cache hit most cycles, one call on expiry.
# ═══════════════════════════════════════════════════════════════════

import logging
import time as _time
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List, Dict

log = logging.getLogger(__name__)

# ── Constants ──
OR_5_MINUTES = 5
OR_15_MINUTES = 15
OR_30_MINUTES = 30
MARKET_OPEN_MINS_CT = 510      # 8:30 AM CT in minutes
MARKET_CLOSE_MINS_CT = 915     # 3:15 PM CT (SPY/QQQ options close)
BAR_BUFFER_MAX_5M = 120        # ~10 hours of 5m bars
BAR_BUFFER_MAX_1M = 420        # 7 hours of 1m bars


@dataclass
class Bar:
    """Single OHLCV bar."""
    timestamp: float      # epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    resolution: int = 5   # minutes

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body_pct(self) -> float:
        """Body as fraction of range. 1.0 = no wicks."""
        return self.body / self.range if self.range > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def upper_wick_pct(self) -> float:
        return self.upper_wick / self.range if self.range > 0 else 0.0

    @property
    def lower_wick_pct(self) -> float:
        return self.lower_wick / self.range if self.range > 0 else 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    def closed_through(self, level: float, direction: str) -> bool:
        """Did this bar CLOSE through a level (not just wick)?"""
        if direction == "DOWN":
            return self.close < level
        return self.close > level

    def wicked_through(self, level: float, direction: str) -> bool:
        """Did this bar wick through a level without closing through it?"""
        if direction == "DOWN":
            return self.low < level <= self.close
        return self.high > level >= self.close


@dataclass
class OpeningRange:
    """Opening range computed from first N minutes of session."""
    high: float = 0.0
    low: float = 0.0
    width: float = 0.0
    width_pct: float = 0.0
    range_type: str = "UNKNOWN"   # narrow / normal / wide
    minutes: int = 30
    bar_count: int = 0
    vwap_at_or_close: float = 0.0
    is_complete: bool = False

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2 if self.high > 0 else 0.0


@dataclass
class VWAPState:
    """Rolling VWAP tracker."""
    cumulative_tpv: float = 0.0   # sum(typical_price * volume)
    cumulative_vol: float = 0.0   # sum(volume)
    value: float = 0.0

    def update(self, bar: Bar):
        tp = (bar.high + bar.low + bar.close) / 3
        vol = max(bar.volume, 1)  # avoid div-by-zero if volume missing
        self.cumulative_tpv += tp * vol
        self.cumulative_vol += vol
        self.value = self.cumulative_tpv / self.cumulative_vol if self.cumulative_vol > 0 else bar.close

    def reset(self):
        self.cumulative_tpv = 0.0
        self.cumulative_vol = 0.0
        self.value = 0.0


@dataclass
class BarMetrics:
    """Derived metrics from recent bar history."""
    avg_range_5: float = 0.0        # avg true range of last 5 bars
    avg_body_pct_5: float = 0.0     # avg body% of last 5 bars
    expansion_ratio: float = 1.0    # current bar range / avg range
    consecutive_direction: int = 0  # +N bullish, -N bearish
    net_move_5: float = 0.0         # close[now] - close[5 ago]
    net_move_pct_5: float = 0.0


@dataclass
class ExecutionState:
    """
    Complete bar-aware market state for a single ticker.
    This replaces the old price_history list of spot samples.
    """
    ticker: str = ""
    # Bar buffers
    bars_5m: List[Bar] = field(default_factory=list)
    bars_1m: List[Bar] = field(default_factory=list)
    # Opening ranges
    or_5: OpeningRange = field(default_factory=lambda: OpeningRange(minutes=5))
    or_15: OpeningRange = field(default_factory=lambda: OpeningRange(minutes=15))
    or_30: OpeningRange = field(default_factory=lambda: OpeningRange(minutes=30))
    # VWAP
    vwap: VWAPState = field(default_factory=VWAPState)
    # Derived
    metrics: BarMetrics = field(default_factory=BarMetrics)
    # Current state
    last_close: float = 0.0
    last_bar_ts: float = 0.0
    session_high: float = 0.0
    session_low: float = 999999.0
    session_date: str = ""
    bars_since_open: int = 0


class BarStateManager:
    """
    Manages bar ingestion, OR computation, VWAP, and derived metrics.
    One instance per monitored ticker.
    """

    def __init__(self, ticker: str, get_bars_fn=None, resolution: int = 5):
        """
        Args:
            ticker: Symbol
            get_bars_fn: callable(ticker, resolution, countback) -> list[dict]
                         Each dict: {t: epoch, o, h, l, c, v}
            resolution: Bar size in minutes. Default 5. Use 1 for SPY to reduce lag.
        """
        self.ticker = ticker
        self._get_bars = get_bars_fn
        self._resolution = resolution
        # Countback for init: cover ~6.5 hours of session
        # 5m: 80 bars, 1m: 400 bars
        self._init_countback = 400 if resolution == 1 else 80
        # Countback for update: last 3 bars (same logic, faster resolution = more recent data)
        self._update_countback = 5 if resolution == 1 else 3
        self.state = ExecutionState(ticker=ticker)
        self._initialized = False

    def initialize(self) -> bool:
        """Fetch today's bars to build initial state. Call once at session start."""
        if not self._get_bars:
            log.warning(f"BarState [{self.ticker}]: no get_bars_fn, cannot initialize")
            return False
        try:
            raw_bars = self._get_bars(self.ticker, self._resolution, self._init_countback)
            if not raw_bars:
                log.warning(f"BarState [{self.ticker}]: no bars returned")
                return False
            bars = self._parse_bars(raw_bars, resolution=self._resolution)
            if not bars:
                return False
            # Filter to today only
            today = _get_today_str()
            today_bars = [b for b in bars if _epoch_to_date(b.timestamp) == today]
            if not today_bars:
                today_bars = bars[-40:]  # fallback: last 40 bars
            self.state.bars_5m = today_bars[-BAR_BUFFER_MAX_5M:]
            self.state.session_date = today
            # Build opening ranges from bars
            self._compute_opening_ranges()
            # Build VWAP from all today's bars
            self.state.vwap.reset()
            for bar in self.state.bars_5m:
                self.state.vwap.update(bar)
            # Update metrics
            self._update_session_extremes()
            self._update_metrics()
            if self.state.bars_5m:
                self.state.last_close = self.state.bars_5m[-1].close
                self.state.last_bar_ts = self.state.bars_5m[-1].timestamp
                self.state.bars_since_open = len(self.state.bars_5m)
            self._initialized = True
            log.info(f"BarState [{self.ticker}]: initialized with {len(self.state.bars_5m)} {self._resolution}m bars, "
                     f"VWAP=${self.state.vwap.value:.2f}, "
                     f"OR30=${self.state.or_30.low:.2f}-${self.state.or_30.high:.2f}")
            return True
        except Exception as e:
            log.error(f"BarState [{self.ticker}]: init failed: {e}", exc_info=True)
            return False

    def update(self) -> Optional[Bar]:
        """
        Fetch latest bars and append new ones. Returns the newest bar if new,
        else None. Call this on each poll cycle.
        """
        if not self._get_bars:
            return None
        try:
            raw = self._get_bars(self.ticker, self._resolution, self._update_countback)
            if not raw:
                return None
            bars = self._parse_bars(raw, resolution=self._resolution)
            if not bars:
                return None
            # Check for new session BEFORE appending — prevents clearing just-appended bars
            today = _get_today_str()
            if today != self.state.session_date:
                self._new_session(today)
                # Reinitialize with full backfill for new session
                return self._reinit_from_bars(bars)
            new_bar = None
            for bar in bars:
                if bar.timestamp > self.state.last_bar_ts:
                    self.state.bars_5m.append(bar)
                    self.state.vwap.update(bar)
                    new_bar = bar
                    self.state.last_bar_ts = bar.timestamp
                    self.state.last_close = bar.close
                    self.state.bars_since_open += 1
            # Trim buffer
            if len(self.state.bars_5m) > BAR_BUFFER_MAX_5M:
                self.state.bars_5m = self.state.bars_5m[-BAR_BUFFER_MAX_5M:]
            # Update OR if still forming
            if not self.state.or_30.is_complete:
                self._compute_opening_ranges()
            # Update derived metrics
            self._update_session_extremes()
            self._update_metrics()
            return new_bar
        except Exception as e:
            log.warning(f"BarState [{self.ticker}]: update failed: {e}")
            return None

    def _reinit_from_bars(self, seed_bars: list) -> Optional['Bar']:
        """After session reset, seed with available bars and re-init."""
        for bar in seed_bars:
            self.state.bars_5m.append(bar)
            self.state.vwap.update(bar)
            self.state.last_bar_ts = bar.timestamp
            self.state.last_close = bar.close
            self.state.bars_since_open += 1
        self._compute_opening_ranges()
        self._update_session_extremes()
        self._update_metrics()
        # Then do a full backfill
        self.initialize()
        return self.state.bars_5m[-1] if self.state.bars_5m else None

    def get_latest_bar(self) -> Optional[Bar]:
        return self.state.bars_5m[-1] if self.state.bars_5m else None

    def get_close(self) -> float:
        return self.state.last_close

    def get_recent_bars(self, n: int = 5) -> List[Bar]:
        return self.state.bars_5m[-n:]

    def bar_closed_through(self, level: float, direction: str, lookback: int = 1) -> bool:
        """Did any of the last N bars close through a level?"""
        for bar in self.state.bars_5m[-lookback:]:
            if bar.closed_through(level, direction):
                return True
        return False

    def bar_wicked_only(self, level: float, direction: str, lookback: int = 1) -> bool:
        """Did bars wick through but NOT close through?"""
        wicked = False
        for bar in self.state.bars_5m[-lookback:]:
            if bar.wicked_through(level, direction):
                wicked = True
            if bar.closed_through(level, direction):
                return False
        return wicked

    def distance_from_vwap(self, price: float = None) -> float:
        """Distance from VWAP as percentage."""
        p = price or self.state.last_close
        if self.state.vwap.value <= 0 or p <= 0:
            return 0.0
        return (p - self.state.vwap.value) / self.state.vwap.value * 100

    def distance_from_or(self, price: float = None, or_minutes: int = 30) -> dict:
        """Distance from opening range boundaries."""
        p = price or self.state.last_close
        orng = {5: self.state.or_5, 15: self.state.or_15, 30: self.state.or_30}.get(or_minutes, self.state.or_30)
        if not orng.is_complete or orng.high <= 0:
            return {"above_or": False, "below_or": False, "inside_or": False, "dist_high": 0, "dist_low": 0}
        return {
            "above_or": p > orng.high,
            "below_or": p < orng.low,
            "inside_or": orng.low <= p <= orng.high,
            "dist_high": (p - orng.high) / orng.high * 100 if orng.high > 0 else 0,
            "dist_low": (p - orng.low) / orng.low * 100 if orng.low > 0 else 0,
            "dist_mid": (p - orng.midpoint) / orng.midpoint * 100 if orng.midpoint > 0 else 0,
        }

    def is_expanding(self, threshold: float = 1.5) -> bool:
        """Is the current bar range expanding vs recent average?"""
        return self.state.metrics.expansion_ratio >= threshold

    # ── Internal: Parse raw API response into Bar objects ──

    def _parse_bars(self, raw: list, resolution: int = 5) -> List[Bar]:
        """Parse list of dicts or API-style response into Bar objects."""
        bars = []
        if isinstance(raw, dict):
            # MarketData.app format: {s: "ok", o: [...], h: [...], ...}
            opens = raw.get("o") or []
            highs = raw.get("h") or []
            lows = raw.get("l") or []
            closes = raw.get("c") or []
            volumes = raw.get("v") or []
            timestamps = raw.get("t") or []
            n = min(len(opens), len(highs), len(lows), len(closes))
            for i in range(n):
                bars.append(Bar(
                    timestamp=timestamps[i] if i < len(timestamps) else 0,
                    open=float(opens[i]),
                    high=float(highs[i]),
                    low=float(lows[i]),
                    close=float(closes[i]),
                    volume=int(volumes[i]) if i < len(volumes) and volumes[i] else 0,
                    resolution=resolution,
                ))
        elif isinstance(raw, list) and raw:
            if isinstance(raw[0], dict):
                for d in raw:
                    bars.append(Bar(
                        timestamp=d.get("t", d.get("timestamp", 0)),
                        open=float(d.get("o", d.get("open", 0))),
                        high=float(d.get("h", d.get("high", 0))),
                        low=float(d.get("l", d.get("low", 0))),
                        close=float(d.get("c", d.get("close", 0))),
                        volume=int(d.get("v", d.get("volume", 0))),
                        resolution=resolution,
                    ))
        return bars

    # ── Internal: Opening Range ──

    def _compute_opening_ranges(self):
        """Compute OR5, OR15, OR30 from the 5m bar buffer."""
        if not self.state.bars_5m:
            return
        # Find session open timestamp (first bar of today)
        open_ts = self.state.bars_5m[0].timestamp
        for minutes, orng in [(5, self.state.or_5), (15, self.state.or_15), (30, self.state.or_30)]:
            cutoff = open_ts + (minutes * 60)
            or_bars = [b for b in self.state.bars_5m if b.timestamp < cutoff]
            if not or_bars:
                continue
            orng.high = max(b.high for b in or_bars)
            orng.low = min(b.low for b in or_bars)
            orng.width = orng.high - orng.low
            mid = (orng.high + orng.low) / 2
            orng.width_pct = orng.width / mid * 100 if mid > 0 else 0
            orng.bar_count = len(or_bars)
            orng.vwap_at_or_close = self.state.vwap.value
            # Enough bars to consider complete?
            expected_bars = minutes // 5
            orng.is_complete = len(or_bars) >= expected_bars
            # Classify
            if orng.width_pct > 0.6:
                orng.range_type = "WIDE"
            elif orng.width_pct < 0.2:
                orng.range_type = "NARROW"
            else:
                orng.range_type = "NORMAL"

    # ── Internal: Session Extremes ──

    def _update_session_extremes(self):
        if not self.state.bars_5m:
            return
        self.state.session_high = max(b.high for b in self.state.bars_5m)
        self.state.session_low = min(b.low for b in self.state.bars_5m)

    # ── Internal: Derived Metrics ──

    def _update_metrics(self):
        bars = self.state.bars_5m
        m = self.state.metrics
        if len(bars) < 2:
            return
        recent = bars[-5:] if len(bars) >= 5 else bars
        m.avg_range_5 = sum(b.range for b in recent) / len(recent)
        m.avg_body_pct_5 = sum(b.body_pct for b in recent) / len(recent)
        current = bars[-1]
        m.expansion_ratio = current.range / m.avg_range_5 if m.avg_range_5 > 0 else 1.0
        # Consecutive direction
        count = 0
        for b in reversed(bars):
            if b.is_bullish:
                if count < 0:
                    break
                count += 1
            elif b.is_bearish:
                if count > 0:
                    break
                count -= 1
            else:
                break
        m.consecutive_direction = count
        # Net move over last 5 bars
        lookback = min(5, len(bars))
        m.net_move_5 = bars[-1].close - bars[-lookback].close
        ref = bars[-lookback].close
        m.net_move_pct_5 = m.net_move_5 / ref * 100 if ref > 0 else 0

    # ── Internal: Session Management ──

    def _new_session(self, date: str):
        """Reset for new trading day."""
        self.state.session_date = date
        self.state.bars_5m.clear()
        self.state.bars_1m.clear()
        self.state.vwap.reset()
        self.state.or_5 = OpeningRange(minutes=5)
        self.state.or_15 = OpeningRange(minutes=15)
        self.state.or_30 = OpeningRange(minutes=30)
        self.state.session_high = 0.0
        self.state.session_low = 999999.0
        self.state.bars_since_open = 0
        self.state.metrics = BarMetrics()
        log.info(f"BarState [{self.ticker}]: new session {date}")


# ── Helpers ──

def _get_today_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _epoch_to_date(epoch: float) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(epoch, ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")
