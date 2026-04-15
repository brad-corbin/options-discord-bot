# swing_scanner.py
# ═══════════════════════════════════════════════════════════════════
# Fibonacci Swing Scanner — Institutional Framework
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Python translation of Brad's Fibonacci Swing Signal v3.0 (Pine)
# plus institutional enhancements:
#   - Relative strength vs SPY
#   - Earnings avoidance
#   - Correlation grouping (max 2 per sector)
#   - Multi-touch fib scoring
#   - Primary trend filter (50/200 SMA)
#   - ATR-based position context
#   - Fib extensions as targets
#   - Macro regime awareness (VIX)
#
# Data source: Yahoo Finance (free, unlimited daily OHLCV)
# Schedule: 8:15 AM CT (pre-market) + 3:30 PM CT (post-close)
# Zero MarketData API calls.
#
# Usage:
#   from swing_scanner import SwingScanner
#   scanner = SwingScanner(enqueue_fn=..., spot_fn=...)
#   scanner.start()
# ═══════════════════════════════════════════════════════════════════

import math
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Callable

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

log = logging.getLogger(__name__)

# Import config from trading_rules
try:
    from trading_rules import (
        SWING_SCANNER_ENABLED, SWING_WATCHLIST, SWING_ONLY_TICKERS,
        SWING_SCAN_LOOKBACK_DAYS, SWING_SCAN_TIMES_CT,
        SWING_FIB_LOOKBACK, SWING_FIB_TOUCH_ZONE_PCT,
        SWING_WEEKLY_EMA_FAST, SWING_WEEKLY_EMA_SLOW, SWING_WEEKLY_MIN_SEP_PCT,
        SWING_DAILY_EMA_FAST, SWING_DAILY_EMA_SLOW,
        SWING_RSI_LENGTH, SWING_RSI_OVERSOLD, SWING_RSI_OVERBOUGHT,
        SWING_VOL_MA_LENGTH, SWING_VOL_CONTRACT_MULT, SWING_VOL_EXPAND_MULT,
        SWING_WICK_MIN_PCT, SWING_CLOSE_ZONE_PCT, SWING_COOLDOWN_BARS,
        SWING_RS_LOOKBACK_DAYS, SWING_RS_REJECT_LONG_BELOW, SWING_RS_REJECT_SHORT_ABOVE,
        SWING_MAX_PER_SECTOR, SWING_SECTOR_MAP,
        SWING_PRIMARY_TREND_SMA, SWING_PRIMARY_TREND_LMA, SWING_PRIMARY_TREND_ENABLED,
        SWING_ATR_LENGTH,
    )
except ImportError:
    log.warning("swing_scanner: trading_rules imports failed, using defaults")
    SWING_SCANNER_ENABLED = False


# ═══════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════

def _ema(data: list, period: int) -> list:
    """Exponential moving average."""
    if not data or len(data) < period:
        return []
    k = 2.0 / (period + 1)
    result = [0.0] * len(data)
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = data[i] * k + result[i - 1] * (1 - k)
    for i in range(period - 1):
        result[i] = result[period - 1]
    return result


def _sma(data: list, period: int) -> list:
    """Simple moving average."""
    if not data or len(data) < period:
        return []
    result = [0.0] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1:i + 1]) / period
    return result


def _rsi(closes: list, period: int = 14) -> list:
    """RSI. Returns list same length as closes."""
    if not closes or len(closes) < period + 1:
        return [50.0] * len(closes)
    result = [50.0] * len(closes)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    if len(gains) < period:
        return result
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            result[i + 1] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return result


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> list:
    """Average True Range."""
    if len(highs) < period + 1:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return _sma(trs, period)


def _find_pivots(highs: list, lows: list, pivot_len: int) -> Tuple[list, list]:
    """
    Swing high/low detection.
    Returns (swing_highs, swing_lows) as lists of (bar_index, price).
    """
    swing_highs = []
    swing_lows = []
    for i in range(pivot_len, len(highs) - pivot_len):
        is_high = all(highs[i] > highs[j]
                      for j in range(i - pivot_len, i + pivot_len + 1) if j != i)
        if is_high:
            swing_highs.append((i, highs[i]))
        is_low = all(lows[i] < lows[j]
                     for j in range(i - pivot_len, i + pivot_len + 1) if j != i)
        if is_low:
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def _aggregate_weekly(bars: List[dict]) -> List[dict]:
    """Aggregate daily OHLCV into weekly bars (Mon-Fri grouping)."""
    if not bars:
        return []
    weeks = {}
    for b in bars:
        dt = b["date"]
        if isinstance(dt, str):
            dt = datetime.strptime(dt[:10], "%Y-%m-%d")
        iso_week = dt.isocalendar()[:2]
        if iso_week not in weeks:
            weeks[iso_week] = {"date": dt, "o": b["o"], "h": b["h"],
                               "l": b["l"], "c": b["c"], "v": b["v"]}
        else:
            w = weeks[iso_week]
            w["h"] = max(w["h"], b["h"])
            w["l"] = min(w["l"], b["l"])
            w["c"] = b["c"]
            w["v"] += b["v"]
    return sorted(weeks.values(), key=lambda w: w["date"])


# ═══════════════════════════════════════════════════════════
# DAILY BAR FETCHER — Schwab primary, Yahoo fallback
# ═══════════════════════════════════════════════════════════

_yf_cache: Dict[str, tuple] = {}
_yf_cache_lock = threading.Lock()
_YF_CACHE_TTL = 14400  # 4 hours

# v7.0: Schwab daily bars callback — set by app.py at startup
_schwab_daily_bars_fn = None   # callable(ticker, days) -> list[dict] or None


def set_daily_bars_fn(fn):
    """Wire Schwab daily bars from app.py. Called once at startup."""
    global _schwab_daily_bars_fn
    _schwab_daily_bars_fn = fn
    log.info("Swing scanner: Schwab daily bars wired")


def fetch_daily_bars(ticker: str, days: int = 120) -> List[dict]:
    """
    v7.2.1: Fetch daily OHLCV via Schwab (sole provider).
    Returns list of dicts: [{date, o, h, l, c, v}, ...]
    Cached for 4 hours. Yahoo fallback removed — Schwab WebSocket
    handles all price data without rate limits.
    """
    cache_key = f"bars:{ticker}:{days}"
    with _yf_cache_lock:
        cached = _yf_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < _YF_CACHE_TTL:
            return cached[0]

    if not _schwab_daily_bars_fn:
        log.warning(f"Daily bars requested for {ticker} but Schwab daily bars fn not wired — "
                     "call set_daily_bars_fn() at startup")
        return []

    try:
        bars = _schwab_daily_bars_fn(ticker, days)
        if bars and len(bars) >= 20:
            with _yf_cache_lock:
                _yf_cache[cache_key] = (bars, time.time())
            return bars
        elif bars is not None:
            log.warning(f"Schwab daily bars for {ticker}: only {len(bars)} bars returned "
                         f"(need ≥20, requested {days} days) — fib zones will be incomplete")
            return bars or []
        else:
            log.warning(f"Schwab daily bars returned None for {ticker} — "
                         "no price history available for fib/swing analysis")
            return []
    except Exception as e:
        log.warning(f"Schwab daily bars failed for {ticker}: {e} — "
                     "no fallback, fib zones unavailable for this ticker")
        return []


