# gap_analyzer.py
# ═══════════════════════════════════════════════════════════════════
# Session Gap Analyzer
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Detects overnight/pre-market gaps and classifies setup quality for
# the first 60-90 minutes of the cash session.
#
# Gap types and heuristic edge (calibrate on your own replay data):
#   GAP_AND_GO       — gap outside prior range WITH follow-through volume
#                      in first 15 min. Historically a trend-day setup.
#                      Default conf boost is a starting point, not calibrated.
#   GAP_AND_FADE     — gap outside prior range, immediately reversing.
#                      Fade setup (contrarian for opposite bias).
#   GAP_FILL         — gap inside prior range. Mean-reversion bias:
#                      price tends to retest prior close.
#   INSIDE_DAY       — no gap, price opened inside prior range.
#
# The confidence deltas returned by score_gap_for_trade() are HEURISTIC
# starting values. Run shadow mode first and adjust the per-type deltas
# based on your attribution report before weighting live.
#
# Combines prior-day high/low/close with current-session open and
# first-5-minute range + volume.
# ═══════════════════════════════════════════════════════════════════

import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────

# Minimum gap size (% of prior close) to be considered a "real" gap
MIN_GAP_PCT = 0.15            # 0.15% = $0.75 on a $500 stock
SIGNIFICANT_GAP_PCT = 0.50    # 0.5%+ = institutionally significant
LARGE_GAP_PCT = 1.5           # 1.5%+ = news gap

# Follow-through volume requirement for GAP_AND_GO
FOLLOW_THROUGH_VOLUME_MULT = 1.75  # first 15-min vol > 1.75x avg opening

# Reversal detection window (first bars)
REVERSAL_WINDOW_BARS = 3      # first 3 5-min bars = 15 min


# ─────────────────────────────────────────────────────────
# GAP DETECTION
# ─────────────────────────────────────────────────────────

def analyze_gap(
    prior_day_high: float,
    prior_day_low: float,
    prior_day_close: float,
    session_open: float,
    first_bars: Optional[List[Dict]] = None,
    avg_opening_volume: Optional[float] = None,
) -> Dict:
    """Analyze the current session's gap quality.

    Args:
        prior_day_high/low/close: Prior session OHLC
        session_open: Today's session open
        first_bars: Optional list of 5-min bars from session open, each with
                    {open, high, low, close, volume}.
        avg_opening_volume: Avg volume in first 15 min of past 10 sessions.

    Returns dict with:
      gap_type:              GAP_AND_GO / GAP_AND_FADE / GAP_FILL / INSIDE_DAY / NO_GAP
      gap_direction:         UP / DOWN / NONE
      gap_pct:               Percentage gap from prior close
      gap_magnitude:         MINOR / NORMAL / SIGNIFICANT / LARGE
      outside_prior_range:   True if open is outside prior day's high/low
      reversal_detected:     True if first bars went back through session open
      follow_through:        True if volume surge in same direction
      bias:                  BULL / BEAR / MIXED
      edge_score:            0-100 quality of the setup
      description:           Human-readable summary
    """
    result = {
        "gap_type": "NO_GAP",
        "gap_direction": "NONE",
        "gap_pct": 0.0,
        "gap_magnitude": "NONE",
        "outside_prior_range": False,
        "reversal_detected": False,
        "follow_through": False,
        "bias": "MIXED",
        "edge_score": 0,
        "description": "",
    }

    if prior_day_close <= 0 or session_open <= 0:
        return result

    gap_pct = ((session_open - prior_day_close) / prior_day_close) * 100
    gap_abs = abs(gap_pct)

    # No gap if below minimum
    if gap_abs < MIN_GAP_PCT:
        result["description"] = "No significant overnight gap"
        return result

    direction = "UP" if gap_pct > 0 else "DOWN"
    outside_range = session_open > prior_day_high or session_open < prior_day_low

    # Magnitude classification
    if gap_abs >= LARGE_GAP_PCT:
        magnitude = "LARGE"
    elif gap_abs >= SIGNIFICANT_GAP_PCT:
        magnitude = "SIGNIFICANT"
    elif gap_abs >= MIN_GAP_PCT:
        magnitude = "NORMAL"
    else:
        magnitude = "MINOR"

    # Reversal / follow-through detection
    reversal = False
    follow_through = False
    if first_bars and len(first_bars) >= 1:
        bars = first_bars[:REVERSAL_WINDOW_BARS]
        # Reversal: any bar's close crosses back through session_open against gap direction
        if direction == "UP":
            # Gap up — reversal if any close is below session_open
            for b in bars:
                if float(b.get("close", 0)) < session_open:
                    reversal = True
                    break
        else:
            # Gap down — reversal if any close is above session_open
            for b in bars:
                if float(b.get("close", 0)) > session_open:
                    reversal = True
                    break

        # Follow-through: first 15-min volume > avg AND price extending in gap direction
        window_vol = sum(float(b.get("volume", 0)) for b in bars)
        latest_close = float(bars[-1].get("close", session_open)) if bars else session_open
        price_extending = (
            (direction == "UP" and latest_close > session_open) or
            (direction == "DOWN" and latest_close < session_open)
        )
        if avg_opening_volume and avg_opening_volume > 0:
            vol_surge = window_vol >= (avg_opening_volume * FOLLOW_THROUGH_VOLUME_MULT)
        else:
            vol_surge = False   # can't confirm without baseline

        follow_through = price_extending and vol_surge

    # Classify gap type
    if outside_range and follow_through and not reversal:
        gap_type = "GAP_AND_GO"
        bias = "BULL" if direction == "UP" else "BEAR"
        edge = 85 if magnitude in ("SIGNIFICANT", "LARGE") else 70
    elif outside_range and reversal:
        gap_type = "GAP_AND_FADE"
        # Fade = opposite bias from gap direction
        bias = "BEAR" if direction == "UP" else "BULL"
        edge = 75 if magnitude in ("SIGNIFICANT", "LARGE") else 55
    elif outside_range:
        # Gap outside but no clear direction yet — ambiguous
        gap_type = "GAP_OUTSIDE_UNCLEAR"
        bias = "MIXED"
        edge = 40
    elif not outside_range:
        gap_type = "GAP_FILL"
        # Mean-reversion: bias opposite the gap direction
        bias = "BEAR" if direction == "UP" else "BULL"
        edge = 55 if magnitude in ("SIGNIFICANT", "LARGE") else 35
    else:
        gap_type = "INSIDE_DAY"
        bias = "MIXED"
        edge = 20

    # Build description
    descs = {
        "GAP_AND_GO": (
            f"Gap {direction} {gap_pct:+.2f}% outside prior range with follow-through volume — "
            f"trend-day setup (bias {bias})"
        ),
        "GAP_AND_FADE": (
            f"Gap {direction} {gap_pct:+.2f}% outside prior range, reversal in first 15 min — "
            f"fade setup (bias {bias})"
        ),
        "GAP_OUTSIDE_UNCLEAR": (
            f"Gap {direction} {gap_pct:+.2f}% outside prior range, no clear direction yet"
        ),
        "GAP_FILL": (
            f"Gap {direction} {gap_pct:+.2f}% inside prior range — "
            f"fill bias toward prior close ({bias})"
        ),
        "INSIDE_DAY": f"Open inside prior range, no meaningful gap",
    }

    result.update({
        "gap_type": gap_type,
        "gap_direction": direction,
        "gap_pct": round(gap_pct, 3),
        "gap_magnitude": magnitude,
        "outside_prior_range": outside_range,
        "reversal_detected": reversal,
        "follow_through": follow_through,
        "bias": bias,
        "edge_score": edge,
        "description": descs.get(gap_type, ""),
        "prior_close": prior_day_close,
        "prior_high": prior_day_high,
        "prior_low": prior_day_low,
        "session_open": session_open,
    })
    return result


