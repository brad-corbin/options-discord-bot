# options_engine_v3.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Brad's Options Engine v3 — Rule-based ITM Debit Spread Builder
#
# v3.4 additions:
#   - Expected Move calculation on trade cards
#   - IV vs RV (realized vol) edge scoring
#   - Edge data feeds into confidence scoring
#
# v3.6 additions:
#   - Bear put debit spread support (mirror of bull call)
#   - Win probability from short delta
#   - Expected value scoring
#   - EM-aware strike placement
#   - Direction-aware confidence scoring

import math
from typing import Any, Dict, List, Optional, Tuple
from trading_rules import *
# v4.1 imports for hard liquidity, ranking, slippage, journal feedback
from trading_rules import (
    get_liquidity_thresholds,
    SLIPPAGE_SPREAD_FACTOR, SLIPPAGE_MIN_EV_AFTER,
    RANK_WEIGHT_EV, RANK_WEIGHT_WIN_PROB, RANK_WEIGHT_LIQUIDITY,
    RANK_WEIGHT_IV_EDGE, RANK_WEIGHT_EM_DISTANCE, RANK_WEIGHT_WIDTH_EFF,
    RANK_MIN_SCORE, MAX_SPREAD_PCT_OF_MID,
    JOURNAL_FEEDBACK_ENABLED, JOURNAL_MIN_TRADES_FOR_STATS,
    JOURNAL_SUPPRESS_WIN_RATE, JOURNAL_REDUCE_WIN_RATE,
    JOURNAL_REDUCE_SIZE_MULT, JOURNAL_LOOKBACK_SIGNALS,
)
# v5.0 imports for adaptive strike placement, trailing stops, dynamic exits
from trading_rules import (
    SHORT_LEG_PLACEMENT, SHORT_LEG_MIN_DELTA_CALL,
    SHORT_LEG_MIN_DELTA_PUT, SHORT_LEG_DTE_ITM_THRESHOLD,
    TRAILING_STOP_ENABLED, TRAILING_STOP_ACTIVATION_PCT,
    TRAILING_STOP_DISTANCE_PCT, TRAILING_STOP_MIN_DISTANCE,
    DYNAMIC_EXIT_0DTE_ENABLED,
    DYNAMIC_EXIT_EARLY_MULT, DYNAMIC_EXIT_MID_MULT,
    DYNAMIC_EXIT_LATE_MULT, DYNAMIC_EXIT_POWER_HOUR_MULT,
    IV_RV_RATIO_BUYER_EDGE, IV_RV_RATIO_SELLER_EDGE,
)


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def as_float(x, default=0.0):
    if x is None:
        return default
    if isinstance(x, list):
        x = x[0] if x else default
    try:
        return float(x) if x is not None else default
    except (ValueError, TypeError):
        return default


def as_int(x, default=0):
    if x is None:
        return int(default)
    if isinstance(x, list):
        x = x[0] if x else default
    try:
        return int(x) if x is not None else int(default)
    except (ValueError, TypeError):
        return int(default)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Risk-free rate used for probability calculations (current ~4.5%)
_RISK_FREE_RATE = 0.045


def _prob_finish_above(spot: float, strike: float, iv: float, dte: int,
                       r: float = _RISK_FREE_RATE, q: float = 0.0) -> Optional[float]:
    """Risk-neutral probability spot finishes above strike by expiry.
    Uses GBS d2 with risk-free rate and dividend yield (defaults to r=4.5%, q=0).
    """
    if spot <= 0 or strike <= 0 or iv is None or iv <= 0 or dte <= 0:
        return None
    t = max(dte / 365.0, 1.0 / 365.0)
    sigma_t = iv * math.sqrt(t)
    if sigma_t <= 0:
        return None
    try:
        d2 = (math.log(spot / strike) + (r - q - 0.5 * iv * iv) * t) / sigma_t
        return max(0.0, min(1.0, _norm_cdf(d2)))
    except (ValueError, ZeroDivisionError):
        return None


def _prob_finish_below(spot: float, strike: float, iv: float, dte: int,
                       r: float = _RISK_FREE_RATE, q: float = 0.0) -> Optional[float]:
    p_above = _prob_finish_above(spot, strike, iv, dte, r=r, q=q)
    if p_above is None:
        return None
    return max(0.0, min(1.0, 1.0 - p_above))


def estimate_vertical_trade_quality(
    *,
    side: str,
    spot: float,
    long_strike: float,
    short_strike: float,
    width: float,
    debit: float,
    dte: int,
    long_iv: Optional[float],
    short_iv: Optional[float],
    short_delta_abs: float,
) -> Dict[str, float]:
    """More honest trade-quality approximation for debit spreads.

    Returns:
      - win_prob: approximate probability of finishing above/below breakeven at expiry
      - max_profit_prob: approximate probability of finishing through short strike
      - expected_value: ternary approximation (max profit / partial profit / max loss)
    """
    avg_iv = 0.0
    ivs = [v for v in (long_iv, short_iv) if v is not None and v > 0]
    if ivs:
        avg_iv = sum(ivs) / len(ivs)

    if side == 'bull':
        breakeven = long_strike + debit
        prob_profit = _prob_finish_above(spot, breakeven, avg_iv, dte) if avg_iv > 0 else None
        prob_max = _prob_finish_above(spot, short_strike, avg_iv, dte) if avg_iv > 0 else None
    else:
        breakeven = long_strike - debit
        prob_profit = _prob_finish_below(spot, breakeven, avg_iv, dte) if avg_iv > 0 else None
        prob_max = _prob_finish_below(spot, short_strike, avg_iv, dte) if avg_iv > 0 else None

    delta_prob = short_delta_abs if short_delta_abs > 0 else None
    # Blend model probability with short-delta proxy when both exist.
    if prob_max is not None and delta_prob is not None:
        prob_max = (0.65 * prob_max) + (0.35 * delta_prob)
    elif prob_max is None and delta_prob is not None:
        prob_max = delta_prob

    if prob_profit is None:
        # Fallback: estimate profit probability from max-profit probability and spread cost.
        cost_hurdle = max(0.05, min(0.95, debit / width if width > 0 else 0.5))
        if prob_max is not None:
            prob_profit = max(0.0, min(0.99, prob_max + (1.0 - cost_hurdle) * 0.15))
        else:
            prob_profit = 0.5

    if prob_max is None:
        prob_max = max(0.0, min(prob_profit, 0.99))

    prob_max = max(0.0, min(prob_max, prob_profit, 0.99))
    prob_profit = max(prob_max, min(prob_profit, 0.995))

    max_profit = max(width - debit, 0.0)
    max_loss = max(debit, 0.0)
    # Partial-profit region sits between breakeven and short strike.
    partial_prob = max(0.0, prob_profit - prob_max)
    partial_profit = max_profit * 0.50  # linear P&L midpoint between breakeven and short strike
    ev = (prob_max * max_profit) + (partial_prob * partial_profit) - ((1.0 - prob_profit) * max_loss)

    return {
        'avg_iv_used': round(avg_iv, 4) if avg_iv > 0 else 0.0,
        'breakeven': round(breakeven, 4),
        'win_prob': round(prob_profit, 4),
        'max_profit_prob': round(prob_max, 4),
        'expected_value': round(ev, 4),
    }


def detect_available_widths(strikes: List[float], spot: float) -> List[float]:
    """Detect available spread widths from strike increments."""
    itm_strikes = sorted([k for k in strikes if k != spot])
    if len(itm_strikes) < 2:
        return []

    increments = set()
    for i in range(len(itm_strikes) - 1):
        diff = round(abs(itm_strikes[i + 1] - itm_strikes[i]), 2)
        if diff > 0:
            increments.add(diff)

    available = []
    for w in WIDTH_PREFERENCE:
        for inc in increments:
            if abs(w / inc - round(w / inc)) < 0.01:
                available.append(w)
                break

    if NO_HALF_DOLLAR_WIDTHS:
        available = [w for w in available if abs(w - 0.50) > 0.01]

    return available


# ─────────────────────────────────────────────────────────
# EXPECTED MOVE & IV vs RV EDGE (v3.4)
# ─────────────────────────────────────────────────────────

def calc_expected_move(spot: float, iv: float, dte: int) -> float:
    """
    Expected Move = spot × IV × sqrt(DTE / 365)
    Returns dollar amount (one side, ~1 std dev).
    """
    if iv is None or iv <= 0 or dte <= 0:
        return 0.0
    return round(spot * iv * math.sqrt(dte / 365.0), 2)


def calc_realized_vol(closes: List[float]) -> float:
    """
    Annualized realized (historical) volatility from daily closes.
    Uses log returns × sqrt(252).
    """
    if not closes or len(closes) < 3:
        return 0.0

    log_returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            log_returns.append(math.log(closes[i] / closes[i - 1]))

    if len(log_returns) < 2:
        return 0.0

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_vol = math.sqrt(variance)
    annual_vol = daily_vol * math.sqrt(RV_ANNUALIZE_FACTOR)
    return round(annual_vol, 4)


def calc_iv_rv_edge(iv: float, rv: float) -> Dict:
    """
    Compare implied volatility to realized volatility.
    For debit spread buyers: RV > IV = good (cheap vol).
    """
    if iv <= 0 or rv <= 0:
        return {
            "edge_pct":    0.0,
            "edge_label":  "UNKNOWN",
            "edge_emoji":  "❓",
            "description": "Insufficient vol data",
            "iv":          iv,
            "rv":          rv,
        }

    iv_pct = iv * 100
    rv_pct = rv * 100
    edge_pct = round(iv_pct - rv_pct, 1)

    if edge_pct < IV_RV_BUYER_EDGE_PCT:
        label = "BUYER"
        emoji = "🟢"
        desc = f"IV cheap vs realized ({iv_pct:.0f}% vs {rv_pct:.0f}%) — vol discount"
    elif edge_pct > IV_RV_SELLER_EDGE_PCT:
        label = "SELLER"
        emoji = "🔴"
        desc = f"IV rich vs realized ({iv_pct:.0f}% vs {rv_pct:.0f}%) — vol premium"
    else:
        label = "NEUTRAL"
        emoji = "⚪"
        desc = f"IV ≈ RV ({iv_pct:.0f}% vs {rv_pct:.0f}%) — fair priced"

    return {
        "edge_pct":    edge_pct,
        "edge_label":  label,
        "edge_emoji":  emoji,
        "description": desc,
        "iv":          iv,
        "rv":          rv,
        "iv_pct":      round(iv_pct, 1),
        "rv_pct":      round(rv_pct, 1),
    }


