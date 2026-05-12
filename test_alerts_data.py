"""Tests for omega_dashboard.alerts_data.

# v11.7 (Patch H.6): hermetic — no network, no Schwab, no Telegram,
# never touches /var/backtest/desk.db. Each test owns its own temp DB
# created via tempfile.mkdtemp + apply_migrations.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────
# Hermetic test setup helpers
# ─────────────────────────────────────────────────────────────────────

def _setup_db():
    """Create temp dir + DB, apply schema, set RECORDER_DB_PATH."""
    tmpdir = tempfile.mkdtemp(prefix="alerts_h6_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown_db(tmpdir):
    for v in ("RECORDER_DB_PATH", "RECORDER_ENABLED",
              "RECORDER_LCB_ENABLED", "RECORDER_V25D_ENABLED",
              "RECORDER_CREDIT_ENABLED", "RECORDER_CONVICTION_ENABLED",
              "RECORDER_TRACKER_ENABLED", "RECORDER_OUTCOMES_ENABLED"):
        os.environ.pop(v, None)
    shutil.rmtree(tmpdir, ignore_errors=True)


def _utc_micros_for(year, month, day, hour=12, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
               .timestamp() * 1_000_000)


def _u(name):
    """Generate a deterministic UUID-v4-shaped alert_id from a label.
    Lets failures map back to the test that wrote the row."""
    h = hashlib.md5(name.encode()).hexdigest()
    # Force the version 4 nibble at position 12 + a valid variant nibble at 16.
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def _insert_alert(db, alert_id, fired_at, engine="long_call_burst",
                  ticker="SPY", classification="BURST_YES", direction="bull",
                  structure=None, dte=None, parent_alert_id=None):
    structure_json = json.dumps(structure or {})
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO alerts (alert_id, fired_at, engine, engine_version, "
        "ticker, classification, direction, suggested_structure, "
        "suggested_dte, spot_at_fire, canonical_snapshot, raw_engine_payload, "
        "parent_alert_id, posted_to_telegram, telegram_chat) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_id, fired_at, engine, f"{engine}@v8.4.2", ticker,
         classification, direction, structure_json, dte, 588.30,
         json.dumps({}), json.dumps({}), parent_alert_id, 1, "main")
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# State 1: missing DB
# ─────────────────────────────────────────────────────────────────────

def test_missing_db_returns_unavailable_with_friendly_error():
    tmpdir = tempfile.mkdtemp(prefix="alerts_h6_")
    bad_path = os.path.join(tmpdir, "does-not-exist.db")
    os.environ["RECORDER_DB_PATH"] = bad_path
    try:
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts()
        assert payload["available"] is False
        assert payload["error"] is not None
        assert "RECORDER_ENABLED" in payload["error"]
        assert payload["today"] == [] and payload["total_count"] == 0
    finally:
        os.environ.pop("RECORDER_DB_PATH", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────
# State 2: empty DB
# ─────────────────────────────────────────────────────────────────────

def test_empty_db_returns_available_with_zero_buckets():
    tmpdir, db = _setup_db()
    try:
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts()
        assert payload["available"] is True
        assert payload["error"] is None
        assert payload["total_count"] == 0
        for bucket in ("today", "yesterday", "this_week", "earlier"):
            assert payload[bucket] == []
        # Status strip is built even when DB is empty.
        assert "status" in payload
        assert payload["status"]["count_today"] == 0
        assert payload["status"]["last_fire_ct"] is None
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Bucketing (states 4 + 3)
# ─────────────────────────────────────────────────────────────────────

def test_bucketing_today_yesterday_this_week_earlier():
    tmpdir, db = _setup_db()
    try:
        # Pin "now" so bucket math is deterministic.
        now = _utc_micros_for(2026, 5, 9, hour=20)  # 2026-05-09 20:00 UTC
        ids = {b: _u(b) for b in
               ("today-1", "yesterday-1", "this-week-1", "earlier-1")}
        _insert_alert(db, ids["today-1"],     fired_at=_utc_micros_for(2026, 5, 9, 16))
        _insert_alert(db, ids["yesterday-1"], fired_at=_utc_micros_for(2026, 5, 8, 16))
        _insert_alert(db, ids["this-week-1"], fired_at=_utc_micros_for(2026, 5, 5, 16))
        _insert_alert(db, ids["earlier-1"],   fired_at=_utc_micros_for(2026, 4, 20, 16))
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        assert [c["alert_id"] for c in payload["today"]]     == [ids["today-1"]]
        assert [c["alert_id"] for c in payload["yesterday"]] == [ids["yesterday-1"]]
        assert [c["alert_id"] for c in payload["this_week"]] == [ids["this-week-1"]]
        assert [c["alert_id"] for c in payload["earlier"]]   == [ids["earlier-1"]]
        assert payload["total_count"] == 4
    finally:
        _teardown_db(tmpdir)


def test_only_old_alerts_state_3():
    """All rows >7 days old. Today/yesterday/this_week empty, earlier populated."""
    tmpdir, db = _setup_db()
    try:
        now = _utc_micros_for(2026, 5, 9, hour=20)
        old1, old2 = _u("old-1"), _u("old-2")
        _insert_alert(db, old1, fired_at=_utc_micros_for(2026, 4, 20, 16))
        _insert_alert(db, old2, fired_at=_utc_micros_for(2026, 3, 15, 16))
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        assert payload["today"] == [] and payload["yesterday"] == []
        assert payload["this_week"] == []
        assert {c["alert_id"] for c in payload["earlier"]} == {old1, old2}
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# format_structure_summary — per-engine + defensive
# ─────────────────────────────────────────────────────────────────────

def test_format_structure_summary_long_call():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("long_call_burst", {
        "type": "long_call", "strike": 215.50,
        "expiry": "2026-05-15", "entry_mark": 2.15,
    })
    assert s == "$215.50C 5/15 @ $2.15", s


def test_format_structure_summary_bull_put():
    # Fixture matches what v8.4 CREDIT actually writes to DB: keys are
    # "short"/"long", NOT "short_strike"/"long_strike" (Patch G.11).
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("credit_v84", {
        "type": "bull_put", "short": 580, "long": 575,
        "expiry": "2026-05-09", "credit": 0.85,
    })
    assert s == "580/575 BULL PUT 5/9 (credit $0.85)", s


def test_format_structure_summary_bear_call():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("credit_v84", {
        "type": "bear_call", "short": 600, "long": 605,
        "expiry": "2026-05-15", "credit": 1.10,
    })
    assert s == "600/605 BEAR CALL 5/15 (credit $1.10)", s


def test_format_structure_summary_bull_put_real_msft_shape():
    """Regression: explicit fixture matching the exact shape v8.4 CREDIT
    writes to alerts.suggested_structure_json — verified against real
    MSFT bull_put rows present in production DB on 2026-05-11. If this
    test fails, the credit-spread feed has regressed."""
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("credit_v84", {
        "type": "bull_put", "short": 405.0, "long": 400.0,
        "width": 5.0, "credit": 1.20, "expiry": "2026-05-15",
    })
    assert s == "405/400 BULL PUT 5/15 (credit $1.20)", s


def test_format_structure_summary_v2_5d_uses_classification_and_direction():
    """v2_5d structure JSON is just {"type":"evaluation"} — grade and
    bias come from the alert row's classification and direction columns."""
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary(
        "v2_5d", {"type": "evaluation"},
        classification="GRADE_A", direction="bull",
    )
    assert s == "Grade A bull", s
    # Falls back gracefully when classification or direction are missing.
    s2 = format_structure_summary("v2_5d", {"type": "evaluation"})
    assert "v2_5d" in s2 and "evaluation" in s2, s2
    s3 = format_structure_summary("v2_5d", {"type": "evaluation"},
                                  classification="GRADE_B", direction=None)
    assert s3 == "Grade B", s3


