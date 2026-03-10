# swing_engine.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Brad's Swing Engine v1.0
#
# Fibonacci-aware ITM debit spread builder for 7-60 DTE swing trades.
# Uses Black-Scholes for theoretical pricing and fair value comparison.
#
# Key differences from scalp engine (options_engine_v3.py):
#   - DTE range: 7-60 (vs 0-10 for scalps)
#   - Fib level drives confidence scoring and strike selection
#   - B-S fair value check — skip spreads priced above theoretical
#   - Exit rules: % profit OR time-based (14 DTE remaining), not same-day
#   - Swing-appropriate sizing (smaller — more time = more capital at risk)

import math
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# SWING RULES
# ─────────────────────────────────────────────────────────

SWING_MIN_DTE            = 7
SWING_MAX_DTE            = 60
SWING_TARGET_DTE         = 30       # Prefer ~30 DTE when available
SWING_MAX_EXPIRATIONS    = 6        # Check more expirations than scalp

SWING_MAX_COST_PCT       = 0.70
SWING_MIN_COST_PCT       = 0.20

# Sizing — smaller than scalp due to longer exposure
SWING_MAX_RISK_USD        = 1000.0
SWING_MAX_RISK_PCT        = 0.015   # 1.5% of account vs 2% for scalps
SWING_ACCOUNT_SIZE        = 100_000.0
SWING_MAX_CONTRACTS       = 20

# B-S fair value gate — don't pay more than X% above theoretical
SWING_BS_PREMIUM_MAX_PCT  = 15.0    # Max 15% above B-S fair value

# Exit rules (different from scalp)
SWING_EXIT_50_PCT         = 0.50    # Primary target: 50% profit
SWING_EXIT_75_PCT         = 0.75    # Extended target: 75% profit
SWING_EXIT_DTE_REMAINING  = 14      # Time stop: close at 14 DTE regardless
SWING_STOP_LOSS_PCT       = 0.35    # Stop at 35% loss (tighter than scalp)

# Fib level weights for confidence scoring
FIB_LEVELS = {
    "61.8": {"weight": 25, "label": "Golden Ratio (61.8%)", "emoji": "🌟"},
    "50.0": {"weight": 18, "label": "Midpoint (50.0%)",     "emoji": "⭐"},
    "38.2": {"weight": 12, "label": "Shallow (38.2%)",      "emoji": "✨"},
    "78.6": {"weight": 8,  "label": "Deep (78.6%)",         "emoji": "💫"},
}

# Extension targets (used for RoR calculation context)
FIB_EXTENSIONS = {
    "1.272": "127.2% extension",
    "1.618": "161.8% extension (golden)",
}

# Swing confidence thresholds
SWING_MIN_CONFIDENCE      = 45
SWING_WIDTH_PREFERENCE    = [1.0, 2.5, 5.0, 10.0]  # Allow wider spreads for swing


# ─────────────────────────────────────────────────────────
# BLACK-SCHOLES CORE
# ─────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using Abramowitz & Stegun approximation."""
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0
    # Use math.erfc for accuracy
    return 0.5 * math.erfc(-x / math.sqrt(2))


def black_scholes_price(
    spot: float,
    strike: float,
    dte: int,
    iv: float,
    rate: float = 0.05,
    option_type: str = "call",
) -> Dict:
    """
    Black-Scholes option pricing.

    Args:
        spot:        Current stock price
        strike:      Option strike price
        dte:         Days to expiration
        iv:          Annualized implied volatility (e.g. 0.25 for 25%)
        rate:        Risk-free rate (default 5%)
        option_type: "call" or "put"

    Returns dict with:
        price:       Theoretical option price
        delta:       Option delta
        gamma:       Option gamma
        theta:       Daily theta (dollars)
        vega:        Vega per 1% IV move
        prob_itm:    Probability of expiring ITM (N(d2) for calls)
        d1, d2:      B-S intermediate values
    """
    if iv <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
        return {"price": 0.0, "delta": 0.0, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0, "prob_itm": 0.0}

    T = dte / 365.0
    sqrt_T = math.sqrt(T)

    try:
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv ** 2) * T) / (iv * sqrt_T)
        d2 = d1 - iv * sqrt_T

        nd1 = _norm_cdf(d1)
        nd2 = _norm_cdf(d2)
        nd1_neg = _norm_cdf(-d1)
        nd2_neg = _norm_cdf(-d2)

        # Standard normal PDF at d1
        pdf_d1 = math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi)

        discount = math.exp(-rate * T)

        if option_type == "call":
            price = spot * nd1 - strike * discount * nd2
            delta = nd1
            prob_itm = nd2
        else:
            price = strike * discount * nd2_neg - spot * nd1_neg
            delta = nd1 - 1.0
            prob_itm = nd2_neg

        gamma = pdf_d1 / (spot * iv * sqrt_T)
        vega = spot * sqrt_T * pdf_d1 * 0.01   # per 1% IV move
        theta = (-(spot * pdf_d1 * iv) / (2 * sqrt_T)
                 - rate * strike * discount * (nd2 if option_type == "call" else nd2_neg)) / 365.0

        return {
            "price":    round(max(price, 0.0), 4),
            "delta":    round(delta, 4),
            "gamma":    round(gamma, 6),
            "theta":    round(theta, 4),
            "vega":     round(vega, 4),
            "prob_itm": round(prob_itm, 4),
            "d1":       round(d1, 4),
            "d2":       round(d2, 4),
        }

    except (ValueError, ZeroDivisionError, OverflowError) as e:
        log.warning(f"B-S calculation error: {e}")
        return {"price": 0.0, "delta": 0.0, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0, "prob_itm": 0.0}


