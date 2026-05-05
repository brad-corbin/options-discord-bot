# v8.4 + Phase 2.8 — Final Deploy Runbook (audit-clean)

This is the merged Phase 2.8 (Telegram routing cleanup) + v8.4 (grading
bug fixes, companion shadow longs, display split, sheet join key)
deploy. All audit issues from `PHASE28_V84_AUDIT_NOTES.md` and the
post-audit follow-up are fixed.

## What's in the audit-clean iteration

Fixes added on top of the audit-fix zip:

1. **Three Phase 2.8 routing regressions** corrected:
   - Conviction reconciliation summary → diagnostic by default
   - EM accuracy/scorecard reports (manual + auto) → diagnostic by default
2. **`backtest/conviction_scorer.py` shim** registers the dynamically-loaded
   module in `sys.modules` before `exec_module` runs. Without this, Python
   3.13's `@dataclass` introspection raises NameError on forward references.
3. **EOD reconciler fallback for `opt_peak_*`** — when the streaming
   watermark is blank (cross-side plays, one-sided quotes, streaming hiccups),
   the reconciler now falls back to `recommendation_tracker.peak_option_mark`
   + `peak_ts` using the new `campaign_id` join key.
4. **`wipe_for_fresh_start.py`** — script for clean-slate Sunday wipe.

## Files in this drop

The full merged tree is in `merge_v84_phase28/`. Drop it into your
repo on top of whatever's there.

## Verification done

```
$ python3 -m py_compile app.py dashboard.py recommendation_tracker.py oi_flow.py
clean

$ python3 test_bug1_fixed.py        ✓ all GBUG1 scenarios
$ python3 test_bug2_fixed.py        ✓ all GBUG2 scenarios
$ python3 test_companion_longs.py   ✓ all 5 companion scenarios
$ python3 test_gbug3_and_splitter.py ✓ all GBUG3 + splitter sections
```

## Env vars — what you actually need to set

Two vars to **flip on** to get the behaviors you asked for:

```bash
RECTRACKER_REVIEW_ONLY_DISPLAY=split        # the scorecard layout you wanted
RECTRACKER_COMPANION_LONGS_ENABLED=1        # spawn long-only shadow alongside spreads
```

Everything else uses sensible defaults. **Leave the following unset** —
they already default correctly:

| Env var | Default | Behavior |
|---|---|---|
| `MORNING_OI_CONFIRMATION_TO_MAIN` | `1` (on) | Morning OI Confirmation → main ✓ |
| `MORNING_STALK_DIGEST_TO_MAIN` | `1` (on) | Stalk digest → main ✓ |
| `POTTER_BOX_SUMMARY_MAIN_ENABLED` | `1` (on) | Potter Box compact summary → main ✓ |
| `UNUSUAL_FLOW_SUMMARY_MAIN_ENABLED` | `1` (on) | 9 AM unusual flow summary → main ✓ |
| `NIGHTLY_SCREEN_MAIN_ENABLED` | `0` (off) | Nightly Screen → diagnostic ✓ |
| `SWING_DIGEST_MAIN_ENABLED` | `0` (off) | Swing scanner digest → diagnostic ✓ |
| `TELEGRAM_DIAG_FALLBACK_TO_MAIN` | `0` (off) | Diag posts no longer leak into main ✓ |
| `CONVICTION_RESULTS_MAIN_ENABLED` | `0` (off) | Conviction ✅/❌ summary → diagnostic ✓ |
| `EM_SCORECARD_MAIN_ENABLED` | `0` (off) | EM accuracy reports → diagnostic ✓ |
| `RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS` | unset | Legacy var; do NOT set — it would override the new display var to "hide" |

If you want any individual report back in main later, flip its env var
to `1`. Each one is independent.

## Unintended-consequences audit

Walking each thing this deploy changes and what could go wrong.

### `RECTRACKER_REVIEW_ONLY_DISPLAY=split`

**What changes:** daily / weekly / open-positions Telegram reports add
two new sections beneath the confirmed numbers — "BOT FOUND
(review-only)" and "COMPANION LONGS" — each with its own WR/net
rollup.

