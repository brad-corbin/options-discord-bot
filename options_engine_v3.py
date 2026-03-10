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


def get_avg_chain_iv(contracts: List[Dict], spot: float) -> float:
    """Average IV from ATM options (within 3% of spot)."""
    atm_range = spot * 0.03
    ivs = []

    for c in contracts:
        strike = as_float(c.get("strike"), 0)
        iv = as_float(c.get("iv"), 0)
        if iv > 0 and abs(strike - spot) <= atm_range:
            ivs.append(iv)

    if not ivs:
        for c in contracts:
            iv = as_float(c.get("iv"), 0)
            if iv > 0:
                ivs.append(iv)

    return sum(ivs) / len(ivs) if ivs else 0.0


# ─────────────────────────────────────────────────────────
# CHAIN DATA BUILDERS
# ─────────────────────────────────────────────────────────

def build_call_quotes(contracts: List[Dict], spot: float) -> Dict[float, Dict]:
    """ITM calls: strikes BELOW spot."""
    quotes = {}
    for c in contracts:
        right = (c.get("right") or "").lower()
        if right != "call":
            continue

        strike = as_float(c.get("strike"), None)
        if strike is None or strike >= spot:
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

        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        if mid is None or mid <= 0:
            continue

        warnings = []
        if oi is not None and oi < MIN_OPEN_INTEREST:
            warnings.append(f"Low OI ({oi})")
        if bid is not None and ask is not None and (ask - bid) > MAX_BID_ASK_SPREAD:
            warnings.append(f"Wide B/A (${ask - bid:.2f})")

        quotes[strike] = {
            "strike": strike,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "oi": oi,
            "volume": vol,
            "delta": delta,
            "iv": iv,
            "theta": theta,
            "vega": vega,
            "itm_amount": round(spot - strike, 2),
            "warnings": warnings,
        }

    return quotes


def build_put_quotes(contracts: List[Dict], spot: float) -> Dict[float, Dict]:
    """ITM puts: strikes ABOVE spot."""
    quotes = {}
    for c in contracts:
        right = (c.get("right") or "").lower()
        if right != "put":
            continue

        strike = as_float(c.get("strike"), None)
        if strike is None or strike <= spot:
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

        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        if mid is None or mid <= 0:
            continue

        warnings = []
        if oi is not None and oi < MIN_OPEN_INTEREST:
            warnings.append(f"Low OI ({oi})")
        if bid is not None and ask is not None and (ask - bid) > MAX_BID_ASK_SPREAD:
            warnings.append(f"Wide B/A (${ask - bid:.2f})")

        quotes[strike] = {
            "strike": strike,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "oi": oi,
            "volume": vol,
            "delta": delta,
            "iv": iv,
            "theta": theta,
            "vega": vega,
            "itm_amount": round(strike - spot, 2),
            "warnings": warnings,
        }

    return quotes


# ─────────────────────────────────────────────────────────
# SPREAD CANDIDATE BUILDERS
# ─────────────────────────────────────────────────────────

