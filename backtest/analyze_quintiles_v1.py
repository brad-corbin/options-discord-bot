#!/usr/bin/env python3
"""
analyze_quintiles_v1.py — Indicator quintile analysis for bt_resolution_study_v2 trades.csv
════════════════════════════════════════════════════════════════════════════════════════════

The prior session claimed MACD hist Q5 hurts bulls (-6 WR) and "Diamond" setups
(EMA+MACD near zero, flipping) are the edge. This analyzer verifies those claims
at 572K scale by binning each indicator into quintiles per scoring system + tier +
direction.

Five indicators analyzed (already in trades.csv):
  - ind_ema_diff_pct    (EMA5 vs EMA21 gap in %)
  - ind_macd_hist       (MACD histogram as % of close)
  - ind_rsi             (0-100)
  - ind_wt2             (Wavetrend secondary line)
  - ind_adx             (trend strength)

THREE analyses:
  1. Plain quintiles — WR per indicator quintile per combo
  2. CB-aligned quintiles — same but only on CB-aligned signals
     (bull+below_cb, bear+above_cb). Answers: does MACD edge hold when we've
     ALREADY filtered for CB alignment, or does CB dominate?
  3. Cross-indicator combos — Diamond setup check:
     EMA near zero AND MACD near zero (both Q2-Q3) as a specific gate

USAGE:
    cd /opt/render/project/src
    python backtest/analyze_quintiles_v1.py

READS:  /tmp/backtest_resolution/trades.csv (or $BACKTEST_DIR/trades.csv)
WRITES: /tmp/backtest_resolution/
        summary_quintiles.csv         — plain quintile WR per indicator
        summary_quintiles_cb.csv      — CB-aligned quintiles only
        summary_diamond.csv           — Diamond setup (EMA+MACD both near zero) WR
        report_quintiles.md           — narrative summary

Runs in under 2 minutes.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("analyze_quintiles")

IN_DIR = Path(os.environ.get("BACKTEST_DIR", "/tmp/backtest_resolution"))
TRADES_CSV = IN_DIR / "trades.csv"
OUT_DIR = IN_DIR


INDICATORS = [
    ("ind_ema_diff_pct", "EMA diff %"),
    ("ind_macd_hist",    "MACD hist"),
    ("ind_rsi",          "RSI"),
    ("ind_wt2",          "WaveTrend 2"),
    ("ind_adx",          "ADX"),
]


@dataclass
class Row:
    """Minimal subset of trades.csv needed for quintile analysis."""
    scoring_system: str
    resolution_min: int
    tier: int
    direction: str
    bucket: str
    win_headline: bool
    move_signed_pct: float
    cb_side: str
    pb_state: str
    ind_ema_diff_pct: float
    ind_macd_hist: float
    ind_rsi: float
    ind_wt2: float
    ind_adx: float


def _parse_bool(v):
    if v is None or v == "":
        return False
    return str(v).lower() in ("true", "1")


def _parse_float(v, default=0.0):
    try:
        return float(v) if v != "" else default
    except (ValueError, TypeError):
        return default


def _parse_int(v, default=0):
    try:
        return int(float(v)) if v != "" else default
    except (ValueError, TypeError):
        return default


def load_trades():
    if not TRADES_CSV.exists():
        log.error(f"trades.csv not found at {TRADES_CSV}")
        sys.exit(1)

    log.info(f"Loading {TRADES_CSV}...")
    rows: list[Row] = []
    with open(TRADES_CSV) as f:
        rdr = csv.DictReader(f)
        fieldnames = rdr.fieldnames or []
        for col in ("ind_ema_diff_pct", "ind_macd_hist", "ind_rsi", "ind_wt2", "ind_adx"):
            if col not in fieldnames:
                log.error(f"trades.csv missing column '{col}' — required for quintile analysis")
                sys.exit(1)

        for r in rdr:
            try:
                rows.append(Row(
                    scoring_system=r["scoring_system"],
                    resolution_min=_parse_int(r["resolution_min"]),
                    tier=_parse_int(r["tier"]),
                    direction=r["direction"],
                    bucket=r["bucket"],
                    win_headline=_parse_bool(r["win_headline"]),
                    move_signed_pct=_parse_float(r["move_signed_pct"]),
                    cb_side=r.get("cb_side", "n/a"),
                    pb_state=r.get("pb_state", "no_box"),
                    ind_ema_diff_pct=_parse_float(r["ind_ema_diff_pct"]),
                    ind_macd_hist=_parse_float(r["ind_macd_hist"]),
                    ind_rsi=_parse_float(r["ind_rsi"]),
                    ind_wt2=_parse_float(r["ind_wt2"]),
                    ind_adx=_parse_float(r["ind_adx"]),
                ))
            except (ValueError, KeyError) as e:
                log.debug(f"Skip malformed row: {e}")
                continue

    log.info(f"Loaded {len(rows):,} trades")
    return rows


def _compute_quintile_boundaries(values):
    """Return quintile cut points [Q20, Q40, Q60, Q80] for a list of numbers."""
    if not values:
        return [0, 0, 0, 0]
    sv = sorted(values)
    n = len(sv)
    return [
        sv[int(n * 0.2)],
        sv[int(n * 0.4)],
        sv[int(n * 0.6)],
        sv[int(n * 0.8)],
    ]


def _assign_quintile(value, boundaries):
    """Return 'Q1'..'Q5' for a value given [Q20, Q40, Q60, Q80] boundaries."""
    if value < boundaries[0]: return "Q1"
    if value < boundaries[1]: return "Q2"
    if value < boundaries[2]: return "Q3"
    if value < boundaries[3]: return "Q4"
    return "Q5"


def _stats(subset):
    n = len(subset)
    if n == 0:
        return (0, 0.0, 0.0)
    wins = sum(1 for r in subset if r.bucket == "full_win")
    wr = 100 * wins / n
    avg = sum(r.move_signed_pct for r in subset) / n
    return (n, wr, avg)


def _baseline_wr(rows, scoring, resolution, tier, direction):
    subset = [r for r in rows
              if r.scoring_system == scoring
              and r.resolution_min == resolution
              and r.tier == tier
              and r.direction == direction]
    if not subset:
        return 0.0
    wins = sum(1 for r in subset if r.bucket == "full_win")
    return 100 * wins / len(subset)


# ═══════════════════════════════════════════════════════════
# ANALYSIS 1: PLAIN QUINTILES
# ═══════════════════════════════════════════════════════════

def analyze_plain_quintiles(rows, out_path):
    """WR per indicator quintile, grouped by scoring × resolution × tier × direction.

    Quintile boundaries are computed PER COMBO so the split is fair — each tier/
    direction has its own indicator distribution.
    """
    log.info("Computing plain quintiles...")

    results = []
    combos = defaultdict(list)
    for r in rows:
        key = (r.scoring_system, r.resolution_min, r.tier, r.direction)
        combos[key].append(r)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "indicator", "quintile", "lower_bound", "upper_bound",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])

        for (scoring, resolution, tier, direction), subset in sorted(combos.items()):
            if len(subset) < 100:
                continue
            baseline = _baseline_wr(rows, scoring, resolution, tier, direction)

            for col, label in INDICATORS:
                values = [getattr(r, col) for r in subset]
                bounds = _compute_quintile_boundaries(values)

                by_q = defaultdict(list)
                for r in subset:
                    q = _assign_quintile(getattr(r, col), bounds)
                    by_q[q].append(r)

                # Write bounds markers for reference
                q_bounds = {
                    "Q1": (min(values), bounds[0]),
                    "Q2": (bounds[0], bounds[1]),
                    "Q3": (bounds[1], bounds[2]),
                    "Q4": (bounds[2], bounds[3]),
                    "Q5": (bounds[3], max(values)),
                }

                for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                    qset = by_q.get(q, [])
                    if not qset:
                        continue
                    n, wr, avg = _stats(qset)
                    lo, hi = q_bounds[q]
                    w.writerow([scoring, resolution, tier, direction,
                                col, q, round(lo, 4), round(hi, 4),
                                n, round(wr, 1), round(avg, 3),
                                round(wr - baseline, 1)])

    log.info(f"Plain quintiles → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 2: CB-ALIGNED QUINTILES
# ═══════════════════════════════════════════════════════════

def analyze_cb_aligned_quintiles(rows, out_path):
    """Same as plain quintiles but filtered to CB-aligned in_box signals only.

    Answers: given a CB-aligned setup already, does indicator quintile still matter?
    Tests whether the MACD/EMA edge is independent of CB side or a proxy for it.
    """
    log.info("Computing CB-aligned quintiles...")

    # Filter: in_box AND CB-aligned (bull+below_cb OR bear+above_cb, also at_cb)
    aligned = [r for r in rows if r.pb_state == "in_box" and (
        (r.direction == "bull" and r.cb_side in ("below_cb", "at_cb")) or
        (r.direction == "bear" and r.cb_side in ("above_cb", "at_cb"))
    )]
    log.info(f"CB-aligned in_box subset: {len(aligned):,} trades")

    combos = defaultdict(list)
    for r in aligned:
        key = (r.scoring_system, r.resolution_min, r.tier, r.direction)
        combos[key].append(r)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "indicator", "quintile", "lower_bound", "upper_bound",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_aligned_baseline_wr_pct"])

        for (scoring, resolution, tier, direction), subset in sorted(combos.items()):
            if len(subset) < 100:
                continue

            # Aligned baseline = WR of all CB-aligned in_box trades for this combo
            aligned_wins = sum(1 for r in subset if r.bucket == "full_win")
            aligned_baseline = 100 * aligned_wins / len(subset)

            for col, label in INDICATORS:
                values = [getattr(r, col) for r in subset]
                bounds = _compute_quintile_boundaries(values)

                by_q = defaultdict(list)
                for r in subset:
                    q = _assign_quintile(getattr(r, col), bounds)
                    by_q[q].append(r)

                q_bounds = {
                    "Q1": (min(values), bounds[0]),
                    "Q2": (bounds[0], bounds[1]),
                    "Q3": (bounds[1], bounds[2]),
                    "Q4": (bounds[2], bounds[3]),
                    "Q5": (bounds[3], max(values)),
                }

                for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                    qset = by_q.get(q, [])
                    if not qset:
                        continue
                    n, wr, avg = _stats(qset)
                    lo, hi = q_bounds[q]
                    w.writerow([scoring, resolution, tier, direction,
                                col, q, round(lo, 4), round(hi, 4),
                                n, round(wr, 1), round(avg, 3),
                                round(wr - aligned_baseline, 1)])

    log.info(f"CB-aligned quintiles → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 3: DIAMOND SETUP
# ═══════════════════════════════════════════════════════════

def analyze_diamond_setup(rows, out_path):
    """Diamond setup = EMA diff near zero AND MACD hist near zero, both flipping.

    Prior session claimed this is the pinescript's signature high-WR pattern.
    Defined operationally as: EMA in Q2-Q4 AND MACD in Q2-Q4 (middle 60% of
    distribution for both, the 'center' that isn't either extreme).

    Report 4 cells per combo:
      - Diamond (both Q2-Q4)
      - EMA-only extreme (EMA Q1/Q5, MACD Q2-Q4)
      - MACD-only extreme (EMA Q2-Q4, MACD Q1/Q5)
      - Both extreme (EMA Q1/Q5, MACD Q1/Q5)
    """
    log.info("Computing Diamond setup analysis...")

    combos = defaultdict(list)
    for r in rows:
        key = (r.scoring_system, r.resolution_min, r.tier, r.direction)
        combos[key].append(r)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "setup_type", "n_trades", "wr_pct", "avg_move_pct",
                    "vs_baseline_wr_pct"])

        for (scoring, resolution, tier, direction), subset in sorted(combos.items()):
            if len(subset) < 200:
                continue
            baseline = _baseline_wr(rows, scoring, resolution, tier, direction)

            ema_vals = [r.ind_ema_diff_pct for r in subset]
            macd_vals = [r.ind_macd_hist for r in subset]
            ema_b = _compute_quintile_boundaries(ema_vals)
            macd_b = _compute_quintile_boundaries(macd_vals)

            buckets = {"diamond": [], "ema_extreme": [], "macd_extreme": [], "both_extreme": []}

            for r in subset:
                ema_q = _assign_quintile(r.ind_ema_diff_pct, ema_b)
                macd_q = _assign_quintile(r.ind_macd_hist, macd_b)
                ema_center = ema_q in ("Q2", "Q3", "Q4")
                macd_center = macd_q in ("Q2", "Q3", "Q4")

                if ema_center and macd_center:
                    buckets["diamond"].append(r)
                elif ema_center and not macd_center:
                    buckets["macd_extreme"].append(r)
                elif not ema_center and macd_center:
                    buckets["ema_extreme"].append(r)
                else:
                    buckets["both_extreme"].append(r)

            for setup_type in ("diamond", "ema_extreme", "macd_extreme", "both_extreme"):
                b = buckets[setup_type]
                if not b:
                    continue
                n, wr, avg = _stats(b)
                w.writerow([scoring, resolution, tier, direction,
                            setup_type, n, round(wr, 1), round(avg, 3),
                            round(wr - baseline, 1)])

    log.info(f"Diamond setup → {out_path}")


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def write_report(rows, out_path):
    md = f"""# Quintile Analysis Report

Trade count: {len(rows):,} signals analyzed.

## What this tests

The prior session claimed:
- MACD hist Q5 (highly positive) hurts bulls by -6 WR points — momentum exhausted
- EMA near zero (Q3) wins +6 WR — Diamond setup, true turning point
- Extreme quintiles are contrarian tells

This analyzer verifies those claims at 572K scale, THREE ways:
1. Plain quintiles (did MACD/EMA edge hold?)
2. CB-aligned quintiles (do these edges survive AFTER CB filter?)
3. Diamond setup (both EMA + MACD center, vs either extreme)

## Headline findings (active_scanner T2 15m — primary setup)

### MACD histogram quintiles (plain)

"""
    # Primary setup analysis
    primary = [r for r in rows
               if r.scoring_system == "active_scanner"
               and r.resolution_min == 15
               and r.tier == 2]
    for direction in ("bull", "bear"):
        sub = [r for r in primary if r.direction == direction]
        if not sub:
            continue
        baseline = _baseline_wr(rows, "active_scanner", 15, 2, direction)
        md += f"**{direction.upper()} (baseline {baseline:.1f}%, n={len(sub)}):**\n"
        vals = [r.ind_macd_hist for r in sub]
        bounds = _compute_quintile_boundaries(vals)
        q_labels = {
            "Q1": f"< {bounds[0]:.4f}",
            "Q2": f"{bounds[0]:.4f} → {bounds[1]:.4f}",
            "Q3": f"{bounds[1]:.4f} → {bounds[2]:.4f}",
            "Q4": f"{bounds[2]:.4f} → {bounds[3]:.4f}",
            "Q5": f"> {bounds[3]:.4f}",
        }
        by_q = defaultdict(list)
        for r in sub:
            by_q[_assign_quintile(r.ind_macd_hist, bounds)].append(r)
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            qs = by_q.get(q, [])
            if not qs:
                continue
            n, wr, avg = _stats(qs)
            delta = wr - baseline
            mark = " ← filter-on" if delta >= 3 else (" ← filter-out" if delta <= -3 else "")
            md += f"- {q} ({q_labels[q]}): n={n}, WR={wr:.1f}% ({delta:+.1f}){mark}\n"
        md += "\n"

    md += "### EMA diff % quintiles (plain)\n\n"
    for direction in ("bull", "bear"):
        sub = [r for r in primary if r.direction == direction]
        if not sub:
            continue
        baseline = _baseline_wr(rows, "active_scanner", 15, 2, direction)
        md += f"**{direction.upper()} (baseline {baseline:.1f}%):**\n"
        vals = [r.ind_ema_diff_pct for r in sub]
        bounds = _compute_quintile_boundaries(vals)
        q_labels = {
            "Q1": f"< {bounds[0]:.4f}",
            "Q2": f"{bounds[0]:.4f} → {bounds[1]:.4f}",
            "Q3": f"{bounds[1]:.4f} → {bounds[2]:.4f}",
            "Q4": f"{bounds[2]:.4f} → {bounds[3]:.4f}",
            "Q5": f"> {bounds[3]:.4f}",
        }
        by_q = defaultdict(list)
        for r in sub:
            by_q[_assign_quintile(r.ind_ema_diff_pct, bounds)].append(r)
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            qs = by_q.get(q, [])
            if not qs:
                continue
            n, wr, avg = _stats(qs)
            delta = wr - baseline
            mark = " ← filter-on" if delta >= 3 else (" ← filter-out" if delta <= -3 else "")
            md += f"- {q} ({q_labels[q]}): n={n}, WR={wr:.1f}% ({delta:+.1f}){mark}\n"
        md += "\n"

    md += "### Diamond setup (EMA + MACD both center vs extreme)\n\n"
    for direction in ("bull", "bear"):
        sub = [r for r in primary if r.direction == direction]
        if not sub:
            continue
        baseline = _baseline_wr(rows, "active_scanner", 15, 2, direction)
        md += f"**{direction.upper()} (baseline {baseline:.1f}%):**\n"
        ema_vals = [r.ind_ema_diff_pct for r in sub]
        macd_vals = [r.ind_macd_hist for r in sub]
        ema_b = _compute_quintile_boundaries(ema_vals)
        macd_b = _compute_quintile_boundaries(macd_vals)
        buckets = {"diamond": [], "ema_extreme": [], "macd_extreme": [], "both_extreme": []}
        for r in sub:
            eq = _assign_quintile(r.ind_ema_diff_pct, ema_b)
            mq = _assign_quintile(r.ind_macd_hist, macd_b)
            ec = eq in ("Q2", "Q3", "Q4")
            mc = mq in ("Q2", "Q3", "Q4")
            if ec and mc: buckets["diamond"].append(r)
            elif ec: buckets["macd_extreme"].append(r)
            elif mc: buckets["ema_extreme"].append(r)
            else: buckets["both_extreme"].append(r)
        for st in ("diamond", "ema_extreme", "macd_extreme", "both_extreme"):
            b = buckets[st]
            if not b: continue
            n, wr, avg = _stats(b)
            delta = wr - baseline
            mark = " ← filter-on" if delta >= 3 else (" ← filter-out" if delta <= -3 else "")
            md += f"- `{st}`: n={n}, WR={wr:.1f}% ({delta:+.1f}){mark}\n"
        md += "\n"

    md += """## Interpretation guide

- **Q1** = lowest 20% of values (most negative / smallest)
- **Q5** = highest 20% of values (most positive / largest)
- **Q3** = middle 20% (the "center" — where trends are turning, not extending)

Prior session claimed MACD Q5 hurts bulls. If that holds, you'll see Q5 with negative vs_baseline.
Prior session claimed Diamond setup wins. If that holds, `diamond` row has positive vs_baseline.

## Files

- `summary_quintiles.csv` — Plain quintile WR per indicator per combo. Full grid.
- `summary_quintiles_cb.csv` — CB-aligned subset only. Tests if edges survive CB filter.
- `summary_diamond.csv` — Diamond vs extreme setup WR per combo.

## Key question to verify

**If Diamond setup shows +3 or more WR vs baseline AND MACD Q5 shows -3 or more WR**, the prior
session's indicator edge holds at 572K scale. Add to gate scoring.

**If CB-aligned quintiles show much smaller swings than plain quintiles**, it means the CB
filter already captures most of the edge and indicator gates are redundant. Don't add.

**If neither Diamond nor MACD Q5 shows meaningful edge**, the prior session saw noise. Drop.

— Not financial advice —
"""
    Path(out_path).write_text(md)
    log.info(f"Report → {out_path}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    rows = load_trades()
    if not rows:
        log.error("No rows loaded")
        sys.exit(1)

    analyze_plain_quintiles(rows, OUT_DIR / "summary_quintiles.csv")
    analyze_cb_aligned_quintiles(rows, OUT_DIR / "summary_quintiles_cb.csv")
    analyze_diamond_setup(rows, OUT_DIR / "summary_diamond.csv")
    write_report(rows, OUT_DIR / "report_quintiles.md")

    log.info(f"DONE. New files in {OUT_DIR}:")
    for fn in ("report_quintiles.md", "summary_quintiles.csv",
               "summary_quintiles_cb.csv", "summary_diamond.csv"):
        p = OUT_DIR / fn
        if p.exists():
            log.info(f"  {fn}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
