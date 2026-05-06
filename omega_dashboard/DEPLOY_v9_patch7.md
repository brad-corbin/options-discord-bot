# DEPLOY — v9 (Patch 7) — Walk 1E

## What this is

Restores engine `gamma_flip` detection for high-vol tickers (AMD, ARM,
COIN, LLY, MRNA, MSTR, SMCI). Removes the v5.1.1 `strike_limit=20`
constraint that was a MarketData.app credit-saving optimization but is
dead under Schwab's per-call billing. Adds a diagnostic log so any
remaining `flip_price=None` cases are visible and self-explaining.

## Pre-flight

- [ ] Confirm Patch 6 is deployed and ran clean for at least one session
- [ ] Save MRNA baseline (`thesis_monitor:MRNA` Redis dump) somewhere
      you can compare against post-deploy. Already captured in chat
      history; copy it to a notes file if you want a durable record.

## Files

Both go to repo root, replacing existing copies:

- `app.py` — 6 strike_limit sites widened, 4 docstrings/comments updated,
  2 diagnostic log blocks added, 5 no-op sites annotated
- `api_cache.py` — `get_chain` docstring updated to reflect both
  MarketData and Schwab billing models

## Deploy steps

1. Copy `app.py` and `api_cache.py` from `v9_patch7/` to repo root
2. Commit with message: `v9 (Patch 7): Walk 1E — full chain + diagnostic log`
3. Push to main
4. Render auto-deploys on push (or trigger manually)
5. Watch deploy logs for AST/import errors

## What to watch for in production logs

After the next silent-thesis cycle (~9:30 AM CT for the 8:15-8:30 sweep,
or whenever `/em <ticker>` is run), expect:

**Success indicators:**

- All 35 EM_TICKERS plus FLOW_TICKERS produce thesis records
- The 7 problem tickers (AMD/ARM/COIN/LLY/MRNA/MSTR/SMCI) now show
  `flip_location` other than `unknown` for most cases
- `dealer_regime` populated as `pin_range` or `trend_expansion` instead
  of `unknown` for those same tickers

**Diagnostic warnings (expected to be rare; tells us what's left):**

```
flip_price=None [_get_0dte_iv] MRNA 2026-05-08:
  spot=46.00 rows=120 strike_range=[35.00,60.00]
  sweep_band=[41.40,50.60] covers_band=True
  gex=-7.92 sign=negative
```

This is the post-Patch-7 case where the chain is wide enough to span the
sweep band but the engine still can't find a flip — meaning the flip is
genuinely outside the engine's ±10% sweep. That's a real market state,
not a bug. We'd need a separate audit (engine sweep widening) to address
it. Patch 7 just makes this case observable and distinguishable from
"chain too narrow."

If you see this pattern with `covers_band=False`, the chain is still
truncated for some reason — check Schwab adapter / api_cache call paths.

## Verification — MRNA specifically

The falsifiable test for Patch 7 is MRNA's thesis post-deploy. Run after
the next silent thesis (or `/em MRNA` after deploy):

```bash
redis-cli -u "$REDIS_URL" --raw GET 'thesis_monitor:MRNA' | python3 -m json.tool | head -60
```

**Pre-Patch-7 baseline (captured 2026-05-05):**
- `gex_value: -7.92`
- `gamma_flip: null`
- `flip_location: "unknown"`
- `dealer_regime: "unknown"`

**Post-Patch-7 expectation, two paths:**

- **Path A (chain widening fixes it):** `gamma_flip` is non-null,
  `flip_location` is `above`/`below`/`at`, `dealer_regime` is
  `pin_range` or `trend_expansion`. → Patch 7 worked, root cause was
  chain truncation.
- **Path B (real market state):** `gamma_flip` still null, log shows
  `covers_band=True` and `gex=` is consistently same-signed across
  values. → Patch 7 widened correctly but the flip is outside ±10% for
  this ticker today. Look at next session — flip location moves with
  market.

Either outcome is informative. Path B is not a Patch 7 failure; it's
the diagnostic log doing its job.

## Rollback

Simple: `git revert <commit>` and redeploy. No env vars to unset, no
data migrations. The chain widening uses standard Schwab API parameters
that worked before; reverting just restores the truncation. The
diagnostic logs disappear on revert. The marker comments on no-op sites
disappear (cosmetic only).

## What's NOT in Patch 7

- The 5 `_derive_structure_levels_from_chain({}, ...)` no-op sites are
  **annotated only**, not changed behaviorally. Verified via unit test
  that they preserve the upstream-enriched walls. Not a bug.
- `swing_engine.SWING_MAX_EXPIRATIONS = 3` (also a v5.1.1 credit-era
  constraint, different cost model — separate audit candidate)
- `schwab_stream.STRIKES_PER_SIDE = 3` is a streaming-bandwidth
  constraint, NOT a credit constraint. Left untouched intentionally.

## Acceptance tests

`test_patch7.py` covers structural verification. 28/28 passing locally
before deploy. Run with `python3 test_patch7.py`.

## Project-rule reminder for next handoff

The Walk 1E discovery surfaced a principle worth carrying:

> When a constraint is uniform across the codebase and the constraint's
> justification is dead, fix it uniformly. Don't leave dead constraints
> in place just because they didn't trigger an observed bug yet. (Inverse
> of the "every behavior change gets gated" rule — that's for new
> behavior; removing dead constraints is restoration of intended
> behavior, different category.)

Without this principle, this audit would have drip-fixed 2 sites and
left 4 broken (including the OI forward sweep bug nobody had observed).
