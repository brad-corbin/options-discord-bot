# income_wiring.py
# ═══════════════════════════════════════════════════════════════════
# Income Scanner — App Wiring
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Contains the app-level functions that connect the income scanner
# to MarketData, Telegram, and the regime system.
#
# Usage from app.py:
#   from income_wiring import create_income_handlers
#   _income_scan_fn, _income_score_fn = create_income_handlers(
#       chain_fn=_cached_md.get_chain,
#       expirations_fn=get_expirations,
#       ohlcv_fn=get_daily_candles_ohlcv,  # or a wrapper
#       regime_fn=lambda: regime_detector.get_regime_package(),
#       post_fn=post_to_telegram,
#   )
# ═══════════════════════════════════════════════════════════════════

import logging
import threading
from typing import Optional, Callable

log = logging.getLogger(__name__)


def create_income_handlers(
    chain_fn: Callable,
    expirations_fn: Callable,
    ohlcv_fn: Callable,
    regime_fn: Callable,
    post_fn: Callable,
    flow_fn: Callable = None,
):
    """
    Factory that creates the two Telegram command handlers.
    Returns (income_scan_fn, income_score_fn) closures.

    chain_fn:       _cached_md.get_chain(ticker, expiry, side=)
    expirations_fn: get_expirations(ticker)
    ohlcv_fn:       function(ticker, days=) → {open, high, low, close, volume}
    regime_fn:      function() → regime package dict
    post_fn:        post_to_telegram(text)
    flow_fn:        function(ticker, strike, trade_type, expiry) → flow_data dict
    """

    def income_scan_fn(chat_id: str, ticker: Optional[str] = None):
        """
        Run income scan and post results to Telegram.
        If ticker is provided, scan that one ticker only.
        Otherwise scan the full default universe.
        """
        try:
            from income_scanner import (
                run_income_scan, scan_ticker_income,
                format_income_alert, INCOME_TICKERS,
            )

            pkg = regime_fn()
            tickers = [ticker] if ticker else None

            def _run():
                try:
                    if ticker:
                        # Single ticker scan
                        opps = scan_ticker_income(
                            ticker, pkg,
                            ohlcv_fn=ohlcv_fn,
                            chain_fn=chain_fn,
                            expirations_fn=expirations_fn,
                            flow_fn=flow_fn,
                        )
                        if not opps:
                            post_fn(f"📊 Income scan {ticker}: no qualifying opportunities found.",
                                    chat_id=chat_id)
                            return

                        # Post top 3 opportunities
                        for opp in opps[:3]:
                            if opp["itqs"]["grade"] != "F":
                                post_fn(format_income_alert(opp), chat_id=chat_id)

                        if not any(o["itqs"]["grade"] != "F" for o in opps):
                            post_fn(f"📊 Income scan {ticker}: all opportunities below threshold (grade F).",
                                    chat_id=chat_id)
                    else:
                        # Full universe scan
                        run_income_scan(
                            regime_package=pkg,
                            ohlcv_fn=ohlcv_fn,
                            chain_fn=chain_fn,
                            expirations_fn=expirations_fn,
                            flow_fn=flow_fn,
                            notify_fn=lambda msg: post_fn(msg, chat_id=chat_id),
                        )

                except Exception as e:
                    log.error(f"Income scan error: {e}", exc_info=True)
                    post_fn(f"⚠️ Income scan error: {type(e).__name__}: {str(e)[:120]}",
                            chat_id=chat_id)

            threading.Thread(target=_run, daemon=True).start()

        except Exception as e:
            log.error(f"Income scan setup error: {e}")
            post_fn(f"⚠️ Income scan failed: {e}", chat_id=chat_id)

    def income_score_fn(
        chat_id: str,
        ticker: str,
        trade_type: str,
        short_strike: float,
        width: float,
        credit: float,
        expiry: Optional[str] = None,
    ):
        """
        Score a specific trade and post the scorecard to Telegram.
        """
        try:
            from income_scanner import score_trade, format_scorecard

            pkg = regime_fn()

            def _run():
                try:
                    result = score_trade(
                        ticker=ticker,
                        trade_type=trade_type,
                        short_strike=short_strike,
                        width=width,
                        credit=credit,
                        regime_package=pkg,
                        ohlcv_fn=ohlcv_fn,
                        chain_fn=chain_fn,
                        expirations_fn=expirations_fn,
                        expiry=expiry,
                        flow_fn=flow_fn,
                    )
                    post_fn(format_scorecard(result), chat_id=chat_id)

                except Exception as e:
                    log.error(f"Income score error: {e}", exc_info=True)
                    post_fn(f"⚠️ Score error: {type(e).__name__}: {str(e)[:120]}",
                            chat_id=chat_id)

            threading.Thread(target=_run, daemon=True).start()

        except Exception as e:
            log.error(f"Income score setup error: {e}")
            post_fn(f"⚠️ Score failed: {e}", chat_id=chat_id)

    return income_scan_fn, income_score_fn


