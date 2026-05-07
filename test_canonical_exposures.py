"""
test_canonical_exposures.py — Unit tests for canonical_exposures.

Includes wrapper-consistency test against direct ExposureEngine.compute()
call. Same discipline as canonical_gamma_flip and canonical_iv_state.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canonical_exposures import canonical_exposures, _empty_result, _safe_float

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition False")
        return False
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    return True


def assert_is_number(actual, msg):
    if actual is None or not isinstance(actual, (int, float)):
        FAILED.append(f"{msg}: expected number, got {actual!r}")
        return False
    return True


# ───────────────────────────────────────────────────────────────────────
# Synthetic chain builders
# ───────────────────────────────────────────────────────────────────────

def _build_realistic_chain(spot: float) -> dict:
    """Put-wall below / call-wall above pattern. Produces a flip and walls
    that exist within the swept band."""
    chain = {
        "s": "ok",
        "optionSymbol": [], "strike": [], "side": [], "expiration": [],
        "openInterest": [], "volume": [], "delta": [], "gamma": [],
        "iv": [], "bid": [], "ask": [],
    }
    # Put wall
    for K in [spot * 0.92, spot * 0.95, spot * 0.97]:
        chain["optionSymbol"].append(f"PW{int(K*100):08d}P")
        chain["strike"].append(round(K, 2))
        chain["side"].append("put")
        chain["expiration"].append("2026-05-09")
        chain["openInterest"].append(8000)
        chain["volume"].append(200)
        chain["delta"].append(-0.30); chain["gamma"].append(0.04)
        chain["iv"].append(0.25); chain["bid"].append(0.5); chain["ask"].append(0.7)

    # Call wall
    for K in [spot * 1.03, spot * 1.05, spot * 1.08]:
        chain["optionSymbol"].append(f"CW{int(K*100):08d}C")
        chain["strike"].append(round(K, 2))
        chain["side"].append("call")
        chain["expiration"].append("2026-05-09")
        chain["openInterest"].append(8000)
        chain["volume"].append(200)
        chain["delta"].append(0.30); chain["gamma"].append(0.04)
        chain["iv"].append(0.25); chain["bid"].append(0.5); chain["ask"].append(0.7)

    # ATM volume both sides
    for K in [spot * 0.99, spot, spot * 1.01]:
        for side in ("call", "put"):
            chain["optionSymbol"].append(f"AT{int(K*100):08d}{side[0].upper()}")
            chain["strike"].append(round(K, 2))
            chain["side"].append(side)
            chain["expiration"].append("2026-05-09")
            chain["openInterest"].append(2000); chain["volume"].append(500)
            chain["delta"].append(0.5 if side == "call" else -0.5)
            chain["gamma"].append(0.05); chain["iv"].append(0.25)
            chain["bid"].append(1.0); chain["ask"].append(1.2)

    return chain


# ───────────────────────────────────────────────────────────────────────
# Empty/edge case tests
# ───────────────────────────────────────────────────────────────────────

def test_empty_chain_returns_empty_result():
    result = canonical_exposures({}, spot=100.0, days_to_exp=3.0)
    assert_eq(result["source"], "empty_chain", "source flagged empty_chain")
    assert_is_none(result["net"]["gex"], "net.gex None")
    assert_is_none(result["walls"]["call_wall"], "walls.call_wall None")
    PASSED.append("test_empty_chain_returns_empty_result")


def test_invalid_spot_returns_empty_result():
    result = canonical_exposures(_build_realistic_chain(100.0), spot=0, days_to_exp=3.0)
    assert_eq(result["source"], "invalid_spot", "source flagged invalid_spot")
    PASSED.append("test_invalid_spot_returns_empty_result")


def test_zero_dte_clamps_safely():
    """DTE 0 should clamp to small positive, not crash."""
    result = canonical_exposures(_build_realistic_chain(100.0), spot=100.0, days_to_exp=0)
    assert_true(result["source"] in ("exposure_engine", "compute_failed"),
                f"reasonable source on dte=0: {result['source']}")
    PASSED.append("test_zero_dte_clamps_safely")


def test_empty_result_helper_shape():
    """Internal sanity: _empty_result returns the same shape as success."""
    err = _empty_result("test_source")
    assert_eq(err["source"], "test_source", "source set")
    assert_eq(set(err["net"].keys()),
              {"gex", "dex", "vanna", "charm", "volga", "speed", "theta", "rho"},
              "net has all 8 Greek keys")
    assert_eq(set(err["walls"].keys()),
              {"call_wall", "put_wall", "gamma_wall", "vol_trigger"},
              "walls has all 4 wall keys")
    PASSED.append("test_empty_result_helper_shape")


# ───────────────────────────────────────────────────────────────────────
# Live integration tests (real options_exposure)
# ───────────────────────────────────────────────────────────────────────

def test_realistic_chain_produces_all_greek_aggregates():
    chain = _build_realistic_chain(100.0)
    result = canonical_exposures(chain, spot=100.0, days_to_exp=3.0)

    if result["source"] != "exposure_engine":
        FAILED.append(f"test_greeks: source not exposure_engine: {result['source']}")
        return

    net = result["net"]
    assert_is_number(net["gex"], "gex is a number")
    assert_is_number(net["dex"], "dex is a number")
    assert_is_number(net["vanna"], "vanna is a number")
    assert_is_number(net["charm"], "charm is a number")
    PASSED.append("test_realistic_chain_produces_all_greek_aggregates")


def test_realistic_chain_produces_walls():
    """Walls aren't wired into BotState yet, but the canonical returns them."""
    chain = _build_realistic_chain(100.0)
    result = canonical_exposures(chain, spot=100.0, days_to_exp=3.0)

    if result["source"] != "exposure_engine":
        FAILED.append(f"test_walls: source not exposure_engine: {result['source']}")
        return

    walls = result["walls"]
    # Walls may be None individually (no clear winner) but the keys must exist
    assert_true("call_wall" in walls, "call_wall key present")
    assert_true("put_wall" in walls, "put_wall key present")
    assert_true("gamma_wall" in walls, "gamma_wall key present")

    # On a put-wall/call-wall chain, at least call_wall and put_wall should
    # be populated (the pattern was designed to produce them).
    assert_is_number(walls["call_wall"], "call_wall populated")
    assert_is_number(walls["put_wall"], "put_wall populated")
    PASSED.append("test_realistic_chain_produces_walls")


