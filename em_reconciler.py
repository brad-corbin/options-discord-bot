# em_reconciler.py
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable

log = logging.getLogger(__name__)


def reconcile_em_predictions(
    redis_client,
    fetch_eod_close: Callable[[str, str], Optional[float]],
    store_set: Callable,
    lookback_days: int = 5,
) -> List[Dict]:
    if not redis_client:
        log.warning("Reconciler: no Redis connection")
        return []

    reconciled = []
    today = datetime.now(timezone.utc).date()

    for day_offset in range(1, lookback_days + 1):
        target_date = today - timedelta(days=day_offset)
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
                if entry.get("reconciled"):
                    reconciled.append(entry)
                    continue

                ticker = entry.get("ticker")
                pred_date = entry.get("date")
                if not ticker or not pred_date:
                    continue

                eod_payload = fetch_eod_close(ticker, pred_date)
                if eod_payload is None:
                    continue

                if isinstance(eod_payload, dict):
                    eod_price = eod_payload.get("close")
                    eod_high = eod_payload.get("high")
                    eod_low = eod_payload.get("low")
                else:
                    eod_price = eod_payload
                    eod_high = None
                    eod_low = None

                if eod_price is None:
                    continue

                entry["eod_price"] = round(float(eod_price), 2)
                entry["eod_high"] = round(float(eod_high), 2) if eod_high is not None else None
                entry["eod_low"] = round(float(eod_low), 2) if eod_low is not None else None
                entry["reconciled"] = True
                entry = _score_prediction(entry)

                ttl = redis_client.ttl(key)
                if ttl and ttl > 0:
                    store_set(key, json.dumps(entry), ttl=ttl)
                else:
                    store_set(key, json.dumps(entry), ttl=90 * 86400)

                reconciled.append(entry)
            except Exception as e:
                log.warning(f"Reconciler: error processing {key}: {e}")
                continue

    log.info(f"Reconciler complete: {len(reconciled)} entries processed")
    return reconciled