def build_itm_debit_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
    expected_move: float = 0,
) -> List[Dict]:
    """
    Bull call debit spread: long lower strike call, short higher strike call.
    Both ITM (below spot).
    """
    candidates = []
    itm_strikes = sorted([k for k in quotes.keys()], reverse=True)

    if len(itm_strikes) < 2:
        return candidates

    em_lower = (spot - expected_move) if expected_move > 0 else 0

    for long_k in itm_strikes:
        long_q = quotes[long_k]

        for width in available_widths:
            short_k = round(long_k + width, 2)

            if short_k >= spot:
                continue

            short_q = quotes.get(short_k)
            if short_q is None:
                continue

            debit = round(long_q["mid"] - short_q["mid"], 4)
            if debit <= 0:
                continue

            cost_pct = debit / width
            if cost_pct > MAX_COST_PCT_OF_WIDTH:
                continue
            if cost_pct < MIN_COST_PCT_OF_WIDTH:
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
            win_prob = round(min(short_delta_abs, 0.99), 4) if short_delta_abs > 0 else 0.5
            ev = round((win_prob * max_profit) - ((1 - win_prob) * max_loss), 4)

            em_proximity = None
            em_zone = "unknown"
            if em_lower > 0:
                em_proximity = round(short_k - em_lower, 2)
                em_zone = "inside" if short_k >= em_lower else "outside"

            same_day_target = round(debit * (1 + SAME_DAY_EXIT_PCT), 2)
            next_day_target = round(debit * (1 + NEXT_DAY_EXIT_PCT), 2)
            extended_target = round(debit * (1 + EXTENDED_HOLD_EXIT_PCT), 2)

            candidates.append({
                "long":           long_k,
                "short":          short_k,
                "width":          width,
                "debit":          round(debit, 2),
                "cost_pct":       round(cost_pct * 100, 1),
                "max_profit":     round(max_profit, 2),
                "max_loss":       round(max_loss, 2),
                "ror":            ror,
                "long_itm":       long_q["itm_amount"],
                "short_itm":      short_q["itm_amount"],
                "long_delta":     long_q.get("delta"),
                "short_delta":    short_q.get("delta"),
                "long_oi":        long_q.get("oi"),
                "short_oi":       short_q.get("oi"),
                "net_theta":      net_theta,
                "net_vega":       net_vega,
                "net_delta":      net_delta,
                "net_gamma":      net_gamma,
                "win_prob":       win_prob,
                "expected_value": ev,
                "em_proximity":   em_proximity,
                "em_zone":        em_zone,
                "same_day_exit":  same_day_target,
                "next_day_exit":  next_day_target,
                "extended_exit":  extended_target,
                "warnings":       long_q["warnings"] + short_q["warnings"],
            })

    return candidates


def build_itm_bear_put_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
    expected_move: float = 0,
) -> List[Dict]:
    """
    Bear put debit spread: long higher strike put, short lower strike put.
    Both ITM (above spot). Mirror of bull call logic.
    """
    candidates = []
    itm_strikes = sorted([k for k in quotes.keys()])  # ascending

    if len(itm_strikes) < 2:
        return candidates

    em_upper = (spot + expected_move) if expected_move > 0 else 0

    for long_k in itm_strikes:
        long_q = quotes[long_k]

        for width in available_widths:
            short_k = round(long_k - width, 2)  # short = lower strike (less ITM)

            if short_k <= spot:
                continue

            short_q = quotes.get(short_k)
            if short_q is None:
                continue

            debit = round(long_q["mid"] - short_q["mid"], 4)
            if debit <= 0:
                continue

            cost_pct = debit / width
            if cost_pct > MAX_COST_PCT_OF_WIDTH:
                continue
            if cost_pct < MIN_COST_PCT_OF_WIDTH:
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

            # For bear put: short is the less ITM leg (lower strike)
            short_delta_abs = abs(short_q.get("delta") or 0)
            win_prob = round(min(short_delta_abs, 0.99), 4) if short_delta_abs > 0 else 0.5
            ev = round((win_prob * max_profit) - ((1 - win_prob) * max_loss), 4)

            # EM zone: short strike should be within EM above spot
            em_proximity = None
            em_zone = "unknown"
            if em_upper > 0:
                em_proximity = round(em_upper - short_k, 2)
                em_zone = "inside" if short_k <= em_upper else "outside"

            same_day_target = round(debit * (1 + SAME_DAY_EXIT_PCT), 2)
            next_day_target = round(debit * (1 + NEXT_DAY_EXIT_PCT), 2)
            extended_target = round(debit * (1 + EXTENDED_HOLD_EXIT_PCT), 2)

            candidates.append({
                "long":           long_k,
                "short":          short_k,
                "width":          width,
                "debit":          round(debit, 2),
                "cost_pct":       round(cost_pct * 100, 1),
                "max_profit":     round(max_profit, 2),
                "max_loss":       round(max_loss, 2),
                "ror":            ror,
                "long_itm":       long_q["itm_amount"],
                "short_itm":      short_q["itm_amount"],
                "long_delta":     long_q.get("delta"),
                "short_delta":    short_q.get("delta"),
                "long_oi":        long_q.get("oi"),
                "short_oi":       short_q.get("oi"),
                "net_theta":      net_theta,
                "net_vega":       net_vega,
                "net_delta":      net_delta,
                "net_gamma":      net_gamma,
                "win_prob":       win_prob,
                "expected_value": ev,
                "em_proximity":   em_proximity,
                "em_zone":        em_zone,
                "same_day_exit":  same_day_target,
                "next_day_exit":  next_day_target,
                "extended_exit":  extended_target,
                "warnings":       long_q["warnings"] + short_q["warnings"],
            })

    return candidates


