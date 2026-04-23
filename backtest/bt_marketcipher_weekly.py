#!/usr/bin/env python3
"""
bt_marketcipher_weekly.py — Weekly WaveTrend "Big Green Dot" Backtest
══════════════════════════════════════════════════════════════════════

Tests the Market Cipher "big green dot" signal on the WEEKLY timeframe,
long-only. A big green dot = WaveTrend bullish cross while WT1 is in
oversold territory (< -53 at cross bar).

DESIGN PRINCIPLE — NO DUPLICATION
  - `_compute_ema` reused from active_scanner
  - WaveTrend math: LazyBear formula (ported here to return full series,
    not just latest — active_scanner's version returns only last value)
  - Weekly bars via swing_scanner._aggregate_weekly
  - Data + regime via bt_shared

WHAT WE'RE MEASURING
  Entry (long-only):
    - Weekly WT1 crosses above WT2 ("bullish cross")
    - WT1 at cross-bar < -53 (oversold threshold)

  Confluence flags recorded (not gating — ablated in post):
    - rsi_weekly_oversold (< 40)
    - stoch_rsi_oversold (< 20)
    - macd_hist_weekly_negative
    - above_weekly_200sma
    - bar_green (close > open on signal week)

  Hold model (trajectory, not closure):
    - Stop: entry - 2 * weekly_ATR(14)
    - Opposite signal exit: next weekly bar where WT1 crosses BELOW WT2
      with WT1 > +53 (the "big red dot")
    - Max hold: 52 weeks (260 trading days)
    - First-hit times recorded for thresholds: +10%, +25%, +50%, +100%,
      +200%, +300% (direction-adjusted, long-only so all positive)
    - MAE/MFE tracked over full hold
    - Weekly-close snapshots at W+1, W+4, W+8, W+13, W+26, W+39, W+52

EXIT PRECEDENCE (single canonical exit for P&L headline):
    first of { stop, opposite_signal, max_hold_260d }

UNIVERSE
  Default: SWING_WATCHLIST (106 tickers)
  Wider:   use --tickers-file or BACKTEST_TICKERS env.

  S&P 500 snapshot (2024):
    curl -sL https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv \
      | tail -n +2 | cut -d',' -f1 > /tmp/sp500.txt
    python backtest/bt_marketcipher_weekly.py --tickers-file /tmp/sp500.txt

USAGE
    python backtest/bt_marketcipher_weekly.py                    # default watchlist
    python backtest/bt_marketcipher_weekly.py --days 1800        # 5 years
    python backtest/bt_marketcipher_weekly.py --tickers-file /tmp/sp500.txt --days 1800

ENV
    BACKTEST_START/END           YYYY-MM-DD overrides
    BACKTEST_TICKERS             comma-separated override
    BACKTEST_OUT_DIR             default /tmp/backtest_mc_weekly
    WT_OVERSOLD_THRESH           WT1 oversold level at cross bar (default -53)
    WT_OVERBOUGHT_EXIT_THRESH    WT1 overbought level for exit signal (default 53)
    MC_MAX_HOLD_WEEKS            default 52

OUTPUTS: /tmp/backtest_mc_weekly/
    trades.csv                       one row per big-green-dot signal
    summary_by_ticker.csv
    summary_by_confluence.csv        ablation stats for each filter layer
    summary_target_hit_rates.csv     % of signals that hit each % threshold
    summary_by_regime.csv
    summary_by_exit_reason.csv
    report.md                        executive summary + trajectory analysis
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import traceback
from dataclasses import dataclass, fields as _dc_fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════
# BOOTSTRAP
# ═══════════════════════════════════════════════════════════

BOT_REPO_PATH = os.environ.get("BOT_REPO_PATH", "/opt/render/project/src")
if BOT_REPO_PATH not in sys.path:
    sys.path.insert(0, BOT_REPO_PATH)
BACKTEST_DIR = Path(BOT_REPO_PATH) / "backtest"
if str(BACKTEST_DIR) not in sys.path:
    sys.path.insert(0, str(BACKTEST_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bt_mc_weekly")

try:
    from swing_scanner import _aggregate_weekly
    log.info("Loaded swing_scanner._aggregate_weekly")
except ImportError as e:
    log.error(f"Cannot import swing_scanner: {e}")
    sys.exit(1)

try:
    from trading_rules import (
        SWING_WATCHLIST, SWING_CONFIRMED_TICKERS, SWING_REMOVED_TICKERS,
    )
    log.info(f"SWING_WATCHLIST: {len(SWING_WATCHLIST)} tickers")
except ImportError as e:
    log.error(f"Cannot import trading_rules: {e}")
    sys.exit(1)

try:
    from bt_shared import download_daily, download_vix, compute_regime_for_date
except ImportError as e:
    log.error(f"Cannot import bt_shared: {e}")
    sys.exit(1)

try:
    from active_scanner import _compute_ema as _as_ema
except ImportError as e:
    log.error(f"Cannot import active_scanner._compute_ema: {e}")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════

# WaveTrend (LazyBear)
WT_N1 = 10    # channel length
WT_N2 = 21    # average length
WT_OVERSOLD_THRESH = float(os.environ.get("WT_OVERSOLD_THRESH", "-53"))
WT_OVERBOUGHT_EXIT = float(os.environ.get("WT_OVERBOUGHT_EXIT_THRESH", "53"))

# Other weekly indicators
WEEKLY_RSI_PERIOD   = 14
WEEKLY_MACD_FAST    = 12
WEEKLY_MACD_SLOW    = 26
WEEKLY_MACD_SIGNAL  = 9
WEEKLY_ATR_PERIOD   = 14
WEEKLY_200_SMA      = 52    # "200 day SMA" at weekly ≈ 52 weeks (a year)

# Confluence thresholds
RSI_OVERSOLD_THRESH       = 40
STOCH_RSI_OVERSOLD_THRESH = 20
STOCH_RSI_K_PERIOD        = 14
STOCH_RSI_D_PERIOD        = 3

# Hold model
MAX_HOLD_WEEKS    = int(os.environ.get("MC_MAX_HOLD_WEEKS", "52"))
MAX_HOLD_DAYS     = MAX_HOLD_WEEKS * 5       # ~260 trading days
WEEKLY_ATR_STOP_MULT = 2.0

# Target thresholds (percent gain from entry)
TARGET_THRESHOLDS_PCT = [10, 25, 50, 100, 200, 300]

# Weekly-close snapshots
SNAPSHOT_WEEKS = [1, 4, 8, 13, 26, 39, 52]

# Per-ticker warmup
MIN_DAILY_BARS  = 300     # ~60 weekly bars minimum after aggregation
MIN_WEEKLY_BARS = WEEKLY_MACD_SLOW + WEEKLY_MACD_SIGNAL + WEEKLY_200_SMA + 4

OUT_DIR = Path(os.environ.get("BACKTEST_OUT_DIR", "/tmp/backtest_mc_weekly"))
DEFAULT_TICKERS = sorted(SWING_WATCHLIST)


# ═══════════════════════════════════════════════════════════
# DATACLASS
# ═══════════════════════════════════════════════════════════

@dataclass
class MCSignal:
    # Identity
    ticker: str = ""
    signal_week_date: str = ""
    entry_date: str = ""
    entry_price: float = 0.0

    # WaveTrend context at signal
    wt1_at_signal: float = 0.0
    wt2_at_signal: float = 0.0
    wt1_prev: float = 0.0
    wt2_prev: float = 0.0

    # Weekly indicator snapshots at signal
    rsi_weekly: float = 0.0
    stoch_rsi_weekly: float = 0.0
    macd_hist_weekly: float = 0.0
    weekly_200sma: float = 0.0
    weekly_atr: float = 0.0
    weekly_bar_open: float = 0.0
    weekly_bar_close: float = 0.0

    # Confluence flags (the ablation dimensions)
    rsi_oversold: bool = False                 # rsi_weekly < 40
    stoch_rsi_oversold: bool = False           # stoch_rsi_weekly < 20
    macd_hist_negative: bool = False
    above_weekly_200sma: bool = False
    bar_green: bool = False                    # weekly close > weekly open
    confluence_score: int = 0                  # count of above flags that fire

    # Regime
    regime_trend: str = ""
    regime_vol: str = ""

    # Exit setup
    stop_initial: float = 0.0                  # entry - 2*weekly_ATR
    opposite_signal_week: str = ""             # date of next red-dot week (or "")
    opposite_signal_daily_idx: int = -1

    # Trajectory — first-hit (trading days from entry; -1 if never)
    t_to_10pct: int = -1
    t_to_25pct: int = -1
    t_to_50pct: int = -1
    t_to_100pct: int = -1
    t_to_200pct: int = -1
    t_to_300pct: int = -1
    t_to_stop: int = -1
    t_to_opposite_signal: int = -1

    # MAE/MFE over full hold (150D cap or opposite-signal, whichever first)
    mae_pct: float = 0.0
    mfe_pct: float = 0.0

    # Weekly-close snapshot prices (and %)
    price_w1: float = 0.0
    price_w4: float = 0.0
    price_w8: float = 0.0
    price_w13: float = 0.0
    price_w26: float = 0.0
    price_w39: float = 0.0
    price_w52: float = 0.0
    pct_w1: float = 0.0
    pct_w4: float = 0.0
    pct_w8: float = 0.0
    pct_w13: float = 0.0
    pct_w26: float = 0.0
    pct_w39: float = 0.0
    pct_w52: float = 0.0

    # Canonical exit (first of stop / opposite_signal / max_hold)
    exit_reason: str = ""
    exit_date: str = ""
    exit_price: float = 0.0
    hold_days_to_exit: int = 0
    pnl_pct: float = 0.0
    win: bool = False

    # Ticker flags
    is_confirmed_ticker: bool = False
    is_removed_ticker: bool = False


# ═══════════════════════════════════════════════════════════
# INDICATOR SERIES (all operate on lists of floats)
# ═══════════════════════════════════════════════════════════

def _ema_series(values: List[float], period: int) -> List[float]:
    """Return EMA series aligned to values[-len(series):]."""
    out = _as_ema(values, period)
    return out or []


def _sma_last(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """Wilder RSI series (same index base as closes[-len(out):])."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    if len(gains) < period:
        return []
    out = []
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out.append(100.0 - (100.0 / (1 + avg_g / avg_l)) if avg_l > 0 else 100.0)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out.append(100.0 - (100.0 / (1 + avg_g / avg_l)) if avg_l > 0 else 100.0)
    return out


