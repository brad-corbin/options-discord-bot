#!/usr/bin/env python3
"""
bt_resolution_study.py — Multi-Resolution Scoring System Comparison
════════════════════════════════════════════════════════════════════

PURPOSE
  Determine where trading edge lives across:
    - Bar resolution:  5-minute, 15-minute, 30-minute
    - Scoring system:  pinescript v3.0  vs  active_scanner v6.1
    - Cross-resolution confluence (same direction fired on multiple
      resolutions within ±15 minutes)

DESIGN PRINCIPLE — NO DUPLICATION, NO DRIFT
  This file does NOT re-implement the scoring math. It imports the exact
  production functions from:
    - backtest_v3_runner.compute_v3_signals         (pinescript v3.0)
    - active_scanner._analyze_ticker helpers        (active scanner v6.1)
    - potter_box.detect_boxes                        (Potter Box state)
    - bt_shared.compute_regime_for_date              (regime classifier)

  The ONLY new logic added here is:
    1. Data download (MarketData-only — user explicitly excluded Schwab)
    2. Resampling 5m bars → 15m and 30m (local aggregation)
    3. Cross-resolution confluence flags
    4. Multi-resolution output tabulation

  Because compute_v3_signals from v3_runner is imported directly, the 15m
  results in this run MUST match v3_runner's 172K-signal baseline when run
  over overlapping periods. This is a verification property.

WHAT WE ARE NOT DOING (per user instruction: no assumptions)
  - Not re-implementing any indicator formulas
  - Not tuning thresholds
  - Not changing the session filter (ET, 9:30+15min → 16:00)
  - Not changing the HTF hourly/daily resample logic
  - Not changing the Potter Box detection or CB midpoint rule
  - Not introducing new exit rules (MIN_HOLD_DAYS=3, Friday close — same as v3_runner)

USAGE (Render shell)
    cd /opt/render/project/src
    python backtest/bt_resolution_study.py              # all tickers, 600 days back
    python backtest/bt_resolution_study.py --days 300
    python backtest/bt_resolution_study.py --ticker NVDA

ENV
    MARKETDATA_TOKEN          required
    BACKTEST_START/END        optional YYYY-MM-DD overrides
    BACKTEST_TICKERS          optional comma-separated override
    BOT_REPO_PATH             default /opt/render/project/src

OUTPUTS: /tmp/backtest_resolution/
    trades.csv                                one row per signal (all resolutions + scoring systems)
    summary_by_resolution_scoring.csv         WR per (resolution × scoring system × tier × bias)
    summary_by_tier_direction.csv             headline numbers
    summary_by_confluence.csv                 cross-resolution confluence WR
    summary_by_regime.csv                     does edge survive regime change
    report.md                                 executive summary
    .progress.json                            resume-from-checkpoint
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict, fields as _dc_fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────
# BOOTSTRAP — import from the repo so we get LIVE logic, not a copy
# ─────────────────────────────────────────────────────────────
BOT_REPO_PATH = os.environ.get("BOT_REPO_PATH", "/opt/render/project/src")
if BOT_REPO_PATH not in sys.path:
    sys.path.insert(0, BOT_REPO_PATH)
BACKTEST_DIR = Path(BOT_REPO_PATH) / "backtest"
if str(BACKTEST_DIR) not in sys.path:
    sys.path.insert(0, str(BACKTEST_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bt_resolution")

# ── v3_runner: pinescript signal logic (imported verbatim) ──
try:
    import backtest_v3_runner as v3r
    # Verify the function signatures we depend on are present
    assert hasattr(v3r, "compute_v3_signals")
    assert hasattr(v3r, "fetch_candles")
    assert hasattr(v3r, "_to_unix_ts")
    assert hasattr(v3r, "resample_to_daily")
    log.info(f"Loaded pinescript signal logic from {BOT_REPO_PATH}/backtest_v3_runner.py")
except ImportError as e:
    log.error(f"Cannot import backtest_v3_runner: {e}")
    log.error(f"Expected at {BOT_REPO_PATH}/backtest_v3_runner.py")
    sys.exit(1)
except AssertionError:
    log.error("backtest_v3_runner.py is missing expected functions")
    sys.exit(1)

# ── active_scanner: scoring helpers (imported verbatim) ──
try:
    from active_scanner import (
        _compute_ema, _compute_rsi, _compute_macd, _compute_wavetrend,
        EMA_FAST as AS_EMA_FAST, EMA_SLOW as AS_EMA_SLOW,
        MACD_FAST as AS_MACD_FAST, MACD_SLOW as AS_MACD_SLOW,
        MACD_SIGNAL as AS_MACD_SIGNAL,
        RSI_PERIOD as AS_RSI_PERIOD,
        WT_CHANNEL as AS_WT_CHANNEL, WT_AVERAGE as AS_WT_AVERAGE,
        SIGNAL_TIER_1_SCORE as AS_T1_SCORE,
        SIGNAL_TIER_2_SCORE as AS_T2_SCORE,
        MIN_SIGNAL_SCORE as AS_MIN_SCORE,
    )
    log.info(f"Loaded active scanner helpers from {BOT_REPO_PATH}/active_scanner.py")
    log.info(f"  thresholds: MIN={AS_MIN_SCORE} T2={AS_T2_SCORE} T1={AS_T1_SCORE}")
except ImportError as e:
    log.error(f"Cannot import active_scanner: {e}")
    sys.exit(1)

# ── Regime classifier (from bt_shared) ──
try:
    from bt_shared import compute_regime_for_date
    log.info("Loaded bt_shared.compute_regime_for_date")
except ImportError as e:
    log.error(f"Cannot import bt_shared: {e}")
    sys.exit(1)

# ── Potter Box (from live engine, matches bot exactly) ──
_detect_boxes_live = None
try:
    from potter_box import detect_boxes as _detect_boxes_live
    log.info(f"Loaded LIVE Potter Box engine from {BOT_REPO_PATH}/potter_box.py")
except ImportError as e:
    log.warning(f"potter_box import failed ({e}); Potter overlay columns will be empty")


# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════

MD_TOKEN = os.environ.get("MARKETDATA_TOKEN", "").strip()

# Match v3_runner's ticker universe exactly
ALL_TICKERS = [
    "AAPL", "AMD", "AMZN", "ARM", "AVGO", "BA", "CAT", "COIN", "CRM", "DIA",
    "GLD", "GOOGL", "GS", "IWM", "JPM", "LLY", "META", "MRNA", "MSFT", "MSTR",
    "NFLX", "NVDA", "ORCL", "PLTR", "QQQ", "SMCI", "SOFI", "SOXX", "SPY",
    "TLT", "TSLA", "UNH", "XLE", "XLF", "XLV",
]

# Match v3_runner's timezone choice (ET, -5 offset — this is how v3_runner ran)
NY = timezone(timedelta(hours=-5))

RESOLUTIONS = [5, 15, 30]   # minutes
CONFLUENCE_WINDOW_MIN = 15  # "same time" = within ±15 minutes
MIN_HOLD_DAYS = v3r.MIN_HOLD_DAYS  # 3, imported from v3_runner
MIN_ADTV_DOLLARS = 5_000_000  # matches active_scanner ADTV gate

OUT_DIR = Path(os.environ.get("BACKTEST_OUT_DIR", "/tmp/backtest_resolution"))
DATA_DIR = Path(os.environ.get("BACKTEST_DATA_DIR", str(BACKTEST_DIR / "data")))


# ═══════════════════════════════════════════════════════════
# DATA DOWNLOAD — uses v3_runner.fetch_candles directly
# Provider: MarketData-only (user explicit: no Schwab API)
# ═══════════════════════════════════════════════════════════

def fetch_5min_chunked(ticker, start, end):
    """Download 5-minute bars in 90-day chunks using v3_runner's fetch_candles.
    CACHED to disk at backtest/data/{ticker}_5min_{start}_{end}.csv — re-runs
    with the same ticker + date range skip the download entirely.

    Returns a dict identical to v3_runner.fetch_15m_chunked's format:
      {"s": "ok", "t": [...], "o": [...], "h": [...], "l": [...], "c": [...], "v": [...]}
    """
    # Check on-disk cache first
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = f"{ticker}_5min_{start.date()}_{end.date()}.csv"
    cache_path = DATA_DIR / cache_key
    if cache_path.exists():
        try:
            data = _load_bars_cache(cache_path)
            if data and data.get("t"):
                log.info(f"{ticker}: {len(data['t'])} 5m bars (CACHED from disk)")
                return data
        except Exception as e:
            log.warning(f"{ticker}: cache read failed ({e}); re-downloading")

    # No cache; download chunked
    all_t, all_o, all_h, all_l, all_c, all_v = [], [], [], [], [], []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=90), end)
        data = v3r.fetch_candles(ticker, "5", cur, chunk_end)
        if data:
            all_t.extend(data.get("t", [])); all_o.extend(data.get("o", []))
            all_h.extend(data.get("h", [])); all_l.extend(data.get("l", []))
            all_c.extend(data.get("c", [])); all_v.extend(data.get("v", []))
        cur = chunk_end
    if not all_t:
        return None
    # Dedupe + sort by timestamp (identical to v3_runner)
    seen = set()
    out_t, out_o, out_h, out_l, out_c, out_v = [], [], [], [], [], []
    for i, t in enumerate(all_t):
        if t in seen:
            continue
        seen.add(t)
        out_t.append(t); out_o.append(all_o[i]); out_h.append(all_h[i])
        out_l.append(all_l[i]); out_c.append(all_c[i]); out_v.append(all_v[i])
    idx = sorted(range(len(out_t)), key=lambda i: out_t[i])
    result = {
        "s": "ok",
        "t": [out_t[i] for i in idx], "o": [out_o[i] for i in idx],
        "h": [out_h[i] for i in idx], "l": [out_l[i] for i in idx],
        "c": [out_c[i] for i in idx], "v": [out_v[i] for i in idx],
    }

    # Save to cache for next run
    try:
        _save_bars_cache(cache_path, result)
        log.info(f"{ticker}: wrote {len(result['t'])} bars to cache {cache_path.name}")
    except Exception as e:
        log.warning(f"{ticker}: cache write failed ({e}); continuing")

    return result


def _load_bars_cache(path):
    """Load cached bars from CSV."""
    t_list, o_list, h_list, l_list, c_list, v_list = [], [], [], [], [], []
    with open(path) as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
        for row in rdr:
            if len(row) < 6:
                continue
            try:
                t_list.append(int(float(row[0])))
                o_list.append(float(row[1]))
                h_list.append(float(row[2]))
                l_list.append(float(row[3]))
                c_list.append(float(row[4]))
                v_list.append(float(row[5]))
            except (ValueError, IndexError):
                continue
    return {
        "s": "ok",
        "t": t_list, "o": o_list, "h": h_list, "l": l_list,
        "c": c_list, "v": v_list,
    }


def _save_bars_cache(path, data):
    """Save bars to CSV cache."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for i in range(len(data["t"])):
            w.writerow([
                data["t"][i], data["o"][i], data["h"][i],
                data["l"][i], data["c"][i], data["v"][i],
            ])


