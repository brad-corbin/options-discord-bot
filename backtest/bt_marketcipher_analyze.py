#!/usr/bin/env python3
"""
bt_marketcipher_analyze.py — Filter Stack Analysis on MC Weekly Output
═══════════════════════════════════════════════════════════════════════

Reads the trades.csv produced by bt_marketcipher_weekly.py and tests all
combinations of confluence flags + regime filters to find the best stack.

Ranks by Kelly fraction, gated on minimum sample size to guard against
overfitting. Also produces drop-out analysis (what each filter *costs* in
sample size vs *adds* in edge).

Does NOT re-run the backtest — purely reads the CSV.

USAGE
    python backtest/bt_marketcipher_analyze.py
    python backtest/bt_marketcipher_analyze.py --csv /tmp/backtest_mc_weekly/trades.csv
    python backtest/bt_marketcipher_analyze.py --min-n 50     # loosen overfit guard

DEFAULTS
    --csv      /tmp/backtest_mc_weekly/trades.csv
    --min-n    100   (minimum signals in bucket to be ranked)
    --out-dir  /tmp/backtest_mc_weekly

OUTPUTS
    filter_stack_all_combinations.csv   all 2^6 = 64 combinations
    filter_stack_ranked.csv             top combos by Kelly (N >= min_n)
    filter_stack_report.md              executive summary + recommendations
"""

import argparse
import csv
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

DEFAULT_CSV = Path("/tmp/backtest_mc_weekly/trades.csv")
DEFAULT_OUT = Path("/tmp/backtest_mc_weekly")


# Filter primitives: (name, predicate, description)
# Each predicate is: given a dict-row from trades.csv, return bool.
def _t(v: Any) -> bool:
    """Truthy parse of CSV string."""
    return str(v).strip().lower() in ("true", "1", "yes")

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


FILTERS: List[Tuple[str, Callable[[dict], bool], str]] = [
    ("rsi_oversold",       lambda r: _t(r.get("rsi_oversold")),
                           "Weekly RSI < 40 at signal"),
    ("stoch_rsi_oversold", lambda r: _t(r.get("stoch_rsi_oversold")),
                           "Weekly Stoch RSI < 20 at signal"),
    ("macd_hist_negative", lambda r: _t(r.get("macd_hist_negative")),
                           "Weekly MACD hist < 0 at signal"),
    ("bar_green",          lambda r: _t(r.get("bar_green")),
                           "Signal-bar close > open (confirming reversal)"),
    ("not_chop",           lambda r: str(r.get("regime_trend","")).strip() != "CHOP",
                           "Regime != CHOP at entry"),
    ("bull_regime",        lambda r: str(r.get("regime_trend","")).strip().startswith("BULL"),
                           "Regime in BULL_BASE or BULL_TRANSITION"),
    # v8.2 (Patch 3): Potter Box confluence. Only populated in bt_marketcipher_daily
    # output; on weekly CSVs these fields don't exist and the filter returns False
    # for every row (no effect on stacks where it's not used).
    ("near_potter_floor_2pct", lambda r: _t(r.get("near_potter_floor_2pct")),
                               "Signal within 2% of a historical Potter Box floor"),
    ("near_potter_floor_5pct", lambda r: _t(r.get("near_potter_floor_5pct")),
                               "Signal within 5% of a historical Potter Box floor"),
]


# ═══════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════

def _stats(rows: List[dict], label: str) -> dict:
    n = len(rows)
    if n == 0:
        return {"label": label, "n": 0}
    wins = [r for r in rows if _t(r.get("win"))]
    losses = [r for r in rows if not _t(r.get("win"))]
    pnls = [_f(r.get("pnl_pct")) for r in rows]
    total_pnl = sum(pnls)
    gw = sum(_f(r.get("pnl_pct")) for r in wins)
    gl = abs(sum(_f(r.get("pnl_pct")) for r in losses))
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    wr = len(wins) / n
    avg_win = (gw / len(wins)) if wins else 0.0
    avg_loss = (gl / len(losses)) if losses else 0.0
    R = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    kelly = (wr - (1 - wr) / R) if R > 0 else 0.0

    # MFE / MAE
    mfe = sum(_f(r.get("mfe_pct")) for r in rows) / n
    mae = sum(_f(r.get("mae_pct")) for r in rows) / n
    hold = sum(_f(r.get("hold_days_to_exit")) for r in rows) / n

    # Target hit rates
    t100_hits = sum(1 for r in rows if int(_f(r.get("t_to_100pct", -1))) >= 0)
    t50_hits = sum(1 for r in rows if int(_f(r.get("t_to_50pct", -1))) >= 0)

    return {
        "label": label, "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(wr * 100, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "avg_pnl_pct": round(total_pnl / n, 3),
        "total_pnl_pct": round(total_pnl, 2),
        "kelly_fraction": round(kelly, 4),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(-avg_loss, 3),
        "avg_mfe_pct": round(mfe, 3),
        "avg_mae_pct": round(mae, 3),
        "avg_hold_days": round(hold, 1),
        "t50_hit_rate_pct": round(t50_hits / n * 100, 2),
        "t100_hit_rate_pct": round(t100_hits / n * 100, 2),
    }


