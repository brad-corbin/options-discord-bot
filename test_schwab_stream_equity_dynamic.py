"""
test_schwab_stream_equity_dynamic.py — unit tests for dynamic equity
subscription on SchwabStreamManager.

No WebSocket, no network. Tests the queue state directly. The actual
WebSocket interaction is exercised in production; this file pins the
contract that add/remove are idempotent and update the right structures.
"""

from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schwab_stream import SchwabStreamManager

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def _make_mgr(tickers=None):
    """Build a manager without starting the thread. We never call .start()
    in these tests — pure queue/state checks."""
    return SchwabStreamManager(
        schwab_client=None,
        tickers=list(tickers or ["AAPL", "MSFT"]),
    )


def test_add_equity_symbols_queues_new_only():
    mgr = _make_mgr(["AAPL"])
    mgr.add_equity_symbols(["AAPL", "MSFT", "NVDA"])
    # AAPL already there, only MSFT/NVDA queued
    assert_eq(sorted(mgr._pending_equity_adds), ["MSFT", "NVDA"],
              "add_equity_symbols queues only new tickers")
    assert_eq(sorted(mgr._tickers), ["AAPL", "MSFT", "NVDA"],
              "add_equity_symbols updates _tickers")


def test_add_equity_symbols_idempotent():
    mgr = _make_mgr(["AAPL"])
    mgr.add_equity_symbols(["MSFT"])
    mgr.add_equity_symbols(["MSFT"])  # second call is a no-op
    assert_eq(mgr._pending_equity_adds, ["MSFT"],
              "add_equity_symbols is idempotent on repeated calls")


def test_add_equity_symbols_case_insensitive_dedup():
    """Constructor input with lowercase tickers must still dedup against
    later uppercase add_equity_symbols calls. Without this, a caller
    passing lowercase from upstream would cause duplicate subscriptions."""
    mgr = _make_mgr(["aapl"])
    mgr.add_equity_symbols(["AAPL"])
    assert_eq(mgr._pending_equity_adds, [],
              "AAPL not re-queued when constructor was passed 'aapl'")
    assert_eq(mgr._tickers, ["AAPL"],
              "constructor uppercases tickers")


def test_add_equity_symbols_empty_noop():
    mgr = _make_mgr(["AAPL"])
    mgr.add_equity_symbols([])
    mgr.add_equity_symbols(None)
    assert_eq(mgr._pending_equity_adds, [], "empty/None input is no-op")


def test_remove_equity_symbols():
    mgr = _make_mgr(["AAPL", "MSFT", "NVDA"])
    mgr.remove_equity_symbols(["MSFT"])
    assert_eq(sorted(mgr._tickers), ["AAPL", "NVDA"],
              "remove_equity_symbols drops from _tickers")
    assert_eq(mgr._pending_equity_unsubs, ["MSFT"],
              "remove_equity_symbols queues for unsub")


def test_remove_equity_symbols_unknown_is_noop():
    mgr = _make_mgr(["AAPL"])
    mgr.remove_equity_symbols(["UNKNOWN"])
    # Still queues it (Schwab will silently ignore an unsub for a symbol
    # that isn't subscribed) but does not crash on an unknown ticker.
    assert_eq(mgr._tickers, ["AAPL"], "remove of unknown ticker leaves _tickers untouched")


if __name__ == "__main__":
    test_add_equity_symbols_queues_new_only()
    test_add_equity_symbols_idempotent()
    test_add_equity_symbols_case_insensitive_dedup()
    test_add_equity_symbols_empty_noop()
    test_remove_equity_symbols()
    test_remove_equity_symbols_unknown_is_noop()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
