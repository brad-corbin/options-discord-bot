# quintile_store.py
# ═══════════════════════════════════════════════════════════════════
# v8.3 Phase 2d: Per-combo quintile boundary storage and lookup.
#
# Purpose
# -------
# The conviction_scorer's rules P5-P11 (indicator quintile penalties/boosts)
# were validated in the 621K backtest using quintile boundaries computed PER
# (scoring_source, timeframe, tier, direction) combo, per indicator. Live
# bot must use the same boundaries for measured edge to carry over.
#
# Design
# ------
# - Boundaries live in Redis under key `scorer:quintile_bounds:v1`
# - A nightly job (quintile_refresh.py) pulls last 30 days of signal_decisions
#   rows, computes per-combo percentile boundaries, and writes them back
# - This module reads Redis and returns the quintile bucket for a given
#   (combo, indicator, value) triple
# - If Redis is empty or unreachable, falls back to FALLBACK_BOUNDS extracted
#   from the 621K backtest's summary_quintiles.csv
#
# Usage
# -----
# from quintile_store import get_quintile_bucket
# q = get_quintile_bucket(
#         indicator="ema_diff_pct",
#         value=0.42,
#         scoring_source="active_scanner",
#         timeframe="5m",
#         tier="T1",
#         direction="bull",
#     )
# # q is "Q1" / "Q2" / "Q3" / "Q4" / "Q5" / "unknown"
#
# Integration
# -----------
# conviction_scorer._build_context_snapshot calls this for each indicator
# and sets context fields ema_diff_quintile, macd_hist_quintile, etc.
#
# Rollback
# --------
# If Redis has bad data, set env QUINTILE_STORE_USE_FALLBACK=true — module
# will ignore Redis and use FALLBACK_BOUNDS only.
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Redis key schema
# ─────────────────────────────────────────────────────────────────
REDIS_KEY_BOUNDS = "scorer:quintile_bounds:v1"
REDIS_TTL_SEC    = 48 * 3600  # 48h — if refresh skips a night, old bounds remain valid

# Supported indicators — scorer rules P5-P11 reference these
SUPPORTED_INDICATORS = (
    "ema_diff_pct",
    "macd_hist",
    "rsi",
    "wt2",
    "adx",
)

# ─────────────────────────────────────────────────────────────────
# Combo key construction
# ─────────────────────────────────────────────────────────────────
def _combo_key(scoring_source: str, timeframe: str, tier: str, direction: str) -> str:
    """Deterministic combo identifier used as dict key in the bounds table.

    Format: 'active_scanner:5m:T1:bull'
    """
    ss = str(scoring_source or "").strip().lower()
    tf = str(timeframe or "").strip().lower()
    t  = str(tier or "").strip().upper()
    if not t.startswith("T"):
        t = "T" + t  # accept "1" → "T1"
    d  = str(direction or "").strip().lower()
    return f"{ss}:{tf}:{t}:{d}"