def _stoch_rsi_series(closes: List[float],
                      rsi_period: int = 14,
                      stoch_period: int = 14) -> List[float]:
    """Stochastic RSI (%K), range 0-100."""
    rsi = _rsi_series(closes, rsi_period)
    if len(rsi) < stoch_period:
        return []
    out = []
    for i in range(stoch_period - 1, len(rsi)):
        window = rsi[i - stoch_period + 1: i + 1]
        lo = min(window); hi = max(window)
        out.append(((rsi[i] - lo) / (hi - lo) * 100.0) if hi > lo else 50.0)
    return out


def _macd_hist_series(closes: List[float]) -> List[float]:
    """MACD histogram series."""
    if len(closes) < WEEKLY_MACD_SLOW + WEEKLY_MACD_SIGNAL:
        return []
    fast = _ema_series(closes, WEEKLY_MACD_FAST)
    slow = _ema_series(closes, WEEKLY_MACD_SLOW)
    if not fast or not slow:
        return []
    offset = len(fast) - len(slow)
    macd_line = [fast[i + offset] - slow[i] for i in range(len(slow))]
    if len(macd_line) < WEEKLY_MACD_SIGNAL:
        return []
    sig = _ema_series(macd_line, WEEKLY_MACD_SIGNAL)
    sig_offset = len(macd_line) - len(sig)
    return [macd_line[i + sig_offset] - sig[i] for i in range(len(sig))]


