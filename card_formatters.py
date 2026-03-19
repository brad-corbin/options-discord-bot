# card_formatters.py
# ═══════════════════════════════════════════════════════════════════
# v4.3 Card Formatters — Plain English + Decision Card + Regime Gate
#
# Three output modes for EM/trade cards:
#   1. "full"      — Detailed institutional card (existing, with label fixes)
#   2. "plain"     — Plain English translation for readability
#   3. "decision"  — 6-line trader decision card
#
# Also provides:
#   - resolve_unified_regime(): single canonical regime from all sources
#   - regime_gate(): hard gate for trade card generation
#
# Usage in app.py:
#   from card_formatters import (
#       format_plain_english_card,
#       format_decision_card,
#       resolve_unified_regime,
#       regime_gate,
#   )
# ═══════════════════════════════════════════════════════════════════

import math
import logging
from typing import Dict, Optional, List, Tuple

from unified_models import resolve_canonical_dealer_regime

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# UNIFIED REGIME RESOLVER
# Merges raw GEX regime + flip-based institutional regime
# into one canonical label so the card never contradicts itself.
# ─────────────────────────────────────────────────────────

def resolve_unified_regime(
    eng: dict,
    cagf: Optional[dict] = None,
    spot: float = 0,
) -> dict:
    """Backward-compatible wrapper around the shared dealer regime resolver."""
    return resolve_canonical_dealer_regime(eng=eng, cagf=cagf, spot=spot)


# ─────────────────────────────────────────────────────────
# REGIME GATE
# Hard gate: should a trade be generated at all?
# ─────────────────────────────────────────────────────────

def regime_gate(
    regime: dict,
    bias: dict,
    cagf: Optional[dict] = None,
    v4_result: Optional[dict] = None,
    dte_rec: Optional[dict] = None,
) -> Tuple[bool, str]:
    """
    Determine if the current regime allows a trade.

    Returns (allowed: bool, reason: str).
    If not allowed, reason explains why.
    """
    label = regime.get("label", "UNKNOWN")
    direction = bias.get("direction", "NEUTRAL")
    score = bias.get("score", 0)
    is_bull = "BULL" in direction

    # ── Gate 1: No directional signal at all ──
    if direction == "NEUTRAL":
        return False, "No directional edge (bias NEUTRAL)"

    # ── Gate 2: Data quality too low ──
    if v4_result and v4_result.get("confidence", {}).get("label") == "LOW":
        return False, "Data quality LOW — insufficient for trade"

    # ── Gate 3: Regime vs strategy mismatch ──
    if label == "SUPPRESSING" and not regime.get("allows_debit_spreads", True):
        if abs(score) < 5:
            return False, (
                f"Regime is SUPPRESSING and bias score only {score:+d}/14. "
                "Debit spreads need stronger edge in suppressing environments."
            )

    # ── Gate 4: CAGF probability opposes bias ──
    if cagf and cagf.get("regime") != "UNKNOWN":
        prob = cagf.get("probability", 50)
        if is_bull and prob < 35:
            return False, f"CAGF strongly bearish ({prob:.0f}% upside) — conflicts with bull bias"
        if not is_bull and prob > 65:
            return False, f"CAGF strongly bullish ({prob:.0f}% upside) — conflicts with bear bias"

    # ── Gate 5: VIX extreme (redundant with existing G4 but explicit) ──
    # Handled in _post_trade_card directly

    return True, "Regime gate passed"


# ─────────────────────────────────────────────────────────
# PLAIN ENGLISH HELPERS
# ─────────────────────────────────────────────────────────

def _plain_direction(direction: str, score: int) -> str:
    """Convert direction + score to plain English."""
    d = direction.upper()
    if "STRONG BULL" in d:
        return "strongly bullish"
    elif "BULL" in d and "SLIGHT" in d:
        return "slightly bullish (low conviction)"
    elif "BULL" in d:
        return "bullish"
    elif "STRONG BEAR" in d:
        return "strongly bearish"
    elif "BEAR" in d and "SLIGHT" in d:
        return "slightly bearish (low conviction)"
    elif "BEAR" in d:
        return "bearish"
    return "neutral — no clear direction"


