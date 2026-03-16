# em_reconciler.py
# ═══════════════════════════════════════════════════════════════════
# EM Prediction Reconciler — compares predicted ranges against actual
# EOD prices to measure prediction accuracy over time.
#
# Runs after market close (4:15 PM CT recommended) to:
#   1. Scan Redis for unreconciled em_log:* entries
#   2. Fetch actual close prices from MarketData.app
#   3. Score each prediction (inside 1σ? 2σ? direction correct?)
#   4. Update entries with results
#   5. Post accuracy summary to Telegram
#
# Integration:
#   In app.py, add a route and scheduler entry:
#     from em_reconciler import reconcile_em_predictions, format_accuracy_report
#
#   Route:   /reconcile (POST with secret)
#   Schedule: add (16, 15) to the scheduler for auto-run after close
#
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable, Tuple

log = logging.getLogger(__name__)


def reconcile_em_predictions(
    redis_client,
    fetch_eod_close: Callable[[str, str], Optional[float]],
    store_set: Callable,
    lookback_days: int = 5,
) -> List[Dict]:
    """
    Scan Redis for unreconciled EM predictions and fill in actual EOD prices.

    Args:
        redis_client: Redis connection (needs .scan_iter or .keys)
        fetch_eod_close: function(ticker, date_str) → float or None
        store_set: app.py's store_set(key, value, ttl) function
        lookback_days: how many days back to scan (default 5 = full trading week)

    Returns:
        List of reconciled entry dicts with accuracy scores added.
    """
    if not redis_client:
        log.warning("Reconciler: no Redis connection")
        return []

    reconciled = []
    today = datetime.now(timezone.utc).date()

    # Scan for em_log:* keys from the last N days
    for day_offset in range(1, lookback_days + 1):
        target_date = today - timedelta(days=day_offset)
        # Skip weekends
        if target_date.weekday() >= 5:
            continue
        date_str = target_date.strftime("%Y-%m-%d")
        pattern = f"em_log:{date_str}:*"

        try:
            keys = list(redis_client.scan_iter(match=pattern, count=100))
        except Exception as e:
            log.warning(f"Reconciler: scan failed for {pattern}: {e}")
            continue

        for key in keys:
            try:
                raw = redis_client.get(key)
                if raw is None:
                    continue
                entry = json.loads(raw)

                # Skip already reconciled
                if entry.get("reconciled"):
                    reconciled.append(entry)
                    continue

                ticker = entry.get("ticker")
                pred_date = entry.get("date")
                if not ticker or not pred_date:
                    continue

                # Fetch actual EOD close
                eod_price = fetch_eod_close(ticker, pred_date)
                if eod_price is None:
                    log.debug(f"Reconciler: no EOD price for {ticker} on {pred_date}")
                    continue

                # Score the prediction
                entry["eod_price"] = round(eod_price, 2)
                entry["reconciled"] = True
                entry = _score_prediction(entry)

                # Save back to Redis (preserve existing TTL)
                ttl = redis_client.ttl(key)
                if ttl and ttl > 0:
                    store_set(key, json.dumps(entry), ttl=ttl)
                else:
                    store_set(key, json.dumps(entry), ttl=90 * 86400)

                reconciled.append(entry)
                log.info(f"Reconciled: {key} | EOD=${eod_price:.2f} | "
                         f"in_1σ={entry.get('in_1_sigma')} | "
                         f"direction_correct={entry.get('direction_correct')}")

            except Exception as e:
                log.warning(f"Reconciler: error processing {key}: {e}")
                continue

    log.info(f"Reconciler complete: {len(reconciled)} entries processed")
    return reconciled


