# schwab_adapter.py
# ═══════════════════════════════════════════════════════════════════
# Schwab API adapter for Omega3000.
# Drop-in replacement for CachedMarketData — same interface,
# Schwab data source, automatic MarketData.app fallback.
#
# Usage in app.py:
#   from schwab_adapter import build_data_router
#   _cached_md = build_data_router(md_get)  # replaces CachedMarketData(md_get)
#
# Architecture:
#   SchwabDataProvider  — translates Schwab REST → MarketData JSON format
#   DataRouter          — tries Schwab first, falls back to MarketData
#   SchwabStreamManager — WebSocket streaming (Phase 2+)
#
# All subsystems see identical JSON — zero code changes downstream.
# ═══════════════════════════════════════════════════════════════════

import os
import time
import logging
import threading
from typing import Optional, Callable, Any
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# TTL Cache (shared with api_cache.py pattern)
# ─────────────────────────────────────────────────────────────

# Schwab has no per-credit billing, so TTLs can be shorter for fresher data
TTL_SPOT        = 10    # real-time quotes, refresh aggressively
TTL_CHAIN       = 30    # chains refresh faster than MarketData's 120s
TTL_EXPIRATIONS = 300   # 5 min (same — changes daily)
TTL_CANDLES     = 600   # 10 min (same)
TTL_OHLC_BARS   = 600   # 10 min (same)
TTL_ADV         = 600   # 10 min (same)
TTL_VIX         = 60    # (same)
TTL_STOCK_QUOTE = 10    # faster refresh
TTL_INTRADAY    = 30    # shorter — no credit cost


class _TTLCache:
    """Simple thread-safe TTL cache (same as api_cache.py)."""

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


def _safe_float(x, default=0.0):
    if x is None:
        return default
    if isinstance(x, list):
        x = x[0] if x else default
    try:
        return float(x) if x is not None else default
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────
# Schwab Client Initialisation
# ─────────────────────────────────────────────────────────────

def _init_schwab_client():
    """Create schwab-py client from env vars.
    Returns Client or None if config missing.

    Token loading priority:
      1. SCHWAB_TOKEN_JSON env var (base64-encoded token.json contents)
         → decoded and written to SCHWAB_TOKEN_PATH on startup
      2. Existing token file at SCHWAB_TOKEN_PATH

    Token persistence:
      A background thread watches the token file for changes.
      When schwab-py refreshes the token, the updated file is
      base64-encoded and pushed to Render's API so SCHWAB_TOKEN_JSON
      always has the latest refresh token. Requires RENDER_API_KEY.
    """
    app_key = os.environ.get("SCHWAB_APP_KEY", "")
    app_secret = os.environ.get("SCHWAB_APP_SECRET", "")
    token_path = os.environ.get("SCHWAB_TOKEN_PATH", "schwab_token.json")

    if not app_key or not app_secret:
        log.warning("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set — Schwab disabled")
        return None

    # Decode base64 token from env var (same pattern as GOOGLE_SHEETS_CREDS)
    token_b64 = os.environ.get("SCHWAB_TOKEN_JSON", "")
    if token_b64:
        try:
            import base64
            token_bytes = base64.b64decode(token_b64)
            os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
            with open(token_path, "wb") as f:
                f.write(token_bytes)
            log.info(f"Schwab token decoded from SCHWAB_TOKEN_JSON → {token_path}")
        except Exception as e:
            log.error(f"Failed to decode SCHWAB_TOKEN_JSON: {e}")

    try:
        from schwab.auth import client_from_token_file
        client = client_from_token_file(token_path, app_key, app_secret)
        log.info("Schwab client initialised from token file")

        # Start token sync thread
        _start_token_sync(token_path)

        return client
    except FileNotFoundError:
        log.error(f"Schwab token file not found at {token_path}. "
                  "Set SCHWAB_TOKEN_JSON env var (base64) or copy token.json to server.")
        return None
    except Exception as e:
        log.error(f"Schwab client init failed: {e}")
        return None


