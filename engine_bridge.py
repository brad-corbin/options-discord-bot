# engine_bridge.py
# ═══════════════════════════════════════════════════════════════════
# Bridge between app.py's existing data format and the v4 engine.
# Drop-in replacement for the raw ExposureEngine calls in app.py.
#
# app.py already fetches chain data from MarketData.app — this module
# converts that data into v4 OptionRow + MarketContext, runs the full
# InstitutionalExpectationEngine.snapshot(), and returns a unified
# result dict that EM/trade/monitor cards can consume directly.
#
# Usage in app.py:
#   from engine_bridge import run_institutional_snapshot
#   result = run_institutional_snapshot(chain_data, spot, dte, ...)
#   # result has: engine, walls, iv, em, bias, confidence, downgrades, audit, ...
# ═══════════════════════════════════════════════════════════════════

import math
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

from options_exposure import (
    OptionRow, MarketContext, OHLC, ScheduledEvent,
    ExposureEngine, InstitutionalExpectationEngine,
    gex_regime, vanna_charm_context, composite_regime,
    InputValidator, DataQualityEngine, ConfidenceEngine,
    AuditLog, RVPolicy, SCHEMA_VERSION, UNITS,
)

log = logging.getLogger(__name__)


def _as_float(val, default=None):
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _as_int(val, default=0):
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def build_option_rows(chain_data: dict, spot: float = 0, days_to_exp: float = 1.0) -> List[OptionRow]:
    """
    Convert MarketData.app chain response into v4 OptionRow objects.
    Reads 'oiChange' array if present (populated by OICache).

    v15: spot and days_to_exp are now optional. When called without spot
    (e.g. from _extract_atm_option_data), the function still parses rows
    using underlying_price=0 — callers that need spot should pass it.
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return []

    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes  = col("strike", None)
    iv_list  = col("iv", None)
    oi_list  = col("openInterest", 0)
    vol_list = col("volume", 0)
    delta_l  = col("delta", None)
    gamma_l  = col("gamma", None)
    theta_l  = col("theta", None)
    vega_l   = col("vega", None)
    bid_l    = col("bid", None)
    ask_l    = col("ask", None)
    sides    = col("side", "")
    oi_chg_l = col("oiChange", None)

    rows = []
    for i in range(n):
        strike = _as_float(strikes[i], 0)
        iv     = _as_float(iv_list[i], 0)
        oi     = _as_int(oi_list[i], 0)
        vol    = _as_int(vol_list[i], 0)
        side   = str(sides[i] or "").lower()

        if strike <= 0 or iv <= 0 or side not in ("call", "put"):
            continue

        oi_change = _as_int(oi_chg_l[i], None) if oi_chg_l[i] is not None else None

        rows.append(OptionRow(
            option_type      = side,
            strike           = strike,
            days_to_exp      = max(days_to_exp, 0.01),
            iv               = iv,
            open_interest    = oi,
            underlying_price = spot,
            volume           = vol,
            bid              = _as_float(bid_l[i]),
            ask              = _as_float(ask_l[i]),
            delta            = _as_float(delta_l[i]),
            gamma            = _as_float(gamma_l[i]),
            theta            = _as_float(theta_l[i]),
            oi_change        = oi_change,
        ))

    return rows


def build_chain_dicts(chain_data: dict) -> List[Dict]:
    """Convert raw MarketData.app chain into plain dicts with standardized field names.

    v15: Added for callers that expect dicts with .get("side"), .get("delta"), etc.
    build_option_rows returns OptionRow dataclasses (option_type, not side) which
    breaks dict-style access. This function returns simple dicts that work with
    _extract_atm_option_data Path A, _iter_chain_contract_rows, and any other
    code that does row.get("field").

    Filters: skips rows with strike <= 0 or side not in (call, put).
    Does NOT filter on iv — ATM contracts may have iv=0 early in the session
    but still have valid delta and bid/ask from market makers.
    """
    sym_list = chain_data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return []

    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes  = col("strike", None)
    iv_list  = col("iv", None)
    oi_list  = col("openInterest", 0)
    vol_list = col("volume", 0)
    delta_l  = col("delta", None)
    gamma_l  = col("gamma", None)
    theta_l  = col("theta", None)
    vega_l   = col("vega", None)
    bid_l    = col("bid", None)
    ask_l    = col("ask", None)
    sides    = col("side", "")
    last_l   = col("last", col("lastPrice", None))

    rows = []
    for i in range(n):
        strike = _as_float(strikes[i], 0)
        side   = str(sides[i] or "").lower()

        if strike <= 0 or side not in ("call", "put"):
            continue

        bid = _as_float(bid_l[i])
        ask = _as_float(ask_l[i])
        mid = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = round((bid + ask) / 2.0, 4)
        elif ask is not None and ask > 0:
            mid = ask
        elif bid is not None and bid > 0:
            mid = bid

        rows.append({
            "strike": strike,
            "side": side,
            "right": side,          # alias — some code checks "right"
            "delta": _as_float(delta_l[i]),
            "gamma": _as_float(gamma_l[i]),
            "theta": _as_float(theta_l[i]),
            "vega": _as_float(vega_l[i]),
            "iv": _as_float(iv_list[i]),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": _as_float(last_l[i] if i < len(last_l) else None),
            "openInterest": _as_int(oi_list[i], 0),
            "volume": _as_int(vol_list[i], 0),
        })

    return rows


def build_market_context(
    spot: float,
    recent_bars: Optional[List[OHLC]] = None,
    events: Optional[List[ScheduledEvent]] = None,
    session_progress: float = 0.5,
    is_0dte: bool = False,
    avg_daily_dollar_volume: Optional[float] = None,
    bid_ask_spread_pct: Optional[float] = None,
) -> MarketContext:
    """Build MarketContext with liquidity inputs from stock data."""
    return MarketContext(
        spot=spot,
        risk_free_rate=0.04,
        recent_bars=recent_bars,
        session_progress=session_progress,
        is_0dte=is_0dte,
        events=events,
        avg_daily_dollar_volume=avg_daily_dollar_volume,
        bid_ask_spread_pct=bid_ask_spread_pct,
    )


def run_institutional_snapshot(
    chain_data: dict,
    spot: float,
    dte: float,
    recent_bars: Optional[List[OHLC]] = None,
    events: Optional[List[ScheduledEvent]] = None,
    session_progress: float = 0.5,
    is_0dte: bool = False,
    avg_daily_dollar_volume: Optional[float] = None,
    bid_ask_spread_pct: Optional[float] = None,
    liquid_index: bool = False,
) -> Dict:
    """
    One-call bridge: takes raw MarketData.app chain dict + spot,
    runs the full v4 institutional snapshot, and returns a result dict
    with everything the EM/trade/monitor cards need.
    """
    # 1. Build v4 rows (now reads oiChange if OICache injected it)
    rows = build_option_rows(chain_data, spot, max(dte, 0.5))
    if not rows:
        return {"error": "no valid option rows", "rows": [], "iv": None}

    # 2. Build context with liquidity inputs
    ctx = build_market_context(
        spot=spot, recent_bars=recent_bars, events=events,
        session_progress=session_progress, is_0dte=is_0dte,
        avg_daily_dollar_volume=avg_daily_dollar_volume,
        bid_ask_spread_pct=bid_ask_spread_pct,
    )

    # 3. Run full v4 snapshot (liquid_index loosens spread detection)
    engine = InstitutionalExpectationEngine(r=0.04)
    snap = engine.snapshot(rows, ctx, liquid_index=liquid_index)

    if not snap or "error" in snap:
        return {"error": snap.get("error", "snapshot failed"), "rows": rows, "iv": None}

    # 4. Extract ATM IV from chain data (same logic as app.py's _get_0dte_iv)
    n = len(chain_data.get("optionSymbol") or [])
    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes  = col("strike", None)
    iv_list  = col("iv", None)
    iv_sides = col("side", "")
    oi_list  = col("openInterest", 0)
    vol_list = col("volume", 0)

    atm_ivs = []
    for pct in (0.005, 0.01, 0.02):
        atm_range = spot * pct
        for i in range(n):
            s  = _as_float(strikes[i], 0)
            iv = _as_float(iv_list[i], 0)
            if iv > 0 and abs(s - spot) <= atm_range:
                atm_ivs.append(iv)
        if atm_ivs:
            break

    avg_iv = round(sum(atm_ivs) / len(atm_ivs), 4) if atm_ivs else None

    # 5. Compute skew and PCR (same as app.py)
    skew = _compute_skew(strikes, iv_list, iv_sides, n, spot)
    pcr  = _compute_pcr(oi_list, vol_list, iv_sides, n)

    # 6. Build legacy engine_result dict for backward compat with _calc_bias
    net = snap.get("dealer_flows", {})
    gex_val   = net.get("gex", 0)
    dex_val   = net.get("dex", 0)
    vanna_val = net.get("vanna", 0)
    charm_val = net.get("charm", 0)

    engine_result = {
        "gex":            round(gex_val / 1_000_000, 2) if abs(gex_val) > 1 else round(gex_val, 2),
        "dex":            round(dex_val / 1_000_000, 2) if abs(dex_val) > 1 else round(dex_val, 2),
        "vanna":          round(vanna_val / 1_000_000, 2) if abs(vanna_val) > 1 else round(vanna_val, 2),
        "charm":          round(charm_val / 1_000_000, 2) if abs(charm_val) > 1 else round(charm_val, 2),
        "flip_price":     net.get("gamma_flip"),
        "is_positive_gex": gex_val >= 0,
        "regime":         snap.get("regime", {}).get("context", {}),
        "vc":             {
            "vanna": snap.get("regime", {}).get("context", {}).get("vanna", ""),
            "charm": snap.get("regime", {}).get("context", {}).get("charm", ""),
        },
    }

    # 7. Build walls dict in app.py's expected format
    snap_walls = snap.get("walls", {})
    by_strike  = snap.get("exposures", {}).get("by_strike", {}) if "exposures" in snap else {}

    # v4.1: Removed redundant ExposureEngine fallback.
    # The v4 snapshot should always populate by_strike.
    # If empty, walls will be empty — that's correct behavior
    # rather than running a duplicate compute path.

    walls = _build_walls(by_strike, spot, snap_walls)

    # 8. Return unified result
    return {
        "rows":           rows,
        "snapshot":       snap,
        "engine_result":  engine_result,
        "walls":          walls,
        "iv":             avg_iv,
        "spot":           spot,
        "by_strike":      by_strike,
        "skew":           skew,
        "pcr":            pcr,
        "confidence":     snap.get("confidence", {}),
        "downgrades":     snap.get("downgrades", []),
        "data_quality":   snap.get("data_quality", {}),
        "trade_sign":     snap.get("trade_sign", {}),
        "audit_log":      snap.get("audit_log", {}),
        "schema_version": snap.get("schema_version", SCHEMA_VERSION),
        "vol_regime":     snap.get("volatility_regime", {}),
    }


def _build_walls(by_strike: dict, spot: float, snap_walls: dict) -> dict:
    """
    Build walls dict in the format app.py's _calc_bias and card functions expect.
    Enforces spot-relative constraints (call wall above spot, put wall below).
    """
    walls = {}

    # Call wall: highest call OI above spot
    call_candidates = sorted(
        [k for k in by_strike if k > spot and by_strike[k].get("call_oi", 0) > 0],
        key=lambda k: by_strike[k]["call_oi"], reverse=True
    )
    if call_candidates:
        cw = call_candidates[0]
        call_top3 = sorted(list(dict.fromkeys(call_candidates))[:3])
        walls["call_wall"]    = cw
        walls["call_wall_oi"] = by_strike[cw]["call_oi"]
        if len(call_top3) > 1:
            walls["call_top3"] = call_top3

    # Put wall: highest put OI below spot
    put_candidates = sorted(
        [k for k in by_strike if k < spot and by_strike[k].get("put_oi", 0) > 0],
        key=lambda k: by_strike[k]["put_oi"], reverse=True
    )
    if put_candidates:
        pw = put_candidates[0]
        put_top3 = sorted(list(dict.fromkeys(put_candidates))[:3], reverse=True)
        walls["put_wall"]    = pw
        walls["put_wall_oi"] = by_strike[pw]["put_oi"]
        if len(put_top3) > 1:
            walls["put_top3"] = put_top3

    # Gamma wall from snapshot
    gw = snap_walls.get("gamma_wall")
    if gw and gw in by_strike:
        walls["gamma_wall"]     = gw
        walls["gamma_wall_gex"] = by_strike[gw].get("gex", 0)

    return walls


def _compute_skew(strikes, iv_list, sides, n, spot) -> dict:
    """ATM call IV vs put IV — same logic as app.py's _get_atm_skew."""
    call_ivs, put_ivs = [], []
    atm_range = spot * 0.02
    for i in range(n):
        strike = _as_float(strikes[i], 0)
        iv     = _as_float(iv_list[i], 0)
        side   = str(sides[i] or "").lower()
        if iv <= 0 or abs(strike - spot) > atm_range:
            continue
        if side == "call":
            call_ivs.append(iv)
        elif side == "put":
            put_ivs.append(iv)
    result = {}
    if call_ivs:
        result["call_iv"] = round(sum(call_ivs) / len(call_ivs) * 100, 1)
    if put_ivs:
        result["put_iv"] = round(sum(put_ivs) / len(put_ivs) * 100, 1)
    return result


