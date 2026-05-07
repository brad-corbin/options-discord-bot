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
