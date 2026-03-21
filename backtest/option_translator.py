# backtest/option_translator.py
# ─────────────────────────────────────────────────────────────────────────────
# Options P&L layer for the SPY backtest.
#
# Strategy:
#   Naked calls (LONG trades) or puts (SHORT trades)
#   Strike: int(spot) + 2 for calls, int(spot) - 2 for puts (~$1-2 OTM)
#   0DTE for Morning/Midday/Afternoon, 1DTE for Power Hour/Close
#
# Pricing method:
#   The MarketData historical options API returns one daily quote per date,
#   not intraday bars. So we use two-point pricing:
#     Entry premium  = daily quote mid on entry date
#     Exit premium   = estimated using delta × underlying move
#                      (entry_premium + delta × underlying_pnl_pts)
#   This gives a realistic P&L without needing intraday option bars.
#
#   For 0DTE trades that hit their stop (loss), we cap exit at $0
#   (option expired worthless or near-worthless).
#
# Usage:
#   python backtest/option_translator.py
#   python backtest/option_translator.py --contracts 5
#   python backtest/option_translator.py --trades backtest/results/trades.csv
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import time
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path

TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: MARKETDATA_TOKEN not set.")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

ONE_DTE_SESSIONS = {"POWER_HOUR", "CLOSE"}
API_DELAY        = 0.4   # seconds between calls


# ── Strike & symbol helpers ───────────────────────────────────────────────────

def get_strike(spot: float, direction: str) -> int:
    base = int(spot)
    return base + 2 if direction == "LONG" else base - 2


def get_expiry(trade_date: str, time_phase: str) -> str:
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    if time_phase in ONE_DTE_SESSIONS:
        nxt = dt + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt.strftime("%Y-%m-%d")
    return trade_date


def build_occ(ticker: str, expiry: str, direction: str, strike: int) -> str:
    dt       = datetime.strptime(expiry, "%Y-%m-%d")
    date_str = dt.strftime("%y%m%d")
    cp       = "C" if direction == "LONG" else "P"
    return f"{ticker.upper()}{date_str}{cp}{int(strike * 1000):08d}"


# ── API ───────────────────────────────────────────────────────────────────────

def md_get(url: str, params: dict = None) -> dict | None:
    try:
        r = requests.get(url,
                         headers={"Authorization": f"Bearer {TOKEN}"},
                         params=params or {},
                         timeout=12)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def fetch_daily_quote(symbol: str, date: str) -> dict | None:
    """
    Fetch a single-day historical option quote.
    Returns dict with mid, iv, delta — or None if unavailable.
    """
    data = md_get(
        f"https://api.marketdata.app/v1/options/quotes/{symbol}/",
        {"date": date}
    )
    time.sleep(API_DELAY)

    if not data or data.get("s") != "ok":
        return None

    def first(key):
        vals = data.get(key, [])
        return vals[0] if vals else None

    mid = first("mid")
    bid = first("bid")
    ask = first("ask")

    if mid is None and bid is not None and ask is not None:
        mid = round((float(bid) + float(ask)) / 2, 4)

    if mid is None:
        return None

    return {
        "mid":   float(mid),
        "iv":    float(first("iv"))    if first("iv")    else None,
        "delta": float(first("delta")) if first("delta") else None,
    }


# ── P&L calculation ───────────────────────────────────────────────────────────

DEFAULT_DELTA = {
    "LONG":  0.35,   # ~$1-2 OTM call
    "SHORT": 0.35,   # ~$1-2 OTM put (absolute value)
}


def calc_option_pnl(
    entry_mid:    float,
    delta:        float,
    underlying_pnl_pts: float,
    direction:    str,
    dte:          int,
    close_reason: str,
) -> float:
    """
    Estimate option exit price and P&L.

    Method: delta approximation
        exit_mid ≈ entry_mid + delta × underlying_move

    For LONG calls:  underlying_move = +underlying_pnl_pts (price went up)
    For SHORT puts:  underlying_move = -underlying_pnl_pts (price went down)

    For 0DTE trades that hit a hard stop, we floor the exit price at $0.01
    (option expired nearly worthless).
    """
    # Underlying move in the option's favor
    if direction == "LONG":
        underlying_move = underlying_pnl_pts
    else:
        # put gains value as spot falls
        underlying_move = underlying_pnl_pts  # pnl_pts for SHORT = entry - close (positive = good)

    exit_mid = entry_mid + delta * underlying_move

    # Floor: option can't go below $0
    exit_mid = max(exit_mid, 0.0)

    # 0DTE stopped-out trades: option likely expired near worthless
    is_hard_stop = "Hard stop" in str(close_reason) or "breached" in str(close_reason)
    if dte == 0 and is_hard_stop:
        exit_mid = 0.05  # assume near-zero at expiry after stop

    return round(exit_mid - entry_mid, 4)


# ── Main translator ───────────────────────────────────────────────────────────

OPTION_FIELDS = [
    "option_symbol",
    "expiry",
    "strike",
    "dte",
    "entry_option_mid",
    "exit_option_mid",
    "option_pnl_per_contract",
    "option_pnl_dollars",
    "option_entry_iv",
    "option_entry_delta",
    "option_pricing_method",
]


