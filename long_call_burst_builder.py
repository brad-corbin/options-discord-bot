# long_call_burst_builder.py
# ═══════════════════════════════════════════════════════════════════
# v8.4 Phase 2.6: Long Call Burst card builder for the V2 momentum-burst path.
#
# Built parallel to credit_card_builder.py. Used when V2 5D edge model
# returns momentum_burst_label == "YES" — those signals are intraday-to-1D
# directional bursts, NOT slow income vehicles. The credit-spread builder
# routinely produces poor RoC on these (3-DTE, $0.18-$0.32 credit) because
# burst regimes don't price slow theta-decay structures cheaply. The long
# call captures the gamma the burst is paying for.
#
# Routing: when called from _post_v84_credit_from_scorer, this builder is
# checked FIRST. If burst=YES and a viable long call is built → post that
# card and skip the credit card. If burst!=YES or call build fails → fall
# through to existing credit card flow.
#
# Env-gated behind BURST_FIRST_ROUTING_ENABLED (default off). Purely
# additive — never blocks or modifies V1 trade card flow or v8.4 credit
# card flow when disabled.
#
# Strike selection: ATM to 2.5% OTM, preferring the most liquid strike
# (tightest bid/ask spread). Burst plays want gamma + responsiveness over
# leverage, so we don't reach for far-OTM lottos.
#
# Exit rules on card (REVIEW ONLY — display, not auto-managed):
#   Target: +50% take
#   Stop:   -40% stop
#   Time:   EOD (intraday) or next session close (1-2 DTE)
#
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
# ═══════════════════════════════════════════════════════════════════

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Config constants
# ─────────────────────────────────────────────────────────

# OTM target band for burst strikes (as fraction of spot).
# Lower bound is -0.5% by default — strikes up to 0.5% ITM are accepted
# because the most liquid strike at spot=$415.83 is often the $415 strike,
# which is technically ITM by $0.83 (~0.2%). Strict OTM-only would force
# the bot to pick $417.50 instead, which costs ~half as much delta.
# Upper bound 2.5% keeps us in high-gamma territory — far enough OTM that
# we capture leverage on the burst, close enough that delta is responsive.
# Both tunable via env so the strike picker can be adjusted without a
# code change once we see how it performs in production.
OTM_BAND_LOW = float(os.getenv("BURST_OTM_BAND_LOW", "-0.005") or -0.005)
OTM_BAND_HIGH = float(os.getenv("BURST_OTM_BAND_HIGH", "0.025") or 0.025)

# Bid/ask spread filter — reject strikes where the spread is wider than
# this fraction of mid. 25% is generous for short-DTE OTM calls but
# blocks the truly-illiquid junk strikes.
MAX_BID_ASK_SPREAD_FRAC = float(os.getenv("BURST_MAX_BID_ASK_SPREAD_FRAC", "0.25") or 0.25)

# Exit-rule defaults (review-only display values)
TARGET_TAKE_PCT = 50      # +50% take
STOP_LOSS_PCT   = 40      # -40% stop


def _env_on(name: str, default: str = "false") -> bool:
    """Standard env flag read — treats 'true'/'1'/'yes'/'on' as on."""
    return os.getenv(name, default).strip().lower() in ("true", "1", "yes", "on")


def is_burst_first_routing_enabled() -> bool:
    """Master env gate. Defaults off. Unset → existing v8.4 credit flow
    is unchanged (no burst routing happens at all)."""
    return _env_on("BURST_FIRST_ROUTING_ENABLED", "false")


# ─────────────────────────────────────────────────────────
# Strike selection
# ─────────────────────────────────────────────────────────

def _pick_burst_call_strike(spot: float, chain: Optional[dict]) -> Optional[dict]:
    """Walk the chain for call strikes in [spot, spot * 1.025] and return
    the most liquid candidate. Returns dict with strike/bid/ask/mid/spread,
    or None if no viable strike found.

    Liquidity rule: bid > 0 AND ask > bid AND (ask - bid) / mid <= 25%.
    Among candidates that pass, prefer the strike closest to ATM (tightest
    delta to spot) since burst plays need gamma + responsiveness.
    """
    if not chain or spot <= 0:
        return None

    strikes = chain.get("strike") or []
    sides = chain.get("side") or []
    bids = chain.get("bid") or []
    asks = chain.get("ask") or []
    if not strikes:
        return None

    band_lo = spot * (1.0 + OTM_BAND_LOW)
    band_hi = spot * (1.0 + OTM_BAND_HIGH)

    candidates = []
    for i, k in enumerate(strikes):
        try:
            strike = float(k)
        except (TypeError, ValueError):
            continue
        side = str(sides[i] if i < len(sides) else "").lower()
        if side and side not in ("call", "c"):
            continue
        if strike < band_lo or strike > band_hi:
            continue
        try:
            bid = float(bids[i] if i < len(bids) else 0) or 0.0
            ask = float(asks[i] if i < len(asks) else 0) or 0.0
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        if mid <= 0:
            continue
        spread_frac = (ask - bid) / mid
        if spread_frac > MAX_BID_ASK_SPREAD_FRAC:
            continue
        candidates.append({
            "strike": strike,
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "mid": round(mid, 2),
            "spread_frac": round(spread_frac, 3),
            "otm_pct": round((strike - spot) / spot * 100.0, 2),
        })

    if not candidates:
        return None

    # Prefer the strike closest to ATM — burst plays want responsiveness.
    candidates.sort(key=lambda c: abs(c["otm_pct"]))
    return candidates[0]


# ─────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────

