# Patch F — active_scanner technicals redirect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the five technicals functions in `active_scanner.py` (`_compute_rsi`, `_compute_macd`, `_compute_ema`, `_compute_adx`, `_rma`) with thin one-line delegations to `canonical_technicals.*`. Zero behavior change. Verified by a caller-level smoke test that includes an `_analyze_ticker` end-to-end sanity check. The now-orphaned MACD constants in active_scanner.py are deleted with a one-line breadcrumb pointing to canonical_technicals.

**Architecture:** Two-step TDD with documentation. F.1 adds a new test file that calls `active_scanner._compute_*` against synthetic data and asserts equality with `canonical_technicals.*` — passes today against native implementations, captures the wiring contract. F.2 replaces the bodies with delegations and deletes the orphan constants; the smoke test plus an `_analyze_ticker` end-to-end smoke must still pass. F.3 updates CLAUDE.md.

**Tech Stack:** Pure Python. Test convention matches existing `test_canonical_*.py` (PASSED/FAILED globals, custom assert helpers, `main()` runner; runnable as `python test_active_scanner_technicals_delegate.py` — no pytest).

**Audit discipline (non-negotiable, from CLAUDE.md):**
- AST-check after every Python file write: `python -c "import ast; ast.parse(open('PATH').read())"`.
- Every change carries a `# v11.7 (Patch F.N):` comment marker.
- Three separate commits — F.1, F.2, F.3 — no bundling.
- DO NOT touch `risk_manager.py`, `canonical_technicals.py`, or any other file beyond `active_scanner.py`, the new test file, and `CLAUDE.md`.
- Behavior change is forbidden in this patch. Same math, same numbers, same callers.

**Spec reference:** `docs/superpowers/specs/2026-05-09-canonical-technicals-patch-f-design.md` (commits `91313e6` + `1bbf48a`). The plan below is the executable form of that spec.

---

## File Structure

- **Create:** `test_active_scanner_technicals_delegate.py` — six tests (5 wrapper-consistency assertions across rsi/macd/ema/adx/rma + 1 `_analyze_ticker` end-to-end smoke). Independent of `test_canonical_technicals.py` (synthetic data builders duplicated locally on purpose).
- **Modify:** `active_scanner.py` — add `import canonical_technicals` after the existing import block; replace bodies of `_compute_ema` (lines 99-106), `_compute_rsi` (109-122), `_compute_macd` (125-146), `_rma` (195-210), `_compute_adx` (213-260) with one-line delegations; delete the `MACD_FAST/SLOW/SIGNAL` constant lines (82-84) and replace them with a one-line breadcrumb comment pointing to canonical_technicals. The constants block (`EMA_FAST`, `EMA_SLOW`, `RSI_PERIOD`, `WT_CHANNEL`, `WT_AVERAGE`) and the still-native `_compute_wavetrend` (149-184) and `_analyze_ticker` (267+) stay untouched.
- **Modify:** `CLAUDE.md` — three surgical edits: add an active_scanner.py bullet to the "Trading engine" repo-layout block, move "Patch F" from "What's queued" into "What's done" with a header bump (Patch E → Patch F), update "What's queued" with risk_manager (Patch G), broader RSI consolidation, and shim cleanup follow-ups.
- **Untouched:** `canonical_technicals.py`, `test_canonical_technicals.py`, `risk_manager.py`, `app.py`, `swing_scanner.py`, `unified_models.py`, `income_scanner.py`, `backtest/*`, all other production code.

---

## Task F.1 — Caller-level smoke test

**Files:**
- Create: `test_active_scanner_technicals_delegate.py`

- [ ] **Step 1: Write the new test file**

Create `test_active_scanner_technicals_delegate.py` with this exact content:

```python
"""
test_active_scanner_technicals_delegate.py — Verify the F.2 shim wiring.

Pre-F.2: this test confirms active_scanner._compute_* matches
canonical_technicals.* (already true — Patch E proved byte-identicalness).

Post-F.2: this test verifies the shim wiring — that active_scanner.X is
importable, present in the namespace, and delegates correctly. The
equality assertions become tautological (both paths call the same code),
but the test still catches: broken imports, missing names, accidental
deletion of a delegation wrapper, AttributeError on the canonical_technicals
side, and end-to-end composition via _analyze_ticker.

Math correctness is verified by test_canonical_technicals.py — don't
duplicate that work here.
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


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition was False")
        return False
    PASSED.append(msg)
    return True


def assert_no_exception(callable_, msg):
    try:
        result = callable_()
    except Exception as e:
        FAILED.append(f"{msg}: raised {type(e).__name__}: {e}")
        return None
    PASSED.append(msg)
    return result


# ───────────────────────────────────────────────────────────────────────
# Synthetic data — duplicated from test_canonical_technicals.py on purpose.
# Keeping the two test files independent: the canonical tests verify the
# math, this file verifies the shim wiring.
# ───────────────────────────────────────────────────────────────────────

def _ramp_closes(start: float, step: float, n: int) -> list:
    return [start + step * i for i in range(n)]


def _alternating_closes(base: float, amp: float, n: int) -> list:
    return [base + amp * (1 if i % 2 == 0 else -1) for i in range(n)]


def _gentle_oscillation(n: int) -> list:
    return [100.0 + 5.0 * math.sin(i / 4.0) for i in range(n)]


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


# ───────────────────────────────────────────────────────────────────────
# Wrapper-consistency: active_scanner.X == canonical_technicals.X
# ───────────────────────────────────────────────────────────────────────

def test_compute_rsi_matches_canonical():
    from active_scanner import _compute_rsi
    from canonical_technicals import rsi
    closes_a = _ramp_closes(100.0, 1.0, 30)
    closes_b = _gentle_oscillation(60)
    closes_c = _alternating_closes(100.0, 1.0, 40)
    assert_eq(_compute_rsi(closes_a), rsi(closes_a),
              "_compute_rsi delegates: ramp default period")
    assert_eq(_compute_rsi(closes_a, 7), rsi(closes_a, 7),
              "_compute_rsi delegates: ramp period=7")
    assert_eq(_compute_rsi(closes_b), rsi(closes_b),
              "_compute_rsi delegates: oscillation default period")
    assert_eq(_compute_rsi(closes_c, 21), rsi(closes_c, 21),
              "_compute_rsi delegates: alternating period=21")


def test_compute_macd_matches_canonical():
    from active_scanner import _compute_macd
    from canonical_technicals import macd
    closes_a = _ramp_closes(100.0, 0.5, 60)
    closes_b = _gentle_oscillation(80)
    closes_c = _alternating_closes(100.0, 1.0, 80)
    assert_eq(_compute_macd(closes_a), macd(closes_a),
              "_compute_macd delegates: ramp")
    assert_eq(_compute_macd(closes_b), macd(closes_b),
              "_compute_macd delegates: oscillation")
    assert_eq(_compute_macd(closes_c), macd(closes_c),
              "_compute_macd delegates: alternating")


def test_compute_ema_matches_canonical():
    from active_scanner import _compute_ema
    from canonical_technicals import _ema
    values = _ramp_closes(100.0, 1.0, 30)
    assert_eq(_compute_ema(values, 12), _ema(values, 12),
              "_compute_ema delegates: ramp period=12")
    assert_eq(_compute_ema(values, 26), _ema(values, 26),
              "_compute_ema delegates: ramp period=26")
    assert_eq(_compute_ema([1.0, 2.0, 3.0], 5), _ema([1.0, 2.0, 3.0], 5),
              "_compute_ema delegates: insufficient data → []")


def test_compute_adx_matches_canonical():
    from active_scanner import _compute_adx
    from canonical_technicals import adx
    h_up,  l_up,  c_up  = _ohlc_uptrend(80)
    h_ch,  l_ch,  c_ch  = _ohlc_choppy(80)
    assert_eq(_compute_adx(h_up, l_up, c_up), adx(h_up, l_up, c_up),
              "_compute_adx delegates: uptrend default length")
    assert_eq(_compute_adx(h_up, l_up, c_up, 7),
              adx(h_up, l_up, c_up, 7),
              "_compute_adx delegates: uptrend length=7")
    assert_eq(_compute_adx(h_ch, l_ch, c_ch), adx(h_ch, l_ch, c_ch),
              "_compute_adx delegates: choppy")


def test_rma_matches_canonical():
    from active_scanner import _rma as as_rma
    from canonical_technicals import _rma as ct_rma
    values = _ramp_closes(1.0, 0.1, 50)
    assert_eq(as_rma(values, 14), ct_rma(values, 14),
              "_rma delegates: ramp length=14")
    assert_eq(as_rma([], 14), ct_rma([], 14),
              "_rma delegates: empty list")


# ───────────────────────────────────────────────────────────────────────
# End-to-end smoke: _analyze_ticker composes correctly through the shims.
# Asserts no exception; result is None or a dict (matches the function's
# documented return contract). Doesn't assert on signal content — the goal
# is to confirm the shim chain composes, not to verify scanner logic.
# ───────────────────────────────────────────────────────────────────────

def _make_fake_intraday(n_bars: int = 80):
    """Return a callable matching active_scanner's intraday_fn signature."""
    closes = _ramp_closes(100.0, 0.1, n_bars)
    highs  = [c + 0.5 for c in closes]
    lows   = [c - 0.5 for c in closes]
    volumes = [100_000] * n_bars  # liquid enough to bypass low-ADTV filter
    bars = {"c": closes, "h": highs, "l": lows, "v": volumes}

    def fake(ticker, resolution=5, countback=80):
        return bars

    return fake


def _make_fake_daily(n_days: int = 30):
    """Return a callable matching active_scanner's daily_candle_fn signature."""
    closes = _ramp_closes(100.0, 0.5, n_days)

    def fake(ticker, days=30):
        return closes

    return fake


def test_analyze_ticker_smoke():
    """End-to-end: _analyze_ticker runs through every shim without raising.

    Uses ticker='SPY' to bypass the low-ADTV reject path. Synthetic data
    is deterministic and sufficient for RSI/MACD/EMA/ADX to populate.
    Result is allowed to be None (no setup detected) or a dict (signal
    detected) — both are valid. The assertion is simply that the function
    completes without raising and returns the right type.
    """
    from active_scanner import _analyze_ticker
    intraday = _make_fake_intraday(80)
    daily    = _make_fake_daily(30)

    result = assert_no_exception(
        lambda: _analyze_ticker(
            ticker="SPY",
            intraday_fn=intraday,
            daily_candle_fn=daily,
            regime="NORMAL",
        ),
        "_analyze_ticker smoke: runs without raising through all shims",
    )

    assert_true(result is None or isinstance(result, dict),
                "_analyze_ticker smoke: returns None or dict per contract")


# ───────────────────────────────────────────────────────────────────────
# Test runner
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_compute_rsi_matches_canonical,
        test_compute_macd_matches_canonical,
        test_compute_ema_matches_canonical,
        test_compute_adx_matches_canonical,
        test_rma_matches_canonical,
        test_analyze_ticker_smoke,
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

- [ ] **Step 2: AST-check the new file**

Run: `python -c "import ast; ast.parse(open('test_active_scanner_technicals_delegate.py').read())"`
Expected: silent success (no traceback).

- [ ] **Step 3: Run the smoke tests against today's native active_scanner**

Run: `python test_active_scanner_technicals_delegate.py`
Expected: all 6 tests pass; PASSED count is around 19 (4 + 3 + 3 + 3 + 2 + 2 = 17 wrapper-consistency assertions + 2 from the smoke test). FAILED is empty. Exit 0.

If a wrapper-consistency test fails: the existing `active_scanner._compute_*` and `canonical_technicals.*` are not byte-identical, which would contradict Patch E's wrapper-consistency proofs. Investigate `canonical_technicals.py` and the source functions before proceeding.

If `test_analyze_ticker_smoke` fails: read the failure message. Common causes — missing `tzdata` on Windows (install: `pip install tzdata`); `current_phase()` raising an unhandled exception (it's wrapped in try/except but check that path); the synthetic data not being shaped right (each list must be length 80, dict keys "c", "h", "l", "v").

If you encounter any failure that suggests the test itself is buggy, stop and report — don't keep editing the test until it passes against broken active_scanner code.

- [ ] **Step 4: Commit**

```bash
git add test_active_scanner_technicals_delegate.py
git commit -m "Patch F.1: caller-level smoke test for active_scanner technicals delegation"
```

---

## Task F.2 — Redirect five functions + delete MACD constants

**Files:**
- Modify: `active_scanner.py`

- [ ] **Step 1: Pre-flight grep — confirm the MACD constants are dead post-redirect**

Run two greps to confirm `MACD_FAST`, `MACD_SLOW`, and `MACD_SIGNAL` are referenced only inside `_compute_macd` (whose body we're about to replace) and not imported externally:

```bash
grep -nE "MACD_FAST|MACD_SLOW|MACD_SIGNAL" active_scanner.py
```

Expected output (lines may differ slightly from session to session):

```
82:MACD_FAST   = 12
83:MACD_SLOW   = 26
84:MACD_SIGNAL = 9
126:    if len(closes) < MACD_SLOW + MACD_SIGNAL:
128:    ema_fast = _compute_ema(closes, MACD_FAST)
129:    ema_slow = _compute_ema(closes, MACD_SLOW)
130:    offset = MACD_SLOW - MACD_FAST
132:    if len(macd_line) < MACD_SIGNAL:
134:    signal = _compute_ema(macd_line, MACD_SIGNAL)
```

Three definition lines (82-84) and six usage lines, all inside `_compute_macd`. No other reference.

```bash
grep -rnE "from active_scanner import.*MACD_|active_scanner\.MACD_" --include="*.py"
```

Expected output: no matches (no external file imports the constants from active_scanner).

If either grep returns more than the expected hits — particularly if any file outside active_scanner.py references `MACD_FAST`/`MACD_SLOW`/`MACD_SIGNAL` — STOP. The constants are NOT dead and should not be deleted in this patch. Report the unexpected hits to the controller; the spec needs revision before F.2 proceeds.

- [ ] **Step 2: Add the canonical_technicals import**

In `active_scanner.py`, locate the import block at the top of the file (it ends with `from ticker_rules import (...)` around line 52). After the closing `)` of that block, add a blank line and then this import:

```python
import canonical_technicals  # v11.7 (Patch F.2): canonical home for RSI/MACD/EMA/ADX.
```

The import lands after the last existing import and before the `log = logging.getLogger(__name__)` line (around line 54).

- [ ] **Step 3: AST-check after the import addition**

Run: `python -c "import ast; ast.parse(open('active_scanner.py').read())"`
Expected: silent success.

- [ ] **Step 4: Replace the body of `_compute_ema`**

Find the existing `_compute_ema` definition at active_scanner.py:99-106:

```python
def _compute_ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    ema = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema
```

Replace its entire body (keeping the `def` line and signature unchanged) with:

```python
def _compute_ema(values: list, period: int) -> list:
    # v11.7 (Patch F.2): delegated to canonical_technicals.
    return canonical_technicals._ema(values, period)