def _plain_dealer_pressure(eng: dict, spot: float) -> List[str]:
    """Translate dealer flow into plain sentences."""
    if not eng:
        return ["Dealer positioning data not available."]

    lines = []
    dex = eng.get("dex", 0)
    charm_m = eng.get("charm", 0)
    vanna_m = eng.get("vanna", 0)

    if dex < -0.25:
        lines.append(
            "Dealers are short shares — if price rises, "
            "they must buy to hedge, which adds buying pressure."
        )
    elif dex > 0.25:
        lines.append(
            "Dealers are long shares — if price rises, "
            "they may sell to rebalance, which can cap rallies."
        )

    if charm_m > 0:
        lines.append(
            "Time decay is working in favor of upward drift, "
            "especially into the afternoon."
        )
    elif charm_m < 0:
        lines.append(
            "Time decay adds selling pressure as the day progresses."
        )

    if vanna_m < 0:
        lines.append(
            "If volatility spikes, dealers will need to sell, "
            "which could push price down."
        )
    elif vanna_m > 0:
        lines.append(
            "If volatility rises, dealer hedging adds buying support."
        )

    return lines if lines else ["Dealer flows are neutral — no strong pressure either way."]


def _plain_regime(regime: dict) -> str:
    """One sentence about the market regime."""
    return regime.get("description", "Market regime is unclear.")


def _plain_vol(iv: float, v4_result: dict) -> str:
    """Plain vol assessment."""
    iv_pct = iv * 100
    vr = v4_result.get("vol_regime", {}) if v4_result else {}
    rv20 = vr.get("realized_vol_20d")

    if rv20 and rv20 > 0:
        rv_pct = rv20 * 100
        spread = iv_pct - rv_pct
        if spread > 3:
            return (
                f"Options are somewhat expensive "
                f"(IV {iv_pct:.0f}% vs actual movement {rv_pct:.0f}%). "
                "Premium sellers have a slight edge."
            )
        elif spread < -3:
            return (
                f"Options are cheap relative to recent movement "
                f"(IV {iv_pct:.0f}% vs actual {rv_pct:.0f}%). "
                "Buying premium may be favorable."
            )
        return f"Options are fairly priced (IV {iv_pct:.0f}%, close to recent movement {rv_pct:.0f}%)."
    if iv_pct < 15:
        return "Volatility is low — expect tight ranges."
    elif iv_pct < 25:
        return "Volatility is moderate — normal trading conditions."
    elif iv_pct < 40:
        return "Volatility is elevated — wider stops, smaller size."
    return "Volatility is extreme — trade very small or sit out."


def _plain_pcr(pcr: dict) -> str:
    """Plain put/call ratio."""
    if not pcr:
        return ""
    pcr_oi = pcr.get("pcr_oi")
    if pcr_oi is None:
        return ""
    if pcr_oi > 1.3:
        return (
            f"Traders are heavily buying puts (PCR {pcr_oi:.2f}), "
            "showing defensive positioning. This can fuel a squeeze if price rises."
        )
    elif pcr_oi > 1.0:
        return f"Slightly more puts than calls (PCR {pcr_oi:.2f}) — mild caution."
    elif pcr_oi < 0.7:
        return f"Traders are aggressively buying calls (PCR {pcr_oi:.2f}) — bullish sentiment."
    return f"Put/call ratio is balanced (PCR {pcr_oi:.2f})."


def _plain_charm_timing(charm_m: float, is_next_day: bool, effective_dte: int) -> str:
    """Context-appropriate charm timing note."""
    if is_next_day or effective_dte > 0:
        if charm_m > 0:
            return (
                "Charm is supportive — time decay works in favor of upside, "
                "especially during afternoon sessions."
            )
        elif charm_m < 0:
            return "Charm is a headwind — time decay adds selling pressure during the session."
        return "Charm is neutral — no strong time-decay pressure."
    else:
        if charm_m > 0:
            return "Charm tailwind — hold into 2:30 PM CT for the afternoon drift."
        elif charm_m < 0:
            return "Charm headwind — do NOT hold into close. Exit by noon CT."
        return "Charm neutral."


# ─────────────────────────────────────────────────────────
# FORMAT: PLAIN ENGLISH EM CARD
# ─────────────────────────────────────────────────────────

