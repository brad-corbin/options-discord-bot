# Research Page Multi-DTE Walls + Producer/Consumer Architecture

**Date:** 2026-05-07
**Author:** Brad C. + Claude (brainstorming session)
**Status:** Draft v3 — pending user review

**v3 changes from v2:** telemetry destination pinned to Redis sorted set (concrete schema); `producer_version` switched from strict equality to `>= MIN_COMPATIBLE_PRODUCER_VERSION` (additive bumps non-disruptive); sign-off checklist gains a `PRODUCER_VERSION` bump checkbox.

**v2 changes from v1:** tiered cadence (front 60s / t7 180s / t30+t60 600s); explicit rate-budget math; universe staging in Patch B; per-build timing telemetry as a hard Patch B deliverable; `producer_version` schema field with reader-side rejection; explicit no-market-hours-pause stance. See Revision History at the end.

---

## Problem Statement

The Research page on the dashboard currently:

1. **Spins for 3+ minutes on cold load** — unusable in practice, blocking the user from a tool he wants to live in.
2. **Shows wall values that don't match Market View's** — because the two pages use different chain expirations. Market View reads `gex:{ticker}` Redis snapshots written by silent thesis (which uses `_get_0dte_chain` — 0-DTE-first). Research computes fresh against next-Friday by default.
3. **Cannot show multi-DTE walls** (front, T+7, T+30, T+60) without making the loading problem dramatically worse — 5 intents × 35 tickers = 175 chain fetches per page, vs. ~140 today.

The root cause is architectural: every page hit triggers fresh computation across 35 tickers. Each hit re-runs `canonical_exposures` 35 times. The Schwab rate limit (110/min) puts a hard floor of ~76 seconds on a fully-cold load before any compute. Adding multi-DTE views breaks this entirely.

Per the canonical-rebuild discipline in `CLAUDE.md` ("Every engine consumes BotState; engines do NOT recompute"), the long-term shape is one producer of state and many consumers. Today every engine still computes its own state. The Research page is the most painful manifestation of that fragmentation, and the right fix is architectural — not another patch on the per-request path.

---

## Goals

1. Research page loads instantly regardless of cold/warm state.
2. Wall values are honest about which chain they reference (intent visible on display).
3. Multi-DTE drilldown becomes feasible (front non-0-DTE, T+7, T+30, T+60).
4. Architecture sets up the "First migration" target on the canonical-rebuild roadmap (silent thesis migrating to read from Redis instead of computing).
5. No build-now-to-tear-down-later patterns. Long-term right answer, not expedient.

## Non-Goals

- Multiple walls per side per expiration (top-3 OI clusters, etc.). Out of scope — `ExposureEngine.compute()` returns one canonical wall per side per chain, that is the canonical answer.
- Migrating silent thesis or other engines to read from Redis. Listed as an enabled future step; separate spec when its turn comes.
- Changing the wall computation algorithm. `ExposureEngine.compute()` is the canonical math, untouched.
- Changing DataRouter or the Schwab adapter. The IO layer is canonical, untouched.
- Showing 0-DTE walls on the Research page. Explicit anti-goal per user instruction.

---

## Architecture

### Long-term shape

```
                    ┌─────────────────────────────┐
                    │  bot_state_producer daemon  │
                    │  (background thread in app) │
                    │  loops every ~60s:          │
                    │  for ticker in DEFAULT:     │
                    │    for intent in 5 intents: │
                    │      build BotState         │
                    │      → Redis                │
                    └─────────────┬───────────────┘
                                  │
                                  ▼
                  Redis: bot_state:{ticker}:{intent}
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
     Research dashboard   Silent thesis        Future engines
     (Patch C, near-term) (later migration)    (conviction etc.)
```

**Single producer, many consumers.** The producer is a real first-class object in the codebase — alongside the silent-thesis daemon, the scheduler, etc. — not a Research-page implementation detail. Redis is the shared store because:
- Survives Render restarts (no first-user-pays cost after deploy)
- Cross-process safe (Render multi-worker, Flask + main bot)
- Already in use for `gex:{ticker}` and similar — natural extension
- The migration target for engines (silent thesis especially) is "read this key" — one-line change per call site

### Components

#### `canonical_expiration` — new canonical wrapper

Single function, intent-based selection:

