# oi_flow.py
# ═══════════════════════════════════════════════════════════════════
# Unified Institutional Flow Detection Layer
#
# Two-phase flow detection:
#   Phase 1 (Intraday): Volume/OI ratio + volume bursts + direction approx
#   Phase 2 (Morning):  OI confirmation → Confirmed Buildup/Unwinding/Churn
#
# Signal hierarchy:
#   Notable (0.5-1x vol/OI)     → Log only, score modifier +3 to +5
#   Significant (1-2x vol/OI)   → Telegram alert, score boost +5 to +8
#   Extreme (2x+ vol/OI)        → Trade generation, always show income idea
#   Confirmed (morning OI delta) → Stalk alert, highest conviction scoring
#
# All state persisted to Redis via PersistentState — survives redeploy.
# Zero additional API credits for piggyback pulls.
# Forward sweeps use cached mode = 1 credit each.
# ═══════════════════════════════════════════════════════════════════

import logging
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable, Tuple

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

# Volume thresholds by liquidity tier (3x from initial conservative values)
VOLUME_TIERS = {
    "index":    {"tickers": {"SPY", "QQQ", "IWM", "DIA"}, "min_volume": 15000},
    "mega_cap": {"tickers": {"AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL"},
                 "min_volume": 6000},
    "large_cap": {"tickers": {"AMD", "AVGO", "NFLX", "CRM", "BA", "LLY", "UNH",
                               "JPM", "GS", "CAT", "ORCL", "ARM"},
                  "min_volume": 3000},
    "mid_cap":  {"tickers": set(), "min_volume": 1500},  # everything else
}

# Vol/OI ratio classification
VOL_OI_NOTABLE = 0.5       # 50% turnover
VOL_OI_SIGNIFICANT = 1.0   # 100% turnover
VOL_OI_EXTREME = 2.0       # 200% turnover

# Filter parameters
MAX_DIST_FROM_SPOT_PCT = 0.10   # Only strikes within 10% of spot
MIN_OPTION_MID_PRICE = 0.10     # Skip penny options
VOLUME_BURST_THRESHOLD = 1000   # Contracts added in single 5-min pull

# Alert cooldowns (seconds)
ALERT_COOLDOWN_NOTABLE = 0          # No alerts for notable
ALERT_COOLDOWN_SIGNIFICANT = 1800   # 30 min
ALERT_COOLDOWN_EXTREME = 300        # 5 min (urgent)

# Campaign thresholds
CAMPAIGN_MIN_DAYS = 2              # Minimum for "persistent" label
CAMPAIGN_STRONG_DAYS = 3           # "Institutional campaign" label

# Trade generation thresholds
TRADE_GEN_MIN_VOL_OI = 2.0         # Extreme tier
TRADE_GEN_MIN_VOLUME_MULTIPLIER = 2  # 2x the tier minimum

# ── CONVICTION PLAY — tiered institutional flow signal ──
# The ONLY flow that fires Telegram alerts (everything else silent).
# Routes by DTE: immediate (0-2), income (3-7), swing (8-30), stalk (30-60)
CONVICTION_MIN_VOL_OI = 10.0        # 10x turnover minimum
CONVICTION_MIN_VOLUME_MULT = 5      # 5x tier minimum volume
CONVICTION_MIN_BURST = 5000         # burst path: 5K+ contracts in one interval
                                    # OR cumulative path: 15x+ vol/OI (no burst needed)
CONVICTION_COOLDOWN = 300           # 5 min between conviction alerts per ticker per tier
                                    # (was 3600 — 1 hour blocked everything after deploy)

# FIX BUG #11-lite: minimum time a posted conviction must be held before a
# direction-flip can fire as an EXIT signal. Prevents "you're on the wrong side"
# alerts from hitting within minutes of the original entry — observed live as
# 5-9 min flip-to-exit which is faster than a user can act on the entry.
# Set from handoff discussion: today's reversal fired at 9 min, user estimates
# 5-7 min from entry → 15 min gives comfortable buffer for trade execution.
CONVICTION_EXIT_MIN_HOLD_SEC = 15 * 60  # 15 minutes

# v8.5 (Phase 2): Conviction quality gates
CONVICTION_NOTIONAL_TIER1 = 10_000_000   # index + mega_cap: $10M premium floor
CONVICTION_NOTIONAL_TIER2 =  5_000_000   # everything else:  $5M floor
CONVICTION_MIN_OI = {                     # min OI before vol/OI ratio is trusted
    "index":     2000,
    "mega_cap":  1000,
    "large_cap":  500,
    "mid_cap":    250,
}
TIER1_NOTIONAL_TICKERS = (VOLUME_TIERS["index"]["tickers"]
                          | VOLUME_TIERS["mega_cap"]["tickers"])
CONVICTION_EXIT_COOLDOWN = 20 * 60       # Bug #4: suppress exit re-fires for 20 min

# Scoring impact
SCORE_NOTABLE_ALIGNED = 3
SCORE_NOTABLE_OPPOSING = -2
SCORE_SIGNIFICANT_ALIGNED = 6
SCORE_SIGNIFICANT_OPPOSING = -5
SCORE_EXTREME_ALIGNED = 10
SCORE_EXTREME_OPPOSING = -8
SCORE_CONFIRMED_ALIGNED = 10
SCORE_CONFIRMED_OPPOSING = -10
SCORE_CAMPAIGN_BONUS = 5           # Per consecutive day (max +15)

# Validator boost (can lift 2→3 but not 1→3)
VALIDATOR_BOOST_SIGNIFICANT = 0.8
VALIDATOR_BOOST_EXTREME = 1.2
VALIDATOR_BOOST_CONFIRMED = 1.5

# Full ticker list for flow tracking (union of all scanners)
FLOW_TICKERS = sorted({
    # Indexes
    "SPY", "QQQ", "IWM", "DIA",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "TSLA", "GOOGL",
    # Large-cap
    "NFLX", "COIN", "AVGO", "PLTR", "CRM", "ORCL", "ARM", "SMCI",
    # Financials
    "JPM", "GS",
    # Industrials / Health / Retail
    "BA", "CAT", "LLY", "UNH",
    # Income scanner tickers
    "MRNA",
    # Macro / Sector ETFs
    "GLD", "TLT", "XLF", "XLE", "XLV", "SOXX",
    # Additional from active scanner
    "MSTR", "SOFI",
})

# Sector map for sector flow aggregation
SECTOR_MAP = {
    "SEMICONDUCTOR": {"NVDA", "AMD", "AVGO", "ARM", "SMCI", "SOXX"},
    "BIG_TECH": {"AAPL", "MSFT", "AMZN", "META", "GOOGL"},
    "FINANCIALS": {"JPM", "GS", "XLF", "COIN", "SOFI"},
    "ENERGY": {"XLE"},
    "HEALTH": {"LLY", "UNH", "MRNA", "XLV"},
    "INDUSTRIAL": {"BA", "CAT"},
    "INDEX": {"SPY", "QQQ", "IWM", "DIA"},
    "MACRO": {"GLD", "TLT"},
}


def _get_volume_tier(ticker: str) -> dict:
    """Get volume tier config for a ticker."""
    t = ticker.upper()
    for tier_name, tier_cfg in VOLUME_TIERS.items():
        if t in tier_cfg["tickers"]:
            return tier_cfg
    return VOLUME_TIERS["mid_cap"]


def _get_sector(ticker: str) -> str:
    """Map ticker to sector."""
    t = ticker.upper()
    for sector, tickers in SECTOR_MAP.items():
        if t in tickers:
            return sector
    return "OTHER"


# ═══════════════════════════════════════════════════════════
# CHAIN DATA PARSING
# ═══════════════════════════════════════════════════════════

def parse_chain_volume_oi(chain_data: dict, spot: float) -> List[dict]:
    """
    Extract per-strike volume, OI, and direction approximation from
    a MarketData chain response.

    Returns list of dicts, one per strike/side:
      {strike, side, volume, oi, vol_oi_ratio, last, mid, bid, ask,
       bidSize, askSize, direction_approx, dist_from_spot_pct}
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return []

    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes = col("strike", None)
    sides = col("side", "")
    volumes = col("volume", 0)
    ois = col("openInterest", 0)
    lasts = col("last", 0)
    mids = col("mid", 0)
    bids = col("bid", 0)
    asks = col("ask", 0)
    bid_sizes = col("bidSize", 0)
    ask_sizes = col("askSize", 0)

    results = []
    for i in range(n):
        strike = strikes[i]
        side = str(sides[i] or "").lower()
        if strike is None or side not in ("call", "put"):
            continue

        vol = int(volumes[i] or 0)
        oi = int(ois[i] or 0)
        last = float(lasts[i] or 0)
        mid = float(mids[i] or 0)
        bid = float(bids[i] or 0)
        ask = float(asks[i] or 0)
        bid_sz = int(bid_sizes[i] or 0)
        ask_sz = int(ask_sizes[i] or 0)

        # Distance filter
        if spot > 0:
            dist_pct = abs(strike - spot) / spot
            if dist_pct > MAX_DIST_FROM_SPOT_PCT:
                continue
        else:
            dist_pct = 0

        # Mid price filter
        if mid < MIN_OPTION_MID_PRICE and last < MIN_OPTION_MID_PRICE:
            continue

        # Vol/OI ratio
        vol_oi = vol / oi if oi > 0 else (999.0 if vol > 0 else 0)

        # ── v7.0: Enhanced direction inference ──────────────────────
        # With 60s continuous scanning (Schwab), these signals are now
        # real-time instead of 15-min delayed. Each signal gets a
        # confidence weight; the composite determines trade side.
        #
        # Signal 1: Last vs Ask/Bid (strongest — actual trade execution)
        #   last >= ask  → aggressive BUY (someone paid the full ask)
        #   last <= bid  → aggressive SELL (someone hit the bid)
        #   This was unreliable with 15-min delay; now it's live.
        #
        # Signal 2: Last vs Mid (weaker confirmation)
        #   last > mid   → buying pressure
        #   last < mid   → selling pressure
        #
        # Signal 3: Book imbalance (bid_size vs ask_size)
        #   bid_heavy    → demand (buyers stacking)
        #   ask_heavy    → supply (sellers stacking)
        #
        # Combined: all signals agree = HIGH confidence direction.
        # Signals conflict = LOW confidence (noise/market-maker activity).
        # ────────────────────────────────────────────────────────────

        direction_approx = "unknown"
        direction_confidence = 0.0  # 0.0-1.0

        buy_signals = 0
        sell_signals = 0
        total_signals = 0

        # Signal 1: Last vs Bid/Ask (weight: 3)
        if last > 0 and bid > 0 and ask > 0 and ask > bid:
            spread = ask - bid
            if spread > 0:
                total_signals += 3
                if last >= ask - (spread * 0.1):
                    # Traded at or above ask — aggressive buyer
                    buy_signals += 3
                elif last <= bid + (spread * 0.1):
                    # Traded at or below bid — aggressive seller
                    sell_signals += 3
                elif last > mid:
                    buy_signals += 1
                    total_signals -= 1  # weaker signal, adjust weight
                elif last < mid:
                    sell_signals += 1
                    total_signals -= 1
        elif mid > 0 and last > 0:
            # Fallback when bid/ask unavailable — use mid comparison
            total_signals += 2
            if last > mid * 1.005:
                buy_signals += 2
            elif last < mid * 0.995:
                sell_signals += 2

        # Signal 2: Book imbalance (weight: 1)
        book_imbalance = "balanced"
        if bid_sz > 0 and ask_sz > 0:
            ratio = bid_sz / ask_sz
            if ratio > 2.0:
                book_imbalance = "bid_heavy"
                buy_signals += 1
                total_signals += 1
            elif ratio < 0.5:
                book_imbalance = "ask_heavy"
                sell_signals += 1
                total_signals += 1
            else:
                total_signals += 1  # neutral counts toward total

        # Composite direction
        if total_signals > 0:
            if buy_signals > sell_signals:
                direction_approx = "buyer_initiated"
                direction_confidence = buy_signals / total_signals
            elif sell_signals > buy_signals:
                direction_approx = "seller_initiated"
                direction_confidence = sell_signals / total_signals
            else:
                direction_approx = "neutral"
                direction_confidence = 0.3

        results.append({
            "strike": float(strike),
            "side": side,
            "volume": vol,
            "oi": oi,
            "vol_oi_ratio": round(vol_oi, 2),
            "last": last,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "bidSize": bid_sz,
            "askSize": ask_sz,
            "direction_approx": direction_approx,
            "direction_confidence": round(direction_confidence, 2),
            "book_imbalance": book_imbalance,
            "dist_from_spot_pct": round(dist_pct * 100, 2),
        })

    return results


# ═══════════════════════════════════════════════════════════
# LIGHTWEIGHT GEX CALCULATOR
# Extracts gamma flip + GEX sign from raw chain OI data.
# Zero additional API cost — uses chain data already in hand.
# ═══════════════════════════════════════════════════════════

def estimate_gex_from_chain(chain_data: dict, spot: float) -> dict:
    """
    Lightweight gamma exposure estimate from chain OI data.

    Returns: {gamma_flip, gex_sign, call_wall, put_wall, max_pain}
    - gamma_flip: strike where net dealer gamma changes sign
    - gex_sign: 'positive' (spot above flip) or 'negative' (spot below flip)
    - call_wall: highest call OI strike (resistance magnet)
    - put_wall: highest put OI strike (support magnet)
    - max_pain: strike where total OI (call+put) is highest

    Proxy calculation — not exact greeks, but directionally correct
    for determining whether dealers amplify or dampen moves.
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0 or spot <= 0:
        return {}

    strikes = chain_data.get("strike", [])
    sides = chain_data.get("side", [])
    ois = chain_data.get("openInterest", [])
    if not isinstance(strikes, list):
        strikes = [strikes] * n
    if not isinstance(sides, list):
        sides = [sides] * n
    if not isinstance(ois, list):
        ois = [ois] * n

    # Aggregate OI per strike per side
    call_oi = {}  # strike → total call OI
    put_oi = {}   # strike → total put OI
    for i in range(n):
        strike = strikes[i]
        side = str(sides[i] or "").lower()
        oi = int(ois[i] or 0)
        if strike is None or oi <= 0:
            continue
        strike = float(strike)
        if side == "call":
            call_oi[strike] = call_oi.get(strike, 0) + oi
        elif side == "put":
            put_oi[strike] = put_oi.get(strike, 0) + oi

    if not call_oi and not put_oi:
        return {}

    all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))

    # Call wall: strike with highest call OI
    call_wall = max(call_oi, key=call_oi.get) if call_oi else 0
    # Put wall: strike with highest put OI
    put_wall = max(put_oi, key=put_oi.get) if put_oi else 0
    # Max pain: strike with highest total OI
    total_oi = {s: call_oi.get(s, 0) + put_oi.get(s, 0) for s in all_strikes}
    max_pain = max(total_oi, key=total_oi.get) if total_oi else 0

    # Gamma flip estimate:
    # Net gamma at each strike ≈ call_oi - put_oi (simplified)
    # Flip point = where net gamma crosses zero
    # Below flip = negative gamma (dealers short gamma, amplify moves)
    # Above flip = positive gamma (dealers long gamma, dampen moves)
    gamma_flip = 0
    prev_net = None
    for s in all_strikes:
        net = call_oi.get(s, 0) - put_oi.get(s, 0)
        if prev_net is not None and prev_net < 0 and net >= 0:
            # Zero crossing — interpolate
            gamma_flip = s
            break
        prev_net = net

    # If no crossing found, use midpoint of call/put walls
    if gamma_flip == 0 and call_wall > 0 and put_wall > 0:
        gamma_flip = (call_wall + put_wall) / 2

    # GEX sign: spot above flip = positive gamma
    gex_sign = "positive" if spot >= gamma_flip else "negative"

    return {
        "gamma_flip": round(gamma_flip, 2),
        "gex_sign": gex_sign,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "max_pain": max_pain,
    }


# ═══════════════════════════════════════════════════════════
# IV SKEW CALCULATOR
# Measures the shape of implied volatility across strikes.
# Steepening put skew = institutions paying premium for protection.
# Zero additional API cost — uses IV from chain data already in hand.
# ═══════════════════════════════════════════════════════════

