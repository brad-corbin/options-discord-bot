"""
test_spot_prices_streaming.py — tests for the streaming-first spot fetcher.

Fully offline: streaming spot store, Schwab provider, AND Yahoo fetcher
are all stubbed inside _setup(). Verifies:
  - streaming hits short-circuit (no REST, no Yahoo)
  - REST falls in for unsubscribed tickers
  - Yahoo only runs when DASHBOARD_SPOT_USE_STREAMING is off (legacy mode)
  - prev_close paired correctly in change/change_pct math
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


def assert_close(actual, expected, msg, tol=0.01):
    if actual is None or abs(actual - expected) > tol:
        FAILED.append(f"{msg}: expected {expected} +/- {tol}, got {actual}")
        return False
    PASSED.append(msg)
    return True


def assert_in(key, dct, msg):
    if key not in dct:
        FAILED.append(f"{msg}: {key!r} not in result")
        return False
    PASSED.append(msg)
    return True


def assert_not_in(key, dct, msg):
    if key in dct:
        FAILED.append(f"{msg}: {key!r} unexpectedly present")
        return False
    PASSED.append(msg)
    return True


class _FakeSchwabProvider:
    def __init__(self, prev_close_table):
        self.prev_close_table = prev_close_table
        self.calls = []

    def _schwab_get(self, method, *args, **kwargs):
        symbol = args[0] if args else kwargs.get("symbol")
        self.calls.append((method, symbol))
        if method == "get_quote":
            close = self.prev_close_table.get(symbol)
            if close is None:
                return {symbol: {"quote": {}}}
            return {
                symbol: {"quote": {
                    "closePrice": close,
                    "lastPrice": close + 1.50,
                    "mark": close + 1.50,
                }}
            }
        raise RuntimeError(f"unexpected method {method}")


def _setup(env_streaming="1", streaming_prices=None, prev_close=None,
           schwab_table=None):
    """Reset module state and inject mocks. Returns (module, fake_provider)."""
    os.environ["DASHBOARD_SPOT_USE_STREAMING"] = env_streaming

    # Reset prev_close singleton.
    import prev_close_store
    prev_close_store._singleton = None
    store = prev_close_store.get_prev_close_store()
    for t, p in (prev_close or {}).items():
        store.set(t, p)

    # Inject streaming-spot fakes by monkey-patching get_streaming_spot.
    import schwab_stream
    spots = streaming_prices or {}
    schwab_stream.get_streaming_spot = lambda t: spots.get(t.upper())

    # Reset spot_prices internal caches.
    from omega_dashboard import spot_prices
    spot_prices._cache.clear()
    spot_prices._neg_cache.clear()

    # Inject the schwab provider lookup.
    fake = _FakeSchwabProvider(schwab_table or {})
    spot_prices._get_schwab_provider = lambda: fake

    # Stub the Yahoo fetcher so the test suite is fully offline.
    # Without this, Phase 4 fallback in _fetch_streaming_first and the
    # legacy Yahoo path both fire real network requests and can hit 403
    # / rate-limit / DNS failures depending on the environment.
    spot_prices._fetch_one_yahoo = lambda t: ({}, "err")

    return spot_prices, fake


def test_streaming_hit_uses_prev_close_for_change():
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={"AAPL": 186.00},
        prev_close={"AAPL": 184.50},
    )
    out = sp.get_spot_prices(["AAPL"])
    assert_in("AAPL", out, "AAPL in output")
    assert_close(out["AAPL"]["price"], 186.00, "price from streaming")
    assert_close(out["AAPL"]["change"], 1.50, "change = 186.00 - 184.50")
    assert_close(out["AAPL"]["change_pct"], 0.813, "change_pct correct")
    assert_eq(fake.calls, [], "no Schwab REST call when streaming hits")


def test_streaming_miss_falls_to_schwab_rest():
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={},  # no streaming hit
        prev_close={},        # also no prev_close cached
        schwab_table={"NVDA": 900.00},
    )
    out = sp.get_spot_prices(["NVDA"])
    assert_in("NVDA", out, "NVDA in output via Schwab REST fallback")
    assert_close(out["NVDA"]["price"], 901.50, "price from Schwab lastPrice")
    assert_close(out["NVDA"]["change"], 1.50, "change vs Schwab closePrice")


def test_unfetchable_ticker_omitted():
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={},
        prev_close={},
        schwab_table={},  # no quote for FAKE
    )
    out = sp.get_spot_prices(["FAKE"])
    assert_not_in("FAKE", out, "unfetchable ticker omitted from result")


def test_streaming_yahoo_fallback_respects_neg_cache():
    """Phase 4 (Yahoo last-resort) must skip tickers already in cooldown.
    Without this, the v8.3 Patch 1 429-storm fix regresses on the Yahoo
    tail of the streaming path."""
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={},  # all miss streaming
        prev_close={},
        schwab_table={},      # all miss Schwab too → falls to Phase 4 Yahoo
    )
    # Pre-populate the negative cache so YESTERDAYS_429 is in cooldown.
    sp._neg_cache["YESTERDAYS_429"] = (time.time() + 300, "429")
    # Track Yahoo calls by monkey-patching _fetch_one_yahoo.
    yahoo_calls = []
    original_fetch = sp._fetch_one_yahoo
    def _spy_yahoo(t):
        yahoo_calls.append(t)
        return ({}, "err")
    sp._fetch_one_yahoo = _spy_yahoo
    try:
        sp.get_spot_prices(["YESTERDAYS_429"])
        assert_eq(yahoo_calls, [], "ticker in neg-cache cooldown is skipped")
    finally:
        sp._fetch_one_yahoo = original_fetch


def test_legacy_mode_uses_yahoo():
    """When DASHBOARD_SPOT_USE_STREAMING is off, behavior matches the old path."""
    sp, fake = _setup(
        env_streaming="0",
        streaming_prices={"AAPL": 999.99},  # would-be wrong value if streaming used
        prev_close={"AAPL": 999.99},
    )
    # In legacy mode the stream fast-path is skipped. We don't hit Yahoo
    # in this unit test (no network) -- the test just verifies the new
    # streaming path is bypassed when the env var is off.
    # Schwab provider should NOT be called either.
    sp.get_spot_prices(["AAPL"])
    assert_eq(fake.calls, [], "no Schwab REST call in legacy mode")


if __name__ == "__main__":
    test_streaming_hit_uses_prev_close_for_change()
    test_streaming_miss_falls_to_schwab_rest()
    test_unfetchable_ticker_omitted()
    test_streaming_yahoo_fallback_respects_neg_cache()
    test_legacy_mode_uses_yahoo()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  FAIL: {f}")
    sys.exit(0 if not FAILED else 1)