def _apply_filter_mask(rows: List[dict], mask: Tuple[bool, ...]) -> List[dict]:
    """mask[i] = True means filter[i] is required. False = filter not applied."""
    active_filters = [f for f, m in zip(FILTERS, mask) if m]
    if not active_filters:
        return list(rows)
    out = []
    for r in rows:
        if all(pred(r) for (_name, pred, _desc) in active_filters):
            out.append(r)
    return out


def _mask_label(mask: Tuple[bool, ...]) -> str:
    active = [name for (name, _, _), m in zip(FILTERS, mask) if m]
    return " & ".join(active) if active else "(no filters)"


# ═══════════════════════════════════════════════════════════
# COMBINATION RUNNER
# ═══════════════════════════════════════════════════════════

def run_all_combinations(rows: List[dict]) -> List[dict]:
    """Test all 2^len(FILTERS) filter combinations. Return list of stat dicts."""
    results = []
    n_filters = len(FILTERS)
    for mask in itertools.product([False, True], repeat=n_filters):
        subset = _apply_filter_mask(rows, mask)
        s = _stats(subset, _mask_label(mask))
        s["mask"] = "|".join("1" if m else "0" for m in mask)
        s["n_filters_active"] = sum(mask)
        results.append(s)
    return results


def single_filter_dropout(rows: List[dict]) -> List[dict]:
    """For each filter individually, compare 'with filter' vs 'without'.
    This shows marginal contribution of each filter."""
    baseline = _stats(rows, "BASELINE_ALL")
    out = [baseline]
    for (name, pred, desc) in FILTERS:
        on_rows = [r for r in rows if pred(r)]
        off_rows = [r for r in rows if not pred(r)]
        out.append({**_stats(on_rows, f"ONLY_{name}_TRUE"), "description": desc})
        out.append({**_stats(off_rows, f"ONLY_{name}_FALSE"), "description": f"not: {desc}"})
    return out


# ═══════════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════════

def _pf_key(r: dict):
    pf = r.get("profit_factor", 0)
    return pf if pf != "inf" else 999.0


def _kelly_key(r: dict):
    return r.get("kelly_fraction", -999)


