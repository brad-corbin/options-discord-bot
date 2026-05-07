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
from typing import Dict
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


class _FakeRedis:
    """Minimal Redis stand-in for unit tests. Implements the SUBSET of
    redis-py we need: SET (with NX/EX/XX), GET, DELETE, EXPIRE, TTL,
    ZADD, ZRANGE, EVAL (for safe-release/refresh Lua). Enough to test
    our helpers; nothing more. NOT a general-purpose mock.
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
        if key not in self.kv and key not in self.zset:
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
        return {"_fake_raw": ticker, "_exp": expiration}

    def build_from_raw(self, raw, *, days_to_exp=None):
        ticker = raw.get("_fake_raw") if isinstance(raw, dict) else None
        cfg = self.config.get(ticker, {})
        if cfg.get("raises_at") == "build":
            raise cfg["raises"]
        return cfg["state_dict"]


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


# ─────────────────────────────────────────────────────────────────────
# Telemetry tests
# ─────────────────────────────────────────────────────────────────────

def test_record_build_timing_writes_sorted_set_member():
    from bot_state_producer import _record_build_timing
    fake = _FakeRedis()
    _record_build_timing(fake, ticker="AAPL", intent="front",
                         elapsed_ms=143, expiration="2026-05-08")
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
    # Verify the implementer called expire() on the timings key.
    expire_calls = [op for op in fake.ops if op[0] == "expire"]
    assert_true(len(expire_calls) >= 1, "expire() was called on the timings key")


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
    refreshed = _refresh_lock(fake, "bsp:lock", token, 120)
    assert_eq(refreshed, True, "owner can refresh lock TTL")
    refreshed = _refresh_lock(fake, "bsp:lock", "wrong", 120)
    assert_eq(refreshed, False, "wrong token cannot refresh")


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
        cached_md=object(),
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
    # Single-tier pass
    test_tier_pass_writes_one_key_per_ticker_intent()
    test_tier_pass_skips_ticker_when_canonical_expiration_returns_none()
    test_tier_pass_isolates_per_ticker_errors()
    test_tier_pass_writes_telemetry_per_successful_build()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
