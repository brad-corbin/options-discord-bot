# backtest/swing_backtest.py
# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtest for the swing scanner.
#
# How it works:
#   - Walks daily bars one at a time, never peeking ahead.
#   - At each bar, calls analyze_swing_setup() with all bars seen so far.
#   - When a signal fires, the trade enters at the NEXT bar's open (realistic).
#   - Positions carry overnight. No EOD force-close.
#   - Exits: stop loss, target 1 (1.272 ext), target 2 (1.618 ext), max-hold.
#   - Cooldown: won't re-enter the same direction within COOLDOWN_BARS days.
#
# Stop placement:
#   - Bull: just below the swing low identified at signal time (swing_low - 0.2*ATR)
#   - Bear: just above the swing high (swing_high + 0.2*ATR)
#
# Exit order within a bar (uses high/low to detect):
#   Stop is checked before targets (conservative — assumes worst first).
#   On the same bar, if both stop and target are hit, stop wins.
#
# Output (backtest/results/):
#   swing_trades.csv   — one row per closed trade
#   swing_summary.txt  — printed stats
#
# Usage:
#   python backtest/swing_backtest.py \
#     --ticker AAPL \
#     --data backtest/data/AAPL_daily_2024-06-01_2026-04-04.csv \
#     --spy  backtest/data/SPY_daily_2024-06-01_2026-04-04.csv \
#     --backtest-from 2025-07-14
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import csv
import argparse
from datetime import datetime, date
from pathlib import Path
from typing import Optional

# ── Repo root on path so we can import swing_scanner + trading_rules ──────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

try:
    from swing_scanner import analyze_swing_setup
except ImportError as e:
    print(f"\nERROR: Could not import swing_scanner: {e}")
    print("Make sure you're running from the repo root or backtest/ directory.")
    sys.exit(1)

# ── Defaults (overridable via CLI) ────────────────────────────────────────────
DEFAULT_MAX_HOLD_DAYS = 15    # close at next open after this many days
DEFAULT_COOLDOWN_DAYS = 3     # no re-entry in same direction within N days
DEFAULT_STOP_ATR_MULT = 0.2   # how far beyond swing_low/high to set stop
DEFAULT_MIN_BARS      = 80    # minimum bars before scanner is allowed to fire


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD BARS
# ═══════════════════════════════════════════════════════════════════════════════

def load_bars(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "timestamp": int(r["timestamp"]),
                "date":      r["date"],
                "o":  float(r["o"]),
                "h":  float(r["h"]),
                "l":  float(r["l"]),
                "c":  float(r["c"]),
                "v":  int(r["v"]),
            })
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def align_spy(ticker_bars: list, spy_bars: list) -> list:
    """Return spy_bars aligned to ticker dates (same index = same date)."""
    spy_by_date = {b["date"]: b for b in spy_bars}
    aligned = []
    for tb in ticker_bars:
        sb = spy_by_date.get(tb["date"])
        aligned.append(sb)   # may be None if SPY missing that date
    return aligned


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE RECORD
# ═══════════════════════════════════════════════════════════════════════════════

