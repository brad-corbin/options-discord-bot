# backtest/download_bars.py
# ─────────────────────────────────────────────────────────────────────────────
# Downloads historical 5-minute bars from MarketData.app and saves to CSV.
#
# MarketData's free/starter plan only supports countback (fetch last N bars).
# The from/to date API parameters require a higher paid tier and return 403.
#
# This version works around that by:
#   1. Calculating how many calendar days back the start date is from today
#   2. Converting that to a bar countback (78 bars/trading day * 1.4 buffer)
#   3. Fetching with countback, then trimming rows to the requested date window
#
# Examples:
#   python backtest/download_bars.py                          # last 22 trading days
#   python backtest/download_bars.py --from 2025-10-01 --to 2025-11-28
#   python backtest/download_bars.py --ticker QQQ --from 2025-07-01 --to 2025-08-29
#
# Output: backtest/data/{TICKER}_5m_{LABEL}.csv
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import argparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Token ─────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: MARKETDATA_TOKEN environment variable is not set.")
    sys.exit(1)


# ── MarketData API ─────────────────────────────────────────────────────────────

def md_get(url: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=20)
    response.raise_for_status()
    return response.json()


def to_ct_str(epoch: float) -> str:
    """Convert epoch timestamp to Central Time human-readable string."""
    dt_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        dt_ct = dt_utc.astimezone(ZoneInfo("America/Chicago"))
    except ImportError:
        import pytz
        dt_ct = dt_utc.astimezone(pytz.timezone("America/Chicago"))
    return dt_ct.strftime("%Y-%m-%d %H:%M")


def to_date_ct(epoch: float) -> str:
    """Convert epoch to YYYY-MM-DD in Central Time."""
    return to_ct_str(epoch)[:10]


def calendar_days_since(date_str: str) -> int:
    """How many calendar days between date_str (YYYY-MM-DD) and today?"""
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    today  = datetime.now(timezone.utc).date()
    return (today - target).days


# ── Core download ─────────────────────────────────────────────────────────────

def download_with_countback(ticker: str, countback: int) -> list:
    """
    Fetch bars using countback. This works on all MarketData plan tiers.
    Returns list of row dicts sorted oldest → newest.
    """
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker.upper()}/"
    print(f"  Fetching {ticker} (countback={countback})...")
    data = md_get(url, {"countback": countback})

    if not isinstance(data, dict) or data.get("s") != "ok":
        print(f"  ERROR: Bad API response: {data}")
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
        rows.append({
            "timestamp":   ts,
            "open":        float(opens[i]),
            "high":        float(highs[i]),
            "low":         float(lows[i]),
            "close":       float(closes[i]),
            "volume":      int(volumes[i]) if i < len(volumes) and volumes[i] else 0,
            "date":        to_date_ct(ts),
            "datetime_ct": to_ct_str(ts),
        })

    rows.sort(key=lambda r: r["timestamp"])
    return rows


def trim_to_dates(rows: list, from_date: str, to_date: str) -> list:
    """Keep only rows whose date falls within [from_date, to_date] inclusive."""
    return [r for r in rows if from_date <= r["date"] <= to_date]


# ── Download modes ─────────────────────────────────────────────────────────────

def fetch_recent(ticker: str, days: int) -> tuple:
    """Fetch the most recent N trading days."""
    countback = int(days * 78 * 1.2)
    print(f"  Mode: last {days} trading days")
    rows = download_with_countback(ticker, countback)
    label = "recent"
    return rows, label


def fetch_date_range(ticker: str, from_date: str, to_date: str) -> tuple:
    """
    Fetch bars for a specific date window.
    Uses countback calculated from calendar distance, then trims.
    Adds a 1.4x buffer to account for weekends, holidays, and partial weeks.
    """
    print(f"  Mode: date range {from_date} → {to_date}")

    # How many calendar days back is the start date?
    cal_days_back = calendar_days_since(from_date)
    # Calendar days → approximate trading days (5/7 ratio), with buffer
    trading_days_est = int(cal_days_back * 5 / 7 * 1.4) + 10
    countback = int(trading_days_est * 78)

    print(f"  Calendar days back: {cal_days_back}  →  countback: {countback}")

    rows = download_with_countback(ticker, countback)

    # Trim to the requested window
    trimmed = trim_to_dates(rows, from_date, to_date)

    if not trimmed:
        print(f"  ERROR: No bars found in range {from_date} → {to_date}")
        print(f"  Got dates: {rows[0]['date']} → {rows[-1]['date']}" if rows else "  No bars at all.")
        sys.exit(1)

    label = f"{from_date}_{to_date}"
    return trimmed, label


# ── Save ───────────────────────────────────────────────────────────────────────

def save_to_csv(rows: list, ticker: str, output_dir: str, label: str) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    filename = f"{ticker.upper()}_5m_{label}.csv"
    filepath = os.path.join(output_dir, filename)

    fieldnames = ["timestamp", "open", "high", "low", "close", "volume", "date", "datetime_ct"]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    dates = sorted(set(r["date"] for r in rows))
    print(f"  Date range saved: {dates[0]} → {dates[-1]}  ({len(dates)} trading days)")
    print(f"  Saved {len(rows)} bars to: {filepath}")
    return filepath


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download historical 5m bars from MarketData.app")
    parser.add_argument("--ticker",   default="SPY", help="Ticker symbol (default: SPY)")
    parser.add_argument("--days",     default=22, type=int,
                        help="Trading days to fetch in recent mode (default: 22)")
    parser.add_argument("--from",     dest="from_date", default=None,
                        help="Start date YYYY-MM-DD — activates date-range mode")
    parser.add_argument("--to",       dest="to_date",   default=None,
                        help="End date YYYY-MM-DD (only used with --from, defaults to today)")
    parser.add_argument("--output",   default=None,
                        help="Output directory (default: backtest/data/)")
    args = parser.parse_args()

    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, "data")

    print(f"\n{'='*55}")
    print(f"  Downloading {args.ticker} historical 5m bars")
    print(f"{'='*55}")

    if args.from_date:
        to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")
        rows, label = fetch_date_range(args.ticker, args.from_date, to_date)
    else:
        rows, label = fetch_recent(args.ticker, args.days)

    path = save_to_csv(rows, args.ticker, args.output, label)

    print("\nFirst 3 bars:")
    for r in rows[:3]:
        print(f"  {r['datetime_ct']}  O={r['open']:.2f}  H={r['high']:.2f}  "
              f"L={r['low']:.2f}  C={r['close']:.2f}")

    print(f"\n✅ Done → {path}")
    print(f"   Total bars: {len(rows)}")
    print()
    print("To run the backtest on this data:")
    print(f"   python backtest/backtest_replay.py --data {path} --ticker {args.ticker}")


if __name__ == "__main__":
    main()
