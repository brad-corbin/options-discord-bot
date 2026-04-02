# api_cache.py
# ═══════════════════════════════════════════════════════════════════
# TTL-aware API response caching layer + API call counter.
# Wraps md_get and other API calls to avoid duplicate fetches
# within the same alert processing cycle.
#
# v5.1.1 API optimisation:
#   - TTLs aligned to poll intervals (spot 55s, chain 120s, bars 55s)
#   - API call counter with per-endpoint breakdown + periodic logging
#   - Daily budget tracking (warns at 75K/90K, logs every 5 min)
#
# Cache tiers:
#   spot price    → 55 sec  (monitor polls 60s; 10s was 100% miss rate)
#   option chain  → 120 sec (chains stable within 2 min; 20s was wasteful)
#   expirations   → 5 min   (changes once per day)
#   daily candles → 10 min  (changes once per day)
#   OHLC bars     → 10 min  (same as candles)
#   ADV + spread  → 10 min  (volume/quote-based liquidity, stable intraday)
#   VIX data      → 60 sec  (changes intraday but not per-second)
#   earnings      → 1 hour  (changes once per day)
#   stock quote   → 15 sec  (for liquidity estimates)
#   intraday bars → 55 sec  (monitor polls 60s; 30s was always miss)
#
# Thread-safe via threading.Lock per cache namespace.
# No external dependencies — pure Python.
#
# Usage in app.py:
#   from api_cache import CachedMarketData
#   cached_md = CachedMarketData(md_get)
#   spot = cached_md.get_spot("SPY")
#   chain = cached_md.get_chain("SPY", "2026-03-16")
#   print(cached_md.get_api_status())  # API call counter
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
# v5.1.1: Optimised to reduce MarketData.app API usage.
#   TTL_SPOT:      10 → 55s  (monitor polls 60s; 10s = always miss)
#   TTL_CHAIN:     20 → 120s (chains stable within 2 min)
#   TTL_INTRADAY:  30 → 55s  (same logic as spot)
# ─────────────────────────────────────────────────────────

TTL_SPOT         = 55    # was 10 — monitor polls every 60s, 10s = 100% miss rate
TTL_CHAIN        = 120   # was 20 — chains don't change fast enough to warrant 20s
TTL_EXPIRATIONS  = 300   # 5 min (unchanged)
TTL_CANDLES      = 600   # 10 min (unchanged)
TTL_OHLC_BARS    = 600   # 10 min (unchanged)
TTL_ADV          = 600   # 10 min (unchanged)
TTL_VIX          = 60    # (unchanged)
TTL_STOCK_QUOTE  = 15    # (unchanged)
TTL_EARNINGS     = 3600  # 1 hour (unchanged)
TTL_REGIME       = 300   # 5 min (unchanged)
TTL_INTRADAY     = 55    # was 30 — monitor polls 60s; 30s = always miss


# ─────────────────────────────────────────────────────────
# API CALL COUNTER — tracks every MarketData.app HTTP request
# ─────────────────────────────────────────────────────────

