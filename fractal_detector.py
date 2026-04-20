# fractal_detector.py
# ═══════════════════════════════════════════════════════════════════
# v8.3 Phase 2b: Native SR proximity computation for the conviction scorer.
#
# Purpose
# -------
# The backtest measures SR-proximity edges (G3, P1, B7, B8, B9) using
# order-3 fractals on 1-hour bars over a 100-bar window. For those edges
# to carry into live trading, the live bot must compute SR distances the
# SAME WAY. This module is a byte-exact port of:
#
#   backtest/bt_resolution_study_v3.py:833-923
#     find_hourly_fractals(bars_1h, order=3)
#     compute_hourly_sr_at(bars_1h, ticker_key, signal_ts, spot)
#
# Why not level_registry?
# -----------------------
# level_registry.py fuses many live-only sources (OI walls, gamma, EM
# boundaries, pin zones) that never existed in the backtest. Measuring
# "level_registry distance" live would be a different signal than the
# backtest measured. Phase 2b audit concluded the alignment risk outweighs
# the richer source set.
#
# Live integration
# ----------------
# from fractal_detector import get_sr_distances_live
# sr = get_sr_distances_live(ticker, spot)  # returns dict with *_dist_*_pct fields
# # Scorer's _build_context_snapshot reads:
# #   ctx["fractal_resistance_above_spot_pct"] = sr["fractal_dist_above_pct"]
# #   ctx["pivot_resistance_above_spot_pct"]   = sr["pivot_dist_above_pct"]
# #   (and below equivalents for bear rules)
#
# Hourly bars are fetched via get_intraday_bars(ticker, resolution=60) using
# the live bot's existing data plumbing. Results are cached per-ticker with
# a 15-minute TTL so hot-path scorer calls don't hammer the adapter.
#
# Rollback
# --------
# FRACTAL_DETECTOR_ENABLED=false → get_sr_distances_live returns the "empty"
# dict (all distances 999.0). Scorer rules G3/P1/B7/B8/B9 degrade gracefully
# because distance 999 means "no level in range" which the rules already
# handle (they only fire when distance is below thresholds like 3%).
# ═══════════════════════════════════════════════════════════════════

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Constants (must match backtest exactly)
# ─────────────────────────────────────────────────────────────────
FRACTAL_ORDER   = 3      # order-3 swing = 5-bar window (i±3)
LOOKBACK_BARS   = 100    # hourly bars for window
MIN_WINDOW_BARS = 10     # below this, return empty (matches backtest)

# ─────────────────────────────────────────────────────────────────
# Caches
# ─────────────────────────────────────────────────────────────────
# Fractals cache keyed by (ticker, cache_bucket_epoch) — bucket is 15min.
# When the bucket changes we recompute — bars move forward by 15 bar
# increments at worst, so cached fractals stay valid within a bucket.
_fractals_cache: Dict[Tuple[str, int], Tuple[List, List]] = {}

# Hourly bars cache keyed by (ticker, bucket) with same 15-min TTL
_bars_cache: Dict[Tuple[str, int], List[dict]] = {}

CACHE_BUCKET_SEC = 15 * 60


def _current_bucket() -> int:
    """Return current 15-minute cache bucket (unix epoch aligned to 15-min)."""
    return int(time.time()) // CACHE_BUCKET_SEC


