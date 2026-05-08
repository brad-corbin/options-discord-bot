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
    env = _make_envelope()
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


def test_load_snapshot_warming_up_on_non_dict_json():
    """JSON-valid but non-dict payload (list, scalar, null) → warming up.
    Defends the 'never propagates exceptions' contract against an unusual
    Redis payload shape from a future bug or a manual SET."""
    from omega_dashboard.research_data import _load_snapshot_from_redis, KEY_PREFIX
    fake = _FakeRedis()
    # Each of these is valid JSON but won't have .get() — would crash
    # the consumer pre-fix.
    for payload, label in [
        ("[1,2,3]", "list"),
        ("42", "int"),
        ('"hello"', "string"),
        ("null", "null"),
    ]:
        fake.kv.clear()
        fake.set(f"{KEY_PREFIX}AAPL:front", payload)
        snap = _load_snapshot_from_redis("AAPL", "front", redis_client=fake)
        assert_eq(snap.warming_up, True,
                  f"non-dict JSON ({label}) → warming up")
        assert_true("not a dict" in (snap.error or ""),
                    f"reason mentions 'not a dict' for {label}")


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


# ─────────────────────────────────────────────────────────────────────
# Patch D.1: DTE helpers + multi-intent walls reader
# ─────────────────────────────────────────────────────────────────────

def test_compute_dte_days_basic():
    from omega_dashboard.research_data import _compute_dte_days
    from datetime import date
    today = date(2026, 5, 7)
    assert_eq(_compute_dte_days("2026-05-07", today=today), 0, "today → 0")
    assert_eq(_compute_dte_days("2026-05-08", today=today), 1, "tomorrow → 1")
    assert_eq(_compute_dte_days("2026-05-15", today=today), 8, "8 days out")
    assert_eq(_compute_dte_days("2026-06-12", today=today), 36, "monthly")


def test_compute_dte_days_handles_invalid_input():
    from omega_dashboard.research_data import _compute_dte_days
    assert_is_none(_compute_dte_days(None), "None input → None")
    assert_is_none(_compute_dte_days(""), "empty string → None")
    assert_is_none(_compute_dte_days("not-a-date"), "garbage → None")


def test_format_dte_tag_format():
    """Spec: '1DTE'/'8D'/'32D'/'61D' — ≤1 days → 'NDTE', >1 → 'ND'."""
    from omega_dashboard.research_data import _format_dte_tag
    assert_eq(_format_dte_tag(0), "0DTE", "0 days → '0DTE'")
    assert_eq(_format_dte_tag(1), "1DTE", "1 day → '1DTE'")
    assert_eq(_format_dte_tag(8), "8D", "8 days → '8D'")
    assert_eq(_format_dte_tag(32), "32D", "32 days → '32D'")
    assert_eq(_format_dte_tag(None), "—", "None → '—' fallback")


def test_load_walls_for_all_intents_full():
    """Producer has all 4 intents populated; reader returns 4 entries."""
    from omega_dashboard.research_data import (
        _load_walls_for_all_intents, INTENTS_ORDER, KEY_PREFIX,
    )
    fake = _FakeRedis()
    for intent, exp, cw, pw in [
        ("front", "2026-05-08", 590.0, 580.0),
        ("t7",    "2026-05-15", 595.0, 575.0),
        ("t30",   "2026-06-08", 600.0, 570.0),
        ("t60",   "2026-07-08", 610.0, 560.0),
    ]:
        env = _make_envelope(
            state_overrides={"ticker": "SPY", "call_wall": cw, "put_wall": pw, "gamma_wall": cw},
            intent=intent,
            expiration=exp,
        )
        fake.set(f"{KEY_PREFIX}SPY:{intent}", json.dumps(env))

    out = _load_walls_for_all_intents("SPY", redis_client=fake)
    assert_eq(len(out), 4, "four entries, one per intent")
    assert_eq([e["intent"] for e in out], list(INTENTS_ORDER), "ordered by INTENTS_ORDER")
    assert_eq(out[0]["call_wall"], 590.0, "front call_wall")
    assert_eq(out[1]["call_wall"], 595.0, "t7 call_wall")
    assert_eq(out[2]["expiration"], "2026-06-08", "t30 expiration carried")
    assert_true(out[3]["dte_days"] is not None and out[3]["dte_days"] > 30,
                "t60 dte_days computed")
    assert_true(out[0]["dte_tag"] in ("0DTE", "1DTE"),
                "front dte_tag populated as 'NDTE'")
    assert_true(out[1]["dte_tag"].endswith("D") and "DTE" not in out[1]["dte_tag"],
                "t7 dte_tag populated as 'ND'")


def test_load_walls_for_all_intents_partial():
    """Spec: missing t60 key → entry exists with None values, doesn't break."""
    from omega_dashboard.research_data import (
        _load_walls_for_all_intents, INTENTS_ORDER, KEY_PREFIX,
    )
    fake = _FakeRedis()
    # Populate only front; t7/t30/t60 missing.
    fake.set(
        f"{KEY_PREFIX}SPY:front",
        json.dumps(_make_envelope(
            state_overrides={"ticker": "SPY", "call_wall": 590.0, "put_wall": 580.0},
            intent="front", expiration="2026-05-08",
        )),
    )

    out = _load_walls_for_all_intents("SPY", redis_client=fake)
    assert_eq(len(out), 4, "four entries even with partial population")
    by_intent = {e["intent"]: e for e in out}
    assert_eq(by_intent["front"]["call_wall"], 590.0, "front populated")
    assert_is_none(by_intent["t7"]["call_wall"], "t7 missing → None")
    assert_is_none(by_intent["t30"]["expiration"], "t30 missing → expiration None")
    assert_is_none(by_intent["t60"]["dte_days"], "t60 missing → dte_days None")


def test_load_walls_for_all_intents_redis_unavailable():
    from omega_dashboard.research_data import _load_walls_for_all_intents, INTENTS_ORDER
    out = _load_walls_for_all_intents("SPY", redis_client=None)
    assert_eq(len(out), 4, "always returns 4 entries (one per intent)")
    assert_eq([e["intent"] for e in out], list(INTENTS_ORDER),
              "intents in canonical order")
    for e in out:
        assert_is_none(e["call_wall"], f"{e['intent']} call_wall is None")
        assert_is_none(e["expiration"], f"{e['intent']} expiration is None")
        assert_eq(e["dte_tag"], "—", f"{e['intent']} dte_tag is '—' fallback")


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
    test_load_snapshot_warming_up_on_non_dict_json()
    test_load_snapshot_warming_up_on_version_mismatch()
    test_load_snapshot_accepts_future_producer_version()
    # _research_data_from_redis
    test_research_data_from_redis_mixed_population()
    test_research_data_from_redis_aggregates_correctly()
    test_research_data_from_redis_returns_unavailable_when_no_redis()
    # Env-var dispatch
    test_research_data_uses_redis_when_env_var_on()
    test_research_data_uses_legacy_when_env_var_off()
    # Patch D.1: DTE helpers + multi-intent walls reader
    test_compute_dte_days_basic()
    test_compute_dte_days_handles_invalid_input()
    test_format_dte_tag_format()
    test_load_walls_for_all_intents_full()
    test_load_walls_for_all_intents_partial()
    test_load_walls_for_all_intents_redis_unavailable()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
