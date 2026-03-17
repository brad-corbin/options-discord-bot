# institutional_flow.py
# ═══════════════════════════════════════════════════════════════════
# Institutional Composite Market Model — ADV-Normalized CAGF
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Five-component directional probability model:
#
#   G — Gamma regime       (spot vs flip, GEX sign)        weight 0.35
#   C — Charm flow         (time-decay hedging / ADV)      weight 0.25
#   V — Vanna flow         (IV-change hedging / ADV)       weight 0.20
#   L — Liquidity pressure (total dealer flow / ADV)       weight 0.10
#   M — Momentum confirm   (EMA trend or price position)   weight 0.10
#
#   Score = w1*G + w2*C + w3*V + w4*L + w5*M
#   Probability = 1 / (1 + e^(-Score))   ← logistic transform
#
# All flow components are normalized against Average Daily Volume
# so signals scale correctly across different market conditions.
#
# Plus a DTE recommendation engine that uses the flow model to
# determine optimal expiration (0DTE vs 1-3DTE vs 3-5DTE).
#
# Usage:
#   from institutional_flow import compute_cagf, recommend_dte
#   cagf = compute_cagf(dealer_flows, iv, rv, spot, vix, adv=adv)
#   dte_rec = recommend_dte(cagf, iv, vix, session_progress)
# ═══════════════════════════════════════════════════════════════════

import math
import logging
from typing import Dict, Optional, List

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# MODEL WEIGHTS (from institutional desk calibration)
# ─────────────────────────────────────────────────────────

W_GAMMA     = 0.35    # gamma regime — dominant factor
W_CHARM     = 0.25    # charm flow — strongest hedging force
W_VANNA     = 0.20    # vanna flow — requires IV change
W_LIQUIDITY = 0.10    # liquidity pressure — flow vs volume
W_MOMENTUM  = 0.10    # momentum confirmation — trend filter

# Logistic scaling factor (controls sigmoid steepness)
# Higher = sharper transition from 50% to extremes
LOGISTIC_K  = 2.5

# Default ADV for SPY/QQQ when not provided ($B)
DEFAULT_ADV_SPY = 30e9    # ~$30B daily dollar volume
DEFAULT_ADV_QQQ = 15e9    # ~$15B daily dollar volume


# ─────────────────────────────────────────────────────────
# CAGF COMPUTATION — ADV-Normalized
# ─────────────────────────────────────────────────────────