def fetch_daily_bars_yahoo(ticker: str, days: int = 120) -> List[dict]:
    """
    Fetch daily OHLCV from Yahoo Finance. Free, unlimited.
    Returns list of dicts: [{date, o, h, l, c, v}, ...]
    Cached for 4 hours.
    """
    if not _YF_AVAILABLE:
        log.warning("yfinance not installed — pip install yfinance")
        return []

    cache_key = f"yf:{ticker}:{days}"
    with _yf_cache_lock:
        cached = _yf_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < _YF_CACHE_TTL:
            return cached[0]

    try:
        end = datetime.now()
        start = end - timedelta(days=days + 10)
        # multi_level_index=False: yfinance >=0.2.38 returns MultiIndex columns
        # by default even for single tickers, making row["Open"] a Series not a
        # scalar. This flag collapses it back to flat columns.
        try:
            df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"), progress=False,
                             auto_adjust=True, multi_level_index=False)
        except TypeError:
            # Older yfinance that doesn't support multi_level_index param
            df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"), progress=False,
                             auto_adjust=True)

        if df is None or df.empty:
            return []

        # Flatten MultiIndex columns if still present (belt-and-suspenders)
        if hasattr(df.columns, "levels"):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        bars = []
        for idx, row in df.iterrows():
            try:
                bars.append({
                    "date": idx.to_pydatetime(),
                    "o": float(row["Open"].item() if hasattr(row["Open"], "item") else row["Open"]),
                    "h": float(row["High"].item() if hasattr(row["High"], "item") else row["High"]),
                    "l": float(row["Low"].item() if hasattr(row["Low"], "item") else row["Low"]),
                    "c": float(row["Close"].item() if hasattr(row["Close"], "item") else row["Close"]),
                    "v": int(row["Volume"].item() if hasattr(row["Volume"], "item") else row["Volume"]),
                })
            except (TypeError, ValueError):
                continue  # skip malformed rows silently

        with _yf_cache_lock:
            _yf_cache[cache_key] = (bars, time.time())

        return bars
    except Exception as e:
        log.warning(f"Yahoo Finance fetch failed for {ticker}: {e}")
        return []


# ═══════════════════════════════════════════════════════════
# HOLD-HORIZON MAP (backtest-derived)
# ═══════════════════════════════════════════════════════════
#
# Based on swing backtest analysis:
#   50% fib bull: best at 20-60D, runner-eligible, income-eligible
#   78.6% fib bull: best at 10-20D, income-eligible
#   38.2% fib bull: selective 15-20D, income-eligible with vol contraction
#   61.8% fib bull: weakest, needs extra confluence
#   Bears: 1-3D tactical only

def classify_hold_horizon(direction, fib_level, weekly_bull, confidence, vol_contracting):
    """
    Classify a swing signal's hold horizon based on backtest findings.
    Returns dict with hold_class, default_hold_days, runner_eligible, income_eligible.
    """
    if direction == "bear":
        return {
            "hold_class": "tactical",
            "default_hold_days": 3,
            "max_hold_days": 5,
            "runner_eligible": False,
            "income_eligible": False,
            "t1_policy": "Take at first touch — do not wait",
            "t2_policy": "Not applicable — bears do not hold for T2",
            "hold_note": "Bear tactical: exit fast, 1-3D max",
        }

    # ── Bull setups ──
    if fib_level == "50.0":
        if weekly_bull and confidence >= 70:
            return {
                "hold_class": "long_hold",
                "default_hold_days": 30,
                "max_hold_days": 60,
                "runner_eligible": True,
                "income_eligible": True,
                "t1_policy": "Take partial at T1, hold runner for T2",
                "t2_policy": "Runner target — 42-48% hit rate by 60D",
                "hold_note": "Premium 50% fib bull — strongest backtest family (+12% at 30D, +19% at 60D)",
            }
        return {
            "hold_class": "medium_hold",
            "default_hold_days": 20,
            "max_hold_days": 40,
            "runner_eligible": True,
            "income_eligible": True,
            "t1_policy": "Take partial at T1, evaluate for runner",
            "t2_policy": "Runner only if weekly bull and constructive price action",
            "hold_note": "50% fib bull — strong hold family, needs time",
        }

    if fib_level == "78.6":
        return {
            "hold_class": "medium_hold",
            "default_hold_days": 15 if weekly_bull else 10,
            "max_hold_days": 20,
            "runner_eligible": False,
            "income_eligible": True,
            "t1_policy": "Take at T1 — this family peaks 10-20D",
            "t2_policy": "Rarely reaches T2 within the optimal hold window",
            "hold_note": "78.6% fib bull — medium-term, best 10-20D",
        }

    if fib_level == "38.2":
        if weekly_bull and vol_contracting:
            return {
                "hold_class": "medium_hold",
                "default_hold_days": 20,
                "max_hold_days": 30,
                "runner_eligible": False,
                "income_eligible": True,
                "t1_policy": "Take at T1 — these need patience but work",
                "t2_policy": "Possible but inconsistent — do not plan for it",
                "hold_note": "38.2% fib bull + vol contraction — selective hold",
            }
        return {
            "hold_class": "selective",
            "default_hold_days": 15,
            "max_hold_days": 20,
            "runner_eligible": False,
            "income_eligible": False,
            "t1_policy": "Take at T1 — do not extend without strong tape",
            "t2_policy": "Not recommended",
            "hold_note": "38.2% fib bull — needs extra confluence",
        }

    if fib_level == "61.8":
        return {
            "hold_class": "selective",
            "default_hold_days": 10,
            "max_hold_days": 15,
            "runner_eligible": False,
            "income_eligible": False,
            "t1_policy": "Take at T1 — weakest bull family",
            "t2_policy": "Do not hold for T2",
            "hold_note": "61.8% fib bull — weakest, trade smaller, require extra confluence",
        }

    # Fallback for any other fib level
    return {
        "hold_class": "standard",
        "default_hold_days": 10,
        "max_hold_days": 20,
        "runner_eligible": False,
        "income_eligible": direction == "bull",
        "t1_policy": "Take at T1",
        "t2_policy": "Evaluate based on context",
        "hold_note": "Standard setup — no specific backtest edge data",
    }


# ═══════════════════════════════════════════════════════════
# SIGNAL CACHE — recent signals accessible by income scanner
# ═══════════════════════════════════════════════════════════

