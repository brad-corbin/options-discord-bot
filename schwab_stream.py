# schwab_stream.py
# ═══════════════════════════════════════════════════════════════════
# Schwab WebSocket streaming + continuous flow scanning for Omega3000.
#
# Phase 2: Replaces 4x-daily flow sweeps with continuous 60s scanning
# and adds real-time spot prices via Schwab WebSocket streaming.
#
# Components:
#   SchwabStreamManager    — WebSocket Level 1 equity quotes (spots)
#   ContinuousFlowScanner  — 60s flow sweep loop (replaces 4x daily)
#
# Usage in app.py:
#   from schwab_stream import start_streaming, start_continuous_flow
#   start_streaming(_cached_md)        # real-time spots
#   start_continuous_flow(...)          # continuous flow detection
# ═══════════════════════════════════════════════════════════════════

import os
import time
import logging
import threading
import asyncio
from datetime import datetime, date, timezone, timedelta
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Shared Spot Price Store — streaming writes, get_spot reads
# ─────────────────────────────────────────────────────────────

class SpotPriceStore:
    """Thread-safe store for streaming spot prices.
    Falls back to REST if streaming data is stale (>30s old).
    """

    def __init__(self):
        self._prices = {}     # ticker → (price, timestamp)
        self._lock = threading.Lock()
        self._stale_threshold = 30  # seconds

    def update(self, ticker: str, price: float):
        with self._lock:
            self._prices[ticker.upper()] = (price, time.monotonic())

    def get(self, ticker: str) -> Optional[float]:
        """Get streaming price if fresh, else None (caller falls back to REST)."""
        with self._lock:
            entry = self._prices.get(ticker.upper())
            if entry is None:
                return None
            price, ts = entry
            if time.monotonic() - ts > self._stale_threshold:
                return None  # stale — let REST handle it
            return price

    def get_all(self) -> dict:
        """Get all current prices (for diagnostics)."""
        with self._lock:
            now = time.monotonic()
            return {
                k: {"price": v[0], "age_s": round(now - v[1], 1)}
                for k, v in self._prices.items()
            }

    @property
    def active_count(self) -> int:
        with self._lock:
            now = time.monotonic()
            return sum(1 for _, (_, ts) in self._prices.items()
                       if now - ts <= self._stale_threshold)


# Global instance — imported by schwab_adapter.py for spot price integration
_spot_store = SpotPriceStore()


def get_streaming_spot(ticker: str) -> Optional[float]:
    """Get a streaming spot price if available and fresh."""
    return _spot_store.get(ticker)


# ─────────────────────────────────────────────────────────────
# SchwabStreamManager — WebSocket Level 1 equity streaming
# ─────────────────────────────────────────────────────────────