class SwingTrade:
    def __init__(self, signal: dict, entry_date: str, entry_price: float):
        self.ticker       = signal["ticker"]
        self.direction    = signal["direction"]  # 'bull' or 'bear'
        self.tier         = signal["tier"]
        self.fib_level    = signal["fib_level"]
        self.fib_price    = signal["fib_price"]
        self.swing_high   = signal["swing_high"]
        self.swing_low    = signal["swing_low"]
        self.target1      = signal["fib_target_1"]
        self.target2      = signal["fib_target_2"]
        self.atr          = signal["atr"]
        self.confidence   = signal["confidence"]
        self.rs_vs_spy    = signal["rs_vs_spy"]
        self.primary_trend = signal.get("primary_trend", "")
        self.rsi          = signal["rsi"]
        self.weekly_bull  = signal["weekly_bull"]
        self.weekly_bear  = signal["weekly_bear"]
        self.vol_contracting = signal["vol_contracting"]
        self.touch_count  = signal["touch_count"]
        self.signal_warnings = "; ".join(signal.get("warnings", []))

        self.entry_date   = entry_date
        self.entry_price  = entry_price

        # Stop: structural level is the swing extreme, but capped at 2x ATR
        # from entry so we never risk more than ~2x ATR regardless of how wide
        # the swing range is. Without this cap, wide swings produce 0.5:1 R:R.
        ATR_STOP_CAP = 2.0
        if self.direction == "bull":
            swing_dist = self.entry_price - (self.swing_low - self.atr * DEFAULT_STOP_ATR_MULT)
            max_dist   = self.atr * ATR_STOP_CAP
            self.stop  = self.entry_price - min(swing_dist, max_dist)
        else:
            swing_dist = (self.swing_high + self.atr * DEFAULT_STOP_ATR_MULT) - self.entry_price
            max_dist   = self.atr * ATR_STOP_CAP
            self.stop  = self.entry_price + min(swing_dist, max_dist)

        # State
        self.close_date   = None
        self.close_price  = None
        self.close_reason = None
        self.hold_days    = 0
        self.mae          = 0.0   # max adverse excursion (pts)
        self.mfe          = 0.0   # max favourable excursion (pts)

    def update_excursion(self, bar: dict):
        """Track MAE/MFE as trade progresses."""
        if self.direction == "bull":
            adverse   = self.entry_price - bar["l"]
            favorable = bar["h"] - self.entry_price
        else:
            adverse   = bar["h"] - self.entry_price
            favorable = self.entry_price - bar["l"]
        self.mae = min(self.mae, -adverse)  # stored as negative
        self.mfe = max(self.mfe,  favorable)

    def pnl(self) -> float:
        if self.close_price is None:
            return 0.0
        if self.direction == "bull":
            return self.close_price - self.entry_price
        else:
            return self.entry_price - self.close_price

    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return self.pnl() / self.entry_price * 100

    def to_dict(self) -> dict:
        return {
            "ticker":          self.ticker,
            "direction":       self.direction,
            "tier":            self.tier,
            "fib_level":       self.fib_level,
            "fib_price":       round(self.fib_price, 2),
            "swing_high":      round(self.swing_high, 2),
            "swing_low":       round(self.swing_low, 2),
            "stop":            round(self.stop, 2),
            "target1":         round(self.target1, 2),
            "target2":         round(self.target2, 2),
            "entry_date":      self.entry_date,
            "entry_price":     round(self.entry_price, 2),
            "close_date":      self.close_date or "",
            "close_price":     round(self.close_price, 2) if self.close_price else "",
            "close_reason":    self.close_reason or "",
            "hold_days":       self.hold_days,
            "pnl_pts":         round(self.pnl(), 4),
            "pnl_pct":         round(self.pnl_pct(), 4),
            "mae_pts":         round(self.mae, 4),
            "mfe_pts":         round(self.mfe, 4),
            "atr":             round(self.atr, 2),
            "confidence":      self.confidence,
            "rs_vs_spy":       round(self.rs_vs_spy, 2),
            "primary_trend":   self.primary_trend,
            "rsi":             round(self.rsi, 1),
            "weekly_bull":     self.weekly_bull,
            "weekly_bear":     self.weekly_bear,
            "vol_contracting": self.vol_contracting,
            "touch_count":     self.touch_count,
            "warnings":        self.signal_warnings,
        }


