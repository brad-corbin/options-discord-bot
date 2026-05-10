"""Alerts feed read-side data layer.

# v11.7 (Patch H.2): pure read-only access to /var/backtest/desk.db.
# Never imports alert_recorder write internals. Opens its own read-only
# sqlite3 connection per request (URI mode=ro), so writers and readers
# stay decoupled at the connection level too.

See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md for schema.
"""
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/var/backtest/desk.db"
LIST_LIMIT = 200                     # hard cap on alerts fetched per page load
CHICAGO_TZ = ZoneInfo("America/Chicago")

# v11.7 (Patch H.8): single track-bar horizon for visual simplicity.
# If outcome data shows distinct decay profiles per engine in the
# data we collect, swap to a per-engine dict in V1.1.
TRACKING_HORIZON_SECONDS = 72 * 60 * 60   # 3 days

# alert_recorder generates alert_ids via uuid.uuid4(). Reject anything
# that doesn't match this shape on the detail route — belt-and-suspenders
# with the SQL parameter binding and Flask's <string:> converter.
_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Single source of truth for engine display. Maps recorder engine slug
# to icon, label, and CSS class suffix. Templates and CSS read these
# via context — DO NOT inline icons in HTML.
ENGINE_DISPLAY: Dict[str, Dict[str, str]] = {
    "long_call_burst":     {"icon": "\U0001F680",         "label": "LONG CALL BURST", "stripe": "lcb"},          # rocket
    "v2_5d":               {"icon": "⚡",             "label": "V2 5D EDGE",      "stripe": "v25d"},         # lightning
    "credit_v84":          {"icon": "\U0001F48E",         "label": "v8.4 CREDIT",     "stripe": "credit"},       # gem
    "oi_flow_conviction":  {"icon": "\U0001F48E\U0001F6A8", "label": "CONVICTION PLAY", "stripe": "conviction"}, # gem + siren
}

# Per-engine DTE convention. Used ONLY on the detail page to tag the
# `suggested_dte` value with its unit. Card view never shows DTE.
DTE_CONVENTION: Dict[str, Optional[str]] = {
    "long_call_burst":     "trading",   # count_trading_days_between
    "credit_v84":          "calendar",  # (expiry - today).days
    "v2_5d":               None,        # no DTE recorded
    "oi_flow_conviction":  None,        # no DTE recorded
}

# Recorder env-var slugs (canonical names from alert_recorder.py:63-79).
# Read at request time so flipping in Render takes effect on next page load.
ENGINE_FLAG_VARS = {
    "long_call_burst":     "RECORDER_LCB_ENABLED",
    "v2_5d":               "RECORDER_V25D_ENABLED",
    "credit_v84":          "RECORDER_CREDIT_ENABLED",
    "oi_flow_conviction":  "RECORDER_CONVICTION_ENABLED",
}
DAEMON_FLAG_VARS = {
    "tracker":  "RECORDER_TRACKER_ENABLED",
    "outcomes": "RECORDER_OUTCOMES_ENABLED",
}


def _db_path() -> str:
    return os.getenv("RECORDER_DB_PATH", DEFAULT_DB_PATH)


def _open_ro() -> sqlite3.Connection:
    """Open a read-only connection to the recorder DB. Caller closes.
    Raises sqlite3.OperationalError if the file doesn't exist (URI ro
    mode rejects missing files, which is what we want — list_alerts
    catches this and returns the friendly empty state)."""
    return sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=2.0)


def _now_micros() -> int:
    """UTC microseconds. Tests can pass an explicit now_micros to fix 'now'."""
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def _flag_on(var: str) -> bool:
    return os.getenv(var, "false").lower() in ("1", "true", "yes")


def _short_expiry(expiry: Any) -> str:
    """'2026-05-15' -> '5/15'. Returns the raw value as str on parse fail."""
    try:
        s = str(expiry)
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return f"{int(s[5:7])}/{int(s[8:10])}"
        return s
    except Exception:
        return str(expiry)


