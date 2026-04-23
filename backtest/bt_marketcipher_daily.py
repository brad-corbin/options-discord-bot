#!/usr/bin/env python3
"""
bt_marketcipher_daily.py — Daily WaveTrend "Big Green Dot" Backtest
════════════════════════════════════════════════════════════════════

Daily-timeframe companion to bt_marketcipher_weekly.py. Same entry logic
(WaveTrend bullish cross in oversold), but on DAILY bars — gives ~10-20×
more signals per ticker, shallower pullback entries, shorter hold horizons.

DESIGN
  - Entry: WaveTrend (LazyBear n1=10, n2=21) bullish cross with WT1 < -40
    (looser than weekly's -53 because daily noise is higher and we want
    regular pullback entries, not deep-washout-only)
  - Long-only
  - Stop: 1.5 × daily ATR (tighter than weekly's 2× — faster timeframe)
  - Opposite signal exit: WT1 crosses below WT2 with WT1 > +53
  - Max hold: 60 trading days (12 weeks)
  - Target thresholds recorded: +5%, +10%, +20%, +35%, +50%, +100%

COMPATIBILITY
  - Produces trades.csv with the SAME column schema as bt_marketcipher_weekly
  - The existing bt_marketcipher_analyze.py works on the output unchanged
    → run the same filter-stack analysis on daily signals

USAGE
    python backtest/bt_marketcipher_daily.py                    # default watchlist
    python backtest/bt_marketcipher_daily.py --days 1800
    python backtest/bt_marketcipher_daily.py --tickers-file /tmp/sp500.txt
    WT_OVERSOLD_THRESH=-53 python backtest/bt_marketcipher_daily.py  # strict

ENV
    WT_OVERSOLD_THRESH           default -40 (set -53 to match weekly semantic)
    WT_OVERBOUGHT_EXIT_THRESH    default +53
    MC_DAILY_MAX_HOLD_DAYS       default 60
    BACKTEST_START/END, BACKTEST_TICKERS, BACKTEST_OUT_DIR  (standard)

OUTPUTS: /tmp/backtest_mc_daily/
    trades.csv
    summary_by_ticker.csv
    summary_by_regime.csv
    summary_by_exit_reason.csv
    summary_by_confluence.csv
    summary_target_hit_rates.csv
    report.md
"""

from __future__ import annotations

import argparse
import csv
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
log = logging.getLogger("bt_mc_daily")

try:
    from trading_rules import SWING_WATCHLIST, SWING_CONFIRMED_TICKERS, SWING_REMOVED_TICKERS
except ImportError as e:
    log.error(f"Cannot import trading_rules: {e}"); sys.exit(1)

try:
    from bt_shared import download_daily, download_vix, compute_regime_for_date
except ImportError as e:
    log.error(f"Cannot import bt_shared: {e}"); sys.exit(1)

try:
    from active_scanner import _compute_ema as _as_ema
except ImportError as e:
    log.error(f"Cannot import active_scanner._compute_ema: {e}"); sys.exit(1)


# ═══════════════════════════════════════════════════════════
# CONSTANTS (daily-scale)
# ═══════════════════════════════════════════════════════════

WT_N1 = 10
WT_N2 = 21
WT_OVERSOLD_THRESH = float(os.environ.get("WT_OVERSOLD_THRESH", "-40"))
WT_OVERBOUGHT_EXIT = float(os.environ.get("WT_OVERBOUGHT_EXIT_THRESH", "53"))

DAILY_RSI_PERIOD    = 14
DAILY_MACD_FAST     = 12
DAILY_MACD_SLOW     = 26
DAILY_MACD_SIGNAL   = 9
DAILY_ATR_PERIOD    = 14
DAILY_200_SMA       = 200

RSI_OVERSOLD_THRESH       = 40
STOCH_RSI_OVERSOLD_THRESH = 20
STOCH_RSI_K_PERIOD        = 14

MAX_HOLD_DAYS       = int(os.environ.get("MC_DAILY_MAX_HOLD_DAYS", "60"))
DAILY_ATR_STOP_MULT = 1.5