```python
def canonical_expiration(
    ticker: str,
    intent: str,            # "zero_dte" | "front" | "t7" | "t30" | "t60"
    *,
    today: date | None = None,    # injectable for testing
    data_router=None,             # required at runtime
) -> str | None:
    """Return ISO date string for the chain matching `intent`, or None
    if no qualifying expiration exists in DataRouter's expiration list."""
```

Intent semantics:

| Intent | Rule |
|---|---|
| `zero_dte` | Today's expiration if it exists (silent thesis only) |
| `front` | First expiration with DTE ≥ 1 |
| `t7` | First expiration with DTE ≥ 7 |
| `t30` | First expiration with DTE ≥ 30 |
| `t60` | First expiration with DTE ≥ 60 |

**Never walks backwards.** All time-based intents (`t7`/`t30`/`t60`) round forward. AAPL on a Tuesday with Mon=6/Wed=8/Fri=10 weeklies → `t7` returns the Wednesday (DTE 8, first ≥ 7). SPY on a Monday → `t7` returns next Monday (DTE 7, exact). COIN with Friday-only weeklies → `t7` returns the Friday closest to but not less than 7 days out.

Returns `None` if no expiration satisfies the intent (e.g., `t60` on a ticker with only weekly chains out 30 days). Callers handle None as "data unavailable for this intent."

Wraps `DataRouter.get_expirations(ticker)` — does not introduce a new fetch path. The expiration list itself is cached by DataRouter (TTL_EXPIRATIONS = 300s).

#### `bot_state_producer` — new daemon

New module: `bot_state_producer.py`. Daemon thread started from `app.py` boot sequence alongside existing daemons.

**Tiered cadence — different intents, different refresh rates.** Walls at 1-DTE move on a 60s timescale; walls at 30/60 days don't. Spending Schwab budget refreshing them at the same rate is waste. Three independent loops:

| Tier | Intents | Cadence | Why |
|---|---|---|---|
| A | `front` | 60s | Front-DTE walls move with intraday flow; users want fresh |
| B | `t7` | 180s | One-week walls shift slowly enough that 3 min is plenty |
| C | `t30`, `t60` | 600s | Monthlies are essentially structural; 10 min is fine |

Each tier runs its own loop body:

```python
def tier_loop(intents: list[str], cadence_sec: int, ttl_sec: int):
    """One tier. Sleeps `cadence_sec` between iterations."""
    while not _shutdown.is_set():
        t0 = time.monotonic()
        for ticker in PRODUCED_TICKERS:
            for intent in intents:
                _build_and_write(ticker, intent, ttl_sec)
        elapsed = time.monotonic() - t0
        log_loop_timing(intents, elapsed)               # Patch B deliverable
        _shutdown.wait(max(0, cadence_sec - elapsed))   # don't double-fire if loop ran long

def _build_and_write(ticker, intent, ttl_sec):
    try:
        exp = canonical_expiration(ticker, intent, data_router=...)
        if exp is None:
            return                                       # no qualifying chain
        t0 = time.monotonic()
        state = BotState.build(ticker, exp, data_router=...)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log_build_timing(ticker, intent, elapsed_ms)     # Patch B deliverable
        payload = _serialize(state, producer_version=PRODUCER_VERSION)
        redis.set(f"bot_state:{ticker}:{intent}", payload, ex=ttl_sec)
    except Exception as e:
        log.warning(f"producer {ticker}/{intent}: {e}")
```

**TTL per tier:** `cadence × 3`. Tier A keys live 180s, Tier B 540s, Tier C 1800s. Readers see stale-empty within one cadence cycle if the producer stops.

**Lifecycle:** three daemon threads (one per tier), restart on uncaught exception (matches silent thesis pattern). Per audit rule 6, has unit tests for start/stop/error isolation per tier.

**Feature flag:** `BOT_STATE_PRODUCER_ENABLED` env var, defaults off — per CLAUDE.md "every new feature needs an on/off env var that defaults to off."

**Multi-worker safety:** if Render runs >1 web worker, only one should be the producer. Use a Redis lock (`SET bot_state_producer_owner $hostname NX EX 90`, refreshed every 30s). Workers that don't hold the lock skip the loop body.