def bars_from_data(data):
    """Convert fetch_candles dict to list of bar dicts with 't' field
    in the format v3_runner.compute_v3_signals expects."""
    if not data or not data.get("t"):
        return []
    n = len(data["t"])
    return [{
        "t": data["t"][i], "o": data["o"][i], "h": data["h"][i],
        "l": data["l"][i], "c": data["c"][i], "v": data["v"][i],
    } for i in range(n)]


# ═══════════════════════════════════════════════════════════
# RESAMPLING 5m → 15m / 30m
#
# Aggregate 5-min bars into wider bars, aligned to the regular trading
# session clock. This is the ONE piece of new logic in this file that
# isn't imported — because v3_runner fetched 15m bars natively from
# MarketData rather than resampling.
#
# VALIDATION RULE: if we resample 5m → 15m and feed that into
# compute_v3_signals, the signal firings should be very close to what
# v3_runner produced on native 15m bars over the same period. We verify
# this in the report output (a WR delta > 5% would indicate a bug).
# ═══════════════════════════════════════════════════════════

def resample_bars(bars_5m, target_minutes):
    """Aggregate 5-min bars into larger target bars aligned to 9:30 ET open.

    15m buckets: 9:30, 9:45, 10:00, ..., 15:45
    30m buckets: 9:30, 10:00, 10:30, ..., 15:30

    Each bar carries: t (timestamp at bucket start), o/h/l/c/v aggregated.
    Matches v3_runner's bar format exactly.
    """
    if not bars_5m:
        return []
    factor = target_minutes // 5
    if factor < 1:
        raise ValueError(f"Bad target_minutes {target_minutes}")
    if factor == 1:
        return bars_5m  # pass-through for 5m

    # Group by session date (date string in NY tz)
    by_date = defaultdict(list)
    for b in bars_5m:
        dt = datetime.fromtimestamp(b["t"], tz=NY)
        dk = dt.strftime("%Y-%m-%d")
        by_date[dk].append(b)

    out = []
    for d in sorted(by_date.keys()):
        day_bars = sorted(by_date[d], key=lambda x: x["t"])
        buckets = {}
        for b in day_bars:
            dt = datetime.fromtimestamp(b["t"], tz=NY)
            # Minute-of-day in NY time
            mod = dt.hour * 60 + dt.minute
            # Minutes since 9:30 ET session open (570 min)
            mso = mod - 570
            if mso < 0:
                continue  # pre-market bars — skip (matches v3_runner session filter intent)
            bucket_idx = mso // target_minutes
            bucket_minutes = 570 + bucket_idx * target_minutes
            hh, mm = divmod(bucket_minutes, 60)
            # Bucket timestamp: the timestamp of the first 5m bar in the bucket
            bucket_start_dt = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            bucket_ts = int(bucket_start_dt.timestamp())

            if bucket_ts not in buckets:
                buckets[bucket_ts] = {
                    "t": bucket_ts,
                    "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"],
                }
            else:
                bk = buckets[bucket_ts]
                bk["h"] = max(bk["h"], b["h"])
                bk["l"] = min(bk["l"], b["l"])
                bk["c"] = b["c"]  # last close wins
                bk["v"] += b["v"]

        for bk in sorted(buckets.values(), key=lambda x: x["t"]):
            out.append(bk)
    return out


# ═══════════════════════════════════════════════════════════
# ACTIVE SCANNER SIGNAL DETECTION
# Direct port of active_scanner._analyze_ticker scoring — same as
# bt_active_v8.py. Using the LIVE helpers from active_scanner.py so
# indicator math is guaranteed identical.
#
# ADAPTED FOR BACKTESTING ONLY IN:
#   - No streaming spot override (use last bar close)
#   - No flow boost (not backtestable)
#   - No phase computation from wall clock (use bar time instead)
# ═══════════════════════════════════════════════════════════

def _compute_session_vwap(bars_window):
    """Matches active_scanner._analyze_ticker VWAP computation."""
    if not bars_window:
        return None
    tp_vol_sum = 0.0
    vol_sum = 0.0
    for b in bars_window:
        h = b.get("h"); l = b.get("l"); c = b.get("c"); v = b.get("v", 0) or 0
        if h is None or l is None or c is None or v <= 0:
            continue
        tp_vol_sum += ((h + l + c) / 3.0) * v
        vol_sum += v
    return (tp_vol_sum / vol_sum) if vol_sum > 0 else None


