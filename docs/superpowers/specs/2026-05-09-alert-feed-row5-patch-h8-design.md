# Patch H.8 — Card row 5 + enriched status badge — design spec

**Date:** 2026-05-09
**Owner:** Brad C.
**Scope:** Single-feature follow-up to Patch H. Adds the 5th card row from
the visual mockup (`docs/superpowers/mockups/2026-05-10-alerts-mockup.html`)
and enriches the row-1 status badge to four states. Read-only addition to
`omega_dashboard/alerts_data.py` + the feed template + CSS + tests.
No new env vars. No schema change. No write-side change.

---

## Goal

Patch H ships a 4-row card. The mockup specifies a 5th row carrying
tracking trajectory (`tracking · [bar] · MFE +N% · current +M%`) plus a
richer row-1 badge (`ACTIVE`/`EVAL`/`EXPIRED`/`+N%`). Patch H deliberately
deferred both because they need per-card lookups against
`alert_price_track` and `alert_outcomes`, which would have been N+1.

H.8 makes those lookups in **3 batched aggregate SQL queries** (against
the same set of alert_ids returned by the existing list query) so the
total query count for `list_alerts()` stays bounded regardless of how
many alerts are in the feed:

| # | Query | Purpose |
|---|---|---|
| 1 | `SELECT … FROM alerts ORDER BY fired_at DESC LIMIT 200` (existing) | Feed rows |
| 2 | `SELECT alert_id, MAX(structure_pnl_pct), MIN(structure_pnl_pct), MAX(elapsed_seconds) FROM alert_price_track WHERE alert_id IN (?, ?, …) GROUP BY alert_id` | MFE / MAE / latest-elapsed per alert |
| 3 | `SELECT alert_id, structure_pnl_pct FROM (SELECT alert_id, structure_pnl_pct, elapsed_seconds, ROW_NUMBER() OVER (PARTITION BY alert_id ORDER BY elapsed_seconds DESC, sampled_at DESC) AS rn FROM alert_price_track WHERE alert_id IN (?, ?, …)) WHERE rn = 1` | Latest pnl sample per alert (window-function form, deterministic tie-breaking on `sampled_at DESC` if two rows ever share `elapsed_seconds`). Requires SQLite 3.25+; Render is well past this. |
| 4 | `SELECT alert_id, MAX(hit_pt1) AS any_pt1, MAX(hit_pt2) AS any_pt2, MAX(hit_pt3) AS any_pt3 FROM alert_outcomes WHERE alert_id IN (?, ?, …) GROUP BY alert_id` | Whether any horizon recorded a PT touch |

**4 queries total per feed render**, regardless of card count. No N+1.

---

## Track bar semantic

Single horizon for V1: 3 days, hard-coded.

```python
# omega_dashboard/alerts_data.py
# v11.7 (Patch H.8): single track-bar horizon for visual simplicity.
# If outcome data shows distinct decay profiles per engine, swap this
# to a per-engine dict in V1.1.
TRACKING_HORIZON_SECONDS = 72 * 60 * 60   # 3 days
```