import threading as _threading

_signal_cache = {}
_signal_cache_lock = _threading.Lock()
_SIGNAL_CACHE_MAX_AGE = 86400 * 7  # 7 days
_persistent_state_ref = None  # set by app.py on startup
_flow_fn_ref = None           # set by app.py for flow scoring
_potter_box_ref = None        # set by app.py for Potter Box convergence


def set_persistent_state(ps):
    """Called by app.py to inject PersistentState for Redis-backed caching."""
    global _persistent_state_ref
    _persistent_state_ref = ps


def set_flow_fn(fn):
    """Called by app.py to inject FlowDetector scoring function."""
    global _flow_fn_ref
    _flow_fn_ref = fn


def set_potter_box(pb):
    """Called by app.py to inject PotterBoxScanner for convergence scoring."""
    global _potter_box_ref
    _potter_box_ref = pb


def _cache_signal(signal):
    """Store a signal in the cache, keyed by ticker. Persists to Redis."""
    with _signal_cache_lock:
        ticker = signal["ticker"]
        _signal_cache[ticker] = {
            "signal": signal,
            "cached_at": time.time(),
        }
    # Also persist to Redis (survives redeploy)
    if _persistent_state_ref:
        try:
            _persistent_state_ref.save_swing_signal(ticker, signal)
        except Exception:
            pass


def get_recent_signals(ticker=None, max_age_days=7):
    """
    Get recent swing signals from the cache.
    If ticker is provided, returns the signal for that ticker or None.
    If ticker is None, returns dict of all cached signals.
    Used by income_scanner for fib confluence detection.
    Falls back to Redis if not in memory (post-redeploy).
    """
    cutoff = time.time() - (max_age_days * 86400)
    with _signal_cache_lock:
        if ticker:
            entry = _signal_cache.get(ticker.upper())
            if entry and entry["cached_at"] > cutoff:
                return entry["signal"]
            # Redis fallback
            if _persistent_state_ref:
                try:
                    sig = _persistent_state_ref.get_swing_signal(ticker.upper())
                    if sig:
                        # Re-populate in-memory cache
                        _signal_cache[ticker.upper()] = {
                            "signal": sig, "cached_at": time.time(),
                        }
                        return sig
                except Exception:
                    pass
            return None
        else:
            # Try Redis for full set if memory is empty (post-redeploy)
            result = {
                t: e["signal"] for t, e in _signal_cache.items()
                if e["cached_at"] > cutoff
            }
            if not result and _persistent_state_ref:
                try:
                    result = _persistent_state_ref.get_all_swing_signals()
                except Exception:
                    pass
            return result


# ═══════════════════════════════════════════════════════════
# FIBONACCI ANALYSIS
# ═══════════════════════════════════════════════════════════

def compute_fib_levels(swing_high: float, swing_low: float) -> dict:
    """Compute all fib retracement and extension levels."""
    r = abs(swing_high - swing_low)
    return {
        "bull_382": swing_high - r * 0.382,
        "bull_500": swing_high - r * 0.500,
        "bull_618": swing_high - r * 0.618,
        "bull_786": swing_high - r * 0.786,
        "bull_ext_127": swing_low + r * 1.272,
        "bull_ext_162": swing_low + r * 1.618,
        "bear_382": swing_low + r * 0.382,
        "bear_500": swing_low + r * 0.500,
        "bear_618": swing_low + r * 0.618,
        "bear_786": swing_low + r * 0.786,
        "bear_ext_127": swing_high - r * 1.272,
        "bear_ext_162": swing_high - r * 1.618,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "fib_range": r,
    }


def check_fib_touch(bar: dict, fibs: dict, touch_pct: float) -> dict:
    """Check if bar touches any fib level. Returns touch info."""
    result = {"bull_touched": False, "bear_touched": False}
    low, high, close = bar["l"], bar["h"], bar["c"]

    for name, key in [("61.8", "bull_618"), ("50.0", "bull_500"),
                      ("38.2", "bull_382"), ("78.6", "bull_786")]:
        level = fibs.get(key, 0)
        if level <= 0:
            continue
        if abs(low - level) / level <= touch_pct and close > level * (1 - touch_pct * 2):
            result.update({"bull_touched": True, "bull_fib_level": name,
                          "bull_fib_price": level,
                          "bull_fib_dist_pct": abs(close - level) / level * 100})
            break

    for name, key in [("61.8", "bear_618"), ("50.0", "bear_500"),
                      ("38.2", "bear_382"), ("78.6", "bear_786")]:
        level = fibs.get(key, 0)
        if level <= 0:
            continue
        if abs(high - level) / level <= touch_pct and close < level * (1 + touch_pct * 2):
            result.update({"bear_touched": True, "bear_fib_level": name,
                          "bear_fib_price": level,
                          "bear_fib_dist_pct": abs(close - level) / level * 100})
            break

    return result


def count_fib_touches(bars: List[dict], fibs: dict, touch_pct: float,
                      lookback: int = 10) -> dict:
    """Count how many times each fib level was touched in recent bars.
    Multi-touch fibs score higher (institutional confirmation)."""
    touches = {"bull": {}, "bear": {}}
    recent = bars[-lookback:] if len(bars) >= lookback else bars

    for bar in recent:
        t = check_fib_touch(bar, fibs, touch_pct)
        if t.get("bull_touched"):
            key = t["bull_fib_level"]
            touches["bull"][key] = touches["bull"].get(key, 0) + 1
        if t.get("bear_touched"):
            key = t["bear_fib_level"]
            touches["bear"][key] = touches["bear"].get(key, 0) + 1

    return touches


# ═══════════════════════════════════════════════════════════
# CANDLE QUALITY (Pine v3.0 translation)
# ═══════════════════════════════════════════════════════════

def candle_quality(bar: dict) -> dict:
    """Assess candle quality for fib touch confirmation."""
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    r = h - l
    if r <= 0:
        return {"bull_strong": False, "bear_strong": False,
                "bull_soft": False, "bear_soft": False}

    bull_wick = (min(o, c) - l) / r * 100
    bull_close = (c - l) / r * 100
    bull_rev = c >= o or bull_close >= (100 - SWING_CLOSE_ZONE_PCT)
    bull_ok = bull_wick >= SWING_WICK_MIN_PCT and bull_close >= (100 - SWING_CLOSE_ZONE_PCT)

    bear_wick = (h - max(o, c)) / r * 100
    bear_close = (h - c) / r * 100
    bear_rev = c <= o or bear_close >= (100 - SWING_CLOSE_ZONE_PCT)
    bear_ok = bear_wick >= SWING_WICK_MIN_PCT and bear_close >= (100 - SWING_CLOSE_ZONE_PCT)

    return {
        "bull_strong": bull_rev and bull_ok,
        "bull_soft": bull_rev or bull_close >= 55,
        "bear_strong": bear_rev and bear_ok,
        "bear_soft": bear_rev or bear_close >= 55,
        "bull_wick_pct": round(bull_wick, 1),
        "bull_close_pct": round(bull_close, 1),
        "bear_wick_pct": round(bear_wick, 1),
        "bear_close_pct": round(bear_close, 1),
    }


