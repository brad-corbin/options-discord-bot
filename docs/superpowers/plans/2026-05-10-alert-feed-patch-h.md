# Alert Feed Dashboard — Patch H Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the user-visible surface for the V1 alert recorder shipped in Patch G. A new "Alerts" dashboard tab between Market View and Portfolio that shows alerts as they're recorded, with a 10-second polling feed, click-through detail pages, and graceful empty-state handling. This is the proof-of-life surface — once H ships, Brad can flip the recorder env gates on with confidence and watch alerts appear live.

**Architecture:** Pure read-side. New module `omega_dashboard/alerts_data.py` opens its own read-only sqlite3 connection (`mode=ro` URI form) to `/var/backtest/desk.db` and runs fresh queries against the recorder schema — no imports from `alert_recorder`'s write internals. Two new Flask routes (`/alerts` page + `/alerts/data` JSON) mirror the existing `/trading` and `/trading/data` pattern. A third route `/alerts/<alert_id>` renders the detail page. Templates match the existing dark-theme + brass-accent + monospace-numerals style and follow `docs/superpowers/mockups/2026-05-10-alerts-mockup.html` for layout. SVG line chart for the price track is server-rendered (no JS charting dep). All env gates default OFF; H is wholly additive — none of the trading path, recorder write path, or daemons change.

**Approved fixes from QC review (incorporated in this revision):**

1. **UUID v4 alert_id validation.** `get_alert_detail` rejects anything that doesn't match `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (case-insensitive). Belt-and-suspenders with the SQL parameter binding and Flask's `<string:>` converter.
2. **`format_structure_summary` for v2_5d.** V2 5D's `suggested_structure` is `{"type": "evaluation"}` only — grade and bias come from the alert row's `classification` and `direction` columns. The formatter signature changes to `format_structure_summary(engine, structure, classification=None, direction=None)`. `_format_card` and `_build_detail_dict` pass `row["classification"]` and `row["direction"]`.
3. **Polling pause on hover.** Feed JS tracks `isHovering` via mouseover/mouseout on `.alert-card`; `poll()` early-returns while hovering. Prevents rare mid-click DOM-swap races.
4. **`var(--brass-bright)` not `var(--gold)`.** `omega.css` defines `--brass`, `--brass-bright`, `--brass-deep` (no `--gold`). All V2 5D accents in the alerts CSS use `var(--brass-bright)`.

**Tech Stack:** Python 3.11, sqlite3 stdlib (read-only URI mode), Flask, Jinja2, vanilla JS for the 10s polling loop, plain CSS additions to `omega_dashboard/static/omega.css`. `zoneinfo.ZoneInfo("America/Chicago")` for time bucketing. No new dependencies.

---

## Context the implementer needs

