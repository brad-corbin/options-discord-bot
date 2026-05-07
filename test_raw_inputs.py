"""
test_raw_inputs.py — Unit tests for raw_inputs.fetch_raw_inputs.

Uses a synthetic DataRouter that returns canned responses, so the test
runs without Schwab credentials, MarketData keys, or a network connection.

This is what testability looks like once the rebuild is in place: any
component that takes a data_router parameter can be tested by passing a
fake. No mocking of imports, no patching the world.
"""

from __future__ import annotations

import sys
from raw_inputs import fetch_raw_inputs, RawInputs, _pivot_chain_to_rows


# ───────────────────────────────────────────────────────────────────────
# Fakes — a DataRouter stand-in that returns canned data.
# ───────────────────────────────────────────────────────────────────────

class FakeDataRouter:
    """Minimal stand-in for the real DataRouter.

    Each method returns a canned response. Override individual methods
    in tests by setting class attributes.
    """
    def __init__(self,
                 spot=550.0,
                 chain=None,
                 quote=None,
                 bars=None,
                 raise_on=None):
        self._spot = spot
        self._chain = chain if chain is not None else _sample_chain_dict()
        self._quote = quote if quote is not None else {"totalVolume": 1_000_000, "avgVolume20d": 800_000}
        self._bars = bars if bars is not None else [
            {"o": 1.0, "h": 1.5, "l": 0.8, "c": 1.2, "v": 1000} for _ in range(504)
        ]
        # raise_on = set of method names that should raise instead of return
        self._raise_on = set(raise_on or [])

    def _maybe_raise(self, method):
        if method in self._raise_on:
            raise RuntimeError(f"FakeDataRouter forced failure on {method}")

    def get_spot(self, ticker, as_float_fn=None):
        self._maybe_raise("get_spot")
        return self._spot

    def get_chain(self, ticker, expiration, side=None, strike_limit=None, feed=None):
        self._maybe_raise("get_chain")
        return self._chain

    def get_stock_quote(self, ticker):
        self._maybe_raise("get_stock_quote")
        return self._quote

    def get_ohlc_bars(self, ticker, days=65):
        self._maybe_raise("get_ohlc_bars")
        return self._bars[:days] if days < len(self._bars) else self._bars


def _sample_chain_dict() -> dict:
    """Realistic MarketData chain response (dict-of-arrays).

    This is the SHAPE that flows through DataRouter — every field is an
    array, indexed in parallel. `engine_bridge.build_option_rows()` is
    designed to consume exactly this shape.
    """
    return {
        "s": "ok",
        "optionSymbol": ["SPY260509C00540000", "SPY260509C00545000",
                         "SPY260509C00550000", "SPY260509C00555000",
                         "SPY260509C00560000"],
        "strike": [540, 545, 550, 555, 560],
        "side": ["call", "call", "call", "call", "call"],
        "expiration": ["2026-05-09"] * 5,
        "openInterest": [100, 500, 2000, 800, 200],
        "volume": [50, 200, 500, 100, 50],
        "delta": [0.85, 0.65, 0.50, 0.35, 0.15],
        "gamma": [0.005, 0.020, 0.045, 0.020, 0.005],
        "iv": [0.25, 0.23, 0.22, 0.23, 0.25],
        "bid": [10.5, 6.0, 2.5, 0.5, 0.05],
        "ask": [10.7, 6.2, 2.7, 0.7, 0.10],
    }


# ───────────────────────────────────────────────────────────────────────
# Tests — plain-function "test_" style, runnable without pytest.
# ───────────────────────────────────────────────────────────────────────

PASSED = []
FAILED = []

def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    return True

def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition was False")
        return False
    return True

def assert_in(needle, haystack, msg):
    if needle not in haystack:
        FAILED.append(f"{msg}: {needle!r} not in {haystack!r}")
        return False
    return True


def test_happy_path():
    router = FakeDataRouter()
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)

    assert_eq(inp.ticker, "SPY", "ticker preserved")
    assert_eq(inp.spot, 550.0, "spot from router")
    assert_eq(inp.expiration, "2026-05-09", "expiration preserved")
    # Chain is RAW dict-of-arrays — same shape DataRouter returned
    assert_true(isinstance(inp.chain, dict), "chain stays as dict-of-arrays")
    assert_eq(len(inp.chain["strike"]), 5, "chain has 5 strikes")
    assert_eq(inp.chain["strike"][0], 540, "first strike")
    assert_eq(inp.chain["openInterest"][2], 2000, "ATM strike OI")
    assert_eq(len(inp.bars), 504, "bars at default lookback")
    assert_eq(inp.quote["totalVolume"], 1_000_000, "quote present")
    assert_true(inp.is_clean, "is_clean True on happy path")
    assert_eq(len(inp.fetch_errors), 0, "no fetch errors")
    PASSED.append("test_happy_path")


def test_chain_rows_helper_pivots():
    """The chain_rows() helper exists for legacy display code."""
    router = FakeDataRouter()
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)
    rows = inp.chain_rows
    assert_eq(len(rows), 5, "5 row dicts")
    assert_eq(rows[0]["strike"], 540, "first row strike")
    assert_eq(rows[2]["openInterest"], 2000, "ATM row OI")
    PASSED.append("test_chain_rows_helper_pivots")


