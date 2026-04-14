#!/usr/bin/env python3
"""
run_all.py — Omega 3000 Full System Backtest
═════════════════════════════════════════════
Runs all backtest engines and produces a combined report.

Engines:
  1. Active Scanner (bt_active.py)     — Intraday 5min signals, 1-5d holds
  2. Swing Scanner (bt_swing.py)       — Daily Fib retracement, 1-15d holds
  3. EM Model (bt_em.py)               — Expected move predictions, premium selling
  4. Income Scanner (bt_income.py)     — Credit spreads at support/resistance
  5. Potter Box (potter_backtest.py)    — Box breakout through voids
  6. Conviction/Flow (bt_conviction.py) — Volume burst + shadow signals

Prerequisites:
  - MARKETDATA_TOKEN env var set (for data download)
  - OR pre-downloaded data in backtest/data/

Usage:
  # Full suite (all tickers, all engines)
  python backtest/run_all.py --full

  # Quick test (1 ticker per engine)
  python backtest/run_all.py --quick

  # Select engines
  python backtest/run_all.py --engines active swing em

  # Custom tickers
  python backtest/run_all.py --tickers SPY QQQ NVDA --engines active em

  # Custom date range
  python backtest/run_all.py --days 365 --engines active swing
"""

import os, sys, argparse, subprocess, time
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Engine definitions
ENGINES = {
    "active": {
        "script": "bt_active.py",
        "desc": "Active Scanner (intraday 5min → 1-5d holds)",
        "quick_args": ["--ticker", "SPY", "--days", "90"],
        "full_args": ["--all", "--days", "270"],
    },
    "swing": {
        "script": "bt_swing.py",
        "desc": "Swing Scanner (daily Fib retracement → 1-15d holds)",
        "quick_args": ["--ticker", "NVDA", "--days", "400"],
        "full_args": ["--all", "--days", "500"],
    },
    "em": {
        "script": "bt_em.py",
        "desc": "EM Model (expected move predictions)",
        "quick_args": ["--ticker", "SPY", "--days", "365"],
        "full_args": ["--ticker", "SPY", "--ticker", "QQQ", "--ticker", "IWM", "--days", "500"],
    },
    "income": {
        "script": "bt_income.py",
        "desc": "Income Scanner (credit spreads at S/R)",
        "quick_args": ["--ticker", "SPY", "--days", "365"],
        "full_args": ["--all", "--days", "365"],
    },
    "potter": {
        "script": "potter_backtest.py",
        "desc": "Potter Box (box breakout through voids)",
        "quick_args": None,  # Requires --data flag
        "full_args": None,
    },
    "conviction": {
        "script": "bt_conviction.py",
        "desc": "Conviction/Shadow (volume burst + technical)",
        "quick_args": ["--ticker", "TSLA", "--days", "180"],
        "full_args": ["--all", "--days", "270"],
    },
}


def run_engine(name, cfg, mode, custom_tickers=None, custom_days=None, out_dir="results"):
    """Run a single backtest engine."""
    script = os.path.join(SCRIPT_DIR, cfg["script"])
    if not os.path.exists(script):
        print(f"  ⚠ Script not found: {script}")
        return False

    if cfg.get(f"{mode}_args") is None:
        print(f"  ⚠ {name}: no {mode} config (requires manual --data flag)")
        return False

    args = [sys.executable, script]
    if custom_tickers:
        # Override tickers
        if name in ("em",):
            for t in custom_tickers:
                args.extend(["--ticker", t])
        else:
            args.extend(["--ticker", custom_tickers[0]])
    else:
        args.extend(cfg[f"{mode}_args"])

    if custom_days:
        # Replace --days if present
        if "--days" in args:
            idx = args.index("--days")
            args[idx + 1] = str(custom_days)
        else:
            args.extend(["--days", str(custom_days)])

    args.extend(["--out-dir", out_dir])

    print(f"\n{'━'*62}")
    print(f"  ▶ {name.upper()}: {cfg['desc']}")
    print(f"  Command: {' '.join(args[1:])}")
    print(f"{'━'*62}\n")

    t0 = time.time()
    try:
        result = subprocess.run(args, cwd=os.path.dirname(SCRIPT_DIR), timeout=3600)
        elapsed = time.time() - t0
        status = "✓" if result.returncode == 0 else "✗"
        print(f"\n  {status} {name} completed in {elapsed:.0f}s (exit code {result.returncode})")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"\n  ✗ {name} timed out after 1 hour")
        return False
    except Exception as e:
        print(f"\n  ✗ {name} failed: {e}")
        return False