def _compute_pcr(oi_list, vol_list, sides, n) -> dict:
    """Put/Call ratio by OI and volume — same logic as app.py's _calc_pcr."""
    call_oi = call_vol = put_oi = put_vol = 0
    for i in range(n):
        oi   = _as_int(oi_list[i], 0)
        vol  = _as_int(vol_list[i], 0)
        side = str(sides[i] or "").lower()
        if side == "call":
            call_oi += oi; call_vol += vol
        elif side == "put":
            put_oi += oi; put_vol += vol
    return {
        "put_oi": put_oi, "call_oi": call_oi,
        "put_vol": put_vol, "call_vol": call_vol,
        "pcr_oi":  round(put_oi / call_oi, 2) if call_oi > 0 else None,
        "pcr_vol": round(put_vol / call_vol, 2) if call_vol > 0 else None,
    }


def format_confidence_header(snap_result: dict) -> str:
    """
    One-line confidence + downgrade header for cards.
    Example: "Confidence: MODERATE (50%) | ⚠ NO_BID_ASK"
    """
    conf = snap_result.get("confidence", {})
    dg   = snap_result.get("downgrades", [])
    label = conf.get("label", "?")
    score = conf.get("composite", 0)

    line = f"Confidence: {label} ({score:.0%})"
    if dg:
        # Show first 2 downgrades, abbreviated
        short_dg = [d.split(":")[0] for d in dg[:2]]
        line += " | ⚠ " + ", ".join(short_dg)
    return line