# ─────────────────────────────────────────────────────────────────
# Bar-shape conversion
# ─────────────────────────────────────────────────────────────────
def _parallel_to_record_bars(raw: dict) -> List[dict]:
    """Convert live MarketData/Schwab shape to backtest record-style bars.

    Input: {"s":"ok", "t":[...], "o":[...], "h":[...], "l":[...], "c":[...], "v":[...]}
    Output: [{"t":..., "o":..., "h":..., "l":..., "c":..., "v":...}, ...]

    Returns [] on malformed input. Never raises.
    """
    if not raw or not isinstance(raw, dict):
        return []
    t = raw.get("t") or []
    o = raw.get("o") or []
    h = raw.get("h") or []
    l = raw.get("l") or []
    c = raw.get("c") or []
    v = raw.get("v") or []
    n = min(len(t), len(o), len(h), len(l), len(c), len(v))
    if n == 0:
        return []
    out = []
    for i in range(n):
        try:
            out.append({
                "t": int(t[i]),
                "o": float(o[i]),
                "h": float(h[i]),
                "l": float(l[i]),
                "c": float(c[i]),
                "v": float(v[i]),
            })
        except (TypeError, ValueError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────
# Core algorithms (byte-exact port from backtest)
# ─────────────────────────────────────────────────────────────────
def find_hourly_fractals(bars_1h: List[dict], order: int = FRACTAL_ORDER) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Find order-K swing highs and lows in 1h bars.

    A bar at index i is a swing high if bars_1h[i]['h'] == max over [i-K, i+K].
    Returns (highs_list, lows_list) of (idx, price) tuples.

    Port of backtest/bt_resolution_study_v3.py:833-845.
    """
    n = len(bars_1h)
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    if n < 2 * order + 1:
        return highs, lows

    for i in range(order, n - order):
        wh = [bars_1h[j]["h"] for j in range(i - order, i + order + 1)]
        wl = [bars_1h[j]["l"] for j in range(i - order, i + order + 1)]
        if bars_1h[i]["h"] == max(wh):
            highs.append((i, bars_1h[i]["h"]))
        if bars_1h[i]["l"] == min(wl):
            lows.append((i, bars_1h[i]["l"]))
    return highs, lows


def _empty_sr_dict() -> dict:
    """Sentinel dict matching backtest shape for empty/missing data."""
    return {
        "fractal_above": 0.0, "fractal_below": 0.0,
        "fractal_dist_above_pct": 999.0, "fractal_dist_below_pct": 999.0,
        "pivot_above": 0.0, "pivot_below": 0.0,
        "pivot_dist_above_pct": 999.0, "pivot_dist_below_pct": 999.0,
    }


def compute_hourly_sr(bars_1h: List[dict], spot: float,
                      signal_ts: Optional[float] = None,
                      ticker_key: Optional[str] = None) -> dict:
    """Compute 1h fractal and pivot SR levels at a given moment.

    Port of backtest/bt_resolution_study_v3.py:848-923.

    Arguments:
        bars_1h:    List of hourly bar dicts (record-style, chronological)
        spot:       Current spot price
        signal_ts:  Unix epoch of signal. If None, uses most recent bar.
        ticker_key: If provided, fractals are cached per ticker (15min bucket).

    Returns a dict with:
        fractal_above, fractal_below            (level prices, or 0.0)
        fractal_dist_above_pct, *below_pct      (distances %, 999.0 if none)
        pivot_above, pivot_below                (level prices, or 0.0)
        pivot_dist_above_pct, *below_pct        (distances %, 999.0 if none)

    Distance formula matches backtest exactly:
        dist_above_pct = (level - spot) / spot * 100    if level > spot else 999
        dist_below_pct = (spot - level) / spot * 100    if level < spot else 999
    """
    if not bars_1h or spot <= 0:
        return _empty_sr_dict()

    # Find cutoff_idx — last bar at or before signal_ts
    if signal_ts is None:
        cutoff_idx = len(bars_1h) - 1
    else:
        cutoff_idx = -1
        for i in range(len(bars_1h) - 1, -1, -1):
            if bars_1h[i]["t"] <= signal_ts:
                cutoff_idx = i
                break

    if cutoff_idx < MIN_WINDOW_BARS:
        return _empty_sr_dict()

    # Window: last LOOKBACK_BARS hours ending at cutoff_idx
    win_start = max(0, cutoff_idx - LOOKBACK_BARS)
    window = bars_1h[win_start: cutoff_idx + 1]
    if len(window) < MIN_WINDOW_BARS:
        return _empty_sr_dict()

    # Pivot method: highest high / lowest low in window
    pivot_hi = max(b["h"] for b in window)
    pivot_lo = min(b["l"] for b in window)
    pivot_above = pivot_hi if pivot_hi > spot else 0.0
    pivot_below = pivot_lo if pivot_lo < spot else 0.0
    pivot_da = ((pivot_above - spot) / spot * 100.0) if pivot_above > 0 else 999.0
    pivot_db = ((spot - pivot_below) / spot * 100.0) if pivot_below > 0 else 999.0

    # Fractal method (cached per ticker if ticker_key supplied)
    if ticker_key:
        bucket = _current_bucket()
        cache_key = (ticker_key, bucket)
        if cache_key in _fractals_cache:
            highs, lows = _fractals_cache[cache_key]
        else:
            highs, lows = find_hourly_fractals(bars_1h, order=FRACTAL_ORDER)
            _fractals_cache[cache_key] = (highs, lows)
            _prune_cache()
    else:
        highs, lows = find_hourly_fractals(bars_1h, order=FRACTAL_ORDER)

    # Nearest fractal above/below spot, constrained to cutoff_idx
    frac_above = 0.0
    for (i, p) in highs:
        if i > cutoff_idx:
            break
        if p > spot:
            if frac_above == 0.0 or p < frac_above:
                frac_above = p
    frac_below = 0.0
    for (i, p) in lows:
        if i > cutoff_idx:
            break
        if p < spot:
            if frac_below == 0.0 or p > frac_below:
                frac_below = p

    frac_da = ((frac_above - spot) / spot * 100.0) if frac_above > 0 else 999.0
    frac_db = ((spot - frac_below) / spot * 100.0) if frac_below > 0 else 999.0

    return {
        "fractal_above": frac_above, "fractal_below": frac_below,
        "fractal_dist_above_pct": frac_da, "fractal_dist_below_pct": frac_db,
        "pivot_above": pivot_above, "pivot_below": pivot_below,
        "pivot_dist_above_pct": pivot_da, "pivot_dist_below_pct": pivot_db,
    }


# ─────────────────────────────────────────────────────────────────
# Cache maintenance
# ─────────────────────────────────────────────────────────────────
def _prune_cache(max_entries: int = 256) -> None:
    """Drop stale buckets from the fractals cache. Keeps most recent 2 buckets
    per ticker, bounds total entries to max_entries."""
    global _fractals_cache, _bars_cache
    current = _current_bucket()
    # Drop anything older than 2 buckets (30 min)
    stale_cutoff = current - 2
    _fractals_cache = {
        k: v for k, v in _fractals_cache.items() if k[1] >= stale_cutoff
    }
    _bars_cache = {
        k: v for k, v in _bars_cache.items() if k[1] >= stale_cutoff
    }
    # Hard cap
    if len(_fractals_cache) > max_entries:
        # Keep newest entries
        sorted_keys = sorted(_fractals_cache.keys(), key=lambda k: k[1], reverse=True)
        _fractals_cache = {k: _fractals_cache[k] for k in sorted_keys[:max_entries]}


# ─────────────────────────────────────────────────────────────────
# Live integration wrapper
# ─────────────────────────────────────────────────────────────────
def _fetch_hourly_bars_live(ticker: str) -> List[dict]:
    """Fetch hourly bars for a ticker, cached per 15-min bucket.

    Uses app.get_intraday_bars with resolution=60. Falls back to returning
    [] if the data fetch fails.
    """
    bucket = _current_bucket()
    cache_key = (ticker.upper(), bucket)
    if cache_key in _bars_cache:
        return _bars_cache[cache_key]

    try:
        from app import get_intraday_bars
        raw = get_intraday_bars(ticker, resolution=60, countback=120)
        bars = _parallel_to_record_bars(raw)
        _bars_cache[cache_key] = bars
        return bars
    except Exception as e:
        log.debug(f"fractal_detector: hourly bar fetch failed for {ticker}: {e}")
        return []


def get_sr_distances_live(ticker: str, spot: float) -> dict:
    """Fetch-and-compute wrapper for scorer context building.

    Returns the same dict shape as compute_hourly_sr. On any failure —
    disabled, bars unavailable, bad spot — returns the empty sentinel
    which scorer handles gracefully (distance 999 → rule doesn't fire).
    """
    if os.getenv("FRACTAL_DETECTOR_ENABLED", "true").strip().lower() != "true":
        return _empty_sr_dict()
    if not ticker or spot is None or spot <= 0:
        return _empty_sr_dict()

    bars = _fetch_hourly_bars_live(ticker)
    if not bars:
        return _empty_sr_dict()

    return compute_hourly_sr(bars, spot, signal_ts=None, ticker_key=ticker.upper())


def clear_caches() -> None:
    """Test/debug hook to reset all caches."""
    global _fractals_cache, _bars_cache
    _fractals_cache = {}
    _bars_cache = {}
