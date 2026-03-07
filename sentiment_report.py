# sentiment_report.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Phase 2B — Holdings Sentiment Report
#   - EMA trend detection (8/21 EMA cross + price position)
#   - VWAP position (above/below)
#   - Volume analysis (current vs 20-day average)
#   - Bullish / Neutral / Bearish bucketing
#   - P/L overlay from portfolio data layer
#
# Uses md_get from app.py (passed in at call time to avoid circular imports).
# Designed to run on-demand (/holdings) or scheduled (cron at 2 PM ET).

import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from portfolio import (
    get_all_holdings,
    calc_holding_pnl,
    get_open_options,
    calc_open_options_pnl,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────

EMA_FAST       = 8       # fast EMA period
EMA_SLOW       = 21      # slow EMA period
VOLUME_LOOKBACK = 20     # days for average volume
CANDLE_FETCH    = 60     # fetch 60 daily candles (enough for EMA 21 + buffer)

# Scoring thresholds
# Each signal contributes a score: +1 bullish, -1 bearish, 0 neutral
# Total score: >= 2 = bullish, <= -2 = bearish, else neutral
BULL_THRESHOLD =  2
BEAR_THRESHOLD = -2


# ─────────────────────────────────────────────────────────
# EMA CALCULATION
# ─────────────────────────────────────────────────────────

def _calc_ema(prices: list, period: int) -> list:
    """
    Calculate EMA for a list of prices.
    Returns list of same length (early values use SMA seed).
    """
    if not prices or len(prices) < period:
        return []

    ema = []
    # Seed with SMA of first `period` values
    sma = sum(prices[:period]) / period
    ema.extend([None] * (period - 1))
    ema.append(sma)

    multiplier = 2.0 / (period + 1)
    for i in range(period, len(prices)):
        val = (prices[i] - ema[-1]) * multiplier + ema[-1]
        ema.append(val)

    return ema


# ─────────────────────────────────────────────────────────
# CANDLE FETCHING
# ─────────────────────────────────────────────────────────

def _fetch_candles(ticker: str, md_get: Callable, days: int = CANDLE_FETCH) -> dict:
    """
    Fetch daily candles from MarketData API.
    Returns dict with 'close', 'high', 'low', 'volume' lists (oldest first),
    or None on failure.

    md_get is the same function from app.py:
        md_get(url, params) → dict
    """
    ticker = ticker.strip().upper()
    # Calculate date range
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(days * 1.6))  # pad for weekends/holidays

    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/daily/{ticker}/",
            {
                "from": start.isoformat(),
                "to":   end.isoformat(),
            },
        )

        if not isinstance(data, dict) or data.get("s") != "ok":
            log.warning(f"Candle fetch failed for {ticker}: {data.get('s', 'unknown')}")
            return None

        closes  = data.get("c", [])
        highs   = data.get("h", [])
        lows    = data.get("l", [])
        volumes = data.get("v", [])

        if not closes or len(closes) < EMA_SLOW + 2:
            log.warning(f"Not enough candle data for {ticker}: {len(closes)} bars")
            return None

        return {
            "close":  closes,
            "high":   highs,
            "low":    lows,
            "volume": volumes,
        }

    except Exception as e:
        log.warning(f"Candle fetch error for {ticker}: {e}")
        return None


def _fetch_quote(ticker: str, md_get: Callable) -> dict:
    """
    Fetch current quote (last price + VWAP if available).
    Returns dict with 'last', 'vwap' keys, or None on failure.
    """
    ticker = ticker.strip().upper()
    try:
        data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")

        last = None
        for field in ("last", "mid", "bid", "ask"):
            v = data.get(field)
            if v is not None:
                try:
                    v = float(v[0]) if isinstance(v, list) else float(v)
                    if v > 0:
                        last = v
                        break
                except (ValueError, TypeError, IndexError):
                    continue

        # VWAP may not be in quote response — that's OK
        vwap = None
        v = data.get("vwap")
        if v is not None:
            try:
                vwap = float(v[0]) if isinstance(v, list) else float(v)
            except (ValueError, TypeError, IndexError):
                pass

        return {"last": last, "vwap": vwap}

    except Exception as e:
        log.warning(f"Quote fetch error for {ticker}: {e}")
        return None


# ─────────────────────────────────────────────────────────
# SENTIMENT SCORING
# ─────────────────────────────────────────────────────────

