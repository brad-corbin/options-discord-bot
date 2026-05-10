# CLAUDE.md — Project briefing for Claude Code sessions

This file is read automatically at the start of every Claude Code session.
It exists so a fresh agent can pick up where the last one left off, with
the same vocabulary, conventions, and discipline as the human running the
project.

If you're a Claude Code session reading this for the first time: read it
end-to-end before touching code. The "Audit discipline" and "Canonical
rebuild" sections are not optional — the human has been burned by sessions
that ignored them, and will push back hard if you skip them.

This file is the project's living context. When we make decisions during
a session that future sessions will need to know — new vocabulary, new
conventions, architectural choices, roadmap status changes, "we tried X
and it didn't work" lessons — proactively suggest updates to this file
before the session ends. Show the proposed diff; the human approves or
edits. Never update silently.

---

## Who I am, what this bot does

I'm Brad C., an options trader (not a developer). I run a Python bot on
Render that trades 1–14 DTE debit spreads, long calls, and long puts using
the Schwab API with a MarketData fallback. Telegram is the primary
notification surface. I trade alongside Seth.

I don't hotfix during market hours. Friday is shakedown day for any new
deploy. Deploys go via GitHub Desktop → Render auto-rebuild.

The current release line is v8.x for the trading engine and v11.x for the
canonical-rebuild work running alongside it.

## Project framing — Corbin Family Trade Desk

The codebase started as a Discord/Telegram options bot and is mid-transition
into a real **trade desk**: a multi-surface platform with Portfolio,
Research (canonical), Market View (signals + plays + measurement), and
Telegram (delivery) as separate surfaces with separate responsibilities.

When in doubt, the desk owns measurement and attribution. Telegram is a
delivery surface but not the source of truth for "did this engine make
money." That's the desk's job. The canonical-rebuild work (Patches A
through D) was the foundation. The recorder (Patches G through I) is
where the foundation pays off.

The phrase "the bot" still appears throughout legacy code and docs.
Don't globally rename — just understand the framing has shifted. New
surfaces go on the desk. Telegram remains for now and may be deprecated
later, but only after measured edge says it's safe.

---

## Repo layout (what matters)

Trading engine (production, ~11k lines):
- `app.py` — main process, Telegram bridge, scheduling, Schwab adapter glue
- `dashboard.py` — Flask app for the omega_dashboard
- `rate_limiter.py`, `thesis_monitor.py`, `schwab_stream.py`,
  `schwab_adapter.py`, `recommendation_tracker.py`, `persistent_state.py`,
  `oi_flow.py`, `position_monitor.py`, etc.
- `active_scanner.py` — main intraday scanner (`_analyze_ticker`, ActiveScanner class). Technical-indicator helpers delegate to canonical_technicals as of Patch F.

The Greeks / exposure math:
- `options_exposure.py` — `ExposureEngine`, `UnifiedIVSurface`,
  `InstitutionalExpectationEngine`. This file is the production-canonical
  source of truth for dealer Greeks, IV surface, gamma/vanna flip math,
  and walls. Patch 9 settled the dealer-side convention here at line ~743.
- `engine_bridge.py` — `build_option_rows()` converts a chain dict-of-arrays
  into the `OptionRow` list that ExposureEngine consumes. **This is the
  canonical chain converter.** Don't write a parallel one.

Dashboard ("The Legacy Desk"):
- `omega_dashboard/__init__.py` — Flask blueprint setup
- `omega_dashboard/routes.py` — page routes, PAGE_TABS config
- `omega_dashboard/research_data.py` — Research page data layer (the canonical-rebuild surface)
- `omega_dashboard/spot_prices.py` — dashboard `/api/spot-prices` fetcher.
  Streaming-first when `DASHBOARD_SPOT_USE_STREAMING=1`, legacy Yahoo when off.
- `omega_dashboard/templates/dashboard/` — Jinja templates
- `omega_dashboard/static/omega.css` — single CSS file, all dashboard styles
- `prev_close_store.py` — canonical previous-close cache. Lazy-fills from
  Schwab `get_quote`, 25h TTL. Pairs with `schwab_stream.get_streaming_spot`
  to feed the dashboard's `change`/`change_pct` columns.

Canonical rebuild (the v11 work — see "Canonical rebuild" below):
- `raw_inputs.py` — DataRouter wrapper bundle
- `bot_state.py` — single canonical state dataclass per ticker
- `canonical_gamma_flip.py` — wraps `ExposureEngine.gamma_flip`
- `canonical_iv_state.py` — wraps `UnifiedIVSurface`
- `canonical_exposures.py` — wraps `ExposureEngine.compute()`
- `canonical_expiration.py` — picks chain expiration by intent (zero_dte / front / t7 / t30 / t60)
- `canonical_technicals.py` — RSI / MACD / ADX lifted byte-identically
  from `active_scanner._compute_rsi/_compute_macd/_compute_adx` (Patch E).
  Pure additive lift. Patch F redirects callers and reconciles
  risk_manager's drifted ADX.
- `bot_state_producer.py` — Patch B daemon. Three-tier loop (front/t7/t30+t60)
  that periodically computes BotState per (ticker, intent) and writes JSON
  envelopes to Redis. Gated by `BOT_STATE_PRODUCER_ENABLED`. Consumer is
  Patch C's Research page rewrite (separate plan).
- `test_bot_state_producer.py` — runnable without network or Redis (fake clients).
- `test_*.py` for each — runnable without network or Schwab credentials

