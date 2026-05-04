"""
Phase 4.5+ — Spot price fetcher.

Calls Yahoo Finance's public chart endpoint for current price + previous close.
No API key required. Cached in-memory for 60 seconds to avoid hammering on
page reloads. Failures are silent — UI shows "—" for any ticker that errored.

Returns shape: {"AAPL": {"price": 184.32, "change": 1.27, "change_pct": 0.69}}
"""

import time
import logging
from typing import Dict, List
import threading

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger(__name__)

# Simple in-memory cache: ticker → (timestamp, data)
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SEC = 60.0

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _fetch_one(ticker: str) -> Dict:
    """Fetch a single ticker. Returns dict or empty dict on any error."""
    if not requests:
        return {}
    try:
        r = requests.get(YAHOO_URL.format(ticker), headers=HEADERS, timeout=4.0)
        r.raise_for_status()
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return {}
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or prev is None:
            return {}
        change = float(price) - float(prev)
        change_pct = (change / float(prev)) * 100.0 if prev else 0.0
        return {
            "ticker": ticker,
            "price": round(float(price), 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "currency": meta.get("currency", "USD"),
            "fetched_at": time.time(),
        }
    except Exception as e:
        log.warning(f"spot fetch failed for {ticker}: {e}")
        return {}


def get_spot_prices(tickers: List[str]) -> Dict[str, Dict]:
    """Fetch spot prices for a list of tickers, with 60s in-memory cache.
    Skips empty/duplicate tickers. Returns dict keyed by ticker (uppercased).
    """
    if not tickers:
        return {}

    # Normalize and dedupe
    cleaned = []
    seen = set()
    for t in tickers:
        if not t:
            continue
        u = str(t).strip().upper()
        if u and u not in seen:
            seen.add(u)
            cleaned.append(u)

    out: Dict[str, Dict] = {}
    now = time.time()
    to_fetch: List[str] = []

    with _cache_lock:
        for t in cleaned:
            cached = _cache.get(t)
            if cached and (now - cached[0]) < _CACHE_TTL_SEC:
                out[t] = cached[1]
            else:
                to_fetch.append(t)

    # Fetch the misses sequentially. For Brad's portfolio (~10-20 tickers),
    # this is fast enough; threading would be more code and more failure modes.
    for t in to_fetch:
        result = _fetch_one(t)
        if result:
            out[t] = result
            with _cache_lock:
                _cache[t] = (now, result)

    return out