**Configuration:**
- `BOT_STATE_PRODUCER_ENABLED` — on/off, default off
- `BOT_STATE_PRODUCER_TIER_A_SEC` / `_TIER_B_SEC` / `_TIER_C_SEC` — per-tier cadence overrides; defaults 60 / 180 / 600
- `BOT_STATE_PRODUCER_TICKERS` — comma-separated override of DEFAULT_TICKERS (used for universe staging — see Patch B)
- `BOT_STATE_PRODUCER_INTENTS_TIER_A` / `_TIER_B` / `_TIER_C` — comma-separated overrides per tier; defaults `front` / `t7` / `t30,t60`. `zero_dte` excluded from all tiers; only enabled when a silent-thesis migration step opts in.

#### Cadence and rate budget

Schwab rate limit is 110/min (per `rate_limiter.py`, env-tunable via `SCHWAB_RATE_PER_MIN`). The producer must coexist with trading-engine traffic without starving it.

**Steady-state fetch cost** (full 35-ticker universe, all four produced intents):

| Tier | Universe × intents | Fetches per cycle | Cycle | Fetches/min |
|---|---|---|---|---|
| A | 35 × 1 = 35 | 35 chains | 60s | 35.0 |
| B | 35 × 1 = 35 | 35 chains | 180s | 11.7 |
| C | 35 × 2 = 70 | 70 chains | 600s | 7.0 |
| **Total** | | | | **~53.7/min** |

That's ~49% of the 110/min budget, leaving 56/min for trading engine + dashboard + everything else. Trading engine's silent-thesis daemon and conviction scorer also pull from the budget — measured headroom shouldn't be a problem in practice, but **see "What we're explicitly NOT doing" below: no market-hours pause.** Backpressure is handled by the existing rate limiter, which queues requests at saturation rather than failing them.

**Cold start.** Stagger tier start times so all three don't fire at T+0. Tier A starts immediately, B at T+10s, C at T+20s. First-pass time for Tier A under saturation: 35 fetches at 110/min = 19s minimum. After that the page has front-intent data for every ticker. Drilldown rows fill in over the next 1-2 minutes as Tiers B/C complete.

**Universe scaling.** Patch B ships with a smaller universe — 10 tickers × {front, t7} = 20 entries — for production-stable validation before scaling to the full 35 × 4 = 140 entries. Scale-up criterion: no producer-related rate-limit warnings for 7 trading days. See Patch B details.

#### Redis schema

Key pattern: `bot_state:{ticker}:{intent}` — one BotState snapshot per ticker per intent.

Value envelope (JSON):

```jsonc
{
  "producer_version": 1,           // bumps on every producer change; reader rejects mismatch
  "convention_version": 2,         // Patch 9 protection; reader rejects mismatch
  "snapshot_version": 1,           // BotState schema version
  "written_at_utc": "2026-05-07T14:30:12Z",
  "intent": "front",               // duplicates the key intent; explicit on the value
  "expiration": "2026-05-08",      // the ISO date canonical_expiration resolved to
  "state": { /* dataclasses.asdict(BotState) — all ~64 fields */ }
}
```

**`producer_version`** bumps on any producer behavior change (new canonical landed, schema field added, serialization fix).

**Reader semantics:** `>= MIN_COMPATIBLE_PRODUCER_VERSION`, NOT strict equality. Readers accept any key whose `producer_version` is at or above their declared minimum. Why:
- Most producer bumps are **additive** — new field, new canonical, extra metadata. Readers reading old-format keys just miss the new fields, no display corruption.
- Strict equality would mean every bump triggers a Research-page blackout until the producer fully repopulates Redis with new-version keys. At Tier C cadence (10 min), that's a potential 10-minute outage per bump.
- Strict equality would discourage frequent bumping, which is the opposite of what we want — we want bumps to be cheap so the field actually gets used.

**Truly breaking changes** (renaming a field, changing units, removing a field readers depend on) require bumping `MIN_COMPATIBLE_PRODUCER_VERSION` in the reader. That's a deliberate code change, not just a constant bump. The deploy procedure for breaking changes:

1. Producer side: deploy new producer (writes `producer_version=N+1`).
2. Wait one full Tier C cycle (10 min) for Redis to repopulate with new-version keys.
3. Reader side: deploy new reader with `MIN_COMPATIBLE_PRODUCER_VERSION = N+1` — old keys (still in Redis under old TTL) start being rejected as "warming up" until they expire.
4. Self-heals within one Tier cycle.

For non-breaking bumps (the common case): just deploy the producer; readers continue accepting old AND new keys with `producer_version >= MIN_COMPATIBLE`.