def compute_cagf(
    dealer_flows: dict,
    iv: float,
    rv: float,
    spot: float,
    vix: float = 20.0,
    session_progress: float = 0.5,
    adv: float = None,
    candle_closes: List[float] = None,
    ticker: str = "SPY",
) -> Dict:
    """
    Compute the Institutional Composite Market Model.

    Five-component ADV-normalized directional score → logistic probability.

    Args:
        dealer_flows: dict with gex, dex, vanna, charm, gamma_flip
        iv: current ATM implied volatility (decimal, e.g. 0.18)
        rv: realized volatility (decimal)
        spot: current spot price
        vix: VIX level
        session_progress: 0.0 (open) to 1.0 (close)
        adv: average daily dollar volume (from _estimate_liquidity)
        candle_closes: recent daily closes for momentum calc (optional)
        ticker: ticker symbol for ADV defaults

    Returns dict with:
        score: float — raw composite score (unbounded)
        probability: float — logistic probability (0-100%)
        direction: STRONG UPSIDE / UPSIDE / NEUTRAL / DOWNSIDE / STRONG DOWNSIDE
        trend_day_probability: float (0-100%)
        components: dict with individual G, C, V, L, M scores
        vol_edge: float (RV - IV)
        regime: TRENDING / SUPPRESSING / NEUTRAL
        explanation: str
    """
    if not dealer_flows:
        return _empty_cagf("No dealer flow data")

    gex   = dealer_flows.get("gex", 0)
    dex   = dealer_flows.get("dex", 0)
    vanna = dealer_flows.get("vanna", 0)
    charm = dealer_flows.get("charm", 0)
    flip  = dealer_flows.get("gamma_flip") or dealer_flows.get("flip_price")

    # ── ADV: use provided value or default by ticker ──
    if adv is None or adv <= 0:
        t = ticker.upper()
        if t in ("QQQ", "NDX"):
            adv = DEFAULT_ADV_QQQ
        else:
            adv = DEFAULT_ADV_SPY
    # Convert GEX/charm/vanna from $M to $ for ADV normalization
    # (engine_bridge reports them in $M already)
    adv_m = adv / 1e6  # ADV in $M for consistent units

    # ═══════════════════════════════════════════════════════
    # COMPONENT 1: GAMMA REGIME (G) — normalized
    # G = (Spot - GammaFlip) / Spot
    # Range: roughly -0.03 to +0.03, scaled to -1..+1
    # ═══════════════════════════════════════════════════════

    if flip and spot > 0:
        g_raw = (spot - flip) / spot  # positive = above flip, negative = below
        # Scale: 1% distance from flip → full signal
        g_normalized = max(-1.0, min(1.0, g_raw * 100))
    elif gex != 0:
        # Fallback: use GEX/ADV as regime proxy
        gex_pct = (gex / adv_m) if adv_m > 0 else 0
        g_normalized = max(-1.0, min(1.0, gex_pct * 50))
    else:
        g_normalized = 0.0

    # ═══════════════════════════════════════════════════════
    # COMPONENT 2: CHARM FLOW (C) — ADV-normalized
    # C = CharmFlow / ADV
    # Positive charm = dealers buy, Negative = dealers sell
    # Charm strengthens as session progresses
    # ═══════════════════════════════════════════════════════

    if charm != 0 and adv_m > 0:
        c_raw = charm / adv_m
        # Scale to -1..+1 (typical charm/ADV ratio is small)
        c_normalized = max(-1.0, min(1.0, c_raw * 200))

        # Session-progress multiplier: charm strengthens into close
        # 0.6x at open → 1.4x at close
        charm_time_mult = 0.6 + session_progress * 0.8
        c_normalized *= charm_time_mult
        c_normalized = max(-1.0, min(1.0, c_normalized))
    else:
        c_normalized = 0.0

    # ═══════════════════════════════════════════════════════
    # COMPONENT 3: VANNA FLOW (V) — ADV-normalized
    # V = (Vanna × ΔIV) / ADV
    # Uses IV vs RV spread as proxy for IV direction
    # ═══════════════════════════════════════════════════════

    vol_edge = (rv - iv) if (iv > 0 and rv > 0) else 0.0

    if vanna != 0 and adv_m > 0:
        # IV direction inference from VRP
        if iv > 0 and rv > 0:
            iv_change_proxy = iv - rv  # positive = IV rich, likely falling
        else:
            iv_change_proxy = 0

        vanna_flow = vanna * iv_change_proxy
        v_raw = vanna_flow / adv_m
        # When IV is rich (positive iv_change_proxy) and vanna positive:
        # IV likely falls → dealers buy → positive flow
        # The sign naturally works out: vanna * (iv-rv) gives selling pressure
        # We want buying when IV falls, so we negate
        v_normalized = max(-1.0, min(1.0, -v_raw * 500))
    else:
        v_normalized = 0.0

    # ═══════════════════════════════════════════════════════
    # COMPONENT 4: LIQUIDITY PRESSURE (L)
    # L = TotalDealerFlow / ADV
    # How large is the hedge flow relative to available volume?
    # ═══════════════════════════════════════════════════════

    total_dealer_flow = abs(gex) + abs(charm) + abs(vanna)
    if total_dealer_flow > 0 and adv_m > 0:
        flow_ratio = total_dealer_flow / adv_m
        # Direction from DEX: negative DEX = dealers short = buying fuel
        dex_direction = -1 if dex < -0.25 else 1 if dex > 0.25 else 0
        # Combined: large flow + directional DEX = strong liquidity pressure
        l_normalized = max(-1.0, min(1.0, flow_ratio * 100 * (dex_direction if dex_direction != 0 else (1 if charm > 0 else -1))))
    else:
        l_normalized = 0.0

    # ═══════════════════════════════════════════════════════
    # COMPONENT 5: MOMENTUM CONFIRMATION (M)
    # M = (EMA21 - EMA55) / Price  or fallback to VIX position
    # ═══════════════════════════════════════════════════════

    m_normalized = 0.0
    if candle_closes and len(candle_closes) >= 55:
        ema21 = _ema(candle_closes, 21)
        ema55 = _ema(candle_closes, 55)
        if ema21 and ema55 and spot > 0:
            m_raw = (ema21 - ema55) / spot
            m_normalized = max(-1.0, min(1.0, m_raw * 200))
    elif candle_closes and len(candle_closes) >= 21:
        ema21 = _ema(candle_closes, 21)
        if ema21 and spot > 0:
            m_raw = (spot - ema21) / spot
            m_normalized = max(-1.0, min(1.0, m_raw * 100))

    # ═══════════════════════════════════════════════════════
    # COMPOSITE SCORE
    # ═══════════════════════════════════════════════════════

    # For directionality, invert gamma: negative gamma ENABLES trends
    # So when price is below flip (g_normalized < 0), that's a
    # "trending regime" which AMPLIFIES the charm/vanna direction.
    # We use gamma as a regime multiplier rather than direction.

    # Gamma regime multiplier: negative gamma amplifies, positive suppresses
    gamma_mult = 1.0 + max(0, -g_normalized) * 0.5  # 1.0 to 1.5x
    gamma_suppress = max(0, g_normalized) * 0.3       # 0 to 0.3 reduction

    # Direction comes from charm + vanna + liquidity + momentum
    directional_score = (
        W_CHARM * c_normalized +
        W_VANNA * v_normalized +
        W_LIQUIDITY * l_normalized +
        W_MOMENTUM * m_normalized
    )

    # Apply gamma regime: amplify in trending, suppress in positive
    regime_adjusted = directional_score * gamma_mult - gamma_suppress * abs(directional_score)

    # Add gamma's own directional contribution
    # Below flip = bearish gamma pressure, above = bullish
    raw_score = W_GAMMA * g_normalized + regime_adjusted

    # ═══════════════════════════════════════════════════════
    # LOGISTIC PROBABILITY TRANSFORM
    # P = 1 / (1 + e^(-k * score))
    # Maps score to 0-100% directional probability
    # ═══════════════════════════════════════════════════════

    probability = 1.0 / (1.0 + math.exp(-LOGISTIC_K * raw_score))
    probability_pct = round(probability * 100, 1)

    # ═══════════════════════════════════════════════════════
    # TREND DAY PROBABILITY
    # Based on alignment of all components
    # ═══════════════════════════════════════════════════════

    gamma_trending = g_normalized < -0.15
    charm_directional = abs(c_normalized) > 0.15
    vanna_directional = abs(v_normalized) > 0.1
    flow_aligned = (
        (c_normalized > 0 and v_normalized >= 0) or
        (c_normalized < 0 and v_normalized <= 0)
    )
    strong_flow = abs(total_dealer_flow / adv_m) > 0.005 if adv_m > 0 else False

    trend_factors = 0.0
    if gamma_trending:
        trend_factors += 0.40
    if charm_directional:
        trend_factors += 0.25
    if vanna_directional and flow_aligned:
        trend_factors += 0.20
    if 15 < vix < 30:
        trend_factors += 0.10
    if strong_flow:
        trend_factors += 0.05
    trend_day_prob = min(1.0, trend_factors)

    # ═══════════════════════════════════════════════════════
    # LABELS
    # ═══════════════════════════════════════════════════════

    if g_normalized < -0.15:
        regime = "TRENDING"
    elif g_normalized > 0.15:
        regime = "SUPPRESSING"
    else:
        regime = "NEUTRAL"

    if probability_pct >= 73:
        direction = "STRONG UPSIDE"
    elif probability_pct >= 58:
        direction = "UPSIDE"
    elif probability_pct <= 27:
        direction = "STRONG DOWNSIDE"
    elif probability_pct <= 42:
        direction = "DOWNSIDE"
    else:
        direction = "NEUTRAL"

    # Vol edge label
    if vol_edge > 0.03:
        vol_label = "CHEAP"
        vol_emoji = "🟢"
    elif vol_edge < -0.03:
        vol_label = "RICH"
        vol_emoji = "🔴"
    else:
        vol_label = "FAIR"
        vol_emoji = "⚪"

    # Strategy suggestion based on probability
    if probability_pct >= 70 or probability_pct <= 30:
        strategy = "DEBIT SPREAD"
        strat_emoji = "🎯"
    elif 55 <= probability_pct <= 70 or 30 <= probability_pct <= 45:
        strategy = "DIRECTIONAL"
        strat_emoji = "📊"
    else:
        strategy = "NEUTRAL / WAIT"
        strat_emoji = "↔️"

    # ═══════════════════════════════════════════════════════
    # EXPLANATION
    # ═══════════════════════════════════════════════════════

    parts = []
    if regime == "TRENDING":
        parts.append("neg gamma (amplifying)")
    elif regime == "SUPPRESSING":
        parts.append("pos gamma (suppressing)")

    if c_normalized > 0.15:
        parts.append("charm buying")
    elif c_normalized < -0.15:
        parts.append("charm selling")

    if v_normalized > 0.1:
        parts.append("vanna bullish")
    elif v_normalized < -0.1:
        parts.append("vanna bearish")

    if abs(l_normalized) > 0.1:
        parts.append(f"liq pressure {'buying' if l_normalized > 0 else 'selling'}")

    if abs(m_normalized) > 0.15:
        parts.append(f"momentum {'up' if m_normalized > 0 else 'down'}")

    if vol_edge > 0.03:
        parts.append(f"vol cheap ({rv*100:.0f}%RV > {iv*100:.0f}%IV)")
    elif vol_edge < -0.03:
        parts.append(f"vol rich ({iv*100:.0f}%IV > {rv*100:.0f}%RV)")

    explanation = " | ".join(parts) if parts else "insufficient data"

    return {
        "score": round(raw_score, 4),
        "probability": probability_pct,
        "direction": direction,
        "strategy": strategy,
        "strategy_emoji": strat_emoji,
        "trend_day_probability": round(trend_day_prob, 2),
        "components": {
            "G": round(g_normalized, 3),
            "C": round(c_normalized, 3),
            "V": round(v_normalized, 3),
            "L": round(l_normalized, 3),
            "M": round(m_normalized, 3),
        },
        "vol_edge": round(vol_edge, 4),
        "vol_label": vol_label,
        "vol_emoji": vol_emoji,
        "regime": regime,
        "adv_used": round(adv / 1e9, 1) if adv else 0,
        "explanation": explanation,
        # Preserve old keys for DTE engine compatibility
        "edge": round(raw_score, 3),
        "gamma_signal": round(g_normalized, 3),
        "charm_signal": round(c_normalized, 3),
        "vanna_signal": round(v_normalized, 3),
        "dex_signal": round(l_normalized, 3),
    }