```

Note: the existing function has no docstring — there's nothing to preserve. The new body is one line plus the marker comment.

- [ ] **Step 5: Replace the body of `_compute_rsi`**

Find the existing `_compute_rsi` definition at active_scanner.py:109-122 and replace its entire body with:

```python
def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    # v11.7 (Patch F.2): delegated to canonical_technicals.
    return canonical_technicals.rsi(closes, period)
```

- [ ] **Step 6: Replace the body of `_compute_macd`**

Find the existing `_compute_macd` definition at active_scanner.py:125-146 and replace its entire body with:

```python
def _compute_macd(closes: list) -> Dict:
    # v11.7 (Patch F.2): delegated to canonical_technicals.
    return canonical_technicals.macd(closes)
```

- [ ] **Step 7: Replace the body of `_rma`**

Find the existing `_rma` definition at active_scanner.py:195-210 and replace its entire body (signature unchanged; the existing docstring goes away — canonical_technicals._rma carries the canonical docstring now):

```python
def _rma(values: list, length: int) -> list:
    # v11.7 (Patch F.2): delegated to canonical_technicals.
    return canonical_technicals._rma(values, length)
```

- [ ] **Step 8: Replace the body of `_compute_adx`**

Find the existing `_compute_adx` definition at active_scanner.py:213-260 and replace its entire body (signature unchanged; existing docstring goes away):

```python
def _compute_adx(highs: list, lows: list, closes: list, length: int = 14) -> float:
    # v11.7 (Patch F.2): delegated to canonical_technicals.
    return canonical_technicals.adx(highs, lows, closes, length)
