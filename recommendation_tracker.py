# recommendation_tracker.py
# ═══════════════════════════════════════════════════════════════════
# RECOMMENDATION EFFECTIVENESS TRACKER — the real Phase 1
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Answers the question: "Are the bot's recommendations winning?"
#
# Every recommendation the bot posts — from any source (scalp, swing,
# income, conviction flow, active scanner) — is recorded at POST TIME
# with its exact contract structure. The tracker then monitors the
# recommended contract's price through the holding window and grades
# WIN / LOSS / SCRATCH based on the actual OPTION P&L against the
# recommendation's own intended exit logic.
#
# Key differences from the old conviction-plays CSV tracker:
#   1. DEDUPLICATES identical recommendations into "campaigns" so 8
#      re-detections of NVDA $197.5 immediate = 1 play, not 8.
#   2. Grades on OPTION P&L, not underlying %. A LONG CALL that gained
#      +50% is a WIN even if the underlying moved +0.2%.
#   3. Covers ALL trade types (immediate / swing / income / conviction).
#   4. Tracks MFE (max favorable excursion) and MAE so you can see
#      near-misses — "this setup was almost a winner".
#   5. Zero user input required. No /filled, no manual tagging.
#
# Architecture:
#   Source modules call record_recommendation() at post time.
#   A polling loop calls poll_and_update() every N seconds with a
#   price_fn callback. The tracker updates watermarks and grades
#   recommendations when exit conditions hit. Daily report generator
#   produces clean Telegram output.
# ═══════════════════════════════════════════════════════════════════

import time
import json
import hashlib
import logging
from typing import Dict, List, Optional, Any, Tuple, Callable
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

# Campaign windows by trade type. Tune here instead of one hardcoded global.
CAMPAIGN_WINDOW_HOURS_BY_TYPE = {
    "immediate": 1.5,
    "conviction": 2.0,
    "swing": 24.0,
    "income": 24.0,
}

# Optional per-source exit overrides. Kept empty by default, but this makes
# grading policy configurable without changing tracker logic later.
SOURCE_EXIT_OVERRIDES = {
    # Example:
    # ("check_ticker", "immediate"): {"target_pct": 0.40, "scratch_band": 0.08},
}

# Default exit logic per trade type. Source modules can override when recording.
# All percentages are of the option's entry premium (or spread's entry debit).
DEFAULT_EXIT_LOGIC = {
    "immediate": {
        "target_pct":      0.50,   # +50% on option = win
        "stop_pct":       -0.35,   # -35% on option = stop
        "max_hold_hours":  7,      # EOD grading (~6.5 session hours + buffer)
        "scratch_band":    0.10,   # ±10% at timeout = scratch
    },
    "swing": {
        "target_pct":      1.00,   # +100% on option = win
        "stop_pct":       -0.35,
        "max_hold_hours":  14 * 24,
        "scratch_band":    0.15,
    },
    "income": {
        "target_pct":      0.50,   # 50% of max credit captured
        "stop_pct":       -2.00,   # 2x credit = stop
        "max_hold_hours":  14 * 24,
        "scratch_band":    0.10,
    },
    "conviction": {                 # fallback; prefer routing to underlying type
        "target_pct":      0.50,
        "stop_pct":       -0.35,
        "max_hold_hours":  3 * 24,
        "scratch_band":    0.10,
    },
}

KEY_PREFIX = "omega:rec_tracker:"
KEY_CAMPAIGN = f"{KEY_PREFIX}campaign:"       # individual records
KEY_BY_DATE  = f"{KEY_PREFIX}by_date:"        # date -> list of campaign_ids (by ENTRY date)
KEY_GRADED_ON = f"{KEY_PREFIX}graded_on:"     # date -> list of campaign_ids (by GRADING date)

STATUS_ACTIVE = "tracking"
STATUS_GRADED = "graded"

GRADE_WIN     = "win"
GRADE_LOSS    = "loss"
GRADE_SCRATCH = "scratch"
GRADE_OPEN    = "open"


# ═══════════════════════════════════════════════════════════════════
# FINGERPRINTING / DEDUPLICATION
# ═══════════════════════════════════════════════════════════════════