# Target thresholds (realistic daily move scale)
TARGET_THRESHOLDS_PCT = [5, 10, 20, 35, 50, 100]

# Fixed-day snapshots (trading days)
SNAPSHOT_DAYS = [1, 3, 5, 10, 20, 30, 45, 60]

# Warmup
MIN_DAILY_BARS = max(DAILY_MACD_SLOW + DAILY_MACD_SIGNAL + DAILY_200_SMA + 5,
                     WT_N2 + WT_N1 + 10)

OUT_DIR = Path(os.environ.get("BACKTEST_OUT_DIR", "/tmp/backtest_mc_daily"))
DEFAULT_TICKERS = sorted(SWING_WATCHLIST)


# ═══════════════════════════════════════════════════════════
# DATACLASS — compatible column schema with weekly variant
# (reuses same field names; "weekly_*" fields become "daily-timeframe values"
#  for analyzer compatibility — the analyzer reads column names not semantics)
# ═══════════════════════════════════════════════════════════

@dataclass
class MCSignal:
    ticker: str = ""
    signal_week_date: str = ""       # actually the signal-bar date (daily)
    entry_date: str = ""
    entry_price: float = 0.0

    wt1_at_signal: float = 0.0
    wt2_at_signal: float = 0.0
    wt1_prev: float = 0.0
    wt2_prev: float = 0.0

    rsi_weekly: float = 0.0           # actually daily RSI (field-name compat)
    stoch_rsi_weekly: float = 0.0
    macd_hist_weekly: float = 0.0
    weekly_200sma: float = 0.0        # 200-day SMA
    weekly_atr: float = 0.0           # daily ATR
    weekly_bar_open: float = 0.0      # signal-bar open
    weekly_bar_close: float = 0.0     # signal-bar close

    rsi_oversold: bool = False
    stoch_rsi_oversold: bool = False
    macd_hist_negative: bool = False
    above_weekly_200sma: bool = False
    bar_green: bool = False
    confluence_score: int = 0

    regime_trend: str = ""
    regime_vol: str = ""

    stop_initial: float = 0.0
    opposite_signal_week: str = ""
    opposite_signal_daily_idx: int = -1

    # Daily targets (renamed from pct levels)
    t_to_5pct: int = -1
    t_to_10pct: int = -1
    t_to_20pct: int = -1
    t_to_35pct: int = -1
    t_to_50pct: int = -1
    t_to_100pct: int = -1
    t_to_stop: int = -1
    t_to_opposite_signal: int = -1

    mae_pct: float = 0.0
    mfe_pct: float = 0.0

    # Fixed-day snapshots (day offsets)
    price_d1: float = 0.0
    price_d3: float = 0.0
    price_d5: float = 0.0
    price_d10: float = 0.0
    price_d20: float = 0.0
    price_d30: float = 0.0
    price_d45: float = 0.0
    price_d60: float = 0.0
    pct_d1: float = 0.0
    pct_d3: float = 0.0
    pct_d5: float = 0.0
    pct_d10: float = 0.0
    pct_d20: float = 0.0
    pct_d30: float = 0.0
    pct_d45: float = 0.0
    pct_d60: float = 0.0

    exit_reason: str = ""
    exit_date: str = ""
    exit_price: float = 0.0
    hold_days_to_exit: int = 0
    pnl_pct: float = 0.0
    win: bool = False

    is_confirmed_ticker: bool = False
    is_removed_ticker: bool = False


# ═══════════════════════════════════════════════════════════
# INDICATOR SERIES (same math as weekly variant, applied to daily)
# ═══════════════════════════════════════════════════════════

def _ema_series(values: List[float], period: int) -> List[float]:
    return _as_ema(values, period) or []


def _rsi_series(closes: List[float], period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff)); losses.append(max(0, -diff))
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