def test_format_structure_summary_conviction():
    from omega_dashboard.alerts_data import format_structure_summary
    s = format_structure_summary("oi_flow_conviction", {
        "strike": 190, "right": "C", "expiry": "2026-05-15",
    })
    assert s == "$190.00C 5/15 (flow conviction)", s


def test_format_structure_summary_malformed_does_not_raise():
    """Defensive: missing keys, unknown engine, non-dict structure all
    return a fallback string instead of raising."""
    from omega_dashboard.alerts_data import format_structure_summary
    assert "[no structure]" in format_structure_summary("long_call_burst", None)
    assert "[no structure]" in format_structure_summary("long_call_burst", "garbage")
    assert "[partial data]" in format_structure_summary("long_call_burst",
                                                        {"type": "long_call"})
    assert "[unknown type]" in format_structure_summary("future_engine",
                                                        {"type": "weird"})
    assert "[partial data]" in format_structure_summary("oi_flow_conviction",
                                                        {"right": "C"})


# ─────────────────────────────────────────────────────────────────────
# _compute_status_badge — Patch H.8 row-1 enriched badge logic
# ─────────────────────────────────────────────────────────────────────

def test_compute_status_badge_v2_5d_returns_eval():
    """v2_5d alerts always render EVAL — even when fresh, even if a
    pnl somehow exists. Engine identity wins."""
    from omega_dashboard.alerts_data import _compute_status_badge
    assert _compute_status_badge("v2_5d", elapsed_seconds=30,
                                 latest_pnl=None) == ("EVAL", "eval")
    assert _compute_status_badge("v2_5d", elapsed_seconds=30,
                                 latest_pnl=12.0) == ("EVAL", "eval")


