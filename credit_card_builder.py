# credit_card_builder.py
# ═══════════════════════════════════════════════════════════════════
# v8.4: Credit spread card builder for the v8.3 scorer POST path.
#
# Runs AFTER the v8.3 scorer decides "post" and AFTER the debit trade
# card is built/posted. Builds a parallel credit card (bull_put or
# bear_call) anchored at the Potter Box boundary — the edge validated
# on 15,700 trades in Phase 1 (83% credit WR, +32% EV on max risk).
#
# Env-gated behind V84_CREDIT_DUAL_POST (default off).
# Purely additive — never blocks or modifies debit card flow.
#
# Authoritative Phase 1 gate (CONVICTION TAKE):
#   1. active_scanner Tier 1/2
#   2. Ticker in Tier-A/B (drops COIN/CRM/MRNA/MSTR/SMCI/SOFI)
#   3. CB side aligned: bull → below_cb/at_cb, bear → above_cb/at_cb
#   4. Hard skip: bear + above_roof
#   5. Hard skip: bull + wave_label=established
#   6. BEAR regime → bulls only
#
# Additional combo filters (from Phase 1 sub-combo detail):
#   - Exclude `established` wave for BOTH biases (weakest tail; bear+UNKNOWN
#     +established = 67.8% WR on n=151)
#
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
# ═══════════════════════════════════════════════════════════════════

import logging
import math
import os
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# v8.4 (Patch 1): Config constants
# ─────────────────────────────────────────────────────────

# From Phase 1 backtest — indices/sectors get $2.50 width, single stocks $5.00.
INDICES_SECTORS = {
    "SPY", "QQQ", "IWM", "DIA",
    "GLD", "TLT",
    "SOXX", "XLE", "XLF", "XLV",
}

# CONVICTION TAKE ticker exclusion (same as Phase 1)
EXCLUDED_TICKERS = {"COIN", "CRM", "MRNA", "MSTR", "SMCI", "SOFI"}

# Combo-level excludes — from Phase 1 sub-combo detail
# `established` wave is the softest tail for both biases
EXCLUDED_WAVE_LABELS = {"established"}

# Width lookup
WIDTH_INDEX_SECTOR = 2.50
WIDTH_SINGLE_STOCK = 5.00


def _env_on(name: str, default: str = "false") -> bool:
    """Standard env flag read — treats 'true'/'1'/'yes' as on."""
    return os.getenv(name, default).strip().lower() in ("true", "1", "yes", "on")


def is_v84_dual_post_enabled() -> bool:
    """Master env gate. Defaults off. Unset → debit-only behavior unchanged."""
    return _env_on("V84_CREDIT_DUAL_POST", "false")


# ─────────────────────────────────────────────────────────
# v8.4 (Patch 1): Gate + width helpers
# ─────────────────────────────────────────────────────────

def ticker_class(ticker: str) -> str:
    return "index_sector" if ticker.upper() in INDICES_SECTORS else "single_stock"


def width_for_ticker(ticker: str) -> float:
    """Per Phase 1: indices/sectors get $2.50 width, single stocks $5.00."""
    return WIDTH_INDEX_SECTOR if ticker.upper() in INDICES_SECTORS else WIDTH_SINGLE_STOCK


def credit_gate_passes(
    bias: str,
    ticker: str,
    pb_state: str,
    cb_side: str,
    wave_label: str,
    regime_trend: str,
    pb_floor: float = 0.0,
    pb_roof: float = 0.0,
) -> tuple[bool, str]:
    """Check whether a v8.3 scorer signal ALSO qualifies for a credit card.

    Returns (passes, reason_if_not). `reason_if_not` is "" when passes=True.
    All gate checks log-debuggable — not a silent pass.
    """
    bias = (bias or "").lower()
    pb_state = (pb_state or "").lower()
    cb_side = (cb_side or "").lower()
    wave_label = (wave_label or "").lower()
    regime_trend = (regime_trend or "").upper()
    ticker = (ticker or "").upper()

    # 1. In-box requirement (backtest edge is specifically in_box)
    if pb_state != "in_box":
        return False, f"pb_state={pb_state} (need in_box)"

    # 2. Need real floor/roof to place the short strike
    if bias == "bull" and pb_floor <= 0:
        return False, "no pb_floor"
    if bias == "bear" and pb_roof <= 0:
        return False, "no pb_roof"

    # 3. Ticker universe (Tier-A/B)
    if ticker in EXCLUDED_TICKERS:
        return False, f"excluded_ticker ({ticker})"

    # 4. CB side alignment
    if bias == "bull" and cb_side not in ("below_cb", "at_cb"):
        return False, f"cb_side={cb_side} (bull needs below_cb or at_cb)"
    if bias == "bear" and cb_side not in ("above_cb", "at_cb"):
        return False, f"cb_side={cb_side} (bear needs above_cb or at_cb)"

    # 5. Hard skip: bear + above_roof (already filtered by pb_state=in_box above,
    #    but leave the check for defense-in-depth if pb_state semantics change)
    if bias == "bear" and pb_state == "above_roof":
        return False, "bear + above_roof"

    # 6. Wave label exclusion — `established` is the weakest tail per Phase 1
    if wave_label in EXCLUDED_WAVE_LABELS:
        return False, f"wave_label={wave_label} (excluded)"

    # 7. BEAR regime → bulls only
    if regime_trend == "BEAR" and bias == "bear":
        return False, "bear + BEAR regime"

    return True, ""


