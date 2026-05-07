# canonical_expiration Registry — Implementation Plan (Patch A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single canonical wrapper `canonical_expiration(ticker, intent, ...)` that picks the right chain expiration per ticker per intent, replacing the current ad-hoc "next Friday" default in `research_data.py`. This is Patch A of the 5-patch program in `docs/superpowers/specs/2026-05-07-research-page-multi-dte-walls-design.md`.

**Architecture:** New thin wrapper file `canonical_expiration.py` at the repo root, alongside the other canonicals (`canonical_gamma_flip.py`, `canonical_iv_state.py`, `canonical_exposures.py`). Five intents: `zero_dte` (today's expiration if it exists), `front` (first DTE ≥ 1), `t7`/`t30`/`t60` (first DTE ≥ N). Never walks backward. Wraps `data_router.get_expirations()`; returns `None` when no qualifying expiration exists. Wired into `omega_dashboard/research_data.py` as a side-effect fix for the wall-mismatch.

**Tech Stack:** Python 3.14 (standard library only — `datetime`, `typing`, `logging`). Synchronous. Uses the existing test pattern from `test_bot_state.py` (PASS/FAIL list, no pytest dependency).

**Reference reading before starting:**
- `docs/superpowers/specs/2026-05-07-research-page-multi-dte-walls-design.md` — sections "Components → canonical_expiration" and "Patch A details"
- `CLAUDE.md` — audit discipline (rules 1, 3, 4, 5, 6 all apply here)
- `canonical_gamma_flip.py` — reference for the canonical-wrapper file pattern, docstring conventions
- `test_canonical_gamma_flip.py` — reference for the test-file pattern (PASS/FAIL list, mock router)

---

## Task 1: Create the module skeleton and intent constants

**Files:**
- Create: `canonical_expiration.py`

- [ ] **Step 1: Create the file with module docstring, imports, and constants only**

Write to `canonical_expiration.py`:

```python
"""
canonical_expiration.py — Single canonical wrapper for picking chain expiration by intent.

PURPOSE
-------
Every place in the codebase that asks "which expiration should I use for this
ticker right now?" calls `canonical_expiration(ticker, intent, ...)`. There is
exactly ONE entry point. Five intents:

  zero_dte — today's expiration if it exists in the chain list. Used by silent
             thesis (existing 0DTE-first behavior). Excluded from Research.
  front    — first expiration with DTE >= 1. Skips 0DTE explicitly. Default
             for the Research page front card.
  t7       — first expiration with DTE >= 7. Used by drilldown.
  t30      — first expiration with DTE >= 30. Used by drilldown.
  t60      — first expiration with DTE >= 60. Used by drilldown.

NEVER WALKS BACKWARD. If today is Tuesday and AAPL has Mon=6/Wed=8/Fri=10
weeklies, t7 returns Wednesday (8 days). Never picks Monday (6 days < 7).

DEPENDENCIES
------------
- DataRouter.get_expirations(ticker): returns the ticker's expiration list.
  Wrapped via the data_router argument so this module stays testable without
  Schwab credentials.

CONTRACT
--------
Returns ISO date string (e.g. "2026-05-09") on success.
Returns None when:
  - no qualifying expiration exists for the intent (e.g. t60 on a ticker with
    only weekly chains out 30 days)
  - data_router.get_expirations() raises (logged, treated as data unavailable)
  - the expiration list is empty

Raises ValueError on:
  - unknown intent
  - missing data_router

WRAPPER-CONSISTENCY DISCIPLINE
------------------------------
Companion test (`test_canonical_expiration.py`) includes a wrapper-consistency
test: build a fixture expiration list, call canonical_expiration, assert the
result matches what a direct filter on data_router.get_expirations() would
have produced. Same pattern as canonical_gamma_flip and canonical_iv_state.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# INTENT CONSTANTS
# ───────────────────────────────────────────────────────────────────────

INTENT_ZERO_DTE = "zero_dte"
INTENT_FRONT = "front"
INTENT_T7 = "t7"
INTENT_T30 = "t30"
INTENT_T60 = "t60"

VALID_INTENTS = frozenset({
    INTENT_ZERO_DTE,
    INTENT_FRONT,
    INTENT_T7,
    INTENT_T30,
    INTENT_T60,
})

# Min DTE per intent for the "first DTE >= N" intents. zero_dte is special-cased.
_MIN_DTE_BY_INTENT = {
    INTENT_FRONT: 1,
    INTENT_T7: 7,
    INTENT_T30: 30,
    INTENT_T60: 60,
}


# ───────────────────────────────────────────────────────────────────────
# Direct-run sanity (filled in by Task 8)
# ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"canonical_expiration.py loaded. Valid intents: {sorted(VALID_INTENTS)}")
```

- [ ] **Step 2: AST-check the new file**

Run: `python -c "import ast; ast.parse(open('canonical_expiration.py', encoding='utf-8').read()); print('AST clean')"`

Expected output: `AST clean`

- [ ] **Step 3: Direct-run smoke test**

Run: `python canonical_expiration.py`

Expected output: `canonical_expiration.py loaded. Valid intents: ['front', 't30', 't60', 't7', 'zero_dte']`

- [ ] **Step 4: Commit**

```bash
git add canonical_expiration.py
git commit -m "Patch A.1: canonical_expiration module skeleton + intent constants"
```

---

## Task 2: Test scaffolding + arg-validation tests (TDD red)

**Files:**
- Create: `test_canonical_expiration.py`

- [ ] **Step 1: Create the test file with the standard scaffolding + first failing tests**

Write to `test_canonical_expiration.py`:

```python
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
# Run all
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_unknown_intent_raises,
        test_missing_data_router_raises,
        test_valid_intents_constant_is_complete,
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
```

- [ ] **Step 2: Run the test file and verify it fails because `canonical_expiration` is not defined**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output: ImportError or AttributeError pointing to `canonical_expiration` not being importable from the module (because the function isn't written yet, only the constants are). The traceback should mention `canonical_expiration` as the missing symbol.

- [ ] **Step 3: Add the function signature stub to `canonical_expiration.py` so the import succeeds**

Edit `canonical_expiration.py`. After the `_MIN_DTE_BY_INTENT` block and before the `if __name__ == "__main__":` block, insert:

```python
# ───────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ───────────────────────────────────────────────────────────────────────

def canonical_expiration(
    ticker: str,
    intent: str,
    *,
    today: Optional[date] = None,
    data_router=None,
) -> Optional[str]:
    """Resolve `intent` to an ISO date string for this ticker's chain.

    See module docstring for intent semantics. Returns None when no
    qualifying expiration exists. Raises ValueError on bad arguments.
    """
    if intent not in VALID_INTENTS:
        raise ValueError(
            f"unknown intent: {intent!r}; valid: {sorted(VALID_INTENTS)}"
        )
    if data_router is None:
        raise ValueError("data_router is required")
    # NotImplementedError until Tasks 3-4 fill in the body.
    raise NotImplementedError("body not yet implemented (Tasks 3-4)")
```

- [ ] **Step 4: Run the tests again — only the validation tests should pass; the third test should not even reach the body**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output:
```
PASSED: 3 / 3
  PASS test_unknown_intent_raises
  PASS test_missing_data_router_raises
  PASS test_valid_intents_constant_is_complete

============================================================
All tests passed.
```

(All three tests exercise validation paths that complete before the `NotImplementedError`.)

- [ ] **Step 5: Commit**

```bash
git add canonical_expiration.py test_canonical_expiration.py
git commit -m "Patch A.2: canonical_expiration arg validation + test scaffolding"
```

---

## Task 3: Implement zero_dte intent (TDD)

**Files:**
- Modify: `test_canonical_expiration.py` (add tests)
- Modify: `canonical_expiration.py` (implement zero_dte branch)

- [ ] **Step 1: Add failing tests for zero_dte**

Edit `test_canonical_expiration.py`. After `test_valid_intents_constant_is_complete()` and before the `# Run all` section header, add:

```python
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
```

Then update the `tests = [...]` list inside `main()` to include the two new tests:

```python
    tests = [
        test_unknown_intent_raises,
        test_missing_data_router_raises,
        test_valid_intents_constant_is_complete,
        test_zero_dte_returns_today_when_today_in_list,
        test_zero_dte_returns_none_when_today_not_in_list,
    ]
```

- [ ] **Step 2: Run the tests and verify the two new tests fail**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output:
```
PASSED: 3 / 5
  PASS test_unknown_intent_raises
  PASS test_missing_data_router_raises
  PASS test_valid_intents_constant_is_complete

FAILED: 2
  FAIL test_zero_dte_returns_today_when_today_in_list: unexpected exception NotImplementedError: body not yet implemented (Tasks 3-4)
  FAIL test_zero_dte_returns_none_when_today_not_in_list: unexpected exception NotImplementedError: body not yet implemented (Tasks 3-4)
```

(Exit code 1.)

- [ ] **Step 3: Implement the body of `canonical_expiration` and the `_select_zero_dte` helper**

Edit `canonical_expiration.py`. Replace the body of `canonical_expiration` (currently `raise NotImplementedError(...)`) with:

```python
    today = today or _today_utc()

    try:
        raw_exps = data_router.get_expirations(ticker)
    except Exception as e:
        log.warning(f"canonical_expiration {ticker}/{intent}: get_expirations failed: {e}")
        return None

    exp_dates = _parse_expirations(raw_exps)
    if not exp_dates:
        return None

    if intent == INTENT_ZERO_DTE:
        return _select_zero_dte(exp_dates, today)

    # All other intents are first-DTE-at-or-above-N. Implemented in Task 4.
    raise NotImplementedError(f"intent {intent!r} not yet implemented (Task 4)")
```

Then, after the `canonical_expiration` function definition and before the `if __name__ == "__main__":` block, add the helpers:

```python
# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

def _today_utc() -> date:
    """Current UTC date, injectable via the `today` argument for testing."""
    return datetime.now(timezone.utc).date()


def _parse_expirations(raw: list) -> list[date]:
    """Convert raw expiration entries to date objects. Sort and de-dupe.

    Skips malformed entries (logs at debug). Returns an empty list if none parse.
    Accepts:
      - ISO strings ("2026-05-09" or "2026-05-09T00:00:00Z" — first 10 chars used)
      - date objects directly
    """
    parsed: list[date] = []
    for e in raw:
        try:
            if isinstance(e, str):
                parsed.append(date.fromisoformat(e[:10]))
            elif isinstance(e, date):
                parsed.append(e)
            else:
                log.debug(f"canonical_expiration: skipping non-date entry {e!r}")
        except (ValueError, TypeError) as parse_err:
            log.debug(f"canonical_expiration: failed to parse {e!r}: {parse_err}")
    return sorted(set(parsed))


def _select_zero_dte(exp_dates: list[date], today: date) -> Optional[str]:
    """Return today's expiration if it's in the list, else None."""
    if today in exp_dates:
        return today.isoformat()
    return None
```

- [ ] **Step 4: Run the tests and verify the new ones pass**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output:
```
PASSED: 5 / 5
  PASS test_unknown_intent_raises
  PASS test_missing_data_router_raises
  PASS test_valid_intents_constant_is_complete
  PASS test_zero_dte_returns_today_when_today_in_list
  PASS test_zero_dte_returns_none_when_today_not_in_list

============================================================
All tests passed.
```

- [ ] **Step 5: AST-check and commit**

```bash
python -c "import ast; ast.parse(open('canonical_expiration.py', encoding='utf-8').read()); print('AST clean')"
git add canonical_expiration.py test_canonical_expiration.py
git commit -m "Patch A.3: canonical_expiration zero_dte intent + helpers"
```

---

## Task 4: Implement the four DTE-threshold intents (front, t7, t30, t60)

**Files:**
- Modify: `test_canonical_expiration.py` (add tests for each threshold intent)
- Modify: `canonical_expiration.py` (implement `_select_min_dte`)

- [ ] **Step 1: Add failing tests for the four threshold intents**

Edit `test_canonical_expiration.py`. After the zero_dte tests and before the `# Run all` section, append:

```python
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
```

Update the `tests = [...]` list to include the new ones:

```python
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
    ]
```

- [ ] **Step 2: Run tests and verify all 8 new tests fail with NotImplementedError**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output: `PASSED: 5 / 13`, with 8 FAIL lines mentioning `NotImplementedError: intent 'front'/'t7'/'t30'/'t60' not yet implemented`.

- [ ] **Step 3: Replace the NotImplementedError stub with the `_select_min_dte` call and add the helper**

Edit `canonical_expiration.py`. Inside `canonical_expiration`, replace this line:

```python
    # All other intents are first-DTE-at-or-above-N. Implemented in Task 4.
    raise NotImplementedError(f"intent {intent!r} not yet implemented (Task 4)")
```

with:

```python
    min_dte = _MIN_DTE_BY_INTENT[intent]
    return _select_min_dte(exp_dates, today, min_dte)
```

Then, after the `_select_zero_dte` helper, append:

```python
def _select_min_dte(exp_dates: list[date], today: date, min_dte: int) -> Optional[str]:
    """Return the first expiration whose DTE >= `min_dte`. Never walks backward.

    `exp_dates` must be sorted ascending. Returns None if no expiration is far
    enough out.
    """
    for exp in exp_dates:
        dte = (exp - today).days
        if dte >= min_dte:
            return exp.isoformat()
    return None
```

- [ ] **Step 4: Run all tests, verify 13/13 pass**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output:
```
PASSED: 13 / 13
  PASS test_unknown_intent_raises
  PASS test_missing_data_router_raises
  PASS test_valid_intents_constant_is_complete
  PASS test_zero_dte_returns_today_when_today_in_list
  PASS test_zero_dte_returns_none_when_today_not_in_list
  PASS test_front_skips_today_picks_dte_1
  PASS test_front_picks_first_qualifying_when_no_0dte
  PASS test_t7_picks_first_dte_at_or_above_7
  PASS test_t7_exact_match_when_dte_7_exists
  PASS test_t7_friday_only_jumps_to_following_friday
  PASS test_t30_picks_monthly
  PASS test_t60_picks_further_monthly
  PASS test_t60_returns_none_when_no_qualifying_expiration

============================================================
All tests passed.
```

- [ ] **Step 5: AST-check and commit**

```bash
python -c "import ast; ast.parse(open('canonical_expiration.py', encoding='utf-8').read()); print('AST clean')"
git add canonical_expiration.py test_canonical_expiration.py
git commit -m "Patch A.4: canonical_expiration front/t7/t30/t60 intents"
```

---

## Task 5: Add malformed-input and upstream-failure tests

**Files:**
- Modify: `test_canonical_expiration.py`

These tests should pass IMMEDIATELY because the implementation already handles these cases (via `_parse_expirations` skipping bad entries, and `try/except` around `get_expirations`). This task is about CONFIRMING those paths via tests, not adding new logic.

- [ ] **Step 1: Add the tests**

Append after the threshold-intent tests, before `# Run all`:

```python
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
```

Add them to the `tests = [...]` list in `main()`:

```python
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
    ]
```

- [ ] **Step 2: Run all tests — 16/16 should pass without changing the implementation**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output: `PASSED: 16 / 16`, all PASS lines listed.

If any FAIL, the implementation has a real bug — fix in `canonical_expiration.py` before continuing.

- [ ] **Step 3: Commit**

```bash
git add test_canonical_expiration.py
git commit -m "Patch A.5: canonical_expiration malformed-input + upstream-failure tests"
```

---

## Task 6: Wrapper-consistency test (audit rule 5)

Audit rule 5 (CLAUDE.md): every canonical wrapper has a test proving its output matches a direct call against the underlying engine. Here the "underlying engine" is just `data_router.get_expirations()` plus a date filter. We confirm the wrapper produces the same answer as a hand-written filter on identical inputs.

**Files:**
- Modify: `test_canonical_expiration.py`

- [ ] **Step 1: Add the wrapper-consistency test**

Append after the malformed-input tests, before `# Run all`:

```python
# ───────────────────────────────────────────────────────────────────────
# Wrapper-consistency test (audit rule 5)
# ───────────────────────────────────────────────────────────────────────

def test_wrapper_consistency_against_direct_filter():
    """canonical_expiration must return the same answer as a hand-written filter
    on identical inputs. If they ever drift, the wrapper has diverged from
    the ground truth.
    """
    router = _MockRouter(AAPL_LIST)
    today = TUE_2026_05_05

    # Direct filter — what canonical_expiration SHOULD produce, computed
    # independently here so we can compare.
    raw = router.get_expirations("AAPL")
    parsed = sorted({date.fromisoformat(e[:10]) for e in raw if isinstance(e, str)})

    expected_zero_dte = today.isoformat() if today in parsed else None
    expected_front = next((e.isoformat() for e in parsed if (e - today).days >= 1), None)
    expected_t7 = next((e.isoformat() for e in parsed if (e - today).days >= 7), None)
    expected_t30 = next((e.isoformat() for e in parsed if (e - today).days >= 30), None)
    expected_t60 = next((e.isoformat() for e in parsed if (e - today).days >= 60), None)

    # Each wrapper call must match the direct-filter result.
    assert_eq(
        canonical_expiration("AAPL", INTENT_ZERO_DTE, today=today, data_router=router),
        expected_zero_dte,
        "zero_dte wrapper matches direct filter",
    )
    assert_eq(
        canonical_expiration("AAPL", INTENT_FRONT, today=today, data_router=router),
        expected_front,
        "front wrapper matches direct filter",
    )
    assert_eq(
        canonical_expiration("AAPL", INTENT_T7, today=today, data_router=router),
        expected_t7,
        "t7 wrapper matches direct filter",
    )
    assert_eq(
        canonical_expiration("AAPL", INTENT_T30, today=today, data_router=router),
        expected_t30,
        "t30 wrapper matches direct filter",
    )
    assert_eq(
        canonical_expiration("AAPL", INTENT_T60, today=today, data_router=router),
        expected_t60,
        "t60 wrapper matches direct filter",
    )
    PASSED.append("test_wrapper_consistency_against_direct_filter")
```

Add to the `tests = [...]` list:

```python
        test_upstream_get_expirations_raises_returns_none,
        test_wrapper_consistency_against_direct_filter,
    ]
```

- [ ] **Step 2: Run tests — 17/17 should pass**

Run: `PYTHONIOENCODING=utf-8 python test_canonical_expiration.py`

Expected output: `PASSED: 17 / 17`.

- [ ] **Step 3: Commit**

```bash
git add test_canonical_expiration.py
git commit -m "Patch A.6: canonical_expiration wrapper-consistency test (audit rule 5)"
```

---

## Task 7: Wire `canonical_expiration` into `omega_dashboard/research_data.py`

This is the side-effect production change. Per the spec: "Side effect: Research page walls now use front non-0-DTE chains for every ticker. Will not match Market View's 0-DTE walls. This is correct per the design — it answers a different question." The Research page is currently unusable (3-min spin), so the visible-to-user impact is small; the architectural alignment is the real win.

**Files:**
- Modify: `omega_dashboard/research_data.py`

- [ ] **Step 1: Re-read the current state of `research_data.py` to verify anchor lines**

Run:

```bash
python -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read()); print('AST clean before edits')"
```

Expected: `AST clean before edits`. If this fails, fix any pre-existing parse errors before continuing.

Open the file and confirm the following are still present (anchor strings, not line numbers — line numbers drift):
- `def _default_expiration() -> str:` — function we're removing
- `def build_ticker_snapshot(ticker: str, expiration: str, *, data_router) -> TickerSnapshot:` — function we're modifying
- `def research_data(` — function we're modifying

If any of these signatures don't match, STOP and re-anchor before editing.

- [ ] **Step 2: Replace `_default_expiration()` with a per-ticker call to `canonical_expiration`**

In `omega_dashboard/research_data.py`, **delete** the function `_default_expiration` entirely (the function plus its docstring — about 7 lines starting at `def _default_expiration() -> str:`).

Then **replace** the body of `build_ticker_snapshot` so it resolves the expiration per-ticker:

Find this exact block:

```python
def build_ticker_snapshot(ticker: str, expiration: str, *, data_router) -> TickerSnapshot:
    """Build a single ticker's research snapshot.

    Returns a TickerSnapshot regardless of success — errors are captured
    inside the snapshot, never raised. Caller renders all snapshots
    uniformly.
    """
    cached = _cache_get(ticker, expiration)
    if cached is not None:
        return cached

    try:
        from bot_state import BotState
        state = BotState.build(ticker, expiration, data_router=data_router)
```

Replace it with:

```python
def build_ticker_snapshot(ticker: str, intent: str = "front", *, data_router) -> TickerSnapshot:
    """Build a single ticker's research snapshot.

    Resolves the chain expiration per-ticker via canonical_expiration, then
    builds BotState. Defaults to intent='front' (first non-0-DTE chain) — the
    Research page's standard view. Returns a TickerSnapshot regardless of
    success; errors are captured inside the snapshot, never raised.
    """
    from canonical_expiration import canonical_expiration
    expiration = canonical_expiration(ticker, intent, data_router=data_router)
    if expiration is None:
        # No qualifying chain (e.g. t60 on a ticker with only short-dated chains).
        return TickerSnapshot(
            ticker=ticker,
            spot=None, gamma_flip=None, distance_from_flip_pct=None,
            flip_location="unknown",
            atm_iv=None, iv_skew_pp=None, iv30=None,
            gex=None, dex=None, vanna=None, charm=None,
            gex_sign="unknown",
            call_wall=None, put_wall=None, gamma_wall=None,
            fields_lit=0, fields_total=0,
            canonical_status={}, chain_clean=False, fetch_errors=[],
            error=f"no chain for intent={intent}",
        )

    cached = _cache_get(ticker, expiration)
    if cached is not None:
        return cached

    try:
        from bot_state import BotState
        state = BotState.build(ticker, expiration, data_router=data_router)
```

(The rest of `build_ticker_snapshot` — the success path that builds the TickerSnapshot from `state` — stays unchanged.)

- [ ] **Step 3: Update `research_data()` to drop the `expiration` arg in favor of `intent`**

In the same file, find:

```python
def research_data(
    tickers: Optional[list] = None,
    expiration: Optional[str] = None,
    *,
    data_router=None,
) -> ResearchData:
```

Replace with:

```python
def research_data(
    tickers: Optional[list] = None,
    intent: str = "front",
    *,
    data_router=None,
) -> ResearchData:
```

Then in the same function body, find:

```python
    if tickers is None:
        tickers = list(DEFAULT_TICKERS)
    if expiration is None:
        expiration = _default_expiration()
```

Replace with:

```python
    if tickers is None:
        tickers = list(DEFAULT_TICKERS)
```

(The `_default_expiration()` call goes away entirely. Per-ticker resolution happens inside `build_ticker_snapshot`.)

Find:

```python
    snapshots = []
    for t in tickers:
        snap = build_ticker_snapshot(t, expiration, data_router=data_router)
        snapshots.append(snap)
```

Replace with:

```python
    snapshots = []
    for t in tickers:
        snap = build_ticker_snapshot(t, intent, data_router=data_router)
        snapshots.append(snap)
```

- [ ] **Step 4: Update the docstring of `research_data` to reflect the new contract**

Find the docstring of `research_data`:

```python
    """Build the full Research page payload.

    Args:
        tickers:      list of tickers to include; defaults to DEFAULT_TICKERS
        expiration:   chain expiration to use for all tickers; if None,
                      uses the next-Friday expiration as a safe default
        data_router:  required for live data. If None, returns an empty
                      payload with available=False (page still renders).

    Returns:
        ResearchData ready for the template.
    """
```

Replace with:

```python
    """Build the full Research page payload.

    Args:
        tickers:      list of tickers to include; defaults to DEFAULT_TICKERS
        intent:       canonical_expiration intent for chain selection. Default
                      'front' = first non-0-DTE expiration per ticker. Other
                      valid values: 't7', 't30', 't60'. ('zero_dte' is reserved
                      for silent thesis and is not used by the Research page.)
        data_router:  required for live data. If None, returns an empty
                      payload with available=False (page still renders).

    Returns:
        ResearchData ready for the template.
    """
```

- [ ] **Step 5: AST-check the modified file**

Run:

```bash
python -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read()); print('AST clean')"
```

Expected: `AST clean`.

- [ ] **Step 6: Run ALL canonical test suites — confirm none of them broke**

Run each in turn (one of them — `test_bot_state.py` — exercises BotState.build, which is downstream of `research_data.py`'s changes; this is the regression check):

```bash
PYTHONIOENCODING=utf-8 python test_raw_inputs.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_gamma_flip.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_iv_state.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_exposures.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_bot_state.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_expiration.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
```

Expected output, all six lines:

```
PASSED: 13 / 13
PASSED: 14 / 14
PASSED: 8 / 8
PASSED: 9 / 9
PASSED: 17 / 17
PASSED: 17 / 17
```

(Total: 78 tests.)

If any test file shows a FAILED line or a different PASSED count, STOP. The most likely cause is `research_data.py` calling `build_ticker_snapshot` with the wrong arg name somewhere we missed. Search for any remaining `expiration=` calls in `research_data.py` and double-check.

- [ ] **Step 7: Commit**

```bash
git add omega_dashboard/research_data.py
git commit -m "Patch A.7: research_data uses canonical_expiration per ticker"
```

---

## Task 8: Update CLAUDE.md and final integration check

Per the living-context rule in CLAUDE.md: "When we make decisions during a session that future sessions will need to know — new vocabulary, new conventions, architectural choices, roadmap status changes, 'we tried X and it didn't work' lessons — proactively suggest updates to this file before the session ends."

`canonical_expiration` is a new canonical wrapper and a new vocabulary term. Both belong in CLAUDE.md.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the "Repo layout" canonical-rebuild listing**

Find this block in `CLAUDE.md` (under "Repo layout (what matters)", in the "Canonical rebuild" section):

```
Canonical rebuild (the v11 work — see "Canonical rebuild" below):
- `raw_inputs.py` — DataRouter wrapper bundle
- `bot_state.py` — single canonical state dataclass per ticker
- `canonical_gamma_flip.py` — wraps `ExposureEngine.gamma_flip`
- `canonical_iv_state.py` — wraps `UnifiedIVSurface`
- `canonical_exposures.py` — wraps `ExposureEngine.compute()`
- `test_*.py` for each — runnable without network or Schwab credentials
```

Replace with:

```
Canonical rebuild (the v11 work — see "Canonical rebuild" below):
- `raw_inputs.py` — DataRouter wrapper bundle
- `bot_state.py` — single canonical state dataclass per ticker
- `canonical_gamma_flip.py` — wraps `ExposureEngine.gamma_flip`
- `canonical_iv_state.py` — wraps `UnifiedIVSurface`
- `canonical_exposures.py` — wraps `ExposureEngine.compute()`
- `canonical_expiration.py` — picks chain expiration by intent (zero_dte / front / t7 / t30 / t60)
- `test_*.py` for each — runnable without network or Schwab credentials
```

- [ ] **Step 2: Add a new vocabulary entry**

Find the "Project vocabulary" section. After the **walls** entry, before the **±25% blanket band / IV-aware band** entry, insert:

```
- **canonical_expiration intent** — short string describing which expiration
  to pick for a chain query. Five intents: `zero_dte` (today's chain), `front`
  (first DTE ≥ 1, never 0DTE), `t7` (first DTE ≥ 7), `t30` (first DTE ≥ 30),
  `t60` (first DTE ≥ 60). Resolved per-ticker via `canonical_expiration()`.
  Different intents = different chains = different walls; that's by design,
  not a bug. Display layers tag values with their intent (e.g. "Call Wall · 1DTE").
```

- [ ] **Step 3: Update the "what's done" line in the Canonical rebuild section**

Find:

```
What's done as of last session (v11.5):
- canonical_gamma_flip
- canonical_iv_state (replaces a brief mistake — see "Audit discipline" below)
- canonical_exposures (Greek aggregates: gex/dex/vanna/charm/gex_sign)
- canonical_walls — wiring-only patch; walls share canonical_exposures'
  ExposureEngine.compute() pass. No separate wrapper file. Wires
  call_wall/put_wall/gamma_wall to BotState; max_pain/pin_zone_low/
  pin_zone_high stay None pending a separate canonical.
- BotState with permissive build, 64 fields total, ~22 currently lit per ticker
- Research page replaces the old Diagnostic placeholder
```

Replace with:

```
What's done as of last session (v11.6 / Patch A):
- canonical_gamma_flip
- canonical_iv_state (replaces a brief mistake — see "Audit discipline" below)
- canonical_exposures (Greek aggregates: gex/dex/vanna/charm/gex_sign)
- canonical_walls — wiring-only patch; walls share canonical_exposures'
  ExposureEngine.compute() pass. No separate wrapper file. Wires
  call_wall/put_wall/gamma_wall to BotState; max_pain/pin_zone_low/
  pin_zone_high stay None pending a separate canonical.
- canonical_expiration — five-intent registry; replaces ad-hoc "next Friday"
  in Research page. Side effect: Research walls now use front non-0-DTE
  chains per ticker. Patch B (producer daemon) not yet shipped, so the page
  is still slow — only the EXPIRATION choice changed in this patch.
- BotState with permissive build, 64 fields total, ~22 currently lit per ticker
- Research page replaces the old Diagnostic placeholder
```

- [ ] **Step 4: Update the "What's queued" list to reflect Patch B as next**

Find:

```
What's queued (in roughly this order):
- canonical_technicals — RSI / MACD / ADX / VWAP. First-class for every engine.
- canonical_pivots — universal pivot math, simple consolidation
```

Replace with:

```
What's queued (in roughly this order):
- bot_state_producer (Patch B) — daemon thread + Redis-backed shared store.
  See spec at docs/superpowers/specs/2026-05-07-research-page-multi-dte-walls-design.md.
  Unlocks the Research page (Patch C) and silent thesis migration (Patch E later).
- Research reads from Redis (Patch C) — pure consumer; the 3-minute spin disappears here.
- Multi-DTE drilldown UI (Patch D) — click-to-expand front/t7/t30/t60 walls per card.
- canonical_technicals — RSI / MACD / ADX / VWAP. First-class for every engine.
- canonical_pivots — universal pivot math, simple consolidation
```

- [ ] **Step 5: Final full-suite test run**

Run all six test files one more time after the CLAUDE.md edits (CLAUDE.md isn't code, but this is the audit-rule-3 final check that nothing slipped):

```bash
PYTHONIOENCODING=utf-8 python test_raw_inputs.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_gamma_flip.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_iv_state.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_exposures.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_bot_state.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
PYTHONIOENCODING=utf-8 python test_canonical_expiration.py 2>&1 | grep -E "^PASSED|^FAILED" | head -2
```

Expected (unchanged from Task 7, this is the audit checkpoint):

```
PASSED: 13 / 13
PASSED: 14 / 14
PASSED: 8 / 8
PASSED: 9 / 9
PASSED: 17 / 17
PASSED: 17 / 17
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "Patch A.8: CLAUDE.md updated for canonical_expiration (v11.6)"
```

- [ ] **Step 7: Show the commit summary for this patch**

Run:

```bash
git log --oneline 'HEAD~8..HEAD'
```

Expected output: 8 commits, oldest to newest:

```
<sha> Patch A.8: CLAUDE.md updated for canonical_expiration (v11.6)
<sha> Patch A.7: research_data uses canonical_expiration per ticker
<sha> Patch A.6: canonical_expiration wrapper-consistency test (audit rule 5)
<sha> Patch A.5: canonical_expiration malformed-input + upstream-failure tests
<sha> Patch A.4: canonical_expiration front/t7/t30/t60 intents
<sha> Patch A.3: canonical_expiration zero_dte intent + helpers
<sha> Patch A.2: canonical_expiration arg validation + test scaffolding
<sha> Patch A.1: canonical_expiration module skeleton + intent constants
```

If any are missing or the order is wrong, the implementer can use `git rebase -i` to clean up before pushing. **Do not push** — pushing is the user's call after reviewing the diff.

---

## Acceptance Criteria

When all 8 tasks are complete:

- [ ] `canonical_expiration.py` exists, AST-clean, ~150 lines, exposes `canonical_expiration()` plus 5 INTENT_* constants and `VALID_INTENTS` frozenset
- [ ] `test_canonical_expiration.py` exists, AST-clean, ~250 lines, 17/17 tests passing
- [ ] All five existing canonical test suites still pass (13 + 14 + 8 + 9 + 17 = 61 — unchanged from before the patch)
- [ ] **Total tests after Patch A: 78** (61 existing + 17 new)
- [ ] `omega_dashboard/research_data.py` no longer contains `_default_expiration`; `build_ticker_snapshot` and `research_data` both take an `intent` argument with default `"front"`
- [ ] CLAUDE.md updated: new file in repo layout, new vocabulary entry, "what's done" line bumped to v11.6
- [ ] 8 small commits, one per task, each with a clear `Patch A.N:` prefix
- [ ] No push — that's the user's review gate

## What Patch A does NOT do (out of scope, see other patches)

- Does NOT make the Research page faster — that's Patch B (producer daemon) + Patch C (reader)
- Does NOT add the WALLS drilldown UI — that's Patch D
- Does NOT migrate silent thesis off `_get_0dte_chain` — that's Patch E (separate spec)
- Does NOT add the `producer_version` / Redis schema — those are Patch B
- Does NOT change `bot_state.py` — v3 spec resolved that intent metadata lives on the Redis envelope, not on the dataclass
