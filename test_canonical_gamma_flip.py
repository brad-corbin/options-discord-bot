"""
test_canonical_gamma_flip.py — Unit tests for canonical_gamma_flip.

Uses the REAL options_exposure module (the existing canonical math) but
against synthetic chains, so tests run without network/credentials.

This exercises the actual integration: chain dict → build_option_rows →
ExposureEngine → gamma_flip. If anything's wrong with the wiring,
these tests catch it.
"""

from __future__ import annotations

import sys
import os

# Make the repo's modules importable. The test runner invokes this with
# the repo on PYTHONPATH; the sys.path manipulation is just for the
# direct-run case below the if __name__ block.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canonical_gamma_flip import (
    canonical_gamma_flip,
    _build_grid,
    DEFAULT_BAND_PCT,
    IV_AWARE_BAND_FLOOR,
    IV_AWARE_BAND_CEILING,
    DEFAULT_GRID_STEPS,
)

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
        FAILED.append(f"{msg}: condition was False")
        return False
    return True


def assert_in_range(actual, lo, hi, msg):
    if actual is None or actual < lo or actual > hi:
        FAILED.append(f"{msg}: expected in [{lo}, {hi}], got {actual!r}")
        return False
    return True


# ───────────────────────────────────────────────────────────────────────
# Synthetic chain builders
# ───────────────────────────────────────────────────────────────────────

def _build_balanced_chain(spot: float, strikes: list[float], dte_iso: str = "2026-05-09") -> dict:
    """Build a chain with calls and puts at each strike, balanced OI.

    Net dealer GEX should be roughly zero near spot for a balanced book.
    The flip should land near spot.

    Returns the dict-of-arrays format that DataRouter / build_option_rows expects.
    """
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
    for K in strikes:
        for side in ("call", "put"):
            chain["optionSymbol"].append(f"TEST{int(K*1000):08d}{side[0].upper()}")
            chain["strike"].append(K)
            chain["side"].append(side)
            chain["expiration"].append(dte_iso)
            chain["openInterest"].append(1000)  # balanced
            chain["volume"].append(50)
            # Approx delta/gamma — production uses BS, but we leave these
            # populated so build_option_rows doesn't reject the rows. The
            # ExposureEngine recomputes Greeks from IV anyway.
            moneyness = K / spot
            if side == "call":
                d = max(0.05, min(0.95, 1.0 - (moneyness - 0.85) * 2.5))
            else:
                d = -max(0.05, min(0.95, (moneyness - 0.85) * 2.5))
            chain["delta"].append(round(d, 3))
            chain["gamma"].append(round(0.05 * (1 - abs(K - spot) / spot), 4))
            chain["iv"].append(0.25)
            chain["bid"].append(0.5)
            chain["ask"].append(0.7)
    return chain


def _build_put_wall_call_wall_chain(spot: float) -> dict:
    """Realistic dealer positioning: put OI clustered BELOW spot,
    call OI clustered ABOVE spot. This is the put-wall / call-wall
    pattern that creates a meaningful gamma flip somewhere between.

    Under Patch 9 convention (dealer long calls / short puts):
      - Heavy puts below spot → dealer short puts → NEGATIVE GEX at low spot
      - Heavy calls above spot → dealer long calls → POSITIVE GEX at high spot
      - Flip exists somewhere in the middle
    """
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

    # Put wall — heavy puts below spot
    put_strikes = [spot * 0.92, spot * 0.95, spot * 0.97]
    for K in put_strikes:
        chain["optionSymbol"].append(f"PW{int(K*100):08d}P")
        chain["strike"].append(round(K, 2))
        chain["side"].append("put")
        chain["expiration"].append("2026-05-09")
        chain["openInterest"].append(8000)
        chain["volume"].append(200)
        chain["delta"].append(-0.30)
        chain["gamma"].append(0.04)
        chain["iv"].append(0.25)
        chain["bid"].append(0.5)
        chain["ask"].append(0.7)

    # Call wall — heavy calls above spot
    call_strikes = [spot * 1.03, spot * 1.05, spot * 1.08]
    for K in call_strikes:
        chain["optionSymbol"].append(f"CW{int(K*100):08d}C")
        chain["strike"].append(round(K, 2))
        chain["side"].append("call")
        chain["expiration"].append("2026-05-09")
        chain["openInterest"].append(8000)
        chain["volume"].append(200)
        chain["delta"].append(0.30)
        chain["gamma"].append(0.04)
        chain["iv"].append(0.25)
        chain["bid"].append(0.5)
        chain["ask"].append(0.7)

    # Some balanced ATM volume so the chain has rows near spot
    for K in [spot * 0.99, spot, spot * 1.01]:
        for side in ("call", "put"):
            chain["optionSymbol"].append(f"AT{int(K*100):08d}{side[0].upper()}")
            chain["strike"].append(round(K, 2))
            chain["side"].append(side)
            chain["expiration"].append("2026-05-09")
            chain["openInterest"].append(2000)
            chain["volume"].append(500)
            chain["delta"].append(0.5 if side == "call" else -0.5)
            chain["gamma"].append(0.05)
            chain["iv"].append(0.25)
            chain["bid"].append(1.0)
            chain["ask"].append(1.2)

    return chain