def _stoch_rsi_series(closes: List[float], rsi_p: int = 14, stoch_p: int = 14) -> List[float]:
    rsi = _rsi_series(closes, rsi_p)
    if len(rsi) < stoch_p:
        return []
    out = []
    for i in range(stoch_p - 1, len(rsi)):
        window = rsi[i - stoch_p + 1: i + 1]
        lo = min(window); hi = max(window)
        out.append(((rsi[i] - lo) / (hi - lo) * 100.0) if hi > lo else 50.0)
    return out


def _macd_hist_series(closes: List[float]) -> List[float]:
    if len(closes) < DAILY_MACD_SLOW + DAILY_MACD_SIGNAL:
        return []
    fast = _ema_series(closes, DAILY_MACD_FAST)
    slow = _ema_series(closes, DAILY_MACD_SLOW)
    if not fast or not slow:
        return []
    off = len(fast) - len(slow)
    macd_line = [fast[i + off] - slow[i] for i in range(len(slow))]
    if len(macd_line) < DAILY_MACD_SIGNAL:
        return []
    sig = _ema_series(macd_line, DAILY_MACD_SIGNAL)
    so = len(macd_line) - len(sig)
    return [macd_line[i + so] - sig[i] for i in range(len(sig))]


def _atr_series(highs, lows, closes, period=14) -> List[float]:
    if len(highs) < period + 1:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return []
    out = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out


def _wavetrend_series(highs, lows, closes) -> Tuple[List[float], List[float]]:
    n = min(len(highs), len(lows), len(closes))
    if n < WT_N2 + WT_N1 + 4:
        return [], []
    hlc3 = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    esa = _ema_series(hlc3, WT_N1)
    if not esa:
        return [], []
    oe = len(hlc3) - len(esa)
    d = [abs(hlc3[i + oe] - esa[i]) for i in range(len(esa))]
    de = _ema_series(d, WT_N1)
    if not de:
        return [], []
    od = len(d) - len(de)
    ci = []
    for i in range(len(de)):
        dv = de[i]; ev = esa[i + od]; hv = hlc3[i + oe + od]
        ci.append((hv - ev) / (0.015 * dv) if dv != 0 else 0.0)
    wt1 = _ema_series(ci, WT_N2)
    if not wt1 or len(wt1) < 4:
        return [], []
    wt2 = _ema_series(wt1, 4)
    if not wt2:
        return [], []
    off = len(wt1) - len(wt2)
    return wt1[off:], wt2


def _detect_green_dots_daily(daily_bars, wt1, wt2) -> List[int]:
    if len(wt1) != len(wt2) or len(wt1) < 2:
        return []
    offset = len(daily_bars) - len(wt1)
    if offset < 0:
        return []
    out = []
    for j in range(1, len(wt1)):
        if wt1[j - 1] < wt2[j - 1] and wt1[j] > wt2[j]:
            if wt1[j] < WT_OVERSOLD_THRESH:
                out.append(j + offset)
    return out


def _detect_red_dots_daily(wt1, wt2, daily_bars) -> List[int]:
    if len(wt1) != len(wt2) or len(wt1) < 2:
        return []
    offset = len(daily_bars) - len(wt1)
    if offset < 0:
        return []
    out = []
    for j in range(1, len(wt1)):
        if wt1[j - 1] > wt2[j - 1] and wt1[j] < wt2[j]:
            if wt1[j] > WT_OVERBOUGHT_EXIT:
                out.append(j + offset)
    return out


# ═══════════════════════════════════════════════════════════
# PER-TICKER BACKTEST
# ═══════════════════════════════════════════════════════════