```

- [ ] **Step 9: Delete the orphan MACD constants**

Find lines 82-84 in `active_scanner.py`:

```python
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
```

Replace those three lines with this single breadcrumb comment (preserving the surrounding constants block layout):

```python
# v11.7 (Patch F.2): MACD_FAST/SLOW/SIGNAL moved to canonical_technicals.MACD_FAST/SLOW/SIGNAL.
```

The surrounding lines 80-81 (`EMA_FAST    = 5` / `EMA_SLOW    = 12`) and 85-87 (`RSI_PERIOD  = 14` / `WT_CHANNEL  = 10` / `WT_AVERAGE  = 21`) stay untouched.

- [ ] **Step 10: AST-check after all the body replacements and the constant deletion**

Run: `python -c "import ast; ast.parse(open('active_scanner.py').read())"`
Expected: silent success.

If parse fails: read the traceback. Common cause — accidental trailing whitespace from the body replacement, or an unclosed brace if the editor mis-replaced. Fix and re-AST-check before moving on.

- [ ] **Step 11: Importability smoke**

Run:

```bash
python -c "import active_scanner; print(active_scanner._compute_rsi([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115]))"
```

Expected: a number is printed (the RSI of a 16-element ramp, equal to 100.0 since the input is monotonically increasing). NOT a traceback.

If this raises `AttributeError`: the canonical_technicals import is missing or misnamed.
If this raises `ImportError`: market_clock or another upstream import is broken (likely a missing tzdata package on Windows).

- [ ] **Step 12: Run the F.1 smoke test against the redirected active_scanner**

Run: `python test_active_scanner_technicals_delegate.py`
Expected: all 6 tests pass. Same PASSED count as in F.1's Step 3 (around 19). FAILED is empty. Exit 0.

If any wrapper-consistency test now fails: the delegation is wrong (wrong canonical function name, wrong argument order, accidental change to a signature). Diff your active_scanner.py changes against the spec.

If `test_analyze_ticker_smoke` now fails: the shim chain is broken end-to-end. Common causes — `_compute_wavetrend` calling `_compute_ema` with wrong args (it shouldn't, since the signature is unchanged); a typo in one of the delegations; the canonical_technicals import not landing at module level.

- [ ] **Step 13: Run test_canonical_technicals.py — must still pass (now partly tautological)**

Run: `python test_canonical_technicals.py`
Expected: all 21 tests pass. Several wrapper-consistency tests in this file (those that import `from active_scanner import _compute_*`) now compare canonical_technicals to itself — they pass tautologically, but should still pass.

If any test in this file fails: something is wrong with canonical_technicals.py itself (which Patch F should not have touched), OR the active_scanner shim raises an exception that the wrapper-consistency comparison catches differently. Re-read your active_scanner.py edits.

- [ ] **Step 14: Run all 8 sibling regression suites**

Run each (they're all in the repo root):

```bash
python test_raw_inputs.py
python test_canonical_gamma_flip.py
python test_canonical_iv_state.py
python test_canonical_exposures.py
python test_canonical_expiration.py
python test_bot_state.py
python test_bot_state_producer.py
python test_research_data_consumer.py
```

Expected: every suite passes. None of these depend on active_scanner directly, but the regression check guards against unintended cross-module side effects.

If any suite regresses: read the failure carefully. If active_scanner is the cause, revert your changes and investigate. If the failure is unrelated (a flaky test, a known issue), document it in the commit message but don't paper over an active_scanner-induced regression.

- [ ] **Step 15: Commit**

```bash
git add active_scanner.py
git commit -m "Patch F.2: redirect active_scanner technicals to canonical_technicals"
```

---

## Task F.3 — CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

The three edits below are surgical. Use Read to locate each anchor first, then Edit to make the change. Do NOT do "while I'm here" cleanups (e.g., the pre-existing `WWhat's queued` typo flagged in the Patch E review stays untouched — it's a separate one-line patch when convenient).

