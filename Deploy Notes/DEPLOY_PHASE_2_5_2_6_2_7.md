# Deploy Runbook — v8.4 Phase 2.5 + 2.5b + 2.5c + 2.6 + 2.7

**Target deploy:** Sunday night, single deploy, five phases bundled.

**What changed:** Four files. App-level header blocks document each patch.
- `app.py`                    — Phase 2.5 + 2.5b + 2.5c + 2.6 + 2.7 (env vars + helpers + header)
- `long_call_burst_builder.py` — NEW (Phase 2.6)
- `market_clock.py`           — extended (Phase 2.6)
- `recommendation_tracker.py` — Phase 2.7 + Phase 2.5b Patch Q

**Default behavior with no env changes:** No-op. All env vars default OFF. A clean deploy without flipping any switch produces zero behavior change.

**What 2.5c adds on top of 2.5b:**
- Pre-gate at `_process_job` removed. The gate now runs only at concrete post-build sites (credit, burst, signal-only) where a stable vehicle_id exists.
- Adds signal-only card dedup using `ticker_bias_signal_only_T{tier}_{wave}` as the vehicle_id.
- `_process_job` bails before `check_ticker` when a post-site gate suppressed a duplicate, preventing redundant signal-only cards.

**What Phase 2.5b adds on top of 2.5:**
- Vehicle-aware gate. Snapshot now stores `vehicle_kind` + `vehicle_id`. Gate suppresses only when current vehicle == prior vehicle.
- Post-success marking. `_mark_scorer_posted` only fires after a card actually posts to Telegram. Failed/skipped post paths leave the snapshot clean.
- Burst tracking. Long Call Burst cards now record to the recommendation tracker as review-only (source `v84_long_call_burst`).
- Default review-only source list extended to cover the new burst source.

---

## Env vars (all default OFF — flip to enable)

### Phase 2.5 — Scorer Repost Gate
```
SCORER_REPOST_GATE_ENABLED=1                          # master switch
SCORER_REPOST_COOLDOWN_MIN=45                         # min minutes between reposts
                                                      # (was 60 in initial draft;
                                                      # 45 is the production-safe value)
SCORER_REPOST_MIN_SCORE_DELTA=5                       # score must jump this much to repost (default 5)
SUPPRESS_SINGLE_ITEM_DIGEST_BEFORE_FULL_CARD=1        # collapse 1-item digest when full card is firing
```

### Phase 2.5b — Vehicle-aware gate / post-success mark / burst tracking
No new env vars. The behavior is built into Phase 2.5's `SCORER_REPOST_GATE_ENABLED` and Phase 2.6's `BURST_FIRST_ROUTING_ENABLED` — flipping those activates the 2.5b improvements (gate uses vehicle fingerprint; mark only on actual post; burst writes to tracker).

The new "allow" reasons that show up in `scorer_suppressed_reposts.csv` and the worker logs are:
- `no_prior_vehicle` — prior fire built nothing; current fire allowed regardless of cooldown
- `vehicle_changed`  — prior posted credit on different strikes/expiry, or burst↔credit transition
- `wave_label_upgrade` (existing) — wave moved into confirm/burst
- `cooldown_elapsed_score_jump` (existing) — both timer and score delta cleared

### Phase 2.6 — Burst routing + trading-day DTE
```
BURST_FIRST_ROUTING_ENABLED=1                         # master switch — burst routing
USE_TRADING_DAYS_FOR_CREDIT_DTE=1                     # independent — fixes credit DTE picker
BURST_OTM_BAND_LOW=-0.005                             # OTM band lower bound, default -0.5% (allows ATM-ish ITM strikes)
BURST_OTM_BAND_HIGH=0.025                             # OTM band upper bound, default 2.5%
BURST_MAX_BID_ASK_SPREAD_FRAC=0.25                    # reject strikes wider than 25% spread/mid
```

