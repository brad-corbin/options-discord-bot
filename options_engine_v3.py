# options_engine_v3.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Brad's Options Engine v3 — Rule-based ITM Debit Spread Builder
#
# This engine is purpose-built for one strategy:
#   Bull ITM call debit spreads, both legs in the money,
#   cost ≤ 70% of width, 1-5 DTE, specific exit targets.
#
# It does NOT try to be a general-purpose spread recommender.
# Every function serves Brad's specific trading rules.

import math
from typing import Any, Dict, List, Optional, Tuple
from trading_rules import *


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def as_float(x, default=0.0):
    """Safely convert to float."""
    if x is None:
        return default
    if isinstance(x, list):
        x = x[0] if x else default
    try:
        return float(x) if x is not None else default
    except (ValueError, TypeError):
        return default


def as_int(x, default=0):
    """Safely convert to int."""
    if x is None:
        return int(default)
    if isinstance(x, list):
        x = x[0] if x else default
    try:
        return int(x) if x is not None else int(default)
    except (ValueError, TypeError):
        return int(default)


def detect_available_widths(strikes: List[float], spot: float) -> List[float]:
    """
    Detect what strike increments are available for this chain.
    Returns list of widths from WIDTH_PREFERENCE that are achievable.
    """
    itm_strikes = sorted([k for k in strikes if k < spot])
    if len(itm_strikes) < 2:
        return []

    # Find actual increments between consecutive ITM strikes
    increments = set()
    for i in range(len(itm_strikes) - 1):
        diff = round(itm_strikes[i + 1] - itm_strikes[i], 2)
        if diff > 0:
            increments.add(diff)

    # Filter WIDTH_PREFERENCE to what's actually achievable
    available = []
    for w in WIDTH_PREFERENCE:
        # Width is achievable if it's a multiple of any available increment
        for inc in increments:
            if abs(w / inc - round(w / inc)) < 0.01:
                available.append(w)
                break

    # Exclude $0.50 widths
    if NO_HALF_DOLLAR_WIDTHS:
        available = [w for w in available if abs(w - 0.50) > 0.01]

    return available


# ─────────────────────────────────────────────────────────
# CHAIN DATA BUILDER
# ─────────────────────────────────────────────────────────

def build_call_quotes(contracts: List[Dict], spot: float) -> Dict[float, Dict]:
    """
    Build a strike→quote lookup for ITM call options only.
    Filters to calls that are in the money (strike < spot).
    """
    quotes = {}
    for c in contracts:
        right = (c.get("right") or "").lower()
        if right != "call":
            continue

        strike = as_float(c.get("strike"), None)
        if strike is None or strike >= spot:
            continue  # OTM or ATM — skip

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

        # Leg-level warnings
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
    """
    Build all valid ITM bull call debit spread candidates.

    Rules enforced:
      - Both legs ITM (both strikes below spot)
      - Debit ≤ 70% of width
      - Debit ≥ 20% of width (sanity check)
      - Width must be from available_widths list
    """
    candidates = []
    itm_strikes = sorted([k for k in quotes.keys()], reverse=True)  # highest first (closest to ATM)

    if len(itm_strikes) < 2:
        return candidates

    for long_k in itm_strikes:
        long_q = quotes[long_k]

        for width in available_widths:
            short_k = round(long_k + width, 2)

            # Short strike must also be ITM (below spot)
            if short_k >= spot:
                continue

            short_q = quotes.get(short_k)
            if short_q is None:
                continue

            # Debit = long mid - short mid (you pay more for the deeper ITM leg)
            debit = round(long_q["mid"] - short_q["mid"], 4)

            if debit <= 0:
                continue

            # Cost filters
            cost_pct = debit / width
            if cost_pct > MAX_COST_PCT_OF_WIDTH:
                continue
            if cost_pct < MIN_COST_PCT_OF_WIDTH:
                continue

            # Compute metrics
            max_profit = round(width - debit, 4)
            max_loss = debit
            ror = round(max_profit / max_loss, 4) if max_loss > 0 else 0

            # Net greeks (long - short)
            net_theta = None
            net_vega = None
            if long_q.get("theta") is not None and short_q.get("theta") is not None:
                net_theta = round(long_q["theta"] - short_q["theta"], 4)
            if long_q.get("vega") is not None and short_q.get("vega") is not None:
                net_vega = round(long_q["vega"] - short_q["vega"], 4)

            # Exit targets
            same_day_target = round(debit * (1 + SAME_DAY_EXIT_PCT), 2)
            next_day_target = round(debit * (1 + NEXT_DAY_EXIT_PCT), 2)
            extended_target = round(debit * (1 + EXTENDED_HOLD_EXIT_PCT), 2)

            # Combine warnings from both legs
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
    """
    Rank spread candidates. Best spread = highest RoR with
    tightest width (prefer $1 > $2.50 > $5).

    Ranking score:
      - RoR (higher = better)
      - Width bonus (tighter = better)
      - Liquidity penalty (warnings = worse)
    """
    def score(c):
        ror = c.get("ror", 0)
        width = c.get("width", 5)

        # Prefer tighter widths: $1 gets +0.3, $2.50 gets +0.1, $5 gets 0
        width_bonus = 0.3 if width <= 1.0 else 0.1 if width <= 2.5 else 0

        # Penalty for warnings
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
    """
    Compute number of contracts based on risk rules.

    Returns (contracts, total_risk_dollars, sizing_note).
    """
    cost_per_contract = debit * 100

    if cost_per_contract <= 0:
        return 1, 0, "Could not compute sizing"

    # Max contracts from dollar limit
    max_from_dollars = int(MAX_RISK_PER_TRADE_USD / cost_per_contract)

    # Max contracts from account % limit
    max_from_pct = int((ACCOUNT_SIZE * MAX_RISK_PCT_ACCOUNT) / cost_per_contract)

    # Take the smaller of the two
    contracts = max(1, min(max_from_dollars, max_from_pct, MAX_CONTRACTS))

    # Apply tier multiplier
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
    """
    Compute stop loss level if applicable.
    Only high-volume tickers get stop losses.
    """
    if USE_STOP_LOSS_ALL or ticker.upper() in HIGH_VOLUME_TICKERS:
        stop_price = round(debit * (1 - STOP_LOSS_PCT), 2)
        return stop_price, f"Stop at ${stop_price:.2f} ({STOP_LOSS_PCT:.0%} loss)"
    else:
        return None, "No stop (low-vol ticker — manage manually)"


