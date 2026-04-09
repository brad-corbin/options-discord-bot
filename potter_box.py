# potter_box.py
# ═══════════════════════════════════════════════════════════════════
# Potter Box — Consolidation Range Detection + Void Mapping
#
# Detects price consolidation boxes, measures empty space (voids),
# tracks box maturation, and generates trade structures when
# institutional flow confirms a breakout direction.
#
# Trade structure: 4:1 ratio with flow confirmation
#   - 4 long options in flow direction (strikes inside void)
#   - 1 long option opposite (hedge at box boundary)
#
# Data: yfinance daily OHLCV (free) + MarketData chains (for strikes)
# Persistence: Redis via PersistentState (survives redeploy)
# Schedule: 8:15 AM CT (detect) + 3:05 PM CT (update + catalog)
# ═══════════════════════════════════════════════════════════════════

import logging
import time
import math
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable, Tuple

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

# Box detection
MIN_BOX_BARS = 8               # minimum bars to qualify as a box
MAX_RANGE_PCT_INDEX = 0.06     # 6% max range for indexes
MAX_RANGE_PCT_MEGA = 0.10      # 10% for mega-caps
MAX_RANGE_PCT_MID = 0.14       # 14% for mid-caps
MIN_TOUCHES_ROOF = 2           # minimum touches on resistance
MIN_TOUCHES_FLOOR = 2          # minimum touches on support
TOUCH_ZONE_PCT = 0.005         # 0.5% tolerance for counting touches

# Void detection
MIN_VOID_PCT = 0.03            # 3% minimum void height to be tradeable
MAX_VOID_BAR_DENSITY = 3       # max candle bodies overlapping a zone = void

# Lookbacks
OHLCV_LOOKBACK_DAYS = 120      # 6 months of daily bars
HISTORY_LOOKBACK_DAYS = 90     # completed box records for averaging

# Default box durations (used until real data accumulates)
DEFAULT_DURATION = {
    "index": 10,
    "mega_cap": 15,
    "large_cap": 18,
    "mid_cap": 20,
}

# Maturity thresholds
MATURITY_EARLY_PCT = 0.50      # < 50% of avg = too early
MATURITY_MID_PCT = 0.75        # 50-75% = mid-life
MATURITY_LATE_PCT = 1.00       # 75-100% = late, breakout probable
# > 100% = overdue

# Trade structure
IV_PERCENTILE_MAX = 35         # only suggest when IV is compressed
MIN_FLOW_LEVEL = "notable"     # minimum flow to assign directional bias

# DTE mapping from box maturity
DTE_MAP = {
    "early": None,             # no trade — watchlist only
    "mid": 28,                 # 4 weeks
    "late": 21,                # 3 weeks
    "overdue": 14,             # 2 weeks
}

# Redis TTLs
TTL_ACTIVE_BOX = 7 * 86400    # 7 days
TTL_HISTORY_BOX = 90 * 86400  # 90 days
TTL_VOID_MAP = 7 * 86400      # 7 days
TTL_ALERT_DEDUP = 48 * 3600   # 48 hours
TTL_DEFAULTS = 30 * 86400     # 30 days

# Liquidity tier map (shared with oi_flow)
TIER_MAP = {
    "index": {"SPY", "QQQ", "IWM", "DIA"},
    "mega_cap": {"AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL"},
    "large_cap": {"AMD", "AVGO", "NFLX", "CRM", "BA", "LLY", "UNH",
                  "JPM", "GS", "CAT", "ORCL", "ARM"},
}


def _get_tier(ticker: str) -> str:
    t = ticker.upper()
    for tier, tickers in TIER_MAP.items():
        if t in tickers:
            return tier
    return "mid_cap"


def _max_range_pct(ticker: str) -> float:
    tier = _get_tier(ticker)
    return {
        "index": MAX_RANGE_PCT_INDEX,
        "mega_cap": MAX_RANGE_PCT_MEGA,
        "large_cap": MAX_RANGE_PCT_MID,
        "mid_cap": MAX_RANGE_PCT_MID,
    }.get(tier, MAX_RANGE_PCT_MID)


# ═══════════════════════════════════════════════════════════
# BOX DETECTION
# ═══════════════════════════════════════════════════════════

