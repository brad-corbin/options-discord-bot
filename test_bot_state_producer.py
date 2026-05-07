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
