# Phase 3.6 Deploy — Scorer is authoritative + signal-only cards

**Single file. Drop-in replacement for `app.py`.**

## What this fixes

Today (4/23) the scorer wrote 7 `decision=post` rows. Zero Telegram cards landed. After tracing `check_ticker`, the cause was three silent gates that were overriding scorer-post decisions:

1. `_um_apply_effective_regime_gate_to_rec` with `requires_trigger=True` kicking trades to pending when raw regime was UNKNOWN or spot was inside a balance zone
2. `_apply_final_trade_gate` blocking on EV/confidence/win-prob thresholds
3. `"No valid spreads across any expiration"` silent rejection when no standard debit spread could be built

The scorer (v8.3, 572K backtest, +8.75 WR at score≥70) is the only validated gate in this stack. The other two were unvalidated defaults from earlier pipeline epochs and shouldn't override it. Now they don't.

## The four changes in check_ticker

**Patch 1 — Scorer bypasses requires_trigger**
- `has_confirmed_trigger = True` when `conviction_decision == "post"`
- Non-scorer paths (manual `/check`, swing, rechecks) unchanged — they still need structure trigger

**Patch 2 — Final gate becomes warnings for scorer-post**
- `_apply_final_trade_gate` EV/confidence/win-prob checks no longer block scorer-post
- Failures stash on `best_rec["final_gate_warning"]`; trade flows through
- Non-scorer paths still hard-gate

**Patch 3 — No valid spreads → signal-only card**
- For scorer-post: build card with signal context + vehicle hints, post to main channel
- Banner: `🔍 SCORER SIGNAL — NO STANDARD SPREAD / FIND BETTER TRADE VEHICLE`
- Journal outcome: `signal_no_vehicle` (new, tracked in trade_journal + dataset)
- Non-scorer paths still return silent rejection

**Patch 4 — New helpers**
- `_vehicle_hints_from_context` — maps scorer reasons + vol regime + spread-failure reasons to 2-4 vehicle suggestions
- `_build_signal_only_card` — formats the full signal card for no-vehicle cases

## Fixes applied on top of the four patches

**Fix 1 — Digest line cosmetic**
- `_process_job` propagates `signal_only=True` from check_ticker return onto the wave result
- `_flush_wave_digest` uses a distinct digest line for signal-only cards:
  - Real trade: `✅ TICKER T2 🐂 72/100 — POSTED ⬆️`
  - Signal-only: `🔍 TICKER T2 🐂 72/100 — SIGNAL ONLY ⬆️ (find vehicle)`

**Fix 2 — Audit dedup on exception path**
- Previously: on card-build failure, the fallthrough wrote two audit rows per signal (`signal_no_vehicle` + `rejected`)
- Now: build card FIRST, only write the `signal_no_vehicle` audit row on success. On failure, fall through to the original `rejected` path. One signal always produces exactly one audit row.

## What stays hard-gated (unchanged)

- `is_duplicate_trade` dedup window (no spam)
- `_validate_live_signal` STALE_SIGNAL and HARD_BLOCK
- Scorer G1/G2/G3 hard gates (the actual ruleset)
- v7 filter, CAGF gate, PIN/CHOP regime gate, pre-chain gate

## Vehicle hint heuristics (what the card will suggest)

| Scorer reason | Hint |
|---|---|
| B8 blue-sky (bull, elevated vol) | Long call with tight stop — spreads drain theta fast |
| B8 blue-sky (bull, normal vol) | Long call or shares — no clean short strike |
| B8 capitulation (bear) | Long put or short shares |
| B1 breakout imminent | Wait for confirmed break, then shares or ITM directional |
| B9 rejection (bear) | Long put — limited upside for put spread |
| B7 near-pivot | Shares or ITM long call/put — spread width too narrow |
| (other) | Review chain — standard spread not viable |

Plus secondary hints from spread-failure reasons (OI/liquidity, width/ROR, ITM strikes) and vol regime warnings.

## Deploy

Single file. Drop-in.

```
git add app.py
git commit -m "Phase 3.6: scorer authoritative + signal-only cards"
git push
```

Render auto-redeploys.

No env var changes. No schema changes. No new dependencies.

## What to watch after deploy

Next scorer-post signal (SOXX / NVDA / QQQ / AMD at 72 with B7+B8) — expect one of:
1. Full trade card posts to main channel (if spread builds and all gates pass as warnings), digest shows `✅ POSTED ⬆️`
2. `🔍 SCORER SIGNAL — NO STANDARD SPREAD` card to main channel (if no spread builds), digest shows `🔍 SIGNAL ONLY ⬆️ (find vehicle)`

In the Render log, look for:
- `[scorer-post] {ticker} final_gate demoted to warning` — Patch 2 firing
- `[scorer-post] {ticker} signal-only card built (no spread)` — Patch 3 firing
- `TV winner queued for digest` — the new path successfully queuing

## Rollback

Revert the file. That's it. No state changes to undo.

## Pre-flight verification (all passed)

- Line count: 13,444 (was 13,167 pre-3.6)
- `python3 -c "import ast; ast.parse(open('app.py').read())"` → OK
- Prior-phase markers preserved:
  - 3.1rem `_continuous_post_gate`: 3 ✓
  - 3.1rem `countback=80`: 3 ✓
  - 3.2 `spot_at_callout`: 5 ✓
  - 3.4 `retrying...nearest future expiry`: 1 ✓
  - 3.4 `Silent thesis loop exception`: 1 ✓
  - 3.5 `scorer_audit`: 12 ✓
  - 3.5.1 `_diagnostics`: 3 ✓
- Phase 3.6 patch sites:
  - `_scorer_post_auth` (Patch 1, line 7206): present
  - `_scorer_post_final` (Patch 2, line 7279): present
  - `_scorer_post_nospread` (Patch 3, line 7086): present
  - `_build_signal_only_card` (Patch 4, line 6861): present
  - `_vehicle_hints_from_context` (Patch 4): present
- Phase 3.6 fix sites:
  - Fix 1 digest line (line 3445): present
  - Fix 1 base propagation (line 4720): present
  - Fix 2 reorder (line 7086-7120): verified — build before audit write

## Known issues still not addressed (not in this patch)

- AMD score=72 three times on 4/23 all decision=log_only (not post) → this is the scorer logging path, not touched. Still needs diagnosis (likely `CONVICTION_POST_THRESHOLD` env override or journal win-rate suppression).
- Scorer_audit endpoint still reads from ephemeral disk — Phase 3.6.1 or Phase 3.7 (persistent disk OR Sheets-backed).
- Conviction re-fire every 8 min (Gap #4), duplicate FLOW/CONVICTION posts (Gap #5), exit signal re-fires (Gap #6) — all separate.