**`convention_version` reader-side rejection** — Patch 9 (Walk 1E) settled the dealer-side sign convention to `convention_version=2`. The producer writes that. Readers verify on read: **strict equality** `if convention_version != 2`, skip render with a warning. Patch 9 protection is non-negotiable — a wrong sign on dealer Greeks is a correctness bug, not a missing field. Strict match here, `>=` for `producer_version`.

**TTL per tier:** Tier A keys 180s, Tier B 540s, Tier C 1800s (3× cadence). If the producer dies, readers see stale-empty within one cadence cycle.

**Serialization details:**
- `dataclasses.asdict(state)` handles BotState's primitive fields cleanly
- `fetch_errors` (tuple of `(operation, error_str)` tuples) → JSON nested arrays. Round-trips losslessly. **Confirmed in test_bot_state_producer.py.**
- `chain_clean: bool` from BotState is preserved in the envelope. Lets readers distinguish "data is real" from "data was built from a partially-failed fetch."
- `canonical_status` (dict) round-trips natively. Useful for surfacing "exposures status: live" / "walls status: stub: ..." on the dashboard.
- `datetime` fields serialize as ISO strings.

#### Research page (consumer)

`omega_dashboard/research_data.py` becomes a pure Redis reader.

```python
def research_data(tickers=None, *, data_router=None) -> ResearchData:
    tickers = tickers or DEFAULT_TICKERS
    snapshots = []
    for t in tickers:
        raw = redis.get(f"bot_state:{t}:front")
        if raw is None:
            snapshots.append(_warming_up_snapshot(t))
            continue
        snapshots.append(_snapshot_from_json(t, raw))
    return ResearchData(...)
```

Default card view shows only `front` intent. Drilldown click reveals `t7`/`t30`/`t60` rows — all data already in Redis, no fetch on click.

Cold-start (Render restart, daemon not yet warmed): missing keys render as "warming up" skeleton cards. Page is interactive within tens of milliseconds; cards populate as the producer's first-pass loop completes (≤ one cadence cycle).

#### Display contract

Every wall value is labeled with its intent. The current display "Call Wall · live $695" becomes "Call Wall · 1DTE $695" or "Call Wall · 7D $695" depending on which intent's data is being shown. This makes the cross-page divergence with Market View honest: Market View shows 0-DTE intent, Research shows front-non-0-DTE intent — same algorithm, different chains, different DTE tags, no user confusion.

---

## Patch Sequence

| # | Patch | Scope | Estimated size |
|---|---|---|---|
| **A** | `canonical_expiration` registry | New module + 5 intents + tests. One ad-hoc call site updated as proof. | ~250 lines incl. tests |
| **B** | `bot_state_producer` daemon + Redis schema | New module + thread. Started from `app.py` boot. Behind feature flag. | ~250 lines incl. tests |
| **C** | Research reads from Redis | Modify `research_data.py` to read keys instead of computing. **The 3-minute spin disappears here.** | ~80 lines |
| **D** | Multi-DTE drilldown UI | `research.html`: WALLS section becomes click-to-expand. Reads `t7`/`t30`/`t60` keys (already in Redis). | ~100 lines (HTML + JS) |
| **E** | *(separate, later)* Silent thesis migration | One-line per call site change. Validates the unified store works for production engines. | TBD; separate spec |

Each patch follows the canonical-rebuild discipline:
1. New file gets its own AST-clean check
2. New canonical wrapper gets a wrapper-consistency test (audit rule 5)
3. Tests run, not described (audit rule 6)
4. Don't bundle (audit rule 4) — each patch ships independently with its own tests passing
5. Update CLAUDE.md when each patch lands (the living-context rule)

### Patch A details

**New files:**
- `canonical_expiration.py` (~100 lines) — the wrapper + intent helpers
- `test_canonical_expiration.py` (~150 lines, ~10 tests)

**Tests:**
- Each intent returns the right expiration on a fixture expiration list
- Each intent returns None when no qualifying expiration exists
- Wrapper-consistency: result matches a direct `data_router.get_expirations()` filter
- Edge: today=Friday, weekly tickers, t7 jumps to following Friday (10 days)
- Edge: AAPL on Tuesday with M/W/F weeklies, t7 picks Wednesday
- Tiebreak: first qualifying date always

**Modified files:**
- `bot_state.py` — add `expiration_intent: str = "front"` field. Default value preserves backward compatibility.
- `omega_dashboard/research_data.py` — `_default_expiration()` removed; `BotState.build()` now called with `expiration=canonical_expiration(ticker, "front", ...)` per ticker.

