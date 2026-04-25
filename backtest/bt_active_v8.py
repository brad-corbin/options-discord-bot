#!/usr/bin/env python3
"""
bt_active_v8.py — Active Scanner Backtest v8 (UNIFIED OVERLAYS)
════════════════════════════════════════════════════════════════
Faithful line-by-line port of active_scanner.py _analyze_ticker() as of
v6.1 (2026-04-08 production), with overlay enrichment columns added
on every trade for confluence analysis.

WHY THIS EXISTS:
  The existing backtest/bt_active.py has its own `detect_signal()` that
  duplicates the scanner's logic. That duplicate has drifted from the
  live code over time. This file imports the scanner's own helper
  functions directly (no duplication) and runs them on historical bars
  the same way the scanner runs them on live bars.

WHAT IT ADDS vs bt_active.py:
  - Overlay columns on every trade:
      * Potter Box state (in_box / above_roof / below_floor / no_box)
      * CB side (above_cb / below_cb) — v3_runner's highest-impact filter
      * Wave label (established / weakening / breakout_probable / imminent)
      * Fib proximity (nearest level + distance %)
      * Swing hi/lo proximity
      * Credit spread outcomes at Potter Box boundaries
      * RSI / MACD / EMA / ADX quintile bins for per-indicator WR
  - Same v3_runner-style output: trades.csv + 7 summary CSVs + report.md
  - Resume-from-checkpoint
  - `--all` flag runs all watchlist tickers

USAGE (Render shell, matches v3_runner):
    cd /opt/render/project/src
    python bt_active_v8.py                  # defaults: all watchlist, 9 months back
    python bt_active_v8.py --days 180       # 6-month lookback
    python bt_active_v8.py --ticker NVDA    # single ticker
    BACKTEST_TICKERS=SPY,QQQ python bt_active_v8.py

ENV:
    MARKETDATA_TOKEN        (required if Schwab unavailable)
    SCHWAB_APP_KEY/SECRET   (optional but preferred — no throttle)
    BACKTEST_START          YYYY-MM-DD (optional; default 270 days back)
    BACKTEST_END            YYYY-MM-DD (optional; default today)
    BACKTEST_TICKERS        comma-separated override

OUTPUTS: /tmp/backtest_active_v8/
    trades.csv
    summary_by_ticker.csv
    summary_by_regime.csv
    summary_by_tier.csv
    summary_by_htf_status.csv
    summary_by_confluence.csv     ← the money table
    summary_by_indicator.csv      ← quintile WR per indicator
    summary_by_credit.csv
    report.md
    .progress.json (for resume)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict, fields as _dc_fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────
# BOOTSTRAP — import from repo root so we get the LIVE scanner helpers
# ─────────────────────────────────────────────────────────────
BOT_REPO_PATH = os.environ.get("BOT_REPO_PATH", "/opt/render/project/src")
if BOT_REPO_PATH not in sys.path:
    sys.path.insert(0, BOT_REPO_PATH)

# bt_shared lives inside backtest/
BACKTEST_DIR = Path(BOT_REPO_PATH) / "backtest"
if str(BACKTEST_DIR) not in sys.path:
    sys.path.insert(0, str(BACKTEST_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bt_active_v8")

# ── Live scanner helpers ──
try:
    from active_scanner import (
        _compute_ema, _compute_rsi, _compute_macd, _compute_wavetrend, _compute_adx,
        EMA_FAST, EMA_SLOW, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
        RSI_PERIOD, WT_CHANNEL, WT_AVERAGE,
        SIGNAL_TIER_1_SCORE, SIGNAL_TIER_2_SCORE, MIN_SIGNAL_SCORE,
    )
    log.info(f"Loaded LIVE scanner helpers from {BOT_REPO_PATH}/active_scanner.py")
    log.info(f"  thresholds: MIN={MIN_SIGNAL_SCORE} T2={SIGNAL_TIER_2_SCORE} T1={SIGNAL_TIER_1_SCORE}")
except ImportError as e:
    log.error(f"Cannot import active_scanner.py: {e}")
    log.error(f"Set BOT_REPO_PATH to the directory containing active_scanner.py")
    sys.exit(1)

# ── Live Potter Box ──
_detect_boxes_live = None
try:
    from potter_box import detect_boxes as _detect_boxes_live
    log.info(f"Loaded LIVE Potter Box engine from {BOT_REPO_PATH}/potter_box.py")
except ImportError as e:
    log.warning(f"Could not import potter_box.py ({e}). Overlay columns will be empty.")

# ── Shared bt utilities ──
try:
    from bt_shared import (
        download_5min, download_daily, download_vix,
        compute_regime_for_date, get_ticker_rule, is_signal_valid_for_regime,
        exit_dates_for, EXIT_DAYS,
        is_market_bar, time_phase,
    )
    log.info(f"Loaded bt_shared utilities from {BACKTEST_DIR}/bt_shared.py")
except ImportError as e:
    log.error(f"Cannot import bt_shared: {e}")
    log.error(f"Ensure {BACKTEST_DIR}/bt_shared.py exists")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════
# CONSTANTS (mirror active_scanner.py + v3_runner conventions)
# ═══════════════════════════════════════════════════════════

ALL_TICKERS = [
    "AAPL", "AMD", "AMZN", "ARM", "AVGO", "BA", "CAT", "COIN", "CRM", "DIA",
    "GLD", "GOOGL", "GS", "IWM", "JPM", "LLY", "META", "MRNA", "MSFT", "MSTR",
    "NFLX", "NVDA", "ORCL", "PLTR", "QQQ", "SMCI", "SOFI", "SOXX", "SPY",
    "TLT", "TSLA", "UNH", "XLE", "XLF", "XLV",
]

DEDUP_BARS = 3       # don't re-emit same-bias signal within N bars
MIN_ADTV_DOLLARS = 5_000_000   # same as active_scanner (non-SPY/QQQ/IWM/DIA)

# Potter Box CB side tolerance (pct of midpoint); identical to v3_runner
CB_TIE_PCT = 0.10   # within 0.10% of midpoint = "at_cb"

# Fib lookback for overlay
FIB_LOOKBACK_DAYS = 34
FIB_LEVELS = [23.6, 38.2, 50.0, 61.8, 78.6]
SWING_FRACTAL_ORDER = 3

OUT_DIR = Path(os.environ.get("BACKTEST_OUT_DIR", "/tmp/backtest_active_v8"))


# ═══════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════

@dataclass
class Trade:
    # Entry identity
    ticker: str
    signal_date: str          # YYYY-MM-DD
    signal_time_ct: str       # HH:MM
    signal_ts: int            # unix
    entry_price: float
    bias: str                 # "bull" / "bear"
    tier: str                 # "1" / "2"
    score: int
    # Regime + quality
    regime: str               # "BULL" / "TRANSITION" / "BEAR"
    regime_valid: bool        # ticker rules passed?
    regime_reason: str
    phase: str                # MORNING / MIDDAY / AFTERNOON / UNKNOWN
    data_quality: str         # full / partial / minimal
    # Score breakdown (critical for debugging what drove the score)
    sb_ema: int = 0
    sb_macd_hist: int = 0
    sb_macd_cross: int = 0
    sb_wt: int = 0
    sb_vwap: int = 0
    sb_htf: int = 0
    sb_volume: int = 0
    sb_rsi: int = 0
    sb_flow: int = 0
    # Indicator values at signal (for quintile analysis)
    ema_dist_pct: float = 0.0
    macd_hist: float = 0.0
    macd_cross_bull: bool = False
    macd_cross_bear: bool = False
    wt1: float = 0.0
    wt2: float = 0.0
    rsi: float = 50.0
    adx: float = 0.0
    volume_ratio: float = 1.0
    htf_status: str = "UNKNOWN"       # CONFIRMED / CONVERGING / OPPOSING / UNKNOWN
    htf_confirmed: bool = False
    htf_converging: bool = False
    daily_bull: bool = False
    above_vwap: bool = False
    # Exit outcomes at multiple horizons (eod, 1d, 2d, 3d, 5d)
    exit_date_eod: str = ""
    exit_price_eod: float = 0.0
    pnl_pct_eod: float = 0.0
    win_eod: bool = False
    exit_date_1d: str = ""
    exit_price_1d: float = 0.0
    pnl_pct_1d: float = 0.0
    win_1d: bool = False
    exit_date_2d: str = ""
    exit_price_2d: float = 0.0
    pnl_pct_2d: float = 0.0
    win_2d: bool = False
    exit_date_3d: str = ""
    exit_price_3d: float = 0.0
    pnl_pct_3d: float = 0.0
    win_3d: bool = False
    exit_date_5d: str = ""
    exit_price_5d: float = 0.0
    pnl_pct_5d: float = 0.0
    win_5d: bool = False
    # MFE / MAE on entry day (intraday)
    mfe_eod_pct: float = 0.0
    mae_eod_pct: float = 0.0
    # ── Potter Box overlay ──
    pb_state: str = "no_box"              # in_box / above_roof / below_floor / post_box / no_box
    pb_floor: float = 0.0
    pb_roof: float = 0.0
    pb_midpoint: float = 0.0              # CB line
    pb_range_pct: float = 0.0
    pb_duration_bars: int = 0
    pb_max_touches: int = 0
    pb_wave_label: str = "none"           # established / weakening / breakout_probable / breakout_imminent
    pb_break_confirmed: bool = False
    # CB side (only meaningful when in_box)
    cb_side: str = "n/a"                  # above_cb / below_cb / at_cb / n/a
    cb_distance_pct: float = 0.0
    # ── Fib overlay ──
    fib_nearest_level: str = "none"       # 23.6 / 38.2 / 50.0 / 61.8 / 78.6
    fib_distance_pct: float = 100.0
    fib_above_or_below: str = "unknown"
    # ── Swing proximity ──
    swing_dist_above_pct: float = 999.0
    swing_dist_below_pct: float = 999.0
    # ── Credit spread outcomes (only when in_box) ──
    credit_short_strike: float = 0.0
    credit_25_bucket: str = "n/a"         # full_win / partial / full_loss / n/a
    credit_25_win_5d: bool = False
    credit_50_bucket: str = "n/a"
    credit_50_win_5d: bool = False
    # ── Confluence bucket label (for summary) ──
    confluence_bucket: str = "none"


# ═══════════════════════════════════════════════════════════
# VWAP (intraday, session-based) — mirrors active_scanner's inline calc
# ═══════════════════════════════════════════════════════════

def _compute_session_vwap(bars_window: list) -> Optional[float]:
    """Given a 5-min bar window (list of dicts with h/l/c/v), compute
    cumulative typical-price VWAP in the same manner active_scanner does.

    Active scanner does: sum((h+l+c)/3 * v) / sum(v) over the whole window.
    That's effectively session VWAP when the window spans the current session.
    """
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


# ═══════════════════════════════════════════════════════════
# DETECT SIGNAL — line-for-line port of active_scanner._analyze_ticker
# ═══════════════════════════════════════════════════════════

def detect_signal_backtest(window_bars: list, daily_closes: list, regime: str, ticker: str) -> Optional[dict]:
    """Port of active_scanner._analyze_ticker to operate on a historical
    5-min bar window + recent daily closes.

    Differences from live scanner:
      - No streaming spot override (use last bar close as spot)
      - No flow boost (flow data not backtestable)
      - No phase from wall clock — derived from last bar's time instead
      - Log level is debug (backtest rejects are expected volume)

    All scoring thresholds, indicator formulas, regime-specific branches
    (TRANSITION CONVERGING, RSI window shifts) come from the LIVE
    `_compute_*` helpers and match production identically.
    """
    if len(window_bars) < 12:
        return None  # insufficient_bars

    closes  = [b["c"] for b in window_bars if b.get("c") is not None]
    highs   = [b["h"] for b in window_bars if b.get("h") is not None]
    lows    = [b["l"] for b in window_bars if b.get("l") is not None]
    volumes = [b.get("v", 0) or 0 for b in window_bars]

    if len(closes) < 12:
        return None

    spot = closes[-1]
    bar_count = len(closes)
    data_quality = "full" if bar_count >= 40 else ("partial" if bar_count >= 20 else "minimal")

    # ADTV gate (same thresholds as live scanner)
    if volumes and len(volumes) >= 10:
        avg_vol_10 = sum(volumes[-10:]) / 10
        adtv = avg_vol_10 * spot * 5 * 60
        if adtv < MIN_ADTV_DOLLARS and ticker not in ("SPY", "QQQ", "IWM", "DIA"):
            return None

    # Session VWAP
    vwap = _compute_session_vwap(window_bars)

    # EMA signals
    ema5  = _compute_ema(closes, EMA_FAST)
    ema12 = _compute_ema(closes, EMA_SLOW)
    if not ema5 or not ema12:
        return None

    ema_bull     = ema5[-1] > ema12[-1]
    ema_dist_pct = ((ema5[-1] - ema12[-1]) / ema12[-1]) * 100 if ema12[-1] > 0 else 0.0

    # MACD
    macd = _compute_macd(closes)

    # WaveTrend (from hlc3)
    hlc3 = [(highs[i] + lows[i] + closes[i]) / 3
            for i in range(min(len(highs), len(lows), len(closes)))]
    wt = _compute_wavetrend(hlc3)

    # RSI (on 5-min closes; 14-period)
    rsi = _compute_rsi(closes, RSI_PERIOD)

    # ADX (same live scanner helper)
    adx_current = _compute_adx(highs, lows, closes, length=14)

    # Volume ratio
    avg_vol      = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    current_vol  = volumes[-1] if volumes else 0
    volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

    # Phase from last bar's time
    try:
        last_time = window_bars[-1].get("time_ct", "")
        if last_time:
            hh = int(last_time.split(":")[0])
            if 8 <= hh < 11:
                phase = "MORNING"
            elif 11 <= hh < 14:
                phase = "MIDDAY"
            elif 14 <= hh < 16:
                phase = "AFTERNOON"
            else:
                phase = "UNKNOWN"
        else:
            phase = "UNKNOWN"
    except Exception:
        phase = "UNKNOWN"

    # Daily trend / HTF status (mirrors live logic exactly)
    daily_bull     = None
    htf_confirmed  = False
    htf_converging = False
    htf_status     = "UNKNOWN"
    if daily_closes and len(daily_closes) >= 21:
        daily_ema8  = _compute_ema(daily_closes, 8)
        daily_ema21 = _compute_ema(daily_closes, 21)
        if daily_ema8 and daily_ema21 and len(daily_ema8) >= 2:
            daily_bull    = daily_ema8[-1] > daily_ema21[-1]
            htf_confirmed = (daily_bull == ema_bull)

            if htf_confirmed:
                htf_status = "CONFIRMED"
            else:
                daily_gap_now  = abs(daily_ema8[-1] - daily_ema21[-1])
                daily_gap_prev = abs(daily_ema8[-2] - daily_ema21[-2])
                if daily_gap_now < daily_gap_prev * 0.98:
                    htf_converging = True
                    htf_status = "CONVERGING"
                else:
                    htf_status = "OPPOSING"

    # ────────── SCORING (mirrors live scanner) ──────────
    score = 0
    bias  = "bull" if ema_bull else "bear"
    sb    = {}

    # EMA distance
    if abs(ema_dist_pct) > 0.03:
        score += 15; sb["ema"] = 15
    elif abs(ema_dist_pct) > 0.01:
        score += 8;  sb["ema"] = 8
    else:
        sb["ema"] = 0

    # MACD histogram + cross
    if macd:
        if bias == "bull" and macd.get("macd_hist", 0) > 0:
            score += 15; sb["macd_hist"] = 15
        elif bias == "bear" and macd.get("macd_hist", 0) < 0:
            score += 15; sb["macd_hist"] = 15
        elif macd.get("macd_hist", 0) != 0:
            score -= 10; sb["macd_hist"] = -10
        else:
            sb["macd_hist"] = 0

        if macd.get("macd_cross_bull") and bias == "bull":
            score += 10; sb["macd_cross"] = 10
        elif macd.get("macd_cross_bear") and bias == "bear":
            score += 10; sb["macd_cross"] = 10
        else:
            sb["macd_cross"] = 0
    else:
        sb["macd_hist"] = 0; sb["macd_cross"] = 0

    # WaveTrend
    if wt:
        if bias == "bull" and wt.get("wt_oversold"):
            score += 15; sb["wt"] = 15
        elif bias == "bear" and wt.get("wt_overbought"):
            score += 15; sb["wt"] = 15
        elif bias == "bull" and wt.get("wt_overbought"):
            score -= 10; sb["wt"] = -10
        elif bias == "bear" and wt.get("wt_oversold"):
            score -= 10; sb["wt"] = -10
        elif bias == "bull" and wt.get("wt_cross_bull"):
            score += 10; sb["wt"] = 10
        elif bias == "bear" and wt.get("wt_cross_bear"):
            score += 10; sb["wt"] = 10
        else:
            sb["wt"] = 0
    else:
        sb["wt"] = 0

    # VWAP side
    if vwap:
        if bias == "bull" and spot > vwap:
            score += 10; sb["vwap"] = 10
        elif bias == "bear" and spot < vwap:
            score += 10; sb["vwap"] = 10
        elif bias == "bull" and spot < vwap:
            score -= 5;  sb["vwap"] = -5
        elif bias == "bear" and spot > vwap:
            score -= 5;  sb["vwap"] = -5
        else:
            sb["vwap"] = 0
    else:
        sb["vwap"] = 0

    # HTF (regime-aware — matches v6.1 P2 fix)
    if htf_confirmed:
        score += 15; sb["htf"] = 15
    elif htf_converging and regime == "TRANSITION":
        # P2: CONVERGING in TRANSITION gets +12 instead of -10 penalty
        score += 12; sb["htf"] = 12
    elif daily_bull is not None:
        if (bias == "bull" and daily_bull) or (bias == "bear" and not daily_bull):
            score += 10; sb["htf"] = 10
        else:
            score -= 10; sb["htf"] = -10
    else:
        sb["htf"] = 0

    # Volume ratio
    if volume_ratio > 1.5:
        score += 10; sb["volume"] = 10
    elif volume_ratio > 1.0:
        score += 5;  sb["volume"] = 5
    else:
        sb["volume"] = 0

    # RSI (regime-aware — matches v6.1 P4 fix)
    if rsi:
        if regime == "TRANSITION" and bias == "bull":
            # P4: TRANSITION bull RSI window is 50-75 (not 40-65)
            if 50 < rsi < 75:
                score += 5; sb["rsi"] = 5
            elif rsi < 45:
                score -= 5; sb["rsi"] = -5
            else:
                sb["rsi"] = 0
        elif bias == "bull" and 40 < rsi < 65:
            score += 5; sb["rsi"] = 5
        elif bias == "bear" and 35 < rsi < 60:
            score += 5; sb["rsi"] = 5
        else:
            sb["rsi"] = 0
    else:
        sb["rsi"] = 0

    # Flow boost — not backtestable, always 0
    sb["flow"] = 0

    # Threshold gate
    if score < MIN_SIGNAL_SCORE:
        return None

    tier = "1" if score >= SIGNAL_TIER_1_SCORE else "2"

    return {
        "bias": bias,
        "tier": tier,
        "score": score,
        "sb": sb,
        "data_quality": data_quality,
        "bar_count": bar_count,
        "close": spot,
        "phase": phase,
        "ema_dist_pct": round(ema_dist_pct, 3),
        "macd_hist": macd.get("macd_hist", 0) if macd else 0,
        "macd_cross_bull": macd.get("macd_cross_bull", False) if macd else False,
        "macd_cross_bear": macd.get("macd_cross_bear", False) if macd else False,
        "wt1": wt.get("wt1", 0) if wt else 0,
        "wt2": wt.get("wt2", 0) if wt else 0,
        "rsi": rsi if rsi else 50.0,
        "adx": round(adx_current, 2),
        "volume_ratio": round(volume_ratio, 2),
        "vwap": vwap,
        "above_vwap": (spot > vwap) if vwap else False,
        "htf_confirmed": htf_confirmed,
        "htf_converging": htf_converging,
        "htf_status": htf_status,
        "daily_bull": daily_bull,
    }


# ═══════════════════════════════════════════════════════════
# OVERLAY COMPUTATION (Potter Box, CB side, Fib, swing)
# ═══════════════════════════════════════════════════════════

_pb_cache = {}

def _potter_for_ticker(daily_bars, ticker):
    """Cache detect_boxes() result per ticker. Called once, reused for all signals."""
    key = (ticker, len(daily_bars))
    if key in _pb_cache:
        return _pb_cache[key]
    if _detect_boxes_live is None:
        _pb_cache[key] = []
        return []
    try:
        boxes = _detect_boxes_live(daily_bars, ticker)
    except Exception as e:
        log.warning(f"detect_boxes failed for {ticker}: {e}")
        boxes = []
    _pb_cache[key] = boxes
    return boxes


def _potter_state_at(daily_bars, idx, ticker, spot_at_signal):
    """Return a dict of Potter Box fields at daily bar idx.

    Classification mirrors v3_runner:
      - in_box:      signal fires during active consolidation
      - above_roof:  box broke upward, signal fires post-confirmed-break
      - below_floor: box broke downward, signal fires post-confirmed-break
      - post_box:    box ended, no confirmed break yet
      - no_box:      no box detected at or before this date
    """
    empty = {
        "state": "no_box", "floor": 0.0, "roof": 0.0, "midpoint": 0.0,
        "range_pct": 0.0, "duration_bars": 0, "max_touches": 0,
        "wave_label": "none", "break_confirmed": False,
        "cb_side": "n/a", "cb_distance_pct": 0.0,
    }
    if idx < 0 or idx >= len(daily_bars):
        return empty

    boxes = _potter_for_ticker(daily_bars, ticker)
    if not boxes:
        return empty

    relevant = [b for b in boxes if b.get("start_idx", -1) <= idx]
    if not relevant:
        return empty
    box = relevant[-1]

    start_idx = box.get("start_idx", -1)
    end_idx   = box.get("end_idx", -1)
    floor     = float(box.get("floor", 0))
    roof      = float(box.get("roof", 0))
    midpoint  = float(box.get("midpoint", (roof + floor) / 2 if roof and floor else 0))
    broken    = bool(box.get("broken", False))
    confirmed = bool(box.get("break_confirmed", False))
    break_dir = box.get("break_direction")

    # Classify
    if start_idx <= idx <= end_idx:
        state = "in_box"
    elif broken and confirmed:
        if break_dir == "up":
            state = "above_roof"
        elif break_dir == "down":
            state = "below_floor"
        else:
            state = "post_box"
    else:
        # Within 5 bars of end = still treat as in_box (might resolve)
        bars_since_end = idx - end_idx
        state = "in_box" if bars_since_end <= 5 else "post_box"

    # CB side (only meaningful when in_box)
    cb_side = "n/a"
    cb_dist_pct = 0.0
    if state == "in_box" and midpoint > 0 and spot_at_signal > 0:
        dist_from_mid_pct = abs(spot_at_signal - midpoint) / spot_at_signal * 100.0
        if dist_from_mid_pct <= CB_TIE_PCT:
            cb_side = "at_cb"
        elif spot_at_signal > midpoint:
            cb_side = "above_cb"
        else:
            cb_side = "below_cb"
        cb_dist_pct = dist_from_mid_pct

    return {
        "state": state, "floor": floor, "roof": roof, "midpoint": midpoint,
        "range_pct": float(box.get("range_pct", 0)),
        "duration_bars": int(box.get("duration_bars", 0)),
        "max_touches": int(box.get("max_touches", 0)),
        "wave_label": str(box.get("wave_label", "none")),
        "break_confirmed": confirmed,
        "cb_side": cb_side, "cb_distance_pct": cb_dist_pct,
    }


def _fib_state_at(daily_bars, idx):
    """Nearest Fibonacci level over a 34-day lookback."""
    if idx < FIB_LOOKBACK_DAYS:
        return {"level": "none", "distance_pct": 100.0, "above_below": "unknown"}
    w = daily_bars[idx - FIB_LOOKBACK_DAYS + 1: idx + 1]
    sh = max(b["h"] for b in w); sl = min(b["l"] for b in w)
    spot = daily_bars[idx]["c"]
    if sh <= sl or spot <= 0:
        return {"level": "none", "distance_pct": 100.0, "above_below": "unknown"}
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
    return {"level": f"{best_lv}", "distance_pct": best_d, "above_below": ab}


def _find_swings(daily_bars, order=SWING_FRACTAL_ORDER):
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


def _swing_state_at(daily_bars, idx, highs, lows):
    spot = daily_bars[idx]["c"]
    if spot <= 0:
        return {"above_pct": 999.0, "below_pct": 999.0}
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
    return {"above_pct": da, "below_pct": db}


def _grade_credit(direction, short, long_strike, exit_price):
    """Same as v3_runner. Returns (bucket, is_win)."""
    if short <= 0 or long_strike <= 0:
        return "n/a", False
    if direction == "bull":
        if exit_price >= short:   return "full_win", True
        elif exit_price > long_strike:  return "partial", False
        else:                      return "full_loss", False
    else:
        if exit_price <= short:   return "full_win", True
        elif exit_price < long_strike:  return "partial", False
        else:                      return "full_loss", False


def _classify_confluence(direction, pb_state):
    if direction == "bull" and pb_state == "above_roof":
        return "pb_aligned_bull"
    if direction == "bear" and pb_state == "below_floor":
        return "pb_aligned_bear"
    if direction == "bull" and pb_state == "below_floor":
        return "pb_opposed"
    if direction == "bear" and pb_state == "above_roof":
        return "pb_opposed"
    if pb_state == "in_box":
        return "pb_in_box"
    if pb_state == "no_box":
        return "pb_no_box"
    return "none"


# ═══════════════════════════════════════════════════════════
# MAIN BACKTEST LOOP
# ═══════════════════════════════════════════════════════════

def run_ticker(ticker: str, intraday: list, daily: list, regime_cache: dict,
               daily_close_by_date: dict, sorted_dates: list) -> list[Trade]:
    """Run the active scanner over all 5-min bars for one ticker.

    At each bar (after the first DEDUP_BARS), call detect_signal_backtest
    with the last 80 bars (same window size as live scanner's countback=80).
    Exits graded at eod/1d/2d/3d/5d horizons.
    """
    trades: list[Trade] = []
    last_sig_bar: dict = {}

    # Group intraday bars by date
    bars_by_date: dict = {}
    for b in intraday:
        bars_by_date.setdefault(b["date"], []).append(b)

    # Precompute daily bar index for Potter Box lookup
    daily_date_to_idx = {b["date"]: i for i, b in enumerate(daily)}
    swing_highs, swing_lows = _find_swings(daily) if daily else ([], [])

    for trade_date in sorted(bars_by_date.keys()):
        day_bars = bars_by_date[trade_date]
        regime = regime_cache.get(trade_date, "BEAR")

        # Daily close series up to the day BEFORE (HTF lookup uses day-before context)
        dc = [daily_close_by_date[d] for d in sorted_dates if d < trade_date]

        for i in range(DEDUP_BARS, len(day_bars)):
            # Scanner uses countback=80 5-min bars — emulate that here
            window_start = max(0, i - 79)
            window = day_bars[window_start: i + 1]

            sig = detect_signal_backtest(window, dc[-30:], regime, ticker)
            if sig is None:
                continue

            # Dedup
            key = (ticker, sig["bias"])
            if key in last_sig_bar and i - last_sig_bar[key] < DEDUP_BARS:
                continue
            last_sig_bar[key] = i

            # Ticker-rules regime gate
            try:
                valid, reason = is_signal_valid_for_regime(
                    ticker, sig["bias"], sig["score"], sig["htf_status"], regime
                )
            except Exception as e:
                valid = True; reason = f"rule_check_failed: {e}"

            entry_price = sig["close"]
            entry_time_ct = day_bars[i].get("time_ct", "")

            # Exit grading
            exits = exit_dates_for(trade_date, sorted_dates)
            exit_fields = {}
            for label, exit_date in exits.items():
                if exit_date and exit_date in daily_close_by_date:
                    exit_p = daily_close_by_date[exit_date]
                    if sig["bias"] == "bull":
                        pnl = exit_p - entry_price
                    else:
                        pnl = entry_price - exit_p
                    pnl_pct = (pnl / entry_price) * 100 if entry_price > 0 else 0.0
                    exit_fields[f"exit_date_{label}"] = exit_date
                    exit_fields[f"exit_price_{label}"] = round(exit_p, 4)
                    exit_fields[f"pnl_pct_{label}"] = round(pnl_pct, 3)
                    exit_fields[f"win_{label}"] = pnl > 0
                else:
                    exit_fields[f"exit_date_{label}"] = ""
                    exit_fields[f"exit_price_{label}"] = 0.0
                    exit_fields[f"pnl_pct_{label}"] = 0.0
                    exit_fields[f"win_{label}"] = False

            # MFE/MAE on entry day (intraday)
            remaining = day_bars[i:]
            mfe_pct = 0.0; mae_pct = 0.0
            if remaining and entry_price > 0:
                if sig["bias"] == "bull":
                    mfe_abs = max(b["h"] for b in remaining) - entry_price
                    mae_abs = entry_price - min(b["l"] for b in remaining)
                else:
                    mfe_abs = entry_price - min(b["l"] for b in remaining)
                    mae_abs = max(b["h"] for b in remaining) - entry_price
                mfe_pct = (mfe_abs / entry_price) * 100
                mae_pct = (mae_abs / entry_price) * 100

            # Overlay: Potter Box at signal date (use daily bar index for day BEFORE)
            d_idx = daily_date_to_idx.get(trade_date, -1)
            pb_lookup_idx = max(0, d_idx - 1)   # previous day's state (to avoid lookahead)
            pb = _potter_state_at(daily, pb_lookup_idx, ticker, entry_price)
            fib = _fib_state_at(daily, pb_lookup_idx)
            sw = _swing_state_at(daily, pb_lookup_idx, swing_highs, swing_lows) if d_idx > 0 else {"above_pct": 999.0, "below_pct": 999.0}

            # Credit spread sim (only when in_box + have 5d exit)
            cs_short = 0.0
            cs_25_b = "n/a"; cs_25_w = False; cs_50_b = "n/a"; cs_50_w = False
            if pb["state"] == "in_box" and pb["floor"] > 0 and pb["roof"] > 0 and exit_fields["win_5d"] is not None:
                exit_p_5d = exit_fields["exit_price_5d"]
                if exit_p_5d > 0:
                    if sig["bias"] == "bull":
                        cs_short = pb["floor"]
                        cs_25_long = pb["floor"] - 2.50
                        cs_50_long = pb["floor"] - 5.00
                    else:
                        cs_short = pb["roof"]
                        cs_25_long = pb["roof"] + 2.50
                        cs_50_long = pb["roof"] + 5.00
                    cs_25_b, cs_25_w = _grade_credit(sig["bias"], cs_short, cs_25_long, exit_p_5d)
                    cs_50_b, cs_50_w = _grade_credit(sig["bias"], cs_short, cs_50_long, exit_p_5d)

            # Confluence bucket
            conf_bucket = _classify_confluence(sig["bias"], pb["state"])

            # Build Trade
            t = Trade(
                ticker=ticker, signal_date=trade_date, signal_time_ct=entry_time_ct,
                signal_ts=int(day_bars[i].get("ts", 0)), entry_price=entry_price,
                bias=sig["bias"], tier=sig["tier"], score=sig["score"],
                regime=regime, regime_valid=valid, regime_reason=str(reason)[:120],
                phase=sig["phase"], data_quality=sig["data_quality"],
                sb_ema=sig["sb"].get("ema", 0),
                sb_macd_hist=sig["sb"].get("macd_hist", 0),
                sb_macd_cross=sig["sb"].get("macd_cross", 0),
                sb_wt=sig["sb"].get("wt", 0),
                sb_vwap=sig["sb"].get("vwap", 0),
                sb_htf=sig["sb"].get("htf", 0),
                sb_volume=sig["sb"].get("volume", 0),
                sb_rsi=sig["sb"].get("rsi", 0),
                sb_flow=sig["sb"].get("flow", 0),
                ema_dist_pct=sig["ema_dist_pct"],
                macd_hist=sig["macd_hist"],
                macd_cross_bull=sig["macd_cross_bull"],
                macd_cross_bear=sig["macd_cross_bear"],
                wt1=sig["wt1"], wt2=sig["wt2"],
                rsi=sig["rsi"], volume_ratio=sig["volume_ratio"],
                adx=sig.get("adx", 0.0),
                htf_status=sig["htf_status"],
                htf_confirmed=sig["htf_confirmed"],
                htf_converging=sig["htf_converging"],
                daily_bull=bool(sig["daily_bull"]) if sig["daily_bull"] is not None else False,
                above_vwap=sig["above_vwap"],
                mfe_eod_pct=round(mfe_pct, 3), mae_eod_pct=round(mae_pct, 3),
                pb_state=pb["state"], pb_floor=pb["floor"], pb_roof=pb["roof"],
                pb_midpoint=pb["midpoint"], pb_range_pct=pb["range_pct"],
                pb_duration_bars=pb["duration_bars"],
                pb_max_touches=pb["max_touches"],
                pb_wave_label=pb["wave_label"],
                pb_break_confirmed=pb["break_confirmed"],
                cb_side=pb["cb_side"], cb_distance_pct=round(pb["cb_distance_pct"], 3),
                fib_nearest_level=fib["level"],
                fib_distance_pct=round(fib["distance_pct"], 2),
                fib_above_or_below=fib["above_below"],
                swing_dist_above_pct=round(sw["above_pct"], 2),
                swing_dist_below_pct=round(sw["below_pct"], 2),
                credit_short_strike=round(cs_short, 2),
                credit_25_bucket=cs_25_b, credit_25_win_5d=cs_25_w,
                credit_50_bucket=cs_50_b, credit_50_win_5d=cs_50_w,
                confluence_bucket=conf_bucket,
                **{k: exit_fields[k] for k in exit_fields},
            )
            trades.append(t)

    return trades


# ═══════════════════════════════════════════════════════════
# SUMMARIES (mirror v3_runner's output structure)
# ═══════════════════════════════════════════════════════════

def _wr_stats(subset: list, exit_label: str = "5d") -> tuple:
    """Return (n, wins, wr_pct, avg_pnl_pct). subset is list[Trade]."""
    if not subset:
        return (0, 0, 0.0, 0.0)
    wins = sum(1 for t in subset if getattr(t, f"win_{exit_label}"))
    avg = sum(getattr(t, f"pnl_pct_{exit_label}") for t in subset) / len(subset)
    return (len(subset), wins, 100 * wins / len(subset), avg)


def write_trades_csv(trades, path):
    if not trades:
        return
    fields = list(Trade.__dataclass_fields__.keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def write_summary_by_ticker(trades, path):
    g = defaultdict(list)
    for t in trades:
        g[(t.ticker, t.tier, t.bias)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "tier", "bias", "n_trades",
                    "wr_eod", "wr_1d", "wr_2d", "wr_3d", "wr_5d",
                    "avg_pnl_5d", "avg_mfe_eod", "avg_mae_eod"])
        for k, ts in sorted(g.items()):
            n = len(ts)
            if n == 0:
                continue
            amfe = sum(t.mfe_eod_pct for t in ts) / n
            amae = sum(t.mae_eod_pct for t in ts) / n
            row = [k[0], k[1], k[2], n]
            for lbl in ("eod", "1d", "2d", "3d", "5d"):
                _, _, wr, _ = _wr_stats(ts, lbl)
                row.append(f"{wr:.1f}")
            _, _, _, avg5 = _wr_stats(ts, "5d")
            row += [f"{avg5:+.3f}", f"{amfe:+.3f}", f"{amae:+.3f}"]
            w.writerow(row)


def write_summary_by_regime(trades, path):
    g = defaultdict(list)
    for t in trades:
        g[(t.regime, t.tier, t.bias)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["regime", "tier", "bias", "n_trades",
                    "wr_eod", "wr_1d", "wr_3d", "wr_5d", "avg_pnl_5d"])
        for k, ts in sorted(g.items()):
            n = len(ts)
            if n == 0:
                continue
            row = [k[0], k[1], k[2], n]
            for lbl in ("eod", "1d", "3d", "5d"):
                _, _, wr, _ = _wr_stats(ts, lbl)
                row.append(f"{wr:.1f}")
            _, _, _, avg5 = _wr_stats(ts, "5d")
            row.append(f"{avg5:+.3f}")
            w.writerow(row)


def write_summary_by_tier(trades, path):
    g = defaultdict(list)
    for t in trades:
        g[(t.tier, t.bias)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tier", "bias", "n_trades",
                    "wr_eod", "wr_1d", "wr_2d", "wr_3d", "wr_5d",
                    "avg_pnl_5d"])
        for k, ts in sorted(g.items()):
            n = len(ts)
            row = [k[0], k[1], n]
            for lbl in ("eod", "1d", "2d", "3d", "5d"):
                _, _, wr, _ = _wr_stats(ts, lbl)
                row.append(f"{wr:.1f}")
            _, _, _, avg5 = _wr_stats(ts, "5d")
            row.append(f"{avg5:+.3f}")
            w.writerow(row)


def write_summary_by_htf(trades, path):
    g = defaultdict(list)
    for t in trades:
        g[(t.htf_status, t.regime, t.bias)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["htf_status", "regime", "bias", "n_trades",
                    "wr_1d", "wr_3d", "wr_5d", "avg_pnl_5d"])
        for k, ts in sorted(g.items()):
            n = len(ts)
            if n == 0:
                continue
            row = [k[0], k[1], k[2], n]
            for lbl in ("1d", "3d", "5d"):
                _, _, wr, _ = _wr_stats(ts, lbl)
                row.append(f"{wr:.1f}")
            _, _, _, avg5 = _wr_stats(ts, "5d")
            row.append(f"{avg5:+.3f}")
            w.writerow(row)


def write_summary_by_confluence(trades, path):
    """Single most important summary — overlay WR vs baseline."""
    # Baseline per (tier, bias) = overall WR at 5d
    baselines = {}
    for tier in ("1", "2"):
        for bias in ("bull", "bear"):
            s = [t for t in trades if t.tier == tier and t.bias == bias]
            _, _, wr, _ = _wr_stats(s, "5d")
            baselines[(tier, bias)] = wr

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dimension", "value", "tier", "bias", "n_trades",
                    "wr_1d", "wr_3d", "wr_5d", "vs_baseline_5d"])

        # Potter Box state
        for tier in ("1", "2"):
            for bias in ("bull", "bear"):
                g = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.bias == bias:
                        g[t.pb_state].append(t)
                for state in ("above_roof", "below_floor", "in_box", "no_box", "post_box"):
                    ts = g.get(state, [])
                    if not ts:
                        continue
                    _, _, wr5, _ = _wr_stats(ts, "5d")
                    _, _, wr3, _ = _wr_stats(ts, "3d")
                    _, _, wr1, _ = _wr_stats(ts, "1d")
                    dlt = wr5 - baselines[(tier, bias)]
                    w.writerow(["potter_box", state, tier, bias, len(ts),
                                f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}", f"{dlt:+.1f}"])

        # CB side (in_box only)
        for tier in ("1", "2"):
            for bias in ("bull", "bear"):
                g = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.bias == bias and t.pb_state == "in_box":
                        g[t.cb_side].append(t)
                for side in ("above_cb", "below_cb", "at_cb"):
                    ts = g.get(side, [])
                    if not ts:
                        continue
                    _, _, wr5, _ = _wr_stats(ts, "5d")
                    _, _, wr3, _ = _wr_stats(ts, "3d")
                    _, _, wr1, _ = _wr_stats(ts, "1d")
                    dlt = wr5 - baselines[(tier, bias)]
                    w.writerow(["cb_side_in_box", side, tier, bias, len(ts),
                                f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}", f"{dlt:+.1f}"])

        # Wave label (in_box only)
        for tier in ("1", "2"):
            for bias in ("bull", "bear"):
                g = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.bias == bias and t.pb_state == "in_box":
                        g[t.pb_wave_label].append(t)
                for wl in ("established", "weakening", "breakout_probable", "breakout_imminent"):
                    ts = g.get(wl, [])
                    if not ts:
                        continue
                    _, _, wr5, _ = _wr_stats(ts, "5d")
                    _, _, wr3, _ = _wr_stats(ts, "3d")
                    _, _, wr1, _ = _wr_stats(ts, "1d")
                    dlt = wr5 - baselines[(tier, bias)]
                    w.writerow(["wave_label_in_box", wl, tier, bias, len(ts),
                                f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}", f"{dlt:+.1f}"])

        # HTF status
        for tier in ("1", "2"):
            for bias in ("bull", "bear"):
                g = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.bias == bias:
                        g[t.htf_status].append(t)
                for status in ("CONFIRMED", "CONVERGING", "OPPOSING", "UNKNOWN"):
                    ts = g.get(status, [])
                    if not ts:
                        continue
                    _, _, wr5, _ = _wr_stats(ts, "5d")
                    _, _, wr3, _ = _wr_stats(ts, "3d")
                    _, _, wr1, _ = _wr_stats(ts, "1d")
                    dlt = wr5 - baselines[(tier, bias)]
                    w.writerow(["htf_status", status, tier, bias, len(ts),
                                f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}", f"{dlt:+.1f}"])

        # Regime validity (ticker rules)
        for tier in ("1", "2"):
            for bias in ("bull", "bear"):
                for valid in (True, False):
                    ts = [t for t in trades if t.tier == tier and t.bias == bias and t.regime_valid == valid]
                    if not ts:
                        continue
                    _, _, wr5, _ = _wr_stats(ts, "5d")
                    _, _, wr3, _ = _wr_stats(ts, "3d")
                    _, _, wr1, _ = _wr_stats(ts, "1d")
                    dlt = wr5 - baselines[(tier, bias)]
                    w.writerow(["regime_valid", str(valid), tier, bias, len(ts),
                                f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}", f"{dlt:+.1f}"])

        # Confluence bucket
        for tier in ("1", "2"):
            for bias in ("bull", "bear"):
                g = defaultdict(list)
                for t in trades:
                    if t.tier == tier and t.bias == bias:
                        g[t.confluence_bucket].append(t)
                for c in sorted(g.keys()):
                    ts = g[c]
                    _, _, wr5, _ = _wr_stats(ts, "5d")
                    _, _, wr3, _ = _wr_stats(ts, "3d")
                    _, _, wr1, _ = _wr_stats(ts, "1d")
                    dlt = wr5 - baselines[(tier, bias)]
                    w.writerow(["confluence_bucket", c, tier, bias, len(ts),
                                f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}", f"{dlt:+.1f}"])


def _quintile_edges(values):
    if not values:
        return []
    s = sorted(values)
    n = len(s)
    edges = [s[0]]
    for q in (0.2, 0.4, 0.6, 0.8):
        edges.append(s[min(int(n * q), n - 1)])
    edges.append(s[-1])
    return edges


def _which_quintile(value, edges):
    if not edges or len(edges) < 6:
        return 0
    for i in range(5):
        if value <= edges[i + 1]:
            return i
    return 4


def write_summary_by_indicator(trades, path):
    """Quintile WR per indicator, per (tier, bias)."""
    indicators = [
        ("score",         lambda t: t.score),
        ("ema_dist_pct",  lambda t: t.ema_dist_pct),
        ("macd_hist",     lambda t: t.macd_hist),
        ("rsi",           lambda t: t.rsi),
        ("adx",           lambda t: t.adx),
        ("wt2",           lambda t: t.wt2),
        ("volume_ratio",  lambda t: t.volume_ratio),
    ]
    baselines = {}
    for tier in ("1", "2"):
        for bias in ("bull", "bear"):
            s = [t for t in trades if t.tier == tier and t.bias == bias]
            _, _, wr, _ = _wr_stats(s, "5d")
            baselines[(tier, bias)] = wr

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["indicator", "quintile", "tier", "bias",
                    "range_low", "range_high", "n_trades",
                    "wr_1d", "wr_3d", "wr_5d", "vs_baseline_5d"])
        for ind_name, acc in indicators:
            for tier in ("1", "2"):
                for bias in ("bull", "bear"):
                    subset = [t for t in trades if t.tier == tier and t.bias == bias]
                    if len(subset) < 50:
                        continue
                    vals = [acc(t) for t in subset]
                    edges = _quintile_edges(vals)
                    if not edges:
                        continue
                    bins = defaultdict(list)
                    for t in subset:
                        q = _which_quintile(acc(t), edges)
                        bins[q].append(t)
                    base = baselines[(tier, bias)]
                    for q_idx in range(5):
                        ts = bins.get(q_idx, [])
                        if not ts:
                            continue
                        _, _, wr5, _ = _wr_stats(ts, "5d")
                        _, _, wr3, _ = _wr_stats(ts, "3d")
                        _, _, wr1, _ = _wr_stats(ts, "1d")
                        dlt = wr5 - base
                        w.writerow([ind_name, f"Q{q_idx+1}", tier, bias,
                                    f"{edges[q_idx]:.3f}", f"{edges[q_idx+1]:.3f}",
                                    len(ts),
                                    f"{wr1:.1f}", f"{wr3:.1f}", f"{wr5:.1f}",
                                    f"{dlt:+.1f}"])


def write_summary_by_credit(trades, path):
    """Credit spread WR at Potter Box boundaries (in_box trades only)."""
    g = defaultdict(list)
    for t in trades:
        if t.pb_state == "in_box" and t.credit_short_strike > 0:
            g[(t.ticker, t.tier, t.bias)].append(t)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "tier", "bias", "n_in_box",
                    "debit_wr_5d", "credit_25_wr_5d", "credit_50_wr_5d",
                    "better_strategy"])
        # Index debit WR from ALL trades (not just in_box)
        debit_wr_lookup = {}
        debit_g = defaultdict(list)
        for t in trades:
            debit_g[(t.ticker, t.tier, t.bias)].append(t)
        for k, ts in debit_g.items():
            _, _, wr, _ = _wr_stats(ts, "5d")
            debit_wr_lookup[k] = wr

        for k, ts in sorted(g.items()):
            if len(ts) < 10:
                continue
            c25_wins = sum(1 for t in ts if t.credit_25_win_5d)
            c50_wins = sum(1 for t in ts if t.credit_50_win_5d)
            wr_c25 = 100 * c25_wins / len(ts)
            wr_c50 = 100 * c50_wins / len(ts)
            wr_debit = debit_wr_lookup.get(k, 0.0)
            best_credit = max(wr_c25, wr_c50)
            if wr_debit >= best_credit + 3:
                best = "debit"
            elif best_credit >= wr_debit + 3:
                best = f"credit_{'25' if wr_c25 >= wr_c50 else '50'}"
            else:
                best = "tie"
            w.writerow([k[0], k[1], k[2], len(ts),
                        f"{wr_debit:.1f}", f"{wr_c25:.1f}", f"{wr_c50:.1f}", best])


def write_report(trades, start, end, path):
    n = len(trades)
    if n == 0:
        Path(path).write_text("# bt_active_v8 report\n\nNo trades generated.\n")
        return

    def stat(subset, lbl="5d"):
        if not subset:
            return (0, 0.0, 0.0)
        wins = sum(1 for t in subset if getattr(t, f"win_{lbl}"))
        avg = sum(getattr(t, f"pnl_pct_{lbl}") for t in subset) / len(subset)
        return (len(subset), 100 * wins / len(subset), avg)

    a_1d  = stat(trades, "1d")
    a_3d  = stat(trades, "3d")
    a_5d  = stat(trades, "5d")
    t1b = stat([t for t in trades if t.tier == "1" and t.bias == "bull"], "5d")
    t2b = stat([t for t in trades if t.tier == "2" and t.bias == "bull"], "5d")
    t1s = stat([t for t in trades if t.tier == "1" and t.bias == "bear"], "5d")
    t2s = stat([t for t in trades if t.tier == "2" and t.bias == "bear"], "5d")
    bull_r = stat([t for t in trades if t.regime == "BULL"], "5d")
    trans_r = stat([t for t in trades if t.regime == "TRANSITION"], "5d")
    bear_r = stat([t for t in trades if t.regime == "BEAR"], "5d")
    confirmed = stat([t for t in trades if t.htf_status == "CONFIRMED"], "5d")
    converging = stat([t for t in trades if t.htf_status == "CONVERGING"], "5d")

    md = f"""# Backtest Report — Active Scanner v8 (UNIFIED)
**Date range:** {start} → {end}
**Total signals fired:** {n}

Faithful port of `active_scanner._analyze_ticker()` v6.1 production logic.
Overlay columns added on every trade (Potter Box, CB side, Fib, credit spreads).

## Headline numbers (5-day exit)

| Subset | N | WR | Avg PnL% |
|---|---|---|---|
| **ALL signals** | {a_5d[0]} | **{a_5d[1]:.1f}%** | {a_5d[2]:+.3f}% |
| T1 Bull | {t1b[0]} | **{t1b[1]:.1f}%** | {t1b[2]:+.3f}% |
| T2 Bull | {t2b[0]} | **{t2b[1]:.1f}%** | {t2b[2]:+.3f}% |
| T1 Bear | {t1s[0]} | **{t1s[1]:.1f}%** | {t1s[2]:+.3f}% |
| T2 Bear | {t2s[0]} | **{t2s[1]:.1f}%** | {t2s[2]:+.3f}% |

## By exit horizon (ALL signals)

| Horizon | N | WR | Avg PnL% |
|---|---|---|---|
| 1d | {a_1d[0]} | {a_1d[1]:.1f}% | {a_1d[2]:+.3f}% |
| 3d | {a_3d[0]} | {a_3d[1]:.1f}% | {a_3d[2]:+.3f}% |
| 5d | {a_5d[0]} | {a_5d[1]:.1f}% | {a_5d[2]:+.3f}% |

## By regime

| Regime | N | 5d WR | Avg PnL% |
|---|---|---|---|
| BULL | {bull_r[0]} | {bull_r[1]:.1f}% | {bull_r[2]:+.3f}% |
| TRANSITION | {trans_r[0]} | {trans_r[1]:.1f}% | {trans_r[2]:+.3f}% |
| BEAR | {bear_r[0]} | {bear_r[1]:.1f}% | {bear_r[2]:+.3f}% |

## By HTF status

| HTF status | N | 5d WR | Avg PnL% |
|---|---|---|---|
| CONFIRMED | {confirmed[0]} | {confirmed[1]:.1f}% | {confirmed[2]:+.3f}% |
| CONVERGING | {converging[0]} | {converging[1]:.1f}% | {converging[2]:+.3f}% |

## Drill-downs

- **`trades.csv`** — one row per signal, all columns
- **`summary_by_ticker.csv`** — which tickers carry the edge
- **`summary_by_regime.csv`** — does edge only exist in specific regimes
- **`summary_by_htf_status.csv`** — P2 TRANSITION CONVERGING fix validation
- **`summary_by_confluence.csv`** — ← **the money table**. Overlay WR vs baseline.
  `vs_baseline_5d` > +5 means this overlay adds edge. < -5 means it destroys it.
- **`summary_by_indicator.csv`** — quintile WR per indicator (is RSI a filter?)
- **`summary_by_credit.csv`** — credit spread WR at Potter Box boundaries

## Compared to v3_runner (pinescript) results

Run both and compare. If v3_runner's pinescript T1/T2 produced ~65% WR with
CB side filter giving 73%+ on T2 Bull below_cb, this file should be showing
similar or better numbers at its own thresholds.

If active scanner's WR is materially lower than pinescript's, the scanner
tuning is suspect — and the v8.2 handoff suggestion to port pinescript into
the bot starts to make sense.

If they're similar, both are hitting the same underlying edge through
different lenses and the choice is operational (which is easier to run).

— Not financial advice —
"""
    Path(path).write_text(md)


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Active Scanner Backtest v8 (UNIFIED)")
    ap.add_argument("--ticker", default=None, help="Single ticker")
    ap.add_argument("--all", action="store_true", help="Run full ALL_TICKERS list")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=270, help="Lookback days if --from not set")
    args = ap.parse_args()

    # Determine tickers
    override = os.environ.get("BACKTEST_TICKERS", "").strip()
    if override:
        tickers = [t.strip().upper() for t in override.split(",") if t.strip()]
    elif args.all:
        tickers = ALL_TICKERS
    elif args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = ALL_TICKERS   # default — all, same as v3_runner default

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Output dir: {OUT_DIR}")

    to_date = args.to_date or date.today().isoformat()
    from_date = args.from_date or (date.today() - timedelta(days=args.days)).isoformat()
    daily_from = (datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=100)).strftime("%Y-%m-%d")
    log.info(f"Range: {from_date} → {to_date}  ({len(tickers)} tickers)")

    # ── Download regime reference data (SPY/QQQ/IWM/VIX daily) ──
    log.info("Downloading regime reference data (SPY/QQQ/IWM/VIX daily)...")
    regime_bars = {}
    for t in ("SPY", "QQQ", "IWM"):
        regime_bars[t] = download_daily(t, daily_from, to_date)
    regime_bars["VIX"] = download_vix(daily_from, to_date)

    # ── Resume-from-checkpoint ──
    progress_path = OUT_DIR / ".progress.json"
    trades_path = OUT_DIR / "trades.csv"
    all_trades: list[Trade] = []
    done: set = set()

    if progress_path.exists():
        try:
            with open(progress_path) as f:
                done = set(json.load(f).get("done", []))
            if trades_path.exists() and done:
                type_map = {}
                for field in _dc_fields(Trade):
                    t_ = field.type
                    if t_ is int or t_ == "int":
                        type_map[field.name] = "int"
                    elif t_ is float or t_ == "float":
                        type_map[field.name] = "float"
                    elif t_ is bool or t_ == "bool":
                        type_map[field.name] = "bool"
                    else:
                        type_map[field.name] = "str"
                with open(trades_path) as f:
                    rdr = csv.DictReader(f)
                    for row in rdr:
                        for name, kind in type_map.items():
                            if name not in row or row[name] is None or row[name] == "":
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
                            # Schema drift — skip this row
                            pass
                log.info(f"Resumed {len(all_trades)} trades from {len(done)} tickers")
        except Exception as e:
            log.warning(f"Resume failed: {e}; starting fresh")
            all_trades = []
            done = set()

    # ── Per-ticker loop ──
    for idx, ticker in enumerate(tickers, 1):
        if ticker in done:
            log.info(f"[{idx}/{len(tickers)}] {ticker}: done, skipping")
            continue

        log.info(f"[{idx}/{len(tickers)}] {ticker}: downloading bars...")
        try:
            intraday = download_5min(ticker, from_date, to_date)
        except Exception as e:
            log.error(f"{ticker}: 5-min download failed: {e}")
            done.add(ticker)
            continue
        if not intraday:
            log.warning(f"{ticker}: no 5-min bars; skipping")
            done.add(ticker)
            continue

        intraday = [b for b in intraday if is_market_bar(b["time_ct"])]
        if not intraday:
            log.warning(f"{ticker}: no market-hours bars; skipping")
            done.add(ticker)
            continue

        if ticker in ("SPY", "QQQ", "IWM"):
            daily = regime_bars.get(ticker, [])
        else:
            try:
                daily = download_daily(ticker, daily_from, to_date)
            except Exception as e:
                log.error(f"{ticker}: daily download failed: {e}")
                daily = []
        if not daily or len(daily) < 30:
            log.warning(f"{ticker}: insufficient daily bars ({len(daily)}); skipping")
            done.add(ticker)
            continue

        # ── Regime map per day ──
        log.info(f"{ticker}: computing daily regimes...")
        daily_close_by_date = {b["date"]: b["c"] for b in daily}
        sorted_dates = sorted(daily_close_by_date.keys())
        # Include intraday dates for exit lookup even if daily is missing
        for b in intraday:
            d = b["date"]
            if d not in daily_close_by_date and d > sorted_dates[0]:
                pass
        sorted_dates = sorted(set(daily_close_by_date.keys()))

        regime_cache = {}
        for b in intraday:
            d = b["date"]
            if d in regime_cache:
                continue
            try:
                _, _, v1_regime, _ = compute_regime_for_date(
                    regime_bars["SPY"], regime_bars["QQQ"],
                    regime_bars["IWM"], regime_bars["VIX"], d
                )
                regime_cache[d] = v1_regime
            except Exception:
                regime_cache[d] = "BEAR"

        log.info(f"{ticker}: {len(intraday)} 5-min bars, {len(daily)} daily bars, signals...")
        trades = run_ticker(ticker, intraday, daily, regime_cache,
                            daily_close_by_date, sorted_dates)
        n_t1 = sum(1 for t in trades if t.tier == "1")
        n_t2 = sum(1 for t in trades if t.tier == "2")
        log.info(f"{ticker}: {len(trades)} trades (T1={n_t1}, T2={n_t2})")
        all_trades.extend(trades)

        # Checkpoint after each ticker
        done.add(ticker)
        write_trades_csv(all_trades, trades_path)
        with open(progress_path, "w") as f:
            json.dump({"done": sorted(done)}, f)

    # ── Final writes ──
    log.info(f"Writing summaries (total: {len(all_trades)} trades)")
    write_trades_csv(all_trades, trades_path)
    write_summary_by_ticker(all_trades, OUT_DIR / "summary_by_ticker.csv")
    write_summary_by_regime(all_trades, OUT_DIR / "summary_by_regime.csv")
    write_summary_by_tier(all_trades, OUT_DIR / "summary_by_tier.csv")
    write_summary_by_htf(all_trades, OUT_DIR / "summary_by_htf_status.csv")
    write_summary_by_confluence(all_trades, OUT_DIR / "summary_by_confluence.csv")
    write_summary_by_indicator(all_trades, OUT_DIR / "summary_by_indicator.csv")
    write_summary_by_credit(all_trades, OUT_DIR / "summary_by_credit.csv")
    write_report(all_trades, from_date, to_date, OUT_DIR / "report.md")

    log.info(f"DONE. Outputs in {OUT_DIR}:")
    for fn in sorted(os.listdir(OUT_DIR)):
        if not fn.startswith("."):
            fp = OUT_DIR / fn
            log.info(f"  {fn}  ({fp.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
