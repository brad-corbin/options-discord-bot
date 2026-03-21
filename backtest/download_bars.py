# backtest/download_bars.py
# ─────────────────────────────────────────────────────────────────────────────
# Step 1 of the backtest pipeline.
# Downloads historical 5-minute bars from MarketData.app and saves them to a
# CSV file so the replay engine can read them without hitting the API again.
#
# Called automatically by the GitHub Actions workflow.
# You do NOT need to run this manually.
#
# Output: backtest/data/{TICKER}_5m.csv
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import argparse
import requests
import csv
from datetime import datetime, timezone
from pathlib import Path

# ── Read token from environment ───────────────────────────────────────────────
TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: MARKETDATA_TOKEN environment variable is not set.")
    sys.exit(1)

# ── MarketData API helper ─────────────────────────────────────────────────────

def md_get(url: str, params: dict = None) -> dict:
    """Make an authenticated GET request to MarketData.app. Returns parsed JSON."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=20)
    response.raise_for_status()
    return response.json()


def download_5m_bars(ticker: str, days: int) -> list:
    """
    Download historical 5-minute bars for a ticker.

    Args:
        ticker: Stock symbol, e.g. "SPY"
        days: Approximate number of trading days to fetch

    Returns:
        List of dicts, each with keys: timestamp, open, high, low, close, volume, date, datetime_ct
    """
    # 78 bars per trading day (6.5 hours * 12 bars/hour), plus 20% buffer
    countback = int(days * 78 * 1.2)

    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker.upper()}/"
    print(f"  Fetching {ticker} 5m bars (countback={countback})...")

    data = md_get(url, {"countback": countback})

    if not isinstance(data, dict) or data.get("s") != "ok":
        print(f"  ERROR: Bad response from MarketData: {data}")
        sys.exit(1)

    timestamps = data.get("t", [])
    opens      = data.get("o", [])
    highs      = data.get("h", [])
    lows       = data.get("l", [])
    closes     = data.get("c", [])
    volumes    = data.get("v", [])
    n = min(len(timestamps), len(opens), len(closes))

    print(f"  Got {n} raw bars from API")

    rows = []
    for i in range(n):
        ts = timestamps[i]
        # Convert epoch to Central Time strings for human readability
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            dt_ct = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        except ImportError:
            import pytz
            dt_ct = dt_utc.astimezone(pytz.timezone("America/Chicago"))

        rows.append({
            "timestamp":   ts,
            "open":        float(opens[i]),
            "high":        float(highs[i]),
            "low":         float(lows[i]),
            "close":       float(closes[i]),
            "volume":      int(volumes[i]) if i < len(volumes) and volumes[i] else 0,
            "date":        dt_ct.strftime("%Y-%m-%d"),
            "datetime_ct": dt_ct.strftime("%Y-%m-%d %H:%M"),
        })

    # Sort oldest → newest
    rows.sort(key=lambda r: r["timestamp"])

    # Show date range
    dates = sorted(set(r["date"] for r in rows))
    print(f"  Date range: {dates[0]} → {dates[-1]}")
    print(f"  Trading days found: {len(dates)}")

    return rows


def save_to_csv(rows: list, ticker: str, output_dir: str) -> str:
    """Save bar data to CSV. Returns the file path."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    filepath = os.path.join(output_dir, f"{ticker.upper()}_5m.csv")

    fieldnames = ["timestamp", "open", "high", "low", "close", "volume", "date", "datetime_ct"]
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Saved {len(rows)} bars to: {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Download historical 5m bars from MarketData.app")
    parser.add_argument("--ticker", default="SPY", help="Ticker symbol (default: SPY)")
    parser.add_argument("--days",   default=22, type=int, help="Trading days to fetch (default: 22)")
    parser.add_argument("--output", default=None, help="Output directory (default: backtest/data/)")
    args = parser.parse_args()

    # Default output dir is backtest/data/ relative to this script's location
    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, "data")

    print(f"\n{'='*55}")
    print(f"  Downloading {args.ticker} historical bars")
    print(f"{'='*55}")

    rows = download_5m_bars(args.ticker, args.days)
    path = save_to_csv(rows, args.ticker, args.output)

    # Print a quick preview
    print("\nFirst 3 bars:")
    for r in rows[:3]:
        print(f"  {r['datetime_ct']}  O={r['open']:.2f}  H={r['high']:.2f}  L={r['low']:.2f}  C={r['close']:.2f}  V={r['volume']}")

    print(f"\n✅ Download complete → {path}")
    print(f"   Total bars: {len(rows)}")


if __name__ == "__main__":
    main()
