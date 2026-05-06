# Patch 9 — Dealer-Side Convention Flip (Walk 1E resolution)

## Summary

Flips the dealer-side sign convention in `_exposures` from "dealer short calls / long puts" to "dealer long calls / short puts" (SqueezeMetrics convention, per "The Implied Order Book", SqueezeMetrics 2020). Removes downstream override workarounds and contradictory narration that were compensating for the inverted convention. Also fixes a pre-existing bug in the `inferred_side` flip condition that becomes visible post-flip.

## Why

Five independent lines of evidence converged:

1. **Empirical test on em_reconciliation data**: SPY/QQQ near-flip zones showed Mann-Whitney p=0.030 that above-flip realizes ~28% smaller moves than below-flip. Geometric layer (above flip = pin) is empirically right.
2. **Black-Scholes math audit**: All per-option Greek formulas in `options_exposure.py:253-305` match canonical Macroption / Merton formulas exactly. The math is right; only the dealer-side aggregation sign was wrong.
3. **Internal inconsistency**: Three override sites (`thesis_monitor.py:4426-4427`, `app.py:12821-12826`) and a contradictory bias-card branch (`app_bias.py:94`) were all force-correcting raw → geometric. Geometric was the de-facto authoritative layer.
4. **User instinct**: Brad's read — "levels held but likelihoods seemed off" — exactly matches "physical-direction logic correct, sign-of-input wrong."
5. **Canonical source**: SqueezeMetrics 2020 paper (p1, p4, p8) explicitly defines the convention. Codebase defaults were exactly opposite.

## What changed (4 files, 4 sites)

### 1. `options_exposure.py:736-752` — source flip + flip-condition fix

**Before:**
```python
ds=-1 if ot=="call" else 1;gs=-1 if ot=="call" else 1
if d["inferred_side"]==TradeSide.SELL and conf>0.65: ds=-ds;gs=-gs
```

**After:**
```python
ds=1 if ot=="call" else -1;gs=1 if ot=="call" else -1
if conf>0.65 and (
    (ot=="call" and d["inferred_side"]==TradeSide.BUY) or
    (ot=="put"  and d["inferred_side"]==TradeSide.SELL)
):
    ds=-ds;gs=-gs
```

Two changes in one block:
- **Defaults flip**: `ds=-1 if call else 1` → `ds=1 if call else -1` (and same for `gs`). This is the convention flip.
- **Flip condition becomes asymmetric**: was `inferred_side==SELL` for both call and put; now `BUY` for calls and `SELL` for puts. This was a pre-existing bug — the symmetric flip only worked correctly for ONE of the two option types under any given convention. Left unfixed, the patch would silently invert the call-side flow handling.

### 2. `thesis_monitor.py:4421-4429` — override block removed

The block that forced `gex_sign` to match geometric whenever `|spot-flip|>1.5%` is deleted. Post-flip, raw and geometric agree by construction. Comment left in place documenting the removal.

### 3. `app.py:12810-12819` — override block removed

Same treatment as `thesis_monitor.py`. The `gex_positive` override block is deleted. `gex_positive = tgex >= 0` now reflects raw sign directly, which matches geometric by construction.

### 4. `app_bias.py:91-97` — contradictory narration simplified

The `tgex > 0 ? "range-bound bias" : "above flip but still negative GEX — trending likely"` split is replaced with unconditional `"range-bound bias"`. Above flip will reliably produce positive raw GEX post-flip; the contradiction-papering branch becomes unreachable in normal cases.

## What is NOT changed (and why)

