# vwap_bands.py
# ═══════════════════════════════════════════════════════════════════
# VWAP Standard Deviation Bands
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Extends your VWAP calculation from a binary above/below signal into
# a statistical mean-reversion framework.
#
# Why bands matter:
#   Raw "above VWAP" is a weak signal — many trend days spend the
#   entire session above VWAP with no mean reversion. Bands let you
#   distinguish "drifting above VWAP" from "stretched to ±2σ VWAP".
#   At ±2σ-2.5σ, mean-reversion probability is elevated (but NOT
#   guaranteed); at ±3σ+ the signal often flips to trend continuation
#   (blow-off moves). These are heuristic zones — validate on replay
#   against your own setups before weighting heavily.
#
# Math:
#   VWAP(t)    = Σ(TP × V) / Σ(V)           ← session VWAP
#   TPD(t)     = (TP - VWAP)² × V           ← weighted squared deviation
#   σ(t)       = √(Σ(TPD) / Σ(V))           ← VWAP std dev
#   Upper(nσ)  = VWAP + n × σ
#   Lower(nσ)  = VWAP - n × σ
#
# Latency: pure math on cached intraday bars. No API calls.
# ═══════════════════════════════════════════════════════════════════

import math
from typing import Dict, List, Optional, Tuple


def compute_vwap_bands(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
    reset_at_index: Optional[int] = None,
) -> Dict:
    """Compute session VWAP and standard deviation bands.

    Args:
        highs, lows, closes, volumes: Parallel arrays of bar data.
        reset_at_index: Index to reset VWAP session (e.g. market open).
                        If None, uses all bars passed.

    Returns dict with:
      ok:              True if enough data
      vwap:            Current VWAP value
      std:             VWAP standard deviation
      upper_1sd, lower_1sd
      upper_2sd, lower_2sd
      upper_3sd, lower_3sd
      spot_zscore:     (close - vwap) / std — how stretched price is
      band_zone:       AT_VWAP / DRIFT / STRETCHED / EXTREME
      band_side:       ABOVE / BELOW / AT
      mean_reversion_edge: 0-1 score — higher = stronger reversion setup
    """
    result = {
        "ok": False,
        "vwap": None,
        "std": None,
        "upper_1sd": None, "lower_1sd": None,
        "upper_2sd": None, "lower_2sd": None,
        "upper_3sd": None, "lower_3sd": None,
        "spot_zscore": 0.0,
        "band_zone": "UNKNOWN",
        "band_side": "AT",
        "mean_reversion_edge": 0.0,
    }

    if not (highs and lows and closes and volumes):
        return result

    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < 5:
        return result

    start = reset_at_index if reset_at_index is not None else 0
    start = max(0, min(start, n - 1))

    # Volume-weighted typical price
    sum_v = 0.0
    sum_pv = 0.0
    for i in range(start, n):
        v = float(volumes[i] or 0)
        if v <= 0:
            continue
        tp = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
        sum_v += v
        sum_pv += tp * v

    if sum_v <= 0:
        return result

    vwap = sum_pv / sum_v

    # Volume-weighted variance
    sum_wvar = 0.0
    for i in range(start, n):
        v = float(volumes[i] or 0)
        if v <= 0:
            continue
        tp = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
        sum_wvar += v * (tp - vwap) ** 2

    variance = sum_wvar / sum_v
    std = math.sqrt(variance)

    current_close = float(closes[-1])
    spot_zscore = (current_close - vwap) / std if std > 0 else 0.0

    # Classify zone
    abs_z = abs(spot_zscore)
    if abs_z >= 2.5:
        zone = "EXTREME"
    elif abs_z >= 1.8:
        zone = "STRETCHED"
    elif abs_z >= 0.5:
        zone = "DRIFT"
    else:
        zone = "AT_VWAP"

    side = "ABOVE" if spot_zscore > 0.1 else "BELOW" if spot_zscore < -0.1 else "AT"

    # Mean reversion edge: peaks near ±2-2.5σ, drops off at ±3σ+ (trending)
    if abs_z < 1.0:
        mr_edge = 0.0
    elif abs_z < 1.8:
        mr_edge = (abs_z - 1.0) / 0.8 * 0.4   # 0 to 0.4 linear
    elif abs_z < 2.5:
        mr_edge = 0.4 + (abs_z - 1.8) / 0.7 * 0.5   # 0.4 to 0.9
    elif abs_z < 3.0:
        mr_edge = 0.9 - (abs_z - 2.5) / 0.5 * 0.4   # decays if trending
    else:
        mr_edge = 0.3   # beyond 3σ = likely trending, low reversion edge

    result.update({
        "ok": True,
        "vwap": round(vwap, 4),
        "std": round(std, 4),
        "upper_1sd": round(vwap + std, 4),
        "lower_1sd": round(vwap - std, 4),
        "upper_2sd": round(vwap + 2 * std, 4),
        "lower_2sd": round(vwap - 2 * std, 4),
        "upper_3sd": round(vwap + 3 * std, 4),
        "lower_3sd": round(vwap - 3 * std, 4),
        "spot_zscore": round(spot_zscore, 3),
        "band_zone": zone,
        "band_side": side,
        "mean_reversion_edge": round(mr_edge, 3),
    })
    return result