def analyze_ticker(ticker: str, md_get: Callable) -> dict:
    """
    Run full sentiment analysis on a single ticker.
    Returns dict with:
      - current price, EMA/VWAP/volume signals
      - individual scores and total score
      - sentiment bucket ("bullish", "neutral", "bearish")
    """
    ticker = ticker.strip().upper()
    result = {
        "ticker":    ticker,
        "price":     None,
        "sentiment": "unknown",
        "score":     0,
        "signals":   {},
        "error":     None,
    }

    # Fetch data
    candles = _fetch_candles(ticker, md_get)
    quote   = _fetch_quote(ticker, md_get)

    if candles is None or quote is None or quote.get("last") is None:
        result["error"] = "data unavailable"
        return result

    closes = candles["close"]
    volumes = candles["volume"]
    price = quote["last"]
    vwap = quote.get("vwap")

    result["price"] = price

    # ── EMA TREND ──
    ema_fast = _calc_ema(closes, EMA_FAST)
    ema_slow = _calc_ema(closes, EMA_SLOW)

    ema_score = 0
    ema_label = "🟡"

    if ema_fast and ema_slow and ema_fast[-1] and ema_slow[-1]:
        fast_val = ema_fast[-1]
        slow_val = ema_slow[-1]

        price_above_fast = price > fast_val
        fast_above_slow  = fast_val > slow_val

        if price_above_fast and fast_above_slow:
            ema_score = 1
            ema_label = "✅"
        elif not price_above_fast and not fast_above_slow:
            ema_score = -1
            ema_label = "❌"
        # else: mixed = 0, neutral

        result["signals"]["ema_fast"] = round(fast_val, 2)
        result["signals"]["ema_slow"] = round(slow_val, 2)

    result["signals"]["ema_score"] = ema_score
    result["signals"]["ema_label"] = ema_label

    # ── VWAP POSITION ──
    vwap_score = 0
    vwap_label = "❌"

    if vwap and vwap > 0:
        if price > vwap:
            vwap_score = 1
            vwap_label = "✅"
        else:
            vwap_score = -1
            vwap_label = "❌"
        result["signals"]["vwap"] = round(vwap, 2)
    else:
        # No VWAP available — use price vs yesterday's close as rough proxy
        if len(closes) >= 2:
            prev_close = closes[-2]
            if price > prev_close:
                vwap_score = 1
                vwap_label = "✅"
            else:
                vwap_score = -1
                vwap_label = "❌"
            result["signals"]["vwap_proxy"] = round(prev_close, 2)

    result["signals"]["vwap_score"] = vwap_score
    result["signals"]["vwap_label"] = vwap_label

    # ── VOLUME ANALYSIS ──
    vol_score = 0
    vol_ratio = 0.0
    vol_label = ""

    if volumes and len(volumes) >= VOLUME_LOOKBACK + 1:
        # Current day volume vs 20-day average
        current_vol = volumes[-1] if volumes[-1] else 0
        avg_vol = sum(v for v in volumes[-(VOLUME_LOOKBACK + 1):-1] if v) / VOLUME_LOOKBACK

        if avg_vol > 0:
            vol_ratio = current_vol / avg_vol

            if vol_ratio >= 1.2:
                vol_score = 1   # above-average volume = conviction
            elif vol_ratio <= 0.6:
                vol_score = -1  # very low volume = fading
            # else: normal volume = neutral

            vol_label = f"Vol{'+'if vol_ratio >= 1 else ''}{vol_ratio:.1f}x"
        else:
            vol_label = "Vol—"
    else:
        vol_label = "Vol—"

    result["signals"]["vol_score"] = vol_score
    result["signals"]["vol_ratio"] = round(vol_ratio, 2)
    result["signals"]["vol_label"] = vol_label

    # ── TOTAL SCORE + BUCKET ──
    total = ema_score + vwap_score + vol_score
    result["score"] = total

    if total >= BULL_THRESHOLD:
        result["sentiment"] = "bullish"
    elif total <= BEAR_THRESHOLD:
        result["sentiment"] = "bearish"
    else:
        result["sentiment"] = "neutral"

    return result


# ─────────────────────────────────────────────────────────
# FULL PORTFOLIO SENTIMENT REPORT
# ─────────────────────────────────────────────────────────