class SchwabStreamManager:
    """Manages a persistent Schwab WebSocket connection for real-time
    Level 1 equity quotes. Runs in a daemon thread with its own
    asyncio event loop.

    Subscribes to all FLOW_TICKERS for real-time spot prices.
    Updates SpotPriceStore which the DataRouter checks before REST.
    """

    def __init__(self, schwab_client, tickers: list):
        self._client = schwab_client
        self._tickers = tickers
        self._thread = None
        self._running = False
        self._connected = False
        self._reconnect_delay = 5  # seconds, doubles on failure, max 60
        self._stats = {
            "updates_received": 0,
            "connects": 0,
            "disconnects": 0,
            "errors": 0,
            "last_update": None,
        }
        self._lock = threading.Lock()

    def start(self):
        """Start the streaming thread."""
        if self._thread and self._thread.is_alive():
            log.warning("Stream manager already running")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="schwab-stream",
            daemon=True,
        )
        self._thread.start()
        log.info(f"Schwab streaming started for {len(self._tickers)} tickers")

    def stop(self):
        self._running = False
        log.info("Schwab streaming stop requested")

    def _run_loop(self):
        """Outer loop with auto-reconnect."""
        while self._running:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._stream())
            except Exception as e:
                with self._lock:
                    self._stats["errors"] += 1
                    self._stats["disconnects"] += 1
                    self._connected = False
                log.warning(f"Schwab stream disconnected: {e}")

            if not self._running:
                break

            # Reconnect with backoff
            delay = min(self._reconnect_delay, 60)
            log.info(f"Schwab stream reconnecting in {delay}s...")
            time.sleep(delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _stream(self):
        """Connect and stream Level 1 equity quotes."""
        from schwab.streaming import StreamClient

        # Get account ID
        resp = self._client.get_account_numbers()
        resp.raise_for_status()
        accounts = resp.json()
        if not accounts:
            raise RuntimeError("No Schwab accounts found")
        account_id = accounts[0].get("hashValue", "")
        if not account_id:
            raise RuntimeError("Cannot get Schwab account hash")

        stream_client = StreamClient(self._client, account_id=account_id)
        await stream_client.login()

        with self._lock:
            self._stats["connects"] += 1
            self._connected = True
        self._reconnect_delay = 5  # reset backoff on success

        log.info(f"Schwab WebSocket connected (account: ...{account_id[-4:]})")

        # Register handler for equity quotes
        def _equity_handler(msg):
            try:
                content = msg.get("content", [])
                for item in content:
                    ticker = item.get("key", "")
                    # Try MARK first, then LAST_PRICE
                    price = item.get("MARK") or item.get("LAST_PRICE") or 0
                    if ticker and price and price > 0:
                        _spot_store.update(ticker, float(price))
                        with self._lock:
                            self._stats["updates_received"] += 1
                            self._stats["last_update"] = time.time()
            except Exception as e:
                log.debug(f"Equity handler error: {e}")

        stream_client.add_level_one_equity_handler(_equity_handler)

        # Subscribe to all tickers
        fields = [
            StreamClient.LevelOneEquityFields.SYMBOL,
            StreamClient.LevelOneEquityFields.LAST_PRICE,
            StreamClient.LevelOneEquityFields.MARK,
            StreamClient.LevelOneEquityFields.BID_PRICE,
            StreamClient.LevelOneEquityFields.ASK_PRICE,
            StreamClient.LevelOneEquityFields.TOTAL_VOLUME,
        ]
        await stream_client.level_one_equity_subs(self._tickers, fields=fields)
        log.info(f"Subscribed to Level 1 equity quotes: {len(self._tickers)} symbols")

        # Read messages until disconnected
        while self._running:
            await stream_client.handle_message()

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "tickers": len(self._tickers),
                "active_spots": _spot_store.active_count,
                **self._stats,
            }


# ─────────────────────────────────────────────────────────────
# ContinuousFlowScanner — replaces 4x daily sweeps
# ─────────────────────────────────────────────────────────────