# ─────────────────────────────────────────────────────────────────
# Fallback boundary table
# ─────────────────────────────────────────────────────────────────
# Extracted from the 621K backtest's summary_quintiles_cb.csv via
# extract_fallback_bounds.py (Phase 3e). Each entry is [q20, q40, q60, q80]
# — the four breakpoints defining Q1-Q5.
#
# Source: /var/backtest/summary_quintiles_cb.csv (CB-aligned subset)
# Filter: n_trades >= 20 per (combo, indicator, quintile)
# Combos: 23
#
# Shape: FALLBACK_BOUNDS[combo_key][indicator] = [q20, q40, q60, q80]
# Combo key format: scoring_source:Nm:T#:direction  (e.g., "active_scanner:5m:T1:bull")
#
# Note: active_scanner ADX not present in this extraction because the 621K
# backtest computed ADX only for pinescript signals. A future re-backtest
# (Phase 3 v3.1) with ADX populated for active_scanner will refresh these
# bounds. Until then, active_scanner ADX quintile lookups return "unknown"
# and scorer rule P11 is a no-op for scanner signals.
FALLBACK_BOUNDS: Dict[str, Dict[str, List[float]]] = {
    "active_scanner:15m:T1:bear": {
        "ema_diff_pct": [-0.2334, -0.1234, -0.0801, -0.0485],
        "macd_hist":    [-0.1489, -0.0645, -0.0299, -0.0168],
        "rsi":          [34.1187, 37.9808, 42.4397, 46.9254],
        "wt2":          [-15.9036, 0.2116, 14.8968, 27.3785],
    },
    "active_scanner:15m:T1:bull": {
        "ema_diff_pct": [0.0431, 0.0823, 0.148, 0.2518],
        "macd_hist":    [0.0083, 0.0256, 0.0695, 0.1784],
        "rsi":          [54.0211, 59.1924, 62.6941, 71.3889],
        "wt2":          [-28.8001, -0.8209, 15.4048, 31.681],
    },
    "active_scanner:15m:T2:bear": {
        "ema_diff_pct": [-0.2741, -0.1579, -0.0913, -0.0477],
        "macd_hist":    [-0.3185, -0.1408, -0.0576, -0.0223],
        "rsi":          [30.6991, 36.4186, 40.5922, 46.2896],
        "wt2":          [-29.2002, -18.4522, -4.9491, 11.7097],
    },
    "active_scanner:15m:T2:bull": {
        "ema_diff_pct": [0.0523, 0.1005, 0.1693, 0.2916],
        "macd_hist":    [0.0295, 0.0854, 0.1862, 0.3844],
        "rsi":          [55.2198, 61.0949, 67.0111, 74.3684],
        "wt2":          [-10.156, 13.3203, 30.0032, 44.4874],
    },
    "active_scanner:30m:T1:bull": {
        "ema_diff_pct": [0.054, 0.1085, 0.1848, 0.339],
        "macd_hist":    [0.0237, 0.0758, 0.1252, 0.2054],
        "rsi":          [52.1426, 56.7884, 61.0415, 65.9145],
        "wt2":          [-30.043, -11.0282, 2.5799, 26.9067],
    },
    "active_scanner:30m:T2:bear": {
        "ema_diff_pct": [-0.3646, -0.187, -0.1106, -0.06],
        "macd_hist":    [-0.4628, -0.2097, -0.0815, -0.0275],
        "rsi":          [31.5857, 37.5397, 42.4051, 47.2996],
        "wt2":          [-27.288, -16.2642, -2.1994, 14.3063],
    },
    "active_scanner:30m:T2:bull": {
        "ema_diff_pct": [0.0714, 0.1388, 0.228, 0.3905],
        "macd_hist":    [0.0405, 0.1084, 0.2557, 0.5123],
        "rsi":          [53.0899, 59.8976, 65.671, 73.6778],
        "wt2":          [-15.9821, 8.9547, 24.9837, 39.9895],
    },
    "active_scanner:5m:T1:bear": {
        "ema_diff_pct": [-0.1219, -0.0754, -0.0509, -0.0345],
        "macd_hist":    [-0.0847, -0.0382, -0.0183, -0.0066],
        "rsi":          [33.338, 38.5618, 42.1613, 45.8277],
        "wt2":          [-17.6749, -6.2784, 6.6181, 22.2929],
    },
    "active_scanner:5m:T1:bull": {
        "ema_diff_pct": [0.0336, 0.0489, 0.0739, 0.1269],
        "macd_hist":    [0.0056, 0.0146, 0.034, 0.0817],
        "rsi":          [53.9297, 58.3256, 62.8145, 68.9109],
        "wt2":          [-26.3073, -4.3935, 16.3368, 36.9361],
    },
    "active_scanner:5m:T2:bear": {
        "ema_diff_pct": [-0.1766, -0.0972, -0.0583, -0.0347],
        "macd_hist":    [-0.1768, -0.0771, -0.0335, -0.0119],
        "rsi":          [28.7589, 36.1063, 40.604, 45.8247],
        "wt2":          [-36.7246, -21.2541, -8.9621, 8.8745],
    },
    "active_scanner:5m:T2:bull": {
        "ema_diff_pct": [0.0377, 0.0633, 0.102, 0.183],
        "macd_hist":    [0.0141, 0.0403, 0.0918, 0.2083],
        "rsi":          [55.1005, 61.0812, 66.615, 73.7631],
        "wt2":          [-3.3995, 18.693, 33.9304, 47.7624],
    },
    "pinescript:15m:T1:bear": {
        "adx":          [21.6018, 25.8269, 30.8863, 39.2178],
        "ema_diff_pct": [-0.097, 0.0117, 0.0749, 0.178],
        "macd_hist":    [-0.0763, -0.0339, -0.003, 0.0397],
        "rsi":          [39.6776, 48.9429, 55.059, 60.809],
        "wt2":          [-11.8497, 21.3906, 40.6558, 54.0131],
    },
    "pinescript:15m:T1:bull": {
        "adx":          [21.9522, 26.1482, 31.6717, 39.3477],
        "ema_diff_pct": [-0.1872, -0.0685, 0.0033, 0.1083],
        "macd_hist":    [-0.0459, -0.0045, 0.0366, 0.0871],
        "rsi":          [41.1468, 46.3909, 51.4849, 59.6637],
        "wt2":          [-52.9869, -38.4832, -19.7052, 10.9797],
    },
    "pinescript:15m:T2:bear": {
        "adx":          [21.9542, 26.1981, 31.0911, 38.26],
        "ema_diff_pct": [-0.1403, -0.0347, 0.0469, 0.1447],
        "macd_hist":    [-0.1075, -0.052, -0.021, 0.0187],
        "rsi":          [39.9331, 48.8614, 55.1916, 61.0316],
        "wt2":          [-23.2466, 2.7683, 25.0774, 43.6876],
    },
    "pinescript:15m:T2:bull": {
        "adx":          [22.239, 27.1494, 33.0337, 41.1771],
        "ema_diff_pct": [-0.1897, -0.0738, 0.0034, 0.1172],
        "macd_hist":    [-0.0276, 0.0162, 0.0509, 0.1087],
        "rsi":          [38.2117, 43.8837, 48.9005, 56.3775],
        "wt2":          [-45.9287, -31.3251, -10.1576, 14.7368],
    },
    "pinescript:30m:T1:bear": {
        "adx":          [22.0546, 26.5138, 31.6233, 37.6818],
        "ema_diff_pct": [-0.0542, 0.103, 0.2344, 0.4643],
        "macd_hist":    [-0.0673, -0.0008, 0.0546, 0.1362],
        "rsi":          [45.6939, 54.1734, 59.3234, 65.2368],
        "wt2":          [11.8978, 38.9375, 51.23, 62.5075],
    },
    "pinescript:30m:T1:bull": {
        "adx":          [22.1952, 27.144, 32.2559, 39.6687],
        "ema_diff_pct": [-0.484, -0.2451, -0.1053, 0.0532],
        "macd_hist":    [-0.1502, -0.0588, -0.008, 0.072],
        "rsi":          [36.9329, 42.4253, 46.9998, 54.1408],
        "wt2":          [-60.6775, -49.4883, -35.8446, -11.5252],
    },
    "pinescript:30m:T2:bear": {
        "adx":          [22.4033, 27.0624, 32.8757, 40.0476],
        "ema_diff_pct": [-0.0866, 0.0739, 0.1944, 0.4052],
        "macd_hist":    [-0.1012, -0.0378, 0.02, 0.0889],
        "rsi":          [46.6301, 55.2501, 60.6165, 66.7494],
        "wt2":          [-2.1414, 26.8241, 43.8089, 58.0271],
    },
    "pinescript:30m:T2:bull": {
        "adx":          [23.4315, 28.4231, 34.108, 41.303],
        "ema_diff_pct": [-0.4439, -0.2168, -0.0804, 0.075],
        "macd_hist":    [-0.104, -0.0281, 0.0354, 0.1094],
        "rsi":          [34.6148, 40.1611, 45.1043, 51.942],
        "wt2":          [-54.8053, -42.2285, -26.3807, -0.3847],
    },
    "pinescript:5m:T1:bear": {
        "adx":          [20.3886, 23.3119, 27.2267, 33.504],
        "ema_diff_pct": [-0.0865, -0.034, 0.0124, 0.059],
        "macd_hist":    [-0.034, -0.0124, 0.0052, 0.0271],
        "rsi":          [35.4046, 41.6668, 48.951, 55.5993],
        "wt2":          [-35.5324, -3.058, 31.2593, 51.9901],
    },
    "pinescript:5m:T1:bull": {
        "adx":          [20.4057, 23.3549, 27.2382, 33.5893],
        "ema_diff_pct": [-0.0715, -0.0145, 0.0274, 0.0879],
        "macd_hist":    [-0.033, -0.009, 0.0109, 0.0386],
        "rsi":          [45.1465, 50.3987, 56.428, 63.7569],
        "wt2":          [-51.8941, -32.6315, -6.7551, 32.1166],
    },
    "pinescript:5m:T2:bear": {
        "adx":          [20.2322, 22.8735, 26.4637, 31.8958],
        "ema_diff_pct": [-0.1202, -0.0548, -0.0139, 0.0464],
        "macd_hist":    [-0.0499, -0.018, 0.0025, 0.023],
        "rsi":          [34.75, 40.8182, 46.8293, 55.3095],
        "wt2":          [-45.2207, -20.7845, 6.4707, 42.0395],
    },
    "pinescript:5m:T2:bull": {
        "adx":          [20.3534, 23.1888, 27.0322, 33.6553],
        "ema_diff_pct": [-0.0765, -0.0194, 0.034, 0.1049],
        "macd_hist":    [-0.0279, -0.0057, 0.0149, 0.0468],
        "rsi":          [41.9834, 48.1816, 55.2689, 62.6447],
        "wt2":          [-48.0258, -29.2216, 4.3957, 35.8367],
    },
}


