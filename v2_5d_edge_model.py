"""
v2_5d_edge_model.py — V2 Edge / Momentum review-card helper

Purpose
-------
Backtest-derived, review-only classifier for active-scanner signals.
This module does NOT open/register trades and does NOT replace V1.
It produces a second Telegram card to be posted directly underneath the
existing V1 legacy scanner card.

Phase 2.4 cleanup
-----------------
Separates three concepts that were previously blurred:
1) setup edge grade (A+ / A / B / SHADOW / BLOCK),
2) live vehicle quality (APPROVED / REJECTED / NOT_CHECKED), and
3) trade expression (5D structure vs momentum burst).

Safety
------
All outputs are REVIEW ONLY. The calling app must not register this as OPEN.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


MODEL_VERSION = "V2_5D_EDGE_MODEL"
MODEL_LABEL = "V2 5D EDGE MODEL"

HIST_WR_BY_GRADE = {
    "A+": 0.84,
    "A": 0.76,
    "B": 0.75,
    "SHADOW": 0.52,
    "BLOCK": 0.44,
}

CREDIT_TRADE_TYPES = {"bull_put", "bear_call", "credit", "iron_condor"}


@dataclass
class V2SetupResult:
    model_version: str = MODEL_VERSION
    label: str = MODEL_LABEL
    action: str = "STALK"           # REVIEW / STALK / NO TRADE
    setup_grade: str = "SHADOW"     # A+ / A / B / SHADOW / BLOCK
    setup_archetype: str = "UNCLASSIFIED"
    bias: str = "unknown"
    hold_window: str = "5 trading days"
    mtf_alignment: str = "unknown"
    historical_proxy_wr: float = 0.0
    preferred_structure: str = "review only"
    short_strike_target: str = "n/a"
    width_guidance: str = "n/a"
    max_debit_guidance: str = "rank by debit/width and liquidity"
    reason: str = ""
    block_reason: str = ""
    review_only_note: str = "REVIEW ONLY — not tracked as an open trade unless Brad confirms entry."

    # Phase 2.4: action-card cleanup fields
    vehicle_status: str = "NOT_CHECKED"        # APPROVED / REJECTED / NOT_CHECKED
    vehicle_reason: str = "No live spread candidate was checked."
    final_action: str = "STALK"                # REVIEW / STALK / NO TRADE / FIND BETTER VEHICLE / MOMENTUM REVIEW
    trade_expression: str = "5D_STRUCTURE"     # 5D_STRUCTURE / MOMENTUM_BURST / SHADOW / BLOCK
    momentum_burst_score: int = 0
    momentum_burst_label: str = "NO"           # YES / WATCH / NO
    momentum_burst_reasons: str = ""
    momentum_hold_window: str = "intraday to 1–2D if burst confirms; 5D only if reclaimed structure holds"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _s(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v).strip()


def _sl(v: Any, default: str = "") -> str:
    return _s(v, default).lower()


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _field(ctx: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(ctx, dict) and n in ctx and ctx.get(n) is not None:
            return ctx.get(n)
    return default


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _score_momentum_burst(context: Dict[str, Any], bias: str, mtf: str, pb_state: str, cb_side: str) -> Tuple[int, List[str]]:
    """Score whether the signal is a fast momentum/expansion opportunity.

    This is deliberately separate from the 5D setup grade. It should only
    label a quick-trade expression when live/current fields support it.
    """
    if bias != "bull":
        return 0, []

    score = 0
    reasons: List[str] = []

    recent_30 = abs(_f(_field(context, "recent_30m_move_pct", "move_30m_pct", default=0)))
    recent_60 = abs(_f(_field(context, "recent_60m_move_pct", "move_60m_pct", "recent_move_pct", default=0)))
    vol_ratio = _f(_field(context, "volume_ratio", "rel_volume", "relative_volume", default=0))
    adx = _f(_field(context, "adx", default=0))
    score_raw = _f(_field(context, "conviction_score", "score", default=0))
    above_vwap = _bool(_field(context, "above_vwap", default=False))
    setup_blob = " ".join(str(_field(context, k, default="")) for k in ("pb_wave_label", "wave_label", "setup_archetype")).lower()
    breakout_flag = any(x in setup_blob for x in ("break", "reclaim", "cb_reclaim"))
    reasons_blob = " ".join(str(x) for x in (_field(context, "conviction_reasons", default=[]) or [])).lower()
    breakdown = _field(context, "conviction_breakdown", default={}) or {}
    regime_blob = " ".join(str(_field(context, k, default="")) for k in ("market_regime", "regime", "v4_regime", "composite_regime")).lower()

    if recent_30 >= 0.75 or recent_60 >= 1.25:
        score += 2
        reasons.append(f"fast tape: recent move {max(recent_30, recent_60):.1f}%")
    if vol_ratio >= 1.5:
        score += 2
        reasons.append(f"volume expansion {vol_ratio:.1f}x")
    if pb_state in {"in_box", "above_roof", "post_box"} and (cb_side in {"below_cb", "at_cb"} or breakout_flag):
        score += 2
        reasons.append("reclaim/break from structure")
    if above_vwap:
        score += 1
        reasons.append("above VWAP")
    if mtf in {"full_aligned", "strong_aligned", "partial_aligned"}:
        score += 1
        reasons.append(f"MTF {mtf}")
    if adx >= 20:
        score += 1
        reasons.append(f"ADX {adx:.0f}+")
    if "explosive" in regime_blob or "strong trend" in regime_blob:
        score += 1
        reasons.append("strong/explosive regime")
    if "blue-sky" in reasons_blob or "above all resistance" in reasons_blob or bool(breakdown.get("B8")):
        score += 1
        reasons.append("blue-sky / above resistance")
    if score_raw >= 70:
        score += 1
        reasons.append(f"scorer {int(score_raw)}/100")

    return score, reasons[:5]


def classify_v2_setup(context: Dict[str, Any]) -> V2SetupResult:
    ticker = _s(_field(context, "ticker", "symbol", default=""), "").upper()
    bias = _sl(_field(context, "bias", "direction", "side", default="unknown"), "unknown")
    pb_state = _sl(_field(context, "pb_state", "potter_state", default="no_box"), "no_box")
    cb_side = _sl(_field(context, "cb_side", default="n/a"), "n/a")
    htf_status = _s(_field(context, "htf_status", default=""), "").upper()
    mtf_raw = _sl(_field(context, "mtf_alignment_label", "mtf_label", default=""), "")
    above_vwap = _bool(_field(context, "above_vwap", default=False))
    daily_bull = _bool(_field(context, "daily_bull", default=False))
    htf_confirmed = _bool(_field(context, "htf_confirmed", default=False)) or htf_status == "CONFIRMED"
    htf_converging = _bool(_field(context, "htf_converging", default=False)) or htf_status == "CONVERGING"

    if mtf_raw:
        mtf = mtf_raw
    elif htf_confirmed and daily_bull and above_vwap:
        mtf = "full_aligned"
    elif htf_confirmed and (daily_bull or above_vwap):
        mtf = "strong_aligned"
    elif htf_converging or daily_bull or above_vwap:
        mtf = "partial_aligned"
    else:
        mtf = "unknown"

    existing_archetype = _s(_field(context, "setup_archetype", default=""), "")
    existing_grade = _s(_field(context, "setup_grade", default=""), "")

    res = V2SetupResult(bias=bias, mtf_alignment=mtf)
    mb_score, mb_reasons = _score_momentum_burst(context, bias, mtf, pb_state, cb_side)
    # Cap display score at 10 so cards do not show impossible values like 11/10.
    # The raw thresholding still uses the uncapped score.
    res.momentum_burst_score = min(int(mb_score), 10)
    res.momentum_burst_label = "YES" if mb_score >= 6 else ("WATCH" if mb_score >= 4 else "NO")
    res.momentum_burst_reasons = " | ".join(mb_reasons)
    if mb_score >= 6:
        res.trade_expression = "MOMENTUM_BURST"
        res.final_action = "MOMENTUM REVIEW"
        res.hold_window = "intraday to 1–2D for momentum portion; 5D only if structure holds"

    if bias != "bull":
        res.action = "NO TRADE"
        res.final_action = "NO TRADE"
        res.trade_expression = "BLOCK"
        res.setup_grade = "BLOCK"
        res.setup_archetype = "BEAR_RESEARCH_ONLY"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["BLOCK"]
        res.block_reason = "Current tested V2 edge is bullish continuation only; broad bear scanner buckets underperformed."
        res.reason = res.block_reason
        return res

    if pb_state == "in_box" and cb_side == "above_cb":
        res.action = "NO TRADE"
        res.final_action = "NO TRADE"
        res.trade_expression = "BLOCK"
        res.setup_grade = "BLOCK"
        res.setup_archetype = "BULL_CHASE_IN_BOX_BLOCK"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["BLOCK"]
        res.block_reason = "Bull signal inside box but already above CB — historically a chase/late bucket."
        res.reason = res.block_reason
        return res

    if pb_state == "in_box" and cb_side in {"below_cb", "at_cb"}:
        res.setup_archetype = "PB_CB_RECLAIM_BULL_APPROVED"
        res.preferred_structure = "Call debit spread review; bull put spread may also fit structure if credit is meaningful."
        res.short_strike_target = "1.0%–1.5% ITM short strike preferred"
        res.width_guidance = "Scan available $1 / $2 / $2.50 / $5 / wider spreads; choose best debit/width edge, not fixed width."
        if mtf == "full_aligned":
            res.action = "REVIEW"
            res.final_action = res.final_action if res.trade_expression == "MOMENTUM_BURST" else "REVIEW"
            res.setup_grade = "A+"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["A+"]
            res.reason = "CB reclaim bull setup with full 15m/30m/60m/daily alignment — strongest tested 5D bucket."
            return res
        if mtf in {"strong_aligned", "partial_aligned"}:
            res.action = "REVIEW"
            res.final_action = res.final_action if res.trade_expression == "MOMENTUM_BURST" else "REVIEW"
            res.setup_grade = "A"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["A"]
            res.reason = "CB reclaim bull setup with supportive MTF alignment."
            return res
        res.action = "REVIEW"
        res.final_action = res.final_action if res.trade_expression == "MOMENTUM_BURST" else "REVIEW"
        res.setup_grade = "B"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["B"]
        res.reason = "Valid CB reclaim bull structure, but MTF alignment is weaker; review smaller/stricter."
        return res

    if pb_state in {"above_roof", "post_box"}:
        res.setup_archetype = "PB_BREAKOUT_BULL_APPROVED"
        res.preferred_structure = "Call debit spread / ITM call debit spread review."
        res.short_strike_target = "1.0%–1.5% ITM short strike preferred; 1.5% minimum for weaker grades."
        res.width_guidance = "Scan available widths and rank real spreads by debit/width edge cushion."
        if mtf == "strong_aligned":
            res.action = "REVIEW"
            res.final_action = res.final_action if res.trade_expression == "MOMENTUM_BURST" else "REVIEW"
            res.setup_grade = "A"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["A"]
            res.reason = "Breakout bull with strong MTF alignment."
            return res
        if mtf in {"full_aligned", "partial_aligned", "mixed"}:
            res.action = "REVIEW"
            res.final_action = res.final_action if res.trade_expression == "MOMENTUM_BURST" else "REVIEW"
            res.setup_grade = "B"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["B"]
            res.reason = "Breakout bull structure; MTF not optimal but historically positive as a continuation bucket."
            return res

    res.action = "STALK"
    res.final_action = res.final_action if res.trade_expression == "MOMENTUM_BURST" else "STALK"
    if res.trade_expression != "MOMENTUM_BURST":
        res.trade_expression = "SHADOW"
    res.setup_grade = existing_grade or "SHADOW"
    res.setup_archetype = existing_archetype or "UNCLASSIFIED"
    res.historical_proxy_wr = HIST_WR_BY_GRADE["SHADOW"]
    res.reason = "No proven V2 A/A+/B 5D pattern detected from available live fields. Log for proof-of-concept only."
    return res


def _is_credit_candidate(candidate: Dict[str, Any]) -> bool:
    if "is_credit" in candidate:
        return _bool(candidate.get("is_credit"))
    tt = _sl(candidate.get("trade_type") or candidate.get("strategy") or "", "")
    if tt in CREDIT_TRADE_TYPES:
        return True
    has_credit = any(candidate.get(k) for k in ("credit", "net_credit"))
    has_debit = any(candidate.get(k) for k in ("debit", "net_debit", "cost"))
    return has_credit and not has_debit


def _candidate_premium(candidate: Dict[str, Any], is_credit: bool) -> float:
    if is_credit:
        v = _field(candidate, "credit", "net_credit", default=0) or 0
    else:
        v = _field(candidate, "debit", "net_debit", "cost", default=0) or 0
    return _f(v, 0.0)


def breakeven_wr(premium: float, width: float, is_credit: bool = False) -> Optional[float]:
    try:
        premium = float(premium); width = float(width)
        if premium <= 0 or width <= 0:
            return None
        be = (width - premium) / width if is_credit else premium / width
        if be <= 0 or be >= 1:
            return None
        return be
    except Exception:
        return None


def edge_cushion(hist_wr: float, premium: float, width: float, is_credit: bool = False) -> Optional[float]:
    be = breakeven_wr(premium, width, is_credit=is_credit)
    if be is None:
        return None
    return hist_wr - be


def expected_value(width: float, premium: float, hist_wr: float, is_credit: bool = False) -> Optional[float]:
    try:
        width = float(width); premium = float(premium); hist_wr = float(hist_wr)
        if width <= 0 or premium <= 0 or hist_wr < 0 or hist_wr > 1:
            return None
        if is_credit:
            max_profit = premium
            max_loss = width - premium
        else:
            max_profit = width - premium
            max_loss = premium
        if max_profit <= 0 or max_loss <= 0:
            return None
        return (hist_wr * max_profit) - ((1.0 - hist_wr) * max_loss)
    except Exception:
        return None


def rank_spread_candidates(candidates: Iterable[Dict[str, Any]], historical_wr: float) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for c in candidates or []:
        width = _field(c, "width", "spread_width", default=0) or 0
        is_credit = _is_credit_candidate(c)
        premium = _candidate_premium(c, is_credit)
        be = breakeven_wr(premium, width, is_credit=is_credit)
        cushion = edge_cushion(historical_wr, premium, width, is_credit=is_credit)
        ev = expected_value(width, premium, historical_wr, is_credit=is_credit)
        out = dict(c)
        out["v2_is_credit"] = is_credit
        out["v2_premium"] = round(premium, 4) if premium else 0.0
        out["v2_hist_wr"] = round(historical_wr, 4)
        out["v2_breakeven_wr"] = round(be, 4) if be is not None else None
        out["v2_edge_cushion"] = round(cushion, 4) if cushion is not None else None
        out["v2_ev_proxy"] = round(ev, 4) if ev is not None else None
        ranked.append(out)
    ranked.sort(key=lambda x: (
        x.get("v2_edge_cushion") if x.get("v2_edge_cushion") is not None else -999,
        x.get("v2_ev_proxy") if x.get("v2_ev_proxy") is not None else -999,
    ), reverse=True)
    return ranked


def _vehicle_quality(candidate: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not candidate:
        return "NOT_CHECKED", "No live spread candidate was checked."
    is_credit = bool(candidate.get("v2_is_credit")) if "v2_is_credit" in candidate else _is_credit_candidate(candidate)
    width = _f(_field(candidate, "width", "spread_width", default=0))
    premium = _f(candidate.get("v2_premium") or _candidate_premium(candidate, is_credit))
    ev = candidate.get("v2_ev_proxy")
    cushion = candidate.get("v2_edge_cushion")
    try:
        ev_f = float(ev) if ev is not None else None
    except Exception:
        ev_f = None
    try:
        cushion_f = float(cushion) if cushion is not None else None
    except Exception:
        cushion_f = None
    if width <= 0 or premium <= 0:
        return "REJECTED", "Missing valid width or premium; cannot approve vehicle."
    if is_credit:
        roc = premium / max(width - premium, 0.01)
        if premium < 0.10:
            return "REJECTED", f"Credit ${premium:.2f} is too small for ${width:.2f} width."
        if roc < 0.06:
            return "REJECTED", f"Credit ROC {roc:.0%} is too low for the tail risk."
    else:
        debit_to_width = premium / width
        if debit_to_width > 0.75:
            return "REJECTED", f"Debit/width {debit_to_width:.0%} is too expensive."
    if cushion_f is not None and cushion_f <= 0:
        return "REJECTED", f"Historical WR does not clear breakeven; edge cushion {cushion_f:+.0%}."
    if ev_f is not None and ev_f <= 0:
        return "REJECTED", f"EV proxy is negative (${ev_f:.2f}/spread)."
    return "APPROVED", "Live vehicle math clears V2 edge filters."


def build_v2_card(result: V2SetupResult, ticker: str = "", spot: Optional[float] = None,
                  best_spread: Optional[Dict[str, Any]] = None,
                  alternatives: Optional[List[Dict[str, Any]]] = None) -> str:
    vehicle_status, vehicle_reason = _vehicle_quality(best_spread)
    result.vehicle_status = vehicle_status
    result.vehicle_reason = vehicle_reason

    if result.action == "NO TRADE" or result.setup_grade == "BLOCK":
        result.final_action = "NO TRADE"
        result.trade_expression = "BLOCK"
    elif vehicle_status == "REJECTED":
        result.final_action = "FIND BETTER VEHICLE"
    elif result.trade_expression == "MOMENTUM_BURST":
        result.final_action = "MOMENTUM REVIEW"
    elif result.setup_grade in {"A+", "A", "B"}:
        result.final_action = "REVIEW"
    else:
        result.final_action = "STALK"

    if result.trade_expression == "MOMENTUM_BURST":
        label = "V2 MOMENTUM BURST"
        header_emoji = "⚡"
    elif result.trade_expression == "BLOCK" or result.final_action == "NO TRADE":
        label = "V2 5D EDGE MODEL"
        header_emoji = "⛔"
    elif result.final_action == "FIND BETTER VEHICLE":
        label = "V2 SETUP VALID / VEHICLE REJECTED"
        header_emoji = "🟠"
    else:
        label = "V2 5D EDGE MODEL"
        header_emoji = "🟢" if result.setup_grade in {"A+", "A"} else ("🟡" if result.setup_grade == "B" else "🧪")

    title_ticker = ticker.upper() if ticker else "SIGNAL"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"{header_emoji} {label} — {result.setup_grade}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Ticker: {title_ticker}" + (f" @ ${float(spot):.2f}" if spot else ""),
        f"Final action: {result.final_action} / REVIEW ONLY",
        f"Setup grade: {result.setup_grade}",
        f"Setup: {result.setup_archetype}",
        f"Vehicle status: {vehicle_status}",
        f"Bias: {result.bias.upper()}",
        f"MTF: {result.mtf_alignment}",
        f"Hold window: {result.hold_window}",
        "",
        "Why this matters:",
        result.reason or result.block_reason or "No reason supplied.",
    ]

    if result.momentum_burst_label in {"YES", "WATCH"}:
        lines += [
            "",
            f"⚡ Momentum read: {result.momentum_burst_label} ({result.momentum_burst_score}/10)",
            f"Reasons: {result.momentum_burst_reasons or 'live momentum fields limited'}",
            f"Momentum hold: {result.momentum_hold_window}",
            "Vehicle note: long call / shares / quick debit may fit better than a slow 5D spread if the burst is active.",
        ]

    if result.action != "NO TRADE":
        lines += [
            "",
            "Preferred structure:",
            result.preferred_structure,
            f"Short strike target: {result.short_strike_target}",
            f"Width guidance: {result.width_guidance}",
            f"Historical proxy WR: {result.historical_proxy_wr:.0%}" if result.historical_proxy_wr else "Historical proxy WR: n/a",
        ]
        if best_spread:
            width = _field(best_spread, "width", "spread_width", default=None)
            long_strike = _field(best_spread, "long_strike", "long", default=None)
            short_strike = _field(best_spread, "short_strike", "short", default=None)
            is_credit = bool(best_spread.get("v2_is_credit")) if "v2_is_credit" in best_spread else _is_credit_candidate(best_spread)
            premium = best_spread.get("v2_premium") if best_spread.get("v2_premium") else _candidate_premium(best_spread, is_credit)
            premium_label = "Credit collected" if is_credit else "Debit paid"
            lines += [
                "",
                "Live vehicle check:",
                f"Status: {vehicle_status}",
                f"Reason: {vehicle_reason}",
                f"Long/Short: {long_strike} / {short_strike}",
                f"Width: ${float(width):.2f}" if width is not None else "Width: n/a",
                f"{premium_label}: ${float(premium):.2f}" if premium else f"{premium_label}: n/a",
            ]
            try:
                w = float(width) if width is not None else None
                p = float(premium) if premium else None
                if w and p and w > 0 and p > 0:
                    if is_credit:
                        max_profit = p
                        max_loss = max(0.0, w - p)
                    else:
                        max_profit = max(0.0, w - p)
                        max_loss = p
                    lines.append(f"Max profit / max loss: ${max_profit:.2f} / ${max_loss:.2f}")
            except Exception:
                pass
            if best_spread.get("v2_breakeven_wr") is not None:
                lines.append(f"Breakeven WR: {best_spread['v2_breakeven_wr']:.0%}")
            if best_spread.get("v2_edge_cushion") is not None:
                lines.append(f"Edge cushion: {best_spread['v2_edge_cushion']:+.0%}")
            if best_spread.get("v2_ev_proxy") is not None:
                lines.append(f"EV proxy: ${best_spread['v2_ev_proxy']:.2f} per spread")
        else:
            lines += ["", "Live vehicle check:", "Status: NOT_CHECKED", "Reason: no live candidate was passed into V2."]
    else:
        lines += ["", "Block reason:", result.block_reason or result.reason]

    lines += ["", result.review_only_note]
    return "\n".join(lines)


# ── v8.5 (V2 PEER restore): functions restored from Apr 27 baseline ────────
# Deleted between Apr 30 → current main while peer-mode call sites in app.py
# were also stripped. Restoring verbatim. The momentum_burst additions in
# build_v2_card above and classify_v2_setup remain unchanged.
def _format_premium_label(best_spread: Dict[str, Any]) -> Tuple[bool, float, str]:
    """Returns (is_credit, premium, label_word)."""
    is_credit = bool(best_spread.get("v2_is_credit")) if "v2_is_credit" in best_spread \
                else _is_credit_candidate(best_spread)
    premium_raw = best_spread.get("v2_premium")
    if premium_raw is None or premium_raw == 0:
        premium_raw = _candidate_premium(best_spread, is_credit)
    try:
        premium = float(premium_raw or 0)
    except Exception:
        premium = 0.0
    label = "collected" if is_credit else "paid"
    return is_credit, premium, label


def build_v2_inline_block(result: Optional[V2SetupResult],
                          best_spread: Optional[Dict[str, Any]] = None) -> str:
    """Compact V2 footer for inline use under a V1 card.

    Designed for ~4-7 lines, ~250-500 chars. Skips ticker/spot/bias because
    the V1 card above already shows them.
    """
    if result is None:
        return ""

    grade = (result.setup_grade or "").upper()
    grade_emoji = {"A+": "🟢", "A": "🟢", "B": "🟡", "SHADOW": "🧪", "BLOCK": "⛔"}.get(grade, "⚪")

    lines = ["─── V2 5D EDGE MODEL ───"]

    # Block / NO TRADE: keep it tight.
    if (result.action or "").upper() == "NO TRADE":
        lines.append(f"{grade_emoji} {grade}  {result.setup_archetype}")
        block_msg = result.block_reason or result.reason
        if block_msg:
            if len(block_msg) > 160:
                block_msg = block_msg[:157] + "..."
            lines.append(f"Reason: {block_msg}")
        lines.append("REVIEW ONLY.")
        return "\n".join(lines)

    # REVIEW / STALK header line
    hold = result.hold_window or "5 trading days"
    hold_short = hold.replace(" trading days", "d").replace(" trading day", "d")
    lines.append(
        f"{grade_emoji} {grade}  {result.setup_archetype} · MTF {result.mtf_alignment} · {hold_short} hold"
    )

    # WR + best spread on one line where possible
    wr_line = (
        f"Hist WR proxy: {result.historical_proxy_wr:.0%}"
        if result.historical_proxy_wr else "Hist WR: n/a"
    )
    if best_spread:
        is_credit, premium, label = _format_premium_label(best_spread)
        width = _field(best_spread, "width", "spread_width", default=None)
        long_s = _field(best_spread, "long_strike", "long", default=None)
        short_s = _field(best_spread, "short_strike", "short", default=None)
        try:
            if width is not None and premium > 0 and long_s is not None and short_s is not None:
                wr_line += (
                    f" · Best ${float(width):.0f}w {long_s}/{short_s}: ${premium:.2f} {label}"
                )
        except Exception:
            pass
    lines.append(wr_line)

    if best_spread:
        be = best_spread.get("v2_breakeven_wr")
        cushion = best_spread.get("v2_edge_cushion")
        ev = best_spread.get("v2_ev_proxy")
        metrics: List[str] = []
        if be is not None:
            metrics.append(f"BE WR {float(be):.0%}")
        if cushion is not None:
            metrics.append(f"Cushion {float(cushion):+.0%}")
        if ev is not None:
            metrics.append(f"EV ${float(ev):+.2f}/spread")
        if metrics:
            lines.append(" · ".join(metrics))

    if result.reason:
        r = result.reason
        if len(r) > 130:
            r = r[:127] + "..."
        lines.append(f"Why: {r}")

    lines.append(result.review_only_note)
    return "\n".join(lines)


def build_v2_orphan_card(result: Optional[V2SetupResult],
                         ticker: str = "",
                         spot: Optional[float] = None,
                         v1_status: str = "V1 silent") -> str:
    """Standalone V2 card for cases where V1 stays silent but V2 has a strong read.

    The header explicitly says (V1 SILENT) so it never gets confused with a
    regular V2-under-V1 followup card. Used by `_maybe_post_v2_orphan` in
    app.py at every V1 short-circuit point.
    """
    if result is None:
        return ""

    grade = (result.setup_grade or "").upper()
    # Distinct emoji from the inline block — orange = "V1 didn't see this"
    grade_emoji = {"A+": "🟡", "A": "🟡", "B": "🟡", "SHADOW": "🧪", "BLOCK": "⛔"}.get(grade, "⚪")
    title = ticker.upper() if ticker else "SIGNAL"
    spot_part = f" @ ${float(spot):.2f}" if spot else ""

    lines = [
        f"{grade_emoji} V2 5D EDGE MODEL — {grade} (V1 SILENT)",
        f"{title}{spot_part} · {(result.bias or '').upper()} · {result.setup_archetype}",
        f"MTF: {result.mtf_alignment} · Hold: {result.hold_window}",
        f"V1 status: {v1_status}",
    ]
    if result.historical_proxy_wr:
        lines.append(f"V2 historical proxy WR: {result.historical_proxy_wr:.0%}")
    if result.reason:
        r = result.reason
        if len(r) > 200:
            r = r[:197] + "..."
        lines.append(f"Why V2 sees it: {r}")
    lines.append("REVIEW ONLY — V1 didn't fire. This is V2's standalone read.")
    return "\n".join(line for line in lines if line)


def build_v2_audit_row(result: V2SetupResult, ticker: str, spot: Optional[float] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = result.to_dict()
    row.update({
        "ticker": ticker.upper(),
        "spot": spot,
        "logged_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if extra:
        row.update(extra)
    return row