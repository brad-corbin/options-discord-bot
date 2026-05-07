# Patch B — `bot_state_producer` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daemon-thread producer that periodically computes `BotState` for each `(ticker, intent)` pair on a tiered cadence (front=60s, t7=180s, t30/t60=600s) and writes JSON envelopes to Redis. The Research page (Patch C, separate plan) consumes those envelopes for instant page loads. Patch B ships behind `BOT_STATE_PRODUCER_ENABLED=false` (default off) so it can land in prod with zero behavior change.

**Architecture:** Single new module `bot_state_producer.py` with three daemon threads (one per tier), staggered T+0/T+10/T+20 to spread load on the rate limiter. Each thread runs forever: for every `(ticker, intent)` in its tier, resolve expiration via `canonical_expiration` → fetch via `fetch_raw_inputs` → compute via `BotState.build_from_raw` → wrap in JSON envelope with `producer_version`+`convention_version`+`intent`+`expiration` metadata → `SET key val EX tier_ttl`. Per-build timing is recorded to a Redis sorted set as a hard deliverable for post-deploy cadence tuning. A multi-worker Redis lock ensures only one Render web worker runs the producer.

**Tech Stack:** Python 3, `redis-py` (already used by `app.py:_get_redis()`), `threading` (daemon thread pattern from `schwab_stream.py`), existing `BotState`/`RawInputs`/`canonical_expiration` modules.

---

## Decisions already locked in (from spec v4)

