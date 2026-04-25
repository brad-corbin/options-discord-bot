#!/usr/bin/env python3
"""
bt_shared.py — Shared Backtest Utilities (Omega 3000)
═══════════════════════════════════════════════════════
Common indicators, data download, regime detection, and ticker rules
used by all individual backtest engines.

Matches production code as of 2026-04-13.
"""

import os, sys, csv, json, time, math, logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
CT_OFFSET = timezone(timedelta(hours=-5))
DATA_DIR = Path(__file__).parent / "data"

# ═══════════════════════════════════════════════════════════
# SCHWAB PROVIDER (primary data source — same path as fetch_backtest_bars.py)
# ═══════════════════════════════════════════════════════════

_schwab_provider = None
_schwab_provider_checked = False


def _get_schwab_provider():
    """Lazy-init SchwabDataProvider from schwab_adapter.py.

    This intentionally uses the same proven access path as
    backtest/fetch_backtest_bars.py instead of maintaining a second
    Schwab client implementation inside bt_shared.py.
    """
    global _schwab_provider, _schwab_provider_checked
    if _schwab_provider_checked:
        return _schwab_provider

    _schwab_provider_checked = True

    try:
        from schwab_adapter import SchwabDataProvider
    except ImportError as e:
        print(f"  WARNING: cannot import SchwabDataProvider from schwab_adapter: {e}")
        return None

    try:
        provider = SchwabDataProvider()
    except Exception as e:
        print(f"  WARNING: SchwabDataProvider init raised {type(e).__name__}: {e}")
        return None

    if not getattr(provider, "available", False):
        has_key = bool(os.environ.get("SCHWAB_APP_KEY"))
        has_secret = bool(os.environ.get("SCHWAB_APP_SECRET"))
        has_token = bool(os.environ.get("SCHWAB_TOKEN_JSON")) or os.path.exists(os.environ.get("SCHWAB_TOKEN_PATH", "schwab_token.json"))
        print(
            "  WARNING: SchwabDataProvider unavailable "
            f"(SCHWAB_APP_KEY={'yes' if has_key else 'no'}, "
            f"SCHWAB_APP_SECRET={'yes' if has_secret else 'no'}, "
            f"token={'yes' if has_token else 'no'})"
        )
        return None

    _schwab_provider = provider
    print("  ✓ SchwabDataProvider initialised via schwab_adapter")
    return _schwab_provider


def _schwab_price_history(ticker, period_type, freq_type, freq, start_dt, end_dt):
    """Call Schwab get_price_history through SchwabDataProvider.

    Returns raw JSON on success or None on failure. Failures are logged with
    enough detail to know whether Schwab credentials, request shape, or API
    availability caused the MarketData fallback.
    """
    provider = _get_schwab_provider()
    if provider is None:
        return None

    try:
        return provider._schwab_get(
            "get_price_history",
            ticker.upper(),
            period_type=period_type,
            frequency_type=freq_type,
            frequency=freq,
            start_datetime=start_dt,
            end_datetime=end_dt,
            need_extended_hours_data=False,
        )
    except Exception as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        text = getattr(resp, "text", "") or ""
        status_part = f" HTTP {status}" if status else ""
        text_part = f" — {text[:200]}" if text else ""
        print(
            f"  WARNING: Schwab price_history failed for {ticker} "
            f"{start_dt.date()}→{end_dt.date()}{status_part}: "
            f"{type(e).__name__}: {e}{text_part}"
        )
        return None


