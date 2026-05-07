# Patch C — Research Page Redis Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `omega_dashboard/research_data.py` to read pre-built BotState envelopes from Redis (written by Patch B's `bot_state_producer`) instead of building them inline on every request. Cold-start dashboard load goes from ~3 minutes to <1 second.

**Architecture:** Add a parallel "consumer" code path inside `research_data.py` that, for each ticker, GETs `bot_state:{ticker}:{intent}` from Redis, parses the JSON envelope, validates `producer_version` ≥ `MIN_COMPATIBLE_PRODUCER_VERSION` and `convention_version` matches expected (Patch 9 protection). Missing keys, version mismatches, or JSON errors render as "warming up" snapshots — the dashboard still renders, just with skeleton cards for tickers Redis doesn't have yet. The whole path is gated by `RESEARCH_USE_REDIS=0` (default off) so the legacy inline-build path stays intact for rollback.

**Tech Stack:** Python 3, `redis-py` (already used by Patch B), Flask (existing dashboard), Jinja2 templates. No new dependencies.

---

## Decisions already locked in (from spec v4 + B series)

- **Reader-side `MIN_COMPATIBLE_PRODUCER_VERSION = 1`.** Bump only on TRULY breaking schema changes; additive producer bumps leave it constant.
- **Reader-side `EXPECTED_CONVENTION_VERSION = 2`.** Strict — Patch 9 protection is non-negotiable. Mismatch → "warming up" + warning log.
- **Forward compatibility:** envelopes with `producer_version > consumer's expected` are ACCEPTED (newer schemas are forward-compatible).
- **Backward incompatibility:** envelopes with `producer_version < MIN_COMPATIBLE` are REJECTED (rendered as warming up with warning).
- **Key format:** `bot_state:{ticker}:{intent}` — matches the producer's `KEY_PREFIX` (already in `bot_state_producer.py:48`).
- **Default intent for the Research page:** `front` (settled in Patch A; CLAUDE.md decision).
- **Env var `RESEARCH_USE_REDIS` defaults to off.** Same rollback pattern as `DASHBOARD_SPOT_USE_STREAMING` (Patch S.3). When unset, the legacy inline-build path runs unchanged. When set to `1`/`true`/`yes`/`on`, the new Redis consumer path runs.
- **No in-memory cache on the Redis path.** Redis itself is the cache, with per-tier TTLs (180s / 540s / 1800s). Dashboard requests do 35 Redis GETs per page load — microseconds.
- **"Warming up" UI distinct from "errored" UI.** TickerSnapshot gains a `warming_up: bool` field; the template differentiates via a new CSS class. Errored cards stay red; warming-up cards get a neutral "skeleton" style.
- **No data_router needed in the Redis path.** The expiration is on the envelope (`env["expiration"]`), so we don't call `canonical_expiration` either. The inline path keeps its `data_router` parameter for legacy use.

---

## File structure

**Modified:**
- `omega_dashboard/research_data.py` — adds `MIN_COMPATIBLE_PRODUCER_VERSION`, `EXPECTED_CONVENTION_VERSION`, `KEY_PREFIX` constants; `warming_up: bool` field on `TickerSnapshot`; private functions `_warming_up_snapshot()`, `_validate_envelope_versions()`, `_snapshot_from_envelope()`, `_load_snapshot_from_redis()`, `_research_data_from_redis()`; env-var-gated dispatch in `research_data()`. ~180 lines added; existing legacy path untouched.
- `omega_dashboard/routes.py` — `_get_redis()` import + pass `redis_client=` to `research_data()`. ~6 lines.
- `omega_dashboard/templates/dashboard/research.html` — new conditional branch for `snap.warming_up` rendering distinct from `snap.error`. ~15 lines.
- `omega_dashboard/static/omega.css` — new `.research-card-warming-up` class with skeleton styling (subtle pulse animation, neutral color). ~20 lines.
- `CLAUDE.md` — vocabulary entries for `RESEARCH_USE_REDIS` and `MIN_COMPATIBLE_PRODUCER_VERSION`/`EXPECTED_CONVENTION_VERSION`; decision entry for the Redis-consumer path. ~25 lines.

**Created:**
- `test_research_data_consumer.py` — repo root, ~280 lines, ~14 tests covering all consumer-side paths with a tiny `_FakeRedis` stub (just `get`/`set`).

**Total touched:** 1 new file (~280 lines), 5 modified files (~245 lines added).

---

# Task C.1 — Reader-side helpers + version gates

**Why first:** All the deterministic parsing/validation lives here. No callers yet, exhaustively unit-tested with mocked envelopes. Sets up B.2's consumer entry point.

**Files:**
- Modify: `omega_dashboard/research_data.py`
- Create: `test_research_data_consumer.py`

### Task 1.1 — Failing tests for the helpers

- [ ] **Step 1: Create the test file**

Create `test_research_data_consumer.py` (repo root):