def _bucket(fired_at_micros: int, now_micros: int) -> str:
    """Returns one of 'today' / 'yesterday' / 'this_week' / 'earlier'.
    All bucket boundaries are CT calendar-date based (handles DST via
    zoneinfo). fired_at and now are both UTC microseconds."""
    fired_dt = datetime.fromtimestamp(fired_at_micros / 1_000_000, tz=timezone.utc)
    now_dt = datetime.fromtimestamp(now_micros / 1_000_000, tz=timezone.utc)
    fired_ct_date = fired_dt.astimezone(CHICAGO_TZ).date()
    now_ct_date = now_dt.astimezone(CHICAGO_TZ).date()
    delta_days = (now_ct_date - fired_ct_date).days
    if delta_days <= 0:
        return "today"
    if delta_days == 1:
        return "yesterday"
    if delta_days <= 7:
        return "this_week"
    return "earlier"


def _humanize_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 60 * 60:
        return f"{seconds // 60}m ago"
    if seconds < 24 * 60 * 60:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    return f"{seconds // 86400}d ago"


def _compute_status_badge(engine: str, elapsed_seconds: int,
                          latest_pnl: Optional[float]) -> tuple:
    """Return (text, style_class) for the row-1 status badge.

    First-match-wins logic:
      1. engine == 'v2_5d'                   -> ('EVAL',     'eval')
      2. elapsed > TRACKING_HORIZON_SECONDS  -> ('EXPIRED',  'expired')
      3. latest_pnl is not None              -> ('+N%' or '-N%', positive/negative)
      4. else                                -> ('ACTIVE',   'active')

    PT-hit info is intentionally NOT in the badge — it lives on row 5.
    Badge focuses on "is this making money now"; row 5 carries
    trajectory + PT detail.
    """
    if engine == "v2_5d":
        return ("EVAL", "eval")
    if elapsed_seconds > TRACKING_HORIZON_SECONDS:
        return ("EXPIRED", "expired")
    if latest_pnl is not None:
        sign = "+" if latest_pnl >= 0 else ""
        text = f"{sign}{int(round(latest_pnl))}%"
        cls = "positive" if latest_pnl >= 0 else "negative"
        return (text, cls)
    return ("ACTIVE", "active")


def format_structure_summary(engine: str, structure: Any,
                             classification: Optional[str] = None,
                             direction: Optional[str] = None) -> str:
    """Build the one-line summary shown on each card.

    Defensive: tolerates missing keys, unknown engine types, and
    malformed structure values. Returns a fallback string rather
    than raising. Never includes DTE — DTE convention varies per
    engine and is shown only on the detail page with a tag.

    For v2_5d the structure JSON is just {"type": "evaluation"} —
    grade and bias live on the alert row's classification and
    direction columns. Callers pass those in.
    """
    try:
        if not isinstance(structure, dict):
            # Distinct from "[partial data]" (dict given but fields missing)
            # and "[unknown type]" (dict given, type not recognized).
            return f"{engine} [no structure]"
        stype = structure.get("type")
        if engine == "long_call_burst" or stype == "long_call":
            strike = structure.get("strike")
            expiry = structure.get("expiry")
            entry = structure.get("entry_mark")
            if strike is None or expiry is None:
                return f"{engine} [partial data]"
            entry_str = f" @ ${entry:.2f}" if isinstance(entry, (int, float)) else ""
            return f"${strike:.2f}C {_short_expiry(expiry)}{entry_str}"
        if stype in ("bull_put", "bear_call"):
            short = structure.get("short_strike")
            long_ = structure.get("long_strike")
            expiry = structure.get("expiry")
            credit = structure.get("credit")
            label = "BULL PUT" if stype == "bull_put" else "BEAR CALL"
            if short is None or long_ is None or expiry is None:
                return f"{engine} [partial data]"
            credit_str = f" (credit ${credit:.2f})" if isinstance(credit, (int, float)) else ""
            return f"{short:.0f}/{long_:.0f} {label} {_short_expiry(expiry)}{credit_str}"
        if engine == "v2_5d":
            grade = (classification or "").replace("GRADE_", "").strip()
            bias = (direction or "").strip()
            if grade and bias:
                return f"Grade {grade} {bias}"
            if grade:
                return f"Grade {grade}"
            if bias:
                return f"v2_5d {bias}"
            return "v2_5d evaluation"
        if engine == "oi_flow_conviction":
            strike = structure.get("strike")
            kind = structure.get("right") or structure.get("type") or "C"
            kind_letter = "C" if "call" in str(kind).lower() or kind == "C" else "P"
            expiry = structure.get("expiry")
            if strike is None or expiry is None:
                return "conviction [partial data]"
            return f"${strike:.2f}{kind_letter} {_short_expiry(expiry)} (flow conviction)"
        return f"{engine} [unknown type]"
    except Exception as e:
        log.warning(f"alerts_data: format_structure_summary({engine}) failed: {e}")
        return f"{engine} [parse error]"