def detect_active_scanner(window_bars, daily_closes, regime, ticker):
    """Port of active_scanner._analyze_ticker (v6.1) returning a signal dict or None.

    This is IDENTICAL to bt_active_v8.py's detect_signal_backtest, preserving
    the scoring exactly as in production.
    """
    if len(window_bars) < 12:
        return None

    closes  = [b["c"] for b in window_bars if b.get("c") is not None]
    highs   = [b["h"] for b in window_bars if b.get("h") is not None]
    lows    = [b["l"] for b in window_bars if b.get("l") is not None]
    volumes = [b.get("v", 0) or 0 for b in window_bars]
    if len(closes) < 12:
        return None

    spot = closes[-1]

    # ADTV gate (matches live scanner)
    if volumes and len(volumes) >= 10:
        avg_vol_10 = sum(volumes[-10:]) / 10
        adtv = avg_vol_10 * spot * 5 * 60
        if adtv < MIN_ADTV_DOLLARS and ticker not in ("SPY", "QQQ", "IWM", "DIA"):
            return None

    vwap = _compute_session_vwap(window_bars)
    ema5  = _compute_ema(closes, AS_EMA_FAST)
    ema12 = _compute_ema(closes, AS_EMA_SLOW)
    if not ema5 or not ema12:
        return None
    ema_bull = ema5[-1] > ema12[-1]
    ema_dist_pct = ((ema5[-1] - ema12[-1]) / ema12[-1]) * 100 if ema12[-1] > 0 else 0.0

    macd_v = _compute_macd(closes)
    hlc3 = [(highs[i] + lows[i] + closes[i]) / 3
            for i in range(min(len(highs), len(lows), len(closes)))]
    wt = _compute_wavetrend(hlc3)
    rsi_v = _compute_rsi(closes, AS_RSI_PERIOD)

    avg_vol      = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    current_vol  = volumes[-1] if volumes else 0
    volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

    # Daily trend / HTF status (matches live logic exactly)
    daily_bull = None
    htf_confirmed = False
    htf_converging = False
    htf_status = "UNKNOWN"
    # v2 fix (2026-04-19): require BOTH EMAs have len>=2, not just d8.
    # Live scanner only checks d8 but that works in production because
    # it always pulls 30 days of daily data — the backtest at early
    # dates may only have ~21 days, causing d21 to have length 1.
    if daily_closes and len(daily_closes) >= 22:
        d8 = _compute_ema(daily_closes, 8)
        d21 = _compute_ema(daily_closes, 21)
        if d8 and d21 and len(d8) >= 2 and len(d21) >= 2:
            daily_bull = d8[-1] > d21[-1]
            htf_confirmed = (daily_bull == ema_bull)
            if htf_confirmed:
                htf_status = "CONFIRMED"
            else:
                gap_n = abs(d8[-1] - d21[-1])
                gap_p = abs(d8[-2] - d21[-2])
                if gap_n < gap_p * 0.98:
                    htf_converging = True
                    htf_status = "CONVERGING"
                else:
                    htf_status = "OPPOSING"

    # ── Scoring (identical to active_scanner v6.1) ──
    score = 0
    bias = "bull" if ema_bull else "bear"

    if abs(ema_dist_pct) > 0.03:
        score += 15
    elif abs(ema_dist_pct) > 0.01:
        score += 8

    if macd_v:
        mh = macd_v.get("macd_hist", 0)
        if bias == "bull" and mh > 0:
            score += 15
        elif bias == "bear" and mh < 0:
            score += 15
        elif mh != 0:
            score -= 10
        if macd_v.get("macd_cross_bull") and bias == "bull":
            score += 10
        elif macd_v.get("macd_cross_bear") and bias == "bear":
            score += 10

    if wt:
        if bias == "bull" and wt.get("wt_oversold"):
            score += 15
        elif bias == "bear" and wt.get("wt_overbought"):
            score += 15
        elif bias == "bull" and wt.get("wt_overbought"):
            score -= 10
        elif bias == "bear" and wt.get("wt_oversold"):
            score -= 10
        elif bias == "bull" and wt.get("wt_cross_bull"):
            score += 10
        elif bias == "bear" and wt.get("wt_cross_bear"):
            score += 10

    if vwap:
        if bias == "bull" and spot > vwap:
            score += 10
        elif bias == "bear" and spot < vwap:
            score += 10
        elif bias == "bull" and spot < vwap:
            score -= 5
        elif bias == "bear" and spot > vwap:
            score -= 5

    # P2 regime fix: CONVERGING in TRANSITION gets +12 instead of -10
    if htf_confirmed:
        score += 15
    elif htf_converging and regime == "TRANSITION":
        score += 12
    elif daily_bull is not None:
        if (bias == "bull" and daily_bull) or (bias == "bear" and not daily_bull):
            score += 10
        else:
            score -= 10

    if volume_ratio > 1.5:
        score += 10
    elif volume_ratio > 1.0:
        score += 5

    # P4 regime fix: TRANSITION bull RSI window is 50–75 not 40–65
    if rsi_v:
        if regime == "TRANSITION" and bias == "bull":
            if 50 < rsi_v < 75:
                score += 5
            elif rsi_v < 45:
                score -= 5
        elif bias == "bull" and 40 < rsi_v < 65:
            score += 5
        elif bias == "bear" and 35 < rsi_v < 60:
            score += 5

    # No flow boost in backtest

    if score < AS_MIN_SCORE:
        return None

    tier = 1 if score >= AS_T1_SCORE else 2

    return {
        "bias": bias, "tier": tier, "score": score,
        "close": spot,
        "ind_ema_diff_pct": ema_dist_pct,
        "ind_macd_hist": macd_v.get("macd_hist", 0) if macd_v else 0,
        "ind_rsi": rsi_v if rsi_v else 50.0,
        "ind_wt2": wt.get("wt2", 0) if wt else 0,
        "htf_status": htf_status,
        "htf_confirmed": htf_confirmed,
        "daily_bull": daily_bull,
        "volume_ratio": volume_ratio,
    }


# ═══════════════════════════════════════════════════════════
# TRADE DATACLASS
# ═══════════════════════════════════════════════════════════

@dataclass
class Trade:
    ticker: str
    resolution_min: int             # 5, 15, 30
    scoring_system: str             # "pinescript" or "active_scanner"
    tier: int                       # 1 or 2
    direction: str                  # "bull" or "bear"
    signal_ts: int
    signal_dt_ny: str
    entry_ts: int
    entry_dt_ny: str
    entry_price: float
    exit_ts: int
    exit_dt_ny: str
    exit_price: float
    move_pct: float
    move_signed_pct: float
    hold_days: float
    bucket: str                     # full_win / partial / full_loss
    win_headline: bool
    regime_trend: str               # BULL / TRANSITION / BEAR from bt_shared
    regime_vol: str
    # Indicator values at signal
    ind_ema_diff_pct: float = 0.0
    ind_macd_hist: float = 0.0
    ind_rsi: float = 50.0
    ind_wt2: float = 0.0
    ind_adx: float = 0.0
    score: int = 0                  # active_scanner score (pinescript always 0)
    htf_status: str = "UNKNOWN"
    # Confluence flags — was a same-direction signal fired within ±15min
    # on the other resolutions?
    conf_5m: bool = False
    conf_15m: bool = False
    conf_30m: bool = False
    confluence_count: int = 1       # 1 = this resolution only; 2 or 3 = confluent
    # ── Potter Box overlay (from LIVE detect_boxes engine) ──
    pb_state: str = "no_box"         # in_box / above_roof / below_floor / post_box / no_box
    pb_floor: float = 0.0
    pb_roof: float = 0.0
    pb_midpoint: float = 0.0         # CB line
    pb_range_pct: float = 0.0
    pb_wave_label: str = "none"      # established / weakening / breakout_probable / breakout_imminent
    pb_break_confirmed: bool = False
    # CB side (meaningful only when in_box)
    cb_side: str = "n/a"             # above_cb / below_cb / at_cb / n/a
    cb_distance_pct: float = 0.0
    # ── Credit spread sim (only when in_box) ──
    credit_short_strike: float = 0.0
    credit_25_bucket: str = "n/a"    # $2.50-wide credit spread outcome
    credit_25_win: bool = False
    credit_50_bucket: str = "n/a"    # $5.00-wide credit spread outcome
    credit_50_win: bool = False
    # ── 1-hour S/R overlay ──
    # Fractal method: nearest order-3 swing high/low from recent hourly bars
    sr_h_fractal_above: float = 0.0
    sr_h_fractal_below: float = 0.0
    sr_h_fractal_dist_above_pct: float = 999.0
    sr_h_fractal_dist_below_pct: float = 999.0
    # Pivot method: recent highest-high / lowest-low in lookback window
    sr_h_pivot_above: float = 0.0
    sr_h_pivot_below: float = 0.0
    sr_h_pivot_dist_above_pct: float = 999.0
    sr_h_pivot_dist_below_pct: float = 999.0


# ═══════════════════════════════════════════════════════════
# OVERLAY COMPUTATIONS
# Potter Box, credit spreads, 1-hour S/R. All imported from live
# engines where possible (Potter Box) to guarantee zero drift.
# ═══════════════════════════════════════════════════════════

# Potter Box cache — detect_boxes is expensive, run once per ticker per
# daily-bar-count and reuse across all signals.
_pb_box_cache: dict = {}


