"""
prev_close_store.py — thread-safe per-ticker previous-close cache.

Filled lazily from Schwab's get_quote endpoint via _cached_md._schwab.
The dashboard's spot_prices module pairs streaming live price with this
prev_close to compute the change/change_pct columns. Without this, the
dashboard would lose its red/green coloring when streaming-first lands.

Audit rule 1: this is the canonical previous-close source. Don't add
parallel implementations elsewhere.

Architecture:
  - In-memory dict (ticker -> (price, fetched_at))
  - 25h TTL: covers a full overnight + buffer for late afternoon refresh
  - Singleton accessed via get_prev_close_store()
  - ensure(tickers, schwab_provider) batches missing fetches at the call
    site of /api/spot-prices — one Schwab call per uncached ticker per day

Failure modes:
  - Schwab errors on a ticker: the ticker is reported back from ensure()
    so the caller can decide whether to retry, drop, or fall back to a
    different source. The cache is NOT poisoned with bogus values.
  - closePrice missing from quote: same as above.
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 25 * 3600  # 25 hours


class PrevCloseStore:
    """Thread-safe per-ticker previous-close cache with TTL.

    Not a singleton on its own; access via get_prev_close_store().
    """

    def __init__(self, ttl_sec: float = DEFAULT_TTL_SEC):
        self._cache: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_sec

    def get(self, ticker: str) -> Optional[float]:
        """Return cached prev_close if fresh, else None."""
        if not ticker:
            return None
        key = ticker.upper()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            price, fetched_at = entry
            if (time.time() - fetched_at) > self._ttl:
                # Expired — drop it so the next ensure() refetches.
                self._cache.pop(key, None)
                return None
            return price

    def set(self, ticker: str, price: float) -> None:
        """Store a prev_close value. Caller is responsible for the value."""
        if not ticker or price is None:
            return
        key = ticker.upper()
        with self._lock:
            self._cache[key] = (float(price), time.time())

    def ensure(self, tickers: List[str], schwab_provider) -> List[str]:
        """Fetch prev_close for any tickers not already cached.

        Args:
            tickers: list of ticker symbols to ensure.
            schwab_provider: an object with `_schwab_get(method, symbol)`,
                typically `_cached_md._schwab` from app.py.

        Returns:
            List of tickers that could not be fetched (Schwab error,
            missing closePrice in response, etc). Successful tickers are
            stored in the cache; the caller does not need to call set()
            after ensure().
        """
        if not tickers:
            return []

        # Snapshot the missing list under lock so we read a consistent
        # _cache view. Note: this does NOT cross-call dedup — two
        # concurrent ensure() calls can each fetch the same ticker once.
        # Acceptable: idempotent writes, at most one wasted call per
        # concurrent caller.
        to_fetch: List[str] = []
        with self._lock:
            for t in tickers:
                if not t:
                    continue
                key = t.upper()
                entry = self._cache.get(key)
                if entry is None:
                    to_fetch.append(key)
                    continue
                _, fetched_at = entry
                if (time.time() - fetched_at) > self._ttl:
                    to_fetch.append(key)

        unfetchable: List[str] = []
        for ticker in to_fetch:
            try:
                data = schwab_provider._schwab_get("get_quote", ticker)
            except Exception as e:
                log.debug(f"prev_close fetch failed for {ticker}: {e}")
                unfetchable.append(ticker)
                continue

            entry = (data or {}).get(ticker, {})
            quote = entry.get("quote", {})
            close = quote.get("closePrice")
            if close is None or close == 0:
                # Schwab returned a quote but no usable closePrice.
                unfetchable.append(ticker)
                continue

            with self._lock:
                self._cache[ticker] = (float(close), time.time())

        if to_fetch:
            log.info(
                f"prev_close ensure: fetched {len(to_fetch) - len(unfetchable)}/"
                f"{len(to_fetch)} tickers, {len(unfetchable)} unfetchable"
            )
        return unfetchable

    def stats(self) -> dict:
        """Diagnostic: how many entries, oldest fetch age."""
        with self._lock:
            now = time.time()
            ages = [now - ts for _, ts in self._cache.values()]
            return {
                "entries": len(self._cache),
                "oldest_age_sec": int(max(ages)) if ages else 0,
                "newest_age_sec": int(min(ages)) if ages else 0,
            }


# ─────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────

_singleton: Optional[PrevCloseStore] = None
_singleton_lock = threading.Lock()


def get_prev_close_store() -> PrevCloseStore:
    """Return the process-wide PrevCloseStore."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = PrevCloseStore()
    return _singleton