def _format_card(row: sqlite3.Row, now_micros: int) -> Dict[str, Any]:
    """Build the per-card dict consumed by _alert_card.html."""
    engine = row["engine"]
    display = ENGINE_DISPLAY.get(engine, {
        "icon": "•", "label": engine, "stripe": "unknown",
    })
    try:
        struct = json.loads(row["suggested_structure"]) if row["suggested_structure"] else {}
    except Exception:
        struct = {}
    summary = format_structure_summary(
        engine, struct,
        classification=row["classification"],
        direction=row["direction"],
    )
    fired_dt = datetime.fromtimestamp(row["fired_at"] / 1_000_000, tz=timezone.utc)
    fired_ct = fired_dt.astimezone(CHICAGO_TZ)
    elapsed_seconds = (now_micros - row["fired_at"]) // 1_000_000
    return {
        "alert_id": row["alert_id"],
        "engine": engine,
        "engine_icon": display["icon"],
        "engine_label": display["label"],
        "stripe": display["stripe"],
        "ticker": row["ticker"],
        "direction": row["direction"],
        "classification": row["classification"],
        "structure_summary": summary,
        "fired_at_ct": fired_ct.strftime("%H:%M:%S CT"),
        "fired_at_relative": _humanize_elapsed(int(elapsed_seconds)),
        "elapsed_seconds": int(elapsed_seconds),
        "is_recent": elapsed_seconds < 5 * 60,    # CSS pulse when True
        "is_old": elapsed_seconds > 24 * 60 * 60, # CSS muted when True
        "parent_alert_id": row["parent_alert_id"],
    }


def _status_strip(buckets: Dict[str, List[Dict[str, Any]]],
                  newest_fired_at_micros: Optional[int]) -> Dict[str, Any]:
    """Build the page-top status strip dict.

    Reads env vars at call time. No DB access. The hover tooltip
    surface lists which engines and daemons are on, derived from
    the canonical RECORDER_*_ENABLED slugs.
    """
    master_on = _flag_on("RECORDER_ENABLED")
    engines_on = [slug for slug, var in ENGINE_FLAG_VARS.items() if _flag_on(var)]
    daemons_on = [name for name, var in DAEMON_FLAG_VARS.items() if _flag_on(var)]
    last_fire_ct = None
    if newest_fired_at_micros:
        dt = datetime.fromtimestamp(newest_fired_at_micros / 1_000_000, tz=timezone.utc)
        last_fire_ct = dt.astimezone(CHICAGO_TZ).strftime("%H:%M")
    return {
        "master_on": master_on,
        "engines_on": engines_on,
        "daemons_on": daemons_on,
        "engines_on_count": len(engines_on),
        "count_today": len(buckets.get("today", [])),
        "last_fire_ct": last_fire_ct,
    }


