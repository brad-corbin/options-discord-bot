"""
test_prev_close_store.py — unit tests for the previous-close cache.

No network. The Schwab client is mocked; we verify the store calls
get_quote with the right symbol and parses closePrice correctly.
"""

from __future__ import annotations
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


class _FakeSchwabProvider:
    """Mock that returns a canned Schwab get_quote response."""
    def __init__(self, table: dict, raise_on=None):
        # table: ticker -> closePrice (or None to simulate parse failure)
        self.table = table
        self.raise_on = raise_on or set()
        self.calls = []

    def _schwab_get(self, method, *args, **kwargs):
        if method != "get_quote":
            raise RuntimeError(f"unexpected schwab method: {method}")
        symbol = args[0] if args else kwargs.get("symbol")
        self.calls.append(symbol)
        if symbol in self.raise_on:
            raise RuntimeError(f"simulated failure for {symbol}")
        close = self.table.get(symbol)
        if close is None:
            return {symbol: {"quote": {}}}  # missing closePrice
        return {symbol: {"quote": {"closePrice": close, "lastPrice": close + 1.0}}}


def test_get_unknown_returns_none():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    assert_is_none(store.get("AAPL"), "unknown ticker returns None")


def test_set_get_roundtrip():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    store.set("AAPL", 184.50)
    assert_eq(store.get("AAPL"), 184.50, "set/get roundtrip")


def test_ttl_expiry():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore(ttl_sec=0)  # immediate expiry
    store.set("AAPL", 184.50)
    time.sleep(0.01)
    assert_is_none(store.get("AAPL"), "expired entry returns None")


def test_ensure_fetches_missing_via_schwab():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    fake = _FakeSchwabProvider({"AAPL": 184.50, "MSFT": 412.30})
    missing = store.ensure(["AAPL", "MSFT"], schwab_provider=fake)
    assert_eq(missing, [], "all tickers fetched, none missing")
    assert_eq(sorted(fake.calls), ["AAPL", "MSFT"],
              "schwab called once per ticker")
    assert_eq(store.get("AAPL"), 184.50, "AAPL prev_close cached")
    assert_eq(store.get("MSFT"), 412.30, "MSFT prev_close cached")


def test_ensure_skips_already_cached():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    store.set("AAPL", 184.50)
    fake = _FakeSchwabProvider({"AAPL": 999.99})  # would-be wrong value
    store.ensure(["AAPL"], schwab_provider=fake)
    assert_eq(fake.calls, [], "ensure skips cached tickers")
    assert_eq(store.get("AAPL"), 184.50, "cached value preserved")


def test_ensure_returns_unfetchable_tickers():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    fake = _FakeSchwabProvider({"AAPL": 184.50}, raise_on={"BAD"})
    missing = store.ensure(["AAPL", "BAD"], schwab_provider=fake)
    assert_eq(missing, ["BAD"], "tickers that errored are reported back")
    assert_eq(store.get("AAPL"), 184.50, "successful tickers still cached")
    assert_is_none(store.get("BAD"), "errored ticker not cached")


def test_singleton_accessor():
    from prev_close_store import get_prev_close_store
    s1 = get_prev_close_store()
    s2 = get_prev_close_store()
    assert_eq(id(s1), id(s2), "get_prev_close_store returns singleton")


if __name__ == "__main__":
    test_get_unknown_returns_none()
    test_set_get_roundtrip()
    test_ttl_expiry()
    test_ensure_fetches_missing_via_schwab()
    test_ensure_skips_already_cached()
    test_ensure_returns_unfetchable_tickers()
    test_singleton_accessor()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
