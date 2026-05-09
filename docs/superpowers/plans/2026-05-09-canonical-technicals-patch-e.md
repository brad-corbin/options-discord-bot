# Patch E — canonical_technicals lift Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift RSI / MACD / ADX math byte-identically out of `active_scanner.py` into a new `canonical_technicals.py` module, with wrapper-consistency tests proving identity. Does NOT touch any caller — pure additive lift. Patch F migrates callers.

**Scope discipline:** Patch E is intentionally scoped to **stateless indicators only**. VWAP is deferred to its own patch (Patch E.5 or later) because three implementations exist today — `vwap_bands.py`, `bar_state.py`, `income_scanner.py` — and reconciliation requires session-anchor and band-multiplier design decisions, not just a lift. RSI / MACD / ADX are pure functions of a closes (and for ADX, highs/lows) array; that simpler shape makes byte-identical lift safe and verifiable.

**Architecture:** New module `canonical_technicals.py` exposes `rsi(closes, period=14)`, `macd(closes)`, and `adx(highs, lows, closes, length=14)` plus the private helpers `_ema(values, period)` and `_rma(values, length)`. Each public function is a verbatim copy of `active_scanner._compute_rsi` / `_compute_macd` / `_compute_adx`. Wrapper-consistency tests import BOTH `canonical_technicals.X` and `active_scanner._compute_X`, run identical inputs through both, and assert exact equality across a range of synthetic close/high/low arrays. ADX uses the active_scanner version (RMA-seeded) — Brad has confirmed risk_manager's SMA-seeded version drifts and will be reconciled in Patch F.

**Tech Stack:** Pure Python (no numpy / no pandas — same as the originals). Test pattern matches the existing `test_canonical_*.py` convention: plain script with `PASSED` / `FAILED` global lists, custom `assert_eq` / `assert_approx` helpers, runnable via `python3 test_canonical_technicals.py`.

**Audit discipline (mandatory per CLAUDE.md):**
- Each task is one separate commit.
- AST-check after every Python file write: `python3 -c "import ast; ast.parse(open('PATH').read())"`.
- Every change carries a `# v11.7 (Patch E.N):` comment marker.
- DO NOT touch `active_scanner.py` or `risk_manager.py`. Patch E is additive only.
- Tests run with `python3 test_canonical_technicals.py` — no pytest.

---

## File Structure

- **Create:** `canonical_technicals.py` — public functions `rsi`, `macd`, `adx`; private helpers `_ema`, `_rma`; module-level constants `MACD_FAST=12`, `MACD_SLOW=26`, `MACD_SIGNAL=9` (matching `active_scanner.py:82-84`).
- **Create:** `test_canonical_technicals.py` — wrapper-consistency tests (canonical vs. `active_scanner._compute_*`) plus deterministic input/output cases plus edge cases.
- **Modify:** `CLAUDE.md` — add Patch E to "What's done" section, add `canonical_technicals` to repo layout.
- **Untouched (per scoping):** `active_scanner.py`, `risk_manager.py`, `bot_state.py`, `bot_state_producer.py`, `omega_dashboard/research_data.py`, dashboard templates. None of these consume canonical_technicals yet — that wiring is Patch F.

---

## Task E.1 — Module scaffold + RSI lift

**Files:**
- Create: `canonical_technicals.py`
- Create: `test_canonical_technicals.py`

- [ ] **Step 1: Write the failing test file with RSI tests**

Create `test_canonical_technicals.py`:

