#!/usr/bin/env python3
"""
fetch_backtest_bars.py — one-off historical intraday bar fetcher for backtesting.

Usage (from bot root, e.g. /opt/render/project/src):
    PYTHONPATH=. python3 backtest/fetch_backtest_bars.py

Output: one CSV per ticker-date pair in /tmp/backtest_bars/
"""

import os
import csv
import sys
import time
from datetime import datetime, timedelta, timezone

# Use zoneinfo (Py3.9+) or fall back to pytz
try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except ImportError:
    import pytz
    CT = pytz.timezone("America/Chicago")

# Target list: (ticker, date_str)  — from thesis reconciliation analysis
BACKTEST_TARGETS = [
    # MEGA setup (|bias|>=5, prim_trig=TRUE, STRONG TREND/EXPLOSIVE)
    ("QQQ",  "2026-04-09"),
    ("QQQ",  "2026-04-15"),
    ("AAPL", "2026-04-15"),
    ("AVGO", "2026-04-15"),
    ("META", "2026-04-15"),
    ("MSFT", "2026-04-15"),
    ("TSLA", "2026-04-15"),
    ("QQQ",  "2026-04-17"),
    ("AAPL", "2026-04-17"),
    ("META", "2026-04-17"),
    ("NVDA", "2026-04-17"),
    ("PLTR", "2026-04-17"),
    ("SPY",  "2026-04-17"),
    ("TSLA", "2026-04-17"),
    ("UNH",  "2026-04-17"),
    ("AAPL", "2026-04-20"),
    ("SPY",  "2026-04-22"),
    ("AAPL", "2026-04-22"),
    ("AMZN", "2026-04-22"),
    ("AVGO", "2026-04-22"),
    ("MSFT", "2026-04-22"),
    # TRAP (same filter, prim_trig=FALSE)
    ("QQQ",  "2026-03-25"),
    ("SPY",  "2026-04-06"),
    ("QQQ",  "2026-04-06"),
    ("SPY",  "2026-04-09"),
    ("QQQ",  "2026-04-10"),
    ("SPY",  "2026-04-10"),
    ("QQQ",  "2026-04-13"),
    ("SPY",  "2026-04-15"),
    ("AMZN", "2026-04-15"),
    ("GOOGL","2026-04-15"),
    ("NVDA", "2026-04-15"),
    ("TLT",  "2026-04-15"),
    ("AMD",  "2026-04-17"),
    ("ARM",  "2026-04-17"),
    ("BA",   "2026-04-17"),
    ("CAT",  "2026-04-17"),
    ("CRM",  "2026-04-17"),
    ("GS",   "2026-04-17"),
    ("JPM",  "2026-04-17"),
    ("MRNA", "2026-04-17"),
    ("MSFT", "2026-04-17"),
    ("SOFI", "2026-04-17"),
    ("TLT",  "2026-04-17"),
    ("SPY",  "2026-04-20"),
    ("AVGO", "2026-04-20"),
    ("GOOGL","2026-04-20"),
    ("META", "2026-04-20"),
    ("TSLA", "2026-04-20"),
    ("QQQ",  "2026-04-21"),
    ("QQQ",  "2026-04-22"),
    ("SPY",  "2026-04-22"),
    ("GOOGL","2026-04-22"),
    # STRONG PIN + neutral
    ("SPY",  "2026-03-18"),
    ("QQQ",  "2026-03-18"),
    ("SPY",  "2026-03-19"),
    ("QQQ",  "2026-03-19"),
    ("META", "2026-03-20"),
    ("SPY",  "2026-03-20"),
    ("QQQ",  "2026-03-23"),
    ("SPY",  "2026-03-24"),
    ("QQQ",  "2026-03-24"),
    ("SPY",  "2026-03-25"),
    ("QQQ",  "2026-03-26"),
    ("SPY",  "2026-03-26"),
    ("QQQ",  "2026-03-27"),
    ("SPY",  "2026-03-27"),
    ("QQQ",  "2026-03-30"),
    ("SPY",  "2026-03-30"),
    ("QQQ",  "2026-04-01"),
    ("SPY",  "2026-04-01"),
    ("QQQ",  "2026-04-02"),
    ("LLY",  "2026-04-17"),
    ("NFLX", "2026-04-17"),
    ("GLD",  "2026-04-17"),
    ("XLV",  "2026-04-17"),
    ("XLE",  "2026-04-17"),
]

BACKTEST_TARGETS = sorted(set(BACKTEST_TARGETS), key=lambda x: (x[1], x[0]))

OUT_DIR = "/tmp/backtest_bars"
RATE_LIMIT_SLEEP = 0.5


