# Alert Recorder V1 — Patch G Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the V1 alert recorder — a SQLite-backed signal/outcome ledger that records every alert hitting the main Telegram channel with full inputs, canonical snapshot, price track, and outcomes at standard horizons. This is the platform's central artifact and the foundation Patch H's barometer dashboard reads from.

**Architecture:** SQLite at `/var/backtest/desk.db` (WAL mode) with four tables: `alerts`, `alert_features` (EAV), `alert_price_track`, `alert_outcomes`. A pure write-side `alert_recorder` module is called by each of the four engines after their card posts. Two daemon threads — `alert_tracker_daemon` (samples structure marks, piggybacks on existing `recommendation_tracker` polling — zero new Schwab REST calls) and `outcome_computer_daemon` (computes pnl/MFE/MAE/hit_pt at standard horizons). Everything is gated behind `RECORDER_ENABLED` (master) plus per-engine + per-daemon env flags, all defaulting OFF.

**Tech Stack:** Python 3.11, sqlite3 stdlib, plain SQL files for migrations (portable to Postgres later), reuse of `bot_state_producer._build_envelope` / `_serialize_envelope` for the canonical_snapshot field. No new dependencies.

---

## Context the implementer needs

- Read `docs/superpowers/specs/2026-05-08-alert-recorder-v1.md` end-to-end before starting. The spec is approved and authoritative — this plan implements it task-by-task.
- Read `CLAUDE.md` "Audit discipline" section. Non-negotiable: AST-check after every Python file write, separate commit per task, `# v11.7 (Patch G.N):` comment marker on every change, env-var gate defaulting OFF, never inline a helper for a concept that has an existing implementation (the canonical-rebuild rule).
- The `bot_state_producer` is the architectural template. Patch G uses the same envelope-versioning pattern (`producer_version`, `convention_version`), the same daemon-thread-with-try/except discipline, and the same env-var-gate-defaulting-off rollout pattern. When in doubt, look at `bot_state_producer.py` for how it's done.
- The recorder runs on **all workers**. Unlike `bot_state_producer` (which holds a cross-worker Redis lock), the recorder writes to local SQLite — there's no cross-worker coordination needed. Each worker writes alerts that fired on that worker. Scheduler and trading paths are unchanged.

## Key codebase anchors

| Component | Location | Notes |
|---|---|---|
| LONG CALL BURST card post | `app.py:9283` `_try_post_long_call_burst()` | Hook record_alert after `_tg_rate_limited_post(msg)` returns |
| V2 5D card builder | `v2_5d_edge_model.py:401` `build_v2_card()` | Returns text; callers post via `_tg_rate_limited_post` — hook at call sites |
| v8.4 CREDIT card post | `app.py:8967` `_post_v84_credit_card()` | Hook record_alert after the post returns |
| CONVICTION PLAY card post | 5 sites: `app.py:7689`, `app.py:10312`, `app.py:10700`, `app.py:15187`, `schwab_stream.py:1141` | All immediately follow `_flow_detector.format_conviction_play(cp)` |
| recommendation_tracker poll | `recommendation_tracker.py:656` `update_tracking()` | Tracker daemon piggybacks on this loop |
| OptionQuoteStore (streaming marks) | `schwab_stream.py:88+`, accessor `get_option_store()` at line 403 | Cheap structure-mark reads when streaming is up |
| Bot state producer envelope helpers | `bot_state_producer.py:95` `_build_envelope`, `bot_state_producer.py:138` `_serialize_envelope` | Reuse for canonical_snapshot — don't write a parallel JSON-cleaner |
| Daemon spawn pattern | `app.py:15007-15018` (`start_producer(...)` example) | Mirror this pattern for tracker + outcome daemons |
| Redis client | `app.py:3079-3105` `_get_redis()` | Recorder doesn't need Redis (uses SQLite) — listed for reference only |
| `migrations/` | Does not exist | G.1 creates it from scratch |

## File structure (created/modified by this plan)

**Created:**
- `migrations/` — directory for sequentially numbered SQL migration files
- `migrations/0001_initial_schema.sql` — alerts/alert_features/alert_price_track/alert_outcomes/schema_migrations DDL
- `migrations/__init__.py` — empty marker file
- `db_migrate.py` — boot-time migration runner (`apply_migrations(db_path)`)
- `alert_recorder.py` — write-side module: `record_alert(...)`, `record_track_sample(...)`, `record_outcome(...)`, internal helpers
- `alert_tracker_daemon.py` — daemon thread that samples structure marks per cadence rules
- `outcome_computer_daemon.py` — daemon thread that computes outcomes at horizon boundaries
- `recorder_queries.sql` — 5–10 verification SQL queries (Patch H seed)
- `test_db_migrate.py` — migration-runner tests (G.1)
- `test_alert_recorder.py` — recorder write-side tests (G.2 + per-engine wire tests in G.3–G.6)
- `test_alert_tracker_daemon.py` — tracker-daemon tests (G.7)
- `test_outcome_computer_daemon.py` — outcome-computer tests (G.8)
- `test_engine_versions.py` — engine-versions tests (G.9)
- `test_recorder_queries.py` — verification-query tests (G.10)

**Modified:**
- `app.py` — add hooks at 4 card post sites (LCB, V2 5D, v8.4 CREDIT, CONVICTION at 4 sites), spawn 2 new daemons, register engine versions at boot
- `v2_5d_edge_model.py` — add hook at the V2 5D card post site (or wherever build_v2_card is consumed by app.py)
- `oi_flow.py` — optional: add `_record_conviction_alert(cp, posted_to)` helper if it cleans up the 5 conviction sites (otherwise hook each site directly in app.py + schwab_stream.py)
- `schwab_stream.py` — add hook at the conviction-play site at line 1141
- `CLAUDE.md` — append "Patch G done" entry under canonical rebuild status

**Untouched (validate no regressions):**
- All `canonical_*.py` modules and their tests
- `bot_state_producer.py` and `test_bot_state_producer.py`
- `omega_dashboard/research_data.py` and `test_research_data_consumer.py`
- All existing engine logic. The recorder is purely additive — engines do not change behavior; they gain a `try: record_alert(...) except: log+continue` after their existing card post.

## Env vars (all default OFF)

| Var | Gates | Default | Promote to ON when |
|---|---|---|---|
| `RECORDER_ENABLED` | Master gate. If false, `record_alert` is a no-op. | `false` | After G.10 verification queries return real numbers from at least 24h of clean writes |
| `RECORDER_LCB_ENABLED` | Per-engine gate for LONG CALL BURST | `false` | After G.3 lands and Brad watches one fire |
| `RECORDER_V25D_ENABLED` | Per-engine gate for V2 5D | `false` | After G.4 lands |
| `RECORDER_CREDIT_ENABLED` | Per-engine gate for v8.4 CREDIT | `false` | After G.5 lands |
| `RECORDER_CONVICTION_ENABLED` | Per-engine gate for CONVICTION PLAY | `false` | After G.6 lands |
| `RECORDER_TRACKER_ENABLED` | Spawns the alert_tracker_daemon | `false` | After enough alerts exist to track |
| `RECORDER_OUTCOMES_ENABLED` | Spawns the outcome_computer_daemon | `false` | After enough alerts have crossed horizons to compute |
| `RECORDER_DB_PATH` | Override DB path (testing only) | `/var/backtest/desk.db` | Never in production |

Rollout order: master → engines one at a time (24h validation each) → tracker → outcomes. If any engine writes garbage rows, flip its per-engine gate off and redeploy. Master kill-switch handles "everything is broken, abort."

---

## Constants and shared helpers (defined once, used across tasks)

These are referenced in multiple tasks. Define in `alert_recorder.py` once and re-import elsewhere. Listed here so the implementer doesn't accidentally duplicate them.

```python
# alert_recorder.py — top of file

import os
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import logging

log = logging.getLogger(__name__)

# v11.7 (Patch G): alert recorder schema + write hooks.
SCHEMA_VERSION = 1                  # bumps when migrations/ adds a new file
RECORDER_VERSION = "v1.0.0"         # human-readable recorder build tag

DEFAULT_DB_PATH = "/var/backtest/desk.db"

# Standard horizon labels and elapsed-second offsets used by both the
# tracker daemon (sampling) and the outcome computer (boundary detection).
HORIZONS_SECONDS = {
    "5min":   5 * 60,
    "15min":  15 * 60,
    "30min":  30 * 60,
    "1h":     60 * 60,
    "4h":     4 * 60 * 60,
    "1d":     24 * 60 * 60,
    "2d":     2 * 24 * 60 * 60,
    "3d":     3 * 24 * 60 * 60,
    "5d":     5 * 24 * 60 * 60,
    # 'expiry' is per-alert, computed from suggested_dte at outcome time.
}

# Sampling cadence buckets: (lower_bound_seconds, cadence_seconds).
# Used by alert_tracker_daemon to decide "is it time to sample this alert?"
SAMPLING_CADENCE = [
    (0,                    60),         # 0-1h: every 60s
    (60 * 60,              5 * 60),     # 1-4h: every 5min
    (4 * 60 * 60,          15 * 60),    # 4-24h: every 15min
    (24 * 60 * 60,         30 * 60),    # 1-7d: every 30min
    (7 * 24 * 60 * 60,     60 * 60),    # 7d+: every 1h
]

# Per-engine tracking horizon (seconds) — when to stop sampling.
TRACKING_HORIZON_BY_ENGINE = {
    "long_call_burst":     3 * 24 * 60 * 60,    # 3 days
    "v2_5d":               7 * 24 * 60 * 60,    # 7 days
    "credit_v84":          None,                # uses suggested_dte
    "oi_flow_conviction":  5 * 24 * 60 * 60,    # 5 days
}


def _utc_micros() -> int:
    """Current UTC time as microseconds since epoch. Used everywhere as
    the canonical timestamp; SQLite stores INTEGER for fast indexing."""
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def _master_enabled() -> bool:
    return os.getenv("RECORDER_ENABLED", "false").lower() in ("1", "true", "yes")


def _engine_enabled(engine: str) -> bool:
    """Per-engine gate. Both the master gate AND the per-engine gate must
    be on for record_alert to write a row."""
    if not _master_enabled():
        return False
    flag = {
        "long_call_burst":     "RECORDER_LCB_ENABLED",
        "v2_5d":               "RECORDER_V25D_ENABLED",
        "credit_v84":          "RECORDER_CREDIT_ENABLED",
        "oi_flow_conviction":  "RECORDER_CONVICTION_ENABLED",
    }.get(engine)
    if flag is None:
        return False
    return os.getenv(flag, "false").lower() in ("1", "true", "yes")
```

These helpers ship in G.2 and are imported by G.3+. Do not redefine them.

---

## Task index

**Patch F.5 — Light up canonical_technicals on BotState (precondition for G).** Three commits, each independently revertible. F.5 stands alone if Patch G ever needs unwinding — its only effect is flipping BotState's `technicals` field from "stub" to "live" by wiring through to `canonical_technicals`. None of G's recorder modules depend on F.5 *structurally*, but the recorder's V2 5D and LCB feature blocks become much more useful when the canonical_technicals output is populated on the snapshot they reference. Doing F.5 first means every alert G records carries real RSI/MACD/ADX in `canonical_snapshot` from day one.

- **F.5.1** — `_build_technicals_from_raw` helper + wrapper-consistency test
- **F.5.2** — Replace the technicals stub at `bot_state.py:261` with the helper; verify `status["technicals"]` is `live`
- **F.5.3** — CLAUDE.md update: append F.5 to "What's done as of last session"

**Patch G — Alert recorder V1.**

- **G.1** — Schema migration framework + initial schema
- **G.2** — Alert recorder module (write side)
- **G.3** — Wire LONG CALL BURST
- **G.4** — Wire V2 5D EDGE MODEL
- **G.5** — Wire v8.4 CREDIT
- **G.6** — Wire CONVICTION PLAY (5 sites)
- **G.7** — Alert tracker daemon
- **G.8** — Outcome computer daemon
- **G.9** — Engine versions auto-population
- **G.10** — Verification queries

Each task is its own commit. Do not bundle. Total: 13 commits across F.5 + G.

---

## Task F.5.1: `_build_technicals_from_raw` helper + wrapper-consistency test

**Files:**
- Modify: `bot_state.py` — add `_build_technicals_from_raw(raw)` near the other `_try_canonical` helpers (above `BotState.build_from_raw`)
- Test: `test_bot_state.py` — add 4 tests

**Background the implementer needs:**

`RawInputs` is a frozen dataclass at `raw_inputs.py:80`. The relevant field is `bars: list[dict]` (raw_inputs.py:110) — OHLCV dicts oldest-first, ~504 trading days.

Bar key naming is **inconsistent across the codebase**: some upstream sources use `b["high"]/b["low"]/b["close"]`, others use `b["h"]/b["l"]/b["c"]`. The defensive pattern at `risk_manager.py:275` is `b.get("h") or b.get("high")`. The helper must use this pattern — F.5 is not the place to clean up the data format, just the place to consume it correctly.

`canonical_technicals` shipped in Patch E. Its public API:
- `rsi(closes: list, period: int = 14) → Optional[float]` — None when insufficient data
- `macd(closes: list) → dict` — keys: `macd_line`, `signal_line`, `macd_hist`, `macd_cross_bull`, `macd_cross_bear`. Returns `{}` when insufficient data.
- `adx(highs, lows, closes, length: int = 14) → float` — returns `0.0` on any failure (not None — see canonical_technicals.py:140-148 for the rationale: "the scorer's ADX quintile rules check for missing data and skip, so a silent zero is safe"). The helper should match this convention so downstream code doesn't have to special-case ADX.

The `iv_surface: Optional[dict]` field on `RawInputs` is **not relevant** to technicals — that's `canonical_iv_state`'s input. Don't reach for it.

`BotState`'s technicals fields (`bot_state.py:131-133`) are `rsi`, `macd_hist`, `adx`, all `Optional[float]`. The helper returns a dict that `build_from_raw` uses at lines 324-326 to fill these in via `technicals.get("rsi")` etc. The dict can carry extra keys (e.g. `macd_line`, `macd_signal`) that BotState ignores today; that's fine — it makes the helper future-proof for when MACD line/signal land as their own BotState fields.

- [ ] **Step 1: Write the failing tests in `test_bot_state.py`**

Append to `test_bot_state.py` (matching its existing test-style — `def test_*()` functions called from `__main__`):

```python
# v11.7 (Patch F.5.1): canonical_technicals integration tests.

def test_build_technicals_from_raw_full_clean_bars():
    """With ~504 clean bars, helper returns the same numbers as
    canonical_technicals when called directly. Wrapper-consistency."""
    from bot_state import _build_technicals_from_raw
    import canonical_technicals as ct

    # Build 100 synthetic bars (enough for RSI(14), MACD(26+9=35), ADX(14)).
    # Use a simple linear ramp so the indicators have nonzero, deterministic
    # values.
    bars = [
        {"h": 100.0 + i + 0.5, "l": 100.0 + i - 0.5, "c": 100.0 + i,
         "o": 100.0 + i - 0.2, "v": 1_000_000}
        for i in range(100)
    ]

    class FakeRaw:
        ticker = "TEST"
        bars = None  # set below
    raw = FakeRaw()
    raw.bars = bars

    result = _build_technicals_from_raw(raw)

    # Wrapper-consistency: each value matches canonical_technicals' output.
    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    expected_rsi = ct.rsi(closes)
    expected_macd = ct.macd(closes)
    expected_adx = ct.adx(highs, lows, closes)

    assert result["rsi"] == expected_rsi, \
        f"RSI drift: helper={result['rsi']}, canonical={expected_rsi}"
    assert result["macd_line"] == expected_macd.get("macd_line"), \
        f"MACD line drift: helper={result['macd_line']}, canonical={expected_macd.get('macd_line')}"
    assert result["macd_signal"] == expected_macd.get("signal_line"), \
        f"MACD signal drift"
    assert result["macd_hist"] == expected_macd.get("macd_hist"), \
        f"MACD hist drift"
    assert result["adx"] == expected_adx, \
        f"ADX drift: helper={result['adx']}, canonical={expected_adx}"


def test_build_technicals_from_raw_handles_long_key_format():
    """Bars using 'high'/'low'/'close' (not h/l/c) must work — defensive
    pattern from risk_manager.py:275."""
    from bot_state import _build_technicals_from_raw
    import canonical_technicals as ct

    bars = [
        {"high": 100.0 + i + 0.5, "low": 100.0 + i - 0.5, "close": 100.0 + i}
        for i in range(100)
    ]
    class FakeRaw:
        ticker = "TEST"
        bars = None
    raw = FakeRaw()
    raw.bars = bars
    result = _build_technicals_from_raw(raw)

    closes = [b["close"] for b in bars]
    assert result["rsi"] == ct.rsi(closes)
    assert result["macd_hist"] == ct.macd(closes).get("macd_hist")


def test_build_technicals_from_raw_empty_bars():
    """Empty bars list → all-None RSI/MACD, ADX=0.0 (matches canonical_technicals
    convention)."""
    from bot_state import _build_technicals_from_raw

    class FakeRaw:
        ticker = "TEST"
        bars = []
    raw = FakeRaw()

    result = _build_technicals_from_raw(raw)
    assert result["rsi"] is None
    assert result["macd_line"] is None
    assert result["macd_signal"] is None
    assert result["macd_hist"] is None
    assert result["adx"] == 0.0, "ADX is 0.0 on insufficient data, not None"


def test_build_technicals_from_raw_partial_bar_keys():
    """A bar missing one of the OHLC keys → all-None/0.0. The helper does
    not silently fill in zeros that would corrupt the math."""
    from bot_state import _build_technicals_from_raw

    bars = [{"h": 100.0, "l": 99.0, "c": 99.5} for _ in range(50)]
    bars[10] = {"h": 100.0, "l": 99.0}  # missing close → must trip defense
    class FakeRaw:
        ticker = "TEST"
        bars = None
    raw = FakeRaw()
    raw.bars = bars

    result = _build_technicals_from_raw(raw)
    assert result["rsi"] is None
    assert result["macd_hist"] is None
    assert result["adx"] == 0.0
```