class _APICallCounter:
    """Thread-safe per-endpoint API call counter with periodic logging.

    Wraps the raw md_get function to intercept every call, classify it
    by endpoint category, and log a summary every LOG_INTERVAL seconds.

    Also tracks daily budget against DAILY_BUDGET and logs warnings
    at 75% and 90% utilisation.
    """
    DAILY_BUDGET = 100_000          # MarketData.app daily limit
    LOG_INTERVAL = 300              # log summary every 5 minutes
    WARN_75_PCT  = int(DAILY_BUDGET * 0.75)
    WARN_90_PCT  = int(DAILY_BUDGET * 0.90)

    def __init__(self, raw_md_get_fn):
        self._raw = raw_md_get_fn
        self._lock = threading.Lock()
        self._counts = {}           # endpoint_category → count
        self._total = 0
        self._credits = 0           # v5.1.1: estimated CREDIT consumption
        self._credit_counts = {}    # endpoint_category → credits
        self._last_log_time = time.time()
        self._last_log_total = 0
        self._last_log_credits = 0
        self._warned_75 = False
        self._warned_90 = False
        self._day_key = ""
        self._start_time = time.time()

    def _classify(self, url: str) -> str:
        """Map a MarketData.app URL to a short category for logging."""
        if "/options/chain/" in url:     return "chain"
        if "/options/expiration" in url:  return "expirations"
        if "/stocks/prices/" in url:     return "spot_smartmid"
        if "/stocks/quotes/" in url:     return "spot_quotes"
        if "/stocks/candles/D/" in url or "/candles/daily/" in url:
            return "daily_candles"
        if "/stocks/candles/" in url:     return "intraday_bars"
        if "/indices/" in url:            return "vix_indices"
        return "other"

    def __call__(self, url, params=None, retries=2):
        """Drop-in replacement for md_get — counts calls AND credits."""
        cat = self._classify(url)
        with self._lock:
            self._counts[cat] = self._counts.get(cat, 0) + 1
            self._total += 1
            # Check day rollover
            try:
                from datetime import datetime, timezone
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                today = ""
            if today != self._day_key:
                if self._day_key:  # not first run
                    log.info(f"API counter: day rolled {self._day_key} → {today}, "
                             f"resetting (prev total: {self._total - 1}, credits: {self._credits})")
                self._day_key = today
                self._counts.clear()
                self._credit_counts.clear()
                self._total = 1
                self._credits = 0
                self._counts[cat] = 1
                self._warned_75 = False
                self._warned_90 = False

        # Delegate to real md_get
        result = self._raw(url, params, retries=retries)

        # Estimate credits consumed from response
        cred = 1  # default: 1 credit per call
        if cat == "chain" and isinstance(result, dict) and result.get("s") == "ok":
            # Chain calls cost 1 credit per option symbol returned
            symbols = result.get("optionSymbol", [])
            if isinstance(symbols, list) and symbols:
                cred = len(symbols)

        with self._lock:
            self._credits += cred
            self._credit_counts[cat] = self._credit_counts.get(cat, 0) + cred
            local_credits = self._credits

        # Budget warnings (based on credits, not calls)
        if local_credits >= self.WARN_90_PCT and not self._warned_90:
            self._warned_90 = True
            log.warning(f"🚨 CREDIT BUDGET 90%: {local_credits:,}/{self.DAILY_BUDGET:,} credits used")
        elif local_credits >= self.WARN_75_PCT and not self._warned_75:
            self._warned_75 = True
            log.warning(f"⚠️ CREDIT BUDGET 75%: {local_credits:,}/{self.DAILY_BUDGET:,} credits used")

        # Periodic summary
        now = time.time()
        if now - self._last_log_time >= self.LOG_INTERVAL:
            self._log_summary(now)

        return result

    def _log_summary(self, now: float):
        with self._lock:
            delta_calls = self._total - self._last_log_total
            delta_credits = self._credits - self._last_log_credits
            elapsed_min = (now - self._last_log_time) / 60
            rate_calls = delta_calls / elapsed_min if elapsed_min > 0 else 0
            rate_credits = delta_credits / elapsed_min if elapsed_min > 0 else 0
            # Credit breakdown (the important one)
            cred_cats = sorted(self._credit_counts.items(), key=lambda x: -x[1])
            breakdown = " | ".join(f"{k}={v:,}" for k, v in cred_cats)
            self._last_log_time = now
            self._last_log_total = self._total
            self._last_log_credits = self._credits
            credits = self._credits
            calls = self._total
        log.info(f"📊 API CREDITS: {credits:,}/{self.DAILY_BUDGET:,} "
                 f"(+{delta_credits:,} in {elapsed_min:.1f}min, {rate_credits:.0f}/min) "
                 f"[{breakdown}] "
                 f"({calls} calls, {rate_calls:.0f} calls/min)")

    @property
    def total(self) -> int:
        return self._total

    @property
    def breakdown(self) -> dict:
        with self._lock:
            return dict(self._counts)

    def get_status(self) -> dict:
        """Full status dict for /status endpoint or diagnostics."""
        with self._lock:
            return {
                "calls": self._total,
                "credits": self._credits,
                "budget": self.DAILY_BUDGET,
                "pct_used": round(self._credits / self.DAILY_BUDGET * 100, 1),
                "call_breakdown": dict(self._counts),
                "credit_breakdown": dict(self._credit_counts),
                "day": self._day_key,
            }


