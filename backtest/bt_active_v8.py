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
  - Phase 1.3: exits graded from 5-minute bars (not adjusted daily closes)
  - Phase 1.3: writes backtest_audit.csv so we can prove bars evaluated vs signals fired
  - Phase 1.8: adds location/timing confluence (OR30, VWAP distance, ATR extension, recent move)
  - Phase 1.8: final summary writers are non-fatal so long runs still upload artifacts
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
    edge_discovery.csv
    edge_by_feature.csv
    edge_by_combo.csv
    missed_edge_candidates.csv
    negative_edge_filters.csv
    approved_setups.csv
    setup_classifier_summary.csv
    edge_by_mtf.csv
    mtf_confluence_summary.csv
    edge_by_location.csv
    setup_grade_summary.csv
    vehicle_selection_summary.csv
    grade_x_vehicle_summary.csv
    debit_strike_placement_summary.csv
    debit_expectancy_summary.csv
    debit_expectancy_topline.csv
    report.md
    .progress.json (for resume)
"""

from __future__ import annotations

import bisect
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
    # ── Data/audit flags ──
    exit_source: str = "5min_last_close"
    bad_data_flag: bool = False
    bad_data_reason: str = ""
    # ── Phase 1.6 approved-setup classifier (backtest-only) ──
    setup_archetype: str = "UNCLASSIFIED"
    approved_setup: bool = False
    setup_action: str = "RESEARCH"          # APPROVE / SHADOW / BLOCK / RESEARCH
    block_reason: str = ""
    suggested_hold_window: str = ""
    suggested_vehicle: str = ""
    setup_score: int = 0
    # ── Phase 1.9 graded setup + vehicle selection (backtest-only) ──
    setup_grade: str = "UNRATED"          # A+ / A / B / SHADOW / BLOCK / EXCLUDE
    grade_reason: str = ""
    vehicle_preference: str = "none"      # credit / debit / wait / shadow / block
    vehicle_reason: str = ""
    debit_score: int = 0
    credit_score: int = 0
    debit_proxy_win: bool = False
    credit_proxy_win: bool = False
    recommended_structure: str = "none"
    # ── Phase 2.0 debit-spread short-strike placement proxy (backtest-only) ──
    debit_short_proxy_pnl_pct: float = 0.0     # directional 5D move used for short-strike win proxy
    debit_required_itm_pct: float = 0.0        # minimum ITM % short strike needed to finish winning
    debit_required_itm_bucket: str = "unknown"
    debit_atm_short_win_5d: bool = False
    debit_itm_0_5_short_win_5d: bool = False
    debit_itm_1_0_short_win_5d: bool = False
    debit_itm_1_5_short_win_5d: bool = False
    debit_itm_2_0_short_win_5d: bool = False
    debit_itm_2_5_short_win_5d: bool = False
    # ── Phase 1.7 multi-timeframe confluence (backtest-only) ──
    mtf_15m_trend: str = "unknown"
    mtf_30m_trend: str = "unknown"
    mtf_60m_trend: str = "unknown"
    mtf_15m_macd: str = "unknown"
    mtf_30m_macd: str = "unknown"
    mtf_60m_macd: str = "unknown"
    mtf_15m_rsi: float = 0.0
    mtf_30m_rsi: float = 0.0
    mtf_60m_rsi: float = 0.0
    mtf_15m_adx: float = 0.0
    mtf_30m_adx: float = 0.0
    mtf_60m_adx: float = 0.0
    mtf_alignment_score: int = 0      # matches - opposes across 15/30/60/daily
    mtf_match_count: int = 0
    mtf_oppose_count: int = 0
    mtf_alignment_label: str = "unknown"
    mtf_stack: str = "unknown"
    # ── Phase 1.8 location/timing confluence (backtest-only) ──
    or30_high: float = 0.0
    or30_low: float = 0.0
    or30_mid: float = 0.0
    or30_state: str = "unknown"
    or30_distance_pct: float = 0.0
    vwap_distance_pct: float = 0.0
    vwap_location_state: str = "unknown"
    atr14_5m: float = 0.0
    atr_extension_pct: float = 0.0
    atr_extension_bucket: str = "unknown"
    recent_move_30m_pct: float = 0.0
    recent_move_60m_pct: float = 0.0
    recent_move_bucket: str = "unknown"
    location_timing_combo: str = "unknown"


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



# ═══════════════════════════════════════════════════════════
# PHASE 1.8 — LOCATION / TIMING CONFLUENCE HELPERS
# ═══════════════════════════════════════════════════════════

def _safe_pct(num: float, den: float) -> float:
    try:
        return (float(num) / float(den)) * 100.0 if den else 0.0
    except Exception:
        return 0.0


def _or30_for_day(day_bars: list) -> dict:
    """Compute opening range from the first six 5-minute market bars."""
    if not day_bars:
        return {"high": 0.0, "low": 0.0, "mid": 0.0}
    opening = day_bars[:6]
    highs = [float(b.get("h", 0) or 0) for b in opening if b.get("h") is not None]
    lows = [float(b.get("l", 0) or 0) for b in opening if b.get("l") is not None]
    if not highs or not lows:
        return {"high": 0.0, "low": 0.0, "mid": 0.0}
    hi = max(highs); lo = min(lows)
    return {"high": hi, "low": lo, "mid": (hi + lo) / 2.0 if hi and lo else 0.0}


def _or30_state(spot: float, or30: dict) -> tuple[str, float]:
    hi = float(or30.get("high", 0) or 0)
    lo = float(or30.get("low", 0) or 0)
    if spot <= 0 or hi <= 0 or lo <= 0 or hi <= lo:
        return "no_or30", 0.0
    if spot > hi:
        return "above_or30", round(_safe_pct(spot - hi, spot), 3)
    if spot < lo:
        return "below_or30", round(_safe_pct(lo - spot, spot), 3)
    return "inside_or30", round(_safe_pct(min(hi - spot, spot - lo), spot), 3)


def _atr_from_window(window_bars: list, period: int = 14) -> float:
    if len(window_bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(window_bars)):
        h = float(window_bars[i].get("h", 0) or 0)
        l = float(window_bars[i].get("l", 0) or 0)
        pc = float(window_bars[i-1].get("c", 0) or 0)
        if h <= 0 or l <= 0 or pc <= 0:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else 0.0


def _bucket_signed_move(v: float) -> str:
    try:
        v = float(v)
    except Exception:
        return "unknown"
    av = abs(v)
    if av < 0.25: return "move_lt_025"
    if av < 0.50: return "move_025_050"
    if av < 1.00: return "move_050_100"
    if av < 2.00: return "move_100_200"
    return "move_200_plus"


def _location_snapshot(window_bars: list, day_bars: list, idx: int, sig: dict, or30: dict) -> dict:
    spot = float(sig.get("close", 0) or 0)
    vwap = sig.get("vwap") or 0.0
    ostate, odist = _or30_state(spot, or30)
    vwap_dist = round(_safe_pct(spot - float(vwap or 0), spot), 3) if spot > 0 and vwap else 0.0
    if not vwap:
        vstate = "no_vwap"
    elif vwap_dist >= 0.75:
        vstate = "extended_above_vwap"
    elif vwap_dist > 0:
        vstate = "above_vwap_clean"
    elif vwap_dist <= -0.75:
        vstate = "extended_below_vwap"
    else:
        vstate = "below_vwap_clean"
    atr = _atr_from_window(window_bars, 14)
    closes = [float(b.get("c", 0) or 0) for b in window_bars if b.get("c") is not None]
    ema12 = _compute_ema(closes, EMA_SLOW) if len(closes) >= EMA_SLOW else []
    atr_ext = abs(spot - ema12[-1]) / atr * 100.0 if atr > 0 and ema12 else 0.0
    if atr_ext < 50: eb = "atr_ext_lt_050"
    elif atr_ext < 100: eb = "atr_ext_050_100"
    elif atr_ext < 150: eb = "atr_ext_100_150"
    elif atr_ext < 250: eb = "atr_ext_150_250"
    else: eb = "atr_ext_250_plus"
    def _move_back(nbars: int) -> float:
        if idx - nbars < 0 or spot <= 0:
            return 0.0
        prior = float(day_bars[idx - nbars].get("c", 0) or 0)
        return round(_safe_pct(spot - prior, prior), 3) if prior > 0 else 0.0
    m30 = _move_back(6)
    m60 = _move_back(12)
    move_bucket = _bucket_signed_move(m60 if abs(m60) >= abs(m30) else m30)
    return {
        "or30_high": round(float(or30.get("high", 0) or 0), 4),
        "or30_low": round(float(or30.get("low", 0) or 0), 4),
        "or30_mid": round(float(or30.get("mid", 0) or 0), 4),
        "or30_state": ostate,
        "or30_distance_pct": odist,
        "vwap_distance_pct": vwap_dist,
        "vwap_location_state": vstate,
        "atr14_5m": round(atr, 4),
        "atr_extension_pct": round(atr_ext, 1),
        "atr_extension_bucket": eb,
        "recent_move_30m_pct": m30,
        "recent_move_60m_pct": m60,
        "recent_move_bucket": move_bucket,
        "location_timing_combo": f"or30={ostate}|vwap={vstate}|atr={eb}|move={move_bucket}",
    }

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
# PHASE 1.7 — MULTI-TIMEFRAME OVERLAY HELPERS (BACKTEST-ONLY)
# ═══════════════════════════════════════════════════════════

def _time_ct_to_minutes(time_ct: str) -> Optional[int]:
    try:
        hh, mm = str(time_ct).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _aggregate_intraday_tf(intraday: list, tf_minutes: int) -> list:
    """Aggregate market-hours 5m bars into completed 15/30/60m bars.

    Buckets are anchored to 08:30 CT. A signal only uses higher-timeframe
    bars whose bucket has completed, avoiding lookahead from still-forming
    15/30/60m candles.
    """
    if not intraday:
        return []
    buckets = {}
    order = []
    session_open_min = 8 * 60 + 30
    for b in sorted(intraday, key=lambda x: x.get("ts", 0)):
        mins = _time_ct_to_minutes(b.get("time_ct", ""))
        if mins is None:
            continue
        elapsed = mins - session_open_min
        if elapsed < 0:
            continue
        bucket_start = (elapsed // tf_minutes) * tf_minutes
        key = (b.get("date"), bucket_start)
        if key not in buckets:
            buckets[key] = {
                "date": b.get("date", ""), "bucket_start": bucket_start,
                "tf_minutes": tf_minutes, "ts": int(b.get("ts", 0) or 0),
                "o": float(b.get("o", b.get("c", 0)) or 0),
                "h": float(b.get("h", 0) or 0),
                "l": float(b.get("l", 0) or 0),
                "c": float(b.get("c", 0) or 0),
                "v": int(b.get("v", 0) or 0), "count": 1,
            }
            order.append(key)
        else:
            a = buckets[key]
            a["h"] = max(a["h"], float(b.get("h", 0) or 0))
            lo = float(b.get("l", 0) or 0)
            a["l"] = lo if a["l"] <= 0 else min(a["l"], lo)
            a["c"] = float(b.get("c", 0) or 0)
            a["v"] += int(b.get("v", 0) or 0)
            a["ts"] = int(b.get("ts", 0) or 0)
            a["count"] += 1
    expected = max(1, tf_minutes // 5)
    out = [buckets[k] for k in order if buckets[k].get("count", 0) >= expected]
    out.sort(key=lambda x: x.get("ts", 0))
    return out


def _indicator_state_from_agg(agg_bars: list, end_pos: int, lookback: int = 90) -> dict:
    if end_pos <= 0:
        return {"trend": "unknown", "macd": "unknown", "rsi": 0.0, "adx": 0.0, "ema_dist_pct": 0.0}
    w = agg_bars[max(0, end_pos - lookback): end_pos]
    closes = [float(b.get("c", 0) or 0) for b in w]
    highs = [float(b.get("h", 0) or 0) for b in w]
    lows = [float(b.get("l", 0) or 0) for b in w]
    if len(closes) < 12:
        return {"trend": "unknown", "macd": "unknown", "rsi": 0.0, "adx": 0.0, "ema_dist_pct": 0.0}
    ema_fast = _compute_ema(closes, EMA_FAST)
    ema_slow = _compute_ema(closes, EMA_SLOW)
    trend = "unknown"
    ema_dist_pct = 0.0
    if ema_fast and ema_slow:
        ema_dist_pct = ((ema_fast[-1] - ema_slow[-1]) / ema_slow[-1]) * 100 if ema_slow[-1] else 0.0
        trend = "neutral" if abs(ema_dist_pct) < 0.02 else ("bull" if ema_fast[-1] > ema_slow[-1] else "bear")
    m = _compute_macd(closes) if len(closes) >= 35 else {}
    hist = float(m.get("macd_hist", 0) or 0) if m else 0.0
    macd_state = "neutral" if abs(hist) < 1e-9 else ("bull" if hist > 0 else "bear")
    rsi_v = _compute_rsi(closes, RSI_PERIOD) or 0.0
    adx_v = _compute_adx(highs, lows, closes, length=14) if len(closes) >= 20 else 0.0
    return {"trend": trend, "macd": macd_state, "rsi": round(float(rsi_v or 0.0), 2),
            "adx": round(float(adx_v or 0.0), 2), "ema_dist_pct": round(float(ema_dist_pct or 0.0), 4)}


def _build_mtf_sources(intraday: list) -> dict:
    sources = {}
    for tf in (15, 30, 60):
        bars = _aggregate_intraday_tf(intraday, tf)
        sources[tf] = {"bars": bars, "ts": [int(b.get("ts", 0) or 0) for b in bars]}
    return sources


def _mtf_snapshot_at(mtf_sources: dict, signal_ts: int, bias: str, daily_bull: bool) -> dict:
    """Alignment score = matches - opposes across 15m, 30m, 60m, and daily trend."""
    bias = (bias or "").lower()
    out = {}; matches = 0; opposes = 0; stack_parts = []
    for tf in (15, 30, 60):
        src = mtf_sources.get(tf, {})
        ts_list = src.get("ts", []); bars = src.get("bars", [])
        pos = bisect.bisect_right(ts_list, int(signal_ts or 0))
        st = _indicator_state_from_agg(bars, pos)
        trend = st.get("trend", "unknown"); macd_state = st.get("macd", "unknown")
        out[f"mtf_{tf}m_trend"] = trend
        out[f"mtf_{tf}m_macd"] = macd_state
        out[f"mtf_{tf}m_rsi"] = st.get("rsi", 0.0)
        out[f"mtf_{tf}m_adx"] = st.get("adx", 0.0)
        stack_parts.append(f"{tf}m={trend}")
        if trend == bias:
            matches += 1
        elif trend in ("bull", "bear") and bias in ("bull", "bear"):
            opposes += 1
    daily_state = "bull" if daily_bull else "bear"
    stack_parts.append(f"D={daily_state}")
    if daily_state == bias:
        matches += 1
    elif bias in ("bull", "bear"):
        opposes += 1
    score = matches - opposes
    if matches >= 4:
        label = "full_aligned"
    elif matches >= 3 and score >= 2:
        label = "strong_aligned"
    elif score >= 1:
        label = "partial_aligned"
    elif score == 0:
        label = "mixed"
    else:
        label = "countertrend"
    out.update({"mtf_alignment_score": int(score), "mtf_match_count": int(matches),
                "mtf_oppose_count": int(opposes), "mtf_alignment_label": label,
                "mtf_stack": "|".join(stack_parts)})
    return out


# ═══════════════════════════════════════════════════════════
# MAIN BACKTEST LOOP
# ═══════════════════════════════════════════════════════════

def _build_intraday_exit_index(bars_by_date: dict) -> tuple[list[str], dict[str, float]]:
    """Build same-source exit prices from the 5-minute dataset.

    This prevents the old split-adjustment mismatch where entries came from
    intraday bars but exits came from adjusted daily closes.
    """
    dates = sorted(bars_by_date.keys())
    last_close_by_date: dict[str, float] = {}
    for d in dates:
        bars = sorted(bars_by_date.get(d, []), key=lambda b: b.get("ts", 0))
        if not bars:
            continue
        last_close_by_date[d] = float(bars[-1].get("c", 0) or 0)
    return dates, last_close_by_date


def _exit_dates_from_intraday(trade_date: str, intraday_dates: list[str]) -> dict:
    """Return eod/1d/2d/3d/5d dates using actual intraday trading dates."""
    labels = {"eod": 0, "1d": 1, "2d": 2, "3d": 3, "5d": 5}
    out = {}
    try:
        idx = intraday_dates.index(trade_date)
    except ValueError:
        return {k: "" for k in labels}
    for label, offset in labels.items():
        j = idx + offset
        out[label] = intraday_dates[j] if 0 <= j < len(intraday_dates) else ""
    return out


def _sanity_check_trade_move(entry_price: float, exit_fields: dict) -> tuple[bool, str]:
    """Flag impossible-looking data without deleting the row."""
    if entry_price <= 0:
        return True, "bad_entry_price"
    reasons = []
    for lbl in ("eod", "1d", "2d", "3d", "5d"):
        px = float(exit_fields.get(f"exit_price_{lbl}", 0) or 0)
        if px <= 0:
            continue
        raw_move = abs(px - entry_price) / entry_price * 100.0
        if raw_move > 60.0:
            reasons.append(f"{lbl}_raw_move_{raw_move:.1f}%")
    return (bool(reasons), ";".join(reasons))


def run_ticker(ticker: str, intraday: list, daily: list, regime_cache: dict,
               daily_close_by_date: dict, sorted_dates: list) -> tuple[list[Trade], dict]:
    """Run the active scanner over all 5-min bars for one ticker.

    Phase 1.3 grading fix:
      - Entry and exit prices now both come from the same 5-minute dataset.
      - Daily bars are still used for regime/HTF/overlay context only.
      - Audit counters prove how many bars were evaluated vs final signals.
    """
    trades: list[Trade] = []

    audit = {
        "ticker": ticker,
        "intraday_bars_loaded": len(intraday or []),
        "daily_bars_loaded": len(daily or []),
        "market_dates": 0,
        "bars_evaluated": 0,
        "no_signal_or_below_threshold": 0,
        "raw_signals_before_dedup": 0,
        "deduped_signals_removed": 0,
        "signals_after_dedup": 0,
        "regime_valid_true": 0,
        "regime_valid_false": 0,
        "final_trades": 0,
        "missing_5d_exit": 0,
        "bad_data_flags": 0,
        "first_date": "",
        "last_date": "",
    }

    bars_by_date: dict = {}
    for b in intraday:
        bars_by_date.setdefault(b["date"], []).append(b)
    for d in list(bars_by_date.keys()):
        bars_by_date[d] = sorted(bars_by_date[d], key=lambda b: b.get("ts", 0))

    intraday_dates, intraday_last_close_by_date = _build_intraday_exit_index(bars_by_date)
    audit["market_dates"] = len(intraday_dates)
    if intraday_dates:
        audit["first_date"] = intraday_dates[0]
        audit["last_date"] = intraday_dates[-1]

    daily_date_to_idx = {b["date"]: i for i, b in enumerate(daily)}
    swing_highs, swing_lows = _find_swings(daily) if daily else ([], [])
    mtf_sources = _build_mtf_sources(intraday)

    for trade_date in sorted(bars_by_date.keys()):
        day_bars = bars_by_date[trade_date]
        day_or30 = _or30_for_day(day_bars)
        regime = regime_cache.get(trade_date, "BEAR")
        dc = [daily_close_by_date[d] for d in sorted_dates if d < trade_date]

        # DEDUP BUGFIX (Phase 1.4):
        # `i` is the bar index inside a single trading day and resets to zero
        # every morning.  The prior implementation kept last_sig_bar across
        # days, so a late-day signal (e.g., i=70) caused the next day's first
        # ~70 bars to be falsely counted as duplicates because `i - 70 < 3`.
        # Reset per day so DEDUP_BARS only suppresses repeated same-day alerts.
        last_sig_bar: dict = {}

        for i in range(DEDUP_BARS, len(day_bars)):
            window_start = max(0, i - 79)
            window = day_bars[window_start: i + 1]
            if len(window) < 12:
                continue

            audit["bars_evaluated"] += 1
            sig = detect_signal_backtest(window, dc[-30:], regime, ticker)
            if sig is None:
                audit["no_signal_or_below_threshold"] += 1
                continue

            audit["raw_signals_before_dedup"] += 1

            key = (ticker, sig["bias"])
            if key in last_sig_bar and i - last_sig_bar[key] < DEDUP_BARS:
                audit["deduped_signals_removed"] += 1
                continue
            last_sig_bar[key] = i
            audit["signals_after_dedup"] += 1

            try:
                valid, reason = is_signal_valid_for_regime(
                    ticker, sig["bias"], sig["score"], sig["htf_status"], regime
                )
            except Exception as e:
                valid = True; reason = f"rule_check_failed: {e}"
            if valid:
                audit["regime_valid_true"] += 1
            else:
                audit["regime_valid_false"] += 1

            entry_price = sig["close"]
            entry_time_ct = day_bars[i].get("time_ct", "")

            exits = _exit_dates_from_intraday(trade_date, intraday_dates)
            exit_fields = {}
            for label, exit_date in exits.items():
                exit_p = intraday_last_close_by_date.get(exit_date, 0.0) if exit_date else 0.0
                if exit_date and exit_p > 0:
                    pnl = (exit_p - entry_price) if sig["bias"] == "bull" else (entry_price - exit_p)
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
            if not exit_fields.get("exit_date_5d") or exit_fields.get("exit_price_5d", 0) <= 0:
                audit["missing_5d_exit"] += 1

            bad_flag, bad_reason = _sanity_check_trade_move(entry_price, exit_fields)
            if bad_flag:
                audit["bad_data_flags"] += 1

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

            d_idx = daily_date_to_idx.get(trade_date, -1)
            pb_lookup_idx = max(0, d_idx - 1)
            pb = _potter_state_at(daily, pb_lookup_idx, ticker, entry_price)
            fib = _fib_state_at(daily, pb_lookup_idx)
            sw = _swing_state_at(daily, pb_lookup_idx, swing_highs, swing_lows) if d_idx > 0 else {"above_pct": 999.0, "below_pct": 999.0}

            cs_short = 0.0
            cs_25_b = "n/a"; cs_25_w = False; cs_50_b = "n/a"; cs_50_w = False
            if pb["state"] == "in_box" and pb["floor"] > 0 and pb["roof"] > 0:
                exit_p_5d = exit_fields["exit_price_5d"]
                if exit_p_5d > 0:
                    if sig["bias"] == "bull":
                        cs_short = pb["floor"]; cs_25_long = pb["floor"] - 2.50; cs_50_long = pb["floor"] - 5.00
                    else:
                        cs_short = pb["roof"]; cs_25_long = pb["roof"] + 2.50; cs_50_long = pb["roof"] + 5.00
                    cs_25_b, cs_25_w = _grade_credit(sig["bias"], cs_short, cs_25_long, exit_p_5d)
                    cs_50_b, cs_50_w = _grade_credit(sig["bias"], cs_short, cs_50_long, exit_p_5d)

            conf_bucket = _classify_confluence(sig["bias"], pb["state"])
            mtf = _mtf_snapshot_at(
                mtf_sources,
                int(day_bars[i].get("ts", 0) or 0),
                sig["bias"],
                bool(sig["daily_bull"]) if sig["daily_bull"] is not None else False,
            )
            loc = _location_snapshot(window, day_bars, i, sig, day_or30)

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
                mtf_15m_trend=mtf.get("mtf_15m_trend", "unknown"),
                mtf_30m_trend=mtf.get("mtf_30m_trend", "unknown"),
                mtf_60m_trend=mtf.get("mtf_60m_trend", "unknown"),
                mtf_15m_macd=mtf.get("mtf_15m_macd", "unknown"),
                mtf_30m_macd=mtf.get("mtf_30m_macd", "unknown"),
                mtf_60m_macd=mtf.get("mtf_60m_macd", "unknown"),
                mtf_15m_rsi=mtf.get("mtf_15m_rsi", 0.0),
                mtf_30m_rsi=mtf.get("mtf_30m_rsi", 0.0),
                mtf_60m_rsi=mtf.get("mtf_60m_rsi", 0.0),
                mtf_15m_adx=mtf.get("mtf_15m_adx", 0.0),
                mtf_30m_adx=mtf.get("mtf_30m_adx", 0.0),
                mtf_60m_adx=mtf.get("mtf_60m_adx", 0.0),
                mtf_alignment_score=mtf.get("mtf_alignment_score", 0),
                mtf_match_count=mtf.get("mtf_match_count", 0),
                mtf_oppose_count=mtf.get("mtf_oppose_count", 0),
                mtf_alignment_label=mtf.get("mtf_alignment_label", "unknown"),
                mtf_stack=mtf.get("mtf_stack", "unknown"),
                or30_high=loc.get("or30_high", 0.0),
                or30_low=loc.get("or30_low", 0.0),
                or30_mid=loc.get("or30_mid", 0.0),
                or30_state=loc.get("or30_state", "unknown"),
                or30_distance_pct=loc.get("or30_distance_pct", 0.0),
                vwap_distance_pct=loc.get("vwap_distance_pct", 0.0),
                vwap_location_state=loc.get("vwap_location_state", "unknown"),
                atr14_5m=loc.get("atr14_5m", 0.0),
                atr_extension_pct=loc.get("atr_extension_pct", 0.0),
                atr_extension_bucket=loc.get("atr_extension_bucket", "unknown"),
                recent_move_30m_pct=loc.get("recent_move_30m_pct", 0.0),
                recent_move_60m_pct=loc.get("recent_move_60m_pct", 0.0),
                recent_move_bucket=loc.get("recent_move_bucket", "unknown"),
                location_timing_combo=loc.get("location_timing_combo", "unknown"),
                exit_source="5min_last_close",
                bad_data_flag=bad_flag,
                bad_data_reason=bad_reason,
                **{k: exit_fields[k] for k in exit_fields},
            )
            apply_setup_classifier(t)
            apply_debit_strike_placement_proxy(t)
            apply_grade_vehicle_classifier(t)
            trades.append(t)

    audit["final_trades"] = len(trades)
    return trades, audit


# ═══════════════════════════════════════════════════════════
# PHASE 1.6 — APPROVED SETUP CLASSIFIER (BACKTEST-ONLY)
# ═══════════════════════════════════════════════════════════

def classify_setup_archetype(t: Trade) -> dict:
    """Classify a scanner-qualified candidate into an approved/shadow/block
    setup bucket based on Phase 1.5 edge-discovery findings.

    This is intentionally backtest-only. It does NOT change live Telegram
    posting or production scoring. The goal is to test whether named setup
    archetypes improve the tradeable edge before promoting anything live.
    """
    bias = (t.bias or "").lower()
    pb = (t.pb_state or "no_box").lower()
    cb = (t.cb_side or "n/a").lower()
    htf = (t.htf_status or "UNKNOWN").upper()
    regime = (t.regime or "UNKNOWN").upper()
    wave = (t.pb_wave_label or "none").lower()
    above_vwap = bool(t.above_vwap)

    out = {
        "setup_archetype": "RAW_SCANNER_RESEARCH",
        "approved_setup": False,
        "setup_action": "RESEARCH",
        "block_reason": "",
        "suggested_hold_window": "observe only",
        "suggested_vehicle": "none",
        "setup_score": 0,
    }

    if t.bad_data_flag:
        out.update({
            "setup_archetype": "BAD_DATA_EXCLUDE",
            "setup_action": "BLOCK",
            "block_reason": t.bad_data_reason or "bad_data_flag",
            "suggested_hold_window": "exclude",
            "suggested_vehicle": "none",
            "setup_score": -100,
        })
        return out

    if not t.exit_date_5d or t.exit_price_5d <= 0:
        out.update({
            "setup_archetype": "MISSING_5D_EXIT_EXCLUDE",
            "setup_action": "BLOCK",
            "block_reason": "missing_5d_exit",
            "suggested_hold_window": "exclude",
            "suggested_vehicle": "none",
            "setup_score": -50,
        })
        return out

    if bias == "bull":
        # Strong hidden edge: inside Potter Box, below/near CB line.
        if pb == "in_box" and cb in ("below_cb", "at_cb"):
            score = 82
            if htf == "CONFIRMED": score += 6
            if regime in ("BULL", "TRANSITION"): score += 4
            if above_vwap: score += 3
            if wave in ("weakening", "breakout_probable", "breakout_imminent"): score += 3
            out.update({
                "setup_archetype": "PB_CB_RECLAIM_BULL_APPROVED",
                "approved_setup": True,
                "setup_action": "APPROVE",
                "suggested_hold_window": "2-5 trading days",
                "suggested_vehicle": "bullish spread or ITM call; consider bull put spread if IV rich",
                "setup_score": min(100, score),
            })
            return out

        # Broad robust bullish continuation bucket.
        if pb in ("above_roof", "post_box"):
            score = 76
            if htf == "CONFIRMED": score += 7
            elif htf == "CONVERGING" and regime == "TRANSITION": score += 5
            if above_vwap: score += 4
            if regime == "BULL": score += 5
            if wave in ("breakout_probable", "breakout_imminent"): score += 4
            out.update({
                "setup_archetype": "PB_BREAKOUT_BULL_APPROVED",
                "approved_setup": True,
                "setup_action": "APPROVE",
                "suggested_hold_window": "2-5 trading days",
                "suggested_vehicle": "call debit spread / ITM call; bull put spread if structure support is clear",
                "setup_score": min(100, score),
            })
            return out

        # Negative/chase bucket from Phase 1.5.
        if pb == "in_box" and cb == "above_cb":
            out.update({
                "setup_archetype": "BULL_CHASE_IN_BOX_BLOCK",
                "setup_action": "BLOCK",
                "block_reason": "bull_in_box_above_cb_late_chase",
                "suggested_hold_window": "none",
                "suggested_vehicle": "wait for pullback to CB or clean break above roof",
                "setup_score": 20,
            })
            return out

        if pb == "below_floor":
            out.update({
                "setup_archetype": "BULL_BELOW_FLOOR_SHADOW",
                "setup_action": "SHADOW",
                "block_reason": "bull_against_potter_box_structure",
                "suggested_hold_window": "research only",
                "suggested_vehicle": "none until reclaim",
                "setup_score": 35,
            })
            return out

        out.update({
            "setup_archetype": "BULL_RAW_SCANNER_SHADOW",
            "setup_action": "SHADOW",
            "block_reason": "no_approved_bull_structure",
            "suggested_hold_window": "research only",
            "suggested_vehicle": "none",
            "setup_score": 45,
        })
        return out

    if bias == "bear":
        # Bear side remains mostly shadow/block until a separate bearish model proves edge.
        if pb == "no_box":
            out.update({
                "setup_archetype": "BEAR_NO_BOX_BLOCK",
                "setup_action": "BLOCK",
                "block_reason": "bear_no_potter_box_structure",
                "suggested_hold_window": "none",
                "suggested_vehicle": "none",
                "setup_score": 10,
            })
            return out
        if pb == "above_roof":
            out.update({
                "setup_archetype": "BEAR_ABOVE_ROOF_BLOCK",
                "setup_action": "BLOCK",
                "block_reason": "bear_against_bullish_potter_breakout",
                "suggested_hold_window": "none",
                "suggested_vehicle": "none unless separate rejection model confirms",
                "setup_score": 15,
            })
            return out
        if pb == "in_box" and cb == "below_cb":
            out.update({
                "setup_archetype": "BEAR_IN_BOX_BELOW_CB_BLOCK",
                "setup_action": "BLOCK",
                "block_reason": "bear_in_box_below_cb_historically_weak",
                "suggested_hold_window": "none",
                "suggested_vehicle": "none",
                "setup_score": 15,
            })
            return out
        if pb == "below_floor" and not above_vwap and htf in ("CONFIRMED", "CONVERGING"):
            score = 58
            if regime in ("BEAR", "TRANSITION"): score += 5
            out.update({
                "setup_archetype": "PB_BREAKDOWN_BEAR_RESEARCH",
                "setup_action": "RESEARCH",
                "approved_setup": False,
                "block_reason": "bear_side_requires_separate_validation",
                "suggested_hold_window": "1-3 trading days if separately confirmed",
                "suggested_vehicle": "bear put spread only after rejection/breakdown confirmation",
                "setup_score": min(75, score),
            })
            return out
        out.update({
            "setup_archetype": "BEAR_RAW_SCANNER_SHADOW",
            "setup_action": "SHADOW",
            "block_reason": "bear_side_not_approved_yet",
            "suggested_hold_window": "research only",
            "suggested_vehicle": "none",
            "setup_score": 35,
        })
        return out

    return out


def apply_setup_classifier(t: Trade) -> Trade:
    c = classify_setup_archetype(t)
    for k, v in c.items():
        setattr(t, k, v)
    return t


# ═══════════════════════════════════════════════════════════
# PHASE 1.9 — GRADED SETUP + VEHICLE SELECTION (BACKTEST-ONLY)
# ═══════════════════════════════════════════════════════════

def _grade_rank(label: str) -> int:
    return {"A+": 5, "A": 4, "B": 3, "SHADOW": 2, "BLOCK": 1, "EXCLUDE": 0}.get(label, 2)


def _score_from_grade(label: str) -> int:
    return {"A+": 95, "A": 85, "B": 72, "SHADOW": 45, "BLOCK": 15, "EXCLUDE": -100}.get(label, 45)


def classify_setup_grade_and_vehicle(t: Trade) -> dict:
    """Assign a clean A+/A/B/Shadow/Block grade and a vehicle preference.

    This is intentionally backtest-only. The setup grade answers,
    "Should we care about this signal?" The vehicle preference answers,
    "If we care, how should the 5D edge be expressed?"

    Credit proxy is based on Potter Box boundary-hold outcomes already
    computed in the backtest. It is NOT an options P/L model; it is a
    structure/price-behavior proxy for whether a bull-put / bear-call
    structure would have survived through the 5D window.
    """
    archetype = (getattr(t, "setup_archetype", "") or "").upper()
    action = (getattr(t, "setup_action", "") or "").upper()
    bias = (t.bias or "").lower()
    pb = (t.pb_state or "").lower()
    cb = (t.cb_side or "").lower()
    mtf = (getattr(t, "mtf_alignment_label", "") or "unknown").lower()
    or30 = (getattr(t, "or30_state", "") or "unknown").lower()
    vwap_state = (getattr(t, "vwap_location_state", "") or "unknown").lower()
    recent_bucket = (getattr(t, "recent_move_bucket", "") or "unknown").lower()

    debit_win = bool(getattr(t, "win_5d", False))
    credit_win = bool(getattr(t, "credit_25_win_5d", False) or getattr(t, "credit_50_win_5d", False))

    out = {
        "setup_grade": "SHADOW",
        "grade_reason": "default_research_only",
        "vehicle_preference": "shadow",
        "vehicle_reason": "no approved vehicle edge yet",
        "debit_score": 0,
        "credit_score": 0,
        "debit_proxy_win": debit_win,
        "credit_proxy_win": credit_win,
        "recommended_structure": "none",
    }

    if getattr(t, "bad_data_flag", False):
        out.update({
            "setup_grade": "EXCLUDE",
            "grade_reason": "bad_data_flag",
            "vehicle_preference": "block",
            "vehicle_reason": "bad data excluded from analysis",
            "recommended_structure": "none",
            "debit_score": -100,
            "credit_score": -100,
        })
        return out

    if action == "BLOCK" or "BLOCK" in archetype:
        out.update({
            "setup_grade": "BLOCK",
            "grade_reason": getattr(t, "block_reason", "") or "classifier_block",
            "vehicle_preference": "block",
            "vehicle_reason": "negative-edge setup bucket",
            "recommended_structure": "none",
            "debit_score": -50,
            "credit_score": -50,
        })
        return out

    # ── A+/A/B setup grading ──
    grade = "SHADOW"
    reasons = []

    if archetype == "PB_CB_RECLAIM_BULL_APPROVED":
        if mtf == "full_aligned":
            grade = "A+"
            reasons.append("CB reclaim + full MTF alignment")
        elif mtf in ("strong_aligned", "partial_aligned"):
            grade = "A"
            reasons.append("CB reclaim + supportive MTF alignment")
        elif mtf == "mixed":
            grade = "B"
            reasons.append("CB reclaim + mixed MTF")
        else:
            grade = "B"
            reasons.append("CB reclaim structure, but MTF weaker/countertrend")

    elif archetype == "PB_BREAKOUT_BULL_APPROVED":
        if mtf == "strong_aligned":
            grade = "A"
            reasons.append("breakout bull + strong MTF alignment")
        elif mtf in ("full_aligned", "partial_aligned"):
            grade = "B"
            reasons.append("breakout bull + acceptable MTF")
        else:
            grade = "B"
            reasons.append("breakout bull works broadly, but MTF not ideal")

    elif action == "APPROVE":
        grade = "B"
        reasons.append("approved setup, unrecognized archetype")
    elif action in ("SHADOW", "RESEARCH"):
        grade = "SHADOW"
        reasons.append(getattr(t, "block_reason", "") or "research only")

    # Location/timing modifiers: these should not rescue a bad setup, but they
    # can downgrade a marginal approved setup or explain vehicle choice.
    if grade in ("A+", "A", "B"):
        if or30 == "below_or30" and grade == "B":
            grade = "SHADOW"
            reasons.append("downgraded: below OR30 on marginal setup")
        elif or30 == "below_or30":
            reasons.append("caution: below OR30")
        if vwap_state == "extended_below_vwap":
            if grade == "A+":
                grade = "A"
            elif grade == "A":
                grade = "B"
            else:
                grade = "SHADOW"
            reasons.append("downgraded: extended below VWAP")

    # ── Vehicle scoring ──
    debit_score = 0
    credit_score = 0
    vehicle_pref = "shadow"
    rec = "none"
    vehicle_reason = []

    if grade in ("A+", "A", "B") and bias == "bull":
        # CB reclaim inside the box appears to behave like a 5D hold/credit
        # structure: price often does not need to explode; it needs not to fail
        # below Potter support. Use the credit boundary proxy as the first
        # vehicle clue, while retaining debit if breakout momentum is strong.
        if archetype == "PB_CB_RECLAIM_BULL_APPROVED":
            credit_score += 70
            debit_score += 50
            vehicle_reason.append("inside-box CB reclaim: structure favors support-hold expression")
            if mtf == "full_aligned":
                credit_score += 12; debit_score += 10
            elif mtf in ("strong_aligned", "partial_aligned"):
                credit_score += 8; debit_score += 6
            if credit_win:
                credit_score += 10
            if debit_win:
                debit_score += 6
            if vwap_state in ("extended_above_vwap", "above_vwap_clean"):
                debit_score += 8
                vehicle_reason.append("VWAP momentum supports debit optionality")
            if recent_bucket in ("move_100_200", "move_200_plus"):
                debit_score -= 5
                credit_score += 4
                vehicle_reason.append("large recent move: prefer credit or wait for retest over chasing debit")

        elif archetype == "PB_BREAKOUT_BULL_APPROVED":
            debit_score += 72
            credit_score += 42
            vehicle_reason.append("above-roof breakout: directional continuation favors debit/ITM expression")
            if mtf in ("strong_aligned", "full_aligned"):
                debit_score += 10
            if vwap_state == "extended_above_vwap":
                debit_score += 5
                credit_score += 6
                vehicle_reason.append("extended above VWAP: continuation can work, but use smaller size / defined risk")
            if or30 == "above_or30":
                debit_score += 4
            if credit_win:
                credit_score += 5
            if debit_win:
                debit_score += 8

        else:
            debit_score += 45
            credit_score += 35
            vehicle_reason.append("approved bull but no specific vehicle model")

        # Final vehicle selection.
        if archetype == "PB_CB_RECLAIM_BULL_APPROVED" and credit_score >= debit_score - 5:
            vehicle_pref = "credit"
            rec = "bull put spread / put credit spread at or below Potter support"
        elif debit_score >= credit_score + 8:
            vehicle_pref = "debit"
            rec = "call debit spread / ITM call targeting 2-5D continuation"
        else:
            vehicle_pref = "hybrid"
            rec = "either bull put spread or call debit spread; choose by IV/credit and entry timing"

    elif grade == "SHADOW":
        vehicle_pref = "shadow"
        rec = "log only / research"
        vehicle_reason.append("setup not approved yet")
    else:
        vehicle_pref = "block"
        rec = "none"
        vehicle_reason.append("blocked or excluded")

    out.update({
        "setup_grade": grade,
        "grade_reason": "; ".join([r for r in reasons if r])[:240],
        "vehicle_preference": vehicle_pref,
        "vehicle_reason": "; ".join(vehicle_reason)[:240],
        "debit_score": int(max(-100, min(100, debit_score))),
        "credit_score": int(max(-100, min(100, credit_score))),
        "debit_proxy_win": debit_win,
        "credit_proxy_win": credit_win,
        "recommended_structure": rec,
    })
    return out



# ═══════════════════════════════════════════════════════════
# PHASE 2.0 — DEBIT SPREAD SHORT-STRIKE PLACEMENT PROXY
# ═══════════════════════════════════════════════════════════

def _required_itm_bucket(required: float) -> str:
    """Bucket the minimum ITM short-strike distance required for a 5D proxy win."""
    try:
        r = float(required)
    except Exception:
        return "unknown"
    if r <= 0:
        return "atm_or_better"
    if r <= 0.5:
        return "needs_0_5_itm"
    if r <= 1.0:
        return "needs_1_0_itm"
    if r <= 1.5:
        return "needs_1_5_itm"
    if r <= 2.0:
        return "needs_2_0_itm"
    if r <= 2.5:
        return "needs_2_5_itm"
    return "needs_gt_2_5_itm"


def apply_debit_strike_placement_proxy(t: Trade) -> Trade:
    """Add short-strike placement proxy fields for 5D debit spreads.

    This is NOT a real options P/L model. It answers one question:
    if the debit spread's short strike were ATM or X% ITM at entry, did
    the underlying finish beyond that short strike at the 5D exit?

    Directional 5D PnL already normalizes bull/bear:
      bull: positive if exit > entry
      bear: positive if exit < entry

    A short strike X% ITM wins if directional_pnl_pct >= -X.
    """
    if getattr(t, "bad_data_flag", False) or getattr(t, "exit_price_5d", 0.0) <= 0:
        t.debit_required_itm_bucket = "bad_or_missing_exit"
        return t

    directional_pct = float(getattr(t, "pnl_pct_5d", 0.0) or 0.0)
    required = max(0.0, -directional_pct)

    t.debit_short_proxy_pnl_pct = round(directional_pct, 3)
    t.debit_required_itm_pct = round(required, 3)
    t.debit_required_itm_bucket = _required_itm_bucket(required)

    t.debit_atm_short_win_5d = directional_pct >= 0.0
    t.debit_itm_0_5_short_win_5d = directional_pct >= -0.5
    t.debit_itm_1_0_short_win_5d = directional_pct >= -1.0
    t.debit_itm_1_5_short_win_5d = directional_pct >= -1.5
    t.debit_itm_2_0_short_win_5d = directional_pct >= -2.0
    t.debit_itm_2_5_short_win_5d = directional_pct >= -2.5
    return t

def apply_grade_vehicle_classifier(t: Trade) -> Trade:
    c = classify_setup_grade_and_vehicle(t)
    for k, v in c.items():
        setattr(t, k, v)
    # Keep legacy suggested_vehicle roughly aligned with the new model so older
    # summary files remain useful.
    if getattr(t, "recommended_structure", "none") != "none":
        t.suggested_vehicle = t.recommended_structure
    if getattr(t, "setup_grade", "SHADOW") in ("A+", "A", "B"):
        t.approved_setup = True
        t.setup_action = "APPROVE"
    elif getattr(t, "setup_grade", "SHADOW") == "BLOCK":
        t.approved_setup = False
        t.setup_action = "BLOCK"
    return t


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
    fields = list(Trade.__dataclass_fields__.keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def write_audit_csv(audits, path):
    fields = [
        "ticker", "intraday_bars_loaded", "daily_bars_loaded", "market_dates",
        "bars_evaluated", "no_signal_or_below_threshold",
        "raw_signals_before_dedup", "deduped_signals_removed",
        "signals_after_dedup", "regime_valid_true", "regime_valid_false",
        "final_trades", "missing_5d_exit", "bad_data_flags",
        "first_date", "last_date",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for a in audits:
            w.writerow({k: a.get(k, "") for k in fields})


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



# ═══════════════════════════════════════════════════════════
# EDGE DISCOVERY OUTPUTS (Phase 1.5)
# ═══════════════════════════════════════════════════════════

EDGE_MIN_N = int(os.environ.get("EDGE_DISCOVERY_MIN_N", "100") or 100)

def _bucket_pct(v, cuts, labels):
    try:
        v = float(v)
    except Exception:
        return "unknown"
    for cut, label in zip(cuts, labels):
        if v <= cut:
            return label
    return labels[-1] if labels else "unknown"

def _edge_clean(trades):
    return [t for t in trades if not getattr(t, "bad_data_flag", False) and getattr(t, "exit_price_5d", 0) > 0]

def _edge_stats(rows, lbl="5d"):
    n, wins, wr, avg = _wr_stats(rows, lbl)
    if not rows:
        return {"n": 0, "wr": 0.0, "avg": 0.0, "mfe": 0.0, "mae": 0.0}
    return {
        "n": n,
        "wr": wr,
        "avg": avg,
        "mfe": sum(t.mfe_eod_pct for t in rows) / len(rows),
        "mae": sum(t.mae_eod_pct for t in rows) / len(rows),
    }

def _stat(rows, lbl="5d"):
    """Compatibility helper used by MTF/location summary writers.

    Phase 1.8 introduced additional summary writers that referenced `_stat`
    while the canonical helper in this file is `_edge_stats`. Keep this thin
    alias so late summary/report writers cannot fail after a long data run.
    """
    return _edge_stats(rows, lbl)

def _edge_fields(t):
    fib_bucket = _bucket_pct(t.fib_distance_pct, [0.25, 0.50, 1.00, 2.00, 9999], ["fib_very_near", "fib_near", "fib_workable", "fib_far", "fib_none"])
    res_bucket = _bucket_pct(t.swing_dist_above_pct, [0.50, 1.00, 2.00, 4.00, 9999], ["res_very_near", "res_near", "res_workable", "res_far", "res_none"])
    sup_bucket = _bucket_pct(t.swing_dist_below_pct, [0.50, 1.00, 2.00, 4.00, 9999], ["sup_very_near", "sup_near", "sup_workable", "sup_far", "sup_none"])
    score_bucket = _bucket_pct(t.score, [54, 64, 74, 84, 100], ["score_lt55", "score_55_64", "score_65_74", "score_75_84", "score_85_plus"])
    rsi_bucket = _bucket_pct(t.rsi, [30, 40, 50, 60, 70, 100], ["rsi_lt30", "rsi_30_40", "rsi_40_50", "rsi_50_60", "rsi_60_70", "rsi_70_plus"])
    adx_bucket = _bucket_pct(t.adx, [15, 20, 25, 35, 100], ["adx_lt15", "adx_15_20", "adx_20_25", "adx_25_35", "adx_35_plus"])
    vol_bucket = _bucket_pct(t.volume_ratio, [0.80, 1.00, 1.25, 1.50, 2.00, 9999], ["vol_lt80", "vol_80_100", "vol_100_125", "vol_125_150", "vol_150_200", "vol_200_plus"])
    macd_sign = "macd_pos" if t.macd_hist > 0 else ("macd_neg" if t.macd_hist < 0 else "macd_zero")
    wt_cross = "wt_bull" if t.wt1 > t.wt2 else ("wt_bear" if t.wt1 < t.wt2 else "wt_flat")
    pb_alignment = "pb_aligned_bull" if (t.bias == "bull" and t.pb_state in ("above_roof", "post_box")) else "pb_aligned_bear" if (t.bias == "bear" and t.pb_state == "below_floor") else "pb_in_box" if t.pb_state == "in_box" else "pb_unaligned"
    return {
        "fib_bucket": fib_bucket, "swing_above_bucket": res_bucket, "swing_below_bucket": sup_bucket,
        "score_bucket": score_bucket, "rsi_bucket": rsi_bucket, "adx_bucket": adx_bucket,
        "volume_bucket": vol_bucket, "macd_sign": macd_sign, "wt_cross": wt_cross,
        "pb_alignment": pb_alignment,
        "regime_htf": f"{t.regime}|{t.htf_status}",
        "structure_combo": f"{t.bias}|{t.pb_state}|{t.cb_side}|{t.pb_wave_label}",
        "core_combo": f"{t.bias}|{t.regime}|{t.htf_status}|{t.confluence_bucket}",
        "location_combo": f"{t.bias}|vwap_{'above' if t.above_vwap else 'below'}|{fib_bucket}|{res_bucket}|{sup_bucket}",
        "timing_location_combo": f"{t.bias}|{getattr(t, 'or30_state', 'unknown')}|{getattr(t, 'vwap_location_state', 'unknown')}|{getattr(t, 'atr_extension_bucket', 'unknown')}",
        "approved_location_combo": f"{getattr(t, 'setup_archetype', 'unknown')}|mtf={getattr(t, 'mtf_alignment_label', 'unknown')}|{getattr(t, 'or30_state', 'unknown')}|{getattr(t, 'vwap_location_state', 'unknown')}|{getattr(t, 'atr_extension_bucket', 'unknown')}",
    }

def _baseline_wr(trades, bias=None):
    rows = _edge_clean([t for t in trades if bias is None or t.bias == bias])
    return _edge_stats(rows, "5d")["wr"]

def write_edge_discovery_csv(trades, path):
    fields = [
        "ticker","signal_date","signal_time_ct","bias","tier","score","regime","regime_valid","regime_reason","htf_status","phase",
        "entry_price","above_vwap","pb_state","cb_side","pb_wave_label","pb_break_confirmed","pb_alignment","confluence_bucket",
        "fib_nearest_level","fib_distance_pct","fib_bucket","swing_dist_above_pct","swing_above_bucket","swing_dist_below_pct","swing_below_bucket",
        "ema_dist_pct","macd_hist","macd_sign","rsi","rsi_bucket","adx","adx_bucket","wt1","wt2","wt_cross","volume_ratio","volume_bucket",
        "score_bucket","regime_htf","structure_combo","core_combo","location_combo",
        "setup_archetype","approved_setup","setup_action","block_reason","suggested_hold_window","suggested_vehicle","setup_score",
        "setup_grade","grade_reason","vehicle_preference","vehicle_reason","debit_score","credit_score",
        "debit_proxy_win","credit_proxy_win","recommended_structure",
        "debit_short_proxy_pnl_pct","debit_required_itm_pct","debit_required_itm_bucket",
        "debit_atm_short_win_5d","debit_itm_0_5_short_win_5d","debit_itm_1_0_short_win_5d",
        "debit_itm_1_5_short_win_5d","debit_itm_2_0_short_win_5d","debit_itm_2_5_short_win_5d",
        "mtf_15m_trend","mtf_30m_trend","mtf_60m_trend",
        "mtf_15m_macd","mtf_30m_macd","mtf_60m_macd",
        "mtf_15m_rsi","mtf_30m_rsi","mtf_60m_rsi",
        "mtf_15m_adx","mtf_30m_adx","mtf_60m_adx",
        "mtf_alignment_score","mtf_match_count","mtf_oppose_count","mtf_alignment_label","mtf_stack",
        "or30_high","or30_low","or30_mid","or30_state","or30_distance_pct",
        "vwap_distance_pct","vwap_location_state","atr14_5m","atr_extension_pct","atr_extension_bucket",
        "recent_move_30m_pct","recent_move_60m_pct","recent_move_bucket","location_timing_combo",
        "timing_location_combo","approved_location_combo",
        "pnl_pct_eod","win_eod","pnl_pct_1d","win_1d","pnl_pct_2d","win_2d","pnl_pct_3d","win_3d","pnl_pct_5d","win_5d",
        "mfe_eod_pct","mae_eod_pct","bad_data_flag","bad_data_reason"
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            d = asdict(t)
            d.update(_edge_fields(t))
            w.writerow({k: d.get(k, "") for k in fields})

def _group_rows(trades, groupers):
    g = defaultdict(list)
    for t in _edge_clean(trades):
        for name, fn in groupers:
            g[(name, str(fn(t) or "unknown"))].append(t)
    return g

def _write_group_summary(trades, path, groupers, include_bias_tier=False):
    all_base = _baseline_wr(trades)
    bull_base = _baseline_wr(trades, "bull")
    bear_base = _baseline_wr(trades, "bear")
    rows = []
    for (dim, val), ts in _group_rows(trades, groupers).items():
        if len(ts) < EDGE_MIN_N:
            continue
        bias = "bull" if sum(1 for t in ts if t.bias == "bull") >= len(ts)/2 else "bear"
        base = bull_base if bias == "bull" else bear_base
        s1, s3, s5 = _edge_stats(ts, "1d"), _edge_stats(ts, "3d"), _edge_stats(ts, "5d")
        rows.append((s5["wr"] - base, s5["avg"], dim, val, bias, s1, s3, s5, all_base, base))
    rows.sort(key=lambda r: (r[0], r[1], r[7]["n"]), reverse=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dimension","value","majority_bias","n","wr_1d","wr_3d","wr_5d","avg_pnl_5d","avg_mfe_eod","avg_mae_eod","vs_majority_bias_wr_5d","vs_all_wr_5d"])
        for lift, avg, dim, val, bias, s1, s3, s5, all_base, base in rows:
            w.writerow([dim, val, bias, s5["n"], f"{s1['wr']:.1f}", f"{s3['wr']:.1f}", f"{s5['wr']:.1f}", f"{s5['avg']:+.3f}", f"{s5['mfe']:+.3f}", f"{s5['mae']:+.3f}", f"{lift:+.1f}", f"{s5['wr'] - all_base:+.1f}"])

def write_edge_by_feature(trades, path):
    groupers = [
        ("bias", lambda t: t.bias), ("ticker", lambda t: t.ticker), ("tier", lambda t: t.tier),
        ("regime", lambda t: t.regime), ("regime_valid", lambda t: bool(t.regime_valid)),
        ("htf_status", lambda t: t.htf_status), ("phase", lambda t: t.phase),
        ("above_vwap", lambda t: bool(t.above_vwap)), ("vwap_location_state", lambda t: getattr(t, "vwap_location_state", "unknown")),
        ("or30_state", lambda t: getattr(t, "or30_state", "unknown")),
        ("atr_extension_bucket", lambda t: getattr(t, "atr_extension_bucket", "unknown")),
        ("recent_move_bucket", lambda t: getattr(t, "recent_move_bucket", "unknown")),
        ("pb_state", lambda t: t.pb_state),
        ("cb_side", lambda t: t.cb_side), ("pb_wave_label", lambda t: t.pb_wave_label),
        ("pb_break_confirmed", lambda t: bool(t.pb_break_confirmed)),
        ("confluence_bucket", lambda t: t.confluence_bucket),
        ("pb_alignment", lambda t: _edge_fields(t)["pb_alignment"]),
        ("fib_bucket", lambda t: _edge_fields(t)["fib_bucket"]),
        ("swing_above_bucket", lambda t: _edge_fields(t)["swing_above_bucket"]),
        ("swing_below_bucket", lambda t: _edge_fields(t)["swing_below_bucket"]),
        ("score_bucket", lambda t: _edge_fields(t)["score_bucket"]),
        ("rsi_bucket", lambda t: _edge_fields(t)["rsi_bucket"]),
        ("adx_bucket", lambda t: _edge_fields(t)["adx_bucket"]),
        ("volume_bucket", lambda t: _edge_fields(t)["volume_bucket"]),
        ("macd_sign", lambda t: _edge_fields(t)["macd_sign"]),
        ("wt_cross", lambda t: _edge_fields(t)["wt_cross"]),
        ("setup_archetype", lambda t: t.setup_archetype),
        ("setup_action", lambda t: t.setup_action),
        ("setup_grade", lambda t: getattr(t, "setup_grade", "UNRATED")),
        ("vehicle_preference", lambda t: getattr(t, "vehicle_preference", "none")),
        ("recommended_structure", lambda t: getattr(t, "recommended_structure", "none")),
        ("approved_setup", lambda t: bool(t.approved_setup)),
        ("mtf_alignment_label", lambda t: t.mtf_alignment_label or "unknown"),
        ("mtf_alignment_score", lambda t: str(t.mtf_alignment_score)),
        ("mtf_15m_trend", lambda t: t.mtf_15m_trend or "unknown"),
        ("mtf_30m_trend", lambda t: t.mtf_30m_trend or "unknown"),
        ("mtf_60m_trend", lambda t: t.mtf_60m_trend or "unknown"),
    ]
    _write_group_summary(trades, path, groupers)

def write_edge_by_combo(trades, path):
    groupers = [
        ("core_combo", lambda t: _edge_fields(t)["core_combo"]),
        ("structure_combo", lambda t: _edge_fields(t)["structure_combo"]),
        ("location_combo", lambda t: _edge_fields(t)["location_combo"]),
        ("bias_regime_htf", lambda t: f"{t.bias}|{t.regime}|{t.htf_status}"),
        ("bias_pb_cb", lambda t: f"{t.bias}|{t.pb_state}|{t.cb_side}"),
        ("bias_pb_wave", lambda t: f"{t.bias}|{t.pb_state}|{t.pb_wave_label}"),
        ("bias_pb_vwap", lambda t: f"{t.bias}|{t.pb_state}|vwap_{'above' if t.above_vwap else 'below'}"),
        ("bias_regime_confluence", lambda t: f"{t.bias}|{t.regime}|{t.confluence_bucket}"),
        ("ticker_bias_confluence", lambda t: f"{t.ticker}|{t.bias}|{t.confluence_bucket}"),
        ("ticker_bias_regime", lambda t: f"{t.ticker}|{t.bias}|{t.regime}"),
        ("setup_archetype", lambda t: t.setup_archetype),
        ("setup_grade", lambda t: getattr(t, "setup_grade", "UNRATED")),
        ("grade_vehicle", lambda t: f"grade={getattr(t, 'setup_grade', 'UNRATED')}|vehicle={getattr(t, 'vehicle_preference', 'none')}"),
        ("setup_vehicle", lambda t: f"{t.setup_archetype}|vehicle={getattr(t, 'vehicle_preference', 'none')}"),
        ("setup_action_archetype", lambda t: f"{t.setup_action}|{t.setup_archetype}"),
        ("approved_bias_regime", lambda t: f"approved={t.approved_setup}|{t.bias}|{t.regime}"),
        ("approved_mtf", lambda t: f"{t.setup_archetype}|mtf={t.mtf_alignment_label}"),
        ("approved_mtf_score", lambda t: f"{t.setup_archetype}|mtf_score={t.mtf_alignment_score}"),
        ("bias_pb_mtf", lambda t: f"{t.bias}|pb={t.pb_state}|cb={t.cb_side}|mtf={t.mtf_alignment_label}"),
        ("bias_fib_mtf", lambda t: f"{t.bias}|fib={_edge_fields(t)['fib_bucket']}|mtf={t.mtf_alignment_label}"),
        ("location_timing", lambda t: getattr(t, "location_timing_combo", "unknown")),
        ("approved_location_timing", lambda t: _edge_fields(t).get("approved_location_combo", "unknown")),
        ("setup_or30_vwap", lambda t: f"{t.setup_archetype}|or30={getattr(t, 'or30_state', 'unknown')}|vwap={getattr(t, 'vwap_location_state', 'unknown')}"),
    ]
    _write_group_summary(trades, path, groupers)

def _write_screen(trades, path, positive=True):
    temp = OUT_DIR / (".edge_combo_tmp_pos.csv" if positive else ".edge_combo_tmp_neg.csv")
    write_edge_by_combo(trades, temp)
    with open(temp, newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            n = int(r["n"])
            wr = float(r["wr_5d"])
            avg = float(r["avg_pnl_5d"])
            lift = float(r["vs_majority_bias_wr_5d"])
        except Exception:
            continue
        if positive:
            ok = n >= EDGE_MIN_N and wr >= 58 and avg > 0.25 and lift >= 3
            why = "Candidate missed edge: consider promoting from research/shadow to approved setup filter"
        else:
            ok = n >= EDGE_MIN_N and (wr <= 47 or avg < -0.25 or lift <= -5)
            why = "Candidate negative edge: consider shadow-only, stronger penalty, or hard block"
        if ok:
            out.append((lift, avg, wr, r, why))
    out.sort(key=lambda x: (x[0], x[1], x[2]), reverse=positive)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["combo_type","combo_value","majority_bias","n","wr_5d","avg_pnl_5d","vs_bias_wr_5d","why_it_matters"])
        for lift, avg, wr, r, why in out[:300]:
            w.writerow([r["dimension"], r["value"], r["majority_bias"], r["n"], r["wr_5d"], r["avg_pnl_5d"], r["vs_majority_bias_wr_5d"], why])
    try:
        temp.unlink()
    except Exception:
        pass

def write_missed_edge_candidates(trades, path):
    _write_screen(trades, path, positive=True)

def write_negative_edge_filters(trades, path):
    _write_screen(trades, path, positive=False)

def write_approved_setups_csv(trades, path):
    fields = [
        "ticker","signal_date","signal_time_ct","bias","tier","score","setup_score",
        "setup_archetype","setup_grade","grade_reason","approved_setup","setup_action","block_reason",
        "suggested_hold_window","suggested_vehicle","vehicle_preference","vehicle_reason",
        "debit_score","credit_score","debit_proxy_win","credit_proxy_win","recommended_structure",
        "regime","regime_valid","htf_status",
        "pb_state","cb_side","pb_wave_label","above_vwap","confluence_bucket",
        "entry_price","pnl_pct_1d","win_1d","pnl_pct_3d","win_3d","pnl_pct_5d","win_5d",
        "mfe_eod_pct","mae_eod_pct","bad_data_flag","bad_data_reason",
    ]
    rows = [t for t in _edge_clean(trades) if t.setup_action in ("APPROVE", "BLOCK", "SHADOW", "RESEARCH")]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in rows:
            d = asdict(t)
            w.writerow({k: d.get(k, "") for k in fields})


def write_setup_classifier_summary(trades, path):
    rows = _edge_clean(trades)
    g = defaultdict(list)
    for t in rows:
        g[(t.setup_action, getattr(t, "setup_grade", "UNRATED"), t.setup_archetype, getattr(t, "vehicle_preference", "none"), getattr(t, "recommended_structure", "none"), t.bias)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_action","setup_grade","setup_archetype","vehicle_preference","recommended_structure","bias","n","wr_eod","wr_1d","wr_2d","wr_3d","wr_5d",
            "avg_pnl_5d","avg_mfe_eod","avg_mae_eod","suggested_hold_window","suggested_vehicle"
        ])
        for (action, grade, archetype, vehicle_pref, recommended_structure, bias), ts in sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(ts) < 25:
                continue
            row = [action, grade, archetype, vehicle_pref, recommended_structure, bias, len(ts)]
            for lbl in ("eod", "1d", "2d", "3d", "5d"):
                _, _, wr, _ = _wr_stats(ts, lbl)
                row.append(f"{wr:.1f}")
            _, _, _, avg5 = _wr_stats(ts, "5d")
            amfe = sum(t.mfe_eod_pct for t in ts) / len(ts)
            amae = sum(t.mae_eod_pct for t in ts) / len(ts)
            hold_counts = defaultdict(int)
            vehicle_counts = defaultdict(int)
            for item in ts:
                hold_counts[item.suggested_hold_window] += 1
                vehicle_counts[item.suggested_vehicle] += 1
            hold = max(hold_counts.items(), key=lambda kv: kv[1])[0] if hold_counts else ""
            vehicle = max(vehicle_counts.items(), key=lambda kv: kv[1])[0] if vehicle_counts else ""
            row += [f"{avg5:+.3f}", f"{amfe:+.3f}", f"{amae:+.3f}", hold, vehicle]
            w.writerow(row)



def write_setup_grade_summary(trades, path):
    rows = _edge_clean(trades)
    groups = defaultdict(list)
    for t in rows:
        groups[(getattr(t, "setup_grade", "UNRATED"), getattr(t, "setup_archetype", "UNKNOWN"), getattr(t, "vehicle_preference", "none"), t.bias)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_grade","setup_archetype","vehicle_preference","bias","n",
            "wr_1d","wr_3d","wr_5d","avg_pnl_5d","avg_mfe_eod","avg_mae_eod",
            "debit_proxy_wr","credit_proxy_wr","recommended_structure"
        ])
        for (grade, archetype, vehicle_pref, bias), ts in sorted(groups.items(), key=lambda kv: (-_grade_rank(kv[0][0]), -len(kv[1]), kv[0])):
            if len(ts) < 25:
                continue
            s1, s3, s5 = _edge_stats(ts, "1d"), _edge_stats(ts, "3d"), _edge_stats(ts, "5d")
            debit_wr = 100 * sum(1 for t in ts if getattr(t, "debit_proxy_win", False)) / len(ts)
            credit_wr = 100 * sum(1 for t in ts if getattr(t, "credit_proxy_win", False)) / len(ts)
            rec_counts = defaultdict(int)
            for item in ts:
                rec_counts[getattr(item, "recommended_structure", "none")] += 1
            rec = max(rec_counts.items(), key=lambda kv: kv[1])[0] if rec_counts else "none"
            w.writerow([
                grade, archetype, vehicle_pref, bias, len(ts),
                f"{s1['wr']:.1f}", f"{s3['wr']:.1f}", f"{s5['wr']:.1f}", f"{s5['avg']:+.3f}",
                f"{s5['mfe']:+.3f}", f"{s5['mae']:+.3f}",
                f"{debit_wr:.1f}", f"{credit_wr:.1f}", rec
            ])


def write_vehicle_selection_summary(trades, path):
    rows = _edge_clean(trades)
    groups = defaultdict(list)
    for t in rows:
        groups[(getattr(t, "vehicle_preference", "none"), getattr(t, "recommended_structure", "none"), getattr(t, "setup_grade", "UNRATED"), getattr(t, "setup_archetype", "UNKNOWN"))].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "vehicle_preference","recommended_structure","setup_grade","setup_archetype","n",
            "wr_5d_debit_proxy","avg_debit_pnl_5d","wr_5d_credit_proxy",
            "avg_debit_score","avg_credit_score","vehicle_edge_note"
        ])
        for (vehicle_pref, rec, grade, archetype), ts in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(ts) < 25:
                continue
            s5 = _edge_stats(ts, "5d")
            credit_wr = 100 * sum(1 for t in ts if getattr(t, "credit_proxy_win", False)) / len(ts)
            debit_score = sum(getattr(t, "debit_score", 0) for t in ts) / len(ts)
            credit_score = sum(getattr(t, "credit_score", 0) for t in ts) / len(ts)
            if credit_wr >= s5["wr"] + 10:
                note = "credit proxy materially stronger than debit price-direction outcome"
            elif s5["wr"] >= credit_wr + 10:
                note = "debit price-direction outcome materially stronger than credit proxy"
            else:
                note = "debit/credit proxies similar; choose by IV/entry"
            w.writerow([
                vehicle_pref, rec, grade, archetype, len(ts),
                f"{s5['wr']:.1f}", f"{s5['avg']:+.3f}", f"{credit_wr:.1f}",
                f"{debit_score:.1f}", f"{credit_score:.1f}", note
            ])


def write_grade_x_vehicle_summary(trades, path):
    rows = _edge_clean(trades)
    groups = defaultdict(list)
    for t in rows:
        groups[(getattr(t, "setup_grade", "UNRATED"), getattr(t, "vehicle_preference", "none"), getattr(t, "mtf_alignment_label", "unknown"), getattr(t, "or30_state", "unknown"), getattr(t, "vwap_location_state", "unknown"))].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_grade","vehicle_preference","mtf_alignment_label","or30_state","vwap_location_state",
            "n","wr_5d_debit_proxy","avg_debit_pnl_5d","wr_5d_credit_proxy","avg_mfe_eod","avg_mae_eod"
        ])
        for key, ts in sorted(groups.items(), key=lambda kv: (-_grade_rank(kv[0][0]), -len(kv[1]), kv[0])):
            if len(ts) < 50:
                continue
            s5 = _edge_stats(ts, "5d")
            credit_wr = 100 * sum(1 for t in ts if getattr(t, "credit_proxy_win", False)) / len(ts)
            w.writerow([
                key[0], key[1], key[2], key[3], key[4], len(ts),
                f"{s5['wr']:.1f}", f"{s5['avg']:+.3f}", f"{credit_wr:.1f}",
                f"{s5['mfe']:+.3f}", f"{s5['mae']:+.3f}"
            ])


def write_edge_by_mtf(trades, path):
    groupers = [
        ("mtf_alignment_label", lambda t: t.mtf_alignment_label or "unknown"),
        ("mtf_alignment_score", lambda t: str(t.mtf_alignment_score)),
        ("mtf_stack", lambda t: t.mtf_stack or "unknown"),
        ("setup_mtf", lambda t: f"{t.setup_archetype}|{t.mtf_alignment_label}"),
        ("approved_mtf", lambda t: f"approved={t.approved_setup}|{t.mtf_alignment_label}"),
        ("pb_mtf", lambda t: f"pb={t.pb_state}|cb={t.cb_side}|{t.mtf_alignment_label}"),
    ]
    _write_group_summary(trades, path, groupers)


def write_mtf_confluence_summary(trades, path):
    clean = [t for t in trades if not t.bad_data_flag]
    groups = defaultdict(list)
    for t in clean:
        groups[(t.setup_archetype, t.bias, t.mtf_alignment_label, t.mtf_alignment_score)].append(t)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_archetype","bias","mtf_alignment_label","mtf_alignment_score",
            "n","wr_1d","wr_3d","wr_5d","avg_pnl_5d","approved_setup","setup_action",
            "suggested_hold_window","suggested_vehicle",
        ])
        for key, ts in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(ts) < 50:
                continue
            s1 = _stat(ts, "1d"); s3 = _stat(ts, "3d"); s5 = _stat(ts, "5d")
            sample = ts[0]
            w.writerow([
                key[0], key[1], key[2], key[3], len(ts),
                f"{s1['wr']:.1f}", f"{s3['wr']:.1f}", f"{s5['wr']:.1f}", f"{s5['avg']:+.3f}",
                sample.approved_setup, sample.setup_action, sample.suggested_hold_window, sample.suggested_vehicle,
            ])




def write_edge_by_location(trades, path):
    groupers = [
        ("or30_state", lambda t: getattr(t, "or30_state", "unknown")),
        ("vwap_location_state", lambda t: getattr(t, "vwap_location_state", "unknown")),
        ("atr_extension_bucket", lambda t: getattr(t, "atr_extension_bucket", "unknown")),
        ("recent_move_bucket", lambda t: getattr(t, "recent_move_bucket", "unknown")),
        ("location_timing_combo", lambda t: getattr(t, "location_timing_combo", "unknown")),
        ("approved_location_combo", lambda t: _edge_fields(t).get("approved_location_combo", "unknown")),
        ("setup_or30", lambda t: f"{t.setup_archetype}|{getattr(t, 'or30_state', 'unknown')}"),
        ("setup_vwap", lambda t: f"{t.setup_archetype}|{getattr(t, 'vwap_location_state', 'unknown')}"),
        ("setup_atr_extension", lambda t: f"{t.setup_archetype}|{getattr(t, 'atr_extension_bucket', 'unknown')}"),
    ]
    _write_group_summary(trades, path, groupers)


def _safe_final_write(label: str, writer, *args, errors: list | None = None):
    """Run a final writer without allowing a late report/summary bug to fail a long run."""
    try:
        writer(*args)
        return True
    except Exception as e:
        msg = f"{label} failed: {type(e).__name__}: {e}"
        log.exception(msg)
        if errors is not None:
            errors.append({"writer": label, "error": str(e), "type": type(e).__name__})
        return False


def write_summary_errors(errors: list, path):
    if not errors:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["writer", "type", "error"])
        w.writeheader()
        w.writerows(errors)


def _pct_true(rows, field: str) -> float:
    if not rows:
        return 0.0
    return 100.0 * sum(1 for t in rows if bool(getattr(t, field, False))) / len(rows)


def write_debit_strike_placement_summary(trades, path):
    """Summarize proxy win rate by debit-spread short-strike placement."""
    rows = _edge_clean(trades)
    groups = defaultdict(list)
    for t in rows:
        groups[(
            getattr(t, "setup_grade", "UNRATED"),
            getattr(t, "setup_archetype", "UNKNOWN"),
            getattr(t, "vehicle_preference", "none"),
            getattr(t, "recommended_structure", "none"),
            getattr(t, "mtf_alignment_label", "unknown"),
            getattr(t, "or30_state", "unknown"),
            getattr(t, "vwap_location_state", "unknown"),
            t.bias,
        )].append(t)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_grade","setup_archetype","vehicle_preference","recommended_structure",
            "mtf_alignment_label","or30_state","vwap_location_state","bias","n",
            "avg_pnl_5d",
            "wr_atm_short","wr_0_5_itm_short","wr_1_0_itm_short","wr_1_5_itm_short",
            "wr_2_0_itm_short","wr_2_5_itm_short",
            "lift_0_5_vs_atm","lift_1_0_vs_atm","lift_1_5_vs_atm","lift_2_0_vs_atm",
            "avg_required_itm_pct","median_required_itm_pct","required_itm_bucket_mode",
            "avg_mfe_eod","avg_mae_eod"
        ])
        for key, ts in sorted(groups.items(), key=lambda kv: (-_grade_rank(kv[0][0]), -len(kv[1]), kv[0])):
            if len(ts) < 25:
                continue
            s5 = _edge_stats(ts, "5d")
            wr_atm = _pct_true(ts, "debit_atm_short_win_5d")
            wr05 = _pct_true(ts, "debit_itm_0_5_short_win_5d")
            wr10 = _pct_true(ts, "debit_itm_1_0_short_win_5d")
            wr15 = _pct_true(ts, "debit_itm_1_5_short_win_5d")
            wr20 = _pct_true(ts, "debit_itm_2_0_short_win_5d")
            wr25 = _pct_true(ts, "debit_itm_2_5_short_win_5d")
            reqs = sorted(float(getattr(t, "debit_required_itm_pct", 0.0) or 0.0) for t in ts)
            avg_req = sum(reqs) / len(reqs) if reqs else 0.0
            med_req = reqs[len(reqs)//2] if reqs else 0.0
            bucket_counts = defaultdict(int)
            for t in ts:
                bucket_counts[getattr(t, "debit_required_itm_bucket", "unknown")] += 1
            mode_bucket = max(bucket_counts.items(), key=lambda kv: kv[1])[0] if bucket_counts else "unknown"
            w.writerow([
                *key, len(ts),
                f"{s5['avg']:+.3f}",
                f"{wr_atm:.1f}", f"{wr05:.1f}", f"{wr10:.1f}", f"{wr15:.1f}",
                f"{wr20:.1f}", f"{wr25:.1f}",
                f"{(wr05 - wr_atm):+.1f}", f"{(wr10 - wr_atm):+.1f}",
                f"{(wr15 - wr_atm):+.1f}", f"{(wr20 - wr_atm):+.1f}",
                f"{avg_req:.3f}", f"{med_req:.3f}", mode_bucket,
                f"{s5['mfe']:+.3f}", f"{s5['mae']:+.3f}"
            ])


# ─────────────────────────────────────────────────────────────
# Phase 2.1 — $2.50 debit spread expectancy proxy
# ─────────────────────────────────────────────────────────────

def _debit_spread_ev(win_rate_pct: float, debit: float, width: float = 2.50) -> dict:
    """Simple expiration-style expectancy proxy for a debit spread.

    Winners are treated as full-width wins and losers as full-debit losses.
    This does not model option mark-to-market, early exits, IV, skew, fills,
    or commissions. It is a comparison tool for short-strike placement and
    assumed debit before building a true options P/L model.
    """
    max_profit = max(0.0, width - debit)
    max_loss = max(0.0, debit)
    wr = max(0.0, min(100.0, win_rate_pct)) / 100.0
    breakeven_wr = (debit / width * 100.0) if width > 0 else 100.0
    ev = (wr * max_profit) - ((1.0 - wr) * max_loss)
    return {
        "width": width,
        "debit": debit,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "breakeven_wr": breakeven_wr,
        "edge_cushion": win_rate_pct - breakeven_wr,
        "ev_per_spread": ev,
        "ev_per_contract": ev * 100.0,
    }


def write_debit_expectancy_summary(trades, path):
    """Summarize $2.50 debit-spread expectancy by setup and ITM short strike.

    Rows are grouped by setup grade/archetype/MTF/vehicle/location, then exploded
    across short-strike placement and assumed debit paid ($1.50/$1.60/$1.70).

    NOTE: This is a price-threshold proxy. A "win" means the 5D underlying close
    finished above the modeled short strike for bullish debit spreads. It is not
    yet a true option P/L model.
    """
    rows = _edge_clean(trades)
    groups = defaultdict(list)
    for t in rows:
        groups[(
            getattr(t, "setup_grade", "UNRATED"),
            getattr(t, "setup_archetype", "UNKNOWN"),
            getattr(t, "vehicle_preference", "none"),
            getattr(t, "recommended_structure", "none"),
            getattr(t, "mtf_alignment_label", "unknown"),
            getattr(t, "or30_state", "unknown"),
            getattr(t, "vwap_location_state", "unknown"),
            t.bias,
        )].append(t)

    placements = [
        ("ATM", 0.0, "debit_atm_short_win_5d"),
        ("0.5% ITM", 0.5, "debit_itm_0_5_short_win_5d"),
        ("1.0% ITM", 1.0, "debit_itm_1_0_short_win_5d"),
        ("1.5% ITM", 1.5, "debit_itm_1_5_short_win_5d"),
        ("2.0% ITM", 2.0, "debit_itm_2_0_short_win_5d"),
        ("2.5% ITM", 2.5, "debit_itm_2_5_short_win_5d"),
    ]
    debits = [1.50, 1.60, 1.70]
    width = 2.50

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_grade", "setup_archetype", "vehicle_preference", "recommended_structure",
            "mtf_alignment_label", "or30_state", "vwap_location_state", "bias", "n",
            "short_strike_placement", "short_strike_itm_pct", "assumed_width", "assumed_debit",
            "max_profit", "max_loss", "proxy_wr_5d", "breakeven_wr", "edge_cushion",
            "ev_per_spread", "ev_per_contract", "avg_pnl_5d", "avg_mfe_eod", "avg_mae_eod",
            "expectancy_label"
        ])
        for key, ts in sorted(groups.items(), key=lambda kv: (-_grade_rank(kv[0][0]), -len(kv[1]), kv[0])):
            if len(ts) < 25:
                continue
            s5 = _edge_stats(ts, "5d")
            for placement_name, itm_pct, field in placements:
                wr = _pct_true(ts, field)
                for debit in debits:
                    ev = _debit_spread_ev(wr, debit, width=width)
                    cushion = ev["edge_cushion"]
                    if ev["ev_per_spread"] > 0.20 and cushion >= 10:
                        label = "strong_positive"
                    elif ev["ev_per_spread"] > 0.05 and cushion >= 3:
                        label = "positive"
                    elif ev["ev_per_spread"] >= 0:
                        label = "thin_positive"
                    else:
                        label = "negative"
                    w.writerow([
                        *key, len(ts),
                        placement_name, f"{itm_pct:.1f}", f"{width:.2f}", f"{debit:.2f}",
                        f"{ev['max_profit']:.2f}", f"{ev['max_loss']:.2f}", f"{wr:.1f}",
                        f"{ev['breakeven_wr']:.1f}", f"{cushion:+.1f}",
                        f"{ev['ev_per_spread']:+.3f}", f"{ev['ev_per_contract']:+.2f}",
                        f"{s5['avg']:+.3f}", f"{s5['mfe']:+.3f}", f"{s5['mae']:+.3f}",
                        label,
                    ])


def write_debit_expectancy_topline(trades, path):
    """Smaller high-signal view: grade/archetype only, no OR30/VWAP split."""
    rows = _edge_clean(trades)
    groups = defaultdict(list)
    for t in rows:
        groups[(
            getattr(t, "setup_grade", "UNRATED"),
            getattr(t, "setup_archetype", "UNKNOWN"),
            getattr(t, "vehicle_preference", "none"),
            getattr(t, "recommended_structure", "none"),
            getattr(t, "mtf_alignment_label", "unknown"),
            t.bias,
        )].append(t)

    placements = [
        ("ATM", 0.0, "debit_atm_short_win_5d"),
        ("0.5% ITM", 0.5, "debit_itm_0_5_short_win_5d"),
        ("1.0% ITM", 1.0, "debit_itm_1_0_short_win_5d"),
        ("1.5% ITM", 1.5, "debit_itm_1_5_short_win_5d"),
        ("2.0% ITM", 2.0, "debit_itm_2_0_short_win_5d"),
        ("2.5% ITM", 2.5, "debit_itm_2_5_short_win_5d"),
    ]
    debits = [1.50, 1.60, 1.70]
    width = 2.50
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "setup_grade", "setup_archetype", "vehicle_preference", "recommended_structure",
            "mtf_alignment_label", "bias", "n", "short_strike_placement", "short_strike_itm_pct",
            "assumed_debit", "proxy_wr_5d", "breakeven_wr", "edge_cushion",
            "ev_per_spread", "ev_per_contract", "expectancy_label"
        ])
        for key, ts in sorted(groups.items(), key=lambda kv: (-_grade_rank(kv[0][0]), -len(kv[1]), kv[0])):
            if len(ts) < 25:
                continue
            for placement_name, itm_pct, field in placements:
                wr = _pct_true(ts, field)
                for debit in debits:
                    ev = _debit_spread_ev(wr, debit, width=width)
                    cushion = ev["edge_cushion"]
                    if ev["ev_per_spread"] > 0.20 and cushion >= 10:
                        label = "strong_positive"
                    elif ev["ev_per_spread"] > 0.05 and cushion >= 3:
                        label = "positive"
                    elif ev["ev_per_spread"] >= 0:
                        label = "thin_positive"
                    else:
                        label = "negative"
                    w.writerow([
                        *key, len(ts), placement_name, f"{itm_pct:.1f}", f"{debit:.2f}",
                        f"{wr:.1f}", f"{ev['breakeven_wr']:.1f}", f"{cushion:+.1f}",
                        f"{ev['ev_per_spread']:+.3f}", f"{ev['ev_per_contract']:+.2f}", label,
                    ])

def write_edge_discovery_outputs(trades, out_dir: Path):
    write_edge_discovery_csv(trades, out_dir / "edge_discovery.csv")
    write_edge_by_feature(trades, out_dir / "edge_by_feature.csv")
    write_edge_by_combo(trades, out_dir / "edge_by_combo.csv")
    write_missed_edge_candidates(trades, out_dir / "missed_edge_candidates.csv")
    write_negative_edge_filters(trades, out_dir / "negative_edge_filters.csv")
    write_approved_setups_csv(trades, out_dir / "approved_setups.csv")
    write_setup_classifier_summary(trades, out_dir / "setup_classifier_summary.csv")
    write_setup_grade_summary(trades, out_dir / "setup_grade_summary.csv")
    write_vehicle_selection_summary(trades, out_dir / "vehicle_selection_summary.csv")
    write_grade_x_vehicle_summary(trades, out_dir / "grade_x_vehicle_summary.csv")
    write_debit_strike_placement_summary(trades, out_dir / "debit_strike_placement_summary.csv")
    write_debit_expectancy_summary(trades, out_dir / "debit_expectancy_summary.csv")
    write_debit_expectancy_topline(trades, out_dir / "debit_expectancy_topline.csv")
    write_edge_by_mtf(trades, out_dir / "edge_by_mtf.csv")
    write_mtf_confluence_summary(trades, out_dir / "mtf_confluence_summary.csv")
    write_edge_by_location(trades, out_dir / "edge_by_location.csv")

def write_report(trades, start, end, path, audits=None):
    n = len(trades)
    audits = audits or []
    bars_eval = sum(int(a.get("bars_evaluated", 0) or 0) for a in audits)
    raw_signals = sum(int(a.get("raw_signals_before_dedup", 0) or 0) for a in audits)
    dedup_removed = sum(int(a.get("deduped_signals_removed", 0) or 0) for a in audits)
    bad_flags = sum(int(a.get("bad_data_flags", 0) or 0) for a in audits)
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

**Phase 1.3 grading fix:** entries and exits are graded from the same 5-minute dataset.
Daily bars are used for regime/HTF/overlay context only, not exit pricing.

**Phase 1.4 dedup fix:** intraday duplicate suppression resets each trading day,
so a late-day signal no longer blocks most of the next day's signals.

## Backtest audit

| Metric | Value |
|---|---:|
| Bars evaluated | {bars_eval:,} |
| Raw signals before dedup | {raw_signals:,} |
| Deduped signals removed | {dedup_removed:,} |
| Final signals/trades | {n:,} |
| Bad-data flags | {bad_flags:,} |

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

- **`trades.csv`** — one row per final scanner signal, exits graded from 5-minute last closes
- **`backtest_audit.csv`** — bars evaluated, raw signals, deduped signals, final trades by ticker
- **`summary_by_ticker.csv`** — which tickers carry the edge
- **`summary_by_regime.csv`** — does edge only exist in specific regimes
- **`summary_by_htf_status.csv`** — P2 TRANSITION CONVERGING fix validation
- **`summary_by_confluence.csv`** — ← **the money table**. Overlay WR vs baseline.
  `vs_baseline_5d` > +5 means this overlay adds edge. < -5 means it destroys it.
- **`summary_by_indicator.csv`** — quintile WR per indicator (is RSI a filter?)
- **`summary_by_credit.csv`** — credit spread WR at Potter Box boundaries
- **`edge_discovery.csv`** — Phase 1.5 research file, one row per scanner-qualified candidate with derived feature/context fields
- **`edge_by_feature.csv`** — which individual features help or hurt vs baseline
- **`edge_by_combo.csv`** — which feature combinations contain hidden edge
- **`missed_edge_candidates.csv`** — strong combinations the live bot may be underusing
- **`negative_edge_filters.csv`** — weak combinations that may deserve shadow-only/block treatment
- **`approved_setups.csv`** — Phase 1.6 classifier output for every clean scanner candidate
- **`setup_classifier_summary.csv`** — approved/shadow/block archetype performance summary
- **`edge_by_mtf.csv`** — 15m/30m/60m/daily alignment summary
- **`mtf_confluence_summary.csv`** — approved setup archetypes segmented by MTF alignment

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
    all_audits: list[dict] = []
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
        trades, audit = run_ticker(ticker, intraday, daily, regime_cache,
                                   daily_close_by_date, sorted_dates)
        n_t1 = sum(1 for t in trades if t.tier == "1")
        n_t2 = sum(1 for t in trades if t.tier == "2")
        log.info(
            f"{ticker}: {len(trades)} trades (T1={n_t1}, T2={n_t2}); "
            f"bars_evaluated={audit.get('bars_evaluated', 0):,}, "
            f"raw_signals={audit.get('raw_signals_before_dedup', 0):,}, "
            f"dedup_removed={audit.get('deduped_signals_removed', 0):,}"
        )
        all_trades.extend(trades)
        all_audits.append(audit)

        # Checkpoint after each ticker
        done.add(ticker)
        write_trades_csv(all_trades, trades_path)
        write_audit_csv(all_audits, OUT_DIR / "backtest_audit.csv")
        with open(progress_path, "w") as f:
            json.dump({"done": sorted(done)}, f)

    # ── Final writes ──
    log.info(f"Writing summaries (total: {len(all_trades)} trades)")
    final_errors = []
    _safe_final_write("trades.csv", write_trades_csv, all_trades, trades_path, errors=final_errors)
    _safe_final_write("backtest_audit.csv", write_audit_csv, all_audits, OUT_DIR / "backtest_audit.csv", errors=final_errors)
    _safe_final_write("summary_by_ticker.csv", write_summary_by_ticker, all_trades, OUT_DIR / "summary_by_ticker.csv", errors=final_errors)
    _safe_final_write("summary_by_regime.csv", write_summary_by_regime, all_trades, OUT_DIR / "summary_by_regime.csv", errors=final_errors)
    _safe_final_write("summary_by_tier.csv", write_summary_by_tier, all_trades, OUT_DIR / "summary_by_tier.csv", errors=final_errors)
    _safe_final_write("summary_by_htf_status.csv", write_summary_by_htf, all_trades, OUT_DIR / "summary_by_htf_status.csv", errors=final_errors)
    _safe_final_write("summary_by_confluence.csv", write_summary_by_confluence, all_trades, OUT_DIR / "summary_by_confluence.csv", errors=final_errors)
    _safe_final_write("summary_by_indicator.csv", write_summary_by_indicator, all_trades, OUT_DIR / "summary_by_indicator.csv", errors=final_errors)
    _safe_final_write("summary_by_credit.csv", write_summary_by_credit, all_trades, OUT_DIR / "summary_by_credit.csv", errors=final_errors)
    _safe_final_write("edge discovery outputs", write_edge_discovery_outputs, all_trades, OUT_DIR, errors=final_errors)
    _safe_final_write("report.md", write_report, all_trades, from_date, to_date, OUT_DIR / "report.md", all_audits, errors=final_errors)
    write_summary_errors(final_errors, OUT_DIR / "summary_writer_errors.csv")

    if final_errors:
        log.warning(f"Final writer errors were captured but run will continue: {final_errors}")
    log.info(f"DONE. Outputs in {OUT_DIR}:")
    for fn in sorted(os.listdir(OUT_DIR)):
        if not fn.startswith("."):
            fp = OUT_DIR / fn
            log.info(f"  {fn}  ({fp.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
