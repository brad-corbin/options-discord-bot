# EM Brief on Market View — design spec

**Date:** 2026-05-10
**Owner:** Brad C.
**Scope:** Surface the rich `/em` Telegram output (DEALER EM BRIEF +
ACTION GUIDE) on the Market View dashboard tab. Add a page-level "refresh
all 35" button that triggers the existing silent-thesis batch. Add an
anchored EM-brief panel at the top of the page with three entry paths
(per-card button, single-ticker input, URL deep-link). All work is
additive to the dashboard surface; no change to the trading path,
recorder, or daemons.

---

## Goal

The Market View page currently shows thin "trading cards" (bias pill,
GEX pill, watch map levels) for each of 35 FLOW_TICKERS. The Telegram
`/em TICKER` command produces a vastly richer output: bias score, 1σ
range, gamma flip, walls, pivots, fib zones, dealer regime, volatility
regime, posture, structure score, plus a plain-English ACTION GUIDE
with conditional setups. **The dashboard should let me see that same
depth on demand**, plus give me a manual button to refresh all 35
silent-thesis snapshots without waiting for the periodic loop.

---

## Architecture

Three additions to the Market View tab, sharing one data path:

1. **All-35 refresh button** in the page header eyebrow. Triggers the
   existing `_generate_silent_thesis(ticker)` for every ticker in
   `FLOW_TICKERS`. Async (35 × ~2-3s = 70-100s); button shows
   `Refreshing 12/35…` progress then reverts to `Refreshed at HH:MM:SS`.
   Existing card grid auto-picks up fresh data on its 5s poll once the
   refresh completes.

2. **Anchored EM-brief panel** below the eyebrow, above the card grid.
   Sticky-scrolled, smooth height transition. Three entry paths populate
   it; one panel, last-write-wins.

3. **Per-card "View EM Brief" button** + a **single-ticker input field**
   at the top of the page next to the refresh button. Clicking either,
   or pressing Enter on the input, populates the panel with that
   ticker's EM brief.

**Data path (the load-bearing decision):**

Extract a new pure-compute helper from the existing `_post_em_card` /
`_generate_silent_thesis` paths:

```python
# app.py (or new em_compute.py)
def _compute_em_brief_data(ticker: str, session: str = "manual") -> Optional[dict]:
    """Pure compute. Returns the structured data dict an EM brief needs.
    Does NOT post to Telegram. Does NOT write to ThesisContext.
    Single source of truth for EM brief content."""
    # Lifted from current _post_em_card / _generate_silent_thesis:
    #   _get_0dte_iv, _calc_intraday_em, _calc_bias, vol regime,
    #   walls, skew, pcr, vix, dealer regime, posture, action guide
    # Returns dict with all fields the dashboard template + Telegram
    # text builder consume.
    ...
```

Both `_post_em_card` (Telegram path) and the new dashboard route consume
this helper. `_post_em_card` then runs `_format_em_brief_text(data)` +
posts to Telegram. The dashboard renders the data dict via Jinja into
structured HTML. `_generate_silent_thesis` remains the
ThesisContext-writing wrapper around the same compute helper.

Refactor scope: pure-extraction. No behavior change to the Telegram path
or the silent-thesis store. The only new thing is a third consumer
(dashboard) reading the same data shape.

---

## Components

### Backend

| File | Change |
|---|---|
| `app.py` | Extract `_compute_em_brief_data(ticker, session)` from existing `_post_em_card` body. Existing `_post_em_card` becomes `data = _compute_em_brief_data(...); text = _format_em_brief_text(data); post_to_telegram(text)`. Existing `_generate_silent_thesis` becomes `data = _compute_em_brief_data(...); _write_thesis_context(...)`. **Zero behavior change to the Telegram path** — same inputs, same outputs, same side effects. |
| `omega_dashboard/em_data.py` | New module. `get_em_brief(ticker, session="manual") -> dict` — calls `_compute_em_brief_data` and shapes the result for the dashboard template (e.g., maps emoji-prefixed Telegram lines into structured sections). `start_refresh_all() -> str` returns a job_id; `get_refresh_progress(job_id) -> dict` returns `{started_at, completed, total, errors, finished_at}`. |
| `omega_dashboard/routes.py` | Three new routes (all `@login_required`): `POST /em/refresh` (triggers all-35 batch, returns job_id); `GET /em/refresh/status/<job_id>` (polled by JS); `GET /em/brief/<ticker>` (synchronous, returns JSON or rendered HTML for the panel). |

### Frontend

