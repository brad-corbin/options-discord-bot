"""
omega_dashboard/spot_prices.py — dashboard spot price fetcher.

S.3 (streaming-first):
    When DASHBOARD_SPOT_USE_STREAMING=1 (default off until proven), this
    module reads spot prices from the existing Schwab WebSocket stream
    via schwab_stream.get_streaming_spot, pairs them with previous-close
    values from prev_close_store, and falls back to a one-off Schwab REST
    quote for tickers that aren't subscribed yet (cold start). Yahoo
    Finance is reserved as a last-resort fallback for tickers Schwab
    doesn't know about (unusual symbols, OTC names, etc.).

    When the env var is off (default), behavior is identical to the
    legacy v8.3 path: Yahoo Finance polling with a 60s positive cache and
    a negative cooldown cache for 429 / error responses.

    The streaming-first switch is ROLLBACK-SAFE: unset the env var, the
    old Yahoo path runs unchanged. No Redis migration, no schema change.

Returns shape (unchanged):
    {"AAPL": {"price": 184.32, "change": 1.27, "change_pct": 0.69, ...}}
"""

from __future__ import annotations
import os
import time
import logging
import threading
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Caches — shared between the streaming and legacy code paths
# ─────────────────────────────────────────────────────────────

_cache: Dict[str, Tuple[float, Dict]] = {}            # ticker -> (ts, data)
_neg_cache: Dict[str, Tuple[float, str]] = {}         # ticker -> (cooldown_until, kind)
_cache_lock = threading.Lock()
_CACHE_TTL_SEC = 60.0


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


_BACKOFF_429_SEC = _env_int("DASHBOARD_SPOT_429_COOLDOWN_SEC", 300)
_BACKOFF_ERR_SEC = _env_int("DASHBOARD_SPOT_ERR_COOLDOWN_SEC", 60)


# ─────────────────────────────────────────────────────────────
# Yahoo Finance — legacy path. Kept verbatim from v8.3 so the
# rollback (env var off) is a true revert, not "mostly the same".
# ─────────────────────────────────────────────────────────────

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _fetch_one_yahoo(ticker: str) -> Tuple[Dict, str]:
    if not requests:
        return ({}, "skip")
    try:
        r = requests.get(YAHOO_URL.format(ticker), headers=HEADERS, timeout=4.0)
        if r.status_code == 429:
            log.warning(f"spot fetch 429 for {ticker} — cooling down {_BACKOFF_429_SEC}s")
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


# ─────────────────────────────────────────────────────────────
# Schwab provider lookup — overridable in tests
# ─────────────────────────────────────────────────────────────

def _get_schwab_provider():
    """Return an object with `_schwab_get(method, symbol)`, or None.

    Production: walks app.py's _cached_md._schwab. Tests monkey-patch
    this function to inject a fake.
    """
    try:
        import app
        cached_md = getattr(app, "_cached_md", None)
        if cached_md is None:
            return None
        schwab = getattr(cached_md, "_schwab", None)
        if schwab is None or not getattr(schwab, "available", False):
            return None
        return schwab
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Streaming-first fetch (S.3)
# ─────────────────────────────────────────────────────────────

