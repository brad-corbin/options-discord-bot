"""
bot_state.py — Canonical immutable state snapshot for a ticker at a moment in time.

PURPOSE
-------
Single source of truth for every per-ticker fact the bot computes. Every
engine consumes BotState; engines do NOT recompute. As canonical compute
functions land (one per patch), their corresponding BotState fields go
from None → real values automatically.

PERMISSIVE BUILD()
------------------
build_from_raw() calls every canonical_X function inside try/except. If a
function raises NotImplementedError (still a stub), the corresponding
field is None. This means BotState.build_from_raw() always returns a valid
object — partially populated during the rebuild, fully populated once all
canonicals land.

Lit fields after Patch 11.2:
  - ticker, spot, timestamp_utc          (from raw_inputs)
  - expiration                           (from raw_inputs)
  - chain_clean                          (from raw_inputs.is_clean)
  - gamma_flip                           (from canonical_gamma_flip — REAL)
  - distance_from_flip_pct               (computed from spot + gamma_flip)
  - flip_location                        (computed from distance)
  - convention_version, snapshot_version (constants)
  - volume_today, rvol                   (trivial summaries from quote)

All other fields: None until their canonical function lands.

This is the file that the dashboard ENGINES tab reads. As canonicals land,
the tab automatically shows more data without any template change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# TYPE ALIASES
# ───────────────────────────────────────────────────────────────────────

DealerRegimeT = Literal["pin_range", "trend_expansion", "unknown"]
FlipLocationT = Literal["above_flip", "below_flip", "at_flip", "unknown"]
PotterLocationT = Literal["above_roof", "in_box", "below_floor", "no_box", "unknown"]
DirectionT = Literal["bull", "bear", "neutral", "unknown"]
VolRegimeT = Literal["calm", "elevated", "explosive", "unknown"]
GexSignT = Literal["positive", "negative", "neutral", "unknown"]


# ───────────────────────────────────────────────────────────────────────
# THE CANONICAL STATE
# ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BotState:
    """
    Canonical immutable snapshot of every per-ticker fact the bot computes.

    Constructed via `BotState.build_from_raw(raw_inputs)` or
    `BotState.build(ticker, expiration, ...)`. Once built, fields are read-only.

    Fields are typed Optional where a canonical compute is still pending —
    those return None until the corresponding canonical function lands.
    """

    # ─── Identity / time ────────────────────────────────────────────────
    ticker: str
    timestamp_utc: datetime
    spot: float
    expiration: str

    # ─── Fetch health ───────────────────────────────────────────────────
    chain_clean: bool
    fetch_errors: tuple

    # ─── Aggregated dealer Greeks ───────────────────────────────────────
    gex: Optional[float] = None
    dex: Optional[float] = None
    vanna: Optional[float] = None
    charm: Optional[float] = None
    gex_sign: GexSignT = "unknown"

    # ─── Flip detection (LIVE post-Patch-11.2) ──────────────────────────
    gamma_flip: Optional[float] = None
    distance_from_flip_pct: Optional[float] = None
    flip_location: FlipLocationT = "unknown"

    # ─── Levels / walls ─────────────────────────────────────────────────
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gamma_wall: Optional[float] = None
    max_pain: Optional[float] = None
    pin_zone_low: Optional[float] = None
    pin_zone_high: Optional[float] = None

    # ─── Pivots ─────────────────────────────────────────────────────────
    pivot: Optional[float] = None
    r1: Optional[float] = None
    r2: Optional[float] = None
    s1: Optional[float] = None
    s2: Optional[float] = None

    # ─── Structure (TA-derived) ─────────────────────────────────────────
    fib_resistance: Optional[float] = None
    fib_support: Optional[float] = None
    vpoc: Optional[float] = None
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None

    # ─── Volatility / EM ────────────────────────────────────────────────
    atm_iv: Optional[float] = None
    iv30: Optional[float] = None
    iv_skew_pp: Optional[float] = None
    em_1sd_intraday: Optional[float] = None
    em_1sd_thesis: Optional[float] = None

    # ─── Volume / OI ────────────────────────────────────────────────────
    volume_today: Optional[float] = None
    rvol: Optional[float] = None
    total_oi: Optional[int] = None
    vol_oi_ratio: Optional[float] = None

    # ─── Technicals ─────────────────────────────────────────────────────
    rsi: Optional[float] = None
    macd_hist: Optional[float] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    vwap: Optional[float] = None

    # ─── Regime / Bias ──────────────────────────────────────────────────
    dealer_regime: DealerRegimeT = "unknown"
    bias_score: Optional[int] = None
    bias_direction: DirectionT = "unknown"
    v4_confidence: Optional[int] = None

    # ─── Vol regime ─────────────────────────────────────────────────────
    vix: Optional[float] = None
    vvix: Optional[float] = None
    vol_regime_label: VolRegimeT = "unknown"
    vol_caution_score: Optional[int] = None

    # ─── Potter Box ─────────────────────────────────────────────────────
    potter_box_active: bool = False
    potter_floor: Optional[float] = None
    potter_roof: Optional[float] = None
    potter_location: PotterLocationT = "unknown"
    potter_aligned_dir: DirectionT = "unknown"

    # ─── Flow / OI confirmation ─────────────────────────────────────────
    unusual_flow_active: bool = False
    unusual_flow_notional: Optional[float] = None
    oi_buildup_signal: bool = False

    # ─── Calendar ───────────────────────────────────────────────────────
    minutes_to_close: Optional[float] = None
    days_to_earnings: Optional[int] = None
    days_to_fomc: Optional[int] = None

    # ─── Versioning ─────────────────────────────────────────────────────
    convention_version: int = 2
    snapshot_version: int = 1

    # ─── Status ─────────────────────────────────────────────────────────
    canonical_status: dict = field(default_factory=dict)


    # ════════════════════════════════════════════════════════════════════
    # CONSTRUCTION
    # ════════════════════════════════════════════════════════════════════

    @classmethod
    def build_from_raw(cls, raw, *, days_to_exp: Optional[float] = None) -> "BotState":
        """
        Pure function: RawInputs → BotState. No fetching.

        Every canonical_X call is wrapped in try/except. NotImplementedError
        from a still-stubbed function is caught silently — the field stays
        None and `canonical_status` records that it was a stub. Other
        exceptions are caught with a warning log and recorded in status.

        Result: BotState.build_from_raw() ALWAYS returns a valid object.
        """
        status: dict = {}

        if days_to_exp is None:
            days_to_exp = _days_to_exp_from_iso(raw.expiration, raw.fetched_at_utc)

        # ─── Live: IV state via canonical_iv_state (Patch 11.3.2) ──
        # Wraps options_exposure.UnifiedIVSurface — the production-canonical
        # IV calculator. Replaces the inline _atm_iv_from_chain that briefly
        # lived here and was correctly called out as the wrong pattern.
        iv_state_result = _try_canonical(
            "iv_state",
            lambda: _call_canonical_iv_state(raw.chain, raw.spot, days_to_exp),
            status,
        ) or {}
        # representative_iv is what production feeds to the gamma_flip IV-aware band
        representative_iv = (iv_state_result.get("representative_iv")
                             if isinstance(iv_state_result, dict) else None)
        atm_iv_val = (iv_state_result.get("atm_iv")
                      if isinstance(iv_state_result, dict) else None)
        iv_skew_pp_val = (iv_state_result.get("iv_skew_pp")
                          if isinstance(iv_state_result, dict) else None)
        iv30_val = (iv_state_result.get("iv30")
                    if isinstance(iv_state_result, dict) else None)

        # ─── Live: gamma_flip via canonical_gamma_flip (Patch 11.2) ──
        # Pass representative_iv (the production-canonical IV value) so the
        # IV-aware band from Patch 8 is used. Without this, blanket ±25%.
        gamma_flip_val = _try_canonical(
            "gamma_flip",
            lambda: _call_canonical_gamma_flip(
                raw.chain, raw.spot, days_to_exp, iv=representative_iv,
            ),
            status,
        )

        # Derived (no canonical needed)
        distance_from_flip_pct: Optional[float] = None
        flip_location: FlipLocationT = "unknown"
        if gamma_flip_val is not None and raw.spot > 0 and gamma_flip_val > 0:
            distance_from_flip_pct = (raw.spot - gamma_flip_val) / gamma_flip_val * 100
            flip_location = _classify_flip_location(distance_from_flip_pct)

        # ─── Live: exposures via canonical_exposures (Patch 11.4) ────
        # Wraps ExposureEngine.compute() — production canonical for
        # dealer Greek aggregates AND walls. This patch wires the Greek
        # aggregates only (gex/dex/vanna/charm/gex_sign). Walls are wired
        # in a follow-on patch; the canonical itself produces both in one pass.
        exposures = _try_canonical(
            "exposures",
            lambda: _call_canonical_exposures(raw.chain, raw.spot, days_to_exp),
            status,
        ) or {}
        net_exposures = (exposures.get("net", {})
                         if isinstance(exposures, dict) else {})
        gex_val = net_exposures.get("gex") if isinstance(net_exposures, dict) else None
        dex_val = net_exposures.get("dex") if isinstance(net_exposures, dict) else None
        vanna_val = net_exposures.get("vanna") if isinstance(net_exposures, dict) else None
        charm_val = net_exposures.get("charm") if isinstance(net_exposures, dict) else None

        # ─── Walls: share canonical_exposures' compute (Patch 11.5) ──
        # Walls and Greek aggregates come from the same ExposureEngine.compute()
        # pass — see canonical_exposures.py "NOTE ON SCOPE". No separate
        # canonical_walls function or wrapper file; this is a wiring-only patch.
        # Status mirrors canonical_exposures since they share the same compute.
        walls = (exposures.get("walls", {}) if isinstance(exposures, dict) else {}) or {}
        status["walls"] = status.get("exposures", "stub")

        # ─── Stubs — replace with real canonical_X as each lands ─────
        pivots = _try_canonical("pivots", lambda: _stub("pivots"), status) or {}
        structure = _try_canonical("structure", lambda: _stub("structure"), status) or {}
        em_state = _try_canonical("em_state", lambda: _stub("em_state"), status) or {}
        technicals = _try_canonical("technicals", lambda: _stub("technicals"), status) or {}
        bias = _try_canonical("bias", lambda: _stub("bias"), status) or {}
        regime = _try_canonical("dealer_regime", lambda: _stub("dealer_regime"), status) or {}
        vol_regime = _try_canonical("vol_regime", lambda: _stub("vol_regime"), status) or {}
        potter = _try_canonical("potter_box", lambda: _stub("potter_box"), status) or {}
        flow = _try_canonical("flow_state", lambda: _stub("flow_state"), status) or {}
        calendar = _try_canonical("calendar", lambda: _stub("calendar"), status) or {}

        # ─── Trivial summaries from raw quote ────────────────────────
        volume_today = _safe_float(raw.quote.get("totalVolume")) if raw.quote else None
        avg_volume_20d = _safe_float(raw.quote.get("avgVolume20d")) if raw.quote else None
        rvol = (volume_today / avg_volume_20d) if (volume_today and avg_volume_20d and avg_volume_20d > 0) else None

        # GEX sign from value when available
        if gex_val is None:
            gex_sign: GexSignT = "unknown"
        elif gex_val > 0:
            gex_sign = "positive"
        elif gex_val < 0:
            gex_sign = "negative"
        else:
            gex_sign = "neutral"

        return cls(
            ticker=raw.ticker,
            timestamp_utc=raw.fetched_at_utc,
            spot=raw.spot,
            expiration=raw.expiration,
            chain_clean=raw.is_clean,
            fetch_errors=raw.fetch_errors,
            gex=gex_val,
            dex=dex_val,
            vanna=vanna_val,
            charm=charm_val,
            gex_sign=gex_sign,
            gamma_flip=gamma_flip_val,
            distance_from_flip_pct=distance_from_flip_pct,
            flip_location=flip_location,
            call_wall=walls.get("call_wall") if isinstance(walls, dict) else None,
            put_wall=walls.get("put_wall") if isinstance(walls, dict) else None,
            gamma_wall=walls.get("gamma_wall") if isinstance(walls, dict) else None,
            max_pain=walls.get("max_pain") if isinstance(walls, dict) else None,
            pin_zone_low=walls.get("pin_zone_low") if isinstance(walls, dict) else None,
            pin_zone_high=walls.get("pin_zone_high") if isinstance(walls, dict) else None,
            pivot=pivots.get("pivot") if isinstance(pivots, dict) else None,
            r1=pivots.get("r1") if isinstance(pivots, dict) else None,
            r2=pivots.get("r2") if isinstance(pivots, dict) else None,
            s1=pivots.get("s1") if isinstance(pivots, dict) else None,
            s2=pivots.get("s2") if isinstance(pivots, dict) else None,
            fib_resistance=structure.get("fib_resistance") if isinstance(structure, dict) else None,
            fib_support=structure.get("fib_support") if isinstance(structure, dict) else None,
            vpoc=structure.get("vpoc") if isinstance(structure, dict) else None,
            swing_high=structure.get("swing_high") if isinstance(structure, dict) else None,
            swing_low=structure.get("swing_low") if isinstance(structure, dict) else None,
            atm_iv=atm_iv_val,
            iv30=iv30_val,
            iv_skew_pp=iv_skew_pp_val,
            em_1sd_intraday=em_state.get("em_1sd_intraday") if isinstance(em_state, dict) else None,
            em_1sd_thesis=em_state.get("em_1sd_thesis") if isinstance(em_state, dict) else None,
            volume_today=volume_today,
            rvol=rvol,
            total_oi=None,
            vol_oi_ratio=None,
            rsi=technicals.get("rsi") if isinstance(technicals, dict) else None,
            macd_hist=technicals.get("macd_hist") if isinstance(technicals, dict) else None,
            adx=technicals.get("adx") if isinstance(technicals, dict) else None,
            atr=technicals.get("atr") if isinstance(technicals, dict) else None,
            vwap=technicals.get("vwap") if isinstance(technicals, dict) else None,
            dealer_regime=regime.get("dealer_regime", "unknown") if isinstance(regime, dict) else "unknown",
            bias_score=bias.get("bias_score") if isinstance(bias, dict) else None,
            bias_direction=bias.get("bias_direction", "unknown") if isinstance(bias, dict) else "unknown",
            v4_confidence=bias.get("v4_confidence") if isinstance(bias, dict) else None,
            vix=vol_regime.get("vix") if isinstance(vol_regime, dict) else None,
            vvix=vol_regime.get("vvix") if isinstance(vol_regime, dict) else None,
            vol_regime_label=vol_regime.get("vol_regime_label", "unknown") if isinstance(vol_regime, dict) else "unknown",
            vol_caution_score=vol_regime.get("vol_caution_score") if isinstance(vol_regime, dict) else None,
            potter_box_active=potter.get("active", False) if isinstance(potter, dict) else False,
            potter_floor=potter.get("floor") if isinstance(potter, dict) else None,
            potter_roof=potter.get("roof") if isinstance(potter, dict) else None,
            potter_location=potter.get("location", "unknown") if isinstance(potter, dict) else "unknown",
            potter_aligned_dir=potter.get("aligned_direction", "unknown") if isinstance(potter, dict) else "unknown",
            unusual_flow_active=flow.get("unusual_flow_active", False) if isinstance(flow, dict) else False,
            unusual_flow_notional=flow.get("unusual_flow_notional") if isinstance(flow, dict) else None,
            oi_buildup_signal=flow.get("oi_buildup_signal", False) if isinstance(flow, dict) else False,
            minutes_to_close=calendar.get("minutes_to_close") if isinstance(calendar, dict) else None,
            days_to_earnings=calendar.get("days_to_earnings") if isinstance(calendar, dict) else None,
            days_to_fomc=calendar.get("days_to_fomc") if isinstance(calendar, dict) else None,
            canonical_status=status,
        )

    @classmethod
    def build(
        cls,
        ticker: str,
        expiration: str,
        *,
        data_router,
        bars_days: int = 504,
    ) -> "BotState":
        """Convenience: fetch raw inputs, then build_from_raw."""
        from raw_inputs import fetch_raw_inputs
        raw = fetch_raw_inputs(
            ticker, expiration, data_router=data_router, bars_days=bars_days
        )
        return cls.build_from_raw(raw)

    # ════════════════════════════════════════════════════════════════════
    # Convenience accessors used by Research page
    # ════════════════════════════════════════════════════════════════════

    @property
    def fields_lit(self) -> int:
        """Count fields with real values (not None / not 'unknown' / not False).

        Used by Research page to show rebuild progress per ticker.
        """
        from dataclasses import fields as dc_fields
        n = 0
        for f in dc_fields(self):
            if f.name in ("canonical_status", "fetch_errors"):
                continue
            v = getattr(self, f.name)
            if v is None:
                continue
            if isinstance(v, str) and v == "unknown":
                continue
            if v is False:
                continue
            n += 1
        return n

    @property
    def fields_total(self) -> int:
        """Total field count, excluding bookkeeping. Denominator for fields_lit."""
        from dataclasses import fields as dc_fields
        return len([
            f for f in dc_fields(self)
            if f.name not in ("canonical_status", "fetch_errors")
        ])


# ───────────────────────────────────────────────────────────────────────
# CANONICAL CALL HELPERS
# ───────────────────────────────────────────────────────────────────────

def _try_canonical(name: str, fn, status: dict):
    """Call a canonical function. Catch any exception, record in status."""
    try:
        result = fn()
        status[name] = "live"
        return result
    except NotImplementedError as e:
        status[name] = f"stub: {str(e)[:80]}" if str(e) else "stub"
        return None
    except Exception as e:
        log.warning(f"canonical_{name} failed: {e}")
        status[name] = f"error: {type(e).__name__}: {str(e)[:80]}"
        return None


def _stub(name: str):
    """Placeholder for canonical functions not yet implemented."""
    raise NotImplementedError(f"canonical_{name} pending implementation")


def _call_canonical_gamma_flip(chain, spot, days_to_exp, *, iv=None):
    """Wrapper around canonical_gamma_flip with proper imports.

    Passes both `iv` and `dte_years` EXPLICITLY when iv is given. Don't rely
    on canonical_gamma_flip's internal auto-derive (Patch 11.2.1) — that's
    a safety net inside the canonical, but the caller's CONTRACT is clearer
    when both args are passed at the call site. Defense-in-depth: if the
    canonical's auto-derive is ever refactored, this caller still works.
    """
    from canonical_gamma_flip import canonical_gamma_flip
    dte_years = (max(days_to_exp, 0.01) / 365.0) if iv is not None else None
    return canonical_gamma_flip(
        chain, spot=spot, days_to_exp=days_to_exp,
        iv=iv, dte_years=dte_years,
    )


def _call_canonical_iv_state(chain, spot, days_to_exp):
    """Wrapper around canonical_iv_state with proper imports."""
    from canonical_iv_state import canonical_iv_state
    return canonical_iv_state(chain, spot=spot, days_to_exp=days_to_exp)


def _call_canonical_exposures(chain, spot, days_to_exp):
    """Wrapper around canonical_exposures with proper imports."""
    from canonical_exposures import canonical_exposures
    return canonical_exposures(chain, spot=spot, days_to_exp=days_to_exp)


# v11.7 (Patch F.5.1): canonical_technicals integration helper.
def _build_technicals_from_raw(raw):
    """Compute RSI / MACD / ADX from raw.bars using canonical_technicals.

    Defensive about bar key naming: some upstream sources use
    'high'/'low'/'close', others use 'h'/'l'/'c'. Mirrors the pattern at
    risk_manager.py:275.

    Returns a dict with keys: rsi (float|None), macd_line (float|None),
    macd_signal (float|None), macd_hist (float|None), adx (float).

    None values for RSI/MACD mean insufficient data (matching
    canonical_technicals' return). ADX returns 0.0 on insufficient data
    rather than None — this matches canonical_technicals.adx and lets
    downstream scorers' ADX-quintile rules check for the zero sentinel
    without special-casing None vs 0.0.

    Forward-compat note: macd_line and macd_signal are returned but
    BotState's dataclass currently only reads macd_hist (see
    build_from_raw at lines ~324-326). The line/signal keys are
    preserved for V2 when MACD line/signal land as their own BotState
    fields. Don't delete them as "unused" — that would break the
    forward-compat surface.
    """
    import canonical_technicals
    bars = getattr(raw, "bars", None) or []
    if not bars:
        return {
            "rsi": None,
            "macd_line": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx": 0.0,
        }

    highs  = [b.get("h") or b.get("high")  for b in bars]
    lows   = [b.get("l") or b.get("low")   for b in bars]
    closes = [b.get("c") or b.get("close") for b in bars]

    # Defend against partial bars — any None breaks the indicator math.
    if not all(highs) or not all(lows) or not all(closes):
        return {
            "rsi": None,
            "macd_line": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx": 0.0,
        }

    rsi_val = canonical_technicals.rsi(closes)
    macd_dict = canonical_technicals.macd(closes) or {}
    adx_val = canonical_technicals.adx(highs, lows, closes)

    return {
        "rsi":         rsi_val,
        "macd_line":   macd_dict.get("macd_line"),
        "macd_signal": macd_dict.get("signal_line"),
        "macd_hist":   macd_dict.get("macd_hist"),
        "adx":         adx_val,
    }


# ───────────────────────────────────────────────────────────────────────
# UTILITIES
# ───────────────────────────────────────────────────────────────────────

def _days_to_exp_from_iso(expiration_iso: str, fetched_at_utc: datetime) -> float:
    """Days from now to expiration, given ISO-format expiration date."""
    try:
        exp_dt = datetime.fromisoformat(expiration_iso.replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        delta = exp_dt - fetched_at_utc
        return max(delta.total_seconds() / 86400.0, 0.01)
    except (ValueError, AttributeError):
        return 3.0


def _classify_flip_location(distance_pct: float) -> FlipLocationT:
    if abs(distance_pct) < 0.25:
        return "at_flip"
    return "above_flip" if distance_pct > 0 else "below_flip"


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ───────────────────────────────────────────────────────────────────────
# Direct-run sanity
# ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dataclasses import fields as dc_fields
    fields_list = dc_fields(BotState)
    print(f"BotState defines {len(fields_list)} fields:")
    for f in fields_list:
        print(f"  {f.name}: {f.type}")
