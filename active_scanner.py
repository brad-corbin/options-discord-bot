# active_scanner.py
# ═══════════════════════════════════════════════════════════════════
# Active Watchlist Scanner — Proactive Signal Generation
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v6.0 changes (2026-04-06):
#   - REGIME-AWARE FILTERING: every signal is checked against TICKER_RULES
#     before being enqueued. Tickers not active in current regime are silently
#     dropped. No manual regime override needed — market_regime.py auto-detects.
#   - FULL-DETAIL ALERTS: signal messages include ALL trade instructions inline.
#     Trader sees spread type, legs, width, DTE, exact exit date, WR, notes,
#     and hard DO-NOT rules without needing to reference any other document.
#   - SLV added to TIER_C watchlist (was missing).
#   - Daily regime refresh: detector.refresh() called at market open each day.
#
# v6.1 changes (2026-04-08 — TRANSITION backtest integration):
#   P2: HTF scoring fix — CONVERGING bulls in TRANSITION get +12 instead
#       of the -10 opposing-daily penalty. CONVERGING is the premium
#       TRANSITION signal (+4.91% 5D avg, PF 9.61) but was systematically
#       under-scored by 22 points vs CONFIRMED.
#   P4: RSI window shift — TRANSITION bull RSI bonus window moved from
#       40–65 to 50–75. RSI < 45 gets -5 penalty (avg -2.82%, PF 0.33).
#       RSI 50–65 is best zone (avg +1.91%, PF 2.02).
#   Regime is now passed to _analyze_ticker() so scoring is context-aware.
#
# ─── REGIME AUTO-DETECTION ─────────────────────────────────────────
#   market_regime.py computes BEAR / TRANSITION / BULL from QQQ and IWM
#   daily closes vs 20-day and 50-day SMAs. Refreshes once per day.
#   Posts a Telegram alert on regime change.
#   No manual intervention required.
#
# Tiered scanning:
#   Tier A (QQQ, IWM):            every 5 min
#   Tier B (mega-cap stocks):      every 10 min
#   Tier C (extended watchlist):   every 15 min
# ═══════════════════════════════════════════════════════════════════

import math
import time
import logging
import threading
from datetime import datetime, date
from typing import Dict, List, Callable, Optional

from market_clock import is_equity_session, current_phase, CT
from market_regime import MarketRegimeDetector, get_market_regime, DEFAULT_REGIME
from ticker_rules import (
    is_signal_valid,
    build_alert_message,
    get_active_tickers,
    TICKER_RULES,
)

log = logging.getLogger(__name__)

# ── Watchlist tiers ──
# Tier A: index ETFs — highest priority, scanned every 5 min
TIER_A = ["QQQ", "IWM"]   # SPY removed — not in backtest ruleset

# Tier B: mega-cap stocks — every 10 min
TIER_B = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "TSLA", "GOOGL",
]

# Tier C: extended watchlist — every 15 min
TIER_C = [
    "GLD", "SLV",       # metals (active in BULL regime)
    "AMD", "NFLX", "COIN", "AVGO", "PLTR",   # extended equity
    "CRM", "ORCL", "ARM", "SMCI", "MSTR", "DIA",
    "XLF", "XLE", "XLV", "SOXX", "TLT",
    "JPM", "GS", "BA", "CAT", "LLY", "UNH",
]

SCAN_INTERVAL_A = 300    # 5 min
SCAN_INTERVAL_B = 600    # 10 min
SCAN_INTERVAL_C = 900    # 15 min

# ── Technical signal thresholds ──
EMA_FAST    = 5
EMA_SLOW    = 12
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
RSI_PERIOD  = 14
WT_CHANNEL  = 10
WT_AVERAGE  = 21

# Signal tier mapping
SIGNAL_TIER_1_SCORE = 75
SIGNAL_TIER_2_SCORE = 55
MIN_SIGNAL_SCORE    = SIGNAL_TIER_2_SCORE   # 55 — scores 50-54 are rejected


# ═══════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS FUNCTIONS (unchanged from v5.x)
# ═══════════════════════════════════════════════════════════

def _compute_ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    ema = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema


def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
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
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return {}
    ema_fast = _compute_ema(closes, MACD_FAST)
    ema_slow = _compute_ema(closes, MACD_SLOW)
    offset = MACD_SLOW - MACD_FAST
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < MACD_SIGNAL:
        return {}
    signal = _compute_ema(macd_line, MACD_SIGNAL)
    hist = macd_line[-1] - signal[-1] if signal else 0
    return {
        "macd_line": macd_line[-1] if macd_line else 0,
        "signal_line": signal[-1] if signal else 0,
        "macd_hist": hist,
        "macd_cross_bull": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] < signal[-2]
                           and macd_line[-1] > signal[-1]) if signal else False,
        "macd_cross_bear": (len(macd_line) >= 2 and len(signal) >= 2
                           and macd_line[-2] > signal[-2]
                           and macd_line[-1] < signal[-1]) if signal else False,
    }


def _compute_wavetrend(hlc3: list) -> Dict:
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
        "wt_cross_bull": (len(wt1) >= 2 and len(wt2_vals) >= 2
                         and wt1[-2] < wt2_vals[-2]
                         and wt1[-1] > wt2_vals[-1]),
        "wt_cross_bear": (len(wt1) >= 2 and len(wt2_vals) >= 2
                         and wt1[-2] > wt2_vals[-2]
                         and wt1[-1] < wt2_vals[-1]),
    }


# ═══════════════════════════════════════════════════════════
# TICKER ANALYSIS (unchanged from v5.x)
# ═══════════════════════════════════════════════════════════

