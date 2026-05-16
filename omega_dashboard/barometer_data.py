"""Barometer dashboard data layer.

# v11.7 (Patch I V0): per-engine + per-engine x direction + per-ticker
# outcome aggregates surfaced on the /barometer page. Reads /var/backtest/desk.db
# via a per-call read-only sqlite3 connection (URI mode=ro). Cached in
# Redis with 5-minute TTL.

Three sections:
  - per_engine          summary across all engines
  - per_engine_direction split by direction (bull/bear)
  - per_ticker          top-N tickers by alert count

Each row shows raw counts always; percentage fields (pt_rate, win_rate,
avg_pnl_pct) are only included when the sample size meets
MIN_SAMPLE_FOR_PCT, so the template can render a 'small sample' tag
without doing the threshold math itself.

The /barometer route reads from G.13.1+G.15-deployed data quality by
default (BAROMETER_SINCE_DATE='2026-05-16'). Override with an env var
to widen the window if needed.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/var/backtest/desk.db"

# v11.7 (Patch I V0): hide pt/win rates for engines below this sample
# count so the template can show 'small sample' instead of a noisy %.
MIN_SAMPLE_FOR_PCT = 5

# Cache the full payload for 5 minutes so successive page loads don't
# rerun aggregates. The barometer changes slowly enough (one alert
# every 5-15 minutes) that 5min staleness is fine.
CACHE_KEY = "dashboard:barometer:v0"
CACHE_TTL_SEC = 300

# Top-N tickers by alert count on the leaderboard.
LEADERBOARD_LIMIT = 10

# Minimum alerts per ticker to be considered in the leaderboard at all.
TICKER_MIN_ALERTS = 3

# Default since-date — G.13.1 deploy date (2026-05-16) when credit and
# long-call mark coverage went from <25% to >75%. Pre-G.13.1 data is
# noisier and would skew aggregates downward. Override via the
# BAROMETER_SINCE_DATE env var (format: YYYY-MM-DD).
DEFAULT_SINCE_DATE = "2026-05-16"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _db_path() -> str:
    return os.getenv("RECORDER_DB_PATH", DEFAULT_DB_PATH)


def _open_ro() -> sqlite3.Connection:
    """Read-only sqlite3 connection. URI mode=ro rejects missing files,
    which is what we want — caller catches sqlite3.OperationalError and
    returns the error-state payload."""
    return sqlite3.connect(
        f"file:{_db_path()}?mode=ro", uri=True, timeout=2.0,
    )


def _since_micros() -> int:
    """Returns the cutoff (UTC microseconds) — alerts fired before this
    are excluded from all aggregates. Reads BAROMETER_SINCE_DATE env
    var (YYYY-MM-DD). Falls back to 0 (include everything) on parse
    failure rather than blocking the whole dashboard."""
    raw = os.getenv("BAROMETER_SINCE_DATE", DEFAULT_SINCE_DATE)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000)
    except Exception as e:
        log.warning(f"barometer: BAROMETER_SINCE_DATE={raw!r} unparseable ({e}); "
                    "including all alerts")
        return 0


def _get_redis():
    """Lazy import of app._get_redis to avoid circular imports at module
    load. Returns None on any failure — the caller falls through to a
    live DB query."""
    try:
        from app import _get_redis as _app_get_redis
        return _app_get_redis()
    except Exception as e:
        log.debug(f"barometer: redis unavailable: {e}")
        return None


def _empty_payload(error: Optional[str] = None) -> Dict[str, Any]:
    return {
        "per_engine": [],
        "per_engine_direction": [],
        "per_ticker": [],
        "min_sample_for_pct": MIN_SAMPLE_FOR_PCT,
        "since_date": os.getenv("BAROMETER_SINCE_DATE", DEFAULT_SINCE_DATE),
        "error": error,
    }


# ─────────────────────────────────────────────────────────────
# Aggregation queries
# ─────────────────────────────────────────────────────────────

# Horizon priority for "the latest pnl we have" — used both in the
# per_engine and per_ticker leaderboard queries. Standard horizons run
# 5min → 5d → expiry; the longest realized horizon at the time of the
# pass is the alert's "final" pnl.
_HORIZON_ORDER_CASE = (
    "CASE oo.horizon "
    "WHEN '5min' THEN 1 WHEN '15min' THEN 2 WHEN '30min' THEN 3 "
    "WHEN '1h' THEN 4 WHEN '4h' THEN 5 WHEN '1d' THEN 6 "
    "WHEN '2d' THEN 7 WHEN '3d' THEN 8 WHEN '5d' THEN 9 "
    "WHEN 'expiry' THEN 10 ELSE 99 END"
)


def _build_alert_best_cte() -> str:
    """Shared CTE: one row per alert with rolled-up MFE/MAE/PT-hit/final-pnl.

    final_pnl is the pnl_pct from the longest-horizon outcome with a
    non-NULL pnl_pct (later horizons trump earlier ones).
    """
    return f"""
        WITH alert_best AS (
          SELECT
            a.alert_id,
            a.engine,
            a.direction,
            a.ticker,
            MAX(o.max_favorable_pct) AS best_mfe,
            MIN(o.max_adverse_pct)   AS worst_mae,
            MAX(o.hit_pt1) AS any_pt1,
            MAX(o.hit_pt2) AS any_pt2,
            MAX(o.hit_pt3) AS any_pt3,
            (
              SELECT oo.pnl_pct
              FROM alert_outcomes oo
              WHERE oo.alert_id = a.alert_id AND oo.pnl_pct IS NOT NULL
              ORDER BY {_HORIZON_ORDER_CASE} DESC
              LIMIT 1
            ) AS final_pnl
          FROM alerts a
          LEFT JOIN alert_outcomes o ON o.alert_id = a.alert_id
          WHERE a.fired_at >= ?
          GROUP BY a.alert_id, a.engine, a.direction, a.ticker
        )
    """


def _summary_item(engine: str, alerts: int, with_outcomes: int,
                  pt1: Optional[int], pt2: Optional[int], pt3: Optional[int],
                  mfe: Optional[float], mae: Optional[float],
                  with_pnl: int, winners: Optional[int],
                  avg_pnl: Optional[float],
                  direction: Optional[str] = None) -> Dict[str, Any]:
    """Shape a single aggregate row. Percentage fields are emitted only
    when sample size hits MIN_SAMPLE_FOR_PCT so the template can decide
    whether to render them."""
    item: Dict[str, Any] = {
        "engine": engine,
        "alerts": int(alerts),
        "with_outcomes": int(with_outcomes or 0),
        "with_pnl": int(with_pnl or 0),
    }
    if direction is not None:
        item["direction"] = direction or "unknown"
    if alerts >= MIN_SAMPLE_FOR_PCT and (with_outcomes or 0) > 0:
        item["pt1_rate"] = round(100.0 * (pt1 or 0) / alerts, 1)
        item["pt2_rate"] = round(100.0 * (pt2 or 0) / alerts, 1)
        item["pt3_rate"] = round(100.0 * (pt3 or 0) / alerts, 1)
    if mfe is not None:
        item["avg_mfe_pct"] = round(mfe, 1)
    if mae is not None:
        item["avg_mae_pct"] = round(mae, 1)
    if with_pnl and with_pnl >= MIN_SAMPLE_FOR_PCT:
        item["win_rate"] = round(100.0 * (winners or 0) / with_pnl, 1)
        item["avg_pnl_pct"] = round(avg_pnl or 0, 2)
    return item


def _per_engine_summary(conn: sqlite3.Connection,
                        since_micros: int) -> List[Dict[str, Any]]:
    sql = _build_alert_best_cte() + """
        SELECT
          engine,
          COUNT(*) AS alerts,
          SUM(CASE WHEN any_pt1 IS NOT NULL THEN 1 ELSE 0 END) AS with_outcomes,
          SUM(any_pt1) AS pt1_hits,
          SUM(any_pt2) AS pt2_hits,
          SUM(any_pt3) AS pt3_hits,
          AVG(best_mfe) AS avg_mfe,
          AVG(worst_mae) AS avg_mae,
          SUM(CASE WHEN final_pnl IS NOT NULL THEN 1 ELSE 0 END) AS with_pnl,
          SUM(CASE WHEN final_pnl > 0 THEN 1 ELSE 0 END) AS winners,
          AVG(final_pnl) AS avg_pnl
        FROM alert_best
        GROUP BY engine
        ORDER BY alerts DESC
    """
    rows = conn.execute(sql, (since_micros,)).fetchall()
    return [
        _summary_item(
            engine=engine, alerts=alerts, with_outcomes=with_out,
            pt1=pt1, pt2=pt2, pt3=pt3,
            mfe=mfe, mae=mae,
            with_pnl=with_pnl, winners=winners, avg_pnl=avg_pnl,
        )
        for (engine, alerts, with_out, pt1, pt2, pt3,
             mfe, mae, with_pnl, winners, avg_pnl) in rows
    ]


def _per_engine_direction(conn: sqlite3.Connection,
                          since_micros: int) -> List[Dict[str, Any]]:
    """Mirror of _per_engine_summary with direction added to GROUP BY."""
    sql = _build_alert_best_cte() + """
        SELECT
          engine, direction,
          COUNT(*) AS alerts,
          SUM(CASE WHEN any_pt1 IS NOT NULL THEN 1 ELSE 0 END) AS with_outcomes,
          SUM(any_pt1) AS pt1_hits,
          SUM(any_pt2) AS pt2_hits,
          SUM(any_pt3) AS pt3_hits,
          AVG(best_mfe) AS avg_mfe,
          AVG(worst_mae) AS avg_mae,
          SUM(CASE WHEN final_pnl IS NOT NULL THEN 1 ELSE 0 END) AS with_pnl,
          SUM(CASE WHEN final_pnl > 0 THEN 1 ELSE 0 END) AS winners,
          AVG(final_pnl) AS avg_pnl
        FROM alert_best
        GROUP BY engine, direction
        ORDER BY engine ASC, alerts DESC
    """
    rows = conn.execute(sql, (since_micros,)).fetchall()
    return [
        _summary_item(
            engine=engine, alerts=alerts, with_outcomes=with_out,
            pt1=pt1, pt2=pt2, pt3=pt3,
            mfe=mfe, mae=mae,
            with_pnl=with_pnl, winners=winners, avg_pnl=avg_pnl,
            direction=direction,
        )
        for (engine, direction, alerts, with_out, pt1, pt2, pt3,
             mfe, mae, with_pnl, winners, avg_pnl) in rows
    ]


def _per_ticker_leaderboard(conn: sqlite3.Connection,
                            since_micros: int,
                            limit: int = LEADERBOARD_LIMIT,
                            ) -> List[Dict[str, Any]]:
    """Top-N tickers by alert count with win rate. Tickers with fewer
    than TICKER_MIN_ALERTS are excluded entirely (the leaderboard is
    meant for tickers we have a feel for)."""
    sql = _build_alert_best_cte() + """
        SELECT
          ticker,
          COUNT(*) AS alerts,
          GROUP_CONCAT(DISTINCT engine) AS engines,
          SUM(CASE WHEN final_pnl IS NOT NULL THEN 1 ELSE 0 END) AS with_pnl,
          SUM(CASE WHEN final_pnl > 0 THEN 1 ELSE 0 END) AS winners,
          AVG(final_pnl) AS avg_pnl
        FROM alert_best
        GROUP BY ticker
        HAVING COUNT(*) >= ?
        ORDER BY alerts DESC, ticker ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (since_micros, TICKER_MIN_ALERTS, limit)).fetchall()
    out: List[Dict[str, Any]] = []
    for (ticker, alerts, engines_csv, with_pnl, winners, avg_pnl) in rows:
        engines = sorted({e for e in (engines_csv or "").split(",") if e})
        item: Dict[str, Any] = {
            "ticker": ticker,
            "alerts": int(alerts),
            "engines": engines,
            "with_pnl": int(with_pnl or 0),
        }
        if with_pnl and with_pnl >= MIN_SAMPLE_FOR_PCT:
            item["win_rate"] = round(100.0 * (winners or 0) / with_pnl, 1)
            item["avg_pnl_pct"] = round(avg_pnl or 0, 2)
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def get_barometer_data(use_cache: bool = True) -> Dict[str, Any]:
    """Main entry point. Returns a dict suitable for direct render into
    the barometer template OR jsonify.

    use_cache=False forces a fresh DB query (useful for tests and for
    a future 'refresh now' button).
    """
    if use_cache:
        r = _get_redis()
        if r is not None:
            try:
                cached = r.get(CACHE_KEY)
                if cached:
                    if isinstance(cached, bytes):
                        cached = cached.decode("utf-8")
                    return json.loads(cached)
            except Exception as e:
                log.debug(f"barometer: cache read failed: {e}")

    since_micros = _since_micros()

    try:
        conn = _open_ro()
    except sqlite3.OperationalError as e:
        return _empty_payload(
            error=f"recorder DB unavailable ({e}). "
                  "Verify RECORDER_ENABLED=true and the daemon has fired at least once."
        )

    try:
        payload: Dict[str, Any] = {
            "per_engine": _per_engine_summary(conn, since_micros),
            "per_engine_direction": _per_engine_direction(conn, since_micros),
            "per_ticker": _per_ticker_leaderboard(conn, since_micros),
            "min_sample_for_pct": MIN_SAMPLE_FOR_PCT,
            "since_date": os.getenv("BAROMETER_SINCE_DATE", DEFAULT_SINCE_DATE),
            "error": None,
        }
    except Exception as e:
        log.warning(f"barometer: aggregation failed: {e}")
        return _empty_payload(error=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if use_cache:
        r = _get_redis()
        if r is not None:
            try:
                r.setex(CACHE_KEY, CACHE_TTL_SEC, json.dumps(payload))
            except Exception as e:
                log.debug(f"barometer: cache write failed: {e}")

    return payload