def _schwab_download_daily(ticker, from_date, to_date):
    """Download daily bars from Schwab. Returns list of bar dicts."""
    try:
        from schwab.client import Client
    except ImportError as e:
        print(f"  WARNING: schwab-py import failed for daily bars: {e}")
        return None

    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, tzinfo=timezone.utc)

    raw = _schwab_price_history(
        ticker,
        period_type=Client.PriceHistory.PeriodType.YEAR,
        freq_type=Client.PriceHistory.FrequencyType.DAILY,
        freq=Client.PriceHistory.Frequency.DAILY,
        start_dt=start, end_dt=end,
    )
    if raw is None:
        return None

    candles = raw.get("candles", [])
    if not candles:
        print(f"  WARNING: Schwab daily returned zero candles for {ticker} {from_date}→{to_date}")
        return None

    bars = []
    for c in candles:
        o, h, l, cl, v = c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume", 0)
        ts_ms = c.get("datetime", 0)
        if any(x is None for x in [o, h, l, cl]):
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(CT_OFFSET)
        bars.append({
            "date": dt.strftime("%Y-%m-%d"),
            "o": float(o), "h": float(h), "l": float(l), "c": float(cl),
            "v": int(v or 0),
        })

    bars.sort(key=lambda b: b["date"])
    return bars if bars else None


def _schwab_download_5min(ticker, from_date, to_date):
    """Download 5-min bars from Schwab.

    Uses the same SchwabDataProvider path as fetch_backtest_bars.py. The
    request is chunked into 7-day windows so Schwab's DAY/MINUTE historical
    endpoint is not asked for an oversized intraday range.
    """
    try:
        from schwab.client import Client
    except ImportError as e:
        print(f"  WARNING: schwab-py import failed for 5m bars: {e}")
        return None

    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, tzinfo=timezone.utc)

    all_bars = []
    chunk_start = start
    chunk_size = timedelta(days=7)
    failed_chunks = 0
    empty_chunks = 0

    while chunk_start < end:
        chunk_end = min(chunk_start + chunk_size, end)

        raw = _schwab_price_history(
            ticker,
            period_type=Client.PriceHistory.PeriodType.DAY,
            freq_type=Client.PriceHistory.FrequencyType.MINUTE,
            freq=Client.PriceHistory.Frequency.EVERY_FIVE_MINUTES,
            start_dt=chunk_start,
            end_dt=chunk_end,
        )

        if raw is None:
            failed_chunks += 1
        elif raw.get("candles"):
            for c in raw["candles"]:
                o, h, l, cl, v = c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume", 0)
                ts_ms = c.get("datetime", 0)
                if any(x is None for x in [o, h, l, cl]):
                    continue
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(CT_OFFSET)
                all_bars.append({
                    "ts": int(dt.timestamp()),
                    "date": dt.strftime("%Y-%m-%d"),
                    "time_ct": dt.strftime("%H:%M"),
                    "o": float(o), "h": float(h), "l": float(l), "c": float(cl),
                    "v": int(v or 0),
                })
        else:
            empty_chunks += 1

        chunk_start = chunk_end + timedelta(days=1)

    if failed_chunks or empty_chunks:
        print(
            f"  Schwab 5m chunks for {ticker}: "
            f"bars={len(all_bars)}, failed_chunks={failed_chunks}, empty_chunks={empty_chunks}"
        )

    all_bars.sort(key=lambda b: b["ts"])
    return all_bars if all_bars else None


# ═══════════════════════════════════════════════════════════
# MARKETDATA.APP FALLBACK
# ═══════════════════════════════════════════════════════════

def _md_get(url, params):
    if not TOKEN:
        return None  # No token — skip silently (Schwab is primary)
    try:
        import requests
    except ImportError:
        return None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    delay = 15
    for attempt in range(1, 5):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code in (429, 403):
                wait = max(int(r.headers.get("Retry-After", delay)), delay)
                if attempt < 4:
                    print(f"  [{r.status_code}] MarketData throttled, waiting {wait}s…"); time.sleep(wait)
                    delay = min(delay * 2, 120); continue
                r.raise_for_status()
            r.raise_for_status(); return r.json()
        except Exception:
            if attempt < 4: time.sleep(delay); delay = min(delay * 2, 120)
            else: return None
    return None