def run_ticker(ticker: str, daily_bars: List[dict],
               regime_map: Dict[str, Dict[str, str]]) -> List[MCSignal]:
    out: List[MCSignal] = []
    if len(daily_bars) < MIN_DAILY_BARS:
        return out

    highs  = [b["h"] for b in daily_bars]
    lows   = [b["l"] for b in daily_bars]
    closes = [b["c"] for b in daily_bars]
    opens  = [b["o"] for b in daily_bars]

    wt1, wt2 = _wavetrend_series(highs, lows, closes)
    if not wt1:
        return out

    rsi_s   = _rsi_series(closes, DAILY_RSI_PERIOD)
    stoch_s = _stoch_rsi_series(closes, DAILY_RSI_PERIOD, STOCH_RSI_K_PERIOD)
    macd_s  = _macd_hist_series(closes)
    atr_s   = _atr_series(highs, lows, closes, DAILY_ATR_PERIOD)

    def _at(series: List[float], d_idx: int) -> float:
        if not series:
            return 0.0
        off = len(daily_bars) - len(series)
        j = d_idx - off
        if 0 <= j < len(series):
            return float(series[j])
        return 0.0

    def _sma_at(d_idx: int, period: int) -> float:
        if d_idx < period:
            return 0.0
        return sum(closes[d_idx - period: d_idx]) / period

    green = _detect_green_dots_daily(daily_bars, wt1, wt2)
    red = set(_detect_red_dots_daily(wt1, wt2, daily_bars))
    if not green:
        return out

    active_until = -1

    for g_idx in green:
        if g_idx <= active_until:
            continue
        if g_idx + 1 >= len(daily_bars):
            continue

        entry_idx = g_idx + 1
        entry_bar = daily_bars[entry_idx]
        ep = entry_bar["o"]
        if ep <= 0:
            continue

        atr = _at(atr_s, g_idx)
        if atr <= 0:
            atr = ep * 0.02

        wt1_s = _at(wt1, g_idx); wt2_s = _at(wt2, g_idx)
        wt1_p = _at(wt1, g_idx - 1); wt2_p = _at(wt2, g_idx - 1)
        rsi_v = _at(rsi_s, g_idx); stoch_v = _at(stoch_s, g_idx)
        macd_v = _at(macd_s, g_idx); sma200 = _sma_at(g_idx, DAILY_200_SMA)

        bar_o = opens[g_idx]; bar_c = closes[g_idx]
        rsi_ok = 0 < rsi_v < RSI_OVERSOLD_THRESH
        stoch_ok = 0 < stoch_v < STOCH_RSI_OVERSOLD_THRESH
        macd_ok = macd_v < 0
        above200 = sma200 > 0 and bar_c > sma200
        bar_g = bar_c > bar_o
        conf = sum([rsi_ok, stoch_ok, macd_ok, above200, bar_g])

        # Next red dot
        opp_idx = -1; opp_date = ""
        for rd in sorted(red):
            if rd > g_idx:
                opp_idx = rd + 1 if (rd + 1) < len(daily_bars) else rd
                opp_date = str(daily_bars[rd].get("date", ""))
                break

        stop = ep - DAILY_ATR_STOP_MULT * atr

        regime_info = regime_map.get(entry_bar.get("date", ""), {})
        row = MCSignal(
            ticker=ticker,
            signal_week_date=str(daily_bars[g_idx].get("date", "")),
            entry_date=str(entry_bar.get("date", "")),
            entry_price=round(ep, 4),
            wt1_at_signal=round(wt1_s, 4), wt2_at_signal=round(wt2_s, 4),
            wt1_prev=round(wt1_p, 4), wt2_prev=round(wt2_p, 4),
            rsi_weekly=round(rsi_v, 2), stoch_rsi_weekly=round(stoch_v, 2),
            macd_hist_weekly=round(macd_v, 4),
            weekly_200sma=round(sma200, 4),
            weekly_atr=round(atr, 4),
            weekly_bar_open=round(bar_o, 4), weekly_bar_close=round(bar_c, 4),
            rsi_oversold=bool(rsi_ok), stoch_rsi_oversold=bool(stoch_ok),
            macd_hist_negative=bool(macd_ok),
            above_weekly_200sma=bool(above200),
            bar_green=bool(bar_g),
            confluence_score=int(conf),
            regime_trend=regime_info.get("trend", "UNKNOWN"),
            regime_vol=regime_info.get("vol", "UNKNOWN"),
            stop_initial=round(stop, 4),
            opposite_signal_week=opp_date,
            opposite_signal_daily_idx=opp_idx,
            is_confirmed_ticker=(ticker in SWING_CONFIRMED_TICKERS),
            is_removed_ticker=(ticker in SWING_REMOVED_TICKERS),
        )

        # Trajectory simulation
        max_ext = min(MAX_HOLD_DAYS, len(daily_bars) - entry_idx - 1)
        if max_ext <= 0:
            continue

        first_hit = {t: -1 for t in TARGET_THRESHOLDS_PCT}
        t_stop = -1; t_opp = -1
        mfe_pct = 0.0; mae_pct = 0.0
        exit_reason = None; exit_price = None; exit_date = None; hold_d = 0
        snap_set = set(SNAPSHOT_DAYS)

        for offset in range(1, max_ext + 1):
            bar = daily_bars[entry_idx + offset]
            hi = bar["h"]; lo = bar["l"]; cl = bar["c"]

            mfe_pts = hi - ep; mae_pts = lo - ep
            if mfe_pts / ep * 100.0 > mfe_pct: mfe_pct = mfe_pts / ep * 100.0
            if mae_pts / ep * 100.0 < mae_pct: mae_pct = mae_pts / ep * 100.0

            gain_pct = (hi - ep) / ep * 100.0
            for t in TARGET_THRESHOLDS_PCT:
                if first_hit[t] == -1 and gain_pct >= t:
                    first_hit[t] = offset

            if t_stop == -1 and stop > 0 and lo <= stop:
                t_stop = offset
            if t_opp == -1 and opp_idx >= 0 and (entry_idx + offset) >= opp_idx:
                t_opp = offset

            if offset in snap_set:
                setattr(row, f"price_d{offset}", round(cl, 4))
                setattr(row, f"pct_d{offset}", round((cl - ep) / ep * 100.0, 4))

            if exit_reason is None:
                if t_stop == offset:
                    exit_reason = "stop"; exit_price = stop
                    exit_date = bar.get("date", ""); hold_d = offset
                elif t_opp == offset:
                    exit_reason = "opposite_signal"
                    exit_price = bar["o"] if (entry_idx + offset) == opp_idx else bar["c"]
                    exit_date = bar.get("date", ""); hold_d = offset

        if exit_reason is None:
            last = daily_bars[entry_idx + max_ext]
            exit_reason = "max_hold"; exit_price = last["c"]
            exit_date = last.get("date", ""); hold_d = max_ext

        row.t_to_5pct   = first_hit[5]
        row.t_to_10pct  = first_hit[10]
        row.t_to_20pct  = first_hit[20]
        row.t_to_35pct  = first_hit[35]
        row.t_to_50pct  = first_hit[50]
        row.t_to_100pct = first_hit[100]
        row.t_to_stop = t_stop; row.t_to_opposite_signal = t_opp
        row.mae_pct = round(mae_pct, 4); row.mfe_pct = round(mfe_pct, 4)

        row.exit_reason = str(exit_reason); row.exit_date = str(exit_date)
        row.exit_price = round(float(exit_price), 4); row.hold_days_to_exit = int(hold_d)
        row.pnl_pct = round((row.exit_price - ep) / ep * 100.0, 4)
        row.win = bool(row.pnl_pct > 0)

        out.append(row)
        active_until = entry_idx + hold_d

    return out