def create_ohlcv_wrapper(daily_candle_fn: Callable, md_get_fn: Callable = None,
                         schwab_bars_fn: Callable = None):
    """
    Creates an ohlcv_fn that returns full OHLCV dict.
    v7.0: Schwab daily bars primary (free, reliable)
    Fallback 1: MarketData daily candles
    Fallback 2: yfinance
    Last resort: closes-only degraded mode
    """

    def ohlcv_fn(ticker, days=250):
        # Strategy 0 (v7.0): Schwab daily bars — free, most reliable
        if schwab_bars_fn:
            try:
                bars = schwab_bars_fn(ticker, days)
                if bars and len(bars) >= 30:
                    return {
                        "open": [b["o"] for b in bars],
                        "high": [b["h"] for b in bars],
                        "low": [b["l"] for b in bars],
                        "close": [b["c"] for b in bars],
                        "volume": [b.get("v", 0) for b in bars],
                    }
            except Exception as e:
                log.debug(f"Schwab OHLCV failed for {ticker}: {e}")

        # Strategy 1: MarketData daily candles (already paid for, never rate-limits)
        if md_get_fn:
            try:
                from datetime import datetime, timezone, timedelta
                from_date = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")
                data = md_get_fn(
                    f"https://api.marketdata.app/v1/stocks/candles/daily/{ticker.upper()}/",
                    {"from": from_date, "countback": days + 5},
                )
                if isinstance(data, dict) and data.get("s") == "ok":
                    opens = data.get("o", [])
                    highs = data.get("h", [])
                    lows = data.get("l", [])
                    closes = data.get("c", [])
                    volumes = data.get("v", [])
                    n = min(len(opens), len(highs), len(lows), len(closes))
                    if n >= 30:
                        return {
                            "open": [float(x) for x in opens[:n]],
                            "high": [float(x) for x in highs[:n]],
                            "low": [float(x) for x in lows[:n]],
                            "close": [float(x) for x in closes[:n]],
                            "volume": [float(x) for x in (volumes[:n] if len(volumes) >= n else [0]*n)],
                        }
            except Exception as e:
                log.debug(f"MarketData OHLCV failed for {ticker}: {e}")

        # Strategy 2: yfinance (can rate-limit and hang)
        try:
            import yfinance as yf
            data = yf.download(ticker, period="1y", interval="1d",
                               progress=False, multi_level_index=False, timeout=15)
            if data is not None and len(data) >= 30:
                return {
                    "open": data["Open"].tolist(),
                    "high": data["High"].tolist(),
                    "low": data["Low"].tolist(),
                    "close": data["Close"].tolist(),
                    "volume": data["Volume"].tolist(),
                }
        except Exception as e:
            log.debug(f"yfinance OHLCV failed for {ticker}: {e}")

        # Strategy 3: closes-only degraded mode
        try:
            closes = daily_candle_fn(ticker, days=days)
            if closes and len(closes) >= 30:
                return {
                    "open": closes, "high": closes, "low": closes,
                    "close": closes, "volume": [0] * len(closes),
                }
        except Exception:
            pass

        return None

    return ohlcv_fn