def test_compute_status_badge_expired_when_past_horizon():
    """elapsed > 3 days flips to EXPIRED regardless of pnl."""
    from omega_dashboard.alerts_data import (_compute_status_badge,
                                             TRACKING_HORIZON_SECONDS)
    over = TRACKING_HORIZON_SECONDS + 1
    assert _compute_status_badge("long_call_burst", elapsed_seconds=over,
                                 latest_pnl=None) == ("EXPIRED", "expired")
    # latest_pnl present but past horizon → still EXPIRED
    assert _compute_status_badge("credit_v84", elapsed_seconds=over,
                                 latest_pnl=8.5) == ("EXPIRED", "expired")


def test_compute_status_badge_positive_pnl():
    """Latest pnl > 0 → '+N%' / 'positive' style."""
    from omega_dashboard.alerts_data import _compute_status_badge
    text, cls = _compute_status_badge("long_call_burst",
                                      elapsed_seconds=600,
                                      latest_pnl=12.4)
    assert text == "+12%", text
    assert cls == "positive"
    # Zero pnl is positive (>=0).
    text2, cls2 = _compute_status_badge("long_call_burst",
                                        elapsed_seconds=600,
                                        latest_pnl=0.0)
    assert text2 == "+0%" and cls2 == "positive"


def test_compute_status_badge_active_when_no_track():
    """Fresh alert with no track samples yet → ACTIVE / 'active'."""
    from omega_dashboard.alerts_data import _compute_status_badge
    assert _compute_status_badge("long_call_burst", elapsed_seconds=30,
                                 latest_pnl=None) == ("ACTIVE", "active")
    assert _compute_status_badge("oi_flow_conviction",
                                 elapsed_seconds=120,
                                 latest_pnl=None) == ("ACTIVE", "active")


def test_compute_status_badge_negative_pnl():
    """Latest pnl < 0 → '-N%' / 'negative' style. Sign formatting check."""
    from omega_dashboard.alerts_data import _compute_status_badge
    text, cls = _compute_status_badge("credit_v84", elapsed_seconds=600,
                                      latest_pnl=-3.2)
    assert text == "-3%", text
    assert cls == "negative"
    # Larger negative — no thousands sep, just integer rounding.
    text2, cls2 = _compute_status_badge("long_call_burst",
                                        elapsed_seconds=600,
                                        latest_pnl=-47.6)
    assert text2 == "-48%" and cls2 == "negative"


# ─────────────────────────────────────────────────────────────────────
# get_alert_detail — assembly + parent linkage + UUID enforcement
# ─────────────────────────────────────────────────────────────────────

