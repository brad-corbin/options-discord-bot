# v8.4 Observability Overhaul — Deploy Runbook

Single deploy fixing every grading and tracking corruption identified in
this session. None of these change what the bot recommends or posts.
They fix what gets **recorded**, how it's **graded**, and how it's
**displayed**.

## Files in this deploy

| File | Status | Risk |
|------|--------|------|
| `app.py` | patched (header + 8 regions) | low |
| `dashboard.py` | patched (header + 4 regions) | low |
| `recommendation_tracker.py` | patched (helpers + 3 reports) | low |

All three AST-clean. Four test files in this directory pass.

## What ships

### 1. GBUG1 — credit-spread sign error (`app.py`)
`_rec_tracker_price_fn` spread branch returned `max(buy_mid - sell_mid, 0)`
for all spreads. For credits this is always 0 → instant +100%
target_hit. Now branches on structure.

### 2. GBUG2 — conviction entry-mark mismatch (`app.py`)
`_record_conviction_recommendation` ignored `cp.get("rec_mid")` and
recorded the FLOW side's mid as entry while polling the recommended
side's mid. Now prefers `rec_mid` in the fall-through chain.

### 3. GBUG3 — Position PnL "Current PnL%" sign (`dashboard.py`)
`_pnl_pct_current` used long-option math for credit spreads → inverted
sign. Now branches on `pricing_mode`.

### 4. Position PnL tab schema additions (`dashboard.py`)
- New columns: **Source Type** (confirmed / review_only / companion_long),
  **Peak At CT** (timestamp from `peak_ts`), **Companion Of** (campaign_id
  of paired record).
- `_sync_headers_if_drifted` runs on first tick after deploy. Detects
  schema drift in the live tab, rewrites the header row. Idempotent.
  No data loss — A2:Z is rebuilt every tick by existing code.

### 5. Display split (`recommendation_tracker.py`)
- New env var `RECTRACKER_REVIEW_ONLY_DISPLAY`:
  - `inline` (default) — pre-2.7 behavior. Mixed in with confirmed.
  - `split` — separate "Bot Found" + "Companion Longs" sections in each
    report, each with its own WR/net. **The mode Brad asked for.**
  - `hide` — 2.7 behavior. Hidden entirely.
- Old `RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS=1` still works
  (mapped to `hide`).
- Affects `generate_daily_report`, `generate_weekly_summary`,
  `generate_open_positions_report`.

### 6. Companion shadow longs (`app.py`)
- Env var `RECTRACKER_COMPANION_LONGS_ENABLED` (default OFF).
- When ON, every spread record (debit AND credit) spawns a sibling
  long_call/long_put record with same expiry.
- Strike selection:
  - **Debit spreads** (bull_call, bear_put): long-leg strike (lean (b),
    apples-to-apples)
  - **Credit spreads** (bull_put, bear_call): short-leg strike,
    directionally-matched right (bull_put → long_call, bear_call →
    long_put)
- Companions tagged `extra_metadata.companion_of=<spread_cid>`,
  graded independently.
- Wired into `_record_check_ticker_recommendation`,
  `_record_swing_recommendation`, `_record_income_opportunity`.

### 7. Sheet join key (`app.py`)
- `conviction_plays.csv` writes `campaign_id` as the last column.
- `_record_conviction_recommendation` mutates `cp["campaign_id"]` on
  success. `_log_conviction_play` reads it.
- Empty for shadow/skipped plays.
- VLOOKUP-able between Omega 3000 sheet (decision logs) and Dashboard
  3000 sheet (Position PnL outcomes).

### 8. Conviction_plays peak-fill audit (no patch)
Documented as a finding, not fixed in this deploy.
`get_option_store().track_conviction` is gated on
`_live_entry_mid > 0`. When the live mid is unavailable (cross-side
pre-GBUG2, one-sided quotes, streaming subscription delays), the OCC
is never registered → EOD reconciler finds nothing. The forward fix is
the recommendation tracker, which polls every 90s via MarketData
regardless of streaming state. **Position PnL with the new Peak At
column becomes the reliable source.** The conviction_plays opt_peak_*
columns become supplementary.

