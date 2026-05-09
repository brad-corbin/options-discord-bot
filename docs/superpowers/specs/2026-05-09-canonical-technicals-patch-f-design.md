# Patch F — active_scanner technicals redirect

**Status:** Approved, ready for implementation planning
**Author:** Brad Corbin (decisions) + Claude (drafting)
**Date:** 2026-05-09
**Patch:** F (active_scanner caller redirect)
**Predecessor:** Patch E (canonical_technicals lift) — shipped 2026-05-09 (commits d97ae55 → b73de5b → a9f9521 on origin/main)
**Related but out of scope:** risk_manager ADX migration (later patch); RSI consolidation across app.py / unified_models.py / swing_scanner.py / income_scanner.py (later patch, needs Wilder-smoothed canonical design pass).

---

## 1. The Question

After Patch E, `canonical_technicals.py` is the single canonical home for stateless RSI / MACD / ADX math. But the production code path that actually consumes those functions — `active_scanner._analyze_ticker` and the half-dozen `backtest/bt_*.py` files that import from active_scanner — still calls the *original* implementations inside `active_scanner.py`. Two physical implementations of the same math exist; canonical_technicals is currently library-with-no-readers.

Patch F fixes that: redirect every technical-indicator function in `active_scanner.py` to delegate into `canonical_technicals.*`. Establishes the migration pattern. Reads no different from the outside; same math, same numbers, same callers, same imports.

## 2. What Patch F is NOT

- **Not a behavior change.** Same math runs, same numbers come out. Zero shift in signal generation, regime detection, or anything downstream.
- **Not a deletion.** `active_scanner._compute_rsi`, `_compute_macd`, `_compute_ema`, `_compute_adx`, and `_rma` continue to exist as Python names. Their bodies become one-line delegations. Backtest imports keep working unchanged.
- **Not the risk_manager ADX migration.** That one shifts ADX values (RMA-seeded vs SMA-seeded), which can move regime-classifier output on borderline inputs. It needs a side-by-side validation step before the cutover. Separate patch.
- **Not the broader RSI consolidation.** `app.py:_rsi`, `unified_models.py:_rsi`, and `swing_scanner.py:_rsi` use Wilder-smoothed RSI (different math). `swing_scanner._rsi` returns a list (different shape). `income_scanner.compute_rsi` rounds to 1 decimal. Each needs design — Wilder canonical? Drift policy? Adapter? — that's not Patch F's job.

## 3. The five functions

Source: `active_scanner.py` lines 99-260. Targets: `canonical_technicals.py`.

| Active scanner function | Lines | Delegates to | Notes |
|---|---|---|---|
| `_compute_rsi(closes, period=14)` | 109-122 | `canonical_technicals.rsi(closes, period)` | Public-style helper; called at active_scanner.py:348 and from backtest/bt_active_v8.py / bt_resolution_study_v3.py / v3_1.py / v2.py / .py |
| `_compute_macd(closes)` | 125-146 | `canonical_technicals.macd(closes)` | Public-style helper; called at active_scanner.py:344 and the same backtest files |
| `_compute_ema(values, period)` | 99-106 | `canonical_technicals._ema(values, period)` | Used internally by `_compute_macd` AND by `_compute_wavetrend` (which Patch E did NOT lift); also called from active_scanner.py:336/337/371/372 and backtest equivalents |
| `_compute_adx(highs, lows, closes, length=14)` | 213-260 | `canonical_technicals.adx(highs, lows, closes, length)` | Called at active_scanner.py:352 and backtest/bt_active_v8.py |
| `_rma(values, length)` | 195-210 | `canonical_technicals._rma(values, length)` | Used internally by `_compute_adx`; imported by `test_canonical_technicals.py:277` (`from active_scanner import _rma`) for wrapper-consistency tests; kept as a delegation wrapper for that and any future external callers |

