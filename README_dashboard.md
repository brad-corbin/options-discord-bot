# DASHBOARD 3000 — README (v8.2)

## What this is

A read-only aggregator that writes a live per-ticker state view to a dedicated Google Sheet every 60 seconds. It also appends an event row to a Signal Log tab each time a signal transitions (new flow, new OI, new Potter Box break, new active scanner tier, thesis bias change, **position close**).

**v8.2 additions on top of Phase 1:**
- **Position PnL tab** — one row per tracked option position, with peak PnL % lifetime, 2:45 PM CT close-price snapshot, peak during hold, and final on close.
- **PnL rollup columns on the Dashboard tab** — per-ticker best peak %, best 2:45 %, best hold peak % across all that ticker's positions.
- **Automatic 2:45 PM CT snapshot** — each position's option PnL % gets captured at/after 14:45 CT on its expiry day. Redis-backed, write-once per campaign, survives bot restarts.
- **Position-close events** in the Signal Log — every time a tracked position transitions to closed, a row lands with final PnL, peak, 2:45, and reason. Outcome column stays blank for manual tagging.

**It does not change how the bot trades.** It does not post to Telegram. It does not write outside its own Redis namespace (`dashboard:close_245:*`). It does not touch the existing `GOOGLE_SHEET_ID` Sheet. It is purely additive observability.

The point is to give you one place to see everything the bot observes and one place to see how every position actually played out, and to build a historical log of signal events that you can retrospectively tag with outcomes — so confluence scoring can be grounded in data instead of intuition.

## How it works

At boot, `app.py` calls `dashboard.init_dashboard(...)` which wires the module to the bot's existing Redis stores (`_persistent_state`), the recommendation tracker (`_rec_tracker`), spot lookup (`get_spot`), Google Sheets auth (`_get_google_access_token`), ticker list (`FLOW_TICKERS`), and regime detectors (`get_regime_package`, `vix_term_structure.get_vix_term_structure`).

If `DASHBOARD_ENABLED=1` and `DASHBOARD_SHEET_ID` is set, the background services starter launches a thread named `dashboard` that runs a 60s loop:

1. Fetches a Sheets access token (cached, shared with the main Sheets writer)
2. Ensures the `Dashboard`, `Signal Log`, and `Position PnL` tabs exist (auto-creates on first run)
3. Pulls **every active and recently-closed (≤30 days) position** from `_rec_tracker` once
4. For each position: if it's 14:45 CT or later on that position's expiry day and no 2:45 snapshot exists yet, captures one to Redis (`dashboard:close_245:{campaign_id}`, TTL 90 days)
5. Builds a per-ticker rollup (best lifetime %, best 2:45 %, best hold peak %) across the positions
6. For each ticker in `FLOW_TICKERS`:
   - Reads Potter Box, OI flags, flow direction, active scanner tier, thesis, GEX sign, gamma flip
   - Reads open campaigns from the recommendation tracker
   - Counts bullish vs. bearish signals and computes a net direction
   - Pulls the PnL rollup for that ticker
   - Detects transitions since the previous tick and queues signal-log events
7. Detects positions that transitioned to closed this tick (previously active → now graded) and queues `position_close` events
8. Overwrites the `Dashboard` tab with the fresh rows (one row per ticker)
9. Overwrites the `Position PnL` tab with the fresh rows (one row per position)
10. Appends new signal-log events (if any) to the `Signal Log` tab

## Environment variables

| Var | Required | Default | What it does |
|---|---|---|---|
| `DASHBOARD_ENABLED` | yes | `0` | Set to `1` to enable. Anything else and the writer thread never starts. |
| `DASHBOARD_SHEET_ID` | yes | — | The Google Sheet ID (the long string in the URL between `/d/` and `/edit`). Must be shared with `bot-sheets-writer@corbin-bot-tracking.iam.gserviceaccount.com` as Editor. |
| `DASHBOARD_INTERVAL_SEC` | no | `60` | Tick cadence in seconds. Minimum is 30. |

There are no separate env vars for the PnL work — it follows the same on/off switch as the rest of the dashboard.

## Turning it on / off

**On:** set `DASHBOARD_ENABLED=1` and `DASHBOARD_SHEET_ID=<your sheet id>` in Render, redeploy.

**Off:** unset `DASHBOARD_ENABLED` (or set to `0`) in Render, redeploy. The thread stops on next restart. No other side effects.