def get_avg_chain_iv(contracts: List[Dict], spot: float, side: str = "both") -> float:
    """Average IV from ATM options (within 3% of spot).

    Args:
        side: "call", "put", or "both". Prefer call IVs for bull spreads,
              put IVs for bear spreads. Defaults to "both" for backward compat.
    """
    atm_range = spot * 0.03
    ivs = []

    for c in contracts:
        right = (c.get("right") or "").lower()
        strike = as_float(c.get("strike"), 0)
        iv = as_float(c.get("iv"), 0)
        if iv <= 0 or abs(strike - spot) > atm_range:
            continue
        if side == "call" and right != "call":
            continue
        if side == "put" and right != "put":
            continue
        ivs.append(iv)

    if not ivs:
        # Fallback: any ATM IV regardless of side
        for c in contracts:
            strike = as_float(c.get("strike"), 0)
            iv = as_float(c.get("iv"), 0)
            if iv > 0 and abs(strike - spot) <= atm_range:
                ivs.append(iv)

    if not ivs:
        # Last resort: nearest available IVs
        for c in contracts:
            iv = as_float(c.get("iv"), 0)
            if iv > 0:
                ivs.append(iv)

    return sum(ivs) / len(ivs) if ivs else 0.0


# ─────────────────────────────────────────────────────────
# CHAIN DATA BUILDERS
# ─────────────────────────────────────────────────────────

def build_call_quotes(contracts: List[Dict], spot: float, ticker: str = "", include_otm: bool = False) -> Dict[float, Dict]:
    """Liquid call quotes. Default returns ITM-only for backward compatibility.
    Set include_otm=True to return the full liquid call ladder for hybrid entry selection.
    """
    _liq = get_liquidity_thresholds(ticker)

    for pass_name, mult in [("strict", 1.0), ("relaxed", 2.5)]:
        _liq_min_oi = max(int(_liq["min_oi"] / mult), 1)
        _liq_max_spread = _liq["max_spread"] * mult
        _liq_max_spread_pct = min(_liq["max_spread_pct"] * mult, 0.50)

        quotes = {}
        filtered_reasons = {}
        for c in contracts:
            right = (c.get("right") or "").lower()
            if right != "call":
                continue

            strike = as_float(c.get("strike"), None)
            if strike is None:
                continue
            if not include_otm and strike >= spot:
                continue

            bid = as_float(c.get("bid"), None)
            ask = as_float(c.get("ask"), None)
            mid = as_float(c.get("mid"), None)
            oi  = as_int(c.get("openInterest"), 0)
            vol = as_int(c.get("volume"), 0)
            delta = as_float(c.get("delta"), None)
            iv  = as_float(c.get("iv"), None)
            theta = as_float(c.get("theta"), None)
            vega = as_float(c.get("vega"), None)
            gamma = as_float(c.get("gamma"), None)

            if mid is None and bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
            if mid is None or mid <= 0:
                filtered_reasons[strike] = "no mid price"
                continue

            warnings = []
            spread_abs = (ask - bid) if (bid is not None and ask is not None) else 0
            spread_pct = spread_abs / mid if mid > 0 else 0

            if oi is not None and oi < _liq_min_oi:
                filtered_reasons[strike] = f"OI {oi} < {_liq_min_oi}"
                continue
            if spread_abs > _liq_max_spread:
                filtered_reasons[strike] = f"spread ${spread_abs:.2f} > ${_liq_max_spread:.2f}"
                continue
            if spread_pct > _liq_max_spread_pct:
                filtered_reasons[strike] = f"spread {spread_pct:.0%} > {_liq_max_spread_pct:.0%}"
                continue

            if oi is not None and oi < _liq["min_oi"] * 2:
                warnings.append(f"Marginal OI ({oi})")
            if spread_pct > _liq["max_spread_pct"] * 0.7:
                warnings.append(f"B/A {spread_pct:.0%} of mid")

            quotes[strike] = {
                "strike": strike, "mid": mid, "bid": bid, "ask": ask,
                "oi": oi, "volume": vol, "delta": delta, "iv": iv,
                "theta": theta, "vega": vega, "gamma": gamma,
                "itm_amount": round(max(spot - strike, 0.0), 2),
                "otm_amount": round(max(strike - spot, 0.0), 2),
                "spread_pct": round(spread_pct, 4),
                "warnings": warnings,
            }

        if len(quotes) >= 2 or pass_name == "relaxed":
            if pass_name == "relaxed" and quotes:
                import logging as _log
                _log.getLogger(__name__).info(
                    f"build_call_quotes({ticker}): relaxed pass found {len(quotes)} strikes "
                    f"(strict found <2). Filters relaxed {mult:.1f}x. include_otm={include_otm}"
                )
            if pass_name == "strict" and len(quotes) < 2 and filtered_reasons:
                import logging as _log
                top_filtered = sorted(filtered_reasons.items(), key=lambda x: -x[0])[:5]
                _log.getLogger(__name__).info(
                    f"build_call_quotes({ticker}): strict pass filtered all candidate calls. "
                    f"Top filtered: {', '.join(f'${k:.0f}:{v}' for k,v in top_filtered)}"
                )
            return quotes

    return quotes


def build_put_quotes(contracts: List[Dict], spot: float, ticker: str = "", include_otm: bool = False) -> Dict[float, Dict]:
    """Liquid put quotes. Default returns ITM-only for backward compatibility.
    Set include_otm=True to return the full liquid put ladder for hybrid entry selection.
    """
    _liq = get_liquidity_thresholds(ticker)

    for pass_name, mult in [("strict", 1.0), ("relaxed", 2.5)]:
        _liq_min_oi = max(int(_liq["min_oi"] / mult), 1)
        _liq_max_spread = _liq["max_spread"] * mult
        _liq_max_spread_pct = min(_liq["max_spread_pct"] * mult, 0.50)

        quotes = {}
        filtered_reasons = {}
        for c in contracts:
            right = (c.get("right") or "").lower()
            if right != "put":
                continue

            strike = as_float(c.get("strike"), None)
            if strike is None:
                continue
            if not include_otm and strike <= spot:
                continue

            bid = as_float(c.get("bid"), None)
            ask = as_float(c.get("ask"), None)
            mid = as_float(c.get("mid"), None)
            oi  = as_int(c.get("openInterest"), 0)
            vol = as_int(c.get("volume"), 0)
            delta = as_float(c.get("delta"), None)
            iv  = as_float(c.get("iv"), None)
            theta = as_float(c.get("theta"), None)
            vega = as_float(c.get("vega"), None)
            gamma = as_float(c.get("gamma"), None)

            if mid is None and bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
            if mid is None or mid <= 0:
                filtered_reasons[strike] = "no mid price"
                continue

            warnings = []
            spread_abs = (ask - bid) if (bid is not None and ask is not None) else 0
            spread_pct = spread_abs / mid if mid > 0 else 0

            if oi is not None and oi < _liq_min_oi:
                filtered_reasons[strike] = f"OI {oi} < {_liq_min_oi}"
                continue
            if spread_abs > _liq_max_spread:
                filtered_reasons[strike] = f"spread ${spread_abs:.2f} > ${_liq_max_spread:.2f}"
                continue
            if spread_pct > _liq_max_spread_pct:
                filtered_reasons[strike] = f"spread {spread_pct:.0%} > {_liq_max_spread_pct:.0%}"
                continue

            if oi is not None and oi < _liq["min_oi"] * 2:
                warnings.append(f"Marginal OI ({oi})")
            if spread_pct > _liq["max_spread_pct"] * 0.7:
                warnings.append(f"B/A {spread_pct:.0%} of mid")

            quotes[strike] = {
                "strike": strike, "mid": mid, "bid": bid, "ask": ask,
                "oi": oi, "volume": vol, "delta": delta, "iv": iv,
                "theta": theta, "vega": vega, "gamma": gamma,
                "itm_amount": round(max(strike - spot, 0.0), 2),
                "otm_amount": round(max(spot - strike, 0.0), 2),
                "spread_pct": round(spread_pct, 4),
                "warnings": warnings,
            }

        if len(quotes) >= 2 or pass_name == "relaxed":
            if pass_name == "relaxed" and quotes:
                import logging as _log
                _log.getLogger(__name__).info(
                    f"build_put_quotes({ticker}): relaxed pass found {len(quotes)} strikes "
                    f"(strict found <2). Filters relaxed {mult:.1f}x. include_otm={include_otm}"
                )
            if pass_name == "strict" and len(quotes) < 2 and filtered_reasons:
                import logging as _log
                top_filtered = sorted(filtered_reasons.items())[:5]
                _log.getLogger(__name__).info(
                    f"build_put_quotes({ticker}): strict pass filtered all candidate puts. "
                    f"Top filtered: {', '.join(f'${k:.0f}:{v}' for k,v in top_filtered)}"
                )
            return quotes

    return quotes



# ─────────────────────────────────────────────────────────
# HYBRID ENTRY HELPERS
# ─────────────────────────────────────────────────────────

HYBRID_ENTRY_ENABLED = True
HYBRID_ATM_BAND_PCT = 0.0035
HYBRID_EARLY_MAX_COST_PCT = 0.50
HYBRID_BALANCED_MAX_COST_PCT = 0.62
HYBRID_EARLY_MIN_COST_PCT = 0.10
HYBRID_BALANCED_MIN_COST_PCT = 0.15
HYBRID_MAX_SHORT_OTM_EM_FRACTION = 0.60
HYBRID_PROFILE_SIZE_MULTIPLIER = {
    "conservative_itm": 1.00,
    "balanced_transition": 0.85,
    "early_atm": 0.70,
}
HYBRID_PROFILE_LABEL = {
    "conservative_itm": "Conservative ITM",
    "balanced_transition": "Balanced",
    "early_atm": "Early ATM",
}


def _atm_band(spot: float) -> float:
    return max(0.25, spot * HYBRID_ATM_BAND_PCT)


def _classify_call_bucket(strike: float, spot: float, atm_band: float) -> str:
    if strike < (spot - atm_band):
        return "itm"
    if strike <= (spot + atm_band):
        return "atm"
    return "otm"


def _classify_put_bucket(strike: float, spot: float, atm_band: float) -> str:
    if strike > (spot + atm_band):
        return "itm"
    if strike >= (spot - atm_band):
        return "atm"
    return "otm"


def _profile_cost_bounds(profile: str) -> Tuple[float, float]:
    if profile == "early_atm":
        return HYBRID_EARLY_MIN_COST_PCT, min(MAX_COST_PCT_OF_WIDTH, HYBRID_EARLY_MAX_COST_PCT)
    if profile == "balanced_transition":
        return HYBRID_BALANCED_MIN_COST_PCT, min(MAX_COST_PCT_OF_WIDTH, HYBRID_BALANCED_MAX_COST_PCT)
    return MIN_COST_PCT_OF_WIDTH, MAX_COST_PCT_OF_WIDTH