Alert recorder (Patch G — V1 measurement infrastructure):
- `migrations/0001_initial_schema.sql` — six-table SQLite schema:
  alerts / alert_features / alert_price_track / alert_outcomes /
  engine_versions / schema_migrations. WAL mode, lives at
  `/var/backtest/desk.db`.
- `db_migrate.py` — boot-time migration runner. Idempotent.
  Called from `app.py` boot just before the daemon spawns.
- `alert_recorder.py` — pure write-side. `record_alert()`,
  `record_track_sample()`, `record_outcome()`, `list_active_alerts()`,
  `get_alert()`. Master + per-engine gates inside; every entrypoint
  try/except → recorder failure NEVER affects engines.
- `alert_tracker_daemon.py` — daemon thread. Samples structure marks
  per cadence (60s/5m/15m/30m/1h by elapsed). Reads only from
  `OptionQuoteStore` + `schwab_stream.get_streaming_spot` — zero new
  Schwab REST calls. Behind `RECORDER_TRACKER_ENABLED`.
- `outcome_computer_daemon.py` — daemon thread. Reads
  `alert_price_track`, writes `alert_outcomes` at standard horizons
  (5min/15min/30min/1h/4h/1d/2d/3d/5d/expiry). ANY-touch hit_pt
  semantics. Idempotent. Behind `RECORDER_OUTCOMES_ENABLED`.
- `recorder_queries.sql` — 10 verification queries (win rate by
  engine + horizon, LCB grade-A vs grade-B via parent_alert_id,
  conditional win rate, etc). Patch H barometer reads these.
- `test_db_migrate.py`, `test_alert_recorder.py`,
  `test_alert_recorder_{lcb,v25d,credit,conviction}_wire.py`,
  `test_alert_tracker_daemon.py`, `test_outcome_computer_daemon.py`,
  `test_engine_versions.py`, `test_recorder_queries.py` — 45 tests
  total. Hermetic; no Schwab/Redis/network needed.

---

## Project vocabulary (terms that mean specific things here)

- **BotState** — the canonical immutable per-ticker state snapshot. See
  `bot_state.py`. Every engine consumes BotState; engines do NOT recompute.
- **Canonical \<X\>** — a single wrapper function that is the only entry
  point in the codebase for some computed concept. The math lives in
  `options_exposure.py`; canonical functions wrap it. There is exactly
  ONE canonical per concept. See "Canonical rebuild" below.
- **Wrapper-consistency test** — required test for every canonical
  function. Builds the underlying engine directly with the same inputs,
  asserts the canonical wrapper returns identical output. If they ever
  drift, the wrapper has diverged.
- **Permissive build** — `BotState.build_from_raw()` wraps every canonical
  call in try/except. NotImplementedError → field is None, status recorded
  as "stub". Other exceptions → field is None, status recorded as "error".
  Build always returns a valid object even mid-rebuild.
- **Patch 9 / Walk 1E** — settled the SqueezeMetrics dealer-side
  convention (dealer LONG calls, SHORT puts) at the source. Post-Patch-9,
  every reader downstream of `_exposures` inherits the correct sign by
  default. The convention version is `2` on BotState.
- **Silent thesis** — the production engine that posts thesis snapshots
  to Telegram. It uses `InstitutionalExpectationEngine.snapshot()` →
  `ExposureEngine.compute()` → `UnifiedIVSurface`. Same code paths the
  canonicals wrap. AMZN gamma_flip on the Research page should match
  silent-thesis output within one IV-aware grid step (~$0.30 on AMZN).
- **DEALER GREEKS, FLIP STATE, IV STATE** — section labels on the
  Research page ticker cards. Each section indicates one canonical's
  output. As more canonicals land, more sections appear.
- **gamma flip** — the price (not strike) where net dealer GEX crosses
  zero, found by linear interpolation between sweep grid points.
- **walls** — call_wall, put_wall, gamma_wall, vol_trigger. Strikes
  where dealer positioning concentrates. ExposureEngine.compute() returns
  these as a single dict alongside Greek aggregates.
- **walls_by_intent** — per-snapshot list of dicts populated by
  `_load_walls_for_all_intents` in `omega_dashboard/research_data.py`.
  One entry per intent in `INTENTS_ORDER = ("front", "t7", "t30", "t60")`.
  Each entry has keys: `intent`, `expiration`, `dte_days`, `dte_tag`,
  `call_wall`, `put_wall`, `gamma_wall`. Powers the click-to-expand
  WALLS disclosure on the Research page — front intent renders in the
  `<summary>` (collapsed), t7/t30/t60 expand below.
- **dte_tag** — display label for an expiration's days-to-expiration.
  Format: "0DTE"/"1DTE" for `dte_days <= 1`, "{n}D" for `dte_days > 1`,
  "—" for unknown. Pre-computed in research_data.py so templates stay
  simple.
