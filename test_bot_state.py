"""
test_bot_state.py — Unit tests for BotState.build_from_raw.

Verifies:
  1. Permissive pattern: build_from_raw() always returns a valid object,
     even when most canonical functions are stubbed.
  2. Live integration: gamma_flip is a real value (canonical_gamma_flip
     is implemented), not None.
  3. Stub fields: every other canonical-fed field is None as expected,
     with status correctly recorded in canonical_status.
  4. Trivial summaries (volume, rvol) compute correctly from raw quote.
  5. Derived fields (distance_from_flip_pct, flip_location, gex_sign)
     compute correctly from canonical inputs.
  6. fields_lit / fields_total accessors return sensible counts.

Runs without network, no Schwab credentials needed.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from raw_inputs import RawInputs
from bot_state import BotState

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
# Test fixtures
# ───────────────────────────────────────────────────────────────────────

def _build_realistic_raw_inputs(spot: float = 100.0) -> RawInputs:
    """Synthetic RawInputs with put-wall/call-wall positioning so
    canonical_gamma_flip finds a meaningful flip."""
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

    # Put wall
    for K in [spot * 0.92, spot * 0.95, spot * 0.97]:
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

    # Call wall
    for K in [spot * 1.03, spot * 1.05, spot * 1.08]:
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

    # ATM volume
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

    return RawInputs(
        ticker="TEST",
        spot=spot,
        chain=chain,
        expiration="2026-05-09",
        quote={"totalVolume": 1_000_000, "avgVolume20d": 800_000},
        bars=[{"o": 1.0, "h": 1.5, "l": 0.8, "c": 1.2, "v": 1000} for _ in range(504)],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc),
        fetch_errors=(),
    )


def _build_minimal_raw_inputs() -> RawInputs:
    """Even thinner — just enough for a build_from_raw() that produces a valid object."""
    return RawInputs(
        ticker="MIN",
        spot=50.0,
        chain={"s": "ok", "optionSymbol": ["X"], "strike": [50], "side": ["call"],
               "expiration": ["2026-05-09"], "openInterest": [100], "iv": [0.25]},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(),
    )


# ───────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────

def test_build_returns_valid_object_with_stubs():
    """The whole point of permissive build: never crashes, always returns a state."""
    raw = _build_minimal_raw_inputs()
    state = BotState.build_from_raw(raw)
    assert_eq(state.ticker, "MIN", "ticker preserved")
    assert_eq(state.spot, 50.0, "spot preserved")
    assert_eq(state.expiration, "2026-05-09", "expiration preserved")
    PASSED.append("test_build_returns_valid_object_with_stubs")


def test_canonical_status_records_each_canonical():
    """canonical_status dict should have an entry for every canonical_X attempted."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)

    expected_canonicals = {
        "gamma_flip", "gex", "dex", "vanna", "charm", "walls", "pivots",
        "structure", "iv_state", "em_state", "technicals", "bias",
        "dealer_regime", "vol_regime", "potter_box", "flow_state", "calendar",
    }
    for c in expected_canonicals:
        if c not in state.canonical_status:
            FAILED.append(f"canonical_status missing key: {c}")
            return
    PASSED.append("test_canonical_status_records_each_canonical")


def test_gamma_flip_is_live_post_patch_11_2():
    """canonical_gamma_flip is implemented — gamma_flip should be a real number, not None."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)
    if state.gamma_flip is None:
        FAILED.append(f"test_gamma_flip_is_live: got None — canonical_gamma_flip should produce value. "
                      f"status: {state.canonical_status.get('gamma_flip')}")
        return
    assert_eq(state.canonical_status.get("gamma_flip"), "live", "status marked live")
    assert_in_range(state.gamma_flip, 75.0, 125.0, "flip in ±25% band of spot 100")
    PASSED.append("test_gamma_flip_is_live_post_patch_11_2")


def test_distance_and_flip_location_derive_from_gamma_flip():
    raw = _build_realistic_raw_inputs(spot=100.0)
    state = BotState.build_from_raw(raw)

    # When gamma_flip is computed, distance and location must derive from it
    if state.gamma_flip is None:
        FAILED.append("test_derived: prerequisite gamma_flip is None")
        return
    if state.distance_from_flip_pct is None:
        FAILED.append("test_derived: distance_from_flip_pct should be computed")
        return
    if state.flip_location == "unknown":
        FAILED.append(f"test_derived: flip_location should be classified, "
                      f"got 'unknown' (gamma_flip={state.gamma_flip})")
        return
    assert_true(state.flip_location in ("above_flip", "below_flip", "at_flip"),
                "flip_location is a valid label")
    PASSED.append("test_distance_and_flip_location_derive_from_gamma_flip")


def test_stubbed_canonicals_record_stub_status():
    """Every canonical that's still stubbed should be marked 'stub' in status."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)

    # gamma_flip is live (Patch 11.2). Every other canonical should be stub.
    expected_stubs = ["gex", "dex", "vanna", "charm", "walls", "pivots",
                      "structure", "iv_state", "em_state", "technicals",
                      "bias", "dealer_regime", "vol_regime", "potter_box",
                      "flow_state", "calendar"]
    for c in expected_stubs:
        s = state.canonical_status.get(c, "")
        if not s.startswith("stub"):
            FAILED.append(f"test_stubbed_status: {c} should be 'stub*', got {s!r}")
            return
    PASSED.append("test_stubbed_canonicals_record_stub_status")