def test_get_alert_detail_returns_full_payload():
    tmpdir, db = _setup_db()
    try:
        fired = _utc_micros_for(2026, 5, 9, 16)
        aid = _u("detail-full")
        _insert_alert(db, aid, fired_at=fired,
                      engine="long_call_burst",
                      structure={"type": "long_call", "strike": 215.5,
                                 "expiry": "2026-05-15", "entry_mark": 2.15},
                      dte=4)
        # Insert features + a track sample + an outcome via raw SQL.
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO alert_features (alert_id, feature_name, "
                     "feature_value) VALUES (?, ?, ?)", (aid, "rsi", 62.5))
        conn.execute("INSERT INTO alert_features (alert_id, feature_name, "
                     "feature_text) VALUES (?, ?, ?)", (aid, "regime", "BULL_BASE"))
        conn.execute("INSERT INTO alert_price_track (alert_id, elapsed_seconds, "
                     "sampled_at, underlying_price, structure_mark, "
                     "structure_pnl_pct, structure_pnl_abs, market_state) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (aid, 60, fired + 60_000_000, 588.5, 2.30,
                      6.98, 15.0, "rth"))
        conn.execute("INSERT INTO alert_outcomes (alert_id, horizon, outcome_at, "
                     "underlying_price, structure_mark, pnl_pct, pnl_abs, "
                     "hit_pt1, hit_pt2, hit_pt3, max_favorable_pct, "
                     "max_adverse_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (aid, "1h", fired + 3_600_000_000, 591.0, 3.10,
                      44.19, 0.95, 1, 0, 0, 50.0, -5.0))
        conn.commit()
        conn.close()

        from omega_dashboard import alerts_data
        d = alerts_data.get_alert_detail(aid)
        assert d is not None
        assert d["alert_id"] == aid
        assert d["engine"] == "long_call_burst"
        assert d["structure_summary"] == "$215.50C 5/15 @ $2.15"
        # LCB is trading-DTE convention.
        assert d["dte_label"] == "4 trading days", d["dte_label"]
        assert len(d["features"]) == 2
        # Alphabetical order: regime before rsi.
        assert d["features"][0]["name"] == "regime"
        assert d["features"][1]["name"] == "rsi"
        assert len(d["track"]) == 1 and d["track"][0]["elapsed_seconds"] == 60
        assert len(d["outcomes"]) == 1 and d["outcomes"][0]["hit_pt1"] is True
        assert "<svg" in d["pnl_svg"]
        assert d["chart_stats"]["samples"] == 1
    finally:
        _teardown_db(tmpdir)


def test_get_alert_detail_missing_returns_none_and_enforces_uuid():
    """alert_id MUST match UUID v4 format. Anything else → None,
    including empty string, non-UUID-looking strings, and obvious
    path-traversal attempts."""
    tmpdir, db = _setup_db()
    try:
        from omega_dashboard import alerts_data
        # Non-UUID inputs all rejected — never even hit the DB.
        assert alerts_data.get_alert_detail("nope") is None
        assert alerts_data.get_alert_detail("") is None
        assert alerts_data.get_alert_detail("../etc/passwd") is None
        assert alerts_data.get_alert_detail("123") is None
        assert alerts_data.get_alert_detail("not-a-uuid-at-all") is None
        # Wrong shape: missing one segment.
        assert alerts_data.get_alert_detail(
            "7f3a9e21-4b8f-4d2c-9a1e") is None
        # Wrong shape: non-hex characters.
        assert alerts_data.get_alert_detail(
            "ZZZZZZZZ-4b8f-4d2c-9a1e-3f7c5d8b9012") is None
        # SQL injection attempt — UUID gate catches it.
        assert alerts_data.get_alert_detail("' OR 1=1 --") is None
        # Valid UUID v4 shape but no row → still None.
        assert alerts_data.get_alert_detail(
            "7f3a9e21-4b8f-4d2c-9a1e-3f7c5d8b9012") is None
    finally:
        _teardown_db(tmpdir)


def test_get_alert_detail_parent_linkage():
    tmpdir, db = _setup_db()
    try:
        fired = _utc_micros_for(2026, 5, 9, 14)
        parent_id = _u("v25d-parent")
        child_id = _u("lcb-child")
        # V2 5D parent: real shape is {"type":"evaluation"}; grade/bias
        # come from columns.
        _insert_alert(db, parent_id, fired_at=fired,
                      engine="v2_5d", classification="GRADE_A",
                      direction="bull",
                      structure={"type": "evaluation"})
        _insert_alert(db, child_id, fired_at=fired + 60_000_000,
                      engine="long_call_burst",
                      structure={"type": "long_call", "strike": 215.0,
                                 "expiry": "2026-05-15", "entry_mark": 2.0},
                      parent_alert_id=parent_id, dte=4)
        from omega_dashboard import alerts_data
        child = alerts_data.get_alert_detail(child_id)
        assert child["parent"]["alert_id"] == parent_id
        assert child["parent"]["engine_label"] == "V2 5D EDGE"
        assert child["children"] == []
        parent = alerts_data.get_alert_detail(parent_id)
        assert parent["parent"] is None
        # Built from columns, not structure JSON.
        assert parent["structure_summary"] == "Grade A bull", parent["structure_summary"]
        assert len(parent["children"]) == 1
        assert parent["children"][0]["alert_id"] == child_id
        assert parent["children"][0]["engine_label"] == "LONG CALL BURST"
    finally:
        _teardown_db(tmpdir)