def test_canonical_matches_direct_compute_call():
    """THE wrapper-consistency test. canonical_exposures must return the same
    `net` aggregates as a direct ExposureEngine.compute() call.

    Same discipline as test_canonical_iv_state's test_canonical_matches_direct...
    """
    from engine_bridge import build_option_rows
    from options_exposure import ExposureEngine

    spot = 100.0
    chain = _build_realistic_chain(spot)

    # Path A: canonical wrapper
    wrapper_result = canonical_exposures(chain, spot=spot, days_to_exp=3.0)
    if wrapper_result["source"] != "exposure_engine":
        FAILED.append(f"test_consistency: wrapper failed: {wrapper_result['source']}")
        return

    # Path B: direct ExposureEngine.compute() with identical inputs
    rows = build_option_rows(chain, spot=spot, days_to_exp=3.0)
    engine = ExposureEngine(r=0.04)
    direct_result = engine.compute(rows)

    # Compare every Greek aggregate
    for greek in ("gex", "dex", "vanna", "charm"):
        wrapper_val = wrapper_result["net"][greek]
        direct_val = direct_result["net"][greek]
        assert_eq(wrapper_val, direct_val,
                  f"net.{greek} matches direct compute()")

    # Compare walls too — they come from the same compute() pass
    for wall in ("call_wall", "put_wall", "gamma_wall"):
        wrapper_val = wrapper_result["walls"][wall]
        direct_val = direct_result["walls"][wall]
        assert_eq(wrapper_val, direct_val,
                  f"walls.{wall} matches direct compute()")

    PASSED.append("test_canonical_matches_direct_compute_call")


def test_call_dominant_chain_produces_positive_gex():
    """Dealer-long-calls convention (post-Patch-9): a chain dominated by
    call OI should produce positive net GEX. This is a sanity check that
    we're inheriting the right convention."""
    spot = 100.0
    chain = _build_realistic_chain(spot)
    # Triple call OI to make the book strongly call-dominant
    for i, side in enumerate(chain["side"]):
        if side == "call":
            chain["openInterest"][i] *= 5

    result = canonical_exposures(chain, spot=spot, days_to_exp=3.0)
    if result["source"] != "exposure_engine":
        FAILED.append(f"test_call_dom: source not exposure_engine: {result['source']}")
        return

    gex = result["net"]["gex"]
    assert_is_number(gex, "gex computed")
    assert_true(gex > 0,
                f"call-dominant book → positive net GEX (Patch 9 convention), got {gex}")
    PASSED.append("test_call_dominant_chain_produces_positive_gex")


def test_safe_float_handles_garbage():
    """Internal helper sanity."""
    assert_eq(_safe_float(None), None, "None → None")
    assert_eq(_safe_float(1.5), 1.5, "float passes through")
    assert_eq(_safe_float(2), 2.0, "int → float")
    assert_eq(_safe_float("3.14"), 3.14, "string-number → float")
    assert_eq(_safe_float("garbage"), None, "garbage → None")
    assert_eq(_safe_float([1, 2]), None, "list → None")
    PASSED.append("test_safe_float_handles_garbage")


# ───────────────────────────────────────────────────────────────────────
# Run all
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_empty_chain_returns_empty_result,
        test_invalid_spot_returns_empty_result,
        test_zero_dte_clamps_safely,
        test_empty_result_helper_shape,
        test_realistic_chain_produces_all_greek_aggregates,
        test_realistic_chain_produces_walls,
        test_canonical_matches_direct_compute_call,
        test_call_dominant_chain_produces_positive_gex,
        test_safe_float_handles_garbage,
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
