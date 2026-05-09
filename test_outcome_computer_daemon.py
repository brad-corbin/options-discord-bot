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


def test_compute_outcome_at_horizon_long_call():
    """Long call: outcome at 1h shows realized pnl based on track samples
    in the window."""
    from outcome_computer_daemon import _compute_outcome_for_horizon
    track = [
        # (elapsed_seconds, structure_mark, structure_pnl_pct)
        (60,   2.92, 2.46),
        (300,  3.40, 19.30),    # within window
        (600,  3.20, 12.28),
        (3600, 3.10, 8.77),    # at 1h (window boundary)
    ]
    structure = {"type": "long_call", "entry_mark": 2.85}
    out = _compute_outcome_for_horizon(structure=structure,
                                       horizon_seconds=3600,
                                       track=track,
                                       pt_levels=(0.20, 0.50, 1.00))
    # Closing pnl is the last sample at-or-before horizon
    assert out["pnl_pct"] == 8.77
    assert out["structure_mark"] == 3.10
    # MFE = max pnl in window
    assert abs(out["max_favorable_pct"] - 19.30) < 0.5
    # MAE = min pnl in window
    assert out["max_adverse_pct"] is not None


def test_compute_outcome_hit_pt_flags():
    """Hit-PT flags reflect 'did the path touch PT anywhere within window'."""
    from outcome_computer_daemon import _compute_outcome_for_horizon
    track = [
        (60,   2.92, 2.46),
        (300,  3.50, 22.81),    # touches PT1 (20%)
        (600,  3.20, 12.28),
        (3600, 3.10, 8.77),    # closes below PT1
    ]
    structure = {"type": "long_call", "entry_mark": 2.85}
    out = _compute_outcome_for_horizon(structure=structure,
                                       horizon_seconds=3600,
                                       track=track,
                                       pt_levels=(0.20, 0.50, 1.00))
    # PT1 was touched at 22.81% — flag should be 1 even though pnl closed at 8.77
    assert out["hit_pt1"] == 1
    assert out["hit_pt2"] == 0  # never reached 50%
    assert out["hit_pt3"] == 0  # never reached 100%


def test_compute_writes_outcomes_for_crossed_horizons():
    """Integration: alert older than 1h with track samples → outcomes for
    5min/15min/30min/1h are written."""
    tmpdir, db = _setup()
    try:
        from alert_recorder import record_alert, record_track_sample
        from outcome_computer_daemon import run_single_pass

        # Record an alert. We'll back-date its fired_at via direct SQL.
        alert_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={"type": "long_call", "entry_mark": 2.85,
                                 "strike": 590.0, "expiry": "2026-05-15"},
            suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        # Back-date the fire time so 1h horizon has been crossed.
        ninety_min_micros = 90 * 60 * 1_000_000
        conn = sqlite3.connect(db)
        conn.execute("UPDATE alerts SET fired_at = fired_at - ? WHERE alert_id = ?",
                     (ninety_min_micros, alert_id))
        conn.commit()
        conn.close()

        # Insert some track samples covering 0-90min.
        for elapsed, mark, pct in [(60, 2.92, 2.46),
                                    (300, 3.40, 19.30),
                                    (600, 3.20, 12.28),
                                    (3600, 3.10, 8.77),
                                    (5400, 3.00, 5.26)]:
            record_track_sample(
                alert_id=alert_id, elapsed_seconds=elapsed,
                underlying_price=590.0, structure_mark=mark,
                structure_pnl_pct=pct, structure_pnl_abs=mark - 2.85,
                market_state="rth",
            )

        # Run the outcome computer.
        run_single_pass()

        # Verify outcomes for 5min/15min/30min/1h written.
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT horizon, pnl_pct, hit_pt1 FROM alert_outcomes "
            "WHERE alert_id = ? ORDER BY horizon", (alert_id,)
        ).fetchall()
        conn.close()
        horizons_written = {r[0] for r in rows}
        for h in ("5min", "15min", "30min", "1h"):
            assert h in horizons_written, f"missing horizon {h}: got {horizons_written}"
        # 4h horizon should NOT be written (only 90 minutes elapsed)
        assert "4h" not in horizons_written
    finally:
        _teardown(tmpdir)


def test_compute_skips_alerts_with_no_track_samples():
    """If an alert has no price_track rows, no outcomes are written."""
    tmpdir, db = _setup()
    try:
        from alert_recorder import record_alert
        from outcome_computer_daemon import run_single_pass
        alert_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={"type": "long_call", "entry_mark": 2.85,
                                 "strike": 590.0, "expiry": "2026-05-15"},
            suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        # Back-date so horizons are crossed
        conn = sqlite3.connect(db)
        conn.execute("UPDATE alerts SET fired_at = fired_at - ? WHERE alert_id = ?",
                     (3 * 24 * 60 * 60 * 1_000_000, alert_id))
        conn.commit()
        conn.close()

        run_single_pass()  # no track samples — should skip

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM alert_outcomes "
                            "WHERE alert_id = ?", (alert_id,)).fetchall()
        conn.close()
        assert rows[0][0] == 0, "should not write outcomes when no track samples exist"
    finally:
        _teardown(tmpdir)


def test_compute_is_idempotent():
    """Re-running the same pass over the same data produces identical rows."""
    tmpdir, db = _setup()
    try:
        from alert_recorder import record_alert, record_track_sample
        from outcome_computer_daemon import run_single_pass
        alert_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={"type": "long_call", "entry_mark": 2.85,
                                 "strike": 590.0, "expiry": "2026-05-15"},
            suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        conn = sqlite3.connect(db)
        conn.execute("UPDATE alerts SET fired_at = fired_at - ? WHERE alert_id = ?",
                     (90 * 60 * 1_000_000, alert_id))
        conn.commit()
        conn.close()
        record_track_sample(alert_id=alert_id, elapsed_seconds=60,
                            underlying_price=590.0, structure_mark=2.92,
                            structure_pnl_pct=2.46, structure_pnl_abs=0.07,
                            market_state="rth")
        record_track_sample(alert_id=alert_id, elapsed_seconds=3600,
                            underlying_price=591.0, structure_mark=3.10,
                            structure_pnl_pct=8.77, structure_pnl_abs=0.25,
                            market_state="rth")

        # First pass
        run_single_pass()
        conn = sqlite3.connect(db)
        rows1 = conn.execute(
            "SELECT horizon, pnl_pct, hit_pt1 FROM alert_outcomes "
            "ORDER BY horizon"
        ).fetchall()
        conn.close()

        # Second pass — should produce identical rows
        run_single_pass()
        conn = sqlite3.connect(db)
        rows2 = conn.execute(
            "SELECT horizon, pnl_pct, hit_pt1 FROM alert_outcomes "
            "ORDER BY horizon"
        ).fetchall()
        conn.close()

        assert rows1 == rows2, f"idempotency broken:\n{rows1}\n{rows2}"
    finally:
        _teardown(tmpdir)


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
