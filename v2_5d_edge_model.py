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
from datetime import datetime, timezone
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
    htf_status = _s(_field(context, "htf_status", default=""), "").upper()
    mtf_raw = _sl(_field(context, "mtf_alignment_label", "mtf_label", default=""), "")
    above_vwap = _bool(_field(context, "above_vwap", default=False))
    daily_bull = _bool(_field(context, "daily_bull", default=False))
    htf_confirmed = _bool(_field(context, "htf_confirmed", default=False)) or htf_status == "CONFIRMED"
    htf_converging = _bool(_field(context, "htf_converging", default=False)) or htf_status == "CONVERGING"
    regime = _s(_field(context, "market_regime", "regime", default=""), "").upper()
    wave = _sl(_field(context, "pb_wave_label", "wave_label", default=""), "")

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

    # Some paths may already pass these from the backtest-derived classifier.
    existing_archetype = _s(_field(context, "setup_archetype", default=""), "")
    existing_grade = _s(_field(context, "setup_grade", default=""), "")

    res = V2SetupResult(bias=bias, mtf_alignment=mtf)

    if bias != "bull":
        res.action = "NO TRADE"
        res.setup_grade = "BLOCK"
        res.setup_archetype = "BEAR_RESEARCH_ONLY"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["BLOCK"]
        res.block_reason = "Current tested V2 edge is bullish 5D continuation only; broad bear scanner buckets underperformed."
        res.reason = res.block_reason
        return res

    # Block known negative bullish structure.
    if pb_state == "in_box" and cb_side == "above_cb":
        res.action = "NO TRADE"
        res.setup_grade = "BLOCK"
        res.setup_archetype = "BULL_CHASE_IN_BOX_BLOCK"
        res.historical_proxy_wr = HIST_WR_BY_GRADE["BLOCK"]
        res.block_reason = "Bull signal inside box but already above CB — historically a chase/late bucket."
        res.reason = res.block_reason
        return res

    # A+ CB reclaim: inside box and below/near CB with full MTF alignment.
    if pb_state == "in_box" and cb_side in {"below_cb", "at_cb"}:
        res.setup_archetype = "PB_CB_RECLAIM_BULL_APPROVED"
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
    if pb_state in {"above_roof", "post_box"}:
        res.setup_archetype = "PB_BREAKOUT_BULL_APPROVED"
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

    res.action = "STALK"
    res.setup_grade = existing_grade or "SHADOW"
    res.setup_archetype = existing_archetype or "UNCLASSIFIED"
    res.historical_proxy_wr = HIST_WR_BY_GRADE["SHADOW"]
    res.reason = "No proven V2 A/A+/B pattern detected from available live fields. Log for proof-of-concept only."
    return res


# Trade types that are credit spreads (you collect premium, max profit = credit,
# max loss = width - credit). Anything else is treated as a debit spread.
CREDIT_TRADE_TYPES = {"bull_put", "bear_call", "credit", "iron_condor"}


def _is_credit_candidate(candidate: Dict[str, Any]) -> bool:
    """Detect whether a spread candidate is a credit spread.

    Order of precedence:
      1. Explicit `is_credit` flag if present.
      2. trade_type in CREDIT_TRADE_TYPES.
      3. Has `credit`/`net_credit` and no `debit`/`net_debit`/`cost`.
    """
    if "is_credit" in candidate:
        return _bool(candidate.get("is_credit"))
    tt = _sl(candidate.get("trade_type") or candidate.get("strategy") or "", "")
    if tt in CREDIT_TRADE_TYPES:
        return True
    has_credit = any(candidate.get(k) for k in ("credit", "net_credit"))
    has_debit = any(candidate.get(k) for k in ("debit", "net_debit", "cost"))
    return has_credit and not has_debit


def _candidate_premium(candidate: Dict[str, Any], is_credit: bool) -> float:
    """Pull the cash flow that defines the spread:
       - credit collected for credit spreads
       - debit paid for debit spreads
    """
    if is_credit:
        v = _field(candidate, "credit", "net_credit", default=0) or 0
    else:
        v = _field(candidate, "debit", "net_debit", "cost", default=0) or 0
    try:
        return float(v)
    except Exception:
        return 0.0


def breakeven_wr(premium: float, width: float, is_credit: bool = False) -> Optional[float]:
    """Win-rate threshold above which the spread is +EV in a binary win/loss model.

    Debit spread:  pay `premium`, max profit = width - premium → BE_WR = premium / width.
    Credit spread: collect `premium`, max loss = width - premium → BE_WR = (width - premium) / width.

    Backward-compatible positional signature: passing (debit, width) without the
    keyword still computes the debit breakeven.
    """
    try:
        premium = float(premium); width = float(width)
        if premium <= 0 or width <= 0:
            return None
        if is_credit:
            be = (width - premium) / width
        else:
            be = premium / width
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
    """Binary-outcome EV. premium is debit paid OR credit collected based on is_credit."""
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
    """Rank already-built real spread candidates from the existing options engine.

    Per-candidate the helper auto-detects credit vs debit and applies the
    correct breakeven / EV math. The original candidate dicts are not mutated;
    enriched copies are returned with v2_* annotations including v2_is_credit
    and v2_premium so downstream rendering can use the right labels.
    """
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


def build_v2_card(result: V2SetupResult, ticker: str = "", spot: Optional[float] = None,
                  best_spread: Optional[Dict[str, Any]] = None,
                  alternatives: Optional[List[Dict[str, Any]]] = None) -> str:
    """Build the Telegram V2 card text."""
    header_emoji = "🟢" if result.setup_grade in {"A+", "A"} else ("🟡" if result.setup_grade == "B" else ("⛔" if result.action == "NO TRADE" else "🧪"))
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

    if result.action != "NO TRADE":
        lines += [
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

            # rank_spread_candidates stamps v2_is_credit + v2_premium. Fall back
            # to detecting from the candidate itself for callers that hand a
            # raw spread dict to build_v2_card without ranking it first.
            if "v2_is_credit" in best_spread:
                is_credit = bool(best_spread.get("v2_is_credit"))
            else:
                is_credit = _is_credit_candidate(best_spread)
            if "v2_premium" in best_spread and best_spread["v2_premium"]:
                premium = best_spread.get("v2_premium")
            else:
                premium = _candidate_premium(best_spread, is_credit)
            premium_label = "Credit collected" if is_credit else "Debit paid"

            lines += [
                "",
                "Best real spread candidate from existing builder:",
                f"Long/Short: {long_strike} / {short_strike}",
                f"Width: ${float(width):.2f}" if width is not None else "Width: n/a",
                (f"{premium_label}: ${float(premium):.2f}" if premium else f"{premium_label}: n/a"),
            ]

            # Show the dollar max profit / max loss explicitly so the EV proxy
            # line below reads correctly for both spread types.
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
        lines += ["Block reason:", result.block_reason or result.reason]

    lines += ["", result.review_only_note]
    return "\n".join(lines)


def build_v2_audit_row(result: V2SetupResult, ticker: str, spot: Optional[float] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = result.to_dict()
    row.update({
        "ticker": ticker.upper(),
        "spot": spot,
        # Always stamp UTC so model_comparison_signals.csv has a real timestamp
        # column (the integration declares "logged_at_utc" in its header but
        # never populated it before this fix).
        "logged_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if extra:
        row.update(extra)
    return row


# ──────────────────────────────────────────────────────────────────────
# Phase 2.5: peer-mode renderers
# ──────────────────────────────────────────────────────────────────────

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