def detect_boxes(bars: List[dict], ticker: str) -> List[dict]:
    """
    Detect consolidation boxes from daily OHLCV bars.

    Each bar: {date, o, h, l, c, v}
    Returns list of box dicts sorted by recency.
    """
    if not bars or len(bars) < MIN_BOX_BARS + 2:
        return []

    max_range = _max_range_pct(ticker)
    n = len(bars)
    boxes = []

    # Sliding window approach: expand window while range stays bounded
    i = 0
    while i < n - MIN_BOX_BARS:
        # Start a potential box
        box_start = i
        running_high = bars[i]["h"]
        running_low = bars[i]["l"]

        j = i + 1
        while j < n:
            candidate_high = max(running_high, bars[j]["h"])
            candidate_low = min(running_low, bars[j]["l"])
            mid = (candidate_high + candidate_low) / 2
            if mid <= 0:
                break
            range_pct = (candidate_high - candidate_low) / mid

            if range_pct > max_range:
                break  # range exceeded, box ends at j-1

            running_high = candidate_high
            running_low = candidate_low
            j += 1

        box_end = j - 1
        box_bars = box_end - box_start + 1

        if box_bars >= MIN_BOX_BARS:
            # Count touches on roof and floor
            roof = running_high
            floor = running_low
            roof_zone = roof * TOUCH_ZONE_PCT
            floor_zone = floor * TOUCH_ZONE_PCT

            roof_touches = 0
            floor_touches = 0
            for k in range(box_start, box_end + 1):
                if abs(bars[k]["h"] - roof) <= roof_zone:
                    roof_touches += 1
                if abs(bars[k]["l"] - floor) <= floor_zone:
                    floor_touches += 1

            if roof_touches >= MIN_TOUCHES_ROOF and floor_touches >= MIN_TOUCHES_FLOOR:
                midpoint = (roof + floor) / 2
                range_pct = (roof - floor) / midpoint if midpoint > 0 else 0

                # Check if box is still active (last bar is within the box)
                last_bar = bars[-1]
                still_active = (last_bar["l"] >= floor * (1 - TOUCH_ZONE_PCT) and
                               last_bar["h"] <= roof * (1 + TOUCH_ZONE_PCT))

                # Determine if box was broken and which direction
                broken = False
                break_direction = None
                break_bar_idx = None
                if not still_active and box_end < n - 1:
                    for k in range(box_end + 1, n):
                        if bars[k]["c"] > roof * (1 + TOUCH_ZONE_PCT):
                            broken = True
                            break_direction = "up"
                            break_bar_idx = k
                            break
                        elif bars[k]["c"] < floor * (1 - TOUCH_ZONE_PCT):
                            broken = True
                            break_direction = "down"
                            break_bar_idx = k
                            break

                # Calculate how far price ran after breakout
                run_distance = 0
                if broken and break_bar_idx is not None:
                    if break_direction == "up":
                        post_high = max(b["h"] for b in bars[break_bar_idx:])
                        run_distance = post_high - roof
                    else:
                        post_low = min(b["l"] for b in bars[break_bar_idx:])
                        run_distance = floor - post_low

                box = {
                    "ticker": ticker.upper(),
                    "roof": round(roof, 2),
                    "floor": round(floor, 2),
                    "midpoint": round(midpoint, 2),
                    "range_pct": round(range_pct * 100, 2),
                    "duration_bars": box_bars,
                    "roof_touches": roof_touches,
                    "floor_touches": floor_touches,
                    "start_idx": box_start,
                    "end_idx": box_end,
                    "start_date": str(bars[box_start]["date"])[:10],
                    "end_date": str(bars[box_end]["date"])[:10],
                    "active": still_active,
                    "broken": broken,
                    "break_direction": break_direction,
                    "run_distance": round(run_distance, 2),
                    "run_pct": round(run_distance / midpoint * 100, 2) if midpoint > 0 else 0,
                }
                boxes.append(box)

            # Skip past this box to find the next one
            i = box_end + 1
        else:
            i += 1

    return boxes


# ═══════════════════════════════════════════════════════════
# VOID (EMPTY SPACE) DETECTION
# ═══════════════════════════════════════════════════════════

