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


def test_compute_evaluation_outcome_bull_above_pt1():
    """G.15: Bull v2_5d, underlying moved +0.6% — hits pt1 (0.5%), not pt2 (1.0%)."""
    from outcome_computer_daemon import _compute_evaluation_outcome_for_horizon
    track = [
        (60, 100.20),
        (300, 100.40),
        (600, 100.55),
        (3600, 100.60),
    ]
    out = _compute_evaluation_outcome_for_horizon(
        spot_at_fire=100.0,
        direction="bull",
        horizon_seconds=3600,
        track=track,
    )
    assert abs(out["pnl_pct"] - 0.60) < 0.001, f"pnl_pct={out['pnl_pct']}"
    assert out["underlying_price"] == 100.60
    assert out["structure_mark"] is None, "evaluation alerts have no structure"
    assert out["hit_pt1"] == 1, "0.60% > pt1 threshold 0.5%"
    assert out["hit_pt2"] == 0, "0.60% < pt2 threshold 1.0%"
    assert out["hit_pt3"] == 0
    assert abs(out["max_favorable_pct"] - 0.60) < 0.001
    # MAE in this bull-only-rising track is the smallest positive move (+0.20%)
    assert abs(out["max_adverse_pct"] - 0.20) < 0.001


def test_compute_evaluation_outcome_bull_negative_no_pts():
    """G.15: Bull v2_5d, underlying went down -1.0% — direction wrong, no pt
    hits, mae=-1.0%."""
    from outcome_computer_daemon import _compute_evaluation_outcome_for_horizon
    track = [
        (60, 99.80),
        (300, 99.50),
        (600, 99.20),
        (3600, 99.00),
    ]
    out = _compute_evaluation_outcome_for_horizon(
        spot_at_fire=100.0,
        direction="bull",
        horizon_seconds=3600,
        track=track,
    )
    assert abs(out["pnl_pct"] - (-1.0)) < 0.001, f"pnl_pct={out['pnl_pct']}"
    assert out["hit_pt1"] == 0
    assert out["hit_pt2"] == 0
    assert out["hit_pt3"] == 0
    assert abs(out["max_adverse_pct"] - (-1.0)) < 0.001
    # Best (least-bad) sample was -0.20% — still negative for a bull alert
    assert abs(out["max_favorable_pct"] - (-0.20)) < 0.001


def test_compute_evaluation_outcome_bear_correct_direction():
    """G.15: Bear v2_5d, underlying went down -2.0% — sign flips, +2.0% pnl,
    all pt-hit flags fire."""
    from outcome_computer_daemon import _compute_evaluation_outcome_for_horizon
    track = [
        (60, 99.50),
        (300, 99.00),
        (600, 98.50),
        (3600, 98.00),
    ]
    out = _compute_evaluation_outcome_for_horizon(
        spot_at_fire=100.0,
        direction="bear",
        horizon_seconds=3600,
        track=track,
    )
    assert abs(out["pnl_pct"] - 2.0) < 0.001, f"pnl_pct={out['pnl_pct']}"
    assert out["hit_pt1"] == 1, "bear-adjusted +0.5% touched (at sample 1: 99.50)"
    assert out["hit_pt2"] == 1, "bear-adjusted +1.0% touched (at sample 2: 99.00)"
    assert out["hit_pt3"] == 1, "bear-adjusted +2.0% touched (at sample 4: 98.00)"
    assert abs(out["max_favorable_pct"] - 2.0) < 0.001
    assert abs(out["max_adverse_pct"] - 0.5) < 0.001


def test_compute_evaluation_outcome_no_samples_returns_null():
    """G.15: empty track → all-NULL result, no division attempts."""
    from outcome_computer_daemon import _compute_evaluation_outcome_for_horizon
    out = _compute_evaluation_outcome_for_horizon(
        spot_at_fire=100.0,
        direction="bull",
        horizon_seconds=3600,
        track=[],
    )
    assert out["pnl_pct"] is None
    assert out["underlying_price"] is None
    assert out["hit_pt1"] == 0
    assert out["hit_pt2"] == 0
    assert out["hit_pt3"] == 0
    assert out["max_favorable_pct"] is None
    assert out["max_adverse_pct"] is None


