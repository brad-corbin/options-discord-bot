# DEPLOY — v9 Patch 8 — IV-aware sweep band widening

**File changed:** `options_exposure.py`
**Three edits:** `_dg` (line 773), `_grid` (line 1065), caller plumbing (line 1124)
**Header marker:** added at top of file
**Env gates:** none — restoration of intended behavior, not a new feature

---

## What this patch does

Widens the gamma-flip / vanna-flip sweep band so the engine can find flips for high-IV tickers. The hardcoded ±10% (`_dg`) and ±12% (`_grid`) bands were credit-era artifacts that worked for low-vol names but missed flips for high-IV names where the flip sits 15–25% from spot.

`_grid` (production thesis path, used by `InstitutionalExpectationEngine.snapshot()`) becomes IV-aware: `pct = max(0.15, min(0.40, 3.0 * iv * sqrt(dte_years)))`. The same grid object is reused for both `gamma_flip` and `vanna_flip` at the call site, so widening flows to both — intentional.

`_dg` (ExposureEngine default for callers without IV in scope — DecaySimulator, public entry at line ~1179) widens to a ±25% blanket. ExposureEngine has no UnifiedIVSurface natively, so IV-aware here would require larger plumbing changes. Blanket ±25% is the consistent default — matches `_grid`'s no-IV fallthrough.

---

## Pre-deploy verification (all run, all clean)

```
$ python3 -c "import ast; ast.parse(open('options_exposure.py').read()); print('AST OK')"
AST OK

$ python3 verify_empirical.py     # BEFORE the edits
BEFORE Patch 8  —  hardcoded pct=0.12 (current production)
  ticker     spot      iv      flip       lo       hi    pct  covers?
  LLY      988.40   0.395    935.99   869.79  1107.01  0.120  YES
  MRNA      46.65   0.792     41.03    41.05    52.25  0.120  NO  <-- flip outside band
  AMD      414.00   1.279    335.51   364.32   463.68  0.120  NO  <-- flip outside band
  ARM      226.22   1.633    191.99   199.07   253.37  0.120  NO  <-- flip outside band

$ python3 verify_after.py         # AFTER the edits
AFTER Patch 8  —  _grid IV-aware (the production thesis path)
  ticker     spot      iv      flip       lo       hi    pct  covers?
  LLY      988.40   0.395    935.99   840.14  1136.66  0.150  YES
  MRNA      46.65   0.792     41.03    36.78    56.52  0.212  YES
  AMD      414.00   1.279    335.51   272.48   555.52  0.342  YES
  ARM      226.22   1.633    191.99   135.73   316.71  0.400  YES

$ python3 test_patch8_iv_band.py
v9 Patch 8 — IV-aware sweep band  —  unit tests
  test_mrna_iv_aware_covers_flip: PASS  (pct=0.2116, band=[36.78, 56.52], flip=41.03)
  test_lly_clamps_to_floor: PASS  (pct=0.1500)
  test_arm_clamps_to_ceiling: PASS  (pct=0.4000)
  test_no_iv_fallthrough: PASS  (pct=0.2500)
  test_dg_widened_to_quarter: PASS  (pct=0.2500, points=201)
  test_explicit_pct_overrides_iv_path: PASS  (pct=0.1000)
All tests passed.
```

Three of four problem tickers had flips OUTSIDE the production ±12% band; all four are inside the new IV-aware band. Patch 8 cleared on the calculation side.

---

## Deploy

1. Commit `options_exposure.py` to main.
2. Push to Render.
3. Wait for build + restart.
4. Run Gate 1 below, ~5 minutes after the restart confirms healthy.

---

## Post-deploy verification

### Gate 1 — Engine output  (5 min after deploy, blocking)

Force-run `/em MRNA AMD ARM`, then:

```bash
for t in MRNA AMD ARM LLY; do
    echo "=== $t ==="
    redis-cli -u "$REDIS_URL" --raw GET "thesis_monitor:$t" | python3 -c '
import json, sys
t = json.load(sys.stdin)
print("  gamma_flip:   ", t["levels"].get("gamma_flip"))
print("  flip_location:", t.get("flip_location"))
print("  dealer_regime:", t.get("dealer_regime"))
'
done
```

**Expected:**
- MRNA: `gamma_flip ≈ 41.03`, `flip_location=below_flip`, `dealer_regime=trend_expansion`
- AMD:  `gamma_flip ≈ 335.51`, `flip_location=below_flip`, `dealer_regime=trend_expansion`
- ARM:  `gamma_flip ≈ 191.99`, `flip_location=below_flip`, `dealer_regime=trend_expansion`
- LLY:  unchanged (already populated, `gamma_flip=935.99`, `dealer_regime=pin_range`)

If any of MRNA/AMD/ARM stay null → **patch failed**, roll back.

### Gate 2 — Trade vehicle change  (next `/em` or silent cycle, non-blocking)

The point of populating `dealer_regime` is downstream: vehicle selection (long calls vs call debit spreads) gates on it. Verify the actual trade-setup output flipped:

```bash
grep -E "Trade setup built: (MRNA|AMD|ARM)" /opt/render/project/src/bot_logs/*.log | tail -20
# or whichever log path your runtime writes to — adjust as needed
```

**Expected:**
- Pre-Patch-8: setups labeled `CALL DEBIT SPREAD` / `PUT DEBIT SPREAD` (conservative default when `dealer_regime=unknown`)
- Post-Patch-8: same tickers labeled `NAKED CALL` / `NAKED PUT` (directional path when `dealer_regime=trend_expansion`)

**Gate 2 is non-blocking for declaring Patch 8 successful.** If Gate 1 passes but Gate 2 doesn't change vehicle selection, the issue is downstream of Patch 8 — most likely the thesis storage hasn't refreshed or the vehicle-selection readers in `thesis_monitor.py` aren't being hit. Don't roll back. Investigate separately. Patch 8 did its job.

---

## Rollback

If Gate 1 fails (any of MRNA/AMD/ARM stay `null` for `gamma_flip` after a clean force-run):

```bash
git revert <commit_sha_for_patch8>
git push                            # triggers Render redeploy
```

Then on Render, force-run `/em MRNA` and confirm Redis returns to the pre-Patch-8 null state. Open a finding doc with the failing Redis output for the next audit cycle.

---

## Files in this patch directory

- `options_exposure.py` — patched (1268 lines, was 1241; +27 = header marker + 3 edit-site comments)
- `verify_empirical.py` — runs against unpatched `_grid` (BEFORE pass)
- `verify_after.py` — runs against patched `_grid` and `_dg` (AFTER pass)
- `test_patch8_iv_band.py` — six unit tests, all passing
- `DEPLOY_v9_patch8.md` — this file

## Audit threads parked (do NOT bundle with Patch 8)

1. **GEX sign-convention audit** — bot uses `gs = -1 if call else 1` in `_exposures`, inverted from standard SqueezeMetrics convention. May be intentional with matching downstream interpretation, may be a long-standing flip compensated for by `STRICT_GEX_SIGN` and the `Thesis GEX overridden` path. Resolve with `git blame` + grep.
2. **gamma_flip consumer audit** — `dealer_regime` is one consumer (vehicle selection); the full graph isn't enumerated. Resolve with `grep -rn "dealer_regime\|flip_location\|above_flip\|below_flip" --include="*.py" . | grep -v options_exposure.py`.
3. **GEXBoard high-leverage usage features** — approaching-flip alert, first-touch event, reclaim event, failed-reclaim short setup, strategy filter on entry. Each independently scopable.