```python
"""
test_research_data_consumer.py — unit tests for the Patch C consumer
path in omega_dashboard/research_data.py.

No network, no live Redis. Uses a tiny _FakeRedis stub (just get/set).
Tests the parsing/validation contract: well-formed envelopes parse to
correct TickerSnapshot, malformed/missing/version-mismatch envelopes
fall through to a warming-up snapshot.
"""

from __future__ import annotations
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: expected truthy, got {cond!r}")
        return False
    PASSED.append(msg)
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


class _FakeRedis:
    """Minimal Redis stand-in: just get/set. Don't add features unless
    the consumer needs them."""
    def __init__(self):
        self.kv = {}

    def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)


def _make_envelope(state_overrides=None, producer_version=1, convention_version=2,
                   intent="front", expiration="2026-05-08") -> dict:
    """Build a producer-shaped envelope for tests. State defaults to
    a fully-populated AAPL snapshot."""
    state = {
        "ticker": "AAPL",
        "spot": 184.50,
        "gamma_flip": 180.00,
        "distance_from_flip_pct": 2.5,
        "flip_location": "above",
        "atm_iv": 0.245,
        "iv_skew_pp": 1.20,
        "iv30": 0.230,
        "gex": 12345678.0,
        "dex": -45000.0,
        "vanna": 1234.5,
        "charm": -567.8,
        "gex_sign": "positive",
        "call_wall": 190.0,
        "put_wall": 175.0,
        "gamma_wall": 182.5,
        "fields_lit": 22,
        "fields_total": 64,
        "canonical_status": {"gamma_flip": "live", "iv_state": "live"},
        "chain_clean": True,
        "fetch_errors": [],
    }
    if state_overrides:
        state.update(state_overrides)
    return {
        "producer_version": producer_version,
        "convention_version": convention_version,
        "intent": intent,
        "expiration": expiration,
        "state": state,
    }


# ─────────────────────────────────────────────────────────────────────
# warming-up snapshot factory
# ─────────────────────────────────────────────────────────────────────

def test_warming_up_snapshot_has_all_none_fields():
    from omega_dashboard.research_data import _warming_up_snapshot
    snap = _warming_up_snapshot("AAPL", reason="missing key")
    assert_eq(snap.ticker, "AAPL", "ticker preserved")
    assert_eq(snap.warming_up, True, "warming_up flag set")
    assert_is_none(snap.spot, "spot is None")
    assert_is_none(snap.gamma_flip, "gamma_flip is None")
    assert_is_none(snap.gex, "gex is None")
    assert_eq(snap.flip_location, "unknown", "flip_location='unknown'")
    assert_eq(snap.gex_sign, "unknown", "gex_sign='unknown'")
    assert_eq(snap.fields_lit, 0, "fields_lit=0")
    assert_eq(snap.canonical_status, {}, "canonical_status={}")
    assert_eq(snap.fetch_errors, [], "fetch_errors=[]")
    assert_eq(snap.error, "missing key", "reason stored in error field for fallback display")


# ─────────────────────────────────────────────────────────────────────
# version validation
# ─────────────────────────────────────────────────────────────────────

def test_validate_envelope_versions_accepts_current():
    from omega_dashboard.research_data import _validate_envelope_versions
    env = _make_envelope(producer_version=1, convention_version=2)
    err = _validate_envelope_versions(env, "AAPL")
    assert_is_none(err, "current versions accepted")


def test_validate_envelope_versions_accepts_future_producer_version():
    """Forward compat: producer_version higher than reader's expected
    is accepted (additive schema changes)."""
    from omega_dashboard.research_data import _validate_envelope_versions
    env = _make_envelope(producer_version=99, convention_version=2)
    err = _validate_envelope_versions(env, "AAPL")
    assert_is_none(err, "future producer_version accepted (forward compat)")


def test_validate_envelope_versions_rejects_old_producer_version():
    """Producer below MIN_COMPATIBLE_PRODUCER_VERSION is rejected."""
    from omega_dashboard.research_data import _validate_envelope_versions
    env = _make_envelope(producer_version=0, convention_version=2)
    err = _validate_envelope_versions(env, "AAPL")
    assert_true(err is not None, "old producer_version rejected")
    assert_true("producer_version" in err, "error mentions producer_version")


def test_validate_envelope_versions_rejects_convention_mismatch():
    """convention_version mismatch is strict (Patch 9 protection)."""
    from omega_dashboard.research_data import _validate_envelope_versions
    env = _make_envelope(producer_version=1, convention_version=1)
    err = _validate_envelope_versions(env, "AAPL")
    assert_true(err is not None, "convention_version mismatch rejected")
    assert_true("convention_version" in err, "error mentions convention_version")


def test_validate_envelope_versions_rejects_missing_versions():
    """Envelope without producer_version or convention_version is malformed."""
    from omega_dashboard.research_data import _validate_envelope_versions
    err1 = _validate_envelope_versions({"convention_version": 2}, "AAPL")
    assert_true(err1 is not None, "missing producer_version rejected")
    err2 = _validate_envelope_versions({"producer_version": 1}, "AAPL")
    assert_true(err2 is not None, "missing convention_version rejected")


# ─────────────────────────────────────────────────────────────────────
# envelope → snapshot parsing
# ─────────────────────────────────────────────────────────────────────

def test_snapshot_from_envelope_full_data():
    from omega_dashboard.research_data import _snapshot_from_envelope
    env = _make_envelope()
    snap = _snapshot_from_envelope("AAPL", env)
    assert_eq(snap.ticker, "AAPL", "ticker on snapshot")
    assert_eq(snap.warming_up, False, "warming_up=False for valid envelope")
    assert_is_none(snap.error, "error=None for valid envelope")
    assert_eq(snap.spot, 184.50, "spot parsed")
    assert_eq(snap.gamma_flip, 180.00, "gamma_flip parsed")
    assert_eq(snap.flip_location, "above", "flip_location parsed")
    assert_eq(snap.atm_iv, 0.245, "atm_iv parsed")
    assert_eq(snap.gex, 12345678.0, "gex parsed")
    assert_eq(snap.gex_sign, "positive", "gex_sign parsed")
    assert_eq(snap.call_wall, 190.0, "call_wall parsed")
    assert_eq(snap.fields_lit, 22, "fields_lit parsed")
    assert_eq(snap.canonical_status, {"gamma_flip": "live", "iv_state": "live"},
              "canonical_status parsed as dict")


def test_snapshot_from_envelope_handles_missing_optional_fields():
    """A producer might emit an envelope with only a few fields populated
    (early days, mid-rebuild). Snapshot construction must not crash."""
    from omega_dashboard.research_data import _snapshot_from_envelope
    env = _make_envelope(state_overrides={
        "ticker": "AAPL", "spot": 184.50,
        # most fields missing — only ticker + spot present
    })
    # Wipe everything except ticker+spot.
    env["state"] = {"ticker": "AAPL", "spot": 184.50}
    snap = _snapshot_from_envelope("AAPL", env)
    assert_eq(snap.ticker, "AAPL", "ticker present")
    assert_eq(snap.spot, 184.50, "spot present")
    assert_is_none(snap.gamma_flip, "missing gamma_flip → None")
    assert_eq(snap.flip_location, "unknown", "missing flip_location → 'unknown'")
    assert_eq(snap.gex_sign, "unknown", "missing gex_sign → 'unknown'")
    assert_eq(snap.fields_lit, 0, "missing fields_lit → 0")
    assert_eq(snap.canonical_status, {}, "missing canonical_status → {}")
    assert_eq(snap.fetch_errors, [], "missing fetch_errors → []")


if __name__ == "__main__":
    test_warming_up_snapshot_has_all_none_fields()
    test_validate_envelope_versions_accepts_current()
    test_validate_envelope_versions_accepts_future_producer_version()
    test_validate_envelope_versions_rejects_old_producer_version()
    test_validate_envelope_versions_rejects_convention_mismatch()
    test_validate_envelope_versions_rejects_missing_versions()
    test_snapshot_from_envelope_full_data()
    test_snapshot_from_envelope_handles_missing_optional_fields()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 2: Run test, verify it fails**

```bash
python3 test_research_data_consumer.py
```

Expected: `ImportError: cannot import name '_warming_up_snapshot' from 'omega_dashboard.research_data'`. All eight tests fail.

### Task 1.2 — Add helpers to `research_data.py`

- [ ] **Step 1: Add `warming_up` field to `TickerSnapshot`**

Find the `TickerSnapshot` dataclass at `omega_dashboard/research_data.py:57-85`. Add `warming_up: bool = False` as a new field (keep it after `error` so default-False doesn't break existing callers that construct snapshots positionally — though all existing constructions use keyword args, so this is defensive).

Replace the dataclass definition with:

```python
@dataclass
class TickerSnapshot:
    """One row in the Research grid, ready for template rendering."""
    ticker: str
    spot: Optional[float]
    gamma_flip: Optional[float]
    distance_from_flip_pct: Optional[float]
    flip_location: str
    # IV state (Patch 11.3.2 — canonical_iv_state)
    atm_iv: Optional[float]
    iv_skew_pp: Optional[float]
    iv30: Optional[float]
    # Dealer Greek aggregates (Patch 11.4 — canonical_exposures)
    gex: Optional[float]
    dex: Optional[float]
    vanna: Optional[float]
    charm: Optional[float]
    gex_sign: str
    # Walls (Patch 11.5 — wired from canonical_exposures, no separate compute)
    call_wall: Optional[float]
    put_wall: Optional[float]
    gamma_wall: Optional[float]
    # Progress
    fields_lit: int
    fields_total: int
    canonical_status: dict
    chain_clean: bool
    fetch_errors: list
    error: Optional[str] = None    # set if build_from_raw failed entirely
    # Patch C: distinguishes "producer hasn't written this ticker yet"
    # (skeleton card, neutral styling) from "build raised an exception"
    # (red error card). Always False on the legacy inline-build path.
    warming_up: bool = False
