# active_scanner.py
# ═══════════════════════════════════════════════════════════════════
# Active Watchlist Scanner — Proactive Signal Generation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Replaces the disabled /scan endpoint with a continuous scanning loop
# that runs during market hours. Computes the same technical signals
# as the TradingView Pine script (EMA crossovers, WaveTrend, MACD,
# RSI+MFI, VWAP) using intraday bar data from MarketData.app.
#
# Tiered scanning:
#   Tier A (SPY, QQQ, IWM):     every 5 min (high priority)
#   Tier B (mega-cap stocks):    every 10 min
#   Tier C (extended watchlist): every 15 min
#
# Pre-chain gate is enforced: only tickers passing all cheap checks
# get their option chains pulled. This keeps API usage manageable.
#
# Usage (in app.py):
#   from active_scanner import ActiveScanner
#   scanner = ActiveScanner(
#       enqueue_fn=_enqueue_signal,
#       spot_fn=get_spot,
#       candle_fn=get_daily_candles,
#       intraday_fn=get_intraday_bars,
#       regime_fn=get_current_regime,
#       vol_regime_fn=get_canonical_vol_regime,
#   )
#   scanner.start()  # launches background thread
# ═══════════════════════════════════════════════════════════════════

import math
import time
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Callable, Optional

log = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")

# ── Watchlist tiers ──
TIER_A = ["SPY", "QQQ", "IWM"]   # 5 min
TIER_B = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL", "AMD",
    "NFLX", "COIN", "AVGO", "PLTR",
]  # 10 min
TIER_C = [
    "CRM", "ORCL", "ARM", "SMCI", "MSTR", "DIA", "GLD",
    "XLF", "XLE", "XLV", "SOXX", "TLT",
    "JPM", "GS", "BA", "CAT", "LLY", "UNH",
]  # 15 min

SCAN_INTERVAL_A = 300    # 5 min
SCAN_INTERVAL_B = 600    # 10 min
SCAN_INTERVAL_C = 900    # 15 min

# ── Technical signal thresholds ──
EMA_FAST = 5
EMA_SLOW = 12
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14
WT_CHANNEL = 10
WT_AVERAGE = 21

# Signal tier mapping
SIGNAL_TIER_1_SCORE = 75    # scanner-generated T1
SIGNAL_TIER_2_SCORE = 55    # scanner-generated T2
MIN_SIGNAL_SCORE = 50       # below this, don't generate signal


def _compute_ema(values: list, period: int) -> list:
    """Compute EMA series."""
    if len(values) < period:
        return []
    ema = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema


def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    """Compute latest RSI value."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def _compute_macd(closes: list) -> Dict:
    """Compute MACD line, signal line, histogram."""
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return {}
    ema_fast = _compute_ema(closes, MACD_FAST)
    ema_slow = _compute_ema(closes, MACD_SLOW)
    offset = MACD_SLOW - MACD_FAST
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < MACD_SIGNAL:
        return {}
    signal = _compute_ema(macd_line, MACD_SIGNAL)
    offset2 = len(macd_line) - len(signal)
    hist = macd_line[-1] - signal[-1] if signal else 0
    return {
        "macd_line": macd_line[-1] if macd_line else 0,
        "signal_line": signal[-1] if signal else 0,
        "macd_hist": hist,
        "macd_cross_bull": len(macd_line) >= 2 and macd_line[-2] < signal[-1] and macd_line[-1] > signal[-1] if signal else False,
        "macd_cross_bear": len(macd_line) >= 2 and macd_line[-2] > signal[-1] and macd_line[-1] < signal[-1] if signal else False,
    }


def _compute_wavetrend(hlc3: list) -> Dict:
    """Compute WaveTrend oscillator (wt1, wt2)."""
    if len(hlc3) < WT_AVERAGE + WT_CHANNEL + 4:
        return {}
    esa = _compute_ema(hlc3, WT_CHANNEL)
    if not esa:
        return {}
    offset = len(hlc3) - len(esa)
    d_series = [abs(hlc3[i + offset] - esa[i]) for i in range(len(esa))]
    de = _compute_ema(d_series, WT_CHANNEL)
    if not de:
        return {}
    offset2 = len(d_series) - len(de)
    ci = []
    for i in range(len(de)):
        d_val = de[i]
        e_val = esa[i + offset2]
        h_val = hlc3[i + offset + offset2]
        ci.append((h_val - e_val) / (0.015 * d_val) if d_val != 0 else 0)
    wt1 = _compute_ema(ci, WT_AVERAGE)
    if not wt1 or len(wt1) < 4:
        return {}
    wt2_vals = _compute_ema(wt1, 4)
    if not wt2_vals:
        return {}
    return {
        "wt1": wt1[-1],
        "wt2": wt2_vals[-1],
        "wt_oversold": wt2_vals[-1] < -30,
        "wt_overbought": wt2_vals[-1] > 60,
        "wt_cross_bull": len(wt1) >= 2 and wt1[-2] < wt2_vals[-1] and wt1[-1] > wt2_vals[-1],
        "wt_cross_bear": len(wt1) >= 2 and wt1[-2] > wt2_vals[-1] and wt1[-1] < wt2_vals[-1],
    }


def _analyze_ticker(
    ticker: str,
    intraday_fn: Callable,
    daily_candle_fn: Callable,
    spot_fn: Callable,
) -> Optional[Dict]:
    """
    Run technical analysis on a ticker using intraday + daily data.
    Returns a signal dict if setup is detected, None otherwise.
    """
    try:
        # Fetch 5-minute bars (80 bars = ~6.5 hours)
        bars = intraday_fn(ticker, resolution=5, countback=80)
        if not bars or not bars.get("c"):
            return None

        closes = [c for c in bars["c"] if c is not None]
        highs = [h for h in bars.get("h", []) if h is not None]
        lows = [l for l in bars.get("l", []) if l is not None]
        volumes = [v for v in bars.get("v", []) if v is not None]

        if len(closes) < 40:
            return None

        spot = closes[-1]

        # VWAP approximation (volume-weighted average of typical prices)
        vwap = None
        if highs and lows and volumes and len(highs) == len(lows) == len(volumes) == len(closes):
            tp_vol_sum = sum((highs[i] + lows[i] + closes[i]) / 3 * volumes[i]
                            for i in range(len(closes)) if volumes[i] > 0)
            vol_sum = sum(v for v in volumes if v > 0)
            if vol_sum > 0:
                vwap = tp_vol_sum / vol_sum

        # EMA crossovers
        ema5 = _compute_ema(closes, EMA_FAST)
        ema12 = _compute_ema(closes, EMA_SLOW)
        if not ema5 or not ema12:
            return None

        ema_bull = ema5[-1] > ema12[-1]
        ema_dist_pct = ((ema5[-1] - ema12[-1]) / ema12[-1]) * 100 if ema12[-1] > 0 else 0

        # MACD
        macd = _compute_macd(closes)

        # WaveTrend
        hlc3 = [(highs[i] + lows[i] + closes[i]) / 3
                for i in range(min(len(highs), len(lows), len(closes)))]
        wt = _compute_wavetrend(hlc3)

        # RSI
        rsi = _compute_rsi(closes, RSI_PERIOD)

        # Volume analysis
        avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
        current_vol = volumes[-1] if volumes else 0
        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        # Daily trend (for HTF confirmation)
        daily_closes = daily_candle_fn(ticker, days=30)
        daily_bull = False
        htf_confirmed = False
        if daily_closes and len(daily_closes) >= 21:
            daily_ema8 = _compute_ema(daily_closes, 8)
            daily_ema21 = _compute_ema(daily_closes, 21)
            if daily_ema8 and daily_ema21:
                daily_bull = daily_ema8[-1] > daily_ema21[-1]
                # HTF confirmed if daily trend aligns with intraday
                htf_confirmed = (daily_bull == ema_bull)

        # ── Score the setup ──
        score = 0
        bias = "bull" if ema_bull else "bear"

        # EMA alignment: +15
        score += 15 if ema_bull or not ema_bull else 0  # always gets this

        # MACD confirmation: +20
        if macd:
            if bias == "bull" and macd.get("macd_hist", 0) > 0:
                score += 15
            elif bias == "bear" and macd.get("macd_hist", 0) < 0:
                score += 15
            if macd.get("macd_cross_bull") and bias == "bull":
                score += 10
            elif macd.get("macd_cross_bear") and bias == "bear":
                score += 10

        # WaveTrend zone: +15
        if wt:
            if bias == "bull" and wt.get("wt_oversold"):
                score += 15
            elif bias == "bear" and wt.get("wt_overbought"):
                score += 15
            elif bias == "bull" and wt.get("wt_cross_bull"):
                score += 10
            elif bias == "bear" and wt.get("wt_cross_bear"):
                score += 10

        # VWAP position: +10
        if vwap:
            if bias == "bull" and spot > vwap:
                score += 10
            elif bias == "bear" and spot < vwap:
                score += 10

        # Daily trend alignment: +15
        if htf_confirmed:
            score += 15
        elif (bias == "bull" and daily_bull) or (bias == "bear" and not daily_bull):
            score += 10

        # Volume confirmation: +10
        if volume_ratio > 1.5:
            score += 10
        elif volume_ratio > 1.0:
            score += 5

        # RSI: +5
        if rsi:
            if bias == "bull" and 40 < rsi < 65:
                score += 5  # bullish but not overbought
            elif bias == "bear" and 35 < rsi < 60:
                score += 5

        if score < MIN_SIGNAL_SCORE:
            return None

        tier = "1" if score >= SIGNAL_TIER_1_SCORE else "2"

        return {
            "ticker": ticker,
            "bias": bias,
            "tier": tier,
            "score": score,
            "close": spot,
            "ema5": ema5[-1],
            "ema12": ema12[-1],
            "ema_dist_pct": round(ema_dist_pct, 3),
            "macd_hist": macd.get("macd_hist", 0),
            "macd_line": macd.get("macd_line", 0),
            "signal_line": macd.get("signal_line", 0),
            "wt1": wt.get("wt1", 0),
            "wt2": wt.get("wt2", 0),
            "rsi_mfi": rsi,
            "rsi_mfi_bull": rsi > 50 if rsi else False,
            "vwap": vwap,
            "above_vwap": spot > vwap if vwap else False,
            "htf_confirmed": htf_confirmed,
            "htf_converging": not htf_confirmed and daily_bull is not None,
            "daily_bull": daily_bull,
            "volume": current_vol,
            "volume_ratio": round(volume_ratio, 2),
            "timeframe": "5",
            "source": "active_scanner",
        }

    except Exception as e:
        log.debug(f"Scanner analysis failed for {ticker}: {e}")
        return None


class ActiveScanner:
    """
    Background scanner that continuously monitors the watchlist
    during market hours and generates signals.
    """

    def __init__(
        self,
        enqueue_fn: Callable,
        spot_fn: Callable,
        candle_fn: Callable,
        intraday_fn: Callable,
        regime_fn: Callable = None,
        vol_regime_fn: Callable = None,
    ):
        self._enqueue = enqueue_fn
        self._spot = spot_fn
        self._candles = candle_fn
        self._intraday = intraday_fn
        self._regime = regime_fn
        self._vol_regime = vol_regime_fn
        self._thread = None
        self._running = False
        self._last_scan: Dict[str, float] = {}  # ticker → last scan timestamp
        self._signal_dedup: Dict[str, float] = {}  # ticker:bias → last signal timestamp
        self._signal_dedup_ttl = 900  # don't re-signal same ticker+bias within 15 min

    def start(self):
        if self._thread and self._thread.is_alive():
            log.info("Scanner already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="active-scanner")
        self._thread.start()
        log.info("Active scanner started")

    def stop(self):
        self._running = False
        log.info("Active scanner stopping")

    def _is_market_hours(self) -> bool:
        now = datetime.now(CT)
        # Mon-Fri, 8:30 AM - 3:00 PM CT
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=8, minute=30, second=0)
        market_close = now.replace(hour=15, minute=0, second=0)
        return market_open <= now <= market_close

    def _should_scan(self, ticker: str, interval: int) -> bool:
        last = self._last_scan.get(ticker, 0)
        return (time.time() - last) >= interval

    def _is_deduped(self, ticker: str, bias: str) -> bool:
        key = f"{ticker}:{bias}"
        last = self._signal_dedup.get(key, 0)
        return (time.time() - last) < self._signal_dedup_ttl

    def _mark_signaled(self, ticker: str, bias: str):
        self._signal_dedup[f"{ticker}:{bias}"] = time.time()

    def _scan_ticker(self, ticker: str):
        """Analyze one ticker and enqueue signal if setup detected."""
        self._last_scan[ticker] = time.time()

        signal = _analyze_ticker(
            ticker=ticker,
            intraday_fn=self._intraday,
            daily_candle_fn=self._candles,
            spot_fn=self._spot,
        )

        if not signal:
            return

        bias = signal["bias"]
        tier = signal["tier"]

        # Dedup: don't re-signal same direction within 15 min
        if self._is_deduped(ticker, bias):
            return

        log.info(f"Scanner signal: {ticker} {bias.upper()} T{tier} "
                 f"(score={signal['score']}, vol_ratio={signal['volume_ratio']:.1f}x)")

        # Build webhook_data matching TV format
        webhook_data = {
            "tier": tier,
            "bias": bias,
            "close": signal["close"],
            "time": datetime.now(CT).strftime("%H:%M:%S"),
            "received_at_epoch": time.time(),
            "ema5": signal["ema5"],
            "ema12": signal["ema12"],
            "ema_dist_pct": signal["ema_dist_pct"],
            "macd_hist": signal["macd_hist"],
            "macd_line": signal["macd_line"],
            "signal_line": signal["signal_line"],
            "wt1": signal["wt1"],
            "wt2": signal["wt2"],
            "rsi_mfi": signal["rsi_mfi"],
            "rsi_mfi_bull": signal["rsi_mfi_bull"],
            "stoch_k": None,
            "stoch_d": None,
            "vwap": signal["vwap"],
            "above_vwap": signal["above_vwap"],
            "htf_confirmed": signal["htf_confirmed"],
            "htf_converging": signal["htf_converging"],
            "daily_bull": signal["daily_bull"],
            "volume": signal["volume"],
            "timeframe": "5",
            "source": "active_scanner",
        }

        tier_emoji = "🥇" if tier == "1" else "🥈"
        dir_emoji = "🐻" if bias == "bear" else "🐂"
        wt2 = signal.get("wt2", 0)
        wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
        vol_str = f"📊 {signal['volume_ratio']:.1f}x avg" if signal.get("volume_ratio", 0) > 1.2 else ""

        signal_msg = "\n".join([
            f"{tier_emoji} SCAN Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()})",
            f"Close: ${signal['close']:.2f} | 5m scan",
            f"1H Trend: {'✅ Confirmed' if signal['htf_confirmed'] else '🟡 Converging'} | "
            f"Daily: {'🟢' if signal['daily_bull'] else '🔴'}",
            f"Wave: {wave_zone} (wt2={wt2:.1f})",
            f"VWAP: {'Above ✅' if signal['above_vwap'] else 'Below'} | "
            f"RSI: {signal.get('rsi_mfi', 0):.0f}" + (f" | {vol_str}" if vol_str else ""),
            "",
        ])

        self._enqueue("tv", ticker, bias, webhook_data, signal_msg)
        self._mark_signaled(ticker, bias)

    def _loop(self):
        """Main scanner loop. Runs during market hours."""
        log.info("Scanner loop started")
        while self._running:
            try:
                if not self._is_market_hours():
                    time.sleep(60)
                    continue

                # Tier A: every 5 min
                for ticker in TIER_A:
                    if self._should_scan(ticker, SCAN_INTERVAL_A):
                        self._scan_ticker(ticker)

                # Tier B: every 10 min
                for ticker in TIER_B:
                    if self._should_scan(ticker, SCAN_INTERVAL_B):
                        self._scan_ticker(ticker)

                # Tier C: every 15 min
                for ticker in TIER_C:
                    if self._should_scan(ticker, SCAN_INTERVAL_C):
                        self._scan_ticker(ticker)

                time.sleep(30)  # check loop every 30s

            except Exception as e:
                log.error(f"Scanner loop error: {e}", exc_info=True)
                time.sleep(60)

        log.info("Scanner loop stopped")

    @property
    def watchlist_size(self) -> int:
        return len(TIER_A) + len(TIER_B) + len(TIER_C)

    @property
    def status(self) -> Dict:
        return {
            "running": self._running,
            "watchlist_size": self.watchlist_size,
            "tickers_scanned": len(self._last_scan),
            "signals_generated": len(self._signal_dedup),
            "tier_a": TIER_A,
            "tier_b": TIER_B,
            "tier_c": TIER_C,
        }