**What could go wrong:**
- **Confirmed numbers will look smaller than they used to.** Pre-deploy,
  WR/net was computed across *all* records including review-only and
  any companion-tagged records. Post-deploy, the top-line number is
  *only* confirmed records. This is the desired behavior, but the
  number will appear to drop. Total trade count goes from "everything
  the bot recorded" to "what you'd actually trade."
- **Long-history records sorting into review-only.** Records with
  `first_source` of `v84_credit_dual_post` or `v84_long_call_burst`
  (set in earlier deploys) now bucket as review-only by default. They
  were always being graded; you'll just now see them under their own
  section instead of mixed into confirmed WR.
- **Reports get longer.** Up to two extra sections per report. The
  "Currently Open" report is the most likely to grow visibly. If
  there's nothing in the new buckets, the sections don't render —
  no empty boilerplate.

**What does NOT change:** any record without `extra_metadata.review_only`,
without a review-only `first_source`, and without `companion_of` —
i.e., all of your real confirmed trades — keep rolling up exactly
the same as before in the confirmed bucket.

### `RECTRACKER_COMPANION_LONGS_ENABLED=1`

**What changes:** every spread record (debit AND credit) the bot
writes spawns a sibling long_call/long_put record at the same expiry.

**What could go wrong:**
- **Tracker write volume roughly doubles on spread-firing days.** Each
  spread → spread record + companion record. Both poll independently
  every 90s.
- **MarketData credit cost.** At recording time, the helper hits the
  chain cache first; if cached (likely, since the spread just fetched
  it), zero extra credit. If not cached or directional side differs
  (always for credit spreads), one fresh side-filtered chain fetch =
  ~1 credit per new companion. Polling thereafter uses the same cache
  as the original spread → no extra ongoing cost.
- **Companion grades on long-only thresholds.** Default exit_logic for
  long_call/long_put is target +50% / stop −35%. The original spread
  may have used different thresholds. This is intentional — the
  comparison answers "would buying the long at the same strike have
  hit +50% before this same trade hit its stop." That's the question
  you want answered.
- **Companion entry mark could be 0 if no chain data.** When the
  helper can't find a usable mid (illiquid strike, market closed when
  recording, side metadata mismatch), the companion is silently
  skipped — no record written, log line at INFO. Spread record is
  unaffected.
- **Failure isolation.** Companion creation is wrapped in try/except.
  Anything going wrong in the companion path cannot break the spread
  recording. Verified in code.

**What does NOT change:**
- The bot still recommends and posts the same things to Telegram. No
  "you'll see twice as many cards." Companion records are observability
  only — they go into the recommendation tracker / Position PnL tab,
  not into Telegram messages.
- The spread's grade is unaffected by what the companion does. The two
  records are independent.

### Routing changes

**What changes:** three existing report types switched from main →
diagnostic by default:
- Conviction reconciliation summary (`✅/❌` daily wins-losses)
- EM accuracy / scorecard reports
- Phase 2.8 cleanup of EOD flow / shadow / scheduled rec reports
  (already in the audit zip you uploaded; included here)

**What could go wrong:**
- **You stop seeing the daily conviction ✅/❌ summary in main.** This
  was the misleading data you flagged earlier — wins-losses computed
  from the conviction_plays.csv path, which is partly broken (the
  `opt_peak_*` blank-fill issue we documented). It's still posted, just
  to diagnostic. If you want it back in main while the underlying data
  is being fixed, set `CONVICTION_RESULTS_MAIN_ENABLED=1`.
- **You stop seeing EM scorecard accuracy in main.** Same reasoning —
  it's research, not a trade signal. Flip `EM_SCORECARD_MAIN_ENABLED=1`
  if you want it back.

**What does NOT change:**
- Trade alerts, conviction play CARDS (the actual flow alerts), Potter
  Box summaries, Unusual Options Flow, Morning OI Confirmation, and
  Stalk Digest all still post to main. Anything actionable stays in
  main; statistical/research summaries move to diagnostic.

### v8.4 grading bug fixes (GBUG1, GBUG2, GBUG3)