def _score_prediction(entry: Dict) -> Dict:
    spot = entry.get("spot_at_prediction")
    eod = entry.get("eod_price")
    eod_high = entry.get("eod_high")
    eod_low = entry.get("eod_low")
    bull_1 = entry.get("bull_1sd")
    bear_1 = entry.get("bear_1sd")
    bull_2 = entry.get("bull_2sd")
    bear_2 = entry.get("bear_2sd")
    em_1sd = entry.get("em_1sd")
    bias_score = entry.get("bias_score", 0)
    direction_buffer_abs = entry.get("direction_buffer_abs")
    gamma_flip = entry.get("gamma_flip")
    call_wall = entry.get("call_wall")
    put_wall = entry.get("put_wall")
    accel_up = entry.get("accel_up")
    accel_dn = entry.get("accel_dn")

    if not all(v is not None for v in [spot, eod, bull_1, bear_1, em_1sd]):
        entry["scoring_error"] = "missing fields"
        return entry

    actual_move = eod - spot
    abs_move = abs(actual_move)
    entry["in_1_sigma"] = (bear_1 <= eod <= bull_1)
    entry["in_2_sigma"] = (bear_2 <= eod <= bull_2) if bear_2 is not None and bull_2 is not None else None

    if bias_score >= 2:
        predicted_direction = "bull"
        entry["direction_correct"] = actual_move > 0
    elif bias_score <= -2:
        predicted_direction = "bear"
        entry["direction_correct"] = actual_move < 0
    else:
        predicted_direction = "neutral"
        entry["direction_correct"] = None
    entry["predicted_direction"] = predicted_direction

    if direction_buffer_abs is None:
        direction_buffer_abs = max(0.5, (em_1sd or 0) * 0.20)
    entry["direction_buffer_abs"] = round(float(direction_buffer_abs), 2)
    if predicted_direction == "bull":
        entry["direction_buffered_correct"] = eod >= (spot + direction_buffer_abs)
    elif predicted_direction == "bear":
        entry["direction_buffered_correct"] = eod <= (spot - direction_buffer_abs)
    else:
        entry["direction_buffered_correct"] = None

    entry["move_actual"] = round(actual_move, 2)
    entry["move_predicted_1sd"] = round(em_1sd, 2)
    entry["move_ratio"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None
    entry["error_sigma"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None

    entry["close_in_pin_zone"] = None
    entry["day_in_pin_zone"] = None
    entry["neutral_condor_success"] = None
    if call_wall is not None and put_wall is not None:
        entry["close_in_pin_zone"] = (put_wall <= eod <= call_wall)
        if eod_high is not None and eod_low is not None:
            entry["day_in_pin_zone"] = (eod_high <= call_wall and eod_low >= put_wall)
            entry["neutral_condor_success"] = entry["day_in_pin_zone"]

    entry["day_in_1_sigma_range"] = None
    if eod_high is not None and eod_low is not None:
        entry["day_in_1_sigma_range"] = (eod_high <= bull_1 and eod_low >= bear_1)
        if entry["neutral_condor_success"] is None:
            entry["neutral_condor_success"] = entry["day_in_1_sigma_range"]

    if predicted_direction == "neutral":
        entry["neutral_correct"] = bool(entry.get("neutral_condor_success")) if entry.get("neutral_condor_success") is not None else entry.get("in_1_sigma")
    else:
        entry["neutral_correct"] = None

    entry["flip_touched"] = (eod_low <= gamma_flip <= eod_high) if (gamma_flip is not None and eod_high is not None and eod_low is not None) else None
    entry["call_wall_touched"] = (eod_high >= call_wall) if (call_wall is not None and eod_high is not None) else None
    entry["put_wall_touched"] = (eod_low <= put_wall) if (put_wall is not None and eod_low is not None) else None
    entry["up_trigger_hit"] = (eod_high >= accel_up) if (accel_up is not None and eod_high is not None) else None
    entry["down_trigger_hit"] = (eod_low <= accel_dn) if (accel_dn is not None and eod_low is not None) else None

    if predicted_direction == "bull":
        entry["primary_trigger_correct"] = entry.get("up_trigger_hit")
    elif predicted_direction == "bear":
        entry["primary_trigger_correct"] = entry.get("down_trigger_hit")
    else:
        entry["primary_trigger_correct"] = entry.get("neutral_condor_success")

    v4_bias = entry.get("v4_bias")
    if v4_bias:
        if v4_bias == "UPSIDE":
            entry["v4_direction_correct"] = actual_move > 0
            entry["v4_direction_buffered_correct"] = eod >= (spot + direction_buffer_abs)
        elif v4_bias == "DOWNSIDE":
            entry["v4_direction_correct"] = actual_move < 0
            entry["v4_direction_buffered_correct"] = eod <= (spot - direction_buffer_abs)
        else:
            entry["v4_direction_correct"] = None
            entry["v4_direction_buffered_correct"] = None

    return entry


def compute_accuracy_stats(entries: List[Dict]) -> Dict:
    reconciled = [e for e in entries if e.get("reconciled") and "in_1_sigma" in e]
    if not reconciled:
        return {"n": 0, "error": "no reconciled entries"}

    n = len(reconciled)
    in_1 = sum(1 for e in reconciled if e.get("in_1_sigma"))
    in_2 = sum(1 for e in reconciled if e.get("in_2_sigma"))
    dir_entries = [e for e in reconciled if e.get("direction_correct") is not None]
    dir_correct = sum(1 for e in dir_entries if e["direction_correct"])
    buf_entries = [e for e in reconciled if e.get("direction_buffered_correct") is not None]
    buf_correct = sum(1 for e in buf_entries if e["direction_buffered_correct"])

    neutral_entries = [e for e in reconciled if e.get("predicted_direction") == "neutral"]
    neutral_correct = sum(1 for e in neutral_entries if e.get("neutral_correct"))
    neutral_close_pin = sum(1 for e in neutral_entries if e.get("close_in_pin_zone"))
    neutral_day_pin = sum(1 for e in neutral_entries if e.get("day_in_pin_zone"))
    neutral_condor = sum(1 for e in neutral_entries if e.get("neutral_condor_success"))

    trig_entries = [e for e in reconciled if e.get("primary_trigger_correct") is not None]
    trig_correct = sum(1 for e in trig_entries if e.get("primary_trigger_correct"))
    flip_entries = [e for e in reconciled if e.get("flip_touched") is not None]
    flip_touched = sum(1 for e in flip_entries if e.get("flip_touched"))

    v4_entries = [e for e in reconciled if e.get("v4_direction_correct") is not None]
    v4_correct = sum(1 for e in v4_entries if e["v4_direction_correct"])
    v4_buf_entries = [e for e in reconciled if e.get("v4_direction_buffered_correct") is not None]
    v4_buf_correct = sum(1 for e in v4_buf_entries if e["v4_direction_buffered_correct"])

    ratios = [e["move_ratio"] for e in reconciled if e.get("move_ratio") is not None]
    avg_ratio = sum(ratios) / len(ratios) if ratios else None
    exceeded_1sd = sum(1 for r in ratios if r > 1.0)

    by_conf = {}
    for label in ("HIGH", "MODERATE", "LOW"):
        subset = [e for e in reconciled if e.get("v4_confidence") == label]
        if subset:
            s_dir = [e for e in subset if e.get("direction_correct") is not None]
            s_buf = [e for e in subset if e.get("direction_buffered_correct") is not None]
            s_neu = [e for e in subset if e.get("predicted_direction") == "neutral"]
            s_trg = [e for e in subset if e.get("primary_trigger_correct") is not None]
            by_conf[label] = {
                "n": len(subset),
                "in_1_sigma_pct": round(sum(1 for e in subset if e.get("in_1_sigma")) / len(subset) * 100, 1),
                "direction_pct": round(sum(1 for e in s_dir if e.get("direction_correct")) / len(s_dir) * 100, 1) if s_dir else None,
                "buffered_direction_pct": round(sum(1 for e in s_buf if e.get("direction_buffered_correct")) / len(s_buf) * 100, 1) if s_buf else None,
                "neutral_pct": round(sum(1 for e in s_neu if e.get("neutral_correct")) / len(s_neu) * 100, 1) if s_neu else None,
                "trigger_pct": round(sum(1 for e in s_trg if e.get("primary_trigger_correct")) / len(s_trg) * 100, 1) if s_trg else None,
            }

    by_ticker = {}
    tickers = set(e.get("ticker", "?") for e in reconciled)
    for t in tickers:
        subset = [e for e in reconciled if e.get("ticker") == t]
        dir_subset = [e for e in subset if e.get("direction_buffered_correct") is not None]
        neu_subset = [e for e in subset if e.get("neutral_correct") is not None]
        by_ticker[t] = {
            "n": len(subset),
            "in_1_sigma_pct": round(sum(1 for e in subset if e.get("in_1_sigma")) / len(subset) * 100, 1),
            "buffered_direction_pct": round(sum(1 for e in dir_subset if e.get("direction_buffered_correct")) / len(dir_subset) * 100, 1) if dir_subset else None,
            "neutral_pct": round(sum(1 for e in neu_subset if e.get("neutral_correct")) / len(neu_subset) * 100, 1) if neu_subset else None,
        }

    return {
        "n": n,
        "in_1_sigma_pct": round(in_1 / n * 100, 1),
        "in_2_sigma_pct": round(in_2 / n * 100, 1),
        "expected_1_sigma_pct": 68.3,
        "expected_2_sigma_pct": 95.4,
        "direction_n": len(dir_entries),
        "direction_correct_pct": round(dir_correct / len(dir_entries) * 100, 1) if dir_entries else None,
        "buffered_direction_n": len(buf_entries),
        "buffered_direction_correct_pct": round(buf_correct / len(buf_entries) * 100, 1) if buf_entries else None,
        "neutral_n": len(neutral_entries),
        "neutral_correct_pct": round(neutral_correct / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "neutral_close_pin_pct": round(neutral_close_pin / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "neutral_day_pin_pct": round(neutral_day_pin / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "neutral_condor_pct": round(neutral_condor / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "trigger_n": len(trig_entries),
        "trigger_correct_pct": round(trig_correct / len(trig_entries) * 100, 1) if trig_entries else None,
        "flip_touch_n": len(flip_entries),
        "flip_touch_pct": round(flip_touched / len(flip_entries) * 100, 1) if flip_entries else None,
        "v4_direction_n": len(v4_entries),
        "v4_direction_correct_pct": round(v4_correct / len(v4_entries) * 100, 1) if v4_entries else None,
        "v4_buffered_direction_n": len(v4_buf_entries),
        "v4_buffered_direction_correct_pct": round(v4_buf_correct / len(v4_buf_entries) * 100, 1) if v4_buf_entries else None,
        "avg_move_ratio": round(avg_ratio, 3) if avg_ratio is not None else None,
        "exceeded_1sd_pct": round(exceeded_1sd / len(ratios) * 100, 1) if ratios else None,
        "by_confidence": by_conf,
        "by_ticker": by_ticker,
    }


def format_accuracy_report(stats: Dict) -> str:
    if stats.get("n", 0) == 0:
        return "📊 EM Accuracy: No reconciled predictions yet."

    n = stats["n"]
    lines = [
        f"📊 EM PREDICTION ACCURACY — {n} predictions",
        "═" * 32,
        "",
        "── Useful Direction ──",
    ]
    if stats.get("direction_n", 0) > 0:
        lines.append(f"  Raw direction:      {stats['direction_correct_pct']:.1f}%  ({stats['direction_n']} predictions)")
    if stats.get("buffered_direction_n", 0) > 0:
        lines.append(f"  Buffered direction: {stats['buffered_direction_correct_pct']:.1f}%  ({stats['buffered_direction_n']} predictions)")
    if stats.get("v4_direction_n", 0) > 0:
        lines.append(f"  v4 raw direction:   {stats['v4_direction_correct_pct']:.1f}%  ({stats['v4_direction_n']} predictions)")
    if stats.get("v4_buffered_direction_n", 0) > 0:
        lines.append(f"  v4 buffered dir:    {stats['v4_buffered_direction_correct_pct']:.1f}%  ({stats['v4_buffered_direction_n']} predictions)")

    if stats.get("neutral_n", 0) > 0:
        lines += [
            "",
            "── Neutral / Range-Bound ──",
            f"  Neutral cards:        {stats['neutral_n']}",
            f"  Neutral correct:      {stats['neutral_correct_pct']:.1f}%",
            f"  Close inside pin:     {stats['neutral_close_pin_pct']:.1f}%" if stats.get('neutral_close_pin_pct') is not None else "  Close inside pin:     n/a",
            f"  Day stayed inside pin:{stats['neutral_day_pin_pct']:.1f}%" if stats.get('neutral_day_pin_pct') is not None else "  Day stayed inside pin:n/a",
            f"  Condor-style success: {stats['neutral_condor_pct']:.1f}%" if stats.get('neutral_condor_pct') is not None else "  Condor-style success: n/a",
        ]

    if stats.get("trigger_n", 0) > 0 or stats.get("flip_touch_n", 0) > 0:
        lines += ["", "── Levels / Dealer Map ──"]
        if stats.get("trigger_n", 0) > 0:
            lines.append(f"  Primary trigger correct: {stats['trigger_correct_pct']:.1f}%  ({stats['trigger_n']} cards)")
        if stats.get("flip_touch_n", 0) > 0:
            lines.append(f"  Gamma flip touched:      {stats['flip_touch_pct']:.1f}%  ({stats['flip_touch_n']} cards)")

    lines += [
        "",
        "── IV Calibration ──",
        f"  1σ close-in-range: {stats['in_1_sigma_pct']:.1f}%  (expected ~{stats['expected_1_sigma_pct']}%)",
        f"  2σ close-in-range: {stats['in_2_sigma_pct']:.1f}%  (expected ~{stats['expected_2_sigma_pct']}%)",
    ]
    diff_1 = stats["in_1_sigma_pct"] - stats["expected_1_sigma_pct"]
    if abs(diff_1) < 5:
        lines.append("  ✅ 1σ range well-calibrated")
    elif diff_1 > 0:
        lines.append(f"  ⚠️ 1σ range too wide by ~{diff_1:.0f}% (IV overestimates moves)")
    else:
        lines.append(f"  ⚠️ 1σ range too narrow by ~{abs(diff_1):.0f}% (IV underestimates moves)")

    if stats.get("avg_move_ratio") is not None:
        lines += [
            "",
            "── Move Size ──",
            f"  Avg actual/predicted: {stats['avg_move_ratio']:.2f}x",
            f"  Exceeded 1σ: {stats['exceeded_1sd_pct']:.1f}% of the time",
        ]

    if stats.get("by_confidence"):
        lines += ["", "── By v4 Snapshot Quality ──"]
        for label in ("HIGH", "MODERATE", "LOW"):
            if label in stats["by_confidence"]:
                c = stats["by_confidence"][label]
                parts = [f"{label}: {c['n']} cards"]
                if c.get("buffered_direction_pct") is not None:
                    parts.append(f"buf dir {c['buffered_direction_pct']:.0f}%")
                elif c.get("direction_pct") is not None:
                    parts.append(f"dir {c['direction_pct']:.0f}%")
                if c.get("neutral_pct") is not None:
                    parts.append(f"neutral {c['neutral_pct']:.0f}%")
                if c.get("trigger_pct") is not None:
                    parts.append(f"trigger {c['trigger_pct']:.0f}%")
                lines.append("  " + " · ".join(parts))

    if stats.get("by_ticker"):
        lines += ["", "── By Ticker ──"]
        for t, s in sorted(stats["by_ticker"].items()):
            extras = []
            if s.get("buffered_direction_pct") is not None:
                extras.append(f"buf dir {s['buffered_direction_pct']:.0f}%")
            if s.get("neutral_pct") is not None:
                extras.append(f"neutral {s['neutral_pct']:.0f}%")
            suffix = (" | " + ", ".join(extras)) if extras else ""
            lines.append(f"  {t}: {s['n']} cards, 1σ {s['in_1_sigma_pct']:.0f}%{suffix}")

    lines += ["", "— Not financial advice —"]
    RETURN_SENTINEL


def fetch_eod_close_marketdata(ticker: str, date_str: str, md_get: Callable) -> Optional[Dict]:
    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",
            {"from": date_str, "to": date_str, "countback": 1}
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return None
        closes = data.get("c", []) or []
        highs = data.get("h", []) or []
        lows = data.get("l", []) or []
        if not closes or closes[-1] is None:
            return None
        return {
            "close": float(closes[-1]),
            "high": float(highs[-1]) if highs and highs[-1] is not None else None,
            "low": float(lows[-1]) if lows and lows[-1] is not None else None,
        }
    except Exception as e:
        log.warning(f"EOD close fetch failed for {ticker} on {date_str}: {e}")
        return None