- [ ] **Step 1: Add the active_scanner repo-layout bullet**

Open `CLAUDE.md`, locate the "Trading engine (production, ~11k lines):" section near the top of the "Repo layout" block. The current bullets are:

```
- `app.py` — main process, Telegram bridge, scheduling, Schwab adapter glue
- `dashboard.py` — Flask app for the omega_dashboard
- `rate_limiter.py`, `thesis_monitor.py`, `schwab_stream.py`,
  `schwab_adapter.py`, `recommendation_tracker.py`, `persistent_state.py`,
  `oi_flow.py`, `position_monitor.py`, etc.
```

Immediately after the multi-name "etc." bullet (the third one above), add a new bullet:

```
- `active_scanner.py` — main intraday scanner (`_analyze_ticker`, ActiveScanner class). Technical-indicator helpers delegate to canonical_technicals as of Patch F.
```

Indentation matches the existing bullets (`- ` at column 0, body wraps at column 2 if the line is long).

- [ ] **Step 2: Bump the "What's done" header from Patch E to Patch F**

Find the line:

```
What's done as of last session (v11.7 / Patch E):
```

Change `Patch E` to `Patch F`:

```
What's done as of last session (v11.7 / Patch F):
```

- [ ] **Step 3: Add the Patch F bullet to "What's done"**

