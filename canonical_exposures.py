"""
canonical_exposures.py — The single canonical computer of dealer exposures.

PURPOSE
-------
Wrap `options_exposure.ExposureEngine.compute()` — the production-canonical
dealer-Greek aggregator — behind ONE entry point. Every reader of "what's
this chain's net dealer GEX/DEX/vanna/charm" or "where are this chain's
gamma/call/put walls" calls `canonical_exposures()`.

WHY THIS WRAPPER EXISTS
-----------------------
ExposureEngine.compute() is the post-Patch-9 source of truth for dealer
exposure. Patch 9 settled the SqueezeMetrics convention (dealer LONG calls,
SHORT puts) at the source — line 743 of options_exposure.py:

  ds=1 if ot=="call" else -1
  gs=1 if ot=="call" else -1

Every reader downstream of `compute()` inherits the correct sign by default.
Per-contract overrides flip it when transaction-level flow analysis indicates
a specific contract bucked the customer-side default.

InstitutionalExpectationEngine.snapshot() (line 1150) calls compute() — that's
the production silent-thesis path. Wrapping compute() means BotState exposes
the same exposure values that drive every downstream engine.

WHAT IT RETURNS
---------------
A dict mirroring `ExposureEngine.compute()` output, but trimmed to the keys
BotState/Research consume. The full structure:

  {
    "net": {
      "gex": float,       # net dealer gamma exposure (positive = dealer long γ)
      "dex": float,       # net dealer delta exposure
      "vanna": float,     # net dealer vanna
      "charm": float,     # net dealer charm
      "volga": float,
      "speed": float,
      "theta": float,
      "rho": float,
    },
    "walls": {
      "call_wall": float|None,   # GEX-weighted call OI strike
      "put_wall":  float|None,   # GEX-weighted put OI strike
      "gamma_wall": float|None,  # max |GEX| strike
      "vol_trigger": float|None, # max |volga| strike
    },
    "by_strike": dict,   # {strike: {gex, dex, vanna, charm, ...}}
    "source": str,       # "exposure_engine" on success, error tag otherwise
  }

When the chain can't produce rows or compute fails, returns a dict with all
None values and a `source` tag indicating the failure reason.

DEPENDENCIES
------------
- engine_bridge.build_option_rows: chain-dict → OptionRow conversion (canonical)
- options_exposure.ExposureEngine.compute(): the actual aggregator

WRAPPER-CONSISTENCY DISCIPLINE
------------------------------
Companion test (`test_canonical_exposures.py`) includes a wrapper-consistency
test: build OptionRows + ExposureEngine directly with the same inputs, call
.compute() directly, assert canonical_exposures returns the same `net` dict.
Same pattern as canonical_gamma_flip and canonical_iv_state.

NOTE ON SCOPE
-------------
This canonical produces walls AND per-strike data, but BotState wires only
the Greek aggregates (gex/dex/vanna/charm/gex_sign) in this patch. Walls
will be wired in a follow-on patch — same canonical source, just read .walls
instead of .net. The fact that we'd need to call this canonical regardless
to get walls means the natural unit IS one wrapper (not separate
canonical_gex / canonical_walls); we just stage which BotState fields go
live in which patch to keep blast radius small.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────

DEFAULT_RISK_FREE_RATE = 0.04


class NetExposuresDict(TypedDict, total=False):
    gex: Optional[float]
    dex: Optional[float]
    vanna: Optional[float]
    charm: Optional[float]
    volga: Optional[float]
    speed: Optional[float]
    theta: Optional[float]
    rho: Optional[float]


class WallsDict(TypedDict, total=False):
    call_wall: Optional[float]
    put_wall: Optional[float]
    gamma_wall: Optional[float]
    vol_trigger: Optional[float]


class ExposuresResultDict(TypedDict, total=False):
    net: NetExposuresDict
    walls: WallsDict
    by_strike: dict
    source: str


# ───────────────────────────────────────────────────────────────────────
# Main entry point
# ───────────────────────────────────────────────────────────────────────

def canonical_exposures(
    chain: dict,
    spot: float,
    days_to_exp: float,
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> ExposuresResultDict:
    """
    Compute dealer exposures for a chain at a given spot.

    Args:
        chain:          Raw chain dict-of-arrays from DataRouter
                        (the shape in `RawInputs.chain`).
        spot:           Current underlying price.
        days_to_exp:    Days until the chain's expiration. Used for OptionRow
                        construction.
        risk_free_rate: Risk-free rate for ExposureEngine. Defaults to 0.04.

    Returns:
        ExposuresResultDict — see module docstring for fields. Always returns
        a dict (never raises); failure modes are encoded in the `source` field
        with empty `net` / `walls` / `by_strike`.
    """
    if spot <= 0:
        return _empty_result("invalid_spot")
    if not chain:
        return _empty_result("empty_chain")
    if days_to_exp <= 0:
        days_to_exp = 0.01

    # ─── Build OptionRows via canonical converter ───────────────────
    try:
        from engine_bridge import build_option_rows
        from options_exposure import ExposureEngine
    except ImportError as e:
        log.error(f"canonical_exposures: required modules not available: {e}")
        return _empty_result("import_failed")

    try:
        rows = build_option_rows(chain, spot=spot, days_to_exp=days_to_exp)
    except Exception as e:
        log.warning(f"canonical_exposures: build_option_rows failed: {e}")
        return _empty_result("build_failed")

    if not rows:
        return _empty_result("no_rows")

    # ─── Wrap ExposureEngine.compute() — production canonical ──────
    try:
        engine = ExposureEngine(r=risk_free_rate)
        result = engine.compute(rows)
    except Exception as e:
        log.warning(f"canonical_exposures: compute failed: {type(e).__name__}: {e}")
        return _empty_result("compute_failed")

    # ─── Pass through compute() output verbatim ─────────────────────
    # The production canonical returns net/walls/by_strike already. Reading
    # specific keys defensively in case future ExposureEngine changes alter
    # the shape — better to return None for a missing key than crash.
    net = result.get("net", {}) if isinstance(result, dict) else {}
    walls = result.get("walls", {}) if isinstance(result, dict) else {}
    by_strike = result.get("by_strike", {}) if isinstance(result, dict) else {}

    return {
        "net": {
            "gex": _safe_float(net.get("gex")),
            "dex": _safe_float(net.get("dex")),
            "vanna": _safe_float(net.get("vanna")),
            "charm": _safe_float(net.get("charm")),
            "volga": _safe_float(net.get("volga")),
            "speed": _safe_float(net.get("speed")),
            "theta": _safe_float(net.get("theta")),
            "rho": _safe_float(net.get("rho")),
        },
        "walls": {
            "call_wall": _safe_float(walls.get("call_wall")),
            "put_wall": _safe_float(walls.get("put_wall")),
            "gamma_wall": _safe_float(walls.get("gamma_wall")),
            "vol_trigger": _safe_float(walls.get("vol_trigger")),
        },
        "by_strike": by_strike,
        "source": "exposure_engine",
    }


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

def _empty_result(source: str) -> ExposuresResultDict:
    """Standard empty/error response shape — keeps the contract uniform."""
    return {
        "net": {
            "gex": None, "dex": None, "vanna": None, "charm": None,
            "volga": None, "speed": None, "theta": None, "rho": None,
        },
        "walls": {
            "call_wall": None, "put_wall": None,
            "gamma_wall": None, "vol_trigger": None,
        },
        "by_strike": {},
        "source": source,
    }


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
    result = canonical_exposures({}, spot=100.0, days_to_exp=3.0)
    print("canonical_exposures on empty chain:")
    print(f"  source: {result['source']}")
    print(f"  net keys: {list(result['net'].keys())}")
    print(f"  walls keys: {list(result['walls'].keys())}")
