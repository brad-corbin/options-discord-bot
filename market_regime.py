# market_regime.py
# ═══════════════════════════════════════════════════════════════════
# Market Direction Regime Detector
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Detects BEAR / TRANSITION / BULL using QQQ and IWM daily price
# relative to their moving averages. Refreshes once per day at market
# open (or on first call). No manual intervention required.
#
# Regime definitions (derived from active scanner backtest 2025-2026):
#
#   BEAR        QQQ below its 20-day SMA for 3+ consecutive closes
#               → Bear CONFIRMED signals on MSFT/IWM/QQQ/META/TSLA (88–97% WR)
#               → Bull signals on GOOGL/GLD/SLV are losing trades — OFF
#
#   TRANSITION  QQQ has crossed above its 20-day SMA and held for 5+ closes
#               → Re-activate GOOGL (CONFIRMED+bull), NVDA/AMZN (OPPOSING)
#               → QQQ flips from bear to bull signal
#               → GLD/SLV still OFF until BULL confirmed
#
#   BULL        QQQ AND IWM both above their 50-day SMA for 10+ closes
#               → Activate GLD (CONFIRMED+bull, RSI<65) and SLV (CONVERGING+bull)
#               → Full ticker roster live
#
# Automatic regime change alerts are posted via the notify_fn callback.
#
# Usage (in app.py):
#   from market_regime import MarketRegimeDetector
#   regime_detector = MarketRegimeDetector(notify_fn=post_telegram_message)
#   regime_detector.refresh(daily_candle_fn=get_daily_candles)
#   regime = regime_detector.get_regime()   # "BEAR" | "TRANSITION" | "BULL"
# ═══════════════════════════════════════════════════════════════════

import logging
import threading
from datetime import datetime, date
from typing import Optional, Callable, List

log = logging.getLogger(__name__)

# ── Trigger thresholds (derived from backtest analysis) ──
BEAR_DAYS_BELOW_MA20       = 3   # QQQ must be below 20d SMA for this many consecutive closes
TRANSITION_DAYS_ABOVE_MA20 = 5   # QQQ must hold above 20d SMA for this many consecutive closes
BULL_DAYS_ABOVE_MA50       = 10  # BOTH QQQ and IWM above 50d SMA for this many consecutive closes
MA20_PERIOD                = 20
MA50_PERIOD                = 50

# ── Regime labels ──
REGIME_BEAR       = "BEAR"
REGIME_TRANSITION = "TRANSITION"
REGIME_BULL       = "BULL"
REGIME_UNKNOWN    = "UNKNOWN"

# ── Default on startup (current conditions as of April 2026) ──
DEFAULT_REGIME = REGIME_BEAR