def generate_sentiment_report(md_get: Callable, get_spot: Callable = None) -> str:
    """
    Run sentiment analysis on ALL holdings.
    Returns formatted Telegram message string.

    md_get:   MarketData API getter (from app.py)
    get_spot: optional spot price fetcher — if None, uses prices from analysis
    """
    holdings = get_all_holdings()
    if not holdings:
        return "📊 No holdings to analyze. Use /hold add TICKER SHARES @PRICE"

    # Analyze each holding
    bullish  = []
    neutral  = []
    bearish  = []
    errors   = []

    price_map = {}  # for P/L calculation

    for ticker in sorted(holdings.keys()):
        analysis = analyze_ticker(ticker, md_get)

        if analysis.get("error"):
            errors.append(ticker)
            continue

        price = analysis["price"]
        if price:
            price_map[ticker] = price

        # Calculate P/L for this holding
        pnl_data = calc_holding_pnl(ticker, price) if price else None

        entry = _format_holding_line(ticker, analysis, pnl_data, holdings[ticker])

        bucket = analysis["sentiment"]
        if bucket == "bullish":
            bullish.append(entry)
        elif bucket == "bearish":
            bearish.append(entry)
        else:
            neutral.append(entry)

    # Build report
    now_str = datetime.now(timezone.utc).strftime("%I:%M %p UTC")
    lines = [f"📊 HOLDINGS SENTIMENT — {now_str}\n"]

    if bullish:
        lines.append("🟢 BULLISH:")
        lines.extend(f"  {e}" for e in bullish)
        lines.append("")

    if neutral:
        lines.append("🟡 NEUTRAL:")
        lines.extend(f"  {e}" for e in neutral)
        lines.append("")

    if bearish:
        lines.append("🔴 BEARISH:")
        lines.extend(f"  {e}" for e in bearish)
        lines.append("")

    if errors:
        lines.append(f"⚠️ Data unavailable: {', '.join(errors)}")
        lines.append("")

    # Portfolio totals
    total_unrealized = 0.0
    total_opt_income = 0.0
    for ticker, price in price_map.items():
        pnl = calc_holding_pnl(ticker, price)
        if "error" not in pnl:
            total_unrealized += pnl["unrealized"]
            total_opt_income += pnl["opt_income"]

    combined = total_unrealized + total_opt_income

    open_opts = get_open_options()
    if open_opts:
        lines.append(f"Open Options: {len(open_opts)} positions")

    lines.append(f"Total Unrealized: {_fmt_money(total_unrealized)}")
    if total_opt_income != 0:
        lines.append(f"Options Income: {_fmt_money(total_opt_income)}")
    lines.append(f"Portfolio P/L: {_fmt_money(combined)}")

    return "\n".join(lines)


def _format_holding_line(ticker: str, analysis: dict, pnl_data: dict,
                         holding: dict) -> str:
    """
    Format one holding line for the sentiment report:
      AAPL  $192.30  EMA✅ VWAP✅ Vol+1.2x  +$680 (+3.7%)
    """
    signals = analysis.get("signals", {})
    price   = analysis.get("price", 0)

    ema_lbl  = signals.get("ema_label", "🟡")
    vwap_lbl = signals.get("vwap_label", "❌")
    vol_lbl  = signals.get("vol_label", "Vol—")

    pnl_str = ""
    if pnl_data and "error" not in pnl_data:
        pnl_str = (
            f"  {_fmt_money(pnl_data['total_pnl'])} "
            f"({_fmt_pct(pnl_data['return_pct'])})"
        )

    tags_str = ""
    if holding.get("tags"):
        tags_str = "  " + " ".join("#" + t for t in holding["tags"])

    return (
        f"{ticker}  ${price:.2f}  "
        f"EMA{ema_lbl} VWAP{vwap_lbl} {vol_lbl}"
        f"{pnl_str}{tags_str}"
    )


# ─────────────────────────────────────────────────────────
# HELPERS (duplicated from holdings_commands to keep
# this module self-contained — no circular dependency)
# ─────────────────────────────────────────────────────────

def _fmt_money(v: float) -> str:
    prefix = "+" if v >= 0 else ""
    return f"{prefix}${v:,.0f}"

def _fmt_pct(v: float) -> str:
    prefix = "+" if v >= 0 else ""
    return f"{prefix}{v:.1f}%"