class ContinuousFlowScanner:
    """Continuously scans flow tickers every SCAN_INTERVAL seconds.
    Replaces the 4x-daily sweep schedule with always-on detection.

    Since Schwab has no credit cost, we can afford to scan frequently.
    Uses round-robin batching to spread API load evenly.

    Architecture:
      - Divides FLOW_TICKERS into batches of BATCH_SIZE
      - Each cycle scans one batch
      - Full rotation through all tickers every ~SCAN_INTERVAL * num_batches seconds
      - e.g., 35 tickers / 5 per batch = 7 batches × 60s = 7 min full rotation
    """

    SCAN_INTERVAL = 60    # seconds between batch scans
    BATCH_SIZE = 5        # tickers per scan cycle

    def __init__(self, tickers: list, cached_md, flow_detector,
                 get_spot_fn: Callable,
                 get_expirations_fn: Callable,
                 post_fn: Callable,
                 log_conviction_fn: Callable = None,
                 get_regime_fn: Callable = None,
                 income_scan_fn: Callable = None,
                 persistent_state=None,
                 intraday_chat_id: str = None):
        self._tickers = tickers
        self._cached_md = cached_md
        self._flow = flow_detector
        self._get_spot = get_spot_fn
        self._get_exps = get_expirations_fn
        self._post = post_fn
        self._log_conviction = log_conviction_fn
        self._get_regime = get_regime_fn
        self._income_scan = income_scan_fn
        self._state = persistent_state
        self._intraday_chat_id = intraday_chat_id

        self._batch_index = 0
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._stats = {
            "scans": 0,
            "alerts": 0,
            "convictions": 0,
            "errors": 0,
            "last_scan": None,
            "last_scan_tickers": [],
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            log.warning("Continuous flow scanner already running")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop,
            name="continuous-flow",
            daemon=True,
        )
        self._thread.start()
        log.info(f"Continuous flow scanner started: {len(self._tickers)} tickers, "
                 f"batch={self.BATCH_SIZE}, interval={self.SCAN_INTERVAL}s")

    def stop(self):
        self._running = False

    def _is_market_hours(self) -> bool:
        """Check if within market hours (8:30 AM - 4:15 PM CT)."""
        try:
            import pytz
            ct = datetime.now(pytz.timezone("US/Central"))
            # Market hours: 8:30 AM to 4:15 PM CT
            market_open = ct.replace(hour=8, minute=30, second=0, microsecond=0)
            market_close = ct.replace(hour=16, minute=15, second=0, microsecond=0)
            # Weekdays only
            if ct.weekday() >= 5:
                return False
            return market_open <= ct <= market_close
        except Exception:
            return True  # default to scanning if timezone check fails

    def _get_batch(self) -> list:
        """Get next batch of tickers (round-robin)."""
        start = self._batch_index * self.BATCH_SIZE
        batch = self._tickers[start:start + self.BATCH_SIZE]
        self._batch_index = (self._batch_index + 1) % (
            (len(self._tickers) + self.BATCH_SIZE - 1) // self.BATCH_SIZE
        )
        if not batch:
            self._batch_index = 0
            batch = self._tickers[:self.BATCH_SIZE]
        return batch

    def _scan_loop(self):
        """Main scan loop — runs continuously during market hours."""
        # Initial delay to let other systems start
        time.sleep(30)

        while self._running:
            try:
                if not self._is_market_hours():
                    time.sleep(60)
                    continue

                batch = self._get_batch()
                self._scan_batch(batch)

                with self._lock:
                    self._stats["scans"] += 1
                    self._stats["last_scan"] = time.time()
                    self._stats["last_scan_tickers"] = batch

            except Exception as e:
                with self._lock:
                    self._stats["errors"] += 1
                log.warning(f"Continuous flow scan error: {e}")

            time.sleep(self.SCAN_INTERVAL)

    def _scan_batch(self, tickers: list):
        """Scan a batch of tickers for flow."""
        today = date.today()
        batch_alerts = []

        for ticker in tickers:
            try:
                # Get spot — prefer streaming, fall back to REST
                spot = None
                streaming = get_streaming_spot(ticker)
                if streaming:
                    spot = streaming
                else:
                    try:
                        spot = self._get_spot(ticker)
                    except Exception:
                        continue
                if not spot or spot <= 0:
                    continue

                # Get expirations
                exps = self._get_exps(ticker) or []

                # Categorise expirations
                short_exps = []  # 0-2 DTE
                fwd_exps = []    # 7-60 DTE
                for exp in exps:
                    try:
                        exp_dt = datetime.fromisoformat(exp).date()
                        dte = (exp_dt - today).days
                        if 0 <= dte <= 2:
                            short_exps.append((exp, dte))
                        if 7 <= dte <= 60:
                            fwd_exps.append(exp)
                    except Exception:
                        continue
                fwd_exps = fwd_exps[:4]

                # Scan short-dated (conviction plays)
                for s_exp, s_dte in short_exps[:2]:
                    self._scan_expiration(ticker, s_exp, s_dte, spot, batch_alerts)

                # Scan forward-dated (income/swing/stalk)
                for exp in fwd_exps:
                    try:
                        exp_dt = datetime.fromisoformat(exp).date()
                        dte = (exp_dt - today).days
                    except Exception:
                        dte = 30
                    self._scan_expiration(ticker, exp, dte, spot, batch_alerts)

            except Exception as e:
                log.debug(f"Continuous flow scan error for {ticker}: {e}")

        # Store for EOD summary
        if batch_alerts and hasattr(self._flow, '_eod_sweep_alerts'):
            self._flow._eod_sweep_alerts.extend(
                [a for a in batch_alerts if a.get("should_alert")])

    def _scan_expiration(self, ticker: str, exp: str, dte: int,
                         spot: float, batch_alerts: list):
        """Scan one ticker/expiration for flow signals."""
        try:
            data = self._cached_md.get_chain(
                ticker, exp, strike_limit=None, feed="cached")
            if not isinstance(data, dict) or data.get("s") != "ok":
                return

            alerts = self._flow.check_intraday_flow(ticker, exp, data, spot)
            batch_alerts.extend(alerts)

            with self._lock:
                self._stats["alerts"] += len(alerts)

            # Conviction detection
            for cp in self._flow.detect_conviction_plays(alerts, dte=max(dte, 0)):
                try:
                    route = cp.get("route", "stalk")
                    msg = self._flow.format_conviction_play(cp)
                    self._post(msg)

                    # Also post to intraday channel
                    if (route in ("immediate", "swing") and
                            self._intraday_chat_id):
                        self._post(msg, chat_id=self._intraday_chat_id)

                    # Income route → trigger ITQS scan
                    if route == "income" and self._income_scan:
                        try:
                            self._income_scan(None, cp["ticker"])
                        except Exception:
                            pass

                    # Log conviction
                    if self._log_conviction:
                        regime = self._get_regime() if self._get_regime else "UNKNOWN"
                        self._log_conviction(cp, regime=regime)

                    # Store boost for EntryValidator
                    if self._state:
                        boost = 14.0 if cp["vol_oi_ratio"] >= 10 else 7.0
                        self._state.save_conviction_boost(
                            cp["ticker"], boost, cp["trade_direction"])

                    with self._lock:
                        self._stats["convictions"] += 1

                    log.info(f"💎 CONVICTION [{route.upper()}] (continuous): "
                             f"{ticker} {cp['trade_side']} "
                             f"${cp['strike']:.0f} ({cp['dte']}DTE)")
                except Exception:
                    pass
        except Exception:
            pass

    @property
    def status(self) -> dict:
        with self._lock:
            stats = dict(self._stats)
        rotation_time = (len(self._tickers) / self.BATCH_SIZE) * self.SCAN_INTERVAL
        stats["rotation_seconds"] = round(rotation_time)
        stats["tickers"] = len(self._tickers)
        return stats