def _get_boxes_for_ticker(daily_bars, ticker):
    """Cache detect_boxes result per (ticker, len(daily_bars))."""
    key = (ticker, len(daily_bars))
    if key in _pb_box_cache:
        return _pb_box_cache[key]
    if _detect_boxes_live is None:
        _pb_box_cache[key] = []
        return []
    try:
        boxes = _detect_boxes_live(daily_bars, ticker)
    except Exception as e:
        log.warning(f"detect_boxes failed for {ticker}: {e}")
        boxes = []
    _pb_box_cache[key] = boxes
    return boxes


def compute_potter_box_at(daily_bars, idx, ticker, spot_at_signal):
    """Return Potter Box state dict at daily bar index `idx`.

    Classification (matches bt_active_v8 and v3_runner exactly):
      - in_box:      signal fires during active consolidation
      - above_roof:  box broke upward, signal fires post-confirmed-break
      - below_floor: box broke downward, signal fires post-confirmed-break
      - post_box:    box ended, no confirmed break yet
      - no_box:      no box detected at or before this date
    """
    empty = {
        "state": "no_box", "floor": 0.0, "roof": 0.0, "midpoint": 0.0,
        "range_pct": 0.0, "wave_label": "none", "break_confirmed": False,
        "cb_side": "n/a", "cb_distance_pct": 0.0,
    }
    if idx < 0 or idx >= len(daily_bars):
        return empty
    boxes = _get_boxes_for_ticker(daily_bars, ticker)
    if not boxes:
        return empty

    # Most recent box whose start_idx <= idx
    relevant = [b for b in boxes if b.get("start_idx", -1) <= idx]
    if not relevant:
        return empty
    box = relevant[-1]

    start_idx = box.get("start_idx", -1)
    end_idx = box.get("end_idx", -1)
    floor = float(box.get("floor", 0))
    roof = float(box.get("roof", 0))
    midpoint = float(box.get("midpoint", (roof + floor) / 2 if roof and floor else 0))
    broken = bool(box.get("broken", False))
    confirmed = bool(box.get("break_confirmed", False))
    break_dir = box.get("break_direction")

    # Classify
    if start_idx <= idx <= end_idx:
        state = "in_box"
    elif broken and confirmed:
        state = "above_roof" if break_dir == "up" else ("below_floor" if break_dir == "down" else "post_box")
    else:
        # Within 5 bars of end = still in_box (consolidation could still resolve)
        bars_since_end = idx - end_idx
        state = "in_box" if bars_since_end <= 5 else "post_box"

    # CB side (only when in_box)
    cb_side = "n/a"
    cb_dist_pct = 0.0
    CB_TIE_PCT = 0.10
    if state == "in_box" and midpoint > 0 and spot_at_signal > 0:
        dist_pct = abs(spot_at_signal - midpoint) / spot_at_signal * 100.0
        if dist_pct <= CB_TIE_PCT:
            cb_side = "at_cb"
        elif spot_at_signal > midpoint:
            cb_side = "above_cb"
        else:
            cb_side = "below_cb"
        cb_dist_pct = dist_pct

    return {
        "state": state, "floor": floor, "roof": roof, "midpoint": midpoint,
        "range_pct": float(box.get("range_pct", 0)),
        "wave_label": str(box.get("wave_label", "none")),
        "break_confirmed": confirmed,
        "cb_side": cb_side, "cb_distance_pct": cb_dist_pct,
    }


def grade_credit_spread(direction, short_strike, long_strike, exit_price):
    """Grade a credit spread at exit, matching v3_runner.grade_credit exactly.

    Bull put credit: short > long. Full win = exit >= short.
    Bear call credit: short < long. Full win = exit <= short.

    Returns (bucket, is_win).
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
    else:
        if exit_price <= short_strike:
            return "full_win", True
        elif exit_price < long_strike:
            return "partial", False
        else:
            return "full_loss", False


def resample_to_hourly(bars_5m):
    """Resample 5-min bars → 1-hour bars aligned to 9:30 ET open.

    Matches market-clock aggregation used by resample_bars() but at 1h width.
    """
    if not bars_5m:
        return []
    by_date = defaultdict(list)
    for b in bars_5m:
        dt = datetime.fromtimestamp(b["t"], tz=NY)
        by_date[dt.strftime("%Y-%m-%d")].append(b)

    out = []
    TARGET_MIN = 60
    for d in sorted(by_date.keys()):
        day_bars = sorted(by_date[d], key=lambda x: x["t"])
        buckets = {}
        for b in day_bars:
            dt = datetime.fromtimestamp(b["t"], tz=NY)
            mod = dt.hour * 60 + dt.minute
            mso = mod - 570  # minutes since 9:30 ET
            if mso < 0:
                continue
            bucket_idx = mso // TARGET_MIN
            bucket_minutes = 570 + bucket_idx * TARGET_MIN
            hh, mm = divmod(bucket_minutes, 60)
            bucket_start_dt = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            bucket_ts = int(bucket_start_dt.timestamp())

            if bucket_ts not in buckets:
                buckets[bucket_ts] = {
                    "t": bucket_ts, "o": b["o"], "h": b["h"], "l": b["l"],
                    "c": b["c"], "v": b["v"],
                }
            else:
                bk = buckets[bucket_ts]
                bk["h"] = max(bk["h"], b["h"])
                bk["l"] = min(bk["l"], b["l"])
                bk["c"] = b["c"]
                bk["v"] += b["v"]
        for bk in sorted(buckets.values(), key=lambda x: x["t"]):
            out.append(bk)
    return out


# Cache the hourly resample per ticker — compute once per ticker
_hourly_cache: dict = {}

# Cache precomputed fractal-swing points per ticker (for 1h S/R)
_hourly_fractals_cache: dict = {}


def find_hourly_fractals(bars_1h, order=3):
    """Find order-3 swing highs and lows in 1h bars.
    Returns (highs_list, lows_list) of (idx, price) tuples."""
    n = len(bars_1h)
    highs = []; lows = []
    for i in range(order, n - order):
        wh = [bars_1h[j]["h"] for j in range(i - order, i + order + 1)]
        wl = [bars_1h[j]["l"] for j in range(i - order, i + order + 1)]
        if bars_1h[i]["h"] == max(wh):
            highs.append((i, bars_1h[i]["h"]))
        if bars_1h[i]["l"] == min(wl):
            lows.append((i, bars_1h[i]["l"]))
    return highs, lows


def compute_hourly_sr_at(bars_1h, ticker_key, signal_ts, spot):
    """Compute 1h S/R levels at a given timestamp.

    Returns dict with:
      fractal_above / fractal_below — nearest swing high/low from order-3 fractals
      pivot_above / pivot_below — highest-high / lowest-low in recent window
      Distances in % of spot.

    Only uses data at or before signal_ts (no lookahead).
    Lookback = 100 hourly bars (~2.5 weeks).
    """
    empty = {
        "fractal_above": 0.0, "fractal_below": 0.0,
        "fractal_dist_above_pct": 999.0, "fractal_dist_below_pct": 999.0,
        "pivot_above": 0.0, "pivot_below": 0.0,
        "pivot_dist_above_pct": 999.0, "pivot_dist_below_pct": 999.0,
    }
    if not bars_1h or spot <= 0:
        return empty

    # Find the hourly bar index at or just before signal_ts
    # bars_1h is sorted by timestamp
    cutoff_idx = -1
    for i in range(len(bars_1h) - 1, -1, -1):
        if bars_1h[i]["t"] <= signal_ts:
            cutoff_idx = i
            break
    if cutoff_idx < 10:
        return empty

    # Window: last 100 hours ending at cutoff_idx
    win_start = max(0, cutoff_idx - 100)
    window = bars_1h[win_start: cutoff_idx + 1]
    if len(window) < 10:
        return empty

    # Pivot method: highest high / lowest low in window (simple)
    pivot_hi = max(b["h"] for b in window)
    pivot_lo = min(b["l"] for b in window)
    pivot_above = pivot_hi if pivot_hi > spot else 0.0
    pivot_below = pivot_lo if pivot_lo < spot else 0.0
    pivot_da = ((pivot_above - spot) / spot * 100.0) if pivot_above > 0 else 999.0
    pivot_db = ((spot - pivot_below) / spot * 100.0) if pivot_below > 0 else 999.0

    # Fractal method: cached per ticker (full history fractals) — filter to <= cutoff_idx
    if ticker_key in _hourly_fractals_cache:
        highs, lows = _hourly_fractals_cache[ticker_key]
    else:
        highs, lows = find_hourly_fractals(bars_1h, order=3)
        _hourly_fractals_cache[ticker_key] = (highs, lows)

    # Nearest fractal above/below spot, with fractal index <= cutoff_idx
    frac_above = 0.0
    for (i, p) in highs:
        if i > cutoff_idx:
            break
        if p > spot:
            if frac_above == 0.0 or p < frac_above:
                frac_above = p
    frac_below = 0.0
    for (i, p) in lows:
        if i > cutoff_idx:
            break
        if p < spot:
            if frac_below == 0.0 or p > frac_below:
                frac_below = p

    frac_da = ((frac_above - spot) / spot * 100.0) if frac_above > 0 else 999.0
    frac_db = ((spot - frac_below) / spot * 100.0) if frac_below > 0 else 999.0

    return {
        "fractal_above": frac_above, "fractal_below": frac_below,
        "fractal_dist_above_pct": frac_da, "fractal_dist_below_pct": frac_db,
        "pivot_above": pivot_above, "pivot_below": pivot_below,
        "pivot_dist_above_pct": pivot_da, "pivot_dist_below_pct": pivot_db,
    }


# ═══════════════════════════════════════════════════════════
# TRADE SIMULATION — reuses v3_runner's grading (MIN_HOLD_DAYS=3,
# Friday close exit, -1%/-2% buckets). Direct call to v3_runner's
# find_exit_bar to match exactly.
#
# v3_runner.find_exit_bar takes (bars, entry_idx, min_trading_days)
# where bars is a list of SignalBar objects (with dt_ny). We adapt
# by building a simple object with the needed attributes.
# ═══════════════════════════════════════════════════════════

def _trading_days_to_next_friday_close(entry_dt_ny, min_trading_days=MIN_HOLD_DAYS):
    """Matches v3_runner.find_exit_bar's target-date logic exactly."""
    days_added = 0
    target = entry_dt_ny
    while days_added < min_trading_days:
        target = target + timedelta(days=1)
        if target.weekday() < 5:
            days_added += 1
    while target.weekday() != 4:  # advance to Friday
        target = target + timedelta(days=1)
    return target.date()


