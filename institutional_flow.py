# institutional_flow.py
# ═══════════════════════════════════════════════════════════════════
# Charm-Adjusted Gamma Flow (CAGF) — Institutional Edge Model
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Combines three dealer hedging forces into a single directional
# forecast for SPY and QQQ:
#
#   1. Gamma Regime — are dealers amplifying or suppressing moves?
#   2. Charm Flow  — time-decay forces dealers to buy or sell
#   3. Vanna Flow  — IV changes force dealers to buy or sell
#
# Plus a DTE recommendation engine that uses the flow model to
# determine optimal expiration (0DTE vs 1-3DTE vs 3-5DTE).
#
# Usage:
#   from institutional_flow import compute_cagf, recommend_dte
#   cagf = compute_cagf(dealer_flows, iv, rv, spot, vix)
#   dte_rec = recommend_dte(cagf, iv, vix, session_progress)
# ═══════════════════════════════════════════════════════════════════

import math
import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# CAGF WEIGHTS — calibrated for SPY/QQQ
# ─────────────────────────────────────────────────────────

W_GAMMA = 0.50    # gamma regime dominates
W_CHARM = 0.30    # charm is the next strongest force
W_VANNA = 0.20    # vanna requires IV change to activate

# Threshold for directional conviction
EDGE_STRONG_THRESHOLD = 0.40    # strong directional signal
EDGE_MODERATE_THRESHOLD = 0.15  # moderate directional signal
EDGE_NEUTRAL_BAND = 0.10       # below this = no edge


# ─────────────────────────────────────────────────────────
# CAGF COMPUTATION
# ─────────────────────────────────────────────────────────