def bs_spread_value(
    spot: float,
    long_strike: float,
    short_strike: float,
    dte: int,
    iv: float,
    rate: float = 0.05,
    option_type: str = "call",
) -> Dict:
    """
    Theoretical value of a debit spread using Black-Scholes.

    For bull call: long lower strike, short higher strike (both ITM).
    For bear put:  long higher strike, short lower strike (both ITM).

    Returns theoretical debit, fair value range, and per-leg greeks.
    """
    long_bs  = black_scholes_price(spot, long_strike,  dte, iv, rate, option_type)
    short_bs = black_scholes_price(spot, short_strike, dte, iv, rate, option_type)

    theoretical_debit = round(long_bs["price"] - short_bs["price"], 4)
    width = abs(long_strike - short_strike)

    # Fair value range: theoretical ± 10%
    fair_low  = round(theoretical_debit * 0.90, 2)
    fair_high = round(theoretical_debit * 1.10, 2)

    net_delta = round(long_bs["delta"] - short_bs["delta"], 4)
    net_theta = round(long_bs["theta"] - short_bs["theta"], 4)
    net_vega  = round(long_bs["vega"]  - short_bs["vega"],  4)
    net_gamma = round(long_bs["gamma"] - short_bs["gamma"], 6)

    # Win probability: probability both legs expire ITM
    # For bull call: P(price > short_strike at expiry) = N(d2) of short leg
    win_prob = short_bs["prob_itm"]

    # Expected value per contract
    max_profit = round(width - theoretical_debit, 4)
    ev = round((win_prob * max_profit) - ((1 - win_prob) * theoretical_debit), 4)

    return {
        "theoretical_debit": max(theoretical_debit, 0.0),
        "fair_low":          fair_low,
        "fair_high":         fair_high,
        "width":             width,
        "max_profit":        max_profit,
        "net_delta":         net_delta,
        "net_theta":         net_theta,
        "net_vega":          net_vega,
        "net_gamma":         net_gamma,
        "win_prob":          win_prob,
        "expected_value":    ev,
        "long_bs":           long_bs,
        "short_bs":          short_bs,
    }


# ─────────────────────────────────────────────────────────
# EXPECTED MOVE (14-day swing horizon)
# ─────────────────────────────────────────────────────────

def calc_swing_expected_move(
    spot: float,
    iv: float,
    days: int = 14,
) -> Dict:
    """
    Calculate expected move over a swing horizon (default 14 days).
    Uses 1 std dev (68% probability range) and 2 std dev (95%).

    Returns both the EM and the Fib extension targets from current price.
    """
    if iv <= 0 or spot <= 0:
        return {}

    em_1sd = round(spot * iv * math.sqrt(days / 365.0), 2)
    em_2sd = round(em_1sd * 2, 2)

    # Fib extension targets from current price
    extensions = {}
    for ratio_str, label in FIB_EXTENSIONS.items():
        ratio = float(ratio_str)
        extensions[ratio_str] = {
            "bull_target": round(spot + (em_1sd * ratio), 2),
            "bear_target": round(spot - (em_1sd * ratio), 2),
            "label":       label,
        }

    return {
        "days":          days,
        "iv":            iv,
        "em_1sd":        em_1sd,
        "em_2sd":        em_2sd,
        "bull_range":    (round(spot - em_1sd, 2), round(spot + em_1sd, 2)),
        "bear_range":    (round(spot - em_2sd, 2), round(spot + em_2sd, 2)),
        "extensions":    extensions,
    }


# ─────────────────────────────────────────────────────────
# FIB LEVEL SCORING
# ─────────────────────────────────────────────────────────