- **Per-option Greek formulas** (`gbs_delta`, `gbs_gamma`, `gbs_vanna`, `gbs_charm`, etc.) — already canonical Black-Scholes/Merton, verified line-by-line against Macroption.
- **CAGF formula in `institutional_flow.py`** — physical-direction comments ("dealers short delta = bullish pressure") map correctly-signed inputs to correct directions. Patch only fixes the inputs; formula was always right.
- **Geometric classifier in `app.py:11778-11781` and `thesis_monitor.py:4461-4469`** — already encodes SqueezeMetrics convention. Becomes consistent with raw signal post-flip.
- **The 37 `gex_sign == "positive"/"negative"` readers** — read the post-flip string label, which is identical pre/post-patch when the override fired (which was always in the >1.5% zones). Patch 10 reader migration is independent and can proceed on its own timeline.
- **v2_edge / v8.4 income plays** — confirmed in audit to consume zero dealer-flow Greeks. Insulated.

## Schema cutover

Patch 9 creates a new sign-convention boundary in stored `em_predictions`, `em_reconciliation`, and Redis-stored thesis context. Same kind of cutover as the 2026-05-05 dashboard cutover, just at a different boundary.

**Pre-Patch-9 stored rows**: `gex_value`, `dex_value`, `vanna_value`, `charm_value` carry inverted signs.
**Post-Patch-9 stored rows**: same fields carry SqueezeMetrics-aligned signs.

