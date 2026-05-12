"""Alert recorder — write side.

# v11.7 (Patch G.2): the only module that writes to the recorder DB.
# Engines call record_alert(...) after their card posts. The tracker
# daemon (G.7) calls record_track_sample(...). The outcome computer (G.8)
# calls record_outcome(...). Every entrypoint is wrapped in try/except —
# recorder failure NEVER affects engine behavior.

See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md.
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RECORDER_VERSION = "v1.0.0"
DEFAULT_DB_PATH = "/var/backtest/desk.db"

# Standard horizon labels used by tracker (sampling) and outcome computer
# (boundary detection). Keys are display labels; values are seconds.
HORIZONS_SECONDS = {
    "5min":  5 * 60,
    "15min": 15 * 60,
    "30min": 30 * 60,
    "1h":    60 * 60,
    "4h":    4 * 60 * 60,
    "1d":    24 * 60 * 60,
    "2d":    2 * 24 * 60 * 60,
    "3d":    3 * 24 * 60 * 60,
    "5d":    5 * 24 * 60 * 60,
}

# Sampling cadence buckets: (lower_bound_seconds, cadence_seconds).
SAMPLING_CADENCE = [
    (0,                    60),
    (60 * 60,              5 * 60),
    (4 * 60 * 60,          15 * 60),
    (24 * 60 * 60,         30 * 60),
    (7 * 24 * 60 * 60,     60 * 60),
]

# Per-engine tracking horizon (seconds). None means "use suggested_dte".
TRACKING_HORIZON_BY_ENGINE = {
    "long_call_burst":     3 * 24 * 60 * 60,
    "v2_5d":               7 * 24 * 60 * 60,
    "credit_v84":          None,
    "oi_flow_conviction":  5 * 24 * 60 * 60,
}


def _utc_micros() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def _master_enabled() -> bool:
    return os.getenv("RECORDER_ENABLED", "false").lower() in ("1", "true", "yes")


def _engine_enabled(engine: str) -> bool:
    """Master gate AND per-engine gate must both be on."""
    if not _master_enabled():
        return False
    flag = {
        "long_call_burst":     "RECORDER_LCB_ENABLED",
        "v2_5d":               "RECORDER_V25D_ENABLED",
        "credit_v84":          "RECORDER_CREDIT_ENABLED",
        "oi_flow_conviction":  "RECORDER_CONVICTION_ENABLED",
    }.get(engine)
    if flag is None:
        return False
    return os.getenv(flag, "false").lower() in ("1", "true", "yes")


def _db_path() -> str:
    return os.getenv("RECORDER_DB_PATH", DEFAULT_DB_PATH)


_conn_lock = threading.Lock()
_conn_cache: Dict[str, sqlite3.Connection] = {}


def _conn() -> sqlite3.Connection:
    """Per-path connection cache. SQLite connections aren't thread-safe to
    share by default, but we guard each write with a process-wide lock so
    a single connection per path is fine for V1 write volumes."""
    path = _db_path()
    with _conn_lock:
        c = _conn_cache.get(path)
        if c is None:
            c = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            _conn_cache[path] = c
        return c


def _stringify_unserializable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _stringify_unserializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_unserializable(x) for x in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _safe_json(obj: Any) -> Optional[str]:
    """Serialize to JSON. Falls back to stringifying unserializable values
    rather than raising."""
    try:
        return json.dumps(obj, default=str, allow_nan=False)
    except (TypeError, ValueError):
        try:
            return json.dumps(_stringify_unserializable(obj), allow_nan=False)
        except Exception:
            return None


def _split_feature(value: Any):
    """EAV split: numeric -> (val, None); string -> (None, text);
    bool -> (1.0/0.0, None); None -> returns None (caller skips)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return (1.0 if value else 0.0, None)
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return (float(value), None)
    if isinstance(value, str):
        return (None, value)
    return (None, str(value))