def compute_iv_skew(chain_data: dict, spot: float) -> dict:
    """
    Compute IV skew from chain data.

    Returns: {atm_iv, put_25d_iv, call_25d_iv, skew_ratio, skew_direction, skew_extreme}
    - atm_iv: at-the-money implied volatility
    - put_25d_iv: ~25 delta put IV (OTM put)
    - call_25d_iv: ~25 delta call IV (OTM call)
    - skew_ratio: put_25d_iv / call_25d_iv (>1.2 = steep put skew)
    - skew_direction: 'put_heavy' | 'call_heavy' | 'neutral'
    - skew_extreme: True if ratio > 1.5 (very steep, institutional fear signal)
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0 or spot <= 0:
        return {}

    strikes = chain_data.get("strike", [])
    sides = chain_data.get("side", [])
    ivs = chain_data.get("iv", [])
    deltas = chain_data.get("delta", [])
    if not isinstance(strikes, list): strikes = [strikes] * n
    if not isinstance(sides, list): sides = [sides] * n
    if not isinstance(ivs, list): ivs = [ivs] * n
    if not isinstance(deltas, list): deltas = [deltas] * n

    # Collect IV by delta bucket
    atm_call_iv = []
    atm_put_iv = []
    otm_put_iv = []   # ~25 delta puts
    otm_call_iv = []  # ~25 delta calls

    for i in range(n):
        iv = ivs[i]
        delta = deltas[i]
        side = str(sides[i] or "").lower()
        if iv is None or delta is None:
            continue
        iv = float(iv)
        delta = float(delta)
        if iv <= 0:
            continue

        abs_delta = abs(delta)

        if side == "call":
            if 0.40 <= abs_delta <= 0.60:
                atm_call_iv.append(iv)
            elif 0.20 <= abs_delta <= 0.30:
                otm_call_iv.append(iv)
        elif side == "put":
            if 0.40 <= abs_delta <= 0.60:
                atm_put_iv.append(iv)
            elif 0.20 <= abs_delta <= 0.30:
                otm_put_iv.append(iv)

    if not atm_call_iv and not atm_put_iv:
        return {}

    atm_iv = sum(atm_call_iv + atm_put_iv) / len(atm_call_iv + atm_put_iv)
    put_25d = sum(otm_put_iv) / len(otm_put_iv) if otm_put_iv else atm_iv
    call_25d = sum(otm_call_iv) / len(otm_call_iv) if otm_call_iv else atm_iv

    skew_ratio = put_25d / call_25d if call_25d > 0 else 1.0
    skew_extreme = skew_ratio > 1.5

    if skew_ratio > 1.2:
        skew_direction = "put_heavy"
    elif skew_ratio < 0.8:
        skew_direction = "call_heavy"
    else:
        skew_direction = "neutral"

    return {
        "atm_iv": round(atm_iv, 4),
        "put_25d_iv": round(put_25d, 4),
        "call_25d_iv": round(call_25d, 4),
        "skew_ratio": round(skew_ratio, 3),
        "skew_direction": skew_direction,
        "skew_extreme": skew_extreme,
    }


# ═══════════════════════════════════════════════════════════
# VOLUME PROFILE / VPOC CALCULATOR
# Computes Volume Point of Control from intraday bar data.
# VPOC = price where most contracts traded today — gravitational center.
# Zero additional cost — uses bar data already pulled by scanner.
# ═══════════════════════════════════════════════════════════

def compute_vpoc(bars: list) -> dict:
    """
    Compute Volume Point of Control from intraday bars.

    bars: list of dicts with 'h', 'l', 'c', 'v' keys (high, low, close, volume)

    Returns: {vpoc, value_area_high, value_area_low, total_volume}
    - vpoc: price level with highest volume (gravitational center)
    - value_area_high/low: 70% of volume traded within this range
    """
    if not bars or len(bars) < 5:
        return {}

    # Build volume-at-price profile using bar midpoints
    # Use 50-cent buckets for price levels
    price_volume = {}
    total_vol = 0

    for bar in bars:
        h = bar.get("h", 0)
        l = bar.get("l", 0)
        v = bar.get("v", 0)
        if not h or not l or not v:
            continue

        mid = (h + l) / 2
        # Round to nearest 50 cents for bucketing
        bucket = round(mid * 2) / 2
        price_volume[bucket] = price_volume.get(bucket, 0) + v
        total_vol += v

    if not price_volume or total_vol == 0:
        return {}

    # VPOC = price with highest volume
    vpoc = max(price_volume, key=price_volume.get)

    # Value Area: 70% of total volume, expanding from VPOC
    sorted_levels = sorted(price_volume.items(), key=lambda x: -x[1])
    va_volume = 0
    va_target = total_vol * 0.70
    va_prices = []

    for price, vol in sorted_levels:
        va_prices.append(price)
        va_volume += vol
        if va_volume >= va_target:
            break

    va_high = max(va_prices) if va_prices else vpoc
    va_low = min(va_prices) if va_prices else vpoc

    return {
        "vpoc": round(vpoc, 2),
        "value_area_high": round(va_high, 2),
        "value_area_low": round(va_low, 2),
        "total_volume": total_vol,
    }


# ═══════════════════════════════════════════════════════════
# CORE FLOW DETECTION
# ═══════════════════════════════════════════════════════════

class FlowDetector:
    """
    Unified institutional flow detection.

    Hooks into every chain pull (piggyback, zero cost).
    Runs dedicated forward sweeps at scheduled times.
    Persists all state to Redis via PersistentState.
    """

    def __init__(self, persistent_state, post_fn: Callable = None):
        """
        persistent_state: PersistentState instance
        post_fn: function(message) — post to Telegram
        """
        self._state = persistent_state
        self._post = post_fn
        self._spot_history = {}  # {ticker: [(timestamp, spot), ...]} for pre-move detection
        # v7.0: Volume velocity tracker — consecutive snapshots show flow direction
        # Key: "ticker:strike:side" → list of (timestamp, volume, direction_approx, confidence)
        self._flow_velocity = {}  # tracks last N snapshots per contract for sustained flow detection
        # Phase 3: streaming option store reference
        self._option_store = None
        self._sweep_alerts = []  # recent sweeps for conviction pipeline

        # ── Issue 2+4: Session-level conviction position tracking ──
        # Tracks active conviction positions to suppress duplicate alerts.
        # {ticker: {"direction": "bullish"/"bearish", "entry_time": iso,
        #           "strike": float, "route": str, "fire_count": int}}
        self._conviction_positions = {}
        # Reference to thesis monitor for wiring conviction → ActiveTrade (Issue 1)
        self._thesis_monitor = None
        self._thesis_monitor_fn = None  # callable(play) → creates ActiveTrade
        # Reference to thesis engine for EM alignment gate
        self._get_thesis_fn = None  # callable(ticker) → ThesisContext or None

        # v7.2 fix: Flush conviction cooldowns from prior process.
        # Prevents pre-deploy cooldown keys from silently blocking post-deploy plays.
        try:
            flushed = self._state.flush_conviction_cooldowns()
            log.info(f"FlowDetector: conviction cooldown flush — cleared {flushed} keys "
                     f"(CONVICTION_COOLDOWN={CONVICTION_COOLDOWN}s)")
        except Exception as e:
            log.warning(f"Cooldown flush on startup failed: {e}")

    def set_option_store(self, store):
        """Wire the OptionQuoteStore for streaming option data overlay."""
        self._option_store = store
        log.info("FlowDetector: option streaming store connected")

    def set_get_thesis_fn(self, fn):
        """Wire thesis lookup for EM alignment gate on conviction plays."""
        self._get_thesis_fn = fn
        log.info("FlowDetector: thesis lookup wired for EM alignment gate")

    def _check_em_alignment(self, ticker: str, trade_direction: str) -> dict:
        """Check if conviction play direction aligns with the morning EM card thesis.

        Returns dict with:
          aligned: bool — True if flow agrees with thesis (or thesis unavailable)
          thesis_bias: str — "BULLISH", "BEARISH", "NEUTRAL", etc.
          thesis_score: int — bias score from EM card (-14 to +14)
          conflict: bool — True if flow directly opposes a strong thesis
          detail: str — human-readable alignment note
        """
        result = {"aligned": True, "thesis_bias": "", "thesis_score": 0,
                  "conflict": False, "detail": "no thesis available",
                  "gex_sign": "", "regime": ""}

        if not self._get_thesis_fn:
            return result

        try:
            thesis = self._get_thesis_fn(ticker)
            if not thesis:
                return result

            bias = getattr(thesis, "bias", "NEUTRAL") or "NEUTRAL"
            score = getattr(thesis, "bias_score", 0) or 0
            gex_sign = getattr(thesis, "gex_sign", "positive") or "positive"
            regime = getattr(thesis, "regime", "UNKNOWN") or "UNKNOWN"

            result["thesis_bias"] = bias
            result["thesis_score"] = score
            result["gex_sign"] = gex_sign
            result["regime"] = regime

            bias_upper = bias.upper()

            # Determine if thesis is bullish, bearish, or neutral
            thesis_bullish = "BULLISH" in bias_upper
            thesis_bearish = "BEARISH" in bias_upper
            thesis_strong = "STRONG" in bias_upper or abs(score) >= 7

            flow_bullish = trade_direction == "bullish"
            flow_bearish = trade_direction == "bearish"

            if thesis_bullish and flow_bullish:
                result["aligned"] = True
                result["detail"] = f"✅ ALIGNED: flow {trade_direction} agrees with EM card {bias} ({score:+d}/14)"
            elif thesis_bearish and flow_bearish:
                result["aligned"] = True
                result["detail"] = f"✅ ALIGNED: flow {trade_direction} agrees with EM card {bias} ({score:+d}/14)"
            elif thesis_bullish and flow_bearish:
                result["aligned"] = False
                result["conflict"] = thesis_strong
                result["detail"] = (f"⚠️ CONFLICT: flow bearish vs EM card {bias} ({score:+d}/14)"
                                    + (" — STRONG thesis, shadow only" if thesis_strong else ""))
            elif thesis_bearish and flow_bullish:
                result["aligned"] = False
                result["conflict"] = thesis_strong
                result["detail"] = (f"⚠️ CONFLICT: flow bullish vs EM card {bias} ({score:+d}/14)"
                                    + (" — STRONG thesis, shadow only" if thesis_strong else ""))
            else:
                # Neutral thesis — flow can go either way
                result["aligned"] = True
                result["detail"] = f"📊 NEUTRAL thesis ({score:+d}/14) — flow direction not opposed"

        except Exception as e:
            result["detail"] = f"alignment check error: {e}"

        return result

    def set_thesis_monitor_fn(self, fn):
        """Wire a callback to create ActiveTrade from conviction plays (Issue 1)."""
        self._thesis_monitor_fn = fn
        log.info("FlowDetector: thesis monitor conviction entry wired")

    def _check_conviction_session_state(self, ticker: str, trade_direction: str,
                                         strike: float, route: str) -> str:
        """Check session state for conviction dedup (Issues 2+4).

        Returns:
          "new"         — first fire, full alert
          "hold"        — same direction re-fire, suppress
          "exit"        — direction flipped (app.py decides whether to post based on posted flag)
          "new_strike"  — same direction but significantly different strike
                          AND enough time has passed since prior fire

        FIX BUG #3: tightened from 3% strike-diff to 5% AND added a
        10-minute min gap even for different-strike entries. Previously
        a 3% drift on SPY ($15) during a trending move produced repeated
        "new_strike" alerts every few minutes. Now requires both a
        meaningful strike change AND real elapsed time.
        """
        existing = self._conviction_positions.get(ticker)
        if not existing:
            return "new"

        existing_dir = existing.get("direction", "")

        # Direction flip → always return "exit" for internal tracking.
        # v7.2: Whether to POST the exit signal to Telegram depends on the
        # 'posted' flag, which app.py checks via exit_prior_was_posted.
        if existing_dir and existing_dir != trade_direction:
            return "exit"

        existing_strike = existing.get("strike", 0)

        # FIX BUG #3: Same direction — check BOTH strike change AND time elapsed.
        # Previously: strike_diff > 3.0% → "new_strike" (too loose, fired on drift).
        # Now: strike_diff > 5.0% AND elapsed > 10 min → "new_strike".
        if existing_strike > 0 and strike > 0:
            strike_diff_pct = abs(strike - existing_strike) / existing_strike * 100
            if strike_diff_pct > 5.0:
                # Additionally require 10+ minutes since prior entry
                _entry_time_iso = existing.get("entry_time")
                if _entry_time_iso:
                    try:
                        from datetime import datetime as _dt
                        _prior_dt = _dt.fromisoformat(_entry_time_iso)
                        _age_sec = (_dt.now() - _prior_dt).total_seconds()
                        if _age_sec >= 600:  # 10 minutes
                            return "new_strike"
                        # Too soon — treat as hold even though strike moved
                    except Exception:
                        # Can't parse time → conservative: allow new_strike
                        return "new_strike"
                else:
                    return "new_strike"

        # Same direction, similar strike (or too-recent) → suppress (HOLD)
        return "hold"

    def _update_conviction_session(self, ticker: str, trade_direction: str,
                                    strike: float, route: str):
        """Update session state after firing a conviction alert.
        Note: posted flag defaults False until confirm_conviction_posted() is called."""
        existing = self._conviction_positions.get(ticker, {})
        fire_count = existing.get("fire_count", 0) + 1
        self._conviction_positions[ticker] = {
            "direction": trade_direction,
            "strike": strike,
            "route": route,
            "entry_time": datetime.now().isoformat(),
            "fire_count": fire_count,
            "posted": existing.get("posted", False),  # v7.2: preserve posted flag
        }

    def confirm_conviction_posted(self, ticker: str, trade_direction: str,
                                   strike: float = 0):
        """v7.2 fix: Called by app.py AFTER a conviction play is actually posted
        to Telegram. Only after this call will exit signals fire for this ticker.

        Prevents exit signals for positions that were detected but blocked by
        Potter Box, dedup, shadow gate, etc.
        """
        # Update in-memory session state
        existing = self._conviction_positions.get(ticker, {})
        existing["posted"] = True
        existing["direction"] = trade_direction
        if strike > 0:
            existing["strike"] = strike
        self._conviction_positions[ticker] = existing

        # Update Redis for cross-restart persistence
        try:
            self._state._json_set(f"conviction_dir:{ticker}", {
                "direction": trade_direction,
                "strike": strike,
                "time": datetime.now().isoformat(),
                "posted": True,
            }, ttl=14400)
        except Exception:
            pass

        log.info(f"Conviction direction CONFIRMED posted: {ticker} {trade_direction} "
                 f"${strike:.0f}")

    def clear_conviction_session(self, ticker: str = None):
        """Clear conviction session state. Called on direction flip or EOD reset."""
        if ticker:
            self._conviction_positions.pop(ticker, None)
        else:
            self._conviction_positions.clear()

    def handle_sweep(self, sweep: dict):
        """Handle a real-time sweep from SweepDetector.

        Converts streaming sweep data into a flow alert compatible with
        the conviction play pipeline, then fires through the normal alert path.
        """
        from schwab_stream import get_streaming_spot, parse_occ_symbol
        ticker = sweep.get("ticker", "")
        if not ticker:
            return

        # Build a flow alert from the sweep
        side = sweep.get("side", "unknown")
        sweep_side = sweep.get("sweep_side", "unknown")

        # Direction: buy sweep on calls = bullish, buy sweep on puts = bearish
        if sweep_side == "buy":
            if side == "call":
                directional = "BULLISH"
                direction_approx = "buyer_initiated"
            else:
                directional = "BEARISH"
                direction_approx = "buyer_initiated"
        elif sweep_side == "sell":
            if side == "call":
                directional = "BEARISH"
                direction_approx = "seller_initiated"
            else:
                directional = "BULLISH"
                direction_approx = "seller_initiated"
        else:
            directional = f"LEAN {'BULLISH' if side == 'call' else 'BEARISH'}"
            direction_approx = "unknown"

        alert = {
            "ticker": ticker,
            "expiry": sweep.get("expiry", ""),
            "strike": sweep.get("strike", 0),
            "side": side,
            "volume": sweep.get("volume_delta", 0),
            "oi": 0,  # not available from sweep
            "vol_oi_ratio": 999.0,  # sweep = extreme
            "flow_level": "extreme",
            "direction_approx": direction_approx,
            "direction_confidence": 0.85 if sweep_side != "unknown" else 0.5,
            "directional_bias": directional,
            "book_imbalance": "balanced",
            "dist_from_spot_pct": 0,
            "spot": sweep.get("underlying_price", 0) or get_streaming_spot(ticker) or 0,
            "mid": (sweep.get("bid", 0) + sweep.get("ask", 0)) / 2 if sweep.get("bid") else sweep.get("last", 0),
            "bid": sweep.get("bid", 0),
            "ask": sweep.get("ask", 0),
            # v8.5 (Phase 2): burst must be numeric (str crashes detect_conviction_plays
            # at `burst >= CONVICTION_MIN_BURST` with silent TypeError). SWEEP display
            # label is still driven by is_streaming_sweep + sweep_notional in formatter.
            "burst": sweep.get("volume_delta", 0),
            "is_burst": True,
            "is_new_strike": False,
            "sustained_flow": False,
            "velocity_count": 1,
            "vol_per_min": sweep.get("volume_delta", 0),  # all in one update
            "should_alert": True,
            "timestamp": datetime.now().isoformat(),
            "rehit_count": 0,
            "is_streaming_sweep": True,  # flag for formatting
            "sweep_notional": sweep.get("notional", 0),
            "sweep_delta": sweep.get("delta", 0),
            "sweep_iv": sweep.get("iv", 0),
        }

        self._sweep_alerts.append(alert)
        # Keep only last 50 sweep alerts
        if len(self._sweep_alerts) > 50:
            self._sweep_alerts = self._sweep_alerts[-50:]

        return alert

    def get_streaming_overlay(self, ticker: str, strike: float, side: str) -> Optional[dict]:
        """Get live streaming data for a specific contract if available.
        Used to enhance chain-polled data with sub-second bid/ask/volume."""
        if not self._option_store:
            return None
        from schwab_stream import parse_occ_symbol
        quotes = self._option_store.get_by_underlying(ticker)
        for q in quotes:
            parsed = parse_occ_symbol(q.get("symbol", ""))
            if parsed and parsed["strike"] == strike and parsed["side"] == side:
                return q
        return None

    # ─────────────────────────────────────────────────────
    # PHASE 1: INTRADAY VOLUME DETECTION
    # ─────────────────────────────────────────────────────

    def check_intraday_flow(self, ticker: str, expiry: str,
                            chain_data: dict, spot: float) -> List[dict]:
        """
        Called on every chain pull. Checks volume/OI ratios, detects
        bursts, approximates direction. Returns list of flow alerts.

        Zero additional API credits — uses data already fetched.
        """
        ticker = ticker.upper()
        tier = _get_volume_tier(ticker)
        min_vol = tier["min_volume"]

        parsed = parse_chain_volume_oi(chain_data, spot)
        if not parsed:
            return []

        # Phase 3: Overlay streaming option data on chain-polled data
        # Streaming bid/ask/volume is sub-second vs 30s chain cache
        if self._option_store:
            for p in parsed:
                overlay = self.get_streaming_overlay(ticker, p["strike"], p["side"])
                if overlay:
                    # Use streaming bid/ask for better direction inference
                    if overlay.get("bid", 0) > 0:
                        p["bid"] = overlay["bid"]
                    if overlay.get("ask", 0) > 0:
                        p["ask"] = overlay["ask"]
                    if overlay.get("last", 0) > 0:
                        p["last"] = overlay["last"]
                    # Recalculate mid from live bid/ask
                    if p["bid"] > 0 and p["ask"] > 0:
                        p["mid"] = round((p["bid"] + p["ask"]) / 2, 4)
                    # Use streaming volume if higher (more current)
                    stream_vol = overlay.get("volume", 0) or 0
                    if stream_vol > p["volume"]:
                        p["volume"] = stream_vol
                    # Store live Greeks on parsed entry for downstream use
                    p["live_delta"] = overlay.get("delta", 0)
                    p["live_iv"] = overlay.get("iv", 0)

        # Compute and store lightweight GEX for this ticker
        # Enables GEX convergence for conviction plays on ALL tickers
        try:
            gex = estimate_gex_from_chain(chain_data, spot)
            if gex and gex.get("gamma_flip", 0) > 0:
                self._state._json_set(f"gex:{ticker}", gex, ttl=7200)
        except Exception:
            pass

        # Compute and store IV skew
        try:
            skew = compute_iv_skew(chain_data, spot)
            if skew and skew.get("atm_iv", 0) > 0:
                self._state._json_set(f"iv_skew:{ticker}", skew, ttl=7200)
        except Exception:
            pass

        # Track spot price history for pre-move detection
        # Keeps last 60 min of spot data per ticker
        try:
            now_ts = time.time()
            if ticker not in self._spot_history:
                self._spot_history[ticker] = []
            self._spot_history[ticker].append((now_ts, spot))
            # Trim to last 60 minutes
            cutoff = now_ts - 3600
            self._spot_history[ticker] = [
                (t, s) for t, s in self._spot_history[ticker] if t > cutoff
            ]
        except Exception:
            pass

        # ── Volume burst detection ──
        prev_snapshot = self._state.get_volume_snapshot(ticker, expiry)
        current_vol_map = {}
        for p in parsed:
            key = f"{p['strike']}|{p['side']}"
            current_vol_map[key] = p["volume"]
        self._state.save_volume_snapshot(ticker, expiry, current_vol_map)

        alerts = []
        today_str = date.today().isoformat()

        for p in parsed:
            vol = p["volume"]
            oi = p["oi"]
            vol_oi = p["vol_oi_ratio"]

            # Gate: minimum volume
            if vol < min_vol:
                continue

            # Classify flow level
            if vol_oi >= VOL_OI_EXTREME:
                flow_level = "extreme"
            elif vol_oi >= VOL_OI_SIGNIFICANT:
                flow_level = "significant"
            elif vol_oi >= VOL_OI_NOTABLE:
                flow_level = "notable"
            else:
                continue

            # Volume burst check
            burst = 0
            if prev_snapshot:
                key = f"{p['strike']}|{p['side']}"
                prev_vol = prev_snapshot.get(key, 0)
                burst = vol - prev_vol
                if burst < 0:
                    burst = 0  # volume doesn't decrease intraday

            is_burst = burst >= VOLUME_BURST_THRESHOLD

            # ── v7.0: Enhanced directional inference ──────────────────
            # Uses composite direction_confidence from parse_chain_volume_oi.
            # With 60s scanning, "buyer_initiated" now means the LAST TRADE
            # hit the ask within the last minute — real signal, not noise.
            dir_conf = p.get("direction_confidence", 0)
            high_confidence = dir_conf >= 0.6

            if p["side"] == "call":
                if p["direction_approx"] == "buyer_initiated":
                    if high_confidence:
                        directional = "BULLISH (aggressive call buying)"
                    else:
                        directional = "BULLISH (call buying)"
                elif p["direction_approx"] == "seller_initiated":
                    if high_confidence:
                        directional = "BEARISH (aggressive call selling)"
                    else:
                        directional = "BEARISH (call selling/writing)"
                else:
                    directional = "BULLISH lean (call volume)"
            else:  # put
                if p["direction_approx"] == "buyer_initiated":
                    if high_confidence:
                        directional = "BEARISH (aggressive put buying)"
                    else:
                        directional = "BEARISH (put buying)"
                elif p["direction_approx"] == "seller_initiated":
                    if high_confidence:
                        directional = "BULLISH (aggressive put selling)"
                    else:
                        directional = "BULLISH (put selling/writing)"
                else:
                    directional = "BEARISH lean (put volume)"

            # ── v7.0: Volume velocity tracking ────────────────────────
            # Track consecutive snapshots for sustained flow detection.
            # 3+ consecutive snapshots with same direction = SUSTAINED.
            # Sustained flow with high confidence = institutional, not noise.
            velocity_key = f"{ticker}:{p['strike']}:{p['side']}"
            now_ts = time.time()
            sustained_flow = False
            velocity_count = 0
            try:
                if velocity_key not in self._flow_velocity:
                    self._flow_velocity[velocity_key] = []

                self._flow_velocity[velocity_key].append({
                    "ts": now_ts,
                    "vol": vol,
                    "burst": burst,
                    "dir": p["direction_approx"],
                    "conf": dir_conf,
                })
                # Trim to last 10 snapshots (10 minutes at 60s interval)
                self._flow_velocity[velocity_key] = [
                    s for s in self._flow_velocity[velocity_key]
                    if now_ts - s["ts"] < 600
                ][-10:]

                # Check for sustained flow: 3+ consecutive same-direction
                recent = self._flow_velocity[velocity_key]
                if len(recent) >= 3:
                    last_3_dirs = [s["dir"] for s in recent[-3:]]
                    if (all(d == "buyer_initiated" for d in last_3_dirs) or
                        all(d == "seller_initiated" for d in last_3_dirs)):
                        sustained_flow = True
                        velocity_count = len(recent)
                        # Boost direction label
                        if "BULLISH" in directional and sustained_flow:
                            directional = directional.replace("BULLISH", "BULLISH SUSTAINED")
                        elif "BEARISH" in directional and sustained_flow:
                            directional = directional.replace("BEARISH", "BEARISH SUSTAINED")
            except Exception:
                pass

            # Volume velocity: contracts per minute in last 3 snapshots
            vol_per_min = 0
            try:
                recent = self._flow_velocity.get(velocity_key, [])
                if len(recent) >= 2:
                    time_span = recent[-1]["ts"] - recent[0]["ts"]
                    vol_delta = recent[-1]["vol"] - recent[0]["vol"]
                    if time_span > 0 and vol_delta > 0:
                        vol_per_min = int(vol_delta / (time_span / 60))
            except Exception:
                pass

            # New strike detection (OI=0 but volume > minimum)
            is_new_strike = oi == 0 and vol >= min_vol

            # Cooldown check
            cooldown_key = f"{ticker}:{p['strike']}:{p['side']}:{flow_level}"
            cooldown_secs = {
                "notable": ALERT_COOLDOWN_NOTABLE,
                "significant": ALERT_COOLDOWN_SIGNIFICANT,
                "extreme": ALERT_COOLDOWN_EXTREME,
            }.get(flow_level, 1800)

            should_alert = True
            if cooldown_secs > 0:
                should_alert = self._state.check_and_set_cooldown(
                    cooldown_key, cooldown_secs
                )

            alert = {
                "ticker": ticker,
                "expiry": expiry,
                "strike": p["strike"],
                "side": p["side"],
                "volume": vol,
                "oi": oi,
                "vol_oi_ratio": vol_oi,
                "flow_level": flow_level,
                "direction_approx": p["direction_approx"],
                "direction_confidence": dir_conf,
                "directional_bias": directional,
                "book_imbalance": p["book_imbalance"],
                "dist_from_spot_pct": p["dist_from_spot_pct"],
                "spot": spot,
                "mid": p.get("mid", 0),
                "bid": p.get("bid", 0),
                "ask": p.get("ask", 0),
                "burst": burst,
                "is_burst": is_burst,
                "is_new_strike": is_new_strike,
                "sustained_flow": sustained_flow,
                "velocity_count": velocity_count,
                "vol_per_min": vol_per_min,
                "should_alert": should_alert,
                "timestamp": datetime.now().isoformat(),
                "rehit_count": 0,
            }

            # Track re-hits: same strike hit multiple times in one session
            if flow_level in ("significant", "extreme"):
                rehit = self._state.increment_flow_rehit(
                    ticker, p["strike"], p["side"])
                alert["rehit_count"] = rehit
                if rehit >= 2:
                    log.info(f"🔄 FLOW RE-HIT #{rehit}: {ticker} "
                           f"${p['strike']:.0f} {p['side']} "
                           f"({vol_oi:.1f}x, {vol:,} vol)")

            alerts.append(alert)

            # Save volume flag for tomorrow's OI confirmation
            if flow_level in ("significant", "extreme"):
                self._state.append_volume_flag(today_str, {
                    "ticker": ticker,
                    "expiry": expiry,
                    "strike": p["strike"],
                    "side": p["side"],
                    "volume": vol,
                    "oi": oi,
                    "vol_oi_ratio": vol_oi,
                    "flow_level": flow_level,
                    "directional_bias": directional,
                    "spot": spot,
                    "date": today_str,
                    # v8.5 (Phase 3.3): timestamp needed by Dashboard `OI Time`
                    # column reader at dashboard._get_oi_snapshot. Previously
                    # the save omitted it, so the reader's fallback chain
                    # (timestamp|time|ts) found nothing and rendered blank.
                    "timestamp": datetime.now().isoformat(),
                })

                # Store flow direction for real-time queries by all subsystems
                # (active scanner override, EntryValidator boost, Potter Box bias)
                flow_dir_str = "bullish" if "BULLISH" in directional.upper() else "bearish"
                # v8.5 (Phase 3.3): compute notional here and save it.
                # Previously this dict omitted `notional` entirely, so both
                # the Dashboard "Flow Notional" column AND the Signal Log
                # flow_conviction `notional=$0` detail were always $0.
                # Options contracts are 100 shares; notional = vol * mid * 100.
                _mid_est = p.get("mid", 0) or 0
                _flow_notional = int(vol * _mid_est * 100) if _mid_est > 0 else 0
                self._state.save_flow_direction(ticker, {
                    "direction": flow_dir_str,
                    "vol_oi": round(vol_oi, 1),
                    "volume": vol,
                    "flow_level": flow_level,
                    "side": p["side"],
                    "strike": p["strike"],
                    "spot": spot,
                    "expiry": expiry,
                    "notional": _flow_notional,
                    "timestamp": datetime.now().isoformat(),
                })

        # Sort by vol_oi_ratio descending
        alerts.sort(key=lambda a: a["vol_oi_ratio"], reverse=True)

        # Cap at top 5 per ticker per check
        return alerts[:5]

    # ─────────────────────────────────────────────────────
    # PHASE 2: MORNING OI CONFIRMATION
    # ─────────────────────────────────────────────────────

    def run_morning_confirmation(self, chain_fn: Callable,
                                 spot_fn: Callable,
                                 expirations_fn: Callable) -> List[dict]:
        """
        Run at 8:15 AM CT. Compares today's settled OI against yesterday's
        baseline at every strike that had a volume flag yesterday.

        Returns list of confirmed flow dicts.
        """
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Handle Monday — check Friday
        if date.today().weekday() == 0:
            yesterday = (date.today() - timedelta(days=3)).isoformat()

        flags = self._state.get_volume_flags(yesterday)
        if not flags:
            log.info("OI confirmation: no volume flags from yesterday")
            return []

        log.info(f"OI confirmation: checking {len(flags)} volume flags from {yesterday}")

        confirmed = []
        tickers_checked = set()

        for flag in flags:
            ticker = flag.get("ticker", "")
            expiry = flag.get("expiry", "")
            strike = flag.get("strike", 0)
            side = flag.get("side", "")

            if not ticker or not expiry:
                continue

            # Get yesterday's OI baseline
            yesterday_oi_data = self._state.get_yesterday_oi_baseline(ticker, expiry)
            if not yesterday_oi_data:
                continue

            # Get today's fresh OI (need chain pull)
            # Only fetch chain once per ticker/expiry
            cache_key = f"{ticker}:{expiry}"
            if cache_key not in tickers_checked:
                tickers_checked.add(cache_key)
                try:
                    spot = spot_fn(ticker) if spot_fn else 0
                    chain = chain_fn(ticker, expiry) if chain_fn else None
                    if chain and isinstance(chain, dict) and chain.get("s") == "ok":
                        # Save today's OI as baseline
                        today_oi = self._parse_oi_from_chain(chain)
                        self._state.save_oi_baseline(ticker, expiry, today_oi)
                except Exception as e:
                    log.debug(f"OI confirmation chain fetch failed for {ticker}: {e}")
                    continue

            # Compare
            today_baseline = self._state.get_oi_baseline(ticker, expiry)
            if not today_baseline:
                continue

            key = f"{float(strike)}|{side}"
            yesterday_oi = yesterday_oi_data.get(key, 0)
            today_oi = today_baseline.get(key, 0)
            oi_change = today_oi - yesterday_oi

            if yesterday_oi == 0 and today_oi == 0:
                continue

            # Classify
            if yesterday_oi > 0:
                oi_change_pct = oi_change / yesterday_oi
            else:
                oi_change_pct = 1.0 if oi_change > 0 else 0

            if oi_change > 100:  # meaningful increase
                flow_type = "confirmed_buildup"
            elif oi_change < -100:  # meaningful decrease
                flow_type = "confirmed_unwinding"
            else:
                flow_type = "churn"

            # Price context
            yesterday_spot = flag.get("spot", 0)
            try:
                today_spot = spot_fn(ticker) if spot_fn else 0
            except Exception:
                today_spot = 0

            price_change = 0
            if yesterday_spot > 0 and today_spot > 0:
                price_change = (today_spot - yesterday_spot) / yesterday_spot * 100

            # Divergence detection
            divergence = False
            if flow_type == "confirmed_buildup":
                if side == "call" and price_change < -0.5:
                    divergence = True  # calls added, stock down = accumulation
                elif side == "put" and price_change > 0.5:
                    divergence = True  # puts added, stock up = hedging into strength

            confirmation = {
                "ticker": ticker,
                "expiry": expiry,
                "strike": strike,
                "side": side,
                "flow_type": flow_type,
                "yesterday_oi": yesterday_oi,
                "today_oi": today_oi,
                "oi_change": oi_change,
                "oi_change_pct": round(oi_change_pct * 100, 1),
                "yesterday_volume": flag.get("volume", 0),
                "yesterday_vol_oi_ratio": flag.get("vol_oi_ratio", 0),
                "yesterday_flow_level": flag.get("flow_level", ""),
                "yesterday_directional": flag.get("directional_bias", ""),
                "yesterday_spot": yesterday_spot,
                "today_spot": today_spot,
                "price_change_pct": round(price_change, 2),
                "divergence": divergence,
                "date": date.today().isoformat(),
            }

            confirmed.append(confirmation)

            # Update campaign
            if flow_type != "churn":
                day_entry = {
                    "date": date.today().isoformat(),
                    "volume": flag.get("volume", 0),
                    "oi_change": oi_change,
                    "vol_oi_ratio": flag.get("vol_oi_ratio", 0),
                    "spot": today_spot or yesterday_spot,
                }
                campaign = self._state.update_flow_campaign(
                    ticker, strike, side, expiry, day_entry, flow_type
                )
                confirmation["campaign"] = campaign

        log.info(f"OI confirmation complete: {len(confirmed)} confirmed "
                 f"({sum(1 for c in confirmed if c['flow_type'] == 'confirmed_buildup')} buildup, "
                 f"{sum(1 for c in confirmed if c['flow_type'] == 'confirmed_unwinding')} unwinding, "
                 f"{sum(1 for c in confirmed if c['flow_type'] == 'churn')} churn)")

        return confirmed

    def _parse_oi_from_chain(self, chain_data: dict) -> Dict[str, int]:
        """Extract {strike|side: oi} from chain data."""
        sym_list = chain_data.get("optionSymbol") or []
        n = len(sym_list)
        if n == 0:
            return {}

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides = col("side", "")
        ois = col("openInterest", 0)

        result = {}
        for i in range(n):
            strike = strikes[i]
            side = str(sides[i] or "").lower()
            oi = int(ois[i] or 0)
            if strike is None or side not in ("call", "put"):
                continue
            result[f"{float(strike)}|{side}"] = oi
        return result

    # ─────────────────────────────────────────────────────
    # STALK ALERT GENERATION
    # ─────────────────────────────────────────────────────

    def generate_stalk_alerts(self, confirmations: List[dict],
                              support_fn: Callable = None) -> List[dict]:
        """
        Generate stalk alerts from confirmed flow.
        Three types: DO NOT CHASE / WATCH FOR TRIGGER / ROOM LEFT

        support_fn: function(ticker) → list of support/resistance dicts

        FIX BUG #3: dedup per-ticker-per-day. Previously, if a ticker had
        multiple confirmed strikes (very common for SPY/QQQ), we'd emit one
        stalk per strike → 4-6 identical-looking STALK ALERTs in a row
        for the same name. Now we keep only the highest-conviction stalk
        per ticker per day (ranked by campaign days then total OI change).
        """
        stalks = []
        # FIX BUG #3: per-ticker best-stalk tracker — we emit only one
        # stalk per ticker per call, picking the strongest by campaign depth.
        best_by_ticker = {}

        for conf in confirmations:
            if conf["flow_type"] == "churn":
                continue

            ticker = conf["ticker"]
            price_change = conf.get("price_change_pct", 0)
            strike = conf["strike"]
            side = conf["side"]
            today_spot = conf.get("today_spot", 0)

            # Determine stalk type
            if conf["flow_type"] == "confirmed_buildup":
                if side == "call":
                    expected_direction = "BULLISH"
                else:
                    expected_direction = "BEARISH"
            else:  # confirmed_unwinding
                if side == "put":
                    expected_direction = "BULLISH (put unwinding)"
                else:
                    expected_direction = "BEARISH (call unwinding)"

            # Classify by price movement
            if expected_direction.startswith("BULLISH"):
                if price_change > 2.0:
                    stalk_type = "do_not_chase"
                elif price_change < 0.5:
                    stalk_type = "watch_for_trigger"
                else:
                    stalk_type = "room_left"
            else:  # bearish
                if price_change < -2.0:
                    stalk_type = "do_not_chase"
                elif price_change > -0.5:
                    stalk_type = "watch_for_trigger"
                else:
                    stalk_type = "room_left"

            # Get support/resistance levels if available
            levels = {}
            if support_fn:
                try:
                    levels = support_fn(ticker) or {}
                except Exception:
                    pass

            # Campaign context
            campaign = conf.get("campaign", {})
            consecutive = campaign.get("consecutive_days", 1)
            total_oi = campaign.get("total_oi_change", conf.get("oi_change", 0))

            stalk = {
                "ticker": ticker,
                "expiry": conf["expiry"],
                "strike": strike,
                "side": side,
                "stalk_type": stalk_type,
                "flow_type": conf["flow_type"],
                "expected_direction": expected_direction,
                "oi_change": conf["oi_change"],
                "oi_change_pct": conf["oi_change_pct"],
                "yesterday_volume": conf.get("yesterday_volume", 0),
                "price_change_pct": price_change,
                "today_spot": today_spot,
                "divergence": conf.get("divergence", False),
                "campaign_days": consecutive,
                "campaign_total_oi": total_oi,
                "support_levels": levels.get("supports", []),
                "resistance_levels": levels.get("resistances", []),
                "date": date.today().isoformat(),
            }

            # FIX BUG #3: per-ticker dedup — only keep the strongest stalk per
            # ticker. Rank by (campaign_days desc, |total_oi| desc, then
            # conviction of stalk_type). Previously appended every strike's
            # stalk, flooding the diagnosis channel with 4-6 near-identical
            # STALK ALERTs for the same ticker (observed on SPY/QQQ).
            _type_priority = {"do_not_chase": 3, "watch_for_trigger": 2, "room_left": 1}
            _rank_key = (
                consecutive,
                abs(total_oi or 0),
                _type_priority.get(stalk_type, 0),
            )
            prev = best_by_ticker.get(ticker)
            if prev is None or _rank_key > prev[0]:
                best_by_ticker[ticker] = (_rank_key, stalk)

        # Emit deduped best-per-ticker stalks
        for _rank, stalk in best_by_ticker.values():
            stalks.append(stalk)
            self._state.save_stalk_alert(stalk["ticker"], stalk)

        return stalks

    # ─────────────────────────────────────────────────────
    # SECTOR FLOW AGGREGATION
    # ─────────────────────────────────────────────────────

    def detect_sector_flow(self, alerts: List[dict]) -> List[dict]:
        """
        Detect when 2+ tickers in the same sector show same-direction flow.
        Returns list of sector flow signals.
        """
        sector_flow = {}

        for alert in alerts:
            if alert.get("flow_level") not in ("significant", "extreme"):
                continue

            ticker = alert["ticker"]
            sector = _get_sector(ticker)
            bias = alert.get("directional_bias", "")

            # Simplify to bull/bear
            direction = "bullish" if "BULLISH" in bias.upper() else "bearish"

            key = f"{sector}:{direction}"
            if key not in sector_flow:
                sector_flow[key] = {
                    "sector": sector,
                    "direction": direction,
                    "tickers": [],
                    "total_volume": 0,
                }
            sf = sector_flow[key]
            if ticker not in [t["ticker"] for t in sf["tickers"]]:
                sf["tickers"].append({
                    "ticker": ticker,
                    "strike": alert["strike"],
                    "side": alert["side"],
                    "volume": alert["volume"],
                    "vol_oi_ratio": alert["vol_oi_ratio"],
                })
                sf["total_volume"] += alert["volume"]

        # Only return sectors with 2+ names
        return [sf for sf in sector_flow.values() if len(sf["tickers"]) >= 2]

    # ─────────────────────────────────────────────────────
    # ROLL DETECTION
    # ─────────────────────────────────────────────────────

    def detect_rolls(self, confirmations: List[dict]) -> List[dict]:
        """
        Detect institutional rolls: OI decrease at one strike + OI increase
        at nearby strike, same side, same expiry.
        """
        rolls = []
        by_ticker_side_exp = {}

        for conf in confirmations:
            if conf["flow_type"] == "churn":
                continue
            key = f"{conf['ticker']}:{conf['side']}:{conf['expiry']}"
            if key not in by_ticker_side_exp:
                by_ticker_side_exp[key] = []
            by_ticker_side_exp[key].append(conf)

        for key, confs in by_ticker_side_exp.items():
            buildups = [c for c in confs if c["flow_type"] == "confirmed_buildup"]
            unwinds = [c for c in confs if c["flow_type"] == "confirmed_unwinding"]

            for unwind in unwinds:
                for buildup in buildups:
                    # Check if they're close in size (within 30%)
                    unwound = abs(unwind["oi_change"])
                    built = abs(buildup["oi_change"])
                    if unwound == 0:
                        continue
                    size_ratio = built / unwound
                    if 0.7 <= size_ratio <= 1.3:
                        direction = "UP" if buildup["strike"] > unwind["strike"] else "DOWN"
                        rolls.append({
                            "ticker": unwind["ticker"],
                            "side": unwind["side"],
                            "expiry": unwind["expiry"],
                            "from_strike": unwind["strike"],
                            "to_strike": buildup["strike"],
                            "contracts": int((unwound + built) / 2),
                            "direction": direction,
                            "signal": f"Still {'bullish' if unwind['side'] == 'call' else 'bearish'}, "
                                      f"{'raising' if direction == 'UP' else 'lowering'} target",
                        })

        return rolls

    # ─────────────────────────────────────────────────────
    # EXPIRY CLUSTERING
    # ─────────────────────────────────────────────────────

    def detect_expiry_clustering(self, alerts: List[dict],
                                  economic_events: List[dict] = None) -> List[dict]:
        """
        Detect when multiple tickers have flow concentrated on the same expiry.
        Cross-reference against known economic events.
        """
        expiry_flow = {}

        for alert in alerts:
            if alert.get("flow_level") not in ("significant", "extreme"):
                continue
            exp = alert.get("expiry", "")
            if not exp:
                continue
            if exp not in expiry_flow:
                expiry_flow[exp] = {"expiry": exp, "tickers": set(), "total_volume": 0}
            expiry_flow[exp]["tickers"].add(alert["ticker"])
            expiry_flow[exp]["total_volume"] += alert["volume"]

        clusters = []
        for exp, data in expiry_flow.items():
            if len(data["tickers"]) >= 3:  # 3+ tickers on same expiry
                cluster = {
                    "expiry": exp,
                    "ticker_count": len(data["tickers"]),
                    "tickers": sorted(data["tickers"]),
                    "total_volume": data["total_volume"],
                    "nearby_events": [],
                }
                # Cross-reference economic calendar
                if economic_events:
                    try:
                        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                        for evt in economic_events:
                            evt_date = datetime.strptime(
                                evt.get("date", "")[:10], "%Y-%m-%d"
                            ).date()
                            if abs((evt_date - exp_date).days) <= 2:
                                cluster["nearby_events"].append(evt.get("event", ""))
                    except (ValueError, TypeError):
                        pass

                clusters.append(cluster)

        return sorted(clusters, key=lambda c: c["ticker_count"], reverse=True)

    # ─────────────────────────────────────────────────────
    # SCORING HELPERS
    # ─────────────────────────────────────────────────────

    def get_flow_score_for_income(self, ticker: str, short_strike: float,
                                   trade_type: str,
                                   expiry: str = None) -> dict:
        """
        Get flow-based scoring adjustment for an income trade.

        Returns:
          {score_adj, reason, flow_level, recommended_expiry, expiry_note}
        """
        ticker = ticker.upper()
        result = {"score_adj": 0, "reasons": [], "flow_level": "none",
                  "recommended_expiry": None, "expiry_note": ""}

        # Check active campaigns
        campaigns = self._state.get_all_flow_campaigns(ticker)
        if not campaigns:
            return result

        # Find most relevant campaign
        best_campaign = None
        best_relevance = 0

        for c in campaigns:
            # Relevance = proximity to short strike + recency + size
            strike_dist = abs(c.get("strike", 0) - short_strike)
            if c.get("spot", short_strike) > 0:
                strike_dist_pct = strike_dist / c["spot"]
            else:
                strike_dist_pct = 1.0

            if strike_dist_pct > 0.10:  # too far from our trade
                continue

            consecutive = c.get("consecutive_days", 0)
            relevance = consecutive * 10 + abs(c.get("total_oi_change", 0)) / 1000

            if relevance > best_relevance:
                best_relevance = relevance
                best_campaign = c

        if not best_campaign:
            return result

        flow_type = best_campaign.get("flow_type", "")
        consecutive = best_campaign.get("consecutive_days", 1)
        camp_side = best_campaign.get("side", "")

        # Determine alignment
        if trade_type == "bull_put":
            # Bull put wants bullish flow
            if camp_side == "call" and flow_type == "confirmed_buildup":
                aligned = True
            elif camp_side == "put" and flow_type == "confirmed_unwinding":
                aligned = True
            elif camp_side == "put" and flow_type == "confirmed_buildup":
                aligned = False  # put buildup opposes bull put
            elif camp_side == "call" and flow_type == "confirmed_unwinding":
                aligned = False  # call unwinding opposes bull put
            else:
                aligned = None
        else:  # bear_call
            if camp_side == "put" and flow_type == "confirmed_buildup":
                aligned = True
            elif camp_side == "call" and flow_type == "confirmed_unwinding":
                aligned = True
            elif camp_side == "call" and flow_type == "confirmed_buildup":
                aligned = False
            elif camp_side == "put" and flow_type == "confirmed_unwinding":
                aligned = False
            else:
                aligned = None

        if aligned is None:
            return result

        if aligned:
            base_score = SCORE_CONFIRMED_ALIGNED
            campaign_bonus = min(consecutive * SCORE_CAMPAIGN_BONUS,
                                 SCORE_CAMPAIGN_BONUS * 3)
            result["score_adj"] = base_score + campaign_bonus
            result["flow_level"] = "confirmed_aligned"
            result["reasons"].append(
                f"✅ Confirmed {flow_type.replace('confirmed_', '')} "
                f"({consecutive}D campaign, {best_campaign.get('total_oi_change', 0):+,} OI) "
                f"— institutions aligned with your trade"
            )
        else:
            base_score = SCORE_CONFIRMED_OPPOSING
            result["score_adj"] = base_score
            result["flow_level"] = "confirmed_opposing"
            result["reasons"].append(
                f"⚠️ Confirmed {flow_type.replace('confirmed_', '')} "
                f"({consecutive}D, {best_campaign.get('total_oi_change', 0):+,} OI) "
                f"— institutions OPPOSING your trade"
            )

        # Expiry recommendation
        if best_campaign.get("expiry"):
            result["recommended_expiry"] = best_campaign["expiry"]
            result["expiry_note"] = (
                f"Institutional flow concentrated at {best_campaign['expiry']} expiry "
                f"— strong edge for income trades aligned with this positioning"
            )

        return result

    def get_flow_score_for_swing(self, ticker: str, fib_price: float,
                                  direction: str) -> dict:
        """
        Get flow-based scoring adjustment for a swing signal.

        Returns: {score_adj, reasons}
        """
        ticker = ticker.upper()
        result = {"score_adj": 0, "reasons": []}

        campaigns = self._state.get_all_flow_campaigns(ticker)
        if not campaigns:
            return result

        for c in campaigns:
            # Check if campaign strike is near the fib level
            if fib_price > 0:
                dist = abs(c.get("strike", 0) - fib_price) / fib_price
                if dist > 0.03:  # more than 3% away
                    continue

            camp_side = c.get("side", "")
            flow_type = c.get("flow_type", "")
            consecutive = c.get("consecutive_days", 1)

            # Alignment check
            if direction == "bull":
                aligned = (camp_side == "call" and "buildup" in flow_type) or \
                          (camp_side == "put" and "unwinding" in flow_type)
            else:
                aligned = (camp_side == "put" and "buildup" in flow_type) or \
                          (camp_side == "call" and "unwinding" in flow_type)

            if aligned:
                bonus = min(8 + consecutive * 2, 15)
                result["score_adj"] += bonus
                result["reasons"].append(
                    f"🏛️ Institutional {flow_type.replace('confirmed_', '')} "
                    f"at ${c['strike']:.0f} {camp_side} ({consecutive}D campaign) "
                    f"— confirms fib level (+{bonus})"
                )
            else:
                penalty = -5
                result["score_adj"] += penalty
                result["reasons"].append(
                    f"⚠️ Institutional {flow_type.replace('confirmed_', '')} "
                    f"at ${c['strike']:.0f} {camp_side} — headwind ({penalty})"
                )
            break  # Use most relevant campaign only

        return result

    def get_validator_boost(self, ticker: str, direction: str,
                            spot: float) -> float:
        """
        Get flow-based boost for EntryValidator scoring.
        Three sources checked (best wins):
        1. Conviction boost from 10x+ vol/OI bursts (0-14)
        2. Intraday flow direction from latest significant+ hit (0-14)
        3. Campaign boost from multi-day OI confirmation (0-1.5)
        """
        ticker = ticker.upper()
        best_boost = 0.0

        # Source 1: Conviction boost (stored when conviction play fires)
        try:
            conv_boost = self._state.get_conviction_boost(ticker, direction)
            if conv_boost > 0:
                best_boost = conv_boost
        except Exception:
            pass

        # Source 2: Intraday flow direction (from latest significant+ detection)
        try:
            fb = self._state.get_flow_conviction_boost(ticker, direction)
            best_boost = max(best_boost, fb)
        except Exception:
            pass

        # Source 3: Campaign-based boost (multi-day OI confirmation)
        campaigns = self._state.get_all_flow_campaigns(ticker)
        for c in (campaigns or []):
            strike = c.get("strike", 0)
            if spot > 0 and abs(strike - spot) / spot > 0.05:
                continue

            camp_side = c.get("side", "")
            flow_type = c.get("flow_type", "")
            consecutive = c.get("consecutive_days", 1)

            if direction == "bull":
                aligned = (camp_side == "call" and "buildup" in flow_type) or \
                          (camp_side == "put" and "unwinding" in flow_type)
            else:
                aligned = (camp_side == "put" and "buildup" in flow_type) or \
                          (camp_side == "call" and "unwinding" in flow_type)

            if aligned:
                if consecutive >= CAMPAIGN_STRONG_DAYS:
                    boost = VALIDATOR_BOOST_CONFIRMED
                elif consecutive >= CAMPAIGN_MIN_DAYS:
                    boost = VALIDATOR_BOOST_EXTREME
                else:
                    boost = VALIDATOR_BOOST_SIGNIFICANT
                best_boost = max(best_boost, boost)

        return best_boost

    # ─────────────────────────────────────────────────────
    # TRADE GENERATION (Extreme flow)
    # ─────────────────────────────────────────────────────

    def generate_flow_trade_ideas(self, alerts: List[dict]) -> List[dict]:
        """
        For Extreme tier flow, generate income trade ideas.
        Always generated regardless of score — user always sees the idea.
        """
        ideas = []
        tier = None

        for alert in alerts:
            if alert.get("flow_level") != "extreme":
                continue

            ticker = alert["ticker"]
            tier_cfg = _get_volume_tier(ticker)
            min_vol = tier_cfg["min_volume"]

            # Extra gate for trade generation
            if alert["volume"] < min_vol * TRADE_GEN_MIN_VOLUME_MULTIPLIER:
                continue

            side = alert["side"]
            strike = alert["strike"]
            spot = alert["spot"]
            expiry = alert.get("expiry", "")
            directional = alert.get("directional_bias", "")

            # Generate income trade aligned with flow
            if "BULLISH" in directional.upper():
                trade_type = "bull_put"
                # Short put below the flow strike
                suggested_short = round(strike * 0.97, 0)  # ~3% below
                suggested_long = suggested_short - 2
            else:
                trade_type = "bear_call"
                suggested_short = round(strike * 1.03, 0)  # ~3% above
                suggested_long = suggested_short + 2

            ideas.append({
                "ticker": ticker,
                "trade_type": trade_type,
                "suggested_short_strike": suggested_short,
                "suggested_long_strike": suggested_long,
                "recommended_expiry": expiry,
                "flow_trigger": {
                    "strike": strike,
                    "side": side,
                    "volume": alert["volume"],
                    "oi": alert["oi"],
                    "vol_oi_ratio": alert["vol_oi_ratio"],
                    "directional_bias": directional,
                },
                "note": (
                    f"Flow-generated idea: {alert['volume']:,} {side}s "
                    f"at ${strike:.0f} ({alert['vol_oi_ratio']:.1f}x vol/OI). "
                    f"This is an institutional thesis, not a technical setup."
                ),
            })

        return ideas

    def _get_recent_move_pct(self, ticker: str, lookback_min: int = 30) -> float:
        """
        Get the percentage price move in the last N minutes.
        Used to detect reactive flow (hedging/profit-taking after a big move)
        vs predictive flow (directional bet in calm conditions).
        """
        history = self._spot_history.get(ticker.upper(), [])
        if len(history) < 2:
            return 0.0
        now_ts = time.time()
        cutoff = now_ts - (lookback_min * 60)
        # Find the oldest spot in the lookback window
        old_spots = [(t, s) for t, s in history if t <= cutoff + 120]  # within 2 min of cutoff
        if not old_spots:
            # Use oldest available
            old_spots = [history[0]]
        oldest_spot = old_spots[0][1]
        current_spot = history[-1][1]
        if oldest_spot <= 0:
            return 0.0
        return abs(current_spot - oldest_spot) / oldest_spot * 100

    def _detect_move_start(self, ticker: str, direction: str,
                           lookback_min: int = 30) -> Optional[float]:
        """v8.5 Phase 3: Return epoch ts of when price started moving in
        `direction` by >0.3%. None if move can't be isolated.

        Uses self._spot_history (same primitive as _get_recent_move_pct)
        rather than schwab_stream bars — keeps this method self-contained
        and dependency-free.
        """
        try:
            history = self._spot_history.get(ticker.upper(), [])
            if len(history) < 5:
                return None
            now_ts = time.time()
            cutoff = now_ts - (lookback_min * 60)
            # Walk newest -> oldest; find first sample where move from that
            # sample to current crossed 0.3% in the requested direction.
            sign = 1 if direction == "bullish" else -1
            current_px = history[-1][1]
            for ts, px in reversed(history[:-1]):
                if ts < cutoff or px <= 0:
                    break
                pct = ((current_px - px) / px) * 100 * sign
                if pct >= 0.3:
                    return float(ts)
            return None
        except Exception:
            return None

    @staticmethod
    def _get_ct_time() -> Tuple[int, int]:
        """Get current Central Time hour and minute."""
        try:
            from zoneinfo import ZoneInfo
            ct = datetime.now(ZoneInfo("America/Chicago"))
            return ct.hour, ct.minute
        except Exception:
            # Fallback: UTC - 5 for CDT
            utc_now = datetime.now()
            ct_hour = (utc_now.hour - 5) % 24
            return ct_hour, utc_now.minute

    # ─────────────────────────────────────────────────────
    # Phase 1b: Recommendation Tracker integration
    # ─────────────────────────────────────────────────────
    def record_play_to_tracker(self, play: dict, spot_val: float,
                                chain_data: Optional[list],
                                rec_tracker_store) -> bool:
        """Record a conviction play to the recommendation tracker.

        Call this once per play right after format_conviction_play() +
        post_to_telegram(). Returns True on successful record.

        Args:
            play:              the conviction play dict (from detect_conviction_plays)
            spot_val:          current underlying spot price
            chain_data:        current option chain for the ticker (used to get option mark)
            rec_tracker_store: the RecommendationStore instance

        The rec_tracker handles deduplication automatically — 8 re-fires
        of the same NVDA idea within 4 hours = 1 campaign with
        duplicate_count=8.
        """
        if rec_tracker_store is None:
            return False
        try:
            # Map play data to recommendation shape
            direction = "bull" if play.get("trade_direction") == "bullish" else "bear"
            route = (play.get("route") or "immediate").lower()
            # "stalk" campaigns graded on swing horizon
            trade_type = "swing" if route == "stalk" else route

            right = "call" if direction == "bull" else "put"
            strike = play.get("rec_strike") or play.get("strike")
            expiry = str(play.get("expiry") or "")[:10]
            if not strike or not expiry:
                return False

            # Try to find the current option mark from the chain
            option_mark = None
            if chain_data:
                for c in chain_data:
                    c_right = (c.get("right") or c.get("option_type") or "").lower()
                    c_strike = c.get("strike")
                    if c_strike is None:
                        continue
                    try:
                        if c_right == right and abs(float(c_strike) - float(strike)) < 0.01:
                            bid = float(c.get("bid") or 0)
                            ask = float(c.get("ask") or 0)
                            if ask > 0:
                                option_mark = (bid + ask) / 2
                            break
                    except (ValueError, TypeError):
                        continue

            # Fall back to play's own mark/mid if chain lookup failed
            if not option_mark:
                option_mark = play.get("current_mark") or play.get("mid") or play.get("option_mark")

            if not option_mark or float(option_mark) <= 0:
                log.debug(f"Conviction tracker: no option mark for {play.get('ticker')} "
                          f"{right} {strike}, skipping record")
                return False

            from recommendation_tracker import record_recommendation as _rr
            _rr(
                store=rec_tracker_store,
                source="conviction_flow",
                ticker=play["ticker"],
                direction=direction,
                trade_type=trade_type,
                structure=f"long_{right}",
                legs=[{
                    "right": right,
                    "strike": float(strike),
                    "expiry": expiry,
                    "action": "buy",
                }],
                entry_option_mark=float(option_mark),
                entry_underlying=float(spot_val),
                confidence=70,   # conviction plays don't have explicit confidence
                regime=None,
                extra_metadata={
                    "vol_oi_ratio": play.get("vol_oi_ratio"),
                    "burst": play.get("burst"),
                    "rehit_count": play.get("rehit_count"),
                    "directional_bias": play.get("directional_bias"),
                    "route": route,
                    "dte": play.get("dte"),
                },
            )
            return True
        except Exception as e:
            log.warning(f"Conviction tracker record failed for "
                        f"{play.get('ticker', '?')}: {e}")
            return False

    # ─────────────────────────────────────────────────────
    # CONVICTION PLAY — tiered by DTE
    # ─────────────────────────────────────────────────────

    def detect_conviction_plays(self, alerts: List[dict],
                                 dte: int = None) -> List[dict]:
        """
        Detect overwhelming institutional flow and route by DTE:
          0-2 DTE → "immediate" — LONG CALL/PUT with 1DTE vehicle
          3-7 DTE → "income"   — auto-score income idea, post if ITQS > 60
          8-30 DTE → "swing"   — boost swing signal or create stalk
          30-60 DTE → "stalk"  — watchlist, track campaign

        Rules:
          - 0DTE after 10:30 CT → silenced (closing/hedging noise)
          - 0DTE never boosts income tier
          - Pre-move >2% in 30 min → flagged as reactive (hedging)
          - Opposite direction on same ticker → exit signal, not new entry
          - Recommends ATM/near-money strike, not just side
        """
        plays = []
        ct_hour, ct_minute = self._get_ct_time()
        ct_minutes_total = ct_hour * 60 + ct_minute
        ZERO_DTE_CUTOFF = 10 * 60 + 30  # 10:30 CT

        for alert in alerts:
            ticker = alert["ticker"]
            vol_oi = alert.get("vol_oi_ratio", 0)
            volume = alert.get("volume", 0)
            burst = alert.get("burst", 0)
            side = alert.get("side", "")
            direction = alert.get("directional_bias", "")

            # Get DTE
            alert_dte = dte
            if alert_dte is None:
                try:
                    exp_str = str(alert.get("expiry", ""))[:10]
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    alert_dte = (exp_date - date.today()).days
                except Exception:
                    alert_dte = 999

            # Skip anything beyond 60 DTE
            if alert_dte > 60:
                continue

            # ── 0DTE TIME GATE ──
            # After 10:30 CT, 0DTE flow is mostly closing/hedging/gamma noise.
            # Not directional conviction. Silence completely.
            if alert_dte == 0 and ct_minutes_total > ZERO_DTE_CUTOFF:
                continue

            # ── LOW CONFIDENCE — Flagging for Future Removal with Data Back Up ──
            # FIX BUG #10 (INSTRUMENT-ONLY, DO NOT BLOCK):
            # Handoff concern: ~2:45 PM CT onward, 3-30 DTE flow can look like
            # conviction but is actually forced EOD retail-close / de-risking
            # that just resembles institutional positioning. We DO NOT have
            # empirical data to justify a hard cutoff yet. Instead, log every
            # late-day 3-30 DTE conviction play at WARN level with a tag so
            # you can grep 'LATE_DAY_HEURISTIC' and build a dataset.
            # Once you have ≥2 weeks of tagged plays graded by the tracker,
            # revisit this block and decide whether to add a real gate.
            # --- To remove: delete this entire block. ---
            LATE_DAY_CUTOFF_MIN = 14 * 60 + 45  # 2:45 PM CT
            # v8.5 (Phase 2, Bug #1): narrowed 3-30 -> 3-7 DTE. EOD de-risking
            # isn't plausible for 11/18 DTE contracts — they don't expire soon enough.
            if (ct_minutes_total >= LATE_DAY_CUTOFF_MIN
                    and 3 <= alert_dte <= 7):
                log.warning(
                    f"LATE_DAY_HEURISTIC [LOW_CONFIDENCE]: {ticker} "
                    f"{alert_dte}DTE conviction fired at "
                    f"{ct_hour:02d}:{ct_minute:02d} CT — may be EOD "
                    f"de-risking rather than institutional positioning. "
                    f"Tag for later review; not blocking."
                )
            # ── end Fix #10 ──

            # v8.5 (Phase 2, Gate A): MIN OI FLOOR
            # vol/OI ratio is meaningless on tiny OI bases (Mon open, deep OTM,
            # Friday-post-expiry resets). Require real OI before trusting the ratio.
            oi_val = alert.get("oi", 0)
            _tier_name = next(
                (n for n, cfg in VOLUME_TIERS.items() if ticker in cfg["tickers"]),
                "mid_cap"
            )
            _min_oi = CONVICTION_MIN_OI.get(_tier_name, 500)
            if oi_val < _min_oi:
                continue  # insufficient OI base — ratio is noise

            # Gate: Vol/OI ratio
            if vol_oi < CONVICTION_MIN_VOL_OI:
                continue

            # Gate: Absolute volume
            tier = _get_volume_tier(ticker)
            if volume < tier["min_volume"] * CONVICTION_MIN_VOLUME_MULT:
                continue

            # Gate: Burst OR overwhelming cumulative ratio
            has_burst = burst >= CONVICTION_MIN_BURST
            has_overwhelming = vol_oi >= CONVICTION_MIN_VOL_OI * 1.5  # 15x+
            # v7.0: Sustained flow can substitute for burst — if the same
            # direction has persisted for 3+ consecutive 60s snapshots,
            # the flow is real even without a single massive burst.
            has_sustained = alert.get("sustained_flow", False)
            if not has_burst and not has_overwhelming and not has_sustained:
                continue

            # v8.5 (Phase 2, Gate B): NOTIONAL DOLLAR FLOOR
            # Real institutional conviction is ≥$5-10M in premium, not N contracts.
            # 30k NVDA contracts × $0.15 = $450k is lottery flow, not smart money.
            _mid_early = alert.get("mid", 0) or 0
            if _mid_early <= 0:
                _mid_early = (alert.get("bid", 0) + alert.get("ask", 0)) / 2
            _notional_early = volume * _mid_early * 100
            _notional_floor = (CONVICTION_NOTIONAL_TIER1
                               if ticker in TIER1_NOTIONAL_TICKERS
                               else CONVICTION_NOTIONAL_TIER2)
            if _notional_early < _notional_floor:
                log.debug(f"💎 NOTIONAL FLOOR: {ticker} "
                          f"${_notional_early/1e6:.2f}M < ${_notional_floor/1e6:.0f}M")
                continue

            # Gate: Clear direction
            # v7.0: Require minimum direction confidence for conviction plays.
            # "lean" directions (unknown trade side) need burst OR sustained
            # to qualify — they're ambiguous without trade-side data.
            dir_conf = alert.get("direction_confidence", 0)
            if "BULLISH" in direction.upper():
                trade_direction = "bullish"
                trade_side = "LONG CALL"
                trade_emoji = "📗🚀"
            elif "BEARISH" in direction.upper():
                trade_direction = "bearish"
                trade_side = "LONG PUT"
                trade_emoji = "📕🚀"
            else:
                continue

            # v7.0: If direction is just a "lean" (no trade-side data),
            # require either burst or sustained flow — leans alone aren't
            # enough for a conviction play without confirmed trade side.
            is_lean = "lean" in direction.lower()
            if is_lean and not has_burst and not has_sustained:
                continue

            # ── DIRECTION FLIP = EXIT SIGNAL ──
            # If we already have a conviction play in the opposite direction
            # for this ticker, this is an exit signal, not a new entry.
            # v7.2: Only fire exit signal if the prior entry was actually posted
            # to Telegram. If prior was never posted (blocked by Potter Box,
            # dedup, shadow gate), treat this as a NEW entry instead.
            # Direction is still tracked eagerly in Redis for internal state.
            is_exit_signal = False
            _exit_prior_was_posted = False
            try:
                existing = self._state._json_get(f"conviction_dir:{ticker}")
                if existing:
                    existing_dir = existing.get("direction", "")
                    if existing_dir and existing_dir != trade_direction:
                        _exit_prior_was_posted = existing.get("posted", False)
                        if _exit_prior_was_posted:
                            # FIX BUG #11-lite: don't fire an exit signal if the
                            # original entry was posted less than
                            # CONVICTION_EXIT_MIN_HOLD_SEC ago. Observed live:
                            # exit firing 9 min after entry, before user could
                            # complete the trade, making the bot tell the user
                            # they're "on the wrong side" of their own fill.
                            _prior_time_iso = existing.get("time")
                            _minutes_since_entry = None
                            if _prior_time_iso:
                                try:
                                    from datetime import datetime as _dt
                                    _prior_dt = _dt.fromisoformat(_prior_time_iso)
                                    _age_sec = (_dt.now() - _prior_dt).total_seconds()
                                    _minutes_since_entry = _age_sec / 60.0
                                    if _age_sec < CONVICTION_EXIT_MIN_HOLD_SEC:
                                        log.info(
                                            f"🔇 EXIT GATED: {ticker} flip "
                                            f"{existing_dir}→{trade_direction} only "
                                            f"{_minutes_since_entry:.1f} min after entry "
                                            f"(need ≥{CONVICTION_EXIT_MIN_HOLD_SEC/60:.0f} min) — "
                                            f"treating as new entry, not exit"
                                        )
                                        # Treat as new entry: don't flag exit
                                        is_exit_signal = False
                                    else:
                                        is_exit_signal = True
                                except Exception:
                                    # Couldn't parse time — be conservative and
                                    # allow exit (old behavior)
                                    is_exit_signal = True
                            else:
                                # No time field — legacy record, allow exit
                                is_exit_signal = True
                        else:
                            # Prior was never posted to user → new entry, not exit
                            log.info(f"Direction flip {ticker}: {existing_dir}→{trade_direction} "
                                     f"but prior was never posted — treating as new entry")
            except Exception:
                pass

            # v7.2.1: conviction_dir write moved to AFTER all gates pass.
            # Previously stored eagerly here, causing direction to oscillate
            # as different expirations for the same ticker were scanned.
            # Now only written when a play is accepted and about to be returned.

            # Route by DTE
            if alert_dte <= 2:
                route = "immediate"
            elif alert_dte <= 7:
                route = "income"
            elif alert_dte <= 30:
                route = "swing"
            else:
                route = "stalk"

            # v8.5 (Phase 2, Gate C): IMMEDIATE AGGRESSIVENESS GATE
            # 0-2 DTE "CONVICTION PLAY" posts are highest-urgency alerts.
            # Require either a streaming sweep (urgency signal: filled across
            # exchanges) OR sustained flow (3+ consecutive 60s scans same
            # direction). "One scan, one big number" is where most false
            # positives live.
            if route == "immediate":
                _has_sweep     = alert.get("is_streaming_sweep", False)
                _has_sustained = alert.get("sustained_flow", False)
                if not _has_sweep and not _has_sustained:
                    log.info(f"💎 IMMEDIATE DROP: {ticker} {alert_dte}DTE "
                             f"— no sweep, no sustained flow (single-scan spike)")
                    continue

            # ── 0DTE NEVER ROUTES TO INCOME ──
            # 0DTE flow expires before the income spread does.
            # The flow signal and the trade vehicle are on different timelines.
            if alert_dte == 0 and route == "income":
                continue

            # ── POTTER BOX STRUCTURAL GATE ──
            # Flow must agree with box structure. Prevents fighting breakouts.
            # Void above + put flow = reactive noise (broken support holders)
            # Void above + call flow = confirms breakout (fire)
            # Void below + call flow = bottom-fishing gamblers (block)
            # Void below + put flow = confirms breakdown (fire)
            # Failed breakout + opposing flow + 3+ DTE = fire with context
            potter_box_gate = "pass"  # pass, block, context
            potter_context = ""
            potter_location = ""
            try:
                pb_data = self._state._json_get(f"potter_box:active:{ticker}")
                if pb_data:
                    box = pb_data.get("box") or pb_data  # handle both formats
                    pb_floor = box.get("floor", 0)
                    pb_roof = box.get("roof", 0)
                    spot_val = alert.get("spot", 0)

                    if pb_floor > 0 and pb_roof > 0 and spot_val > 0:
                        # Determine price location
                        if spot_val > pb_roof * 1.005:  # above roof (0.5% buffer)
                            potter_location = "void_above"
                            if trade_direction == "bearish":
                                if alert_dte <= 2:
                                    # 0-2DTE bearish in void above = reactive noise
                                    potter_box_gate = "block"
                                    potter_context = (
                                        f"📦 BLOCKED: {ticker} in void above Potter Box "
                                        f"(${pb_floor:.0f}-${pb_roof:.0f}). "
                                        f"Bearish 0DTE flow is reactive — hedging/profit-taking, "
                                        f"not directional conviction.")
                                elif alert_dte >= 3:
                                    # 3+ DTE bearish in void above = possible failed breakout
                                    # Only fire if we can confirm the breakout is failing
                                    potter_box_gate = "context"
                                    potter_context = (
                                        f"📦 CAUTION: {ticker} in void above box "
                                        f"(${pb_floor:.0f}-${pb_roof:.0f}). "
                                        f"Bearish {alert_dte}DTE flow may signal breakout failure. "
                                        f"Confirm price drops below ${pb_roof:.0f} before entry.")
                            else:
                                # Bullish flow in void above = confirms breakout
                                potter_context = (
                                    f"📦 BREAKOUT CONFIRMED: {ticker} above box "
                                    f"${pb_roof:.0f} with bullish flow — momentum aligned")

                        elif spot_val < pb_floor * 0.995:  # below floor
                            potter_location = "void_below"
                            if trade_direction == "bullish":
                                if alert_dte <= 2:
                                    potter_box_gate = "block"
                                    potter_context = (
                                        f"📦 BLOCKED: {ticker} in void below Potter Box "
                                        f"(${pb_floor:.0f}-${pb_roof:.0f}). "
                                        f"Bullish 0DTE flow is bottom-fishing, "
                                        f"not institutional conviction.")
                                elif alert_dte >= 3:
                                    potter_box_gate = "context"
                                    potter_context = (
                                        f"📦 CAUTION: {ticker} in void below box "
                                        f"(${pb_floor:.0f}-${pb_roof:.0f}). "
                                        f"Bullish {alert_dte}DTE flow may signal breakdown failure. "
                                        f"Confirm price recovers above ${pb_floor:.0f} before entry.")
                            else:
                                potter_context = (
                                    f"📦 BREAKDOWN CONFIRMED: {ticker} below box "
                                    f"${pb_floor:.0f} with bearish flow — momentum aligned")

                        else:  # Inside box
                            potter_location = "inside"
                            dist_to_floor = abs(spot_val - pb_floor) / spot_val * 100
                            dist_to_roof = abs(spot_val - pb_roof) / spot_val * 100

                            if dist_to_floor < 1.5 and trade_direction == "bullish":
                                potter_context = (
                                    f"📦 BOUNCE PLAY: {ticker} near box floor ${pb_floor:.0f} "
                                    f"with bullish flow — structural support")
                            elif dist_to_roof < 1.5 and trade_direction == "bearish":
                                potter_context = (
                                    f"📦 REJECTION PLAY: {ticker} near box ceiling ${pb_roof:.0f} "
                                    f"with bearish flow — structural resistance")
                            elif dist_to_floor < 1.5 and trade_direction == "bearish":
                                potter_context = (
                                    f"📦 ⚠️ Testing support: {ticker} near floor ${pb_floor:.0f} "
                                    f"— bearish flow may break or bounce")
                            elif dist_to_roof < 1.5 and trade_direction == "bullish":
                                potter_context = (
                                    f"📦 ⚠️ Testing resistance: {ticker} near ceiling ${pb_roof:.0f} "
                                    f"— bullish flow may break or reject")
            except Exception:
                pass

            # Apply Potter Box gate
            if potter_box_gate == "block":
                log.info(f"📦 Potter Box BLOCKED conviction: {ticker} {trade_side} "
                       f"({potter_location}, {alert_dte}DTE)")
                continue

            # Cooldown per ticker per route
            cooldown_key = f"conviction:{route}:{ticker}"
            if not self._state.check_and_set_cooldown(
                cooldown_key, CONVICTION_COOLDOWN
            ):
                log.info(f"⏳ COOLDOWN blocked conviction: {ticker} {trade_side} "
                         f"${alert['strike']:.0f} ({route}, {CONVICTION_COOLDOWN}s)")
                continue

            # ── SESSION-LEVEL DEDUP (Issues 2+4) ──
            # Check if we already have an active conviction position for this ticker.
            # Same direction re-fires → suppress (HOLD).
            # Direction flip → exit signal (handled above via is_exit_signal).
            # Significantly different strike → allow as new setup.
            session_action = self._check_conviction_session_state(
                ticker, trade_direction, alert["strike"], route)
            if session_action == "hold":
                existing_pos = self._conviction_positions.get(ticker, {})
                fire_ct = existing_pos.get("fire_count", 0)
                log.info(f"💎 DEDUP: {ticker} {trade_side} suppressed — "
                         f"same direction fire #{fire_ct + 1}, holding position")
                # Still count it for fire_count tracking
                self._update_conviction_session(ticker, trade_direction, alert["strike"], route)
                continue
            # "exit" is already handled by is_exit_signal logic above
            # "new" and "new_strike" proceed to full alert

            # ── PRE-MOVE FILTER ──
            # If stock moved >2% in last 30 min, flow is likely reactive
            # (hedging, profit-taking, closing losers) not predictive.
            recent_move_pct = self._get_recent_move_pct(ticker, lookback_min=30)
            is_reactive = recent_move_pct >= 2.0

            # v8.5 (Phase 2, Gate D): REACTIVE + IMMEDIATE = DROP
            # Reactive flow on 0-2 DTE is almost certainly hedging reaction to
            # the underlying move, not directional conviction. Promote the flag
            # to a gate for the immediate route only. Swing/income/stalk still
            # tolerate reactive flow (real institutions position into moves).
            if is_reactive and route == "immediate":
                log.info(f"💎 REACTIVE DROP: {ticker} moved {recent_move_pct:.1f}% "
                         f"in 30min — 0DTE flow is hedging, not directional")
                continue

            # Dollar estimate
            mid = alert.get("mid", 0) or 0
            if mid <= 0:
                mid = (alert.get("bid", 0) + alert.get("ask", 0)) / 2
            notional = volume * mid * 100

            # ── EM ALIGNMENT GATE ──
            # Check if flow direction agrees with the morning thesis.
            # Misaligned plays (flow fights EM card) are shadow-logged, not fired.
            em_alignment = self._check_em_alignment(ticker, trade_direction)
            is_em_aligned = em_alignment.get("aligned", True)
            is_em_conflict = em_alignment.get("conflict", False)

            # ── ATM STRIKE GUIDANCE ──
            # Recommend ATM or first ITM strike based on current spot
            spot = alert.get("spot", 0)
            if spot > 0:
                if trade_direction == "bullish":
                    # ATM call or first ITM (strike just below spot)
                    atm_strike = round(spot)  # nearest dollar
                    strike_guidance = f"Buy ATM CALL near ${atm_strike:.0f}"
                else:
                    atm_strike = round(spot)
                    strike_guidance = f"Buy ATM PUT near ${atm_strike:.0f}"
            else:
                strike_guidance = trade_side
                atm_strike = alert.get("strike", 0)

            # ── 1DTE VEHICLE for 0DTE plays ──
            # The institutional flow is the signal. Next expiry is the vehicle.
            # Avoids theta death spiral on late-morning entries.
            recommend_1dte = (alert_dte == 0)

            # Check for shadow signal convergence
            shadow = self._get_shadow_signal(ticker)
            has_shadow = shadow is not None
            shadow_agrees = False
            if has_shadow:
                sb = (shadow.get("bias", "") or "").lower()
                shadow_agrees = (
                    (trade_direction == "bullish" and "bull" in sb) or
                    (trade_direction == "bearish" and "bear" in sb)
                )

            # GEX convergence: is flow hitting a gamma level?
            gex_amplified = False
            gex_context = ""
            try:
                gamma_flip = self._state.get_gamma_flip_level(ticker)
                gex_sign = self._state.get_gex_sign(ticker)
                if gamma_flip > 0:
                    strike_val = alert["strike"]
                    spot_val = alert.get("spot", 0)
                    dist_to_flip_pct = abs(strike_val - gamma_flip) / spot_val * 100 if spot_val > 0 else 999

                    if dist_to_flip_pct < 1.5:  # strike within 1.5% of gamma flip
                        if gex_sign == "negative":
                            gex_amplified = True
                            gex_context = (f"GEX- amplified: flow at ${strike_val:.0f} "
                                         f"near gamma flip ${gamma_flip:.0f} — "
                                         f"dealer hedging will AMPLIFY move")
                        else:
                            gex_context = (f"GEX+ dampened: flow near gamma flip "
                                         f"${gamma_flip:.0f} — dealer hedging may CAP move")
            except Exception:
                pass

            # Re-hit data
            rehit_count = alert.get("rehit_count", 0)

            # IV skew data
            iv_skew = {}
            try:
                iv_skew = self._state._json_get(f"iv_skew:{ticker}") or {}
            except Exception:
                pass

            # VPOC data
            vpoc_data = {}
            vpoc_near = False
            try:
                vpoc_data = self._state._json_get(f"vpoc:{ticker}") or {}
                if vpoc_data.get("vpoc") and alert.get("spot"):
                    vpoc_dist = abs(alert["strike"] - vpoc_data["vpoc"]) / alert["spot"] * 100
                    vpoc_near = vpoc_dist < 1.0
            except Exception:
                pass

            # v8.5 Phase 3: signal lag instrumentation.
            # Measures seconds between when the underlying began moving in the
            # flow's direction and when the flow detector generated this alert.
            # None when the move can't be isolated (chop / no recent movement /
            # not enough spot history). Purely informational — no gating.
            _signal_lag_sec = None
            try:
                _alert_ts_iso = alert.get("timestamp")
                if _alert_ts_iso:
                    _alert_ts = datetime.fromisoformat(_alert_ts_iso).timestamp()
                    _move_start = self._detect_move_start(
                        ticker, trade_direction, lookback_min=30
                    )
                    if _move_start:
                        _signal_lag_sec = max(0, int(_alert_ts - _move_start))
            except Exception:
                pass

            play = {
                "ticker": ticker,
                "strike": alert["strike"],
                "side": side,
                "trade_side": trade_side,
                "trade_direction": trade_direction,
                "trade_emoji": trade_emoji,
                "volume": volume,
                "oi": alert.get("oi", 0),
                "vol_oi_ratio": vol_oi,
                "burst": burst,
                "dte": alert_dte,
                "route": route,
                "expiry": alert.get("expiry", ""),
                "spot": alert.get("spot", 0),
                "mid": mid,
                "ask": alert.get("ask", 0),
                "notional": notional,
                "directional_bias": direction,
                "has_shadow": has_shadow,
                "shadow_agrees": shadow_agrees,
                "shadow_signal": shadow,
                "gex_amplified": gex_amplified,
                "gex_context": gex_context,
                "rehit_count": rehit_count,
                "iv_skew": iv_skew,
                "vpoc_data": vpoc_data,
                "vpoc_near": vpoc_near,
                # New fields
                "is_exit_signal": is_exit_signal,
                "exit_prior_was_posted": _exit_prior_was_posted,  # v7.2: was prior entry shown to user?
                "is_reactive": is_reactive,
                "recent_move_pct": round(recent_move_pct, 1),
                "recommend_1dte": recommend_1dte,
                "strike_guidance": strike_guidance,
                "potter_box_gate": potter_box_gate,
                "potter_context": potter_context,
                # v7.0: Enhanced direction data
                "direction_confidence": dir_conf,
                "sustained_flow": alert.get("sustained_flow", False),
                "velocity_count": alert.get("velocity_count", 0),
                "vol_per_min": alert.get("vol_per_min", 0),
                "potter_location": potter_location,
                # Issue 2+4: session dedup state
                "session_action": session_action,  # "new" or "new_strike"
                # streaming sweep flag
                "is_streaming_sweep": alert.get("is_streaming_sweep", False),
                "sweep_notional": alert.get("sweep_notional", 0),
                # EM alignment gate
                "em_aligned": is_em_aligned,
                "em_conflict": is_em_conflict,
                "em_thesis_bias": em_alignment.get("thesis_bias", ""),
                "em_thesis_score": em_alignment.get("thesis_score", 0),
                "em_detail": em_alignment.get("detail", ""),
                "em_gex_sign": em_alignment.get("gex_sign", ""),
                "em_regime": em_alignment.get("regime", ""),
                # Shadow-only flag: misaligned plays are logged but not posted.
                # Only applies to DTE < 5 — EM card is intraday context.
                # Longer-dated institutional positioning (swing/stalk) operates
                # on a different timeframe and should not be gated by intraday structure.
                "is_shadow_only": is_em_conflict and alert_dte < 5,
                # v8.5 Phase 3: latency instrumentation (None if indeterminate)
                "signal_lag_sec": _signal_lag_sec,
            }

            # ── Issue 3: Resolve recommended contract details ──
            # When flow side differs from trade side (e.g. put flow → LONG CALL),
            # look up the actual recommended contract's bid/ask/mid from streaming.
            rec_side = "call" if trade_direction == "bullish" else "put"
            if rec_side != side and self._option_store:
                rec_quote = self.get_streaming_overlay(ticker, atm_strike, rec_side)
                if rec_quote:
                    _rb = rec_quote.get("bid", 0) or 0
                    _ra = rec_quote.get("ask", 0) or 0
                    play["rec_bid"] = _rb
                    play["rec_ask"] = _ra
                    # FIX BUG #9: only compute a two-sided mid when both bid>0 and ask>0.
                    # Previously (bid + ask) / 2 with bid=0 produced a fake midpoint
                    # like Ask=1.16, Mid=0.58. When only ask is available, use ask as
                    # the best-available mark (shows a conservative execution price).
                    if _rb > 0 and _ra > 0:
                        play["rec_mid"] = round((_rb + _ra) / 2, 2)
                    elif _ra > 0:
                        play["rec_mid"] = _ra  # one-sided: don't fabricate a midpoint
                    else:
                        play["rec_mid"] = 0
                    play["rec_strike"] = atm_strike
                    play["rec_side"] = rec_side
                    play["has_rec_contract"] = True

            # ── 1DTE contract price lookup ──
            # When we recommend 1DTE for theta protection, look up the actual
            # 1DTE contract's bid/ask so the card shows the right price.
            if recommend_1dte and self._option_store:
                try:
                    from schwab_stream import build_occ_symbol, parse_occ_symbol
                    _1dte_date = date.today() + timedelta(days=1)
                    # Skip weekends
                    while _1dte_date.weekday() >= 5:
                        _1dte_date += timedelta(days=1)
                    _1dte_exp = _1dte_date.strftime("%Y-%m-%d")
                    _1dte_side = rec_side  # same side as recommended
                    _1dte_sym = build_occ_symbol(ticker, _1dte_exp, _1dte_side, atm_strike)
                    _1dte_q = self._option_store.get(_1dte_sym)
                    if _1dte_q:
                        _1b = _1dte_q.get("bid", 0) or 0
                        _1a = _1dte_q.get("ask", 0) or 0
                        if _1a > 0:
                            play["dte1_bid"] = _1b
                            play["dte1_ask"] = _1a
                            # FIX BUG #9: only compute mid when both sides valid.
                            # Pre-fix: Ask=1.16, Bid=0 produced Mid=0.58 (fake midpoint).
                            # Post-fix: one-sided quote → mid = ask (conservative mark).
                            if _1b > 0 and _1a > 0:
                                play["dte1_mid"] = round((_1b + _1a) / 2, 2)
                            else:
                                play["dte1_mid"] = _1a  # one-sided, use ask as mark
                            play["dte1_expiry"] = _1dte_exp
                            play["dte1_strike"] = atm_strike
                            play["dte1_side"] = _1dte_side
                            play["has_1dte_quote"] = True
                            log.info(f"1DTE quote for {ticker}: {_1dte_sym} "
                                     f"bid=${_1b:.2f} ask=${_1a:.2f}"
                                     + (" (one-sided: mid=ask)" if _1b <= 0 else ""))
                except Exception as _1e:
                    log.debug(f"1DTE lookup for {ticker}: {_1e}")

            # ── Issue 1: Wire conviction into thesis monitor ──
            # v7.2.1: Check return value BEFORE appending to plays[].
            # If thesis_monitor says "already tracking this ticker+direction",
            # the play must NOT be returned — otherwise it gets posted to Telegram again.
            if self._thesis_monitor_fn and route in ("immediate", "swing"):
                try:
                    _tm_accepted = self._thesis_monitor_fn(play)
                    if _tm_accepted is False:
                        # Already tracking — update fire count but don't return play
                        self._update_conviction_session(ticker, trade_direction, alert["strike"], route)
                        continue
                except Exception as _tm_err:
                    log.debug(f"Conviction → thesis monitor failed: {_tm_err}")

            # ── v7.2.1: Store direction ONLY after all gates passed ──
            # Previously stored eagerly before routing/dedup/cooldown checks,
            # which caused direction oscillation across expirations.
            try:
                self._state._json_set(f"conviction_dir:{ticker}", {
                    "direction": trade_direction,
                    "strike": alert["strike"],
                    "time": datetime.now().isoformat(),
                    "posted": False,  # upgraded to True by confirm_conviction_posted()
                }, ttl=14400)
            except Exception:
                pass

            # ── Issue 2+4: Update session state after successful fire ──
            self._update_conviction_session(ticker, trade_direction, alert["strike"], route)

            # All gates passed — add to return list
            plays.append(play)

        return plays

    def _get_shadow_signal(self, ticker: str) -> Optional[dict]:
        """Get stored shadow signal for a ticker (intraday, 4hr TTL)."""
        try:
            return self._state._json_get(f"shadow:{ticker.upper()}")
        except Exception:
            return None

    def format_conviction_play(self, play: dict) -> str:
        """Format conviction play — adapts by route tier."""
        ticker = play["ticker"]
        strike = play["strike"]
        side = play["side"]
        trade_side = play["trade_side"]
        emoji = play["trade_emoji"]
        route = play["route"]
        exp_str = str(play.get("expiry", ""))[:10]
        dte = play.get("dte", 0)
        dte_label = f"{dte}DTE" if dte > 0 else "0DTE"
        is_exit = play.get("is_exit_signal", False)
        is_reactive = play.get("is_reactive", False)
        recommend_1dte = play.get("recommend_1dte", False)
        strike_guidance = play.get("strike_guidance", trade_side)

        notional = play.get("notional", 0)
        if notional >= 1_000_000:
            notional_str = f"${notional / 1_000_000:.1f}M"
        elif notional >= 1_000:
            notional_str = f"${notional / 1_000:.0f}K"
        else:
            notional_str = f"${notional:.0f}"

        # v7.2.1: Show .50 strikes accurately (e.g., $207.50 not $208)
        # Defined here so it's available in both exit and normal paths.
        _fmt_strike = lambda v: f"${v:.2f}" if v % 1 != 0 else f"${v:.0f}"

        # ── EXIT SIGNAL: opposite direction on same ticker ──
        if is_exit:
            header = f"🔄 EXIT SIGNAL — {ticker}"
            lines = [
                header,
                "━" * 28,
                f"⚠️ Institutions REVERSED on {ticker}",
                f"⚡ {play['volume']:,} contracts at {_fmt_strike(strike)} {side.upper()} "
                f"({play['vol_oi_ratio']:.0f}x vol/OI)",
                f"💰 Notional: {notional_str}",
                f"Direction: {play['directional_bias']}",
                "",
                f"🎯 ACTION: Close existing {ticker} position",
                f"Flow flipped — prior direction no longer supported by institutional money.",
                f"💵 Spot: ${play['spot']:.2f}",
            ]
            return "\n".join(lines)

        # Route-specific header and action
        if route == "immediate":
            header = f"💎🚨 CONVICTION PLAY — {ticker} {emoji}"
            action = f"🎯 ACTION: {strike_guidance}"
            urgency = f"⚠️ Institutions put {notional_str} on a {dte_label} {side}. They expect the move TODAY."
        elif route == "income":
            header = f"💎 FLOW CONVICTION — {ticker} (INCOME)"
            action = f"🎯 Income setup: {dte_label} expiry — auto-scoring below"
            urgency = f"📊 {notional_str} institutional flow at {_fmt_strike(strike)}. Short-term thesis."
        elif route == "swing":
            header = f"💎 FLOW CONVICTION — {ticker} (SWING)"
            action = f"🎯 Swing setup: {dte}D expiry aligns with swing hold horizon"
            urgency = f"📊 {notional_str} institutional positioning through {exp_str}."
        else:  # stalk
            header = f"💎 FLOW CONVICTION — {ticker} (CAMPAIGN)"
            action = f"🎯 Watchlist: {dte}D institutional campaign building"
            urgency = f"📊 {notional_str} positioned through {exp_str}. Track for entry."

        lines = [
            header,
            "━" * 28,
        ]

        # v7.1: YOUR TRADE line — prominently shows what YOU should trade
        # This prevents confusion when flow shows "PUT" but direction is bullish
        # (institutions selling puts = bullish, but user sees "PUT" and thinks bearish)
        trade_direction = play.get("trade_direction", "")
        _your_side = "CALL" if trade_direction == "bullish" else "PUT"
        _your_verb = "BULLISH" if trade_direction == "bullish" else "BEARISH"
        _your_strike = play.get("rec_strike", strike)
        lines.append(f"🎯 YOUR TRADE: Buy {_your_side} — Institutions are {_your_verb}")
        if play.get("rec_strike") and play.get("rec_strike") != strike:
            lines.append(f"   Recommended: {_fmt_strike(play['rec_strike'])} {_your_side} | Flow strike: {_fmt_strike(strike)}")
        else:
            lines.append(f"   Strike: {_fmt_strike(_your_strike)} {_your_side}")
        lines.append("━" * 28)

        # Flow data (what institutions did — NOT what you trade)
        lines.append(
            f"⚡ Flow: {play['volume']:,} contracts at {_fmt_strike(strike)} {side.upper()} "
            f"({play['vol_oi_ratio']:.0f}x vol/OI)"
        )
        lines.append(f"💰 Notional: {notional_str}")

        burst = play.get("burst", 0)
        if play.get("is_streaming_sweep"):
            sweep_notional = play.get("sweep_notional", 0)
            lines.append(f"⚡ REAL-TIME SWEEP: ${sweep_notional:,.0f} notional detected via streaming")
        elif burst >= CONVICTION_MIN_BURST:
            lines.append(f"Burst: +{burst:,} in last interval")
        else:
            lines.append(f"Buildup: {play['vol_oi_ratio']:.0f}x cumulative session vol/OI")

        lines.append(f"Direction: {play['directional_bias']}")

        # v7.0: Direction confidence + velocity indicators
        dir_conf = play.get("direction_confidence", 0)
        if dir_conf >= 0.75:
            lines.append(f"🎯 Direction confidence: HIGH ({dir_conf:.0%}) — trade side confirmed at bid/ask")
        elif dir_conf >= 0.5:
            lines.append(f"📊 Direction confidence: MODERATE ({dir_conf:.0%})")

        if play.get("sustained_flow"):
            vel_count = play.get("velocity_count", 0)
            vol_pm = play.get("vol_per_min", 0)
            lines.append(f"🔥 SUSTAINED FLOW: {vel_count} consecutive snapshots same direction"
                        + (f" ({vol_pm:,}/min)" if vol_pm > 0 else ""))
        elif play.get("vol_per_min", 0) > 500:
            lines.append(f"⚡ Sweep velocity: {play['vol_per_min']:,} contracts/min")

        # ── PRE-MOVE WARNING ──
        if is_reactive:
            move_pct = play.get("recent_move_pct", 0)
            lines.append(f"")
            lines.append(f"⚠️ REACTIVE FLOW: {ticker} moved {move_pct:.1f}% "
                        f"in last 30 min — likely hedging/profit-taking, not new conviction")

        # Shadow signal convergence
        if play.get("shadow_agrees"):
            shadow = play.get("shadow_signal", {})
            lines.append(f"")
            lines.append(f"🔗 CONFIRMED by shadow signal: {shadow.get('bias','')} "
                        f"(score {shadow.get('score','?')}, HTF {shadow.get('htf','')})")

        # GEX convergence
        if play.get("gex_amplified"):
            lines.append(f"⚡ {play['gex_context']}")
        elif play.get("gex_context"):
            lines.append(f"📐 {play['gex_context']}")

        # Re-hit indicator
        rehit = play.get("rehit_count", 0)
        if rehit >= 3:
            lines.append(f"🔄 MULTI-HIT #{rehit}: institutions returning to this strike repeatedly")
        elif rehit >= 2:
            lines.append(f"🔄 RE-HIT #{rehit}: same strike hit again — consensus building")

        # IV skew context
        skew = play.get("iv_skew", {})
        if skew.get("skew_extreme"):
            lines.append(f"📊 STEEP PUT SKEW: {skew['skew_ratio']:.2f}x "
                        f"(25d put IV {skew['put_25d_iv']*100:.0f}% vs "
                        f"call {skew['call_25d_iv']*100:.0f}%) — institutional fear signal")
        elif skew.get("skew_direction") == "put_heavy":
            lines.append(f"📊 Put skew {skew.get('skew_ratio',1):.2f}x — moderate protection buying")

        # VPOC convergence
        if play.get("vpoc_near"):
            vpoc = play.get("vpoc_data", {})
            lines.append(f"📍 Flow near VPOC ${vpoc.get('vpoc',0):.2f} — volume gravitational center")

        # EM card alignment context
        em_detail = play.get("em_detail", "")
        if em_detail:
            lines.append(f"")
            if play.get("is_shadow_only"):
                lines.append(f"📋 EM Card: {em_detail}")
                lines.append(f"🔇 SHADOW ONLY — flow fights strong EM thesis on short-dated play")
            elif play.get("em_conflict") and play.get("dte", 0) >= 5:
                # Long-dated flow fighting intraday EM = strategic institutional positioning
                lines.append(f"📋 EM Card: {em_detail}")
                lines.append(f"🏗️ STRATEGIC: Institutions positioning against intraday structure — "
                             f"multi-day thesis, consider building throughout session")
            else:
                lines.append(f"📋 EM Card: {em_detail}")

        # Potter Box structural context
        potter_ctx = play.get("potter_context", "")
        if potter_ctx:
            lines.append(f"")
            lines.append(potter_ctx)

        # Earnings warning
        if play.get("earnings_in_window"):
            lines.append(f"")
            lines.append(f"⚠️ EARNINGS WARNING: {play.get('earnings_note', 'earnings within expiry window')}")

        # ── 1DTE VEHICLE RECOMMENDATION ──
        # Issue 3: When we have resolved recommended contract details, show those
        has_rec = play.get("has_rec_contract", False)
        rec_ask = play.get("rec_ask", 0)
        rec_mid = play.get("rec_mid", 0)
        rec_strike_val = play.get("rec_strike", 0)
        rec_side_label = play.get("rec_side", "").upper()

        if recommend_1dte and route == "immediate":
            lines += [
                "", action,
                f"📅 Flow detected on: {exp_str} (0DTE)",
                f"👉 Recommended: Trade 1DTE for theta protection",
            ]
            # Show 1DTE contract price if available from streaming
            has_1dte = play.get("has_1dte_quote", False)
            if has_1dte:
                _1side = play.get("dte1_side", "").upper()
                _1strike = play.get("dte1_strike", 0)
                _1ask = play.get("dte1_ask", 0)
                _1mid = play.get("dte1_mid", 0)
                _1exp = play.get("dte1_expiry", "")
                lines.append(f"💵 1DTE {_1side} {_fmt_strike(_1strike)} ({_1exp}): "
                             f"Ask ${_1ask:.2f} (Mid ${_1mid:.2f}) | Spot: ${play['spot']:.2f}")
                # Also show 0DTE for comparison
                lines.append(f"   (0DTE reference: Ask ${play.get('ask', 0):.2f})")
            elif has_rec and rec_ask > 0:
                lines.append(f"💵 Rec {rec_side_label} {_fmt_strike(rec_strike_val)}: "
                             f"Ask ${rec_ask:.2f} (Mid ${rec_mid:.2f}) | Spot: ${play['spot']:.2f}")
                lines.append(f"   ⚠️ 1DTE quote unavailable — price shown is 0DTE")
            else:
                lines.append(f"💵 Ask (0DTE): ${play.get('ask', 0):.2f} | Spot: ${play['spot']:.2f}")
                lines.append(f"   ⚠️ 1DTE quote unavailable — price shown is 0DTE")
        else:
            lines += ["", action, f"📅 Expiry: {exp_str} ({dte_label})"]
            if has_rec and rec_ask > 0:
                lines.append(f"💵 Rec {rec_side_label} {_fmt_strike(rec_strike_val)}: "
                             f"Ask ${rec_ask:.2f} (Mid ${rec_mid:.2f}) | Spot: ${play['spot']:.2f}")
            elif has_rec:
                # FIX BUG #8: we have a recommended contract on a different side
                # from the flow trigger, but the rec contract's live quote is
                # unavailable. Do NOT fall through to play["ask"] — that's the
                # wrong-side flow-trigger price (e.g. put ask when recommending
                # a call). Show a clear unavailable marker instead.
                lines.append(f"💵 Rec {rec_side_label} {_fmt_strike(rec_strike_val)}: "
                             f"price unavailable (stream miss) | Spot: ${play['spot']:.2f}")
                lines.append(f"   ⚠️ Quote contract live; price will populate on next tick")
            else:
                # No rec contract resolved — flow side matches trade side, so
                # play["ask"] IS the right contract. Safe to use.
                lines.append(f"💵 Ask: ${play.get('ask', 0):.2f} | Spot: ${play['spot']:.2f}")

        # v7.2.1: Consolidated contract summary — strike, expiry, cost in plain text
        _contract_side = "CALL" if trade_direction == "bullish" else "PUT"
        if play.get("has_1dte_quote"):
            _c_strike = play.get("dte1_strike", 0)
            _c_exp = play.get("dte1_expiry", "")
            _c_ask = play.get("dte1_ask", 0)
            _c_mid = play.get("dte1_mid", 0)
        elif play.get("has_rec_contract"):
            _c_strike = play.get("rec_strike", 0)
            _c_exp = exp_str
            _c_ask = play.get("rec_ask", 0)
            _c_mid = play.get("rec_mid", 0)
        else:
            _c_strike = _your_strike
            _c_exp = exp_str
            _c_ask = play.get("ask", 0)
            _c_mid = play.get("mid", 0)

        if _c_strike > 0 and _c_exp:
            _cost_part = ""
            if _c_ask > 0 and _c_mid > 0:
                _cost_part = f" — Ask ${_c_ask:.2f} (Mid ${_c_mid:.2f})"
            elif _c_ask > 0:
                _cost_part = f" — Ask ${_c_ask:.2f}"
            elif _c_mid > 0:
                _cost_part = f" — Est ${_c_mid:.2f}"
            lines.append(f"📋 Contract: {_fmt_strike(_c_strike)} {_contract_side} — Exp {_c_exp}{_cost_part}")

        # Issue 5: Suggest spread alternative for defined-risk
        if route in ("immediate", "swing") and play.get("spot", 0) > 0:
            lines.append(f"📊 For defined risk: run spread engine on {trade_side.split()[-1].lower()} debit spread")

        lines += ["", urgency]

        return "\n".join(lines)

    @staticmethod
    def _dte_label(expiry: str) -> tuple[int, str, str]:
        """Return (dte, label, active_status) for an option expiration."""
        try:
            exp_dt = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
            dte = (exp_dt - date.today()).days
        except Exception:
            return -999, "DTE?", "unknown"
        if dte < 0:
            return dte, f"EXPIRED {abs(dte)}D ago", "expired"
        if dte == 0:
            return dte, "0DTE", "intraday only"
        if dte <= 3:
            return dte, f"{dte}DTE", "very short-term"
        if dte <= 10:
            return dte, f"{dte}DTE", "weekly"
        if dte <= 30:
            return dte, f"{dte}DTE", "swing"
        return dte, f"{dte}DTE", "larger thesis"

    @staticmethod
    def _strike_context_line(ticker: str, strike: float, side: str, expiry: str,
                             spot: float, oi_change: int, oi_change_pct: float,
                             flow_type: str = "buildup") -> str:
        """Plain-English DTE + moneyness context for morning OI confirmations."""
        dte, dte_label, active = FlowDetector._dte_label(expiry)
        try:
            strike_f = float(strike)
            spot_f = float(spot or 0)
            if spot_f > 0:
                dist = abs(strike_f - spot_f) / spot_f * 100.0
                loc = "above" if strike_f > spot_f else "below"
                if dist < 0.35:
                    money = "ATM"
                else:
                    if side == "call":
                        money = "OTM" if strike_f > spot_f else "ITM"
                    else:
                        money = "OTM" if strike_f < spot_f else "ITM"
                mny = f" — {money} {dist:.1f}% {loc} spot ${spot_f:.2f}"
            else:
                mny = ""
        except Exception:
            mny = ""
        if active == "expired":
            read = "historical only; expiration has passed"
        elif dte == 0:
            read = "intraday-only positioning"
        elif dte <= 3 and dte >= 1:
            read = "short-term momentum / expiry magnet"
        elif dte <= 10 and dte >= 4:
            read = "weekly directional positioning"
        elif dte <= 30 and dte >= 11:
            read = "swing positioning"
        else:
            read = active
        side_emoji = "📗" if side == "call" else "📕"
        flow_word = "OI" if flow_type != "unwinding" else "OI unwind"
        return (
            f"  {side_emoji} {ticker} ${float(strike):.0f} {str(side).upper()} "
            f"exp {str(expiry)[:10]} ({dte_label}, {active}){mny} — "
            f"{flow_word} {int(oi_change):+,} ({float(oi_change_pct):+.0f}%) | {read}"
        )

    # ─────────────────────────────────────────────────────
    # FORMATTING
    # ─────────────────────────────────────────────────────

    def format_intraday_alert(self, alert: dict) -> str:
        """Format a single intraday flow alert for Telegram."""
        level = alert["flow_level"]
        if level == "extreme":
            emoji = "🚨"
            label = "EXTREME FLOW"
        elif level == "significant":
            emoji = "🔥"
            label = "SIGNIFICANT FLOW"
        else:
            emoji = "📊"
            label = "NOTABLE FLOW"

        side_emoji = "📗" if alert["side"] == "call" else "📕"
        dist_dir = "above" if alert["dist_from_spot_pct"] > 0 else "below"

        burst_tag = " ⚡ BURST" if alert.get("is_burst") else ""
        new_tag = " 🆕 NEW STRIKE" if alert.get("is_new_strike") else ""

        lines = [
            f"{emoji} {label} — {alert['ticker']}{burst_tag}{new_tag}",
            "━" * 28,
            f"{side_emoji} ${alert['strike']:.0f} {alert['side'].upper()} "
            f"({abs(alert['dist_from_spot_pct']):.1f}% {dist_dir} spot ${alert['spot']:.2f})",
            f"Volume: {alert['volume']:,} | OI: {alert['oi']:,} "
            f"({alert['vol_oi_ratio']:.1f}x turnover)",
            f"Exp: {alert.get('expiry', 'N/A')}",
            f"Direction: {alert['directional_bias']}",
            f"Book: {alert['book_imbalance'].replace('_', ' ')}",
        ]

        if alert.get("is_burst"):
            lines.append(f"Burst: +{alert['burst']:,} contracts in last interval")

        return "\n".join(lines)

    def format_sweep_alert(self, sweep: dict) -> str:
        """Format a real-time streaming sweep alert for Telegram."""
        side_emoji = "📗" if sweep.get("side") == "call" else "📕"
        sweep_dir = sweep.get("sweep_side", "unknown")
        if sweep_dir == "buy":
            dir_emoji = "🟢"
            dir_label = "BUY SWEEP"
        elif sweep_dir == "sell":
            dir_emoji = "🔴"
            dir_label = "SELL SWEEP"
        else:
            dir_emoji = "⚪"
            dir_label = "SWEEP"

        notional = sweep.get("sweep_notional", 0) or sweep.get("notional", 0)
        vol = sweep.get("volume", 0) or sweep.get("volume_delta", 0)
        delta = sweep.get("sweep_delta", 0) or sweep.get("delta", 0)
        iv = sweep.get("sweep_iv", 0) or sweep.get("iv", 0)

        lines = [
            f"⚡ {dir_emoji} {dir_label} — {sweep['ticker']}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"{side_emoji} {sweep.get('strike', 0)} {sweep.get('side', '').upper()} | "
            f"Exp: {sweep.get('expiry', 'N/A')}",
            f"📊 Volume: {vol:,} contracts | ${notional:,.0f} notional",
        ]
        if delta:
            lines.append(f"Δ={delta:.2f} | IV={iv:.1f}%")
        bid = sweep.get("bid", 0)
        ask = sweep.get("ask", 0)
        last = sweep.get("last", 0)
        if bid and ask:
            lines.append(f"Bid/Ask: ${bid:.2f}/${ask:.2f} | Last: ${last:.2f}")

        return "\n".join(lines)

    def format_confirmation_summary(self, confirmations: List[dict],
                                     rolls: List[dict] = None,
                                     sector_flow: List[dict] = None) -> str:
        """Format morning OI confirmation summary for Telegram."""
        if not confirmations:
            return ""

        buildups = [c for c in confirmations if c["flow_type"] == "confirmed_buildup"]
        unwinds = [c for c in confirmations if c["flow_type"] == "confirmed_unwinding"]

        lines = [
            "🏛️ MORNING OI CONFIRMATION",
            "━" * 28,
            f"Checked {len(confirmations)} volume flags from yesterday",
        ]

        if buildups:
            lines.append("")
            lines.append("✅ CONFIRMED BUILDUP (new positions opened):")
            for c in buildups[:5]:
                side_emoji = "📗" if c["side"] == "call" else "📕"
                div_tag = " 🔀 DIVERGENCE" if c.get("divergence") else ""
                campaign = c.get("campaign", {})
                camp_tag = ""
                if campaign.get("consecutive_days", 0) >= CAMPAIGN_STRONG_DAYS:
                    camp_tag = f" 🏗️ {campaign['consecutive_days']}D CAMPAIGN"
                elif campaign.get("consecutive_days", 0) >= CAMPAIGN_MIN_DAYS:
                    camp_tag = f" 📅 Day {campaign['consecutive_days']}"
                _ctx_line = self._strike_context_line(
                    c['ticker'], c['strike'], c['side'], c['expiry'],
                    c.get('today_spot') or c.get('yesterday_spot') or 0,
                    c['oi_change'], c['oi_change_pct'], flow_type="buildup",
                )
                lines.append(_ctx_line + camp_tag + div_tag)

        if unwinds:
            lines.append("")
            lines.append("🔻 CONFIRMED UNWINDING (positions closed):")
            for c in unwinds[:5]:
                side_emoji = "📗" if c["side"] == "call" else "📕"
                _ctx_line = self._strike_context_line(
                    c['ticker'], c['strike'], c['side'], c['expiry'],
                    c.get('today_spot') or c.get('yesterday_spot') or 0,
                    c['oi_change'], c['oi_change_pct'], flow_type="unwinding",
                )
                lines.append(_ctx_line)

        if rolls:
            lines.append("")
            lines.append("🔄 ROLLS DETECTED:")
            for r in rolls[:3]:
                lines.append(
                    f"  {r['ticker']} {r['side'].upper()} "
                    f"${r['from_strike']:.0f} → ${r['to_strike']:.0f} "
                    f"(~{r['contracts']:,} contracts {r['direction']})"
                )
                lines.append(f"    → {r['signal']}")

        if sector_flow:
            lines.append("")
            lines.append("🏭 SECTOR FLOW:")
            for sf in sector_flow[:3]:
                tickers = ", ".join(t["ticker"] for t in sf["tickers"])
                lines.append(
                    f"  {sf['sector']}: {sf['direction'].upper()} "
                    f"across {len(sf['tickers'])} names ({tickers})"
                )

        return "\n".join(lines)

    def format_stalk_alert(self, stalk: dict) -> str:
        """Format a stalk alert for Telegram."""
        t = stalk
        ticker = t["ticker"]

        if t["stalk_type"] == "do_not_chase":
            header = f"👁️ STALK ALERT — {ticker} (DO NOT CHASE)"
            action = "⛔ Do NOT chase. Wait for pullback."
        elif t["stalk_type"] == "watch_for_trigger":
            header = f"👁️ STALK ALERT — {ticker} (WATCH FOR TRIGGER)"
            action = "🎯 Watch for trigger. Institutions positioned but move hasn't fired."
        else:
            header = f"👁️ STALK ALERT — {ticker} (ROOM LEFT)"
            action = f"📍 Room remaining. Partial move ({t['price_change_pct']:+.1f}%)."

        side_emoji = "📗" if t["side"] == "call" else "📕"
        div_tag = "\n🔀 DIVERGENCE — institutions buying into weakness = accumulation" if t.get("divergence") else ""

        campaign_tag = ""
        if t.get("campaign_days", 0) >= CAMPAIGN_STRONG_DAYS:
            campaign_tag = f"\n🏗️ INSTITUTIONAL CAMPAIGN — {t['campaign_days']} consecutive days, {t['campaign_total_oi']:+,} total OI"
        elif t.get("campaign_days", 0) >= CAMPAIGN_MIN_DAYS:
            campaign_tag = f"\n📅 Persistent flow — Day {t['campaign_days']}"

        lines = [
            header,
            "━" * 28,
            f"✅ Confirmed {t['flow_type'].replace('confirmed_', '')} yesterday: "
            f"OI {t['oi_change']:+,} ({t['oi_change_pct']:+.0f}%) "
            f"at ${t['strike']:.0f} {t['side'].upper()} ({t['expiry']})",
            f"Price since: {t['price_change_pct']:+.1f}%"
            + (f" (spot ${t['today_spot']:.2f})" if t.get("today_spot") else ""),
            "",
            action,
            f"{side_emoji} Direction: {t['expected_direction']}",
        ]

        if div_tag:
            lines.append(div_tag)
        if campaign_tag:
            lines.append(campaign_tag)

        # Add support/resistance levels
        supports = t.get("support_levels", [])
        resistances = t.get("resistance_levels", [])
        if supports:
            lines.append("")
            for s in supports[:2]:
                if isinstance(s, dict):
                    lines.append(f"📍 Support: ${s.get('price', 0):.2f} ({s.get('label', '')})")
                else:
                    lines.append(f"📍 Support: ${s:.2f}")
        if resistances:
            for r in resistances[:2]:
                if isinstance(r, dict):
                    lines.append(f"📍 Resistance: ${r.get('price', 0):.2f} ({r.get('label', '')})")
                else:
                    lines.append(f"📍 Resistance: ${r:.2f}")

        return "\n".join(lines)

    def format_flow_trade_idea(self, idea: dict) -> str:
        """Format a flow-generated trade idea for Telegram."""
        trigger = idea["flow_trigger"]
        trade = "Bull Put Spread" if idea["trade_type"] == "bull_put" else "Bear Call Spread"
        dir_emoji = "🟢" if idea["trade_type"] == "bull_put" else "🔴"
        exp_str = str(idea.get("recommended_expiry", ""))[:10]

        lines = [
            f"🚨 FLOW-GENERATED INCOME IDEA — {idea['ticker']}",
            "━" * 28,
            f"Trigger: {trigger['volume']:,} {trigger['side']}s at "
            f"${trigger['strike']:.0f} ({trigger['vol_oi_ratio']:.1f}x vol/OI)",
            f"Direction: {trigger['directional_bias']}",
            "",
            f"{dir_emoji} Suggested: {trade} "
            f"${idea['suggested_short_strike']:.0f}/${idea['suggested_long_strike']:.0f}",
            f"🧭 Recommended expiry: {exp_str} "
            f"(where flow concentrated — strong edge)",
            "",
            f"📋 Run: /score {idea['ticker']} {idea['suggested_short_strike']:.0f} {exp_str}",
        ]
        return "\n".join(lines)

    def format_flow_ideas_digest(self, ideas: List[dict]) -> List[str]:
        """Batch flow income ideas into digest messages, chunked to fit Telegram's 4096 char limit."""
        if not ideas:
            return []

        # Build all idea lines
        idea_lines = []
        for idea in ideas:
            trigger = idea["flow_trigger"]
            trade = "BPS" if idea["trade_type"] == "bull_put" else "BCS"
            dir_emoji = "🟢" if idea["trade_type"] == "bull_put" else "🔴"
            exp_str = str(idea.get("recommended_expiry", ""))[:10]

            idea_lines.append(
                f"{dir_emoji} {idea['ticker']} — {trade} "
                f"${idea['suggested_short_strike']:.0f}/${idea['suggested_long_strike']:.0f} "
                f"({trigger['volume']:,} {trigger['side']}s @ ${trigger['strike']:.0f}, "
                f"{trigger['vol_oi_ratio']:.1f}x)\n"
                f"   /score {idea['ticker']} {idea['suggested_short_strike']:.0f} {exp_str}"
            )

        # Chunk into messages under 3800 chars (leave margin for header)
        messages = []
        chunk_lines = []
        chunk_len = 0
        chunk_count = 0

        for line in idea_lines:
            line_len = len(line) + 1  # +1 for newline
            if chunk_len + line_len > 3600 and chunk_lines:
                # Flush current chunk
                chunk_count += 1
                header = f"🚨 FLOW INCOME IDEAS ({len(ideas)} total, part {chunk_count})\n" + "━" * 28
                messages.append(header + "\n" + "\n".join(chunk_lines))
                chunk_lines = []
                chunk_len = 0
            chunk_lines.append(line)
            chunk_len += line_len

        # Flush remaining
        if chunk_lines:
            chunk_count += 1
            if chunk_count == 1:
                header = f"🚨 FLOW INCOME IDEAS — {len(ideas)} setups\n" + "━" * 28
            else:
                header = f"🚨 FLOW INCOME IDEAS ({len(ideas)} total, part {chunk_count})\n" + "━" * 28
            messages.append(header + "\n" + "\n".join(chunk_lines))

        return messages

    def format_grouped_flow_alerts(self, alerts: List[dict]) -> List[str]:
        """
        Group flow alerts by ticker into one card per ticker.
        Returns list of formatted messages (one per ticker).
        """
        if not alerts:
            return []

        # Group by ticker
        by_ticker = {}
        for a in alerts:
            t = a["ticker"]
            if t not in by_ticker:
                by_ticker[t] = []
            by_ticker[t].append(a)

        messages = []
        for ticker, ticker_alerts in by_ticker.items():
            # Sort by flow level (extreme first) then volume
            level_order = {"extreme": 0, "significant": 1, "notable": 2}
            ticker_alerts.sort(key=lambda x: (
                level_order.get(x.get("flow_level", "notable"), 3),
                -x.get("volume", 0),
            ))

            # Determine highest level for the card header
            top_level = ticker_alerts[0].get("flow_level", "notable")
            if top_level == "extreme":
                emoji, label = "🚨", "EXTREME FLOW"
            elif top_level == "significant":
                emoji, label = "🔥", "SIGNIFICANT FLOW"
            else:
                emoji, label = "📊", "NOTABLE FLOW"

            # Check for any bursts or new strikes
            has_burst = any(a.get("is_burst") for a in ticker_alerts)
            has_new = any(a.get("is_new_strike") for a in ticker_alerts)
            tags = ""
            if has_burst:
                tags += " ⚡ BURST"
            if has_new:
                tags += " 🆕 NEW"

            spot = ticker_alerts[0].get("spot", 0)

            lines = [
                f"{emoji} {label} — {ticker}{tags}",
                "━" * 28,
                f"Spot: ${spot:.2f} | {len(ticker_alerts)} strikes active",
                "",
            ]

            for a in ticker_alerts:
                side_emoji = "📗" if a["side"] == "call" else "📕"
                dist_dir = "above" if a.get("dist_from_spot_pct", 0) > 0 else "below"
                lvl = a.get("flow_level", "")[0].upper() if a.get("flow_level") else "?"
                burst = " ⚡" if a.get("is_burst") else ""
                new = " 🆕" if a.get("is_new_strike") else ""

                lines.append(
                    f"{side_emoji} ${a['strike']:.0f} {a['side'].upper()} "
                    f"| Vol {a['volume']:,} / OI {a['oi']:,} "
                    f"({a['vol_oi_ratio']:.1f}x) "
                    f"| {a.get('directional_bias', '?')}{burst}{new}"
                )

            # Add expiry info from first alert
            exp = ticker_alerts[0].get("expiry", "")
            if exp:
                lines.append(f"\nExp: {exp}")

            messages.append("\n".join(lines))

        return messages

    def format_sector_flow_alert(self, sector: dict) -> str:
        """Format a sector flow alert."""
        tickers = ", ".join(t["ticker"] for t in sector["tickers"])
        total_vol = sector["total_volume"]
        return (
            f"🏭 SECTOR FLOW: {sector['sector']} — {sector['direction'].upper()}\n"
            f"Tickers: {tickers} ({len(sector['tickers'])} names)\n"
            f"Combined volume: {total_vol:,}\n"
            f"Signal: Institutional sector-wide positioning"
        )

    def format_expiry_cluster_alert(self, cluster: dict) -> str:
        """Format an expiry clustering alert."""
        tickers = ", ".join(cluster["tickers"][:10])
        events = ", ".join(cluster.get("nearby_events", [])[:3])
        event_line = f"\n📅 Nearby event: {events}" if events else ""
        return (
            f"📅 EXPIRY CLUSTERING: {cluster['expiry']}\n"
            f"Heavy flow across {cluster['ticker_count']} tickers: {tickers}\n"
            f"Combined volume: {cluster['total_volume']:,}"
            f"{event_line}\n"
            f"Signal: Institutions positioning through this date"
        )

    def format_eod_flow_summary(self, sweep_alerts: List[dict]) -> List[str]:
        """
        Single end-of-day summary of institutional flow.
        Replaces per-sweep alert spam. Shows the day's picture in 1-2 messages.
        """
        if not sweep_alerts:
            return []

        # Group by ticker, count by level
        by_ticker = {}
        for a in sweep_alerts:
            t = a["ticker"]
            if t not in by_ticker:
                by_ticker[t] = {"extreme": 0, "significant": 0, "notable": 0,
                                "calls": 0, "puts": 0, "total_vol": 0,
                                "max_vol_oi": 0, "top_strike": None}
            level = a.get("flow_level", "notable")
            if level in by_ticker[t]:
                by_ticker[t][level] += 1
            if a["side"] == "call":
                by_ticker[t]["calls"] += a["volume"]
            else:
                by_ticker[t]["puts"] += a["volume"]
            by_ticker[t]["total_vol"] += a["volume"]
            if a["vol_oi_ratio"] > by_ticker[t]["max_vol_oi"]:
                by_ticker[t]["max_vol_oi"] = a["vol_oi_ratio"]
                by_ticker[t]["top_strike"] = a

        # Sort by total volume
        ranked = sorted(by_ticker.items(), key=lambda x: -x[1]["total_vol"])

        # Build summary lines
        lines = [
            "🏛️ END-OF-DAY FLOW SUMMARY",
            "━" * 28,
            f"Scanned {len(by_ticker)} tickers | {len(sweep_alerts)} active strikes",
            "",
        ]

        for ticker, data in ranked[:15]:  # top 15
            if data["extreme"] == 0 and data["significant"] == 0:
                continue  # skip notable-only tickers

            # Determine net direction
            if data["calls"] > data["puts"] * 1.5:
                direction = "🟢 BULLISH"
            elif data["puts"] > data["calls"] * 1.5:
                direction = "🔴 BEARISH"
            else:
                direction = "⚪ MIXED"

            level_tag = ""
            if data["extreme"] > 0:
                level_tag = f" 🚨×{data['extreme']}"
            if data["significant"] > 0:
                level_tag += f" 🔥×{data['significant']}"

            top = data["top_strike"]
            top_info = ""
            if top:
                top_info = (f" | Top: ${top['strike']:.0f} {top['side']} "
                          f"({top['vol_oi_ratio']:.1f}x)")

            lines.append(
                f"{direction} {ticker}{level_tag} — "
                f"C:{data['calls']:,} P:{data['puts']:,}{top_info}"
            )

        # Check campaigns
        campaigns = []
        for ticker in by_ticker:
            try:
                tc = self._state.get_all_flow_campaigns(ticker)
                for c in tc:
                    if c.get("consecutive_days", 0) >= CAMPAIGN_MIN_DAYS:
                        campaigns.append(c)
            except Exception:
                pass

        if campaigns:
            lines.append("")
            lines.append(f"📊 Active campaigns: {len(campaigns)}")
            for c in sorted(campaigns, key=lambda x: -x.get("consecutive_days", 0))[:5]:
                lines.append(
                    f"  {c.get('ticker','?')} ${c.get('strike',0):.0f} {c.get('side','?')} "
                    f"— {c.get('consecutive_days',0)}D, "
                    f"{c.get('flow_type','?').replace('_',' ')}"
                )

        # Chunk to fit Telegram
        messages = []
        msg = "\n".join(lines)
        if len(msg) <= 3800:
            messages.append(msg)
        else:
            mid = len(lines) // 2
            messages.append("\n".join(lines[:mid]))
            messages.append("\n".join(lines[mid:]))

        return messages

    def check_flow_convergence(self, ticker: str, signal_type: str,
                                signal_direction: str) -> Optional[dict]:
        """
        Check if institutional flow converges with another signal.

        signal_type: 'shadow', 'swing', 'potter_box', 'income'
        signal_direction: 'bullish' or 'bearish'

        Returns convergence info if flow confirms the signal direction,
        None if no convergence.
        """
        try:
            campaigns = self._state.get_all_flow_campaigns(ticker)
            if not campaigns:
                return None

            for c in campaigns:
                side = c.get("side", "")
                flow_type = c.get("flow_type", "")

                # Determine flow direction
                if side == "call" and "buildup" in flow_type:
                    flow_dir = "bullish"
                elif side == "put" and "buildup" in flow_type:
                    flow_dir = "bearish"
                elif side == "call" and "unwinding" in flow_type:
                    flow_dir = "bearish"
                elif side == "put" and "unwinding" in flow_type:
                    flow_dir = "bullish"
                else:
                    continue

                if flow_dir == signal_direction:
                    return {
                        "converged": True,
                        "signal_type": signal_type,
                        "flow_direction": flow_dir,
                        "campaign": c,
                        "days": c.get("consecutive_days", 0),
                        "note": (f"Flow confirms {signal_type}: "
                                f"{c.get('side','')} {c.get('flow_type','').replace('_',' ')} "
                                f"at ${c.get('strike',0):.0f} ({c.get('consecutive_days',0)}D campaign)")
                    }
        except Exception:
            pass
        return None

    # ─────────────────────────────────────────────────────
    # CROSS-ASSET ROTATION DETECTION
    # ─────────────────────────────────────────────────────

    # Rotation patterns: when multiple asset classes move together,
    # it signals a macro regime shift, not just a single-name trade.
    ROTATION_PATTERNS = {
        "risk_off": {
            "description": "RISK-OFF ROTATION — institutions moving to safety",
            "emoji": "🛡️",
            "require_bearish": {"SPY", "QQQ", "IWM"},
            "require_bullish": {"TLT", "GLD"},
            "min_matches": 3,  # need at least 3 of the 5
        },
        "risk_on": {
            "description": "RISK-ON ROTATION — institutions adding equity exposure",
            "emoji": "🚀",
            "require_bullish": {"SPY", "QQQ", "IWM", "XLF"},
            "require_bearish": {"TLT", "GLD"},
            "min_matches": 3,
        },
        "tech_rotation": {
            "description": "TECH ROTATION — institutional tech accumulation",
            "emoji": "💻",
            "require_bullish": {"QQQ", "NVDA", "AMD", "SOXX"},
            "require_bearish": set(),
            "min_matches": 3,
        },
        "defensive": {
            "description": "DEFENSIVE ROTATION — rotating into safe sectors",
            "emoji": "🏥",
            "require_bullish": {"XLV", "XLE", "GLD"},
            "require_bearish": {"QQQ", "IWM"},
            "min_matches": 3,
        },
    }

    def detect_cross_asset_rotation(self, sweep_alerts: List[dict]) -> List[dict]:
        """
        Detect macro rotation by analyzing flow direction across asset classes.
        Only considers significant+ flow to avoid noise.

        Returns list of detected rotation patterns.
        """
        if not sweep_alerts:
            return []

        # Build per-ticker direction summary from significant+ alerts
        ticker_direction = {}
        for a in sweep_alerts:
            if a.get("flow_level") not in ("significant", "extreme"):
                continue
            ticker = a["ticker"]
            direction = (a.get("directional_bias", "") or "").upper()

            # Accumulate call vs put volume per ticker
            if ticker not in ticker_direction:
                ticker_direction[ticker] = {"call_vol": 0, "put_vol": 0}
            if a["side"] == "call":
                ticker_direction[ticker]["call_vol"] += a["volume"]
            else:
                ticker_direction[ticker]["put_vol"] += a["volume"]

        # Classify each ticker as bullish/bearish
        bullish_tickers = set()
        bearish_tickers = set()
        for ticker, vols in ticker_direction.items():
            if vols["call_vol"] > vols["put_vol"] * 1.5:
                bullish_tickers.add(ticker)
            elif vols["put_vol"] > vols["call_vol"] * 1.5:
                bearish_tickers.add(ticker)

        # Check each rotation pattern
        detected = []
        for pattern_name, pattern in self.ROTATION_PATTERNS.items():
            bullish_matches = bullish_tickers & pattern.get("require_bullish", set())
            bearish_matches = bearish_tickers & pattern.get("require_bearish", set())
            total_matches = len(bullish_matches) + len(bearish_matches)

            if total_matches >= pattern["min_matches"]:
                detected.append({
                    "pattern": pattern_name,
                    "description": pattern["description"],
                    "emoji": pattern["emoji"],
                    "bullish_matches": sorted(bullish_matches),
                    "bearish_matches": sorted(bearish_matches),
                    "match_count": total_matches,
                    "required": pattern["min_matches"],
                    "all_bullish": sorted(bullish_tickers),
                    "all_bearish": sorted(bearish_tickers),
                })

        return detected

    def format_rotation_alert(self, rotation: dict) -> str:
        """Format a cross-asset rotation detection for Telegram."""
        lines = [
            f"{rotation['emoji']} {rotation['description']}",
            "━" * 28,
        ]

        if rotation["bullish_matches"]:
            tickers = ", ".join(rotation["bullish_matches"])
            lines.append(f"🟢 Bullish flow: {tickers}")
        if rotation["bearish_matches"]:
            tickers = ", ".join(rotation["bearish_matches"])
            lines.append(f"🔴 Bearish flow: {tickers}")

        lines.append(f"")
        lines.append(f"Signal: {rotation['match_count']}/{rotation['required']}+ "
                     f"asset classes confirm rotation pattern")

        # Actionable guidance per rotation type
        pattern = rotation.get("pattern", "")
        if pattern == "risk_off":
            lines += [
                "",
                "🎯 ACTION GUIDANCE:",
                "  • Favor: Puts on SPY/QQQ/IWM",
                "  • Favor: Calls on TLT/GLD",
                "  • Reduce long equity exposure",
                "  • Tighten stops on bullish swing trades",
            ]
        elif pattern == "risk_on":
            lines += [
                "",
                "🎯 ACTION GUIDANCE:",
                "  • Favor: Calls on SPY/QQQ/IWM",
                "  • Reduce bond/gold long positions",
                "  • Widen stops on bullish swings — trend is your friend",
            ]
        elif pattern == "tech_rotation":
            lines += [
                "",
                "🎯 ACTION GUIDANCE:",
                "  • Favor: Calls on QQQ/NVDA/AMD/SOXX",
                "  • Tech leading — overweight semiconductor/AI names",
                "  • Watch for QQQ breakout above resistance",
            ]
        elif pattern == "defensive":
            lines += [
                "",
                "🎯 ACTION GUIDANCE:",
                "  • Favor: Calls on XLV/XLE/GLD",
                "  • Reduce tech/growth exposure",
                "  • Institutions rotating into value/safety",
            ]

        return "\n".join(lines)
