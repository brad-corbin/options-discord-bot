# shadow_filter.py
# ═══════════════════════════════════════════════════════════════════
# Shadow Filter Logger — Tracks signals blocked by v7 filters
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Every signal that WOULD have been traded under old rules but is
# now blocked by the new v7 filters gets logged here with its
# actual outcome filled in after the fact.
#
# This prevents overfitting by continuously validating that filters
# are saving money, not blocking edge.
#
# Usage in app.py:
#   from shadow_filter import log_filtered_signal, reconcile_shadow_outcomes
#   
#   # In signal processing:
#   should_filter, reason, category = should_filter_signal(ticker, bias, regime)
#   if should_filter:
#       log_filtered_signal(ticker, bias, signal_data, reason, category, spot)
#
#   # Nightly after close:
#   reconcile_shadow_outcomes(md_get_fn)
# ═══════════════════════════════════════════════════════════════════

import logging
import time
import json
import threading
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional, Callable

log = logging.getLogger(__name__)

# Alert thresholds
SHADOW_ALERT_MIN_SIGNALS = 30        # minimum signals before alerting
SHADOW_ALERT_WR_THRESHOLD = 0.55     # alert if shadow WR exceeds this
SHADOW_ALERT_EV_THRESHOLD = 1.0      # alert if shadow EV exceeds this %
SHADOW_MONTHLY_MIN_SIGNALS = 15      # minimum for monthly ticker review
SHADOW_TICKER_RECOVERY_WR = 0.55     # removed ticker alert threshold
SHADOW_TICKER_RECOVERY_MONTHS = 2    # consecutive months above threshold

# Storage keys
SHADOW_LOG_KEY = "shadow_filter:log"
SHADOW_STATS_KEY = "shadow_filter:stats"
SHADOW_MONTHLY_KEY = "shadow_filter:monthly"


