# options_skew.py
# ═══════════════════════════════════════════════════════════════════
# Options Skew Analyzer
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Computes 25-delta put/call IV skew — a leading institutional positioning
# signal that you're currently not using despite having the full chain data.
#
# Why it matters:
#   When smart money accumulates protection (buys puts / sells calls),
#   25-delta put IV rises faster than 25-delta call IV. This skew
#   steepening often leads price moves by 1-3 days.
#
#   Skew COMPRESSION (puts getting cheaper relative to calls) often
#   marks complacency tops. Skew EXPANSION marks fear buildup.
#
# Outputs integrate directly into:
#   - compute_confidence() via SKEW_CONFIDENCE_BOOSTS / PENALTIES
#   - shared_model_snapshot for trade card display
#   - vol_regime caution scoring as a 7th input
#
# Data latency: Uses cached chain data (120s TTL). No new API calls.
# ═══════════════════════════════════════════════════════════════════

import math
import time
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────

# Delta targets for skew sampling
TARGET_DELTA_PUT  = 0.25   # 25-delta put (abs)
TARGET_DELTA_CALL = 0.25   # 25-delta call
DELTA_TOLERANCE   = 0.05   # accept 0.20-0.30 delta range

# Skew thresholds (percentage points of IV difference)
# These are calibrated against SPY historical — adjust for individual names.
SKEW_EXTREME_STEEP_PCT     = 6.0    # >6pp = extreme fear skew (bullish contrarian)
SKEW_STEEP_PCT             = 3.0    # >3pp = elevated put premium
SKEW_NORMAL_PCT            = 1.5    # 1.5-3pp = typical
SKEW_FLAT_PCT              = 0.5    # <0.5pp = complacency / call chase
SKEW_INVERTED_PCT          = -0.5   # calls > puts = euphoria / squeeze pending

# Rate-of-change thresholds (pp / day)
SKEW_ROC_RAPID_EXPANSION   = 1.5    # >1.5pp steepening in one day = institutional fear
SKEW_ROC_RAPID_COMPRESSION = -1.5   # >1.5pp flattening = complacency building

# History buffer for ROC calculation (in-memory; caller can persist)
_SKEW_HISTORY_SIZE = 20  # days


# ─────────────────────────────────────────────────────────
# SKEW HISTORY STORE
# ─────────────────────────────────────────────────────────

class SkewHistory:
    """Per-ticker rolling skew history. Thread-safe via dict ops.
    Wire this to PersistentState if you want skew ROC across restarts.
    """

    def __init__(self, max_size: int = _SKEW_HISTORY_SIZE):
        self._history: Dict[str, List[Tuple[float, float]]] = {}  # ticker -> [(ts, skew_pp)]
        self._max_size = max_size

    def record(self, ticker: str, skew_pp: float, ts: Optional[float] = None):
        t = ts or time.time()
        entries = self._history.setdefault(ticker.upper(), [])
        entries.append((t, skew_pp))
        if len(entries) > self._max_size:
            entries[:] = entries[-self._max_size:]

    def get_prior(self, ticker: str, min_age_sec: int = 3600) -> Optional[float]:
        """Get skew value at least min_age_sec old. Default 1hr for intraday ROC."""
        entries = self._history.get(ticker.upper(), [])
        if not entries:
            return None
        now = time.time()
        for ts, sk in reversed(entries):
            if now - ts >= min_age_sec:
                return sk
        return entries[0][1] if entries else None

    def get_daily_prior(self, ticker: str) -> Optional[float]:
        """Get skew from ~24h ago for daily ROC."""
        return self.get_prior(ticker, min_age_sec=20 * 3600)


# Module-level default instance — callers can replace with their own
default_skew_history = SkewHistory()


# ─────────────────────────────────────────────────────────
# CORE SKEW COMPUTATION
# ─────────────────────────────────────────────────────────