Bar fill = `min(elapsed_seconds / TRACKING_HORIZON_SECONDS, 1.0)`, rendered
as percentage on `<span class="track-bar"><span class="track-bar-fill"
style="width:{pct}%"></span></span>` (matches mockup's classes verbatim).

The mockup's example widths (70 / 60 / 85) were illustrative — the
mockup file gets a clarifying comment so future sessions don't reverse-
engineer them as a formula.

---

## Status badge logic

Single helper `_compute_status_badge(card_data, now_micros)` returns
`(text, style_class)`. Logic, first match wins:

```python
def _compute_status_badge(engine, elapsed_seconds, latest_pnl):
    """Return (text, style_class). Both fields land on the card dict;
    template chooses display via the style_class CSS suffix."""
    if engine == "v2_5d":
        return ("EVAL", "eval")
    if elapsed_seconds > TRACKING_HORIZON_SECONDS:
        return ("EXPIRED", "expired")
    if latest_pnl is not None:
        sign = "+" if latest_pnl >= 0 else ""
        text = f"{sign}{int(round(latest_pnl))}%"
        cls = "positive" if latest_pnl >= 0 else "negative"
        return (text, cls)
    return ("ACTIVE", "active")
```

Badge focuses on **"is this making money now"**. PT-hit info (`★ PT1 hit`)
stays in row 5 — separation of concerns: badge = current state, row 5 =
trajectory.

PT-hit source: `alert_outcomes` rows (the canonical source — the outcome
computer daemon writes hit_pt1/2/3 with ANY-touch semantics). Aggregated
in query 4 above; surfaces in row 5 as `★ PT1 hit` when any horizon row
has `hit_pt1=1`.

---

## Row 5 content matrix

| State | Content |
|---|---|
| **V2 5D evaluation alert** (engine=='v2_5d') | `outcomes compute at 5min · 15min · 30min · 1h · 4h · 1d` (5min/15min/30min/1h/4h/1d come from `HORIZONS_SECONDS` keys in alert_recorder.py — sorted by seconds, stripped at 1d for visual brevity) |
| **No track samples yet** (alert_id not in track-aggregates result) | `tracking starts in {N}s` where N = max(60 - elapsed_seconds, 0). Once first sample lands, switches to active state below. |
| **Active tracking, no PT hit** | `tracking · [bar] · MFE {±N}% · current {±M}%` |
| **Active tracking, PT1+ hit** | `tracking · [bar] · MFE {±N}% · current {±M}% · ★ PT{tier}` (highest tier touched: PT3 > PT2 > PT1) |
| **Past horizon (elapsed > 3d), tracking ended** | `tracking · [bar 100%] · MFE {±N}% · final {±M}%` (where `final` reads from the same `current_pct` field — once tracking ends, the latest sample IS the final; no extra query) |

**MFE + PT-hit shown together (decision: Option B).** Mockup shows MFE
replaced by the PT star, but the MFE-vs-current spread carries
operationally distinct stories — "peaked at PT then drifted" vs "ran
way past PT then gave it back" — and the mockup self-declares as
reference-only. Cost is one extra `<span>` per card. If the bare-star
compact form proves preferred in practice, drop MFE in V1.2 — easy
edit, no data-layer change.

---

## Card dict additions

`_format_card` gains a 5th argument carrying the per-alert aggregates
fetched in queries 2-4, defaulting to None for the warming-up case.

```python
def _format_card(row, now_micros, aggregates=None):
    # aggregates: {"mfe_pct": float, "mae_pct": float,
    #              "latest_pnl_pct": float, "any_pt1": bool,
    #              "any_pt2": bool, "any_pt3": bool,
    #              "latest_elapsed": int}
    # or None if no track samples exist for this alert_id.
```

New fields on the card dict (additive to Patch H's shape):

```python
{
    # ... existing Patch H fields ...
    "badge_text":   "+12%",        # was always None or "ACTIVE"
    "badge_class":  "positive",    # one of: active, eval, expired, positive, negative
    "row5": {
        "mode": "active" | "warming" | "v2_5d" | "expired",
        "bar_pct": 47,             # int 0-100, only when mode in (active, expired)
        "mfe_pct": 18.0,           # only when mode == active and no PT hit
        "current_pct": 7.0,        # only when mode in (active, expired)
        "pt_hit_label": None,      # "★ PT1 hit" / "★ PT2 hit" / None
        "warming_seconds_left": 0, # only when mode == warming
        "horizons_text": "...",    # only when mode == v2_5d
    }
}
```

Template reads `card.row5.mode` and renders the matching variant.
`_alert_card.html` partial gains a fifth `<div class="alert-card-row5">`
block with a `{% if card.row5.mode == 'active' %} … {% elif … %} …` chain.

---

## Changes per file

| File | Change |
|---|---|
| `omega_dashboard/alerts_data.py` | Add `TRACKING_HORIZON_SECONDS`, `_compute_status_badge()`, `_fetch_aggregates(conn, alert_ids)` (returns dict keyed by alert_id with the 4 aggregate fields). Modify `list_alerts()` to call it after the main query and pass to `_format_card`. Modify `_format_card` to accept aggregates and populate `badge_text` / `badge_class` / `row5`. |
| `omega_dashboard/templates/dashboard/_alert_card.html` | Replace the static `{% if card.is_recent %}ACTIVE{% endif %}` badge with `{% if card.badge_text %}<span class="alert-card-badge badge-{{ card.badge_class }}">{{ card.badge_text }}</span>{% endif %}`. Add the row 5 block per the mode chain above. |
| `omega_dashboard/templates/dashboard/alerts.html` | Update the inline JS `renderCard()` to mirror the new template (badge text+class, row 5 mode-driven). Same data — no new poll fields. |
| `omega_dashboard/static/omega.css` | Add `.alert-card-badge.badge-eval` (transparent + brass-deep border) and `.alert-card-badge.badge-expired` (muted) variants. Add `.alert-card-row5` + `.track-bar` + `.track-bar-fill` + `.alert-card-row5-pt-hit` classes per the mockup. |
| `test_alerts_data.py` | Add 4 badge tests Brad specified: `test_compute_status_badge_v2_5d_returns_eval`, `test_compute_status_badge_expired_when_past_horizon`, `test_compute_status_badge_positive_pnl`, `test_compute_status_badge_active_when_no_track`. Add aggregate-query tests: `test_list_alerts_attaches_track_aggregates`, `test_list_alerts_pt_hit_from_outcomes_aggregate`, `test_list_alerts_warming_state_when_no_track_samples`, `test_list_alerts_does_not_explode_with_zero_alerts` (empty IN clause guard). |
| `docs/superpowers/mockups/2026-05-10-alerts-mockup.html` | Add the clarifying comment near `.track-bar-fill` styles noting the bar means `elapsed / 3-day-horizon`, and remove the V1 deferral comment from the top (since H.8 is the patch that ships row 5). |
| `CLAUDE.md` | Append H.8 entry under "What's done as of last session". |

No `app.py` change. No alert_recorder change. No daemon change. No schema
change. No env var change.

**Sync-point code comments (mandatory):**

- In `alerts.html`'s `renderCard()` JS, immediately above the function:
  `// keep in lockstep with _alert_card.html — server vs client render`
  `// drift would silently break polling`
- Near the `warming_seconds_left` computation in `alerts_data.py`:
  `# intentional at-render staleness — V1 doesn't tick this client-side.`
  `# Browser shows the value computed at fetch time; the next 10s poll`
  `# refreshes it.`

---

## Empty-IN guard

If the alerts table has zero rows, queries 2-4 receive an empty IN clause
which SQLite rejects (`OperationalError: near ")"`). `_fetch_aggregates`
must early-return `{}` when given an empty list of alert_ids — caller
treats every card as "warming" (which renders as the no-samples-yet state,
correct for an empty DB).

---

## Test coverage additions

9 new tests on top of Patch H's 15 (24 total after H.8):

1. `test_compute_status_badge_v2_5d_returns_eval` — engine='v2_5d' → ('EVAL', 'eval')
2. `test_compute_status_badge_expired_when_past_horizon` — elapsed > 3d → ('EXPIRED', 'expired')
3. `test_compute_status_badge_positive_pnl` — latest_pnl=12.4 → ('+12%', 'positive')
4. `test_compute_status_badge_active_when_no_track` — latest_pnl=None, fresh → ('ACTIVE', 'active')
5. `test_compute_status_badge_negative_pnl` — latest_pnl=-3.2 → ('-3%', 'negative') (sign formatting)
6. `test_list_alerts_attaches_track_aggregates` — insert alert + 3 track samples (-2, +5, +12) → card.row5.mfe_pct == 12, mae_pct == -2, current_pct == 12
7. `test_list_alerts_pt_hit_from_outcomes_aggregate` — outcome row with hit_pt1=1 → card.row5.pt_hit_label == "★ PT1"; with hit_pt3=1 → "★ PT3" (highest tier wins)
8. `test_list_alerts_warming_state_when_no_track_samples` — alert with zero track rows → card.row5.mode == "warming", warming_seconds_left > 0
9. `test_list_alerts_does_not_explode_with_zero_alerts` — empty DB → no crash, `_fetch_aggregates` handles empty input (early-return on empty alert_id list, never builds `IN ()` SQL)

Total: 24 tests in `test_alerts_data.py` after H.8.

---

## Acceptance criteria

1. New "Alerts" tab still renders in <100ms cold (4 SQL queries, all indexed).
2. Cards in the feed show the row 5 content per the matrix above.
3. Row 1 badge shows EVAL for v2_5d alerts, EXPIRED for >3d alerts, +N%/-N% for any tracked alert with at least one sample, ACTIVE for fresh-but-not-yet-sampled alerts.
4. PT-hit star surfaces in row 5 when any outcome row has hit_pt1/2/3 == 1.
5. Empty DB still renders the friendly empty-state from Patch H (no regression).
6. All 23 tests in `test_alerts_data.py` pass.
7. Full canonical + recorder regression battery still green.
8. Read-only contract preserved — no writes to `/var/backtest/desk.db` from any H.8 code.

---

## Risk & rollback

**Risk:** The aggregate queries scan `alert_price_track` and
`alert_outcomes`. With the existing `idx_track_alert` and
`idx_outcomes_horizon` indexes plus a max IN list of 200 alert_ids,
worst case is sub-10ms per query on a 100k-row track table.

**Rollback:** Six commits, no schema change, no env var added. Revert the
H.8 commit series; the page falls back to Patch H's 4-row cards. No
data loss.

---

## Out of scope (deferred to V1.2 or later)

- Per-engine TRACKING_HORIZON_SECONDS — V1 uses a single 3-day horizon;
  promote to per-engine if outcome data shows distinct decay profiles.
- "Final" rendering for past-horizon alerts (showing locked-in pnl
  separately from MFE) — current spec just relabels "current" → "final"
  but keeps the same fields. If meaningful divergence emerges, add a
  separate locked field in V1.2.
- Stale-data detection (last_elapsed too old vs now → "tracking lost").
  Tracker daemon reliability is the right place to fix that.
- Detail-page row 5 — detail page already has the full SVG chart +
  outcomes table + track table; row 5 isn't needed there.