def _empty_cagf(reason: str) -> Dict:
    return {
        "score": 0, "probability": 50.0, "direction": "NEUTRAL",
        "strategy": "NEUTRAL / WAIT", "strategy_emoji": "↔️",
        "trend_day_probability": 0,
        "components": {"G": 0, "C": 0, "V": 0, "L": 0, "M": 0},
        "vol_edge": 0, "vol_label": "UNKNOWN", "vol_emoji": "❓",
        "regime": "UNKNOWN", "adv_used": 0, "explanation": reason,
        "edge": 0, "gamma_signal": 0, "charm_signal": 0,
        "vanna_signal": 0, "dex_signal": 0,
    }


def _ema(closes: List[float], period: int) -> Optional[float]:
    """Compute EMA of the last N closes. Returns final value or None."""
    if not closes or len(closes) < period:
        return None
    mult = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema = (closes[i] - ema) * mult + ema
    return ema


# ─────────────────────────────────────────────────────────
# DTE RECOMMENDATION ENGINE
# ─────────────────────────────────────────────────────────

DTE_PROFILES = {
    "0DTE": {
        "dte": 0, "label": "0DTE", "emoji": "⚡",
        "description": "Maximum gamma exposure, fastest profit/loss",
        "requires_trending": True, "requires_direction": True,
        "min_trend_prob": 0.55, "max_vix": 35, "min_vix": 10,
        "charm_benefit": True, "theta_burn": "extreme",
    },
    "1DTE": {
        "dte": 1, "label": "1DTE", "emoji": "🔥",
        "description": "High gamma, overnight risk, charm plays",
        "requires_trending": True, "requires_direction": True,
        "min_trend_prob": 0.40, "max_vix": 35, "min_vix": 10,
        "charm_benefit": True, "theta_burn": "high",
    },
    "2-3DTE": {
        "dte": 3, "label": "2-3 DTE", "emoji": "📊",
        "description": "Balanced gamma/theta, allows follow-through",
        "requires_trending": False, "requires_direction": True,
        "min_trend_prob": 0.25, "max_vix": 40, "min_vix": 8,
        "charm_benefit": False, "theta_burn": "moderate",
    },
    "3-5DTE": {
        "dte": 5, "label": "3-5 DTE", "emoji": "📅",
        "description": "Lower gamma, wider range, thesis trades",
        "requires_trending": False, "requires_direction": False,
        "min_trend_prob": 0.0, "max_vix": 45, "min_vix": 0,
        "charm_benefit": False, "theta_burn": "low",
    },
}


