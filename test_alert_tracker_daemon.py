"""Tests for alert_tracker_daemon. Patch G.7."""
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
    try:
        import alert_recorder
        with alert_recorder._conn_lock:
            for c in alert_recorder._conn_cache.values():
                try:
                    c.close()
                except Exception:
                    pass
            alert_recorder._conn_cache.clear()
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_should_sample_at_60s_in_first_hour():
    """Cadence rule: 60s in 0-1h."""
    from alert_tracker_daemon import _should_sample
    assert _should_sample(elapsed_seconds=0,    last_sample_elapsed=None) is True
    assert _should_sample(elapsed_seconds=59,   last_sample_elapsed=0)    is False
    assert _should_sample(elapsed_seconds=60,   last_sample_elapsed=0)    is True
    assert _should_sample(elapsed_seconds=120,  last_sample_elapsed=60)   is True
    # 1-4h bucket: 5min cadence
    assert _should_sample(elapsed_seconds=3700, last_sample_elapsed=3600) is False
    assert _should_sample(elapsed_seconds=3900, last_sample_elapsed=3600) is True


def test_should_sample_past_horizon_returns_false():
    """Stop sampling once past tracking horizon."""
    from alert_tracker_daemon import _should_sample
    # Past 3-day horizon
    assert _should_sample(elapsed_seconds=4 * 24 * 60 * 60,
                          last_sample_elapsed=3 * 24 * 60 * 60,
                          horizon_seconds=3 * 24 * 60 * 60) is False


def test_compute_pnl_for_long_call():
    from alert_tracker_daemon import _compute_pnl
    abs_, pct = _compute_pnl(
        structure={"type": "long_call", "entry_mark": 2.85},
        current_mark=3.10,
    )
    assert abs(abs_ - 0.25) < 0.001
    assert abs(pct - (0.25 / 2.85 * 100)) < 0.001


def test_compute_pnl_for_credit_spread():
    """Credit spread: PnL is positive when current mark < credit (decayed)."""
    from alert_tracker_daemon import _compute_pnl
    abs_, pct = _compute_pnl(
        structure={"type": "bull_put", "credit": 0.85, "width": 5.0},
        current_mark=0.30,
    )
    assert abs_ > 0
    # Risk = width - credit = 4.15. PnL pct = (credit - current) / risk * 100.
    expected_pct = (0.85 - 0.30) / (5.0 - 0.85) * 100
    assert abs(pct - expected_pct) < 0.001


def test_fetch_structure_mark_reads_short_long_keys_for_credit_spread():
    """Regression for Patch G.11: v8.4 CREDIT writes spread legs as
    'short' and 'long', NOT 'short_strike' / 'long_strike'. Prior
    versions of _fetch_structure_mark read the wrong keys and silently
    returned None for every credit alert, leaving alert_price_track
    empty for bull_put / bear_call.

    Fixture mirrors the exact shape pulled from production DB for the
    MSFT bull_put rows."""
    from alert_tracker_daemon import _fetch_structure_mark

    # Capture OCCs requested by the tracker so we can prove it built
    # them from the 'short' / 'long' keys (and not silently failed).
    requested = []

    class _FakeStore:
        def get_live_premium(self, occ, **kwargs):
            requested.append(occ)
            # short leg priced higher than long leg → positive net mid
            return 1.50 if "00405000" in occ else 0.30

    with mock.patch("schwab_stream.get_option_store", return_value=_FakeStore()):
        mark = _fetch_structure_mark(
            structure={
                "type": "bull_put",
                "short": 405.0, "long": 400.0,
                "width": 5.0, "credit": 1.20,
                "expiry": "2026-05-15",
            },
            ticker="MSFT",
        )

    assert mark is not None, (
        "Tracker returned None — likely still reading wrong dict keys "
        "(should be 'short'/'long', not 'short_strike'/'long_strike')."
    )
    assert abs(mark - 1.20) < 0.001, f"Expected 1.50-0.30=1.20, got {mark}"
    assert len(requested) == 2
    assert any("00405000" in occ for occ in requested), requested
    assert any("00400000" in occ for occ in requested), requested