# ─────────────────────────────────────────────────────────
# v8.4 (Patch 1): Strike placement
# ─────────────────────────────────────────────────────────

def _option_increment(spot: float) -> float:
    """Mirror of income_scanner._option_increment — used as fallback when
    chain doesn't have the exact strike we want."""
    if spot > 200:
        return 2.50
    if spot > 100:
        return 1.00
    return 0.50


def _strike_below_with_chain(level: float, spot: float, chain: dict) -> float:
    """Highest listed PUT strike below `level`, or increment fallback."""
    if chain and chain.get("strike"):
        puts = sorted(set(
            s for s, side in zip(chain["strike"], chain.get("side", []))
            if s is not None and side == "put" and s < level
        ), reverse=True)
        if puts:
            return float(puts[0])
    inc = _option_increment(spot)
    return round(math.floor((level - 0.01) / inc) * inc, 2)


def _strike_above_with_chain(level: float, spot: float, chain: dict) -> float:
    """Lowest listed CALL strike above `level`, or increment fallback."""
    if chain and chain.get("strike"):
        calls = sorted(set(
            s for s, side in zip(chain["strike"], chain.get("side", []))
            if s is not None and side == "call" and s > level
        ))
        if calls:
            return float(calls[0])
    inc = _option_increment(spot)
    return round(math.ceil((level + 0.01) / inc) * inc, 2)


def _credit_from_chain(chain: dict, short_strike: float, long_strike: float,
                       side: str) -> tuple[Optional[float], Optional[float]]:
    """Pull mid-to-mid credit from chain if available. Returns (credit, width)
    or (None, None) if chain doesn't have both strikes."""
    if not chain or not chain.get("strike"):
        return None, None
    try:
        strikes = chain["strike"]
        sides = chain.get("side", [])
        bids = chain.get("bid", [])
        asks = chain.get("ask", [])
        short_mid = None
        long_mid = None
        for i, s in enumerate(strikes):
            if sides[i] != side:
                continue
            if abs(s - short_strike) < 0.01:
                short_mid = (float(bids[i]) + float(asks[i])) / 2.0
            elif abs(s - long_strike) < 0.01:
                long_mid = (float(bids[i]) + float(asks[i])) / 2.0
        if short_mid is None or long_mid is None:
            return None, None
        credit = max(0.0, short_mid - long_mid)
        return credit, abs(short_strike - long_strike)
    except (IndexError, ValueError, TypeError, KeyError):
        return None, None


# ─────────────────────────────────────────────────────────
# v8.4 (Patch 1): Spread builders
# ─────────────────────────────────────────────────────────

def build_bull_put_spread(
    ticker: str,
    spot: float,
    pb_floor: float,
    expiry: str,
    chain: Optional[dict] = None,
    dte: Optional[int] = None,
) -> Optional[dict]:
    """Build a bull_put spread anchored at the Potter Box floor.

    Short = first listed PUT strike ≤ pb_floor.
    Long  = short − width (width by ticker class).
    Returns a dict usable by format_credit_card + tracker hooks, or None
    if the strikes can't be placed sensibly.
    """
    if spot <= 0 or pb_floor <= 0:
        return None

    width = width_for_ticker(ticker)
    short_strike = _strike_below_with_chain(pb_floor, spot, chain or {})
    if short_strike <= 0 or short_strike >= spot:
        return None
    long_strike = round(short_strike - width, 2)
    if long_strike <= 0:
        return None

    credit, real_width = _credit_from_chain(chain or {}, short_strike, long_strike, "put")
    if credit is None:
        credit = round(width * 0.33, 2)
        real_width = width
        chain_tag = "est"
    else:
        chain_tag = "live"

    roc_pct = (credit / (real_width - credit) * 100.0) if (real_width - credit) > 0 else 0.0
    breakeven = round(short_strike - credit, 2)
    cushion_pct = ((spot - short_strike) / spot * 100.0) if spot > 0 else 0.0

    return {
        "trade_type": "bull_put",
        "ticker": ticker.upper(),
        "spot": round(spot, 2),
        "short_strike": short_strike,
        "long_strike": long_strike,
        "width": real_width,
        "credit": round(credit, 2),
        "breakeven": breakeven,
        "roc_pct": round(roc_pct, 1),
        "cushion_pct": round(cushion_pct, 2),
        "pb_floor": round(pb_floor, 2),
        "expiry": expiry,
        "dte": dte,
        "chain_tag": chain_tag,
        "ticker_class": ticker_class(ticker),
    }