## Env vars summary

```bash
# Enable the new display mode (the one Brad asked for):
RECTRACKER_REVIEW_ONLY_DISPLAY=split

# Enable companion shadow longs alongside every spread:
RECTRACKER_COMPANION_LONGS_ENABLED=1

# (Existing — still works for back-compat, mapped to 'hide'):
# RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS=1
```

Default state with no env-var changes is functionally a no-op for
points 5 and 6 (existing behavior preserved). Points 1-4 and 7-8
take effect on deploy regardless of env vars — they're correctness
fixes.

## Verification done

```
$ python3 -c "import ast; ast.parse(open('app.py').read())"
AST clean
$ python3 -c "import ast; ast.parse(open('dashboard.py').read())"
AST clean
$ python3 -c "import ast; ast.parse(open('recommendation_tracker.py').read())"
AST clean

$ python3 test_bug1_fixed.py        ✓ 3/3 scenarios
$ python3 test_bug2_fixed.py        ✓ 3/3 scenarios
$ python3 test_companion_longs.py   ✓ 5/5 scenarios
$ python3 test_gbug3_and_splitter.py ✓ all sections
```

## Post-deploy watch

**First market open after deploy:**
- Position PnL tab header row should auto-update to 19 columns
  (was 16). Look for the log line:
  `dashboard: header drift detected on 'Position PnL' — live=16 cols, expected=19 cols. Rewriting header row.`
- Existing data rows (A2 onward) get cleared and rebuilt by the next
  tick. Phantom +100%/-100% credit spread rows from GBUG1 should
  re-render with sane numbers from now on.
- Closed credit spreads: "Current PnL%" column now matches "Peak PnL%
  Lifetime" in sign convention.

**With `RECTRACKER_REVIEW_ONLY_DISPLAY=split` set:**
- Daily / weekly / open-positions Telegram reports gain "BOT FOUND"
  and "COMPANION LONGS" sections beneath the confirmed numbers.
- WR / net rollups computed separately per section. Confirmed numbers
  reflect only what Brad would have actually traded.

**With `RECTRACKER_COMPANION_LONGS_ENABLED=1` set:**
- Every spread the bot records now spawns a paired long. Roughly 2x
  the recommendation_tracker write volume on spread-firing days.
- After ~2 weeks of data, compare:
  ```
  Companion Longs WR/net  vs.  Confirmed Spreads WR/net
  ```
  in the weekly summary. If companions consistently outperform spreads,
  Brad's intuition that "the bot should be calling longs not spreads"
  is validated.

## Rollback

Every patch carries a `# v8.4 (Patch GBUG{1,2,3}):` or `# v8.4:` marker
for grep. To revert any single patch, find the marker and restore the
old block. No state migration — just redeploy old `app.py` /
`dashboard.py` / `recommendation_tracker.py`.

To disable the new behaviors without redeploying:
- Unset `RECTRACKER_REVIEW_ONLY_DISPLAY` → reverts to inline display
- Unset `RECTRACKER_COMPANION_LONGS_ENABLED` → companions stop spawning
  (existing companion records continue to grade out)

## What this does NOT fix

- **Historical bad records.** Every credit spread and every cross-side
  conviction play already in the store is graded as a phantom win.
  Shadow analytics will continue to show inflated WR until those
  records are wiped or re-graded. Separate cleanup task — not in this
  deploy.
- **`/confirm` Telegram command.** Per Brad's instruction this turn,
  this is dropped from the v8.4 plan entirely. The display split with
  `companion_of` semantics + Source Type column gives Brad the
  scorecard distinction he wanted without manual entry friction.
- **Conviction_plays opt_peak_* fill reliability.** Streaming-tracker
  dependency makes that path unreliable; the forward fix is to rely
  on Position PnL + Peak At. No code change in this deploy.
- **Group A / Group B / Group C / Group F from prior handoff.** Group
  A (review-only grading exclusion) is now obsolete — the data
  corruption it tried to mitigate was actually GBUG1+GBUG2+GBUG3, all
  fixed here. Group B (channel routing), Group C (flow card
  DTE/labels), and Group F (audit) remain valid future work.
