# em_reconciler.py
# ═══════════════════════════════════════════════════════════════════
# EM Prediction Reconciler — compares predicted ranges and dealer-map
# levels against actual end-of-day behavior so the bot can improve.
# ═══════════════════════════════════════════════════════════════════

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable, Any

log = logging.getLogger(__name__)


CONF_BUFFER_MULTIPLIERS = {
    "HIGH": 0.18,
    "MODERATE": 0.12,
    "LOW": 0.08,
}


def _coerce_eod_payload(payload: Any) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if payload is None:
        return None, None, None
    if isinstance(payload, dict):
        return payload.get("close"), payload.get("high"), payload.get("low")
    try:
        v = float(payload)
        return v, None, None
    except Exception:
        return None, None, None


def _direction_buffer(entry: Dict) -> float:
    stored = entry.get("direction_buffer_abs")
    if stored is not None:
        try:
            return float(stored)
        except Exception:
            pass
    em_1sd = float(entry.get("em_1sd") or 0.0)
    conf = str(entry.get("v4_confidence") or "MODERATE").upper()
    mult = CONF_BUFFER_MULTIPLIERS.get(conf, 0.12)
    ticker = str(entry.get("ticker") or "").upper()
    floor = 0.75 if ticker in {"SPY", "QQQ", "SPX", "IWM", "DIA"} else 0.50
    return round(max(floor, em_1sd * mult), 2)