class CachedMarketData:
    """
    Caching wrapper around md_get and related API calls.
    Drop-in enhancement for app.py — same interface, fewer API calls.

    v5.1.1: Wraps md_get with _APICallCounter to track per-endpoint
    usage and log periodic summaries with daily budget warnings.

    Usage:
        cached_md = CachedMarketData(md_get)
        spot = cached_md.get_spot("SPY")
        chain = cached_md.get_chain("SPY", "2026-03-16")
        print(cached_md.get_api_status())  # counter status
    """

    def __init__(self, md_get_fn: Callable):
        # Wrap raw md_get with API call counter
        self._api_counter = _APICallCounter(md_get_fn)
        self._md_get = self._api_counter   # counter is callable, same signature
        self._spot_cache = _TTLCache(TTL_SPOT)
        self._chain_cache = _TTLCache(TTL_CHAIN)
        self._exp_cache = _TTLCache(TTL_EXPIRATIONS)
        self._candle_cache = _TTLCache(TTL_CANDLES)
        self._ohlc_cache = _TTLCache(TTL_OHLC_BARS)
        self._adv_cache = _TTLCache(TTL_ADV)
        self._vix_cache = _TTLCache(TTL_VIX)
        self._quote_cache = _TTLCache(TTL_STOCK_QUOTE)
        self._intraday_cache = _TTLCache(TTL_INTRADAY)

    # ── Spot Price ──
    def get_spot(self, ticker: str, as_float_fn=None) -> float:
        """Cached spot price using SmartMid (real-time on all plans).
        v5.1: Switched from /stocks/quotes/ (15-min delayed on Trader plan)
              to /stocks/prices/ (SmartMid, real-time on all plans).
              Falls back to /stocks/quotes/ if SmartMid fails.
        """
        key = ticker.upper()
        cached = self._spot_cache.get(key)
        if cached is not None:
            return cached

        af = as_float_fn or _default_as_float

        # Primary: SmartMid (real-time on all plans)
        try:
            data = self._md_get(
                f"https://api.marketdata.app/v1/stocks/prices/{key}/"
            )
            if isinstance(data, dict) and data.get("s") == "ok":
                mid_arr = data.get("mid")
                if isinstance(mid_arr, list) and mid_arr:
                    v = af(mid_arr[0], 0.0)
                    if v > 0:
                        self._spot_cache.set(key, v)
                        return v
                elif isinstance(mid_arr, (int, float)):
                    v = af(mid_arr, 0.0)
                    if v > 0:
                        self._spot_cache.set(key, v)
                        return v
        except Exception as e:
            log.debug(f"SmartMid failed for {key}, falling back to quotes: {e}")

        # Fallback: /stocks/quotes/ (15-min delayed but better than nothing)
        data = self._md_get(
            f"https://api.marketdata.app/v1/stocks/quotes/{key}/"
        )
        for field in ("last", "mid", "bid", "ask"):
            v = af(data.get(field), 0.0)
            if v > 0:
                self._spot_cache.set(key, v)
                return v
        raise RuntimeError(f"Cannot parse spot for {key}")

    # ── Option Chain ──
    def get_chain(self, ticker: str, expiration: str,
                  side: str = None, strike_limit: int = None) -> dict:
        """Cached option chain for a specific expiration.

        v5.1.1 API credit optimization:
        MarketData.app charges 1 credit PER OPTION SYMBOL in the response.
        SPY with 390 contracts = 390 credits per fetch!

        Args:
            side: "call" or "put" — halves credit cost by fetching one side only
            strike_limit: int — limits to N nearest-ATM strikes per side.
                          With strike_limit=20: SPY drops from 390 to ~40 contracts.
                          Combined with side: drops to ~20 contracts (95% savings).
        """
        # Cache key includes filters so filtered/unfiltered don't collide
        filter_tag = ""
        if side:
            filter_tag += f":s={side}"
        if strike_limit:
            filter_tag += f":sl={strike_limit}"
        key = f"{ticker.upper()}:{expiration}{filter_tag}"
        cached = self._chain_cache.get(key)
        if cached is not None:
            return cached

        params = {"expiration": expiration}
        if side:
            params["side"] = side
        if strike_limit:
            params["strikeLimit"] = strike_limit

        data = self._md_get(
            f"https://api.marketdata.app/v1/options/chain/{ticker.upper()}/",
            params,
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

    # ── Intraday Bars (5m OHLCV for bar-aware monitor) ──
    def get_intraday_bars(self, ticker: str, resolution: int = 5,
                          countback: int = 80) -> dict:
        """
        Cached intraday OHLCV bars from MarketData.app.
        Returns raw API dict: {s: "ok", o: [...], h: [...], l: [...], c: [...], v: [...], t: [...]}
        Caller (BarStateManager) parses into Bar objects.

        v5.1.1: Now caches ALL countback values (previously skipped cb<=5).
        With TTL_INTRADAY=55s and monitor polling at 60s, the cache naturally
        expires before the next poll — no staleness risk, but prevents duplicate
        fetches within the same cycle (e.g. prefetch + check_ticker).

        Args:
            ticker: Symbol
            resolution: Bar size in minutes (1, 5, 15, 30, 60)
            countback: Number of bars to fetch
        """
        key = f"intra:{ticker.upper()}:{resolution}:{countback}"
        cached = self._intraday_cache.get(key)
        if cached is not None:
            return cached
        try:
            data = self._md_get(
                f"https://api.marketdata.app/v1/stocks/candles/{resolution}/{ticker.upper()}/",
                {"countback": countback},
            )
            if isinstance(data, dict) and data.get("s") == "ok":
                self._intraday_cache.set(key, data)
                return data
            log.warning(f"Intraday bars {ticker} {resolution}m: bad response")
            return {}
        except Exception as e:
            log.warning(f"Intraday bars fetch failed for {ticker}: {e}")
            return {}

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
        """Cached VIX + VIX9D + term structure.
        v4.3: Multi-fallback strategy:
          1. CBOE direct CSV (free, no API key, most reliable)
          2. /v1/indices/quotes/ (real-time MarketData)
          3. /v1/indices/candles/ (daily close MarketData)
          4. Returns {} if all fail — caller handles IV proxy fallback
        """
        cached = self._vix_cache.get("vix")
        if cached is not None:
            return cached

        af = as_float_fn or _default_as_float
        vix = 0.0
        vix9d = 0.0

        # ── Strategy 1: CBOE direct CSV (free, reliable, works after hours) ──
        try:
            import requests as _req
            resp = _req.get(
                "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                timeout=8, headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) > 1:
                # Last row: DATE,OPEN,HIGH,LOW,CLOSE
                parts = lines[-1].strip().split(",")
                if len(parts) >= 5:
                    vix = af(parts[4], 0)
                    if vix > 0:
                        log.info(f"VIX from CBOE direct: {vix:.2f} (date: {parts[0]})")
        except Exception as e:
            log.debug(f"CBOE VIX CSV fetch failed: {e}")

        # ── Strategy 2: MarketData indices quotes ──
        if vix <= 0:
            def _parse_quote(data):
                if not isinstance(data, dict):
                    return 0.0
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
                    "https://api.marketdata.app/v1/indices/quotes/VIX/"
                ))
                if vix > 0:
                    log.info(f"VIX from MarketData indices/quotes: {vix}")
            except Exception:
                pass

        # ── Strategy 3: MarketData indices candles ──
        if vix <= 0:
            try:
                data = self._md_get(
                    "https://api.marketdata.app/v1/indices/candles/daily/VIX/",
                    {"countback": 3},
                )
                if isinstance(data, dict) and data.get("s") == "ok":
                    closes = data.get("c", [])
                    if closes:
                        vix = af(closes[-1], 0)
                        if vix > 0:
                            log.info(f"VIX from MarketData candle: {vix}")
            except Exception:
                pass

        if vix <= 0:
            log.warning("VIX: All sources failed — returning empty for IV proxy fallback")
            return {}

        # ── VIX9D: try MarketData then CBOE ──
        try:
            def _pq(data):
                if not isinstance(data, dict):
                    return 0.0
                for field in ("last", "mid", "bid"):
                    v = data.get(field)
                    if isinstance(v, list):
                        v = v[0] if v else None
                    val = af(v, 0)
                    if val > 0:
                        return val
                return 0.0
            vix9d = _pq(self._md_get(
                "https://api.marketdata.app/v1/indices/quotes/VIX9D/"
            ))
        except Exception:
            pass
        if vix9d <= 0:
            try:
                import requests as _req
                resp = _req.get(
                    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
                    timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                lines = resp.text.strip().split("\n")
                if len(lines) > 1:
                    parts = lines[-1].strip().split(",")
                    if len(parts) >= 5:
                        vix9d = af(parts[4], 0)
                        if vix9d > 0:
                            log.info(f"VIX9D from CBOE direct: {vix9d:.2f}")
            except Exception:
                pass

        # ── Term structure ──
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
        log.info(f"VIX data resolved: vix={result['vix']} vix9d={result.get('vix9d')} term={result['term']}")
        return result

    # ── Liquidity Estimate (ADV + bid-ask spread) ──
    def get_liquidity(self, ticker: str, as_float_fn=None) -> tuple:
        """
        Cached ADV (avg daily dollar volume) and bid-ask spread pct.
        Returns (adv, spread_pct) — either may be None on failure.
        TTL=10min: volume is stable enough intraday.
        """
        key = ticker.upper()
        cached = self._adv_cache.get(key)
        if cached is not None:
            return cached

        af = as_float_fn or _default_as_float
        adv = None
        spread = None

        try:
            from datetime import datetime, timezone, timedelta
            from_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            raw = self._md_get(
                f"https://api.marketdata.app/v1/stocks/candles/D/{key}/",
                {"from": from_date},
            )
            if isinstance(raw, dict) and raw.get("s") == "ok":
                closes = raw.get("c") or []
                volumes = raw.get("v") or []
                n = min(len(closes), len(volumes))
                if n >= 5:
                    dvols = []
                    for i in range(max(n - 20, 0), n):
                        c = af(closes[i], 0)
                        v = af(volumes[i], 0)
                        if c > 0 and v > 0:
                            dvols.append(c * v)
                    if dvols:
                        adv = sum(dvols) / len(dvols)
        except Exception as e:
            log.debug(f"Cached ADV fetch failed for {ticker}: {e}")

        try:
            quote = self.get_stock_quote(key)
            if isinstance(quote, dict) and quote.get("s") == "ok":
                bid_raw = quote.get("bid")
                ask_raw = quote.get("ask")
                bid = af(bid_raw[0] if isinstance(bid_raw, list) else bid_raw, 0)
                ask = af(ask_raw[0] if isinstance(ask_raw, list) else ask_raw, 0)
                if bid > 0 and ask > 0 and ask > bid:
                    spread = (ask - bid) / ((ask + bid) / 2)
        except Exception as e:
            log.debug(f"Cached spread fetch failed for {ticker}: {e}")

        result = (adv, spread)
        # Only cache if we got at least one value; don't cache double-None
        if adv is not None or spread is not None:
            self._adv_cache.set(key, result)
        return result

    # ── Raw md_get passthrough (for uncached calls) ──
    def raw_get(self, url: str, params=None) -> dict:
        """Direct md_get passthrough for uncacheable calls."""
        return self._md_get(url, params)

    # ── Cache Stats ──
    def get_stats(self) -> dict:
        stats = {
            "spot": self._spot_cache.stats,
            "chain": self._chain_cache.stats,
            "expirations": self._exp_cache.stats,
            "candles": self._candle_cache.stats,
            "ohlc": self._ohlc_cache.stats,
            "adv": self._adv_cache.stats,
            "vix": self._vix_cache.stats,
            "quote": self._quote_cache.stats,
            "intraday": self._intraday_cache.stats,
        }
        # Include API counter if available
        if hasattr(self, '_api_counter'):
            stats["api_counter"] = self._api_counter.get_status()
        return stats

    def get_api_status(self) -> dict:
        """Get API call counter status for /status endpoint or diagnostics.
        Returns dict with total, budget, pct_used, breakdown, day."""
        if hasattr(self, '_api_counter'):
            return self._api_counter.get_status()
        return {"total": 0, "budget": 100000, "pct_used": 0, "breakdown": {}, "day": ""}

    def prune_all(self):
        """Remove expired entries from all caches."""
        for cache in (self._spot_cache, self._chain_cache, self._exp_cache,
                      self._candle_cache, self._ohlc_cache, self._adv_cache,
                      self._vix_cache, self._quote_cache, self._intraday_cache):
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