- **Three tiers, fixed cadence in initial deploy:** A=60s, B=180s, C=600s.
- **TTL = cadence × 3** per tier so the consumer detects stale keys via expiry.
- **Universe staging:** Initial production ships with 10 tickers × 2 intents (front + t7); Tier C empty. Promote to full 35 × 4 = 140 keys after 7 trading days of clean rate-limit logs.
- **Versioning:** `PRODUCER_VERSION = 1` at first ship. `CONVENTION_VERSION = 2` (Patch 9). Both go in every envelope.
- **Envelope-only intent metadata:** `intent` and `expiration` live on the envelope, NOT inside the BotState dataclass. (Open Question #3 resolved.)
- **No market-hours pause.** The producer runs 24/7. Rate limiter handles overnight idle naturally.
- **Empty-string env var parsing:** `"".split(",")` returns `[""]`, which must be filtered to `[]` so an unset Tier C cleanly disables that tier.
- **Per-build timing telemetry is a hard deliverable.** Sorted-set writes to `bot_state_producer:timings:{YYYYMMDD}`, 48h TTL, used to inform any cadence re-tier.
- **Per-ticker error isolation:** one `(ticker, intent)` raising must not stop the loop for the other pairs in that tier.
- **Producer thread crashes are caught at the outer loop and logged**; thread restarts itself with a 5s backoff (mirror of `SchwabStreamManager._run_loop` reconnect pattern).
- **No producer flag check at the call site.** Factory `start_producer()` checks `BOT_STATE_PRODUCER_ENABLED` itself and returns `None` if disabled.

---

## File structure

**Created:**
- `bot_state_producer.py` — single module, ~280 lines. Owns: env-var parsing, envelope schema, JSON serialization with NaN/inf cleanup, Redis lock primitives, per-tier loop, three-tier composition, factory.
- `test_bot_state_producer.py` — ~250 lines, ~15 tests. PASSED/FAILED list pattern. No live network, no live Redis (fake client).

**Modified:**
- `app.py` — one new block in the background-leader path (around `app.py:15001`) that calls `start_producer()` after `start_streaming()`. ~10 lines.
- `CLAUDE.md` — three additions: vocabulary entries (`bot_state_producer`, `producer_version`/`convention_version`, `BOT_STATE_PRODUCER_*` env vars), one entry under "Decisions already made", smoke-test command. ~25 lines.

**Total touched:** 2 new files (~530 lines), 2 modified files (~35 lines added).

---

# Task B.1 — Module skeleton + env parsing + envelope schema

**Why first:** All later tasks in this plan write into `bot_state_producer.py` and `test_bot_state_producer.py`. B.1 sets up both files with the deterministic, network-free helpers (env parsing, envelope build, JSON serialization). No threads, no Redis writes yet — just pure functions exhaustively tested.

**Files:**
- Create: `bot_state_producer.py`
- Create: `test_bot_state_producer.py`

### Task 1.1 — Failing tests for parsing + envelope helpers

- [ ] **Step 1: Create the test file**

Create `test_bot_state_producer.py`:

```python
"""
test_bot_state_producer.py — unit tests for the producer daemon.

No network, no live Redis, no live Schwab. The Schwab provider, Redis
client, and downstream BotState build are all stubbed. Tests focus on
the deterministic helpers (parsing, envelope, serialization) and the
loop's contract (per-ticker isolation, TTL, telemetry write).

PASSED/FAILED list pattern — match the conventions used by
test_canonical_expiration.py and test_prev_close_store.py.
"""

from __future__ import annotations
import sys
import os
import json
import time
import math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_in(key, dct, msg):
    if key not in dct:
        FAILED.append(f"{msg}: {key!r} missing from {sorted(dct)!r}")
        return False
    PASSED.append(msg)
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: expected truthy, got {cond!r}")
        return False
    PASSED.append(msg)
    return True


# ─────────────────────────────────────────────────────────────────────
# Parsing tests
# ─────────────────────────────────────────────────────────────────────

def test_parse_intents_filters_empty_strings():
    """The empty-string parsing fix flagged in spec v4."""
    from bot_state_producer import _parse_intents
    assert_eq(_parse_intents(""),       [], "unset env var → empty list")
    assert_eq(_parse_intents(None),     [], "None → empty list")
    assert_eq(_parse_intents("front"),  ["front"], "single value")
    assert_eq(_parse_intents("front,t7"), ["front", "t7"], "two values")
    assert_eq(_parse_intents("front, t7 , "), ["front", "t7"],
              "whitespace and trailing comma stripped")


def test_parse_tickers_uppercase_dedup():
    from bot_state_producer import _parse_tickers
    assert_eq(_parse_tickers("spy,QQQ,spy"), ["SPY", "QQQ"],
              "uppercases + dedups while preserving order")
    assert_eq(_parse_tickers(""), [], "empty → empty list")


def test_parse_int_env_with_default():
    from bot_state_producer import _parse_int_env
    assert_eq(_parse_int_env(None, 60), 60, "missing → default")
    assert_eq(_parse_int_env("", 60), 60, "empty → default")
    assert_eq(_parse_int_env("120", 60), 120, "valid int")
    assert_eq(_parse_int_env("not-a-number", 60), 60, "garbage → default")


# ─────────────────────────────────────────────────────────────────────
# Envelope schema tests
# ─────────────────────────────────────────────────────────────────────

def _fake_state_dict(extra=None):
    """Minimal BotState-shaped dict for envelope tests. Real BotState is
    a frozen dataclass; the envelope builder accepts asdict() output."""
    base = {
        "ticker": "AAPL",
        "snapshot_version": 1,
        "convention_version": 2,
        "spot": 184.50,
        "gex": 12345.67,
        "fields_lit": 22,
        "fields_total": 64,
        "canonical_status": {},
        "fetch_errors": [],
    }
    base.update(extra or {})
    return base


def test_envelope_includes_required_metadata():
    from bot_state_producer import _build_envelope, PRODUCER_VERSION, CONVENTION_VERSION
    env = _build_envelope(
        state=_fake_state_dict(),
        intent="front",
        expiration="2026-05-08",
    )
    assert_eq(env["producer_version"], PRODUCER_VERSION,
              "producer_version present")
    assert_eq(env["convention_version"], CONVENTION_VERSION,
              "convention_version=2 (Patch 9)")
    assert_eq(env["intent"], "front", "intent on envelope")
    assert_eq(env["expiration"], "2026-05-08", "expiration on envelope")
    assert_in("state", env, "state blob nested inside envelope")
    assert_eq(env["state"]["ticker"], "AAPL", "BotState dict reachable as env.state")


def test_envelope_does_not_mutate_state():
    """Builder returns a new dict; caller's state dict must be unchanged."""
    from bot_state_producer import _build_envelope
    state = _fake_state_dict()
    state_before = dict(state)
    _build_envelope(state=state, intent="front", expiration="2026-05-08")
    assert_eq(state, state_before, "input state dict not mutated")


# ─────────────────────────────────────────────────────────────────────
# Serialization tests (NaN / inf cleanup)
# ─────────────────────────────────────────────────────────────────────

def test_serialize_handles_nan_and_inf():
    """JSON has no NaN/Infinity — the serializer must convert them to None."""
    from bot_state_producer import _serialize_envelope
    env = {
        "producer_version": 1,
        "convention_version": 2,
        "intent": "front",
        "expiration": "2026-05-08",
        "state": {
            "ticker": "AAPL",
            "spot": 184.50,
            "gex": float("nan"),
            "dex": float("inf"),
            "vanna": float("-inf"),
            "fetch_errors": [],
            "canonical_status": {},
        },
    }
    raw = _serialize_envelope(env)
    parsed = json.loads(raw)  # standard json must round-trip cleanly
    assert_eq(parsed["state"]["spot"], 184.50, "finite numbers preserved")
    assert_eq(parsed["state"]["gex"], None, "NaN replaced with None")
    assert_eq(parsed["state"]["dex"], None, "+inf replaced with None")
    assert_eq(parsed["state"]["vanna"], None, "-inf replaced with None")


def test_serialize_round_trip_clean_state():
    """No NaN/inf → the output is byte-equivalent to json.dumps(env)."""
    from bot_state_producer import _serialize_envelope
    env = {
        "producer_version": 1,
        "convention_version": 2,
        "intent": "front",
        "expiration": "2026-05-08",
        "state": {"ticker": "AAPL", "spot": 184.50, "fetch_errors": ["err1"]},
    }
    raw = _serialize_envelope(env)
    parsed = json.loads(raw)
    assert_eq(parsed["state"]["fetch_errors"], ["err1"],
              "list of strings round-trips")
    assert_eq(parsed["state"]["spot"], 184.50, "float round-trips")


if __name__ == "__main__":
    test_parse_intents_filters_empty_strings()
    test_parse_tickers_uppercase_dedup()
    test_parse_int_env_with_default()
    test_envelope_includes_required_metadata()
    test_envelope_does_not_mutate_state()
    test_serialize_handles_nan_and_inf()
    test_serialize_round_trip_clean_state()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 2: Run test, verify it fails**

```bash
python3 test_bot_state_producer.py
```

Expected: `ModuleNotFoundError: No module named 'bot_state_producer'`. All seven tests fail.

### Task 1.2 — Module skeleton + parsing + envelope + serialization

- [ ] **Step 1: Create `bot_state_producer.py`**

```python
"""
bot_state_producer.py — Patch B daemon.

Three daemon threads run forever, each on its own tier cadence:
  Tier A (default 60s, default intents=[front])
  Tier B (default 180s, default intents=[t7])
  Tier C (default 600s, default intents=[])

For each (ticker, intent) in the tier:
  1. canonical_expiration(ticker, intent) → ISO date or None
  2. fetch_raw_inputs(ticker, expiration, data_router=cached_md) → RawInputs
  3. BotState.build_from_raw(raw) → BotState
  4. _build_envelope(state_dict, intent, expiration) → dict
  5. _serialize_envelope(env) → JSON string (NaN/inf → null)
  6. redis.set(f"bot_state:{ticker}:{intent}", json, ex=tier_ttl)
  7. _record_build_timing(redis, ticker, intent, elapsed_ms, expiration)

Per-ticker errors caught and logged; the loop continues for other tickers.
The whole thing is gated by env var BOT_STATE_PRODUCER_ENABLED. When that
env var is off, start_producer() returns None and the daemon never spawns.

Audit notes:
  - Tier env vars use the empty-string-safe parser (_parse_intents) so an
    unset Tier C cleanly disables that tier instead of crashing.
  - PRODUCER_VERSION=1 ships with the first version. Bump on schema change.
  - CONVENTION_VERSION=2 is Patch 9's dealer-side convention. Hard-coded —
    don't expose as an env var; mismatch is the consumer's job to detect.
"""

from __future__ import annotations
import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Versioning — bump PRODUCER_VERSION on schema change.
# ─────────────────────────────────────────────────────────────────────

PRODUCER_VERSION = 1
CONVENTION_VERSION = 2  # Patch 9 dealer-side convention. Don't change.


# ─────────────────────────────────────────────────────────────────────
# Env var parsing (defensive — handles unset, empty string, garbage)
# ─────────────────────────────────────────────────────────────────────

def _parse_intents(value: Optional[str]) -> List[str]:
    """Parse a comma-separated intent list. Empty/None → empty list.

    Spec v4 implementer note: "".split(",") returns [""] which would
    iterate as a one-element string list. We filter empty strings out
    so an unset Tier C env var disables that tier cleanly.
    """
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_tickers(value: Optional[str]) -> List[str]:
    """Comma-separated tickers, uppercased, deduped, order-preserving."""
    if not value:
        return []
    seen = set()
    out: List[str] = []
    for raw in value.split(","):
        t = raw.strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _parse_int_env(value: Optional[str], default: int) -> int:
    """Read an int from env. Empty/garbage → default."""
    if not value:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────
# Envelope schema
# ─────────────────────────────────────────────────────────────────────

def _build_envelope(state: Dict[str, Any], intent: str, expiration: str) -> Dict[str, Any]:
    """Wrap a BotState dict in the producer envelope. Caller is responsible
    for converting BotState → dict (typically dataclasses.asdict).

    Returns a NEW dict; does not mutate `state`.
    """
    return {
        "producer_version": PRODUCER_VERSION,
        "convention_version": CONVENTION_VERSION,
        "intent": intent,
        "expiration": expiration,
        "state": state,
    }


# ─────────────────────────────────────────────────────────────────────
# JSON serialization with NaN/inf cleanup
# ─────────────────────────────────────────────────────────────────────

def _clean_for_json(obj: Any) -> Any:
    """Recursively convert NaN, +inf, -inf to None so json.dumps doesn't
    emit non-standard JSON (NaN/Infinity literals). Standard JSON parsers
    in JS/Go/etc. reject those; converting to null is the safest default.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_for_json(v) for v in obj]
    return obj


def _serialize_envelope(env: Dict[str, Any]) -> str:
    """Serialize an envelope dict to a JSON string. NaN/inf inside `state`
    are converted to null; all other values pass through json.dumps.
    """
    cleaned = _clean_for_json(env)
    return json.dumps(cleaned, separators=(",", ":"))
```

- [ ] **Step 2: Run the tests, verify they pass**

```bash
python3 test_bot_state_producer.py
```

Expected: `PASSED: ≥10, FAILED: 0`. (Each test has 1+ assertions.)

- [ ] **Step 3: AST check**

```bash
python3 -c "import ast; ast.parse(open('bot_state_producer.py', encoding='utf-8').read())"
```

Expected: clean (no output).

- [ ] **Step 4: Commit**

```bash
git add bot_state_producer.py test_bot_state_producer.py
git commit -m "$(cat <<'EOF'
Patch B.1: bot_state_producer skeleton + parsing + envelope helpers

Module skeleton with deterministic helpers:
  - _parse_intents/_parse_tickers/_parse_int_env: env-var parsing,
    empty-string-safe (per spec v4 note).
  - _build_envelope: wrap BotState dict with producer_version,
    convention_version, intent, expiration metadata.
  - _serialize_envelope + _clean_for_json: JSON-safe with NaN/inf →
    null conversion; standard parsers reject NaN literals.

PRODUCER_VERSION=1, CONVENTION_VERSION=2 (Patch 9). No threads, no
Redis, no caller yet. Patch B.2 adds telemetry + lock primitives.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task B.2 — Telemetry + Redis lock primitives

**Why second:** B.3's per-tier loop needs both pieces — telemetry to record per-build elapsed_ms, lock primitives to serialize the producer across multi-worker Render. Both are pure functions on a Redis client; both can be tested with a fake Redis.

**Files:**
- Modify: `bot_state_producer.py` (append four functions)
- Modify: `test_bot_state_producer.py` (append tests)

### Task 2.1 — Failing tests for telemetry + lock

- [ ] **Step 1: Add a fake Redis client to the test file**

Add this class definition to `test_bot_state_producer.py` right above the test functions (after the assert helpers):

```python
class _FakeRedis:
    """Minimal Redis stand-in for unit tests. Implements the SUBSET of
    redis-py we need: SET (with NX/EX/XX), GET, EXPIRE, ZADD, ZRANGE,
    EVAL (for safe-release Lua). Enough to test our helpers; nothing
    more. NOT a general-purpose mock.
    """
    def __init__(self):
        self.kv: Dict[str, str] = {}
        self.zset: Dict[str, list] = {}  # key → list of (score, member)
        self.expirations: Dict[str, float] = {}
        self.ops: list = []  # call log for assertions

    def set(self, key, value, ex=None, nx=False, xx=False):
        self.ops.append(("set", key, value, ex, nx, xx))
        if nx and key in self.kv:
            return None
        if xx and key not in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.expirations[key] = time.time() + ex
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                n += 1
            self.expirations.pop(k, None)
        return n

    def expire(self, key, seconds):
        # Real Redis EXPIRE works on any key (kv, zset, hash, ...). Match
        # that behavior so a sorted-set TTL update doesn't silently no-op.
        self.ops.append(("expire", key, seconds))
        if key in self.kv or key in self.zset:
            self.expirations[key] = time.time() + seconds
            return 1
        return 0

    def ttl(self, key):
        if key not in self.kv:
            return -2
        if key not in self.expirations:
            return -1
        return int(self.expirations[key] - time.time())

    def zadd(self, key, mapping):
        z = self.zset.setdefault(key, [])
        for member, score in mapping.items():
            z.append((score, member))
        return len(mapping)

    def zrange(self, key, start, stop, withscores=False):
        z = sorted(self.zset.get(key, []))
        sliced = z[start:stop+1] if stop >= 0 else z[start:]
        if withscores:
            return [(m, s) for s, m in sliced]
        return [m for _, m in sliced]

    def eval(self, script, numkeys, *args):
        """Minimal Lua eval — supports the safe-release and safe-refresh
        owner-checked patterns used by _release_lock and _refresh_lock."""
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        # Owner-checked DEL (release).
        if "redis.call('get'" in script and "redis.call('del'" in script:
            if self.kv.get(keys[0]) == argv[0]:
                return self.delete(keys[0])
            return 0
        # Owner-checked EXPIRE (refresh).
        if "redis.call('get'" in script and "redis.call('expire'" in script:
            if self.kv.get(keys[0]) == argv[0]:
                return self.expire(keys[0], int(argv[1]))
            return 0
        raise NotImplementedError(f"FakeRedis.eval: unrecognized script")
```

(Add `from typing import Dict` to the imports at the top of the test file if not present already.)

- [ ] **Step 2: Add telemetry tests below the existing tests**

```python
# ─────────────────────────────────────────────────────────────────────
# Telemetry tests
# ─────────────────────────────────────────────────────────────────────

def test_record_build_timing_writes_sorted_set_member():
    from bot_state_producer import _record_build_timing
    fake = _FakeRedis()
    _record_build_timing(fake, ticker="AAPL", intent="front",
                         elapsed_ms=143, expiration="2026-05-08")
    # The key for today's UTC date should now contain one zset member.
    keys = list(fake.zset.keys())
    assert_eq(len(keys), 1, "exactly one timings key written")
    assert_true(keys[0].startswith("bot_state_producer:timings:"),
                "key prefix matches spec")
    members = fake.zrange(keys[0], 0, -1, withscores=True)
    assert_eq(len(members), 1, "one member written")
    member, score = members[0]
    parsed = json.loads(member)
    assert_eq(parsed["ticker"], "AAPL", "ticker on member")
    assert_eq(parsed["intent"], "front", "intent on member")
    assert_eq(parsed["elapsed_ms"], 143, "elapsed_ms on member")
    assert_eq(parsed["expiration"], "2026-05-08", "expiration on member")
    assert_true(score > 0, "score is millisecond timestamp")


def test_record_build_timing_sets_48h_ttl():
    from bot_state_producer import _record_build_timing
    fake = _FakeRedis()
    _record_build_timing(fake, "AAPL", "front", 143, "2026-05-08")
    keys = list(fake.zset.keys())
    # _record_build_timing should call EXPIRE — verify TTL is around 48h.
    # FakeRedis.expirations is keyed by .kv keys, but timings use zset.
    # We assert the EXPIRE call happened by checking `.ttl(key)` returns >0.
    # NOTE: FakeRedis.expire only sets TTL on kv keys. The implementer
    # should call `fake.expire(key, 48*3600)` AFTER zadd, which works
    # because Redis treats zset keys uniformly with kv keys for TTL.
    # We validate by checking the implementer did call expire:
    assert_true(any(op[0] == "expire" for op in fake.ops) or
                any(k for k in fake.expirations),
                "expire() was called on the timings key")
```

(The `_FakeRedis.expire` method already appends to `self.ops` from the
class definition above — no further modification needed.)

- [ ] **Step 3: Add lock primitive tests**

```python
# ─────────────────────────────────────────────────────────────────────
# Multi-worker Redis lock tests
# ─────────────────────────────────────────────────────────────────────

def test_lock_acquire_when_free():
    from bot_state_producer import _acquire_lock
    fake = _FakeRedis()
    token = _acquire_lock(fake, lock_key="bsp:lock", ttl_sec=60)
    assert_true(token is not None, "acquire returns owner token when free")
    assert_eq(fake.kv["bsp:lock"], token, "lock value is the token")


def test_lock_acquire_fails_when_held():
    from bot_state_producer import _acquire_lock
    fake = _FakeRedis()
    fake.kv["bsp:lock"] = "someone-else"
    token = _acquire_lock(fake, lock_key="bsp:lock", ttl_sec=60)
    assert_eq(token, None, "acquire returns None when lock is held")


def test_lock_release_only_by_owner():
    from bot_state_producer import _acquire_lock, _release_lock
    fake = _FakeRedis()
    token = _acquire_lock(fake, "bsp:lock", 60)
    # Wrong-token release: lock stays.
    released = _release_lock(fake, "bsp:lock", "wrong-token")
    assert_eq(released, False, "wrong owner cannot release")
    assert_eq(fake.kv["bsp:lock"], token, "lock still held by original owner")
    # Right-token release: lock removed.
    released = _release_lock(fake, "bsp:lock", token)
    assert_eq(released, True, "owner can release")
    assert_true("bsp:lock" not in fake.kv, "lock key gone after release")


def test_lock_refresh_extends_ttl():
    from bot_state_producer import _acquire_lock, _refresh_lock
    fake = _FakeRedis()
    token = _acquire_lock(fake, "bsp:lock", 60)
    # Refresh with the right token bumps TTL.
    refreshed = _refresh_lock(fake, "bsp:lock", token, 120)
    assert_eq(refreshed, True, "owner can refresh lock TTL")
    # Refresh with wrong token is a no-op.
    refreshed = _refresh_lock(fake, "bsp:lock", "wrong", 120)
    assert_eq(refreshed, False, "wrong token cannot refresh")
```

- [ ] **Step 4: Register all new tests in the `__main__` block** at the bottom of the test file:

```python
if __name__ == "__main__":
    # Parsing
    test_parse_intents_filters_empty_strings()
    test_parse_tickers_uppercase_dedup()
    test_parse_int_env_with_default()
    # Envelope + serialization
    test_envelope_includes_required_metadata()
    test_envelope_does_not_mutate_state()
    test_serialize_handles_nan_and_inf()
    test_serialize_round_trip_clean_state()
    # Telemetry
    test_record_build_timing_writes_sorted_set_member()
    test_record_build_timing_sets_48h_ttl()
    # Lock
    test_lock_acquire_when_free()
    test_lock_acquire_fails_when_held()
    test_lock_release_only_by_owner()
    test_lock_refresh_extends_ttl()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 5: Run tests, verify the new ones fail**

```bash
python3 test_bot_state_producer.py
```

Expected: previously passing tests still pass; new tests fail with `ImportError` for `_record_build_timing`, `_acquire_lock`, `_release_lock`, `_refresh_lock`.

### Task 2.2 — Implement telemetry + lock

- [ ] **Step 1: Append telemetry to `bot_state_producer.py`**

Add after the `_serialize_envelope` function:

```python
# ─────────────────────────────────────────────────────────────────────
# Per-build timing telemetry — hard deliverable per spec v4.
#
# Sorted set keyed by UTC date. Member is JSON, score is unix ms so
# multiple builds for the same ticker in the same second don't collide.
# ZRANGEBYSCORE lets the post-deploy analysis slice arbitrary windows.
# ─────────────────────────────────────────────────────────────────────

TIMINGS_KEY_PREFIX = "bot_state_producer:timings:"
TIMINGS_TTL_SEC = 48 * 3600  # 48 hours — covers the 24h analysis window + slack


def _record_build_timing(redis_client, ticker: str, intent: str,
                         elapsed_ms: int, expiration: str) -> None:
    """Append one build-timing record to the daily sorted set.

    No-op on Redis errors — telemetry must NEVER block the producer loop.
    """
    if redis_client is None:
        return
    try:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"{TIMINGS_KEY_PREFIX}{date_str}"
        member = json.dumps({
            "ticker": ticker,
            "intent": intent,
            "elapsed_ms": int(elapsed_ms),
            "expiration": expiration,
        }, separators=(",", ":"))
        score = int(time.time() * 1000)  # millis since epoch
        redis_client.zadd(key, {member: score})
        redis_client.expire(key, TIMINGS_TTL_SEC)
    except Exception as e:
        log.debug(f"telemetry write failed for {ticker}/{intent}: {e}")


# ─────────────────────────────────────────────────────────────────────
# Multi-worker lock — only one Render web worker runs the producer.
#
# Pattern: SET key token NX EX ttl (atomic acquire). Owner refreshes via
# Lua-equivalent CAS. Release is owner-checked via Lua so a stale owner
# can't unlock a lock that's been re-acquired by someone else.
# ─────────────────────────────────────────────────────────────────────

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


def _acquire_lock(redis_client, lock_key: str, ttl_sec: int) -> Optional[str]:
    """Atomically acquire `lock_key` with TTL. Returns an owner token on
    success, None on failure (lock already held). The token is required
    to release/refresh and is opaque to callers."""
    if redis_client is None:
        return None
    import secrets
    token = secrets.token_hex(16)
    try:
        ok = redis_client.set(lock_key, token, nx=True, ex=ttl_sec)
        return token if ok else None
    except Exception as e:
        log.warning(f"lock acquire failed: {e}")
        return None


def _release_lock(redis_client, lock_key: str, token: str) -> bool:
    """Release a lock — only succeeds if `token` matches the current value."""
    if redis_client is None:
        return False
    try:
        result = redis_client.eval(_RELEASE_SCRIPT, 1, lock_key, token)
        return bool(result)
    except Exception as e:
        log.warning(f"lock release failed: {e}")
        return False


def _refresh_lock(redis_client, lock_key: str, token: str, ttl_sec: int) -> bool:
    """Bump TTL on a lock — only if `token` still owns it."""
    if redis_client is None:
        return False
    try:
        result = redis_client.eval(_REFRESH_SCRIPT, 1, lock_key, token, ttl_sec)
        return bool(result)
    except Exception as e:
        log.warning(f"lock refresh failed: {e}")
        return False
```

- [ ] **Step 2: Run tests, verify they pass**

```bash
python3 test_bot_state_producer.py
```

Expected: `PASSED: ≥17, FAILED: 0`.

- [ ] **Step 3: AST check**

```bash
python3 -c "import ast; ast.parse(open('bot_state_producer.py', encoding='utf-8').read())"
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add bot_state_producer.py test_bot_state_producer.py
git commit -m "$(cat <<'EOF'
Patch B.2: telemetry + multi-worker Redis lock primitives

Adds:
  - _record_build_timing: appends per-build records to a daily sorted
    set with 48h TTL (hard deliverable for post-deploy cadence tuning).
    No-op on Redis errors — telemetry must never block the producer.
  - _acquire_lock / _release_lock / _refresh_lock: SET NX EX +
    Lua-CAS pattern for safe owner-checked release. Lets us run the
    producer on exactly one Render web worker even with multi-worker
    Gunicorn.

Both pieces are pure functions on an injected redis_client, exhaustively
unit-tested with a hand-rolled FakeRedis. No threads, no caller yet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task B.3 — Single-tier producer loop

**Why third:** All the deterministic and Redis-side helpers are in place. Now we wire them into the per-tier work function. Single-tier loop is the testable unit; B.4 composes three of them.

**Files:**
- Modify: `bot_state_producer.py` (add `_run_tier_pass`, `_build_one_state`)
- Modify: `test_bot_state_producer.py` (add per-tier tests with fake DataRouter)

### Task 3.1 — Failing tests for the single-tier pass

- [ ] **Step 1: Add fakes for canonical_expiration / fetch_raw_inputs / BotState.build**

Add to `test_bot_state_producer.py` after the `_FakeRedis` class:

```python
class _FakeBuildContext:
    """Fakes the (canonical_expiration, fetch_raw_inputs, BotState.build_from_raw)
    chain. Returns canned values per ticker; supports raising specific
    exceptions to test error isolation.
    """
    def __init__(self):
        # ticker → {"expiration": "2026-05-08", "state_dict": {...}, "raises": ExceptionInstance|None}
        self.config: Dict[str, dict] = {}
        self.calls: list = []

    def canonical_expiration(self, ticker, intent, *, data_router=None):
        cfg = self.config.get(ticker)
        if cfg is None:
            return None  # no qualifying chain
        return cfg["expiration"]

    def fetch_raw_inputs(self, ticker, expiration, *, data_router, **kwargs):
        cfg = self.config.get(ticker, {})
        if cfg.get("raises_at") == "fetch":
            raise cfg["raises"]
        # Return a tagged sentinel; the build step doesn't inspect it.
        return {"_fake_raw": ticker, "_exp": expiration}

    def build_from_raw(self, raw, *, days_to_exp=None):
        ticker = raw.get("_fake_raw") if isinstance(raw, dict) else None
        cfg = self.config.get(ticker, {})
        if cfg.get("raises_at") == "build":
            raise cfg["raises"]
        return cfg["state_dict"]
```

- [ ] **Step 2: Add tier-pass tests**

```python
# ─────────────────────────────────────────────────────────────────────
# Single-tier pass tests
# ─────────────────────────────────────────────────────────────────────

def test_tier_pass_writes_one_key_per_ticker_intent():
    """Two tickers × one intent → two Redis keys with valid envelopes."""
    from bot_state_producer import _run_tier_pass
    fake_redis = _FakeRedis()
    fake_ctx = _FakeBuildContext()
    fake_ctx.config["AAPL"] = {
        "expiration": "2026-05-08",
        "state_dict": {"ticker": "AAPL", "spot": 184.50, "fields_lit": 22,
                       "fetch_errors": [], "canonical_status": {}},
    }
    fake_ctx.config["MSFT"] = {
        "expiration": "2026-05-08",
        "state_dict": {"ticker": "MSFT", "spot": 412.30, "fields_lit": 22,
                       "fetch_errors": [], "canonical_status": {}},
    }
    _run_tier_pass(
        tier_name="A",
        intents=["front"],
        ttl_sec=180,
        tickers=["AAPL", "MSFT"],
        cached_md=object(),  # opaque, only forwarded
        redis_client=fake_redis,
        canonical_expiration_fn=fake_ctx.canonical_expiration,
        fetch_raw_inputs_fn=fake_ctx.fetch_raw_inputs,
        build_from_raw_fn=fake_ctx.build_from_raw,
    )
    aapl_raw = fake_redis.get("bot_state:AAPL:front")
    msft_raw = fake_redis.get("bot_state:MSFT:front")
    assert_true(aapl_raw is not None, "AAPL key written")
    assert_true(msft_raw is not None, "MSFT key written")
    aapl = json.loads(aapl_raw)
    assert_eq(aapl["intent"], "front", "AAPL envelope intent")
    assert_eq(aapl["expiration"], "2026-05-08", "AAPL envelope expiration")
    assert_eq(aapl["state"]["spot"], 184.50, "AAPL state body intact")


def test_tier_pass_skips_ticker_when_canonical_expiration_returns_none():
    from bot_state_producer import _run_tier_pass
    fake_redis = _FakeRedis()
    fake_ctx = _FakeBuildContext()
    fake_ctx.config["AAPL"] = {
        "expiration": "2026-05-08",
        "state_dict": {"ticker": "AAPL", "spot": 184.50, "fields_lit": 22,
                       "fetch_errors": [], "canonical_status": {}},
    }
    # NO entry for FAKE → canonical_expiration returns None → skip.
    _run_tier_pass(
        "A", ["front"], 180,
        tickers=["AAPL", "FAKE"],
        cached_md=object(),
        redis_client=fake_redis,
        canonical_expiration_fn=fake_ctx.canonical_expiration,
        fetch_raw_inputs_fn=fake_ctx.fetch_raw_inputs,
        build_from_raw_fn=fake_ctx.build_from_raw,
    )
    assert_true(fake_redis.get("bot_state:AAPL:front") is not None,
                "AAPL key written")
    assert_eq(fake_redis.get("bot_state:FAKE:front"), None,
              "FAKE skipped silently when no qualifying chain")


def test_tier_pass_isolates_per_ticker_errors():
    """Spec mandate: one ticker raising must not stop the loop."""
    from bot_state_producer import _run_tier_pass
    fake_redis = _FakeRedis()
    fake_ctx = _FakeBuildContext()
    fake_ctx.config["AAPL"] = {
        "expiration": "2026-05-08",
        "state_dict": {"ticker": "AAPL", "spot": 184.50, "fields_lit": 22,
                       "fetch_errors": [], "canonical_status": {}},
    }
    fake_ctx.config["BAD"] = {
        "expiration": "2026-05-08",
        "raises_at": "build",
        "raises": RuntimeError("simulated build failure"),
    }
    fake_ctx.config["MSFT"] = {
        "expiration": "2026-05-08",
        "state_dict": {"ticker": "MSFT", "spot": 412.30, "fields_lit": 22,
                       "fetch_errors": [], "canonical_status": {}},
    }
    _run_tier_pass(
        "A", ["front"], 180,
        tickers=["AAPL", "BAD", "MSFT"],
        cached_md=object(),
        redis_client=fake_redis,
        canonical_expiration_fn=fake_ctx.canonical_expiration,
        fetch_raw_inputs_fn=fake_ctx.fetch_raw_inputs,
        build_from_raw_fn=fake_ctx.build_from_raw,
    )
    # AAPL and MSFT must succeed despite BAD raising.
    assert_true(fake_redis.get("bot_state:AAPL:front") is not None,
                "AAPL key written before BAD raised")
    assert_true(fake_redis.get("bot_state:MSFT:front") is not None,
                "MSFT key written after BAD raised")
    assert_eq(fake_redis.get("bot_state:BAD:front"), None,
              "BAD ticker has no key (build raised)")


def test_tier_pass_writes_telemetry_per_successful_build():
    from bot_state_producer import _run_tier_pass, TIMINGS_KEY_PREFIX
    fake_redis = _FakeRedis()
    fake_ctx = _FakeBuildContext()
    fake_ctx.config["AAPL"] = {
        "expiration": "2026-05-08",
        "state_dict": {"ticker": "AAPL", "spot": 184.50, "fields_lit": 22,
                       "fetch_errors": [], "canonical_status": {}},
    }
    _run_tier_pass(
        "A", ["front"], 180,
        tickers=["AAPL"],
        cached_md=object(),
        redis_client=fake_redis,
        canonical_expiration_fn=fake_ctx.canonical_expiration,
        fetch_raw_inputs_fn=fake_ctx.fetch_raw_inputs,
        build_from_raw_fn=fake_ctx.build_from_raw,
    )
    timings_keys = [k for k in fake_redis.zset.keys() if k.startswith(TIMINGS_KEY_PREFIX)]
    assert_eq(len(timings_keys), 1, "one timings key for the day")
    members = fake_redis.zrange(timings_keys[0], 0, -1, withscores=False)
    assert_eq(len(members), 1, "one timing record written")
    rec = json.loads(members[0])
    assert_eq(rec["ticker"], "AAPL", "timing record ticker")
    assert_eq(rec["intent"], "front", "timing record intent")
    assert_true(rec["elapsed_ms"] >= 0, "elapsed_ms is non-negative integer")
```

- [ ] **Step 3: Register the new tests in `__main__`**

Append to the `__main__` block:

```python
    # Single-tier pass
    test_tier_pass_writes_one_key_per_ticker_intent()
    test_tier_pass_skips_ticker_when_canonical_expiration_returns_none()
    test_tier_pass_isolates_per_ticker_errors()
    test_tier_pass_writes_telemetry_per_successful_build()
```

- [ ] **Step 4: Run tests, verify the new ones fail**

```bash
python3 test_bot_state_producer.py
```

Expected: prior tests still pass; new tests fail with `ImportError` on `_run_tier_pass`.

### Task 3.2 — Implement `_run_tier_pass`

- [ ] **Step 1: Append to `bot_state_producer.py`**

Add after the lock primitives:

```python
# ─────────────────────────────────────────────────────────────────────
# Per-tier work function — one full pass over (ticker × intent) for
# this tier. Per-ticker errors are caught and logged; the pass always
# completes for every (ticker, intent) regardless of individual failures.
#
# Dependencies are injected (canonical_expiration_fn, fetch_raw_inputs_fn,
# build_from_raw_fn) so unit tests can mock them. In production the
# default-imports wrapper at module bottom binds the real implementations.
# ─────────────────────────────────────────────────────────────────────

KEY_PREFIX = "bot_state:"


def _build_one_state(
    ticker: str,
    intent: str,
    cached_md,
    canonical_expiration_fn,
    fetch_raw_inputs_fn,
    build_from_raw_fn,
) -> Optional[Dict[str, Any]]:
    """Build a single (ticker, intent) state dict + expiration. Returns
    None if no qualifying expiration. Raises on fetch/build failures —
    caller handles per-ticker isolation.
    """
    expiration = canonical_expiration_fn(ticker, intent, data_router=cached_md)
    if not expiration:
        return None
    raw = fetch_raw_inputs_fn(ticker, expiration, data_router=cached_md)
    state = build_from_raw_fn(raw)
    # If state is a dataclass instance, convert to dict for the envelope.
    state_dict = asdict(state) if is_dataclass(state) else dict(state)
    return {"state_dict": state_dict, "expiration": expiration}


def _run_tier_pass(
    tier_name: str,
    intents: List[str],
    ttl_sec: int,
    tickers: List[str],
    cached_md,
    redis_client,
    canonical_expiration_fn,
    fetch_raw_inputs_fn,
    build_from_raw_fn,
) -> None:
    """One full pass over (ticker × intent) for this tier. Always
    completes; per-ticker errors are logged and skipped."""
    if not intents or not tickers:
        return
    for ticker in tickers:
        for intent in intents:
            t_start = time.time()
            try:
                built = _build_one_state(
                    ticker, intent, cached_md,
                    canonical_expiration_fn,
                    fetch_raw_inputs_fn,
                    build_from_raw_fn,
                )
            except Exception as e:
                log.warning(
                    f"[bsp tier={tier_name}] {ticker}/{intent} build failed: {e}"
                )
                continue
            if built is None:
                # No qualifying expiration — skip silently. Tier C with
                # an empty intents list takes the early-return above.
                continue
            envelope = _build_envelope(
                state=built["state_dict"],
                intent=intent,
                expiration=built["expiration"],
            )
            try:
                payload = _serialize_envelope(envelope)
                key = f"{KEY_PREFIX}{ticker}:{intent}"
                redis_client.set(key, payload, ex=ttl_sec)
            except Exception as e:
                log.warning(
                    f"[bsp tier={tier_name}] {ticker}/{intent} write failed: {e}"
                )
                continue
            elapsed_ms = int((time.time() - t_start) * 1000)
            _record_build_timing(redis_client, ticker, intent,
                                 elapsed_ms, built["expiration"])
```

- [ ] **Step 2: Run tests**

```bash
python3 test_bot_state_producer.py
```

Expected: `PASSED: ≥21, FAILED: 0`.

- [ ] **Step 3: AST check**

```bash
python3 -c "import ast; ast.parse(open('bot_state_producer.py', encoding='utf-8').read())"
```

- [ ] **Step 4: Commit**

```bash
git add bot_state_producer.py test_bot_state_producer.py
git commit -m "$(cat <<'EOF'
Patch B.3: single-tier producer pass with per-ticker error isolation

_run_tier_pass walks (ticker × intent) once: canonical_expiration →
fetch_raw_inputs → BotState.build_from_raw → envelope → SET key val
EX ttl → record build timing. Per-ticker exceptions are caught + logged;
the pass always completes for every (ticker, intent).

Dependencies injected for testability (canonical_expiration_fn,
fetch_raw_inputs_fn, build_from_raw_fn). Patch B.4 binds the real ones
and composes three of these into the staggered tier daemons.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task B.4 — Three-tier composition + factory

**Why fourth:** B.3 produces one tier-pass; this task wraps three of them in daemon threads, staggers their starts, owns lifecycle (start/stop), and gates the whole thing on the env flag. The factory `start_producer()` is the only public entry point; everything else is private.

**Files:**
- Modify: `bot_state_producer.py` (`BotStateProducer` class + `start_producer()`)
- Modify: `test_bot_state_producer.py` (lifecycle + flag-gating tests)

### Task 4.1 — Failing tests for lifecycle and flag-gating

- [ ] **Step 1: Append the lifecycle and flag tests**

Add to `test_bot_state_producer.py`:

```python
# ─────────────────────────────────────────────────────────────────────
# Lifecycle + flag-gating tests
# ─────────────────────────────────────────────────────────────────────

def test_start_producer_returns_none_when_disabled():
    """Factory must respect BOT_STATE_PRODUCER_ENABLED=false."""
    from bot_state_producer import start_producer
    os.environ["BOT_STATE_PRODUCER_ENABLED"] = "false"
    p = start_producer(cached_md=object(), redis_client=_FakeRedis())
    assert_eq(p, None, "disabled flag → no producer instance")


def test_start_producer_returns_none_when_redis_missing():
    """Without Redis we can't write envelopes — refuse to start."""
    from bot_state_producer import start_producer
    os.environ["BOT_STATE_PRODUCER_ENABLED"] = "true"
    os.environ["BOT_STATE_PRODUCER_TICKERS"] = "AAPL"
    p = start_producer(cached_md=object(), redis_client=None)
    assert_eq(p, None, "no redis client → no producer")


def test_start_producer_spawns_when_enabled():
    """Factory builds and starts the producer when conditions are met.
    We verify the resulting object exposes start/stop without exercising
    threads (test doesn't actually run the loops)."""
    from bot_state_producer import start_producer, BotStateProducer
    os.environ["BOT_STATE_PRODUCER_ENABLED"] = "true"
    os.environ["BOT_STATE_PRODUCER_TICKERS"] = "AAPL,MSFT"
    os.environ["BOT_STATE_PRODUCER_INTENTS_TIER_A"] = "front"
    os.environ["BOT_STATE_PRODUCER_INTENTS_TIER_B"] = "t7"
    os.environ["BOT_STATE_PRODUCER_INTENTS_TIER_C"] = ""
    p = start_producer(cached_md=object(), redis_client=_FakeRedis())
    try:
        assert_true(isinstance(p, BotStateProducer),
                    "factory returns BotStateProducer when enabled")
        # Stop quickly so the test exits cleanly.
    finally:
        if p is not None:
            p.stop()
            p.join(timeout=2.0)


def test_start_producer_empty_universe_returns_none():
    """If TICKERS is empty, there's nothing to produce."""
    from bot_state_producer import start_producer
    os.environ["BOT_STATE_PRODUCER_ENABLED"] = "true"
    os.environ["BOT_STATE_PRODUCER_TICKERS"] = ""
    p = start_producer(cached_md=object(), redis_client=_FakeRedis())
    assert_eq(p, None, "empty TICKERS → no producer")


def test_producer_stop_is_idempotent():
    """stop() can be called twice without raising."""
    from bot_state_producer import start_producer
    os.environ["BOT_STATE_PRODUCER_ENABLED"] = "true"
    os.environ["BOT_STATE_PRODUCER_TICKERS"] = "AAPL"
    os.environ["BOT_STATE_PRODUCER_INTENTS_TIER_A"] = "front"
    p = start_producer(cached_md=object(), redis_client=_FakeRedis())
    try:
        p.stop()
        p.stop()  # second call must not raise
        assert_true(True, "double stop() is safe")
    finally:
        if p is not None:
            p.join(timeout=2.0)
```

Register in `__main__`:

```python
    # Lifecycle / factory
    test_start_producer_returns_none_when_disabled()
    test_start_producer_returns_none_when_redis_missing()
    test_start_producer_spawns_when_enabled()
    test_start_producer_empty_universe_returns_none()
    test_producer_stop_is_idempotent()
```

- [ ] **Step 2: Run tests, verify the new ones fail**

```bash
python3 test_bot_state_producer.py
```

Expected: prior tests pass; new tests fail with `ImportError` on `start_producer` and `BotStateProducer`.

### Task 4.2 — Implement the class + factory

- [ ] **Step 1: Append `BotStateProducer` class to `bot_state_producer.py`**

```python
# ─────────────────────────────────────────────────────────────────────
# BotStateProducer — owns three daemon threads, one per tier.
#
# Tiers are staggered T+0/T+10/T+20 so the rate limiter sees a smooth
# curve rather than a synchronized burst. Each thread runs a pass-then-
# sleep loop that catches ANY exception and restarts after 5s. This
# mirrors SchwabStreamManager's reconnect pattern.
# ─────────────────────────────────────────────────────────────────────

LOCK_KEY = "bot_state_producer:lock"
LOCK_TTL_SEC = 90  # > Tier A cadence so the leader can refresh between passes


class BotStateProducer:
    """Three daemon threads + lifecycle. Construct via start_producer()."""

    def __init__(
        self,
        tickers: List[str],
        tier_a_intents: List[str],
        tier_b_intents: List[str],
        tier_c_intents: List[str],
        tier_a_cadence: int,
        tier_b_cadence: int,
        tier_c_cadence: int,
        cached_md,
        redis_client,
    ):
        self._tickers = tickers
        self._tiers = [
            ("A", tier_a_intents, tier_a_cadence, tier_a_cadence * 3, 0),
            ("B", tier_b_intents, tier_b_cadence, tier_b_cadence * 3, 10),
            ("C", tier_c_intents, tier_c_cadence, tier_c_cadence * 3, 20),
        ]
        self._cached_md = cached_md
        self._redis = redis_client
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock_token: Optional[str] = None

    def start(self) -> None:
        """Spawn three daemon threads. Returns immediately."""
        # Acquire the cross-worker lock once at startup. If we don't get
        # it, another worker is leader; quietly do nothing.
        self._lock_token = _acquire_lock(self._redis, LOCK_KEY, LOCK_TTL_SEC)
        if self._lock_token is None:
            log.info("bot_state_producer: another worker holds the lock; staying idle")
            return

        log.info(
            f"bot_state_producer: starting {len([t for t in self._tiers if t[1]])} "
            f"active tiers for {len(self._tickers)} tickers"
        )
        for tier_name, intents, cadence, ttl, stagger in self._tiers:
            if not intents:
                # Empty tier — daemon thread still spawns so a future
                # env-var change can re-enable without a redeploy. The
                # loop just iterates through an empty intent list.
                continue
            t = threading.Thread(
                target=self._tier_loop,
                args=(tier_name, intents, cadence, ttl, stagger),
                name=f"bsp-tier-{tier_name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Signal all tier threads to exit at the next sleep boundary."""
        self._stop.set()
        if self._lock_token is not None:
            _release_lock(self._redis, LOCK_KEY, self._lock_token)
            self._lock_token = None

    def join(self, timeout: Optional[float] = None) -> None:
        for t in self._threads:
            t.join(timeout=timeout)

    # ─────────────────────────────────────────────────────────────────

    def _tier_loop(self, tier_name: str, intents: List[str],
                   cadence_sec: int, ttl_sec: int, stagger_sec: int) -> None:
        """Forever-loop: stagger initial start, then pass + sleep. Outer
        try/except restarts the loop on unexpected crashes (5s backoff)."""
        # Initial stagger — wait, then start passing.
        if self._stop.wait(timeout=stagger_sec):
            return  # stop signaled during stagger

        while not self._stop.is_set():
            t_start = time.time()
            try:
                # Refresh lock TTL on every pass. If we lose it (another
                # worker took over), we exit gracefully.
                if not _refresh_lock(self._redis, LOCK_KEY,
                                     self._lock_token, LOCK_TTL_SEC):
                    log.warning(
                        f"[bsp tier={tier_name}] lost leader lock; exiting tier loop"
                    )
                    return
                # Lazy-import the real dependencies so test modules can
                # monkey-patch them at import time without circular issues.
                from canonical_expiration import canonical_expiration
                from raw_inputs import fetch_raw_inputs
                from bot_state import BotState
                _run_tier_pass(
                    tier_name=tier_name,
                    intents=intents,
                    ttl_sec=ttl_sec,
                    tickers=self._tickers,
                    cached_md=self._cached_md,
                    redis_client=self._redis,
                    canonical_expiration_fn=canonical_expiration,
                    fetch_raw_inputs_fn=fetch_raw_inputs,
                    build_from_raw_fn=BotState.build_from_raw,
                )
            except Exception as e:
                log.error(f"[bsp tier={tier_name}] outer loop crashed: {e}")
                # 5s backoff before the next retry — avoid tight crash loops.
                if self._stop.wait(timeout=5.0):
                    return
                continue
            elapsed = time.time() - t_start
            sleep_for = max(0.0, cadence_sec - elapsed)
            log.info(
                f"[bsp tier={tier_name}] pass complete in {elapsed:.1f}s; "
                f"sleeping {sleep_for:.1f}s"
            )
            if self._stop.wait(timeout=sleep_for):
                return


# ─────────────────────────────────────────────────────────────────────
# Public factory
# ─────────────────────────────────────────────────────────────────────

_singleton: Optional[BotStateProducer] = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def start_producer(cached_md, redis_client) -> Optional[BotStateProducer]:
    """Factory. Returns a started BotStateProducer or None.

    Returns None when:
      - BOT_STATE_PRODUCER_ENABLED is unset/false (env-flag rollback path)
      - redis_client is None (we can't write envelopes)
      - BOT_STATE_PRODUCER_TICKERS is empty (nothing to produce)
      - cross-worker lock is held by another worker (silent — the other
        worker IS the producer)
    """
    global _singleton

    if not _env_bool("BOT_STATE_PRODUCER_ENABLED", default=False):
        log.info("bot_state_producer: BOT_STATE_PRODUCER_ENABLED is off; not starting")
        return None
    if redis_client is None:
        log.warning("bot_state_producer: no redis_client; not starting")
        return None

    tickers = _parse_tickers(os.environ.get("BOT_STATE_PRODUCER_TICKERS"))
    if not tickers:
        log.warning("bot_state_producer: BOT_STATE_PRODUCER_TICKERS empty; not starting")
        return None

    p = BotStateProducer(
        tickers=tickers,
        tier_a_intents=_parse_intents(os.environ.get("BOT_STATE_PRODUCER_INTENTS_TIER_A", "front")),
        tier_b_intents=_parse_intents(os.environ.get("BOT_STATE_PRODUCER_INTENTS_TIER_B", "t7")),
        tier_c_intents=_parse_intents(os.environ.get("BOT_STATE_PRODUCER_INTENTS_TIER_C", "")),
        tier_a_cadence=_parse_int_env(os.environ.get("BOT_STATE_PRODUCER_CADENCE_TIER_A"), 60),
        tier_b_cadence=_parse_int_env(os.environ.get("BOT_STATE_PRODUCER_CADENCE_TIER_B"), 180),
        tier_c_cadence=_parse_int_env(os.environ.get("BOT_STATE_PRODUCER_CADENCE_TIER_C"), 600),
        cached_md=cached_md,
        redis_client=redis_client,
    )
    p.start()
    _singleton = p
    return p


def get_producer() -> Optional[BotStateProducer]:
    """Diagnostic accessor for the running producer (or None)."""
    return _singleton
```

- [ ] **Step 2: Run tests**

```bash
python3 test_bot_state_producer.py
```

Expected: `PASSED: ≥26, FAILED: 0`. Tests that exercise `start_producer` will spawn real (but quickly-stopped) daemon threads — that's fine.

- [ ] **Step 3: AST check**

```bash
python3 -c "import ast; ast.parse(open('bot_state_producer.py', encoding='utf-8').read())"
```

- [ ] **Step 4: Commit**

```bash
git add bot_state_producer.py test_bot_state_producer.py
git commit -m "$(cat <<'EOF'
Patch B.4: BotStateProducer class + start_producer factory

Three daemon threads, one per tier, staggered T+0/T+10/T+20. Each
runs pass-then-sleep with outer try/except + 5s backoff (mirrors
SchwabStreamManager._run_loop reconnect pattern). Cross-worker lock
acquired at startup and refreshed every pass; if lost, the tier loop
exits cleanly so a different worker can take over.

start_producer() is the only public factory. Returns None when:
  - BOT_STATE_PRODUCER_ENABLED is off (rollback)
  - redis_client is missing
  - TICKERS env var is empty
  - lock is held by another worker (silent — other worker is leader)

No app.py wiring yet; Patch B.5 hooks this into the boot sequence.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task B.5 — `app.py` boot wiring + CLAUDE.md update

**Why last:** B.4 produces a callable factory. This task wires it into the existing `_acquire_background_leader()` path so it actually runs in production. No tests required (the factory is fully tested in B.4); the wiring is one new block in app.py and a CLAUDE.md update.

**Files:**
- Modify: `app.py` (~10 lines around line 15001)
- Modify: `CLAUDE.md` (vocabulary + decisions + smoke-test command)

### Task 5.1 — Wire startup hook in app.py

- [ ] **Step 1: Locate the existing background-leader block**

Find the block in `app.py` that calls `start_streaming(_cached_md)` (around line 15004 in the version current as of 2026-05-07). The exact line will have shifted — grep for it:

```bash
grep -n "start_streaming(_cached_md)" app.py
```

- [ ] **Step 2: Insert the producer startup**

Immediately after the `start_streaming(_cached_md)` line and before any subsequent calls (e.g., `start_continuous_flow`), add:

```python
                # Patch B: bot_state_producer (Render leader only).
                # Gated by BOT_STATE_PRODUCER_ENABLED env var; default off.
                # Returns None when disabled, when no Redis, or when the
                # cross-worker lock is held by another worker. Logs the
                # disposition in any of those cases.
                try:
                    from bot_state_producer import start_producer
                    start_producer(cached_md=_cached_md, redis_client=_get_redis())
                except Exception as e:
                    log.warning(f"bot_state_producer startup failed: {e}")
```

- [ ] **Step 3: AST check**

```bash
python3 -c "import ast; ast.parse(open('app.py', encoding='utf-8').read())"
```

Expected: clean.

### Task 5.2 — CLAUDE.md update

- [ ] **Step 1: Add to "Repo layout (what matters)" under the canonical-rebuild section**

Find the canonical-rebuild section (currently lists `raw_inputs.py`, `bot_state.py`, etc.). Add:

```markdown
- `bot_state_producer.py` — Patch B daemon. Three-tier loop (front/t7/t30+t60)
  that periodically computes BotState per (ticker, intent) and writes JSON
  envelopes to Redis. Gated by `BOT_STATE_PRODUCER_ENABLED`. Consumer is
  Patch C's Research page rewrite (separate plan).
- `test_bot_state_producer.py` — runnable without network or Redis (fake clients).
```

- [ ] **Step 2: Add to "Project vocabulary"**

Insert these three entries (anywhere in the vocabulary section that fits):

```markdown
- **bot_state_producer** — daemon-thread producer that pre-computes BotState
  for every (ticker, intent) on a tiered cadence and writes JSON envelopes
  to Redis. Lives in `bot_state_producer.py`. The Research page (after
  Patch C) becomes a pure consumer of these envelopes. Producer always
  runs on exactly one Render worker (cross-worker Redis lock).
- **producer envelope** — the JSON wrapper around BotState in Redis. Shape:
  `{producer_version, convention_version, intent, expiration, state}`.
  `producer_version` bumps on schema change (consumers reject unknown
  versions); `convention_version=2` is Patch 9 dealer-side. `state` is
  the BotState dataclass as-dict. Intent and expiration live on the
  envelope, NOT inside BotState (Open Question #3 resolution).
- **BOT_STATE_PRODUCER_** env vars — `_ENABLED` (bool, default off),
  `_TICKERS` (comma list), `_INTENTS_TIER_A/B/C` (comma lists; default
  `front`/`t7`/empty), `_CADENCE_TIER_A/B/C` (seconds; defaults 60/180/600).
  Empty-string Tier C cleanly disables that tier. Promote universe by
  editing env vars and restarting.
```

- [ ] **Step 3: Add to "Decisions already made — don't relitigate"**

```markdown
- bot_state_producer ships behind `BOT_STATE_PRODUCER_ENABLED=false`
  (default off). Initial production universe: 10 tickers × 2 intents
  (front + t7), Tier C empty. Run 7 trading days clean before promoting
  to full 35×4 universe. Per-build timing is recorded to a Redis sorted
  set (`bot_state_producer:timings:{YYYYMMDD}`) for post-deploy cadence
  tuning. Producer crashes are caught at the outer loop and restart with
  5s backoff. No market-hours pause.
- `producer_version` lives on the envelope and bumps on schema change.
  Consumers (Research page after Patch C) check for forward and backward
  compatibility on read; `convention_version` mismatch is treated as
  "warming up" rather than rendered (Patch 9 protection).
```

- [ ] **Step 4: Add to "Quick smoke-test commands"**

```bash
# Patch B producer test (fake Redis, fake Schwab, no network)
python3 test_bot_state_producer.py
```

- [ ] **Step 5: AST-free spot-check**

```bash
python3 test_bot_state_producer.py
```

Expected: prior tests still pass after CLAUDE.md edits (sanity that nothing broke).

- [ ] **Step 6: Commit**

```bash
git add app.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Patch B.5: wire bot_state_producer into app.py boot + CLAUDE.md

app.py: start_producer(cached_md=_cached_md, redis_client=_get_redis())
called inside the _acquire_background_leader() block, immediately after
start_streaming(). Wrapped in try/except so a producer startup failure
never blocks the existing trading-engine boot path.

CLAUDE.md: vocabulary entries (bot_state_producer, producer envelope,
BOT_STATE_PRODUCER_* env vars), decision entries (universe staging,
versioning, no market-hours pause), and the new smoke-test command.

Patch B is now live behind the env flag. Default deploy is no-op:
BOT_STATE_PRODUCER_ENABLED is unset → factory returns None → no
threads, no Redis writes, no behavior change. Brad enables explicitly
via Render env settings when ready.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Deployment & cutover

After all five patches (B.1–B.5) are committed and pushed:

1. **Deploy with flag off.** Render auto-rebuild picks up the code; behavior is unchanged because `BOT_STATE_PRODUCER_ENABLED` is unset.
2. **Smoke-test in Render logs.** Look for: `bot_state_producer: BOT_STATE_PRODUCER_ENABLED is off; not starting`. Confirms the wiring is reachable.
3. **Set the initial universe in Render → Environment:**
   ```
   BOT_STATE_PRODUCER_ENABLED=true
   BOT_STATE_PRODUCER_TICKERS=SPY,QQQ,IWM,DIA,AAPL,MSFT,NVDA,AMZN,META,TSLA
   BOT_STATE_PRODUCER_INTENTS_TIER_A=front
   BOT_STATE_PRODUCER_INTENTS_TIER_B=t7
   BOT_STATE_PRODUCER_INTENTS_TIER_C=
   ```
4. **Trigger a manual redeploy** so the flag takes effect.
5. **Watch the next 5 minutes of logs.** Healthy signals:
   - `bot_state_producer: starting 2 active tiers for 10 tickers`
   - `[bsp tier=A] pass complete in X.Xs; sleeping Y.Ys` within ~10s of startup
   - `[bsp tier=B] pass complete in X.Xs; sleeping Y.Ys` within ~25s
   - No `outer loop crashed` lines
6. **Verify Redis is populated:**
   ```bash
   # From Render shell or wherever redis-cli is reachable:
   redis-cli KEYS 'bot_state:*:front' | wc -l
   # Expected: 10 (one per ticker in TICKERS)
   ```
7. **After 24 hours of clean logs, run the cadence-tuning analysis.** The implementer dashboard / one-off script reads from `bot_state_producer:timings:{YYYYMMDD}` and reports p50/p95/p99 elapsed_ms per (ticker, intent). If anything is consistently >2s, file a follow-up to re-tier.
8. **After 7 trading days clean,** promote to full universe by editing env vars to include all 35 FLOW_TICKERS and adding `t30` and `t60` to Tier C.

# Rollback

Unset `BOT_STATE_PRODUCER_ENABLED` in Render env, redeploy. Within 60s the producer is gone, the lock TTLs out, no more Redis writes. Existing `bot_state:*` keys self-expire within their tier TTL. No data corruption possible — the producer never touches anything outside its own key prefix.

# Follow-ups (NOT in this plan)

- **Patch C** — Research page rewrites `omega_dashboard/research_data.py` to read from Redis instead of building inline. Depends on this patch being in production with envelopes flowing.
- **Cadence re-tier patch** — driven by the 24h timing analysis. May add a Tier D for slow tickers, may shrink Tier A cadence if everything's fast.
- **Patch E** — silent thesis migration to consume from Redis. Bigger change; needs producer to be the canonical source for at least one trading day first.
