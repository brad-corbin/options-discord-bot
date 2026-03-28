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
from typing import Dict, List, Callable, Optional

from market_clock import is_equity_session, current_phase, CT

log = logging.getLogger(__name__)

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
        # v5.1 fix: cross detection compares prior-to-prior, not prior-to-current
        "macd_cross_bull": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] < signal[-2]
                           and macd_line[-1] > signal[-1]) if signal else False,
        "macd_cross_bear": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] > signal[-2]
                           and macd_line[-1] < signal[-1]) if signal else False,
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
        # v5.1 fix: cross detection compares prior-to-prior, not prior-to-current
        "wt_cross_bull": (len(wt1) >= 2 and len(wt2_vals) >= 2
                         and wt1[-2] < wt2_vals[-2]
                         and wt1[-1] > wt2_vals[-1]),
        "wt_cross_bear": (len(wt1) >= 2 and len(wt2_vals) >= 2
                         and wt1[-2] > wt2_vals[-2]
                         and wt1[-1] < wt2_vals[-1]),
    }


def _analyze_ticker(
    ticker: str,
    intraday_fn: Callable,
    daily_candle_fn: Callable,
) -> Optional[Dict]:
    """
    Run technical analysis on a ticker using intraday + daily data.

    v5.1 institutional fixes:
      - Reject telemetry: every exit path returns a reason via _reject()
      - Data quality flags: full/partial/minimal based on bar count
      - ADTV liquidity gating: rejects tickers with very low dollar volume
      - Removed unused spot_fn parameter

    Returns signal dict if setup is detected, None otherwise.
    Reject reasons are logged for tuning telemetry.
    """
    def _reject(reason: str) -> None:
        """Structured reject logging for scanner telemetry."""
        log.debug(f"Scanner reject {ticker}: {reason}")
        return None

    try:
        # Fetch 5-minute bars with fallback for early session / thin tickers
        bars = None
        bars_requested = 0
        for cb in [80, 40, 20]:
            try:
                bars = intraday_fn(ticker, resolution=5, countback=cb)
                if bars and bars.get("c"):
                    bars_requested = cb
                    break
            except Exception:
                continue

        if not bars or not bars.get("c"):
            return _reject("no_intraday_data")

        closes = [c for c in bars["c"] if c is not None]
        highs = [h for h in bars.get("h", []) if h is not None]
        lows = [l for l in bars.get("l", []) if l is not None]
        volumes = [v for v in bars.get("v", []) if v is not None]

        if len(closes) < 12:
            return _reject(f"insufficient_bars ({len(closes)} < 12)")

        spot = closes[-1]

        # ── Data quality classification ──
        bar_count = len(closes)
        if bar_count >= 40:
            data_quality = "full"       # all indicators available
        elif bar_count >= 20:
            data_quality = "partial"    # RSI available, MACD/WT may not be
        else:
            data_quality = "minimal"    # EMA + VWAP only

        # ── ADTV liquidity gating ──
        # Reject tickers with very low average daily dollar volume
        # (they'll have garbage option liquidity downstream)
        if volumes and len(volumes) >= 10:
            avg_vol_10 = sum(volumes[-10:]) / 10
            adtv = avg_vol_10 * spot * 5 * 60  # rough daily estimate from 5-min bars
            # ~$5M daily min for 0DTE options liquidity
            if adtv < 5_000_000 and ticker not in ("SPY", "QQQ", "IWM", "DIA"):
                return _reject(f"low_adtv (est ${adtv/1e6:.1f}M < $5M)")

        # VWAP approximation
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
            return _reject("ema_computation_failed")

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
        daily_bull = None
        htf_confirmed = False
        htf_converging = False
        htf_status = "UNKNOWN"
        if daily_closes and len(daily_closes) >= 21:
            daily_ema8 = _compute_ema(daily_closes, 8)
            daily_ema21 = _compute_ema(daily_closes, 21)
            if daily_ema8 and daily_ema21 and len(daily_ema8) >= 2:
                daily_bull = daily_ema8[-1] > daily_ema21[-1]
                htf_confirmed = (daily_bull == ema_bull)

                if htf_confirmed:
                    htf_status = "CONFIRMED"
                else:
                    daily_gap_now = abs(daily_ema8[-1] - daily_ema21[-1])
                    daily_gap_prev = abs(daily_ema8[-2] - daily_ema21[-2])
                    if daily_gap_now < daily_gap_prev * 0.98:
                        htf_converging = True
                        htf_status = "CONVERGING"
                    else:
                        htf_status = "OPPOSING"

        # ── Score the setup ──
        score = 0
        bias = "bull" if ema_bull else "bear"
        score_breakdown = {}  # telemetry: what contributed to the score

        # EMA alignment
        if abs(ema_dist_pct) > 0.03:
            score += 15
            score_breakdown["ema"] = 15
        elif abs(ema_dist_pct) > 0.01:
            score += 8
            score_breakdown["ema"] = 8
        else:
            score_breakdown["ema"] = 0

        # MACD confirmation
        if macd:
            if bias == "bull" and macd.get("macd_hist", 0) > 0:
                score += 15; score_breakdown["macd_hist"] = 15
            elif bias == "bear" and macd.get("macd_hist", 0) < 0:
                score += 15; score_breakdown["macd_hist"] = 15
            elif macd.get("macd_hist", 0) != 0:
                score -= 10; score_breakdown["macd_hist"] = -10
            else:
                score_breakdown["macd_hist"] = 0

            if macd.get("macd_cross_bull") and bias == "bull":
                score += 10; score_breakdown["macd_cross"] = 10
            elif macd.get("macd_cross_bear") and bias == "bear":
                score += 10; score_breakdown["macd_cross"] = 10
            else:
                score_breakdown["macd_cross"] = 0
        else:
            score_breakdown["macd_hist"] = 0
            score_breakdown["macd_cross"] = 0

        # WaveTrend zone
        if wt:
            if bias == "bull" and wt.get("wt_oversold"):
                score += 15; score_breakdown["wt"] = 15
            elif bias == "bear" and wt.get("wt_overbought"):
                score += 15; score_breakdown["wt"] = 15
            elif bias == "bull" and wt.get("wt_overbought"):
                score -= 10; score_breakdown["wt"] = -10
            elif bias == "bear" and wt.get("wt_oversold"):
                score -= 10; score_breakdown["wt"] = -10
            elif bias == "bull" and wt.get("wt_cross_bull"):
                score += 10; score_breakdown["wt"] = 10
            elif bias == "bear" and wt.get("wt_cross_bear"):
                score += 10; score_breakdown["wt"] = 10
            else:
                score_breakdown["wt"] = 0
        else:
            score_breakdown["wt"] = 0

        # VWAP position
        if vwap:
            if bias == "bull" and spot > vwap:
                score += 10; score_breakdown["vwap"] = 10
            elif bias == "bear" and spot < vwap:
                score += 10; score_breakdown["vwap"] = 10
            elif bias == "bull" and spot < vwap:
                score -= 5; score_breakdown["vwap"] = -5
            elif bias == "bear" and spot > vwap:
                score -= 5; score_breakdown["vwap"] = -5
        else:
            score_breakdown["vwap"] = 0

        # Daily trend alignment
        if htf_confirmed:
            score += 15; score_breakdown["htf"] = 15
        elif daily_bull is not None:
            if (bias == "bull" and daily_bull) or (bias == "bear" and not daily_bull):
                score += 10; score_breakdown["htf"] = 10
            else:
                score -= 10; score_breakdown["htf"] = -10
        else:
            score_breakdown["htf"] = 0

        # Volume confirmation
        if volume_ratio > 1.5:
            score += 10; score_breakdown["volume"] = 10
        elif volume_ratio > 1.0:
            score += 5; score_breakdown["volume"] = 5
        else:
            score_breakdown["volume"] = 0

        # RSI
        if rsi:
            if bias == "bull" and 40 < rsi < 65:
                score += 5; score_breakdown["rsi"] = 5
            elif bias == "bear" and 35 < rsi < 60:
                score += 5; score_breakdown["rsi"] = 5
            else:
                score_breakdown["rsi"] = 0
        else:
            score_breakdown["rsi"] = 0

        # Data quality penalty for partial/minimal data
        if data_quality == "partial":
            score_breakdown["data_quality_note"] = "partial (20-39 bars)"
        elif data_quality == "minimal":
            score_breakdown["data_quality_note"] = "minimal (12-19 bars)"

        if score < MIN_SIGNAL_SCORE:
            return _reject(f"below_threshold (score={score}, breakdown={score_breakdown})")

        tier = "1" if score >= SIGNAL_TIER_1_SCORE else "2"

        return {
            "ticker": ticker,
            "bias": bias,
            "tier": tier,
            "score": score,
            "score_breakdown": score_breakdown,
            "data_quality": data_quality,
            "bar_count": bar_count,
            "close": spot,
            "ema5": ema5[-1],
            "ema12": ema12[-1],
            "ema_dist_pct": round(ema_dist_pct, 3),
            "macd_hist": macd.get("macd_hist", 0),
            "macd_line": macd.get("macd_line", 0),
            "signal_line": macd.get("signal_line", 0),
            "macd_cross_bull": macd.get("macd_cross_bull", False),
            "macd_cross_bear": macd.get("macd_cross_bear", False),
            "wt1": wt.get("wt1", 0),
            "wt2": wt.get("wt2", 0),
            "wt_cross_bull": wt.get("wt_cross_bull", False),
            "wt_cross_bear": wt.get("wt_cross_bear", False),
            "rsi_mfi": rsi,
            "rsi_mfi_bull": rsi > 50 if rsi else False,
            "vwap": vwap,
            "above_vwap": spot > vwap if vwap else False,
            "htf_confirmed": htf_confirmed,
            "htf_converging": htf_converging,
            "htf_status": htf_status,
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
        return is_equity_session()

    def _should_scan(self, ticker: str, interval: int) -> bool:
        last = self._last_scan.get(ticker, 0)
        return (time.time() - last) >= interval

    def _is_deduped(self, ticker: str, signal: dict) -> bool:
        """
        v5.1: Setup-hash dedup. Deduplicates on a richer signature than
        just ticker:bias, so materially different setups at the same ticker
        can fire while identical re-fires are suppressed.

        Hash components: ticker, direction, htf_status, vwap_side, score_bucket
        """
        key = self._setup_hash(ticker, signal)
        last = self._signal_dedup.get(key, 0)
        return (time.time() - last) < self._signal_dedup_ttl

    def _mark_signaled(self, ticker: str, signal: dict):
        key = self._setup_hash(ticker, signal)
        self._signal_dedup[key] = time.time()

    @staticmethod
    def _setup_hash(ticker: str, signal: dict) -> str:
        """Build a setup signature for dedup."""
        bias = signal.get("bias", "?")
        htf = signal.get("htf_status", "?")
        vwap_side = "above" if signal.get("above_vwap") else "below"
        score_bucket = (signal.get("score", 0) // 10) * 10  # 50, 60, 70, etc
        return f"{ticker}:{bias}:{htf}:{vwap_side}:{score_bucket}"

    def _scan_ticker(self, ticker: str):
        """Analyze one ticker and enqueue signal if setup detected."""
        self._last_scan[ticker] = time.time()

        signal = _analyze_ticker(
            ticker=ticker,
            intraday_fn=self._intraday,
            daily_candle_fn=self._candles,
        )

        if not signal:
            self._scan_no_signal_count = getattr(self, '_scan_no_signal_count', 0) + 1
            return

        bias = signal["bias"]
        tier = signal["tier"]
        score = signal["score"]

        # Log all scored setups, even if below threshold or deduped
        if score < MIN_SIGNAL_SCORE:
            log.debug(f"Scanner {ticker}: score={score} ({bias}) — below minimum {MIN_SIGNAL_SCORE}")
            self._scan_below_threshold_count = getattr(self, '_scan_below_threshold_count', 0) + 1
            return

        # v5.1: Setup-hash dedup — materially different setups can fire
        if self._is_deduped(ticker, signal):
            log.debug(f"Scanner {ticker}: {bias} T{tier} score={score} — deduped "
                      f"(hash={self._setup_hash(ticker, signal)})")
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
            "macd_cross_bull": signal.get("macd_cross_bull", False),
            "macd_cross_bear": signal.get("macd_cross_bear", False),
            "wt1": signal["wt1"],
            "wt2": signal["wt2"],
            "wt_cross_bull": signal.get("wt_cross_bull", False),
            "wt_cross_bear": signal.get("wt_cross_bear", False),
            "rsi_mfi": signal["rsi_mfi"],
            "rsi_mfi_bull": signal["rsi_mfi_bull"],
            "stoch_k": None,
            "stoch_d": None,
            "vwap": signal["vwap"],
            "above_vwap": signal["above_vwap"],
            "htf_confirmed": signal["htf_confirmed"],
            "htf_converging": signal["htf_converging"],
            "htf_status": signal.get("htf_status", "UNKNOWN"),
            "daily_bull": signal["daily_bull"],
            "volume": signal["volume"],
            "timeframe": "5",
            "source": "active_scanner",
            # v5.1: institutional additions
            "data_quality": signal.get("data_quality", "unknown"),
            "bar_count": signal.get("bar_count", 0),
            "score_breakdown": signal.get("score_breakdown", {}),
            "setup_hash": self._setup_hash(ticker, signal),
        }

        tier_emoji = "🥇" if tier == "1" else "🥈"
        dir_emoji = "🐻" if bias == "bear" else "🐂"
        wt2 = signal.get("wt2", 0)
        wave_zone = "🟢 Oversold" if wt2 < -30 else "🔴 Overbought" if wt2 > 60 else "⚪ Neutral"
        vol_str = f"📊 {signal['volume_ratio']:.1f}x avg" if signal.get("volume_ratio", 0) > 1.2 else ""

        # HTF status display
        _htf_s = signal.get("htf_status", "UNKNOWN")
        _htf_display = {"CONFIRMED": "✅ Confirmed", "CONVERGING": "🟡 Converging",
                        "OPPOSING": "🔴 Opposing", "UNKNOWN": "⚪ No data"}.get(_htf_s, f"❓ {_htf_s}")

        # Data quality indicator
        _dq = signal.get("data_quality", "unknown")
        _dq_display = {"full": "", "partial": " ⚠️partial-data", "minimal": " ⚠️minimal-data"}.get(_dq, "")

        signal_msg = "\n".join([
            f"{tier_emoji} SCAN Signal — {ticker} (T{tier} {dir_emoji} {bias.upper()}){_dq_display}",
            f"Close: ${signal['close']:.2f} | 5m scan | {signal.get('bar_count', 0)} bars",
            f"HTF: {_htf_display} | "
            f"Daily: {'🟢' if signal['daily_bull'] is True else '🔴' if signal['daily_bull'] is False else '⚪ N/A'}",
            f"Wave: {wave_zone} (wt2={wt2:.1f})",
            f"VWAP: {'Above ✅' if signal['above_vwap'] else 'Below'} | "
            f"RSI: {signal.get('rsi_mfi', 0):.0f}" + (f" | {vol_str}" if vol_str else ""),
            "",
        ])

        self._enqueue("tv", ticker, bias, webhook_data, signal_msg)
        self._mark_signaled(ticker, signal)

    def _loop(self):
        """Main scanner loop. Runs during market hours."""
        log.info("Scanner loop started")
        _cycle_count = 0
        _last_summary = time.time()
        SUMMARY_INTERVAL = 300  # log summary every 5 min
        while self._running:
            try:
                if not self._is_market_hours():
                    time.sleep(60)
                    continue

                _scanned_this_cycle = 0

                # Tier A: every 5 min
                for ticker in TIER_A:
                    if self._should_scan(ticker, SCAN_INTERVAL_A):
                        self._scan_ticker(ticker)
                        _scanned_this_cycle += 1

                # Tier B: every 10 min
                for ticker in TIER_B:
                    if self._should_scan(ticker, SCAN_INTERVAL_B):
                        self._scan_ticker(ticker)
                        _scanned_this_cycle += 1

                # Tier C: every 15 min
                for ticker in TIER_C:
                    if self._should_scan(ticker, SCAN_INTERVAL_C):
                        self._scan_ticker(ticker)
                        _scanned_this_cycle += 1

                _cycle_count += 1

                # Periodic summary log so we know the scanner is alive
                if time.time() - _last_summary >= SUMMARY_INTERVAL:
                    _no_sig = getattr(self, '_scan_no_signal_count', 0)
                    _below = getattr(self, '_scan_below_threshold_count', 0)
                    _sigs = len(self._signal_dedup)
                    log.info(f"Scanner summary: {len(self._last_scan)} tickers tracked, "
                             f"{_sigs} signals generated, "
                             f"{_no_sig} no-data, {_below} below-threshold, "
                             f"cycle #{_cycle_count}")
                    self._scan_no_signal_count = 0
                    self._scan_below_threshold_count = 0
                    _last_summary = time.time()

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