**Side effect:** Research page walls now use front non-0-DTE chains for every ticker. Will not match Market View's 0-DTE walls. This is correct per the design — it answers a different question — but the display contract (Patch D) is what makes that obvious to the user. Until D lands, the values just change but aren't labeled with intent.

### Patch B details

**New files:**
- `bot_state_producer.py` (~250 lines — three-tier loop + telemetry + lock + serialization)
- `test_bot_state_producer.py` (~150 lines)

**Tests:**
- Each tier daemon starts and stops cleanly
- One ticker raising an exception doesn't stop the loop for other tickers (per-tier isolation)
- Redis keys written with correct envelope schema (`producer_version`, `convention_version`, `intent`, `expiration`, `state`) and per-tier TTL
- Multi-worker lock acquired/released correctly (mock Redis)
- Cadence and intent overrides via env vars
- Per-build timing logs emit to expected destination

**Modified files:**
- `app.py` — add producer to boot sequence, gated by `BOT_STATE_PRODUCER_ENABLED` env var

**Hard deliverable: per-build timing telemetry.** Each `BotState.build` call inside the producer writes one entry to a Redis sorted set:

- **Key:** `bot_state_producer:timings:{YYYYMMDD}` — one sorted set per UTC day
- **Score:** unix-timestamp-milliseconds of when the build completed (`time.time() * 1000`, integer)
- **Member:** JSON string `{"ticker": "...", "intent": "...", "elapsed_ms": N, "expiration": "..."}` — millisecond-precise scores avoid duplicate-member collisions
- **TTL:** 48 hours on the key (refreshed via `EXPIRE` on each write) — enough window for the 24-hour analysis with breathing room

Why sorted set, not stdout:
- ZRANGEBYSCORE lets us slice by time window without scraping logs
- One Redis-side query gives us "all SPY builds in the last hour" or "all builds today between 9:30 and 10:00 ET"
- Stdout-into-Render-logs is technically possible but log-grep at the end of 24h is brittle compared to a structured query

After ~24 hours of production data, run an analysis (one-off script, can be a Jupyter notebook):
- p50, p95, p99 of `elapsed_ms` per (ticker, intent)
- Which tickers/intents are >2s consistently — those are candidates for slower tiers
- Whether the steady-state fetches/min math holds (the 53.7/min estimate)

If the data shows the cadence assumptions are wrong, **re-tier in a follow-on patch with measured numbers**. This is not optional — it's a deliverable of Patch B.

**Universe staging.** Initial production rollout uses a 10-ticker × 2-intent universe via env vars:

```
BOT_STATE_PRODUCER_TICKERS=SPY,QQQ,IWM,DIA,AAPL,MSFT,NVDA,AMZN,META,TSLA
BOT_STATE_PRODUCER_INTENTS_TIER_A=front
BOT_STATE_PRODUCER_INTENTS_TIER_B=t7
BOT_STATE_PRODUCER_INTENTS_TIER_C=    # empty — Tier C disabled at first
```

Total: 20 keys at steady state. ~13 fetches/min budget. Run for **7 trading days** with no producer-related rate-limit warnings → flip env vars to scale to the full 35 × 4 = 140 keys. Documented in the deploy notes for Patch B.

**Important:** ships disabled by default (`BOT_STATE_PRODUCER_ENABLED=false`). Brad enables explicitly. Patch B can land in prod with zero behavior change.

### Patch C details

**Modified files:**
- `omega_dashboard/research_data.py` — replace BotState.build() loop with Redis reads
- `omega_dashboard/templates/dashboard/research.html` — handle "warming up" snapshots in the per-card render

**Reader-side version checks.** When deserializing each Redis value:

```python
# Lower bound: bump only on TRULY breaking schema changes.
# Additive producer bumps (new fields, new canonicals) leave this constant alone.
MIN_COMPATIBLE_PRODUCER_VERSION = 1

# Strict — Patch 9 protection is non-negotiable.
EXPECTED_CONVENTION_VERSION = 2

def _snapshot_from_envelope(ticker, raw_json):
    env = json.loads(raw_json)
    pv = env.get("producer_version")
    if pv is None or pv < MIN_COMPATIBLE_PRODUCER_VERSION:
        log.warning(f"{ticker}: producer_version {pv!r} below "
                    f"MIN_COMPATIBLE={MIN_COMPATIBLE_PRODUCER_VERSION}")
        return _warming_up_snapshot(ticker)
    if env.get("convention_version") != EXPECTED_CONVENTION_VERSION:
        log.warning(f"{ticker}: convention_version mismatch (Patch 9 protection)")
        return _warming_up_snapshot(ticker)
    return _snapshot_from_state_dict(ticker, env["state"], env["intent"], env["expiration"])
```