```python
"""
test_canonical_technicals.py — Unit + wrapper-consistency tests for
canonical_technicals.

Wrapper-consistency: every public function in canonical_technicals must
produce byte-identical output to its source-of-truth in active_scanner.
We import the source directly and compare. If the canonical ever drifts,
these tests fail.
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_approx(actual, expected, tol, msg):
    if actual is None or abs(actual - expected) > tol:
        FAILED.append(f"{msg}: expected ~{expected} ±{tol}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition was False")
        return False
    PASSED.append(msg)
    return True


# ───────────────────────────────────────────────────────────────────────
# Deterministic synthetic data
# ───────────────────────────────────────────────────────────────────────

def _ramp_closes(start: float, step: float, n: int) -> list:
    return [start + step * i for i in range(n)]


def _alternating_closes(base: float, amp: float, n: int) -> list:
    return [base + amp * (1 if i % 2 == 0 else -1) for i in range(n)]


def _gentle_oscillation(n: int) -> list:
    return [100.0 + 5.0 * math.sin(i / 4.0) for i in range(n)]


# ───────────────────────────────────────────────────────────────────────
# RSI tests
# ───────────────────────────────────────────────────────────────────────

def test_rsi_insufficient_data_returns_none():
    from canonical_technicals import rsi
    assert_is_none(rsi([100.0, 101.0]), "rsi: <period+1 closes returns None")
    assert_is_none(rsi([], 14), "rsi: empty list returns None")
    assert_is_none(rsi(_ramp_closes(100, 1, 14), 14), "rsi: exactly period closes returns None")


def test_rsi_pure_uptrend_returns_100():
    from canonical_technicals import rsi
    closes = _ramp_closes(100.0, 1.0, 30)
    val = rsi(closes, period=14)
    assert_approx(val, 100.0, 1e-9, "rsi: monotonic uptrend → 100")


def test_rsi_pure_downtrend_near_zero():
    from canonical_technicals import rsi
    closes = _ramp_closes(200.0, -1.0, 30)
    val = rsi(closes, period=14)
    assert_true(val is not None and val < 1.0, "rsi: monotonic downtrend → ~0")


def test_rsi_wrapper_consistency_uptrend():
    """Canonical rsi must match active_scanner._compute_rsi byte-for-byte."""
    from canonical_technicals import rsi as canon
    from active_scanner import _compute_rsi as src
    closes = _ramp_closes(100.0, 1.0, 30)
    assert_eq(canon(closes), src(closes),
              "rsi wrapper-consistency: uptrend default period")
    assert_eq(canon(closes, 7), src(closes, 7),
              "rsi wrapper-consistency: uptrend period=7")


def test_rsi_wrapper_consistency_oscillation():
    from canonical_technicals import rsi as canon
    from active_scanner import _compute_rsi as src
    closes = _gentle_oscillation(60)
    assert_eq(canon(closes), src(closes),
              "rsi wrapper-consistency: oscillation default period")
    assert_eq(canon(closes, 21), src(closes, 21),
              "rsi wrapper-consistency: oscillation period=21")


def test_rsi_wrapper_consistency_alternating():
    from canonical_technicals import rsi as canon
    from active_scanner import _compute_rsi as src
    closes = _alternating_closes(100.0, 1.0, 40)
    assert_eq(canon(closes), src(closes),
              "rsi wrapper-consistency: alternating up/down")


# ───────────────────────────────────────────────────────────────────────
# Test runner
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_rsi_insufficient_data_returns_none,
        test_rsi_pure_uptrend_returns_100,
        test_rsi_pure_downtrend_near_zero,
        test_rsi_wrapper_consistency_uptrend,
        test_rsi_wrapper_consistency_oscillation,
        test_rsi_wrapper_consistency_alternating,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAILED.append(f"{t.__name__}: unexpected exception "
                          f"{type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"PASSED: {len(PASSED)}")
    for p in PASSED:
        print(f"  ✓ {p}")
    if FAILED:
        print(f"\nFAILED: {len(FAILED)}")
        for f in FAILED:
            print(f"  ✗ {f}")
        sys.exit(1)
    print(f"\n{'='*60}")
    print("All tests passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails (canonical_technicals not yet defined)**

Run: `python3 test_canonical_technicals.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'canonical_technicals'` on every test.

- [ ] **Step 3: Create canonical_technicals.py with RSI**

Create `canonical_technicals.py`:

```python
"""
canonical_technicals.py — Single canonical home for RSI / MACD / ADX.

PURPOSE
-------
Multiple files in the codebase compute the same technical indicators by
hand (see active_scanner.py, risk_manager.py, app.py, swing_scanner.py,
income_scanner.py, unified_models.py, backtest_v3_runner.py, backtest/
bt_*.py). The canonical-rebuild discipline says: there should be ONE
implementation per concept, and a wrapper-consistency test should prove
the canonical matches its source-of-truth byte-for-byte.

Patch E lifts RSI / MACD / ADX out of active_scanner.py — those are the
versions the production trade-decision engines (V2 5D Edge Model, Long
Call Burst classifier, conviction scorer feature ingestion) depend on.

Patch E does NOT touch any caller. active_scanner.py, risk_manager.py
and friends keep their own implementations unchanged. Patch F redirects
callers to canonical_technicals and reconciles risk_manager's drifted
ADX (SMA-seeded Wilder, vs. active_scanner's RMA-seeded version that's
aligned with backtest_v3_runner's ind_adx quintile data).

CONVENTIONS
-----------
- Pure Python, no numpy / pandas. Mirrors the originals exactly.
- All math is byte-identical to active_scanner. Wrapper-consistency
  tests in test_canonical_technicals.py prove this.
- Public API: rsi(closes, period=14), macd(closes),
  adx(highs, lows, closes, length=14).
- Private helpers: _ema (for MACD), _rma (for ADX). These mirror
  active_scanner._compute_ema and active_scanner._rma respectively.

VERSION
-------
Lifted under Patch E (v11.7). See docs/superpowers/plans/
2026-05-09-canonical-technicals-patch-e.md.
"""

