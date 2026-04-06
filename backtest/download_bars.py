# backtest/download_bars.py
# ─────────────────────────────────────────────────────────────────────────────
# Downloads historical 5-minute bars from MarketData.app and saves to CSV.
#
# Two modes:
#   Recent mode  (default): fetches last N trading days using countback.
#                            Works on all plan tiers.
#   Date range mode:        passes from/to directly to the API.
#                            Requires Trader plan or higher.
#
# Examples:
#   python backtest/download_bars.py --ticker AAPL --days 22
#   python backtest/download_bars.py --ticker AAPL --from 2026-02-01 --to 2026-02-14
#   python backtest/download_bars.py --ticker CAT  --from 2026-01-01
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import time
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Token ──────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: MARKETDATA_TOKEN environment variable is not set.")
    sys.exit(1)

# ── Retry settings ─────────────────────────────────────────────────────────────
MAX_RETRIES  = 4
BASE_BACKOFF = 15   # seconds, doubles each retry


# ── MarketData API ─────────────────────────────────────────────────────────────

def md_get(url: str, params: dict = None) -> dict:
    """GET with retry/back-off on 429 and 403."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    delay = BASE_BACKOFF

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers,
                                    params=params or {}, timeout=30)

            if response.status_code in (429, 403):
                wait = max(int(response.headers.get("Retry-After", delay)), delay)
                if attempt < MAX_RETRIES:
                    print(f"  [{response.status_code}] Waiting {wait}s "
                          f"(attempt {attempt}/{MAX_RETRIES})...")
                    time.sleep(wait)
                    delay = min(delay * 2, 120)
                    continue
                else:
                    print(f"\n  ERROR {response.status_code}: {response.text[:400]}")
                    if response.status_code == 403:
                        print("\n  HINT: 403 on a date-range request usually means the")
                        print("  from/to API parameters require Trader plan or higher.")
                        print("  Use --days mode for countback-based fetching instead.")
                    response.raise_for_status()

            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                print(f"  Timeout on attempt {attempt}. Retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                print("  ERROR: Request timed out after all retries.")
                sys.exit(1)

    sys.exit(1)


# ── Time helpers ───────────────────────────────────────────────────────────────

def to_ct_str(epoch: float) -> str:
    dt_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        dt_ct = dt_utc.astimezone(ZoneInfo("America/Chicago"))
    except ImportError:
        import pytz
        dt_ct = dt_utc.astimezone(pytz.timezone("America/Chicago"))
    return dt_ct.strftime("%Y-%m-%d %H:%M")


def to_date_ct(epoch: float) -> str:
    return to_ct_str(epoch)[:10]


# ── Parse API response into row dicts ─────────────────────────────────────────

def parse_candles(data: dict) -> list:
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


# ── Download modes ─────────────────────────────────────────────────────────────

def fetch_recent(ticker: str, days: int) -> tuple:
    """
    Fetch the most recent N trading days using countback.
    Works on all MarketData plan tiers.
    """
    countback = int(days * 78 * 1.05)
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker.upper()}/"

    print(f"  Mode: last {days} trading days (countback={countback})")
    print(f"  Fetching {ticker.upper()}...")

    data = md_get(url, {"countback": countback})
    rows = parse_candles(data)
    print(f"  Got {len(rows)} bars")
    return rows, "recent"


def fetch_date_range(ticker: str, from_date: str, to_date: str) -> tuple:
    """
    Fetch bars for an explicit date range using native from/to API parameters.
    Requires MarketData Trader plan or higher.
    """
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker.upper()}/"

    print(f"  Mode: date range {from_date} → {to_date}  (native from/to API)")
    print(f"  Fetching {ticker.upper()}...")

    # MarketData's to parameter is exclusive (returns data up to but NOT including
    # the to date), so bump it forward by one day to include the requested end date.
    from datetime import timedelta
    to_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
    to_exclusive = to_dt.strftime("%Y-%m-%d")

    params = {
        "from": from_date,
        "to":   to_exclusive,
    }
    print(f"  (to adjusted to {to_exclusive} so {to_date} is included)")

    data = md_get(url, params)
    rows = parse_candles(data)
    print(f"  Got {len(rows)} bars")

    if not rows:
        print(f"  ERROR: No bars returned for {ticker} in range {from_date} → {to_date}")
        print("  Check that the ticker is valid and the date range contains trading days.")
        sys.exit(1)

    label = f"{from_date}_{to_date}"
    return rows, label


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
    print(f"  Saved {len(rows)} bars → {filepath}")
    return filepath


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download historical 5m bars from MarketData.app"
    )
    parser.add_argument("--ticker", default="SPY",
                        help="Ticker symbol (default: SPY)")
    parser.add_argument("--days",   default=22, type=int,
                        help="Trading days to fetch in recent mode (default: 22)")
    parser.add_argument("--from",   dest="from_date", default=None,
                        help="Start date YYYY-MM-DD — activates date-range mode (Trader plan+)")
    parser.add_argument("--to",     dest="to_date",   default=None,
                        help="End date YYYY-MM-DD (defaults to today if --from is set)")
    parser.add_argument("--output", default=None,
                        help="Output directory (default: backtest/data/)")
    args = parser.parse_args()

    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, "data")

    print(f"\n{'='*55}")
    print(f"  Downloading {args.ticker.upper()} historical 5m bars")
    print(f"{'='*55}")

    if args.from_date:
        to_date = args.to_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