Old-version keys (below MIN_COMPATIBLE) and convention-mismatch keys render as "warming up" (same UX as missing key), and the reader logs a warning.

**Tests:**
- Mock Redis returning JSON for SPY, missing key for QQQ → SPY card renders, QQQ shows warming-up skeleton
- Mock Redis returning a key with `producer_version=99` (newer than reader expects) → reader **accepts**, renders successfully (forward compatibility)
- Mock Redis returning a key with `producer_version=0` (below MIN_COMPATIBLE=1) → reader logs warning, renders warming-up
- Mock Redis returning a key with `convention_version=1` → reader logs warning, renders warming-up (strict)
- Stale Redis (TTL expired between read attempts) → falls through to warming-up
- Existing 61 tests still pass

**Operational note:** Patch C requires Patch B's producer to be enabled. Sequence: ship B disabled → enable in env → verify Redis has keys → ship C.

### Patch D details

**Modified files:**
- `omega_dashboard/templates/dashboard/research.html` — WALLS section becomes a clickable disclosure
- `omega_dashboard/static/omega.css` — animation/styling for the expand state
- `omega_dashboard/research_data.py` — TickerSnapshot extended to carry t7/t30/t60 wall data alongside front

**Display detail:**
- Default: 1 row per side (Call Wall · 1DTE $X, Put Wall · 1DTE $Y)
- Expanded: 4 rows per side, one per intent, with DTE tag
- DTE tag examples: "1DTE", "8D", "32D", "61D" — shows the actual DTE of the chain that produced the wall

**Tests:**
- Mock Redis returning all 4 intents for a ticker → expanded view renders correctly
- Mock missing t60 key → expanded view shows "—" for that row, doesn't break
- Pure DOM toggle, no fetch on expand (already cached)

---

## Data Flow

**Producer cycle (per tier; A=60s, B=180s, C=600s):**

```
For each (ticker, intent) in this tier:
  → canonical_expiration(ticker, intent)
  → fetch_raw_inputs(ticker, expiration)  # via DataRouter caches
  → BotState.build_from_raw()             # 3 canonicals: gamma_flip, iv_state, exposures
  → log_build_timing(ticker, intent, elapsed_ms)
  → wrap in envelope: {producer_version, convention_version, intent, expiration, state}
  → JSON serialize
  → redis.set("bot_state:{ticker}:{intent}", json, ex=tier_ttl)

After full pass: log_loop_timing(intents, elapsed)
Sleep: max(0, cadence - elapsed)
```

**Consumer (Research page request):**

```
GET /research
  → for each ticker in DEFAULT_TICKERS:
      → redis.get("bot_state:{ticker}:front")
      → JSON deserialize → render TickerSnapshot
  → render template (instant — pure read path)

(drilldown click on a card):
  → JS reads pre-loaded t7/t30/t60 data attached to the card
  → toggles disclosure CSS class
  → no network round-trip
```

**Cold start (Render restart):**

```
T+0:    app.py boots, daemon starts
T+0:    user hits /research → all keys missing → all cards warming-up
T+5s:   producer's first loop completes ~half the universe
T+30s:  producer's first loop completes
T+30s+: page hits show real data
```

User-visible: the page is INSTANT but cards fill in over the first cadence cycle. Compared to the current "spin 3 min on cold start," this is a step-change.

---

## Error Handling

