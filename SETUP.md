# Phase 1 — Auth + Navigation Scaffolding

Self-contained Flask Blueprint. Drop the `dashboard/` folder into your bot
repo at the same level as `app.py`, add 2 lines to `app.py`, set 2 env vars
on Render, and you're live.

## File drop

Place this `dashboard/` directory directly in your bot repo:

```
your-bot/
├── app.py
├── active_scanner.py
├── thesis_monitor.py
├── ...
└── dashboard/                    ← drop here
    ├── __init__.py
    ├── routes.py
    ├── templates/
    │   └── dashboard/
    │       ├── base.html
    │       ├── login.html
    │       ├── command_center.html
    │       ├── trading.html
    │       ├── portfolio.html
    │       └── diagnostic.html
    └── static/
        └── dashboard/
            └── omega.css
```

## Wire it into `app.py`

Find this line near the top (around line 720):

```python
app = Flask(__name__)
```

Right after it, add:

```python
# ── Omega dashboard ───────────────────────────────────────
from dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "").strip() or os.urandom(32)
```

That's it. The Blueprint owns these routes and won't conflict with anything
existing:

- `GET  /`                        — landing redirect
- `GET  /login`, `POST /login`    — password form
- `GET  /logout`                  — clear session
- `GET  /account/<key>`           — account switcher
- `GET  /dashboard`               — Command Center (placeholder in phase 1)
- `GET  /trading`                 — Trading view (placeholder in phase 1)
- `GET  /portfolio`               — Portfolio (placeholder in phase 1)
- `GET  /diagnostic`              — Diagnostic (placeholder in phase 1)
- `GET  /dashboard/health`        — JSON health probe
- `GET  /dashboard/static/*`      — CSS & assets

None of these collide with your existing `/health`, `/em`, `/scan`, `/journal`,
`/holdings_scan`, `/swing`, etc.

## Required env vars on Render

In your service's **Environment** tab, add:

| Key | Value | Notes |
|---|---|---|
| `DASHBOARD_PASSWORD` | (something memorable) | Single password protecting all dashboard routes |
| `DASHBOARD_SECRET_KEY` | (any random ~32 char string) | Signs session cookies |

Generate a secret key easily: `python -c "import secrets; print(secrets.token_hex(32))"`

If `DASHBOARD_PASSWORD` isn't set, the login route returns a 500 with a clear
message rather than opening the dashboard with no password. Fail-safe.

## Verify it works

After redeploy, hit the health endpoint to confirm the Blueprint is registered:

```
https://your-bot.onrender.com/dashboard/health
```

You should get back:

```json
{
  "status": "ok",
  "module": "omega-dashboard",
  "phase": 1,
  "auth_configured": true
}
```

Then visit:

```
https://your-bot.onrender.com/login
```

Enter the password you set, and you'll land on the Command Center page.
The four tabs across the top all work, the five account chips switch the
accent color across the page, and "Log out" clears the session.

## What's in this phase

- ✅ Single-password auth, Flask session, 30-day cookie persistence
- ✅ Header with logo, page tabs, system status strip
- ✅ Account switcher (Combined / Mine / Mom / Partnership / Kyleigh)
- ✅ Account-keyed accent color theme (CSS variables)
- ✅ Cookie-persisted active account choice
- ✅ Four placeholder pages with build roadmap visible
- ✅ Login page with branded mark and clean form
- ✅ Mobile-responsive header + sub-header
- ✅ The full visual system (typography, colors, panel styles, animations)
  ready for phases 2–6 to build on

## What's not in this phase

By design — held strictly to the spec:

- ❌ No data reads (Redis, Sheets) — placeholder pages only
- ❌ No writes — Portfolio entry forms are phase 4
- ❌ No live updating — phase 3+
- ❌ No charts or capital bars — phase 3
- ❌ No daily snapshots / restore — phase 2

If something here doesn't work the way the mockup looked, that's a phase 1
bug worth flagging. If something here is missing that wasn't in phase 1
scope, it'll be in a later phase.

## After phase 1 ships

Phase 2 starts: daily snapshot job + write audit log + restore page. The
durability layer goes in *before* phase 4's writes so the data wipe never
happens again.