def _analyze_ticker(
    ticker: str,
    intraday_fn: Callable,
    daily_candle_fn: Callable,
    regime: str = "BEAR",
    flow_boost_fn: Callable = None,
) -> Optional[Dict]:
    """
    Run technical analysis on a ticker using intraday + daily data.
    Returns signal dict if a setup is detected, None otherwise.
    """
    def _reject(reason: str) -> None:
        log.debug(f"Scanner reject {ticker}: {reason}")
        return None

    try:
        bars = None
        try:
            bars = intraday_fn(ticker, resolution=5, countback=80)
            if bars and bars.get("c"):
                pass
        except Exception:
            pass

        if not bars or not bars.get("c"):
            return _reject("no_intraday_data")

        closes  = [c for c in bars["c"] if c is not None]
        highs   = [h for h in bars.get("h", []) if h is not None]
        lows    = [l for l in bars.get("l", []) if l is not None]
        volumes = [v for v in bars.get("v", []) if v is not None]

        if len(closes) < 12:
            return _reject(f"insufficient_bars ({len(closes)} < 12)")

        spot = closes[-1]

        bar_count = len(closes)
        if bar_count >= 40:
            data_quality = "full"
        elif bar_count >= 20:
            data_quality = "partial"
        else:
            data_quality = "minimal"

        if volumes and len(volumes) >= 10:
            avg_vol_10 = sum(volumes[-10:]) / 10
            adtv = avg_vol_10 * spot * 5 * 60
            if adtv < 5_000_000 and ticker not in ("SPY", "QQQ", "IWM", "DIA"):
                return _reject(f"low_adtv (est ${adtv/1e6:.1f}M < $5M)")

        vwap = None
        if highs and lows and volumes and len(highs) == len(lows) == len(volumes) == len(closes):
            tp_vol_sum = sum((highs[i] + lows[i] + closes[i]) / 3 * volumes[i]
                            for i in range(len(closes)) if volumes[i] > 0)
            vol_sum = sum(v for v in volumes if v > 0)
            if vol_sum > 0:
                vwap = tp_vol_sum / vol_sum

        ema5  = _compute_ema(closes, EMA_FAST)
        ema12 = _compute_ema(closes, EMA_SLOW)
        if not ema5 or not ema12:
            return _reject("ema_computation_failed")

        ema_bull     = ema5[-1] > ema12[-1]
        ema_dist_pct = ((ema5[-1] - ema12[-1]) / ema12[-1]) * 100 if ema12[-1] > 0 else 0

        macd  = _compute_macd(closes)
        hlc3  = [(highs[i] + lows[i] + closes[i]) / 3
                 for i in range(min(len(highs), len(lows), len(closes)))]
        wt    = _compute_wavetrend(hlc3)
        rsi   = _compute_rsi(closes, RSI_PERIOD)

        avg_vol      = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
        current_vol  = volumes[-1] if volumes else 0
        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        # Current market phase (MORNING / MIDDAY / AFTERNOON)
        try:
            phase = current_phase()
        except Exception:
            phase = "UNKNOWN"

        # Daily trend
        daily_closes  = daily_candle_fn(ticker, days=30)
        daily_bull    = None
        htf_confirmed = False
        htf_converging = False
        htf_status    = "UNKNOWN"
        if daily_closes and len(daily_closes) >= 21:
            daily_ema8  = _compute_ema(daily_closes, 8)
            daily_ema21 = _compute_ema(daily_closes, 21)
            if daily_ema8 and daily_ema21 and len(daily_ema8) >= 2:
                daily_bull    = daily_ema8[-1] > daily_ema21[-1]
                htf_confirmed = (daily_bull == ema_bull)

                if htf_confirmed:
                    htf_status = "CONFIRMED"
                else:
                    daily_gap_now  = abs(daily_ema8[-1] - daily_ema21[-1])
                    daily_gap_prev = abs(daily_ema8[-2] - daily_ema21[-2])
                    if daily_gap_now < daily_gap_prev * 0.98:
                        htf_converging = True
                        htf_status = "CONVERGING"
                    else:
                        htf_status = "OPPOSING"

        # ── Score the setup ──
        score = 0
        bias  = "bull" if ema_bull else "bear"
        score_breakdown = {}

        if abs(ema_dist_pct) > 0.03:
            score += 15; score_breakdown["ema"] = 15
        elif abs(ema_dist_pct) > 0.01:
            score += 8;  score_breakdown["ema"] = 8
        else:
            score_breakdown["ema"] = 0

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

        if vwap:
            if bias == "bull" and spot > vwap:
                score += 10; score_breakdown["vwap"] = 10
            elif bias == "bear" and spot < vwap:
                score += 10; score_breakdown["vwap"] = 10
            elif bias == "bull" and spot < vwap:
                score -= 5;  score_breakdown["vwap"] = -5
            elif bias == "bear" and spot > vwap:
                score -= 5;  score_breakdown["vwap"] = -5
        else:
            score_breakdown["vwap"] = 0

        if htf_confirmed:
            score += 15; score_breakdown["htf"] = 15
        elif htf_converging and regime == "TRANSITION":
            # P2: CONVERGING is the premium TRANSITION signal (+4.91% 5D avg)
            # Do NOT penalize for opposing daily — that's expected in convergence
            score += 12; score_breakdown["htf"] = 12
        elif daily_bull is not None:
            if (bias == "bull" and daily_bull) or (bias == "bear" and not daily_bull):
                score += 10; score_breakdown["htf"] = 10
            else:
                score -= 10; score_breakdown["htf"] = -10
        else:
            score_breakdown["htf"] = 0

        if volume_ratio > 1.5:
            score += 10; score_breakdown["volume"] = 10
        elif volume_ratio > 1.0:
            score += 5;  score_breakdown["volume"] = 5
        else:
            score_breakdown["volume"] = 0

        if rsi:
            if regime == "TRANSITION" and bias == "bull":
                # P4: TRANSITION bull RSI window is 50–75 (not 40–65)
                # RSI < 45 is strongly negative (-2.82%, PF 0.33)
                # RSI 50–65 is best zone (+1.91%, PF 2.02)
                if 50 < rsi < 75:
                    score += 5; score_breakdown["rsi"] = 5
                elif rsi < 45:
                    score -= 5; score_breakdown["rsi"] = -5
                else:
                    score_breakdown["rsi"] = 0
            elif bias == "bull" and 40 < rsi < 65:
                score += 5; score_breakdown["rsi"] = 5
            elif bias == "bear" and 35 < rsi < 60:
                score += 5; score_breakdown["rsi"] = 5
            else:
                score_breakdown["rsi"] = 0
        else:
            score_breakdown["rsi"] = 0

        # ── Institutional flow boost ──
        # Can lift borderline signals (50-54) over threshold (55)
        # Cannot save structurally bad signals (< 45)
        flow_boost = 0
        if flow_boost_fn:
            try:
                raw_boost = flow_boost_fn(ticker, bias, spot)
                # Translate 0-1.5 to 0-10 points
                flow_boost = round(min(raw_boost * 7, 10))
                if flow_boost > 0:
                    score += flow_boost
                    score_breakdown["flow"] = flow_boost
                    log.info(f"Scanner {ticker}: flow boost +{flow_boost} (total {score})")
                else:
                    score_breakdown["flow"] = 0
            except Exception:
                score_breakdown["flow"] = 0
        else:
            score_breakdown["flow"] = 0

        if score < MIN_SIGNAL_SCORE:
            return _reject(f"below_threshold (score={score})")

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
            "phase": phase,
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


