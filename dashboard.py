# dashboard.py
# ═══════════════════════════════════════════════════════════════════
# OMEGA 3000 — Live Dashboard
#
# Reads from the bot's existing Redis stores + regime detectors and
# writes a per-ticker state view to a dedicated Google Sheet every 60s.
# Also appends signal events to a Signal Log tab for retrospective
# outcome tagging.
#
# PURE READ-ONLY from the bot's perspective. This module never writes
# to Redis, never touches the trading pipeline, never posts to Telegram.
# The only external effect is Google Sheets writes to the dashboard Sheet.
#
# WHY IT EXISTS:
#   The bot has ~20 analytical layers (Potter Box, GEX, Flow Conviction,
#   Active Scanner, EM Model, Thesis Monitor, etc.) that each emit
#   Telegram alerts independently. The trader can't see "all signals
#   on MSTR right now" in one place, so confluence is done mentally and
#   can't be measured. This dashboard gives the trader one row per ticker
#   with every live signal visible, and logs every signal event for
#   later confluence-vs-outcome analysis.
#
# ENV VARS:
#   DASHBOARD_ENABLED      "1" to enable the writer thread (default: off)
#   DASHBOARD_SHEET_ID     Google Sheet ID to write to (required)
#   DASHBOARD_INTERVAL_SEC Update cadence in seconds (default: 60)
#
# The service account that writes to the main bot Sheet must have
# Editor access on the dashboard Sheet. Tabs are auto-created on
# first run.
# ═══════════════════════════════════════════════════════════════════

import os
import time
import json
import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Callable

import requests

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "0") == "1"
DASHBOARD_SHEET_ID = os.getenv("DASHBOARD_SHEET_ID", "").strip()
try:
    DASHBOARD_INTERVAL_SEC = max(30, int(os.getenv("DASHBOARD_INTERVAL_SEC", "60")))
except (TypeError, ValueError):
    DASHBOARD_INTERVAL_SEC = 60

# Tab names in the Sheet. Do not change these without updating the
# whole writer; they're used as keys in _sheet_tab_ids cache.
TAB_DASHBOARD = "Dashboard"
TAB_SIGNAL_LOG = "Signal Log"
TAB_POSITION_PNL = "Position PnL"   # v8.2: per-position PnL tracking (Option A)

# Header rows for each tab. Order here = column order in the Sheet.
DASHBOARD_HEADERS = [
    "Ticker",
    "Spot",
    "%Day",
    "PB Floor",
    "PB Roof",
    "PB Location",          # above / in / below / none
    "OI Time",              # HH:MM CT, today only
    "OI Side",              # call / put / none
    "OI Direction",         # buildup / unwind / none
    "Flow Time",            # HH:MM CT, today only
    "Flow Direction",       # bullish / bearish / none
    "Flow Notional",        # $ amount
    "AS Signal",            # T1/T2/T3 + 🐂/🐻 or blank
    "Thesis Bias",          # BULLISH / BEARISH / NEUTRAL
    "Thesis Score",         # -14 to +14
    "GEX Sign",             # positive / negative / unknown
    "Gamma Flip",           # price level or blank
    "Open Campaigns",       # count from recommendation_tracker
    "Best MFE",             # best MFE % among open campaigns
    # v8.2 additions (Option B — per-ticker PnL rollups across positions):
    "Best Peak PnL%",       # max lifetime peak PnL% across this ticker's positions
    "Best 2:45 PnL%",       # max close-at-2:45 PnL% across this ticker's positions
    "Best Hold Peak%",      # max peak-during-hold PnL% across this ticker's positions
    "Signals Bullish",      # count of bullish signals this row
    "Signals Bearish",      # count of bearish signals this row
    "Net Direction",        # bullish / bearish / mixed / none
    "Updated",              # HH:MM:SS CT
]

SIGNAL_LOG_HEADERS = [
    "Date",                 # YYYY-MM-DD
    "Time CT",              # HH:MM:SS
    "Ticker",
    "Signal Type",          # potter_box_breakout / oi_confirm / flow_conviction / active_scanner / thesis / position_close
    "Direction",            # bullish / bearish / neutral
    "Detail",               # free-form (strike, notional, score, PnL summary on close, etc.)
    "Market Regime",        # BULL_BASE / BEAR_CRISIS / TRANSITION / NORMAL
    "Vol Regime",           # NORMAL / TRANSITION / EMERGENCY
    "Dealer Regime",        # per-ticker if available
    "VIX",                  # raw VIX level
    "Outcome",              # (manual tag: win / loss / scratch / skip)
    "Notes",                # (manual: free-form)
]

# v8.2: Position PnL tab — one row per tracked option position. Append-on-new,
# update-in-place while active, final values locked on close/grade.
POSITION_PNL_HEADERS = [
    "Opened CT",                    # YYYY-MM-DD HH:MM CT
    "Ticker",
    "Campaign ID",                  # tracker's id; primary key for update-in-place
    "Structure",                    # long_call / long_put / bull_call_spread / etc.
    "Legs",                         # compact string: "+165C 250516 / -170C 250516"
    "Side",                         # bull / bear (direction)
    "Strike",                       # primary (first) leg strike
    "Entry Premium",                # entry_option_mark
    "Peak Premium",                 # peak_option_mark (live while active)
    "Peak PnL% Lifetime",           # (peak - entry) / entry * 100
    "2:45 PnL%",                    # snapshot at 2:45 PM CT on expiry day (or last hold day)
    "Peak PnL% During Hold",        # peak PnL while user was actively holding
    "Closed CT",                    # YYYY-MM-DD HH:MM CT, blank while active
    "Close Premium",                # exit_option_mark, blank while active
    "Current PnL%",                 # live if active, final if closed
    "Status",                       # open / closed
]


# ═══════════════════════════════════════════════════════════════════
# MODULE-LEVEL STATE (wired by init_dashboard)
# ═══════════════════════════════════════════════════════════════════

# Callables supplied by app.py at boot:
_persistent_state = None         # PersistentState instance
_rec_tracker = None              # RecommendationStore instance
_get_spot_fn = None              # (ticker) -> float or None
_get_token_fn = None             # () -> Bearer token str or None
_get_flow_tickers_fn = None      # () -> list[str]
_get_market_regime_pkg_fn = None # () -> regime package dict
_get_vix_ts_fn = None            # () -> dict (vix term structure)

# Internal state:
_thread_started = False
_dashboard_lock = threading.Lock()
_signal_log_seen_keys = set()  # dedup: skip re-logging the same event

# Tab ID cache so we don't re-resolve sheetId on every write
_sheet_tab_ids: Dict[str, int] = {}


# ═══════════════════════════════════════════════════════════════════
# INITIALIZATION
# ═══════════════════════════════════════════════════════════════════

