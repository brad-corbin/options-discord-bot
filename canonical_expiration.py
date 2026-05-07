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
