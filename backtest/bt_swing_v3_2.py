#!/usr/bin/env python3
"""
bt_swing_v3_2.py — Swing Scanner Backtest (Trajectory + Overlays)
═══════════════════════════════════════════════════════════════════════════

Walk-forward backtest of the production swing scanner over the full
SWING_WATCHLIST. Supersedes bt_swing_v3_1.py.

DESIGN PRINCIPLE — NO DUPLICATION, NO DRIFT
  Imports live logic verbatim:
    - swing_scanner.analyze_swing_setup      (Tier 1/2 Fib + candle + trend)
    - swing_scanner.classify_hold_horizon    (hold-class policy)
    - potter_box.detect_boxes                (Potter Box level detection)
    - bt_shared.compute_regime_for_date      (BULL/TRANSITION/BEAR)
    - active_scanner._compute_ema/_compute_macd  (daily EMA + MACD for diamond)

WHAT'S NEW vs v3.1
  1. MAX_HOLD_DAYS = 150 (was 15) — per user: earnings can't be filtered out
     over that horizon, so we test through them.
  2. TRAJECTORY MODEL — each signal is followed up to 150 trading days post-
     entry. Instead of one exit, we record:
        - Price snapshots at D+1/3/5/10/20/30/60/90/150
        - Signed move % at each horizon (direction-adjusted)
        - First-hit time for: target1, target2, stop, max-retrace, Potter
          Box roof/floor/midpoint (returns -1 if never within 150d)
  3. POTTER BOX overlay — state at signal time via live detect_boxes engine,
     plus time-to-hit for roof/floor/midpoint levels during the trade.
  4. BLACK-SCHOLES EM comparison — compares live BS-style expected-move
     prediction (spot × σ × √(t/365)) to realized move at 14/21/30d.
     Uses 30-day realized vol as IV proxy (we don't have historical chains).
  5. DIAMOND presence — computes daily EMA-diff and MACD-hist on each of
     days [−5 … +5] around signal; post-processing pass derives quintile
     bounds from this run's signal population and flags daily diamond.
  6. EARNINGS tracking — optional, opt-in via EARNINGS_ENABLED=true env.
     Uses yfinance for historical earnings dates.

WHAT CANONICAL EXIT REMAINS (for headline P&L)
  First-of {stop, target1, target2, max_hold=150D}. Recorded alongside the
  full trajectory so you can re-run other exit rules by re-analyzing
  trades.csv columns — no re-backtest needed.

USAGE
    python backtest/bt_swing_v3_2.py                       # full watchlist, 900 days
    python backtest/bt_swing_v3_2.py --ticker NVDA
    python backtest/bt_swing_v3_2.py --confirmed-only
    EARNINGS_ENABLED=true python backtest/bt_swing_v3_2.py
    BACKTEST_TICKERS=NVDA,TSLA python backtest/bt_swing_v3_2.py

ENV
    BACKTEST_START/END           YYYY-MM-DD overrides
    BACKTEST_TICKERS             comma-separated override
    BACKTEST_OUT_DIR             default /tmp/backtest_swing_v32
    SWING_BT_MAX_HOLD            max trading-day hold (default 150)
    EARNINGS_ENABLED             "true" to fetch yfinance earnings (default off)
    SCHWAB_REFRESH_TOKEN         preferred data source
    MARKETDATA_TOKEN             fallback

OUTPUTS: /tmp/backtest_swing_v32/
    trades.csv                       one row per signal (wide, ~95 cols)
    summary_by_ticker.csv
    summary_by_fib_level.csv
    summary_by_setup_quality.csv
    summary_by_tier_direction.csv
    summary_by_regime.csv
    summary_by_confidence_bucket.csv
    summary_by_exit_reason.csv
    summary_by_pb_state.csv          (in_box vs out_of_box)
    summary_time_to_target.csv       (avg days to T1/T2 by fib/tier)
    summary_em_accuracy.csv          (actual vs predicted at 21d)
    summary_diamond_presence.csv     (diamond WR uplift)
    report.md                        executive summary
    .progress.json                   resume checkpoint
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
log = logging.getLogger("bt_swing_v3_2")

# ── Live signal logic ──
try:
    from swing_scanner import analyze_swing_setup
    log.info(f"Loaded swing_scanner.analyze_swing_setup")
except ImportError as e:
    log.error(f"Cannot import swing_scanner: {e}")
    sys.exit(1)

# ── Ticker universe ──
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

# ── Data + regime helpers ──
try:
    from bt_shared import (
        download_daily, download_vix, compute_regime_for_date,
    )
    log.info("Loaded bt_shared")
except ImportError as e:
    log.error(f"Cannot import bt_shared: {e}")
    sys.exit(1)

# ── Potter Box LIVE engine ──
_detect_boxes_live = None
try:
    from potter_box import detect_boxes as _detect_boxes_live
    log.info("Loaded potter_box.detect_boxes")
except ImportError as e:
    log.warning(f"potter_box import failed ({e}); PB overlay disabled")

# ── EMA + MACD helpers from active_scanner (for daily diamond) ──
try:
    from active_scanner import _compute_ema as _as_compute_ema
    from active_scanner import _compute_macd as _as_compute_macd  # returns dict
    log.info("Loaded active_scanner._compute_ema / _compute_macd for daily diamond")
except ImportError as e:
    log.warning(f"active_scanner import failed ({e}); daily diamond disabled")
    _as_compute_ema = None
    _as_compute_macd = None


# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════

MAX_HOLD_DAYS     = int(os.environ.get("SWING_BT_MAX_HOLD", "150"))
STOP_ATR_MULT     = 0.2
ATR_STOP_CAP      = 2.0
COOLDOWN_BARS     = 3
MIN_WARMUP_BARS   = 220

# Fixed-day trajectory snapshots (trading days after entry)
SNAPSHOT_DAYS = [1, 3, 5, 10, 20, 30, 60, 90, 150]

# Black-Scholes EM horizons (trading days ≈ calendar days here; we use
# trading-day offsets and convert to calendar days via *1.4 when computing
# √T to keep EM comparable to the live bot which uses calendar DTE).
EM_HORIZONS_TRADING_DAYS = [14, 21, 30]

# Diamond window around signal day (trading days; 0 = signal day)
DIAMOND_WINDOW = list(range(-5, 6))  # [-5..+5]

# Daily indicator params (match scanner's intraday timeframe conventions
# adapted to daily bars)
DIAMOND_EMA_FAST = 5    # same as SWING_DAILY_EMA_FAST=8? No — scanner uses 8/21 daily;
DIAMOND_EMA_SLOW = 12   # for diamond we mirror active_scanner (5/12) to keep it
                        # as a second, faster indicator independent of the trend
                        # already captured in daily_bull. See diamond_detector.py.
REALIZED_VOL_LOOKBACK_SHORT = 30
REALIZED_VOL_LOOKBACK_LONG  = 60

# Risk-free rate for BS (matches swing_engine default)
RISK_FREE_RATE = 0.05

OUT_DIR  = Path(os.environ.get("BACKTEST_OUT_DIR", "/tmp/backtest_swing_v32"))
DATA_DIR = Path(os.environ.get("BACKTEST_DATA_DIR", str(BACKTEST_DIR / "data")))
EARNINGS_ENABLED = os.environ.get("EARNINGS_ENABLED", "false").strip().lower() == "true"

DEFAULT_TICKERS = sorted(SWING_WATCHLIST)


# ═══════════════════════════════════════════════════════════
# SIGNAL DATACLASS — wide trajectory row
# ═══════════════════════════════════════════════════════════

@dataclass
class SwingSignal:
    # ── Identity ──
    ticker: str = ""
    direction: str = ""
    tier: int = 0
    setup_quality: str = ""
    signal_date: str = ""
    entry_date: str = ""

    # ── Signal context ──
    fib_level: str = ""
    fib_price: float = 0.0
    swing_high: float = 0.0
    swing_low: float = 0.0
    fib_range: float = 0.0
    fib_target_1: float = 0.0
    fib_target_2: float = 0.0
    confidence: int = 0
    rs_vs_spy: float = 0.0
    rsi: float = 0.0
    primary_trend: str = ""
    weekly_bull: bool = False
    weekly_bear: bool = False
    weekly_converging: bool = False
    daily_bull: bool = False
    daily_bear: bool = False
    htf_confirmed: bool = False
    htf_converging: bool = False
    vol_contracting: bool = False
    vol_expanding: bool = False
    atr_val: float = 0.0
    touch_count: int = 0
    hold_class: str = ""
    default_hold_days: int = 0
    max_hold_days_horizon: int = 0
    runner_eligible: bool = False
    income_eligible: bool = False
    warnings: str = ""

    # ── Regime ──
    regime_trend: str = ""
    regime_vol: str = ""

    # ── Entry ──
    entry_price: float = 0.0
    stop_atr_capped: float = 0.0

    # ── Fixed-day price snapshots ──
    price_d1: float = 0.0
    price_d3: float = 0.0
    price_d5: float = 0.0
    price_d10: float = 0.0
    price_d20: float = 0.0
    price_d30: float = 0.0
    price_d60: float = 0.0
    price_d90: float = 0.0
    price_d150: float = 0.0
    # Direction-adjusted signed move % (positive = favorable, negative = adverse)
    move_pct_d1: float = 0.0
    move_pct_d3: float = 0.0
    move_pct_d5: float = 0.0
    move_pct_d10: float = 0.0
    move_pct_d20: float = 0.0
    move_pct_d30: float = 0.0
    move_pct_d60: float = 0.0
    move_pct_d90: float = 0.0
    move_pct_d150: float = 0.0

    # ── Time-to-event (trading days from entry; -1 if never within 150) ──
    t_to_target1: int = -1
    t_to_target2: int = -1
    t_to_stop: int = -1
    t_to_max_retrace: int = -1  # swing_low for bull, swing_high for bear
    t_to_pb_roof: int = -1
    t_to_pb_floor: int = -1
    t_to_pb_midpoint: int = -1

    # ── Potter Box at signal ──
    pb_state: str = "no_box"         # in_box / above_roof / below_floor / post_box / no_box
    pb_roof: float = 0.0
    pb_floor: float = 0.0
    pb_midpoint: float = 0.0
    pb_range_pct: float = 0.0
    pb_dist_roof_pct: float = 0.0
    pb_dist_floor_pct: float = 0.0
    pb_dist_mid_pct: float = 0.0
    pb_wave_label: str = ""

    # ── Black-Scholes EM comparison ──
    realized_vol_30d: float = 0.0    # annualized σ
    realized_vol_60d: float = 0.0
    em_1sd_14d: float = 0.0          # predicted ±$ move at horizon
    em_1sd_21d: float = 0.0
    em_1sd_30d: float = 0.0
    actual_move_14d: float = 0.0     # signed, direction-adjusted ($, favorable=+)
    actual_move_21d: float = 0.0
    actual_move_30d: float = 0.0
    actual_vs_em_ratio_14d: float = 0.0
    actual_vs_em_ratio_21d: float = 0.0
    actual_vs_em_ratio_30d: float = 0.0
    em_outcome_14d: str = ""         # reverse / inside_1sd / 1sd_2sd / beyond_2sd
    em_outcome_21d: str = ""
    em_outcome_30d: str = ""

    # ── Diamond (daily timeframe; raw inputs recorded for post-hoc bucketing) ──
    # Each window is semicolon-joined floats for offsets -5..+5
    ema_diff_pct_d0: float = 0.0
    macd_hist_d0: float = 0.0
    ema_diff_window: str = ""        # 11 values semicolon-joined
    macd_hist_window: str = ""
    # Post-processed (populated after quintile bounds computed from run pop.)
    diamond_d0: bool = False
    diamond_in_window_11d: bool = False
    diamond_offset_first_hit: int = 99   # days from signal to nearest diamond-day
    diamond_days_in_window: int = 0

    # ── Earnings (opt-in) ──
    earnings_enabled: bool = False
    earnings_within_21d_entry: bool = False
    earnings_within_90d_entry: bool = False
    earnings_count_in_hold: int = 0      # how many earnings during 150d post-entry

    # ── Canonical single-exit for P&L summaries ──
    exit_reason: str = ""
    exit_date: str = ""
    exit_price: float = 0.0
    hold_days_to_exit: int = 0
    pnl_pts: float = 0.0
    pnl_pct: float = 0.0
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    win: bool = False

    # ── Watchlist status flags ──
    is_confirmed_ticker: bool = False
    is_removed_ticker: bool = False


# ═══════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ═══════════════════════════════════════════════════════════

def _compute_realized_vol(closes: List[float], lookback: int) -> float:
    """Annualized σ of daily log returns over last `lookback` bars."""
    if len(closes) < lookback + 1:
        return 0.0
    logs: List[float] = []
    for i in range(len(closes) - lookback, len(closes)):
        if closes[i-1] > 0 and closes[i] > 0:
            logs.append(math.log(closes[i] / closes[i-1]))
    if len(logs) < 2:
        return 0.0
    mean = sum(logs) / len(logs)
    var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
    sigma_daily = math.sqrt(var)
    return round(sigma_daily * math.sqrt(252), 6)  # annualized


def _compute_em(spot: float, sigma_annual: float, days_trading: int) -> float:
    """Black-Scholes-style 1σ expected move in $ over `days_trading` trading days.

    Matches swing_engine.calc_swing_expected_move but uses trading days
    (converted via 252/yr instead of 365/yr).
    """
    if spot <= 0 or sigma_annual <= 0 or days_trading <= 0:
        return 0.0
    return round(spot * sigma_annual * math.sqrt(days_trading / 252.0), 4)


def _macd_hist_series(closes: List[float]) -> List[float]:
    """Return the full MACD histogram series aligned to closes[-len(series):]."""
    if _as_compute_ema is None or len(closes) < 35:
        return []
    fast = _as_compute_ema(closes, 12)
    slow = _as_compute_ema(closes, 26)
    if not fast or not slow:
        return []
    # fast is len(closes)-11, slow is len(closes)-25 per _compute_ema convention.
    # Align on the shorter (slow) series.
    offset = len(fast) - len(slow)
    macd_line = [fast[i + offset] - slow[i] for i in range(len(slow))]
    if len(macd_line) < 9:
        return []
    signal = _as_compute_ema(macd_line, 9)
    if not signal:
        return []
    sig_offset = len(macd_line) - len(signal)
    hist = [macd_line[i + sig_offset] - signal[i] for i in range(len(signal))]
    return hist


def _ema_diff_series(closes: List[float], fast_p: int, slow_p: int) -> List[float]:
    """Return (fast_ema - slow_ema) / slow_ema * 100 series aligned to last bars."""
    if _as_compute_ema is None:
        return []
    fast = _as_compute_ema(closes, fast_p)
    slow = _as_compute_ema(closes, slow_p)
    if not fast or not slow:
        return []
    offset = len(fast) - len(slow)
    out = []
    for i in range(len(slow)):
        f = fast[i + offset]
        s = slow[i]
        if s <= 0:
            out.append(0.0)
        else:
            out.append((f - s) / s * 100.0)
    return out


# ═══════════════════════════════════════════════════════════
# POTTER BOX OVERLAY  (adapted from bt_resolution_study_v3_1._get_boxes_for_ticker)
# ═══════════════════════════════════════════════════════════

_pb_cache: Dict[Tuple[str, int], list] = {}


def _get_boxes(daily_bars: List[dict], ticker: str) -> list:
    """Cache detect_boxes once per (ticker, bar-count). Matches v3_1 pattern."""
    key = (ticker, len(daily_bars))
    if key in _pb_cache:
        return _pb_cache[key]
    if _detect_boxes_live is None:
        _pb_cache[key] = []
        return []
    try:
        boxes = _detect_boxes_live(daily_bars, ticker)
    except Exception as e:
        log.debug(f"detect_boxes({ticker}) raised: {e}")
        boxes = []
    _pb_cache[key] = boxes or []
    return _pb_cache[key]


def _pb_state_at(daily_bars: List[dict], idx: int, ticker: str,
                 spot_at_signal: float) -> dict:
    """Potter Box state at daily bar idx (no lookahead — uses boxes built from
    bars[:idx+1] implicitly because detect_boxes's confirmed-break logic
    consumes forward bars. We therefore filter boxes to those whose start_idx
    lies before our idx and ignore the post-idx confirmation state).

    This matches the live bot's get_active_box() semantics at the time of
    signal — you only see boxes that have started by today.
    """
    empty = {"state": "no_box", "floor": 0.0, "roof": 0.0, "midpoint": 0.0,
             "range_pct": 0.0, "wave_label": "", "dist_roof_pct": 0.0,
             "dist_floor_pct": 0.0, "dist_mid_pct": 0.0}
    if idx < 0 or idx >= len(daily_bars):
        return empty
    boxes = _get_boxes(daily_bars, ticker)
    if not boxes:
        return empty

    relevant = [b for b in boxes if b.get("start_idx", -1) <= idx]
    if not relevant:
        return empty
    box = relevant[-1]

    start_idx = box.get("start_idx", -1)
    end_idx = box.get("end_idx", -1)
    floor = float(box.get("floor", 0.0))
    roof  = float(box.get("roof", 0.0))
    mid   = float(box.get("midpoint", (roof + floor) / 2 if roof and floor else 0.0))
    broken = bool(box.get("broken", False))
    confirmed = bool(box.get("break_confirmed", False))
    break_dir = box.get("break_direction")

    if start_idx <= idx <= end_idx:
        state = "in_box"
    elif broken and confirmed:
        state = ("above_roof" if break_dir == "up"
                 else ("below_floor" if break_dir == "down" else "post_box"))
    else:
        bars_since_end = idx - end_idx
        state = "in_box" if bars_since_end <= 5 else "post_box"

    dr = ((roof - spot_at_signal) / spot_at_signal * 100.0) if (roof > 0 and spot_at_signal > 0) else 0.0
    df = ((spot_at_signal - floor) / spot_at_signal * 100.0) if (floor > 0 and spot_at_signal > 0) else 0.0
    dm = ((spot_at_signal - mid) / spot_at_signal * 100.0) if (mid > 0 and spot_at_signal > 0) else 0.0

    return {
        "state": state, "floor": floor, "roof": roof, "midpoint": mid,
        "range_pct": float(box.get("range_pct", 0.0)),
        "wave_label": str(box.get("wave_label", "")),
        "dist_roof_pct": round(dr, 3),
        "dist_floor_pct": round(df, 3),
        "dist_mid_pct": round(dm, 3),
    }


# ═══════════════════════════════════════════════════════════
# EARNINGS (yfinance, opt-in)
# ═══════════════════════════════════════════════════════════

_earnings_cache: Dict[str, List[str]] = {}


def _fetch_earnings_dates(ticker: str) -> List[str]:
    """Return list of YYYY-MM-DD earnings dates via yfinance. Cached on disk.

    Only runs when EARNINGS_ENABLED=true. Returns [] on any failure.
    """
    if not EARNINGS_ENABLED:
        return []
    if ticker in _earnings_cache:
        return _earnings_cache[ticker]

    cache_path = DATA_DIR / f"earnings_{ticker}.json"
    if cache_path.exists():
        try:
            dates = json.loads(cache_path.read_text())
            if isinstance(dates, list):
                _earnings_cache[ticker] = dates
                return dates
        except Exception:
            pass

    try:
        import yfinance as yf
        yf_t = yf.Ticker(ticker)
        ed = yf_t.earnings_dates
        dates = []
        if ed is not None and hasattr(ed, "index"):
            for d in ed.index:
                try:
                    ds = d.strftime("%Y-%m-%d")
                    dates.append(ds)
                except Exception:
                    continue
        dates = sorted(set(dates))
        try:
            cache_path.write_text(json.dumps(dates))
        except Exception:
            pass
        _earnings_cache[ticker] = dates
        return dates
    except Exception as e:
        log.debug(f"earnings fetch failed for {ticker}: {e}")
        _earnings_cache[ticker] = []
        return []


def _earnings_between(dates: List[str], from_date: str, to_date: str) -> int:
    """Count earnings dates in [from_date, to_date] inclusive."""
    return sum(1 for d in dates if from_date <= d <= to_date)


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
        log.warning("Regime map: missing SPY/QQQ/IWM; returning empty")
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
# CORE SIGNAL ANALYSIS — generate + build trajectory row
# ═══════════════════════════════════════════════════════════

def _generate_signal(ticker: str,
                     window_bars: List[dict],
                     spy_window: Optional[List[dict]],
                     earnings_dates: Optional[List[str]]) -> Optional[dict]:
    """Wrap live analyze_swing_setup. Returns signal dict or None."""
    if not window_bars or len(window_bars) < 60:
        return None
    try:
        return analyze_swing_setup(
            ticker=ticker,
            daily_bars=window_bars,
            spy_bars=spy_window,
            vix=20.0,
            earnings_dates=earnings_dates or None,
        )
    except Exception as e:
        log.debug(f"{ticker}: analyze_swing_setup raised {type(e).__name__}: {e}")
        return None


def _build_trajectory(sig: dict,
                      pos: dict,
                      daily_bars: List[dict],
                      entry_idx: int,
                      earnings_dates_t: List[str]) -> Dict[str, Any]:
    """Walk forward up to MAX_HOLD_DAYS bars from entry_idx. Collect:
       - Fixed-day snapshots (price & direction-adjusted move %)
       - First-hit times for target1, target2, stop, max-retrace, PB levels
       - MAE/MFE over the window
       - Canonical single-exit (first of stop/T1/T2/max_hold)
       - Actual moves at 14/21/30d (for EM comparison)
       - Earnings count during hold
    """
    d = pos["direction"]
    ep = pos["entry_price"]
    stop = pos["stop"]
    t1 = pos["target1"]
    t2 = pos["target2"]
    pb_roof = pos["pb_roof"]
    pb_floor = pos["pb_floor"]
    pb_mid = pos["pb_midpoint"]

    # Max-retrace = the swing extreme opposite to direction (full reversal)
    max_retrace_level = (sig.get("swing_low", 0.0) if d == "bull"
                         else sig.get("swing_high", 0.0))

    out: Dict[str, Any] = {
        "mae_pct": 0.0, "mfe_pct": 0.0,
        "snapshots": {},      # {day_offset: close_price}
        "first_hit": {k: -1 for k in
                      ("target1", "target2", "stop", "max_retrace",
                       "pb_roof", "pb_floor", "pb_midpoint")},
        "actual_moves": {},   # {14: signed_$, 21: ..., 30: ...}
        "exit_reason": None, "exit_price": None, "exit_date": None,
        "hold_days_to_exit": 0,
        "earnings_count_in_hold": 0,
    }

    max_extent = min(MAX_HOLD_DAYS, len(daily_bars) - entry_idx - 1)
    if max_extent <= 0:
        # No forward bars — close at entry
        out["exit_reason"] = "max_hold"
        out["exit_price"] = ep
        out["exit_date"] = pos["entry_date"]
        return out

    mfe_pts = 0.0
    mae_pts = 0.0
    snapshot_set = set(SNAPSHOT_DAYS)
    em_horizon_set = set(EM_HORIZONS_TRADING_DAYS)

    canonical_exit_locked = False

    for offset in range(1, max_extent + 1):
        bar = daily_bars[entry_idx + offset]
        hi, lo, cl = bar["h"], bar["l"], bar["c"]

        # MAE/MFE update
        if d == "bull":
            mfe_pts = max(mfe_pts, hi - ep)
            mae_pts = min(mae_pts, lo - ep)  # negative when adverse
        else:
            mfe_pts = max(mfe_pts, ep - lo)
            mae_pts = min(mae_pts, ep - hi)

        # Fixed-day snapshots
        if offset in snapshot_set:
            out["snapshots"][offset] = cl

        # EM horizon actual move (direction-adjusted $)
        if offset in em_horizon_set:
            if d == "bull":
                out["actual_moves"][offset] = round(cl - ep, 4)
            else:
                out["actual_moves"][offset] = round(ep - cl, 4)

        # First-hit tests (record only once each)
        def _hit(k: str):
            if out["first_hit"][k] == -1:
                out["first_hit"][k] = offset

        if d == "bull":
            if stop > 0 and lo <= stop: _hit("stop")
            if t1 > 0 and hi >= t1:     _hit("target1")
            if t2 > 0 and hi >= t2:     _hit("target2")
            if max_retrace_level > 0 and lo <= max_retrace_level: _hit("max_retrace")
            if pb_roof > 0 and hi >= pb_roof: _hit("pb_roof")
            if pb_floor > 0 and lo <= pb_floor: _hit("pb_floor")
            if pb_mid > 0:
                if (lo <= pb_mid <= hi):
                    _hit("pb_midpoint")
        else:  # bear
            if stop > 0 and hi >= stop: _hit("stop")
            if t1 > 0 and lo <= t1:     _hit("target1")
            if t2 > 0 and lo <= t2:     _hit("target2")
            if max_retrace_level > 0 and hi >= max_retrace_level: _hit("max_retrace")
            if pb_roof > 0 and hi >= pb_roof: _hit("pb_roof")
            if pb_floor > 0 and lo <= pb_floor: _hit("pb_floor")
            if pb_mid > 0:
                if (lo <= pb_mid <= hi):
                    _hit("pb_midpoint")

        # Canonical single-exit (stop > T2 > T1 precedence inside one bar)
        if not canonical_exit_locked:
            ex_reason = None
            ex_price = None
            if d == "bull":
                if stop > 0 and lo <= stop:
                    ex_price = stop; ex_reason = "stop"
                elif t2 > 0 and hi >= t2:
                    ex_price = t2; ex_reason = "target2"
                elif t1 > 0 and hi >= t1:
                    ex_price = t1; ex_reason = "target1"
            else:
                if stop > 0 and hi >= stop:
                    ex_price = stop; ex_reason = "stop"
                elif t2 > 0 and lo <= t2:
                    ex_price = t2; ex_reason = "target2"
                elif t1 > 0 and lo <= t1:
                    ex_price = t1; ex_reason = "target1"
            if ex_reason is not None:
                out["exit_reason"] = ex_reason
                out["exit_price"] = ex_price
                out["exit_date"] = bar.get("date", "")
                out["hold_days_to_exit"] = offset
                canonical_exit_locked = True

    # If never triggered, canonical exit = max_hold at last bar
    if not canonical_exit_locked:
        last_idx = entry_idx + max_extent
        last_bar = daily_bars[last_idx]
        out["exit_reason"] = "max_hold"
        out["exit_price"] = last_bar["c"]
        out["exit_date"] = last_bar.get("date", "")
        out["hold_days_to_exit"] = max_extent

    out["mae_pct"] = round(mae_pts / ep * 100.0, 4) if ep > 0 else 0.0
    out["mfe_pct"] = round(mfe_pts / ep * 100.0, 4) if ep > 0 else 0.0

    # Earnings count in hold window (entry_date → exit_date or max_hold)
    if earnings_dates_t:
        from_d = pos["entry_date"]
        to_d = out["exit_date"] or daily_bars[min(entry_idx + max_extent, len(daily_bars)-1)].get("date", from_d)
        if from_d and to_d:
            out["earnings_count_in_hold"] = _earnings_between(earnings_dates_t, from_d, to_d)

    return out


# ═══════════════════════════════════════════════════════════
# PER-TICKER BACKTEST LOOP
# ═══════════════════════════════════════════════════════════

def run_ticker_backtest(ticker: str,
                        daily_bars: List[dict],
                        spy_bars: List[dict],
                        regime_map: Dict[str, Dict[str, str]]) -> List[SwingSignal]:
    """Walk forward, fire signals via live analyze_swing_setup, build full
    trajectory + overlays for each signal.

    Positioning: one active signal per ticker at a time (cooldown matches live).
    """
    out: List[SwingSignal] = []
    if len(daily_bars) < MIN_WARMUP_BARS:
        log.info(f"{ticker}: {len(daily_bars)} bars < warmup {MIN_WARMUP_BARS}; skip")
        return out

    # Pre-compute closes array (indexed by bar) for realized-vol + diamond
    closes_full = [b["c"] for b in daily_bars]

    # Pre-compute EMA-diff and MACD-hist series over ALL closes once per ticker.
    # These are aligned to the END of the bars array — series[-N:] maps to bars[-N:].
    ema_diff_full = _ema_diff_series(closes_full, DIAMOND_EMA_FAST, DIAMOND_EMA_SLOW)
    macd_hist_full = _macd_hist_series(closes_full)
    # Each series is shorter than closes_full; calculate offset to align.
    ed_offset = len(closes_full) - len(ema_diff_full) if ema_diff_full else len(closes_full)
    mh_offset = len(closes_full) - len(macd_hist_full) if macd_hist_full else len(closes_full)

    def _ema_diff_at(idx: int) -> float:
        """EMA diff pct at daily bar idx, or 0 if not computable."""
        if not ema_diff_full:
            return 0.0
        j = idx - ed_offset
        if 0 <= j < len(ema_diff_full):
            return float(ema_diff_full[j])
        return 0.0

    def _macd_hist_at(idx: int) -> float:
        if not macd_hist_full:
            return 0.0
        j = idx - mh_offset
        if 0 <= j < len(macd_hist_full):
            return float(macd_hist_full[j])
        return 0.0

    spy_by_date = {b["date"]: b for b in spy_bars} if spy_bars else {}

    # Earnings (opt-in)
    earnings_dates_t: List[str] = _fetch_earnings_dates(ticker) if EARNINGS_ENABLED else []

    last_signal_bar = -999
    in_position_until = -1   # bar index after which we can accept a new signal

    for i in range(MIN_WARMUP_BARS, len(daily_bars)):
        bar = daily_bars[i]
        bar_date = bar.get("date", "")

        # Don't fire new signals while the previous trajectory is still active
        if i <= in_position_until:
            continue
        if i - last_signal_bar < COOLDOWN_BARS:
            continue

        visible = daily_bars[: i + 1]
        visible_spy = None
        if spy_by_date:
            vs = [spy_by_date[d["date"]] for d in visible if d.get("date") in spy_by_date]
            if len(vs) >= 25:
                visible_spy = vs

        sig = _generate_signal(ticker, visible, visible_spy, earnings_dates_t or None)
        if sig is None:
            continue

        # ── Entry on next bar open ──
        if i + 1 >= len(daily_bars):
            continue
        entry_bar = daily_bars[i + 1]
        ep = entry_bar["o"]
        if ep <= 0:
            continue
        entry_idx = i + 1
        entry_date = entry_bar.get("date", "")

        atr_val = float(sig.get("atr", 0.0) or 0.0)
        if atr_val <= 0:
            atr_val = ep * 0.015

        # ATR-capped stop
        if sig["direction"] == "bull":
            swing_dist = ep - (sig.get("swing_low", ep) - atr_val * STOP_ATR_MULT)
            stop_val = ep - min(swing_dist, atr_val * ATR_STOP_CAP)
        else:
            swing_dist = (sig.get("swing_high", ep) + atr_val * STOP_ATR_MULT) - ep
            stop_val = ep + min(swing_dist, atr_val * ATR_STOP_CAP)

        # ── Potter Box state at signal (index i — one bar before entry) ──
        pb = _pb_state_at(daily_bars, i, ticker, ep)

        # ── Realized vol + BS EM predictions (causal: uses closes[: i+1]) ──
        closes_upto_i = closes_full[: i + 1]
        rv30 = _compute_realized_vol(closes_upto_i, REALIZED_VOL_LOOKBACK_SHORT)
        rv60 = _compute_realized_vol(closes_upto_i, REALIZED_VOL_LOOKBACK_LONG)

        em14 = _compute_em(ep, rv30, 14)
        em21 = _compute_em(ep, rv30, 21)
        em30 = _compute_em(ep, rv30, 30)

        # ── Diamond window inputs ──
        # Raw EMA-diff and MACD-hist across [signal_idx - 5 .. signal_idx + 5].
        # We only fill values that exist in the dataset (forward values that
        # would be look-ahead for THE SIGNAL DECISION but are fine here because
        # we're tracking what happens AROUND the signal for study purposes).
        ed_win: List[float] = []
        mh_win: List[float] = []
        for off in DIAMOND_WINDOW:
            j = i + off
            if 0 <= j < len(daily_bars):
                ed_win.append(round(_ema_diff_at(j), 6))
                mh_win.append(round(_macd_hist_at(j), 6))
            else:
                ed_win.append(0.0); mh_win.append(0.0)

        # Regime
        regime_info = regime_map.get(bar_date, {})
        regime_trend = regime_info.get("trend", "UNKNOWN")
        regime_vol = regime_info.get("vol", "UNKNOWN")

        # ── Build position state for trajectory simulation ──
        pos = {
            "direction": sig["direction"],
            "entry_price": ep,
            "entry_date": entry_date,
            "stop": round(stop_val, 4),
            "target1": float(sig.get("fib_target_1", 0.0)),
            "target2": float(sig.get("fib_target_2", 0.0)),
            "pb_roof": pb["roof"],
            "pb_floor": pb["floor"],
            "pb_midpoint": pb["midpoint"],
        }

        traj = _build_trajectory(sig, pos, daily_bars, entry_idx, earnings_dates_t)

        # Gate: mark position as active up to exit (canonical single-exit)
        in_position_until = entry_idx + traj["hold_days_to_exit"]
        last_signal_bar = i

        # ── Build SwingSignal row ──
        row = SwingSignal(
            ticker=ticker,
            direction=sig["direction"],
            tier=int(sig.get("tier", 2)),
            setup_quality=str(sig.get("setup_quality", "STANDARD")),
            signal_date=str(bar_date),
            entry_date=str(entry_date),

            fib_level=str(sig.get("fib_level", "")),
            fib_price=float(sig.get("fib_price", 0.0)),
            swing_high=float(sig.get("swing_high", 0.0)),
            swing_low=float(sig.get("swing_low", 0.0)),
            fib_range=float(sig.get("fib_range", 0.0)),
            fib_target_1=float(sig.get("fib_target_1", 0.0)),
            fib_target_2=float(sig.get("fib_target_2", 0.0)),
            confidence=int(sig.get("confidence", 50)),
            rs_vs_spy=float(sig.get("rs_vs_spy", 0.0)),
            rsi=float(sig.get("rsi", 50.0)),
            primary_trend=str(sig.get("primary_trend", "neutral")),
            weekly_bull=bool(sig.get("weekly_bull", False)),
            weekly_bear=bool(sig.get("weekly_bear", False)),
            weekly_converging=bool(sig.get("weekly_converging", False)),
            daily_bull=bool(sig.get("daily_bull", False)),
            daily_bear=bool(sig.get("daily_bear", False)),
            htf_confirmed=bool(sig.get("htf_confirmed", False)),
            htf_converging=bool(sig.get("htf_converging", False)),
            vol_contracting=bool(sig.get("vol_contracting", False)),
            vol_expanding=bool(sig.get("vol_expanding", False)),
            atr_val=round(atr_val, 4),
            touch_count=int(sig.get("touch_count", 0)),
            hold_class=str(sig.get("hold_class", "standard")),
            default_hold_days=int(sig.get("default_hold_days", 0)),
            max_hold_days_horizon=int(sig.get("max_hold_days", 0)),
            runner_eligible=bool(sig.get("runner_eligible", False)),
            income_eligible=bool(sig.get("income_eligible", False)),
            warnings="; ".join(sig.get("warnings", []) or []),

            regime_trend=regime_trend,
            regime_vol=regime_vol,

            entry_price=round(ep, 4),
            stop_atr_capped=round(stop_val, 4),

            pb_state=pb["state"], pb_roof=pb["roof"], pb_floor=pb["floor"],
            pb_midpoint=pb["midpoint"], pb_range_pct=pb["range_pct"],
            pb_dist_roof_pct=pb["dist_roof_pct"],
            pb_dist_floor_pct=pb["dist_floor_pct"],
            pb_dist_mid_pct=pb["dist_mid_pct"],
            pb_wave_label=pb["wave_label"],

            realized_vol_30d=rv30,
            realized_vol_60d=rv60,
            em_1sd_14d=em14, em_1sd_21d=em21, em_1sd_30d=em30,

            ema_diff_pct_d0=round(_ema_diff_at(i), 6),
            macd_hist_d0=round(_macd_hist_at(i), 6),
            ema_diff_window=";".join(f"{x:.6f}" for x in ed_win),
            macd_hist_window=";".join(f"{x:.6f}" for x in mh_win),

            earnings_enabled=EARNINGS_ENABLED,
            earnings_count_in_hold=traj["earnings_count_in_hold"],

            is_confirmed_ticker=(ticker in SWING_CONFIRMED_TICKERS),
            is_removed_ticker=(ticker in SWING_REMOVED_TICKERS),
        )

        # Earnings proximity to entry
        if EARNINGS_ENABLED and earnings_dates_t:
            entry_dt = datetime.strptime(entry_date, "%Y-%m-%d").date() if entry_date else None
            if entry_dt:
                row.earnings_within_21d_entry = any(
                    0 <= (datetime.strptime(d, "%Y-%m-%d").date() - entry_dt).days <= 21
                    for d in earnings_dates_t if d >= entry_date
                )
                row.earnings_within_90d_entry = any(
                    0 <= (datetime.strptime(d, "%Y-%m-%d").date() - entry_dt).days <= 90
                    for d in earnings_dates_t if d >= entry_date
                )

        # Fixed-day snapshots into typed fields
        snaps = traj["snapshots"]
        for dnum in SNAPSHOT_DAYS:
            price = snaps.get(dnum, 0.0)
            setattr(row, f"price_d{dnum}", round(float(price), 4) if price else 0.0)
            if price and ep > 0:
                if sig["direction"] == "bull":
                    mv = (price - ep) / ep * 100.0
                else:
                    mv = (ep - price) / ep * 100.0
                setattr(row, f"move_pct_d{dnum}", round(mv, 4))

        # Time-to-event
        row.t_to_target1    = traj["first_hit"]["target1"]
        row.t_to_target2    = traj["first_hit"]["target2"]
        row.t_to_stop       = traj["first_hit"]["stop"]
        row.t_to_max_retrace = traj["first_hit"]["max_retrace"]
        row.t_to_pb_roof    = traj["first_hit"]["pb_roof"]
        row.t_to_pb_floor   = traj["first_hit"]["pb_floor"]
        row.t_to_pb_midpoint = traj["first_hit"]["pb_midpoint"]

        # EM comparison
        for h in EM_HORIZONS_TRADING_DAYS:
            am = traj["actual_moves"].get(h, 0.0)
            em = {14: em14, 21: em21, 30: em30}[h]
            setattr(row, f"actual_move_{h}d", round(am, 4))
            ratio = (am / em) if em > 0 else 0.0
            setattr(row, f"actual_vs_em_ratio_{h}d", round(ratio, 4))
            # Classify
            if em <= 0:
                cls = ""
            elif ratio < 0:
                cls = "reverse"
            elif abs(ratio) <= 1.0:
                cls = "inside_1sd"
            elif abs(ratio) <= 2.0:
                cls = "1sd_2sd"
            else:
                cls = "beyond_2sd"
            setattr(row, f"em_outcome_{h}d", cls)

        # Canonical exit + P&L
        row.exit_reason = str(traj["exit_reason"])
        row.exit_date = str(traj["exit_date"])
        row.exit_price = round(float(traj["exit_price"]), 4)
        row.hold_days_to_exit = int(traj["hold_days_to_exit"])
        if sig["direction"] == "bull":
            row.pnl_pts = round(row.exit_price - row.entry_price, 4)
        else:
            row.pnl_pts = round(row.entry_price - row.exit_price, 4)
        row.pnl_pct = round((row.pnl_pts / row.entry_price * 100.0) if row.entry_price > 0 else 0.0, 4)
        row.mae_pct = traj["mae_pct"]
        row.mfe_pct = traj["mfe_pct"]
        row.win = bool(row.pnl_pct > 0)

        out.append(row)

    return out


# ═══════════════════════════════════════════════════════════
# POST-PROCESSING: Daily diamond (quintile bounds from this run)
# ═══════════════════════════════════════════════════════════

def _quintile_bounds(values: List[float]) -> Optional[List[float]]:
    """Return 4 cut-points (q20, q40, q60, q80) or None if too few values."""
    vs = sorted([v for v in values if v is not None])
    if len(vs) < 10:
        return None
    def q(p):
        k = (len(vs) - 1) * p
        f = int(k); c = min(f + 1, len(vs) - 1)
        if f == c:
            return vs[f]
        return vs[f] + (vs[c] - vs[f]) * (k - f)
    return [q(0.2), q(0.4), q(0.6), q(0.8)]


def _bucket_q(value: float, bounds: List[float]) -> str:
    if not bounds or len(bounds) != 4:
        return "unknown"
    if value <= bounds[0]: return "Q1"
    if value <= bounds[1]: return "Q2"
    if value <= bounds[2]: return "Q3"
    if value <= bounds[3]: return "Q4"
    return "Q5"


def annotate_diamond(signals: List[SwingSignal]) -> None:
    """Compute per-combo (tier, direction) quintile bounds on the full run
    population's ema_diff_pct_d0 and macd_hist_d0. Then for each signal,
    walk the 11-value window and determine diamond presence.

    Diamond = ema_diff_q ∈ {Q2,Q3,Q4} AND macd_hist_q ∈ {Q2,Q3,Q4}
    (matches diamond_detector.compute_diamond_live exactly).
    """
    if not signals:
        return

    # Per-combo bounds
    by_combo: Dict[str, Dict[str, List[float]]] = {}
    for s in signals:
        key = f"T{s.tier}_{s.direction}"
        d = by_combo.setdefault(key, {"ema_diff": [], "macd_hist": []})
        d["ema_diff"].append(s.ema_diff_pct_d0)
        d["macd_hist"].append(s.macd_hist_d0)

    bounds: Dict[str, Dict[str, Optional[List[float]]]] = {}
    for key, data in by_combo.items():
        bounds[key] = {
            "ema_diff": _quintile_bounds(data["ema_diff"]),
            "macd_hist": _quintile_bounds(data["macd_hist"]),
        }
        ed_b = bounds[key]["ema_diff"]
        mh_b = bounds[key]["macd_hist"]
        log.info(f"  quintile bounds {key}: "
                 f"ema_diff={ed_b}  macd_hist={mh_b}  (n={len(data['ema_diff'])})")

    MIDDLE = {"Q2", "Q3", "Q4"}

    for s in signals:
        key = f"T{s.tier}_{s.direction}"
        eb = bounds.get(key, {}).get("ema_diff")
        mb = bounds.get(key, {}).get("macd_hist")
        if not eb or not mb:
            continue

        # Diamond at d0
        ed0_q = _bucket_q(s.ema_diff_pct_d0, eb)
        mh0_q = _bucket_q(s.macd_hist_d0, mb)
        s.diamond_d0 = (ed0_q in MIDDLE and mh0_q in MIDDLE)

        # Walk the 11-value window
        try:
            ed_vals = [float(x) for x in s.ema_diff_window.split(";")] if s.ema_diff_window else []
            mh_vals = [float(x) for x in s.macd_hist_window.split(";")] if s.macd_hist_window else []
        except ValueError:
            ed_vals, mh_vals = [], []

        if len(ed_vals) == len(DIAMOND_WINDOW) and len(mh_vals) == len(DIAMOND_WINDOW):
            diamond_offsets: List[int] = []
            for idx, off in enumerate(DIAMOND_WINDOW):
                if (_bucket_q(ed_vals[idx], eb) in MIDDLE
                        and _bucket_q(mh_vals[idx], mb) in MIDDLE):
                    diamond_offsets.append(off)
            s.diamond_days_in_window = len(diamond_offsets)
            s.diamond_in_window_11d = bool(diamond_offsets)
            if diamond_offsets:
                # Closest offset to 0 (signal day)
                s.diamond_offset_first_hit = min(diamond_offsets, key=lambda o: abs(o))
            else:
                s.diamond_offset_first_hit = 99


# ═══════════════════════════════════════════════════════════
# CSV I/O
# ═══════════════════════════════════════════════════════════

def _init_trades_csv(path: Path) -> List[str]:
    fields = [f.name for f in _dc_fields(SwingSignal)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
    return fields


def _append_trades_csv(signals: List[SwingSignal], path: Path,
                       fields: List[str]) -> None:
    if not signals:
        return
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        for s in signals:
            w.writerow([getattr(s, k) for k in fields])


def _load_trades_csv(path: Path) -> List[SwingSignal]:
    if not path.exists():
        return []
    field_names = [f.name for f in _dc_fields(SwingSignal)]
    # Under `from __future__ import annotations`, field.type is a STRING like
    # "float" not the type object. Key off the string name.
    field_types = {f.name: (f.type if isinstance(f.type, str) else f.type.__name__)
                   for f in _dc_fields(SwingSignal)}

    def _cvt(name: str, raw: str):
        ft = field_types.get(name, "str")
        if raw is None or raw == "":
            if ft == "int": return 0
            if ft == "float": return 0.0
            if ft == "bool": return False
            return ""
        if ft == "int":
            try: return int(float(raw))
            except ValueError: return 0
        if ft == "float":
            try: return float(raw)
            except ValueError: return 0.0
        if ft == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes")
        return raw

    rows: List[SwingSignal] = []
    with open(path) as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                kwargs = {k: _cvt(k, row.get(k, "")) for k in field_names}
                rows.append(SwingSignal(**kwargs))
            except Exception:
                continue
    return rows


# ═══════════════════════════════════════════════════════════
# SUMMARIES
# ═══════════════════════════════════════════════════════════

def _group_stats(rows: List[SwingSignal], key_fn) -> Dict[str, dict]:
    buckets: Dict[str, List[SwingSignal]] = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        buckets.setdefault(str(k), []).append(r)

    out: Dict[str, dict] = {}
    for k, rs in buckets.items():
        n = len(rs)
        wins = [r for r in rs if r.win]
        losses = [r for r in rs if not r.win]
        wr = (len(wins) / n * 100.0) if n else 0.0
        gp = sum(r.pnl_pct for r in wins)
        gl = abs(sum(r.pnl_pct for r in losses))
        pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
        avg_t1 = [r.t_to_target1 for r in rs if r.t_to_target1 >= 0]
        avg_t2 = [r.t_to_target2 for r in rs if r.t_to_target2 >= 0]
        avg_stop = [r.t_to_stop for r in rs if r.t_to_stop >= 0]

        out[k] = {
            "bucket": k, "trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(wr, 2),
            "avg_pnl_pct": round(sum(r.pnl_pct for r in rs) / n, 3) if n else 0.0,
            "avg_hold_days": round(sum(r.hold_days_to_exit for r in rs) / n, 2) if n else 0.0,
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "avg_mae_pct": round(sum(r.mae_pct for r in rs) / n, 3) if n else 0.0,
            "avg_mfe_pct": round(sum(r.mfe_pct for r in rs) / n, 3) if n else 0.0,
            "t1_hit_rate_pct": round(len(avg_t1) / n * 100.0, 2) if n else 0.0,
            "avg_days_to_t1": round(sum(avg_t1) / len(avg_t1), 2) if avg_t1 else -1,
            "t2_hit_rate_pct": round(len(avg_t2) / n * 100.0, 2) if n else 0.0,
            "avg_days_to_t2": round(sum(avg_t2) / len(avg_t2), 2) if avg_t2 else -1,
            "stop_hit_rate_pct": round(len(avg_stop) / n * 100.0, 2) if n else 0.0,
        }
    return out


def _write_bucket_csv(stats: Dict[str, dict], path: Path, header: str) -> None:
    if not stats:
        path.write_text(f"{header},trades,wins,losses,win_rate_pct,avg_pnl_pct,"
                        "avg_hold_days,profit_factor,avg_mae_pct,avg_mfe_pct,"
                        "t1_hit_rate_pct,avg_days_to_t1,t2_hit_rate_pct,"
                        "avg_days_to_t2,stop_hit_rate_pct\n")
        return
    keys = list(next(iter(stats.values())).keys())
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([header] + keys[1:])
        for row in sorted(stats.values(), key=lambda d: -d["trades"]):
            w.writerow([row[k] for k in keys])


def _conf_bucket(c: int) -> str:
    if c < 60: return "<60"
    if c < 70: return "60-69"
    if c < 80: return "70-79"
    if c < 90: return "80-89"
    return "90+"


def write_summaries(rows: List[SwingSignal], out_dir: Path) -> None:
    log.info(f"Writing summaries (n={len(rows)})...")

    _write_bucket_csv(_group_stats(rows, lambda r: r.ticker),
                      out_dir / "summary_by_ticker.csv", "ticker")
    _write_bucket_csv(_group_stats(rows, lambda r: r.fib_level or "none"),
                      out_dir / "summary_by_fib_level.csv", "fib_level")
    _write_bucket_csv(_group_stats(rows, lambda r: r.setup_quality),
                      out_dir / "summary_by_setup_quality.csv", "setup_quality")
    _write_bucket_csv(_group_stats(rows, lambda r: f"T{r.tier}_{r.direction}"),
                      out_dir / "summary_by_tier_direction.csv", "tier_direction")
    _write_bucket_csv(_group_stats(rows, lambda r: r.regime_trend),
                      out_dir / "summary_by_regime.csv", "regime")
    _write_bucket_csv(_group_stats(rows, lambda r: _conf_bucket(r.confidence)),
                      out_dir / "summary_by_confidence_bucket.csv", "confidence_bucket")
    _write_bucket_csv(_group_stats(rows, lambda r: r.exit_reason),
                      out_dir / "summary_by_exit_reason.csv", "exit_reason")
    _write_bucket_csv(_group_stats(rows, lambda r: r.pb_state),
                      out_dir / "summary_by_pb_state.csv", "pb_state")
    _write_bucket_csv(_group_stats(rows, lambda r: "diamond" if r.diamond_in_window_11d else "no_diamond"),
                      out_dir / "summary_diamond_presence.csv", "diamond_in_window")

    # EM accuracy summary
    em_stats = _write_em_summary(rows, out_dir / "summary_em_accuracy.csv")
    log.info(f"  EM 21d: {em_stats}")

    log.info("Summaries written")


def _write_em_summary(rows: List[SwingSignal], path: Path) -> dict:
    """Classify signals by EM outcome at 21d horizon and compute WR per bucket."""
    outcomes = ("reverse", "inside_1sd", "1sd_2sd", "beyond_2sd")
    buckets = {o: [] for o in outcomes}
    for r in rows:
        o = r.em_outcome_21d
        if o in buckets:
            buckets[o].append(r)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["em_outcome_21d", "trades", "wins", "losses", "win_rate_pct",
                    "avg_pnl_pct", "avg_actual_move_21d", "median_abs_ratio_21d"])
        total_summary = {}
        for o in outcomes:
            bs = buckets[o]
            n = len(bs)
            if n == 0:
                w.writerow([o, 0, 0, 0, 0.0, 0.0, 0.0, 0.0])
                total_summary[o] = 0
                continue
            wins = [r for r in bs if r.win]
            am = [r.actual_move_21d for r in bs]
            ratios_abs = sorted(abs(r.actual_vs_em_ratio_21d) for r in bs)
            median_abs = ratios_abs[n // 2]
            w.writerow([
                o, n, len(wins), n - len(wins),
                round(len(wins) / n * 100.0, 2),
                round(sum(r.pnl_pct for r in bs) / n, 3),
                round(sum(am) / n, 3),
                round(median_abs, 3),
            ])
            total_summary[o] = n
    return total_summary


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def _fmt(s: dict) -> str:
    pf = s["profit_factor"]
    return (f"{s['trades']:>4}T | WR {s['win_rate_pct']:>5.1f}% | "
            f"PnL {s['avg_pnl_pct']:+6.2f}% | PF {pf} | "
            f"hold {s['avg_hold_days']:.1f}d | "
            f"T1 {s['t1_hit_rate_pct']:.0f}%@{s['avg_days_to_t1']}d | "
            f"T2 {s['t2_hit_rate_pct']:.0f}%@{s['avg_days_to_t2']}d")


def write_report(rows: List[SwingSignal], start_s: str, end_s: str,
                 n_tickers: int, out_path: Path) -> None:
    total = len(rows)
    if total == 0:
        out_path.write_text("# Swing Backtest v3.2 — No trades\n")
        return

    wins = sum(1 for r in rows if r.win)
    wr = wins / total * 100.0
    total_pnl = sum(r.pnl_pct for r in rows)
    gw = sum(r.pnl_pct for r in rows if r.win)
    gl = abs(sum(r.pnl_pct for r in rows if not r.win))
    pf = (gw / gl) if gl > 0 else float("inf")

    lines: List[str] = []
    lines.append("# Swing Scanner Backtest v3.2 — Trajectory + Overlays")
    lines.append("")
    lines.append(f"- Range: **{start_s} → {end_s}**")
    lines.append(f"- Tickers scanned: **{n_tickers}**")
    lines.append(f"- Max hold: **{MAX_HOLD_DAYS}** trading days (earnings NOT filtered)")
    lines.append(f"- Signal logic: imported from `swing_scanner.analyze_swing_setup`")
    lines.append(f"- EARNINGS_ENABLED: **{EARNINGS_ENABLED}**")
    lines.append("")

    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **{total}** signals, **{wr:.1f}%** WR, **{total_pnl:+.1f}%** total P&L, PF **{pf:.2f}**"
                 if pf != float("inf")
                 else f"- **{total}** signals, **{wr:.1f}%** WR, **{total_pnl:+.1f}%** total P&L, PF **inf**")
    lines.append("")

    by_td = _group_stats(rows, lambda r: f"T{r.tier}_{r.direction}")
    lines.append("## By Tier × Direction"); lines.append(""); lines.append("```")
    for k in sorted(by_td.keys()):
        lines.append(f"  {k:<10}  {_fmt(by_td[k])}")
    lines.append("```"); lines.append("")

    by_fib = _group_stats(rows, lambda r: r.fib_level or "none")
    lines.append("## By Fib Level"); lines.append(""); lines.append("```")
    for k in ["38.2", "50.0", "61.8", "78.6"]:
        if k in by_fib:
            lines.append(f"  fib {k:<5}  {_fmt(by_fib[k])}")
    lines.append("```"); lines.append("")

    by_q = _group_stats(rows, lambda r: r.setup_quality)
    lines.append("## By Setup Quality"); lines.append(""); lines.append("```")
    for k in ["FLAGSHIP", "PREMIUM", "STRONG", "SELECTIVE", "TACTICAL", "STANDARD"]:
        if k in by_q:
            lines.append(f"  {k:<10}  {_fmt(by_q[k])}")
    lines.append("```"); lines.append("")

    by_r = _group_stats(rows, lambda r: r.regime_trend)
    lines.append("## By Regime"); lines.append(""); lines.append("```")
    for k in sorted(by_r.keys()):
        lines.append(f"  {k:<14}  {_fmt(by_r[k])}")
    lines.append("```"); lines.append("")

    by_pb = _group_stats(rows, lambda r: r.pb_state)
    lines.append("## By Potter Box State at Signal"); lines.append(""); lines.append("```")
    for k in sorted(by_pb.keys()):
        lines.append(f"  {k:<14}  {_fmt(by_pb[k])}")
    lines.append("```"); lines.append("")

    # Diamond uplift
    with_d = [r for r in rows if r.diamond_in_window_11d]
    without_d = [r for r in rows if not r.diamond_in_window_11d]
    lines.append("## Diamond Presence (±5 daily window)")
    lines.append("")
    lines.append("```")
    for label, group in [("diamond    ", with_d), ("no_diamond ", without_d)]:
        if group:
            w2 = sum(1 for r in group if r.win) / len(group) * 100.0
            pn = sum(r.pnl_pct for r in group) / len(group)
            lines.append(f"  {label} {len(group):>4}T | WR {w2:5.1f}% | avg PnL {pn:+6.2f}%")
    lines.append("```"); lines.append("")

    # EM accuracy at 21d
    em_outcomes = {"reverse": [], "inside_1sd": [], "1sd_2sd": [], "beyond_2sd": []}
    for r in rows:
        if r.em_outcome_21d in em_outcomes:
            em_outcomes[r.em_outcome_21d].append(r)
    lines.append("## Black-Scholes EM Accuracy (21-day horizon)")
    lines.append("")
    lines.append("*Predicted 1σ move from realized vol. `ratio` = actual_move / em_1sd.*")
    lines.append("")
    lines.append("```")
    for k in ["reverse", "inside_1sd", "1sd_2sd", "beyond_2sd"]:
        bs = em_outcomes[k]
        if not bs:
            continue
        w2 = sum(1 for r in bs if r.win) / len(bs) * 100.0
        pn = sum(r.pnl_pct for r in bs) / len(bs)
        avg_ratio = sum(r.actual_vs_em_ratio_21d for r in bs) / len(bs)
        lines.append(f"  {k:<12} {len(bs):>4} sig | WR {w2:5.1f}% | "
                     f"avg PnL {pn:+6.2f}% | avg ratio {avg_ratio:+.2f}")
    lines.append("```"); lines.append("")

    # Time to target distribution
    t1_hit = [r.t_to_target1 for r in rows if r.t_to_target1 >= 0]
    t2_hit = [r.t_to_target2 for r in rows if r.t_to_target2 >= 0]
    stop_hit = [r.t_to_stop for r in rows if r.t_to_stop >= 0]
    lines.append("## Time-to-Event Summary")
    lines.append("")
    lines.append("```")
    if t1_hit:
        lines.append(f"  target1: hit {len(t1_hit):>4}/{total} ({len(t1_hit)/total*100:.1f}%) | "
                     f"avg {sum(t1_hit)/len(t1_hit):.1f}d | median {sorted(t1_hit)[len(t1_hit)//2]}d")
    if t2_hit:
        lines.append(f"  target2: hit {len(t2_hit):>4}/{total} ({len(t2_hit)/total*100:.1f}%) | "
                     f"avg {sum(t2_hit)/len(t2_hit):.1f}d | median {sorted(t2_hit)[len(t2_hit)//2]}d")
    if stop_hit:
        lines.append(f"  stop:    hit {len(stop_hit):>4}/{total} ({len(stop_hit)/total*100:.1f}%) | "
                     f"avg {sum(stop_hit)/len(stop_hit):.1f}d | median {sorted(stop_hit)[len(stop_hit)//2]}d")
    lines.append("```"); lines.append("")

    # Fixed-day average moves
    lines.append("## Average Move % at Fixed Horizons")
    lines.append("")
    lines.append("*Direction-adjusted (positive = favorable).*")
    lines.append("")
    lines.append("```")
    for dnum in SNAPSHOT_DAYS:
        field_name = f"move_pct_d{dnum}"
        vals = [getattr(r, field_name) for r in rows]
        nz = [v for v in vals if v != 0]
        avg = sum(nz) / len(nz) if nz else 0.0
        lines.append(f"  D+{dnum:<3}  n={len(nz):>4}  avg {avg:+6.2f}%")
    lines.append("```"); lines.append("")

    # Top tickers by PF
    by_tkr = _group_stats(rows, lambda r: r.ticker)
    rankable = [(k, s) for k, s in by_tkr.items() if s["trades"] >= 5]
    def _pfk(x):
        return x[1]["profit_factor"] if x[1]["profit_factor"] != "inf" else 999.0
    lines.append("## Top 15 Tickers by Profit Factor (min 5 trades)")
    lines.append(""); lines.append("```")
    for k, s in sorted(rankable, key=_pfk, reverse=True)[:15]:
        tag = " [C]" if k in SWING_CONFIRMED_TICKERS else (" [R]" if k in SWING_REMOVED_TICKERS else "")
        lines.append(f"  {k:<6}{tag}  {_fmt(s)}")
    lines.append("```"); lines.append("")

    lines.append("## Bottom 15 Tickers by Profit Factor (min 5 trades)")
    lines.append(""); lines.append("```")
    for k, s in sorted(rankable, key=_pfk)[:15]:
        tag = " [C]" if k in SWING_CONFIRMED_TICKERS else (" [R]" if k in SWING_REMOVED_TICKERS else "")
        lines.append(f"  {k:<6}{tag}  {_fmt(s)}")
    lines.append("```"); lines.append("")

    lines.append("## Files")
    lines.append("")
    lines.append("- `trades.csv` — one row per signal, ~95 cols (trajectory + all overlays)")
    lines.append("- `summary_by_*.csv` — grouped stats (ticker/fib/quality/regime/...)")
    lines.append("- `summary_em_accuracy.csv` — BS EM 21d outcomes")
    lines.append("- `summary_diamond_presence.csv` — diamond WR comparison")
    lines.append("")
    lines.append("---")
    lines.append("*Generated by `bt_swing_v3_2.py`. Signal math imported live from "
                 "`swing_scanner.analyze_swing_setup`; Potter Box from `potter_box.detect_boxes`.*")

    out_path.write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Swing Scanner Backtest v3.2 — trajectory+overlays")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--confirmed-only", action="store_true")
    ap.add_argument("--skip-removed", action="store_true")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=900)
    ap.add_argument("--no-resume", action="store_true")
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

    # Dates
    end_str = args.to_date or os.environ.get("BACKTEST_END") or date.today().isoformat()
    start_str = args.from_date or os.environ.get("BACKTEST_START") or (
        (date.today() - timedelta(days=args.days)).isoformat()
    )
    log.info(f"Range: {start_str} → {end_str}  ({len(tickers)} tickers, "
             f"max_hold={MAX_HOLD_DAYS}d, earnings={'ON' if EARNINGS_ENABLED else 'OFF'})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Regime + SPY
    regime_map = build_regime_map(start_str, end_str)
    try:
        spy_bars = download_daily("SPY", start_str, end_str)
        log.info(f"SPY: {len(spy_bars)} bars")
    except Exception as e:
        log.error(f"SPY download failed: {e}")
        spy_bars = []

    # Resume/checkpoint
    progress_path = OUT_DIR / ".progress.json"
    trades_path = OUT_DIR / "trades.csv"
    done: set = set()
    trades_written = 0

    use_resume = (not args.no_resume) and progress_path.exists() and trades_path.exists()
    if use_resume:
        try:
            with open(progress_path) as f:
                done = set(json.load(f).get("done", []))
            with open(trades_path) as f:
                next(f, None)
                for _ in f:
                    trades_written += 1
            csv_fields = [fd.name for fd in _dc_fields(SwingSignal)]
            log.info(f"Resumed: {trades_written} signals across {len(done)} done tickers")
        except Exception as e:
            log.warning(f"Resume failed ({e}); starting fresh")
            done = set()
            trades_written = 0
            csv_fields = _init_trades_csv(trades_path)
    else:
        csv_fields = _init_trades_csv(trades_path)
        log.info(f"Initialized trades.csv with {len(csv_fields)} columns")

    # Per-ticker loop
    all_rows: List[SwingSignal] = []
    skipped_nodata = 0
    skipped_error = 0

    for idx, ticker in enumerate(tickers, 1):
        if ticker in done:
            log.info(f"[{idx}/{len(tickers)}] {ticker}: done, skip")
            continue

        log.info(f"[{idx}/{len(tickers)}] {ticker}: download...")
        try:
            daily = download_daily(ticker, start_str, end_str)
        except Exception as e:
            log.warning(f"{ticker}: download error: {e}")
            skipped_error += 1
            done.add(ticker)
            continue

        if not daily or len(daily) < MIN_WARMUP_BARS:
            log.info(f"{ticker}: {len(daily) if daily else 0} bars, skipping")
            skipped_nodata += 1
            done.add(ticker)
            try:
                with open(progress_path, "w") as f:
                    json.dump({"done": sorted(done)}, f)
            except Exception:
                pass
            continue

        # Clear per-ticker PB cache entry so detect_boxes runs fresh
        _pb_cache.pop((ticker, len(daily)), None)

        try:
            ticker_rows = run_ticker_backtest(ticker, daily, spy_bars, regime_map)
        except Exception as e:
            log.error(f"{ticker}: backtest failed: {e}")
            log.debug(traceback.format_exc())
            skipped_error += 1
            done.add(ticker)
            continue

        try:
            _append_trades_csv(ticker_rows, trades_path, csv_fields)
        except Exception as e:
            log.error(f"{ticker}: CSV append failed: {e}")

        trades_written += len(ticker_rows)
        all_rows.extend(ticker_rows)
        done.add(ticker)

        try:
            with open(progress_path, "w") as f:
                json.dump({"done": sorted(done)}, f)
        except Exception:
            pass

        log.info(f"  → {ticker}: {len(ticker_rows)} signals (total: {trades_written:,})")

    log.info("")
    log.info(f"DONE. {trades_written:,} signals, {len(tickers)} tickers "
             f"({skipped_nodata} no-data, {skipped_error} errors)")

    # Reload if resumed (all_rows is partial)
    if use_resume or len(all_rows) != trades_written:
        log.info("Re-reading trades.csv for post-processing...")
        all_rows = _load_trades_csv(trades_path)

    # ── Post-processing: daily diamond annotation ──
    try:
        log.info("Post-processing: computing daily quintile bounds + diamond flags...")
        annotate_diamond(all_rows)
        # Rewrite trades.csv with annotated rows
        csv_fields = _init_trades_csv(trades_path)
        _append_trades_csv(all_rows, trades_path, csv_fields)
        log.info(f"  diamond annotations written to trades.csv")
    except Exception as e:
        log.error(f"Diamond annotation failed: {e}")
        log.debug(traceback.format_exc())

    # Summaries
    try:
        write_summaries(all_rows, OUT_DIR)
    except Exception as e:
        log.error(f"Summary write failed: {e}")
        log.debug(traceback.format_exc())

    # Report
    try:
        write_report(all_rows, start_str, end_str, len(tickers), OUT_DIR / "report.md")
        log.info(f"report.md → {OUT_DIR / 'report.md'}")
    except Exception as e:
        log.error(f"Report failed: {e}")


if __name__ == "__main__":
    main()