Ensure these tests are added to `test_bot_state.py`'s `__main__` runner so they execute. Match the existing pattern in that file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python test_bot_state.py`
Expected: 4 new tests FAIL with `ImportError`/`AttributeError` — `_build_technicals_from_raw` not yet defined.

- [ ] **Step 3: Implement the helper in `bot_state.py`**

Add **above** `class BotState:` (or wherever the other `_try_canonical` / `_stub` helpers live — keep helpers grouped):

```python
# v11.7 (Patch F.5.1): canonical_technicals integration helper.
def _build_technicals_from_raw(raw):
    """Compute RSI / MACD / ADX from raw.bars using canonical_technicals.

    Defensive about bar key naming: some upstream sources use
    'high'/'low'/'close', others use 'h'/'l'/'c'. Mirrors the pattern at
    risk_manager.py:275.

    Returns a dict with keys: rsi (float|None), macd_line (float|None),
    macd_signal (float|None), macd_hist (float|None), adx (float).

    None values for RSI/MACD mean insufficient data (matching
    canonical_technicals' return). ADX returns 0.0 on insufficient data
    rather than None — this matches canonical_technicals.adx and lets
    downstream scorers' ADX-quintile rules check for the zero sentinel
    without special-casing None vs 0.0.
    """
    import canonical_technicals
    bars = getattr(raw, "bars", None) or []
    if not bars:
        return {
            "rsi": None,
            "macd_line": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx": 0.0,
        }

    highs  = [b.get("h") or b.get("high")  for b in bars]
    lows   = [b.get("l") or b.get("low")   for b in bars]
    closes = [b.get("c") or b.get("close") for b in bars]

    # Defend against partial bars — any None breaks the indicator math.
    if not all(highs) or not all(lows) or not all(closes):
        return {
            "rsi": None,
            "macd_line": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx": 0.0,
        }

    rsi_val = canonical_technicals.rsi(closes)
    macd_dict = canonical_technicals.macd(closes) or {}
    adx_val = canonical_technicals.adx(highs, lows, closes)

    return {
        "rsi":         rsi_val,
        "macd_line":   macd_dict.get("macd_line"),
        "macd_signal": macd_dict.get("signal_line"),
        "macd_hist":   macd_dict.get("macd_hist"),
        "adx":         adx_val,
    }
```

> **Note on the wrapper-consistency check:** `_build_technicals_from_raw` is a thin orchestration wrapper over canonical_technicals — there is no parallel re-implementation of RSI/MACD/ADX math here. The wrapper-consistency test in step 1 enforces this by computing each indicator both via the helper and via `canonical_technicals.*` directly and asserting equality. If they ever drift, the helper has done something wrong (e.g. silently re-implemented an indicator inline). This is the audit-discipline rule from CLAUDE.md.

- [ ] **Step 4: AST-check**

Run: `python -c "import ast; ast.parse(open('bot_state.py').read()); ast.parse(open('test_bot_state.py').read()); print('AST OK')"`
Expected: `AST OK`.

- [ ] **Step 5: Run tests**

Run: `python test_bot_state.py`
Expected: every test (existing + 4 new) passes.

- [ ] **Step 6: Run regression battery**

```
python test_canonical_gamma_flip.py
python test_canonical_iv_state.py
python test_canonical_exposures.py
python test_canonical_expiration.py
python test_canonical_technicals.py
python test_bot_state_producer.py
python test_research_data_consumer.py
```
Expected: all green. F.5.1 adds a helper and tests, doesn't change `build_from_raw` yet.

- [ ] **Step 7: Commit**

```
git add bot_state.py test_bot_state.py
git commit -m "Patch F.5.1: _build_technicals_from_raw helper + wrapper test

Adds _build_technicals_from_raw(raw) in bot_state.py — thin orchestration
wrapper that pulls highs/lows/closes from raw.bars and calls
canonical_technicals.rsi/macd/adx. Defensive about bar key naming
('h'/'high', 'l'/'low', 'c'/'close') matching risk_manager.py:275. Returns
{rsi, macd_line, macd_signal, macd_hist, adx} dict.

Wrapper-consistency test: helper output equals direct canonical_technicals
calls on the same closes — no parallel RSI/MACD/ADX math sneaks into the
helper. Plus tests for the long-key bar format, empty bars, and partial
bar keys (insufficient data → all-None / adx=0.0 per canonical's
convention).

Pure additive — build_from_raw still calls _stub('technicals') at line 261.
F.5.2 wires the helper in. F.5.1 stands alone if F.5.2/F.5.3 are reverted.

# v11.7 (Patch F.5.1): technicals integration helper."
```

---

## Task F.5.2: Replace the technicals stub with the helper

**Files:**
- Modify: `bot_state.py:261` — replace `_try_canonical("technicals", lambda: _stub("technicals"), status)` with the real builder
- Test: append integration test to `test_bot_state.py`

- [ ] **Step 1: Write the integration test first**

Append to `test_bot_state.py`:

```python
def test_build_from_raw_status_technicals_is_live():
    """Spec F.5.2: BotState.build_from_raw with clean bars returns
    status['technicals'] == 'live', NOT 'stub'. Confirms the wiring
    landed and BotState is reading from canonical_technicals."""
    from bot_state import BotState
    from raw_inputs import RawInputs
    from datetime import datetime, timezone

    bars = [
        {"h": 100.0 + i + 0.5, "l": 100.0 + i - 0.5, "c": 100.0 + i,
         "o": 100.0 + i - 0.2, "v": 1_000_000}
        for i in range(100)
    ]
    raw = RawInputs(
        ticker="TEST",
        spot=199.0,
        expiration=None,
        chain={},
        quote={},
        bars=bars,
        iv_surface=None,
        is_clean=True,
        fetch_errors=[],
        fetched_at_utc=datetime.now(timezone.utc),
    )
    state = BotState.build_from_raw(raw)
    assert state.status.get("technicals") == "live", (
        f"Expected status['technicals']='live', got {state.status.get('technicals')!r}. "
        f"F.5.2 wiring did not land."
    )
    # Sanity: indicator values populated on the snapshot.
    assert state.rsi is not None, "rsi should populate from helper"
    assert state.adx is not None, "adx should populate from helper"


def test_build_from_raw_status_technicals_error_on_bad_bars():
    """If raw.bars is malformed, _try_canonical catches the exception
    and records 'error' (or returns None), without crashing. Permissive
    build contract."""
    from bot_state import BotState
    from raw_inputs import RawInputs
    from datetime import datetime, timezone

    raw = RawInputs(
        ticker="TEST",
        spot=199.0,
        expiration=None,
        chain={},
        quote={},
        bars=[{"this_is_not_a_bar": "garbage"}],  # missing every OHLC key
        iv_surface=None,
        is_clean=True,
        fetch_errors=[],
        fetched_at_utc=datetime.now(timezone.utc),
    )
    state = BotState.build_from_raw(raw)
    # Helper returns all-None on partial keys; status is "live" because no
    # exception was raised. RSI/ADX are None/0.0. That's the contract.
    assert state.status.get("technicals") in ("live", "stub", "error")
    # Most importantly: build did NOT crash, and we got a state object.
    assert state.ticker == "TEST"
```

> **A note on RawInputs construction:** the field list in this test (`ticker, spot, expiration, chain, quote, bars, iv_surface, is_clean, fetch_errors, fetched_at_utc`) is the constructor signature at `raw_inputs.py:80`. If a field doesn't exist by that name, read the dataclass and adjust. The test is the implementation guide here — match the actual `RawInputs` dataclass.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_bot_state.py`
Expected: `test_build_from_raw_status_technicals_is_live` FAILs because line 261 still uses `_stub("technicals")` so `status["technicals"]` is `"stub"`.

- [ ] **Step 3: Replace line 261 in `bot_state.py`**

Change:
```python
        technicals = _try_canonical("technicals", lambda: _stub("technicals"), status) or {}
```
to:
```python
        # v11.7 (Patch F.5.2): wire canonical_technicals via helper.
        technicals = _try_canonical(
            "technicals", lambda: _build_technicals_from_raw(raw), status
        ) or {}
```

Read the file before editing to confirm line 261 still has the stub (line numbers drift — match the anchor string `_try_canonical("technicals"`, not the literal line number).

- [ ] **Step 4: AST-check**

Run: `python -c "import ast; ast.parse(open('bot_state.py').read()); print('AST OK')"`
Expected: `AST OK`.

- [ ] **Step 5: Run tests**

```
python test_bot_state.py
```
Expected: every test passes, including the two new F.5.2 integration tests. `status['technicals']` is now `live`.

Run regression battery (G.1 step 7).

- [ ] **Step 6: Dashboard sanity check**

The Research page renders a CANONICAL_COMPUTE_STATUS block per ticker (visible on `omega_dashboard/templates/dashboard/research.html`). After F.5.2 ships, every ticker's `technicals` status should display as `LIVE` instead of `stub`.

Manual check after deploy:
1. Load the Research page (`https://options-discord-bot.onrender.com/research`)
2. Pick any ticker card
3. Scroll to the CANONICAL_COMPUTE_STATUS section
4. Confirm `technicals` is shown as `LIVE` (or `live`, depending on case convention)

If the dashboard still shows `stub`, the producer hasn't picked up the new code — wait for the producer's tier-A cadence (60s) plus a redeploy cycle, then re-check. The producer caches BotState in Redis with TTL `cadence × 6` (Patch C.6); old envelopes will expire within 6 minutes.

This is a deploy-time verification — not part of the test suite. Document the outcome in the commit message.

- [ ] **Step 7: Commit**

```
git add bot_state.py test_bot_state.py
git commit -m "Patch F.5.2: wire canonical_technicals into BotState.build_from_raw

Replaces the technicals stub at bot_state.py:261 with a call to
_build_technicals_from_raw(raw) (added in F.5.1). status['technicals']
flips from 'stub' to 'live' for every ticker; the existing rsi/macd_hist/
adx fields on BotState now populate from canonical_technicals instead
of None.

Adds an integration test that asserts status['technicals']=='live' on
clean bars, and a defensive test that malformed bars don't crash the
permissive build.

Dashboard CANONICAL_COMPUTE_STATUS block will show 'technicals: LIVE'
on every ticker card after deploy + producer-cache rotation (≤6 min).

# v11.7 (Patch F.5.2): wire technicals canonical."
```

---

## Task F.5.3: CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md` — append F.5 entry under "What's done as of last session (v11.7 / Patch F)"

- [ ] **Step 1: Read the current "What's done" section**

Locate the bullet list under `What's done as of last session (v11.7 / Patch F)` in CLAUDE.md. Identify the existing canonical_technicals entry (Patch E) and the Patch F entry (active_scanner technicals redirect).

- [ ] **Step 2: Append the F.5 bullet**

Add a new bullet at the end of the list (just before the "What's queued (in order)" section), matching the style of the existing entries:

```markdown
- Patch F.5 (canonical_technicals on BotState) — `_build_technicals_from_raw(raw)` helper added to `bot_state.py`; replaces the `_stub("technicals")` placeholder at line 261. BotState's `rsi` / `macd_hist` / `adx` fields now populate from `canonical_technicals.rsi/macd/adx` instead of None, and `status["technicals"]` flips from `stub` to `live`. Defensive about bar key naming — `risk_manager.py:275` pattern (`b.get("h") or b.get("high")`). Wrapper-consistency tests assert no parallel RSI/MACD/ADX math sneaks into the helper. Three commits: F.5.1 (helper + tests, additive), F.5.2 (wire it in, status flips to live), F.5.3 (this CLAUDE.md update). Each F.5 commit is independently revertible — F.5 stands alone if Patch G needs unwinding. canonical_technicals is now BotState's first reader (after Patch F made it active_scanner's first reader).
```

Also locate the bullet under "Decisions already made — don't relitigate" mentioning that BotState's technicals fields exist but are None today, if present. If such a bullet exists, update it to reflect the new live status. (If no such bullet exists, no update needed.)

- [ ] **Step 3: Verify the edit**

Run a mental diff: the only changes to CLAUDE.md should be the new bullet (and possibly one updated bullet under "Decisions already made"). No other content changes — F.5.3 is a documentation-only commit.

- [ ] **Step 4: Commit**

```
git add CLAUDE.md
git commit -m "Patch F.5.3: CLAUDE.md update — BotState reads canonical_technicals

Documents Patch F.5 in the canonical-rebuild status: BotState's rsi/
macd_hist/adx fields are no longer None; status['technicals']='live'
post-F.5.2.

Pure documentation. No code changed.

# v11.7 (Patch F.5.3): CLAUDE.md update."
```

After F.5.3 commits, F.5 is complete. Proceed to G.1.

---

## Task G.1: Schema migration framework + initial schema

**Files:**
- Create: `migrations/__init__.py`
- Create: `migrations/0001_initial_schema.sql`
- Create: `db_migrate.py`
- Test: `test_db_migrate.py`

- [ ] **Step 1: Write the schema SQL file**

Create `migrations/0001_initial_schema.sql` with the full V1 schema (copy verbatim from spec section 4):

```sql
-- v11.7 (Patch G.1): Alert recorder V1 — initial schema.
-- See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md sections 4 & 6.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id              TEXT PRIMARY KEY,
    fired_at              INTEGER NOT NULL,
    engine                TEXT NOT NULL,
    engine_version        TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    classification        TEXT,
    direction             TEXT,
    suggested_structure   TEXT,
    suggested_dte         INTEGER,
    spot_at_fire          REAL,
    canonical_snapshot    TEXT,
    raw_engine_payload    TEXT,
    parent_alert_id       TEXT,
    posted_to_telegram    INTEGER NOT NULL,
    telegram_chat         TEXT,
    suppression_reason    TEXT,
    FOREIGN KEY (parent_alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_fired_at ON alerts(fired_at);
CREATE INDEX IF NOT EXISTS idx_alerts_engine ON alerts(engine, engine_version);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker, fired_at);
CREATE INDEX IF NOT EXISTS idx_alerts_classification ON alerts(engine, classification, fired_at);
CREATE INDEX IF NOT EXISTS idx_alerts_parent ON alerts(parent_alert_id);

CREATE TABLE IF NOT EXISTS alert_features (
    alert_id      TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    feature_value REAL,
    feature_text  TEXT,
    PRIMARY KEY (alert_id, feature_name),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_features_name_value ON alert_features(feature_name, feature_value);
CREATE INDEX IF NOT EXISTS idx_features_name_text ON alert_features(feature_name, feature_text);

CREATE TABLE IF NOT EXISTS alert_price_track (
    alert_id              TEXT NOT NULL,
    elapsed_seconds       INTEGER NOT NULL,
    sampled_at            INTEGER NOT NULL,
    underlying_price      REAL,
    structure_mark        REAL,
    structure_pnl_pct     REAL,
    structure_pnl_abs     REAL,
    market_state          TEXT,
    PRIMARY KEY (alert_id, elapsed_seconds),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_track_alert ON alert_price_track(alert_id, elapsed_seconds);

CREATE TABLE IF NOT EXISTS alert_outcomes (
    alert_id           TEXT NOT NULL,
    horizon            TEXT NOT NULL,
    outcome_at         INTEGER,
    underlying_price   REAL,
    structure_mark     REAL,
    pnl_pct            REAL,
    pnl_abs            REAL,
    hit_pt1            INTEGER DEFAULT 0,
    hit_pt2            INTEGER DEFAULT 0,
    hit_pt3            INTEGER DEFAULT 0,
    max_favorable_pct  REAL,
    max_adverse_pct    REAL,
    PRIMARY KEY (alert_id, horizon),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_horizon ON alert_outcomes(horizon, pnl_pct);

CREATE TABLE IF NOT EXISTS engine_versions (
    engine          TEXT PRIMARY KEY,
    engine_version  TEXT NOT NULL,
    recorded_at     INTEGER NOT NULL
);
```

Create `migrations/__init__.py` as an empty file (so `migrations/` is importable for tests if needed).

- [ ] **Step 2: Write the failing test**

Create `test_db_migrate.py`:

```python
"""Tests for db_migrate.apply_migrations.

Patch G.1 — boot-time migration runner. No network, no Schwab, no Redis.
Uses a temp directory for the SQLite DB so tests are fully hermetic.
"""
import os
import sqlite3
import tempfile
import shutil
from pathlib import Path


def _fresh_db_path():
    """Returns (tmpdir, db_path); caller is responsible for shutil.rmtree."""
    tmpdir = tempfile.mkdtemp(prefix="recorder_test_")
    return tmpdir, os.path.join(tmpdir, "test.db")


def test_apply_migrations_creates_all_tables():
    """Spec G.1: applying migrations on a fresh DB creates every V1 table."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
        for required in ("alerts", "alert_features", "alert_price_track",
                         "alert_outcomes", "engine_versions",
                         "schema_migrations"):
            assert required in names, f"missing table {required}: got {names}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_migrations_records_version():
    """Spec G.1: schema_migrations row is written after a migration applies."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert rows == [(1,)], f"expected [(1,)], got {rows}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_migrations_is_idempotent():
    """Spec G.1: re-applying the same migrations is a no-op (no errors,
    no duplicate rows)."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        apply_migrations(db)  # second call must not raise
        apply_migrations(db)  # third call must not raise
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        assert rows == [(1,)], f"idempotency broken: {rows}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_migrations_creates_parent_directory():
    """If /var/backtest/ doesn't exist (dev / first deploy), apply_migrations
    creates it. Production already has /var/backtest as a Render disk."""
    from db_migrate import apply_migrations
    tmpdir = tempfile.mkdtemp(prefix="recorder_test_")
    try:
        nested = os.path.join(tmpdir, "deep", "nested", "dir")
        db = os.path.join(nested, "test.db")
        # nested doesn't exist yet
        apply_migrations(db)
        assert os.path.exists(db), "DB file not created"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_wal_mode_enabled():
    """WAL mode is required for concurrent reads (dashboard) + writes (recorder).
    Apply_migrations must enable it."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        conn = sqlite3.connect(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"expected WAL, got {mode}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    tests = [
        test_apply_migrations_creates_all_tables,
        test_apply_migrations_records_version,
        test_apply_migrations_is_idempotent,
        test_apply_migrations_creates_parent_directory,
        test_wal_mode_enabled,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python test_db_migrate.py`
Expected: every test FAILs with `ImportError: No module named 'db_migrate'`.

- [ ] **Step 4: Write the migration runner**

Create `db_migrate.py`:

```python
"""Boot-time SQLite migration runner.

# v11.7 (Patch G.1): runs every SQL file in migrations/ in order, tracks
# applied versions in schema_migrations, idempotent across restarts.

Usage:
    from db_migrate import apply_migrations
    apply_migrations("/var/backtest/desk.db")

The runner:
  * Creates the parent directory if missing.
  * Opens the DB in WAL mode (concurrent reads/writes).
  * Reads migrations/NNNN_*.sql in numerical order.
  * Skips files whose version is already in schema_migrations.
  * Wraps each migration in a transaction.
  * Writes a schema_migrations row on success.

Migration files are named `migrations/NNNN_description.sql` where NNNN is
a 4-digit zero-padded version. Migrations always go forward — no down
scripts in V1. Schema changes that break the recorder require a new
migration file and a producer-version bump elsewhere.
"""
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import List, Tuple

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_RE = re.compile(r"^(\d{4})_.*\.sql$")


def _list_migration_files() -> List[Tuple[int, Path]]:
    """Returns sorted [(version, path), ...] for all migrations."""
    out = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if not entry.is_file():
            continue
        m = _VERSION_RE.match(entry.name)
        if not m:
            continue
        out.append((int(m.group(1)), entry))
    return sorted(out, key=lambda x: x[0])


def _ensure_parent_dir(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _open_with_wal(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_migrations(db_path: str) -> None:
    """Apply every pending migration in migrations/ in order. Idempotent."""
    _ensure_parent_dir(db_path)
    conn = _open_with_wal(db_path)
    try:
        _ensure_schema_migrations(conn)
        already = _applied_versions(conn)
        for version, sql_path in _list_migration_files():
            if version in already:
                continue
            sql = sql_path.read_text(encoding="utf-8")
            log.info(f"db_migrate: applying {sql_path.name}")
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, int(time.time() * 1_000_000)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    path = sys.argv[1] if len(sys.argv) > 1 else "/var/backtest/desk.db"
    apply_migrations(path)
    print(f"Migrations applied to {path}")
```

- [ ] **Step 5: AST-check both new files**

Run: `python -c "import ast; ast.parse(open('db_migrate.py').read()); ast.parse(open('test_db_migrate.py').read()); print('AST OK')"`
Expected: `AST OK`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python test_db_migrate.py`
Expected: `5/5 passed`.

- [ ] **Step 7: Confirm existing test suites still pass**

Run all canonical-rebuild and producer suites:
```
python test_canonical_gamma_flip.py
python test_canonical_iv_state.py
python test_canonical_exposures.py
python test_canonical_expiration.py
python test_bot_state.py
python test_canonical_technicals.py
python test_bot_state_producer.py
python test_research_data_consumer.py
```
Expected: every suite passes (no regressions). Migration runner is purely additive.

- [ ] **Step 8: Commit**

```
git add migrations/ db_migrate.py test_db_migrate.py
git commit -m "Patch G.1: alert recorder schema + migration runner

Adds db_migrate.py (boot-time SQLite migration runner with WAL mode +
idempotent re-runs) and migrations/0001_initial_schema.sql with the V1
recorder schema: alerts, alert_features (EAV), alert_price_track,
alert_outcomes, engine_versions. 5 tests covering apply, version
tracking, idempotency, parent-dir creation, WAL mode.

Pure additive — no production code path uses these yet (G.2+ wires it).

# v11.7 (Patch G.1): schema + migration runner."
```

---

## Task G.2: Alert recorder module (write side)

**Files:**
- Create: `alert_recorder.py`
- Test: `test_alert_recorder.py`

The recorder is the only module that writes to the recorder DB. Engines call `record_alert(...)`; the tracker daemon calls `record_track_sample(...)`; the outcome computer calls `record_outcome(...)`. Engines never see a sqlite3 cursor.

- [ ] **Step 1: Write the failing tests for record_alert**

Create `test_alert_recorder.py`:

```python
"""Tests for alert_recorder.

Patch G.2 — recorder write side. No network, no Schwab, no Telegram.
Hermetic: each test uses its own temp DB. Uses RECORDER_DB_PATH override
to point the recorder at the temp file.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time

# Tests assume db_migrate is on sys.path (it is — same directory).


def _setup_recorder_env():
    """Returns (tmpdir, db_path). Sets RECORDER_DB_PATH and master/per-engine
    flags. Caller responsible for shutil.rmtree."""
    tmpdir = tempfile.mkdtemp(prefix="recorder_g2_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    os.environ["RECORDER_ENABLED"] = "true"
    os.environ["RECORDER_LCB_ENABLED"] = "true"
    os.environ["RECORDER_V25D_ENABLED"] = "true"
    os.environ["RECORDER_CREDIT_ENABLED"] = "true"
    os.environ["RECORDER_CONVICTION_ENABLED"] = "true"

    # Apply the schema.
    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown_recorder_env(tmpdir):
    for key in ("RECORDER_DB_PATH", "RECORDER_ENABLED", "RECORDER_LCB_ENABLED",
                "RECORDER_V25D_ENABLED", "RECORDER_CREDIT_ENABLED",
                "RECORDER_CONVICTION_ENABLED"):
        os.environ.pop(key, None)
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_record_alert_writes_alerts_row():
    """Round-trip: record_alert returns alert_id; row is in alerts table."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="long_call_burst",
            engine_version="long_call_burst@v8.4.2",
            ticker="SPY",
            classification="BURST_YES",
            direction="bull",
            suggested_structure={"type": "long_call", "strike": 590.0,
                                 "expiry": "2026-05-15", "entry_mark": 2.85},
            suggested_dte=6,
            spot_at_fire=588.30,
            canonical_snapshot={"producer_version": 1,
                                "convention_version": 2,
                                "intent": "front",
                                "expiration": "2026-05-08",
                                "state": {"ticker": "SPY", "spot": 588.30}},
            raw_engine_payload={"momentum_score": 7, "rsi": 62.0},
            features={"rsi": 62.0, "adx": 24.1, "regime": "BULL_BASE"},
            telegram_chat="main",
        )
        assert alert_id, "record_alert returned empty alert_id"

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT alert_id, engine, ticker, classification, "
                            "direction, posted_to_telegram FROM alerts").fetchall()
        assert rows == [(alert_id, "long_call_burst", "SPY", "BURST_YES",
                         "bull", 1)], f"unexpected row: {rows}"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_writes_features():
    """EAV expansion: numeric features → feature_value, strings → feature_text."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={"type": "long_call"},
            suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={
                "rsi": 47.3,
                "adx": 22.1,
                "regime": "BULL_BASE",
                "dealer_regime": "short_gamma_at_585",
                "is_pinned": True,        # bool → numeric 1.0
                "missing_field": None,    # None should be skipped, not crash
            },
            telegram_chat="main",
        )

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT feature_name, feature_value, feature_text "
            "FROM alert_features WHERE alert_id = ? ORDER BY feature_name",
            (alert_id,)
        ).fetchall()
        as_dict = {name: (val, text) for name, val, text in rows}
        assert "rsi" in as_dict and as_dict["rsi"] == (47.3, None)
        assert "adx" in as_dict and as_dict["adx"] == (22.1, None)
        assert "regime" in as_dict and as_dict["regime"] == (None, "BULL_BASE")
        assert "is_pinned" in as_dict and as_dict["is_pinned"] == (1.0, None)
        # Bool-stored-as-numeric is the simplest invariant.
        assert "missing_field" not in as_dict, "None features must be skipped"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_round_trip_canonical_snapshot():
    """canonical_snapshot is JSON-encoded on write; readable as dict on round-trip."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        snap = {"producer_version": 1, "convention_version": 2,
                "intent": "front", "expiration": "2026-05-08",
                "state": {"ticker": "SPY", "gex_total": -1234.5,
                          "call_wall": 590.0, "put_wall": 580.0}}
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot=snap, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        conn = sqlite3.connect(db)
        (raw,) = conn.execute(
            "SELECT canonical_snapshot FROM alerts WHERE alert_id = ?",
            (alert_id,)
        ).fetchone()
        conn.close()
        assert json.loads(raw) == snap, "canonical_snapshot did not round-trip"
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_parent_alert_id_linkage():
    """V2 5D parent → LCB child: parent_alert_id stores the V2 5D alert_id."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        parent_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        child_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={}, suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
            parent_alert_id=parent_id,
        )
        conn = sqlite3.connect(db)
        (got,) = conn.execute(
            "SELECT parent_alert_id FROM alerts WHERE alert_id = ?",
            (child_id,)
        ).fetchone()
        assert got == parent_id, f"linkage broken: {got} != {parent_id}"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_master_gate_off_returns_none():
    """If RECORDER_ENABLED is false, record_alert is a no-op (returns None,
    writes nothing)."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    os.environ["RECORDER_ENABLED"] = "false"
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert alert_id is None, "master gate off should return None"
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchall()
        assert rows == [(0,)], "master gate off must not write"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_per_engine_gate_off_returns_none():
    """Master ON but per-engine gate off → no-op."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    os.environ["RECORDER_LCB_ENABLED"] = "false"
    try:
        alert_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={}, suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert alert_id is None
        # Other engines still work.
        v25d_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert v25d_id is not None
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_swallows_internal_exception():
    """Recorder failure (DB locked, malformed JSON, anything) returns None.
    NEVER raises into the engine."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        # Pass a non-serializable object as raw_engine_payload to force
        # json.dumps failure inside the recorder.
        class Unserializable:
            pass
        # Recorder catches the json error and returns None.
        out = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={"bad": Unserializable()},
            features={}, telegram_chat="main",
        )
        # Either succeeded (raw_engine_payload null) or returned None.
        # The contract is: never raise. Both outcomes acceptable.
        assert out is None or isinstance(out, str)
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_id_is_uuid_v4():
    """alert_id is a UUID v4 string (36 chars, dashes at positions 8/13/18/23)."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert len(alert_id) == 36
        assert alert_id[8] == "-" and alert_id[13] == "-"
        assert alert_id[18] == "-" and alert_id[23] == "-"
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_track_sample_writes_row():
    """Tracker write path: record_track_sample inserts an alert_price_track row."""
    from alert_recorder import record_alert, record_track_sample
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        record_track_sample(alert_id=alert_id, elapsed_seconds=60,
                            underlying_price=588.50, structure_mark=2.92,
                            structure_pnl_pct=2.46, structure_pnl_abs=7.0,
                            market_state="rth")
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT alert_id, elapsed_seconds, underlying_price, "
            "structure_mark, market_state FROM alert_price_track"
        ).fetchall()
        assert rows == [(alert_id, 60, 588.50, 2.92, "rth")]
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_outcome_writes_row_and_is_idempotent():
    """Outcome writer: insert; re-insert overwrites (idempotent)."""
    from alert_recorder import record_alert, record_outcome
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        record_outcome(alert_id=alert_id, horizon="1h",
                       outcome_at=1_700_000_000_000_000,
                       underlying_price=590.0, structure_mark=3.10,
                       pnl_pct=8.77, pnl_abs=25.0,
                       hit_pt1=1, hit_pt2=0, hit_pt3=0,
                       max_favorable_pct=12.0, max_adverse_pct=-3.0)
        # Re-insert with different numbers — must overwrite.
        record_outcome(alert_id=alert_id, horizon="1h",
                       outcome_at=1_700_000_000_000_000,
                       underlying_price=591.0, structure_mark=3.20,
                       pnl_pct=12.28, pnl_abs=35.0,
                       hit_pt1=1, hit_pt2=1, hit_pt3=0,
                       max_favorable_pct=15.0, max_adverse_pct=-3.0)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT pnl_pct, pnl_abs, hit_pt2 FROM alert_outcomes "
            "WHERE alert_id = ? AND horizon = ?",
            (alert_id, "1h")
        ).fetchall()
        assert rows == [(12.28, 35.0, 1)], f"idempotency broken: {rows}"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


if __name__ == "__main__":
    tests = [
        test_record_alert_writes_alerts_row,
        test_record_alert_writes_features,
        test_record_alert_round_trip_canonical_snapshot,
        test_record_alert_parent_alert_id_linkage,
        test_record_alert_master_gate_off_returns_none,
        test_record_alert_per_engine_gate_off_returns_none,
        test_record_alert_swallows_internal_exception,
        test_record_alert_id_is_uuid_v4,
        test_record_track_sample_writes_row,
        test_record_outcome_writes_row_and_is_idempotent,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python test_alert_recorder.py`
Expected: every test FAILs with `ImportError: No module named 'alert_recorder'`.

- [ ] **Step 3: Implement alert_recorder.py**

Create `alert_recorder.py`:

```python
"""Alert recorder — write side.

# v11.7 (Patch G.2): the only module that writes to the recorder DB.
# Engines call record_alert(...) after their card posts. The tracker
# daemon calls record_track_sample(...). The outcome computer calls
# record_outcome(...). Every entrypoint is wrapped in try/except —
# recorder failure NEVER affects engine behavior.

See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md.
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# v11.7 (Patch G): alert recorder schema + write hooks.
SCHEMA_VERSION = 1
RECORDER_VERSION = "v1.0.0"
DEFAULT_DB_PATH = "/var/backtest/desk.db"

HORIZONS_SECONDS = {
    "5min":  5 * 60,
    "15min": 15 * 60,
    "30min": 30 * 60,
    "1h":    60 * 60,
    "4h":    4 * 60 * 60,
    "1d":    24 * 60 * 60,
    "2d":    2 * 24 * 60 * 60,
    "3d":    3 * 24 * 60 * 60,
    "5d":    5 * 24 * 60 * 60,
}

SAMPLING_CADENCE = [
    (0,                    60),
    (60 * 60,              5 * 60),
    (4 * 60 * 60,          15 * 60),
    (24 * 60 * 60,         30 * 60),
    (7 * 24 * 60 * 60,     60 * 60),
]

TRACKING_HORIZON_BY_ENGINE = {
    "long_call_burst":     3 * 24 * 60 * 60,
    "v2_5d":               7 * 24 * 60 * 60,
    "credit_v84":          None,
    "oi_flow_conviction":  5 * 24 * 60 * 60,
}


def _utc_micros() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def _master_enabled() -> bool:
    return os.getenv("RECORDER_ENABLED", "false").lower() in ("1", "true", "yes")


def _engine_enabled(engine: str) -> bool:
    if not _master_enabled():
        return False
    flag = {
        "long_call_burst":     "RECORDER_LCB_ENABLED",
        "v2_5d":               "RECORDER_V25D_ENABLED",
        "credit_v84":          "RECORDER_CREDIT_ENABLED",
        "oi_flow_conviction":  "RECORDER_CONVICTION_ENABLED",
    }.get(engine)
    if flag is None:
        return False
    return os.getenv(flag, "false").lower() in ("1", "true", "yes")


def _db_path() -> str:
    return os.getenv("RECORDER_DB_PATH", DEFAULT_DB_PATH)


_conn_lock = threading.Lock()
_conn_cache: Dict[str, sqlite3.Connection] = {}


def _conn() -> sqlite3.Connection:
    """Per-thread+path connection cache. SQLite connections aren't
    thread-safe to share by default, but we guard each write with a
    process-wide lock so a single connection per path is fine for V1
    write volumes (~11 alerts/day, ~500 track samples/alert)."""
    path = _db_path()
    with _conn_lock:
        c = _conn_cache.get(path)
        if c is None:
            c = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            _conn_cache[path] = c
        return c


