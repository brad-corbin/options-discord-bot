# Patch 11.3.2 — `canonical_iv_state` (corrects the 11.3.1 inline-helper sin)

## What this is

Replaces the inline `_atm_iv_from_chain()` from Patch 11.3.1 with a proper
canonical wrapper around `options_exposure.UnifiedIVSurface` — the
production canonical IV calculator that's been actively maintained
("Fix #14" with sqrt(1/DTE) × distance × OI weighting).

This is the patch that **should have been** Patch 11.3.1. The inline
helper was the wrong move and Brad correctly called it out.

## Files

### New
- `canonical_iv_state.py` — wraps UnifiedIVSurface (~190 lines, well-commented)
- `test_canonical_iv_state.py` — 8 tests including wrapper-consistency

### Updated
- `bot_state.py` — deletes inline `_atm_iv_from_chain`, calls canonical_iv_state,
  passes `representative_iv` (not naive ATM) into canonical_gamma_flip
- `test_bot_state.py` — replaces 2 tests of deleted helper with 3 tests of
  the canonical iv_state path

### Test count

```
test_raw_inputs.py:               13/13 passing
test_canonical_gamma_flip.py:     14/14 passing
test_canonical_iv_state.py:        8/8  passing  (NEW)
test_bot_state.py:                15/15 passing  (was 14, +1 new pattern)
TOTAL:                            50/50
```

## What canonical_iv_state does

```python
result = canonical_iv_state(chain, spot=274.90, days_to_exp=3.0)
# result = {
#   "representative_iv": 0.30,       # weighted-avg IV (production-canonical)
#   "atm_iv": 0.30,                   # IV at strike closest to spot
#   "iv_skew_pp": 0.0,                # 95%/105% skew, in IV percentage points
#   "iv30": None,                     # cross-expiration, out of scope
#   "source": "unified_iv_surface",   # diagnostic — where the value came from
# }
```

Internally:
1. Calls `engine_bridge.build_option_rows()` (canonical chain → OptionRow)
2. Instantiates `ExposureEngine(r=0.04)`
3. Instantiates `UnifiedIVSurface(rows, engine)` — same as production code
   (line 1150 of options_exposure.py)
4. Calls `surface.representative_iv(spot)` — the value production uses for
   the IV-aware band in gamma_flip / vanna_flip
5. Calls `surface.strike_iv(spot, spot)` — proper near-strike pooling, not
   naive single-strike pick
6. Computes skew from `strike_iv` at 95% and 105% of spot

The wrapper-consistency test (`test_canonical_matches_direct_unified_iv_surface_call`)
proves canonical_iv_state output is bit-identical to a direct UnifiedIVSurface
call. Same discipline as canonical_gamma_flip's wrapper-consistency test.

## What changed in bot_state.py

Three concrete changes:

1. **Deleted `_atm_iv_from_chain()`** entirely. The 31st implementation of
   "what's this chain's ATM IV" is gone.

2. **`build_from_raw()` now calls `canonical_iv_state`** as the FIRST canonical
   (before gamma_flip) so its `representative_iv` can be passed into
   canonical_gamma_flip's `iv` parameter for the IV-aware band.

3. **Wired up `atm_iv`, `iv_skew_pp`, `iv30` fields** from the canonical
   result. Previously these were None even on healthy chains because the
   `iv_state` canonical was a stub.

## Why the AMZN discrepancy was the symptom, not the disease

Brad observed silent thesis showed AMZN flip at $250.54, Research showed
$252.10. I correctly diagnosed the band-width difference but applied the
wrong fix — wrote a naive inline ATM IV extractor.

The actual problem: BotState wasn't using the canonical IV calculator
that production uses. So the fix wasn't "extract some IV value somehow,"
it was "use canonical_iv_state which wraps the same UnifiedIVSurface
that silent-thesis uses."

After this patch, BotState and silent-thesis pull representative_iv from
the same code path. Differences in the gamma_flip output (if any) will
trace to genuine differences in the chain or grid math, not to "we used
a different IV value."

## Files to drop

```
canonical_iv_state.py        → /canonical_iv_state.py        (NEW)
test_canonical_iv_state.py   → /test_canonical_iv_state.py   (NEW)
bot_state.py                 → /bot_state.py                 (REPLACE)
test_bot_state.py            → /test_bot_state.py            (REPLACE)
```

Four file drops. Nothing else changes — `canonical_gamma_flip.py`,
`raw_inputs.py`, `research_data.py`, `research.html`, `omega.css`,
`routes.py` all stay the same.

Commit, push via GitHub Desktop, Render redeploys.

## Process discipline going forward

What I should have done in 11.3.1 (and didn't):

```
BEFORE writing any "small inline helper":
  1. grep the inventory for the concept name (atm_iv, walls, gex, etc.)
  2. If 2+ existing implementations exist, write a canonical_X wrapper
     around the most-sophisticated existing one. Never inline.
  3. The wrapper-consistency test pattern is mandatory.
```

This is a hard rule for the rest of the rebuild. If I draft another
inline helper, that's me violating my own discipline and you should
push back on it again.

## Rollback

Worst case: revert these four files. The Research page goes back to
showing iv_state stubs and gamma_flip with blanket band. AMZN flip
discrepancy returns. Nothing else changes.

## After redeploy

1. Click Research
2. AMZN gamma_flip should now match silent thesis ($250.54 ± 1 grid step)
3. atm_iv field on the canonical status grid should go LIVE
4. Each ticker card's "PENDING CANONICAL COMPUTES" list should be one
   item shorter (iv_state removed)

If gamma_flip values still don't match silent thesis after this lands,
that's a real divergence to investigate — no longer can be blamed on
"different IV inputs."

## Header markers

```python
# canonical_iv_state.py — v11.3.2: wraps UnifiedIVSurface (production canonical)
# test_canonical_iv_state.py — v11.3.2: 8 tests, includes wrapper-consistency
# bot_state.py — v11.3.2: deletes inline _atm_iv_from_chain, calls canonical_iv_state
```
