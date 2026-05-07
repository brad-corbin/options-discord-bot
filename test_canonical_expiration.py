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
# Tests — front / t7 / t30 / t60 (first DTE >= N)
# ───────────────────────────────────────────────────────────────────────

def test_front_skips_today_picks_dte_1():
    """SPY on a Monday: 0DTE excluded, front returns next day (DTE 1)."""
    router = _MockRouter(SPY_LIST)
    result = canonical_expiration("SPY", INTENT_FRONT,
                                  today=MON_2026_05_04, data_router=router)
    assert_eq(result, "2026-05-05", "front returns first DTE >= 1, skipping 0DTE")
    PASSED.append("test_front_skips_today_picks_dte_1")


def test_front_picks_first_qualifying_when_no_0dte():
    """AAPL on a Tuesday with no Tuesday chain: front returns Wed (DTE 1)."""
    router = _MockRouter(AAPL_LIST)
    result = canonical_expiration("AAPL", INTENT_FRONT,
                                  today=TUE_2026_05_05, data_router=router)
    assert_eq(result, "2026-05-06", "front returns next available expiration (Wed, DTE 1)")
    PASSED.append("test_front_picks_first_qualifying_when_no_0dte")


def test_t7_picks_first_dte_at_or_above_7():
    """AAPL Tuesday: M/W/F weeklies. t7 picks Wed of next week (DTE 8 >= 7)."""
    router = _MockRouter(AAPL_LIST)
    result = canonical_expiration("AAPL", INTENT_T7,
                                  today=TUE_2026_05_05, data_router=router)
    assert_eq(result, "2026-05-13", "t7 returns Wed of next week (DTE 8)")
    PASSED.append("test_t7_picks_first_dte_at_or_above_7")


def test_t7_exact_match_when_dte_7_exists():
    """SPY Monday: dailies, DTE 7 exists exactly → t7 returns it."""
    router = _MockRouter(SPY_LIST)
    result = canonical_expiration("SPY", INTENT_T7,
                                  today=MON_2026_05_04, data_router=router)
    assert_eq(result, "2026-05-11", "t7 returns exact DTE-7 when available")
    PASSED.append("test_t7_exact_match_when_dte_7_exists")


def test_t7_friday_only_jumps_to_following_friday():
    """COIN-style Friday-only weeklies: this Fri is DTE 3 (< 7),
    next Fri is DTE 10 → t7 picks next Fri."""
    router = _MockRouter(FRIDAY_ONLY_LIST)
    result = canonical_expiration("COIN", INTENT_T7,
                                  today=TUE_2026_05_05, data_router=router)
    assert_eq(result, "2026-05-15", "t7 jumps over close Friday to following Friday")
    PASSED.append("test_t7_friday_only_jumps_to_following_friday")


def test_t30_picks_monthly():
    """AAPL Tuesday: t30 returns the ~45-DTE monthly (no closer >=30 chain)."""
    router = _MockRouter(AAPL_LIST)
    result = canonical_expiration("AAPL", INTENT_T30,
                                  today=TUE_2026_05_05, data_router=router)
    assert_eq(result, "2026-06-19", "t30 returns first DTE >= 30")
    PASSED.append("test_t30_picks_monthly")


def test_t60_picks_further_monthly():
    """AAPL Tuesday: t60 returns the ~73-DTE monthly."""
    router = _MockRouter(AAPL_LIST)
    result = canonical_expiration("AAPL", INTENT_T60,
                                  today=TUE_2026_05_05, data_router=router)
    assert_eq(result, "2026-07-17", "t60 returns first DTE >= 60")
    PASSED.append("test_t60_picks_further_monthly")


def test_t60_returns_none_when_no_qualifying_expiration():
    """AAPL Tuesday with chain only out 30 days: t60 returns None."""
    short_list = ["2026-05-06", "2026-05-08", "2026-05-13", "2026-06-04"]  # max DTE 30
    router = _MockRouter(short_list)
    result = canonical_expiration("AAPL", INTENT_T60,
                                  today=TUE_2026_05_05, data_router=router)
    assert_is_none(result, "t60 None when no expiration is far enough out")
    PASSED.append("test_t60_returns_none_when_no_qualifying_expiration")


