# Patch 11.4.1 — Review cleanup before deploy

## Why

You held Patch 11.4 for a small cleanup pass with four items. All four
addressed.

## Changes

### Fix 1 — bot_state.py passes `dte_years` explicitly to canonical_gamma_flip

You're correct as a contract concern. The auto-derive IS in
canonical_gamma_flip (Patch 11.2.1), so functionally it works today —
but bot_state.py reading alone doesn't show that IV-aware widening will
happen. If anyone refactors the canonical's auto-derive away, bot_state.py
silently regresses to ±25% blanket with no failing test.

Fix in `_call_canonical_gamma_flip()`:

```python
def _call_canonical_gamma_flip(chain, spot, days_to_exp, *, iv=None):
    from canonical_gamma_flip import canonical_gamma_flip
    dte_years = (max(days_to_exp, 0.01) / 365.0) if iv is not None else None
    return canonical_gamma_flip(
        chain, spot=spot, days_to_exp=days_to_exp,
        iv=iv, dte_years=dte_years,
    )
```

Defense-in-depth. Caller is now explicit, doesn't rely on the canonical's
internal safety net.

### Fix 2 — Research roadmap updated

Stale text removed. The roadmap section in `research.html` now reflects
reality:

```
DONE — raw_inputs.py
DONE — canonical_gamma_flip
DONE — BotState.build_from_raw
DONE — canonical_iv_state              (was missing before)
DONE — canonical_exposures             (was listed as canonical_gex / NEXT)
NEXT — canonical_walls                 (description updated: "same canonical, no new compute")
NEXT — canonical_technicals
LATER — First shadow engine cutover
```

I also did a sanity audit on the divs — `<div` opens vs `</div>` closes
balance 56/56 across the file. I couldn't find the extra closing div you
flagged; if it shows up rendered after this update, please send me the
view and I'll dig deeper.

### Fix 3 — research_data cache key includes expiration

```python
# Before:
_CACHE[ticker] = (timestamp, snap)

# After:
_CACHE[(ticker, expiration)] = (timestamp, snap)
```

Smoke-tested: caching SPY/2026-05-09 and SPY/2026-05-16 separately now
returns the right snapshot for each — no cross-contamination.

### Fix 4 — CSS confirmed unchanged

I audited every CSS class used in the new GREEKS section. All exist in
the existing `research_page_styles.css` from Patch 11.3:

```
research-card-section, research-card-eyebrow, research-live,
research-card-row, research-card-label, research-card-value,
positive, negative
```

No CSS changes in v11.4 / v11.4.1. Six-file deploy list is correct.

## Files (replaces three from v11.4)

```
bot_state.py        → /bot_state.py                         (REPLACE)
research_data.py    → /omega_dashboard/research_data.py     (REPLACE)
research.html       → /omega_dashboard/templates/dashboard/research.html  (REPLACE)
```

The other three v11.4 files (`canonical_exposures.py`, `test_canonical_exposures.py`,
`test_bot_state.py`) are unchanged — drop them as they were in v11.4.

## Tests

```
test_raw_inputs.py:               13/13 passing
test_canonical_gamma_flip.py:     14/14 passing
test_canonical_iv_state.py:        8/8  passing
test_canonical_exposures.py:       9/9  passing
test_bot_state.py:                16/16 passing
TOTAL:                            60/60
```

All 60 still pass after the fixes — no behavior regression.

## Final deploy list (6 files)

```
canonical_exposures.py        → /canonical_exposures.py        (NEW, from v11.4)
test_canonical_exposures.py   → /test_canonical_exposures.py   (NEW, from v11.4)
bot_state.py                  → /bot_state.py                  (REPLACE, v11.4.1)
test_bot_state.py             → /test_bot_state.py             (REPLACE, from v11.4)
research_data.py              → /omega_dashboard/research_data.py (REPLACE, v11.4.1)
research.html                 → /omega_dashboard/templates/dashboard/research.html (REPLACE, v11.4.1)
```

CSS unchanged. No additional file needed.

Commit, push, Render redeploys.

## On the "extra div" question

I couldn't find an extra closing div in research.html — `<div` opens and
`</div>` closes balance at 55/55 in v11.4 and 56/56 in v11.4.1 (one row
added to roadmap). If you see something layout-wise that looks broken
after redeploy, screenshot and I'll dig in. It's possible the original
concern was about `{% endif %}` boundaries inside Jinja conditionals
making the visual nesting hard to read — that's cosmetic in the source,
not a real div issue. But happy to re-audit if you spot it rendered.
