# Patch 11.2 — `canonical_gamma_flip` (BotState rebuild · step 2 of N)

## What this is

The first canonical compute function lands. Wraps the existing post-Patch-9
`ExposureEngine.gamma_flip()` math behind a single entry point that every
flip-reading site in the codebase will eventually redirect to.

Also includes a correction to `raw_inputs.py` from Patch 11.1 — chain shape
should be raw dict-of-arrays (matches what `engine_bridge.build_option_rows()`
expects), not pivoted to list-of-dicts.

## Files

### New
- `canonical_gamma_flip.py` — the wrapper (~180 lines, well-commented)
- `test_canonical_gamma_flip.py` — 13 unit tests, all passing

### Updated (corrected from Patch 11.1)
- `raw_inputs.py` — `chain` field now stays as raw dict-of-arrays. Added
  `chain_rows` property as helper for legacy display code that wants
  list-of-dicts. Removes the parallel `_normalize_chain_response`
  pivot that was *adding* fragmentation rather than removing it.
- `test_raw_inputs.py` — tests updated for the corrected chain shape.
  All 13 still pass.

## Combined test results

```
test_raw_inputs.py:               13/13 passing
test_canonical_gamma_flip.py:     13/13 passing
TOTAL:                            26/26
```

All run without network or credentials, against synthetic data.

## What the wrapper does

```python
flip = canonical_gamma_flip(
    raw.chain,          # dict-of-arrays from DataRouter
    spot=raw.spot,
    days_to_exp=3.0,
    iv=0.30,            # optional — enables IV-aware band widening
    dte_years=3/365,    # required if iv given
)
# Returns: float (price where net GEX crosses zero) or None (flip outside band)
```

Internally:
1. Calls `engine_bridge.build_option_rows()` to convert chain → OptionRow list
2. Builds a price sweep grid (IV-aware widening if iv+dte_years, else ±25%)
3. Calls `options_exposure.ExposureEngine.gamma_flip()` to find zero-crossing
4. Returns the price or None

## Why this matters

The data inventory found **165 hits across 24 files** for gamma flip detection,
plus 46 more hits for `flip_price` (same concept, different name) in 9 more
files. Every one of those is now a candidate for redirection through
`canonical_gamma_flip()`. After enough redirects, there's exactly ONE place
in the codebase where "what's this ticker's flip" is computed.

The wrapper-consistency test (`test_canonical_matches_direct_engine_call`)
proves the wrapper output is bit-identical to a direct ExposureEngine call.
That's the contract — switching call sites to the wrapper changes nothing
about the math, only the centralization.

## What this does NOT do

- Does not redirect any existing call sites yet. Those happen as separate
  patches, one engine at a time, during the shadow-deployment phase.
- Does not delete the existing flip-finding code. Same reason.
- Does not change any behavior in the deployed bot. Both new files sit
  unused until something explicitly imports them.

## How to verify

```bash
# AST clean
python3 -c "import ast; ast.parse(open('canonical_gamma_flip.py').read())"
python3 -c "import ast; ast.parse(open('test_canonical_gamma_flip.py').read())"

# All 26 tests pass
python3 test_raw_inputs.py
python3 test_canonical_gamma_flip.py
```

## Deploy

Nothing to deploy. Two new files (`canonical_gamma_flip.py`,
`test_canonical_gamma_flip.py`) and two updated files (`raw_inputs.py`,
`test_raw_inputs.py`). No call site in the bot uses them yet.

Drop them into the repo. Bot keeps running unchanged.

## Mistake caught (worth noting)

I introduced `_normalize_chain_response()` in Patch 11.1 that pivoted the
chain to list-of-dicts. When I started writing `canonical_gamma_flip` and
needed to call `engine_bridge.build_option_rows()`, I discovered the
canonical converter expects dict-of-arrays — the *opposite* of what I'd
just normalized to.

That's exactly the kind of mistake the audit-discipline rule is designed
to catch ("verify against the actual file before editing"). I designed
the chain shape based on `_chain_rows_from_response` in app.py without
checking that `build_option_rows` is the actually-canonical converter.

Lesson: when there are multiple existing converters for the same data,
the right canonical one is the one used by the most-canonical *consumer*
(ExposureEngine), not the one used by display code.

## Header markers

```python
# canonical_gamma_flip.py — v11.2 (BotState step 2): canonical wrapper for gamma flip
# test_canonical_gamma_flip.py — v11.2: 13 unit tests, includes wrapper-consistency
# raw_inputs.py — v11.2 correction: chain stays as dict-of-arrays
# test_raw_inputs.py — v11.2 correction: chain shape tests updated
```

## Next up (Brad's plan: A → C → B)

A is done. **C is next** — write `BotState.build()` against canonical_gamma_flip
plus stubs for everything else, then the first dashboard ENGINES tab renderer
that displays the in-progress state. Lets you SEE the new system taking shape
even before the rest of the canonical functions land.

Then B: `canonical_walls` — bigger conversation because there are multiple
existing implementations to choose from.