# ───────────────────────────────────────────────────────────────────────
# Tests — malformed input + upstream failure
# ───────────────────────────────────────────────────────────────────────

def test_empty_expiration_list_returns_none():
    """No expirations available → None for any intent."""
    router = _MockRouter([])
    for intent in (INTENT_ZERO_DTE, INTENT_FRONT, INTENT_T7, INTENT_T30, INTENT_T60):
        result = canonical_expiration("ANY", intent,
                                      today=TUE_2026_05_05, data_router=router)
        assert_is_none(result, f"{intent} on empty list returns None")
    PASSED.append("test_empty_expiration_list_returns_none")


def test_malformed_entries_skipped():
    """Garbage entries (None, weird strings, ints) are skipped, not crashed on."""
    bad_list = [
        None,
        "not-a-date",
        "2026-13-99",   # invalid month/day
        42,             # int
        "2026-05-13",   # legitimate one to make sure we still pick it
    ]
    router = _MockRouter(bad_list)
    result = canonical_expiration("AAPL", INTENT_T7,
                                  today=TUE_2026_05_05, data_router=router)
    assert_eq(result, "2026-05-13", "malformed entries skipped, valid one picked")
    PASSED.append("test_malformed_entries_skipped")


def test_upstream_get_expirations_raises_returns_none():
    """If data_router.get_expirations raises, log and return None — never propagate."""
    router = _RaisingRouter()
    result = canonical_expiration("AAPL", INTENT_FRONT,
                                  today=TUE_2026_05_05, data_router=router)
    assert_is_none(result, "upstream failure returns None, doesn't propagate")
    PASSED.append("test_upstream_get_expirations_raises_returns_none")


# ───────────────────────────────────────────────────────────────────────
# Wrapper-consistency test (audit rule 5)
# ───────────────────────────────────────────────────────────────────────

def test_wrapper_consistency_against_direct_filter():
    """canonical_expiration must return the same answer as a hand-written filter
    on identical inputs. If they ever drift, the wrapper has diverged from
    the ground truth.

    Runs the consistency check against three different fixture shapes:
      - AAPL_LIST (M/W/F weeklies + monthlies; zero_dte returns None)
      - SPY_LIST  (daily expirations; zero_dte returns today)
      - FRIDAY_ONLY_LIST (sparse weeklies; tests jump-to-following-Friday)
    """
    def _check(label, router, today):
        raw = router.get_expirations("X")
        parsed = sorted({date.fromisoformat(e[:10]) for e in raw if isinstance(e, str)})

        expected = {
            INTENT_ZERO_DTE: today.isoformat() if today in parsed else None,
            INTENT_FRONT: next((e.isoformat() for e in parsed if (e - today).days >= 1), None),
            INTENT_T7: next((e.isoformat() for e in parsed if (e - today).days >= 7), None),
            INTENT_T30: next((e.isoformat() for e in parsed if (e - today).days >= 30), None),
            INTENT_T60: next((e.isoformat() for e in parsed if (e - today).days >= 60), None),
        }
        for intent, expected_val in expected.items():
            actual = canonical_expiration("X", intent, today=today, data_router=router)
            assert_eq(actual, expected_val,
                      f"{label}: {intent} wrapper matches direct filter")

    _check("AAPL", _MockRouter(AAPL_LIST), TUE_2026_05_05)
    _check("SPY", _MockRouter(SPY_LIST), MON_2026_05_04)
    _check("FRIDAY_ONLY", _MockRouter(FRIDAY_ONLY_LIST), TUE_2026_05_05)
    PASSED.append("test_wrapper_consistency_against_direct_filter")


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
        test_front_skips_today_picks_dte_1,
        test_front_picks_first_qualifying_when_no_0dte,
        test_t7_picks_first_dte_at_or_above_7,
        test_t7_exact_match_when_dte_7_exists,
        test_t7_friday_only_jumps_to_following_friday,
        test_t30_picks_monthly,
        test_t60_picks_further_monthly,
        test_t60_returns_none_when_no_qualifying_expiration,
        test_empty_expiration_list_returns_none,
        test_malformed_entries_skipped,
        test_upstream_get_expirations_raises_returns_none,
        test_wrapper_consistency_against_direct_filter,
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