TRADE_FIELDS = [
    "ticker", "direction", "tier", "fib_level", "fib_price",
    "swing_high", "swing_low", "stop", "target1", "target2",
    "entry_date", "entry_price", "close_date", "close_price", "close_reason",
    "hold_days", "pnl_pts", "pnl_pct", "mae_pts", "mfe_pts",
    "atr", "confidence", "rs_vs_spy", "primary_trend", "rsi",
    "weekly_bull", "weekly_bear", "vol_contracting", "touch_count", "warnings",
]


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def run_backtest(
    ticker: str,
    ticker_bars: list,
    aligned_spy: list,
    backtest_from: str,
    max_hold_days: int,
    cooldown_days: int,
    output_dir: str,
):
    closed_trades = []
    open_trade: Optional[SwingTrade] = None
    pending_signal: Optional[dict]   = None   # signal fires → enters next bar
    last_signal_dir: Optional[str]   = None
    last_signal_date: Optional[date] = None
    signals_fired   = 0
    signals_skipped = 0

    bt_start = datetime.strptime(backtest_from, "%Y-%m-%d").date()

    print(f"\n{'─'*55}")
    print(f"  Walk-forward: {ticker}  |  backtest start: {backtest_from}")
    print(f"  Max hold: {max_hold_days}d  |  Cooldown: {cooldown_days}d")
    print(f"{'─'*55}")

    for i, bar in enumerate(ticker_bars):
        bar_date = datetime.strptime(bar["date"], "%Y-%m-%d").date()

        # ── Only start signalling / managing once past backtest start ─────────
        in_backtest = bar_date >= bt_start

        # ── Build SPY slice (all SPY bars up to and including today) ──────────
        spy_slice = [aligned_spy[j] for j in range(i + 1)
                     if aligned_spy[j] is not None]

        # ── Manage open trade ─────────────────────────────────────────────────
        if open_trade is not None and in_backtest:
            open_trade.hold_days += 1
            open_trade.update_excursion(bar)

            stop     = open_trade.stop
            target1  = open_trade.target1
            target2  = open_trade.target2
            closed   = False

            if open_trade.direction == "bull":
                # Stop hit?
                if bar["l"] <= stop:
                    open_trade.close_price  = stop
                    open_trade.close_date   = bar["date"]
                    open_trade.close_reason = "stop"
                    closed = True
                # Target 2 (full exit at extension)?
                elif bar["h"] >= target2:
                    open_trade.close_price  = target2
                    open_trade.close_date   = bar["date"]
                    open_trade.close_reason = "target2"
                    closed = True
                # Target 1 (half exit — recorded as single close for simplicity)?
                elif bar["h"] >= target1:
                    open_trade.close_price  = target1
                    open_trade.close_date   = bar["date"]
                    open_trade.close_reason = "target1"
                    closed = True
            else:  # bear
                if bar["h"] >= stop:
                    open_trade.close_price  = stop
                    open_trade.close_date   = bar["date"]
                    open_trade.close_reason = "stop"
                    closed = True
                elif bar["l"] <= target2:
                    open_trade.close_price  = target2
                    open_trade.close_date   = bar["date"]
                    open_trade.close_reason = "target2"
                    closed = True
                elif bar["l"] <= target1:
                    open_trade.close_price  = target1
                    open_trade.close_date   = bar["date"]
                    open_trade.close_reason = "target1"
                    closed = True

            # Max-hold expiry (close at next bar — approximated as this bar's close)
            if not closed and open_trade.hold_days >= max_hold_days:
                open_trade.close_price  = bar["c"]
                open_trade.close_date   = bar["date"]
                open_trade.close_reason = "max_hold"
                closed = True

            if closed:
                closed_trades.append(open_trade)
                pnl = open_trade.pnl()
                result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
                print(f"  CLOSE {open_trade.direction.upper():4s} {open_trade.close_date}"
                      f"  {open_trade.close_reason:<10}  {pnl:+.2f} pts  [{result}]"
                      f"  hold={open_trade.hold_days}d")
                last_signal_dir  = open_trade.direction
                last_signal_date = datetime.strptime(open_trade.close_date, "%Y-%m-%d").date()
                open_trade = None

        # ── Enter pending signal (signal fired yesterday, enter today's open) ─
        if pending_signal is not None and in_backtest and open_trade is None:
            entry_price = bar["o"]
            trade = SwingTrade(pending_signal, bar["date"], entry_price)
            open_trade = trade
            print(f"  ENTER {trade.direction.upper():4s} {bar['date']}"
                  f"  @ {entry_price:.2f}"
                  f"  stop={trade.stop:.2f}"
                  f"  t1={trade.target1:.2f}"
                  f"  t2={trade.target2:.2f}"
                  f"  conf={trade.confidence}  fib={trade.fib_level}%"
                  f"  T{trade.tier}")
            pending_signal = None

        # ── Check for new signal (need enough bars, and no open position) ─────
        if (i >= DEFAULT_MIN_BARS
                and in_backtest
                and open_trade is None
                and pending_signal is None):

            ticker_slice = ticker_bars[:i + 1]

            signal = analyze_swing_setup(
                ticker=ticker,
                daily_bars=ticker_slice,
                spy_bars=spy_slice if spy_slice else None,
            )

            if signal:
                # Cooldown check
                skip = False
                if (last_signal_dir == signal["direction"]
                        and last_signal_date is not None):
                    days_since = (bar_date - last_signal_date).days
                    if days_since < cooldown_days:
                        skip = True
                        signals_skipped += 1

                if not skip:
                    signals_fired += 1
                    pending_signal = signal
                    dir_label = signal["direction"].upper()
                    print(f"  SIGNAL {dir_label:4s} {bar['date']}"
                          f"  fib={signal['fib_level']}%"
                          f"  T{signal['tier']}"
                          f"  conf={signal['confidence']}"
                          f"  RS={signal['rs_vs_spy']:+.1f}%"
                          f"  → enter tomorrow's open")

    # ── Force-close any trade still open at end of data ──────────────────────
    if open_trade is not None:
        last_bar = ticker_bars[-1]
        open_trade.close_price  = last_bar["c"]
        open_trade.close_date   = last_bar["date"]
        open_trade.close_reason = "data_end"
        open_trade.update_excursion(last_bar)
        closed_trades.append(open_trade)
        print(f"  CLOSE {open_trade.direction.upper():4s} {open_trade.close_date}"
              f"  data_end  {open_trade.pnl():+.2f} pts")

    print(f"\n  Signals fired: {signals_fired}  |  Skipped (cooldown): {signals_skipped}")
    print(f"  Closed trades: {len(closed_trades)}")

    # ── Write trades CSV ──────────────────────────────────────────────────────
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    trades_path = os.path.join(output_dir, "swing_trades.csv")
    with open(trades_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        for t in closed_trades:
            writer.writerow(t.to_dict())

    # ── Summary ───────────────────────────────────────────────────────────────
    write_summary(closed_trades, ticker, output_dir, backtest_from, max_hold_days)

    return closed_trades


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

def write_summary(trades: list, ticker: str, output_dir: str,
                  backtest_from: str, max_hold_days: int):
    if not trades:
        print("\n  No closed trades — nothing to summarise.")
        return

    wins   = [t for t in trades if t.pnl() > 0]
    losses = [t for t in trades if t.pnl() < 0]
    pnls   = [t.pnl() for t in trades]
    total  = sum(pnls)
    wr     = len(wins) / len(trades) * 100 if trades else 0

    gross_win  = sum(t.pnl() for t in wins)
    gross_loss = abs(sum(t.pnl() for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    avg_hold   = sum(t.hold_days for t in trades) / len(trades)
    avg_win    = sum(t.pnl() for t in wins)   / len(wins)   if wins   else 0
    avg_loss   = sum(t.pnl() for t in losses) / len(losses) if losses else 0

    def group_stat(key_fn):
        groups = {}
        for t in trades:
            k = key_fn(t)
            if k not in groups:
                groups[k] = []
            groups[k].append(t.pnl())
        return {k: (len(v), sum(v), sum(1 for x in v if x > 0) / len(v) * 100)
                for k, v in groups.items()}

    lines = []
    lines.append("=" * 60)
    lines.append(f"  SWING BACKTEST SUMMARY — {ticker}")
    lines.append(f"  Backtest from: {backtest_from}  |  Max hold: {max_hold_days}d")
    lines.append("=" * 60)
    lines.append(f"  Total trades:    {len(trades)}")
    lines.append(f"  Win rate:        {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    lines.append(f"  Total P&L:       {total:+.2f} pts")
    lines.append(f"  Avg trade:       {total/len(trades):+.3f} pts")
    lines.append(f"  Avg winner:      {avg_win:+.3f} pts")
    lines.append(f"  Avg loser:       {avg_loss:+.3f} pts")
    lines.append(f"  Profit factor:   {pf:.2f}" if pf != float("inf") else "  Profit factor:   inf")
    lines.append(f"  Best:            {max(pnls):+.3f} pts")
    lines.append(f"  Worst:           {min(pnls):+.3f} pts")
    lines.append(f"  Avg hold:        {avg_hold:.1f} days")
    lines.append("")

    for label, key_fn in [
        ("By direction", lambda t: t.direction.upper()),
        ("By tier",      lambda t: f"T{t.tier}"),
        ("By fib level", lambda t: f"Fib {t.fib_level}%"),
        ("By close reason", lambda t: t.close_reason),
    ]:
        lines.append(f"  {label}:")
        for k, (n, pnl, wr_) in sorted(group_stat(key_fn).items(),
                                         key=lambda x: -x[1][1]):
            lines.append(f"    {k:<20}: {n:>3} trades  {wr_:>5.1f}% win  {pnl:+.3f} pts")
        lines.append("")

    # Hold duration buckets
    hold_buckets = {"1d": 0, "2-3d": 0, "4-5d": 0, "6-10d": 0, "max_hold": 0}
    for t in trades:
        if t.hold_days == 1:             hold_buckets["1d"]       += 1
        elif t.hold_days <= 3:           hold_buckets["2-3d"]     += 1
        elif t.hold_days <= 5:           hold_buckets["4-5d"]     += 1
        elif t.close_reason == "max_hold": hold_buckets["max_hold"] += 1
        else:                            hold_buckets["6-10d"]    += 1

    lines.append("  Hold duration:")
    for k, n in hold_buckets.items():
        lines.append(f"    {k:<8}: {n}")
    lines.append("")
    lines.append("=" * 60)

    summary_text = "\n".join(lines)
    print("\n" + summary_text)

    summary_path = os.path.join(output_dir, "swing_summary.txt")
    Path(summary_path).write_text(summary_text + "\n", encoding="utf-8")
    print(f"\n  Results → {output_dir}/")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Walk-forward swing backtest")
    parser.add_argument("--ticker",         required=True)
    parser.add_argument("--data",           required=True,
                        help="Path to ticker daily CSV (from download_daily_bars.py)")
    parser.add_argument("--spy",            required=True,
                        help="Path to SPY daily CSV")
    parser.add_argument("--backtest-from",  dest="backtest_from", required=True,
                        help="Date to start recording trades YYYY-MM-DD (bars before this warm up indicators)")
    parser.add_argument("--max-hold",       dest="max_hold", default=DEFAULT_MAX_HOLD_DAYS,
                        type=int, help=f"Max days to hold (default: {DEFAULT_MAX_HOLD_DAYS})")
    parser.add_argument("--cooldown",       default=DEFAULT_COOLDOWN_DAYS,
                        type=int, help=f"Cooldown days between same-direction signals (default: {DEFAULT_COOLDOWN_DAYS})")
    parser.add_argument("--output",         default=None,
                        help="Output directory (default: backtest/results/)")
    args = parser.parse_args()

    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, "results")

    ticker_bars = load_bars(args.data)
    spy_bars    = load_bars(args.spy)

    print(f"\n  Loaded {len(ticker_bars)} {args.ticker.upper()} daily bars")
    print(f"  Loaded {len(spy_bars)} SPY daily bars")
    print(f"  Date range: {ticker_bars[0]['date']} → {ticker_bars[-1]['date']}")

    aligned_spy = align_spy(ticker_bars, spy_bars)

    run_backtest(
        ticker       = args.ticker.upper(),
        ticker_bars  = ticker_bars,
        aligned_spy  = aligned_spy,
        backtest_from = args.backtest_from,
        max_hold_days = args.max_hold,
        cooldown_days = args.cooldown,
        output_dir   = args.output,
    )

    print("\n✅ Swing backtest complete.")


if __name__ == "__main__":
    main()