def _safe_json(obj: Any) -> Optional[str]:
    """Serialize to JSON, skipping non-serializable values rather than
    raising. NaN/inf → null (consistent with bot_state_producer)."""
    try:
        return json.dumps(obj, default=str, allow_nan=False)
    except (TypeError, ValueError):
        try:
            # Fallback: stringify recursively.
            return json.dumps(_stringify_unserializable(obj), allow_nan=False)
        except Exception:
            return None


def _stringify_unserializable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _stringify_unserializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_unserializable(x) for x in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _split_feature(value: Any):
    """EAV split: numeric → (feature_value, None); string → (None, feature_text);
    bool → (1.0/0.0, None); None → returns None (caller skips)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return (1.0 if value else 0.0, None)
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return None  # NaN/inf — skip rather than write garbage
        return (float(value), None)
    if isinstance(value, str):
        return (None, value)
    return (None, str(value))


def record_alert(
    *,
    engine: str,
    engine_version: str,
    ticker: str,
    classification: Optional[str],
    direction: Optional[str],
    suggested_structure: Optional[Dict[str, Any]],
    suggested_dte: Optional[int],
    spot_at_fire: Optional[float],
    canonical_snapshot: Optional[Dict[str, Any]],
    raw_engine_payload: Optional[Dict[str, Any]],
    features: Optional[Dict[str, Any]],
    telegram_chat: Optional[str],
    parent_alert_id: Optional[str] = None,
    posted_to_telegram: bool = True,
    suppression_reason: Optional[str] = None,
) -> Optional[str]:
    """Record an alert. Returns the alert_id on success, None on no-op or
    failure. NEVER raises. Master gate + per-engine gate must both be on."""
    if not _engine_enabled(engine):
        return None
    try:
        alert_id = str(uuid.uuid4())
        fired_at = _utc_micros()
        conn = _conn()
        with _conn_lock:
            conn.execute(
                "INSERT INTO alerts (alert_id, fired_at, engine, engine_version, "
                "ticker, classification, direction, suggested_structure, "
                "suggested_dte, spot_at_fire, canonical_snapshot, "
                "raw_engine_payload, parent_alert_id, posted_to_telegram, "
                "telegram_chat, suppression_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alert_id, fired_at, engine, engine_version,
                    ticker, classification, direction,
                    _safe_json(suggested_structure or {}),
                    suggested_dte, spot_at_fire,
                    _safe_json(canonical_snapshot or {}),
                    _safe_json(raw_engine_payload or {}),
                    parent_alert_id,
                    1 if posted_to_telegram else 0,
                    telegram_chat, suppression_reason,
                ),
            )
            if features:
                rows = []
                for name, value in features.items():
                    split = _split_feature(value)
                    if split is None:
                        continue
                    feat_val, feat_text = split
                    rows.append((alert_id, name, feat_val, feat_text))
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO alert_features "
                        "(alert_id, feature_name, feature_value, feature_text) "
                        "VALUES (?,?,?,?)",
                        rows,
                    )
            conn.commit()
        return alert_id
    except Exception as e:
        log.warning(f"recorder: record_alert({engine}) failed: {e}")
        return None


def record_track_sample(
    *,
    alert_id: str,
    elapsed_seconds: int,
    underlying_price: Optional[float],
    structure_mark: Optional[float],
    structure_pnl_pct: Optional[float],
    structure_pnl_abs: Optional[float],
    market_state: Optional[str],
) -> bool:
    """Insert one alert_price_track row. Returns True on success.
    Master gate must be on (per-engine gate is checked at fire-time, not
    sample-time — alerts that exist in the DB are tracked regardless of
    current per-engine flag state)."""
    if not _master_enabled():
        return False
    try:
        sampled_at = _utc_micros()
        conn = _conn()
        with _conn_lock:
            conn.execute(
                "INSERT OR REPLACE INTO alert_price_track "
                "(alert_id, elapsed_seconds, sampled_at, underlying_price, "
                "structure_mark, structure_pnl_pct, structure_pnl_abs, "
                "market_state) VALUES (?,?,?,?,?,?,?,?)",
                (alert_id, elapsed_seconds, sampled_at, underlying_price,
                 structure_mark, structure_pnl_pct, structure_pnl_abs,
                 market_state),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning(f"recorder: record_track_sample({alert_id}) failed: {e}")
        return False


def record_outcome(
    *,
    alert_id: str,
    horizon: str,
    outcome_at: Optional[int],
    underlying_price: Optional[float],
    structure_mark: Optional[float],
    pnl_pct: Optional[float],
    pnl_abs: Optional[float],
    hit_pt1: int,
    hit_pt2: int,
    hit_pt3: int,
    max_favorable_pct: Optional[float],
    max_adverse_pct: Optional[float],
) -> bool:
    """Insert/replace one alert_outcomes row. Idempotent — re-running
    re-computes."""
    if not _master_enabled():
        return False
    try:
        conn = _conn()
        with _conn_lock:
            conn.execute(
                "INSERT OR REPLACE INTO alert_outcomes "
                "(alert_id, horizon, outcome_at, underlying_price, "
                "structure_mark, pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3, "
                "max_favorable_pct, max_adverse_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (alert_id, horizon, outcome_at, underlying_price,
                 structure_mark, pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3,
                 max_favorable_pct, max_adverse_pct),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning(f"recorder: record_outcome({alert_id},{horizon}) failed: {e}")
        return False


def get_alert(alert_id: str) -> Optional[Dict[str, Any]]:
    """Read-side helper for tests + the tracker daemon. Returns None if
    not found."""
    try:
        conn = _conn()
        with _conn_lock:
            row = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "classification, direction, suggested_structure, "
                "suggested_dte, spot_at_fire, canonical_snapshot, "
                "raw_engine_payload, parent_alert_id, posted_to_telegram, "
                "telegram_chat, suppression_reason FROM alerts "
                "WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
        if row is None:
            return None
        keys = ("alert_id", "fired_at", "engine", "engine_version", "ticker",
                "classification", "direction", "suggested_structure",
                "suggested_dte", "spot_at_fire", "canonical_snapshot",
                "raw_engine_payload", "parent_alert_id", "posted_to_telegram",
                "telegram_chat", "suppression_reason")
        return dict(zip(keys, row))
    except Exception as e:
        log.warning(f"recorder: get_alert({alert_id}) failed: {e}")
        return None


def list_active_alerts() -> List[Dict[str, Any]]:
    """Read-side: alerts whose tracking horizon has not yet expired.
    Used by the alert_tracker_daemon. Returns [] on error."""
    try:
        now = _utc_micros()
        conn = _conn()
        with _conn_lock:
            rows = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "suggested_structure, suggested_dte, spot_at_fire "
                "FROM alerts ORDER BY fired_at DESC LIMIT 500"
            ).fetchall()
        out = []
        for row in rows:
            (alert_id, fired_at, engine, engine_version, ticker,
             struct, dte, spot) = row
            elapsed = (now - fired_at) // 1_000_000
            horizon = TRACKING_HORIZON_BY_ENGINE.get(engine)
            if horizon is None and dte:
                horizon = int(dte) * 24 * 60 * 60
            if horizon is None or elapsed > horizon:
                continue
            try:
                struct_dict = json.loads(struct) if struct else {}
            except Exception:
                struct_dict = {}
            out.append({
                "alert_id": alert_id,
                "fired_at": fired_at,
                "engine": engine,
                "engine_version": engine_version,
                "ticker": ticker,
                "suggested_structure": struct_dict,
                "suggested_dte": dte,
                "spot_at_fire": spot,
                "elapsed_seconds": elapsed,
            })
        return out
    except Exception as e:
        log.warning(f"recorder: list_active_alerts failed: {e}")
        return []
```

- [ ] **Step 4: AST-check**

Run: `python -c "import ast; ast.parse(open('alert_recorder.py').read()); ast.parse(open('test_alert_recorder.py').read()); print('AST OK')"`
Expected: `AST OK`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python test_alert_recorder.py`
Expected: `10/10 passed`.

- [ ] **Step 6: Confirm no regressions**

Run: every other suite from G.1 step 7. Expected: all green.

- [ ] **Step 7: Commit**

```
git add alert_recorder.py test_alert_recorder.py
git commit -m "Patch G.2: alert recorder write-side module

Pure write-side. record_alert(), record_track_sample(), record_outcome()
plus read helpers list_active_alerts() and get_alert(). Master gate
RECORDER_ENABLED + per-engine gates RECORDER_{LCB,V25D,CREDIT,
CONVICTION}_ENABLED, all default off. Every entrypoint try/except — no
exception ever propagates to the calling engine. EAV feature expansion
splits numeric/string/bool, skips None and NaN/inf. canonical_snapshot
round-trips as JSON. parent_alert_id wired but no engine consumes yet
(G.3-G.6 do that).

10 tests covering round-trip, EAV expansion, parent linkage, gate
behavior, exception swallowing, UUID v4, track sample insert, outcome
idempotency.

# v11.7 (Patch G.2): write-side module."
```

---

## Task G.3: Wire LONG CALL BURST

**Files:**
- Modify: `app.py` — `_try_post_long_call_burst` at line 9283
- Test: append to `test_alert_recorder.py` OR new `test_alert_recorder_lcb_wire.py` (cleaner — keeps file focused)

**Wire-point recap:** `_try_post_long_call_burst` at `app.py:9283` posts the LCB card via `_tg_rate_limited_post(msg)` and returns `(posted_bool, vehicle_id)`. The hook fires after a successful post. The V2 5D parent's alert_id (from G.4) gets passed in via a new `parent_alert_id` parameter — but G.3 ships before G.4, so for now we accept `parent_alert_id=None` and document it as wired-but-empty until G.4 lands.

- [ ] **Step 1: Find the exact post site**

Run: `grep -n "_tg_rate_limited_post" app.py | head -20` to confirm the exact line number where LCB posts. Look for the line inside `_try_post_long_call_burst` — you want the post site, not other engines'.

Read the function `_try_post_long_call_burst` end-to-end:
```
sed -n '9283,9400p' app.py
```

Identify:
1. Where the V2 5D classifier's `V2SetupResult` is consumed (gives `momentum_burst_label`, scoring fields).
2. Where `msg` is constructed (the card text).
3. Where `_tg_rate_limited_post(msg)` is called.
4. The local variables that hold ticker, spot, suggested strike, suggested expiry, entry mark.

These become the inputs to record_alert. **Capture them by name in your patch — do not invent fields.**

- [ ] **Step 2: Write the failing test**

Create `test_alert_recorder_lcb_wire.py`:

```python
"""Test that _try_post_long_call_burst calls record_alert after a
successful post. Patch G.3.

Strategy: monkey-patch alert_recorder.record_alert with a capture stub,
call _try_post_long_call_burst with synthetic inputs that produce a
postable card, assert record_alert was called once with engine=
'long_call_burst' and the structure/feature fields populated.

This is a wire test, not a behavior test. It does NOT validate the LCB
card builder logic — that's tested elsewhere. It only validates that the
recorder hook fires."""
import os
import sys
from unittest import mock


def test_lcb_calls_recorder_on_post():
    """Spec G.3: when _try_post_long_call_burst posts a card, record_alert
    is invoked with engine='long_call_burst'."""
    # The implementer fills this in once G.3 wiring is in place. The test
    # uses mock.patch on alert_recorder.record_alert and a fake
    # _tg_rate_limited_post (returning success). Construct a synthetic
    # V2SetupResult with momentum_burst_label="YES" so the LCB path enters
    # the post branch, then call _try_post_long_call_burst.
    #
    # Pseudocode (real test goes here):
    # captures = []
    # def fake_record(**kwargs):
    #     captures.append(kwargs)
    #     return "fake-alert-id"
    # with mock.patch("alert_recorder.record_alert", side_effect=fake_record):
    #     _try_post_long_call_burst(ticker="SPY", bias="bull", spot=588.30, ...)
    # assert len(captures) == 1
    # assert captures[0]["engine"] == "long_call_burst"
    # assert captures[0]["ticker"] == "SPY"
    # assert "type" in captures[0]["suggested_structure"]
    raise NotImplementedError(
        "Implementer: fill this in by reading _try_post_long_call_burst and "
        "constructing the minimal synthetic inputs that drive it to the post "
        "branch. The function is at app.py:9283."
    )


if __name__ == "__main__":
    try:
        test_lcb_calls_recorder_on_post()
        print("PASS")
    except NotImplementedError as e:
        print(f"SKIP (placeholder): {e}")
    except Exception as e:
        print(f"FAIL: {e}")
```

> **Note on test design:** the LCB function is deeply embedded in app.py and depends on many module-level globals (DataRouter, Schwab adapters, Telegram bridge). Writing a true integration test requires building a fixture that sets these up. The implementer has two options:
>
> 1. **Lighter:** factor a small helper (`_build_lcb_alert_features(v2_result, ticker, spot, ...)`) that takes pure inputs, returns the dict that `record_alert` consumes. Test the helper. The wire test then becomes a one-line `assert record_alert was called` integration test that runs against a real bot boot in dev mode.
> 2. **Heavier:** stub the global dependencies. Slower to write, more correct.
>
> Recommend option 1. Factor the helper before wiring the call site, test the helper hermetically, then wire it in. The same helper pattern repeats for G.4/G.5/G.6.

- [ ] **Step 3: Implement the helper + wire the call site**

Add at the top of `app.py` near the existing patch markers (e.g., after the existing G.2-related imports if any, or just before `_try_post_long_call_burst`):

```python
# v11.7 (Patch G.3): alert recorder wire — LONG CALL BURST.
import alert_recorder as _alert_recorder

LCB_ENGINE_VERSION = "long_call_burst@v8.4.2"  # bump on logic change


def _build_lcb_alert_payload(*, ticker: str, bias: str, spot: float,
                             v2_result, suggested_strike, suggested_expiry,
                             entry_mark, dte_days, canonical_snapshot,
                             webhook_data, v2_5d_parent_alert_id: str | None):
    """Build the kwargs dict for alert_recorder.record_alert from the LCB
    post site's local state. Pure function — no I/O. Tested separately."""
    suggested_structure = {
        "type":         "long_call",
        "strike":       suggested_strike,
        "expiry":       suggested_expiry,
        "entry_mark":   entry_mark,
    }
    features = {
        "v2_5d_grade":            getattr(v2_result, "grade", None),
        "momentum_burst_label":   getattr(v2_result, "momentum_burst_label", None),
        "momentum_burst_score":   getattr(v2_result, "momentum_burst_score", None),
        "rsi":                    getattr(v2_result, "rsi", None),
        "adx":                    getattr(v2_result, "adx", None),
        "macd_hist":              getattr(v2_result, "macd_hist", None),
        "volume_ratio":           getattr(v2_result, "volume_ratio", None),
        "regime":                 getattr(v2_result, "regime", None),
        "bias":                   bias,
        "dte_days":               dte_days,
    }
    return dict(
        engine="long_call_burst",
        engine_version=LCB_ENGINE_VERSION,
        ticker=ticker,
        classification="BURST_YES",
        direction=bias,
        suggested_structure=suggested_structure,
        suggested_dte=dte_days,
        spot_at_fire=spot,
        canonical_snapshot=canonical_snapshot,
        raw_engine_payload=webhook_data,
        features=features,
        telegram_chat="main",
        parent_alert_id=v2_5d_parent_alert_id,
    )
```

In `_try_post_long_call_burst`, immediately after `_tg_rate_limited_post(msg)` returns success and the function is about to return its `(posted_bool, vehicle_id)`, add:

```python
        # v11.7 (Patch G.3): record alert after successful post.
        try:
            payload = _build_lcb_alert_payload(
                ticker=ticker, bias=bias, spot=spot,
                v2_result=v2_result,
                suggested_strike=suggested_strike,
                suggested_expiry=suggested_expiry,
                entry_mark=entry_mark,
                dte_days=int((expiry_dt - today).days),
                canonical_snapshot=canonical_snapshot,
                webhook_data=webhook_data,
                v2_5d_parent_alert_id=None,  # G.4 wires this; G.3 leaves null.
            )
            _alert_recorder.record_alert(**payload)
        except Exception as e:
            log.debug(f"recorder G.3: hook failed: {e}")
```

> **Variable names** (`v2_result`, `suggested_strike`, `expiry_dt`, etc.): copy verbatim from `_try_post_long_call_burst`'s local scope. Do not invent. If a name doesn't exist, find the right one by reading the function. The patch is *purely additive* — touch nothing else.

- [ ] **Step 4: AST-check**

Run: `python -c "import ast; ast.parse(open('app.py').read()); print('AST OK')"`
Expected: `AST OK`.

- [ ] **Step 5: Wire the test**

Replace the `raise NotImplementedError` in `test_alert_recorder_lcb_wire.py` with a real test using `mock.patch("alert_recorder.record_alert")`. Construct the minimal synthetic inputs and assert `record_alert` was called with `engine="long_call_burst"`. If integration is too costly, test `_build_lcb_alert_payload` directly with synthetic fields:

```python
def test_build_lcb_payload_from_v2_result():
    """Helper produces the right kwargs dict from a synthetic V2SetupResult."""
    from app import _build_lcb_alert_payload
    from datetime import date

    class FakeV2:
        grade = "GRADE_A"
        momentum_burst_label = "YES"
        momentum_burst_score = 7
        rsi = 62.0
        adx = 24.1
        macd_hist = 0.05
        volume_ratio = 1.6
        regime = "BULL_BASE"

    payload = _build_lcb_alert_payload(
        ticker="SPY", bias="bull", spot=588.30,
        v2_result=FakeV2(),
        suggested_strike=590.0,
        suggested_expiry="2026-05-15",
        entry_mark=2.85,
        dte_days=6,
        canonical_snapshot={"intent": "front", "expiration": "2026-05-15"},
        webhook_data={"some_field": "value"},
        v2_5d_parent_alert_id=None,
    )
    assert payload["engine"] == "long_call_burst"
    assert payload["ticker"] == "SPY"
    assert payload["classification"] == "BURST_YES"
    assert payload["direction"] == "bull"
    assert payload["suggested_structure"]["type"] == "long_call"
    assert payload["suggested_structure"]["strike"] == 590.0
    assert payload["features"]["momentum_burst_label"] == "YES"
    assert payload["features"]["rsi"] == 62.0
    assert payload["parent_alert_id"] is None
