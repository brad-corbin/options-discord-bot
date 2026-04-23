#!/usr/bin/env python3
"""
bt_winner_profile.py — Winner vs Loser Profile Analysis
══════════════════════════════════════════════════════════

Reads a trades.csv and asks: what's systematically different between
winning and losing signals at the MOMENT OF ENTRY? Unlike the filter-
stack analyzer (which tests AND combinations of binary flags), this tool
profiles the continuous features and flags the hidden commonalities.

Uses ONLY signal-time features — deliberately excludes outcome fields
like pnl_pct, hold_days, mfe/mae, exit_reason, target-hit times, price
snapshots. No look-ahead.

USAGE
    python backtest/bt_winner_profile.py --csv /tmp/backtest_mc_daily/trades.csv
    python backtest/bt_winner_profile.py --csv /var/data/backtest_archive/mc_daily_latest/trades.csv

OUTPUTS (same dir as --csv unless --out-dir set):
    winner_profile_separation.csv   every numeric feature, win/loss means
    winner_profile_quintiles.csv    quintile WR curves for top features
    winner_profile_categorical.csv  categorical WR per value
    winner_profile_report.md        executive summary

The report is the main output. Open the CSVs for drill-down.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════
# FEATURE DEFINITIONS — signal-time ONLY
# ═══════════════════════════════════════════════════════════

def _f(v: Any) -> Optional[float]:
    try:
        if v == "" or v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _t(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


# Raw numeric features (read directly from CSV). These are all known at
# signal time — no outcome data allowed.
RAW_NUMERIC_FEATURES = [
    "wt1_at_signal", "wt2_at_signal", "wt1_prev", "wt2_prev",
    "rsi_weekly", "stoch_rsi_weekly", "macd_hist_weekly",
    "weekly_200sma", "weekly_atr",
    "weekly_bar_open", "weekly_bar_close",
    "confluence_score",
    "nearest_potter_floor", "pct_from_potter_floor", "potter_floors_in_history",
    "entry_price", "stop_initial",
]


# Derived numeric features — computed from raw columns. Each takes the
# raw row dict and returns float or None. None rows are skipped for that
# feature but kept for others.
def _derived_pct_from_200sma(r: Dict[str, Any]) -> Optional[float]:
    sma = _f(r.get("weekly_200sma"))
    cl = _f(r.get("weekly_bar_close"))
    if sma is None or cl is None or sma <= 0:
        return None
    return (cl - sma) / sma * 100.0


def _derived_atr_pct_of_price(r: Dict[str, Any]) -> Optional[float]:
    atr = _f(r.get("weekly_atr"))
    ep = _f(r.get("entry_price"))
    if atr is None or ep is None or ep <= 0:
        return None
    return atr / ep * 100.0


def _derived_wt1_delta(r: Dict[str, Any]) -> Optional[float]:
    cur = _f(r.get("wt1_at_signal")); prev = _f(r.get("wt1_prev"))
    if cur is None or prev is None:
        return None
    return cur - prev


def _derived_wt_spread(r: Dict[str, Any]) -> Optional[float]:
    wt1 = _f(r.get("wt1_at_signal")); wt2 = _f(r.get("wt2_at_signal"))
    if wt1 is None or wt2 is None:
        return None
    return wt1 - wt2  # how far wt1 is above wt2 at the cross — cross strength


def _derived_bar_body_pct(r: Dict[str, Any]) -> Optional[float]:
    o = _f(r.get("weekly_bar_open")); c = _f(r.get("weekly_bar_close"))
    if o is None or c is None or o <= 0:
        return None
    return (c - o) / o * 100.0


def _derived_stop_risk_pct(r: Dict[str, Any]) -> Optional[float]:
    ep = _f(r.get("entry_price")); st = _f(r.get("stop_initial"))
    if ep is None or st is None or ep <= 0 or st <= 0:
        return None
    return (ep - st) / ep * 100.0  # % risk on stop


def _derived_abs_pct_from_pf(r: Dict[str, Any]) -> Optional[float]:
    pct = _f(r.get("pct_from_potter_floor"))
    if pct is None:
        return None
    return abs(pct)


DERIVED_NUMERIC_FEATURES: Dict[str, Callable[[Dict[str, Any]], Optional[float]]] = {
    "pct_from_200sma":     _derived_pct_from_200sma,
    "atr_pct_of_price":    _derived_atr_pct_of_price,
    "wt1_delta":           _derived_wt1_delta,
    "wt_spread":           _derived_wt_spread,
    "bar_body_pct":        _derived_bar_body_pct,
    "stop_risk_pct":       _derived_stop_risk_pct,
    "abs_pct_from_pf":     _derived_abs_pct_from_pf,
}


CATEGORICAL_FEATURES = [
    "regime_trend", "regime_vol",
    "rsi_oversold", "stoch_rsi_oversold", "macd_hist_negative",
    "above_weekly_200sma", "bar_green",
    "is_confirmed_ticker", "is_removed_ticker",
    "near_potter_floor_2pct", "near_potter_floor_5pct",
]


# ═══════════════════════════════════════════════════════════
# STATS PRIMITIVES
# ═══════════════════════════════════════════════════════════

def _percentiles(vals: List[float]) -> Dict[str, float]:
    if not vals:
        return {"mean": 0.0, "median": 0.0, "p25": 0.0, "p75": 0.0,
                "min": 0.0, "max": 0.0, "std": 0.0, "n": 0}
    s = sorted(vals)
    n = len(s)
    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n if n > 1 else 0.0
    return {
        "mean":   round(mean, 4),
        "median": round(s[n // 2], 4),
        "p25":    round(s[n // 4], 4),
        "p75":    round(s[3 * n // 4], 4),
        "min":    round(s[0], 4),
        "max":    round(s[-1], 4),
        "std":    round(math.sqrt(var), 4),
        "n":      n,
    }


def _pooled_std(wins: List[float], losses: List[float]) -> float:
    """Pooled standard deviation across two samples."""
    if not wins or not losses:
        return 0.0
    n1, n2 = len(wins), len(losses)
    if n1 < 2 or n2 < 2:
        return 0.0
    v1 = statistics.variance(wins); v2 = statistics.variance(losses)
    pooled = ((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2)
    return math.sqrt(pooled) if pooled > 0 else 0.0


def _trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, float]:
    """Same headline stats used by the filter-stack analyzer."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr_pct": 0.0, "pf": 0.0, "kelly": 0.0, "avg_pnl": 0.0}
    wins = [t for t in trades if _t(t.get("win"))]
    losses = [t for t in trades if not _t(t.get("win"))]
    pnls = [_f(t.get("pnl_pct")) or 0.0 for t in trades]
    wpnl = [_f(t.get("pnl_pct")) or 0.0 for t in wins]
    lpnl = [_f(t.get("pnl_pct")) or 0.0 for t in losses]
    gw = sum(wpnl); gl = abs(sum(lpnl))
    wr = len(wins) / n
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    aw = (gw / len(wins)) if wins else 0.0
    al = (gl / len(losses)) if losses else 0.0
    R = (aw / al) if al > 0 else 0.0
    kelly = (wr - (1 - wr) / R) if R > 0 else 0.0
    return {
        "n": n, "wr_pct": round(wr * 100, 2),
        "pf": round(pf, 3) if pf != float("inf") else float("inf"),
        "kelly": round(kelly, 4),
        "avg_pnl": round(sum(pnls) / n, 3),
        "avg_win_pct": round(aw, 3),
        "avg_loss_pct": round(-al, 3),
    }