def score_fib_level(
    fib_level: str,
    fib_distance_pct: float,
    direction: str = "bull",
    weekly_confirmed: bool = False,
    daily_confirmed: bool = False,
) -> Tuple[int, List[str]]:
    """
    Score the quality of a Fibonacci entry setup.

    Args:
        fib_level:         Which Fib level price is near ("61.8", "50.0", etc.)
        fib_distance_pct:  How close price is to the level (0.0 = exact, 2.0 = 2% away)
        direction:         "bull" or "bear"
        weekly_confirmed:  Weekly trend confirms direction
        daily_confirmed:   Daily trend confirms direction

    Returns (score_addition, reasons)
    """
    score = 0
    reasons = []

    # Base score from Fib level quality
    fib_info = FIB_LEVELS.get(fib_level, {})
    if fib_info:
        base = fib_info["weight"]

        # Distance penalty — closer to level = better
        if fib_distance_pct <= 0.5:
            score += base
            reasons.append(f"Fib {fib_level}% {fib_info['emoji']} (exact — {fib_distance_pct:.1f}% away)")
        elif fib_distance_pct <= 1.5:
            score += int(base * 0.8)
            reasons.append(f"Fib {fib_level}% {fib_info['emoji']} (near — {fib_distance_pct:.1f}% away)")
        elif fib_distance_pct <= 3.0:
            score += int(base * 0.5)
            reasons.append(f"Fib {fib_level}% (approaching — {fib_distance_pct:.1f}% away)")
        else:
            score += int(base * 0.2)
            reasons.append(f"Fib {fib_level}% (distant — {fib_distance_pct:.1f}% away)")

    # Trend confirmation bonuses
    if weekly_confirmed and daily_confirmed:
        score += 20
        reasons.append("Weekly + Daily trend aligned")
    elif weekly_confirmed:
        score += 12
        reasons.append("Weekly trend confirmed")
    elif daily_confirmed:
        score += 8
        reasons.append("Daily trend confirmed")
    else:
        score -= 10
        reasons.append("No trend confirmation")

    return score, reasons


# ─────────────────────────────────────────────────────────
# DTE SELECTOR
# ─────────────────────────────────────────────────────────

def select_best_dte(
    chains: List[Tuple],
    spot: float,
    iv: float,
    fib_level: str = "61.8",
) -> List[Tuple]:
    """
    Score and rank available expirations for swing trading.

    Scoring factors:
      - Proximity to TARGET_DTE (30 days)
      - IV term structure (prefer lower IV expirations)
      - Liquidity at that expiration
      - Fib-adjusted time horizon (61.8% setups deserve more time)

    Returns chains sorted by score, best first.
    """
    # Fib setups near 61.8% deserve more time to develop
    fib_time_bonus = {
        "61.8": 35,   # Golden ratio — give it 35 extra target days
        "50.0": 20,
        "38.2": 10,
        "78.6": 45,   # Deep retracement — needs most time
    }
    adjusted_target = SWING_TARGET_DTE + fib_time_bonus.get(fib_level, 0)

    scored = []
    for exp, dte, contracts in chains:
        if dte < SWING_MIN_DTE or dte > SWING_MAX_DTE:
            continue

        # Proximity to adjusted target DTE
        dte_score = 100 - abs(dte - adjusted_target) * 2

        # Liquidity score — count contracts with valid bid/ask
        liquid = sum(
            1 for c in contracts
            if c.get("bid") and c.get("ask") and c.get("openInterest", 0) > 50
        )
        liq_score = min(liquid / 10, 10)  # cap at 10

        total = dte_score + liq_score
        scored.append((total, exp, dte, contracts))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [(exp, dte, contracts) for _, exp, dte, contracts in scored]


# ─────────────────────────────────────────────────────────
# SWING SPREAD BUILDER
# ─────────────────────────────────────────────────────────