```

- [ ] **Step 2: Add reader-side constants and helpers**

Add the following block immediately after the `_cache_put` function (around line 127), BEFORE the existing `build_ticker_snapshot` function:

```python
# ─────────────────────────────────────────────────────────────────────
# Patch C: Redis consumer helpers
#
# Reads pre-built BotState envelopes from Redis (written by Patch B's
# bot_state_producer). Validates schema versioning; falls through to a
# warming-up snapshot on any failure mode (missing key, JSON error,
# version mismatch). Gated by RESEARCH_USE_REDIS env var (see
# research_data() below).
# ─────────────────────────────────────────────────────────────────────

# Schema version compat — bump only on TRULY breaking changes.
# Additive producer bumps (new fields, new canonicals) leave this alone.
MIN_COMPATIBLE_PRODUCER_VERSION = 1

# Strict — Patch 9 dealer-side convention. Mismatch = warming-up.
EXPECTED_CONVENTION_VERSION = 2

# Producer's Redis key prefix (must match bot_state_producer.KEY_PREFIX).
KEY_PREFIX = "bot_state:"


def _warming_up_snapshot(ticker: str, reason: str = "warming up") -> "TickerSnapshot":
    """Construct a placeholder snapshot for tickers without (or with
    invalid) Redis data. The template renders these as skeleton cards."""
    return TickerSnapshot(
        ticker=ticker,
        spot=None,
        gamma_flip=None,
        distance_from_flip_pct=None,
        flip_location="unknown",
        atm_iv=None,
        iv_skew_pp=None,
        iv30=None,
        gex=None,
        dex=None,
        vanna=None,
        charm=None,
        gex_sign="unknown",
        call_wall=None,
        put_wall=None,
        gamma_wall=None,
        fields_lit=0,
        fields_total=0,
        canonical_status={},
        chain_clean=False,
        fetch_errors=[],
        error=reason,
        warming_up=True,
    )


def _validate_envelope_versions(envelope: dict, ticker: str) -> Optional[str]:
    """Returns None if the envelope's versions are acceptable, else an
    error string suitable for log + warming-up display.

    Forward compat: producer_version > our expected is accepted.
    Backward incompat: producer_version < MIN_COMPATIBLE is rejected.
    Convention strict: convention_version != EXPECTED_CONVENTION_VERSION
    is rejected (Patch 9 protection).
    """
    pv = envelope.get("producer_version")
    if pv is None:
        return f"{ticker}: envelope missing producer_version"
    if pv < MIN_COMPATIBLE_PRODUCER_VERSION:
        return (f"{ticker}: producer_version {pv} below "
                f"MIN_COMPATIBLE={MIN_COMPATIBLE_PRODUCER_VERSION}")

    cv = envelope.get("convention_version")
    if cv is None:
        return f"{ticker}: envelope missing convention_version"
    if cv != EXPECTED_CONVENTION_VERSION:
        return (f"{ticker}: convention_version {cv} != expected "
                f"{EXPECTED_CONVENTION_VERSION} (Patch 9 protection)")

    return None


def _snapshot_from_envelope(ticker: str, envelope: dict) -> "TickerSnapshot":
    """Convert a validated producer envelope to a TickerSnapshot.

    Caller must have already validated envelope versions — this function
    parses the `state` dict permissively, defaulting missing fields to
    None / "unknown" / 0 / {} / [] as appropriate.
    """
    state = envelope.get("state") or {}
    return TickerSnapshot(
        ticker=ticker,
        spot=state.get("spot"),
        gamma_flip=state.get("gamma_flip"),
        distance_from_flip_pct=state.get("distance_from_flip_pct"),
        flip_location=state.get("flip_location") or "unknown",
        atm_iv=state.get("atm_iv"),
        iv_skew_pp=state.get("iv_skew_pp"),
        iv30=state.get("iv30"),
        gex=state.get("gex"),
        dex=state.get("dex"),
        vanna=state.get("vanna"),
        charm=state.get("charm"),
        gex_sign=state.get("gex_sign") or "unknown",
        call_wall=state.get("call_wall"),
        put_wall=state.get("put_wall"),
        gamma_wall=state.get("gamma_wall"),
        fields_lit=state.get("fields_lit", 0),
        fields_total=state.get("fields_total", 0),
        canonical_status=state.get("canonical_status") or {},
        chain_clean=bool(state.get("chain_clean", False)),
        fetch_errors=state.get("fetch_errors") or [],
        error=None,
        warming_up=False,
    )