# ─────────────────────────────────────────────────────────────────
# Redis client accessor (lazy)
# ─────────────────────────────────────────────────────────────────
_redis_client = None  # cached client instance
_redis_init_attempted = False


def _get_redis():
    """Get the shared Redis client, initializing lazily. Returns None if unavailable."""
    global _redis_client, _redis_init_attempted
    if _redis_init_attempted:
        return _redis_client
    _redis_init_attempted = True

    try:
        # Prefer the app's persistent_state Redis if available
        from persistent_state import get_redis_client
        _redis_client = get_redis_client()
        if _redis_client is not None:
            log.debug("quintile_store using persistent_state Redis client")
            return _redis_client
    except Exception as e:
        log.debug(f"quintile_store: persistent_state Redis unavailable: {e}")

    # Fallback to direct redis connection
    try:
        import redis
        url = os.getenv("REDIS_URL", "").strip()
        if not url:
            log.debug("quintile_store: REDIS_URL not set")
            return None
        _redis_client = redis.from_url(url, decode_responses=True)
        _redis_client.ping()
        log.debug("quintile_store using direct redis.from_url client")
        return _redis_client
    except Exception as e:
        log.debug(f"quintile_store: direct Redis init failed: {e}")
        _redis_client = None
        return None


# ─────────────────────────────────────────────────────────────────
# Bounds lookup with cache
# ─────────────────────────────────────────────────────────────────
_bounds_cache: Optional[Dict[str, Dict[str, List[float]]]] = None
_bounds_cache_epoch: float = 0
_BOUNDS_CACHE_TTL_SEC = 300  # 5 min — Redis read is cheap but cache saves roundtrips