def _parse_ts(ts) -> datetime:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CT_OFFSET)
    ts_str = str(ts).strip()
    try: return datetime.fromtimestamp(float(ts_str), tz=timezone.utc).astimezone(CT_OFFSET)
    except (ValueError, OSError): pass
    if len(ts_str) >= 6 and ts_str[-3] == ':' and ts_str[-6] in ('+', '-'):
        ts_str = ts_str[:-3] + ts_str[-2:]
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CT_OFFSET)
        except ValueError: continue
    try:
        dt = datetime.strptime(ts_str[:10], "%Y-%m-%d")
        return dt.replace(hour=12, tzinfo=timezone.utc).astimezone(CT_OFFSET)
    except ValueError: pass
    raise ValueError(f"Cannot parse timestamp: {ts!r}")


def _parse_date_str(ts) -> str:
    ts_str = str(ts).strip()
    if len(ts_str) == 10 and ts_str[4] == '-' and ts_str[7] == '-': return ts_str
    if len(ts_str) > 10 and ts_str[4] == '-' and ts_str[7] == '-': return ts_str[:10]
    return _parse_ts(ts).strftime("%Y-%m-%d")


def _md_download_daily(ticker, from_date, to_date):
    """Fallback: download daily bars from MarketData.app."""
    url = f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/"
    params = {"from": from_date, "to": to_date}
    data = _md_get(url, params)
    if not data or data.get("s") != "ok":
        return None
    bars = []
    for i, ts in enumerate(data.get("t", [])):
        try: d = _parse_date_str(ts)
        except ValueError: continue
        bars.append({
            "date": d, "o": data["o"][i], "h": data["h"][i],
            "l": data["l"][i], "c": data["c"][i],
            "v": data.get("v", [0]*len(data["t"]))[i],
        })
    bars = [b for b in bars if b["c"] is not None]
    bars.sort(key=lambda b: b["date"])
    return bars if bars else None


def _md_download_5min(ticker, from_date, to_date):
    """Fallback: download 5-min bars from MarketData.app."""
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker}/"
    params = {"from": from_date, "to": to_date, "dateformat": "timestamp"}
    data = _md_get(url, params)
    if not data or data.get("s") != "ok":
        return None
    bars = []
    for i, ts in enumerate(data.get("t", [])):
        try: dt_ct = _parse_ts(ts)
        except ValueError: continue
        bars.append({
            "ts": int(dt_ct.timestamp()), "date": dt_ct.strftime("%Y-%m-%d"),
            "time_ct": dt_ct.strftime("%H:%M"),
            "o": data["o"][i], "h": data["h"][i], "l": data["l"][i],
            "c": data["c"][i], "v": data.get("v", [0]*len(data["t"]))[i],
        })
    bars = [b for b in bars if b["c"] is not None]
    bars.sort(key=lambda b: b["ts"])
    return bars if bars else None


# ═══════════════════════════════════════════════════════════
# UNIFIED DOWNLOAD (Schwab → MarketData fallback → cache)
# ═══════════════════════════════════════════════════════════