```

- [ ] **Step 3: Add `import json` and `import os` to the top of the file**

Find the existing imports at the top of `omega_dashboard/research_data.py`:

```python
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
```

Replace with:

```python
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
python3 test_research_data_consumer.py
```

Expected: `PASSED: ≥17, FAILED: 0` (8 tests, ~17 assertions across them).

- [ ] **Step 5: AST check + regression sanity**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read())"
python3 -c "import ast; ast.parse(open('test_research_data_consumer.py', encoding='utf-8').read())"
```

Expected: silent.

- [ ] **Step 6: Commit**

```bash
git add omega_dashboard/research_data.py test_research_data_consumer.py
git commit -m "$(cat <<'EOF'
Patch C.1: Research consumer helpers — version gates + warming-up

Adds reader-side primitives for the upcoming Redis consumer path:
  - MIN_COMPATIBLE_PRODUCER_VERSION = 1, EXPECTED_CONVENTION_VERSION = 2,
    KEY_PREFIX = "bot_state:" module constants
  - TickerSnapshot.warming_up: bool = False (new field, distinguishes
    "producer hasn't written yet" skeleton from "build raised" error)
  - _warming_up_snapshot(ticker, reason): factory for placeholder rows
  - _validate_envelope_versions(env, ticker): returns None/error-str.
    Forward-compatible on producer_version (newer accepted), strict on
    convention_version (Patch 9 protection)
  - _snapshot_from_envelope(ticker, env): permissive parser for the
    producer's JSON envelope shape

Pure functions, exhaustively unit-tested (8 tests, 17 assertions).
No callers yet; Patch C.2 wires _load_snapshot_from_redis on top.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task C.2 — Consumer entry point

**Why second:** Wraps C.1's helpers with the Redis read + JSON-decode + validate pipeline. Returns a TickerSnapshot for ANY input — happy path, missing key, malformed JSON, version mismatch all fall through to warming-up. Still no caller in the production research_data() flow yet.

**Files:**
- Modify: `omega_dashboard/research_data.py` (add `_load_snapshot_from_redis` and `_research_data_from_redis`)
- Modify: `test_research_data_consumer.py` (append tests)

### Task 2.1 — Failing tests for the consumer

- [ ] **Step 1: Append tests to `test_research_data_consumer.py`**

Add these tests after the existing tests, BEFORE the `if __name__ == "__main__":` block:

```python
# ─────────────────────────────────────────────────────────────────────
# Redis consumer entry point
# ─────────────────────────────────────────────────────────────────────

def test_load_snapshot_returns_warming_up_when_key_missing():
    from omega_dashboard.research_data import _load_snapshot_from_redis
    fake = _FakeRedis()
    snap = _load_snapshot_from_redis("AAPL", "front", redis_client=fake)
    assert_eq(snap.ticker, "AAPL", "ticker preserved")
    assert_eq(snap.warming_up, True, "missing key → warming up")
    assert_eq(snap.error, "missing key", "reason set")


def test_load_snapshot_returns_warming_up_when_redis_is_none():
    from omega_dashboard.research_data import _load_snapshot_from_redis
    snap = _load_snapshot_from_redis("AAPL", "front", redis_client=None)
    assert_eq(snap.warming_up, True, "no redis client → warming up")
    assert_eq(snap.error, "redis unavailable", "reason set")


def test_load_snapshot_parses_valid_envelope():
    from omega_dashboard.research_data import _load_snapshot_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    fake.set(f"{KEY_PREFIX}AAPL:front", json.dumps(_make_envelope()))
    snap = _load_snapshot_from_redis("AAPL", "front", redis_client=fake)
    assert_eq(snap.warming_up, False, "valid envelope → not warming up")
    assert_eq(snap.spot, 184.50, "spot parsed")
    assert_eq(snap.gex, 12345678.0, "gex parsed")


def test_load_snapshot_warming_up_on_malformed_json():
    from omega_dashboard.research_data import _load_snapshot_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    fake.set(f"{KEY_PREFIX}AAPL:front", "not-valid-json{{{")
    snap = _load_snapshot_from_redis("AAPL", "front", redis_client=fake)
    assert_eq(snap.warming_up, True, "malformed JSON → warming up")


def test_load_snapshot_warming_up_on_version_mismatch():
    """convention_version=1 (Patch 9 mismatch) → warming up, not silent accept."""
    from omega_dashboard.research_data import _load_snapshot_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    fake.set(f"{KEY_PREFIX}AAPL:front",
             json.dumps(_make_envelope(convention_version=1)))
    snap = _load_snapshot_from_redis("AAPL", "front", redis_client=fake)
    assert_eq(snap.warming_up, True, "convention_version mismatch → warming up")
    assert_true("convention_version" in (snap.error or ""),
                "error explains the mismatch")


def test_load_snapshot_accepts_future_producer_version():
    """producer_version=99 (newer than reader expects) is accepted."""
    from omega_dashboard.research_data import _load_snapshot_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    fake.set(f"{KEY_PREFIX}AAPL:front",
             json.dumps(_make_envelope(producer_version=99)))
    snap = _load_snapshot_from_redis("AAPL", "front", redis_client=fake)
    assert_eq(snap.warming_up, False,
              "future producer_version accepted (forward compat)")
    assert_eq(snap.spot, 184.50, "data still parsed")


# ─────────────────────────────────────────────────────────────────────
# Universe-level consumer
# ─────────────────────────────────────────────────────────────────────