def test_missing_ticker_raises():
    router = FakeDataRouter()
    try:
        fetch_raw_inputs("", "2026-05-09", data_router=router)
        FAILED.append("test_missing_ticker_raises: expected ValueError, got nothing")
    except ValueError:
        PASSED.append("test_missing_ticker_raises")


def test_missing_data_router_raises():
    try:
        fetch_raw_inputs("SPY", "2026-05-09", data_router=None)
        FAILED.append("test_missing_data_router_raises: expected ValueError, got nothing")
    except ValueError:
        PASSED.append("test_missing_data_router_raises")


def test_chain_failure_records_error():
    router = FakeDataRouter(raise_on={"get_chain"})
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)

    assert_eq(inp.spot, 550.0, "spot still fetched even though chain failed")
    assert_eq(inp.chain, {}, "chain empty dict on failure")
    assert_true(not inp.is_clean, "is_clean False")
    assert_eq(len(inp.fetch_errors), 1, "exactly one fetch error")
    assert_eq(inp.fetch_errors[0][0], "get_chain", "error tagged correctly")
    PASSED.append("test_chain_failure_records_error")


def test_chain_error_envelope_treated_as_failure():
    """MarketData ({"s": "error"}) responses get caught, not silently passed."""
    err_chain = {"s": "error", "errmsg": "no contracts found"}
    router = FakeDataRouter(chain=err_chain)
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)
    assert_eq(inp.chain, {}, "error envelope replaced with empty dict")
    assert_true(not inp.is_clean, "fetch error recorded")
    assert_eq(inp.fetch_errors[0][0], "get_chain", "tagged as get_chain failure")
    PASSED.append("test_chain_error_envelope_treated_as_failure")


def test_multiple_failures_all_recorded():
    router = FakeDataRouter(raise_on={"get_chain", "get_ohlc_bars", "get_stock_quote"})
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)

    assert_eq(inp.spot, 550.0, "spot still fetched")
    assert_eq(inp.chain, {}, "chain empty dict")
    assert_eq(inp.bars, [], "bars empty")
    assert_eq(inp.quote, {}, "quote empty")
    assert_true(not inp.is_clean, "is_clean False")
    assert_eq(len(inp.fetch_errors), 3, "three fetch errors")
    error_ops = {e[0] for e in inp.fetch_errors}
    assert_eq(error_ops, {"get_chain", "get_ohlc_bars", "get_stock_quote"}, "all three ops recorded")
    PASSED.append("test_multiple_failures_all_recorded")


def test_zero_spot_is_recorded_as_error():
    router = FakeDataRouter(spot=0.0)
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)

    assert_eq(inp.spot, 0.0, "spot is 0")
    assert_true(not inp.is_clean, "is_clean False")
    assert_eq(inp.fetch_errors[0][0], "get_spot", "spot error recorded")
    PASSED.append("test_zero_spot_is_recorded_as_error")


def test_immutability():
    router = FakeDataRouter()
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router)
    try:
        inp.spot = 999.0  # type: ignore
        FAILED.append("test_immutability: expected FrozenInstanceError")
    except Exception as e:
        if "frozen" not in str(e).lower() and "FrozenInstanceError" not in type(e).__name__:
            FAILED.append(f"test_immutability: wrong exception type: {e}")
            return
        PASSED.append("test_immutability")


def test_pivot_helper_handles_empty():
    assert_eq(_pivot_chain_to_rows(None), [], "None → []")
    assert_eq(_pivot_chain_to_rows({}), [], "empty dict → []")
    assert_eq(_pivot_chain_to_rows("garbage"), [], "wrong type → []")
    PASSED.append("test_pivot_helper_handles_empty")


def test_pivot_helper_pivots_correctly():
    pivoted = _pivot_chain_to_rows(_sample_chain_dict())
    assert_eq(len(pivoted), 5, "5 rows")
    assert_eq(pivoted[0]["strike"], 540, "row 0 strike")
    assert_eq(pivoted[4]["strike"], 560, "row 4 strike")
    assert_eq(pivoted[2]["delta"], 0.50, "ATM delta")
    PASSED.append("test_pivot_helper_pivots_correctly")


def test_iv_surface_passthrough():
    router = FakeDataRouter()
    fake_surface = {"version": 1, "data": "..."}
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router, iv_surface=fake_surface)
    assert_eq(inp.iv_surface, fake_surface, "iv_surface passed through unchanged")
    PASSED.append("test_iv_surface_passthrough")


def test_custom_bars_days():
    router = FakeDataRouter()
    inp = fetch_raw_inputs("SPY", "2026-05-09", data_router=router, bars_days=65)
    assert_eq(len(inp.bars), 65, "custom bars_days respected")
    PASSED.append("test_custom_bars_days")


# ───────────────────────────────────────────────────────────────────────
# Run all tests
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_happy_path,
        test_chain_rows_helper_pivots,
        test_missing_ticker_raises,
        test_missing_data_router_raises,
        test_chain_failure_records_error,
        test_chain_error_envelope_treated_as_failure,
        test_multiple_failures_all_recorded,
        test_zero_spot_is_recorded_as_error,
        test_immutability,
        test_pivot_helper_handles_empty,
        test_pivot_helper_pivots_correctly,
        test_iv_surface_passthrough,
        test_custom_bars_days,
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
        print(f"All tests passed.")


if __name__ == "__main__":
    main()
