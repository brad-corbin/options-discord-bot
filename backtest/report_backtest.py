#!/usr/bin/env python3
"""
Standalone backtest report script.

What it does:
- Reads trades from either a trades.csv file OR a backtest ZIP file
- Prints a plain-English summary to the console
- Writes grouped stats CSVs
- Writes an equity curve CSV
- Writes a drawdown CSV
- Runs simple filter / "what-if" scenarios without changing the strategy engine

Examples:
    python backtest/report_backtest.py --input backtest/results/trades.csv --output backtest/report
    python backtest/report_backtest.py --input backtest-results-SPY-13.zip --output backtest/report
"""

from __future__ import annotations

import argparse
import io
import os
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterable, Tuple

import pandas as pd


REQUIRED_COLUMNS = {
    "date",
    "ticker",
    "direction",
    "entry_type",
    "setup_score",
    "time_phase",
    "bias",
    "regime",
    "gex_sign",
    "volatility_regime",
    "prior_day_context",
    "entry_bar_time",
    "close_bar_time",
    "close_reason",
    "status",
    "pnl_pts",
    "pnl_pct",
    "mae_pts",
    "mfe_pts",
    "exit_policy",
}


GROUP_COLUMNS = [
    "ticker",
    "direction",
    "entry_type",
    "setup_score",
    "time_phase",
    "regime",
    "gex_sign",
    "bias",
    "volatility_regime",
    "prior_day_context",
    "exit_policy",
    "status",
]


