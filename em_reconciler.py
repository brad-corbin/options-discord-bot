# em_reconciler.py
# Compares logged EM predictions against next-session closes so the bot can
# learn from measurable outcomes rather than just wide 1σ containment.

import csv
import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable

import requests
import jwt

log = logging.getLogger(__name__)

AUTO_LOG_DIR = os.getenv("AUTO_LOG_DIR", "/mnt/data/bot_logs").strip() or "/mnt/data/bot_logs"
AUTO_LOG_ENABLE = os.getenv("AUTO_LOG_ENABLE", "1").strip().lower() not in ("0", "false", "no", "off")
GOOGLE_SHEETS_ENABLE = os.getenv("GOOGLE_SHEETS_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1bXyAVQ8dB-dTyFVN6uv6PVtuphwRZ6VjIp2W9dcm9iw").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_RECON_TAB = os.getenv("GOOGLE_SHEET_RECON_TAB", "em_reconciliation").strip() or "em_reconciliation"


def _ensure_log_dir():
    try:
        os.makedirs(AUTO_LOG_DIR, exist_ok=True)
    except Exception as e:
        log.warning(f"Reconciler log dir unavailable ({AUTO_LOG_DIR}): {e}")


def _append_csv_row(filename: str, fieldnames: List[str], row: Dict):
    if not AUTO_LOG_ENABLE:
        return
    try:
        _ensure_log_dir()
        path = os.path.join(AUTO_LOG_DIR, filename)
        exists = os.path.exists(path)
        safe_row = {k: row.get(k) for k in fieldnames}
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists or os.path.getsize(path) == 0:
                writer.writeheader()
            writer.writerow(safe_row)
        _append_google_sheet_row(fieldnames, safe_row)
    except Exception as e:
        log.warning(f"Reconciler CSV auto-log failed ({filename}): {e}")


_google_sheets_token_cache = {"token": None, "exp": 0}
_google_sheets_sa_cache = None
_google_sheets_headers_ok = False


def _load_google_service_account() -> Optional[Dict]:
    global _google_sheets_sa_cache
    if _google_sheets_sa_cache is not None:
        return _google_sheets_sa_cache
    try:
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            _google_sheets_sa_cache = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            return _google_sheets_sa_cache
        if GOOGLE_SERVICE_ACCOUNT_FILE and os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
                _google_sheets_sa_cache = json.load(f)
                return _google_sheets_sa_cache
        default_path = "/mnt/data/corbin-bot-tracking-0249b119c63f.json"
        if os.path.exists(default_path):
            with open(default_path, "r", encoding="utf-8") as f:
                _google_sheets_sa_cache = json.load(f)
                return _google_sheets_sa_cache
    except Exception as e:
        log.warning(f"Reconciler Google Sheets credentials load failed: {e}")
    return None


def _get_google_access_token() -> Optional[str]:
    if not GOOGLE_SHEETS_ENABLE or not GOOGLE_SHEET_ID:
        return None
    now = int(time.time())
    if _google_sheets_token_cache.get("token") and now < int(_google_sheets_token_cache.get("exp", 0)) - 60:
        return _google_sheets_token_cache["token"]
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
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if token:
            _google_sheets_token_cache.update({"token": token, "exp": issued + int(data.get("expires_in", 3600))})
            return token
    except Exception as e:
        log.warning(f"Reconciler Google Sheets token fetch failed: {e}")
    return None


def _append_google_sheet_values(values: List[List], token: str) -> bool:
    try:
        rng = requests.utils.quote(f"{GOOGLE_SHEET_RECON_TAB}!A:A", safe="!")
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
        log.warning(f"Reconciler Google Sheets append failed: {e}")
        return False


def _sheet_headers_exist(token: str) -> bool:
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{requests.utils.quote(GOOGLE_SHEET_RECON_TAB + '!1:1', safe='')}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        vals = resp.json().get("values", [])
        return bool(vals and any((str(x).strip() for x in vals[0])))
    except Exception as e:
        log.warning(f"Reconciler Google Sheets header check failed: {e}")
        return True


