# Patch 11.3 — Research page + BotState (BotState rebuild · step 3 of N)

## What this is

The dashboard ENGINES surface — except renamed to **Research**, replacing the
empty Diagnostic placeholder you've had sitting there waiting for Phase 6.

Combined with:
- `BotState` — the canonical immutable state contract, with permissive build
- `research_data.py` — universe-level Research payload builder
- `research.html` — Legacy Desk styled template
- CSS additions matching your existing tokens
- Routes patch (rename Diagnostic → Research)

## Files in this patch

### New
- `bot_state.py` — canonical state dataclass + `build_from_raw()` (~370 lines)
- `test_bot_state.py` — 12 tests, all passing
- `omega_dashboard/research_data.py` — universe builder (~190 lines)
- `omega_dashboard/templates/dashboard/research.html` — Legacy Desk styled
- `research_page_styles.css` — appended to `omega_dashboard/static/omega.css`
- `PATCH_11_3_routes_changes.md` — exact routes.py edits

### Patched (small)
- `omega_dashboard/routes.py` — rename PAGE_TABS entry, replace diagnostic route
- `omega_dashboard/static/omega.css` — append research_page_styles.css

### Tests
```
test_raw_inputs.py:               13/13 passing
test_canonical_gamma_flip.py:     13/13 passing
test_bot_state.py:                12/12 passing
TOTAL:                            38/38
```

## End-to-end demonstration

Running synthetic put-wall/call-wall chain through the full pipeline:

```
Ticker: SPY
Spot: $100.00
Gamma flip: 99.89                ← REAL (from canonical_gamma_flip)
Distance from flip: +0.11%       ← derived
Flip location: at_flip           ← derived
GEX sign: unknown                ← stub (canonical_gex pending)
Volume today: 1,000,000          ← from quote
rvol: 1.25x                      ← from quote
Fields lit: 12/64

Canonical status:
  gamma_flip: live                ← Patch 11.2 lit it
  walls: stub: pending            ← Patch 11.4 will light it
  gex: stub: pending              ← Patch 11.5 will light it
  technicals: stub: pending       ← Patch 11.6 will light it
  ... (16 more canonicals queued)
```

Every canonical that lands flips from `stub` → `live` automatically.
Template doesn't change. Engines don't change.

## What you'll see in the dashboard

After deploy, click **Research** in the top nav. Three sections:

**1. REBUILD PROGRESS** — top of page

Shows a grid of 17 canonical compute statuses across the universe.
Each cell color-coded:
- **Green left border** = live across all tickers
- **Brass left border** = partial (some tickers live, some stub)
- **Dim** = stub (canonical not yet implemented)
- **Oxblood** = error (canonical implemented but failing)

After this patch lands: only `gamma_flip` is green. Others dim. Visual
representation of "how far through the rebuild are we."

**2. TICKER STATE** — middle, 35 cards