### Phase 2.7 — Review-only filter
```
RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS=1          # master switch
RECTRACKER_REVIEW_ONLY_SOURCES=v84_credit_dual_post   # CSV of source names that are review-only
```

### Recommended Sunday turn-on order
1. Deploy first with everything OFF — confirm no regressions in Monday open.
2. Flip `RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS=1` first — display-only, lowest blast radius. Removes the `MSFT 405/400 (×3 fires) → +100% target_hit` from the daily report.
3. Flip `SUPPRESS_SINGLE_ITEM_DIGEST_BEFORE_FULL_CARD=1` — also display-only.
4. Flip `USE_TRADING_DAYS_FOR_CREDIT_DTE=1` — fixes Friday-with-weekend DTE picking the wrong expiry.
5. Flip `SCORER_REPOST_GATE_ENABLED=1` with cooldown=45 (not 60). Phase 2.5c moves the gate to post-build, so a same-score+same-wave fire that would produce a different vehicle is no longer at risk of being falsely suppressed.
6. Flip `BURST_FIRST_ROUTING_ENABLED=1` last. Burst cards now record to the recommendation tracker so we'll have data after a week.

If anything goes sideways, unset the offending env var and restart the worker. No code rollback needed.

---

## What each phase fixes

### Phase 2.5 — Repost noise
**Before:** Same scorer signal (ticker + bias + wave_label) re-fires every ~5 min for hours. SOXX BULL conv-72 fired 10x on 4/30, MSFT 405/400 fired 3x within 2 hours on 5/01.

**After:** Repost suppressed unless (a) cooldown has elapsed AND score jumped ≥ delta, OR (b) wave label upgraded into a confirm/burst state. Production replay against 5/01 messages: 26 scorer cards → 8 posted, 18 suppressed (69% reduction), zero false suppressions. Suppressed reposts logged to `scorer_suppressed_reposts.csv` for audit.

### Phase 2.5b — Vehicle-aware gate / post-success mark / burst tracking
**Before (issues found in Phase 2.5 review):**
1. Gate keyed on `ticker+bias+score+wave_label`. The MSFT 5/01 sequence (V2 said burst=YES at 10:38 but rejected vehicle, no card posted; legitimate retry at 13:19 should be allowed) would still suppress because score+wave were unchanged.
2. `_mark_scorer_posted` was called BEFORE the credit/burst card actually posted, so a Telegram outage / chain fetch failure / builder rejection still left the bot believing it had posted, suppressing legitimate retries.
3. Long Call Burst card posted to Telegram but never wrote to the recommendation tracker, so per-burst performance was unknowable.

**After:**
1. Gate snapshot now stores `vehicle_kind` (`credit` / `burst` / `none`) and `vehicle_id` (e.g. `MSFT_405.0_400.0_2026-05-04`). Gate suppresses only when current vehicle == prior vehicle. New strikes / new expiry / burst↔credit transition / "prior posted nothing" all explicitly allowed.
2. `_mark_scorer_posted` moved from before the post path to after, called only when at least one card actually posted to Telegram.
3. Burst cards write to the tracker with `source="v84_long_call_burst"`, `extra_metadata={"review_only": True, "momentum_burst": True, "v2_model": True, "burst_score": ..., "burst_reasons": ..., "trading_dte": ...}` — review-only by default (matches Phase 2.7 v8.4 credit treatment).
4. Default `RECTRACKER_REVIEW_ONLY_SOURCES` extended to `"v84_credit_dual_post,v84_long_call_burst"` so the Phase 2.7 filter hides both bot sources by default.

7 verification scenarios pass: prior-fire-posted-nothing → allowed; vehicle changed (strikes or kind) → allowed; identical vehicle within cooldown → suppressed; wave-upgrade regression intact; cooldown-elapsed-score-jump regression intact; pre-build call (no current vehicle_id) → falls through correctly; burst record + Patch Q default → review-only filter catches by source name.