# ─────────────────────────────────────────────────────────────
# Factory functions for app.py integration
# ─────────────────────────────────────────────────────────────

_stream_manager = None
_flow_scanner = None


def start_streaming(cached_md) -> Optional[SchwabStreamManager]:
    """Start Schwab WebSocket streaming for real-time spot prices.

    Call after _cached_md is created. If Schwab is available,
    starts streaming; otherwise returns None silently.

    Usage in app.py:
        from schwab_stream import start_streaming
        start_streaming(_cached_md)
    """
    global _stream_manager

    # Check if Schwab provider is available
    schwab_provider = None
    if hasattr(cached_md, '_schwab') and cached_md._schwab.available:
        schwab_provider = cached_md._schwab
    elif hasattr(cached_md, '_client') and cached_md._client:
        schwab_provider = cached_md

    if not schwab_provider or not hasattr(schwab_provider, '_client'):
        log.info("Schwab streaming: provider not available, skipping")
        return None

    try:
        from oi_flow import FLOW_TICKERS
        _stream_manager = SchwabStreamManager(
            schwab_client=schwab_provider._client,
            tickers=list(FLOW_TICKERS),
        )
        _stream_manager.start()
        return _stream_manager
    except Exception as e:
        log.warning(f"Failed to start Schwab streaming: {e}")
        return None


def start_continuous_flow(cached_md, flow_detector,
                          get_spot_fn, get_expirations_fn,
                          post_fn, log_conviction_fn=None,
                          get_regime_fn=None, income_scan_fn=None,
                          persistent_state=None,
                          intraday_chat_id=None) -> ContinuousFlowScanner:
    """Start continuous flow scanning (replaces 4x daily sweeps).

    Usage in app.py:
        from schwab_stream import start_continuous_flow
        start_continuous_flow(
            cached_md=_cached_md,
            flow_detector=_flow_detector,
            get_spot_fn=get_spot,
            get_expirations_fn=get_expirations,
            post_fn=post_to_telegram,
            log_conviction_fn=_log_conviction_play,
            get_regime_fn=get_current_regime,
            income_scan_fn=_income_scan_fn,
            persistent_state=_persistent_state,
            intraday_chat_id=TELEGRAM_CHAT_INTRADAY,
        )
    """
    global _flow_scanner

    try:
        from oi_flow import FLOW_TICKERS
        _flow_scanner = ContinuousFlowScanner(
            tickers=list(FLOW_TICKERS),
            cached_md=cached_md,
            flow_detector=flow_detector,
            get_spot_fn=get_spot_fn,
            get_expirations_fn=get_expirations_fn,
            post_fn=post_fn,
            log_conviction_fn=log_conviction_fn,
            get_regime_fn=get_regime_fn,
            income_scan_fn=income_scan_fn,
            persistent_state=persistent_state,
            intraday_chat_id=intraday_chat_id,
        )
        _flow_scanner.start()
        return _flow_scanner
    except Exception as e:
        log.warning(f"Failed to start continuous flow scanner: {e}")
        return None