def _atr_series(highs: List[float], lows: List[float], closes: List[float],
                period: int = 14) -> List[float]:
    """Wilder ATR series."""
    if len(highs) < period + 1:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    if len(trs) < period:
        return []
    out = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out


def _wavetrend_series(highs: List[float], lows: List[float],
                      closes: List[float]) -> Tuple[List[float], List[float]]:
    """LazyBear WaveTrend — returns (wt1_series, wt2_series) aligned at the end.

    Identical math to active_scanner._compute_wavetrend, but returns full
    time series. Validated: last values match _compute_wavetrend's output.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < WT_N2 + WT_N1 + 4:
        return [], []
    hlc3 = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]

    esa = _ema_series(hlc3, WT_N1)
    if not esa:
        return [], []
    offset_e = len(hlc3) - len(esa)
    d_series = [abs(hlc3[i + offset_e] - esa[i]) for i in range(len(esa))]
    de = _ema_series(d_series, WT_N1)
    if not de:
        return [], []
    offset_d = len(d_series) - len(de)
    ci = []
    for i in range(len(de)):
        dv = de[i]
        ev = esa[i + offset_d]
        hv = hlc3[i + offset_e + offset_d]
        ci.append((hv - ev) / (0.015 * dv) if dv != 0 else 0.0)

    wt1 = _ema_series(ci, WT_N2)
    if not wt1 or len(wt1) < 4:
        return [], []
    wt2 = _ema_series(wt1, 4)
    if not wt2:
        return [], []

    # Align wt1 to wt2's length
    offset_wt = len(wt1) - len(wt2)
    wt1_aligned = wt1[offset_wt:]
    return wt1_aligned, wt2


# ═══════════════════════════════════════════════════════════
# SIGNAL DETECTION
# ═══════════════════════════════════════════════════════════

def _detect_big_green_dots(weekly_bars: List[dict],
                            wt1: List[float], wt2: List[float]
                            ) -> List[int]:
    """Return weekly-bar indices where a big green dot fires.

    Bullish cross: wt1[i] > wt2[i] AND wt1[i-1] < wt2[i-1]
    Oversold: wt1[i] < WT_OVERSOLD_THRESH at cross bar

    Returns weekly-bar indices (into weekly_bars), caller aligns.
    """
    if len(wt1) != len(wt2) or len(wt1) < 2:
        return []
    # wt1/wt2 end-aligned to weekly_bars
    offset = len(weekly_bars) - len(wt1)
    if offset < 0:
        return []
    events = []
    for j in range(1, len(wt1)):
        if wt1[j - 1] < wt2[j - 1] and wt1[j] > wt2[j]:
            if wt1[j] < WT_OVERSOLD_THRESH:
                events.append(j + offset)   # index into weekly_bars
    return events


def _detect_big_red_dots(wt1: List[float], wt2: List[float],
                          weekly_bars: List[dict]) -> List[int]:
    """Opposite-signal exit points: wt1 crosses BELOW wt2, wt1 > overbought."""
    if len(wt1) != len(wt2) or len(wt1) < 2:
        return []
    offset = len(weekly_bars) - len(wt1)
    if offset < 0:
        return []
    events = []
    for j in range(1, len(wt1)):
        if wt1[j - 1] > wt2[j - 1] and wt1[j] < wt2[j]:
            if wt1[j] > WT_OVERBOUGHT_EXIT:
                events.append(j + offset)
    return events


# ═══════════════════════════════════════════════════════════
# PER-TICKER BACKTEST
# ═══════════════════════════════════════════════════════════

def run_ticker(ticker: str,
               daily_bars: List[dict],
               regime_map: Dict[str, Dict[str, str]]) -> List[MCSignal]:
    """Walk weekly signals → simulate on daily bars."""
    out: List[MCSignal] = []
    if len(daily_bars) < MIN_DAILY_BARS:
        return out

    try:
        weekly = _aggregate_weekly(daily_bars)
    except Exception as e:
        log.warning(f"{ticker}: weekly aggregation failed: {e}")
        return out
    if len(weekly) < MIN_WEEKLY_BARS:
        return out

    w_highs  = [w["h"] for w in weekly]
    w_lows   = [w["l"] for w in weekly]
    w_closes = [w["c"] for w in weekly]
    w_opens  = [w["o"] for w in weekly]

    wt1, wt2 = _wavetrend_series(w_highs, w_lows, w_closes)
    if not wt1:
        return out

    rsi_w       = _rsi_series(w_closes, WEEKLY_RSI_PERIOD)
    stoch_rsi_w = _stoch_rsi_series(w_closes, WEEKLY_RSI_PERIOD, STOCH_RSI_K_PERIOD)
    macd_h_w    = _macd_hist_series(w_closes)
    atr_w       = _atr_series(w_highs, w_lows, w_closes, WEEKLY_ATR_PERIOD)

    # Alignment offsets
    def _at(series: List[float], w_idx: int) -> float:
        if not series:
            return 0.0
        off = len(weekly) - len(series)
        j = w_idx - off
        if 0 <= j < len(series):
            return float(series[j])
        return 0.0

    green_dots = _detect_big_green_dots(weekly, wt1, wt2)
    red_dots   = set(_detect_big_red_dots(wt1, wt2, weekly))
    if not green_dots:
        return out

    # Map weekly date → daily index for entry + exit lookup
    daily_by_date: Dict[str, int] = {}
    daily_dates_sorted: List[str] = []
    for i, db in enumerate(daily_bars):
        d = db.get("date", "")
        if d:
            daily_by_date[d] = i
            daily_dates_sorted.append(d)
    daily_dates_sorted.sort()

    def _date_str(w: dict) -> str:
        d = w.get("date", "")
        if isinstance(d, datetime): d = d.strftime("%Y-%m-%d")
        elif isinstance(d, date):   d = d.strftime("%Y-%m-%d")
        return str(d)[:10]

    def _first_daily_after(week_close_date: str) -> int:
        for d in daily_dates_sorted:
            if d > week_close_date:
                return daily_by_date[d]
        return -1

    # Pre-compute red-dot → first-daily-idx for O(1) lookup
    red_dot_daily_idx: Dict[int, int] = {}
    for w_idx in red_dots:
        wcd = _date_str(weekly[w_idx])
        di = _first_daily_after(wcd)
        if di >= 0:
            red_dot_daily_idx[w_idx] = di

    # Weekly 200-SMA (52-week SMA as proxy)
    def _w200_at(w_idx: int) -> float:
        if w_idx < WEEKLY_200_SMA:
            return 0.0
        return sum(w_closes[w_idx - WEEKLY_200_SMA: w_idx]) / WEEKLY_200_SMA

    # Track position overlap to avoid double-booking a ticker
    active_until_daily_idx = -1

    for w_idx in green_dots:
        # Entry = first daily bar open AFTER weekly close
        week_close_date = _date_str(weekly[w_idx])
        entry_idx = _first_daily_after(week_close_date)
        if entry_idx < 0 or entry_idx >= len(daily_bars):
            continue
        if entry_idx <= active_until_daily_idx:
            continue   # still in a prior trade

        entry_bar = daily_bars[entry_idx]
        ep = entry_bar["o"]
        if ep <= 0:
            continue

        # Weekly ATR at signal
        watr = _at(atr_w, w_idx)
        if watr <= 0:
            watr = ep * 0.04  # 4% fallback

        # Indicators at signal
        wt1_s = _at(wt1, w_idx)
        wt2_s = _at(wt2, w_idx)
        wt1_p = _at(wt1, w_idx - 1)
        wt2_p = _at(wt2, w_idx - 1)
        rsi_v = _at(rsi_w, w_idx)
        stoch_v = _at(stoch_rsi_w, w_idx)
        macd_v = _at(macd_h_w, w_idx)
        sma200 = _w200_at(w_idx)

        # Confluence flags
        w_open = w_opens[w_idx]
        w_close = w_closes[w_idx]
        rsi_ok   = rsi_v < RSI_OVERSOLD_THRESH and rsi_v > 0
        stoch_ok = stoch_v < STOCH_RSI_OVERSOLD_THRESH and stoch_v > 0
        macd_ok  = macd_v < 0
        above200 = sma200 > 0 and w_close > sma200
        bar_g    = w_close > w_open
        conf_count = sum([rsi_ok, stoch_ok, macd_ok, above200, bar_g])

        # Find next red-dot AFTER this green dot
        opp_daily_idx = -1
        opp_week_str = ""
        for rw in sorted(red_dots):
            if rw > w_idx:
                opp_daily_idx = red_dot_daily_idx.get(rw, -1)
                opp_week_str = _date_str(weekly[rw])
                break

        # Stop
        stop_price = ep - WEEKLY_ATR_STOP_MULT * watr

        # Build row with indicator snapshots
        regime_info = regime_map.get(entry_bar.get("date", ""), {})
        row = MCSignal(
            ticker=ticker,
            signal_week_date=week_close_date,
            entry_date=str(entry_bar.get("date", "")),
            entry_price=round(ep, 4),
            wt1_at_signal=round(wt1_s, 4),
            wt2_at_signal=round(wt2_s, 4),
            wt1_prev=round(wt1_p, 4),
            wt2_prev=round(wt2_p, 4),
            rsi_weekly=round(rsi_v, 2),
            stoch_rsi_weekly=round(stoch_v, 2),
            macd_hist_weekly=round(macd_v, 4),
            weekly_200sma=round(sma200, 4),
            weekly_atr=round(watr, 4),
            weekly_bar_open=round(w_open, 4),
            weekly_bar_close=round(w_close, 4),
            rsi_oversold=bool(rsi_ok),
            stoch_rsi_oversold=bool(stoch_ok),
            macd_hist_negative=bool(macd_ok),
            above_weekly_200sma=bool(above200),
            bar_green=bool(bar_g),
            confluence_score=int(conf_count),
            regime_trend=regime_info.get("trend", "UNKNOWN"),
            regime_vol=regime_info.get("vol", "UNKNOWN"),
            stop_initial=round(stop_price, 4),
            opposite_signal_week=opp_week_str,
            opposite_signal_daily_idx=opp_daily_idx,
            is_confirmed_ticker=(ticker in SWING_CONFIRMED_TICKERS),
            is_removed_ticker=(ticker in SWING_REMOVED_TICKERS),
        )

        # ── Trajectory simulation (long-only) ──
        max_extent = min(MAX_HOLD_DAYS, len(daily_bars) - entry_idx - 1)
        mfe_pct = 0.0; mae_pct = 0.0

        first_hit: Dict[int, int] = {t: -1 for t in TARGET_THRESHOLDS_PCT}
        t_stop = -1
        t_opp  = -1

        # Weekly snapshot tracking (every 5 trading days from entry)
        weekly_snap_idx = {w: entry_idx + w * 5 for w in SNAPSHOT_WEEKS}

        # Canonical exit
        exit_reason = None; exit_price = None; exit_date = None
        hold_days_exit = 0

        for offset in range(1, max_extent + 1):
            bar = daily_bars[entry_idx + offset]
            hi = bar["h"]; lo = bar["l"]; cl = bar["c"]

            # MAE/MFE (long-only: MAE = low below entry, MFE = high above)
            mfe_pts = hi - ep
            mae_pts = lo - ep
            if mfe_pts > mfe_pct * ep / 100.0:
                mfe_pct = mfe_pts / ep * 100.0
            if mae_pts < mae_pct * ep / 100.0:
                mae_pct = mae_pts / ep * 100.0

            # Target hits (long-only, so only gains matter)
            gain_hi_pct = (hi - ep) / ep * 100.0
            for t in TARGET_THRESHOLDS_PCT:
                if first_hit[t] == -1 and gain_hi_pct >= t:
                    first_hit[t] = offset

            # Stop hit (intraday low)
            if t_stop == -1 and stop_price > 0 and lo <= stop_price:
                t_stop = offset

            # Opposite-signal hit (next bar at or after opp_daily_idx)
            if t_opp == -1 and opp_daily_idx >= 0 and (entry_idx + offset) >= opp_daily_idx:
                t_opp = offset

            # Canonical exit precedence: stop → opposite_signal
            if exit_reason is None:
                if t_stop == offset:
                    exit_reason = "stop"; exit_price = stop_price
                    exit_date = bar.get("date", ""); hold_days_exit = offset
                elif t_opp == offset:
                    # Exit at the daily bar's open (opposite signal bar)
                    exit_reason = "opposite_signal"
                    exit_price = bar["o"] if (entry_idx + offset) == opp_daily_idx else bar["c"]
                    exit_date = bar.get("date", ""); hold_days_exit = offset

        if exit_reason is None:
            last = daily_bars[entry_idx + max_extent]
            exit_reason = "max_hold"
            exit_price = last["c"]
            exit_date = last.get("date", "")
            hold_days_exit = max_extent

        # Weekly snapshots
        for wk, snap_idx in weekly_snap_idx.items():
            if snap_idx < len(daily_bars):
                sp = daily_bars[snap_idx]["c"]
                setattr(row, f"price_w{wk}", round(sp, 4))
                setattr(row, f"pct_w{wk}", round((sp - ep) / ep * 100.0, 4))

        # Populate trajectory fields
        row.t_to_10pct  = first_hit[10]
        row.t_to_25pct  = first_hit[25]
        row.t_to_50pct  = first_hit[50]
        row.t_to_100pct = first_hit[100]
        row.t_to_200pct = first_hit[200]
        row.t_to_300pct = first_hit[300]
        row.t_to_stop   = t_stop
        row.t_to_opposite_signal = t_opp

        row.mae_pct = round(mae_pct, 4)
        row.mfe_pct = round(mfe_pct, 4)

        row.exit_reason = str(exit_reason)
        row.exit_date = str(exit_date)
        row.exit_price = round(float(exit_price), 4)
        row.hold_days_to_exit = int(hold_days_exit)
        row.pnl_pct = round((row.exit_price - ep) / ep * 100.0, 4)
        row.win = bool(row.pnl_pct > 0)

        out.append(row)
        active_until_daily_idx = entry_idx + hold_days_exit

    return out


# ═══════════════════════════════════════════════════════════
# REGIME MAP
# ═══════════════════════════════════════════════════════════

def build_regime_map(start_date: str, end_date: str) -> Dict[str, Dict[str, str]]:
    log.info(f"Building regime map {start_date} → {end_date}...")
    try:
        spy = download_daily("SPY", start_date, end_date)
        qqq = download_daily("QQQ", start_date, end_date)
        iwm = download_daily("IWM", start_date, end_date)
        vix = download_vix(start_date, end_date)
    except Exception as e:
        log.error(f"Regime data download failed: {e}")
        return {}
    if not spy or not qqq or not iwm:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for d in sorted({b["date"] for b in spy}):
        try:
            _s, trend, vol, _b = compute_regime_for_date(spy, qqq, iwm, vix, d)
            out[d] = {"trend": trend, "vol": vol}
        except Exception:
            out[d] = {"trend": "UNKNOWN", "vol": "UNKNOWN"}
    log.info(f"Regime map: {len(out)} dates")
    return out


# ═══════════════════════════════════════════════════════════
# CSV I/O + STATS
# ═══════════════════════════════════════════════════════════

def _init_csv(path: Path) -> List[str]:
    fields = [f.name for f in _dc_fields(MCSignal)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(fields)
    return fields


def _append_csv(rows: List[MCSignal], path: Path, fields: List[str]) -> None:
    if not rows:
        return
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([getattr(r, k) for k in fields])


def _mode_stats(rows: List[MCSignal], label: str) -> dict:
    n = len(rows)
    if n == 0:
        return {"mode": label, "trades": 0}
    wins = [r for r in rows if r.win]
    losses = [r for r in rows if not r.win]
    total_pnl = sum(r.pnl_pct for r in rows)
    gw = sum(r.pnl_pct for r in wins)
    gl = abs(sum(r.pnl_pct for r in losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    avg_win = (gw / len(wins)) if wins else 0.0
    avg_loss = (gl / len(losses)) if losses else 0.0
    R = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    wr = len(wins) / n
    kelly = (wr - (1 - wr) / R) if R > 0 else 0.0
    return {
        "mode": label, "trades": n, "wins": len(wins),
        "win_rate_pct": round(wr * 100, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "avg_pnl_pct": round(total_pnl / n, 3),
        "total_pnl_pct": round(total_pnl, 2),
        "avg_hold_days": round(sum(r.hold_days_to_exit for r in rows) / n, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(-avg_loss, 3),
        "avg_mfe_pct": round(sum(r.mfe_pct for r in rows) / n, 3),
        "avg_mae_pct": round(sum(r.mae_pct for r in rows) / n, 3),
        "kelly_fraction": round(kelly, 4),
    }


def _bucket_stats(rows: List[MCSignal], key_fn) -> Dict[str, dict]:
    buckets: Dict[str, List[MCSignal]] = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        buckets.setdefault(str(k), []).append(r)
    return {k: _mode_stats(v, k) for k, v in buckets.items()}


def _write_bucket_csv(stats: Dict[str, dict], path: Path, header: str) -> None:
    if not stats:
        path.write_text(f"{header},trades,win_rate_pct,profit_factor,avg_pnl_pct,"
                        "avg_hold_days,avg_mfe_pct,kelly_fraction\n")
        return
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([header, "trades", "wins", "win_rate_pct", "profit_factor",
                    "avg_pnl_pct", "total_pnl_pct", "avg_hold_days",
                    "avg_win_pct", "avg_loss_pct", "avg_mfe_pct", "avg_mae_pct",
                    "kelly_fraction"])
        for s in sorted(stats.values(), key=lambda d: -d.get("trades", 0)):
            w.writerow([s["mode"], s.get("trades", 0), s.get("wins", 0),
                        s.get("win_rate_pct", 0), s.get("profit_factor", 0),
                        s.get("avg_pnl_pct", 0), s.get("total_pnl_pct", 0),
                        s.get("avg_hold_days", 0), s.get("avg_win_pct", 0),
                        s.get("avg_loss_pct", 0), s.get("avg_mfe_pct", 0),
                        s.get("avg_mae_pct", 0), s.get("kelly_fraction", 0)])


def _target_hit_rate_table(rows: List[MCSignal]) -> List[Tuple[int, int, int, float, float]]:
    """Return [(threshold_pct, n_hit, n_total, hit_rate, avg_days_to_hit)]"""
    out: List[Tuple[int, int, int, float, float]] = []
    n = len(rows)
    if n == 0:
        return out
    for t in TARGET_THRESHOLDS_PCT:
        attr = f"t_to_{t}pct"
        hits = [getattr(r, attr) for r in rows if getattr(r, attr) >= 0]
        avg_d = sum(hits) / len(hits) if hits else 0.0
        out.append((t, len(hits), n, round(len(hits) / n * 100.0, 2), round(avg_d, 1)))
    return out


def _write_target_hit_csv(rows: List[MCSignal], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["threshold_pct", "n_hit", "n_total", "hit_rate_pct", "avg_days_to_hit"])
        for row in _target_hit_rate_table(rows):
            w.writerow(row)


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def write_report(rows: List[MCSignal], out_dir: Path, n_tickers: int,
                 start_s: str, end_s: str) -> None:
    total = len(rows)
    lines: List[str] = []
    lines.append("# Weekly Market Cipher Big Green Dot — Backtest Report")
    lines.append("")
    lines.append(f"- Range: **{start_s} → {end_s}**  |  Tickers: **{n_tickers}**")
    lines.append(f"- Entry: WaveTrend bullish cross, WT1 < {WT_OVERSOLD_THRESH}")
    lines.append(f"- Exit: stop (2× weekly ATR) / opposite signal (red dot, WT1 > "
                 f"{WT_OVERBOUGHT_EXIT}) / max hold {MAX_HOLD_WEEKS} weeks")
    lines.append(f"- Grading: spot move, long-only")
    lines.append("")

    if total == 0:
        lines.append("**No signals generated.** Check data availability or widen date range.")
        (out_dir / "report.md").write_text("\n".join(lines) + "\n")
        return

    headline = _mode_stats(rows, "OVERALL")
    lines.append("## Headline")
    lines.append("")
    pf_str = f"{headline['profit_factor']}" if headline['profit_factor'] != "inf" else "inf"
    lines.append(f"- **{total}** signals across {n_tickers} tickers")
    lines.append(f"- **WR {headline['win_rate_pct']:.1f}%**, PF **{pf_str}**, "
                 f"avg PnL **{headline['avg_pnl_pct']:+.2f}%**, "
                 f"**Kelly {headline['kelly_fraction']:+.3f}**")
    lines.append(f"- Avg hold: {headline['avg_hold_days']:.0f} trading days "
                 f"(~{headline['avg_hold_days']/5:.0f} weeks)")
    lines.append(f"- Avg winner: {headline['avg_win_pct']:+.2f}%  |  "
                 f"Avg loser: {headline['avg_loss_pct']:+.2f}%")
    lines.append(f"- Avg MFE (unrealized peak): {headline['avg_mfe_pct']:+.2f}%  |  "
                 f"Avg MAE: {headline['avg_mae_pct']:+.2f}%")
    lines.append("")

    # Target hit rates — the headline answer to "how often do we get big moves?"
    lines.append("## Target Hit Rates (% of signals that reached each gain level)")
    lines.append("")
    lines.append("```")
    lines.append(f"  {'Threshold':<10}  {'Hit':>5}  {'Total':>5}  {'Rate':>6}  {'Avg Days':>9}  {'Weeks':>6}")
    lines.append(f"  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*9}  {'-'*6}")
    for t, hit, tot, rate, avg_d in _target_hit_rate_table(rows):
        wks = avg_d / 5.0 if avg_d else 0.0
        lines.append(f"  +{t}%{' '*(8-len(str(t)))}  {hit:>5}  {tot:>5}  "
                     f"{rate:>5.1f}%  {avg_d:>8.1f}d  {wks:>5.1f}w")
    lines.append("```")
    lines.append("")

    # Confluence ablation — the key question: what filter gives real WR lift?
    lines.append("## Confluence Ablation — impact of each filter layer")
    lines.append("")
    lines.append("*Each row: if you'd filtered ONLY to signals where this flag is True.*")
    lines.append("")
    lines.append("```")
    lines.append(f"  {'Filter':<28}  {'N':>5}  {'WR':>6}  {'PF':>6}  {'AvgPnL':>8}  {'Kelly':>7}")
    lines.append(f"  {'-'*28}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}")
    filters = [
        ("(no filter — all signals)", lambda r: True),
        ("rsi_oversold (<40)",        lambda r: r.rsi_oversold),
        ("stoch_rsi_oversold (<20)",  lambda r: r.stoch_rsi_oversold),
        ("macd_hist_negative",        lambda r: r.macd_hist_negative),
        ("above_weekly_200sma",       lambda r: r.above_weekly_200sma),
        ("bar_green (close > open)",  lambda r: r.bar_green),
        ("confluence_score >= 2",     lambda r: r.confluence_score >= 2),
        ("confluence_score >= 3",     lambda r: r.confluence_score >= 3),
        ("confluence_score >= 4",     lambda r: r.confluence_score >= 4),
        ("confluence_score == 5",     lambda r: r.confluence_score == 5),
    ]
    for label, pred in filters:
        sub = [r for r in rows if pred(r)]
        if not sub:
            lines.append(f"  {label:<28}  {'  0':>5}  {'--':>6}  {'--':>6}  {'--':>8}  {'--':>7}")
            continue
        s = _mode_stats(sub, label)
        pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {label:<28}  {s['trades']:>5}  {s['win_rate_pct']:>5.1f}%  "
                     f"{pf:>6}  {s['avg_pnl_pct']:>+7.2f}%  {s['kelly_fraction']:>+.3f}")
    lines.append("```")
    lines.append("")

    # Exit reason breakdown
    by_exit = _bucket_stats(rows, lambda r: r.exit_reason)
    lines.append("## By Exit Reason")
    lines.append("")
    lines.append("```")
    for k in sorted(by_exit.keys()):
        s = by_exit[k]
        pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {k:<18}  {s['trades']:>5}T  WR {s['win_rate_pct']:>5.1f}%  "
                     f"PF {pf:>6}  PnL {s['avg_pnl_pct']:>+7.2f}%  "
                     f"MFE {s['avg_mfe_pct']:>+7.2f}%")
    lines.append("```")
    lines.append("")

    # Regime
    by_regime = _bucket_stats(rows, lambda r: r.regime_trend)
    lines.append("## By Regime at Entry")
    lines.append("")
    lines.append("```")
    for k in sorted(by_regime.keys()):
        s = by_regime[k]
        pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {k:<18}  {s['trades']:>5}T  WR {s['win_rate_pct']:>5.1f}%  "
                     f"PF {pf:>6}  PnL {s['avg_pnl_pct']:>+7.2f}%")
    lines.append("```")
    lines.append("")

    # Top tickers
    by_tkr = _bucket_stats(rows, lambda r: r.ticker)
    rankable = [(k, s) for k, s in by_tkr.items() if s.get("trades", 0) >= 3]
    def _pfk(x):
        return x[1]["profit_factor"] if x[1]["profit_factor"] != "inf" else 999.0
    lines.append("## Top 20 Tickers by Profit Factor (min 3 signals)")
    lines.append("")
    lines.append("```")
    for k, s in sorted(rankable, key=_pfk, reverse=True)[:20]:
        pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        tag = " [C]" if k in SWING_CONFIRMED_TICKERS else ""
        lines.append(f"  {k:<6}{tag}  {s['trades']:>3}T  WR {s['win_rate_pct']:>5.1f}%  "
                     f"PF {pf:>6}  PnL {s['avg_pnl_pct']:>+6.2f}%  "
                     f"MFE {s['avg_mfe_pct']:>+7.2f}%")
    lines.append("```")
    lines.append("")

    # Weekly snapshots — direction-of-move by horizon
    lines.append("## Average Move % at Weekly Horizons (direction-adjusted)")
    lines.append("")
    lines.append("```")
    for wk in SNAPSHOT_WEEKS:
        field = f"pct_w{wk}"
        vals = [getattr(r, field) for r in rows if getattr(r, field) != 0]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        lines.append(f"  W+{wk:<3}  n={len(vals):>4}  avg {avg:+7.2f}%")
    lines.append("```")
    lines.append("")

    lines.append("## Files")
    lines.append("")
    lines.append("- `trades.csv` — one row per big-green-dot signal (~60 columns)")
    lines.append("- `summary_target_hit_rates.csv` — hit rates at each gain threshold")
    lines.append("- `summary_by_confluence.csv` — per-filter ablation")
    lines.append("- `summary_by_ticker.csv` / `summary_by_regime.csv` / `summary_by_exit_reason.csv`")
    lines.append("")
    lines.append("---")
    lines.append("*Generated by `bt_marketcipher_weekly.py`. WaveTrend math: "
                 "LazyBear formula (n1=10, n2=21). Weekly bars via "
                 "`swing_scanner._aggregate_weekly`. EMA from active_scanner.*")

    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def _load_tickers_file(path: Path) -> List[str]:
    out = []
    with open(path) as f:
        for line in f:
            t = line.strip().upper().split(",")[0].split()[0] if line.strip() else ""
            if t and not t.startswith("#"):
                out.append(t)
    # Dedupe preserving order
    seen = set(); uniq = []
    for t in out:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


def main():
    ap = argparse.ArgumentParser(description="Weekly Market Cipher Big Green Dot Backtest")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--tickers-file", default=None, help="Line-per-ticker file")
    ap.add_argument("--confirmed-only", action="store_true")
    ap.add_argument("--skip-removed", action="store_true")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=1800,
                    help="Lookback days (default 1800 ≈ 5 years)")
    args = ap.parse_args()

    # Ticker selection
    override = os.environ.get("BACKTEST_TICKERS", "").strip()
    if override:
        tickers = [t.strip().upper() for t in override.split(",") if t.strip()]
    elif args.tickers_file:
        tickers = _load_tickers_file(Path(args.tickers_file))
        log.info(f"Loaded {len(tickers)} tickers from {args.tickers_file}")
    elif args.ticker:
        tickers = [args.ticker.upper()]
    elif args.confirmed_only:
        tickers = sorted(SWING_CONFIRMED_TICKERS)
    else:
        tickers = list(DEFAULT_TICKERS)
    if args.skip_removed:
        tickers = [t for t in tickers if t not in SWING_REMOVED_TICKERS]

    # Date range
    end_str = args.to_date or os.environ.get("BACKTEST_END") or date.today().isoformat()
    start_str = args.from_date or os.environ.get("BACKTEST_START") or (
        (date.today() - timedelta(days=args.days)).isoformat()
    )
    log.info(f"Range: {start_str} → {end_str}  ({len(tickers)} tickers, "
             f"max_hold={MAX_HOLD_WEEKS}w)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    regime_map = build_regime_map(start_str, end_str)

    trades_path = OUT_DIR / "trades.csv"
    csv_fields = _init_csv(trades_path)
    all_rows: List[MCSignal] = []
    no_data = 0; errors = 0

    for idx, ticker in enumerate(tickers, 1):
        if idx % 25 == 0 or idx == 1:
            log.info(f"[{idx}/{len(tickers)}] {ticker}: processing...")
        try:
            daily = download_daily(ticker, start_str, end_str)
        except Exception as e:
            log.debug(f"{ticker}: download error: {e}")
            errors += 1
            continue
        if not daily or len(daily) < MIN_DAILY_BARS:
            no_data += 1
            continue
        try:
            rows = run_ticker(ticker, daily, regime_map)
        except Exception as e:
            log.warning(f"{ticker}: run failed: {e}")
            log.debug(traceback.format_exc())
            errors += 1
            continue
        if rows:
            _append_csv(rows, trades_path, csv_fields)
            all_rows.extend(rows)

    log.info(f"DONE. {len(all_rows)} signals across {len(tickers) - no_data - errors} tickers "
             f"({no_data} no-data, {errors} errors)")

    # Summaries
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: r.ticker),
                      OUT_DIR / "summary_by_ticker.csv", "ticker")
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: r.regime_trend),
                      OUT_DIR / "summary_by_regime.csv", "regime")
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: r.exit_reason),
                      OUT_DIR / "summary_by_exit_reason.csv", "exit_reason")
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: f"conf_{r.confluence_score}"),
                      OUT_DIR / "summary_by_confluence.csv", "confluence_score")
    _write_target_hit_csv(all_rows, OUT_DIR / "summary_target_hit_rates.csv")

    # Report
    write_report(all_rows, OUT_DIR, len(tickers), start_str, end_str)
    log.info(f"report.md → {OUT_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