from __future__ import annotations

from typing import Dict, List, Optional


# v11.7 (Patch E.2): MACD constants — mirror active_scanner.py:82-84.
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


# v11.7 (Patch E.1): RSI lifted byte-identically from
# active_scanner._compute_rsi. Wilder's classic RSI but using simple
# averages over the last `period` gains/losses rather than RMA — this
# matches what the production scanner has shipped for years and what
# the conviction scorer's RSI quintile rules are calibrated against.
def rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))
```

- [ ] **Step 4: AST-check canonical_technicals.py**

Run: `python3 -c "import ast; ast.parse(open('canonical_technicals.py').read())"`
Expected: silent success (no traceback).

- [ ] **Step 5: AST-check test_canonical_technicals.py**

Run: `python3 -c "import ast; ast.parse(open('test_canonical_technicals.py').read())"`
Expected: silent success.

- [ ] **Step 6: Run RSI tests, verify all pass**

Run: `python3 test_canonical_technicals.py`
Expected: `PASSED: <n>`, no FAILED entries, exit 0. If any wrapper-consistency test fails, the lift is not byte-identical — diff against `active_scanner._compute_rsi` and fix.

- [ ] **Step 7: Commit**

```bash
git add canonical_technicals.py test_canonical_technicals.py
git commit -m "Patch E.1: canonical_technicals.rsi lifted from active_scanner"
```

---

## Task E.2 — MACD lift (+ _ema helper)

**Files:**
- Modify: `canonical_technicals.py` (add `_ema` and `macd`)
- Modify: `test_canonical_technicals.py` (add MACD tests + register them in `main()`)

- [ ] **Step 1: Append MACD tests to test_canonical_technicals.py**

In `test_canonical_technicals.py`, BEFORE the `def main():` block, append:

```python
# ───────────────────────────────────────────────────────────────────────
# MACD tests
# ───────────────────────────────────────────────────────────────────────

def test_macd_insufficient_data_returns_empty():
    from canonical_technicals import macd
    assert_eq(macd([100.0] * 20), {},
              "macd: <slow+signal closes returns {}")
    assert_eq(macd([]), {}, "macd: empty list returns {}")


def test_macd_returns_required_keys():
    from canonical_technicals import macd
    closes = _ramp_closes(100.0, 0.5, 60)
    out = macd(closes)
    for key in ("macd_line", "signal_line", "macd_hist",
                "macd_cross_bull", "macd_cross_bear"):
        assert_true(key in out, f"macd: result has key {key!r}")


