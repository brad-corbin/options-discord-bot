# Phase 2.8 + v8.4 Audit Fix Notes

This package merges the uploaded v8.4 observability files into the Phase 2.8 Telegram cleanup base and fixes issues found during audit.

## Issues found in the uploaded `files (3).zip`

1. It reverted Phase 2.8 routing behavior:
   - diagnostic posts could fall back into main by default
   - scheduled recommendation reports posted to main
   - Nightly Screen posted to main
   - Morning OI Confirmation posted to diagnosis instead of main
   - individual stalk alerts still posted instead of one Morning Stalk Digest
   - EOD flow / rotation reports still posted to main
   - Swing scanner was wired to main by default

2. The test file `test_gbug3_and_splitter.py` used hard-coded `/home/claude/...` paths and failed outside that machine.

3. `recommendation_tracker.py` had an unreachable duplicated weekly-summary block inside `poll_and_update_if_market_open()`.

4. Split-mode report behavior hid Bot Found / Companion sections when there were no confirmed records.

5. `RecommendationStore.list_graded_in_range()` could throw a TypeError if a historical graded record had `exit_ts=None`.

6. Credit-spread companion long tracking could accidentally price a bull-put companion long call using a same-strike put row if the passed chain rows lacked side metadata.

7. `dashboard._pnl_pct_lifetime()` still used long/debit math as a fallback for credit spreads when `mfe_pct` was missing.

8. Recommendation report headers still used `RECOMMENDATION RESULTS` language instead of the cleaner Bot Idea Tracking label.

## Fixes applied

- Restored Phase 2.8 routing env vars:
  - `TELEGRAM_DIAG_FALLBACK_TO_MAIN=0` default
  - `NIGHTLY_SCREEN_MAIN_ENABLED=0` default
  - `MORNING_OI_CONFIRMATION_TO_MAIN=1` default
  - `MORNING_STALK_DIGEST_TO_MAIN=1` default
  - `SWING_DIGEST_MAIN_ENABLED=0` default
  - `POTTER_BOX_SUMMARY_MAIN_ENABLED=1` default
  - `UNUSUAL_FLOW_SUMMARY_MAIN_ENABLED=1` default

- Kept Potter Box summaries and old Unusual Flow summary main-enabled by default.
- Kept Morning OI Confirmation in main by default.
- Replaced individual Morning Stalk Alerts with one Morning Stalk Digest.
- Routed Nightly Screen, scheduled rec reports, shadow reports, EOD flow summaries, and swing digests to diagnostic by default.
- Fixed test paths to be relative.
- Fixed split-mode report rendering when only Bot Found / Companion records exist.
- Removed unreachable duplicated code.
- Added a safer timestamp fallback in `list_graded_in_range()`.
- Forced credit-spread companion-long pricing to fetch the correct option side.
- Fixed dashboard lifetime PnL fallback math for credit spreads.

## Verification

```bash
python -m py_compile app.py dashboard.py recommendation_tracker.py test_bug1_fixed.py test_bug2_fixed.py test_companion_longs.py test_gbug3_and_splitter.py
python test_bug1_fixed.py
python test_bug2_fixed.py
python test_companion_longs.py
python test_gbug3_and_splitter.py
python -m compileall -q .
```

All checks passed in the audit workspace.