# v11.7 (Patch G.13): subscribe option streaming to alert legs.
def _subscribe_alert_legs(structure: Optional[Dict[str, Any]], ticker: str) -> None:
    """Subscribe option streaming to all OCC symbols referenced in this
    alert's suggested_structure.

    Called from record_alert after the row INSERT commits. Fixes the
    silent track-data gap where alert legs fell outside the default
    ATM streaming window (~16 symbols per ticker) and
    OptionQuoteStore.get_live_premium returned None on every tracker
    sample — leaving structure_mark NULL across the alert's lifetime.
    After subscription, the streaming connection adds the legs within
    1-2s; samples from that point have live marks.

    Subscribe-only — no REST fallback. Per-sample REST would pressure
    the shared Schwab rate limit on a continuous-tracking workload.
    The first 0-2 minutes of a new alert may have NULL marks while
    subscription propagates; outcomes happen at 5min+ horizons so
    they're unaffected.

    Pure additive: any failure (missing manager, OCC build error,
    streaming hiccup) logs but never raises. Caller wraps in its own
    try/except as a second layer of defense — recorder hooks NEVER
    affect engines (CLAUDE.md, recorder-default-state decision).

    Supported structure types:
      - long_call / long_put: single leg, strike from structure['strike']
      - bull_put / bear_call: two legs, structure['short'] + 'long'
        (G.11 field-name convention — no _strike suffix)
    Expiry from structure['expiry'] in either case. Unknown types
    log at DEBUG and no-op.
    """
    if not isinstance(structure, dict):
        return
    expiry = structure.get("expiry")
    if not expiry:
        return

    # Lazy import: schwab_stream is a large module and the recorder
    # module is imported during tests that don't have streaming set up.
    # Any import-time error here is captured and downgrades to a no-op.
    try:
        from schwab_stream import build_occ_symbol, get_option_symbol_manager
        manager = get_option_symbol_manager()
        if manager is None:
            log.debug("recorder G.13: option symbol manager not initialized; skip")
            return
    except Exception as e:
        log.debug(f"recorder G.13: schwab_stream import failed: {e}")
        return

    stype = (structure.get("type") or "").lower()
    occ_symbols: List[str] = []
    try:
        if stype in ("long_call", "long_put"):
            strike = structure.get("strike")
            if strike is None:
                return
            side = "call" if stype == "long_call" else "put"
            occ_symbols.append(
                build_occ_symbol(ticker, expiry, side, float(strike))
            )
        elif stype in ("bull_put", "bear_call"):
            short = structure.get("short")
            long_ = structure.get("long")
            if short is None or long_ is None:
                return
            side = "put" if stype == "bull_put" else "call"
            occ_symbols.extend([
                build_occ_symbol(ticker, expiry, side, float(short)),
                build_occ_symbol(ticker, expiry, side, float(long_)),
            ])
        else:
            log.debug(
                f"recorder G.13: unknown structure type {stype!r} for {ticker}"
            )
            return
    except Exception as e:
        log.warning(f"recorder G.13: OCC build failed for {ticker}: {e}")
        return

    if not occ_symbols:
        return

    try:
        # subscribe_specific is idempotent — OptionSymbolManager filters
        # out OCCs already in self._subscribed before forwarding to the
        # stream manager (schwab_stream.py:2444). Re-subscribing the
        # same leg from a repeat alert is a no-op.
        manager.subscribe_specific(occ_symbols)
        log.info(
            f"recorder G.13: subscribed {len(occ_symbols)} alert OCC "
            f"symbols for {ticker} ({stype})"
        )
        # Pool-size telemetry. The actual property is manager.status
        # (a dict with 'subscribed' int), NOT the spec-hypothesized
        # get_subscription_count() method. V1 strategy is
        # deploy-as-cleanup: each redeploy rebuilds streaming from
        # FLOW_TICKERS at the ~560-symbol baseline, dropping all
        # alert-leg subscriptions. Warn before the ~1000 Schwab
        # streaming ceiling so long deploy windows surface the risk.
        try:
            current_size = int(manager.status.get("subscribed", 0))
            threshold = int(os.environ.get(
                "RECORDER_SUBSCRIPTION_POOL_WARN_THRESHOLD", "800"))
            if current_size > threshold:
                log.warning(
                    f"recorder G.13: option subscription pool at "
                    f"{current_size} symbols (threshold {threshold}). "
                    f"Approaching Schwab streaming ceiling (~1000). "
                    f"Deploy resets pool to ~560 baseline."
                )
        except Exception as e:
            log.debug(f"recorder G.13: pool size telemetry failed: {e}")
    except Exception as e:
        log.warning(
            f"recorder G.13: subscribe_specific failed for {ticker}: {e}"
        )


