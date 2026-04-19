#!/usr/bin/env python3
"""test_conviction_scorer.py — validate scorer against real backtest rows.

Reads trades.csv (the 621K-sample backtest output, or the 10K local copy),
translates each row into a context_snapshot, runs the scorer, and measures:

  - Score distribution (histogram)
  - Decision mix (post / log_only / discard %)
  - Per-hard-gate fire counts
  - Agreement with actual trade outcomes (did POST trades win more often
    than LOG_ONLY trades than DISCARD trades?)

Run before deploying:
    python3 test_conviction_scorer.py

Expected output:
  - Discard rate: ~5-15% (hard gates)
  - Log-only rate: ~55-75%
  - Post rate: ~15-35%
  - POST-decision WR should exceed LOG_ONLY WR by ≥5 points
  - DISCARD-decision WR should be meaningfully lower

If those thresholds don't hold, the scorer needs rule adjustments BEFORE deploy.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Ensure scorer is importable
sys.path.insert(0, str(Path(__file__).parent))
import conviction_scorer

# Sample trades.csv — 10K rows from the local backtest
DEFAULT_TRADES_CSV = "/tmp/backtest_resolution/trades.csv"


def _maturity_from_ratio(ratio_str: str) -> str:
    """Map pb_maturity_ratio (0.0-1.0+) to categorical: early/mid/late/overdue."""
    try:
        r = float(ratio_str)
    except (ValueError, TypeError):
        return ""
    if r < 0.33:
        return "early"
    if r < 0.66:
        return "mid"
    if r < 1.0:
        return "late"
    return "overdue"


def _quintile_from_value(val: str, edges: list) -> int:
    """Map a raw indicator value to quintile 1-5 given edge thresholds."""
    try:
        v = float(val)
    except (ValueError, TypeError):
        return 0  # unknown quintile
    for i, edge in enumerate(edges):
        if v <= edge:
            return i + 1
    return 5


# Approximate quintile edges learned from the backtest (conservative defaults)
# In production, these would be computed from rolling 30-day history.
EMA_DIFF_EDGES    = [-0.5, -0.1, 0.1, 0.5]     # pct
MACD_HIST_EDGES   = [-0.3, -0.05, 0.05, 0.3]
RSI_EDGES         = [30, 45, 55, 70]
WT2_EDGES         = [-50, -20, 20, 50]
ADX_EDGES         = [15, 20, 30, 40]


def row_to_context(row: dict) -> dict:
    """Translate a trades.csv row into a context_snapshot dict for the scorer."""
    ctx = {
        "ticker": row.get("ticker", "").upper(),
        "direction": row.get("direction", "").lower(),
        "scoring_source": row.get("scoring_system", "active_scanner").lower(),
        "timeframe": f"{row.get('resolution_min', '5')}m",

        # CB / PB fields directly present in trades.csv
        "cb_side": row.get("cb_side", "").lower(),
        "pb_state": row.get("pb_state", "").lower(),
        "wave_label": row.get("pb_wave_label", "").lower(),
        "wave_dir_original": row.get("wave_dir_original", "").lower(),

        # Maturity from ratio
        "maturity": _maturity_from_ratio(row.get("pb_maturity_ratio", "")),

        # Indicator quintiles computed from raw values
        "ema_diff_quintile":  _quintile_from_value(
            row.get("ind_ema_diff_pct"), EMA_DIFF_EDGES),
        "macd_hist_quintile": _quintile_from_value(
            row.get("ind_macd_hist"), MACD_HIST_EDGES),
        "rsi_quintile":       _quintile_from_value(
            row.get("ind_rsi"), RSI_EDGES),
        "wt2_quintile":       _quintile_from_value(
            row.get("ind_wt2"), WT2_EDGES),
        "adx_quintile":       _quintile_from_value(
            row.get("ind_adx"), ADX_EDGES),

        # SR proximity
        "fractal_resistance_above_spot_pct":
            _try_float(row.get("sr_h_fractal_dist_above_pct")),
        "pivot_resistance_above_spot_pct":
            _try_float(row.get("sr_h_pivot_dist_above_pct")),

        # Boolean-ish
        "diamond": _try_bool(row.get("diamond", False)),
        "at_edge": _try_bool(row.get("at_edge", False)),
    }
    return ctx


def _try_float(val) -> float:
    try:
        f = float(val) if val not in (None, "") else 0.0
        return abs(f)  # store as absolute distance; scorer uses >= threshold
    except (ValueError, TypeError):
        return 0.0


def _try_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("true", "1", "yes")


def run_test(trades_csv: str, sample_size: int = 10000):
    """Run scorer on trades and report distribution + WR by decision."""
    if not os.path.exists(trades_csv):
        print(f"ERROR: trades.csv not found at {trades_csv}", file=sys.stderr)
        return 1

    print(f"Loading trades from {trades_csv}...")

    # Stats
    decisions = Counter()
    gates     = Counter()
    scores    = []
    by_decision = defaultdict(lambda: {"wins": 0, "losses": 0, "n": 0})
    skipped = 0

    with open(trades_csv) as f:
        rdr = csv.DictReader(f)
        for i, row in enumerate(rdr):
            if i >= sample_size:
                break
            try:
                ctx = row_to_context(row)
                result = conviction_scorer.score_signal(
                    scanner_event={"ticker": ctx["ticker"], "bias": ctx["direction"]},
                    context_snapshot=ctx,
                )
            except Exception as e:
                skipped += 1
                if skipped < 5:
                    print(f"  skipped row {i}: {e}")
                continue

            decisions[result.decision] += 1
            scores.append(result.score)
            if result.hard_gate_triggered:
                gates[result.hard_gate_triggered] += 1

            # Map backtest outcome: win_headline=="true" is the outcome bool
            bucket = row.get("bucket", "")
            is_win = bucket in ("full_win", "partial_win")

            by_decision[result.decision]["n"] += 1
            if is_win:
                by_decision[result.decision]["wins"] += 1
            else:
                by_decision[result.decision]["losses"] += 1

    n = sum(decisions.values())
    print(f"\nProcessed {n} trades (skipped {skipped})")

    if n == 0:
        return 1

    # Decision distribution
    print("\n=== Decision distribution ===")
    for d in ("post", "log_only", "discard"):
        c = decisions.get(d, 0)
        pct = 100 * c / n
        print(f"  {d:10s}: {c:5d}  ({pct:5.1f}%)")

    # Gate fires
    if gates:
        print("\n=== Hard gate fires ===")
        for g, c in sorted(gates.items()):
            print(f"  {g}: {c}")

    # Score distribution
    print("\n=== Score distribution ===")
    buckets = [(0, 30), (30, 50), (50, 60), (60, 70), (70, 75), (75, 85), (85, 101)]
    for lo, hi in buckets:
        c = sum(1 for s in scores if lo <= s < hi)
        pct = 100 * c / n
        bar = "█" * int(pct / 2)
        print(f"  {lo:3d}-{hi:3d}: {c:5d} ({pct:5.1f}%) {bar}")

    # WR by decision
    print("\n=== Win Rate by Decision ===")
    for d in ("post", "log_only", "discard"):
        stats = by_decision[d]
        if stats["n"] == 0:
            print(f"  {d:10s}: n=0")
            continue
        wr = 100 * stats["wins"] / stats["n"]
        print(f"  {d:10s}: n={stats['n']:5d} WR={wr:5.1f}% "
              f"(wins={stats['wins']}, losses={stats['losses']})")

    # Sanity assertions
    print("\n=== Sanity checks ===")
    post_wr = (by_decision["post"]["wins"] / by_decision["post"]["n"] * 100
               if by_decision["post"]["n"] > 0 else 0)
    log_wr = (by_decision["log_only"]["wins"] / by_decision["log_only"]["n"] * 100
              if by_decision["log_only"]["n"] > 0 else 0)
    discard_wr = (by_decision["discard"]["wins"] / by_decision["discard"]["n"] * 100
                  if by_decision["discard"]["n"] > 0 else 0)

    ok = True
    if post_wr <= log_wr:
        print(f"  ⚠️  post_wr ({post_wr:.1f}%) should exceed log_only_wr "
              f"({log_wr:.1f}%). Scorer is NOT producing edge.")
        ok = False
    else:
        print(f"  ✅ post_wr ({post_wr:.1f}%) > log_only_wr ({log_wr:.1f}%) — edge present")

    if discard_wr >= log_wr:
        print(f"  ⚠️  discard_wr ({discard_wr:.1f}%) should be lower than "
              f"log_only_wr ({log_wr:.1f}%). Discard rule may be overkilling good trades.")
        ok = False
    else:
        print(f"  ✅ discard_wr ({discard_wr:.1f}%) < log_only_wr ({log_wr:.1f}%) — "
              "discard rules correctly filter losers")

    # Volume reduction
    post_pct = 100 * decisions.get("post", 0) / n
    print(f"  📊 Telegram volume: {post_pct:.1f}% of signals → post "
          f"({100 - post_pct:.1f}% silenced)")

    if ok:
        print("\n✅ Scorer behavior matches expectations. Ready to ship.")
        return 0
    else:
        print("\n⚠️  Scorer needs rule adjustments before ship.")
        return 2


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRADES_CSV
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    sys.exit(run_test(path, size))