def _load_bounds_from_redis() -> Optional[Dict]:
    """Return the parsed bounds dict from Redis, or None on any failure."""
    r = _get_redis()
    if r is None:
        return None
    try:
        raw = r.get(REDIS_KEY_BOUNDS)
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            log.warning(f"quintile_store: Redis payload wrong shape: {type(data)}")
            return None
        return data
    except Exception as e:
        log.warning(f"quintile_store: Redis read failed: {e}")
        return None


def _get_bounds_table() -> Dict[str, Dict[str, List[float]]]:
    """Return the active bounds table (Redis preferred, fallback otherwise).

    Cached for _BOUNDS_CACHE_TTL_SEC to minimize Redis roundtrips on hot path.
    """
    global _bounds_cache, _bounds_cache_epoch
    import time
    now = time.time()

    # Respect kill switch
    if os.getenv("QUINTILE_STORE_USE_FALLBACK", "false").strip().lower() == "true":
        return FALLBACK_BOUNDS

    # Cache hit
    if _bounds_cache is not None and (now - _bounds_cache_epoch) < _BOUNDS_CACHE_TTL_SEC:
        return _bounds_cache

    # Try Redis
    live = _load_bounds_from_redis()
    if live:
        _bounds_cache = live
        _bounds_cache_epoch = now
        return live

    # Fall back
    _bounds_cache = FALLBACK_BOUNDS
    _bounds_cache_epoch = now
    return FALLBACK_BOUNDS