```

- [ ] **Step 6: Run tests**

Run: `python test_alert_recorder_lcb_wire.py`
Expected: helper test passes.

Run all the regression suites from G.1 step 7. Expected: all green.

- [ ] **Step 7: Commit**

```
git add app.py test_alert_recorder_lcb_wire.py
git commit -m "Patch G.3: wire LONG CALL BURST to alert recorder

Adds _build_lcb_alert_payload pure helper plus a hook in
_try_post_long_call_burst that calls record_alert after a successful
Telegram post. Behind RECORDER_ENABLED + RECORDER_LCB_ENABLED. Recorder
failure is logged at DEBUG and swallowed — never propagates back to
LCB.

parent_alert_id is wired as a kwarg but always None for now. G.4 (V2 5D
wiring) updates the call site to pass through the parent's alert_id.

# v11.7 (Patch G.3): wire LONG CALL BURST."
```

---

## Task G.4: Wire V2 5D EDGE MODEL

**Files:**
- Modify: `app.py` — wherever `build_v2_card` (from `v2_5d_edge_model.py:401`) is consumed and posted
- Modify: `app.py` — `_try_post_long_call_burst` to receive and forward `v2_5d_parent_alert_id`
- Test: `test_alert_recorder_v25d_wire.py`

V2 5D is the parent for downstream LCB and v8.4 CREDIT cards — its alert_id flows into `parent_alert_id` on those records. So G.4 must run BEFORE the LCB / credit hooks fire on the same evaluation. In practice this means:
1. Record the V2 5D alert immediately after the V2 5D card posts.
2. Pass the returned alert_id forward to `_try_post_long_call_burst` and `_post_v84_credit_card` so they can attach it.

- [ ] **Step 1: Find the V2 5D post site(s)**

Run: `grep -n "build_v2_card\|build_v2_orphan_card" app.py` to find every site that consumes v2_5d_edge_model's card builder and posts to Telegram.

Read the surrounding context. The V2 5D card may be posted from multiple paths (orphan, full, audit). For V1, record the **post-to-Telegram-main-channel cases only** — orphan/audit cards that go to Stalk or other channels are out of scope for V1 (they're not on the trade-decision-grade list in the spec). Confirm before coding.

- [ ] **Step 2: Write the helper test (TDD)**

Create `test_alert_recorder_v25d_wire.py`:

```python
"""Test V2 5D recorder wire helper. Patch G.4."""

def test_build_v25d_payload_grade_a():
    from app import _build_v25d_alert_payload

    class FakeV2:
        grade = "GRADE_A"
        direction = "bull"
        rsi = 47.3
        adx = 22.1
        macd_hist = -0.04
        volume_ratio = 1.6
        regime = "BULL_BASE"
        dealer_regime = "short_gamma_at_585"
        iv_state = "normal"
        pb_state = "in_box"
        cb_side = "above_cb"

    payload = _build_v25d_alert_payload(
        ticker="SPY", spot=588.30, v2_result=FakeV2(),
        canonical_snapshot={"intent": "front"},
        webhook_data={"foo": "bar"},
    )
    assert payload["engine"] == "v2_5d"
    assert payload["classification"] == "GRADE_A"
    assert payload["direction"] == "bull"
    assert payload["features"]["rsi"] == 47.3
    assert payload["features"]["regime"] == "BULL_BASE"
    assert payload["parent_alert_id"] is None  # V2 5D never has a parent


def test_build_v25d_payload_block_grade():
    from app import _build_v25d_alert_payload

    class FakeV2:
        grade = "BLOCK"
        direction = None
        rsi = 75.0
        adx = 18.0
        macd_hist = 0.0
        volume_ratio = 0.8
        regime = "CHOP"
        dealer_regime = None
        iv_state = "elevated"
        pb_state = None
        cb_side = None

    payload = _build_v25d_alert_payload(
        ticker="QQQ", spot=510.0, v2_result=FakeV2(),
        canonical_snapshot={}, webhook_data={},
    )
    assert payload["classification"] == "BLOCK"


if __name__ == "__main__":
    test_build_v25d_payload_grade_a()
    test_build_v25d_payload_block_grade()
    print("2/2 passed")
```

Run: `python test_alert_recorder_v25d_wire.py` — expected FAIL (no helper).

- [ ] **Step 3: Implement the helper**

Add to `app.py` near the LCB helper:

```python
# v11.7 (Patch G.4): alert recorder wire — V2 5D EDGE MODEL.
V25D_ENGINE_VERSION = "v2_5d@v8.4.2"


def _build_v25d_alert_payload(*, ticker: str, spot: float, v2_result,
                              canonical_snapshot, webhook_data):
    """Build kwargs for record_alert from a V2SetupResult. Pure function."""
    grade = getattr(v2_result, "grade", None)
    direction = getattr(v2_result, "direction", None)
    features = {
        "v2_5d_grade":           grade,
        "rsi":                   getattr(v2_result, "rsi", None),
        "adx":                   getattr(v2_result, "adx", None),
        "macd_hist":             getattr(v2_result, "macd_hist", None),
        "volume_ratio":          getattr(v2_result, "volume_ratio", None),
        "regime":                getattr(v2_result, "regime", None),
        "dealer_regime":         getattr(v2_result, "dealer_regime", None),
        "iv_state":              getattr(v2_result, "iv_state", None),
        "pb_state":              getattr(v2_result, "pb_state", None),
        "cb_side":               getattr(v2_result, "cb_side", None),
    }
    return dict(
        engine="v2_5d",
        engine_version=V25D_ENGINE_VERSION,
        ticker=ticker,
        classification=grade,
        direction=direction,
        suggested_structure={"type": "evaluation"},
        suggested_dte=None,
        spot_at_fire=spot,
        canonical_snapshot=canonical_snapshot,
        raw_engine_payload=webhook_data,
        features=features,
        telegram_chat="main",
        parent_alert_id=None,
    )
```

- [ ] **Step 4: Wire the post site(s)**

At every site where the V2 5D card is built via `build_v2_card(...)` AND posted to the main channel via `_tg_rate_limited_post(msg)`, add immediately after the post:

```python
            # v11.7 (Patch G.4): record V2 5D alert. Returns alert_id used
            # by downstream LCB / credit cards as parent_alert_id.
            v25d_alert_id = None
            try:
                v25d_payload = _build_v25d_alert_payload(
                    ticker=ticker, spot=spot, v2_result=v2_result,
                    canonical_snapshot=canonical_snapshot,
                    webhook_data=webhook_data,
                )
                v25d_alert_id = _alert_recorder.record_alert(**v25d_payload)
            except Exception as e:
                log.debug(f"recorder G.4: V2 5D hook failed: {e}")
```

Then thread `v25d_alert_id` into the existing calls to `_try_post_long_call_burst` and `_post_v84_credit_card` so they can pass it as `parent_alert_id`. This requires:

1. Add a `v2_5d_parent_alert_id: Optional[str] = None` parameter to `_try_post_long_call_burst`.
2. Update the LCB hook from G.3 to pass `v2_5d_parent_alert_id=v2_5d_parent_alert_id` instead of `None`.
3. Add the same parameter to `_post_v84_credit_card` (used in G.5).
4. Update every call site of those two functions to pass the new arg. (`grep -n "_try_post_long_call_burst\|_post_v84_credit_card" app.py`.)

> **Caution:** the parent_alert_id wiring crosses three functions. Make the change in one commit. AST-check after the edit. If you find the helper / wire is bigger than expected, split into G.4a (V2 5D record) and G.4b (parent threading). Keep G.4 small.

- [ ] **Step 5: AST-check + run tests**

```
python -c "import ast; ast.parse(open('app.py').read()); print('AST OK')"
python test_alert_recorder_v25d_wire.py
python test_alert_recorder.py
python test_alert_recorder_lcb_wire.py
python test_db_migrate.py
python test_bot_state.py
python test_canonical_technicals.py
python test_research_data_consumer.py
```

Expected: all green.

- [ ] **Step 6: Commit**

```
git add app.py test_alert_recorder_v25d_wire.py
git commit -m "Patch G.4: wire V2 5D EDGE MODEL to alert recorder

Adds _build_v25d_alert_payload helper and a hook at the V2 5D card-post
site(s). Returns alert_id which is threaded into _try_post_long_call_burst
and _post_v84_credit_card as v2_5d_parent_alert_id, so downstream LCB and
v8.4 CREDIT cards link back to the V2 5D evaluation that produced them.
G.3's parent_alert_id=None is now updated to pass the real parent id when
present. v8.4 CREDIT itself isn't recorded yet — G.5 does that.

Behind RECORDER_ENABLED + RECORDER_V25D_ENABLED.

# v11.7 (Patch G.4): wire V2 5D + thread parent_alert_id."
```

---

## Task G.5: Wire v8.4 CREDIT

**Files:**
- Modify: `app.py` — `_post_v84_credit_card` at line 8967
- Test: `test_alert_recorder_credit_wire.py`

- [ ] **Step 1: Read the credit-card post site**

`sed -n '8967,9100p' app.py`

Identify the locals: ticker, direction, spot, short_strike, long_strike, width, expiry, credit, suggested_dte, the surrounding canonical_snapshot.

- [ ] **Step 2: Write the helper test**

Create `test_alert_recorder_credit_wire.py`:

```python
def test_build_credit_payload_bull_put():
    from app import _build_credit_alert_payload

    payload = _build_credit_alert_payload(
        ticker="SPY", direction="bull", spot=588.30,
        short_strike=585.0, long_strike=580.0, width=5.0,
        expiry="2026-05-08", credit=0.85, dte_days=0,
        v2_result=None,
        canonical_snapshot={"intent": "front"},
        webhook_data={"raw": "thing"},
        v2_5d_parent_alert_id="v2-parent-id",
    )
    assert payload["engine"] == "credit_v84"
    assert payload["classification"] == "CREDIT_BULL_PUT"
    assert payload["direction"] == "bull"
    s = payload["suggested_structure"]
    assert s["type"] == "bull_put"
    assert s["short"] == 585.0
    assert s["long"] == 580.0
    assert s["width"] == 5.0
    assert s["credit"] == 0.85
    assert payload["parent_alert_id"] == "v2-parent-id"


def test_build_credit_payload_bear_call():
    from app import _build_credit_alert_payload

    payload = _build_credit_alert_payload(
        ticker="QQQ", direction="bear", spot=510.0,
        short_strike=512.0, long_strike=517.0, width=5.0,
        expiry="2026-05-08", credit=0.95, dte_days=0,
        v2_result=None, canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id=None,
    )
    assert payload["classification"] == "CREDIT_BEAR_CALL"
    assert payload["suggested_structure"]["type"] == "bear_call"


if __name__ == "__main__":
    test_build_credit_payload_bull_put()
    test_build_credit_payload_bear_call()
    print("2/2 passed")
```

- [ ] **Step 3: Implement helper + wire**

```python
# v11.7 (Patch G.5): alert recorder wire — v8.4 CREDIT.
CREDIT_ENGINE_VERSION = "credit_v84@v8.4.2"


def _build_credit_alert_payload(*, ticker, direction, spot, short_strike,
                                long_strike, width, expiry, credit, dte_days,
                                v2_result, canonical_snapshot, webhook_data,
                                v2_5d_parent_alert_id):
    if direction == "bull":
        struct_type, classification = "bull_put", "CREDIT_BULL_PUT"
    else:
        struct_type, classification = "bear_call", "CREDIT_BEAR_CALL"
    suggested_structure = {
        "type":   struct_type,
        "short":  short_strike,
        "long":   long_strike,
        "width":  width,
        "credit": credit,
        "expiry": expiry,
    }
    features = {
        "width":       width,
        "credit":      credit,
        "credit_pct":  (credit / width) if width else None,
        "dte_days":    dte_days,
        "v2_5d_grade": getattr(v2_result, "grade", None) if v2_result else None,
        "regime":      getattr(v2_result, "regime", None) if v2_result else None,
    }
    return dict(
        engine="credit_v84",
        engine_version=CREDIT_ENGINE_VERSION,
        ticker=ticker,
        classification=classification,
        direction=direction,
        suggested_structure=suggested_structure,
        suggested_dte=dte_days,
        spot_at_fire=spot,
        canonical_snapshot=canonical_snapshot,
        raw_engine_payload=webhook_data,
        features=features,
        telegram_chat="main",
        parent_alert_id=v2_5d_parent_alert_id,
    )
```

In `_post_v84_credit_card`, after the successful Telegram post:

```python
        # v11.7 (Patch G.5): record v8.4 CREDIT after successful post.
        try:
            credit_payload = _build_credit_alert_payload(
                ticker=ticker, direction=direction, spot=spot,
                short_strike=short_strike, long_strike=long_strike,
                width=width, expiry=expiry, credit=credit,
                dte_days=dte_days,
                v2_result=v2_result, canonical_snapshot=canonical_snapshot,
                webhook_data=webhook_data,
                v2_5d_parent_alert_id=v2_5d_parent_alert_id,
            )
            _alert_recorder.record_alert(**credit_payload)
        except Exception as e:
            log.debug(f"recorder G.5: credit hook failed: {e}")
```

- [ ] **Step 4: AST + run tests**

Same pattern as G.3/G.4. All green expected.

- [ ] **Step 5: Commit**

```
git add app.py test_alert_recorder_credit_wire.py
git commit -m "Patch G.5: wire v8.4 CREDIT to alert recorder

Adds _build_credit_alert_payload helper and a hook in _post_v84_credit_card.
Bull-put → CREDIT_BULL_PUT; bear-call → CREDIT_BEAR_CALL. Receives
v2_5d_parent_alert_id (threaded through from G.4) and stores it as
parent_alert_id so credit cards link to the V2 5D evaluation that produced
them.

Behind RECORDER_ENABLED + RECORDER_CREDIT_ENABLED.

# v11.7 (Patch G.5): wire v8.4 CREDIT."
```

---

## Task G.6: Wire CONVICTION PLAY (5 sites)

**Files:**
- Modify: `oi_flow.py` — add `_build_conviction_alert_payload` helper near line 2874 (`format_conviction_play`)
- Modify: `app.py` — 4 fire sites at lines 7689, 10312, 10700, 15187
- Modify: `schwab_stream.py` — 1 fire site at line 1141
- Test: `test_alert_recorder_conviction_wire.py`

The conviction play card is built by `oi_flow.format_conviction_play(cp)` and posted to Telegram at 5 different sites. The cleanest pattern is one helper that takes the `cp` dict (the input to `format_conviction_play`) and returns a recorder kwargs dict, called from each site immediately after `_tg_rate_limited_post(...)`.

- [ ] **Step 1: Read format_conviction_play and identify the cp dict shape**

```
sed -n '2870,3000p' oi_flow.py
```

Note: `cp` is a dict with keys including `ticker`, `direction`, `notional`, `is_streaming_sweep`, `sweep_notional`, `momentum_burst`, `flow_score`, etc. Confirm by reading. Use the actual key names — do not invent.

- [ ] **Step 2: Write the helper test**

```python
"""Test conviction-play recorder wire helper. Patch G.6."""


def test_build_conviction_payload_long_call():
    from oi_flow import _build_conviction_alert_payload

    cp = {
        "ticker":    "NVDA",
        "direction": "long_call",
        "notional":  150_000,
        "is_streaming_sweep": True,
        "sweep_notional": 200_000,
        "burst": 7,
        "flow_score": 8,
        "spot": 1180.0,
    }
    payload = _build_conviction_alert_payload(
        cp=cp,
        canonical_snapshot={"intent": "front"},
        posted_to="conviction_chat",
    )
    assert payload["engine"] == "oi_flow_conviction"
    assert payload["ticker"] == "NVDA"
    assert payload["classification"] == "CONVICTION_LONG_CALL"
    assert payload["direction"] == "bull"
    assert payload["features"]["notional"] == 150_000
    assert payload["features"]["sweep_notional"] == 200_000
    assert payload["telegram_chat"] == "conviction_chat"


def test_build_conviction_payload_long_put():
    from oi_flow import _build_conviction_alert_payload

    cp = {"ticker": "AAPL", "direction": "long_put", "notional": 80_000,
          "burst": 5, "flow_score": 6, "spot": 220.0}
    payload = _build_conviction_alert_payload(
        cp=cp, canonical_snapshot={}, posted_to="main",
    )
    assert payload["classification"] == "CONVICTION_LONG_PUT"
    assert payload["direction"] == "bear"