def canonical_fingerprint(
    ticker: str,
    direction: str,
    structure: str,
    legs: List[Dict],
    trade_type: str,
) -> str:
    """Deterministic hash identifying 'same idea'.

    Identical fingerprints within the campaign window are collapsed.
    legs is a list of {right, strike, expiry, action} dicts.
    """
    leg_sig = tuple(sorted([
        (
            str(leg.get("right", "")).lower(),
            round(float(leg.get("strike") or 0), 2),
            str(leg.get("expiry", ""))[:10],
            str(leg.get("action") or "buy").lower(),
        )
        for leg in (legs or [])
    ]))
    payload = (
        f"{ticker.upper()}|{direction.lower()}|{structure}|"
        f"{leg_sig}|{trade_type.lower()}"
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _campaign_window_hours(trade_type: str, source: Optional[str] = None) -> float:
    trade_type = (trade_type or "").lower()
    return float(CAMPAIGN_WINDOW_HOURS_BY_TYPE.get(trade_type, 4.0))


def _window_bucket(ts: float, window_hours: float) -> int:
    return int(ts // max(window_hours * 3600.0, 1.0))


def campaign_id_from_parts(fingerprint: str, ts: float, trade_type: str, source: Optional[str] = None) -> str:
    hours = _campaign_window_hours(trade_type, source)
    return f"{fingerprint}:b{_window_bucket(ts, hours)}"


def _effective_exit_logic(trade_type: str, source: Optional[str] = None, exit_logic: Optional[Dict] = None) -> Dict:
    base = dict(DEFAULT_EXIT_LOGIC.get((trade_type or "").lower(), DEFAULT_EXIT_LOGIC["conviction"]))
    if source:
        base.update(SOURCE_EXIT_OVERRIDES.get((source, (trade_type or "").lower()), {}))
    if exit_logic:
        base.update(exit_logic)
    return base


def _derive_pricing_mode(structure: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    s = (structure or "").lower()
    if s in ("long_call", "long_put"):
        return "long_mark"
    if s in ("bull_call_spread", "bear_put_spread"):
        return "debit_spread_net"
    if s in ("bull_put_spread", "bear_call_spread"):
        return "credit_spread_debit_to_close"
    return "long_mark"


# ═══════════════════════════════════════════════════════════════════
# STORE
# ═══════════════════════════════════════════════════════════════════

class RecommendationStore:
    """Persistent store for tracked recommendations.

    Inject get_fn and set_fn (e.g. from PersistentState). Scan_fn enables
    listing all stored records; if unavailable we fall back to an in-memory
    date index.
    """

    def __init__(
        self,
        get_fn: Optional[Callable] = None,
        set_fn: Optional[Callable] = None,
        scan_fn: Optional[Callable] = None,
    ):
        self._get_fn = get_fn
        self._set_fn = set_fn
        self._scan_fn = scan_fn
        self._memory: Dict[str, str] = {}

    # ── low-level ─────────────────────────────────────────
    def _raw_get(self, key: str) -> Optional[str]:
        if self._get_fn:
            try:
                v = self._get_fn(key)
                if v is not None:
                    return v
            except Exception as e:
                log.warning(f"rec_tracker get error [{key}]: {e}")
        return self._memory.get(key)

    def _raw_set(self, key: str, value: str) -> None:
        self._memory[key] = value
        if self._set_fn:
            try:
                self._set_fn(key, value)
            except Exception as e:
                log.warning(f"rec_tracker set error [{key}]: {e}")

    # ── campaign records ──────────────────────────────────
    def load_campaign(self, campaign_id: str) -> Optional[Dict]:
        raw = self._raw_get(KEY_CAMPAIGN + campaign_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def save_campaign(self, campaign_id: str, record: Dict) -> None:
        self._raw_set(KEY_CAMPAIGN + campaign_id, json.dumps(record, default=str))

    # ── date index ────────────────────────────────────────
    def add_to_date_index(self, date_str: str, campaign_id: str) -> None:
        existing = self._raw_get(KEY_BY_DATE + date_str)
        try:
            ids = set(json.loads(existing)) if existing else set()
        except Exception:
            ids = set()
        ids.add(campaign_id)
        self._raw_set(KEY_BY_DATE + date_str, json.dumps(sorted(ids)))

    def add_to_graded_index(self, date_str: str, campaign_id: str) -> None:
        """Index by the date the campaign was GRADED (separate from entry date)."""
        existing = self._raw_get(KEY_GRADED_ON + date_str)
        try:
            ids = set(json.loads(existing)) if existing else set()
        except Exception:
            ids = set()
        ids.add(campaign_id)
        self._raw_set(KEY_GRADED_ON + date_str, json.dumps(sorted(ids)))

    def list_campaign_ids_graded_on(self, date_str: str) -> List[str]:
        raw = self._raw_get(KEY_GRADED_ON + date_str)
        if not raw:
            return []
        try:
            return list(json.loads(raw))
        except Exception:
            return []

    def list_campaign_ids_for_date(self, date_str: str) -> List[str]:
        raw = self._raw_get(KEY_BY_DATE + date_str)
        if not raw:
            return []
        try:
            return list(json.loads(raw))
        except Exception:
            return []

    # ── bulk listing ──────────────────────────────────────
    def list_all_active(self) -> List[Dict]:
        """All campaigns still in STATUS_ACTIVE."""
        out = []
        seen_ids = set()

        # Iterate recent 30 days of date index — covers typical swing holding window
        today = datetime.now(timezone.utc).date()
        for i in range(30):
            d = (today - timedelta(days=i)).isoformat()
            for cid in self.list_campaign_ids_for_date(d):
                if cid in seen_ids:
                    continue
                rec = self.load_campaign(cid)
                if rec and rec.get("status") == STATUS_ACTIVE:
                    out.append(rec)
                    seen_ids.add(cid)
        return out

    def list_graded_for_date(self, date_str: str) -> List[Dict]:
        out = []
        for cid in self.list_campaign_ids_for_date(date_str):
            rec = self.load_campaign(cid)
            if rec and rec.get("status") == STATUS_GRADED:
                out.append(rec)
        return out

    def list_graded_in_range(self, since_ts: float) -> List[Dict]:
        today = datetime.now(timezone.utc).date()
        out = []
        seen = set()
        days = max(1, int((time.time() - since_ts) / 86400) + 2)
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            for cid in self.list_campaign_ids_for_date(d):
                if cid in seen:
                    continue
                seen.add(cid)
                rec = self.load_campaign(cid)
                if rec and rec.get("status") == STATUS_GRADED:
                    if rec.get("exit_ts", rec.get("entry_ts", 0)) >= since_ts:
                        out.append(rec)
        return out


# ═══════════════════════════════════════════════════════════════════
# RECORD / DEDUPE
# ═══════════════════════════════════════════════════════════════════

def record_recommendation(
    store: RecommendationStore,
    source: str,                  # "check_ticker" / "conviction_flow" / "swing_engine" / "income_scanner" / "active_scanner"
    ticker: str,
    direction: str,               # "bull" / "bear"
    trade_type: str,              # "immediate" / "swing" / "income" / "conviction"
    structure: str,               # "long_call" / "long_put" / "bull_call_spread" / "bear_put_spread" / "bull_put_spread" / "bear_call_spread"
    legs: List[Dict],             # [{"right": "call"/"put", "strike": 500, "expiry": "YYYY-MM-DD", "action": "buy"/"sell"}]
    entry_option_mark: float,     # for long options: option mark at post. For spreads: net debit or credit
    entry_underlying: float,
    confidence: Optional[int] = None,
    regime: Optional[str] = None,
    exit_logic: Optional[Dict] = None,
    extra_metadata: Optional[Dict] = None,
    shadow_signals: Optional[Dict] = None,   # Phase 1b: {skew, vwap, gap, total_delta}
    pricing_mode: Optional[str] = None,      # long_mark / debit_spread_net / credit_spread_debit_to_close
    ts: Optional[float] = None,
) -> Dict:
    """Record a recommendation. Returns dict describing what happened.

    Returns:
        {
            "campaign_id": str,
            "is_new_campaign": bool,
            "duplicate_count": int,
            "record": dict,
        }
    """
    ts = ts or time.time()
    ticker = ticker.upper()
    direction = direction.lower()
    trade_type = trade_type.lower()

    fingerprint = canonical_fingerprint(
        ticker, direction, structure, legs, trade_type
    )
    cid = campaign_id_from_parts(fingerprint, ts, trade_type, source)

    existing = store.load_campaign(cid)
    if existing is not None:
        # Duplicate — bump counter, refresh last_posted_ts
        existing["duplicate_count"] = int(existing.get("duplicate_count", 1)) + 1
        existing["last_posted_ts"] = ts
        existing.setdefault("sources_seen", []).append(source)
        store.save_campaign(cid, existing)
        log.info(
            f"RecTracker: duplicate #{existing['duplicate_count']} of {cid} "
            f"({ticker} {direction} {trade_type})"
        )
        return {
            "campaign_id": cid,
            "is_new_campaign": False,
            "duplicate_count": existing["duplicate_count"],
            "record": existing,
        }

    # New campaign
    effective_exit_logic = _effective_exit_logic(trade_type, source, exit_logic)

    # Absolute deadline for grading — used by max-hold check
    deadline_ts = ts + effective_exit_logic["max_hold_hours"] * 3600

    record = {
        "campaign_id":        cid,
        "fingerprint":        fingerprint,
        "status":             STATUS_ACTIVE,
        "grade":              GRADE_OPEN,

        # Identity
        "ticker":             ticker,
        "direction":          direction,
        "trade_type":         trade_type,
        "structure":          structure,
        "legs":               legs,
        "pricing_mode":       _derive_pricing_mode(structure, pricing_mode),
        "campaign_window_hours": _campaign_window_hours(trade_type, source),

        # Posting metadata
        "first_source":       source,
        "sources_seen":       [source],
        "entry_ts":           ts,
        "entry_iso":          datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "entry_date":         datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "last_posted_ts":     ts,
        "duplicate_count":    1,

        # Entry prices
        "entry_option_mark":     float(entry_option_mark),
        "entry_underlying":      float(entry_underlying),

        # Decision context
        "confidence":         confidence,
        "regime":             regime,
        "exit_logic":         effective_exit_logic,
        "deadline_ts":        deadline_ts,
        "extra":              extra_metadata or {},

        # Tracking state (updated by poll loop)
        "last_option_mark":      float(entry_option_mark),
        "last_underlying":       float(entry_underlying),
        "last_updated_ts":       ts,
        "peak_option_mark":      float(entry_option_mark),
        "peak_ts":               ts,
        "trough_option_mark":    float(entry_option_mark),
        "trough_ts":             ts,
        "mfe_pct":               0.0,
        "mae_pct":               0.0,
        "observations":          0,

        # Grading result (populated when graded)
        "exit_ts":           None,
        "exit_option_mark":  None,
        "exit_underlying":   None,
        "exit_reason":       None,
        "pnl_pct":           None,
        "pnl_per_contract":  None,

        # Phase 1b: shadow signals (computed at post time, not applied to confidence)
        "shadow_signals":    shadow_signals,
    }

    store.save_campaign(cid, record)
    store.add_to_date_index(record["entry_date"], cid)
    log.info(
        f"RecTracker: new campaign {cid} — {ticker} {direction} {trade_type} "
        f"{structure} entry ${entry_option_mark:.2f} from {source}"
    )
    return {
        "campaign_id": cid,
        "is_new_campaign": True,
        "duplicate_count": 1,
        "record": record,
    }


# ═══════════════════════════════════════════════════════════════════
# TRACKING / GRADING
# ═══════════════════════════════════════════════════════════════════

def _compute_pnl_pct(entry_mark: float, current_mark: float) -> float:
    """Option P&L as fraction of entry. Works for longs and credit spreads alike:
       - Long option / debit spread: entry_mark is debit paid. If current > entry, profit.
       - Credit spread: entry_mark is credit received (positive). If current < entry,
         the spread is closable for less than credit = profit. We handle by caller
         passing 'current_mark' as the live net debit to close (so a credit spread
         with $0.20 credit at entry and $0.05 debit to close = +75% profit by signed
         convention: (entry 0.20 - exit 0.05) / 0.20 = 0.75). Callers that track
         credit spreads must pass exit_as_debit_to_close.
    """
    if entry_mark <= 0:
        return 0.0
    return (current_mark - entry_mark) / entry_mark


def _compute_credit_pnl_pct(credit_received: float, current_debit_to_close: float) -> float:
    """Signed P&L for credit strategies."""
    if credit_received <= 0:
        return 0.0
    return (credit_received - current_debit_to_close) / credit_received


def _current_pnl_from_record(rec: Dict) -> float:
    entry = float(rec.get("entry_option_mark") or 0)
    current = float(rec.get("last_option_mark", entry) or 0)
    pricing_mode = rec.get("pricing_mode") or _derive_pricing_mode(rec.get("structure"))
    if pricing_mode == "credit_spread_debit_to_close":
        return _compute_credit_pnl_pct(entry, current)
    return _compute_pnl_pct(entry, current)


def update_tracking(
    store: RecommendationStore,
    campaign_id: str,
    current_option_mark: float,
    current_underlying: float,
    now_ts: Optional[float] = None,
) -> Optional[Dict]:
    """Update peak/trough, check grading conditions. Returns updated record."""
    rec = store.load_campaign(campaign_id)
    if rec is None or rec.get("status") != STATUS_ACTIVE:
        return rec

    now_ts = now_ts or time.time()

    entry_mark = float(rec["entry_option_mark"])
    trade_type = rec["trade_type"]

    # Compute P&L based on stored pricing convention
    pricing_mode = rec.get("pricing_mode") or _derive_pricing_mode(rec.get("structure"))
    is_credit = pricing_mode == "credit_spread_debit_to_close"
    if is_credit:
        pnl_pct = _compute_credit_pnl_pct(entry_mark, current_option_mark)
    else:
        pnl_pct = _compute_pnl_pct(entry_mark, current_option_mark)

    # Update watermarks
    rec["last_option_mark"] = current_option_mark
    rec["last_underlying"] = current_underlying
    rec["last_updated_ts"] = now_ts
    rec["observations"] = int(rec.get("observations", 0)) + 1

    # MFE / MAE in % of entry
    if pnl_pct > rec.get("mfe_pct", 0):
        rec["mfe_pct"] = pnl_pct
        rec["peak_option_mark"] = current_option_mark
        rec["peak_ts"] = now_ts
    if pnl_pct < rec.get("mae_pct", 0):
        rec["mae_pct"] = pnl_pct
        rec["trough_option_mark"] = current_option_mark
        rec["trough_ts"] = now_ts

    # Check grading conditions
    exit_logic = rec["exit_logic"]
    target = float(exit_logic["target_pct"])
    stop = float(exit_logic["stop_pct"])
    deadline = float(rec["deadline_ts"])

    grade = None
    exit_reason = None
    if pnl_pct >= target:
        grade = GRADE_WIN
        exit_reason = "target_hit"
    elif pnl_pct <= stop:
        grade = GRADE_LOSS
        exit_reason = "stop_hit"
    elif now_ts >= deadline:
        scratch_band = float(exit_logic.get("scratch_band", 0.10))
        if pnl_pct > scratch_band:
            grade = GRADE_WIN
            exit_reason = "deadline_profitable"
        elif pnl_pct < -scratch_band:
            grade = GRADE_LOSS
            exit_reason = "deadline_unprofitable"
        else:
            grade = GRADE_SCRATCH
            exit_reason = "deadline_scratch"

    if grade is not None:
        rec["status"] = STATUS_GRADED
        rec["grade"] = grade
        rec["exit_reason"] = exit_reason
        rec["exit_ts"] = now_ts
        rec["exit_date"] = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rec["exit_option_mark"] = current_option_mark
        rec["exit_underlying"] = current_underlying
        rec["pnl_pct"] = round(pnl_pct, 4)
        # Per-contract P&L in dollars: option prices × 100 multiplier
        rec["pnl_per_contract"] = round((current_option_mark - entry_mark) * 100, 2)
        if is_credit:
            # For credit spreads, the $ P&L is (credit_received - debit_to_close) × 100
            rec["pnl_per_contract"] = round((entry_mark - current_option_mark) * 100, 2)
        log.info(
            f"RecTracker: graded {campaign_id} {grade.upper()} "
            f"({exit_reason}) pnl={pnl_pct:+.1%} mfe={rec['mfe_pct']:+.1%}"
        )
        # Register in the graded-date index so the daily report can find it
        store.add_to_graded_index(rec["exit_date"], campaign_id)

    store.save_campaign(campaign_id, rec)
    return rec


def force_grade_expired(
    store: RecommendationStore,
    now_ts: Optional[float] = None,
) -> int:
    """Force-grade any ACTIVE campaigns whose deadline has passed using the
    last observed price. Returns count of records graded.
    """
    now_ts = now_ts or time.time()
    active = store.list_all_active()
    graded_count = 0
    for rec in active:
        if now_ts < rec.get("deadline_ts", 0):
            continue
        # Use last observed price to grade
        last_mark = rec.get("last_option_mark", rec["entry_option_mark"])
        last_underlying = rec.get("last_underlying", rec["entry_underlying"])
        update_tracking(store, rec["campaign_id"], last_mark, last_underlying, now_ts)
        graded_count += 1
    if graded_count:
        log.info(f"RecTracker: force-graded {graded_count} deadline-expired campaigns")
    return graded_count


# ═══════════════════════════════════════════════════════════════════
# POLLING
# ═══════════════════════════════════════════════════════════════════

def poll_and_update(
    store: RecommendationStore,
    price_fn: Callable,       # (ticker, expiry_str, right, strike, structure, legs) -> float or None
    spot_fn: Callable,        # (ticker) -> float or None
    now_ts: Optional[float] = None,
) -> Dict:
    """Iterate all active campaigns, fetch current prices, update state.

    price_fn signature:
        price_fn(ticker, expiry, right, strike, structure, legs)
            -> float (current net option mark) or None if unavailable.
        For spreads, callers are expected to return the NET mark
        (debit for long spreads, debit-to-close for credit spreads).

    Returns summary dict: {polled, updated, graded, failed}.
    """
    now_ts = now_ts or time.time()
    active = store.list_all_active()
    polled = 0
    updated = 0
    graded = 0
    failed = 0

    for rec in active:
        polled += 1
        try:
            underlying = spot_fn(rec["ticker"])
            if underlying is None or underlying <= 0:
                # Use last-known
                underlying = rec.get("last_underlying") or rec["entry_underlying"]

            legs = rec.get("legs") or []
            # For single-leg structures, extract the primary leg
            if rec["structure"] in ("long_call", "long_put"):
                primary = legs[0] if legs else {}
                price = price_fn(
                    rec["ticker"],
                    primary.get("expiry"),
                    primary.get("right"),
                    primary.get("strike"),
                    rec["structure"],
                    legs,
                )
            else:
                # Spreads: delegate entirely to caller via structure + legs
                price = price_fn(
                    rec["ticker"],
                    legs[0].get("expiry") if legs else None,
                    None, None,
                    rec["structure"],
                    legs,
                )

            if price is None or price < 0:
                failed += 1
                continue

            before_status = rec.get("status")
            updated_rec = update_tracking(
                store, rec["campaign_id"], float(price), float(underlying), now_ts
            )
            updated += 1
            if (updated_rec and updated_rec.get("status") == STATUS_GRADED
                and before_status == STATUS_ACTIVE):
                graded += 1
        except Exception as e:
            log.warning(f"Poll failed for {rec.get('campaign_id')}: {e}")
            failed += 1

    # Also force-grade any deadline-expired that we couldn't price
    graded += force_grade_expired(store, now_ts)

    return {"polled": polled, "updated": updated, "graded": graded, "failed": failed}


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM MESSAGE SPLITTER
# ═══════════════════════════════════════════════════════════════════
# Telegram's API hard-limits text messages to 4096 chars. A busy day's
# /recresults can easily exceed this. This helper splits long messages
# at natural section boundaries (preferred: section dividers '━━━━',
# then blank lines, then single newlines) and prefixes each chunk with
# a [1/N] counter so recipients know more is coming. Never breaks mid-line.

TELEGRAM_MAX_CHARS = 4096
TELEGRAM_SAFE_MARGIN = 200   # leave room for chunk headers, emoji double-bytes


def split_for_telegram(text: str, max_chars: int = TELEGRAM_MAX_CHARS - TELEGRAM_SAFE_MARGIN) -> List[str]:
    """Split a long message into Telegram-safe chunks.

    Split priority:
      1. Section dividers ('━━━━' line)
      2. Double newlines (paragraph breaks)
      3. Single newlines (line breaks)
      4. Hard break at max_chars (last resort, never breaks mid-char)

    Always preserves whole lines. Adds [i/N] prefix when > 1 chunk.
    """
    if not text or len(text) <= max_chars:
        return [text] if text else [""]

    chunks: List[str] = []
    remaining = text

    while len(remaining) > max_chars:
        slice_end = max_chars

        # Try section divider first — this is the cleanest split point
        # (e.g., between "BY TRADE TYPE" and "BY SOURCE" sections)
        divider_pos = remaining.rfind("━━━━", 0, slice_end)
        if divider_pos > max_chars // 3:
            # Walk back to the start of that divider's line
            line_start = remaining.rfind("\n", 0, divider_pos)
            split_at = line_start if line_start > 0 else divider_pos
        else:
            # Fall back to blank-line break
            blank_break = remaining.rfind("\n\n", 0, slice_end)
            if blank_break > max_chars // 3:
                split_at = blank_break
            else:
                # Fall back to single newline
                line_break = remaining.rfind("\n", 0, slice_end)
                if line_break > 0:
                    split_at = line_break
                else:
                    # No newlines in range — hard break (rare)
                    split_at = slice_end

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    # Prefix with part numbers if multi-chunk
    if len(chunks) > 1:
        n = len(chunks)
        chunks = [f"[{i+1}/{n}]\n{c}" for i, c in enumerate(chunks)]

    return chunks


def reply_long(reply_fn: Callable, text: str) -> int:
    """Send a possibly-long message via Telegram, splitting if needed.

    reply_fn should be the Telegram-posting callable (typically bound
    to the current chat). Returns the number of chunks sent.
    """
    chunks = split_for_telegram(text)
    for c in chunks:
        try:
            reply_fn(c)
        except Exception as e:
            log.warning(f"Telegram reply chunk failed: {e}")
    return len(chunks)


# ═══════════════════════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════════════════════

def _conf_bucket(c: Optional[int]) -> str:
    if c is None:
        return "unk"
    if c < 60:
        return "<60"
    if c < 70:
        return "60-70"
    if c < 80:
        return "70-80"
    if c < 90:
        return "80-90"
    return "90+"


def _fmt_emoji(grade: str) -> str:
    return {
        GRADE_WIN: "✅",
        GRADE_LOSS: "❌",
        GRADE_SCRATCH: "⚪",
        GRADE_OPEN: "⏳",
    }.get(grade, "❓")


def _fmt_structure(rec: Dict) -> str:
    s = rec["structure"]
    legs = rec.get("legs") or []
    if s in ("long_call", "long_put"):
        right = "CALL" if "call" in s else "PUT"
        strike = legs[0].get("strike", "?") if legs else "?"
        exp = legs[0].get("expiry", "")[:10] if legs else ""
        return f"LONG {right} ${strike} {exp}"
    if s in ("bull_call_spread", "bear_put_spread", "bull_put_spread", "bear_call_spread"):
        strikes = "/".join(str(leg.get("strike", "?")) for leg in legs)
        label = s.replace("_", " ").upper()
        return f"{label} {strikes}"
    return f"{s.upper()}"


def generate_daily_report(
    store: RecommendationStore,
    date_str: Optional[str] = None,
) -> str:
    """Telegram-ready daily report.

    Shows:
      - Recommendations GRADED on date_str (regardless of when they entered).
      - Recommendations POSTED on date_str that are still active.
      - All currently-open positions (useful for Tuesday's report to still
        show Monday's swing trade).

    This means a 14-day swing posted Monday will appear as:
      - Monday's report: entered today, still active
      - Tue-Thu reports: still tracking (under "CURRENTLY OPEN")
      - Friday's report (if graded): under "GRADED TODAY" with MFE/outcome
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Trades graded on this date (regardless of entry date)
    graded_ids = store.list_campaign_ids_graded_on(date_str)
    graded = []
    for cid in graded_ids:
        rec = store.load_campaign(cid)
        if rec and rec.get("status") == STATUS_GRADED:
            graded.append(rec)

    # Trades posted on this date (may be active or already graded)
    posted_today_ids = store.list_campaign_ids_for_date(date_str)
    posted_today = []
    for cid in posted_today_ids:
        rec = store.load_campaign(cid)
        if rec:
            posted_today.append(rec)
    posted_today_active = [r for r in posted_today if r.get("status") == STATUS_ACTIVE]

    # All active positions across all dates (for CURRENTLY OPEN section)
    all_active = store.list_all_active()
    # De-dupe with posted_today_active
    posted_today_ids_set = {r["campaign_id"] for r in posted_today_active}
    open_from_prior_days = [r for r in all_active
                            if r["campaign_id"] not in posted_today_ids_set]

    duplicate_count_posted = sum(
        int(r.get("duplicate_count", 1)) - 1 for r in posted_today
    )

    wins = [r for r in graded if r.get("grade") == GRADE_WIN]
    losses = [r for r in graded if r.get("grade") == GRADE_LOSS]
    scratch = [r for r in graded if r.get("grade") == GRADE_SCRATCH]

    lines = [f"📊 RECOMMENDATION RESULTS — {date_str}"]
    lines.append("━" * 44)

    total_graded = len(graded)
    if total_graded > 0:
        wr = len(wins) / total_graded
        avg_pnl_pct = sum(r.get("pnl_pct", 0) or 0 for r in graded) / total_graded
        net_pnl_per_contract = sum(r.get("pnl_per_contract", 0) or 0 for r in graded)
    else:
        wr = 0
        avg_pnl_pct = 0
        net_pnl_per_contract = 0

    # ── Section 1: Today's activity ──
    lines.append(f"Posted today: {len(posted_today)} ideas"
                 f"  (dedup suppressed: {duplicate_count_posted})")
    lines.append(
        f"Graded today: {total_graded}   "
        f"Open from prior days: {len(open_from_prior_days)}"
    )
    if total_graded > 0:
        lines.append(
            f"Win rate: {wr:.0%}  ({len(wins)}W / {len(losses)}L / {len(scratch)}S)"
        )
        lines.append(
            f"Avg option P&L: {avg_pnl_pct:+.1%}  |  "
            f"Net $/contract: ${net_pnl_per_contract:+,.0f}"
        )
    lines.append("")

    if not graded and not posted_today and not open_from_prior_days:
        lines.append("No activity.")
        return "\n".join(lines)

    # ── By trade type (graded trades only) ──
    if graded:
        lines.append("━━━━ GRADED TODAY — BY TRADE TYPE ━━━━")
        by_type: Dict[str, List[Dict]] = defaultdict(list)
        for r in graded:
            by_type[r["trade_type"]].append(r)
        for tt in ("immediate", "swing", "income", "conviction"):
            recs = by_type.get(tt, [])
            if not recs:
                continue
            w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
            l = sum(1 for r in recs if r["grade"] == GRADE_LOSS)
            s = sum(1 for r in recs if r["grade"] == GRADE_SCRATCH)
            net = sum(r.get("pnl_per_contract", 0) or 0 for r in recs)
            wr_t = w / len(recs) if recs else 0
            lines.append(
                f"  {tt:10s} {len(recs):2d} trades, {w}W/{l}L/{s}S "
                f"(WR {wr_t:.0%}), net ${net:+,.0f}"
            )
        lines.append("")

        # ── By source ──
        lines.append("━━━━ GRADED TODAY — BY SOURCE ━━━━")
        by_source: Dict[str, List[Dict]] = defaultdict(list)
        for r in graded:
            by_source[r.get("first_source", "unknown")].append(r)
        for src, recs in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
            w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
            wr_s = w / len(recs) if recs else 0
            lines.append(
                f"  {src:22s} {len(recs):2d} trades, WR {wr_s:.0%}"
            )
        lines.append("")

        # ── By confidence ──
        lines.append("━━━━ GRADED TODAY — BY CONFIDENCE ━━━━")
        by_conf: Dict[str, List[Dict]] = defaultdict(list)
        for r in graded:
            by_conf[_conf_bucket(r.get("confidence"))].append(r)
        for bucket in ("<60", "60-70", "70-80", "80-90", "90+", "unk"):
            recs = by_conf.get(bucket, [])
            if not recs:
                continue
            w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
            wr_c = w / len(recs) if recs else 0
            lines.append(
                f"  conf {bucket:6s} {len(recs):2d} trades, WR {wr_c:.0%}"
            )
        lines.append("")

        # ── Notable ──
        lines.append("━━━━ NOTABLE (graded today) ━━━━")
        best = max(graded, key=lambda r: r.get("pnl_pct", 0) or 0)
        worst = min(graded, key=lambda r: r.get("pnl_pct", 0) or 0)
        lines.append(
            f"  Best:  {best['ticker']} {_fmt_structure(best)} "
            f"→ {best.get('pnl_pct', 0):+.1%}"
        )
        lines.append(
            f"  Worst: {worst['ticker']} {_fmt_structure(worst)} "
            f"→ {worst.get('pnl_pct', 0):+.1%}"
        )
        for r in graded:
            if r["grade"] == GRADE_LOSS:
                tgt = r["exit_logic"].get("target_pct", 1)
                if r.get("mfe_pct", 0) >= tgt:
                    lines.append(
                        f"  ⚠️  Near-miss: {r['ticker']} {_fmt_structure(r)} "
                        f"peaked {r['mfe_pct']:+.1%} before stopping"
                    )
                    break
        lines.append("")

        # ── Graded detail ──
        lines.append("━━━━ GRADED DETAIL (closed today) ━━━━")
        for r in sorted(graded, key=lambda x: x.get("pnl_pct", 0) or 0, reverse=True):
            emoji = _fmt_emoji(r["grade"])
            dup_note = (f" (×{r['duplicate_count']} fires)"
                        if int(r.get("duplicate_count", 1)) > 1 else "")
            # Show hold time for multi-day trades
            hold_days = 0
            try:
                hold_days = (r.get("exit_ts", 0) - r.get("entry_ts", 0)) / 86400
            except Exception:
                pass
            hold_note = f" [{hold_days:.1f}d hold]" if hold_days >= 1 else ""
            lines.append(
                f"  {emoji} {r['ticker']} {_fmt_structure(r)} ({r['trade_type']})"
                f"{dup_note}{hold_note} "
                f"→ {r.get('pnl_pct', 0):+.1%} "
                f"[MFE {r.get('mfe_pct', 0):+.1%} / MAE {r.get('mae_pct', 0):+.1%}] "
                f"{r.get('exit_reason', '')}"
            )

    # ── Posted today, still active ──
    if posted_today_active:
        lines.append("")
        lines.append("━━━━ POSTED TODAY — STILL TRACKING ━━━━")
        for r in sorted(posted_today_active, key=lambda x: -(x.get("mfe_pct") or 0)):
            dup_note = (f" (×{r['duplicate_count']})"
                        if r.get("duplicate_count", 1) > 1 else "")
            current_pnl = _current_pnl_from_record(r)
            lines.append(
                f"  ⏳ {r['ticker']} {_fmt_structure(r)} ({r['trade_type']})"
                f"{dup_note} → {current_pnl:+.1%} "
                f"[MFE {r.get('mfe_pct', 0):+.1%}]"
            )

    # ── Open positions from prior days ──
    if open_from_prior_days:
        lines.append("")
        lines.append("━━━━ CURRENTLY OPEN (from prior days) ━━━━")
        for r in sorted(open_from_prior_days, key=lambda x: -(x.get("mfe_pct") or 0)):
            dup_note = (f" (×{r['duplicate_count']})"
                        if r.get("duplicate_count", 1) > 1 else "")
            current_pnl = _current_pnl_from_record(r)
            days_held = (time.time() - r.get("entry_ts", time.time())) / 86400
            max_hold_days = r.get("exit_logic", {}).get("max_hold_hours", 24) / 24
            days_remaining = max_hold_days - days_held
            entry_date = r.get("entry_date", "?")
            lines.append(
                f"  ⏳ {r['ticker']} {_fmt_structure(r)} ({r['trade_type']})"
                f"{dup_note}"
            )
            lines.append(
                f"       Entered {entry_date} "
                f"({days_held:.1f}d held, {days_remaining:.1f}d left) "
                f"→ {current_pnl:+.1%} "
                f"[MFE {r.get('mfe_pct', 0):+.1%} "
                f"on {datetime.fromtimestamp(r.get('peak_ts', r.get('entry_ts', 0)), tz=timezone.utc).strftime('%m-%d')}]"
            )

    return "\n".join(lines)


def analyze_shadow_edge_from_campaigns(
    store: RecommendationStore,
    lookback_days: int = 30,
    min_sample_size: int = 5,
) -> Dict:
    """Compute per-signal edge metrics using graded campaigns as ground truth.

    Phase 1b analyzer. Walks all graded campaigns in the lookback window,
    extracts the shadow_signals each was stamped with at post time, and
    correlates the proposed confidence deltas with actual outcomes.

    Returns structure identical to shadow_signals.analyze_shadow_edge() but
    sourced from recommendation_tracker's own graded campaigns rather than
    trade_journal's closed trades.

    Use this instead of shadow_signals.analyze_shadow_edge() once the
    recommendation_tracker is deployed — same metrics, much larger sample.
    """
    from collections import defaultdict

    since_ts = time.time() - lookback_days * 86400
    graded = store.list_graded_in_range(since_ts)
    graded_with_shadow = [g for g in graded if g.get("shadow_signals")]

    result = {
        "window": {
            "lookback_days": lookback_days,
            "graded_total": len(graded),
            "with_shadow_data": len(graded_with_shadow),
        },
        "signals": {},
        "recommendation": "",
    }

    if len(graded_with_shadow) < min_sample_size:
        result["recommendation"] = (
            f"Only {len(graded_with_shadow)} graded recommendations with shadow data. "
            f"Need {min_sample_size}+ for meaningful analysis. Keep collecting."
        )
        return result

    # Build "joined" shape compatible with per-signal analyzer below
    joined = []
    for rec in graded_with_shadow:
        ss = rec.get("shadow_signals") or {}
        joined.append({
            "trade": {
                "pnl_usd": rec.get("pnl_per_contract", 0),
                "confidence": rec.get("confidence"),
                "win": rec.get("grade") == GRADE_WIN,
                "grade": rec.get("grade"),
            },
            "shadow": ss,
        })

    # Analyze each signal
    for signal_name in ("skew", "vwap", "gap"):
        result["signals"][signal_name] = _analyze_single_shadow_signal(
            joined, signal_name
        )

    # Combined delta bucketing
    result["signals"]["combined_delta"] = _analyze_combined_shadow_delta(joined)

    # Recommendation
    result["recommendation"] = _synthesize_shadow_recommendation(result["signals"])
    return result


def _analyze_single_shadow_signal(joined: List[Dict], signal_name: str) -> Dict:
    pos_deltas = []
    neg_deltas = []
    silent = []
    for j in joined:
        sig = j["shadow"].get(signal_name, {}) or {}
        delta = sig.get("delta", 0) or 0
        trade = j["trade"]
        pnl = float(trade.get("pnl_usd") or 0)
        is_win = bool(trade.get("win"))
        entry = {"pnl": pnl, "win": is_win, "delta": delta, "trade": trade}
        if delta > 0:
            pos_deltas.append(entry)
        elif delta < 0:
            neg_deltas.append(entry)
        else:
            silent.append(entry)

    def stats(entries):
        if not entries:
            return {"count": 0, "win_rate": 0, "avg_pnl": 0, "net_pnl": 0, "avg_delta": 0}
        wins = sum(1 for e in entries if e["win"])
        return {
            "count": len(entries),
            "win_rate": round(wins / len(entries), 3),
            "avg_pnl": round(sum(e["pnl"] for e in entries) / len(entries), 2),
            "net_pnl": round(sum(e["pnl"] for e in entries), 2),
            "avg_delta": round(sum(e["delta"] for e in entries) / len(entries), 2),
        }

    pos_stats = stats(pos_deltas)
    neg_stats = stats(neg_deltas)
    silent_stats = stats(silent)

    # Counterfactual: if negative deltas had actually been applied, how many
    # recommendations would have dropped below the 60-confidence gate?
    cf_silenced = []
    for e in neg_deltas:
        conf = e["trade"].get("confidence") or 0
        if conf + e["delta"] < 60:
            cf_silenced.append(e)
    cf_losers = sum(1 for e in cf_silenced if not e["win"])
    cf_precision = round(cf_losers / len(cf_silenced), 3) if cf_silenced else 0
    cf_dollars = round(
        sum(abs(e["pnl"]) for e in cf_silenced if not e["win"])
        - sum(e["pnl"] for e in cf_silenced if e["win"]),
        2,
    )

    return {
        "positive_delta_stats": pos_stats,
        "negative_delta_stats": neg_stats,
        "silent_stats": silent_stats,
        "edge_metric": {
            "pos_wr_minus_neg_wr": round(
                pos_stats["win_rate"] - neg_stats["win_rate"], 3
            ),
            "pos_pnl_minus_neg_pnl_per_trade": round(
                pos_stats["avg_pnl"] - neg_stats["avg_pnl"], 2
            ),
        },
        "counterfactual_silence": {
            "trades_silenced": len(cf_silenced),
            "losers_silenced": cf_losers,
            "precision": cf_precision,
            "dollars_saved": cf_dollars,
        },
    }


def _analyze_combined_shadow_delta(joined: List[Dict]) -> Dict:
    from collections import defaultdict
    buckets = defaultdict(list)
    for j in joined:
        total = j["shadow"].get("total_delta", 0) or 0
        trade = j["trade"]
        pnl = float(trade.get("pnl_usd") or 0)
        is_win = bool(trade.get("win"))
        if total <= -10:
            label = "strong_negative"
        elif total <= -3:
            label = "mild_negative"
        elif total < 3:
            label = "neutral"
        elif total < 10:
            label = "mild_positive"
        else:
            label = "strong_positive"
        buckets[label].append({"pnl": pnl, "win": is_win})

    result = {}
    for label, items in buckets.items():
        wins = sum(1 for i in items if i["win"])
        result[label] = {
            "count": len(items),
            "win_rate": round(wins / len(items), 3),
            "net_pnl": round(sum(i["pnl"] for i in items), 2),
        }
    return result


def _synthesize_shadow_recommendation(signals: Dict) -> str:
    parts = []
    for name in ("skew", "vwap", "gap"):
        sig = signals.get(name, {})
        if not sig:
            continue
        edge = sig.get("edge_metric", {}).get("pos_wr_minus_neg_wr", 0)
        cf = sig.get("counterfactual_silence", {})
        cf_precision = cf.get("precision", 0)
        n_silenced = cf.get("trades_silenced", 0)
        dollars_saved = cf.get("dollars_saved", 0)

        if edge >= 0.15 and cf_precision >= 0.60 and n_silenced >= 5:
            parts.append(
                f"✅ {name.upper()}: GO LIVE. Edge {edge:+.2f} WR diff, "
                f"would have silenced {cf.get('losers_silenced', 0)}/{n_silenced} "
                f"losers, saving ${dollars_saved:,.0f}."
            )
        elif edge >= 0.05:
            parts.append(
                f"🟡 {name.upper()}: PROMISING. Edge {edge:+.2f}. Wait for more data."
            )
        elif edge <= -0.05:
            parts.append(
                f"❌ {name.upper()}: INVERTED ({edge:+.2f}). Do NOT go live — "
                f"signal appears anti-correlated with outcomes."
            )
        else:
            parts.append(
                f"⚪ {name.upper()}: NO EDGE detected ({edge:+.2f})."
            )
    return "\n".join(parts) if parts else "Insufficient data."


def format_shadow_edge_report(analysis: Dict) -> str:
    """Telegram-ready shadow edge report."""
    w = analysis.get("window", {})
    lines = ["🔍 Shadow Signal Edge Analysis"]
    lines.append(f"Window: {w.get('lookback_days', 0)}d | "
                 f"{w.get('with_shadow_data', 0)} of {w.get('graded_total', 0)} "
                 f"graded recs have shadow data")
    lines.append("")

    for name in ("skew", "vwap", "gap"):
        sig = analysis.get("signals", {}).get(name)
        if not sig:
            continue
        lines.append(f"━━━━ {name.upper()} ━━━━")
        pos = sig.get("positive_delta_stats", {})
        neg = sig.get("negative_delta_stats", {})
        sil = sig.get("silent_stats", {})
        edge = sig.get("edge_metric", {})
        cf = sig.get("counterfactual_silence", {})
        lines.append(
            f"  Pos delta: {pos.get('count', 0)}T  WR {pos.get('win_rate', 0):.0%}  "
            f"${pos.get('avg_pnl', 0):+.0f}/trade"
        )
        lines.append(
            f"  Neg delta: {neg.get('count', 0)}T  WR {neg.get('win_rate', 0):.0%}  "
            f"${neg.get('avg_pnl', 0):+.0f}/trade"
        )
        lines.append(
            f"  Silent:    {sil.get('count', 0)}T  WR {sil.get('win_rate', 0):.0%}"
        )
        lines.append(
            f"  Edge: {edge.get('pos_wr_minus_neg_wr', 0):+.2f} WR, "
            f"${edge.get('pos_pnl_minus_neg_pnl_per_trade', 0):+.0f}/trade"
        )
        if cf.get("trades_silenced", 0) > 0:
            lines.append(
                f"  Counterfactual: silence {cf['trades_silenced']} "
                f"({cf['losers_silenced']} losers, "
                f"precision {cf['precision']:.0%}), "
                f"save ${cf['dollars_saved']:+,.0f}"
            )
        lines.append("")

    combined = analysis.get("signals", {}).get("combined_delta", {})
    if combined:
        lines.append("━━━━ COMBINED DELTA ━━━━")
        for label in ("strong_negative", "mild_negative", "neutral",
                      "mild_positive", "strong_positive"):
            b = combined.get(label)
            if not b:
                continue
            lines.append(
                f"  {label:20s} {b['count']:3d}T  WR {b['win_rate']:.0%}  "
                f"net ${b['net_pnl']:+,.0f}"
            )
        lines.append("")

    lines.append("━━━━ RECOMMENDATION ━━━━")
    lines.append(analysis.get("recommendation", "(no recommendation)"))
    return "\n".join(lines)


def generate_weekly_summary(store: RecommendationStore, days: int = 7) -> str:
    """Summary across the last N days, showing overall performance and
    breakdowns by trade type and source."""
    since = time.time() - days * 86400
    graded = store.list_graded_in_range(since)
    if not graded:
        return f"📊 Weekly Recommendation Summary ({days}d)\n\nNo graded recommendations in window."

    wins = [r for r in graded if r["grade"] == GRADE_WIN]
    losses = [r for r in graded if r["grade"] == GRADE_LOSS]

    lines = [f"📊 Weekly Recommendation Summary ({days}d)"]
    lines.append("━" * 44)
    lines.append(f"Graded: {len(graded)}")
    lines.append(
        f"Win rate: {len(wins)/len(graded):.0%}  ({len(wins)}W / {len(losses)}L)"
    )
    net = sum(r.get("pnl_per_contract", 0) or 0 for r in graded)
    lines.append(f"Net $/contract: ${net:+,.0f}")
    lines.append("")

    lines.append("━━━━ BY TRADE TYPE ━━━━")
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for r in graded:
        by_type[r["trade_type"]].append(r)
    for tt, recs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
        net_tt = sum(r.get("pnl_per_contract", 0) or 0 for r in recs)
        lines.append(
            f"  {tt:10s} {len(recs):3d} trades, WR {w/len(recs):.0%}, net ${net_tt:+,.0f}"
        )

    lines.append("")
    lines.append("━━━━ BY SOURCE ━━━━")
    by_source: Dict[str, List[Dict]] = defaultdict(list)
    for r in graded:
        by_source[r.get("first_source", "unknown")].append(r)
    for src, recs in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
        net_s = sum(r.get("pnl_per_contract", 0) or 0 for r in recs)
        lines.append(
            f"  {src:22s} {len(recs):3d} trades, WR {w/len(recs):.0%}, net ${net_s:+,.0f}"
        )

    return "\n".join(lines)


def generate_open_positions_report(store: RecommendationStore) -> str:
    """All currently-tracking positions regardless of entry date.
    Use this for /recopen. Useful for seeing the full swing/income book."""
    active = store.list_all_active()
    if not active:
        return "📊 Currently Open Positions\n\nNo active recommendations."

    lines = [f"📊 Currently Open — {len(active)} positions"]
    lines.append("━" * 44)

    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for r in active:
        by_type[r["trade_type"]].append(r)

    now = time.time()
    for tt in ("immediate", "swing", "income", "conviction"):
        recs = by_type.get(tt, [])
        if not recs:
            continue
        lines.append("")
        lines.append(f"━━━━ {tt.upper()} ({len(recs)}) ━━━━")
        for r in sorted(recs, key=lambda x: -(x.get("mfe_pct") or 0)):
            dup_note = (f" (×{r['duplicate_count']})"
                        if r.get("duplicate_count", 1) > 1 else "")
            days_held = (now - r.get("entry_ts", now)) / 86400
            max_hold_days = r.get("exit_logic", {}).get("max_hold_hours", 24) / 24
            days_remaining = max_hold_days - days_held
            current_pnl = _current_pnl_from_record(r)
            entry_date = r.get("entry_date", "?")
            peak_date = (
                datetime.fromtimestamp(
                    r.get("peak_ts", r.get("entry_ts", 0)),
                    tz=timezone.utc,
                ).strftime("%m-%d")
                if r.get("peak_ts") else "?"
            )
            lines.append(
                f"  {r['ticker']} {_fmt_structure(r)}{dup_note}"
            )
            lines.append(
                f"    Entered {entry_date} "
                f"({days_held:.1f}d held, {days_remaining:.1f}d left) | "
                f"Source: {r.get('first_source', '?')} | "
                f"Conf: {r.get('confidence', '?')}"
            )
            lines.append(
                f"    Entry ${r['entry_option_mark']:.2f}  →  "
                f"Current ${r.get('last_option_mark', r['entry_option_mark']):.2f}  "
                f"({current_pnl:+.1%})"
            )
            lines.append(
                f"    Peak ${r.get('peak_option_mark', r['entry_option_mark']):.2f} "
                f"({r.get('mfe_pct', 0):+.1%}) on {peak_date}  |  "
                f"Trough {r.get('mae_pct', 0):+.1%}"
            )
            tgt = r.get("exit_logic", {}).get("target_pct", 0)
            stop = r.get("exit_logic", {}).get("stop_pct", 0)
            lines.append(
                f"    Target: {tgt:+.0%}  Stop: {stop:+.0%}"
            )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# MARKET HOURS AWARE POLLING
# ═══════════════════════════════════════════════════════════════════

def is_market_hours(now_ts: Optional[float] = None) -> bool:
    """True if currently in regular US equity-options session (8:30-15:00 CT).

    Does not account for holidays — caller should integrate with their
    existing holiday calendar if precision matters. For options tracking,
    polling on holidays is harmless (chains will return stale quotes which
    the tracker handles by leaving MFE/MAE unchanged).
    """
    now_ts = now_ts or time.time()
    ct = datetime.fromtimestamp(now_ts, tz=ZoneInfo("America/Chicago"))

    # Monday=0 .. Sunday=6
    if ct.weekday() >= 5:   # Sat/Sun
        return False
    minutes = ct.hour * 60 + ct.minute
    # Session: 8:30 AM (510) - 3:00 PM (900) CT
    return 510 <= minutes <= 900


def poll_and_update_if_market_open(
    store: RecommendationStore,
    price_fn: Callable,
    spot_fn: Callable,
    now_ts: Optional[float] = None,
) -> Dict:
    """Wrapper around poll_and_update that skips off-hours.

    Off-hours polling produces wide stale quotes that can register bogus
    MFE/MAE watermarks. This helper is the safe default for scheduled jobs
    that run around the clock.

    Note: force_grade_expired() still runs off-hours so immediate trades
    still get their deadline_scratch grading applied after EOD.
    """
    now_ts = now_ts or time.time()

    if not is_market_hours(now_ts):
        # Still check deadlines — immediate trades need EOD grading
        graded = force_grade_expired(store, now_ts)
        return {
            "polled": 0, "updated": 0, "graded": graded, "failed": 0,
            "skipped_reason": "market_closed",
        }

    return poll_and_update(store, price_fn, spot_fn, now_ts)
    since = time.time() - days * 86400
    graded = store.list_graded_in_range(since)
    if not graded:
        return f"📊 Weekly Recommendation Summary ({days}d)\n\nNo graded recommendations in window."

    wins = [r for r in graded if r["grade"] == GRADE_WIN]
    losses = [r for r in graded if r["grade"] == GRADE_LOSS]

    lines = [f"📊 Weekly Recommendation Summary ({days}d)"]
    lines.append("━" * 44)
    lines.append(f"Graded: {len(graded)}")
    lines.append(
        f"Win rate: {len(wins)/len(graded):.0%}  ({len(wins)}W / {len(losses)}L)"
    )
    net = sum(r.get("pnl_per_contract", 0) or 0 for r in graded)
    lines.append(f"Net $/contract: ${net:+,.0f}")
    lines.append("")

    lines.append("━━━━ BY TRADE TYPE ━━━━")
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for r in graded:
        by_type[r["trade_type"]].append(r)
    for tt, recs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
        net_tt = sum(r.get("pnl_per_contract", 0) or 0 for r in recs)
        lines.append(
            f"  {tt:10s} {len(recs):3d} ideas, WR {w/len(recs):.0%}, net ${net_tt:+,.0f}"
        )

    lines.append("")
    lines.append("━━━━ BY SOURCE ━━━━")
    by_source: Dict[str, List[Dict]] = defaultdict(list)
    for r in graded:
        by_source[r.get("first_source", "unknown")].append(r)
    for src, recs in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        w = sum(1 for r in recs if r["grade"] == GRADE_WIN)
        net_s = sum(r.get("pnl_per_contract", 0) or 0 for r in recs)
        lines.append(
            f"  {src:22s} {len(recs):3d} ideas, WR {w/len(recs):.0%}, net ${net_s:+,.0f}"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# INTEGRATION NOTES
# ═══════════════════════════════════════════════════════════════════
#
# WIRING (5 source modules):
#
# 1. app.py check_ticker — after a trade card is posted (scalp path).
#    Call site: right after _fill_store.record_recommendation(...).
#
#    Single-leg (long call/put) from scalp engine:
#        record_recommendation(
#            store=_rec_tracker,
#            source="check_ticker",
#            ticker=ticker, direction=direction,
#            trade_type="immediate" if dte <= 2 else "swing",
#            structure=f"long_{'call' if direction=='bull' else 'put'}",
#            legs=[{
#                "right": "call" if direction == "bull" else "put",
#                "strike": trade.get("long"),
#                "expiry": best_rec.get("exp"),
#                "action": "buy",
#            }],
#            entry_option_mark=trade.get("debit", 0),
#            entry_underlying=spot,
#            confidence=best_rec.get("confidence"),
#            regime=regime.get("label"),
#        )
#
#    Debit spread (if trade has both long and short strikes):
#        legs=[
#            {"right": "call" or "put", "strike": trade["long"],
#             "expiry": best_rec["exp"], "action": "buy"},
#            {"right": "call" or "put", "strike": trade["short"],
#             "expiry": best_rec["exp"], "action": "sell"},
#        ]
#        structure = "bull_call_spread" / "bear_put_spread"
#        entry_option_mark = trade["debit"]   (net)
#
# 2. oi_flow.py — conviction plays. Post-dispatch, after Telegram post:
#        record_recommendation(
#            store=_rec_tracker,
#            source="conviction_flow",
#            ticker=alert["ticker"], direction=direction,
#            trade_type=play["route"],  # "immediate" / "swing" / "income"
#            structure="long_call" if bullish else "long_put",
#            legs=[{"right": "call" if bullish else "put",
#                   "strike": strike, "expiry": expiry, "action": "buy"}],
#            entry_option_mark=current_option_mark,   # pull from chain_fn
#            entry_underlying=spot,
#            extra_metadata={"vol_oi_ratio": vol_oi, "burst": burst},
#        )
#    The duplicate suppression happens automatically — 8 re-fires of the
#    same NVDA idea inside 4 hours = 1 campaign with duplicate_count=8.
#    You can delete or disable _reconcile_conviction_plays() at this point.
#
# 3. swing_engine.py — when posting a swing recommendation:
#        record_recommendation(
#            source="swing_engine", trade_type="swing",
#            structure=appropriate_debit_spread or long option,
#            legs=[...], entry_option_mark=debit, ...
#        )
#
# 4. income_scanner.py — when grade >= C and posting:
#        record_recommendation(
#            source="income_scanner", trade_type="income",
#            structure="bull_put_spread" or "bear_call_spread",
#            legs=[
#                {"right": "put" or "call", "strike": short_strike,
#                 "expiry": expiry, "action": "sell"},
#                {"right": "put" or "call", "strike": long_strike,
#                 "expiry": expiry, "action": "buy"},
#            ],
#            entry_option_mark=credit,   # positive number
#            ...
#        )
#
# 5. active_scanner.py — already flows into check_ticker; do NOT double-
#    record. It'll be captured via (1).
#
# POLLING: wire into your existing scheduled jobs (e.g. schedule.py).
# Every 60-120s during market hours, call:
#
#    from recommendation_tracker import poll_and_update
#    poll_and_update(
#        store=_rec_tracker,
#        price_fn=your_option_mid_fn,   # see below
#        spot_fn=get_spot,
#    )
#
# A reference price_fn using your cached chain:
#
#    def _rec_price_fn(ticker, expiry, right, strike, structure, legs):
#        if structure in ("long_call", "long_put"):
#            side = "call" if "call" in structure else "put"
#            chain = _cached_md.get_chain(ticker, expiry, side=side)
#            for c in chain:
#                if abs(c["strike"] - strike) < 0.01:
#                    bid = c.get("bid", 0) or 0
#                    ask = c.get("ask", 0) or 0
#                    return (bid + ask) / 2 if ask > 0 else None
#            return None
#        # For spreads: fetch both legs, compute net
#        if structure in ("bull_call_spread", "bear_put_spread",
#                         "bull_put_spread", "bear_call_spread"):
#            mids = {}
#            for leg in legs:
#                side = leg["right"]
#                chain = _cached_md.get_chain(ticker, leg["expiry"], side=side)
#                for c in chain:
#                    if abs(c["strike"] - leg["strike"]) < 0.01:
#                        bid = c.get("bid", 0) or 0
#                        ask = c.get("ask", 0) or 0
#                        mids[leg["strike"]] = (bid + ask) / 2 if ask > 0 else None
#                        break
#            # Net: buy-leg mid minus sell-leg mid for debit spreads
#            buy_leg = next((l for l in legs if l["action"] == "buy"), None)
#            sell_leg = next((l for l in legs if l["action"] == "sell"), None)
#            if not buy_leg or not sell_leg:
#                return None
#            buy_mid = mids.get(buy_leg["strike"])
#            sell_mid = mids.get(sell_leg["strike"])
#            if buy_mid is None or sell_mid is None:
#                return None
#            if structure in ("bull_call_spread", "bear_put_spread"):
#                return buy_mid - sell_mid   # debit to close (roughly)
#            else:  # credit spreads — return debit-to-close
#                return buy_mid - sell_mid   # same calc; caller convention
#        return None
#
# TELEGRAM COMMANDS:
#
#    /recresults [YYYY-MM-DD]      → generate_daily_report(date)
#    /recweek                      → generate_weekly_summary(days=7)
#    /recmonth                     → generate_weekly_summary(days=30)
#
# DEPRECATING THE OLD CONVICTION REPORT:
# Once you've verified the new tracker produces results you trust, you
# can:
#   (a) Comment out _reconcile_conviction_plays in app.py, OR
#   (b) Keep it running in parallel for cross-check during validation.
# The new tracker does NOT read from or write to conviction_plays.csv —
# they're fully independent.