**Recommended migration steps at deploy**:
1. Flush stale convention-1 thesis blobs from Redis. The live thesis writer is `thesis_monitor:{ticker}` (NOT `thesis:{ticker}` — that's the forward-compat dead path per `persistent_state.py:692`). Targets:
   - `thesis_monitor:*` — per-ticker thesis blobs (carry `gex_value`, `dex_value`, `vanna_value`, `charm_value` under old convention)
   - `thesis_monitor:tickers` — the ticker list (so the rebuild is clean)
   - `gex:*` — per-ticker GEX cache from `persistent_state.py:643/664/761`
   
   Do NOT flush all of Redis — that would clear queues and other state. Be specific to the prefixes above.
2. The first `/em` cycle after deploy will repopulate `thesis_monitor:*` and `gex:*` with convention-2 data. Mixed-convention reads are bounded to that ~5-10 minute window before fresh data overwrites.
3. `convention_version=2` is added by the patch to the em_predictions entry dict and CSV fieldnames (`app.py:11754`). Pre-Patch-9 rows have no `convention_version` field (NULL/missing). Analytical code joining pre/post-cutover rows MUST filter or branch on this column.
4. Do not auto-flip historical em_predictions rows. Keep them under their original convention with implicit `convention_version=1` (NULL) tagging.

## Pre-deploy acceptance test

Run `python3 test_patch9.py` from the repo root before deploy. All 14 assertions must pass:

- AST parse on all 4 changed files
- 6 per-row sign cases on `ExposureEngine` (UNKNOWN-flow defaults for call/put + the four high-confidence flow combinations)
- No remaining `Thesis GEX overridden` / `GEX sign overridden` log calls in `thesis_monitor.py` / `app.py`
- `convention_version` present in both the em_predictions entry dict and the CSV fieldnames

If the test fails, do not deploy — investigate the failing assertion first.

## Post-deploy verification gates

### Gate A — first /em cycle confirms the flip took effect (immediate)

Run the next scheduled `/em` cycle and verify in logs:
- For SPY/QQQ snapshots within ±1.5% of flip: `dealer_regime` field matches geometric label (`pin_range` if above flip, `trend_expansion` if below). This was the production-visible bug pre-patch.
- No `Thesis GEX overridden` or `GEX sign overridden` log lines fire (the override sites are deleted, but if any other override path fires it indicates a missed site).

### Gate B — sign distribution sanity (within first session)

Pull stored `gex_value` from the last 5 sessions and confirm distribution:
- Pre-patch: `gex_value` was negative on roughly 70-80% of snapshots (per "GEX rarely negative" framework, indicating inverted convention).
- Post-patch: `gex_value` should be positive on roughly 70-80% of snapshots, matching the SqueezeMetrics 2020 paper's empirical claim "GEX is very rarely negative" on indices.

If post-patch distribution is still mostly negative, the patch hasn't taken effect or there's another inversion site.

### Gate C — CAGF directional integrity (1-2 weeks)

Log CAGF outputs (score, direction, components) alongside realized session direction. Check whether the C/V/L portion of the score (composite minus G·W_GAMMA contribution) correlates positively with realized direction.

- If correlation is positive: convention flip cascaded correctly through CAGF formula. No further work.
- If correlation is negative: CAGF formula directions were tuned against pre-patch convention and need C/V/L sign flips. Ship a small follow-up patch.

Per the integrity check pre-deploy, hypothesis A (formula correct, inputs were wrong) is strongly favored. Gate C confirms or refutes.

### Gate D — flip-condition correctness on flow data (1-2 weeks)

Sample post-deploy snapshots where `dealer_sign_confidence > 0.65` for individual contracts. Confirm:
- Calls with high-conf BUY: per-row `gex` contribution is NEGATIVE (dealer short call).
- Calls with high-conf SELL: per-row `gex` contribution is POSITIVE (dealer long call, default).
- Puts with high-conf BUY: per-row `gex` contribution is NEGATIVE (dealer short put, default).
- Puts with high-conf SELL: per-row `gex` contribution is POSITIVE (dealer long put).

The asymmetric-flip fix was empirically verified on synthetic data pre-deploy (`/home/claude/flip_test.py` and `flip_test2.py`). Production verification confirms the fix works against real flow data, not just synthetic.

## Silenced Telegram routes — DO NOT auto-re-enable

Brad has silenced most non-(v2_edge, v8.4) Telegram routes because they were producing unreliable outputs. The convention bug was the upstream cause for many of those routes' unreliability. **The convention fix is necessary but not sufficient for re-enabling them.**

For each silenced route, before re-enabling:
1. Collect 2-3 weeks of post-patch data (em_predictions snapshots tagged with route source)
2. Compare predictions to outcomes (em_reconciliation join)
3. If accuracy meets threshold, route can be considered for re-enable.
4. If accuracy is still poor, the convention wasn't the only issue — investigate the route-specific logic.

High-confidence "should now work" candidates: CAGF-driven outputs, app_bias card narration, thesis-driven trade cards. These had well-formed logic operating on inverted inputs.

Lower-confidence candidates: anything that was producing weird outputs even when mentally inverting the sign while reading them. The convention was masking secondary issues there.

## Rollback

If post-deploy data shows the patch is producing worse outcomes than pre-patch (Gate A or B fails decisively), rollback is a **single-commit revert** (the patch touches 4 files but ships as one commit):

```bash
git revert <patch-9-commit>
```

The four edits are scoped to four files with no migrations or schema changes that can't be reversed by re-running. Stored data tagged `convention_version=2` would need to either be deleted or have its sign flipped on rollback — recommend deleting Redis keys (`thesis_monitor:*`, `gex:*`) and recomputing on next /em cycle to keep things clean. Pre-Patch-9 em_predictions rows (no `convention_version` field) are untouched by rollback.

## Files changed

- `options_exposure.py` (+19 / −2): source convention flip + flip-condition asymmetry fix
- `thesis_monitor.py` (+6 / −4): override block removal
- `app.py` (+6 / −15): override block removal
- `app_bias.py` (+4 / −2): contradictory narration simplified

Total: 4 files, 35 insertions, 23 deletions.

## What this patch does NOT close out

- **Patch 10 reader migration** (37 `gex_sign ==` readers → `_gex_branch(dealer_regime)`): independent, can proceed on its own timeline. Patch 9 makes Patch 10 cleaner because raw and override now agree by construction.
- **CAGF formula directional verification** (Gate C above): pending 1-2 weeks of post-deploy data.
- **Re-enabling silenced Telegram routes**: pending per-route validation, see "Silenced Telegram routes" above.
- **`unified_models.py:473` `gex_raw_negative` field**: field becomes meaningless post-patch (raw and override agree, so "raw" no longer distinguishes). Recommend deletion in a small follow-up; left in place to minimize blast radius of this patch.