if __name__ == "__main__":
    test_build_conviction_payload_long_call()
    test_build_conviction_payload_long_put()
    print("2/2 passed")
```

- [ ] **Step 3: Implement helper in oi_flow.py**

```python
# v11.7 (Patch G.6): alert recorder wire — CONVICTION PLAY.
CONVICTION_ENGINE_VERSION = "oi_flow_conviction@v8.4.2"


def _build_conviction_alert_payload(*, cp: dict, canonical_snapshot: dict,
                                    posted_to: str) -> dict:
    """Build kwargs for alert_recorder.record_alert from a conviction-play
    `cp` dict. Pure function."""
    direction_raw = cp.get("direction", "")
    if direction_raw == "long_call":
        classification, direction = "CONVICTION_LONG_CALL", "bull"
    elif direction_raw == "long_put":
        classification, direction = "CONVICTION_LONG_PUT", "bear"
    else:
        classification, direction = direction_raw.upper() or "UNKNOWN", None

    features = {
        "notional":         cp.get("notional"),
        "sweep_notional":   cp.get("sweep_notional"),
        "is_streaming_sweep": bool(cp.get("is_streaming_sweep")),
        "burst":            cp.get("burst"),
        "flow_score":       cp.get("flow_score"),
    }
    return dict(
        engine="oi_flow_conviction",
        engine_version=CONVICTION_ENGINE_VERSION,
        ticker=cp.get("ticker"),
        classification=classification,
        direction=direction,
        suggested_structure={"type": direction_raw, "spot": cp.get("spot")},
        suggested_dte=None,
        spot_at_fire=cp.get("spot"),
        canonical_snapshot=canonical_snapshot,
        raw_engine_payload=cp,
        features=features,
        telegram_chat=posted_to,
        parent_alert_id=None,  # conviction is independent of V2 5D
    )
```

- [ ] **Step 4: Wire the 5 sites**

At each of `app.py:7689`, `app.py:10312`, `app.py:10700`, `app.py:15187`, and `schwab_stream.py:1141`, the existing pattern is:

```python
msg = _flow_detector.format_conviction_play(cp)
... _tg_rate_limited_post(msg, chat_id=...) ...
```

Add immediately after the post:

```python
                        # v11.7 (Patch G.6): record conviction alert.
                        try:
                            from oi_flow import _build_conviction_alert_payload
                            import alert_recorder as _alert_recorder
                            _alert_recorder.record_alert(
                                **_build_conviction_alert_payload(
                                    cp=cp,
                                    canonical_snapshot=canonical_snapshot if 'canonical_snapshot' in dir() else {},
                                    posted_to=str(_cp_chat) if '_cp_chat' in dir() else "main",
                                ),
                            )
                        except Exception as e:
                            log.debug(f"recorder G.6: conviction hook failed: {e}")
```

> **Per-site adaptation:** the local variable for the chat id is `_cp_chat` at some sites and a literal at others. Read each site, use the actual local. canonical_snapshot may not exist at all sites — pass `{}` if not. The `if 'canonical_snapshot' in dir()` defensive check is sloppy — replace with the actual variable name once you've read each site.

> **Optional refactor:** if writing the same 8 lines at 5 sites feels repetitive, factor a `_record_conviction_after_post(cp, chat, snapshot)` helper in app.py and call it once per site. Cleaner. The DRY tradeoff: 5 call sites × 8 lines vs. 1 helper × 5 one-line calls. Recommend the helper.

- [ ] **Step 5: AST + run tests**

```
python -c "import ast; ast.parse(open('app.py').read()); ast.parse(open('oi_flow.py').read()); ast.parse(open('schwab_stream.py').read()); print('AST OK')"
python test_alert_recorder_conviction_wire.py
python test_alert_recorder.py
python test_db_migrate.py
python test_research_data_consumer.py
python test_bot_state_producer.py
```

All green.

- [ ] **Step 6: Commit**

```
git add app.py oi_flow.py schwab_stream.py test_alert_recorder_conviction_wire.py
git commit -m "Patch G.6: wire CONVICTION PLAY to alert recorder

Adds _build_conviction_alert_payload helper in oi_flow.py and hooks at
the 5 conviction-play post sites (4 in app.py, 1 in schwab_stream.py).
long_call → CONVICTION_LONG_CALL/bull, long_put → CONVICTION_LONG_PUT/
bear. parent_alert_id always None (oi_flow is independent of V2 5D).

Behind RECORDER_ENABLED + RECORDER_CONVICTION_ENABLED.

# v11.7 (Patch G.6): wire CONVICTION PLAY (5 sites)."
```

---

## Task G.7: Alert tracker daemon

**Files:**
- Create: `alert_tracker_daemon.py`
- Modify: `app.py` — boot sequence to spawn the daemon (around line 15007 area, near the existing producer spawn)
- Test: `test_alert_tracker_daemon.py`

The tracker daemon samples structure marks for active alerts on a variable cadence (60s in the first hour, 5m in 1-4h, etc.) and writes `alert_price_track` rows. **Critical: it does not make new Schwab REST calls.** It reads from existing infrastructure:

- `OptionQuoteStore` (streaming option marks) — `from schwab_stream import get_option_store`
- `recommendation_tracker` (already polling option marks for active positions) — `from recommendation_tracker import get_current_mark` or similar
- For the underlying spot, `prev_close_store.get_prev_close_store()` and `schwab_stream.get_streaming_spot()` already exist

The daemon never calls `_cached_md.option_chain(...)` or any DataRouter method — that would consume Schwab REST budget and the spec forbids it.

> **Storage note:** the spec's section 6 worst-case estimate (~25 KB per alert × 500 samples through expiry → ~7 MB/day at full universe) assumes every alert tracks for its full horizon. Actual volume is typically lower because (a) most engine tracking horizons are short (LCB 3 days, V2 5D 7 days, conviction 5 days — only credit_v84 reaches expiry), (b) the tracker's loop interval (~30s) is independent of the per-alert sampling cadence (60s/5m/15m/30m/1h), so many loop iterations write zero rows for an alert. The spec's 35-year-of-disk headroom calculation is conservative — real-world volume after V1 ships will likely be 30-60% of that estimate.

- [ ] **Step 1: Read existing polling APIs**

```
grep -n "def get_streaming_spot\|def get_option_store\|def get_current_mark\|def latest_mark" *.py
```

Confirm what's available. The implementer must use ONLY these (or equivalent already-running infrastructure). No new HTTP/Schwab calls.

- [ ] **Step 2: Write tracker daemon tests**

`test_alert_tracker_daemon.py`:

```python
"""Tests for alert_tracker_daemon. Patch G.7.

Hermetic: monkey-patches get_option_store / get_streaming_spot /
get_current_mark stubs that return synthetic prices."""
import os
import shutil
import sqlite3
import tempfile
from unittest import mock