def _auto_subscribe_enabled() -> bool:
    """Gate for Patch G.13. Defaults ON (per spec — the behavior change
    is a fix, not an opt-in feature). Set false to revert to the old
    behavior where legs outside the ATM window stay unsubscribed."""
    return os.getenv(
        "RECORDER_AUTO_SUBSCRIBE_ENABLED", "true"
    ).lower() in ("1", "true", "yes")


def record_alert(
    *,
    engine: str,
    engine_version: str,
    ticker: str,
    classification: Optional[str],
    direction: Optional[str],
    suggested_structure: Optional[Dict[str, Any]],
    suggested_dte: Optional[int],
    spot_at_fire: Optional[float],
    canonical_snapshot: Optional[Dict[str, Any]],
    raw_engine_payload: Optional[Dict[str, Any]],
    features: Optional[Dict[str, Any]],
    telegram_chat: Optional[str],
    parent_alert_id: Optional[str] = None,
    posted_to_telegram: bool = True,
    suppression_reason: Optional[str] = None,
) -> Optional[str]:
    """Record an alert. Returns alert_id on success, None on no-op or
    failure. NEVER raises."""
    if not _engine_enabled(engine):
        return None
    try:
        alert_id = str(uuid.uuid4())
        fired_at = _utc_micros()
        conn = _conn()
        with _conn_lock:
            conn.execute(
                "INSERT INTO alerts (alert_id, fired_at, engine, engine_version, "
                "ticker, classification, direction, suggested_structure, "
                "suggested_dte, spot_at_fire, canonical_snapshot, "
                "raw_engine_payload, parent_alert_id, posted_to_telegram, "
                "telegram_chat, suppression_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alert_id, fired_at, engine, engine_version,
                    ticker, classification, direction,
                    _safe_json(suggested_structure or {}),
                    suggested_dte, spot_at_fire,
                    _safe_json(canonical_snapshot or {}),
                    _safe_json(raw_engine_payload or {}),
                    parent_alert_id,
                    1 if posted_to_telegram else 0,
                    telegram_chat, suppression_reason,
                ),
            )
            if features:
                rows = []
                for name, value in features.items():
                    split = _split_feature(value)
                    if split is None:
                        continue
                    feat_val, feat_text = split
                    rows.append((alert_id, name, feat_val, feat_text))
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO alert_features "
                        "(alert_id, feature_name, feature_value, feature_text) "
                        "VALUES (?,?,?,?)",
                        rows,
                    )
            conn.commit()
        # v11.7 (Patch G.13): subscribe option streaming to the alert's
        # structure legs so the tracker daemon can read non-NULL marks.
        # Two layers of defense — the helper has its own try/except,
        # AND we wrap the call so an unexpected error never causes us
        # to return None (the alert row IS already committed at this
        # point).
        if _auto_subscribe_enabled():
            try:
                _subscribe_alert_legs(suggested_structure, ticker)
            except Exception as e:
                log.warning(
                    f"recorder G.13: subscribe wrapper failed for {ticker}: {e}"
                )
        return alert_id
    except Exception as e:
        log.warning(f"recorder: record_alert({engine}) failed: {e}")
        return None