def build_swing_spreads(
    contracts: List[Dict],
    spot: float,
    dte: int,
    iv: float,
    direction: str = "bull",
    swing_em: Dict = None,
) -> List[Dict]:
    """
    Build ITM debit spread candidates for swing trading.
    Uses B-S fair value to filter overpriced spreads.

    Bull: ITM calls (strikes below spot)
    Bear: ITM puts  (strikes above spot)
    """
    swing_em = swing_em or {}
    candidates = []

    # Filter to correct option type and ITM strikes
    if direction == "bull":
        relevant = [
            c for c in contracts
            if (c.get("right") or "").lower() == "call"
            and (c.get("strike") or 0) < spot
            and (c.get("mid") or 0) > 0
        ]
        relevant.sort(key=lambda c: c["strike"], reverse=True)  # highest ITM first
    else:
        relevant = [
            c for c in contracts
            if (c.get("right") or "").lower() == "put"
            and (c.get("strike") or 0) > spot
            and (c.get("mid") or 0) > 0
        ]
        relevant.sort(key=lambda c: c["strike"])  # lowest ITM first

    if len(relevant) < 2:
        return candidates

    strikes = [c["strike"] for c in relevant]
    quote_map = {c["strike"]: c for c in relevant}

    for long_c in relevant:
        long_k = long_c["strike"]
        long_mid = long_c.get("mid") or 0
        if long_mid <= 0:
            continue

        for width in SWING_WIDTH_PREFERENCE:
            if direction == "bull":
                short_k = round(long_k + width, 2)
                if short_k >= spot:
                    continue
            else:
                short_k = round(long_k - width, 2)
                if short_k <= spot:
                    continue

            short_c = quote_map.get(short_k)
            if not short_c:
                continue

            short_mid = short_c.get("mid") or 0
            if short_mid <= 0:
                continue

            market_debit = round(long_mid - short_mid, 4)
            if market_debit <= 0:
                continue

            cost_pct = market_debit / width
            if cost_pct > SWING_MAX_COST_PCT or cost_pct < SWING_MIN_COST_PCT:
                continue

            # ── Black-Scholes fair value check ──
            opt_type = "call" if direction == "bull" else "put"
            bs = bs_spread_value(spot, long_k, short_k, dte, iv,
                                 option_type=opt_type)
            theoretical = bs["theoretical_debit"]

            if theoretical > 0:
                bs_premium_pct = ((market_debit - theoretical) / theoretical) * 100
            else:
                bs_premium_pct = 0.0

            # Skip if market price is too far above fair value
            if bs_premium_pct > SWING_BS_PREMIUM_MAX_PCT:
                continue

            max_profit = round(width - market_debit, 4)
            max_loss = market_debit
            ror = round(max_profit / max_loss, 4) if max_loss > 0 else 0

            # Use B-S win probability (more accurate for longer DTE)
            win_prob = bs["win_prob"]
            ev = bs["expected_value"]

            # EM zone check (14-day swing horizon)
            em_zone = "unknown"
            em_proximity = None
            em_1sd = swing_em.get("em_1sd", 0)
            if em_1sd > 0:
                if direction == "bull":
                    em_boundary = spot - em_1sd
                    em_proximity = round(short_k - em_boundary, 2)
                    em_zone = "inside" if short_k >= em_boundary else "outside"
                else:
                    em_boundary = spot + em_1sd
                    em_proximity = round(em_boundary - short_k, 2)
                    em_zone = "inside" if short_k <= em_boundary else "outside"

            # Swing exit targets
            same_target     = round(market_debit * (1 + SWING_EXIT_50_PCT), 2)
            extended_target = round(market_debit * (1 + SWING_EXIT_75_PCT), 2)
            stop_price      = round(market_debit * (1 - SWING_STOP_LOSS_PCT), 2)

            # Liquidity warnings
            warnings = []
            long_oi = long_c.get("openInterest", 0) or 0
            short_oi = short_c.get("openInterest", 0) or 0
            long_ba = (long_c.get("ask") or 0) - (long_c.get("bid") or 0)
            short_ba = (short_c.get("ask") or 0) - (short_c.get("bid") or 0)

            if long_oi < 50 or short_oi < 50:
                warnings.append(f"Low OI ({min(long_oi, short_oi)})")
            if long_ba > 0.75 or short_ba > 0.75:
                warnings.append(f"Wide B/A (${max(long_ba, short_ba):.2f})")

            candidates.append({
                "long":              long_k,
                "short":             short_k,
                "width":             width,
                "debit":             round(market_debit, 2),
                "cost_pct":          round(cost_pct * 100, 1),
                "max_profit":        round(max_profit, 2),
                "max_loss":          round(max_loss, 2),
                "ror":               ror,
                "long_itm":          round(abs(spot - long_k), 2),
                "short_itm":         round(abs(spot - short_k), 2),
                # B-S data
                "theoretical_debit": round(theoretical, 2),
                "bs_premium_pct":    round(bs_premium_pct, 1),
                "win_prob":          round(win_prob, 4),
                "expected_value":    round(ev, 4),
                "net_delta":         bs["net_delta"],
                "net_theta":         bs["net_theta"],
                "net_vega":          bs["net_vega"],
                "net_gamma":         bs["net_gamma"],
                # EM
                "em_zone":           em_zone,
                "em_proximity":      em_proximity,
                # Exits
                "exit_50pct":        same_target,
                "exit_75pct":        extended_target,
                "stop_price":        stop_price,
                "dte_stop":          SWING_EXIT_DTE_REMAINING,
                "warnings":          warnings,
            })

    return candidates


