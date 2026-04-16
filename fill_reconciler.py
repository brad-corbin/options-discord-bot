# fill_reconciler.py
# ═══════════════════════════════════════════════════════════════════
# Fill Reconciliation & Slippage Calibration
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Compares recommended trade parameters (from trade cards) to actual
# fills. Produces:
#   - Slippage factor calibration (validates the 0.15/0.25/0.35 tiers)
#   - Fill quality report by ticker, time-of-day, spread width
#   - Alerts when slippage consistently exceeds model estimate
#
# Your existing SLIPPAGE_SPREAD_FACTOR values are educated guesses.
# This module lets you calibrate them from real fill data.
#
# How it works:
#   1. On trade post, record recommended_debit + est_slippage + timestamp
#   2. On trade entry (human clicks Fill), user sends one of:
#        /filled TRADE_ID PRICE        — exact match, preferred
#        /filled TICKER PRICE          — falls back to most recent PENDING
#                                         recommendation for that ticker
#   3. Reconciler compares actual fill to recommendation, computes
#      realized slippage, and updates rolling averages per ticker tier
#   4. Weekly report shows per-tier slippage reality vs assumption
# ═══════════════════════════════════════════════════════════════════

import json
import math
import time
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────

# Lookback window for rolling slippage average
SLIPPAGE_LOOKBACK_DAYS = 30
MIN_FILLS_FOR_CALIBRATION = 10

# Ticker tier mapping (mirrors trading_rules.py)
INDEX_TIERS = {"SPY", "QQQ", "IWM", "DIA", "SPX"}
LARGE_CAP_TIERS = {
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA",
    "AMD", "AVGO", "NFLX", "COIN",
}

# Storage keys in PersistentState / Redis
STATE_KEY_PREFIX = "omega:fill_recon:"
STATE_KEY_RECORDS = f"{STATE_KEY_PREFIX}records"


# ─────────────────────────────────────────────────────────
# FILL RECORD STORE
# ─────────────────────────────────────────────────────────

