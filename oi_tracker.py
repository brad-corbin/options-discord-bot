# oi_tracker.py
# ═══════════════════════════════════════════════════════════════════
# Daily OI Change Tracker — Institutional Flow Detection (Free)
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Tracks aggregate open interest per ticker across all expirations.
# Compares today vs yesterday to detect:
#   - Large OI increases (someone building positions)
#   - Call/put skew changes (directional intent)
#   - Strike concentration (key levels emerging)
#
# Piggybacks on existing chain fetches — zero additional API calls.
# Stores daily snapshots in Redis with 7-day TTL.
#
# This gets you ~60-70% of what Unusual Whales provides.
# What it CAN'T tell you: intraday sweep timing, dark pool prints,
# opening-vs-closing, bought-vs-sold. Those require tick-level data.
#
# Usage:
#   from oi_tracker import OITracker
#   tracker = OITracker(store_get_fn, store_set_fn)
#   tracker.record_chain(ticker, expiration, chain_data)  # call after every chain fetch
#   summary = tracker.get_daily_movers()                  # morning scan
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable, Tuple

log = logging.getLogger(__name__)

# Configuration
OI_DAILY_TTL         = 7 * 86400    # keep 7 days of snapshots
OI_MOVER_THRESHOLD   = 0.15         # 15% change = "mover"
OI_SPIKE_THRESHOLD   = 0.30         # 30% change = "spike"
OI_MIN_TOTAL         = 5000         # ignore tickers with < 5K total OI
OI_TOP_STRIKES       = 5            # track top N strikes by OI change
OI_SUMMARY_HOUR_CT   = 9            # post summary at 9:00 AM CT
OI_SUMMARY_MINUTE_CT = 0


