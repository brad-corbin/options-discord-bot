# DEPLOY — v9 (Patch 7.1) — Walk 1E continued

## What this is

Diagnostic-only enhancement to Patch 7. Adds an empirical band-width test
to the existing `flip_price=None` log block, plus three cosmetic
corrections to the original Patch 7 diagnostic.

**No engine behavior changes.** Trading logic is untouched. The only
runtime difference is what gets logged when the engine fails to find a
gamma flip — and that block already only fires when `flip_price is
None` (rare event post-Patch-7).

## Why

Patch 7 successfully widened the chain (verified — MRNA went from ~40
to 112 contracts). But MRNA's `flip_price` is still null post-Patch-7.
The Patch 7 diagnostic claimed `covers_band=True`, which suggested
Path B (real market state) — but the diagnostic was *lying* in three
ways:

1. It logged `sweep_band=[spot*0.90, spot*1.10]` (±10%). Production
   actually uses ±12% via `InstitutionalExpectationEngine._grid`
   (line 1065 in `options_exposure.py`). The diagnostic reported the
   wrong band.
2. It logged `sign=unknown` because it looked for `engine_result.gex_sign`
   — that key doesn't exist. The engine_result has `is_positive_gex`
   (bool). Cosmetic but misleading.
3. It logged `rows=112` (raw chain count) but didn't show how many of
   those survived `build_option_rows` IV filtering — which is what the
   engine actually saw.

Patch 7.1 corrects all three and adds the empirical test the data
demanded: when a flip isn't found in production's ±12% band, the
diagnostic now also runs `gamma_flip()` with a ±25% band on the same
chain. If it finds a flip there, the production grid is the bottleneck
(Patch 8 will widen it). If not, the bug is somewhere else and we audit
further.

## Files

Single file:
- `app.py` — replaces existing copy. AST clean.

## Deploy steps

1. Copy `app.py` from `v9_patch7_1/` to repo root (overwrites Patch 7's app.py)
2. Commit: `v9 (Patch 7.1): empirical band-width test + diag corrections`
3. Push, Render auto-deploys

## What to look for in logs

Existing log line (Patch 7) — now corrected:

```
flip_price=None [_get_0dte_iv] MRNA 2026-05-08:
  spot=46.65 rows=112 strike_range=[30.00,80.00]
  sweep_band=[41.05,52.25]  ← was [41.98,51.32], now correctly ±12%
  covers_band=True
  gex=-8.84 sign=negative   ← was sign=unknown, now correct
```

NEW second line (Patch 7.1):

```
  [Patch 7.1 diag] MRNA: flip_at_25pct_band=$54.20
    gex_curve_minmax=[-12.4,3.8] signs(+/-/0)=12/89/0 engine_rows=87
```

Three things this tells us:

| Field | What it tells us |
|---|---|
| `flip_at_25pct_band` | If a number → wider band finds the flip → Patch 8 = widen `_grid`. If `None` → flip is genuinely outside ±25% or there's a different bug. |
| `gex_curve_minmax` | Min/max net GEX across production ±12% band. If both signs present but no flip detected, suggests `_fz` issue. If both same sign, no flip exists in band. |
| `signs(+/-/0)` | Distribution of GEX sign across the 101 grid points. `0/101/0` = monotonically negative. `12/89/0` = mostly negative, some positive — flip should exist somewhere in band. |
| `engine_rows` | Post-filter row count (vs raw `rows=112`). Tells us if the IV filter dropped anything significant. |

## Decision branches after one log cycle

- **`flip_at_25pct_band` returns a number for MRNA + most of the 7
  problem tickers** → confirms band-width hypothesis. Write Patch 8 =
  widen `_grid` from ±12% to ±25% (or make IV-aware).
- **`flip_at_25pct_band` still None for MRNA but with mixed signs in
  curve** → bug in `_fz` zero-crossing detection. Different audit.
- **`flip_at_25pct_band` still None and curve is all-negative** → flip
  is genuinely outside ±25%. Either real market state for that ticker
  today, or band needs to go even wider. Try ±50% in next iteration.
- **`engine_rows` is much smaller than `rows`** → IV filter is dropping
  a lot. Different audit on `build_option_rows` filter.

## Cost

~123ms per failing-ticker call. Fires only when `flip_price is None`.
Bounded — even if all 35 tickers fail simultaneously (won't happen),
that's <5 seconds extra log work per silent thesis cycle. Wrapped in
try/except — can never crash trading.

## Rollback

`git revert <commit>` and redeploy. Reverts to Patch 7's diagnostic.
No data, no env vars, no migrations.

## What this is NOT

- Not a fix for the MRNA `flip_price=None` problem. That's Patch 8.
- Not a behavior change. Diagnostic only.
- Not a substitute for Walk 1F (the proper sweep-band audit).

This is the empirical confirmation step that comes between Patch 7's
chain widening and Patch 8's actual fix. After one good log cycle,
Patch 8 has data to design against instead of a hypothesis.