- Read `docs/superpowers/specs/2026-05-08-alert-recorder-v1.md` end-to-end before starting. The recorder schema is what H queries — column names, types, JSON shapes are all defined there.
- Read `docs/superpowers/plans/2026-05-09-alert-recorder-patch-g.md` for what shipped in G. In particular: `migrations/0001_initial_schema.sql` (the six-table schema H reads), `alert_recorder.py` (the write side H must NOT import from), and the env-var gate names.
- Read `CLAUDE.md` end-to-end. Especially "Audit discipline" (non-negotiable) and the recent Patch G entries under "What's done as of last session" + "Decisions already made — don't relitigate". The DTE convention divergence between LCB (trading-DTE) and v8.4 CREDIT (calendar-DTE) is critical for H — see "Audit discipline" constraint #5 below.
- The trading tab is the architectural template. `omega_dashboard/routes.py:297-325` shows the page-route + JSON-polling-route pair pattern Patch H mirrors. `omega_dashboard/templates/dashboard/trading.html` shows the polling-loop JS pattern. `omega_dashboard/templates/dashboard/_trading_card.html` shows the existing card style — match its visual language.
- The alerts feed runs on every dashboard worker. Read-only sqlite3 access from multiple workers is safe (WAL mode is set by G's migration runner, readers never block readers). The recorder daemons run on the worker that holds the leader lock (per CLAUDE.md). The dashboard worker may or may not be the leader — that's fine, reads work either way.

## Key codebase anchors

| Component | Location | Notes |
|---|---|---|
| `PAGE_TABS` definition | `omega_dashboard/routes.py:67-73` | Insert new tab between `trading` and `portfolio` |
| Existing page+JSON route pair | `omega_dashboard/routes.py:297-325` | Copy this pattern for `/alerts` + `/alerts/data` |
| Tab-nav loop in base layout | `omega_dashboard/templates/dashboard/base.html:35-40` | No edits — picks up the new tab automatically |
| Trading template (polling JS reference) | `omega_dashboard/templates/dashboard/trading.html` | Copy the polling loop shape; adjust endpoint + payload |
| Existing card style reference | `omega_dashboard/templates/dashboard/_trading_card.html` | Visual language to harmonize with |
| Dashboard CSS file | `omega_dashboard/static/omega.css` | All styling additions go here. Reuse the actual tokens (defined at lines 17-63): `--bg-void`, `--bg-panel`, `--bg-elev`, `--border`, `--border-bright`, `--text`, `--text-muted`, `--text-dim`, `--brass`, `--brass-bright`, `--brass-deep`, `--positive`, `--positive-bright`, `--negative`, `--negative-soft`, `--neutral`, `--warn`, plus the account-palette tokens `--mine`, `--mom`, `--partner`, `--kyleigh`, `--clay`. Engine accent mapping (per mockup): LCB→`--warn`, V2 5D→`--brass-bright`, credit→`--partner`, conviction→`--mine`. |
| Recorder schema | `migrations/0001_initial_schema.sql` | The authoritative column list H queries against |
| Recorder write side (do NOT import private helpers) | `alert_recorder.py` | `_conn`, `_db_path`, `_master_enabled`, `_engine_enabled` are all underscore-private — H does NOT import them. Public read helpers `get_alert` / `list_active_alerts` exist but use the recorder's RW connection — H writes its own RO queries instead for a clean boundary. |
| Boot-time migration runner | `db_migrate.py` | Called by tests via `apply_migrations(db_path)` to create the schema in temp DBs |
| Env-var-gate canonical names | `alert_recorder.py:63-79` | `RECORDER_ENABLED`, `RECORDER_LCB_ENABLED`, `RECORDER_V25D_ENABLED`, `RECORDER_CREDIT_ENABLED`, `RECORDER_CONVICTION_ENABLED`, `RECORDER_TRACKER_ENABLED`, `RECORDER_OUTCOMES_ENABLED`, `RECORDER_DB_PATH` |
| Existing test pattern (PASS/FAIL counter) | `test_alert_recorder.py:330-346` | Match this convention for `test_alerts_data.py` |

## File structure (created/modified by this plan)

**Created:**
- `omega_dashboard/alerts_data.py` — read-side data layer. `list_alerts()`, `get_alert_detail()`, `format_structure_summary()`, `_build_pnl_svg()`, status-strip helper. Owns its own RO sqlite3 connection.
- `omega_dashboard/templates/dashboard/alerts.html` — feed page (status strip + today's cards + collapsible historical sections). Inline polling JS.
- `omega_dashboard/templates/dashboard/alerts_detail.html` — detail page (header + parent/children linkage + structure block + SVG chart + outcomes table + features table + collapsible raw JSON).
- `omega_dashboard/templates/dashboard/_alert_card.html` — single-card partial used by both the initial render and (server-rendered) by the JSON polling endpoint's payload.
- `test_alerts_data.py` — hermetic tests (8-12) covering bucketing, structure_summary formatting, defensive parsing, empty-state matrix, detail assembly, parent linkage.

**Modified:**
- `omega_dashboard/routes.py` — `PAGE_TABS` insert, three new routes (`/alerts`, `/alerts/data`, `/alerts/<alert_id>`), all `@login_required`.
- `omega_dashboard/static/omega.css` — additions for `.alerts-page`, `.alerts-status-strip`, `.alert-card`, `.alert-card.engine-{lcb|v25d|credit|conviction}`, `.alerts-detail-*`, pulse animation keyframes. Pure CSS — no JS-driven animation.
- `CLAUDE.md` — append Patch H entry to "What's done as of last session"; add `/alerts` route + `alerts_data.py` to Repo layout.

**Untouched (validate no regressions):**
- All `canonical_*.py` modules and their tests
- `bot_state_producer.py`, `omega_dashboard/research_data.py`
- `app.py` — engines, daemons, recorder hooks, scheduling, leader-lock — none of it changes
- `alert_recorder.py`, `alert_tracker_daemon.py`, `outcome_computer_daemon.py`, `db_migrate.py` — recorder write side untouched
- All existing dashboard templates and routes — Trading, Portfolio, Research, Durability, Desk all unchanged

## Env vars

| Var | Read by | Default | Notes |
|---|---|---|---|
| `RECORDER_DB_PATH` | `alerts_data.py` (RO open) | `/var/backtest/desk.db` | Same default as `alert_recorder.py`. Override only in tests. |
| `RECORDER_ENABLED` | `alerts_data.py` (status strip) | `false` | Read at request time. Drives green-vs-gray dot. |
| `RECORDER_{LCB,V25D,CREDIT,CONVICTION}_ENABLED` | `alerts_data.py` (status strip hover) | `false` | Determines which engines are listed as "on" in the hover tooltip. |
| `RECORDER_{TRACKER,OUTCOMES}_ENABLED` | `alerts_data.py` (status strip hover) | `false` | Surfaced in the same tooltip, useful when tracking cards say "no samples yet". |

H reads env vars only — no writes, no setting. The status-strip read happens once per `list_alerts()` call, no caching.

---

## Constants and shared helpers (defined once in `alerts_data.py`)

These constants and helpers are used across multiple sub-tasks. Defined once at the top of `alerts_data.py`. Listed here so the implementer doesn't accidentally duplicate them across the module or scatter engine-icon strings into templates.

```python
# omega_dashboard/alerts_data.py — top of file

"""Alerts feed read-side data layer.

# v11.7 (Patch H.2): pure read-only access to /var/backtest/desk.db.
# Never imports alert_recorder write internals. Opens its own read-only
# sqlite3 connection per request (URI mode=ro), so writers and readers
# stay decoupled at the connection level too.

See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md for schema.
"""
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/var/backtest/desk.db"
LIST_LIMIT = 200                     # hard cap on alerts fetched per page load
CHICAGO_TZ = ZoneInfo("America/Chicago")

# alert_recorder generates alert_ids via uuid.uuid4(). Reject anything
# that doesn't match this shape on the detail route — belt-and-suspenders
# with the SQL parameter binding and Flask's <string:> converter.
_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Single source of truth for engine display. Maps recorder engine slug
# to the icon, label, and CSS class suffix used by templates and CSS.
# Templates and CSS read these via context — DO NOT inline icons in HTML.
ENGINE_DISPLAY: Dict[str, Dict[str, str]] = {
    "long_call_burst":     {"icon": "🚀",   "label": "LONG CALL BURST", "stripe": "lcb"},
    "v2_5d":               {"icon": "⚡",   "label": "V2 5D EDGE",      "stripe": "v25d"},
    "credit_v84":          {"icon": "💎",   "label": "v8.4 CREDIT",     "stripe": "credit"},
    "oi_flow_conviction":  {"icon": "💎🚨", "label": "CONVICTION PLAY", "stripe": "conviction"},
}

# Per-engine DTE convention. Used ONLY on the detail page to tag the
# `suggested_dte` value with its unit. Card view never displays DTE.
DTE_CONVENTION: Dict[str, Optional[str]] = {
    "long_call_burst":     "trading",   # count_trading_days_between
    "credit_v84":          "calendar",  # (expiry - today).days
    "v2_5d":               None,        # no DTE recorded
    "oi_flow_conviction":  None,        # no DTE recorded
}

# Recorder env-var slugs (canonical names from alert_recorder.py:63-79).
# Read at request time so flipping in Render takes effect on next page load.
ENGINE_FLAG_VARS = {
    "long_call_burst":     "RECORDER_LCB_ENABLED",
    "v2_5d":               "RECORDER_V25D_ENABLED",
    "credit_v84":          "RECORDER_CREDIT_ENABLED",
    "oi_flow_conviction":  "RECORDER_CONVICTION_ENABLED",
}
DAEMON_FLAG_VARS = {
    "tracker":  "RECORDER_TRACKER_ENABLED",
    "outcomes": "RECORDER_OUTCOMES_ENABLED",
}


def _db_path() -> str:
    return os.getenv("RECORDER_DB_PATH", DEFAULT_DB_PATH)


def _open_ro() -> sqlite3.Connection:
    """Open a read-only connection to the recorder DB. Caller closes.
    Raises sqlite3.OperationalError if the file doesn't exist (URI ro
    mode rejects missing files, which is what we want — list_alerts
    catches this and returns the friendly empty state)."""
    return sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=2.0)


def _now_micros() -> int:
    """UTC microseconds. Tests can monkeypatch this to fix 'now'."""
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def _flag_on(var: str) -> bool:
    return os.getenv(var, "false").lower() in ("1", "true", "yes")
```

These ship in H.2 and are used by H.3, H.4, H.5. Do not redefine elsewhere.

---

## Task index

- **H.1** — Add "Alerts" tab to `PAGE_TABS`
- **H.2** — `alerts_data.py` read-side data layer
- **H.3** — Routes for `/alerts`, `/alerts/data`, `/alerts/<alert_id>`
- **H.4** — Feed template (`alerts.html` + `_alert_card.html`) + CSS
- **H.5** — Detail template (`alerts_detail.html`) + SVG chart helper + CSS
- **H.6** — `test_alerts_data.py` (hermetic, 8-12 tests)
- **H.7** — `CLAUDE.md` update

Each task is its own commit. Do not bundle. Total: 7 commits.

---

## Task H.1: Add "Alerts" tab to `PAGE_TABS`

**Files:**
- Modify: `omega_dashboard/routes.py:67-73` — `PAGE_TABS` list
- Test: smoke-check via Flask URL build (no new test file)

- [ ] **Step 1: Insert tab definition between `trading` and `portfolio`**

In `omega_dashboard/routes.py` at lines 67-73, the current `PAGE_TABS` is:

```python
# v11.7 (Patch H.1): Alerts tab between Market View and Portfolio.
PAGE_TABS = [
    {"key": "dashboard",  "label": "Desk",         "endpoint": "dashboard.command_center"},
    {"key": "trading",    "label": "Market View",  "endpoint": "dashboard.trading"},
    {"key": "alerts",     "label": "Alerts",       "endpoint": "dashboard.alerts"},
    {"key": "portfolio",  "label": "Portfolio",    "endpoint": "dashboard.portfolio"},
    {"key": "research",   "label": "Research",     "endpoint": "dashboard.research"},
    {"key": "restore",    "label": "Durability",   "endpoint": "dashboard.restore"},
]
```

The endpoint name `dashboard.alerts` will be defined in H.3. Until H.3 lands, the tab nav will fail to render with `BuildError`. That's expected — H.1 and H.3 must ship together OR H.1 lands second.

**Decision: ship H.3 (route stub returning a 503-ish "not yet wired" page) BEFORE H.1.** That means the implementation order is H.3 → H.2 → H.1 → H.4/H.5/H.6/H.7. The plan task numbering reflects logical order (tab visible → data → route → feed → detail → tests → docs), but commit order swaps H.1 to land after H.3.

- [ ] **Step 2: AST-check**

```powershell
python -c "import ast; ast.parse(open('omega_dashboard/routes.py').read()); print('AST OK')"
```

Expected: `AST OK`

- [ ] **Step 3: Smoke-check the tab loads**

After H.3 has shipped (so the endpoint exists), in PowerShell:

```powershell
python -c "from app import app; client = app.test_client(); r = client.get('/dashboard'); print('status', r.status_code); print('alerts in body', b'Alerts' in r.data)"
```

Expected: status 200 (or 302 to /login if no session — that's also fine, means routes loaded), and either "Alerts" present or a redirect status. Failure mode: `BuildError: Could not build url for endpoint 'dashboard.alerts'` — means H.3 hasn't shipped yet.

- [ ] **Step 4: Commit**

```powershell
git add omega_dashboard/routes.py
git commit -m @'
Patch H.1: Add Alerts tab to PAGE_TABS

Inserts the new "Alerts" tab between Market View and Portfolio.
Endpoint dashboard.alerts is defined in H.3.
'@
```

---

## Task H.2: `alerts_data.py` read-side data layer

**Files:**
- Create: `omega_dashboard/alerts_data.py` — module owning ALL reads against `/var/backtest/desk.db`
- Test: `test_alerts_data.py` is created in H.6; this task ships the production module first, tests follow

H.2 ships in three logical chunks committed together as one patch: constants + helpers (already shown above), bucketing + structure_summary formatter, and `list_alerts` + `get_alert_detail`. The order of steps below introduces each function once with its complete code so the engineer never has to context-switch.

- [ ] **Step 1: Create the file with module docstring, imports, constants, and helpers**

Write `omega_dashboard/alerts_data.py` starting with the constants-and-helpers block from the top of this plan (the `# omega_dashboard/alerts_data.py — top of file` block). Stop after `_flag_on(var)`.

- [ ] **Step 2: Add the time-bucketing function**

The bucket logic uses Chicago time. "Today" = same calendar date as `now` in CT. "Yesterday" = previous CT calendar date. "This week" = within the last 7 calendar days, excluding today and yesterday. "Earlier" = anything older.

Append to `alerts_data.py`:

```python
def _bucket(fired_at_micros: int, now_micros: int) -> str:
    """Returns one of 'today' / 'yesterday' / 'this_week' / 'earlier'.
    All bucket boundaries are CT calendar-date based (handles DST via
    zoneinfo). fired_at and now are both UTC microseconds."""
    fired_dt = datetime.fromtimestamp(fired_at_micros / 1_000_000, tz=timezone.utc)
    now_dt = datetime.fromtimestamp(now_micros / 1_000_000, tz=timezone.utc)
    fired_ct_date = fired_dt.astimezone(CHICAGO_TZ).date()
    now_ct_date = now_dt.astimezone(CHICAGO_TZ).date()
    delta_days = (now_ct_date - fired_ct_date).days
    if delta_days <= 0:
        return "today"
    if delta_days == 1:
        return "yesterday"
    if delta_days <= 7:
        return "this_week"
    return "earlier"
```

- [ ] **Step 3: Add the defensive `format_structure_summary` formatter**

Per-engine structure summary built from the `suggested_structure` JSON column. MUST tolerate missing keys, unknown engine types, and malformed dicts without raising — return a fallback string instead. Card view rule: NO DTE ever. (DTE is shown only on the detail page with explicit convention tagging.)

Append to `alerts_data.py`:

```python
def format_structure_summary(engine: str, structure: Any,
                             classification: Optional[str] = None,
                             direction: Optional[str] = None) -> str:
    """Build the one-line summary shown on each card.

    Defensive: tolerates missing keys, unknown engine types, and
    malformed structure values. Returns a fallback string rather
    than raising. Never includes DTE — DTE convention varies per
    engine and is shown only on the detail page with a tag.

    For v2_5d the structure JSON is just {"type": "evaluation"} —
    grade and bias live on the alert row's classification and
    direction columns. Callers pass those in.
    """
    try:
        if not isinstance(structure, dict):
            structure = {}
        stype = structure.get("type")
        if engine == "long_call_burst" or stype == "long_call":
            strike = structure.get("strike")
            expiry = structure.get("expiry")
            entry = structure.get("entry_mark")
            if strike is None or expiry is None:
                return f"{engine} [partial data]"
            entry_str = f" @ ${entry:.2f}" if isinstance(entry, (int, float)) else ""
            return f"${strike:.2f}C {_short_expiry(expiry)}{entry_str}"
        if stype in ("bull_put", "bear_call"):
            short = structure.get("short_strike")
            long_ = structure.get("long_strike")
            expiry = structure.get("expiry")
            credit = structure.get("credit")
            label = "BULL PUT" if stype == "bull_put" else "BEAR CALL"
            if short is None or long_ is None or expiry is None:
                return f"{engine} [partial data]"
            credit_str = f" (credit ${credit:.2f})" if isinstance(credit, (int, float)) else ""
            return f"{short:.0f}/{long_:.0f} {label} {_short_expiry(expiry)}{credit_str}"
        if engine == "v2_5d":
            # Grade/bias come from row columns, NOT structure JSON.
            grade = (classification or "").replace("GRADE_", "")
            bias = direction or ""
            if grade and bias:
                return f"Grade {grade} {bias}"
            if grade:
                return f"Grade {grade}"
            if bias:
                return f"v2_5d {bias}"
            return "v2_5d evaluation"
        if engine == "oi_flow_conviction":
            strike = structure.get("strike")
            kind = structure.get("right") or structure.get("type") or "C"
            kind_letter = "C" if "call" in str(kind).lower() or kind == "C" else "P"
            expiry = structure.get("expiry")
            if strike is None or expiry is None:
                return "conviction [partial data]"
            return f"${strike:.2f}{kind_letter} {_short_expiry(expiry)} (flow conviction)"
        return f"{engine} [unknown type]"
    except Exception as e:
        log.warning(f"alerts_data: format_structure_summary({engine}) failed: {e}")
        return f"{engine} [parse error]"


def _short_expiry(expiry: Any) -> str:
    """'2026-05-15' -> '5/15'. Returns the raw value as str on parse fail."""
    try:
        s = str(expiry)
        # ISO date YYYY-MM-DD
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return f"{int(s[5:7])}/{int(s[8:10])}"
        return s
    except Exception:
        return str(expiry)
```

- [ ] **Step 4: Add the status-strip helper**

Reads env vars at call time. No DB access. Returns the dict the template renders into the top status pill.

```python
def _status_strip(buckets: Dict[str, List[Dict[str, Any]]],
                  newest_fired_at_micros: Optional[int]) -> Dict[str, Any]:
    """Build the page-top status strip dict.

    - master_on: green-dot vs gray
    - engines_on: list of engine slugs whose per-engine flag is true
    - daemons_on: list of daemon names whose flag is true
    - count_today: len(buckets['today'])
    - last_fire_ct: 'HH:MM' in CT, or None if no alerts fetched
    """
    master_on = _flag_on("RECORDER_ENABLED")
    engines_on = [slug for slug, var in ENGINE_FLAG_VARS.items() if _flag_on(var)]
    daemons_on = [name for name, var in DAEMON_FLAG_VARS.items() if _flag_on(var)]
    last_fire_ct = None
    if newest_fired_at_micros:
        dt = datetime.fromtimestamp(newest_fired_at_micros / 1_000_000, tz=timezone.utc)
        last_fire_ct = dt.astimezone(CHICAGO_TZ).strftime("%H:%M")
    return {
        "master_on": master_on,
        "engines_on": engines_on,
        "daemons_on": daemons_on,
        "count_today": len(buckets.get("today", [])),
        "last_fire_ct": last_fire_ct,
    }
```

- [ ] **Step 5: Add `_format_card` (one alert row → display dict)**

```python
def _format_card(row: sqlite3.Row, now_micros: int) -> Dict[str, Any]:
    """Build the per-card dict consumed by _alert_card.html."""
    engine = row["engine"]
    display = ENGINE_DISPLAY.get(engine, {
        "icon": "•", "label": engine, "stripe": "unknown",
    })
    try:
        struct = json.loads(row["suggested_structure"]) if row["suggested_structure"] else {}
    except Exception:
        struct = {}
    summary = format_structure_summary(engine, struct,
                                       classification=row["classification"],
                                       direction=row["direction"])
    fired_dt = datetime.fromtimestamp(row["fired_at"] / 1_000_000, tz=timezone.utc)
    fired_ct = fired_dt.astimezone(CHICAGO_TZ)
    elapsed_seconds = (now_micros - row["fired_at"]) // 1_000_000
    return {
        "alert_id": row["alert_id"],
        "engine": engine,
        "engine_icon": display["icon"],
        "engine_label": display["label"],
        "stripe": display["stripe"],
        "ticker": row["ticker"],
        "direction": row["direction"],
        "classification": row["classification"],
        "structure_summary": summary,
        "fired_at_ct": fired_ct.strftime("%H:%M:%S CT"),
        "fired_at_relative": _humanize_elapsed(elapsed_seconds),
        "elapsed_seconds": int(elapsed_seconds),
        "is_recent": elapsed_seconds < 5 * 60,    # CSS pulse when True
        "is_old": elapsed_seconds > 24 * 60 * 60, # CSS muted when True
        "parent_alert_id": row["parent_alert_id"],
    }


def _humanize_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 60 * 60:
        return f"{seconds // 60}m ago"
    if seconds < 24 * 60 * 60:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
```

- [ ] **Step 6: Add `list_alerts` — single-query, bucketed in Python, capped at LIST_LIMIT**

```python
def list_alerts(limit: int = LIST_LIMIT,
                now_micros: Optional[int] = None) -> Dict[str, Any]:
    """Return bucketed feed payload. ONE SQL query against alerts table.
    Does NOT fetch features/outcomes/track per row (no N+1).

    Empty-state matrix:
      1. DB file missing      -> available=False, helpful error
      2. DB exists, zero rows -> available=True, all buckets empty
      3. Rows but all >7d old -> available=True, only 'earlier' populated
      4. Normal               -> available=True, populated buckets
    """
    now = now_micros if now_micros is not None else _now_micros()
    payload: Dict[str, Any] = {
        "available": False,
        "error": None,
        "today": [], "yesterday": [], "this_week": [], "earlier": [],
        "total_count": 0,
        "status": _status_strip({}, None),
    }
    try:
        conn = _open_ro()
    except sqlite3.OperationalError as e:
        payload["error"] = (
            f"Recorder DB not found at {_db_path()}. "
            f"Check RECORDER_ENABLED is set in production env."
        )
        log.info(f"alerts_data: DB open failed: {e}")
        return payload
    try:
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "classification, direction, suggested_structure, "
                "suggested_dte, parent_alert_id "
                "FROM alerts ORDER BY fired_at DESC LIMIT ?",
                (int(limit),)
            ).fetchall()
        except sqlite3.DatabaseError as e:
            payload["error"] = f"Recorder DB unreadable: {e}"
            log.warning(f"alerts_data: list_alerts query failed: {e}")
            return payload
    finally:
        try:
            conn.close()
        except Exception:
            pass
    payload["available"] = True
    newest = rows[0]["fired_at"] if rows else None
    for row in rows:
        try:
            card = _format_card(row, now)
            bucket = _bucket(row["fired_at"], now)
            payload[bucket].append(card)
        except Exception as e:
            log.warning(f"alerts_data: card build failed for {row['alert_id']}: {e}")
            continue
    payload["total_count"] = (
        len(payload["today"]) + len(payload["yesterday"])
        + len(payload["this_week"]) + len(payload["earlier"])
    )
    payload["status"] = _status_strip(payload, newest)
    return payload
```

- [ ] **Step 7: Add `get_alert_detail` — six single-row queries for ONE alert**

```python
def get_alert_detail(alert_id: str,
                     now_micros: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return full detail dict for one alert, or None if not found.
    Performs at most 6 single-table queries. Never raises — returns None
    on any failure path (caller renders 404)."""
    if not isinstance(alert_id, str) or not _UUID_V4.match(alert_id):
        log.info(f"alerts_data: rejected non-UUID alert_id={alert_id!r}")
        return None
    now = now_micros if now_micros is not None else _now_micros()
    try:
        conn = _open_ro()
    except sqlite3.OperationalError as e:
        log.info(f"alerts_data: DB open failed in detail: {e}")
        return None
    try:
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "classification, direction, suggested_structure, "
                "suggested_dte, spot_at_fire, canonical_snapshot, "
                "raw_engine_payload, parent_alert_id, posted_to_telegram, "
                "telegram_chat, suppression_reason "
                "FROM alerts WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
            if row is None:
                return None
            features = conn.execute(
                "SELECT feature_name, feature_value, feature_text "
                "FROM alert_features WHERE alert_id = ? "
                "ORDER BY feature_name",
                (alert_id,)
            ).fetchall()
            track = conn.execute(
                "SELECT elapsed_seconds, sampled_at, underlying_price, "
                "structure_mark, structure_pnl_pct, structure_pnl_abs, market_state "
                "FROM alert_price_track WHERE alert_id = ? "
                "ORDER BY elapsed_seconds",
                (alert_id,)
            ).fetchall()
            outcomes = conn.execute(
                "SELECT horizon, outcome_at, underlying_price, structure_mark, "
                "pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3, "
                "max_favorable_pct, max_adverse_pct "
                "FROM alert_outcomes WHERE alert_id = ?",
                (alert_id,)
            ).fetchall()
            parent = None
            if row["parent_alert_id"]:
                parent = conn.execute(
                    "SELECT alert_id, engine, classification, direction "
                    "FROM alerts WHERE alert_id = ?",
                    (row["parent_alert_id"],)
                ).fetchone()
            children = conn.execute(
                "SELECT alert_id, engine, classification "
                "FROM alerts WHERE parent_alert_id = ? ORDER BY fired_at",
                (alert_id,)
            ).fetchall()
        except sqlite3.DatabaseError as e:
            log.warning(f"alerts_data: detail query failed for {alert_id}: {e}")
            return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # Build the response dict (helper does the heavy lifting)
    return _build_detail_dict(row, features, track, outcomes, parent, children, now)
```

`_build_detail_dict` calls `format_structure_summary` with the alert row's
`classification` and `direction` so v2_5d alerts get "Grade A bull" instead
of "v2_5d evaluation":

```python
# Inside _build_detail_dict — see Step 8.
summary = format_structure_summary(
    engine, structure,
    classification=row["classification"],
    direction=row["direction"],
)
```

- [ ] **Step 8: Add `_build_detail_dict` — pure function that formats query results**

```python
def _build_detail_dict(row, features, track, outcomes, parent, children,
                       now_micros: int) -> Dict[str, Any]:
    """Pure formatting — no DB access. Builds the dict the detail
    template renders."""
    engine = row["engine"]
    display = ENGINE_DISPLAY.get(engine, {
        "icon": "•", "label": engine, "stripe": "unknown",
    })
    try:
        structure = json.loads(row["suggested_structure"]) if row["suggested_structure"] else {}
    except Exception:
        structure = {}
    try:
        snapshot = json.loads(row["canonical_snapshot"]) if row["canonical_snapshot"] else {}
    except Exception:
        snapshot = {}
    try:
        raw_payload = json.loads(row["raw_engine_payload"]) if row["raw_engine_payload"] else {}
    except Exception:
        raw_payload = {}
    fired_dt = datetime.fromtimestamp(row["fired_at"] / 1_000_000, tz=timezone.utc)
    fired_ct = fired_dt.astimezone(CHICAGO_TZ)
    dte_convention = DTE_CONVENTION.get(engine)
    dte_label = None
    if row["suggested_dte"] is not None and dte_convention:
        unit = "trading day" if dte_convention == "trading" else "calendar day"
        n = int(row["suggested_dte"])
        dte_label = f"{n} {unit}{'s' if n != 1 else ''}"
    feature_rows = [
        {"name": f["feature_name"],
         "value": (f["feature_value"] if f["feature_value"] is not None
                   else f["feature_text"])}
        for f in features
    ]
    track_rows = [
        {"elapsed_seconds": t["elapsed_seconds"],
         "structure_mark": t["structure_mark"],
         "structure_pnl_pct": t["structure_pnl_pct"],
         "structure_pnl_abs": t["structure_pnl_abs"],
         "market_state": t["market_state"]}
        for t in track
    ]
    outcome_rows = [
        {"horizon": o["horizon"],
         "pnl_pct": o["pnl_pct"], "pnl_abs": o["pnl_abs"],
         "hit_pt1": bool(o["hit_pt1"]), "hit_pt2": bool(o["hit_pt2"]),
         "hit_pt3": bool(o["hit_pt3"]),
         "max_favorable_pct": o["max_favorable_pct"],
         "max_adverse_pct": o["max_adverse_pct"]}
        for o in outcomes
    ]
    parent_dict = None
    if parent is not None:
        parent_engine = parent["engine"]
        parent_display = ENGINE_DISPLAY.get(parent_engine, {})
        parent_dict = {
            "alert_id": parent["alert_id"],
            "engine_label": parent_display.get("label", parent_engine),
            "classification": parent["classification"],
            "direction": parent["direction"],
        }
    children_list = []
    for c in children:
        c_engine = c["engine"]
        c_display = ENGINE_DISPLAY.get(c_engine, {})
        children_list.append({
            "alert_id": c["alert_id"],
            "engine_label": c_display.get("label", c_engine),
            "classification": c["classification"],
        })
    return {
        "alert_id": row["alert_id"],
        "engine": engine,
        "engine_icon": display["icon"],
        "engine_label": display["label"],
        "stripe": display["stripe"],
        "engine_version": row["engine_version"],
        "ticker": row["ticker"],
        "classification": row["classification"],
        "direction": row["direction"],
        "structure": structure,
        "structure_summary": format_structure_summary(
            engine, structure,
            classification=row["classification"],
            direction=row["direction"],
        ),
        "spot_at_fire": row["spot_at_fire"],
        "fired_at_ct": fired_ct.strftime("%Y-%m-%d %H:%M:%S CT"),
        "dte_value": row["suggested_dte"],
        "dte_label": dte_label,                    # None when no convention
        "parent": parent_dict,
        "children": children_list,
        "features": feature_rows,
        "track": track_rows,
        "outcomes": outcome_rows,
        "pnl_svg": _build_pnl_svg(track_rows),     # empty string if no rows
        "canonical_snapshot": snapshot,
        "raw_engine_payload": raw_payload,
        "telegram_chat": row["telegram_chat"],
        "posted_to_telegram": bool(row["posted_to_telegram"]),
    }
```

- [ ] **Step 9: Add `_build_pnl_svg` — server-rendered inline SVG**

```python
def _build_pnl_svg(track_rows: List[Dict[str, Any]]) -> str:
    """Server-rendered SVG line chart of structure_pnl_pct over time.

    Returns "" if no track rows. Width 720, height 180. Polyline only —
    no axis labels in SVG; the template renders the legend below.
    Defensive: skips rows where pnl_pct is None.
    """
    pts = [(r["elapsed_seconds"], r["structure_pnl_pct"])
           for r in track_rows
           if r.get("structure_pnl_pct") is not None
           and r.get("elapsed_seconds") is not None]
    if not pts:
        return ""
    width, height, pad = 720, 180, 16
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x_min, x_max = min(xs), max(xs) or 1
    y_min, y_max = min(ys), max(ys)
    if y_max == y_min:
        y_max = y_min + 1
    def _x(v): return pad + (v - x_min) * (width - 2 * pad) / (x_max - x_min or 1)
    def _y(v): return height - pad - (v - y_min) * (height - 2 * pad) / (y_max - y_min)
    poly = " ".join(f"{_x(x):.1f},{_y(y):.1f}" for x, y in pts)
    zero_line = ""
    if y_min <= 0 <= y_max:
        zy = _y(0)
        zero_line = (f'<line x1="{pad}" y1="{zy:.1f}" x2="{width - pad}" '
                     f'y2="{zy:.1f}" stroke="#6D5A36" stroke-dasharray="3 3" />')
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" class="alerts-pnl-chart">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#171A1D" />'
        f'{zero_line}'
        f'<polyline fill="none" stroke="#73B27B" stroke-width="1.6" '
        f'points="{poly}" />'
        f'</svg>'
    )
```

- [ ] **Step 10: AST-check**

```powershell
python -c "import ast; ast.parse(open('omega_dashboard/alerts_data.py').read()); print('AST OK')"
```

- [ ] **Step 11: Smoke-import in PowerShell**

```powershell
python -c "from omega_dashboard.alerts_data import list_alerts, get_alert_detail, format_structure_summary; print('import OK')"
```

Expected: `import OK`. If it fails, fix syntax/import issues before moving on.

- [ ] **Step 12: Commit**

```powershell
git add omega_dashboard/alerts_data.py
git commit -m @'
Patch H.2: Read-side data layer for the alerts feed

omega_dashboard/alerts_data.py — owns its own RO sqlite3 connection
to /var/backtest/desk.db. Never imports alert_recorder write internals.
Provides list_alerts() (single-query feed, bucketed in Python,
capped at LIST_LIMIT=200), get_alert_detail() (6 single-row queries
for one alert), defensive format_structure_summary(), CT bucketing
via zoneinfo, env-var status reads, and a server-rendered SVG
chart helper. No DTE on cards; per-engine convention tag on detail.
'@
```

---

## Task H.3: Routes for `/alerts`, `/alerts/data`, `/alerts/<alert_id>`

**Files:**
- Modify: `omega_dashboard/routes.py` — append three routes near the existing `/trading` routes (~line 325)

- [ ] **Step 1: Add the three routes**

Append after the existing `/trading/data` route (`omega_dashboard/routes.py:325`):

```python
# v11.7 (Patch H.3): alerts feed routes. Read-only against the
# recorder DB; mirrors the /trading + /trading/data pattern.

@dashboard_bp.route("/alerts", methods=["GET"])
@login_required
def alerts():
    from . import alerts_data
    page_data = alerts_data.list_alerts()
    return render_page(
        "dashboard/alerts.html",
        page_key="alerts",
        page_data=page_data,
    )


@dashboard_bp.route("/alerts/data", methods=["GET"])
@login_required
def alerts_data_json():
    from . import alerts_data
    payload = alerts_data.list_alerts()
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard_bp.route("/alerts/<string:alert_id>", methods=["GET"])
@login_required
def alerts_detail(alert_id):
    from . import alerts_data
    detail = alerts_data.get_alert_detail(alert_id)
    if detail is None:
        # 404 with friendly empty-state, not raw Flask 404.
        return render_page(
            "dashboard/alerts_detail.html",
            page_key="alerts",
            page_data={"available": False,
                       "error": f"Alert {alert_id} not found.",
                       "detail": None},
        ), 404
    return render_page(
        "dashboard/alerts_detail.html",
        page_key="alerts",
        page_data={"available": True, "error": None, "detail": detail},
    )
```

`<string:alert_id>` is Flask's default converter — it accepts everything except slashes. The defensive check inside `get_alert_detail` (UUID v4 regex match) is the real gate — anything not matching the UUID shape returns None, the route then renders the friendly 404 page.

- [ ] **Step 2: AST-check**

```powershell
python -c "import ast; ast.parse(open('omega_dashboard/routes.py').read()); print('AST OK')"
```

- [ ] **Step 3: Confirm endpoint registered**

```powershell
python -c "from app import app; rules = sorted(str(r) for r in app.url_map.iter_rules() if '/alerts' in str(r)); print(rules)"
```

Expected: 3 rules — `/alerts`, `/alerts/data`, `/alerts/<string:alert_id>`. The templates referenced don't exist yet — that's fine, the route registration succeeds independently.

- [ ] **Step 4: Commit**

```powershell
git add omega_dashboard/routes.py
git commit -m @'
Patch H.3: Routes for /alerts feed and /alerts/<alert_id> detail

Three new login-required routes mirror the /trading + /trading/data
pattern. /alerts and /alerts/data return the same payload (HTML vs
JSON). /alerts/<string:alert_id> returns 404 + friendly page when
get_alert_detail returns None. Templates ship in H.4 / H.5.
'@
```

---

## Task H.4: Feed template (`alerts.html` + `_alert_card.html`) + CSS

**Files:**
- Create: `omega_dashboard/templates/dashboard/alerts.html`
- Create: `omega_dashboard/templates/dashboard/_alert_card.html`
- Modify: `omega_dashboard/static/omega.css` — append alerts-page styles

The feed page has three zones: status strip, today's cards, collapsible historical. The polling loop swaps `innerHTML` of the card containers every 10 seconds. Pulse animation and muting are pure CSS. Initial render uses `page_data` from H.3.

- [ ] **Step 1: Create `_alert_card.html` partial**

```jinja
{# v11.7 (Patch H.4): single alert card.
   Used by alerts.html initial render AND by the polling JS-rendered
   payload (the JS calls a POST endpoint? No — the JS just rebuilds
   from JSON. To keep server and JS in sync, the JSON endpoint
   returns the same fields the template reads here, and the JS
   builds DOM matching this structure verbatim).

   Card classes:
     .alert-card                  — base
     .alert-card-engine-{stripe}  — color-coded left border
     .alert-card-recent           — pulse animation when <5min old
     .alert-card-old              — muted opacity when >24h old
#}
<a class="alert-card alert-card-engine-{{ card.stripe }}
          {% if card.is_recent %}alert-card-recent{% endif %}
          {% if card.is_old %}alert-card-old{% endif %}"
   href="{{ url_for('dashboard.alerts_detail', alert_id=card.alert_id) }}"
   data-alert-id="{{ card.alert_id }}">

  <div class="alert-card-line1">
    <span class="alert-card-time num">{{ card.fired_at_ct }}</span>
    <span class="alert-card-relative">{{ card.fired_at_relative }}</span>
    {# Status badge slot — populated by the JSON endpoint when track exists #}
    <span class="alert-card-badge">{% if card.is_recent %}[active]{% endif %}</span>
  </div>

  <div class="alert-card-line2">
    <span class="alert-card-engine">{{ card.engine_icon }} {{ card.engine_label }}</span>
    <span class="alert-card-ticker">{{ card.ticker }}</span>
    {% if card.direction == 'bull' %}
      <span class="alert-card-chevron positive">▲</span>
    {% elif card.direction == 'bear' %}
      <span class="alert-card-chevron negative">▼</span>
    {% endif %}
  </div>

  <div class="alert-card-line3 num">{{ card.structure_summary }}</div>

  {% if card.parent_alert_id %}
    <div class="alert-card-line4">
      <span class="alert-card-parent">↳ parent alert</span>
    </div>
  {% endif %}
</a>
```

Note: the "tracking summary" line (line 5 in the mockup — `tracking · [bar] · MFE +18% · current +7% · ★ PT1 hit`) is **deferred to Patch H.8 (V1.1)**. It needs per-card lookups against `alert_price_track` and `alert_outcomes`, which violates the no-N+1 rule. The clean solution is batched IN-clause aggregate queries (1 alerts + 1 track-aggregate + 1 latest-sample + 1 outcomes-aggregate = 4 SQL statements regardless of row count) — explicitly out of scope for Patch H. Card view shows lines 1-4 only; tracking detail lives on the detail page. The mockup's row 5 is the V1.1 target; see the comment block at the top of `docs/superpowers/mockups/2026-05-10-alerts-mockup.html`.

- [ ] **Step 2: Create `alerts.html` (the page template)**

```jinja
{% extends "dashboard/base.html" %}

{% block title %}The Legacy Desk &mdash; Alerts{% endblock %}
{% block brand_sub %}Alerts · {{ active_account.label }}{% endblock %}

{% block content %}

<div class="alerts-page fade-in">

  {# ── Zone 1: Status strip ─────────────────────────────────── #}
  <div class="alerts-status-strip">
    {% set status = page_data.status %}
    <span class="alerts-status-dot
                 {% if status.master_on %}live{% else %}off{% endif %}"
          title="engines on: {{ status.engines_on|join(', ') if status.engines_on else 'none' }} · daemons on: {{ status.daemons_on|join(', ') if status.daemons_on else 'none' }}"></span>
    <span class="alerts-status-label">
      Recorder {% if status.master_on %}LIVE{% else %}OFF{% endif %}
    </span>
    <span class="alerts-status-sep">·</span>
    <span class="alerts-status-count num">{{ status.count_today }} today</span>
    {% if status.last_fire_ct %}
      <span class="alerts-status-sep">·</span>
      <span class="num">last fire {{ status.last_fire_ct }} CT</span>
    {% endif %}
    <span class="alerts-status-sep">·</span>
    <span class="alerts-status-refresh">refresh in <span id="alerts-refresh-countdown" class="num">10</span>s</span>
  </div>

  {# ── Empty-state matrix ──────────────────────────────────── #}
  {% if not page_data.available %}
    <div class="alerts-empty">
      {{ page_data.error or "Recorder hasn't been initialized. Check RECORDER_ENABLED is set." }}
    </div>
  {% elif page_data.total_count == 0 %}
    <div class="alerts-empty">
      No alerts captured yet. Recorder runs during market hours when env gates are flipped on.
    </div>
  {% elif page_data.today|length == 0 and page_data.yesterday|length == 0 and page_data.this_week|length == 0 and page_data.earlier|length > 0 %}
    <div class="alerts-empty alerts-empty-soft">
      No recent activity. Showing historical alerts below.
    </div>
  {% endif %}

  {# ── Zone 2: Today ────────────────────────────────────────── #}
  {% if page_data.today %}
    <section class="alerts-section">
      <h2 class="alerts-section-heading">Today ({{ page_data.today|length }})</h2>
      <div class="alerts-cards" id="alerts-cards-today">
        {% for card in page_data.today %}
          {% include 'dashboard/_alert_card.html' %}
        {% endfor %}
      </div>
    </section>
  {% endif %}

  {# ── Zone 3: Historical (collapsed) ──────────────────────── #}
  {% if page_data.yesterday %}
    <details class="alerts-section alerts-section-historical">
      <summary><h2 class="alerts-section-heading">Yesterday ({{ page_data.yesterday|length }})</h2></summary>
      <div class="alerts-cards" id="alerts-cards-yesterday">
        {% for card in page_data.yesterday %}
          {% include 'dashboard/_alert_card.html' %}
        {% endfor %}
      </div>
    </details>
  {% endif %}

  {% if page_data.this_week %}
    <details class="alerts-section alerts-section-historical">
      <summary><h2 class="alerts-section-heading">This week ({{ page_data.this_week|length }})</h2></summary>
      <div class="alerts-cards" id="alerts-cards-this-week">
        {% for card in page_data.this_week %}
          {% include 'dashboard/_alert_card.html' %}
        {% endfor %}
      </div>
    </details>
  {% endif %}

  {% if page_data.earlier %}
    <details class="alerts-section alerts-section-historical">
      <summary><h2 class="alerts-section-heading">Earlier ({{ page_data.earlier|length }})</h2></summary>
      <div class="alerts-cards" id="alerts-cards-earlier">
        {% for card in page_data.earlier %}
          {% include 'dashboard/_alert_card.html' %}
        {% endfor %}
      </div>
    </details>
  {% endif %}

</div>

<script>
(function () {
  'use strict';
  // v11.7 (Patch H.4): 10s polling that fully replaces card grids.
  // Simple innerHTML swap — no diff, no per-card animation collisions.
  const POLL_INTERVAL_MS = 10000;
  const ENDPOINT = "{{ url_for('dashboard.alerts_data_json') }}";
  const DETAIL_BASE = "/alerts/";
  const countdownEl = document.getElementById('alerts-refresh-countdown');
  let secondsLeft = POLL_INTERVAL_MS / 1000;
  // Pause polling while the user is hovering a card — prevents the
  // 10s innerHTML swap from racing a click. Tracked via delegated
  // mouseover/mouseout on the .alerts-page container.
  let isHovering = false;
  const pageEl = document.querySelector('.alerts-page');
  if (pageEl) {
    pageEl.addEventListener('mouseover', (e) => {
      if (e.target.closest('.alert-card')) isHovering = true;
    });
    pageEl.addEventListener('mouseout', (e) => {
      if (e.target.closest('.alert-card')) isHovering = false;
    });
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function renderCard(c) {
    const recent = c.is_recent ? ' alert-card-recent' : '';
    const old_   = c.is_old    ? ' alert-card-old'    : '';
    const chev = c.direction === 'bull'
      ? '<span class="alert-card-chevron positive">▲</span>'
      : c.direction === 'bear'
        ? '<span class="alert-card-chevron negative">▼</span>'
        : '';
    const parent = c.parent_alert_id
      ? '<div class="alert-card-line4"><span class="alert-card-parent">↳ parent alert</span></div>'
      : '';
    const badge = c.is_recent ? '[active]' : '';
    return (
      '<a class="alert-card alert-card-engine-' + escapeHtml(c.stripe) + recent + old_ +
      '" href="' + DETAIL_BASE + encodeURIComponent(c.alert_id) +
      '" data-alert-id="' + escapeHtml(c.alert_id) + '">' +
      '<div class="alert-card-line1">' +
        '<span class="alert-card-time num">' + escapeHtml(c.fired_at_ct) + '</span>' +
        '<span class="alert-card-relative">' + escapeHtml(c.fired_at_relative) + '</span>' +
        '<span class="alert-card-badge">' + escapeHtml(badge) + '</span>' +
      '</div>' +
      '<div class="alert-card-line2">' +
        '<span class="alert-card-engine">' + escapeHtml(c.engine_icon) + ' ' + escapeHtml(c.engine_label) + '</span>' +
        '<span class="alert-card-ticker">' + escapeHtml(c.ticker) + '</span>' + chev +
      '</div>' +
      '<div class="alert-card-line3 num">' + escapeHtml(c.structure_summary) + '</div>' +
      parent +
      '</a>'
    );
  }

  function refreshBucket(elementId, cards) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.innerHTML = (cards || []).map(renderCard).join('');
  }

  function poll() {
    if (isHovering) { secondsLeft = POLL_INTERVAL_MS / 1000; return; }
    fetch(ENDPOINT, { credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(payload => {
        if (!payload.available) return;
        refreshBucket('alerts-cards-today',     payload.today);
        refreshBucket('alerts-cards-yesterday', payload.yesterday);
        refreshBucket('alerts-cards-this-week', payload.this_week);
        refreshBucket('alerts-cards-earlier',   payload.earlier);
      })
      .catch(e => { /* swallow — keep polling */ })
      .finally(() => { secondsLeft = POLL_INTERVAL_MS / 1000; });
  }

  setInterval(() => {
    secondsLeft -= 1;
    if (secondsLeft <= 0) { poll(); secondsLeft = POLL_INTERVAL_MS / 1000; }
    if (countdownEl) countdownEl.textContent = secondsLeft;
  }, 1000);
})();
</script>

{% endblock %}
```

- [ ] **Step 3: Append CSS to `omega_dashboard/static/omega.css`**

Append the following to the end of `omega.css`:

```css
/* ───────────────────────────────────────────────────────────
   v11.7 (Patch H.4): Alerts feed page styles.
   Reuses --bg-panel, --border-bright, --positive, --negative
   from the root variable block.
   ─────────────────────────────────────────────────────────── */
.alerts-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 24px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}

.alerts-status-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--border-bright);
  font-size: 11px;
}
.alerts-status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #555;
  display: inline-block;
}
.alerts-status-dot.live { background: var(--positive-bright); box-shadow: 0 0 6px var(--positive); }
.alerts-status-dot.off  { background: #555; }
.alerts-status-label { font-weight: 600; }
.alerts-status-sep { opacity: 0.5; }
.alerts-status-count, .alerts-status-refresh { opacity: 0.85; }

.alerts-empty {
  padding: 24px 16px;
  background: var(--bg-panel);
  border: 1px dashed var(--border-bright);
  border-radius: 4px;
  text-align: center;
  opacity: 0.85;
}
.alerts-empty-soft { opacity: 0.65; font-size: 11px; }

.alerts-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.alerts-section-heading {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin: 0;
  opacity: 0.85;
}
.alerts-section-historical > summary {
  cursor: pointer;
  list-style: none;
}
.alerts-section-historical > summary::-webkit-details-marker { display: none; }
.alerts-section-historical > summary::before {
  content: '▶ ';
  display: inline-block;
  transition: transform 0.15s;
}
.alerts-section-historical[open] > summary::before { content: '▼ '; }

.alerts-cards {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.alert-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 14px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-left: 4px solid var(--border-bright);
  text-decoration: none;
  color: inherit;
  transition: border-color 0.15s, transform 0.05s;
}
.alert-card:hover {
  border-color: var(--border-bright);
  transform: translateX(1px);
}
.alert-card-engine-lcb        { border-left-color: var(--warn); }
.alert-card-engine-v25d       { border-left-color: var(--brass-bright); }
.alert-card-engine-credit     { border-left-color: var(--partner); }
.alert-card-engine-conviction { border-left-color: var(--mine); }
.alert-card-engine-unknown    { border-left-color: var(--border-bright); }

.alert-card-recent { animation: alertPulse 2s ease-in-out infinite; }
@keyframes alertPulse {
  0%, 100% { border-left-width: 4px; }
  50%      { border-left-width: 6px; }
}
.alert-card-old { opacity: 0.6; }

.alert-card-line1 {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 10px;
  opacity: 0.75;
}
.alert-card-badge { margin-left: auto; }
.alert-card-line2 {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  font-weight: 600;
}
.alert-card-engine { letter-spacing: 0.04em; }
.alert-card-ticker { font-weight: 700; }
.alert-card-chevron.positive { color: var(--positive-bright); }
.alert-card-chevron.negative { color: var(--negative-soft); }
.alert-card-line3 { font-size: 11px; opacity: 0.92; }
.alert-card-line4 { font-size: 10px; opacity: 0.7; font-style: italic; padding-left: 12px; }
```

- [ ] **Step 4: AST-check (templates are not Python; CSS is not validated). Smoke-render the page.**

```powershell
python -c "from app import app; client = app.test_client(); client.set_cookie('localhost', 'session', 'test'); r = client.get('/alerts'); print('status', r.status_code); print(b'alerts-page' in r.data)"
```

Expected: status 200 (if test_client honors a faked session) or 302 to /login. Either is fine — confirms the template renders without a Jinja error. If status is 500, read the response body to see the Jinja error.

The simplest reliable smoke is to visit the page in a real browser after `flask run` with `RECORDER_DB_PATH` pointed at a temp DB. Document this in the commit message.

- [ ] **Step 5: Commit**

```powershell
git add omega_dashboard/templates/dashboard/alerts.html `
        omega_dashboard/templates/dashboard/_alert_card.html `
        omega_dashboard/static/omega.css
git commit -m @'
Patch H.4: Alerts feed template + CSS

Three zones: status strip (live/off + count + last fire +
refresh countdown), today's cards (full width, color-coded
engine stripe, pulse for <5min, muted for >24h), and
collapsible Yesterday / This week / Earlier sections.
10s polling via vanilla JS with full innerHTML swap of card
grids. Pulse and muted are pure CSS. Four empty states all
covered: missing DB, empty DB, all-old, normal.
'@
```

---

## Task H.5: Detail template (`alerts_detail.html`) + CSS

**Files:**
- Create: `omega_dashboard/templates/dashboard/alerts_detail.html`
- Modify: `omega_dashboard/static/omega.css` — append detail-page styles

The detail page renders the full payload returned by `get_alert_detail`. Layered: header (most relevant) → structure → SVG chart → outcomes → features → collapsible raw JSON. Every sub-section handles its empty case explicitly.

- [ ] **Step 1: Create `alerts_detail.html`**

```jinja
{% extends "dashboard/base.html" %}

{% block title %}The Legacy Desk &mdash; Alert Detail{% endblock %}
{% block brand_sub %}Alert · {{ active_account.label }}{% endblock %}

{% block content %}

<div class="alerts-detail-page fade-in">

  <div class="alerts-detail-back">
    <a href="{{ url_for('dashboard.alerts') }}" class="alerts-detail-back-link">← back to feed</a>
  </div>

  {% if not page_data.available %}
    <div class="alerts-empty">
      {{ page_data.error or "Alert not found." }}
    </div>
  {% else %}
    {% set d = page_data.detail %}

    {# ── Header ──────────────────────────────────────────── #}
    <div class="alerts-detail-header alert-card-engine-{{ d.stripe }}">
      <div class="alerts-detail-header-row">
        <span class="alerts-detail-engine">{{ d.engine_icon }} {{ d.engine_label }}</span>
        <span class="alerts-detail-ticker">{{ d.ticker }}</span>
        {% if d.direction == 'bull' %}<span class="alert-card-chevron positive">▲</span>
        {% elif d.direction == 'bear' %}<span class="alert-card-chevron negative">▼</span>{% endif %}
      </div>
      <div class="alerts-detail-meta num">
        <span>{{ d.fired_at_ct }}</span>
        <span>·</span>
        <span title="full alert id">{{ d.alert_id[:8] }}…</span>
        <span>·</span>
        <span>v{{ d.engine_version }}</span>
        {% if d.classification %}
          <span>·</span>
          <span>{{ d.classification }}</span>
        {% endif %}
      </div>
    </div>

    {# ── Parent / children linkage ──────────────────────── #}
    {% if d.parent %}
      <div class="alerts-detail-linkage">
        ← parent
        <a href="{{ url_for('dashboard.alerts_detail', alert_id=d.parent.alert_id) }}">
          {{ d.parent.engine_label }}
          {% if d.parent.classification %}({{ d.parent.classification }}{% if d.parent.direction %} {{ d.parent.direction }}{% endif %}){% endif %}
        </a>
      </div>
    {% endif %}
    {% if d.children %}
      <div class="alerts-detail-linkage">
        Children:
        {% for c in d.children %}
          <a href="{{ url_for('dashboard.alerts_detail', alert_id=c.alert_id) }}">
            {{ c.engine_label }}{% if c.classification %} ({{ c.classification }}){% endif %}
          </a>{% if not loop.last %} · {% endif %}
        {% endfor %}
      </div>
    {% endif %}

    {# ── Structure block ────────────────────────────────── #}
    <section class="alerts-detail-section">
      <h3 class="alerts-detail-eyebrow">Structure</h3>
      <div class="alerts-detail-structure num">
        {{ d.structure_summary }}
        {% if d.spot_at_fire %}
          <span class="alerts-detail-meta">spot at fire ${{ "%.2f"|format(d.spot_at_fire) }}</span>
        {% endif %}
      </div>
      {% if d.dte_label %}
        <div class="alerts-detail-dte">DTE: <span class="num">{{ d.dte_label }}</span></div>
      {% elif d.dte_value is not none %}
        <div class="alerts-detail-dte alerts-detail-dte-untagged">
          DTE: <span class="num">{{ d.dte_value }}</span> (engine has no convention; raw value)
        </div>
      {% endif %}
    </section>

    {# ── Price track (server-rendered SVG) ─────────────── #}
    <section class="alerts-detail-section">
      <h3 class="alerts-detail-eyebrow">Price track</h3>
      {% if d.pnl_svg %}
        {{ d.pnl_svg|safe }}
        <div class="alerts-detail-meta num">
          {{ d.track|length }} samples ·
          last sample at {{ d.track[-1].elapsed_seconds }}s elapsed ·
          last pnl {{ "%.2f"|format(d.track[-1].structure_pnl_pct or 0) }}%
        </div>
      {% else %}
        <div class="alerts-empty alerts-empty-soft">
          Price tracking not yet started — first sample lands at 60s elapsed.
        </div>
      {% endif %}
    </section>

    {# ── Outcomes table ────────────────────────────────── #}
    <section class="alerts-detail-section">
      <h3 class="alerts-detail-eyebrow">Outcomes</h3>
      {% if d.outcomes %}
        <table class="alerts-detail-table">
          <thead>
            <tr>
              <th>Horizon</th><th>PnL %</th><th>PT1</th><th>PT2</th><th>PT3</th><th>MFE %</th><th>MAE %</th>
            </tr>
          </thead>
          <tbody>
            {% for o in d.outcomes %}
              <tr>
                <td>{{ o.horizon }}</td>
                <td class="num {% if o.pnl_pct and o.pnl_pct > 0 %}positive{% elif o.pnl_pct and o.pnl_pct < 0 %}negative{% endif %}">
                  {% if o.pnl_pct is not none %}{{ "%.2f"|format(o.pnl_pct) }}{% else %}—{% endif %}
                </td>
                <td>{% if o.hit_pt1 %}✓{% else %}—{% endif %}</td>
                <td>{% if o.hit_pt2 %}✓{% else %}—{% endif %}</td>
                <td>{% if o.hit_pt3 %}✓{% else %}—{% endif %}</td>
                <td class="num">{% if o.max_favorable_pct is not none %}{{ "%.2f"|format(o.max_favorable_pct) }}{% else %}—{% endif %}</td>
                <td class="num">{% if o.max_adverse_pct is not none %}{{ "%.2f"|format(o.max_adverse_pct) }}{% else %}—{% endif %}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <div class="alerts-empty alerts-empty-soft">No outcomes computed yet.</div>
      {% endif %}
    </section>

    {# ── Features table ────────────────────────────────── #}
    <section class="alerts-detail-section">
      <h3 class="alerts-detail-eyebrow">Features ({{ d.features|length }})</h3>
      {% if d.features %}
        <table class="alerts-detail-table">
          <thead><tr><th>Name</th><th>Value</th></tr></thead>
          <tbody>
            {% for f in d.features %}
              <tr>
                <td>{{ f.name }}</td>
                <td class="num">{{ f.value }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <div class="alerts-empty alerts-empty-soft">No features captured.</div>
      {% endif %}
    </section>

    {# ── Collapsible raw JSON ───────────────────────────── #}
    <section class="alerts-detail-section">
      <details>
        <summary class="alerts-detail-eyebrow">Canonical snapshot (raw JSON)</summary>
        <pre class="alerts-detail-raw">{{ d.canonical_snapshot | tojson(indent=2) }}</pre>
      </details>
      <details>
        <summary class="alerts-detail-eyebrow">Raw engine payload</summary>
        <pre class="alerts-detail-raw">{{ d.raw_engine_payload | tojson(indent=2) }}</pre>
      </details>
    </section>
  {% endif %}

</div>

{% endblock %}
```

- [ ] **Step 2: Append detail-page CSS to `omega.css`**

```css
/* ───────────────────────────────────────────────────────────
   v11.7 (Patch H.5): Alert detail page styles.
   ─────────────────────────────────────────────────────────── */
.alerts-detail-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 24px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  max-width: 920px;
}
.alerts-detail-back-link {
  font-size: 11px;
  opacity: 0.7;
  text-decoration: none;
}
.alerts-detail-back-link:hover { opacity: 1; }
.alerts-detail-header {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-left: 4px solid var(--border-bright);
}
.alerts-detail-header-row {
  display: flex; align-items: center; gap: 12px;
  font-size: 14px; font-weight: 600;
}
.alerts-detail-engine { letter-spacing: 0.04em; }
.alerts-detail-ticker { font-weight: 700; font-size: 16px; }
.alerts-detail-meta { display: flex; gap: 6px; opacity: 0.7; font-size: 10px; }
.alerts-detail-linkage {
  font-size: 11px;
  opacity: 0.85;
  padding: 6px 10px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
}
.alerts-detail-linkage a { color: var(--brass-bright); text-decoration: none; }
.alerts-detail-linkage a:hover { text-decoration: underline; }
.alerts-detail-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.alerts-detail-eyebrow {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  opacity: 0.7;
  margin: 0;
  cursor: pointer;
}
.alerts-detail-structure {
  padding: 10px 14px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  font-size: 13px;
}
.alerts-detail-dte { font-size: 11px; opacity: 0.85; }
.alerts-detail-dte-untagged { color: var(--brass-bright); }
.alerts-detail-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
.alerts-detail-table th, .alerts-detail-table td {
  padding: 4px 8px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}
.alerts-detail-table th { opacity: 0.7; font-weight: 500; }
.alerts-detail-raw {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 12px;
  font-size: 10px;
  overflow-x: auto;
  margin-top: 4px;
}
.alerts-pnl-chart {
  width: 100%;
  height: auto;
  background: var(--bg-panel);
  border: 1px solid var(--border);
}
```

- [ ] **Step 3: Smoke-render the 404 path**

```powershell
python -c "from app import app; client = app.test_client(); r = client.get('/alerts/does-not-exist'); print('status', r.status_code); print('not found in body', b'not found' in r.data.lower())"
```

Expected: status 404 (or 302 to /login if no session). The bytes check should be true if the test_client gets past auth.

- [ ] **Step 4: Commit**

```powershell
git add omega_dashboard/templates/dashboard/alerts_detail.html `
        omega_dashboard/static/omega.css
git commit -m @'
Patch H.5: Alert detail page template + CSS

Layered detail view: header card with engine/ticker/parent linkage,
structure block (DTE shown only with explicit per-engine convention
tag), server-rendered SVG line chart of price track, outcomes table,
features table, collapsible raw JSON for canonical_snapshot and
raw_engine_payload. Every sub-section has its own empty state — no
section crashes when its data is missing. 404 path renders friendly
"alert not found" instead of raw Flask 404.
'@
```

---

## Task H.6: `test_alerts_data.py` — hermetic tests

**Files:**
- Create: `test_alerts_data.py`

Tests are hermetic: each test creates its own temp DB via `tempfile.mkdtemp`, sets `RECORDER_DB_PATH` env var, calls `db_migrate.apply_migrations(db)`, inserts synthetic rows via raw SQL (avoids importing the recorder write module at all — clean boundary), runs the assertion, tears down in `finally`. Match `test_alert_recorder.py:330-346` for the PASS/FAIL counter pattern.

- [ ] **Step 1: Write the test file with setup/teardown helpers**

Create `test_alerts_data.py`:

```python
"""Tests for omega_dashboard.alerts_data.

# v11.7 (Patch H.6): hermetic — no network, no Schwab, no Telegram,
# never touches /var/backtest/desk.db. Each test owns its own temp DB.
"""
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone


def _setup_db():
    tmpdir = tempfile.mkdtemp(prefix="alerts_h6_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown_db(tmpdir):
    os.environ.pop("RECORDER_DB_PATH", None)
    for v in ("RECORDER_ENABLED", "RECORDER_LCB_ENABLED", "RECORDER_V25D_ENABLED",
              "RECORDER_CREDIT_ENABLED", "RECORDER_CONVICTION_ENABLED",
              "RECORDER_TRACKER_ENABLED", "RECORDER_OUTCOMES_ENABLED"):
        os.environ.pop(v, None)
    shutil.rmtree(tmpdir, ignore_errors=True)


def _utc_micros_for(year, month, day, hour=12, minute=0):
    """Helper: build a UTC microseconds timestamp from a calendar date."""
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
               .timestamp() * 1_000_000)


def _insert_alert(db, alert_id, fired_at, engine="long_call_burst",
                  ticker="SPY", classification="BURST_YES", direction="bull",
                  structure=None, dte=None, parent_alert_id=None):
    structure_json = json.dumps(structure or {})
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO alerts (alert_id, fired_at, engine, engine_version, "
        "ticker, classification, direction, suggested_structure, "
        "suggested_dte, spot_at_fire, canonical_snapshot, raw_engine_payload, "
        "parent_alert_id, posted_to_telegram, telegram_chat) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_id, fired_at, engine, f"{engine}@v8.4.2", ticker,
         classification, direction, structure_json, dte, 588.30,
         json.dumps({}), json.dumps({}), parent_alert_id, 1, "main")
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 2: Add empty/missing-DB tests (states 1 + 2 of the 4-state matrix)**

```python
def test_missing_db_returns_unavailable_with_friendly_error():
    tmpdir = tempfile.mkdtemp(prefix="alerts_h6_")
    bad_path = os.path.join(tmpdir, "does-not-exist.db")
    os.environ["RECORDER_DB_PATH"] = bad_path
    try:
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts()
        assert payload["available"] is False
        assert payload["error"] is not None
        assert "RECORDER_ENABLED" in payload["error"]
        assert payload["today"] == [] and payload["total_count"] == 0
    finally:
        os.environ.pop("RECORDER_DB_PATH", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_empty_db_returns_available_with_zero_buckets():
    tmpdir, db = _setup_db()
    try:
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts()
        assert payload["available"] is True
        assert payload["error"] is None
        assert payload["total_count"] == 0
        for bucket in ("today", "yesterday", "this_week", "earlier"):
            assert payload[bucket] == []
    finally:
        _teardown_db(tmpdir)
```

- [ ] **Step 3: Add bucketing tests (the time-bucket logic, including state 3)**

```python
def _u(name):
    """Generate a deterministic UUID v4-shaped alert_id for tests.
    Uses the test name as a seed so failures are easy to map back to
    the test that wrote the row."""
    import hashlib
    h = hashlib.md5(name.encode()).hexdigest()
    # Force the version 4 nibble so it's a valid v4-shape UUID.
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-{h[16:20]}-{h[20:32]}"


def test_bucketing_today_yesterday_this_week_earlier():
    tmpdir, db = _setup_db()
    try:
        # Pin "now" to a specific instant so bucket math is deterministic.
        now = _utc_micros_for(2026, 5, 9, hour=20)  # 2026-05-09 20:00 UTC
        # Insert one alert in each bucket. Times in UTC; CT bucketing
        # handles the offset.
        ids = {b: _u(b) for b in
               ("today-1", "yesterday-1", "this-week-1", "earlier-1")}
        _insert_alert(db, ids["today-1"],     fired_at=_utc_micros_for(2026, 5, 9, 16))
        _insert_alert(db, ids["yesterday-1"], fired_at=_utc_micros_for(2026, 5, 8, 16))
        _insert_alert(db, ids["this-week-1"], fired_at=_utc_micros_for(2026, 5, 5, 16))
        _insert_alert(db, ids["earlier-1"],   fired_at=_utc_micros_for(2026, 4, 20, 16))
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        assert [c["alert_id"] for c in payload["today"]]     == [ids["today-1"]]
        assert [c["alert_id"] for c in payload["yesterday"]] == [ids["yesterday-1"]]
        assert [c["alert_id"] for c in payload["this_week"]] == [ids["this-week-1"]]
        assert [c["alert_id"] for c in payload["earlier"]]   == [ids["earlier-1"]]
        assert payload["total_count"] == 4
    finally:
        _teardown_db(tmpdir)


def test_only_old_alerts_state_3():
    """All rows >7 days old. Today/yesterday/this_week empty, earlier populated."""
    tmpdir, db = _setup_db()
    try:
        now = _utc_micros_for(2026, 5, 9, hour=20)
        old1, old2 = _u("old-1"), _u("old-2")
        _insert_alert(db, old1, fired_at=_utc_micros_for(2026, 4, 20, 16))
        _insert_alert(db, old2, fired_at=_utc_micros_for(2026, 3, 15, 16))
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        assert payload["today"] == [] and payload["yesterday"] == []
        assert payload["this_week"] == []
        assert {c["alert_id"] for c in payload["earlier"]} == {old1, old2}
    finally:
        _teardown_db(tmpdir)
```

- [ ] **Step 4: Add `format_structure_summary` tests covering each engine + malformed**

```python
def test_format_structure_summary_long_call():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("long_call_burst", {
        "type": "long_call", "strike": 215.50,
        "expiry": "2026-05-15", "entry_mark": 2.15,
    })
    assert s == "$215.50C 5/15 @ $2.15", s


def test_format_structure_summary_bull_put():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("credit_v84", {
        "type": "bull_put", "short_strike": 580, "long_strike": 575,
        "expiry": "2026-05-09", "credit": 0.85,
    })
    assert s == "580/575 BULL PUT 5/9 (credit $0.85)", s


def test_format_structure_summary_bear_call():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("credit_v84", {
        "type": "bear_call", "short_strike": 600, "long_strike": 605,
        "expiry": "2026-05-15", "credit": 1.10,
    })
    assert s == "600/605 BEAR CALL 5/15 (credit $1.10)", s


def test_format_structure_summary_v2_5d_uses_classification_and_direction():
    """v2_5d structure JSON is just {"type":"evaluation"} — grade and
    bias come from the alert row's classification and direction columns."""
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary(
        "v2_5d", {"type": "evaluation"},
        classification="GRADE_A", direction="bull",
    )
    assert s == "Grade A bull", s
    # Falls back gracefully when classification or direction are missing.
    s2 = format_structure_summary("v2_5d", {"type": "evaluation"})
    assert "v2_5d" in s2 and "evaluation" in s2, s2
    s3 = format_structure_summary("v2_5d", {"type": "evaluation"},
                                  classification="GRADE_B", direction=None)
    assert s3 == "Grade B", s3


def test_format_structure_summary_conviction():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("oi_flow_conviction", {
        "strike": 190, "right": "C", "expiry": "2026-05-15",
    })
    assert s == "$190.00C 5/15 (flow conviction)", s


def test_format_structure_summary_malformed_does_not_raise():
    """Defensive: missing keys, unknown engine, non-dict structure all
    return a fallback string instead of raising."""
    from omega_dashboard.alerts_data import format_structure_summary
    assert "[no structure]"  in format_structure_summary("long_call_burst", None)
    assert "[no structure]"  in format_structure_summary("long_call_burst", "garbage")
    assert "[partial data]"  in format_structure_summary("long_call_burst",
                                                         {"type": "long_call"})
    assert "[unknown type]"  in format_structure_summary("future_engine",
                                                         {"type": "weird"})
    assert "[partial data]"  in format_structure_summary("oi_flow_conviction",
                                                         {"right": "C"})
```

- [ ] **Step 5: Add detail tests (assembly + parent linkage + empty sub-sections)**

```python
def test_get_alert_detail_returns_full_payload():
    tmpdir, db = _setup_db()
    try:
        fired = _utc_micros_for(2026, 5, 9, 16)
        aid = _u("detail-full")
        _insert_alert(db, aid, fired_at=fired,
                      engine="long_call_burst",
                      structure={"type": "long_call", "strike": 215.5,
                                 "expiry": "2026-05-15", "entry_mark": 2.15},
                      dte=4)
        # Insert features + a track sample + an outcome via raw SQL.
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO alert_features (alert_id, feature_name, "
                     "feature_value) VALUES (?, ?, ?)",
                     (aid, "rsi", 62.5))
        conn.execute("INSERT INTO alert_features (alert_id, feature_name, "
                     "feature_text) VALUES (?, ?, ?)",
                     (aid, "regime", "BULL_BASE"))
        conn.execute("INSERT INTO alert_price_track (alert_id, elapsed_seconds, "
                     "sampled_at, underlying_price, structure_mark, "
                     "structure_pnl_pct, structure_pnl_abs, market_state) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (aid, 60, fired + 60_000_000, 588.5, 2.30,
                      6.98, 15.0, "rth"))
        conn.execute("INSERT INTO alert_outcomes (alert_id, horizon, outcome_at, "
                     "underlying_price, structure_mark, pnl_pct, pnl_abs, "
                     "hit_pt1, hit_pt2, hit_pt3, max_favorable_pct, "
                     "max_adverse_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (aid, "1h", fired + 3_600_000_000, 591.0, 3.10,
                      44.19, 0.95, 1, 0, 0, 50.0, -5.0))
        conn.commit()
        conn.close()

        from omega_dashboard import alerts_data
        d = alerts_data.get_alert_detail(aid)
        assert d is not None
        assert d["alert_id"] == aid
        assert d["engine"] == "long_call_burst"
        assert d["structure_summary"] == "$215.50C 5/15 @ $2.15"
        assert d["dte_label"] == "4 trading days"   # LCB is trading-DTE
        assert len(d["features"]) == 2
        assert d["features"][0]["name"] == "regime"  # alphabetical
        assert d["features"][1]["name"] == "rsi"
        assert len(d["track"]) == 1 and d["track"][0]["elapsed_seconds"] == 60
        assert len(d["outcomes"]) == 1 and d["outcomes"][0]["hit_pt1"] is True
        assert "<svg" in d["pnl_svg"]
    finally:
        _teardown_db(tmpdir)


def test_get_alert_detail_missing_returns_none_and_enforces_uuid():
    """alert_id MUST match UUID v4 format. Anything else → None,
    including empty string, non-UUID-looking strings, and obvious
    path-traversal attempts."""
    tmpdir, db = _setup_db()
    try:
        from omega_dashboard import alerts_data
        # Non-UUID inputs all rejected — never even hit the DB.
        assert alerts_data.get_alert_detail("nope") is None
        assert alerts_data.get_alert_detail("") is None
        assert alerts_data.get_alert_detail("../etc/passwd") is None
        assert alerts_data.get_alert_detail("123") is None
        assert alerts_data.get_alert_detail("not-a-uuid-at-all") is None
        # Wrong shape: missing one segment.
        assert alerts_data.get_alert_detail(
            "7f3a9e21-4b8f-4d2c-9a1e") is None
        # Wrong shape: non-hex characters.
        assert alerts_data.get_alert_detail(
            "ZZZZZZZZ-4b8f-4d2c-9a1e-3f7c5d8b9012") is None
        # Valid UUID v4 shape but no row → still None.
        assert alerts_data.get_alert_detail(
            "7f3a9e21-4b8f-4d2c-9a1e-3f7c5d8b9012") is None
    finally:
        _teardown_db(tmpdir)


def test_get_alert_detail_parent_linkage():
    tmpdir, db = _setup_db()
    try:
        fired = _utc_micros_for(2026, 5, 9, 14)
        parent_id = _u("v25d-parent")
        child_id  = _u("lcb-child")
        _insert_alert(db, parent_id, fired_at=fired,
                      engine="v2_5d", classification="GRADE_A",
                      direction="bull",
                      structure={"type": "evaluation"})  # real shape
        _insert_alert(db, child_id, fired_at=fired + 60_000_000,
                      engine="long_call_burst",
                      structure={"type": "long_call", "strike": 215.0,
                                 "expiry": "2026-05-15", "entry_mark": 2.0},
                      parent_alert_id=parent_id, dte=4)
        from omega_dashboard import alerts_data
        child = alerts_data.get_alert_detail(child_id)
        assert child["parent"]["alert_id"] == parent_id
        assert child["parent"]["engine_label"] == "V2 5D EDGE"
        assert child["children"] == []
        parent = alerts_data.get_alert_detail(parent_id)
        assert parent["parent"] is None
        assert parent["structure_summary"] == "Grade A bull"  # built from columns
        assert len(parent["children"]) == 1
        assert parent["children"][0]["alert_id"] == child_id
        assert parent["children"][0]["engine_label"] == "LONG CALL BURST"
    finally:
        _teardown_db(tmpdir)


def test_get_alert_detail_empty_subsections_render():
    """Alert with no features, no track, no outcomes — all sub-sections
    still build without crashing."""
    tmpdir, db = _setup_db()
    try:
        fired = _utc_micros_for(2026, 5, 9, 14)
        bare = _u("bare")
        _insert_alert(db, bare, fired_at=fired)
        from omega_dashboard import alerts_data
        d = alerts_data.get_alert_detail(bare)
        assert d is not None
        assert d["features"] == []
        assert d["track"] == []
        assert d["outcomes"] == []
        assert d["pnl_svg"] == ""
    finally:
        _teardown_db(tmpdir)
```

- [ ] **Step 6: Add list-cap test (no N+1, respects LIST_LIMIT)**

```python
def test_list_alerts_respects_limit_and_does_no_per_row_lookups():
    """Insert 250 alerts; list_alerts should fetch only 200 and never
    look at alert_features / alert_price_track / alert_outcomes."""
    tmpdir, db = _setup_db()
    try:
        # 250 alerts spread across "today"
        base = _utc_micros_for(2026, 5, 9, 14)
        ids = [_u(f"cap-{i:03d}") for i in range(250)]
        for i, aid in enumerate(ids):
            _insert_alert(db, aid, fired_at=base + i * 1_000_000)
        # Add a feature row — list_alerts must NOT touch it
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO alert_features VALUES (?, ?, ?, ?)",
                     (ids[1], "rsi", 60.0, None))
        conn.commit()
        conn.close()
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts()
        assert payload["total_count"] == 200, payload["total_count"]
        # Cards have no 'features' key — proves list view doesn't enrich
        for card in payload["today"][:5]:
            assert "features" not in card
            assert "track" not in card
            assert "outcomes" not in card
    finally:
        _teardown_db(tmpdir)
```

- [ ] **Step 7: Add the PASS/FAIL test runner footer**

```python
if __name__ == "__main__":
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(0 if failures == 0 else 1)
```

- [ ] **Step 8: AST-check + run the test file**

```powershell
python -c "import ast; ast.parse(open('test_alerts_data.py').read()); print('AST OK')"
python test_alerts_data.py
```

Expected: all tests PASS, exit code 0. Test count should be ~13 (the eight covering the matrix + the limit test + 5 structure-summary tests). If any fail, fix the production code in `alerts_data.py` (not the tests) and re-run.

- [ ] **Step 9: Run the full canonical regression battery**

```powershell
python test_canonical_gamma_flip.py
python test_canonical_iv_state.py
python test_canonical_exposures.py
python test_canonical_expiration.py
python test_canonical_technicals.py
python test_bot_state.py
python test_bot_state_producer.py
python test_db_migrate.py
python test_alert_recorder.py
python test_alert_tracker_daemon.py
python test_outcome_computer_daemon.py
python test_engine_versions.py
python test_recorder_queries.py
```

Each should report all-pass. If any fail, do NOT proceed — Patch H must not regress anything.

- [ ] **Step 10: Commit**

```powershell
git add test_alerts_data.py
git commit -m @'
Patch H.6: Hermetic tests for alerts_data

13 tests covering: missing DB, empty DB, all-old DB, bucketing
(today/yesterday/this_week/earlier with deterministic now), each
engine's structure_summary formatter, malformed structure dict
handling, detail page assembly, parent/children linkage, empty
sub-sections (no track/outcomes/features), and the LIST_LIMIT
cap with proof of no N+1 lookups. Each test owns its own temp
DB; never touches /var/backtest/desk.db. Full canonical
regression battery passes.
'@
```

---

## Task H.7: `CLAUDE.md` update

**Files:**
- Modify: `CLAUDE.md` — append to "What's done as of last session" + "Repo layout"

- [ ] **Step 1: Append Patch H entry to "What's done"**

In `CLAUDE.md` under "What's done as of last session (v11.7 / Patch F):", add a new bullet at the end of the list (after the Patch G hotfix entry). Use the same prose voice as the existing entries:

```markdown
- Patch H (alert feed dashboard) — Patch G's recorder gets a user-visible
  surface. New `/alerts` tab between Market View and Portfolio shows
  alerts as they're recorded, polling `/alerts/data` every 10s for fresh
  cards. Three-zone layout: status strip (live/off pill, count today,
  last fire time, refresh countdown), today's feed (full-width cards
  color-coded by engine — LCB orange, V2 5D gold, credit teal,
  conviction red — with pure-CSS pulse for <5min and muted opacity for
  >24h), and collapsible Yesterday / This week / Earlier sections.
  Click any card → detail page with parent/children linkage, structure
  block (DTE shown only with explicit per-engine convention tag —
  LCB "trading days" vs CREDIT "calendar days" — never side-by-side
  without distinguishing), server-rendered SVG line chart of price
  track, outcomes table (horizon × pnl/PT1/PT2/PT3/MFE/MAE), features
  table (alphabetical), and collapsible raw JSON for canonical_snapshot
  + raw_engine_payload. Read-side is `omega_dashboard/alerts_data.py`
  with its own RO sqlite3 connection (`mode=ro` URI form) — does NOT
  import any private helper from `alert_recorder.py`, clean R/W
  boundary. Single-query feed (200-row cap, bucketed in Python — no
  N+1) and 6-single-row-query detail. 13 hermetic tests in
  `test_alerts_data.py`. Empty-state matrix explicitly handles four
  cases: missing DB, empty DB, all-old DB, partial detail
  sub-sections. All env gates default OFF; H is purely additive and
  doesn't touch the trading path, recorder write path, or daemons.
```

- [ ] **Step 2: Append to "Repo layout" → "Dashboard"**

Find the "Dashboard ('The Legacy Desk')" section (~line 65 of CLAUDE.md). After the existing list of `omega_dashboard/*.py` entries, add:

```markdown
- `omega_dashboard/alerts_data.py` — Alerts feed read-side data layer
  (Patch H). Owns its own read-only sqlite3 connection to
  `/var/backtest/desk.db`; never imports `alert_recorder` write
  internals. `list_alerts()` (single-query feed, capped at 200,
  bucketed in CT via `zoneinfo`), `get_alert_detail()` (6 single-row
  queries for one alert), defensive `format_structure_summary()`,
  status-strip env reads, and a server-rendered SVG line chart helper.
- `/alerts` and `/alerts/<alert_id>` routes in `omega_dashboard/routes.py`
  pair with `/alerts/data` JSON endpoint (Patch H). Mirrors the
  `/trading` + `/trading/data` pattern at `routes.py:297-325`.
```

- [ ] **Step 3: (Optional) Append a "Decisions already made" entry**

If the implementer's reading of the implementation surfaces a decision Brad and the QC Claude haven't already locked in, propose appending it. Otherwise skip — Patch H reuses existing decisions.

Likely candidates (only add if QC didn't flag them as already-implicit):
- "Alerts feed polls every 10s with full innerHTML swap of the card grids — simple, no diff, no per-card animation collisions."
- "Per-engine DTE convention is shown ONLY on the detail page with explicit unit tagging ('4 trading days' vs '0 calendar days'). Card view never shows DTE."
- "alerts_data.py opens its own read-only sqlite3 connection (URI mode=ro) — never reuses alert_recorder's R/W connection. Clean boundary so future recorder refactors don't break the dashboard."

- [ ] **Step 4: Commit**

```powershell
git add CLAUDE.md
git commit -m @'
Docs: Patch H — alert feed dashboard

Append Patch H to "What's done as of last session" and add the
new alerts_data module + routes to the Dashboard section of
"Repo layout".
'@
```

---

## Risk & Rollback

**Risks:**

1. **Reading the recorder DB while a writer holds a lock.** SQLite WAL mode (set by G's migration runner) lets readers run without blocking writers, but a reader could see a tiny window of inconsistency between query and the next read. Mitigation: each H query is a single SELECT — no multi-statement transaction reads anything that could be inconsistent. The detail page's 6 separate queries could in theory see a partial write to `alert_outcomes` partway through the page render, but the worst case is "an outcome row doesn't appear yet" — which is the same as "not yet computed", which is already a friendly empty state.
2. **Polling cost at scale.** 10s polling × N concurrent dashboard sessions × 200-row scan. SQLite read of 200 indexed rows on a few-MB DB is sub-millisecond; even with 10 concurrent sessions this is negligible. Hard cap LIST_LIMIT prevents pathological growth.
3. **Empty-state false positives.** If the env vars are read incorrectly (typo in name) the status strip would say "OFF" while the recorder is actually writing. Mitigation: the env-var names are pinned in a constants dict (`ENGINE_FLAG_VARS`, `DAEMON_FLAG_VARS`) sourced from `alert_recorder.py:63-79` — single source of truth. Test or observation will catch a typo immediately.
4. **DTE convention shown without context.** This is the single highest-value guardrail in the prompt. Mitigation: card view has NO DTE field at all (line 3 is structure_summary which is per-engine and skips DTE). Detail view's `dte_label` is built from the `DTE_CONVENTION` map; if an engine has no convention, the value is shown but tagged with "(engine has no convention; raw value)" — never silently rendered as "N DTE".
5. **Path-traversal on detail route.** Flask's `<string:>` converter blocks slashes; `get_alert_detail` additionally rejects empty / `..` / `/` substrings; the SQL query uses parameter binding. Three layers of defense.
6. **Pulse animation distracting during active trading.** CSS-only, opacity-cycling left border at a 2s period — gentle, not flashing. If Brad finds it annoying, removing the rule is a one-line CSS edit; behavior unchanged.

**Rollback:**

- **Tab too early / a regression in alerts_data.py:** revert the H.1 commit (removes the tab). Routes and templates remain but are unreachable from the nav. Page still loads at `/alerts` if URL-typed.
- **Page itself breaks:** revert all of H. Six commits, no schema changes, no migrations, no env vars added — pure rollback. Recorder write path is untouched, so engines and daemons are unaffected.
- **Per-engine display breaks:** edit `ENGINE_DISPLAY` constant in `alerts_data.py` and redeploy. Single source of truth means a single edit fixes all surfaces.
- **Polling too aggressive in production:** edit `POLL_INTERVAL_MS` in `alerts.html`'s `<script>` from 10000 to 30000 and redeploy. No data layer change.

There is no env var to gate H — the page is harmless when the recorder is OFF (renders the friendly empty state). If a true kill-switch is wanted post-ship, wrap the route bodies with `if not os.getenv("ALERTS_PAGE_ENABLED", "true")` and return a "tab disabled" placeholder. Out of scope for V1.

---

## Out of scope (deferred to Patch I or later)

- Aggregate win rate cards, regime breakdowns, MFE/MAE histograms, drift watch — Patch I (barometer dashboard) needs 30+ days of clean recorder data to be statistically meaningful
- Engine performance over time, took-trade tracking, home-page barometer strip — Patch I+
- Filtering / searching the feed — V2; defer until the feed is busy enough to need it
- Suppressed-alerts filter — V2 and depends on the recorder ever capturing them (it doesn't in V1)
- Sort controls — chronological reverse is the default and only option
- User notes / comments on alerts — V2; would require a writeback path which violates the read-only contract
- Tracking summary line on cards (e.g. `tracking — +N% MFE — current +M%`) — would require N+1 lookups against `alert_price_track`. Deferred until either (a) write-side denormalizes the track summary onto `alerts`, or (b) we explicitly accept a lighter per-card lookup pattern. Documented as a deliberate V1 simplification in H.4.
- Real-time push (WebSocket) — V2; 10s polling is good enough for proof-of-life
- Per-card status badges richer than `[active]` — V2, see "Tracking summary line" above
