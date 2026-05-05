# Phase 3.3 Deploy — Dashboard + Signal Log data completeness

Three files changed. No new env vars. Additive only. Rollback = revert the three files + rename the Sheet tab back.

## What this fixes

Every column you flagged was populating blank because of one of three patterns:
1. **Data saved to wrong key** (thesis)
2. **Data not saved at all** (notional, OI timestamp)
3. **Reader looking for wrong field names** (vol_regime, dealer_regime, OI direction, %Day hardcoded to None)

All three patterns get fixed in this deploy. Plus a new Spot column on Signal Log for weekly grading.

## Fix summary — Dashboard tab

| Column | Was showing | Now | Root cause |
|---|---|---|---|
| **%Day** | Blank (always) | Percent day change | Hardcoded to `None` since Phase 1 TODO. Now sourced from `prior_day_close` in the thesis blob — free data, already fetched for the Thesis fix. |
| **OI Time** | Blank | HH:MM CT of volume flag | `append_volume_flag` dict omitted `timestamp`. One-line add. |
| **OI Direction** | Blank | buildup / unwind / bullish / bearish | Same-day flags never have `flow_type` (assigned during next-morning confirmation). Reader now falls back to the `directional_bias` field that IS saved at write time. |
| **Flow Notional** | $0 (always) | Actual $ value | `save_flow_direction` dict omitted `notional`. Now computed inline as `vol × mid × 100`. |
| **Thesis Bias** | NEUTRAL (always) | BULLISH / BEARISH / NEUTRAL as actually computed | Reader called `persistent_state.get_thesis(ticker)` → Redis key `thesis:{TICKER}`. That key is never written. thesis_monitor persists to `thesis_monitor:{ticker}` (different key entirely). Fixed the reader to look at the real key. |
| **Thesis Score** | 0 (always) | Actual bias score | Same root cause as Thesis Bias. |

## Fix summary — Signal Log tab

| Column | Was showing | Now | Root cause |
|---|---|---|---|
| **Vol Regime** | Blank | CONTANGO / FLAT / BACKWARDATION / SEVERE_BACKWARDATION | Reader used `vts.get("state")` — wrong key. Actual key in `vix_term_structure.get_vix_term_structure()` is `term_structure`. |
| **Dealer Regime** | Blank | SPY's unified regime label (uppercased) | Never queried. Now reads `regime` from SPY's thesis blob at `thesis_monitor:SPY`. |
| **Spot (NEW)** | Didn't exist | Ticker spot at callout | New 13th column. Tab header now needs to be re-written. |
| **notional=$0 in `flow_conviction` detail** | `notional=$0` | Real $ value | Shares root cause with Dashboard's Flow Notional — same one-line fix. |

## What's in this deploy

| File | Change | LOC delta |
|---|---|---|
| `oi_flow.py` | 2 dict additions: `timestamp` in `append_volume_flag`, `notional` in `save_flow_direction` | +16 |
| `dashboard.py` | 6 fixes + 1 new column + 1 diagnostic: thesis key, %Day compute, OI direction fallback, vol_regime key, dealer_regime source, Spot column, AS Signal diagnostic | +249 |
| `app.py` header | Phase 3.3 entry above Phase 3.2 | +39 |

## Required Sheet migration BEFORE deploy

The new Signal Log Spot column means the 13-column header needs to write clean. If you leave the existing "Signal Log" tab as-is:
- Old 12-column rows stay (no data loss)
- New 13-column rows will write, but column L ("Outcome") will shift to column M underneath the new "Spot" header because the header only lists 12 items. The writer doesn't re-write the header after first creation — so the new header doesn't land until you force a rewrite.

**Recommended migration (2 clicks in the Sheet UI):**

1. Rename the existing "Signal Log" tab → "Signal_Log_pre33" (right-click the tab, Rename).
2. Deploy. The dashboard bootstrap code will auto-create a fresh "Signal Log" tab with the new 13-column header.

Historical data is preserved in the renamed `_pre33` tab. You can `IMPORTRANGE` it into reports if needed.

If you forget this step, the patch still works — you'll just have a cosmetically off header row 1 until you next clear+rename the tab.

## AS Signal — diagnostic added, fix NOT guaranteed yet

AS Signal persists as blank in your screenshots even for tickers that fired signals. I traced two possible causes:
- **Journal stores `bias`, reader looks for `side`** — fixed in this patch (one-line fallback)
- **Journal query returns empty for tickers that should have entries** — unclear without runtime evidence