def list_alerts(limit: int = LIST_LIMIT,
                now_micros: Optional[int] = None) -> Dict[str, Any]:
    """Return bucketed feed payload. ONE SQL query against alerts table.
    Does NOT fetch features/outcomes/track per row (no N+1).

    Empty-state matrix:
      1. DB file missing      -> available=False, helpful error
      2. DB exists, zero rows -> available=True, all buckets empty
      3. Rows but all >7d old -> available=True, only 'earlier' populated
      4. Normal               -> available=True, populated buckets
    """
    now = now_micros if now_micros is not None else _now_micros()
    payload: Dict[str, Any] = {
        "available": False,
        "error": None,
        "today": [], "yesterday": [], "this_week": [], "earlier": [],
        "total_count": 0,
        "status": _status_strip({}, None),
    }
    try:
        conn = _open_ro()
    except sqlite3.OperationalError as e:
        payload["error"] = (
            f"Recorder DB not found at {_db_path()}. "
            f"Check RECORDER_ENABLED is set in production env."
        )
        log.info(f"alerts_data: DB open failed: {e}")
        return payload
    try:
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "classification, direction, suggested_structure, "
                "suggested_dte, parent_alert_id "
                "FROM alerts ORDER BY fired_at DESC LIMIT ?",
                (int(limit),)
            ).fetchall()
        except sqlite3.DatabaseError as e:
            payload["error"] = f"Recorder DB unreadable: {e}"
            log.warning(f"alerts_data: list_alerts query failed: {e}")
            return payload
    finally:
        try:
            conn.close()
        except Exception:
            pass
    payload["available"] = True
    newest = rows[0]["fired_at"] if rows else None
    for row in rows:
        try:
            card = _format_card(row, now)
            bucket = _bucket(row["fired_at"], now)
            payload[bucket].append(card)
        except Exception as e:
            log.warning(f"alerts_data: card build failed for {row['alert_id']}: {e}")
            continue
    payload["total_count"] = (
        len(payload["today"]) + len(payload["yesterday"])
        + len(payload["this_week"]) + len(payload["earlier"])
    )
    payload["status"] = _status_strip(payload, newest)
    return payload