def translate_row(row: dict, contracts: int, ticker: str) -> dict:
    empty = {f: "" for f in OPTION_FIELDS}

    direction   = row.get("direction", "")
    date        = row.get("date", "")
    time_phase  = row.get("time_phase", "")
    close_reason= row.get("close_reason", "")

    try:
        spot    = float(row["entry_price"])
        pnl_pts = float(row["pnl_pts"])
    except (TypeError, ValueError, KeyError):
        return empty

    if not direction or not date or not time_phase:
        return empty

    strike = get_strike(spot, direction)
    expiry = get_expiry(date, time_phase)
    symbol = build_occ(ticker, expiry, direction, strike)
    dte    = (datetime.strptime(expiry, "%Y-%m-%d") -
              datetime.strptime(date,   "%Y-%m-%d")).days

    # Fetch entry quote
    quote = fetch_daily_quote(symbol, date)

    if quote is None:
        result = dict(empty)
        result.update({
            "option_symbol":       symbol,
            "expiry":              expiry,
            "strike":              strike,
            "dte":                 dte,
            "option_pricing_method": "unavailable",
        })
        return result

    entry_mid = quote["mid"]
    delta     = abs(quote["delta"]) if quote.get("delta") else DEFAULT_DELTA[direction]
    iv        = quote.get("iv")

    # Calculate exit premium via delta approximation
    pnl_per   = calc_option_pnl(entry_mid, delta, pnl_pts,
                                 direction, dte, close_reason)
    exit_mid  = round(entry_mid + pnl_per, 4)
    pnl_usd   = round(pnl_per * 100 * contracts, 2)

    return {
        "option_symbol":           symbol,
        "expiry":                  expiry,
        "strike":                  strike,
        "dte":                     dte,
        "entry_option_mid":        round(entry_mid, 4),
        "exit_option_mid":         max(round(exit_mid, 4), 0.0),
        "option_pnl_per_contract": pnl_per,
        "option_pnl_dollars":      pnl_usd,
        "option_entry_iv":         round(iv, 4) if iv else "",
        "option_entry_delta":      round(delta, 4),
        "option_pricing_method":   "delta_approx",
    }


def run(trades_path: str, output_path: str, contracts: int, ticker: str):
    print(f"\n{'='*55}")
    print(f"  Options P&L Translator — {ticker}")
    print(f"{'='*55}")
    print(f"  Input:     {trades_path}")
    print(f"  Contracts: {contracts}  |  Multiplier: $100/contract")
    print()

    with open(trades_path, newline="", encoding="utf-8") as f:
        reader    = csv.DictReader(f)
        all_rows  = list(reader)
        base_cols = list(reader.fieldnames or [])

    real_rows = [
        r for r in all_rows
        if r.get("pnl_pts") not in ("", None)
        and not str(r.get("close_reason", "")).startswith("Combo gate")
    ]

    print(f"  Total rows:   {len(all_rows)}")
    print(f"  Real trades:  {len(real_rows)}")
    print()

    opt_by_id = {}
    priced_count = unavail_count = 0

    for i, row in enumerate(real_rows):
        date      = row.get("date", "?")
        direction = row.get("direction", "?")
        phase     = row.get("time_phase", "?")
        symbol    = build_occ(
            ticker,
            get_expiry(date, phase),
            direction,
            get_strike(float(row.get("entry_price", 0) or 0), direction),
        )
        print(f"  [{i+1:>3}/{len(real_rows)}] {date} {direction:<5} {phase:<12} {symbol}")

        opt = translate_row(row, contracts, ticker)
        opt_by_id[id(row)] = opt

        if opt.get("option_pricing_method") == "unavailable":
            unavail_count += 1
            print(f"            ⚠ unavailable")
        else:
            priced_count += 1
            print(f"            entry=${opt.get('entry_option_mid','?'):>6}  "
                  f"exit=${opt.get('exit_option_mid','?'):>6}  "
                  f"P&L=${opt.get('option_pnl_dollars','?'):>8}")

    # Write output
    all_fields = base_cols + [f for f in OPTION_FIELDS if f not in base_cols]
    Path(os.path.dirname(output_path)).mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            merged = dict(row)
            merged.update(opt_by_id.get(id(row), {f: "" for f in OPTION_FIELDS}))
            writer.writerow(merged)

    # Summary
    priced_rows = [r for r in real_rows
                   if opt_by_id.get(id(r), {}).get("option_pnl_dollars") not in ("", None)]
    print(f"\n{'─'*55}")
    print(f"  ✅ Priced: {priced_count}   ⚠ Unavailable: {unavail_count}")
    print(f"  📄 {output_path}")

    if priced_rows:
        pnls  = [float(opt_by_id[id(r)]["option_pnl_dollars"]) for r in priced_rows]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p < 0]
        total = sum(pnls)

        print(f"\n{'='*55}")
        print(f"  OPTIONS SUMMARY  ({contracts} contract{'s' if contracts>1 else ''} × $100)")
        print(f"{'='*55}")
        print(f"  Trades priced:  {len(pnls)}")
        print(f"  Win rate:       {len(wins)/len(pnls)*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Total P&L:      ${total:+,.2f}")
        print(f"  Avg / trade:    ${total/len(pnls):+,.2f}")
        if wins:   print(f"  Avg winner:     ${sum(wins)/len(wins):+,.2f}")
        if losses: print(f"  Avg loser:      ${sum(losses)/len(losses):+,.2f}")
        print(f"  Best:           ${max(pnls):+,.2f}")
        print(f"  Worst:          ${min(pnls):+,.2f}")
        print(f"{'='*55}")

    print("\n✅ Done.\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--trades",    default=None)
    p.add_argument("--output",    default=None)
    p.add_argument("--contracts", default=1, type=int)
    p.add_argument("--ticker",    default="SPY")
    a = p.parse_args()

    if a.trades is None:
        a.trades = os.path.join(SCRIPT_DIR, "results", "trades.csv")
    if a.output is None:
        a.output = os.path.join(SCRIPT_DIR, "results", "trades_with_options.csv")

    if not os.path.exists(a.trades):
        print(f"ERROR: {a.trades} not found. Run backtest_replay.py first.")
        sys.exit(1)

    run(a.trades, a.output, a.contracts, a.ticker)


if __name__ == "__main__":
    main()