def get_stream_status() -> dict:
    """Get combined status of streaming + continuous scanner."""
    status = {}
    if _stream_manager:
        status["streaming"] = _stream_manager.status
    if _flow_scanner:
        status["continuous_flow"] = _flow_scanner.status
    if _fib_monitor:
        status["fib_monitor"] = _fib_monitor.status
    status["spot_store"] = _spot_store.get_all()
    return status


# ─────────────────────────────────────────────────────────────
# SwingFibMonitor — intraday Fib zone alerts from streaming
# ─────────────────────────────────────────────────────────────

class SwingFibMonitor:
    """Monitors streaming spots against pre-computed Fib retracement zones.

    Instead of waiting for the daily close to check Fib touches (3x/day
    scan schedule), this catches the moment price enters a Fib zone
    intraday and fires an early warning alert.

    Architecture:
      1. On startup + daily refresh: compute Fib zones for all swing tickers
         using daily bars (swing highs/lows → retracement levels)
      2. Every CHECK_INTERVAL seconds: check streaming spots against zones
      3. When price enters a zone: fire an early alert with full context
      4. Cooldown prevents spam (1 alert per ticker per zone per 4 hours)
    """

    CHECK_INTERVAL = 30      # seconds between spot checks
    ZONE_TOUCH_PCT = 1.5     # % distance to count as "in the zone"
    ALERT_COOLDOWN = 14400   # 4 hours between same zone alerts
    REFRESH_HOUR_CT = 8      # refresh Fib zones at 8 AM CT daily

    def __init__(self, daily_bars_fn: Callable, post_fn: Callable,
                 tickers: list, enqueue_fn: Callable = None):
        """
        Args:
            daily_bars_fn: callable(ticker, days) -> list[{date, o, h, l, c, v}]
            post_fn: callable(message) — post to Telegram
            tickers: list of tickers to monitor
            enqueue_fn: optional — enqueue swing signal for full evaluation
        """
        self._bars_fn = daily_bars_fn
        self._post = post_fn
        self._enqueue = enqueue_fn
        self._tickers = tickers
        self._fib_zones = {}       # ticker → {fibs, swing_high, swing_low, weekly_bull, ...}
        self._cooldowns = {}       # "ticker:level:direction" → timestamp
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._stats = {
            "zones_computed": 0,
            "alerts_fired": 0,
            "checks": 0,
            "last_check": None,
            "last_refresh": None,
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="fib-monitor", daemon=True)
        self._thread.start()
        log.info(f"Swing Fib monitor started: {len(self._tickers)} tickers")

    def stop(self):
        self._running = False

    def _run(self):
        # Initial zone computation
        time.sleep(45)  # let streaming establish first
        self._refresh_zones()

        last_refresh_day = ""
        while self._running:
            try:
                if not self._is_market_hours():
                    time.sleep(60)
                    continue

                # Daily refresh at REFRESH_HOUR_CT
                try:
                    import pytz
                    ct = datetime.now(pytz.timezone("US/Central"))
                    today_str = ct.strftime("%Y-%m-%d")
                    if (ct.hour == self.REFRESH_HOUR_CT and
                            today_str != last_refresh_day):
                        self._refresh_zones()
                        last_refresh_day = today_str
                except Exception:
                    pass

                self._check_spots()
                with self._lock:
                    self._stats["checks"] += 1
                    self._stats["last_check"] = time.time()

            except Exception as e:
                log.debug(f"Fib monitor error: {e}")

            time.sleep(self.CHECK_INTERVAL)

    def _is_market_hours(self) -> bool:
        try:
            import pytz
            ct = datetime.now(pytz.timezone("US/Central"))
            if ct.weekday() >= 5:
                return False
            market_open = ct.replace(hour=8, minute=30, second=0, microsecond=0)
            market_close = ct.replace(hour=15, minute=15, second=0, microsecond=0)
            return market_open <= ct <= market_close
        except Exception:
            return True

    def _refresh_zones(self):
        """Compute Fib zones for all tickers from daily bars."""
        from swing_scanner import (
            _find_pivots, compute_fib_levels,
            SWING_FIB_LOOKBACK, _ema,
        )
        from trading_rules import (
            SWING_WEEKLY_EMA_FAST, SWING_WEEKLY_EMA_SLOW,
            SWING_WEEKLY_MIN_SEP_PCT,
        )

        computed = 0
        for ticker in self._tickers:
            try:
                bars = self._bars_fn(ticker, 310)
                if not bars or len(bars) < 60:
                    continue

                highs = [b["h"] for b in bars]
                lows = [b["l"] for b in bars]
                closes = [b["c"] for b in bars]
                spot = closes[-1]

                # Find swing points
                pivot_len = max(2, round(SWING_FIB_LOOKBACK / 5))
                swing_highs, swing_lows = _find_pivots(highs, lows, pivot_len)
                if not swing_highs or not swing_lows:
                    continue

                last_sh = swing_highs[-1][1]
                last_sl = swing_lows[-1][1]
                fibs = compute_fib_levels(last_sh, last_sl)

                # Weekly trend context
                from swing_scanner import _aggregate_weekly
                weekly_bars = _aggregate_weekly(bars)
                weekly_bull = False
                weekly_bear = False
                if len(weekly_bars) >= SWING_WEEKLY_EMA_SLOW + 2:
                    w_closes = [w["c"] for w in weekly_bars]
                    w_ema_f = _ema(w_closes, SWING_WEEKLY_EMA_FAST)
                    w_ema_s = _ema(w_closes, SWING_WEEKLY_EMA_SLOW)
                    wef, wes = w_ema_f[-1], w_ema_s[-1]
                    w_gap = abs(wef - wes)
                    w_min_sep = w_closes[-1] * (SWING_WEEKLY_MIN_SEP_PCT / 100)
                    weekly_bull = wef > wes and w_gap >= w_min_sep
                    weekly_bear = wef < wes and w_gap >= w_min_sep

                self._fib_zones[ticker] = {
                    "fibs": fibs,
                    "swing_high": last_sh,
                    "swing_low": last_sl,
                    "weekly_bull": weekly_bull,
                    "weekly_bear": weekly_bear,
                    "spot_at_compute": spot,
                }
                computed += 1
            except Exception as e:
                log.debug(f"Fib zone computation failed for {ticker}: {e}")

        with self._lock:
            self._stats["zones_computed"] = computed
            self._stats["last_refresh"] = time.time()
        log.info(f"Fib zones refreshed: {computed}/{len(self._tickers)} tickers")

    def _check_spots(self):
        """Check streaming spots against Fib zones."""
        touch_pct = self.ZONE_TOUCH_PCT / 100
        now = time.time()

        for ticker, zone in self._fib_zones.items():
            spot = get_streaming_spot(ticker)
            if not spot or spot <= 0:
                continue

            fibs = zone["fibs"]

            # Check bull Fib levels (price pulling back to support)
            if zone.get("weekly_bull"):
                for name, key in [("50.0", "bull_500"), ("61.8", "bull_618"),
                                  ("38.2", "bull_382"), ("78.6", "bull_786")]:
                    level = fibs.get(key, 0)
                    if level <= 0:
                        continue
                    dist_pct = abs(spot - level) / level
                    if dist_pct <= touch_pct and spot >= level * 0.99:
                        self._fire_fib_alert(ticker, spot, name, level,
                                             "bull", dist_pct * 100, zone, now)
                        break  # only fire closest level

            # Check bear Fib levels (price rallying to resistance)
            if zone.get("weekly_bear"):
                for name, key in [("50.0", "bear_500"), ("61.8", "bear_618"),
                                  ("38.2", "bear_382"), ("78.6", "bear_786")]:
                    level = fibs.get(key, 0)
                    if level <= 0:
                        continue
                    dist_pct = abs(spot - level) / level
                    if dist_pct <= touch_pct and spot <= level * 1.01:
                        self._fire_fib_alert(ticker, spot, name, level,
                                             "bear", dist_pct * 100, zone, now)
                        break

    def _fire_fib_alert(self, ticker: str, spot: float, fib_name: str,
                        fib_level: float, direction: str, dist_pct: float,
                        zone: dict, now: float):
        """Fire an early Fib zone alert if not in cooldown."""
        cooldown_key = f"{ticker}:{fib_name}:{direction}"
        last_fired = self._cooldowns.get(cooldown_key, 0)
        if now - last_fired < self.ALERT_COOLDOWN:
            return

        self._cooldowns[cooldown_key] = now

        dir_emoji = "🐂" if direction == "bull" else "🐻"
        trend = "WEEKLY BULL ✅" if zone.get("weekly_bull") else "WEEKLY BEAR 🔴"
        sh = zone.get("swing_high", 0)
        sl = zone.get("swing_low", 0)

        msg = (
            f"🪜 SWING FIB ALERT — {ticker} {dir_emoji}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Price ${spot:.2f} entering {fib_name}% zone (${fib_level:.2f})\n"
            f"   Distance: {dist_pct:.1f}% from level\n"
            f"📊 Trend: {trend}\n"
            f"📐 Swing range: ${sl:.2f} → ${sh:.2f}\n"
            f"\n"
            f"⏰ EARLY WARNING — daily scanner will confirm at close.\n"
            f"   Watch for wick rejection or hold at this level.\n"
            f"   Use /checkswing {ticker} for full analysis."
        )

        try:
            self._post(msg)
            with self._lock:
                self._stats["alerts_fired"] += 1
            log.info(f"Fib alert: {ticker} {direction} at {fib_name}% "
                     f"(${spot:.2f} near ${fib_level:.2f})")
        except Exception as e:
            log.warning(f"Fib alert failed for {ticker}: {e}")

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._stats)


