# Omega 3000 — Phase 4.5 Deploy Notes

Deploy is the same as Phase 4: drag this `omega_dashboard/` folder into the repo, replacing the existing one. No `app.py` changes needed — the blueprint registration from Phase 4 still works.

## Recommended deploy + run order

After deploying the zip, on the dashboard:

1. **Settings → Maintenance → Repair option data** — patches missing fields on legacy closed/assigned options so the YTD calculator counts them correctly.
2. **Settings → Maintenance → Retro-fix wheel assignments** (only if you haven't already) — backfills share lots for past assignments that didn't auto-create them.
3. **Settings → Maintenance → Backfill campaign history** — reconstructs campaign event timelines from the audit log so cards show accurate premium history.

All three are idempotent. Run in order. If you've already done #2, skip it and go to #1 → #3.

## What's new in Phase 4.5

Everything user-visible from this release. Six things plus the data layer.

### 1. Audit log is no longer ambiguous

The `Recent Activity · Undo Last Action` table on the Settings tab now shows the *full* position context — exp, contracts, sub-account, open date — for every entry. Click any row to expand and see the full event JSON. The undo confirmation dialog also shows the full context so you can't accidentally undo the wrong LMND $80 CSP when you have three of them.

### 2. Assignment auto-creates the share lot

When you mark a CSP `assigned`, the dashboard now **automatically buys the 100×N shares at strike** (debits cash via the existing add-holding path, logs everything). Same in reverse for CC called away.

The close form has a new strip below the regular fields:

> ↳ if assigned: buy 100 HOOD @ $109 for $10,900 (debits cash)  [● Auto] [○ Skip]  Override fill $ ___

- **Auto** is the default. One click — no more two-step "mark assigned, then go to Holdings → Add Share Lot".
- **Skip** preserves the legacy behavior (status flips, no shares created) for edge cases.
- **Override fill $** is for the rare case where the assignment filled at a non-strike price.

The auto-handled assignment audits as `option_assigned_with_shares` (or `cc_called_away_with_shares` for CC). Undo reverses BOTH the option close AND the share lot in one shot.

### 3. Wheel campaigns

The Holdings sub-tab now shows an **Active Wheels** panel above your share holdings. Each card shows:

- Ticker · Sub-account · Status (active_csp_phase / active_holding)
- Shares held & weighted cost basis · Effective basis (cost − premium/share)
- Total premium collected · Duration · Open CC/CSP counts

Click a card to expand and see the full event timeline (every CSP, CC, roll, and assignment in the campaign).

When you toggle **Show closed history** on Holdings, you also see a **Closed Wheel Campaigns** section with rollup P&L cards for completed wheels (premium + share P&L = total P&L).

The campaign rules (per Brad):

- Each open CSP starts its own campaign until assigned. After assignment, it merges into the existing share-holding campaign for that ticker + sub-account.
- Campaigns are scoped per **ticker + sub-account** — same ticker in Brokerage vs BC Roth is two campaigns.
- A campaign closes when shares hit zero AND no open puts/calls remain.

Important: campaigns are a **read-only rollup concept**. They never affect cash math, holdings math, or option state. If campaign data ever gets corrupted, deleting the `{account}:portfolio:wheel_campaigns` Redis key won't break anything — the underlying state is unchanged.

### 4. Retro-fix for legacy assignments

On the Settings tab there's a new **MAINTENANCE** section (separate from the danger zone) with a `Retro-fix wheel assignments →` button.

It scans the audit log for past `option_assigned` entries (CSPs marked assigned before Phase 4.5 was deployed) where no matching share lot was added within 7 days, then offers a checkbox preview:

> ☑ LMND CSP $80 ×1 · Volkman Wheel · 2026-04-30  →  buy 100 LMND @ $80 ($8,000 debit) · new campaign

You can uncheck individual rows. Each applied fix:

- Creates the missing 100×N share lot at strike (auto-debits cash via `add_holding`)
- Wires the campaign so it shows up on the Holdings tab
- Logs as `retrofix_assignment` — individually undoable from the audit log

Already-fixed assignments (with matching share lots) are excluded from the candidate list automatically.

### 5. Backfill campaign history

Right after the retro-fix button, there's a `Backfill campaign history →` button. The retro-fix only creates the share lot — the campaign card it produces shows `Premium collected: $0 · 0 days` because the campaign was just born this morning. Hitting **Backfill** walks the audit log for every existing campaign and reconstructs the full event timeline:

- The original `csp_open` event (with the actual premium you collected)
- Any `csp_rolled` events (with the correct net credit on each roll)
- The `csp_assigned` event (already there from retro-fix)
- For closed wheels, the full `cc_open` → `cc_called_away` chain

After backfill, the LMND card shows "Premium collected: $900 · 47 days" instead of "$0 · 0 days", and the closed campaigns rollup actually has accurate P&L. The audit log entries themselves are unchanged — backfill only rebuilds the derived rollup data on the campaigns.

The function is **idempotent** — running it twice produces no additional changes. Safe to re-run any time you suspect campaign data drifted.

Recommended sequence:
1. Run **Retro-fix wheel assignments** once to backfill the missing share lots.
2. Run **Backfill campaign history** once after that to populate the timelines.
3. Going forward, both run automatically on every assignment, so you shouldn't need either button again.

### 6. YTD income calculator (rewritten)

The Command page's "INCOME · ALL REALIZED" hero card now walks the **cash ledger** directly instead of iterating positions. This matches your stated rule: premium is income at collection, BTC without roll is expense at close, roll credits/debits are income/expense in the roll month.

The previous calc had two bugs that were under-counting your YTD:

- **Open positions contributed $0** — even though you'd already collected the premium in cash. Eight open wheel options × ~$700 average premium = ~$5,600 of real income that wasn't showing up.
- **Rolls were being counted as losses** — when you roll a CSP up by closing the old at $X and opening the new at $Y > $X, the position-iteration calc booked the closed leg's `(open_premium − close_premium)` as a realized loss, even though the cash impact was a positive net credit. Your seven May rolls showed "This Month: −$2,163" when you'd actually netted a few hundred dollars.

The new calc sums these cash event types month-by-month: `option_open`, `option_close`, `spread_open`, `spread_close`, `roll_credit`, `roll_debit`. Plus `realized_pnl` from sold share lots. It ignores deposits, withdrawals, share buys/sells (replaced by sold-lot P&L), and transfers — those aren't trading income.

After deploy, your YTD figure will jump significantly upward to match what your cash ledger actually shows. Should land closer to your spreadsheet's $19,739 number.

### 7. Repair option data

On the Settings tab there's a `Repair option data →` button. The YTD calculator skips any option missing `close_date`, `premium`, or `contracts` — so if any of your legacy closed positions have those fields blank, they get dropped from the income total entirely.

Repair walks every closed/expired/assigned/rolled option in your account and fills missing fields from the audit log:

- **close_date** — falls back to `exp`, then audit's recorded close_date, then audit timestamp
- **premium** — pulled from the original `add_option` audit
- **contracts** — pulled from the original `add_option` audit
- **close_premium** — defaulted to 0 for assigned/expired (the standard convention)
- **direction** — inferred from option type (`sell` for CSP/CC, `buy` for LONG_PUT/LONG_CALL)

Idempotent and per-option audited as `repair_option_data`.

### 8. Edit closed positions

You can now edit the **sub-account** and **note** on any closed option, closed spread, or sold share lot — without touching cash math or P&L. Open Portfolio, toggle "Show closed history" on the Options/Spreads/Holdings tab, and click the new `edit` link on any row.

When you change a sub-account, the linked cash ledger entries are updated to match (so the cash-by-sub-account breakdown stays consistent). Cash totals don't change. P&L doesn't change.

This was needed because the original Phase 4 spread form defaulted to "Brokerage" — if you accidentally added a Volkman wheel spread under Brokerage, there was no way to fix it after closing without doing the full delete-and-recreate dance. Now it's two clicks.

## Deploy steps

1. Delete existing `omega_dashboard/` folder in the repo.
2. Drag the new `omega_dashboard/` from this zip into the repo.
3. GitHub Desktop → commit → push.
4. Wait for Render auto-deploy.
5. Hit `/dashboard/health` — should still report `phase: 4` (4.5 is a polish release, not a phase bump).
6. Go to **Portfolio → Settings → Maintenance → Retro-fix wheel assignments →** to backfill the 4-7 missing share lots from your existing assignments.
7. Test the new auto-handle flow on the next assignment that comes up.

## Files changed vs Phase 4

- `omega_dashboard/campaigns.py` — **NEW**, 580 lines, all campaign logic
- `omega_dashboard/writes.py` — added auto-handle plumbing in `close_option`, audit enrichment in `get_recent_audit_entries`, `retrofix_scan_assignments` + `retrofix_apply`, undo handlers for new ops, campaign hooks in `add_option`/`close_option`/`add_holding`/`sell_holding`/`roll_option`, campaigns key in `wipe_account`
- `omega_dashboard/routes.py` — `portfolio_option_close` reads `auto_handle_shares` + `actual_fill_price`, new `/portfolio/retrofix/wheel-assignments` GET + POST routes
- `omega_dashboard/templates/dashboard/portfolio.html` — auto-handle strip in close form, enriched audit log table with click-to-expand, active campaigns panel on Holdings, closed campaigns in History toggle, Maintenance section on Settings
- `omega_dashboard/templates/dashboard/retrofix.html` — **NEW**, retro-fix preview/apply page
- `omega_dashboard/static/omega.css` — campaign cards, auto-handle strip, audit row expand, maintenance section

## Smoke tests (passed during build)

1. Add CSP → campaign auto-created in `active_csp_phase`
2. Assign CSP with auto-handle → shares created, cash debited, campaign transitions to `active_holding`
3. Assign with skip → status flips, NO shares (legacy behavior preserved)
4. Audit log enrichment: every entry has `headline` + `detail` for distinguishing positions
5. Second CSP on same ticker+sub while shares held → attaches to holding campaign
6. CC called away with auto-handle → shares removed, cash credited
7. Override fill price → uses override for both basis and cash math
8. Partial CSP assignment (1 of 3) → only 100 shares created, 2 contracts stay open
9. Same ticker, two sub-accounts → independent campaigns
10. Full wheel cycle (CSP → assign → CC → called away) → campaign auto-closes
11. Roll within active campaign → both old & new attach to same campaign
12. Retrofix scan on legacy assignments → finds candidates, excludes already-fixed
13. Retrofix apply with selective checkboxes → only checked rows get fixed
14. Each retrofix is individually undoable from audit log
15. Undo of auto-handled assignment → reverses BOTH option AND share lot
16. All seven portfolio sub-tabs render without errors
17. Retrofix preview page renders correctly
18. `actual_fill_price` empty-string from form → falls back to strike
19. `expired` / `closed` status with auto_handle=true → no shares (no-op correctly)
20. LONG_CALL/LONG_PUT assignment with auto_handle=true → no shares (not a wheel)

## What's NOT in 4.5 (deferred to a later phase)

- The redeploy-spam Telegram alert from `option_assigned` events. Spec calls this a Phase 5 concern.
- A tickbox to hide closed campaigns by sub-account or date.
- Campaign-level edit/delete UI (you'd wipe the Redis key to start over if needed).

## If something breaks

The campaign data is purely additive. If the Holdings page errors out and you suspect the campaigns code:

1. Open Render shell.
2. `redis-cli del brad:portfolio:wheel_campaigns mom:portfolio:wheel_campaigns partner:portfolio:wheel_campaigns`
3. Reload. The Holdings tab will show no campaigns but everything else works.

The `option_assigned_with_shares` audit op IS undoable, so if an auto-handle goes weird on an actual assignment, just undo it from the Settings tab and re-do it manually with `Skip` selected.