# ═══════════════════════════════════════════════════════════
# RELATIVE STRENGTH vs SPY
# ═══════════════════════════════════════════════════════════

_spy_cache = {"bars": None, "ts": 0}


def compute_relative_strength(ticker_bars: List[dict], spy_bars: List[dict],
                               lookback: int = 20) -> float:
    """
    Relative strength: ticker % change vs SPY % change over lookback days.
    Returns the difference: positive = outperforming SPY.
    """
    if len(ticker_bars) < lookback + 1 or len(spy_bars) < lookback + 1:
        return 0.0

    t_now = ticker_bars[-1]["c"]
    t_then = ticker_bars[-(lookback + 1)]["c"]
    s_now = spy_bars[-1]["c"]
    s_then = spy_bars[-(lookback + 1)]["c"]

    if t_then <= 0 or s_then <= 0:
        return 0.0

    ticker_pct = (t_now - t_then) / t_then * 100
    spy_pct = (s_now - s_then) / s_then * 100

    return round(ticker_pct - spy_pct, 2)


# ═══════════════════════════════════════════════════════════
# CORE ANALYSIS (one ticker)
# ═══════════════════════════════════════════════════════════

def analyze_swing_setup(
    ticker: str,
    daily_bars: List[dict],
    spy_bars: List[dict] = None,
    vix: float = 20.0,
    earnings_dates: List[str] = None,
) -> Optional[dict]:
    """
    Full swing signal analysis for one ticker.

    Returns signal dict if T1 or T2 setup detected, None otherwise.
    Includes all institutional enrichment.
    """
    if not daily_bars or len(daily_bars) < 60:
        return None

    closes = [b["c"] for b in daily_bars]
    highs = [b["h"] for b in daily_bars]
    lows = [b["l"] for b in daily_bars]
    volumes = [b.get("v", 0) for b in daily_bars]
    n = len(daily_bars)
    bar = daily_bars[-1]  # latest bar
    spot = bar["c"]

    # ── ADTV liquidity gate (real 20-day average daily dollar volume) ──
    if len(daily_bars) >= 20:
        adtv = sum(b["v"] * b["c"] for b in daily_bars[-20:]) / 20
        if adtv < 5_000_000 and ticker not in ("SPY", "QQQ", "IWM", "DIA"):
            log.debug(f"Swing scanner {ticker}: ADTV ${adtv/1e6:.1f}M < $5M, skipping")
            return None

    # ── Weekly trend (aggregated from daily) ──
    weekly_bars = _aggregate_weekly(daily_bars)
    if len(weekly_bars) < SWING_WEEKLY_EMA_SLOW + 2:
        return None

    w_closes = [w["c"] for w in weekly_bars]
    w_ema_f = _ema(w_closes, SWING_WEEKLY_EMA_FAST)
    w_ema_s = _ema(w_closes, SWING_WEEKLY_EMA_SLOW)

    wef, wes = w_ema_f[-1], w_ema_s[-1]
    wef_prev = w_ema_f[-2] if len(w_ema_f) >= 2 else wef
    wes_prev = w_ema_s[-2] if len(w_ema_s) >= 2 else wes

    w_gap = abs(wef - wes)
    w_gap_prev = abs(wef_prev - wes_prev)
    w_min_sep = w_closes[-1] * (SWING_WEEKLY_MIN_SEP_PCT / 100)
    w_separated = w_gap >= w_min_sep

    weekly_bull = wef > wes and w_separated
    weekly_bear = wef < wes and w_separated
    weekly_bull_loose = wef > wes
    weekly_bear_loose = wef < wes
    weekly_converging = (weekly_bull_loose or weekly_bear_loose) and w_gap < w_gap_prev

    weekly_bull_ok = weekly_bull or (weekly_bull_loose and weekly_converging)
    weekly_bear_ok = weekly_bear or (weekly_bear_loose and weekly_converging)

    # ── Daily trend ──
    d_ema_f = _ema(closes, SWING_DAILY_EMA_FAST)
    d_ema_s = _ema(closes, SWING_DAILY_EMA_SLOW)
    if not d_ema_f or not d_ema_s:
        return None

    daily_bull = d_ema_f[-1] > d_ema_s[-1]
    daily_bear = d_ema_f[-1] < d_ema_s[-1]
    d_gap = abs(d_ema_f[-1] - d_ema_s[-1])
    d_gap_prev = abs(d_ema_f[-2] - d_ema_s[-2]) if len(d_ema_f) >= 2 else d_gap
    daily_confirmed_bull = daily_bull and d_gap >= d_gap_prev
    daily_confirmed_bear = daily_bear and d_gap >= d_gap_prev
    daily_converging = d_gap < d_gap_prev

    bull_trend_ok = daily_bull or daily_converging
    bear_trend_ok = daily_bear or daily_converging

    # ── Primary trend (50/200 SMA) ──
    primary_trend = "neutral"
    if SWING_PRIMARY_TREND_ENABLED and len(closes) >= SWING_PRIMARY_TREND_LMA:
        sma50 = _sma(closes, SWING_PRIMARY_TREND_SMA)
        sma200 = _sma(closes, SWING_PRIMARY_TREND_LMA)
        if sma50 and sma200 and sma50[-1] > 0 and sma200[-1] > 0:
            if sma50[-1] > sma200[-1]:
                primary_trend = "bullish"
            else:
                primary_trend = "bearish"

    # ── RSI ──
    rsi_vals = _rsi(closes, SWING_RSI_LENGTH)
    rsi_val = rsi_vals[-1] if rsi_vals else 50
    rsi_bull = rsi_val <= SWING_RSI_OVERSOLD
    rsi_bear = rsi_val >= SWING_RSI_OVERBOUGHT

    # ── Volume ──
    vol_sma = _sma(volumes, SWING_VOL_MA_LENGTH)
    vol_ma_val = vol_sma[-1] if vol_sma else 0
    vol_contracting = volumes[-1] < vol_ma_val * SWING_VOL_CONTRACT_MULT if vol_ma_val > 0 else False
    vol_expanding = volumes[-1] > vol_ma_val * SWING_VOL_EXPAND_MULT if vol_ma_val > 0 else False

    # ── ATR ──
    atr_vals = _atr(highs, lows, closes, SWING_ATR_LENGTH)
    atr_val = atr_vals[-1] if atr_vals else 0

    # ── Swing pivots + Fibs ──
    pivot_len = max(2, round(SWING_FIB_LOOKBACK / 5))
    swing_highs, swing_lows = _find_pivots(highs, lows, pivot_len)

    if not swing_highs or not swing_lows:
        return None

    last_sh = swing_highs[-1][1]
    last_sl = swing_lows[-1][1]

    fibs = compute_fib_levels(last_sh, last_sl)

    # ── Fib touch detection ──
    touch_pct = SWING_FIB_TOUCH_ZONE_PCT / 100
    touch = check_fib_touch(bar, fibs, touch_pct)

    if not touch["bull_touched"] and not touch["bear_touched"]:
        return None  # no fib touch on latest bar

    # ── Multi-touch scoring ──
    multi_touches = count_fib_touches(daily_bars, fibs, touch_pct, lookback=10)

    # ── Candle quality ──
    cq = candle_quality(bar)

    # ── Build signal ──
    bull_fib_touched = touch.get("bull_touched", False)
    bear_fib_touched = touch.get("bear_touched", False)
    fib_level = touch.get("bull_fib_level") or touch.get("bear_fib_level", "")
    fib_price = touch.get("bull_fib_price") or touch.get("bear_fib_price", 0)
    fib_dist = touch.get("bull_fib_dist_pct") or touch.get("bear_fib_dist_pct", 0)

    # Pine v3.0 tier logic (exact translation)
    strong_bull = cq["bull_strong"] and bull_fib_touched
    soft_bull = cq["bull_soft"] and bull_fib_touched
    strong_bear = cq["bear_strong"] and bear_fib_touched
    soft_bear = cq["bear_soft"] and bear_fib_touched

    t1_extras_bull = vol_contracting or rsi_bull
    t1_extras_bear = vol_contracting or rsi_bear

    tier1_bull = (strong_bull
                  and fib_level in ("61.8", "50.0")
                  and weekly_bull_ok
                  and (daily_confirmed_bull or daily_converging)
                  and t1_extras_bull)
    tier2_bull = (soft_bull
                  and weekly_bull_ok
                  and bull_trend_ok
                  and not tier1_bull)

    tier1_bear = (strong_bear
                  and fib_level in ("61.8", "50.0")
                  and weekly_bear_ok
                  and (daily_confirmed_bear or daily_converging)
                  and t1_extras_bear)
    tier2_bear = (soft_bear
                  and weekly_bear_ok
                  and bear_trend_ok
                  and not tier1_bear)

    if not (tier1_bull or tier2_bull or tier1_bear or tier2_bear):
        return None

    # Determine direction and tier
    if tier1_bull:
        direction, tier = "bull", 1
    elif tier1_bear:
        direction, tier = "bear", 1
    elif tier2_bull:
        direction, tier = "bull", 2
    elif tier2_bear:
        direction, tier = "bear", 2
    else:
        return None

    # ═══ INSTITUTIONAL FILTERS ═══

    rejection_reasons = []

    # ── Relative strength vs SPY ──
    rs_vs_spy = 0.0
    if spy_bars:
        rs_vs_spy = compute_relative_strength(daily_bars, spy_bars, SWING_RS_LOOKBACK_DAYS)
        if direction == "bull" and rs_vs_spy < SWING_RS_REJECT_LONG_BELOW:
            rejection_reasons.append(f"RS too weak for long ({rs_vs_spy:+.1f}% vs SPY)")
        if direction == "bear" and rs_vs_spy > SWING_RS_REJECT_SHORT_ABOVE:
            rejection_reasons.append(f"RS too strong for short ({rs_vs_spy:+.1f}% vs SPY)")

    # ── Primary trend filter ──
    if SWING_PRIMARY_TREND_ENABLED:
        if direction == "bull" and primary_trend == "bearish":
            # Don't reject outright, but demote T1 → T2
            if tier == 1:
                tier = 2
                rejection_reasons.append("Demoted T1→T2: 50 SMA < 200 SMA (death cross)")
        if direction == "bear" and primary_trend == "bullish" and rs_vs_spy > 2.0:
            rejection_reasons.append(f"Rejected: shorting in golden cross + strong RS ({rs_vs_spy:+.1f}%)")

    # ── Earnings check ──
    if earnings_dates:
        from datetime import date
        today = date.today()
        for ed_str in earnings_dates:
            try:
                ed = datetime.strptime(ed_str[:10], "%Y-%m-%d").date()
                days_to_earnings = (ed - today).days
                if 0 < days_to_earnings <= 60:  # within DTE window
                    rejection_reasons.append(f"Earnings in {days_to_earnings} days ({ed_str})")
                    break
            except ValueError:
                continue

    # Check for hard rejections (RS filter can be hard)
    hard_rejected = any("Rejected:" in r for r in rejection_reasons)
    if hard_rejected:
        log.info(f"Swing scanner {ticker}: REJECTED — {'; '.join(rejection_reasons)}")
        return None

    # ── Multi-touch bonus ──
    touch_count = 0
    if direction == "bull" and fib_level in multi_touches.get("bull", {}):
        touch_count = multi_touches["bull"][fib_level]
    elif direction == "bear" and fib_level in multi_touches.get("bear", {}):
        touch_count = multi_touches["bear"][fib_level]

    # ── Fib extensions as targets ──
    if direction == "bull":
        fib_target_1 = fibs.get("bull_ext_127", 0)
        fib_target_2 = fibs.get("bull_ext_162", 0)
    else:
        fib_target_1 = fibs.get("bear_ext_127", 0)
        fib_target_2 = fibs.get("bear_ext_162", 0)

    # ── Confidence scoring ──
    confidence = 50
    conf_reasons = []
    warnings = []

    if tier == 1:
        confidence += 15
        conf_reasons.append("T1 signal (+15)")
    else:
        confidence += 5
        conf_reasons.append("T2 signal (+5)")

    if fib_level == "50.0":
        confidence += 12
        conf_reasons.append("50% retracement — strongest backtest family (+12)")
    elif fib_level == "78.6":
        confidence += 8
        conf_reasons.append("78.6% deep retracement (+8)")
    elif fib_level == "61.8":
        confidence += 6
        conf_reasons.append("Golden ratio 61.8% (+6)")
    elif fib_level == "38.2":
        confidence += 5
        conf_reasons.append("38.2% shallow pullback (+5)")

    if touch_count >= 3:
        confidence += 8
        conf_reasons.append(f"Multi-touch: {touch_count}x (+8)")
    elif touch_count >= 2:
        confidence += 4
        conf_reasons.append(f"Double-touch: {touch_count}x (+4)")

    if weekly_bull and direction == "bull":
        confidence += 5
        conf_reasons.append("Weekly bull confirmed (+5)")
    elif weekly_bear and direction == "bear":
        confidence += 5
        conf_reasons.append("Weekly bear confirmed (+5)")

    if vol_contracting:
        confidence += 3
        conf_reasons.append("Volume contracting on pullback (+3)")

    if abs(rs_vs_spy) > 2:
        if (direction == "bull" and rs_vs_spy > 2) or (direction == "bear" and rs_vs_spy < -2):
            confidence += 5
            conf_reasons.append(f"Strong RS alignment ({rs_vs_spy:+.1f}%) (+5)")

    if primary_trend == "bullish" and direction == "bull":
        confidence += 5
        conf_reasons.append("Above 200 SMA (+5)")
    elif primary_trend == "bearish" and direction == "bear":
        confidence += 5
        conf_reasons.append("Below 200 SMA (+5)")

    # RSI sweet spot (backtest-derived: 45-60 for bulls = constructive pullback)
    if direction == "bull":
        if 45 <= rsi_val <= 60:
            confidence += 5
            conf_reasons.append(f"RSI {rsi_val:.0f} in pullback sweet spot 45–60 (+5)")
        elif 60 < rsi_val <= 70:
            confidence += 2
            conf_reasons.append(f"RSI {rsi_val:.0f} slightly warm (+2)")
        elif rsi_val > 70:
            confidence -= 3
            conf_reasons.append(f"RSI {rsi_val:.0f} already extended — late chase risk (−3)")
            warnings.append(f"RSI {rsi_val:.0f} extended")
        elif 35 <= rsi_val < 45:
            confidence += 1
            conf_reasons.append(f"RSI {rsi_val:.0f} weak but not broken (+1)")
        elif rsi_val < 35:
            confidence -= 2
            conf_reasons.append(f"RSI {rsi_val:.0f} deeply weak — needs strong support (−2)")
            warnings.append(f"RSI {rsi_val:.0f} oversold")
    else:  # bear
        if 55 <= rsi_val <= 70:
            confidence += 3
            conf_reasons.append(f"RSI {rsi_val:.0f} in bear fade zone (+3)")
        elif rsi_val > 75:
            confidence += 5
            conf_reasons.append(f"RSI {rsi_val:.0f} overbought — ripe for fade (+5)")

    # ── Institutional flow scoring ──
    if _flow_fn_ref:
        try:
            flow_result = _flow_fn_ref(ticker, fib_price, direction)
            flow_adj = flow_result.get("score_adj", 0)
            if flow_adj != 0:
                confidence += flow_adj
                for fr in flow_result.get("reasons", []):
                    conf_reasons.append(fr)
        except Exception:
            pass

    # ── Potter Box convergence ──
    # A swing signal at a fib level that aligns with a Potter Box boundary
    # is structurally stronger — two independent systems agree on the level.
    if _potter_box_ref:
        try:
            pb = _potter_box_ref.get_active_box(ticker)
            if pb and pb.get("floor") and pb.get("roof"):
                floor = pb["floor"]
                roof = pb["roof"]
                cb = (floor + roof) / 2  # cost basis / 50% line
                spot_price = last_close
                tolerance = spot_price * 0.015  # 1.5% tolerance

                # Check if fib level aligns with box boundaries
                if abs(fib_price - floor) < tolerance:
                    confidence += 8
                    conf_reasons.append(f"Potter Box floor ${floor:.0f} aligns with fib {fib_level} (+8)")
                elif abs(fib_price - roof) < tolerance:
                    confidence += 8
                    conf_reasons.append(f"Potter Box roof ${roof:.0f} aligns with fib {fib_level} (+8)")
                elif abs(fib_price - cb) < tolerance:
                    confidence += 5
                    conf_reasons.append(f"Potter Box CB line ${cb:.0f} aligns with fib {fib_level} (+5)")
                # Check if price is inside the box (consolidation context)
                elif floor <= spot_price <= roof:
                    confidence += 3
                    conf_reasons.append(f"Inside Potter Box ${floor:.0f}–${roof:.0f} (+3)")
        except Exception:
            pass

    # Merge rejection reasons into warnings, deduplicate
    for r in rejection_reasons:
        if r not in warnings:
            warnings.append(r)

    signal = {
        "ticker": ticker,
        "direction": direction,
        "tier": tier,
        "fib_level": fib_level,
        "fib_price": round(fib_price, 2),
        "fib_dist_pct": round(fib_dist, 2),
        "fib_range": round(fibs["fib_range"], 2),
        "swing_high": round(fibs["swing_high"], 2),
        "swing_low": round(fibs["swing_low"], 2),
        "fib_target_1": round(fib_target_1, 2),
        "fib_target_2": round(fib_target_2, 2),
        "spot": round(spot, 2),
        "confidence": min(confidence, 100),
        "conf_reasons": conf_reasons,
        # Trend context
        "weekly_bull": weekly_bull,
        "weekly_bear": weekly_bear,
        "weekly_bull_loose": weekly_bull_loose,
        "weekly_bear_loose": weekly_bear_loose,
        "weekly_converging": weekly_converging,
        "daily_bull": daily_bull,
        "daily_bear": daily_bear,
        "htf_confirmed": daily_confirmed_bull if direction == "bull" else daily_confirmed_bear,
        "htf_converging": daily_converging,
        "primary_trend": primary_trend,
        # Momentum
        "rsi": round(rsi_val, 1),
        "rsi_bull": rsi_bull,
        "vol_contracting": vol_contracting,
        "vol_expanding": vol_expanding,
        "volume": volumes[-1],
        "vol_ma": round(vol_ma_val, 0),
        "atr": round(atr_val, 2),
        # Candle quality
        "candle_quality": cq,
        "touch_count": touch_count,
        # Relative strength
        "rs_vs_spy": rs_vs_spy,
        # Metadata
        "warnings": warnings,
        "scan_time": datetime.now().isoformat(),
        "source": "swing_scanner",
        "type": "swing",
    }

    # ── Hold-horizon classification (backtest-derived) ──
    horizon = classify_hold_horizon(
        direction=direction, fib_level=fib_level,
        weekly_bull=weekly_bull, confidence=confidence,
        vol_contracting=vol_contracting,
    )
    signal["hold_class"] = horizon["hold_class"]
    signal["default_hold_days"] = horizon["default_hold_days"]
    signal["max_hold_days"] = horizon["max_hold_days"]
    signal["runner_eligible"] = horizon["runner_eligible"]
    signal["income_eligible"] = horizon["income_eligible"]
    signal["t1_policy"] = horizon["t1_policy"]
    signal["t2_policy"] = horizon["t2_policy"]
    signal["hold_note"] = horizon["hold_note"]

    # ── Setup quality classification (backtest-derived) ──
    # Identifies premium setups that deserve special attention
    rsi_sweet = 45 <= rsi_val <= 60
    conf_final = min(confidence, 100)

    if direction == "bear":
        signal["setup_quality"] = "TACTICAL"
        signal["setup_label"] = "Bear tactical — 1-3D, strict exits"
    elif (fib_level == "50.0" and weekly_bull and rsi_sweet
          and vol_contracting and conf_final >= 70):
        signal["setup_quality"] = "FLAGSHIP"
        signal["setup_label"] = (
            "⭐ FLAGSHIP: 50% fib + weekly bull + RSI pullback + vol contraction — "
            "strongest backtest edge (+12% at 30D, +19% at 60D)"
        )
    elif fib_level == "50.0" and weekly_bull and conf_final >= 70:
        signal["setup_quality"] = "PREMIUM"
        signal["setup_label"] = "🔷 PREMIUM: 50% fib + weekly bull — strong hold candidate"
    elif fib_level in ("78.6", "38.2") and weekly_bull and conf_final >= 65:
        signal["setup_quality"] = "STRONG"
        signal["setup_label"] = f"🔹 STRONG: {fib_level}% fib + weekly bull — medium hold"
    elif fib_level == "61.8":
        signal["setup_quality"] = "SELECTIVE"
        signal["setup_label"] = "⚪ SELECTIVE: 61.8% fib — weakest family, trade smaller"
    else:
        signal["setup_quality"] = "STANDARD"
        signal["setup_label"] = "📊 STANDARD: valid signal, standard management"

    log.info(f"Swing scanner {ticker}: T{tier} {direction.upper()} at fib {fib_level}% "
             f"(conf={conf_final}, RS={rs_vs_spy:+.1f}%, "
             f"primary={primary_trend}, touches={touch_count}, "
             f"hold={horizon['hold_class']} {horizon['default_hold_days']}D, "
             f"quality={signal['setup_quality']})")

    # Cache signal for income scanner access
    _cache_signal(signal)

    return signal


