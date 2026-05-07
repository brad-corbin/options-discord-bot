# Research Page Multi-DTE Walls + Producer/Consumer Architecture

**Date:** 2026-05-07
**Author:** Brad C. + Claude (brainstorming session)
**Status:** Draft — pending user review

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

Loop body, every `CADENCE_SECONDS` (default 60):

```python
for ticker in DEFAULT_TICKERS:                  # the 35-ticker universe
    for intent in PRODUCED_INTENTS:             # 5 intents
        try:
            exp = canonical_expiration(ticker, intent, data_router=...)
            if exp is None:
                continue                        # skip if no qualifying chain
            state = BotState.build(ticker, exp, data_router=...)
            payload = _serialize(state)
            redis.set(f"bot_state:{ticker}:{intent}", payload,
                      ex=CADENCE_SECONDS * 3)   # 180s TTL
        except Exception as e:
            log.warning(f"producer {ticker}/{intent}: {e}")
            continue                            # one ticker error doesn't stop the loop
```

**Lifecycle:** daemon thread, restarts on uncaught exception (matches silent thesis pattern). Logs once per loop with timing breakdown. Per audit rule 6, has unit tests for start/stop/error isolation.

**Feature flag:** `BOT_STATE_PRODUCER_ENABLED` env var, defaults off — per CLAUDE.md "every new feature needs an on/off env var that defaults to off."

**Multi-worker safety:** if Render runs >1 web worker, only one should be the producer. Use a Redis lock (`SET bot_state_producer_owner $hostname NX EX 90`, refreshed every 30s). Workers that don't hold the lock skip the loop body.

**Configuration:**
- `BOT_STATE_PRODUCER_ENABLED` — on/off, default off
- `BOT_STATE_PRODUCER_CADENCE_SEC` — loop interval, default 60
- `BOT_STATE_PRODUCER_TICKERS` — comma-separated override of DEFAULT_TICKERS
- `BOT_STATE_PRODUCER_INTENTS` — comma-separated override of {front, t7, t30, t60} (zero_dte excluded by default; only enabled for silent-thesis migration step)

#### Redis schema

Key pattern: `bot_state:{ticker}:{intent}` — one BotState snapshot per ticker per intent.

Value: JSON-serialized BotState dataclass. All ~64 fields preserved, including `canonical_status` dict (for debugging "is this canonical live for this ticker") and `fetch_errors` tuple.

TTL: 180 seconds (3× default cadence). If the producer dies, readers see stale-empty within one cadence cycle, never stale-stuck.

Serialization detail: `dataclasses.asdict(state)` handles primitives. `fetch_errors` (tuple of tuples) round-trips through JSON as nested arrays. `canonical_status` (dict) round-trips natively. `datetime` fields serialize as ISO strings.

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
- `bot_state_producer.py` (~150 lines)
- `test_bot_state_producer.py` (~100 lines)

**Tests:**
- Daemon starts and stops cleanly
- One ticker raising an exception doesn't stop the loop for other tickers
- Redis keys written with correct schema and TTL
- Multi-worker lock acquired/released correctly (mock Redis)
- Cadence configurable via env var

**Modified files:**
- `app.py` — add producer to boot sequence, gated by `BOT_STATE_PRODUCER_ENABLED` env var

**Important:** ships disabled by default. Brad enables it explicitly when ready to validate. This means Patch B can ship to prod without affecting anything.

### Patch C details

**Modified files:**
- `omega_dashboard/research_data.py` — replace BotState.build() loop with Redis reads
- `omega_dashboard/templates/dashboard/research.html` — handle "warming up" snapshots in the per-card render

**Tests:**
- Mock Redis returning JSON for SPY, missing key for QQQ → SPY card renders, QQQ shows warming-up skeleton
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

**Producer cycle (every 60s):**

```
ticker, intent
  → canonical_expiration(ticker, intent)
  → fetch_raw_inputs(ticker, expiration)  # via DataRouter caches
  → BotState.build_from_raw()             # 3 canonicals: gamma_flip, iv_state, exposures
  → JSON serialize
  → redis.set("bot_state:{ticker}:{intent}", json, ex=180)
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
| Producer rate-limit pressure | DataRouter's existing rate_limiter wraps the underlying Schwab calls. Producer waits in line like any other caller. Loop time grows under saturation but never violates rate limit. |

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

1. **Render multi-worker count**: how many web workers does Render run? If 1, the lock is overkill. If >1, the lock is essential. Defaulting to "assume >1" — implementing the lock is cheap insurance.

2. **JSON serialization edge case**: `BotState.fetch_errors` is `tuple[tuple[str, str]]`. Through `dataclasses.asdict` it becomes nested lists. On read, the consumer doesn't currently care (it only reads a few specific fields). Confirming this is fine, not a blocker.

3. **Drilldown UX trigger**: click on the WALLS eyebrow text? On the section as a whole? Disclosure triangle? — leaving for Patch D when we look at the live design. Mockup decision, not architecture.

4. **`zero_dte` intent** — included in the registry per design but EXCLUDED from `PRODUCED_INTENTS` by default. Only useful for silent thesis (Patch E, separate). Keeping it in the registry now means Patch E doesn't need to revisit the `canonical_expiration` API.

5. **`expiration_intent` field on BotState** — adds metadata. Default `"front"` keeps existing test coverage clean. Worth confirming this is the right metadata to attach (vs. e.g. a separate dict on the producer's serialized envelope, leaving BotState pure).

---

## What I'm Not Going to Do (until you approve)

- Touch DataRouter / Schwab adapter
- Modify silent thesis or `_get_0dte_chain`
- Change `ExposureEngine.compute()` walls algorithm
- Compute multiple walls per side
- Implement reactive WebSocket/SSE pushes (polling is fine for snapshot data)
- Bundle patches (each ships independently with passing tests)

---

## Sign-off Checkboxes (for the implementer)

Before each patch lands:
- [ ] AST-clean on every modified .py file (audit rule 3)
- [ ] All five existing canonical test suites still pass
- [ ] New tests for the patch all green
- [ ] Patch is single-concept (audit rule 4)
- [ ] Wrapper-consistency test exists if a new canonical (audit rule 5)
- [ ] CLAUDE.md updated to reflect the new "what's done" line (living-context rule)
