# data_providers.py
# External data providers: Finnhub (IV rank, earnings)
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import requests

log = logging.getLogger(__name__)

FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "").strip()
FINNHUB_BASE  = "https://finnhub.io/api/v1"
MARKETDATA_TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()

# Cache to avoid hammering Finnhub on every scan
# key: ticker → (value, timestamp)
_cache: dict = {}
CACHE_TTL = 3600  # 1 hour

def as_float(val, default=None):
    try:
        if val is None:
            return default
        return float(val)
    except Exception:
        return default

def _cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.time() - ts > CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value):
    _cache[key] = (value, time.time())


def _finnhub_get(endpoint: str, params: dict) -> Optional[dict]:
    if not FINNHUB_TOKEN:
        log.warning("FINNHUB_TOKEN not set")
        return None
    try:
        params["token"] = FINNHUB_TOKEN
        r = requests.get(
            f"{FINNHUB_BASE}/{endpoint}",
            params=params,
            timeout=1.5,  # hard cap — 6 concurrent workers × 4s = 24s wasted on stall
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Finnhub {endpoint} error: {e}")
        return None


# ─────────────────────────────────────────────────────────
# IV RANK & PERCENTILE
# ─────────────────────────────────────────────────────────

def get_iv_rank_from_candles(ticker: str, iv_current: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Computes IV rank and IV percentile using 1 year of daily stock candles
    from MarketData.app to estimate historical volatility range.

    Since MarketData doesn't provide historical IV, we use a 30-day rolling
    realized volatility as a proxy for the IV history, then compare current
    ATM IV against that range.

    Returns (iv_rank, iv_percentile).
    """
    cache_key = f"ivrank_candles:{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not MARKETDATA_TOKEN:
        return None, None

    try:
        today     = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=365)).strftime("%Y-%m-%d")
        to_date   = today.strftime("%Y-%m-%d")

        r = requests.get(
            f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/",
            headers={"Authorization": f"Bearer {MARKETDATA_TOKEN}"},
            params={"from": from_date, "to": to_date},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        closes = data.get("c") or []
        if len(closes) < 30:
            return None, None

        # Compute 30-day rolling realized volatility (annualized)
        import math
        rv_series = []
        for i in range(30, len(closes)):
            window = closes[i-30:i]
            if len(window) < 2:
                continue
            returns = [math.log(window[j] / window[j-1])
                      for j in range(1, len(window))
                      if window[j-1] > 0]
            if not returns:
                continue
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)  # Bessel correction
            rv = math.sqrt(variance * 252)
            rv_series.append(rv)

        if len(rv_series) < 10:
            return None, None

        rv_min = min(rv_series)
        rv_max = max(rv_series)
        rng    = rv_max - rv_min

        if rng <= 0:
            return None, None

        # IV rank: where does current IV sit vs historical RV range
        iv_rank = round(min(max((iv_current - rv_min) / rng * 100, 0), 100), 1)

        # IV percentile: % of days where RV was below current IV
        below = sum(1 for v in rv_series if v < iv_current)
        iv_pct = round(below / len(rv_series) * 100, 1)

        # HV20 = most recent 20-day realized vol
        hv20 = rv_series[-1] if rv_series else None

        result = (iv_rank, iv_pct, hv20)
        _cache_set(cache_key, result)
        return result

    except Exception as e:
        log.warning(f"IV rank candles error for {ticker}: {e}")
        return None, None, None


# ─────────────────────────────────────────────────────────
# EARNINGS DATE CHECK
# ─────────────────────────────────────────────────────────

def get_earnings_warning(ticker: str, within_days: int = 5) -> Tuple[bool, Optional[str]]:
    cache_key = f"earnings:{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Use FMP earnings calendar instead of Finnhub
    FMP_TOKEN = os.getenv("FMP_TOKEN", "").strip()
    if not FMP_TOKEN:
        return False, None

    today    = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=within_days)

    try:
        resp = requests.get(
            "https://financialmodelingprep.com/stable/earning-calendar",
            params={
                "from": today.strftime("%Y-%m-%d"),
                "to": end_date.strftime("%Y-%m-%d"),
                "apikey": FMP_TOKEN,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        err_str = str(e)
        if FMP_TOKEN:
            err_str = err_str.replace(FMP_TOKEN, "***")
        log.warning(f"FMP earnings calendar error: {err_str}")
        _cache_set(cache_key, (False, None))
        return False, None

    has_earnings  = False
    warning_msg   = None

    if isinstance(data, list):
        for event in data:
            symbol = (event.get("symbol") or "").upper()
            if symbol != ticker.upper():
                continue

            date_str = event.get("date") or ""
            # FMP uses "bmo"/"amc" in the "time" field
            hour = (event.get("time") or "").lower()

            try:
                earn_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                days_away = (earn_date - today).days

                if 0 <= days_away <= within_days:
                    has_earnings = True
                    timing = "BMO" if "bmo" in hour else "AMC" if "amc" in hour else ""
                    timing_str = f" ({timing})" if timing else ""
                    eps_est = event.get("epsEstimated")
                    eps_str = f" | EPS est: ${eps_est:.2f}" if eps_est else ""

                    if days_away == 0:
                        day_label = "TODAY"
                    elif days_away == 1:
                        day_label = "TOMORROW"
                    else:
                        day_label = f"in {days_away} days ({date_str})"

                    warning_msg = (
                        f"🚨 EARNINGS {day_label}{timing_str}{eps_str} — "
                        f"spreads carry gap risk"
                    )
                    break
            except Exception:
                continue

    result = (has_earnings, warning_msg)
    _cache_set(cache_key, result)
    return result


# ─────────────────────────────────────────────────────────
# COMBINED ENRICHMENT  (single call from scan_ticker)
# ─────────────────────────────────────────────────────────

def get_iv_rank_from_closes(iv_current: float, closes: list) -> tuple:
    """
    Compute IV rank and percentile directly from pre-fetched closes.
    Same math as get_iv_rank_from_candles but skips the HTTP fetch.
    Returns (iv_rank, iv_percentile, hv20) or (None, None, None) on failure.
    """
    try:
        import math as _math
        if not closes or len(closes) < 32 or not iv_current or iv_current <= 0:
            return None, None, None

        rv_series = []
        for i in range(30, len(closes)):
            window = closes[i-30:i]
            if len(window) < 2:
                continue
            returns = [_math.log(window[j] / window[j-1])
                      for j in range(1, len(window))
                      if window[j-1] > 0]
            if not returns:
                continue
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)  # Bessel
            rv = _math.sqrt(variance * 252)
            rv_series.append(rv)

        if len(rv_series) < 10:
            return None, None, None

        rv_min = min(rv_series)
        rv_max = max(rv_series)
        rng    = rv_max - rv_min
        if rng <= 0:
            return None, None, None

        iv_rank = round(min(max((iv_current - rv_min) / rng * 100, 0), 100), 1)
        below   = sum(1 for v in rv_series if v < iv_current)
        iv_pct  = round(below / len(rv_series) * 100, 1)
        hv20    = rv_series[-1] if rv_series else None
        return iv_rank, iv_pct, hv20
    except Exception:
        return None, None, None


def enrich_ticker(ticker: str) -> dict:
    """
    Single entry point for all enrichment data.
    Uses MarketData.app for IV rank, Finnhub only for earnings.
    """
    # IV rank via MarketData candles (no Finnhub needed)
    iv_current = None   # will be filled from chain in app.py
    iv_rank    = None
    iv_pct     = None

    # Earnings via Finnhub (free tier supports this)
    has_earnings, earnings_warn = get_earnings_warning(ticker)

    return {
        "iv_current":    iv_current,
        "iv_rank":       iv_rank,
        "iv_percentile": iv_pct,
        "has_earnings":  has_earnings,
        "earnings_warn": earnings_warn,
    }