# ═══════════════════════════════════════════════════════════
# REGIME / I/O / STATS / REPORT
# ═══════════════════════════════════════════════════════════

def build_regime_map(start_date, end_date):
    log.info(f"Building regime map {start_date} → {end_date}...")
    try:
        spy = download_daily("SPY", start_date, end_date)
        qqq = download_daily("QQQ", start_date, end_date)
        iwm = download_daily("IWM", start_date, end_date)
        vix = download_vix(start_date, end_date)
    except Exception as e:
        log.error(f"regime download failed: {e}"); return {}
    if not spy or not qqq or not iwm:
        return {}
    out = {}
    for d in sorted({b["date"] for b in spy}):
        try:
            _, trend, vol, _b = compute_regime_for_date(spy, qqq, iwm, vix, d)
            out[d] = {"trend": trend, "vol": vol}
        except Exception:
            out[d] = {"trend": "UNKNOWN", "vol": "UNKNOWN"}
    log.info(f"  {len(out)} dates")
    return out


def _init_csv(path):
    fields = [f.name for f in _dc_fields(MCSignal)]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerow(fields)
    return fields


def _append_csv(rows, path, fields):
    if not rows: return
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        for r in rows: w.writerow([getattr(r, k) for k in fields])


def _mode_stats(rows, label):
    n = len(rows)
    if n == 0: return {"mode": label, "trades": 0}
    wins = [r for r in rows if r.win]
    losses = [r for r in rows if not r.win]
    total = sum(r.pnl_pct for r in rows)
    gw = sum(r.pnl_pct for r in wins)
    gl = abs(sum(r.pnl_pct for r in losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    aw = gw / len(wins) if wins else 0.0
    al = gl / len(losses) if losses else 0.0
    R = aw / al if al > 0 else 0.0
    wr = len(wins) / n
    kelly = wr - (1 - wr) / R if R > 0 else 0.0
    return {
        "mode": label, "trades": n, "wins": len(wins),
        "win_rate_pct": round(wr * 100, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "avg_pnl_pct": round(total / n, 3),
        "total_pnl_pct": round(total, 2),
        "avg_hold_days": round(sum(r.hold_days_to_exit for r in rows) / n, 2),
        "avg_win_pct": round(aw, 3), "avg_loss_pct": round(-al, 3),
        "avg_mfe_pct": round(sum(r.mfe_pct for r in rows) / n, 3),
        "avg_mae_pct": round(sum(r.mae_pct for r in rows) / n, 3),
        "kelly_fraction": round(kelly, 4),
    }


def _bucket_stats(rows, key_fn):
    buckets = {}
    for r in rows:
        k = key_fn(r)
        if k is None: continue
        buckets.setdefault(str(k), []).append(r)
    return {k: _mode_stats(v, k) for k, v in buckets.items()}


def _write_bucket_csv(stats, path, header):
    if not stats:
        path.write_text(f"{header},trades\n"); return
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


def _target_hit_table(rows):
    n = len(rows)
    out = []
    if n == 0: return out
    for t in TARGET_THRESHOLDS_PCT:
        attr = f"t_to_{t}pct"
        hits = [getattr(r, attr) for r in rows if getattr(r, attr) >= 0]
        avg_d = sum(hits) / len(hits) if hits else 0.0
        out.append((t, len(hits), n, round(len(hits) / n * 100, 2), round(avg_d, 1)))
    return out


def _write_target_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["threshold_pct", "n_hit", "n_total", "hit_rate_pct", "avg_days_to_hit"])
        for row in _target_hit_table(rows):
            w.writerow(row)


def write_report(rows, out_dir, n_tickers, start_s, end_s):
    total = len(rows)
    lines = ["# Daily WaveTrend Big Green Dot — Backtest Report", "",
             f"- Range: **{start_s} → {end_s}**  |  Tickers: **{n_tickers}**",
             f"- Entry: WaveTrend bullish cross, WT1 < {WT_OVERSOLD_THRESH} (daily)",
             f"- Exit: stop (1.5× daily ATR) / red dot (WT1 > {WT_OVERBOUGHT_EXIT}) / max hold {MAX_HOLD_DAYS}d",
             f"- Long-only, spot grading", ""]

    if total == 0:
        lines.append("**No signals generated.**")
        (out_dir / "report.md").write_text("\n".join(lines) + "\n"); return

    h = _mode_stats(rows, "OVERALL")
    pf = f"{h['profit_factor']}" if h['profit_factor'] != "inf" else "inf"
    lines += ["## Headline", "",
              f"- **{total}** signals across {n_tickers} tickers",
              f"- **WR {h['win_rate_pct']:.1f}%**, PF **{pf}**, "
              f"avg PnL **{h['avg_pnl_pct']:+.2f}%**, **Kelly {h['kelly_fraction']:+.3f}**",
              f"- Avg hold: {h['avg_hold_days']:.1f} trading days",
              f"- Avg winner: {h['avg_win_pct']:+.2f}%  |  "
              f"Avg loser: {h['avg_loss_pct']:+.2f}%",
              f"- Avg MFE: {h['avg_mfe_pct']:+.2f}%  |  Avg MAE: {h['avg_mae_pct']:+.2f}%",
              ""]

    lines += ["## Target Hit Rates", "", "```",
              f"  {'Threshold':<10}  {'Hit':>6}  {'Total':>6}  {'Rate':>6}  {'Avg Days':>9}",
              f"  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*9}"]
    for t, hit, tot, rate, avg_d in _target_hit_table(rows):
        lines.append(f"  +{t}%{' '*(8-len(str(t)))}  {hit:>6}  {tot:>6}  "
                     f"{rate:>5.1f}%  {avg_d:>8.1f}d")
    lines += ["```", ""]

    lines += ["## Confluence Ablation", "",
              "*For each filter: stats if you kept ONLY signals where it's True.*", "",
              "```",
              f"  {'Filter':<28}  {'N':>5}  {'WR':>6}  {'PF':>6}  {'AvgPnL':>8}  {'Kelly':>7}",
              f"  {'-'*28}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}"]
    filters = [
        ("(no filter)", lambda r: True),
        ("rsi_oversold (<40)", lambda r: r.rsi_oversold),
        ("stoch_rsi_oversold (<20)", lambda r: r.stoch_rsi_oversold),
        ("macd_hist_negative", lambda r: r.macd_hist_negative),
        ("above_200sma", lambda r: r.above_weekly_200sma),
        ("bar_green", lambda r: r.bar_green),
        ("confluence_score >= 2", lambda r: r.confluence_score >= 2),
        ("confluence_score >= 3", lambda r: r.confluence_score >= 3),
        ("confluence_score >= 4", lambda r: r.confluence_score >= 4),
    ]
    for lbl, p in filters:
        sub = [r for r in rows if p(r)]
        if not sub:
            lines.append(f"  {lbl:<28}  {'  0':>5}  {'--':>6}  {'--':>6}  {'--':>8}  {'--':>7}")
            continue
        s = _mode_stats(sub, lbl)
        pfs = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {lbl:<28}  {s['trades']:>5}  {s['win_rate_pct']:>5.1f}%  "
                     f"{pfs:>6}  {s['avg_pnl_pct']:>+7.2f}%  {s['kelly_fraction']:>+.3f}")
    lines += ["```", ""]

    by_exit = _bucket_stats(rows, lambda r: r.exit_reason)
    lines += ["## By Exit Reason", "", "```"]
    for k in sorted(by_exit.keys()):
        s = by_exit[k]; pfs = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {k:<18}  {s['trades']:>5}T  WR {s['win_rate_pct']:>5.1f}%  "
                     f"PF {pfs:>6}  PnL {s['avg_pnl_pct']:>+7.2f}%  MFE {s['avg_mfe_pct']:>+7.2f}%")
    lines += ["```", ""]

    by_regime = _bucket_stats(rows, lambda r: r.regime_trend)
    lines += ["## By Regime", "", "```"]
    for k in sorted(by_regime.keys()):
        s = by_regime[k]; pfs = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {k:<18}  {s['trades']:>5}T  WR {s['win_rate_pct']:>5.1f}%  "
                     f"PF {pfs:>6}  PnL {s['avg_pnl_pct']:>+7.2f}%")
    lines += ["```", ""]

    by_tkr = _bucket_stats(rows, lambda r: r.ticker)
    rankable = [(k, s) for k, s in by_tkr.items() if s.get("trades", 0) >= 5]
    def _pfk(x): return x[1]["profit_factor"] if x[1]["profit_factor"] != "inf" else 999.0
    lines += ["## Top 20 Tickers by PF (min 5 signals)", "", "```"]
    for k, s in sorted(rankable, key=_pfk, reverse=True)[:20]:
        pfs = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        tag = " [C]" if k in SWING_CONFIRMED_TICKERS else ""
        lines.append(f"  {k:<6}{tag}  {s['trades']:>4}T  WR {s['win_rate_pct']:>5.1f}%  "
                     f"PF {pfs:>6}  PnL {s['avg_pnl_pct']:>+6.2f}%  MFE {s['avg_mfe_pct']:>+7.2f}%")
    lines += ["```", "",
              "## Files", "",
              "- `trades.csv` — signal rows (same schema as weekly variant)",
              "- Run `bt_marketcipher_analyze.py --csv /tmp/backtest_mc_daily/trades.csv` "
              "for filter stack analysis",
              "",
              "---",
              "*Generated by `bt_marketcipher_daily.py`. WT1 oversold threshold: "
              f"{WT_OVERSOLD_THRESH}. Max hold: {MAX_HOLD_DAYS} days.*"]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def _load_tickers_file(path):
    out = []
    with open(path) as f:
        for line in f:
            t = line.strip().upper().split(",")[0].split()[0] if line.strip() else ""
            if t and not t.startswith("#"): out.append(t)
    seen = set(); uniq = []
    for t in out:
        if t not in seen: seen.add(t); uniq.append(t)
    return uniq


