"""
test_canonical_expiration.py — Unit tests for canonical_expiration registry.

Includes wrapper-consistency test against a direct expiration-list filter.
Same discipline as canonical_gamma_flip and canonical_iv_state.

Runs without network. Mock data_router supplies fixture expiration lists.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date

from canonical_expiration import (
    canonical_expiration,
    INTENT_ZERO_DTE, INTENT_FRONT, INTENT_T7, INTENT_T30, INTENT_T60,
    VALID_INTENTS,
)

PASSED = []
FAILED = []


# ───────────────────────────────────────────────────────────────────────
# Assertion helpers (match the pattern used by test_bot_state.py)
# ───────────────────────────────────────────────────────────────────────

def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    return True


def assert_raises(exc_type, fn, msg):
    try:
        fn()
        FAILED.append(f"{msg}: expected {exc_type.__name__}, no exception raised")
        return False
    except exc_type:
        return True
    except Exception as e:
        FAILED.append(f"{msg}: expected {exc_type.__name__}, got {type(e).__name__}: {e}")
        return False


# ───────────────────────────────────────────────────────────────────────
# Mock data_router
# ───────────────────────────────────────────────────────────────────────

class _MockRouter:
    """Minimal stand-in for DataRouter. get_expirations returns the fixture."""

    def __init__(self, expirations):
        self.expirations = list(expirations)
        self.calls = 0

    def get_expirations(self, ticker):
        self.calls += 1
        return list(self.expirations)


class _RaisingRouter:
    """Router whose get_expirations always raises."""

    def get_expirations(self, ticker):
        raise RuntimeError("simulated upstream failure")


# ───────────────────────────────────────────────────────────────────────
# Test fixtures — fixed dates so tests are deterministic
# ───────────────────────────────────────────────────────────────────────

# A Tuesday with AAPL-style M/W/F weeklies + monthly tail.
TUE_2026_05_05 = date(2026, 5, 5)
AAPL_LIST = [
    "2026-05-04",  # Mon (today-1, irrelevant once today=Tue)
    "2026-05-06",  # Wed (DTE 1 from today=Tue)
    "2026-05-08",  # Fri (DTE 3)
    "2026-05-11",  # next Mon (DTE 6)
    "2026-05-13",  # next Wed (DTE 8)  ← t7 should land here
    "2026-05-15",  # next Fri (DTE 10)
    "2026-06-19",  # ~45 DTE (closest to t30)
    "2026-07-17",  # ~73 DTE (closest to t60)
]

# Friday-only weeklies (e.g. COIN style — limited weekly schedule)
FRIDAY_ONLY_LIST = [
    "2026-05-08",  # this Fri (DTE 3 from today=Tue)
    "2026-05-15",  # next Fri (DTE 10)  ← t7 should land here
    "2026-05-22",  # following Fri (DTE 17)
    "2026-06-19",  # monthly (DTE 45) ← t30
    "2026-07-17",  # monthly (DTE 73) ← t60
]

# SPY-style: daily expirations
MON_2026_05_04 = date(2026, 5, 4)
SPY_LIST = [
    "2026-05-04",  # today (0 DTE)
    "2026-05-05",  # 1
    "2026-05-06",  # 2
    "2026-05-07",  # 3
    "2026-05-08",  # 4
    "2026-05-11",  # 7  ← t7 should land here exactly
    "2026-06-03",  # 30 ← t30 (or close)
    "2026-07-03",  # 60 ← t60 (or close)
]


# ───────────────────────────────────────────────────────────────────────
# Tests — argument validation
# ───────────────────────────────────────────────────────────────────────

def test_unknown_intent_raises():
    router = _MockRouter(AAPL_LIST)
    assert_raises(
        ValueError,
        lambda: canonical_expiration("AAPL", "bogus_intent",
                                     today=TUE_2026_05_05, data_router=router),
        "unknown intent should raise ValueError",
    )
    PASSED.append("test_unknown_intent_raises")


def test_missing_data_router_raises():
    assert_raises(
        ValueError,
        lambda: canonical_expiration("AAPL", INTENT_FRONT,
                                     today=TUE_2026_05_05, data_router=None),
        "missing data_router should raise ValueError",
    )
    PASSED.append("test_missing_data_router_raises")


def test_valid_intents_constant_is_complete():
    """Sanity check on the public constant — five intents, no more."""
    assert_eq(VALID_INTENTS,
              frozenset({"zero_dte", "front", "t7", "t30", "t60"}),
              "VALID_INTENTS contains exactly the five public intents")
    PASSED.append("test_valid_intents_constant_is_complete")


# ───────────────────────────────────────────────────────────────────────
# Tests — zero_dte intent
# ───────────────────────────────────────────────────────────────────────

def test_zero_dte_returns_today_when_today_in_list():
    """SPY on a Monday: today's expiration exists (daily), zero_dte returns it."""
    router = _MockRouter(SPY_LIST)  # contains 2026-05-04 == today
    result = canonical_expiration("SPY", INTENT_ZERO_DTE,
                                  today=MON_2026_05_04, data_router=router)
    assert_eq(result, "2026-05-04", "zero_dte returns today's ISO string")
    PASSED.append("test_zero_dte_returns_today_when_today_in_list")


def test_zero_dte_returns_none_when_today_not_in_list():
    """AAPL on a Tuesday: AAPL has M/W/F only, no Tuesday chain → None."""
    router = _MockRouter(AAPL_LIST)  # contains no 2026-05-05
    result = canonical_expiration("AAPL", INTENT_ZERO_DTE,
                                  today=TUE_2026_05_05, data_router=router)
    assert_is_none(result, "zero_dte returns None when today not in expiration list")
    PASSED.append("test_zero_dte_returns_none_when_today_not_in_list")


# ───────────────────────────────────────────────────────────────────────
# Run all
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_unknown_intent_raises,
        test_missing_data_router_raises,
        test_valid_intents_constant_is_complete,
        test_zero_dte_returns_today_when_today_in_list,
        test_zero_dte_returns_none_when_today_not_in_list,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAILED.append(f"{t.__name__}: unexpected exception {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"PASSED: {len(PASSED)} / {len(tests)}")
    for p in PASSED:
        print(f"  PASS {p}")
    if FAILED:
        print(f"\nFAILED: {len(FAILED)}")
        for f in FAILED:
            print(f"  FAIL {f}")
        sys.exit(1)
    else:
        print(f"\n{'='*60}")
        print("All tests passed.")


if __name__ == "__main__":
    main()