class OITracker:
    """
    Aggregates per-ticker OI from chain fetches.
    Tracks day-over-day changes for flow detection.
    """

    def __init__(self, store_get_fn: Callable, store_set_fn: Callable):
        self._get = store_get_fn
        self._set = store_set_fn
        # In-memory accumulator for today's chains
        # {ticker: {call_oi: int, put_oi: int, strikes: {strike|side: oi}, updated_at: float}}
        self._today: Dict[str, dict] = {}
        self._today_date: str = ""  # YYYY-MM-DD
        self._last_summary_date: str = ""

    def _daily_key(self, ticker: str, date_str: str) -> str:
        return f"oi_daily:{ticker.upper()}:{date_str}"

    def _ensure_today(self):
        """Reset accumulator if the date rolled over."""
        today = date.today().isoformat()
        if today != self._today_date:
            # Save yesterday's accumulated data before resetting
            if self._today_date and self._today:
                self._flush_to_store(self._today_date)
            self._today = {}
            self._today_date = today

    # ── Recording ──

    def record_chain(self, ticker: str, expiration: str, chain_data: dict):
        """
        Record OI from a chain fetch. Call this after every chain pull.
        Aggregates across all expirations for the same ticker on the same day.
        """
        self._ensure_today()

        if not chain_data or not chain_data.get("optionSymbol"):
            return

        ticker = ticker.upper()
        n = len(chain_data.get("optionSymbol", []))
        if n == 0:
            return

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides = col("side", "")
        oi_list = col("openInterest", 0)

        call_oi = 0
        put_oi = 0
        strike_oi: Dict[str, int] = {}

        for i in range(n):
            strike = strikes[i]
            side = str(sides[i] or "").lower()
            oi = int(oi_list[i] or 0)
            if strike is None or side not in ("call", "put") or oi <= 0:
                continue

            if side == "call":
                call_oi += oi
            else:
                put_oi += oi

            key = f"{float(strike):.2f}|{side}"
            strike_oi[key] = oi

        # Merge into today's accumulator (update, not replace — handles multiple expirations)
        if ticker not in self._today:
            self._today[ticker] = {
                "call_oi": 0, "put_oi": 0,
                "strikes": {}, "expirations": set(),
                "updated_at": time.time(),
            }

        entry = self._today[ticker]
        # Only add this expiration's OI if we haven't seen it today
        if expiration not in entry.get("expirations", set()):
            entry["call_oi"] += call_oi
            entry["put_oi"] += put_oi
            for k, v in strike_oi.items():
                entry["strikes"][k] = entry["strikes"].get(k, 0) + v
            entry["expirations"].add(expiration)
            entry["updated_at"] = time.time()

    def _flush_to_store(self, date_str: str):
        """Save today's accumulated OI to Redis."""
        for ticker, data in self._today.items():
            total = data["call_oi"] + data["put_oi"]
            if total < OI_MIN_TOTAL:
                continue

            # Find top strikes by absolute OI
            sorted_strikes = sorted(data["strikes"].items(),
                                     key=lambda x: x[1], reverse=True)
            top = sorted_strikes[:OI_TOP_STRIKES * 2]  # calls + puts

            snapshot = {
                "date": date_str,
                "total_oi": total,
                "call_oi": data["call_oi"],
                "put_oi": data["put_oi"],
                "call_pct": round(data["call_oi"] / total * 100, 1) if total > 0 else 50,
                "put_pct": round(data["put_oi"] / total * 100, 1) if total > 0 else 50,
                "top_strikes": [{"strike_key": k, "oi": v} for k, v in top],
                "expiration_count": len(data.get("expirations", set())),
                "updated_at": data["updated_at"],
            }

            try:
                self._set(
                    self._daily_key(ticker, date_str),
                    json.dumps(snapshot),
                    ttl=OI_DAILY_TTL,
                )
            except Exception as e:
                log.debug(f"OI tracker flush failed for {ticker}: {e}")

    def flush(self):
        """Force flush current day to store. Call at end of day."""
        self._ensure_today()
        if self._today_date and self._today:
            self._flush_to_store(self._today_date)
            log.info(f"OI tracker: flushed {len(self._today)} tickers for {self._today_date}")

    # ── Comparison ──

    def _get_snapshot(self, ticker: str, date_str: str) -> Optional[dict]:
        """Load a daily snapshot from store."""
        try:
            raw = self._get(self._daily_key(ticker, date_str))
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    def get_ticker_change(self, ticker: str) -> Optional[dict]:
        """
        Compare today's OI vs yesterday's for one ticker.
        Returns change dict or None if no comparison available.
        """
        self._ensure_today()
        ticker = ticker.upper()

        today_data = self._today.get(ticker)
        if not today_data:
            return None

        today_total = today_data["call_oi"] + today_data["put_oi"]
        if today_total < OI_MIN_TOTAL:
            return None

        # Find yesterday (skip weekends)
        yesterday = date.today() - timedelta(days=1)
        if yesterday.weekday() == 6:  # Sunday → use Friday
            yesterday -= timedelta(days=2)
        elif yesterday.weekday() == 5:  # Saturday → use Friday
            yesterday -= timedelta(days=1)

        prior = self._get_snapshot(ticker, yesterday.isoformat())
        if not prior:
            return None

        prior_total = prior.get("total_oi", 0)
        if prior_total < OI_MIN_TOTAL:
            return None

        total_change = today_total - prior_total
        total_change_pct = total_change / prior_total if prior_total > 0 else 0

        call_change = today_data["call_oi"] - prior.get("call_oi", 0)
        put_change = today_data["put_oi"] - prior.get("put_oi", 0)
        call_change_pct = call_change / prior.get("call_oi", 1) if prior.get("call_oi", 0) > 0 else 0
        put_change_pct = put_change / prior.get("put_oi", 1) if prior.get("put_oi", 0) > 0 else 0

        # Find which strikes changed most
        today_strikes = today_data.get("strikes", {})
        prior_strikes = {s["strike_key"]: s["oi"]
                        for s in prior.get("top_strikes", [])}

        strike_changes = []
        for key, oi in today_strikes.items():
            prev = prior_strikes.get(key, 0)
            if prev > 0:
                delta = oi - prev
                if abs(delta) > 500:  # meaningful change
                    strike_changes.append({
                        "strike_key": key,
                        "current_oi": oi,
                        "prior_oi": prev,
                        "change": delta,
                        "change_pct": round(delta / prev * 100, 1),
                    })

        strike_changes.sort(key=lambda x: abs(x["change"]), reverse=True)

        # Determine flow direction
        if call_change_pct > 0.10 and put_change_pct < 0.05:
            flow_bias = "BULLISH"
        elif put_change_pct > 0.10 and call_change_pct < 0.05:
            flow_bias = "BEARISH"
        elif total_change_pct > OI_MOVER_THRESHOLD:
            flow_bias = "ACCUMULATION"
        elif total_change_pct < -OI_MOVER_THRESHOLD:
            flow_bias = "UNWINDING"
        else:
            flow_bias = "NEUTRAL"

        return {
            "ticker": ticker,
            "today_total": today_total,
            "prior_total": prior_total,
            "total_change": total_change,
            "total_change_pct": round(total_change_pct * 100, 1),
            "call_oi": today_data["call_oi"],
            "call_change": call_change,
            "call_change_pct": round(call_change_pct * 100, 1),
            "put_oi": today_data["put_oi"],
            "put_change": put_change,
            "put_change_pct": round(put_change_pct * 100, 1),
            "call_put_ratio": round(today_data["call_oi"] / today_data["put_oi"], 2) if today_data["put_oi"] > 0 else 999,
            "flow_bias": flow_bias,
            "top_strike_changes": strike_changes[:OI_TOP_STRIKES],
            "is_mover": abs(total_change_pct) >= OI_MOVER_THRESHOLD,
            "is_spike": abs(total_change_pct) >= OI_SPIKE_THRESHOLD,
        }

    def get_daily_movers(self) -> List[dict]:
        """
        Scan all tracked tickers and return those with significant OI changes.
        Sorted by absolute change percentage.
        """
        self._ensure_today()
        movers = []

        for ticker in self._today:
            change = self.get_ticker_change(ticker)
            if change and change.get("is_mover"):
                movers.append(change)

        movers.sort(key=lambda x: abs(x.get("total_change_pct", 0)), reverse=True)
        return movers

    # ── Summary Formatting ──

    def format_morning_summary(self) -> str:
        """
        Format OI movers for Telegram morning post.
        Shows tickers with 15%+ OI change, top strike changes, flow direction.
        """
        movers = self.get_daily_movers()

        if not movers:
            return "📊 OI Tracker: No significant OI changes detected vs prior session."

        lines = [
            "📊 ── OI CHANGE SUMMARY ──",
            "",
        ]

        spikes = [m for m in movers if m.get("is_spike")]
        regular = [m for m in movers if not m.get("is_spike")]

        if spikes:
            lines.append("🔥 OI SPIKES (30%+):")
            for m in spikes[:5]:
                bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴",
                             "ACCUMULATION": "📈", "UNWINDING": "📉",
                             "NEUTRAL": "⚪"}.get(m["flow_bias"], "❓")
                lines.append(
                    f"  {bias_emoji} {m['ticker']}: {m['total_change_pct']:+.1f}% "
                    f"({m['total_change']:+,} contracts) — {m['flow_bias']}"
                )
                lines.append(
                    f"    Calls: {m['call_change_pct']:+.1f}% | "
                    f"Puts: {m['put_change_pct']:+.1f}% | "
                    f"C/P: {m['call_put_ratio']:.2f}"
                )
                # Top strike change
                if m.get("top_strike_changes"):
                    top = m["top_strike_changes"][0]
                    parts = top["strike_key"].split("|")
                    strike_val = parts[0] if parts else "?"
                    side = parts[1].upper() if len(parts) > 1 else "?"
                    lines.append(
                        f"    Biggest: ${strike_val} {side} "
                        f"{top['change']:+,} ({top['change_pct']:+.1f}%)"
                    )
                lines.append("")

        if regular:
            lines.append("📊 OI MOVERS (15%+):")
            for m in regular[:10]:
                bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴",
                             "ACCUMULATION": "📈", "UNWINDING": "📉",
                             "NEUTRAL": "⚪"}.get(m["flow_bias"], "❓")
                lines.append(
                    f"  {bias_emoji} {m['ticker']}: {m['total_change_pct']:+.1f}% "
                    f"(calls {m['call_change_pct']:+.1f}% / puts {m['put_change_pct']:+.1f}%) "
                    f"— {m['flow_bias']}"
                )

        lines.append("")
        lines.append(f"Tracking {len(self._today)} tickers | "
                    f"{len(movers)} movers detected")

        return "\n".join(lines)

    def format_ticker_detail(self, ticker: str) -> str:
        """Detailed OI breakdown for one ticker (for /oi command)."""
        change = self.get_ticker_change(ticker.upper())
        if not change:
            data = self._today.get(ticker.upper())
            if data:
                total = data["call_oi"] + data["put_oi"]
                return (f"📊 {ticker.upper()} OI: {total:,} total "
                       f"({data['call_oi']:,} calls / {data['put_oi']:,} puts)\n"
                       f"No prior session data for comparison yet.")
            return f"📊 {ticker.upper()}: No OI data recorded today."

        lines = [
            f"📊 ── {change['ticker']} OI DETAIL ──",
            "",
            f"Total OI: {change['today_total']:,} ({change['total_change_pct']:+.1f}% vs prior)",
            f"  Calls: {change['call_oi']:,} ({change['call_change_pct']:+.1f}%)",
            f"  Puts:  {change['put_oi']:,} ({change['put_change_pct']:+.1f}%)",
            f"  C/P Ratio: {change['call_put_ratio']:.2f}",
            f"  Flow: {change['flow_bias']}",
        ]

        if change.get("top_strike_changes"):
            lines.append("")
            lines.append("Top strike changes:")
            for sc in change["top_strike_changes"][:5]:
                parts = sc["strike_key"].split("|")
                strike_val = parts[0] if parts else "?"
                side = parts[1].upper() if len(parts) > 1 else "?"
                lines.append(
                    f"  ${strike_val} {side}: {sc['change']:+,} "
                    f"({sc['change_pct']:+.1f}%) — now {sc['current_oi']:,}"
                )

        return "\n".join(lines)

    # ── Status ──

    @property
    def status(self) -> dict:
        return {
            "tickers_tracked": len(self._today),
            "today_date": self._today_date,
            "last_summary_date": self._last_summary_date,
            "ticker_list": sorted(self._today.keys()),
        }

    def should_post_summary(self) -> bool:
        """Check if it's time to post the morning summary."""
        try:
            import pytz
            ct = pytz.timezone("America/Chicago")
            now = datetime.now(ct)
            if now.weekday() >= 5:
                return False
            today_str = now.strftime("%Y-%m-%d")
            if today_str == self._last_summary_date:
                return False
            if now.hour == OI_SUMMARY_HOUR_CT and now.minute <= 5:
                self._last_summary_date = today_str
                return True
        except Exception:
            pass
        return False