def _build_record(ticker: str, price: float, prev_close: Optional[float]) -> Dict:
    if prev_close is None or prev_close == 0:
        change = 0.0
        change_pct = 0.0
    else:
        change = float(price) - float(prev_close)
        change_pct = (change / float(prev_close)) * 100.0
    return {
        "ticker": ticker,
        "price": round(float(price), 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "currency": "USD",
        "fetched_at": time.time(),
    }


def _fetch_one_schwab(ticker: str, schwab_provider) -> Optional[Dict]:
    """One Schwab get_quote returning a complete record (price + change).

    Used for cold-start (ticker not yet streaming) and for tickers that
    don't appear in the streaming sub list. Returns None on any failure.
    """
    try:
        data = schwab_provider._schwab_get("get_quote", ticker)
    except Exception as e:
        log.warning(f"schwab spot fetch failed for {ticker}: {e}")
        return None

    entry = (data or {}).get(ticker, {})
    quote = entry.get("quote", {})
    price = quote.get("lastPrice") or quote.get("mark")
    close = quote.get("closePrice")
    if not price:
        return None
    return _build_record(ticker, float(price), float(close) if close else None)


def _fetch_streaming_first(tickers: List[str]) -> Dict[str, Dict]:
    """The streaming-first path. Activated when DASHBOARD_SPOT_USE_STREAMING=1."""
    out: Dict[str, Dict] = {}
    if not tickers:
        return out

    # Lazy imports — these modules are only present when the bot is fully
    # wired. In test contexts they're stubbed.
    try:
        from schwab_stream import get_streaming_spot
    except Exception:
        get_streaming_spot = lambda t: None

    try:
        from prev_close_store import get_prev_close_store
        prev_store = get_prev_close_store()
    except Exception:
        prev_store = None

    schwab_provider = _get_schwab_provider()

    # Phase 1: streaming hits — collect tickers that have a fresh stream price.
    streaming_hits: Dict[str, float] = {}
    streaming_misses: List[str] = []
    for t in tickers:
        price = get_streaming_spot(t)
        if price is not None and price > 0:
            streaming_hits[t] = float(price)
        else:
            streaming_misses.append(t)

    # Phase 2: top up prev_close for streaming hits via the store (one
    # batch of REST calls if cache is cold, then nothing on warm cache).
    if streaming_hits and prev_store is not None and schwab_provider is not None:
        prev_store.ensure(list(streaming_hits.keys()), schwab_provider)

    for t, price in streaming_hits.items():
        prev = prev_store.get(t) if prev_store else None
        out[t] = _build_record(t, price, prev)

    # Phase 3: streaming misses — one Schwab quote each (covers cold-start
    # and unsubscribed tickers). If Schwab also fails, fall through to Yahoo.
    yahoo_misses: List[str] = []
    if streaming_misses and schwab_provider is not None:
        for t in streaming_misses:
            rec = _fetch_one_schwab(t, schwab_provider)
            if rec:
                out[t] = rec
            else:
                yahoo_misses.append(t)
    else:
        yahoo_misses = streaming_misses

    # Phase 4: last-resort Yahoo. Mirror the legacy negative-cache discipline:
    # tickers in cooldown are silently skipped, and post-fetch failures
    # populate the cooldown so we don't hammer Yahoo on the next page poll.
    # Without this, the v8.3 Patch 1 429-storm bug regresses on the Yahoo
    # tail of the streaming path.
    now = time.time()
    if yahoo_misses:
        with _cache_lock:
            in_cooldown = {
                t for t in yahoo_misses
                if (_neg_cache.get(t) and now < _neg_cache[t][0])
            }
        yahoo_misses = [t for t in yahoo_misses if t not in in_cooldown]

    for t in yahoo_misses:
        result, status = _fetch_one_yahoo(t)
        if status == "ok" and result:
            out[t] = result
        elif status == "429" and _BACKOFF_429_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_429_SEC, "429")
        elif status == "err" and _BACKOFF_ERR_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_ERR_SEC, "err")

    return out


# ─────────────────────────────────────────────────────────────
# Legacy Yahoo-only path — DASHBOARD_SPOT_USE_STREAMING=0
# ─────────────────────────────────────────────────────────────

def _fetch_legacy_yahoo(tickers: List[str]) -> Dict[str, Dict]:
    """Behavior-identical to the v8.3 implementation."""
    out: Dict[str, Dict] = {}
    if not tickers:
        return out

    now = time.time()
    to_fetch: List[str] = []
    with _cache_lock:
        for t in tickers:
            cached = _cache.get(t)
            if cached and (now - cached[0]) < _CACHE_TTL_SEC:
                out[t] = cached[1]
                continue
            neg = _neg_cache.get(t)
            if neg and now < neg[0]:
                log.debug(f"spot fetch for {t} suppressed (cooldown {int(neg[0] - now)}s)")
                continue
            to_fetch.append(t)

    for t in to_fetch:
        result, status = _fetch_one_yahoo(t)
        if status == "ok" and result:
            out[t] = result
            with _cache_lock:
                _cache[t] = (time.time(), result)
                _neg_cache.pop(t, None)
        elif status == "429" and _BACKOFF_429_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_429_SEC, "429")
        elif status == "err" and _BACKOFF_ERR_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_ERR_SEC, "err")

    return out


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def get_spot_prices(tickers: List[str]) -> Dict[str, Dict]:
    """Fetch spot prices for a list of tickers.

    Streaming-first when DASHBOARD_SPOT_USE_STREAMING=1, else legacy Yahoo.
    The 60s positive cache and negative cooldown cache apply in both modes
    for tickers that end up on the Yahoo fallback path.
    """
    if not tickers:
        return {}

    cleaned = []
    seen = set()
    for t in tickers:
        if not t:
            continue
        u = str(t).strip().upper()
        if u and u not in seen:
            seen.add(u)
            cleaned.append(u)

    if _env_bool("DASHBOARD_SPOT_USE_STREAMING", default=False):
        return _fetch_streaming_first(cleaned)
    return _fetch_legacy_yahoo(cleaned)
