# Phase 2 — Data Durability

Snapshots, audit log, and restore. Built on the Phase 1 framework.

## What ships in this phase

- **Daily snapshot job** — full portfolio state captured to a dated Sheets tab nightly
- **Manual snapshot** — "Snapshot Now" button on the Durability page for on-demand
- **Restore page** — `/restore` (linked as "Durability" in the nav)
- **Restore action** — overwrite Redis from any historical snapshot, with confirmation modal
- **Audit log** — Phase 4 portfolio writes will append here automatically; Phase 2 already logs all restores
- **30-day rolling retention** — snapshots older than 30 days auto-pruned

## File drop

Same pattern as Phase 1 — replace your existing `dashboard/` folder with this one,
or merge the new files into it:

```
your-bot/
├── app.py
└── dashboard/
    ├── __init__.py            ← updated
    ├── routes.py              ← updated
    ├── durability.py          ← new
    ├── scheduler.py           ← new
    ├── templates/dashboard/
    │   ├── base.html          (unchanged)
    │   ├── login.html         (unchanged)
    │   ├── command_center.html  ← updated (roadmap reflects Phase 2)
    │   ├── trading.html       (unchanged)
    │   ├── portfolio.html     (unchanged)
    │   ├── diagnostic.html    (unchanged)
    │   └── restore.html       ← new
    └── static/
        └── omega.css          ← updated (Phase 2 styles appended)
```

## No `app.py` changes needed

The 3 lines you added in Phase 1 still do the right thing. The new `durability`
and `scheduler` modules late-bind to `app.py` helpers at function-call time, so
nothing extra needed.

## Optional new env vars

Three new env vars, all optional. Defaults are sensible.

| Key | Default | What it does |
|---|---|---|
| `OMEGA_SNAPSHOT_TIME_UTC` | `06:00` | UTC time the daily snapshot fires (default ≈ 1am Central) |
| `OMEGA_SNAPSHOT_RETENTION_DAYS` | `30` | How many daily snapshot tabs to keep before auto-pruning |
| `OMEGA_SNAPSHOT_SCHEDULER_ENABLED` | `1` | Set to `0` if you want to drive snapshots externally (cron, manual only) |

The defaults work for everyone. Skip these unless you want to change something specific.

## How it uses the existing Sheets infrastructure

This phase reuses the bot's existing Google Sheets auth. No new credentials.
Specifically it uses:

- `GOOGLE_SHEET_ID` — already set
- `GOOGLE_SHEETS_ENABLE` — already set
- `GOOGLE_SERVICE_ACCOUNT_JSON` — already set

If those work for your existing flow (signal_decisions, em_predictions tabs etc),
they work for snapshots too.

## What gets snapshotted

For each known account (`brad` and `mom` currently — Phase 4 will add more):

- **Holdings** — every share position with cost basis and tags
- **Options** — every CSP, CC, long, spread leg with current status
- **Spreads** — all open and closed spread positions
- **Cash** — running cash balance and any deposits/withdrawals tracked

Format in the snapshot tab is one row per data record:
```
section    | account | key       | value_json                        | captured_at
holdings   | brad    | AAPL      | {"shares":100,"cost_basis":175}   | 2026-05-03T06:00:00Z
options    | brad    | opt_1     | {"id":"opt_1","ticker":"AAPL"...} | 2026-05-03T06:00:00Z
cash       | brad    | data      | {"balance":50000}                 | 2026-05-03T06:00:00Z
```

This format is human-readable — you can open the snapshot tab in Sheets and see
exactly what your portfolio looked like at that point in time.

## Restore flow

1. Visit `/restore` (or click "Durability" in the top nav)
2. See the list of available snapshots, newest first
3. Click "Restore →" on any snapshot
4. Modal opens — type `RESTORE` to confirm
5. Click Restore — overwrites Redis from that snapshot, logs the event to audit

The current state is **not** automatically backed up before a restore. The
expectation is: you don't restore unless something is wrong. If you want a
safety net before restoring, click "Snapshot Now" first — that captures the
current (broken) state to a tab you can restore back to if needed.

## Verifying after deploy

Hit the health endpoint:
```
https://options-discord-bot.onrender.com/dashboard/health
```

Should return:
```json
{
  "status": "ok",
  "module": "omega-dashboard",
  "phase": 2,
  "auth_configured": true,
  "durability": {
    "sheets_available": true,
    "portfolio_available": true,
    "store_initialized": true,
    "snapshot_count": 0,
    "retention_days": 30
  }
}
```

All four `durability` flags should be `true`. If `sheets_available` is `false`,
check that your existing `GOOGLE_SHEETS_ENABLE=1` is set. If `store_initialized`
is `false`, the bot hasn't called `portfolio.init_store()` yet — wait a minute
for full startup.

After confirming health, log into the dashboard, click **Durability** in the nav,
click **Snapshot Now** — you should see a green flash message and a new entry in
the Available Snapshots list.

## What this protects

The data wipe scenario: a bad deploy overwrites or zeroes Redis. Without
Phase 2, that data is gone. With Phase 2, the most recent snapshot is at most
24 hours old — and you can restore from any of the last 30. Worst case loss: one
day of activity.

## What's next

**Phase 3 — Read-only Views.** Command Center and Trading view both light up
with live data from Redis and the bot's existing background processes. No writes
yet (those are Phase 4). The plumbing built in Phase 2 (late-binding to
`portfolio` and `app`) is the same pattern Phase 3 will use to read.