# ═══════════════════════════════════════════════════════════
# ACTIVE SCANNER CLASS
# ═══════════════════════════════════════════════════════════

class ActiveScanner:
    """
    Background scanner that continuously monitors the watchlist during
    market hours and generates regime-filtered, fully-detailed signals.

    v6.0: Integrates MarketRegimeDetector. Every signal is checked
    against TICKER_RULES for the current regime before being posted.
    """

    def __init__(
        self,
        enqueue_fn: Callable,
        spot_fn: Callable,
        candle_fn: Callable,
        intraday_fn: Callable,
        regime_detector: Optional[MarketRegimeDetector] = None,
        regime_fn: Callable = None,        # kept for backwards compat
        vol_regime_fn: Callable = None,    # kept for backwards compat
        shadow_log_fn: Callable = None,    # v6.1: shadow log for non-active tickers
        flow_boost_fn: Callable = None,    # v6.1: institutional flow boost
    ):
        self._enqueue      = enqueue_fn
        self._spot         = spot_fn
        self._candles      = candle_fn
        self._intraday     = intraday_fn
        self._vol_regime   = vol_regime_fn
        self._shadow_log_fn = shadow_log_fn  # v6.1
        self._flow_boost_fn = flow_boost_fn  # v6.1

        # v6.0: regime detector (auto-detects BEAR/TRANSITION/BULL)
        self._regime_detector: Optional[MarketRegimeDetector] = regime_detector

        self._thread = None
        self._running = False
        self._last_scan: Dict[str, float] = {}
        self._signal_dedup: Dict[str, float] = {}
        self._signal_dedup_ttl = 900   # 15 min dedup window

        # Track last regime refresh date
        self._last_regime_refresh_date: Optional[date] = None

        # v6.1: Stagger startup scans to prevent all 33 tickers hitting the
        # API simultaneously on deploy. Assign random initial last_scan times
        # so the first cycle spreads out over the full scan intervals.
        self._stagger_startup()

    def _stagger_startup(self):
        """Assign random initial scan offsets so tickers don't all fire at once."""
        import random
        now = time.time()
        all_tiers = [
            (TIER_A, SCAN_INTERVAL_A),
            (TIER_B, SCAN_INTERVAL_B),
            (TIER_C, SCAN_INTERVAL_C),
        ]
        for tickers, interval in all_tiers:
            for ticker in tickers:
                # Set last_scan to a random point in the past within one interval
                # so the first scan is staggered across the full interval window
                self._last_scan[ticker] = now - random.uniform(0, interval)

    # ── Regime helpers ───────────────────────────────────────

    def _get_regime(self) -> str:
        """Returns current market regime string."""
        if self._regime_detector:
            return self._regime_detector.get_regime()
        return get_market_regime()

    def _refresh_regime_if_needed(self):
        """Refresh regime once per day at market open."""
        if self._regime_detector is None:
            return
        today = date.today()
        if self._last_regime_refresh_date == today:
            return
        try:
            new_regime = self._regime_detector.refresh(
                daily_candle_fn=lambda t, days=60: self._candles(t, days=days)
            )
            self._last_regime_refresh_date = today
            log.info(f"Regime refreshed: {new_regime}")
        except Exception as e:
            log.error(f"Regime refresh failed: {e}", exc_info=True)

    # ── Dedup helpers ────────────────────────────────────────

    def _setup_hash(self, ticker: str, signal: dict) -> str:
        bias       = signal.get("bias", "?")
        htf        = signal.get("htf_status", "?")
        vwap_side  = "above" if signal.get("above_vwap") else "below"
        score_bkt  = (signal.get("score", 0) // 10) * 10
        phase      = signal.get("phase", "?")
        return f"{ticker}:{bias}:{htf}:{vwap_side}:{score_bkt}:{phase}"

    def _is_deduped(self, ticker: str, signal: dict) -> bool:
        key  = self._setup_hash(ticker, signal)
        last = self._signal_dedup.get(key, 0)
        return (time.time() - last) < self._signal_dedup_ttl

    def _mark_signaled(self, ticker: str, signal: dict):
        key = self._setup_hash(ticker, signal)
        self._signal_dedup[key] = time.time()

    # ── Core scan ────────────────────────────────────────────

    def _scan_ticker(self, ticker: str):
        """
        Analyze one ticker. If it passes technical analysis AND the
        regime-aware rule filter, build a full-detail alert and enqueue.
        """
        self._last_scan[ticker] = time.time()

        regime = self._get_regime()

        signal = _analyze_ticker(
            ticker=ticker,
            intraday_fn=self._intraday,
            daily_candle_fn=self._candles,
            regime=regime,
            flow_boost_fn=self._flow_boost_fn,
        )
        if not signal:
            return

        score  = signal["score"]
        bias   = signal["bias"]
        tier   = signal["tier"]

        # ── Regime-aware rule filter ──────────────────────────
        # Only tickers in TICKER_RULES are allowed through.
        # Everything else is suppressed — no ungated alerts.
        if ticker not in TICKER_RULES:
            log.debug(f"Scanner {ticker}: not in TICKER_RULES — suppressed")
            # v6.1: shadow log — ticker has no rule in any regime
            if score >= MIN_SIGNAL_SCORE and self._shadow_log_fn:
                try:
                    self._shadow_log_fn(ticker, regime, signal, "no_rule_in_regime")
                except Exception:
                    pass
            return

        if not is_signal_valid(ticker, regime, signal):
            log.debug(
                f"Scanner {ticker}: {bias} score={score} htf={signal['htf_status']} "
                f"phase={signal.get('phase')} — filtered (regime={regime})"
            )
            # v6.1: shadow log — ticker has a rule but this signal didn't pass it
            if score >= MIN_SIGNAL_SCORE and self._shadow_log_fn:
                try:
                    self._shadow_log_fn(ticker, regime, signal, "rule_exists_signal_filtered")
                except Exception:
                    pass
            return

        # ── Dedup ────────────────────────────────────────────
        if self._is_deduped(ticker, signal):
            log.debug(
                f"Scanner {ticker}: {bias} T{tier} score={score} — deduped"
            )
            return

        log.info(
            f"Scanner signal: {ticker} {bias.upper()} T{tier} "
            f"(score={score}, regime={regime}, htf={signal['htf_status']}, "
            f"vol_ratio={signal['volume_ratio']:.1f}x)"
        )

        # ── Build webhook data ────────────────────────────────
        webhook_data = {
            "tier": tier,
            "bias": bias,
            "close": signal["close"],
            "phase": signal.get("phase", ""),
            "time": datetime.now(CT).strftime("%H:%M:%S"),
            "received_at_epoch": time.time(),
            "market_regime": regime,
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
            "data_quality": signal.get("data_quality", "unknown"),
            "bar_count": signal.get("bar_count", 0),
            "score_breakdown": signal.get("score_breakdown", {}),
            "setup_hash": self._setup_hash(ticker, signal),
        }

        # ── Build alert message ───────────────────────────────
        signal_msg = build_alert_message(ticker, regime, signal)

        self._enqueue("tv", ticker, bias, webhook_data, signal_msg)
        self._mark_signaled(ticker, signal)

    def _legacy_message(
        self,
        ticker: str,
        bias: str,
        tier: str,
        signal: dict,
        regime: str,
    ) -> str:
        """Fallback message format for tickers not in TICKER_RULES."""
        tier_emoji  = "🥇" if tier == "1" else "🥈"
        dir_emoji   = "🐻" if bias == "bear" else "🐂"
        regime_emoji = {"BEAR": "🔴", "TRANSITION": "🟡", "BULL": "🟢"}.get(regime, "⚪")
        wt2         = signal.get("wt2", 0)
        wave_zone   = ("🟢 Oversold" if wt2 < -30 else
                       "🔴 Overbought" if wt2 > 60 else "⚪ Neutral")
        vol_str = (f"📊 {signal['volume_ratio']:.1f}x avg"
                   if signal.get("volume_ratio", 0) > 1.2 else "")
        htf_display = {
            "CONFIRMED": "✅ Confirmed", "CONVERGING": "🟡 Converging",
            "OPPOSING": "🔴 Opposing", "UNKNOWN": "⚪ No data",
        }.get(signal.get("htf_status", "UNKNOWN"), "❓")
        dq = signal.get("data_quality", "unknown")
        dq_display = {"full": "", "partial": " ⚠️partial-data",
                      "minimal": " ⚠️minimal-data"}.get(dq, "")
        rsi = signal.get("rsi_mfi")
        rsi_str = f"RSI: {rsi:.0f}" if rsi else ""

        return "\n".join([
            f"{tier_emoji} SCAN — {ticker} {dir_emoji} {bias.upper()}{dq_display}  {regime_emoji} {regime}",
            f"Close: ${signal['close']:.2f} | Phase: {signal.get('phase','')} | Score: {signal['score']}",
            f"HTF: {htf_display} | Daily: {'🟢' if signal['daily_bull'] else '🔴' if signal['daily_bull'] is False else '⚪'}",
            f"Wave: {wave_zone} (wt2={wt2:.1f})",
            f"VWAP: {'Above ✅' if signal['above_vwap'] else 'Below'} | {rsi_str} | {vol_str}",
            "",
        ])

    # ── Scanner lifecycle ────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            log.info("Scanner already running")
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="active-scanner"
        )
        self._thread.start()
        log.info("Active scanner started (v6.0 — regime-aware)")

    def stop(self):
        self._running = False
        log.info("Active scanner stopping")

    def _is_market_hours(self) -> bool:
        return is_equity_session()

    def _should_scan(self, ticker: str, interval: int) -> bool:
        last = self._last_scan.get(ticker, 0)
        return (time.time() - last) >= interval

    def _loop(self):
        """Main scanner loop. Runs during market hours."""
        log.info("Scanner loop started")
        _cycle_count = 0
        _last_summary = time.time()
        SUMMARY_INTERVAL = 300

        while self._running:
            try:
                if not self._is_market_hours():
                    time.sleep(60)
                    continue

                # Refresh regime once per day (at first scan after open)
                self._refresh_regime_if_needed()

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

                if time.time() - _last_summary >= SUMMARY_INTERVAL:
                    regime = self._get_regime()
                    active = get_active_tickers(regime)
                    log.info(
                        f"Scanner summary: regime={regime}, "
                        f"active_tickers={active}, "
                        f"cycle=#{_cycle_count}, "
                        f"dedup_keys={len(self._signal_dedup)}"
                    )
                    _last_summary = time.time()

                time.sleep(30)

            except Exception as e:
                log.error(f"Scanner loop error: {e}", exc_info=True)
                time.sleep(60)

        log.info("Scanner loop stopped")

    # ── Status ───────────────────────────────────────────────

    @property
    def watchlist_size(self) -> int:
        return len(TIER_A) + len(TIER_B) + len(TIER_C)

    @property
    def status(self) -> Dict:
        regime = self._get_regime()
        return {
            "running": self._running,
            "market_regime": regime,
            "active_tickers": get_active_tickers(regime),
            "watchlist_size": self.watchlist_size,
            "tickers_scanned": len(self._last_scan),
            "signals_generated": len(self._signal_dedup),
            "regime_detector": (
                self._regime_detector.get_status()
                if self._regime_detector else None
            ),
            "tier_a": TIER_A,
            "tier_b": TIER_B,
            "tier_c": TIER_C,
        }
