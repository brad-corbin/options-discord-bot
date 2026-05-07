"""
canonical_iv_state.py — The single canonical computer of IV state for a chain.

PURPOSE
-------
Wrap `options_exposure.UnifiedIVSurface` (the production-canonical IV
calculator, "Fix #14" maintained) behind ONE entry point. Every reader
of "what's this chain's representative IV" / "what's this strike's IV"
calls `canonical_iv_state()`.

WHY THIS WRAPPER EXISTS
-----------------------
The data_inventory found 41 matches for ATM IV / representative IV
across 6 files. Existing implementations:
  1. options_exposure.UnifiedIVSurface.representative_iv() — sophisticated:
     ExposureEngine.resolve_iv() + sqrt(1/DTE) × distance × OI weighting
  2. app.py:_get_0dte_iv() — full v4 chain analysis path
  3. app.py:_infer_expiry_iv_from_rows() — three-tier fallback
  4. app.py:_infer_expiry_iv_with_fallbacks() — chain-level wrapper of the above
  5. oi_flow.py:atm_iv = sum(...)/len(...) — averaging
  6. swing_engine.py:atm_iv = []  (collection list)

UnifiedIVSurface is the production-canonical. It's what
InstitutionalExpectationEngine.snapshot() uses (line 1151 of
options_exposure.py). It's what the IV-aware band in
gamma_flip / vanna_flip is fed by. Wrapping it here means every
caller — BotState, dashboards, any future engine — runs through the
same battle-tested computation.

CONTRACT
--------
Input: raw chain dict-of-arrays (the shape in `RawInputs.chain`),
       spot price, days_to_exp.
Output: dict with:
  - representative_iv: float — weighted-average IV (sqrt(1/DTE) × dist × OI).
                              This is the value to feed to canonical_gamma_flip
                              for the IV-aware band, matching production behavior.
  - atm_iv: float — IV at strike closest to spot (uses surface.strike_iv
                    which has its own near-strike pooling logic, NOT a
                    naive single-strike pick).
  - iv_skew_pp: Optional[float] — 95%/105% strike IV difference in
                                  percentage points. Roughly a 25-delta skew
                                  proxy. None if not enough chain depth.
  - iv30: None — placeholder; computing 30-day IV needs a multi-expiration
                 surface, out of scope for a single-chain canonical.
  - source: str — "unified_iv_surface" on success, "no_rows" / "build_failed" /
                  "import_failed" / "compute_failed" on failure.

Returns None for representative_iv (with appropriate `source`) when the chain
can't produce rows or any step fails. Callers handle None by falling back
to whatever default they used before — typically the ±25% blanket band.

WRAPPER-CONSISTENCY DISCIPLINE
------------------------------
The companion test (test_canonical_iv_state.py) includes a wrapper-consistency
test: build a UnifiedIVSurface directly with the same inputs, call
representative_iv(), assert the wrapper returns the identical value. If they
ever diverge, the wrapper has drifted. Same pattern as
test_canonical_gamma_flip's test_canonical_matches_direct_engine_call.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────

DEFAULT_RISK_FREE_RATE = 0.04
SKEW_LOWER_PCT = 0.95   # 95% strike for put side of skew
SKEW_UPPER_PCT = 1.05   # 105% strike for call side of skew


class IVStateDict(TypedDict, total=False):
    representative_iv: Optional[float]
    atm_iv: Optional[float]
    iv_skew_pp: Optional[float]
    iv30: Optional[float]
    source: str


# ───────────────────────────────────────────────────────────────────────
# Main entry point
# ───────────────────────────────────────────────────────────────────────

def canonical_iv_state(
    chain: dict,
    spot: float,
    days_to_exp: float,
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> IVStateDict:
    """
    Compute IV state for a chain at a given spot.

    Args:
        chain:          Raw chain dict-of-arrays from DataRouter
                        (the shape in `RawInputs.chain`).
        spot:           Current underlying price.
        days_to_exp:    Days until the chain's expiration. Used for
                        OptionRow construction.
        risk_free_rate: Risk-free rate for ExposureEngine. Defaults to 0.04.

    Returns:
        IVStateDict — see module docstring for fields. Always returns a
        dict (never raises); failure modes are encoded in the `source` field
        with representative_iv/atm_iv as None.
    """
    if spot <= 0:
        return _empty_result("invalid_spot")
    if not chain:
        return _empty_result("empty_chain")
    if days_to_exp <= 0:
        days_to_exp = 0.01   # match canonical_gamma_flip clamping

    # ─── Build OptionRows via the canonical converter ─────────────────
    try:
        from engine_bridge import build_option_rows
        from options_exposure import ExposureEngine, UnifiedIVSurface
    except ImportError as e:
        log.error(f"canonical_iv_state: required modules not available: {e}")
        return _empty_result("import_failed")

    try:
        rows = build_option_rows(chain, spot=spot, days_to_exp=days_to_exp)
    except Exception as e:
        log.warning(f"canonical_iv_state: build_option_rows failed: {e}")
        return _empty_result("build_failed")

    if not rows:
        return _empty_result("no_rows")

    # ─── Wrap UnifiedIVSurface — the production canonical ─────────────
    try:
        engine = ExposureEngine(r=risk_free_rate)
        surface = UnifiedIVSurface(rows, engine)

        rep_iv = surface.representative_iv(spot)
        atm_iv = surface.strike_iv(spot, spot)

        # Skew: IV(95%) - IV(105%), in percentage points.
        # Positive skew = puts more expensive than equidistant calls (typical equity skew).
        try:
            iv_low = surface.strike_iv(spot * SKEW_LOWER_PCT, spot)
            iv_high = surface.strike_iv(spot * SKEW_UPPER_PCT, spot)
            if iv_low > 0 and iv_high > 0:
                iv_skew_pp = (iv_low - iv_high) * 100.0
            else:
                iv_skew_pp = None
        except Exception as e:
            log.debug(f"canonical_iv_state: skew compute failed: {e}")
            iv_skew_pp = None

        return {
            "representative_iv": rep_iv if rep_iv > 0 else None,
            "atm_iv": atm_iv if atm_iv > 0 else None,
            "iv_skew_pp": iv_skew_pp,
            "iv30": None,   # cross-expiration, out of scope here
            "source": "unified_iv_surface",
        }
    except Exception as e:
        log.warning(f"canonical_iv_state: surface compute failed: {type(e).__name__}: {e}")
        return _empty_result("compute_failed")


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

def _empty_result(source: str) -> IVStateDict:
    """Standard empty/error response shape — keeps the contract uniform."""
    return {
        "representative_iv": None,
        "atm_iv": None,
        "iv_skew_pp": None,
        "iv30": None,
        "source": source,
    }


# ───────────────────────────────────────────────────────────────────────
# Direct-run sanity
# ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # No chain — should produce safe empty result
    result = canonical_iv_state({}, spot=100.0, days_to_exp=3.0)
    print("canonical_iv_state on empty chain:")
    for k, v in result.items():
        print(f"  {k}: {v}")