def recommend_dte(
    cagf: Dict,
    iv: float,
    vix: float = 20.0,
    session_progress: float = 0.5,
    has_direction: bool = True,
) -> Dict:
    """
    Recommend optimal DTE based on the CAGF institutional flow model.

    Uses the probability output to determine conviction level:
    - >= 70% or <= 30% = strong conviction → shorter DTE
    - 55-70% or 30-45% = moderate → 2-3 DTE
    - 45-55% = neutral → 3-5 DTE or wait
    """
    prob = cagf.get("probability", 50)
    edge = cagf.get("edge", 0)
    abs_edge = abs(edge)
    trend_prob = cagf.get("trend_day_probability", 0)
    regime = cagf.get("regime", "UNKNOWN")
    direction = cagf.get("direction", "NEUTRAL")
    charm_sig = cagf.get("charm_signal", 0)
    vol_label = cagf.get("vol_label", "FAIR")

    # Conviction from probability (how far from 50%)
    conviction = abs(prob - 50) / 50  # 0 = neutral, 1 = max conviction

    recommendations = []
    avoid = []

    for key, profile in DTE_PROFILES.items():
        score = 0
        reasons = []
        blocked = False
        block_reason = ""

        if vix > profile["max_vix"]:
            blocked = True
            block_reason = f"VIX {vix:.0f} > {profile['max_vix']} max"
        if vix < profile["min_vix"]:
            blocked = True
            block_reason = f"VIX {vix:.0f} < {profile['min_vix']} min"
        if profile["requires_trending"] and regime != "TRENDING":
            blocked = True
            block_reason = f"requires negative gamma ({regime})"
        if profile["requires_direction"] and "NEUTRAL" in direction:
            blocked = True
            block_reason = f"requires directional signal ({direction})"
        if trend_prob < profile["min_trend_prob"]:
            blocked = True
            block_reason = f"trend prob {trend_prob:.0%} < {profile['min_trend_prob']:.0%}"

        if blocked:
            avoid.append(f"{profile['label']}: {block_reason}")
            continue

        # Conviction scoring
        if conviction >= 0.4:   # prob >= 70% or <= 30%
            score += 3
            reasons.append(f"high conviction ({prob:.0f}%)")
        elif conviction >= 0.15:
            score += 2
            reasons.append(f"moderate conviction ({prob:.0f}%)")
        else:
            score += 1

        if trend_prob >= 0.6:
            score += 2
            reasons.append(f"trend prob {trend_prob:.0%}")
        elif trend_prob >= 0.4:
            score += 1

        if profile["charm_benefit"] and abs(charm_sig) > 0.25:
            score += 2
            reasons.append(f"charm {'tail' if charm_sig > 0 else 'head'}wind")

        if vol_label == "CHEAP" and profile["dte"] <= 1:
            score += 1
            reasons.append("cheap vol → max gamma")
        elif vol_label == "RICH" and profile["dte"] >= 3:
            score += 1
            reasons.append("rich vol → less theta")

        if profile["dte"] == 0 and session_progress > 0.7:
            score -= 2
            reasons.append("late session")
        elif profile["dte"] == 0 and session_progress < 0.3:
            score += 1
            reasons.append("early session")

        if regime == "TRENDING" and profile["dte"] <= 1:
            score += 1
            reasons.append("trending → short DTE")
        elif regime == "SUPPRESSING" and profile["dte"] >= 3:
            score += 1
            reasons.append("suppressing → longer DTE")

        recommendations.append({
            "key": key, "profile": profile,
            "score": score, "reasons": reasons,
        })

    if not recommendations:
        return {
            "primary": {
                "label": "3-5 DTE", "dte": 5, "emoji": "📅",
                "score": 0, "reasoning": "No shorter DTE meets conditions",
                "theta_burn": "low",
            },
            "secondary": None, "avoid": avoid,
            "reasoning": "All aggressive DTEs blocked — default 3-5 DTE",
        }

    recommendations.sort(key=lambda r: r["score"], reverse=True)
    primary = recommendations[0]
    secondary = recommendations[1] if len(recommendations) > 1 else None

    primary_result = {
        "label": primary["profile"]["label"],
        "dte": primary["profile"]["dte"],
        "emoji": primary["profile"]["emoji"],
        "score": primary["score"],
        "reasoning": " | ".join(primary["reasons"]),
        "theta_burn": primary["profile"]["theta_burn"],
        "description": primary["profile"]["description"],
    }
    secondary_result = None
    if secondary:
        secondary_result = {
            "label": secondary["profile"]["label"],
            "dte": secondary["profile"]["dte"],
            "emoji": secondary["profile"]["emoji"],
            "score": secondary["score"],
            "reasoning": " | ".join(secondary["reasons"]),
        }

    reasoning_parts = [f"Best: {primary_result['label']} (score {primary['score']})"]
    if regime == "TRENDING":
        reasoning_parts.append("negative gamma")
    if conviction >= 0.3:
        reasoning_parts.append(f"conviction {prob:.0f}%")
    if trend_prob >= 0.5:
        reasoning_parts.append(f"trend day {trend_prob:.0%}")

    return {
        "primary": primary_result,
        "secondary": secondary_result,
        "avoid": avoid,
        "reasoning": " | ".join(reasoning_parts),
    }


