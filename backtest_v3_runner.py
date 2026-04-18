#!/usr/bin/env python3
"""
backtest_v3_runner.py

ENRICHED backtest of Brad's Unified Signal v3.0 pinescript indicator.

Adds:
  1. Next-Friday hold rule — min 3 trading days; Thu/Fri signals roll to next Fri
  2. Parallel 2-week exit column
  3. Potter Box overlay (20-day range consolidation)
  4. Fib proximity (34-bar swing hi/lo levels)
  5. Swing H/L proximity (order-3 fractals)
  6. Timing cuts (hour-of-day, day-of-week, days-to-Friday)

Does NOT touch the live bot.

Usage (Render shell):
    cd /opt/render/project/src
    python backtest_v3_runner.py

Outputs to /tmp/backtest_v3/:
    trades.csv, summary_by_ticker.csv, summary_by_regime.csv,
    summary_by_timing.csv, summary_by_confluence.csv, report.md

Environment:
    MARKETDATA_TOKEN (required)
    BACKTEST_START (optional, default 2023-08-01)
    BACKTEST_END   (optional, default today)
    BACKTEST_TICKERS (optional, comma-separated override)
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

MD_TOKEN = os.environ.get("MARKETDATA_TOKEN", "").strip()

DEFAULT_TICKERS = [
    "AAPL", "AMD", "AMZN", "ARM", "AVGO", "BA", "CAT", "COIN", "CRM", "DIA",
    "GLD", "GOOGL", "GS", "IWM", "JPM", "LLY", "META", "MRNA", "MSFT", "MSTR",
    "NFLX", "NVDA", "ORCL", "PLTR", "QQQ", "SMCI", "SOFI", "SOXX", "SPY",
    "TLT", "TSLA", "UNH", "XLE", "XLF", "XLV",
]

# v3.0 pinescript parameters
EMA_FAST = 5; EMA_SLOW = 12; EMA_PCT_REQ = 5.0
MACD_FAST = 12; MACD_SLOW = 26; MACD_SIGNAL = 9; MACD_PCT_REQ = 10.0
WT_CHANNEL = 7; WT_AVG = 10; WT_OB1 = 60.0; WT_OS1 = -30.0
RSI_MFI_LEN = 72
STOCH_RSI_LEN = 14; STOCH_LEN = 14; STOCH_K = 3; STOCH_D = 3
HTF_EMA_FAST = 5; HTF_EMA_SLOW = 12
NEAR_BARS = 3; NO_ENTRY_MINS = 15
ADX_LEN = 14; ADX_THRESHOLD = 18.0
CQ_ZONE_PCT = 30.0; CQ_MIN_BODY_PCT = 25.0

SHORT_STRIKE_PCT = 0.01
LONG_STRIKE_PCT = 0.02
MIN_HOLD_DAYS = 3

POTTER_LOOKBACK_DAYS = 20
POTTER_MAX_RANGE_PCT = 8.0
POTTER_BREAKOUT_BUFFER = 0.3

FIB_LOOKBACK_DAYS = 34
FIB_LEVELS = [23.6, 38.2, 50.0, 61.8, 78.6]
SWING_FRACTAL_ORDER = 3

OUT_DIR = "/tmp/backtest_v3"
RATE_LIMIT_SECONDS = 0.3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("backtest")
NY = timezone(timedelta(hours=-5))

# ─────────────────────────────────────────────────────────────
# LIVE POTTER BOX ENGINE INTEGRATION
# ─────────────────────────────────────────────────────────────
# Use the actual bot's detect_boxes() from potter_box.py so backtest numbers
# match what the live engine would have computed on each date. We import
# from the repo's potter_box.py (expected on sys.path when run from the
# Render project root).
BOT_REPO_PATH = os.environ.get("BOT_REPO_PATH", "/opt/render/project/src")
if BOT_REPO_PATH not in sys.path:
    sys.path.insert(0, BOT_REPO_PATH)

_detect_boxes_live = None
try:
    from potter_box import detect_boxes as _detect_boxes_live  # type: ignore
    log.info(f"Loaded LIVE Potter Box engine from {BOT_REPO_PATH}/potter_box.py")
except Exception as e:
    log.warning(f"Could not import live potter_box.py ({e}). Set BOT_REPO_PATH "
                f"env var to the directory containing potter_box.py, or run from the "
                f"Render project root. Falling back to approximation.")


def _to_unix_ts(t):
    """Normalize any timestamp MarketData might return into unix integer.
    MD sometimes ignores dateformat=timestamp and returns ISO strings for daily
    candles. Handle int, float, and ISO date/datetime strings."""
    if isinstance(t, (int, float)):
        return int(t)
    if isinstance(t, str):
        s = t.strip()
        # Try ISO datetime first (e.g. "2023-08-01T00:00:00-04:00" or "2023-08-01T13:45:00Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("Z") else s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
        # datetime.fromisoformat handles lots of edge cases
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            pass
    raise ValueError(f"Cannot parse timestamp: {t!r}")


def fetch_candles(ticker, resolution, start, end):
    if not MD_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN not set")
    url = f"https://api.marketdata.app/v1/stocks/candles/{resolution}/{ticker.upper()}/"
    params = {"from": int(start.timestamp()), "to": int(end.timestamp()), "dateformat": "timestamp"}
    headers = {"Authorization": f"Bearer {MD_TOKEN}"}
    try:
        time.sleep(RATE_LIMIT_SECONDS)
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code not in (200, 203):
            log.warning(f"MD {ticker} {resolution}: HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        if data.get("s") != "ok":
            log.warning(f"MD {ticker} {resolution}: status={data.get('s')}")
            return None
        # Normalize timestamps — MD sometimes returns strings despite dateformat=timestamp
        if "t" in data:
            try:
                data["t"] = [_to_unix_ts(t) for t in data["t"]]
            except ValueError as e:
                log.warning(f"MD {ticker} {resolution}: timestamp parse error: {e}")
                return None
        return data
    except Exception as e:
        log.warning(f"MD fetch failed {ticker} {resolution}: {e}")
        return None


def fetch_15m_chunked(ticker, start, end):
    all_t, all_o, all_h, all_l, all_c, all_v = [], [], [], [], [], []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=90), end)
        data = fetch_candles(ticker, "15", cur, chunk_end)
        if data:
            all_t.extend(data.get("t", [])); all_o.extend(data.get("o", []))
            all_h.extend(data.get("h", [])); all_l.extend(data.get("l", []))
            all_c.extend(data.get("c", [])); all_v.extend(data.get("v", []))
        cur = chunk_end
    if not all_t:
        return None
    seen = set()
    out_t, out_o, out_h, out_l, out_c, out_v = [], [], [], [], [], []
    for i, t in enumerate(all_t):
        if t in seen:
            continue
        seen.add(t)
        out_t.append(t); out_o.append(all_o[i]); out_h.append(all_h[i])
        out_l.append(all_l[i]); out_c.append(all_c[i]); out_v.append(all_v[i])
    idx = sorted(range(len(out_t)), key=lambda i: out_t[i])
    return {
        "s": "ok",
        "t": [out_t[i] for i in idx],
        "o": [out_o[i] for i in idx],
        "h": [out_h[i] for i in idx],
        "l": [out_l[i] for i in idx],
        "c": [out_c[i] for i in idx],
        "v": [out_v[i] for i in idx],
    }


# ═══════ Indicators ═══════

def ema(values, length):
    if not values:
        return []
    out = [values[0]]
    k = 2.0 / (length + 1)
    for i in range(1, len(values)):
        out.append(values[i] * k + out[-1] * (1 - k))
    return out


def sma(values, length):
    out = []
    for i in range(len(values)):
        start = max(0, i - length + 1)
        window = values[start:i + 1]
        out.append(sum(window) / len(window))
    return out


def rma(values, length):
    if not values:
        return []
    alpha = 1.0 / length
    out = [values[0]]
    for i in range(1, len(values)):
        out.append(values[i] * alpha + out[-1] * (1 - alpha))
    return out


def rsi(values, length):
    n = len(values)
    if n == 0:
        return []
    gains = [0.0]; losses = [0.0]
    for i in range(1, n):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
    avg_g = rma(gains, length); avg_l = rma(losses, length)
    out = []
    for i in range(n):
        if avg_l[i] == 0:
            out.append(100.0 if avg_g[i] > 0 else 50.0)
        else:
            rs = avg_g[i] / avg_l[i]
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def macd(close, fast, slow, signal_len):
    ef = ema(close, fast); es = ema(close, slow)
    line = [ef[i] - es[i] for i in range(len(close))]
    sig = ema(line, signal_len)
    hist = [line[i] - sig[i] for i in range(len(close))]
    return line, sig, hist


def wave_trend(h, l, c, channel, avg):
    hlc3 = [(h[i] + l[i] + c[i]) / 3.0 for i in range(len(c))]
    esa = ema(hlc3, channel)
    d_abs = [abs(hlc3[i] - esa[i]) for i in range(len(c))]
    d_wt = ema(d_abs, channel)
    ci = []
    for i in range(len(c)):
        denom = 0.015 * d_wt[i]
        ci.append((hlc3[i] - esa[i]) / denom if denom != 0 else 0.0)
    wt1 = ema(ci, avg)
    wt2 = sma(wt1, 4)
    return wt1, wt2


def mfi_rsi_avg(h, l, c, v, length):
    n = len(c)
    hlc3 = [(h[i] + l[i] + c[i]) / 3.0 for i in range(n)]
    mfi_vals = []
    for i in range(n):
        up = 0.0; dn = 0.0
        start = max(0, i - length + 1)
        for j in range(start, i + 1):
            if j == 0:
                continue
            ch = hlc3[j] - hlc3[j - 1]
            val = v[j] * hlc3[j]
            if ch > 0:
                up += val
            elif ch < 0:
                dn += val
        if dn == 0:
            mfi_vals.append(100.0)
        else:
            ratio = up / dn
            mfi_vals.append(100.0 - 100.0 / (1.0 + ratio))
    rsi_vals = rsi(c, length)
    return [(rsi_vals[i] + mfi_vals[i]) / 2.0 for i in range(n)]


def stoch_rsi(c, rsi_len, stoch_len, sk, sd):
    rv = rsi(c, rsi_len)
    n = len(rv)
    raw = []
    for i in range(n):
        start = max(0, i - stoch_len + 1)
        w = rv[start:i + 1]
        lo = min(w); hi = max(w)
        if hi == lo:
            raw.append(50.0)
        else:
            raw.append((rv[i] - lo) / (hi - lo) * 100.0)
    k = sma(raw, sk); d = sma(k, sd)
    return k, d


def adx(h, l, c, length):
    n = len(c)
    if n < 2:
        return [0.0] * n
    dmp = [0.0]; dmn = [0.0]; tr = [h[0] - l[0]]
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        dmp.append(up if up > dn and up > 0 else 0.0)
        dmn.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    stt = rma(tr, length); sp = rma(dmp, length); sn = rma(dmn, length)
    dip = [100 * sp[i] / stt[i] if stt[i] != 0 else 0.0 for i in range(n)]
    din = [100 * sn[i] / stt[i] if stt[i] != 0 else 0.0 for i in range(n)]
    dx = []
    for i in range(n):
        s = dip[i] + din[i]
        dx.append(100 * abs(dip[i] - din[i]) / s if s != 0 else 0.0)
    return rma(dx, length)


def resample_to_hourly(bars_15m):
    if not bars_15m:
        return []
    buckets = {}
    for b in bars_15m:
        ts = b["t"]; hts = (ts // 3600) * 3600
        if hts not in buckets:
            buckets[hts] = {"t": hts, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
        else:
            x = buckets[hts]
            x["h"] = max(x["h"], b["h"]); x["l"] = min(x["l"], b["l"])
            x["c"] = b["c"]; x["v"] += b["v"]
    return sorted(buckets.values(), key=lambda x: x["t"])


def resample_to_daily(bars_15m):
    if not bars_15m:
        return []
    buckets = {}
    for b in bars_15m:
        dt = datetime.fromtimestamp(b["t"], tz=NY)
        dk = dt.strftime("%Y-%m-%d")
        if dk not in buckets:
            buckets[dk] = {
                "t": int(dt.replace(hour=0, minute=0, second=0).timestamp()),
                "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"], "date": dk,
            }
        else:
            x = buckets[dk]
            x["h"] = max(x["h"], b["h"]); x["l"] = min(x["l"], b["l"])
            x["c"] = b["c"]; x["v"] += b["v"]
    return sorted(buckets.values(), key=lambda x: x["t"])


# ═══════ Overlays ═══════

@dataclass
class PBState:
    state: str              # "in_box" / "above_roof" / "below_floor" / "no_box" / "post_box"
    floor: float            # Actual price from detected box (0 if no_box)
    roof: float             # Actual price from detected box (0 if no_box)
    range_pct: float        # Live engine range_pct field
    box_age_days: int       # duration_bars from live engine
    midpoint: float = 0.0   # (roof + floor) / 2 — the CB line
    max_touches: int = 0    # max(roof_touches, floor_touches)
    wave_label: str = "none"  # established / weakening / breakout_probable / breakout_imminent / none
    break_confirmed: bool = False


# Cache of detect_boxes results per ticker (key: ticker, value: list of box dicts)
_pb_box_cache: dict = {}


def _reset_pb_cache():
    """Call between tickers if memory becomes a concern."""
    global _pb_box_cache
    _pb_box_cache = {}


def _get_boxes_for_ticker(daily_bars, ticker):
    """Run detect_boxes ONCE per ticker against full daily bars, cache the result.
    Returns list of box dicts from the live engine."""
    cache_key = (ticker, len(daily_bars))
    if cache_key in _pb_box_cache:
        return _pb_box_cache[cache_key]

    if _detect_boxes_live is None:
        _pb_box_cache[cache_key] = []
        return []

    try:
        # Live engine expects bars with o/h/l/c/date fields — our daily_bars have all of these
        boxes = _detect_boxes_live(daily_bars, ticker)
    except Exception as e:
        log.warning(f"detect_boxes failed for {ticker}: {e}")
        boxes = []
    _pb_box_cache[cache_key] = boxes
    return boxes


def compute_potter_box(daily_bars, idx, ticker="UNKNOWN"):
    """Potter Box state at daily bar idx, using the LIVE bot engine.

    Behavior: at signal date (idx), find the most recent detected box whose
    start_idx <= idx. Classify:
      - in_box:      signal fires during active consolidation (start_idx ≤ idx ≤ end_idx)
      - above_roof:  box broke upward, signal fires after confirmed up break
      - below_floor: box broke downward, signal fires after confirmed down break
      - post_box:    box ended but no confirmed break yet (limbo)
      - no_box:      no box has ever started at or before this date
    """
    if idx < 0 or idx >= len(daily_bars):
        return PBState("no_box", 0, 0, 0.0, 0)

    boxes = _get_boxes_for_ticker(daily_bars, ticker)
    if not boxes:
        # No live engine, or no boxes detected in this ticker's history
        return PBState("no_box", 0, 0, 0.0, 0)

    # Find the most recent box that starts before or at signal date
    # Boxes are returned in chronological order by detect_boxes (increasing start_idx)
    relevant = [b for b in boxes if b.get("start_idx", -1) <= idx]
    if not relevant:
        return PBState("no_box", 0, 0, 0.0, 0)

    box = relevant[-1]  # most recent relevant box
    start_idx = box.get("start_idx", -1)
    end_idx = box.get("end_idx", -1)
    floor = float(box.get("floor", 0))
    roof = float(box.get("roof", 0))
    midpoint = float(box.get("midpoint", (roof + floor) / 2 if roof and floor else 0))
    range_pct = float(box.get("range_pct", 0))
    duration = int(box.get("duration_bars", 0))
    max_touches = int(box.get("max_touches", 0))
    wave_label = str(box.get("wave_label", "none"))
    broken = bool(box.get("broken", False))
    break_confirmed = bool(box.get("break_confirmed", False))
    break_direction = box.get("break_direction", None)

    # Classify state at signal date
    if start_idx <= idx <= end_idx:
        # Signal fired during active consolidation
        state = "in_box"
    elif broken and break_confirmed:
        # Box broke with ≥5% confirmed move. Signal fires post-breakout.
        if break_direction == "up":
            state = "above_roof"
        elif break_direction == "down":
            state = "below_floor"
        else:
            state = "post_box"
    else:
        # Box ended but no confirmed break. If signal is very close to end of box,
        # treat as "in_box" still (consolidation could still be resolving).
        bars_since_end = idx - end_idx
        if bars_since_end <= 5:
            state = "in_box"
        else:
            state = "post_box"

    return PBState(
        state=state, floor=floor, roof=roof, range_pct=range_pct,
        box_age_days=duration, midpoint=midpoint, max_touches=max_touches,
        wave_label=wave_label, break_confirmed=break_confirmed,
    )


@dataclass
class FibStateT:
    nearest_level: str
    nearest_price: float
    distance_pct: float
    swing_high: float
    swing_low: float
    above_or_below: str


def compute_fib_state(daily_bars, idx):
    if idx < FIB_LOOKBACK_DAYS:
        return FibStateT("none", 0, 100.0, 0, 0, "unknown")
    w = daily_bars[idx - FIB_LOOKBACK_DAYS + 1: idx + 1]
    sh = max(b["h"] for b in w); sl = min(b["l"] for b in w)
    spot = daily_bars[idx]["c"]
    if sh <= sl or spot <= 0:
        return FibStateT("none", 0, 100.0, sh, sl, "unknown")
    mid = (sh + sl) / 2
    if spot > mid:
        levels = [(lv, sh - (sh - sl) * (lv / 100.0)) for lv in FIB_LEVELS]
    else:
        levels = [(lv, sl + (sh - sl) * (lv / 100.0)) for lv in FIB_LEVELS]
    best_lv = None; best_p = 0.0; best_d = float("inf")
    for lv, p in levels:
        d = abs(spot - p) / spot * 100.0
        if d < best_d:
            best_d = d; best_lv = lv; best_p = p
    ab = "above" if spot > best_p else "below"
    return FibStateT(f"{best_lv}", best_p, best_d, sh, sl, ab)


@dataclass
class SwingStateT:
    nearest_above: float
    nearest_below: float
    distance_above_pct: float
    distance_below_pct: float


def find_swing_points(daily_bars, order=SWING_FRACTAL_ORDER):
    n = len(daily_bars)
    highs = []; lows = []
    for i in range(order, n - order):
        wh = [daily_bars[j]["h"] for j in range(i - order, i + order + 1)]
        wl = [daily_bars[j]["l"] for j in range(i - order, i + order + 1)]
        if daily_bars[i]["h"] == max(wh):
            highs.append((i, daily_bars[i]["h"]))
        if daily_bars[i]["l"] == min(wl):
            lows.append((i, daily_bars[i]["l"]))
    return highs, lows


def compute_swing_state(daily_bars, idx, highs, lows):
    spot = daily_bars[idx]["c"]
    if spot <= 0:
        return SwingStateT(0, 0, 0, 0)
    na = 0.0
    for (i, p) in highs:
        if i < idx and p > spot:
            if na == 0.0 or p < na:
                na = p
    nb = 0.0
    for (i, p) in lows:
        if i < idx and p < spot:
            if nb == 0.0 or p > nb:
                nb = p
    da = ((na - spot) / spot * 100.0) if na > 0 else 999.0
    db = ((spot - nb) / spot * 100.0) if nb > 0 else 999.0
    return SwingStateT(na, nb, da, db)


# ═══════ Signal logic ═══════

@dataclass
class SignalBar:
    idx: int
    ts: int
    dt_ny: datetime
    open: float
    high: float
    low: float
    close: float
    tier1_buy: bool = False
    tier2_buy: bool = False
    tier1_sell: bool = False
    tier2_sell: bool = False
    htf_bull_confirmed: bool = False
    htf_bear_confirmed: bool = False
    daily_bull: bool = False
    daily_bear: bool = False
    # Indicator values at signal bar (for quintile analysis)
    ind_ema_diff_pct: float = 0.0    # (ema_fast - ema_slow) / close * 100
    ind_macd_hist_pct: float = 0.0   # macd_hist / close * 100
    ind_rsi_mfi: float = 50.0        # RSI+MFI avg (0-100)
    ind_stoch_k: float = 50.0        # StochRSI K (0-100)
    ind_stoch_d: float = 50.0        # StochRSI D (0-100)
    ind_wt2: float = 0.0             # Wave Trend 2
    ind_rsi: float = 50.0            # plain RSI
    ind_adx: float = 0.0             # ADX reading
    # Candle strength at signal bar
    candle_body_pct: float = 0.0     # |close - open| / range * 100
    candle_close_pos_pct: float = 50.0  # close position in range (0=at low, 100=at high)


def compute_v3_signals(bars_15m):
    n = len(bars_15m)
    if n < 50:
        return []
    o = [b["o"] for b in bars_15m]; h = [b["h"] for b in bars_15m]
    l = [b["l"] for b in bars_15m]; c = [b["c"] for b in bars_15m]
    v = [b["v"] for b in bars_15m]

    ef = ema(c, EMA_FAST); es = ema(c, EMA_SLOW)
    edf = [ef[i] - es[i] for i in range(n)]
    ml, sl, hs = macd(c, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    wt1, wt2 = wave_trend(h, l, c, WT_CHANNEL, WT_AVG)
    rm = mfi_rsi_avg(h, l, c, v, RSI_MFI_LEN)
    sk, sd = stoch_rsi(c, STOCH_RSI_LEN, STOCH_LEN, STOCH_K, STOCH_D)
    # Plain RSI-14 for per-signal reporting (separate from the 72-period RSI+MFI)
    rsi14 = rsi(c, 14)

    # Session VWAP
    vwap_vals = [0.0] * n; cum_pv = 0.0; cum_v = 0.0; prev_day = None
    for i in range(n):
        dt = datetime.fromtimestamp(bars_15m[i]["t"], tz=NY)
        dk = dt.strftime("%Y-%m-%d")
        if dk != prev_day:
            cum_pv = 0.0; cum_v = 0.0; prev_day = dk
        typ = (h[i] + l[i] + c[i]) / 3.0
        cum_pv += typ * v[i]; cum_v += v[i]
        vwap_vals[i] = cum_pv / cum_v if cum_v > 0 else typ

    adx_v = adx(h, l, c, ADX_LEN)

    # HTF
    hourly = resample_to_hourly(bars_15m)
    if len(hourly) >= HTF_EMA_SLOW:
        hc = [b["c"] for b in hourly]
        hef = ema(hc, HTF_EMA_FAST); hes = ema(hc, HTF_EMA_SLOW)
        hf = [0.0] * n; hs_arr = [0.0] * n; hdp = [0.0] * n
        hp = 0
        for i in range(n):
            ts = bars_15m[i]["t"]
            while hp + 1 < len(hourly) and hourly[hp + 1]["t"] <= ts:
                hp += 1
            hf[i] = hef[hp]; hs_arr[i] = hes[hp]
            pi = max(0, hp - 1)
            hdp[i] = hef[pi] - hes[pi]
    else:
        hf = list(ef); hs_arr = list(es); hdp = [0.0] * n

    daily = resample_to_daily(bars_15m)
    if len(daily) >= HTF_EMA_SLOW:
        dc = [b["c"] for b in daily]
        def_fast = ema(dc, HTF_EMA_FAST); def_slow = ema(dc, HTF_EMA_SLOW)
        dbf = [0.0] * n; dbs = [0.0] * n
        dmap = {daily[k]["date"]: k for k in range(len(daily))}
        for i in range(n):
            dt = datetime.fromtimestamp(bars_15m[i]["t"], tz=NY)
            dk = dt.strftime("%Y-%m-%d")
            di = dmap.get(dk, 0); pi = max(0, di - 1)
            dbf[i] = def_fast[pi]; dbs[i] = def_slow[pi]
    else:
        dbf = [0.0] * n; dbs = [0.0] * n

    def session_ok(ts):
        dt = datetime.fromtimestamp(ts, tz=NY)
        if dt.weekday() >= 5:
            return False
        open_t = dt.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        if dt < open_t + timedelta(minutes=NO_ENTRY_MINS):
            return False
        if dt > close_t:
            return False
        return True

    eps = 1e-10
    ema_cb = [False] * n; ema_cs = [False] * n
    for i in range(1, n):
        aed = abs(edf[i]); aep = max(abs(edf[i - 1]), eps)
        grow = (100.0 + EMA_PCT_REQ) / 100.0; shr = (100.0 - EMA_PCT_REQ) / 100.0
        ba = (ef[i] > es[i]) and (edf[i] >= edf[i - 1] * grow)
        bb = (ef[i] < es[i]) and (aed <= aep * shr)
        ema_cb[i] = ba or bb
        sb = (ef[i] < es[i]) and (aed >= aep * grow)
        sa = (ef[i] > es[i]) and (aed <= aep * shr)
        ema_cs[i] = sb or sa

    macd_cb = [False] * n; macd_cs = [False] * n
    for i in range(1, n):
        ah = abs(hs[i]); ahp = max(abs(hs[i - 1]), eps)
        closer = (100.0 - MACD_PCT_REQ) / 100.0; farther = (100.0 + MACD_PCT_REQ) / 100.0
        bb = (ml[i] < sl[i]) and (ah <= ahp * closer)
        ba = (ml[i] > sl[i]) and (ah >= ahp * farther)
        macd_cb[i] = bb or ba
        sa = (ml[i] > sl[i]) and (ah <= ahp * closer)
        sb2 = (ml[i] < sl[i]) and (ah >= ahp * farther)
        macd_cs[i] = sa or sb2

    db = [ema_cb[i] and macd_cb[i] for i in range(n)]
    ds = [ema_cs[i] and macd_cs[i] for i in range(n)]
    wt_up = [False] * n; wt_dn = [False] * n
    for i in range(1, n):
        wt_up[i] = (wt1[i - 1] <= wt2[i - 1]) and (wt1[i] > wt2[i])
        wt_dn[i] = (wt1[i - 1] >= wt2[i - 1]) and (wt1[i] < wt2[i])

    mb = [rm[i] >= 50 for i in range(n)]
    mbr = [rm[i] < 50 for i in range(n)]
    wos = [wt2[i] <= WT_OS1 for i in range(n)]
    wob = [wt2[i] >= WT_OB1 for i in range(n)]
    av = [c[i] > vwap_vals[i] for i in range(n)]
    bv = [c[i] < vwap_vals[i] for i in range(n)]

    sbu = [False] * n; sbe = [False] * n
    for i in range(1, n):
        sbu[i] = (sk[i - 1] <= sd[i - 1]) and (sk[i] > sd[i])
        sbe[i] = (sk[i - 1] >= sd[i - 1]) and (sk[i] < sd[i])

    hd = [hf[i] - hs_arr[i] for i in range(n)]
    hbc = [hf[i] > hs_arr[i] for i in range(n)]
    hbrc = [hf[i] < hs_arr[i] for i in range(n)]
    hbcv = [(hf[i] < hs_arr[i]) and (abs(hd[i]) < abs(hdp[i])) for i in range(n)]
    hbrcv = [(hf[i] > hs_arr[i]) and (abs(hd[i]) < abs(hdp[i])) for i in range(n)]
    hbok = [hbc[i] or hbcv[i] for i in range(n)]
    hbrok = [hbrc[i] or hbrcv[i] for i in range(n)]

    db_d = [dbf[i] > dbs[i] for i in range(n)]
    dbr_d = [dbf[i] < dbs[i] for i in range(n)]
    adx_ok = [adx_v[i] >= ADX_THRESHOLD for i in range(n)]

    cq_b = [False] * n; cq_s = [False] * n
    for i in range(n):
        rng = max(h[i] - l[i], 1e-6)
        body = abs(c[i] - o[i]); bpc = body / rng * 100.0
        chp = (c[i] - l[i]) / rng * 100.0; clp = (h[i] - c[i]) / rng * 100.0
        cq_b[i] = (chp >= (100.0 - CQ_ZONE_PCT)) and (bpc >= CQ_MIN_BODY_PCT)
        cq_s[i] = (clp >= (100.0 - CQ_ZONE_PCT)) and (bpc >= CQ_MIN_BODY_PCT)

    ldb = -10000; lwb = -10000; lds = -10000; lws = -10000
    results = []
    for i in range(n):
        if db[i]:
            ldb = i
        if wt_up[i]:
            lwb = i
        if ds[i]:
            lds = i
        if wt_dn[i]:
            lws = i
        bn = abs(ldb - lwb) <= NEAR_BARS and (ldb == i or lwb == i)
        sn = abs(lds - lws) <= NEAR_BARS and (lds == i or lws == i)
        sok = session_ok(bars_15m[i]["t"])
        bco = mb[i] or av[i] or wos[i] or sbu[i]
        bec = mbr[i] or bv[i] or wob[i] or sbe[i]
        t1b = bn and hbok[i] and sok and adx_ok[i] and cq_b[i]
        t1s = sn and hbrok[i] and sok and adx_ok[i] and cq_s[i]
        t2b = db[i] and hbok[i] and sok and adx_ok[i] and bco and not t1b
        t2s = ds[i] and hbrok[i] and sok and adx_ok[i] and bec and not t1s

        dt_ny = datetime.fromtimestamp(bars_15m[i]["t"], tz=NY)

        # Candle strength at signal bar
        rng_i = max(h[i] - l[i], 1e-9)
        body = abs(c[i] - o[i])
        body_pct = body / rng_i * 100.0
        # Close position in range — for bulls we want close near high (high position
        # value). For bears we want close near low. Store the raw position; summary
        # code can interpret per-direction.
        close_pos_pct = (c[i] - l[i]) / rng_i * 100.0

        # Indicator values at signal bar
        close_safe = max(c[i], 1e-9)
        ema_diff_pct = (ef[i] - es[i]) / close_safe * 100.0
        macd_hist_pct = hs[i] / close_safe * 100.0

        results.append(SignalBar(
            idx=i, ts=bars_15m[i]["t"], dt_ny=dt_ny,
            open=o[i], high=h[i], low=l[i], close=c[i],
            tier1_buy=t1b, tier2_buy=t2b, tier1_sell=t1s, tier2_sell=t2s,
            htf_bull_confirmed=hbc[i], htf_bear_confirmed=hbrc[i],
            daily_bull=db_d[i], daily_bear=dbr_d[i],
            ind_ema_diff_pct=ema_diff_pct,
            ind_macd_hist_pct=macd_hist_pct,
            ind_rsi_mfi=rm[i],
            ind_stoch_k=sk[i],
            ind_stoch_d=sd[i],
            ind_wt2=wt2[i],
            ind_rsi=rsi14[i],
            ind_adx=adx_v[i],
            candle_body_pct=body_pct,
            candle_close_pos_pct=close_pos_pct,
        ))
    return results


# ═══════ Trade model ═══════

@dataclass
class Trade:
    ticker: str
    tier: int
    direction: str
    signal_ts: int
    signal_dt_ny: str
    entry_ts: int
    entry_dt_ny: str
    entry_price: float
    short_strike: float
    long_strike: float
    exit_ts: int
    exit_dt_ny: str
    exit_price: float
    move_pct: float
    move_signed_pct: float
    mae_pct: float
    mfe_pct: float
    hold_days: float
    bucket: str
    win_headline: bool
    exit_2w_ts: int
    exit_2w_price: float
    move_2w_signed_pct: float
    bucket_2w: str
    win_2w_headline: bool
    htf_aligned: bool
    daily_aligned: bool
    regime_trend: str
    regime_vol: str
    signal_hour_et: int
    signal_dow: str
    days_to_friday: int
    pb_state: str
    pb_range_pct: float
    pb_box_age: int
    # Live-engine Potter Box extras
    pb_floor: float = 0.0
    pb_roof: float = 0.0
    pb_midpoint: float = 0.0          # CB line
    pb_wave_label: str = "none"
    pb_max_touches: int = 0
    pb_break_confirmed: bool = False
    # CB line analysis at signal (only meaningful when in_box)
    cb_side: str = "n/a"              # "above_cb" / "below_cb" / "at_cb" / "n/a"
    cb_distance_pct: float = 0.0      # abs(spot - midpoint) / spot * 100
    # Candle strength at signal bar (15m)
    candle_body_pct: float = 0.0
    candle_close_pos_pct: float = 50.0
    # Indicator values at signal bar
    ind_ema_diff_pct: float = 0.0
    ind_macd_hist_pct: float = 0.0
    ind_rsi_mfi: float = 50.0
    ind_stoch_k: float = 50.0
    ind_stoch_d: float = 50.0
    ind_wt2: float = 0.0
    ind_rsi: float = 50.0
    ind_adx: float = 0.0
    fib_level: str = "none"
    fib_distance_pct: float = 0.0
    fib_spot_above: str = "unknown"
    swing_dist_above_pct: float = 0.0
    swing_dist_below_pct: float = 0.0
    confluence_bucket: str = "none"
    # Credit spread outcomes (only populated when in_box; otherwise 'n/a')
    # Bull signal -> bull put credit at floor. Bear signal -> bear call credit at roof.
    credit_short_strike: float = 0.0
    credit_25_long_strike: float = 0.0     # $2.50 width
    credit_50_long_strike: float = 0.0     # $5.00 width
    # Primary Friday exit
    credit_25_bucket: str = "n/a"
    credit_25_win: bool = False
    credit_50_bucket: str = "n/a"
    credit_50_win: bool = False
    # 2-week Friday exit
    credit_25_2w_bucket: str = "n/a"
    credit_25_2w_win: bool = False
    credit_50_2w_bucket: str = "n/a"
    credit_50_2w_win: bool = False


def find_exit_bar(bars, entry_idx, min_trading_days=MIN_HOLD_DAYS):
    entry_dt = bars[entry_idx].dt_ny
    days_added = 0
    target = entry_dt
    while days_added < min_trading_days:
        target = target + timedelta(days=1)
        if target.weekday() < 5:
            days_added += 1
    while target.weekday() != 4:
        target = target + timedelta(days=1)
    td = target.date()
    last = None
    for j in range(entry_idx, len(bars)):
        bdt = bars[j].dt_ny
        if bdt.date() < td:
            continue
        if bdt.date() > td:
            break
        if bdt.date() == td and bdt.hour < 16:
            last = j
    return last


def find_2w_exit_bar(bars, entry_idx):
    entry_dt = bars[entry_idx].dt_ny
    days_added = 0
    target = entry_dt
    while days_added < 10:
        target = target + timedelta(days=1)
        if target.weekday() < 5:
            days_added += 1
    while target.weekday() != 4:
        target = target + timedelta(days=1)
    td = target.date()
    last = None
    for j in range(entry_idx, len(bars)):
        bdt = bars[j].dt_ny
        if bdt.date() < td:
            continue
        if bdt.date() > td:
            break
        if bdt.date() == td and bdt.hour < 16:
            last = j
    return last


def trading_days_to_friday(dt):
    wd = dt.weekday()
    if wd <= 4:
        return 4 - wd
    return 0


def grade(signed):
    if signed >= -1.0:
        return "full_win", True
    elif signed >= -2.0:
        return "partial", False
    else:
        return "full_loss", False


def grade_credit(direction: str, short_strike: float, long_strike: float, exit_price: float):
    """Grade a credit spread at exit.

    direction='bull' -> bull put credit: short_strike > long_strike.
      Full win: exit >= short (OTM) — premium retained.
      Partial: long < exit < short — between strikes, partial loss.
      Full loss: exit <= long — max loss (width - premium).

    direction='bear' -> bear call credit: short_strike < long_strike.
      Full win: exit <= short.
      Partial: short < exit < long.
      Full loss: exit >= long.
    """
    if short_strike <= 0 or long_strike <= 0:
        return "n/a", False
    if direction == "bull":
        if exit_price >= short_strike:
            return "full_win", True
        elif exit_price > long_strike:
            return "partial", False
        else:
            return "full_loss", False
    else:  # bear
        if exit_price <= short_strike:
            return "full_win", True
        elif exit_price < long_strike:
            return "partial", False
        else:
            return "full_loss", False


def simulate_trades(ticker, bars, daily_bars, regime_map):
    trades = []
    highs, lows = find_swing_points(daily_bars)
    day_map = {daily_bars[k]["date"]: k for k in range(len(daily_bars))}

    for i, sb in enumerate(bars):
        if i + 1 >= len(bars):
            continue
        tier = 0; direction = None
        if sb.tier1_buy:
            tier, direction = 1, "bull"
        elif sb.tier2_buy:
            tier, direction = 2, "bull"
        elif sb.tier1_sell:
            tier, direction = 1, "bear"
        elif sb.tier2_sell:
            tier, direction = 2, "bear"
        else:
            continue

        entry_idx = i + 1
        entry_bar = bars[entry_idx]
        entry_price = entry_bar.open
        if direction == "bull":
            ss = entry_price * (1.0 - SHORT_STRIKE_PCT)
            ls = entry_price * (1.0 - LONG_STRIKE_PCT)
        else:
            ss = entry_price * (1.0 + SHORT_STRIKE_PCT)
            ls = entry_price * (1.0 + LONG_STRIKE_PCT)

        exit_idx = find_exit_bar(bars, entry_idx, MIN_HOLD_DAYS)
        if exit_idx is None:
            continue
        exit_bar = bars[exit_idx]
        exit_price = exit_bar.close

        if direction == "bull":
            mfe_hi = max(bars[j].high for j in range(entry_idx, exit_idx + 1))
            mae_lo = min(bars[j].low for j in range(entry_idx, exit_idx + 1))
            mfe_pct = (mfe_hi - entry_price) / entry_price * 100.0
            mae_pct = (mae_lo - entry_price) / entry_price * 100.0
        else:
            mfe_lo = min(bars[j].low for j in range(entry_idx, exit_idx + 1))
            mae_hi = max(bars[j].high for j in range(entry_idx, exit_idx + 1))
            mfe_pct = (entry_price - mfe_lo) / entry_price * 100.0
            mae_pct = (entry_price - mae_hi) / entry_price * 100.0

        move = (exit_price - entry_price) / entry_price * 100.0
        signed = move if direction == "bull" else -move
        bucket, win = grade(signed)

        exit_2w_idx = find_2w_exit_bar(bars, entry_idx)
        if exit_2w_idx is not None:
            e2 = bars[exit_2w_idx]
            e2p = e2.close
            m2 = (e2p - entry_price) / entry_price * 100.0
            s2 = m2 if direction == "bull" else -m2
            b2, w2 = grade(s2)
            e2_ts = e2.ts
        else:
            e2_ts = 0; e2p = 0.0; s2 = 0.0; b2 = "truncated"; w2 = False

        hold_days = (exit_bar.dt_ny - entry_bar.dt_ny).total_seconds() / 86400.0

        edk = entry_bar.dt_ny.strftime("%Y-%m-%d")
        rr = regime_map.get(edk, {})
        rt = rr.get("trend", "UNKNOWN"); rv = rr.get("vol", "UNKNOWN")

        shr = sb.dt_ny.hour
        dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][sb.dt_ny.weekday()]
        d2f = trading_days_to_friday(sb.dt_ny)

        sdk = sb.dt_ny.strftime("%Y-%m-%d")
        tidx = day_map.get(sdk, 0)
        pidx = max(0, tidx - 1)
        pb = compute_potter_box(daily_bars, pidx, ticker=ticker)
        fb = compute_fib_state(daily_bars, pidx)
        sw = compute_swing_state(daily_bars, pidx, highs, lows)

        # CB line analysis (the cost-basis midpoint of the Potter Box)
        cb_side = "n/a"
        cb_dist_pct = 0.0
        if pb.state == "in_box" and pb.midpoint > 0:
            spot_at_signal = sb.close
            if spot_at_signal > pb.midpoint:
                cb_side = "above_cb"
            elif spot_at_signal < pb.midpoint:
                cb_side = "below_cb"
            else:
                cb_side = "at_cb"
            if spot_at_signal > 0:
                cb_dist_pct = abs(spot_at_signal - pb.midpoint) / spot_at_signal * 100.0

        # Credit spread simulation — only when in_box (we need a boundary to sell at)
        cs_short = 0.0; cs_long_25 = 0.0; cs_long_50 = 0.0
        cs_25_b = "n/a"; cs_25_w = False; cs_50_b = "n/a"; cs_50_w = False
        cs_25_b_2w = "n/a"; cs_25_w_2w = False; cs_50_b_2w = "n/a"; cs_50_w_2w = False
        if pb.state == "in_box" and pb.floor > 0 and pb.roof > 0:
            if direction == "bull":
                # Bull put credit: sell put at floor, buy put $W below
                cs_short = pb.floor
                cs_long_25 = pb.floor - 2.50
                cs_long_50 = pb.floor - 5.00
            else:
                # Bear call credit: sell call at roof, buy call $W above
                cs_short = pb.roof
                cs_long_25 = pb.roof + 2.50
                cs_long_50 = pb.roof + 5.00
            # Primary Friday exit
            cs_25_b, cs_25_w = grade_credit(direction, cs_short, cs_long_25, exit_price)
            cs_50_b, cs_50_w = grade_credit(direction, cs_short, cs_long_50, exit_price)
            # 2-week Friday exit
            if exit_2w_idx is not None:
                cs_25_b_2w, cs_25_w_2w = grade_credit(direction, cs_short, cs_long_25, e2p)
                cs_50_b_2w, cs_50_w_2w = grade_credit(direction, cs_short, cs_long_50, e2p)

        conf = "none"
        if direction == "bull" and pb.state == "above_roof":
            conf = "pb_aligned_bull"
        elif direction == "bear" and pb.state == "below_floor":
            conf = "pb_aligned_bear"
        elif direction == "bull" and pb.state == "below_floor":
            conf = "pb_opposed"
        elif direction == "bear" and pb.state == "above_roof":
            conf = "pb_opposed"
        elif pb.state == "in_box":
            conf = "pb_in_box"
        elif pb.state == "no_box":
            conf = "pb_no_box"

        htf_a = (direction == "bull" and sb.htf_bull_confirmed) or (direction == "bear" and sb.htf_bear_confirmed)
        da_a = (direction == "bull" and sb.daily_bull) or (direction == "bear" and sb.daily_bear)

        trades.append(Trade(
            ticker=ticker, tier=tier, direction=direction,
            signal_ts=sb.ts, signal_dt_ny=sb.dt_ny.isoformat(),
            entry_ts=entry_bar.ts, entry_dt_ny=entry_bar.dt_ny.isoformat(),
            entry_price=entry_price, short_strike=ss, long_strike=ls,
            exit_ts=exit_bar.ts, exit_dt_ny=exit_bar.dt_ny.isoformat(),
            exit_price=exit_price, move_pct=move, move_signed_pct=signed,
            mae_pct=mae_pct, mfe_pct=mfe_pct, hold_days=hold_days,
            bucket=bucket, win_headline=win,
            exit_2w_ts=e2_ts, exit_2w_price=e2p,
            move_2w_signed_pct=s2, bucket_2w=b2, win_2w_headline=w2,
            htf_aligned=htf_a, daily_aligned=da_a,
            regime_trend=rt, regime_vol=rv,
            signal_hour_et=shr, signal_dow=dow, days_to_friday=d2f,
            pb_state=pb.state, pb_range_pct=pb.range_pct, pb_box_age=pb.box_age_days,
            pb_floor=pb.floor, pb_roof=pb.roof, pb_midpoint=pb.midpoint,
            pb_wave_label=pb.wave_label, pb_max_touches=pb.max_touches,
            pb_break_confirmed=pb.break_confirmed,
            cb_side=cb_side, cb_distance_pct=cb_dist_pct,
            candle_body_pct=sb.candle_body_pct,
            candle_close_pos_pct=sb.candle_close_pos_pct,
            ind_ema_diff_pct=sb.ind_ema_diff_pct,
            ind_macd_hist_pct=sb.ind_macd_hist_pct,
            ind_rsi_mfi=sb.ind_rsi_mfi,
            ind_stoch_k=sb.ind_stoch_k,
            ind_stoch_d=sb.ind_stoch_d,
            ind_wt2=sb.ind_wt2,
            ind_rsi=sb.ind_rsi,
            ind_adx=sb.ind_adx,
            fib_level=fb.nearest_level, fib_distance_pct=fb.distance_pct, fib_spot_above=fb.above_or_below,
            swing_dist_above_pct=sw.distance_above_pct, swing_dist_below_pct=sw.distance_below_pct,
            confluence_bucket=conf,
            credit_short_strike=cs_short,
            credit_25_long_strike=cs_long_25, credit_50_long_strike=cs_long_50,
            credit_25_bucket=cs_25_b, credit_25_win=cs_25_w,
            credit_50_bucket=cs_50_b, credit_50_win=cs_50_w,
            credit_25_2w_bucket=cs_25_b_2w, credit_25_2w_win=cs_25_w_2w,
            credit_50_2w_bucket=cs_50_b_2w, credit_50_2w_win=cs_50_w_2w,
        ))
    return trades


# ═══════ Regime ═══════

def build_regime_map(start, end):
    log.info("Fetching SPY + VIX daily...")
    spy = fetch_candles("SPY", "D", start - timedelta(days=250), end)
    vix = None
    try:
        url = "https://api.marketdata.app/v1/indices/candles/D/VIX/"
        params = {"from": int((start - timedelta(days=250)).timestamp()),
                  "to": int(end.timestamp()), "dateformat": "timestamp"}
        headers = {"Authorization": f"Bearer {MD_TOKEN}"}
        time.sleep(RATE_LIMIT_SECONDS)
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code in (200, 203):
            d = r.json()
            if d.get("s") == "ok":
                # Normalize VIX timestamps to unix ints
                if "t" in d:
                    try:
                        d["t"] = [_to_unix_ts(t) for t in d["t"]]
                    except ValueError as e:
                        log.warning(f"VIX timestamp parse error: {e}")
                        d = None
                vix = d
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")

    if not spy:
        log.warning("No SPY data; regimes UNKNOWN")
        return {}

    st = spy["t"]; sc = spy["c"]
    sm = sma(sc, 200)
    vix_by = {}
    if vix:
        for i, t in enumerate(vix["t"]):
            d = datetime.fromtimestamp(t, tz=NY).strftime("%Y-%m-%d")
            vix_by[d] = vix["c"][i]

    rm = {}
    for i, t in enumerate(st):
        d = datetime.fromtimestamp(t, tz=NY).strftime("%Y-%m-%d")
        trend = "BULL" if sc[i] > sm[i] else "BEAR"
        vv = vix_by.get(d)
        if vv is None:
            vol = "UNKNOWN"
        elif vv < 15:
            vol = "LOW"
        elif vv < 20:
            vol = "NORMAL"
        elif vv < 30:
            vol = "ELEVATED"
        else:
            vol = "CRISIS"
        rm[d] = {"trend": trend, "vol": vol, "spy": sc[i], "vix": vv}
    return rm


# ═══════ Output ═══════

def write_trades_csv(trades, path):
    fields = list(Trade.__dataclass_fields__.keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def _stats(subset):
    if not subset:
        return (0, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0.0)
    n = len(subset)
    fw = sum(1 for t in subset if t.bucket == "full_win")
    fp = sum(1 for t in subset if t.bucket == "partial")
    fl = sum(1 for t in subset if t.bucket == "full_loss")
    am = sum(t.move_signed_pct for t in subset) / n
    amae = sum(t.mae_pct for t in subset) / n
    amfe = sum(t.mfe_pct for t in subset) / n
    fw2 = sum(1 for t in subset if t.bucket_2w == "full_win")
    n2 = sum(1 for t in subset if t.bucket_2w != "truncated")
    am2 = (sum(t.move_2w_signed_pct for t in subset if t.bucket_2w != "truncated") / n2) if n2 > 0 else 0.0
    return (n, fw, fp, fl, am, amae, amfe, fw2, am2)


def write_summary_by_ticker(trades, path):
    buckets = defaultdict(list)
    for t in trades:
        buckets[(t.ticker, t.tier, t.direction)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "tier", "direction", "n_trades", "full_win", "partial", "full_loss",
                    "headline_wr_pct", "win_or_partial_pct", "avg_move_signed_pct",
                    "avg_mae_pct", "avg_mfe_pct", "wr_2w_pct", "avg_move_2w_pct"])
        for (tk, ti, di), ts in sorted(buckets.items()):
            n, fw, fp, fl, am, amae, amfe, fw2, am2 = _stats(ts)
            if n == 0:
                continue
            w.writerow([tk, ti, di, n, fw, fp, fl,
                        f"{100*fw/n:.1f}", f"{100*(fw+fp)/n:.1f}",
                        f"{am:+.2f}", f"{amae:+.2f}", f"{amfe:+.2f}",
                        f"{100*fw2/n:.1f}", f"{am2:+.2f}"])


def write_summary_by_regime(trades, path):
    buckets = defaultdict(list)
    for t in trades:
        buckets[(t.regime_trend, t.regime_vol, t.tier, t.direction)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["regime_trend", "regime_vol", "tier", "direction", "n_trades",
                    "full_win", "partial", "full_loss",
                    "headline_wr_pct", "win_or_partial_pct",
                    "avg_move_signed_pct", "wr_2w_pct"])
        for k, ts in sorted(buckets.items()):
            n, fw, fp, fl, am, _, _, fw2, _ = _stats(ts)
            if n == 0:
                continue
            w.writerow([k[0], k[1], k[2], k[3], n, fw, fp, fl,
                        f"{100*fw/n:.1f}", f"{100*(fw+fp)/n:.1f}",
                        f"{am:+.2f}", f"{100*fw2/n:.1f}"])


def write_summary_by_timing(trades, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dimension", "value", "tier", "direction", "n_trades",
                    "full_win", "partial", "full_loss",
                    "headline_wr_pct", "win_or_partial_pct"])
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_hour = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction:
                        by_hour[t.signal_hour_et].append(t)
                for hr in sorted(by_hour.keys()):
                    ts = by_hour[hr]
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    w.writerow(["hour_of_day", f"{hr:02d}:00", tier, direction, n, fw, fp, fl,
                                f"{100*fw/n:.1f}", f"{100*(fw+fp)/n:.1f}"])
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_dow = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction:
                        by_dow[t.signal_dow].append(t)
                for dow in ("Mon", "Tue", "Wed", "Thu", "Fri"):
                    ts = by_dow.get(dow, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    w.writerow(["day_of_week", dow, tier, direction, n, fw, fp, fl,
                                f"{100*fw/n:.1f}", f"{100*(fw+fp)/n:.1f}"])
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_d2f = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction:
                        by_d2f[t.days_to_friday].append(t)
                for d2f in sorted(by_d2f.keys()):
                    ts = by_d2f[d2f]
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    w.writerow(["days_to_friday", str(d2f), tier, direction, n, fw, fp, fl,
                                f"{100*fw/n:.1f}", f"{100*(fw+fp)/n:.1f}"])


def write_summary_by_confluence(trades, path):
    baselines = {}
    for tier in (1, 2):
        for direction in ("bull", "bear"):
            s = [t for t in trades if t.tier == tier and t.direction == direction]
            n, fw, _, _, _, _, _, _, _ = _stats(s)
            baselines[(tier, direction)] = (100 * fw / n) if n > 0 else 0.0

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dimension", "value", "tier", "direction", "n_trades",
                    "full_win", "partial", "full_loss",
                    "headline_wr_pct", "win_or_partial_pct", "vs_baseline_wr_pct"])

        # Potter Box
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_pb = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction:
                        by_pb[t.pb_state].append(t)
                for state in ("above_roof", "below_floor", "in_box", "no_box"):
                    ts = by_pb.get(state, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    dlt = wr - baselines[(tier, direction)]
                    w.writerow(["potter_box", state, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}"])

        # Fib (near level, within 1%)
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_fib = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction and t.fib_distance_pct <= 1.0:
                        by_fib[t.fib_level].append(t)
                for lvl in ("23.6", "38.2", "50.0", "61.8", "78.6"):
                    ts = by_fib.get(lvl, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    dlt = wr - baselines[(tier, direction)]
                    w.writerow(["fib_near_level_within_1pct", lvl, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}"])

        # Confluence bucket
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_c = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction:
                        by_c[t.confluence_bucket].append(t)
                for c in sorted(by_c.keys()):
                    ts = by_c[c]
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    dlt = wr - baselines[(tier, direction)]
                    w.writerow(["confluence_bucket", c, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}"])

        # Alignment
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                for name, acc in (("htf_aligned", lambda t: t.htf_aligned),
                                  ("daily_aligned", lambda t: t.daily_aligned)):
                    for val in (True, False):
                        ts = [t for t in trades
                              if t.tier == tier and t.direction == direction and acc(t) == val]
                        n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                        if n == 0:
                            continue
                        wr = 100 * fw / n
                        dlt = wr - baselines[(tier, direction)]
                        w.writerow([name, str(val), tier, direction, n, fw, fp, fl,
                                    f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}"])

        # CB line side (only meaningful for in_box trades)
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_cb = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction and t.pb_state == "in_box":
                        by_cb[t.cb_side].append(t)
                for side in ("above_cb", "below_cb", "at_cb"):
                    ts = by_cb.get(side, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    dlt = wr - baselines[(tier, direction)]
                    w.writerow(["cb_side_in_box", side, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}"])

        # Wave label at signal (only meaningful when in_box)
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                by_wl = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.direction == direction and t.pb_state == "in_box":
                        by_wl[t.pb_wave_label].append(t)
                for wl in ("established", "weakening", "breakout_probable", "breakout_imminent"):
                    ts = by_wl.get(wl, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    dlt = wr - baselines[(tier, direction)]
                    w.writerow(["wave_label_in_box", wl, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}"])


def _credit_stats(trades, which="25"):
    """Credit spread stats.
    which='25' uses credit_25_*, '50' uses credit_50_*.
    Returns (n_total, n_with_credit, full_win, partial, full_loss, wr_pct, wr_2w_pct)."""
    with_credit = [t for t in trades if getattr(t, f"credit_{which}_bucket") != "n/a"]
    n_total = len(trades)
    n_wc = len(with_credit)
    if n_wc == 0:
        return (n_total, 0, 0, 0, 0, 0.0, 0.0)
    fw = sum(1 for t in with_credit if getattr(t, f"credit_{which}_bucket") == "full_win")
    fp = sum(1 for t in with_credit if getattr(t, f"credit_{which}_bucket") == "partial")
    fl = sum(1 for t in with_credit if getattr(t, f"credit_{which}_bucket") == "full_loss")
    fw2 = sum(1 for t in with_credit if getattr(t, f"credit_{which}_2w_bucket") == "full_win")
    return (n_total, n_wc, fw, fp, fl, 100 * fw / n_wc, 100 * fw2 / n_wc)


def write_summary_by_credit(trades, path):
    """Per-ticker credit spread WR at $2.50 and $5.00 widths (in_box signals only).

    For bull signals, this is a bull put credit at the Potter Box floor.
    For bear signals, this is a bear call credit at the Potter Box roof.
    Debit WR is also included for side-by-side comparison.
    """
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker", "tier", "direction",
            "n_total_signals", "n_in_box",
            "debit_wr_pct",
            "credit_25_wr_pct", "credit_25_partial_pct", "credit_25_2w_wr_pct",
            "credit_50_wr_pct", "credit_50_partial_pct", "credit_50_2w_wr_pct",
            "better_strategy",
        ])
        by_key = defaultdict(list)
        for t in trades:
            by_key[(t.ticker, t.tier, t.direction)].append(t)
        for (tk, ti, di), ts in sorted(by_key.items()):
            n_total = len(ts)
            if n_total == 0:
                continue
            # Debit WR (all signals, not in_box-restricted)
            debit_fw = sum(1 for t in ts if t.bucket == "full_win")
            debit_wr = 100 * debit_fw / n_total if n_total > 0 else 0.0
            # Credit WR (only in_box signals, because we need a boundary)
            _, n_wc_25, fw_25, fp_25, _, wr_25, wr2_25 = _credit_stats(ts, "25")
            _, n_wc_50, fw_50, fp_50, _, wr_50, wr2_50 = _credit_stats(ts, "50")
            partial_pct_25 = (100 * fp_25 / n_wc_25) if n_wc_25 > 0 else 0.0
            partial_pct_50 = (100 * fp_50 / n_wc_50) if n_wc_50 > 0 else 0.0
            # Tag which strategy performed best (requires ≥ 20 trades for credit to be meaningful)
            if n_wc_25 < 20 and n_wc_50 < 20:
                best = "insufficient_credit_data"
            else:
                best_credit_wr = max(wr_25 if n_wc_25 >= 20 else 0.0, wr_50 if n_wc_50 >= 20 else 0.0)
                if debit_wr >= best_credit_wr + 3:
                    best = "debit"
                elif best_credit_wr >= debit_wr + 3:
                    best = f"credit_{'25' if wr_25 >= wr_50 else '50'}"
                else:
                    best = "tie"
            w.writerow([
                tk, ti, di, n_total, max(n_wc_25, n_wc_50),
                f"{debit_wr:.1f}",
                f"{wr_25:.1f}" if n_wc_25 > 0 else "n/a",
                f"{partial_pct_25:.1f}" if n_wc_25 > 0 else "n/a",
                f"{wr2_25:.1f}" if n_wc_25 > 0 else "n/a",
                f"{wr_50:.1f}" if n_wc_50 > 0 else "n/a",
                f"{partial_pct_50:.1f}" if n_wc_50 > 0 else "n/a",
                f"{wr2_50:.1f}" if n_wc_50 > 0 else "n/a",
                best,
            ])


def _quintile_bins(values):
    """Compute 5 equal-count quintile bin edges from a list of floats.
    Returns (edges, labels) where edges is 6 boundary values and labels is 5 bin names."""
    if not values:
        return [], []
    s = sorted(values)
    n = len(s)
    edges = [s[0]]
    for q in (0.2, 0.4, 0.6, 0.8):
        idx = int(n * q)
        edges.append(s[min(idx, n - 1)])
    edges.append(s[-1])
    labels = [f"Q{i+1}_[{edges[i]:.2f},{edges[i+1]:.2f}]" for i in range(5)]
    return edges, labels


def _which_quintile(value, edges):
    """Return 0-4 bin index for value given 6 edge boundaries."""
    if not edges or len(edges) < 6:
        return 0
    for i in range(5):
        if value <= edges[i + 1]:
            return i
    return 4  # clamp


def write_summary_by_indicator(trades, path):
    """WR by quintile of each indicator, per tier + direction.

    For each (tier, direction, indicator), split that subset into 5 equal-count
    quintile bins by indicator value, then compute WR per bin. This shows which
    indicator levels produce stronger signals.
    """
    indicators = [
        ("ema_diff_pct", lambda t: t.ind_ema_diff_pct),
        ("macd_hist_pct", lambda t: t.ind_macd_hist_pct),
        ("rsi_mfi", lambda t: t.ind_rsi_mfi),
        ("stoch_k", lambda t: t.ind_stoch_k),
        ("stoch_d", lambda t: t.ind_stoch_d),
        ("wt2", lambda t: t.ind_wt2),
        ("rsi", lambda t: t.ind_rsi),
        ("adx", lambda t: t.ind_adx),
        ("candle_body_pct", lambda t: t.candle_body_pct),
        ("candle_close_pos_pct", lambda t: t.candle_close_pos_pct),
    ]

    # Precompute baseline WR per (tier, direction)
    baselines = {}
    for tier in (1, 2):
        for direction in ("bull", "bear"):
            s = [t for t in trades if t.tier == tier and t.direction == direction]
            n, fw, _, _, _, _, _, _, _ = _stats(s)
            baselines[(tier, direction)] = (100 * fw / n) if n > 0 else 0.0

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "indicator", "quintile", "tier", "direction",
            "value_range_low", "value_range_high",
            "n_trades", "full_win", "partial", "full_loss",
            "headline_wr_pct", "win_or_partial_pct", "vs_baseline_wr_pct",
        ])
        for ind_name, accessor in indicators:
            for tier in (1, 2):
                for direction in ("bull", "bear"):
                    subset = [t for t in trades if t.tier == tier and t.direction == direction]
                    if len(subset) < 50:  # need enough data for meaningful quintiles
                        continue
                    values = [accessor(t) for t in subset]
                    edges, _ = _quintile_bins(values)
                    if not edges:
                        continue
                    bins = defaultdict(list)
                    for t in subset:
                        v = accessor(t)
                        q = _which_quintile(v, edges)
                        bins[q].append(t)
                    baseline = baselines[(tier, direction)]
                    for q_idx in range(5):
                        ts = bins.get(q_idx, [])
                        n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                        if n == 0:
                            continue
                        wr = 100 * fw / n
                        dlt = wr - baseline
                        w.writerow([
                            ind_name, f"Q{q_idx+1}", tier, direction,
                            f"{edges[q_idx]:.3f}", f"{edges[q_idx+1]:.3f}",
                            n, fw, fp, fl,
                            f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{dlt:+.1f}",
                        ])


def write_summary_by_refires(trades, path):
    """WR stratified by how many times the signal fired in the same ISO week.

    Three analyses:
    - fires_in_week: WR of trades in weeks with N total fires (1, 2, 3, 4+)
    - fire_position_in_week: WR by position of the trade within the week (1st, 2nd, 3rd, 4+)
    - first_fire_given_total_fires: WR of the FIRST fire, split by total fires that week.
      This answers: "if I enter the first signal and more keep firing, does my first trade
      win more often than if it had been a lone signal?"
    """
    # Group by (ticker, iso_year, iso_week, tier, direction)
    week_groups = defaultdict(list)
    for idx, t in enumerate(trades):
        dt = datetime.fromisoformat(t.signal_dt_ny)
        iso_y, iso_w, _ = dt.isocalendar()
        key = (t.ticker, iso_y, iso_w, t.tier, t.direction)
        week_groups[key].append((idx, t))

    # Tag each trade with (fires_count_this_week, fire_position)
    fires_info = {}  # idx -> (count, position)
    for key, group in week_groups.items():
        group.sort(key=lambda x: x[1].signal_ts)
        n = len(group)
        for pos, (idx, _) in enumerate(group, 1):
            fires_info[idx] = (n, pos)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dimension", "value", "tier", "direction", "n_trades",
                    "full_win", "partial", "full_loss",
                    "headline_wr_pct", "win_or_partial_pct"])

        # 1. Distribution & WR by total fires in week
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                buckets = defaultdict(list)
                for idx, t in enumerate(trades):
                    if t.tier == tier and t.direction == direction:
                        fires, _ = fires_info.get(idx, (1, 1))
                        bucket = str(fires) if fires <= 3 else "4+"
                        buckets[bucket].append(t)
                for bucket in ("1", "2", "3", "4+"):
                    ts = buckets.get(bucket, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    w.writerow(["fires_in_week", bucket, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}"])

        # 2. WR by fire position within the week
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                buckets = defaultdict(list)
                for idx, t in enumerate(trades):
                    if t.tier == tier and t.direction == direction:
                        _, pos = fires_info.get(idx, (1, 1))
                        bucket = str(pos) if pos <= 3 else "4+"
                        buckets[bucket].append(t)
                for bucket in ("1", "2", "3", "4+"):
                    ts = buckets.get(bucket, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    w.writerow(["fire_position_in_week", bucket, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}"])

        # 3. WR of FIRST fire, split by how many total fires that week
        #    This is the money question for the roll decision.
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                buckets = defaultdict(list)
                for idx, t in enumerate(trades):
                    if t.tier == tier and t.direction == direction:
                        fires, pos = fires_info.get(idx, (1, 1))
                        if pos == 1:  # First fire only
                            bucket = str(fires) if fires <= 3 else "4+"
                            buckets[bucket].append(t)
                for bucket in ("1", "2", "3", "4+"):
                    ts = buckets.get(bucket, [])
                    n, fw, fp, fl, _, _, _, _, _ = _stats(ts)
                    if n == 0:
                        continue
                    wr = 100 * fw / n
                    w.writerow(["first_fire_given_total_fires", bucket, tier, direction, n, fw, fp, fl,
                                f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}"])


def write_report(trades, start, end, path):
    n_total = len(trades)
    if n_total == 0:
        with open(path, "w") as f:
            f.write("# Backtest Report\n\nNo trades generated.\n")
        return

    n_t1 = sum(1 for t in trades if t.tier == 1)
    n_t2 = sum(1 for t in trades if t.tier == 2)

    def stat(subset):
        if not subset:
            return (0, 0.0, 0.0, 0.0)
        fw = sum(1 for t in subset if t.bucket == "full_win")
        fp = sum(1 for t in subset if t.bucket == "partial")
        fw2 = sum(1 for t in subset if t.bucket_2w == "full_win")
        return (len(subset), 100*fw/len(subset), 100*(fw+fp)/len(subset), 100*fw2/len(subset))

    a = stat(trades)
    t1b = stat([t for t in trades if t.tier == 1 and t.direction == "bull"])
    t2b = stat([t for t in trades if t.tier == 2 and t.direction == "bull"])
    t1s = stat([t for t in trades if t.tier == 1 and t.direction == "bear"])
    t2s = stat([t for t in trades if t.tier == 2 and t.direction == "bear"])
    bull_r = stat([t for t in trades if t.regime_trend == "BULL"])
    bear_r = stat([t for t in trades if t.regime_trend == "BEAR"])

    md = f"""# Backtest Report — Brad's Unified Signal v3.0 (ENRICHED)