def download_5min(ticker, from_date, to_date, cache=True):
    """Download 5-min bars. Tries Schwab first, then MarketData fallback."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / f"{ticker}_5m_{from_date}_{to_date}.csv"

    # 1. Disk cache
    if cache and cache_path.exists():
        bars = load_5min_csv(cache_path)
        if bars:
            print(f"  {ticker} 5m: {len(bars)} bars (cached)")
            return bars

    print(f"  Downloading 5m bars {ticker} {from_date}→{to_date}…")

    # 2. Schwab (primary — no throttle, chunks 28-day windows)
    bars = _schwab_download_5min(ticker, from_date, to_date)
    if bars:
        print(f"  → {len(bars)} bars (Schwab)")
    else:
        # 3. MarketData fallback
        print(f"  Schwab unavailable, trying MarketData…")
        bars = _md_download_5min(ticker, from_date, to_date)
        if bars:
            print(f"  → {len(bars)} bars (MarketData)")
        else:
            print(f"  WARNING: no 5m data for {ticker}")
            return []

    # Cache to disk
    if cache and bars:
        with open(cache_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(bars[0].keys()))
            w.writeheader(); w.writerows(bars)

    return bars


def download_daily(ticker, from_date, to_date, cache=True):
    """Download daily bars. Tries Schwab first, then MarketData fallback."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / f"{ticker}_daily_{from_date}_{to_date}.csv"

    # 1. Disk cache
    if cache and cache_path.exists():
        bars = load_daily_csv(cache_path)
        if bars:
            print(f"  {ticker} daily: {len(bars)} bars (cached)")
            return bars

    print(f"  Downloading daily bars {ticker} {from_date}→{to_date}…")

    # 2. Schwab (primary)
    bars = _schwab_download_daily(ticker, from_date, to_date)
    if bars:
        print(f"  → {len(bars)} daily bars (Schwab)")
    else:
        # 3. MarketData fallback
        print(f"  Schwab unavailable, trying MarketData…")
        bars = _md_download_daily(ticker, from_date, to_date)
        if bars:
            print(f"  → {len(bars)} daily bars (MarketData)")
        else:
            print(f"  WARNING: no daily data for {ticker}")
            return []

    # Cache to disk
    if cache and bars:
        with open(cache_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(bars[0].keys()))
            w.writeheader(); w.writerows(bars)

    return bars


def load_5min_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({"ts": int(r.get("ts", 0) or 0), "date": r["date"],
                "time_ct": r.get("time_ct", r.get("datetime_ct", "")[-5:] if r.get("datetime_ct") else ""),
                "o": float(r.get("o", r.get("open", 0))),
                "h": float(r.get("h", r.get("high", 0))),
                "l": float(r.get("l", r.get("low", 0))),
                "c": float(r.get("c", r.get("close", 0))),
                "v": int(float(r.get("v", r.get("volume", 0)) or 0))})
    rows.sort(key=lambda r: r["ts"] or 0)
    return rows


def load_daily_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({"date": r.get("date", ""),
                "o": float(r.get("o", r.get("open", r.get("Open", 0)))),
                "h": float(r.get("h", r.get("high", r.get("High", 0)))),
                "l": float(r.get("l", r.get("low", r.get("Low", 0)))),
                "c": float(r.get("c", r.get("close", r.get("Close", 0)))),
                "v": int(float(r.get("v", r.get("volume", r.get("Volume", 0))) or 0))})
    rows.sort(key=lambda r: r["date"])
    return rows


# ═══════════════════════════════════════════════════════════
# INDICATORS (exact match to active_scanner.py)
# ═══════════════════════════════════════════════════════════

EMA_FAST = 5; EMA_SLOW = 12
MACD_FAST = 12; MACD_SLOW = 26; MACD_SIGNAL_P = 9
RSI_PERIOD = 14; WT_CHANNEL = 10; WT_AVERAGE = 21

def ema(values: list, period: int) -> list:
    if len(values) < period: return []
    e = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        e.append(v * mult + e[-1] * (1 - mult))
    return e

def sma(values: list, period: int) -> Optional[float]:
    if len(values) < period: return None
    return sum(values[-period:]) / period

def rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1: return None
    gains = [max(0, closes[i]-closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i-1]-closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100.0 if al == 0 else 100.0 - (100.0 / (1 + ag / al))

def macd(closes: list) -> Dict:
    if len(closes) < MACD_SLOW + MACD_SIGNAL_P: return {}
    ef = ema(closes, MACD_FAST); es = ema(closes, MACD_SLOW)
    offset = MACD_SLOW - MACD_FAST
    ml = [ef[i+offset] - es[i] for i in range(len(es))]
    if len(ml) < MACD_SIGNAL_P: return {}
    sig = ema(ml, MACD_SIGNAL_P)
    return {
        "macd_hist": ml[-1] - sig[-1] if sig else 0,
        "macd_cross_bull": (len(ml)>=2 and len(sig)>=2 and ml[-2]<sig[-2] and ml[-1]>sig[-1]) if sig else False,
        "macd_cross_bear": (len(ml)>=2 and len(sig)>=2 and ml[-2]>sig[-2] and ml[-1]<sig[-1]) if sig else False,
    }

def wavetrend(hlc3: list) -> Dict:
    if len(hlc3) < WT_AVERAGE + WT_CHANNEL + 4: return {}
    esa_v = ema(hlc3, WT_CHANNEL)
    if not esa_v: return {}
    off = len(hlc3) - len(esa_v)
    d_s = [abs(hlc3[i+off] - esa_v[i]) for i in range(len(esa_v))]
    de = ema(d_s, WT_CHANNEL)
    if not de: return {}
    o2 = len(d_s) - len(de)
    ci = [(hlc3[i+off+o2] - esa_v[i+o2]) / (0.015 * de[i]) if de[i] != 0 else 0 for i in range(len(de))]
    wt1 = ema(ci, WT_AVERAGE)
    if not wt1 or len(wt1) < 4: return {}
    wt2 = ema(wt1, 4)
    if not wt2: return {}
    return {
        "wt1": wt1[-1], "wt2": wt2[-1],
        "wt_oversold": wt2[-1] < -30, "wt_overbought": wt2[-1] > 60,
        "wt_cross_bull": len(wt1)>=2 and len(wt2)>=2 and wt1[-2]<wt2[-2] and wt1[-1]>wt2[-1],
        "wt_cross_bear": len(wt1)>=2 and len(wt2)>=2 and wt1[-2]>wt2[-2] and wt1[-1]<wt2[-1],
    }

def atr(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if len(trs) < period: return None
    return sum(trs[-period:]) / period


# ═══════════════════════════════════════════════════════════
# MARKET REGIME DETECTION (exact match to market_regime.py)
# ═══════════════════════════════════════════════════════════

# Thresholds from regime_config.py
REGIME_THRESHOLDS = {"BULL_BASE": 6, "BULL_TRANSITION": 2, "CHOP": -1, "BEAR_TRANSITION": -5, "BEAR_CRISIS": -99}
V2_TO_V1 = {"BULL_BASE": "BULL", "BULL_TRANSITION": "TRANSITION", "CHOP": "TRANSITION",
            "BEAR_TRANSITION": "TRANSITION", "BEAR_CRISIS": "BEAR"}
REGIME_DEFAULTS = {
    "BULL_BASE":       {"long_min_score": 55, "short_min_score": 75, "default_hold_long": 5, "default_hold_short": 1},
    "BULL_TRANSITION": {"long_min_score": 60, "short_min_score": 80, "default_hold_long": 5, "default_hold_short": 1},
    "CHOP":            {"long_min_score": 70, "short_min_score": 70, "default_hold_long": 1, "default_hold_short": 1},
    "BEAR_TRANSITION": {"long_min_score": 72, "short_min_score": 60, "default_hold_long": 1, "default_hold_short": 3},
    "BEAR_CRISIS":     {"long_min_score": 80, "short_min_score": 55, "default_hold_long": 1, "default_hold_short": 5},
}


def _slope_positive(closes, period):
    s = sma(closes, period)
    if s is None or len(closes) < period + 5: return False
    s_prev = sum(closes[-(period+5):-(5)]) / period
    return s > s_prev


def compute_regime_score(spy_c, qqq_c, iwm_c, vix_c=None):
    """Compute core regime score from daily close arrays. Returns (score, core_regime, v1_regime, details)."""
    score = 0; d = {}

    # Trend (SPY + QQQ vs MA20/MA50)
    t = 0
    spy20 = sma(spy_c, 20); spy50 = sma(spy_c, 50)
    qqq20 = sma(qqq_c, 20); qqq50 = sma(qqq_c, 50)
    if spy20 and spy_c[-1] > spy20: t += 1
    if spy50 and spy_c[-1] > spy50: t += 1
    if _slope_positive(spy_c, 20): t += 1
    if qqq20 and qqq_c[-1] > qqq20: t += 1
    if qqq50 and qqq_c[-1] > qqq50: t += 1
    if _slope_positive(qqq_c, 20): t += 1
    if spy20 and spy_c[-1] < spy20: t -= 1
    if qqq20 and qqq_c[-1] < qqq20: t -= 1
    d["trend"] = t; score += t

    # Breadth (IWM)
    b = 0
    iwm20 = sma(iwm_c, 20); iwm50 = sma(iwm_c, 50)
    if iwm20 and iwm_c[-1] > iwm20: b += 1
    if iwm50 and iwm_c[-1] > iwm50: b += 1
    if iwm20 and iwm50 and iwm_c[-1] < iwm20 and iwm_c[-1] < iwm50: b -= 1
    d["breadth"] = b; score += b

    # Vol
    v = 0
    if vix_c and len(vix_c) >= 11:
        vn = vix_c[-1]; va = sum(vix_c[-10:])/10
        vals = vix_c[-10:]; vs = (sum((x-va)**2 for x in vals)/len(vals))**0.5
        if vn < va: v += 1
        if vs > 0 and vn > va + vs: v -= 1
        if vs > 0 and vn > va + 2*vs: v -= 1
        d["vix"] = round(vn, 1)
    d["vol"] = v; score += v

    # Classify
    if score >= REGIME_THRESHOLDS["BULL_BASE"]: core = "BULL_BASE"
    elif score >= REGIME_THRESHOLDS["BULL_TRANSITION"]: core = "BULL_TRANSITION"
    elif score >= REGIME_THRESHOLDS["CHOP"]: core = "CHOP"
    elif score >= REGIME_THRESHOLDS["BEAR_TRANSITION"]: core = "BEAR_TRANSITION"
    else: core = "BEAR_CRISIS"

    v1 = V2_TO_V1.get(core, "BEAR")
    return score, core, v1, d


def compute_regime_for_date(spy_bars, qqq_bars, iwm_bars, vix_bars, target_date):
    """Compute regime at a specific date using only bars up to that date."""
    spy_c = [b["c"] for b in spy_bars if b["date"] <= target_date]
    qqq_c = [b["c"] for b in qqq_bars if b["date"] <= target_date]
    iwm_c = [b["c"] for b in iwm_bars if b["date"] <= target_date]
    vix_c = [b["c"] for b in vix_bars if b["date"] <= target_date] if vix_bars else None
    if len(spy_c) < 55 or len(qqq_c) < 55 or len(iwm_c) < 55:
        return 0, "BEAR_CRISIS", "BEAR", {}
    return compute_regime_score(spy_c[-70:], qqq_c[-70:], iwm_c[-70:],
                                vix_c[-20:] if vix_c else None)


# ═══════════════════════════════════════════════════════════
# TICKER RULES (from ticker_rules.py — regime-specific filtering)
# ═══════════════════════════════════════════════════════════

# Simplified rule table — key rules that affect signal acceptance
# Format: TICKER_RULES[ticker][regime] = {active, bias_filter, score_min, score_max, max_hold}
TICKER_RULES_TABLE = {
    "MSFT":  {"BEAR": {"active": True, "bias": "bear", "htf": "CONFIRMED", "score_min": 60, "score_max": 79, "max_hold": 5},
              "TRANSITION": {"active": True, "bias": "bear", "htf": "CONFIRMED", "score_min": 60, "score_max": 79, "max_hold": 3},
              "BULL": {"active": True, "bias": "bear", "htf": "CONFIRMED", "score_min": 60, "score_max": 79, "max_hold": 3}},
    "IWM":   {"BEAR": {"active": True, "bias": "bear", "htf": "CONFIRMED", "score_min": 60, "score_max": 99, "max_hold": 5},
              "TRANSITION": {"active": True, "bias": "bear", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "any", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3}},
    "QQQ":   {"BEAR": {"active": True, "bias": "bear", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "TRANSITION": {"active": True, "bias": "bear", "htf": None, "score_min": 60, "score_max": 79, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 79, "max_hold": 3}},
    "SPY":   {"BEAR": {"active": True, "bias": "bear", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "TRANSITION": {"active": True, "bias": "any", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 5}},
    "META":  {"BEAR": {"active": True, "bias": "bear", "htf": "CONFIRMED", "score_min": 60, "score_max": 99, "max_hold": 5},
              "TRANSITION": {"active": False}, "BULL": {"active": True, "bias": "any", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 5}},
    "TSLA":  {"BEAR": {"active": True, "bias": "bear", "htf": "CONFIRMED", "score_min": 60, "score_max": 99, "max_hold": 3},
              "TRANSITION": {"active": True, "bias": "any", "htf": None, "score_min": 65, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "any", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 5}},
    "NVDA":  {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "AMD":   {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "bull", "htf": "CONVERGING", "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "GOOGL": {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 79, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "AVGO":  {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "bull", "htf": "CONVERGING", "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "AMZN":  {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "AAPL":  {"BEAR": {"active": True, "bias": "bear", "htf": None, "score_min": 60, "score_max": 79, "max_hold": 3},
              "TRANSITION": {"active": True, "bias": "any", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "PLTR":  {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "bull", "htf": "CONVERGING", "score_min": 65, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}},
    "COIN":  {"BEAR": {"active": False}, "TRANSITION": {"active": True, "bias": "any", "htf": None, "score_min": 65, "score_max": 99, "max_hold": 3},
              "BULL": {"active": True, "bias": "any", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 5}},
    "GLD":   {"BEAR": {"active": False}, "TRANSITION": {"active": False},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 79, "max_hold": 5}},
    "SLV":   {"BEAR": {"active": False}, "TRANSITION": {"active": False},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 79, "max_hold": 5}},
    "NFLX":  {"BEAR": {"active": False}, "TRANSITION": {"active": False},
              "BULL": {"active": True, "bias": "bull", "htf": None, "score_min": 60, "score_max": 99, "max_hold": 5}},
}

# Default rule for tickers not in the table
DEFAULT_TICKER_RULE = {"active": True, "bias": "any", "htf": None, "score_min": 55, "score_max": 99, "max_hold": 5}


def get_ticker_rule(ticker: str, regime: str) -> Dict:
    """Get the active rule for a ticker in a given regime."""
    rules = TICKER_RULES_TABLE.get(ticker.upper(), {})
    return rules.get(regime, DEFAULT_TICKER_RULE)


def is_signal_valid_for_regime(ticker, bias, score, htf_status, regime):
    """Check if a signal passes ticker_rules filtering for current regime."""
    rule = get_ticker_rule(ticker, regime)
    if not rule.get("active", True):
        return False, "ticker_inactive_in_regime"
    if rule.get("bias") not in ("any", None) and rule["bias"] != bias:
        return False, f"wrong_bias_{bias}_need_{rule['bias']}"
    if score < rule.get("score_min", 55):
        return False, f"score_{score}_below_min_{rule['score_min']}"
    if score > rule.get("score_max", 99):
        return False, f"score_{score}_above_max_{rule['score_max']}"
    htf_req = rule.get("htf")
    if htf_req and htf_status != htf_req:
        return False, f"htf_{htf_status}_need_{htf_req}"
    return True, "pass"


# ═══════════════════════════════════════════════════════════
# TIME UTILITIES
# ═══════════════════════════════════════════════════════════

MARKET_OPEN_CT = (8, 30)   # 8:30 CT
MARKET_CLOSE_CT = (15, 0)  # 15:00 CT

def is_market_bar(time_ct: str) -> bool:
    try:
        h, m = int(time_ct[:2]), int(time_ct[3:5]); tm = h*60+m
        return MARKET_OPEN_CT[0]*60+MARKET_OPEN_CT[1] <= tm < MARKET_CLOSE_CT[0]*60+MARKET_CLOSE_CT[1]
    except: return True

def time_phase(time_ct: str) -> str:
    try:
        h, m = int(time_ct[:2]), int(time_ct[3:5]); tm = h*60+m
        o = MARKET_OPEN_CT[0]*60+MARKET_OPEN_CT[1]
        return "MORNING" if tm < o+90 else "MIDDAY" if tm < o+210 else "AFTERNOON"
    except: return "UNKNOWN"


# ═══════════════════════════════════════════════════════════
# EXIT DATE HELPERS
# ═══════════════════════════════════════════════════════════

EXIT_DAYS = {"eod": 0, "1d": 1, "2d": 2, "3d": 3, "5d": 5}

def exit_dates_for(ref_date, sorted_dates):
    try: idx = sorted_dates.index(ref_date)
    except ValueError:
        idx = next((j for j, d in enumerate(sorted_dates) if d >= ref_date), None)
        if idx is None: return {k: None for k in EXIT_DAYS}
    return {label: sorted_dates[idx+n] if idx+n < len(sorted_dates) else None
            for label, n in EXIT_DAYS.items()}


# ═══════════════════════════════════════════════════════════
# STATS HELPERS
# ═══════════════════════════════════════════════════════════

def compute_win_stats(trades, col):
    valid = [t for t in trades if t.get(f"win_{col}") is not None]
    if not valid: return {"n": 0, "wr": 0.0, "avg_pct": 0.0, "pf": 0.0}
    wins = [t for t in valid if t[f"win_{col}"]]
    gw = sum(t[f"pnl_pct_{col}"] for t in wins)
    gl = sum(t[f"pnl_pct_{col}"] for t in valid if not t[f"win_{col}"])
    pf = abs(gw/gl) if gl < 0 else float("inf")
    return {"n": len(valid), "wr": len(wins)/len(valid)*100,
            "avg_pct": sum(t[f"pnl_pct_{col}"] for t in valid)/len(valid),
            "pf": round(pf, 2)}


def write_csv(path, trades):
    if not trades: return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader(); w.writerows(trades)
    print(f"  CSV → {path}")


def download_vix(from_date, to_date, cache=True):
    """Download VIX daily bars. Tries Schwab $VIX.X, then MarketData ^VIX."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / f"VIX_daily_{from_date}_{to_date}.csv"
    if cache and cache_path.exists():
        bars = load_daily_csv(cache_path)
        if bars:
            print(f"  VIX daily: {len(bars)} bars (cached)")
            return bars

    # Schwab uses $VIX.X
    bars = _schwab_download_daily("$VIX.X", from_date, to_date)
    if bars:
        print(f"  VIX: {len(bars)} bars (Schwab)")
    else:
        # MarketData uses ^VIX (URL-encoded as %5EVIX)
        data = _md_get(f"https://api.marketdata.app/v1/stocks/candles/D/%5EVIX/",
                       {"from": from_date, "to": to_date})
        if data and data.get("s") == "ok":
            bars = [{"date": _parse_date_str(data["t"][i]),
                     "o": data.get("o", [0]*len(data["t"]))[i],
                     "h": data.get("h", [0]*len(data["t"]))[i],
                     "l": data.get("l", [0]*len(data["t"]))[i],
                     "c": data["c"][i],
                     "v": 0}
                    for i in range(len(data["t"]))]
            print(f"  VIX: {len(bars)} bars (MarketData)")
        else:
            print("  VIX: unavailable")
            return []

    if cache and bars:
        with open(cache_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(bars[0].keys()))
            w.writeheader(); w.writerows(bars)
    return bars