def get_alert_detail(alert_id: str,
                     now_micros: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return full detail dict for one alert, or None if not found.

    UUID v4 shape is enforced: anything that doesn't match _UUID_V4
    returns None without ever hitting the DB. SQL parameter binding
    is the second line of defense.

    Performs at most 6 single-table queries on success. Never raises —
    returns None on any failure path (caller renders 404).
    """
    if not isinstance(alert_id, str) or not _UUID_V4.match(alert_id):
        log.info(f"alerts_data: rejected non-UUID alert_id={alert_id!r}")
        return None
    now = now_micros if now_micros is not None else _now_micros()
    try:
        conn = _open_ro()
    except sqlite3.OperationalError as e:
        log.info(f"alerts_data: DB open failed in detail: {e}")
        return None
    try:
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT alert_id, fired_at, engine, engine_version, ticker, "
                "classification, direction, suggested_structure, "
                "suggested_dte, spot_at_fire, canonical_snapshot, "
                "raw_engine_payload, parent_alert_id, posted_to_telegram, "
                "telegram_chat, suppression_reason "
                "FROM alerts WHERE alert_id = ?",
                (alert_id,)
            ).fetchone()
            if row is None:
                return None
            features = conn.execute(
                "SELECT feature_name, feature_value, feature_text "
                "FROM alert_features WHERE alert_id = ? "
                "ORDER BY feature_name",
                (alert_id,)
            ).fetchall()
            track = conn.execute(
                "SELECT elapsed_seconds, sampled_at, underlying_price, "
                "structure_mark, structure_pnl_pct, structure_pnl_abs, market_state "
                "FROM alert_price_track WHERE alert_id = ? "
                "ORDER BY elapsed_seconds",
                (alert_id,)
            ).fetchall()
            outcomes = conn.execute(
                "SELECT horizon, outcome_at, underlying_price, structure_mark, "
                "pnl_pct, pnl_abs, hit_pt1, hit_pt2, hit_pt3, "
                "max_favorable_pct, max_adverse_pct "
                "FROM alert_outcomes WHERE alert_id = ?",
                (alert_id,)
            ).fetchall()
            parent = None
            if row["parent_alert_id"]:
                parent = conn.execute(
                    "SELECT alert_id, engine, classification, direction "
                    "FROM alerts WHERE alert_id = ?",
                    (row["parent_alert_id"],)
                ).fetchone()
            children = conn.execute(
                "SELECT alert_id, engine, classification "
                "FROM alerts WHERE parent_alert_id = ? ORDER BY fired_at",
                (alert_id,)
            ).fetchall()
        except sqlite3.DatabaseError as e:
            log.warning(f"alerts_data: detail query failed for {alert_id}: {e}")
            return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return _build_detail_dict(row, features, track, outcomes, parent, children, now)


def _build_detail_dict(row, features, track, outcomes, parent, children,
                       now_micros: int) -> Dict[str, Any]:
    """Pure formatting — no DB access. Builds the dict the detail
    template renders."""
    engine = row["engine"]
    display = ENGINE_DISPLAY.get(engine, {
        "icon": "•", "label": engine, "stripe": "unknown",
    })
    try:
        structure = json.loads(row["suggested_structure"]) if row["suggested_structure"] else {}
    except Exception:
        structure = {}
    try:
        snapshot = json.loads(row["canonical_snapshot"]) if row["canonical_snapshot"] else {}
    except Exception:
        snapshot = {}
    try:
        raw_payload = json.loads(row["raw_engine_payload"]) if row["raw_engine_payload"] else {}
    except Exception:
        raw_payload = {}
    fired_dt = datetime.fromtimestamp(row["fired_at"] / 1_000_000, tz=timezone.utc)
    fired_ct = fired_dt.astimezone(CHICAGO_TZ)
    dte_convention = DTE_CONVENTION.get(engine)
    dte_label = None
    if row["suggested_dte"] is not None and dte_convention:
        unit = "trading day" if dte_convention == "trading" else "calendar day"
        n = int(row["suggested_dte"])
        dte_label = f"{n} {unit}{'s' if n != 1 else ''}"
    feature_rows = [
        {"name": f["feature_name"],
         "value": (f["feature_value"] if f["feature_value"] is not None
                   else f["feature_text"])}
        for f in features
    ]
    track_rows = [
        {"elapsed_seconds": t["elapsed_seconds"],
         "structure_mark": t["structure_mark"],
         "structure_pnl_pct": t["structure_pnl_pct"],
         "structure_pnl_abs": t["structure_pnl_abs"],
         "market_state": t["market_state"]}
        for t in track
    ]
    outcome_rows = [
        {"horizon": o["horizon"],
         "pnl_pct": o["pnl_pct"], "pnl_abs": o["pnl_abs"],
         "hit_pt1": bool(o["hit_pt1"]), "hit_pt2": bool(o["hit_pt2"]),
         "hit_pt3": bool(o["hit_pt3"]),
         "max_favorable_pct": o["max_favorable_pct"],
         "max_adverse_pct": o["max_adverse_pct"]}
        for o in outcomes
    ]
    parent_dict = None
    if parent is not None:
        parent_engine = parent["engine"]
        parent_display = ENGINE_DISPLAY.get(parent_engine, {})
        parent_dict = {
            "alert_id": parent["alert_id"],
            "engine_label": parent_display.get("label", parent_engine),
            "classification": parent["classification"],
            "direction": parent["direction"],
        }
    children_list = []
    for c in children:
        c_engine = c["engine"]
        c_display = ENGINE_DISPLAY.get(c_engine, {})
        children_list.append({
            "alert_id": c["alert_id"],
            "engine_label": c_display.get("label", c_engine),
            "classification": c["classification"],
        })
    summary = format_structure_summary(
        engine, structure,
        classification=row["classification"],
        direction=row["direction"],
    )
    # Chart stats — derived from the track list, no extra DB hit.
    pnl_values = [t["structure_pnl_pct"] for t in track_rows
                  if t.get("structure_pnl_pct") is not None]
    chart_stats = None
    if pnl_values:
        chart_stats = {
            "current_pct": pnl_values[-1],
            "mfe_pct": max(pnl_values),
            "mae_pct": min(pnl_values),
            "samples": len(pnl_values),
        }
    return {
        "alert_id": row["alert_id"],
        "alert_id_short": row["alert_id"][:8],
        "engine": engine,
        "engine_icon": display["icon"],
        "engine_label": display["label"],
        "stripe": display["stripe"],
        "engine_version": row["engine_version"],
        "ticker": row["ticker"],
        "classification": row["classification"],
        "direction": row["direction"],
        "structure": structure,
        "structure_summary": summary,
        "spot_at_fire": row["spot_at_fire"],
        "fired_at_ct": fired_ct.strftime("%Y-%m-%d %H:%M:%S CT"),
        "fired_at_relative": _humanize_elapsed(
            int((now_micros - row["fired_at"]) // 1_000_000)),
        "dte_value": row["suggested_dte"],
        "dte_label": dte_label,
        "parent": parent_dict,
        "children": children_list,
        "features": feature_rows,
        "track": track_rows,
        "outcomes": outcome_rows,
        "pnl_svg": _build_pnl_svg(track_rows),
        "chart_stats": chart_stats,
        "canonical_snapshot": snapshot,
        "raw_engine_payload": raw_payload,
        "telegram_chat": row["telegram_chat"],
        "posted_to_telegram": bool(row["posted_to_telegram"]),
    }


def _build_pnl_svg(track_rows: List[Dict[str, Any]]) -> str:
    """Server-rendered SVG line chart of structure_pnl_pct over elapsed_seconds.

    Returns "" if no track rows. Defensive: skips rows where pnl_pct
    or elapsed_seconds is None. No client-side charting library needed.
    """
    pts = [(r["elapsed_seconds"], r["structure_pnl_pct"])
           for r in track_rows
           if r.get("structure_pnl_pct") is not None
           and r.get("elapsed_seconds") is not None]
    if not pts:
        return ""
    width, height, pad = 720, 180, 18
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x_min, x_max = min(xs), max(xs)
    if x_max == x_min:
        x_max = x_min + 1
    y_min, y_max = min(ys), max(ys)
    if y_max == y_min:
        y_max = y_min + 1

    def _x(v: float) -> float:
        return pad + (v - x_min) * (width - 2 * pad) / (x_max - x_min)

    def _y(v: float) -> float:
        return height - pad - (v - y_min) * (height - 2 * pad) / (y_max - y_min)

    poly = " ".join(f"{_x(x):.1f},{_y(y):.1f}" for x, y in pts)
    zero_line = ""
    if y_min <= 0 <= y_max:
        zy = _y(0)
        zero_line = (f'<line x1="{pad}" y1="{zy:.1f}" x2="{width - pad}" '
                     f'y2="{zy:.1f}" stroke="#6D5A36" stroke-dasharray="3 3" />')
    # MFE marker (max y).
    mfe_idx = ys.index(max(ys))
    mfe_x, mfe_y = _x(xs[mfe_idx]), _y(ys[mfe_idx])
    # Current marker (last point).
    cur_x, cur_y = _x(xs[-1]), _y(ys[-1])
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" class="alerts-pnl-chart" '
        f'preserveAspectRatio="none">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#171A1D" />'
        f'{zero_line}'
        f'<polyline fill="none" stroke="#D5B06B" stroke-width="1.5" '
        f'points="{poly}" />'
        f'<circle cx="{mfe_x:.1f}" cy="{mfe_y:.1f}" r="3" fill="#D5B06B" />'
        f'<circle cx="{cur_x:.1f}" cy="{cur_y:.1f}" r="3" fill="#73B27B" />'
        f'</svg>'
    )