def compute_cagf(
    dealer_flows: dict,
    iv: float,
    rv: float,
    spot: float,
    vix: float = 20.0,
    session_progress: float = 0.5,
) -> Dict:
    """
    Compute Charm-Adjusted Gamma Flow — the combined institutional
    edge score from dealer hedging mechanics.

    Args:
        dealer_flows: dict with gex, dex, vanna, charm, gamma_flip from v4 engine
        iv: current ATM implied volatility (decimal, e.g. 0.18)
        rv: realized volatility (decimal)
        spot: current spot price
        vix: VIX level
        session_progress: 0.0 (open) to 1.0 (close)

    Returns dict with:
        edge: float (-1 to +1) — combined directional signal
        direction: UPSIDE / DOWNSIDE / NEUTRAL
        trend_day_probability: float (0-1)
        gamma_signal: float (-1 to +1)
        charm_signal: float (-1 to +1)
        vanna_signal: float (-1 to +1)
        vol_edge: float (RV - IV)
        regime: TRENDING / SUPPRESSING / NEUTRAL
        explanation: str — human-readable summary
    """
    if not dealer_flows:
        return _empty_cagf("No dealer flow data")

    gex = dealer_flows.get("gex", 0)
    dex = dealer_flows.get("dex", 0)
    vanna = dealer_flows.get("vanna", 0)
    charm = dealer_flows.get("charm", 0)
    flip = dealer_flows.get("gamma_flip") or dealer_flows.get("flip_price")

    # ═══════════════════════════════════════════════════════
    # COMPONENT 1: Gamma Regime Signal (-1 to +1)
    # ═══════════════════════════════════════════════════════
    #
    # Negative gamma = trending environment (dealers chase price)
    # Positive gamma = suppressing environment (dealers fade moves)
    # Flip price position adds conviction

    if flip and spot and spot > 0:
        flip_dist_pct = (spot - flip) / spot
        if spot > flip:
            # Above flip: positive gamma territory (suppressing)
            # Closer to flip = weaker suppression
            gamma_signal = min(1.0, max(0.1, flip_dist_pct * 20))
        else:
            # Below flip: negative gamma territory (trending)
            gamma_signal = max(-1.0, min(-0.1, flip_dist_pct * 20))
    elif gex != 0:
        # No flip found — use raw GEX sign
        gamma_signal = 1.0 if gex > 0 else -1.0
    else:
        gamma_signal = 0.0

    # GEX magnitude adds conviction (large negative GEX = very trending)
    gex_magnitude = abs(gex)
    if gex_magnitude > 5:       # > $5B
        gamma_signal *= 1.0
    elif gex_magnitude > 2:     # > $2B
        gamma_signal *= 0.8
    elif gex_magnitude > 0.5:   # > $500M
        gamma_signal *= 0.6
    else:
        gamma_signal *= 0.3     # weak signal

    gamma_signal = max(-1.0, min(1.0, gamma_signal))

    # ═══════════════════════════════════════════════════════
    # COMPONENT 2: Charm Flow Signal (-1 to +1)
    # ═══════════════════════════════════════════════════════
    #
    # Positive charm = dealers must buy (bullish drift)
    # Negative charm = dealers must sell (bearish drift)
    # Charm strengthens as session progresses (strongest into close)

    if charm != 0:
        # Normalize charm to -1 to +1 range
        # Typical charm values: -5M to +5M for SPY
        charm_normalized = max(-1.0, min(1.0, charm / 3.0))

        # Charm effect INCREASES as session progresses
        # At open: 50% weight. At close: 150% weight.
        charm_time_mult = 0.5 + session_progress * 1.0
        charm_signal = charm_normalized * charm_time_mult
        charm_signal = max(-1.0, min(1.0, charm_signal))
    else:
        charm_signal = 0.0

    # ═══════════════════════════════════════════════════════
    # COMPONENT 3: Vanna Flow Signal (-1 to +1)
    # ═══════════════════════════════════════════════════════
    #
    # When IV is falling: positive vanna → dealer buying
    # When IV is rising: positive vanna → dealer selling
    # We use IV vs RV as proxy for IV direction

    vol_edge = (rv - iv) if (iv > 0 and rv > 0) else 0

    if vanna != 0:
        # Normalize vanna
        vanna_normalized = max(-1.0, min(1.0, vanna / 3.0))

        # IV direction inference:
        # If IV > RV (expensive vol), IV likely to fall → vanna buying
        # If IV < RV (cheap vol), IV could rise → vanna selling
        if iv > 0 and rv > 0:
            if iv > rv * 1.05:
                # IV rich — likely to fall → positive vanna = buying
                iv_direction = 1.0
            elif iv < rv * 0.95:
                # IV cheap — could rise → positive vanna = selling
                iv_direction = -1.0
            else:
                iv_direction = 0.0
        else:
            iv_direction = 0.0

        vanna_signal = vanna_normalized * iv_direction
        vanna_signal = max(-1.0, min(1.0, vanna_signal))
    else:
        vanna_signal = 0.0

    # ═══════════════════════════════════════════════════════
    # COMBINED EDGE
    # ═══════════════════════════════════════════════════════

    # For DIRECTIONAL signal, we invert gamma because negative gamma
    # ENABLES trends — so a negative gamma_signal is bullish if
    # charm + vanna are positive (trend can run)
    gamma_trending = -gamma_signal  # flip: negative gamma → positive trending score

    # Charm and vanna provide the DIRECTION
    flow_direction = charm_signal + vanna_signal

    # DEX provides additional directional confirmation
    dex_signal = 0
    if dex < -1.0:
        dex_signal = 0.3   # dealers short delta = buying fuel
    elif dex < -0.25:
        dex_signal = 0.15
    elif dex > 1.0:
        dex_signal = -0.3  # dealers long delta = selling fuel
    elif dex > 0.25:
        dex_signal = -0.15

    # Combined: regime enables, flow directs
    raw_edge = (
        W_GAMMA * gamma_trending +
        W_CHARM * charm_signal +
        W_VANNA * vanna_signal +
        dex_signal * 0.15  # small DEX nudge
    )
    edge = max(-1.0, min(1.0, raw_edge))

    # ═══════════════════════════════════════════════════════
    # TREND DAY PROBABILITY
    # ═══════════════════════════════════════════════════════
    #
    # High when: negative gamma + aligned charm/vanna + VIX not extreme
    # The "all three align" condition from the institutional model

    gamma_trending_bool = gamma_signal < -0.2  # negative gamma regime
    charm_directional = abs(charm_signal) > 0.2
    vanna_directional = abs(vanna_signal) > 0.1
    flow_aligned = (charm_signal > 0 and vanna_signal >= 0) or (charm_signal < 0 and vanna_signal <= 0)

    trend_factors = 0
    if gamma_trending_bool:
        trend_factors += 0.40  # biggest factor
    if charm_directional:
        trend_factors += 0.25
    if vanna_directional and flow_aligned:
        trend_factors += 0.20
    if 15 < vix < 30:
        trend_factors += 0.10  # not crisis, not dead
    if abs(dex_signal) > 0.1:
        trend_factors += 0.05

    trend_day_prob = min(1.0, trend_factors)

    # ═══════════════════════════════════════════════════════
    # REGIME + DIRECTION LABELS
    # ═══════════════════════════════════════════════════════

    if gamma_signal < -0.2:
        regime = "TRENDING"
    elif gamma_signal > 0.2:
        regime = "SUPPRESSING"
    else:
        regime = "NEUTRAL"

    if edge >= EDGE_STRONG_THRESHOLD:
        direction = "STRONG UPSIDE"
    elif edge >= EDGE_MODERATE_THRESHOLD:
        direction = "UPSIDE"
    elif edge <= -EDGE_STRONG_THRESHOLD:
        direction = "STRONG DOWNSIDE"
    elif edge <= -EDGE_MODERATE_THRESHOLD:
        direction = "DOWNSIDE"
    else:
        direction = "NEUTRAL"

    # ═══════════════════════════════════════════════════════
    # VOL EDGE FOR SPREAD SELECTION
    # ═══════════════════════════════════════════════════════

    if vol_edge > 0.03:
        vol_label = "CHEAP"     # RV > IV → buy options
        vol_emoji = "🟢"
    elif vol_edge < -0.03:
        vol_label = "RICH"      # IV > RV → sell options
        vol_emoji = "🔴"
    else:
        vol_label = "FAIR"
        vol_emoji = "⚪"

    # ═══════════════════════════════════════════════════════
    # EXPLANATION
    # ═══════════════════════════════════════════════════════

    parts = []
    if regime == "TRENDING":
        parts.append(f"negative gamma (dealers amplify)")
    else:
        parts.append(f"positive gamma (dealers suppress)")

    if charm_signal > 0.2:
        parts.append("charm buying into close")
    elif charm_signal < -0.2:
        parts.append("charm selling into close")

    if vanna_signal > 0.1:
        parts.append("vanna supports upside")
    elif vanna_signal < -0.1:
        parts.append("vanna supports downside")

    if vol_edge > 0.03:
        parts.append(f"vol cheap (RV {rv*100:.0f}% > IV {iv*100:.0f}%)")
    elif vol_edge < -0.03:
        parts.append(f"vol rich (IV {iv*100:.0f}% > RV {rv*100:.0f}%)")

    explanation = " | ".join(parts) if parts else "insufficient data"

    return {
        "edge": round(edge, 3),
        "direction": direction,
        "trend_day_probability": round(trend_day_prob, 2),
        "gamma_signal": round(gamma_signal, 3),
        "charm_signal": round(charm_signal, 3),
        "vanna_signal": round(vanna_signal, 3),
        "dex_signal": round(dex_signal, 3),
        "vol_edge": round(vol_edge, 4),
        "vol_label": vol_label,
        "vol_emoji": vol_emoji,
        "regime": regime,
        "explanation": explanation,
    }