SCENARIOS: Dict[str, Callable[[pd.DataFrame], pd.DataFrame]] = {
    "all_trades": lambda df: df.copy(),
    "score_5_only": lambda df: df[df["setup_score"] >= 5].copy(),
    "no_afternoon": lambda df: df[~df["time_phase"].isin(["AFTERNOON"])].copy(),
    "score_5_no_afternoon": lambda df: df[(df["setup_score"] >= 5) & (~df["time_phase"].isin(["AFTERNOON"]))].copy(),
    "long_only": lambda df: df[df["direction"] == "LONG"].copy(),
    "exclude_gex_neg_squeeze": lambda df: df[df["exit_policy"] != "GEX_NEG_SQUEEZE"].copy(),
    "exclude_low_vol_chop_shorts": lambda df: df[~((df["regime"] == "LOW_VOL_CHOP") & (df["direction"] == "SHORT"))].copy(),
    "score_5_morning_power_close": lambda df: df[(df["setup_score"] >= 5) & (df["time_phase"].isin(["MORNING", "POWER_HOUR", "CLOSE"]))].copy(),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate reports from a backtest trades file or ZIP.")
    parser.add_argument(
        "--input",
        required=True,
        help="Path to trades.csv or a backtest ZIP containing results/trades.csv",
    )
    parser.add_argument(
        "--output",
        default="backtest/report",
        help="Folder where report files should be written. Default: backtest/report",
    )
    return parser.parse_args()


def load_trades(input_path: Path) -> pd.DataFrame:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if input_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(input_path, "r") as zf:
            candidates = [name for name in zf.namelist() if name.endswith("trades.csv")]
            if not candidates:
                raise RuntimeError("No trades.csv found inside ZIP.")
            preferred = None
            for name in candidates:
                if name.endswith("results/trades.csv"):
                    preferred = name
                    break
            chosen = preferred or candidates[0]
            with zf.open(chosen) as f:
                df = pd.read_csv(io.BytesIO(f.read()))
    else:
        df = pd.read_csv(input_path)

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise RuntimeError(f"Trades file is missing required columns: {missing_text}")

    return normalize_trades(df)


def normalize_trades(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in ["entry_bar_time", "close_bar_time"]:
        out[col] = pd.to_datetime(out[col], errors="coerce")

    out["date"] = pd.to_datetime(out["date"], errors="coerce")

    numeric_cols = ["setup_score", "entry_price", "close_price", "stop_level", "pnl_pts", "pnl_pct", "mae_pts", "mae_pct", "mfe_pts", "mfe_pct"]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    text_cols = [
        "ticker", "direction", "entry_type", "setup_label", "level_name", "level_tier", "time_phase", "bias", "regime",
        "gex_sign", "volatility_regime", "prior_day_context", "close_reason", "status", "exit_policy", "validation_summary"
    ]
    for col in text_cols:
        if col in out.columns:
            out[col] = out[col].fillna("UNKNOWN").astype(str)

    out = out.sort_values(["close_bar_time", "entry_bar_time", "date"], na_position="last").reset_index(drop=True)
    out["is_win"] = out["pnl_pts"] > 0
    out["is_loss"] = out["pnl_pts"] < 0
    out["is_flat"] = out["pnl_pts"] == 0
    out["trade_num"] = range(1, len(out) + 1)
    out["cum_pnl_pts"] = out["pnl_pts"].fillna(0).cumsum()
    out["equity_peak_pts"] = out["cum_pnl_pts"].cummax()
    out["drawdown_pts"] = out["cum_pnl_pts"] - out["equity_peak_pts"]
    out["bars_held"] = compute_bars_held(out)
    return out


def compute_bars_held(df: pd.DataFrame) -> pd.Series:
    if "entry_bar_time" not in df.columns or "close_bar_time" not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index)
    minutes = (df["close_bar_time"] - df["entry_bar_time"]).dt.total_seconds() / 60.0
    bars = (minutes / 5.0).round()
    return bars.astype("Int64")


def make_summary(df: pd.DataFrame) -> Dict[str, float]:
    closed = df[df["close_bar_time"].notna()].copy()
    total = len(df)
    closed_count = len(closed)
    open_count = int(total - closed_count)
    wins = int((closed["pnl_pts"] > 0).sum())
    losses = int((closed["pnl_pts"] < 0).sum())
    flats = int((closed["pnl_pts"] == 0).sum())
    win_rate = (wins / closed_count * 100.0) if closed_count else 0.0
    total_pnl = float(closed["pnl_pts"].sum()) if closed_count else 0.0
    avg_trade = float(closed["pnl_pts"].mean()) if closed_count else 0.0
    median_trade = float(closed["pnl_pts"].median()) if closed_count else 0.0
    best = float(closed["pnl_pts"].max()) if closed_count else 0.0
    worst = float(closed["pnl_pts"].min()) if closed_count else 0.0
    profit_factor = calculate_profit_factor(closed)
    expectancy = avg_trade
    max_drawdown = float(df["drawdown_pts"].min()) if len(df) else 0.0

    return {
        "total_trades": total,
        "closed_trades": closed_count,
        "open_trades": open_count,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": win_rate,
        "total_pnl_pts": total_pnl,
        "avg_trade_pts": avg_trade,
        "median_trade_pts": median_trade,
        "best_trade_pts": best,
        "worst_trade_pts": worst,
        "profit_factor": profit_factor,
        "expectancy_pts": expectancy,
        "max_drawdown_pts": max_drawdown,
    }


def calculate_profit_factor(df: pd.DataFrame) -> float:
    gross_profit = float(df.loc[df["pnl_pts"] > 0, "pnl_pts"].sum())
    gross_loss = abs(float(df.loc[df["pnl_pts"] < 0, "pnl_pts"].sum()))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def grouped_stats(df: pd.DataFrame, column: str) -> pd.DataFrame:
    if column not in df.columns:
        raise KeyError(f"Column not found: {column}")

    g = df.groupby(column, dropna=False)
    stats = g["pnl_pts"].agg(["count", "sum", "mean", "median", "max", "min"]).reset_index()
    wins = g.apply(lambda x: (x["pnl_pts"] > 0).sum(), include_groups=False).reset_index(name="wins")
    losses = g.apply(lambda x: (x["pnl_pts"] < 0).sum(), include_groups=False).reset_index(name="losses")
    flats = g.apply(lambda x: (x["pnl_pts"] == 0).sum(), include_groups=False).reset_index(name="flats")
    avg_mfe = g["mfe_pts"].mean().reset_index(name="avg_mfe_pts")
    avg_mae = g["mae_pts"].mean().reset_index(name="avg_mae_pts")
    avg_bars = g["bars_held"].mean().reset_index(name="avg_bars_held")

    out = stats.merge(wins, on=column).merge(losses, on=column).merge(flats, on=column)
    out = out.merge(avg_mfe, on=column).merge(avg_mae, on=column).merge(avg_bars, on=column)
    out["win_rate_pct"] = (out["wins"] / out["count"] * 100.0).round(1)
    out = out.rename(
        columns={
            "count": "trades",
            "sum": "total_pnl_pts",
            "mean": "avg_pnl_pts",
            "median": "median_pnl_pts",
            "max": "best_trade_pts",
            "min": "worst_trade_pts",
        }
    )
    return out.sort_values(["total_pnl_pts", "win_rate_pct"], ascending=[False, False]).reset_index(drop=True)


def build_equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "trade_num",
        "date",
        "entry_bar_time",
        "close_bar_time",
        "ticker",
        "direction",
        "entry_type",
        "setup_score",
        "time_phase",
        "regime",
        "exit_policy",
        "pnl_pts",
        "cum_pnl_pts",
        "equity_peak_pts",
        "drawdown_pts",
    ]
    keep = [c for c in cols if c in df.columns]
    return df[keep].copy()