def write_combined_report(results, out_dir):
    """Read all individual summaries and combine into one report."""
    out_path = Path(out_dir)
    lines = [
        "═" * 62,
        "  OMEGA 3000 — FULL SYSTEM BACKTEST REPORT",
        f"  Generated: {date.today().isoformat()}",
        "═" * 62,
        "",
        "  ENGINE STATUS:",
    ]

    for name, success in results.items():
        status = "✓ PASS" if success else "✗ FAIL/SKIP"
        lines.append(f"    {name:>12}: {status}")

    lines.append("")

    # Collect all summary files
    for name in results:
        summary_files = sorted(out_path.glob(f"*{name}*.txt"))
        if not summary_files:
            summary_files = sorted(out_path.glob(f"*{name.split('_')[0]}*.txt"))
        for sf in summary_files:
            lines.append(f"\n{'─'*62}")
            lines.append(f"  FROM: {sf.name}")
            lines.append(f"{'─'*62}")
            try:
                lines.append(sf.read_text())
            except Exception:
                lines.append("  (could not read)")

    report = "\n".join(lines)
    report_path = out_path / "FULL_BACKTEST_REPORT.txt"
    report_path.write_text(report)
    print(f"\n{'═'*62}")
    print(f"  Combined report → {report_path}")
    print(f"{'═'*62}")


def main():
    ap = argparse.ArgumentParser(description="Omega 3000 Full System Backtest")
    ap.add_argument("--quick", action="store_true", help="Quick test: 1 ticker per engine")
    ap.add_argument("--full", action="store_true", help="Full suite: all tickers, all engines")
    ap.add_argument("--engines", nargs="+", default=None,
                    help=f"Select engines: {', '.join(ENGINES.keys())}")
    ap.add_argument("--tickers", nargs="+", default=None, help="Override tickers")
    ap.add_argument("--days", type=int, default=None, help="Override lookback days")
    ap.add_argument("--out-dir", default="results", help="Output directory")
    args = ap.parse_args()

    if not args.quick and not args.full and not args.engines:
        print("Usage: run_all.py --quick | --full | --engines active swing em ...")
        print(f"\nAvailable engines:")
        for name, cfg in ENGINES.items():
            print(f"  {name:>12}: {cfg['desc']}")
        return

    mode = "full" if args.full else "quick"
    engine_names = args.engines or list(ENGINES.keys())

    # Remove potter from auto-run if no data files specified
    if "potter" in engine_names and not args.tickers:
        print("  Note: Potter box requires --data flag; skipping in auto mode.")
        print("  Run manually: python backtest/potter_backtest.py --ticker AAPL --data path/to/daily.csv")
        engine_names = [e for e in engine_names if e != "potter"]

    out_dir = args.out_dir
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*62}")
    print(f"  OMEGA 3000 BACKTEST SUITE")
    print(f"  Mode: {mode.upper()} | Engines: {', '.join(engine_names)}")
    if args.tickers:
        print(f"  Tickers: {', '.join(args.tickers)}")
    if args.days:
        print(f"  Lookback: {args.days} days")
    print(f"  Output: {out_dir}/")
    print(f"{'═'*62}")

    if not os.getenv("MARKETDATA_TOKEN"):
        print("\n  ⚠ WARNING: MARKETDATA_TOKEN not set. Data download will fail.")
        print("  Set it with: export MARKETDATA_TOKEN=your_token_here")
        print("  Or use pre-downloaded data files.\n")

    results = {}
    total_start = time.time()

    for name in engine_names:
        if name not in ENGINES:
            print(f"  Unknown engine: {name}")
            results[name] = False
            continue
        results[name] = run_engine(
            name, ENGINES[name], mode,
            custom_tickers=args.tickers,
            custom_days=args.days,
            out_dir=out_dir,
        )

    total_elapsed = time.time() - total_start

    print(f"\n{'═'*62}")
    print(f"  BACKTEST SUITE COMPLETE — {total_elapsed:.0f}s total")
    print(f"{'═'*62}")
    for name, success in results.items():
        status = "✓" if success else "✗"
        print(f"  {status} {name}")

    write_combined_report(results, out_dir)


if __name__ == "__main__":
    main()