# ─────────────────────────────────────────────────────────
# CONFIDENCE SCORING
# ─────────────────────────────────────────────────────────

def compute_confidence(
    webhook_data: Dict,
    trade: Dict,
    has_earnings: bool = False,
    has_dividend: bool = False,
) -> Tuple[int, List[str]]:
    """
    Score trade confidence from 0-100 based on webhook signal data
    and trade quality metrics.
    """
    score = 30  # base score — signal fired so there's some confidence
    reasons = []

    # Signal tier
    tier = webhook_data.get("tier", "2")
    if tier == "1":
        score += CONFIDENCE_BOOSTS["tier_1"]
        reasons.append(f"T1 signal (+{CONFIDENCE_BOOSTS['tier_1']})")
    else:
        score += CONFIDENCE_BOOSTS["tier_2"]
        reasons.append(f"T2 signal (+{CONFIDENCE_BOOSTS['tier_2']})")

    # Trend confirmation
    if webhook_data.get("htf_confirmed"):
        score += CONFIDENCE_BOOSTS["htf_confirmed"]
        reasons.append("1H trend confirmed")
    elif webhook_data.get("htf_converging"):
        score += CONFIDENCE_BOOSTS["htf_converging"]
        reasons.append("1H trend converging")
    else:
        score += CONFIDENCE_PENALTIES["htf_diverging"]
        reasons.append("1H trend diverging")

    # Daily trend
    if webhook_data.get("daily_bull"):
        score += CONFIDENCE_BOOSTS["daily_bull"]
        reasons.append("Daily trend bullish")
    else:
        score += CONFIDENCE_PENALTIES.get("daily_bear", 0)
        reasons.append("Daily trend bearish")

    # RSI+MFI
    if webhook_data.get("rsi_mfi_bull"):
        score += CONFIDENCE_BOOSTS["rsi_mfi_bull"]
        reasons.append("RSI+MFI buying")

    # VWAP
    if webhook_data.get("above_vwap"):
        score += CONFIDENCE_BOOSTS["above_vwap"]
        reasons.append("Above VWAP")

    # Wave trend zone
    wt2 = as_float(webhook_data.get("wt2"), 0)
    if wt2 < -30:
        score += CONFIDENCE_BOOSTS["wave_oversold"]
        reasons.append("Wave oversold")
    elif wt2 > 60:
        score += CONFIDENCE_PENALTIES["wave_overbought"]
        reasons.append("Wave overbought")

    # Trade quality
    ror = trade.get("ror", 0)
    if ror >= 0.50:
        score += 5
        reasons.append(f"Strong RoR ({ror:.2f})")

    # Liquidity warnings
    warn_count = len(trade.get("warnings", []))
    if warn_count:
        score += warn_count * CONFIDENCE_PENALTIES.get("low_oi", -5)
        reasons.append(f"{warn_count} liquidity warning(s)")

    # Deal-breakers
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
) -> Dict[str, Any]:
    """
    Main entry point. Given a ticker's option chain and signal data,
    returns a complete trade recommendation or rejection with reason.

    This enforces ALL of Brad's trading rules.
    """
    webhook_data = webhook_data or {}
    result = {"ok": False, "ticker": ticker, "spot": spot, "dte": dte, "exp": expiration}

    # ── Rule: Direction must be bull ──
    bias = webhook_data.get("bias", "bull")
    if bias != "bull":
        result["reason"] = f"Direction '{bias}' not allowed — bull only"
        return result

    # ── Rule: DTE must be 1-5 ──
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

    # ── Build width ladder (top candidate per width) ──
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
    confidence, conf_reasons = compute_confidence(
        webhook_data, best, has_earnings, has_dividend
    )

    if confidence < MIN_CONFIDENCE_TO_TRADE:
        result["reason"] = f"Confidence {confidence}/100 below {MIN_CONFIDENCE_TO_TRADE} threshold"
        result["confidence"] = confidence
        result["conf_reasons"] = conf_reasons
        return result

    # ── Build exit targets for position ──
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
    }


# ─────────────────────────────────────────────────────────
# TRADE CARD FORMATTER (Telegram message)
# ─────────────────────────────────────────────────────────

def format_trade_card(rec: Dict) -> str:
    """
    Format a trade recommendation into a clean Telegram message.
    """
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
        "",
    ]

    # Trade details
    lines += [
        f"Long:  ${trade['long']} (${trade['long_itm']:.2f} ITM)",
        f"Short: ${trade['short']} (${trade['short_itm']:.2f} ITM)",
        f"Width: ${trade['width']:.2f} | Cost: ${trade['debit']:.2f} ({trade['cost_pct']:.0f}%)",
        f"Max Profit: ${trade['max_profit']:.2f} | RoR: {trade['ror']:.0%}",
        "",
    ]

    # Greeks if available
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

    # Confidence breakdown (first 3 reasons)
    if rec.get("conf_reasons"):
        lines.append("🧠 " + " | ".join(rec["conf_reasons"][:3]))
        lines.append("")

    lines.append("— Not financial advice —")
    return "\n".join(lines)