class FillRecordStore:
    """Stores recommended trade → actual fill reconciliation records.

    Backing store: inject any dict-like object (PersistentState, Redis, etc.)
    or use in-memory default.
    """

    def __init__(self, get_fn=None, set_fn=None):
        self._get_fn = get_fn
        self._set_fn = set_fn
        self._memory: List[Dict] = []

    def _load_records(self) -> List[Dict]:
        if self._get_fn:
            try:
                raw = self._get_fn(STATE_KEY_RECORDS)
                if raw:
                    return json.loads(raw)
            except Exception as e:
                log.warning(f"fill_reconciler load error: {e}")
        return list(self._memory)

    def _save_records(self, records: List[Dict]):
        # Cap at 2000 records to prevent unbounded growth
        if len(records) > 2000:
            records = records[-2000:]
        self._memory = records
        if self._set_fn:
            try:
                self._set_fn(STATE_KEY_RECORDS, json.dumps(records))
            except Exception as e:
                log.warning(f"fill_reconciler save error: {e}")

    def record_recommendation(
        self,
        trade_id: str,
        ticker: str,
        direction: str,
        trade_type: str,
        recommended_debit: float,
        est_slippage: float,
        display_debit: float,
        long_strike: float,
        short_strike: Optional[float],
        width: float,
        expiration: str,
        contracts: int,
        confidence: int,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a trade recommendation for later reconciliation."""
        rec = {
            "trade_id": trade_id,
            "ticker": ticker.upper(),
            "direction": direction,
            "trade_type": trade_type,
            "recommended_debit": round(float(recommended_debit), 4),
            "est_slippage": round(float(est_slippage), 4),
            "display_debit": round(float(display_debit), 4),
            "long_strike": float(long_strike),
            "short_strike": float(short_strike) if short_strike is not None else None,
            "width": float(width),
            "expiration": expiration,
            "contracts": int(contracts),
            "confidence": int(confidence),
            "recommended_ts": timestamp or time.time(),
            "actual_fill_price": None,
            "actual_fill_ts": None,
            "realized_slippage": None,
            "status": "PENDING",
        }
        records = self._load_records()
        # Replace any existing PENDING record for the same trade_id
        records = [r for r in records if r.get("trade_id") != trade_id]
        records.append(rec)
        self._save_records(records)
        log.info(f"Fill recon: recorded recommendation {trade_id} {ticker} @ ${recommended_debit:.2f}")

    def record_fill(
        self,
        trade_id: str,
        actual_fill_price: float,
        fill_ts: Optional[float] = None,
    ) -> Dict:
        """Reconcile a trade recommendation against the actual fill.

        Returns the updated record with realized_slippage computed, or
        a dict with status='NOT_FOUND' if no matching recommendation.
        """
        records = self._load_records()
        updated = None
        for r in records:
            if r.get("trade_id") == trade_id and r.get("status") == "PENDING":
                rec_debit = float(r.get("recommended_debit", 0))
                realized = actual_fill_price - rec_debit
                r["actual_fill_price"] = round(float(actual_fill_price), 4)
                r["actual_fill_ts"] = fill_ts or time.time()
                r["realized_slippage"] = round(realized, 4)
                r["status"] = "RECONCILED"
                updated = r
                break

        if updated:
            self._save_records(records)
            log.info(
                f"Fill recon: reconciled {trade_id} @ ${actual_fill_price:.2f} "
                f"(rec ${updated['recommended_debit']:.2f}, slippage ${updated['realized_slippage']:+.2f})"
            )
            return updated
        return {"status": "NOT_FOUND", "trade_id": trade_id}

    def resolve_fill(
        self,
        identifier: str,
        actual_fill_price: float,
        fill_ts: Optional[float] = None,
    ) -> Dict:
        """User-friendly fill reconciliation. Accepts either a trade_id
        or a ticker symbol. If given a ticker, matches the most recent
        PENDING recommendation for that ticker.

        This is what the /filled Telegram command should call.
        Returns the reconciled record or {status: 'NOT_FOUND'}.
        """
        identifier = (identifier or "").strip()
        if not identifier:
            return {"status": "NOT_FOUND", "reason": "empty identifier"}

        # Try exact trade_id match first
        result = self.record_fill(identifier, actual_fill_price, fill_ts)
        if result.get("status") == "RECONCILED":
            return result

        # Fall back to ticker-based lookup: find most recent PENDING for this ticker
        ticker = identifier.upper()
        records = self._load_records()
        pending = [
            r for r in records
            if r.get("ticker") == ticker and r.get("status") == "PENDING"
        ]
        if not pending:
            return {
                "status": "NOT_FOUND",
                "identifier": identifier,
                "reason": (
                    f"No pending recommendation matching trade_id or "
                    f"ticker '{identifier}'"
                ),
            }

        # Use the most recent pending (largest recommended_ts)
        pending.sort(key=lambda r: r.get("recommended_ts", 0), reverse=True)
        matched = pending[0]
        ambiguous = len(pending) > 1
        result = self.record_fill(matched["trade_id"], actual_fill_price, fill_ts)
        if ambiguous:
            result["warning"] = (
                f"{len(pending)} pending recommendations for {ticker} — "
                f"matched most recent; use trade_id to disambiguate"
            )
        return result


    def mark_skipped(self, trade_id: str, reason: str = "") -> None:
        """User didn't take the trade — clean record out of pending."""
        records = self._load_records()
        for r in records:
            if r.get("trade_id") == trade_id:
                r["status"] = "SKIPPED"
                r["skip_reason"] = reason
                r["skip_ts"] = time.time()
                break
        self._save_records(records)

    def get_records(
        self,
        since_ts: Optional[float] = None,
        status: Optional[str] = None,
        ticker: Optional[str] = None,
    ) -> List[Dict]:
        records = self._load_records()
        if since_ts is not None:
            records = [r for r in records if r.get("recommended_ts", 0) >= since_ts]
        if status is not None:
            records = [r for r in records if r.get("status") == status]
        if ticker is not None:
            records = [r for r in records if r.get("ticker") == ticker.upper()]
        return records


# Module-level default instance for convenience
default_store = FillRecordStore()


# ─────────────────────────────────────────────────────────
# SLIPPAGE CALIBRATION
# ─────────────────────────────────────────────────────────

def ticker_tier(ticker: str) -> str:
    t = (ticker or "").upper()
    if t in INDEX_TIERS:
        return "INDEX"
    if t in LARGE_CAP_TIERS:
        return "LARGE_CAP"
    return "DEFAULT"


def compute_tier_slippage_stats(
    reconciled: List[Dict],
) -> Dict[str, Dict]:
    """Compute realized slippage stats per ticker tier.

    Returns {tier: {mean_slippage_dollars, mean_slippage_pct_of_width,
                     implied_factor, count}}
    """
    by_tier: Dict[str, List[Dict]] = defaultdict(list)
    for r in reconciled:
        if r.get("status") != "RECONCILED":
            continue
        tier = ticker_tier(r.get("ticker", ""))
        by_tier[tier].append(r)

    results = {}
    for tier, records in by_tier.items():
        if not records:
            continue
        realized_dollars = [float(r.get("realized_slippage", 0)) for r in records]
        widths = [float(r.get("width", 1)) for r in records]
        rec_debits = [float(r.get("recommended_debit", 0)) for r in records]

        mean_slip = sum(realized_dollars) / len(realized_dollars)
        mean_slip_pct_width = (
            sum(s / w for s, w in zip(realized_dollars, widths) if w > 0)
            / max(len([w for w in widths if w > 0]), 1)
        )

        # Implied slippage factor: if model says slippage = factor × debit × spread_pct,
        # factor ≈ realized_slippage / (debit × avg_spread_pct_estimate).
        # We don't store avg_spread_pct, so use a proxy: factor = realized / debit
        nonzero_pairs = [(s, d) for s, d in zip(realized_dollars, rec_debits) if d > 0]
        if nonzero_pairs:
            implied_factor = sum(s / d for s, d in nonzero_pairs) / len(nonzero_pairs)
        else:
            implied_factor = None

        # Standard deviation for confidence intervals
        n = len(realized_dollars)
        if n >= 2:
            mean = mean_slip
            var = sum((x - mean) ** 2 for x in realized_dollars) / (n - 1)
            std = math.sqrt(var)
        else:
            std = 0

        results[tier] = {
            "count": n,
            "mean_slippage_usd": round(mean_slip, 4),
            "std_slippage_usd": round(std, 4),
            "mean_slippage_pct_of_width": round(mean_slip_pct_width, 4),
            "implied_factor": round(implied_factor, 4) if implied_factor is not None else None,
            "p50_slippage": round(sorted(realized_dollars)[n // 2], 4) if n > 0 else 0,
            "p95_slippage": round(sorted(realized_dollars)[int(n * 0.95)], 4) if n > 0 else 0,
        }
    return results


def suggest_slippage_factors(
    store: Optional[FillRecordStore] = None,
    lookback_days: int = SLIPPAGE_LOOKBACK_DAYS,
) -> Dict[str, Dict]:
    """Analyze reconciled fills and suggest new slippage factor values.

    Returns:
      {tier: {current_factor, suggested_factor, sample_size, confidence}}

    Compare suggested_factor to your hardcoded values in trading_rules.py:
      INDEX_SLIPPAGE_SPREAD_FACTOR     = 0.15
      LARGE_CAP_SLIPPAGE_SPREAD_FACTOR = 0.25
      SLIPPAGE_SPREAD_FACTOR           = 0.35   (default)
    """
    store = store or default_store
    since = time.time() - lookback_days * 86400
    records = store.get_records(since_ts=since, status="RECONCILED")

    if not records:
        return {}

    stats = compute_tier_slippage_stats(records)
    current_factors = {
        "INDEX": 0.15,
        "LARGE_CAP": 0.25,
        "DEFAULT": 0.35,
    }

    suggestions = {}
    for tier, tier_stats in stats.items():
        count = tier_stats["count"]
        current = current_factors.get(tier, 0.35)
        implied = tier_stats["implied_factor"]

        if count < MIN_FILLS_FOR_CALIBRATION:
            suggestions[tier] = {
                "current_factor": current,
                "suggested_factor": current,
                "sample_size": count,
                "confidence": "INSUFFICIENT",
                "message": f"Need {MIN_FILLS_FOR_CALIBRATION - count} more fills for calibration",
            }
            continue

        if implied is None:
            suggestions[tier] = {
                "current_factor": current,
                "suggested_factor": current,
                "sample_size": count,
                "confidence": "LOW",
                "message": "Could not compute implied factor",
            }
            continue

        # Blended suggestion: 60% implied + 40% current, to avoid whipsaw
        suggested = round(0.6 * implied + 0.4 * current, 3)
        suggested = max(0.05, min(0.60, suggested))   # clamp to sane range

        delta_pct = ((suggested - current) / current) * 100 if current > 0 else 0

        confidence = "HIGH" if count >= 30 else "MEDIUM" if count >= 15 else "LOW"

        if abs(delta_pct) < 10:
            message = f"Factor {current:.2f} holds up well in live data ({count} fills)"
        elif delta_pct > 0:
            message = (
                f"Live slippage is {delta_pct:.0f}% HIGHER than model — "
                f"consider raising factor to {suggested:.2f}"
            )
        else:
            message = (
                f"Live slippage is {abs(delta_pct):.0f}% LOWER than model — "
                f"consider lowering factor to {suggested:.2f}"
            )

        suggestions[tier] = {
            "current_factor": current,
            "suggested_factor": suggested,
            "implied_factor": implied,
            "sample_size": count,
            "mean_slippage_usd": tier_stats["mean_slippage_usd"],
            "p95_slippage_usd": tier_stats["p95_slippage"],
            "confidence": confidence,
            "message": message,
        }

    return suggestions


# ─────────────────────────────────────────────────────────
# REPORTS
# ─────────────────────────────────────────────────────────

def generate_slippage_report(
    store: Optional[FillRecordStore] = None,
    lookback_days: int = SLIPPAGE_LOOKBACK_DAYS,
) -> str:
    """Build Telegram-ready slippage calibration report."""
    store = store or default_store
    suggestions = suggest_slippage_factors(store, lookback_days)

    if not suggestions:
        return f"📏 Slippage Calibration\n\nNo reconciled fills in last {lookback_days} days."

    lines = [f"📏 Slippage Calibration ({lookback_days}d lookback)"]
    lines.append("")

    for tier in ("INDEX", "LARGE_CAP", "DEFAULT"):
        if tier not in suggestions:
            lines.append(f"━━━━ {tier} ━━━━")
            lines.append("  No fills recorded")
            lines.append("")
            continue
        s = suggestions[tier]
        lines.append(f"━━━━ {tier} ━━━━")
        lines.append(f"  Current factor:   {s['current_factor']:.3f}")
        lines.append(f"  Suggested:        {s.get('suggested_factor', s['current_factor']):.3f}")
        if 'implied_factor' in s:
            lines.append(f"  Implied (raw):    {s['implied_factor']:.3f}")
        lines.append(f"  Sample size:      {s['sample_size']} fills")
        lines.append(f"  Confidence:       {s['confidence']}")
        if 'mean_slippage_usd' in s:
            lines.append(f"  Mean slippage:    ${s['mean_slippage_usd']:+.2f}/contract")
            lines.append(f"  P95 slippage:     ${s.get('p95_slippage_usd', 0):+.2f}/contract")
        lines.append(f"  → {s['message']}")
        lines.append("")

    return "\n".join(lines)


def generate_fill_quality_report(
    store: Optional[FillRecordStore] = None,
    lookback_days: int = 7,
) -> str:
    """Per-ticker fill quality over recent period."""
    store = store or default_store
    since = time.time() - lookback_days * 86400
    records = store.get_records(since_ts=since, status="RECONCILED")

    if not records:
        return f"📊 Fill Quality\n\nNo reconciled fills in last {lookback_days} days."

    by_ticker: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        by_ticker[r["ticker"]].append(float(r.get("realized_slippage", 0)))

    lines = [f"📊 Fill Quality ({lookback_days}d) — {len(records)} fills reconciled"]
    lines.append("")

    sorted_tickers = sorted(
        by_ticker.items(),
        key=lambda kv: sum(kv[1]) / len(kv[1]),
    )
    for ticker, slips in sorted_tickers:
        mean_slip = sum(slips) / len(slips)
        worst = max(slips)
        emoji = "🟢" if mean_slip < 0.03 else "🟡" if mean_slip < 0.08 else "🔴"
        lines.append(
            f"  {emoji} {ticker}: {len(slips)} fills, "
            f"mean ${mean_slip:+.2f}, worst ${worst:+.2f}"
        )

    return "\n".join(lines)