def _profile_fit_multiplier(profile: str, aggression: str) -> float:
    matrix = {
        "conservative": {
            "conservative_itm": 1.08,
            "balanced_transition": 0.95,
            "early_atm": 0.82,
        },
        "balanced": {
            "conservative_itm": 0.99,
            "balanced_transition": 1.08,
            "early_atm": 0.92,
        },
        "early": {
            "conservative_itm": 0.92,
            "balanced_transition": 1.04,
            "early_atm": 1.10,
        },
    }
    return matrix.get(aggression, matrix["balanced"]).get(profile, 1.0)


def _entry_size_multiplier(profile: str) -> float:
    return HYBRID_PROFILE_SIZE_MULTIPLIER.get(profile, 1.0)


def _dynamic_min_win_probability(profile: str, confidence: int) -> float:
    floor = MIN_WIN_PROBABILITY
    if profile == "balanced_transition" and confidence >= 65:
        return max(0.42, floor - 0.03)
    if profile == "early_atm" and confidence >= 72:
        return max(0.38, floor - 0.07)
    return floor


def _determine_entry_plan(
    webhook_data: Dict,
    bias: str,
    regime: Optional[Dict] = None,
    v4_flow: Optional[Dict] = None,
    vol_edge: Optional[Dict] = None,
    dte: int = 3,  # v5.0: DTE-aware entry plan
) -> Dict[str, Any]:
    regime = regime or {}
    v4_flow = v4_flow or {}
    vol_edge = vol_edge or {}

    score = 0.0
    reasons = []
    is_bear = bias == "bear"

    tier = str(webhook_data.get("tier", "2") or "2")
    if tier == "1":
        score += 1.0
        reasons.append("T1 signal")
    else:
        score += 0.3
        reasons.append("T2/manual signal")

    if webhook_data.get("htf_confirmed"):
        score += 1.0
        reasons.append("HTF confirmed")
    elif webhook_data.get("htf_converging"):
        score += 0.5
        reasons.append("HTF converging")

    daily_bull = bool(webhook_data.get("daily_bull", False))
    if (not is_bear and daily_bull) or (is_bear and not daily_bull):
        score += 0.9
        reasons.append("daily trend aligned")
    else:
        score -= 0.6
        reasons.append("daily trend fighting")

    rsi_mfi_bull = bool(webhook_data.get("rsi_mfi_bull", False))
    above_vwap = bool(webhook_data.get("above_vwap", False))
    wt2 = as_float(webhook_data.get("wt2"), 0)
    if not is_bear and rsi_mfi_bull:
        score += 0.4
        reasons.append("RSI/MFI aligned")
    if is_bear and not rsi_mfi_bull:
        score += 0.4
        reasons.append("RSI/MFI aligned")
    if not is_bear and above_vwap:
        score += 0.4
        reasons.append("VWAP aligned")
    if is_bear and not above_vwap:
        score += 0.4
        reasons.append("VWAP aligned")
    if not is_bear and wt2 < -30:
        score += 0.4
        reasons.append("wave oversold")
    if is_bear and wt2 > 60:
        score += 0.4
        reasons.append("wave overbought")

    if v4_flow:
        v4_bias = (v4_flow.get("bias") or "").upper()
        if (not is_bear and v4_bias == "UPSIDE") or (is_bear and v4_bias == "DOWNSIDE"):
            score += 0.9
            reasons.append("dealer flow aligned")
        elif v4_bias in {"UPSIDE", "DOWNSIDE"}:
            score -= 0.8
            reasons.append("dealer flow fights")

        gex = as_float(v4_flow.get("gex"), 0)
        if gex < 0:
            score += 0.45
            reasons.append("negative GEX")
        elif gex > 0:
            score -= 0.35
            reasons.append("positive GEX")

        conf_label = (v4_flow.get("confidence_label") or "").upper()
        if conf_label == "HIGH":
            score += 0.25
        elif conf_label == "LOW":
            score -= 0.35

    adx_regime = (regime.get("adx_regime") or "").upper()
    vix_regime = (regime.get("vix_regime") or "").upper()
    if adx_regime == "TRENDING":
        score += 0.65
        reasons.append("trending tape")
    elif adx_regime == "CHOPPY":
        score -= 0.85
        reasons.append("choppy tape")

    if vix_regime in {"LOW", "NORMAL"}:
        score += 0.2
    elif vix_regime == "CRISIS":
        if is_bear:
            score += 0.15
        else:
            score -= 0.65
            reasons.append("fear tape vs bull")

    edge_label = (vol_edge.get("edge_label") or "").upper()
    if edge_label == "BUYER":
        score += 0.25
    elif edge_label == "SELLER":
        score -= 0.15

    # ── v5.0: DTE-based aggression adjustment ──
    # 0-1 DTE: push toward early_atm/balanced — cheaper spreads work,
    #          theta is your friend on the short leg.
    # 2-3 DTE: neutral — let other signals decide.
    # 4+ DTE: push toward conservative — more time = more risk on short leg.
    if dte <= 1:
        score += 1.2
        reasons.append("0-1 DTE favors ATM short leg")
    elif dte <= 3:
        score += 0.4
        reasons.append("short DTE favors balanced entry")
    elif dte >= 7:
        score -= 0.6
        reasons.append("multi-day hold favors ITM protection")

    # v5.0: IV/RV ratio in entry plan
    _iv_val = vol_edge.get("iv", 0) if vol_edge else 0
    _rv_val = vol_edge.get("rv", 0) if vol_edge else 0
    if _rv_val > 0 and _iv_val > 0:
        _iv_rv_ratio = _iv_val / _rv_val
        if _iv_rv_ratio < 0.90:
            score += 0.5
            reasons.append("cheap IV — aggressive entry OK")
        elif _iv_rv_ratio > 1.15:
            score -= 0.4
            reasons.append("rich IV — conservative entry preferred")

    if score >= 4.0:
        aggression = "early"
        preferred = ["early_atm", "balanced_transition", "conservative_itm"]
    elif score >= 2.2:
        aggression = "balanced"
        preferred = ["balanced_transition", "conservative_itm", "early_atm"]
    else:
        aggression = "conservative"
        preferred = ["conservative_itm", "balanced_transition", "early_atm"]

    return {
        "score": round(score, 2),
        "aggression": aggression,
        "preferred_profiles": preferred,
        "reasons": reasons[:6],
    }


def _should_skip_call_profile(profile: str, long_k: float, short_k: float, spot: float, expected_move: float, atm_band: float) -> bool:
    if profile not in {"conservative_itm", "balanced_transition", "early_atm"}:
        return True
    if profile == "early_atm":
        max_short = spot + max(atm_band, expected_move * HYBRID_MAX_SHORT_OTM_EM_FRACTION if expected_move > 0 else atm_band * 2)
        if short_k > max_short:
            return True
    if profile == "balanced_transition" and expected_move > 0:
        max_short = spot + expected_move
        if short_k > max_short:
            return True
    return False


def _should_skip_put_profile(profile: str, long_k: float, short_k: float, spot: float, expected_move: float, atm_band: float) -> bool:
    if profile not in {"conservative_itm", "balanced_transition", "early_atm"}:
        return True
    if profile == "early_atm":
        min_short = spot - max(atm_band, expected_move * HYBRID_MAX_SHORT_OTM_EM_FRACTION if expected_move > 0 else atm_band * 2)
        if short_k < min_short:
            return True
    if profile == "balanced_transition" and expected_move > 0:
        min_short = spot - expected_move
        if short_k < min_short:
            return True
    return False


def _classify_bull_profile(long_k: float, short_k: float, spot: float, atm_band: float) -> Optional[Tuple[str, str, str]]:
    long_bucket = _classify_call_bucket(long_k, spot, atm_band)
    short_bucket = _classify_call_bucket(short_k, spot, atm_band)
    if long_bucket == "itm" and short_bucket == "itm":
        return "conservative_itm", long_bucket, short_bucket
    if long_bucket == "itm" and short_bucket in {"atm", "otm"}:
        return "balanced_transition", long_bucket, short_bucket
    if long_bucket == "atm" and short_bucket == "otm":
        return "early_atm", long_bucket, short_bucket
    return None


def _classify_bear_profile(long_k: float, short_k: float, spot: float, atm_band: float) -> Optional[Tuple[str, str, str]]:
    long_bucket = _classify_put_bucket(long_k, spot, atm_band)
    short_bucket = _classify_put_bucket(short_k, spot, atm_band)
    if long_bucket == "itm" and short_bucket == "itm":
        return "conservative_itm", long_bucket, short_bucket
    if long_bucket == "itm" and short_bucket in {"atm", "otm"}:
        return "balanced_transition", long_bucket, short_bucket
    if long_bucket == "atm" and short_bucket == "otm":
        return "early_atm", long_bucket, short_bucket
    return None

# ─────────────────────────────────────────────────────────

