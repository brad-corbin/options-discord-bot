# backtest/option_translator.py
# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Options P&L layer for the backtest.
#
# Reads the trades.csv produced by backtest_replay.py and adds real options
# pricing from MarketData.app historical quotes API. Outputs a new CSV with
# actual dollar P&L per contract alongside all original trade data.
#
# Strategy modeled:
#   - SPY naked calls (LONG trades) or puts (SHORT trades)
#   - Strike: int(spot) ± 2  →  ~$1-2 OTM in trade direction
#     e.g. spot 683.23 LONG  → call at 685
#          spot 683.56 SHORT → put  at 681
#   - Expiration:
#       Sessions before Power Hour (MORNING/MIDDAY/AFTERNOON) → 0DTE
#       Power Hour / Close → 1DTE (next trading day)
#   - Entry: option mid price at the entry bar
#   - Exit:  option mid price at the exit bar
#
# Usage:
#   python backtest/option_translator.py
#   python backtest/option_translator.py --trades backtest/results/trades.csv
#   python backtest/option_translator.py --contracts 5
#
# Output:
#   backtest/results/trades_with_options.csv
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import time
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ── Token ─────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: MARKETDATA_TOKEN environment variable is not set.")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Config ─────────────────────────────────────────────────────────────────────
# Sessions that use 1DTE instead of 0DTE
ONE_DTE_SESSIONS = {"POWER_HOUR", "CLOSE"}

# Rate limiting — MarketData API has request limits
API_DELAY_SECONDS = 0.5   # pause between API calls


# ═════════════════════════════════════════════════════════════════════════════
# STRIKE & SYMBOL HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_strike(spot: float, direction: str) -> int:
    """
    Calculate option strike price.

    Formula: truncate spot to integer, then ±2 in trade direction.
    This produces ~$1-2 OTM strikes.

    Examples:
        spot 683.23, LONG  → int(683.23)=683 → 683+2 = 685 (call)
        spot 683.56, SHORT → int(683.56)=683 → 683-2 = 681 (put)
    """
    base = int(spot)
    return base + 2 if direction == "LONG" else base - 2


def get_expiration_date(trade_date: str, time_phase: str) -> str:
    """
    Determine option expiration date.

    0DTE: trade_date itself (sessions before Power Hour)
    1DTE: next trading day (Power Hour and Close sessions)

    Skips weekends when calculating next trading day.
    Note: does not account for market holidays — adjust manually if needed.
    """
    dt = datetime.strptime(trade_date, "%Y-%m-%d")

    if time_phase in ONE_DTE_SESSIONS:
        # Advance to next trading day
        next_day = dt + timedelta(days=1)
        while next_day.weekday() >= 5:  # 5=Sat, 6=Sun
            next_day += timedelta(days=1)
        return next_day.strftime("%Y-%m-%d")
    else:
        return trade_date


def build_occ_symbol(ticker: str, expiry: str, direction: str, strike: int) -> str:
    """
    Build OCC-standard option symbol.

    Format: {TICKER}{YYMMDD}{C|P}{8-digit strike (price × 1000, zero-padded)}

    Examples:
        SPY 0DTE call at 685 expiring 2026-03-20 → SPY260320C00685000
        SPY 0DTE put  at 681 expiring 2026-03-20 → SPY260320P00681000
        SPY 1DTE call at 687 expiring 2026-03-23 → SPY260323C00687000
    """
    dt = datetime.strptime(expiry, "%Y-%m-%d")
    date_str = dt.strftime("%y%m%d")
    cp = "C" if direction == "LONG" else "P"
    strike_str = f"{int(strike * 1000):08d}"
    return f"{ticker.upper()}{date_str}{cp}{strike_str}"


# ═════════════════════════════════════════════════════════════════════════════
# MARKETDATA API
# ═════════════════════════════════════════════════════════════════════════════