# ═══════════════════════════════════════════════════════════
# FEATURE ANALYSIS
# ═══════════════════════════════════════════════════════════

def _extract_feature(rows: List[Dict[str, Any]], name: str
                     ) -> Tuple[List[Tuple[float, bool]], int]:
    """Return [(value, is_win), ...] for rows where feature is non-null.
    Second return = count of skipped rows (null feature)."""
    out: List[Tuple[float, bool]] = []
    skipped = 0

    if name in DERIVED_NUMERIC_FEATURES:
        fn = DERIVED_NUMERIC_FEATURES[name]
        for r in rows:
            v = fn(r)
            if v is None:
                skipped += 1
                continue
            out.append((v, _t(r.get("win"))))
    else:
        for r in rows:
            v = _f(r.get(name))
            if v is None:
                skipped += 1
                continue
            out.append((v, _t(r.get("win"))))
    return out, skipped


def separation_analysis(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each numeric feature, compute winner-vs-loser separation."""
    features = RAW_NUMERIC_FEATURES + list(DERIVED_NUMERIC_FEATURES.keys())
    out: List[Dict[str, Any]] = []

    for name in features:
        extracted, skipped = _extract_feature(rows, name)
        if not extracted:
            continue
        wins = [v for v, w in extracted if w]
        losses = [v for v, w in extracted if not w]
        if len(wins) < 5 or len(losses) < 5:
            continue

        wstats = _percentiles(wins)
        lstats = _percentiles(losses)
        pooled = _pooled_std(wins, losses)
        mean_diff = wstats["mean"] - lstats["mean"]
        sep = (mean_diff / pooled) if pooled > 0 else 0.0

        out.append({
            "feature": name,
            "n_total": len(extracted), "n_skipped": skipped,
            "n_wins": len(wins), "n_losses": len(losses),
            "win_mean":   wstats["mean"],
            "win_median": wstats["median"],
            "win_p25":    wstats["p25"], "win_p75": wstats["p75"],
            "loss_mean":   lstats["mean"],
            "loss_median": lstats["median"],
            "loss_p25":    lstats["p25"], "loss_p75": lstats["p75"],
            "mean_diff":  round(mean_diff, 4),
            "pooled_std": round(pooled, 4),
            "separation": round(sep, 4),
            "abs_separation": round(abs(sep), 4),
        })

    out.sort(key=lambda d: d["abs_separation"], reverse=True)
    return out


def quintile_analysis(rows: List[Dict[str, Any]], feature: str
                      ) -> Optional[Dict[str, Any]]:
    """Bucket rows into quintiles by feature, compute stats per quintile."""
    extracted, _ = _extract_feature(rows, feature)
    if len(extracted) < 100:
        return None
    vals = [v for v, _ in extracted]
    vals_sorted = sorted(vals)
    n = len(vals_sorted)

    bounds = [
        vals_sorted[n // 5],
        vals_sorted[2 * n // 5],
        vals_sorted[3 * n // 5],
        vals_sorted[4 * n // 5],
    ]

    # Index the trades by their value bucket
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "Q1": [], "Q2": [], "Q3": [], "Q4": [], "Q5": [],
    }
    for r in rows:
        if feature in DERIVED_NUMERIC_FEATURES:
            v = DERIVED_NUMERIC_FEATURES[feature](r)
        else:
            v = _f(r.get(feature))
        if v is None:
            continue
        if v <= bounds[0]:      buckets["Q1"].append(r)
        elif v <= bounds[1]:    buckets["Q2"].append(r)
        elif v <= bounds[2]:    buckets["Q3"].append(r)
        elif v <= bounds[3]:    buckets["Q4"].append(r)
        else:                    buckets["Q5"].append(r)

    q_stats: Dict[str, Dict[str, float]] = {}
    for q, trades in buckets.items():
        q_stats[q] = _trade_stats(trades)

    # Monotonic detection: is WR strictly rising or falling across Q1→Q5?
    wrs = [q_stats[f"Q{i}"].get("wr_pct", 0.0) for i in (1, 2, 3, 4, 5)]
    rising = all(wrs[i] <= wrs[i + 1] + 0.1 for i in range(4))   # loose monotone
    falling = all(wrs[i] >= wrs[i + 1] - 0.1 for i in range(4))
    strict_rising = all(wrs[i] < wrs[i + 1] for i in range(4))
    strict_falling = all(wrs[i] > wrs[i + 1] for i in range(4))
    wr_range = max(wrs) - min(wrs)

    return {
        "feature": feature,
        "bounds": bounds,
        "q_stats": q_stats,
        "wrs": wrs,
        "wr_range_pct": round(wr_range, 2),
        "monotone_rising": rising,
        "monotone_falling": falling,
        "strict_rising": strict_rising,
        "strict_falling": strict_falling,
    }


def categorical_analysis(rows: List[Dict[str, Any]]
                          ) -> List[Dict[str, Any]]:
    """For each categorical feature, WR and PF per category value."""
    baseline = _trade_stats(rows)
    base_wr = baseline["wr_pct"]
    out: List[Dict[str, Any]] = []
    for feat in CATEGORICAL_FEATURES:
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            val = str(r.get(feat, "")).strip()
            if val in ("True", "1", "yes"):
                val = "TRUE"
            elif val in ("False", "0", "no", ""):
                val = "FALSE"
            buckets[val].append(r)
        for val, trades in buckets.items():
            if len(trades) < 20:
                continue
            s = _trade_stats(trades)
            out.append({
                "feature": feat, "value": val,
                "n": s["n"], "wr_pct": s["wr_pct"],
                "pf": s["pf"] if s["pf"] != float("inf") else "inf",
                "kelly": s["kelly"],
                "d_wr_vs_baseline_pct": round(s["wr_pct"] - base_wr, 2),
                "avg_pnl_pct": s["avg_pnl"],
            })
    # Sort by |ΔWR|
    out.sort(key=lambda d: abs(d["d_wr_vs_baseline_pct"]), reverse=True)
    return out


# ═══════════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════════

def write_separation_csv(sep: List[Dict[str, Any]], path: Path) -> None:
    if not sep:
        path.write_text("feature,n_wins,n_losses,separation,mean_diff\n")
        return
    keys = ["feature", "n_total", "n_wins", "n_losses",
            "win_mean", "win_median", "win_p25", "win_p75",
            "loss_mean", "loss_median", "loss_p25", "loss_p75",
            "mean_diff", "pooled_std", "separation", "abs_separation"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for s in sep:
            w.writerow([s.get(k, "") for k in keys])


def write_quintiles_csv(quintile_data: List[Dict[str, Any]], path: Path) -> None:
    rows = []
    for qd in quintile_data:
        feature = qd["feature"]
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            s = qd["q_stats"][q]
            rows.append({
                "feature": feature, "quintile": q,
                "n": s["n"], "wr_pct": s["wr_pct"],
                "pf": s["pf"] if s["pf"] != float("inf") else "inf",
                "kelly": s["kelly"],
                "avg_pnl_pct": s["avg_pnl"],
            })
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feature", "quintile", "n", "wr_pct", "pf", "kelly", "avg_pnl_pct"])
        for r in rows:
            w.writerow([r[k] for k in ["feature", "quintile", "n", "wr_pct", "pf",
                                        "kelly", "avg_pnl_pct"]])


def write_categorical_csv(cat: List[Dict[str, Any]], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feature", "value", "n", "wr_pct", "pf", "kelly",
                    "d_wr_vs_baseline_pct", "avg_pnl_pct"])
        for c in cat:
            w.writerow([c["feature"], c["value"], c["n"], c["wr_pct"],
                        c["pf"], c["kelly"],
                        c["d_wr_vs_baseline_pct"], c["avg_pnl_pct"]])


def write_report(rows: List[Dict[str, Any]],
                 sep: List[Dict[str, Any]],
                 cat: List[Dict[str, Any]],
                 quintile_data: List[Dict[str, Any]],
                 out_path: Path) -> None:
    baseline = _trade_stats(rows)
    pf_bl = baseline["pf"] if baseline["pf"] != float("inf") else "inf"
    lines: List[str] = []
    lines.append("# Winner Profile Analysis — what separates wins from losses")
    lines.append("")
    lines.append(f"- Input: **{len(rows)}** signals")
    lines.append(f"- Baseline: **WR {baseline['wr_pct']}%, PF {pf_bl}, "
                 f"Kelly {baseline['kelly']:+.3f}**, "
                 f"avg PnL {baseline['avg_pnl']:+.2f}%")
    lines.append("")
    lines.append("*Method: for each signal-time feature, compute mean on winners "
                 "vs losers, measure separation in standard-deviation units. "
                 "Rank by |separation|. Outcome fields (pnl, mae, mfe, exit, "
                 "hold days, target hit times) EXCLUDED — no look-ahead.*")
    lines.append("")

    # Top separating continuous features
    lines.append("## Top Separating Features (win_mean vs loss_mean)")
    lines.append("")
    lines.append("*Separation = (win_mean − loss_mean) ÷ pooled_std. "
                 "Positive = winners have HIGHER value. Negative = winners LOWER.*")
    lines.append("")
    lines.append("```")
    lines.append(f"  {'Feature':<24} {'win_mean':>10} {'loss_mean':>10} "
                 f"{'mean_diff':>10} {'separation':>10}  Nwin/Nlos")
    lines.append(f"  {'-'*24} {'-'*10} {'-'*10} {'-'*10} {'-'*10}  ---------")
    for s in sep[:15]:
        lines.append(f"  {s['feature']:<24} {s['win_mean']:>+10.3f} "
                     f"{s['loss_mean']:>+10.3f} {s['mean_diff']:>+10.3f} "
                     f"{s['separation']:>+10.3f}  {s['n_wins']}/{s['n_losses']}")
    lines.append("```")
    lines.append("")
    lines.append("**How to read:** |separation| ≥ 0.2 is meaningful, ≥ 0.3 is strong, "
                 "≥ 0.5 is rare and valuable. Values < 0.1 are essentially noise.")
    lines.append("")

    # Quintile WR curves for top features
    lines.append("## Quintile WR Curves — top 5 separating features")
    lines.append("")
    lines.append("*Bucket signals into equal-size quintiles by feature value. "
                 "WR per quintile reveals where the edge is concentrated.*")
    lines.append("")
    for qd in quintile_data[:5]:
        feat = qd["feature"]
        wrs = qd["wrs"]
        wr_range = qd["wr_range_pct"]
        hint = ""
        if qd["strict_rising"]:  hint = " ← strictly monotone ↑"
        elif qd["strict_falling"]: hint = " ← strictly monotone ↓"
        elif qd["monotone_rising"]: hint = " ← rising trend"
        elif qd["monotone_falling"]: hint = " ← falling trend"
        lines.append(f"### `{feat}`{hint}")
        lines.append("")
        lines.append(f"- Quintile bounds: {[round(b, 3) for b in qd['bounds']]}")
        lines.append(f"- WR range across quintiles: **{wr_range:.1f} pct points**")
        lines.append("")
        lines.append("```")
        lines.append(f"  {'Q':<4} {'range':<22} {'N':>4} {'WR':>6} {'PF':>6} "
                     f"{'Kelly':>7} {'AvgPnL':>8}")
        lines.append(f"  {'-'*4} {'-'*22} {'-'*4} {'-'*6} {'-'*6} {'-'*7} {'-'*8}")
        bounds = qd["bounds"]
        range_strs = [
            f"≤ {bounds[0]:+.2f}",
            f"{bounds[0]:+.2f} → {bounds[1]:+.2f}",
            f"{bounds[1]:+.2f} → {bounds[2]:+.2f}",
            f"{bounds[2]:+.2f} → {bounds[3]:+.2f}",
            f"> {bounds[3]:+.2f}",
        ]
        for i, q in enumerate(("Q1", "Q2", "Q3", "Q4", "Q5")):
            s = qd["q_stats"][q]
            pf = s["pf"] if s["pf"] != float("inf") else "inf"
            pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
            lines.append(f"  {q:<4} {range_strs[i]:<22} {s['n']:>4} "
                         f"{s['wr_pct']:>5.1f}% {pf_s:>6} "
                         f"{s['kelly']:>+.3f} {s['avg_pnl']:>+7.2f}%")
        lines.append("```")
        lines.append("")

    # Categorical
    lines.append("## Categorical Features by Win Rate")
    lines.append("")
    lines.append("*Each category's WR vs baseline WR. Big |ΔWR| = category is "
                 "real signal. Small |ΔWR| = noise.*")
    lines.append("")
    lines.append("```")
    lines.append(f"  {'Feature':<26} {'Value':<10} {'N':>5} {'WR':>6} "
                 f"{'PF':>6} {'ΔWR':>7} {'Kelly':>7}")
    lines.append(f"  {'-'*26} {'-'*10} {'-'*5} {'-'*6} {'-'*6} {'-'*7} {'-'*7}")
    for c in cat[:30]:
        pf = c["pf"]; pf_s = f"{pf}" if pf == "inf" else f"{pf:.2f}"
        sign = "+" if c["d_wr_vs_baseline_pct"] >= 0 else ""
        lines.append(f"  {c['feature']:<26} {c['value']:<10} {c['n']:>5} "
                     f"{c['wr_pct']:>5.1f}% {pf_s:>6} "
                     f"{sign}{c['d_wr_vs_baseline_pct']:>5.1f}pp  "
                     f"{c['kelly']:>+.3f}")
    lines.append("```")
    lines.append("")

    # Recommendation synthesis
    lines.append("## Synthesis — where's the hidden edge?")
    lines.append("")
    top_sep = [s for s in sep if s["abs_separation"] >= 0.2][:5]
    if top_sep:
        lines.append("**Continuous features with real separation (|sep| ≥ 0.2):**")
        lines.append("")
        for s in top_sep:
            direction = "higher" if s["separation"] > 0 else "lower"
            lines.append(f"- `{s['feature']}` — winners have **{direction}** values "
                         f"({s['win_mean']:+.2f} vs {s['loss_mean']:+.2f}, "
                         f"sep {s['separation']:+.2f})")
    else:
        lines.append("**No continuous feature showed |separation| ≥ 0.2.** "
                     "The raw MC signal may not have exploitable continuous edges "
                     "beyond what the binary filters already capture. Consider "
                     "adding new features (fib level, sector, day of week) via "
                     "backtest patch.")
    lines.append("")

    best_cat = [c for c in cat if abs(c["d_wr_vs_baseline_pct"]) >= 3 and c["n"] >= 100][:5]
    if best_cat:
        lines.append("**Categorical cuts with real WR edge (|ΔWR| ≥ 3pp, N ≥ 100):**")
        lines.append("")
        for c in best_cat:
            sign = "+" if c["d_wr_vs_baseline_pct"] >= 0 else ""
            lines.append(f"- `{c['feature']}={c['value']}` — "
                         f"WR {c['wr_pct']:.1f}% ({sign}{c['d_wr_vs_baseline_pct']:.1f}pp), "
                         f"N={c['n']}, Kelly {c['kelly']:+.3f}")
    lines.append("")

    # Deployment-ready rule hint
    best_quintile_rules: List[str] = []
    for qd in quintile_data[:5]:
        wrs = qd["wrs"]
        best_q_idx = wrs.index(max(wrs))
        best_q = f"Q{best_q_idx + 1}"
        best_s = qd["q_stats"][best_q]
        if best_s["n"] >= 100 and best_s["wr_pct"] > baseline["wr_pct"] + 3:
            bounds = qd["bounds"]
            if best_q_idx == 0:
                rng = f"≤ {bounds[0]:.2f}"
            elif best_q_idx == 4:
                rng = f"> {bounds[3]:.2f}"
            else:
                rng = f"in [{bounds[best_q_idx-1]:.2f}, {bounds[best_q_idx]:.2f}]"
            best_quintile_rules.append(
                f"  - `{qd['feature']}` {rng} → WR {best_s['wr_pct']:.1f}%, "
                f"N={best_s['n']}, Kelly {best_s['kelly']:+.3f}"
            )

    if best_quintile_rules:
        lines.append("**Single-feature quintile rules worth testing (WR > baseline+3pp, N ≥ 100):**")
        lines.append("")
        lines += best_quintile_rules
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## What this analysis does NOT see")
    lines.append("")
    lines.append("Features not currently in trades.csv that may hold edge — "
                 "would require backtest re-run to extract:")
    lines.append("")
    lines.append("- **Fib retracement level** at signal (38.2 / 50 / 61.8 / 78.6)")
    lines.append("- **Distance from weekly 40-SMA** (proper 200-day trend proxy)")
    lines.append("- **Volume profile** — was the green dot on above-average volume?")
    lines.append("- **Sector/industry** — some sectors may mean-revert better than others")
    lines.append("- **Days since last earnings** — proximity effects")
    lines.append("- **VIX regime at signal** — distinct from bot's trend/vol classes")
    lines.append("- **Wick anatomy** — signal bar's lower wick as % of body (long lower "
                 "wick = rejection of lower prices = stronger turn)")
    lines.append("")
    lines.append("If this analysis shows no single-feature edge ≥ 0.2 separation, "
                 "adding fib + wick + volume features is the next research step "
                 "(single backtest patch, then re-run + re-analyze).")
    lines.append("")
    lines.append("## Files")
    lines.append("")
    lines.append("- `winner_profile_separation.csv` — full ranked feature separation table")
    lines.append("- `winner_profile_quintiles.csv` — quintile WR breakdown for top features")
    lines.append("- `winner_profile_categorical.csv` — categorical feature WR table")

    out_path.write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Winner-vs-loser profile analysis")
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output dir. Defaults to --csv's parent.")
    args = ap.parse_args()

    if not args.csv.exists():
        print(f"ERROR: not found: {args.csv}")
        return 1

    if args.out_dir is None:
        args.out_dir = args.csv.parent
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading {args.csv}...")
    with open(args.csv) as f:
        rows = list(csv.DictReader(f))
    print(f"  {len(rows)} rows")

    baseline = _trade_stats(rows)
    pf = baseline["pf"] if baseline["pf"] != float("inf") else "inf"
    print(f"  baseline: WR {baseline['wr_pct']}%, PF {pf}, "
          f"Kelly {baseline['kelly']:+.3f}")

    print(f"\nComputing feature separation...")
    sep = separation_analysis(rows)
    print(f"  {len(sep)} features analyzed")

    print(f"Computing quintile WR curves for top separating features...")
    quintile_data: List[Dict[str, Any]] = []
    for s in sep[:10]:  # top 10 by separation
        qd = quintile_analysis(rows, s["feature"])
        if qd:
            quintile_data.append(qd)
    print(f"  {len(quintile_data)} features have valid quintile curves")

    print(f"Computing categorical WR table...")
    cat = categorical_analysis(rows)
    print(f"  {len(cat)} category values analyzed")

    # Write artifacts
    sep_csv = args.out_dir / "winner_profile_separation.csv"
    qs_csv = args.out_dir / "winner_profile_quintiles.csv"
    cat_csv = args.out_dir / "winner_profile_categorical.csv"
    report = args.out_dir / "winner_profile_report.md"

    write_separation_csv(sep, sep_csv); print(f"  wrote {sep_csv}")
    write_quintiles_csv(quintile_data, qs_csv); print(f"  wrote {qs_csv}")
    write_categorical_csv(cat, cat_csv); print(f"  wrote {cat_csv}")
    write_report(rows, sep, cat, quintile_data, report)
    print(f"  wrote {report}")

    # Quick summary
    print()
    print("=" * 60)
    print("QUICK SUMMARY")
    print("=" * 60)
    if sep:
        print(f"\nTop 5 separating features:")
        for s in sep[:5]:
            arrow = "↑" if s["separation"] > 0 else "↓"
            print(f"  {s['feature']:<24}  sep {s['separation']:>+6.3f} {arrow}  "
                  f"(win mean {s['win_mean']:+.2f} vs loss mean {s['loss_mean']:+.2f})")

    print(f"\nFull report: cat {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