def _sma(closes: List[float], period: int) -> Optional[float]:
    """Simple moving average of the last `period` closes."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _consecutive_above(closes: List[float], ma_period: int) -> int:
    """
    Returns the number of consecutive trailing closes that are
    above their own SMA computed over the full series ending at that bar.
    """
    if len(closes) < ma_period + 1:
        return 0
    count = 0
    for i in range(len(closes) - 1, ma_period - 2, -1):
        window = closes[: i + 1]
        sma = sum(window[-ma_period:]) / ma_period
        if closes[i] > sma:
            count += 1
        else:
            break
    return count


def _consecutive_below(closes: List[float], ma_period: int) -> int:
    """Returns consecutive trailing closes below their SMA."""
    if len(closes) < ma_period + 1:
        return 0
    count = 0
    for i in range(len(closes) - 1, ma_period - 2, -1):
        window = closes[: i + 1]
        sma = sum(window[-ma_period:]) / ma_period
        if closes[i] < sma:
            count += 1
        else:
            break
    return count


class MarketRegimeDetector:
    """
    Determines whether the market is in BEAR, TRANSITION, or BULL regime
    using QQQ and IWM daily closes relative to their moving averages.

    Refreshes once per calendar day. Thread-safe.
    """

    def __init__(self, notify_fn: Optional[Callable] = None):
        self._lock = threading.Lock()
        self._regime: str = DEFAULT_REGIME
        self._last_refresh_date: Optional[date] = None
        self._notify_fn = notify_fn  # callback(msg: str) for regime change alerts

        # Detail state for status display
        self._qqq_price: float = 0.0
        self._qqq_ma20:  float = 0.0
        self._qqq_ma50:  float = 0.0
        self._iwm_price: float = 0.0
        self._iwm_ma50:  float = 0.0
        self._qqq_above_ma20_days: int = 0
        self._qqq_below_ma20_days: int = 0
        self._both_above_ma50_days: int = 0
        self._regime_since: Optional[date] = None

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def get_regime(self) -> str:
        """Returns current regime: BEAR | TRANSITION | BULL"""
        with self._lock:
            return self._regime

    def needs_refresh(self) -> bool:
        """True if we haven't refreshed today."""
        with self._lock:
            return self._last_refresh_date != date.today()

    def refresh(self, daily_candle_fn: Callable) -> str:
        """
        Fetch fresh daily data, recompute regime.
        Call once per day at market open (or on first signal request).

        daily_candle_fn(ticker, days=60) must return a list of daily
        closing prices, oldest first. Returns the new regime string.
        """
        try:
            qqq_closes = daily_candle_fn("QQQ", days=70)
            iwm_closes = daily_candle_fn("IWM", days=70)

            if not qqq_closes or len(qqq_closes) < MA50_PERIOD + 5:
                log.warning("MarketRegime: insufficient QQQ data — keeping current regime")
                return self._regime

            if not iwm_closes or len(iwm_closes) < MA50_PERIOD + 5:
                log.warning("MarketRegime: insufficient IWM data — keeping current regime")
                return self._regime

            new_regime = self._compute_regime(qqq_closes, iwm_closes)

            with self._lock:
                old_regime = self._regime
                self._regime = new_regime
                self._last_refresh_date = date.today()
                if self._regime_since is None:   # first successful load
                    self._regime_since = date.today()

            if new_regime != old_regime:
                self._on_regime_change(old_regime, new_regime)

            log.info(
                f"MarketRegime: {new_regime} | "
                f"QQQ ${self._qqq_price:.2f} / MA20 ${self._qqq_ma20:.2f} / MA50 ${self._qqq_ma50:.2f} | "
                f"IWM MA50 ${self._iwm_ma50:.2f} | "
                f"Below-MA20 streak: {self._qqq_below_ma20_days}d | "
                f"Above-MA20 streak: {self._qqq_above_ma20_days}d"
            )
            return new_regime

        except Exception as e:
            log.error(f"MarketRegime refresh failed: {e}", exc_info=True)
            return self._regime

    def get_status(self) -> dict:
        """Full status dict for /regime command or logging."""
        with self._lock:
            return {
                "regime": self._regime,
                "last_refresh": str(self._last_refresh_date),
                "regime_since": str(self._regime_since),
                "qqq_price": self._qqq_price,
                "qqq_ma20": self._qqq_ma20,
                "qqq_ma50": self._qqq_ma50,
                "qqq_vs_ma20_pct": (
                    (self._qqq_price - self._qqq_ma20) / self._qqq_ma20 * 100
                    if self._qqq_ma20 > 0 else 0
                ),
                "iwm_price": self._iwm_price,
                "iwm_ma50": self._iwm_ma50,
                "qqq_below_ma20_days": self._qqq_below_ma20_days,
                "qqq_above_ma20_days": self._qqq_above_ma20_days,
                "both_above_ma50_days": self._both_above_ma50_days,
                "triggers": {
                    "bear_trigger": f"QQQ below MA20 ≥{BEAR_DAYS_BELOW_MA20}d",
                    "transition_trigger": f"QQQ above MA20 ≥{TRANSITION_DAYS_ABOVE_MA20}d",
                    "bull_trigger": f"QQQ+IWM above MA50 ≥{BULL_DAYS_ABOVE_MA50}d",
                },
            }

    def format_status_message(self) -> str:
        """Human-readable regime status for Telegram/Discord."""
        s = self.get_status()
        regime_emoji = {"BEAR": "🔴", "TRANSITION": "🟡", "BULL": "🟢"}.get(s["regime"], "⚪")

        qqq_vs = s["qqq_vs_ma20_pct"]
        qqq_vs_str = f"{qqq_vs:+.1f}% vs MA20"

        lines = [
            f"{regime_emoji} MARKET REGIME: {s['regime']}",
            f"Since: {s['regime_since']} | Updated: {s['last_refresh']}",
            f"",
            f"QQQ: ${s['qqq_price']:.2f} ({qqq_vs_str})",
            f"  MA20: ${s['qqq_ma20']:.2f} | MA50: ${s['qqq_ma50']:.2f}",
            f"  Below MA20: {s['qqq_below_ma20_days']}d streak | Above MA20: {s['qqq_above_ma20_days']}d streak",
            f"IWM: ${s['iwm_price']:.2f} | MA50: ${s['iwm_ma50']:.2f}",
            f"",
            f"Triggers:",
            f"  → BEAR: QQQ below MA20 for {BEAR_DAYS_BELOW_MA20}+ days (now: {s['qqq_below_ma20_days']})",
            f"  → TRANSITION: QQQ above MA20 for {TRANSITION_DAYS_ABOVE_MA20}+ days (now: {s['qqq_above_ma20_days']})",
            f"  → BULL: Both above MA50 for {BULL_DAYS_ABOVE_MA50}+ days (now: {s['both_above_ma50_days']})",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _compute_regime(self, qqq_closes: List[float], iwm_closes: List[float]) -> str:
        """Core regime logic. Updates internal state metrics."""
        qqq_price = qqq_closes[-1]
        qqq_ma20  = _sma(qqq_closes, MA20_PERIOD) or 0.0
        qqq_ma50  = _sma(qqq_closes, MA50_PERIOD) or 0.0
        iwm_price = iwm_closes[-1]
        iwm_ma50  = _sma(iwm_closes, MA50_PERIOD) or 0.0

        qqq_above_ma20 = _consecutive_above(qqq_closes, MA20_PERIOD)
        qqq_below_ma20 = _consecutive_below(qqq_closes, MA20_PERIOD)

        # Both-above-MA50 streak: min of the two streaks
        qqq_above_ma50 = _consecutive_above(qqq_closes, MA50_PERIOD)
        iwm_above_ma50 = _consecutive_above(iwm_closes, MA50_PERIOD)
        both_above_ma50 = min(qqq_above_ma50, iwm_above_ma50)

        # Store for status display
        self._qqq_price = qqq_price
        self._qqq_ma20  = qqq_ma20
        self._qqq_ma50  = qqq_ma50
        self._iwm_price = iwm_price
        self._iwm_ma50  = iwm_ma50
        self._qqq_above_ma20_days = qqq_above_ma20
        self._qqq_below_ma20_days = qqq_below_ma20
        self._both_above_ma50_days = both_above_ma50

        # ── Decision tree ──
        # BULL takes highest priority (requires both ETFs above MA50)
        if both_above_ma50 >= BULL_DAYS_ABOVE_MA50:
            return REGIME_BULL

        # TRANSITION: QQQ has crossed and held above MA20
        if qqq_above_ma20 >= TRANSITION_DAYS_ABOVE_MA20:
            return REGIME_TRANSITION

        # BEAR: QQQ has been below MA20 for enough consecutive days
        if qqq_below_ma20 >= BEAR_DAYS_BELOW_MA20:
            return REGIME_BEAR

        # In between — keep current regime (hysteresis: don't flip on 1-2 day moves)
        with self._lock:
            return self._regime

    def _on_regime_change(self, old_regime: str, new_regime: str):
        """Called when regime changes. Posts alert and logs."""
        emoji_map = {"BEAR": "🔴", "TRANSITION": "🟡", "BULL": "🟢"}
        old_e = emoji_map.get(old_regime, "⚪")
        new_e = emoji_map.get(new_regime, "⚪")

        with self._lock:
            self._regime_since = date.today()

        action_map = {
            REGIME_BEAR: (
                "Active rule changes:\n"
                "• QQQ → CONFIRMED+bear ≥60 (3d exit)\n"
                "• GOOGL, NVDA, AMZN → SUSPENDED\n"
                "• GLD, SLV → SUSPENDED\n"
                "• MSFT, IWM, META, TSLA → CONFIRMED+bear (no change)\n"
                "• AAPL → CONVERGING+bull 5d (no change)\n"
                "Expected WR: ~92% on bear CONFIRMED group"
            ),
            REGIME_TRANSITION: (
                "Active rule changes:\n"
                "• QQQ → CONFIRMED+bull 60-79 (3d exit)\n"
                "• GOOGL → CONFIRMED+bull 60-79 (3d exit) — REACTIVATED\n"
                "• NVDA → OPPOSING any direction (5d exit) — REACTIVATED\n"
                "• AMZN → OPPOSING any direction (3d exit) — REACTIVATED\n"
                "• MSFT, IWM, META, TSLA bear signals remain active\n"
                "• GLD, SLV still SUSPENDED\n"
                "Expected WR: ~72% blended"
            ),
            REGIME_BULL: (
                "Active rule changes:\n"
                "• GLD → CONFIRMED+bull 60-79, RSI<65 (3d exit) — REACTIVATED\n"
                "• SLV → CONVERGING+bull all scores (5d exit) — REACTIVATED\n"
                "• All TRANSITION rules remain active\n"
                "Expected WR: ~73% blended"
            ),
        }
        action = action_map.get(new_regime, "Review per-ticker rules.")

        msg = (
            f"⚠️ REGIME CHANGE DETECTED\n"
            f"{old_e} {old_regime} → {new_e} {new_regime}\n"
            f"\n"
            f"{action}\n"
            f"\n"
            f"QQQ: ${self._qqq_price:.2f} | MA20: ${self._qqq_ma20:.2f} | MA50: ${self._qqq_ma50:.2f}\n"
            f"IWM MA50: ${self._iwm_ma50:.2f}"
        )

        log.warning(f"REGIME CHANGE: {old_regime} → {new_regime}")

        if self._notify_fn:
            try:
                self._notify_fn(msg)
            except Exception as e:
                log.error(f"Failed to send regime change alert: {e}")


# ── Module-level singleton ──
# Instantiated in app.py and passed to ActiveScanner
_detector: Optional[MarketRegimeDetector] = None


def init_regime_detector(notify_fn: Optional[Callable] = None) -> MarketRegimeDetector:
    """Call once in app.py startup."""
    global _detector
    _detector = MarketRegimeDetector(notify_fn=notify_fn)
    return _detector


def get_market_regime() -> str:
    """Quick access for scanner — returns current regime string."""
    if _detector is None:
        return DEFAULT_REGIME
    return _detector.get_regime()
