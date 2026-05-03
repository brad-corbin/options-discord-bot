# Omega 3000 — Phase 4.5 Deploy Notes

Deploy is the same as Phase 4: drag this `omega_dashboard/` folder into the repo, replacing the existing one. No `app.py` changes needed — the blueprint registration from Phase 4 still works.

## What's new in Phase 4.5

Four user-visible changes plus the data layer that powers them.

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