def invalidate_cache():
    """Force next lookup to re-fetch from Redis. Called after nightly refresh."""
    global _bounds_cache, _bounds_cache_epoch
    _bounds_cache = None
    _bounds_cache_epoch = 0


# ─────────────────────────────────────────────────────────────────
# Quintile bucket assignment
# ─────────────────────────────────────────────────────────────────
def _assign_quintile(value: float, bounds: List[float]) -> str:
    """Assign a value to Q1-Q5 given a list of 4 breakpoints [q20, q40, q60, q80].

    Matches backtest's _assign_q in backtest/analyze_combined_v1.py:298-303:
      value < bounds[0] → Q1
      value < bounds[1] → Q2
      value < bounds[2] → Q3
      value < bounds[3] → Q4
      else              → Q5
    """
    if not bounds or len(bounds) < 4:
        return "unknown"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if v < bounds[0]:
        return "Q1"
    if v < bounds[1]:
        return "Q2"
    if v < bounds[2]:
        return "Q3"
    if v < bounds[3]:
        return "Q4"
    return "Q5"


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────
def get_quintile_bucket(
    indicator: str,
    value: float,
    scoring_source: str,
    timeframe: str,
    tier: str,
    direction: str,
) -> str:
    """Return 'Q1'..'Q5' for the given (combo, indicator, value).

    Returns 'unknown' if:
    - indicator not supported
    - no bounds available for this combo (neither Redis nor fallback)
    - value is not a valid float

    This function never raises. Failures degrade gracefully to 'unknown',
    and conviction_scorer's rules check for 'unknown' before firing so the
    decision pipeline is never blocked by a quintile lookup error.
    """
    if indicator not in SUPPORTED_INDICATORS:
        return "unknown"

    combo = _combo_key(scoring_source, timeframe, tier, direction)
    table = _get_bounds_table()

    combo_bounds = table.get(combo)
    if not combo_bounds:
        # Try lowercase fallback (FALLBACK_BOUNDS keys are lowercase)
        combo_bounds = table.get(combo.lower())
    if not combo_bounds:
        log.debug(f"quintile_store: no bounds for combo {combo}")
        return "unknown"

    bounds = combo_bounds.get(indicator)
    if not bounds:
        return "unknown"

    return _assign_quintile(value, bounds)


def write_bounds(table: Dict[str, Dict[str, List[float]]]) -> bool:
    """Write a fresh bounds table to Redis. Called by quintile_refresh.py.

    Returns True on success, False otherwise. Never raises.
    """
    r = _get_redis()
    if r is None:
        log.warning("quintile_store: cannot write — Redis unavailable")
        return False
    try:
        r.setex(REDIS_KEY_BOUNDS, REDIS_TTL_SEC, json.dumps(table))
        invalidate_cache()
        log.info(f"quintile_store: wrote {len(table)} combos to Redis")
        return True
    except Exception as e:
        log.error(f"quintile_store: Redis write failed: {e}")
        return False


def health_check() -> Dict:
    """Lightweight status probe for /health or startup sanity checks."""
    table = _get_bounds_table()
    source = "redis" if _bounds_cache is not None and _bounds_cache is not FALLBACK_BOUNDS else "fallback"
    return {
        "source": source,
        "combos": len(table),
        "indicators_per_combo": max((len(v) for v in table.values()), default=0),
        "redis_available": _get_redis() is not None,
    }