After F.2, every active_scanner technical-indicator call (including `_compute_wavetrend`'s internal use of `_compute_ema`) lands in canonical_technicals. The `_compute_wavetrend` function itself stays native — its lift is a future patch.

## 4. Sub-patch breakdown

Three commits, each with its own `# v11.7 (Patch F.N):` marker. AST-check after every Python file write. No bundling.

### F.1 — Caller-level smoke test

**File:** `test_active_scanner_technicals_delegate.py` (new, repo root, matches `test_canonical_*.py` convention)

**Purpose:** Verify the shim wiring is correct. After F.2 lands, the test ALSO passes (tautologically, since both paths call the same code) — but its job is to prove the import surface exists and active_scanner's helper names produce the expected output. Pre-F.2, it passes because Patch E proved byte-identicalness; post-F.2, it passes because the delegation works.

**Test pattern (per Brad's refinement #1):** goldens are computed at test time via wrapper-consistency comparison, NOT hardcoded literals. The pattern is:

```python
from active_scanner import _compute_rsi
from canonical_technicals import rsi
assert _compute_rsi(closes) == rsi(closes)
```

Verifies: (a) the active_scanner name is importable, (b) it produces the same output as canonical_technicals' counterpart. Hardcoded goldens would duplicate canonical_technicals' own tests and create maintenance debt.

**Test file header comment (per Brad's refinement #2).** The new file's module docstring must explain its purpose to future readers — particularly that the assertions become tautological after F.2 lands but still serve a real role:

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
```

**Test functions** (one per delegation + one end-to-end):

1. `test_compute_rsi_matches_canonical` — synthetic ramp + oscillation + alternating closes at default period and one non-default period; assert active_scanner._compute_rsi(...) == canonical_technicals.rsi(...).
2. `test_compute_macd_matches_canonical` — same input shapes; assert dict equality.
3. `test_compute_ema_matches_canonical` — direct test of the helper at periods 12 and 26 (the values MACD uses).
4. `test_compute_adx_matches_canonical` — synthetic OHLC (uptrend, choppy) at default and length=7; assert exact equality.
5. `test_rma_matches_canonical` — direct helper test, ramp values at length=14.
6. `test_analyze_ticker_smoke` (per Brad's refinement #3) — constructs fake `intraday_fn` and `daily_candle_fn` that return synthetic OHLC bars sufficient for RSI/MACD/ADX to populate (~100 intraday bars, ~20 daily). Calls `active_scanner._analyze_ticker("TEST", fake_intraday, fake_daily, regime="NORMAL")`. Asserts no exception is raised AND the return is either `None` (no setup detected) or a dict with `rsi`, `macd_line`, and `adx_current` keys present. Confirms the shim chain composes end-to-end, not just in isolation.

**Synthetic data builders** are duplicated locally in this test file (NOT imported from `test_canonical_technicals.py`) to keep the two test files independent — one is the lift verifier, the other is the wiring verifier.

**Test runner pattern** matches existing `test_canonical_*.py` files: `PASSED` / `FAILED` global lists, custom assert helpers, `main()` runner with explicit test list, runnable as `python test_active_scanner_technicals_delegate.py`. No pytest.

**Acceptance:** All 6 test functions pass against the *current* (pre-F.2) native implementations. AST-check passes.

### F.2 — Redirect five functions

**File:** `active_scanner.py` (modify only)

**Pattern (per Brad's design choice):** thin delegation wrappers. Replace each function body with a one-line call to `canonical_technicals.*`. Keep the function definition (signature + decorator if any). Add `# v11.7 (Patch F.2):` marker on each.

**Concrete edits:**

```python
# v11.7 (Patch F.2): delegated to canonical_technicals.
def _compute_ema(values: list, period: int) -> list:
    return canonical_technicals._ema(values, period)


# v11.7 (Patch F.2): delegated to canonical_technicals.
def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    return canonical_technicals.rsi(closes, period)


# v11.7 (Patch F.2): delegated to canonical_technicals.
def _compute_macd(closes: list) -> Dict:
    return canonical_technicals.macd(closes)


# v11.7 (Patch F.2): delegated to canonical_technicals.
def _rma(values: list, length: int) -> list:
    return canonical_technicals._rma(values, length)


# v11.7 (Patch F.2): delegated to canonical_technicals.
def _compute_adx(highs: list, lows: list, closes: list, length: int = 14) -> float:
    return canonical_technicals.adx(highs, lows, closes, length)
```

Each replaces the existing function body. Function signatures and return type annotations stay untouched.

**MACD constants — delete in F.2 (per Brad's polish item #1).** Pre-F.2 verification confirms `MACD_FAST`, `MACD_SLOW`, and `MACD_SIGNAL` (active_scanner.py:82-84) are referenced only inside `_compute_macd`'s body (lines 126-134) and are not imported externally:

- `grep -nE "MACD_FAST|MACD_SLOW|MACD_SIGNAL" active_scanner.py` returns only their definitions and uses inside `_compute_macd`.
- `grep -rnE "from active_scanner import.*MACD_|active_scanner\.MACD_" --include="*.py"` returns no matches.

Once `_compute_macd`'s body is replaced with the delegation, the three constants become dead code. Delete them in the same F.2 commit. Replace lines 82-84 with a single one-line breadcrumb so future readers can grep their way to canonical_technicals:

```python
# MACD_FAST/SLOW/SIGNAL moved to canonical_technicals.MACD_FAST/SLOW/SIGNAL (Patch F).
```

Surrounding constants in the same block (`EMA_FAST`, `EMA_SLOW`, `RSI_PERIOD`, `WT_CHANNEL`, `WT_AVERAGE`) are still used by `_analyze_ticker` (336/337/348) and `_compute_wavetrend` (152/167) — they stay.

The `_compute_wavetrend` function (lines 149-184) stays native — it now calls the delegated `_compute_ema`, which routes to canonical_technicals._ema. Same math, no Wavetrend behavior change.

**Add the import.** Locate the existing import block near the top of `active_scanner.py` (lines 38-52, ending with `from ticker_rules import (...)`). Append a blank line followed by:

```python
import canonical_technicals  # v11.7 (Patch F.2): canonical home for RSI/MACD/EMA/ADX.
```

The import lands after the existing block and before the `log = logging.getLogger(__name__)` line. Existing imports (`from typing import Dict, List, Callable, Optional`, etc.) stay; the redirect doesn't drop or add typing requirements.

**Function ordering and docstrings.** The five function definitions stay in their current source positions (`_compute_ema` at ~99, `_compute_rsi` at ~109, `_compute_macd` at ~125, `_rma` at ~195, `_compute_adx` at ~213). Existing docstrings on those functions are removed when the body becomes a one-liner — the canonical_technicals docstrings remain the source of truth for the math. The unchanged `_compute_wavetrend` (lines 149-184) keeps its docstring; its internal call to `_compute_ema` now routes through canonical_technicals.

**Verification (per Brad's refinement #3):**
1. `python test_active_scanner_technicals_delegate.py` — must pass all 6 tests including the `_analyze_ticker` smoke test, confirming the shim chain composes correctly.
2. `python test_canonical_technicals.py` — still passes (tautologically — wrapper-consistency tests now compare canonical_technicals to itself, but they pass).
3. `python test_raw_inputs.py / test_canonical_gamma_flip.py / test_canonical_iv_state.py / test_canonical_exposures.py / test_canonical_expiration.py / test_bot_state.py / test_bot_state_producer.py / test_research_data_consumer.py` — no regression in any sibling canonical or producer/consumer suite (per Brad's polish item #4).
4. AST-check: `python -c "import ast; ast.parse(open('active_scanner.py').read())"` passes.
5. Importability check: `python -c "import active_scanner; print(active_scanner._compute_rsi([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114]))"` produces a number (not an exception).

**Acceptance:** all five verifications green. F.1's smoke test still passes. No regression.

### F.3 — CLAUDE.md update

**File:** `CLAUDE.md`

Three small edits:

1. **Repo layout note for active_scanner** (per Brad's refinement #2). The existing CLAUDE.md "Trading engine (production, ~11k lines)" block lists `app.py`, `dashboard.py`, then a multi-name bullet ending in "etc." — `active_scanner.py` is not currently called out by name. Add a new bullet immediately after the multi-name "etc." line. Exact wording:

   > `active_scanner.py` — main intraday scanner (`_analyze_ticker`, ActiveScanner class). Technical-indicator helpers delegate to canonical_technicals as of Patch F.

2. **What's done as of last session.** Move "Patch F" out of the queued list and add it to the done section. Bump the section header from `(v11.7 / Patch E)` to `(v11.7 / Patch F)`. Add bullet:

   > Patch F (active_scanner technicals redirect) — `_compute_rsi/_compute_macd/_compute_ema/_compute_adx/_rma` in active_scanner.py are now thin delegation wrappers around `canonical_technicals.*`. Zero behavior change verified by `test_active_scanner_technicals_delegate.py` (6 tests including an `_analyze_ticker` end-to-end sanity check). Backtest imports keep working unchanged through the existing names. canonical_technicals is no longer library-with-no-readers — active_scanner is its first production consumer.

3. **What's queued.** Remove the existing Patch F bullet (now done). Add follow-up notes:

   > - Patch G (or later): risk_manager ADX migration. risk_manager._compute_adx is SMA-seeded Wilder ADX, drifts from active_scanner's RMA-seeded variant (now canonical). Migration shifts ADX values, may shift regime classifier on borderline inputs. Plan: capture the actual numerical drift on real SPY data first (separate one-off script), include the drift in the commit message, gate behind env var if shift is meaningful.
   > - Later patch: RSI consolidation across `app.py:_rsi`, `unified_models.py:_rsi` (Wilder-smoothed, different from canonical), `swing_scanner.py:_rsi` (Wilder-smoothed AND list-returning, different shape), `income_scanner.compute_rsi` (close to canonical, rounds to 1 decimal). Needs design pass — Wilder-smoothed canonical, OR migration with documented drift, OR list-returning canonical for swing_scanner. Multiple sub-patches when it lands.
   > - Patch E.5 (or later): canonical_vwap — unchanged from prior CLAUDE.md note.
   > - Eventual cleanup: delete the `_compute_*` shims in active_scanner once nothing imports them. Requires confirming no external caller references the legacy names.

**Acceptance:** `git diff CLAUDE.md` shows only the three planned edits. Surgical — no other sections touched.

## 5. Test plan summary

| Step | Command | Expected |
|---|---|---|
| F.1 acceptance | `python test_active_scanner_technicals_delegate.py` | All 6 tests pass against native implementations |
| F.2 verification 1 | `python test_active_scanner_technicals_delegate.py` | Same 6 tests still pass against delegations |
| F.2 verification 2 | `python test_canonical_technicals.py` | All 21 tests pass (now tautological for wrapper-consistency tests) |
| F.2 verification 3 | All 8 sibling canonical / producer / consumer suites | All pass — no regression |
| F.2 verification 4 | AST-check active_scanner.py | Silent success |
| F.2 verification 5 | Importability smoke | `_compute_rsi(...)` returns a number, not exception |
| F.3 acceptance | `git diff CLAUDE.md` | Only the three planned edits visible |

## 6. Risk and rollback

**Behavior change risk: low (zero expected).** The delegation calls byte-identical math through a different name. Patch E's wrapper-consistency tests proved byte-identicalness for E.1/E.2/E.3. F.1's smoke test confirms post-redirect behavior matches.

**Production blast radius: low.** Affects exactly one production code path — `active_scanner._analyze_ticker` running every 2-15 minutes per tier. No new env vars, no new dependencies, no schema changes. Render auto-rebuild is safe.

**Rollback:** revert F.2's commit. F.1 (smoke test) and F.3 (docs) are individually revertable; reverting F.2 alone restores native math implementations.

**Pre-existing Windows wrinkle:** `active_scanner` imports `market_clock`, which uses `zoneinfo` and may need the `tzdata` package on Windows. Existing repo dependency, not introduced by Patch F. The smoke test imports `active_scanner` directly, so on Windows the test runs cleanly only if `tzdata` is installed (already true in Brad's environment per Patch E.1's spec review).

## 7. Acceptance criteria

After Patch F ships:

1. `active_scanner._compute_rsi/_compute_macd/_compute_ema/_compute_adx/_rma` exist as thin one-line delegations to `canonical_technicals.*`.
2. `_compute_wavetrend` is unchanged but routes through canonical via the delegated `_compute_ema`.
3. `test_active_scanner_technicals_delegate.py` exists with 6 tests, all passing.
4. All existing tests pass — `test_canonical_technicals.py` (21 tests, now partly tautological), `test_raw_inputs.py`, `test_canonical_gamma_flip.py`, `test_canonical_iv_state.py`, `test_canonical_exposures.py`, `test_canonical_expiration.py`, `test_bot_state.py`, `test_bot_state_producer.py`, `test_research_data_consumer.py`.
5. CLAUDE.md updated: active_scanner repo-layout note clarified, Patch F moved to "done" section, Patch G (risk_manager) and the broader RSI consolidation queued explicitly.
6. Three commits, each with `Patch F.N:` prefix on the message; each AST-checked; nothing else modified.

## 8. Out of scope

- **risk_manager._compute_adx migration** — separate patch; needs side-by-side drift measurement first.
- **app.py / unified_models.py / swing_scanner.py / income_scanner.py RSI sites** — separate patch family; needs Wilder-smoothed canonical design pass.
- **Deleting active_scanner._compute_* shims** — separate cleanup patch; requires confirming no external caller depends on the legacy names.
- **Updating backtest/* imports to point at canonical_technicals directly** — backtest files keep importing from active_scanner; the shims forward correctly. Migration is a future cleanup, not Patch F.
- **canonical_vwap** — unchanged from the Patch E note; still needs its own design pass.
- **`_compute_wavetrend` lift** — Patch E intentionally skipped this (it's session-stateless but more complex than RSI/MACD/ADX, and there's no production caller outside active_scanner to demand canonicalization). Stays native in active_scanner; uses the now-delegated `_compute_ema`.

---

**End of design spec. Ready to plan.**