**Date range:** {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}
**Total signals fired:** {n_total}  (Tier 1: {n_t1}  |  Tier 2: {n_t2})

## Grading rule
- **Full win:** price moved ≤ 1% against signal direction by Friday close (short strike still ITM)
- **Partial:** price moved 1-2% against (between strikes — NOT counted as a win)
- **Full loss:** price moved > 2% against (past long strike)

## Hold rule
- Minimum 3 trading days, exit at next Friday close.
- Parallel 2-week exit column shows whether giving trades more room improves results.

---

## Headline numbers

| Subset | N | Full-win WR | +Partial | 2-week WR |
|---|---|---|---|---|
| **ALL** | {a[0]} | **{a[1]:.1f}%** | {a[2]:.1f}% | {a[3]:.1f}% |
| T1 Bull | {t1b[0]} | **{t1b[1]:.1f}%** | {t1b[2]:.1f}% | {t1b[3]:.1f}% |
| T2 Bull | {t2b[0]} | **{t2b[1]:.1f}%** | {t2b[2]:.1f}% | {t2b[3]:.1f}% |
| T1 Bear | {t1s[0]} | **{t1s[1]:.1f}%** | {t1s[2]:.1f}% | {t1s[3]:.1f}% |
| T2 Bear | {t2s[0]} | **{t2s[1]:.1f}%** | {t2s[2]:.1f}% | {t2s[3]:.1f}% |