def format_plain_english_card(
    ticker: str,
    spot: float,
    iv: float,
    em: dict,
    bias: dict,
    eng: dict,
    regime: dict,
    cagf: Optional[dict],
    dte_rec: Optional[dict],
    pcr: Optional[dict],
    v4_result: Optional[dict],
    session_label: str = "",
    target_date: str = "",
    is_next_day: bool = False,
) -> str:
    """
    Format the EM analysis as a plain-English readable card.
    """
    direction = bias.get("direction", "NEUTRAL")
    score = bias.get("score", 0)
    em_1sd = em.get("em_1sd", 0)
    bear_1sd = em.get("bear_1sd", 0)
    bull_1sd = em.get("bull_1sd", 0)
    bear_2sd = em.get("bear_2sd", 0)
    bull_2sd = em.get("bull_2sd", 0)

    lines = []

    # ── Header ──
    session_str = f" — {session_label}" if session_label else ""
    lines.append(f"📋 {ticker}{session_str}")
    lines.append(f"Price: ${spot:.2f}  |  IV: {iv*100:.1f}%")
    if target_date:
        lines.append(f"Looking at: {target_date}")
    lines.append("")

    # ── Vol context ──
    lines.append(_plain_vol(iv, v4_result))
    lines.append("")

    # ── Expected move ──
    lines.append("Expected Move")
    lines.append(f"  Normal range (68%):  ${bear_1sd:.2f} → ${bull_1sd:.2f}  (±${em_1sd:.2f})")
    lines.append(f"  Wide range (95%):    ${bear_2sd:.2f} → ${bull_2sd:.2f}")
    lines.append("")

    # ── Direction ──
    plain_dir = _plain_direction(direction, score)
    lines.append(f"Direction: {plain_dir}")
    lines.append("")

    # ── Dealer pressure ──
    lines.append("What Dealers Are Doing")
    for p in _plain_dealer_pressure(eng, spot):
        lines.append(f"  {p}")
    lines.append("")

    # ── Regime ──
    lines.append("Market Environment")
    lines.append(f"  {_plain_regime(regime)}")

    # ── CAGF probability ──
    if cagf and cagf.get("regime") != "UNKNOWN":
        prob = cagf.get("probability", 50)
        trend_prob = cagf.get("trend_day_probability", 0)
        if prob >= 60:
            lines.append(f"  Upside probability: {prob:.0f}% — market leans higher.")
        elif prob <= 40:
            lines.append(f"  Downside probability: {100-prob:.0f}% — market leans lower.")
        else:
            lines.append(f"  No strong directional probability ({prob:.0f}% upside).")

        if trend_prob >= 0.6:
            lines.append(f"  Trend day likely ({trend_prob:.0%}) — directional moves may accelerate.")
        elif trend_prob >= 0.4:
            lines.append(f"  Trend day possible ({trend_prob:.0%}).")
        else:
            lines.append(f"  Choppy/range day likely ({trend_prob:.0%} trend probability).")
    lines.append("")

    # ── Sentiment ──
    pcr_str = _plain_pcr(pcr)
    if pcr_str:
        lines.append("Sentiment")
        lines.append(f"  {pcr_str}")
        lines.append("")

    # ── DTE recommendation ──
    if dte_rec and dte_rec.get("primary"):
        p = dte_rec["primary"]
        lines.append(f"Best Trade Duration: {p['label']}")
        if p.get("reasoning"):
            lines.append(f"  {p['reasoning']}")
        avoid = dte_rec.get("avoid", [])
        if avoid:
            avoid_labels = [a.split(":")[0] for a in avoid[:2]]
            lines.append(f"  Avoid: {', '.join(avoid_labels)}")
        lines.append("")

    # ── Bottom line ──
    lines.append("Bottom Line")
    verdict = bias.get("verdict", "")
    if verdict:
        lines.append(f"  {verdict}")
    else:
        lines.append(f"  The market has a {plain_dir} lean.")
    lines.append("")
    lines.append("— Not financial advice —")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# FORMAT: 6-LINE TRADER DECISION CARD
# ─────────────────────────────────────────────────────────

