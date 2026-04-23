#!/usr/bin/env python3
"""
bt_weekly_diamond.py — Weekly Diamond Gate vs Direct Entry
═══════════════════════════════════════════════════════════════════════════

Tests whether the daily diamond concept, lifted to the WEEKLY timeframe,
produces a better edge than taking daily swing signals alone.

Two modes tested side-by-side:

  WEEKLY_GATE
    1. Weekly bar closes with diamond active (both weekly EMA-diff and
       weekly MACD-hist in middle quintiles {Q2,Q3,Q4})
    2. Opens a 5-trading-day "window" starting the next trading day
    3. First `analyze_swing_setup()` daily signal to fire during the
       window triggers entry (next-bar open, direction from daily signal)
    4. Canonical exit: stop / T1 / T2 / max_hold=150D

  WEEKLY_ENTRY
    1. Weekly bar closes with diamond active
    2. Direction derived from sign agreement of weekly EMA-diff and
       weekly MACD-hist:
         both + → bull
         both − → bear
         disagree → skip (no trade)
    3. Enter at next Monday's open
    4. Targets: fib 1.272 / 1.618 extensions from most recent daily
       swing high→low pair. Stop = 2×daily ATR (matches v3.2).
    5. Canonical exit: stop / T1 / T2 / max_hold=150D

COMPARISON
  Baseline numbers (1,083 signals, 40.0% WR, PF 1.12) are pulled from
  the v3.2 trades.csv if present at BASELINE_TRADES_CSV env var. The
  three-way report renders WR/PF/Kelly side by side.

DESIGN PRINCIPLE — NO DUPLICATION
  - `analyze_swing_setup` imported verbatim from swing_scanner (daily signal)
  - `_aggregate_weekly` reused from swing_scanner (weekly bar construction)
  - `_compute_ema / _compute_macd` reused from active_scanner
  - `compute_fib_levels` reused from swing_scanner (for WEEKLY_ENTRY targets)
  - Data loading + regime via bt_shared

USAGE
    python backtest/bt_weekly_diamond.py                    # full watchlist
    python backtest/bt_weekly_diamond.py --ticker NVDA
    python backtest/bt_weekly_diamond.py --confirmed-only
    python backtest/bt_weekly_diamond.py --skip-removed

ENV
    BACKTEST_START/END           YYYY-MM-DD overrides
    BACKTEST_TICKERS             comma-separated override
    BACKTEST_OUT_DIR             default /tmp/backtest_weekly_diamond
    WEEKLY_GATE_WINDOW_DAYS      window size for GATE mode (default 5)
    WEEKLY_EMA_FAST/SLOW         default 5, 12
    WEEKLY_MACD_FAST/SLOW/SIGNAL default 12, 26, 9
    BASELINE_TRADES_CSV          path to v3.2 trades.csv for comparison
                                 (default /tmp/backtest_swing_v32/trades.csv)

OUTPUTS: /tmp/backtest_weekly_diamond/
    trades_weekly_gate.csv           one row per GATE-mode signal
    trades_weekly_entry.csv          one row per ENTRY-mode signal
    summary_gate_by_*.csv            GATE breakdowns
    summary_entry_by_*.csv           ENTRY breakdowns
    comparison.md                    side-by-side headline report
    weekly_diamond_events.csv        raw list of weekly diamond fires
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
from dataclasses import dataclass, field, fields as _dc_fields
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
log = logging.getLogger("bt_weekly_diamond")

try:
    from swing_scanner import (
        analyze_swing_setup, _aggregate_weekly, compute_fib_levels,
    )
    log.info("Loaded swing_scanner.analyze_swing_setup + _aggregate_weekly + compute_fib_levels")
except ImportError as e:
    log.error(f"Cannot import swing_scanner: {e}")
    sys.exit(1)

try:
    from trading_rules import (
        SWING_WATCHLIST, SWING_CONFIRMED_TICKERS, SWING_REMOVED_TICKERS,
    )
    log.info(f"SWING_WATCHLIST: {len(SWING_WATCHLIST)} tickers "
             f"(confirmed {len(SWING_CONFIRMED_TICKERS)}, "
             f"removed {len(SWING_REMOVED_TICKERS)})")
except ImportError as e:
    log.error(f"Cannot import trading_rules: {e}")
    sys.exit(1)

try:
    from bt_shared import download_daily, download_vix, compute_regime_for_date
    log.info("Loaded bt_shared")
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

WEEKLY_EMA_FAST   = int(os.environ.get("WEEKLY_EMA_FAST", "5"))
WEEKLY_EMA_SLOW   = int(os.environ.get("WEEKLY_EMA_SLOW", "12"))
WEEKLY_MACD_FAST  = int(os.environ.get("WEEKLY_MACD_FAST", "12"))
WEEKLY_MACD_SLOW  = int(os.environ.get("WEEKLY_MACD_SLOW", "26"))
WEEKLY_MACD_SIGNAL = int(os.environ.get("WEEKLY_MACD_SIGNAL", "9"))

GATE_WINDOW_DAYS  = int(os.environ.get("WEEKLY_GATE_WINDOW_DAYS", "5"))
MAX_HOLD_DAYS     = int(os.environ.get("SWING_BT_MAX_HOLD", "150"))
STOP_ATR_MULT     = 0.2
ATR_STOP_CAP      = 2.0
COOLDOWN_BARS     = 3

# Minimum warmup so weekly indicators are populated + 200 daily SMA
MIN_WARMUP_BARS_DAILY  = 220
MIN_WARMUP_BARS_WEEKLY = max(WEEKLY_EMA_SLOW, WEEKLY_MACD_SLOW) + WEEKLY_MACD_SIGNAL + 4

# Snapshot horizons + EM horizons (matches v3.2 for apples-to-apples)
SNAPSHOT_DAYS = [1, 3, 5, 10, 20, 30, 60, 90, 150]
EM_HORIZONS_TRADING_DAYS = [14, 21, 30]
REALIZED_VOL_LOOKBACK = 30

OUT_DIR  = Path(os.environ.get("BACKTEST_OUT_DIR", "/tmp/backtest_weekly_diamond"))
BASELINE_TRADES_CSV = Path(os.environ.get(
    "BASELINE_TRADES_CSV", "/tmp/backtest_swing_v32/trades.csv"))
DEFAULT_TICKERS = sorted(SWING_WATCHLIST)


# ═══════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════

@dataclass
class WeeklySignal:
    """Single weekly-diamond-triggered trade."""
    # Identity
    ticker: str = ""
    mode: str = ""                   # "WEEKLY_GATE" or "WEEKLY_ENTRY"
    direction: str = ""
    weekly_diamond_date: str = ""    # week-close date where diamond fired

    # Weekly diamond context
    weekly_ema_diff: float = 0.0
    weekly_macd_hist: float = 0.0
    weekly_ema_diff_q: str = ""      # Q1..Q5
    weekly_macd_hist_q: str = ""

    # Daily signal (only populated for WEEKLY_GATE)
    daily_signal_date: str = ""
    daily_tier: int = 0
    daily_fib_level: str = ""
    daily_setup_quality: str = ""
    daily_confidence: int = 0
    daily_rs_vs_spy: float = 0.0
    daily_rsi: float = 0.0

    # Entry
    entry_date: str = ""
    entry_price: float = 0.0
    atr_val: float = 0.0
    stop: float = 0.0
    target1: float = 0.0
    target2: float = 0.0

    # Regime
    regime_trend: str = ""
    regime_vol: str = ""

    # Fixed-day snapshots
    price_d1: float = 0.0
    price_d5: float = 0.0
    price_d10: float = 0.0
    price_d20: float = 0.0
    price_d30: float = 0.0
    price_d60: float = 0.0
    price_d90: float = 0.0
    price_d150: float = 0.0
    move_pct_d1: float = 0.0
    move_pct_d5: float = 0.0
    move_pct_d10: float = 0.0
    move_pct_d20: float = 0.0
    move_pct_d30: float = 0.0
    move_pct_d60: float = 0.0
    move_pct_d90: float = 0.0
    move_pct_d150: float = 0.0

    # First-hit
    t_to_target1: int = -1
    t_to_target2: int = -1
    t_to_stop: int = -1

    # Exit
    exit_reason: str = ""
    exit_date: str = ""
    exit_price: float = 0.0
    hold_days_to_exit: int = 0
    pnl_pts: float = 0.0
    pnl_pct: float = 0.0
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    win: bool = False

    # Watchlist tags
    is_confirmed_ticker: bool = False
    is_removed_ticker: bool = False


# ═══════════════════════════════════════════════════════════
# WEEKLY INDICATOR COMPUTATION
# ═══════════════════════════════════════════════════════════

def _weekly_ema_diff_series(weekly_closes: List[float]) -> List[float]:
    """(fast_ema - slow_ema) / slow_ema * 100 on weekly closes."""
    fast = _as_ema(weekly_closes, WEEKLY_EMA_FAST)
    slow = _as_ema(weekly_closes, WEEKLY_EMA_SLOW)
    if not fast or not slow:
        return []
    offset = len(fast) - len(slow)
    out = []
    for i in range(len(slow)):
        f = fast[i + offset]; s = slow[i]
        out.append(((f - s) / s * 100.0) if s > 0 else 0.0)
    return out


def _weekly_macd_hist_series(weekly_closes: List[float]) -> List[float]:
    """MACD histogram series on weekly closes."""
    if len(weekly_closes) < WEEKLY_MACD_SLOW + WEEKLY_MACD_SIGNAL:
        return []
    fast = _as_ema(weekly_closes, WEEKLY_MACD_FAST)
    slow = _as_ema(weekly_closes, WEEKLY_MACD_SLOW)
    if not fast or not slow:
        return []
    offset = len(fast) - len(slow)
    macd_line = [fast[i + offset] - slow[i] for i in range(len(slow))]
    if len(macd_line) < WEEKLY_MACD_SIGNAL:
        return []
    sig = _as_ema(macd_line, WEEKLY_MACD_SIGNAL)
    if not sig:
        return []
    sig_offset = len(macd_line) - len(sig)
    return [macd_line[i + sig_offset] - sig[i] for i in range(len(sig))]


def _compute_realized_vol(closes: List[float], lookback: int) -> float:
    """Annualized σ of log returns over last `lookback` bars (252/yr)."""
    if len(closes) < lookback + 1:
        return 0.0
    logs: List[float] = []
    for i in range(len(closes) - lookback, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            logs.append(math.log(closes[i] / closes[i - 1]))
    if len(logs) < 2:
        return 0.0
    mean = sum(logs) / len(logs)
    var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
    return round(math.sqrt(var) * math.sqrt(252), 6)


def _compute_atr(highs: List[float], lows: List[float], closes: List[float],
                 period: int = 14) -> float:
    """Simple ATR (last value)."""
    if len(highs) < period + 1:
        return 0.0
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


# ═══════════════════════════════════════════════════════════
# QUINTILE BOUNDS (computed from this run's weekly population)
# ═══════════════════════════════════════════════════════════

def _quintile_bounds(values: List[float]) -> Optional[List[float]]:
    vs = sorted([v for v in values if v is not None])
    if len(vs) < 20:
        return None
    def q(p):
        k = (len(vs) - 1) * p
        f = int(k); c = min(f + 1, len(vs) - 1)
        return vs[f] if f == c else vs[f] + (vs[c] - vs[f]) * (k - f)
    return [q(0.2), q(0.4), q(0.6), q(0.8)]


def _bucket_q(value: float, bounds: List[float]) -> str:
    if not bounds or len(bounds) != 4:
        return "unknown"
    if value <= bounds[0]: return "Q1"
    if value <= bounds[1]: return "Q2"
    if value <= bounds[2]: return "Q3"
    if value <= bounds[3]: return "Q4"
    return "Q5"


MIDDLE_Q = {"Q2", "Q3", "Q4"}


# ═══════════════════════════════════════════════════════════
# WEEKLY DIAMOND DETECTION (per-ticker)
# ═══════════════════════════════════════════════════════════

def _detect_weekly_diamond_events(
    ticker: str,
    daily_bars: List[dict],
    weekly_bars: List[dict],
    weekly_ema_diff: List[float],
    weekly_macd_hist: List[float],
    bounds_ema: List[float],
    bounds_macd: List[float],
) -> List[dict]:
    """Return list of events: each event = {
        'weekly_idx': int,
        'weekly_date': str,
        'ema_diff': float, 'macd_hist': float,
        'ema_diff_q': str, 'macd_hist_q': str,
        'direction_from_sign': 'bull'|'bear'|None,
        'next_daily_idx_after': int,   # first daily bar whose date > weekly close
    }

    A weekly bar qualifies if BOTH ema_diff and macd_hist are in middle
    quintiles {Q2,Q3,Q4}. Sign-direction = both-positive → bull,
    both-negative → bear, disagree → None.
    """
    events: List[dict] = []
    if not bounds_ema or not bounds_macd:
        return events

    # Align series to weekly_bars — series are shorter; offset maps index
    ed_offset = len(weekly_bars) - len(weekly_ema_diff)
    mh_offset = len(weekly_bars) - len(weekly_macd_hist)
    if ed_offset < 0 or mh_offset < 0:
        return events

    # Build date → daily_idx map for lookup
    daily_date_to_idx = {b["date"]: i for i, b in enumerate(daily_bars)}
    daily_dates_sorted = sorted(daily_date_to_idx.keys())

    for w_idx in range(len(weekly_bars)):
        ed_j = w_idx - ed_offset
        mh_j = w_idx - mh_offset
        if ed_j < 0 or ed_j >= len(weekly_ema_diff):
            continue
        if mh_j < 0 or mh_j >= len(weekly_macd_hist):
            continue

        ed_val = weekly_ema_diff[ed_j]
        mh_val = weekly_macd_hist[mh_j]
        ed_q = _bucket_q(ed_val, bounds_ema)
        mh_q = _bucket_q(mh_val, bounds_macd)

        if not (ed_q in MIDDLE_Q and mh_q in MIDDLE_Q):
            continue

        # Sign-direction
        if ed_val > 0 and mh_val > 0:
            direction_sign = "bull"
        elif ed_val < 0 and mh_val < 0:
            direction_sign = "bear"
        else:
            direction_sign = None

        # Weekly bar "date" field is the last daily bar of that ISO week
        week_close_date = weekly_bars[w_idx].get("date", "")
        if isinstance(week_close_date, datetime):
            week_close_date = week_close_date.strftime("%Y-%m-%d")
        elif isinstance(week_close_date, date):
            week_close_date = week_close_date.strftime("%Y-%m-%d")
        week_close_date = str(week_close_date)[:10]

        # First daily bar strictly AFTER week close
        next_daily_idx = -1
        for d in daily_dates_sorted:
            if d > week_close_date:
                next_daily_idx = daily_date_to_idx[d]
                break
        if next_daily_idx < 0:
            continue

        events.append({
            "weekly_idx": w_idx,
            "weekly_date": week_close_date,
            "ema_diff": ed_val,
            "macd_hist": mh_val,
            "ema_diff_q": ed_q,
            "macd_hist_q": mh_q,
            "direction_from_sign": direction_sign,
            "next_daily_idx_after": next_daily_idx,
        })
    return events


# ═══════════════════════════════════════════════════════════
# TRAJECTORY SIMULATION (shared by both modes)
# ═══════════════════════════════════════════════════════════

def _simulate_trajectory(direction: str, entry_idx: int, entry_price: float,
                         stop: float, t1: float, t2: float,
                         daily_bars: List[dict]) -> dict:
    """Walk forward up to MAX_HOLD_DAYS from entry_idx. Return:
       - Fixed-day snapshots
       - First-hit times (t1/t2/stop)
       - MAE/MFE pct
       - Canonical single exit (first of stop/T2/T1, else max_hold)
    """
    out: Dict[str, Any] = {
        "mae_pct": 0.0, "mfe_pct": 0.0,
        "snapshots": {}, "first_hit": {"target1": -1, "target2": -1, "stop": -1},
        "exit_reason": None, "exit_price": None, "exit_date": None,
        "hold_days_to_exit": 0,
    }
    max_extent = min(MAX_HOLD_DAYS, len(daily_bars) - entry_idx - 1)
    if max_extent <= 0:
        out["exit_reason"] = "max_hold"
        out["exit_price"] = entry_price
        out["exit_date"] = daily_bars[entry_idx].get("date", "")
        return out

    snapshot_set = set(SNAPSHOT_DAYS)
    mfe_pts = 0.0; mae_pts = 0.0
    locked = False

    for offset in range(1, max_extent + 1):
        bar = daily_bars[entry_idx + offset]
        hi, lo, cl = bar["h"], bar["l"], bar["c"]

        if direction == "bull":
            mfe_pts = max(mfe_pts, hi - entry_price)
            mae_pts = min(mae_pts, lo - entry_price)
        else:
            mfe_pts = max(mfe_pts, entry_price - lo)
            mae_pts = min(mae_pts, entry_price - hi)

        if offset in snapshot_set:
            out["snapshots"][offset] = cl

        def _h(k):
            if out["first_hit"][k] == -1:
                out["first_hit"][k] = offset

        if direction == "bull":
            if stop > 0 and lo <= stop: _h("stop")
            if t1 > 0 and hi >= t1: _h("target1")
            if t2 > 0 and hi >= t2: _h("target2")
        else:
            if stop > 0 and hi >= stop: _h("stop")
            if t1 > 0 and lo <= t1: _h("target1")
            if t2 > 0 and lo <= t2: _h("target2")

        if not locked:
            ex_r = None; ex_p = None
            if direction == "bull":
                if stop > 0 and lo <= stop: ex_p, ex_r = stop, "stop"
                elif t2 > 0 and hi >= t2: ex_p, ex_r = t2, "target2"
                elif t1 > 0 and hi >= t1: ex_p, ex_r = t1, "target1"
            else:
                if stop > 0 and hi >= stop: ex_p, ex_r = stop, "stop"
                elif t2 > 0 and lo <= t2: ex_p, ex_r = t2, "target2"
                elif t1 > 0 and lo <= t1: ex_p, ex_r = t1, "target1"
            if ex_r is not None:
                out["exit_reason"] = ex_r; out["exit_price"] = ex_p
                out["exit_date"] = bar.get("date", "")
                out["hold_days_to_exit"] = offset
                locked = True

    if not locked:
        last = daily_bars[entry_idx + max_extent]
        out["exit_reason"] = "max_hold"
        out["exit_price"] = last["c"]
        out["exit_date"] = last.get("date", "")
        out["hold_days_to_exit"] = max_extent

    out["mae_pct"] = round(mae_pts / entry_price * 100.0, 4) if entry_price > 0 else 0.0
    out["mfe_pct"] = round(mfe_pts / entry_price * 100.0, 4) if entry_price > 0 else 0.0
    return out


def _populate_trajectory_fields(row: WeeklySignal, direction: str,
                                entry_price: float, traj: dict) -> None:
    """Copy trajectory output into the WeeklySignal row fields."""
    snaps = traj["snapshots"]
    for dnum in SNAPSHOT_DAYS:
        if dnum in (3,):
            continue  # dataclass doesn't have price_d3
        price = snaps.get(dnum, 0.0)
        f_name = f"price_d{dnum}"
        if hasattr(row, f_name):
            setattr(row, f_name, round(float(price), 4) if price else 0.0)
            if price and entry_price > 0:
                mv = ((price - entry_price) / entry_price * 100.0) if direction == "bull" \
                     else ((entry_price - price) / entry_price * 100.0)
                setattr(row, f"move_pct_d{dnum}", round(mv, 4))

    row.t_to_target1 = traj["first_hit"]["target1"]
    row.t_to_target2 = traj["first_hit"]["target2"]
    row.t_to_stop = traj["first_hit"]["stop"]

    row.exit_reason = str(traj["exit_reason"])
    row.exit_date = str(traj["exit_date"])
    row.exit_price = round(float(traj["exit_price"]), 4)
    row.hold_days_to_exit = int(traj["hold_days_to_exit"])
    if direction == "bull":
        row.pnl_pts = round(row.exit_price - row.entry_price, 4)
    else:
        row.pnl_pts = round(row.entry_price - row.exit_price, 4)
    row.pnl_pct = round((row.pnl_pts / row.entry_price * 100.0) if row.entry_price > 0 else 0.0, 4)
    row.mae_pct = traj["mae_pct"]
    row.mfe_pct = traj["mfe_pct"]
    row.win = bool(row.pnl_pct > 0)


# ═══════════════════════════════════════════════════════════
# ENTRY-MODE IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════

def _find_recent_swing(daily_bars: List[dict], idx: int,
                       pivot_len: int = 10, lookback: int = 60) -> Tuple[float, float]:
    """Return (swing_high, swing_low) from the last `lookback` bars ending
    at `idx`. Uses simple pivot detection.
    """
    start = max(0, idx - lookback)
    highs = [b["h"] for b in daily_bars[start: idx + 1]]
    lows  = [b["l"] for b in daily_bars[start: idx + 1]]
    if len(highs) < 2 * pivot_len + 1:
        return max(highs), min(lows)
    sh_candidates = []
    sl_candidates = []
    for i in range(pivot_len, len(highs) - pivot_len):
        if all(highs[i] >= highs[i - j] for j in range(1, pivot_len + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, pivot_len + 1)):
            sh_candidates.append(highs[i])
        if all(lows[i] <= lows[i - j] for j in range(1, pivot_len + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, pivot_len + 1)):
            sl_candidates.append(lows[i])
    sh = sh_candidates[-1] if sh_candidates else max(highs)
    sl = sl_candidates[-1] if sl_candidates else min(lows)
    if sh <= sl:
        sh = max(highs); sl = min(lows)
    return sh, sl


def _compute_stop(direction: str, entry_price: float, atr_val: float,
                  swing_high: float, swing_low: float) -> float:
    """Same ATR-capped stop formula as v3.2 / live swing_engine."""
    if direction == "bull":
        swing_dist = entry_price - (swing_low - atr_val * STOP_ATR_MULT)
        return entry_price - min(swing_dist, atr_val * ATR_STOP_CAP)
    else:
        swing_dist = (swing_high + atr_val * STOP_ATR_MULT) - entry_price
        return entry_price + min(swing_dist, atr_val * ATR_STOP_CAP)


def _run_gate_mode(ticker: str, daily_bars: List[dict], spy_bars: List[dict],
                   events: List[dict],
                   regime_map: Dict[str, Dict[str, str]]) -> List[WeeklySignal]:
    """WEEKLY_GATE: for each diamond event, scan the next GATE_WINDOW_DAYS
    for a daily swing signal via analyze_swing_setup. First one enters.
    """
    out: List[WeeklySignal] = []
    spy_by_date = {b["date"]: b for b in spy_bars} if spy_bars else {}

    # Track last used entry_idx to avoid overlapping trades
    cooldown_until = -1

    for ev in events:
        start_idx = ev["next_daily_idx_after"]
        if start_idx <= cooldown_until:
            continue

        found = False
        for scan_off in range(GATE_WINDOW_DAYS):
            scan_idx = start_idx + scan_off
            if scan_idx + 1 >= len(daily_bars):
                break
            visible = daily_bars[: scan_idx + 1]
            visible_spy = None
            if spy_by_date:
                vs = [spy_by_date[d["date"]] for d in visible if d.get("date") in spy_by_date]
                if len(vs) >= 25:
                    visible_spy = vs
            try:
                sig = analyze_swing_setup(
                    ticker=ticker, daily_bars=visible, spy_bars=visible_spy,
                    vix=20.0, earnings_dates=None,
                )
            except Exception:
                sig = None
            if sig is None:
                continue

            # Entry next-bar open
            entry_idx = scan_idx + 1
            entry_bar = daily_bars[entry_idx]
            ep = entry_bar["o"]
            if ep <= 0:
                continue

            atr_val = float(sig.get("atr", 0.0) or 0.0)
            if atr_val <= 0:
                atr_val = ep * 0.015
            stop = _compute_stop(sig["direction"], ep, atr_val,
                                 float(sig.get("swing_high", ep * 1.05)),
                                 float(sig.get("swing_low",  ep * 0.95)))
            t1 = float(sig.get("fib_target_1", 0.0))
            t2 = float(sig.get("fib_target_2", 0.0))

            traj = _simulate_trajectory(
                sig["direction"], entry_idx, ep, stop, t1, t2, daily_bars,
            )

            regime_info = regime_map.get(entry_bar.get("date", ""), {})

            row = WeeklySignal(
                ticker=ticker, mode="WEEKLY_GATE", direction=sig["direction"],
                weekly_diamond_date=str(ev["weekly_date"]),
                weekly_ema_diff=round(ev["ema_diff"], 6),
                weekly_macd_hist=round(ev["macd_hist"], 6),
                weekly_ema_diff_q=ev["ema_diff_q"],
                weekly_macd_hist_q=ev["macd_hist_q"],
                daily_signal_date=str(daily_bars[scan_idx].get("date", "")),
                daily_tier=int(sig.get("tier", 2)),
                daily_fib_level=str(sig.get("fib_level", "")),
                daily_setup_quality=str(sig.get("setup_quality", "STANDARD")),
                daily_confidence=int(sig.get("confidence", 50)),
                daily_rs_vs_spy=float(sig.get("rs_vs_spy", 0.0)),
                daily_rsi=float(sig.get("rsi", 50.0)),
                entry_date=str(entry_bar.get("date", "")),
                entry_price=round(ep, 4),
                atr_val=round(atr_val, 4),
                stop=round(stop, 4),
                target1=round(t1, 4), target2=round(t2, 4),
                regime_trend=regime_info.get("trend", "UNKNOWN"),
                regime_vol=regime_info.get("vol", "UNKNOWN"),
                is_confirmed_ticker=(ticker in SWING_CONFIRMED_TICKERS),
                is_removed_ticker=(ticker in SWING_REMOVED_TICKERS),
            )
            _populate_trajectory_fields(row, sig["direction"], ep, traj)
            out.append(row)

            cooldown_until = entry_idx + traj["hold_days_to_exit"]
            found = True
            break
        # If no daily signal in window, event is discarded (this is the design)
    return out


def _run_entry_mode(ticker: str, daily_bars: List[dict],
                    events: List[dict],
                    regime_map: Dict[str, Dict[str, str]]) -> List[WeeklySignal]:
    """WEEKLY_ENTRY: enter at next daily open after weekly diamond fires.
    Direction from sign agreement. Fib extensions off most recent daily
    swing high/low for targets.
    """
    out: List[WeeklySignal] = []
    cooldown_until = -1

    for ev in events:
        direction = ev["direction_from_sign"]
        if direction is None:
            continue  # indicators disagree, skip
        entry_idx = ev["next_daily_idx_after"]
        if entry_idx <= cooldown_until:
            continue
        if entry_idx >= len(daily_bars):
            continue

        entry_bar = daily_bars[entry_idx]
        ep = entry_bar["o"]
        if ep <= 0:
            continue

        # Compute ATR using the 14 bars leading up to entry
        lookback_start = max(0, entry_idx - 30)
        highs = [b["h"] for b in daily_bars[lookback_start:entry_idx]]
        lows  = [b["l"] for b in daily_bars[lookback_start:entry_idx]]
        closes = [b["c"] for b in daily_bars[lookback_start:entry_idx]]
        atr_val = _compute_atr(highs, lows, closes, 14)
        if atr_val <= 0:
            atr_val = ep * 0.015

        # Recent swing high/low — used only for the stop calculation.
        # For TARGETS we use ATR-projected levels (not fib extensions),
        # because a weekly diamond event can fire anywhere in price action,
        # not just at a retracement. Fib extension math assumes price is
        # WITHIN the swing range; projecting from an already-run move would
        # put "bull" targets below entry. ATR multiples in the trade
        # direction keep R:R coherent: stop ≈ 1.5-2×ATR, T1 = 3×ATR (≈2:1),
        # T2 = 6×ATR (≈4:1).
        # v8.2 (Patch 2): replaced fib extensions with ATR-projected targets.
        sh, sl = _find_recent_swing(daily_bars, entry_idx - 1, pivot_len=10, lookback=60)
        if sh <= sl:
            continue

        if direction == "bull":
            t1 = ep + atr_val * 3.0
            t2 = ep + atr_val * 6.0
        else:
            t1 = ep - atr_val * 3.0
            t2 = ep - atr_val * 6.0

        stop = _compute_stop(direction, ep, atr_val, sh, sl)

        traj = _simulate_trajectory(direction, entry_idx, ep, stop, t1, t2, daily_bars)

        regime_info = regime_map.get(entry_bar.get("date", ""), {})

        row = WeeklySignal(
            ticker=ticker, mode="WEEKLY_ENTRY", direction=direction,
            weekly_diamond_date=str(ev["weekly_date"]),
            weekly_ema_diff=round(ev["ema_diff"], 6),
            weekly_macd_hist=round(ev["macd_hist"], 6),
            weekly_ema_diff_q=ev["ema_diff_q"],
            weekly_macd_hist_q=ev["macd_hist_q"],
            entry_date=str(entry_bar.get("date", "")),
            entry_price=round(ep, 4),
            atr_val=round(atr_val, 4),
            stop=round(stop, 4),
            target1=round(float(t1), 4), target2=round(float(t2), 4),
            regime_trend=regime_info.get("trend", "UNKNOWN"),
            regime_vol=regime_info.get("vol", "UNKNOWN"),
            is_confirmed_ticker=(ticker in SWING_CONFIRMED_TICKERS),
            is_removed_ticker=(ticker in SWING_REMOVED_TICKERS),
        )
        _populate_trajectory_fields(row, direction, ep, traj)
        out.append(row)

        cooldown_until = entry_idx + traj["hold_days_to_exit"]
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
        log.error(f"Regime map download failed: {e}")
        return {}
    if not spy or not qqq or not iwm:
        return {}
    result: Dict[str, Dict[str, str]] = {}
    for d in sorted({b["date"] for b in spy}):
        try:
            _s, trend, vol, _b = compute_regime_for_date(spy, qqq, iwm, vix, d)
            result[d] = {"trend": trend, "vol": vol}
        except Exception:
            result[d] = {"trend": "UNKNOWN", "vol": "UNKNOWN"}
    log.info(f"Regime map built: {len(result)} dates")
    return result


# ═══════════════════════════════════════════════════════════
# CSV I/O
# ═══════════════════════════════════════════════════════════

def _init_csv(path: Path) -> List[str]:
    fields = [f.name for f in _dc_fields(WeeklySignal)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(fields)
    return fields


def _append_csv(rows: List[WeeklySignal], path: Path, fields: List[str]) -> None:
    if not rows:
        return
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([getattr(r, k) for k in fields])


def _load_baseline_v32(path: Path) -> Optional[dict]:
    """Read v3.2 trades.csv and compute headline stats for comparison."""
    if not path.exists():
        log.warning(f"Baseline trades.csv not found at {path}")
        return None
    n = 0; wins = 0; total_pnl = 0.0; gw = 0.0; gl = 0.0
    total_hold = 0
    with open(path) as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                pnl = float(row.get("pnl_pct", 0) or 0)
                win = row.get("win", "False") == "True"
                hold = int(float(row.get("hold_days_to_exit", 0) or 0))
            except ValueError:
                continue
            n += 1
            total_pnl += pnl
            total_hold += hold
            if win:
                wins += 1; gw += pnl
            else:
                gl += abs(pnl)
    if n == 0:
        return None
    wr = wins / n * 100.0
    pf = (gw / gl) if gl > 0 else float("inf")
    avg_pnl = total_pnl / n
    avg_hold = total_hold / n
    # Kelly: W - (1-W)/R where R = avg_win / avg_loss
    avg_win = gw / wins if wins else 0.0
    avg_loss = gl / (n - wins) if (n - wins) > 0 else 0.0
    R = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    kelly = ((wins / n) - (1 - wins / n) / R) if R > 0 else 0.0
    return {
        "mode": "BASELINE_V32", "trades": n, "wins": wins,
        "win_rate_pct": round(wr, 2), "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "avg_pnl_pct": round(avg_pnl, 3), "total_pnl_pct": round(total_pnl, 2),
        "avg_hold_days": round(avg_hold, 2),
        "kelly_fraction": round(kelly, 4),
    }


def _mode_stats(rows: List[WeeklySignal], mode_label: str) -> dict:
    n = len(rows)
    if n == 0:
        return {"mode": mode_label, "trades": 0}
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
        "mode": mode_label, "trades": n, "wins": len(wins),
        "win_rate_pct": round(wr * 100, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "avg_pnl_pct": round(total_pnl / n, 3),
        "total_pnl_pct": round(total_pnl, 2),
        "avg_hold_days": round(sum(r.hold_days_to_exit for r in rows) / n, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(-avg_loss, 3),
        "kelly_fraction": round(kelly, 4),
    }


def _bucket_stats(rows: List[WeeklySignal], key_fn) -> Dict[str, dict]:
    buckets: Dict[str, List[WeeklySignal]] = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        buckets.setdefault(str(k), []).append(r)
    return {k: _mode_stats(rs, k) for k, rs in buckets.items()}


def _write_bucket_csv(stats: Dict[str, dict], path: Path, header: str) -> None:
    if not stats:
        path.write_text(f"{header},trades,win_rate_pct,profit_factor,avg_pnl_pct,avg_hold_days,kelly_fraction\n")
        return
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([header, "trades", "wins", "win_rate_pct", "profit_factor",
                    "avg_pnl_pct", "total_pnl_pct", "avg_hold_days",
                    "avg_win_pct", "avg_loss_pct", "kelly_fraction"])
        for row in sorted(stats.values(), key=lambda d: -d.get("trades", 0)):
            w.writerow([row["mode"], row.get("trades", 0), row.get("wins", 0),
                        row.get("win_rate_pct", 0), row.get("profit_factor", 0),
                        row.get("avg_pnl_pct", 0), row.get("total_pnl_pct", 0),
                        row.get("avg_hold_days", 0), row.get("avg_win_pct", 0),
                        row.get("avg_loss_pct", 0), row.get("kelly_fraction", 0)])


# ═══════════════════════════════════════════════════════════
# COMPARISON REPORT
# ═══════════════════════════════════════════════════════════

def write_comparison(baseline: Optional[dict], gate_rows: List[WeeklySignal],
                     entry_rows: List[WeeklySignal], out_dir: Path,
                     n_tickers: int, start_s: str, end_s: str) -> None:
    gate_s = _mode_stats(gate_rows, "WEEKLY_GATE")
    entry_s = _mode_stats(entry_rows, "WEEKLY_ENTRY")

    lines: List[str] = []
    lines.append("# Weekly Diamond Backtest — 3-Way Comparison")
    lines.append("")
    lines.append(f"- Range: **{start_s} → {end_s}**  |  Tickers: **{n_tickers}**")
    lines.append(f"- Weekly EMA: **{WEEKLY_EMA_FAST}/{WEEKLY_EMA_SLOW}** on weekly bars")
    lines.append(f"- Weekly MACD: **{WEEKLY_MACD_FAST}/{WEEKLY_MACD_SLOW}/{WEEKLY_MACD_SIGNAL}** on weekly bars")
    lines.append(f"- Gate window: **{GATE_WINDOW_DAYS}** trading days  |  Max hold: **{MAX_HOLD_DAYS}** days")
    lines.append("")

    lines.append("## Headline — which mode wins?")
    lines.append("")
    lines.append("```")
    lines.append(f"  {'MODE':<16}  {'N':>5}  {'WR%':>6}  {'PF':>6}  {'AvgPnL':>8}  {'Kelly':>7}  {'Hold':>6}")
    lines.append(f"  {'-'*16}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*6}")
    if baseline:
        pf_str = f"{baseline['profit_factor']}" if baseline['profit_factor'] != "inf" else "inf"
        lines.append(f"  {'BASELINE_V32':<16}  {baseline['trades']:>5}  "
                     f"{baseline['win_rate_pct']:>5.1f}%  {pf_str:>6}  "
                     f"{baseline['avg_pnl_pct']:>+7.2f}%  {baseline['kelly_fraction']:>7.3f}  "
                     f"{baseline['avg_hold_days']:>5.1f}d")
    for s in (gate_s, entry_s):
        if s.get("trades", 0) == 0:
            lines.append(f"  {s['mode']:<16}  {'  0':>5}  {'--':>6}  {'--':>6}  {'--':>8}  {'--':>7}  {'--':>6}")
            continue
        pf_str = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
        lines.append(f"  {s['mode']:<16}  {s['trades']:>5}  "
                     f"{s['win_rate_pct']:>5.1f}%  {pf_str:>6}  "
                     f"{s['avg_pnl_pct']:>+7.2f}%  {s['kelly_fraction']:>7.3f}  "
                     f"{s['avg_hold_days']:>5.1f}d")
    lines.append("```")
    lines.append("")
    lines.append("*Kelly = optimal fraction of capital per trade given WR and win/loss ratio.*")
    lines.append("")

    for label, rows in [("WEEKLY_GATE", gate_rows), ("WEEKLY_ENTRY", entry_rows)]:
        if not rows:
            continue
        lines.append(f"## {label} — breakdowns")
        lines.append("")

        by_dir = _bucket_stats(rows, lambda r: r.direction)
        lines.append("**By direction**")
        lines.append(""); lines.append("```")
        for k in sorted(by_dir.keys()):
            s = by_dir[k]
            pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
            lines.append(f"  {k:<6}  {s['trades']:>4}T  WR {s['win_rate_pct']:>5.1f}%  "
                         f"PF {pf:>5}  PnL {s['avg_pnl_pct']:>+6.2f}%  Kelly {s['kelly_fraction']:+.3f}")
        lines.append("```"); lines.append("")

        by_regime = _bucket_stats(rows, lambda r: r.regime_trend)
        lines.append("**By regime**")
        lines.append(""); lines.append("```")
        for k in sorted(by_regime.keys()):
            s = by_regime[k]
            pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
            lines.append(f"  {k:<16}  {s['trades']:>4}T  WR {s['win_rate_pct']:>5.1f}%  "
                         f"PF {pf:>5}  PnL {s['avg_pnl_pct']:>+6.2f}%")
        lines.append("```"); lines.append("")

        by_exit = _bucket_stats(rows, lambda r: r.exit_reason)
        lines.append("**By exit reason**")
        lines.append(""); lines.append("```")
        for k in sorted(by_exit.keys()):
            s = by_exit[k]
            pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
            lines.append(f"  {k:<10}  {s['trades']:>4}T  WR {s['win_rate_pct']:>5.1f}%  "
                         f"PF {pf:>5}  PnL {s['avg_pnl_pct']:>+6.2f}%")
        lines.append("```"); lines.append("")

        by_ticker = _bucket_stats(rows, lambda r: r.ticker)
        rankable = [(k, s) for k, s in by_ticker.items() if s.get("trades", 0) >= 5]
        def _pfk(x):
            return x[1]["profit_factor"] if x[1]["profit_factor"] != "inf" else 999.0
        lines.append("**Top 10 tickers (min 5 trades)**")
        lines.append(""); lines.append("```")
        for k, s in sorted(rankable, key=_pfk, reverse=True)[:10]:
            pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
            tag = " [C]" if k in SWING_CONFIRMED_TICKERS else ""
            lines.append(f"  {k:<6}{tag}  {s['trades']:>3}T  WR {s['win_rate_pct']:>5.1f}%  "
                         f"PF {pf:>5}  PnL {s['avg_pnl_pct']:>+6.2f}%")
        lines.append("```"); lines.append("")

        # Diamond direction interpretation — for WEEKLY_ENTRY, this shows
        # which sign-direction (bull vs bear) carries the edge
        if label == "WEEKLY_ENTRY":
            bull = [r for r in rows if r.direction == "bull"]
            bear = [r for r in rows if r.direction == "bear"]
            lines.append("**Bull-only subset (drop bears)**")
            if bull:
                s = _mode_stats(bull, "WEEKLY_ENTRY_BULL")
                pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
                lines.append(f"  → {s['trades']}T  WR {s['win_rate_pct']}%  PF {pf}  "
                             f"PnL {s['avg_pnl_pct']:+.2f}%  Kelly {s['kelly_fraction']:+.3f}")
            lines.append("")

    # Same bull-only cut for GATE
    if gate_rows:
        bull_gate = [r for r in gate_rows if r.direction == "bull"]
        if bull_gate:
            s = _mode_stats(bull_gate, "WEEKLY_GATE_BULL")
            pf = f"{s['profit_factor']}" if s['profit_factor'] != "inf" else "inf"
            lines.append("## Bull-only cross-cut (applies to all modes)")
            lines.append(""); lines.append("```")
            lines.append(f"  WEEKLY_GATE  bull-only  → {s['trades']}T  WR {s['win_rate_pct']}%  "
                         f"PF {pf}  PnL {s['avg_pnl_pct']:+.2f}%  Kelly {s['kelly_fraction']:+.3f}")
            if entry_rows:
                bull_entry = [r for r in entry_rows if r.direction == "bull"]
                if bull_entry:
                    s2 = _mode_stats(bull_entry, "WEEKLY_ENTRY_BULL")
                    pf2 = f"{s2['profit_factor']}" if s2['profit_factor'] != "inf" else "inf"
                    lines.append(f"  WEEKLY_ENTRY bull-only → {s2['trades']}T  WR {s2['win_rate_pct']}%  "
                                 f"PF {pf2}  PnL {s2['avg_pnl_pct']:+.2f}%  Kelly {s2['kelly_fraction']:+.3f}")
            lines.append("```"); lines.append("")

    lines.append("## Files")
    lines.append("")
    lines.append("- `trades_weekly_gate.csv` — WEEKLY_GATE trade rows")
    lines.append("- `trades_weekly_entry.csv` — WEEKLY_ENTRY trade rows")
    lines.append("- `weekly_diamond_events.csv` — raw weekly diamond fires (pre-entry)")
    lines.append("- `summary_gate_by_*.csv` / `summary_entry_by_*.csv` — per-bucket breakdowns")
    lines.append("")
    lines.append("---")
    lines.append("*Generated by `bt_weekly_diamond.py`. Daily signals from "
                 "`swing_scanner.analyze_swing_setup`; weekly bars via "
                 "`swing_scanner._aggregate_weekly`; EMA/MACD from active_scanner.*")
    (out_dir / "comparison.md").write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Weekly Diamond Backtest")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--confirmed-only", action="store_true")
    ap.add_argument("--skip-removed", action="store_true")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=900)
    args = ap.parse_args()

    # Ticker selection
    override = os.environ.get("BACKTEST_TICKERS", "").strip()
    if override:
        tickers = [t.strip().upper() for t in override.split(",") if t.strip()]
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
             f"max_hold={MAX_HOLD_DAYS}d, gate_window={GATE_WINDOW_DAYS}d)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: download + compute weekly indicators for all tickers ──
    log.info("Phase 1: per-ticker weekly indicator computation...")
    ticker_data: Dict[str, dict] = {}
    all_weekly_ed: List[float] = []
    all_weekly_mh: List[float] = []

    try:
        spy_bars = download_daily("SPY", start_str, end_str)
        log.info(f"SPY: {len(spy_bars)} bars")
    except Exception as e:
        log.error(f"SPY download failed: {e}")
        spy_bars = []

    for idx, ticker in enumerate(tickers, 1):
        log.info(f"[{idx}/{len(tickers)}] {ticker}: download...")
        try:
            daily = download_daily(ticker, start_str, end_str)
        except Exception as e:
            log.warning(f"{ticker}: download error: {e}")
            continue
        if not daily or len(daily) < MIN_WARMUP_BARS_DAILY:
            continue
        try:
            weekly = _aggregate_weekly(daily)
        except Exception as e:
            log.warning(f"{ticker}: weekly aggregate failed: {e}")
            continue
        if len(weekly) < MIN_WARMUP_BARS_WEEKLY:
            continue

        w_closes = [w["c"] for w in weekly]
        w_ed = _weekly_ema_diff_series(w_closes)
        w_mh = _weekly_macd_hist_series(w_closes)
        if not w_ed or not w_mh:
            continue

        ticker_data[ticker] = {
            "daily": daily,
            "weekly": weekly,
            "weekly_ed": w_ed,
            "weekly_mh": w_mh,
        }
        all_weekly_ed.extend(w_ed)
        all_weekly_mh.extend(w_mh)

    log.info(f"Phase 1 done: {len(ticker_data)} tickers with valid weekly data")

    # ── Phase 2: compute quintile bounds on aggregate weekly population ──
    bounds_ema = _quintile_bounds(all_weekly_ed)
    bounds_mh  = _quintile_bounds(all_weekly_mh)
    if not bounds_ema or not bounds_mh:
        log.error("Insufficient weekly data to compute quintile bounds")
        sys.exit(1)
    log.info(f"Quintile bounds computed from {len(all_weekly_ed)} weekly observations:")
    log.info(f"  ema_diff: {bounds_ema}")
    log.info(f"  macd_hist: {bounds_mh}")

    # ── Phase 3: detect weekly diamond events per ticker ──
    log.info("Phase 3: detecting weekly diamond events...")
    events_by_ticker: Dict[str, List[dict]] = {}
    total_events = 0
    for ticker, td in ticker_data.items():
        events = _detect_weekly_diamond_events(
            ticker, td["daily"], td["weekly"], td["weekly_ed"], td["weekly_mh"],
            bounds_ema, bounds_mh,
        )
        events_by_ticker[ticker] = events
        total_events += len(events)
    log.info(f"  {total_events} weekly diamond events across {len(events_by_ticker)} tickers")

    # Write raw events CSV
    events_path = OUT_DIR / "weekly_diamond_events.csv"
    with open(events_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "weekly_date", "ema_diff", "macd_hist",
                    "ema_diff_q", "macd_hist_q", "direction_from_sign",
                    "next_daily_idx_after"])
        for ticker, evs in events_by_ticker.items():
            for ev in evs:
                w.writerow([ticker, ev["weekly_date"], ev["ema_diff"], ev["macd_hist"],
                            ev["ema_diff_q"], ev["macd_hist_q"],
                            ev["direction_from_sign"] or "", ev["next_daily_idx_after"]])

    # ── Phase 4: regime map (needed for entry-date regime annotation) ──
    regime_map = build_regime_map(start_str, end_str)

    # ── Phase 5: run both entry modes ──
    log.info("Phase 5: running WEEKLY_GATE mode...")
    gate_path = OUT_DIR / "trades_weekly_gate.csv"
    gate_fields = _init_csv(gate_path)
    all_gate: List[WeeklySignal] = []
    for ticker, td in ticker_data.items():
        try:
            rows = _run_gate_mode(ticker, td["daily"], spy_bars,
                                  events_by_ticker[ticker], regime_map)
        except Exception as e:
            log.warning(f"{ticker} gate mode failed: {e}")
            log.debug(traceback.format_exc())
            continue
        _append_csv(rows, gate_path, gate_fields)
        all_gate.extend(rows)
    log.info(f"  WEEKLY_GATE: {len(all_gate)} trades")

    log.info("Phase 5b: running WEEKLY_ENTRY mode...")
    entry_path = OUT_DIR / "trades_weekly_entry.csv"
    entry_fields = _init_csv(entry_path)
    all_entry: List[WeeklySignal] = []
    for ticker, td in ticker_data.items():
        try:
            rows = _run_entry_mode(ticker, td["daily"],
                                   events_by_ticker[ticker], regime_map)
        except Exception as e:
            log.warning(f"{ticker} entry mode failed: {e}")
            log.debug(traceback.format_exc())
            continue
        _append_csv(rows, entry_path, entry_fields)
        all_entry.extend(rows)
    log.info(f"  WEEKLY_ENTRY: {len(all_entry)} trades")

    # ── Phase 6: summaries ──
    log.info("Phase 6: writing summaries...")
    for label, rows, prefix in [("GATE", all_gate, "gate"),
                                 ("ENTRY", all_entry, "entry")]:
        if not rows:
            continue
        _write_bucket_csv(_bucket_stats(rows, lambda r: r.ticker),
                          OUT_DIR / f"summary_{prefix}_by_ticker.csv", "ticker")
        _write_bucket_csv(_bucket_stats(rows, lambda r: r.direction),
                          OUT_DIR / f"summary_{prefix}_by_direction.csv", "direction")
        _write_bucket_csv(_bucket_stats(rows, lambda r: r.regime_trend),
                          OUT_DIR / f"summary_{prefix}_by_regime.csv", "regime")
        _write_bucket_csv(_bucket_stats(rows, lambda r: r.exit_reason),
                          OUT_DIR / f"summary_{prefix}_by_exit_reason.csv", "exit_reason")

    # ── Phase 7: comparison report ──
    baseline = _load_baseline_v32(BASELINE_TRADES_CSV)
    write_comparison(baseline, all_gate, all_entry, OUT_DIR,
                     len(ticker_data), start_str, end_str)
    log.info(f"comparison.md → {OUT_DIR / 'comparison.md'}")
    log.info("DONE")


if __name__ == "__main__":
    main()