def rank_swing_candidates(candidates: List[Dict]) -> List[Dict]:
    """
    Rank swing candidates by expected value + B-S fair value.
    Prefer spreads closest to theoretical fair value with highest EV.
    """
    def score(c):
        ev = c.get("expected_value", 0)
        ror = c.get("ror", 0)
        width = c.get("width", 5)
        em_zone = c.get("em_zone", "unknown")
        bs_premium = abs(c.get("bs_premium_pct", 10))

        width_bonus = 0.2 if width <= 2.5 else 0.05
        em_bonus = 0.3 if em_zone == "inside" else -0.1
        bs_bonus = (SWING_BS_PREMIUM_MAX_PCT - bs_premium) / 100  # closer to fair = better
        warn_penalty = len(c.get("warnings", [])) * 0.15

        return ev + ror * 0.4 + width_bonus + em_bonus + bs_bonus - warn_penalty

    return sorted(candidates, key=score, reverse=True)


# ─────────────────────────────────────────────────────────
# SWING CONFIDENCE SCORING
# ─────────────────────────────────────────────────────────

def compute_swing_confidence(
    webhook_data: Dict,
    trade: Dict,
    fib_score: int,
    fib_reasons: List[str],
    iv_rank: float = 0,
    swing_em: Dict = None,
    bs_data: Dict = None,
    direction: str = "bull",
) -> Tuple[int, List[str]]:
    """
    Swing-specific confidence scoring.

    Weights Fib level quality heavily, then trend confirmation,
    then B-S fair value, then IV environment.
    """
    score = 30
    reasons = list(fib_reasons)
    swing_em = swing_em or {}
    bs_data = bs_data or {}
    is_bear = direction == "bear"

    # ── Fib level quality (already scored externally) ──
    score += fib_score

    # ── Tier boost ──
    tier = webhook_data.get("tier", "2")
    if tier == "1":
        score += 15
        reasons.append("T1 swing signal")
    else:
        score += 5
        reasons.append("T2 swing signal")

    # ── HTF trend ──
    if webhook_data.get("htf_confirmed"):
        score += 12
        reasons.append("Daily trend confirmed")
    elif webhook_data.get("htf_converging"):
        score += 6
        reasons.append("Daily trend converging")
    else:
        score -= 15
        reasons.append("Daily trend diverging")

    # ── Weekly trend ──
    weekly_bull = webhook_data.get("weekly_bull", False)
    weekly_bear = webhook_data.get("weekly_bear", False)
    if is_bear:
        if weekly_bear:
            score += 15
            reasons.append("Weekly trend bearish (confirms bear)")
        elif weekly_bull:
            score -= 12
            reasons.append("Weekly trend bullish (fights bear)")
    else:
        if weekly_bull:
            score += 15
            reasons.append("Weekly trend bullish (confirms bull)")
        elif weekly_bear:
            score -= 12
            reasons.append("Weekly trend bearish (fights bull)")

    # ── Volume contraction (coiling before move) ──
    if webhook_data.get("vol_contracting"):
        score += 8
        reasons.append("Volume contracting (coiling)")

    # ── RSI/MFI direction ──
    rsi_mfi_bull = webhook_data.get("rsi_mfi_bull", False)
    if is_bear:
        if not rsi_mfi_bull:
            score += 5
            reasons.append("RSI+MFI selling")
    else:
        if rsi_mfi_bull:
            score += 5
            reasons.append("RSI+MFI buying")

    # ── IV environment ──
    # For swing debit spread buyers: low IV = cheap entry
    if iv_rank > 0:
        if iv_rank < 20:
            score += 12
            reasons.append(f"IV very low (rank {iv_rank:.0f}) — cheap entry")
        elif iv_rank < 40:
            score += 6
            reasons.append(f"IV low (rank {iv_rank:.0f})")
        elif iv_rank > 70:
            score -= 10
            reasons.append(f"IV elevated (rank {iv_rank:.0f}) — expensive entry")

    # ── B-S fair value ──
    bs_premium = trade.get("bs_premium_pct", 0)
    if bs_premium < 0:
        score += 8
        reasons.append(f"Spread below B-S fair value ({bs_premium:+.1f}%) — value entry")
    elif bs_premium < 5:
        score += 4
        reasons.append(f"Spread near B-S fair value ({bs_premium:+.1f}%)")
    elif bs_premium > 10:
        score -= 6
        reasons.append(f"Spread above B-S fair value ({bs_premium:+.1f}%)")

    # ── Win probability from B-S ──
    win_prob = trade.get("win_prob", 0)
    if win_prob >= 0.75:
        score += 8
        reasons.append(f"B-S win prob {win_prob:.0%}")
    elif win_prob >= 0.60:
        score += 4
        reasons.append(f"B-S win prob {win_prob:.0%}")
    elif win_prob < 0.45:
        score -= 8
        reasons.append(f"B-S win prob low ({win_prob:.0%})")

    # ── EM zone ──
    em_zone = trade.get("em_zone", "unknown")
    if em_zone == "inside":
        score += 5
        reasons.append("Short strike inside 14-day EM")
    elif em_zone == "outside":
        score -= 8
        reasons.append("Short strike beyond 14-day EM")

    score = max(0, min(100, score))
    return score, reasons