def format_decision_card(
    ticker: str,
    spot: float,
    em: dict,
    bias: dict,
    eng: dict,
    regime: dict,
    cagf: Optional[dict],
    dte_rec: Optional[dict],
    v4_result: Optional[dict],
    spread_type: str = "",
    long_label: str = "",
    short_label: str = "",
    stop_level: float = 0,
    effective_dte_label: str = "",
    size_pct: int = 0,
    walls: Optional[dict] = None,
    expiry_label: str = "",
    est_cost: Optional[float] = None,
) -> str:
    """Decision-first Telegram card with the contract shown immediately."""
    direction = bias.get("direction", "NEUTRAL")
    score = bias.get("score", 0)
    conf_label = "?"
    conf_score = 0
    if v4_result:
        conf_label = v4_result.get("confidence", {}).get("label", "?")
        conf_score = v4_result.get("confidence", {}).get("composite", 0)

    em_1sd = em.get("em_1sd", 0)
    bear_1sd = em.get("bear_1sd", 0)
    bull_1sd = em.get("bull_1sd", 0)
    flip = (eng or {}).get("flip_price")
    dex = (eng or {}).get("dex", 0)
    plain_dir = _plain_direction(direction, score).split("(")[0].strip().title()

    reasons = []
    if dex < -0.25:
        reasons.append("dealers are short delta, so rallies can attract buying support")
    elif dex > 0.25:
        reasons.append("dealers are long delta, so pops can meet selling pressure")
    if regime.get("label") == "TRENDING":
        reasons.append("the current regime favors directional follow-through")
    elif regime.get("label") == "SUPPRESSING":
        reasons.append("price may move slower, so staying conservative matters")
    elif regime.get("label") == "MIXED":
        reasons.append("the setup has directional edge but still needs confirmation")
    if long_label and short_label:
        reasons.append("the spread is defined-risk and easier to manage than naked premium")
    if not reasons:
        reasons.append("the setup has a modest edge but still needs clean entry confirmation")

    if flip:
        if spot >= flip:
            flip_note = f"Gamma Flip: ${flip:.2f} — price is above it now. A clean break below can make moves less stable and weaken bullish holds."
        else:
            flip_note = f"Gamma Flip: ${flip:.2f} — price is below it now. Staying below can amplify pressure; reclaiming it can weaken bearish momentum."
    else:
        flip_note = "Gamma Flip: not available on this chain."

    contract_line = "No exact contract selected"
    if spread_type and long_label and short_label:
        exp_part = f" {expiry_label}" if expiry_label else ""
        contract_line = f"Buy {ticker}{exp_part} {long_label} / Sell {ticker}{exp_part} {short_label}"
    elif dte_rec and dte_rec.get("primary"):
        p = dte_rec["primary"]
        contract_line = f"Best fit today: {p['label']}"

    put_wall = (walls or {}).get("put_wall")
    call_wall = (walls or {}).get("call_wall")
    gamma_wall = (walls or {}).get("gamma_wall")
    dist_flip_pct = None
    if flip and spot:
        dist_flip_pct = abs((flip - spot) / spot) * 100

    lines = [
        f"📊 {ticker} — Trade Decision",
        f"🎯 Trade: {contract_line}",
        f"📈 Bias: {plain_dir} | 💪 Confidence: {conf_label} ({conf_score:.0%})",
        f"📐 Expected Move: ${bear_1sd:.2f} → ${bull_1sd:.2f} (±${em_1sd:.2f})" if em_1sd else "📐 Expected Move: unavailable",
    ]
    if est_cost is not None and est_cost > 0:
        lines.append(f"💵 Pricing: est. net debit ~${est_cost:.2f} per spread")
    lines.append(f"🧠 Why take it: {reasons[0]}.")
    if len(reasons) > 1:
        lines.append(f"💭 Why this contract: {reasons[1]}.")
    if flip:
        if spot >= flip:
            flip_line = f"Gamma Flip: ${flip:.2f} — price is above it now. A break below can weaken bullish holds and increase chop."
        else:
            flip_line = f"Gamma Flip: ${flip:.2f} — price is below it now. Staying below can amplify pressure; reclaiming it can weaken bearish momentum."
        if dist_flip_pct is not None and dist_flip_pct >= 2.5:
            side = "overhead" if flip > spot else "below"
            flip_line += f" It is fairly far {side}, so use it more as a regime line than a tight trigger."
        lines.append(f"☢️ Gamma Flip: " + flip_line.replace("Gamma Flip: ", ""))
    else:
        lines.append("☢️ Gamma Flip: not available on this chain.")

    if stop_level:
        lines.append(f"📋 Plan: entry near ${spot:.2f}; risk gets worse through ${stop_level:.2f}; size {size_pct}% of normal.")
    else:
        lines.append(f"📋 Plan: entry near ${spot:.2f}; size {size_pct}% of normal and wait for confirmation.")

    data_parts = []
    if put_wall is not None:
        data_parts.append(f"Put Wall ${put_wall:.2f}")
    if call_wall is not None:
        data_parts.append(f"Call Wall ${call_wall:.2f}")
    if gamma_wall is not None:
        data_parts.append(f"Gamma Wall ${gamma_wall:.2f}")
    if data_parts:
        lines.append("📦 Data: " + " | ".join(data_parts))

    lines += ["", "— Not financial advice —"]
    return "\n".join(lines)