| Failure | Behavior |
|---|---|
| Producer thread crashes | Logs error, app.py daemon-restart pattern picks it up (same as silent thesis). |
| One ticker fails in producer loop | Logged, loop continues for remaining tickers. Stale or missing key TTLs out within 180s. |
| Redis unavailable | Producer logs warnings, retries with exponential backoff. Consumer (Research page) falls through to a "data layer unavailable" message — same fallback the page already has when DataRouter is None. |
| Missing keys at consumer (cold start, daemon paused) | Consumer renders "warming up" skeleton, JS polls every 5s. Equivalent to Market View's pattern. |
| Stale data (>180s old) | TTL handles it — key disappears, consumer sees missing → "warming up" skeleton. Self-healing. |
| Producer rate-limit pressure | DataRouter's existing rate_limiter wraps the underlying Schwab calls. Producer waits in line like any other caller. Loop time grows under saturation but never violates rate limit. **No market-hours pause.** |
| `producer_version` mismatch on read | Reader logs warning, renders "warming up" skeleton for that ticker. Prevents stale-format data display after a deploy. |
| `convention_version` mismatch on read | Same: log warning, render warming-up. Patch 9 protection — no display of dealer-side-flipped numbers. |

---

## Testing Plan

Per audit rule 5: every canonical wrapper has a wrapper-consistency test.
Per audit rule 6: tests run, not described.

| Test file | Status | Coverage |
|---|---|---|
| `test_raw_inputs.py` | existing | DataRouter wrapping (13 tests) |
| `test_canonical_gamma_flip.py` | existing | gamma_flip canonical (14 tests) |
| `test_canonical_iv_state.py` | existing | iv_state canonical (8 tests) |
| `test_canonical_exposures.py` | existing | exposures canonical (9 tests) |
| `test_bot_state.py` | existing | BotState dataclass + build (17 tests after Patch 11.5) |
| `test_canonical_expiration.py` | **new (Patch A)** | expiration registry, all intents, edges (~10 tests) |
| `test_bot_state_producer.py` | **new (Patch B)** | daemon lifecycle, error isolation, schema (~5 tests) |

After all patches: ~93 tests, all green.

---

## Open Questions (need decisions before implementation)

1. **Render multi-worker count**: how many web workers does Render run? If 1, the lock is overkill. If >1, the lock is essential. Defaulting to "assume >1" — implementing the lock is cheap insurance. **(v2: still open — verify before Patch B ships)**

2. **Drilldown UX trigger**: click on the WALLS eyebrow text? On the section as a whole? Disclosure triangle? — leaving for Patch D when we look at the live design. Mockup decision, not architecture.

3. **`expiration_intent` field on BotState vs envelope-only**: v2 puts `intent` and `expiration` on the JSON envelope (outside the `state` blob), keeping BotState itself pure. **Resolved: envelope-only.** No BotState dataclass field. Cleaner — the intent is producer metadata, not state data.

**Resolved in v2 (no longer open):**

- ~~JSON serialization of `fetch_errors`~~ → spec now explicitly tests round-trip in `test_bot_state_producer.py`.
- ~~`zero_dte` intent~~ → kept in `canonical_expiration` API but excluded from default `PRODUCED_INTENTS_TIER_A/B/C`. Used only by silent thesis migration (Patch E).
- ~~Cadence assumption (1s/op math)~~ → replaced with explicit per-tier rate budget; per-build timing telemetry is a hard Patch B deliverable; re-tier with measured numbers if reality diverges.
- ~~Schema provenance~~ → `producer_version`, `convention_version`, `chain_clean` all explicit in the envelope. Reader-side rejection on version mismatch.

---

## What I'm Not Going to Do (until you approve)

- Touch DataRouter / Schwab adapter
- Modify silent thesis or `_get_0dte_chain`
- Change `ExposureEngine.compute()` walls algorithm
- Compute multiple walls per side (one canonical wall per side per intent — `ExposureEngine` already returns this)
- Implement reactive WebSocket/SSE pushes (polling is fine for snapshot data)
- Bundle patches (each ships independently with passing tests)
- **Pause the producer during market open or close.** Those are exactly the windows when fresh walls matter most — positioning resets, gamma rolls, intraday flow shifts. Pausing then would show stale data when the user most needs fresh. Backpressure is handled by the existing rate_limiter; if trading-engine traffic surges, producer requests queue behind it and complete a cycle later. That's the right tradeoff: producer stretches, never blanks.

---

## Sign-off Checkboxes (for the implementer)

Before each patch lands:
- [ ] AST-clean on every modified .py file (audit rule 3)
- [ ] All five existing canonical test suites still pass
- [ ] New tests for the patch all green
- [ ] Patch is single-concept (audit rule 4)
- [ ] Wrapper-consistency test exists if a new canonical (audit rule 5)
- [ ] CLAUDE.md updated to reflect the new "what's done" line (living-context rule)
- [ ] **If producer schema or serialization changed: `PRODUCER_VERSION` bumped in `bot_state_producer.py`. If the change is breaking, `MIN_COMPATIBLE_PRODUCER_VERSION` also bumped in the reader (`research_data.py`).** This is structural insurance — the version field only protects readers if it actually gets bumped when behavior changes. Cheap to do at the moment of the change; impossible to retrofit later.

