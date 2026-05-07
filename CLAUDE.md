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

---

## Repo layout (what matters)

Trading engine (production, ~11k lines):
- `app.py` — main process, Telegram bridge, scheduling, Schwab adapter glue
- `dashboard.py` — Flask app for the omega_dashboard
- `rate_limiter.py`, `thesis_monitor.py`, `schwab_stream.py`,
  `schwab_adapter.py`, `recommendation_tracker.py`, `persistent_state.py`,
  `oi_flow.py`, `position_monitor.py`, etc.

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
- `test_*.py` for each — runnable without network or Schwab credentials

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

What's done as of last session (v11.6 / Patch A):
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
- BotState with permissive build, 64 fields total, ~22 currently lit per ticker
- Research page replaces the old Diagnostic placeholder

What's queued (in roughly this order):
- bot_state_producer (Patch B) — daemon thread + Redis-backed shared store.
  See spec at docs/superpowers/specs/2026-05-07-research-page-multi-dte-walls-design.md.
  Unlocks the Research page (Patch C) and silent thesis migration (Patch E later).
- Research reads from Redis (Patch C) — pure consumer; the 3-minute spin disappears here.
- Multi-DTE drilldown UI (Patch D) — click-to-expand front/t7/t30/t60 walls per card.
- canonical_technicals — RSI / MACD / ADX / VWAP. First-class for every engine.
- canonical_pivots — universal pivot math, simple consolidation
- canonical_em_state, canonical_dealer_regime, canonical_potter_box,
  canonical_flow_state, canonical_calendar — exact order TBD by Brad
- First migration: pick one production engine that calls
  `ExposureEngine.compute()` directly, redirect to read from
  `state.gex` etc., side-by-side log to confirm same value, then commit.

The migration step is the more important question once enough canonicals
exist. Right now silent_thesis still computes its own everything — the
canonicals only feed the Research dashboard. Migration is what makes the
rebuild real and not just parallel.

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