def record_track_sample(
    *,
    alert_id: str,
    elapsed_seconds: int,
    underlying_price: Optional[float],
    structure_mark: Optional[float],
    structure_pnl_pct: Optional[float],
    structure_pnl_abs: Optional[float],
    market_state: Optional[str],
) -> bool:
    """Insert one alert_price_track row. Master gate must be on."""
    if not _master_enabled():
        return False
    try:
        sampled_at = _utc_micros()
        conn = _conn()
        with _conn_lock:
            conn.execute(
                "INSERT OR REPLACE INTO alert_price_track "
                "(alert_id, elapsed_seconds, sampled_at, underlying_price, "
                "structure_mark, structure_pnl_pct, structure_pnl_abs, "
                "market_state) VALUES (?,?,?,?,?,?,?,?)",
                (alert_id, elapsed_seconds, sampled_at, underlying_price,
                 structure_mark, structure_pnl_pct, structure_pnl_abs,
                 market_state),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning(f"recorder: record_track_sample({alert_id}) failed: {e}")
        return False


def record_outcome(
    *,
    alert_id: str,
    horizon: str,
    outcome_at: Optional[int],
    underlying_price: Optional[float],
    structure_mark: Optional[float],
    pnl_pct: Optional[float],
    pnl_abs: Optional[float],
    hit_pt1: int,
    hit_pt2: int,
    hit_pt3: int,
    max_favorable_pct: Optional[float],
    max_adverse_pct: Optional[float],
) -> bool:
    """Insert/replace one alert_outcomes row. Idempotent."""
    if not _master_enabled():
        return False
    try:
        conn = _conn()
        with _conn_lock:
            conn.execute(
                "INSERT OR REPLACE INTO alert_outcomes "
                "(alert_id, horizon, outcome_at, underlying_price, "
                "structure_mark, pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3, "
                "max_favorable_pct, max_adverse_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (alert_id, horizon, outcome_at, underlying_price,
                 structure_mark, pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3,
                 max_favorable_pct, max_adverse_pct),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning(f"recorder: record_outcome({alert_id},{horizon}) failed: {e}")
        return False


def get_alert(alert_id: str) -> Optional[Dict[str, Any]]:
    """Read-side helper. Returns None if not found."""
    try:
        conn = _conn()
        with _conn_lock:
            row = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "classification, direction, suggested_structure, "
                "suggested_dte, spot_at_fire, canonical_snapshot, "
                "raw_engine_payload, parent_alert_id, posted_to_telegram, "
                "telegram_chat, suppression_reason FROM alerts "
                "WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
        if row is None:
            return None
        keys = ("alert_id", "fired_at", "engine", "engine_version", "ticker",
                "classification", "direction", "suggested_structure",
                "suggested_dte", "spot_at_fire", "canonical_snapshot",
                "raw_engine_payload", "parent_alert_id", "posted_to_telegram",
                "telegram_chat", "suppression_reason")
        return dict(zip(keys, row))
    except Exception as e:
        log.warning(f"recorder: get_alert({alert_id}) failed: {e}")
        return None


def list_active_alerts() -> List[Dict[str, Any]]:
    """Read-side: alerts whose tracking horizon has not expired. Used by
    alert_tracker_daemon (G.7). Returns [] on error."""
    try:
        now = _utc_micros()
        conn = _conn()
        with _conn_lock:
            rows = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "suggested_structure, suggested_dte, spot_at_fire "
                "FROM alerts ORDER BY fired_at DESC LIMIT 500"
            ).fetchall()
        out = []
        for row in rows:
            (alert_id, fired_at, engine, engine_version, ticker,
             struct, dte, spot) = row
            elapsed = (now - fired_at) // 1_000_000
            horizon = TRACKING_HORIZON_BY_ENGINE.get(engine)
            if horizon is None and dte:
                horizon = int(dte) * 24 * 60 * 60
            if horizon is None or elapsed > horizon:
                continue
            try:
                struct_dict = json.loads(struct) if struct else {}
            except Exception:
                struct_dict = {}
            out.append({
                "alert_id": alert_id,
                "fired_at": fired_at,
                "engine": engine,
                "engine_version": engine_version,
                "ticker": ticker,
                "suggested_structure": struct_dict,
                "suggested_dte": dte,
                "spot_at_fire": spot,
                "elapsed_seconds": elapsed,
            })
        return out
    except Exception as e:
        log.warning(f"recorder: list_active_alerts failed: {e}")
        return []