def build_drawdown_table(df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    out = df[["trade_num", "date", "close_bar_time", "ticker", "direction", "entry_type", "setup_score", "time_phase", "regime", "exit_policy", "pnl_pts", "cum_pnl_pts", "drawdown_pts"]].copy()
    out = out.sort_values("drawdown_pts").head(top_n).reset_index(drop=True)
    return out


def scenario_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, fn in SCENARIOS.items():
        sub = fn(df)
        stats = make_summary(sub)
        rows.append({
            "scenario": name,
            "trades": stats["total_trades"],
            "closed_trades": stats["closed_trades"],
            "win_rate_pct": round(stats["win_rate"], 1),
            "total_pnl_pts": round(stats["total_pnl_pts"], 4),
            "avg_trade_pts": round(stats["avg_trade_pts"], 4),
            "profit_factor": round(stats["profit_factor"], 3) if stats["profit_factor"] != float("inf") else "inf",
            "max_drawdown_pts": round(stats["max_drawdown_pts"], 4),
        })
    out = pd.DataFrame(rows)
    return out.sort_values("total_pnl_pts", ascending=False).reset_index(drop=True)


def write_text_summary(df: pd.DataFrame, output_dir: Path) -> None:
    s = make_summary(df)

    lines = []
    lines.append("=" * 60)
    lines.append("  REPORT SUMMARY")
    lines.append("=" * 60)
    lines.append(f"  Total trades:      {s['total_trades']}")
    lines.append(f"  Closed trades:     {s['closed_trades']}")
    lines.append(f"  Open trades:       {s['open_trades']}")
    lines.append("")
    lines.append(f"  Win rate:          {s['win_rate']:.1f}%  ({s['wins']}W / {s['losses']}L / {s['flats']} flat)")
    lines.append(f"  Total P&L:         {s['total_pnl_pts']:+.4f} pts")
    lines.append(f"  Avg trade:         {s['avg_trade_pts']:+.4f} pts")
    lines.append(f"  Median trade:      {s['median_trade_pts']:+.4f} pts")
    lines.append(f"  Best trade:        {s['best_trade_pts']:+.4f} pts")
    lines.append(f"  Worst trade:       {s['worst_trade_pts']:+.4f} pts")
    lines.append(f"  Profit factor:     {s['profit_factor']:.3f}" if s['profit_factor'] != float('inf') else "  Profit factor:     inf")
    lines.append(f"  Expectancy:        {s['expectancy_pts']:+.4f} pts/trade")
    lines.append(f"  Max drawdown:      {s['max_drawdown_pts']:+.4f} pts")
    lines.append("")

    scenario_df = scenario_table(df)
    lines.append("Top scenario tests:")
    for _, row in scenario_df.head(8).iterrows():
        lines.append(
            f"  {row['scenario']:<28} {int(row['trades']):>4} trades | "
            f"{row['win_rate_pct']:>5}% win | {row['total_pnl_pts']:+.4f} pts | DD {row['max_drawdown_pts']:+.4f}"
        )

    (output_dir / "report_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_console_summary(df: pd.DataFrame, output_dir: Path) -> None:
    s = make_summary(df)
    print()
    print("=" * 60)
    print("REPORT SUMMARY")
    print("=" * 60)
    print(f"Trades:         {s['total_trades']}")
    print(f"Closed:         {s['closed_trades']}")
    print(f"Open:           {s['open_trades']}")
    print(f"Win rate:       {s['win_rate']:.1f}%")
    print(f"Total P&L:      {s['total_pnl_pts']:+.4f} pts")
    print(f"Avg trade:      {s['avg_trade_pts']:+.4f} pts")
    print(f"Profit factor:  {s['profit_factor']:.3f}" if s['profit_factor'] != float('inf') else "Profit factor:  inf")
    print(f"Max drawdown:   {s['max_drawdown_pts']:+.4f} pts")
    print()
    print("Best quick what-if tests:")
    scen = scenario_table(df)
    for _, row in scen.head(6).iterrows():
        print(
            f"  {row['scenario']:<28} {int(row['trades']):>4} trades | "
            f"{row['win_rate_pct']:>5}% win | {row['total_pnl_pts']:+.4f} pts"
        )
    print()
    print(f"Report files written to: {output_dir}")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = load_trades(input_path)

    # Save normalized trades used by the report.
    trades.to_csv(output_dir / "normalized_trades.csv", index=False)

    # Save grouped tables.
    for col in GROUP_COLUMNS:
        if col in trades.columns:
            grouped_stats(trades, col).to_csv(output_dir / f"by_{col}.csv", index=False)

    # Save equity curve, drawdown table, and scenario tests.
    build_equity_curve(trades).to_csv(output_dir / "equity_curve.csv", index=False)
    build_drawdown_table(trades).to_csv(output_dir / "worst_drawdowns.csv", index=False)
    scenario_table(trades).to_csv(output_dir / "scenario_tests.csv", index=False)

    # Save a very simple daily P&L view.
    daily = trades.groupby(trades["date"].dt.date, dropna=False)["pnl_pts"].agg(["count", "sum", "mean"]).reset_index()
    daily = daily.rename(columns={"date": "session_date", "count": "trades", "sum": "total_pnl_pts", "mean": "avg_pnl_pts"})
    daily.to_csv(output_dir / "daily_pnl.csv", index=False)

    write_text_summary(trades, output_dir)
    print_console_summary(trades, output_dir)


if __name__ == "__main__":
    main()