def build_bear_call_spread(
    ticker: str,
    spot: float,
    pb_roof: float,
    expiry: str,
    chain: Optional[dict] = None,
    dte: Optional[int] = None,
) -> Optional[dict]:
    """Build a bear_call spread anchored at the Potter Box roof.

    Short = first listed CALL strike ≥ pb_roof.
    Long  = short + width (width by ticker class).
    """
    if spot <= 0 or pb_roof <= 0:
        return None

    width = width_for_ticker(ticker)
    short_strike = _strike_above_with_chain(pb_roof, spot, chain or {})
    if short_strike <= 0 or short_strike <= spot:
        return None
    long_strike = round(short_strike + width, 2)

    credit, real_width = _credit_from_chain(chain or {}, short_strike, long_strike, "call")
    if credit is None:
        credit = round(width * 0.33, 2)
        real_width = width
        chain_tag = "est"
    else:
        chain_tag = "live"

    roc_pct = (credit / (real_width - credit) * 100.0) if (real_width - credit) > 0 else 0.0
    breakeven = round(short_strike + credit, 2)
    cushion_pct = ((short_strike - spot) / spot * 100.0) if spot > 0 else 0.0

    return {
        "trade_type": "bear_call",
        "ticker": ticker.upper(),
        "spot": round(spot, 2),
        "short_strike": short_strike,
        "long_strike": long_strike,
        "width": real_width,
        "credit": round(credit, 2),
        "breakeven": breakeven,
        "roc_pct": round(roc_pct, 1),
        "cushion_pct": round(cushion_pct, 2),
        "pb_roof": round(pb_roof, 2),
        "expiry": expiry,
        "dte": dte,
        "chain_tag": chain_tag,
        "ticker_class": ticker_class(ticker),
    }


# ─────────────────────────────────────────────────────────
# v8.4 (Patch 1): Telegram card formatter
# ─────────────────────────────────────────────────────────

def format_credit_card(spread: dict, conviction_score: Optional[int] = None,
                        wave_label: str = "", cb_side: str = "") -> str:
    """Format a Telegram-ready credit spread card.

    Intentionally compact — this is the SECOND message posted (after the
    debit card), not the primary surface.
    """
    t = spread
    tt = t["trade_type"]
    label = "BULL PUT" if tt == "bull_put" else "BEAR CALL"
    boundary = "PB floor" if tt == "bull_put" else "PB roof"
    boundary_px = t.get("pb_floor", t.get("pb_roof", 0))
    chain_tag = "📡 live" if t["chain_tag"] == "live" else "📊 est"
    conv_line = f"Conv {conviction_score}/100 | " if conviction_score else ""

    lines = [
        f"💎 v8.4 CREDIT — {label} {t['ticker']} ${t['short_strike']:.2f}/${t['long_strike']:.2f}",
        "━" * 28,
        f"{conv_line}Spot ${t['spot']:.2f} | {boundary} ${boundary_px:.2f} | "
        f"Cushion {t['cushion_pct']:.1f}%",
        f"Credit: ${t['credit']:.2f} on ${t['width']:.2f} wide "
        f"({t['roc_pct']:.0f}% RoC) [{chain_tag}]",
        f"BE: ${t['breakeven']:.2f} | Exp: {t['expiry']} ({t.get('dte','?')}d)",
        "",
        f"🎯 Close at 50% max profit | 🛑 Stop at 2× credit",
        "— Not financial advice —",
    ]
    if wave_label or cb_side:
        ctx_bits = []
        if wave_label:
            ctx_bits.append(f"wave={wave_label}")
        if cb_side:
            ctx_bits.append(f"cb={cb_side}")
        lines.insert(3, f"Context: {' | '.join(ctx_bits)}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# v8.4 (Patch 1): End-to-end convenience wrapper
# ─────────────────────────────────────────────────────────

def build_credit_spread_if_gated(
    bias: str,
    ticker: str,
    spot: float,
    pb_state: str,
    pb_floor: float,
    pb_roof: float,
    cb_side: str,
    wave_label: str,
    regime_trend: str,
    expiry: str,
    chain: Optional[dict] = None,
    dte: Optional[int] = None,
) -> tuple[Optional[dict], str]:
    """One-shot: check gate + build spread. Returns (spread_dict|None, reason).

    Caller can:
        spread, reason = build_credit_spread_if_gated(...)
        if spread is None:
            log.debug(f"v8.4 credit gated out: {reason}")
            return
        # post card, register tracker, etc.
    """
    if not is_v84_dual_post_enabled():
        return None, "V84_CREDIT_DUAL_POST disabled"

    passes, reason = credit_gate_passes(
        bias=bias, ticker=ticker, pb_state=pb_state, cb_side=cb_side,
        wave_label=wave_label, regime_trend=regime_trend,
        pb_floor=pb_floor, pb_roof=pb_roof,
    )
    if not passes:
        return None, reason

    if bias.lower() == "bull":
        spread = build_bull_put_spread(ticker, spot, pb_floor, expiry, chain, dte)
    else:
        spread = build_bear_call_spread(ticker, spot, pb_roof, expiry, chain, dte)

    if spread is None:
        return None, "spread_build_failed"
    return spread, ""