def test_get_alert_detail_empty_subsections_render():
    """Alert with no features, no track, no outcomes — all sub-sections
    still build without crashing."""
    tmpdir, db = _setup_db()
    try:
        fired = _utc_micros_for(2026, 5, 9, 14)
        bare = _u("bare")
        _insert_alert(db, bare, fired_at=fired)
        from omega_dashboard import alerts_data
        d = alerts_data.get_alert_detail(bare)
        assert d is not None
        assert d["features"] == []
        assert d["track"] == []
        assert d["outcomes"] == []
        assert d["pnl_svg"] == ""
        assert d["chart_stats"] is None
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Patch H.8 aggregate queries — track + outcomes batched IN-clause
# ─────────────────────────────────────────────────────────────────────

def _insert_track(db, alert_id, samples):
    """samples: list of (elapsed_seconds, structure_pnl_pct) tuples."""
    conn = sqlite3.connect(db)
    base_sampled = 1_700_000_000_000_000
    for elapsed, pnl in samples:
        conn.execute(
            "INSERT INTO alert_price_track (alert_id, elapsed_seconds, "
            "sampled_at, underlying_price, structure_mark, "
            "structure_pnl_pct, structure_pnl_abs, market_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (alert_id, elapsed, base_sampled + elapsed * 1_000_000,
             100.0, 1.0, pnl, 0.0, "rth")
        )
    conn.commit()
    conn.close()


def _insert_outcome(db, alert_id, horizon, hit_pt1=0, hit_pt2=0, hit_pt3=0,
                   pnl_pct=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO alert_outcomes (alert_id, horizon, outcome_at, "
        "underlying_price, structure_mark, pnl_pct, pnl_abs, "
        "hit_pt1, hit_pt2, hit_pt3, max_favorable_pct, max_adverse_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_id, horizon, 1_700_000_001_000_000, 100.0, 1.0,
         pnl_pct, 0.0, hit_pt1, hit_pt2, hit_pt3, None, None)
    )
    conn.commit()
    conn.close()


def test_list_alerts_attaches_track_aggregates():
    """Three track samples with pnl_pct values -2, +5, +12. The
    aggregate query should produce mfe=12, mae=-2, latest=12."""
    tmpdir, db = _setup_db()
    try:
        now = _utc_micros_for(2026, 5, 9, hour=20)
        aid = _u("agg-track")
        _insert_alert(db, aid, fired_at=_utc_micros_for(2026, 5, 9, hour=18),
                      engine="long_call_burst")
        _insert_track(db, aid, [(60, -2.0), (120, 5.0), (180, 12.0)])
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        cards = payload["today"]
        assert len(cards) == 1
        card = cards[0]
        assert card["row5"]["mode"] == "active", card["row5"]
        assert card["row5"]["mfe_pct"] == 12.0, card["row5"]
        assert card["row5"]["current_pct"] == 12.0, card["row5"]
        # Badge derives from latest pnl, not MFE.
        assert card["badge_text"] == "+12%", card["badge_text"]
        assert card["badge_class"] == "positive"
    finally:
        _teardown_db(tmpdir)