| File | Change |
|---|---|
| `omega_dashboard/templates/dashboard/_em_brief_panel.html` | New partial. Renders the structured EM brief: header (ticker + DTE + verdict), bias pill, levels grid (spot/1σ range/gamma flip/max pain/walls), local structure (R/S/pivot), fib + VPOC + pin zone, triggers list (micro/range-break/regime-shift), dealer regime block, vol regime + posture + structure score, ACTION GUIDE block (thesis line, GEX context, gamma flip context, pin zone context, "what to watch" setups). |
| `omega_dashboard/templates/dashboard/trading.html` | Add page-header strip with: refresh-all button, single-ticker input, status text. Add anchored EM-brief panel container (initially empty, populated by JS). Add per-card "View EM Brief" button (small icon in card corner). Modify the inline JS: add `loadEmBrief(ticker)` that fetches `/em/brief/<ticker>` and swaps panel innerHTML; wire up the three entry paths (per-card click, input Enter, URL `?em=` on page load); add `triggerRefreshAll()` that POSTs and polls status. |
| `omega_dashboard/static/omega.css` | Add `.em-brief-panel` (sticky positioning, smooth height transition, loading overlay), `.em-brief-header`, `.em-brief-bias-pill`, `.em-brief-levels-grid` (2-column), `.em-brief-triggers`, `.em-brief-regime-block`, `.em-brief-action-guide`, `.em-brief-empty` (placeholder), `.em-brief-loading` (spinner overlay), `.em-brief-error`. Plus `.refresh-all-btn` + `.em-ticker-input` + per-card `.tcard-em-button`. |

### Tests