def md_get(url: str, params: dict = None) -> dict:
    """Make an authenticated GET request to MarketData.app."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = requests.get(url, headers=headers, params=params or {}, timeout=15)
    response.raise_for_status()
    return response.json()


def fetch_option_quote(symbol: str, trade_date: str) -> dict:
    """
    Fetch historical option quote for a specific date.

    Returns dict with keys: mid, bid, ask, iv, delta, gamma, theta, vega
    Returns None if the quote is unavailable (option didn't exist, no data, etc.)

    API endpoint: /v1/options/quotes/{symbol}/?date={YYYY-MM-DD}
    """
    url = f"https://api.marketdata.app/v1/options/quotes/{symbol}/"
    try:
        data = md_get(url, {"date": trade_date})

        if not isinstance(data, dict) or data.get("s") != "ok":
            return None

        # Response is columnar: mid=[...], bid=[...], etc.
        # We want the first (and usually only) data point for the date
        mid  = data.get("mid",   [None])[0]
        bid  = data.get("bid",   [None])[0]
        ask  = data.get("ask",   [None])[0]
        iv   = data.get("iv",    [None])[0]
        delta= data.get("delta", [None])[0]

        if mid is None and bid is not None and ask is not None:
            mid = round((bid + ask) / 2, 4)

        return {
            "mid":   mid,
            "bid":   bid,
            "ask":   ask,
            "iv":    iv,
            "delta": delta,
        }

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return None  # Option didn't exist or no data for that date
        raise
    except Exception:
        return None


def fetch_option_candles(symbol: str, trade_date: str, resolution: int = 5) -> list:
    """
    Fetch intraday option candles for a specific date (if available on plan).
    Returns list of {timestamp, open, high, low, close, volume} dicts, or [].

    This gives better entry/exit precision than daily quotes.
    Falls back gracefully if candles are not available.
    """
    url = f"https://api.marketdata.app/v1/options/candles/{resolution}/{symbol}/"
    try:
        data = md_get(url, {"date": trade_date})
        if not isinstance(data, dict) or data.get("s") != "ok":
            return []

        timestamps = data.get("t", [])
        closes     = data.get("c", [])
        opens      = data.get("o", [])

        candles = []
        for i in range(len(timestamps)):
            candles.append({
                "timestamp": timestamps[i],
                "close":     float(closes[i]) if i < len(closes) else None,
                "open":      float(opens[i])  if i < len(opens)  else None,
            })
        return candles

    except Exception:
        return []


def get_option_price_at_time(symbol: str, trade_date: str, bar_time_str: str,
                              fallback_daily: bool = True) -> float | None:
    """
    Get option mid price as close as possible to bar_time_str.

    Strategy:
    1. Try intraday candles → find the candle closest to bar_time_str
    2. Fall back to daily quote (mid) if candles unavailable

    bar_time_str: "YYYY-MM-DD HH:MM" in Central Time
    """
    # Try intraday candles first
    candles = fetch_option_candles(symbol, trade_date)
    if candles:
        # Parse target time as epoch
        try:
            from zoneinfo import ZoneInfo
            target_dt = datetime.strptime(bar_time_str, "%Y-%m-%d %H:%M").replace(
                tzinfo=ZoneInfo("America/Chicago"))
        except ImportError:
            import pytz
            target_dt = pytz.timezone("America/Chicago").localize(
                datetime.strptime(bar_time_str, "%Y-%m-%d %H:%M"))

        target_epoch = target_dt.timestamp()
        best_candle = min(candles, key=lambda c: abs(c["timestamp"] - target_epoch))
        price = best_candle.get("close") or best_candle.get("open")
        if price:
            return float(price)

    # Fall back to daily quote
    if fallback_daily:
        quote = fetch_option_quote(symbol, trade_date)
        if quote and quote.get("mid"):
            return float(quote["mid"])

    return None


# ═════════════════════════════════════════════════════════════════════════════
# MAIN TRANSLATOR
# ═════════════════════════════════════════════════════════════════════════════

OPTION_FIELDS = [
    "option_symbol",
    "expiry",
    "strike",
    "dte",
    "entry_option_price",
    "exit_option_price",
    "option_pnl_per_contract",
    "option_pnl_dollars",       # per_contract × 100 (standard lot)
    "option_entry_iv",
    "option_entry_delta",
    "option_pricing_method",    # "candle", "daily_quote", or "unavailable"
]


def translate_trade(row: dict, contracts: int, ticker: str) -> dict:
    """
    Process one trade row and add options data.

    Returns a dict of option fields to merge into the row.
    All fields are empty strings if pricing is unavailable.
    """
    empty = {f: "" for f in OPTION_FIELDS}

    # Skip trades without complete data
    entry_price = row.get("entry_price")
    close_price = row.get("close_price")
    direction   = row.get("direction", "")
    date        = row.get("date", "")
    time_phase  = row.get("time_phase", "")
    entry_time  = row.get("entry_bar_time", "")
    exit_time   = row.get("close_bar_time", "")

    if not all([entry_price, close_price, direction, date, time_phase]):
        return empty

    try:
        spot = float(entry_price)
    except (ValueError, TypeError):
        return empty

    # Build option parameters
    strike = get_strike(spot, direction)
    expiry = get_expiration_date(date, time_phase)
    symbol = build_occ_symbol(ticker, expiry, direction, strike)

    # Calculate DTE
    trade_dt  = datetime.strptime(date, "%Y-%m-%d")
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    dte = (expiry_dt - trade_dt).days

    result = {
        "option_symbol": symbol,
        "expiry":        expiry,
        "strike":        strike,
        "dte":           dte,
    }

    # Fetch entry option price
    print(f"    {symbol} — fetching entry price ({entry_time or date})...")
    entry_op = get_option_price_at_time(symbol, date, entry_time) if entry_time else None
    time.sleep(API_DELAY_SECONDS)

    if entry_op is None:
        print(f"    ⚠ Entry price unavailable for {symbol}")
        result.update({f: "" for f in OPTION_FIELDS if f not in result})
        result["option_pricing_method"] = "unavailable"
        return result

    # Fetch exit option price (may be different date for multi-day holds)
    exit_date = exit_time[:10] if exit_time else date
    print(f"    {symbol} — fetching exit price ({exit_time or exit_date})...")
    exit_op = get_option_price_at_time(symbol, exit_date, exit_time) if exit_time else None
    time.sleep(API_DELAY_SECONDS)

    if exit_op is None:
        # For 0DTE options that expired, use $0 as exit price
        if dte == 0:
            exit_op = 0.00
            print(f"    ℹ 0DTE expired worthless → exit price $0.00")
        else:
            print(f"    ⚠ Exit price unavailable for {symbol}")
            result["option_pricing_method"] = "unavailable"
            result.update({k: "" for k in ["entry_option_price","exit_option_price",
                                            "option_pnl_per_contract","option_pnl_dollars",
                                            "option_entry_iv","option_entry_delta"]})
            return result

    # P&L calculation
    # For calls (LONG): profit when price rises → exit_op - entry_op
    # For puts (SHORT): profit when price falls → exit_op - entry_op
    # (put value increases as spot falls, so same formula works)
    pnl_per_contract = round(exit_op - entry_op, 4)
    pnl_dollars      = round(pnl_per_contract * 100 * contracts, 2)

    # Fetch greeks at entry (optional, best effort)
    entry_quote = fetch_option_quote(symbol, date)
    time.sleep(API_DELAY_SECONDS)

    result.update({
        "entry_option_price":      round(entry_op, 4),
        "exit_option_price":       round(exit_op, 4)  if exit_op else "",
        "option_pnl_per_contract": pnl_per_contract,
        "option_pnl_dollars":      pnl_dollars,
        "option_entry_iv":         round(entry_quote["iv"], 4)    if entry_quote and entry_quote.get("iv")    else "",
        "option_entry_delta":      round(entry_quote["delta"], 4) if entry_quote and entry_quote.get("delta") else "",
        "option_pricing_method":   "candle" if entry_time else "daily_quote",
    })

    return result


def run_translator(trades_path: str, output_path: str,
                   contracts: int, ticker: str) -> None:
    print(f"\n{'='*55}")
    print(f"  Options P&L Translator — {ticker}")
    print(f"{'='*55}\n")
    print(f"  Input:     {trades_path}")
    print(f"  Output:    {output_path}")
    print(f"  Ticker:    {ticker}")
    print(f"  Contracts: {contracts}")
    print()

    # Load trades
    with open(trades_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        trades = list(reader)
        base_fields = reader.fieldnames or []

    # Filter to real trades only (skip combo gate rejections)
    real_trades = [
        t for t in trades
        if t.get("pnl_pts") not in ("", None)
        and not str(t.get("close_reason", "")).startswith("Combo gate")
    ]
    all_trades = trades  # keep all for output

    print(f"  Total rows: {len(trades)}")
    print(f"  Real trades to price: {len(real_trades)}\n")

    # Build lookup: trade_id → option data
    option_data = {}
    success = 0
    unavailable = 0

    for i, row in enumerate(real_trades):
        trade_id = row.get("_trade_id") or f"row_{i}"
        date     = row.get("date", "?")
        direction= row.get("direction", "?")
        phase    = row.get("time_phase", "?")

        print(f"  [{i+1}/{len(real_trades)}] {date} {direction} {phase}")

        opt = translate_trade(row, contracts, ticker)
        option_data[id(row)] = opt   # use object id as key

        if opt.get("option_pricing_method") == "unavailable":
            unavailable += 1
        elif opt.get("entry_option_price") != "":
            success += 1

    print(f"\n  ✅ Priced: {success}  ⚠ Unavailable: {unavailable}")

    # Write output — merge option fields into all trade rows
    all_fields = base_fields + [f for f in OPTION_FIELDS if f not in base_fields]
    Path(os.path.dirname(output_path)).mkdir(parents=True, exist_ok=True)

    # Build a map from real_trades rows to option data
    real_trade_set = {id(r): r for r in real_trades}
    opt_by_id = {}
    for i, row in enumerate(real_trades):
        opt_by_id[id(row)] = option_data.get(id(row), {f: "" for f in OPTION_FIELDS})

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        for row in all_trades:
            merged = dict(row)
            opt = opt_by_id.get(id(row), {f: "" for f in OPTION_FIELDS})
            merged.update(opt)
            writer.writerow(merged)

    print(f"\n  📄 trades_with_options.csv → {output_path}")

    # Print summary
    priced = [r for r in real_trades if opt_by_id.get(id(r), {}).get("option_pnl_dollars") not in ("", None)]
    if priced:
        total_usd = sum(float(opt_by_id[id(r)]["option_pnl_dollars"]) for r in priced)
        wins  = [r for r in priced if float(opt_by_id[id(r)]["option_pnl_dollars"]) > 0]
        losses= [r for r in priced if float(opt_by_id[id(r)]["option_pnl_dollars"]) < 0]
        print(f"\n{'='*55}")
        print(f"  OPTIONS P&L SUMMARY ({contracts} contract{'s' if contracts>1 else ''})")
        print(f"{'='*55}")
        print(f"  Trades priced:    {len(priced)}")
        print(f"  Win rate:         {len(wins)/len(priced)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Total P&L:        ${total_usd:+,.2f}")
        print(f"  Avg per trade:    ${total_usd/len(priced):+,.2f}")
        if wins:
            avg_w = sum(float(opt_by_id[id(r)]["option_pnl_dollars"]) for r in wins) / len(wins)
            print(f"  Avg winner:       ${avg_w:+,.2f}")
        if losses:
            avg_l = sum(float(opt_by_id[id(r)]["option_pnl_dollars"]) for r in losses) / len(losses)
            print(f"  Avg loser:        ${avg_l:+,.2f}")
        print(f"{'='*55}")

    print(f"\n✅ Done.")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Add real options P&L to backtest trades.csv")
    parser.add_argument("--trades", default=None,
                        help="Path to trades.csv (default: backtest/results/trades.csv)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path (default: backtest/results/trades_with_options.csv)")
    parser.add_argument("--contracts", default=1, type=int,
                        help="Number of option contracts per trade (default: 1)")
    parser.add_argument("--ticker", default="SPY",
                        help="Ticker symbol (default: SPY)")
    args = parser.parse_args()

    if args.trades is None:
        args.trades = os.path.join(SCRIPT_DIR, "results", "trades.csv")
    if args.output is None:
        args.output = os.path.join(SCRIPT_DIR, "results", "trades_with_options.csv")

    if not os.path.exists(args.trades):
        print(f"ERROR: trades.csv not found at {args.trades}")
        print("Run backtest_replay.py first.")
        sys.exit(1)

    run_translator(args.trades, args.output, args.contracts, args.ticker)


if __name__ == "__main__":
    main()