# ─────────────────────────────────────────────────────────
# RANKING
# ─────────────────────────────────────────────────────────

def rank_candidates(candidates: List[Dict]) -> List[Dict]:
    """
    Rank by Expected Value (primary) + width bonus + EM zone bonus.
    """
    def score(c):
        ev = c.get("expected_value", 0)
        ror = c.get("ror", 0)
        width = c.get("width", 5)
        em_zone = c.get("em_zone", "unknown")

        width_bonus = 0.3 if width <= 1.0 else 0.1 if width <= 2.5 else 0

        em_bonus = 0
        em_prox = c.get("em_proximity")
        if em_zone == "inside" and em_prox is not None:
            if em_prox <= 2.0:
                em_bonus = 0.4
            elif em_prox <= 5.0:
                em_bonus = 0.2
            else:
                em_bonus = 0.05
        elif em_zone == "outside":
            em_bonus = -0.2

        warn_penalty = len(c.get("warnings", [])) * 0.1

        return ev + ror * 0.3 + width_bonus + em_bonus - warn_penalty

    return sorted(candidates, key=score, reverse=True)


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
        return stop_price, f"Stop at ${stop_price:.2f} ({STOP_LOSS_PCT:.0%} loss)"
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
) -> Tuple[int, List[str]]:
    """
    Score trade confidence 0-100.
    v3.6: direction-aware — bear trades score differently from bull.
    """
    reasons = []
    vol_edge = vol_edge or {}
    em_data = em_data or {}
    regime = regime or {}
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
        score = 55
        reasons.append("Manual check (no signal)")

        ror = trade.get("ror", 0)
        if ror >= 0.50:
            score += 10
            reasons.append(f"Strong RoR ({ror:.2f})")
        elif ror >= 0.30:
            score += 5
            reasons.append(f"OK RoR ({ror:.2f})")

        cost_pct = trade.get("cost_pct", 100)
        if cost_pct <= 55:
            score += 10
            reasons.append(f"Great pricing ({cost_pct:.0f}%)")
        elif cost_pct <= 65:
            score += 5
            reasons.append(f"Good pricing ({cost_pct:.0f}%)")

        warn_count = len(trade.get("warnings", []))
        if warn_count:
            score -= warn_count * 5
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

    # ── Deal-breakers ──
    if has_earnings:
        score += CONFIDENCE_PENALTIES["earnings_week"]
        reasons.append("EARNINGS WEEK — BLOCKED")

    if has_dividend:
        score += CONFIDENCE_PENALTIES["dividend_in_dte"]
        reasons.append("DIVIDEND IN DTE — BLOCKED")

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
) -> Dict[str, Any]:
    """
    Main entry point. Returns a complete trade recommendation or rejection.
    v3.6: supports both bull call and bear put debit spreads.
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

    # ── Vol / EM data ──
    avg_iv = get_avg_chain_iv(contracts, spot)
    expected_move = calc_expected_move(spot, avg_iv, dte) if avg_iv > 0 else 0.0
    rv = calc_realized_vol(candle_closes) if candle_closes else 0.0
    vol_edge = calc_iv_rv_edge(avg_iv, rv) if avg_iv > 0 and rv > 0 else {}

    em_data = {
        "expected_move": expected_move,
        "spot":          spot,
        "iv":            avg_iv,
        "rv":            rv,
        "dte":           dte,
    }

    # ── Route by direction ──
    if bias == "bear":
        quotes = build_put_quotes(contracts, spot)
        if len(quotes) < 2:
            result["reason"] = f"Not enough ITM put strikes (need 2, found {len(quotes)})"
            return result

        itm_strike_list = sorted(quotes.keys())
        available_widths = detect_available_widths(itm_strike_list, spot)
        if not available_widths:
            result["reason"] = "No valid widths available from put strike increments"
            return result

        candidates = build_itm_bear_put_spreads(quotes, spot, available_widths, expected_move)
        spread_side = "put"
        spread_label = "BEAR PUT"
    else:
        quotes = build_call_quotes(contracts, spot)
        if len(quotes) < 2:
            result["reason"] = f"Not enough ITM call strikes (need 2, found {len(quotes)})"
            return result

        available_widths = detect_available_widths(list(quotes.keys()), spot)
        if not available_widths:
            result["reason"] = "No valid widths available from call strike increments"
            return result

        candidates = build_itm_debit_spreads(quotes, spot, available_widths, expected_move)
        spread_side = "call"
        spread_label = "BULL CALL"

    if not candidates:
        result["reason"] = (
            f"No valid ITM debit spreads found "
            f"(widths tried: {available_widths}, "
            f"cost cap: {MAX_COST_PCT_OF_WIDTH:.0%}, "
            f"ITM strikes: {len(quotes)})"
        )
        return result

    # ── Rank and pick best ──
    ranked = rank_candidates(candidates)
    best = ranked[0]

    # ── Width ladder ──
    ladder = []
    seen_widths = set()
    for c in ranked:
        w = c["width"]
        if w not in seen_widths:
            seen_widths.add(w)
            ladder.append(c)

    # ── Position sizing ──
    tier = webhook_data.get("tier", "2")
    num_contracts, total_risk, sizing_note = compute_position_size(best["debit"], tier)

    # ── Stop loss ──
    stop_price, stop_note = compute_stop_loss(ticker, best["debit"])

    # ── Confidence scoring ──
    regime = regime or {}
    confidence, conf_reasons = compute_confidence(
        webhook_data, best, has_earnings, has_dividend,
        vol_edge=vol_edge,
        em_data=em_data,
        regime=regime,
        direction=bias,
    )

    if confidence < MIN_CONFIDENCE_TO_TRADE:
        result["reason"] = f"Confidence {confidence}/100 below {MIN_CONFIDENCE_TO_TRADE} threshold"
        result["confidence"] = confidence
        result["conf_reasons"] = conf_reasons
        return result

    # ── Win probability gate (v3.7) ──
    # Short leg delta must be >= MIN_WIN_PROBABILITY.
    # Prevents taking spreads where the short strike is unlikely to stay ITM.
    win_prob = best.get("win_prob", 0)
    if win_prob < MIN_WIN_PROBABILITY:
        result["reason"] = (
            f"Win probability {win_prob:.0%} below {MIN_WIN_PROBABILITY:.0%} minimum "
            f"(short delta too low — strike not ITM enough)"
        )
        result["confidence"] = confidence
        result["conf_reasons"] = conf_reasons
        return result

    # ── Apply regime size multiplier ──
    regime_size_mult = regime.get("size_mult", 1.0)
    regime_note = ""
    if regime_size_mult < 1.0 and regime_size_mult > 0:
        num_contracts = max(1, int(num_contracts * regime_size_mult))
        total_risk = num_contracts * best["debit"] * 100
        regime_note = f" | Regime ×{regime_size_mult} (choppy — sized down)"

    # Rebuild sizing note after regime adjustment — one contract count, no contradictions
    sizing_note = (
        f"{num_contracts} contract(s) × ${best['debit']:.2f} = ${total_risk:.0f} risk "
        f"[max ${MAX_RISK_PER_TRADE_USD:.0f}, {MAX_RISK_PCT_ACCOUNT:.0%} acct]{regime_note}"
    )

    # ── Exit targets ──
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
        "ok":               True,
        "ticker":           ticker,
        "spot":             spot,
        "dte":              dte,
        "exp":              expiration,
        "direction":        bias,
        "spread_type":      "debit",
        "side":             spread_side,
        "spread_label":     spread_label,

        "trade":            best,
        "ladder":           ladder,
        "candidate_count":  len(candidates),

        "contracts":        num_contracts,
        "total_risk":       total_risk,
        "sizing_note":      sizing_note,

        "stop_price":       stop_price,
        "stop_note":        stop_note,

        "exits":            exits,

        "confidence":       confidence,
        "conf_reasons":     conf_reasons,

        "tier":             tier,
        "webhook_data":     webhook_data,

        "expected_move":    expected_move,
        "vol_edge":         vol_edge,
        "em_data":          em_data,
        "regime":           regime,
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
    spread_label = rec.get("spread_label", "BULL CALL")
    direction    = rec.get("direction", "bull")
    tier_emoji   = "🥇" if tier == "1" else "🥈"
    dir_emoji    = "🐻" if direction == "bear" else "🐂"

    lines = [
        f"{tier_emoji} {ticker} — {dir_emoji} {spread_label} DEBIT SPREAD",
        f"Signal: Tier {tier} | Confidence: {conf}/100",
        f"Spot: ${rec['spot']:.2f} | DTE: {rec['dte']} ({rec['exp']})",
    ]

    # Expected Move
    em = rec.get("expected_move", 0)
    if EM_DISPLAY_ON_CARD and em > 0:
        em_low  = round(rec["spot"] - em, 2)
        em_high = round(rec["spot"] + em, 2)
        lines.append(f"Expected Move: ±${em:.2f} ({em_low} – {em_high})")

    lines.append("")

    # Legs
    opt_type = "put" if direction == "bear" else "call"
    lines += [
        f"Long:  ${trade['long']} (${trade['long_itm']:.2f} ITM {opt_type})",
        f"Short: ${trade['short']} (${trade['short_itm']:.2f} ITM {opt_type})",
        f"Width: ${trade['width']:.2f} | Cost: ${trade['debit']:.2f} ({trade['cost_pct']:.0f}%)",
        f"Max Profit: ${trade['max_profit']:.2f} | RoR: {trade['ror']:.0%}",
    ]

    # Win prob / EV
    wp = trade.get("win_prob", 0)
    ev = trade.get("expected_value", 0)
    if wp > 0:
        ev_emoji = "🟢" if ev > 0 else "🔴"
        lines.append(f"Win Prob: {wp:.0%} | EV: {ev_emoji} ${ev:.2f}/contract")

    # EM zone
    em_zone = trade.get("em_zone", "unknown")
    em_prox = trade.get("em_proximity")
    if em_zone != "unknown" and em_prox is not None:
        zone_emoji = "✅" if em_zone == "inside" else "⚠️"
        lines.append(f"EM Zone: {zone_emoji} Short strike {em_zone} EM (${em_prox:+.2f} from boundary)")

    lines.append("")

    # Vol edge
    vol_edge = rec.get("vol_edge", {})
    if IV_RV_DISPLAY_ON_CARD and vol_edge.get("edge_label") and vol_edge["edge_label"] != "UNKNOWN":
        lines.append(
            f"Vol Edge: {vol_edge['edge_emoji']} {vol_edge['edge_label']} "
            f"(IV {vol_edge.get('iv_pct', 0):.0f}% vs RV {vol_edge.get('rv_pct', 0):.0f}% | "
            f"spread {vol_edge.get('edge_pct', 0):+.1f}pp)"
        )

    # Regime — single line, no trailing blank before sizing
    regime = rec.get("regime", {})
    if regime.get("label"):
        lines.append(
            f"Regime: {regime.get('emoji', '⚪')} {regime['label']} "
            f"(VIX {regime.get('vix', 0):.0f} | ADX {regime.get('adx', 0):.0f})"
        )

    lines.append("")

    # Greeks
    if trade.get("net_theta") is not None:
        parts = []
        if trade.get("net_delta") is not None: parts.append(f"Δ {trade['net_delta']:.3f}")
        if trade.get("net_gamma") is not None: parts.append(f"Γ {trade['net_gamma']:.4f}")
        parts.append(f"Θ ${trade['net_theta']:.3f}/day")
        if trade.get("net_vega") is not None:  parts.append(f"V ${trade['net_vega']:.3f}/pt")
        lines.append(" | ".join(parts))

        # Dynamic exit hints
        dynamic_exits = []
        nd = trade.get("net_delta")
        ng = trade.get("net_gamma")
        if nd is not None and abs(nd) > 0.85:
            dynamic_exits.append("Delta > 0.85 → close early (diminishing returns)")
        if ng is not None and abs(ng) > 0.05 and rec.get("dte", 5) <= 1:
            dynamic_exits.append("Gamma spike on 0-1 DTE → tighten stop (pin risk)")
        if trade.get("net_vega") is not None and abs(trade["net_vega"]) > 0.03:
            dynamic_exits.append("IV crush >5pts → close (vega drag)")
        if dynamic_exits:
            lines.append("⚡ " + " | ".join(dynamic_exits))

        lines.append("")

    # Sizing — single consolidated line
    lines.append(f"Size: {rec['sizing_note']}")
    lines.append("")

    # Exit targets
    lines += [
        "📊 Exit Targets:",
        f"  Same Day (30%): sell at ${exits['same_day']['sell_at']:.2f} → +${exits['same_day']['profit_total']:.0f}",
        f"  Next Day (35%): sell at ${exits['next_day']['sell_at']:.2f} → +${exits['next_day']['profit_total']:.0f}",
        f"  Extended (50%): sell at ${exits['extended']['sell_at']:.2f} → +${exits['extended']['profit_total']:.0f}",
    ]

    # Stop
    if rec.get("stop_price"):
        lines.append(f"  Stop: ${rec['stop_price']:.2f} ({rec['stop_note']})")
    else:
        lines.append(f"  {rec['stop_note']}")

    lines.append("")

    # Width ladder
    ladder = rec.get("ladder", [])
    if len(ladder) > 1:
        lines.append("📐 Width Options:")
        for c in ladder:
            star  = " ⭐" if c["long"] == trade["long"] and c["short"] == trade["short"] else ""
            ev_c  = c.get("expected_value", 0)
            wp_c  = c.get("win_prob", 0)
            lines.append(
                f"  ${c['width']:.2f}w | ${c['debit']:.2f} ({c['cost_pct']:.0f}%) | "
                f"EV ${ev_c:.2f} | {wp_c:.0%} win | {c['long']}/{c['short']}{star}"
            )
        lines.append("")

    # Liquidity warnings
    if trade.get("warnings"):
        lines.append("⚠️ " + "; ".join(trade["warnings"][:3]))
        lines.append("")

    # Confidence breakdown — max 3 items, each on own line for readability
    if rec.get("conf_reasons"):
        reasons_short = rec["conf_reasons"][:3]
        lines.append("🧠 " + " | ".join(reasons_short))
        lines.append("")

    # Footer — always last
    lines.append("— Not financial advice —")
    return "\n".join(lines)