def _empty_cagf(reason: str) -> Dict:
    return {
        "edge": 0, "direction": "NEUTRAL",
        "trend_day_probability": 0, "gamma_signal": 0,
        "charm_signal": 0, "vanna_signal": 0, "dex_signal": 0,
        "vol_edge": 0, "vol_label": "UNKNOWN", "vol_emoji": "❓",
        "regime": "UNKNOWN", "explanation": reason,
    }


# ─────────────────────────────────────────────────────────
# DTE RECOMMENDATION ENGINE
# ─────────────────────────────────────────────────────────

# DTE profiles with expected behavior
DTE_PROFILES = {
    "0DTE": {
        "dte": 0,
        "label": "0DTE",
        "emoji": "⚡",
        "description": "Maximum gamma exposure, fastest profit/loss",
        "requires_trending": True,    # needs negative gamma
        "requires_direction": True,   # needs clear flow signal
        "min_trend_prob": 0.55,       # need high trend day prob
        "max_vix": 35,                # too dangerous above this
        "min_vix": 10,                # need some movement
        "charm_benefit": True,        # charm drift is same-day
        "theta_burn": "extreme",
    },
    "1DTE": {
        "dte": 1,
        "label": "1DTE",
        "emoji": "🔥",
        "description": "High gamma, overnight risk, charm plays",
        "requires_trending": True,
        "requires_direction": True,
        "min_trend_prob": 0.40,
        "max_vix": 35,
        "min_vix": 10,
        "charm_benefit": True,
        "theta_burn": "high",
    },
    "2-3DTE": {
        "dte": 3,
        "label": "2-3 DTE",
        "emoji": "📊",
        "description": "Balanced gamma/theta, allows follow-through",
        "requires_trending": False,   # works in either regime
        "requires_direction": True,
        "min_trend_prob": 0.25,
        "max_vix": 40,
        "min_vix": 8,
        "charm_benefit": False,       # charm effect diluted
        "theta_burn": "moderate",
    },
    "3-5DTE": {
        "dte": 5,
        "label": "3-5 DTE",
        "emoji": "📅",
        "description": "Lower gamma, wider range, thesis trades",
        "requires_trending": False,
        "requires_direction": False,  # works even neutral
        "min_trend_prob": 0.0,
        "max_vix": 45,
        "min_vix": 0,
        "charm_benefit": False,
        "theta_burn": "low",
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

    The logic:
    - 0DTE: Only when negative gamma + strong flow signal + high trend prob
    - 1DTE: Negative gamma + moderate flow, or strong charm into close
    - 2-3DTE: Default when there's a directional edge but gamma is mixed
    - 3-5DTE: Fallback for weak/neutral signals or high VIX

    Returns dict with:
        primary: dict (best DTE recommendation)
        secondary: dict or None (alternative if conditions change)
        avoid: list of str (DTEs to avoid and why)
        reasoning: str
    """
    edge = cagf.get("edge", 0)
    abs_edge = abs(edge)
    trend_prob = cagf.get("trend_day_probability", 0)
    regime = cagf.get("regime", "UNKNOWN")
    direction = cagf.get("direction", "NEUTRAL")
    charm_sig = cagf.get("charm_signal", 0)
    vol_label = cagf.get("vol_label", "FAIR")

    recommendations = []
    avoid = []

    # ── Score each DTE profile ──
    for key, profile in DTE_PROFILES.items():
        score = 0
        reasons = []
        blocked = False
        block_reason = ""

        # VIX gates
        if vix > profile["max_vix"]:
            blocked = True
            block_reason = f"VIX {vix:.0f} > {profile['max_vix']} max"
        if vix < profile["min_vix"]:
            blocked = True
            block_reason = f"VIX {vix:.0f} < {profile['min_vix']} min"

        # Trending requirement
        if profile["requires_trending"] and regime != "TRENDING":
            blocked = True
            block_reason = f"requires negative gamma (current: {regime})"

        # Direction requirement
        if profile["requires_direction"] and "NEUTRAL" in direction:
            blocked = True
            block_reason = f"requires directional signal (current: {direction})"

        # Trend probability gate
        if trend_prob < profile["min_trend_prob"]:
            blocked = True
            block_reason = f"trend prob {trend_prob:.0%} < {profile['min_trend_prob']:.0%} min"

        if blocked:
            avoid.append(f"{profile['label']}: {block_reason}")
            continue

        # ── Scoring ──

        # Edge strength
        if abs_edge >= EDGE_STRONG_THRESHOLD:
            score += 3
            reasons.append("strong edge")
        elif abs_edge >= EDGE_MODERATE_THRESHOLD:
            score += 2
            reasons.append("moderate edge")
        else:
            score += 1

        # Trend day alignment
        if trend_prob >= 0.6:
            score += 2
            reasons.append(f"high trend prob ({trend_prob:.0%})")
        elif trend_prob >= 0.4:
            score += 1

        # Charm benefit (0DTE/1DTE get bonus when charm is strong)
        if profile["charm_benefit"] and abs(charm_sig) > 0.3:
            score += 2
            reasons.append(f"charm {'tailwind' if charm_sig > 0 else 'headwind'}")

        # Vol edge: cheap vol favors shorter DTE (more gamma exposure)
        if vol_label == "CHEAP" and profile["dte"] <= 1:
            score += 1
            reasons.append("cheap vol → max gamma")
        elif vol_label == "RICH" and profile["dte"] >= 3:
            score += 1
            reasons.append("rich vol → less theta burn")

        # Session progress: late in day disfavors 0DTE
        if profile["dte"] == 0 and session_progress > 0.7:
            score -= 2
            reasons.append("late session → limited time")
        elif profile["dte"] == 0 and session_progress < 0.3:
            score += 1
            reasons.append("early session → full day ahead")

        # Gamma regime bonus
        if regime == "TRENDING" and profile["dte"] <= 1:
            score += 1
            reasons.append("trending regime → short DTE")
        elif regime == "SUPPRESSING" and profile["dte"] >= 3:
            score += 1
            reasons.append("suppressing regime → longer DTE")

        recommendations.append({
            "key": key,
            "profile": profile,
            "score": score,
            "reasons": reasons,
        })

    if not recommendations:
        return {
            "primary": {
                "label": "3-5 DTE",
                "dte": 5,
                "emoji": "📅",
                "score": 0,
                "reasoning": "No shorter DTE meets conditions",
                "theta_burn": "low",
            },
            "secondary": None,
            "avoid": avoid,
            "reasoning": "All aggressive DTEs blocked — default to 3-5 DTE for safety",
        }

    # Sort by score descending
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

    # Build overall reasoning
    reasoning_parts = [f"Best: {primary_result['label']} (score {primary['score']})"]
    if regime == "TRENDING":
        reasoning_parts.append("negative gamma enables directional trades")
    if trend_prob >= 0.5:
        reasoning_parts.append(f"trend day likely ({trend_prob:.0%})")
    if abs(charm_sig) > 0.3:
        charm_dir = "bullish" if charm_sig > 0 else "bearish"
        reasoning_parts.append(f"charm {charm_dir} drift into close")

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

    edge = cagf["edge"]
    direction = cagf["direction"]
    trend_prob = cagf["trend_day_probability"]
    regime = cagf["regime"]

    # Edge bar visualization
    bar_len = 10
    edge_pos = int((edge + 1) / 2 * bar_len)  # map -1..+1 to 0..10
    edge_pos = max(0, min(bar_len, edge_pos))
    bar = "▓" * edge_pos + "░" * (bar_len - edge_pos)

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
        "🏛️ INSTITUTIONAL FLOW (CAGF)",
        f"  {regime_emoji} Regime: {regime}  |  Edge: {edge:+.2f}",
        f"  {dir_emoji} Direction: {direction}",
        f"  BEAR [{bar}] BULL",
        f"  {trend_label}",
        f"  {cagf['vol_emoji']} Vol: {cagf['vol_label']} (edge {cagf['vol_edge']:+.1%})",
        f"  💡 {cagf['explanation']}",
    ]

    return lines


def format_dte_block(dte_rec: Dict) -> list:
    """Format DTE recommendation for the EM card — returns list of lines."""
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
        f"     Theta burn: {primary['theta_burn']}",
    ]

    if primary.get("reasoning"):
        lines.append(f"     Why: {primary['reasoning']}")

    if secondary:
        lines.append(f"  {secondary['emoji']} Alt:  {secondary['label']}  (score {secondary['score']})")

    if avoid:
        lines.append(f"  ❌ Avoid: {' | '.join(avoid[:2])}")

    return lines