def test_macd_wrapper_consistency_uptrend():
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _ramp_closes(100.0, 0.5, 60)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: uptrend")


def test_macd_wrapper_consistency_oscillation():
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _gentle_oscillation(80)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: oscillation")


def test_macd_wrapper_consistency_choppy():
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _alternating_closes(100.0, 1.0, 80)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: alternating")


def test_macd_wrapper_consistency_minimum_length():
    """Right at the boundary: slow + signal = 26 + 9 = 35 closes."""
    from canonical_technicals import macd as canon
    from active_scanner import _compute_macd as src
    closes = _ramp_closes(100.0, 0.3, 35)
    assert_eq(canon(closes), src(closes),
              "macd wrapper-consistency: 35-close minimum")


def test_ema_wrapper_consistency():
    from canonical_technicals import _ema as canon
    from active_scanner import _compute_ema as src
    values = _ramp_closes(100.0, 1.0, 30)
    assert_eq(canon(values, 12), src(values, 12),
              "_ema wrapper-consistency: ramp period=12")
    assert_eq(canon(values, 26), src(values, 26),
              "_ema wrapper-consistency: ramp period=26")
    assert_eq(canon([1.0, 2.0, 3.0], 5), src([1.0, 2.0, 3.0], 5),
              "_ema wrapper-consistency: insufficient data → []")
```

Then update the `tests` list in `main()` to include the new tests:

```python
    tests = [
        test_rsi_insufficient_data_returns_none,
        test_rsi_pure_uptrend_returns_100,
        test_rsi_pure_downtrend_near_zero,
        test_rsi_wrapper_consistency_uptrend,
        test_rsi_wrapper_consistency_oscillation,
        test_rsi_wrapper_consistency_alternating,
        test_macd_insufficient_data_returns_empty,
        test_macd_returns_required_keys,
        test_macd_wrapper_consistency_uptrend,
        test_macd_wrapper_consistency_oscillation,
        test_macd_wrapper_consistency_choppy,
        test_macd_wrapper_consistency_minimum_length,
        test_ema_wrapper_consistency,
    ]
```

- [ ] **Step 2: AST-check the test file**

Run: `python3 -c "import ast; ast.parse(open('test_canonical_technicals.py').read())"`
Expected: silent success.

- [ ] **Step 3: Run tests to verify MACD tests fail**

Run: `python3 test_canonical_technicals.py`
Expected: RSI tests pass; MACD/`_ema` tests fail with `ImportError: cannot import name 'macd' / '_ema' from 'canonical_technicals'`.

- [ ] **Step 4: Append _ema and macd to canonical_technicals.py**

Append to `canonical_technicals.py` (after the `rsi` function):

```python
# v11.7 (Patch E.2): _ema helper lifted byte-identically from
# active_scanner._compute_ema. Used by macd().
def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    ema = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema


# v11.7 (Patch E.2): MACD lifted byte-identically from
# active_scanner._compute_macd. Returns macd_line, signal_line,
# macd_hist, and bull/bear cross flags. Returns {} when insufficient
# data — the conviction scorer treats {} as "MACD unavailable, skip".
def macd(closes: list) -> Dict:
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return {}
    ema_fast = _ema(closes, MACD_FAST)
    ema_slow = _ema(closes, MACD_SLOW)
    offset = MACD_SLOW - MACD_FAST
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < MACD_SIGNAL:
        return {}
    signal = _ema(macd_line, MACD_SIGNAL)
    hist = macd_line[-1] - signal[-1] if signal else 0
    return {
        "macd_line": macd_line[-1] if macd_line else 0,
        "signal_line": signal[-1] if signal else 0,
        "macd_hist": hist,
        "macd_cross_bull": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] < signal[-2]
                           and macd_line[-1] > signal[-1]) if signal else False,
        "macd_cross_bear": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] > signal[-2]
                           and macd_line[-1] < signal[-1]) if signal else False,
    }