- **canonical_expiration intent** — short string describing which expiration
  to pick for a chain query. Five intents: `zero_dte` (today's chain), `front`
  (first DTE ≥ 1, never 0DTE), `t7` (first DTE ≥ 7), `t30` (first DTE ≥ 30),
  `t60` (first DTE ≥ 60). Resolved per-ticker via `canonical_expiration()`.
  Different intents = different chains = different walls; that's by design,
  not a bug. Display layers tag values with their intent (e.g. "Call Wall · 1DTE").
- **±25% blanket band / IV-aware band** — gamma_flip search grid.
  Blanket = ±25% always (used when no IV context). IV-aware = ±3σ of
  expected price movement, clamped to [15%, 40%] (Patch 8). Pass `iv` AND
  `dte_years` to canonical_gamma_flip to get IV-aware behavior.
- **bot_state_producer** — daemon-thread producer that pre-computes BotState
  for every (ticker, intent) on a tiered cadence and writes JSON envelopes
  to Redis. Lives in `bot_state_producer.py`. The Research page (after
  Patch C) becomes a pure consumer of these envelopes. Producer always
  runs on exactly one Render worker (cross-worker Redis lock). The
  lock-keeper thread polls for acquisition continuously (Patch B.7), so
  the producer auto-recovers from redeploy gaps where a stale lock from
  the prior worker is still in Redis at boot. Tier threads block on the
  acquired event before each pass — work only happens while we hold the
  leader lock.
- **RESEARCH_USE_REDIS** — env var (default off) gating the consumer path
  in `omega_dashboard/research_data.py`. On → reads pre-built BotState
  envelopes from Redis (Patch B's `bot_state_producer` writes them).
  Off → legacy inline-build path runs unchanged (build BotState per
  request via DataRouter + canonical_expiration). Same rollback
  contract as `DASHBOARD_SPOT_USE_STREAMING` and
  `BOT_STATE_PRODUCER_ENABLED`: unset the env var, redeploy, behavior
  reverts within 60s.
- **MIN_COMPATIBLE_PRODUCER_VERSION / EXPECTED_CONVENTION_VERSION** —
  consumer-side schema gates in `omega_dashboard/research_data.py`.
  `MIN_COMPATIBLE_PRODUCER_VERSION = 1` rejects envelopes from very-old
  producers (forward-compatible: newer producer versions are accepted).
  `EXPECTED_CONVENTION_VERSION = 2` is strict — Patch 9 dealer-side
  protection. Mismatch → render the ticker as "warming up" with a
  warning log; never display dealer-side-flipped numbers.
- **producer envelope** — the JSON wrapper around BotState in Redis. Shape:
  `{producer_version, convention_version, intent, expiration, state}`.
  `producer_version` bumps on schema change (consumers reject unknown
  versions); `convention_version=2` is Patch 9 dealer-side. `state` is
  the BotState dataclass as-dict, with NaN/+inf/-inf floats converted to
  null and datetime/date converted to ISO 8601 strings (Patch B.6 hotfix —
  json.dumps doesn't accept NaN literals or raw datetime objects). Intent
  and expiration live on the envelope, NOT inside BotState (Open Question
  #3 resolution).
- **BOT_STATE_PRODUCER_ env vars** — `_ENABLED` (bool, default off),
  `_TICKERS` (comma list), `_INTENTS_TIER_A/B/C` (comma lists; default
  `front`/`t7`/empty), `_CADENCE_TIER_A/B/C` (seconds; defaults 60/180/600).
  Empty-string Tier C cleanly disables that tier. Promote universe by
  editing env vars and restarting.
- **Stalk / Alpha SPY Omega** — Telegram channels (separate from the
  main bot channel) for diagnostic/stalk content and non-confluence
  Potter Box breakouts.
- **prev_close_store** — the canonical previous-close source. One module
  per concept (audit rule 1). Used by `omega_dashboard/spot_prices.py` to
  pair streaming live price with yesterday's close. Don't add parallel
  implementations elsewhere; if you need prev_close, call
  `get_prev_close_store().get(ticker)`.
- **DASHBOARD_SPOT_USE_STREAMING** — env var (default off) gating the
  streaming-first path in `omega_dashboard/spot_prices.py`. On → Schwab
  WebSocket spots + prev_close_store + Schwab REST fallback. Off →
  byte-identical v8.3 Yahoo behavior. Rollback is unset+redeploy.
- **add_equity_symbols / remove_equity_symbols** — `SchwabStreamManager`
  methods for dynamic Level 1 equity sub management (mirror of the
  existing option pattern). The dashboard's `/api/register-tickers` calls
  these on page load so portfolio/watchlist tickers join the stream.

---

## Canonical rebuild — the architectural backbone

The bot has 130+ Python files. Many concepts (gamma flip, walls, ATM IV,
GEX) had multiple parallel implementations across those files. The
canonical rebuild is collapsing each concept to ONE entry point.

The pattern, in order:

1. **Inventory the existing implementations.** Use `data_inventory.py` /
   `field_inventory.py` if available, or grep. NEVER write a new helper
   without first finding what already exists. (This rule is here because
   I broke it, got called out, and we're not going to repeat that.)

2. **Pick the canonical.** Usually the most-sophisticated existing
   implementation that production code is already using. Examples:
   `UnifiedIVSurface` for IV state, `ExposureEngine.compute()` for
   exposures and walls.

3. **Write `canonical_X.py`** as a thin wrapper. Import the canonical,
   normalize input/output to the BotState contract, return a dict or
   scalar matching the BotState fields. Should be ~150-250 lines including
   docstrings.

4. **Write `test_canonical_X.py`** including the wrapper-consistency
   test. Pattern: build the underlying engine directly, call canonical
   wrapper, assert identical output. This is non-negotiable for every
   canonical.

5. **Wire into `bot_state.py`** by adding the canonical to
   `build_from_raw`'s try/except chain. Remove its name from the stub
   list. Replace the field reads to consume the new canonical's result.

6. **Wire into `omega_dashboard/research_data.py`** by adding the new
   fields to `TickerSnapshot`.

7. **Update `omega_dashboard/templates/dashboard/research.html`** to
   render a new section for the canonical's output. Match the existing
   visual pattern (eyebrow + research-card-row pairs).

8. **Run all canonical test suites.** Currently: `test_raw_inputs`,
   `test_canonical_gamma_flip`, `test_canonical_iv_state`,
   `test_canonical_exposures`, `test_canonical_expiration`,
   `test_bot_state`. All must pass.

9. **AST-check every Python file touched.** `python3 -c "import ast;
   ast.parse(open('file.py').read())"`. Never ship a file that hasn't
   parsed clean.

10. **Update CLAUDE.md if a major architectural decision was made.**

What's done as of last session (v11.7 / Patch F):
- canonical_gamma_flip
- canonical_iv_state (replaces a brief mistake — see "Audit discipline" below)
- canonical_exposures (Greek aggregates: gex/dex/vanna/charm/gex_sign)
- canonical_walls — wiring-only patch; walls share canonical_exposures'
  ExposureEngine.compute() pass. No separate wrapper file. Wires
  call_wall/put_wall/gamma_wall to BotState; max_pain/pin_zone_low/
  pin_zone_high stay None pending a separate canonical.
- canonical_expiration — five-intent registry; replaces ad-hoc "next Friday"
  in Research page. Side effect: Research walls now use front non-0-DTE
  chains per ticker. Patch B (producer daemon) not yet shipped, so the page
  is still slow — only the EXPIRATION choice changed in this patch.
- canonical_technicals (Patch E) — RSI / MACD / ADX lifted byte-identically
  out of active_scanner.py into canonical_technicals.py with wrapper-
  consistency tests. ADX uses active_scanner's RMA-seeded version (aligned
  with backtest_v3_runner.py:346-364 ind_adx quintile data). risk_manager's
  SMA-seeded _compute_adx is documented as DRIFT and reconciled in a later patch.
  Patch E is purely additive — no caller is modified.
- Patch F (active_scanner technicals redirect) — `_compute_rsi/_compute_macd/_compute_ema/_compute_adx/_rma` in active_scanner.py are now thin delegation wrappers around `canonical_technicals.*`. Zero behavior change verified by `test_active_scanner_technicals_delegate.py` (6 tests including an `_analyze_ticker` end-to-end sanity check). Backtest imports keep working unchanged through the existing names. canonical_technicals is no longer library-with-no-readers — active_scanner is its first production consumer. Orphaned `MACD_FAST/SLOW/SIGNAL` constants in active_scanner.py deleted; canonical_technicals.MACD_FAST/SLOW/SIGNAL is the single source.
- BotState with permissive build, 64 fields total, ~22 currently lit per ticker
- Research page replaces the old Diagnostic placeholder
- Multi-DTE walls drilldown (Patch D) — Research page WALLS section is now
  a native HTML `<details>` disclosure. Front-DTE walls render in the
  collapsed `<summary>`; t7/t30/t60 rows expand below with `dte_tag` labels
  ("8D"/"32D"/"61D"). Touches `omega_dashboard/research_data.py` (DTE helpers,
  `_load_walls_for_all_intents`, `walls_by_intent` field on TickerSnapshot),
  `omega_dashboard/templates/dashboard/research.html` (disclosure markup +
  legacy fallback), `omega_dashboard/static/omega.css` (disclosure styling).
  This completes the consumer side of the producer/consumer architecture:
  producer was Patch B, consumer skeleton was Patch C, multi-DTE drilldown
  is Patch D. The producer was already writing all 4 intents per ticker —
  Patch D finally surfaces them.
- Patch F.5 (canonical_technicals on BotState) — `_build_technicals_from_raw(raw)` helper added to `bot_state.py`; replaces the technicals stub in `build_from_raw`. BotState's `rsi` / `macd_hist` / `adx` fields now populate from `canonical_technicals.rsi/macd/adx` instead of None, and `canonical_status["technicals"]` flips from `stub` to `live`. Defensive about bar key naming — `risk_manager.py:275` pattern (`b.get("h") or b.get("high")`). Wrapper-consistency tests assert no parallel RSI/MACD/ADX math sneaks into the helper. canonical_technicals now has two production readers: active_scanner (Patch F) and BotState (F.5).
- Patch G (alert recorder V1) — the platform's central measurement
  artifact shipped. Schema at `/var/backtest/desk.db` (SQLite, WAL).
  Four engines wired: LONG CALL BURST, V2 5D EDGE MODEL, v8.4 CREDIT,
  CONVICTION PLAY. Each engine's post-Telegram-send hook calls
  `record_alert(...)` with full input snapshot, canonical_snapshot,
  classification, parent_alert_id where applicable (V2 5D parents
  LCB and credit). Two daemons: `alert_tracker_daemon` samples
  structure marks on variable cadence (zero new Schwab REST —
  piggybacks on existing OptionQuoteStore) and `outcome_computer_daemon`
  computes pnl/MFE/MAE/hit_pt at horizon boundaries with ANY-touch
  semantics. Engine versions auto-registered at boot via
  `register_engine_versions()`. `apply_migrations()` now runs at boot
  just before daemon spawn — without G.9, the schema would never
  exist on a fresh deploy. Gated behind `RECORDER_ENABLED` (master)
  + per-engine + per-daemon flags, all default OFF; staged rollout
  per spec. Recorder failures NEVER propagate to engines (try/except
  at every hook + inside `record_alert` itself). 13-commit series
  (F.5.1/2/3 + G.1-G.10) plus an OHLC bar-shape hotfix and an
  implementation plan in `docs/superpowers/plans/`. 45 new tests +
  full canonical regression battery green.
- Patch G hotfix (F.5.1 OHLC support) — production was flooding logs
  with "canonical_technicals failed: 'OHLC' object has no attribute
  'get'". Root cause: F.5.1's `_build_technicals_from_raw` assumed
  bars are dicts with `.get()`, but production passes both dicts AND
  `OHLC` dataclass instances (`options_exposure.py:501`, fields
  `.high/.low/.close`, no short aliases). Fix: tiny `_bar_field(b,
  short_key, long_key)` inner helper that uses `b.get(...)` for
  dict-like and `getattr(...)` otherwise. Both naming conventions
  still supported. New `test_build_technicals_from_raw_handles_ohlc_objects`
  pins the regression.

What's queued (in order):

**Foundation work (additive, no behavior change to trading path):**
- Patch E.5 (or later): canonical_vwap — session VWAP + bands. Three
  implementations exist today (vwap_bands.py, bar_state.py, income_scanner.py).
  Reconciliation requires session-anchor and band-multiplier design decisions —
  it's not a stateless lift. Patch E was intentionally scoped to stateless
  indicators only; VWAP needs its own design pass.
- Later patch: risk_manager ADX migration. risk_manager._compute_adx is SMA-seeded Wilder ADX, drifts from active_scanner's RMA-seeded variant (now canonical). Migration shifts ADX values, may shift regime classifier on borderline inputs. Plan: capture the actual numerical drift on real SPY data first (separate one-off script), include the drift in the commit message, gate behind env var if shift is meaningful.
- Later patch: RSI consolidation across `app.py:_rsi`, `unified_models.py:_rsi` (Wilder-smoothed, different from canonical), `swing_scanner.py:_rsi` (Wilder-smoothed AND list-returning, different shape), `income_scanner.compute_rsi` (close to canonical, rounds to 1 decimal). Needs design pass — Wilder-smoothed canonical, OR migration with documented drift, OR list-returning canonical for swing_scanner. Multiple sub-patches when it lands.
- Eventual cleanup: delete the `_compute_*` shims in active_scanner once nothing imports them. Requires confirming no external caller references the legacy names.

**The recorder — V1 measurement infrastructure:**

The recorder is the platform's central artifact and the reason all the
canonical-rebuild work exists. Records every alert that hits the main
Telegram channel with full input vector + canonical snapshot + price
track + outcomes at multiple horizons. Answers Brad's actual question:
"of the alerts you're showing me, what's the win rate, and would I have
won at horizon X."

- Patch G — V1 alert recorder. SQLite at /var/backtest/desk.db. Four
  tables: alerts + alert_features + alert_price_track + alert_outcomes.
  Records 4 engines: LONG CALL BURST, V2 5D EDGE MODEL, v8.4 CREDIT,
  CONVICTION PLAY. Full universe (whatever the bot is running on, no
  ticker filter). Price tracking piggybacks on existing
  recommendation_tracker polling — zero new Schwab REST calls.
  See spec at docs/superpowers/specs/2026-05-08-alert-recorder-v1.md.
- Patch H — Barometer dashboard. Query layer + Market View page surfacing
  win rate by engine, classification, regime, holding horizon. This is
  where Patch G's data becomes visible. Without H, the recorder is a
  silent database. With H, it's the lever-finding workbench.

**V2+ work (real, valuable, not V1):**
- Took-trade tracking (link Brad's portfolio entries to alert_ids)
- Campaigns / rolls / transformations (the JPM-rolling-to-next-Friday model)
- Multi-signal justification (one position, multiple signals over time)
- Engine promotion logic (proven vs. unproven dashboard surfaces)
- Setup-level dedup with fire sub-rows (currently V1 records each fire as
  its own alert; dedup is a V2 query-layer concern)
- Recording suppressed/internal events (V1 records only what hits Telegram)
- Exit gate (Patches J/K/L: shadow → notify → trusted)

**Other canonicals (build only when a concrete consumer needs them):**
- canonical_pivots, canonical_em_state, canonical_dealer_regime,
  canonical_potter_box, canonical_flow_state, canonical_calendar.
  These are NOT built ahead of consumers. They are built when a
  Patch G/H/I consumer surfaces a concrete dependency. The "build
  canonicals for completeness" trap is the parallel-system trap.

**First migration (deferred to V2):**
Once V1 recorder + barometer ship and a few weeks of side-by-side data
exists, pick one production engine and redirect it to read from
canonicals + recorder data instead of computing inline. Migration is
what makes the rebuild real and not just parallel.

---

## Audit discipline (non-negotiable)

These rules exist because I've watched them get violated and the cost
is high. Don't argue with them.

1. **Never inline a helper for a concept that has existing implementations.**
   Always grep for the concept first. If 2+ implementations exist, write
   a `canonical_X` wrapper around the most-sophisticated one. Never write
   a parallel ad-hoc implementation in `bot_state.py`, in a route handler,
   in a one-off script, anywhere. (I broke this rule writing
   `_atm_iv_from_chain` inline in bot_state.py during the v11.3.1 patch.
   Brad caught it. We're not repeating it.)

2. **Verify patch anchors against the actual file before editing.**
   Line numbers drift. grep for the anchor string. Don't trust line
   numbers from a previous session.

3. **AST-check after every edit.** `python3 -c "import ast;
   ast.parse(open('file.py').read())"`. Never ship a file that hasn't
   parsed clean.

4. **Don't bundle patches.** Each patch does one thing. Small blast
   radius, easy rollback, easy to verify. If something feels like 2
   patches, split it.

5. **Wrapper-consistency tests are mandatory.** Every canonical wrapper
   must have a test that proves its output is identical to a direct call
   on the underlying engine. Without this, drift goes undetected.

6. **Run the tests, don't just describe them.** When QA'ing, actually
   execute. "These tests would check..." is not the same as "these tests
   passed."

7. **If a session runs long, write a precise handoff note.** What's
   done, what's half-done, what decisions were made, what the next session
   needs to finish. Don't ship partial files labeled as finals.

---

## Code style

- Every patch gets a visible `# v<version> (Patch N):` or `# v<version>:`
  comment marker so I can grep for it later.
- Header blocks on `app.py` list every patch applied with file +
  one-line description.
- Wrap new background work in try/except at every level. A dashboard
  bug must never bring down trading.
- Log every failure. Silent `except: pass` is the enemy. At minimum
  `log.debug` with context, `log.warning` for anything actionable.
- Every new feature needs an on/off env var that defaults to off.
- Anything new that writes to Sheets/Redis/disk is purely additive.
  Daemon thread, never blocks trading. Reads from existing data stores.
  Fails gracefully (warn + retry, not crash). Has a clear rollback —
  unset env var, redeploy, done.

---

## How I want to be talked to

- I'm not a developer. Skip the lecture, give me the answer.
- Real file deliverables go to `/mnt/user-data/outputs/` (in the
  Anthropic environment) or directly into the repo (in Claude Code).
  Don't paste full files in chat.
- When I ask "continue," pick up from where you actually stopped. Don't
  recap unless you're at a decision point.
- When QA'ing, actually run the tests.
- When something's done, say it's done. Don't invent more work.
- If I push back hard on something, the right move is usually to agree
  and fix it, not defend the original choice. Especially around the
  inline-helper rule.

---

## Decisions already made — don't relitigate

- Log threshold 60 (final gate), post threshold set via `/confidence`
  command (not env var)
- Schwab rate limit: 110/min, env-tunable (`SCHWAB_RATE_PER_MIN`)
- Grading uses option PnL only, not spot PnL
- `EM_TICKERS = ["SPY", "QQQ"]` stays; silent-EM with Patch 9 fallback
  covers 35 tickers
- DataRouter (`_cached_md`) is the canonical IO layer. Don't write
  parallel Schwab/MarketData calls.
- `RawInputs.chain` stays as raw dict-of-arrays (the shape DataRouter
  returns and `build_option_rows` expects). Don't pivot it to
  list-of-dicts in the canonical path. `chain_rows` helper exists for
  legacy display only.
- `canonical_gamma_flip` returns a PRICE (interpolated between grid
  points), not a strike.
- BotState `convention_version=2` (post-Patch-9), `snapshot_version=1`.
- 504-bar OHLC default for `RawInputs` (covers Potter Box, the longest
  real lookback).
- Default Research page intent: `front` (first non-0-DTE expiration per ticker, via `canonical_expiration`).
- Research page cache: 60-second in-memory, keyed by (ticker, expiration).
- Dashboard spot prices: streaming-first path (`DASHBOARD_SPOT_USE_STREAMING=1`)
  reads from `schwab_stream.get_streaming_spot` + `prev_close_store`, with
  Schwab REST `get_quote` as cold-start fallback and Yahoo as last resort.
  Yahoo path retained verbatim from v8.3 as the rollback target — unset the
  env var to revert. Trading-engine paths (`/trading/data`, silent thesis,
  scanner) are unaffected; they still use existing quote logic.
- `_tickers` on `SchwabStreamManager` is normalized uppercase at construction
  (Patch S.1 fix). Don't pass lowercase symbols to `add_equity_symbols` and
  expect Schwab to deduplicate — the manager dedups locally first.
- bot_state_producer ships behind `BOT_STATE_PRODUCER_ENABLED=false`
  (default off). Initial production universe: 10 tickers × 2 intents
  (front + t7), Tier C empty. Run 7 trading days clean before promoting
  to full 35×4 universe. Per-build timing is recorded to a Redis sorted
  set (`bot_state_producer:timings:{YYYYMMDD}`) for post-deploy cadence
  tuning. Producer crashes are caught at the outer loop and restart with
  5s backoff. No market-hours pause. Cross-worker lock is maintained by
  a dedicated lock-keeper thread (path b — see review notes), independent
  of tier work.
- `producer_version` lives on the envelope and bumps on schema change.
  Consumers (Research page after Patch C) check for forward and backward
  compatibility on read; `convention_version` mismatch is treated as
  "warming up" rather than rendered (Patch 9 protection).
- bot_state_producer lock-keeper is poll-acquire, not fail-fast (Patch B.7).
  At startup the producer is "running" structurally regardless of whether
  it currently holds the cross-worker Redis lock; the keeper polls every
  LOCK_REFRESH_INTERVAL_SEC=60 to acquire, and tier threads block on
  `_lock_state.acquired` before each pass. This was added after the first
  env-flag flip in production hit a redeploy gap (old worker's 90s lock
  TTL hadn't expired when new worker booted, producer stayed dormant
  forever). The keeper also releases the lock on graceful stop() so the
  next deploy acquires immediately instead of waiting for TTL.
- Research page reads from Redis when `RESEARCH_USE_REDIS=1` (Patch C).
  Cold-start dashboard load goes from ~3 minutes (inline build per
  request) to <1 second (35 Redis GETs of pre-built envelopes). The
  inline path stays in `research_data.py` for rollback; default off
  on first ship. Consumer is permissive: missing keys, malformed JSON,
  version mismatches all render as "warming up" skeleton cards (CSS
  class `research-card-warming-up`) — the dashboard never errors out
  whole-page on a single bad ticker.
- Research page auto-refreshes while any card is warming up
  (Patch C.5/C.6/C.7 — three iterations). Server-rendered page with
  inline JS that reloads every 5s, capped at 60 attempts (5 min).
  Sticky bottom bar shows the countdown plus Stop/Refresh-now buttons,
  so user always has explicit control. Once all cards populate the
  bar disappears (the script tag is conditional on
  `selectattr('warming_up')` being non-empty). After the 60-attempt
  cap the bar switches to "paused" and waits for manual retry —
  protects against runaway loops if the producer is genuinely stuck.

  Producer-side companion: tier TTL is `cadence × 6` (Patch C.6, was
  `× 3`). Without this, when the producer's actual pass time
  exceeded the TTL — common at startup or under rate-limit pressure
  with a 35×4 universe — populated cards would expire BEFORE the
  next pass rewrote them, so the auto-refresh thrashed forever.
  Tier A: 360s TTL (was 180s), Tier B: 1080s, Tier C: 3600s.
- Schema versioning split: `producer_version` is forward-compatible
  (newer producer accepted by older reader as long as MIN_COMPATIBLE
  is met). `convention_version` is strict-equal — mismatch is treated
  as "warming up" rather than rendered, to prevent ever displaying
  dealer-side-flipped numbers post a Patch 9-style convention shift.
- Research page WALLS section is a native HTML `<details>` disclosure.
  Front-DTE Call/Put walls in the collapsed `<summary>`; t7/t30/t60 rows
  expand below with their own `dte_tag` labels. Zero JavaScript — full
  keyboard accessibility comes free with `<details>`/`<summary>`. Legacy
  single-intent block (Call/Put/Gamma rows, no DTE tags) stays as a
  fallback when `walls_by_intent` is empty (env-var-off path or
  warming-up snapshots).
- **The recorder is the platform's central artifact.** Every alert that
  hits the main Telegram channel records its full input vector + the
  canonical snapshot at fire time + spot. Outcomes attach to alerts via
  stable alert_ids. The recorder schema is immutable; rebuilds change
  canonicals but old alerts must remain queryable in their original
  context. See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md.

- **Engine versioning is mandatory.** Every engine has a version string.
  When scoring weights, thresholds, or logic change, the version bumps.
  The recorder stores engine + engine_version with every alert. Queries
  always filter or group by version. Without this, long-window analysis
  silently mixes apples and oranges and the lever-finding doesn't work.

- **SQLite for forever-storage, Redis for live state only.** The
  signal/outcome ledger lives in durable storage at /var/backtest/desk.db
  (Render persistent disk, 5 GB current, snapshot-backed). Redis stays
  for live state with TTLs ≤ 24h: BotState envelopes, OI cache,
  prev_close_store, lock keys, telemetry sorted sets, recent scan_results.
  The 25 MB Redis cap (free tier with allkeys-lru eviction) is hard.
  At 16/25 MB used today, recorder data will not fit and would evict
  live state. SQLite is the only correct answer for the recorder.

- **Capture more than the engine reads.** When an alert fires, the
  recorder snapshots the entire BotState envelope — not just the fields
  the engine consumed. This is how new levers get found later: by
  querying alerts against canonicals that didn't exist or weren't wired
  in at fire time, but whose state was preserved.

- **Recorder is V1 narrow on purpose.** V1 records 4 engines on full
  universe, with price tracks and outcomes at standard horizons. It does
  NOT track took-trade, campaigns, transformations, suppressed events,
  or implement engine promotion. These are V2+. The V1 schema does not
  preclude any of them — they are purely additive. Old V1 data stays
  valid forever.

- **canonical_technicals lift order.** Lift the math byte-identically
  into canonical_technicals.py FIRST (Patch E), then migrate
  active_scanner and risk_manager to import from canonical (Patch F),
  then build measurement infrastructure on top (Patch G+). This avoids
  the dashboard-locked-to-active_scanner-internals problem.

- **No speculative canonicals.** Future canonicals (em_state,
  dealer_regime, potter_box, flow_state, calendar) are NOT built ahead
  of consumers. They are built when a Patch G/H/I consumer surfaces a
  concrete dependency.

- **Exit gate is staged.** Future Patches J → K → L: shadow → notify →
  trusted. Each stage requires explicit Brad approval based on data
  from the prior stage. Trusted mode is the only stage with real blast
  radius and is not implemented without prior discussion of fail-safes
  and kill switches.
- **Recorder default state: every gate OFF.** `RECORDER_ENABLED`
  (master) and per-engine (`_LCB`, `_V25D`, `_CREDIT`, `_CONVICTION`)
  + per-daemon (`_TRACKER`, `_OUTCOMES`) all default to `false`.
  Staged rollout: master ON → per-engine one at a time (24h
  validation each) → tracker ON → outcomes ON. Rollback: unset
  master, redeploy; recorder hooks become no-ops within 60s, DB
  intact.
- **Recorder uses SQLite at `/var/backtest/desk.db`, NOT Redis or
  Postgres.** Free Render Redis is 25 MB capped with allkeys-lru
  eviction; recorder data would evict live state. Postgres free
  tier has 90-day retention (incompatible with forever-storage).
  SQLite on the persistent disk is durable, queryable, snapshot-
  backed. Schema is portable to Postgres later via SQLAlchemy if
  scale demands it.
- **Recorder hooks NEVER affect engines.** Every wire site is
  wrapped in try/except → `log.warning` on failure → return.
  `record_alert` itself has internal try/except → returns None on
  failure. Two layers of defense; the contract is load-bearing.
  Don't simplify by removing the outer try/except; the inner one
  exists to handle expected error modes (DB locked, malformed
  payload), the outer one exists to catch unexpected ones (import
  errors after schema migration, etc).
- **DTE convention divergence is a known data-quality flag.**
  LCB records `suggested_dte` as TRADING-DTE (`count_trading_days_between`).
  v8.4 CREDIT records CALENDAR-DTE (`(expiry - today).days`).
  V2 5D and CONVICTION PLAY record None (no structure / no DTE field).

  Implication: any barometer query that filters or groups by `suggested_dte`
  across engines is comparing apples to oranges. Patch I's design must
  either (a) normalize at query time to one convention, (b) add a
  `dte_convention` column to alerts and require queries to filter by it,
  or (c) only group within-engine. Until Patch I, this divergence sits
  in the data — every alert recorded today carries one of two semantics.

  Backfilling a normalization later is awkward but possible since both
  the original `suggested_dte` and the structure's `expiry` are recorded.
- **V2SetupResult attribute names are `setup_grade` and `bias`,
  NOT `grade` and `direction`.** Helpers (`_build_v25d_alert_payload`,
  `_build_lcb_alert_payload`'s v2_5d_grade lookup,
  `_build_credit_alert_payload`'s v2_5d_grade lookup) use defensive
  `getattr(v2_result, "setup_grade", None) or getattr(v2_result,
  "grade", None)` so future renames don't break recorder records.
- **Conviction-play hook count is 8, not 5.** The spec said "5
  sites" but site 1 in `app.py:_run_v4_prefilter` has 4 route
  branches (immediate / income / swing / stalk), each with its own
  Telegram post + its own recorder hook. Plus one site each in
  `app.py:10671`, `11064`, `15548`, and `schwab_stream.py:1183`. The
  `_record_conviction_after_post(cp, posted_to)` wrapper in app.py
  DRYs the 7 app.py sites; schwab_stream uses an inline try/except
  to keep its module-top imports tight.
- **`apply_migrations` is now wired at boot in `app.py` inside the
  leader-only `_acquire_background_leader()` block.** Without G.9
  adding this, the schema never gets created on a fresh deploy and
  every recorder write fails silently. The migration runner is
  idempotent — re-running on subsequent boots does nothing.
- **Conviction is independent of V2 5D — `parent_alert_id=None`
  always.** OI flow conviction plays don't chain off a V2 5D
  evaluation the way LCB / credit do. If conviction ever becomes
  V2 5D-gated, this changes; until then, hardcoded None.
---

## Known issues — document but don't fix unless asked

- Conviction plays re-fire every 8 min on the same flow snapshot
  (NVDA 4/15 = 21 posts)
- Same conviction posts as both `CONVICTION PLAY` and `FLOW CONVICTION`
  (2× noise)
- Exit signals re-fire multiple times on the same position
- Option peaks fire on every new intraday high, not PT1/PT2/PT3 only
- `BREAKDOWN CONFIRMED` Potter Box annotation shows 0 hits in Telegram
  export — low priority

---

## Infrastructure

- Render hosts the bot. Deploy = git push from GitHub Desktop, Render
  auto-rebuilds.
- Public repo: `github.com/brad-corbin/options-discord-bot`
- Sheet ID for em_predictions / dashboard data:
  `1v9UN6qoTWdFJWe332qfnrvJwMF88JavN-MteHL66Iv8`
- Service account: `bot-sheets-writer@corbin-bot-tracking.iam.gserviceaccount.com`
- Dashboard URL: `https://options-discord-bot.onrender.com/`
  (login-protected via DASHBOARD_SECRET_KEY / DASHBOARD_PASSWORD env vars)

---

## Quick smoke-test commands

```bash
# AST check every changed Python file
python3 -c "import ast; ast.parse(open('PATH').read())"

# Run all canonical-rebuild test suites
python3 test_raw_inputs.py
python3 test_canonical_gamma_flip.py
python3 test_canonical_iv_state.py
python3 test_canonical_exposures.py
python3 test_canonical_expiration.py
python3 test_bot_state.py
python3 test_canonical_technicals.py

# Patch B producer test (fake Redis, fake Schwab, no network)
python3 test_bot_state_producer.py

# Streaming-spots dashboard tests (Patch S.1-S.4)
python3 test_schwab_stream_equity_dynamic.py
python3 test_prev_close_store.py
python3 test_spot_prices_streaming.py

# End-to-end demo of BotState pipeline (requires the repo's
# options_exposure.py, engine_bridge.py to be importable)
python3 -c "
from datetime import datetime, timezone
from raw_inputs import RawInputs
from bot_state import BotState
# ... build a synthetic chain, call BotState.build_from_raw(raw)
"
```
