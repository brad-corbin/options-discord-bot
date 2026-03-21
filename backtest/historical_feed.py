# backtest/historical_feed.py
# ─────────────────────────────────────────────────────────────────────────────
# Reads the saved CSV of historical bars and provides a get_bars_fn interface
# that matches what your live bot uses — but only reveals bars up to the
# current replay cursor position. This prevents the engine from "seeing
# the future."
#
# This file does NOT call any API. It only reads local CSV files.
# ─────────────────────────────────────────────────────────────────────────────

import csv
from typing import List, Optional


class HistoricalFeed:
    """
    Loads historical bars from a CSV file and exposes them as a cursor-based
    feed. The engine can only see bars up to and including the current cursor
    position — it cannot look ahead.

    Usage:
        feed = HistoricalFeed("backtest/data/SPY_5m.csv")
        days = feed.get_trading_days()   # e.g. ["2026-02-10", "2026-02-11", ...]

        for day in days:
            bars = feed.get_bars_for_day(day)
            for i, bar in enumerate(bars):
                feed.set_cursor(day, i)   # advance cursor to this bar
                # now call engine.evaluate() — it will call get_bars_fn internally
                # and only see bars[0..i] for today

    The get_bars_fn you pass to the engine looks like this:
        def get_bars_fn(ticker, resolution, countback):
            return feed.get_bars_fn(ticker, resolution, countback)
    """

    def __init__(self, csv_path: str):
        """
        Load bars from CSV. The CSV must have these columns:
            timestamp, open, high, low, close, volume, date, datetime_ct
        """
        self._all_bars: List[dict] = []
        self._bars_by_day: dict = {}   # date_str -> list of bar dicts
        self._ticker: str = ""

        # ── Infer ticker from filename ────────────────────────────────────────
        import os
        basename = os.path.basename(csv_path)          # e.g. "SPY_5m.csv"
        self._ticker = basename.split("_")[0].upper()  # e.g. "SPY"

        # ── Read CSV ─────────────────────────────────────────────────────────
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bar = {
                    "timestamp":   int(float(row["timestamp"])),
                    "open":        float(row["open"]),
                    "high":        float(row["high"]),
                    "low":         float(row["low"]),
                    "close":       float(row["close"]),
                    "volume":      int(float(row["volume"])),
                    "date":        row["date"],
                    "datetime_ct": row["datetime_ct"],
                }
                self._all_bars.append(bar)
                self._bars_by_day.setdefault(bar["date"], []).append(bar)

        # Sort each day's bars by timestamp (oldest first)
        for date in self._bars_by_day:
            self._bars_by_day[date].sort(key=lambda b: b["timestamp"])

        # ── Cursor state ──────────────────────────────────────────────────────
        # The cursor controls which bars the engine can see.
        # set_cursor(day, index) reveals bars[0..index] for that day.
        self._current_day: str = ""
        self._current_index: int = 0   # inclusive — bars[0..index] are visible

        print(f"HistoricalFeed loaded: {self._ticker}, "
              f"{len(self._all_bars)} bars, "
              f"{len(self._bars_by_day)} trading days")

    # ── Cursor control ────────────────────────────────────────────────────────

    def set_cursor(self, day: str, index: int):
        """
        Set the replay cursor to bar[index] on the given day.
        After this call, get_bars_fn will only return bars up to this point.
        index is 0-based (0 = first bar of the day).
        """
        self._current_day = day
        self._current_index = index

    def get_current_bar(self) -> Optional[dict]:
        """Return the bar the cursor is currently pointing at."""
        bars = self._bars_by_day.get(self._current_day, [])
        if 0 <= self._current_index < len(bars):
            return bars[self._current_index]
        return None

    # ── Day / bar access ──────────────────────────────────────────────────────

    def get_trading_days(self) -> List[str]:
        """Return sorted list of all trading days in the dataset."""
        return sorted(self._bars_by_day.keys())

    def get_bars_for_day(self, date_str: str) -> List[dict]:
        """Return all bars for a specific trading day, sorted oldest first."""
        return self._bars_by_day.get(date_str, [])

    def get_prior_day_bars(self, date_str: str) -> List[dict]:
        """Return all bars from the trading day immediately before date_str."""
        days = self.get_trading_days()
        if date_str not in days:
            return []
        idx = days.index(date_str)
        if idx == 0:
            return []
        return self._bars_by_day.get(days[idx - 1], [])

    # ── The get_bars_fn that you inject into the engine ───────────────────────

    def get_bars_fn(self, ticker: str, resolution: int, countback: int) -> dict:
        """
        This is the function you pass to ThesisMonitorEngine as get_bars_fn.
        It mimics the MarketData.app response format exactly.

        The engine calls this with:
            get_bars_fn(ticker, 5, 80)   → initialization: all today's bars up to cursor
            get_bars_fn(ticker, 5, 3)    → update: last 3 bars up to cursor

        We ignore resolution (always 5m in our data) and ticker (single-ticker
        feed for now).

        Returns dict in MarketData columnar format:
            {s: "ok", t: [...], o: [...], h: [...], l: [...], c: [...], v: [...]}
        """
        bars_today = self._bars_by_day.get(self._current_day, [])

        # Only reveal bars up to and including the cursor position
        visible = bars_today[: self._current_index + 1]

        # Apply countback — return at most the last N visible bars
        visible = visible[-countback:]

        if not visible:
            return {}

        return {
            "s": "ok",
            "t": [b["timestamp"] for b in visible],
            "o": [b["open"]      for b in visible],
            "h": [b["high"]      for b in visible],
            "l": [b["low"]       for b in visible],
            "c": [b["close"]     for b in visible],
            "v": [b["volume"]    for b in visible],
        }

    # ── Convenience: build prior-day summary for thesis ───────────────────────

    def get_prior_day_summary(self, date_str: str) -> Optional[dict]:
        """
        Returns a summary of the prior trading day's price action.
        Used by the replay engine to build the auto-thesis.

        Returns dict with: high, low, close, open, date
        Returns None if there is no prior day data.
        """
        prior_bars = self.get_prior_day_bars(date_str)
        if not prior_bars:
            return None

        return {
            "high":  max(b["high"]  for b in prior_bars),
            "low":   min(b["low"]   for b in prior_bars),
            "close": prior_bars[-1]["close"],
            "open":  prior_bars[0]["open"],
            "date":  prior_bars[0]["date"],
        }