def init_dashboard(
    persistent_state,
    rec_tracker,
    get_spot_fn: Callable[[str], Optional[float]],
    get_token_fn: Callable[[], Optional[str]],
    get_flow_tickers_fn: Callable[[], List[str]],
    get_market_regime_pkg_fn: Optional[Callable[[], Dict]] = None,
    get_vix_ts_fn: Optional[Callable[[], Dict]] = None,
) -> bool:
    """Wire dashboard dependencies. Call once from app.py at boot.

    Returns True if dashboard is ready to start, False if disabled or misconfigured.
    """
    global _persistent_state, _rec_tracker, _get_spot_fn, _get_token_fn
    global _get_flow_tickers_fn, _get_market_regime_pkg_fn, _get_vix_ts_fn

    if not DASHBOARD_ENABLED:
        log.info("dashboard: DASHBOARD_ENABLED not set, skipping init")
        return False

    if not DASHBOARD_SHEET_ID:
        log.warning("dashboard: DASHBOARD_ENABLED=1 but DASHBOARD_SHEET_ID not set — skipping init")
        return False

    _persistent_state = persistent_state
    _rec_tracker = rec_tracker
    _get_spot_fn = get_spot_fn
    _get_token_fn = get_token_fn
    _get_flow_tickers_fn = get_flow_tickers_fn
    _get_market_regime_pkg_fn = get_market_regime_pkg_fn
    _get_vix_ts_fn = get_vix_ts_fn

    log.info(
        f"dashboard: initialized (sheet={DASHBOARD_SHEET_ID[:12]}..., "
        f"interval={DASHBOARD_INTERVAL_SEC}s)"
    )
    return True


def start_dashboard_thread() -> bool:
    """Launch the background writer thread. Idempotent.

    Returns True if the thread was started, False if already running or disabled.
    """
    global _thread_started
    if _thread_started:
        log.debug("dashboard: thread already started")
        return False
    if not DASHBOARD_ENABLED or not DASHBOARD_SHEET_ID:
        return False
    if _persistent_state is None or _rec_tracker is None:
        log.warning("dashboard: cannot start thread — init_dashboard() not called or failed")
        return False

    t = threading.Thread(target=_dashboard_loop, daemon=True, name="dashboard")
    t.start()
    _thread_started = True
    log.info(f"dashboard: writer thread started ({DASHBOARD_INTERVAL_SEC}s cadence)")
    return True


# ═══════════════════════════════════════════════════════════════════
# GOOGLE SHEETS LOW-LEVEL
# ═══════════════════════════════════════════════════════════════════

def _sheets_url(path: str) -> str:
    return f"https://sheets.googleapis.com/v4/spreadsheets/{DASHBOARD_SHEET_ID}{path}"


def _get_sheet_metadata(token: str) -> Optional[Dict]:
    """Fetch the spreadsheet metadata (for sheetId lookup on tab create)."""
    try:
        resp = requests.get(
            _sheets_url(""),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"dashboard: sheet metadata fetch failed: {e}")
        return None


def _refresh_tab_id_cache(token: str) -> None:
    """Populate _sheet_tab_ids from the Sheet metadata."""
    meta = _get_sheet_metadata(token)
    if not meta:
        return
    _sheet_tab_ids.clear()
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        title = props.get("title")
        sid = props.get("sheetId")
        if title and sid is not None:
            _sheet_tab_ids[title] = sid
    log.debug(f"dashboard: tab id cache refreshed: {list(_sheet_tab_ids.keys())}")


