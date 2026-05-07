"""
test_canonical_iv_state.py — Unit tests for canonical_iv_state.

Includes wrapper-consistency test: build UnifiedIVSurface directly, call
representative_iv() / strike_iv(), assert wrapper output is identical.
This is the discipline that should have been followed when I wrote the
inline _atm_iv_from_chain in bot_state.py — it would have caught the
fact that I was doing something different from canonical.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canonical_iv_state import canonical_iv_state, _empty_result

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    return True


def assert_approx(actual, expected, tol, msg):
    if actual is None or abs(actual - expected) > tol:
        FAILED.append(f"{msg}: expected ~{expected} ±{tol}, got {actual!r}")
        return False
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition False")
        return False
    return True


def assert_in_range(actual, lo, hi, msg):
    if actual is None or actual < lo or actual > hi:
        FAILED.append(f"{msg}: {actual!r} not in [{lo}, {hi}]")
        return False
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    return True


# ───────────────────────────────────────────────────────────────────────
# Synthetic chain builders
# ───────────────────────────────────────────────────────────────────────

def _build_chain(spot: float, ivs: dict = None) -> dict:
    """Realistic balanced chain. ivs: optional override map of {strike: iv_value}.
    Defaults to flat 0.25 IV across all strikes."""
    chain = {
        "s": "ok",
        "optionSymbol": [],
        "strike": [],
        "side": [],
        "expiration": [],
        "openInterest": [],
        "volume": [],
        "delta": [],
        "gamma": [],
        "iv": [],
        "bid": [],
        "ask": [],
    }
    strikes = [spot * 0.90, spot * 0.95, spot * 0.97, spot * 0.99,
               spot, spot * 1.01, spot * 1.03, spot * 1.05, spot * 1.10]
    for K in strikes:
        K = round(K, 2)
        for side in ("call", "put"):
            chain["optionSymbol"].append(f"T{int(K*100):08d}{side[0].upper()}")
            chain["strike"].append(K)
            chain["side"].append(side)
            chain["expiration"].append("2026-05-09")
            chain["openInterest"].append(2000)
            chain["volume"].append(100)
            d = 0.5 if side == "call" else -0.5
            chain["delta"].append(d)
            chain["gamma"].append(0.04)
            iv = (ivs or {}).get(K, 0.25)
            chain["iv"].append(iv)
            chain["bid"].append(1.0)
            chain["ask"].append(1.2)
    return chain


def _build_skewed_chain(spot: float) -> dict:
    """Chain with classic equity skew: OTM puts more expensive than OTM calls."""
    iv_map = {}
    strikes = [spot * 0.90, spot * 0.95, spot * 0.97, spot * 0.99,
               spot, spot * 1.01, spot * 1.03, spot * 1.05, spot * 1.10]
    for K in strikes:
        K = round(K, 2)
        if K < spot * 0.99:        # OTM puts → higher IV
            iv_map[K] = 0.35
        elif K > spot * 1.01:      # OTM calls → lower IV
            iv_map[K] = 0.20
        else:
            iv_map[K] = 0.27
    return _build_chain(spot, iv_map)


# ───────────────────────────────────────────────────────────────────────
# Empty/edge case tests
# ───────────────────────────────────────────────────────────────────────

def test_empty_chain_returns_empty_result():
    result = canonical_iv_state({}, spot=100.0, days_to_exp=3.0)
    assert_eq(result["source"], "empty_chain", "source flagged empty_chain")
    assert_is_none(result["representative_iv"], "rep_iv None")
    assert_is_none(result["atm_iv"], "atm_iv None")
    PASSED.append("test_empty_chain_returns_empty_result")


def test_invalid_spot_returns_empty_result():
    result = canonical_iv_state(_build_chain(100.0), spot=0, days_to_exp=3.0)
    assert_eq(result["source"], "invalid_spot", "source flagged invalid_spot")
    assert_is_none(result["representative_iv"], "rep_iv None")
    PASSED.append("test_invalid_spot_returns_empty_result")


def test_zero_dte_clamps_safely():
    """DTE 0 (expiration day) should not crash — clamps to small positive."""
    result = canonical_iv_state(_build_chain(100.0), spot=100.0, days_to_exp=0)
    # Should still produce a result, not crash
    assert_true(result["source"] == "unified_iv_surface" or
                result["source"].startswith("compute_failed"),
                f"reasonable source on dte=0: {result['source']}")
    PASSED.append("test_zero_dte_clamps_safely")


# ───────────────────────────────────────────────────────────────────────
# Live integration tests (real options_exposure)
# ───────────────────────────────────────────────────────────────────────

def test_flat_iv_chain_returns_flat_iv():
    """Chain with all strikes at IV=0.25 should produce representative_iv ≈ 0.25.
    UnifiedIVSurface uses ExposureEngine.resolve_iv internally which can re-derive
    IV from greeks — small differences from input are expected, not equality."""
    chain = _build_chain(100.0)   # all strikes 0.25 IV
    result = canonical_iv_state(chain, spot=100.0, days_to_exp=3.0)

    if result["source"] != "unified_iv_surface":
        FAILED.append(f"test_flat_iv: source not unified_iv_surface: {result['source']}")
        return
    assert_approx(result["representative_iv"], 0.25, 0.05,
                  "flat-IV chain → representative_iv near 0.25")
    assert_approx(result["atm_iv"], 0.25, 0.05,
                  "flat-IV chain → atm_iv near 0.25")
    PASSED.append("test_flat_iv_chain_returns_flat_iv")


def test_skewed_chain_produces_positive_skew():
    """Classic equity skew (OTM puts > OTM calls) → iv_skew_pp > 0."""
    chain = _build_skewed_chain(100.0)
    result = canonical_iv_state(chain, spot=100.0, days_to_exp=3.0)

    if result["source"] != "unified_iv_surface":
        FAILED.append(f"test_skew: source not unified_iv_surface: {result['source']}")
        return
    skew = result["iv_skew_pp"]
    if skew is None:
        FAILED.append("test_skew: iv_skew_pp None on a chain that should have skew")
        return
    assert_true(skew > 0, f"equity skew should be positive (puts > calls), got {skew}")
    PASSED.append("test_skewed_chain_produces_positive_skew")


def test_canonical_matches_direct_unified_iv_surface_call():
    """THE wrapper-consistency test. Build UnifiedIVSurface directly with the
    same inputs; assert canonical_iv_state returns identical representative_iv
    and atm_iv. If they ever drift, the wrapper has diverged from the canonical.

    This is the test pattern that should be standard for every canonical_X.
    Was added for canonical_gamma_flip (test_canonical_matches_direct_engine_call);
    here as the same discipline.
    """
    from engine_bridge import build_option_rows
    from options_exposure import ExposureEngine, UnifiedIVSurface

    spot = 100.0
    chain = _build_chain(spot)

    # Path A: canonical wrapper
    wrapper_result = canonical_iv_state(chain, spot=spot, days_to_exp=3.0)
    if wrapper_result["source"] != "unified_iv_surface":
        FAILED.append(f"test_consistency: wrapper failed: {wrapper_result['source']}")
        return

    # Path B: direct UnifiedIVSurface call with identical inputs
    rows = build_option_rows(chain, spot=spot, days_to_exp=3.0)
    engine = ExposureEngine(r=0.04)
    surface = UnifiedIVSurface(rows, engine)
    direct_rep_iv = surface.representative_iv(spot)
    direct_atm_iv = surface.strike_iv(spot, spot)

    assert_eq(wrapper_result["representative_iv"], direct_rep_iv,
              "wrapper representative_iv == direct UnifiedIVSurface.representative_iv")
    assert_eq(wrapper_result["atm_iv"], direct_atm_iv,
              "wrapper atm_iv == direct UnifiedIVSurface.strike_iv(spot, spot)")
    PASSED.append("test_canonical_matches_direct_unified_iv_surface_call")


def test_iv30_is_none():
    """iv30 stays None until a multi-expiration canonical lands."""
    chain = _build_chain(100.0)
    result = canonical_iv_state(chain, spot=100.0, days_to_exp=3.0)
    assert_is_none(result["iv30"], "iv30 None — multi-expiration not in scope")
    PASSED.append("test_iv30_is_none")


def test_empty_result_helper_shape():
    """Internal sanity: _empty_result returns the same shape as success."""
    err = _empty_result("test_source")
    assert_eq(err["source"], "test_source", "source set")
    assert_is_none(err["representative_iv"], "rep_iv None")
    assert_is_none(err["atm_iv"], "atm_iv None")
    assert_is_none(err["iv_skew_pp"], "skew None")
    assert_is_none(err["iv30"], "iv30 None")
    PASSED.append("test_empty_result_helper_shape")


# ───────────────────────────────────────────────────────────────────────
# Run all
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_empty_chain_returns_empty_result,
        test_invalid_spot_returns_empty_result,
        test_zero_dte_clamps_safely,
        test_flat_iv_chain_returns_flat_iv,
        test_skewed_chain_produces_positive_skew,
        test_canonical_matches_direct_unified_iv_surface_call,
        test_iv30_is_none,
        test_empty_result_helper_shape,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAILED.append(f"{t.__name__}: unexpected exception {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"PASSED: {len(PASSED)} / {len(tests)}")
    for p in PASSED:
        print(f"  ✓ {p}")
    if FAILED:
        print(f"\nFAILED: {len(FAILED)}")
        for f in FAILED:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print(f"\n{'='*60}")
        print("All tests passed.")


if __name__ == "__main__":
    main()