```

- [ ] **Step 5: AST-check canonical_technicals.py**

Run: `python3 -c "import ast; ast.parse(open('canonical_technicals.py').read())"`
Expected: silent success.

- [ ] **Step 6: Run all tests, verify pass**

Run: `python3 test_canonical_technicals.py`
Expected: PASSED count includes all RSI + MACD + _ema tests, FAILED is empty, exit 0. If any wrapper-consistency test fails, the lift drifted — diff `_ema` / `macd` against `active_scanner._compute_ema` / `_compute_macd` and fix.

- [ ] **Step 7: Commit**

```bash
git add canonical_technicals.py test_canonical_technicals.py
git commit -m "Patch E.2: canonical_technicals.macd + _ema lifted from active_scanner"
```

---

## Task E.3 — ADX lift (+ _rma helper)

**Files:**
- Modify: `canonical_technicals.py` (add `_rma` and `adx`)
- Modify: `test_canonical_technicals.py` (add ADX tests + register them in `main()`)

- [ ] **Step 1: Append ADX tests to test_canonical_technicals.py**

In `test_canonical_technicals.py`, BEFORE the `def main():` block, append:

```python
# ───────────────────────────────────────────────────────────────────────
# ADX tests
# ───────────────────────────────────────────────────────────────────────

def _ohlc_uptrend(n: int):
    closes = _ramp_closes(100.0, 1.0, n)
    highs = [c + 0.5 for c in closes]
    lows  = [c - 0.5 for c in closes]
    return highs, lows, closes


def _ohlc_choppy(n: int):
    closes = _alternating_closes(100.0, 1.0, n)
    highs = [c + 0.5 for c in closes]
    lows  = [c - 0.5 for c in closes]
    return highs, lows, closes


def _ohlc_oscillation(n: int):
    closes = _gentle_oscillation(n)
    highs = [c + 0.4 for c in closes]
    lows  = [c - 0.4 for c in closes]
    return highs, lows, closes


def test_adx_insufficient_data_returns_zero():
    from canonical_technicals import adx
    h, l, c = _ohlc_uptrend(10)
    assert_eq(adx(h, l, c, 14), 0.0,
              "adx: <length+1 bars returns 0.0")
    assert_eq(adx([], [], [], 14), 0.0,
              "adx: empty arrays return 0.0")


def test_adx_mismatched_array_lengths_returns_zero():
    from canonical_technicals import adx
    closes = _ramp_closes(100.0, 1.0, 30)
    highs  = closes[:25]
    lows   = closes[:25]
    assert_eq(adx(highs, lows, closes, 14), 0.0,
              "adx: mismatched lengths return 0.0")


def test_adx_strong_trend_yields_positive_value():
    from canonical_technicals import adx
    h, l, c = _ohlc_uptrend(80)
    val = adx(h, l, c, 14)
    assert_true(val > 0.0, "adx: strong uptrend yields adx > 0")


def test_adx_wrapper_consistency_uptrend():
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_uptrend(80)
    assert_eq(canon(h, l, c), src(h, l, c),
              "adx wrapper-consistency: uptrend default length")
    assert_eq(canon(h, l, c, 7), src(h, l, c, 7),
              "adx wrapper-consistency: uptrend length=7")


def test_adx_wrapper_consistency_choppy():
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_choppy(80)
    assert_eq(canon(h, l, c), src(h, l, c),
              "adx wrapper-consistency: choppy")


def test_adx_wrapper_consistency_oscillation():
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_oscillation(120)
    assert_eq(canon(h, l, c), src(h, l, c),
              "adx wrapper-consistency: oscillation")


def test_adx_wrapper_consistency_minimum_length():
    """Right at the boundary: length+1 bars."""
    from canonical_technicals import adx as canon
    from active_scanner import _compute_adx as src
    h, l, c = _ohlc_uptrend(15)  # length=14, needs >= 15 bars
    assert_eq(canon(h, l, c, 14), src(h, l, c, 14),
              "adx wrapper-consistency: 15-bar minimum")


