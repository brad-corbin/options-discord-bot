# DEPLOY v8.2 — Runbook

Short, in-order, Brad-friendly. No pre-deploy during market hours.

---

## 1. Files to upload to the repo

**New (drop in repo root):**
- `rate_limiter.py`
- `dashboard.py` *(replaces any prior dashboard.py; v8.2 adds Position PnL)*

**Patched (replace existing):**
- `app.py` *(all Patches 6–11, dashboard wiring, v8.2 header block)*
- `schwab_stream.py` *(Patch 1)*
- `schwab_adapter.py` *(Patches 4, 5)*
- `thesis_monitor.py` *(Patches 2, 3)*

**Docs (repo root, reference):**
- `README_dashboard.md`
- `DEPLOY_v8_2.md` *(this file)*

---

## 2. Render env vars

**Set before deploy:**

```
# Startup silence — KEEP ON for first deploy, turn off once stable
BOT_STARTUP_QUIET=1
BOT_STARTUP_QUIET_SEC=300

# Dashboard (Phase 1 + v8.2 PnL)
DASHBOARD_ENABLED=1
DASHBOARD_SHEET_ID=1v9UN6qoTWdFJWe332qfnrvJwMF88JavN-MteHL66Iv8
DASHBOARD_INTERVAL_SEC=60
```

**Optional (defaults are fine for first deploy):**

```
SCHWAB_RATE_PER_MIN=110
SCHWAB_RATE_BURST=110
SCHWAB_RATE_TIMEOUT_S=60
```

**One-time Sheets access:** share the dashboard Sheet ID above with `bot-sheets-writer@corbin-bot-tracking.iam.gserviceaccount.com` as **Editor**. If this hasn't been done, Position PnL tab creation will fail silently and the dashboard column rollups will stay blank.

---

## 3. Deploy order

1. Commit & push all six code files.
2. Set env vars in Render (section 2).
3. Trigger a deploy.
4. Watch logs for the first 15 minutes (section 4 grep checks).
5. If checks pass: unset `BOT_STARTUP_QUIET`, redeploy so normal Telegram traffic resumes.
6. Run `/confidence 75` in Telegram once the bot is responsive — this activates Patch 10's post gate.

Post `BOT_STARTUP_QUIET` unset, expect normal trading alerts to fire again immediately. If trading alerts *still* don't fire during market hours: check `/confidence` is set reasonable and search logs for `Trade card posted suppressed`.

---

## 4. Post-deploy verification (grep)

Run these against Render logs within 15 minutes of first market-open after the deploy. Each bullet is pass/fail.

**Boot-time sanity:**

| Grep | Pass if |
|---|---|
| `Schwab rate limiter initialized` | Appears once |
| `Dashboard 3000: writer thread started` | Appears once |
| `dashboard: initialized` | Appears once |

**Patch-specific:**

| Grep | Pass if |
|---|---|
| `AttributeError: 'ThesisMonitorDaemon'` | **Returns nothing** (Patch 2 confirmed) |
| `Thesis monitor: \d+s for \d+ streaming tickers` | Appears (Patch 2 confirmed — daemon now logging past the old crash point) |
| `Silent thesis generation complete: \d+ generated, \d+ already had EM cards, \d+ failed` | Appears daily ~8:25 AM CT (Patch 8) |
| `Silent thesis using fallback expiry` | Appears for at least some of the 22 previously-failing tickers (Patch 9) |
| `Telegram SUPPRESSED \(startup-quiet` | Appears during the quiet window only (Patch 11) |
| `Conviction OCC build failed for` | If present, should show full context + traceback (Patch 3) |
| OCC letters in position logs | Look for BOTH `C` and `P` on conviction/active trades — not all-P (Patch 1) |

**Dashboard + PnL:**