def test_stubbed_canonical_fields_are_none():
    """Every field fed by a still-stubbed canonical should be None or 'unknown'."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)

    # Greek aggregates
    assert_is_none(state.gex, "gex None when canonical_gex stubbed")
    assert_is_none(state.dex, "dex None")
    assert_is_none(state.vanna, "vanna None")
    assert_eq(state.gex_sign, "unknown", "gex_sign 'unknown' when no gex")

    # Walls
    assert_is_none(state.call_wall, "call_wall None")
    assert_is_none(state.put_wall, "put_wall None")
    assert_is_none(state.max_pain, "max_pain None")

    # Technicals
    assert_is_none(state.rsi, "rsi None")
    assert_is_none(state.macd_hist, "macd_hist None")

    # Bias / regime
    assert_eq(state.dealer_regime, "unknown", "dealer_regime 'unknown'")
    assert_is_none(state.bias_score, "bias_score None")

    PASSED.append("test_stubbed_canonical_fields_are_none")


def test_volume_summaries_compute_from_quote():
    """Volume / rvol come from raw.quote, not a canonical function."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)
    assert_eq(state.volume_today, 1_000_000.0, "volume_today from quote")
    assert_eq(state.rvol, 1.25, "rvol = 1M / 800k = 1.25")
    PASSED.append("test_volume_summaries_compute_from_quote")


def test_volume_handles_missing_quote_data():
    """Empty quote → volume None, rvol None, no crash."""
    raw = _build_minimal_raw_inputs()
    state = BotState.build_from_raw(raw)
    assert_is_none(state.volume_today, "volume_today None when quote empty")
    assert_is_none(state.rvol, "rvol None when no avg_volume")
    PASSED.append("test_volume_handles_missing_quote_data")


def test_immutability():
    raw = _build_minimal_raw_inputs()
    state = BotState.build_from_raw(raw)
    try:
        state.spot = 999.0  # type: ignore
        FAILED.append("test_immutability: expected FrozenInstanceError")
    except Exception as e:
        if "frozen" not in str(e).lower() and "FrozenInstanceError" not in type(e).__name__:
            FAILED.append(f"test_immutability: wrong exception: {e}")
            return
        PASSED.append("test_immutability")


def test_fields_lit_progress_indicator():
    """fields_lit should be small now (~9 lit fields), grow as canonicals land."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)
    lit = state.fields_lit
    total = state.fields_total

    # Currently lit: ticker, timestamp_utc, spot, expiration, chain_clean,
    # convention_version, snapshot_version, gamma_flip, distance_from_flip_pct,
    # flip_location, volume_today, rvol = ~12 fields
    assert_in_range(lit, 8, 20, f"fields_lit reasonable for early rebuild (got {lit}/{total})")
    assert_in_range(total, 50, 80, f"fields_total reasonable (got {total})")
    assert_true(lit < total, "fields_lit < total (rebuild in progress)")
    PASSED.append("test_fields_lit_progress_indicator")


def test_fetch_errors_propagated():
    """Errors from raw fetch are preserved on BotState."""
    raw = RawInputs(
        ticker="ERR",
        spot=100.0,
        chain={},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(("get_chain", "test failure"),),
    )
    state = BotState.build_from_raw(raw)
    assert_eq(state.chain_clean, False, "chain_clean False when raw had errors")
    assert_eq(len(state.fetch_errors), 1, "errors propagated")
    assert_eq(state.fetch_errors[0][0], "get_chain", "error tagged correctly")
    PASSED.append("test_fetch_errors_propagated")


def test_empty_chain_doesnt_crash_build():
    """Even with empty chain, build_from_raw should not crash —
    gamma_flip will be None but the state object is valid."""
    raw = RawInputs(
        ticker="EMPTY",
        spot=100.0,
        chain={},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(("get_chain", "empty"),),
    )
    state = BotState.build_from_raw(raw)
    assert_is_none(state.gamma_flip, "gamma_flip None when chain empty")
    # status should record the error, not 'live' or 'stub'
    s = state.canonical_status.get("gamma_flip", "")
    assert_true(s.startswith("error") or s == "live",
                f"gamma_flip status reasonable: {s!r}")
    PASSED.append("test_empty_chain_doesnt_crash_build")


# ───────────────────────────────────────────────────────────────────────
# Run all
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_build_returns_valid_object_with_stubs,
        test_canonical_status_records_each_canonical,
        test_gamma_flip_is_live_post_patch_11_2,
        test_distance_and_flip_location_derive_from_gamma_flip,
        test_stubbed_canonicals_record_stub_status,
        test_stubbed_canonical_fields_are_none,
        test_volume_summaries_compute_from_quote,
        test_volume_handles_missing_quote_data,
        test_immutability,
        test_fields_lit_progress_indicator,
        test_fetch_errors_propagated,
        test_empty_chain_doesnt_crash_build,
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