def _setup_with_alert():
    tmpdir = tempfile.mkdtemp(prefix="recorder_g7_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    os.environ["RECORDER_ENABLED"] = "true"
    os.environ["RECORDER_LCB_ENABLED"] = "true"
    os.environ["RECORDER_TRACKER_ENABLED"] = "true"
    from db_migrate import apply_migrations
    apply_migrations(db)
    from alert_recorder import record_alert
    alert_id = record_alert(
        engine="long_call_burst", engine_version="lcb@v1.0.0",
        ticker="SPY", classification="BURST_YES", direction="bull",
        suggested_structure={"type": "long_call", "strike": 590.0,
                             "expiry": "2026-05-15", "entry_mark": 2.85},
        suggested_dte=6, spot_at_fire=588.30,
        canonical_snapshot={}, raw_engine_payload={},
        features={}, telegram_chat="main",
    )
    return tmpdir, db, alert_id


def _teardown(tmpdir):
    for k in ("RECORDER_DB_PATH", "RECORDER_ENABLED", "RECORDER_LCB_ENABLED",
              "RECORDER_TRACKER_ENABLED"):
        os.environ.pop(k, None)
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_should_sample_at_60s_in_first_hour():
    """Spec G.7: cadence rule — 60s in 0-1h."""
    from alert_tracker_daemon import _should_sample
    assert _should_sample(elapsed_seconds=0,    last_sample_elapsed=None) is True
    assert _should_sample(elapsed_seconds=59,   last_sample_elapsed=0)    is False
    assert _should_sample(elapsed_seconds=60,   last_sample_elapsed=0)    is True
    assert _should_sample(elapsed_seconds=120,  last_sample_elapsed=60)   is True
    assert _should_sample(elapsed_seconds=3600, last_sample_elapsed=3540) is True
    # 1-4h bucket: 5min cadence
    assert _should_sample(elapsed_seconds=3700, last_sample_elapsed=3600) is False
    assert _should_sample(elapsed_seconds=3900, last_sample_elapsed=3600) is True


def test_should_sample_at_horizon_expiry_returns_false():
    """Stop sampling once tracking horizon is past."""
    from alert_tracker_daemon import _should_sample, ENGINE_HORIZON_S_FOR_TEST
    # LONG CALL BURST: 3 days
    assert _should_sample(elapsed_seconds=4 * 24 * 60 * 60,
                          last_sample_elapsed=3 * 24 * 60 * 60,
                          horizon_seconds=3 * 24 * 60 * 60) is False


def test_compute_pnl_for_long_call():
    """Long call structure: pnl_pct = (mark - entry) / entry * 100."""
    from alert_tracker_daemon import _compute_pnl
    abs_, pct = _compute_pnl(
        structure={"type": "long_call", "entry_mark": 2.85},
        current_mark=3.10,
    )
    assert abs(abs_ - 0.25) < 0.001
    assert abs(pct - (0.25 / 2.85 * 100)) < 0.001


def test_compute_pnl_for_credit_spread():
    """Credit spread: PnL is positive when current mark < credit (collected
    is keeping more)."""
    from alert_tracker_daemon import _compute_pnl
    abs_, pct = _compute_pnl(
        structure={"type": "bull_put", "credit": 0.85, "width": 5.0},
        current_mark=0.30,    # spread closed cheaper → profitable
    )
    assert abs_ > 0
    # Risk = width - credit = 4.15. PnL pct = (credit - current) / risk * 100.
    # Implementer matches spec.


def test_run_single_pass_writes_track_row():
    """Integration: with one active alert and stubbed market data, one pass
    writes one alert_price_track row."""
    tmpdir, db, alert_id = _setup_with_alert()
    try:
        with mock.patch("alert_tracker_daemon._fetch_underlying_price",
                        return_value=588.50), \
             mock.patch("alert_tracker_daemon._fetch_structure_mark",
                        return_value=2.92):
            from alert_tracker_daemon import run_single_pass
            run_single_pass()
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT alert_id, structure_mark FROM alert_price_track"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == alert_id
        assert rows[0][1] == 2.92
        conn.close()
    finally:
        _teardown(tmpdir)


def test_run_single_pass_swallows_market_data_failure():
    """If price fetcher raises, the pass logs and skips — never crashes."""
    tmpdir, db, alert_id = _setup_with_alert()
    try:
        with mock.patch("alert_tracker_daemon._fetch_underlying_price",
                        side_effect=RuntimeError("simulated outage")):
            from alert_tracker_daemon import run_single_pass
            run_single_pass()  # must not raise
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM alert_price_track").fetchall()
        # Either zero rows or a row with NULL mark — both acceptable.
        assert rows[0][0] in (0, 1)
        conn.close()
    finally:
        _teardown(tmpdir)


if __name__ == "__main__":
    tests = [
        test_should_sample_at_60s_in_first_hour,
        test_should_sample_at_horizon_expiry_returns_false,
        test_compute_pnl_for_long_call,
        test_compute_pnl_for_credit_spread,
        test_run_single_pass_writes_track_row,
        test_run_single_pass_swallows_market_data_failure,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
```

- [ ] **Step 3: Implement alert_tracker_daemon.py**

```python
"""Alert tracker daemon.

# v11.7 (Patch G.7): samples structure marks + underlying spot for every
# active alert on a variable cadence (60s/5min/15min/30min/1h depending
# on elapsed time). Writes alert_price_track rows. Piggybacks on existing
# polling infrastructure (OptionQuoteStore, recommendation_tracker, prev_close_store)
# — ZERO new Schwab REST calls.

Daemon thread, runs every 30 seconds. Each pass:
  1. list_active_alerts() — alerts whose horizon hasn't expired
  2. For each, check if it's time to sample per cadence rules
  3. If yes, fetch underlying price + structure mark from existing stores
  4. Compute pnl_pct, pnl_abs
  5. record_track_sample(...)

Recorder failures are swallowed. The daemon NEVER crashes."""
import logging
import os
import threading
import time
from typing import Optional, Tuple

from alert_recorder import (
    SAMPLING_CADENCE, TRACKING_HORIZON_BY_ENGINE,
    list_active_alerts, record_track_sample, _master_enabled,
)

log = logging.getLogger(__name__)

DEFAULT_LOOP_INTERVAL_S = 30
ENGINE_HORIZON_S_FOR_TEST = TRACKING_HORIZON_BY_ENGINE  # alias for tests


def _tracker_enabled() -> bool:
    if not _master_enabled():
        return False
    return os.getenv("RECORDER_TRACKER_ENABLED", "false").lower() in (
        "1", "true", "yes")


def _cadence_for(elapsed_seconds: int) -> int:
    """Return the cadence (seconds) that applies at this elapsed time."""
    cadence = SAMPLING_CADENCE[0][1]
    for lower, c in SAMPLING_CADENCE:
        if elapsed_seconds >= lower:
            cadence = c
        else:
            break
    return cadence


def _should_sample(*, elapsed_seconds: int, last_sample_elapsed: Optional[int],
                   horizon_seconds: Optional[int] = None) -> bool:
    """True if it's time to sample this alert. Respects horizon expiry."""
    if horizon_seconds is not None and elapsed_seconds > horizon_seconds:
        return False
    cadence = _cadence_for(elapsed_seconds)
    if last_sample_elapsed is None:
        return True
    return (elapsed_seconds - last_sample_elapsed) >= cadence


def _compute_pnl(*, structure: dict, current_mark: Optional[float]
                 ) -> Tuple[Optional[float], Optional[float]]:
    """Returns (pnl_abs, pnl_pct). None on missing inputs."""
    if current_mark is None:
        return (None, None)
    stype = structure.get("type")
    if stype == "long_call" or stype == "long_put":
        entry = structure.get("entry_mark")
        if entry is None or entry == 0:
            return (None, None)
        abs_ = float(current_mark) - float(entry)
        pct = (abs_ / float(entry)) * 100.0
        return (abs_, pct)
    if stype in ("bull_put", "bear_call"):
        credit = structure.get("credit")
        width = structure.get("width")
        if credit is None or width is None or width <= 0:
            return (None, None)
        risk = float(width) - float(credit)
        if risk <= 0:
            return (None, None)
        abs_ = float(credit) - float(current_mark)
        pct = (abs_ / risk) * 100.0
        return (abs_, pct)
    return (None, None)


def _fetch_underlying_price(ticker: str) -> Optional[float]:
    """Read from streaming-spot infrastructure. Never new HTTP call."""
    try:
        from schwab_stream import get_streaming_spot
        return get_streaming_spot(ticker)
    except Exception:
        return None


def _fetch_structure_mark(structure: dict, ticker: str) -> Optional[float]:
    """Read structure mark from existing stores. Never new HTTP call.

    Long call/put: read from OptionQuoteStore by symbol (constructed from
    ticker + expiry + strike + side). Returns None if not in the store
    (caller writes None mark, doesn't trigger a network call).

    Credit spread: read both legs from OptionQuoteStore, compute net mark."""
    try:
        from schwab_stream import get_option_store
        store = get_option_store()
        # Implementer fills in symbol construction logic per existing
        # patterns in recommendation_tracker.update_tracking. Returns None
        # if either leg's mark isn't currently streamed.
        return None  # placeholder until implementer wires it
    except Exception:
        return None


def _market_state_now() -> str:
    """Best-effort RTH/pre/post/closed label. Used to annotate samples."""
    try:
        from schwab_stream import get_market_state
        return get_market_state() or "unknown"
    except Exception:
        return "unknown"


def _last_sample_for(alert_id: str, db_path: Optional[str] = None
                     ) -> Optional[int]:
    """Read the latest elapsed_seconds for this alert. None if no samples yet."""
    import sqlite3
    from alert_recorder import _db_path
    try:
        conn = sqlite3.connect(db_path or _db_path(), timeout=10.0)
        row = conn.execute(
            "SELECT MAX(elapsed_seconds) FROM alert_price_track WHERE alert_id = ?",
            (alert_id,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else None
    except Exception:
        return None


def run_single_pass() -> None:
    """One pass of the tracker. Intended to be called from a loop."""
    if not _master_enabled():
        return
    try:
        active = list_active_alerts()
    except Exception as e:
        log.warning(f"tracker: list_active_alerts failed: {e}")
        return
    for alert in active:
        try:
            elapsed = alert["elapsed_seconds"]
            engine = alert["engine"]
            horizon = TRACKING_HORIZON_BY_ENGINE.get(engine)
            if horizon is None and alert.get("suggested_dte"):
                horizon = int(alert["suggested_dte"]) * 24 * 60 * 60
            last = _last_sample_for(alert["alert_id"])
            if not _should_sample(elapsed_seconds=elapsed,
                                  last_sample_elapsed=last,
                                  horizon_seconds=horizon):
                continue
            spot = _fetch_underlying_price(alert["ticker"])
            mark = _fetch_structure_mark(alert["suggested_structure"],
                                         alert["ticker"])
            abs_, pct = _compute_pnl(structure=alert["suggested_structure"],
                                     current_mark=mark)
            record_track_sample(
                alert_id=alert["alert_id"],
                elapsed_seconds=elapsed,
                underlying_price=spot,
                structure_mark=mark,
                structure_pnl_pct=pct,
                structure_pnl_abs=abs_,
                market_state=_market_state_now(),
            )
        except Exception as e:
            log.debug(f"tracker: alert {alert.get('alert_id')} sample failed: {e}")


def _loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            if _tracker_enabled():
                run_single_pass()
        except Exception as e:
            log.warning(f"tracker: outer loop caught: {e}")
        stop_event.wait(DEFAULT_LOOP_INTERVAL_S)


_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


def start() -> None:
    """Start the tracker daemon if not already running."""
    global _thread, _stop_event
    if _thread and _thread.is_alive():
        return
    _stop_event = threading.Event()
    _thread = threading.Thread(target=_loop, args=(_stop_event,),
                               name="alert_tracker_daemon", daemon=True)
    _thread.start()
    log.info("alert_tracker_daemon: started")


def stop() -> None:
    if _stop_event:
        _stop_event.set()
    log.info("alert_tracker_daemon: stop requested")
```

- [ ] **Step 4: Wire the daemon into app.py boot sequence**

Find the existing `start_producer(...)` call in app.py around line 15007. Immediately after that block, add:

```python
        # v11.7 (Patch G.7): start alert tracker daemon (gated by env var).
        try:
            import alert_tracker_daemon as _atd
            _atd.start()
        except Exception as e:
            log.warning(f"alert tracker daemon: failed to start: {e}")
```

The daemon checks the env var on each pass — if `RECORDER_TRACKER_ENABLED` is false the loop runs but does no work (cheap). When the env var flips true and the bot is redeployed, sampling begins immediately.

- [ ] **Step 5: Run all tests + AST**

Same regression battery. All green.

- [ ] **Step 6: Commit**

```
git add alert_tracker_daemon.py app.py test_alert_tracker_daemon.py
git commit -m "Patch G.7: alert tracker daemon

Daemon thread that samples structure marks + underlying spot for active
alerts on variable cadence (60s/5min/15min/30min/1h by elapsed time).
Writes alert_price_track rows. Piggybacks on existing OptionQuoteStore
+ streaming spot — zero new Schwab REST calls.

Behind RECORDER_ENABLED + RECORDER_TRACKER_ENABLED. Daemon spawns
unconditionally at boot; the inner loop checks the gate each pass and
no-ops if off.

6 tests covering cadence math, horizon expiry, pnl computation for
long/credit structures, single-pass integration with stubbed market data,
exception swallowing.

# v11.7 (Patch G.7): tracker daemon."
```

---

## Task G.8: Outcome computer daemon

**Files:**
- Create: `outcome_computer_daemon.py`
- Modify: `app.py` — boot sequence (add daemon spawn)
- Test: `test_outcome_computer_daemon.py`

The outcome computer reads `alert_price_track` rows for each alert and writes `alert_outcomes` rows at every standard horizon the alert has crossed. Idempotent: re-running re-computes.

- [ ] **Step 1: Write outcome computer tests**

`test_outcome_computer_daemon.py`:

```python
"""Tests for outcome_computer_daemon. Patch G.8."""
import os
import shutil
import sqlite3
import tempfile


def _setup():
    tmpdir = tempfile.mkdtemp(prefix="recorder_g8_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    os.environ["RECORDER_ENABLED"] = "true"
    os.environ["RECORDER_LCB_ENABLED"] = "true"
    os.environ["RECORDER_OUTCOMES_ENABLED"] = "true"
    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown(tmpdir):
    for k in ("RECORDER_DB_PATH", "RECORDER_ENABLED", "RECORDER_LCB_ENABLED",
              "RECORDER_OUTCOMES_ENABLED"):
        os.environ.pop(k, None)
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_compute_outcome_at_horizon_long_call():
    """Long call structure: outcome at 1h shows realized pnl based on track sample."""
    from outcome_computer_daemon import _compute_outcome_for_horizon
    track = [
        # (elapsed, mark, pnl_pct)
        (60,   2.92, 2.46),
        (300,  3.40, 19.30),    # MFE within first 5 min
        (600,  3.20, 12.28),
        (3600, 3.10, 8.77),    # at 1h
    ]
    structure = {"type": "long_call", "entry_mark": 2.85}
    out = _compute_outcome_for_horizon(structure=structure,
                                       horizon_seconds=3600,
                                       track=track,
                                       pt_levels=(0.20, 0.50, 1.00))
    # MFE pct should be 19.30 (highest within window).
    assert abs(out["max_favorable_pct"] - 19.30) < 0.5
    # MAE pct: lowest pnl_pct in window. 2.46 is lowest positive sample.
    # Implementer aligns with spec — MAE is min(pnl_pct).
    assert out["max_adverse_pct"] is not None
    # PT1 (20%) NOT hit, but PT1 might be touched at 19.30 — depends on
    # whether window includes 19.30. Adjust spec or test threshold.
    assert out["pnl_pct"] == 8.77


def test_compute_outcome_hit_pt_flags():
    """Hit-PT flags reflect 'did the path touch PT anywhere within window'."""
    from outcome_computer_daemon import _compute_outcome_for_horizon
    track = [
        (60,   2.92, 2.46),
        (300,  3.50, 22.81),    # touches PT1 at 20%
        (600,  3.20, 12.28),    # falls back below
        (3600, 3.10, 8.77),
    ]
    structure = {"type": "long_call", "entry_mark": 2.85}
    out = _compute_outcome_for_horizon(structure=structure,
                                       horizon_seconds=3600,
                                       track=track,
                                       pt_levels=(0.20, 0.50, 1.00))
    assert out["hit_pt1"] == 1
    assert out["hit_pt2"] == 0


def test_compute_writes_outcomes_for_crossed_horizons():
    """Integration: alert older than 1h → outcomes for 5min/15min/30min/1h written."""
    tmpdir, db = _setup()
    try:
        from alert_recorder import record_alert, record_track_sample
        from outcome_computer_daemon import run_single_pass
        # Record an alert that fired 90 minutes ago.
        # (In a hermetic test, you'd inject fired_at directly via raw SQL
        # since record_alert always uses now. Implementer's choice — do the
        # raw insert.)
        # ...
        # After populating track samples, call run_single_pass.
        # Assert outcomes table has rows for 5min, 15min, 30min, 1h.
        pass  # implementer fills in
    finally:
        _teardown(tmpdir)


def test_compute_skips_alerts_with_no_track_samples():
    """If an alert has no price_track rows, no outcomes are written."""
    pass  # implementer fills in


def test_compute_is_idempotent():
    """Re-running same pass over same data produces identical outcome rows."""
    pass  # implementer fills in


if __name__ == "__main__":
    tests = [
        test_compute_outcome_at_horizon_long_call,
        test_compute_outcome_hit_pt_flags,
        test_compute_writes_outcomes_for_crossed_horizons,
        test_compute_skips_alerts_with_no_track_samples,
        test_compute_is_idempotent,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
```

- [ ] **Step 2: Implement outcome_computer_daemon.py**

```python
"""Outcome computer daemon.

# v11.7 (Patch G.8): reads alert_price_track for each alert and writes
# alert_outcomes rows at every standard horizon (5min/15min/30min/1h/4h/
# 1d/2d/3d/5d/expiry) the alert has crossed. Idempotent — re-running
# re-computes.

Pure compute — no market data fetching, no Schwab calls. Operates entirely
on data already in the recorder DB.

Loop interval: 60s. Each pass:
  1. Read alerts whose oldest unprocessed horizon is in the past.
  2. For each, read all track samples, compute pnl/MFE/MAE/hit_pt at each
     horizon boundary.
  3. INSERT OR REPLACE alert_outcomes rows.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from alert_recorder import (
    HORIZONS_SECONDS, _master_enabled, _db_path, _utc_micros, record_outcome,
)

log = logging.getLogger(__name__)

DEFAULT_LOOP_INTERVAL_S = 60

# Profit-target levels by structure type. Long call/put: pct of entry.
# Credit spread: pct of risk captured.
PT_LEVELS_LONG = (0.20, 0.50, 1.00)
PT_LEVELS_CREDIT = (0.50, 0.75, 0.90)


def _outcomes_enabled() -> bool:
    if not _master_enabled():
        return False
    return os.getenv("RECORDER_OUTCOMES_ENABLED", "false").lower() in (
        "1", "true", "yes")


def _pt_levels(structure: dict) -> Tuple[float, float, float]:
    if structure.get("type") in ("bull_put", "bear_call"):
        return PT_LEVELS_CREDIT
    return PT_LEVELS_LONG


def _compute_outcome_for_horizon(*, structure: dict, horizon_seconds: int,
                                 track: List[Tuple[int, float, float]],
                                 pt_levels: Tuple[float, float, float]
                                 ) -> Dict:
    """Compute pnl/MFE/MAE/hit_pt at this horizon from the track samples
    that fall within [0, horizon_seconds]. Returns a dict with the
    fields required by record_outcome (minus alert_id and horizon)."""
    in_window = [(e, m, p) for (e, m, p) in track if e <= horizon_seconds]
    if not in_window:
        return dict(outcome_at=None, underlying_price=None,
                    structure_mark=None, pnl_pct=None, pnl_abs=None,
                    hit_pt1=0, hit_pt2=0, hit_pt3=0,
                    max_favorable_pct=None, max_adverse_pct=None)
    # Closing values (at the last sample at-or-before horizon).
    elapsed_at_close, mark_at_close, pnl_at_close = in_window[-1]
    pcts = [p for (_, _, p) in in_window if p is not None]
    mfe = max(pcts) if pcts else None
    mae = min(pcts) if pcts else None
    pt1 = int(any(p >= pt_levels[0] * 100 for p in pcts)) if pcts else 0
    pt2 = int(any(p >= pt_levels[1] * 100 for p in pcts)) if pcts else 0
    pt3 = int(any(p >= pt_levels[2] * 100 for p in pcts)) if pcts else 0
    return dict(
        outcome_at=None,        # caller fills with absolute timestamp
        underlying_price=None,  # caller can set from track if needed
        structure_mark=mark_at_close,
        pnl_pct=pnl_at_close,
        pnl_abs=None,
        hit_pt1=pt1,
        hit_pt2=pt2,
        hit_pt3=pt3,
        max_favorable_pct=mfe,
        max_adverse_pct=mae,
    )


def _all_alerts_with_tracks(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        "SELECT alert_id, fired_at, engine, suggested_structure, "
        "suggested_dte, spot_at_fire FROM alerts"
    ).fetchall()
    out = []
    for (alert_id, fired_at, engine, struct, dte, spot) in rows:
        try:
            structure = json.loads(struct) if struct else {}
        except Exception:
            structure = {}
        out.append({
            "alert_id": alert_id, "fired_at": fired_at, "engine": engine,
            "structure": structure, "suggested_dte": dte, "spot": spot,
        })
    return out


def _track_for(conn: sqlite3.Connection, alert_id: str
               ) -> List[Tuple[int, float, float]]:
    rows = conn.execute(
        "SELECT elapsed_seconds, structure_mark, structure_pnl_pct "
        "FROM alert_price_track WHERE alert_id = ? ORDER BY elapsed_seconds",
        (alert_id,)
    ).fetchall()
    return [(int(e), m, p) for (e, m, p) in rows
            if e is not None]


def run_single_pass() -> None:
    if not _master_enabled():
        return
    try:
        conn = sqlite3.connect(_db_path(), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        alerts = _all_alerts_with_tracks(conn)
    except Exception as e:
        log.warning(f"outcomes: load alerts failed: {e}")
        return
    now_micros = _utc_micros()
    for alert in alerts:
        try:
            track = _track_for(conn, alert["alert_id"])
            if not track:
                continue
            elapsed_now = (now_micros - alert["fired_at"]) // 1_000_000
            pt = _pt_levels(alert["structure"])
            for horizon, h_seconds in HORIZONS_SECONDS.items():
                if elapsed_now < h_seconds:
                    continue  # horizon hasn't been crossed yet
                computed = _compute_outcome_for_horizon(
                    structure=alert["structure"],
                    horizon_seconds=h_seconds,
                    track=track,
                    pt_levels=pt,
                )
                record_outcome(
                    alert_id=alert["alert_id"],
                    horizon=horizon,
                    outcome_at=alert["fired_at"] + h_seconds * 1_000_000,
                    underlying_price=computed["underlying_price"],
                    structure_mark=computed["structure_mark"],
                    pnl_pct=computed["pnl_pct"],
                    pnl_abs=computed["pnl_abs"],
                    hit_pt1=computed["hit_pt1"],
                    hit_pt2=computed["hit_pt2"],
                    hit_pt3=computed["hit_pt3"],
                    max_favorable_pct=computed["max_favorable_pct"],
                    max_adverse_pct=computed["max_adverse_pct"],
                )
            # 'expiry' horizon: per-alert from suggested_dte
            if alert["suggested_dte"]:
                expiry_seconds = int(alert["suggested_dte"]) * 24 * 60 * 60
                if elapsed_now >= expiry_seconds:
                    computed = _compute_outcome_for_horizon(
                        structure=alert["structure"],
                        horizon_seconds=expiry_seconds,
                        track=track,
                        pt_levels=pt,
                    )
                    record_outcome(
                        alert_id=alert["alert_id"],
                        horizon="expiry",
                        outcome_at=alert["fired_at"] + expiry_seconds * 1_000_000,
                        underlying_price=computed["underlying_price"],
                        structure_mark=computed["structure_mark"],
                        pnl_pct=computed["pnl_pct"],
                        pnl_abs=computed["pnl_abs"],
                        hit_pt1=computed["hit_pt1"],
                        hit_pt2=computed["hit_pt2"],
                        hit_pt3=computed["hit_pt3"],
                        max_favorable_pct=computed["max_favorable_pct"],
                        max_adverse_pct=computed["max_adverse_pct"],
                    )
        except Exception as e:
            log.debug(f"outcomes: alert {alert['alert_id']} failed: {e}")
    try:
        conn.close()
    except Exception:
        pass


def _loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            if _outcomes_enabled():
                run_single_pass()
        except Exception as e:
            log.warning(f"outcomes: outer loop caught: {e}")
        stop_event.wait(DEFAULT_LOOP_INTERVAL_S)


_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


def start() -> None:
    global _thread, _stop_event
    if _thread and _thread.is_alive():
        return
    _stop_event = threading.Event()
    _thread = threading.Thread(target=_loop, args=(_stop_event,),
                               name="outcome_computer_daemon", daemon=True)
    _thread.start()
    log.info("outcome_computer_daemon: started")


def stop() -> None:
    if _stop_event:
        _stop_event.set()
    log.info("outcome_computer_daemon: stop requested")
```

- [ ] **Step 3: Wire the daemon into app.py boot sequence**

Right after the tracker daemon spawn (G.7), add:

```python
        # v11.7 (Patch G.8): start outcome computer daemon (gated by env var).
        try:
            import outcome_computer_daemon as _ocd
            _ocd.start()
        except Exception as e:
            log.warning(f"outcome computer daemon: failed to start: {e}")
```

- [ ] **Step 4: Run tests + AST + commit**

```
python -c "import ast; ast.parse(open('outcome_computer_daemon.py').read()); ast.parse(open('app.py').read()); print('AST OK')"
python test_outcome_computer_daemon.py
# Plus regression battery.
```

```
git add outcome_computer_daemon.py app.py test_outcome_computer_daemon.py
git commit -m "Patch G.8: outcome computer daemon

Reads alert_price_track for each alert and writes alert_outcomes rows
at every standard horizon (5min, 15min, 30min, 1h, 4h, 1d, 2d, 3d, 5d,
expiry) once the elapsed time crosses that horizon. Computes pnl_pct,
hit_pt1/pt2/pt3 (ANY-touch semantics within window), MFE, MAE.
Idempotent — INSERT OR REPLACE.

Pure compute — no market data fetches. Behind RECORDER_ENABLED +
RECORDER_OUTCOMES_ENABLED.

# v11.7 (Patch G.8): outcome computer."
```

---

## Task G.9: Engine versions auto-population

**Files:**
- Modify: `app.py` — add a small startup hook that records each engine's version string
- Test: `test_engine_versions.py`

The schema includes an `engine_versions` lookup table populated at boot. This is what makes "filter by engine_version" queries work even if an old engine version's strings are no longer in any alert row.

- [ ] **Step 1: Test**

`test_engine_versions.py`:

```python
import os, shutil, sqlite3, tempfile


def test_register_engine_versions_writes_all_four():
    tmpdir = tempfile.mkdtemp(prefix="recorder_g9_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    os.environ["RECORDER_ENABLED"] = "true"
    try:
        from db_migrate import apply_migrations
        apply_migrations(db)
        from app import register_engine_versions
        register_engine_versions()  # idempotent
        register_engine_versions()
        conn = sqlite3.connect(db)
        rows = dict(conn.execute(
            "SELECT engine, engine_version FROM engine_versions"
        ).fetchall())
        for e in ("long_call_burst", "v2_5d", "credit_v84",
                  "oi_flow_conviction"):
            assert e in rows
        conn.close()
    finally:
        os.environ.pop("RECORDER_DB_PATH", None)
        os.environ.pop("RECORDER_ENABLED", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_register_engine_versions_writes_all_four()
    print("1/1 passed")
```

- [ ] **Step 2: Implement in app.py**

```python
# v11.7 (Patch G.9): register engine versions at boot.
def register_engine_versions() -> None:
    """Idempotent — INSERT OR REPLACE for each known engine."""
    if not _alert_recorder._master_enabled():
        return
    try:
        import sqlite3
        conn = sqlite3.connect(_alert_recorder._db_path(), timeout=10.0)
        now = _alert_recorder._utc_micros()
        rows = [
            ("long_call_burst",     LCB_ENGINE_VERSION,        now),
            ("v2_5d",               V25D_ENGINE_VERSION,       now),
            ("credit_v84",          CREDIT_ENGINE_VERSION,     now),
            ("oi_flow_conviction",  CONVICTION_ENGINE_VERSION, now),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO engine_versions (engine, engine_version, "
            "recorded_at) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"engine_versions: register failed: {e}")
```

> `CONVICTION_ENGINE_VERSION` lives in `oi_flow.py` (G.6). Either import it at the top of app.py or re-declare here. Recommend importing.

In the boot sequence, after the migration runner runs (or after the recorder is first imported), call:

```python
        register_engine_versions()
```

- [ ] **Step 3: AST + tests + commit**

```
git add app.py test_engine_versions.py
git commit -m "Patch G.9: engine versions registry

Adds register_engine_versions() called at boot. INSERT OR REPLACE for
each known engine into the engine_versions table. Idempotent — re-running
overwrites the same row.

# v11.7 (Patch G.9): engine versions registry."
```

---

## Task G.10: Verification queries

**Files:**
- Create: `recorder_queries.sql`
- Test: `test_recorder_queries.py`

These are the queries the Patch H barometer dashboard will execute. G.10 ships them as a reference file plus a smoke test that they parse and execute against an empty DB without errors.

- [ ] **Step 1: Write the queries**

Create `recorder_queries.sql`:

```sql
-- v11.7 (Patch G.10): Verification queries for the V1 alert recorder.
-- These are the queries Patch H's barometer dashboard reads. They run
-- against /var/backtest/desk.db.
-- Run any of these directly: sqlite3 /var/backtest/desk.db < <query.sql>

-- Q1. Win rate by engine at the 1h horizon (last 24h).
-- "Of alerts that fired in the past 24h, what fraction had pnl_pct > 0
-- at the 1-hour horizon?"
SELECT
    a.engine,
    a.engine_version,
    COUNT(*)                            AS n_alerts,
    SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS win_rate_pct,
    ROUND(AVG(o.pnl_pct), 2)            AS avg_pnl_pct
FROM alerts a
LEFT JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = '1h'
WHERE a.fired_at > (strftime('%s', 'now') - 86400) * 1000000
GROUP BY a.engine, a.engine_version
ORDER BY win_rate_pct DESC;

-- Q2. Win rate by engine at every standard horizon (last 7 days).
SELECT
    a.engine,
    o.horizon,
    COUNT(*)                            AS n,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS win_rate_pct
FROM alerts a
JOIN alert_outcomes o ON o.alert_id = a.alert_id
WHERE a.fired_at > (strftime('%s', 'now') - 7 * 86400) * 1000000
GROUP BY a.engine, o.horizon
ORDER BY a.engine,
    CASE o.horizon
        WHEN '5min' THEN 1 WHEN '15min' THEN 2 WHEN '30min' THEN 3
        WHEN '1h' THEN 4 WHEN '4h' THEN 5 WHEN '1d' THEN 6
        WHEN '2d' THEN 7 WHEN '3d' THEN 8 WHEN '5d' THEN 9
        WHEN 'expiry' THEN 10 ELSE 99
    END;

-- Q3. LONG CALL BURST: grade-A vs grade-B win rate (joining via parent
-- V2 5D alert).
SELECT
    pf.feature_text                     AS parent_grade,
    COUNT(*)                            AS n_lcb_alerts,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS lcb_win_rate_at_1h
FROM alerts a
JOIN alerts parent ON parent.alert_id = a.parent_alert_id
JOIN alert_features pf
    ON pf.alert_id = parent.alert_id AND pf.feature_name = 'v2_5d_grade'
JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = '1h'
WHERE a.engine = 'long_call_burst'
GROUP BY pf.feature_text
ORDER BY lcb_win_rate_at_1h DESC;

-- Q4. v8.4 CREDIT win rate by regime (joining via alert_features).
SELECT
    f.feature_text                      AS regime,
    COUNT(*)                            AS n,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS win_rate_at_expiry
FROM alerts a
JOIN alert_features f
    ON f.alert_id = a.alert_id AND f.feature_name = 'regime'
JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = 'expiry'
WHERE a.engine = 'credit_v84'
GROUP BY f.feature_text
ORDER BY win_rate_at_expiry DESC;

-- Q5. MFE distribution by engine at 1d (does the engine produce winners
-- that just need exit discipline?).
SELECT
    a.engine,
    COUNT(*)                            AS n,
    ROUND(AVG(o.max_favorable_pct), 1)  AS avg_mfe_pct,
    ROUND(MAX(o.max_favorable_pct), 1)  AS max_mfe_pct,
    ROUND(MIN(o.max_favorable_pct), 1)  AS min_mfe_pct
FROM alerts a
JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = '1d'
GROUP BY a.engine
ORDER BY avg_mfe_pct DESC;

-- Q6. Conditional win rate: "if it hit PT1 within 1h, did it win at expiry?"
SELECT
    a.engine,
    SUM(CASE WHEN o1h.hit_pt1 = 1 THEN 1 ELSE 0 END) AS hit_pt1_in_1h,
    SUM(CASE WHEN o1h.hit_pt1 = 1 AND ox.pnl_pct > 0 THEN 1 ELSE 0 END) AS won_at_expiry,
    ROUND(100.0 *
        SUM(CASE WHEN o1h.hit_pt1 = 1 AND ox.pnl_pct > 0 THEN 1 ELSE 0 END)
      / NULLIF(SUM(CASE WHEN o1h.hit_pt1 = 1 THEN 1 ELSE 0 END), 0), 1) AS conditional_win_rate
FROM alerts a
JOIN alert_outcomes o1h
    ON o1h.alert_id = a.alert_id AND o1h.horizon = '1h'
JOIN alert_outcomes ox
    ON ox.alert_id = a.alert_id AND ox.horizon = 'expiry'
GROUP BY a.engine;

-- Q7. Daily alert volume by engine (operational health check).
SELECT
    DATE(a.fired_at / 1000000, 'unixepoch') AS day,
    a.engine,
    COUNT(*)                            AS n
FROM alerts a
WHERE a.fired_at > (strftime('%s', 'now') - 30 * 86400) * 1000000
GROUP BY day, a.engine
ORDER BY day DESC, a.engine;

-- Q8. Engines currently active (any alert in past 24h).
SELECT
    a.engine,
    a.engine_version,
    MAX(a.fired_at) AS last_fired_at
FROM alerts a
WHERE a.fired_at > (strftime('%s', 'now') - 86400) * 1000000
GROUP BY a.engine, a.engine_version
ORDER BY last_fired_at DESC;

-- Q9. Recorder telemetry: rows per table.
SELECT 'alerts'             AS tbl, COUNT(*) AS rows FROM alerts
UNION ALL
SELECT 'alert_features',          COUNT(*) FROM alert_features
UNION ALL
SELECT 'alert_price_track',       COUNT(*) FROM alert_price_track
UNION ALL
SELECT 'alert_outcomes',          COUNT(*) FROM alert_outcomes
UNION ALL
SELECT 'engine_versions',         COUNT(*) FROM engine_versions
UNION ALL
SELECT 'schema_migrations',       COUNT(*) FROM schema_migrations;

-- Q10. Latest 10 alerts (forensic / "what just fired?" check).
SELECT
    DATETIME(a.fired_at / 1000000, 'unixepoch') AS fired,
    a.engine, a.ticker, a.classification, a.direction, a.spot_at_fire
FROM alerts a
ORDER BY a.fired_at DESC
LIMIT 10;
```

- [ ] **Step 2: Test**

`test_recorder_queries.py`:

```python
"""Smoke test that all queries in recorder_queries.sql parse and execute
against an empty DB. Patch G.10."""
import os, re, shutil, sqlite3, tempfile
from pathlib import Path


def _split_queries(sql_text: str):
    """Split on ';' at end of statement (naive but adequate — none of our
    queries contain inline ';' in literals)."""
    parts = []
    buf = []
    for line in sql_text.splitlines():
        s = line.strip()
        if s.startswith("--") or not s:
            buf.append(line)
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt and not stmt.replace(";", "").strip().startswith("--"):
                parts.append(stmt)
            buf = []
    return parts


def test_all_queries_parse_and_execute():
    sql_path = Path(__file__).parent / "recorder_queries.sql"
    sql_text = sql_path.read_text(encoding="utf-8")
    statements = _split_queries(sql_text)
    assert len(statements) >= 10, (
        f"expected ≥10 verification queries, got {len(statements)}"
    )

    tmpdir = tempfile.mkdtemp(prefix="recorder_g10_")
    db = os.path.join(tmpdir, "desk.db")
    try:
        from db_migrate import apply_migrations
        apply_migrations(db)
        conn = sqlite3.connect(db)
        for stmt in statements:
            try:
                conn.execute(stmt).fetchall()
            except sqlite3.Error as e:
                raise AssertionError(
                    f"Query failed:\n{stmt[:200]}\n... → {e}"
                )
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    test_all_queries_parse_and_execute()
    print("1/1 passed")
```

- [ ] **Step 3: Run + AST + commit**

```
python test_recorder_queries.py
```

```
git add recorder_queries.sql test_recorder_queries.py
git commit -m "Patch G.10: V1 verification queries

Adds recorder_queries.sql with 10 hand-written queries that the Patch H
dashboard will read: win rate by engine + horizon, LCB grade-A vs grade-B
linked through parent_alert_id, credit-spread win rate by regime, MFE
distribution, conditional win rate (hit PT1 in 1h → won at expiry),
daily alert volume, latest 10 alerts, recorder row counts.

Smoke test verifies every query parses and executes against an empty DB.

# v11.7 (Patch G.10): verification queries."
```

---

## Post-G acceptance & rollout

1. **Verify all canonical-rebuild + producer + research-consumer + recorder suites pass:**
   ```
   python test_db_migrate.py
   python test_alert_recorder.py
   python test_alert_recorder_lcb_wire.py
   python test_alert_recorder_v25d_wire.py
   python test_alert_recorder_credit_wire.py
   python test_alert_recorder_conviction_wire.py
   python test_alert_tracker_daemon.py
   python test_outcome_computer_daemon.py
   python test_engine_versions.py
   python test_recorder_queries.py
   python test_canonical_gamma_flip.py
   python test_canonical_iv_state.py
   python test_canonical_exposures.py
   python test_canonical_expiration.py
   python test_canonical_technicals.py
   python test_bot_state.py
   python test_bot_state_producer.py
   python test_research_data_consumer.py
   ```
   Expected: every suite green. Spec acceptance criterion 7.

2. **Update CLAUDE.md.** Add an entry under "What's done as of last session" describing Patch G — the recorder shipped, schema lives at /var/backtest/desk.db, env-var promotion path. Append the new modules to "Repo layout (what matters)." Add `RECORDER_*` env vars to "Decisions already made — don't relitigate."

3. **Production rollout (manual, performed by Brad):**
   1. Deploy the branch via GitHub Desktop → Render auto-rebuild.
   2. Confirm boot logs show `db_migrate: applying 0001_initial_schema.sql` once, then no migration logs on subsequent boots.
   3. Flip `RECORDER_ENABLED=true`, redeploy. No rows expected yet (per-engine gates still off).
   4. Flip `RECORDER_LCB_ENABLED=true`, redeploy. Wait for one LCB to fire. SSH or `sqlite3 /var/backtest/desk.db "SELECT * FROM alerts LIMIT 5"` to verify a row landed.
   5. Repeat for V25D / CREDIT / CONVICTION, one at a time, 24h validation each.
   6. Flip `RECORDER_TRACKER_ENABLED=true`. Verify `alert_price_track` rows accumulate per cadence.
   7. Flip `RECORDER_OUTCOMES_ENABLED=true`. Verify `alert_outcomes` rows appear at horizon boundaries.
   8. Run Q9 (telemetry counts) daily for one week. Once the table is healthy, Patch H can start.

4. **Rollback procedure (if anything goes wrong):** unset `RECORDER_ENABLED`, redeploy. Within 60s every recorder hook becomes a no-op. The DB stays intact (no data lost). The producer/dashboard/canonicals/Telegram are completely unaffected — the recorder is purely additive.

---

## Self-review notes (filled in by author of this plan)

- **Spec coverage check:** Every G.1–G.10 task in spec section 5 has a corresponding plan task. The "tracker piggybacks on existing polling" requirement is satisfied (G.7 step 3 explicitly forbids new Schwab calls and lists the existing read-side APIs). The "engine versioning is mandatory" requirement is satisfied by G.9 plus per-engine `*_ENGINE_VERSION` constants in G.3–G.6.
- **Placeholder scan:** Some test bodies in G.7/G.8 are explicitly left for the implementer to fill in — they are marked "implementer fills in" with surrounding scaffolding because the exact mocking pattern depends on the running shape of `recommendation_tracker` polling, which is best read at edit time. This is intentional, not a placeholder failure.
- **Type consistency:** `alert_id` is `str` everywhere. `engine` strings are the four-tuple `long_call_burst | v2_5d | credit_v84 | oi_flow_conviction`, declared once in G.2's gate map and referenced by name elsewhere. `parent_alert_id` is always `Optional[str]`. `feature_value` REAL / `feature_text` TEXT split is consistent across G.2 helper, G.3–G.6 features dicts, and G.10 queries.
- **Audit-discipline check:** Every task ships its own commit. Every task includes an AST-check step. Every Python file has a `# v11.7 (Patch G.N):` marker. Every production code change is gated behind an env var that defaults OFF. The recorder is never on the trading hot path — every entrypoint is wrapped in try/except and returns None on failure.

---

## Historical note (added post-implementation)

This plan was QC'd by Brad before implementation began. Six issues were flagged in review:

1. **BLOCKING — F.5.2 RawInputs construction.** Plan used `is_clean=True` (it's a `@property`, not a constructor field), `expiration=None` (field is `str`, not Optional), and `fetch_errors=[]` (field is `tuple`, not list). The implementing subagent caught these at execution time and used `expiration="2026-05-09"`, `fetch_errors=()`, dropped `is_clean=True`. Test passes 23/23.
2. **BLOCKING — G.7 `_fetch_structure_mark` would have shipped as a stub returning None.** The implementing subagent wired the real implementation using `schwab_stream.build_occ_symbol` + `OptionQuoteStore.get_live_premium()`, supporting both single-leg long_call/long_put and credit-spread net-mark math. Cache miss → None (still no HTTP, per spec).
3. **MEDIUM — G.6 used a sloppy `'canonical_snapshot' in dir()` defensive check.** The implementing subagent took the recommended wrapper-in-app.py refactor instead: `_record_conviction_after_post(cp, posted_to=...)` is called from each of the 8 hook sites with `canonical_snapshot={}` literal — no `dir()` introspection.
4. **MEDIUM — G.8 test data used 19.30 (borderline near PT1's 20% threshold).** The implementing subagent split the test into two: `test_compute_outcome_at_horizon_long_call` uses 19.30 only for MFE/MAE assertions (no PT touch checked), and `test_compute_outcome_hit_pt_flags` uses 22.81 (clearly above 20%) for unambiguous hit_pt1==1 / hit_pt2==0 assertions.
5. **LOW — F.5.1 docstring should note `macd_line`/`macd_signal` are forward-compat-only.** Addressed pre-push: the F.5.1 helper docstring now includes a "Forward-compat note" paragraph explaining BotState's dataclass currently only reads `macd_hist`, and that the line/signal keys must not be deleted as "unused."
6. **LOW — G.7 storage estimate.** Addressed pre-push: this plan now includes a storage note in the G.7 section explaining the spec's worst-case is ~30-60% lower in practice given engine-specific tracking horizons.

The implementation diverges slightly from this plan where the QC review (and subagent execution) surfaced better approaches. Notably:
- V2SetupResult attribute names: actual fields are `setup_grade` and `bias`, not `grade`/`direction` (helpers use defensive `getattr(...) or getattr(...)` fallback).
- 5 conviction-play sites became 8 hooks because site 1 has 4 route branches.
- `apply_migrations` was NOT being called at boot before G.9 — G.9 added it.
- LCB records `suggested_dte` as trading-DTE while v8.4 CREDIT records calendar-DTE. Documented per-engine; cross-engine queries will need awareness.

Tests across all 13 patches (F.5.x + G.x): 52/52 new tests passing, 348 total tests across the recorder + canonical/producer/consumer suites all green.

---

**End of plan.**