| Grep | Pass if |
|---|---|
| `dashboard: wrote \d+ ticker rows` | Appears every ~60s |
| `dashboard: wrote \d+ position pnl rows` | Appears every ~60s (may be 0 if no positions tracked yet) |
| `dashboard: position-close detection bootstrapped` | Appears once on first dashboard tick (silent bootstrap — expected) |
| `dashboard: 2:45 snapshot captured` | Appears on expiry day afternoons; zero outside of those windows is correct |
| `dashboard: appended \d+ signal events` | Appears when signals transition |

**Confidence gate:**

| Grep | Pass if |
|---|---|
| `Trade card posted suppressed: .* telegram gate 75` | Appears for any trade card built with conf < 75 after you've run `/confidence 75` |
| `🔇 .* gated` | Appears as a digest-line icon for suppressed tickers |

**Rate-limiter contention:**

| Grep | Pass if |
|---|---|
| `Rate limiter waited \d+\.\d+s` | Occasional brief waits fine; sustained >5s means bucket too tight — bump `SCHWAB_RATE_PER_MIN` |

---

## 5. Verify in the Sheet

Open the dashboard Sheet. Within ~2 minutes of first successful tick:

- **Three tabs exist:** `Dashboard`, `Signal Log`, `Position PnL`.
- **`Dashboard` tab** has one header row (26 columns including the three new `Best Peak PnL%`, `Best 2:45 PnL%`, `Best Hold Peak%`) and one row per flow ticker.
- **`Position PnL` tab** has the 16-column header. Rows appear as soon as there's at least one tracked position.
- **`Signal Log` tab** is append-only. A `position_close` row appears the first time a tracked position transitions to closed *after* dashboard boot (previously-closed positions don't backfill — by design).

If Dashboard populates but Position PnL stays empty:
1. Check `grep "Position PnL tab not ready"` — most likely a Sheets permissions issue.
2. Check the recommendation tracker is populated: `grep "RecTracker: new campaign"` should be appearing for recent trades.

---

## 6. Rollback

All changes are additive or surgical. To revert:

**Dashboard only (keep patches):**
- Set `DASHBOARD_ENABLED=0` in Render, redeploy. Sheet stays as it is; writes stop.

**Individual patch:**
- Each patch has a `# v7.3` or `# v8.2` label near its change. Search for the label, delete the block, redeploy.
- Patch 4 depends on `rate_limiter.py` — if reverting Patch 4, you can leave `rate_limiter.py` in the repo; nothing else imports it.

**Full revert:**
- `git revert` the v8.2 commit(s). Redeploy. No cleanup needed.
- Redis keys under `dashboard:close_245:*` can be deleted or left — no other code reads them.

---

## 7. Known issues that persist in v8.2 (document, don't fix)

- Conviction plays can re-fire every 8 minutes on the same flow snapshot (deferred to v8.3+).
- Same conviction signal posts as both `CONVICTION PLAY` (immediate) and `FLOW CONVICTION` (swing) — 2× noise per signal. Deferred.
- Exit signals can re-fire multiple times on the same position. Deferred.
- Option peaks fire on every new intraday high, not just PT1/PT2/PT3 thresholds. Deferred.
- `BREAKDOWN CONFIRMED` Potter Box annotation may have a code-path issue. Low priority.

None of these block v8.2.

---

## 8. What to do if something goes sideways

**Bot won't start at all:** most likely Patch 4's `from rate_limiter import rate_limit`. Confirm `rate_limiter.py` is in the repo root next to `app.py`. If the file is there and import still fails, check for a whitespace/encoding issue on the import line.

**Trading alerts silent during market hours:** `BOT_STARTUP_QUIET` probably still on. Unset and redeploy. If still silent, check `/confidence` isn't too high — default if unset is 0, which doesn't suppress anything.

**Sheets quota errors:** the dashboard writes 5-8 calls per minute. If you see 429s from Sheets, the service account might be shared with another bot. Bump `DASHBOARD_INTERVAL_SEC` to 120 to halve the pressure.

**Silent EM still failing for the same tickers after Patch 9:** the fallback-expiry lookup returned no future expirations. Check `get_expirations(TICKER)` for those specific tickers. Usually means an options-chain data source issue, not a patch bug.
