#!/usr/bin/env python3
"""
analyze_overlays_v1.py — Deep overlay analysis of bt_resolution_study_v2 trades.csv
═════════════════════════════════════════════════════════════════════════════════════

Reads the trades.csv from bt_resolution_study_v2.py (which now includes Potter
Box, CB side, credit spread, and 1-hour S/R overlay columns) and produces
summary CSVs that answer:

1. POTTER BOX STATE GATING
   Does filtering signals by Potter Box state (in_box / above_roof / below_floor /
   post_box / no_box) improve WR? Which state carries edge?

2. CB SIDE GATING (the big one from earlier analysis)
   When Potter Box is in_box, does CB side (above_cb / below_cb / at_cb)
   meaningfully improve WR? Your 172K pinescript backtest showed below_cb
   T2 Bull at 73% WR vs 55% above_cb — a 17-point lift. Does the same pattern
   hold at this scale and across active_scanner + pinescript?

3. POTTER BOX × CB SIDE × ACTIVE SCANNER INTERACTION
   Your specific question: "Can we figure out the Potter Box Rule in relation
   to the CB when gating with Active Scanner?"

4. CREDIT SPREAD ANALYSIS
   When in_box, how do credit spreads ($2.50 and $5.00 wide) at Potter Box
   boundaries perform? Is it a viable "funding leg" for the debit spread?
   Your double-win hypothesis.

5. WAVE LABEL GATING
   Does signal WR differ when fired on 'established' vs 'weakening' vs
   'breakout_probable' vs 'breakout_imminent' box waves?

6. 1-HOUR S/R PROXIMITY
   Two methods (fractal + pivot) tracking distance to nearest level.
   Does proximity to a hourly level improve/hurt WR? Which side matters
   (above for bulls = room to run; below for bulls = support nearby)?

USAGE (Render shell):
    cd /opt/render/project/src
    python backtest/analyze_overlays_v1.py

READS:  /tmp/backtest_resolution/trades.csv
WRITES: /tmp/backtest_resolution/
        summary_potter_box.csv
        summary_cb_side.csv
        summary_wave_label.csv
        summary_credit_spreads.csv
        summary_sr_hourly.csv
        report_overlays.md

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
log = logging.getLogger("analyze_overlays")

IN_DIR = Path(os.environ.get("BACKTEST_DIR", "/tmp/backtest_resolution"))
TRADES_CSV = IN_DIR / "trades.csv"
OUT_DIR = IN_DIR


@dataclass
class Row:
    """Minimal subset of trades.csv columns needed for overlay analysis."""
    ticker: str
    resolution_min: int
    scoring_system: str
    tier: int
    direction: str
    bucket: str
    win_headline: bool
    move_signed_pct: float
    regime_trend: str
    # Potter Box overlay
    pb_state: str
    pb_floor: float
    pb_roof: float
    pb_midpoint: float
    pb_wave_label: str
    # CB side
    cb_side: str
    cb_distance_pct: float
    # Credit spreads
    credit_short_strike: float
    credit_25_bucket: str
    credit_25_win: bool
    credit_50_bucket: str
    credit_50_win: bool
    # 1-hour S/R
    sr_h_fractal_above: float
    sr_h_fractal_below: float
    sr_h_fractal_dist_above_pct: float
    sr_h_fractal_dist_below_pct: float
    sr_h_pivot_above: float
    sr_h_pivot_below: float
    sr_h_pivot_dist_above_pct: float
    sr_h_pivot_dist_below_pct: float


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
    overlay_missing_count = 0
    with open(TRADES_CSV) as f:
        rdr = csv.DictReader(f)
        # Check if overlay columns exist
        fieldnames = rdr.fieldnames or []
        has_pb = "pb_state" in fieldnames
        has_sr = "sr_h_fractal_above" in fieldnames
        if not has_pb:
            log.error("trades.csv does not have Potter Box columns — you need to re-run "
                      "with bt_resolution_study_v2.py (not v1) for overlay analysis.")
            sys.exit(1)
        if not has_sr:
            log.warning("trades.csv does not have 1h S/R columns — S/R analysis will be skipped.")

        for r in rdr:
            try:
                rows.append(Row(
                    ticker=r["ticker"],
                    resolution_min=_parse_int(r["resolution_min"]),
                    scoring_system=r["scoring_system"],
                    tier=_parse_int(r["tier"]),
                    direction=r["direction"],
                    bucket=r["bucket"],
                    win_headline=_parse_bool(r["win_headline"]),
                    move_signed_pct=_parse_float(r["move_signed_pct"]),
                    regime_trend=r["regime_trend"],
                    pb_state=r.get("pb_state", "no_box"),
                    pb_floor=_parse_float(r.get("pb_floor", 0)),
                    pb_roof=_parse_float(r.get("pb_roof", 0)),
                    pb_midpoint=_parse_float(r.get("pb_midpoint", 0)),
                    pb_wave_label=r.get("pb_wave_label", "none"),
                    cb_side=r.get("cb_side", "n/a"),
                    cb_distance_pct=_parse_float(r.get("cb_distance_pct", 0)),
                    credit_short_strike=_parse_float(r.get("credit_short_strike", 0)),
                    credit_25_bucket=r.get("credit_25_bucket", "n/a"),
                    credit_25_win=_parse_bool(r.get("credit_25_win", False)),
                    credit_50_bucket=r.get("credit_50_bucket", "n/a"),
                    credit_50_win=_parse_bool(r.get("credit_50_win", False)),
                    sr_h_fractal_above=_parse_float(r.get("sr_h_fractal_above", 0)),
                    sr_h_fractal_below=_parse_float(r.get("sr_h_fractal_below", 0)),
                    sr_h_fractal_dist_above_pct=_parse_float(r.get("sr_h_fractal_dist_above_pct", 999), 999.0),
                    sr_h_fractal_dist_below_pct=_parse_float(r.get("sr_h_fractal_dist_below_pct", 999), 999.0),
                    sr_h_pivot_above=_parse_float(r.get("sr_h_pivot_above", 0)),
                    sr_h_pivot_below=_parse_float(r.get("sr_h_pivot_below", 0)),
                    sr_h_pivot_dist_above_pct=_parse_float(r.get("sr_h_pivot_dist_above_pct", 999), 999.0),
                    sr_h_pivot_dist_below_pct=_parse_float(r.get("sr_h_pivot_dist_below_pct", 999), 999.0),
                ))
            except (ValueError, KeyError) as e:
                log.debug(f"Skip malformed row: {e}")
                continue

    log.info(f"Loaded {len(rows):,} trades")
    return rows


def _stats(subset):
    if not subset:
        return (0, 0, 0.0, 0.0)
    wins = sum(1 for r in subset if r.bucket == "full_win")
    partials = sum(1 for r in subset if r.bucket == "partial")
    avg = sum(r.move_signed_pct for r in subset) / len(subset)
    return (len(subset), wins, 100*wins/len(subset), avg)


def _baseline_wr(rows, scoring, resolution, tier, direction):
    """Compute baseline WR for (scoring × resolution × tier × direction)."""
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
# ANALYSIS 1: POTTER BOX STATE
# ═══════════════════════════════════════════════════════════

def analyze_potter_box(rows, out_path):
    """WR by Potter Box state, faceted by scoring × resolution × tier × direction."""
    log.info("Analyzing Potter Box state...")

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "pb_state",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        baseline = _baseline_wr(rows, scoring, resolution, tier, direction)
                        by_state = defaultdict(list)
                        for r in rows:
                            if (r.scoring_system == scoring and r.resolution_min == resolution
                                    and r.tier == tier and r.direction == direction):
                                by_state[r.pb_state].append(r)
                        for state in ("in_box", "above_roof", "below_floor", "post_box", "no_box"):
                            subset = by_state.get(state, [])
                            if not subset:
                                continue
                            n, _, wr, avg = _stats(subset)
                            w.writerow([scoring, resolution, tier, direction, state,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

    log.info(f"Potter Box → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 2: CB SIDE (only when in_box)
# ═══════════════════════════════════════════════════════════

def analyze_cb_side(rows, out_path):
    """WR by CB side, only on in_box trades. Compared to in_box baseline
    (not overall baseline) since that's the relevant conditional."""
    log.info("Analyzing CB side (in_box trades only)...")

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "cb_side",
                    "n_trades", "wr_pct", "avg_move_pct",
                    "vs_in_box_baseline_wr_pct", "vs_overall_baseline_wr_pct"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        # Overall baseline
                        overall_baseline = _baseline_wr(rows, scoring, resolution, tier, direction)

                        # in_box baseline — WR of all in_box signals for this combo
                        in_box_subset = [r for r in rows
                                         if r.scoring_system == scoring
                                         and r.resolution_min == resolution
                                         and r.tier == tier
                                         and r.direction == direction
                                         and r.pb_state == "in_box"]
                        if not in_box_subset:
                            continue
                        ib_wins = sum(1 for r in in_box_subset if r.bucket == "full_win")
                        in_box_baseline = 100 * ib_wins / len(in_box_subset)

                        by_cb = defaultdict(list)
                        for r in in_box_subset:
                            by_cb[r.cb_side].append(r)

                        for side in ("below_cb", "above_cb", "at_cb"):
                            subset = by_cb.get(side, [])
                            if not subset:
                                continue
                            n, _, wr, avg = _stats(subset)
                            w.writerow([scoring, resolution, tier, direction, side,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - in_box_baseline, 1),
                                        round(wr - overall_baseline, 1)])

    log.info(f"CB side → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 3: WAVE LABEL (only when in_box)
# ═══════════════════════════════════════════════════════════

def analyze_wave_label(rows, out_path):
    """WR by wave label — established / weakening / breakout_probable / breakout_imminent."""
    log.info("Analyzing wave labels (in_box trades only)...")

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "wave_label",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        in_box_subset = [r for r in rows
                                         if r.scoring_system == scoring
                                         and r.resolution_min == resolution
                                         and r.tier == tier
                                         and r.direction == direction
                                         and r.pb_state == "in_box"]
                        if not in_box_subset:
                            continue
                        ib_wins = sum(1 for r in in_box_subset if r.bucket == "full_win")
                        in_box_baseline = 100 * ib_wins / len(in_box_subset)

                        by_wl = defaultdict(list)
                        for r in in_box_subset:
                            by_wl[r.pb_wave_label].append(r)

                        for wl in ("established", "weakening", "breakout_probable",
                                   "breakout_imminent", "none"):
                            subset = by_wl.get(wl, [])
                            if not subset:
                                continue
                            n, _, wr, avg = _stats(subset)
                            w.writerow([scoring, resolution, tier, direction, wl,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - in_box_baseline, 1)])

    log.info(f"Wave label → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 4: CREDIT SPREADS (when in_box)
# ═══════════════════════════════════════════════════════════

def analyze_credit_spreads(rows, out_path):
    """For in_box trades, compare debit WR vs credit $2.50 WR vs credit $5.00 WR.

    Bull signal → bull put credit at floor.
    Bear signal → bear call credit at roof.

    Answers your double-win hypothesis: can a credit spread fund the debit?
    """
    log.info("Analyzing credit spreads (in_box trades only)...")

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "n_in_box", "debit_wr_pct",
                    "credit_25_wr_pct", "credit_50_wr_pct",
                    "credit_25_partial_pct", "credit_50_partial_pct",
                    "better_strategy"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        in_box_subset = [r for r in rows
                                         if r.scoring_system == scoring
                                         and r.resolution_min == resolution
                                         and r.tier == tier
                                         and r.direction == direction
                                         and r.pb_state == "in_box"
                                         and r.credit_short_strike > 0]
                        n = len(in_box_subset)
                        if n < 20:
                            continue

                        # Debit WR
                        debit_wins = sum(1 for r in in_box_subset if r.bucket == "full_win")
                        debit_wr = 100 * debit_wins / n

                        # Credit 25 WR
                        c25_wins = sum(1 for r in in_box_subset if r.credit_25_win)
                        c25_partials = sum(1 for r in in_box_subset if r.credit_25_bucket == "partial")
                        c25_wr = 100 * c25_wins / n
                        c25_partial_pct = 100 * c25_partials / n

                        # Credit 50 WR
                        c50_wins = sum(1 for r in in_box_subset if r.credit_50_win)
                        c50_partials = sum(1 for r in in_box_subset if r.credit_50_bucket == "partial")
                        c50_wr = 100 * c50_wins / n
                        c50_partial_pct = 100 * c50_partials / n

                        # Pick better strategy (≥3 points difference = meaningful)
                        best_credit = max(c25_wr, c50_wr)
                        if debit_wr >= best_credit + 3:
                            best = "debit"
                        elif best_credit >= debit_wr + 3:
                            best = f"credit_{'25' if c25_wr >= c50_wr else '50'}"
                        else:
                            best = "tie"

                        w.writerow([scoring, resolution, tier, direction, n,
                                    round(debit_wr, 1), round(c25_wr, 1), round(c50_wr, 1),
                                    round(c25_partial_pct, 1), round(c50_partial_pct, 1),
                                    best])

    log.info(f"Credit spreads → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 5: 1-HOUR S/R PROXIMITY
# ═══════════════════════════════════════════════════════════

def analyze_sr_hourly(rows, out_path):
    """WR by 1-hour S/R distance.

    For BULL signals: what matters is distance-to-above (resistance) — is there
    room to run? And distance-to-below (support) — is support nearby to fall back on?

    For BEAR signals: inverted. Distance-to-below matters (how far can it fall),
    and distance-to-above matters (resistance overhead).

    Bins distance into: <0.5%, 0.5-1%, 1-2%, 2-3%, 3%+
    Two methods: fractal and pivot.
    """
    log.info("Analyzing 1h S/R proximity...")

    # Check if S/R columns have data
    has_sr_data = any(r.sr_h_fractal_dist_above_pct < 999 or
                      r.sr_h_fractal_dist_below_pct < 999
                      for r in rows[:1000])
    if not has_sr_data:
        log.warning("No 1h S/R data found in trades.csv — skipping S/R analysis")
        return

    def _bin(d):
        if d >= 999: return "none"
        if d < 0.5: return "<0.5%"
        if d < 1.0: return "0.5-1%"
        if d < 2.0: return "1-2%"
        if d < 3.0: return "2-3%"
        return "3%+"

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "sr_method", "sr_side", "distance_bucket",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        baseline = _baseline_wr(rows, scoring, resolution, tier, direction)

                        subset = [r for r in rows
                                  if r.scoring_system == scoring
                                  and r.resolution_min == resolution
                                  and r.tier == tier
                                  and r.direction == direction]
                        if len(subset) < 100:
                            continue

                        # Fractal, above
                        buckets = defaultdict(list)
                        for r in subset:
                            buckets[_bin(r.sr_h_fractal_dist_above_pct)].append(r)
                        for b in ("<0.5%", "0.5-1%", "1-2%", "2-3%", "3%+", "none"):
                            s = buckets.get(b, [])
                            if not s:
                                continue
                            n, _, wr, avg = _stats(s)
                            w.writerow([scoring, resolution, tier, direction,
                                        "fractal", "above_spot", b,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

                        # Fractal, below
                        buckets = defaultdict(list)
                        for r in subset:
                            buckets[_bin(r.sr_h_fractal_dist_below_pct)].append(r)
                        for b in ("<0.5%", "0.5-1%", "1-2%", "2-3%", "3%+", "none"):
                            s = buckets.get(b, [])
                            if not s:
                                continue
                            n, _, wr, avg = _stats(s)
                            w.writerow([scoring, resolution, tier, direction,
                                        "fractal", "below_spot", b,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

                        # Pivot, above
                        buckets = defaultdict(list)
                        for r in subset:
                            buckets[_bin(r.sr_h_pivot_dist_above_pct)].append(r)
                        for b in ("<0.5%", "0.5-1%", "1-2%", "2-3%", "3%+", "none"):
                            s = buckets.get(b, [])
                            if not s:
                                continue
                            n, _, wr, avg = _stats(s)
                            w.writerow([scoring, resolution, tier, direction,
                                        "pivot", "above_spot", b,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

                        # Pivot, below
                        buckets = defaultdict(list)
                        for r in subset:
                            buckets[_bin(r.sr_h_pivot_dist_below_pct)].append(r)
                        for b in ("<0.5%", "0.5-1%", "1-2%", "2-3%", "3%+", "none"):
                            s = buckets.get(b, [])
                            if not s:
                                continue
                            n, _, wr, avg = _stats(s)
                            w.writerow([scoring, resolution, tier, direction,
                                        "pivot", "below_spot", b,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

    log.info(f"1h S/R → {out_path}")


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def write_report(rows, out_path):
    """Build a narrative report with headline findings from each analysis."""
    total = len(rows)

    md = f"""# Overlay Analysis Report

Trade count: {total:,} signals analyzed.

## Overlay columns analyzed

- **Potter Box state** (in_box / above_roof / below_floor / post_box / no_box)
- **CB side** (above_cb / below_cb / at_cb — only on in_box trades)
- **Wave label** (established / weakening / breakout_probable / breakout_imminent)
- **Credit spread outcomes** at Potter Box floor (bull) / roof (bear)
- **1-hour S/R distance** via fractal (order-3 swing) and pivot (recent hi/lo)

## Headline findings (15m active_scanner T2 — your primary trading setup)

### Potter Box gating
"""
    # Compute 15m active_scanner T2 stats per state per direction
    for direction in ("bull", "bear"):
        baseline = _baseline_wr(rows, "active_scanner", 15, 2, direction)
        md += f"\n**{direction.upper()} (baseline WR {baseline:.1f}%):**\n"

        for state in ("in_box", "above_roof", "below_floor", "no_box"):
            subset = [r for r in rows
                      if r.scoring_system == "active_scanner"
                      and r.resolution_min == 15
                      and r.tier == 2
                      and r.direction == direction
                      and r.pb_state == state]
            if not subset:
                continue
            n, _, wr, avg = _stats(subset)
            delta = wr - baseline
            marker = " ← filter-on candidate" if delta >= 5 else (" ← filter-out candidate" if delta <= -5 else "")
            md += f"- `{state}` n={n}: {wr:.1f}% WR ({delta:+.1f} vs baseline, avg {avg:+.2f}%){marker}\n"

    md += "\n### CB side gating (active_scanner T2 15m in_box only)\n"
    for direction in ("bull", "bear"):
        in_box = [r for r in rows
                  if r.scoring_system == "active_scanner"
                  and r.resolution_min == 15
                  and r.tier == 2
                  and r.direction == direction
                  and r.pb_state == "in_box"]
        if not in_box:
            continue
        ib_wins = sum(1 for r in in_box if r.bucket == "full_win")
        ib_wr = 100 * ib_wins / len(in_box)
        md += f"\n**{direction.upper()} (in_box baseline {ib_wr:.1f}%, n={len(in_box)}):**\n"
        for side in ("below_cb", "above_cb", "at_cb"):
            subset = [r for r in in_box if r.cb_side == side]
            if not subset:
                continue
            n, _, wr, avg = _stats(subset)
            delta = wr - ib_wr
            marker = " ← STRONG filter-on" if delta >= 10 else (" ← STRONG filter-out" if delta <= -10 else "")
            md += f"- `{side}` n={n}: {wr:.1f}% WR ({delta:+.1f} vs in_box baseline, avg {avg:+.2f}%){marker}\n"

    md += "\n### Credit spread viability (active_scanner T2 15m in_box)\n"
    for direction in ("bull", "bear"):
        in_box = [r for r in rows
                  if r.scoring_system == "active_scanner"
                  and r.resolution_min == 15
                  and r.tier == 2
                  and r.direction == direction
                  and r.pb_state == "in_box"
                  and r.credit_short_strike > 0]
        if len(in_box) < 20:
            continue

        debit_wins = sum(1 for r in in_box if r.bucket == "full_win")
        c25_wins = sum(1 for r in in_box if r.credit_25_win)
        c50_wins = sum(1 for r in in_box if r.credit_50_win)

        n = len(in_box)
        md += f"\n**{direction.upper()} (n={n}):**\n"
        md += f"- Debit spread WR: {100*debit_wins/n:.1f}%\n"
        md += f"- Credit spread $2.50-wide WR: {100*c25_wins/n:.1f}%\n"
        md += f"- Credit spread $5.00-wide WR: {100*c50_wins/n:.1f}%\n"

        # Double-win feasibility
        both_win_25 = sum(1 for r in in_box if r.bucket == "full_win" and r.credit_25_win)
        both_win_50 = sum(1 for r in in_box if r.bucket == "full_win" and r.credit_50_win)
        md += f"- Both-win (debit + credit $2.50): {100*both_win_25/n:.1f}% of signals\n"
        md += f"- Both-win (debit + credit $5.00): {100*both_win_50/n:.1f}% of signals\n"

    md += """

## Files

- `summary_potter_box.csv` — WR by Potter Box state, full grid
- `summary_cb_side.csv` — WR by CB side on in_box trades, full grid
- `summary_wave_label.csv` — WR by wave label (box maturity)
- `summary_credit_spreads.csv` — Debit vs credit WR comparison per combo
- `summary_sr_hourly.csv` — 1h S/R proximity WR by distance bucket

## Interpretation guide

**vs_baseline_wr_pct column** in each CSV tells you:
- **≥ +5** = filter-on candidate. Gating on this condition adds edge.
- **≤ -5** = filter-out candidate. Gating against this condition improves WR.
- **Between** = noise / inconclusive at this sample size.

**For credit spread columns**: if `better_strategy` = "credit_25" or "credit_50",
the credit spread at that width has higher WR than debit. That means:
- You *could* funding-trade: run debit AND credit simultaneously
- Or you could switch tickers that underperform on debit but work well on credit
  to credit-only income mode

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

    analyze_potter_box(rows, OUT_DIR / "summary_potter_box.csv")
    analyze_cb_side(rows, OUT_DIR / "summary_cb_side.csv")
    analyze_wave_label(rows, OUT_DIR / "summary_wave_label.csv")
    analyze_credit_spreads(rows, OUT_DIR / "summary_credit_spreads.csv")
    analyze_sr_hourly(rows, OUT_DIR / "summary_sr_hourly.csv")
    write_report(rows, OUT_DIR / "report_overlays.md")

    log.info(f"DONE. New files in {OUT_DIR}:")
    for fn in ("report_overlays.md", "summary_potter_box.csv", "summary_cb_side.csv",
               "summary_wave_label.csv", "summary_credit_spreads.csv",
               "summary_sr_hourly.csv"):
        p = OUT_DIR / fn
        if p.exists():
            log.info(f"  {fn}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