### Phase 2.5c — True vehicle-aware gate (post-build suppression)
**Before (issue found in Phase 2.5b review):**
The 2.5b helper supported `current_vehicle_kind` and `current_vehicle_id` parameters, but the actual `_process_job` call site still ran the gate before the vehicle was known — passing empty vehicle info. That meant the `vehicle_changed` allow path was unreachable from the pre-gate, and a same-score+same-wave fire that would have produced a different vehicle (different strikes, different expiry, or burst-vs-credit transition) could still be suppressed before the bot discovered the new vehicle.

**After:**
- Pre-gate at `_process_job` REMOVED. No suppression check runs before the vehicle is built.
- Gate now runs at three concrete post sites where a stable `vehicle_id` exists:
  - `_post_v84_credit_card` — vehicle_id = `"TICKER_SHORT_LONG_EXPIRY"`, vehicle_kind = `"credit"`
  - `_try_post_long_call_burst` — vehicle_id = `"TICKER_STRIKEC_EXPIRY"`, vehicle_kind = `"burst"`
  - signal-only path in `_process_job` post-`check_ticker` — vehicle_id = `"TICKER_BIAS_signal_only_T{tier}_{wave}"`, vehicle_kind = `"signal_only"`
- When a post-site gate suppresses, the post fn returns `("suppressed", reason)`. `_process_job` sees this and bails before `check_ticker` can post a redundant signal-only card.
- Signal-only cards are now also deduped (was previously not addressed — repeated SOXX-style "NO STANDARD SPREAD" cards).

**Patch S — signal-only suppression when real vehicle already posted:**
The 2.5c base let the signal-only block run unconditionally after the credit/burst post path. Two failure modes when both fired:
1. UX: user saw a v8.4 CREDIT card AND a "NO STANDARD SPREAD / FIND BETTER VEHICLE" card for the same fire — contradictory.
2. Snapshot integrity: signal-only's mark overwrote the credit/burst vehicle_id, so the next identical credit fire saw `vehicle_changed` (signal_only → credit) and was allowed — defeating Phase 2.5 spam suppression.

Fix: at the top of the signal-only block, if `_vehicle_kind in ("credit", "burst")` for this event, skip both the post AND the mark. The credit/burst snapshot from the earlier path is the source of truth. `_vehicle_kind`/`_vehicle_id` are initialized at job-level scope so the signal-only block can read them whether or not the scorer block ran.

This protects:
- `no_vehicle → valid_vehicle` (MSFT-style late-vehicle case)
- `credit_A → credit_B` (different strikes/expiry on same name)
- `credit → burst` (vehicle-kind transition)
- `signal_only → real vehicle` (signal escalates to a real card)

While still suppressing:
- Identical credit spread strikes/expiry within cooldown (the SOXX-style spam)
- Identical burst strike/expiry within cooldown
- Identical signal-only ticker/bias/tier/wave within cooldown
- Signal-only post entirely when a real credit/burst card already posted for the same scorer event (Patch S)

Cost trade-off: builders now run for SOXX-style repeats before being suppressed at post-site. Chain fetches are cached (TTL ~5min), so the additional cost per duplicate is one builder pass — a few function calls. Acceptable given the false-suppress risk it eliminates.

8 verification scenarios pass: (T1) prior posted nothing → first_post → allowed at post-site; (T2) same vehicle within cooldown → duplicate_within_cooldown → suppressed at post-site; (T3) signal-only vehicle_id namespace-separated from credit/burst (signal_only → credit on same name allowed via vehicle_changed); (T4) repeated signal-only fires suppressed; plus 4 Patch S scenarios that reproduce the reviewer's bug without the patch and verify the fix with it.

### Phase 2.6 — Burst routing + DTE picker
**Before:** When V2 5D edge model said `momentum_burst_label=YES`, the bot still posted a v8.4 BULL PUT credit spread as the headline with a "long call may fit better" hint underneath. No actual long call card with strike/price was ever generated. Separately, the credit-spread DTE picker counted calendar days, so Friday-to-Monday looked like 3 DTE when it's really 1 trading day — bot picked thin 5/04 credit ($0.32) instead of the real 5/08 income window ($1.05).