def test_fetch_structure_mark_long_call_passes_lenient_stale_threshold():
    """G.13.1: tracker daemon must read with a 10-minute staleness window so
    OTM long calls with infrequent ticks still produce marks. The default 60s
    is too strict — ticks routinely arrive 60-300s apart on low-activity
    options, leaving alert_price_track empty between updates.
    """
    from alert_tracker_daemon import _fetch_structure_mark

    captured_kwargs = []

    class _FakeStore:
        def get_live_premium(self, occ, **kwargs):
            captured_kwargs.append(kwargs)
            return 2.92

    with mock.patch("schwab_stream.get_option_store", return_value=_FakeStore()):
        mark = _fetch_structure_mark(
            structure={"type": "long_call", "strike": 590.0,
                       "expiry": "2026-05-15", "entry_mark": 2.85},
            ticker="SPY",
        )

    assert mark == 2.92
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("stale_threshold") == 600, (
        f"Tracker must pass stale_threshold=600 (10 min); got {captured_kwargs[0]}. "
        "This is the G.13.1 fix — without the lenient threshold, the store's "
        "default 60s rejects ticks arriving 60-300s apart and the tracker "
        "writes NULL marks even when fresh quotes exist seconds before."
    )


def test_fetch_structure_mark_credit_spread_passes_lenient_stale_threshold():
    """G.13.1: both legs of a credit spread must read with stale_threshold=600.

    OTM spread legs (e.g. XLF 52/54.5C, XLV 143/140.5P) are the worst-case
    for staleness — both legs decay slowly and tick infrequently. Pre-G.13.1
    coverage was 0.3% in market hours. The whole point is to keep these
    reads working when ticks are sparse.
    """
    from alert_tracker_daemon import _fetch_structure_mark

    captured = []

    class _FakeStore:
        def get_live_premium(self, occ, **kwargs):
            captured.append((occ, kwargs))
            return 1.50 if "00405000" in occ else 0.30

    with mock.patch("schwab_stream.get_option_store", return_value=_FakeStore()):
        mark = _fetch_structure_mark(
            structure={
                "type": "bull_put",
                "short": 405.0, "long": 400.0,
                "width": 5.0, "credit": 1.20,
                "expiry": "2026-05-15",
            },
            ticker="MSFT",
        )

    assert mark is not None
    assert abs(mark - 1.20) < 0.001
    assert len(captured) == 2, "both legs must be queried"
    for occ, kwargs in captured:
        assert kwargs.get("stale_threshold") == 600, (
            f"Spread leg {occ!r} was queried without stale_threshold=600: "
            f"kwargs={kwargs}. Both legs must use the lenient threshold."
        )


def test_fetch_structure_mark_returns_none_when_legs_missing():
    """Defensive: structure with 'type': 'bull_put' but missing short/long
    must return None rather than raising or building bogus OCCs."""
    from alert_tracker_daemon import _fetch_structure_mark

    class _NeverCalledStore:
        def get_live_premium(self, occ, **kwargs):
            raise AssertionError(f"store should not be queried; got {occ!r}")

    with mock.patch("schwab_stream.get_option_store", return_value=_NeverCalledStore()):
        mark = _fetch_structure_mark(
            structure={"type": "bull_put", "expiry": "2026-05-15"},
            ticker="MSFT",
        )
    assert mark is None


def test_run_single_pass_writes_track_row():
    """Integration: with one active alert and stubbed market data, one pass
    writes one alert_price_track row."""
    tmpdir, db, alert_id = _setup_with_alert()
    try:
        with mock.patch("alert_tracker_daemon._fetch_underlying_price",
                        return_value=588.50), \
             mock.patch("alert_tracker_daemon._fetch_structure_mark",
                        return_value=2.92), \
             mock.patch("alert_tracker_daemon._market_state_now",
                        return_value="rth"):
            from alert_tracker_daemon import run_single_pass
            run_single_pass()
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT alert_id, structure_mark, market_state FROM alert_price_track"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == alert_id
        assert rows[0][1] == 2.92
        assert rows[0][2] == "rth"
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
        # Underlying price fetch failure is caught per-alert; structure_mark and
        # market_state fetches continue, and record_track_sample is always called.
        # Result: 1 row with underlying_price=NULL.
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM alert_price_track").fetchall()
        assert rows[0][0] == 1, "Daemon should record 1 row even with underlying price fetch failure"
        conn.close()
    finally:
        _teardown(tmpdir)


if __name__ == "__main__":
    tests = [
        test_should_sample_at_60s_in_first_hour,
        test_should_sample_past_horizon_returns_false,
        test_compute_pnl_for_long_call,
        test_compute_pnl_for_credit_spread,
        test_fetch_structure_mark_reads_short_long_keys_for_credit_spread,
        test_fetch_structure_mark_long_call_passes_lenient_stale_threshold,
        test_fetch_structure_mark_credit_spread_passes_lenient_stale_threshold,
        test_fetch_structure_mark_returns_none_when_legs_missing,
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
            import traceback
            print(f"FAIL: {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
