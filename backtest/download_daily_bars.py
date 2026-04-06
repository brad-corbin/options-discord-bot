# backtest/download_daily_bars.py
# ─────────────────────────────────────────────────────────────────────────────
# Downloads daily OHLCV bars from MarketData.app for the swing backtest.
# Always downloads both the target ticker AND SPY (needed for relative
# strength calculation inside analyze_swing_setup).
#
# Requires Trader plan — uses native from/to date parameters.
#
# Examples:
#   python backtest/download_daily_bars.py --ticker AAPL --from 2024-06-01 --to 2026-04-04
#   python backtest/download_daily_bars.py --ticker NVDA --from 2024-06-01
#
# Output:
#   backtest/data/{TICKER}_daily_{from}_{to}.csv
#   backtest/data/SPY_daily_{from}_{to}.csv   (always)
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import time
import argparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: MARKETDATA_TOKEN environment variable is not set.")
    sys.exit(1)

MAX_RETRIES  = 4
BASE_BACKOFF = 15


def md_get(url: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    delay = BASE_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=30)
            if r.status_code in (429, 403):
                wait = max(int(r.headers.get("Retry-After", delay)), delay)
                if attempt < MAX_RETRIES:
                    print(f"  [{r.status_code}] Waiting {wait}s (attempt {attempt}/{MAX_RETRIES})...")
                    time.sleep(wait)
                    delay = min(delay * 2, 120)
                    continue
                r.raise_for_status()
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                print(f"  Timeout. Retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                print("  ERROR: Request timed out.")
                sys.exit(1)
    sys.exit(1)


def fetch_daily(ticker: str, from_date: str, to_date: str) -> list:
    """Fetch daily bars. to_date is bumped +1 day (MarketData to is exclusive)."""
    to_exc = (datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api.marketdata.app/v1/stocks/candles/D/{ticker.upper()}/"
    print(f"  Fetching {ticker.upper()} daily bars {from_date} → {to_date}...")
    data = md_get(url, {"from": from_date, "to": to_exc})

    if not isinstance(data, dict) or data.get("s") != "ok":
        print(f"  ERROR: Bad response: {data}")
        sys.exit(1)

    t  = data.get("t", [])
    o  = data.get("o", [])
    h  = data.get("h", [])
    l  = data.get("l", [])
    c  = data.get("c", [])
    v  = data.get("v", [])
    n  = min(len(t), len(o), len(c))

    rows = []
    for i in range(n):
        dt_utc = datetime.fromtimestamp(t[i], tz=timezone.utc)
        rows.append({
            "timestamp": t[i],
            "date":      dt_utc.strftime("%Y-%m-%d"),
            "o":  float(o[i]),
            "h":  float(h[i]),
            "l":  float(l[i]),
            "c":  float(c[i]),
            "v":  int(v[i]) if i < len(v) and v[i] else 0,
        })

    rows.sort(key=lambda r: r["timestamp"])
    print(f"  Got {len(rows)} daily bars")
    return rows


def save_csv(rows: list, ticker: str, output_dir: str, from_date: str, to_date: str) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    label    = f"{from_date}_{to_date}"
    filename = f"{ticker.upper()}_daily_{label}.csv"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "date", "o", "h", "l", "c", "v"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {len(rows)} bars → {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Download daily bars for swing backtest")
    parser.add_argument("--ticker",  required=True, help="Ticker symbol")
    parser.add_argument("--from",    dest="from_date", required=True,
                        help="Start date YYYY-MM-DD (include warmup — use 2024-06-01 for a Jul-2025 backtest start)")
    parser.add_argument("--to",      dest="to_date", default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--output",  default=None, help="Output directory (default: backtest/data/)")
    args = parser.parse_args()

    to_date = args.to_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, "data")

    print(f"\n{'='*55}")
    print(f"  Downloading daily bars for swing backtest")
    print(f"  Ticker: {args.ticker.upper()}   SPY (always)")
    print(f"  Range:  {args.from_date} → {to_date}")
    print(f"{'='*55}")

    ticker_rows = fetch_daily(args.ticker, args.from_date, to_date)
    ticker_path = save_csv(ticker_rows, args.ticker, args.output, args.from_date, to_date)

    time.sleep(2)  # brief pause between requests

    spy_rows = fetch_daily("SPY", args.from_date, to_date)
    spy_path = save_csv(spy_rows, "SPY", args.output, args.from_date, to_date)

    print(f"\n✅ Done.")
    print(f"   {args.ticker.upper()}: {len(ticker_rows)} bars → {ticker_path}")
    print(f"   SPY:  {len(spy_rows)} bars → {spy_path}")
    print(f"\nNext step:")
    print(f"   python backtest/swing_backtest.py \\")
    print(f"     --ticker {args.ticker.upper()} \\")
    print(f"     --data {ticker_path} \\")
    print(f"     --spy  {spy_path} \\")
    print(f"     --backtest-from 2025-07-14")


if __name__ == "__main__":
    main()