def get_provider():
    """Load the bot's Schwab data provider."""
    try:
        from schwab_adapter import SchwabDataProvider
    except ImportError as e:
        print(f"ERROR: cannot import SchwabDataProvider from schwab_adapter.")
        print(f"       ImportError: {e}")
        print(f"       Run from bot root with: PYTHONPATH=. python3 backtest/fetch_backtest_bars.py")
        sys.exit(1)

    provider = SchwabDataProvider()  # uses default client init, reads SCHWAB_* env vars
    if not provider.available:
        print("ERROR: SchwabDataProvider is not available.")
        print("       Check that SCHWAB_APP_KEY, SCHWAB_APP_SECRET, and")
        print("       schwab_token.json are set up in the bot's env.")
        sys.exit(1)
    return provider


def fetch_1min_for_day(provider, ticker: str, date_str: str) -> list:
    """
    Fetch 1-minute bars for a specific trading day (CT dates).
    Returns list of dicts: ts_utc, ts_ct, open, high, low, close, volume
    """
    from schwab.client import Client

    y, m, d = map(int, date_str.split("-"))
    # Build CT datetimes for pre-open and post-close, convert to UTC
    try:
        # zoneinfo path
        start_ct = datetime(y, m, d, 8, 30, tzinfo=CT)
        end_ct   = datetime(y, m, d, 15, 15, tzinfo=CT)
    except TypeError:
        # pytz path
        start_ct = CT.localize(datetime(y, m, d, 8, 30))
        end_ct   = CT.localize(datetime(y, m, d, 15, 15))
    start_utc = start_ct.astimezone(timezone.utc)
    end_utc   = end_ct.astimezone(timezone.utc)

    try:
        raw = provider._schwab_get(
            "get_price_history",
            ticker.upper(),
            period_type=Client.PriceHistory.PeriodType.DAY,
            frequency_type=Client.PriceHistory.FrequencyType.MINUTE,
            frequency=Client.PriceHistory.Frequency.EVERY_MINUTE,
            start_datetime=start_utc,
            end_datetime=end_utc,
            need_extended_hours_data=False,
        )
    except Exception as e:
        print(f"  ERROR fetching {ticker} {date_str}: {type(e).__name__}: {e}")
        return []

    if not raw:
        return []
    candles = raw.get("candles") if isinstance(raw, dict) else None
    if not candles:
        return []

    out = []
    for c in candles:
        ts_ms = c.get("datetime")
        if ts_ms is None:
            continue
        ts_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        ts_ct = ts_utc.astimezone(CT)
        out.append({
            "ts_utc": ts_utc.isoformat(),
            "ts_ct": ts_ct.isoformat(),
            "open": c.get("open"),
            "high": c.get("high"),
            "low": c.get("low"),
            "close": c.get("close"),
            "volume": c.get("volume", 0),
        })
    return out


def write_csv(out_dir: str, ticker: str, date_str: str, bars: list):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{ticker}_{date_str}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["ts_utc", "ts_ct", "open", "high", "low", "close", "volume"],
        )
        w.writeheader()
        for b in bars:
            w.writerow(b)
    return path


def main():
    print(f"Backtest bar fetcher")
    print(f"  Targets:   {len(BACKTEST_TARGETS)} ticker-date pairs")
    print(f"  Output:    {OUT_DIR}/")
    print(f"  Frequency: 1-minute bars, 08:30 CT -> 15:15 CT per day")
    print()

    provider = get_provider()
    print(f"Schwab provider initialized.")
    print()

    ok = 0
    empty = 0
    err = 0
    for i, (ticker, date_str) in enumerate(BACKTEST_TARGETS, 1):
        print(f"[{i:>2}/{len(BACKTEST_TARGETS)}] {ticker} {date_str}", end=" ")
        sys.stdout.flush()
        try:
            bars = fetch_1min_for_day(provider, ticker, date_str)
            if not bars:
                print(f"-> empty")
                empty += 1
            else:
                path = write_csv(OUT_DIR, ticker, date_str, bars)
                print(f"-> {len(bars)} bars -> {path}")
                ok += 1
        except Exception as e:
            print(f"-> ERROR: {type(e).__name__}: {e}")
            err += 1
        time.sleep(RATE_LIMIT_SLEEP)

    print()
    print("=" * 60)
    print(f"DONE")
    print(f"  Success: {ok}")
    print(f"  Empty:   {empty}")
    print(f"  Errors:  {err}")
    print()
    print(f"Output in {OUT_DIR}/")
    print()
    print("Next: tar -czf /tmp/bars.tar.gz -C /tmp backtest_bars/")
    print("      base64 /tmp/bars.tar.gz > /tmp/bars.b64")
    print("      cat /tmp/bars.b64   # copy output, paste into next Claude chat")


if __name__ == "__main__":
    main()