def test_research_data_from_redis_mixed_population():
    """SPY populated, QQQ missing → SPY card lit, QQQ shows warming-up."""
    from omega_dashboard.research_data import _research_data_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    spy_env = _make_envelope(state_overrides={"ticker": "SPY", "spot": 590.00})
    fake.set(f"{KEY_PREFIX}SPY:front", json.dumps(spy_env))
    # No key for QQQ.

    payload = _research_data_from_redis(
        tickers=["SPY", "QQQ"],
        intent="front",
        redis_client=fake,
    )
    assert_eq(payload.tickers_total, 2, "two tickers requested")
    assert_eq(len(payload.snapshots), 2, "two snapshots returned")
    by_ticker = {s.ticker: s for s in payload.snapshots}
    assert_eq(by_ticker["SPY"].warming_up, False, "SPY not warming up")
    assert_eq(by_ticker["SPY"].spot, 590.00, "SPY spot from envelope")
    assert_eq(by_ticker["QQQ"].warming_up, True, "QQQ warming up (no key)")


def test_research_data_from_redis_aggregates_correctly():
    """fields_lit_avg should reflect ONLY tickers with data, not warming-up ones."""
    from omega_dashboard.research_data import _research_data_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    fake.set(f"{KEY_PREFIX}SPY:front",
             json.dumps(_make_envelope(state_overrides={
                 "ticker": "SPY", "spot": 590.00, "fields_lit": 22, "fields_total": 64,
             })))
    payload = _research_data_from_redis(
        tickers=["SPY", "QQQ"],
        intent="front",
        redis_client=fake,
    )
    assert_eq(payload.tickers_with_data, 1, "1 of 2 has data")
    assert_eq(payload.fields_total, 64, "fields_total from populated ticker")
    assert_eq(payload.fields_lit_avg, 22.0, "avg = 22 (only counting populated)")


def test_research_data_from_redis_returns_unavailable_when_no_redis():
    from omega_dashboard.research_data import _research_data_from_redis
    payload = _research_data_from_redis(
        tickers=["SPY"],
        intent="front",
        redis_client=None,
    )
    assert_eq(payload.available, False, "no redis → available=False")
    assert_true("redis" in (payload.error or "").lower(), "error mentions redis")
```

Update the `if __name__ == "__main__":` block to include the new tests:

```python
if __name__ == "__main__":
    # warming-up factory
    test_warming_up_snapshot_has_all_none_fields()
    # version validation
    test_validate_envelope_versions_accepts_current()
    test_validate_envelope_versions_accepts_future_producer_version()
    test_validate_envelope_versions_rejects_old_producer_version()
    test_validate_envelope_versions_rejects_convention_mismatch()
    test_validate_envelope_versions_rejects_missing_versions()
    # envelope → snapshot
    test_snapshot_from_envelope_full_data()
    test_snapshot_from_envelope_handles_missing_optional_fields()
    # _load_snapshot_from_redis
    test_load_snapshot_returns_warming_up_when_key_missing()
    test_load_snapshot_returns_warming_up_when_redis_is_none()
    test_load_snapshot_parses_valid_envelope()
    test_load_snapshot_warming_up_on_malformed_json()
    test_load_snapshot_warming_up_on_version_mismatch()
    test_load_snapshot_accepts_future_producer_version()
    # _research_data_from_redis
    test_research_data_from_redis_mixed_population()
    test_research_data_from_redis_aggregates_correctly()
    test_research_data_from_redis_returns_unavailable_when_no_redis()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 2: Run tests, verify the new ones fail**

```bash
python3 test_research_data_consumer.py
```

Expected: prior tests pass; new tests fail with `ImportError: cannot import name '_load_snapshot_from_redis' / '_research_data_from_redis'`.

### Task 2.2 — Implement the consumer

- [ ] **Step 1: Append `_load_snapshot_from_redis` to `omega_dashboard/research_data.py`**

Add immediately after `_snapshot_from_envelope` (the end of the C.1 helper block):

```python
def _load_snapshot_from_redis(
    ticker: str,
    intent: str,
    *,
    redis_client,
) -> "TickerSnapshot":
    """GET bot_state:{ticker}:{intent}, decode + validate, return a
    TickerSnapshot. Any failure mode (no Redis, missing key, malformed
    JSON, version mismatch) returns a warming-up snapshot with the
    failure reason set as snap.error.
    """
    if redis_client is None:
        return _warming_up_snapshot(ticker, reason="redis unavailable")

    key = f"{KEY_PREFIX}{ticker}:{intent}"
    try:
        raw = redis_client.get(key)
    except Exception as e:
        log.warning(f"research_data: redis GET {key} failed: {e}")
        return _warming_up_snapshot(ticker, reason="redis error")

    if raw is None:
        return _warming_up_snapshot(ticker, reason="missing key")

    try:
        envelope = json.loads(raw)
    except Exception as e:
        log.warning(f"research_data: malformed envelope for {ticker}: {e}")
        return _warming_up_snapshot(ticker, reason="malformed envelope")

    err = _validate_envelope_versions(envelope, ticker)
    if err is not None:
        log.warning(f"research_data: {err}")
        return _warming_up_snapshot(ticker, reason=err)

    try:
        return _snapshot_from_envelope(ticker, envelope)
    except Exception as e:
        log.warning(f"research_data: snapshot construction failed for {ticker}: {e}")
        return _warming_up_snapshot(ticker, reason=f"parse error: {e}")
```

- [ ] **Step 2: Append `_research_data_from_redis` after `_load_snapshot_from_redis`**

