# Phase 3 — Snapshot-Based Read-only Views

This is a clean rebuild. **No bot internals reads. No live API calls. No hangs possible.**

The Command Center reads from your most recent portfolio snapshot in Sheets.
Data is "as of" your last snapshot (refreshes daily at 06:00 UTC, or click
"Snapshot Now" on the Durability page for an immediate fresh capture).

---

## Step-by-step setup (designed for non-developers)

### Step 1 — Replace the dashboard folder

1. Open your `Documents/GitHub/options-discord-bot/` folder in File Explorer
2. **Delete** the existing `omega_dashboard/` folder (right-click → Delete)
3. Unzip this zip somewhere temporary
4. Inside the unzip, you'll see an `omega_dashboard/` folder
5. **Drag and drop** that `omega_dashboard/` folder into your project folder — replacing what was there

### Step 2 — Update `app.py`

Open `app.py` in any text editor. Find the section that looks something like this (around line 720):

```python
app = Flask(__name__)
#
# OMEGA DASHBOARD (web command console)
#
from omega_dashboard import dashboard_bp
from werkzeug.middleware.proxy_fix import ProxyFix

# Trust Render's reverse proxy — required for session cookies over HTTPS
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

_dashboard_secret = os.getenv("DASHBOARD_SECRET_KEY", "").strip()
if not _dashboard_secret:
    raise RuntimeError(
        "DASHBOARD_SECRET_KEY env var is empty. Set it on Render."
    )
app.secret_key = _dashboard_secret

# Cookie settings appropriate for HTTPS deployment behind Render's proxy
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

app.register_blueprint(dashboard_bp)
```

**Replace that entire block with this simpler version:**

```python
app = Flask(__name__)
#
# OMEGA DASHBOARD (web command console)
#
from omega_dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "").strip() or os.urandom(32)
```

That's it. Save the file.

### Step 3 — Commit and push via GitHub Desktop

1. Open GitHub Desktop
2. You'll see "X changed files" in the left panel — confirming your changes are detected
3. In the bottom-left, type a commit summary like: `Phase 3 — clean snapshot-based dashboard`
4. Click **Commit to main**
5. At the top, click **Push origin**

Render will auto-deploy in about 60-90 seconds.

### Step 4 — Verify it works

After deploy completes:

1. Visit `https://options-discord-bot.onrender.com/dashboard/health`

   You should see:
   ```json
   {
     "status":"ok",
     "module":"omega-dashboard",
     "phase":3,
     "auth_configured":true,
     "durability":{"sheets_available":true,...}
   }
   ```

2. Visit `https://options-discord-bot.onrender.com/login` and log in

3. You should land on the Command Center showing real data from your snapshot

---

## What you get

**Command Center** (`/dashboard`):
- **Snapshot meta strip** at top — shows which snapshot you're viewing and when it was captured
- **Income hero** — Year-to-date and Month-to-date realized option income (from closed/expired/assigned/rolled options in the snapshot)
- **Goal pace bar** — once you have at least one completed month of income history
- **Cash panel** — total cash with breakdown by underlying account
- **Status strip** — open positions counts (wheel options / spreads / holdings)
- **Open Positions panel** — your full open positions, grouped by type, color-coded by account

**Trading View** (`/trading`):
- Placeholder for now. Live alerts feed and watch map require integration with bot internals that we deferred for stability.

**Durability** (`/restore`):
- Unchanged from Phase 2. Snapshots, audit log, restore.

**Portfolio + Diagnostic**:
- Placeholders, scheduled for Phase 4 and Phase 6.

---

## What's deliberately NOT in this phase

- **Live spot prices on holdings** — would need a live API call per page render, which is what hung the old Phase 3
- **Live alerts feed** — same reason
- **Watch map cards with thesis levels** — required reading bot internals
- **Scanner status / regime tag in header** — could hang
- **Capital progression bars** — needs Phase 4 starting balance tracking

These are all valuable and will come back in later phases when they can be done safely.

---

## How to refresh data

The dashboard auto-pulls from the latest snapshot. New snapshots happen:

1. **Automatic daily at 06:00 UTC** (1am Central) — every night
2. **On demand** — go to **Durability** tab → click **Snapshot Now**

After clicking Snapshot Now, refresh the Command Center page — new data appears.

---

## If anything goes wrong

If `/dashboard/health` doesn't return phase 3, or login breaks:

1. Take a screenshot of the URL and what's shown
2. Check Render logs for errors
3. We can roll back to the Phase 2 working state in seconds via Render's deploy history

The point of this rebuild is that there are NO complex moving parts. If something
fails, it's a simple thing to find. No proxy fixes, no cookie magic, no bot
internals — just read snapshot, render template, return.