def test_rma_wrapper_consistency():
    from canonical_technicals import _rma as canon
    from active_scanner import _rma as src
    values = _ramp_closes(1.0, 0.1, 50)
    assert_eq(canon(values, 14), src(values, 14),
              "_rma wrapper-consistency: ramp length=14")
    assert_eq(canon([], 14), src([], 14),
              "_rma wrapper-consistency: empty list")
    assert_eq(canon([1.0, 2.0], 0), src([1.0, 2.0], 0),
              "_rma wrapper-consistency: length=0 returns []")
```

Then update the `tests` list in `main()` to include the new tests:

```python
    tests = [
        test_rsi_insufficient_data_returns_none,
        test_rsi_pure_uptrend_returns_100,
        test_rsi_pure_downtrend_near_zero,
        test_rsi_wrapper_consistency_uptrend,
        test_rsi_wrapper_consistency_oscillation,
        test_rsi_wrapper_consistency_alternating,
        test_macd_insufficient_data_returns_empty,
        test_macd_returns_required_keys,
        test_macd_wrapper_consistency_uptrend,
        test_macd_wrapper_consistency_oscillation,
        test_macd_wrapper_consistency_choppy,
        test_macd_wrapper_consistency_minimum_length,
        test_ema_wrapper_consistency,
        test_adx_insufficient_data_returns_zero,
        test_adx_mismatched_array_lengths_returns_zero,
        test_adx_strong_trend_yields_positive_value,
        test_adx_wrapper_consistency_uptrend,
        test_adx_wrapper_consistency_choppy,
        test_adx_wrapper_consistency_oscillation,
        test_adx_wrapper_consistency_minimum_length,
        test_rma_wrapper_consistency,
    ]
```

- [ ] **Step 2: AST-check the test file**

Run: `python3 -c "import ast; ast.parse(open('test_canonical_technicals.py').read())"`
Expected: silent success.

- [ ] **Step 3: Run tests to verify ADX tests fail**

Run: `python3 test_canonical_technicals.py`
Expected: RSI + MACD tests pass; ADX/`_rma` tests fail with `ImportError: cannot import name 'adx' / '_rma' from 'canonical_technicals'`.

- [ ] **Step 4: Append _rma and adx to canonical_technicals.py**

Append to `canonical_technicals.py` (after the `macd` function):

```python
# v11.7 (Patch E.3): _rma (Wilder's recursive moving average) lifted
# byte-identically from active_scanner._rma. Used by adx().
def _rma(values: list, length: int) -> list:
    """Wilder's smoothing (RMA). Recursive moving average.

    Lifted byte-identically from active_scanner._rma (which itself was
    ported from backtest_v3_runner.py to keep live ADX aligned with
    backtest's ind_adx quintile data). Used internally by adx().
    """
    if not values or length <= 0:
        return []
    out = []
    s = 0.0
    for i, v in enumerate(values):
        if i == 0:
            s = float(v)
        else:
            s = s + (float(v) - s) / length
        out.append(s)
    return out