def test_run_single_pass_routes_v2_5d_through_evaluation_path():
    """G.15 integration: v2_5d alert with underlying-only track samples gets
    real (non-NULL) outcomes at crossed horizons."""
    tmpdir, db = _setup()
    os.environ["RECORDER_V25D_ENABLED"] = "true"
    try:
        from alert_recorder import record_alert, record_track_sample
        from outcome_computer_daemon import run_single_pass
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="LLY", classification="GRADE_A", direction="bull",
            suggested_structure={"type": "evaluation"},
            suggested_dte=None, spot_at_fire=968.32,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        # Back-date so 1h horizon has been crossed.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE alerts SET fired_at = fired_at - ? WHERE alert_id = ?",
                     (90 * 60 * 1_000_000, alert_id))
        conn.commit()
        conn.close()
        # Track samples: underlying climbing from 968.32 to 980.00 (+1.2%)
        for elapsed, u in [(60, 970.00), (300, 974.50), (600, 977.00),
                           (3600, 980.00), (5400, 978.50)]:
            record_track_sample(
                alert_id=alert_id, elapsed_seconds=elapsed,
                underlying_price=u, structure_mark=None,
                structure_pnl_pct=None, structure_pnl_abs=None,
                market_state="rth",
            )

        run_single_pass()

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT horizon, pnl_pct, underlying_price, hit_pt1, hit_pt2, hit_pt3, "
            "max_favorable_pct, structure_mark "
            "FROM alert_outcomes WHERE alert_id = ? ORDER BY horizon",
            (alert_id,)
        ).fetchall()
        conn.close()
        assert rows, "v2_5d outcomes should be written"
        by_horizon = {r[0]: r for r in rows}
        # 1h sample is at u=980.00 → (980 - 968.32) / 968.32 * 100 * 1.0 ≈ +1.206%
        h1h = by_horizon["1h"]
        assert h1h[1] is not None, f"pnl_pct at 1h must not be NULL: {h1h}"
        assert abs(h1h[1] - 1.206) < 0.05, f"1h pnl ~+1.2%, got {h1h[1]}"
        assert h1h[2] == 980.00, f"underlying_price at 1h should equal closing sample"
        assert h1h[3] == 1, "pt1 hit (>0.5%)"
        assert h1h[4] == 1, "pt2 hit (>1.0%)"
        assert h1h[5] == 0, "pt3 not hit (<2.0%)"
        assert h1h[7] is None, "structure_mark must be NULL for evaluation alerts"
    finally:
        os.environ.pop("RECORDER_V25D_ENABLED", None)
        _teardown(tmpdir)


def test_run_single_pass_routes_long_call_through_structure_path():
    """G.15 regression: long_call alerts must still use the structure-mark
    path (not the new evaluation path). PT thresholds are 20/50/100%, not
    the v2_5d 0.5/1/2%."""
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
        # Track shows option mark climbing +8.77% — within LCB's structure path
        # would NOT hit pt1=20%. If the v2_5d evaluation path were wrongly used
        # (pt1=0.5%), pt1 would fire — proves routing.
        for elapsed, mark, pct in [(60, 2.92, 2.46),
                                    (300, 3.00, 5.26),
                                    (600, 3.05, 7.02),
                                    (3600, 3.10, 8.77)]:
            record_track_sample(
                alert_id=alert_id, elapsed_seconds=elapsed,
                underlying_price=590.0, structure_mark=mark,
                structure_pnl_pct=pct, structure_pnl_abs=mark - 2.85,
                market_state="rth",
            )

        run_single_pass()

        conn = sqlite3.connect(db)
        h1h = conn.execute(
            "SELECT pnl_pct, structure_mark, hit_pt1 FROM alert_outcomes "
            "WHERE alert_id = ? AND horizon = '1h'", (alert_id,)
        ).fetchone()
        conn.close()
        assert h1h is not None, "1h outcome must exist for LCB alert"
        # pnl_pct is the structure path's structure_pnl_pct (+8.77%), NOT
        # the v2_5d underlying %.
        assert abs(h1h[0] - 8.77) < 0.01, (
            f"LCB pnl_pct must use structure path (8.77), got {h1h[0]} "
            f"— if this is the underlying % move, the v2_5d branch was taken"
        )
        assert h1h[1] == 3.10, "structure_mark must be populated for LCB"
        # 8.77% < 20% LCB pt1 threshold — should be 0
        assert h1h[2] == 0, (
            f"LCB pt1 (20%) must not fire at 8.77% — got {h1h[2]} "
            f"(if 1, the 0.5% evaluation threshold was wrongly used)"
        )
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
        test_compute_evaluation_outcome_bull_above_pt1,
        test_compute_evaluation_outcome_bull_negative_no_pts,
        test_compute_evaluation_outcome_bear_correct_direction,
        test_compute_evaluation_outcome_no_samples_returns_null,
        test_run_single_pass_routes_v2_5d_through_evaluation_path,
        test_run_single_pass_routes_long_call_through_structure_path,
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