**After:** Burst-first routing — when V2 says burst=YES AND a liquid 0–2 trading-DTE ATM call exists, post a `🚀 LONG CALL BURST` card as the headline and skip the credit card. Replay output for MSFT 5/01:
```
🚀 LONG CALL BURST — MSFT $415.00C
Conv 71/100 | Spot $415.83 | Strike $415.00 (-0.2% OTM)
Debit: $2.28 (bid $2.20 / ask $2.35) [📡 live]
BE: $417.28 | Exp: 2026-05-04 (1d)
Burst 6/10: reclaim/break from structure | above VWAP | MTF full_aligned | ADX 30+ | scorer 71/100
🎯 Target: +50% | 🛑 Stop: -40% | ⏰ Time stop: next session close
```
For the credit DTE bug — when `USE_TRADING_DAYS_FOR_CREDIT_DTE=1`, the picker uses `count_trading_days_between()` from `market_clock.py`. Friday-with-weekend-attached now correctly classifies Mon as 1 trading-DTE (not 3 calendar). 7 integration tests + 8 builder tests all pass against real production webhook data.

### Phase 2.7 — Review-only filter
**Before:** Morning briefing showed v8.4 CREDIT review-only records under "Currently Open" with `$0.00` movement. Daily report graded `MSFT BULL PUT 405.0/400.0 (×3 fires) → +100% target_hit` even though the bot's posted spread was never traded — the bot self-graded its review card and corrupted the win-rate stat.

**After:** When the filter is enabled, records flagged review-only (or with a source name in the configured CSV list) are hidden from the three display reports AND excluded from win-rate / net-PnL math. Records still persist; campaigns still grade in the background; the data is preserved for backtest/audit. Forward records get an explicit `extra_metadata.review_only=True` flag at write time (Patch M); legacy records match by source-name fallback. Future `/confirm` command can flip `extra_metadata.confirmed_entry=True` to override the filter for an actual entry.

---

## Files in this deploy

```
app.py                        14,371 lines  (was 13,961 pre-2.5 / 14,048 post-2.5)
long_call_burst_builder.py       311 lines  NEW
market_clock.py                  244 lines  (was 195, +49 for count_trading_days_between)
recommendation_tracker.py      1,751 lines  (was 1,638, +113 for filter helpers)
```

Header blocks in `app.py` document Phases 2.5, 2.6, 2.7 in order with rollback notes.

## Rollback paths

- **Whole deploy bad:** revert all four files to pre-deploy git SHA. No DB migrations to undo.
- **One phase causing issues:** unset its env var(s), restart worker. No code revert needed since defaults are off.
- **Specific issue with Phase 2.6 strike picker:** tune via `BURST_OTM_BAND_LOW` / `BURST_OTM_BAND_HIGH` / `BURST_MAX_BID_ASK_SPREAD_FRAC` without redeploying.
- **Want review-only records back in reports temporarily:** unset `RECTRACKER_FILTER_REVIEW_ONLY_FROM_REPORTS` (instant, no restart needed if env is read on each call — it is).

## Test coverage summary

| Phase | Test type                      | Cases | Result        |
|-------|--------------------------------|-------|---------------|
| 2.5   | Production replay (5/01 msgs)  | 26    | 8 / 18 split  |
| 2.5   | Isolation                      | 11    | All pass      |
| 2.6   | Builder isolation              | 8     | All pass      |
| 2.6   | Trading-day counter            | 8     | All pass      |
| 2.6   | Routing integration (MSFT 5/01)| 7     | All pass      |
| 2.7   | Filter on/off behavior         | 6     | All pass      |
| 2.7   | _is_review_only_record units   | 7     | All pass      |

Total: 71 tests across the three phases. AST-clean on all four files.