def _ensure_tab_exists(tab_name: str, headers: List[str], token: str) -> bool:
    """Create the tab if missing, write the header row if empty.
    Returns True if tab is ready for writes."""
    if tab_name in _sheet_tab_ids:
        return True

    _refresh_tab_id_cache(token)
    if tab_name in _sheet_tab_ids:
        return True

    # Tab doesn't exist — create it
    try:
        body = {
            "requests": [{
                "addSheet": {
                    "properties": {
                        "title": tab_name,
                        "gridProperties": {
                            "rowCount": 1000,
                            "columnCount": max(26, len(headers) + 2),
                            "frozenRowCount": 1,
                        },
                    }
                }
            }]
        }
        resp = requests.post(
            _sheets_url(":batchUpdate"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        if resp.status_code == 200:
            log.info(f"dashboard: created tab '{tab_name}'")
            # Cache the new sheetId
            try:
                reply = resp.json().get("replies", [{}])[0]
                new_id = reply.get("addSheet", {}).get("properties", {}).get("sheetId")
                if new_id is not None:
                    _sheet_tab_ids[tab_name] = new_id
            except Exception:
                _refresh_tab_id_cache(token)
        else:
            log.warning(f"dashboard: tab create failed for '{tab_name}': "
                        f"{resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        log.warning(f"dashboard: tab create exception for '{tab_name}': {e}")
        return False

    # Write header row
    return _write_tab_headers(tab_name, headers, token)


def _write_tab_headers(tab_name: str, headers: List[str], token: str) -> bool:
    try:
        rng = requests.utils.quote(f"{tab_name}!1:1", safe="!:")
        resp = requests.put(
            _sheets_url(f"/values/{rng}"),
            params={"valueInputOption": "USER_ENTERED"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": [headers]},
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"dashboard: wrote headers to '{tab_name}' ({len(headers)} cols)")
        return True
    except Exception as e:
        log.warning(f"dashboard: header write failed for '{tab_name}': {e}")
        return False


def _write_dashboard_values(rows: List[List[Any]], token: str) -> bool:
    """Overwrite the Dashboard tab data area (rows 2+) with fresh rows."""
    if not rows:
        return True
    try:
        # Clear existing data rows first so stale tickers don't linger
        clear_rng = requests.utils.quote(f"{TAB_DASHBOARD}!A2:Z1000", safe="!:")
        requests.post(
            _sheets_url(f"/values/{clear_rng}:clear"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        # Write the new batch
        write_rng = requests.utils.quote(f"{TAB_DASHBOARD}!A2", safe="!:")
        resp = requests.put(
            _sheets_url(f"/values/{write_rng}"),
            params={"valueInputOption": "USER_ENTERED"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": rows, "majorDimension": "ROWS"},
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"dashboard: data write failed: {e}")
        return False


def _write_position_pnl_values(rows: List[List[Any]], token: str) -> bool:
    """Overwrite the Position PnL tab data area (rows 2+) with fresh rows.

    v8.2: positions can transition from active -> closed between ticks
    (peak/current/status changes), so we do a full overwrite each tick
    rather than append. Cleared range is wider (Z column) because the tab
    has 16 columns and we want headroom.
    """
    if not rows:
        # Still clear residual rows (e.g. all positions dropped)
        try:
            clear_rng = requests.utils.quote(f"{TAB_POSITION_PNL}!A2:Z5000", safe="!:")
            requests.post(
                _sheets_url(f"/values/{clear_rng}:clear"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except Exception:
            pass
        return True
    try:
        clear_rng = requests.utils.quote(f"{TAB_POSITION_PNL}!A2:Z5000", safe="!:")
        requests.post(
            _sheets_url(f"/values/{clear_rng}:clear"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        write_rng = requests.utils.quote(f"{TAB_POSITION_PNL}!A2", safe="!:")
        resp = requests.put(
            _sheets_url(f"/values/{write_rng}"),
            params={"valueInputOption": "USER_ENTERED"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": rows, "majorDimension": "ROWS"},
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"dashboard: position pnl write failed: {e}")
        return False


def _append_signal_log_rows(rows: List[List[Any]], token: str) -> bool:
    if not rows:
        return True
    try:
        rng = requests.utils.quote(f"{TAB_SIGNAL_LOG}!A:L", safe="!:")
        resp = requests.post(
            _sheets_url(f"/values/{rng}:append"),
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": rows, "majorDimension": "ROWS"},
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"dashboard: signal log append failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
# AGGREGATOR — read every store, build one row per ticker
# ═══════════════════════════════════════════════════════════════════

def _ct_now() -> datetime:
    return datetime.now(ZoneInfo("America/Chicago"))


def _today_ct_str() -> str:
    return _ct_now().strftime("%Y-%m-%d")


def _fmt_time_ct(iso_or_ts) -> str:
    """Convert an ISO string or epoch timestamp to HH:MM CT. Returns '' on failure."""
    if not iso_or_ts:
        return ""
    try:
        if isinstance(iso_or_ts, (int, float)):
            dt = datetime.fromtimestamp(iso_or_ts, tz=timezone.utc)
        else:
            s = str(iso_or_ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M")
    except Exception:
        return ""


def _is_today_ct(iso_or_ts) -> bool:
    if not iso_or_ts:
        return False
    try:
        if isinstance(iso_or_ts, (int, float)):
            dt = datetime.fromtimestamp(iso_or_ts, tz=timezone.utc)
        else:
            s = str(iso_or_ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d") == _today_ct_str()
    except Exception:
        return False


def _safe_get(obj, key, default=None):
    """Dict-like safe get that also tolerates None objects."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_potter_box_snapshot(ticker: str) -> Dict[str, Any]:
    """Read Potter Box state for a ticker. Returns dict with floor/roof/location."""
    try:
        pb = _persistent_state._json_get(f"potter_box:active:{ticker}")
    except Exception as e:
        log.debug(f"dashboard: potter_box read failed for {ticker}: {e}")
        return {"floor": None, "roof": None, "location": "none"}

    if not pb:
        return {"floor": None, "roof": None, "location": "none"}

    box = pb.get("box") or pb
    floor = box.get("floor") or box.get("box_low")
    roof = box.get("roof") or box.get("box_high")

    spot = None
    try:
        spot = _get_spot_fn(ticker) if _get_spot_fn else None
    except Exception:
        spot = None

    location = "none"
    if floor and roof and spot:
        try:
            f = float(floor)
            r = float(roof)
            s = float(spot)
            if s > r * 1.005:
                location = "above"
            elif s < f * 0.995:
                location = "below"
            else:
                location = "in"
        except (TypeError, ValueError):
            location = "none"

    return {"floor": floor, "roof": roof, "location": location}


def _get_oi_snapshot(ticker: str) -> Dict[str, Any]:
    """Read today's OI confirmation flags for a ticker."""
    try:
        flags = _persistent_state.get_volume_flags(_today_ct_str()) or []
    except Exception as e:
        log.debug(f"dashboard: oi flags read failed: {e}")
        return {"time": "", "side": "none", "direction": "none"}

    # Filter to this ticker's flags, most recent first
    t_flags = [f for f in flags if (f.get("ticker") or "").upper() == ticker.upper()]
    if not t_flags:
        return {"time": "", "side": "none", "direction": "none"}

    # Most recent by timestamp (fall back to last-in-list)
    latest = None
    latest_ts = None
    for f in t_flags:
        ts = f.get("timestamp") or f.get("time") or f.get("ts")
        if ts and (latest_ts is None or ts > latest_ts):
            latest = f
            latest_ts = ts
    if latest is None:
        latest = t_flags[-1]

    # Direction inference — the flag type tells us buildup vs unwind
    flow_type = (latest.get("flow_type") or latest.get("type") or "").lower()
    if "buildup" in flow_type or "confirmed_buildup" in flow_type:
        direction = "buildup"
    elif "unwind" in flow_type or "confirmed_unwinding" in flow_type:
        direction = "unwind"
    else:
        direction = flow_type or "none"

    side = (latest.get("side") or "none").lower()

    return {
        "time": _fmt_time_ct(latest.get("timestamp") or latest.get("time")),
        "side": side,
        "direction": direction,
    }


def _get_flow_snapshot(ticker: str) -> Dict[str, Any]:
    """Read latest flow direction for a ticker."""
    try:
        fd = _persistent_state.get_flow_direction(ticker) or {}
    except Exception as e:
        log.debug(f"dashboard: flow direction read failed for {ticker}: {e}")
        return {"time": "", "direction": "none", "notional": 0}

    if not fd:
        return {"time": "", "direction": "none", "notional": 0}

    # Only surface if it's today
    ts = fd.get("time") or fd.get("timestamp") or fd.get("ts")
    if not _is_today_ct(ts):
        return {"time": "", "direction": "none", "notional": 0}

    direction = (fd.get("direction") or "").lower()
    if direction not in ("bullish", "bearish"):
        direction = "none"

    notional = fd.get("notional") or 0
    try:
        notional = int(float(notional))
    except (TypeError, ValueError):
        notional = 0

    return {
        "time": _fmt_time_ct(ts),
        "direction": direction,
        "notional": notional,
    }


def _get_active_scanner_snapshot(ticker: str) -> str:
    """Read latest active scanner signal for a ticker from trade_journal.
    Returns a short label like 'T2 🐂' or '' if none today."""
    try:
        import trade_journal as _tj
        entries = _tj.query_journal(
            ticker=ticker.upper(),
            entry_type="signal",
            date_from=_today_ct_str(),
            limit=5,
        )
    except Exception as e:
        log.debug(f"dashboard: trade_journal query failed for {ticker}: {e}")
        return ""

    if not entries:
        return ""

    # Most recent entry is first (query_journal returns most-recent-first)
    latest = entries[0]
    tier = latest.get("tier") or ""
    side = (latest.get("side") or "").lower()
    arrow = "🐂" if side in ("bull", "long", "buy") else ("🐻" if side in ("bear", "short", "sell") else "")
    if tier:
        return f"T{tier} {arrow}".strip()
    return arrow or ""


def _get_thesis_snapshot(ticker: str) -> Dict[str, Any]:
    """Read thesis for a ticker."""
    try:
        t = _persistent_state.get_thesis(ticker)
    except Exception as e:
        log.debug(f"dashboard: thesis read failed for {ticker}: {e}")
        return {"bias": "NEUTRAL", "score": 0}

    if not t:
        return {"bias": "NEUTRAL", "score": 0}

    bias = _safe_get(t, "bias", "NEUTRAL") or "NEUTRAL"
    score = _safe_get(t, "bias_score", 0) or 0
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 0
    return {"bias": str(bias).upper(), "score": score}


def _get_gex_snapshot(ticker: str) -> Dict[str, Any]:
    """Read GEX sign and gamma flip level."""
    sign = ""
    flip = None
    try:
        sign = (_persistent_state.get_gex_sign(ticker) or "").lower()
    except Exception as e:
        log.debug(f"dashboard: gex sign read failed for {ticker}: {e}")
    try:
        flip = _persistent_state.get_gamma_flip_level(ticker)
        if flip is not None:
            flip = float(flip)
            if flip <= 0:
                flip = None
    except Exception:
        flip = None
    return {"sign": sign or "unknown", "flip": flip}


def _get_open_campaigns_snapshot(ticker: str) -> Dict[str, Any]:
    """Count open tracker campaigns for this ticker and find best MFE."""
    if _rec_tracker is None:
        return {"count": 0, "best_mfe": 0.0}
    try:
        active = _rec_tracker.list_all_active() or []
    except Exception as e:
        log.debug(f"dashboard: list_all_active failed: {e}")
        return {"count": 0, "best_mfe": 0.0}

    t_up = ticker.upper()
    matching = [r for r in active if (r.get("ticker") or "").upper() == t_up]
    if not matching:
        return {"count": 0, "best_mfe": 0.0}
    best_mfe = 0.0
    for r in matching:
        mfe = r.get("mfe_pct", 0) or 0
        try:
            mfe = float(mfe)
        except (TypeError, ValueError):
            mfe = 0.0
        if mfe > best_mfe:
            best_mfe = mfe
    return {"count": len(matching), "best_mfe": best_mfe}


def _count_signal_directions(
    flow_dir: str,
    oi_side: str,
    oi_dir: str,
    thesis_bias: str,
    pb_location: str,
    as_signal: str,
) -> Dict[str, int]:
    """Count bullish vs bearish votes from the primary directional signals.

    Rules:
      - flow_dir: bullish or bearish directly
      - OI: buildup on calls = bullish, on puts = bearish;
            unwinding calls = bearish, unwinding puts = bullish
      - thesis: "BULLISH" / "BEARISH" in the bias string
      - Potter Box: "above" = bullish breakout signal, "below" = bearish
      - Active scanner: 🐂 bullish, 🐻 bearish
    """
    bull = 0
    bear = 0

    if flow_dir == "bullish":
        bull += 1
    elif flow_dir == "bearish":
        bear += 1

    if oi_dir == "buildup":
        if oi_side == "call":
            bull += 1
        elif oi_side == "put":
            bear += 1
    elif oi_dir == "unwind":
        if oi_side == "call":
            bear += 1
        elif oi_side == "put":
            bull += 1

    tb = (thesis_bias or "").upper()
    if "BULLISH" in tb:
        bull += 1
    elif "BEARISH" in tb:
        bear += 1

    if pb_location == "above":
        bull += 1
    elif pb_location == "below":
        bear += 1

    if "🐂" in (as_signal or ""):
        bull += 1
    elif "🐻" in (as_signal or ""):
        bear += 1

    return {"bullish": bull, "bearish": bear}


def get_ticker_snapshot(ticker: str, pnl_rollup: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Build a complete dashboard row for one ticker. Never raises.

    v8.2: accepts an optional pnl_rollup dict from _get_position_pnl_rollup_map
    so Option B columns (Best Peak PnL%, Best 2:45 PnL%, Best Hold Peak%)
    can be populated per ticker without re-querying the tracker for every
    ticker in the flow list.
    """
    ticker = (ticker or "").upper()
    snap = {"ticker": ticker}

    # Spot + % day
    try:
        spot = _get_spot_fn(ticker) if _get_spot_fn else None
    except Exception as e:
        log.debug(f"dashboard: spot fetch failed for {ticker}: {e}")
        spot = None
    snap["spot"] = spot

    # TODO: %day requires previous close — leave empty for Phase 1 to avoid
    # another data dependency. Can add later from daily candles.
    snap["pct_day"] = None

    pb = _get_potter_box_snapshot(ticker)
    oi = _get_oi_snapshot(ticker)
    flow = _get_flow_snapshot(ticker)
    as_sig = _get_active_scanner_snapshot(ticker)
    thesis = _get_thesis_snapshot(ticker)
    gex = _get_gex_snapshot(ticker)
    camps = _get_open_campaigns_snapshot(ticker)

    snap.update({
        "pb_floor": pb["floor"],
        "pb_roof": pb["roof"],
        "pb_location": pb["location"],
        "oi_time": oi["time"],
        "oi_side": oi["side"],
        "oi_direction": oi["direction"],
        "flow_time": flow["time"],
        "flow_direction": flow["direction"],
        "flow_notional": flow["notional"],
        "active_scanner": as_sig,
        "thesis_bias": thesis["bias"],
        "thesis_score": thesis["score"],
        "gex_sign": gex["sign"],
        "gamma_flip": gex["flip"],
        "open_campaigns": camps["count"],
        "best_mfe": camps["best_mfe"],
    })

    # v8.2: Option B PnL rollups (supplied once per tick, applied per ticker)
    if pnl_rollup:
        snap["best_peak_pct_lifetime"] = pnl_rollup.get("best_peak_pct_lifetime")
        snap["best_245_pct"] = pnl_rollup.get("best_245_pct")
        snap["best_hold_peak_pct"] = pnl_rollup.get("best_hold_peak_pct")
    else:
        snap["best_peak_pct_lifetime"] = None
        snap["best_245_pct"] = None
        snap["best_hold_peak_pct"] = None

    counts = _count_signal_directions(
        flow["direction"], oi["side"], oi["direction"],
        thesis["bias"], pb["location"], as_sig,
    )
    snap["signals_bullish"] = counts["bullish"]
    snap["signals_bearish"] = counts["bearish"]

    if counts["bullish"] > counts["bearish"] and counts["bullish"] >= 2:
        snap["net_direction"] = "bullish"
    elif counts["bearish"] > counts["bullish"] and counts["bearish"] >= 2:
        snap["net_direction"] = "bearish"
    elif counts["bullish"] == 0 and counts["bearish"] == 0:
        snap["net_direction"] = "none"
    else:
        snap["net_direction"] = "mixed"

    snap["updated"] = _ct_now().strftime("%H:%M:%S")
    return snap


# ═══════════════════════════════════════════════════════════════════
# REGIME CONTEXT
# ═══════════════════════════════════════════════════════════════════

def _get_regime_context() -> Dict[str, Any]:
    """Single-call regime snapshot for signal log stamping and header display."""
    ctx = {
        "market_regime": "",
        "vol_regime": "",
        "dealer_regime": "",
        "vix": None,
    }
    if _get_market_regime_pkg_fn:
        try:
            pkg = _get_market_regime_pkg_fn() or {}
            ctx["market_regime"] = str(
                pkg.get("effective_regime") or pkg.get("core_regime") or ""
            )
        except Exception as e:
            log.debug(f"dashboard: market regime fetch failed: {e}")

    if _get_vix_ts_fn:
        try:
            vts = _get_vix_ts_fn() or {}
            ctx["vix"] = vts.get("vix") or vts.get("VIX")
            # Vol regime: derive from term structure state if present
            state = vts.get("state") or vts.get("regime") or ""
            ctx["vol_regime"] = str(state).upper() if state else ""
        except Exception as e:
            log.debug(f"dashboard: vix term structure fetch failed: {e}")

    return ctx


# ═══════════════════════════════════════════════════════════════════
# SIGNAL LOG DETECTION — diff against prior snapshots
# ═══════════════════════════════════════════════════════════════════

_prior_snapshots: Dict[str, Dict[str, Any]] = {}


def _detect_new_signals(ticker: str, snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compare this snapshot to the prior one for the ticker. Return new events.

    An event is emitted when a directional signal transitions from 'none' to
    something, or when it changes direction.
    """
    events = []
    prior = _prior_snapshots.get(ticker, {})

    def _changed(key: str) -> bool:
        return prior.get(key) != snap.get(key)

    # Flow conviction transitions
    if _changed("flow_direction") and snap.get("flow_direction") in ("bullish", "bearish"):
        events.append({
            "signal_type": "flow_conviction",
            "direction": snap["flow_direction"],
            "detail": f"notional=${snap.get('flow_notional', 0):,}",
        })

    # OI confirmation transitions
    if _changed("oi_direction") and snap.get("oi_direction") in ("buildup", "unwind"):
        direction = "bullish" if (
            (snap.get("oi_direction") == "buildup" and snap.get("oi_side") == "call")
            or (snap.get("oi_direction") == "unwind" and snap.get("oi_side") == "put")
        ) else "bearish"
        events.append({
            "signal_type": "oi_confirm",
            "direction": direction,
            "detail": f"{snap.get('oi_direction')} on {snap.get('oi_side')}s",
        })

    # Potter Box breakout transitions
    if _changed("pb_location") and snap.get("pb_location") in ("above", "below"):
        direction = "bullish" if snap["pb_location"] == "above" else "bearish"
        events.append({
            "signal_type": "potter_box_breakout",
            "direction": direction,
            "detail": (
                f"broke {snap['pb_location']} "
                f"box ${snap.get('pb_floor', '?')}-${snap.get('pb_roof', '?')}"
            ),
        })

    # Active scanner tier signal
    if _changed("active_scanner") and snap.get("active_scanner"):
        as_sig = snap["active_scanner"]
        direction = "bullish" if "🐂" in as_sig else ("bearish" if "🐻" in as_sig else "neutral")
        events.append({
            "signal_type": "active_scanner",
            "direction": direction,
            "detail": as_sig,
        })

    # Thesis bias change
    if _changed("thesis_bias") and snap.get("thesis_bias") not in ("NEUTRAL", ""):
        tb = snap["thesis_bias"]
        direction = "bullish" if "BULLISH" in tb else ("bearish" if "BEARISH" in tb else "neutral")
        events.append({
            "signal_type": "thesis",
            "direction": direction,
            "detail": f"{tb} score={snap.get('thesis_score', 0)}",
        })

    # Cache this snapshot as prior
    _prior_snapshots[ticker] = dict(snap)
    return events


# ═══════════════════════════════════════════════════════════════════
# POSITION PnL TRACKING (v8.2 — Option A + Option B)
#
# Data sources:
#   - RecommendationStore.list_all_active()        — active positions
#   - RecommendationStore.list_graded_in_range(ts) — recently closed
#
# The tracker already maintains:
#   entry_option_mark, peak_option_mark, last_option_mark, mfe_pct,
#   exit_option_mark, exit_ts, status, legs
# so all "peak lifetime" data is sourced directly. We add:
#   1. 2:45 PM CT snapshot captured per-tick into Redis
#   2. Close-event detection -> Signal Log row on state transition
#   3. Per-ticker rollup for Dashboard tab (Option B)
#   4. Per-position rows for Position PnL tab  (Option A)
# ═══════════════════════════════════════════════════════════════════

# Redis key scheme: dashboard:close_245:{campaign_id}
# TTL 90 days — long enough for retrospective review, short enough
# that a stray test campaign doesn't linger forever.
_CLOSE_245_KEY_PREFIX = "dashboard:close_245:"
_CLOSE_245_TTL_SEC = 90 * 86400

# 2:45 PM Central snapshot window (inclusive start, no upper bound — once
# captured the snapshot is write-once). We snapshot on/after 14:45 CT
# on a position's expiry day, since that's the "last meaningful price
# before expiry decay collapses optionality".
_SNAPSHOT_HOUR_CT = 14
_SNAPSHOT_MIN_CT = 45

# Module-level close-detection state. Rebuilt lazily; idempotent if lost.
_last_seen_graded_ids: set = set()
_close_events_bootstrap_done: bool = False


def _campaign_expiry_date(rec: Dict[str, Any]):
    """Pull the nearest expiry from a campaign's legs. Returns a date or None."""
    from datetime import date as _date
    legs = rec.get("legs") or []
    dates = []
    for leg in legs:
        raw = str(leg.get("expiry", ""))[:10]
        if not raw:
            continue
        try:
            dates.append(_date.fromisoformat(raw))
        except Exception:
            continue
    return min(dates) if dates else None


def _format_legs_short(rec: Dict[str, Any]) -> str:
    """Compact string for the Legs column: '+165C 250516 / -170C 250516'."""
    legs = rec.get("legs") or []
    parts = []
    for leg in legs:
        action = str(leg.get("action") or "buy").lower()
        sign = "+" if action.startswith("b") else "-"
        strike = leg.get("strike")
        try:
            strike_s = f"{float(strike):g}" if strike is not None else "?"
        except (TypeError, ValueError):
            strike_s = "?"
        right = str(leg.get("right") or "").upper()[:1]
        exp_raw = str(leg.get("expiry", ""))[:10]
        exp_s = exp_raw.replace("-", "")[2:] if exp_raw else ""
        parts.append(f"{sign}{strike_s}{right} {exp_s}".strip())
    return " / ".join(parts)


def _primary_leg(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Return the first buy leg if any (or the first leg) — for Side/Strike columns."""
    legs = rec.get("legs") or []
    for leg in legs:
        action = str(leg.get("action") or "buy").lower()
        if action.startswith("b"):
            return leg
    return legs[0] if legs else {}


def _pnl_pct_lifetime(rec: Dict[str, Any]) -> Optional[float]:
    """Peak option PnL% over the position's whole tracked life, as a fraction.

    Uses mfe_pct if populated; otherwise derives from peak/entry marks. The
    tracker stores mfe_pct as a fraction (e.g. 0.35 = 35%). Returns None
    when neither source is usable — missing peak data reads as missing,
    not as '-100%'.
    """
    mfe = rec.get("mfe_pct")
    try:
        if mfe is not None:
            return float(mfe)
    except (TypeError, ValueError):
        pass
    entry = rec.get("entry_option_mark")
    peak = rec.get("peak_option_mark")
    if entry is None or peak is None:
        return None
    try:
        e = float(entry)
        p = float(peak)
        if e > 0:
            return (p - e) / e
    except (TypeError, ValueError):
        pass
    return None


def _pnl_pct_current(rec: Dict[str, Any]) -> Optional[float]:
    """Current (or final, if closed) option PnL% as a fraction.

    Returns None when the relevant mark is missing — a graded record without
    exit_option_mark, or an active record without last_option_mark, is a
    data-integrity case and shouldn't silently render as '-100%'.
    """
    entry = rec.get("entry_option_mark")
    if rec.get("status") == "graded":
        current = rec.get("exit_option_mark")
    else:
        current = rec.get("last_option_mark")
    if entry is None or current is None:
        return None
    try:
        e = float(entry)
        c = float(current)
        if e > 0:
            return (c - e) / e
    except (TypeError, ValueError):
        pass
    return None


def _pnl_pct_hold_peak(rec: Dict[str, Any]) -> Optional[float]:
    """Peak PnL% during the window the user was actively holding.

    In the current tracker, polling stops at grade so peak-during-hold ==
    peak-lifetime. Kept as its own function so this can diverge later if
    the bot begins tracking premium past close (e.g. for retrospective
    'would have made' analysis).
    """
    return _pnl_pct_lifetime(rec)


def _close_245_key(campaign_id: str) -> str:
    return f"{_CLOSE_245_KEY_PREFIX}{campaign_id}"


def _get_close_245_snapshot(campaign_id: str) -> Optional[Dict[str, Any]]:
    """Read the stored 2:45 snapshot for a campaign, or None."""
    if _persistent_state is None or not campaign_id:
        return None
    try:
        return _persistent_state._json_get(_close_245_key(campaign_id))
    except Exception as e:
        log.debug(f"dashboard: 2:45 snapshot read failed for {campaign_id}: {e}")
        return None


def _snapshot_close_245_if_due(rec: Dict[str, Any]) -> bool:
    """Write the 2:45 PM CT snapshot for this position if the window is open
    and we don't already have one. Idempotent; returns True if a write was
    performed this call.
    """
    if _persistent_state is None:
        return False

    cid = rec.get("campaign_id")
    if not cid:
        return False

    # Already captured — the snapshot is write-once by design.
    if _get_close_245_snapshot(cid) is not None:
        return False

    # Window check: we only snapshot on/after 14:45 CT on the position's
    # expiry day (or the last hold day if user closed earlier — for the
    # graded case we use exit_ts as the "last hold day" proxy).
    now_ct = _ct_now()
    exp_d = _campaign_expiry_date(rec)
    if exp_d is None:
        return False

    if rec.get("status") == "graded":
        # For already-closed positions, we only snapshot if close was on
        # the expiry day AND 2:45 window had been reached at close time.
        try:
            from datetime import datetime as _dt
            exit_ts = float(rec.get("exit_ts") or 0)
            if exit_ts <= 0:
                return False
            exit_ct = _dt.fromtimestamp(exit_ts, tz=ZoneInfo("America/Chicago"))
        except Exception:
            return False
        if exit_ct.date() != exp_d:
            return False
        if (exit_ct.hour, exit_ct.minute) < (_SNAPSHOT_HOUR_CT, _SNAPSHOT_MIN_CT):
            return False
    else:
        # Active position: snapshot once now_ct >= 14:45 on expiry day.
        if now_ct.date() != exp_d:
            return False
        if (now_ct.hour, now_ct.minute) < (_SNAPSHOT_HOUR_CT, _SNAPSHOT_MIN_CT):
            return False

    pnl = _pnl_pct_current(rec)
    if pnl is None:
        log.debug(f"dashboard: 2:45 snapshot skipped for {cid} — PnL not computable")
        return False

    snapshot = {
        "campaign_id": cid,
        "ticker": rec.get("ticker", ""),
        "pnl_pct": pnl,
        "captured_ts": time.time(),
        "captured_ct": now_ct.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_mark": rec.get("entry_option_mark"),
        "mark_at_snapshot": (rec.get("exit_option_mark")
                             if rec.get("status") == "graded"
                             else rec.get("last_option_mark")),
    }
    try:
        ok = _persistent_state._json_set(
            _close_245_key(cid), snapshot, _CLOSE_245_TTL_SEC
        )
    except Exception as e:
        log.warning(f"dashboard: 2:45 snapshot write failed for {cid}: {e}")
        return False
    if ok:
        log.info(
            f"dashboard: 2:45 snapshot captured for {rec.get('ticker','?')} "
            f"(campaign {cid[:12]}): PnL {pnl*100:+.1f}%"
        )
    return bool(ok)


def _all_relevant_positions() -> List[Dict[str, Any]]:
    """Active + recently-closed campaigns (last 30 days). Deduplicated by id."""
    if _rec_tracker is None:
        return []
    out = []
    seen = set()
    try:
        for r in _rec_tracker.list_all_active() or []:
            cid = r.get("campaign_id")
            if cid and cid not in seen:
                seen.add(cid)
                out.append(r)
    except Exception as e:
        log.debug(f"dashboard: list_all_active failed: {e}")
    try:
        since_ts = time.time() - (30 * 86400)
        for r in _rec_tracker.list_graded_in_range(since_ts) or []:
            cid = r.get("campaign_id")
            if cid and cid not in seen:
                seen.add(cid)
                out.append(r)
    except Exception as e:
        log.debug(f"dashboard: list_graded_in_range failed: {e}")
    return out


def _get_position_pnl_rollup_map(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Aggregate per-ticker best lifetime / best 2:45 / best hold-peak across
    every supplied position record. Used for Option B Dashboard columns.

    Returns: { "AAPL": {"best_peak_pct_lifetime": 0.42, "best_245_pct": 0.18,
                         "best_hold_peak_pct": 0.42}, ... }
    """
    out: Dict[str, Dict[str, float]] = {}
    for rec in records:
        t = (rec.get("ticker") or "").upper()
        if not t:
            continue
        bucket = out.setdefault(t, {
            "best_peak_pct_lifetime": None,
            "best_245_pct": None,
            "best_hold_peak_pct": None,
        })

        lp = _pnl_pct_lifetime(rec)
        if lp is not None:
            if bucket["best_peak_pct_lifetime"] is None or lp > bucket["best_peak_pct_lifetime"]:
                bucket["best_peak_pct_lifetime"] = lp

        hp = _pnl_pct_hold_peak(rec)
        if hp is not None:
            if bucket["best_hold_peak_pct"] is None or hp > bucket["best_hold_peak_pct"]:
                bucket["best_hold_peak_pct"] = hp

        cid = rec.get("campaign_id")
        if cid:
            snap = _get_close_245_snapshot(cid)
            if snap and snap.get("pnl_pct") is not None:
                p = snap["pnl_pct"]
                if bucket["best_245_pct"] is None or p > bucket["best_245_pct"]:
                    bucket["best_245_pct"] = p

    return out


def _position_pnl_row(rec: Dict[str, Any]) -> List[Any]:
    """One row of the Position PnL tab, matching POSITION_PNL_HEADERS order."""
    from datetime import datetime as _dt

    cid = rec.get("campaign_id", "")
    ticker = rec.get("ticker", "")
    structure = rec.get("structure", "")
    status_raw = rec.get("status", "")
    status = "closed" if status_raw == "graded" else "open"

    # Times in Central
    try:
        entry_ts = float(rec.get("entry_ts") or 0)
        opened_ct = (_dt.fromtimestamp(entry_ts, tz=ZoneInfo("America/Chicago"))
                     .strftime("%Y-%m-%d %H:%M"))
    except Exception:
        opened_ct = ""
    closed_ct = ""
    if rec.get("exit_ts"):
        try:
            closed_ct = (_dt.fromtimestamp(float(rec["exit_ts"]),
                                           tz=ZoneInfo("America/Chicago"))
                         .strftime("%Y-%m-%d %H:%M"))
        except Exception:
            closed_ct = ""

    leg = _primary_leg(rec)
    side_raw = (rec.get("direction") or "").lower()
    side = "bull" if side_raw in ("bull", "long") else ("bear" if side_raw in ("bear", "short") else side_raw)
    strike_s = ""
    try:
        if leg.get("strike") is not None:
            strike_s = f"{float(leg['strike']):g}"
    except (TypeError, ValueError):
        pass

    entry_prem = rec.get("entry_option_mark")
    peak_prem = rec.get("peak_option_mark")
    close_prem = rec.get("exit_option_mark")

    pnl_lifetime = _pnl_pct_lifetime(rec)
    pnl_hold = _pnl_pct_hold_peak(rec)
    pnl_current = _pnl_pct_current(rec)

    snap_245 = _get_close_245_snapshot(cid) if cid else None
    pnl_245 = snap_245.get("pnl_pct") if snap_245 else None

    return [
        opened_ct,
        ticker,
        cid,
        structure,
        _format_legs_short(rec),
        side,
        strike_s,
        _fmt_num(entry_prem, 2),
        _fmt_num(peak_prem, 2),
        _fmt_pct(pnl_lifetime),
        _fmt_pct(pnl_245),
        _fmt_pct(pnl_hold),
        closed_ct,
        _fmt_num(close_prem, 2) if close_prem is not None else "",
        _fmt_pct(pnl_current),
        status,
    ]


def _detect_close_events(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return Signal Log-ready event dicts for positions newly in 'graded'
    state this tick. Bootstraps silently on first call so we don't fire
    'close' events for positions that were already closed before the bot
    booted.
    """
    global _last_seen_graded_ids, _close_events_bootstrap_done

    graded = {
        r.get("campaign_id"): r
        for r in records
        if r.get("status") == "graded" and r.get("campaign_id")
    }

    if not _close_events_bootstrap_done:
        _last_seen_graded_ids = set(graded.keys())
        _close_events_bootstrap_done = True
        log.info(
            f"dashboard: position-close detection bootstrapped "
            f"(ignoring {len(_last_seen_graded_ids)} pre-existing graded positions)"
        )
        return []

    new_ids = set(graded.keys()) - _last_seen_graded_ids
    _last_seen_graded_ids = set(graded.keys())

    events = []
    for cid in new_ids:
        rec = graded[cid]
        t = (rec.get("ticker") or "").upper()
        side = (rec.get("direction") or "").lower()
        direction = "bullish" if side in ("bull", "long") else ("bearish" if side in ("bear", "short") else "neutral")
        pnl_lifetime = _pnl_pct_lifetime(rec)
        pnl_current = _pnl_pct_current(rec)
        pnl_hold = _pnl_pct_hold_peak(rec)
        snap_245 = _get_close_245_snapshot(cid)
        pnl_245 = snap_245.get("pnl_pct") if snap_245 else None

        def _p(v):
            return f"{v*100:+.1f}%" if isinstance(v, (int, float)) else "n/a"

        detail = (
            f"closed {rec.get('structure','?')} "
            f"entry=${rec.get('entry_option_mark','?')} "
            f"exit=${rec.get('exit_option_mark','?')} "
            f"final={_p(pnl_current)} peak={_p(pnl_lifetime)} "
            f"245={_p(pnl_245)} hold_peak={_p(pnl_hold)} "
            f"reason={rec.get('exit_reason','?')}"
        )
        events.append({
            "ticker": t,
            "signal_type": "position_close",
            "direction": direction,
            "detail": detail,
        })
    return events


# ═══════════════════════════════════════════════════════════════════
# ROW FORMATTING
# ═══════════════════════════════════════════════════════════════════

def _fmt_money(v) -> str:
    if v is None or v == 0:
        return ""
    try:
        n = int(float(v))
        if n >= 1_000_000:
            return f"${n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"${n/1_000:.0f}K"
        return f"${n}"
    except (TypeError, ValueError):
        return ""


def _fmt_num(v, places: int = 2) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v):.{places}f}"
    except (TypeError, ValueError):
        return ""


def _fmt_pct(v) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v)*100:+.1f}%"
    except (TypeError, ValueError):
        return ""


def _dashboard_row_values(snap: Dict[str, Any]) -> List[Any]:
    """Convert a snapshot dict to the list of cell values matching DASHBOARD_HEADERS."""
    return [
        snap.get("ticker", ""),
        _fmt_num(snap.get("spot"), 2),
        _fmt_pct(snap.get("pct_day")),
        _fmt_num(snap.get("pb_floor"), 2),
        _fmt_num(snap.get("pb_roof"), 2),
        snap.get("pb_location", ""),
        snap.get("oi_time", ""),
        snap.get("oi_side", "") if snap.get("oi_side") != "none" else "",
        snap.get("oi_direction", "") if snap.get("oi_direction") != "none" else "",
        snap.get("flow_time", ""),
        snap.get("flow_direction", "") if snap.get("flow_direction") != "none" else "",
        _fmt_money(snap.get("flow_notional")),
        snap.get("active_scanner", ""),
        snap.get("thesis_bias", ""),
        snap.get("thesis_score", ""),
        snap.get("gex_sign", "") if snap.get("gex_sign") != "unknown" else "",
        _fmt_num(snap.get("gamma_flip"), 2),
        snap.get("open_campaigns", 0),
        _fmt_pct(snap.get("best_mfe")) if snap.get("best_mfe") else "",
        # v8.2 (Option B): per-ticker PnL rollups sourced from
        # _get_position_pnl_rollup_map(). Blank when no positions exist for
        # that ticker or the rollup is unavailable.
        _fmt_pct(snap.get("best_peak_pct_lifetime")),
        _fmt_pct(snap.get("best_245_pct")),
        _fmt_pct(snap.get("best_hold_peak_pct")),
        snap.get("signals_bullish", 0),
        snap.get("signals_bearish", 0),
        snap.get("net_direction", ""),
        snap.get("updated", ""),
    ]


def _signal_log_row(ticker: str, event: Dict[str, Any], regime_ctx: Dict[str, Any]) -> List[Any]:
    now = _ct_now()
    return [
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        ticker,
        event.get("signal_type", ""),
        event.get("direction", ""),
        event.get("detail", ""),
        regime_ctx.get("market_regime", ""),
        regime_ctx.get("vol_regime", ""),
        regime_ctx.get("dealer_regime", ""),
        regime_ctx.get("vix", ""),
        "",  # Outcome — filled manually
        "",  # Notes — filled manually
    ]


# ═══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════

def _dashboard_loop():
    """60s loop: aggregate, write Dashboard tab, append Signal Log events."""
    log.info(f"dashboard: loop entering — first write in {DASHBOARD_INTERVAL_SEC}s")
    # Initial delay so the rest of the bot has time to finish boot
    time.sleep(min(DASHBOARD_INTERVAL_SEC, 30))

    # Consecutive failure counter — after 5 consecutive failures, slow down
    # to avoid log spam and quota pressure. Resets on any successful write.
    consecutive_failures = 0

    while True:
        try:
            _dashboard_tick()
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            log.warning(f"dashboard: tick error (#{consecutive_failures}): {e}")

        # Back off if we're failing
        if consecutive_failures >= 5:
            sleep_sec = min(600, DASHBOARD_INTERVAL_SEC * 5)
            log.warning(
                f"dashboard: {consecutive_failures} consecutive failures, "
                f"backing off to {sleep_sec}s"
            )
            time.sleep(sleep_sec)
        else:
            time.sleep(DASHBOARD_INTERVAL_SEC)


def _dashboard_tick() -> None:
    """One iteration: fetch token, ensure tabs, aggregate, write.

    v8.2: also drives position PnL — the 2:45 close snapshot, the Option-A
    Position PnL tab, the Option-B per-ticker rollup columns on the
    Dashboard tab, and position-close events appended to Signal Log.
    """
    with _dashboard_lock:
        token = _get_token_fn() if _get_token_fn else None
        if not token:
            log.warning("dashboard: no sheets token available — skipping this tick")
            return

        # Ensure tabs exist (cheap after first call via cache)
        if not _ensure_tab_exists(TAB_DASHBOARD, DASHBOARD_HEADERS, token):
            log.warning(f"dashboard: {TAB_DASHBOARD} tab not ready — skipping tick")
            return
        if not _ensure_tab_exists(TAB_SIGNAL_LOG, SIGNAL_LOG_HEADERS, token):
            log.warning(f"dashboard: {TAB_SIGNAL_LOG} tab not ready — skipping tick")
            return
        # v8.2: Position PnL tab — best-effort; missing tab should never
        # stall the rest of the tick, so we warn and continue without it.
        pnl_tab_ready = _ensure_tab_exists(
            TAB_POSITION_PNL, POSITION_PNL_HEADERS, token
        )
        if not pnl_tab_ready:
            log.warning(
                f"dashboard: {TAB_POSITION_PNL} tab not ready — "
                f"continuing without PnL tab; dashboard rollup columns will be blank"
            )

        tickers = []
        try:
            tickers = list(_get_flow_tickers_fn() or [])
        except Exception as e:
            log.warning(f"dashboard: flow tickers fetch failed: {e}")
        if not tickers:
            log.warning("dashboard: no tickers to aggregate — skipping tick")
            return

        # v8.2: fetch all position records ONCE per tick, then derive
        # everything downstream from that list (rollup, 2:45 snapshots,
        # Position PnL rows, close-event detection).
        position_records: List[Dict[str, Any]] = []
        try:
            position_records = _all_relevant_positions()
        except Exception as e:
            log.warning(f"dashboard: position record fetch failed: {e}")

        # 2:45 PM CT snapshots (write-once per campaign).
        snapshots_written = 0
        for rec in position_records:
            try:
                if _snapshot_close_245_if_due(rec):
                    snapshots_written += 1
            except Exception as e:
                log.debug(f"dashboard: 2:45 snapshot loop error: {e}")
        if snapshots_written:
            log.info(f"dashboard: 2:45 snapshots captured this tick: {snapshots_written}")

        # Per-ticker rollup for Option B columns (built once, indexed
        # into by ticker symbol).
        try:
            rollup_map = _get_position_pnl_rollup_map(position_records)
        except Exception as e:
            log.warning(f"dashboard: rollup build failed: {e}")
            rollup_map = {}

        regime_ctx = _get_regime_context()
        rows = []
        all_events = []

        for t in tickers:
            try:
                snap = get_ticker_snapshot(
                    t, pnl_rollup=rollup_map.get(t.upper())
                )
            except Exception as e:
                log.warning(f"dashboard: snapshot failed for {t}: {e}")
                continue
            rows.append(_dashboard_row_values(snap))

            try:
                events = _detect_new_signals(t, snap)
                for e in events:
                    all_events.append(_signal_log_row(t, e, regime_ctx))
            except Exception as e:
                log.warning(f"dashboard: event detection failed for {t}: {e}")

        # v8.2: position close events -> Signal Log (bootstrap is silent
        # on first tick so we don't spam events for pre-existing closes).
        try:
            close_events = _detect_close_events(position_records)
            for ce in close_events:
                all_events.append(
                    _signal_log_row(ce["ticker"], ce, regime_ctx)
                )
            if close_events:
                log.info(f"dashboard: detected {len(close_events)} newly-closed positions")
        except Exception as e:
            log.warning(f"dashboard: close-event detection failed: {e}")

        if rows:
            ok = _write_dashboard_values(rows, token)
            if ok:
                log.info(
                    f"dashboard: wrote {len(rows)} ticker rows "
                    f"(regime={regime_ctx.get('market_regime','?')}, "
                    f"vix={regime_ctx.get('vix','?')})"
                )
            else:
                log.warning(f"dashboard: dashboard write FAILED ({len(rows)} rows)")
                raise RuntimeError("dashboard write returned False")

        # v8.2: write the Position PnL tab (Option A). Full overwrite each
        # tick so stale state can't linger. Build rows oldest-first so
        # recent positions sit at the bottom of the tab (newer-at-bottom
        # matches tracker.entry_date ordering).
        if pnl_tab_ready:
            try:
                pnl_records_sorted = sorted(
                    position_records,
                    key=lambda r: float(r.get("entry_ts") or 0),
                )
                pnl_rows = []
                for rec in pnl_records_sorted:
                    try:
                        pnl_rows.append(_position_pnl_row(rec))
                    except Exception as e:
                        log.debug(
                            f"dashboard: position pnl row build failed for "
                            f"{rec.get('campaign_id','?')}: {e}"
                        )
                ok = _write_position_pnl_values(pnl_rows, token)
                if ok:
                    log.info(
                        f"dashboard: wrote {len(pnl_rows)} position pnl rows"
                    )
                else:
                    log.warning(
                        f"dashboard: position pnl write FAILED "
                        f"({len(pnl_rows)} rows)"
                    )
            except Exception as e:
                log.warning(f"dashboard: position pnl section failed: {e}")

        if all_events:
            ok = _append_signal_log_rows(all_events, token)
            if ok:
                log.info(f"dashboard: appended {len(all_events)} signal events")
            else:
                log.warning(f"dashboard: signal log append FAILED ({len(all_events)} events)")
                # don't raise — signal log failure shouldn't bring down the dashboard