```python
def _research_data_from_redis(
    tickers: list,
    intent: str,
    *,
    redis_client,
) -> "ResearchData":
    """Build the full Research page payload by reading each ticker's
    envelope from Redis. Tickers without populated keys render as
    warming-up. Returns ResearchData ready for the template."""
    if redis_client is None:
        return ResearchData(
            fetched_at_utc=datetime.now(timezone.utc),
            tickers_total=len(tickers),
            tickers_with_data=0,
            tickers_errored=0,
            fields_lit_avg=0.0,
            fields_total=0,
            canonical_status_summary={},
            snapshots=[],
            available=False,
            error="redis client not available",
        )

    snapshots = [
        _load_snapshot_from_redis(t, intent, redis_client=redis_client)
        for t in tickers
    ]

    # Aggregate metrics — count "with_data" as not-warming-up, not-errored.
    with_data = sum(1 for s in snapshots if not s.warming_up and s.error is None)
    errored = sum(1 for s in snapshots if s.error is not None and not s.warming_up)
    fields_lit_avg = (
        sum(s.fields_lit for s in snapshots if not s.warming_up) / max(with_data, 1)
        if with_data > 0 else 0.0
    )
    fields_total = next((s.fields_total for s in snapshots if s.fields_total > 0), 0)

    status_summary: dict = {}
    for s in snapshots:
        for cname, cstatus in s.canonical_status.items():
            if cname not in status_summary:
                status_summary[cname] = {"live": 0, "stub": 0, "error": 0}
            if cstatus == "live":
                status_summary[cname]["live"] += 1
            elif cstatus.startswith("stub"):
                status_summary[cname]["stub"] += 1
            else:
                status_summary[cname]["error"] += 1

    return ResearchData(
        fetched_at_utc=datetime.now(timezone.utc),
        tickers_total=len(tickers),
        tickers_with_data=with_data,
        tickers_errored=errored,
        fields_lit_avg=fields_lit_avg,
        fields_total=fields_total,
        canonical_status_summary=status_summary,
        snapshots=snapshots,
        available=True,
        error=None,
    )
```

- [ ] **Step 3: Run tests, verify they pass**

```bash
python3 test_research_data_consumer.py
```

Expected: `PASSED: ≥30, FAILED: 0`.

- [ ] **Step 4: AST check**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read())"
```

- [ ] **Step 5: Regression sanity — neither consumer path is wired yet, so legacy tests should pass unchanged**

```bash
python3 test_canonical_expiration.py
python3 test_prev_close_store.py
```

Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add omega_dashboard/research_data.py test_research_data_consumer.py
git commit -m "$(cat <<'EOF'
Patch C.2: Research consumer entry point — Redis read + parse pipeline

_load_snapshot_from_redis(ticker, intent, *, redis_client) does the
full pipeline: GET, json.loads, validate versions, build snapshot.
Every failure mode returns warming-up with the reason logged at
warning level. No exception ever propagates to the caller.

_research_data_from_redis(tickers, intent, *, redis_client) is the
universe-level wrapper: walks the ticker list, builds ResearchData
including aggregate metrics. Tickers without populated keys count
toward tickers_total but not tickers_with_data.

No caller in the production research_data() flow yet; Patch C.3 adds
the env-var-gated dispatch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task C.3 — Wire into `research_data()` with env-var gate

**Why third:** Now we activate the consumer in production. Behavior is gated by `RESEARCH_USE_REDIS` (default off), so the deploy is no-op until Brad flips the flag. Legacy inline path stays intact for rollback.

**Files:**
- Modify: `omega_dashboard/research_data.py` (`research_data()` dispatch, ~10 lines)
- Modify: `test_research_data_consumer.py` (env-gating tests)

### Task 3.1 — Failing test for the env-var dispatch

- [ ] **Step 1: Append the env-gating test**

Add to `test_research_data_consumer.py`, before the `if __name__ == "__main__":` block:

```python
# ─────────────────────────────────────────────────────────────────────
# Env-var dispatch in research_data()
# ─────────────────────────────────────────────────────────────────────

def test_research_data_uses_redis_when_env_var_on():
    """RESEARCH_USE_REDIS=1 → dispatch to consumer path; data_router ignored."""
    from omega_dashboard.research_data import research_data, KEY_PREFIX
    os.environ["RESEARCH_USE_REDIS"] = "1"
    fake = _FakeRedis()
    fake.set(f"{KEY_PREFIX}SPY:front",
             json.dumps(_make_envelope(state_overrides={
                 "ticker": "SPY", "spot": 590.00,
             })))
    payload = research_data(
        tickers=["SPY"],
        intent="front",
        data_router=None,           # would normally cause unavailable
        redis_client=fake,          # but Redis path takes over
    )
    assert_eq(payload.available, True, "Redis path returns available=True")
    assert_eq(len(payload.snapshots), 1, "one snapshot")
    assert_eq(payload.snapshots[0].spot, 590.00, "spot from envelope")


def test_research_data_uses_legacy_when_env_var_off():
    """RESEARCH_USE_REDIS=0 → legacy path; redis_client ignored."""
    from omega_dashboard.research_data import research_data
    os.environ["RESEARCH_USE_REDIS"] = "0"
    payload = research_data(
        tickers=["SPY"],
        intent="front",
        data_router=None,           # legacy path returns unavailable
        redis_client=_FakeRedis(),  # ignored in legacy mode
    )
    assert_eq(payload.available, False,
              "legacy path with no data_router returns unavailable")
```

Update `__main__` to call them:

```python
    # Env-var dispatch
    test_research_data_uses_redis_when_env_var_on()
    test_research_data_uses_legacy_when_env_var_off()
```

- [ ] **Step 2: Run tests, verify the new ones fail**

```bash
python3 test_research_data_consumer.py
```

Expected: prior tests pass; new tests fail because `research_data()` doesn't yet take a `redis_client` parameter or check the env var.

### Task 3.2 — Wire the dispatch

- [ ] **Step 1: Add `_env_bool` helper near the top of `research_data.py`**

Add immediately after the imports + `log = logging.getLogger(__name__)` block (around line 35):

```python
def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var. Truthy: 1/true/yes/on (case-insensitive).
    Anything else (or unset) → default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