def build_bull_call_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
    expected_move: float = 0,
    dte: int = 0,
    atm_band: float = 0.0,
) -> List[Dict]:
    candidates = []
    strikes = sorted(quotes.keys())
    atm_band = atm_band or _atm_band(spot)
    em_upper = (spot + expected_move) if expected_move > 0 else 0

    for long_k in strikes:
        long_q = quotes[long_k]
        for width in available_widths:
            short_k = round(long_k + width, 2)
            short_q = quotes.get(short_k)
            if short_q is None:
                continue

            # v5.0: Delta-based short leg filter
            # Replaces rigid BOTH_LEGS_ITM with continuous delta constraint.
            # Allows ATM short leg for 0-3 DTE while preventing far OTM.
            _short_delta_val = abs(short_q.get("delta") or 0)
            if SHORT_LEG_PLACEMENT == "DELTA":
                _min_delta = 0.55 if dte >= SHORT_LEG_DTE_ITM_THRESHOLD else abs(SHORT_LEG_MIN_DELTA_CALL)
                if _short_delta_val > 0 and _short_delta_val < _min_delta:
                    continue

            profile_info = _classify_bull_profile(long_k, short_k, spot, atm_band)
            if not profile_info:
                continue
            profile, long_bucket, short_bucket = profile_info
            if _should_skip_call_profile(profile, long_k, short_k, spot, expected_move, atm_band):
                continue

            debit = round(long_q["mid"] - short_q["mid"], 4)
            if debit <= 0:
                continue

            cost_pct = debit / width
            min_cost, max_cost = _profile_cost_bounds(profile)
            if cost_pct > max_cost or cost_pct < min_cost:
                continue

            max_profit = round(width - debit, 4)
            max_loss = debit
            ror = round(max_profit / max_loss, 4) if max_loss > 0 else 0

            net_theta = net_vega = net_delta = net_gamma = None
            if long_q.get("theta") is not None and short_q.get("theta") is not None:
                net_theta = round(long_q["theta"] - short_q["theta"], 4)
            if long_q.get("vega") is not None and short_q.get("vega") is not None:
                net_vega = round(long_q["vega"] - short_q["vega"], 4)
            if long_q.get("delta") is not None and short_q.get("delta") is not None:
                net_delta = round(long_q["delta"] - short_q["delta"], 4)
            long_gamma = long_q.get("gamma") or 0
            short_gamma = short_q.get("gamma") or 0
            if long_gamma or short_gamma:
                net_gamma = round(long_gamma - short_gamma, 6)

            short_delta_abs = abs(short_q.get("delta") or 0)
            quality = estimate_vertical_trade_quality(
                side="bull",
                spot=spot,
                long_strike=long_k,
                short_strike=short_k,
                width=width,
                debit=debit,
                dte=dte,
                long_iv=long_q.get("iv"),
                short_iv=short_q.get("iv"),
                short_delta_abs=short_delta_abs,
            )
            win_prob = quality["win_prob"]
            ev = quality["expected_value"]

            em_proximity = None
            em_zone = "unknown"
            if em_upper > 0:
                em_proximity = round(em_upper - short_k, 2)
                em_zone = "inside" if short_k <= em_upper else "outside"

            same_day_target = round(debit * (1 + SAME_DAY_EXIT_PCT), 2)
            next_day_target = round(debit * (1 + NEXT_DAY_EXIT_PCT), 2)
            extended_target = round(debit * (1 + EXTENDED_HOLD_EXIT_PCT), 2)

            natural_debit = None
            if long_q.get("ask") is not None and short_q.get("bid") is not None:
                try:
                    natural_debit = max(0.0, float(long_q.get("ask") or 0) - float(short_q.get("bid") or 0))
                except Exception:
                    natural_debit = None

            candidates.append({
                "long": long_k,
                "short": short_k,
                "width": width,
                "debit": round(debit, 2),
                "natural_debit": round(natural_debit, 2) if natural_debit is not None else None,
                "cost_pct": round(cost_pct * 100, 1),
                "max_profit": round(max_profit, 2),
                "max_loss": round(max_loss, 2),
                "ror": ror,
                "long_itm": long_q["itm_amount"],
                "short_itm": short_q["itm_amount"],
                "long_delta": long_q.get("delta"),
                "short_delta": short_q.get("delta"),
                "long_oi": long_q.get("oi"),
                "short_oi": short_q.get("oi"),
                "long_bid": long_q.get("bid"),
                "long_ask": long_q.get("ask"),
                "short_bid": short_q.get("bid"),
                "short_ask": short_q.get("ask"),
                "long_spread_pct": long_q.get("spread_pct"),
                "short_spread_pct": short_q.get("spread_pct"),
                "net_theta": net_theta,
                "net_vega": net_vega,
                "net_delta": net_delta,
                "net_gamma": net_gamma,
                "win_prob": win_prob,
                "max_profit_prob": quality.get("max_profit_prob"),
                "breakeven": quality.get("breakeven"),
                "expected_value": ev,
                "em_proximity": em_proximity,
                "em_zone": em_zone,
                "same_day_exit": same_day_target,
                "next_day_exit": next_day_target,
                "extended_exit": extended_target,
                "entry_profile": profile,
                "profile_label": HYBRID_PROFILE_LABEL.get(profile, profile),
                "long_bucket": long_bucket,
                "short_bucket": short_bucket,
                "warnings": long_q["warnings"] + short_q["warnings"],
            })

    return candidates


def build_bear_put_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
    expected_move: float = 0,
    dte: int = 0,
    atm_band: float = 0.0,
) -> List[Dict]:
    candidates = []
    strikes = sorted(quotes.keys())
    atm_band = atm_band or _atm_band(spot)
    em_lower = (spot - expected_move) if expected_move > 0 else 0

    for long_k in strikes:
        long_q = quotes[long_k]
        for width in available_widths:
            short_k = round(long_k - width, 2)
            short_q = quotes.get(short_k)
            if short_q is None:
                continue

            # v5.0: Delta-based short leg filter (bear puts)
            _short_delta_val = abs(short_q.get("delta") or 0)
            if SHORT_LEG_PLACEMENT == "DELTA":
                _min_delta = 0.55 if dte >= SHORT_LEG_DTE_ITM_THRESHOLD else abs(SHORT_LEG_MIN_DELTA_PUT)
                if _short_delta_val > 0 and _short_delta_val < _min_delta:
                    continue

            profile_info = _classify_bear_profile(long_k, short_k, spot, atm_band)
            if not profile_info:
                continue
            profile, long_bucket, short_bucket = profile_info
            if _should_skip_put_profile(profile, long_k, short_k, spot, expected_move, atm_band):
                continue

            debit = round(long_q["mid"] - short_q["mid"], 4)
            if debit <= 0:
                continue

            cost_pct = debit / width
            min_cost, max_cost = _profile_cost_bounds(profile)
            if cost_pct > max_cost or cost_pct < min_cost:
                continue

            max_profit = round(width - debit, 4)
            max_loss = debit
            ror = round(max_profit / max_loss, 4) if max_loss > 0 else 0

            net_theta = net_vega = net_delta = net_gamma = None
            if long_q.get("theta") is not None and short_q.get("theta") is not None:
                net_theta = round(long_q["theta"] - short_q["theta"], 4)
            if long_q.get("vega") is not None and short_q.get("vega") is not None:
                net_vega = round(long_q["vega"] - short_q["vega"], 4)
            if long_q.get("delta") is not None and short_q.get("delta") is not None:
                net_delta = round(long_q["delta"] - short_q["delta"], 4)
            long_gamma = long_q.get("gamma") or 0
            short_gamma = short_q.get("gamma") or 0
            if long_gamma or short_gamma:
                net_gamma = round(long_gamma - short_gamma, 6)

            short_delta_abs = abs(short_q.get("delta") or 0)
            quality = estimate_vertical_trade_quality(
                side="bear",
                spot=spot,
                long_strike=long_k,
                short_strike=short_k,
                width=width,
                debit=debit,
                dte=dte,
                long_iv=long_q.get("iv"),
                short_iv=short_q.get("iv"),
                short_delta_abs=short_delta_abs,
            )
            win_prob = quality["win_prob"]
            ev = quality["expected_value"]

            em_proximity = None
            em_zone = "unknown"
            if em_lower > 0:
                em_proximity = round(short_k - em_lower, 2)
                em_zone = "inside" if short_k >= em_lower else "outside"

            same_day_target = round(debit * (1 + SAME_DAY_EXIT_PCT), 2)
            next_day_target = round(debit * (1 + NEXT_DAY_EXIT_PCT), 2)
            extended_target = round(debit * (1 + EXTENDED_HOLD_EXIT_PCT), 2)

            natural_debit = None
            if long_q.get("ask") is not None and short_q.get("bid") is not None:
                try:
                    natural_debit = max(0.0, float(long_q.get("ask") or 0) - float(short_q.get("bid") or 0))
                except Exception:
                    natural_debit = None

            candidates.append({
                "long": long_k,
                "short": short_k,
                "width": width,
                "debit": round(debit, 2),
                "natural_debit": round(natural_debit, 2) if natural_debit is not None else None,
                "cost_pct": round(cost_pct * 100, 1),
                "max_profit": round(max_profit, 2),
                "max_loss": round(max_loss, 2),
                "ror": ror,
                "long_itm": long_q["itm_amount"],
                "short_itm": short_q["itm_amount"],
                "long_delta": long_q.get("delta"),
                "short_delta": short_q.get("delta"),
                "long_oi": long_q.get("oi"),
                "short_oi": short_q.get("oi"),
                "long_bid": long_q.get("bid"),
                "long_ask": long_q.get("ask"),
                "short_bid": short_q.get("bid"),
                "short_ask": short_q.get("ask"),
                "long_spread_pct": long_q.get("spread_pct"),
                "short_spread_pct": short_q.get("spread_pct"),
                "net_theta": net_theta,
                "net_vega": net_vega,
                "net_delta": net_delta,
                "net_gamma": net_gamma,
                "win_prob": win_prob,
                "max_profit_prob": quality.get("max_profit_prob"),
                "breakeven": quality.get("breakeven"),
                "expected_value": ev,
                "em_proximity": em_proximity,
                "em_zone": em_zone,
                "same_day_exit": same_day_target,
                "next_day_exit": next_day_target,
                "extended_exit": extended_target,
                "entry_profile": profile,
                "profile_label": HYBRID_PROFILE_LABEL.get(profile, profile),
                "long_bucket": long_bucket,
                "short_bucket": short_bucket,
                "warnings": long_q["warnings"] + short_q["warnings"],
            })

    return candidates


def build_itm_debit_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
    expected_move: float = 0,
    dte: int = 0,
) -> List[Dict]:
    """Backward-compatible wrapper for the conservative bull profile only."""
    candidates = build_bull_call_spreads(quotes, spot, available_widths, expected_move, dte=dte, atm_band=_atm_band(spot))
    return [c for c in candidates if c.get("entry_profile") == "conservative_itm"]


def build_itm_bear_put_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
    expected_move: float = 0,
    dte: int = 0,
) -> List[Dict]:
    """Backward-compatible wrapper for the conservative bear profile only."""
    candidates = build_bear_put_spreads(quotes, spot, available_widths, expected_move, dte=dte, atm_band=_atm_band(spot))
    return [c for c in candidates if c.get("entry_profile") == "conservative_itm"]



# ─────────────────────────────────────────────────────────
# RANKING
# ─────────────────────────────────────────────────────────