def detect_voids(bars: List[dict], boxes: List[dict],
                 ticker: str) -> List[dict]:
    """
    Detect empty price zones — areas where price passed through
    quickly without consolidating. Low bar density = void.

    Returns list of void dicts with location relative to nearest box.
    """
    if not bars or len(bars) < 20:
        return []

    spot = bars[-1]["c"]
    if spot <= 0:
        return []

    # Build a price density map
    # Divide the price range into buckets and count how many candle bodies
    # overlap each bucket
    all_highs = [b["h"] for b in bars]
    all_lows = [b["l"] for b in bars]
    price_max = max(all_highs)
    price_min = min(all_lows)
    price_range = price_max - price_min
    if price_range <= 0:
        return []

    # Bucket size: 0.5% of current price
    bucket_size = spot * 0.005
    num_buckets = int(price_range / bucket_size) + 1
    if num_buckets > 500:
        bucket_size = price_range / 500
        num_buckets = 500

    density = [0] * num_buckets

    for bar in bars:
        body_high = max(bar["o"], bar["c"])
        body_low = min(bar["o"], bar["c"])
        # Count candle body overlap with each bucket
        for bi in range(num_buckets):
            bucket_low = price_min + bi * bucket_size
            bucket_high = bucket_low + bucket_size
            if body_high >= bucket_low and body_low <= bucket_high:
                density[bi] += 1

    # Find contiguous low-density zones (voids)
    voids = []
    in_void = False
    void_start_bucket = 0

    for bi in range(num_buckets):
        if density[bi] <= MAX_VOID_BAR_DENSITY:
            if not in_void:
                in_void = True
                void_start_bucket = bi
        else:
            if in_void:
                # Void ended
                void_low = price_min + void_start_bucket * bucket_size
                void_high = price_min + bi * bucket_size
                void_height = void_high - void_low
                void_pct = void_height / spot if spot > 0 else 0

                if void_pct >= MIN_VOID_PCT:
                    # Determine position relative to spot and boxes
                    position = "above" if void_low > spot else "below"
                    avg_density = sum(density[void_start_bucket:bi]) / max(1, bi - void_start_bucket)

                    # Find nearest box boundary
                    nearest_box = None
                    for box in boxes:
                        if position == "above" and abs(box["roof"] - void_low) / spot < 0.02:
                            nearest_box = box
                            break
                        elif position == "below" and abs(box["floor"] - void_high) / spot < 0.02:
                            nearest_box = box
                            break

                    voids.append({
                        "ticker": ticker.upper(),
                        "low": round(void_low, 2),
                        "high": round(void_high, 2),
                        "height": round(void_height, 2),
                        "height_pct": round(void_pct * 100, 2),
                        "position": position,
                        "avg_density": round(avg_density, 1),
                        "adjacent_to_box": nearest_box is not None,
                        "adjacent_box_roof": nearest_box["roof"] if nearest_box else None,
                        "adjacent_box_floor": nearest_box["floor"] if nearest_box else None,
                    })

                in_void = False

    # Check if we ended in a void
    if in_void:
        void_low = price_min + void_start_bucket * bucket_size
        void_high = price_min + num_buckets * bucket_size
        void_height = void_high - void_low
        void_pct = void_height / spot if spot > 0 else 0
        if void_pct >= MIN_VOID_PCT:
            position = "above" if void_low > spot else "below"
            voids.append({
                "ticker": ticker.upper(),
                "low": round(void_low, 2),
                "high": round(void_high, 2),
                "height": round(void_height, 2),
                "height_pct": round(void_pct * 100, 2),
                "position": position,
                "avg_density": 0,
                "adjacent_to_box": False,
                "adjacent_box_roof": None,
                "adjacent_box_floor": None,
            })

    return voids


# ═══════════════════════════════════════════════════════════
# BOX MATURITY ANALYSIS
# ═══════════════════════════════════════════════════════════

def classify_maturity(box: dict, historical_avg_duration: float) -> dict:
    """
    Classify box maturity based on current duration vs historical average.
    Returns maturity label and suggested DTE.
    """
    duration = box.get("duration_bars", 0)
    avg = historical_avg_duration if historical_avg_duration > 0 else 15

    ratio = duration / avg if avg > 0 else 1.0

    if ratio < MATURITY_EARLY_PCT:
        label = "early"
        note = f"Box is young ({duration} bars, avg {avg:.0f}). Watchlist only."
    elif ratio < MATURITY_MID_PCT:
        label = "mid"
        note = f"Box maturing ({duration} bars, avg {avg:.0f}). Position entry window."
    elif ratio < MATURITY_LATE_PCT:
        label = "late"
        note = f"Box mature ({duration} bars, avg {avg:.0f}). Breakout probable."
    else:
        label = "overdue"
        note = f"Box overdue ({duration} bars, avg {avg:.0f}). Breakout imminent."

    suggested_dte = DTE_MAP.get(label)

    return {
        "maturity": label,
        "maturity_ratio": round(ratio, 2),
        "duration_bars": duration,
        "historical_avg": round(avg, 1),
        "suggested_dte": suggested_dte,
        "note": note,
    }


