"""Tests for omega_dashboard.barometer_data. Patch I V0.

Hermetic — no network, no Schwab, no Telegram, never touches
/var/backtest/desk.db. Each test owns its own temp DB created via
tempfile.mkdtemp + apply_migrations + direct-SQL inserts.

Covers per-engine summary, per-engine x direction cut, per-ticker
leaderboard, small-sample suppression, cache hit short-circuit, and
the missing-DB graceful fallback.
"""
import json
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock


# ─────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────

def _setup_db():
    tmpdir = tempfile.mkdtemp(prefix="barometer_v0_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    # Pin the cutoff far in the past so seeded data is always included.
    os.environ["BAROMETER_SINCE_DATE"] = "2020-01-01"
    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown_db(tmpdir):
    for v in ("RECORDER_DB_PATH", "BAROMETER_SINCE_DATE"):
        os.environ.pop(v, None)
    # Force module reload so the next test gets a fresh module state.
    sys.modules.pop("omega_dashboard.barometer_data", None)
    shutil.rmtree(tmpdir, ignore_errors=True)


def _u(name):
    """UUID-v4-shaped id derived from a label for deterministic test data."""
    h = hashlib.md5(name.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def _utc_micros(year, month, day, hour=12, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
               .timestamp() * 1_000_000)


def _insert_alert(db, alert_id, fired_at, engine="long_call_burst",
                  ticker="SPY", direction="bull", structure=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO alerts (alert_id, fired_at, engine, engine_version, "
        "ticker, classification, direction, suggested_structure, "
        "suggested_dte, spot_at_fire, canonical_snapshot, raw_engine_payload, "
        "parent_alert_id, posted_to_telegram, telegram_chat) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_id, fired_at, engine, f"{engine}@v1", ticker,
         "TEST", direction, json.dumps(structure or {}), None, 100.0,
         "{}", "{}", None, 1, "main")
    )
    conn.commit()
    conn.close()


def _insert_outcome(db, alert_id, horizon, pnl_pct,
                    hit_pt1=0, hit_pt2=0, hit_pt3=0,
                    mfe=None, mae=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO alert_outcomes (alert_id, horizon, outcome_at, "
        "underlying_price, structure_mark, pnl_pct, pnl_abs, "
        "hit_pt1, hit_pt2, hit_pt3, max_favorable_pct, max_adverse_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert_id, horizon, None, None, None, pnl_pct, None,
         hit_pt1, hit_pt2, hit_pt3, mfe, mae)
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# Empty-DB / unavailable-DB
# ─────────────────────────────────────────────────────────────────────

def test_empty_db_returns_empty_sections():
    tmpdir, db = _setup_db()
    try:
        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        assert data["error"] is None
        assert data["per_engine"] == []
        assert data["per_engine_direction"] == []
        assert data["per_ticker"] == []
        assert data["min_sample_for_pct"] >= 1
    finally:
        _teardown_db(tmpdir)


def test_missing_db_returns_error_state():
    """If the DB file doesn't exist, get_barometer_data returns a payload
    with `error` set and empty sections — never raises."""
    tmpdir = tempfile.mkdtemp(prefix="barometer_v0_missing_")
    bad_path = os.path.join(tmpdir, "does-not-exist.db")
    os.environ["RECORDER_DB_PATH"] = bad_path
    os.environ["BAROMETER_SINCE_DATE"] = "2020-01-01"
    try:
        sys.modules.pop("omega_dashboard.barometer_data", None)
        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        assert data["error"] is not None
        assert data["per_engine"] == []
        assert data["per_ticker"] == []
    finally:
        for v in ("RECORDER_DB_PATH", "BAROMETER_SINCE_DATE"):
            os.environ.pop(v, None)
        sys.modules.pop("omega_dashboard.barometer_data", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────
# Per-engine summary
# ─────────────────────────────────────────────────────────────────────

def test_per_engine_summary_basic_aggregation():
    """6 LCB alerts: 5 with pnl outcomes (4 winners, 1 loser), 1 without.
    Sample sizes — 6 alerts ≥ MIN_SAMPLE_FOR_PCT (5) → pt rates emit;
    5 with_pnl ≥ MIN_SAMPLE_FOR_PCT (5) → win_rate emits. Numerically:
    win_rate = 4/5 = 80%, pt1 ANY-touch = 4/6 = 66.7%, avg_pnl = 34.20."""
    tmpdir, db = _setup_db()
    try:
        fired_at = _utc_micros(2026, 5, 16)
        a1, a2, a3, a4, a5, a6 = (_u(f"a{i}") for i in range(1, 7))
        for aid in (a1, a2, a3, a4, a5, a6):
            _insert_alert(db, aid, fired_at, engine="long_call_burst")
        # a1: winner +25, hit_pt1 (5min + 1h — final-pnl uses the 1h row)
        _insert_outcome(db, a1, "5min", 22.0, hit_pt1=1, mfe=22.0, mae=-2.0)
        _insert_outcome(db, a1, "1h",   25.0, hit_pt1=1, mfe=30.0, mae=-2.0)
        # a2: winner +55, hit_pt1 + pt2
        _insert_outcome(db, a2, "1h",   55.0, hit_pt1=1, hit_pt2=1,
                        mfe=60.0, mae=-5.0)
        # a3: winner +101, hits all three
        _insert_outcome(db, a3, "1h",  101.0, hit_pt1=1, hit_pt2=1, hit_pt3=1,
                        mfe=110.0, mae=-1.0)
        # a4: loser -20, no hits
        _insert_outcome(db, a4, "1h",  -20.0, hit_pt1=0, hit_pt2=0, hit_pt3=0,
                        mfe=5.0, mae=-22.0)
        # a5: winner +10, hit_pt1 only
        _insert_outcome(db, a5, "1h",   10.0, hit_pt1=1, hit_pt2=0, hit_pt3=0,
                        mfe=12.0, mae=-3.0)
        # a6: no outcomes — counts toward `alerts` but not `with_pnl`.

        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        assert len(data["per_engine"]) == 1
        row = data["per_engine"][0]
        assert row["engine"] == "long_call_burst"
        assert row["alerts"] == 6
        assert row["with_pnl"] == 5
        # 4 winners out of 5 with pnl = 80%
        assert row["win_rate"] == 80.0, f"win_rate={row.get('win_rate')}"
        # avg_pnl over the 5 with-pnl alerts: (25+55+101-20+10)/5 = 34.20
        assert abs(row["avg_pnl_pct"] - 34.20) < 0.01
        # PT-hit ANY-touch over all alerts (denominator = 6):
        # 4 of 6 hit pt1 → 66.7
        assert abs(row["pt1_rate"] - 66.7) < 0.1, f"pt1_rate={row.get('pt1_rate')}"
        # 2 of 6 hit pt2 → 33.3
        assert abs(row["pt2_rate"] - 33.3) < 0.1
        # 1 of 6 hit pt3 → 16.7
        assert abs(row["pt3_rate"] - 16.7) < 0.1
    finally:
        _teardown_db(tmpdir)


def test_small_sample_suppresses_percentages():
    """Engine with only 3 alerts (< MIN_SAMPLE_FOR_PCT=5) — pt_rate /
    win_rate fields must NOT appear in the result so the template can
    render 'small sample' tag."""
    tmpdir, db = _setup_db()
    try:
        fired_at = _utc_micros(2026, 5, 16)
        for i in range(3):
            aid = _u(f"small{i}")
            _insert_alert(db, aid, fired_at, engine="credit_v84")
            _insert_outcome(db, aid, "1h", 5.0, hit_pt1=1)
        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        row = next(r for r in data["per_engine"] if r["engine"] == "credit_v84")
        assert row["alerts"] == 3
        assert "pt1_rate" not in row, (
            f"pt1_rate must be absent for small sample; got {row}"
        )
        assert "win_rate" not in row
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Per-engine × direction
# ─────────────────────────────────────────────────────────────────────

def test_per_engine_direction_splits_bull_bear():
    """6 bull + 6 bear LCB alerts produce 2 rows (engine, direction).
    Bull pnl avg differs from bear pnl avg — proves the GROUP BY is real."""
    tmpdir, db = _setup_db()
    try:
        fired_at = _utc_micros(2026, 5, 16)
        # 6 bulls, all winners at +30
        for i in range(6):
            aid = _u(f"bull{i}")
            _insert_alert(db, aid, fired_at, direction="bull")
            _insert_outcome(db, aid, "1h", 30.0, hit_pt1=1, hit_pt2=0)
        # 6 bears, all losers at -10
        for i in range(6):
            aid = _u(f"bear{i}")
            _insert_alert(db, aid, fired_at, direction="bear")
            _insert_outcome(db, aid, "1h", -10.0, hit_pt1=0)
        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        rows = {(r["engine"], r["direction"]): r
                for r in data["per_engine_direction"]}
        assert ("long_call_burst", "bull") in rows
        assert ("long_call_burst", "bear") in rows
        assert rows[("long_call_burst", "bull")]["avg_pnl_pct"] == 30.0
        assert rows[("long_call_burst", "bear")]["avg_pnl_pct"] == -10.0
        assert rows[("long_call_burst", "bull")]["win_rate"] == 100.0
        assert rows[("long_call_burst", "bear")]["win_rate"] == 0.0
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Per-ticker leaderboard
# ─────────────────────────────────────────────────────────────────────

def test_per_ticker_leaderboard_excludes_below_3_alerts():
    """Tickers with <3 alerts excluded; ticker with exactly 3 included.
    Larger ticker sorts first."""
    tmpdir, db = _setup_db()
    try:
        fired_at = _utc_micros(2026, 5, 16)
        # SPY: 5 alerts
        for i in range(5):
            _insert_alert(db, _u(f"spy{i}"), fired_at, ticker="SPY")
        # QQQ: 3 alerts (boundary — included)
        for i in range(3):
            _insert_alert(db, _u(f"qqq{i}"), fired_at, ticker="QQQ")
        # NVDA: 2 alerts (excluded)
        for i in range(2):
            _insert_alert(db, _u(f"nvda{i}"), fired_at, ticker="NVDA")

        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        tickers = [r["ticker"] for r in data["per_ticker"]]
        assert "SPY" in tickers
        assert "QQQ" in tickers
        assert "NVDA" not in tickers, "ticker with <3 alerts must be excluded"
        # Sort order: highest count first
        assert tickers[0] == "SPY", f"order: {tickers}"
    finally:
        _teardown_db(tmpdir)


# ─────────────────────────────────────────────────────────────────────
# Caching
# ─────────────────────────────────────────────────────────────────────

def test_cache_hit_skips_db_query():
    """When Redis returns a cached payload, get_barometer_data returns it
    immediately without opening a SQLite connection — verified by
    deleting the DB file first so any DB access would raise."""
    tmpdir, db = _setup_db()
    try:
        cached_payload = {
            "per_engine": [{"engine": "from_cache", "alerts": 99}],
            "per_engine_direction": [],
            "per_ticker": [],
            "min_sample_for_pct": 5,
            "error": None,
        }

        class _FakeRedis:
            def __init__(self, payload):
                self._payload = json.dumps(payload).encode()
                self.gets = []

            def get(self, key):
                self.gets.append(key)
                return self._payload

            def setex(self, *args, **kwargs):
                raise AssertionError("cache hit should NOT write")

        fake = _FakeRedis(cached_payload)

        # Delete the DB so any SQLite open would fail loudly.
        os.remove(db)

        from omega_dashboard import barometer_data
        with mock.patch.object(barometer_data, "_get_redis", return_value=fake):
            data = barometer_data.get_barometer_data(use_cache=True)
        assert data["per_engine"] == [{"engine": "from_cache", "alerts": 99}]
        assert fake.gets, "cache must have been consulted"
    finally:
        _teardown_db(tmpdir)


def test_since_date_filter_excludes_old_alerts():
    """Alerts fired before BAROMETER_SINCE_DATE are filtered out."""
    tmpdir, db = _setup_db()
    # Override the wide-open default with a specific cutoff
    os.environ["BAROMETER_SINCE_DATE"] = "2026-05-16"
    sys.modules.pop("omega_dashboard.barometer_data", None)
    try:
        # 5 alerts before the cutoff — should NOT count
        for i in range(5):
            _insert_alert(db, _u(f"old{i}"),
                          _utc_micros(2026, 5, 1), ticker="OLD")
        # 6 alerts after the cutoff — SHOULD count
        for i in range(6):
            _insert_alert(db, _u(f"new{i}"),
                          _utc_micros(2026, 5, 17), ticker="NEW")
        from omega_dashboard import barometer_data
        data = barometer_data.get_barometer_data(use_cache=False)
        row = data["per_engine"][0]
        assert row["alerts"] == 6, (
            f"only post-cutoff alerts must count; got {row['alerts']}"
        )
        tickers = [r["ticker"] for r in data["per_ticker"]]
        assert "OLD" not in tickers
        assert "NEW" in tickers
    finally:
        _teardown_db(tmpdir)


if __name__ == "__main__":
    tests = [
        test_empty_db_returns_empty_sections,
        test_missing_db_returns_error_state,
        test_per_engine_summary_basic_aggregation,
        test_small_sample_suppresses_percentages,
        test_per_engine_direction_splits_bull_bear,
        test_per_ticker_leaderboard_excludes_below_3_alerts,
        test_cache_hit_skips_db_query,
        test_since_date_filter_excludes_old_alerts,
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