In the "What's done" bullet list (in the same block as the header you just bumped), find the existing "canonical_technicals (Patch E)" bullet (added during Patch E.4). Immediately after it, add a new bullet:

```
- Patch F (active_scanner technicals redirect) — `_compute_rsi/_compute_macd/_compute_ema/_compute_adx/_rma` in active_scanner.py are now thin delegation wrappers around `canonical_technicals.*`. Zero behavior change verified by `test_active_scanner_technicals_delegate.py` (6 tests including an `_analyze_ticker` end-to-end sanity check). Backtest imports keep working unchanged through the existing names. canonical_technicals is no longer library-with-no-readers — active_scanner is its first production consumer. Orphaned `MACD_FAST/SLOW/SIGNAL` constants in active_scanner.py deleted; canonical_technicals.MACD_FAST/SLOW/SIGNAL is the single source.
```

- [ ] **Step 4: Replace the "What's queued" Patch F bullet with the follow-up notes**

In the "What's queued" section, find the existing Patch F bullet (it currently reads something like "Patch F: redirect active_scanner.py and risk_manager.py to consume from canonical_technicals..."). Replace it with three new follow-up bullets:

```
- Patch G (or later): risk_manager ADX migration. risk_manager._compute_adx is SMA-seeded Wilder ADX, drifts from active_scanner's RMA-seeded variant (now canonical). Migration shifts ADX values, may shift regime classifier on borderline inputs. Plan: capture the actual numerical drift on real SPY data first (separate one-off script), include the drift in the commit message, gate behind env var if shift is meaningful.
- Later patch: RSI consolidation across `app.py:_rsi`, `unified_models.py:_rsi` (Wilder-smoothed, different from canonical), `swing_scanner.py:_rsi` (Wilder-smoothed AND list-returning, different shape), `income_scanner.compute_rsi` (close to canonical, rounds to 1 decimal). Needs design pass — Wilder-smoothed canonical, OR migration with documented drift, OR list-returning canonical for swing_scanner. Multiple sub-patches when it lands.
- Eventual cleanup: delete the `_compute_*` shims in active_scanner once nothing imports them. Requires confirming no external caller references the legacy names.
```

