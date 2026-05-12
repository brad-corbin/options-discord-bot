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


# ─────────────────────────────────────────────────────────────────────
# Patch G.13 — option subscription for alert structure legs
# ─────────────────────────────────────────────────────────────────────
#
# The helper imports schwab_stream lazily, so we patch the symbols on
# the SOURCE module (schwab_stream.get_option_symbol_manager + the
# build_occ_symbol callable). That's what `from schwab_stream import X`
# resolves to at call time.

def _g13_fake_manager(captured: list):
    """Minimal stand-in for OptionSymbolManager with the two attributes
    the helper touches: subscribe_specific(list) and status dict."""
    class _Fake:
        status = {"subscribed": 600}
        def subscribe_specific(self, occ_symbols):
            captured.extend(occ_symbols)
    return _Fake()


def _g13_patch_stream(captured_subs, captured_occ_args, manager=None):
    """Returns a context-manager-style helper that patches
    schwab_stream.{get_option_symbol_manager, build_occ_symbol}.

    Caller threads captured_subs / captured_occ_args lists in to inspect
    what the helper did.
    """
    from unittest import mock
    if manager is None:
        manager = _g13_fake_manager(captured_subs)

    def _fake_build_occ(ticker, expiry, side, strike):
        captured_occ_args.append((ticker, expiry, side, strike))
        return f"{ticker}:{expiry}:{side}:{strike}"   # synthetic shape

    return mock.patch.multiple(
        "schwab_stream",
        get_option_symbol_manager=lambda: manager,
        build_occ_symbol=_fake_build_occ,
    )


def test_g13_subscribe_alert_legs_long_call_builds_one_occ():
    from alert_recorder import _subscribe_alert_legs
    subs, occ_args = [], []
    with _g13_patch_stream(subs, occ_args):
        _subscribe_alert_legs(
            {"type": "long_call", "strike": 590.0, "expiry": "2026-05-15"},
            "SPY",
        )
    assert len(occ_args) == 1, f"expected 1 build_occ call, got {occ_args}"
    assert occ_args[0] == ("SPY", "2026-05-15", "call", 590.0)
    assert len(subs) == 1, f"expected 1 subscribed OCC, got {subs}"


def test_g13_subscribe_alert_legs_bull_put_builds_two_occs():
    """Real-shape MSFT bull_put fixture from Patch G.11 — proves the
    helper reads `short` / `long` (NOT `short_strike` / `long_strike`)."""
    from alert_recorder import _subscribe_alert_legs
    subs, occ_args = [], []
    with _g13_patch_stream(subs, occ_args):
        _subscribe_alert_legs(
            {"type": "bull_put", "short": 405.0, "long": 400.0,
             "width": 5.0, "credit": 1.20, "expiry": "2026-05-15"},
            "MSFT",
        )
    assert len(occ_args) == 2, f"expected 2 build_occ calls, got {occ_args}"
    # bull_put → put side, both legs
    sides = {a[2] for a in occ_args}
    strikes = {a[3] for a in occ_args}
    assert sides == {"put"}, f"expected put-side legs, got {sides}"
    assert strikes == {405.0, 400.0}, f"expected 405/400, got {strikes}"
    assert len(subs) == 2


def test_g13_subscribe_alert_legs_no_op_when_manager_none():
    """If option streaming hasn't started, helper must silently no-op.
    Important: this is the path most CI / hermetic tests hit (no live
    streaming in the test process)."""
    from alert_recorder import _subscribe_alert_legs
    subs, occ_args = [], []
    # manager=lambda: None → get_option_symbol_manager() returns None.
    # build_occ_symbol patched but should never be called because the
    # helper short-circuits on the manager check.
    from unittest import mock
    with mock.patch.multiple(
        "schwab_stream",
        get_option_symbol_manager=lambda: None,
        build_occ_symbol=lambda *a, **kw: occ_args.append(a) or "NEVER",
    ):
        # Must NOT raise
        _subscribe_alert_legs(
            {"type": "long_call", "strike": 590.0, "expiry": "2026-05-15"},
            "SPY",
        )
    assert subs == [], "no manager → no subscribe call"
    assert occ_args == [], "no manager → never reach OCC builder"