| File | Change |
|---|---|
| `test_em_data.py` | Hermetic tests. Mock `_compute_em_brief_data` (or stub the underlying Schwab/IV calls) and assert the route returns the expected JSON shape. Cover: valid ticker → populated brief; invalid ticker → 404 with friendly error; refresh-all start/poll/completion flow; concurrent refresh-all requests (second call returns existing job_id, doesn't restart). |

---

## Data flow

### Single-ticker view (per-card or input)

```
Click View button on card  ──┐
Type "HOOD" + Enter         ──┼──► JS: fetch GET /em/brief/HOOD
URL ?em=HOOD on page load   ──┘                  │
                                                 ▼
                              omega_dashboard.em_data.get_em_brief("HOOD")
                                                 │
                                                 ▼
                              app._compute_em_brief_data("HOOD", "manual")
                                                 │
                                                 ▼
                              dict { spot, iv, em, walls, bias,
                                     dealer_regime, action_guide, ... }
                                                 │
                                                 ▼
                              JSON response → JS swaps panel innerHTML
```

Total cost: 1 Schwab chain pull + cached IV/walls/regime computation.
~2-3s typical, ~5s worst case. Synchronous request; spinner overlay
shows during compute.

### All-35 refresh

```
Click refresh button ──► POST /em/refresh
                              │
                              ▼
       em_data.start_refresh_all() spawns daemon thread,
       returns job_id, writes "em_refresh:{job_id}" to Redis with
       {started_at, total: 35, completed: 0}
                              │
                              ▼
   Daemon loops: for ticker in FLOW_TICKERS:
       _generate_silent_thesis(ticker)   (writes ThesisContext)
       redis.hincrby("em_refresh:{job_id}", "completed", 1)
                              │
                              ▼ (35 iterations later)
       redis.hset("em_refresh:{job_id}", "finished_at", now)

JS polls GET /em/refresh/status/<job_id> every 2s:
   "Refreshing 12/35…"  →  "Refreshed at 14:24 CT"

Existing card grid keeps polling /trading/data every 5s; cards
update naturally as ThesisContext fills in. Refresh button itself
shows progress text but doesn't block the rest of the page.
```

Errors per ticker are logged + counted (`errors` key on the Redis hash)
but don't fail the whole batch. If 35/35 finish with 3 errors, status
shows `Refreshed at 14:24 CT (3 errors)`.

---

## Display fidelity

The Telegram brief is dense plain text with emoji. The dashboard
re-renders the same data as structured HTML using existing omega.css
tokens. Section breakdown:

| Telegram section | HTML rendering |
|---|---|
| `🎯 HOOD — DEALER EM BRIEF (1 DTE) \| Exp: 05-15` | Header card with engine-style left border, ticker (large mono), DTE tag, expiry date |
| `⚪ NO TRADE — RANGE / PIN RISK …` | Verdict pill (color by verdict: green = trade-on, gold = neutral, red = avoid) |
| `🧭 Bias: SLIGHT BULLISH (score 1/14)` | Bias pill (chevron + label + score fraction) |
| `📍 Spot / 📐 1σ Range / 🌀 Gamma Flip / …` | 2-column grid of label/value pairs in mono. Tokens: `--text-dim` for labels, `--text` for values |
| `🤝 Neutral read: range / condor structure favored …` | Italic paragraph, full width |
| `⚡ Micro triggers / 🧨 Range-break / 🔄 Regime-shift` | Triggers section: 3 sub-blocks (Micro/Range/Regime), each with up-arrow/down-arrow rows colored by direction |
| `⚙️ Dealer Regime / 🧭 Raw Dealer Flow / 🚦 Entry filter` | Dealer-regime block: regime label as pill, raw flow + entry filter as info rows |
| `🏦 Dealer Structure / 🌡️ Volatility Regime / 🪖 Posture / 🧱 Structure` | 4 info rows in a compact grid, each with eyebrow + value |
| `📡 WHAT TO DO — Plain English` (Action Guide message) | ACTION GUIDE block — separate visual section below the brief, with thesis summary, GEX context box, gamma flip context box, pin zone context box, `🎯 SETUPS TO WATCH` numbered list with bull/bear color coding, `⚠️ MOMENTUM RULES` callout, `📌 PIN ZONE ACTIVE` callout when relevant |
| `— Not financial advice —` | Footer, muted |

Accent colors:
- Verdict pill: green for active setup, brass-bright for neutral/wait, red for no-trade
- Bias chevron: positive-bright (▲) / negative-soft (▼) / brass-deep (─)
- Triggers: positive for "up" rows, negative for "down" rows
- Action guide setup callouts: green border for long setups, red for short setups

Match existing dashboard typography: Cinzel for section eyebrows,
JetBrains Mono for numerals, Outfit for body. No new fonts.

---

## Anchored panel UX details

- **Default state (no ticker selected):** ~60px tall, single line:
  *"Click any card or type a ticker above to see the dealer EM brief."*
  Brass-deep text, dashed border.
- **Loading state:** keeps current panel content visible (or empty
  placeholder if first load), overlays a centered spinner with
  "Computing brief for HOOD…" caption. Doesn't change panel height.
- **Populated state:** smooth height transition to full content
  (~600-800px). Auto-scroll page to top on populate so the brief is in
  view. Sticky positioning so it stays pinned while you scroll the grid
  below.
- **Error state:** brief panel shows friendly message
  ("Couldn't compute brief for HOOD: IV unavailable. Try again or pick
  another ticker."). × button clears.
- **Dismiss controls:**
  - × button in panel top-right → clears to placeholder, removes `?em=`
    from URL via `history.replaceState`
  - Esc key (when panel focused or anywhere on page) → clears
- **URL state:** `?em=HOOD` is the deep-link format.
  - Page load with `?em=HOOD` → panel auto-populates
  - Click a card or type a ticker → URL updates via
    `history.replaceState` (no full reload)
  - Clear → URL strips the param
- **Per-panel refresh:** small ↻ icon next to ticker in panel header →
  re-runs `get_em_brief(ticker)` for just this one ticker. Useful when
  the all-35 batch is mid-flight or you want a fresh number.
- **Last-write-wins:** if you click card A then click card B before A's
  fetch finishes, B's fetch wins. Implementation: track a
  `currentRequestId`; ignore stale responses.

**Session parameter:** Telegram `/em` supports `morning` / `afternoon` /
`manual` session args. V1 dashboard always uses `session="manual"` —
matches the existing `_post_em_card` auto-detect logic (after-hours
auto-flips to next-day preview). If session selection becomes useful
later, add a small dropdown to the panel header in V1.1.

---

## Refresh button UX

- **Idle state:** brass-bright button, label `↻ Refresh all 35`.
- **Mid-refresh:** disabled, label `Refreshing 12/35…`. Counter polls
  every 2s.
- **Done state:** `Refreshed at 14:24 CT (3 errors)` for 30 seconds,
  then reverts to idle. Errors clickable (opens a small toast/log
  showing which tickers failed).
- **Mid-refresh + click again:** no-op (button disabled).
- **Page navigation away during refresh:** background thread keeps
  running. Re-visit Market View → button shows current progress (status
  is in Redis, persists across page reloads).

---

## Acceptance criteria

1. Click `↻ Refresh all 35` → all 35 FLOW_TICKERS get
   `_generate_silent_thesis` called within ~90 seconds. ThesisContext
   updates in Redis. Existing card grid pills (bias / GEX) refresh on
   the next 5s poll.
2. Click any ticker card's `View EM Brief` button → anchored panel
   populates with that ticker's full structured EM brief within ~3
   seconds.
3. Type `HOOD` in the input field + press Enter → panel populates
   with HOOD's brief, even though HOOD is not in FLOW_TICKERS. No
   ThesisContext write for HOOD.
4. Reload page with `?em=HOOD` in URL → panel auto-populates HOOD on
   page load.
5. The Telegram `/em HOOD` text output matches the dashboard panel
   content section-for-section (modulo formatting). Verdict, bias,
   levels, walls, triggers, dealer regime, action guide all surface.
6. Click two cards in quick succession → only the second card's brief
   ends up in the panel (last-write-wins).
7. Refresh button mid-flight → page nav away → return → button still
   shows current `Refreshing N/35…` count (state persists in Redis).
8. Existing Market View functionality unchanged: card grid, polling,
   search bar, account switcher all work as before.
9. Telegram `/em TICKER` and silent-thesis loop continue to produce
   identical output to today's behavior (refactor is pure-extraction).
10. All existing test suites still green; new `test_em_data.py` covers
    the new module.

---

## Risk & rollback

**Risks:**

- **Schwab rate limit during all-35 refresh.** 35 chain pulls + IV +
  walls computations in ~90s = ~25 calls/sec at peak. Existing
  `SCHWAB_RATE_PER_MIN=110` (CLAUDE.md decision) gives 1.83/sec average,
  so 25/sec needs to be smoothed. **Mitigation:** the daemon thread
  serializes per-ticker (one at a time), no parallelism. 35 tickers ×
  3s/ticker = ~105s total, well within rate limit.
- **`_post_em_card` refactor regression.** The pure-extraction must
  not change Telegram output. **Mitigation:** capture current Telegram
  output for 3 representative tickers (1 trade-on, 1 neutral, 1 no-data)
  before refactor; assert byte-identical text after refactor. Add a
  regression test that calls `_compute_em_brief_data` + `_format_em_brief_text`
  and snapshots the result.
- **Single-ticker input for unknown ticker.** User types "FAKE" — Schwab
  returns no chain. **Mitigation:** route catches the exception,
  returns 200 with friendly error payload; panel renders the error
  state. Never 500.
- **Concurrent refresh-all requests.** User clicks refresh, then clicks
  again 5s later. **Mitigation:** `start_refresh_all()` checks for an
  in-flight job in Redis; if one exists, returns its job_id rather than
  starting a new one. Idempotent.

**Rollback:**

- All commits are additive. Revert in reverse order; nothing in the
  refactor changes existing Telegram or silent-thesis behavior, so
  rolling back `_compute_em_brief_data` extraction restores previous
  state cleanly.
- Env-var kill switch: `EM_BRIEF_DASHBOARD_ENABLED=false` (default
  true after ship) hides the new panel + button, returns 410 from new
  routes, leaves the existing trading_data path untouched. One env var
  flip + redeploy = rollback.

---

## Out of scope (deferred to V2 or later)

- **Compare mode** (side-by-side two tickers in one panel). Panel is
  designed to grow this later via a "+ compare" button next to the
  ticker name.
- **Saving / pinning briefs** (favorites bar). V2 if the feature gets
  heavy use.
- **Editing the FLOW_TICKERS list from the UI.** Stays env-var driven
  in V1; Patch H.x V2 if needed.
- **Per-engine refresh sub-buttons** ("Refresh just LCB universe").
  All-or-nothing in V1.
- **Mobile responsive polish.** Panel and grid will work on mobile but
  layout isn't optimized; V1.1 if Brad uses mobile.
- **Recording EM-brief views in the alert recorder.** The recorder is
  for engine fires, not user lookups. Out of scope.
- **WebSocket-driven progress** for the all-35 refresh. Polling every 2s
  is good enough; switch to SSE/WebSocket only if user clicks 50+
  refreshes per session.
- **Brief delta / diff** ("HOOD bias changed from neutral to slight
  bullish since last refresh"). Interesting but not V1.

---

## What this gets me

Once shipped:
- Open Market View → see the existing 35-card grid as today
- Click ↻ Refresh all 35 → ~90s later the bias/GEX pills are fresh
- Click any card's View EM Brief → full Telegram-equivalent brief in
  the anchored panel above the grid, sticky as I scroll to compare
- Type HOOD + Enter → see HOOD's brief without adding HOOD to
  monitoring or polluting the channel
- Bookmark `/trading?em=HOOD` → reload restores the brief
- Telegram channel stays clean (no spam from clicks); ThesisContext
  cache stays accurate (refresh button updates it just like the
  periodic loop)

This is the first dashboard surface that exposes the full /em depth.
Patches H/H.8 made the recorder visible; this makes the EM brief
visible.