```

- [ ] **Step 2: Update the `research_data()` signature and add the dispatch**

Find the existing `research_data()` function (around line 224). Replace it with:

```python
def research_data(
    tickers: Optional[list] = None,
    intent: str = "front",
    *,
    data_router=None,
    redis_client=None,
) -> ResearchData:
    """Build the full Research page payload.

    When RESEARCH_USE_REDIS=1, reads pre-built envelopes from Redis
    (Patch C consumer path). Otherwise builds inline via DataRouter +
    canonical_expiration + BotState.build (legacy v11 path).

    Args:
        tickers:      list of tickers to include; defaults to DEFAULT_TICKERS
        intent:       canonical_expiration intent for chain selection. Default
                      'front' = first non-0-DTE expiration per ticker. Other
                      valid values: 't7', 't30', 't60'.
        data_router:  required for legacy path. Ignored when RESEARCH_USE_REDIS=1.
        redis_client: required for consumer path. Ignored when env var is off.

    Returns:
        ResearchData ready for the template.
    """
    if tickers is None:
        tickers = list(DEFAULT_TICKERS)

    # Patch C: env-var-gated dispatch. Default off → legacy inline path
    # runs unchanged. Set to 1/true to activate the Redis consumer.
    if _env_bool("RESEARCH_USE_REDIS", default=False):
        return _research_data_from_redis(
            tickers=tickers,
            intent=intent,
            redis_client=redis_client,
        )

    # Legacy inline-build path (unchanged from v11.6 / Patch A).
    if data_router is None:
        return ResearchData(
            fetched_at_utc=datetime.now(timezone.utc),
            tickers_total=len(tickers),
            tickers_with_data=0,
            tickers_errored=0,
            fields_lit_avg=0.0,
            fields_total=0,
            canonical_status_summary={},
            snapshots=[],
            available=False,
            error="data_router not configured (Research page needs DataRouter)",
        )

    snapshots = []
    for t in tickers:
        snap = build_ticker_snapshot(t, intent, data_router=data_router)
        snapshots.append(snap)

    with_data = sum(1 for s in snapshots if s.error is None and s.spot)
    errored = sum(1 for s in snapshots if s.error is not None)
    fields_lit_avg = (
        sum(s.fields_lit for s in snapshots if s.error is None) / max(with_data, 1)
        if with_data > 0 else 0.0
    )
    fields_total = next((s.fields_total for s in snapshots if s.fields_total > 0), 0)

    status_summary: dict = {}
    for s in snapshots:
        for cname, cstatus in s.canonical_status.items():
            if cname not in status_summary:
                status_summary[cname] = {"live": 0, "stub": 0, "error": 0}
            if cstatus == "live":
                status_summary[cname]["live"] += 1
            elif cstatus.startswith("stub"):
                status_summary[cname]["stub"] += 1
            else:
                status_summary[cname]["error"] += 1

    return ResearchData(
        fetched_at_utc=datetime.now(timezone.utc),
        tickers_total=len(tickers),
        tickers_with_data=with_data,
        tickers_errored=errored,
        fields_lit_avg=fields_lit_avg,
        fields_total=fields_total,
        canonical_status_summary=status_summary,
        snapshots=snapshots,
        available=True,
        error=None,
    )
```

- [ ] **Step 3: Run tests**

```bash
python3 test_research_data_consumer.py
```

Expected: `PASSED: ≥34, FAILED: 0`.

- [ ] **Step 4: AST check**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read())"
```

- [ ] **Step 5: Commit**

```bash
git add omega_dashboard/research_data.py test_research_data_consumer.py
git commit -m "$(cat <<'EOF'
Patch C.3: env-gated dispatch — research_data() routes to Redis or legacy

research_data() gains a redis_client kwarg and an _env_bool gate on
RESEARCH_USE_REDIS (default off). Set the env var to 1/true and the
consumer path runs; data_router is ignored. Default off → legacy
inline-build path runs unchanged for clean rollback.

Two new tests verify the dispatch in both directions. No production
caller has changed — Patch C.4 wires routes.py to pass _get_redis().

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task C.4 — Route wiring + template + CLAUDE.md

**Why last:** With the data layer fully tested, this task connects it to the production HTTP path and updates docs. Small, focused changes.

**Files:**
- Modify: `omega_dashboard/routes.py` (~6 lines, two call sites)
- Modify: `omega_dashboard/templates/dashboard/research.html` (~15 lines, warming-up branch)
- Modify: `omega_dashboard/static/omega.css` (~20 lines, `.research-card-warming-up` styling)
- Modify: `CLAUDE.md` (~25 lines, vocab + decision)

### Task 4.1 — Wire `redis_client` into both research routes

- [ ] **Step 1: Locate the two call sites**

```bash
grep -n "rd.research_data" omega_dashboard/routes.py
```

Expected output: two matches (page route + JSON feed). The exact line numbers may have shifted since Patch A; use grep to find them.

- [ ] **Step 2: Update both call sites**

For EACH match, change the surrounding two lines from:

```python
    data_router = _get_bot_data_router()
    payload = rd.research_data(data_router=data_router)
```

to:

```python
    data_router = _get_bot_data_router()
    # Patch C: pass the Redis client so the consumer path is reachable.
    # When RESEARCH_USE_REDIS env var is off, redis_client is ignored.
    from app import _get_redis
    payload = rd.research_data(
        data_router=data_router,
        redis_client=_get_redis(),
    )
```

- [ ] **Step 3: AST check**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/routes.py', encoding='utf-8').read())"
```

### Task 4.2 — Update the template for warming-up rendering

- [ ] **Step 1: Locate the per-card render block**

```bash
grep -n 'snap.error' omega_dashboard/templates/dashboard/research.html
```

Expected: two matches (the `{% if snap.error %}` guard around the error message and the `research-card-error` CSS class on the outer div).

- [ ] **Step 2: Differentiate warming-up from error in the outer card class**

Find the line that reads:

```html
      <div class="research-card {% if snap.error %}research-card-error{% endif %}">
```

Replace with:

```html
      <div class="research-card
                  {% if snap.warming_up %}research-card-warming-up
                  {% elif snap.error %}research-card-error{% endif %}">
```

- [ ] **Step 3: Differentiate the inner status block**

Find the inner block that renders the error message:

```html
        {% if snap.error %}
          <div class="research-card-error">
            {{ snap.error }}
          </div>
        {% else %}
```

Replace with:

```html
        {% if snap.warming_up %}
          <div class="research-card-warming-up-msg">
            <span class="muted">Warming up &middot;</span> {{ snap.error }}
          </div>
        {% elif snap.error %}
          <div class="research-card-error">
            {{ snap.error }}
          </div>
        {% else %}
```

### Task 4.3 — Add CSS for warming-up cards

- [ ] **Step 1: Append the warming-up styles to `omega_dashboard/static/omega.css`**

