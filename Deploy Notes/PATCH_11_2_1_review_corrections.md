# Patch 11.2.1 — Review corrections to canonical_gamma_flip

## Context

v11.2 was approved with two non-blocking cautions surfaced in review.
Both apply cleanly before the wrapper sees its first live call site,
so fixing now is cheaper than fixing across N migrated callers later.

## Two corrections applied

### Caution 1 — Documentation accuracy: "strike" → "price"

The docstring said "strike where net dealer GEX crosses zero." The
underlying engine sweeps spot across a grid of prices and uses linear
interpolation between adjacent grid points to find the zero. So a chain
with strikes [95, 100, 105] can produce a flip of 99.89 — between
strikes, on the continuous price axis. Updated wording everywhere in
the file:

- Module docstring: "Find the PRICE where dealer net gamma exposure
  crosses zero" + new explicit NOTE ON TERMINOLOGY paragraph
- Return-value section: "The PRICE (rounded to 2 decimals) where net
  dealer GEX crosses zero, found by linear interpolation between grid
  points. NOT necessarily a listed strike"

### Caution 2 — Safety: auto-derive dte_years from days_to_exp

Previously: caller had to pass BOTH `iv` and `dte_years` to get
IV-aware band widening. Pass `iv` alone, silently fell back to ±25%
blanket. That's a footgun — easy to forget the second argument.

Now: if `iv` is provided but `dte_years` is None, auto-derive
`dte_years = max(days_to_exp, 0.01) / 365.0`. Caller can still
override explicitly when they want a different time horizon than the
chain's DTE (rare — e.g. thesis-DTE band on a chain-DTE flip). Default
behavior is now the obvious behavior.

Code change at the top of `canonical_gamma_flip()`:

```python
if iv is not None and dte_years is None:
    dte_years = max(days_to_exp, 0.01) / 365.0
```

Plus updated docstring to describe both modes (auto-derive vs explicit
override).

## New test guards the auto-derive behavior

Added `test_iv_only_auto_derives_dte_years`:

- Calls canonical_gamma_flip with `iv=2.0` only (no dte_years)
- Calls canonical_gamma_flip with `iv=2.0, dte_years=3/365` (explicit)
- Asserts both return identical flip values
- Repeats for 60-day chain to confirm the auto-derive isn't accidentally
  using a fixed value

If the auto-derive ever regresses, this test catches it.

## Test count

```
test_raw_inputs.py:               13/13 passing
test_canonical_gamma_flip.py:     14/14 passing  (was 13, +1 for auto-derive test)
test_bot_state.py:                12/12 passing
TOTAL:                            39/39
```

## Files updated

- `canonical_gamma_flip.py` — both caution fixes applied
- `test_canonical_gamma_flip.py` — new test added

Existing v11.3 files (bot_state.py, research.html, etc.) unchanged.
The auto-derive change is API-compatible — every existing call site
continues to work identically.

## Header marker

```python
# canonical_gamma_flip.py — v11.2.1: docstring "strike"→"price"; auto-derive dte_years
```
