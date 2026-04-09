# app.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v4.2 UPGRADE (2026-03-17):
#   - CHECK_TICKER_TIMEOUT_SEC imported from trading_rules (45s, was hardcoded 75s)
#   - PREFETCH_WORKER_WAIT_SEC used consistently (was hardcoded 60s in one path)
#   - PIN regime gate: blocks directional spreads when v4 prefilter reports *PIN
#   - CHOP regime gate: raises confidence threshold + reduces size in choppy tape
#   - _estimate_iv_rank: IV clamped to SWING_IV_MIN/MAX (same fix as swing_engine)
#   - _get_ohlc_bars: OHLC_WARN_ONCE_PER_CYCLE dedup (32 duplicate MA warnings today)
#   - CHOP_REGIME_SIZE_MULT applied in _post_trade_card size block
#
# v4.0/v4.1 preserved:
#   - v4 institutional engine integration via engine_bridge.py
#   - Confidence scoring on all cards (HIGH/MODERATE/LOW)
#   - Wave prefetch cache, Redis-backed signal queue
#   - CAGF regime gate for SPY/QQQ/SPX
#   - EM-aware strike placement and width clamping
#   - EM accuracy logger + auto-reconciler

from telegram_commands import (
    handle_command,
    register_webhook,
    is_paused,
    get_confidence_gate,
    set_last_scan,
    get_state,
    send_reply,
    init_shared_state,
)

import jwt
import os
import time
import math
import json
import hashlib
import base64
import logging
import csv
import threading
import queue
import fcntl
import portfolio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify

from options_engine_v3 import (
    recommend_trade,
    format_trade_card,
    as_float,
    as_int,
)
from data_providers import (
    enrich_ticker,
    get_earnings_warning,
    get_iv_rank_from_candles,
)
from trading_rules import (
    MIN_DTE, MAX_DTE, TARGET_DTE,
    MIN_CONFIDENCE_TO_TRADE,
    MIN_WIN_PROBABILITY,
    ALLOWED_DIRECTIONS,
    NO_EARNINGS_WEEK,
    RV_LOOKBACK_DAYS,
    JOURNAL_LOG_ALL_SIGNALS,
    IMMEDIATE_POST_TIER, IMMEDIATE_POST_MIN_CONF, IMMEDIATE_POST_0DTE,
    DIGEST_CARD_CACHE_TTL_SEC,
    # v4.2
    CHECK_TICKER_TIMEOUT_SEC,
    CHOP_REGIME_CONF_GATE, CHOP_REGIME_SIZE_MULT,
    PIN_REGIME_BLOCK_BEAR_PUTS, PIN_REGIME_BLOCK_BULL_CALLS,
    SWING_IV_MIN, SWING_IV_MAX, SWING_IV_ATM_BAND_PCT,
    OHLC_WARN_ONCE_PER_CYCLE,
    # v5.0
    PRECHAIN_GATE_ENABLED,
    ACTIVE_SCANNER_ENABLED,
    FUNDAMENTAL_SCREENING_ENABLED,
    CONFIDENCE_BOOSTS, CONFIDENCE_PENALTIES,
    IV_RV_RATIO_BUYER_EDGE, IV_RV_RATIO_SELLER_EDGE,
    HIGH_VOLUME_TICKERS,
    # v5.1
    SWING_SCANNER_ENABLED,
)
# ── v4 engine bridge ──
from engine_bridge import (
    run_institutional_snapshot,
    build_option_rows,
    build_chain_dicts,
    format_confidence_header,
    format_trade_sign_line,
    format_vol_regime_line,
)
from options_exposure import SCHEMA_VERSION
from oi_cache import OICache
from api_cache import CachedMarketData
from institutional_flow import compute_cagf, recommend_dte, format_cagf_block, format_dte_block
from card_formatters import (
    format_plain_english_card,
    format_decision_card,
    resolve_unified_regime,
    regime_gate,
)
from unified_models import (
    build_canonical_vol_regime as _um_build_canonical_vol_regime,
    format_canonical_vol_line as _um_format_canonical_vol_line,
    apply_vol_overlay_to_rec as _um_apply_vol_overlay_to_rec,
    build_canonical_structure_context as _um_build_canonical_structure_context,
    apply_structure_overlay_to_rec as _um_apply_structure_overlay_to_rec,
    build_manual_swing_signal_context as _um_build_manual_swing_signal_context,
    build_shared_model_snapshot as _um_build_shared_model_snapshot,
    format_shared_snapshot_lines as _um_format_shared_snapshot_lines,
    classify_rejection_bucket as _um_classify_rejection_bucket,
    apply_effective_regime_gate_to_rec as _um_apply_effective_regime_gate_to_rec,
)
from options_exposure import implied_vol as _solve_implied_vol
from em_reconciler import (
    reconcile_em_predictions,
    compute_accuracy_stats,
    format_accuracy_report,
    fetch_eod_close_marketdata,
)

import risk_manager
import trade_journal

# ── v5.0 imports ──
from prechain_gate import should_pull_chains
from vix_term_structure import get_vix_term_structure, format_term_structure_line
from economic_calendar import (
    get_events_in_window, has_high_impact_today,
    get_confidence_adjustment as get_macro_confidence,
    format_calendar_line,
)
from fundamental_screener import (
    get_fundamentals, get_swing_confidence_adjustments,
    classify_lynch, batch_fetch_fundamentals,
)
from sector_rotation import (
    get_sector_rank, get_all_sector_rankings,
    format_sector_line,
)
from active_scanner import ActiveScanner
from swing_scanner import SwingScanner
from income_wiring import create_income_handlers, create_ohlcv_wrapper
from portfolio_greeks import PortfolioGreeks
from regime_detector import RegimeDetector
from oi_tracker import OITracker
from persistent_state import PersistentState
from oi_flow import FlowDetector, FLOW_TICKERS
from potter_box import PotterBoxScanner

# ── v4.3: Thesis Monitor ──
from thesis_monitor import (
    get_engine as get_thesis_engine,
    init_daemon as init_thesis_daemon,
    build_thesis_from_em_card,
)

# OI cache will be initialized after store_get/store_set are defined
_oi_cache = None
_oi_tracker = None  # v5.1: daily OI change tracker
_persistent_state = None  # v6.1: Redis-backed persistent state
_flow_detector = None     # v6.1: unified institutional flow detection
_potter_scanner = None    # v6.1: Potter Box consolidation scanner

# ─────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────
# ENV VARS
# ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN",      "").strip()
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID",        "").strip()
TELEGRAM_CHAT_INTRADAY  = os.getenv("TELEGRAM_CHAT_INTRADAY",  "").strip()  # intraday monitor alerts
TV_WEBHOOK_SECRET   = os.getenv("TV_WEBHOOK_SECRET",   "").strip()
MARKETDATA_TOKEN    = os.getenv("MARKETDATA_TOKEN",    "").strip()
WATCHLIST           = os.getenv("WATCHLIST",           "").strip()
SCAN_SECRET         = os.getenv("SCAN_SECRET",         "").strip()
BOT_URL             = os.getenv("BOT_URL",             "").strip()
REDIS_URL           = os.getenv("REDIS_URL",           "").strip()
SCAN_WORKERS        = int(os.getenv("SCAN_WORKERS", "4") or 4)
DEDUP_TTL_SECONDS   = int(os.getenv("DEDUP_TTL_SECONDS", "3600") or 3600)

# Live validation: TradingView is the trigger, bot data is the source of truth
SCALP_SIGNAL_WARN_DRIFT_PCT     = float(os.getenv("SCALP_SIGNAL_WARN_DRIFT_PCT",   "0.20") or 0.20)
SCALP_SIGNAL_REJECT_DRIFT_PCT   = float(os.getenv("SCALP_SIGNAL_REJECT_DRIFT_PCT", "0.35") or 0.35)
SCALP_SIGNAL_HARD_BLOCK_PCT     = float(os.getenv("SCALP_SIGNAL_HARD_BLOCK_PCT",   "1.50") or 1.50)
SWING_SIGNAL_WARN_DRIFT_PCT     = float(os.getenv("SWING_SIGNAL_WARN_DRIFT_PCT",   "0.45") or 0.45)
SWING_SIGNAL_REJECT_DRIFT_PCT   = float(os.getenv("SWING_SIGNAL_REJECT_DRIFT_PCT", "0.75") or 0.75)
SWING_SIGNAL_HARD_BLOCK_PCT     = float(os.getenv("SWING_SIGNAL_HARD_BLOCK_PCT",   "2.50") or 2.50)
SIGNAL_WARN_CONF_PENALTY        = int(os.getenv("SIGNAL_WARN_CONF_PENALTY",        "6")    or 6)
SIGNAL_MODERATE_CONF_PENALTY    = int(os.getenv("SIGNAL_MODERATE_CONF_PENALTY",    "12")   or 12)
SIGNAL_HARD_REJECT_CONF_PENALTY = int(os.getenv("SIGNAL_HARD_REJECT_CONF_PENALTY", "20")   or 20)
SIGNAL_STALE_AFTER_SEC          = int(os.getenv("SIGNAL_STALE_AFTER_SEC",          "900")  or 900)
PENDING_RECHECK_ENABLE         = os.getenv("PENDING_RECHECK_ENABLE",         "1").strip().lower() not in ("0", "false", "no", "off")
PENDING_RECHECK_DELAYS_SEC     = os.getenv("PENDING_RECHECK_DELAYS_SEC",     "300,900,1800").strip() or "300,900,1800"
PENDING_RECHECK_MAX_SIGNAL_AGE_SEC = int(os.getenv("PENDING_RECHECK_MAX_SIGNAL_AGE_SEC", "5400") or 5400)
PENDING_TRIGGER_BUFFER_PCT     = float(os.getenv("PENDING_TRIGGER_BUFFER_PCT", "0.05") or 0.05)
PENDING_RETRACE_GRACE_PCT      = float(os.getenv("PENDING_RETRACE_GRACE_PCT",  "0.03") or 0.03)

TELEGRAM_PORTFOLIO_CHAT_ID     = os.getenv("TELEGRAM_PORTFOLIO_CHAT_ID",     "").strip()
TELEGRAM_MOM_PORTFOLIO_CHAT_ID = os.getenv("TELEGRAM_MOM_PORTFOLIO_CHAT_ID", "").strip()

ACCOUNT_CHAT_IDS = {
    "brad": TELEGRAM_PORTFOLIO_CHAT_ID,
    "mom":  TELEGRAM_MOM_PORTFOLIO_CHAT_ID,
}

# ─────────────────────────────────────────────────────────
# DATASET / DIAGNOSTICS AUT0-LOGGING
# ─────────────────────────────────────────────────────────
DIAGNOSTIC_CHAT_ID = os.getenv("DIAGNOSTIC_CHAT_ID", "").strip()
AUTO_LOG_DIR = os.getenv("AUTO_LOG_DIR", "/mnt/data/bot_logs").strip() or "/mnt/data/bot_logs"
AUTO_LOG_ENABLE = os.getenv("AUTO_LOG_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
AUTO_LOG_DIAGNOSTICS = os.getenv("AUTO_LOG_DIAGNOSTICS", "0").strip().lower() in ("1", "true", "yes", "on")
GOOGLE_SHEETS_ENABLE = os.getenv("GOOGLE_SHEETS_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1bXyAVQ8dB-dTyFVN6uv6PVtuphwRZ6VjIp2W9dcm9iw").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_SIGNAL_TAB = os.getenv("GOOGLE_SHEET_SIGNAL_TAB", "signal_decisions").strip() or "signal_decisions"
GOOGLE_SHEET_EM_TAB = os.getenv("GOOGLE_SHEET_EM_TAB", "em_predictions").strip() or "em_predictions"
GOOGLE_SHEET_RECON_TAB = os.getenv("GOOGLE_SHEET_RECON_TAB", "em_reconciliation").strip() or "em_reconciliation"
GOOGLE_SHEET_CRISIS_TAB = os.getenv("GOOGLE_SHEET_CRISIS_TAB", "crisis_put_signals").strip() or "crisis_put_signals"
GOOGLE_SHEET_SHADOW_TAB = os.getenv("GOOGLE_SHEET_SHADOW_TAB", "shadow_signals").strip() or "shadow_signals"

# ─────────────────────────────────────────────────────────
# REDIS
# ─────────────────────────────────────────────────────────
_redis_client = None


class _MemStore:
    """
    TTL-aware in-memory fallback store.
    Mirrors Redis setex/get/exists interface.
    """
    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def _prune(self):
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._data.items() if exp is not None and now >= exp]
        for k in expired:
            del self._data[k]

    def set(self, key: str, value: str, ttl: int = 0):
        exp = (time.monotonic() + ttl) if ttl > 0 else None
        with self._lock:
            self._prune()
            self._data[key] = (value, exp)

    def get(self, key: str):
        with self._lock:
            self._prune()
            entry = self._data.get(key)
            return entry[0] if entry is not None else None

    def exists(self, key: str) -> bool:
        with self._lock:
            self._prune()
            return key in self._data


_mem_store = _MemStore()
_csv_lock = threading.Lock()


def _ensure_log_dir():
    try:
        os.makedirs(AUTO_LOG_DIR, exist_ok=True)
    except Exception as e:
        log.warning(f"Auto-log dir unavailable ({AUTO_LOG_DIR}): {e}")


def _append_jsonl(filename: str, row: dict):
    if not AUTO_LOG_ENABLE:
        return
    try:
        _ensure_log_dir()
        path = os.path.join(AUTO_LOG_DIR, filename)
        payload = dict(row or {})
        payload.setdefault("logged_at_utc", datetime.now(timezone.utc).isoformat())
        with _csv_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"JSONL auto-log failed ({filename}): {e}")


def _append_csv_row(filename: str, fieldnames: list, row: dict):
    if not AUTO_LOG_ENABLE:
        return
    try:
        _ensure_log_dir()
        path = os.path.join(AUTO_LOG_DIR, filename)
        safe_row = {k: row.get(k) for k in fieldnames}
        exists = os.path.exists(path)
        with _csv_lock:
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not exists or os.path.getsize(path) == 0:
                    writer.writeheader()
                writer.writerow(safe_row)
        log.info(f"CSV auto-log wrote: {filename} -> {path}")
        _append_google_sheet_row(filename, fieldnames, safe_row)
    except Exception as e:
        log.warning(f"CSV auto-log failed ({filename}): {e}")


_google_sheets_lock = threading.Lock()
_google_sheets_token_cache = {"token": None, "exp": 0}
_google_sheets_header_tabs = set()
_google_sheets_sa_cache = None


def _tab_for_filename(filename: str) -> str:
    mapping = {
        "signal_decisions.csv": GOOGLE_SHEET_SIGNAL_TAB,
        "em_predictions.csv": GOOGLE_SHEET_EM_TAB,
        "em_reconciliation.csv": GOOGLE_SHEET_RECON_TAB,
        "crisis_put_signals.csv": GOOGLE_SHEET_CRISIS_TAB,
        "shadow_signals.csv": GOOGLE_SHEET_SHADOW_TAB,
    }
    return mapping.get(filename, "")


def _load_google_service_account() -> dict | None:
    global _google_sheets_sa_cache
    if _google_sheets_sa_cache is not None:
        return _google_sheets_sa_cache
    raw = GOOGLE_SERVICE_ACCOUNT_JSON
    try:
        if raw:
            _google_sheets_sa_cache = json.loads(raw)
            log.info(f"Google Sheets service account loaded from env: {_google_sheets_sa_cache.get('client_email','?')}")
            return _google_sheets_sa_cache
        if GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
                _google_sheets_sa_cache = json.load(f)
                log.info(f"Google Sheets service account loaded from file: {_google_sheets_sa_cache.get('client_email','?')}")
                return _google_sheets_sa_cache
        default_path = "/mnt/data/corbin-bot-tracking-0249b119c63f.json"
        if os.path.exists(default_path):
            with open(default_path, "r", encoding="utf-8") as f:
                _google_sheets_sa_cache = json.load(f)
                log.info(f"Google Sheets service account loaded from default file: {_google_sheets_sa_cache.get('client_email','?')}")
                return _google_sheets_sa_cache
        log.warning("Google Sheets credentials not found in env or file.")
    except Exception as e:
        log.warning(f"Google Sheets credentials load failed: {e}")
    return None


def _get_google_access_token() -> str | None:
    if not GOOGLE_SHEETS_ENABLE or not GOOGLE_SHEET_ID:
        log.info("Google Sheets token fetch skipped: disabled or sheet id missing")
        return None
    now = int(time.time())
    cached = _google_sheets_token_cache
    if cached.get("token") and now < int(cached.get("exp", 0)) - 60:
        return cached["token"]
    sa = _load_google_service_account()
    if not sa:
        return None
    try:
        issued = int(time.time())
        payload = {
            "iss": sa["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file",
            "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
            "iat": issued,
            "exp": issued + 3600,
        }
        assertion = jwt.encode(payload, sa["private_key"], algorithm="RS256")
        resp = requests.post(
            sa.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        exp = issued + int(data.get("expires_in", 3600))
        if token:
            _google_sheets_token_cache.update({"token": token, "exp": exp})
            log.info("Google Sheets token acquired successfully")
            return token
    except Exception as e:
        log.warning(f"Google Sheets token fetch failed: {e}")
    return None


def _sheet_headers_exist(tab: str, token: str) -> bool:
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{requests.utils.quote(tab + '!1:1', safe='!:')}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        values = resp.json().get("values", [])
        return bool(values and any((str(x).strip() for x in values[0])))
    except Exception as e:
        log.warning(f"Google Sheets header check failed for {tab}: {e}")
        return True


def _append_google_sheet_values(tab: str, values: list, token: str) -> bool:
    try:
        rng = requests.utils.quote(f"{tab}!A:A", safe="!:")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{rng}:append"
        resp = requests.post(
            url,
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": values},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        # Log response body for debugging 400 errors
        resp_body = ""
        try:
            resp_body = f" | response: {resp.text[:300]}"
        except Exception:
            pass
        log.warning(f"Google Sheets append failed for {tab}: {e}{resp_body}")
        return False




def _append_google_sheet_row(filename: str, fieldnames: list, row: dict):
    tab = _tab_for_filename(filename)
    if not (GOOGLE_SHEETS_ENABLE and tab and GOOGLE_SHEET_ID):
        return
    token = _get_google_access_token()
    if not token:
        return

    def _fetch_headers(tab_name: str, bearer: str):
        try:
            rng = requests.utils.quote(f"{tab_name}!1:1", safe="!:")
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{rng}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {bearer}"}, timeout=10)
            resp.raise_for_status()
            vals = resp.json().get("values", [])
            return [str(v).strip() for v in vals[0]] if vals else []
        except Exception as e:
            log.warning(f"Google Sheets header fetch failed for {tab_name}: {e}")
            return []

    def _write_headers(tab_name: str, headers: list, bearer: str) -> bool:
        try:
            rng = requests.utils.quote(f"{tab_name}!1:1", safe="!:")
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{rng}"
            resp = requests.put(
                url,
                params={"valueInputOption": "USER_ENTERED"},
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                json={"values": [headers]},
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            log.warning(f"Google Sheets header write failed for {tab_name}: {e}")
            return False

    def _sanitize_cell(v):
        """Convert any value to a Google Sheets safe string."""
        if v is None:
            return ""
        if isinstance(v, float):
            import math
            if math.isnan(v) or math.isinf(v):
                return ""
            return v  # keep as number
        if isinstance(v, (list, dict, set, tuple)):
            return str(v)[:200]
        if isinstance(v, bool):
            return str(v)
        return v

    values = [[_sanitize_cell(row.get(k)) for k in fieldnames]]
    try:
        with _google_sheets_lock:
            current_headers = _fetch_headers(tab, token)
            if current_headers != fieldnames:
                if _write_headers(tab, fieldnames, token):
                    log.info(f"Google Sheets headers synced for tab '{tab}' ({len(fieldnames)} cols)")
            ok = _append_google_sheet_values(tab, values, token)
            if ok:
                log.info(f"Google Sheets append OK for tab '{tab}' (1 row)")
            else:
                log.warning(f"Google Sheets append returned False for tab '{tab}'")
    except Exception as e:
        log.warning(f"Google Sheets row sync failed for {filename}: {e}")


def _post_diagnostic(text: str):
    if not (AUTO_LOG_DIAGNOSTICS and DIAGNOSTIC_CHAT_ID and text):
        return
    try:
        post_to_telegram(text, chat_id=DIAGNOSTIC_CHAT_ID)
    except Exception as e:
        log.warning(f"Diagnostic post failed: {e}")


# ─────────────────────────────────────────────────────────
# SHADOW SIGNAL LOGGER (v6.1)
# ─────────────────────────────────────────────────────────
_SHADOW_FIELDS = [
    "date", "time_ct", "ticker", "regime", "bias", "score",
    "htf", "phase", "close", "vwap_above", "wt2", "rsi", "why_blocked",
]

def _log_shadow_signal(ticker: str, regime: str, signal: dict, why_blocked: str):
    """
    Log a quality signal from a non-active ticker to shadow_signals.csv + Sheets.
    Called by ActiveScanner when a signal passes technical thresholds but is
    filtered by the regime-aware rule gate. No Telegram alert is sent.

    why_blocked values:
      'no_rule_in_regime'       — ticker not in TICKER_RULES for this regime at all
      'rule_exists_signal_filtered' — ticker has a rule but signal didn't match
                                      (wrong HTF, score out of range, wrong phase, etc.)
    """
    try:
        from market_clock import CT
        now = datetime.now(CT)
        row = {
            "date":        now.strftime("%Y-%m-%d"),
            "time_ct":     now.strftime("%H:%M"),
            "ticker":      ticker,
            "regime":      regime,
            "bias":        signal.get("bias", ""),
            "score":       signal.get("score", ""),
            "htf":         signal.get("htf_status", ""),
            "phase":       signal.get("phase", ""),
            "close":       signal.get("close", ""),
            "vwap_above":  str(signal.get("above_vwap", "")),
            "wt2":         round(signal.get("wt2", 0), 1),
            "rsi":         round(signal.get("rsi_mfi", 0), 1) if signal.get("rsi_mfi") else "",
            "why_blocked": why_blocked,
        }
        _append_csv_row("shadow_signals.csv", _SHADOW_FIELDS, row)
    except Exception as e:
        log.debug(f"Shadow log failed for {ticker}: {e}")


# ─────────────────────────────────────────────────────────
# SOURCE TYPE CLASSIFIER (v5.1.1)
# ─────────────────────────────────────────────────────────
def _classify_source_type(source: str, ts_utc_str: str) -> str:
    """Classify signal source into actionable categories.
    Returns: 'tv_hourly', 'tv_daily', 'scanner', 'manual', or 'other'.
    tv_daily signals arrive at ~3:00 PM CT (daily candle close) and CANNOT
    be traded until the next morning. All other types are actionable same session.

    FIX #6: Uses pytz for proper CT conversion (handles CST/CDT automatically).
    FIX #5: Caller must pass the actual signal timestamp, not datetime.now().
    """
    source = (source or "").strip().lower()
    if source == "active_scanner":
        return "scanner"
    if source == "check":
        return "manual"
    if source != "tv":
        return "other"
    try:
        ts = datetime.fromisoformat(ts_utc_str.replace("Z", "+00:00"))
        try:
            import pytz
            ct = pytz.timezone("America/Chicago")
            ts_ct = ts.astimezone(ct)
            hour_ct = ts_ct.hour
        except ImportError:
            hour_ct = (ts.hour - 5) % 24  # fallback if pytz missing
        if hour_ct >= 15:  # 3:00 PM CT or later = daily candle close
            return "tv_daily"
        return "tv_hourly"
    except Exception:
        return "tv_hourly"


# ─────────────────────────────────────────────────────────
# CRISIS LONG PUT RECOMMENDATION ENGINE (v5.1.1)
# ─────────────────────────────────────────────────────────
# FIX #9: Thread-safe dedup dict
_crisis_put_seen_lock = threading.Lock()
_crisis_put_seen_tickers = {}  # dedup_key → True

def _evaluate_crisis_put(ticker: str, bias: str, source_type: str,
                         vol_regime: dict, spot: float, vix: float = None,
                         confidence: float = None):
    """Check if a signal qualifies for a CRISIS long put recommendation.
    If it qualifies, look up the ATM put contract, log to Google Sheets,
    and send a Telegram alert. Runs in a background thread to not block signal flow.
    """
    from trading_rules import (
        CRISIS_PUT_ENABLED, CRISIS_PUT_WHITELIST, CRISIS_PUT_BLACKLIST,
        CRISIS_PUT_ALLOWED_SOURCES, CRISIS_PUT_DTE_TARGET, CRISIS_PUT_DTE_MIN,
        CRISIS_PUT_DTE_MAX, CRISIS_PUT_SCALE1_PCT, CRISIS_PUT_MAX_HOLD_DAYS,
        CRISIS_PUT_MAX_POSITIONS, CRISIS_PUT_MIN_CAUTION,
    )
    if not CRISIS_PUT_ENABLED:
        return
    # v6.1: Gate on caution score not CRISIS label — fires in ELEVATED too (caution 4+).
    vol_caution = (vol_regime or {}).get("caution_score", 0) if isinstance(vol_regime, dict) else 0
    vol_label = (vol_regime or {}).get("label", "") if isinstance(vol_regime, dict) else ""
    if vol_caution < CRISIS_PUT_MIN_CAUTION:
        return
    if bias != "bear":
        return
    if source_type not in CRISIS_PUT_ALLOWED_SOURCES:
        return
    tk = ticker.upper().strip()
    if tk not in CRISIS_PUT_WHITELIST:
        return
    if tk in CRISIS_PUT_BLACKLIST:
        return
    if not spot or spot <= 0:
        return

    # FIX #1: Only enforce max positions in live mode (paper mode = data collection)
    from trading_rules import CRISIS_PUT_AUTO_EXECUTE
    if CRISIS_PUT_AUTO_EXECUTE:
        open_count = len(_crisis_put_get_open_positions())
        if open_count >= CRISIS_PUT_MAX_POSITIONS:
            log.info(f"Crisis put: {tk} skipped — already at max positions ({open_count}/{CRISIS_PUT_MAX_POSITIONS})")
            return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dedup_key = f"{tk}:{today}"

    # FIX #3: Reserve with "pending" state under lock to prevent race duplicates
    with _crisis_put_seen_lock:
        state = _crisis_put_seen_tickers.get(dedup_key)
        if state in ("pending", "done"):
            log.info(f"Crisis put: {tk} already {'in flight' if state == 'pending' else 'recommended'} today, skipping")
            return
        _crisis_put_seen_tickers[dedup_key] = "pending"
        # Prune old entries
        for k in list(_crisis_put_seen_tickers):
            if not k.endswith(f":{today}"):
                _crisis_put_seen_tickers.pop(k, None)

    def _do_crisis_put_lookup():
        try:
            success = _crisis_put_find_and_alert(
                tk, spot, vix, confidence, source_type,
                vol_regime, today,
                CRISIS_PUT_DTE_TARGET, CRISIS_PUT_DTE_MIN, CRISIS_PUT_DTE_MAX,
                CRISIS_PUT_SCALE1_PCT, CRISIS_PUT_MAX_HOLD_DAYS,
            )
            with _crisis_put_seen_lock:
                if success:
                    _crisis_put_seen_tickers[dedup_key] = "done"
                else:
                    # FIX #3/#7: Remove pending so next signal can retry
                    _crisis_put_seen_tickers.pop(dedup_key, None)
        except Exception as e:
            log.error(f"Crisis put lookup failed for {tk}: {e}", exc_info=True)
            with _crisis_put_seen_lock:
                _crisis_put_seen_tickers.pop(dedup_key, None)

    threading.Thread(target=_do_crisis_put_lookup, daemon=True, name=f"crisis-put-{tk}").start()


def _crisis_put_find_and_alert(ticker, spot, vix, confidence, source_type,
                                vol_regime, today,
                                dte_target, dte_min, dte_max,
                                profit_target, max_hold_days) -> bool:
    """Look up the best ATM put contract and send recommendation alert.
    Returns True if recommendation was successfully generated, False otherwise.
    """
    try:
        exps = get_expirations(ticker)
    except Exception as e:
        log.warning(f"Crisis put: can't get expirations for {ticker}: {e}")
        return False
    if not exps:
        log.warning(f"Crisis put: no expirations for {ticker}")
        return False

    now = datetime.now(timezone.utc).date()
    best_exp = None
    best_dte = None
    best_dte_diff = 999
    for exp_str in exps:
        try:
            exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
            dte = (exp_date - now).days
            if dte < dte_min or dte > dte_max:
                continue
            diff = abs(dte - dte_target)
            if diff < best_dte_diff:
                best_dte_diff = diff
                best_exp = exp_str
                best_dte = dte
        except Exception:
            continue

    if not best_exp or not best_dte:
        log.info(f"Crisis put: no suitable expiration for {ticker} (DTE {dte_min}-{dte_max})")
        return False

    try:
        chain_data = _cached_md.get_chain(ticker, str(best_exp)[:10], side="put")
    except Exception as e:
        log.warning(f"Crisis put: chain fetch failed for {ticker} {best_exp}: {e}")
        return False

    if not chain_data or chain_data.get("s") != "ok":
        log.warning(f"Crisis put: no chain data for {ticker} {best_exp}")
        return False

    strikes = chain_data.get("strike", [])
    mids = chain_data.get("mid", [])
    symbols = chain_data.get("optionSymbol", [])
    bids = chain_data.get("bid", [])
    asks = chain_data.get("ask", [])

    if not strikes or not mids:
        return False

    atm_idx = None
    atm_diff = 999999
    for i, s in enumerate(strikes):
        try:
            diff = abs(float(s) - spot)
            if diff < atm_diff:
                atm_diff = diff
                atm_idx = i
        except (ValueError, TypeError):
            continue

    if atm_idx is None:
        return False

    strike = float(strikes[atm_idx])
    mid = float(mids[atm_idx]) if mids[atm_idx] is not None else None
    bid = float(bids[atm_idx]) if bids and bids[atm_idx] is not None else None
    ask = float(asks[atm_idx]) if asks and asks[atm_idx] is not None else None
    symbol = symbols[atm_idx] if symbols else f"{ticker} P{strike}"

    if not mid or mid <= 0:
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        else:
            log.info(f"Crisis put: no valid mid price for {ticker} {strike}P")
            return False

    target_price = round(mid * (1 + profit_target), 2)
    exit_date = (now + timedelta(days=max_hold_days)).strftime("%Y-%m-%d")
    premium_pct = (mid / spot) * 100

    from trading_rules import (
        CRISIS_PUT_AUTO_EXECUTE, CRISIS_PUT_INITIAL_CONTRACTS,
        CRISIS_PUT_SCALEIN_DROP_PCT, CRISIS_PUT_SCALEIN_MAX_DAYS,
        CRISIS_PUT_SCALE2_PCT, CRISIS_PUT_TRAIL_GIVEBACK,
    )
    is_paper = not CRISIS_PUT_AUTO_EXECUTE
    scale2_price = round(mid * (1 + CRISIS_PUT_SCALE2_PCT), 2)
    scalein_spot = round(spot * (1 - CRISIS_PUT_SCALEIN_DROP_PCT), 2)

    _log_crisis_put_signal(
        ticker=ticker, source_type=source_type, spot=spot, vix=vix,
        confidence=confidence, vol_regime=vol_regime,
        symbol=symbol, strike=strike, dte=best_dte,
        expiration=str(best_exp)[:10], put_cost=mid, bid=bid, ask=ask,
        target_price=target_price, exit_date=exit_date,
        premium_pct=premium_pct, today=today, is_paper=is_paper,
    )

    vix_str = f"VIX {vix:.1f}" if vix else "VIX ?"
    conf_str = f"conf {confidence:.0f}" if confidence else ""
    spread_str = f"${bid:.2f}/${ask:.2f}" if bid and ask else ""

    alert = (
        f"🔴 CRISIS PUT — {ticker} (Tranche 1/3)\n"
        f"\n"
        f"📋 Contract: {symbol}\n"
        f"   Strike: ${strike:.2f} | DTE: {best_dte}\n"
        f"   Exp: {str(best_exp)[:10]}\n"
        f"\n"
        f"💰 BUY {CRISIS_PUT_INITIAL_CONTRACTS} contract @ ${mid:.2f} mid {spread_str}\n"
        f"   Premium: {premium_pct:.1f}% of spot (${spot:.2f})\n"
        f"\n"
        f"📐 SCALE PLAN:\n"
        f"   Add 1 contract if {ticker} drops to ${scalein_spot:.2f} ({CRISIS_PUT_SCALEIN_DROP_PCT*100:.0f}%↓) within {CRISIS_PUT_SCALEIN_MAX_DAYS}d\n"
        f"   Sell 1/3 @ ${target_price:.2f} (+{profit_target*100:.0f}%)\n"
        f"   Sell 1/3 @ ${scale2_price:.2f} (+{CRISIS_PUT_SCALE2_PCT*100:.0f}%)\n"
        f"   Trail last 1/3 w/ {CRISIS_PUT_TRAIL_GIVEBACK*100:.0f}% giveback\n"
        f"   Hard exit: {exit_date} (day {max_hold_days})\n"
        f"\n"
        f"📊 {vix_str} | {source_type} | {conf_str}"
    )

    try:
        post_to_telegram(alert)
        log.info(f"Crisis put alert sent: {ticker} {symbol} ${mid:.2f}")
    except Exception as e:
        log.warning(f"Crisis put Telegram alert failed for {ticker}: {e}")

    # Store position in Redis with institutional scaling state
    _crisis_put_store_position(
        ticker=ticker, symbol=symbol, strike=strike,
        expiration=str(best_exp)[:10], dte=best_dte,
        entry_price=mid, target_price=target_price,
        exit_by_date=exit_date, spot=spot,
        source_type=source_type, is_paper=is_paper,
        scalein_spot=scalein_spot,
    )
    return True


def _log_crisis_put_signal(ticker, source_type, spot, vix, confidence,
                           vol_regime, symbol, strike, dte, expiration,
                           put_cost, bid, ask, target_price, exit_date,
                           premium_pct, today, is_paper=True):
    """Log a crisis put recommendation to Google Sheets + CSV."""
    try:
        try:
            import pytz
            ct = pytz.timezone("America/Chicago")
            now_ct = datetime.now(ct)
        except ImportError:
            now_ct = datetime.now(timezone.utc) - timedelta(hours=5)
        row = {
            "signal_date": today,
            "signal_time_ct": now_ct.strftime("%H:%M"),
            "source_type": source_type,
            "ticker": ticker,
            "signal_spot": round(spot, 2) if spot else None,
            "bias": "bear",
            "vol_regime": (vol_regime or {}).get("label") if isinstance(vol_regime, dict) else None,
            "vix": round(vix, 1) if vix else None,
            "confidence": round(confidence, 1) if confidence else None,
            "contract": symbol,
            "strike": strike,
            "dte_at_entry": dte,
            "expiration": expiration,
            "put_cost_mid": round(put_cost, 2) if put_cost else None,
            "put_bid": round(bid, 2) if bid else None,
            "put_ask": round(ask, 2) if ask else None,
            "target_price_30pct": round(target_price, 2) if target_price else None,
            "exit_by_date": exit_date,
            "premium_pct_of_spot": round(premium_pct, 2) if premium_pct else None,
            "status": "paper" if is_paper else "recommended",
            "exit_date": None,
            "exit_reason": None,
            "exit_put_price": None,
            "pnl_dollars": None,
            "pnl_pct": None,
        }
        fieldnames = list(row.keys())
        _append_csv_row("crisis_put_signals.csv", fieldnames, row)
        _append_jsonl("crisis_put_signals.jsonl", row)
        log.info(f"Crisis put signal logged: {ticker} {symbol}")
    except Exception as e:
        log.warning(f"Crisis put signal log failed for {ticker}: {e}")


# ─────────────────────────────────────────────────────────
# CRISIS PUT POSITION TRACKER (v5.1.1)
# ─────────────────────────────────────────────────────────
_CRISIS_PUT_REDIS_PREFIX = "crisis_put:open:"
_CRISIS_PUT_REDIS_TTL = 8 * 86400  # auto-expire after 8 days

def _crisis_put_store_position(ticker, symbol, strike, expiration, dte,
                                entry_price, target_price, exit_by_date, spot,
                                source_type="", is_paper=True, scalein_spot=0):
    """Store an open crisis put position in Redis for monitoring."""
    r = _get_redis()
    if not r:
        log.warning("Crisis put: Redis unavailable — position not tracked")
        return
    entry_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{_CRISIS_PUT_REDIS_PREFIX}{ticker.upper()}:{entry_date}"
    pos = {
        "ticker": ticker.upper(),
        "symbol": symbol,
        "strike": strike,
        "expiration": expiration,
        "dte_at_entry": dte,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "target_price": target_price,
        "exit_by_date": exit_by_date,
        "entry_spot": spot,
        "source_type": source_type,
        "is_paper": is_paper,
        "status": "open",
        # Institutional scaling state
        "phase": "TRANCHE_1",
        "scalein_spot": scalein_spot,
        "scalein_done": False,
        "scalein_alerted": False,
        "scale1_done": False,
        "scale2_done": False,
        "contracts_held": 1,
        "peak_put_price": entry_price,
    }
    r.set(key, json.dumps(pos), ex=_CRISIS_PUT_REDIS_TTL)
    log.info(f"Crisis put position stored: {ticker} {symbol} @ ${entry_price:.2f}")


def _crisis_put_get_open_positions() -> list:
    """Retrieve all open crisis put positions from Redis."""
    r = _get_redis()
    if not r:
        return []
    positions = []
    try:
        for key in r.scan_iter(f"{_CRISIS_PUT_REDIS_PREFIX}*"):
            raw = r.get(key)
            if raw:
                pos = json.loads(raw)
                if pos.get("status") == "open":
                    positions.append(pos)
    except Exception as e:
        log.warning(f"Crisis put position scan failed: {e}")
    return positions


def _crisis_put_monitor():
    """Institutional position monitor: scale-in, scale-out in thirds, trail.
    Called every 10 minutes during market hours by the EM scheduler.

    Phases: TRANCHE_1 -> SCALED_IN -> SCALE_1 -> SCALE_2 -> TRAIL
    """
    from trading_rules import (
        CRISIS_PUT_MAX_HOLD_DAYS, CRISIS_PUT_SCALEIN_DROP_PCT,
        CRISIS_PUT_SCALEIN_MAX_DAYS, CRISIS_PUT_SCALE1_PCT,
        CRISIS_PUT_SCALE2_PCT, CRISIS_PUT_TRAIL_GIVEBACK,
    )

    positions = _crisis_put_get_open_positions()
    if not positions:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for pos in positions:
        ticker = pos["ticker"]
        try:
            entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
            now_date = datetime.now(timezone.utc).date()
            days_held = (now_date - entry_date).days
            entry_price = pos["entry_price"]

            # -- Hard exit: day 7 max hold --
            if days_held >= CRISIS_PUT_MAX_HOLD_DAYS or today >= pos["exit_by_date"]:
                _crisis_put_exit(pos, reason="max_hold", days_held=days_held)
                continue

            # -- Get current put price --
            current_price = _crisis_put_get_current_price(pos)
            if current_price is None:
                continue

            pnl_pct = (current_price - entry_price) / entry_price * 100

            # Track peak put price for trailing stop
            peak = pos.get("peak_put_price", entry_price)
            if current_price > peak:
                peak = current_price
                pos["peak_put_price"] = peak
                _crisis_put_update_redis(pos)

            # -- Phase: TRANCHE_1 -- watch for scale-in opportunity --
            if not pos.get("scalein_done") and not pos.get("scalein_alerted"):
                if days_held <= CRISIS_PUT_SCALEIN_MAX_DAYS:
                    current_spot = _crisis_put_get_spot(ticker)
                    scalein_spot = pos.get("scalein_spot", 0)
                    if current_spot and scalein_spot and current_spot <= scalein_spot:
                        pos["scalein_alerted"] = True
                        _crisis_put_update_redis(pos)
                        drop_pct = (pos["entry_spot"] - current_spot) / pos["entry_spot"] * 100
                        alert = (
                            f"\U0001f7e1 CRISIS PUT SCALE-IN \u2014 {ticker} (Tranche 2/3)\n"
                            f"\n"
                            f"\U0001f4cb {pos['symbol']}\n"
                            f"   Stock dropped {drop_pct:.1f}% to ${current_spot:.2f}\n"
                            f"\n"
                            f"\U0001f4b0 ADD 1 contract @ current mid ~${current_price:.2f}\n"
                            f"   Original entry: ${entry_price:.2f} (now {pnl_pct:+.1f}%)\n"
                            f"\n"
                            f"\U0001f4d0 Thesis confirming \u2014 stock moving in your direction"
                        )
                        try:
                            post_to_telegram(alert)
                        except Exception:
                            pass
                elif days_held > CRISIS_PUT_SCALEIN_MAX_DAYS:
                    pos["scalein_done"] = True
                    _crisis_put_update_redis(pos)

            # -- Scale-out 1: sell 1/3 at +30% --
            if not pos.get("scale1_done") and pnl_pct >= CRISIS_PUT_SCALE1_PCT * 100:
                pos["scale1_done"] = True
                pos["phase"] = "SCALE_1"
                contracts = pos.get("contracts_held", 1)
                sell_qty = max(1, contracts // 3) if contracts > 1 else 0
                if sell_qty > 0:
                    pos["contracts_held"] = contracts - sell_qty
                _crisis_put_update_redis(pos)
                sell_msg = f"SELL {sell_qty} contract \u2014 lock in profit" if sell_qty > 0 else "+30% HIT \u2014 take partial profit"
                s2_price = entry_price * (1 + CRISIS_PUT_SCALE2_PCT)
                alert = (
                    f"\U0001f4b0 CRISIS PUT SCALE 1/3 \u2014 {ticker}\n"
                    f"\n"
                    f"\U0001f4cb {pos['symbol']}\n"
                    f"   Premium: ${entry_price:.2f} \u2192 ${current_price:.2f} (+{pnl_pct:.0f}%)\n"
                    f"\n"
                    f"\U0001f514 {sell_msg}\n"
                    f"   {pos.get('contracts_held', 1)} contract(s) remaining \u2014 let them run\n"
                    f"   Next target: +{CRISIS_PUT_SCALE2_PCT*100:.0f}% (${s2_price:.2f})"
                )
                try:
                    post_to_telegram(alert)
                except Exception:
                    pass

            # -- Scale-out 2: sell 1/3 at +60% --
            elif pos.get("scale1_done") and not pos.get("scale2_done") and pnl_pct >= CRISIS_PUT_SCALE2_PCT * 100:
                pos["scale2_done"] = True
                pos["phase"] = "SCALE_2"
                contracts = pos.get("contracts_held", 1)
                sell_qty = max(1, contracts // 2) if contracts > 1 else 0
                if sell_qty > 0:
                    pos["contracts_held"] = contracts - sell_qty
                _crisis_put_update_redis(pos)
                sell_msg = f"SELL {sell_qty} contract \u2014 premium nearly doubled" if sell_qty > 0 else "+60% HIT \u2014 take more profit"
                alert = (
                    f"\U0001f4b0\U0001f4b0 CRISIS PUT SCALE 2/3 \u2014 {ticker}\n"
                    f"\n"
                    f"\U0001f4cb {pos['symbol']}\n"
                    f"   Premium: ${entry_price:.2f} \u2192 ${current_price:.2f} (+{pnl_pct:.0f}%)\n"
                    f"\n"
                    f"\U0001f514 {sell_msg}\n"
                    f"   {pos.get('contracts_held', 1)} contract(s) remaining\n"
                    f"   Trailing with {CRISIS_PUT_TRAIL_GIVEBACK*100:.0f}% giveback from peak"
                )
                try:
                    post_to_telegram(alert)
                except Exception:
                    pass

            # -- Trail last contracts: exit on 25% giveback from peak --
            elif pos.get("scale2_done"):
                pos["phase"] = "TRAIL"
                if peak > entry_price and current_price < peak:
                    giveback = (peak - current_price) / (peak - entry_price)
                    if giveback >= CRISIS_PUT_TRAIL_GIVEBACK:
                        _crisis_put_exit(pos, reason="trail_giveback", days_held=days_held,
                                         exit_price=current_price, pnl_pct=pnl_pct)
                        continue

            # -- Day before max hold warning --
            if days_held >= CRISIS_PUT_MAX_HOLD_DAYS - 1:
                log.info(f"Crisis put: {ticker} day {days_held}, P&L {pnl_pct:+.1f}% \u2014 exiting tomorrow")

        except Exception as e:
            log.warning(f"Crisis put monitor error for {ticker}: {e}")


def _crisis_put_get_current_price(pos) -> float | None:
    """Fetch the current mid price for an open crisis put position."""
    try:
        ticker = pos["ticker"]
        expiration = pos["expiration"]
        strike = pos["strike"]

        chain_data = _cached_md.get_chain(ticker, expiration, side="put")
        if not chain_data or chain_data.get("s") != "ok":
            return None

        strikes = chain_data.get("strike", [])
        mids = chain_data.get("mid", [])
        bids = chain_data.get("bid", [])
        asks = chain_data.get("ask", [])

        for i, s in enumerate(strikes):
            if abs(float(s) - strike) < 0.01:
                mid = float(mids[i]) if mids and mids[i] is not None else None
                if not mid or mid <= 0:
                    bid = float(bids[i]) if bids and bids[i] is not None else 0
                    ask = float(asks[i]) if asks and asks[i] is not None else 0
                    if bid > 0 and ask > 0:
                        mid = (bid + ask) / 2
                return mid
        return None
    except Exception as e:
        log.warning(f"Crisis put price fetch failed for {pos.get('ticker')}: {e}")
        return None


def _crisis_put_get_spot(ticker: str) -> float | None:
    """Get current spot price for scale-in check."""
    try:
        return get_spot(ticker)
    except Exception:
        return None


def _crisis_put_update_redis(pos):
    """Update an open position state in Redis."""
    r = _get_redis()
    if not r:
        return
    key = f"{_CRISIS_PUT_REDIS_PREFIX}{pos['ticker']}:{pos['entry_date']}"
    r.set(key, json.dumps(pos), ex=_CRISIS_PUT_REDIS_TTL)


def _crisis_put_exit(pos, reason, days_held, exit_price=None, pnl_pct=None):
    """Close a crisis put position \u2014 alert, log to sheets, remove from Redis."""
    ticker = pos["ticker"]
    entry_price = pos["entry_price"]
    contracts = pos.get("contracts_held", 1)

    if exit_price is None:
        exit_price = _crisis_put_get_current_price(pos)

    if exit_price is not None and entry_price:
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        pnl_dollars = (exit_price - entry_price) * 100 * contracts
    else:
        pnl_pct = 0.0
        pnl_dollars = 0.0

    exit_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        row = {
            "signal_date": pos["entry_date"],
            "signal_time_ct": "",
            "source_type": pos.get("source_type", ""),
            "ticker": ticker,
            "signal_spot": pos.get("entry_spot"),
            "bias": "bear",
            "vol_regime": "CRISIS",
            "vix": None,
            "confidence": None,
            "contract": pos["symbol"],
            "strike": pos["strike"],
            "dte_at_entry": pos.get("dte_at_entry"),
            "expiration": pos["expiration"],
            "put_cost_mid": entry_price,
            "put_bid": None,
            "put_ask": None,
            "target_price_30pct": pos["target_price"],
            "exit_by_date": pos["exit_by_date"],
            "premium_pct_of_spot": None,
            "status": f"closed_{reason}",
            "exit_date": exit_date,
            "exit_reason": reason,
            "exit_put_price": round(exit_price, 2) if exit_price is not None else None,
            "pnl_dollars": round(pnl_dollars, 2),
            "pnl_pct": round(pnl_pct, 1),
        }
        fieldnames = list(row.keys())
        _append_csv_row("crisis_put_signals.csv", fieldnames, row)
    except Exception as e:
        log.warning(f"Crisis put exit log failed for {ticker}: {e}")

    reason_labels = {
        "max_hold": "DAY 7 MAX HOLD",
        "target_hit": "TARGET HIT",
        "trail_giveback": "TRAIL GIVEBACK EXIT",
    }
    emoji = "\u2705" if pnl_pct > 0 else "\u274c"
    exit_price_str = f"${exit_price:.2f}" if exit_price is not None else "N/A (market closed)"
    pnl_dollar_str = f"${pnl_dollars:+,.0f}" if exit_price is not None else "N/A"
    phase = pos.get("phase", "TRANCHE_1")

    alert = (
        f"{emoji} CRISIS PUT EXIT \u2014 {ticker}\n"
        f"\n"
        f"\U0001f4cb {pos['symbol']}\n"
        f"   {reason_labels.get(reason, reason.upper())} (day {days_held})\n"
        f"   Phase: {phase} | {contracts} contract(s)\n"
        f"\n"
        f"\U0001f4b0 Entry: ${entry_price:.2f} \u2192 Exit: {exit_price_str}\n"
        f"   P&L: {pnl_pct:+.1f}% ({pnl_dollar_str} on {contracts} contract(s))\n"
        f"\n"
        f"\u26a0\ufe0f Close remaining position manually"
    )
    try:
        post_to_telegram(alert)
    except Exception as e:
        log.warning(f"Crisis put exit alert failed for {ticker}: {e}")

    r = _get_redis()
    if r:
        r.delete(f"{_CRISIS_PUT_REDIS_PREFIX}{ticker}:{pos['entry_date']}")
    log.info(f"Crisis put closed: {ticker} {reason} P&L {pnl_pct:+.1f}% ({contracts} contracts)")


def _log_signal_dataset_event(ticker: str, webhook_data: dict, outcome: str, reason: str = "", best_rec: dict = None,
                              signal_validation: dict = None, regime: dict = None, v4_flow: dict = None,
                              spot: float = None, expirations_checked: int = None, vol_regime: dict = None):
    try:
        wd = dict(webhook_data or {})
        best_rec = dict(best_rec or {})
        trade = (best_rec or {}).get("trade", {}) if best_rec else {}
        requested_bias = wd.get("requested_bias") or wd.get("bias")
        evaluated_bias = (best_rec or {}).get("direction") or wd.get("evaluated_bias") or wd.get("bias")
        rejection_bucket = (
            best_rec.get("rejection_bucket")
            or best_rec.get("structure_rejection_bucket")
            or _um_classify_rejection_bucket(reason or best_rec.get("reason", ""))
        )
        scoreable = best_rec.get("scoreable")
        if scoreable is None:
            scoreable = wd.get("manual_scoreable")
        matched_requested_direction = best_rec.get("matched_requested_direction")
        if matched_requested_direction is None and requested_bias and evaluated_bias and requested_bias != "both":
            matched_requested_direction = str(requested_bias).lower() == str(evaluated_bias).lower()
        _ts_utc = datetime.now(timezone.utc).isoformat()
        row = {
            "ts_utc": _ts_utc,
            "event": "signal_decision",
            "ticker": ticker,
            "mode": wd.get("type") or "scalp",
            "source": wd.get("source") or "tv",
            "source_mode": f"{wd.get('source') or 'tv'}:{wd.get('type') or 'scalp'}",
            "bias": wd.get("bias"),
            "requested_bias": requested_bias,
            "evaluated_bias": evaluated_bias,
            "tier": wd.get("tier"),
            "outcome": outcome,
            "reason": (reason or "")[:300],
            "rejection_bucket": rejection_bucket,
            "candidate_scoreable": scoreable,
            "matched_requested_direction": matched_requested_direction,
            "signal_time": wd.get("time"),
            "signal_price": wd.get("close"),
            "live_spot": spot if spot is not None else wd.get("live_spot"),
            "signal_age_sec": (signal_validation or {}).get("signal_age_sec"),
            "drift_pct": (signal_validation or {}).get("drift_pct"),
            "validation_ok": (signal_validation or {}).get("ok"),
            "validation_penalty": (signal_validation or {}).get("confidence_penalty"),
            "recheck_attempt": wd.get("recheck_attempt"),
            "is_recheck": bool(wd.get("is_recheck")),
            "recheck_age_allowed": (signal_validation or {}).get("recheck_age_allowed"),
            "entry_trigger_kind": (best_rec or {}).get("entry_trigger_kind"),
            "entry_trigger_price": (best_rec or {}).get("entry_trigger_price"),
            "entry_trigger_confirmed": (best_rec or {}).get("entry_trigger_confirmed"),
            "final_gate_ok": (best_rec or {}).get("final_gate_ok"),
            "final_gate_reason": (best_rec or {}).get("final_gate_reason"),
            "expiration": (best_rec or {}).get("exp"),
            "dte": (best_rec or {}).get("dte"),
            "long_strike": trade.get("long"),
            "short_strike": trade.get("short"),
            "debit": trade.get("debit"),
            "width": trade.get("width"),
            "ror": trade.get("ror"),
            "win_prob": trade.get("win_prob"),
            "ev_per_contract": trade.get("ev_per_contract") or trade.get("expected_value"),
            "confidence": (best_rec or {}).get("confidence"),
            "confidence_pre_validation": (best_rec or {}).get("confidence_pre_validation"),
            "confidence_pre_structure": (best_rec or {}).get("confidence_pre_structure"),
            "confidence_pre_vol_regime": (best_rec or {}).get("confidence_pre_vol_regime"),
            "contracts": (best_rec or {}).get("contracts"),
            "expirations_checked": expirations_checked,
            "regime": (regime or {}).get("regime") if isinstance(regime, dict) else None,
            "vix": (regime or {}).get("vix") if isinstance(regime, dict) else None,
            "adx": (regime or {}).get("adx") if isinstance(regime, dict) else None,
            "dealer_regime": ((best_rec or {}).get("shared_model_snapshot") or {}).get("dealer_regime", {}).get("label") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_effective_regime": ((best_rec or {}).get("shared_model_snapshot") or {}).get("effective_regime", {}).get("label") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_horizon_label": ((best_rec or {}).get("shared_model_snapshot") or {}).get("effective_regime", {}).get("horizon_label") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_entry_allowed": ((best_rec or {}).get("shared_model_snapshot") or {}).get("effective_regime", {}).get("entry_allowed") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_trigger_required": ((best_rec or {}).get("shared_model_snapshot") or {}).get("effective_regime", {}).get("requires_trigger") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_source": ((best_rec or {}).get("shared_model_snapshot") or {}).get("dealer_regime", {}).get("source") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_flip_price": ((best_rec or {}).get("shared_model_snapshot") or {}).get("dealer_regime", {}).get("flip_price") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_spot_vs_flip": ((best_rec or {}).get("shared_model_snapshot") or {}).get("dealer_regime", {}).get("spot_vs_flip") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "dealer_max_pain": ((best_rec or {}).get("shared_model_snapshot") or {}).get("dealer_regime", {}).get("max_pain") if isinstance((best_rec or {}).get("shared_model_snapshot"), dict) else None,
            "v4_composite_regime": (v4_flow or {}).get("composite_regime") if isinstance(v4_flow, dict) else None,
            "v4_confidence_label": (v4_flow or {}).get("confidence_label") if isinstance(v4_flow, dict) else None,
            "vol_regime_label": (vol_regime or {}).get("label") if isinstance(vol_regime, dict) else None,
            "vol_regime_base": (vol_regime or {}).get("base") if isinstance(vol_regime, dict) else None,
            "vol_caution_score": (vol_regime or {}).get("caution_score") if isinstance(vol_regime, dict) else None,
            "vol_transition_warning": (vol_regime or {}).get("transition_warning") if isinstance(vol_regime, dict) else None,
            "vol_term_structure": (vol_regime or {}).get("term_structure") if isinstance(vol_regime, dict) else None,
            "vol_vvix": (vol_regime or {}).get("vvix") if isinstance(vol_regime, dict) else None,
            "vol_size_mult": (vol_regime or {}).get("size_mult") if isinstance(vol_regime, dict) else None,
            "posture": (best_rec or {}).get("posture") or ((vol_regime or {}).get("posture") if isinstance(vol_regime, dict) else None),
            "structure_overlay_score": (best_rec or {}).get("structure_overlay_score"),
            "structure_local_support": (best_rec or {}).get("structure_local_support"),
            "structure_local_resistance": (best_rec or {}).get("structure_local_resistance"),
            "structure_balance_zone_low": (best_rec or {}).get("structure_balance_zone_low"),
            "structure_balance_zone_high": (best_rec or {}).get("structure_balance_zone_high"),
            "structure_outer_bracket_low": (best_rec or {}).get("structure_outer_bracket_low"),
            "structure_outer_bracket_high": (best_rec or {}).get("structure_outer_bracket_high"),
            "structure_confluence": (best_rec or {}).get("structure_confluence"),
            "source_type": _classify_source_type(wd.get("source") or "tv", _ts_utc),
            "log_schema": "v6_effective_regime",
        }
        fieldnames = list(row.keys())
        _append_csv_row("signal_decisions.csv", fieldnames, row)
        _append_jsonl("signal_decisions.jsonl", row)

        diag = (
            f"🧪 {ticker} {str(evaluated_bias or wd.get('bias', '')).upper()} T{wd.get('tier', '?')} | {outcome}\n"
            f"spot ${row['live_spot']} | drift {row['drift_pct']}% | conf {row['confidence']}\n"
            f"bucket: {row['rejection_bucket']} | source: {row['source_type']} | reason: {row['reason'] or '—'}"
        )
        _post_diagnostic(diag)

        # v5.1.1: Evaluate for CRISIS long put recommendation
        _evaluate_crisis_put(
            ticker=ticker,
            bias=str(evaluated_bias or wd.get("bias", "")).lower(),
            source_type=row["source_type"],
            vol_regime=vol_regime,
            spot=spot if spot is not None else as_float(wd.get("live_spot")),
            vix=as_float((regime or {}).get("vix")) if isinstance(regime, dict) else None,
            confidence=as_float((best_rec or {}).get("confidence")),
        )
    except Exception as e:
        log.warning(f"Signal dataset log failed for {ticker}: {e}")


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            log.warning("Redis cached connection dead — reconnecting")
            _redis_client = None

    if not REDIS_URL:
        return None
    try:
        import redis
        _redis_client = redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_timeout=10, socket_connect_timeout=5, retry_on_timeout=True,
        )
        _redis_client.ping()
        log.info("Redis connected")
        return _redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}), using in-memory fallback")
        _redis_client = None
        return None


def store_set(key: str, value: str, ttl: int = 0):
    r = _get_redis()
    if r:
        try:
            if ttl: r.setex(key, ttl, value)
            else: r.set(key, value)
            return
        except Exception as e:
            log.warning(f"Redis set failed ({e}) — writing to mem fallback")
    _mem_store.set(key, value, ttl)


def store_get(key: str):
    r = _get_redis()
    if r:
        try: return r.get(key)
        except Exception: pass
    return _mem_store.get(key)


def store_exists(key: str) -> bool:
    r = _get_redis()
    if r:
        try: return bool(r.exists(key))
        except Exception: pass
    return _mem_store.exists(key)


def store_scan(pattern: str) -> list:
    """Scan Redis for keys matching pattern. Returns list of key strings."""
    r = _get_redis()
    if r:
        try:
            keys = []
            cursor = 0
            while True:
                cursor, batch = r.scan(cursor, match=pattern, count=100)
                keys.extend([k.decode() if isinstance(k, bytes) else k for k in batch])
                if cursor == 0:
                    break
            return keys
        except Exception as e:
            log.debug(f"Redis scan failed for {pattern}: {e}")
    return []

# ─────────────────────────────────────────────────────────
# DEDUP
# ─────────────────────────────────────────────────────────

def trade_dedup_key(ticker, direction, short_k, long_k) -> str:
    raw = f"{ticker}:{direction}:{short_k}:{long_k}"
    return "dedup:" + hashlib.md5(raw.encode()).hexdigest()

def is_duplicate_trade(ticker, direction, short_k, long_k) -> bool:
    return store_exists(trade_dedup_key(ticker, direction, short_k, long_k))

def mark_trade_sent(ticker, direction, short_k, long_k):
    store_set(trade_dedup_key(ticker, direction, short_k, long_k), "1", ttl=DEDUP_TTL_SECONDS)

# ─────────────────────────────────────────────────────────
# UNIFIED SIGNAL QUEUE — Redis-backed (v3.9)
# ─────────────────────────────────────────────────────────

QUEUE_MAX        = 80
QUEUE_WORKERS    = max(1, SCAN_WORKERS)
SIGNAL_TTL_SEC   = 480
TG_MIN_GAP_SEC   = 0.8
WAVE_COLLECT_SEC = 90

REDIS_QUEUE_KEY  = "signal_queue"

_mem_signal_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)

_tg_last_post_time: float = 0.0
_tg_lock = threading.Lock()

# ─────────────────────────────────────────────────────────
# WAVE PREFETCH CACHE — v4.3
# Pre-fetches spot, chains, candles, regime, v4 prefilter
# for all unique tickers in a signal wave BEFORE workers start.
# Workers then pull from cache instead of hitting APIs.
# ─────────────────────────────────────────────────────────

_prefetch_cache = {}
_prefetch_lock = threading.Lock()
_prefetch_ttl = 120  # seconds — data is fresh enough for 2 minutes


def _prefetch_get(ticker: str) -> dict:
    """Get prefetched data for a ticker, or None if expired/missing."""
    with _prefetch_lock:
        entry = _prefetch_cache.get(ticker)
        if entry and (time.time() - entry["ts"]) < _prefetch_ttl:
            return entry
    return None


def _prefetch_set(ticker: str, data: dict):
    """Store prefetched data for a ticker."""
    with _prefetch_lock:
        _prefetch_cache[ticker] = {**data, "ts": time.time()}


def _prefetch_ticker(ticker: str):
    """
    v5.0: Fetch data with pre-chain qualification gate.

    OLD ORDER (v4.3):
      1. Spot (1 API call)
      2. CHAINS (5 API calls) ← EXPENSIVE, done before any checks
      3. Enrichment, Candles, Regime, V4

    NEW ORDER (v5.0):
      1. Spot (1 API call)
      2. Enrichment (Finnhub, 0 MarketData calls)
      3. Candles (usually cached, 0-1 calls)
      4. Regime + vol regime (cached, 0 calls)
      5. VIX term structure (Yahoo, 0 MarketData calls)
      6. Economic calendar (Finnhub cached, 0 calls)
      7. Sector data (Yahoo cached, 0 calls)
      8. >>> PRE-CHAIN GATE DECISION <<<
      9. CHAINS (5 calls) — ONLY IF QUALIFIED
      10. V4 prefilter

    Saves ~5 API calls for every rejected signal (~60% rejection rate).
    """
    ticker = ticker.strip().upper()

    # Skip if already fresh in cache
    if _prefetch_get(ticker):
        return

    try:
        t0 = time.time()

        # 1. Spot price (1 call — always needed)
        spot = get_spot(ticker)

        # 2. Enrichment: earnings (Finnhub, 0 MarketData calls)
        enrichment = {"has_earnings": False, "earnings_warn": None}
        try:
            import threading as _threading
            _enrich_result = {}
            def _fetch_enrich():
                try:
                    _enrich_result.update(enrich_ticker(ticker))
                except Exception:
                    pass
            _et = _threading.Thread(target=_fetch_enrich, daemon=True)
            _et.start()
            _et.join(timeout=2.0)
            if _enrich_result:
                enrichment = _enrich_result
        except Exception:
            pass

        # 3. Daily candles (usually cached at 10 min TTL)
        candle_closes = get_daily_candles(ticker, days=RV_LOOKBACK_DAYS + 5)

        # 4. Regime (cached)
        regime = get_current_regime()
        vol_regime = get_canonical_vol_regime(ticker, candle_closes)

        # 5. VIX term structure (Yahoo Finance, 0 MarketData calls)
        term_structure = {}
        try:
            term_structure = get_vix_term_structure()
        except Exception as e:
            log.debug(f"Prefetch {ticker}: VIX term structure failed: {e}")

        # 6. Economic calendar (Finnhub, cached 6hrs, 0 MarketData calls)
        econ_events = []
        try:
            econ_events = get_events_in_window(dte_days=MAX_DTE)
        except Exception as e:
            log.debug(f"Prefetch {ticker}: econ calendar failed: {e}")

        # 7. Sector data (Yahoo Finance, cached 30min, 0 MarketData calls)
        sector_data = {}
        try:
            sector_data = get_sector_rank(ticker)
        except Exception as e:
            log.debug(f"Prefetch {ticker}: sector rank failed: {e}")

        # ═══ PRE-CHAIN GATE (v5.0) ═══
        # All checks above used 0-2 API calls total.
        # Chains cost 5 calls. Only pull if signal qualifies.
        if PRECHAIN_GATE_ENABLED:
            if enrichment.get("has_earnings") and NO_EARNINGS_WEEK:
                log.info(f"Prefetch {ticker}: SKIPPING CHAINS — earnings block "
                         f"(saved ~5 API calls)")
                _prefetch_set(ticker, {
                    "spot": spot, "chains": None,
                    "enrichment": enrichment, "candle_closes": candle_closes,
                    "regime": regime, "vol_regime": vol_regime,
                    "v4_flow": {}, "term_structure": term_structure,
                    "econ_events": econ_events, "sector_data": sector_data,
                    "prechain_skip": True,
                    "prechain_reason": "earnings in DTE window",
                })
                return

            vl = (vol_regime.get("label") or "").upper()
            vc = vol_regime.get("caution_score", 0)
            # v5.0 fix: DON'T skip chains in CRISIS at prefetch level.
            # Prefetch doesn't know signal direction. Bear signals are
            # valid in CRISIS and need chains. The per-signal pre-chain
            # gate in _process_job handles direction-aware blocking.
            # Only skip if caution=8 AND this is clearly not a bear day
            # (we can't know that here, so we just log and proceed).
            if vl == "CRISIS" or vc >= 6:
                log.info(f"Prefetch {ticker}: CRISIS regime "
                         f"(VIX {vol_regime.get('vix', '?')}, caution {vc}/8) "
                         f"— fetching chains anyway (bears valid in CRISIS)")

        # ═══ CHAINS (only reached if pre-chain gate passed) ═══
        chains = get_options_chain(ticker)

        # V4 prefilter (reuses chains — no extra API calls)
        v4_flow = _run_v4_prefilter(ticker, spot, chains, candle_closes)

        elapsed = time.time() - t0

        _prefetch_set(ticker, {
            "spot": spot,
            "chains": chains,
            "enrichment": enrichment,
            "candle_closes": candle_closes,
            "regime": regime,
            "vol_regime": vol_regime,
            "v4_flow": v4_flow,
            "term_structure": term_structure,
            "econ_events": econ_events,
            "sector_data": sector_data,
            "prechain_skip": False,
        })

        log.info(f"Prefetch {ticker}: {len(chains)} exps, vol={vol_regime.get('label','?')}, "
                 f"sector={sector_data.get('relative_strength','?')}, "
                 f"term={term_structure.get('term_structure','?')}, "
                 f"v4={v4_flow.get('composite_regime', '?') if v4_flow else 'N/A'}, "
                 f"{elapsed:.1f}s")

    except Exception as e:
        log.warning(f"Prefetch failed for {ticker}: {e}")
        _prefetch_set(ticker, {
            "spot": None, "chains": None, "enrichment": {},
            "candle_closes": [], "regime": {}, "vol_regime": {}, "v4_flow": {},
            "term_structure": {}, "econ_events": [], "sector_data": {},
            "error": str(e),
        })


def _prefetch_wave(tickers: list):
    """
    Pre-fetch data for all unique tickers in a signal wave.
    Uses a thread pool for parallel fetching with controlled concurrency.
    """
    unique = list(dict.fromkeys(t.strip().upper() for t in tickers))

    # Filter out tickers already in cache
    to_fetch = [t for t in unique if not _prefetch_get(t)]

    if not to_fetch:
        log.info(f"Wave prefetch: all {len(unique)} tickers already cached")
        return

    log.info(f"Wave prefetch: fetching {len(to_fetch)} tickers "
             f"({len(unique) - len(to_fetch)} already cached)")

    # Use max 4 threads to avoid API rate limits, but still parallel
    with ThreadPoolExecutor(max_workers=min(4, len(to_fetch))) as pool:
        pool.map(_prefetch_ticker, to_fetch)

    log.info(f"Wave prefetch complete: {len(to_fetch)} tickers ready")

_wave_lock         = threading.Lock()
_wave_results: list = []
_wave_last_arrival: float = 0.0


def _record_wave_result(result: dict):
    global _wave_last_arrival
    with _wave_lock:
        _wave_results.append(result)
        _wave_last_arrival = time.time()


def _flush_wave_digest():
    """
    v4.1: Compact digest with /tradecard retrieval.
    - Immediate post: T1 + conf >= 75 (or 0DTE)
    - Digest: one-liner per signal, full card cached for /tradecard TICKER
    """
    global _wave_results
    with _wave_lock:
        if not _wave_results:
            return
        results = list(_wave_results)
        _wave_results = []

    immediate_cards = []   # full cards to post now
    digest_lines = []      # compact one-liners
    pending_lines = []     # watchlist / recheck candidates
    skipped_lines = []     # rejected signals

    for r in results:
        job_type = r.get("job_type", "tv")
        ticker = r.get("ticker", "?")
        bias = r.get("bias", "bull")
        tier = r.get("tier", "?")
        conf = r.get("confidence")
        card = r.get("card")
        won = r.get("outcome") == "trade"

        dir_emoji = "🐻" if bias == "bear" else "🐂"
        conf_str = f"{conf}/100" if conf is not None else "—"
        type_label = "SWING" if job_type == "swing" else "TV"

        if (won or r.get("outcome") == "pending") and card:
            cache_key = f"tradecard:{ticker.upper()}"
            store_set(cache_key, card, ttl=DIGEST_CARD_CACHE_TTL_SEC)

        if won and card:
            is_immediate = (
                (tier in IMMEDIATE_POST_TIER and conf is not None and conf >= IMMEDIATE_POST_MIN_CONF)
            )

            if is_immediate:
                immediate_cards.append(card)
                digest_lines.append(f"  ✅ {ticker} T{tier} {dir_emoji} {conf_str} — POSTED ⬆️")
            else:
                digest_lines.append(f"  📋 {ticker} T{tier} {dir_emoji} {conf_str} — /tradecard {ticker}")
        elif r.get("outcome") == "pending":
            reason = r.get("reason", "waiting on recheck")[:50]
            pending_lines.append(f"  ⏳ {ticker} T{tier} {dir_emoji} {conf_str} — {reason}")
        else:
            reason = r.get("reason", "no setup")[:60]
            skipped_lines.append(f"  ❌ {ticker} T{tier} {dir_emoji} {conf_str} — {reason}")

    lines = []
    if digest_lines or pending_lines or skipped_lines:
        lines.append(f"📊 SIGNAL DIGEST ({len(digest_lines)} trades, {len(pending_lines)} pending, {len(skipped_lines)} skipped)")
        lines.append("")
        if digest_lines:
            lines.append("── Trades ──")
            lines.extend(digest_lines)
        if pending_lines:
            lines.append("")
            lines.append("── Pending / Recheck ──")
            lines.extend(pending_lines)
        if skipped_lines:
            lines.append("")
            lines.append("── Skipped ──")
            lines.extend(skipped_lines)
        lines.append("")
        lines.append("💡 Use /tradecard TICKER for full card")

    if not lines and not immediate_cards:
        return

    log.info(f"Wave digest: {len(immediate_cards)} immediate, "
             f"{len(digest_lines)} digest, {len(skipped_lines)} skipped")

    if lines:
        _tg_rate_limited_post("\n".join(lines))

    for card_text in immediate_cards:
        _tg_rate_limited_post(card_text)


def _digest_poster_thread():
    log.info("Wave digest poster started")
    while True:
        time.sleep(5)
        with _wave_lock:
            has_results  = bool(_wave_results)
            last_arrival = _wave_last_arrival
        if has_results and (time.time() - last_arrival) >= WAVE_COLLECT_SEC:
            try:
                _flush_wave_digest()
            except Exception as e:
                log.error(f"Digest flush error: {e}", exc_info=True)




def _tg_rate_limited_post(text: str, max_retries: int = 6, chat_id: str = None):
    global _tg_last_post_time
    with _tg_lock:
        now = time.time()
        gap = now - _tg_last_post_time
        if gap < TG_MIN_GAP_SEC:
            time.sleep(TG_MIN_GAP_SEC - gap)
        _tg_last_post_time = time.time()
    return post_to_telegram(text, max_retries=max_retries, chat_id=chat_id)


def _enqueue_signal(job_type: str, ticker: str, bias: str,
                    webhook_data: dict, signal_msg: str):
    job = {
        "job_type": job_type, "ticker": ticker, "bias": bias,
        "webhook_data": webhook_data, "signal_msg": signal_msg,
        "enqueued_at": time.time(),
    }

    # v5.0: Skip prefetch for stale signals — the worker's pre-chain gate
    # will reject them anyway, so don't waste 5 chain API calls.
    _received_at = webhook_data.get("received_at_epoch", 0)
    _signal_age = time.time() - _received_at if _received_at else 0
    if _signal_age < 300:  # only prefetch for fresh signals (< 5 min)
        _record_prefetch_ticker(ticker)
    else:
        log.debug(f"Skipping prefetch for {ticker}: signal age {_signal_age:.0f}s (stale)")

    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            pipe.lpush(REDIS_QUEUE_KEY, json.dumps(job))
            pipe.ltrim(REDIS_QUEUE_KEY, 0, QUEUE_MAX - 1)
            pipe.execute()
            qsize = r.llen(REDIS_QUEUE_KEY)
            log.info(f"{job_type.upper()} signal pushed to Redis: {ticker} (depth: {qsize})")
            if qsize > QUEUE_MAX * 0.7:
                log.warning(f"Redis signal queue at {qsize}/{QUEUE_MAX}")
            return
        except Exception as e:
            log.warning(f"Redis enqueue failed ({e}), falling back to memory queue")

    mem_job = (job_type, ticker, bias, webhook_data, signal_msg, job["enqueued_at"])
    try:
        _mem_signal_queue.put_nowait(mem_job)
        log.info(f"{job_type.upper()} signal queued (memory fallback): {ticker}")
    except queue.Full:
        try:
            dropped = _mem_signal_queue.get_nowait()
            log.warning(f"Memory queue full — dropped: {dropped[1]} ({dropped[0]})")
            _mem_signal_queue.task_done()
        except queue.Empty:
            pass
        try:
            _mem_signal_queue.put_nowait(mem_job)
        except queue.Full:
            log.error(f"Memory queue still full — signal lost: {ticker} ({job_type})")



def _pending_recheck_delays() -> list[int]:
    out = []
    raw = str(PENDING_RECHECK_DELAYS_SEC or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(30, int(float(part))))
        except Exception:
            continue
    return out or [300, 900, 1800]


def _schedule_pending_recheck(ticker: str, bias: str, webhook_data: dict, reason: str = "") -> tuple[bool, int | None, int, int]:
    if not PENDING_RECHECK_ENABLE:
        return False, None, 0, 0

    wd = dict(webhook_data or {})
    if str(wd.get("source") or "").lower() == "check":
        return False, None, int(wd.get("recheck_attempt") or 0), 0

    delays = _pending_recheck_delays()
    attempt = int(wd.get("recheck_attempt") or 0)
    total = len(delays)
    if attempt >= total:
        return False, None, attempt, total

    received_at = str(wd.get("received_at_epoch") or "na")
    dedup_key = f"pending_recheck:{ticker}:{bias}:{received_at}:{attempt+1}"
    if store_exists(dedup_key):
        return False, delays[attempt], attempt, total
    store_set(dedup_key, "1", ttl=max(delays[attempt] + 3600, 7200))

    next_wd = dict(wd)
    next_wd["recheck_attempt"] = attempt + 1
    next_wd["allow_recheck_after_stale"] = True
    next_wd["pending_reason"] = (reason or wd.get("pending_reason") or "")[:200]
    next_wd["is_recheck"] = True

    def _fire():
        try:
            time.sleep(delays[attempt])
            signal_msg = f"Pending recheck {attempt+1}/{total}: {ticker} {bias.upper()}"
            _enqueue_signal("tv", ticker, bias, next_wd, signal_msg)
            log.info(f"Pending recheck enqueued: {ticker} {bias} attempt {attempt+1}/{total} after {delays[attempt]}s")
        except Exception as e:
            log.warning(f"Pending recheck failed for {ticker}: {e}")

    threading.Thread(target=_fire, daemon=True, name=f"pending-recheck-{ticker}-{attempt+1}").start()
    return True, delays[attempt], attempt, total

# ─────────────────────────────────────────────────────────
# PREFETCH COLLECTOR — v4.3
# Collects tickers from incoming signal burst, then prefetches
# all data in parallel before workers start processing.
# ─────────────────────────────────────────────────────────

_prefetch_pending_lock = threading.Lock()
_prefetch_pending: list = []
_prefetch_last_signal: float = 0.0
_prefetch_thread_active = False
_prefetch_wave_done = threading.Event()  # Workers wait on this
_prefetch_wave_done.set()  # Start in "done" state (no wave pending)
PREFETCH_SETTLE_SEC = 3  # wait this long after last signal before prefetching
PREFETCH_WORKER_WAIT_SEC = 80  # max time workers wait for prefetch (prefetch takes 50-85s on cold chains)


def _record_prefetch_ticker(ticker: str):
    """Called from _enqueue_signal to track tickers needing prefetch."""
    global _prefetch_last_signal
    with _prefetch_pending_lock:
        _prefetch_pending.append(ticker.strip().upper())
        _prefetch_last_signal = time.time()
        _prefetch_wave_done.clear()  # Signal workers to wait
        _maybe_start_prefetch_thread()


def _maybe_start_prefetch_thread():
    """Start the prefetch thread if not already running."""
    global _prefetch_thread_active
    if _prefetch_thread_active:
        return
    _prefetch_thread_active = True
    threading.Thread(target=_prefetch_collector_thread, daemon=True, name="wave-prefetch").start()


def _prefetch_collector_thread():
    """
    Wait for signal burst to settle, then prefetch all unique tickers.
    This runs once per burst, then exits.
    """
    global _prefetch_thread_active
    try:
        # Wait for signals to stop arriving
        while True:
            time.sleep(1)
            with _prefetch_pending_lock:
                elapsed = time.time() - _prefetch_last_signal
                if elapsed >= PREFETCH_SETTLE_SEC and _prefetch_pending:
                    tickers = list(_prefetch_pending)
                    _prefetch_pending.clear()
                    break
                elif not _prefetch_pending:
                    # No signals pending
                    _prefetch_wave_done.set()
                    return

        # Dedupe and prefetch
        unique = list(dict.fromkeys(tickers))
        log.info(f"Wave prefetch triggered: {len(unique)} unique tickers from {len(tickers)} signals")
        _prefetch_wave(unique)

    except Exception as e:
        log.error(f"Prefetch collector error: {e}", exc_info=True)
    finally:
        _prefetch_wave_done.set()  # Unblock workers even on error
        _prefetch_thread_active = False
        log.info("Prefetch wave complete — workers unblocked")


def _process_job(worker_id: int, job: dict):
    job_type     = job["job_type"]
    ticker       = job["ticker"]
    bias         = job["bias"]
    webhook_data = job["webhook_data"]
    enqueued_at  = job["enqueued_at"]
    tier_label   = webhook_data.get("tier", "?")

    age_sec = time.time() - enqueued_at
    if age_sec > SIGNAL_TTL_SEC:
        log.warning(f"[worker-{worker_id}] Stale {job_type} signal dropped: {ticker} ({age_sec:.0f}s)")
        return

    log.info(f"[worker-{worker_id}] Processing {job_type}: {ticker} {bias} T{tier_label} ({age_sec:.0f}s ago)")

    base = {
        "job_type": job_type, "ticker": ticker, "bias": bias,
        "tier": tier_label, "outcome": "skip", "card": None,
        "confidence": None, "reason": "",
    }

    # ── v4.3: VOL REGIME EARLY GATE ──────────────────────────────────
    # Check cached vol regime BEFORE any chain fetches or API calls.
    # If CRISIS, almost nothing passes downstream gates anyway.
    # Saves 500-800+ API calls per day on heavy signal days.
    # VIX is cached from CBOE (0 extra API calls). Daily candles are
    # cached with 10-min TTL. Total cost: 0-1 API calls.
    # ─────────────────────────────────────────────────────────────────
    try:
        _early_vol = get_canonical_vol_regime(ticker, get_daily_candles(ticker, days=30))
        _early_label = (_early_vol or {}).get("label", "")
        _early_caution = (_early_vol or {}).get("caution_score", 0)
        _early_vix = (_early_vol or {}).get("vix", 0)

        if _early_label == "CRISIS" or _early_caution >= 6:
            # v5.0: Direction-aware CRISIS gate.
            # Bears in CRISIS = correct trade direction. Allow through.
            # Bulls in CRISIS = fighting the market. Block.
            if job_type == "swing" and bias == "bull":
                reason = f"🚨 VIX Crisis Regime (VIX {_early_vix:.1f}, caution {_early_caution}/8) — bull swing blocked"
                base["reason"] = reason
                log.info(f"[worker-{worker_id}] {ticker} {job_type} EARLY BLOCK: {reason}")
                _record_wave_result(base)
                return

            if job_type == "swing" and bias == "bear":
                log.info(f"[worker-{worker_id}] {ticker} CRISIS regime but bear swing — allowing through "
                         f"(VIX {_early_vix:.1f}, caution {_early_caution}/8)")

            # Manual swing (/checkswing BOTH): allow through with warning
            if job_type == "swing" and bias not in ("bull", "bear"):
                log.info(f"[worker-{worker_id}] {ticker} CRISIS regime, manual swing check — allowing through")

            # TV scalp: block bull calls in CRISIS (risk_manager blocks anyway)
            if job_type == "tv" and bias == "bull":
                reason = f"🚨 VIX Crisis Regime (VIX {_early_vix:.1f}) — bull calls blocked before chain fetch"
                base["reason"] = reason
                log.info(f"[worker-{worker_id}] {ticker} {job_type} EARLY BLOCK: {reason}")
                _record_wave_result(base)
                return

            # TV bear puts in CRISIS: allow through (bears can work) but log
            if job_type == "tv":
                log.info(f"[worker-{worker_id}] {ticker} CRISIS regime but bear signal — allowing through")
    except Exception as e:
        log.debug(f"[worker-{worker_id}] Vol regime early gate skipped for {ticker}: {e}")

    if job_type == "tv":
        check_spread_exit_warning(ticker, bias, webhook_data)

        if bias not in ALLOWED_DIRECTIONS:
            base["reason"] = f"{bias} not in allowed directions"
            _record_wave_result(base)
            return

        # ── v4.1: CAGF regime gate for liquid index tickers ──
        # Pine = timing trigger, Python CAGF = regime context
        # Only gate SPY/QQQ/SPX — other tickers skip this check
        if ticker.upper() in ("SPY", "QQQ", "SPX"):
            try:
                _spot = get_spot(ticker)
                _bars = _get_ohlc_bars(ticker, days=65)
                _adv, _ = _estimate_liquidity(ticker, _spot)
                _closes = get_daily_candles(ticker, days=60)
                _vix_data = _get_vix_data()
                _vix_val = _vix_data.get("vix", 20) if _vix_data else 20

                # Run a quick v4 snapshot for dealer flows
                from datetime import date as _date
                _today = _date.today().strftime("%Y-%m-%d")
                _chain_data, _c_spot, _c_exp = _get_0dte_chain(ticker, _today)
                if _chain_data and _c_spot:
                    _oi_cache.apply_oi_changes_to_chain(ticker, _c_exp, _chain_data)
                    _v4 = run_institutional_snapshot(
                        chain_data=_chain_data, spot=_c_spot, dte=0.5,
                        recent_bars=_bars, is_0dte=True,
                        avg_daily_dollar_volume=_adv,
                        liquid_index=True,
                    )
                    if not _v4.get("error"):
                        _eng = _v4.get("engine_result", {})
                        _rv = _v4.get("vol_regime", {}).get("realized_vol_20d", 0) or 0
                        _iv = _v4.get("iv", 0) or 0

                        import pytz as _pytz
                        _ct = _pytz.timezone("America/Chicago")
                        _now_ct = datetime.now(_ct)
                        _mkt_open = _now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
                        _mkt_close = _now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
                        _sess_secs = (_mkt_close - _mkt_open).total_seconds()
                        _elapsed = max(0, (_now_ct - _mkt_open).total_seconds())
                        _sess_prog = min(1.0, _elapsed / _sess_secs) if _sess_secs > 0 else 0.5

                        _cagf = compute_cagf(
                            dealer_flows={
                                "gex": _eng.get("gex", 0), "dex": _eng.get("dex", 0),
                                "vanna": _eng.get("vanna", 0), "charm": _eng.get("charm", 0),
                                "gamma_flip": _eng.get("flip_price"),
                            },
                            iv=_iv, rv=_rv, spot=_c_spot, vix=_vix_val,
                            session_progress=_sess_prog, adv=_adv,
                            candle_closes=_closes, ticker=ticker,
                        )

                        _trend_prob = _cagf.get("trend_day_probability", 0)
                        _regime = _cagf.get("regime", "UNKNOWN")
                        _prob = _cagf.get("probability", 50)

                        # Gate: T1 requires trend_prob >= 0.55, T2 >= 0.40
                        min_trend = 0.55 if tier_label == "1" else 0.40
                        if _trend_prob < min_trend:
                            base["reason"] = (
                                f"CAGF regime gate: trend prob {_trend_prob:.0%} < {min_trend:.0%} "
                                f"({_regime}, {_prob:.0f}% directional)"
                            )
                            log.info(f"[worker-{worker_id}] {ticker} T{tier_label} blocked by CAGF: "
                                     f"trend={_trend_prob:.0%} regime={_regime} prob={_prob:.0f}%")
                            _record_wave_result(base)
                            return

                        log.info(f"[worker-{worker_id}] {ticker} CAGF passed: "
                                 f"trend={_trend_prob:.0%} regime={_regime} prob={_prob:.0f}%")
            except Exception as e:
                log.warning(f"[worker-{worker_id}] CAGF gate error for {ticker}: {e} — proceeding without gate")

        # ── v4.2: PIN regime gate (all tickers) ──
        # Uses prefetch v4_flow if available (no extra API call).
        # Blocks directional debit spreads when v4 regime contains PIN —
        # same logic check_ticker enforces via "not enough ITM strikes",
        # but surfaced here earlier to save a full timeout cycle.
        _cached_prefetch = _prefetch_get(ticker)
        if _cached_prefetch and _cached_prefetch.get("v4_flow"):
            _v4f = _cached_prefetch["v4_flow"]
            _composite = (_v4f.get("composite_regime") or "").upper()
            if "PIN" in _composite:
                if bias == "bear" and PIN_REGIME_BLOCK_BEAR_PUTS:
                    # v6.1: In a BEAR market regime, PIN days are normal consolidation
                    # before continuation — don't block bear puts. Only block in
                    # BULL/TRANSITION where PIN means price is stuck, not continuing.
                    _mkt_regime_for_pin = get_market_regime()
                    _mkt_label_for_pin = (_mkt_regime_for_pin.get("label") or "").upper()
                    if "BEAR" not in _mkt_label_for_pin:
                        base["reason"] = f"PIN regime ({_composite}) — bear puts blocked (non-BEAR market)"
                        log.info(f"[worker-{worker_id}] {ticker} blocked: {base['reason']}")
                        _record_wave_result(base)
                        return
                    else:
                        log.info(f"[worker-{worker_id}] {ticker}: PIN regime but BEAR market — allowing bear puts through")
                if bias == "bull" and PIN_REGIME_BLOCK_BULL_CALLS:
                    base["reason"] = f"PIN regime ({_composite}) — bull calls blocked"
                    log.info(f"[worker-{worker_id}] {ticker} blocked: {base['reason']}")
                    _record_wave_result(base)
                    return

        # ── v4.2: CHOP regime confidence gate (all tickers) ──
        # LOW VOL CHOP: only take trades that clear the tighter confidence threshold.
        # Regime is cheap to fetch — already cached by prefetch or get_current_regime().
        _current_regime = (_cached_prefetch.get("regime") if _cached_prefetch else None) or get_current_regime()
        _regime_label = (_current_regime.get("label") or "").upper()
        if "CHOP" in _regime_label:
            # Pre-check: if we have a prefetch confidence score, gate early
            if _cached_prefetch and _cached_prefetch.get("v4_flow"):
                _conf_score = _cached_prefetch["v4_flow"].get("confidence_score", 1.0)
                # confidence_score is 0.0–1.0; CHOP_REGIME_CONF_GATE is 0–100
                if _conf_score * 100 < CHOP_REGIME_CONF_GATE:
                    base["reason"] = (
                        f"CHOP regime gate: v4 conf {_conf_score*100:.0f}/100 "
                        f"< {CHOP_REGIME_CONF_GATE} (LOW VOL CHOP)"
                    )
                    log.info(f"[worker-{worker_id}] {ticker} blocked: {base['reason']}")
                    _record_wave_result(base)
                    return

        # ── v5.0: UNIFIED PRE-CHAIN GATE ──────────────────────────────
        # Runs all cheap checks before any chain API calls.
        # Catches signals that prefetch didn't block (e.g. webhook-specific
        # checks like signal freshness, price drift, pre-confidence).
        if PRECHAIN_GATE_ENABLED:
            _cached = _prefetch_get(ticker)

            # Check if prefetch already skipped chains
            if _cached and _cached.get("prechain_skip"):
                base["reason"] = f"Pre-chain gate: {_cached.get('prechain_reason', 'rejected')}"
                log.info(f"[worker-{worker_id}] {ticker} blocked by prefetch pre-chain gate")
                _record_wave_result(base)
                return

            # Run full pre-chain gate with webhook data
            _gate_result = should_pull_chains(
                ticker=ticker,
                bias=bias,
                webhook_data=webhook_data,
                live_spot=_cached.get("spot") if _cached else None,
                candle_closes=_cached.get("candle_closes") if _cached else None,
                regime=_cached.get("regime") if _cached else None,
                vol_regime=_cached.get("vol_regime") if _cached else None,
                enrichment=_cached.get("enrichment") if _cached else None,
                econ_events=_cached.get("econ_events") if _cached else None,
                sector_data=_cached.get("sector_data") if _cached else None,
                job_type="tv",
            )

            if not _gate_result["qualified"]:
                base["reason"] = (f"Pre-chain gate [{_gate_result['gate_failed']}]: "
                                  f"{_gate_result['reason']}")
                log.info(f"[worker-{worker_id}] {ticker} blocked by pre-chain gate: "
                         f"{_gate_result['gate_failed']} "
                         f"(saved {_gate_result['api_calls_saved']} API calls)")
                _record_wave_result(base)
                return

            log.info(f"[worker-{worker_id}] {ticker} passed pre-chain gate "
                     f"(pre-conf={_gate_result['pre_confidence']}, "
                     f"gates={','.join(_gate_result['gates_passed'])})")
        # ── end pre-chain gate ────────────────────────────────────────

        rec = check_ticker_with_timeout(ticker, direction=bias, webhook_data=webhook_data)
        base["confidence"] = rec.get("confidence")

        if rec.get("error") == "timeout":
            base["reason"] = "timeout"
            _record_wave_result(base)
            return

        if not rec.get("ok"):
            if rec.get("pending"):
                base["outcome"] = "pending"
                base["reason"] = rec.get("reason", "pending recheck")
                base["card"] = rec.get("card")
                _record_wave_result(base)
                return
            base["reason"] = rec.get("reason", "no valid spread")
            _record_wave_result(base)
            return

        if not rec.get("posted") and rec.get("reason") == "Duplicate trade in dedup window":
            base["reason"] = "duplicate"
            _record_wave_result(base)
            return

        card_text = rec.get("card")
        base["outcome"] = "trade"
        base["card"]    = card_text
        _record_wave_result(base)
        log.info(f"TV winner queued for digest: {ticker} {bias} T{tier_label} conf={rec.get('confidence')}/100")

    elif job_type == "swing":
        from swing_engine import recommend_swing_trade, format_swing_card
        try:
            spot   = get_spot(ticker)
        except Exception as e:
            base["reason"] = f"spot fetch error: {e}"
            _record_wave_result(base)
            return

        # ── v5.0: Fundamental + sector enrichment for swing ──
        _swing_fundamental_data = {}
        _swing_sector_data = {}
        if FUNDAMENTAL_SCREENING_ENABLED:
            try:
                _swing_fundamental_data = get_fundamentals(ticker)
                _swing_sector_data = get_sector_rank(ticker)

                # Apply Lynch classification confidence adjustment
                _lynch = _swing_fundamental_data.get("lynch_category", {})
                _lynch_boost = _lynch.get("confidence_boost", 0)
                if _lynch_boost != 0:
                    webhook_data["lynch_boost"] = _lynch_boost
                    webhook_data["lynch_category"] = _lynch.get("category", "UNCLASSIFIED")
                    webhook_data["peg_signal"] = _lynch.get("peg_signal", "N/A")

                _sector_adj = _swing_sector_data.get("confidence_adjustment", 0)
                if _sector_adj != 0:
                    webhook_data["sector_adjustment"] = _sector_adj
                    webhook_data["sector_strength"] = _swing_sector_data.get("relative_strength", "NEUTRAL")

                log.info(f"[worker-{worker_id}] Swing fundamentals {ticker}: "
                         f"score={_swing_fundamental_data.get('fundamental_score', '?')}/100, "
                         f"lynch={_lynch.get('category', '?')}, "
                         f"sector=#{_swing_sector_data.get('rank', '?')}")
            except Exception as e:
                log.warning(f"[worker-{worker_id}] Swing fundamental enrichment failed for {ticker}: {e}")

        # ── v5.0: Swing pre-chain gate ──
        if PRECHAIN_GATE_ENABLED:
            _sw_vol_regime = get_canonical_vol_regime(ticker, get_daily_candles(ticker, days=30))
            _sw_gate = should_pull_chains(
                ticker=ticker, bias=bias, webhook_data=webhook_data,
                live_spot=spot, vol_regime=_sw_vol_regime,
                fundamental_data=_swing_fundamental_data,
                sector_data=_swing_sector_data,
                job_type="swing",
            )
            if not _sw_gate["qualified"]:
                base["reason"] = f"Pre-chain gate (swing) [{_sw_gate['gate_failed']}]: {_sw_gate['reason']}"
                log.info(f"[worker-{worker_id}] {ticker} swing blocked by pre-chain gate "
                         f"(saved {_sw_gate['api_calls_saved']} API calls)")
                _record_wave_result(base)
                return

        try:
            chains = get_options_chain_swing(ticker)
        except Exception as e:
            base["reason"] = f"data fetch error: {e}"
            _record_wave_result(base)
            return

        if not chains:
            base["reason"] = "no options chain data in 7-60 DTE range"
            _record_wave_result(base)
            return

        candles = get_daily_candles(ticker, days=252)
        vol_regime = get_canonical_vol_regime(ticker, candles)
        iv_rank = _estimate_iv_rank(chains, candles)
        structure_ctx = _build_canonical_structure_context(ticker, spot, _get_daily_ohlcv_rows(ticker, days=90))
        webhook_data = dict(webhook_data or {})
        webhook_data["vol_regime_module"] = vol_regime

        # ── v4.3: Always enrich swing signals with candle-derived context ──
        # Previously only manual /checkswing got this enrichment.
        # Automated TradingView swing alerts often don't send htf_confirmed,
        # weekly_bull, rsi_mfi_bull, vol_contracting — causing massive
        # confidence penalties (−20, −8, −5, −5) that make passing impossible.
        # Now we compute these from candle data for ALL swing signals,
        # but only BACKFILL fields that TradingView didn't provide.
        is_manual = webhook_data.get("source") == "check" or webhook_data.get("tier") == "manual"
        manual_ctx = _compute_manual_swing_signal_context(ticker, spot, _get_daily_ohlcv_rows(ticker, days=90), webhook_data.get("bias") or bias, structure_ctx)

        if is_manual:
            # Manual mode: overwrite everything (existing behavior)
            webhook_data.update(manual_ctx)
        else:
            # Automated mode: only backfill fields that TV didn't send
            # These boolean fields default to False when missing from the webhook,
            # which is indistinguishable from "TV sent false" vs "TV didn't send".
            # We use the candle-derived values as the ground truth when TV is silent.
            _backfill_keys = [
                "htf_confirmed", "htf_converging", "weekly_bull", "weekly_bear",
                "daily_bull", "rsi_mfi_bull", "vol_contracting",
                "structure_bias_score", "structure_reasons",
                "fib_level", "fib_distance_pct",
            ]
            for k in _backfill_keys:
                # Only backfill if the webhook value is the "empty" default
                # For booleans: False is the default (TV didn't send it)
                # For strings/numbers: use manual_ctx value if webhook has default
                wv = webhook_data.get(k)
                mv = manual_ctx.get(k)
                if mv is not None:
                    if isinstance(wv, bool) and wv is False and mv:
                        webhook_data[k] = mv
                    elif k == "fib_distance_pct" and wv == 2.0 and mv != 2.0:
                        # 2.0 is the default — replace with computed value
                        webhook_data[k] = mv
                    elif k == "fib_level" and wv == "61.8" and mv != "61.8":
                        # "61.8" is the default — replace with computed value
                        webhook_data[k] = mv
                    elif k in ("structure_bias_score", "structure_reasons") and not wv:
                        webhook_data[k] = mv
            log.info(f"Swing enrichment for {ticker}: htf_confirmed={webhook_data.get('htf_confirmed')} "
                     f"weekly_bull={webhook_data.get('weekly_bull')} weekly_bear={webhook_data.get('weekly_bear')} "
                     f"vol_contracting={webhook_data.get('vol_contracting')} rsi_mfi_bull={webhook_data.get('rsi_mfi_bull')}")
        rec = recommend_swing_trade(
            ticker=ticker, spot=spot, chains=chains,
            webhook_data=webhook_data, iv_rank=iv_rank,
        )
        rec = _apply_canonical_vol_overlay_to_rec(rec, vol_regime, mode="swing") if rec.get("ok") else rec
        rec = _apply_canonical_structure_overlay_to_rec(rec, structure_ctx, mode="swing") if rec.get("ok") else rec
        base["confidence"] = rec.get("confidence")
        # v5.0: Direction-aware CRISIS gate for swing trades.
        # Bears in CRISIS = correct trade (market tanking). Lower confidence gate.
        # Bulls in CRISIS = fighting the tide. Require exceptional confidence.
        if rec.get("ok") and vol_regime.get("label") == "CRISIS":
            _sw_conf = int(rec.get("confidence") or 0)
            if bias == "bull" and _sw_conf < 80:
                rec = {"ok": False, "reason": f"CRISIS regime — bull swing requires conf >= 80 (got {_sw_conf})", "confidence": _sw_conf}
                base["confidence"] = _sw_conf
            elif bias == "bear" and _sw_conf < 55:
                rec = {"ok": False, "reason": f"CRISIS regime — bear swing requires conf >= 55 (got {_sw_conf})", "confidence": _sw_conf}
                base["confidence"] = _sw_conf
            elif bias == "bear":
                # Bear passed — add CRISIS note to card
                rec["vol_regime_note"] = (rec.get("vol_regime_note") or "") + "\n⚠️ CRISIS REGIME — reduced sizing recommended. Bears valid but volatility is extreme."

        if not rec.get("ok"):
            base["reason"] = rec.get("reason", "no valid setup")
            _record_wave_result(base)
        else:
            base["outcome"] = "trade"
            _swing_card = format_swing_card(rec)
            if rec.get("vol_regime_note"):
                _swing_card = rec["vol_regime_note"] + "\n\n" + _swing_card
            base["card"]    = _swing_card
            _record_wave_result(base)
            log.info(f"Swing winner queued for digest: {ticker} {bias} conf={rec.get('confidence')}/100")


def _signal_queue_worker_redis(worker_id: int):
    log.info(f"Redis signal worker-{worker_id} started")
    last_heartbeat = time.time()

    while True:
        now = time.time()
        if now - last_heartbeat > 60:
            r_hb = _get_redis()
            depth = r_hb.llen(REDIS_QUEUE_KEY) if r_hb else "?"
            log.info(f"[worker-{worker_id}] heartbeat — Redis queue depth: {depth}")
            last_heartbeat = now

        r = _get_redis()
        if not r:
            log.warning(f"[worker-{worker_id}] Redis unavailable — sleeping 5s")
            time.sleep(5)
            continue

        try:
            result = r.brpop(REDIS_QUEUE_KEY, timeout=5)
            if not result:
                continue
            _, raw = result
            job = json.loads(raw)
            try:
                _process_job(worker_id, job)
            except Exception as e:
                log.error(f"[worker-{worker_id}] Job error for {job.get('ticker')}: {e}", exc_info=True)
        except Exception as e:
            log.error(f"[worker-{worker_id}] Redis worker error: {e}", exc_info=True)
            global _redis_client
            _redis_client = None
            time.sleep(3)


def _signal_queue_worker_memory(worker_id: int):
    log.info(f"Memory signal worker-{worker_id} started")
    last_heartbeat = time.time()

    while True:
        now = time.time()
        if now - last_heartbeat > 60:
            log.info(f"[worker-{worker_id}] heartbeat — memory queue depth: {_mem_signal_queue.qsize()}")
            last_heartbeat = now

        try:
            try:
                job_tuple = _mem_signal_queue.get(block=True, timeout=5)
            except queue.Empty:
                continue
            if job_tuple is None:
                break

            job_type, ticker, bias, webhook_data, signal_msg, enqueued_at = job_tuple
            job = {
                "job_type": job_type, "ticker": ticker, "bias": bias,
                "webhook_data": webhook_data, "signal_msg": signal_msg,
                "enqueued_at": enqueued_at,
            }
            try:
                _process_job(worker_id, job)
            except Exception as e:
                log.error(f"[worker-{worker_id}] Memory job error for {ticker}: {e}", exc_info=True)
            finally:
                _mem_signal_queue.task_done()
        except Exception as e:
            log.error(f"[worker-{worker_id}] Memory worker error: {e}", exc_info=True)
            time.sleep(2)


def _start_workers():
    r = _get_redis()
    worker_fn = _signal_queue_worker_redis if r else _signal_queue_worker_memory
    mode = "Redis" if r else "memory"
    log.info(f"Starting {QUEUE_WORKERS} {mode} signal workers (TTL {SIGNAL_TTL_SEC}s)")
    threads = []
    for i in range(QUEUE_WORKERS):
        t = threading.Thread(target=worker_fn, args=(i + 1,), daemon=True, name=f"signal-worker-{i + 1}")
        t.start()
        threads.append(t)
    return threads


_queue_worker_threads = []


# ─────────────────────────────────────────────────────────
# check_ticker WITH HARD TIMEOUT (v3.9)
# ─────────────────────────────────────────────────────────

def check_ticker_with_timeout(ticker, direction="bull", webhook_data=None, timeout_sec=None):
    timeout_sec = timeout_sec if timeout_sec is not None else CHECK_TICKER_TIMEOUT_SEC
    result = {}
    exc_holder = []

    def _run():
        try:
            result.update(check_ticker(ticker, direction=direction, webhook_data=webhook_data))
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        log.error(f"check_ticker({ticker}) TIMED OUT after {timeout_sec}s")
        return {"ticker": ticker, "ok": False, "posted": False, "error": "timeout", "reason": "timeout"}

    if exc_holder:
        raise exc_holder[0]
    return result


# ─────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────

def post_to_telegram(text: str, max_retries: int = 4, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not cid:
        log.error("post_to_telegram: TELEGRAM_BOT_TOKEN or CHAT_ID not set")
        return 400, "TELEGRAM tokens not set"
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
    last_err = ""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                preview = text[:60].replace("\n", " ")
                log.info(f"Telegram OK (chat={cid}): {preview}...")
                return 200, ""
            if r.status_code == 429:
                try: retry_after = r.json().get("parameters", {}).get("retry_after", 15)
                except Exception: retry_after = 15
                log.warning(f"Telegram rate limited — waiting {retry_after}s (attempt {attempt+1})")
                time.sleep(retry_after + 1)
                continue
            last_err = r.text[:300] if r.text else f"HTTP {r.status_code}"
            log.warning(f"Telegram attempt {attempt+1} failed: {r.status_code} — {last_err}")
        except Exception as e:
            last_err = str(e)
            log.warning(f"Telegram attempt {attempt+1} exception: {last_err}")
        time.sleep(min(1.5 * (attempt + 1), 6.0))
    log.error(f"Telegram FAILED after {max_retries} attempts: {last_err}")
    return 500, f"Telegram failed: {last_err}"


def get_portfolio_chat_id(account: str) -> str:
    cid = ACCOUNT_CHAT_IDS.get(account, "")
    return cid if cid else TELEGRAM_CHAT_ID


def post_to_intraday(text: str):
    """Post to the dedicated intraday channel (TELEGRAM_CHAT_INTRADAY).
    Used exclusively by the thesis monitor daemon — dealer briefs, action
    guides, swing and spread alerts all continue using post_to_telegram()
    which defaults to TELEGRAM_CHAT_ID (main channel).
    Falls back to main channel if TELEGRAM_CHAT_INTRADAY is not configured."""
    cid = TELEGRAM_CHAT_INTRADAY or TELEGRAM_CHAT_ID
    if not cid:
        log.error("post_to_intraday: no chat ID configured")
        return
    post_to_telegram(text, chat_id=cid)

# ─────────────────────────────────────────────────────────
# MARKETDATA API
# ─────────────────────────────────────────────────────────

def md_get(url, params=None, retries=2):
    """MarketData API GET with retry on timeout.
    timeout=15s — gives the API time to respond under load.
    retries=2   — tries up to 3 times total before giving up.
    """
    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {MARKETDATA_TOKEN}"},
                             params=params or {}, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout as e:
            last_err = e
            if attempt < retries:
                log.warning(f"md_get timeout (attempt {attempt+1}/{retries+1}): {url.split('/')[-2]}")
                continue
        except Exception as e:
            raise
    raise last_err

# v4.1: Cached API layer — reduces duplicate API calls by 70-90%
# v5.1.1: Counter wraps md_get inside CachedMarketData for per-endpoint tracking
_cached_md = CachedMarketData(md_get)

def get_api_status() -> dict:
    """v5.1.1: Get API call counter status for /status endpoint.
    Returns dict with total, budget, pct_used, breakdown by endpoint, day."""
    return _cached_md.get_api_status()

def get_spot(ticker: str) -> float:
    return _cached_md.get_spot(ticker, as_float_fn=as_float)

def get_expirations(ticker: str) -> list:
    return _cached_md.get_expirations(ticker)

def get_daily_candles(ticker: str, days: int = 30) -> list:
    return _cached_md.get_daily_candles(ticker, days)

def get_intraday_bars(ticker: str, resolution: int = 5, countback: int = 80) -> dict:
    """Fetch intraday OHLCV bars. Returns raw API dict for BarStateManager.

    Retries with larger countback values on 404 — MarketData returns 404 when
    the requested number of bars don't exist yet (e.g. early session or slow
    periods with sparse 1-minute activity). Stepping up gives the API more
    time window to find bars.

    v5.1.1: For small countbacks (<=10, from monitor update polls), don't retry
    with [10, 20, 40] — just try the requested value. Previously countback=5
    generated [5, 10, 20, 40] = 4 API calls even though the monitor will
    retry on its own next cycle 60s later. This was wasting ~3 API calls
    per monitor poll when bars weren't available.
    """
    # For monitor update polls (small countback), just try the requested value.
    # For scanner/init calls (large countback), retry with fallback sizes.
    if countback <= 10:
        countbacks_to_try = [countback]
    else:
        countbacks_to_try = sorted(set([countback, 20, 40]))
    last_err = None
    for cb in countbacks_to_try:
        try:
            result = _cached_md.get_intraday_bars(ticker, resolution, cb)
            if result:
                return result
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "Not Found" in err_str:
                log.debug(f"Intraday bars 404 for {ticker} countback={cb}, trying larger")
                last_err = e
                continue
            # Non-404 error — don't retry
            raise
    if last_err:
        raise last_err
    return {}

def get_vix() -> float:
    # v4.3 fix: Try MarketData API first (paid, reliable), then Yahoo, then IV proxy.
    # Previous order was Yahoo first, which has been broken/unreliable.
    try:
        vix_data = _get_vix_data()
        if vix_data and vix_data.get("vix", 0) > 0:
            v = vix_data["vix"]
            log.info(f"VIX from MarketData API: {v:.2f}")
            return v
    except Exception as e:
        log.warning(f"VIX MarketData fetch failed: {e}")
    try:
        resp = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                           params={"interval": "1d", "range": "1d"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.raise_for_status()
        result = resp.json().get("chart", {}).get("result", [])
        if result:
            meta = result[0].get("meta", {})
            for field in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
                v = as_float(meta.get(field), 0.0)
                if v > 0:
                    log.info(f"VIX from Yahoo Finance: {v:.2f}")
                    return v
    except Exception as e:
        log.warning(f"VIX Yahoo fetch failed: {e}")
    try:
        spy_chains = get_options_chain_swing("SPY")
        ivs = []
        for exp, dte, contracts in spy_chains[:3]:
            for c in contracts:
                iv = as_float(c.get("iv") or c.get("impliedVolatility"), 0.0)
                if 0.05 < iv < 2.0:
                    ivs.append(iv * 100)
        if ivs:
            proxy = round(sum(ivs) / len(ivs), 1)
            log.info(f"VIX proxy from SPY IV: {proxy:.1f}")
            return proxy
    except Exception as e:
        log.warning(f"VIX SPY-IV fallback failed: {e}")
    log.warning("VIX unavailable from ALL sources — returning 20.0 as neutral default")
    return 20.0


_regime_cache = {"data": None, "ts": 0}

def get_current_regime() -> dict:
    now = time.time()
    if _regime_cache["data"] and (now - _regime_cache["ts"]) < 300:
        return _regime_cache["data"]
    try:
        vix = get_vix()
        spy_candles = get_daily_candles("SPY", days=65)
        regime = risk_manager.classify_regime(vix=vix, spy_candles=spy_candles)
        _regime_cache["data"] = regime
        _regime_cache["ts"] = now
        log.info(f"Regime: {regime.get('label')} (VIX {vix:.1f}, ADX {regime.get('adx', 0):.0f})")
        return regime
    except Exception as e:
        log.warning(f"Regime detection failed: {e}")
        return {"label": "UNKNOWN", "emoji": "❓", "vix": 0, "adx": 0,
                "vix_regime": "UNKNOWN", "adx_regime": "UNKNOWN", "size_mult": 1.0}


def _market_today_date():
    """Use America/Chicago so 0DTE/1DTE align with your trading day, not UTC midnight."""
    try:
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


# ─────────────────────────────────────────────────────────
# CANONICAL VOLATILITY REGIME MODULE
# Shared overlay for EM, scalp, and swing.
# Uses simple, robust signals only: VIX band, VIX vs 200DMA,
# VIX9D/VIX term structure, VVIX warning, and realized vol context.
# ─────────────────────────────────────────────────────────

_vol_regime_market_cache = {"data": None, "ts": 0}
_vol_regime_symbol_cache = {}


def _fetch_yahoo_chart_closes(symbol: str, range_days: int = 450) -> list:
    try:
        sym = requests.utils.quote(symbol, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        resp = requests.get(url, params={"range": f"{range_days}d", "interval": "1d", "includePrePost": "false"}, timeout=8)
        resp.raise_for_status()
        data = resp.json() or {}
        result = (((data.get("chart") or {}).get("result") or [None])[0] or {})
        quotes = (((result.get("indicators") or {}).get("quote") or [None])[0] or {})
        closes = quotes.get("close") or []
        out = []
        for c in closes:
            try:
                if c is not None:
                    out.append(float(c))
            except Exception:
                pass
        if out:
            return out
    except Exception as e:
        log.debug(f"Yahoo closes fetch failed for {symbol}: {e}")
    # v4.3: Fallback to MarketData API for index history
    try:
        # MarketData uses plain tickers (VIX, not ^VIX)
        md_symbol = symbol.replace("^", "").replace("$", "")
        # Indices (VIX, VIX9D, VVIX, DJI, etc.) use /indices/ not /stocks/
        is_index = md_symbol.upper() in ("VIX", "VIX9D", "VVIX", "DJI", "SPX", "NDX", "RUT", "OEX")
        base_path = "indices" if is_index else "stocks"
        data = _cached_md.raw_get(  # v5.1.1: counter
            f"https://api.marketdata.app/v1/{base_path}/candles/daily/{md_symbol}/",
            {"countback": min(range_days, 500)},
        )
        if isinstance(data, dict) and data.get("s") == "ok":
            closes = data.get("c", [])
            out = [float(c) for c in closes if c is not None]
            if out:
                log.info(f"Got {len(out)} closes for {symbol} from MarketData fallback")
                return out
    except Exception as e:
        log.debug(f"MarketData closes fallback failed for {symbol}: {e}")
    return []


def _fetch_yahoo_last(symbol: str) -> float | None:
    try:
        closes = _fetch_yahoo_chart_closes(symbol, range_days=10)
        return float(closes[-1]) if closes else None
    except Exception:
        pass
    # v4.3: MarketData fallback for last price
    try:
        md_symbol = symbol.replace("^", "").replace("$", "")
        is_index = md_symbol.upper() in ("VIX", "VIX9D", "VVIX", "DJI", "SPX", "NDX", "RUT", "OEX")
        base_path = "indices" if is_index else "stocks"
        data = _cached_md.raw_get(f"https://api.marketdata.app/v1/{base_path}/quotes/{md_symbol}/")  # v5.1.1: counter
        if isinstance(data, dict):
            for field in ("last", "mid", "bid"):
                v = data.get(field)
                if isinstance(v, list):
                    v = v[0] if v else None
                val = as_float(v, 0)
                if val > 0:
                    log.info(f"Got {symbol} = {val} from MarketData fallback")
                    return val
    except Exception as e:
        log.debug(f"MarketData last price fallback failed for {symbol}: {e}")
    return None


def _calc_ann_rv_from_closes(closes: list, window: int = 20) -> float | None:
    try:
        vals = [float(x) for x in closes if x is not None and float(x) > 0]
        if len(vals) < window + 1:
            return None
        vals = vals[-(window + 1):]
        rets = []
        for i in range(1, len(vals)):
            prev = vals[i - 1]
            cur = vals[i]
            if prev > 0 and cur > 0:
                rets.append(math.log(cur / prev))
        if len(rets) < max(3, window - 1):
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        return math.sqrt(var) * math.sqrt(252) * 100.0
    except Exception:
        return None


def _get_vix_ma200() -> float | None:
    now = time.time()
    cached = _vol_regime_market_cache.get("vix_ma200")
    ts = _vol_regime_market_cache.get("vix_ma200_ts", 0)
    if cached is not None and (now - ts) < 3600:
        return cached
    # Try Yahoo first
    closes = _fetch_yahoo_chart_closes("^VIX", range_days=420)
    # v4.3: Fallback to CBOE direct CSV if Yahoo fails
    if len(closes) < 200:
        try:
            resp = requests.get(
                "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) > 200:
                # Parse CLOSE column (index 4) from last 250 rows
                cboe_closes = []
                for line in lines[-250:]:
                    parts = line.strip().split(",")
                    if len(parts) >= 5:
                        try:
                            cboe_closes.append(float(parts[4]))
                        except (ValueError, TypeError):
                            pass
                if len(cboe_closes) >= 200:
                    closes = cboe_closes
                    log.info(f"VIX MA200: using CBOE direct CSV ({len(closes)} closes)")
        except Exception as e:
            log.debug(f"CBOE VIX history fallback failed: {e}")
    if len(closes) < 200:
        return None
    ma = sum(closes[-200:]) / 200.0
    _vol_regime_market_cache["vix_ma200"] = ma
    _vol_regime_market_cache["vix_ma200_ts"] = now
    return ma


def _get_vvix_value() -> float | None:
    now = time.time()
    cached = _vol_regime_market_cache.get("vvix")
    ts = _vol_regime_market_cache.get("vvix_ts", 0)
    if cached is not None and (now - ts) < 900:
        return cached
    vvix = _fetch_yahoo_last("^VVIX")
    # v4.3: CBOE fallback for VVIX
    if not vvix:
        try:
            resp = requests.get(
                "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
                timeout=8, headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) > 1:
                parts = lines[-1].strip().split(",")
                if len(parts) >= 5:
                    vvix = as_float(parts[4], 0)
                    if vvix > 0:
                        log.info(f"VVIX from CBOE direct: {vvix:.1f}")
        except Exception as e:
            log.debug(f"CBOE VVIX fallback failed: {e}")
    _vol_regime_market_cache["vvix"] = vvix
    _vol_regime_market_cache["vvix_ts"] = now
    return vvix


def get_canonical_vol_regime(ticker: str = "SPY", candle_closes: list | None = None, vix_override: dict | None = None) -> dict:
    ticker = (ticker or "SPY").upper()
    cache_key = ticker
    now = time.time()
    cached = _vol_regime_symbol_cache.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 300:
        return cached.get("data", {})

    market = _get_vix_data() or {}
    # v4.3: Use vix_override (e.g. IV proxy) if MarketData returned nothing
    if (not market or not market.get("vix")) and vix_override and vix_override.get("vix"):
        market = vix_override
        log.info(f"Vol regime using VIX override: {market.get('vix')} (source: {market.get('source', 'override')})")
    closes = candle_closes or get_daily_candles(ticker, days=65) or get_daily_candles("SPY", days=65)
    # Fetch SPY closes for market-level RV spike detection.
    # For non-SPY tickers, rv_spike now fires on SPY volatility, not just
    # the ticker's own quiet vol — catches broad-market stress (e.g. GLD).
    spy_closes_for_rv = (
        closes if ticker == "SPY"
        else (get_daily_candles("SPY", days=65) or [])
    )
    result = _um_build_canonical_vol_regime(
        ticker=ticker,
        candle_closes=closes,
        market=market,
        fetch_vix9d_fn=_fetch_yahoo_last,
        get_vix_ma200_fn=_get_vix_ma200,
        get_vvix_value_fn=_get_vvix_value,
        now_ts=now,
        spy_closes=spy_closes_for_rv,
    )
    # v4.3: Diagnostic logging for vol regime pipeline
    log.info(f"Vol regime [{ticker}]: label={result.get('label')} base={result.get('base')} "
             f"vix={result.get('vix')} vix9d={result.get('vix9d')} vvix={result.get('vvix')} "
             f"term={result.get('term_structure')} ma200={result.get('vix_ma200')} "
             f"caution={result.get('caution_score')} rv5={result.get('rv5')} rv20={result.get('rv20')}")
    _vol_regime_symbol_cache[cache_key] = {"data": result, "ts": now}
    return result


def _format_canonical_vol_line(vol_regime: dict) -> str:
    return _um_format_canonical_vol_line(vol_regime)


def _apply_canonical_vol_overlay_to_rec(rec: dict, vol_regime: dict, mode: str = "scalp") -> dict:
    return _um_apply_vol_overlay_to_rec(rec, vol_regime, mode=mode)


def _ema(values, length: int):
    vals = [float(v) for v in (values or []) if v is not None]
    if not vals:
        return None
    alpha = 2.0 / (length + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _rsi(values, length: int = 14):
    vals = [float(v) for v in (values or []) if v is not None]
    if len(vals) < length + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _mfi(rows, length: int = 14):
    rows = rows or []
    if len(rows) < length + 1:
        return None
    pos = []
    neg = []
    prev_tp = None
    for r in rows:
        tp = (float(r['high']) + float(r['low']) + float(r['close'])) / 3.0
        mf = tp * float(r.get('volume') or 0.0)
        if prev_tp is not None:
            if tp > prev_tp:
                pos.append(mf); neg.append(0.0)
            elif tp < prev_tp:
                pos.append(0.0); neg.append(mf)
            else:
                pos.append(0.0); neg.append(0.0)
        prev_tp = tp
    if len(pos) < length:
        return None
    pmf = sum(pos[-length:]); nmf = sum(neg[-length:])
    if nmf == 0:
        return 100.0
    ratio = pmf / nmf
    return 100.0 - (100.0 / (1.0 + ratio))


def _compute_manual_swing_signal_context(ticker: str, spot: float, rows: list, direction: str, structure_ctx: dict | None = None) -> dict:
    return _um_build_manual_swing_signal_context(ticker, spot, rows, direction, structure_ctx)


def _build_canonical_structure_context(ticker: str, spot: float, rows: list | None = None) -> dict:
    rows = rows if rows is not None else _get_daily_ohlcv_rows(ticker, days=90)
    return _um_build_canonical_structure_context(ticker, spot, rows)


def _apply_canonical_structure_overlay_to_rec(rec: dict, structure_ctx: dict | None, mode: str = 'scalp') -> dict:
    return _um_apply_structure_overlay_to_rec(rec, structure_ctx, mode=mode)


def _dedupe_expirations_by_dte(valid_exps: list, max_per_dte: int = 1) -> list:
    """
    When provider expirations straddle UTC midnight, two expirations can both appear as DTE 0.
    Keep only the earliest expiration per DTE bucket to save API calls.
    """
    buckets = {}
    for dte, exp in valid_exps:
        buckets.setdefault(dte, []).append(exp)

    deduped = []
    for dte in sorted(buckets.keys()):
        # earliest expiry first; if caller wants more than one per DTE, keep that many
        for exp in sorted(buckets[dte])[:max_per_dte]:
            deduped.append((dte, exp))
    return deduped


def _is_http_429(exc: Exception) -> bool:
    try:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status == 429 or "429" in str(exc)
    except Exception:
        return "429" in str(exc)



# ─────────────────────────────────────────────────────────
# OPTIONS CHAIN — SCALP (0-10 DTE)
# ─────────────────────────────────────────────────────────

def get_options_chain(ticker: str, side: str = None, strike_limit: int = 20) -> list:
    """Fetch option chains for scalp-eligible expirations (0-10 DTE).

    v5.1.1 API credit optimization:
    MarketData.app charges 1 credit PER option symbol returned.
    SPY full chain = 390 credits. With strikeLimit=20 + side filter = ~20 credits.
    This single change saves ~70,000 credits/day.

    Args:
        side: "call" or "put" — fetch one side only (50% credit savings)
        strike_limit: limit to N nearest-ATM strikes per side (default 20).
                      Set to None for full chain (OI sweep).
    """
    from trading_rules import MAX_EXPIRATIONS_TO_PULL
    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = _market_today_date()

    valid_exps = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if MIN_DTE <= dte <= MAX_DTE:
                valid_exps.append((dte, exp))
        except Exception:
            continue
    valid_exps.sort(key=lambda x: x[0])
    valid_exps = _dedupe_expirations_by_dte(valid_exps, max_per_dte=1)

    if not valid_exps:
        for exp in exps:
            try:
                dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
                valid_exps.append((dte, exp))
            except Exception:
                continue
        valid_exps.sort(key=lambda x: x[0])

    if not valid_exps:
        raise RuntimeError(f"No usable expirations for {ticker}")

    exps_to_fetch = valid_exps[:MAX_EXPIRATIONS_TO_PULL]
    results = []
    saw_rate_limit = False

    for dte, exp in exps_to_fetch:
        try:
            data = _cached_md.get_chain(ticker, exp, side=side, strike_limit=strike_limit)  # v5.1.1: strikeLimit + side filter
            if not isinstance(data, dict) or data.get("s") != "ok":
                continue
            sym_list = data.get("optionSymbol") or []
            if not sym_list:
                continue
            n = len(sym_list)

            def col(name, default=None):
                v = data.get(name, default)
                return v if isinstance(v, list) else [default] * n

            contracts = []
            sides = col("side", "")
            for i in range(n):
                right = (sides[i] or "").lower().strip()
                right = "call" if right in ("c", "call") else "put" if right in ("p", "put") else right
                contracts.append({
                    "optionSymbol": col("optionSymbol", "")[i],
                    "right": right, "strike": as_float(col("strike", None)[i], None),
                    "expiration": exp, "dte": dte,
                    "openInterest": as_int(col("openInterest", 0)[i], 0),
                    "volume": as_int(col("volume", 0)[i], 0),
                    "iv": as_float(col("iv", None)[i], None),
                    "gamma": as_float(col("gamma", 0.0)[i], 0.0),
                    "delta": as_float(col("delta", None)[i], None),
                    "theta": as_float(col("theta", None)[i], None),
                    "vega": as_float(col("vega", None)[i], None),
                    "bid": as_float(col("bid", None)[i], None),
                    "ask": as_float(col("ask", None)[i], None),
                    "mid": as_float(col("mid", None)[i], None),
                })
            if contracts:
                results.append((exp, dte, contracts))
                log.info(f"{ticker}: fetched {len(contracts)} contracts for {exp} (DTE {dte})")
        except Exception as e:
            if _is_http_429(e):
                saw_rate_limit = True
                log.warning(f"{ticker}: rate limit while fetching chain for {exp}: {e}")
                break
            log.warning(f"{ticker}: failed to fetch chain for {exp}: {e}")
            continue

    if not results:
        if saw_rate_limit:
            raise RuntimeError(f"Rate limit hit while fetching options chain for {ticker}. Try again shortly.")
        raise RuntimeError(f"No valid chains fetched for {ticker}")
    return results


# ─────────────────────────────────────────────────────────
# OPTIONS CHAIN — SWING (7-60 DTE)
# ─────────────────────────────────────────────────────────

def get_options_chain_swing(ticker: str, side: str = None, strike_limit: int = 20) -> list:
    """Fetch swing chains (7-60 DTE) with credit-saving filters.
    v5.1.1: strikeLimit + side filter saves ~80-95% of chain credits."""
    from swing_engine import SWING_MIN_DTE, SWING_MAX_DTE, SWING_MAX_EXPIRATIONS
    ticker = ticker.strip().upper()
    exps   = get_expirations(ticker)
    today  = _market_today_date()

    valid_exps = []
    for exp in exps:
        try:
            dte = max((datetime.fromisoformat(exp).date() - today).days, 0)
            if SWING_MIN_DTE <= dte <= SWING_MAX_DTE:
                valid_exps.append((dte, exp))
        except Exception:
            continue
    valid_exps.sort(key=lambda x: x[0])
    valid_exps = _dedupe_expirations_by_dte(valid_exps, max_per_dte=1)

    if not valid_exps:
        raise RuntimeError(f"No swing expirations ({SWING_MIN_DTE}-{SWING_MAX_DTE} DTE) for {ticker}")

    results = []
    saw_rate_limit = False
    for dte, exp in valid_exps[:SWING_MAX_EXPIRATIONS]:
        try:
            data = _cached_md.get_chain(ticker, exp, side=side, strike_limit=strike_limit)  # v5.1.1: strikeLimit + side filter
            if not isinstance(data, dict) or data.get("s") != "ok":
                continue
            sym_list = data.get("optionSymbol") or []
            if not sym_list:
                continue
            n = len(sym_list)

            def col(name, default=None):
                v = data.get(name, default)
                return v if isinstance(v, list) else [default] * n

            contracts = []
            sides = col("side", "")
            for i in range(n):
                right = (sides[i] or "").lower().strip()
                right = "call" if right in ("c", "call") else "put" if right in ("p", "put") else right
                contracts.append({
                    "optionSymbol": col("optionSymbol", "")[i],
                    "right": right, "strike": as_float(col("strike", None)[i], None),
                    "expiration": exp, "dte": dte,
                    "openInterest": as_int(col("openInterest", 0)[i], 0),
                    "volume": as_int(col("volume", 0)[i], 0),
                    "iv": as_float(col("iv", None)[i], None),
                    "gamma": as_float(col("gamma", 0.0)[i], 0.0),
                    "delta": as_float(col("delta", None)[i], None),
                    "theta": as_float(col("theta", None)[i], None),
                    "vega": as_float(col("vega", None)[i], None),
                    "bid": as_float(col("bid", None)[i], None),
                    "ask": as_float(col("ask", None)[i], None),
                    "mid": as_float(col("mid", None)[i], None),
                })
            if contracts:
                results.append((exp, dte, contracts))
                log.info(f"Swing {ticker}: fetched {len(contracts)} contracts for {exp} (DTE {dte})")
        except Exception as e:
            log.warning(f"Swing {ticker}: failed chain for {exp}: {e}")
            continue
    return results


def _get_contracts_for_expiry(ticker: str, exp: str) -> list:
    """Fetch a single-expiry chain snapshot for wheel/monitor suggestions."""
    data = _cached_md.get_chain(ticker, exp, strike_limit=20)  # v5.1.1: strikeLimit saves credits
    if not isinstance(data, dict) or data.get("s") != "ok":
        return []
    sym_list = data.get("optionSymbol") or []
    if not sym_list:
        return []
    n = len(sym_list)

    def col(name, default=None):
        v = data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    contracts = []
    sides = col("side", "")
    for i in range(n):
        right = (sides[i] or "").lower().strip()
        right = "call" if right in ("c", "call") else "put" if right in ("p", "put") else right
        bid = as_float(col("bid", None)[i], None)
        ask = as_float(col("ask", None)[i], None)
        mid = as_float(col("mid", None)[i], None)
        strike = as_float(col("strike", None)[i], None)
        if strike is None:
            continue
        contracts.append({
            "optionSymbol": col("optionSymbol", "")[i],
            "right": right,
            "strike": strike,
            "expiration": exp,
            "openInterest": as_int(col("openInterest", 0)[i], 0),
            "volume": as_int(col("volume", 0)[i], 0),
            "iv": as_float(col("iv", None)[i], None),
            "delta": as_float(col("delta", None)[i], None),
            "bid": bid,
            "ask": ask,
            "mid": mid,
        })
    return contracts


def _wheel_est_credit(contract: dict) -> float | None:
    bid = contract.get("bid")
    ask = contract.get("ask")
    mid = contract.get("mid")
    vals = [v for v in (bid, ask, mid) if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    if bid and mid:
        return round((bid + mid) / 2.0, 2)
    if mid:
        return round(mid, 2)
    return round(bid or ask or 0.0, 2)


def _score_wheel_candidate(c: dict, target_delta: float, liquidity_floor: int = 50) -> float:
    oi = c.get("openInterest") or 0
    vol = c.get("volume") or 0
    delta = abs(c.get("delta") or 0.0)
    bid = c.get("bid") or 0.0
    ask = c.get("ask") or 0.0
    spread_pen = 0.0
    if bid > 0 and ask >= bid:
        mid = max((bid + ask) / 2.0, 0.01)
        spread_pen = max(0.0, (ask - bid) / mid)
    liq_bonus = min((oi + vol) / max(liquidity_floor, 1), 2.0)
    return -abs(delta - target_delta) + 0.20 * liq_bonus - 0.15 * spread_pen


def _pick_wheel_short(contract_rows: list, side: str, spot: float, em: dict, walls: dict, adjusted_basis=None):
    side = side.lower().strip()
    candidates = []
    target_delta = 0.22
    for c in contract_rows:
        if c.get("right") != side:
            continue
        strike = c.get("strike")
        if strike is None:
            continue
        credit = _wheel_est_credit(c)
        if credit is None or credit < 0.05:
            continue
        oi = c.get("openInterest") or 0
        delta = abs(c.get("delta") or 0.0)
        if side == "call":
            if strike <= spot:
                continue
            if adjusted_basis is not None and strike < adjusted_basis:
                continue
            structural_ok = strike >= max((adjusted_basis or 0), spot + 0.35 * (em.get("em_1sd") or 0))
            if walls.get("call_wall"):
                structural_ok = structural_ok or strike >= walls.get("call_wall")
        else:
            if strike >= spot:
                continue
            structural_ok = strike <= spot - 0.30 * (em.get("em_1sd") or 0)
            if walls.get("put_wall"):
                structural_ok = structural_ok or strike <= walls.get("put_wall")
        if delta and not (0.10 <= delta <= 0.40):
            continue
        score = _score_wheel_candidate(c, target_delta)
        if structural_ok:
            score += 0.30
        if oi < 25:
            score -= 0.40
        candidates.append((score, c, credit))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, c, credit = candidates[0]
    strike = c.get("strike")
    delta = abs(c.get("delta") or 0.0)
    oi = c.get("openInterest") or 0
    bid = c.get("bid") or 0.0
    ask = c.get("ask") or 0.0
    mid = c.get("mid") or 0.0
    return {
        "strike": strike,
        "credit": credit,
        "delta": delta,
        "oi": oi,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "expiration": c.get("expiration"),
    }


def _wheel_label_from_delta(delta: float | None, option_type: str) -> tuple[str, str]:
    d = abs(delta or 0.0)
    if d <= 0.18:
        return "Conservative", "🟢"
    if d <= 0.30:
        return "Balanced", "🟡"
    return "Aggressive", "🔴"


def _wheel_premium_quality(credit: float | None, spot: float | None) -> tuple[str, str]:
    try:
        ratio = (credit or 0.0) / max(spot or 0.0, 0.01)
    except Exception:
        ratio = 0.0
    if ratio >= 0.035:
        return "Strong", "🟢"
    if ratio >= 0.015:
        return "Fair", "🟡"
    return "Light", "⚪"


def _build_wheel_focus_block(ticker: str, expiry: str, spot: float, em: dict, walls: dict):
    """Return wheel-focused lines for /monitorlong using the selected buffered expiry."""
    try:
        wheel = risk_manager.calc_wheel_pnl(ticker, account="brad")
    except Exception:
        wheel = {"has_shares": False, "adjusted_basis": None, "stage": "ACTIVE", "stage_emoji": "🔄", "shares": 0}
    contracts = _get_contracts_for_expiry(ticker, expiry)
    if not contracts:
        return []

    adjusted_basis = wheel.get("adjusted_basis")
    cc = _pick_wheel_short(contracts, "call", spot, em, walls or {}, adjusted_basis=adjusted_basis)
    csp = _pick_wheel_short(contracts, "put", spot, em, walls or {}, adjusted_basis=None)

    lines = ["", "🔄 Wheel Focus (30 DTE style):"]
    stage = wheel.get("stage", "ACTIVE")
    stage_emoji = wheel.get("stage_emoji", "🔄")
    basis_txt = f" | Adjusted basis: ${adjusted_basis:.2f}" if adjusted_basis is not None else ""
    lines.append(f"  {stage_emoji} Stage: {stage}{basis_txt}")

    if wheel.get("has_shares") and cc:
        fit, fit_emoji = _wheel_label_from_delta(cc.get("delta"), "call")
        prem, prem_emoji = _wheel_premium_quality(cc.get("credit"), spot)
        basis_guard = f" above basis ${adjusted_basis:.2f}" if adjusted_basis is not None else " above spot"
        lines.append(
            f"  📞 Preferred CC: Sell {ticker} {expiry} ${cc['strike']:.1f}C for ~${cc['credit']:.2f} credit "
            f"(Δ {cc['delta']:.2f}, OI {cc['oi']})"
        )
        lines.append(f"     Why: keeps the call{basis_guard} and nearer the upper expected-move / resistance zone without capping too early.")
        lines.append(f"     ⚖️ Wheel fit: {fit_emoji} {fit} | 💵 Premium quality: {prem_emoji} {prem}")
    elif cc:
        fit, fit_emoji = _wheel_label_from_delta(cc.get("delta"), "call")
        prem, prem_emoji = _wheel_premium_quality(cc.get("credit"), spot)
        lines.append(
            f"  📞 CC watch: {ticker} {expiry} ${cc['strike']:.1f}C ~${cc['credit']:.2f} credit "
            f"(Δ {cc['delta']:.2f}, OI {cc['oi']})"
        )
        lines.append("     Why: sits above spot and closer to the upper expected-move / resistance area, which gives upside room before assignment risk climbs.")
        lines.append(f"     ⚖️ Wheel fit: {fit_emoji} {fit} | 💵 Premium quality: {prem_emoji} {prem}")

    if csp:
        fit, fit_emoji = _wheel_label_from_delta(csp.get("delta"), "put")
        prem, prem_emoji = _wheel_premium_quality(csp.get("credit"), spot)
        lines.append(
            f"  🔻 Preferred CSP: Sell {ticker} {expiry} ${csp['strike']:.1f}P for ~${csp['credit']:.2f} credit "
            f"(Δ {csp['delta']:.2f}, OI {csp['oi']})"
        )
        lines.append("     Why: places the strike under spot and closer to support / lower expected-move territory.")
        lines.append(f"     ⚖️ Wheel fit: {fit_emoji} {fit} | 💵 Premium quality: {prem_emoji} {prem}")

    if not cc and not csp:
        lines.append("  No clean 30 DTE wheel strikes found on this chain.")
    return lines


# ─────────────────────────────────────────────────────────
# IV RANK HELPER
# ─────────────────────────────────────────────────────────

def _estimate_iv_rank(chains: list, candle_closes: list) -> float:
    """Estimate IV rank using the correct 52-week range formula.

    Uses get_iv_rank_from_candles() when candle_closes are available,
    which computes rank as (current_IV - rv_min) / (rv_max - rv_min) * 100.
    Falls back to a ratio-based heuristic only when candle data is absent.
    """
    try:
        from options_engine_v3 import get_avg_chain_iv as _get_avg_chain_iv
        from data_providers import get_iv_rank_from_candles

        # Gather ATM IVs from the nearest expiry
        current_ivs = []
        for exp, dte, contracts in chains:
            for c in contracts:
                iv = c.get("iv")
                if iv and SWING_IV_MIN < iv < SWING_IV_MAX:
                    current_ivs.append(iv)
            if current_ivs:
                break

        if not current_ivs:
            return 50.0

        current_iv = sum(current_ivs) / len(current_ivs)

        # Preferred path: proper 52-week range rank from pre-fetched closes
        if candle_closes and len(candle_closes) >= 30:
            from data_providers import get_iv_rank_from_closes
            iv_rank, iv_pct, hv20 = get_iv_rank_from_closes(current_iv, candle_closes)
            if iv_rank is not None:
                return round(iv_rank, 1)

        # Fallback: IV/RV ratio heuristic when candles unavailable
        from options_engine_v3 import calc_realized_vol
        rv = calc_realized_vol(candle_closes) if candle_closes else 0
        if rv <= 0:
            return 50.0
        ratio = current_iv / rv
        rank = min(100, max(0, (ratio - 0.5) / 1.5 * 100))
        return round(rank, 1)
    except Exception:
        return 50.0


# ─────────────────────────────────────────────────────────
# EXIT WARNING
# ─────────────────────────────────────────────────────────

def check_spread_exit_warning(ticker: str, signal_bias: str, webhook_data: dict):
    ticker = ticker.upper()
    if signal_bias == "bear":
        warn_direction = "bull"; warn_side = "call"
    elif signal_bias == "bull":
        warn_direction = "bear"; warn_side = "put"
    else:
        return

    for account in ("brad", "mom"):
        try:
            open_spreads = portfolio.get_open_spreads_for_ticker(ticker, account=account)
            if not open_spreads:
                continue
            at_risk = [sp for sp in open_spreads
                       if sp.get("direction", "bull") == warn_direction or sp.get("side", "call") == warn_side]
            if not at_risk:
                continue

            tier = webhook_data.get("tier", "?")
            wt2 = as_float(webhook_data.get("wt2"), 0)
            close_price = as_float(webhook_data.get("close"), 0)
            wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
            trend_str = ("✅ Confirmed" if webhook_data.get("htf_confirmed")
                         else "🟡 Converging" if webhook_data.get("htf_converging")
                         else "❌ Diverging")
            acct_label = "👩 Mom" if account == "mom" else "📁 Brad"

            for sp in at_risk:
                sp_debit = sp.get("debit", 0); sp_contracts = sp.get("contracts", 1)
                total_risk = sp_debit * sp_contracts * 100
                targets = sp.get("targets", {}); short_strike = sp.get("short", 0)
                long_strike = sp.get("long", 0); sp_side = sp.get("side", "call")
                side_label = "BULL CALL" if sp_side == "call" else "BEAR PUT"
                urgency = "⚠️"; urgency_note = "Monitor position"

                if close_price > 0 and sp_side == "call":
                    if short_strike > 0 and close_price <= short_strike:
                        urgency = "🚨"; urgency_note = "PRICE AT/BELOW SHORT STRIKE"
                    elif long_strike > 0 and close_price <= long_strike:
                        urgency = "🔴"; urgency_note = "Price between strikes — partial loss"
                elif close_price > 0 and sp_side == "put":
                    if short_strike > 0 and close_price >= short_strike:
                        urgency = "🚨"; urgency_note = "PRICE AT/ABOVE SHORT STRIKE"
                    elif long_strike > 0 and close_price >= long_strike:
                        urgency = "🔴"; urgency_note = "Price between strikes — partial loss"

                stop_level = targets.get("stop", 0)

                lines = [
                    f"{urgency} EXIT WARNING — {ticker} {side_label} ({acct_label})",
                    f"TV Signal: T{tier} {signal_bias.upper()} | Close: ${close_price:.2f}",
                    f"1H Trend: {trend_str} | Wave: {wave_zone}", "",
                    f"Open Spread: {sp['id']}",
                    f"  ${long_strike}/{short_strike} @${sp_debit:.2f} x{sp_contracts}",
                    f"  Risk: ${total_risk:,.0f} | Exp: {sp.get('exp', '?')}",
                    f"  {urgency_note}", "",
                    f"Targets: sell@${targets.get('same_day', 0):.2f} (30%) | "
                    f"${targets.get('next_day', 0):.2f} (35%) | ${targets.get('extended', 0):.2f} (50%)",
                ]
                if stop_level:
                    lines.append(f"Stop: ${stop_level:.2f}")
                lines.extend(["", "Action: Consider closing or tightening stop", "— Not financial advice —"])
                post_to_telegram("\n".join(lines))
                log.info(f"Exit warning posted for {ticker} {side_label} spread {sp['id']} ({account})")
        except Exception as e:
            log.error(f"Exit warning check failed for {ticker}/{account}: {e}")


# ─────────────────────────────────────────────────────────
# V4 PREFILTER — institutional flow quality gate for scalp engine
# Runs v4 snapshot on nearest-DTE chain before spread selection.
# Returns a v4_flow dict that compute_confidence can score.
# ─────────────────────────────────────────────────────────

def _contracts_to_chain_data(contracts: list) -> dict:
    """
    Convert v3 contract dicts back to MarketData.app columnar format
    so engine_bridge can consume it.
    """
    if not contracts:
        return {}
    keys = {
        "optionSymbol": "optionSymbol", "strike": "strike", "side": "right",
        "iv": "iv", "openInterest": "openInterest", "volume": "volume",
        "delta": "delta", "gamma": "gamma", "theta": "theta", "vega": "vega",
        "bid": "bid", "ask": "ask",
    }
    result = {"s": "ok"}
    for md_key, contract_key in keys.items():
        result[md_key] = [c.get(contract_key) for c in contracts]
    return result


def _run_v4_prefilter(ticker: str, spot: float, chains: list, candle_closes: list) -> dict:
    """
    Run v4 institutional snapshot on the nearest-DTE chain.
    Returns a v4_flow dict for compute_confidence, or empty dict on failure.

    The v4_flow dict contains:
      - confidence_label: HIGH/MODERATE/LOW
      - gex: net GEX value
      - bias: UPSIDE/DOWNSIDE/NEUTRAL
      - gamma_flip: flip price or None
      - spot: current spot
      - composite_regime: regime label string
      - downgrades: list of downgrade flags
    """
    try:
        if not chains:
            return {}

        # Use nearest-DTE chain (first in list, already sorted by DTE)
        exp, dte, contracts = chains[0]

        # Convert to MarketData.app format for the v4 engine
        chain_data = _contracts_to_chain_data(contracts)
        if not chain_data.get("optionSymbol"):
            return {}

        # Apply OI cache
        _oi_cache.apply_oi_changes_to_chain(ticker, exp, chain_data)

        # Build OHLC bars from candle closes for RV
        bars = _get_ohlc_bars(ticker, days=65)

        # Estimate liquidity
        adv, spread_pct = _estimate_liquidity(ticker, spot)

        # Run v4 snapshot
        v4 = run_institutional_snapshot(
            chain_data=chain_data, spot=spot, dte=max(dte, 0.5),
            recent_bars=bars, is_0dte=(dte == 0),
            avg_daily_dollar_volume=adv,
            bid_ask_spread_pct=spread_pct,
            liquid_index=_is_liquid(ticker),
        )

        # Save OI snapshot for next run
        _oi_cache.save_snapshot(ticker, exp, chain_data)
        if _oi_tracker:
            _oi_tracker.record_chain(ticker, exp, chain_data, spot=spot)

        # ── Intraday flow detection (volume/OI + direction approx) ──
        try:
            if _flow_detector:
                _persistent_state.save_oi_baseline(ticker, exp,
                    _oi_cache._parse_chain_oi(chain_data) if _oi_cache else {})
                flow_alerts = _flow_detector.check_intraday_flow(ticker, exp, chain_data, spot)
                postable = [fa for fa in flow_alerts
                           if fa.get("should_alert") and
                           fa.get("flow_level") in ("significant", "extreme")]
                if postable:
                    grouped = _flow_detector.format_grouped_flow_alerts(postable)
                    for msg in grouped:
                        try:
                            post_to_telegram(msg)
                            log.info(f"Flow alert: {ticker} ({len(postable)} strikes)")
                        except Exception:
                            pass
                # Generate trade ideas for extreme flow
                ideas = _flow_detector.generate_flow_trade_ideas(flow_alerts)
                if ideas:
                    try:
                        digest_msgs = _flow_detector.format_flow_ideas_digest(ideas)
                        for dm in digest_msgs:
                            post_to_telegram(dm)
                    except Exception:
                        pass
        except Exception as _ofe:
            log.debug(f"Flow check error for {ticker}: {_ofe}")

        if v4.get("error"):
            log.warning(f"v4 prefilter failed for {ticker}: {v4['error']}")
            return {}

        snap = v4.get("snapshot", {})
        confidence = snap.get("confidence", {})
        dealer = snap.get("dealer_flows", {})
        regime = snap.get("regime", {})
        adj = snap.get("adjusted_expectation", {})

        v4_flow = {
            "confidence_label": confidence.get("label", ""),
            "confidence_score": confidence.get("composite", 0),
            "gex": dealer.get("gex", 0),
            "dex": dealer.get("dex", 0),
            "vanna": dealer.get("vanna", 0),
            "charm": dealer.get("charm", 0),
            "bias": adj.get("bias", "NEUTRAL"),
            "bias_score": adj.get("bias_score", 0),
            "gamma_flip": dealer.get("gamma_flip"),
            "spot": spot,
            "composite_regime": regime.get("regime", ""),
            "downgrades": snap.get("downgrades", []),
            "vol_regime_label": snap.get("volatility_regime", {}).get("label", ""),
        }

        log.info(f"v4 prefilter {ticker}: conf={confidence.get('label')} "
                 f"gex={dealer.get('gex', 0):.0f} bias={adj.get('bias')} "
                 f"regime={regime.get('regime', '?')} flip={dealer.get('gamma_flip')}")

        return v4_flow

    except Exception as e:
        log.warning(f"v4 prefilter error for {ticker}: {e}")
        return {}


# ─────────────────────────────────────────────────────────
# CHECK TICKER — scalp engine (0-10 DTE) + v4 flow quality gate
# ─────────────────────────────────────────────────────────

def _signal_validation_thresholds(webhook_data: dict) -> tuple[float, float, float]:
    """Returns (warn_pct, reject_pct, hard_block_pct).

    reject_pct -> warn + hard entry line (trade still proceeds, confidence docked).
    hard_block_pct -> absolute block (setup structurally invalid at this drift).
    """
    timeframe = str((webhook_data or {}).get("timeframe") or "").lower()
    is_swing = any(tag in timeframe for tag in ("d", "w", "day", "week")) or bool((webhook_data or {}).get("is_swing"))
    if is_swing:
        return SWING_SIGNAL_WARN_DRIFT_PCT, SWING_SIGNAL_REJECT_DRIFT_PCT, SWING_SIGNAL_HARD_BLOCK_PCT
    return SCALP_SIGNAL_WARN_DRIFT_PCT, SCALP_SIGNAL_REJECT_DRIFT_PCT, SCALP_SIGNAL_HARD_BLOCK_PCT


def _validate_live_signal(ticker: str, live_spot: float, webhook_data: dict | None) -> dict:
    webhook_data = webhook_data or {}
    alert_close = as_float(webhook_data.get("close"), 0.0)
    received_at = as_float(webhook_data.get("received_at_epoch"), 0.0)
    now_ts = time.time()
    signal_age_sec = max(0, int(now_ts - received_at)) if received_at else None
    warn_pct, reject_pct, hard_block_pct = _signal_validation_thresholds(webhook_data)
    allow_extended_recheck = bool(webhook_data.get("allow_recheck_after_stale") or webhook_data.get("recheck_attempt"))
    stale_recheck_allowed = False

    result = {
        "ok": True,
        "label": "LIVE_OK",
        "reason": "",
        "signal_age_sec": signal_age_sec,
        "alert_close": alert_close if alert_close > 0 else None,
        "live_spot": live_spot,
        "drift_pct": None,
        "warn_threshold_pct": warn_pct,
        "reject_threshold_pct": reject_pct,
        "hard_block_threshold_pct": hard_block_pct,
        "confidence_penalty": 0,
        "card_note": "",
        "hard_entry_note": "",
        "recheck_age_allowed": False,
    }

    def _finish(payload: dict) -> dict:
        payload["recheck_age_allowed"] = stale_recheck_allowed
        if stale_recheck_allowed:
            prefix = f"⏳ Planned recheck on older signal ({signal_age_sec}s old)."
            existing = str(payload.get("card_note") or "").strip()
            payload["card_note"] = (prefix + (" " + existing if existing else "")).strip()
            payload["confidence_penalty"] = max(int(payload.get("confidence_penalty") or 0), SIGNAL_WARN_CONF_PENALTY)
        return payload

    if alert_close <= 0 or live_spot <= 0:
        result["label"] = "NO_REFERENCE"
        result["reason"] = "Missing alert close or live spot for validation"
        result["card_note"] = "🟡 Signal validation: alert close unavailable — trade priced from live data only."
        return _finish(result)

    drift_pct = abs((live_spot - alert_close) / alert_close) * 100.0
    result["drift_pct"] = round(drift_pct, 3)

    if signal_age_sec is not None and signal_age_sec > SIGNAL_STALE_AFTER_SEC:
        if allow_extended_recheck and signal_age_sec <= PENDING_RECHECK_MAX_SIGNAL_AGE_SEC:
            stale_recheck_allowed = True
        else:
            result.update({
                "ok": False,
                "label": "STALE_SIGNAL",
                "reason": f"Signal stale ({signal_age_sec}s old > {SIGNAL_STALE_AFTER_SEC}s)",
                "card_note": f"🚫 Signal validation: stale alert ({signal_age_sec}s old).",
            })
            return _finish(result)

    if drift_pct > hard_block_pct:
        result.update({
            "ok": False,
            "label": "HARD_BLOCK",
            "reason": f"Drift {drift_pct:.2f}% exceeds hard block {hard_block_pct:.2f}% — setup invalid",
            "card_note": (
                f"🚫 Price moved too far from signal (${alert_close:.2f} → ${live_spot:.2f}, "
                f"{drift_pct:.2f}%). Setup no longer valid."
            ),
        })
        return _finish(result)

    if drift_pct > reject_pct:
        is_bull_drift = live_spot > alert_close
        buffer = round(alert_close * 0.0005, 2)
        if is_bull_drift:
            hard_entry = round(alert_close + buffer, 2)
            entry_note = (
                f"⏳ Price drifted {drift_pct:.2f}% above TV signal (${alert_close:.2f} → ${live_spot:.2f}). "
                f"Wait for pullback — hard entry line: ${hard_entry:.2f}. "
                f"Do not chase above ${live_spot:.2f}."
            )
        else:
            hard_entry = round(alert_close - buffer, 2)
            entry_note = (
                f"⏳ Price drifted {drift_pct:.2f}% below TV signal (${alert_close:.2f} → ${live_spot:.2f}). "
                f"Wait for bounce — hard entry line: ${hard_entry:.2f}. "
                f"Do not chase below ${live_spot:.2f}."
            )
        result.update({
            "ok": True,
            "label": "DRIFT_WARN_ENTRY",
            "reason": f"Drift {drift_pct:.2f}% — entry guidance given, confidence docked {SIGNAL_HARD_REJECT_CONF_PENALTY}pts",
            "confidence_penalty": SIGNAL_HARD_REJECT_CONF_PENALTY,
            "hard_entry_price": hard_entry,
            "hard_entry_note": entry_note,
            "card_note": entry_note,
        })
        return _finish(result)

    if drift_pct >= warn_pct:
        result.update({
            "label": "DRIFT_WARN",
            "reason": f"Live spot drift {drift_pct:.2f}% exceeded warn threshold {warn_pct:.2f}%",
            "confidence_penalty": SIGNAL_MODERATE_CONF_PENALTY,
            "card_note": f"🟡 Signal validation: live spot drift {drift_pct:.2f}% vs TV alert (${alert_close:.2f} → ${live_spot:.2f}); confidence reduced.",
        })
        return _finish(result)

    if drift_pct >= (warn_pct * 0.5):
        result.update({
            "label": "DRIFT_LIGHT",
            "reason": f"Live spot drift {drift_pct:.2f}% is elevated but within limits",
            "confidence_penalty": SIGNAL_WARN_CONF_PENALTY,
            "card_note": f"🟢 Signal validation: live spot drift only {drift_pct:.2f}% from TV alert (${alert_close:.2f} → ${live_spot:.2f}).",
        })
        return _finish(result)

    result["card_note"] = f"🟢 Signal validation: TV ${alert_close:.2f} vs live ${live_spot:.2f} ({drift_pct:.2f}% drift)."
    return _finish(result)


def _derive_structure_trigger(direction: str, spot: float, structure_ctx: dict | None) -> dict:
    direction = str(direction or "bull").lower()
    ps = ((structure_ctx or {}).get("price_structure") or {})
    local_support = as_float(ps.get("local_support_1"), 0.0)
    local_resistance = as_float(ps.get("local_resistance_1"), 0.0)
    balance_low = as_float(ps.get("local_balance_zone_low"), 0.0)
    balance_high = as_float(ps.get("local_balance_zone_high"), 0.0)
    buffer_mult = max(PENDING_TRIGGER_BUFFER_PCT / 100.0, 0.0)

    if direction == "bear":
        cands = [x for x in (local_support, balance_low) if x > 0]
        trigger_price = min(cands) if cands else None
        buffer = max((trigger_price or spot or 0) * buffer_mult, 0.05) if trigger_price else 0.0
        confirmed = bool(trigger_price and spot <= (trigger_price - buffer))
        desc = f"Break below ${trigger_price:.2f}" if trigger_price else "Break below local support"
    else:
        cands = [x for x in (local_resistance, balance_high) if x > 0]
        trigger_price = max(cands) if cands else None
        buffer = max((trigger_price or spot or 0) * buffer_mult, 0.05) if trigger_price else 0.0
        confirmed = bool(trigger_price and spot >= (trigger_price + buffer))
        desc = f"Break above ${trigger_price:.2f}" if trigger_price else "Break above local resistance"

    return {
        "trigger_price": round(trigger_price, 2) if trigger_price else None,
        "confirmed": confirmed,
        "description": desc,
    }


def _evaluate_entry_trigger(direction: str, spot: float, signal_validation: dict | None, structure_ctx: dict | None) -> dict:
    signal_validation = signal_validation or {}
    label = str(signal_validation.get("label") or "")
    alert_close = as_float(signal_validation.get("alert_close"), 0.0)
    live_spot = as_float(signal_validation.get("live_spot"), spot)
    hard_entry = as_float(signal_validation.get("hard_entry_price"), 0.0)
    grace_mult = max(PENDING_RETRACE_GRACE_PCT / 100.0, 0.0)

    if label == "DRIFT_WARN_ENTRY" and hard_entry > 0:
        drift_above = live_spot > alert_close
        grace = max(hard_entry * grace_mult, 0.03)
        if drift_above:
            ready = spot <= (hard_entry + grace)
            desc = f"Wait for pullback to ~${hard_entry:.2f}"
        else:
            ready = spot >= (hard_entry - grace)
            desc = f"Wait for bounce back to ~${hard_entry:.2f}"
        return {
            "confirmed": ready,
            "pending": not ready,
            "kind": "retrace",
            "trigger_price": round(hard_entry, 2),
            "reason": desc,
        }

    struct_trigger = _derive_structure_trigger(direction, spot, structure_ctx)
    return {
        "confirmed": bool(struct_trigger.get("confirmed")),
        "pending": False,
        "kind": "breakout",
        "trigger_price": struct_trigger.get("trigger_price"),
        "reason": struct_trigger.get("description") or "Trigger required",
    }


def _apply_final_trade_gate(rec: dict, mode: str = "scalp") -> tuple[bool, str, dict]:
    rec = dict(rec or {})
    trade = rec.get("trade") or {}
    reasons = []

    try:
        conf = int(rec.get("confidence") or 0)
    except Exception:
        conf = 0
    min_conf = MIN_CONFIDENCE_TO_TRADE if mode == "scalp" else 58
    if conf < min_conf:
        reasons.append(f"Final confidence {conf}/100 below {min_conf} after overlays")

    try:
        ev = float(trade.get("ev_after_slippage", trade.get("expected_value", trade.get("ev_per_contract", 0))) or 0.0)
    except Exception:
        ev = 0.0
    if ev <= 0:
        reasons.append(f"Final EV ${ev:.2f} is not positive")

    try:
        wp = float(trade.get("win_prob", 0) or 0.0)
    except Exception:
        wp = 0.0
    if wp < MIN_WIN_PROBABILITY:
        reasons.append(f"Final win probability {wp:.0%} below {MIN_WIN_PROBABILITY:.0%}")

    rec["final_gate_ok"] = not reasons
    rec["final_gate_reason"] = "; ".join(reasons)[:220] if reasons else ""
    return (not reasons), rec.get("final_gate_reason", ""), rec


def _build_pending_trade_card(best_rec: dict, signal_validation: dict | None, pending_note: str, attempt: int, total: int, next_delay: int | None) -> str:
    card = format_trade_card(best_rec)
    shared_lines = _um_format_shared_snapshot_lines(best_rec.get("shared_model_snapshot"))
    if shared_lines:
        card += "\n\n" + "\n".join(shared_lines)
    validation_note = (signal_validation or {}).get("card_note")
    prefix_lines = [f"⏳ WATCHLIST — pending recheck {attempt}/{total}", pending_note]
    if next_delay:
        mins = max(1, int(round(next_delay / 60.0)))
        prefix_lines.append(f"Auto recheck scheduled in ~{mins}m.")
    if validation_note and validation_note not in pending_note:
        prefix_lines.append(validation_note)
    return "\n".join([x for x in prefix_lines if x]).strip() + "\n\n" + card


def check_ticker(ticker, direction="bull", webhook_data=None):
    ticker = ticker.strip().upper()
    webhook_data = webhook_data or {"bias": direction, "tier": "2"}

    try:
        # v4.3: Smart prefetch-aware data loading
        # If a prefetch wave is in progress, wait for this ticker's data
        # rather than hitting APIs independently and causing rate limit cascades
        cached = _prefetch_get(ticker)
        if not cached and _prefetch_thread_active:
            wait_start = time.time()
            while (time.time() - wait_start) < PREFETCH_WORKER_WAIT_SEC:
                time.sleep(2)
                cached = _prefetch_get(ticker)
                if cached:
                    log.info(f"check_ticker({ticker}): prefetch ready ({time.time()-wait_start:.0f}s wait)")
                    break
                if not _prefetch_thread_active:
                    break
            if not cached:
                log.info(f"check_ticker({ticker}): prefetch wait expired — live fetch")

        if cached and cached.get("spot") and cached.get("chains"):
            spot = cached["spot"]
            chains = cached["chains"]
            enrichment = cached.get("enrichment", {})
            candle_closes = cached.get("candle_closes", [])
            regime = cached.get("regime") or get_current_regime()
            vol_regime = cached.get("vol_regime") or get_canonical_vol_regime(ticker, candle_closes)
            v4_flow = cached.get("v4_flow", {})
            has_earnings = enrichment.get("has_earnings", False)
            earnings_warn = enrichment.get("earnings_warn")
            log.info(f"check_ticker({ticker}): using prefetch cache")
        else:
            # No cache — fetch live once, then store so the opposite side of /check can reuse it.
            spot = get_spot(ticker); chains = get_options_chain(ticker)
            # Non-blocking earnings check — same 2s cap as prefetch path.
            enrichment = {"has_earnings": False, "earnings_warn": None}
            try:
                import threading as _threading
                _enrich_result = {}
                def _fetch_enrich_live():
                    try:
                        _enrich_result.update(enrich_ticker(ticker))
                    except Exception:
                        pass
                _et = _threading.Thread(target=_fetch_enrich_live, daemon=True)
                _et.start()
                _et.join(timeout=2.0)
                if _enrich_result:
                    enrichment = _enrich_result
            except Exception:
                pass
            has_earnings = enrichment.get("has_earnings", False)
            earnings_warn = enrichment.get("earnings_warn")
            candle_closes = get_daily_candles(ticker, days=RV_LOOKBACK_DAYS + 5)
            regime = get_current_regime()
            vol_regime = get_canonical_vol_regime(ticker, candle_closes)
            v4_flow = _run_v4_prefilter(ticker, spot, chains, candle_closes)
            _prefetch_set(ticker, {
                "spot": spot,
                "chains": chains,
                "enrichment": enrichment,
                "candle_closes": candle_closes,
                "regime": regime,
                "vol_regime": vol_regime,
                "v4_flow": v4_flow,
            })
            log.info(f"check_ticker({ticker}): live fetch cached for reuse")
        has_dividend = False
        structure_ctx = _build_canonical_structure_context(ticker, spot, _get_daily_ohlcv_rows(ticker, days=90))

        log.info(f"check_ticker({ticker}): spot={spot} expirations={len(chains)} "
                 f"earnings={has_earnings} candles={len(candle_closes)} direction={direction}"
                 f" vol={vol_regime.get('label','?')}"
                 f"{' v4=' + v4_flow.get('composite_regime', '?') if v4_flow else ''}")

        signal_validation = _validate_live_signal(ticker, spot, webhook_data)
        webhook_data = dict(webhook_data or {})
        webhook_data["live_spot"] = spot
        webhook_data["signal_validation"] = signal_validation
        webhook_data["vol_regime_module"] = vol_regime

        if not signal_validation.get("ok", True):
            reason = signal_validation.get("reason") or "signal validation failed"
            trade_journal.log_signal(ticker, webhook_data, outcome="rejected", reason=reason[:200])
            _log_signal_dataset_event(ticker, webhook_data, outcome="rejected_validation", reason=reason, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot, expirations_checked=len(chains) if chains else 0, vol_regime=vol_regime)
            log.info(f"check_ticker({ticker}): rejected by validation — {reason}")
            return {
                "ticker": ticker, "ok": False, "posted": False,
                "reason": reason,
                "confidence": None,
                "signal_validation": signal_validation,
            }

        all_recs = []; all_reasons = []
        for exp, dte, contracts in chains:
            rec = recommend_trade(
                ticker=ticker, spot=spot, contracts=contracts, dte=dte,
                expiration=exp, webhook_data=webhook_data,
                has_earnings=has_earnings, has_dividend=has_dividend,
                candle_closes=candle_closes, regime=regime,
                v4_flow=v4_flow,
                vol_regime=vol_regime,
            )
            if rec.get("ok"):
                rec = _apply_canonical_structure_overlay_to_rec(rec, structure_ctx, mode="scalp")
                all_recs.append(rec)
                log.info(f"  {exp} (DTE {dte}): ✅ {direction} trade found")
            else:
                reason = rec.get("reason", "unknown")
                all_reasons.append(f"DTE {dte} ({exp}): {reason}")
                log.info(f"  {exp} (DTE {dte}): ❌ {reason}")

        if not all_recs:
            combined_reason = "No valid spreads across any expiration"
            if all_reasons:
                combined_reason += "\n" + "\n".join(all_reasons[:4])
            trade_journal.log_signal(ticker, webhook_data, outcome="rejected", reason=combined_reason[:200])
            _log_signal_dataset_event(ticker, webhook_data, outcome="rejected_no_setup", reason=combined_reason, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot, expirations_checked=len(chains), vol_regime=vol_regime)
            return {"ticker": ticker, "ok": False, "posted": False,
                    "reason": combined_reason, "confidence": None}

        def rec_score(r):
            trade = r.get("trade", {}); ror = trade.get("ror", 0)
            width = trade.get("width", 5); dte_val = r.get("dte", 5)
            width_bonus = 0.3 if width <= 1.0 else 0.1 if width <= 2.5 else 0
            dte_bonus = 0.1 * (1.0 / (1 + abs(dte_val - TARGET_DTE)))
            conf_bonus = (float(r.get("confidence") or 0) / 100.0) * 0.8
            struct_bonus = (float(r.get("structure_overlay_score") or 0) / 100.0)
            return ror + width_bonus + dte_bonus + conf_bonus + struct_bonus

        all_recs.sort(key=rec_score, reverse=True)
        best_rec = all_recs[0]

        other_exps = []; seen_exps = {best_rec.get("exp")}
        for r in all_recs[1:]:
            if r.get("exp") not in seen_exps:
                seen_exps.add(r.get("exp")); other_exps.append(r)

        penalty = int((signal_validation or {}).get("confidence_penalty") or 0)
        if penalty > 0:
            base_conf = int(best_rec.get("confidence", 0) or 0)
            best_rec["confidence_pre_validation"] = base_conf
            best_rec["confidence"] = max(0, base_conf - penalty)
            best_rec["signal_validation_penalty"] = penalty

        best_rec = _apply_canonical_vol_overlay_to_rec(best_rec, vol_regime, mode="scalp")
        best_rec = _apply_canonical_structure_overlay_to_rec(best_rec, structure_ctx, mode="scalp")
        best_rec["shared_model_snapshot"] = _um_build_shared_model_snapshot(
            ticker=ticker,
            spot=spot,
            vol_regime=vol_regime,
            structure_ctx=structure_ctx,
            rec=best_rec,
            v4_flow=v4_flow,
            mode="scalp",
        )
        trigger_eval = _evaluate_entry_trigger(direction, spot, signal_validation, structure_ctx)
        best_rec["entry_trigger_kind"] = trigger_eval.get("kind")
        best_rec["entry_trigger_price"] = trigger_eval.get("trigger_price")
        best_rec["entry_trigger_confirmed"] = trigger_eval.get("confirmed")
        best_rec["entry_trigger_reason"] = trigger_eval.get("reason")

        if trigger_eval.get("pending"):
            scheduled, next_delay, attempt_idx, total_attempts = _schedule_pending_recheck(
                ticker, direction, webhook_data, reason=trigger_eval.get("reason", "pending retrace")
            )
            pending_reason = trigger_eval.get("reason") or "Waiting for a better entry"
            if scheduled and next_delay:
                pending_reason = f"{pending_reason} — recheck {attempt_idx + 1}/{total_attempts} queued"
            best_rec["reason"] = pending_reason
            best_rec["rejection_bucket"] = "pending_retrace"
            card = _build_pending_trade_card(best_rec, signal_validation, pending_reason, attempt_idx + 1, total_attempts, next_delay)
            trade_journal.log_signal(ticker, webhook_data, outcome="pending", confidence=best_rec.get("confidence"), reason=pending_reason[:200])
            _log_signal_dataset_event(
                ticker, webhook_data, outcome="pending_recheck", reason=pending_reason, best_rec=best_rec,
                signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot,
                expirations_checked=len(chains), vol_regime=vol_regime
            )
            return {
                "ticker": ticker, "ok": False, "posted": False, "pending": True, "card": card,
                "reason": pending_reason, "confidence": best_rec.get("confidence")
            }

        has_confirmed_trigger = bool(
            (webhook_data or {}).get("source") != "check"
            and (signal_validation or {}).get("ok")
            and trigger_eval.get("confirmed")
        )
        eff_allowed, eff_reason, best_rec = _um_apply_effective_regime_gate_to_rec(
            best_rec,
            best_rec.get("shared_model_snapshot"),
            mode="scalp",
            has_confirmed_trigger=has_confirmed_trigger,
        )
        best_rec["shared_model_snapshot"] = _um_build_shared_model_snapshot(
            ticker=ticker,
            spot=spot,
            vol_regime=vol_regime,
            structure_ctx=structure_ctx,
            rec=best_rec,
            v4_flow=v4_flow,
            mode="scalp",
        )
        if not eff_allowed:
            best_rec["ok"] = False
            best_rec["reason"] = eff_reason
            is_trigger_wait = bool(best_rec.get("effective_regime_requires_trigger") and not has_confirmed_trigger)
            best_rec["rejection_bucket"] = best_rec.get("rejection_bucket") or ("pending_trigger" if is_trigger_wait else "effective_regime_block")
            if is_trigger_wait:
                scheduled, next_delay, attempt_idx, total_attempts = _schedule_pending_recheck(
                    ticker, direction, webhook_data, reason=trigger_eval.get("reason") or eff_reason
                )
                pending_reason = trigger_eval.get("reason") or eff_reason or "Trigger required before entry"
                if scheduled and next_delay:
                    pending_reason = f"{pending_reason} — recheck {attempt_idx + 1}/{total_attempts} queued"
                card = _build_pending_trade_card(best_rec, signal_validation, pending_reason, attempt_idx + 1, total_attempts, next_delay)
                trade_journal.log_signal(ticker, webhook_data, outcome="pending", confidence=best_rec.get("confidence"), reason=pending_reason[:200])
                _log_signal_dataset_event(
                    ticker, webhook_data, outcome="pending_recheck", reason=pending_reason, best_rec=best_rec,
                    signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot,
                    expirations_checked=len(chains), vol_regime=vol_regime
                )
                return {
                    "ticker": ticker, "ok": False, "posted": False, "pending": True, "card": card,
                    "reason": pending_reason, "confidence": best_rec.get("confidence")
                }

            trade_journal.log_signal(ticker, webhook_data, outcome="rejected", reason=eff_reason[:200])
            _log_signal_dataset_event(
                ticker,
                webhook_data,
                outcome="rejected_effective_regime",
                reason=eff_reason,
                best_rec=best_rec,
                signal_validation=signal_validation,
                regime=regime,
                v4_flow=v4_flow,
                spot=spot,
                expirations_checked=len(chains),
                vol_regime=vol_regime,
            )
            card = format_trade_card(best_rec)
            shared_lines = _um_format_shared_snapshot_lines(best_rec.get("shared_model_snapshot"))
            if shared_lines:
                card += "\n\n" + "\n".join(shared_lines)
            validation_note = (signal_validation or {}).get("card_note")
            if validation_note:
                card = validation_note + "\n\n" + card
            return {"ticker": ticker, "ok": False, "posted": True, "card": card, "reason": eff_reason, "confidence": best_rec.get("confidence")}

        final_allowed, final_reason, best_rec = _apply_final_trade_gate(best_rec, mode="scalp")
        if not final_allowed:
            best_rec["ok"] = False
            best_rec["reason"] = final_reason
            best_rec["rejection_bucket"] = best_rec.get("rejection_bucket") or "post_overlay_gate"
            trade_journal.log_signal(ticker, webhook_data, outcome="rejected", reason=final_reason[:200])
            _log_signal_dataset_event(
                ticker, webhook_data, outcome="rejected_post_overlay", reason=final_reason, best_rec=best_rec,
                signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot,
                expirations_checked=len(chains), vol_regime=vol_regime
            )
            card = format_trade_card(best_rec)
            shared_lines = _um_format_shared_snapshot_lines(best_rec.get("shared_model_snapshot"))
            if shared_lines:
                card += "\n\n" + "\n".join(shared_lines)
            validation_note = (signal_validation or {}).get("card_note")
            if validation_note:
                card = validation_note + "\n\n" + card
            return {
                "ticker": ticker, "ok": False, "posted": False, "reason": final_reason,
                "card": card, "confidence": best_rec.get("confidence")
            }
        trade = best_rec.get("trade", {})

        if is_duplicate_trade(ticker, direction, trade.get("short"), trade.get("long")):
            trade_journal.log_signal(ticker, webhook_data, outcome="duplicate", confidence=best_rec.get("confidence"))
            _log_signal_dataset_event(ticker, webhook_data, outcome="duplicate", reason="Duplicate trade in dedup window", best_rec=best_rec, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow, spot=spot, expirations_checked=len(chains), vol_regime=vol_regime)
            return {"ticker": ticker, "ok": True, "posted": False, "reason": "Duplicate trade in dedup window"}

        risk_result = risk_manager.check_risk_limits(
            ticker=ticker, debit=trade.get("debit", 0),
            contracts=best_rec.get("contracts", 1), regime=regime, direction=direction,
        )

        card = format_trade_card(best_rec)
        shared_lines = _um_format_shared_snapshot_lines(best_rec.get("shared_model_snapshot"))
        if shared_lines:
            card += "\n\n" + "\n".join(shared_lines)
        validation_note = (signal_validation or {}).get("card_note")
        if validation_note:
            card = validation_note + "\n\n" + card

        if not risk_result["allowed"]:
            block_reasons = "; ".join(risk_result["blocks"])
            card = "🚫 RISK LIMIT HIT — DO NOT ENTER\n" + block_reasons + "\n\n" + card
        if risk_result.get("warnings"):
            card += "\n⚠️ Risk: " + " | ".join(risk_result["warnings"][:3])
        if other_exps:
            alt_lines = ["\n📅 Other Expirations:"]
            for r in other_exps[:3]:
                t = r.get("trade", {})
                alt_lines.append(f"  DTE {r['dte']} ({r['exp']}): ${t['debit']:.2f} on ${t['width']} wide | RoR {t['ror']:.0%} | {t['long']}/{t['short']}")
            card += "\n".join(alt_lines)
        if has_earnings and earnings_warn:
            card = earnings_warn + "\n\n" + card

        conf = best_rec.get("confidence", 0)
        log.info(f"Trade card built: {ticker} {direction} conf={conf}/100")
        mark_trade_sent(ticker, direction, trade.get("short"), trade.get("long"))
        trade_journal.log_signal(ticker, webhook_data,
            outcome="trade_opened" if risk_result["allowed"] else "risk_blocked",
            confidence=best_rec.get("confidence"))
        _log_signal_dataset_event(
            ticker, webhook_data,
            outcome="trade_opened" if risk_result["allowed"] else "risk_blocked",
            reason=("; ".join(risk_result.get("blocks", [])) if not risk_result["allowed"] else ""),
            best_rec=best_rec, signal_validation=signal_validation, regime=regime, v4_flow=v4_flow,
            spot=spot, expirations_checked=len(chains), vol_regime=vol_regime
        )

        # v4.3: Cache card for /tradecard retrieval (same as wave digest path)
        try:
            cache_key = f"tradecard:{ticker.upper()}"
            store_set(cache_key, card, ttl=DIGEST_CARD_CACHE_TTL_SEC)
            log.debug(f"Trade card cached: {cache_key}")
        except Exception:
            pass  # non-critical

        return {
            "ticker": ticker, "ok": True, "posted": True, "card": card,
            "confidence": best_rec.get("confidence"), "trade": trade,
            "expirations_checked": len(chains),
            "signal_validation": signal_validation,
        }
    except Exception as e:
        log.error(f"check_ticker({ticker}): {type(e).__name__}: {e}")
        if _is_http_429(e) or "rate limit" in str(e).lower():
            return {"ticker": ticker, "ok": False, "posted": False,
                    "reason": f"Rate limit hit while checking {ticker}. Try again shortly.",
                    "error": f"{type(e).__name__}: {str(e)[:160]}"}
        return {"ticker": ticker, "ok": False, "posted": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    r = _get_redis()
    queue_depth = r.llen(REDIS_QUEUE_KEY) if r else _mem_signal_queue.qsize()
    return jsonify({"status": "ok", "redis": r is not None, "queue_depth": queue_depth,
                    "workers": QUEUE_WORKERS, "engine_version": SCHEMA_VERSION})

@app.route("/debug", methods=["GET"])
def debug():
    r = _get_redis()
    queue_depth = r.llen(REDIS_QUEUE_KEY) if r else _mem_signal_queue.qsize()
    return jsonify({
        "TELEGRAM_set": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "MARKETDATA_set": bool(MARKETDATA_TOKEN),
        "REDIS_connected": r is not None,
        "REDIS_queue_depth": queue_depth,
        "WATCHLIST_len": len([t for t in WATCHLIST.split(",") if t.strip()]),
        "ENGINE_VERSION": SCHEMA_VERSION,
        "QUEUE_WORKERS": QUEUE_WORKERS,
        "SIGNAL_TTL_S": SIGNAL_TTL_SEC,
        "DEDUP_TTL_S": DEDUP_TTL_SECONDS,
        "ALLOWED_DIRECTIONS": ALLOWED_DIRECTIONS,
        "PORTFOLIO_CHANNEL_set": bool(TELEGRAM_PORTFOLIO_CHAT_ID),
        "MOM_CHANNEL_set": bool(TELEGRAM_MOM_PORTFOLIO_CHAT_ID),
        "API_CACHE": _cached_md.get_stats(),
        "SIGNAL_VALIDATION": {
            "SCALP_WARN_PCT": SCALP_SIGNAL_WARN_DRIFT_PCT,
            "SCALP_REJECT_PCT": SCALP_SIGNAL_REJECT_DRIFT_PCT,
            "SWING_WARN_PCT": SWING_SIGNAL_WARN_DRIFT_PCT,
            "SWING_REJECT_PCT": SWING_SIGNAL_REJECT_DRIFT_PCT,
            "STALE_AFTER_SEC": SIGNAL_STALE_AFTER_SEC,
        },
    })

@app.route("/tgtest", methods=["GET"])
def tgtest():
    st, body = post_to_telegram(f"✅ Telegram test OK (v4.0 engine {SCHEMA_VERSION})")
    return jsonify({"status": st, "body": body})


# ─────────────────────────────────────────────────────────
# TELEGRAM WEBHOOK
# ─────────────────────────────────────────────────────────

@app.route("/telegram_webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    if secret != os.getenv("TELEGRAM_WEBHOOK_SECRET", ""):
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    message = data.get("message") or data.get("edited_message") or {}
    if not message:
        return jsonify({"ok": True})
    text = message.get("text", ""); chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str(message.get("from", {}).get("id", ""))
    if not text.startswith("/"):
        return jsonify({"ok": True})

    cmd_word = text.split()[0].lower() if text.split() else text.lower()
    dedup_key = f"cmd_dedup:{user_id}:{cmd_word}"
    if store_exists(dedup_key):
        log.info(f"Command dedup hit — suppressing: {cmd_word} from {user_id}")
        return jsonify({"ok": True})
    store_set(dedup_key, "1", ttl=8)
    tickers = [t.strip().upper() for t in WATCHLIST.split(",") if t.strip()]

    def run_command():
        handle_command(
            user_id=user_id, chat_id=chat_id, text=text,
            scan_fn=lambda t: check_ticker(t),
            full_scan_fn=lambda: scan_watchlist_internal(tickers),
            check_fn=check_ticker, watchlist=tickers,
            get_spot_fn=get_spot, md_get_fn=md_get, post_fn=post_to_telegram,
            get_portfolio_chat_id_fn=get_portfolio_chat_id,
            get_regime_fn=get_current_regime,
            post_em_card_fn=_post_em_card, post_monitor_card_fn=_post_monitor_card,
            post_checkswing_card_fn=_post_checkswing_card,
            thesis_engine=get_thesis_engine(),
            post_income_scan_fn=_income_scan_fn,
            post_income_score_fn=_income_score_fn,
        )

    threading.Thread(target=run_command, daemon=True).start()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────
# TRADINGVIEW WEBHOOK — scalp signals (/tv)
# ─────────────────────────────────────────────────────────

@app.route("/tv", methods=["POST"])
def tv_webhook():
    data = request.get_json(silent=True) or {}
    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "").strip().lower()
    tier   = str(data.get("tier") or "").strip()
    close  = as_float(data.get("close"), None)

    validation_errors = []
    if not ticker: validation_errors.append("missing 'ticker'")
    elif not ticker.replace(".", "").isalpha(): validation_errors.append(f"invalid ticker '{ticker}'")
    if bias not in ("bull", "bear"): validation_errors.append(f"invalid bias '{bias}'")
    if tier not in ("1", "2", "3"): validation_errors.append(f"invalid tier '{tier}'")
    if close is None or close <= 0: validation_errors.append(f"invalid close '{data.get('close')}'")

    if validation_errors:
        reason = "; ".join(validation_errors)
        log.warning(f"TV webhook rejected — {reason}")
        return jsonify({"error": "invalid_payload", "reason": reason}), 400

    log.info(f"TV signal: {ticker} bias={bias} tier={tier} close={close}")

    webhook_data = {
        "tier": tier, "bias": bias, "close": close, "time": data.get("time", ""),
        "received_at_epoch": time.time(),
        "ema5": as_float(data.get("ema5")), "ema12": as_float(data.get("ema12")),
        "ema_dist_pct": as_float(data.get("ema_dist_pct")),
        "macd_hist": as_float(data.get("macd_hist")),
        "macd_line": as_float(data.get("macd_line")),
        "signal_line": as_float(data.get("signal_line")),
        "wt1": as_float(data.get("wt1")), "wt2": as_float(data.get("wt2")),
        "rsi_mfi": as_float(data.get("rsi_mfi")),
        "rsi_mfi_bull": data.get("rsi_mfi_bull") in (True, "true"),
        "stoch_k": as_float(data.get("stoch_k")), "stoch_d": as_float(data.get("stoch_d")),
        "vwap": as_float(data.get("vwap")),
        "above_vwap": data.get("above_vwap") in (True, "true"),
        "htf_confirmed": data.get("htf_confirmed") in (True, "true"),
        "htf_converging": data.get("htf_converging") in (True, "true"),
        "daily_bull": data.get("daily_bull") in (True, "true"),
        "volume": as_float(data.get("volume")), "timeframe": data.get("timeframe", ""),
    }

    def _build_signal_msg():
        tier_emoji = "🥇" if tier == "1" else "🥈" if tier == "2" else "📢"
        wt2 = webhook_data.get("wt2") or 0
        wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
        trend_str = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                     else "🟡 Converging" if webhook_data["htf_converging"] else "❌ Diverging")
        dir_emoji = "🐻" if bias == "bear" else "🐂"
        return "\n".join([
            f"{tier_emoji} TV Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
            f"Close: ${close:.2f} | {data.get('timeframe', '')} timeframe",
            f"1H Trend: {trend_str} | Daily: {'🟢' if webhook_data['daily_bull'] else '🔴'}",
            f"Wave: {wave_zone} (wt2={wt2:.1f})",
            f"VWAP: {'Above ✅' if webhook_data['above_vwap'] else 'Below'} | "
            f"RSI+MFI: {'Buying ✅' if webhook_data['rsi_mfi_bull'] else 'Selling'}", "",
        ])

    signal_msg = _build_signal_msg()
    _enqueue_signal("tv", ticker, bias, webhook_data, signal_msg)
    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# SWING WEBHOOK (/swing)
# ─────────────────────────────────────────────────────────

@app.route("/swing", methods=["POST"])
def swing_webhook():
    data = request.get_json(silent=True) or {}
    if TV_WEBHOOK_SECRET:
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "").strip().upper()
    bias   = (data.get("bias")   or "").strip().lower()
    tier   = str(data.get("tier") or "").strip()
    close  = as_float(data.get("close"), None)

    validation_errors = []
    if not ticker: validation_errors.append("missing 'ticker'")
    elif not ticker.replace(".", "").isalpha(): validation_errors.append(f"invalid ticker '{ticker}'")
    if bias not in ("bull", "bear"): validation_errors.append(f"invalid bias '{bias}'")
    if tier not in ("1", "2", "3"): validation_errors.append(f"invalid tier '{tier}'")
    if close is None or close <= 0: validation_errors.append(f"invalid close '{data.get('close')}'")

    if validation_errors:
        reason = "; ".join(validation_errors)
        log.warning(f"Swing webhook rejected — {reason}")
        return jsonify({"error": "invalid_payload", "reason": reason}), 400

    log.info(f"Swing signal: {ticker} bias={bias} tier={tier}")

    webhook_data = {
        "tier": tier, "bias": bias, "close": close, "time": data.get("time", ""),
        "received_at_epoch": time.time(),
        "fib_level": data.get("fib_level", "61.8"),
        "fib_distance_pct": as_float(data.get("fib_distance_pct"), 2.0),
        "fib_high": as_float(data.get("fib_high")), "fib_low": as_float(data.get("fib_low")),
        "fib_range": as_float(data.get("fib_range")),
        "fib_ext_127": as_float(data.get("fib_ext_127")),
        "fib_ext_162": as_float(data.get("fib_ext_162")),
        "weekly_bull": data.get("weekly_bull") in (True, "true"),
        "weekly_bear": data.get("weekly_bear") in (True, "true"),
        "htf_confirmed": data.get("htf_confirmed") in (True, "true"),
        "htf_converging": data.get("htf_converging") in (True, "true"),
        "daily_bull": data.get("daily_bull") in (True, "true"),
        "rsi": as_float(data.get("rsi")),
        "rsi_mfi_bull": data.get("rsi_mfi_bull") in (True, "true"),
        "vol_contracting": data.get("vol_contracting") in (True, "true"),
        "vol_expanding": data.get("vol_expanding") in (True, "true"),
        "volume": as_float(data.get("volume")), "timeframe": data.get("timeframe", "D"),
    }

    tier_emoji = "🥇" if tier == "1" else "🥈"
    dir_emoji  = "🐻" if bias == "bear" else "🐂"
    fib_level  = webhook_data["fib_level"]
    fib_emojis = {"61.8": "🌟", "50.0": "⭐", "38.2": "✨", "78.6": "💫"}
    fib_emoji  = fib_emojis.get(str(fib_level), "📐")
    weekly_str = "🟢 Bull" if webhook_data["weekly_bull"] else "🔴 Bear"
    daily_str  = ("✅ Confirmed" if webhook_data["htf_confirmed"]
                  else "🟡 Converging" if webhook_data["htf_converging"] else "❌ Diverging")
    vol_str    = "🟢 Contracting" if webhook_data["vol_contracting"] else "📊 Normal"

    signal_msg = "\n".join([
        f"{tier_emoji} SWING Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
        f"Fib: {fib_emoji} {fib_level}% level ({webhook_data['fib_distance_pct']:.1f}% away)",
        f"Close: ${close:.2f} | {webhook_data['timeframe']} chart",
        f"Weekly: {weekly_str} | Daily: {daily_str}", f"Volume: {vol_str}", "",
    ])
    _enqueue_signal("swing", ticker, bias, webhook_data, signal_msg)
    return jsonify({"status": "accepted", "ticker": ticker, "tier": tier}), 200


# ─────────────────────────────────────────────────────────
# 0DTE EM TRIGGER
# ─────────────────────────────────────────────────────────

EM_SCHEDULE_TIMES_CT = [(8, 45), (9, 15), (14, 45)]
# 8:45 AM  — pre-open / first-minute snap. Chain greeks may be stale (no live market).
# 9:15 AM  — mid-morning refresh (v15). Market has been open 45 min. Greeks are live.
#            This ensures thesis monitor gets ATM delta/premium for premium stop.
#            Also refreshes bias score with live price action data.
# 2:45 PM  — afternoon / next-day preview for power hour entries.
EM_TICKERS = ["SPY", "QQQ"]

@app.route("/em", methods=["POST", "GET"])
def em_trigger():
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403
    tickers = [t.strip().upper() for t in
               (data.get("tickers") or ",".join(EM_TICKERS)).split(",") if t.strip()]
    session = data.get("session", "manual")

    def run():
        for ticker in tickers:
            _post_em_card(ticker, session)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "accepted", "tickers": tickers, "session": session})


# ─────────────────────────────────────────────────────────
# WATCHLIST SCAN — DISABLED
# ─────────────────────────────────────────────────────────

def scan_watchlist_internal(tickers: list, max_posts: int = 6):
    log.info("scan_watchlist_internal called but scan is disabled")
    return

@app.route("/scan", methods=["POST"])
def scan_watchlist():
    """v5.0: Re-enabled. Triggers an immediate scan cycle via the active scanner."""
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    if _scanner and _scanner._running:
        from active_scanner import TIER_A, TIER_B, TIER_C
        def _force_scan():
            for t in TIER_A + TIER_B + TIER_C:
                _scanner._last_scan[t] = 0
                _scanner._scan_ticker(t)
        threading.Thread(target=_force_scan, daemon=True).start()
        return jsonify({"status": "accepted", "tickers": _scanner.watchlist_size})
    else:
        return jsonify({"status": "error", "reason": "Scanner not running — set ACTIVE_SCANNER_ENABLED=True"}), 503


# ─────────────────────────────────────────────────────────
# EM RECONCILER — compares predictions to actual EOD prices
# ─────────────────────────────────────────────────────────

@app.route("/reconcile", methods=["POST", "GET"])
def reconcile_route():
    """
    Trigger EM prediction reconciliation.
    Compares past EM cards against actual EOD closes.
    POST/GET with secret param for auth.
    Optional params:
      - lookback: days to scan (default 5)
      - post: "true" to post summary to Telegram
    """
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    lookback = int(data.get("lookback", request.args.get("lookback", 5)))
    post_summary = str(data.get("post", request.args.get("post", "true"))).lower() == "true"

    def run():
        try:
            r = _get_redis()
            if not r:
                log.warning("Reconciler: no Redis — cannot scan")
                return

            # Build the EOD fetcher using app.py's md_get
            def eod_fetcher(ticker, date_str):
                return fetch_eod_close_marketdata(ticker, date_str, md_get)

            entries = reconcile_em_predictions(
                redis_client=r,
                fetch_eod_close=eod_fetcher,
                store_set=store_set,
                lookback_days=lookback,
            )

            if not entries:
                log.info("Reconciler: no entries to process")
                return

            stats = compute_accuracy_stats(entries)
            report = format_accuracy_report(stats)

            log.info(f"Reconciler stats: n={stats.get('n')} "
                     f"1σ={stats.get('in_1_sigma_pct')}% "
                     f"dir={stats.get('direction_correct_pct')}%")

            if post_summary and stats.get("n", 0) > 0:
                post_to_telegram(report)
                log.info("Reconciler: accuracy report posted to Telegram")

        except Exception as e:
            log.error(f"Reconciler error: {e}", exc_info=True)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "accepted", "lookback_days": lookback})


def _run_reconciler_auto():
    """Called by the EM scheduler at 4:15 PM CT to auto-reconcile."""
    try:
        r = _get_redis()
        if not r:
            return

        def eod_fetcher(ticker, date_str):
            return fetch_eod_close_marketdata(ticker, date_str, md_get)

        entries = reconcile_em_predictions(
            redis_client=r,
            fetch_eod_close=eod_fetcher,
            store_set=store_set,
            lookback_days=3,
        )

        if entries:
            stats = compute_accuracy_stats(entries)
            if stats.get("n", 0) >= 5:
                # Only post summary once we have enough data
                report = format_accuracy_report(stats)
                post_to_telegram(report)
                log.info(f"Auto-reconciler: posted accuracy report ({stats['n']} entries)")
            else:
                log.info(f"Auto-reconciler: {stats.get('n', 0)} entries — waiting for more data before posting")
    except Exception as e:
        log.error(f"Auto-reconciler error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────
# HOLDINGS SENTIMENT SCAN
# ─────────────────────────────────────────────────────────

@app.route("/holdings_scan", methods=["POST"])
def holdings_scan():
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.headers.get("X-Scan-Secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    def run_scan():
        from sentiment_report import generate_sentiment_report
        for account in ("brad", "mom"):
            try:
                report = generate_sentiment_report(md_get, account=account)
                target_chat = get_portfolio_chat_id(account)
                post_to_telegram(report, chat_id=target_chat)
            except Exception as e:
                log.error(f"Holdings scan error ({account}): {e}")
                post_to_telegram(f"⚠️ Holdings scan failed ({account}): {type(e).__name__}",
                                chat_id=get_portfolio_chat_id(account))

    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "accepted"})


# ─────────────────────────────────────────────────────────
# CHAIN DATA CACHE
# ─────────────────────────────────────────────────────────

_CHAIN_CACHE_TTL = 60
_chain_cache: dict = {}
_chain_cache_lock = threading.Lock()

def _chain_cache_get(ticker: str, expiry: str):
    key = (ticker.upper(), expiry)
    with _chain_cache_lock:
        entry = _chain_cache.get(key)
        if entry and (time.monotonic() - entry[3]) < _CHAIN_CACHE_TTL:
            return entry[0], entry[1], entry[2]
        if entry:
            del _chain_cache[key]
    return None

def _chain_cache_set(ticker: str, expiry: str, data, spot: float):
    key = (ticker.upper(), expiry)
    with _chain_cache_lock:
        now = time.monotonic()
        stale = [k for k, v in _chain_cache.items() if (now - v[3]) >= _CHAIN_CACHE_TTL]
        for k in stale:
            del _chain_cache[k]
        _chain_cache[key] = (data, spot, expiry, now)


def _get_0dte_chain(ticker: str, target_date_str: str = None) -> tuple:
    try:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = target_date_str or today_utc
        cached = _chain_cache_get(ticker, target)
        if cached:
            return cached
        spot = get_spot(ticker)
        data = _cached_md.get_chain(ticker, target, strike_limit=20)  # v5.1.1: strikeLimit saves credits
        if not isinstance(data, dict) or data.get("s") != "ok" or not data.get("optionSymbol"):
            exps = get_expirations(ticker)
            future_exps = [e for e in exps if e >= target]
            if not future_exps:
                return None, None, None
            target = future_exps[0]
            cached = _chain_cache_get(ticker, target)
            if cached:
                return cached
            data = _cached_md.get_chain(ticker, target, strike_limit=20)  # v5.1.1: strikeLimit saves credits
            if not isinstance(data, dict) or data.get("s") != "ok":
                return None, None, None
        if not data.get("optionSymbol"):
            return None, None, None
        _chain_cache_set(ticker, target, data, spot)
        return data, spot, target
    except Exception as e:
        log.warning(f"Chain fetch failed for {ticker}: {e}")
        return None, None, None


def _get_next_trading_day(from_date) -> str:
    d = from_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────
# V4 ENGINE INTEGRATION — _get_0dte_iv
# Uses OI cache for oi_change, stock data for liquidity, liquid_index for spread detection
# ─────────────────────────────────────────────────────────

def _get_0dte_iv(ticker: str, target_date_str: str = None) -> tuple:
    """
    Full v4 institutional chain analysis.
    Returns: (iv, spot, expiration, engine_result, walls, skew, pcr, vix, v4_result)
    The 9th element (v4_result) contains confidence, downgrades, trade_sign, audit, vol_regime.
    """
    empty = (None, None, None, None, {}, {}, {}, {}, {})
    try:
        data, spot, target = _get_0dte_chain(ticker, target_date_str)
        if data is None:
            return empty

        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        from datetime import date as _date
        try:
            dte = max((_date.fromisoformat(target) - _date.fromisoformat(today_utc)).days, 0)
        except Exception:
            dte = 0

        # ── OI change: diff current OI against cached prior snapshot ──
        _oi_cache.apply_oi_changes_to_chain(ticker, target, data)

        # ── OHLC bars for RV ──
        bars = _get_ohlc_bars(ticker, days=65)

        # ── Estimate liquidity from stock data ──
        adv, spread = _estimate_liquidity(ticker, spot)

        v4 = run_institutional_snapshot(
            chain_data=data, spot=spot, dte=max(dte, 0.5),
            recent_bars=bars, is_0dte=(dte == 0),
            avg_daily_dollar_volume=adv,
            bid_ask_spread_pct=spread,
            liquid_index=_is_liquid(ticker),
        )

        # ── Save current OI as reference for next run ──
        _oi_cache.save_snapshot(ticker, target, data)
        if _oi_tracker:
            _oi_tracker.record_chain(ticker, target, data, spot=spot)
        # Intraday flow detection
        try:
            if _flow_detector:
                _persistent_state.save_oi_baseline(ticker, target,
                    _oi_cache._parse_chain_oi(data) if _oi_cache else {})
                flow_alerts = _flow_detector.check_intraday_flow(ticker, target, data, spot)
                postable = [fa for fa in flow_alerts
                           if fa.get("should_alert") and
                           fa.get("flow_level") in ("significant", "extreme")]
                if postable:
                    for msg in _flow_detector.format_grouped_flow_alerts(postable):
                        try:
                            post_to_telegram(msg)
                        except Exception:
                            pass
        except Exception:
            pass

        iv_meta = {"iv": v4.get("iv"), "source": "institutional_snapshot", "inferred": False, "notes": []}
        if v4.get("iv") is None:
            iv_meta = _infer_expiry_iv_with_fallbacks(ticker, target, dte, data, spot)
            if iv_meta.get("iv"):
                log.info(f"{ticker} {target}: IV fallback {iv_meta.get('source')} -> {float(iv_meta['iv']) * 100:.1f}%")
            elif v4.get("error"):
                return empty
            else:
                return empty

        final_iv = iv_meta.get("iv")
        if final_iv is None:
            return empty

        v4["iv"] = final_iv
        v4["iv_meta"] = iv_meta
        vix = _discover_vix_market_snapshot()
        enriched_walls = _derive_structure_levels_from_chain(data, spot, v4.get("walls", {}), v4.get("engine_result", {}))

        return (
            final_iv, spot, target,
            v4.get("engine_result", {}), enriched_walls,
            v4.get("skew", {}), v4.get("pcr", {}), vix,
            v4,
        )

    except Exception as e:
        log.warning(f"0DTE IV fetch failed for {ticker}: {e}")
        return empty


def _get_chain_for_expiry(ticker: str, target_date_str: str) -> tuple:
    """Fetch chain for specific expiry. Cached."""
    try:
        cached = _chain_cache_get(ticker, target_date_str)
        if cached:
            return cached
        spot = get_spot(ticker)
        data = _cached_md.get_chain(ticker, target_date_str, strike_limit=20)  # v5.1.1: strikeLimit saves credits
        if not isinstance(data, dict) or data.get("s") != "ok" or not data.get("optionSymbol"):
            return None, None, None
        _chain_cache_set(ticker, target_date_str, data, spot)
        return data, spot, target_date_str
    except Exception as e:
        log.warning(f"Chain fetch failed for {ticker} exp={target_date_str}: {e}")
        return None, None, None


def _option_mid_price(contract: dict) -> float | None:
    try:
        bid = as_float(contract.get("bid"), 0.0)
        ask = as_float(contract.get("ask"), 0.0)
        last = as_float(contract.get("last") or contract.get("lastPrice"), 0.0)
        mid = 0.0
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        elif ask > 0:
            mid = ask
        elif bid > 0:
            mid = bid
        elif last > 0:
            mid = last
        return mid if mid > 0 else None
    except Exception:
        return None


def _iter_chain_contract_rows(chain_data: dict) -> list:
    try:
        return build_chain_dicts(chain_data or {}) or []
    except Exception:
        return []


def _infer_expiry_iv_from_rows(rows: list, spot: float, dte: float) -> dict:
    rows = rows or []
    direct = []
    nearby = []
    solver = []
    if not spot or not rows:
        return {"iv": None, "source": "none", "inferred": False, "notes": ["no rows"]}

    band = max(float(spot) * 0.03, 1.0)
    for row in rows:
        strike = as_float(row.get("strike"), 0.0)
        if strike <= 0:
            continue
        dist = abs(strike - spot)
        iv = as_float(row.get("iv") or row.get("impliedVolatility"), 0.0)
        if 0.05 < iv < 3.0:
            nearby.append((dist, iv))
            if dist <= band:
                direct.append(iv)
            continue

        mid = _option_mid_price(row)
        if mid is None:
            continue
        side = _normalize_option_side(row.get("side") or row.get("option_type") or row.get("type"))
        if side not in ("call", "put"):
            continue
        try:
            sigma = _solve_implied_vol(side, float(spot), float(strike), max(float(dte), 0.5) / 365.0, 0.0, 0.0, float(mid))
            if 0.05 < sigma < 3.0:
                solver.append((dist, sigma))
        except Exception:
            continue

    if len(direct) >= 3:
        return {"iv": sum(direct) / len(direct), "source": "chain_atm_iv", "inferred": False, "notes": [f"ATM direct IVs={len(direct)}"]}
    if nearby:
        nearby.sort(key=lambda x: x[0])
        vals = [iv for _, iv in nearby[: min(8, len(nearby))]]
        return {"iv": sum(vals) / len(vals), "source": "chain_nearby_iv", "inferred": True, "notes": [f"direct IV fallback count={len(vals)}"]}
    if solver:
        solver.sort(key=lambda x: x[0])
        vals = [iv for _, iv in solver[: min(6, len(solver))]]
        return {"iv": sum(vals) / len(vals), "source": "chain_solver_iv", "inferred": True, "notes": [f"solver IV count={len(vals)}"]}
    return {"iv": None, "source": "none", "inferred": False, "notes": ["no IV fallback succeeded"]}


def _infer_expiry_iv_with_fallbacks(ticker: str, target_date_str: str, dte: float, chain_data: dict, spot: float) -> dict:
    rows = _iter_chain_contract_rows(chain_data)
    meta = _infer_expiry_iv_from_rows(rows, spot, dte)
    if meta.get("iv"):
        return meta

    try:
        swing_chains = get_options_chain_swing(ticker)
    except Exception:
        swing_chains = []
    nearest = None
    for exp, other_dte, contracts in swing_chains:
        if str(exp) == str(target_date_str):
            continue
        rows2 = list(contracts or [])
        proxy = _infer_expiry_iv_from_rows(rows2, spot, other_dte)
        if proxy.get("iv"):
            nearest = proxy
            nearest["source"] = "nearest_expiry_proxy"
            nearest["inferred"] = True
            nearest.setdefault("notes", []).append(f"proxy_from={exp}")
            break
    return nearest or meta


def _get_chain_iv_for_expiry(ticker: str, target_date_str: str, dte: float) -> tuple:
    """Full v4 chain analysis for any expiry (monitor cards)."""
    empty = (None, None, None, None, {}, {}, {}, {}, {})
    try:
        data, spot, target = _get_chain_for_expiry(ticker, target_date_str)
        if data is None:
            return empty

        # ── OI change ──
        _oi_cache.apply_oi_changes_to_chain(ticker, target, data)

        bars = _get_ohlc_bars(ticker, days=65)
        adv, spread = _estimate_liquidity(ticker, spot)

        v4 = run_institutional_snapshot(
            chain_data=data, spot=spot, dte=max(dte, 0.5),
            recent_bars=bars, is_0dte=(dte <= 1),
            avg_daily_dollar_volume=adv,
            bid_ask_spread_pct=spread,
            liquid_index=_is_liquid(ticker),
        )

        # ── Save OI snapshot ──
        _oi_cache.save_snapshot(ticker, target, data)
        if _oi_tracker:
            _oi_tracker.record_chain(ticker, target, data, spot=spot)
        # Intraday flow detection
        try:
            if _flow_detector:
                _persistent_state.save_oi_baseline(ticker, target,
                    _oi_cache._parse_chain_oi(data) if _oi_cache else {})
                flow_alerts = _flow_detector.check_intraday_flow(ticker, target, data, spot)
                postable = [fa for fa in flow_alerts
                           if fa.get("should_alert") and
                           fa.get("flow_level") in ("significant", "extreme")]
                if postable:
                    for msg in _flow_detector.format_grouped_flow_alerts(postable):
                        try:
                            post_to_telegram(msg)
                        except Exception:
                            pass
        except Exception:
            pass

        iv_meta = {"iv": v4.get("iv"), "source": "institutional_snapshot", "inferred": False, "notes": []}
        if v4.get("iv") is None:
            iv_meta = _infer_expiry_iv_with_fallbacks(ticker, target, dte, data, spot)
            if iv_meta.get("iv"):
                log.info(f"{ticker} {target}: IV fallback {iv_meta.get('source')} -> {float(iv_meta['iv']) * 100:.1f}%")
            elif v4.get("error"):
                return empty
            else:
                return empty

        final_iv = iv_meta.get("iv")
        if final_iv is None:
            return empty

        v4["iv"] = final_iv
        v4["iv_meta"] = iv_meta
        vix = _discover_vix_market_snapshot()
        enriched_walls = _derive_structure_levels_from_chain(data, spot, v4.get("walls", {}), v4.get("engine_result", {}))
        return (final_iv, spot, target, v4.get("engine_result", {}), enriched_walls,
                v4.get("skew", {}), v4.get("pcr", {}), vix, v4)
    except Exception as e:
        log.warning(f"Chain IV fetch failed for {ticker} exp={target_date_str}: {e}")
        return empty


def _normalize_option_side(raw) -> str:
    s = str(raw or "").lower().strip()
    if s in ("c", "call"):
        return "call"
    if s in ("p", "put"):
        return "put"
    return s


def _fmt_money(val, decimals: int = 2) -> str:
    if val is None:
        return "n/a"
    try:
        return f"${float(val):,.{decimals}f}"
    except Exception:
        return str(val)



def _get_daily_ohlcv_rows(ticker: str, days: int = 90) -> list:
    try:
        from_date = (datetime.now(timezone.utc) - timedelta(days=max(days * 2, 45))).strftime("%Y-%m-%d")
        raw = _cached_md.raw_get(f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/", {"from": from_date})  # v5.1.1: route through counter
        if not raw or raw.get("s") != "ok":
            return []
        opens = raw.get("o") or []
        highs = raw.get("h") or []
        lows = raw.get("l") or []
        closes = raw.get("c") or []
        vols = raw.get("v") or []
        ts = raw.get("t") or []
        n = min(len(opens), len(highs), len(lows), len(closes), len(vols), len(ts))
        rows = []
        for i in range(max(0, n - days), n):
            o = as_float(opens[i], None); h = as_float(highs[i], None)
            l = as_float(lows[i], None); c = as_float(closes[i], None)
            v = as_float(vols[i], 0.0)
            if None in (o, h, l, c) or min(o, h, l, c) <= 0:
                continue
            rows.append({"open": o, "high": h, "low": l, "close": c, "volume": max(v, 0.0), "t": ts[i]})
        return rows
    except Exception as e:
        log.warning(f"Daily OHLCV fetch failed for {ticker}: {e}")
        return []


def _compute_price_structure_levels(ticker: str, spot: float, days: int = 90) -> dict:
    rows = _get_daily_ohlcv_rows(ticker, days=days)
    out = {
        "pivot": None, "r1": None, "s1": None, "r2": None, "s2": None,
        "swing_high": None, "swing_low": None,
        "fib_support": None, "fib_resistance": None,
        "vp_support": None, "vp_resistance": None, "vpoc": None,
        "local_support_1": None, "local_resistance_1": None,
        "local_support_sources": None, "local_resistance_sources": None,
        "structure_confluence": 0,
    }
    if not rows or len(rows) < 8 or not spot:
        return out

    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    opens = [r["open"] for r in rows]

    # Pivot points (previous completed daily bar)
    prev = rows[-1]
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3.0
    r1 = 2 * pivot - prev["low"]
    s1 = 2 * pivot - prev["high"]
    rng = prev["high"] - prev["low"]
    r2 = pivot + rng
    s2 = pivot - rng
    out.update({"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2})

    # Swing highs / lows (simple local extrema)
    order = 3
    swing_highs = []
    swing_lows = []
    for i in range(order, len(rows) - order):
        h = highs[i]; l = lows[i]
        if h >= max(highs[i - order:i + order + 1]):
            swing_highs.append(h)
        if l <= min(lows[i - order:i + order + 1]):
            swing_lows.append(l)
    out["swing_high"] = min([x for x in swing_highs if x > spot], default=None)
    out["swing_low"] = max([x for x in swing_lows if x < spot], default=None)

    # Fibonacci retracement over recent lookback window
    lookback = min(len(rows), 34)
    hi = max(highs[-lookback:])
    lo = min(lows[-lookback:])
    if hi > lo:
        fib_ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
        fib_levels = sorted({lo + (hi - lo) * r for r in fib_ratios})
        out["fib_support"] = max([lvl for lvl in fib_levels if lvl <= spot], default=None)
        out["fib_resistance"] = min([lvl for lvl in fib_levels if lvl >= spot], default=None)

    # Volume profile (daily close-volume acceptance by price bin)
    if vols and max(vols) > 0:
        pmin = min(lows); pmax = max(highs)
        if pmax > pmin:
            bin_count = 24
            step = (pmax - pmin) / bin_count
            if step > 0:
                bins = [pmin + i * step for i in range(bin_count + 1)]
                profile = [0.0 for _ in range(bin_count)]
                for c, v in zip(closes, vols):
                    idx = int(min(max((c - pmin) / step, 0), bin_count - 1))
                    profile[idx] += v
                mids = [pmin + (i + 0.5) * step for i in range(bin_count)]
                if profile:
                    out["vpoc"] = mids[max(range(len(profile)), key=lambda i: profile[i])]
                    below = [(profile[i], mids[i]) for i in range(len(mids)) if mids[i] < spot]
                    above = [(profile[i], mids[i]) for i in range(len(mids)) if mids[i] > spot]
                    if below:
                        out["vp_support"] = max(below, key=lambda x: x[0])[1]
                    if above:
                        out["vp_resistance"] = max(above, key=lambda x: x[0])[1]

    supports = []
    resistances = []
    def add_level(kind: str, value):
        if value is None:
            return
        value = float(value)
        if value < spot:
            supports.append((value, kind))
        elif value > spot:
            resistances.append((value, kind))

    add_level("swing_low", out["swing_low"])
    add_level("s1", out["s1"])
    add_level("s2", out["s2"])
    add_level("fib", out["fib_support"])
    add_level("vp", out["vp_support"])
    add_level("pivot", out["pivot"])

    add_level("swing_high", out["swing_high"])
    add_level("r1", out["r1"])
    add_level("r2", out["r2"])
    add_level("fib", out["fib_resistance"])
    add_level("vp", out["vp_resistance"])
    add_level("pivot", out["pivot"])

    if supports:
        supports.sort(key=lambda x: spot - x[0])
        primary = supports[0][0]
        tol = max(spot * 0.0035, 0.75)
        srcs = [name for value, name in supports if abs(value - primary) <= tol]
        out["local_support_1"] = primary
        out["local_support_sources"] = " + ".join(sorted(set(srcs)))
    if resistances:
        resistances.sort(key=lambda x: x[0] - spot)
        primary = resistances[0][0]
        tol = max(spot * 0.0035, 0.75)
        srcs = [name for value, name in resistances if abs(value - primary) <= tol]
        out["local_resistance_1"] = primary
        out["local_resistance_sources"] = " + ".join(sorted(set(srcs)))

    out["structure_confluence"] = len([x for x in [out.get("local_support_sources"), out.get("local_resistance_sources")] if x])
    return out


def _merge_price_structure_with_walls(price_structure: dict, chain_structure: dict, spot: float, em: dict | None = None) -> dict:
    merged = dict(chain_structure or {})
    ps = dict(price_structure or {})
    em_1sd = (em or {}).get("em_1sd") or 0.0
    tol = max(spot * 0.004, 1.0)

    call_wall = merged.get("call_wall")
    put_wall = merged.get("put_wall")
    gamma_wall = merged.get("gamma_wall")

    if ps.get("local_resistance_1") is not None:
        local_r = ps["local_resistance_1"]
        if call_wall is None or call_wall <= spot or abs(call_wall - spot) > max((em_1sd * 2.5), spot * 0.12):
            merged["call_wall"] = local_r
        merged["local_resistance_1"] = local_r
        merged["local_resistance_sources"] = ps.get("local_resistance_sources")
    if ps.get("local_support_1") is not None:
        local_s = ps["local_support_1"]
        if put_wall is None or put_wall >= spot or abs(put_wall - spot) > max((em_1sd * 2.5), spot * 0.12):
            merged["put_wall"] = local_s
        merged["local_support_1"] = local_s
        merged["local_support_sources"] = ps.get("local_support_sources")
    if gamma_wall is None and ps.get("pivot") is not None:
        merged["gamma_wall"] = ps.get("pivot")

    for k in ("pivot", "r1", "s1", "r2", "s2", "swing_high", "swing_low", "fib_support", "fib_resistance", "vp_support", "vp_resistance", "vpoc", "structure_confluence"):
        merged[k] = ps.get(k)

    pin_low = merged.get("put_wall") or ps.get("local_support_1")
    pin_high = merged.get("call_wall") or ps.get("local_resistance_1")
    if pin_low is not None:
        merged["pin_zone_low"] = pin_low
    if pin_high is not None:
        merged["pin_zone_high"] = pin_high
    return merged


def _format_unified_regime_line(regime) -> str:
    if not regime:
        return "UNKNOWN — no regime data"
    if isinstance(regime, str):
        return regime
    label = str(regime.get("label") or regime.get("regime") or "UNKNOWN").upper()
    desc = str(regime.get("description") or "").strip()
    source = str(regime.get("source") or "").strip()
    spot_vs_flip = regime.get("spot_vs_flip")
    extras = []
    if spot_vs_flip in ("above", "below"):
        extras.append(f"spot {spot_vs_flip} flip")
    if source and source != "unknown":
        extras.append(source)
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"{label}{suffix} — {desc}" if desc else f"{label}{suffix}"



def _derive_structure_levels_from_chain(data: dict, spot: float, base_walls: dict | None = None, eng: dict | None = None) -> dict:
    """
    Backfill structural levels from raw chain data so logging/output always has the same
    canonical fields even when the upstream snapshot omits some of them.

    v5 refinement:
    - prefer *local* walls nearest spot rather than global far-away OI maxima
    - keep top3 local ladders for context
    - narrow pin zone to actionable nearby support/resistance
    """
    walls = dict(base_walls or {})
    if not isinstance(data, dict):
        return walls

    sym_list = data.get("optionSymbol") or []
    n = len(sym_list)
    if n == 0:
        return walls

    def col(name, default=None):
        v = data.get(name, default)
        return v if isinstance(v, list) else [default] * n

    strikes = col("strike", None)
    sides = col("side", col("right", ""))
    ois = col("openInterest", 0)
    gammas = col("gamma", None)

    by_strike = {}
    unique_strikes = set()
    for i in range(n):
        try:
            strike = as_float(strikes[i], None)
        except Exception:
            strike = None
        if strike is None or strike <= 0:
            continue
        side = _normalize_option_side(sides[i])
        oi = max(as_int(ois[i], 0), 0)
        gamma = abs(as_float(gammas[i], 0.0) or 0.0)
        row = by_strike.setdefault(strike, {"call_oi": 0, "put_oi": 0, "gamma_abs": 0.0})
        if side == "call":
            row["call_oi"] += oi
        elif side == "put":
            row["put_oi"] += oi
        if gamma and oi:
            row["gamma_abs"] += gamma * oi
        unique_strikes.add(strike)

    if not by_strike:
        return walls

    sorted_strikes = sorted(unique_strikes)
    strike_steps = [b - a for a, b in zip(sorted_strikes, sorted_strikes[1:]) if b > a]
    strike_step = min(strike_steps) if strike_steps else max(round(max(spot, 1) * 0.0025, 2), 0.5)

    def _normalized_oi(side_key: str) -> float:
        vals = [v.get(side_key, 0) for v in by_strike.values() if v.get(side_key, 0) > 0]
        return max(vals) if vals else 1.0

    max_call_oi = _normalized_oi("call_oi")
    max_put_oi = _normalized_oi("put_oi")
    max_gamma_abs = max([v.get("gamma_abs", 0.0) for v in by_strike.values()] + [1.0])

    # Search a local band around spot first. Fall back broader if needed.
    local_band_abs = max(spot * 0.06, strike_step * 10)   # ~6% or 10 strikes
    wider_band_abs = max(spot * 0.12, strike_step * 18)  # fallback if sparse

    def _candidate_rows(side_key: str, above: bool, band_abs: float):
        out = []
        for strike in sorted_strikes:
            if above and strike <= spot:
                continue
            if not above and strike >= spot:
                continue
            dist = abs(strike - spot)
            if dist > band_abs:
                continue
            oi_val = by_strike[strike].get(side_key, 0)
            if oi_val <= 0:
                continue
            out.append((strike, oi_val, dist))
        return out

    def _rank_wall_candidates(side_key: str, above: bool, band_abs: float, oi_norm: float):
        rows = _candidate_rows(side_key, above, band_abs)
        ranked = []
        for strike, oi_val, dist in rows:
            # proximity matters more than raw OI for actionable local walls.
            proximity = 1.0 / (1.0 + (dist / max(band_abs, strike_step)))
            oi_score = (oi_val / max(oi_norm, 1.0))
            score = 0.62 * proximity + 0.38 * oi_score
            ranked.append((score, strike, oi_val, dist))
        ranked.sort(key=lambda x: (-x[0], x[3]))
        return ranked

    def _pick_local_wall(side_key: str, above: bool, oi_norm: float):
        ranked = _rank_wall_candidates(side_key, above, local_band_abs, oi_norm)
        if not ranked:
            ranked = _rank_wall_candidates(side_key, above, wider_band_abs, oi_norm)
        if not ranked:
            return None, []
        chosen = ranked[0][1]
        top3 = [r[1] for r in ranked[:3]]
        return chosen, top3

    # Local actionable walls
    chosen_call, call_top3 = _pick_local_wall("call_oi", True, max_call_oi)
    chosen_put, put_top3 = _pick_local_wall("put_oi", False, max_put_oi)

    # Preserve upstream walls only if they are near spot / actionable.
    existing_call = as_float(walls.get("call_wall"), None)
    existing_put = as_float(walls.get("put_wall"), None)
    if existing_call is not None and existing_call > spot and abs(existing_call - spot) <= wider_band_abs:
        chosen_call = existing_call
    if existing_put is not None and existing_put < spot and abs(existing_put - spot) <= wider_band_abs:
        chosen_put = existing_put

    if chosen_call is not None:
        walls["call_wall"] = chosen_call
        walls["call_wall_oi"] = by_strike.get(chosen_call, {}).get("call_oi", 0)
        walls["call_top3"] = sorted(set(call_top3 or [chosen_call]))
    if chosen_put is not None:
        walls["put_wall"] = chosen_put
        walls["put_wall_oi"] = by_strike.get(chosen_put, {}).get("put_oi", 0)
        walls["put_top3"] = sorted(set(put_top3 or [chosen_put]), reverse=True)

    # Gamma wall: strongest local gamma node, not the broadest distant extreme.
    gamma_ranked = []
    gamma_band = max(spot * 0.05, strike_step * 8)
    for strike in sorted_strikes:
        gamma_abs = by_strike[strike].get("gamma_abs", 0.0)
        if gamma_abs <= 0:
            continue
        dist = abs(strike - spot)
        if dist > max(gamma_band, wider_band_abs):
            continue
        proximity = 1.0 / (1.0 + (dist / max(gamma_band, strike_step)))
        gamma_score = (gamma_abs / max_gamma_abs)
        score = 0.55 * proximity + 0.45 * gamma_score
        gamma_ranked.append((score, strike, gamma_abs, dist))
    gamma_ranked.sort(key=lambda x: (-x[0], x[3]))
    if gamma_ranked:
        gw = gamma_ranked[0][1]
        walls["gamma_wall"] = gw
        walls["gamma_wall_gex"] = gamma_ranked[0][2]
    elif not walls.get("gamma_wall"):
        gamma_candidates = [k for k, v in by_strike.items() if v.get("gamma_abs", 0) > 0]
        if gamma_candidates:
            gw = max(gamma_candidates, key=lambda k: by_strike[k]["gamma_abs"])
            walls["gamma_wall"] = gw
            walls["gamma_wall_gex"] = by_strike[gw].get("gamma_abs", 0)

    if not walls.get("gamma_flip") and eng and eng.get("flip_price") is not None:
        walls["gamma_flip"] = eng.get("flip_price")

    # Max pain remains global by definition.
    if not walls.get("max_pain"):
        all_strikes = sorted(unique_strikes)
        best_strike = None
        best_payout = None
        for settle in all_strikes:
            payout = 0.0
            for strike, row in by_strike.items():
                call_oi = row.get("call_oi", 0)
                put_oi = row.get("put_oi", 0)
                if call_oi:
                    payout += max(0.0, settle - strike) * call_oi * 100
                if put_oi:
                    payout += max(0.0, strike - settle) * put_oi * 100
            if best_payout is None or payout < best_payout:
                best_payout = payout
                best_strike = settle
        if best_strike is not None:
            walls["max_pain"] = best_strike

    # Actionable pin/range zone should be local, not broad full-chain extremes.
    if chosen_put is not None:
        walls["pin_zone_low"] = chosen_put
    if chosen_call is not None:
        walls["pin_zone_high"] = chosen_call

    # Optional max pain proximity info for downstream decisions.
    mp = as_float(walls.get("max_pain"), None)
    if mp is not None and spot:
        walls["max_pain_dist_pct"] = abs(mp - spot) / max(spot, 0.01) * 100.0

    return walls


def _estimate_liquidity(ticker: str, spot: float) -> tuple:
    """
    Estimate ADV and bid-ask spread from stock candles + quote.
    Returns (avg_daily_dollar_volume, bid_ask_spread_pct) or (None, None).
    """
    adv = None
    spread = None
    try:
        # ADV: average of last 20 days of (close * volume)
        candles = get_daily_candles(ticker, days=25)
        if candles and len(candles) >= 5:
            # candles is a list of close prices from get_daily_candles
            # We need volume too — fetch raw candles
            from_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            raw = _cached_md.raw_get(f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",  # v5.1.1: counter
                        {"from": from_date})
            if raw and raw.get("s") == "ok":
                closes = raw.get("c") or []
                volumes = raw.get("v") or []
                n = min(len(closes), len(volumes))
                if n >= 5:
                    dvols = []
                    for i in range(max(n - 20, 0), n):
                        c = as_float(closes[i], 0)
                        v = as_float(volumes[i], 0)
                        if c > 0 and v > 0:
                            dvols.append(c * v)
                    if dvols:
                        adv = sum(dvols) / len(dvols)
    except Exception as e:
        log.debug(f"ADV estimate failed for {ticker}: {e}")

    try:
        # Spread from stock quote
        stock_data = _cached_md.raw_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")  # v5.1.1: counter
        if stock_data and stock_data.get("s") == "ok":
            bid = as_float((stock_data.get("bid") or [None])[0], 0)
            ask = as_float((stock_data.get("ask") or [None])[0], 0)
            if bid > 0 and ask > 0 and ask > bid:
                spread = (ask - bid) / ((ask + bid) / 2)
    except Exception as e:
        log.debug(f"Spread estimate failed for {ticker}: {e}")

    return adv, spread



def _get_ct_session_progress() -> float:
    """Return regular-session progress in America/Chicago as 0.0..1.0."""
    try:
        import pytz
        ct = pytz.timezone("America/Chicago")
        now_ct = datetime.now(ct)
        mkt_open = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
        mkt_close = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
        session_secs = (mkt_close - mkt_open).total_seconds()
        elapsed = max(0.0, (now_ct - mkt_open).total_seconds())
        return min(1.0, elapsed / session_secs) if session_secs > 0 else 0.5
    except Exception:
        return 0.5



def _extract_v4_flow_snapshot(v4_result: dict, spot: float = 0.0) -> dict:
    """Normalize a full v4 snapshot into the shared v4_flow shape."""
    if not isinstance(v4_result, dict):
        return {}

    snap = v4_result.get("snapshot") or {}
    confidence = snap.get("confidence") or v4_result.get("confidence") or {}
    dealer = snap.get("dealer_flows") or v4_result.get("dealer_flows") or {}
    regime = snap.get("regime") or v4_result.get("regime") or {}
    adj = snap.get("adjusted_expectation") or v4_result.get("adjusted_expectation") or {}

    out = {
        "confidence_label": confidence.get("label", ""),
        "confidence_score": confidence.get("composite", 0),
        "gex": dealer.get("gex", 0),
        "dex": dealer.get("dex", 0),
        "vanna": dealer.get("vanna", 0),
        "charm": dealer.get("charm", 0),
        "bias": adj.get("bias", "NEUTRAL"),
        "bias_score": adj.get("bias_score", 0),
        "gamma_flip": dealer.get("gamma_flip") or dealer.get("flip_price"),
        "spot": spot or v4_result.get("spot") or snap.get("spot") or 0,
        "composite_regime": regime.get("regime", "") or regime.get("label", ""),
        "downgrades": snap.get("downgrades") or v4_result.get("downgrades") or [],
        "vol_regime_label": (snap.get("volatility_regime") or v4_result.get("vol_regime") or {}).get("label", ""),
    }
    return out



def _compute_cagf_snapshot(
    ticker: str,
    spot: float,
    iv: float,
    eng: dict,
    vix: dict | float | None = None,
    v4_result: dict = None,
    candle_closes: list = None,
    adv: float = None,
) -> dict:
    """Reusable institutional-flow snapshot for EM/monitor/shared dealer regime logic.

    v4.5: passes vol_caution_score and vol_transition_warning from canonical
    vol regime so compute_cagf can suppress directional probability during
    structurally risky vol environments.
    """
    ticker = (ticker or "").upper().strip()
    eng = eng or {}
    if not ticker or not eng or ticker not in ("SPY", "QQQ", "SPX"):
        return None
    if iv is None or spot is None or spot <= 0:
        return None

    try:
        vol_regime = {}
        if isinstance(v4_result, dict):
            vol_regime = v4_result.get("vol_regime") or v4_result.get("volatility_regime") or {}
        rv = as_float(vol_regime.get("realized_vol_20d"), 0.0)
        closes = candle_closes or get_daily_candles(ticker, days=60)
        if rv <= 0:
            canonical_vol = get_canonical_vol_regime(ticker, candle_closes=closes)
            rv_pct = as_float((canonical_vol or {}).get("rv20"), 0.0)
            rv = rv_pct / 100.0 if rv_pct > 0 else 0.0
        else:
            canonical_vol = get_canonical_vol_regime(ticker, candle_closes=closes)

        # Extract canonical vol regime signals for CAGF suppression
        canonical_vol = canonical_vol or {}
        vol_caution = int(canonical_vol.get("caution_score") or 0)
        vol_transition = bool(canonical_vol.get("transition_warning"))

        if adv is None:
            adv, _ = _estimate_liquidity(ticker, spot)

        if isinstance(vix, dict):
            vix_val = as_float(vix.get("vix"), 20.0)
        else:
            vix_val = as_float(vix, 20.0)

        return compute_cagf(
            dealer_flows={
                "gex": eng.get("gex", 0),
                "dex": eng.get("dex", 0),
                "vanna": eng.get("vanna", 0),
                "charm": eng.get("charm", 0),
                "gamma_flip": eng.get("flip_price") or eng.get("gamma_flip"),
            },
            iv=iv,
            rv=rv,
            spot=spot,
            vix=vix_val,
            session_progress=_get_ct_session_progress(),
            adv=adv,
            candle_closes=closes,
            ticker=ticker,
            vol_caution_score=vol_caution,
            vol_transition_warning=vol_transition,
        )
    except Exception as e:
        log.warning(f"CAGF compute failed for {ticker}: {e}")
        return None


# v4.2: Per-cycle OHLC warning dedup.
# api_cache.get_ohlc_bars logs a WARNING on every timeout, which produced
# 32 identical lines for MA in today's run. We gate to one warning per
# ticker per process lifetime using a simple set. The set stays small
# (one entry per unique ticker that has ever timed out) and never needs
# to be cleared — the underlying cache TTL handles re-fetching.
_ohlc_warned_tickers: set = set()


def _get_ohlc_bars(ticker: str, days: int = 65) -> list:
    """Fetch daily OHLC and convert to options_exposure.OHLC objects (cached).
    v4.2: Suppresses repeated timeout warnings for the same ticker (OHLC_WARN_ONCE_PER_CYCLE).
    """
    if OHLC_WARN_ONCE_PER_CYCLE and ticker.upper() in _ohlc_warned_tickers:
        # Warning already emitted for this ticker — fetch silently
        import logging as _logging
        _orig_level = logging.getLogger("api_cache").level
        logging.getLogger("api_cache").setLevel(_logging.ERROR)
        try:
            return _cached_md.get_ohlc_bars(ticker, days)
        finally:
            logging.getLogger("api_cache").setLevel(_orig_level)

    result = _cached_md.get_ohlc_bars(ticker, days)
    if not result and OHLC_WARN_ONCE_PER_CYCLE:
        _ohlc_warned_tickers.add(ticker.upper())
    return result


def _get_atm_skew(strikes, iv_list, sides, n, spot):
    call_ivs, put_ivs = [], []
    atm_range = spot * 0.02
    for i in range(n):
        strike = as_float(strikes[i], 0); iv = as_float(iv_list[i], 0)
        side = str(sides[i] or "").lower()
        if iv <= 0 or abs(strike - spot) > atm_range: continue
        if side == "call": call_ivs.append(iv)
        elif side == "put": put_ivs.append(iv)
    result = {}
    if call_ivs: result["call_iv"] = round(sum(call_ivs) / len(call_ivs) * 100, 1)
    if put_ivs: result["put_iv"] = round(sum(put_ivs) / len(put_ivs) * 100, 1)
    return result


def _calc_pcr(oi_list, vol_l, sides, n):
    call_oi = call_vol = put_oi = put_vol = 0
    for i in range(n):
        oi = int(as_float(oi_list[i], 0)); vol = int(as_float(vol_l[i], 0))
        side = str(sides[i] or "").lower()
        if side == "call": call_oi += oi; call_vol += vol
        elif side == "put": put_oi += oi; put_vol += vol
    return {
        "put_oi": put_oi, "call_oi": call_oi, "put_vol": put_vol, "call_vol": call_vol,
        "pcr_oi": round(put_oi / call_oi, 2) if call_oi > 0 else None,
        "pcr_vol": round(put_vol / call_vol, 2) if call_vol > 0 else None,
    }


def _get_vix_data():
    result = _cached_md.get_vix_data(as_float_fn=as_float)
    if not result or not result.get("vix"):
        log.warning("VIX data empty from MarketData cache — vol regime will use defaults")
    return result

def _discover_vix_market_snapshot():
    """Alias kept for call sites in _get_0dte_iv and _get_chain_iv_for_expiry."""
    return _get_vix_data() or {}


# ─────────────────────────────────────────────────────────
# _calc_bias — KEPT AS-IS (14-signal scoring system)
# This is better than the engine's 4-signal composite.
# The v4 engine feeds it the same engine_result/walls/skew/pcr/vix
# it always consumed, plus v4_result is available for confidence.
# ─────────────────────────────────────────────────────────

# NOTE: _calc_bias is preserved EXACTLY as in the original app.py.
# It is ~400 lines of the 14-signal scoring system.
# For brevity in this file, the function is imported or pasted from
# the original. The ONLY change is that callers now also pass v4_result
# to the card formatters, which _calc_bias doesn't need to know about.
#
# >>> PASTE YOUR EXISTING _calc_bias() FUNCTION HERE UNCHANGED <<<
# It takes: (spot, em, walls, skew, eng, pcr, vix) and returns the bias dict.
# The v4 integration does NOT modify this function at all.

from app_bias import _calc_bias  # ← or paste the full function inline


# ─────────────────────────────────────────────────────────
# DISPLAY HELPERS — same as original + v4 confidence header
# ─────────────────────────────────────────────────────────

def _format_em_block(em, spot, bias_score, label=""):
    header = f"📐 EXPECTED MOVE{(' ' + label) if label else ''}"
    bull_1 = em["bull_1sd"]; bear_1 = em["bear_1sd"]
    bull_2 = em["bull_2sd"]; bear_2 = em["bear_2sd"]
    em_1 = em["em_1sd"]; em_2 = em["em_2sd"]; em_pct = em["em_pct_1sd"]
    line_1sd = f"  1σ (68%):  ${bear_1:.2f}  ←  ${spot:.2f}  →  ${bull_1:.2f}"
    # Bias score informs lean but NOT the range — range is pure IV math
    if bias_score >= 4:
        lean = f"  📈 Lean: BULLISH  (score {bias_score:+d}/14)"
    elif bias_score >= 2:
        lean = f"  📈 Lean: slight bullish  (score {bias_score:+d}/14)"
    elif bias_score <= -4:
        lean = f"  📉 Lean: BEARISH  (score {bias_score:+d}/14)"
    elif bias_score <= -2:
        lean = f"  📉 Lean: slight bearish  (score {bias_score:+d}/14)"
    else:
        lean = f"  ↔️  No clear lean  (score {bias_score:+d}/14)"
    return ["", "─" * 32, header, line_1sd, f"             ±${em_1:.2f}  ({em_pct:.2f}%)",
            f"  2σ (95%):  ${bear_2:.2f}  ←→  ${bull_2:.2f}", f"             ±${em_2:.2f}", lean]


def _format_skew_line(skew):
    if not skew or "call_iv" not in skew or "put_iv" not in skew: return ""
    c = skew["call_iv"]; p = skew["put_iv"]; diff = abs(p - c)
    if diff < 1.0: reading = "balanced"
    elif p > c: reading = f"puts pricier by {diff:.1f}% — {'aggressive' if diff >= 5 else 'mild'} fear"
    else: reading = f"calls pricier by {diff:.1f}% — {'aggressive' if diff >= 5 else 'mild'} greed"
    return f"  IV Skew: Calls {c}% / Puts {p}% — {reading}"


def _calc_intraday_em(spot, iv, hours_remaining):
    if iv <= 0 or hours_remaining <= 0: return {}
    hours_in_year = 252 * 6.5
    em_1sd = round(spot * iv * math.sqrt(hours_remaining / hours_in_year), 2)
    em_2sd = round(em_1sd * 1.96, 2)  # 1.96σ = 95.0% confidence interval
    return {
        "em_1sd": em_1sd, "em_2sd": em_2sd,
        "bull_1sd": round(spot + em_1sd, 2), "bear_1sd": round(spot - em_1sd, 2),
        "bull_2sd": round(spot + em_2sd, 2), "bear_2sd": round(spot - em_2sd, 2),
        "em_pct_1sd": round((em_1sd / spot) * 100, 2), "hours_used": round(hours_remaining, 2),
    }


# ─────────────────────────────────────────────────────────
# LIQUID SYMBOL DETECTION
# ─────────────────────────────────────────────────────────

LIQUID_SYMBOLS = {
    # Major indices
    "SPY", "QQQ", "SPX", "NDX", "IWM", "DIA",
    # Mega-cap tech
    "AAPL", "NVDA", "TSLA", "AMZN", "META", "MSFT", "GOOGL", "GOOG",
    # High-volume single names
    "AMD", "NFLX", "COIN", "MSTR", "ARM", "PLTR", "SMCI", "INTC", "MU", "SOFI",
    # Commodity & sector ETFs
    "GLD", "SLV", "GDX", "USO", "XLE", "XLF", "XLK", "XLV", "XLI", "XLP",
    # Bond & international ETFs
    "TLT", "HYG", "EEM", "EFA", "FXI",
    # Volatility
    "VXX", "UVXY", "SQQQ", "TQQQ",
}

def _is_liquid(ticker): return ticker.upper() in LIQUID_SYMBOLS


# ─────────────────────────────────────────────────────────
# EXPIRY SELECTION
# ─────────────────────────────────────────────────────────

def _find_expiry_nearest(ticker):
    try:
        from datetime import date as _date
        today = _date.today(); today_str = today.strftime("%Y-%m-%d")
        exps = get_expirations(ticker)
        future = [e for e in exps if e >= today_str]
        if not future: return None, None
        exp = future[0]; dte = (_date.fromisoformat(exp) - today).days
        return exp, dte
    except Exception as e:
        log.warning(f"_find_expiry_nearest failed for {ticker}: {e}")
        return None, None




def _find_expiry_closest_to_21(ticker):
    """
    Monitor-long selection for a ~21-day thesis, but with extra time buffer.
    Prefer 28–35 DTE when available so trades and wheel levels have grace time.
    Fall back to the nearest >=21 DTE expiry if the buffered band is unavailable.
    """
    try:
        from datetime import date as _date
        today = _date.today(); today_str = today.strftime("%Y-%m-%d")
        exps = get_expirations(ticker)
        buffered = []
        fallback = []
        for e in exps:
            dte = (_date.fromisoformat(e) - today).days
            if e < today_str or dte < 15:
                continue
            row = (abs(dte - 31), dte, e)
            if 28 <= dte <= 35:
                buffered.append(row)
            if dte >= 21:
                fallback.append((abs(dte - 28), dte, e))
        if buffered:
            buffered.sort(); _, dte, exp = buffered[0]
            return exp, dte
        if fallback:
            fallback.sort(); _, dte, exp = fallback[0]
            return exp, dte
        return None, None
    except Exception as e:
        log.warning(f"_find_expiry_closest_to_21 failed for {ticker}: {e}")
        return None, None


# ─────────────────────────────────────────────────────────────────────
# EM ACCURACY LOGGER — saves predictions for backtest analysis
# Stores each EM card's prediction in Redis so we can compare
# against actual EOD prices and compute hit rates over time.
# ─────────────────────────────────────────────────────────────────────

def _log_em_prediction(ticker: str, session: str, spot: float, em: dict, bias: dict, v4_result: dict, walls: dict = None, eng: dict = None, cagf: dict = None, vol_regime: dict = None):
    """
    Log EM prediction to Redis and to an auto-tracked dataset for later tuning.
    Key: em_log:{date}:{ticker}:{session}
    Stored as JSON with prediction data. TTL: 90 days.
    """
    try:
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.strftime("%Y-%m-%d")
        key = f"em_log:{now_str}:{ticker}:{session}"
        chain_struct = _derive_structure_levels_from_chain({}, spot, walls or {}, eng or {})
        price_struct = _compute_price_structure_levels(ticker, spot, days=90)
        struct = _merge_price_structure_with_walls(price_struct, chain_struct, spot, em)
        eng = eng or {}
        max_pain = struct.get("max_pain") or eng.get("max_pain")
        entry = {
            "ticker": ticker,
            "date": now_str,
            "session": session,
            "logged_at_utc": now_dt.isoformat(),
            "spot_at_prediction": spot,
            "em_1sd": em.get("em_1sd"),
            "bull_1sd": em.get("bull_1sd"),
            "bear_1sd": em.get("bear_1sd"),
            "bull_2sd": em.get("bull_2sd"),
            "bear_2sd": em.get("bear_2sd"),
            "bias_score": bias.get("score"),
            "bias_direction": bias.get("direction"),
            "v4_confidence": v4_result.get("confidence", {}).get("label") if v4_result else None,
            "v4_confidence_composite": v4_result.get("confidence", {}).get("composite") if v4_result else None,
            "v4_bias": v4_result.get("snapshot", {}).get("adjusted_expectation", {}).get("bias") if v4_result else None,
            "v4_regime": v4_result.get("snapshot", {}).get("regime", {}).get("regime") if v4_result else None,
            "gamma_flip": struct.get("gamma_flip") or eng.get("flip_price"),
            "call_wall": struct.get("call_wall"),
            "put_wall": struct.get("put_wall"),
            "gamma_wall": struct.get("gamma_wall"),
            "max_pain": max_pain,
            "pin_zone_low": struct.get("pin_zone_low"),
            "pin_zone_high": struct.get("pin_zone_high"),
            "pivot": struct.get("pivot"),
            "r1": struct.get("r1"),
            "s1": struct.get("s1"),
            "r2": struct.get("r2"),
            "s2": struct.get("s2"),
            "swing_high": struct.get("swing_high"),
            "swing_low": struct.get("swing_low"),
            "fib_resistance": struct.get("fib_resistance"),
            "fib_support": struct.get("fib_support"),
            "vp_resistance": struct.get("vp_resistance"),
            "vp_support": struct.get("vp_support"),
            "vpoc": struct.get("vpoc"),
            "local_resistance_1": struct.get("local_resistance_1"),
            "local_support_1": struct.get("local_support_1"),
            "local_resistance_sources": struct.get("local_resistance_sources"),
            "local_support_sources": struct.get("local_support_sources"),
            "structure_confluence": struct.get("structure_confluence"),
            "cagf_direction": (cagf or {}).get("direction"),
            "cagf_regime": (cagf or {}).get("regime"),
            "trend_day_probability": (cagf or {}).get("trend_day_probability"),
            "vol_regime_label": (vol_regime or {}).get("label"),
            "vol_regime_base": (vol_regime or {}).get("base"),
            "vol_caution_score": (vol_regime or {}).get("caution_score"),
            "vol_transition_warning": (vol_regime or {}).get("transition_warning"),
            "vol_term_structure": (vol_regime or {}).get("term_structure"),
            "vol_vvix": (vol_regime or {}).get("vvix"),
            "vol_size_mult": (vol_regime or {}).get("size_mult"),
            "eod_price": None,
            "reconciled": False,
        }
        store_set(key, json.dumps(entry), ttl=90 * 86400)

        fieldnames = [
            "logged_at_utc","date","session","ticker","spot_at_prediction","em_1sd","bull_1sd","bear_1sd",
            "bull_2sd","bear_2sd","bias_score","bias_direction","v4_confidence","v4_confidence_composite",
            "v4_bias","v4_regime","gamma_flip","call_wall","put_wall","gamma_wall","max_pain",
            "pin_zone_low","pin_zone_high","pivot","r1","s1","r2","s2","swing_high","swing_low",
            "fib_resistance","fib_support","vp_resistance","vp_support","vpoc","local_resistance_1","local_support_1",
            "local_resistance_sources","local_support_sources","structure_confluence",
            "cagf_direction","cagf_regime","trend_day_probability",
            "vol_regime_label","vol_regime_base","vol_caution_score","vol_transition_warning","vol_term_structure","vol_vvix","vol_size_mult"
        ]
        _append_csv_row("em_predictions.csv", fieldnames, entry)
        _append_jsonl("em_predictions.jsonl", entry)
        log.debug(f"EM prediction logged: {key}")
    except Exception as e:
        log.warning(f"EM prediction log failed: {e}")

# ─────────────────────────────────────────────────────────────────────
# ── ATM option snapshot for thesis monitor premium stop ──────────────────────
def _extract_atm_option_data(chain_data: dict, spot: float) -> dict:
    """Extract ATM call and put delta + mid price from a raw chain dict.

    Called once per em card run, result stored on ThesisContext.
    Used by the thesis monitor to approximate option premium loss each poll
    without fetching live quotes. Zero API calls — reads from already-fetched data.

    v15: Two-path extraction:
      Path A: via build_option_rows (structured row dicts).
      Path B: direct raw parallel-array read from marketdata.app format.
    Falls back to Path B if Path A yields nothing, which happens when
    build_option_rows drops the 'side' or 'delta' fields.

    Returns dict with keys: call_delta, call_premium, put_delta, put_premium.
    All values default to 0.0 if chain is thin or ATM cannot be found.
    """
    result = {"call_delta": 0.0, "call_premium": 0.0,
              "put_delta":  0.0, "put_premium":  0.0}
    if not chain_data or not spot:
        log.info("ATM extract: no chain data or spot")
        return result
    try:
        # ── Path A: structured rows ───────────────────────────────────
        best_call = None; best_call_dist = float("inf")
        best_put  = None; best_put_dist  = float("inf")

        rows = build_chain_dicts(chain_data) or []
        rows_checked = 0; rows_no_side = 0; rows_no_delta = 0; rows_no_mid = 0

        for row in rows:
            strike = as_float(row.get("strike"), 0.0)
            if not strike or strike <= 0:
                continue
            dist = abs(strike - spot)
            # Only check nearby strikes (within 5%) for efficiency
            if dist > spot * 0.05:
                continue
            rows_checked += 1
            side = (row.get("side") or row.get("option_type") or row.get("type") or "").lower()
            if side not in ("call", "put"):
                side = (row.get("right") or "").lower()
            if side not in ("call", "put"):
                rows_no_side += 1
                continue

            delta = abs(as_float(row.get("delta"), 0.0))
            mid = _option_mid_price(row)
            if not delta or delta <= 0:
                rows_no_delta += 1
            if not mid or mid <= 0:
                rows_no_mid += 1
            if not mid or mid <= 0 or delta <= 0:
                continue

            if side == "call" and dist < best_call_dist:
                best_call = {"delta": delta, "premium": round(mid, 2)}
                best_call_dist = dist
            elif side == "put" and dist < best_put_dist:
                best_put = {"delta": delta, "premium": round(mid, 2)}
                best_put_dist = dist

        # ── Path B fallback: raw parallel arrays ──────────────────────
        # If Path A yielded nothing, try reading directly from the raw
        # marketdata.app chain format (parallel arrays keyed by field name).
        if not best_call and not best_put:
            log.info(f"ATM extract Path A: {len(rows)} rows, {rows_checked} near ATM, "
                     f"{rows_no_side} no side, {rows_no_delta} no delta, {rows_no_mid} no mid — "
                     f"falling back to Path B (raw arrays)")

            raw_strikes = chain_data.get("strike") or []
            raw_sides   = chain_data.get("side") or chain_data.get("right") or []
            raw_deltas  = chain_data.get("delta") or []
            raw_bids    = chain_data.get("bid") or []
            raw_asks    = chain_data.get("ask") or []
            raw_mids    = chain_data.get("mid") or []
            raw_lasts   = chain_data.get("last") or chain_data.get("lastPrice") or []
            n = len(raw_strikes)

            for i in range(n):
                strike = as_float(raw_strikes[i] if i < len(raw_strikes) else 0, 0.0)
                if not strike or strike <= 0:
                    continue
                dist = abs(strike - spot)
                if dist > spot * 0.05:
                    continue

                side_raw = raw_sides[i] if i < len(raw_sides) else ""
                side = _normalize_option_side(side_raw)
                if side not in ("call", "put"):
                    continue

                delta = abs(as_float(raw_deltas[i] if i < len(raw_deltas) else 0, 0.0))

                # Compute mid from bid/ask or mid or last
                bid = as_float(raw_bids[i] if i < len(raw_bids) else 0, 0.0)
                ask = as_float(raw_asks[i] if i < len(raw_asks) else 0, 0.0)
                mid_direct = as_float(raw_mids[i] if i < len(raw_mids) else 0, 0.0)
                last = as_float(raw_lasts[i] if i < len(raw_lasts) else 0, 0.0)

                mid = 0.0
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                elif mid_direct > 0:
                    mid = mid_direct
                elif ask > 0:
                    mid = ask
                elif last > 0:
                    mid = last

                if mid <= 0 or delta <= 0:
                    continue

                if side == "call" and dist < best_call_dist:
                    best_call = {"delta": delta, "premium": round(mid, 2)}
                    best_call_dist = dist
                elif side == "put" and dist < best_put_dist:
                    best_put = {"delta": delta, "premium": round(mid, 2)}
                    best_put_dist = dist

        if best_call:
            result["call_delta"]   = round(best_call["delta"],   3)
            result["call_premium"] = best_call["premium"]
        if best_put:
            result["put_delta"]    = round(best_put["delta"],    3)
            result["put_premium"]  = best_put["premium"]

        if best_call or best_put:
            log.info(f"ATM snapshot: call δ={result['call_delta']:.3f} ${result['call_premium']:.2f} | "
                     f"put δ={result['put_delta']:.3f} ${result['put_premium']:.2f} | spot=${spot:.2f}")
        else:
            log.warning(f"ATM extract: FAILED to find ATM options near spot=${spot:.2f} "
                        f"(chain has {len(chain_data.get('strike', []))} strikes, "
                        f"rows={len(rows)}, checked={rows_checked})")
    except Exception as e:
        log.warning(f"ATM option data extraction failed: {e}", exc_info=True)

    return result


# _post_em_card — v4 INTEGRATED
# Now shows confidence, downgrades, trade sign, vol regime from v4 engine.
# _calc_bias 14-signal scoring preserved exactly.
# ─────────────────────────────────────────────────────────────────────

def _post_em_card(ticker: str, session: str):
    try:
        import pytz
        ct = pytz.timezone("America/Chicago"); now_ct = datetime.now(ct); today_dt = now_ct.date()
        is_afternoon = (session == "afternoon")

        # v4.2: Auto-detect after-hours — if market is closed and session
        # is not explicitly "afternoon", switch to next-day preview mode.
        # Market hours: 8:30 AM – 3:00 PM CT
        market_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
        market_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
        is_market_closed = now_ct > market_close_ct or now_ct < market_open_ct

        if not is_afternoon and is_market_closed:
            log.info(f"EM card: {session} session at {now_ct.strftime('%I:%M %p CT')} — "
                     f"market closed, auto-switching to next-day preview")
            is_afternoon = True  # treat as afternoon/next-day

        if is_afternoon:
            target_date_str = _get_next_trading_day(today_dt)
            hours_for_em = 6.5; session_emoji = "🌆"; session_label = "Next Day Preview"
            horizon_note = f"Full session EM for {target_date_str}"
        else:
            target_date_str = today_dt.strftime("%Y-%m-%d")
            hours_for_em = max((market_close_ct - now_ct).total_seconds() / 3600, 0.25)
            # v15: Distinguish pre-open from live session for thesis label
            if now_ct >= market_open_ct:
                session_emoji = "🔔"; session_label = "Today (Live)"
            else:
                session_emoji = "🌅"; session_label = "Today (Pre-Open)"
            horizon_note = f"{hours_for_em:.1f}h remaining today"

        # v4: 9-element tuple now includes v4_result
        result_tuple = _get_0dte_iv(ticker, target_date_str)
        iv, spot, expiration = result_tuple[0], result_tuple[1], result_tuple[2]
        eng, walls, skew, pcr, vix = result_tuple[3], result_tuple[4], result_tuple[5], result_tuple[6], result_tuple[7]
        v4_result = result_tuple[8] if len(result_tuple) > 8 else {}

        if iv is None or spot is None:
            log.warning(f"EM card skipped for {ticker}: IV unavailable")
            post_to_telegram(f"⚠️ {ticker} EM: could not fetch IV for {target_date_str}")
            return

        em = _calc_intraday_em(spot, iv, hours_for_em)
        if not em: return

        # v4.3: VIX proxy from chain IV when all VIX API sources fail.
        # For SPY, ATM IV × 100 ≈ VIX. For others, close enough for regime classification.
        if not vix or not vix.get("vix"):
            if iv and iv > 0:
                proxy_vix = round(iv * 100, 1)
                vix = {"vix": proxy_vix, "vix9d": None, "term": "unknown", "source": "iv_proxy"}
                log.info(f"VIX proxy from {ticker} ATM IV: {proxy_vix:.1f} (all API sources failed)")

        vol_regime = get_canonical_vol_regime(ticker, get_daily_candles(ticker, days=30), vix_override=vix)
        bias = _calc_bias(spot, em, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        iv_pct = iv * 100
        if iv_pct < 10: iv_emoji = "🟢"; iv_note = "Low IV — tight range."
        elif iv_pct < 20: iv_emoji = "🟡"; iv_note = "Moderate IV — EM ranges reliable."
        elif iv_pct < 35: iv_emoji = "🔴"; iv_note = "Elevated IV — respect stops."
        else: iv_emoji = "🚨"; iv_note = "EXTREME IV — EM may understate. Minimum size."

        # ══════ HEADER ══════
        lines = [
            f"{session_emoji} {ticker} — Institutional EM Brief ({session_label})",
            f"Spot: ${spot:.2f}  |  IV: {iv_emoji} {iv_pct:.1f}%  |  Exp: {expiration}",
            f"Session: {horizon_note}",
        ]

        # ── v4.3: Confidence (single line, no duplicate) ──
        if v4_result:
            conf = v4_result.get("confidence", {})
            dg = v4_result.get("downgrades", [])
            conf_label_str = conf.get("label", "?")
            conf_score = conf.get("composite", 0)
            conf_line = f"Confidence: {conf_label_str} ({conf_score:.0%})"
            if dg:
                short_dg = [d.split(":")[0] for d in dg[:2]]
                conf_line += " | Data note: " + ", ".join(short_dg)
            lines.append(conf_line)

        if vix and vix.get("vix"):
            v = vix["vix"]; v9d = vix.get("vix9d"); term = vix.get("term", "")
            term_str = {"inverted": " 🚨 INVERTED", "normal": " ✅ normal", "flat": " flat"}.get(term, "")
            v9d_str = f"  VIX9D: {v9d}{term_str}" if v9d else ""
            lines.append(f"VIX: {v}{v9d_str}")

        # ── v4: Vol regime from engine ──
        if v4_result:
            vr_line = format_vol_regime_line(v4_result)
            if vr_line:
                lines.append(f"Vol: {vr_line}")
        cvr_line = _format_canonical_vol_line(vol_regime)
        if cvr_line:
            lines.append(f"Vol Overlay: {cvr_line}")

        # ══════ EXPECTED MOVE ══════
        lines += _format_em_block(em, spot, bias["score"])

        # ══════ DEALER FLOW ══════
        if eng:
            tgex = eng.get("gex", 0); dex = eng.get("dex", 0)
            vanna_m = eng.get("vanna", 0); charm_m = eng.get("charm", 0)
            flip = eng.get("flip_price"); regime = eng.get("regime", {})

            gex_icon = "🧲" if tgex >= 0 else "⚡"
            gex_sign = f"+${tgex:.1f}M" if tgex >= 0 else f"-${abs(tgex):.1f}M"
            gex_mode = "SUPPRESSING moves" if tgex >= 0 else "AMPLIFYING moves"
            dex_note = "dealers LONG → SELL on drops" if dex >= 0 else "dealers SHORT → BUY on rallies"
            vanna_note = "IV spike → dealer BUY" if vanna_m >= 0 else "IV spike → dealer SELL"
            charm_note = "removes sell hedges (bullish)" if charm_m >= 0 else "adds sell hedges (bearish)"

            lines += ["", "─" * 32, "⚙️ DEALER FLOW",
                f"  {gex_icon} GEX:   {gex_sign}  —  {gex_mode}",
                f"  📍 Flip:  ${flip:.2f}  ({'above' if spot > flip else 'BELOW'})" if flip else "  📍 Flip:  not found",
                f"  📊 DEX:   {'+'if dex>=0 else ''}{dex:.1f}M  —  {dex_note}",
                f"  🌊 Vanna: {'+'if vanna_m>=0 else ''}{vanna_m:.1f}M  —  {vanna_note}",
                f"  ⏱️ Charm: {'+'if charm_m>=0 else ''}{charm_m:.1f}M  —  {charm_note}",
            ]
            if regime.get("preferred"): lines.append(f"  ✅ Favors: {regime['preferred']}")
            if regime.get("avoid"): lines.append(f"  ❌ Avoid:  {regime['avoid']}")

        # ── v4: Trade sign ──
        if v4_result:
            ts_line = format_trade_sign_line(v4_result)
            if ts_line:
                lines.append(f"  🔍 {ts_line}")

        # ══════ INSTITUTIONAL FLOW MODEL (CAGF) — SPY/QQQ ══════
        cagf = None
        dte_rec = None
        if eng and ticker.upper() in ("SPY", "QQQ", "SPX"):
            # Compute CAGF from dealer flows
            import pytz as _pytz
            _ct = _pytz.timezone("America/Chicago")
            _now_ct = datetime.now(_ct)
            _mkt_open = _now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
            _mkt_close = _now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
            _session_secs = (_mkt_close - _mkt_open).total_seconds()
            _elapsed = max(0, (_now_ct - _mkt_open).total_seconds())
            _sess_progress = min(1.0, _elapsed / _session_secs) if _session_secs > 0 else 0.5

            # Get RV from v4 result
            _rv = 0
            if v4_result and v4_result.get("vol_regime"):
                _rv = v4_result["vol_regime"].get("realized_vol_20d", 0) or 0

            _vix_val = vix.get("vix", 20) if vix else 20

            # Get ADV for normalization
            _adv, _ = _estimate_liquidity(ticker, spot)

            # Get candle closes for momentum component
            _closes = get_daily_candles(ticker, days=60)

            cagf = compute_cagf(
                dealer_flows={
                    "gex": eng.get("gex", 0),
                    "dex": eng.get("dex", 0),
                    "vanna": eng.get("vanna", 0),
                    "charm": eng.get("charm", 0),
                    "gamma_flip": eng.get("flip_price"),
                },
                iv=iv,
                rv=_rv,
                spot=spot,
                vix=_vix_val,
                session_progress=_sess_progress,
                adv=_adv,
                candle_closes=_closes,
                ticker=ticker,
            )
            lines += format_cagf_block(cagf)

            # DTE recommendation
            dte_rec = recommend_dte(
                cagf=cagf,
                iv=iv,
                vix=_vix_val,
                session_progress=_sess_progress,
            )
            lines += format_dte_block(dte_rec)

            log.info(f"CAGF {ticker}: edge={cagf['edge']:+.2f} dir={cagf['direction']} "
                     f"trend={cagf['trend_day_probability']:.0%} regime={cagf['regime']} "
                     f"dte_rec={dte_rec['primary']['label']}")

        # ══════ KEY LEVELS ══════
        if walls:
            lines += ["", "─" * 32, "🧱 KEY LEVELS"]
            if "call_wall" in walls:
                cw = walls["call_wall"]; cw_oi = walls["call_wall_oi"]
                dist = ((cw - spot) / spot) * 100
                cw_tag = "⚠️ INSIDE 1σ" if cw <= em["bull_1sd"] else ("within 2σ" if cw <= em["bull_2sd"] else "outside 2σ")
                lines.append(f"  📵 Resistance: ${cw:.0f}  ({cw_oi:,} OI, +{dist:.1f}%) — {cw_tag}")
            if "put_wall" in walls:
                pw = walls["put_wall"]; pw_oi = walls["put_wall_oi"]
                dist = ((spot - pw) / spot) * 100
                pw_tag = "⚠️ INSIDE 1σ" if pw >= em["bear_1sd"] else ("within 2σ" if pw >= em["bear_2sd"] else "outside 2σ")
                lines.append(f"  🛡️ Support:    ${pw:.0f}  ({pw_oi:,} OI, -{dist:.1f}%) — {pw_tag}")
            if "gamma_wall" in walls:
                gw = walls["gamma_wall"]; gw_dist = ((gw - spot) / spot) * 100
                lines.append(f"  🎯 Gamma Wall: ${gw:.0f}  ({gw_dist:+.1f}%)")
            if "call_top3" in walls:
                lines.append(f"  📵 Resistance stack: {'  →  '.join(f'${x:.0f}' for x in walls['call_top3'])}")
            if "put_top3" in walls:
                lines.append(f"  🛡️ Support stack:    {'  →  '.join(f'${x:.0f}' for x in walls['put_top3'])}")
            if ("call_wall" in walls and "put_wall" in walls and
                walls["call_wall"] <= em["bull_1sd"] and walls["put_wall"] >= em["bear_1sd"]):
                pin_w = walls["call_wall"] - walls["put_wall"]
                lines.append(f"  📌 PIN ZONE: ${walls['put_wall']:.0f} – ${walls['call_wall']:.0f}  (${pin_w:.0f} wide)")

        # ══════ SENTIMENT ══════
        skew_str = _format_skew_line(skew)
        pcr_oi_str = f"OI {pcr['pcr_oi']:.2f}" if pcr and pcr.get("pcr_oi") is not None else "OI n/a"
        pcr_vol_str = f"Vol {pcr['pcr_vol']:.2f}" if pcr and pcr.get("pcr_vol") is not None else "Vol n/a"
        lines += ["", "─" * 32, "📊 SENTIMENT", f"  PCR:  {pcr_oi_str}  |  {pcr_vol_str}"]
        if skew_str: lines.append(skew_str)

        # ══════ DIRECTIONAL LEAN ══════
        dir_emoji = {"STRONG BULLISH": "🟢🟢", "BULLISH": "🟢", "SLIGHT BULLISH": "🟡",
                     "NEUTRAL": "⚪", "SLIGHT BEARISH": "🟠", "BEARISH": "🔴", "STRONG BEARISH": "🔴🔴"
                    }.get(bias["direction"], "⚪")
        strength_str = f"  [{bias['strength']}]" if bias.get("strength") else ""
        score_str = f"{bias['score']:+d}/{bias['max_score']}"
        na_count = bias.get("na_count", 0)
        dot_bar = ("▲" * bias["up_count"]) + ("▼" * bias["down_count"]) + ("◆" * bias["neu_count"]) + ("—" * na_count)

        lines += ["", "═" * 32, f"{dir_emoji}  BIAS MODEL: {bias['direction']}{strength_str}",
            f"Score: {score_str}  |  {dot_bar}",
            f"  ▲ {bias['up_count']} bullish  ·  ▼ {bias['down_count']} bearish  ·  ◆ {bias['neu_count']} neutral" + (f"  ·  — {na_count} n/a" if na_count else ""),
            "", f"📋 {bias['verdict']}", "", "── Signal Breakdown ({}/14) ──".format(bias['n_signals'])]
        for arrow, text in bias["signals"]:
            lines.append(f"  {arrow}  {text}")
        # ── TRADE DAY GATE ────────────────────────────────────────────────────
        # Determines whether conditions are suitable for live options trading.
        # If not, an emphatic banner is added to the card so there is zero
        # ambiguity.  The intraday engine still runs and logs everything for
        # analysis — this gate is purely a human communication tool.
        #
        # TRADEABLE requires ALL of:
        #   1. Trend day probability >= 0.60  (cagf says market has direction)
        #   2. CAGF regime is TRENDING or SUPPRESSING  (not NEUTRAL/pinned)
        #   3. Vol regime is not CRISIS
        #   4. VIX term structure is not INVERTED  (contango required)
        # ─────────────────────────────────────────────────────────────────────
        _cagf_regime      = (cagf or {}).get("regime", "NEUTRAL")
        _trend_prob       = (cagf or {}).get("trend_day_probability", 0.0)
        _vol_base         = (vol_regime or {}).get("regime", "NORMAL")
        _term_struct      = (vix or {}).get("term", "unknown")

        _regime_ok   = _cagf_regime in ("TRENDING", "SUPPRESSING")
        _trend_ok    = _trend_prob >= 0.60
        _vol_ok      = _vol_base not in ("CRISIS",)
        _term_ok     = _term_struct not in ("inverted",)

        _tradeable   = _regime_ok and _trend_ok and _vol_ok and _term_ok

        # Build the reasons list for the banner so the trader knows exactly why
        _no_trade_reasons = []
        if not _regime_ok:
            _no_trade_reasons.append(
                f"Regime is {_cagf_regime} — no directional edge (need TRENDING or SUPPRESSING)"
            )
        if not _trend_ok:
            _no_trade_reasons.append(
                f"Trend probability {_trend_prob:.0%} — below 60% threshold"
            )
        if not _vol_ok:
            _no_trade_reasons.append(
                f"Vol regime is {_vol_base} — gap risk too high for defined-risk trades"
            )
        if not _term_ok:
            _no_trade_reasons.append(
                f"VIX term structure INVERTED — elevated tail risk, stand aside"
            )

        if _tradeable:
            # Positive confirmation — clear green light with key stat
            lines += [
                "",
                "🟢" * 10,
                f"✅  VALID TRADE DAY — {ticker}",
                f"    Regime: {_cagf_regime}  |  Trend prob: {_trend_prob:.0%}  |  Vol: {_vol_base}",
                "    Intraday alerts are ACTIONABLE. Size normally.",
                "🟢" * 10,
            ]
        else:
            # No-trade day — make it impossible to miss
            lines += [
                "",
                "🚫" * 10,
                f"❌  NO VALID TRADES TODAY — {ticker}",
                "",
            ]
            for _r in _no_trade_reasons:
                lines.append(f"    ▸ {_r}")
            lines += [
                "",
                "    The intraday engine is still running and logging all",
                "    signals for analysis — DO NOT trade them live.",
                "    Treat today as observation only.",
                "",
                "    Wait for a morning card that shows ✅ VALID TRADE DAY",
                "    before putting real money on any alert.",
                "🚫" * 10,
            ]

        log.info(
            f"Trade day gate: {ticker} | tradeable={_tradeable} | "
            f"regime={_cagf_regime} trend_prob={_trend_prob:.0%} "
            f"vol={_vol_base} term={_term_struct}"
        )
        # ─────────────────────────────────────────────────────────────────────

        lines += ["═" * 32, "", f"💡 {iv_note}", "— Not financial advice —"]

        log.info(f"EM snapshot built: {ticker} | {session_label} | spot={spot} | IV={iv_pct:.1f}% | "
                 f"EM=±${em['em_1sd']} | score={bias['score']} | lean={bias['direction']} | "
                 f"conf={v4_result.get('confidence', {}).get('label', '?')}")

        # ── EM accuracy logger: save prediction for backtest ──
        _log_em_prediction(ticker, session, spot, em, bias, v4_result, walls=walls, eng=eng, cagf=cagf, vol_regime=vol_regime)

        # ── v4.3: Resolve unified regime for trade card + plain cards ──
        unified_regime = resolve_unified_regime(eng or {}, cagf, spot)

        # Live /em output should include the richer dealer brief when no trade qualifies.

        # ── Compute session progress for trade card timing gates ──
        _mkt_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
        _mkt_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
        _session_secs = (_mkt_close_ct - _mkt_open_ct).total_seconds()
        _elapsed_secs = max(0, (now_ct - _mkt_open_ct).total_seconds())
        _sess_progress = min(1.0, _elapsed_secs / _session_secs) if _session_secs > 0 else 0.5

        # ── Thesis Monitor: store thesis for continuous monitoring ──
        try:
            _prior_close = None
            _daily = get_daily_candles(ticker, days=3)
            if _daily and len(_daily) >= 2:
                _prior_close = _daily[-2] if isinstance(_daily[-2], (int, float)) else None

            _thesis_chain_struct = _derive_structure_levels_from_chain({}, spot, walls or {}, eng or {})
            _thesis_price_struct = _compute_price_structure_levels(ticker, spot, days=90)
            _thesis_local_walls = _merge_price_structure_with_walls(_thesis_price_struct, _thesis_chain_struct, spot, em)

            # ── ATM option snapshot for premium stop ──
            # Re-uses the cached chain (no extra API call). Extracts the nearest
            # ATM call and put delta + mid price so the thesis monitor can
            # approximate a 20% premium stop each poll without live quotes.
            _atm_chain, _atm_spot, _ = _get_0dte_chain(ticker, target_date_str)
            _atm_data = _extract_atm_option_data(_atm_chain or {}, _atm_spot or spot)

            _thesis = build_thesis_from_em_card(
                ticker=ticker,
                spot=spot,
                bias=bias,
                eng=eng or {},
                em=em,
                walls=walls or {},
                cagf=cagf,
                vix=vix or {},
                v4_result=v4_result,
                session_label=session_label,
                local_walls=_thesis_local_walls,
                prior_day_close=_prior_close,
                atm_call_delta=_atm_data["call_delta"],
                atm_call_premium=_atm_data["call_premium"],
                atm_put_delta=_atm_data["put_delta"],
                atm_put_premium=_atm_data["put_premium"],
            )
            get_thesis_engine().store_thesis(ticker, _thesis)
            log.info(f"Thesis stored for monitoring: {ticker} | {session_label}")
        except Exception as _te:
            log.warning(f"Thesis store failed for {ticker}: {_te}")

        _post_trade_card(
            ticker=ticker, spot=spot, expiration=expiration,
            eng=eng or {}, walls=walls or {}, bias=bias, em=em,
            vix=vix or {}, pcr=pcr or {}, is_0dte=(not is_afternoon), v4_result=v4_result,
            cagf=cagf, dte_rec=dte_rec,
            now_ct=now_ct, session_progress=_sess_progress,
            is_next_day=is_afternoon, unified_regime=unified_regime,
            canonical_vol=vol_regime,
        )

        # ── Post follow-up plain English guidance from thesis monitor ──
        try:
            _te = get_thesis_engine()
            _thesis_obj = _te.get_thesis(ticker)
            if _thesis_obj:
                # Seed first price into fresh state — intentional, this is thesis creation
                # NOT a read-only guidance call. Without this, build_guidance has no price_history.
                _te.evaluate(ticker, spot)
                guidance = _te.build_guidance(ticker, spot)
                g_lines = [f"📡 {ticker} — ACTION GUIDE @ ${spot:.2f}", ""]
                for item in guidance:
                    if item["type"] == "divider":
                        g_lines.append(f"\n{item['text']}")
                    elif item["type"] == "critical":
                        g_lines.append(f"🔥 {item['text']}")
                    elif item["type"] == "warning":
                        g_lines.append(f"⚠️ {item['text']}")
                    elif item["type"] == "bullish":
                        g_lines.append(f"🟢 {item['text']}")
                    elif item["type"] == "bearish":
                        g_lines.append(f"🔴 {item['text']}")
                    elif item["type"] == "time":
                        g_lines.append(f"🕐 {item['text']}")
                    elif item["type"] == "context":
                        g_lines.append(f"📋 {item['text']}")
                    else:
                        g_lines.append(f"  {item['text']}")
                g_lines.append("")
                g_lines.append("📡 Monitoring active — alerts will post if levels break or fail.")
                g_lines.append("Use /monitor guidance for updated read anytime.")
                g_lines.append("— Not financial advice —")
                post_to_telegram("\n".join(g_lines))
                log.info(f"Plain English guidance posted for {ticker}")
        except Exception as _ge:
            log.warning(f"Guidance post failed for {ticker}: {_ge}")

    except Exception as e:
        log.error(f"EM card error for {ticker} ({session}): {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────
# PLAIN ENGLISH ACTION BLOCK — appended to EM/no-trade cards
# Tells the trader WHAT TO DO, not just what the levels are.
# ─────────────────────────────────────────────────────────────────────

def _build_action_block(
    ticker: str, spot: float, eng: dict, bias: dict, em: dict,
    local_walls: dict = None, cagf: dict = None,
) -> list:
    """Build plain English 'what to do' lines for EM cards."""
    lines = []
    eng = eng or {}
    lw = local_walls or {}
    cagf = cagf or {}
    tgex = eng.get("gex", 0)
    flip = eng.get("flip_price")
    struct_r = lw.get("local_resistance_1")
    struct_s = lw.get("local_support_1")
    pin_low = lw.get("pin_zone_low")
    pin_high = lw.get("pin_zone_high")
    em_1sd = em.get("em_1sd") or 0.0
    range_low = em.get("bear_1sd")
    range_high = em.get("bull_1sd")

    # v4.3: Reconcile GEX sign with gamma flip position.
    # Raw GEX can be positive, but if spot is far below the flip,
    # the effective dealer positioning is amplifying, not suppressing.
    # This catches the contradiction where the card says "GEX positive"
    # while spot is $17 below the flip in full trending territory.
    gex_positive = tgex >= 0
    if flip is not None and spot > 0:
        dist_from_flip_pct = (flip - spot) / spot * 100
        if dist_from_flip_pct > 1.5:
            # Spot is meaningfully below flip — effective gamma is negative
            # even if raw GEX number is positive
            gex_positive = False
            log.info(f"GEX sign overridden: raw GEX {tgex:+.1f}M but spot is {dist_from_flip_pct:.1f}% below flip — treating as negative gamma")
        elif dist_from_flip_pct < -1.5:
            # Spot is meaningfully above flip — effective gamma is positive
            gex_positive = True

    # v4.3: Pin zone sanity — check if it's actionable or absurdly wide
    pin_actionable = False
    if pin_low is not None and pin_high is not None and em_1sd > 0:
        pin_width = abs(pin_high - pin_low)
        pin_actionable = pin_width <= 2.5 * em_1sd

    # v4.3: Range break triggers — use tight pin if actionable, else EM boundaries
    if pin_actionable:
        range_break_up = pin_high
        range_break_dn = pin_low
    else:
        range_break_up = range_high or struct_r or lw.get("call_wall")
        range_break_dn = range_low or struct_s or lw.get("put_wall")

    lines.append("")
    lines.append("─" * 32)
    lines.append("📡 WHAT TO DO — Plain English")

    # ── Time of day context ──
    try:
        from thesis_monitor import _get_time_phase_ct
        tp = _get_time_phase_ct()
        lines.append(f"🕐 {tp['label']}: {tp['note']}")
    except Exception:
        pass

    # ── GEX environment (the single most important thing) ──
    if gex_positive:
        lines.append("⚙️ GEX is POSITIVE — failed moves and mean reversion are MORE LIKELY than continuation.")
        lines.append("   → Don't chase breakouts. TRADE THE FAILURES.")
    else:
        lines.append("⚙️ GEX is NEGATIVE — moves can ACCELERATE. Breakdowns are dangerous.")
        lines.append("   → Respect the breaks. Use wider stops or smaller size.")

    # ── Gamma flip position ──
    if flip is not None:
        dist_note = f" ({abs(flip - spot):.2f} away)" if abs(flip - spot) > em_1sd else ""
        if spot > flip:
            lines.append(f"📈 Above gamma flip ${flip:.2f}{dist_note} — bullish structure. Dealers buy dips.")
        else:
            lines.append(f"📉 Below gamma flip ${flip:.2f}{dist_note} — bearish/trending. Breakdowns can extend.")

    # ── Specific action setups ──
    lines.append("")
    lines.append("🎯 SETUPS TO WATCH:")

    if struct_s is not None:
        if gex_positive:
            lines.append(
                f"  IF price breaks below ${struct_s:.2f} AND fails to continue (2-3 five-minute candles)"
                f" AND reclaims it → GO LONG (squeeze). Shorts get trapped. Stop below ${struct_s:.2f}."
            )
        else:
            lines.append(
                f"  IF price breaks below ${struct_s:.2f} WITH large 5m candles"
                f" AND continues lower → SHORT is valid. Stop above ${struct_s:.2f}."
            )

    if struct_r is not None:
        if gex_positive:
            lines.append(
                f"  IF price breaks above ${struct_r:.2f} AND fails to continue (2-3 five-minute candles)"
                f" AND loses it → GO SHORT (fade). Longs get trapped. Stop above ${struct_r:.2f}."
            )
        else:
            lines.append(
                f"  IF price breaks above ${struct_r:.2f} WITH momentum on 5m candles"
                f" AND holds → LONG is valid. Stop below ${struct_r:.2f}."
            )

    # ── Range break scenarios ──
    if range_break_dn is not None and range_break_dn != struct_s:
        lines.append(
            f"  IF price drops below ${range_break_dn:.2f} (range break) → watch for trap."
            f" Reclaim = squeeze long. Continuation = real breakdown."
        )
    if range_break_up is not None and range_break_up != struct_r:
        lines.append(
            f"  IF price pops above ${range_break_up:.2f} (range break) → watch for trap."
            f" Lost = fade short. Holds = real breakout."
        )

    # ── Momentum filter ──
    lines.append("")
    lines.append("⚠️ MOMENTUM RULES (on 5m chart):")
    lines.append("  A break is ONLY valid with large 5m candles + continuation on the next candle.")
    lines.append("  Small candles breaking a level = likely a TRAP. Wait for the failure.")
    lines.append("  If your trade's momentum fades (5m candles getting smaller) → tighten or exit.")

    # ── Pin zone behavior (only show if actionable) ──
    if pin_actionable and gex_positive:
        lines.append("")
        lines.append(f"📌 PIN ZONE ACTIVE: ${pin_low:.2f}–${pin_high:.2f}")
        lines.append("  Price WANTS to stay here. Fade the edges. Don't chase direction.")

    return lines


# ─────────────────────────────────────────────────────────────────────
# _post_trade_card — v4 INTEGRATED
# Added v4_result parameter for confidence gating.
# ─────────────────────────────────────────────────────────────────────

def _app_spread_width(
    setup_type: str,          # "FAILED" | "BREAK" | "RETEST"
    direction: str,           # "LONG" | "SHORT"  (bull/bear normalised below)
    spot: float,
    em_1sd: float,
    local_wall: float | None, # nearest opposing level price (resistance for longs, support for shorts)
    gex_sign: str,            # "positive" | "negative"
    bias_score: int,
    step: float = 1.0,        # strike increment (1 for SPY/QQQ, 5 for SPX)
) -> float:
    """Compute spread width using the same ladder algorithm as thesis_monitor._contract_suggestion.

    Ensures app.py trade cards and thesis monitor alerts always agree on width.

    Steps:
      1  usable EM fraction by setup type
      2  d3 = em_1sd × fraction
      3  d1 = distance to nearest opposing level
      4  target_move = min(d1, d3) if both available, else whichever exists
      5  raw_width = 0.80 × target_move
      6  snap to step increments, apply hard caps
      7  quality modifier (narrow on weak conditions, widen on strong)
    """
    # Step 1 — usable EM fraction
    # v4.3: FAILED raised from 0.30→0.40 — narrow spreads ($1-$2) can't
    # overcome theta intraday. Need at least $3 wide for delta to dominate.
    fracs = {"FAILED": 0.40, "RETEST": 0.35, "BREAK": 0.50}
    frac = fracs.get(setup_type, 0.30)

    # Step 2 — EM budget
    d3 = (em_1sd * frac) if em_1sd > 0 else None

    # Step 3 — nearest opposing level distance
    d1 = None
    if local_wall is not None and local_wall > 0:
        d1 = abs(local_wall - spot)
        if d1 <= 0:
            d1 = None

    # Step 4 — target move
    # v4.3: EM floor — nearby levels cap the width via min(d1, d3), but the
    # result can't go below 50% of the EM budget. This prevents pin zones
    # (where every level is $1-2 away) from forcing $1-wide spreads that
    # can't overcome theta intraday. The EM budget always gets at least
    # half its weight so delta has room to work.
    candidates = [x for x in [d1, d3] if x is not None and x > 0]
    level_capped = min(candidates) if candidates else step
    em_floor = (d3 * 0.50) if d3 is not None and d3 > 0 else step
    target_move = max(level_capped, em_floor)

    # Step 5 — raw width
    raw_width = 0.80 * target_move

    # Step 6 — snap to step increments
    snapped = max(step, round(raw_width / step) * step)

    # Hard caps by setup type
    # v4.3: FAILED raised from 2→3 — $1-$2 spreads need the underlying to
    # blow past the short strike to profit, which rarely happens intraday.
    # $3-wide gives delta room to work before theta eats the gain.
    max_widths = {"FAILED": 3 * step, "RETEST": 3 * step, "BREAK": 4 * step}
    snapped = min(snapped, max_widths.get(setup_type, 3 * step))

    # Step 7 — quality modifier
    narrow = (
        (gex_sign == "positive" and setup_type == "BREAK") or
        abs(bias_score) <= 2 or
        (d1 is not None and d1 < 0.75)
    )
    widen = (
        setup_type == "BREAK" and
        (d1 is not None and d1 > 2.0) and
        abs(bias_score) >= 4 and
        gex_sign == "negative"
    )

    if narrow:
        snapped = max(step, snapped - step)
    elif widen:
        snapped = min(max_widths.get(setup_type, 3 * step), snapped + step)

    return float(snapped)


def _post_trade_card(ticker, spot, expiration, eng, walls, bias, em, vix, pcr,
                     is_0dte=True, v4_result=None, cagf=None, dte_rec=None,
                     now_ct=None, session_progress=None, is_next_day=False,
                     unified_regime=None, canonical_vol=None):
    """
    Trade recommendation card — v4.2.
    CAGF-integrated: respects DTE recommendation, session timing, and EM-aware strike placement.

    v4.2 fixes:
      - Timestamp uses CT timezone (not server-local/UTC)
      - Session gate: blocks 0DTE when <15 min remain or market closed
      - DTE recommendation enforced: if CAGF says avoid 0DTE, card uses recommended DTE
      - Afternoon session → next-day trade card (not 0DTE)
      - Strikes constrained to EM 1σ boundary (short strike inside expected move)
      - Width scaled relative to EM range (no wider than 1σ move)
    """
    try:
        direction = bias.get("direction", "NEUTRAL")
        score = bias.get("score", 0)
        is_bull = "BULL" in direction
        tgex = eng.get("gex", 0) if eng else 0
        dex = eng.get("dex", 0) if eng else 0
        flip = eng.get("flip_price") if eng else None
        charm_m = eng.get("charm", 0) if eng else 0
        neg_gex = tgex < 0
        v = vix.get("vix", 20) if vix else 20
        exp_short = expiration[5:] if expiration else "?"

        # ── CT timestamp (v4.2 fix) ──
        if now_ct is None:
            try:
                import pytz
                now_ct = datetime.now(pytz.timezone("America/Chicago"))
            except Exception:
                now_ct = datetime.now()
        time_str = now_ct.strftime('%I:%M %p CT')

        # ── Determine effective DTE from CAGF recommendation ──
        # Start with the raw is_0dte flag, then let CAGF override
        effective_dte_label = "0DTE" if is_0dte else "NEXT DAY"
        effective_dte = 0 if is_0dte else 1
        dte_was_upgraded = False
        dte_upgrade_reason = ""

        # v4.2: CAGF DTE enforcement — if CAGF says avoid 0DTE, use recommended DTE
        if dte_rec and dte_rec.get("primary"):
            rec_dte = dte_rec["primary"].get("dte", 0)
            rec_label = dte_rec["primary"].get("label", "0DTE")
            avoid_list = dte_rec.get("avoid", [])

            # Check if 0DTE is in the avoid list
            dte_0_avoided = any("0DTE" in a for a in avoid_list)
            dte_1_avoided = any("1DTE" in a for a in avoid_list)

            if is_0dte and dte_0_avoided:
                # CAGF says no 0DTE — upgrade to recommended DTE
                effective_dte = rec_dte
                effective_dte_label = rec_label
                dte_was_upgraded = True
                dte_upgrade_reason = avoid_list[0] if avoid_list else "0DTE conditions not met"
                log.info(f"Trade card DTE upgraded: 0DTE → {rec_label} | {dte_upgrade_reason}")

        # v4.2: Next-day session → always at least 1 DTE, prefer CAGF recommendation
        if is_next_day:
            if dte_rec and dte_rec.get("primary"):
                rec_dte = dte_rec["primary"].get("dte", 1)
                effective_dte = max(rec_dte, 1)
                effective_dte_label = dte_rec["primary"]["label"]
            else:
                effective_dte = max(effective_dte, 1)
                effective_dte_label = f"{effective_dte} DTE"
            dte_was_upgraded = True
            dte_upgrade_reason = "afternoon session → next trading day"

        # ── Session timing gate (v4.2 fix) ──
        # Block 0DTE trades when market is closed or <15 min remain
        if effective_dte == 0 and session_progress is not None:
            mkt_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
            mkt_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
            is_market_hours = mkt_open_ct <= now_ct <= mkt_close_ct
            minutes_remaining = max(0, (mkt_close_ct - now_ct).total_seconds() / 60)

            if not is_market_hours:
                # Outside market hours — upgrade to next-day DTE
                if dte_rec and dte_rec.get("primary"):
                    effective_dte = max(dte_rec["primary"].get("dte", 1), 1)
                    effective_dte_label = dte_rec["primary"].get("label", "1 DTE")
                else:
                    effective_dte = 1
                    effective_dte_label = "1 DTE"
                dte_was_upgraded = True
                dte_upgrade_reason = f"market closed ({time_str})"
                log.info(f"Trade card: 0DTE blocked — market closed at {time_str}")

            elif minutes_remaining < 15:
                # Less than 15 minutes left — too late for 0DTE
                if dte_rec and dte_rec.get("primary"):
                    effective_dte = max(dte_rec["primary"].get("dte", 1), 1)
                    effective_dte_label = dte_rec["primary"].get("label", "1 DTE")
                else:
                    effective_dte = 1
                    effective_dte_label = "1 DTE"
                dte_was_upgraded = True
                dte_upgrade_reason = f"<15 min to close ({minutes_remaining:.0f} min left)"
                log.info(f"Trade card: 0DTE blocked — only {minutes_remaining:.0f} min remaining")

        # ── Build card title based on effective DTE ──
        if effective_dte == 0:
            card_title = "0DTE TRADE SETUP"
        else:
            card_title = f"TRADE SETUP ({effective_dte_label})"

        def no_trade(reason, emoji="🟡"):
            chain_struct = _derive_structure_levels_from_chain({}, spot, walls or {}, eng or {})
            price_struct = _compute_price_structure_levels(ticker, spot, days=90)
            local_walls = _merge_price_structure_with_walls(price_struct, chain_struct, spot, em)
            flip = local_walls.get("gamma_flip") or (eng or {}).get("flip_price")
            call_wall = local_walls.get("call_wall")
            put_wall = local_walls.get("put_wall")
            gamma_wall = local_walls.get("gamma_wall")
            max_pain = local_walls.get("max_pain") or (eng or {}).get("max_pain")
            bias_line = f"{bias['direction']} (score {bias['score']}/14)"
            range_low = em.get('bear_1sd')
            range_high = em.get('bull_1sd')
            struct_r = local_walls.get("local_resistance_1")
            struct_s = local_walls.get("local_support_1")
            regime_line = _format_unified_regime_line(unified_regime)
            pin_low = local_walls.get("pin_zone_low")
            pin_high = local_walls.get("pin_zone_high")
            pivot = local_walls.get("pivot")
            r1 = local_walls.get("r1")
            s1 = local_walls.get("s1")
            fib_r = local_walls.get("fib_resistance")
            fib_s = local_walls.get("fib_support")
            vpoc = local_walls.get("vpoc")
            em_1sd = em.get("em_1sd") or 0.0
            pin_width = abs((pin_high or 0) - (pin_low or 0)) if pin_low is not None and pin_high is not None else None
            max_pain_close = bool(max_pain is not None and em_1sd > 0 and abs(max_pain - spot) <= 0.35 * em_1sd)
            tight_pin = bool(pin_width is not None and em_1sd > 0 and pin_width <= 2.2 * em_1sd)
            pin_favored = tight_pin or max_pain_close

            micro_up = struct_r
            micro_dn = struct_s
            # v4.3: Don't use pin zone edges as range break triggers when zone is too wide
            if tight_pin:
                range_break_up = pin_high or call_wall or range_high
                range_break_dn = pin_low or put_wall or range_low
            else:
                # Fall back to EM 1σ boundaries or local structure
                range_break_up = range_high or struct_r or call_wall
                range_break_dn = range_low or struct_s or put_wall
            regime_shift_up = flip if flip is not None and flip > spot else None
            regime_shift_dn = flip if flip is not None and flip < spot else None

            headline_emoji = emoji
            headline_text = reason
            if pin_favored and abs(bias.get("score", 0)) <= 1:
                headline_emoji = "⚪"
                if pin_low is not None and pin_high is not None:
                    zone_txt = f"{_fmt_money(pin_low)}–{_fmt_money(pin_high)}"
                else:
                    zone_txt = f"{_fmt_money(range_low)}–{_fmt_money(range_high)}"
                if bias.get("direction") == "NEUTRAL":
                    headline_text = f"RANGE / PIN RISK — no directional edge inside {zone_txt}."
                else:
                    headline_text = f"RANGE / PIN RISK — {bias.get('direction').lower()} lean too weak inside {zone_txt}."

            lines = [
                f"🎯 {ticker} — DEALER EM BRIEF ({effective_dte_label})  |  Exp: {exp_short}",
                f"{headline_emoji} NO TRADE — {headline_text}",
                "",
                f"🧭 Bias: {bias_line}",
                f"📍 Spot: {_fmt_money(spot)}",
                f"📐 1σ Range: {_fmt_money(range_low)} – {_fmt_money(range_high)}",
            ]
            if flip is not None:
                side_vs_flip = "above" if spot > flip else "below"
                lines.append(f"🌀 Gamma Flip: {_fmt_money(flip)}  (spot {side_vs_flip})")
            if max_pain is not None:
                lines.append(f"🧲 Max Pain: {_fmt_money(max_pain)}")
            if call_wall is not None:
                lines.append(f"📵 Call Wall / Resistance: {_fmt_money(call_wall)}")
            if put_wall is not None:
                lines.append(f"🛡️ Put Wall / Support: {_fmt_money(put_wall)}")
            if gamma_wall is not None:
                lines.append(f"🎯 Gamma Wall: {_fmt_money(gamma_wall)}")
            if struct_r is not None:
                src_txt = local_walls.get("local_resistance_sources")
                lines.append(f"🧱 Local Resistance: {_fmt_money(struct_r)}" + (f"  ({src_txt})" if src_txt else ""))
            if struct_s is not None:
                src_txt = local_walls.get("local_support_sources")
                lines.append(f"🧱 Local Support: {_fmt_money(struct_s)}" + (f"  ({src_txt})" if src_txt else ""))
            if pivot is not None:
                pivot_line = f"🧭 Pivot: {_fmt_money(pivot)}"
                if r1 is not None and s1 is not None:
                    pivot_line += f"  |  R1 {_fmt_money(r1)} / S1 {_fmt_money(s1)}"
                lines.append(pivot_line)
            if fib_r is not None or fib_s is not None:
                fib_left = _fmt_money(fib_s) if fib_s is not None else "n/a"
                fib_right = _fmt_money(fib_r) if fib_r is not None else "n/a"
                lines.append(f"🪜 Fib Zone: {fib_left} ↔ {fib_right}")
            if vpoc is not None:
                lines.append(f"📊 VPOC / Acceptance: {_fmt_money(vpoc)}")
            if pin_low is not None and pin_high is not None:
                if tight_pin:
                    lines.append(f"📌 Pin Zone: {_fmt_money(pin_low)} – {_fmt_money(pin_high)}")
                    lines.append("🤝 Neutral read: range / condor structure favored while price stays inside the pin zone.")
                else:
                    # v4.3: Don't show absurdly wide pin zones as actionable
                    if em_1sd > 0:
                        lines.append(f"📌 Pin Zone: {_fmt_money(pin_low)} – {_fmt_money(pin_high)}  ⚠️ TOO WIDE ({pin_width:.0f} vs EM ±{em_1sd:.2f}) — not actionable for pinning.")
                    else:
                        lines.append(f"📌 Pin Zone: {_fmt_money(pin_low)} – {_fmt_money(pin_high)}")
            if max_pain_close:
                lines.append(f"🧲 Magnet: spot is trading close to Max Pain {_fmt_money(max_pain)}.")

            if micro_up is not None or micro_dn is not None:
                lines.append("")
                lines.append("⚡ Micro triggers")
                if micro_up is not None:
                    lines.append(f"• Up: above {_fmt_money(micro_up)}")
                if micro_dn is not None:
                    lines.append(f"• Down: below {_fmt_money(micro_dn)}")
            if range_break_up is not None or range_break_dn is not None:
                lines.append("🧨 Range-break triggers")
                if range_break_up is not None:
                    lines.append(f"• Up: above {_fmt_money(range_break_up)}")
                if range_break_dn is not None:
                    lines.append(f"• Down: below {_fmt_money(range_break_dn)}")
            if regime_shift_up is not None or regime_shift_dn is not None:
                lines.append("🔄 Regime-shift trigger")
                if regime_shift_up is not None:
                    lines.append(f"• Reclaim above gamma flip: {_fmt_money(regime_shift_up)}")
                if regime_shift_dn is not None:
                    lines.append(f"• Lose gamma flip: {_fmt_money(regime_shift_dn)}")
            em_shared_rec = {
                "ticker": ticker,
                "spot": spot,
                "structure_overlay_score": 0 if pin_favored else None,
                "structure_local_support": struct_s,
                "structure_local_resistance": struct_r,
                "structure_balance_zone_low": local_walls.get("local_balance_zone_low"),
                "structure_balance_zone_high": local_walls.get("local_balance_zone_high"),
                "structure_outer_bracket_low": local_walls.get("outer_bracket_low"),
                "structure_outer_bracket_high": local_walls.get("outer_bracket_high"),
                "structure_confluence": local_walls.get("structure_confluence"),
            }
            em_snapshot = _um_build_shared_model_snapshot(
                ticker=ticker,
                spot=spot,
                dealer_regime=unified_regime,
                vol_regime=canonical_vol,
                structure_ctx={"ticker": ticker, "spot": spot, "price_structure": local_walls},
                rec=em_shared_rec,
                eng=eng,
                cagf=cagf,
                walls=local_walls,
                mode="em",
                horizon_label=effective_dte_label,
            )
            lines.extend(_um_format_shared_snapshot_lines(em_snapshot))

            # ── Plain English action block ──
            try:
                action_lines = _build_action_block(
                    ticker=ticker, spot=spot, eng=eng or {}, bias=bias, em=em,
                    local_walls=local_walls, cagf=cagf,
                )
                lines.extend(action_lines)
            except Exception as _ab_err:
                log.warning(f"Action block build failed: {_ab_err}")

            lines += [
                "",
                "— Not financial advice —",
            ]
            # ── Thesis Monitor: store thesis even when no trade qualifies ──
            try:
                _nt_chain, _nt_spot, _ = _get_0dte_chain(ticker, expiration)
                _nt_atm = _extract_atm_option_data(_nt_chain or {}, _nt_spot or spot)
                _nt_thesis = build_thesis_from_em_card(
                    ticker=ticker, spot=spot, bias=bias,
                    eng=eng or {}, em=em, walls=walls or {},
                    cagf=cagf, vix=vix or {},
                    v4_result=v4_result,
                    session_label=effective_dte_label,
                    local_walls=local_walls,
                    atm_call_delta=_nt_atm["call_delta"],
                    atm_call_premium=_nt_atm["call_premium"],
                    atm_put_delta=_nt_atm["put_delta"],
                    atm_put_premium=_nt_atm["put_premium"],
                )
                get_thesis_engine().store_thesis(ticker, _nt_thesis)
            except Exception as _te:
                log.warning(f"Thesis persistence failed for {ticker}: {_te}")
            post_to_telegram("\n".join(lines))
            log.info(f"Trade card blocked: {ticker} | {reason}")

        # ══════ GATE CHECKS ══════

        # G0 — v4 confidence gate: LOW data quality = no trade
        if v4_result and v4_result.get("confidence", {}).get("label") == "LOW":
            conf_score = v4_result["confidence"].get("composite", 0)
            dg = v4_result.get("downgrades", [])
            dg_str = "; ".join(d.split(":")[0] for d in dg[:2]) if dg else "insufficient data"
            no_trade(f"Data quality LOW ({conf_score:.0%}) — {dg_str}", "⚠️")
            return

        local_struct = _merge_price_structure_with_walls(
            _compute_price_structure_levels(ticker, spot, days=90),
            _derive_structure_levels_from_chain({}, spot, walls or {}, eng or {}),
            spot,
            em,
        )
        local_call = local_struct.get("local_resistance_1") or local_struct.get("call_wall")
        local_put = local_struct.get("local_support_1") or local_struct.get("put_wall")
        local_max_pain = local_struct.get("max_pain") or (eng or {}).get("max_pain")
        em_1sd_now = em.get("em_1sd") or 0.0
        local_pin = False
        near_max_pain = False
        near_opposing_structure = False
        if local_call is not None and local_put is not None and local_put < spot < local_call:
            pin_width = abs(local_call - local_put)
            if em_1sd_now > 0:
                local_pin = pin_width <= (2.2 * em_1sd_now)
        if local_max_pain is not None and em_1sd_now > 0:
            near_max_pain = abs(local_max_pain - spot) <= (0.35 * em_1sd_now)
        if em_1sd_now > 0:
            if is_bull and local_call is not None:
                near_opposing_structure = (local_call - spot) <= (0.35 * em_1sd_now)
            elif (not is_bull) and local_put is not None:
                near_opposing_structure = (spot - local_put) <= (0.35 * em_1sd_now)

        # G1 — No directional edge
        if direction == "NEUTRAL":
            if local_pin or near_max_pain:
                no_trade(f"Bias NEUTRAL (score {score:+d}/14). Pin / range risk favored.", "⚪")
            else:
                no_trade(f"Bias NEUTRAL (score {score:+d}/14). No directional edge.", "⚪")
            return

        # G2 — Edge too thin
        if direction in ("SLIGHT BULLISH", "SLIGHT BEARISH") and abs(score) < 2:
            if local_pin or near_max_pain:
                no_trade(f"Lean {direction} but score only {score:+d}/14 — pin risk too high.", "🟡")
            elif near_opposing_structure:
                no_trade(f"Lean {direction} but nearby structure is too close to spot.", "🟡")
            else:
                no_trade(f"Lean {direction} but score only {score:+d}/14 — edge too thin.", "🟡")
            return

        # G3 — Flip mismatch
        if flip:
            flip_wrong = (is_bull and spot < flip) or (not is_bull and spot > flip)
            if flip_wrong:
                side_word = "BELOW" if is_bull else "ABOVE"
                no_trade(f"Spot ${spot:.2f} is {side_word} gamma flip ${flip:.2f}.", "🔴")
                return

        # G4 — VIX extreme
        if v >= 40:
            no_trade(f"VIX {v} extreme — stand aside.", "🚨")
            return

        # G4.5 — Canonical volatility overlay gate
        if canonical_vol and canonical_vol.get("label") == "CRISIS" and abs(score) < 6:
            no_trade(f"Vol regime {canonical_vol.get('label')} — directional setups must be exceptional.", "🚨")
            return
        if canonical_vol and canonical_vol.get("label") == "TRANSITION" and abs(score) < 2 and (local_pin or near_max_pain):
            no_trade("Transition volatility regime + pin risk — wait for cleaner expansion.", "⚠️")
            return

        # G5 — Positive GEX + weak score
        if not neg_gex and abs(score) < 5:
            no_trade(f"GEX positive (+${tgex:.1f}M), score only {score:+d}/14 — not enough edge.", "🧲")
            return

        # G5.5 — Pin zone trade card block (v4.3)
        # When GEX+ and price is trapped inside the pin zone (between put wall
        # and call wall), directional debit spreads contradict the thesis.
        # The action guide already says "fade edges, don't chase direction" —
        # this makes the trade card match. Score ≥ 4 can override with size cut.
        _pin_low = (walls or {}).get("put_wall") or (walls or {}).get("pin_zone_low")
        _pin_high = (walls or {}).get("call_wall") or (walls or {}).get("pin_zone_high")
        if not neg_gex and _pin_low and _pin_high and _pin_low < _pin_high:
            if _pin_low <= spot <= _pin_high and abs(score) <= 3:
                no_trade(
                    f"Inside pin zone ${_pin_low:.0f}-${_pin_high:.0f} with GEX+ — "
                    f"debit spreads fight the pin. Score {score:+d}/14 not enough to override.",
                    "📌"
                )
                return

        # ── v4.3: Resolve unified regime if not passed from caller ──
        if unified_regime is None:
            unified_regime = resolve_unified_regime(eng, cagf, spot)

        # G6 — v4.3 REGIME GATE: strategy must fit the environment
        gate_ok, gate_reason = regime_gate(
            regime=unified_regime, bias=bias, cagf=cagf,
            v4_result=v4_result, dte_rec=dte_rec,
        )
        if not gate_ok:
            no_trade(f"Regime gate: {gate_reason}", "🚫")
            return

        # G7 — CAGF direction conflict (v4.2)
        # If CAGF has strong directional signal opposing the bias, flag it
        cagf_conflict = False
        cagf_conflict_note = ""
        if cagf and cagf.get("regime") != "UNKNOWN":
            cagf_prob = cagf.get("probability", 50)
            cagf_dir = cagf.get("direction", "NEUTRAL")
            # Bull bias but CAGF says < 40% upside = conflict
            if is_bull and cagf_prob < 40:
                cagf_conflict = True
                cagf_conflict_note = f"⚠️ CAGF conflict: bias BULL but flow {cagf_prob:.0f}% upside ({cagf_dir})"
            # Bear bias but CAGF says > 60% upside = conflict
            elif not is_bull and cagf_prob > 60:
                cagf_conflict = True
                cagf_conflict_note = f"⚠️ CAGF conflict: bias BEAR but flow {cagf_prob:.0f}% upside ({cagf_dir})"

        # ══════ EM-AWARE WIDTH — ladder aligned with thesis monitor ══════
        ticker_up = ticker.upper()
        step = 5 if ticker_up in ("SPX", "NDX") else 1

        em_1sd = em.get("em_1sd", 0) if em else 0

        # Derive setup_type from what we know at trade card time.
        # Trade cards fire on confirmed bias + passing gates — closest to BREAK.
        # Downgraded to FAILED if bias is weak (mean-reversion scenario).
        gex_sign_str = "negative" if tgex < 0 else "positive"
        tc_setup_type = "BREAK" if abs(score) >= 4 else "FAILED"

        # Nearest opposing level: local_call for bulls, local_put for bears
        local_call = walls.get("call_wall") if walls else None
        local_put  = walls.get("put_wall")  if walls else None
        tc_local_wall = local_call if is_bull else local_put

        width = _app_spread_width(
            setup_type=tc_setup_type,
            direction="LONG" if is_bull else "SHORT",
            spot=spot,
            em_1sd=em_1sd,
            local_wall=tc_local_wall,
            gex_sign=gex_sign_str,
            bias_score=score,
            step=step,
        )

        # Safety floor: never below minimum step
        if width < step:
            width = float(step)

        # ══════ EM-AWARE STRIKES (v4.2 fix) ══════
        bull_1sd = em.get("bull_1sd", spot + 999) if em else spot + 999
        bear_1sd = em.get("bear_1sd", spot - 999) if em else spot - 999

        if is_bull:
            long_strike = (int(spot) // step) * step
            if long_strike >= spot: long_strike -= step
            short_strike = long_strike + width

            # v4.2: Clamp short strike to EM 1σ upper boundary
            em_upper_strike = (int(bull_1sd) // step) * step
            if short_strike > em_upper_strike and em_1sd > 0:
                old_short = short_strike
                short_strike = em_upper_strike
                width = short_strike - long_strike
                if width < step:
                    # EM range is too tight for any spread at this step — use minimum
                    short_strike = long_strike + step
                    width = step
                log.info(f"Trade card: bull short strike clamped ${old_short} → ${short_strike} "
                         f"(EM 1σ upper = ${bull_1sd:.2f})")

            spread_type = "CALL DEBIT SPREAD"
            long_label = f"${long_strike:.0f}C"; short_label = f"${short_strike:.0f}C"
            stop_level = flip if flip else round(spot * (1 - 0.007), 2)
        else:
            long_strike = (int(spot) // step) * step
            if long_strike <= spot: long_strike += step
            short_strike = long_strike - width

            # v4.2: Clamp short strike to EM 1σ lower boundary
            em_lower_strike = int(math.ceil(bear_1sd / step)) * step
            if short_strike < em_lower_strike and em_1sd > 0:
                old_short = short_strike
                short_strike = em_lower_strike
                width = long_strike - short_strike
                if width < step:
                    short_strike = long_strike - step
                    width = step
                log.info(f"Trade card: bear short strike clamped ${old_short} → ${short_strike} "
                         f"(EM 1σ lower = ${bear_1sd:.2f})")

            spread_type = "PUT DEBIT SPREAD"
            long_label = f"${long_strike:.0f}P"; short_label = f"${short_strike:.0f}P"
            stop_level = flip if flip else round(spot * (1 + 0.007), 2)

        actual_width = abs(short_strike - long_strike)
        # v4.3: Width-aware cost estimate. Narrow spreads ($1-2) cost ~60-65% of width.
        # Wider spreads ($3-5) cost less as a % because the short leg offsets more.
        # Old flat 60% overestimated cost on wider spreads, underestimated on narrow ones.
        if actual_width >= 5:
            cost_pct_est = 0.50
        elif actual_width >= 3:
            cost_pct_est = 0.55
        else:
            cost_pct_est = 0.62
        cost_est = round(actual_width * cost_pct_est, 2)
        max_profit = round(actual_width - cost_est, 2)
        rr = round(max_profit / cost_est, 2) if cost_est > 0 else 0

        # v4.3: Theta warning for narrow intraday spreads
        _theta_warn = ""
        if actual_width <= 2 and effective_dte <= 1:
            _theta_warn = (
                f"⚠️ NARROW SPREAD WARNING: ${actual_width:.0f}-wide at {effective_dte} DTE — "
                f"theta dominates. Spread won't reach max value until near expiry. "
                f"Consider wider ($3-$4) or single-leg if high conviction."
            )

        # ══════ SIZE ══════
        if v >= 28: base_pct = 25
        elif v >= 20: base_pct = 50
        elif v >= 15: base_pct = 75
        else: base_pct = 100
        dex_confirms = (is_bull and dex < -0.25) or (not is_bull and dex > 0.25)
        dex_disagrees = (is_bull and dex > 0.25) or (not is_bull and dex < -0.25)
        if dex_confirms: size_pct = min(base_pct + 25, 100); dex_note = "DEX confirms → +1 tier"
        elif dex_disagrees: size_pct = max(base_pct - 25, 25); dex_note = "DEX disagrees → -1 tier"
        else: size_pct = base_pct; dex_note = "DEX neutral"

        # v4.2: Reduce size if CAGF conflicts with bias direction
        if cagf_conflict:
            size_pct = max(int(size_pct * 0.5), 25)
            dex_note += " | CAGF conflict → halved"

        # v4.2: Reduce size in CHOP regime
        _tc_regime_label = (unified_regime.get("label") or "").upper() if unified_regime else ""
        if "CHOP" in _tc_regime_label:
            size_pct = max(int(size_pct * CHOP_REGIME_SIZE_MULT), 25)
            dex_note += f" | CHOP regime → ×{CHOP_REGIME_SIZE_MULT}"

        # ══════ TIMING ══════
        charm_tail = charm_m > 0
        if effective_dte == 0:
            timing_note = "Charm tailwind — hold into 2:30 PM CT." if charm_tail else "Charm headwind — do NOT hold into close."
            timing_warn = "Exit by 2:45 PM CT." if charm_tail else "⚠️ EXIT by noon CT."
            entry_cutoff = "⚠️ No new entries after 2:30 PM CT"
        else:
            # Multi-day trades: context-appropriate charm language
            if is_next_day:
                timing_note = ("Charm supportive — time decay favors upside drift during sessions."
                               if charm_tail else
                               "Charm headwind — time decay adds selling pressure during sessions.")
            else:
                timing_note = ("Charm tailwind — favorable into close."
                               if charm_tail else
                               "Charm headwind — consider early entry.")
            timing_warn = f"Target hold: {effective_dte} trading day{'s' if effective_dte > 1 else ''}."
            entry_cutoff = "📌 Enter at/near open for best fill" if is_next_day else "⚠️ No new entries after 2:30 PM CT"

        gex_note = f"⚡ Negative GEX (-${abs(tgex):.1f}M) — debit spreads confirmed." if neg_gex else f"🧲 Positive GEX (+${tgex:.1f}M) — width tightened."
        regime_label = unified_regime.get("label", "UNKNOWN") if unified_regime else "UNKNOWN"
        regime_desc = unified_regime.get("description", "") if unified_regime else ""

        dir_emoji = {"STRONG BULLISH": "🟢🟢", "BULLISH": "🟢", "SLIGHT BULLISH": "🟡",
                     "SLIGHT BEARISH": "🟠", "BEARISH": "🔴", "STRONG BEARISH": "🔴🔴"}.get(direction, "⚪")

        lines = [
            f"🎯 {ticker} — {card_title}",
            f"Generated: {time_str}  |  Exp: {exp_short}",
            f"Bias model: {dir_emoji} {direction}  [Score: {score:+d}/14]",
        ]

        # ── v4: Confidence line ──
        if v4_result:
            lines.append(format_confidence_header(v4_result))
        if canonical_vol:
            lines.append(f"🌡️ Vol Regime: {_format_canonical_vol_line(canonical_vol)}")
            lines.append(f"🪖 Posture: {canonical_vol.get('posture','')}")

        # ── v4.2: DTE upgrade notice ──
        if dte_was_upgraded:
            lines.append(f"📆 DTE: {effective_dte_label} (reason: {dte_upgrade_reason})")

        lines += [
            "━" * 32, "",
            f"⚙️ REGIME: {regime_label} ({unified_regime.get('source', 'unknown')})",
            f"  {regime_desc}",
            f"  GEX: {'negative' if neg_gex else 'positive'} ({tgex:+.1f}M)",
            "",
        ]

        # ── CAGF Institutional Edge (SPY/QQQ) ──
        if cagf and cagf.get("regime") != "UNKNOWN":
            prob = cagf.get("probability", 50)
            edge_emoji = "🟢" if prob >= 58 else "🔴" if prob <= 42 else "⚪"
            lines.append(f"🏛️ FLOW: {edge_emoji} {cagf['direction']}  ({prob:.0f}% upside prob)")
            lines.append(f"  Trend Day: {cagf['trend_day_probability']:.0%}  |  Vol: {cagf['vol_emoji']} {cagf['vol_label']}  |  {cagf['strategy_emoji']} {cagf['strategy']}")

        # ── DTE Recommendation ──
        if dte_rec and dte_rec.get("primary"):
            p = dte_rec["primary"]
            lines.append(f"📆 DTE: {p['emoji']} {p['label']} recommended  (score {p['score']})")
            if p.get("reasoning"):
                lines.append(f"  {p['reasoning']}")
            avoid_list = dte_rec.get("avoid", [])
            if avoid_list:
                lines.append(f"  ❌ {avoid_list[0]}")

        # ── v4.2: CAGF conflict warning ──
        if cagf_conflict:
            lines.append(f"  {cagf_conflict_note}")

        # ── EM zone check for short strike ──
        if em_1sd > 0:
            if is_bull:
                em_distance = bull_1sd - short_strike
                em_zone = "inside" if short_strike <= bull_1sd else "outside"
            else:
                em_distance = short_strike - bear_1sd
                em_zone = "inside" if short_strike >= bear_1sd else "outside"
            em_zone_emoji = "✅" if em_zone == "inside" else "⚠️"
            em_zone_note = f"  {em_zone_emoji} Short strike {em_zone} EM 1σ (${em_distance:+.2f} from boundary)"
        else:
            em_zone_note = ""

        lines += [
            "",
            f"📋 SETUP: ITM {spread_type}",
            f"  Buy:   {ticker} {long_label}", f"  Sell:  {ticker} {short_label}",
            f"  Width: ${actual_width:.2f}  |  Est. cost: ~${cost_est:.2f}/contract",
            f"  Max profit: ~${max_profit:.2f}/contract  |  R/R: ~{rr:.1f}:1",
        ]
        if em_zone_note:
            lines.append(em_zone_note)
        if _theta_warn:
            lines.append(f"  {_theta_warn}")

        lines += [
            "",
            f"📍 LEVELS",
            f"  Entry zone:  ${spot:.2f} ± 0.3%",
            f"  Hard stop:   ${stop_level:.2f}", "",
            f"⏱️ TIMING", f"  {timing_note}", f"  {timing_warn}",
            f"  {entry_cutoff}", "",
            f"📊 SIZE",
            f"  VIX {v} → base: {base_pct}%  |  {dex_note}",
            f"  → FINAL: {size_pct}% of normal position size",
            "━" * 32, "— Not financial advice —",
        ]

        log.info(f"Trade setup built: {ticker} | {direction} | {effective_dte_label} | "
                 f"{spread_type} {long_label}/{short_label} | "
                 f"width=${actual_width} (EM 1σ=${em_1sd:.2f}) | "
                 f"size={size_pct}% | dte_upgraded={dte_was_upgraded} | "
                 f"regime={unified_regime.get('label', '?') if unified_regime else '?'} | "
                 f"conf={v4_result.get('confidence', {}).get('label', '?') if v4_result else 'N/A'}")

        # ── v4.3: Post a single decision-first card and cache it for /tradecard ──
        try:
            dc = format_decision_card(
                ticker=ticker, spot=spot, em=em, bias=bias,
                eng=eng, regime=unified_regime or {},
                cagf=cagf, dte_rec=dte_rec, v4_result=v4_result,
                spread_type=spread_type, long_label=long_label,
                short_label=short_label, stop_level=stop_level,
                effective_dte_label=effective_dte_label, size_pct=size_pct,
                walls=walls or {}, expiry_label=expiration, est_cost=cost_est,
            )
            decision_rec = {
                "ticker": ticker,
                "spot": spot,
                "posture": canonical_vol.get("posture") if canonical_vol else None,
                "structure_overlay_score": 0 if local_pin or near_max_pain else None,
                "structure_local_support": local_struct.get("local_support_1") if isinstance(local_struct, dict) else None,
                "structure_local_resistance": local_struct.get("local_resistance_1") if isinstance(local_struct, dict) else None,
                "structure_balance_zone_low": local_struct.get("local_balance_zone_low") if isinstance(local_struct, dict) else None,
                "structure_balance_zone_high": local_struct.get("local_balance_zone_high") if isinstance(local_struct, dict) else None,
                "structure_outer_bracket_low": local_struct.get("outer_bracket_low") if isinstance(local_struct, dict) else None,
                "structure_outer_bracket_high": local_struct.get("outer_bracket_high") if isinstance(local_struct, dict) else None,
                "structure_confluence": local_struct.get("structure_confluence") if isinstance(local_struct, dict) else None,
            }
            decision_snapshot = _um_build_shared_model_snapshot(
                ticker=ticker,
                spot=spot,
                dealer_regime=unified_regime,
                vol_regime=canonical_vol,
                structure_ctx={"ticker": ticker, "spot": spot, "price_structure": local_struct},
                rec=decision_rec,
                eng=eng,
                cagf=cagf,
                walls=walls,
                mode="scalp",
                horizon_label=effective_dte_label,
            )
            decision_lines = _um_format_shared_snapshot_lines(decision_snapshot)
            final_dc = dc + ("\n\n" + "\n".join(decision_lines) if decision_lines else "")
            post_to_telegram(final_dc)
            try:
                cache_key = f"tradecard:{ticker.upper()}"
                store_set(cache_key, final_dc, ttl=DIGEST_CARD_CACHE_TTL_SEC)
            except Exception:
                pass
        except Exception as e:
            log.warning(f"Decision card failed for {ticker}: {e}")

    except Exception as e:
        log.error(f"Trade card error for {ticker}: {e}", exc_info=True)


def _append_shared_regime_lines(lines: list, canonical_vol: dict = None, unified_regime: dict = None, management_note: str = "", structure_ctx: dict = None, rec: dict = None, eng: dict = None, cagf: dict = None, v4_flow: dict = None, walls: dict = None, mode: str = "scalp", horizon_label: str = None):
    snap = _um_build_shared_model_snapshot(
        ticker=(rec or {}).get("ticker") or (structure_ctx or {}).get("ticker") or "",
        spot=float((rec or {}).get("spot") or (structure_ctx or {}).get("spot") or 0.0),
        dealer_regime=unified_regime,
        vol_regime=canonical_vol,
        structure_ctx=structure_ctx,
        rec=rec,
        eng=eng,
        cagf=cagf,
        v4_flow=v4_flow,
        walls=walls,
        mode=mode,
        horizon_label=horizon_label,
    )
    lines.extend(_um_format_shared_snapshot_lines(snap))
    if management_note:
        lines.append(f"🛠️ Management focus: {management_note}")
    return lines


def _summarize_swing_reject_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        return "No valid swing spread found"
    lines = [ln.strip() for ln in reason.splitlines() if ln.strip()]
    if not lines:
        return reason[:180]

    head = lines[0]
    detail = lines[1] if len(lines) > 1 else ""
    combined = f"{head} — {detail}".strip(" —")
    lc = combined.lower()

    # Manual /checkswing often has no TradingView-style signal context, so a raw
    # 0/100 confidence read is misleading. Call that out explicitly.
    if "confidence 0/100 below" in lc:
        return "No scoreable swing candidate survived manual-thesis scoring"
    if "confidence" in lc and "/100 below" in lc:
        return combined[:220]
    if "win prob" in lc and "below" in lc:
        return combined[:220]
    if "slippage" in lc or "negative ev" in lc or "fair value" in lc:
        return combined[:220]
    if "no valid spreads" in lc:
        return combined[:220]
    if "not enough" in lc:
        return combined[:220]
    return combined[:220]


def _swing_reject_rank(reason: str) -> int:
    r = (reason or "").lower()
    if "confidence 0/100 below" in r:
        return 1
    if "confidence" in r or "win prob" in r:
        return 4
    if "slippage" in r or "negative ev" in r or "fair value" in r:
        return 5
    if "no valid spreads" in r:
        return 3
    if "not enough" in r or "no expirations" in r:
        return 2
    return 0


def _post_checkswing_card(ticker: str, forced_direction: str = None):
    try:
        from swing_engine import recommend_swing_trade, format_swing_card
        ticker = ticker.upper()
        spot = get_spot(ticker)
        chains = get_options_chain_swing(ticker)
        if not chains:
            post_to_telegram(f"❌ {ticker} — NO SWING SETUP\nReason: no options chain data in 7-60 DTE range\n\n— Not financial advice —")
            return
        candles = get_daily_candles(ticker, days=252)
        raw_rows = _get_daily_ohlcv_rows(ticker, days=90)
        structure_ctx = _build_canonical_structure_context(ticker, spot, raw_rows)
        iv_rank = _estimate_iv_rank(chains, candles)
        canonical_vol = get_canonical_vol_regime(ticker, candle_closes=candles)
        current_regime = get_current_regime()
        v4_flow = _run_v4_prefilter(ticker, spot, chains, candles)
        directions = [forced_direction] if forced_direction else ["bull", "bear"]
        valid = []
        rejects = []
        for direction in directions:
            wd = _compute_manual_swing_signal_context(ticker, spot, raw_rows, direction, structure_ctx)
            wd.update({"type": "swing", "source": "check"})
            rec = recommend_swing_trade(ticker=ticker, spot=spot, chains=chains, webhook_data=wd, iv_rank=iv_rank)
            rec = _apply_canonical_vol_overlay_to_rec(rec, canonical_vol, mode="swing")
            rec = _apply_canonical_structure_overlay_to_rec(rec, structure_ctx, mode="swing") if rec.get("ok") else rec
            if rec.get("ok"):
                valid.append(rec)
            else:
                rejects.append((direction, rec.get("reason", "no valid setup")))
        if not valid:
            checked = forced_direction.upper() if forced_direction else "BOTH"
            parts = [f"❌ {ticker} — NO SWING SETUP", f"🧪 Checked: {checked} | Spot: ${spot:.2f}"]

            ranked_rejects = sorted(rejects, key=lambda dr: (_swing_reject_rank(dr[1]), -len(dr[1] or "")), reverse=True)
            if ranked_rejects:
                best_dir, best_reason = ranked_rejects[0]
                best_emoji = "🐂" if best_dir == "bull" else "🐻"
                parts.append(f"🔎 Closest fail: {best_emoji} {best_dir.upper()} — {_summarize_swing_reject_reason(best_reason)}")

            if len(rejects) > 1:
                parts.append("")
                parts.append("📋 Side-by-side")
                for direction, reason in rejects:
                    emoji = "🐂" if direction == "bull" else "🐻"
                    parts.append(f"• {emoji} {direction.upper()}: {_summarize_swing_reject_reason(reason)}")

            if all(rejects) and all("confidence 0/100 below" in (r or "").lower() for _, r in rejects):
                parts.append("")
                parts.append("ℹ️ Note: manual swing checks do not have full alert-context scoring, so these were unscored / unconvincing rather than true 0-confidence setups.")

            shared_fail_snapshot = _um_build_shared_model_snapshot(
                ticker=ticker,
                spot=spot,
                vol_regime=canonical_vol,
                structure_ctx=structure_ctx,
                rec={},
                v4_flow=v4_flow,
                mode="swing",
            )
            fail_lines = _um_format_shared_snapshot_lines(shared_fail_snapshot)
            if fail_lines:
                parts.append("")
                parts.extend(fail_lines)

            parts.append("")
            parts.append("— Not financial advice —")
            post_to_telegram("\n".join(parts))
            try:
                _log_signal_dataset_event(
                    ticker=ticker,
                    webhook_data={"type": "swing", "source": "check", "bias": forced_direction or "both", "requested_bias": forced_direction or "both", "tier": "manual", "manual_scoreable": bool(raw_rows and len(raw_rows) >= 25)},
                    outcome="rejected_no_setup",
                    reason=" | ".join(f"{d}:{_summarize_swing_reject_reason(r)}" for d, r in rejects)[:300],
                    regime=current_regime,
                    v4_flow=v4_flow,
                    spot=spot,
                    expirations_checked=len(chains),
                    vol_regime=canonical_vol,
                )
            except Exception:
                pass
            return

        valid.sort(key=lambda r: ((r.get("confidence") or 0), ((r.get("trade") or {}).get("expected_value") or 0), ((r.get("trade") or {}).get("ror") or 0)), reverse=True)

        # v5.1 fix: try each valid rec against the regime gate.
        # If the best one gets blocked, try the next. If all blocked,
        # combine regime rejections with spread rejections for full picture.
        approved_rec = None
        regime_rejects = []
        for rec in valid:
            expiration = rec.get("exp")
            dte = rec.get("dte")
            try:
                result_tuple = _get_chain_iv_for_expiry(ticker, expiration, dte)
                eng, walls = result_tuple[3], result_tuple[4]
            except Exception:
                eng, walls = {}, {}
            unified_regime = resolve_unified_regime(eng or {}, None, spot)
            rec["shared_model_snapshot"] = _um_build_shared_model_snapshot(
                ticker=ticker, spot=spot, vol_regime=canonical_vol,
                structure_ctx=structure_ctx, rec=rec, eng=eng, walls=walls, mode="swing",
            )
            eff_allowed, eff_reason, rec = _um_apply_effective_regime_gate_to_rec(
                rec, rec.get("shared_model_snapshot"), mode="swing", has_confirmed_trigger=False,
            )
            rec["shared_model_snapshot"] = _um_build_shared_model_snapshot(
                ticker=ticker, spot=spot, vol_regime=canonical_vol,
                structure_ctx=structure_ctx, rec=rec, eng=eng, walls=walls, mode="swing",
            )
            if eff_allowed:
                approved_rec = rec
                break
            else:
                regime_rejects.append((
                    rec.get("direction", "?"),
                    eff_reason or "Effective regime blocks fresh swing entry here.",
                ))

        if not approved_rec:
            # All valid recs were regime-gated. Combine with spread rejects for full picture.
            all_rejects = rejects + regime_rejects
            checked = forced_direction.upper() if forced_direction else "BOTH"
            parts = [f"❌ {ticker} — NO SWING SETUP", f"🧪 Checked: {checked} | Spot: ${spot:.2f}"]

            ranked_rejects = sorted(all_rejects, key=lambda dr: (_swing_reject_rank(dr[1]), -len(dr[1] or "")), reverse=True)
            if ranked_rejects:
                best_dir, best_reason = ranked_rejects[0]
                best_emoji = "🐂" if best_dir == "bull" else "🐻"
                parts.append(f"🔎 Closest fail: {best_emoji} {best_dir.upper()} — {_summarize_swing_reject_reason(best_reason)}")

            if len(all_rejects) > 1:
                parts.append("")
                parts.append("📋 Side-by-side")
                for direction, reason in all_rejects:
                    emoji = "🐂" if direction == "bull" else "🐻"
                    parts.append(f"• {emoji} {direction.upper()}: {_summarize_swing_reject_reason(reason)}")

            # Add snapshot from last evaluated rec
            last_rec = valid[-1] if valid else {}
            shared_snapshot = last_rec.get("shared_model_snapshot")
            if shared_snapshot:
                fail_lines = _um_format_shared_snapshot_lines(shared_snapshot)
                if fail_lines:
                    parts.append("")
                    parts.extend(fail_lines)

            parts.append("")
            parts.append("— Not financial advice —")
            post_to_telegram("\n".join([p for p in parts if p is not None]))
            try:
                _log_signal_dataset_event(
                    ticker=ticker,
                    webhook_data={"type": "swing", "source": "check", "bias": forced_direction or "both", "requested_bias": forced_direction or "both", "tier": "manual", "manual_scoreable": bool(raw_rows and len(raw_rows) >= 25)},
                    outcome="rejected_effective_regime",
                    reason=" | ".join(f"{d}:{_summarize_swing_reject_reason(r)}" for d, r in all_rejects)[:300],
                    best_rec=last_rec,
                    spot=spot,
                    expirations_checked=len(chains),
                    vol_regime=canonical_vol,
                )
            except Exception:
                pass
            return

        rec = approved_rec
        card = format_swing_card(rec)
        extras = []
        _append_shared_regime_lines(extras, canonical_vol, unified_regime, structure_ctx=structure_ctx, rec=rec, eng=eng, walls=walls, mode="swing")
        final_card = card + ("\n\n" + "\n".join(extras) if extras else "")
        post_to_telegram(final_card)
        try:
            _log_signal_dataset_event(
                ticker=ticker,
                webhook_data={"type": "swing", "source": "check", "bias": rec.get("direction"), "requested_bias": forced_direction or rec.get("direction"), "tier": rec.get("tier", "manual"), "manual_scoreable": rec.get("scoreable")},
                outcome="trade_opened",
                reason="",
                best_rec=rec,
                spot=spot,
                expirations_checked=len(chains),
                vol_regime=canonical_vol,
            )
        except Exception:
            pass
    except Exception as e:
        log.error(f"/checkswing {ticker}: {e}", exc_info=True)
        post_to_telegram(f"⚠️ Swing check failed for {ticker}: {type(e).__name__}")


# ─────────────────────────────────────────────────────────────────────
# _post_monitor_card — v4 INTEGRATED
# ─────────────────────────────────────────────────────────────────────

def _post_monitor_card(ticker: str, mode: str):
    try:
        ticker = ticker.upper()
        if mode == "long":
            expiry, dte = _find_expiry_closest_to_21(ticker)
            mode_label = "Swing Outlook (~21-day thesis / buffered DTE)"
            mode_emoji = "📅"
        else:
            expiry, dte = _find_expiry_nearest(ticker)
            mode_label = "Near-Term Outlook"
            mode_emoji = "⚡"

        if not expiry:
            post_to_telegram(f"⚠️ {ticker}: no valid expiration for /monitor{mode}")
            return

        liquid = _is_liquid(ticker)
        candles = get_daily_candles(ticker, days=252)
        canonical_vol = get_canonical_vol_regime(ticker, candle_closes=candles)
        result_tuple = _get_chain_iv_for_expiry(ticker, expiry, dte)
        iv, spot, expiration = result_tuple[0], result_tuple[1], result_tuple[2]
        eng, walls, skew, pcr, vix = result_tuple[3], result_tuple[4], result_tuple[5], result_tuple[6], result_tuple[7]
        v4_result = result_tuple[8] if len(result_tuple) > 8 else {}
        v4_flow = _extract_v4_flow_snapshot(v4_result, spot)

        if not liquid:
            eng = {}
        cagf = _compute_cagf_snapshot(
            ticker=ticker,
            spot=spot,
            iv=iv,
            eng=eng,
            vix=vix,
            v4_result=v4_result,
            candle_closes=candles,
        )
        unified_regime = resolve_unified_regime(eng or {}, cagf, spot) if spot else {}
        structure_ctx = _build_canonical_structure_context(ticker, spot, _get_daily_ohlcv_rows(ticker, days=90)) if spot else {}

        if iv is None or spot is None:
            post_to_telegram(f"⚠️ {ticker}: could not fetch IV for {expiry} (DTE={dte})")
            return

        thesis_days = 21 if mode == "long" else max(dte, 1)
        em = _calc_intraday_em(spot, iv, thesis_days * 6.5)
        if not em:
            return
        bias = _calc_bias(spot, em or {}, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        iv_pct = iv * 100
        if iv_pct < 15:
            iv_emoji, iv_note = "🟢", "Low IV"
        elif iv_pct < 30:
            iv_emoji, iv_note = "🟡", "Moderate IV"
        elif iv_pct < 50:
            iv_emoji, iv_note = "🔴", "Elevated IV"
        else:
            iv_emoji, iv_note = "🚨", "Extreme IV"

        conf_label = v4_result.get("confidence", {}).get("label", "?") if v4_result else "?"
        conf_score = v4_result.get("confidence", {}).get("composite", 0) if v4_result else 0
        dg = v4_result.get("downgrades", []) if v4_result else []
        downgrade_note = ""
        if dg:
            downgrade_note = " | ⚠ " + "; ".join(d.split(":")[0] for d in dg[:2])

        direction = bias.get("direction", "NEUTRAL")
        simple_dir = "Bullish" if "BULL" in direction else "Bearish" if "BEAR" in direction else "Neutral"
        verdict = bias.get("verdict", "Monitoring only.")

        tgex = eng.get("gex", 0) if eng else 0
        dex = eng.get("dex", 0) if eng else 0
        vanna_m = eng.get("vanna", 0) if eng else 0
        charm_m = eng.get("charm", 0) if eng else 0
        flip = eng.get("flip_price") if eng else None
        call_wall = walls.get("call_wall") if walls else None
        put_wall = walls.get("put_wall") if walls else None
        gamma_wall = walls.get("gamma_wall") if walls else None

        reason_lines = []
        counter_lines = []
        dist_flip_pct = None
        if flip is not None and spot:
            dist_flip_pct = abs((flip - spot) / spot) * 100

        if simple_dir == "Bullish":
            if dex < -0.25:
                reason_lines.append("Dealers are short delta, so rallies can attract mechanical buying.")
            if charm_m > 0:
                reason_lines.append("Charm is supportive, which can help upside drift as time passes.")
            if put_wall is not None and put_wall < spot:
                reason_lines.append(f"Put wall near ${put_wall:.2f} can act as support on pullbacks.")
            if flip is not None:
                if spot >= flip:
                    reason_lines.append(f"Price is above gamma flip ${flip:.2f}; holding above it usually keeps bullish trades cleaner.")
                else:
                    counter_lines.append(f"Price is still below gamma flip ${flip:.2f}, so bullish follow-through needs more confirmation.")
            if dex > 0.25:
                counter_lines.append("Dealers are long delta, so upside can face hedging-related selling.")
            if charm_m < 0:
                counter_lines.append("Charm is a headwind, so time decay may add pressure.")
        elif simple_dir == "Bearish":
            if dex > 0.25:
                reason_lines.append("Dealers are long delta, so rallies can meet hedging-related selling pressure.")
            if charm_m < 0:
                reason_lines.append("Charm is a headwind, so time decay can add downside pressure.")
            if call_wall is not None and call_wall > spot:
                reason_lines.append(f"Call wall near ${call_wall:.2f} can act as resistance on bounces.")
            if flip is not None:
                if spot <= flip:
                    reason_lines.append(f"Price is below gamma flip ${flip:.2f}; staying below it usually keeps bearish trades cleaner.")
                else:
                    counter_lines.append(f"Price is above gamma flip ${flip:.2f}, so bearish follow-through needs more confirmation.")
            if dex < -0.25:
                counter_lines.append("Dealers are short delta, so sharp rallies can attract mechanical buying.")
            if charm_m > 0:
                counter_lines.append("Charm is supportive, which can help upside drift if price stabilizes.")
        else:
            if flip is not None:
                if spot >= flip:
                    reason_lines.append(f"Price is above gamma flip ${flip:.2f}; holding above it keeps upside conditions steadier.")
                else:
                    reason_lines.append(f"Price is below gamma flip ${flip:.2f}; staying below it can make moves more unstable.")
            if abs(dex) > 0.25:
                reason_lines.append("Dealer delta positioning is strong enough to matter on sharp moves.")
            if abs(charm_m) > 0:
                reason_lines.append("Charm flow is active, so time decay can still influence direction.")

        if not reason_lines:
            reason_lines.append(verdict)

        if flip is None:
            flip_line = "Gamma flip unavailable for this chain."
        else:
            if dist_flip_pct is not None and dist_flip_pct >= 2.5:
                if spot >= flip:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is above it now. It is fairly far from spot, so treat it more like a regime line than a tight trigger. A clean break back below can make bullish holds less reliable."
                else:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is below it now. It is fairly far from spot, so treat it more like a regime line than a tight trigger. A reclaim back above usually makes bearish holds less reliable."
            else:
                if spot >= flip:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is above it now. A break below can weaken bullish holds and increase chop."
                else:
                    flip_line = f"Gamma Flip: ${flip:.2f} — price is below it now. A reclaim above can weaken bearish holds and increase chop."

        lines = [
            f"{mode_emoji} {ticker} — {mode_label}",
            f"🎯 Spot / Exp: ${spot:.2f} | IV: {iv_emoji} {iv_pct:.1f}% | Exp: {expiration} ({dte} DTE)",
            f"📈 Bias: {simple_dir} | 💪 Confidence: {conf_label} ({conf_score:.0%}){downgrade_note}",
            "",
            "🧠 What matters:",
        ]
        for item in reason_lines[:3]:
            lines.append(f"  • {item}")
        if counter_lines:
            lines.append(f"  • Counterpoint: {counter_lines[0]}")

        lines += [
            "",
            f"☢️ Gamma note: {flip_line}",
            "",
            "📐 Expected Move (~21-day thesis):" if mode == "long" else f"📐 Expected Move ({thesis_days}-day view):",
            f"  1σ: ${em['bear_1sd']:.2f} → ${em['bull_1sd']:.2f} (±${em['em_1sd']:.2f})",
            f"  2σ: ${em['bear_2sd']:.2f} → ${em['bull_2sd']:.2f}",
            "",
            f"💡 Bottom line: {verdict}",
            "",
            "📦 Data:",
        ]

        if put_wall is not None:
            lines.append(f"  Put Wall / Support: ${put_wall:.2f}")
        if call_wall is not None:
            lines.append(f"  Call Wall / Resistance: ${call_wall:.2f}")
        if gamma_wall is not None:
            lines.append(f"  Gamma Wall: ${gamma_wall:.2f}")
        if flip is not None:
            lines.append(f"  Gamma Flip: ${flip:.2f}")
        if liquid and eng:
            lines.append(f"  GEX: {tgex:+.1f}M | DEX: {dex:+.1f}M | Vanna: {vanna_m:+.1f}M | Charm: {charm_m:+.1f}M")
        if v4_result:
            vr_line = format_vol_regime_line(v4_result)
            if vr_line:
                lines.append(f"  Vol: {vr_line}")
        lines.append(f"  IV note: {iv_note}")
        lines.append("")
        management_note = (
            "Use this as a wheel/thesis monitor — not an automatic swing entry." if mode == "long"
            else "Use this to decide whether to roll, trim, or close early if price breaks support/resistance."
        )
        _append_shared_regime_lines(
            lines,
            canonical_vol,
            unified_regime,
            management_note,
            structure_ctx=structure_ctx,
            eng=eng,
            cagf=cagf,
            v4_flow=v4_flow,
            walls=walls,
            mode="monitor_long" if mode == "long" else "monitor_short",
        )
        if mode == "long":
            lines += _build_wheel_focus_block(ticker, expiration, spot, em, walls or {})
            lines += ["", "📌 Monitoring / wheel management only — no swing entry.", "— Not financial advice —"]
        else:
            lines += ["", "📌 Monitoring only — manage existing trade / roll risk, not a fresh entry signal.", "— Not financial advice —"]

        post_to_telegram("\n".join(lines))
        log.info(f"Monitor card: {ticker} | mode={mode} | DTE={dte} | lean={bias['direction']} | conf={conf_label}")

    except Exception as e:
        log.error(f"Monitor card error for {ticker} mode={mode}: {e}", exc_info=True)
        post_to_telegram(f"⚠️ Monitor card failed for {ticker}: {type(e).__name__}")


# ─────────────────────────────────────────────────────────────────────
# EM SCHEDULER
# ─────────────────────────────────────────────────────────────────────

def _em_scheduler():
    try:
        import pytz
    except ImportError:
        log.error("pytz not installed — EM scheduler disabled")
        return
    log.info(f"0DTE EM scheduler started — fires at {EM_SCHEDULE_TIMES_CT} CT on weekdays")
    log.info("EM reconciler scheduled at 16:15 CT on weekdays")
    fired_today: set = set()
    while True:
        try:
            ct = pytz.timezone("America/Chicago"); now_ct = datetime.now(ct)
            if now_ct.weekday() >= 5: time.sleep(60); continue
            date_str = now_ct.strftime("%Y-%m-%d")
            for hour, minute in EM_SCHEDULE_TIMES_CT:
                fire_key = (date_str, hour, minute)
                if fire_key in fired_today: continue
                if now_ct.hour == hour and abs(now_ct.minute - minute) <= 1:
                    session = "morning" if hour < 12 else "afternoon"
                    fired_today.add(fire_key)
                    log.info(f"EM scheduler firing: {session}")
                    for ticker in EM_TICKERS:
                        threading.Thread(target=_post_em_card, args=(ticker, session), daemon=True).start()

            # ── Auto-reconciler: 4:15 PM CT (after market close) ──
            recon_key = (date_str, 16, 15)
            if recon_key not in fired_today:
                if now_ct.hour == 16 and abs(now_ct.minute - 15) <= 1:
                    fired_today.add(recon_key)
                    log.info("EM reconciler firing (4:15 PM CT)")
                    threading.Thread(target=_run_reconciler_auto, daemon=True, name="reconciler").start()

            # ── v5.1: Regime transition detector (every 30 min during market hours) ──
            if 8 <= now_ct.hour < 16:
                _regime_key = (date_str, now_ct.hour, now_ct.minute // 30)
                if _regime_key not in fired_today:
                    fired_today.add(_regime_key)
                    try:
                        _vd = _get_vix_data() or {}
                        _vix_val = _vd.get("vix", 0)
                        _vix9d = _vd.get("vix9d", 0)
                        if _vix_val > 0:
                            _gex_data = _vd.get("gex", {}) if isinstance(_vd.get("gex"), dict) else {}
                            _gex_sign = "negative" if _gex_data.get("gex", 0) < 0 else "positive"
                            _term = "inverted" if _vix9d > _vix_val + 0.5 else "contango" if _vix9d < _vix_val - 2 else "flat"
                            _transition = _regime_detector.update(
                                vix=_vix_val,
                                vix9d=_vix9d,
                                gex_sign=_gex_sign,
                                term_structure=_term,
                            )
                            if _transition:
                                log.info(f"Regime transition: {_transition.transition_type}")
                                try:
                                    post_to_telegram(_transition.format_alert())
                                except Exception:
                                    pass
                    except Exception as _re:
                        log.debug(f"Regime detector update error: {_re}")

            # ── v5.1.1: Crisis put position monitor (every 10 min during market hours) ──
            if 8 <= now_ct.hour < 16:
                _cpm_key = (date_str, now_ct.hour, now_ct.minute // 10, "crisis_put_mon")
                if _cpm_key not in fired_today:
                    fired_today.add(_cpm_key)
                    try:
                        threading.Thread(target=_crisis_put_monitor, daemon=True, name="crisis-put-mon").start()
                    except Exception as _cpe:
                        log.debug(f"Crisis put monitor error: {_cpe}")

            # ── v5.1: OI Tracker — morning summary + end-of-day flush ──
            if _oi_tracker:
                # Morning unusual flow post at 9:00 AM CT (before sweep runs)
                # Posts yesterday's closing OI vs prior day — overnight positioning.
                _oi_sum_key = (date_str, "oi_summary")
                if _oi_sum_key not in fired_today and _oi_tracker.should_post_summary():
                    fired_today.add(_oi_sum_key)
                    try:
                        _oi_msg = _oi_tracker.format_unusual_flow()
                        if _oi_msg and "No unusual" not in _oi_msg:
                            post_to_telegram(_oi_msg)
                            log.info("OI tracker: unusual flow summary posted")
                    except Exception as _oe:
                        log.debug(f"OI tracker summary error: {_oe}")

                # End-of-day flush at 4:20 PM CT
                _oi_flush_key = (date_str, "oi_flush")
                if _oi_flush_key not in fired_today:
                    if now_ct.hour == 16 and 18 <= now_ct.minute <= 22:
                        fired_today.add(_oi_flush_key)
                        try:
                            _oi_tracker.flush()
                            log.info("OI tracker: end-of-day flush complete")
                        except Exception as _oe:
                            log.debug(f"OI tracker flush error: {_oe}")

                # Daily OI forward sweep at 9:30 AM CT (market open)
                # Captures overnight institutional positioning on forward expirations.
                # Compares today's opening OI vs yesterday's close snapshot.
                _oi_sweep_key = (date_str, "oi_sweep")
                if _oi_sweep_key not in fired_today and _oi_tracker.should_run_sweep():
                    fired_today.add(_oi_sweep_key)
                    try:
                        from oi_tracker import OI_FORWARD_MIN_DTE, OI_FORWARD_MAX_DTE

                        def _oi_forward_chain_fn(ticker: str, expiration: str):
                            """
                            Fetch a specific forward expiration chain for OI tracking.
                            Uses feed=cached (1 credit total) + strikeLimit=None for
                            SPY/QQQ, strikeLimit=20 for everything else.
                            """
                            try:
                                full_chain_tickers = {"SPY", "QQQ"}
                                t = ticker.strip().upper()
                                limit = None if t in full_chain_tickers else 20
                                data = _cached_md.get_chain(t, expiration,
                                    strike_limit=limit, feed="cached")
                                if isinstance(data, dict) and data.get("s") == "ok" and data.get("optionSymbol"):
                                    return data
                            except Exception as e:
                                log.debug(f"OI chain fetch {ticker} {expiration}: {e}")
                            return None

                        def _oi_expirations_fn(ticker: str):
                            """Return all available expirations for a ticker."""
                            try:
                                return get_expirations(ticker.strip().upper()) or []
                            except Exception:
                                return []

                        def _run_sweep():
                            _oi_tracker.run_daily_sweep(
                                chain_fn=_oi_forward_chain_fn,
                                spot_fn=get_spot,
                                expirations_fn=_oi_expirations_fn,
                            )
                        threading.Thread(target=_run_sweep, daemon=True, name="oi-sweep").start()
                        log.info("OI forward sweep triggered (background thread)")
                    except Exception as _oe:
                        log.debug(f"OI sweep trigger error: {_oe}")

            # ── v6.0: Income scan — daily at 8:15 AM CT ──
            _income_key = (date_str, "income_scan")
            if _income_key not in fired_today and _income_scan_fn:
                if now_ct.hour == 8 and 14 <= now_ct.minute <= 16:
                    fired_today.add(_income_key)
                    log.info("Income scanner firing (8:15 AM CT)")
                    try:
                        _income_scan_fn(TELEGRAM_CHAT_ID)
                    except Exception as _ie:
                        log.debug(f"Income scan error: {_ie}")

            # ── v6.1: Potter Box Scan — 8:15 AM CT + 3:05 PM CT ──
            # MUST run AFTER flow confirmation so campaigns are fresh
            if _potter_scanner:
                # Morning scan (after flow confirmation has written campaigns)
                _pb_am_key = (date_str, "potter_box_am")
                if _pb_am_key not in fired_today:
                    if now_ct.hour == 8 and 17 <= now_ct.minute <= 19:
                        fired_today.add(_pb_am_key)
                        def _run_potter_scan():
                            try:
                                from oi_flow import FLOW_TICKERS
                                from swing_scanner import fetch_daily_bars_yahoo

                                def _pb_ohlcv(ticker):
                                    bars = fetch_daily_bars_yahoo(ticker, days=504)
                                    return bars if bars else None

                                setups = _potter_scanner.scan_all(
                                    tickers=FLOW_TICKERS,
                                    ohlcv_fn=_pb_ohlcv,
                                    chain_fn=lambda t, e: _cached_md.get_chain(
                                        t, e, feed="cached"),
                                    spot_fn=get_spot,
                                    expirations_fn=lambda t: get_expirations(t) or [],
                                )
                                if setups:
                                    summary = _potter_scanner.format_summary(setups)
                                    if summary:
                                        post_to_telegram(summary)
                                    for s in setups:
                                        if s.get("trade") and s.get("flow_direction"):
                                            try:
                                                post_to_telegram(_potter_scanner.format_alert(s))
                                            except Exception:
                                                pass
                            except Exception as _pe:
                                log.warning(f"Potter Box scan error: {_pe}")
                        threading.Thread(target=_run_potter_scan, daemon=True,
                                       name="potter-box-am").start()
                        log.info("Potter Box AM scan triggered (8:18 CT, after flow confirm)")

                # Afternoon scan (separate key so it fires independently)
                _pb_pm_key = (date_str, "potter_box_pm")
                if _pb_pm_key not in fired_today:
                    if now_ct.hour == 15 and 4 <= now_ct.minute <= 6:
                        fired_today.add(_pb_pm_key)
                        def _run_potter_scan_pm():
                            try:
                                from oi_flow import FLOW_TICKERS
                                from swing_scanner import fetch_daily_bars_yahoo

                                def _pb_ohlcv(ticker):
                                    bars = fetch_daily_bars_yahoo(ticker, days=504)
                                    return bars if bars else None

                                setups = _potter_scanner.scan_all(
                                    tickers=FLOW_TICKERS,
                                    ohlcv_fn=_pb_ohlcv,
                                    chain_fn=lambda t, e: _cached_md.get_chain(
                                        t, e, feed="cached"),
                                    spot_fn=get_spot,
                                    expirations_fn=lambda t: get_expirations(t) or [],
                                )
                                if setups:
                                    summary = _potter_scanner.format_summary(setups)
                                    if summary:
                                        post_to_telegram(summary)
                                    for s in setups:
                                        if s.get("trade") and s.get("flow_direction"):
                                            try:
                                                post_to_telegram(_potter_scanner.format_alert(s))
                                            except Exception:
                                                pass
                            except Exception as _pe:
                                log.warning(f"Potter Box PM scan error: {_pe}")
                        threading.Thread(target=_run_potter_scan_pm, daemon=True,
                                       name="potter-box-pm").start()
                        log.info("Potter Box PM scan triggered (3:05 CT)")

            # ── v6.1: Institutional Flow Detection ──
            if _flow_detector and _persistent_state:

                # 8:15 AM CT — Morning OI confirmation (yesterday's volume → today's OI)
                _flow_confirm_key = (date_str, "flow_confirm")
                if _flow_confirm_key not in fired_today:
                    if now_ct.hour == 8 and 14 <= now_ct.minute <= 16:
                        fired_today.add(_flow_confirm_key)
                        def _run_morning_confirm():
                            try:
                                def _flow_chain_fn(ticker, expiration):
                                    try:
                                        data = _cached_md.get_chain(
                                            ticker.upper(), expiration,
                                            strike_limit=None,
                                            feed="cached")  # 1 credit — OI only
                                        if isinstance(data, dict) and data.get("s") == "ok":
                                            return data
                                    except Exception:
                                        pass
                                    return None

                                confirmations = _flow_detector.run_morning_confirmation(
                                    chain_fn=_flow_chain_fn,
                                    spot_fn=get_spot,
                                    expirations_fn=lambda t: get_expirations(t) or [],
                                )
                                if confirmations:
                                    rolls = _flow_detector.detect_rolls(confirmations)
                                    sector = _flow_detector.detect_sector_flow(confirmations)
                                    msg = _flow_detector.format_confirmation_summary(
                                        confirmations, rolls, sector)
                                    if msg:
                                        post_to_telegram(msg)

                                    stalks = _flow_detector.generate_stalk_alerts(confirmations)
                                    for stalk in stalks:
                                        try:
                                            post_to_telegram(_flow_detector.format_stalk_alert(stalk))
                                        except Exception:
                                            pass
                            except Exception as _fe:
                                log.warning(f"Morning flow confirmation error: {_fe}")
                        threading.Thread(target=_run_morning_confirm, daemon=True,
                                       name="flow-confirm").start()
                        log.info("Flow morning confirmation triggered")

                # Forward flow sweeps — 9:15, 11:00, 1:30, 2:45 CT
                _flow_sweep_times = [(9, 15), (11, 0), (13, 30), (14, 45)]
                for _fh, _fm in _flow_sweep_times:
                    _flow_sweep_key = (date_str, f"flow_sweep_{_fh}_{_fm}")
                    if _flow_sweep_key not in fired_today:
                        if now_ct.hour == _fh and abs(now_ct.minute - _fm) <= 1:
                            fired_today.add(_flow_sweep_key)
                            def _run_flow_sweep(_hour=_fh, _minute=_fm):
                                try:
                                    from oi_flow import FLOW_TICKERS
                                    sweep_alerts = []
                                    for ticker in FLOW_TICKERS:
                                        try:
                                            spot = get_spot(ticker)
                                            if not spot or spot <= 0:
                                                continue
                                            exps = get_expirations(ticker) or []
                                            # Forward expirations: 7-60 DTE
                                            from datetime import date as _d
                                            today = _d.today()
                                            fwd_exps = []
                                            for exp in exps:
                                                try:
                                                    exp_dt = datetime.fromisoformat(exp).date()
                                                    dte = (exp_dt - today).days
                                                    if 7 <= dte <= 60:
                                                        fwd_exps.append(exp)
                                                except Exception:
                                                    continue
                                            fwd_exps = fwd_exps[:4]  # max 4 expirations

                                            for exp in fwd_exps:
                                                try:
                                                    data = _cached_md.get_chain(
                                                        ticker, exp, strike_limit=None,
                                                        feed="cached")  # 1 credit total
                                                    if not isinstance(data, dict) or data.get("s") != "ok":
                                                        continue
                                                    alerts = _flow_detector.check_intraday_flow(
                                                        ticker, exp, data, spot)
                                                    sweep_alerts.extend(alerts)
                                                except Exception:
                                                    continue
                                        except Exception:
                                            continue

                                    # Post significant+ alerts — GROUPED BY TICKER
                                    postable_alerts = [fa for fa in sweep_alerts
                                                      if fa.get("should_alert") and
                                                      fa.get("flow_level") in ("significant", "extreme")]
                                    grouped_msgs = _flow_detector.format_grouped_flow_alerts(postable_alerts)
                                    for msg in grouped_msgs:
                                        try:
                                            post_to_telegram(msg)
                                        except Exception:
                                            pass

                                    # Generate trade ideas — post as digest
                                    ideas = _flow_detector.generate_flow_trade_ideas(sweep_alerts)
                                    if ideas:
                                        try:
                                            digest_msgs = _flow_detector.format_flow_ideas_digest(ideas)
                                            for dm in digest_msgs:
                                                post_to_telegram(dm)
                                        except Exception:
                                            pass

                                    # Sector flow
                                    sectors = _flow_detector.detect_sector_flow(sweep_alerts)
                                    for sf in sectors:
                                        try:
                                            post_to_telegram(_flow_detector.format_sector_flow_alert(sf))
                                        except Exception:
                                            pass

                                    # Expiry clustering
                                    try:
                                        from economic_calendar import get_events_in_window
                                        econ = get_events_in_window(dte_days=30)
                                    except Exception:
                                        econ = []
                                    clusters = _flow_detector.detect_expiry_clustering(sweep_alerts, econ)
                                    for cl in clusters:
                                        try:
                                            post_to_telegram(_flow_detector.format_expiry_cluster_alert(cl))
                                        except Exception:
                                            pass

                                    log.info(f"Flow sweep {_hour}:{_minute:02d} complete: "
                                           f"{len(sweep_alerts)} alerts across {len(FLOW_TICKERS)} tickers")
                                except Exception as _fe:
                                    log.warning(f"Flow sweep error: {_fe}")
                            threading.Thread(target=_run_flow_sweep, daemon=True,
                                           name=f"flow-sweep-{_fh}{_fm}").start()
                            log.info(f"Flow sweep triggered ({_fh}:{_fm:02d} CT)")

                # 3:05 PM CT — End-of-day: save volume flags + OI baseline
                _flow_eod_key = (date_str, "flow_eod")
                if _flow_eod_key not in fired_today:
                    if now_ct.hour == 15 and 4 <= now_ct.minute <= 6:
                        fired_today.add(_flow_eod_key)
                        log.info("Flow end-of-day save triggered (3:05 PM CT)")

            fired_today = {k for k in fired_today if k[0] == date_str}
        except Exception as e:
            log.error(f"EM scheduler error: {e}", exc_info=True)
        time.sleep(60)


# ─────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────

_background_lock_fh = None
_background_services_started = False
_scanner = None  # v5.0: active scanner instance
_swing_scanner = None  # v5.1: swing scanner instance
_income_scan_fn = None   # v6.0: income scanner scan callback
_income_score_fn = None  # v6.0: income scanner score callback
_portfolio_greeks = PortfolioGreeks()  # v5.1: portfolio-level Greeks aggregator
_regime_detector = RegimeDetector()    # v5.1: regime transition detector
_background_services_lock = threading.Lock()


def _acquire_background_leader() -> bool:
    """Ensure only one local process starts daemon threads under Gunicorn/Render."""
    global _background_lock_fh
    if _background_lock_fh is not None:
        return True
    lock_path = os.getenv("OMEGA_BG_LOCK_PATH", "/tmp/omega_background.lock").strip() or "/tmp/omega_background.lock"
    try:
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        _background_lock_fh = fh
        return True
    except OSError:
        return False


def _start_background_services_once():
    global _background_services_started, _queue_worker_threads
    global _scanner  # v5.0
    global _swing_scanner  # v5.1
    with _background_services_lock:
        if _background_services_started:
            return True
        if not _acquire_background_leader():
            log.info("Background services not started in this worker (leader lock held elsewhere)")
            return False
        threading.Thread(target=_digest_poster_thread, daemon=True, name="digest-poster").start()
        _queue_worker_threads = _start_workers()
        threading.Thread(target=_em_scheduler, daemon=True, name="em-scheduler").start()
        init_thesis_daemon(
            get_spot_fn=get_spot, intraday_post_fn=post_to_intraday,
            store_get_fn=store_get, store_set_fn=store_set,
            get_bars_fn=lambda ticker, res, count: get_intraday_bars(ticker, res, count),
        )
        # v5.1: Wire portfolio Greeks into thesis monitor
        from thesis_monitor import set_portfolio_greeks
        set_portfolio_greeks(_portfolio_greeks)
        # v6.1: Wire PersistentState for ORB/trade persistence
        if _persistent_state:
            from thesis_monitor import set_persistent_state as _thesis_set_ps
            _thesis_set_ps(_persistent_state)
        # v5.0: Start active scanner
        global _scanner
        if ACTIVE_SCANNER_ENABLED:
            _scanner = ActiveScanner(
                enqueue_fn=_enqueue_signal,
                spot_fn=get_spot,
                candle_fn=get_daily_candles,
                intraday_fn=get_intraday_bars,
                regime_fn=get_current_regime,
                vol_regime_fn=get_canonical_vol_regime,
                shadow_log_fn=_log_shadow_signal,
                flow_boost_fn=(lambda ticker, direction, spot:
                    _flow_detector.get_validator_boost(ticker, direction, spot)
                    if _flow_detector else 0.0),
            )
            _scanner.start()
            log.info(f"Active scanner started: {_scanner.watchlist_size} tickers")
        else:
            log.info("Active scanner disabled by ACTIVE_SCANNER_ENABLED=False")
        # v5.1: Start swing scanner
        if SWING_SCANNER_ENABLED:
            def _get_vix_for_scanner():
                try:
                    vd = _get_vix_data()
                    return vd.get("vix", 20) if vd else 20
                except Exception:
                    return 20
            _swing_scanner = SwingScanner(
                enqueue_fn=_enqueue_signal,
                post_fn=post_to_telegram,
                earnings_fn=lambda t: enrich_ticker(t),
                vix_fn=_get_vix_for_scanner,
            )
            # Inject persistent state for Redis-backed signal cache
            from swing_scanner import set_persistent_state as _swing_set_ps
            from swing_scanner import set_flow_fn as _swing_set_flow
            if _persistent_state:
                _swing_set_ps(_persistent_state)
            if _flow_detector:
                _swing_set_flow(lambda ticker, fib_price, direction:
                    _flow_detector.get_flow_score_for_swing(ticker, fib_price, direction))
            _swing_scanner.start()
            log.info(f"Swing scanner started: {_swing_scanner.status.get('watchlist_size', 0)} tickers "
                     f"({_swing_scanner.status.get('swing_only_tickers', 0)} swing-only)")
        else:
            log.info("Swing scanner disabled by SWING_SCANNER_ENABLED=False")

        # v6.0: Wire income scanner
        global _income_scan_fn, _income_score_fn
        try:
            from market_regime import get_regime_package
            _income_ohlcv = create_ohlcv_wrapper(get_daily_candles, md_get_fn=md_get)

            # Flow scoring callback for income scanner
            def _income_flow_fn(ticker, strike, trade_type, expiry=None):
                if _flow_detector:
                    return _flow_detector.get_flow_score_for_income(
                        ticker, strike, trade_type, expiry)
                return None

            _income_scan_fn, _income_score_fn = create_income_handlers(
                chain_fn=_cached_md.get_chain,
                expirations_fn=get_expirations,
                ohlcv_fn=_income_ohlcv,
                regime_fn=get_regime_package,
                post_fn=post_to_telegram,
                flow_fn=_income_flow_fn,
            )
            log.info("Income scanner wired: /income and /score commands active (3-layer regime + flow)")
        except Exception as e:
            log.error(f"Income scanner wiring failed: {e}")
            _income_scan_fn = None
            _income_score_fn = None

        _background_services_started = True
        log.info("Background services started in leader process")
        return True


def _initialize_app():
    global _oi_cache
    global _oi_tracker
    global _persistent_state
    global _flow_detector
    global _potter_scanner
    with app.app_context():
        portfolio.init_store(store_get, store_set)
        trade_journal.init_store(store_get, store_set)
        init_shared_state(store_get, store_set)
        _oi_cache = OICache(store_get, store_set)
        _oi_tracker = OITracker(store_get, store_set)
        _persistent_state = PersistentState(store_get, store_set, store_scan)
        _flow_detector = FlowDetector(_persistent_state, post_fn=post_to_telegram)
        _potter_scanner = PotterBoxScanner(_persistent_state, flow_detector=_flow_detector,
                                           post_fn=post_to_telegram)
        log.info(f"OI cache + tracker + flow detector + Potter Box initialized (Redis: {_get_redis() is not None})")
        _tg_ws = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        if _tg_ws and BOT_URL and _acquire_background_leader():
            register_webhook(BOT_URL, _tg_ws)
        _start_background_services_once()


# ─────────────────────────────────────────────────────────
# v5.0 ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.route("/scanner", methods=["GET"])
def scanner_status():
    """Active scanner status and watchlist info."""
    if _scanner:
        return jsonify(_scanner.status)
    return jsonify({"running": False, "reason": "Scanner not initialized"})


# ─────────────────────────────────────────────────────────
# v5.1.1: TRADE JOURNAL ENDPOINT — view signals and performance
# ─────────────────────────────────────────────────────────

@app.route("/journal", methods=["GET"])
def journal_view():
    """Query trade journal entries and performance stats.

    Query params:
        type:    signal | open | close  (default: all)
        ticker:  filter by ticker
        days:    lookback days (default: 7)
        outcome: trade_opened | rejected | pending | duplicate
        limit:   max entries (default: 50)
        stats:   if "true", include aggregate stats

    Examples:
        /journal                         — last 50 entries, 7 days
        /journal?type=close&days=30      — closed trades, 30 days
        /journal?ticker=SPY&type=open    — SPY trade opens
        /journal?stats=true              — entries + aggregate stats
        /journal?type=signal&outcome=trade_opened  — signals that became trades
    """
    try:
        entry_type = request.args.get("type")
        ticker = request.args.get("ticker")
        outcome = request.args.get("outcome")
        days = int(request.args.get("days", 7))
        limit = int(request.args.get("limit", 50))
        include_stats = request.args.get("stats", "").lower() == "true"

        from datetime import timedelta
        date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        entries = trade_journal.query_journal(
            entry_type=entry_type,
            ticker=ticker,
            outcome=outcome,
            date_from=date_from,
            limit=limit,
        )

        result = {
            "entries": entries,
            "count": len(entries),
            "filters": {
                "type": entry_type,
                "ticker": ticker,
                "outcome": outcome,
                "days": days,
                "date_from": date_from,
            },
        }

        if include_stats:
            result["stats"] = trade_journal.calc_journal_stats(ticker=ticker)

        # Also include API credit status
        result["api_credits"] = get_api_status()

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/journal/stats", methods=["GET"])
def journal_stats():
    """Aggregate trade performance stats.

    Query params:
        ticker: filter by ticker (optional)

    Returns: signal count, win rate, P&L by tier/confidence/vol edge,
             Greeks attribution breakdown.
    """
    try:
        ticker = request.args.get("ticker")
        stats = trade_journal.calc_journal_stats(ticker=ticker)
        stats["api_credits"] = get_api_status()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fundamentals/<ticker>", methods=["GET"])
def get_ticker_fundamentals(ticker):
    """Get fundamental data + Lynch classification for a ticker."""
    try:
        data = get_fundamentals(ticker.strip().upper())
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/crisis_puts", methods=["GET"])
def crisis_puts_status():
    """View open crisis put positions and their current P&L.
    Returns all open positions with live pricing if market is open."""
    try:
        positions = _crisis_put_get_open_positions()
        results = []
        for pos in positions:
            entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
            days_held = (datetime.now(timezone.utc).date() - entry_date).days
            current_price = _crisis_put_get_current_price(pos)
            entry_price = pos["entry_price"]
            pnl_pct = ((current_price - entry_price) / entry_price * 100) if current_price else None
            pnl_dollars = ((current_price - entry_price) * 100) if current_price else None
            results.append({
                "ticker": pos["ticker"],
                "contract": pos["symbol"],
                "strike": pos["strike"],
                "expiration": pos["expiration"],
                "entry_date": pos["entry_date"],
                "entry_price": entry_price,
                "current_price": round(current_price, 2) if current_price else None,
                "target_price": pos["target_price"],
                "exit_by_date": pos["exit_by_date"],
                "days_held": days_held,
                "pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else None,
                "pnl_dollars": round(pnl_dollars, 0) if pnl_dollars is not None else None,
                "source_type": pos.get("source_type", ""),
                "is_paper": pos.get("is_paper", True),
            })
        from trading_rules import (
            CRISIS_PUT_ENABLED, CRISIS_PUT_AUTO_EXECUTE,
            CRISIS_PUT_WHITELIST, CRISIS_PUT_BLACKLIST, CRISIS_PUT_MAX_POSITIONS,
        )
        return jsonify({
            "enabled": CRISIS_PUT_ENABLED,
            "auto_execute": CRISIS_PUT_AUTO_EXECUTE,
            "mode": "paper" if not CRISIS_PUT_AUTO_EXECUTE else "live",
            "open_positions": len(results),
            "max_positions": CRISIS_PUT_MAX_POSITIONS,
            "positions": results,
            "whitelist_count": len(CRISIS_PUT_WHITELIST),
            "blacklist_count": len(CRISIS_PUT_BLACKLIST),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/screen", methods=["POST"])
def run_fundamental_screen():
    """Trigger nightly fundamental screen for all watchlist tickers."""
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    def _run():
        tickers = HIGH_VOLUME_TICKERS
        log.info(f"Fundamental screen: {len(tickers)} tickers")
        results = batch_fetch_fundamentals(tickers)
        fast_growers = [t for t, d in results.items()
                        if d.get("lynch_category", {}).get("category") == "FAST_GROWER"]
        if fast_growers:
            post_to_telegram(
                f"📊 Nightly Screen: {len(fast_growers)} fast growers detected\n"
                + "\n".join(f"  • {t} (PEG={results[t].get('peg_ratio', '?')})"
                            for t in fast_growers[:10])
            )
        log.info(f"Fundamental screen complete: {len(fast_growers)} fast growers")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "accepted", "tickers": len(HIGH_VOLUME_TICKERS)})


@app.route("/sectors", methods=["GET"])
def sector_rankings():
    """Current sector relative strength rankings."""
    rankings = get_all_sector_rankings()
    return jsonify(rankings)


@app.route("/vix_term", methods=["GET"])
def vix_term():
    """VIX term structure: contango/backwardation, VIX9D, VVIX."""
    ts = get_vix_term_structure()
    return jsonify(ts)


@app.route("/calendar", methods=["GET"])
def economic_calendar_endpoint():
    """US economic events in DTE window. ?dte=5 (default)."""
    dte = int(request.args.get("dte", 5))
    events = get_events_in_window(dte_days=dte)
    return jsonify(events)


@app.route("/swing_scanner", methods=["GET"])
def swing_scanner_status():
    """Swing scanner status and watchlist info."""
    if _swing_scanner:
        return jsonify(_swing_scanner.status)
    return jsonify({"running": False, "reason": "Swing scanner not initialized"})


@app.route("/swing_scan", methods=["POST"])
def trigger_swing_scan():
    """Trigger an immediate swing scan of the full watchlist."""
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    if _swing_scanner:
        def _run():
            signals = _swing_scanner.force_scan()
            log.info(f"Manual swing scan complete: {len(signals)} signals")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "accepted", "watchlist_size": _swing_scanner.status.get("watchlist_size", 0)})
    return jsonify({"status": "error", "reason": "Swing scanner not running"}), 503


@app.route("/income_scan", methods=["POST"])
def trigger_income_scan():
    """Trigger an immediate income scan of the full watchlist."""
    data = request.get_json(force=True, silent=True) or {}
    supplied = (data.get("secret") or request.args.get("secret") or "").strip()
    if SCAN_SECRET and supplied != SCAN_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    ticker = (data.get("ticker") or "").strip().upper() or None
    if _income_scan_fn:
        from income_scanner import INCOME_TICKERS
        _income_scan_fn(TELEGRAM_CHAT_ID, ticker)
        return jsonify({"status": "accepted", "ticker": ticker, "tickers": INCOME_TICKERS})
    return jsonify({"status": "error", "reason": "Income scanner not wired"}), 503


@app.route("/portfolio", methods=["GET"])
def portfolio_status():
    """Portfolio-level Greeks aggregator summary."""
    fmt = request.args.get("format", "json")
    if fmt == "text":
        return _portfolio_greeks.format_summary(), 200, {"Content-Type": "text/plain"}
    return jsonify(_portfolio_greeks.get_summary())


@app.route("/regime", methods=["GET"])
def regime_status():
    """Regime transition detector status."""
    return jsonify(_regime_detector.get_status())


@app.route("/oi", methods=["GET"])
def oi_movers():
    """OI change summary — daily movers."""
    if not _oi_tracker:
        return jsonify({"error": "OI tracker not initialized"}), 503
    fmt = request.args.get("format", "json")
    if fmt == "text":
        return _oi_tracker.format_morning_summary(), 200, {"Content-Type": "text/plain"}
    return jsonify({
        "movers": _oi_tracker.get_daily_movers(),
        "status": _oi_tracker.status,
    })


@app.route("/oi/<ticker>", methods=["GET"])
def oi_ticker_detail(ticker):
    """Detailed OI breakdown for one ticker."""
    if not _oi_tracker:
        return jsonify({"error": "OI tracker not initialized"}), 503
    fmt = request.args.get("format", "json")
    if fmt == "text":
        return _oi_tracker.format_ticker_detail(ticker), 200, {"Content-Type": "text/plain"}
    change = _oi_tracker.get_ticker_change(ticker.upper())
    if change:
        return jsonify(change)
    return jsonify({"ticker": ticker.upper(), "message": "No data available"}), 404


_initialize_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