```css
/* ─── Patch C: Research warming-up state ─── */
/* Distinguishes "producer hasn't written this ticker yet" (neutral
   skeleton) from "build raised an exception" (existing red card). */
.research-card-warming-up {
  opacity: 0.55;
  border-style: dashed;
  animation: research-warming-pulse 2.4s ease-in-out infinite;
}

.research-card-warming-up-msg {
  font-size: 0.85em;
  padding: 0.6em 0.8em;
  color: var(--research-warming-color, #888);
  font-style: italic;
}

@keyframes research-warming-pulse {
  0%, 100% { opacity: 0.55; }
  50%      { opacity: 0.75; }
}
```

### Task 4.4 — CLAUDE.md update

- [ ] **Step 1: Add to "Project vocabulary"**

Find the existing `bot_state_producer` vocabulary entry (Patch B added it earlier). Add this new entry IMMEDIATELY AFTER it:

```markdown
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
```

- [ ] **Step 2: Add to "Decisions already made"**

Append to the end of that section:

```markdown
- Research page reads from Redis when `RESEARCH_USE_REDIS=1` (Patch C).
  Cold-start dashboard load goes from ~3 minutes (inline build per
  request) to <1 second (35 Redis GETs of pre-built envelopes). The
  inline path stays in `research_data.py` for rollback; default off
  on first ship. Consumer is permissive: missing keys, malformed JSON,
  version mismatches all render as "warming up" skeleton cards (CSS
  class `research-card-warming-up`) — the dashboard never errors out
  whole-page on a single bad ticker.
- Schema versioning split: `producer_version` is forward-compatible
  (newer producer accepted by older reader as long as MIN_COMPATIBLE
  is met). `convention_version` is strict-equal — mismatch is treated
  as "warming up" rather than rendered, to prevent ever displaying
  dealer-side-flipped numbers post a Patch 9-style convention shift.
```

### Task 4.5 — Final regression sweep + deploy notes

- [ ] **Step 1: Run all relevant test suites**

```bash
python3 test_research_data_consumer.py
python3 test_bot_state_producer.py
python3 test_canonical_expiration.py
python3 test_prev_close_store.py
python3 test_spot_prices_streaming.py
python3 test_canonical_gamma_flip.py
python3 test_canonical_iv_state.py
python3 test_canonical_exposures.py
python3 test_bot_state.py
```

Expected: all clean. Each suite's PASSED count should match its prior baseline plus the new C.x tests in `test_research_data_consumer.py`.

- [ ] **Step 2: AST-check every modified Python file**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read())"
python3 -c "import ast; ast.parse(open('omega_dashboard/routes.py', encoding='utf-8').read())"
```

- [ ] **Step 3: Commit**

```bash
git add omega_dashboard/routes.py omega_dashboard/templates/dashboard/research.html omega_dashboard/static/omega.css CLAUDE.md
git commit -m "$(cat <<'EOF'
Patch C.4: route wiring + warming-up template + CLAUDE.md

routes.py: both Research routes (page + JSON feed) now pass
redis_client=_get_redis() to research_data(). When RESEARCH_USE_REDIS
is off, redis_client is ignored — same call pattern, no behavior change.

research.html: distinct rendering for warming_up=True vs error. Warming-up
cards get a dashed-border + opacity-pulse skeleton (CSS class
.research-card-warming-up); errored cards stay red as before. Inner
status block prefixes warming-up reason with a muted "Warming up ·"
label so users can tell at a glance.

omega.css: new .research-card-warming-up + .research-card-warming-up-msg
classes plus a 2.4s pulse keyframe.

CLAUDE.md: vocab entries for RESEARCH_USE_REDIS,
MIN_COMPATIBLE_PRODUCER_VERSION, EXPECTED_CONVENTION_VERSION; decision
entries documenting the Redis-consumer rollback path and the
forward/backward versioning split.

Patch C is now live behind the env flag. Default deploy is no-op:
RESEARCH_USE_REDIS unset → legacy inline path runs unchanged. Brad
flips the env var when ready and the dashboard goes from 3-minute
cold-start spin to <1s.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Deployment & cutover

After all four patches (C.1–C.4) are committed and pushed:

1. **Deploy with flag off.** Render auto-rebuild picks up the code; behavior is unchanged because `RESEARCH_USE_REDIS` is unset. The Research page still does the inline 3-minute cold-start spin.

2. **Verify Patch B is producing.** Before flipping the consumer, confirm Redis has populated keys. From Render shell:
   ```bash
   redis-cli -u $REDIS_URL KEYS 'bot_state:*:front' | wc -l
   ```
   Expected: 35 (one per ticker in `BOT_STATE_PRODUCER_TICKERS`). If less, the producer hasn't run a full Tier A pass yet — wait 60s and recheck.

3. **Flip the env var.** In Render → Environment, set `RESEARCH_USE_REDIS=true`. Trigger a manual redeploy.

4. **Smoke-test the Research page.** Hit `/research` in your browser. The page should render in <1s. Look at the per-ticker cards:
   - Cards with all data populated → producer wrote them, reader parsed them. ✓
   - Cards with the dashed-border "Warming up · ..." style → producer hasn't written that ticker yet (or version mismatch). Should be zero on a healthy deploy.

5. **Verify in logs:** the Research page no longer logs `build_ticker_snapshot {ticker}: ...` entries (those came from the inline path). Instead you should see ONLY the producer's `[bsp tier=A]` and `[bsp tier=B]` lines.

6. **Rollback path:** unset `RESEARCH_USE_REDIS` in Render env, redeploy. Within 60s the Research page is back on the legacy inline path. The producer keeps running independently — no data loss.

# Follow-ups (NOT in this plan)

- **Patch D — Multi-DTE drilldown UI.** Click-to-expand front/t7/t30/t60 walls per card. Producer is already writing all four intents (Tier C is populated as of today's deploy), so the data is there.
- **Cadence re-tier patch** — driven by the 24h timing analysis from `bot_state_producer:timings:{YYYYMMDD}` sorted set.
- **Delete the legacy inline path.** After 7 trading days of clean Redis-consumer traffic, follow-up patch removes `build_ticker_snapshot()` and the `data_router` parameter from `research_data()`. CLAUDE.md gets updated to reflect Redis-consumer as the only path.
- **Patch E — silent thesis migration.** Bigger change; the silent thesis still computes BotState inline. Migrating it to read from Redis is the strategic move that makes the canonical rebuild "real" instead of parallel infrastructure.