# v11.7 (Patch E.3): ADX lifted byte-identically from
# active_scanner._compute_adx. RMA-seeded Wilder ADX (NOT the SMA-seeded
# variant in risk_manager._compute_adx — that one is documented in
# Patch F as "DRIFT: not canonical, reconcile to canonical_technicals.adx").
# Returns the most recent ADX reading or 0.0 on insufficient data /
# malformed inputs / arithmetic error. The conviction scorer treats 0.0
# as "ADX unavailable" and skips ADX-quintile rules accordingly.
def adx(highs: list, lows: list, closes: list, length: int = 14) -> float:
    """Compute the current ADX value from OHLC arrays.

    Returns the most recent ADX reading as a float. Returns 0.0 on any
    failure — the scorer's ADX quintile rules check for missing data
    and skip, so a silent zero is safe.

    Matches active_scanner._compute_adx exactly (which was ported from
    backtest_v3_runner.py:346-364 for backtest-vs-live alignment).
    """
    try:
        n = len(closes)
        if n < 2 or len(highs) != n or len(lows) != n:
            return 0.0
        if n < length + 1:
            # Not enough bars for Wilder's smoothing to stabilize
            return 0.0

        dmp = [0.0]
        dmn = [0.0]
        tr = [highs[0] - lows[0]]
        for i in range(1, n):
            up = highs[i] - highs[i - 1]
            dn = lows[i - 1] - lows[i]
            dmp.append(up if up > dn and up > 0 else 0.0)
            dmn.append(dn if dn > up and dn > 0 else 0.0)
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

        stt = _rma(tr, length)
        sp = _rma(dmp, length)
        sn = _rma(dmn, length)

        dip = [100 * sp[i] / stt[i] if stt[i] != 0 else 0.0 for i in range(n)]
        din = [100 * sn[i] / stt[i] if stt[i] != 0 else 0.0 for i in range(n)]

        dx = []
        for i in range(n):
            s = dip[i] + din[i]
            dx.append(100 * abs(dip[i] - din[i]) / s if s != 0 else 0.0)

        adx_series = _rma(dx, length)
        return float(adx_series[-1]) if adx_series else 0.0
    except Exception:
        # Defensive — never let ADX computation break signal analysis
        return 0.0
```

- [ ] **Step 5: AST-check canonical_technicals.py**

Run: `python3 -c "import ast; ast.parse(open('canonical_technicals.py').read())"`
Expected: silent success.

- [ ] **Step 6: Run all tests, verify pass**

Run: `python3 test_canonical_technicals.py`
Expected: All 21 tests pass, FAILED is empty, exit 0. If any wrapper-consistency test fails — especially the ADX ones — diff against `active_scanner._compute_adx` / `_rma` and fix.

- [ ] **Step 7: Run all canonical-rebuild test suites for regression check**

Run each (per CLAUDE.md "Quick smoke-test commands"):

```bash
python3 test_raw_inputs.py
python3 test_canonical_gamma_flip.py
python3 test_canonical_iv_state.py
python3 test_canonical_exposures.py
python3 test_canonical_expiration.py
python3 test_bot_state.py
python3 test_canonical_technicals.py
```

Expected: all pass. canonical_technicals is purely additive — no existing test should regress. If something does regress, stop and investigate (likely import side-effects in canonical_technicals.py or accidental name collision).

- [ ] **Step 8: Commit**

```bash
git add canonical_technicals.py test_canonical_technicals.py
git commit -m "Patch E.3: canonical_technicals.adx + _rma lifted from active_scanner"
```

---

## Task E.4 — CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

The "What's done as of last session" list and the "Repo layout" section need to mention canonical_technicals so the next Claude Code session has it in context.

- [ ] **Step 1: Read the current "Repo layout" section to find the canonical-rebuild block**

Open `CLAUDE.md`, locate the section beginning `Canonical rebuild (the v11 work — see "Canonical rebuild" below):`. Confirm the bullet list immediately under it.

- [ ] **Step 2: Add canonical_technicals to the repo-layout bullet list**

In the canonical-rebuild block of "Repo layout", add this line in the natural alphabetic / logical order — directly under `canonical_expiration.py`:

```
- `canonical_technicals.py` — RSI / MACD / ADX lifted byte-identically
  from `active_scanner._compute_rsi/_compute_macd/_compute_adx` (Patch E).
  Pure additive lift. Patch F redirects callers and reconciles
  risk_manager's drifted ADX.
```

- [ ] **Step 3: Update "What's done as of last session" to add Patch E**

Locate the bullet list starting `What's done as of last session (v11.7 / Patch D):`. After the `canonical_expiration` bullet (or adjacent to it), add:

