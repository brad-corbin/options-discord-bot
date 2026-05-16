"""Tests for OptionQuoteStore staleness threshold. Patch G.13.1.

The store's get() and get_live_premium() return None when a cached quote
is older than its staleness threshold. Pre-G.13.1 that threshold was
hardcoded to self._stale_threshold (60s default). G.13.1 parameterizes
it so the tracker daemon can read 10-minute-stale quotes without
affecting trading-path callers that need fresh data.
"""
from unittest import mock


def _store_with_quote_aged(age_seconds: float):
    """Build a store, push one quote, then return it with monotonic shifted
    forward by age_seconds so subsequent get() calls see the quote as aged.
    """
    import schwab_stream
    store = schwab_stream.OptionQuoteStore()
    # Pin the timestamp the update will stamp into _ts.
    with mock.patch.object(schwab_stream.time, "monotonic", return_value=1000.0):
        store.update("SPY   260515C00590000",
                     {"bid": 2.80, "ask": 2.90, "mark": 2.85, "last": 2.85})
    return store, schwab_stream


def test_get_returns_quote_when_within_default_threshold():
    """Sanity: with default 60s threshold and a 30s-old quote, get() returns it."""
    store, mod = _store_with_quote_aged(30)
    with mock.patch.object(mod.time, "monotonic", return_value=1030.0):
        q = store.get("SPY   260515C00590000")
    assert q is not None
    assert q["bid"] == 2.80


def test_get_returns_none_when_past_default_threshold():
    """With default 60s threshold and a 120s-old quote, get() returns None.

    This is the bug behavior G.13.1 mitigates: ticks arriving every 60-300s
    on low-activity options cause the tracker to read None on every sample.
    """
    store, mod = _store_with_quote_aged(120)
    with mock.patch.object(mod.time, "monotonic", return_value=1120.0):
        q = store.get("SPY   260515C00590000")
    assert q is None, "Default 60s threshold should reject a 120s-old quote"


def test_get_with_lenient_stale_threshold_returns_aged_quote():
    """Custom stale_threshold=600 should return a 120s-old quote that the
    default would discard."""
    store, mod = _store_with_quote_aged(120)
    with mock.patch.object(mod.time, "monotonic", return_value=1120.0):
        q = store.get("SPY   260515C00590000", stale_threshold=600)
    assert q is not None, (
        "Custom stale_threshold=600 should accept a 120s-old quote that the "
        "default 60s threshold discards. This is the entire point of G.13.1."
    )
    assert q["bid"] == 2.80


def test_get_with_lenient_threshold_still_rejects_truly_old_quote():
    """Even with 600s threshold, a 700s-old quote should be None."""
    store, mod = _store_with_quote_aged(700)
    with mock.patch.object(mod.time, "monotonic", return_value=1700.0):
        q = store.get("SPY   260515C00590000", stale_threshold=600)
    assert q is None, "stale_threshold=600 should still reject 700s-old quotes"


def test_get_live_premium_with_lenient_stale_threshold():
    """get_live_premium should accept the threshold and pass it through."""
    store, mod = _store_with_quote_aged(120)
    with mock.patch.object(mod.time, "monotonic", return_value=1120.0):
        mid_default = store.get_live_premium("SPY   260515C00590000")
        mid_lenient = store.get_live_premium("SPY   260515C00590000",
                                             stale_threshold=600)
    assert mid_default is None, "default 60s rejects 120s-old quote"
    assert mid_lenient is not None, "lenient 600s accepts 120s-old quote"
    # mid = (2.80 + 2.90) / 2 = 2.85
    assert abs(mid_lenient - 2.85) < 0.001


def test_module_level_get_live_premium_accepts_stale_threshold():
    """Module-level helper must also expose the parameter."""
    import schwab_stream
    # Reset the module-level store so this test is hermetic.
    schwab_stream._option_store = schwab_stream.OptionQuoteStore()
    with mock.patch.object(schwab_stream.time, "monotonic", return_value=2000.0):
        schwab_stream._option_store.update(
            "QQQ   260515C00480000",
            {"bid": 4.00, "ask": 4.10, "mark": 4.05, "last": 4.05},
        )
    with mock.patch.object(schwab_stream.time, "monotonic", return_value=2200.0):
        mid_default = schwab_stream.get_live_premium("QQQ   260515C00480000")
        mid_lenient = schwab_stream.get_live_premium(
            "QQQ   260515C00480000", stale_threshold=600,
        )
    assert mid_default is None
    assert mid_lenient is not None
    assert abs(mid_lenient - 4.05) < 0.001


if __name__ == "__main__":
    tests = [
        test_get_returns_quote_when_within_default_threshold,
        test_get_returns_none_when_past_default_threshold,
        test_get_with_lenient_stale_threshold_returns_aged_quote,
        test_get_with_lenient_threshold_still_rejects_truly_old_quote,
        test_get_live_premium_with_lenient_stale_threshold,
        test_module_level_get_live_premium_accepts_stale_threshold,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL: {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