def reconcile_em_predictions(
    redis_client,
    fetch_eod_close: Callable[[str, str], Any],
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
                eod_close, eod_high, eod_low = _coerce_eod_payload(eod_payload)
                if eod_close is None:
                    log.debug(f"Reconciler: no EOD price for {ticker} on {pred_date}")
                    continue

                entry["eod_price"] = round(float(eod_close), 2)
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
                log.info(
                    f"Reconciled: {key} | EOD=${eod_close:.2f} | raw={entry.get('direction_correct')} | "
                    f"buffered={entry.get('buffered_direction_correct')} | neutral={entry.get('neutral_correct')}"
                )
            except Exception as e:
                log.warning(f"Reconciler: error processing {key}: {e}")
                continue

    log.info(f"Reconciler complete: {len(reconciled)} entries processed")
    return reconciled


def _score_prediction(entry: Dict) -> Dict:
    spot = entry.get("spot_at_prediction")
    eod = entry.get("eod_price")
    bull_1 = entry.get("bull_1sd")
    bear_1 = entry.get("bear_1sd")
    bull_2 = entry.get("bull_2sd")
    bear_2 = entry.get("bear_2sd")
    em_1sd = entry.get("em_1sd")
    bias_score = entry.get("bias_score", 0)
    eod_high = entry.get("eod_high")
    eod_low = entry.get("eod_low")

    if not all(v is not None for v in [spot, eod, bull_1, bear_1, em_1sd]):
        entry["scoring_error"] = "missing fields"
        return entry

    spot = float(spot)
    eod = float(eod)
    bull_1 = float(bull_1)
    bear_1 = float(bear_1)
    bull_2 = float(bull_2) if bull_2 is not None else None
    bear_2 = float(bear_2) if bear_2 is not None else None
    em_1sd = float(em_1sd)
    actual_move = eod - spot
    abs_move = abs(actual_move)
    buffer_abs = _direction_buffer(entry)
    entry["direction_buffer_abs"] = round(buffer_abs, 2)

    entry["in_1_sigma"] = (bear_1 <= eod <= bull_1)
    entry["in_2_sigma"] = (bear_2 <= eod <= bull_2) if bear_2 is not None and bull_2 is not None else None

    if bias_score >= 2:
        entry["predicted_direction"] = "bull"
        entry["direction_correct"] = actual_move > 0
        entry["buffered_direction_correct"] = eod >= (spot + buffer_abs)
    elif bias_score <= -2:
        entry["predicted_direction"] = "bear"
        entry["direction_correct"] = actual_move < 0
        entry["buffered_direction_correct"] = eod <= (spot - buffer_abs)
    else:
        entry["predicted_direction"] = "neutral"
        entry["direction_correct"] = None
        entry["buffered_direction_correct"] = None

    pin_lo = entry.get("put_wall")
    pin_hi = entry.get("call_wall")
    if pin_lo is not None and pin_hi is not None and pin_lo > pin_hi:
        pin_lo, pin_hi = pin_hi, pin_lo

    if pin_lo is not None and pin_hi is not None:
        close_in_pin = pin_lo <= eod <= pin_hi
        if eod_high is not None and eod_low is not None:
            day_in_pin = (float(eod_low) >= pin_lo) and (float(eod_high) <= pin_hi)
        else:
            day_in_pin = close_in_pin
    else:
        close_in_pin = None
        if eod_high is not None and eod_low is not None:
            day_in_pin = (float(eod_low) >= bear_1) and (float(eod_high) <= bull_1)
        else:
            day_in_pin = entry["in_1_sigma"]

    entry["close_in_pin_zone"] = close_in_pin
    entry["day_in_pin_zone"] = day_in_pin
    entry["condor_style_success"] = day_in_pin
    if entry["predicted_direction"] == "neutral":
        entry["neutral_correct"] = day_in_pin if day_in_pin is not None else entry["in_1_sigma"]
    else:
        entry["neutral_correct"] = None

    accel_up = entry.get("accel_up")
    accel_dn = entry.get("accel_dn")
    if entry["predicted_direction"] == "bull" and accel_up is not None:
        entry["primary_trigger_correct"] = (eod_high or eod) >= float(accel_up)
    elif entry["predicted_direction"] == "bear" and accel_dn is not None:
        entry["primary_trigger_correct"] = (eod_low or eod) <= float(accel_dn)
    elif entry["predicted_direction"] == "neutral":
        entry["primary_trigger_correct"] = None
    else:
        entry["primary_trigger_correct"] = None

    gamma_flip = entry.get("gamma_flip")
    if gamma_flip is not None:
        hi_check = float(eod_high) if eod_high is not None else eod
        lo_check = float(eod_low) if eod_low is not None else eod
        entry["gamma_flip_touched"] = lo_check <= float(gamma_flip) <= hi_check
    else:
        entry["gamma_flip_touched"] = None

    max_pain = entry.get("max_pain")
    if max_pain is not None:
        entry["max_pain_close_hit"] = abs(eod - float(max_pain)) <= buffer_abs
    else:
        entry["max_pain_close_hit"] = None

    entry["move_actual"] = round(actual_move, 2)
    entry["move_predicted_1sd"] = round(em_1sd, 2)
    entry["move_ratio"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None
    entry["error_sigma"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None

    v4_bias = entry.get("v4_bias")
    if v4_bias == "UPSIDE":
        entry["v4_direction_correct"] = actual_move > 0
        entry["v4_buffered_direction_correct"] = eod >= (spot + buffer_abs)
    elif v4_bias == "DOWNSIDE":
        entry["v4_direction_correct"] = actual_move < 0
        entry["v4_buffered_direction_correct"] = eod <= (spot - buffer_abs)
    else:
        entry["v4_direction_correct"] = None
        entry["v4_buffered_direction_correct"] = None

    return entry


def _pct(num: int, den: int) -> Optional[float]:
    return round(num / den * 100, 1) if den else None


def compute_accuracy_stats(entries: List[Dict]) -> Dict:
    reconciled = [e for e in entries if e.get("reconciled") and "in_1_sigma" in e]
    if not reconciled:
        return {"n": 0, "error": "no reconciled entries"}

    n = len(reconciled)
    ratios = [e["move_ratio"] for e in reconciled if e.get("move_ratio") is not None]

    raw_entries = [e for e in reconciled if e.get("direction_correct") is not None]
    raw_ok = sum(1 for e in raw_entries if e.get("direction_correct"))
    buf_entries = [e for e in reconciled if e.get("buffered_direction_correct") is not None]
    buf_ok = sum(1 for e in buf_entries if e.get("buffered_direction_correct"))

    v4_raw_entries = [e for e in reconciled if e.get("v4_direction_correct") is not None]
    v4_raw_ok = sum(1 for e in v4_raw_entries if e.get("v4_direction_correct"))
    v4_buf_entries = [e for e in reconciled if e.get("v4_buffered_direction_correct") is not None]
    v4_buf_ok = sum(1 for e in v4_buf_entries if e.get("v4_buffered_direction_correct"))

    neutral_entries = [e for e in reconciled if e.get("predicted_direction") == "neutral"]
    neutral_ok = sum(1 for e in neutral_entries if e.get("neutral_correct"))
    close_pin_entries = [e for e in reconciled if e.get("close_in_pin_zone") is not None]
    close_pin_ok = sum(1 for e in close_pin_entries if e.get("close_in_pin_zone"))
    day_pin_entries = [e for e in reconciled if e.get("day_in_pin_zone") is not None]
    day_pin_ok = sum(1 for e in day_pin_entries if e.get("day_in_pin_zone"))
    condor_entries = [e for e in reconciled if e.get("condor_style_success") is not None]
    condor_ok = sum(1 for e in condor_entries if e.get("condor_style_success"))

    trig_entries = [e for e in reconciled if e.get("primary_trigger_correct") is not None]
    trig_ok = sum(1 for e in trig_entries if e.get("primary_trigger_correct"))
    flip_entries = [e for e in reconciled if e.get("gamma_flip_touched") is not None]
    flip_ok = sum(1 for e in flip_entries if e.get("gamma_flip_touched"))
    pain_entries = [e for e in reconciled if e.get("max_pain_close_hit") is not None]
    pain_ok = sum(1 for e in pain_entries if e.get("max_pain_close_hit"))

    by_conf = {}
    for label in ("HIGH", "MODERATE", "LOW"):
        subset = [e for e in reconciled if str(e.get("v4_confidence") or "").upper() == label]
        if not subset:
            continue
        s_raw = [e for e in subset if e.get("direction_correct") is not None]
        s_buf = [e for e in subset if e.get("buffered_direction_correct") is not None]
        by_conf[label] = {
            "n": len(subset),
            "raw_direction_pct": _pct(sum(1 for e in s_raw if e.get("direction_correct")), len(s_raw)),
            "buffered_direction_pct": _pct(sum(1 for e in s_buf if e.get("buffered_direction_correct")), len(s_buf)),
        }

    by_ticker = {}
    for t in sorted(set(e.get("ticker", "?") for e in reconciled)):
        subset = [e for e in reconciled if e.get("ticker") == t]
        s_buf = [e for e in subset if e.get("buffered_direction_correct") is not None]
        s_condor = [e for e in subset if e.get("condor_style_success") is not None]
        by_ticker[t] = {
            "n": len(subset),
            "buffered_direction_pct": _pct(sum(1 for e in s_buf if e.get("buffered_direction_correct")), len(s_buf)),
            "condor_style_pct": _pct(sum(1 for e in s_condor if e.get("condor_style_success")), len(s_condor)),
        }

    return {
        "n": n,
        "in_1_sigma_pct": _pct(sum(1 for e in reconciled if e.get("in_1_sigma")), n),
        "in_2_sigma_pct": _pct(sum(1 for e in reconciled if e.get("in_2_sigma")), n),
        "expected_1_sigma_pct": 68.3,
        "expected_2_sigma_pct": 95.4,
        "avg_move_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "exceeded_1sd_pct": _pct(sum(1 for r in ratios if r > 1.0), len(ratios)),
        "raw_direction_n": len(raw_entries),
        "raw_direction_pct": _pct(raw_ok, len(raw_entries)),
        "buffered_direction_n": len(buf_entries),
        "buffered_direction_pct": _pct(buf_ok, len(buf_entries)),
        "v4_raw_direction_n": len(v4_raw_entries),
        "v4_raw_direction_pct": _pct(v4_raw_ok, len(v4_raw_entries)),
        "v4_buffered_direction_n": len(v4_buf_entries),
        "v4_buffered_direction_pct": _pct(v4_buf_ok, len(v4_buf_entries)),
        "neutral_n": len(neutral_entries),
        "neutral_correct_pct": _pct(neutral_ok, len(neutral_entries)),
        "pin_close_n": len(close_pin_entries),
        "pin_close_pct": _pct(close_pin_ok, len(close_pin_entries)),
        "pin_day_n": len(day_pin_entries),
        "pin_day_pct": _pct(day_pin_ok, len(day_pin_entries)),
        "condor_n": len(condor_entries),
        "condor_pct": _pct(condor_ok, len(condor_entries)),
        "trigger_n": len(trig_entries),
        "trigger_pct": _pct(trig_ok, len(trig_entries)),
        "flip_touch_n": len(flip_entries),
        "flip_touch_pct": _pct(flip_ok, len(flip_entries)),
        "max_pain_n": len(pain_entries),
        "max_pain_pct": _pct(pain_ok, len(pain_entries)),
        "by_confidence": by_conf,
        "by_ticker": by_ticker,
    }


def format_accuracy_report(stats: Dict) -> str:
    if stats.get("n", 0) == 0:
        return "📊 EM Scorecard: No reconciled predictions yet."

    n = stats["n"]
    lines = [
        f"📊 EM PREDICTION SCORECARD — {n} predictions",
        "═" * 36,
        "",
        "── Useful Direction ──",
        f"  Raw direction:        {stats.get('raw_direction_pct', 0):.1f}%  ({stats.get('raw_direction_n', 0)} predictions)",
        f"  Buffered direction:   {stats.get('buffered_direction_pct', 0):.1f}%  ({stats.get('buffered_direction_n', 0)} predictions)",
    ]
    if stats.get("v4_raw_direction_n", 0):
        lines.append(f"  v4 raw direction:     {stats.get('v4_raw_direction_pct', 0):.1f}%  ({stats.get('v4_raw_direction_n', 0)})")
    if stats.get("v4_buffered_direction_n", 0):
        lines.append(f"  v4 buffered direction:{stats.get('v4_buffered_direction_pct', 0):.1f}%  ({stats.get('v4_buffered_direction_n', 0)})")

    lines += ["", "── Neutral / Range Outcome ──"]
    if stats.get("neutral_n", 0):
        lines.append(f"  Neutral correct:      {stats.get('neutral_correct_pct', 0):.1f}%  ({stats.get('neutral_n', 0)} neutral calls)")
    if stats.get("pin_close_n", 0):
        lines.append(f"  Pin-zone close rate:  {stats.get('pin_close_pct', 0):.1f}%")
    if stats.get("pin_day_n", 0):
        lines.append(f"  Day stayed in range:  {stats.get('pin_day_pct', 0):.1f}%")
    if stats.get("condor_n", 0):
        lines.append(f"  Condor-style success: {stats.get('condor_pct', 0):.1f}%")

    lines += ["", "── Dealer Levels ──"]
    if stats.get("trigger_n", 0):
        lines.append(f"  Primary trigger hit:  {stats.get('trigger_pct', 0):.1f}%")
    if stats.get("flip_touch_n", 0):
        lines.append(f"  Gamma flip touched:   {stats.get('flip_touch_pct', 0):.1f}%")
    if stats.get("max_pain_n", 0):
        lines.append(f"  Max-pain close hit:   {stats.get('max_pain_pct', 0):.1f}%")

    lines += [
        "",
        "── IV Calibration ──",
        f"  1σ close-in-range:    {stats['in_1_sigma_pct']:.1f}%  (expected {stats['expected_1_sigma_pct']}%)",
        f"  2σ close-in-range:    {stats['in_2_sigma_pct']:.1f}%  (expected {stats['expected_2_sigma_pct']}%)",
    ]
    if stats.get("avg_move_ratio") is not None:
        lines.append(f"  Avg actual/predicted: {stats['avg_move_ratio']:.2f}x")
    if stats.get("exceeded_1sd_pct") is not None:
        lines.append(f"  Exceeded 1σ:          {stats['exceeded_1sd_pct']:.1f}%")

    if stats.get("by_confidence"):
        lines += ["", "── By Snapshot Quality ──"]
        for label in ("HIGH", "MODERATE", "LOW"):
            c = stats["by_confidence"].get(label)
            if not c:
                continue
            raw_txt = f"raw {c['raw_direction_pct']:.0f}%" if c.get("raw_direction_pct") is not None else "raw n/a"
            buf_txt = f"buffered {c['buffered_direction_pct']:.0f}%" if c.get("buffered_direction_pct") is not None else "buffered n/a"
            lines.append(f"  {label}: {c['n']} cards | {raw_txt} | {buf_txt}")

    if stats.get("by_ticker"):
        lines += ["", "── By Ticker ──"]
        for t, s in stats["by_ticker"].items():
            extras = []
            if s.get("buffered_direction_pct") is not None:
                extras.append(f"buffered {s['buffered_direction_pct']:.0f}%")
            if s.get("condor_style_pct") is not None:
                extras.append(f"condor {s['condor_style_pct']:.0f}%")
            extra_txt = " | " + " | ".join(extras) if extras else ""
            lines.append(f"  {t}: {s['n']} cards{extra_txt}")

    lines += ["", "— Not financial advice —"]
    return "\n".join(lines)


def fetch_eod_close_marketdata(ticker: str, date_str: str, md_get: Callable) -> Optional[Dict[str, float]]:
    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",
            {"from": date_str, "to": date_str, "countback": 1}
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return None
        closes = data.get("c", [])
        highs = data.get("h", [])
        lows = data.get("l", [])
        if not closes or closes[-1] is None:
            return None
        out = {"close": float(closes[-1])}
        if highs and highs[-1] is not None:
            out["high"] = float(highs[-1])
        if lows and lows[-1] is not None:
            out["low"] = float(lows[-1])
        return out
    except Exception as e:
        log.warning(f"EOD candle fetch failed for {ticker} on {date_str}: {e}")
        return None