def build_long_call_burst(
    ticker: str,
    spot: float,
    expiry: str,
    chain: Optional[dict] = None,
    dte: Optional[int] = None,
    momentum_score: Optional[int] = None,
    momentum_reasons: str = "",
) -> Optional[dict]:
    """Build a Long Call Burst trade dict, or None if no viable strike.

    Returns a dict shaped similarly to credit_card_builder output so the
    same CSV/audit pipelines can log it without schema changes:

        {
            "trade_type": "long_call_burst",
            "ticker": "MSFT",
            "spot": 415.83,
            "strike": 416.0,
            "otm_pct": 0.04,
            "debit": 1.20,
            "bid": 1.15, "ask": 1.25, "mid": 1.20,
            "spread_frac": 0.083,
            "breakeven": 417.20,
            "expiry": "2026-05-02",
            "dte": 1,
            "chain_tag": "live",
            "momentum_score": 6,
            "momentum_reasons": "...",
            "target_pct": 50,
            "stop_pct": 40,
            "ticker_class": "single_stock",
        }

    Returns None when:
      - spot is invalid
      - chain has no liquid call strike in the OTM band
    """
    if spot <= 0 or not ticker:
        return None

    pick = _pick_burst_call_strike(spot, chain or {})
    if not pick:
        return None

    debit = pick["mid"]
    breakeven = round(pick["strike"] + debit, 2)

    return {
        "trade_type": "long_call_burst",
        "ticker": ticker.upper(),
        "spot": round(spot, 2),
        "strike": pick["strike"],
        "otm_pct": pick["otm_pct"],
        "debit": debit,
        "bid": pick["bid"],
        "ask": pick["ask"],
        "mid": pick["mid"],
        "spread_frac": pick["spread_frac"],
        "breakeven": breakeven,
        "expiry": expiry,
        "dte": dte,
        "chain_tag": "live",  # only "live" path supported — no estimate fallback
        "momentum_score": int(momentum_score) if momentum_score is not None else None,
        "momentum_reasons": momentum_reasons or "",
        "target_pct": TARGET_TAKE_PCT,
        "stop_pct": STOP_LOSS_PCT,
        "ticker_class": "single_stock",  # reserved for future per-class sizing
    }


# ─────────────────────────────────────────────────────────
# Telegram formatter
# ─────────────────────────────────────────────────────────

def format_burst_card(burst: dict, conviction_score: Optional[int] = None,
                       wave_label: str = "", cb_side: str = "") -> str:
    """Telegram-ready card for a long call burst. Compact, mirrors v8.4
    credit card layout so the channel feels consistent."""
    t = burst
    chain_tag = "📡 live" if t.get("chain_tag") == "live" else "📊 est"
    conv_line = f"Conv {conviction_score}/100 | " if conviction_score else ""
    burst_score = t.get("momentum_score")
    burst_reasons = t.get("momentum_reasons", "")

    # Time-stop label — for 0-DTE / intraday burst the time stop is EOD,
    # for 1-2 DTE it's "next session close." Card just says it; tracker
    # is review-only, no automation behind it.
    dte = t.get("dte")
    if dte is None or dte <= 0:
        time_stop = "EOD"
    elif dte == 1:
        time_stop = "next session close"
    else:
        time_stop = f"{dte}D close"

    lines = [
        f"🚀 LONG CALL BURST — {t['ticker']} ${t['strike']:.2f}C",
        "━" * 28,
        f"{conv_line}Spot ${t['spot']:.2f} | Strike ${t['strike']:.2f} "
        f"({t['otm_pct']:+.1f}% OTM)",
        f"Debit: ${t['debit']:.2f} (bid ${t['bid']:.2f} / ask ${t['ask']:.2f}) "
        f"[{chain_tag}]",
        f"BE: ${t['breakeven']:.2f} | Exp: {t['expiry']} ({dte}d)",
    ]
    if burst_score is not None:
        lines.append(f"Burst {burst_score}/10: {burst_reasons or 'live momentum confirmed'}")
    if wave_label or cb_side:
        ctx_bits = []
        if wave_label:
            ctx_bits.append(f"wave={wave_label}")
        if cb_side:
            ctx_bits.append(f"cb={cb_side}")
        lines.append(f"Context: {' | '.join(ctx_bits)}")
    lines.extend([
        "",
        f"🎯 Target: +{t['target_pct']}% | 🛑 Stop: -{t['stop_pct']}% | "
        f"⏰ Time stop: {time_stop}",
        "— Not financial advice. Burst plays are intraday-to-1D vehicles. —",
    ])
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# End-to-end convenience wrapper
# ─────────────────────────────────────────────────────────

def build_burst_card_if_gated(
    bias: str,
    ticker: str,
    spot: float,
    expiry: str,
    chain: Optional[dict] = None,
    dte: Optional[int] = None,
    momentum_score: Optional[int] = None,
    momentum_reasons: str = "",
) -> tuple[Optional[dict], str]:
    """One-shot: check env gate + bias gate + build. Returns (burst|None, reason).

    Bias gate: burst is bull-only by V2 design (v2_5d_edge_model lines 119
    + 206). bias='bear' returns None with reason 'bear_not_supported'.

    Caller pattern:
        burst, reason = build_burst_card_if_gated(...)
        if burst is None:
            log.debug(f"long call burst gated out: {reason}")
            return False  # fall through to credit path
        # post card via format_burst_card
    """
    if not is_burst_first_routing_enabled():
        return None, "BURST_FIRST_ROUTING_ENABLED disabled"

    if (bias or "").lower() != "bull":
        return None, "bear_not_supported"

    burst = build_long_call_burst(
        ticker=ticker, spot=spot, expiry=expiry,
        chain=chain, dte=dte,
        momentum_score=momentum_score,
        momentum_reasons=momentum_reasons,
    )
    if burst is None:
        return None, "no_liquid_atm_call_strike"
    return burst, ""