# ─────────────────────────────────────────────────────────
# SWING POSITION SIZING
# ─────────────────────────────────────────────────────────

def compute_swing_size(debit: float, tier: str = "2") -> Tuple[int, float, str]:
    """
    Smaller sizing than scalp — more capital at risk over longer hold.
    """
    cost_per = debit * 100
    if cost_per <= 0:
        return 1, 0, "Cannot compute sizing"

    max_from_usd = int(SWING_MAX_RISK_USD / cost_per)
    max_from_pct = int((SWING_ACCOUNT_SIZE * SWING_MAX_RISK_PCT) / cost_per)

    contracts = max(1, min(max_from_usd, max_from_pct, SWING_MAX_CONTRACTS))

    # Tier multiplier
    mult = 1.0 if tier == "1" else 0.75
    contracts = max(1, int(contracts * mult))

    total = contracts * cost_per
    note = (f"{contracts} contract(s) × ${debit:.2f} = ${total:.0f} risk "
            f"[swing max ${SWING_MAX_RISK_USD:.0f}, {SWING_MAX_RISK_PCT:.1%} acct]")

    return contracts, total, note


# ─────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────

def recommend_swing_trade(
    ticker: str,
    spot: float,
    chains: List[Tuple],
    webhook_data: Dict,
    iv_rank: float = 0,
) -> Dict[str, Any]:
    """
    Main entry point for swing trade recommendations.

    Args:
        ticker:       Ticker symbol
        spot:         Current price
        chains:       List of (exp, dte, contracts) tuples
        webhook_data: Pine webhook payload (swing format)
        iv_rank:      IV percentile rank (0-100)

    Returns complete trade recommendation or rejection with reason.
    """
    webhook_data = webhook_data or {}
    result = {"ok": False, "ticker": ticker, "spot": spot, "type": "swing"}

    direction = (webhook_data.get("bias") or "bull").lower()
    if direction not in ("bull", "bear"):
        result["reason"] = f"Direction '{direction}' not allowed"
        return result

    fib_level       = str(webhook_data.get("fib_level", "61.8"))
    fib_distance    = float(webhook_data.get("fib_distance_pct", 2.0))
    weekly_bull     = webhook_data.get("weekly_bull", False)
    weekly_bear     = webhook_data.get("weekly_bear", False)
    weekly_ok       = weekly_bull if direction == "bull" else weekly_bear
    daily_ok        = webhook_data.get("htf_confirmed") or webhook_data.get("htf_converging")

    # ── Score the Fib setup ──
    fib_score, fib_reasons = score_fib_level(
        fib_level, fib_distance, direction,
        weekly_confirmed=weekly_ok,
        daily_confirmed=bool(daily_ok),
    )

    # ── Get average IV from best chain ──
    all_iv = []
    for _, _, contracts in chains:
        for c in contracts:
            iv = c.get("iv")
            if iv and iv > 0:
                all_iv.append(iv)
    avg_iv = sum(all_iv) / len(all_iv) if all_iv else 0.25  # fallback 25%

    # ── Select best DTE ──
    sorted_chains = select_best_dte(chains, spot, avg_iv, fib_level)
    if not sorted_chains:
        result["reason"] = f"No expirations in {SWING_MIN_DTE}-{SWING_MAX_DTE} DTE range"
        return result

    # ── 14-day swing expected move ──
    swing_em = calc_swing_expected_move(spot, avg_iv, days=14)

    # ── Try chains in order until we find a good spread ──
    all_reasons = []
    best_rec = None

    for exp, dte, contracts in sorted_chains[:SWING_MAX_EXPIRATIONS]:
        candidates = build_swing_spreads(
            contracts, spot, dte, avg_iv,
            direction=direction,
            swing_em=swing_em,
        )

        if not candidates:
            all_reasons.append(f"DTE {dte} ({exp}): no valid spreads")
            continue

        ranked = rank_swing_candidates(candidates)
        best = ranked[0]

        # Build width ladder
        ladder = []
        seen_w = set()
        for c in ranked:
            if c["width"] not in seen_w:
                seen_w.add(c["width"])
                ladder.append(c)

        # Position sizing
        tier = webhook_data.get("tier", "2")
        num_contracts, total_risk, sizing_note = compute_swing_size(best["debit"], tier)

        # Confidence scoring
        confidence, conf_reasons = compute_swing_confidence(
            webhook_data, best,
            fib_score=fib_score,
            fib_reasons=fib_reasons,
            iv_rank=iv_rank,
            swing_em=swing_em,
            direction=direction,
        )

        if confidence < SWING_MIN_CONFIDENCE:
            all_reasons.append(
                f"DTE {dte}: confidence {confidence}/100 below {SWING_MIN_CONFIDENCE}"
            )
            continue

        # ── Build exits ──
        exits = {
            "primary": {
                "target_pct": "50%",
                "sell_at":    best["exit_50pct"],
                "profit_per": round((best["exit_50pct"] - best["debit"]) * 100, 2),
                "profit_total": round((best["exit_50pct"] - best["debit"]) * 100 * num_contracts, 2),
            },
            "extended": {
                "target_pct": "75%",
                "sell_at":    best["exit_75pct"],
                "profit_per": round((best["exit_75pct"] - best["debit"]) * 100, 2),
                "profit_total": round((best["exit_75pct"] - best["debit"]) * 100 * num_contracts, 2),
            },
            "time_stop": {
                "rule": f"Close at {SWING_EXIT_DTE_REMAINING} DTE remaining regardless of P/L",
                "dte_remaining": SWING_EXIT_DTE_REMAINING,
            },
        }

        best_rec = {
            "ok":            True,
            "ticker":        ticker,
            "spot":          spot,
            "dte":           dte,
            "exp":           exp,
            "direction":     direction,
            "spread_label":  "BEAR PUT" if direction == "bear" else "BULL CALL",
            "trade":         best,
            "ladder":        ladder,
            "contracts":     num_contracts,
            "total_risk":    total_risk,
            "sizing_note":   sizing_note,
            "exits":         exits,
            "confidence":    confidence,
            "conf_reasons":  conf_reasons,
            "tier":          tier,
            "fib_level":     fib_level,
            "fib_distance":  fib_distance,
            "fib_score":     fib_score,
            "avg_iv":        avg_iv,
            "iv_rank":       iv_rank,
            "swing_em":      swing_em,
            "webhook_data":  webhook_data,
        }
        break

    if not best_rec:
        combined = "No valid swing spread found"
        if all_reasons:
            combined += "\n" + "\n".join(all_reasons[:4])
        result["reason"] = combined
        return result

    return best_rec