def main():
    ap = argparse.ArgumentParser(description="Daily WaveTrend Big Green Dot Backtest")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--tickers-file", default=None)
    ap.add_argument("--confirmed-only", action="store_true")
    ap.add_argument("--skip-removed", action="store_true")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=1800)
    args = ap.parse_args()

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

    end_str = args.to_date or os.environ.get("BACKTEST_END") or date.today().isoformat()
    start_str = args.from_date or os.environ.get("BACKTEST_START") or (
        (date.today() - timedelta(days=args.days)).isoformat())
    log.info(f"Range: {start_str} → {end_str}  ({len(tickers)} tickers, "
             f"oversold={WT_OVERSOLD_THRESH}, max_hold={MAX_HOLD_DAYS}d)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    regime_map = build_regime_map(start_str, end_str)

    trades_path = OUT_DIR / "trades.csv"
    csv_fields = _init_csv(trades_path)
    all_rows = []
    nodata = 0; errors = 0

    for idx, ticker in enumerate(tickers, 1):
        if idx % 25 == 0 or idx == 1:
            log.info(f"[{idx}/{len(tickers)}] {ticker}: processing...")
        try:
            daily = download_daily(ticker, start_str, end_str)
        except Exception as e:
            log.debug(f"{ticker}: download: {e}"); errors += 1; continue
        if not daily or len(daily) < MIN_DAILY_BARS:
            nodata += 1; continue
        try:
            rows = run_ticker(ticker, daily, regime_map)
        except Exception as e:
            log.warning(f"{ticker}: run failed: {e}")
            log.debug(traceback.format_exc()); errors += 1; continue
        if rows:
            _append_csv(rows, trades_path, csv_fields)
            all_rows.extend(rows)

    log.info(f"DONE. {len(all_rows)} signals across "
             f"{len(tickers) - nodata - errors} tickers "
             f"({nodata} no-data, {errors} errors)")

    _write_bucket_csv(_bucket_stats(all_rows, lambda r: r.ticker),
                      OUT_DIR / "summary_by_ticker.csv", "ticker")
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: r.regime_trend),
                      OUT_DIR / "summary_by_regime.csv", "regime")
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: r.exit_reason),
                      OUT_DIR / "summary_by_exit_reason.csv", "exit_reason")
    _write_bucket_csv(_bucket_stats(all_rows, lambda r: f"conf_{r.confluence_score}"),
                      OUT_DIR / "summary_by_confluence.csv", "confluence_score")
    _write_target_csv(all_rows, OUT_DIR / "summary_target_hit_rates.csv")
    write_report(all_rows, OUT_DIR, len(tickers), start_str, end_str)
    log.info(f"report.md → {OUT_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
