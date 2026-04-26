"""
v2_5d_edge_model.py — V2 5D Edge Model review-card helper

Purpose
-------
Backtest-derived, review-only classifier for active-scanner signals.
This module does NOT open/register trades and does NOT replace V1.
It produces a second Telegram card to be posted directly underneath the
existing V1 legacy scanner card.

Model source
------------
Derived from scanner-native backtests through Phase 2.1:
- A+ edge: PB_CB_RECLAIM_BULL + full MTF alignment
- A edge: CB reclaim with strong/partial MTF OR breakout bull with strong MTF
- B edge: breakout/CB reclaim with weaker but valid context
- Blocks: bull chase inside box, most bear weak-structure buckets
- Debit spread short-strike target: 1.0%–1.5% ITM, but choose actual spread
  by live chain debit/width math, not fixed width.

Safety
------
All outputs are REVIEW ONLY. The calling app must not register this as OPEN.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


MODEL_VERSION = "V2_5D_EDGE_MODEL"
MODEL_LABEL = "V2 5D EDGE MODEL"

# Backtest proxy reference values. These are intentionally conservative round numbers
# for card display and live candidate ranking.
HIST_WR_BY_GRADE = {
    "A+": 0.84,   # A+ at ~1.5% ITM short-strike proxy
    "A": 0.76,    # A at ~1.5% ITM short-strike proxy
    "B": 0.75,    # B at ~1.5% ITM proxy, requires selectivity
    "SHADOW": 0.52,
    "BLOCK": 0.44,
}


@dataclass
class V2SetupResult:
    model_version: str = MODEL_VERSION
    label: str = MODEL_LABEL
    action: str = "SHADOW"          # REVIEW / SHADOW / BLOCK
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


def classify_v2_setup(context: Dict[str, Any]) -> V2SetupResult:
    """Classify a scanner signal into V2 5D edge buckets.

    The context can be a merged dict from webhook_data, recommendation metadata,
    scorer context, Potter Box overlay, or any live snapshot fields. The function
    is defensive: missing fields produce SHADOW, not exceptions.
    """
    ticker = _s(_field(context, "ticker", "symbol", default=""), "").upper()
    bias = _sl(_field(context, "bias", "direction", "side", default="unknown"), "unknown")
    pb_state = _sl(_field(context, "pb_state", "potter_state", default="no_box"), "no_box")
    cb_side = _sl(_field(context, "cb_side", default="n/a"), "n/a")
    mtf = _sl(_field(context, "mtf_alignment_label", "mtf_label", default="unknown"), "unknown")
    above_vwap = _bool(_field(context, "above_vwap", default=False))
    or30_state = _sl(_field(context, "or30_state", default="unknown"), "unknown")

    # Some paths may already pass these from the backtest-derived classifier.
    existing_archetype = _s(_field(context, "setup_archetype", default=""), "")
    existing_grade = _s(_field(context, "setup_grade", default=""), "")

    res = V2SetupResult(bias=bias, mtf_alignment=mtf)

    if bias != "bull":
        res.action = "BLOCK"
        res.setup_grade = "BLOCK"
        res.setup_archetype = "BEAR_RESEARCH_ONLY"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["BLOCK"]
        res.block_reason = "Current tested V2 edge is bullish 5D continuation only; broad bear scanner buckets underperformed."
        res.reason = res.block_reason
        return res

    # Block known negative bullish structure.
    if pb_state == "in_box" and cb_side == "above_cb":
        res.action = "BLOCK"
        res.setup_grade = "BLOCK"
        res.setup_archetype = "BULL_CHASE_IN_BOX_BLOCK"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["BLOCK"]
        res.block_reason = "Bull signal inside box but already above CB — historically a chase/late bucket."
        res.reason = res.block_reason
        return res

    # A+ CB reclaim: inside box and below/near CB with full MTF alignment.
    if pb_state == "in_box" and cb_side in {"below_cb", "at_cb"}:
        res.setup_archetype = "PB_CB_RECLAIM_BULL"
        res.preferred_structure = "Call debit spread review; bull put spread may also fit structure."
        res.short_strike_target = "1.0%–1.5% ITM short strike preferred"
        res.width_guidance = "Scan available $1 / $2 / $2.50 / $5 / wider spreads; choose best debit/width edge, not fixed width."
        if mtf == "full_aligned":
            res.action = "REVIEW"
            res.setup_grade = "A+"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["A+"]
            res.reason = "CB reclaim bull setup with full 15m/30m/60m/daily alignment — strongest tested 5D bucket."
            return res
        if mtf in {"strong_aligned", "partial_aligned"}:
            res.action = "REVIEW"
            res.setup_grade = "A"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["A"]
            res.reason = "CB reclaim bull setup with supportive MTF alignment."
            return res
        res.action = "REVIEW"
        res.setup_grade = "B"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["B"]
        res.reason = "Valid CB reclaim bull structure, but MTF alignment is weaker; review smaller/stricter."
        return res

    # Breakout bull above roof.
    if pb_state == "above_roof":
        res.setup_archetype = "PB_BREAKOUT_BULL"
        res.preferred_structure = "Call debit spread / ITM call debit spread review."
        res.short_strike_target = "1.0%–1.5% ITM short strike preferred; 1.5% minimum for weaker grades."
        res.width_guidance = "Scan available widths and rank real spreads by debit/width edge cushion."
        if mtf == "strong_aligned":
            res.action = "REVIEW"
            res.setup_grade = "A"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["A"]
            res.reason = "Breakout bull with strong MTF alignment."
            return res
        if mtf in {"full_aligned", "partial_aligned", "mixed"}:
            res.action = "REVIEW"
            res.setup_grade = "B"
            res.historical_proxy_wr = HIST_WR_BY_GRADE["B"]
            res.reason = "Breakout bull structure; MTF not optimal but historically positive as a 5D continuation bucket."
            return res

    res.action = "SHADOW"
    res.setup_grade = existing_grade or "SHADOW"
    res.setup_archetype = existing_archetype or "UNCLASSIFIED"
    res.historical_proxy_wr = HIST_WR_BY_GRADE["SHADOW"]
    res.reason = "No proven V2 A/A+/B pattern detected from available live fields. Log for proof-of-concept only."
    return res


def breakeven_wr(debit: float, width: float) -> Optional[float]:
    try:
        debit = float(debit); width = float(width)
        if debit <= 0 or width <= 0:
            return None
        return debit / width
    except Exception:
        return None


def edge_cushion(hist_wr: float, debit: float, width: float) -> Optional[float]:
    be = breakeven_wr(debit, width)
    if be is None:
        return None
    return hist_wr - be


def expected_value(width: float, debit: float, hist_wr: float) -> Optional[float]:
    try:
        width = float(width); debit = float(debit); hist_wr = float(hist_wr)
        if width <= 0 or debit <= 0 or hist_wr < 0 or hist_wr > 1:
            return None
        max_profit = width - debit
        if max_profit <= 0:
            return None
        return (hist_wr * max_profit) - ((1.0 - hist_wr) * debit)
    except Exception:
        return None


def rank_spread_candidates(candidates: Iterable[Dict[str, Any]], historical_wr: float) -> List[Dict[str, Any]]:
    """Rank already-built real spread candidates from the existing options engine.

    Expected candidate keys are flexible but should include width and debit.
    This does not fetch chains or build spreads. It only ranks real candidates
    produced by the existing spread builder.
    """
    ranked: List[Dict[str, Any]] = []
    for c in candidates or []:
        width = _field(c, "width", "spread_width", default=0) or 0
        debit = _field(c, "debit", "net_debit", "cost", default=0) or 0
        be = breakeven_wr(debit, width)
        cushion = edge_cushion(historical_wr, debit, width)
        ev = expected_value(width, debit, historical_wr)
        out = dict(c)
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


def build_v2_card(result: V2SetupResult, ticker: str = "", spot: Optional[float] = None,
                  best_spread: Optional[Dict[str, Any]] = None,
                  alternatives: Optional[List[Dict[str, Any]]] = None) -> str:
    """Build the Telegram V2 card text."""
    header_emoji = "🟢" if result.setup_grade in {"A+", "A"} else ("🟡" if result.setup_grade == "B" else ("⛔" if result.setup_grade == "BLOCK" else "🧪"))
    title_ticker = ticker.upper() if ticker else "SIGNAL"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"{header_emoji} {MODEL_LABEL} — {result.setup_grade}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Ticker: {title_ticker}" + (f" @ ${float(spot):.2f}" if spot else ""),
        f"Action: {result.action} / REVIEW ONLY",
        f"Setup: {result.setup_archetype}",
        f"Bias: {result.bias.upper()}",
        f"MTF: {result.mtf_alignment}",
        f"Hold window: {result.hold_window}",
        "",
        "Why this matters:",
        result.reason or result.block_reason or "No reason supplied.",
        "",
    ]

    if result.action != "BLOCK":
        lines += [
            "Preferred structure:",
            result.preferred_structure,
            f"Short strike target: {result.short_strike_target}",
            f"Width guidance: {result.width_guidance}",
            f"Historical proxy WR: {result.historical_proxy_wr:.0%}" if result.historical_proxy_wr else "Historical proxy WR: n/a",
        ]
        if best_spread:
            width = _field(best_spread, "width", "spread_width", default=None)
            debit = _field(best_spread, "debit", "net_debit", "cost", default=None)
            long_strike = _field(best_spread, "long_strike", default=None)
            short_strike = _field(best_spread, "short_strike", default=None)
            lines += [
                "",
                "Best real spread candidate from existing builder:",
                f"Long/Short: {long_strike} / {short_strike}",
                f"Width: ${float(width):.2f}" if width is not None else "Width: n/a",
                f"Debit: ${float(debit):.2f}" if debit is not None else "Debit: n/a",
            ]
            if best_spread.get("v2_breakeven_wr") is not None:
                lines.append(f"Breakeven WR: {best_spread['v2_breakeven_wr']:.0%}")
            if best_spread.get("v2_edge_cushion") is not None:
                lines.append(f"Edge cushion: {best_spread['v2_edge_cushion']:+.0%}")
            if best_spread.get("v2_ev_proxy") is not None:
                lines.append(f"EV proxy: ${best_spread['v2_ev_proxy']:.2f} per spread")
    else:
        lines += ["Block reason:", result.block_reason or result.reason]

    lines += ["", result.review_only_note]
    return "\n".join(lines)


def build_v2_audit_row(result: V2SetupResult, ticker: str, spot: Optional[float] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = result.to_dict()
    row.update({"ticker": ticker.upper(), "spot": spot})
    if extra:
        row.update(extra)
    return row