def rank_candidates(candidates: List[Dict], iv_edge_label: str = "NEUTRAL", entry_plan: Dict = None) -> List[Dict]:
    """
    Composite scoring model with hybrid-entry preference.
    Candidates are scored on quality, then nudged toward the entry profile
    that best matches the current directional conviction.
    """
    if not candidates:
        return []

    entry_plan = entry_plan or {}
    aggression = entry_plan.get("aggression", "balanced")

    evs = [c.get("expected_value", 0) for c in candidates]
    ev_max = max(abs(e) for e in evs) if evs else 1.0
    ev_max = max(ev_max, 0.01)

    scored = []
    for c in candidates:
        spread_pct_avg = 0
        long_spread = c.get("long_spread_pct", 0) or 0
        short_spread = c.get("short_spread_pct", 0) or 0
        if long_spread or short_spread:
            spread_pct_avg = (long_spread + short_spread) / 2

        debit = c.get("debit", 0)
        warnings = c.get("warnings", [])
        est_slippage = debit * SLIPPAGE_SPREAD_FACTOR * spread_pct_avg if spread_pct_avg > 0 else 0.01
        ev_raw = c.get("expected_value", 0)
        ev_after_slippage = ev_raw - est_slippage
        c["ev_after_slippage"] = round(ev_after_slippage, 4)
        c["est_slippage"] = round(est_slippage, 4)

        natural_debit = c.get("natural_debit")
        display_debit = debit + est_slippage
        if natural_debit is not None:
            display_debit = min(float(natural_debit), display_debit)
        c["display_debit"] = round(display_debit, 2)

        if ev_after_slippage <= SLIPPAGE_MIN_EV_AFTER:
            c["_rejected"] = "slippage_or_negative_ev"
            continue

        ev_score = max(0, (ev_after_slippage / ev_max + 1) / 2)
        if ev_after_slippage <= 0:
            ev_score = 0

        wp = c.get("win_prob", 0.5)
        wp_score = min(wp, 1.0)
        breakeven_prob_needed = max(0.05, min(0.95, c.get("debit", 0) / max(c.get("width", 1.0), 0.01)))
        if wp < breakeven_prob_needed:
            wp_score *= 0.7

        warn_count = len(warnings)
        liq_score = max(0, 1.0 - warn_count * 0.25)
        long_oi = c.get("long_oi", 0) or 0
        short_oi = c.get("short_oi", 0) or 0
        avg_oi = (long_oi + short_oi) / 2
        if avg_oi >= 5000:
            liq_score = min(1.0, liq_score + 0.2)
        elif avg_oi >= 2000:
            liq_score = min(1.0, liq_score + 0.1)

        iv_score = 0.5
        if iv_edge_label == "BUYER":
            iv_score = 0.85
        elif iv_edge_label == "SELLER":
            iv_score = 0.15

        em_zone = c.get("em_zone", "unknown")
        em_prox = c.get("em_proximity")
        if em_zone == "inside" and em_prox is not None:
            em_score = min(1.0, 0.6 + 0.4 * max(0, 1 - abs(em_prox) / 5.0))
        elif em_zone == "outside":
            em_score = 0.2
        else:
            em_score = 0.4

        width = c.get("width", 5)
        width_score = 0.95 if width <= 1.0 else 0.78 if width <= 2.5 else 0.52 if width <= 5.0 else 0.35
        cost_pct = (c.get("cost_pct", 100) or 100) / 100.0
        if cost_pct >= 0.68:
            width_score *= 0.8
        if c.get("entry_profile") == "early_atm" and cost_pct <= 0.35:
            width_score = min(1.0, width_score + 0.10)
        if c.get("entry_profile") == "balanced_transition" and cost_pct <= 0.50:
            width_score = min(1.0, width_score + 0.05)

        composite = (
            ev_score * RANK_WEIGHT_EV +
            wp_score * RANK_WEIGHT_WIN_PROB +
            liq_score * RANK_WEIGHT_LIQUIDITY +
            iv_score * RANK_WEIGHT_IV_EDGE +
            em_score * RANK_WEIGHT_EM_DISTANCE +
            width_score * RANK_WEIGHT_WIDTH_EFF
        )

        profile = c.get("entry_profile", "conservative_itm")
        profile_mult = _profile_fit_multiplier(profile, aggression)
        composite *= profile_mult

        c["entry_fit_multiplier"] = round(profile_mult, 4)
        c["rank_score"] = round(composite, 4)
        c["rank_context"] = {
            "ev_after_slippage": round(ev_after_slippage, 4),
            "breakeven_prob_needed": round(breakeven_prob_needed, 4),
            "wp_minus_hurdle": round(wp - breakeven_prob_needed, 4),
            "entry_aggression": aggression,
            "entry_profile": profile,
        }

        if composite >= RANK_MIN_SCORE:
            scored.append(c)

    return sorted(scored, key=lambda c: c.get("rank_score", 0), reverse=True)



# ─────────────────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────────────────

def compute_position_size(
    debit: float,
    tier: str = "1",
) -> Tuple[int, float, str]:
    cost_per_contract = debit * 100

    if cost_per_contract <= 0:
        return 1, 0, "Could not compute sizing"

    max_from_dollars = int(MAX_RISK_PER_TRADE_USD / cost_per_contract)
    max_from_pct = int((ACCOUNT_SIZE * MAX_RISK_PCT_ACCOUNT) / cost_per_contract)

    contracts = max(1, min(max_from_dollars, max_from_pct, MAX_CONTRACTS))

    multiplier = TIER1_SIZE_MULTIPLIER if tier == "1" else TIER2_SIZE_MULTIPLIER
    contracts = max(1, int(contracts * multiplier))

    total_risk = contracts * cost_per_contract

    note = (f"{contracts} contract(s) × ${debit:.2f} = ${total_risk:.0f} risk "
            f"[max ${MAX_RISK_PER_TRADE_USD:.0f}, {MAX_RISK_PCT_ACCOUNT:.0%} acct]")

    return contracts, total_risk, note


# ─────────────────────────────────────────────────────────
# STOP LOSS LOGIC
# ─────────────────────────────────────────────────────────

def compute_stop_loss(
    ticker: str,
    debit: float,
) -> Tuple[Optional[float], str]:
    if USE_STOP_LOSS_ALL or ticker.upper() in HIGH_VOLUME_TICKERS:
        stop_price = round(debit * (1 - STOP_LOSS_PCT), 2)
        return stop_price, f"{STOP_LOSS_PCT:.0%} loss"
    else:
        return None, "No stop (low-vol ticker — manage manually)"


# ─────────────────────────────────────────────────────────
# CONFIDENCE SCORING (v3.6: direction-aware)
# ─────────────────────────────────────────────────────────