def find_nearest_delta_iv(
    contracts: List[Dict],
    target_delta: float,
    option_right: str,
    tolerance: float = DELTA_TOLERANCE,
) -> Optional[Tuple[float, float, float]]:
    """Find the contract whose |delta| is closest to target.

    Returns (strike, iv, actual_delta) or None.

    option_right: "call" or "put"
    target_delta: positive value (e.g. 0.25); put deltas are flipped internally.
    """
    best = None
    best_diff = float('inf')

    for c in contracts:
        right = (c.get("right") or "").lower()
        if right != option_right:
            continue

        delta = c.get("delta")
        iv = c.get("iv")
        strike = c.get("strike")

        if delta is None or iv is None or strike is None:
            continue

        try:
            delta = float(delta)
            iv = float(iv)
            strike = float(strike)
        except (ValueError, TypeError):
            continue

        if iv <= 0 or iv > 10:  # sanity bounds
            continue

        abs_delta = abs(delta)
        diff = abs(abs_delta - target_delta)

        if diff > tolerance:
            continue

        if diff < best_diff:
            best_diff = diff
            best = (strike, iv, delta)

    return best


def compute_skew(
    contracts: List[Dict],
    spot: float,
    ticker: str = "SPY",
    history: Optional[SkewHistory] = None,
    record_to_history: bool = True,
) -> Dict:
    """Compute 25-delta put/call skew from an option chain.

    Returns dict with:
      skew_pp:            IV_put_25d - IV_call_25d (percentage points)
      skew_ratio:         IV_put_25d / IV_call_25d
      skew_label:         EXTREME_STEEP / STEEP / NORMAL / FLAT / INVERTED
      skew_emoji:         visual indicator
      daily_roc_pp:       day-over-day change (None if no prior)
      roc_label:          RAPID_EXPANSION / EXPANDING / STABLE / COMPRESSING / RAPID_COMPRESSION
      put_strike:         strike used for put IV
      call_strike:        strike used for call IV
      put_iv:             IV at put strike (decimal, e.g. 0.28)
      call_iv:            IV at call strike
      description:        human-readable summary
      ok:                 True if computation succeeded
    """
    history = history if history is not None else default_skew_history

    result = {
        "ok": False,
        "skew_pp": None,
        "skew_ratio": None,
        "skew_label": "UNKNOWN",
        "skew_emoji": "❓",
        "daily_roc_pp": None,
        "roc_label": "UNKNOWN",
        "put_strike": None,
        "call_strike": None,
        "put_iv": None,
        "call_iv": None,
        "description": "Insufficient chain data",
    }

    if not contracts or spot <= 0:
        return result

    put_data = find_nearest_delta_iv(contracts, TARGET_DELTA_PUT, "put")
    call_data = find_nearest_delta_iv(contracts, TARGET_DELTA_CALL, "call")

    if put_data is None or call_data is None:
        return result

    put_strike, put_iv, put_delta = put_data
    call_strike, call_iv, call_delta = call_data

    # Skew in percentage points of IV
    skew_pp = round((put_iv - call_iv) * 100, 2)
    skew_ratio = round(put_iv / call_iv, 3) if call_iv > 0 else None

    # Categorize
    if skew_pp >= SKEW_EXTREME_STEEP_PCT:
        label, emoji = "EXTREME_STEEP", "🔴🔴"
        desc = f"Extreme put premium (+{skew_pp:.1f}pp) — institutional fear at peak"
    elif skew_pp >= SKEW_STEEP_PCT:
        label, emoji = "STEEP", "🔴"
        desc = f"Elevated put skew (+{skew_pp:.1f}pp) — hedging demand"
    elif skew_pp >= SKEW_NORMAL_PCT:
        label, emoji = "NORMAL", "🟡"
        desc = f"Normal skew (+{skew_pp:.1f}pp) — typical positioning"
    elif skew_pp >= SKEW_FLAT_PCT:
        label, emoji = "FLAT", "🟢"
        desc = f"Flat skew (+{skew_pp:.1f}pp) — low fear / complacency"
    elif skew_pp >= SKEW_INVERTED_PCT:
        label, emoji = "VERY_FLAT", "🟢"
        desc = f"Very flat skew ({skew_pp:+.1f}pp) — complacency building"
    else:
        label, emoji = "INVERTED", "⚠️🟢"
        desc = f"Inverted skew ({skew_pp:.1f}pp) — call premium exceeds put, squeeze/euphoria signal"

    # Record to history for ROC
    if record_to_history:
        history.record(ticker, skew_pp)

    # Daily rate of change
    prior = history.get_daily_prior(ticker)
    daily_roc_pp = None
    roc_label = "UNKNOWN"
    if prior is not None:
        daily_roc_pp = round(skew_pp - prior, 2)
        if daily_roc_pp >= SKEW_ROC_RAPID_EXPANSION:
            roc_label = "RAPID_EXPANSION"
            desc += f" | RAPID skew expansion (+{daily_roc_pp:.1f}pp/d) — fear building fast"
        elif daily_roc_pp >= 0.5:
            roc_label = "EXPANDING"
        elif daily_roc_pp <= SKEW_ROC_RAPID_COMPRESSION:
            roc_label = "RAPID_COMPRESSION"
            desc += f" | RAPID skew compression ({daily_roc_pp:.1f}pp/d) — hedges being unwound"
        elif daily_roc_pp <= -0.5:
            roc_label = "COMPRESSING"
        else:
            roc_label = "STABLE"

    result.update({
        "ok": True,
        "skew_pp": skew_pp,
        "skew_ratio": skew_ratio,
        "skew_label": label,
        "skew_emoji": emoji,
        "daily_roc_pp": daily_roc_pp,
        "roc_label": roc_label,
        "put_strike": put_strike,
        "call_strike": call_strike,
        "put_iv": round(put_iv, 4),
        "call_iv": round(call_iv, 4),
        "put_delta": round(put_delta, 3),
        "call_delta": round(call_delta, 3),
        "description": desc,
    })
    return result