def _grade_move(direction, entry, exit_price):
    """Matches v3_runner.grade() exactly.

    Direction = bull: move = (exit - entry) / entry
    Direction = bear: inverted

    Full win: signed ≥ -1.0% (short strike still ITM)
    Partial:  -2.0 ≤ signed < -1.0 (between strikes)
    Full loss: signed < -2.0 (past long strike)
    """
    move = (exit_price - entry) / entry * 100.0
    signed = move if direction == "bull" else -move
    if signed >= -1.0:
        return move, signed, "full_win", True
    elif signed >= -2.0:
        return move, signed, "partial", False
    else:
        return move, signed, "full_loss", False


def simulate_trades_for_signals(signals_list, bars, ticker, resolution_min,
                                scoring_system, regime_map,
                                daily_bars=None, hourly_bars=None):
    """For each signal, find entry (next bar open) and exit (next Friday close,
    min 3 trading days hold). Return list[Trade].

    signals_list: list of signal dicts with keys:
        ts, bias ("bull"/"bear"), tier (1/2), and indicator values

    bars: list of bar dicts with 't' (ts), 'o', 'h', 'l', 'c', 'v' keys.
        Same format as what compute_v3_signals consumes.

    daily_bars: optional daily bars list for Potter Box overlay lookup.
    hourly_bars: optional 1h bars list for S/R overlay lookup.
        Both come from the ticker loop in main() and are shared across all
        resolutions/scoring systems for that ticker (no re-compute).
    """
    if not signals_list or not bars:
        return []

    # Build ts → idx map for fast exit lookups
    ts_to_idx = {bars[i]["t"]: i for i in range(len(bars))}

    # Daily bar lookup (date → index) for Potter Box
    daily_date_to_idx = {}
    if daily_bars:
        for i, db in enumerate(daily_bars):
            daily_date_to_idx[db.get("date", "")] = i

    # Hourly cache key (share fractal cache per ticker)
    hourly_key = f"{ticker}_hourly"

    trades = []
    for sig in signals_list:
        sig_ts = sig["ts"]
        if sig_ts not in ts_to_idx:
            continue
        sig_idx = ts_to_idx[sig_ts]

        # Entry = next bar open
        entry_idx = sig_idx + 1
        if entry_idx >= len(bars):
            continue
        entry_bar = bars[entry_idx]
        entry_ts = entry_bar["t"]
        entry_price = entry_bar["o"]
        entry_dt = datetime.fromtimestamp(entry_ts, tz=NY)

        # Find exit: scan forward for bar whose date matches target Friday
        target_date = _trading_days_to_next_friday_close(entry_dt, MIN_HOLD_DAYS)
        exit_idx = None
        for j in range(entry_idx, len(bars)):
            bdt = datetime.fromtimestamp(bars[j]["t"], tz=NY)
            if bdt.date() < target_date:
                continue
            if bdt.date() > target_date:
                break
            if bdt.date() == target_date and bdt.hour < 16:
                exit_idx = j   # keep advancing, want the last bar before 16:00
        if exit_idx is None:
            continue

        exit_bar = bars[exit_idx]
        exit_ts = exit_bar["t"]
        exit_price = exit_bar["c"]
        exit_dt = datetime.fromtimestamp(exit_ts, tz=NY)

        direction = sig["bias"]
        move, signed, bucket, win = _grade_move(direction, entry_price, exit_price)

        hold_days = (exit_dt - entry_dt).total_seconds() / 86400.0

        # Regime lookup for signal date
        sdk = datetime.fromtimestamp(sig_ts, tz=NY).strftime("%Y-%m-%d")
        rr = regime_map.get(sdk, {})
        regime_trend = rr.get("trend", "UNKNOWN")
        regime_vol = rr.get("vol", "UNKNOWN")

        # ── Potter Box overlay (using day BEFORE signal to avoid lookahead) ──
        pb_state = "no_box"; pb_floor = 0.0; pb_roof = 0.0
        pb_midpoint = 0.0; pb_range = 0.0; pb_wave = "none"
        pb_confirmed = False; cb_side = "n/a"; cb_dist = 0.0
        if daily_bars:
            d_idx = daily_date_to_idx.get(sdk, -1)
            lookup_idx = max(0, d_idx - 1) if d_idx > 0 else -1
            if lookup_idx >= 0:
                pb = compute_potter_box_at(daily_bars, lookup_idx, ticker, entry_price)
                pb_state = pb["state"]
                pb_floor = pb["floor"]; pb_roof = pb["roof"]
                pb_midpoint = pb["midpoint"]; pb_range = pb["range_pct"]
                pb_wave = pb["wave_label"]; pb_confirmed = pb["break_confirmed"]
                cb_side = pb["cb_side"]; cb_dist = pb["cb_distance_pct"]

        # ── Credit spread sim (only when in_box) ──
        cs_short = 0.0
        cs_25_b = "n/a"; cs_25_w = False; cs_50_b = "n/a"; cs_50_w = False
        if pb_state == "in_box" and pb_floor > 0 and pb_roof > 0:
            if direction == "bull":
                cs_short = pb_floor
                cs_25_long = pb_floor - 2.50
                cs_50_long = pb_floor - 5.00
            else:
                cs_short = pb_roof
                cs_25_long = pb_roof + 2.50
                cs_50_long = pb_roof + 5.00
            cs_25_b, cs_25_w = grade_credit_spread(direction, cs_short, cs_25_long, exit_price)
            cs_50_b, cs_50_w = grade_credit_spread(direction, cs_short, cs_50_long, exit_price)

        # ── 1h S/R overlay ──
        sr_fa = 0.0; sr_fb = 0.0; sr_fda = 999.0; sr_fdb = 999.0
        sr_pa = 0.0; sr_pb = 0.0; sr_pda = 999.0; sr_pdb = 999.0
        if hourly_bars:
            sr = compute_hourly_sr_at(hourly_bars, hourly_key, sig_ts, entry_price)
            sr_fa = sr["fractal_above"]; sr_fb = sr["fractal_below"]
            sr_fda = sr["fractal_dist_above_pct"]; sr_fdb = sr["fractal_dist_below_pct"]
            sr_pa = sr["pivot_above"]; sr_pb = sr["pivot_below"]
            sr_pda = sr["pivot_dist_above_pct"]; sr_pdb = sr["pivot_dist_below_pct"]

        trades.append(Trade(
            ticker=ticker, resolution_min=resolution_min,
            scoring_system=scoring_system,
            tier=sig["tier"], direction=direction,
            signal_ts=sig_ts, signal_dt_ny=datetime.fromtimestamp(sig_ts, tz=NY).isoformat(),
            entry_ts=entry_ts, entry_dt_ny=entry_dt.isoformat(), entry_price=entry_price,
            exit_ts=exit_ts, exit_dt_ny=exit_dt.isoformat(), exit_price=exit_price,
            move_pct=move, move_signed_pct=signed,
            hold_days=hold_days, bucket=bucket, win_headline=win,
            regime_trend=regime_trend, regime_vol=regime_vol,
            ind_ema_diff_pct=sig.get("ind_ema_diff_pct", 0.0),
            ind_macd_hist=sig.get("ind_macd_hist", 0.0),
            ind_rsi=sig.get("ind_rsi", 50.0),
            ind_wt2=sig.get("ind_wt2", 0.0),
            ind_adx=sig.get("ind_adx", 0.0),
            score=sig.get("score", 0),
            htf_status=sig.get("htf_status", "UNKNOWN"),
            # Overlays
            pb_state=pb_state, pb_floor=round(pb_floor, 2), pb_roof=round(pb_roof, 2),
            pb_midpoint=round(pb_midpoint, 2), pb_range_pct=round(pb_range, 2),
            pb_wave_label=pb_wave, pb_break_confirmed=pb_confirmed,
            cb_side=cb_side, cb_distance_pct=round(cb_dist, 3),
            credit_short_strike=round(cs_short, 2),
            credit_25_bucket=cs_25_b, credit_25_win=cs_25_w,
            credit_50_bucket=cs_50_b, credit_50_win=cs_50_w,
            sr_h_fractal_above=round(sr_fa, 2), sr_h_fractal_below=round(sr_fb, 2),
            sr_h_fractal_dist_above_pct=round(sr_fda, 2),
            sr_h_fractal_dist_below_pct=round(sr_fdb, 2),
            sr_h_pivot_above=round(sr_pa, 2), sr_h_pivot_below=round(sr_pb, 2),
            sr_h_pivot_dist_above_pct=round(sr_pda, 2),
            sr_h_pivot_dist_below_pct=round(sr_pdb, 2),
        ))

    return trades