Per-ticker BotState card. Shows:
- Ticker, spot, fields-lit count
- **FLIP STATE** section (live): gamma_flip, distance, location
- **PENDING CANONICAL COMPUTES** section (lists what's not lit yet)
- Fetch errors if any

Cards lay out in a grid, ~3-4 across depending on viewport. Auto-fits.

**3. REBUILD ROADMAP** — bottom, always visible

Tracks what's done and what's next. After this patch:
- DONE: raw_inputs.py, canonical_gamma_flip, BotState.build_from_raw
- NEXT: canonical_walls, canonical_gex, canonical_technicals
- LATER: First shadow engine cutover (silent_thesis on BotState)

Roadmap rows update as patches ship.

## Visual style — matches Legacy Desk

- Cards use existing `--bg-panel` / `--bg-elev` tokens
- Eyebrows use `--brass` color, Cinzel font, 2px letter-spacing
- Numerics in JetBrains Mono (matches Trading page level rows)
- Color coding: green for "live" (#5C8A4F), oxblood for errors (#8B3A30)
- Nothing imported from outside the existing system — same fonts, palette,
  spacing as Desk / Trading / Portfolio

## How the data flows

```
User loads /research
   ↓
routes.py: research()
   ↓
research_data.research_data(data_router=_cached_md)
   ↓
For each ticker in DEFAULT_TICKERS (35 tickers):
   ├─ Check 60s in-memory cache
   ├─ If miss: BotState.build(ticker, expiration, data_router)
   │   ↓
   │  raw_inputs.fetch_raw_inputs() — DataRouter call
   │   ↓
   │  BotState.build_from_raw(raw)
   │   ├─ canonical_gamma_flip(raw.chain, raw.spot, dte) → real value
   │   ├─ canonical_gex(...) → NotImplementedError (stub)
   │   ├─ canonical_walls(...) → NotImplementedError (stub)
   │   ├─ ... (every other canonical raises stub)
   │   ↓
   │  BotState with gamma_flip lit, others None, status recorded
   ↓
ResearchData payload with 35 snapshots
   ↓
research.html renders cards
```

Every canonical wrapped in try/except — page renders even if canonical
functions crash. Always-render guarantee.

## Cache strategy

In-memory, 60s TTL, per-ticker. First page load makes ~35 Schwab calls
(one per ticker, all cached for 60s). Within 60s subsequent loads pay
zero Schwab cost. Per-process cache so single-instance Render deployment
is fine. If horizontally scaled, swap to Redis (out of scope for this patch).

## How to verify

```bash
# Drop the new files
cp bot_state.py /path/to/repo/
cp raw_inputs.py /path/to/repo/
cp canonical_gamma_flip.py /path/to/repo/
cp research_data.py /path/to/repo/omega_dashboard/
cp research.html /path/to/repo/omega_dashboard/templates/dashboard/

# Append CSS
cat research_page_styles.css >> /path/to/repo/omega_dashboard/static/omega.css

# Apply routes changes per PATCH_11_3_routes_changes.md
# (rename PAGE_TABS entry, replace diagnostic() with research() handler)

# AST check everything
cd /path/to/repo
python3 -c "import ast; ast.parse(open('bot_state.py').read())"
python3 -c "import ast; ast.parse(open('raw_inputs.py').read())"
python3 -c "import ast; ast.parse(open('canonical_gamma_flip.py').read())"
python3 -c "import ast; ast.parse(open('omega_dashboard/research_data.py').read())"
python3 -c "import ast; ast.parse(open('omega_dashboard/routes.py').read())"

# Run tests
python3 test_bot_state.py        # 12/12
python3 test_raw_inputs.py       # 13/13
python3 test_canonical_gamma_flip.py  # 13/13

# Deploy. Click Research tab. See the rebuild taking shape.
```

## Rollback

Worst case: revert the routes.py changes. The new files just sit there
unused. Old Diagnostic page can be restored by reverting that one line
in PAGE_TABS plus restoring the old diagnostic() route.

## What this unlocks

Brad sees:
- The rebuild taking shape in the browser (not Telegram, not chat output)
- Per-ticker live state for the canonical functions that exist
- A roadmap of what's coming
- Progress percentages that go up as canonicals land

Going forward: each canonical compute patch (canonical_walls, canonical_gex,
etc.) lands → the corresponding BotState fields go from None → real values
→ the Research cards show more data → the canonical status grid goes from
dim → green for that compute. Same pattern, repeated.

## Header markers

```python
# bot_state.py — v11.3 (BotState step 3): canonical state contract + permissive build
# test_bot_state.py — v11.3: 12 unit tests for BotState.build_from_raw
# omega_dashboard/research_data.py — v11.3: universe-level Research payload
# omega_dashboard/templates/dashboard/research.html — v11.3: Legacy Desk styled
```

## Where we are in the rebuild

| Patch | Component | Status |
|---|---|---|
| 11.1 | `raw_inputs.py` — DataRouter wrapper | ✓ shipped |
| 11.2 | `canonical_gamma_flip` | ✓ shipped |
| 11.3 | BotState + Research page | ✓ shipping NOW |
| 11.4 | `canonical_walls` (Step B from your plan) | next |
| 11.5 | `canonical_gex` | queued |
| 11.6 | `canonical_technicals` (RSI/MACD/ADX/VWAP) | queued |
| 11.7+ | Remaining 14 canonicals | queued |
| 11.X | First shadow engine cutover (silent_thesis on BotState) | after critical mass of canonicals |

The Research page now renders the rebuild's progress visually. As patches
land, you literally watch the page get more populated — no waiting for
Telegram, no wondering what's working.