def test_g13_subscribe_alert_legs_unknown_type_silent():
    """Unknown structure type must not raise and must not call subscribe.
    Structure shape from a hypothetical future engine that records
    structures the recorder doesn't yet know how to subscribe."""
    from alert_recorder import _subscribe_alert_legs
    subs, occ_args = [], []
    with _g13_patch_stream(subs, occ_args):
        _subscribe_alert_legs(
            {"type": "iron_condor", "expiry": "2026-05-15",
             "short_put": 400, "long_put": 395,
             "short_call": 410, "long_call": 415},
            "SPY",
        )
    assert occ_args == []
    assert subs == []


def test_g13_record_alert_calls_subscribe_when_gate_on():
    """End-to-end: record_alert with the gate ON (default) invokes
    _subscribe_alert_legs exactly once with the structure + ticker
    arguments. Asserts the wire-in point fires AND that an unhandled
    exception in the helper does NOT clobber the returned alert_id
    (the row is already committed)."""
    from unittest import mock
    tmpdir, db = _setup_recorder_env()
    try:
        with mock.patch("alert_recorder._subscribe_alert_legs") as fake_sub:
            from alert_recorder import record_alert
            structure = {"type": "long_call", "strike": 590.0,
                         "expiry": "2026-05-15", "entry_mark": 2.85}
            alert_id = record_alert(
                engine="long_call_burst",
                engine_version="long_call_burst@v8.4.2",
                ticker="SPY",
                classification="BURST_YES", direction="bull",
                suggested_structure=structure,
                suggested_dte=6, spot_at_fire=588.30,
                canonical_snapshot={}, raw_engine_payload={},
                features={}, telegram_chat="main",
            )
            assert alert_id is not None
            fake_sub.assert_called_once_with(structure, "SPY")

        # Now flip the gate OFF and prove the helper is NOT called.
        os.environ["RECORDER_AUTO_SUBSCRIBE_ENABLED"] = "false"
        try:
            with mock.patch("alert_recorder._subscribe_alert_legs") as fake_sub_off:
                from alert_recorder import record_alert
                alert_id2 = record_alert(
                    engine="long_call_burst",
                    engine_version="long_call_burst@v8.4.2",
                    ticker="QQQ",
                    classification="BURST_YES", direction="bull",
                    suggested_structure=structure,
                    suggested_dte=6, spot_at_fire=480.30,
                    canonical_snapshot={}, raw_engine_payload={},
                    features={}, telegram_chat="main",
                )
                assert alert_id2 is not None
                fake_sub_off.assert_not_called()
        finally:
            os.environ.pop("RECORDER_AUTO_SUBSCRIBE_ENABLED", None)

        # And prove the wrapper try/except: helper raising does NOT
        # clobber the returned alert_id (row was already committed).
        with mock.patch(
            "alert_recorder._subscribe_alert_legs",
            side_effect=RuntimeError("simulated streaming hiccup"),
        ):
            from alert_recorder import record_alert
            alert_id3 = record_alert(
                engine="long_call_burst",
                engine_version="long_call_burst@v8.4.2",
                ticker="IWM",
                classification="BURST_YES", direction="bull",
                suggested_structure=structure,
                suggested_dte=6, spot_at_fire=220.30,
                canonical_snapshot={}, raw_engine_payload={},
                features={}, telegram_chat="main",
            )
            assert alert_id3 is not None, (
                "subscribe helper exception must NOT clobber alert_id — "
                "row was already committed"
            )
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
        test_g13_subscribe_alert_legs_long_call_builds_one_occ,
        test_g13_subscribe_alert_legs_bull_put_builds_two_occs,
        test_g13_subscribe_alert_legs_no_op_when_manager_none,
        test_g13_subscribe_alert_legs_unknown_type_silent,
        test_g13_record_alert_calls_subscribe_when_gate_on,
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