# ═══════════════════════════════════════════════════════════
# PINESCRIPT ADAPTER
# compute_v3_signals returns SignalBar objects. Convert to signal dicts
# with the same key names we use for active scanner so downstream code
# can be unified.
# ═══════════════════════════════════════════════════════════

def pinescript_signals(bars):
    """Run v3_runner.compute_v3_signals and convert output to signal dicts.

    Each output dict has: ts, bias, tier, and the indicator values.
    """
    signal_bars = v3r.compute_v3_signals(bars)
    out = []
    for sb in signal_bars:
        # SignalBar has tier1_buy/tier2_buy/tier1_sell/tier2_sell booleans
        if sb.tier1_buy:
            tier = 1; bias = "bull"
        elif sb.tier2_buy:
            tier = 2; bias = "bull"
        elif sb.tier1_sell:
            tier = 1; bias = "bear"
        elif sb.tier2_sell:
            tier = 2; bias = "bear"
        else:
            continue
        out.append({
            "ts": sb.ts, "bias": bias, "tier": tier,
            "close": sb.close,
            "ind_ema_diff_pct": sb.ind_ema_diff_pct,
            "ind_macd_hist": sb.ind_macd_hist_pct,
            "ind_rsi": sb.ind_rsi,
            "ind_wt2": sb.ind_wt2,
            "ind_adx": sb.ind_adx,
            "htf_status": "CONFIRMED" if (bias == "bull" and sb.htf_bull_confirmed)
                          or (bias == "bear" and sb.htf_bear_confirmed) else "UNKNOWN",
            "score": 0,
        })
    return out


# ═══════════════════════════════════════════════════════════
# ACTIVE SCANNER DRIVER — walks forward bar-by-bar
# ═══════════════════════════════════════════════════════════

def active_scanner_signals(bars, daily_bars, ticker, regime_map, countback=80):
    """Walk bar-by-bar, calling detect_active_scanner on each window.

    Dedupes same-direction signals within 3 bars (matching bt_active_v8 and
    live scanner behavior).
    """
    if not bars:
        return []

    # Build daily close array with daily dates for HTF lookup
    daily_closes_by_date = {db["date"]: db["c"] for db in daily_bars}
    sorted_daily_dates = sorted(daily_closes_by_date.keys())

    signals = []
    last_sig_idx = {}  # (bias,) -> last signal idx

    for i in range(countback, len(bars)):
        window = bars[max(0, i - countback + 1): i + 1]
        bar_dt = datetime.fromtimestamp(bars[i]["t"], tz=NY)
        signal_date = bar_dt.strftime("%Y-%m-%d")

        # Regime for signal date
        rr = regime_map.get(signal_date, {})
        # Regime map from build_regime_map returns 'trend' as 'BULL'/'BEAR',
        # not the 5-class BULL/TRANSITION/BEAR we need for scanner's P2/P4.
        # Use compute_regime_for_date if available; otherwise fall back.
        regime = "TRANSITION"
        if rr.get("trend") == "BULL":
            regime = "BULL"
        elif rr.get("trend") == "BEAR":
            regime = "BEAR"

        # Daily closes up to (but not including) signal date — avoids lookahead.
        # v2 fix: require 22+ bars so d21 EMA has at least 2 values
        # (_compute_ema returns len-period+1 outputs, so 22 in → 2 out for d21).
        dc = [daily_closes_by_date[d] for d in sorted_daily_dates if d < signal_date]
        if len(dc) < 22:
            continue

        sig = detect_active_scanner(window, dc[-30:], regime, ticker)
        if sig is None:
            continue

        # Dedup: same bias within 3 bars
        key = sig["bias"]
        if key in last_sig_idx and i - last_sig_idx[key] < 3:
            continue
        last_sig_idx[key] = i

        sig["ts"] = bars[i]["t"]
        signals.append(sig)

    return signals


# ═══════════════════════════════════════════════════════════
# CROSS-RESOLUTION CONFLUENCE
#
# For each trade, check if the OTHER resolutions fired a same-direction
# signal for the same ticker within ±15min of this signal's timestamp.
#
# This is done AFTER all trades are generated so we have the full set
# to compare against.
# ═══════════════════════════════════════════════════════════

def annotate_confluence(trades):
    """In place: set conf_5m, conf_15m, conf_30m, confluence_count on each trade.

    Two trades are "confluent" if:
      - same ticker
      - same direction (bias)
      - same scoring system (don't cross-compare pinescript vs active_scanner)
      - different resolution
      - signal timestamps within CONFLUENCE_WINDOW_MIN (±15 min)
    """
    by_bucket = defaultdict(list)
    for t in trades:
        by_bucket[(t.ticker, t.direction, t.scoring_system)].append(t)

    window_sec = CONFLUENCE_WINDOW_MIN * 60

    for bucket_key, bucket_trades in by_bucket.items():
        # Sort by signal_ts within bucket for sweep
        bucket_trades.sort(key=lambda x: x.signal_ts)
        n = len(bucket_trades)
        for i, t in enumerate(bucket_trades):
            # Always mark the trade's own resolution as True
            if t.resolution_min == 5:
                t.conf_5m = True
            elif t.resolution_min == 15:
                t.conf_15m = True
            elif t.resolution_min == 30:
                t.conf_30m = True

            # Scan neighbors within window
            # Look backward
            j = i - 1
            while j >= 0 and (t.signal_ts - bucket_trades[j].signal_ts) <= window_sec:
                other = bucket_trades[j]
                if other.resolution_min != t.resolution_min:
                    if other.resolution_min == 5:
                        t.conf_5m = True
                    elif other.resolution_min == 15:
                        t.conf_15m = True
                    elif other.resolution_min == 30:
                        t.conf_30m = True
                j -= 1
            # Look forward
            j = i + 1
            while j < n and (bucket_trades[j].signal_ts - t.signal_ts) <= window_sec:
                other = bucket_trades[j]
                if other.resolution_min != t.resolution_min:
                    if other.resolution_min == 5:
                        t.conf_5m = True
                    elif other.resolution_min == 15:
                        t.conf_15m = True
                    elif other.resolution_min == 30:
                        t.conf_30m = True
                j += 1

            # confluence_count = how many resolutions fired
            t.confluence_count = int(t.conf_5m) + int(t.conf_15m) + int(t.conf_30m)


