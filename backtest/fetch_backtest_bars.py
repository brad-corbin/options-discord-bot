#!/usr/bin/env python3
"""
fetch_backtest_bars.py — one-off historical intraday bar fetcher for backtesting.

Run this ON YOUR DEPLOY MACHINE (Render shell, local clone, whatever) where
your bot's Schwab auth already works.

What it does:
  1. Reads a list of (ticker, date) pairs from BACKTEST_TARGETS (inline below).
  2. For each, fetches 1-minute bars for that full trading day (US/Central).
  3. Writes one CSV per (ticker, date) to OUT_DIR.
  4. Prints a summary when done.

Output format:
  OUT_DIR/<ticker>_<date>.csv
  columns: timestamp_utc,timestamp_ct,open,high,low,close,volume

Dependencies:
  Uses your bot's existing schwab_adapter. Must be run from the bot's
  root directory (where schwab_adapter.py lives) OR with PYTHONPATH
  pointing at it.

Rate limits:
  ~80 fetches total. Schwab API limit is 120/min, so this will complete
  in about 1-2 minutes. Adds a 0.5s sleep between calls to be safe.

To run on Render:
    1. Open Render dashboard → your bot service → Shell tab
    2. cd to bot directory (usually /opt/render/project/src)
    3. python3 fetch_backtest_bars.py
    4. Wait ~2 minutes
    5. Check /tmp/backtest_bars/ for CSVs

To download the CSVs off Render:
    See DOWNLOAD_INSTRUCTIONS below or ask Claude.
"""

import os
import csv
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Target list: (ticker, date_str)  — expanded from backtest analysis
# 22 MEGA setups + 38 TRAP setups + 20 STRONG PIN neutral setups = 80
BACKTEST_TARGETS = [
    # ─── MEGA setup (|bias|>=5, prim_trig=TRUE, STRONG TREND/EXPLOSIVE) ───
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
    # ─── TRAP setup (same filter, prim_trig=FALSE) — need to see why they lost ───
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
    ("QQQ",  "2026-04-22"),
    ("GOOGL","2026-04-22"),
    # ─── STRONG PIN + neutral (condor setup candidates) ───
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
    ("QQQ",  "2026-03-25"),
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

# Dedupe (same ticker/date might appear in multiple buckets)
BACKTEST_TARGETS = sorted(set(BACKTEST_TARGETS), key=lambda x: (x[1], x[0]))

OUT_DIR = "/tmp/backtest_bars"
RATE_LIMIT_SLEEP = 0.5  # seconds between calls


def get_adapter():
    """Load the bot's schwab adapter. Assumes CWD is the bot root."""
    try:
        from schwab_adapter import SchwabAdapter
    except ImportError as e:
        print(f"ERROR: cannot import schwab_adapter. Run from bot root.")
        print(f"       ImportError: {e}")
        sys.exit(1)

    # The adapter needs initialization — look at how app.py does it.
    # Usually: SchwabAdapter(app_key=..., app_secret=..., account_id=...,
    #                        token_path=...)
    # These env vars are typically the same names your bot uses.
    APP_KEY = os.environ.get("SCHWAB_APP_KEY")
    APP_SECRET = os.environ.get("SCHWAB_APP_SECRET")
    TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "/tmp/schwab_token.json")
    ACCOUNT_ID = os.environ.get("SCHWAB_ACCOUNT_ID", "")

    if not APP_KEY or not APP_SECRET:
        print("ERROR: SCHWAB_APP_KEY and SCHWAB_APP_SECRET env vars required.")
        print("       These should already be set in your Render environment.")
        sys.exit(1)

    # Init — signature may vary between versions; adjust to your actual init
    adapter = SchwabAdapter(
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        token_path=TOKEN_PATH,
        account_id=ACCOUNT_ID,
    )
    return adapter


def fetch_1min_for_day(adapter, ticker: str, date_str: str) -> list:
    """
    Fetch 1-minute bars for a specific trading day.
    date_str: 'YYYY-MM-DD' in US/Central date.
    Returns: list of dicts with keys: ts_utc, ts_ct, open, high, low, close, volume
    """
    from schwab.client import Client

    # Build UTC start/end covering CT 08:30 to 15:00 (market hours)
    ct = ZoneInfo("America/Chicago")
    y, m, d = map(int, date_str.split("-"))
    start_ct = datetime(y, m, d, 8, 30, tzinfo=ct)   # pre-open buffer
    end_ct   = datetime(y, m, d, 15, 15, tzinfo=ct)  # post-close buffer
    start_utc = start_ct.astimezone(timezone.utc)
    end_utc   = end_ct.astimezone(timezone.utc)

    try:
        # Use adapter's internal _schwab_get to call get_price_history directly
        raw = adapter._schwab_get(
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
        print(f"  ERROR fetching {ticker} {date_str}: {e}")
        return []

    # Schwab returns dict with 'candles' list
    if not raw or "candles" not in raw:
        print(f"  NO DATA {ticker} {date_str}: raw keys = {list(raw.keys()) if raw else 'None'}")
        return []

    out = []
    for c in raw.get("candles", []):
        # c has: datetime (ms epoch UTC), open, high, low, close, volume
        ts_ms = c.get("datetime")
        if ts_ms is None: continue
        ts_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        ts_ct = ts_utc.astimezone(ct)
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
    print(f"  Frequency: 1-minute bars, 08:30 CT → 15:15 CT per day")
    print()

    adapter = get_adapter()
    print(f"Schwab adapter initialized.")
    print()

    ok = 0
    empty = 0
    err = 0
    for i, (ticker, date_str) in enumerate(BACKTEST_TARGETS, 1):
        print(f"[{i:>2}/{len(BACKTEST_TARGETS)}] {ticker} {date_str}", end=" ")
        sys.stdout.flush()
        try:
            bars = fetch_1min_for_day(adapter, ticker, date_str)
            if not bars:
                print(f"→ empty")
                empty += 1
            else:
                path = write_csv(OUT_DIR, ticker, date_str, bars)
                print(f"→ {len(bars)} bars → {path}")
                ok += 1
        except Exception as e:
            print(f"→ ERROR: {e}")
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
    print("Next step: download this directory from Render, then tell Claude")
    print("the files are uploaded.")


if __name__ == "__main__":
    main()
