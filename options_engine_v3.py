# options_engine_v3.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Brad's Options Engine v3 — Rule-based ITM Debit Spread Builder
#
# v3.4 additions:
#   - Expected Move calculation on trade cards
#   - IV vs RV (realized vol) edge scoring
#   - Edge data feeds into confidence scoring

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
    itm_strikes = sorted([k for k in strikes if k < spot])
    if len(itm_strikes) < 2:
        return []

    increments = set()
    for i in range(len(itm_strikes) - 1):
        diff = round(itm_strikes[i + 1] - itm_strikes[i], 2)
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
    Calculate the expected move for a given spot, IV, and DTE.

    Expected Move = spot × IV × sqrt(DTE / 365)

    This gives the 1-standard-deviation expected range.
    ~68% chance price stays within ±EM.

    Args:
        spot: current stock price
        iv:   annualized implied volatility (e.g. 0.25 for 25%)
        dte:  days to expiration

    Returns:
        Dollar amount of expected move (one side)
    """
    if iv is None or iv <= 0 or dte <= 0:
        return 0.0
    return round(spot * iv * math.sqrt(dte / 365.0), 2)


def calc_realized_vol(closes: List[float]) -> float:
    """
    Calculate annualized realized (historical) volatility from daily closes.

    Uses log returns and annualizes by sqrt(252).

    Args:
        closes: list of daily closing prices (most recent last),
                should be at least RV_LOOKBACK_DAYS + 1 long

    Returns:
        Annualized realized volatility as a decimal (e.g. 0.22 for 22%)
    """
    if not closes or len(closes) < 3:
        return 0.0

    log_returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            log_returns.append(math.log(closes[i] / closes[i - 1]))

    if len(log_returns) < 2:
        return 0.0

    # Standard deviation of log returns
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_vol = math.sqrt(variance)

    # Annualize
    annual_vol = daily_vol * math.sqrt(RV_ANNUALIZE_FACTOR)
    return round(annual_vol, 4)


def calc_iv_rv_edge(iv: float, rv: float) -> Dict:
    """
    Compare implied volatility to realized volatility.

    Returns edge analysis:
      - edge_pct: (IV - RV) as percentage points
      - edge_label: "BUYER" / "SELLER" / "NEUTRAL"
      - edge_emoji: visual indicator
      - description: human-readable explanation

    For us (debit spread buyers):
      - RV > IV = good (vol is cheap, we're getting a deal)
      - IV > RV = bad (vol is expensive, we're overpaying)
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

    # Convert to percentages for display
    iv_pct = iv * 100
    rv_pct = rv * 100
    edge_pct = round(iv_pct - rv_pct, 1)

    if edge_pct < IV_RV_BUYER_EDGE_PCT:
        # IV < RV → implied vol is cheap → buyer's edge
        label = "BUYER"
        emoji = "🟢"
        desc = f"IV cheap vs realized ({iv_pct:.0f}% vs {rv_pct:.0f}%) — vol discount"
    elif edge_pct > IV_RV_SELLER_EDGE_PCT:
        # IV > RV → implied vol is expensive → seller's edge (bad for us)
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
    """
    Get average IV from the ATM options in the chain.
    Uses strikes closest to spot for the most representative IV.
    """
    # Collect IVs from strikes near ATM (within 3% of spot)
    atm_range = spot * 0.03
    ivs = []

    for c in contracts:
        strike = as_float(c.get("strike"), 0)
        iv = as_float(c.get("iv"), 0)
        if iv > 0 and abs(strike - spot) <= atm_range:
            ivs.append(iv)

    if not ivs:
        # Fallback: use any available IV
        for c in contracts:
            iv = as_float(c.get("iv"), 0)
            if iv > 0:
                ivs.append(iv)

    return sum(ivs) / len(ivs) if ivs else 0.0


# ─────────────────────────────────────────────────────────
# CHAIN DATA BUILDER
# ─────────────────────────────────────────────────────────

def build_call_quotes(contracts: List[Dict], spot: float) -> Dict[float, Dict]:
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


# ─────────────────────────────────────────────────────────
# SPREAD CANDIDATE BUILDER
# ─────────────────────────────────────────────────────────

def build_itm_debit_spreads(
    quotes: Dict[float, Dict],
    spot: float,
    available_widths: List[float],
) -> List[Dict]:
    candidates = []
    itm_strikes = sorted([k for k in quotes.keys()], reverse=True)

    if len(itm_strikes) < 2:
        return candidates

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

            net_theta = None
            net_vega = None
            if long_q.get("theta") is not None and short_q.get("theta") is not None:
                net_theta = round(long_q["theta"] - short_q["theta"], 4)
            if long_q.get("vega") is not None and short_q.get("vega") is not None:
                net_vega = round(long_q["vega"] - short_q["vega"], 4)

            same_day_target = round(debit * (1 + SAME_DAY_EXIT_PCT), 2)
            next_day_target = round(debit * (1 + NEXT_DAY_EXIT_PCT), 2)
            extended_target = round(debit * (1 + EXTENDED_HOLD_EXIT_PCT), 2)

            all_warnings = long_q["warnings"] + short_q["warnings"]

            candidates.append({
                "long":          long_k,
                "short":         short_k,
                "width":         width,
                "debit":         round(debit, 2),
                "cost_pct":      round(cost_pct * 100, 1),
                "max_profit":    round(max_profit, 2),
                "max_loss":      round(max_loss, 2),
                "ror":           ror,
                "long_itm":      long_q["itm_amount"],
                "short_itm":     short_q["itm_amount"],
                "long_delta":    long_q.get("delta"),
                "short_delta":   short_q.get("delta"),
                "long_oi":       long_q.get("oi"),
                "short_oi":      short_q.get("oi"),
                "net_theta":     net_theta,
                "net_vega":      net_vega,
                "same_day_exit": same_day_target,
                "next_day_exit": next_day_target,
                "extended_exit": extended_target,
                "warnings":      all_warnings,
            })

    return candidates


# ─────────────────────────────────────────────────────────
# RANKING
# ─────────────────────────────────────────────────────────

def rank_candidates(candidates: List[Dict]) -> List[Dict]:
    def score(c):
        ror = c.get("ror", 0)
        width = c.get("width", 5)

        width_bonus = 0.3 if width <= 1.0 else 0.1 if width <= 2.5 else 0
        warn_penalty = len(c.get("warnings", [])) * 0.1

        return ror + width_bonus - warn_penalty

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
# CONFIDENCE SCORING (v3.4: includes IV/RV edge)
# ─────────────────────────────────────────────────────────

def compute_confidence(
    webhook_data: Dict,
    trade: Dict,
    has_earnings: bool = False,
    has_dividend: bool = False,
    vol_edge: Dict = None,
    em_data: Dict = None,
) -> Tuple[int, List[str]]:
    """
    Score trade confidence from 0-100 based on webhook signal data,
    trade quality metrics, and IV/RV edge.

    v3.4: vol_edge and em_data feed into scoring.
    """
    reasons = []
    vol_edge = vol_edge or {}
    em_data = em_data or {}

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
            reasons.append("1H trend confirmed")
        elif webhook_data.get("htf_converging"):
            score += CONFIDENCE_BOOSTS["htf_converging"]
            reasons.append("1H trend converging")
        else:
            score += CONFIDENCE_PENALTIES["htf_diverging"]
            reasons.append("1H trend diverging")

        if webhook_data.get("daily_bull"):
            score += CONFIDENCE_BOOSTS["daily_bull"]
            reasons.append("Daily trend bullish")
        else:
            score += CONFIDENCE_PENALTIES.get("daily_bear", 0)
            reasons.append("Daily trend bearish")

        if webhook_data.get("rsi_mfi_bull"):
            score += CONFIDENCE_BOOSTS["rsi_mfi_bull"]
            reasons.append("RSI+MFI buying")

        if webhook_data.get("above_vwap"):
            score += CONFIDENCE_BOOSTS["above_vwap"]
            reasons.append("Above VWAP")

        wt2 = as_float(webhook_data.get("wt2"), 0)
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

    # ── IV / RV Edge scoring (applies to BOTH modes) ──
    edge_label = vol_edge.get("edge_label", "UNKNOWN")
    if edge_label == "BUYER":
        score += CONFIDENCE_BOOSTS.get("rv_edge", 10)
        reasons.append(f"Vol edge: BUYER ({vol_edge.get('description', '')})")
    elif edge_label == "SELLER":
        # IV rich = bad for us as debit spread buyers
        score += CONFIDENCE_PENALTIES.get("iv_crushed", -5)
        reasons.append(f"Vol edge: SELLER ({vol_edge.get('description', '')})")
    elif edge_label == "NEUTRAL" and vol_edge.get("iv", 0) > 0:
        reasons.append(f"Vol: neutral ({vol_edge.get('iv_pct', 0):.0f}% IV / {vol_edge.get('rv_pct', 0):.0f}% RV)")

    # ── Expected move: are our strikes within the EM? ──
    em_amount = em_data.get("expected_move", 0)
    if em_amount > 0 and trade.get("short"):
        spot = em_data.get("spot", 0)
        short_strike = trade.get("short", 0)
        # For bull call spread: short strike should be within EM of spot
        distance_to_short = abs(spot - short_strike) if spot > 0 else 0
        if distance_to_short <= em_amount:
            score += CONFIDENCE_BOOSTS.get("within_em", 5)
            reasons.append("Short strike within EM")
        else:
            score += CONFIDENCE_PENALTIES.get("beyond_em", -8)
            reasons.append("Short strike beyond EM")

    # ── Deal-breakers (apply to BOTH modes) ──
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
) -> Dict[str, Any]:
    """
    Main entry point. Given a ticker's option chain and signal data,
    returns a complete trade recommendation or rejection with reason.

    v3.4: accepts candle_closes for RV calculation.
    candle_closes = list of recent daily close prices (most recent last).
    """
    webhook_data = webhook_data or {}
    candle_closes = candle_closes or []
    result = {"ok": False, "ticker": ticker, "spot": spot, "dte": dte, "exp": expiration}

    # ── Rule: Direction must be bull ──
    bias = webhook_data.get("bias", "bull")
    if bias != "bull":
        result["reason"] = f"Direction '{bias}' not allowed — bull only"
        return result

    # ── Rule: DTE must be in range ──
    if dte < MIN_DTE or dte > MAX_DTE:
        result["reason"] = f"DTE {dte} outside {MIN_DTE}-{MAX_DTE} range"
        return result

    # ── Rule: No earnings week ──
    if NO_EARNINGS_WEEK and has_earnings:
        result["reason"] = "Earnings this week — trade blocked"
        result["deal_breaker"] = "earnings"
        return result

    # ── Rule: No dividend in DTE ──
    if NO_DIVIDEND_IN_DTE and has_dividend:
        result["reason"] = "Dividend ex-date within DTE — trade blocked"
        result["deal_breaker"] = "dividend"
        return result

    # ── Build ITM call quotes ──
    quotes = build_call_quotes(contracts, spot)
    if len(quotes) < 2:
        result["reason"] = f"Not enough ITM call strikes (need 2, found {len(quotes)})"
        return result

    # ── Detect available widths ──
    available_widths = detect_available_widths(list(quotes.keys()), spot)
    if not available_widths:
        result["reason"] = "No valid widths available from strike increments"
        return result

    # ── Build spread candidates ──
    candidates = build_itm_debit_spreads(quotes, spot, available_widths)
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

    # ── Build width ladder ──
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

    # ── Expected Move & IV/RV Edge (v3.4) ──
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

    # ── Confidence scoring (now includes vol edge) ──
    confidence, conf_reasons = compute_confidence(
        webhook_data, best, has_earnings, has_dividend,
        vol_edge=vol_edge,
        em_data=em_data,
    )

    if confidence < MIN_CONFIDENCE_TO_TRADE:
        result["reason"] = f"Confidence {confidence}/100 below {MIN_CONFIDENCE_TO_TRADE} threshold"
        result["confidence"] = confidence
        result["conf_reasons"] = conf_reasons
        return result

    # ── Build exit targets ──
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

    # ── Success ──
    return {
        "ok":               True,
        "ticker":           ticker,
        "spot":             spot,
        "dte":              dte,
        "exp":              expiration,
        "direction":        "bull",
        "spread_type":      "debit",
        "side":             "call",

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

        # v3.4 — Vol edge data
        "expected_move":    expected_move,
        "vol_edge":         vol_edge,
        "em_data":          em_data,
    }


# ─────────────────────────────────────────────────────────
# TRADE CARD FORMATTER (Telegram message) — v3.4 updated
# ─────────────────────────────────────────────────────────

def format_trade_card(rec: Dict) -> str:
    if not rec.get("ok"):
        reason = rec.get("reason", "Unknown")
        conf = rec.get("confidence")
        lines = [
            f"❌ {rec.get('ticker', '?')} — NO TRADE",
            f"Reason: {reason}",
        ]
        if conf is not None:
            lines.append(f"Confidence: {conf}/100")
        lines.append("")
        lines.append("— Not financial advice —")
        return "\n".join(lines)

    trade = rec["trade"]
    exits = rec["exits"]
    ticker = rec["ticker"]
    tier = rec.get("tier", "?")
    conf = rec.get("confidence", 0)

    # Header
    tier_emoji = "🥇" if tier == "1" else "🥈"
    lines = [
        f"{tier_emoji} {ticker} — BULL CALL DEBIT SPREAD",
        f"Signal: Tier {tier} | Confidence: {conf}/100",
        f"Spot: ${rec['spot']:.2f} | DTE: {rec['dte']} ({rec['exp']})",
    ]

    # v3.4 — Expected Move line
    em = rec.get("expected_move", 0)
    if EM_DISPLAY_ON_CARD and em > 0:
        em_low = round(rec["spot"] - em, 2)
        em_high = round(rec["spot"] + em, 2)
        lines.append(f"Expected Move: ±${em:.2f} ({em_low} – {em_high})")

    lines.append("")

    # Trade details
    lines += [
        f"Long:  ${trade['long']} (${trade['long_itm']:.2f} ITM)",
        f"Short: ${trade['short']} (${trade['short_itm']:.2f} ITM)",
        f"Width: ${trade['width']:.2f} | Cost: ${trade['debit']:.2f} ({trade['cost_pct']:.0f}%)",
        f"Max Profit: ${trade['max_profit']:.2f} | RoR: {trade['ror']:.0%}",
        "",
    ]

    # v3.4 — IV vs RV Edge line
    vol_edge = rec.get("vol_edge", {})
    if IV_RV_DISPLAY_ON_CARD and vol_edge.get("edge_label") and vol_edge["edge_label"] != "UNKNOWN":
        lines.append(
            f"Vol Edge: {vol_edge['edge_emoji']} {vol_edge['edge_label']} "
            f"(IV {vol_edge.get('iv_pct', 0):.0f}% vs RV {vol_edge.get('rv_pct', 0):.0f}% | "
            f"spread {vol_edge.get('edge_pct', 0):+.1f}pp)"
        )
        lines.append("")

    # Greeks
    if trade.get("net_theta") is not None:
        lines.append(
            f"Theta: ${trade['net_theta']:.3f}/day | "
            f"Vega: ${trade.get('net_vega', 0):.3f}/pt"
        )
        lines.append("")

    # Position sizing
    lines += [
        f"Size: {rec['contracts']} contract(s) | ${rec['total_risk']:.0f} risk",
        rec["sizing_note"],
        "",
    ]

    # Exit targets
    lines += [
        "📊 Exit Targets:",
        f"  Same Day (30%): sell at ${exits['same_day']['sell_at']:.2f} → +${exits['same_day']['profit_total']:.0f}",
        f"  Next Day (35%): sell at ${exits['next_day']['sell_at']:.2f} → +${exits['next_day']['profit_total']:.0f}",
        f"  Extended (50%): sell at ${exits['extended']['sell_at']:.2f} → +${exits['extended']['profit_total']:.0f}",
        "",
    ]

    # Stop loss
    if rec.get("stop_price"):
        lines.append(f"🛑 Stop: ${rec['stop_price']:.2f} ({rec['stop_note']})")
    else:
        lines.append(f"🛑 {rec['stop_note']}")
    lines.append("")

    # Width ladder
    ladder = rec.get("ladder", [])
    if len(ladder) > 1:
        lines.append("📐 Width Options:")
        for c in ladder:
            star = " ⭐" if c["long"] == trade["long"] and c["short"] == trade["short"] else ""
            lines.append(
                f"  ${c['width']:.2f}w | ${c['debit']:.2f} ({c['cost_pct']:.0f}%) | "
                f"RoR {c['ror']:.0%} | {c['long']}/{c['short']}{star}"
            )
        lines.append("")

    # Warnings
    if trade.get("warnings"):
        lines.append("⚠️ " + "; ".join(trade["warnings"][:3]))
        lines.append("")

    # Confidence breakdown
    if rec.get("conf_reasons"):
        lines.append("🧠 " + " | ".join(rec["conf_reasons"][:4]))
        lines.append("")

    lines.append("— Not financial advice —")
    return "\n".join(lines)