def compute_confidence(
    webhook_data: Dict,
    trade: Dict,
    has_earnings: bool = False,
    has_dividend: bool = False,
    vol_edge: Dict = None,
    em_data: Dict = None,
    regime: Dict = None,
    direction: str = "bull",
    v4_flow: Dict = None,
    vol_regime: Dict = None,
) -> Tuple[int, List[str]]:
    """
    Score trade confidence 0-100.
    v3.6: direction-aware — bear trades score differently from bull.
    v3.9: v4 flow quality signals from options_exposure engine.
    v4.5: canonical vol regime awareness (VVIX, term structure, RV spike,
          transition warning) via vol_regime from build_canonical_vol_regime.
    """
    reasons = []
    vol_edge = vol_edge or {}
    em_data = em_data or {}
    regime = regime or {}
    v4_flow = v4_flow or {}
    vol_regime = vol_regime or {}
    is_bear = direction == "bear"

    is_manual = (
        webhook_data.get("tier") in (None, "2", "0", "") and
        not webhook_data.get("htf_confirmed") and
        not webhook_data.get("htf_converging") and
        not webhook_data.get("rsi_mfi_bull") and
        not webhook_data.get("above_vwap") and
        as_float(webhook_data.get("wt2"), 0) == 0
    )

    if is_manual:
        score = 53
        reasons.append("Manual check (no signal)")

        ror = trade.get("ror", 0)
        if ror >= 0.50:
            score += 8
            reasons.append(f"Strong RoR ({ror:.2f})")
        elif ror >= 0.30:
            score += 5
            reasons.append(f"OK RoR ({ror:.2f})")

        ev_after = trade.get("ev_after_slippage", trade.get("expected_value", 0))
        if ev_after > 0.05:
            score += 4
            reasons.append(f"Positive EV after slippage (${ev_after:.2f})")
        elif ev_after > 0:
            score += 1
            reasons.append(f"Slightly positive EV (${ev_after:.2f})")
        else:
            score -= 10
            reasons.append(f"Negative EV after slippage (${ev_after:.2f})")

        wp = trade.get("win_prob", 0.5)
        if wp >= 0.60:
            score += 4
            reasons.append(f"Good profit probability ({wp:.0%})")
        elif wp < 0.50:
            score -= 6
            reasons.append(f"Low profit probability ({wp:.0%})")

        cost_pct = trade.get("cost_pct", 100)
        if cost_pct <= 55:
            score += 8
            reasons.append(f"Great pricing ({cost_pct:.0f}%)")
        elif cost_pct <= 65:
            score += 4
            reasons.append(f"Good pricing ({cost_pct:.0f}%)")

        warn_count = len(trade.get("warnings", []))
        if warn_count:
            score -= warn_count * 4
            reasons.append(f"{warn_count} liquidity warning(s)")

    else:
        score = 30

        tier = webhook_data.get("tier", "2")
        if tier == "1":
            score += CONFIDENCE_BOOSTS["tier_1"]
            reasons.append(f"T1 signal (+{CONFIDENCE_BOOSTS['tier_1']})")
        else:
            score += CONFIDENCE_BOOSTS["tier_2"]
            reasons.append(f"T2 signal (+{CONFIDENCE_BOOSTS['tier_2']})")

        if webhook_data.get("htf_confirmed"):
            score += CONFIDENCE_BOOSTS["htf_confirmed"]
            reasons.append("HTF trend confirmed")
        elif webhook_data.get("htf_converging"):
            score += CONFIDENCE_BOOSTS["htf_converging"]
            reasons.append("HTF trend converging")
        else:
            score += CONFIDENCE_PENALTIES["htf_diverging"]
            reasons.append("HTF trend diverging")

        daily_bull = webhook_data.get("daily_bull", False)
        if is_bear:
            # Bear trade: daily bearish is a boost, daily bullish is a penalty
            if not daily_bull:
                score += CONFIDENCE_BOOSTS.get("daily_bear", 10)
                reasons.append("Daily trend bearish (confirms bear)")
            else:
                score += CONFIDENCE_PENALTIES.get("daily_bull", -10)
                reasons.append("Daily trend bullish (fights bear)")
        else:
            # Bull trade: daily bullish is a boost, daily bearish is a penalty
            if daily_bull:
                score += CONFIDENCE_BOOSTS["daily_bull"]
                reasons.append("Daily trend bullish")
            else:
                score += CONFIDENCE_PENALTIES.get("daily_bear", -10)
                reasons.append("Daily trend bearish")

        rsi_mfi_bull = webhook_data.get("rsi_mfi_bull", False)
        if is_bear:
            if not rsi_mfi_bull:
                score += CONFIDENCE_BOOSTS.get("rsi_mfi_bear", 5)
                reasons.append("RSI+MFI selling (confirms bear)")
        else:
            if rsi_mfi_bull:
                score += CONFIDENCE_BOOSTS["rsi_mfi_bull"]
                reasons.append("RSI+MFI buying")

        above_vwap = webhook_data.get("above_vwap", False)
        if is_bear:
            if not above_vwap:
                score += CONFIDENCE_BOOSTS.get("below_vwap", 5)
                reasons.append("Below VWAP (confirms bear)")
        else:
            if above_vwap:
                score += CONFIDENCE_BOOSTS["above_vwap"]
                reasons.append("Above VWAP")

        wt2 = as_float(webhook_data.get("wt2"), 0)
        if is_bear:
            if wt2 > 60:
                score += CONFIDENCE_BOOSTS.get("wave_overbought", 10)
                reasons.append("Wave overbought (confirms bear)")
            elif wt2 < -30:
                score += CONFIDENCE_PENALTIES.get("wave_oversold", -15)
                reasons.append("Wave oversold (fights bear)")
        else:
            if wt2 < -30:
                score += CONFIDENCE_BOOSTS["wave_oversold"]
                reasons.append("Wave oversold")
            elif wt2 > 60:
                score += CONFIDENCE_PENALTIES["wave_overbought"]
                reasons.append("Wave overbought")

        ror = trade.get("ror", 0)
        if ror >= 0.50:
            score += 5
            reasons.append(f"Strong RoR ({ror:.2f})")

        ev_after = trade.get("ev_after_slippage", trade.get("expected_value", 0))
        if ev_after > 0.05:
            score += 4
            reasons.append(f"Positive EV after slippage (${ev_after:.2f})")
        elif ev_after <= 0:
            score -= 8
            reasons.append(f"Negative EV after slippage (${ev_after:.2f})")

        wp = trade.get("win_prob", 0.5)
        if wp >= 0.60:
            score += 4
            reasons.append(f"Good profit probability ({wp:.0%})")
        elif wp < 0.50:
            score -= 5
            reasons.append(f"Low profit probability ({wp:.0%})")

        warn_count = len(trade.get("warnings", []))
        if warn_count:
            score += warn_count * CONFIDENCE_PENALTIES.get("low_oi", -5)
            reasons.append(f"{warn_count} liquidity warning(s)")

    # ── IV / RV Edge (both directions) ──
    edge_label = vol_edge.get("edge_label", "UNKNOWN")
    if edge_label == "BUYER":
        score += CONFIDENCE_BOOSTS.get("rv_edge", 10)
        reasons.append(f"Vol edge: BUYER ({vol_edge.get('description', '')})")
    elif edge_label == "SELLER":
        score += CONFIDENCE_PENALTIES.get("iv_crushed", -5)
        reasons.append(f"Vol edge: SELLER ({vol_edge.get('description', '')})")
    elif edge_label == "NEUTRAL" and vol_edge.get("iv", 0) > 0:
        reasons.append(f"Vol: neutral ({vol_edge.get('iv_pct', 0):.0f}% IV / {vol_edge.get('rv_pct', 0):.0f}% RV)")

    # ── Expected Move ──
    em_amount = em_data.get("expected_move", 0)
    if em_amount > 0 and trade.get("short"):
        spot = em_data.get("spot", 0)
        short_strike = trade.get("short", 0)
        if is_bear:
            distance_to_short = abs(short_strike - spot) if spot > 0 else 0
        else:
            distance_to_short = abs(spot - short_strike) if spot > 0 else 0

        if distance_to_short <= em_amount:
            score += CONFIDENCE_BOOSTS.get("within_em", 5)
            reasons.append("Short strike within EM")
        else:
            score += CONFIDENCE_PENALTIES.get("beyond_em", -8)
            reasons.append("Short strike beyond EM")

    # ── Market Regime ──
    adx_regime   = regime.get("adx_regime", "")
    vix_regime   = regime.get("vix_regime", "")
    regime_label = regime.get("label", "")
    vix_val      = regime.get("vix", 0)

    if adx_regime == "TRENDING" and vix_regime in ("LOW", "NORMAL"):
        score += CONFIDENCE_BOOSTS.get("regime_trending", 8)
        reasons.append(f"Regime: {regime_label} (trending)")
    elif vix_regime == "LOW":
        score += CONFIDENCE_BOOSTS.get("regime_low_vix", 5)
        reasons.append(f"Regime: low VIX ({vix_val:.0f})")

    if adx_regime == "CHOPPY":
        score += CONFIDENCE_PENALTIES.get("regime_choppy", -10)
        reasons.append(f"Regime: choppy (ADX {regime.get('adx', 0):.0f})")

    if vix_regime == "CRISIS":
        if is_bear:
            # Bear spreads in CRISIS: market fear confirms direction
            # IV inflation compresses net debit — actually favorable for spread buyers
            score += 8
            reasons.append(f"CRISIS VIX {vix_val:.0f} — confirms bear direction")
        else:
            # Bull spreads fighting extreme fear — meaningful penalty
            score += CONFIDENCE_PENALTIES.get("regime_crisis", -10)
            reasons.append(f"Regime: CRISIS VIX {vix_val:.0f} (fighting fear)")
    elif vix_regime == "ELEVATED":
        if is_bear:
            # Elevated VIX slightly favors bears
            score += 3
            reasons.append(f"Elevated VIX {vix_val:.0f} (mild bear confirm)")
        else:
            score += CONFIDENCE_PENALTIES.get("regime_high_vix", -5)
            reasons.append(f"Regime: elevated VIX {vix_val:.0f}")

    # ── v4 Institutional Flow Quality (v3.9) ──
    # These signals come from the v4 options_exposure engine when available.
    # They measure dealer positioning agreement with the trade direction.
    if v4_flow:
        # Data quality gate: LOW quality data = penalize
        v4_conf_label = v4_flow.get("confidence_label", "")
        if v4_conf_label == "LOW":
            score -= 15
            reasons.append("v4 data quality LOW (-15)")
        elif v4_conf_label == "HIGH":
            score += 5
            reasons.append("v4 data quality HIGH (+5)")

        # GEX regime agreement
        gex_val = v4_flow.get("gex", 0)
        gex_positive = gex_val >= 0
        if gex_positive:
            # Positive GEX = suppressive environment, fights directional trades
            score -= 8
            reasons.append(f"GEX positive (suppressing, -8)")
        else:
            # Negative GEX = trending environment, favors directional trades
            score += 8
            reasons.append(f"GEX negative (trending, +8)")

        # Dealer flow direction agreement
        v4_bias = v4_flow.get("bias", "NEUTRAL")  # UPSIDE / DOWNSIDE / NEUTRAL
        if is_bear:
            if v4_bias == "DOWNSIDE":
                score += 10
                reasons.append("v4 dealer flow confirms DOWNSIDE (+10)")
            elif v4_bias == "UPSIDE":
                score -= 10
                reasons.append("v4 dealer flow shows UPSIDE (fights bear, -10)")
        else:
            if v4_bias == "UPSIDE":
                score += 10
                reasons.append("v4 dealer flow confirms UPSIDE (+10)")
            elif v4_bias == "DOWNSIDE":
                score -= 10
                reasons.append("v4 dealer flow shows DOWNSIDE (fights bull, -10)")

        # Flip price alignment
        flip = v4_flow.get("gamma_flip")
        if flip and v4_flow.get("spot"):
            spot_v4 = v4_flow["spot"]
            if is_bear:
                if spot_v4 < flip:
                    score += 5
                    reasons.append(f"Spot below gamma flip ${flip:.0f} (confirms bear, +5)")
                else:
                    score -= 5
                    reasons.append(f"Spot above gamma flip ${flip:.0f} (fights bear, -5)")
            else:
                if spot_v4 > flip:
                    score += 5
                    reasons.append(f"Spot above gamma flip ${flip:.0f} (confirms bull, +5)")
                else:
                    score -= 5
                    reasons.append(f"Spot below gamma flip ${flip:.0f} (fights bull, -5)")

        # Composite regime agreement
        v4_regime = v4_flow.get("composite_regime", "")
        if "PIN" in v4_regime:
            # Pinning regime fights directional trades
            score -= 8
            reasons.append(f"v4 regime: {v4_regime} (fights directional, -8)")
        elif "TREND" in v4_regime or "EXPLOSIVE" in v4_regime:
            score += 5
            reasons.append(f"v4 regime: {v4_regime} (confirms directional, +5)")

    # ── Canonical Vol Regime (v4.5) ──
    # Uses the full 6-input caution model rather than raw VIX alone.
    # Supplements (does not replace) the basic VIX regime block above.
    if vol_regime:
        vol_label     = vol_regime.get("label", "")
        caution_score = int(vol_regime.get("caution_score") or 0)
        transition    = bool(vol_regime.get("transition_warning"))
        vvix_warn     = bool(vol_regime.get("vvix_warning"))
        rv_spike      = bool(vol_regime.get("rv_spike"))
        above_ma200   = bool(vol_regime.get("above_ma200"))
        term          = (vol_regime.get("term_structure") or "").lower()
        vvix_val      = vol_regime.get("vvix") or 0

        # Incremental penalty for each caution point above the neutral threshold.
        # caution 0-2 = no penalty (covered by basic VIX block already).
        # caution 3 = -3, 4 = -6, 5 = -9, 6+ = -12 (caps at 4 increments).
        caution_excess = min(max(caution_score - 2, 0), 4)
        if caution_excess > 0:
            pen = caution_excess * 3
            score -= pen
            reasons.append(
                f"Vol caution score {caution_score}/8 (−{pen})"
            )

        # TRANSITION warning: vol is transitioning upward — reduce confidence
        # more for bull trades (going against rising fear) than bear.
        if transition and vol_label not in ("ELEVATED", "CRISIS"):
            trans_pen = 4 if is_bear else 7
            score -= trans_pen
            reasons.append(
                f"Vol transition warning (−{trans_pen})"
            )

        # Inverted term structure: near-term fear exceeds 30-day — urgent signal.
        if term == "inverted":
            if is_bear:
                score += 3
                reasons.append("Inverted VIX term (confirms fear, bear +3)")
            else:
                score -= 6
                reasons.append("Inverted VIX term (rising fear, bull −6)")
        elif term == "flat" and caution_score >= 3:
            score -= 2
            reasons.append("Flat VIX term structure (−2)")

        # VVIX spike: vol-of-vol elevated → dealer hedging unstable
        if vvix_warn:
            pen = 4 if not is_bear else 2
            score -= pen
            reasons.append(
                f"VVIX {vvix_val:.0f} elevated — hedging unstable (−{pen})"
            )

        # Short-term RV spike: realized vol accelerating vs medium-term
        if rv_spike:
            if is_bear:
                score += 3
                reasons.append("RV spike confirms bearish momentum (+3)")
            else:
                score -= 4
                reasons.append("RV spike fighting bull entry (−4)")

        # VIX above 200-day MA: sustained elevated vol regime
        if above_ma200 and vol_label in ("NORMAL", "LOW"):
            score -= 3
            reasons.append("VIX above 200d MA despite low base (−3)")

    # ── Deal-breakers ──
    if has_earnings:
        score += CONFIDENCE_PENALTIES["earnings_week"]
        reasons.append("EARNINGS WEEK — BLOCKED")

    if has_dividend:
        score += CONFIDENCE_PENALTIES["dividend_in_dte"]
        reasons.append("DIVIDEND IN DTE — BLOCKED")

    if is_manual:
        score = min(score, 84)
    else:
        score = min(score, 96)

    score = max(0, min(100, score))
    return score, reasons