def write_all_combinations_csv(results: List[dict], path: Path) -> None:
    if not results:
        return
    keys = ["mask", "n_filters_active", "label", "n", "wins", "losses",
            "win_rate_pct", "profit_factor", "kelly_fraction",
            "avg_pnl_pct", "total_pnl_pct",
            "avg_win_pct", "avg_loss_pct", "avg_mfe_pct", "avg_mae_pct",
            "avg_hold_days", "t50_hit_rate_pct", "t100_hit_rate_pct"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in sorted(results, key=lambda x: (-x.get("n_filters_active", 0),
                                                 -(x.get("kelly_fraction", -999) or 0))):
            w.writerow([r.get(k, "") for k in keys])


def write_ranked_csv(results: List[dict], path: Path, min_n: int) -> None:
    qualifying = [r for r in results if r.get("n", 0) >= min_n]
    qualifying.sort(key=_kelly_key, reverse=True)
    keys = ["rank", "label", "n_filters_active", "n",
            "win_rate_pct", "profit_factor", "kelly_fraction",
            "avg_pnl_pct", "t50_hit_rate_pct", "t100_hit_rate_pct",
            "avg_mfe_pct", "avg_mae_pct", "avg_hold_days"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for i, r in enumerate(qualifying, 1):
            w.writerow([i] + [r.get(k, "") for k in keys[1:]])


def _fmt_stat(s: dict) -> str:
    pf = s.get("profit_factor", 0)
    pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
    return (f"{s['n']:>4}T | WR {s['win_rate_pct']:>5.1f}% | "
            f"PF {pf_s:>5} | Kelly {s['kelly_fraction']:>+7.3f} | "
            f"PnL {s['avg_pnl_pct']:>+6.2f}% | "
            f"+50% hit {s.get('t50_hit_rate_pct',0):>4.1f}% | "
            f"+100% hit {s.get('t100_hit_rate_pct',0):>4.1f}%")


def write_report(results: List[dict], baseline: dict, rows: List[dict],
                 out_path: Path, min_n: int) -> None:
    lines: List[str] = []
    lines.append("# Market Cipher Weekly — Filter Stack Analysis")
    lines.append("")
    lines.append(f"- Input: {len(rows)} signals")
    lines.append(f"- Sample-size floor: **N >= {min_n}** (guards against overfitting)")
    lines.append(f"- Filter primitives: {', '.join(name for name, _, _ in FILTERS)}")
    lines.append("")

    # Baseline
    lines.append("## Baseline (no filters)")
    lines.append(""); lines.append("```")
    lines.append(f"  {_fmt_stat(baseline)}")
    lines.append("```"); lines.append("")

    # Single-filter dropout
    lines.append("## Single-Filter Impact — what each filter contributes")
    lines.append("")
    lines.append("*Shows stats if you kept ONLY signals where the flag is True vs False.*")
    lines.append(""); lines.append("```")
    lines.append(f"  {'Filter':<30} {'n':>4} {'WR':>6} {'PF':>6} {'Kelly':>8}  ΔKelly")
    lines.append(f"  {'-'*30} {'-'*4} {'-'*6} {'-'*6} {'-'*8}  ------")
    base_kelly = baseline.get("kelly_fraction", 0)
    dropout = single_filter_dropout(rows)
    for d in dropout[1:]:  # skip baseline itself
        lbl = d.get("label", "")
        if d.get("n", 0) == 0:
            lines.append(f"  {lbl:<30} {'0':>4}  ---    ---    ---     ---")
            continue
        pf = d.get("profit_factor", 0)
        pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
        dk = d.get("kelly_fraction", 0) - base_kelly
        lines.append(f"  {lbl:<30} {d['n']:>4} {d['win_rate_pct']:>5.1f}% "
                     f"{pf_s:>6} {d.get('kelly_fraction',0):>+8.3f}  {dk:>+.3f}")
    lines.append("```"); lines.append("")

    # Ranked combinations (Kelly, N >= min_n)
    qualifying = [r for r in results if r.get("n", 0) >= min_n]
    qualifying.sort(key=_kelly_key, reverse=True)

    lines.append(f"## Top 20 Filter Stacks by Kelly (N >= {min_n})")
    lines.append("")
    lines.append("*Ordered by Kelly fraction. Higher Kelly = bigger edge. "
                 "Stacks with N < min_n are excluded to guard against overfitting.*")
    lines.append(""); lines.append("```")
    lines.append(f"  # | {'Filter Stack':<45} | {'N':>4} | {'WR':>6} | {'PF':>6} | "
                 f"{'Kelly':>7} | {'+100%':>6}")
    lines.append(f"  - | {'-'*45} | {'-'*4} | {'-'*6} | {'-'*6} | {'-'*7} | {'-'*6}")
    for i, r in enumerate(qualifying[:20], 1):
        lbl = r["label"][:45]
        pf = r.get("profit_factor", 0)
        pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
        lines.append(f"  {i:>2} | {lbl:<45} | {r['n']:>4} | "
                     f"{r['win_rate_pct']:>5.1f}% | {pf_s:>6} | "
                     f"{r['kelly_fraction']:>+7.3f} | {r.get('t100_hit_rate_pct',0):>5.1f}%")
    lines.append("```"); lines.append("")

    # Top by WR (different lens)
    by_wr = sorted(qualifying, key=lambda r: -r.get("win_rate_pct", 0))
    lines.append(f"## Top 10 by Win Rate (N >= {min_n})")
    lines.append(""); lines.append("```")
    for i, r in enumerate(by_wr[:10], 1):
        lbl = r["label"][:45]
        pf = r.get("profit_factor", 0)
        pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
        lines.append(f"  {i:>2}. {lbl:<45}  WR {r['win_rate_pct']:>5.1f}% | "
                     f"N {r['n']:>3} | PF {pf_s:>5} | Kelly {r['kelly_fraction']:>+7.3f}")
    lines.append("```"); lines.append("")

    # Top by PF
    by_pf = sorted(qualifying, key=_pf_key, reverse=True)
    lines.append(f"## Top 10 by Profit Factor (N >= {min_n})")
    lines.append(""); lines.append("```")
    for i, r in enumerate(by_pf[:10], 1):
        lbl = r["label"][:45]
        pf = r.get("profit_factor", 0)
        pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
        lines.append(f"  {i:>2}. {lbl:<45}  PF {pf_s:>5} | "
                     f"N {r['n']:>3} | WR {r['win_rate_pct']:>5.1f}% | "
                     f"Kelly {r['kelly_fraction']:>+7.3f}")
    lines.append("```"); lines.append("")

    # Recommendation
    if qualifying:
        best_kelly = qualifying[0]
        lines.append("## Recommended Production Filter")
        lines.append("")
        base_k = baseline.get("kelly_fraction", 0)
        uplift = best_kelly.get("kelly_fraction", 0) - base_k
        pct_lift = (uplift / base_k * 100) if base_k > 0 else 0
        lines.append(f"- **Filter stack:** `{best_kelly['label']}`")
        lines.append(f"- **N:** {best_kelly['n']} signals (from {len(rows)} baseline, "
                     f"keeps {best_kelly['n']/len(rows)*100:.0f}%)")
        lines.append(f"- **WR:** {best_kelly['win_rate_pct']}% "
                     f"(baseline {baseline['win_rate_pct']}%)")
        lines.append(f"- **PF:** {best_kelly['profit_factor']} "
                     f"(baseline {baseline['profit_factor']})")
        lines.append(f"- **Kelly:** {best_kelly['kelly_fraction']:+.3f} "
                     f"(baseline {base_k:+.3f}, **uplift {uplift:+.3f} = {pct_lift:+.0f}%**)")
        lines.append(f"- **Avg PnL/trade:** {best_kelly['avg_pnl_pct']:+.2f}%")
        lines.append(f"- **+100% hit rate:** {best_kelly.get('t100_hit_rate_pct',0):.1f}%")
        lines.append("")
        qk_pct = min(best_kelly['kelly_fraction'] / 4, 0.10) * 100
        lines.append(f"- **Quarter-Kelly position size:** ~{qk_pct:.1f}% of capital per trade")
        lines.append("")
        lines.append("## Important Cautions")
        lines.append("")
        lines.append(f"- Backtest includes look-ahead in quintile calibration (standard limitation)")
        lines.append(f"- 5-year window covers 2022 bear + 2023-25 rally — future regimes may differ")
        lines.append(f"- Signal count of ~{best_kelly['n']/5:.0f}/year across ~500 tickers = "
                     "realistic alert density")
        lines.append(f"- Each +1 filter drops sample size — don't add more than shown above even "
                     "if edge looks higher")
        lines.append("")

    lines.append("## Files")
    lines.append("")
    lines.append("- `filter_stack_all_combinations.csv` — all 64 filter combinations")
    lines.append("- `filter_stack_ranked.csv` — ranked by Kelly with N >= min_n")
    lines.append("")

    out_path.write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Filter stack analysis on MC weekly trades.csv")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output dir for analysis files. Defaults to --csv's parent dir.")
    ap.add_argument("--min-n", type=int, default=100,
                    help="Minimum signals in bucket to qualify (default 100)")
    args = ap.parse_args()

    if not args.csv.exists():
        print(f"ERROR: trades.csv not found at {args.csv}")
        print(f"Run bt_marketcipher_weekly.py first, or pass --csv <path>.")
        return 1

    print(f"Loading {args.csv}...")
    with open(args.csv) as f:
        rows = list(csv.DictReader(f))
    print(f"  {len(rows)} rows loaded")

    # v8.2 (Patch 3): if --out-dir not provided, derive from csv parent
    # so running on /tmp/backtest_mc_daily/trades.csv writes analysis
    # files to /tmp/backtest_mc_daily, not /tmp/backtest_mc_weekly.
    if args.out_dir is None:
        args.out_dir = args.csv.parent

    baseline = _stats(rows, "BASELINE")
    print(f"  baseline: WR {baseline['win_rate_pct']}%, "
          f"PF {baseline['profit_factor']}, Kelly {baseline['kelly_fraction']}")

    print(f"\nRunning {2**len(FILTERS)} filter combinations...")
    results = run_all_combinations(rows)
    print(f"  {len(results)} combinations computed")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    all_path = args.out_dir / "filter_stack_all_combinations.csv"
    write_all_combinations_csv(results, all_path)
    print(f"  wrote {all_path}")

    ranked_path = args.out_dir / "filter_stack_ranked.csv"
    write_ranked_csv(results, ranked_path, args.min_n)
    print(f"  wrote {ranked_path}")

    report_path = args.out_dir / "filter_stack_report.md"
    write_report(results, baseline, rows, report_path, args.min_n)
    print(f"  wrote {report_path}")

    print()
    print(f"=== Quick summary ===")
    qualifying = [r for r in results if r.get("n", 0) >= args.min_n]
    qualifying.sort(key=_kelly_key, reverse=True)
    if qualifying:
        print(f"Best filter stack (N >= {args.min_n}):")
        top = qualifying[0]
        print(f"  {top['label']}")
        print(f"  N={top['n']}  WR {top['win_rate_pct']}%  "
              f"PF {top['profit_factor']}  Kelly {top['kelly_fraction']:+.3f}  "
              f"(baseline Kelly {baseline['kelly_fraction']:+.3f})")
    print()
    print(f"Full report: cat {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