# ─────────────────────────────────────────────────────────
# SWING TRADE CARD FORMATTER
# ─────────────────────────────────────────────────────────

def format_swing_card(rec: Dict) -> str:
    if not rec.get("ok"):
        return (
            f"❌ {rec.get('ticker', '?')} — NO SWING TRADE\n"
            f"Reason: {rec.get('reason', 'Unknown')}\n\n"
            f"— Not financial advice —"
        )

    trade   = rec["trade"]
    exits   = rec["exits"]
    ticker  = rec["ticker"]
    tier    = rec.get("tier", "?")
    conf    = rec.get("confidence", 0)
    direction = rec.get("direction", "bull")
    fib_level = rec.get("fib_level", "?")
    spread_label = rec.get("spread_label", "BULL CALL")

    tier_emoji = "🥇" if tier == "1" else "🥈"
    dir_emoji  = "🐻" if direction == "bear" else "🐂"

    fib_info = FIB_LEVELS.get(str(fib_level), {})
    fib_emoji = fib_info.get("emoji", "📐")
    fib_label = fib_info.get("label", f"{fib_level}% Fib")

    lines = [
        f"{tier_emoji} {ticker} — {dir_emoji} SWING {spread_label}",
        f"Signal: Tier {tier} | Confidence: {conf}/100",
        f"Spot: ${rec['spot']:.2f} | DTE: {rec['dte']} ({rec['exp']})",
        f"Fib: {fib_emoji} {fib_label} ({rec.get('fib_distance', 0):.1f}% away)",
        "",
    ]

    # 14-day expected move
    swing_em = rec.get("swing_em", {})
    if swing_em.get("em_1sd"):
        em = swing_em["em_1sd"]
        bull_r, bear_r = swing_em.get("bull_range", (0, 0))
        lines.append(f"14-Day EM (1σ): ±${em:.2f} | Range: ${bull_r:.2f} – ${bear_r:.2f}")

        # Extension targets
        exts = swing_em.get("extensions", {})
        if exts:
            ext_lines = []
            for ratio, data in exts.items():
                key = "bull_target" if direction == "bull" else "bear_target"
                ext_lines.append(f"${data[key]:.2f} ({data['label']})")
            lines.append("Targets: " + " | ".join(ext_lines))
        lines.append("")

    # Trade legs
    if direction == "bear":
        lines += [
            f"Long:  ${trade['long']} (${trade['long_itm']:.2f} ITM put)",
            f"Short: ${trade['short']} (${trade['short_itm']:.2f} ITM put)",
        ]
    else:
        lines += [
            f"Long:  ${trade['long']} (${trade['long_itm']:.2f} ITM call)",
            f"Short: ${trade['short']} (${trade['short_itm']:.2f} ITM call)",
        ]

    lines += [
        f"Width: ${trade['width']:.2f} | Cost: ${trade['debit']:.2f} ({trade['cost_pct']:.0f}%)",
        f"Max Profit: ${trade['max_profit']:.2f} | RoR: {trade['ror']:.0%}",
    ]

    # B-S fair value
    theoretical = trade.get("theoretical_debit", 0)
    bs_premium  = trade.get("bs_premium_pct", 0)
    if theoretical > 0:
        bs_emoji = "🟢" if bs_premium <= 0 else "🟡" if bs_premium <= 8 else "🔴"
        lines.append(
            f"B-S Fair Value: {bs_emoji} ${theoretical:.2f} theoretical "
            f"({bs_premium:+.1f}% vs market)"
        )

    # Win probability and EV
    wp = trade.get("win_prob", 0)
    ev = trade.get("expected_value", 0)
    if wp > 0:
        ev_emoji = "🟢" if ev > 0 else "🔴"
        lines.append(f"B-S Win Prob: {wp:.0%} | EV: {ev_emoji} ${ev:.2f}/contract")

    # EM zone
    em_zone = trade.get("em_zone", "unknown")
    em_prox = trade.get("em_proximity")
    if em_zone != "unknown" and em_prox is not None:
        zone_emoji = "✅" if em_zone == "inside" else "⚠️"
        lines.append(f"EM Zone: {zone_emoji} Short strike {em_zone} 14-day EM (${em_prox:+.2f})")

    lines.append("")

    # IV context
    iv_rank = rec.get("iv_rank", 0)
    avg_iv  = rec.get("avg_iv", 0)
    if avg_iv > 0:
        iv_emoji = "🟢" if iv_rank < 30 else "🟡" if iv_rank < 60 else "🔴"
        lines.append(
            f"IV: {iv_emoji} {avg_iv*100:.0f}% | Rank: {iv_rank:.0f}/100 "
            f"({'cheap entry' if iv_rank < 30 else 'elevated' if iv_rank > 60 else 'normal'})"
        )
        lines.append("")

    # Greeks
    nd = trade.get("net_delta")
    nt = trade.get("net_theta")
    nv = trade.get("net_vega")
    ng = trade.get("net_gamma")
    if nt is not None:
        parts = []
        if nd is not None: parts.append(f"Δ {nd:.3f}")
        if ng is not None: parts.append(f"Γ {ng:.5f}")
        if nt is not None: parts.append(f"Θ ${nt:.3f}/day")
        if nv is not None: parts.append(f"V ${nv:.3f}/pt")
        lines.append(" | ".join(parts))
        lines.append("")

    # Sizing
    lines += [
        f"Size: {rec['contracts']} contract(s) | ${rec['total_risk']:.0f} risk",
        rec["sizing_note"],
        "",
    ]

    # Swing exit rules
    lines += [
        "📊 Swing Exit Rules:",
        f"  Primary (50%):  sell at ${exits['primary']['sell_at']:.2f} → +${exits['primary']['profit_total']:.0f}",
        f"  Extended (75%): sell at ${exits['extended']['sell_at']:.2f} → +${exits['extended']['profit_total']:.0f}",
        f"  Time Stop:      {exits['time_stop']['rule']}",
        f"  Loss Stop:      ${trade['stop_price']:.2f} (35% loss)",
        "",
    ]

    # Width ladder
    ladder = rec.get("ladder", [])
    if len(ladder) > 1:
        lines.append("📐 Width Options:")
        for c in ladder:
            star = " ⭐" if c["long"] == trade["long"] and c["short"] == trade["short"] else ""
            lines.append(
                f"  ${c['width']:.2f}w | ${c['debit']:.2f} ({c['cost_pct']:.0f}%) | "
                f"B-S {c.get('win_prob',0):.0%} win | "
                f"EV ${c.get('expected_value',0):.2f} | "
                f"{c['long']}/{c['short']}{star}"
            )
        lines.append("")

    if trade.get("warnings"):
        lines.append("⚠️ " + "; ".join(trade["warnings"][:3]))
        lines.append("")

    # Confidence breakdown
    if rec.get("conf_reasons"):
        lines.append("🧠 " + " | ".join(rec["conf_reasons"][:5]))
        lines.append("")

    lines.append("— Not financial advice —")
    return "\n".join(lines)