## By market regime

| Regime | N | WR | +Partial |
|---|---|---|---|
| BULL regime | {bull_r[0]} | {bull_r[1]:.1f}% | {bull_r[2]:.1f}% |
| BEAR regime | {bear_r[0]} | {bear_r[1]:.1f}% | {bear_r[2]:.1f}% |

---

## How to read this

- **50% is coin flip.** Above 55% means real edge. Above 65% means you trade it.
- If the 2-week WR is higher than primary WR, **extending hold improves results** —
  candidate rule: roll trades to next Friday on breakeven weeks.
- If 2-week WR is lower, short holds are the right call.

## Drill-downs

- **`summary_by_ticker.csv`** — which tickers carry the edge
- **`summary_by_regime.csv`** — does edge only exist in BULL/LOW-vol, etc.
- **`summary_by_timing.csv`** — hour-of-day, day-of-week, days-to-Friday
- **`summary_by_confluence.csv`** — Potter Box, Fib, HTF/Daily alignment
  — the **`vs_baseline_wr_pct`** column is the key one. It shows how much each
  overlay changes WR vs. pinescript alone. **Overlays with +5 or more are
  candidates to gate on. Overlays with −5 or more are candidates to filter out.**

`trades.csv` has every trade with full context for spot-checks.
"""
    with open(path, "w") as f:
        f.write(md)


# ═══════ Main ═══════

def main():
    if not MD_TOKEN:
        log.error("MARKETDATA_TOKEN not set")
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    log.info(f"Output: {OUT_DIR}")

    start_str = os.environ.get("BACKTEST_START", "2023-08-01")
    end_str = os.environ.get("BACKTEST_END", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    log.info(f"Range: {start.date()} → {end.date()}")

    override = os.environ.get("BACKTEST_TICKERS", "").strip()
    tickers = [t.strip().upper() for t in override.split(",") if t.strip()] if override else DEFAULT_TICKERS
    log.info(f"Tickers ({len(tickers)}): {','.join(tickers)}")

    regime_map = build_regime_map(start, end)
    log.info(f"Regime map: {len(regime_map)} entries")

    all_trades = []
    progress_path = os.path.join(OUT_DIR, ".progress.json")
    done = set()
    if os.path.exists(progress_path):
        try:
            with open(progress_path) as f:
                done = set(json.load(f).get("done", []))
        except Exception:
            pass

    trades_path = os.path.join(OUT_DIR, "trades.csv")
    if done and os.path.exists(trades_path):
        try:
            with open(trades_path) as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    for k in ("tier", "signal_ts", "entry_ts", "exit_ts", "exit_2w_ts",
                              "signal_hour_et", "days_to_friday", "pb_box_age"):
                        row[k] = int(row[k])
                    for k in ("entry_price", "short_strike", "long_strike", "exit_price",
                              "move_pct", "move_signed_pct", "mae_pct", "mfe_pct", "hold_days",
                              "exit_2w_price", "move_2w_signed_pct",
                              "pb_range_pct", "fib_distance_pct",
                              "swing_dist_above_pct", "swing_dist_below_pct"):
                        row[k] = float(row[k])
                    for k in ("win_headline", "win_2w_headline", "htf_aligned", "daily_aligned"):
                        row[k] = str(row[k]).lower() in ("true", "1")
                    all_trades.append(Trade(**row))
            log.info(f"Resumed {len(all_trades)} trades from {len(done)} tickers")
        except Exception as e:
            log.warning(f"Resume failed: {e}; starting fresh")
            all_trades = []; done = set()

    for idx, ticker in enumerate(tickers, 1):
        if ticker in done:
            log.info(f"[{idx}/{len(tickers)}] {ticker}: done, skipping")
            continue
        log.info(f"[{idx}/{len(tickers)}] {ticker}: fetching 15m...")
        bd = fetch_15m_chunked(ticker, start, end)
        if not bd or len(bd.get("t", [])) < 100:
            log.warning(f"{ticker}: insufficient data")
            done.add(ticker)
            continue
        bars_15m = [{"t": bd["t"][i], "o": bd["o"][i], "h": bd["h"][i],
                     "l": bd["l"][i], "c": bd["c"][i], "v": bd["v"][i]}
                    for i in range(len(bd["t"]))]
        log.info(f"{ticker}: {len(bars_15m)} 15m bars, signals...")
        sig_bars = compute_v3_signals(bars_15m)
        daily_bars = resample_to_daily(bars_15m)
        if len(daily_bars) < POTTER_LOOKBACK_DAYS + 5:
            log.warning(f"{ticker}: not enough daily bars for overlays")
            done.add(ticker)
            continue
        log.info(f"{ticker}: {len(daily_bars)} daily bars, trades...")
        trades = simulate_trades(ticker, sig_bars, daily_bars, regime_map)
        n_t1 = sum(1 for t in trades if t.tier == 1)
        n_t2 = sum(1 for t in trades if t.tier == 2)
        log.info(f"{ticker}: {len(trades)} trades (T1={n_t1}, T2={n_t2})")
        all_trades.extend(trades)

        done.add(ticker)
        write_trades_csv(all_trades, trades_path)
        with open(progress_path, "w") as f:
            json.dump({"done": sorted(done)}, f)

    log.info(f"Writing summaries (total: {len(all_trades)} trades)")
    write_trades_csv(all_trades, trades_path)
    write_summary_by_ticker(all_trades, os.path.join(OUT_DIR, "summary_by_ticker.csv"))
    write_summary_by_regime(all_trades, os.path.join(OUT_DIR, "summary_by_regime.csv"))
    write_summary_by_timing(all_trades, os.path.join(OUT_DIR, "summary_by_timing.csv"))
    write_summary_by_confluence(all_trades, os.path.join(OUT_DIR, "summary_by_confluence.csv"))
    write_summary_by_credit(all_trades, os.path.join(OUT_DIR, "summary_by_credit.csv"))
    write_summary_by_indicator(all_trades, os.path.join(OUT_DIR, "summary_by_indicator.csv"))
    write_summary_by_refires(all_trades, os.path.join(OUT_DIR, "summary_by_refires.csv"))
    write_report(all_trades, start, end, os.path.join(OUT_DIR, "report.md"))

    log.info(f"DONE. Outputs in {OUT_DIR}:")
    for fn in sorted(os.listdir(OUT_DIR)):
        if not fn.startswith("."):
            fp = os.path.join(OUT_DIR, fn)
            log.info(f"  {fn}  ({os.path.getsize(fp)} bytes)")


if __name__ == "__main__":
    main()