# ─────────────────────────────────────────────────────────
# CONFIDENCE INTEGRATION
# ─────────────────────────────────────────────────────────

# Drop-in boost/penalty values for options_engine_v3.compute_confidence.
# Add these to CONFIDENCE_BOOSTS / CONFIDENCE_PENALTIES in trading_rules.py:
#
#   "skew_extreme_contrarian":  10,    # Extreme put skew + bull trade = contrarian edge
#   "skew_steep_confirms_bear":  8,    # Steep skew + bear trade = aligned with hedging flow
#   "skew_flat_confirms_bull":   5,    # Flat skew + bull trade = no protection demand
#   "skew_inverted_bull_caution":-6,   # Inverted + bull = squeeze risk but complacent
#   "skew_rapid_expansion_bull":-8,    # Skew expanding fast + bull entry = fighting fear flow
#   "skew_rapid_expansion_bear": 8,    # Skew expanding fast + bear entry = aligned
#   "skew_rapid_compression_bull": 6,  # Skew compressing + bull = hedges unwinding
#   "skew_rapid_compression_bear": -6, # Skew compressing + bear = smart money selling puts

def score_skew_for_trade(
    skew_result: Dict,
    direction: str = "bull",
) -> Tuple[int, List[str]]:
    """Return (confidence_delta, reason_bits) for a skew observation.

    Plug this into compute_confidence() right after the vol_regime block.
    """
    if not skew_result or not skew_result.get("ok"):
        return 0, []

    is_bear = direction == "bear"
    label = skew_result.get("skew_label", "UNKNOWN")
    roc_label = skew_result.get("roc_label", "UNKNOWN")
    skew_pp = skew_result.get("skew_pp", 0)
    roc_pp = skew_result.get("daily_roc_pp", 0) or 0

    delta = 0
    reasons = []

    # Level-based scoring
    if label == "EXTREME_STEEP":
        if is_bear:
            # Bear into extreme fear: already crowded, reduce edge
            delta -= 4
            reasons.append(f"Skew extreme steep ({skew_pp:+.1f}pp) — bear trade crowded (−4)")
        else:
            # Bull into extreme fear: contrarian edge, hedges tend to be unwound
            delta += 10
            reasons.append(f"Skew extreme steep ({skew_pp:+.1f}pp) — contrarian bull edge (+10)")
    elif label == "STEEP":
        if is_bear:
            delta += 8
            reasons.append(f"Steep skew ({skew_pp:+.1f}pp) confirms bear (+8)")
        else:
            delta -= 3
            reasons.append(f"Steep skew ({skew_pp:+.1f}pp) — hedging flow fights bull (−3)")
    elif label == "NORMAL":
        # No edge either way
        pass
    elif label == "FLAT":
        if is_bear:
            delta -= 4
            reasons.append(f"Flat skew ({skew_pp:+.1f}pp) — no fear, bear thesis weak (−4)")
        else:
            delta += 5
            reasons.append(f"Flat skew ({skew_pp:+.1f}pp) confirms bull (+5)")
    elif label == "VERY_FLAT":
        if is_bear:
            # Squeeze risk but also target-rich environment
            delta += 3
            reasons.append(f"Very flat skew ({skew_pp:+.1f}pp) — complacency, bear edge (+3)")
        else:
            delta -= 2
            reasons.append(f"Very flat skew ({skew_pp:+.1f}pp) — euphoria warning (−2)")
    elif label == "INVERTED":
        if is_bear:
            # Calls richer than puts — squeeze/euphoria — bear gets paid if it reverses
            delta += 5
            reasons.append(f"Inverted skew ({skew_pp:+.1f}pp) — euphoria top risk, bear edge (+5)")
        else:
            delta -= 6
            reasons.append(f"Inverted skew ({skew_pp:+.1f}pp) — squeeze/blow-off risk (−6)")

    # Rate-of-change scoring (can compound with level)
    if roc_label == "RAPID_EXPANSION":
        if is_bear:
            delta += 8
            reasons.append(f"Skew expanding rapidly (+{roc_pp:.1f}pp/d) — institutional fear (+8)")
        else:
            delta -= 8
            reasons.append(f"Skew expanding rapidly (+{roc_pp:.1f}pp/d) — fighting hedging (−8)")
    elif roc_label == "RAPID_COMPRESSION":
        if is_bear:
            delta -= 6
            reasons.append(f"Skew compressing rapidly ({roc_pp:.1f}pp/d) — hedges unwinding (−6)")
        else:
            delta += 6
            reasons.append(f"Skew compressing rapidly ({roc_pp:.1f}pp/d) — relief rally setup (+6)")

    return delta, reasons