class ShadowFilterLogger:
    """Logs filtered signals and tracks their outcomes for filter validation."""

    def __init__(self, persistent_state=None, post_fn=None, sheet_append_fn=None):
        """
        Args:
            persistent_state: PersistentState instance for storage
            post_fn: function to post Telegram alerts
            sheet_append_fn: function to append to Google Sheets
        """
        self._state = persistent_state
        self._post_fn = post_fn
        self._sheet_fn = sheet_append_fn
        self._lock = threading.Lock()
        self._pending = []  # signals awaiting outcome reconciliation

    def log_filtered_signal(
        self,
        ticker: str,
        bias: str,
        signal_data: dict,
        filter_reason: str,
        filter_category: str,
        spot: float,
        engine: str = "active_scanner",
        option_symbol: str = None,
        option_strike: float = None,
        option_expiry: str = None,
        option_entry_mid: float = None,
        option_dte: int = None,
    ):
        """Log a signal that was blocked by v7 filters.
        
        Only call this for signals that WOULD have been traded under old rules
        but are now blocked by the new filters.
        
        For option P&L tracking, pass the option contract that would have been
        traded. If not available at signal time, reconciliation will attempt to
        look up the ATM option from the chain.
        """
        now = datetime.now(timezone.utc)
        opt_type = "put" if bias == "bear" else "call"
        entry = {
            "ts_utc": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "ticker": ticker.upper(),
            "engine": engine,
            "bias": bias,
            "score": signal_data.get("score", 0),
            "tier": signal_data.get("tier", "2"),
            "htf_status": signal_data.get("htf_status", "UNKNOWN"),
            "confidence": signal_data.get("confidence", 0),
            "volume_ratio": signal_data.get("volume_ratio", 1.0),
            "phase": signal_data.get("phase", "UNKNOWN"),
            "regime": signal_data.get("regime", "UNKNOWN"),
            "vix": signal_data.get("vix", 0),
            "spot_at_signal": round(spot, 2),
            "filter_applied": filter_reason,
            "filter_category": filter_category,
            "would_have_traded": True,
            # Outcomes — filled in by reconciliation (close-based)
            "eod_price": None,
            "close_1d": None, "close_3d": None, "close_5d": None,
            "pnl_eod": None, "pnl_1d": None, "pnl_3d": None, "pnl_5d": None,
            "win_eod": None, "win_1d": None, "win_3d": None, "win_5d": None,
            # ── OPTION P&L TRACKING ──────────────────────────────
            # Track the actual option that would have been traded.
            # Record entry premium at signal time, then fetch the
            # option's mid price each night through expiration.
            # Peak value = highest mid seen = best exit opportunity.
            "option_symbol": option_symbol,
            "option_strike": option_strike,
            "option_expiry": option_expiry,
            "option_type": opt_type,
            "option_entry_mid": option_entry_mid,
            "option_dte_at_entry": option_dte,
            # Nightly snapshots — filled in each night by reconciliation
            "option_mids": [],           # list of {"date": "YYYY-MM-DD", "mid": float}
            "option_peak_mid": None,     # highest mid seen through expiration
            "option_peak_date": None,    # date of peak mid
            "option_pnl_peak_pct": None, # (peak_mid - entry_mid) / entry_mid * 100
            "option_pnl_peak_dollars": None,  # peak_mid - entry_mid per contract
            "option_final_mid": None,    # last mid before expiration
            "option_pnl_final_pct": None,# P&L at expiration / final close
            "option_win_at_peak": None,  # True if peak mid > entry mid
            "option_win_at_close": None, # True if held to expiry and still won
            "reconciled": False,
        }

        with self._lock:
            self._pending.append(entry)

        # Persist
        if self._state:
            try:
                existing = self._state._json_get(SHADOW_LOG_KEY) or []
                existing.append(entry)
                # Keep last 500 entries
                if len(existing) > 500:
                    existing = existing[-500:]
                self._state._json_set(SHADOW_LOG_KEY, existing, ttl=86400 * 90)
            except Exception as e:
                log.warning(f"Shadow filter persist failed: {e}")

        # Google Sheets
        if self._sheet_fn:
            try:
                self._sheet_fn("shadow_filtered_signals", entry)
            except Exception as e:
                log.debug(f"Shadow sheet append failed: {e}")

        log.info(f"Shadow logged: {ticker} {bias} blocked by {filter_category}: {filter_reason}")

    def reconcile_outcomes(self, spot_fn: Callable, daily_bar_fn: Callable = None):
        """Fill in EOD/1d/3d/5d outcomes AND MFE for pending shadow signals.
        Call after market close each day.
        
        Args:
            spot_fn: function(ticker) -> float that returns current/EOD price
            daily_bar_fn: function(ticker, date_str) -> dict with 'high','low','close'
                          If None, falls back to spot_fn only (no MFE tracking).
                          date_str is 'YYYY-MM-DD'. Returns None if no data.
        """
        if not self._state:
            return

        try:
            entries = self._state._json_get(SHADOW_LOG_KEY) or []
        except Exception:
            return

        today = date.today().isoformat()
        updated = False

        for entry in entries:
            if entry.get("reconciled"):
                continue

            signal_date = entry.get("date", "")
            if not signal_date:
                continue

            try:
                sig_date = datetime.strptime(signal_date, "%Y-%m-%d").date()
            except ValueError:
                continue

            days_ago = (date.today() - sig_date).days
            ticker = entry.get("ticker", "")
            spot_at = entry.get("spot_at_signal", 0)
            bias = entry.get("bias", "bull")

            if spot_at <= 0 or not ticker:
                continue

            # Get current price for this ticker
            try:
                current_price = spot_fn(ticker)
                if not current_price or current_price <= 0:
                    continue
            except Exception:
                continue

            # ── Update MFE: track high/low through DTE period ──
            # Each night, update the running high/low from signal date through today
            if daily_bar_fn and days_ago >= 0 and days_ago <= 7:
                running_high = entry.get("high_through_dte") or spot_at
                running_low = entry.get("low_through_dte") or spot_at

                # Check each trading day from signal through today
                check_date = sig_date
                while check_date <= date.today():
                    try:
                        bar = daily_bar_fn(ticker, check_date.strftime("%Y-%m-%d"))
                        if bar:
                            day_high = bar.get("high", 0) or 0
                            day_low = bar.get("low", 0) or 0
                            if day_high > 0:
                                running_high = max(running_high, day_high)
                            if day_low > 0:
                                running_low = min(running_low, day_low)
                    except Exception:
                        pass
                    check_date += timedelta(days=1)

                entry["high_through_dte"] = round(running_high, 2)
                entry["low_through_dte"] = round(running_low, 2)

                # Compute MFE based on bias
                if bias == "bull":
                    mfe_move = running_high - spot_at
                    entry["mfe_price"] = round(running_high, 2)
                else:
                    mfe_move = spot_at - running_low
                    entry["mfe_price"] = round(running_low, 2)

                entry["mfe_pct"] = round(mfe_move / spot_at * 100, 3)
                entry["win_mfe"] = mfe_move > 0
                updated = True
            elif days_ago >= 0:
                # Fallback: use spot price for rough MFE estimate
                running_high = max(entry.get("high_through_dte") or spot_at, current_price)
                running_low = min(entry.get("low_through_dte") or spot_at, current_price)
                entry["high_through_dte"] = round(running_high, 2)
                entry["low_through_dte"] = round(running_low, 2)
                if bias == "bull":
                    entry["mfe_pct"] = round((running_high - spot_at) / spot_at * 100, 3)
                    entry["mfe_price"] = round(running_high, 2)
                else:
                    entry["mfe_pct"] = round((spot_at - running_low) / spot_at * 100, 3)
                    entry["mfe_price"] = round(running_low, 2)
                entry["win_mfe"] = (entry["mfe_pct"] or 0) > 0
                updated = True

            # ── Fill close-based outcomes by day ──
            if days_ago >= 0 and entry.get("eod_price") is None:
                entry["eod_price"] = current_price
                pnl = (current_price - spot_at) if bias == "bull" else (spot_at - current_price)
                entry["pnl_eod"] = round(pnl / spot_at * 100, 3)
                entry["win_eod"] = pnl > 0
                updated = True

            if days_ago >= 1 and entry.get("close_1d") is None:
                entry["close_1d"] = current_price
                pnl = (current_price - spot_at) if bias == "bull" else (spot_at - current_price)
                entry["pnl_1d"] = round(pnl / spot_at * 100, 3)
                entry["win_1d"] = pnl > 0
                updated = True

            if days_ago >= 3 and entry.get("close_3d") is None:
                entry["close_3d"] = current_price
                pnl = (current_price - spot_at) if bias == "bull" else (spot_at - current_price)
                entry["pnl_3d"] = round(pnl / spot_at * 100, 3)
                entry["win_3d"] = pnl > 0
                updated = True

            if days_ago >= 5 and entry.get("close_5d") is None:
                entry["close_5d"] = current_price
                pnl = (current_price - spot_at) if bias == "bull" else (spot_at - current_price)
                entry["pnl_5d"] = round(pnl / spot_at * 100, 3)
                entry["win_5d"] = pnl > 0

                # Compute how much better MFE was than close
                mfe_pct = entry.get("mfe_pct", 0) or 0
                close_pct = entry["pnl_5d"]
                entry["mfe_vs_close_5d"] = round(mfe_pct - close_pct, 3)

                entry["reconciled"] = True
                updated = True

        if updated:
            try:
                self._state._json_set(SHADOW_LOG_KEY, entries, ttl=86400 * 90)
            except Exception:
                pass

    def compute_filter_scorecard(self) -> Dict:
        """Compute how each filter category is performing.
        Returns dict of {category: {n, wr, ev, saved_pct, cost_pct}}."""
        if not self._state:
            return {}

        try:
            entries = self._state._json_get(SHADOW_LOG_KEY) or []
        except Exception:
            return {}

        reconciled = [e for e in entries if e.get("reconciled")]
        if not reconciled:
            return {}

        categories = {}
        for e in reconciled:
            cat = e.get("filter_category", "unknown")
            if cat not in categories:
                categories[cat] = {"signals": [], "wins_5d": 0, "total_ev": 0, "n": 0}
            categories[cat]["signals"].append(e)
            categories[cat]["n"] += 1
            if e.get("win_5d"):
                categories[cat]["wins_5d"] += 1
            if e.get("pnl_5d") is not None:
                categories[cat]["total_ev"] += e["pnl_5d"]

        result = {}
        for cat, data in categories.items():
            n = data["n"]
            wr = data["wins_5d"] / n if n > 0 else 0
            ev = data["total_ev"] / n if n > 0 else 0
            saved = 1 - wr  # % of blocked signals that WERE losers (filter saved us)
            result[cat] = {
                "n": n, "wr_5d": round(wr * 100, 1), "ev_5d": round(ev, 3),
                "saved_pct": round(saved * 100, 1),
                "cost_pct": round(wr * 100, 1),  # % that were winners we missed
            }

        return result

    def check_alert_thresholds(self) -> List[str]:
        """Check if any filter is blocking too many winners.
        Returns list of alert messages to send."""
        scorecard = self.compute_filter_scorecard()
        if not scorecard:
            return []

        alerts = []
        for cat, data in scorecard.items():
            if data["n"] < SHADOW_ALERT_MIN_SIGNALS:
                continue

            wr = data["wr_5d"] / 100
            ev = data["ev_5d"]

            if wr > SHADOW_ALERT_WR_THRESHOLD:
                alerts.append(
                    f"⚠️ SHADOW ALERT: '{cat}' filter is blocking signals that are "
                    f"{data['wr_5d']:.0f}% winners at 5d over {data['n']} signals. "
                    f"Avg EV: {ev:+.2f}%. Filter may need review."
                )

            if ev > SHADOW_ALERT_EV_THRESHOLD:
                alerts.append(
                    f"⚠️ SHADOW ALERT: '{cat}' filter is blocking +{ev:.2f}% EV "
                    f"signals ({data['n']} signals). Significant money may be left on table."
                )

        # Check removed tickers specifically
        try:
            entries = self._state._json_get(SHADOW_LOG_KEY) or []
        except Exception:
            entries = []

        for ticker in ["META", "NFLX"]:
            ticker_entries = [e for e in entries if e.get("ticker") == ticker and e.get("reconciled")]
            if len(ticker_entries) >= SHADOW_MONTHLY_MIN_SIGNALS:
                wins = sum(1 for e in ticker_entries if e.get("win_5d"))
                wr = wins / len(ticker_entries)
                if wr > SHADOW_TICKER_RECOVERY_WR:
                    alerts.append(
                        f"⚠️ SHADOW TICKER RECOVERY: {ticker} shadow signals are "
                        f"{wr*100:.0f}% winners at 5d over {len(ticker_entries)} signals. "
                        f"Consider re-enabling."
                    )

        return alerts

    def format_monthly_report(self) -> str:
        """Generate monthly shadow filter report."""
        scorecard = self.compute_filter_scorecard()
        if not scorecard:
            return "📊 Shadow Filter Report: No reconciled signals yet."

        lines = [
            "📊 SHADOW FILTER MONTHLY REPORT",
            "═" * 40,
            "",
        ]

        total_blocked = sum(d["n"] for d in scorecard.values())
        total_saved = sum(d["n"] * d["saved_pct"] / 100 for d in scorecard.values())

        lines.append(f"Total blocked signals: {total_blocked}")
        lines.append(f"Correctly blocked (losers): {total_saved:.0f} ({total_saved/total_blocked*100:.0f}%)")
        lines.append("")

        for cat, data in sorted(scorecard.items(), key=lambda x: -x[1]["n"]):
            emoji = "✅" if data["saved_pct"] > 55 else "⚠️" if data["saved_pct"] > 45 else "🔴"
            lines.append(
                f"{emoji} {cat}: {data['n']} signals | "
                f"Shadow WR: {data['wr_5d']:.0f}% | "
                f"EV: {data['ev_5d']:+.2f}% | "
                f"Saved: {data['saved_pct']:.0f}%"
            )

        alerts = self.check_alert_thresholds()
        if alerts:
            lines.append("")
            lines.append("⚠️ ALERTS:")
            for a in alerts:
                lines.append(f"  {a}")

        return "\n".join(lines)
