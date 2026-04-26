"""
v2_dual_card_integration.py — V2 review-card posting wrapper

Purpose
-------
Small integration helper for the existing V1 scanner card flow.
Call `post_v2_under_v1(...)` immediately after the V1 Telegram card posts.

Safety
------
- REVIEW ONLY. Does not register/open/monitor any trade.
- Catches all exceptions and returns a small status dict instead of breaking V1.
- Logs V2 separately to model_comparison_signals.csv.
- Uses existing app helpers passed in by caller; no new Telegram or Sheets systems.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional

from v2_5d_edge_model import (
    classify_v2_setup,
    build_v2_card,
    build_v2_audit_row,
    rank_spread_candidates,
)


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except Exception:
        return default


def _merge_context(*sources: Any) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    for src in sources:
        if isinstance(src, dict):
            ctx.update(src)
    return ctx


def post_v2_under_v1(
    *,
    ticker: str,
    bias: str,
    telegram_chat_id: str,
    telegram_post_fn: Callable[[str, str], Any],
    append_csv_row_fn: Optional[Callable[[str, list, dict], Any]] = None,
    log_warning_fn: Optional[Callable[[str], Any]] = None,
    context_sources: Iterable[Any] = (),
    candidate_spreads: Optional[Iterable[Dict[str, Any]]] = None,
    enabled: bool = True,
    post_enabled: bool = True,
) -> Dict[str, Any]:
    """Post the V2 5D review card directly underneath V1.

    Parameters are dependency-injected from app.py so this module stays small
    and cannot accidentally open positions or call unrelated app state.
    """
    if not enabled:
        return {"posted": False, "reason": "disabled"}

    try:
        ctx = _merge_context(*context_sources)
        ctx.setdefault("ticker", ticker)
        ctx.setdefault("bias", bias)
        ctx.setdefault("direction", bias)

        spot = _as_float(
            ctx.get("close") or ctx.get("spot") or ctx.get("spot_at_callout") or ctx.get("entry_price"),
            0.0,
        )

        result = classify_v2_setup(ctx)

        ranked_spreads = []
        best_spread = None
        alternatives = []
        if candidate_spreads:
            ranked_spreads = rank_spread_candidates(candidate_spreads, result.historical_proxy_wr)
            best_spread = ranked_spreads[0] if ranked_spreads else None
            alternatives = ranked_spreads[1:3] if ranked_spreads else []

        card = build_v2_card(result, ticker=ticker, spot=spot, best_spread=best_spread, alternatives=alternatives)

        if post_enabled:
            telegram_post_fn(telegram_chat_id, card)

        if append_csv_row_fn:
            fields = [
                "logged_at_utc", "model_version", "ticker", "spot", "bias",
                "action", "setup_grade", "setup_archetype", "mtf_alignment",
                "historical_proxy_wr", "preferred_structure", "short_strike_target",
                "width_guidance", "reason", "block_reason", "review_only_note",
                "best_width", "best_debit", "best_debit_to_width", "best_edge_cushion", "best_ev_proxy",
            ]
            extra = {"bias": bias}
            if best_spread:
                width = _as_float(best_spread.get("width") or best_spread.get("spread_width"), 0)
                debit = _as_float(best_spread.get("debit") or best_spread.get("net_debit") or best_spread.get("cost"), 0)
                extra.update({
                    "best_width": width,
                    "best_debit": debit,
                    "best_debit_to_width": round(debit / width, 4) if width else "",
                    "best_edge_cushion": best_spread.get("v2_edge_cushion", ""),
                    "best_ev_proxy": best_spread.get("v2_ev_proxy", ""),
                })
            else:
                extra.update({
                    "best_width": "", "best_debit": "", "best_debit_to_width": "",
                    "best_edge_cushion": "", "best_ev_proxy": "",
                })
            append_csv_row_fn("model_comparison_signals.csv", fields, build_v2_audit_row(result, ticker, spot=spot, extra=extra))

        return {
            "posted": bool(post_enabled),
            "setup_grade": result.setup_grade,
            "setup_archetype": result.setup_archetype,
            "action": result.action,
            "best_spread": best_spread,
        }
    except Exception as e:
        if log_warning_fn:
            try:
                log_warning_fn(f"V2 5D review card failed for {ticker}: {e}")
            except Exception:
                pass
        return {"posted": False, "reason": f"error: {e}"}