**Emergency off (no redeploy):** the dashboard thread fails gracefully if Sheets is unreachable — it logs a warning and retries. If after 5 consecutive failures it's still failing, it backs off to a 5-minute cadence instead of 60s, so it won't spam your logs or your Sheets quota.

## What you'll see

### `Dashboard` tab

Row 1 is the header. Rows 2+ are one per ticker, refreshed every 60s.

Columns through "Best MFE" are unchanged from Phase 1. **New in v8.2:**

| Column | What it means |
|---|---|
| Best Peak PnL% | Highest lifetime peak option PnL% across all this ticker's tracked positions (active + closed in last 30 days) |
| Best 2:45 PnL% | Highest 2:45 PM CT snapshot PnL% across this ticker's positions. Blank if no expiry day snapshots yet. |
| Best Hold Peak% | Highest peak-during-hold PnL% across this ticker's positions. Equal to Best Peak PnL% in the current bot (tracker stops at grade); kept as a separate column for forward compat if post-close tracking is added later. |

The remaining columns (Signals Bullish, Signals Bearish, Net Direction, Updated) moved over by three positions. If you had conditional formatting set up, update the column references after the first v8.2 write.

Suggested conditional formatting (do this once in the Sheet):
- Highlight whole row green when `Net Direction = bullish`
- Highlight whole row red when `Net Direction = bearish`
- Bold the ticker when `Open Campaigns > 0`
- Color-scale the `Best Peak PnL%` column (green for positive, red for negative)

### `Position PnL` tab (new in v8.2)

Append-on-new, update-in-place while active, values lock on close. One row per tracked position.

| Column | What it means |
|---|---|
| Opened CT | `YYYY-MM-DD HH:MM` in Central |
| Ticker | Symbol |
| Campaign ID | Tracker's primary key. Matches what the recommendation tracker uses internally. |
| Structure | `long_call` / `long_put` / `bull_call_spread` / `bear_put_spread` / etc. |
| Legs | Compact format: `+165C 250516 / -170C 250516` (sign + strike + right + yymmdd) |
| Side | `bull` / `bear` |
| Strike | Primary (buy-leg) strike |
| Entry Premium | Entry option mark (or net debit for spreads) |
| Peak Premium | Highest option mark seen since entry (live while active) |
| Peak PnL% Lifetime | `(peak_premium - entry_premium) / entry_premium` × 100 |
| 2:45 PnL% | Snapshot at 2:45 PM CT on the position's expiry day (or last hold day if closed earlier). Blank until captured. |
| Peak PnL% During Hold | Peak PnL% over the window the user was holding. Equal to lifetime for now; see Best Hold Peak% note above. |
| Closed CT | `YYYY-MM-DD HH:MM`, blank while active |
| Close Premium | Exit option mark, blank while active |
| Current PnL% | Live PnL% if active, final PnL% if closed |
| Status | `open` / `closed` |

Each tick, the tab is fully overwritten with the current state of all positions from the last 30 days (active + closed). That means:
- Peak Premium and Current PnL% update in place as the position moves
- 2:45 PnL% lights up automatically on the expiry day around 14:45 CT
- When a position closes, Closed CT / Close Premium / Status fill in and the row stops moving

Rows are ordered oldest-first (newest at bottom) so the active/recently-closed stuff is visible without scrolling.

### `Signal Log` tab

Append-only. One row per signal transition. Columns unchanged from Phase 1: Date, Time CT, Ticker, Signal Type, Direction, Detail, Market Regime, Vol Regime, Dealer Regime, VIX, Outcome, Notes.

**v8.2 adds one new signal type: `position_close`.** When a position transitions from active to closed, one row lands with:

- `Signal Type` = `position_close`
- `Direction` = bullish / bearish (from the position's direction)
- `Detail` = `closed <structure> entry=$X exit=$Y final=+Z% peak=+A% 245=+B% hold_peak=+C% reason=<tracker's exit_reason>`
- `Outcome` and `Notes` stay blank for your manual tag

**Bootstrap note:** on the very first dashboard tick after a bot restart, previously-closed positions that were already graded before the tick are **not** emitted as `position_close` events. Only transitions observed by the running dashboard fire events. This keeps the Signal Log from spamming retroactive closes on every restart.

## Signal counting logic

Unchanged from Phase 1. Phase 1 uses a simple directional-vote count:

- Flow direction: +1 bullish or +1 bearish
- OI flag:
  - buildup on calls → +1 bullish
  - buildup on puts → +1 bearish
  - unwind on calls → +1 bearish (positions closed = losing confidence)
  - unwind on puts → +1 bullish (covering puts = losing bearish conviction)
- Thesis bias: +1 in its stated direction
- Potter Box: `above` roof → +1 bullish (breakout); `below` floor → +1 bearish
- Active scanner: 🐂 → +1 bullish, 🐻 → +1 bearish

Max possible: 5 bullish or 5 bearish. This is intentionally naive — the goal is observation, not filtering. Weights will be tuned from real data (including the new PnL data in v8.2) later.

## Logging and how to audit

The dashboard writes to the Python logger at the `dashboard` logger name (inherits from root). Grep patterns in Render logs:

| Grep | Shows |
|---|---|
| `grep dashboard` | all dashboard activity |
| `grep "dashboard: wrote"` | successful ticker writes with regime context |
| `grep "dashboard: appended"` | signal log events written |
| `grep "dashboard: wrote .* position pnl rows"` | Position PnL tab writes |
| `grep "dashboard: 2:45 snapshot captured"` | 2:45 snapshots as they land (one line per position captured) |
| `grep "dashboard: detected .* newly-closed"` | position-close events being emitted |
| `grep "dashboard: position-close detection bootstrapped"` | first-tick silence on pre-existing closes |
| `grep "dashboard: .*failed"` | failures (Sheets errors, Redis misses, etc.) |
| `grep "dashboard: tick error"` | loop-level exceptions (with consecutive count) |
| `grep "dashboard: .*skip"` | skipped ticks (no token, no tickers, no tabs) |
| `grep "dashboard: init"` | startup and config |

If the dashboard appears silent, check in order:
1. `grep "dashboard: initialized"` — is init succeeding? If not, `DASHBOARD_ENABLED` or `DASHBOARD_SHEET_ID` is wrong.
2. `grep "dashboard: writer thread started"` — did the thread actually start?
3. `grep "dashboard: no sheets token"` — auth failure; check `GOOGLE_SERVICE_ACCOUNT_JSON` or the default file
4. `grep "dashboard: no tickers"` — `FLOW_TICKERS` is empty (shouldn't happen; investigate `oi_flow.py`)
5. `grep "dashboard: tab .* not ready"` — service account probably doesn't have Editor on the Sheet

If PnL-specific things look broken:
- Position PnL tab empty but Dashboard is writing → check `grep "dashboard: Position PnL tab not ready"` or `grep "dashboard: position pnl write failed"`. Most likely a Sheets permissions or quota issue.
- 2:45 snapshots never capture → check position expiries. The snapshot window is 14:45 CT ONLY on the position's expiry day. If you're on the wrong day, snapshots correctly don't fire.
- Dashboard rollup columns empty → no positions exist for those tickers, or the snapshot hasn't fired yet for 2:45 %.

## Failure modes (designed behavior)

| Situation | What happens |
|---|---|
| Sheets unreachable | `log.warning`, skip that tick, retry next tick |
| Service account can't write to Sheet | Tab creation fails, `log.warning`, dashboard stays silent (won't crash) |
| Position PnL tab can't be created | `log.warning`; Dashboard tab still writes, rollup columns still work, Position PnL tab just stays empty |
| Redis miss reading a 2:45 snapshot | Field blank in the row; next tick retries naturally |
| Redis write fails for 2:45 snapshot | `log.warning`; next tick will attempt again (snapshot is write-once, so a failed attempt doesn't lock the key) |
| Redis miss for a single ticker's data | That field is empty in the row; other fields render normally |
| A specific read function throws | `log.debug` (suppressed at INFO level), that field is empty, row still writes |
| Campaign record missing `legs` | Legs column blank, strike blank; everything else still populates |
| 5 consecutive tick failures | Backs off to 5× cadence (default 5 min), keeps trying |
| `DASHBOARD_ENABLED=0` | Thread never starts, no writes, no log spam |
| Dashboard code has a bug that raises in the loop | Caught at loop level, `log.warning` with error, next tick proceeds normally |

**Key property:** nothing the dashboard does can bring down the bot or affect trading. It runs in a daemon thread with every read wrapped in try/except, every write wrapped in try/except, and a top-level catch around the loop body itself.

## Redis namespace

v8.2 introduces one Redis key prefix:

- `dashboard:close_245:{campaign_id}` — write-once 2:45 PM CT snapshot per campaign. TTL 90 days. Payload: `{pnl_pct, captured_ts, captured_ct, entry_mark, mark_at_snapshot, ticker, campaign_id}`.

Nothing else in the bot reads these keys; deleting them is safe (the dashboard will re-capture on the next 2:45 CT window for any still-active positions). The main Redis namespace used by the trading pipeline is untouched.

## What's NOT in v8.2 (still deferred to later phases)

- **%Day column** — needs daily candle cache. Left blank to avoid another data dependency.
- **`/audit <TICKER>` Telegram command** — later phase.
- **Historical confluence WR analysis** — later. Needs ~3-4 weeks of Signal Log data with Outcome tagged.
- **Open-trade tracking sections (<5 DTE and 6+ DTE) in a dedicated Telegram post** — later. The Position PnL tab covers the same ground in the Sheet for now.
- **0DTE journal tab matching the existing format** — later.
- **Regime-conditional signal counting** — later. Current logic ignores regime; it's captured on every Signal Log row for later analysis.
- **Per-close "would have held to X" analysis** — the 2:45 snapshot feeds this, but the math/visualization isn't in v8.2.
- **Post-close tracking of premium** (to make Peak During Hold distinct from Peak Lifetime) — tracker currently stops polling at grade. Revisit if/when that changes.

## Render resource impact

Measured expectations including v8.2 additions:

- **Memory:** +35-60 MB (cached snapshots, position records, gspread client state, thread locals)
- **CPU:** negligible baseline, one small spike every 60s during Sheets I/O + Redis reads
- **Sheets API:** ~5-8 write calls per minute (Dashboard clear+write, Position PnL clear+write, occasional header/tab ops, Signal Log appends). Still well under Google's 60 writes/minute per-user quota.
- **Redis:** ~1 write per new 2:45 snapshot (rare, usually a few per day at expiry), ~N reads per tick where N = total positions in last 30 days. Negligible.
- **Network:** ~30-60 KB out per tick

You're at 15% memory / ~0% CPU baseline. This module adds <3% of capacity either way.

## Operational checklist for first deploy

1. Confirm `DASHBOARD_SHEET_ID=1v9UN6qoTWdFJWe332qfnrvJwMF88JavN-MteHL66Iv8` is set in Render env
2. Confirm `DASHBOARD_ENABLED=1` is set in Render env
3. Confirm the Sheet is shared with `bot-sheets-writer@corbin-bot-tracking.iam.gserviceaccount.com` as **Editor**
4. Deploy the three files (`dashboard.py`, `app.py`, `rate_limiter.py`)
5. Watch logs for `Dashboard 3000: writer thread started`
6. After the first full interval (default 60s), check the Sheet — you should see three tabs: `Dashboard`, `Signal Log`, and `Position PnL` auto-created with headers, and the Dashboard tab populated with ticker rows.
7. After the next tick with any tracked position, the Position PnL tab should have one row per position.
8. On the next expiry day at/after 14:45 CT, the 2:45 PnL% column will start populating.
9. If something looks off, grep the patterns above before changing anything.

## Rollback

All changes are reversible without data loss:

- Set `DASHBOARD_ENABLED=0` in Render env and redeploy. Thread stops, writes stop, the Sheet keeps whatever it last had.
- The `dashboard.py` file can be reverted to the Phase 1 version; `app.py` will log a warning about the new PnL attributes being missing, then continue normally. The Dashboard tab will have three extra columns with stale data — clear them manually or accept the blanks.
- Redis keys in `dashboard:close_245:*` can be deleted safely. They're only read by the dashboard module.

## Known limitations (unchanged from Phase 1)

- **Signal transitions only.** The Signal Log writes when a value *changes*. If the bot is restarted mid-session, the first tick after restart may re-emit some directional events as "new" because prior state isn't persisted across restarts. Acceptable; position-close events are specifically bootstrapped silent so this doesn't apply to them.
- **No historical backfill.** Past signals or past position closes from before the dashboard came online aren't reconstructed.
- **`trade_journal` freshness.** The Active Scanner tier column reads from `trade_journal`. If the journal isn't being populated, this column stays blank.
- **`%Day` blank.** Will be added once the daily-candle integration is wired.
- **30-day lookback.** Positions closed more than 30 days ago drop off the Position PnL tab and stop contributing to the Dashboard rollup. Tune in `_all_relevant_positions()` if you want a wider window.
