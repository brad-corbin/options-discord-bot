# DEPLOY — v9 Patch 6 (IV skew threshold recalibration)

**One sentence:** Adds `STRICT_SKEW_PP=1` env gate to switch
`oi_flow.compute_iv_skew()` from geometric ratio thresholds to percentage-
point thresholds matching `options_skew.SKEW_*_PCT` constants. Default off.

## Files

| File | Change |
|---|---|
| `oi_flow.py` | Patch 6 — adds `_STRICT_SKEW_PP` gate, `_SKEW_*_PP` constants, `skew_pp` field. Threshold logic branches on the gate. |
| `test_walk1d_patch6_iv_skew.py` | New — 53 acceptance tests, all passing. |

No other files modified.

## What it does

`compute_iv_skew()` previously called skew "extreme" using a geometric
threshold (`skew_ratio > 1.5`). At low absolute IV (compressed VIX
regimes) this fired on routine 5pp differences — false-positive "STEEP
PUT SKEW" lines on conviction alerts. The rest of the system
(`options_skew.compute_skew()`) uses percentage-point thresholds and would
not flag those cases.

Patch 6 makes the threshold match. Behavior with `STRICT_SKEW_PP=0`
(default) is identical to pre-Patch-6. With `STRICT_SKEW_PP=1`, the
"extreme" / "put_heavy" / "call_heavy" labels follow the pp-based
constants from `options_skew.py`.

The new field `skew_pp` is **always** included in the returned dict
regardless of mode (forward-compat). No reader is broken by either mode.

## Threshold table

| Label | Legacy (default) | Strict (`STRICT_SKEW_PP=1`) |
|---|---|---|
| `skew_extreme = True` | `skew_ratio > 1.5` | `skew_pp > 6.0` |
| `skew_direction = "put_heavy"` | `skew_ratio > 1.2` | `skew_pp > 3.0` |
| `skew_direction = "call_heavy"` | `skew_ratio < 0.8` | `skew_pp < -0.5` |
| `skew_direction = "neutral"` | otherwise | otherwise |

## Deploy steps

1. Replace `oi_flow.py` with the new version. Confirm AST parses:

   ```
   python3 -c "import ast; ast.parse(open('oi_flow.py').read())"
   ```

   Expected output: nothing (silent success).

2. Drop `test_walk1d_patch6_iv_skew.py` into the repo root. Run it:

   ```
   python3 test_walk1d_patch6_iv_skew.py
   ```

   Expected: `RESULT: 53 passed, 0 failed`. The script exits 0 on full
   pass.

3. Commit and push. Mark with the `v9 (Patch 6)` marker — already
   embedded in the file at four sites: the section header (line ~430),
   the `skew_pp` computation comment, the `_STRICT_SKEW_PP` branch, and
   the return field.

4. **Do not set `STRICT_SKEW_PP=1` on initial deploy.** Ship the patch
   in default-off mode. The new `skew_pp` field will start showing up in
   conviction alerts' `play["iv_skew"]` payload immediately, but the
   `skew_extreme` and `skew_direction` flags retain legacy behavior.

5. After at least one trading session of clean logs, decide whether to
   flip the gate.

## How to flip the gate

Set in Render env:

```
STRICT_SKEW_PP=1
```

Restart the service. `compute_iv_skew()` reads the gate at module
import — runtime changes do not take effect without restart.

## Rollback

Two paths:

- **Soft rollback:** unset `STRICT_SKEW_PP` (or set `=0`) in Render env,
  restart. Behavior reverts to legacy. No code change needed.
- **Hard rollback:** `git revert` the Patch 6 commit. The function
  reverts to its pre-Patch-6 form. The `skew_pp` field disappears from
  the output dict. No reader currently consumes `skew_pp`, so this is
  safe.

## Failure modes to watch

- **AST/import errors at startup** — file is invalid. Hard rollback.
- **Conviction alerts losing the "STEEP PUT SKEW" line entirely** —
  unexpected; either gate is set with bad data, or upstream IV column
  is empty. Check `compute_iv_skew` logs and confirm chain pulls are
  populating `iv` / `delta` columns.
- **Conviction alerts firing "STEEP PUT SKEW" on cases that don't look
  steep** — this is the bug Patch 6 fixes. If gate is `=1` and this
  still happens, check `_STRICT_SKEW_PP` was actually picked up
  (`grep _STRICT_SKEW_PP` in startup logs if any).

## Why no env gate witness was required

Project rule says behavior changes Patch 4+ get gated and pre-deployed
with the gate disabled, then enabled later. That rule is applied here:
default off, operator flips when ready. No `→ extreme` log witness is
required prior to enabling because the divergence is mathematical, not
state-based — every `compute_iv_skew()` call now also returns `skew_pp`,
and the tests verify the divergence table from `WALK1D_FINDINGS.md`
explicitly.

## What this patch does NOT do

- Does **not** delete or rename `compute_iv_skew()`.
- Does **not** modify the writer at line ~996 or the reader at line ~2621.
  Those continue to round-trip the same Redis key with the same TTL.
- Does **not** modify the conviction-play formatter at line ~2969. It
  still reads `skew["skew_extreme"]` and `skew["skew_direction"]` —
  those fields still exist in both modes.
- Does **not** touch `options_skew.py`. That module remains the
  source-of-truth for the constants; if its values change, the mirrored
  `_SKEW_*_PP` constants in `oi_flow.py` must be updated to match.
- Does **not** consolidate the two skew systems. That's a v8.3+
  consideration (Option B in `WALK1D_FINDINGS.md`).

## After deploy — Walk 1D status

- Track A (gex bundle 4c/4a/4b/5): closed.
- Track C (Walk 1D — iv_skew): **closed with Patch 6**.
- Track B (Walk 1C — `regime_detector` audit): open. Recommended next
  chat. Files: `app.py` + `regime_detector.py`. See Walk 1B Findings §4.