def _score_prediction(entry: Dict) -> Dict:
    """
    Score a single prediction against the actual EOD price.
    Adds these fields to the entry:
      - in_1_sigma: bool — did EOD close inside predicted 1σ range?
      - in_2_sigma: bool — did EOD close inside predicted 2σ range?
      - direction_correct: bool — did the bias correctly predict direction?
      - move_actual: float — actual $ move from prediction spot
      - move_predicted_1sd: float — predicted 1σ $ move
      - move_ratio: float — actual/predicted (>1 = underestimated, <1 = overestimated)
      - error_sigma: float — how many σ away the actual close was
    """
    spot = entry.get("spot_at_prediction")
    eod = entry.get("eod_price")
    bull_1 = entry.get("bull_1sd")
    bear_1 = entry.get("bear_1sd")
    bull_2 = entry.get("bull_2sd")
    bear_2 = entry.get("bear_2sd")
    em_1sd = entry.get("em_1sd")
    bias_score = entry.get("bias_score", 0)

    if not all(v is not None for v in [spot, eod, bull_1, bear_1, em_1sd]):
        entry["scoring_error"] = "missing fields"
        return entry

    actual_move = eod - spot
    abs_move = abs(actual_move)

    # Range checks
    entry["in_1_sigma"] = (bear_1 <= eod <= bull_1)
    entry["in_2_sigma"] = (bear_2 <= eod <= bull_2) if bear_2 is not None and bull_2 is not None else None

    # Direction check: did bias_score predict the right side?
    if bias_score >= 2:
        # Predicted bull
        entry["direction_correct"] = actual_move > 0
        entry["predicted_direction"] = "bull"
    elif bias_score <= -2:
        # Predicted bear
        entry["direction_correct"] = actual_move < 0
        entry["predicted_direction"] = "bear"
    else:
        # Neutral — no directional prediction made
        entry["direction_correct"] = None
        entry["predicted_direction"] = "neutral"

    # Move magnitude
    entry["move_actual"] = round(actual_move, 2)
    entry["move_predicted_1sd"] = round(em_1sd, 2)
    entry["move_ratio"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None
    entry["error_sigma"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None

    # v4 accuracy (if v4 prediction was present)
    v4_bias = entry.get("v4_bias")
    if v4_bias:
        if v4_bias == "UPSIDE":
            entry["v4_direction_correct"] = actual_move > 0
        elif v4_bias == "DOWNSIDE":
            entry["v4_direction_correct"] = actual_move < 0
        else:
            entry["v4_direction_correct"] = None

    return entry


def compute_accuracy_stats(entries: List[Dict]) -> Dict:
    """
    Aggregate accuracy statistics from a set of reconciled entries.
    Returns a stats dict with hit rates and distribution info.
    """
    reconciled = [e for e in entries if e.get("reconciled") and "in_1_sigma" in e]
    if not reconciled:
        return {"n": 0, "error": "no reconciled entries"}

    n = len(reconciled)
    in_1 = sum(1 for e in reconciled if e.get("in_1_sigma"))
    in_2 = sum(1 for e in reconciled if e.get("in_2_sigma"))

    # Direction accuracy (only for entries where a direction was predicted)
    dir_entries = [e for e in reconciled if e.get("direction_correct") is not None]
    dir_correct = sum(1 for e in dir_entries if e["direction_correct"])

    # v4 direction accuracy
    v4_entries = [e for e in reconciled if e.get("v4_direction_correct") is not None]
    v4_correct = sum(1 for e in v4_entries if e["v4_direction_correct"])

    # Move ratio distribution
    ratios = [e["move_ratio"] for e in reconciled if e.get("move_ratio") is not None]
    avg_ratio = sum(ratios) / len(ratios) if ratios else None
    # % of times actual move exceeded predicted 1σ
    exceeded_1sd = sum(1 for r in ratios if r > 1.0)

    # By confidence level
    by_conf = {}
    for label in ("HIGH", "MODERATE", "LOW"):
        subset = [e for e in reconciled if e.get("v4_confidence") == label]
        if subset:
            s_in_1 = sum(1 for e in subset if e.get("in_1_sigma"))
            s_dir = [e for e in subset if e.get("direction_correct") is not None]
            s_dir_ok = sum(1 for e in s_dir if e["direction_correct"])
            by_conf[label] = {
                "n": len(subset),
                "in_1_sigma_pct": round(s_in_1 / len(subset) * 100, 1),
                "direction_pct": round(s_dir_ok / len(s_dir) * 100, 1) if s_dir else None,
            }

    # By ticker
    by_ticker = {}
    tickers = set(e.get("ticker", "?") for e in reconciled)
    for t in tickers:
        subset = [e for e in reconciled if e.get("ticker") == t]
        t_in_1 = sum(1 for e in subset if e.get("in_1_sigma"))
        by_ticker[t] = {
            "n": len(subset),
            "in_1_sigma_pct": round(t_in_1 / len(subset) * 100, 1),
        }

    return {
        "n": n,
        "in_1_sigma_pct": round(in_1 / n * 100, 1),
        "in_2_sigma_pct": round(in_2 / n * 100, 1),
        "expected_1_sigma_pct": 68.3,
        "expected_2_sigma_pct": 95.4,
        "direction_n": len(dir_entries),
        "direction_correct_pct": round(dir_correct / len(dir_entries) * 100, 1) if dir_entries else None,
        "v4_direction_n": len(v4_entries),
        "v4_direction_correct_pct": round(v4_correct / len(v4_entries) * 100, 1) if v4_entries else None,
        "avg_move_ratio": round(avg_ratio, 3) if avg_ratio else None,
        "exceeded_1sd_pct": round(exceeded_1sd / len(ratios) * 100, 1) if ratios else None,
        "by_confidence": by_conf,
        "by_ticker": by_ticker,
    }


def format_accuracy_report(stats: Dict) -> str:
    """
    Format accuracy stats into a Telegram-friendly message.
    """
    if stats.get("n", 0) == 0:
        return "📊 EM Accuracy: No reconciled predictions yet."

    n = stats["n"]
    lines = [
        f"📊 EM PREDICTION ACCURACY — {n} predictions",
        "═" * 32,
        "",
        "── Range Accuracy ──",
        f"  1σ hit rate:  {stats['in_1_sigma_pct']:.1f}%  (expected: {stats['expected_1_sigma_pct']}%)",
        f"  2σ hit rate:  {stats['in_2_sigma_pct']:.1f}%  (expected: {stats['expected_2_sigma_pct']}%)",
    ]

    # Interpretation
    diff_1 = stats["in_1_sigma_pct"] - stats["expected_1_sigma_pct"]
    if abs(diff_1) < 5:
        lines.append("  ✅ 1σ range well-calibrated")
    elif diff_1 > 0:
        lines.append(f"  ⚠️ 1σ range too wide by ~{diff_1:.0f}% (IV overestimates moves)")
    else:
        lines.append(f"  ⚠️ 1σ range too narrow by ~{abs(diff_1):.0f}% (IV underestimates moves)")

    # Move ratio
    if stats.get("avg_move_ratio") is not None:
        lines += [
            "",
            "── Move Size ──",
            f"  Avg actual/predicted: {stats['avg_move_ratio']:.2f}x",
            f"  Exceeded 1σ: {stats['exceeded_1sd_pct']:.1f}% of the time",
        ]

    # Direction accuracy
    if stats.get("direction_n", 0) > 0:
        lines += [
            "",
            "── Direction Accuracy (bias score ≥2) ──",
            f"  Bias score: {stats['direction_correct_pct']:.1f}% correct  ({stats['direction_n']} predictions)",
        ]
    if stats.get("v4_direction_n", 0) > 0:
        lines.append(
            f"  v4 engine:  {stats['v4_direction_correct_pct']:.1f}% correct  ({stats['v4_direction_n']} predictions)"
        )

    # By confidence
    if stats.get("by_confidence"):
        lines += ["", "── By v4 Confidence ──"]
        for label in ("HIGH", "MODERATE", "LOW"):
            if label in stats["by_confidence"]:
                c = stats["by_confidence"][label]
                dir_str = f", dir {c['direction_pct']:.0f}%" if c.get("direction_pct") is not None else ""
                lines.append(f"  {label}: {c['n']} cards, 1σ {c['in_1_sigma_pct']:.0f}%{dir_str}")

    # By ticker
    if stats.get("by_ticker"):
        lines += ["", "── By Ticker ──"]
        for t, s in sorted(stats["by_ticker"].items()):
            lines.append(f"  {t}: {s['n']} cards, 1σ {s['in_1_sigma_pct']:.0f}%")

    lines += ["", "— Not financial advice —"]
    return "\n".join(lines)


def fetch_eod_close_marketdata(ticker: str, date_str: str, md_get: Callable) -> Optional[float]:
    """
    Fetch EOD close price from MarketData.app candles API.
    Uses the daily candle for the specific date.
    """
    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",
            {"from": date_str, "to": date_str, "countback": 1}
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return None
        closes = data.get("c", [])
        if closes and len(closes) > 0 and closes[-1] is not None:
            return float(closes[-1])
        return None
    except Exception as e:
        log.warning(f"EOD close fetch failed for {ticker} on {date_str}: {e}")
        return None