# ─────────────────────────────────────────────────────────
# VOL REGIME INTEGRATION
# ─────────────────────────────────────────────────────────

def skew_caution_contribution(skew_result: Dict) -> int:
    """Return caution score adjustment for the canonical vol regime.

    Plug into build_canonical_vol_regime as a 7th input.
    Positive values increase caution.
    """
    if not skew_result or not skew_result.get("ok"):
        return 0

    label = skew_result.get("skew_label", "UNKNOWN")
    roc_label = skew_result.get("roc_label", "UNKNOWN")

    caution = 0
    if label == "EXTREME_STEEP":
        caution += 2  # fear at peak often precedes bounce, but still elevated caution
    elif label == "STEEP":
        caution += 1
    elif label == "INVERTED":
        caution += 1  # squeeze setup is dangerous
    # FLAT and NORMAL add zero

    if roc_label == "RAPID_EXPANSION":
        caution += 1  # something is happening, worth caution

    return caution


# ─────────────────────────────────────────────────────────
# DISPLAY HELPER
# ─────────────────────────────────────────────────────────

def format_skew_line(skew_result: Dict) -> str:
    """One-line summary for trade cards / snapshot display."""
    if not skew_result or not skew_result.get("ok"):
        return ""
    emoji = skew_result.get("skew_emoji", "❓")
    label = skew_result.get("skew_label", "UNKNOWN")
    skew_pp = skew_result.get("skew_pp", 0)
    roc = skew_result.get("daily_roc_pp")
    parts = [f"{emoji} Skew {label}: {skew_pp:+.1f}pp"]
    if roc is not None:
        parts.append(f"ROC {roc:+.1f}pp/d")
    put_iv = skew_result.get("put_iv")
    call_iv = skew_result.get("call_iv")
    if put_iv and call_iv:
        parts.append(f"25Δ put {put_iv*100:.1f}% / call {call_iv*100:.1f}%")
    return "📐 " + " | ".join(parts)
