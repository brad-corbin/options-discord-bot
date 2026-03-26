# sector_rotation.py
# ═══════════════════════════════════════════════════════════════════
# Sector Relative Strength — Rotation Detection
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Tracks 11 SPDR sector ETFs and ranks them by momentum.
# Uses Yahoo Finance (free). Cached 30 min intraday, 6hr daily.
# Zero MarketData API calls.
#
# Usage:
#   from sector_rotation import get_sector_rank, get_all_sector_rankings
#   rank = get_sector_rank("NVDA")  # returns sector rank info
#   all_ranks = get_all_sector_rankings()
# ═══════════════════════════════════════════════════════════════════

import time
import logging
import threading
import math
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 1800  # 30 minutes

_YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# SPDR Sector ETFs
SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLC": "Communication Services",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary",
    "XLB": "Materials",
}

# Map tickers to sectors
TICKER_SECTOR_MAP = {
    "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLC", "META": "XLC",
    "AMZN": "XLY", "NVDA": "XLK", "AMD": "XLK", "TSLA": "XLY",
    "NFLX": "XLC", "COIN": "XLF", "CRM": "XLK", "ORCL": "XLK",
    "AVGO": "XLK", "PLTR": "XLK", "ARM": "XLK", "SMCI": "XLK",
    "MSTR": "XLK", "JPM": "XLF", "GS": "XLF", "BAC": "XLF",
    "V": "XLK", "MA": "XLK", "UNH": "XLV", "JNJ": "XLV",
    "LLY": "XLV", "PFE": "XLV", "MRNA": "XLV",
    "XOM": "XLE", "CVX": "XLE", "HD": "XLY", "WMT": "XLP",
    "COST": "XLP", "DIS": "XLC", "BA": "XLI", "CAT": "XLI",
    "RTX": "XLI", "LMT": "XLI", "GE": "XLI",
    # Index ETFs don't have sector rotation
    "SPY": None, "QQQ": None, "IWM": None, "DIA": None,
    "SPX": None, "GLD": None,
}


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        value, ts = entry
        if time.time() - ts > CACHE_TTL:
            del _cache[key]
            return None
        return value


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.time())


def _fetch_returns(symbol: str) -> Optional[Dict]:
    """Fetch 20-day and 60-day returns from Yahoo Finance."""
    try:
        resp = requests.get(
            f"{_YAHOO_BASE}/{symbol}",
            params={"interval": "1d", "range": "3mo"},
            headers=_YAHOO_HEADERS, timeout=5,
        )
        resp.raise_for_status()
        result = resp.json().get("chart", {}).get("result", [])
        if not result:
            return None

        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]

        if len(closes) < 21:
            return None

        current = closes[-1]
        ret_20d = (current / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
        ret_60d = (current / closes[0] - 1) * 100 if len(closes) >= 60 else ret_20d

        return {
            "return_20d": round(ret_20d, 2),
            "return_60d": round(ret_60d, 2),
            "current": current,
        }
    except Exception as e:
        log.debug(f"Sector returns failed for {symbol}: {e}")
        return None


def get_all_sector_rankings() -> List[Dict]:
    """
    Rank all 11 sectors by combined 20d + 60d momentum.

    Returns sorted list (best → worst):
    [
        {"etf": "XLK", "sector": "Technology", "rank": 1, "return_20d": 5.2, ...},
        ...
    ]
    """
    cached = _cache_get("sector_rankings")
    if cached is not None:
        return cached

    rankings = []
    for etf, sector in SECTOR_ETFS.items():
        ret = _fetch_returns(etf)
        if ret:
            # Composite score: 60% weight on 20d, 40% on 60d
            composite = ret["return_20d"] * 0.6 + ret["return_60d"] * 0.4
            rankings.append({
                "etf": etf,
                "sector": sector,
                "return_20d": ret["return_20d"],
                "return_60d": ret["return_60d"],
                "composite_score": round(composite, 2),
            })
        else:
            rankings.append({
                "etf": etf, "sector": sector,
                "return_20d": 0, "return_60d": 0, "composite_score": 0,
            })

    # Sort by composite score (highest = strongest sector)
    rankings.sort(key=lambda x: x["composite_score"], reverse=True)

    # Add rank
    for i, r in enumerate(rankings):
        r["rank"] = i + 1
        r["total"] = len(rankings)
        r["relative_strength"] = (
            "STRONG" if r["rank"] <= 3 else
            "NEUTRAL" if r["rank"] <= 8 else
            "WEAK"
        )

    _cache_set("sector_rankings", rankings)
    log.info(f"Sector rankings: top={rankings[0]['etf']} ({rankings[0]['composite_score']:.1f}), "
             f"bottom={rankings[-1]['etf']} ({rankings[-1]['composite_score']:.1f})")
    return rankings


def get_sector_rank(ticker: str) -> Dict:
    """
    Get the sector rank for a specific ticker.

    Returns:
        {
            "sector_etf": "XLK",
            "sector_name": "Technology",
            "rank": 2,
            "total": 11,
            "relative_strength": "STRONG",
            "return_20d": 5.2,
            "confidence_adjustment": 5,  # points to add/subtract
        }
    """
    ticker = ticker.strip().upper()
    sector_etf = TICKER_SECTOR_MAP.get(ticker)

    if sector_etf is None:
        return {
            "sector_etf": None, "sector_name": "Index/Other",
            "rank": 1, "total": 11, "relative_strength": "NEUTRAL",
            "return_20d": 0, "confidence_adjustment": 0,
        }

    rankings = get_all_sector_rankings()
    for r in rankings:
        if r["etf"] == sector_etf:
            # Confidence adjustment based on rank
            if r["rank"] <= 3:
                conf_adj = 5
            elif r["rank"] <= 6:
                conf_adj = 0
            elif r["rank"] <= 8:
                conf_adj = -3
            else:
                conf_adj = -5

            return {
                "sector_etf": sector_etf,
                "sector_name": r["sector"],
                "rank": r["rank"],
                "total": r["total"],
                "relative_strength": r["relative_strength"],
                "return_20d": r["return_20d"],
                "return_60d": r["return_60d"],
                "confidence_adjustment": conf_adj,
            }

    return {
        "sector_etf": sector_etf, "sector_name": SECTOR_ETFS.get(sector_etf, "Unknown"),
        "rank": 6, "total": 11, "relative_strength": "NEUTRAL",
        "return_20d": 0, "confidence_adjustment": 0,
    }


def format_sector_line(sr: Dict) -> str:
    """One-line summary for trade cards."""
    if not sr or not sr.get("sector_etf"):
        return ""
    emojis = {"STRONG": "🟢", "NEUTRAL": "⚪", "WEAK": "🔴"}
    emoji = emojis.get(sr.get("relative_strength", "NEUTRAL"), "⚪")
    return (f"{emoji} Sector: {sr['sector_name']} "
            f"(#{sr['rank']}/{sr['total']}, 20d {sr['return_20d']:+.1f}%)")
