"""Tests for omega_dashboard.em_data.

# v11.7 (Patch M.8): hermetic — no network, no Schwab, no Telegram.
# Mocks app._compute_em_brief_data so each test owns its own scenario.
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock


def _kill_switch_env(value):
    """Helper to set/unset the EM_BRIEF_DASHBOARD_ENABLED env var."""
    if value is None:
        os.environ.pop("EM_BRIEF_DASHBOARD_ENABLED", None)
    else:
        os.environ["EM_BRIEF_DASHBOARD_ENABLED"] = value


def test_get_em_brief_returns_disabled_when_kill_switch_set():
    _kill_switch_env("false")
    try:
        from omega_dashboard import em_data
        result = em_data.get_em_brief("SPY")
        assert result["available"] is False
        assert "disabled" in (result["error"] or "").lower()
    finally:
        _kill_switch_env(None)


def test_get_em_brief_returns_data_when_compute_succeeds():
    _kill_switch_env(None)
    fake_data = {
        "ticker": "SPY", "session_resolved": "manual",
        "expiration": "2026-05-13", "target_date_str": "2026-05-13",
        "hours_for_em": 6.5, "session_emoji": "🌆",
        "session_label": "Test", "horizon_note": "test",
        "iv": 0.18, "spot": 588.50,
        "eng": {"gex": 12.4, "dex": -3.1, "vanna": 1.2, "charm": -0.8,
                "flip_price": 585.0, "regime": {}},
        "walls": {"call_wall": 595, "put_wall": 580, "gamma_wall": 590},
        "skew": None, "pcr": None, "vix": {"vix": 18.5},
        "v4_result": {}, "vol_regime": {"regime": "NORMAL"},
        "em": {"bull_1sd": 590.0, "bear_1sd": 585.0,
               "bull_2sd": 595.0, "bear_2sd": 580.0,
               "range_1sd": 5.0, "range_2sd": 15.0},
        "bias": {"direction": "SLIGHT BULLISH", "score": 1, "max_score": 14,
                 "verdict": "NEUTRAL — wait", "signals": [],
                 "up_count": 1, "down_count": 0, "neu_count": 0,
                 "na_count": 13, "n_signals": 1, "strength": ""},
        "cagf": None, "dte_rec": None,
        "available_sections": ["header", "em_range", "walls", "bias",
                               "dealer_flow", "vol_regime"],
    }
    with patch("app._compute_em_brief_data", return_value=fake_data):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("SPY")
    assert result["available"] is True
    assert result["error"] is None
    assert result["partial_warning"] is None  # all required sections present
    assert result["data"]["ticker"] == "SPY"
    assert result["computed_at_ct"]  # CT timestamp string


def test_get_em_brief_renders_partial_warning_when_sections_missing():
    """When available_sections doesn't include all REQUIRED_SECTIONS,
    partial_warning should be set (drives the warning banner — QC fix #2)."""
    fake_data = {
        "ticker": "FAKE", "session_resolved": "manual",
        "iv": 0.20, "spot": 100.0, "expiration": "2026-05-15",
        "target_date_str": "2026-05-15", "hours_for_em": 1.0,
        "session_emoji": "🔔", "session_label": "Test", "horizon_note": "x",
        "eng": None, "walls": None, "skew": None, "pcr": None,
        "vix": {"vix": 20}, "v4_result": {}, "vol_regime": {"regime": "NORMAL"},
        "em": {"bull_1sd": 102, "bear_1sd": 98, "bull_2sd": 104, "bear_2sd": 96,
               "range_1sd": 4, "range_2sd": 8},
        "bias": None, "cagf": None, "dte_rec": None,
        "available_sections": ["header", "em_range"],   # missing walls + bias
    }
    with patch("app._compute_em_brief_data", return_value=fake_data):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("FAKE")
    assert result["available"] is True
    assert result["partial_warning"] is not None
    assert "walls" in result["partial_warning"]
    assert "bias" in result["partial_warning"]


def test_get_em_brief_returns_unavailable_when_compute_returns_none():
    with patch("app._compute_em_brief_data", return_value=None):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("ZZZZ")
    assert result["available"] is False
    assert "no option chain" in (result["error"] or "").lower()


def test_get_em_brief_swallows_compute_exceptions():
    with patch("app._compute_em_brief_data",
               side_effect=RuntimeError("schwab boom")):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("SPY")
    assert result["available"] is False
    assert "RuntimeError" in (result["error"] or "")


def test_start_refresh_all_returns_disabled_when_kill_switch_set():
    _kill_switch_env("false")
    try:
        from omega_dashboard import em_data
        result = em_data.start_refresh_all()
        assert result["job_id"] is None
        assert "disabled" in (result["error"] or "").lower()
    finally:
        _kill_switch_env(None)


def test_start_refresh_all_returns_no_redis_when_unavailable():
    _kill_switch_env(None)
    with patch("omega_dashboard.em_data._redis", return_value=None):
        from omega_dashboard import em_data
        result = em_data.start_refresh_all()
    assert result["job_id"] is None
    assert "Redis" in (result["error"] or "")


def test_get_refresh_progress_returns_not_found_for_unknown_job():
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {}
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("00000000-0000-4000-a000-000000000000")
    assert result["found"] is False


def test_get_refresh_progress_decodes_redis_hash_correctly():
    """Redis-py returns bytes; the helper must decode and coerce ints."""
    started_ms = int(time.time() * 1000) - 30_000  # 30s ago
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {
        b"started_at": str(started_ms).encode(),
        b"total": b"35",
        b"completed": b"12",
        b"errors": b"1",
    }
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("a-job-id")
    assert result["found"] is True
    assert result["total"] == 35
    assert result["completed"] == 12
    assert result["errors"] == 1
    assert result["finished_at"] is None
    assert result["elapsed_seconds"] >= 29  # ~30s ago, allow 1s slack
    assert result["slow_caption"] is False  # only 30s elapsed


def test_get_refresh_progress_sets_slow_caption_after_60s():
    started_ms = int(time.time() * 1000) - 90_000  # 90s ago
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {
        b"started_at": str(started_ms).encode(),
        b"total": b"35", b"completed": b"22", b"errors": b"0",
    }
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("a-job-id")
    assert result["slow_caption"] is True


def test_get_refresh_progress_shows_finished_when_complete():
    started_ms = int(time.time() * 1000) - 100_000
    finished_ms = started_ms + 95_000
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {
        b"started_at": str(started_ms).encode(),
        b"total": b"35", b"completed": b"35", b"errors": b"0",
        b"finished_at": str(finished_ms).encode(),
    }
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("a-job-id")
    assert result["finished_at"] == finished_ms
    assert result["slow_caption"] is False  # finished, no caption


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL: {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