def _append_google_sheet_row(fieldnames: List[str], row: Dict):
    global _google_sheets_headers_ok
    if not GOOGLE_SHEETS_ENABLE:
        return
    token = _get_google_access_token()
    if not token:
        return
    if not _google_sheets_headers_ok:
        if not _sheet_headers_exist(token):
            _append_google_sheet_values([fieldnames], token)
        _google_sheets_headers_ok = True
    _append_google_sheet_values([[row.get(k) for k in fieldnames]], token)


def _buffer_multiplier(snapshot_quality: Optional[str]) -> float:
    label = (snapshot_quality or "").upper()
    if label == "HIGH":
        return 0.18
    if label == "MODERATE":
        return 0.12
    return 0.08


def _buffer_dollars(entry: Dict) -> Optional[float]:
    spot = entry.get("spot_at_prediction")
    em_1sd = entry.get("em_1sd")
    if spot is None or em_1sd is None:
        return None
    mult = _buffer_multiplier(entry.get("v4_confidence"))
    # Floor tuned for index products, but still sane for single names.
    floor = 0.75 if spot >= 100 else 0.35
    return round(max(floor, float(em_1sd) * mult), 2)


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

                eod_price = fetch_eod_close(ticker, pred_date)
                if eod_price is None:
                    log.debug(f"Reconciler: no EOD price for {ticker} on {pred_date}")
                    continue

                entry["eod_price"] = round(float(eod_price), 2)
                entry["reconciled"] = True
                entry["reconciled_at_utc"] = datetime.now(timezone.utc).isoformat()
                entry = _score_prediction(entry)

                ttl = redis_client.ttl(key)
                if ttl and ttl > 0:
                    store_set(key, json.dumps(entry), ttl=ttl)
                else:
                    store_set(key, json.dumps(entry), ttl=90 * 86400)

                reconciled.append(entry)
                _append_reconciliation_dataset(entry)
                log.info(
                    f"Reconciled: {key} | EOD=${eod_price:.2f} | raw={entry.get('direction_correct')} | "
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

    if not all(v is not None for v in [spot, eod, bull_1, bear_1, em_1sd]):
        entry["scoring_error"] = "missing fields"
        return entry

    spot = float(spot); eod = float(eod); em_1sd = float(em_1sd)
    actual_move = eod - spot
    abs_move = abs(actual_move)
    buf = _buffer_dollars(entry)

    entry["direction_buffer"] = buf
    entry["in_1_sigma"] = (float(bear_1) <= eod <= float(bull_1))
    entry["in_2_sigma"] = (float(bear_2) <= eod <= float(bull_2)) if bear_2 is not None and bull_2 is not None else None

    predicted_direction = "neutral"
    direction_correct = None
    buffered_correct = None
    primary_trigger = None
    primary_trigger_correct = None

    gamma_flip = entry.get("gamma_flip")
    call_wall = entry.get("call_wall")
    put_wall = entry.get("put_wall")
    max_pain = entry.get("max_pain")

    if bias_score >= 2:
        predicted_direction = "bull"
        direction_correct = actual_move > 0
        buffered_correct = eod >= (spot + buf) if buf is not None else direction_correct
        primary_trigger = gamma_flip if gamma_flip and float(gamma_flip) > spot else call_wall
        if primary_trigger is not None:
            primary_trigger_correct = eod >= float(primary_trigger)
    elif bias_score <= -2:
        predicted_direction = "bear"
        direction_correct = actual_move < 0
        buffered_correct = eod <= (spot - buf) if buf is not None else direction_correct
        primary_trigger = gamma_flip if gamma_flip and float(gamma_flip) < spot else put_wall
        if primary_trigger is not None:
            primary_trigger_correct = eod <= float(primary_trigger)
    else:
        pin_low = float(put_wall) if put_wall is not None else float(bear_1)
        pin_high = float(call_wall) if call_wall is not None else float(bull_1)
        neutral_correct = (pin_low <= eod <= pin_high)
        entry["pin_zone_low"] = pin_low
        entry["pin_zone_high"] = pin_high
        entry["neutral_correct"] = neutral_correct
        entry["condor_style_success"] = neutral_correct
        if max_pain is not None:
            mp_buf = max(0.5, (buf or 0.5))
            entry["max_pain_close_hit"] = abs(eod - float(max_pain)) <= mp_buf
        else:
            entry["max_pain_close_hit"] = None

    entry["predicted_direction"] = predicted_direction
    entry["direction_correct"] = direction_correct
    entry["buffered_direction_correct"] = buffered_correct
    entry["primary_trigger"] = primary_trigger
    entry["primary_trigger_correct"] = primary_trigger_correct
    entry["gamma_flip_touched"] = abs(eod - float(gamma_flip)) <= max(0.5, (buf or 0.5)) if gamma_flip is not None else None
    entry["max_pain_close_hit"] = entry.get("max_pain_close_hit") if predicted_direction == "neutral" else (
        abs(eod - float(max_pain)) <= max(0.5, (buf or 0.5)) if max_pain is not None else None
    )

    entry["move_actual"] = round(actual_move, 2)
    entry["move_predicted_1sd"] = round(em_1sd, 2)
    entry["move_ratio"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None
    entry["error_sigma"] = round(abs_move / em_1sd, 3) if em_1sd > 0 else None

    v4_bias = entry.get("v4_bias")
    if v4_bias:
        if v4_bias == "UPSIDE":
            entry["v4_direction_correct"] = actual_move > 0
            entry["v4_buffered_direction_correct"] = eod >= (spot + buf) if buf is not None else (actual_move > 0)
        elif v4_bias == "DOWNSIDE":
            entry["v4_direction_correct"] = actual_move < 0
            entry["v4_buffered_direction_correct"] = eod <= (spot - buf) if buf is not None else (actual_move < 0)
        else:
            entry["v4_direction_correct"] = None
            entry["v4_buffered_direction_correct"] = None

    return entry


def compute_accuracy_stats(entries: List[Dict]) -> Dict:
    reconciled = [e for e in entries if e.get("reconciled") and "in_1_sigma" in e]
    if not reconciled:
        return {"n": 0, "error": "no reconciled entries"}

    n = len(reconciled)
    in_1 = sum(1 for e in reconciled if e.get("in_1_sigma"))
    in_2 = sum(1 for e in reconciled if e.get("in_2_sigma"))
    ratios = [e["move_ratio"] for e in reconciled if e.get("move_ratio") is not None]

    dir_entries = [e for e in reconciled if e.get("direction_correct") is not None]
    dir_correct = sum(1 for e in dir_entries if e.get("direction_correct"))
    buf_entries = [e for e in reconciled if e.get("buffered_direction_correct") is not None]
    buf_correct = sum(1 for e in buf_entries if e.get("buffered_direction_correct"))

    v4_entries = [e for e in reconciled if e.get("v4_direction_correct") is not None]
    v4_correct = sum(1 for e in v4_entries if e.get("v4_direction_correct"))
    v4_buf_entries = [e for e in reconciled if e.get("v4_buffered_direction_correct") is not None]
    v4_buf_correct = sum(1 for e in v4_buf_entries if e.get("v4_buffered_direction_correct"))

    neutral_entries = [e for e in reconciled if e.get("predicted_direction") == "neutral"]
    neutral_correct = sum(1 for e in neutral_entries if e.get("neutral_correct"))
    condor_success = sum(1 for e in neutral_entries if e.get("condor_style_success"))
    pin_close_hits = sum(1 for e in neutral_entries if e.get("neutral_correct"))

    trigger_entries = [e for e in reconciled if e.get("primary_trigger_correct") is not None]
    trigger_hits = sum(1 for e in trigger_entries if e.get("primary_trigger_correct"))
    flip_entries = [e for e in reconciled if e.get("gamma_flip_touched") is not None]
    flip_hits = sum(1 for e in flip_entries if e.get("gamma_flip_touched"))
    max_pain_entries = [e for e in reconciled if e.get("max_pain_close_hit") is not None]
    max_pain_hits = sum(1 for e in max_pain_entries if e.get("max_pain_close_hit"))

    by_quality = {}
    for label in ("HIGH", "MODERATE", "LOW"):
        subset = [e for e in reconciled if e.get("v4_confidence") == label]
        if subset:
            raw_dir = [e for e in subset if e.get("direction_correct") is not None]
            buf_dir = [e for e in subset if e.get("buffered_direction_correct") is not None]
            by_quality[label] = {
                "n": len(subset),
                "raw_direction_pct": round(sum(1 for e in raw_dir if e.get("direction_correct")) / len(raw_dir) * 100, 1) if raw_dir else None,
                "buffered_direction_pct": round(sum(1 for e in buf_dir if e.get("buffered_direction_correct")) / len(buf_dir) * 100, 1) if buf_dir else None,
                "in_1_sigma_pct": round(sum(1 for e in subset if e.get("in_1_sigma")) / len(subset) * 100, 1),
            }

    by_ticker = {}
    tickers = sorted(set(e.get("ticker", "?") for e in reconciled))
    for t in tickers:
        subset = [e for e in reconciled if e.get("ticker") == t]
        raw_dir = [e for e in subset if e.get("direction_correct") is not None]
        buf_dir = [e for e in subset if e.get("buffered_direction_correct") is not None]
        neutral_sub = [e for e in subset if e.get("predicted_direction") == "neutral"]
        by_ticker[t] = {
            "n": len(subset),
            "raw_direction_pct": round(sum(1 for e in raw_dir if e.get("direction_correct")) / len(raw_dir) * 100, 1) if raw_dir else None,
            "buffered_direction_pct": round(sum(1 for e in buf_dir if e.get("buffered_direction_correct")) / len(buf_dir) * 100, 1) if buf_dir else None,
            "condor_pct": round(sum(1 for e in neutral_sub if e.get("condor_style_success")) / len(neutral_sub) * 100, 1) if neutral_sub else None,
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
        "v4_direction_n": len(v4_entries),
        "v4_direction_correct_pct": round(v4_correct / len(v4_entries) * 100, 1) if v4_entries else None,
        "v4_buffered_direction_n": len(v4_buf_entries),
        "v4_buffered_direction_correct_pct": round(v4_buf_correct / len(v4_buf_entries) * 100, 1) if v4_buf_entries else None,
        "neutral_n": len(neutral_entries),
        "neutral_correct_pct": round(neutral_correct / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "condor_success_pct": round(condor_success / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "pin_close_pct": round(pin_close_hits / len(neutral_entries) * 100, 1) if neutral_entries else None,
        "trigger_n": len(trigger_entries),
        "trigger_correct_pct": round(trigger_hits / len(trigger_entries) * 100, 1) if trigger_entries else None,
        "flip_touch_n": len(flip_entries),
        "flip_touch_pct": round(flip_hits / len(flip_entries) * 100, 1) if flip_entries else None,
        "max_pain_n": len(max_pain_entries),
        "max_pain_hit_pct": round(max_pain_hits / len(max_pain_entries) * 100, 1) if max_pain_entries else None,
        "avg_move_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "exceeded_1sd_pct": round(sum(1 for r in ratios if r > 1.0) / len(ratios) * 100, 1) if ratios else None,
        "by_confidence": by_quality,
        "by_ticker": by_ticker,
    }


def format_accuracy_report(stats: Dict) -> str:
    if stats.get("n", 0) == 0:
        return "📊 EM Scorecard: No reconciled predictions yet."

    n = stats["n"]
    lines = [
        f"📊 EM PREDICTION SCORECARD — {n} predictions",
        "═" * 32,
        "",
        "── Useful Direction ──",
        f"  Raw direction:      {stats['direction_correct_pct']:.1f}%  ({stats['direction_n']})" if stats.get("direction_n") else "  Raw direction:      n/a",
        f"  Buffered direction: {stats['buffered_direction_correct_pct']:.1f}%  ({stats['buffered_direction_n']})" if stats.get("buffered_direction_n") else "  Buffered direction: n/a",
        f"  v4 raw direction:   {stats['v4_direction_correct_pct']:.1f}%  ({stats['v4_direction_n']})" if stats.get("v4_direction_n") else "  v4 raw direction:   n/a",
        f"  v4 buffered:        {stats['v4_buffered_direction_correct_pct']:.1f}%  ({stats['v4_buffered_direction_n']})" if stats.get("v4_buffered_direction_n") else "  v4 buffered:        n/a",
    ]

    if stats.get("neutral_n"):
        lines += [
            "",
            "── Neutral / Range Outcome ──",
            f"  Neutral correct:    {stats['neutral_correct_pct']:.1f}%  ({stats['neutral_n']})",
            f"  Pin-zone close:     {stats['pin_close_pct']:.1f}%",
            f"  Condor success:     {stats['condor_success_pct']:.1f}%",
        ]

    if stats.get("trigger_n") or stats.get("flip_touch_n") or stats.get("max_pain_n"):
        lines += ["", "── Dealer Levels ──"]
        if stats.get("trigger_n"):
            lines.append(f"  Primary trigger:    {stats['trigger_correct_pct']:.1f}%")
        if stats.get("flip_touch_n"):
            lines.append(f"  Gamma flip touch:   {stats['flip_touch_pct']:.1f}%")
        if stats.get("max_pain_n"):
            lines.append(f"  Max pain close hit: {stats['max_pain_hit_pct']:.1f}%")

    lines += [
        "",
        "── IV Calibration ──",
        f"  1σ close-in-range:  {stats['in_1_sigma_pct']:.1f}%  (exp {stats['expected_1_sigma_pct']}%)",
        f"  2σ close-in-range:  {stats['in_2_sigma_pct']:.1f}%  (exp {stats['expected_2_sigma_pct']}%)",
        f"  Avg move ratio:     {stats['avg_move_ratio']:.2f}x" if stats.get("avg_move_ratio") is not None else "  Avg move ratio:     n/a",
    ]

    if stats.get("by_confidence"):
        lines += ["", "── By Snapshot Quality ──"]
        for label in ("HIGH", "MODERATE", "LOW"):
            if label in stats["by_confidence"]:
                s = stats["by_confidence"][label]
                raw = f"raw {s['raw_direction_pct']:.0f}%" if s.get("raw_direction_pct") is not None else "raw n/a"
                buf = f"buf {s['buffered_direction_pct']:.0f}%" if s.get("buffered_direction_pct") is not None else "buf n/a"
                lines.append(f"  {label}: {s['n']} cards | {raw} | {buf}")

    if stats.get("by_ticker"):
        lines += ["", "── By Ticker ──"]
        for t, s in stats["by_ticker"].items():
            parts = [f"{s['n']} cards"]
            if s.get("buffered_direction_pct") is not None:
                parts.append(f"buf {s['buffered_direction_pct']:.0f}%")
            if s.get("condor_pct") is not None:
                parts.append(f"condor {s['condor_pct']:.0f}%")
            lines.append(f"  {t}: " + " | ".join(parts))

    lines += ["", "— Not financial advice —"]
    return "\n".join(lines)


def _append_reconciliation_dataset(entry: Dict):
    row = {
        "reconciled_at_utc": entry.get("reconciled_at_utc"),
        "date": entry.get("date"),
        "session": entry.get("session"),
        "ticker": entry.get("ticker"),
        "spot_at_prediction": entry.get("spot_at_prediction"),
        "eod_price": entry.get("eod_price"),
        "predicted_direction": entry.get("predicted_direction"),
        "bias_score": entry.get("bias_score"),
        "v4_confidence": entry.get("v4_confidence"),
        "direction_buffer": entry.get("direction_buffer"),
        "direction_correct": entry.get("direction_correct"),
        "buffered_direction_correct": entry.get("buffered_direction_correct"),
        "neutral_correct": entry.get("neutral_correct"),
        "condor_style_success": entry.get("condor_style_success"),
        "primary_trigger": entry.get("primary_trigger"),
        "primary_trigger_correct": entry.get("primary_trigger_correct"),
        "gamma_flip": entry.get("gamma_flip"),
        "gamma_flip_touched": entry.get("gamma_flip_touched"),
        "max_pain": entry.get("max_pain"),
        "max_pain_close_hit": entry.get("max_pain_close_hit"),
        "em_1sd": entry.get("em_1sd"),
        "in_1_sigma": entry.get("in_1_sigma"),
        "in_2_sigma": entry.get("in_2_sigma"),
        "move_actual": entry.get("move_actual"),
        "move_ratio": entry.get("move_ratio"),
    }
    fieldnames = list(row.keys())
    _append_csv_row("em_reconciliation.csv", fieldnames, row)


def fetch_eod_close_marketdata(ticker: str, date_str: str, md_get: Callable) -> Optional[float]:
    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",
            {"from": date_str, "to": date_str, "countback": 1}
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return None
        closes = data.get("c", [])
        if closes and closes[-1] is not None:
            return float(closes[-1])
        return None
    except Exception as e:
        log.warning(f"EOD close fetch failed for {ticker} on {date_str}: {e}")
        return None