def test_list_alerts_pt_hit_from_outcomes_aggregate():
    """PT-hit label uses HIGHEST tier touched (PT3 > PT2 > PT1).
    Verifies the outcomes any-PT GROUP BY query produces correct flags."""
    tmpdir, db = _setup_db()
    try:
        now = _utc_micros_for(2026, 5, 9, hour=20)
        # First alert: hit_pt1=1 only
        a1 = _u("pt1-only")
        _insert_alert(db, a1, fired_at=_utc_micros_for(2026, 5, 9, hour=18),
                      engine="long_call_burst")
        _insert_track(db, a1, [(60, 8.0)])
        _insert_outcome(db, a1, horizon="5min", hit_pt1=1)
        # Second alert: hit_pt1=1 AND hit_pt3=1 across different horizons
        # — highest tier (PT3) wins regardless of which horizon row holds it
        a2 = _u("pt-stack")
        _insert_alert(db, a2, fired_at=_utc_micros_for(2026, 5, 9, hour=17),
                      engine="long_call_burst")
        _insert_track(db, a2, [(60, 25.0)])
        _insert_outcome(db, a2, horizon="5min", hit_pt1=1)
        _insert_outcome(db, a2, horizon="15min", hit_pt1=1, hit_pt3=1)
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        cards = {c["alert_id"]: c for c in payload["today"]}
        assert cards[a1]["row5"]["pt_hit_label"] == "★ PT1", \
            cards[a1]["row5"]
        assert cards[a2]["row5"]["pt_hit_label"] == "★ PT3", \
            cards[a2]["row5"]
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Patch H.10 — row 5 stalled mode
# ─────────────────────────────────────────────────────────────────────
#
# _build_row5 is a pure function — no DB needed. Tests cover the
# warming → stalled split: under-90s with no samples still warms; at-90s
# or with bad-pnl samples flips to stalled.

def test_h10_row5_stalled_when_samples_exist_but_pnl_null():
    """Track samples exist (latest_elapsed is set) but latest_pnl_pct is
    None — the pre-G.13 silent-NULL-mark case. Must report
    mode='stalled' with reason='no pnl data', NOT warming.
    Pre-H.10 this rendered as 'tracking starts in 0s' forever."""
    from omega_dashboard.alerts_data import _build_row5
    aggregates = {
        "latest_elapsed": 600,
        "latest_pnl_pct": None,
        "mfe_pct": None, "mae_pct": None,
        "any_pt1": False, "any_pt2": False, "any_pt3": False,
    }
    out = _build_row5("credit_v84", elapsed_seconds=6 * 60 * 60, aggregates=aggregates)
    assert out["mode"] == "stalled", out
    assert out["reason"] == "no pnl data", out
    assert isinstance(out["bar_pct"], int)
    assert 0 <= out["bar_pct"] <= 100


def test_h10_row5_stalled_when_no_samples_after_90s():
    """No track samples at all and elapsed > 90s → stalled with
    reason='no samples yet'. (Tracker may be off, alert legs
    unsubscribed pre-G.13, etc.)"""
    from omega_dashboard.alerts_data import _build_row5
    out = _build_row5("long_call_burst", elapsed_seconds=120, aggregates=None)
    assert out["mode"] == "stalled", out
    assert out["reason"] == "no samples yet", out


def test_h10_row5_warming_still_works_under_60s():
    """Under 60s elapsed with no samples → warming, with countdown.
    Pre-H.10 behavior preserved inside the grace window."""
    from omega_dashboard.alerts_data import _build_row5
    out = _build_row5("long_call_burst", elapsed_seconds=30, aggregates=None)
    assert out["mode"] == "warming", out
    assert out["warming_seconds_left"] == 30, out


def test_h10_row5_warming_to_stalled_boundary_at_90s():
    """The 90s grace window IS the cutoff. elapsed=89 → warming;
    elapsed=90 → stalled. Pin the exact threshold so a future tweak
    doesn't silently widen or narrow the grace."""
    from omega_dashboard.alerts_data import _build_row5
    just_warming = _build_row5("long_call_burst", elapsed_seconds=89,
                               aggregates=None)
    assert just_warming["mode"] == "warming", just_warming
    just_stalled = _build_row5("long_call_burst", elapsed_seconds=90,
                               aggregates=None)
    assert just_stalled["mode"] == "stalled", just_stalled
    assert just_stalled["reason"] == "no samples yet"


def test_h10_row5_active_unchanged_when_pnl_present():
    """Sanity: existing active-mode behavior is not regressed by H.10.
    Aggregates with valid latest_pnl_pct → mode='active' as before."""
    from omega_dashboard.alerts_data import _build_row5
    aggregates = {
        "latest_elapsed": 600,
        "latest_pnl_pct": 3.5,
        "mfe_pct": 5.0, "mae_pct": -1.0,
        "any_pt1": True, "any_pt2": False, "any_pt3": False,
    }
    out = _build_row5("long_call_burst", elapsed_seconds=900,
                     aggregates=aggregates)
    assert out["mode"] == "active", out
    assert out["current_pct"] == 3.5
    assert out["pt_hit_label"] == "★ PT1"


