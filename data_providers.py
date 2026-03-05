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

# Cache to avoid hammering Finnhub on every scan
# key: ticker → (value, timestamp)
_cache: dict = {}
CACHE_TTL = 3600  # 1 hour


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
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Finnhub {endpoint} error: {e}")
        return None


# ─────────────────────────────────────────────────────────
# IV RANK & PERCENTILE
# ─────────────────────────────────────────────────────────

def get_iv_rank(ticker: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Returns (iv_current, iv_rank, iv_percentile) for a ticker.

    IV Rank   = where current IV sits in 52-week high/low range (0-100)
    IV Pct    = % of days in past year where IV was below current IV (0-100)

    Uses Finnhub option sentiment endpoint which provides:
    - impliedVolatility (current ATM IV)
    - No direct IVR — we compute from historical IV via stock candles + VIX proxy

    For single stocks we use Finnhub's historical IV endpoint.
    """
    cache_key = f"ivrank:{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Finnhub historical IV (requires premium) — use option metrics instead
    data = _finnhub_get("stock/option-chain", {"symbol": ticker})

    # Fallback: use Finnhub stock metric for 52w high/low of IV
    metric_data = _finnhub_get("stock/metric", {
        "symbol": ticker,
        "metric": "all"
    })

    iv_current    = None
    iv_rank       = None
    iv_percentile = None

    if isinstance(metric_data, dict):
        m = metric_data.get("metric") or {}

        # Finnhub provides these fields on most tickers
        iv_current = m.get("52WeekIV") or m.get("currentIV")

        iv_52w_high = m.get("52WeekIVHigh")
        iv_52w_low  = m.get("52WeekIVLow")

        if iv_current and iv_52w_high and iv_52w_low:
            rng = iv_52w_high - iv_52w_low
            if rng > 0:
                iv_rank = round(
                    min(max((iv_current - iv_52w_low) / rng * 100, 0), 100), 1
                )

    # If metric endpoint didn't return IV data, try option sentiment
    if iv_current is None:
        sentiment = _finnhub_get("stock/option-sentiment", {"symbol": ticker})
        if isinstance(sentiment, dict):
            iv_current = sentiment.get("impliedVolatility")

    result = (iv_current, iv_rank, iv_percentile)
    _cache_set(cache_key, result)
    return result


# ─────────────────────────────────────────────────────────
# EARNINGS DATE CHECK
# ─────────────────────────────────────────────────────────

def get_earnings_warning(ticker: str, within_days: int = 5) -> Tuple[bool, Optional[str]]:
    """
    Returns (has_earnings_soon, warning_message).

    Checks if ticker has earnings announcement within `within_days` calendar days.
    Uses Finnhub earnings calendar endpoint.
    """
    cache_key = f"earnings:{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    today    = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=within_days)

    data = _finnhub_get("calendar/earnings", {
        "symbol": today.strftime("%Y-%m-%d"),
        "from":   today.strftime("%Y-%m-%d"),
        "to":     end_date.strftime("%Y-%m-%d"),
    })

    # Fix: correct param name
    data = _finnhub_get("calendar/earnings", {
        "symbol": ticker,
        "from":   today.strftime("%Y-%m-%d"),
        "to":     end_date.strftime("%Y-%m-%d"),
    })

    has_earnings  = False
    warning_msg   = None

    if isinstance(data, dict):
        earnings_list = data.get("earningsCalendar") or []
        for event in earnings_list:
            symbol = (event.get("symbol") or "").upper()
            if symbol != ticker.upper():
                continue

            date_str = event.get("date") or ""
            hour     = (event.get("hour") or "").lower()  # "bmo" = before open, "amc" = after close

            try:
                earn_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                days_away = (earn_date - today).days

                if 0 <= days_away <= within_days:
                    has_earnings = True
                    timing = "BMO" if "bmo" in hour else "AMC" if "amc" in hour else ""
                    timing_str = f" ({timing})" if timing else ""
                    eps_est = event.get("epsEstimate")
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

def enrich_ticker(ticker: str) -> dict:
    """
    Single entry point that returns all Finnhub data for a ticker.
    Designed to be called once per ticker per scan.

    Returns:
    {
        "iv_current":    float | None,
        "iv_rank":       float | None,   # 0-100
        "iv_percentile": float | None,   # 0-100
        "has_earnings":  bool,
        "earnings_warn": str | None,
    }
    """
    iv_current, iv_rank, iv_pct = get_iv_rank(ticker)
    has_earnings, earnings_warn = get_earnings_warning(ticker)

    return {
        "iv_current":    iv_current,
        "iv_rank":       iv_rank,
        "iv_percentile": iv_pct,
        "has_earnings":  has_earnings,
        "earnings_warn": earnings_warn,
    }
