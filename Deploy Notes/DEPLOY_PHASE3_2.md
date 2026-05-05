# Phase 3.2 Deploy — scorer_decisions audit fixes + spot_at_callout

Independent patch. Deploys cleanly on top of Phase 3 or Phase 3.1, both. No ordering requirement; can deploy before or after 3.1. 2 files, ~85 lines changed total. No new env vars.

## What this fixes

Four issues observed in `scorer_decisions.csv` that silently degraded the v8.3 backtested edge and blocked weekly grading:

| # | Symptom | Root cause | File |
|---|---|---|---|
| A | `adx` column blank on every row | Field computed in `_analyze_ticker` returned in signal dict but not copied into `webhook_data` | `active_scanner.py:~772` |
| B | `macd_hist=0`, `wt2=0`, `diamond=FALSE` on every row | `get_intraday_bars` retry order was ascending — countback=80 tried 20 first, returned 20 bars, MACD+WT need ≥35 bars, silent `{}` return | `app.py:4912-4915` |
| C | `#ERROR!` in reasons_summary on positive-weight rows | Strings `+3 B1: ...` interpreted as formulas by Sheets. Leading `B1` is valid cell reference → formula eval fails → `#ERROR!` | `app.py:3621` |
| D | No way to grade signals weekly vs Friday close | No `spot_at_callout` column in scorer_decisions.csv | `app.py:4107-4137` and `4219-4249` (both write sites) |

Fix B is the structurally important one — restores MACD/WT across all active_scanner signals, which fixes diamond, which fixes the FALLBACK_BOUNDS quintile lookup the v8.3 Phase 2 scorer depends on. The others are audit/grading cleanup.

## What's in this deploy

| File | Change | LOC |
|---|---|---|
| `active_scanner.py` | Fix A — adds `"adx": signal.get("adx", 0)` to `webhook_data` dict in `_scan_ticker` | +4 |
| `app.py` | Fix B — inverts `get_intraday_bars` countback retry order (requested size first, fall back smaller on 404) | +6/-1 |
| `app.py` | Fix C — prepends space to each `reasons_summary` item in `_build_conviction_reasons` | +7/-1 |
| `app.py` | Fix D — adds `spot_at_callout` column to scorer_decisions.csv at both write sites | +10 |
| `app.py` header | Adds v8.5 Phase 3.2 entry above Phase 3 | +24 |

## Deployment note — CSV schema change

Fix D adds a new column to `scorer_decisions.csv` AND the corresponding Google Sheet tab. The `_append_csv_row` helper writes the header only on first-write (when file doesn't exist or size is zero). On deploy:

- **Local CSV on Render disk**: old rows keep their old column count. New rows will have a trailing `spot_at_callout` value but the header row will still show only the old 22 columns. When viewing in Excel/Sheets, the new column will appear as an unnamed 23rd column.
- **Google Sheet tab**: `_append_google_sheet_row` likely already handles the new header; worst case the new column appends as blank in row 1 of the `scorer_decisions` tab.

**Recommended before deploy**: archive the existing local CSV and optionally clear the Sheet tab so the new 23-column header is written clean:

```bash
# On Render (SSH or via the file system tool if you have one):
mv /app/auto_logs/scorer_decisions.csv /app/auto_logs/scorer_decisions.2026-04-21.csv

# Or just leave the old file alone — the new column will still record,
# just with a slightly cosmetic misalignment in the CSV header row.
```

In Google Sheets, the simplest option is to rename the existing `scorer_decisions` tab to `scorer_decisions_pre32` so the new tab gets created fresh.

## Fix B — what to expect after deploy

Before 3.2: `scorer_decisions.csv` had `macd_hist=0`, `wt2=0`, `diamond=FALSE` on every row. After 3.2:
- During active sessions (≥35 5m bars available), those fields populate with real values.
- Early-session scans (first 2 hours after open) may still see `{}` returns because there genuinely aren't 35 5m bars yet — that's physics, not a bug. Check `bar_count` in the logs (scanner emits `insufficient_bars` at DEBUG when < 12).

**Verify the fix is live with one grep after first market-hours session:**

```bash
# Should produce non-zero hits on real macd_hist values after deploy:
awk -F',' 'NR>1 && $17!="0" && $17!="" {print}' scorer_decisions.csv | wc -l

# Should produce non-FALSE diamond values (TRUE when both quintiles middle):
awk -F',' 'NR>1 && $10=="TRUE" {print}' scorer_decisions.csv | head -5
```

If both stay zero after a full session post-deploy, MACD/WT still aren't computing — dig into bar_count specifically.

## Fix C — what to expect in Sheets

After deploy, every reasons_summary cell will have a single leading space. Sheets renders the space as normal padding; no visible change to how the text reads. The `#ERROR!` disappears from positive-weight rows.

If you prefer no leading space at all, alternative is to have the writer prepend `'` (apostrophe) — Sheets strips the apostrophe and forces text mode. Let me know if you want that variant instead; it's a one-character change.

## Fix D — using spot_at_callout for weekly grading

New column position: LAST column (after `reasons_summary`). Data type: float.

Weekly grading workflow (what the column enables):

1. On Friday at 3:00 PM CT equity close, note the closing spot price for each ticker that appeared in scorer_decisions.csv during the week.
2. For each `post` or `log_only` row: compute `(friday_close - spot_at_callout) / spot_at_callout` (or the signed variant for bear bias).
3. Split into buckets: score 60-69 / 70-79 / 80+ and decision=post vs log_only.
4. Look for the edge: does the scorer's score correlate with 5-day forward spot move?

`discard` rows (G1/G2 hard gates) are also worth tracking — if they systematically would have made money, the hard gates are too tight.

## Post-deploy verification (first full session)

```bash
# Fix A verified: adx column has values
awk -F',' 'NR>1 && $20!="" {print}' scorer_decisions.csv | wc -l  # non-zero

# Fix B verified: macd_hist + wt2 populate
awk -F',' 'NR>1 && $17!="0" && $17!="" {c++} END {print c}' scorer_decisions.csv

# Fix C verified: no #ERROR! in reasons_summary column (column 22)
grep -c "#ERROR" scorer_decisions.csv   # should be 0

# Fix D verified: spot_at_callout (column 23) populated
awk -F',' 'NR>1 && $23!="" {print $2, $23}' scorer_decisions.csv | head -10
```

## Rollback

All four fixes are surgical. No state migrations.

1. **Full revert**: restore `app.py` and `active_scanner.py` to pre-3.2. Pre-existing CSV rows with `spot_at_callout` values stay in file (they'll just be in an unexpected column beyond the old 22-column header); can be grep-filtered or left alone.
2. **Partial revert (keep what's working, drop the risky bit)**: if Fix B causes an unexpected API-rate issue because it now actually requests 80 bars every time (where previously it was getting 20), revert JUST that retry block. Keep A, C, D. That's unlikely — 80 bars is one API call, same as 20 bars, just returns more data.

## Not included

- The rec-tracker 404 loop on UNH/NFLX/XLV (marketdata.app 404). Separate issue, waiting on those positions to close.
- Any change to the Sheets writer. The space-prefix fix is cleaner than hacking valueInputOption at the Sheets layer.

## Ordering

- Deploy during off-hours (Thursday evening preferred).
- Optionally rename `scorer_decisions` Sheet tab to `scorer_decisions_pre32` before deploy so the 23-column header writes clean.
- Deploy, verify `# v8.5 Phase 3.2` appears in header of `app.py` on Render.
- Let Tuesday–Friday run a full session, then run the 4 verification greps above.
