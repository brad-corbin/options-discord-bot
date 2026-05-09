"""Tests for alert_recorder.

Patch G.2 — recorder write side. No network, no Schwab, no Telegram.
Hermetic: each test uses its own temp DB. Uses RECORDER_DB_PATH override
to point the recorder at the temp file.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time


def _setup_recorder_env():
    """Returns (tmpdir, db_path). Sets RECORDER_DB_PATH and master/per-engine
    flags. Caller responsible for shutil.rmtree."""
    tmpdir = tempfile.mkdtemp(prefix="recorder_g2_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    os.environ["RECORDER_ENABLED"] = "true"
    os.environ["RECORDER_LCB_ENABLED"] = "true"
    os.environ["RECORDER_V25D_ENABLED"] = "true"
    os.environ["RECORDER_CREDIT_ENABLED"] = "true"
    os.environ["RECORDER_CONVICTION_ENABLED"] = "true"

    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown_recorder_env(tmpdir):
    for key in ("RECORDER_DB_PATH", "RECORDER_ENABLED", "RECORDER_LCB_ENABLED",
                "RECORDER_V25D_ENABLED", "RECORDER_CREDIT_ENABLED",
                "RECORDER_CONVICTION_ENABLED"):
        os.environ.pop(key, None)
    # Give the recorder's connection cache a chance to clear before deletion
    # (Windows holds file locks aggressively).
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


def test_record_alert_writes_alerts_row():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="long_call_burst",
            engine_version="long_call_burst@v8.4.2",
            ticker="SPY",
            classification="BURST_YES",
            direction="bull",
            suggested_structure={"type": "long_call", "strike": 590.0,
                                 "expiry": "2026-05-15", "entry_mark": 2.85},
            suggested_dte=6,
            spot_at_fire=588.30,
            canonical_snapshot={"producer_version": 1,
                                "convention_version": 2,
                                "intent": "front",
                                "expiration": "2026-05-08",
                                "state": {"ticker": "SPY", "spot": 588.30}},
            raw_engine_payload={"momentum_score": 7, "rsi": 62.0},
            features={"rsi": 62.0, "adx": 24.1, "regime": "BULL_BASE"},
            telegram_chat="main",
        )
        assert alert_id, "record_alert returned empty alert_id"

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT alert_id, engine, ticker, classification, "
                            "direction, posted_to_telegram FROM alerts").fetchall()
        assert rows == [(alert_id, "long_call_burst", "SPY", "BURST_YES",
                         "bull", 1)], f"unexpected row: {rows}"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_writes_features():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={"type": "long_call"},
            suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={
                "rsi": 47.3,
                "adx": 22.1,
                "regime": "BULL_BASE",
                "dealer_regime": "short_gamma_at_585",
                "is_pinned": True,
                "missing_field": None,
            },
            telegram_chat="main",
        )
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT feature_name, feature_value, feature_text "
            "FROM alert_features WHERE alert_id = ? ORDER BY feature_name",
            (alert_id,)
        ).fetchall()
        as_dict = {name: (val, text) for name, val, text in rows}
        assert as_dict["rsi"] == (47.3, None)
        assert as_dict["adx"] == (22.1, None)
        assert as_dict["regime"] == (None, "BULL_BASE")
        assert as_dict["is_pinned"] == (1.0, None)
        assert "missing_field" not in as_dict, "None features must be skipped"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_round_trip_canonical_snapshot():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        snap = {"producer_version": 1, "convention_version": 2,
                "intent": "front", "expiration": "2026-05-08",
                "state": {"ticker": "SPY", "gex_total": -1234.5,
                          "call_wall": 590.0, "put_wall": 580.0}}
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot=snap, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        conn = sqlite3.connect(db)
        (raw,) = conn.execute(
            "SELECT canonical_snapshot FROM alerts WHERE alert_id = ?",
            (alert_id,)
        ).fetchone()
        conn.close()
        assert json.loads(raw) == snap, "canonical_snapshot did not round-trip"
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_parent_alert_id_linkage():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        parent_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        child_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={}, suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
            parent_alert_id=parent_id,
        )
        conn = sqlite3.connect(db)
        (got,) = conn.execute(
            "SELECT parent_alert_id FROM alerts WHERE alert_id = ?",
            (child_id,)
        ).fetchone()
        assert got == parent_id, f"linkage broken: {got} != {parent_id}"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_master_gate_off_returns_none():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    os.environ["RECORDER_ENABLED"] = "false"
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert alert_id is None
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM alerts").fetchall()
        assert rows == [(0,)], "master gate off must not write"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_per_engine_gate_off_returns_none():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    os.environ["RECORDER_LCB_ENABLED"] = "false"
    try:
        alert_id = record_alert(
            engine="long_call_burst", engine_version="lcb@v1.0.0",
            ticker="SPY", classification="BURST_YES", direction="bull",
            suggested_structure={}, suggested_dte=6, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert alert_id is None
        v25d_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert v25d_id is not None
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_swallows_internal_exception():
    """Recorder failure NEVER raises into the engine."""
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        class Unserializable:
            pass
        out = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={"bad": Unserializable()},
            features={}, telegram_chat="main",
        )
        assert out is None or isinstance(out, str)
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_alert_id_is_uuid_v4():
    from alert_recorder import record_alert
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        assert len(alert_id) == 36
        assert alert_id[8] == "-" and alert_id[13] == "-"
        assert alert_id[18] == "-" and alert_id[23] == "-"
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_track_sample_writes_row():
    from alert_recorder import record_alert, record_track_sample
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        record_track_sample(alert_id=alert_id, elapsed_seconds=60,
                            underlying_price=588.50, structure_mark=2.92,
                            structure_pnl_pct=2.46, structure_pnl_abs=7.0,
                            market_state="rth")
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT alert_id, elapsed_seconds, underlying_price, "
            "structure_mark, market_state FROM alert_price_track"
        ).fetchall()
        assert rows == [(alert_id, 60, 588.50, 2.92, "rth")]
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


def test_record_outcome_writes_row_and_is_idempotent():
    from alert_recorder import record_alert, record_outcome
    tmpdir, db = _setup_recorder_env()
    try:
        alert_id = record_alert(
            engine="v2_5d", engine_version="v2_5d@v8.4.2",
            ticker="SPY", classification="GRADE_A", direction="bull",
            suggested_structure={}, suggested_dte=7, spot_at_fire=588.30,
            canonical_snapshot={}, raw_engine_payload={},
            features={}, telegram_chat="main",
        )
        record_outcome(alert_id=alert_id, horizon="1h",
                       outcome_at=1_700_000_000_000_000,
                       underlying_price=590.0, structure_mark=3.10,
                       pnl_pct=8.77, pnl_abs=25.0,
                       hit_pt1=1, hit_pt2=0, hit_pt3=0,
                       max_favorable_pct=12.0, max_adverse_pct=-3.0)
        record_outcome(alert_id=alert_id, horizon="1h",
                       outcome_at=1_700_000_000_000_000,
                       underlying_price=591.0, structure_mark=3.20,
                       pnl_pct=12.28, pnl_abs=35.0,
                       hit_pt1=1, hit_pt2=1, hit_pt3=0,
                       max_favorable_pct=15.0, max_adverse_pct=-3.0)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT pnl_pct, pnl_abs, hit_pt2 FROM alert_outcomes "
            "WHERE alert_id = ? AND horizon = ?",
            (alert_id, "1h")
        ).fetchall()
        assert rows == [(12.28, 35.0, 1)], f"idempotency broken: {rows}"
        conn.close()
    finally:
        _teardown_recorder_env(tmpdir)


if __name__ == "__main__":
    tests = [
        test_record_alert_writes_alerts_row,
        test_record_alert_writes_features,
        test_record_alert_round_trip_canonical_snapshot,
        test_record_alert_parent_alert_id_linkage,
        test_record_alert_master_gate_off_returns_none,
        test_record_alert_per_engine_gate_off_returns_none,
        test_record_alert_swallows_internal_exception,
        test_record_alert_id_is_uuid_v4,
        test_record_track_sample_writes_row,
        test_record_outcome_writes_row_and_is_idempotent,
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