# ─────────────────────────────────────────────────────────────
# Fib Monitor Factory
# ─────────────────────────────────────────────────────────────

_fib_monitor = None


def start_fib_monitor(daily_bars_fn: Callable, post_fn: Callable,
                      enqueue_fn: Callable = None) -> Optional[SwingFibMonitor]:
    """Start intraday Fib zone monitoring.

    Usage in app.py:
        from schwab_stream import start_fib_monitor
        start_fib_monitor(
            daily_bars_fn=lambda t, d: _schwab_daily_bars(t, d),
            post_fn=post_to_telegram,
        )
    """
    global _fib_monitor
    try:
        from trading_rules import SWING_WATCHLIST
        _fib_monitor = SwingFibMonitor(
            daily_bars_fn=daily_bars_fn,
            post_fn=post_fn,
            tickers=sorted(SWING_WATCHLIST),
            enqueue_fn=enqueue_fn,
        )
        _fib_monitor.start()
        return _fib_monitor
    except Exception as e:
        log.warning(f"Failed to start Fib monitor: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# PotterBoxBreakMonitor — real-time box break/reclaim detection
# ─────────────────────────────────────────────────────────────

class PotterBoxBreakMonitor:
    """Monitors streaming spots against active Potter Box boundaries.

    Potter Box detection runs 2-3x daily on daily bars (boxes are multi-day
    patterns). But BREAKS happen intraday — and with the old 2x/day scan,
    you wouldn't know until hours later.

    This monitor checks streaming spots every 30s against stored active boxes
    in Redis (written by the Potter Box scanner). When price breaks a floor
    or ceiling, it fires an immediate alert.
    """

    CHECK_INTERVAL = 30
    ALERT_COOLDOWN = 3600   # 1 hour between same break alerts
    BREAK_CONFIRM_PCT = 0.15  # must break by 0.15% to confirm (not just touch)

    def __init__(self, potter_box_scanner, post_fn: Callable, tickers: list):
        self._potter = potter_box_scanner
        self._post = post_fn
        self._tickers = tickers
        self._cooldowns = {}   # "ticker:break_type" → timestamp
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._stats = {"checks": 0, "breaks_detected": 0, "last_check": None}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="potter-break-monitor", daemon=True)
        self._thread.start()
        log.info(f"Potter Box break monitor started: {len(self._tickers)} tickers")

    def stop(self):
        self._running = False

    def _run(self):
        time.sleep(60)  # let Potter Box AM scan populate Redis first
        while self._running:
            try:
                if not self._is_market_hours():
                    time.sleep(60)
                    continue
                self._check_breaks()
                with self._lock:
                    self._stats["checks"] += 1
                    self._stats["last_check"] = time.time()
            except Exception as e:
                log.debug(f"Potter break monitor error: {e}")
            time.sleep(self.CHECK_INTERVAL)

    def _is_market_hours(self) -> bool:
        try:
            import pytz
            ct = datetime.now(pytz.timezone("US/Central"))
            if ct.weekday() >= 5:
                return False
            return ct.replace(hour=8, minute=30) <= ct <= ct.replace(hour=15, minute=15)
        except Exception:
            return True

    def _check_breaks(self):
        now = time.time()
        for ticker in self._tickers:
            spot = get_streaming_spot(ticker)
            if not spot or spot <= 0:
                continue

            # Get active box from Potter Box scanner's Redis cache
            try:
                box = self._potter.get_active_box(ticker)
                if not box:
                    continue
            except Exception:
                continue

            floor = box.get("floor", 0)
            roof = box.get("roof", 0)
            if floor <= 0 or roof <= 0 or floor >= roof:
                continue

            break_margin = spot * (self.BREAK_CONFIRM_PCT / 100)

            # Check ceiling break (breakout)
            if spot > roof + break_margin:
                self._fire_break(ticker, spot, "BREAKOUT", roof, floor, box, now)

            # Check floor break (breakdown)
            elif spot < floor - break_margin:
                self._fire_break(ticker, spot, "BREAKDOWN", roof, floor, box, now)

            # Check reclaim from outside
            elif floor <= spot <= roof:
                # If we previously alerted a break, reclaim is noteworthy
                breakout_key = f"{ticker}:BREAKOUT"
                breakdown_key = f"{ticker}:BREAKDOWN"
                if (breakout_key in self._cooldowns or
                        breakdown_key in self._cooldowns):
                    self._fire_break(ticker, spot, "RECLAIM", roof, floor, box, now)

    def _fire_break(self, ticker: str, spot: float, break_type: str,
                    roof: float, floor: float, box: dict, now: float):
        cooldown_key = f"{ticker}:{break_type}"
        last_fired = self._cooldowns.get(cooldown_key, 0)
        if now - last_fired < self.ALERT_COOLDOWN:
            return

        self._cooldowns[cooldown_key] = now
        box_width = roof - floor
        box_pct = (box_width / floor) * 100 if floor > 0 else 0
        bars_in_box = box.get("duration_bars", "?")

        if break_type == "BREAKOUT":
            emoji = "🚀"
            dist = ((spot - roof) / roof) * 100
            action = f"Price ${spot:.2f} broke ABOVE ceiling ${roof:.2f} (+{dist:.2f}%)"
            guidance = "Watch for continuation. Failed breakout = short opportunity."
        elif break_type == "BREAKDOWN":
            emoji = "💥"
            dist = ((floor - spot) / floor) * 100
            action = f"Price ${spot:.2f} broke BELOW floor ${floor:.2f} (-{dist:.2f}%)"
            guidance = "Watch for continuation. Failed breakdown = long opportunity."
        else:  # RECLAIM
            emoji = "🔄"
            action = f"Price ${spot:.2f} RECLAIMED inside box ${floor:.2f}-${roof:.2f}"
            guidance = "Break failed — range trade logic applies. Fade the edges."

        msg = (
            f"{emoji} POTTER BOX {break_type} — {ticker}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{action}\n"
            f"📦 Box: ${floor:.2f} - ${roof:.2f} ({box_pct:.1f}% wide, {bars_in_box} bars)\n"
            f"\n"
            f"💡 {guidance}"
        )

        try:
            self._post(msg)
            with self._lock:
                self._stats["breaks_detected"] += 1
            log.info(f"Potter Box {break_type}: {ticker} ${spot:.2f} "
                     f"(box ${floor:.2f}-${roof:.2f})")
        except Exception as e:
            log.warning(f"Potter break alert failed for {ticker}: {e}")

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._stats)


# ─────────────────────────────────────────────────────────────
# Potter Box Break Monitor Factory
# ─────────────────────────────────────────────────────────────

_box_monitor = None


def start_box_break_monitor(potter_box_scanner, post_fn: Callable) -> Optional[PotterBoxBreakMonitor]:
    """Start real-time Potter Box break monitoring.

    Usage in app.py:
        from schwab_stream import start_box_break_monitor
        start_box_break_monitor(_potter_box, post_to_telegram)
    """
    global _box_monitor
    try:
        from oi_flow import FLOW_TICKERS
        _box_monitor = PotterBoxBreakMonitor(
            potter_box_scanner=potter_box_scanner,
            post_fn=post_fn,
            tickers=list(FLOW_TICKERS),
        )
        _box_monitor.start()
        return _box_monitor
    except Exception as e:
        log.warning(f"Failed to start Potter Box break monitor: {e}")
        return None