# ─────────────────────────────────────────────────────────────────────
# Patch H.10 — chart hover targets in _build_pnl_svg
# ─────────────────────────────────────────────────────────────────────

def test_h10_format_chart_elapsed_compact_format():
    """Tooltip label format — no 'ago' suffix. Boundary cases (60s, 1h)
    and multi-unit (h+m, d+h)."""
    from omega_dashboard.alerts_data import _format_chart_elapsed
    assert _format_chart_elapsed(0) == "0s"
    assert _format_chart_elapsed(45) == "45s"
    assert _format_chart_elapsed(60) == "1m"
    assert _format_chart_elapsed(17 * 60) == "17m"
    assert _format_chart_elapsed(3 * 3600) == "3h"
    assert _format_chart_elapsed(3 * 3600 + 12 * 60) == "3h 12m"
    assert _format_chart_elapsed(2 * 86400) == "2d"
    assert _format_chart_elapsed(2 * 86400 + 4 * 3600) == "2d 4h"


def test_h10_pnl_svg_emits_hit_targets_per_data_point():
    """Every track row with non-None pnl gets a chart-data-point-target
    circle with data-elapsed and data-pnl attributes. Rendered AFTER
    polyline + accent markers so they win the hover hit-test."""
    from omega_dashboard.alerts_data import _build_pnl_svg
    track = [
        {"elapsed_seconds": 60,  "structure_pnl_pct": 2.5},
        {"elapsed_seconds": 300, "structure_pnl_pct": -1.0},
        {"elapsed_seconds": 600, "structure_pnl_pct": 4.7},
    ]
    svg = _build_pnl_svg(track)
    assert svg.count('class="chart-data-point-target"') == 3
    assert 'data-elapsed="1m"' in svg
    assert 'data-elapsed="5m"' in svg
    assert 'data-elapsed="10m"' in svg
    assert 'data-pnl="+2.5%"' in svg
    assert 'data-pnl="-1.0%"' in svg
    assert 'data-pnl="+4.7%"' in svg
    # Targets emitted last (after polyline + accent markers) so the
    # hit-test layering is correct. Check that the last chart-data-point-target
    # appears after the last existing accent circle (the green "current"
    # marker, fill="#73B27B").
    last_target = svg.rfind('chart-data-point-target')
    last_accent = svg.rfind('fill="#73B27B"')
    assert last_target > last_accent > -1, (
        "hit targets must render after accent markers for hover-hit "
        "ordering — pre-H.10 SVG paint order would have invisible "
        "targets blocked by visible markers"
    )


def test_h10_pnl_svg_skips_rows_with_null_pnl():
    """A track row with None pnl is omitted from the chart entirely —
    no hit target, no polyline point. Important for credit spreads
    pre-G.13 where many samples are NULL."""
    from omega_dashboard.alerts_data import _build_pnl_svg
    track = [
        {"elapsed_seconds": 60,  "structure_pnl_pct": 2.5},
        {"elapsed_seconds": 120, "structure_pnl_pct": None},
        {"elapsed_seconds": 300, "structure_pnl_pct": -1.0},
    ]
    svg = _build_pnl_svg(track)
    assert svg.count('class="chart-data-point-target"') == 2
    assert 'data-elapsed="2m"' not in svg  # the None-pnl row is skipped


# ─────────────────────────────────────────────────────────────────────
# Patch H.10 — OUTCOMES table pnl cell distinguishes pending / n/a / value
# ─────────────────────────────────────────────────────────────────────

def test_h10_outcome_pnl_pending_when_outcome_at_null():
    """outcome_at IS NULL → horizon not yet reached. Render 'pending'."""
    from omega_dashboard.alerts_data import _format_outcome_pnl_cell
    cell = _format_outcome_pnl_cell(outcome_at=None, pnl_pct=None)
    assert cell == {"label": "pending", "css_class": "pnl-pending"}


