# backtest/download_bars.py
# ─────────────────────────────────────────────────────────────────────────────
# Downloads historical 5-minute bars from MarketData.app and saves to CSV.
# Supports both "most recent N days" and "specific date range" modes.
#
# Called automatically by the GitHub Actions workflow.
#
# Examples:
#   python backtest/download_bars.py                        # last 22 trading days of SPY
#   python backtest/download_bars.py --from 2025-10-01 --to 2025-11-30
#   python backtest/download_bars.py --ticker QQQ --from 2025-06-01 --to 2025-07-31
#
# Output: backtest/data/{TICKER}_5m_{LABEL}.csv
#   e.g.  backtest/data/SPY_5m_recent.csv
#         backtest/data/SPY_5m_2025-10-01_2025-11-30.csv
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import argparse
import requests
from datetime import datetime, timezone
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
    """Convert epoch timestamp to Central Time string for human readability."""
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


# ── Download modes ─────────────────────────────────────────────────────────────

def download_by_countback(ticker: str, days: int) -> list:
    """
    Fetch the most recent N trading days using countback.
    78 bars/day * days * 1.2 buffer = safe countback.
    """
    countback = int(days * 78 * 1.2)
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker.upper()}/"
    print(f"  Mode: last {days} trading days  (countback={countback})")
    data = md_get(url, {"countback": countback})
    return _unpack(data, ticker)


def download_by_date_range(ticker: str, from_date: str, to_date: str) -> list:
    """
    Fetch bars between two dates (YYYY-MM-DD).
    MarketData accepts 'from' and 'to' as date strings.
    """
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker.upper()}/"
    print(f"  Mode: date range {from_date} → {to_date}")
    data = md_get(url, {"from": from_date, "to": to_date})
    return _unpack(data, ticker)


def _unpack(data: dict, ticker: str) -> list:
    """Convert MarketData columnar response into a list of row dicts."""
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

    print(f"  Got {n} bars from API")

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

    # Sort oldest → newest
    rows.sort(key=lambda r: r["timestamp"])

    dates = sorted(set(r["date"] for r in rows))
    print(f"  Date range in data: {dates[0]} → {dates[-1]}")
    print(f"  Trading days: {len(dates)}")
    return rows


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

    print(f"  Saved {len(rows)} bars to: {filepath}")
    return filepath


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download historical 5m bars from MarketData.app")
    parser.add_argument("--ticker",  default="SPY",    help="Ticker symbol (default: SPY)")
    parser.add_argument("--days",    default=22, type=int,
                        help="Trading days to fetch in countback mode (default: 22, ignored if --from/--to used)")
    parser.add_argument("--from",    dest="from_date", default=None,
                        help="Start date YYYY-MM-DD (activates date-range mode)")
    parser.add_argument("--to",      dest="to_date",   default=None,
                        help="End date YYYY-MM-DD (activates date-range mode, defaults to today)")
    parser.add_argument("--output",  default=None,
                        help="Output directory (default: backtest/data/)")
    args = parser.parse_args()

    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, "data")

    print(f"\n{'='*55}")
    print(f"  Downloading {args.ticker} historical 5m bars")
    print(f"{'='*55}")

    # Choose mode
    if args.from_date:
        to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")
        rows = download_by_date_range(args.ticker, args.from_date, to_date)
        label = f"{args.from_date}_{to_date}"
    else:
        rows = download_by_countback(args.ticker, args.days)
        label = "recent"

    path = save_to_csv(rows, args.ticker, args.output, label)

    # Preview
    print("\nFirst 3 bars:")
    for r in rows[:3]:
        print(f"  {r['datetime_ct']}  O={r['open']:.2f}  H={r['high']:.2f}  L={r['low']:.2f}  C={r['close']:.2f}")

    print(f"\n✅ Done → {path}")
    print(f"   Total bars: {len(rows)}")
    print()
    print("To run the backtest on this data:")
    print(f"   python backtest/backtest_replay.py --data {path} --ticker {args.ticker}")


if __name__ == "__main__":
    main()