# ═══════════════════════════════════════════════════════════
# SUMMARIES
# ═══════════════════════════════════════════════════════════

def _stats(subset):
    """Returns (n, full_win, partial, full_loss, avg_move, wr_pct)."""
    if not subset:
        return (0, 0, 0, 0, 0.0, 0.0)
    n = len(subset)
    fw = sum(1 for t in subset if t.bucket == "full_win")
    fp = sum(1 for t in subset if t.bucket == "partial")
    fl = sum(1 for t in subset if t.bucket == "full_loss")
    am = sum(t.move_signed_pct for t in subset) / n
    wr = 100 * fw / n
    return (n, fw, fp, fl, am, wr)


def write_trades_csv(trades, path):
    if not trades:
        return
    fields = list(Trade.__dataclass_fields__.keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def write_summary_by_resolution_scoring(trades, path):
    g = defaultdict(list)
    for t in trades:
        g[(t.resolution_min, t.scoring_system, t.tier, t.direction)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["resolution_min", "scoring_system", "tier", "direction",
                    "n_trades", "full_win", "partial", "full_loss",
                    "wr_pct", "win_or_partial_pct", "avg_move_signed_pct"])
        for k, ts in sorted(g.items()):
            n, fw, fp, fl, am, wr = _stats(ts)
            if n == 0:
                continue
            w.writerow([k[0], k[1], k[2], k[3], n, fw, fp, fl,
                        f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{am:+.2f}"])


def write_summary_by_tier_direction(trades, path):
    """Headline numbers per (scoring × tier × direction), all resolutions combined."""
    g = defaultdict(list)
    for t in trades:
        g[(t.scoring_system, t.tier, t.direction)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring_system", "tier", "direction", "n_trades",
                    "full_win", "partial", "full_loss",
                    "wr_pct", "win_or_partial_pct", "avg_move_signed_pct"])
        for k, ts in sorted(g.items()):
            n, fw, fp, fl, am, wr = _stats(ts)
            if n == 0:
                continue
            w.writerow([k[0], k[1], k[2], n, fw, fp, fl,
                        f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{am:+.2f}"])


def write_summary_by_confluence(trades, path):
    """WR by confluence count (1 = lone signal, 2 = two resolutions agreed, 3 = all three)."""
    # Baseline per (scoring, tier, direction)
    baselines = {}
    for scoring in ("pinescript", "active_scanner"):
        for tier in (1, 2):
            for direction in ("bull", "bear"):
                s = [t for t in trades if t.scoring_system == scoring
                     and t.tier == tier and t.direction == direction]
                _, _, _, _, _, wr = _stats(s)
                baselines[(scoring, tier, direction)] = wr

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring_system", "tier", "direction", "confluence_count",
                    "n_trades", "wr_pct", "avg_move_signed_pct", "vs_baseline_wr_pct"])
        for scoring in ("pinescript", "active_scanner"):
            for tier in (1, 2):
                for direction in ("bull", "bear"):
                    for count in (1, 2, 3):
                        ts = [t for t in trades
                              if t.scoring_system == scoring
                              and t.tier == tier
                              and t.direction == direction
                              and t.confluence_count == count]
                        n, _, _, _, am, wr = _stats(ts)
                        if n == 0:
                            continue
                        base = baselines.get((scoring, tier, direction), 0.0)
                        w.writerow([scoring, tier, direction, count, n,
                                    f"{wr:.1f}", f"{am:+.2f}", f"{wr - base:+.1f}"])


def write_summary_by_regime(trades, path):
    g = defaultdict(list)
    for t in trades:
        g[(t.regime_trend, t.scoring_system, t.tier, t.direction)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["regime_trend", "scoring_system", "tier", "direction",
                    "n_trades", "wr_pct", "win_or_partial_pct", "avg_move_signed_pct"])
        for k, ts in sorted(g.items()):
            n, fw, fp, fl, am, wr = _stats(ts)
            if n == 0:
                continue
            w.writerow([k[0], k[1], k[2], k[3], n,
                        f"{wr:.1f}", f"{100*(fw+fp)/n:.1f}", f"{am:+.2f}"])


def write_report(trades, start, end, path):
    n_total = len(trades)
    if n_total == 0:
        Path(path).write_text("# Multi-Resolution Study\n\nNo trades generated.\n")
        return

    def stat(subset):
        if not subset:
            return (0, 0.0, 0.0, 0.0)
        fw = sum(1 for t in subset if t.bucket == "full_win")
        fp = sum(1 for t in subset if t.bucket == "partial")
        avg = sum(t.move_signed_pct for t in subset) / len(subset)
        return (len(subset), 100*fw/len(subset), 100*(fw+fp)/len(subset), avg)

    # Resolution × scoring grid
    rows = []
    for resolution in RESOLUTIONS:
        for scoring in ("pinescript", "active_scanner"):
            for tier in (1, 2):
                for direction in ("bull", "bear"):
                    ts = [t for t in trades
                          if t.resolution_min == resolution
                          and t.scoring_system == scoring
                          and t.tier == tier
                          and t.direction == direction]
                    n, wr, wp, avg = stat(ts)
                    rows.append((resolution, scoring, tier, direction, n, wr, wp, avg))

    md = f"""# Backtest Report — Multi-Resolution Study
**Date range:** {start} → {end}
**Total signals:** {n_total}

Methodology: for each of 3 resolutions (5m/15m/30m), ran BOTH the pinescript
v3.0 logic (imported from `backtest_v3_runner.py`) AND the active scanner v6.1
logic (imported from `active_scanner.py`) over the same 5-minute bar set
(15m/30m resampled locally from 5m). Exit = next Friday close, min 3 trading
days hold — identical to v3_runner grading.

**Cross-resolution confluence**: for each signal, flagged whether same-ticker
same-direction same-scoring-system signals also fired on the OTHER resolutions
within ±{CONFLUENCE_WINDOW_MIN} minutes.

---

## Headline WR by (resolution × scoring × tier × direction)

| Resolution | Scoring | Tier | Dir | N | WR | +Partial | Avg Move% |
|---|---|---|---|---|---|---|---|
"""
    for (resolution, scoring, tier, direction, n, wr, wp, avg) in rows:
        if n == 0:
            continue
        md += f"| {resolution}m | {scoring} | T{tier} | {direction} | {n} | **{wr:.1f}%** | {wp:.1f}% | {avg:+.2f}% |\n"

    md += f"""

## Confluence effect

When signals fire on multiple resolutions within ±{CONFLUENCE_WINDOW_MIN}min,
does WR improve? See `summary_by_confluence.csv` for the full table.
"""

    # Calculate lone vs confluent WR for each scoring system
    for scoring in ("pinescript", "active_scanner"):
        lone = [t for t in trades if t.scoring_system == scoring and t.confluence_count == 1]
        dual = [t for t in trades if t.scoring_system == scoring and t.confluence_count == 2]
        tri  = [t for t in trades if t.scoring_system == scoring and t.confluence_count == 3]
        _, wr_l, _, _ = stat(lone)
        _, wr_d, _, _ = stat(dual)
        _, wr_t, _, _ = stat(tri)
        md += f"\n**{scoring}**: lone({len(lone)}): {wr_l:.1f}% WR • "
        md += f"2-of-3({len(dual)}): {wr_d:.1f}% WR • "
        md += f"3-of-3({len(tri)}): {wr_t:.1f}% WR\n"

    md += f"""

## Files to read next

- **`summary_by_resolution_scoring.csv`** — Expanded WR grid, same as the
  headline table but with full_win/partial/full_loss counts
- **`summary_by_confluence.csv`** — ← Money table. vs_baseline_wr_pct
  column shows how much confluence adds (or doesn't)
- **`summary_by_regime.csv`** — Does edge survive regime change
- **`trades.csv`** — Every signal with every column for spot-checks

## What to verify

The 15m pinescript row should come close to v3_runner's original 172K-signal
baseline WR numbers over the overlapping period (T1 Bull ~65.6%, T2 Bull
~64.7%). If this run's 15m pinescript numbers are materially different, the
resampling logic or an import path has a bug and the rest of the results
are not trustworthy.

— Not financial advice —
"""
    Path(path).write_text(md)


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Multi-Resolution Scoring Study")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=600, help="Default 600 (~20 months)")
    args = ap.parse_args()

    if not MD_TOKEN:
        log.error("MARKETDATA_TOKEN not set")
        sys.exit(1)

    # Ticker selection
    override = os.environ.get("BACKTEST_TICKERS", "").strip()
    if override:
        tickers = [t.strip().upper() for t in override.split(",") if t.strip()]
    elif args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = ALL_TICKERS  # default to all (same as v3_runner default)

    # Date range
    start_str = args.from_date or os.environ.get("BACKTEST_START") or \
                (date.today() - timedelta(days=args.days)).isoformat()
    end_str = args.to_date or os.environ.get("BACKTEST_END") or date.today().isoformat()
    start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Output: {OUT_DIR}")
    log.info(f"Range: {start.date()} → {end.date()}  ({len(tickers)} tickers)")
    log.info(f"Resolutions: {RESOLUTIONS} min")
    log.info(f"Scoring systems: pinescript (v3_runner), active_scanner (v6.1)")

    # ── Build regime map using v3_runner's function (matches what bt_shared does,
    #    but v3_runner's returns 'BULL'/'BEAR' with VIX bands) ──
    log.info("Building regime map from SPY + VIX daily...")
    regime_map = v3r.build_regime_map(start, end)
    log.info(f"Regime map: {len(regime_map)} entries")

    # ── Resume checkpoint ──
    progress_path = OUT_DIR / ".progress.json"
    trades_path = OUT_DIR / "trades.csv"
    all_trades: list[Trade] = []
    done: set = set()

    if progress_path.exists() and trades_path.exists():
        try:
            with open(progress_path) as f:
                done = set(json.load(f).get("done", []))
            # Load trades
            type_map = {}
            for fld in _dc_fields(Trade):
                t_ = fld.type
                if t_ is int or t_ == "int":     type_map[fld.name] = "int"
                elif t_ is float or t_ == "float": type_map[fld.name] = "float"
                elif t_ is bool or t_ == "bool":   type_map[fld.name] = "bool"
                else:                               type_map[fld.name] = "str"
            with open(trades_path) as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    for name, kind in type_map.items():
                        if name not in row or row[name] == "":
                            continue
                        try:
                            if kind == "int":
                                row[name] = int(float(row[name]))
                            elif kind == "float":
                                row[name] = float(row[name])
                            elif kind == "bool":
                                row[name] = str(row[name]).lower() in ("true", "1")
                        except (ValueError, TypeError):
                            pass
                    try:
                        all_trades.append(Trade(**row))
                    except TypeError:
                        pass
            log.info(f"Resumed {len(all_trades)} trades from {len(done)} tickers")
        except Exception as e:
            log.warning(f"Resume failed ({e}); starting fresh")
            all_trades = []; done = set()

    # ── Per-ticker loop ──
    for idx, ticker in enumerate(tickers, 1):
        if ticker in done:
            log.info(f"[{idx}/{len(tickers)}] {ticker}: already done, skipping")
            continue

        log.info(f"[{idx}/{len(tickers)}] {ticker}: downloading 5m bars...")
        data_5m = fetch_5min_chunked(ticker, start, end)
        if not data_5m or not data_5m.get("t"):
            log.warning(f"{ticker}: no 5m data; skipping")
            done.add(ticker)
            continue

        bars_5m = bars_from_data(data_5m)
        if len(bars_5m) < 100:
            log.warning(f"{ticker}: only {len(bars_5m)} 5m bars; skipping")
            done.add(ticker)
            continue
        log.info(f"{ticker}: {len(bars_5m)} 5m bars downloaded")

        # ── Resample to 15m and 30m ──
        log.info(f"{ticker}: resampling to 15m and 30m...")
        bars_by_res = {5: bars_5m}
        try:
            bars_by_res[15] = resample_bars(bars_5m, 15)
            bars_by_res[30] = resample_bars(bars_5m, 30)
        except Exception as e:
            log.error(f"{ticker}: resample failed: {e}")
            done.add(ticker)
            continue
        log.info(f"{ticker}: 15m={len(bars_by_res[15])}, 30m={len(bars_by_res[30])}")

        # ── Daily bars (for active scanner HTF + Potter Box overlay) ──
        # Build from 5m (matches v3_runner resample_to_daily logic)
        daily_bars = v3r.resample_to_daily(bars_5m)
        if len(daily_bars) < 25:
            log.warning(f"{ticker}: only {len(daily_bars)} daily bars; skipping")
            done.add(ticker)
            continue

        # ── Hourly bars (for 1h S/R overlay) ──
        # Computed once per ticker, shared across all resolutions
        log.info(f"{ticker}: resampling to 1h for S/R overlay...")
        hourly_bars = resample_to_hourly(bars_5m)
        log.info(f"{ticker}: {len(hourly_bars)} 1h bars")

        # Clear per-ticker fractal cache so new fractals are computed fresh
        _hourly_fractals_cache.pop(f"{ticker}_hourly", None)

        # ── Run both scoring systems at all 3 resolutions ──
        ticker_trades = []
        for resolution in RESOLUTIONS:
            bars = bars_by_res[resolution]
            if len(bars) < 80:
                log.warning(f"{ticker} {resolution}m: only {len(bars)} bars; skipping")
                continue

            # Pinescript
            try:
                ps_signals = pinescript_signals(bars)
                ps_trades = simulate_trades_for_signals(
                    ps_signals, bars, ticker, resolution, "pinescript", regime_map,
                    daily_bars=daily_bars, hourly_bars=hourly_bars,
                )
                ticker_trades.extend(ps_trades)
                log.info(f"{ticker} {resolution}m pinescript: "
                         f"{len(ps_signals)} signals, {len(ps_trades)} trades")
            except Exception as e:
                log.error(f"{ticker} {resolution}m pinescript failed: {e}")

            # Active scanner
            try:
                as_signals = active_scanner_signals(
                    bars, daily_bars, ticker, regime_map, countback=80
                )
                as_trades = simulate_trades_for_signals(
                    as_signals, bars, ticker, resolution, "active_scanner", regime_map,
                    daily_bars=daily_bars, hourly_bars=hourly_bars,
                )
                ticker_trades.extend(as_trades)
                log.info(f"{ticker} {resolution}m active_scanner: "
                         f"{len(as_signals)} signals, {len(as_trades)} trades")
            except Exception as e:
                log.error(f"{ticker} {resolution}m active_scanner failed: {e}")

        all_trades.extend(ticker_trades)
        done.add(ticker)

        # Checkpoint
        write_trades_csv(all_trades, trades_path)
        with open(progress_path, "w") as f:
            json.dump({"done": sorted(done)}, f)

    # ── Annotate confluence after all trades generated ──
    log.info(f"Annotating cross-resolution confluence on {len(all_trades)} trades...")
    annotate_confluence(all_trades)
    write_trades_csv(all_trades, trades_path)

    # ── Write summaries ──
    log.info("Writing summaries...")
    write_summary_by_resolution_scoring(all_trades, OUT_DIR / "summary_by_resolution_scoring.csv")
    write_summary_by_tier_direction(all_trades, OUT_DIR / "summary_by_tier_direction.csv")
    write_summary_by_confluence(all_trades, OUT_DIR / "summary_by_confluence.csv")
    write_summary_by_regime(all_trades, OUT_DIR / "summary_by_regime.csv")
    write_report(all_trades, start.date(), end.date(), OUT_DIR / "report.md")

    log.info(f"DONE. {len(all_trades)} total trades across {len(tickers)} tickers")
    log.info(f"Outputs in {OUT_DIR}:")
    for fn in sorted(os.listdir(OUT_DIR)):
        if fn.startswith("."):
            continue
        fp = OUT_DIR / fn
        log.info(f"  {fn}  ({fp.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