def score_vwap_bands_for_trade(
    band_result: Dict,
    direction: str = "bull",
) -> Tuple[int, List[str]]:
    """Return (confidence_delta, reasons) for VWAP band analysis.

    The logic:
      - Trend-following (bull above VWAP DRIFT): small boost
      - Mean-reversion (bull at lower_2sd): larger boost
      - Fighting trend (bull at upper_3sd STRETCHED): penalty
    """
    if not band_result or not band_result.get("ok"):
        return 0, []

    zone = band_result.get("band_zone", "UNKNOWN")
    side = band_result.get("band_side", "AT")
    z = band_result.get("spot_zscore", 0.0)
    mr_edge = band_result.get("mean_reversion_edge", 0.0)
    is_bear = direction == "bear"

    delta = 0
    reasons = []

    # Bull logic
    if not is_bear:
        if zone == "DRIFT" and side == "ABOVE":
            delta += 4
            reasons.append(f"Above VWAP drift zone (z={z:+.2f}), trend-following edge (+4)")
        elif zone == "STRETCHED" and side == "BELOW":
            # Mean reversion opportunity for bull
            delta += 8
            reasons.append(f"Bull at {z:.2f}σ below VWAP — mean-reversion edge (+8)")
        elif zone == "EXTREME" and side == "BELOW":
            # Could be climax low or breakdown — depends on edge score
            if mr_edge >= 0.7:
                delta += 10
                reasons.append(f"Bull at extreme {z:.2f}σ below VWAP — reversion setup (+10)")
            else:
                delta += 3  # trending down, bull is contrarian
                reasons.append(f"Bull at {z:.2f}σ below VWAP — late reversion (+3)")
        elif zone == "STRETCHED" and side == "ABOVE":
            delta -= 4
            reasons.append(f"Bull at {z:+.2f}σ above VWAP — chasing extended move (−4)")
        elif zone == "EXTREME" and side == "ABOVE":
            delta -= 8
            reasons.append(f"Bull at extreme {z:+.2f}σ above VWAP — reversion risk (−8)")
    # Bear logic (mirror)
    else:
        if zone == "DRIFT" and side == "BELOW":
            delta += 4
            reasons.append(f"Below VWAP drift zone (z={z:.2f}), trend-following edge (+4)")
        elif zone == "STRETCHED" and side == "ABOVE":
            delta += 8
            reasons.append(f"Bear at {z:+.2f}σ above VWAP — mean-reversion edge (+8)")
        elif zone == "EXTREME" and side == "ABOVE":
            if mr_edge >= 0.7:
                delta += 10
                reasons.append(f"Bear at extreme {z:+.2f}σ above VWAP — reversion setup (+10)")
            else:
                delta += 3
                reasons.append(f"Bear at {z:+.2f}σ above VWAP — late reversion (+3)")
        elif zone == "STRETCHED" and side == "BELOW":
            delta -= 4
            reasons.append(f"Bear at {z:.2f}σ below VWAP — chasing extended move (−4)")
        elif zone == "EXTREME" and side == "BELOW":
            delta -= 8
            reasons.append(f"Bear at extreme {z:.2f}σ below VWAP — reversion risk (−8)")

    return delta, reasons


def format_vwap_bands_line(band_result: Dict) -> str:
    """One-line summary for trade cards."""
    if not band_result or not band_result.get("ok"):
        return ""
    zone = band_result.get("band_zone", "UNKNOWN")
    if zone == "UNKNOWN":
        return ""
    side = band_result.get("band_side", "AT")
    z = band_result.get("spot_zscore", 0)
    vwap = band_result.get("vwap")
    if zone == "AT_VWAP":
        return f"📊 VWAP ${vwap:.2f} | at value (z={z:+.2f})"
    return f"📊 VWAP ${vwap:.2f} | {zone} {side} (z={z:+.2f}σ)"