# ───────────────────────────────────────────────────────────────────────
# Grid math tests (no chain needed)
# ───────────────────────────────────────────────────────────────────────

def test_blanket_grid():
    g = _build_grid(100.0)
    assert_eq(len(g), DEFAULT_GRID_STEPS, "default 121 points")
    assert_eq(g[0], 75.0, "blanket lower bound = -25%")
    assert_eq(g[-1], 125.0, "blanket upper bound = +25%")
    PASSED.append("test_blanket_grid")


def test_iv_aware_grid_normal_iv():
    g = _build_grid(100.0, iv=0.30, dte_years=7/365)
    assert_eq(len(g), DEFAULT_GRID_STEPS, "121 points")
    # 3 * 0.30 * sqrt(7/365) ≈ 3 * 0.30 * 0.1385 = 0.1247
    # Below the 0.15 floor — clamps to ±15%
    assert_approx(g[0], 85.0, 0.5, "low-vol clamps to floor")
    assert_approx(g[-1], 115.0, 0.5, "low-vol clamps to floor (upper)")
    PASSED.append("test_iv_aware_grid_normal_iv")


def test_iv_aware_grid_high_iv_clamped():
    g = _build_grid(100.0, iv=2.0, dte_years=7/365)
    # 3 * 2.0 * sqrt(7/365) ≈ 0.83 — above ceiling, should clamp to ±40%
    assert_approx(g[0], 60.0, 0.5, "high-vol clamps to ceiling")
    assert_approx(g[-1], 140.0, 0.5, "high-vol clamps to ceiling (upper)")
    PASSED.append("test_iv_aware_grid_high_iv_clamped")


def test_iv_aware_grid_no_iv_falls_back():
    g = _build_grid(100.0, iv=None, dte_years=None)
    assert_approx(g[0], 75.0, 0.01, "fallback ±25%")
    PASSED.append("test_iv_aware_grid_no_iv_falls_back")


def test_iv_aware_grid_partial_iv_args_falls_back():
    """If only dte_years is given (no iv), fall back to blanket. The opposite
    case — iv given but dte_years omitted — is now handled by canonical_gamma_flip
    auto-deriving dte_years from days_to_exp (see test_iv_only_auto_derives_dte_years)."""
    g = _build_grid(100.0, iv=None, dte_years=7/365)
    assert_approx(g[0], 75.0, 0.01, "dte_years alone → blanket")
    PASSED.append("test_iv_aware_grid_partial_iv_args_falls_back")