# ─────────────────────────────────────────────────────────
# PUBLIC API — MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────

def recommend_trade(
    ticker: str,
    spot: float,
    contracts: List[Dict],
    dte: int,
    expiration: str,
    webhook_data: Dict = None,
    has_earnings: bool = False,
    has_dividend: bool = False,
    candle_closes: List[float] = None,
    regime: Dict = None,
    v4_flow: Dict = None,
    vol_regime: Dict = None,
) -> Dict[str, Any]:
    """
    Main entry point. Returns a complete trade recommendation or rejection.
    Phase 2 adds a hybrid entry framework:
      - conservative_itm: both legs ITM
      - balanced_transition: long ITM, short near/through spot
      - early_atm: long ATM, short modest OTM
    """
    webhook_data = webhook_data or {}
    candle_closes = candle_closes or []
    result = {"ok": False, "ticker": ticker, "spot": spot, "dte": dte, "exp": expiration}

    bias = webhook_data.get("bias", "bull")
    if bias not in ALLOWED_DIRECTIONS:
        result["reason"] = f"Direction '{bias}' not in allowed directions"
        return result

    if dte < MIN_DTE or dte > MAX_DTE:
        result["reason"] = f"DTE {dte} outside {MIN_DTE}-{MAX_DTE} range"
        return result

    if NO_EARNINGS_WEEK and has_earnings:
        result["reason"] = "Earnings this week — trade blocked"
        result["deal_breaker"] = "earnings"
        return result

    if NO_DIVIDEND_IN_DTE and has_dividend:
        result["reason"] = "Dividend ex-date within DTE — trade blocked"
        result["deal_breaker"] = "dividend"
        return result

    avg_iv = get_avg_chain_iv(contracts, spot, side="call" if bias == "bull" else "put")
    expected_move = calc_expected_move(spot, avg_iv, dte) if avg_iv > 0 else 0.0
    rv = calc_realized_vol(candle_closes) if candle_closes else 0.0
    vol_edge = calc_iv_rv_edge(avg_iv, rv) if avg_iv > 0 and rv > 0 else {}

    em_data = {
        "expected_move": expected_move,
        "spot": spot,
        "iv": avg_iv,
        "rv": rv,
        "dte": dte,
    }

    regime = regime or {}
    entry_plan = _determine_entry_plan(webhook_data, bias, regime=regime, v4_flow=v4_flow, vol_edge=vol_edge, dte=dte)
    atm_band = _atm_band(spot)

    if bias == "bear":
        quotes = build_put_quotes(contracts, spot, ticker=ticker, include_otm=HYBRID_ENTRY_ENABLED)
        if len(quotes) < 2:
            result["reason"] = f"Not enough liquid put strikes after hybrid filter (need 2, found {len(quotes)})"
            return result

        available_widths = detect_available_widths(sorted(quotes.keys()), spot)
        if not available_widths:
            result["reason"] = "No valid widths available from put strike increments"
            return result

        candidates = build_bear_put_spreads(quotes, spot, available_widths, expected_move, dte=dte, atm_band=atm_band)
        spread_side = "put"
        spread_label = "BEAR PUT"
    else:
        quotes = build_call_quotes(contracts, spot, ticker=ticker, include_otm=HYBRID_ENTRY_ENABLED)
        if len(quotes) < 2:
            result["reason"] = f"Not enough liquid call strikes after hybrid filter (need 2, found {len(quotes)})"
            return result

        available_widths = detect_available_widths(list(quotes.keys()), spot)
        if not available_widths:
            result["reason"] = "No valid widths available from call strike increments"
            return result

        candidates = build_bull_call_spreads(quotes, spot, available_widths, expected_move, dte=dte, atm_band=atm_band)
        spread_side = "call"
        spread_label = "BULL CALL"

    if not candidates:
        result["reason"] = (
            f"No valid hybrid debit spreads found "
            f"(widths tried: {available_widths}, liquid strikes: {len(quotes)}, entry mode: {entry_plan.get('aggression')})"
        )
        result["entry_plan"] = entry_plan
        return result

    ranked = rank_candidates(
        candidates,
        iv_edge_label=vol_edge.get("edge_label", "NEUTRAL") if vol_edge else "NEUTRAL",
        entry_plan=entry_plan,
    )
    if not ranked:
        rejected = {}
        for c in candidates:
            reason = c.get("_rejected", "filtered")
            rejected[reason] = rejected.get(reason, 0) + 1
        rej_txt = ", ".join(f"{k}:{v}" for k, v in sorted(rejected.items())) if rejected else "all filtered"
        profiles = {}
        for c in candidates:
            p = c.get("entry_profile", "unknown")
            profiles[p] = profiles.get(p, 0) + 1
        profile_txt = ", ".join(f"{k}:{v}" for k, v in sorted(profiles.items()))
        result["reason"] = (
            f"No ranked {spread_label} spreads passed quality filters "
            f"(widths tried: {available_widths}; profiles: {profile_txt}; rejects: {rej_txt})"
        )
        result["deal_breaker"] = "ranking_filters"
        result["entry_plan"] = entry_plan
        return result
    best = ranked[0]

    ladder = []
    seen_widths = set()
    for c in ranked:
        w = (c["width"], c.get("entry_profile"))
        if w not in seen_widths:
            seen_widths.add(w)
            ladder.append(c)

    tier = webhook_data.get("tier", "2")
    num_contracts, total_risk, sizing_note = compute_position_size(best["debit"], tier)

    stop_price, stop_note = compute_stop_loss(ticker, best["debit"])

    confidence, conf_reasons = compute_confidence(
        webhook_data, best, has_earnings, has_dividend,
        vol_edge=vol_edge,
        em_data=em_data,
        regime=regime,
        direction=bias,
        v4_flow=v4_flow,
        vol_regime=vol_regime,
    )

    if confidence < MIN_CONFIDENCE_TO_TRADE:
        result["reason"] = f"Confidence {confidence}/100 below {MIN_CONFIDENCE_TO_TRADE} threshold"
        result["confidence"] = confidence
        result["conf_reasons"] = conf_reasons
        result["entry_plan"] = entry_plan
        return result

    win_prob = best.get("win_prob", 0)
    min_win_prob_required = _dynamic_min_win_probability(best.get("entry_profile", "conservative_itm"), confidence)
    if win_prob < min_win_prob_required:
        result["reason"] = (
            f"Profit probability {win_prob:.0%} below {min_win_prob_required:.0%} minimum "
            f"for {best.get('profile_label', 'this entry mode')}"
        )
        result["confidence"] = confidence
        result["conf_reasons"] = conf_reasons
        result["entry_plan"] = entry_plan
        return result

    profile_size_mult = _entry_size_multiplier(best.get("entry_profile", "conservative_itm"))
    regime_size_mult = regime.get("size_mult", 1.0)
    combined_size_mult = profile_size_mult * regime_size_mult

    sizing_tags = []
    if profile_size_mult < 1.0:
        sizing_tags.append(f"entry ×{profile_size_mult:.2f}")
    if regime_size_mult < 1.0 and regime_size_mult > 0:
        sizing_tags.append(f"regime ×{regime_size_mult:.2f}")

    if combined_size_mult < 1.0 and combined_size_mult > 0:
        num_contracts = max(1, int(num_contracts * combined_size_mult))
        total_risk = num_contracts * best["debit"] * 100

    regime_note = f" | {'; '.join(sizing_tags)}" if sizing_tags else ""
    sizing_note = (
        f"{num_contracts} contract(s) × ${best['debit']:.2f} = ${total_risk:.0f} risk "
        f"[max ${MAX_RISK_PER_TRADE_USD:.0f}, {MAX_RISK_PCT_ACCOUNT:.0%} acct]{regime_note}"
    )

    exits = {
        "same_day": {
            "target_pct": f"{SAME_DAY_EXIT_PCT:.0%}",
            "sell_at": best["same_day_exit"],
            "profit_per_contract": round((best["same_day_exit"] - best["debit"]) * 100, 2),
            "profit_total": round((best["same_day_exit"] - best["debit"]) * 100 * num_contracts, 2),
        },
        "next_day": {
            "target_pct": f"{NEXT_DAY_EXIT_PCT:.0%}",
            "sell_at": best["next_day_exit"],
            "profit_per_contract": round((best["next_day_exit"] - best["debit"]) * 100, 2),
            "profit_total": round((best["next_day_exit"] - best["debit"]) * 100 * num_contracts, 2),
        },
        "extended": {
            "target_pct": f"{EXTENDED_HOLD_EXIT_PCT:.0%}",
            "sell_at": best["extended_exit"],
            "profit_per_contract": round((best["extended_exit"] - best["debit"]) * 100, 2),
            "profit_total": round((best["extended_exit"] - best["debit"]) * 100 * num_contracts, 2),
        },
    }

    return {
        "ok": True,
        "ticker": ticker,
        "spot": spot,
        "dte": dte,
        "exp": expiration,
        "direction": bias,
        "spread_type": "debit",
        "side": spread_side,
        "spread_label": spread_label,
        "trade": best,
        "ladder": ladder,
        "candidate_count": len(candidates),
        "contracts": num_contracts,
        "total_risk": total_risk,
        "sizing_note": sizing_note,
        "stop_price": stop_price,
        "stop_note": stop_note,
        "exits": exits,
        "confidence": confidence,
        "conf_reasons": conf_reasons,
        "tier": tier,
        "webhook_data": webhook_data,
        "expected_move": expected_move,
        "vol_edge": vol_edge,
        "em_data": em_data,
        "regime": regime,
        "entry_plan": entry_plan,
        "min_win_prob_required": min_win_prob_required,
        # v5.0: trailing stop fields
        "trailing_stop": {
            "enabled": TRAILING_STOP_ENABLED,
            "activation_pct": TRAILING_STOP_ACTIVATION_PCT,
            "trail_distance_pct": TRAILING_STOP_DISTANCE_PCT,
            "min_distance_pct": TRAILING_STOP_MIN_DISTANCE,
            "activation_price": round(best["debit"] * (1 + TRAILING_STOP_ACTIVATION_PCT), 2),
        } if TRAILING_STOP_ENABLED else {"enabled": False},
        # v5.0: dynamic 0DTE exit targets
        "dynamic_exit": {
            "enabled": True,
            "base_target_pct": SAME_DAY_EXIT_PCT,
            "early_target_pct": round(SAME_DAY_EXIT_PCT * DYNAMIC_EXIT_EARLY_MULT, 3),
            "mid_target_pct": round(SAME_DAY_EXIT_PCT * DYNAMIC_EXIT_MID_MULT, 3),
            "late_target_pct": round(SAME_DAY_EXIT_PCT * DYNAMIC_EXIT_LATE_MULT, 3),
            "power_hour_target_pct": round(SAME_DAY_EXIT_PCT * DYNAMIC_EXIT_POWER_HOUR_MULT, 3),
        } if DYNAMIC_EXIT_0DTE_ENABLED and dte == 0 else {"enabled": False},
        # v5.0: IV/RV ratio for downstream confidence
        "iv_rv_ratio": round(avg_iv / rv, 3) if rv > 0 and avg_iv > 0 else None,
        "avg_iv": avg_iv,
        "hv20": rv,
    }