def _start_token_sync(token_path: str):
    """Background thread that watches token.json for changes and syncs
    the updated token back to Render's SCHWAB_TOKEN_JSON env var.

    Requires env vars:
      RENDER_API_KEY     — Render API key (Account Settings → API Keys)
      RENDER_SERVICE_ID  — auto-set by Render, or set manually
    """
    render_api_key = os.environ.get("RENDER_API_KEY", "")
    service_id = os.environ.get("RENDER_SERVICE_ID", "")

    if not render_api_key:
        log.info("RENDER_API_KEY not set — token auto-sync disabled. "
                 "Token will still work but won't survive redeploys after 7 days.")
        return
    if not service_id:
        log.warning("RENDER_SERVICE_ID not set — token auto-sync disabled.")
        return

    def _sync_loop():
        import base64
        last_mtime = 0
        try:
            last_mtime = os.path.getmtime(token_path)
        except OSError:
            pass

        while True:
            try:
                time.sleep(60)  # check every 60 seconds
                try:
                    current_mtime = os.path.getmtime(token_path)
                except OSError:
                    continue
                if current_mtime <= last_mtime:
                    continue

                # Token file changed — read and push to Render
                last_mtime = current_mtime
                with open(token_path, "rb") as f:
                    token_bytes = f.read()
                new_b64 = base64.b64encode(token_bytes).decode("utf-8")

                import requests as _req
                resp = _req.put(
                    f"https://api.render.com/v1/services/{service_id}/env-vars/SCHWAB_TOKEN_JSON",
                    headers={
                        "Authorization": f"Bearer {render_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"value": new_b64},
                    timeout=15,
                )
                if resp.status_code == 200:
                    log.info("Schwab token synced to Render env var (no redeploy triggered)")
                else:
                    log.warning(f"Schwab token sync failed: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                log.debug(f"Token sync error: {e}")

    t = threading.Thread(target=_sync_loop, name="schwab-token-sync", daemon=True)
    t.start()
    log.info("Schwab token auto-sync thread started")


# ─────────────────────────────────────────────────────────────
# Format Translators: Schwab → MarketData JSON
# ─────────────────────────────────────────────────────────────

def _schwab_chain_to_md_format(schwab_data: dict, target_exp: str = None,
                                side_filter: str = None) -> dict:
    """Convert Schwab option chain response to MarketData.app parallel-array format.

    Schwab returns nested dicts:
      {callExpDateMap: {"2026-04-10:0": {"580.0": [{...}]}},
       putExpDateMap: {...}}

    MarketData returns parallel arrays:
      {s: "ok", optionSymbol: [...], strike: [...], side: [...], ...}
    """
    if not isinstance(schwab_data, dict):
        return {"s": "error", "errmsg": "bad schwab response"}

    status = schwab_data.get("status", "")
    if status != "SUCCESS":
        return {"s": "error", "errmsg": f"schwab status: {status}"}

    # Collect all contracts into parallel arrays
    symbols = []
    strikes = []
    sides = []
    bids = []
    asks = []
    mids = []
    lasts = []
    volumes = []
    open_interests = []
    ivs = []
    deltas = []
    gammas = []
    thetas = []
    vegas = []

    maps_to_scan = []
    if side_filter != "put":
        maps_to_scan.append(("call", schwab_data.get("callExpDateMap", {})))
    if side_filter != "call":
        maps_to_scan.append(("put", schwab_data.get("putExpDateMap", {})))

    for side_label, exp_map in maps_to_scan:
        if not isinstance(exp_map, dict):
            continue
        for exp_key, strike_map in exp_map.items():
            # exp_key format: "2026-04-10:0" — extract date part
            exp_date = exp_key.split(":")[0] if ":" in exp_key else exp_key
            if target_exp and exp_date != target_exp:
                continue
            if not isinstance(strike_map, dict):
                continue
            for strike_str, contracts in strike_map.items():
                if not isinstance(contracts, list):
                    continue
                for c in contracts:
                    if not isinstance(c, dict):
                        continue
                    sym = c.get("symbol", "")
                    bid = _safe_float(c.get("bid"), 0)
                    ask = _safe_float(c.get("ask"), 0)
                    mid = _safe_float(c.get("mark"), 0)  # Schwab uses "mark" for mid
                    if mid == 0 and bid > 0 and ask > 0:
                        mid = (bid + ask) / 2

                    symbols.append(sym)
                    strikes.append(_safe_float(c.get("strikePrice"), 0))
                    sides.append(side_label)
                    bids.append(bid)
                    asks.append(ask)
                    mids.append(round(mid, 4))
                    lasts.append(_safe_float(c.get("last"), 0))
                    volumes.append(int(_safe_float(c.get("totalVolume"), 0)))
                    open_interests.append(int(_safe_float(c.get("openInterest"), 0)))
                    ivs.append(round(_safe_float(c.get("volatility"), 0) / 100, 4))  # Schwab IV is percentage
                    deltas.append(round(_safe_float(c.get("delta"), 0), 4))
                    gammas.append(round(_safe_float(c.get("gamma"), 0), 6))
                    thetas.append(round(_safe_float(c.get("theta"), 0), 4))
                    vegas.append(round(_safe_float(c.get("vega"), 0), 4))

    if not symbols:
        return {"s": "error", "errmsg": "no contracts found"}

    return {
        "s": "ok",
        "optionSymbol": symbols,
        "strike": strikes,
        "side": sides,
        "bid": bids,
        "ask": asks,
        "mid": mids,
        "last": lasts,
        "volume": volumes,
        "openInterest": open_interests,
        "iv": ivs,
        "delta": deltas,
        "gamma": gammas,
        "theta": thetas,
        "vega": vegas,
    }


def _schwab_quote_to_spot(schwab_data: dict, ticker: str) -> float:
    """Extract spot price from Schwab get_quote response.
    Schwab returns: {TICKER: {quote: {lastPrice: ..., mark: ..., ...}}}
    """
    entry = schwab_data.get(ticker.upper(), {})
    quote = entry.get("quote", {})
    # Prefer mark (mid), then lastPrice
    for field in ("mark", "lastPrice", "bidPrice", "askPrice"):
        v = _safe_float(quote.get(field), 0)
        if v > 0:
            return v
    raise RuntimeError(f"Cannot parse Schwab spot for {ticker}")


def _schwab_bars_to_md_format(schwab_data: dict) -> dict:
    """Convert Schwab price history to MarketData intraday bar format.
    Schwab: {candles: [{open, high, low, close, volume, datetime}, ...]}
    MarketData: {s: "ok", o: [...], h: [...], l: [...], c: [...], v: [...], t: [...]}
    """
    candles = schwab_data.get("candles", [])
    if not candles:
        return {}

    return {
        "s": "ok",
        "o": [c.get("open", 0) for c in candles],
        "h": [c.get("high", 0) for c in candles],
        "l": [c.get("low", 0) for c in candles],
        "c": [c.get("close", 0) for c in candles],
        "v": [c.get("volume", 0) for c in candles],
        "t": [int(c.get("datetime", 0) / 1000) for c in candles],  # ms → unix sec
    }


def _schwab_expirations_to_list(schwab_data: dict) -> list:
    """Convert Schwab expiration chain to sorted date strings.
    Schwab: {expirationList: [{expirationDate: "2026-04-10", ...}, ...]}
    """
    exp_list = schwab_data.get("expirationList", [])
    dates = set()
    for entry in exp_list:
        d = entry.get("expirationDate", "")
        if d:
            dates.add(str(d)[:10])
    return sorted(dates)


# ─────────────────────────────────────────────────────────────
# SchwabDataProvider — implements CachedMarketData interface
# ─────────────────────────────────────────────────────────────

class SchwabDataProvider:
    """Schwab REST API data provider with TTL caching.
    Same method signatures as CachedMarketData.
    """

    def __init__(self, client=None):
        self._client = client or _init_schwab_client()
        self._spot_cache = _TTLCache(TTL_SPOT)
        self._chain_cache = _TTLCache(TTL_CHAIN)
        self._exp_cache = _TTLCache(TTL_EXPIRATIONS)
        self._candle_cache = _TTLCache(TTL_CANDLES)
        self._ohlc_cache = _TTLCache(TTL_OHLC_BARS)
        self._adv_cache = _TTLCache(TTL_ADV)
        self._vix_cache = _TTLCache(TTL_VIX)
        self._quote_cache = _TTLCache(TTL_STOCK_QUOTE)
        self._intraday_cache = _TTLCache(TTL_INTRADAY)

        # Stats
        self._lock = threading.Lock()
        self._call_counts = {}
        self._total_calls = 0
        self._errors = 0

    @property
    def available(self) -> bool:
        return self._client is not None

    def _count(self, endpoint: str):
        with self._lock:
            self._call_counts[endpoint] = self._call_counts.get(endpoint, 0) + 1
            self._total_calls += 1

    def _schwab_get(self, method_name: str, *args, **kwargs):
        """Call a schwab-py client method and return parsed JSON."""
        if not self._client:
            raise RuntimeError("Schwab client not initialised")
        method = getattr(self._client, method_name)
        resp = method(*args, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # ── Spot Price ──
    def get_spot(self, ticker: str, as_float_fn=None) -> float:
        key = ticker.upper()
        cached = self._spot_cache.get(key)
        if cached is not None:
            return cached

        self._count("spot")
        data = self._schwab_get("get_quote", key)
        spot = _schwab_quote_to_spot(data, key)
        self._spot_cache.set(key, spot)
        return spot

    # ── Option Chain ──
    def get_chain(self, ticker: str, expiration: str,
                  side: str = None, strike_limit: int = None,
                  feed: str = None) -> dict:
        filter_tag = ""
        if side:
            filter_tag += f":s={side}"
        if strike_limit:
            filter_tag += f":sl={strike_limit}"
        key = f"{ticker.upper()}:{expiration}{filter_tag}"
        cached = self._chain_cache.get(key)
        if cached is not None:
            return cached

        self._count("chain")
        from schwab.client import Client

        kwargs = {
            "symbol": ticker.upper(),
            "strategy": Client.Options.Strategy.SINGLE,
            "from_date": datetime.strptime(expiration, "%Y-%m-%d").date(),
            "to_date": datetime.strptime(expiration, "%Y-%m-%d").date(),
        }
        if side == "call":
            kwargs["contract_type"] = Client.Options.ContractType.CALL
        elif side == "put":
            kwargs["contract_type"] = Client.Options.ContractType.PUT
        else:
            kwargs["contract_type"] = Client.Options.ContractType.ALL
        if strike_limit:
            kwargs["strike_count"] = strike_limit

        raw = self._schwab_get("get_option_chain", **kwargs)
        result = _schwab_chain_to_md_format(raw, target_exp=expiration,
                                             side_filter=side)
        if result.get("s") == "ok":
            self._chain_cache.set(key, result)
        return result

    # ── Expirations ──
    def get_expirations(self, ticker: str) -> list:
        key = ticker.upper()
        cached = self._exp_cache.get(key)
        if cached is not None:
            return cached

        self._count("expirations")
        raw = self._schwab_get("get_option_expiration_chain", key)
        result = _schwab_expirations_to_list(raw)
        if result:
            self._exp_cache.set(key, result)
        return result

    # ── Daily Candles ──
    def get_daily_candles(self, ticker: str, days: int = 30) -> list:
        key = f"{ticker.upper()}:{days}"
        cached = self._candle_cache.get(key)
        if cached is not None:
            return cached

        self._count("daily_candles")
        from schwab.client import Client
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days + 10)
            raw = self._schwab_get(
                "get_price_history", ticker.upper(),
                period_type=Client.PriceHistory.PeriodType.MONTH,
                frequency_type=Client.PriceHistory.FrequencyType.DAILY,
                frequency=Client.PriceHistory.Frequency.EVERY_MINUTE,  # ignored for daily
                start_datetime=start,
                end_datetime=end,
                need_extended_hours_data=False,
            )
            candles = raw.get("candles", [])
            closes = [float(c["close"]) for c in candles if c.get("close") is not None]
            if closes:
                self._candle_cache.set(key, closes)
            return closes
        except Exception as e:
            log.warning(f"Schwab daily candles failed for {ticker}: {e}")
            return []

    # ── OHLC Bars ──
    def get_ohlc_bars(self, ticker: str, days: int = 65) -> list:
        key = f"ohlc:{ticker.upper()}:{days}"
        cached = self._ohlc_cache.get(key)
        if cached is not None:
            return cached

        self._count("ohlc_bars")
        from schwab.client import Client
        try:
            from options_exposure import OHLC as _OHLC
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days + 10)
            raw = self._schwab_get(
                "get_price_history", ticker.upper(),
                period_type=Client.PriceHistory.PeriodType.MONTH,
                frequency_type=Client.PriceHistory.FrequencyType.DAILY,
                frequency=Client.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start,
                end_datetime=end,
                need_extended_hours_data=False,
            )
            candles = raw.get("candles", [])
            bars = []
            for i, c in enumerate(candles):
                o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
                if any(x is None or x <= 0 for x in [o, h, l, cl]):
                    continue
                pc = candles[i - 1]["close"] if i > 0 else o
                bars.append(_OHLC(
                    open=float(o), high=float(h), low=float(l),
                    close=float(cl), prev_close=float(pc),
                ))
            result = bars if len(bars) >= 10 else []
            self._ohlc_cache.set(key, result)
            return result
        except Exception as e:
            log.warning(f"Schwab OHLC bars failed for {ticker}: {e}")
            return []

    # ── Intraday Bars ──
    def get_intraday_bars(self, ticker: str, resolution: int = 5,
                          countback: int = 80) -> dict:
        key = f"intra:{ticker.upper()}:{resolution}:{countback}"
        cached = self._intraday_cache.get(key)
        if cached is not None:
            return cached

        self._count("intraday_bars")
        from schwab.client import Client

        freq_map = {
            1: Client.PriceHistory.Frequency.EVERY_MINUTE,
            5: Client.PriceHistory.Frequency.EVERY_FIVE_MINUTES,
            10: Client.PriceHistory.Frequency.EVERY_TEN_MINUTES,
            15: Client.PriceHistory.Frequency.EVERY_FIFTEEN_MINUTES,
            30: Client.PriceHistory.Frequency.EVERY_THIRTY_MINUTES,
        }
        freq = freq_map.get(resolution, Client.PriceHistory.Frequency.EVERY_FIVE_MINUTES)

        try:
            # Estimate time window from countback
            minutes_needed = countback * resolution
            end = datetime.now(timezone.utc)
            # Add buffer for market hours gaps
            start = end - timedelta(minutes=int(minutes_needed * 2.5))
            raw = self._schwab_get(
                "get_price_history", ticker.upper(),
                period_type=Client.PriceHistory.PeriodType.DAY,
                frequency_type=Client.PriceHistory.FrequencyType.MINUTE,
                frequency=freq,
                start_datetime=start,
                end_datetime=end,
                need_extended_hours_data=False,
            )
            result = _schwab_bars_to_md_format(raw)
            if result and result.get("s") == "ok":
                # Trim to countback
                n = len(result.get("c", []))
                if n > countback:
                    for field in ("o", "h", "l", "c", "v", "t"):
                        if field in result:
                            result[field] = result[field][-countback:]
                self._intraday_cache.set(key, result)
                return result
            return {}
        except Exception as e:
            log.warning(f"Schwab intraday bars failed for {ticker}: {e}")
            return {}

    # ── Stock Quote ──
    def get_stock_quote(self, ticker: str) -> dict:
        key = ticker.upper()
        cached = self._quote_cache.get(key)
        if cached is not None:
            return cached

        self._count("stock_quote")
        try:
            raw = self._schwab_get("get_quote", key)
            entry = raw.get(key, {})
            quote = entry.get("quote", {})
            # Translate to MarketData-ish format for get_liquidity compatibility
            result = {
                "s": "ok",
                "bid": [_safe_float(quote.get("bidPrice"), 0)],
                "ask": [_safe_float(quote.get("askPrice"), 0)],
                "last": [_safe_float(quote.get("lastPrice"), 0)],
                "volume": [int(_safe_float(quote.get("totalVolume"), 0))],
            }
            self._quote_cache.set(key, result)
            return result
        except Exception as e:
            log.warning(f"Schwab stock quote failed for {ticker}: {e}")
            return {}

    # ── VIX Data ──
    def get_vix_data(self, as_float_fn=None) -> dict:
        """VIX via Schwab quote for $VIX.
        Falls back to CBOE CSV (same as MarketData version).
        """
        cached = self._vix_cache.get("vix")
        if cached is not None:
            return cached

        af = as_float_fn or _safe_float
        vix = 0.0
        vix9d = 0.0

        # Try Schwab quote for $VIX
        self._count("vix")
        try:
            raw = self._schwab_get("get_quote", "$VIX")
            entry = raw.get("$VIX", {})
            quote = entry.get("quote", {})
            vix = af(quote.get("lastPrice"), 0)
        except Exception as e:
            log.debug(f"Schwab VIX quote failed: {e}")

        # Fallback: CBOE CSV
        if vix <= 0:
            try:
                import requests as _req
                resp = _req.get(
                    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                    timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                lines = resp.text.strip().split("\n")
                if len(lines) > 1:
                    parts = lines[-1].strip().split(",")
                    if len(parts) >= 5:
                        vix = af(parts[4], 0)
            except Exception:
                pass

        if vix <= 0:
            return {}

        # VIX9D
        try:
            raw9d = self._schwab_get("get_quote", "$VIX9D")
            entry9d = raw9d.get("$VIX9D", {})
            quote9d = entry9d.get("quote", {})
            vix9d = af(quote9d.get("lastPrice"), 0)
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
            except Exception:
                pass

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

    # ── Liquidity ──
    def get_liquidity(self, ticker: str, as_float_fn=None) -> tuple:
        key = ticker.upper()
        cached = self._adv_cache.get(key)
        if cached is not None:
            return cached

        af = as_float_fn or _safe_float
        adv = None
        spread = None

        try:
            closes = self.get_daily_candles(ticker, days=30)
            # Need volume too — re-fetch with full data
            from schwab.client import Client
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=40)
            raw = self._schwab_get(
                "get_price_history", key,
                period_type=Client.PriceHistory.PeriodType.MONTH,
                frequency_type=Client.PriceHistory.FrequencyType.DAILY,
                frequency=Client.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start, end_datetime=end,
            )
            candles = raw.get("candles", [])
            if len(candles) >= 5:
                dvols = []
                for c in candles[-20:]:
                    cl = af(c.get("close"), 0)
                    vol = af(c.get("volume"), 0)
                    if cl > 0 and vol > 0:
                        dvols.append(cl * vol)
                if dvols:
                    adv = sum(dvols) / len(dvols)
        except Exception as e:
            log.debug(f"Schwab ADV failed for {ticker}: {e}")

        try:
            quote = self.get_stock_quote(key)
            if isinstance(quote, dict) and quote.get("s") == "ok":
                bid = af(quote.get("bid", [0])[0] if isinstance(quote.get("bid"), list) else quote.get("bid"), 0)
                ask = af(quote.get("ask", [0])[0] if isinstance(quote.get("ask"), list) else quote.get("ask"), 0)
                if bid > 0 and ask > 0 and ask > bid:
                    spread = (ask - bid) / ((ask + bid) / 2)
        except Exception:
            pass

        result = (adv, spread)
        if adv is not None or spread is not None:
            self._adv_cache.set(key, result)
        return result

    # ── Passthrough (not applicable for Schwab but needed for interface) ──
    def raw_get(self, url: str, params=None) -> dict:
        raise RuntimeError("raw_get not supported on Schwab — use MarketData fallback")

    # ── Stats ──
    def get_stats(self) -> dict:
        return {
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

    def get_api_status(self) -> dict:
        with self._lock:
            return {
                "source": "schwab",
                "calls": self._total_calls,
                "credits": 0,  # Schwab has no credit system
                "budget": 999999,
                "pct_used": 0,
                "call_breakdown": dict(self._call_counts),
                "credit_breakdown": {},
                "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "errors": self._errors,
            }

    def prune_all(self):
        for cache in (self._spot_cache, self._chain_cache, self._exp_cache,
                      self._candle_cache, self._ohlc_cache, self._adv_cache,
                      self._vix_cache, self._quote_cache, self._intraday_cache):
            cache.prune()


# ─────────────────────────────────────────────────────────────
# DataRouter — Schwab primary, MarketData fallback
# ─────────────────────────────────────────────────────────────

class DataRouter:
    """Routes data requests to Schwab first, MarketData.app on failure.
    Same interface as CachedMarketData — drop-in replacement.
    """

    def __init__(self, schwab_provider: SchwabDataProvider, md_fallback):
        """
        Args:
            schwab_provider: SchwabDataProvider instance
            md_fallback: CachedMarketData instance (existing MarketData wrapper)
        """
        self._schwab = schwab_provider
        self._md = md_fallback
        self._lock = threading.Lock()
        self._schwab_calls = 0
        self._md_fallback_calls = 0
        self._schwab_errors = 0

    def _try_schwab_first(self, method_name: str, *args, **kwargs):
        """Try Schwab, fall back to MarketData on any error."""
        if self._schwab.available:
            try:
                result = getattr(self._schwab, method_name)(*args, **kwargs)
                # Validate the result isn't empty/error for dict returns
                if isinstance(result, dict) and result.get("s") == "error":
                    raise RuntimeError(f"Schwab returned error: {result.get('errmsg', '')}")
                with self._lock:
                    self._schwab_calls += 1
                return result
            except Exception as e:
                with self._lock:
                    self._schwab_errors += 1
                log.warning(f"Schwab {method_name} failed, falling back to MarketData: {e}")

        # Fallback to MarketData
        with self._lock:
            self._md_fallback_calls += 1
        return getattr(self._md, method_name)(*args, **kwargs)

    def get_spot(self, ticker: str, as_float_fn=None) -> float:
        # Phase 2: Check streaming spot prices first (sub-second freshness)
        try:
            from schwab_stream import get_streaming_spot
            streaming = get_streaming_spot(ticker)
            if streaming is not None and streaming > 0:
                return streaming
        except ImportError:
            pass
        return self._try_schwab_first("get_spot", ticker, as_float_fn=as_float_fn)

    def get_chain(self, ticker: str, expiration: str,
                  side: str = None, strike_limit: int = None,
                  feed: str = None) -> dict:
        return self._try_schwab_first("get_chain", ticker, expiration,
                                       side=side, strike_limit=strike_limit, feed=feed)

    def get_expirations(self, ticker: str) -> list:
        return self._try_schwab_first("get_expirations", ticker)

    def get_daily_candles(self, ticker: str, days: int = 30) -> list:
        return self._try_schwab_first("get_daily_candles", ticker, days)

    def get_ohlc_bars(self, ticker: str, days: int = 65) -> list:
        return self._try_schwab_first("get_ohlc_bars", ticker, days)

    def get_intraday_bars(self, ticker: str, resolution: int = 5,
                          countback: int = 80) -> dict:
        return self._try_schwab_first("get_intraday_bars", ticker, resolution, countback)

    def get_stock_quote(self, ticker: str) -> dict:
        return self._try_schwab_first("get_stock_quote", ticker)

    def get_vix_data(self, as_float_fn=None) -> dict:
        return self._try_schwab_first("get_vix_data", as_float_fn=as_float_fn)

    def get_liquidity(self, ticker: str, as_float_fn=None) -> tuple:
        return self._try_schwab_first("get_liquidity", ticker, as_float_fn=as_float_fn)

    def raw_get(self, url: str, params=None) -> dict:
        """raw_get always goes to MarketData — Schwab has no generic REST passthrough."""
        with self._lock:
            self._md_fallback_calls += 1
        return self._md.raw_get(url, params)

    def get_stats(self) -> dict:
        stats = self._schwab.get_stats() if self._schwab.available else {}
        stats["md_fallback"] = self._md.get_stats()
        stats["routing"] = {
            "schwab_calls": self._schwab_calls,
            "md_fallback_calls": self._md_fallback_calls,
            "schwab_errors": self._schwab_errors,
            "schwab_available": self._schwab.available,
        }
        return stats

    def get_api_status(self) -> dict:
        schwab_status = self._schwab.get_api_status() if self._schwab.available else {}
        md_status = self._md.get_api_status()
        return {
            "source": "schwab+marketdata_fallback",
            "schwab": schwab_status,
            "marketdata": md_status,
            "routing": {
                "schwab_calls": self._schwab_calls,
                "md_fallback_calls": self._md_fallback_calls,
                "schwab_errors": self._schwab_errors,
                "schwab_available": self._schwab.available,
            },
        }

    def prune_all(self):
        if self._schwab.available:
            self._schwab.prune_all()
        self._md.prune_all()


# ─────────────────────────────────────────────────────────────
# Factory function — single call to replace CachedMarketData
# ─────────────────────────────────────────────────────────────

def build_data_router(md_get_fn: Callable) -> DataRouter:
    """Build the Schwab→MarketData data router.

    Usage in app.py (replace ONE line):
        # OLD: _cached_md = CachedMarketData(md_get)
        # NEW:
        from schwab_adapter import build_data_router
        _cached_md = build_data_router(md_get)

    If SCHWAB_APP_KEY is not set, Schwab is disabled and all calls
    route to MarketData — identical to the old behavior.
    """
    from api_cache import CachedMarketData
    md_fallback = CachedMarketData(md_get_fn)
    schwab = SchwabDataProvider()
    router = DataRouter(schwab, md_fallback)

    if schwab.available:
        log.info("DataRouter: Schwab PRIMARY, MarketData FALLBACK")
    else:
        log.info("DataRouter: Schwab DISABLED, MarketData ONLY")

    return router