To diagnose cause #2 without guessing, I added a one-time-per-ticker-per-deploy diagnostic log line:

```
[Phase3.3 diag] AS SPY: 3 entries, latest keys=tier=2 side=None bias='bullish' outcome='log_only'
[Phase3.3 diag] AS AAPL: 0 entries (date_from=2026-04-22)
```

After 24h of market-hours runtime post-deploy, search Render logs for `[Phase3.3 diag] AS` and share what's there. Three possible patterns:

| Pattern seen | Diagnosis |
|---|---|
| All tickers `0 entries` | `trade_journal.init_store` not firing OR journal Redis key name mismatch. Check for `"Trade journal store initialized"` in Render startup log — if absent, that's the bug. |
| Entries present, `tier=None` and `bias=None` | log_signal isn't receiving webhook_data with tier/bias fields. Trace active_scanner→worker→log_signal path. |
| Entries present, `tier=2, bias='bullish'` | The Phase 3.3 fallback fix IS enough. AS Signal will show "T2 🐂" for those rows. |

So pattern #3 is the expected outcome — the fallback fix in this patch IS the full fix IF the journal has data. The diagnostic tells us definitively.

## Deploy sequence (Thursday evening preferred, off-hours)

1. **Stop here if you haven't already deployed Phase 3.1 remediation + 3.2**. Phase 3.3 builds on those; applying 3.3 without them is still safe (it only touches fields they don't touch) but you'd lose the prior fixes.

2. On the Sheet: right-click "Signal Log" tab → Rename → "Signal_Log_pre33".

3. Replace on Render (from `/mnt/user-data/outputs/phase33/`):
   - `oi_flow.py`
   - `dashboard.py`
   - `app.py`

4. Deploy.

5. Confirm startup log contains:
   ```
   Trade journal store initialized
   dashboard: loop entering — first write in 60s
   ```
   Neither of those lines is new, but both being present means baseline health is intact.

## Post-deploy verification (first full session)

**Dashboard tab (check within ~2 min of first tick after open):**
- `%Day` column: non-zero float on tickers where thesis_monitor has `prior_day_close` saved. Tickers without thesis blob: blank (not $0, blank).
- `Flow Notional` column: non-zero dollar amount on rows where flow_direction is not "none".
- `OI Time` column: HH:MM CT on rows where OI Side has a value. Blank on rows where OI Side is also blank (no volume flag for that ticker yet).
- `OI Direction` column: buildup / unwind / bullish / bearish on rows where OI Side has a value.
- `Thesis Bias` column: BULLISH / BEARISH / NEUTRAL per the thesis_monitor computation. Blank on tickers thesis_monitor hasn't run on yet.
- `Thesis Score` column: integer score (-14 to +14).

**Signal Log tab (after first signal event post-deploy):**
- New column 11 "Spot": ticker spot at event time.
- `Vol Regime` (col 8): CONTANGO / FLAT / BACKWARDATION / SEVERE_BACKWARDATION.
- `Dealer Regime` (col 9): SPY's regime label.
- Any new `flow_conviction` event detail: `notional=$1,234,567` instead of `notional=$0`.

**Diagnostic log (grep after first ~30 min of market-hours runtime):**
```bash
grep "\[Phase3.3 diag\] AS" render.log | head -20
```
Share the output — that's what tells us whether AS Signal is fully fixed by this patch or needs a Phase 3.4 follow-up.

## Rollback

All fixes are surgical. No state migrations.

- **Full revert**: restore `oi_flow.py`, `dashboard.py`, `app.py` to pre-3.3. New Dashboard and Signal Log data remains in-place (it was only appended, never modified). Spot column stays as an unused 13th column until you clear+rename the tab.
- **Partial revert** (if something specific breaks): each fix is independent. Revert the specific dict-addition or function replacement. No cascading dependencies between fixes.

## Ordering

Order of the three files doesn't matter — all three can deploy simultaneously. No env var, no migration other than the Sheet tab rename.

## Deferred

- Previous-close fetch for tickers where thesis_monitor hasn't run (edge case — most tickers will have thesis)
- AS Signal confirmation — requires post-deploy runtime evidence via the new diagnostic

## Acknowledgment

Built from the Render-current files at `/home/claude/v8.5/audit/options-discord-bot-main/`, not pristine, per the revised process after the Phase 3.1→3.2 regression. All three files parse clean. Diff sizes sanity-checked.