# ═══════════════════════════════════════════════════════════
# STRIKE SELECTION FROM CHAIN
# ═══════════════════════════════════════════════════════════

def select_strikes(chain_data: dict, spot: float, direction: str,
                   box: dict, void_above: dict, void_below: dict,
                   target_dte: int) -> Optional[dict]:
    """
    Select specific strikes from MarketData chain for a Potter Box trade.

    Direction determines the 4:1 ratio:
      bullish: 4 long calls (strikes inside void above) + 1 long put (at/below floor)
      bearish: 4 long puts (strikes inside void below) + 1 long call (at/above roof)

    Returns trade structure with real bid/ask/IV from chain.
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return None

    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes = col("strike", None)
    sides = col("side", "")
    bids = col("bid", 0)
    asks = col("ask", 0)
    mids = col("mid", 0)
    ivs = col("iv", 0)
    deltas = col("delta", 0)
    ois = col("openInterest", 0)
    volumes = col("volume", 0)
    dtes = col("dte", 0)

    # Build contract lookup
    contracts = []
    for i in range(n):
        if strikes[i] is None:
            continue
        contracts.append({
            "symbol": sym_list[i],
            "strike": float(strikes[i]),
            "side": str(sides[i] or "").lower(),
            "bid": float(bids[i] or 0),
            "ask": float(asks[i] or 0),
            "mid": float(mids[i] or 0),
            "iv": float(ivs[i] or 0),
            "delta": float(deltas[i] or 0),
            "oi": int(ois[i] or 0),
            "volume": int(volumes[i] or 0),
            "dte": int(dtes[i] or 0),
        })

    calls = sorted([c for c in contracts if c["side"] == "call"],
                   key=lambda c: c["strike"])
    puts = sorted([c for c in contracts if c["side"] == "put"],
                  key=lambda c: c["strike"])

    if not calls or not puts:
        return None

    roof = box["roof"]
    floor = box["floor"]

    if direction == "bullish":
        # Primary: 4 long calls inside the void above
        # Target strikes: just above the roof, inside the void
        void = void_above
        if not void:
            return None

        void_low = void["low"]
        void_high = void["high"]
        void_mid = (void_low + void_high) / 2

        # Find 1 call strike just above roof (entry) and 1 deeper in void (runner)
        primary_candidates = [c for c in calls
                              if roof * 0.99 <= c["strike"] <= void_mid
                              and c["mid"] >= 0.10
                              and c["ask"] > 0]
        if not primary_candidates:
            # Fallback: any call above spot
            primary_candidates = [c for c in calls
                                  if c["strike"] > spot
                                  and c["mid"] >= 0.10
                                  and c["ask"] > 0]

        if not primary_candidates:
            return None

        # Pick strike closest to 30 delta (good risk/reward for directional)
        primary_candidates.sort(key=lambda c: abs(abs(c["delta"]) - 0.30))
        primary_strike = primary_candidates[0]

        # Hedge: 1 long put at or below the floor
        hedge_candidates = [p for p in puts
                           if p["strike"] <= floor * 1.01
                           and p["mid"] >= 0.05
                           and p["ask"] > 0]
        if not hedge_candidates:
            hedge_candidates = [p for p in puts
                               if p["strike"] < spot
                               and p["mid"] >= 0.05
                               and p["ask"] > 0]

        if not hedge_candidates:
            return None

        # Pick put closest to 20 delta (cheap hedge)
        hedge_candidates.sort(key=lambda c: abs(abs(c["delta"]) - 0.20))
        hedge_strike = hedge_candidates[0]

        # Target: top of the void
        target_price = void_high

    else:  # bearish
        # Primary: 4 long puts inside the void below
        void = void_below
        if not void:
            return None

        void_low = void["low"]
        void_high = void["high"]
        void_mid = (void_low + void_high) / 2

        primary_candidates = [p for p in puts
                              if void_mid <= p["strike"] <= floor * 1.01
                              and p["mid"] >= 0.10
                              and p["ask"] > 0]
        if not primary_candidates:
            primary_candidates = [p for p in puts
                                  if p["strike"] < spot
                                  and p["mid"] >= 0.10
                                  and p["ask"] > 0]

        if not primary_candidates:
            return None

        primary_candidates.sort(key=lambda c: abs(abs(c["delta"]) - 0.30))
        primary_strike = primary_candidates[0]

        # Hedge: 1 long call at or above the roof
        hedge_candidates = [c for c in calls
                           if c["strike"] >= roof * 0.99
                           and c["mid"] >= 0.05
                           and c["ask"] > 0]
        if not hedge_candidates:
            hedge_candidates = [c for c in calls
                               if c["strike"] > spot
                               and c["mid"] >= 0.05
                               and c["ask"] > 0]

        if not hedge_candidates:
            return None

        hedge_candidates.sort(key=lambda c: abs(abs(c["delta"]) - 0.20))
        hedge_strike = hedge_candidates[0]

        target_price = void_low

    # Calculate total cost
    primary_cost = primary_strike["ask"] * 4 * 100  # 4 contracts
    hedge_cost = hedge_strike["ask"] * 1 * 100      # 1 contract
    total_cost = primary_cost + hedge_cost
    max_loss = total_cost  # defined risk — can't lose more than premium paid

    # Estimate profit at target
    if direction == "bullish":
        intrinsic_at_target = max(0, target_price - primary_strike["strike"])
        primary_value_at_target = intrinsic_at_target * 4 * 100
    else:
        intrinsic_at_target = max(0, primary_strike["strike"] - target_price)
        primary_value_at_target = intrinsic_at_target * 4 * 100

    profit_at_target = primary_value_at_target - total_cost
    reward_risk = profit_at_target / total_cost if total_cost > 0 else 0

    return {
        "direction": direction,
        "structure": f"4:1 {'call' if direction == 'bullish' else 'put'}/{'put' if direction == 'bullish' else 'call'}",
        "primary": {
            "side": "call" if direction == "bullish" else "put",
            "strike": primary_strike["strike"],
            "symbol": primary_strike["symbol"],
            "qty": 4,
            "ask": primary_strike["ask"],
            "bid": primary_strike["bid"],
            "mid": primary_strike["mid"],
            "iv": round(primary_strike["iv"] * 100, 1) if primary_strike["iv"] < 5 else round(primary_strike["iv"], 1),
            "delta": round(primary_strike["delta"], 3),
            "oi": primary_strike["oi"],
            "dte": primary_strike["dte"],
        },
        "hedge": {
            "side": "put" if direction == "bullish" else "call",
            "strike": hedge_strike["strike"],
            "symbol": hedge_strike["symbol"],
            "qty": 1,
            "ask": hedge_strike["ask"],
            "bid": hedge_strike["bid"],
            "mid": hedge_strike["mid"],
            "iv": round(hedge_strike["iv"] * 100, 1) if hedge_strike["iv"] < 5 else round(hedge_strike["iv"], 1),
            "delta": round(hedge_strike["delta"], 3),
            "oi": hedge_strike["oi"],
            "dte": hedge_strike["dte"],
        },
        "total_cost": round(total_cost, 2),
        "max_loss": round(max_loss, 2),
        "target_price": round(target_price, 2),
        "profit_at_target": round(profit_at_target, 2),
        "reward_risk": round(reward_risk, 2),
    }


# ═══════════════════════════════════════════════════════════
# POTTER BOX SCANNER
# ═══════════════════════════════════════════════════════════

class PotterBoxScanner:
    """
    Main scanner class. Detects boxes, maps voids, checks flow,
    selects strikes, and generates trade alerts.
    """

    def __init__(self, persistent_state, flow_detector=None,
                 post_fn: Callable = None):
        self._state = persistent_state
        self._flow = flow_detector
        self._post = post_fn

    # ── Redis keys ──

    def _active_key(self, ticker: str) -> str:
        return f"potter_box:active:{ticker.upper()}"

    def _history_key(self, ticker: str, date_str: str) -> str:
        return f"potter_box:history:{ticker.upper()}:{date_str}"

    def _void_key(self, ticker: str) -> str:
        return f"potter_box:void:{ticker.upper()}"

    def _alert_key(self, ticker: str, roof: float) -> str:
        return f"potter_box:alert:{ticker.upper()}:{roof}"

    def _defaults_key(self, ticker: str) -> str:
        return f"potter_box:avg_duration:{ticker.upper()}"

    # ── Persistence ──

    def _save_active_box(self, ticker: str, box: dict):
        self._state._json_set(self._active_key(ticker), box, TTL_ACTIVE_BOX)

    def get_active_box(self, ticker: str) -> Optional[dict]:
        return self._state._json_get(self._active_key(ticker))

    def _save_void_map(self, ticker: str, voids: list):
        self._state._json_set(self._void_key(ticker), voids, TTL_VOID_MAP)

    def get_void_map(self, ticker: str) -> list:
        return self._state._json_get(self._void_key(ticker)) or []

    def _log_completed_box(self, ticker: str, box: dict):
        """Log a completed (broken) box to history for duration averaging."""
        today = date.today().isoformat()
        key = self._history_key(ticker, today)
        self._state._json_set(key, box, TTL_HISTORY_BOX)
        # Update running average
        self._update_avg_duration(ticker, box["duration_bars"])

    def _update_avg_duration(self, ticker: str, new_duration: int):
        """Update the running average box duration for a ticker."""
        key = self._defaults_key(ticker)
        existing = self._state._json_get(key)
        if existing:
            count = existing.get("count", 0) + 1
            total = existing.get("total", 0) + new_duration
        else:
            count = 1
            total = new_duration
        avg = total / count if count > 0 else DEFAULT_DURATION.get(_get_tier(ticker), 15)
        self._state._json_set(key, {
            "count": count, "total": total, "avg": round(avg, 1),
        }, TTL_DEFAULTS)

    def get_avg_duration(self, ticker: str) -> float:
        """Get historical average box duration for a ticker."""
        key = self._defaults_key(ticker)
        data = self._state._json_get(key)
        if data and data.get("count", 0) >= 2:
            return data["avg"]
        return DEFAULT_DURATION.get(_get_tier(ticker), 15)

    # ── Core Scan ──

    def scan_ticker(self, ticker: str, bars: List[dict],
                    chain_fn: Callable = None,
                    spot_fn: Callable = None,
                    expirations_fn: Callable = None,
                    iv_percentile_fn: Callable = None) -> Optional[dict]:
        """
        Full Potter Box analysis for one ticker.
        Returns setup dict if actionable, None otherwise.
        """
        ticker = ticker.upper()
        if not bars or len(bars) < 30:
            return None

        spot = bars[-1]["c"]
        if spot <= 0:
            return None

        # Detect all boxes
        all_boxes = detect_boxes(bars, ticker)
        if not all_boxes:
            return None

        # Find the active box (current consolidation)
        active_boxes = [b for b in all_boxes if b["active"]]
        completed_boxes = [b for b in all_boxes if b["broken"]]

        # Log any newly completed boxes to history
        for cb in completed_boxes:
            try:
                self._log_completed_box(ticker, cb)
            except Exception:
                pass

        if not active_boxes:
            # No active consolidation — save void map for reference
            voids = detect_voids(bars, all_boxes, ticker)
            self._save_void_map(ticker, voids)
            return None

        # Use the most recent active box
        box = active_boxes[-1]
        self._save_active_box(ticker, box)

        # Detect voids
        voids = detect_voids(bars, all_boxes, ticker)
        self._save_void_map(ticker, voids)

        # Find voids adjacent to the active box
        void_above = None
        void_below = None
        for v in voids:
            if v["position"] == "above" and v.get("adjacent_to_box"):
                if void_above is None or v["height_pct"] > void_above["height_pct"]:
                    void_above = v
            elif v["position"] == "below" and v.get("adjacent_to_box"):
                if void_below is None or v["height_pct"] > void_below["height_pct"]:
                    void_below = v

        # Also check non-adjacent voids if nothing adjacent found
        if void_above is None:
            above_voids = [v for v in voids if v["position"] == "above"]
            if above_voids:
                void_above = max(above_voids, key=lambda v: v["height_pct"])
        if void_below is None:
            below_voids = [v for v in voids if v["position"] == "below"]
            if below_voids:
                void_below = max(below_voids, key=lambda v: v["height_pct"])

        has_void = void_above is not None or void_below is not None
        if not has_void:
            return None  # no empty space to trade into

        # Maturity analysis
        avg_duration = self.get_avg_duration(ticker)
        maturity = classify_maturity(box, avg_duration)

        if maturity["maturity"] == "early":
            # Too early — note the box but don't alert
            log.debug(f"Potter Box {ticker}: active box detected but too early "
                     f"({box['duration_bars']} bars, avg {avg_duration:.0f})")
            return None

        # Check flow bias
        flow_direction = None
        flow_context = {}
        if self._flow:
            try:
                campaigns = self._state.get_all_flow_campaigns(ticker)
                for c in campaigns:
                    strike = c.get("strike", 0)
                    # Flow near the roof = bullish breakout bias
                    if abs(strike - box["roof"]) / spot < 0.03:
                        if c.get("side") == "call" and "buildup" in c.get("flow_type", ""):
                            flow_direction = "bullish"
                            flow_context = c
                            break
                        elif c.get("side") == "put" and "unwinding" in c.get("flow_type", ""):
                            flow_direction = "bullish"
                            flow_context = c
                            break
                    # Flow near the floor = bearish breakdown bias
                    if abs(strike - box["floor"]) / spot < 0.03:
                        if c.get("side") == "put" and "buildup" in c.get("flow_type", ""):
                            flow_direction = "bearish"
                            flow_context = c
                            break
                        elif c.get("side") == "call" and "unwinding" in c.get("flow_type", ""):
                            flow_direction = "bearish"
                            flow_context = c
                            break
            except Exception:
                pass

        # Check IV percentile if available
        iv_pct = None
        if iv_percentile_fn:
            try:
                iv_pct = iv_percentile_fn(ticker)
            except Exception:
                pass

        # Build setup result
        setup = {
            "ticker": ticker,
            "box": box,
            "void_above": void_above,
            "void_below": void_below,
            "maturity": maturity,
            "flow_direction": flow_direction,
            "flow_context": flow_context,
            "iv_percentile": iv_pct,
            "spot": round(spot, 2),
            "scan_time": datetime.now().isoformat(),
        }

        # Select strikes if we have chain access and directional bias
        if flow_direction and chain_fn and expirations_fn:
            target_dte = maturity.get("suggested_dte", 21)
            void_for_direction = void_above if flow_direction == "bullish" else void_below

            if void_for_direction and target_dte:
                try:
                    # Find best expiration matching target DTE
                    exps = expirations_fn(ticker) or []
                    today = date.today()
                    best_exp = None
                    best_dte_diff = 999

                    for exp in exps:
                        try:
                            exp_date = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
                            dte = (exp_date - today).days
                            if dte < 7:
                                continue  # skip near-term
                            diff = abs(dte - target_dte)
                            if diff < best_dte_diff:
                                best_dte_diff = diff
                                best_exp = exp
                        except (ValueError, TypeError):
                            continue

                    if best_exp:
                        chain = chain_fn(ticker, best_exp)
                        if isinstance(chain, dict) and chain.get("s") == "ok":
                            trade = select_strikes(
                                chain, spot, flow_direction, box,
                                void_above, void_below, target_dte,
                            )
                            if trade:
                                setup["trade"] = trade
                                setup["expiry"] = best_exp

                except Exception as e:
                    log.debug(f"Potter Box strike selection failed for {ticker}: {e}")

        return setup

    # ── Full Scan ──

    def scan_all(self, tickers: list, ohlcv_fn: Callable,
                 chain_fn: Callable = None,
                 spot_fn: Callable = None,
                 expirations_fn: Callable = None,
                 iv_percentile_fn: Callable = None) -> List[dict]:
        """Scan all tickers for Potter Box setups."""
        setups = []

        for ticker in tickers:
            try:
                bars = ohlcv_fn(ticker)
                if not bars:
                    continue

                # Convert OHLCV dict format to bar list if needed
                if isinstance(bars, dict) and "close" in bars:
                    bar_list = []
                    closes = bars.get("close", [])
                    opens = bars.get("open", closes)
                    highs = bars.get("high", closes)
                    lows = bars.get("low", closes)
                    vols = bars.get("volume", [0] * len(closes))
                    for i in range(len(closes)):
                        bar_list.append({
                            "date": "",
                            "o": opens[i], "h": highs[i],
                            "l": lows[i], "c": closes[i],
                            "v": vols[i] if i < len(vols) else 0,
                        })
                    bars = bar_list

                setup = self.scan_ticker(
                    ticker, bars,
                    chain_fn=chain_fn,
                    spot_fn=spot_fn,
                    expirations_fn=expirations_fn,
                    iv_percentile_fn=iv_percentile_fn,
                )
                if setup:
                    setups.append(setup)

            except Exception as e:
                log.debug(f"Potter Box scan error for {ticker}: {e}")

        log.info(f"Potter Box scan complete: {len(setups)} setups from {len(tickers)} tickers")
        return setups

    # ── Formatting ──

    def format_alert(self, setup: dict) -> str:
        """Format a Potter Box setup as Telegram alert."""
        ticker = setup["ticker"]
        box = setup["box"]
        mat = setup["maturity"]
        spot = setup["spot"]
        va = setup.get("void_above")
        vb = setup.get("void_below")
        flow_dir = setup.get("flow_direction")
        flow_ctx = setup.get("flow_context", {})
        iv_pct = setup.get("iv_percentile")
        trade = setup.get("trade")

        mat_emoji = {"mid": "📅", "late": "⏰", "overdue": "🚨"}.get(mat["maturity"], "📅")

        lines = [
            f"📦 POTTER BOX — {ticker}",
            "━" * 28,
            f"Box: ${box['floor']:.2f} (floor) – ${box['roof']:.2f} (roof)",
            f"50% Cost Basis: ${box['midpoint']:.2f}",
            f"Range: {box['range_pct']:.1f}% | "
            f"Touches: {box['roof_touches']}R / {box['floor_touches']}F",
            f"{mat_emoji} Duration: {box['duration_bars']} bars "
            f"(avg {mat['historical_avg']:.0f} — {mat['maturity'].upper()})",
        ]

        if va:
            lines.append(f"⬆️ Void Above: ${va['low']:.2f} → ${va['high']:.2f} "
                        f"(${va['height']:.2f}, {va['height_pct']:.1f}%)")
        if vb:
            lines.append(f"⬇️ Void Below: ${vb['high']:.2f} → ${vb['low']:.2f} "
                        f"(${vb['height']:.2f}, {vb['height_pct']:.1f}%)")

        if flow_dir:
            camp_days = flow_ctx.get("consecutive_days", 0)
            camp_oi = flow_ctx.get("total_oi_change", 0)
            lines.append("")
            lines.append(f"🏛️ Flow: {flow_dir.upper()} "
                        f"({camp_days}D campaign, {camp_oi:+,} OI)")

        if iv_pct is not None:
            iv_tag = "compressed ✅" if iv_pct <= IV_PERCENTILE_MAX else "elevated ⚠️"
            lines.append(f"📊 IV Percentile: {iv_pct:.0f}% ({iv_tag})")

        if trade:
            p = trade["primary"]
            h = trade["hedge"]
            lines.append("")
            lines.append(f"{'🟢' if flow_dir == 'bullish' else '🔴'} "
                        f"Suggested: {trade['structure']}")
            lines.append(f"  📗 {p['qty']}x ${p['strike']:.0f} {p['side'].upper()} "
                        f"@ ${p['ask']:.2f} (δ{p['delta']:.2f}, IV {p['iv']:.0f}%)")
            lines.append(f"  📕 {h['qty']}x ${h['strike']:.0f} {h['side'].upper()} "
                        f"@ ${h['ask']:.2f} (δ{h['delta']:.2f}, IV {h['iv']:.0f}%)")
            lines.append(f"  Exp: {setup.get('expiry', '?')} "
                        f"({p['dte']}D)")
            lines.append(f"  Total cost: ${trade['total_cost']:.2f} | "
                        f"Max loss: ${trade['max_loss']:.2f}")
            lines.append(f"  Target: ${trade['target_price']:.2f} | "
                        f"Profit at target: ${trade['profit_at_target']:.2f} "
                        f"({trade['reward_risk']:.1f}:1 R/R)")
        elif flow_dir:
            lines.append("")
            lines.append(f"Direction: {flow_dir.upper()} — awaiting chain data for strikes")
        else:
            lines.append("")
            lines.append("No flow bias yet — watchlist only")
            lines.append("Waiting for institutional positioning at box boundary")

        return "\n".join(lines)

    def format_summary(self, setups: List[dict]) -> str:
        """Format multiple setups as summary."""
        if not setups:
            return ""
        lines = [
            f"📦 POTTER BOX SCAN — {len(setups)} setups",
            "━" * 28,
        ]
        for s in setups:
            box = s["box"]
            mat = s["maturity"]
            flow = s.get("flow_direction", "none")
            trade = s.get("trade")
            mat_emoji = {"mid": "📅", "late": "⏰", "overdue": "🚨"}.get(mat["maturity"], "📅")
            flow_tag = f"{'🟢' if flow == 'bullish' else '🔴'} {flow.upper()}" if flow else "⚪ no bias"
            trade_tag = f" | ${trade['total_cost']:.0f} cost, {trade['reward_risk']:.1f}:1" if trade else ""
            lines.append(
                f"  {mat_emoji} {s['ticker']} ${box['floor']:.0f}–${box['roof']:.0f} "
                f"({box['duration_bars']}D {mat['maturity']}) | "
                f"{flow_tag}{trade_tag}"
            )
        return "\n".join(lines)