**What changes:** credit spreads no longer instantly grade +100% target
hit. Cross-side conviction plays no longer record the wrong entry mark.
Position PnL "Current PnL%" column for credit spreads now shows correct
sign.

**What could go wrong:**
- **Existing graded records in storage are still corrupted.** Every
  credit spread already in your tracker shows as a phantom +100%
  target_hit. The fixes are forward-only. Until those records age
  out of the lookback windows (or get wiped manually), shadow analytics
  / weekly summaries will still include some inflated WR. Allow
  ~7 days post-deploy for the rolling windows to refresh.
- **First post-deploy day's reports may still look weird.** Mix of
  pre-deploy phantom-wins and post-deploy real grades. By end of week
  the data is clean.

**What does NOT change:** any record that was graded correctly before
(long calls, long puts, debit spreads) keeps its grade unchanged.

### Position PnL tab schema

**What changes:** three new columns added — Source Type, Peak At CT,
Companion Of. The header-drift detector (`_sync_headers_if_drifted`)
runs on the first tick after deploy, sees the live tab is 16 cols and
the in-code schema is 19, rewrites the header row.

**What could go wrong:**
- **First tick after deploy: header row gets rewritten.** Existing data
  in A2:Z is cleared and rebuilt from records. This happens every tick
  anyway under normal operation, so no real data loss — but if you
  were eyeballing the tab when deploy happens, you'll briefly see
  empty rows.
- **Three new columns at the right edge.** If you have any pivots,
  charts, or filters on this tab in your Sheet, they'll need to be
  extended to cover the new columns. Existing references to columns A
  through K (Opened CT, Ticker, Campaign ID, Structure, Legs, Side,
  Strike, Entry Premium, Peak Premium, Peak PnL Lifetime, 2:45 PnL%)
  shifted by one column — Source Type slotted in at column D, so
  Structure moved from D→E and everything after also shifts.

**Rollback for this specific change:** revert `dashboard.py` to old
schema. The drift detector will re-write the header back to 16 cols
on the next tick.

### Sheet join key

**What changes:** `conviction_plays.csv` now has a `campaign_id`
column at the end. Cross-references to the Position PnL tab on the
other sheet.

**What could go wrong:**
- **Existing rows in the CSV have empty campaign_id.** Only new rows
  (post-deploy) carry the join key.
- **CSV column count changes.** Anything reading conviction_plays.csv
  by column index (instead of header name) will break. The codebase
  itself uses DictReader/DictWriter, so internal reads are safe.
  External downstream — your Sheet's import or any backtest scripts
  — should be checked.

**What does NOT change:** existing rows aren't rewritten or moved.

## Deploy order recommendation

1. Push the merge to your Render repo as one commit.
2. Set the two new env vars in Render config:
   ```
   RECTRACKER_REVIEW_ONLY_DISPLAY=split
   RECTRACKER_COMPANION_LONGS_ENABLED=1
   ```
3. Redeploy.
4. First market open: watch the dashboard log line confirming header
   drift detection (`dashboard: header drift detected on 'Position
   PnL'...`).
5. Watch one full session for any tracker poll errors. Companions are
   gated behind the env var so you can flip back instantly if anything
   looks wrong.

## What still isn't fixed (known, deferred)

- **Historical bad records in tracker storage.** Need a one-time wipe
  or re-grade pass. Not in this deploy.
- **Conviction_plays.csv `opt_peak_*` blank fill.** Streaming-tracker
  dependency makes that path unreliable. The recommendation tracker
  via Position PnL is the forward solution.
- **`/confirm` Telegram command.** Dropped from v8.4 plan per your
  earlier instruction.
- **Group C (flow card DTE/labels)** and **Group F (audit)** from the
  earlier handoff remain valid future work.

## Rollback

Each change carries a `# v8.4` or `# v8.4 (Patch GBUG{1,2,3}):` marker.
Grep, find, restore old block, redeploy.

To back out the new env-var-driven behavior without redeploying:
- Unset `RECTRACKER_REVIEW_ONLY_DISPLAY` → reports revert to inline
- Unset `RECTRACKER_COMPANION_LONGS_ENABLED` → companions stop spawning
  (existing companion records continue to grade out cleanly)

Both are one-line changes in Render config.
