# Phase 4 — Portfolio Writes (Polished)

The dashboard becomes write-capable. Manual entry forms for cash, holdings,
options, spreads, rolls, and transfers (Kyleigh + Clay). **Plus a comprehensive
mistake-recovery layer** — smart deletes, edit forms, and one-click undo for
every action.

---

## What's new in Phase 4

**Account model expanded:**
- Top bar now has 6 chips: Combined / Mine / Mom / Partnership / Kyleigh / Clay
- Mine theme uses authentic Aggie Maroon (`#500000`) with a brighter accent (`#8b1818`) for big numbers so they stay readable
- Underlying portfolio accounts: brad / mom / partner (all support positions)
- Kyleigh & Clay are notional-only ledgers (transfers in/out, no positions)

**Portfolio page is now a sub-tabbed entry interface:**
- **Cash** — deposits, withdrawals, manual adjustments. Per sub-account breakdown.
- **Holdings** — share lots with cost basis, plus separate ETF lump-sum tracking.
- **Options** — CSP / CC / Long Call / Long Put with auto-tagging.
- **Spreads** — Bull Put, Bear Call, Bull Call, Bear Put, Iron Condor, Inverse Condor.
- **Rolls** — single-action: closes old + opens new + logs net cash event.
- **Transfers** — Kyleigh + Clay parallel ledgers.
- **Settings** — sub-account tag management, audit log with undo, wipe (snapshot-first).

**Mistake recovery (the polish):**
- **Smart delete** — clicking Delete on any position shows a modal with all linked cash events. You choose: "fully undo (remove cash too)" OR "keep cash, just remove position record."
- **Edit buttons** — fix typos in option strike/exp/premium/contracts without re-entering.
- **Undo last action** — Settings tab shows last 15 audit entries. One click reverses any of them, including rolls (reopens old, deletes new, removes net cash) and transfers (reverses both ledgers).

**Sub-account tags**: Brokerage / BC Rollover / BC Roth / CC Roth / CC Rollover / Volkman Wheel / Partnership (default = Brokerage). User-extensible from Settings.

**All entry forms support backdating** — enter your historical positions with their actual dates and your YTD income chart will be accurate immediately.

---

## Step-by-step setup

### Step 1 — Replace the dashboard folder

1. Open your `Documents/GitHub/options-discord-bot/` folder
2. **Delete** the existing `omega_dashboard/` folder
3. Unzip this zip somewhere temporary
4. Inside the unzip, find the `omega_dashboard/` folder
5. **Drag** that folder into your project folder

### Step 2 — `app.py` is unchanged

No `app.py` changes needed for Phase 4! The wiring from Phase 3 still works.

### Step 3 — Commit and push via GitHub Desktop

1. ~10 changed files will appear
2. Commit summary: `Phase 4 — portfolio writes (polished)`
3. Click **Commit to main** → **Push origin**

### Step 4 — Verify

After Render redeploys (~60-90s):

1. `/dashboard/health` should return `"phase": 4`
2. Click **PORTFOLIO** in nav → you'll land on Cash sub-tab
3. Try entering a cash deposit (your initial capital)
4. Click HOLDINGS, OPTIONS, SPREADS sub-tabs
5. Click TRANSFERS → see Kyleigh + Clay panels side by side
6. Click SETTINGS → see audit log at bottom with Undo buttons

---

## How to enter your portfolio

**Recommended order:**

1. **Cash → enter starting balances per account** with `Brokerage` sub-account tag
2. **Cash → additional deposits** (rollovers into BC Rollover, etc.)
3. **Holdings → share positions** with cost basis, sub-account, real acquisition dates
4. **Options → open positions** with original premiums and dates (cash auto-credited)
5. **Holdings → ETF lump-sums** for things you don't track per share
6. **Spreads → any open spreads**
7. **Transfers → Kyleigh/Clay history**

**Closing a historical option:**
- Open the position with its original date
- Click **Close** in the row → mark expired/closed/assigned with the actual close date
- Or click the **Or roll →** link to use the Rolls wizard if you rolled it
- Cash event logged at the close date — your monthly income reflects it correctly

---

## Mistake recovery (read this!)

You will make mistakes entering data. That's expected. Here's how to fix them:

### Typo in a position's strike/premium/etc.
1. Go to the row, click **Edit**
2. Change the wrong field, click Save
3. Done. (Note: edits don't auto-adjust cash. If you mis-entered the premium and it credited the wrong cash amount, see below.)

### Position fully wrong, want to remove it cleanly
1. Click **Delete** on the row → modal opens
2. Modal lists all linked cash events with amounts and dates
3. Leave the **"Also reverse these cash events"** checkbox CHECKED (default)
4. Click Delete → position gone, cash perfectly clean

### Want to remove just the position record but keep cash (rare)
1. Click **Delete** → modal opens
2. UNCHECK the cash reversal checkbox
3. Click Delete → only the position is removed, cash entries stay

### Made a recent action you want to undo entirely
1. Go to **Settings** sub-tab
2. Scroll to **Recent Activity** section
3. Find the action in the list
4. Click **Undo** next to it
5. Confirms → action reversed, including any cash events created by it

**Undo handles every action type:**
- `add_option` / `add_spread` / `add_holding` / `add_lumpsum` → removes them and reverses cash
- `option_closed/expired/assigned` → reopens the option, reverses close cash
- `roll_option` → deletes new option, reopens old option, reverses net cash
- `cash_*` events → deletes the cash event
- `transfer_kyleigh/clay` → removes from recipient ledger AND reverses source cash
- `edit_holding/option` → restores prior values
- `sell_holding` → re-adds the lot back

---

## Wiping everything (start completely fresh)

Settings → Danger zone:
- Pick the account (or "ALL ACCOUNTS")
- Type the exact confirmation phrase: `WIPE BRAD` or `WIPE ALL ACCOUNTS`
- A snapshot is taken FIRST (recoverable via Durability tab)
- Redis cleared, all forms empty, ready for fresh entry

Even if you mess up after a wipe, the pre-wipe snapshot is your safety net.

---

## After your first entry

Click **DURABILITY** → **Snapshot Now** to push your entries to Sheets, so:
- Command Center reflects your data (Command Center reads from snapshots)
- You have a recovery point

The nightly snapshot at 06:00 UTC handles ongoing capture.

---

## What's deferred to later phases

- **Campaign rollup at assignment** (CSP assigned → fold expired CSPs into adjusted basis) — Phase 4.5
- **Capital progression bars on Command Center** — Phase 4.5 (now that we have starting balances)
- **Holdings sentiment** — Phase 5
- **Diagnostic page** — Phase 6
- **Mobile responsive polish, sub-account dashboard filter** — Phase 7

---

## If anything breaks

Same as always:
1. Take a screenshot
2. Check Render logs
3. Roll back via Render's deploy history (one click)

Phase 4 is additive — Phases 1/2/3 unchanged. If Phase 4 has an issue, rolling
back to the Phase 3 commit gives you the working read-only dashboard while
we fix.