# ═══════════════════════════════════════════════════════════
# SCANNER CLASS
# ═══════════════════════════════════════════════════════════

class SwingScanner:
    """
    Runs swing analysis across the full watchlist.
    Scheduled 2x daily. Uses Yahoo Finance for data.
    """

    def __init__(
        self,
        enqueue_fn: Callable = None,
        post_fn: Callable = None,
        earnings_fn: Callable = None,
        vix_fn: Callable = None,
    ):
        """
        enqueue_fn: function(job_type, ticker, bias, webhook_data, signal_msg)
        post_fn:    function(message) — post to Telegram
        earnings_fn: function(ticker) -> {"has_earnings": bool, "next_date": str}
        vix_fn:     function() -> float
        """
        self._enqueue = enqueue_fn
        self._post = post_fn
        self._earnings_fn = earnings_fn
        self._vix_fn = vix_fn
        self._running = False
        self._thread = None
        self._last_scan = {}  # ticker -> last signal time
        self._signal_cooldown = {}  # ticker:direction -> timestamp

    def start(self):
        if not SWING_SCANNER_ENABLED:
            log.info("Swing scanner disabled")
            return
        if not _schwab_daily_bars_fn and not _YF_AVAILABLE:
            log.error("Swing scanner requires Schwab daily bars or yfinance fallback")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="swing-scanner")
        self._thread.start()
        log.info(f"Swing scanner started: {len(SWING_WATCHLIST)} tickers")

    def stop(self):
        self._running = False

    def scan_all(self) -> List[dict]:
        """Run a full scan of the watchlist. Returns list of signals."""
        vix = self._vix_fn() if self._vix_fn else 20.0

        # Fetch SPY bars first (needed for relative strength)
        spy_bars = fetch_daily_bars("SPY", SWING_SCAN_LOOKBACK_DAYS)
        if not spy_bars:
            log.warning("Swing scanner: failed to fetch SPY bars, RS disabled")

        all_signals = []
        no_data = 0
        no_signal = 0

        for ticker in sorted(SWING_WATCHLIST):
            try:
                bars = fetch_daily_bars(ticker, SWING_SCAN_LOOKBACK_DAYS)
                if not bars or len(bars) < 60:
                    no_data += 1
                    continue

                # Earnings check
                earnings_dates = []
                if self._earnings_fn:
                    try:
                        ed = self._earnings_fn(ticker)
                        if ed and ed.get("next_date"):
                            earnings_dates = [ed["next_date"]]
                    except Exception:
                        pass

                signal = analyze_swing_setup(
                    ticker=ticker,
                    daily_bars=bars,
                    spy_bars=spy_bars,
                    vix=vix,
                    earnings_dates=earnings_dates,
                )

                if signal:
                    # Cooldown: don't re-signal same ticker+direction within 24h
                    cd_key = f"{ticker}:{signal['direction']}"
                    last = self._signal_cooldown.get(cd_key, 0)
                    if time.time() - last < 86400:
                        log.debug(f"Swing scanner {ticker}: {signal['direction']} cooldown active")
                        continue
                    self._signal_cooldown[cd_key] = time.time()
                    all_signals.append(signal)
                else:
                    no_signal += 1

            except Exception as e:
                log.warning(f"Swing scanner error for {ticker}: {e}")

        log.info(f"Swing scan complete: {len(all_signals)} signals, "
                 f"{no_signal} no-setup, {no_data} no-data "
                 f"({len(SWING_WATCHLIST)} tickers)")

        # ── Correlation grouping: max per sector ──
        if all_signals:
            all_signals = self._apply_correlation_filter(all_signals)

        return all_signals

    def _apply_correlation_filter(self, signals: List[dict]) -> List[dict]:
        """Limit signals to max N per sector to avoid correlated bets."""
        sector_counts = {}
        filtered = []

        # Sort by confidence descending — keep highest conviction per sector
        signals.sort(key=lambda s: s.get("confidence", 0), reverse=True)

        for sig in signals:
            ticker = sig["ticker"]
            sector = self._get_sector(ticker)

            count = sector_counts.get(sector, 0)
            if count >= SWING_MAX_PER_SECTOR:
                log.info(f"Swing scanner: {ticker} filtered (sector {sector} "
                         f"already has {count} signals)")
                continue

            sector_counts[sector] = count + 1
            filtered.append(sig)

        if len(filtered) < len(signals):
            log.info(f"Correlation filter: {len(signals)} → {len(filtered)} signals "
                     f"(max {SWING_MAX_PER_SECTOR} per sector)")

        return filtered

    def _get_sector(self, ticker: str) -> str:
        """Map ticker to sector for correlation grouping."""
        t = ticker.upper()
        for sector, tickers in SWING_SECTOR_MAP.items():
            if t in tickers:
                return sector
        return "OTHER"

    def _fire_signals(self, signals: List[dict]):
        """Enqueue signals into the swing engine and post summary."""
        if not signals:
            return

        for sig in signals:
            ticker = sig["ticker"]
            direction = sig["direction"]
            tier = sig["tier"]

            webhook_data = {
                "bias": direction,
                "tier": str(tier),
                "type": "swing",
                "source": "swing_scanner",
                "fib_level": sig["fib_level"],
                "fib_distance_pct": sig["fib_dist_pct"],
                "fib_high": sig["swing_high"],
                "fib_low": sig["swing_low"],
                "fib_range": sig["fib_range"],
                "weekly_bull": sig["weekly_bull"],
                "weekly_bear": sig["weekly_bear"],
                "htf_confirmed": sig["htf_confirmed"],
                "htf_converging": sig["htf_converging"],
                "daily_bull": sig["daily_bull"],
                "daily_bear": sig["daily_bear"],
                "rsi": sig["rsi"],
                "vol_contracting": sig["vol_contracting"],
                "vol_expanding": sig["vol_expanding"],
                "volume": sig["volume"],
                "vol_ma": sig["vol_ma"],
                "fib_ext_127": sig["fib_target_1"],
                "fib_ext_162": sig["fib_target_2"],
                "pre_confidence": sig["confidence"],
                "rs_vs_spy": sig["rs_vs_spy"],
                "primary_trend": sig["primary_trend"],
                "atr": sig["atr"],
                "is_snapback": (direction == "bull" and sig["primary_trend"] == "bearish"),
                "vix": sig.get("vix", 20),
                # Hold-horizon (backtest-derived)
                "hold_class": sig["hold_class"],
                "default_hold_days": sig["default_hold_days"],
                "max_hold_days": sig["max_hold_days"],
                "runner_eligible": sig["runner_eligible"],
                "income_eligible": sig["income_eligible"],
            }

            webhook_data["setup_quality"] = sig.get("setup_quality", "STANDARD")

            # Hold-horizon labels for display
            hold_class = sig["hold_class"]
            hold_emoji = {"long_hold": "🏗️", "medium_hold": "📅", "selective": "⚡", "tactical": "💨", "standard": "📅"}.get(hold_class, "📅")
            runner_tag = " 🏃 runner" if sig["runner_eligible"] else ""
            income_tag = " 💰 income" if sig["income_eligible"] else ""

            # Setup quality header
            quality = sig.get("setup_quality", "STANDARD")
            quality_header = sig.get("setup_label", "")

            signal_msg = (
                f"📊 Swing Scanner: {ticker} T{tier} {direction.upper()}\n"
                f"{quality_header}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Fib {sig['fib_level']}% @ ${sig['fib_price']:.2f} "
                f"(dist {sig['fib_dist_pct']:.1f}%)\n"
                f"Conf: {sig['confidence']}/100 | RS: {sig['rs_vs_spy']:+.1f}%\n"
                f"Weekly: {'🟢' if sig['weekly_bull'] else '🔴' if sig['weekly_bear'] else '⚪'} | "
                f"Daily: {'🟢' if sig['daily_bull'] else '🔴' if sig['daily_bear'] else '⚪'} | "
                f"RSI: {sig['rsi']:.0f}\n"
                f"Primary: {sig['primary_trend']} | "
                f"Vol: {'📉 contracting' if sig['vol_contracting'] else '📈 expanding' if sig['vol_expanding'] else '➡️ normal'}\n"
                f"Targets: ${sig['fib_target_1']:.2f} / ${sig['fib_target_2']:.2f}\n"
                f"{hold_emoji} Hold: {sig['default_hold_days']}–{sig['max_hold_days']}D ({hold_class.replace('_', ' ')}){runner_tag}{income_tag}\n"
                f"T1: {sig['t1_policy']}\n"
                + (f"⚠️ {', '.join(sig['warnings'])}" if sig.get("warnings") else "")
            )

            if self._enqueue:
                self._enqueue("swing", ticker, direction, webhook_data, signal_msg)
                log.info(f"Swing signal enqueued: {ticker} T{tier} {direction.upper()} [{quality}]")

        # Post summary to Telegram
        if self._post and signals:
            summary_lines = [
                f"📊 ── SWING SCAN RESULTS ({len(signals)} setups) ──",
                "",
            ]
            for sig in signals:
                quality = sig.get("setup_quality", "STANDARD")
                q_emoji = {"FLAGSHIP": "⭐", "PREMIUM": "🔷", "STRONG": "🔹",
                           "SELECTIVE": "⚪", "TACTICAL": "💨", "STANDARD": "📊"}.get(quality, "📊")
                dir_emoji = "🟢" if sig["direction"] == "bull" else "🔴"
                hold_tag = f"{sig['default_hold_days']}D"
                extras = []
                if sig.get("runner_eligible"): extras.append("🏃")
                if sig.get("income_eligible"): extras.append("💰")
                extra_str = " " + "".join(extras) if extras else ""
                summary_lines.append(
                    f"{q_emoji}{dir_emoji} {sig['ticker']} T{sig['tier']} "
                    f"{sig['direction'].upper()} — "
                    f"Fib {sig['fib_level']}% | Conf {sig['confidence']} | "
                    f"{hold_tag}{extra_str} [{quality}]"
                )
            summary_lines.append("")
            summary_lines.append("Signals auto-enqueued for swing engine.")
            self._post("\n".join(summary_lines))

    def _loop(self):
        """Main loop — runs at scheduled times."""
        log.info("Swing scanner loop started")
        while self._running:
            try:
                from market_clock import is_weekday, _now_ct
                now = _now_ct()

                if not is_weekday(now):
                    time.sleep(300)
                    continue

                current_time = now.strftime("%H:%M")

                # Check if it's scan time (within 2 minute window)
                for scan_time in SWING_SCAN_TIMES_CT:
                    scan_key = f"scan:{now.strftime('%Y-%m-%d')}:{scan_time}"
                    if scan_key in self._last_scan:
                        continue

                    # Parse scan time
                    sh, sm = int(scan_time.split(":")[0]), int(scan_time.split(":")[1])
                    scan_minutes = sh * 60 + sm
                    now_minutes = now.hour * 60 + now.minute

                    if 0 <= (now_minutes - scan_minutes) <= 2:
                        log.info(f"Swing scanner firing: {scan_time} CT scan")
                        self._last_scan[scan_key] = time.time()
                        signals = self.scan_all()
                        self._fire_signals(signals)

                time.sleep(30)

            except Exception as e:
                log.error(f"Swing scanner loop error: {e}", exc_info=True)
                time.sleep(300)

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "watchlist_size": len(SWING_WATCHLIST),
            "scalp_tickers": len(SWING_WATCHLIST - SWING_ONLY_TICKERS),
            "swing_only_tickers": len(SWING_ONLY_TICKERS),
            "signals_today": len([k for k, v in self._signal_cooldown.items()
                                  if time.time() - v < 86400]),
            "last_scans": {k: v for k, v in self._last_scan.items()},
        }

    def force_scan(self) -> List[dict]:
        """Manual trigger — run scan immediately."""
        log.info("Swing scanner: manual scan triggered")
        signals = self.scan_all()
        self._fire_signals(signals)
        return signals