# ─────────────────────────────────────────────────────────
# TRADE CARD FORMATTER
# ─────────────────────────────────────────────────────────

def format_trade_card(rec: Dict) -> str:
    if not rec.get("ok"):
        reason = rec.get("reason", "Unknown")
        conf   = rec.get("confidence")
        lines  = [f"❌ {rec.get('ticker', '?')} — NO TRADE", f"Reason: {reason}"]
        if conf is not None:
            lines.append(f"Confidence: {conf}/100")
        lines.append("— Not financial advice —")
        return "\n".join(lines)

    trade        = rec["trade"]
    exits        = rec["exits"]
    ticker       = rec["ticker"]
    tier         = rec.get("tier", "?")
    conf         = rec.get("confidence", 0)
    direction    = rec.get("direction", "bull")
    dir_word     = "Bullish" if direction == "bull" else "Bearish"
    opt_type     = "Call" if direction == "bull" else "Put"
    tier_emoji   = "🥇" if str(tier) == "1" else "🥈"

    risk_per = round(trade['debit'] * 100, 2)
    max_profit_per = round(trade['max_profit'] * 100, 2)
    contract_line = f"BUY {trade['long']}/{trade['short']} {opt_type} Debit Spread"
    em = rec.get("expected_move", 0)
    entry_label = trade.get("profile_label", HYBRID_PROFILE_LABEL.get(trade.get("entry_profile"), "Conservative ITM"))

    why = []
    if trade.get("em_zone") == "inside":
        why.append("short strike is still inside the expected move")
    if trade.get("width", 0) <= 1.0:
        why.append("it uses the tightest width available")
    elif trade.get("width", 0) <= 2.5:
        why.append("it keeps width relatively conservative")
    if trade.get("entry_profile") == "early_atm":
        why.append("it uses the earlier ATM profile to cut cost when conviction is stronger")
    elif trade.get("entry_profile") == "balanced_transition":
        why.append("it balances room for error with a lower debit than full ITM")
    elif trade.get("long_itm", 0) > 0:
        why.append("the long leg starts ITM to give the trade more room")
    if rec.get("vol_edge", {}).get("edge_label") == "BUYER":
        why.append("IV vs RV is favorable for a debit buyer")
    if not why:
        why.append("it is a defined-risk way to express the directional view")

    risk_notes = []
    if trade.get("expected_value", 0) <= 0:
        risk_notes.append("edge is thin at this fill")
    risk_notes.extend(trade.get("warnings", [])[:2])
    if not risk_notes:
        risk_notes.append("avoid chasing a poor fill")

    display_debit = trade.get("display_debit", trade.get("debit"))
    lines = [
        f"{tier_emoji} {ticker} — {dir_word} Trade",
        f"🎯 Contract: {contract_line}",
        f"📅 Exp: {rec['exp']} | DTE: {rec['dte']} | 💪 Confidence: {conf}/100 | Entry: {entry_label}",
        f"💵 Cost: ~${display_debit:.2f} est. fill (${risk_per:.0f} max risk) | Max Profit: ${trade['max_profit']:.2f} (${max_profit_per:.0f})",
    ]

    if EM_DISPLAY_ON_CARD and em > 0:
        em_low  = round(rec["spot"] - em, 2)
        em_high = round(rec["spot"] + em, 2)
        lines.append(f"📐 Expected Move: ±${em:.2f} (${em_low:.2f} – ${em_high:.2f})")

    lines += [
        "",
        "🧠 Why this trade:",
        f"  • {'; '.join(why[:3])}.",
        "",
        "📋 Plan:",
        f"  • Work the entry near a fair fill around ~${display_debit:.2f}.",
        f"  • First target: ${exits['same_day']['sell_at']:.2f} (+30%).",
        f"  • Next target: ${exits['next_day']['sell_at']:.2f} (+35%).",
        f"  • Extended target: ${exits['extended']['sell_at']:.2f} (+50%).",
    ]
    if rec.get("stop_price"):
        lines.append(f"  • Stop: ${rec['stop_price']:.2f} ({rec['stop_note']}).")
    else:
        lines.append(f"  • Stop: {rec['stop_note']}.")

    lines += [
        "",
        f"⚠️ Main risk: {'; '.join(risk_notes[:2])}.",
        "",
        "📦 Data:",
        f"  Width ${trade['width']:.2f} | Long {trade['long']} / Short {trade['short']}",
        f"  Buckets: long {trade.get('long_bucket', '?')} | short {trade.get('short_bucket', '?')} | ITM long ${trade['long_itm']:.2f} / short ${trade['short_itm']:.2f}",
        f"  RoR {trade['ror']:.0%} | Win Prob {trade.get('win_prob', 0):.0%} | EV ${trade.get('expected_value', 0):.2f}/contract",
    ]

    if trade.get("long_bid") is not None and trade.get("long_ask") is not None and trade.get("short_bid") is not None and trade.get("short_ask") is not None:
        mid_txt = trade.get("debit", 0)
        nat_txt = trade.get("natural_debit")
        if nat_txt is not None:
            lines.append(
                f"  Chain: long {trade['long_bid']:.2f}/{trade['long_ask']:.2f} | short {trade['short_bid']:.2f}/{trade['short_ask']:.2f} | mid ${mid_txt:.2f} / natural ${nat_txt:.2f}"
            )
        else:
            lines.append(
                f"  Chain: long {trade['long_bid']:.2f}/{trade['long_ask']:.2f} | short {trade['short_bid']:.2f}/{trade['short_ask']:.2f} | mid ${mid_txt:.2f}"
            )

    vol_edge = rec.get("vol_edge", {})
    if IV_RV_DISPLAY_ON_CARD and vol_edge.get("edge_label") and vol_edge["edge_label"] != "UNKNOWN":
        lines.append(
            f"  Vol: {vol_edge['edge_label']} ({vol_edge.get('iv_pct', 0):.0f}% IV vs {vol_edge.get('rv_pct', 0):.0f}% RV | {vol_edge.get('edge_pct', 0):+.1f}pp)"
        )

    regime = rec.get("regime", {})
    if regime.get("label"):
        lines.append(f"  Regime: {regime.get('label')} (VIX {regime.get('vix', 0):.0f} | ADX {regime.get('adx', 0):.0f})")

    if trade.get("net_theta") is not None:
        parts = []
        if trade.get("net_delta") is not None: parts.append(f"Δ {trade['net_delta']:.3f}")
        if trade.get("net_gamma") is not None: parts.append(f"Γ {trade['net_gamma']:.4f}")
        parts.append(f"Θ ${trade['net_theta']:.3f}/day")
        if trade.get("net_vega") is not None: parts.append(f"V ${trade['net_vega']:.3f}/pt")
        lines.append("  " + " | ".join(parts))

    if rec.get("conf_reasons"):
        lines.append("💭 Why confidence: " + " | ".join(rec["conf_reasons"][:3]))

    lines += ["", f"Size: {rec['sizing_note']}", "", "— Not financial advice —"]
    return "\n".join(lines)



# ═══════════════════════════════════════════════════════════
# JOURNAL FEEDBACK LOOP (v4.1)
# Queries trade journal for ticker win rate and adjusts sizing.
# ═══════════════════════════════════════════════════════════

def get_journal_adjustment(ticker: str, store_get_fn=None) -> dict:
    """
    Check trade journal stats for this ticker.
    Returns dict with:
      - allowed: bool (False = suppress this ticker entirely)
      - size_mult: float (1.0 = normal, 0.5 = reduced)
      - reason: str (explanation)
    """
    if not JOURNAL_FEEDBACK_ENABLED or store_get_fn is None:
        return {"allowed": True, "size_mult": 1.0, "reason": ""}

    try:
        import json
        # Try to load journal stats from store
        key = f"journal_stats:{ticker.upper()}"
        raw = store_get_fn(key)
        if raw is None:
            return {"allowed": True, "size_mult": 1.0, "reason": ""}

        stats = json.loads(raw)
        total = stats.get("total", 0)
        wins = stats.get("wins", 0)

        if total < JOURNAL_MIN_TRADES_FOR_STATS:
            return {"allowed": True, "size_mult": 1.0,
                    "reason": f"Insufficient data ({total}/{JOURNAL_MIN_TRADES_FOR_STATS} trades)"}

        win_rate = wins / total if total > 0 else 0

        if win_rate < JOURNAL_SUPPRESS_WIN_RATE:
            return {"allowed": False, "size_mult": 0.0,
                    "reason": f"SUPPRESSED: {ticker} win rate {win_rate:.0%} < {JOURNAL_SUPPRESS_WIN_RATE:.0%} over {total} trades"}

        if win_rate < JOURNAL_REDUCE_WIN_RATE:
            return {"allowed": True, "size_mult": JOURNAL_REDUCE_SIZE_MULT,
                    "reason": f"Size reduced: {ticker} win rate {win_rate:.0%} < {JOURNAL_REDUCE_WIN_RATE:.0%} over {total} trades"}

        return {"allowed": True, "size_mult": 1.0,
                "reason": f"{ticker} win rate {win_rate:.0%} over {total} trades"}

    except Exception:
        return {"allowed": True, "size_mult": 1.0, "reason": ""}