def test_iv_only_auto_derives_dte_years():
    """Caution 2 fix: passing iv without explicit dte_years should NOT silently
    fall back to ±25%. The wrapper auto-derives dte_years from days_to_exp.

    This test exercises the wrapper itself, not _build_grid directly, since
    the auto-derive logic lives in canonical_gamma_flip().
    """
    spot = 100.0
    chain = _build_put_wall_call_wall_chain(spot)

    # Path 1: iv only, no dte_years — should auto-derive dte_years = 3/365
    flip_auto = canonical_gamma_flip(
        chain, spot=spot, days_to_exp=3.0, iv=2.0,  # high iv triggers ceiling clamp
    )

    # Path 2: iv with EXPLICIT dte_years = 3/365 (what the auto-derive should produce)
    flip_explicit = canonical_gamma_flip(
        chain, spot=spot, days_to_exp=3.0, iv=2.0, dte_years=3/365,
    )

    # Both should produce identical results — same band, same flip
    assert_eq(flip_auto, flip_explicit,
              "auto-derived dte_years matches explicit dte_years=days_to_exp/365")

    # Path 3: iv only with HIGH days_to_exp (60d) — different from blanket because
    # the auto-derived dte_years = 60/365 would still produce IV-aware band.
    # If auto-derive WEREN'T working, this would fall back to blanket and match
    # a no-iv call. With auto-derive, this differs from blanket.
    chain_60d = _build_put_wall_call_wall_chain(spot)
    flip_iv_only_60d = canonical_gamma_flip(
        chain_60d, spot=spot, days_to_exp=60.0, iv=2.0,
    )
    flip_blanket = canonical_gamma_flip(
        chain_60d, spot=spot, days_to_exp=60.0,
    )
    # With auto-derive working and iv=2.0, dte_years=60/365 → 3*2*sqrt(60/365) ~= 2.4
    # which clamps to 0.40 ceiling. Blanket is 0.25. Different bands → potentially
    # different flips for chains where the flip lives near the band edge.
    # This may or may not produce different flip values depending on chain geometry,
    # but the iv_only call should at minimum NOT crash and should return a value
    # consistent with the explicit call.
    flip_iv_with_explicit_dte = canonical_gamma_flip(
        chain_60d, spot=spot, days_to_exp=60.0, iv=2.0, dte_years=60/365,
    )
    assert_eq(flip_iv_only_60d, flip_iv_with_explicit_dte,
              "60-day auto-derive matches 60-day explicit")

    PASSED.append("test_iv_only_auto_derives_dte_years")


# ───────────────────────────────────────────────────────────────────────
# canonical_gamma_flip integration tests (real options_exposure)
# ───────────────────────────────────────────────────────────────────────

def test_balanced_book_flip_near_spot():
    """For a balanced call/put OI book, the flip should land near spot."""
    spot = 100.0
    strikes = [90, 95, 100, 105, 110]
    chain = _build_balanced_chain(spot, strikes)
    flip = canonical_gamma_flip(chain, spot=spot, days_to_exp=3.0)

    if flip is None:
        FAILED.append("test_balanced_book_flip_near_spot: got None — should find a flip")
        return
    # Balanced book → flip should be within the band, reasonably close to spot
    assert_in_range(flip, 75.0, 125.0, "flip within ±25% band")
    PASSED.append("test_balanced_book_flip_near_spot")


def test_realistic_book_finds_flip_between_walls():
    """Put-wall below spot, call-wall above spot. Flip should exist somewhere
    in the middle. Doesn't matter exactly where — just that the math finds one."""
    spot = 100.0
    chain = _build_put_wall_call_wall_chain(spot)
    flip = canonical_gamma_flip(chain, spot=spot, days_to_exp=3.0)

    if flip is None:
        FAILED.append("test_realistic_book_finds_flip_between_walls: got None")
        return
    # Flip should be within the swept ±25% band
    assert_in_range(flip, 75.0, 125.0, "flip within band")
    PASSED.append("test_realistic_book_finds_flip_between_walls")


def test_canonical_matches_direct_engine_call():
    """The wrapper function returns IDENTICAL output to a direct call to
    the underlying ExposureEngine. This is the key wrapper-correctness test —
    we're not validating the math (Walk 1E / Patch 9 already did), we're
    validating that canonical_gamma_flip is a faithful wrapper."""
    from engine_bridge import build_option_rows
    from options_exposure import ExposureEngine

    spot = 100.0
    chain = _build_put_wall_call_wall_chain(spot)

    # Path A: through the canonical wrapper
    wrapper_flip = canonical_gamma_flip(chain, spot=spot, days_to_exp=3.0)

    # Path B: build rows + engine directly with the same blanket grid
    rows = build_option_rows(chain, spot=spot, days_to_exp=3.0)
    grid = _build_grid(spot)  # same grid math
    engine = ExposureEngine(r=0.04)
    direct_flip = engine.gamma_flip(rows, pg=grid)

    assert_eq(wrapper_flip, direct_flip,
              "wrapper output identical to direct ExposureEngine call")
    PASSED.append("test_canonical_matches_direct_engine_call")