---

## Revision History

### v3 — 2026-05-07

Pushback after v2 identified three operational gaps. v3 closes them:

**Resolved:**
- **Telemetry destination pinned.** v2 left "stdout or Redis sorted set" as an open choice. v3 specifies Redis sorted set with concrete schema (`bot_state_producer:timings:{YYYYMMDD}`, score=unix_ms, member=JSON, TTL=48h). Enables ZRANGEBYSCORE queries instead of log-scraping for the 24h analysis.
- **`producer_version` semantics changed from strict equality to `>= MIN_COMPATIBLE_PRODUCER_VERSION`.** v2's strict-match design would cause a Research blackout on every producer bump (up to 10 min at Tier C cadence). v3 accepts forward-compatible versions; `MIN_COMPATIBLE` only bumps on truly breaking schema changes. Documented deploy procedure for breaking vs additive bumps. `convention_version` stays strict (Patch 9 protection is non-negotiable).
- **Sign-off checklist gains `PRODUCER_VERSION` bump checkbox.** Structural reminder — without it, the version field only works if the human discipline holds. Cheap insurance.

**Unchanged from v2:**
- Tiered cadence (front 60s / t7 180s / t30+t60 600s)
- Rate-budget math (~53.7/min vs 110/min)
- Universe staging (10×2 → 35×4)
- No-market-hours-pause stance
- Five-intent registry
- 5-patch sequence

### v2 — 2026-05-07

Authored after pushback on v1. Most v1 architecture survives intact; the changes below tighten the operational story.

**Added:**
- **Tiered cadence** (`bot_state_producer` → tier table). Tier A `front` 60s; Tier B `t7` 180s; Tier C `t30`+`t60` 600s. Replaces the v1 single 60s flat loop. Aligns refresh rate with how fast each intent's data actually changes.
- **Cadence and rate budget subsection** with explicit math (~53.7 fetches/min steady-state vs 110/min Schwab budget). Cold-start staggering (Tier A at T+0, B at T+10s, C at T+20s).
- **Per-build timing telemetry as a hard Patch B deliverable** — log `(ticker, intent, elapsed_ms)` per build, examine after 24h, re-tier with measured data if assumptions are wrong.
- **Universe staging in Patch B** — initial 10 × 2 = 20 keys for production-stable validation; scale to 35 × 4 = 140 via env vars after 7 trading days clean.
- **`producer_version` schema field** with reader-side rejection on mismatch. Prevents stale-format data display after deploys.
- **Explicit `convention_version=2` reader-side rejection** (Patch 9 protection) wired into Patch C's reader code.
- **`chain_clean` and `fetch_errors` confirmed in envelope serialization** — readers can distinguish real data from data built on a partially-failed fetch.
- **Explicit no-market-hours-pause stance** in "What I'm Not Going to Do" — those windows are when fresh walls matter most; pausing then defeats the purpose. Rate limiter handles priority.

**Resolved (moved out of Open Questions):**
- JSON serialization of `fetch_errors` (now explicitly tested in `test_bot_state_producer.py`)
- `zero_dte` intent placement (in registry, excluded from default produced intents)
- `expiration_intent` field placement — **decision: envelope-only, not on BotState dataclass.** Keeps BotState pure.

**Unchanged from v1:**
- Five-intent registry (`zero_dte`, `front`, `t7`, `t30`, `t60`)
- 5-patch sequence (A: canonical_expiration; B: producer; C: Research reads Redis; D: drilldown UI; E: silent thesis migration — separate spec)
- Single producer / many consumers architecture
- Multi-worker Redis lock
- Feature-flag gating on Patch B (`BOT_STATE_PRODUCER_ENABLED` defaults off)
- Cold-start "warming up" skeleton UX
- Audit-rule sign-off checklist

### v1 — 2026-05-07

Initial design. Single 60s flat cadence; full 35 × 4 universe from day one; no rate-budget math; basic Redis schema (no producer_version); telemetry mentioned but not a hard deliverable. Pushback identified four real gaps (cadence math, universe staging, schema provenance, telemetry-as-deliverable) → v2.