```
- canonical_technicals (Patch E) — RSI / MACD / ADX lifted byte-identically
  out of active_scanner.py into canonical_technicals.py with wrapper-
  consistency tests. ADX uses active_scanner's RMA-seeded version (aligned
  with backtest_v3_runner.py:346-364 ind_adx quintile data). risk_manager's
  SMA-seeded _compute_adx is documented as DRIFT and reconciled in Patch F.
  Patch E is purely additive — no caller is modified.
```

Update the section header from `(v11.7 / Patch D)` to `(v11.7 / Patch E)` to reflect the new "as of" point.

- [ ] **Step 4: Update "What's queued" to remove canonical_technicals from the queue**

Locate the bullet list starting `What's queued (in roughly this order):`. Replace the line currently reading `canonical_technicals — RSI / MACD / ADX / VWAP. First-class for every engine.` with:

```
- Patch E.5 (or later): canonical_vwap — session VWAP + bands. Three
  implementations exist today (vwap_bands.py, bar_state.py, income_scanner.py).
  Reconciliation requires session-anchor and band-multiplier design decisions —
  it's not a stateless lift. Patch E was intentionally scoped to stateless
  indicators only; VWAP needs its own design pass.
- Patch F: redirect active_scanner.py and risk_manager.py to consume from
  canonical_technicals. Reconcile risk_manager's drifted ADX (document any
  regime-classification shift in commit message). Add DRIFT comment marker
  at risk_manager._compute_adx.
```

- [ ] **Step 5: Add canonical_technicals to "Quick smoke-test commands"**

Find the `# Run all canonical-rebuild test suites` block. Append before the `# Patch B producer test` line:

```bash
python3 test_canonical_technicals.py
```

- [ ] **Step 6: AST-irrelevant for CLAUDE.md, but proofread the diff**

Run: `git diff CLAUDE.md`
Expected: only the additions described above. No accidental edits to other sections, no markdown formatting drift. If anything else changed, revert it.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "Patch E.4: CLAUDE.md updated for canonical_technicals (v11.7)"
```

---

## Acceptance criteria

After all four tasks ship cleanly:

1. `canonical_technicals.py` exists with `rsi`, `macd`, `adx`, `_ema`, `_rma`, and the three MACD constants.
2. `test_canonical_technicals.py` runs with `python3 test_canonical_technicals.py` and all 21 tests pass, including 11 wrapper-consistency tests.
3. `python3 test_raw_inputs.py`, `python3 test_canonical_gamma_flip.py`, `python3 test_canonical_iv_state.py`, `python3 test_canonical_exposures.py`, `python3 test_canonical_expiration.py`, `python3 test_bot_state.py` still pass — no regression in any existing canonical test.
4. `active_scanner.py` and `risk_manager.py` are unchanged in this branch (verifiable via `git diff main -- active_scanner.py risk_manager.py` returning empty).
5. CLAUDE.md reflects Patch E in "What's done", "Repo layout", "What's queued", and "Quick smoke-test commands".
6. Each task committed separately with a `Patch E.N:` prefix on the message.
7. Every Python file written has been AST-checked.

---

## Out of scope (Patch F and beyond)

- VWAP — deferred to Patch E.5 (or later). Three implementations exist today: `vwap_bands.py`, `bar_state.py`, `income_scanner.py`. Reconciliation requires session-anchor and band-multiplier design decisions, not just a lift. Patch E is intentionally scoped to stateless indicators only.
- Migrating active_scanner.py / risk_manager.py to consume from canonical_technicals — Patch F.
- Adding the `# DRIFT: not canonical` comment to `risk_manager._compute_adx` — Patch F (touches risk_manager, out of scope here).
- Wiring canonical_technicals into BotState fields — separate canonical (rsi/macd/adx/vwap aren't currently BotState fields; that's a design decision to make alongside VWAP).
- Migrating other technicals consumers (`app.py:6966 _rsi`, `swing_scanner.py:98 _rsi`, `unified_models.py:28 _rsi`, `income_scanner.py:665 compute_rsi`, etc.) — same Patch F discussion; those add to the migration scope and may justify splitting Patch F into multiple sub-patches if it gets large.
