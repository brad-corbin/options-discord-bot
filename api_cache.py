# api_cache.py
# ═══════════════════════════════════════════════════════════════════
# TTL-aware API response caching layer.
# Wraps md_get and other API calls to avoid duplicate fetches
# within the same alert processing cycle.
#
# Cache tiers:
#   spot price    → 10 sec  (changes fast but multiple calls per alert)
#   option chain  → 20 sec  (expensive call, stable within a cycle)
#   expirations   → 5 min   (changes once per day)
#   daily candles → 10 min  (changes once per day)
#   OHLC bars     → 10 min  (same as candles)
#   VIX data      → 60 sec  (changes intraday but not per-second)
#   earnings      → 1 hour  (changes once per day)
#   stock quote   → 15 sec  (for liquidity estimates)
#
# Thread-safe via threading.Lock per cache namespace.
# No external dependencies — pure Python.
#
# Usage in app.py:
#   from api_cache import CachedMarketData
#   cached_md = CachedMarketData(md_get)
#   spot = cached_md.get_spot("SPY")
#   chain = cached_md.get_chain("SPY", "2026-03-16")
# ═══════════════════════════════════════════════════════════════════

import time
import logging
import threading
from typing import Optional, Callable, Any

log = logging.getLogger(__name__)


class _TTLCache:
    """Simple thread-safe TTL cache."""

    def __init__(self, default_ttl: float = 30.0):
        self._data: dict = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._data[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: float = None):
        ttl = ttl if ttl is not None else self._default_ttl
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)

    def invalidate(self, key: str):
        with self._lock:
            self._data.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()

    def prune(self):
        """Remove expired entries."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, exp) in self._data.items() if now > exp]
            for k in expired:
                del self._data[k]

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 2) if total > 0 else 0,
            "size": len(self._data),
        }


# ─────────────────────────────────────────────────────────
# CACHE TTL CONSTANTS (seconds)
# ─────────────────────────────────────────────────────────

TTL_SPOT         = 10
TTL_CHAIN        = 20
TTL_EXPIRATIONS  = 300   # 5 min
TTL_CANDLES      = 600   # 10 min
TTL_OHLC_BARS    = 600   # 10 min
TTL_VIX          = 60
TTL_STOCK_QUOTE  = 15
TTL_EARNINGS     = 3600  # 1 hour
TTL_REGIME       = 300   # 5 min


class CachedMarketData:
    """
    Caching wrapper around md_get and related API calls.
    Drop-in enhancement for app.py — same interface, fewer API calls.

    Usage:
        cached_md = CachedMarketData(md_get)
        spot = cached_md.get_spot("SPY")
        chain = cached_md.get_chain("SPY", "2026-03-16")
    """

    def __init__(self, md_get_fn: Callable):
        self._md_get = md_get_fn
        self._spot_cache = _TTLCache(TTL_SPOT)
        self._chain_cache = _TTLCache(TTL_CHAIN)
        self._exp_cache = _TTLCache(TTL_EXPIRATIONS)
        self._candle_cache = _TTLCache(TTL_CANDLES)
        self._ohlc_cache = _TTLCache(TTL_OHLC_BARS)
        self._vix_cache = _TTLCache(TTL_VIX)
        self._quote_cache = _TTLCache(TTL_STOCK_QUOTE)

    # ── Spot Price ──
    def get_spot(self, ticker: str, as_float_fn=None) -> float:
        """Cached spot price. Falls back to raw md_get on miss."""
        key = ticker.upper()
        cached = self._spot_cache.get(key)
        if cached is not None:
            return cached

        data = self._md_get(
            f"https://api.marketdata.app/v1/stocks/quotes/{key}/"
        )
        af = as_float_fn or _default_as_float
        for field in ("last", "mid", "bid", "ask"):
            v = af(data.get(field), 0.0)
            if v > 0:
                self._spot_cache.set(key, v)
                return v
        raise RuntimeError(f"Cannot parse spot for {key}")

    # ── Option Chain ──
    def get_chain(self, ticker: str, expiration: str) -> dict:
        """Cached option chain for a specific expiration."""
        key = f"{ticker.upper()}:{expiration}"
        cached = self._chain_cache.get(key)
        if cached is not None:
            return cached

        data = self._md_get(
            f"https://api.marketdata.app/v1/options/chain/{ticker.upper()}/",
            {"expiration": expiration},
        )
        if isinstance(data, dict) and data.get("s") == "ok":
            self._chain_cache.set(key, data)
        return data

    # ── Expirations ──
    def get_expirations(self, ticker: str) -> list:
        """Cached expiration dates."""
        key = ticker.upper()
        cached = self._exp_cache.get(key)
        if cached is not None:
            return cached

        data = self._md_get(
            f"https://api.marketdata.app/v1/options/expirations/{key}/"
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            raise RuntimeError(f"Bad expirations for {key}")
        result = sorted(set(str(e)[:10] for e in (data.get("expirations") or []) if e))
        self._exp_cache.set(key, result)
        return result

    # ── Daily Candles (close prices only) ──
    def get_daily_candles(self, ticker: str, days: int = 30) -> list:
        """Cached daily close prices."""
        key = f"{ticker.upper()}:{days}"
        cached = self._candle_cache.get(key)
        if cached is not None:
            return cached

        from datetime import datetime, timezone, timedelta
        from_date = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
        try:
            data = self._md_get(
                f"https://api.marketdata.app/v1/stocks/candles/daily/{ticker.upper()}/",
                {"from": from_date, "countback": days + 5},
            )
            if not isinstance(data, dict) or data.get("s") != "ok":
                return []
            closes = data.get("c", [])
            result = [float(c) for c in closes if c is not None] if isinstance(closes, list) else []
            self._candle_cache.set(key, result)
            return result
        except Exception as e:
            log.warning(f"Cached candles fetch failed for {ticker}: {e}")
            return []

    # ── OHLC Bars (full bars for v4 engine) ──
    def get_ohlc_bars(self, ticker: str, days: int = 65) -> list:
        """Cached OHLC bars as options_exposure.OHLC objects."""
        key = f"ohlc:{ticker.upper()}:{days}"
        cached = self._ohlc_cache.get(key)
        if cached is not None:
            return cached

        from datetime import datetime, timezone, timedelta
        try:
            from options_exposure import OHLC as _OHLC
            from_date = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
            data = self._md_get(
                f"https://api.marketdata.app/v1/stocks/candles/D/{ticker.upper()}/",
                {"from": from_date},
            )
            if not isinstance(data, dict) or data.get("s") != "ok":
                return []
            opens = data.get("o") or []
            highs = data.get("h") or []
            lows = data.get("l") or []
            closes = data.get("c") or []
            n = min(len(opens), len(highs), len(lows), len(closes))
            bars = []
            for i in range(n):
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                if any(x is None or x <= 0 for x in [o, h, l, c]):
                    continue
                pc = closes[i - 1] if i > 0 else o
                bars.append(_OHLC(
                    open=float(o), high=float(h), low=float(l),
                    close=float(c), prev_close=float(pc),
                ))
            result = bars if len(bars) >= 10 else []
            self._ohlc_cache.set(key, result)
            return result
        except Exception as e:
            log.warning(f"Cached OHLC bars fetch failed for {ticker}: {e}")
            return []

    # ── Stock Quote (for liquidity estimates) ──
    def get_stock_quote(self, ticker: str) -> dict:
        """Cached stock quote with bid/ask/volume."""
        key = ticker.upper()
        cached = self._quote_cache.get(key)
        if cached is not None:
            return cached

        data = self._md_get(
            f"https://api.marketdata.app/v1/stocks/quotes/{key}/"
        )
        if isinstance(data, dict):
            self._quote_cache.set(key, data)
        return data

    # ── VIX Data ──
    def get_vix_data(self, as_float_fn=None) -> dict:
        """Cached VIX + VIX9D + term structure."""
        cached = self._vix_cache.get("vix")
        if cached is not None:
            return cached

        af = as_float_fn or _default_as_float

        def _parse_quote(data):
            for field in ("last", "mid", "bid"):
                v = data.get(field)
                if isinstance(v, list):
                    v = v[0] if v else None
                val = af(v, 0)
                if val > 0:
                    return val
            return 0.0

        try:
            vix = _parse_quote(self._md_get(
                "https://api.marketdata.app/v1/stocks/quotes/VIX/"
            ))
            vix9d = _parse_quote(self._md_get(
                "https://api.marketdata.app/v1/stocks/quotes/VIX9D/"
            ))
            if vix <= 0:
                return {}
            if vix9d > 0:
                if vix9d > vix * 1.05:
                    term = "inverted"
                elif vix9d < vix * 0.95:
                    term = "normal"
                else:
                    term = "flat"
            else:
                term = "unknown"
            result = {
                "vix": round(vix, 1),
                "vix9d": round(vix9d, 1) if vix9d > 0 else None,
                "term": term,
            }
            self._vix_cache.set("vix", result)
            return result
        except Exception as e:
            log.debug(f"Cached VIX fetch failed: {e}")
            return {}

    # ── Raw md_get passthrough (for uncached calls) ──
    def raw_get(self, url: str, params=None) -> dict:
        """Direct md_get passthrough for uncacheable calls."""
        return self._md_get(url, params)

    # ── Cache Stats ──
    def get_stats(self) -> dict:
        return {
            "spot": self._spot_cache.stats,
            "chain": self._chain_cache.stats,
            "expirations": self._exp_cache.stats,
            "candles": self._candle_cache.stats,
            "ohlc": self._ohlc_cache.stats,
            "vix": self._vix_cache.stats,
            "quote": self._quote_cache.stats,
        }

    def prune_all(self):
        """Remove expired entries from all caches."""
        for cache in (self._spot_cache, self._chain_cache, self._exp_cache,
                      self._candle_cache, self._ohlc_cache, self._vix_cache,
                      self._quote_cache):
            cache.prune()


def _default_as_float(x, default=0.0):
    if x is None:
        return default
    if isinstance(x, list):
        x = x[0] if x else default
    try:
        return float(x) if x is not None else default
    except (ValueError, TypeError):
        return default