def test_h10_outcome_pnl_na_when_outcome_at_set_but_pnl_null():
    """outcome_at IS NOT NULL AND pnl_pct IS NULL → horizon crossed but
    mark unavailable. Render 'n/a' (NOT 'pending' — the data is never
    coming). Distinct CSS class so it can be styled differently from
    pending later if Brad wants."""
    from omega_dashboard.alerts_data import _format_outcome_pnl_cell
    cell = _format_outcome_pnl_cell(
        outcome_at=1_700_000_000_000_000, pnl_pct=None
    )
    assert cell == {"label": "n/a", "css_class": "pnl-na"}, cell


def test_h10_outcome_pnl_value_when_pnl_present():
    """Positive, negative, and zero pnl all render with sign-formatted
    value and a class reflecting the sign."""
    from omega_dashboard.alerts_data import _format_outcome_pnl_cell
    pos = _format_outcome_pnl_cell(outcome_at=1, pnl_pct=12.345)
    assert pos["label"] == "+12.3%"
    assert pos["css_class"] == "pnl-pos"
    neg = _format_outcome_pnl_cell(outcome_at=1, pnl_pct=-3.7)
    assert neg["label"] == "-3.7%"
    assert neg["css_class"] == "pnl-neg"
    zero = _format_outcome_pnl_cell(outcome_at=1, pnl_pct=0.0)
    assert zero["label"] == "+0.0%"
    assert zero["css_class"] == "pnl-zero"


def test_list_alerts_warming_state_when_no_track_samples():
    """Alert fired 30s ago with no track samples → mode='warming',
    warming_seconds_left counts down from 60. Badge falls back to ACTIVE."""
    tmpdir, db = _setup_db()
    try:
        now = _utc_micros_for(2026, 5, 9, hour=20, minute=0)
        # Fired 30s before "now"
        fired = now - 30 * 1_000_000
        aid = _u("warming")
        _insert_alert(db, aid, fired_at=fired, engine="long_call_burst")
        # No track samples inserted.
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts(now_micros=now)
        card = payload["today"][0]
        assert card["row5"]["mode"] == "warming", card["row5"]
        # 60 - 30 = 30 seconds left
        assert card["row5"]["warming_seconds_left"] == 30, card["row5"]
        # Badge: no pnl available yet → ACTIVE
        assert card["badge_text"] == "ACTIVE"
        assert card["badge_class"] == "active"
    finally:
        _teardown_db(tmpdir)


def test_list_alerts_does_not_explode_with_zero_alerts():
    """Empty alerts table → _fetch_aggregates must early-return {} so
    we never construct an 'IN ()' clause (which SQLite rejects)."""
    tmpdir, db = _setup_db()
    try:
        from omega_dashboard import alerts_data
        # Direct unit test of the helper.
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        try:
            assert alerts_data._fetch_aggregates(conn, []) == {}
        finally:
            conn.close()
        # End-to-end: list_alerts with empty DB returns the friendly
        # empty payload (state 2) without raising.
        payload = alerts_data.list_alerts()
        assert payload["available"] is True
        assert payload["total_count"] == 0
        # No exception raised.
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# LIST_LIMIT cap + no N+1 on the list view
# ─────────────────────────────────────────────────────────────────────

def test_list_alerts_respects_limit_and_does_no_per_row_lookups():
    """Insert 250 alerts; list_alerts should fetch only 200 and never
    look at alert_features / alert_price_track / alert_outcomes."""
    tmpdir, db = _setup_db()
    try:
        base = _utc_micros_for(2026, 5, 9, 14)
        ids = [_u(f"cap-{i:03d}") for i in range(250)]
        for i, aid in enumerate(ids):
            _insert_alert(db, aid, fired_at=base + i * 1_000_000)
        # Add a feature row — list_alerts must NOT touch it
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO alert_features VALUES (?, ?, ?, ?)",
                     (ids[1], "rsi", 60.0, None))
        conn.commit()
        conn.close()
        from omega_dashboard import alerts_data
        payload = alerts_data.list_alerts()
        assert payload["total_count"] == 200, payload["total_count"]
        # Cards have no 'features' / 'track' / 'outcomes' keys — proves
        # list view doesn't enrich.
        for card in payload["today"][:5]:
            assert "features" not in card
            assert "track" not in card
            assert "outcomes" not in card
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Test runner footer (matches test_alert_recorder.py convention)
# ─────────────────────────────────────────────────────────────────────

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
