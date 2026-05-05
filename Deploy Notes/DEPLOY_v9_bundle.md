# DEPLOY — v9 bundle (Patches 1+1.1, 2 rev1, 3)

**Status:** built, AST-clean, tested. Ready to ship.
**Strategy:** Option B strict — all four patches deploy together in one push.
**Blast radius:** additive only. No reader migrated. No existing behavior changed.

```
v9 Patch 3: approved
Scope: thesis_monitor.py only
Risk: low
Behavior change: none intended
Main validation: dealer_regime mirrors existing gex_sign override
```

**Post-deploy validation:** run the Redis invariant check from the Walk 1B doc for a few tickers after the next silent thesis run. Any mismatch between `gex_sign` and the `dealer_regime` mapping (positive ↔ pin_range, negative ↔ trend_expansion) should stop Patch 4.

**Known non-blocker:** Patch 3 does not add the missing symmetric above-flip log line — the override still prints only when the below-flip branch fires, not when the above-flip branch sets `gex_sign = "positive"`. That asymmetry is deferred to Patch 4 per the Walk 1B rev1 plan.

---

## Files in this drop

| Patch     | File                  | What it does                                                                                          |
| --------- | --------------------- | ----------------------------------------------------------------------------------------------------- |
| 1 + 1.1   | `app_bias.py`         | (built earlier — see prior session output)                                                            |
| 2a + 2b   | `oi_flow.py`, `persistent_state.py` | (built earlier — see prior session output)                                                |
| **3**     | `thesis_monitor.py`   | Adds three additive fields to `ThesisContext`: `gex_value_sign`, `flip_location`, `dealer_regime`.    |

This runbook covers Patch 3 specifically. Combine with the prior runbooks for 1/1.1 and 2 to assemble the full deploy.

---

## Patch 3 — what changed

Four edit sites, all in `thesis_monitor.py`:

1. **`ThesisContext` dataclass** — three new fields with safe defaults (`"neutral"`, `"unknown"`, `"unknown"`).
2. **`build_thesis_from_em_card`** (around the GEX override block) — populates the three fields. Computes `dealer_regime` by recomputing the same override-distance value used by the existing wrapper override, preserving lockstep with legacy `gex_sign`. This intentionally mirrors the current production formula instead of introducing a new regime calculation.
3. **`_persist_thesis`** — three new keys persisted to Redis under the existing `thesis_monitor:{ticker}` key.
4. **`_load_thesis_from_store`** — three new fields hydrated with safe defaults so legacy Redis blobs still load cleanly.

The existing silent default `gex_sign=d.get("gex_sign", "positive")` on the load path is **untouched**. That removal is Patch 4.

Grep marker: `# v9 (Patch 3):`

---

## Why "additive only" matters

No downstream consumer reads `gex_value_sign`, `flip_location`, or `dealer_regime` yet. They exist purely so future patches can migrate consumers off `gex_sign` one at a time. Until Patch 4+:

- `gex_sign` keeps its current meaning and behavior (literal sign + override).
- The new fields run in parallel and agree with `gex_sign` for every ticker that hits the override.
- If we have to pull this patch, no consumer breaks — they were never reading the new fields.

---

## Test results

Ran `test_patch3.py` against the modified file. **27/27 checks passed.**

| # | Test                                                       | Result |
| - | ---------------------------------------------------------- | ------ |
| 1 | AST clean                                                  | PASS   |
| 2 | AAPL morning case (gex −94.54M, spot above flip)           | 4/4    |
| 3 | SOXX case (gex +1.9M, spot below flip)                     | 4/4    |
| 4 | Edge case: no `flip_price` provided                        | 4/4    |
| 5 | Persist/load roundtrip preserves all three fields          | 4/4    |
| 5b | Legacy Redis blob (no new keys) hydrates to defaults      | 4/4    |
| 6 | `at_flip` band straddle (±0.25 %)                          | 2/2    |
| 7 | `gex_value == 0` → `gex_value_sign = "neutral"`            | 2/2    |
| 8 | Within ±1.5 % band, negative gex → `trend_expansion`       | 2/2    |

The lockstep invariant — `dealer_regime` agreeing with `gex_sign` on every override case — held across Tests 2, 3, 7, and 8.

---

## Deploy steps

1. **Pre-flight, off-hours.** This is a Render redeploy. Don't do it during market hours.
2. **Stage the file.** Copy `thesis_monitor.py` from this drop into your repo. Commit alongside the Patch 1/1.1 and Patch 2 files from the prior drops.
3. **Quick local check before push:**
   ```bash
   python3 -c "import ast; ast.parse(open('thesis_monitor.py').read()); print('AST clean')"
   ```
4. **Push to Render.** Watch the build log; this patch adds no new dependencies and no env vars.
5. **First-tick smoke test.** After redeploy, on the next em-card build, look for:
   - No new errors in the thesis-monitor logger.
   - Existing `Thesis GEX overridden:` log lines should still print exactly as before — the override path is unchanged.
6. **Optional spot check.** If you want to confirm the new fields are populating on the live bot, you can read `thesis_monitor:AAPL` (or any monitored ticker) from Redis after the next em card. The blob should contain `gex_value_sign`, `flip_location`, and `dealer_regime` keys.

---

## Rollback

If anything looks wrong:

1. Revert `thesis_monitor.py` to the prior version. No env var to flip — the patch has no on/off toggle because there's no behavior change to gate.
2. Redeploy. Existing Redis blobs with the new keys are forward-compatible with the old code (the old loader simply ignores them).

Time to rollback: one redeploy cycle. No data migration needed.

---

## What's deferred

These were all flagged in the handoff as Patch 4+ work and should **not** ship with this drop:

- Killing the silent `gex_sign=d.get("gex_sign", "positive")` default on the load path.
- Migrating any reader from `gex_sign` to `dealer_regime`.
- `regime_detector.update(gex_sign=…)` at `app.py:14045` — convention audit (Walk 1C).
- `iv_skew:{ticker}` writer/reader pair in `oi_flow.py` (Walk 1D).
- Consumer audit on `should_use_long_option`'s callers.