The existing Patch E.5 (canonical_vwap) bullet stays unchanged (still queued).

- [ ] **Step 5: Proofread the diff**

Run: `git diff CLAUDE.md`
Expected: only the four edits described above (one bullet added to repo layout; one header bumped; one bullet added to "What's done"; one bullet replaced in "What's queued" with three new bullets). No accidental reformatting, no whitespace churn, no other section touched.

If you see any unintended changes: revert and redo the edit precisely. The "How I want to be talked to" / "Decisions already made" / "Known issues" / "Infrastructure" / "Quick smoke-test commands" sections must NOT change.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "Patch F.3: CLAUDE.md updated for active_scanner technicals redirect (v11.7)"
```

---

## Acceptance criteria

After all three tasks ship cleanly:

1. `test_active_scanner_technicals_delegate.py` exists with 6 tests (5 wrapper-consistency + 1 `_analyze_ticker` smoke). All pass.
2. `active_scanner.py` has `import canonical_technicals` after the existing import block.
3. `active_scanner._compute_rsi`, `_compute_macd`, `_compute_ema`, `_compute_adx`, `_rma` each have a one-line delegation body with the `# v11.7 (Patch F.2):` marker and no other content beyond the def line.
4. `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL` are removed from active_scanner.py and replaced with a one-line breadcrumb comment.
5. `_compute_wavetrend`, `_analyze_ticker`, and the constants `EMA_FAST/EMA_SLOW/RSI_PERIOD/WT_CHANNEL/WT_AVERAGE` are unchanged.
6. `python test_canonical_technicals.py` still passes (21 tests).
7. The 8 sibling regression suites all still pass (`test_raw_inputs`, `test_canonical_gamma_flip`, `test_canonical_iv_state`, `test_canonical_exposures`, `test_canonical_expiration`, `test_bot_state`, `test_bot_state_producer`, `test_research_data_consumer`).
8. `python -c "import active_scanner; print(active_scanner._compute_rsi([100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115]))"` prints a number (100.0 expected for that ramp).
9. CLAUDE.md updated: active_scanner repo-layout bullet, Patch F in "What's done" with header bump, Patch G / RSI consolidation / shim cleanup queued in "What's queued".
10. Three commits, each with `Patch F.N:` prefix on the message; each AST-checked.
11. `risk_manager.py`, `canonical_technicals.py`, and all other production files are unchanged. Verifiable: `git diff main~3..main -- risk_manager.py canonical_technicals.py` returns empty.

---

## Out of scope (deferred to future patches)

- **risk_manager._compute_adx migration** (Patch G or later): SMA-seeded Wilder ADX drifts from canonical RMA-seeded variant. Behavior change on regime classifier requires drift measurement and possibly an env-var gate.
- **app.py / unified_models.py / swing_scanner.py / income_scanner.py RSI consolidation**: different math (Wilder-smoothed) and different shapes (list-returning); needs Wilder-smoothed canonical or documented drift policy.
- **Deleting active_scanner._compute_* shims**: requires confirming no external caller references the legacy names. Future cleanup patch.
- **Updating backtest/* imports to canonical_technicals directly**: backtest files keep importing from active_scanner; the shims forward correctly. Future cleanup.
- **canonical_vwap / Patch E.5**: separate design pass.
- **`_compute_wavetrend` lift**: not part of canonical-rebuild scope per Patch E note.

---

## Risk and rollback

**Behavior change risk:** none expected. The delegation calls byte-identical math through a different name. Patch E proved byte-identicalness. F.1's smoke test confirms post-redirect behavior matches.

**Production blast radius:** low. Affects exactly one production code path — `active_scanner._analyze_ticker`. No new env vars, no new dependencies, no schema changes. Render auto-rebuild is safe.

**Rollback:**
- Revert F.2 alone (`git revert <F.2 SHA>`) restores native math implementations and the MACD constants. F.1 (smoke test) and F.3 (docs) are independently revertable.
- Full rollback: `git revert <F.3 SHA> <F.2 SHA> <F.1 SHA>` brings the working tree back to the pre-F.1 state.
