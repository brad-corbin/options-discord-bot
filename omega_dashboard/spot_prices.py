"""
Phase 4.5+ — Spot price fetcher.

Calls Yahoo Finance's public chart endpoint for current price + previous close.
No API key required. Cached in-memory for 60 seconds to avoid hammering on
page reloads. Failures are silent — UI shows "—" for any ticker that errored.

v8.3 (Patch 1): Negative cache for failed fetches.
    Previously: a 429'd or otherwise failing ticker was not cached, so every
    subsequent /api/spot-prices request re-hit Yahoo immediately. Symptom in
    Brad's logs: ASTS getting "spot fetch failed ... 429" 8x in 28 minutes,
    and a 28-ticker burst at 15:37 all 429ing back-to-back.
    Now: 429 responses cool down for DASHBOARD_SPOT_429_COOLDOWN_SEC (default
    300s), other errors cool down for DASHBOARD_SPOT_ERR_COOLDOWN_SEC
    (default 60s). Tickers in cooldown are silently omitted from results;
    UI shows "—" as before. Successful fetch clears the negative entry.
    Rollback: set either env var to 0 to disable that flavor of neg-caching.

Returns shape: {"AAPL": {"price": 184.32, "change": 1.27, "change_pct": 0.69}}
"""

import os
import time
import logging
from typing import Dict, List, Tuple
import threading

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger(__name__)

# Positive cache: ticker → (timestamp, data)
_cache: Dict[str, Tuple[float, Dict]] = {}
# v8.3 (Patch 1): negative cache. ticker → (cooldown_until_ts, kind in {"429","err"})
_neg_cache: Dict[str, Tuple[float, str]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SEC = 60.0


# v8.3 (Patch 1): tunable backoffs. Set either env var to 0 to disable.
def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


_BACKOFF_429_SEC = _env_int("DASHBOARD_SPOT_429_COOLDOWN_SEC", 300)
_BACKOFF_ERR_SEC = _env_int("DASHBOARD_SPOT_ERR_COOLDOWN_SEC", 60)


YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _fetch_one(ticker: str) -> Tuple[Dict, str]:
    """Fetch a single ticker.

    v8.3 (Patch 1): now returns (data, status) where status in
    {"ok", "429", "err", "skip"} so the caller can apply the right backoff.
    Empty data dict on any non-ok status.
    """
    if not requests:
        return ({}, "skip")
    try:
        r = requests.get(YAHOO_URL.format(ticker), headers=HEADERS, timeout=4.0)
        # v8.3 (Patch 1): explicit 429 path — log once, longer backoff.
        if r.status_code == 429:
            log.warning(
                f"spot fetch 429 for {ticker} — cooling down {_BACKOFF_429_SEC}s"
            )
            return ({}, "429")
        r.raise_for_status()
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return ({}, "err")
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or prev is None:
            return ({}, "err")
        change = float(price) - float(prev)
        change_pct = (change / float(prev)) * 100.0 if prev else 0.0
        return (
            {
                "ticker": ticker,
                "price": round(float(price), 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "currency": meta.get("currency", "USD"),
                "fetched_at": time.time(),
            },
            "ok",
        )
    except Exception as e:
        log.warning(f"spot fetch failed for {ticker}: {e}")
        return ({}, "err")


def get_spot_prices(tickers: List[str]) -> Dict[str, Dict]:
    """Fetch spot prices for a list of tickers, with 60s in-memory cache.
    Skips empty/duplicate tickers. Returns dict keyed by ticker (uppercased).

    v8.3 (Patch 1): tickers in negative cooldown are silently skipped so we
    don't hammer Yahoo on every page refresh while throttled.
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
            # Positive cache hit?
            cached = _cache.get(t)
            if cached and (now - cached[0]) < _CACHE_TTL_SEC:
                out[t] = cached[1]
                continue
            # v8.3 (Patch 1): negative cache hit? Skip silently (debug-level only).
            neg = _neg_cache.get(t)
            if neg and now < neg[0]:
                log.debug(
                    f"spot fetch for {t} suppressed "
                    f"(cooldown {int(neg[0] - now)}s, kind={neg[1]})"
                )
                continue
            to_fetch.append(t)

    # Fetch the misses sequentially. For Brad's portfolio (~10-20 tickers),
    # this is fast enough; threading would be more code and more failure modes.
    for t in to_fetch:
        result, status = _fetch_one(t)
        if status == "ok" and result:
            out[t] = result
            with _cache_lock:
                _cache[t] = (time.time(), result)
                # v8.3 (Patch 1): clear any stale negative entry on recovery.
                _neg_cache.pop(t, None)
        elif status == "429" and _BACKOFF_429_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_429_SEC, "429")
        elif status == "err" and _BACKOFF_ERR_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_ERR_SEC, "err")
        # status == "skip" (no requests lib) → don't poison the cache

    return out