# ─────────────────────────────────────────────────────────
# CARD FORMATTING HELPERS
# ─────────────────────────────────────────────────────────

def format_cagf_block(cagf: Dict) -> list:
    """Format CAGF for the EM card — returns list of lines."""
    if not cagf or cagf.get("regime") == "UNKNOWN":
        return []

    prob = cagf["probability"]
    direction = cagf["direction"]
    trend_prob = cagf["trend_day_probability"]
    regime = cagf["regime"]
    comp = cagf.get("components", {})

    # Probability bar: 0% (full bear) to 100% (full bull)
    bar_len = 10
    bar_pos = int(prob / 100 * bar_len)
    bar_pos = max(0, min(bar_len, bar_pos))
    bar = "▓" * bar_pos + "░" * (bar_len - bar_pos)

    regime_emoji = "⚡" if regime == "TRENDING" else "🧲" if regime == "SUPPRESSING" else "↔️"
    dir_emoji = "🟢" if "UPSIDE" in direction else "🔴" if "DOWNSIDE" in direction else "⚪"

    # Trend day indicator
    if trend_prob >= 0.60:
        trend_label = f"🔥 TREND DAY LIKELY ({trend_prob:.0%})"
    elif trend_prob >= 0.40:
        trend_label = f"📊 Trending possible ({trend_prob:.0%})"
    else:
        trend_label = f"↔️ Chop/range likely ({trend_prob:.0%})"

    lines = [
        "", "─" * 32,
        "🏛️ INSTITUTIONAL FLOW MODEL",
        f"  {regime_emoji} Regime: {regime}",
        f"  {dir_emoji} {direction}  —  {prob:.0f}% upside probability",
        f"  BEAR [{bar}] BULL",
        f"  {trend_label}",
        f"  {cagf['vol_emoji']} Vol: {cagf['vol_label']} (edge {cagf['vol_edge']:+.1%})",
        f"  {cagf['strategy_emoji']} Strategy: {cagf['strategy']}",
        f"  G={comp.get('G',0):+.2f} C={comp.get('C',0):+.2f} V={comp.get('V',0):+.2f} L={comp.get('L',0):+.2f} M={comp.get('M',0):+.2f}",
        f"  💡 {cagf['explanation']}",
    ]

    return lines


def format_dte_block(dte_rec: Dict) -> list:
    """Format DTE recommendation for the EM card."""
    if not dte_rec:
        return []

    primary = dte_rec["primary"]
    secondary = dte_rec.get("secondary")
    avoid = dte_rec.get("avoid", [])

    lines = [
        "", "─" * 32,
        "📆 DTE RECOMMENDATION",
        f"  {primary['emoji']} Best: {primary['label']}  (score {primary['score']})",
        f"     {primary.get('description', '')}",
        f"     Theta: {primary['theta_burn']}",
    ]
    if primary.get("reasoning"):
        lines.append(f"     Why: {primary['reasoning']}")
    if secondary:
        lines.append(f"  {secondary['emoji']} Alt:  {secondary['label']}  (score {secondary['score']})")
    if avoid:
        lines.append(f"  ❌ Avoid: {' | '.join(avoid[:2])}")

    return lines