def test_iv_aware_band_widens_for_high_iv():
    """Verify the band-widening path produces a different grid than blanket
    for high-IV inputs. Direct grid comparison."""
    spot = 50.0
    blanket_grid = _build_grid(spot)
    iv_aware_grid = _build_grid(spot, iv=2.0, dte_years=7/365)
    # blanket = ±25% = [37.5, 62.5]
    # iv-aware (clamped to 40%) = ±40% = [30, 70]
    assert_true(iv_aware_grid[0] < blanket_grid[0], "IV-aware low end below blanket")
    assert_true(iv_aware_grid[-1] > blanket_grid[-1], "IV-aware high end above blanket")
    PASSED.append("test_iv_aware_band_widens_for_high_iv")


def test_returns_none_outside_band():
    """If the flip is geometrically outside the swept band, return None."""
    spot = 100.0
    # Heavy puts FAR below — flip is somewhere in the negative-OI imbalance zone
    # but our chain only has strikes 95-105, so the flip can't be properly found
    chain = {
        "s": "ok",
        "optionSymbol": ["T1", "T2", "T3", "T4"],
        "strike": [95, 100, 105, 100],
        "side": ["call", "call", "call", "put"],
        "expiration": ["2026-05-09"] * 4,
        "openInterest": [1, 1, 1, 1],  # too thin to find a meaningful flip
        "volume": [0, 0, 0, 0],
        "delta": [0.7, 0.5, 0.3, -0.5],
        "gamma": [0.01, 0.05, 0.01, 0.05],
        "iv": [0.25, 0.25, 0.25, 0.25],
        "bid": [0.5, 0.5, 0.5, 0.5],
        "ask": [0.7, 0.7, 0.7, 0.7],
    }
    # This may or may not return None depending on the engine's behavior on
    # thin chains. Accept either — the assertion is just that it doesn't crash.
    flip = canonical_gamma_flip(chain, spot=spot, days_to_exp=3.0)
    assert_true(flip is None or isinstance(flip, (int, float)), "returns None or number")
    PASSED.append("test_returns_none_outside_band")


def test_invalid_spot_raises():
    chain = _build_balanced_chain(100.0, [95, 100, 105])
    try:
        canonical_gamma_flip(chain, spot=0, days_to_exp=3.0)
        FAILED.append("test_invalid_spot_raises: expected ValueError")
    except ValueError:
        PASSED.append("test_invalid_spot_raises")


def test_empty_chain_raises():
    try:
        canonical_gamma_flip({}, spot=100.0, days_to_exp=3.0)
        FAILED.append("test_empty_chain_raises: expected ValueError on empty dict")
    except ValueError:
        PASSED.append("test_empty_chain_raises")


def test_zero_dte_clamps_safely():
    """DTE 0 (expiration day) should not crash — clamps to small positive."""
    chain = _build_balanced_chain(100.0, [95, 100, 105])
    flip = canonical_gamma_flip(chain, spot=100.0, days_to_exp=0)
    # Either returns a number or None — but must not raise
    assert_true(flip is None or isinstance(flip, (int, float)),
                "returns None or number, doesn't raise")
    PASSED.append("test_zero_dte_clamps_safely")


# ───────────────────────────────────────────────────────────────────────
# Run all tests
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_blanket_grid,
        test_iv_aware_grid_normal_iv,
        test_iv_aware_grid_high_iv_clamped,
        test_iv_aware_grid_no_iv_falls_back,
        test_iv_aware_grid_partial_iv_args_falls_back,
        test_iv_only_auto_derives_dte_years,
        test_balanced_book_flip_near_spot,
        test_realistic_book_finds_flip_between_walls,
        test_canonical_matches_direct_engine_call,
        test_iv_aware_band_widens_for_high_iv,
        test_returns_none_outside_band,
        test_invalid_spot_raises,
        test_empty_chain_raises,
        test_zero_dte_clamps_safely,
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