def format_trade_sign_line(snap_result: dict) -> str:
    """One-line trade sign summary for cards."""
    ts = snap_result.get("trade_sign", {})
    buy  = ts.get("inferred_buy_rows", 0)
    sell = ts.get("inferred_sell_rows", 0)
    conf = ts.get("avg_confidence", 0)
    spread_pct = ts.get("spread_leg_pct", 0)
    groups = ts.get("spread_groups", [])

    line = f"Flow Sign: {buy} buy / {sell} sell (conf {conf:.0%})"
    if spread_pct > 0:
        line += f" | {spread_pct:.0%} spread legs"
    if groups:
        line += f" [{', '.join(groups[:3])}]"
    return line


def format_vol_regime_line(snap_result: dict) -> str:
    """One-line vol regime from v4 engine."""
    vr = snap_result.get("vol_regime", {})
    label  = vr.get("label", "")
    rv20   = vr.get("realized_vol_20d")
    vrp20  = vr.get("vrp_20d")
    source = vr.get("rv_source", "")

    parts = []
    if label:
        parts.append(label)
    if rv20 is not None:
        parts.append(f"RV20: {rv20:.1%}")
    if vrp20 is not None:
        parts.append(f"VRP: {vrp20:.1%}")
    if source:
        parts.append(f"({source})")
    return "  ".join(parts) if parts else ""
