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
    status["spot_store"] = _spot_store.get_all()
    return status