# ─────────────────────────────────────────────────────────
# TRADE CONFIDENCE INTEGRATION
# ─────────────────────────────────────────────────────────

def score_gap_for_trade(
    gap_result: Dict,
    direction: str = "bull",
) -> Tuple[int, List[str]]:
    """Return (confidence_delta, reasons) based on gap setup.

    Only active in first ~60 min of session — caller should check time.
    """
    if not gap_result or gap_result.get("gap_type") == "NO_GAP":
        return 0, []

    gap_type = gap_result.get("gap_type", "")
    bias = gap_result.get("bias", "MIXED")
    edge = gap_result.get("edge_score", 0)
    gap_direction_up = gap_result.get("gap_direction") == "UP"
    is_bear = direction == "bear"

    # Bias match
    bias_matches = (
        (bias == "BULL" and not is_bear) or
        (bias == "BEAR" and is_bear)
    )
    bias_opposes = (
        (bias == "BULL" and is_bear) or
        (bias == "BEAR" and not is_bear)
    )

    delta = 0
    reasons = []

    if gap_type == "GAP_AND_GO":
        if bias_matches:
            delta += 10
            reasons.append(f"Gap-and-go setup confirms {direction} (+10)")
        elif bias_opposes:
            delta -= 10
            reasons.append(f"Fighting gap-and-go trend day (−10)")
    elif gap_type == "GAP_AND_FADE":
        if bias_matches:
            delta += 8
            reasons.append(f"Gap-and-fade reversal confirms {direction} (+8)")
        elif bias_opposes:
            delta -= 6
            reasons.append(f"Fighting gap-and-fade reversal (−6)")
    elif gap_type == "GAP_FILL":
        if bias_matches:
            delta += 4
            reasons.append(f"Gap-fill bias supports {direction} (+4)")
        # Opposing gap-fill = fighting mean reversion = small penalty
        elif bias_opposes:
            delta -= 2
            reasons.append(f"Fighting gap-fill mean reversion (−2)")
    elif gap_type == "GAP_OUTSIDE_UNCLEAR":
        # Unresolved gap outside range = wait for direction
        delta -= 3
        reasons.append("Large gap not yet resolved (−3)")

    return delta, reasons


def format_gap_line(gap_result: Dict) -> str:
    """One-line summary."""
    if not gap_result or gap_result.get("gap_type") in ("NO_GAP", "INSIDE_DAY"):
        return ""
    gtype = gap_result.get("gap_type", "")
    gap_pct = gap_result.get("gap_pct", 0)
    bias = gap_result.get("bias", "MIXED")
    edge = gap_result.get("edge_score", 0)
    emoji_map = {
        "GAP_AND_GO": "🚀",
        "GAP_AND_FADE": "🔄",
        "GAP_FILL": "🎯",
        "GAP_OUTSIDE_UNCLEAR": "❓",
    }
    emoji = emoji_map.get(gtype, "📏")
    return f"{emoji} Gap: {gtype.replace('_', ' ')} {gap_pct:+.2f}% (bias {bias}, edge {edge})"
