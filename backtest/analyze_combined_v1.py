#!/usr/bin/env python3
"""
analyze_combined_v1.py — All edge analyses in one run
════════════════════════════════════════════════════════════════════════════════

Consolidates every analyzer we've built into a single script that reads the
bt_resolution_study_v3.py trades.csv and produces every summary CSV we need.

INCLUDED ANALYSES:
  1. Overlays      — Potter state, CB side, credit spreads, wave label, 1h S/R
                     (same as analyze_overlays_v1)
  2. Per-ticker    — WR per ticker/combo (same as analyze_trades_v1)
  3. Re-fire       — fires-per-week distribution, position WR, roll-up sim
  4. Quintiles     — indicator quintile WR, Diamond setup
                     (same as analyze_quintiles_v1)
  5. NEW: Bar expansion    — signal bar + surrounding 5m bars, WR by expansion ratio
  6. NEW: Wave A/B         — original vs corrected wave direction, which predicts WR
  7. NEW: Maturity filter  — WR by box maturity (early/mid/late/overdue)
  8. NEW: Setup intersection — raw → in_box → at_edge → wave_aligned → expansion stack
                     Per-ticker lift measurement

USAGE (Render shell):
    cd /opt/render/project/src
    python backtest/analyze_combined_v1.py

READS:  /tmp/backtest_resolution/trades.csv  (from bt_resolution_study_v3)
WRITES: /tmp/backtest_resolution/
        report_combined.md                    — narrative synthesis
        summary_per_ticker.csv                — WR per (ticker × combo)
        summary_potter_box.csv                — WR by PB state
        summary_cb_side.csv                   — WR by CB side (in_box only)
        summary_wave_label.csv                — WR by wave label
        summary_credit_spreads.csv            — debit vs credit WR
        summary_sr_hourly.csv                 — 1h S/R proximity WR
        summary_refire.csv                    — fires-per-week + position WR
        summary_rollup.csv                    — roll-up strategy comparison
        summary_quintiles.csv                 — indicator quintile WR
        summary_quintiles_cb.csv              — quintiles on CB-aligned only
        summary_diamond.csv                   — Diamond setup WR
        summary_bar_expansion.csv             — bar expansion WR bins
        summary_wave_ab.csv                   — wave A vs B direction A/B test
        summary_maturity.csv                  — WR by box maturity
        summary_setup_intersection.csv        — filter stack lift per ticker

Runs in ~3-5 minutes on 572K signals.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("analyze_combined")

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


# ═══════════════════════════════════════════════════════════
# DATA MODEL
# ═══════════════════════════════════════════════════════════

@dataclass
class Row:
    ticker: str
    resolution_min: int
    scoring_system: str
    tier: int
    direction: str
    signal_ts: int
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    move_signed_pct: float
    bucket: str
    win_headline: bool
    regime_trend: str
    # Indicators
    ind_ema_diff_pct: float
    ind_macd_hist: float
    ind_rsi: float
    ind_wt2: float
    ind_adx: float
    # Potter Box overlay
    pb_state: str
    pb_floor: float
    pb_roof: float
    pb_midpoint: float
    pb_wave_label: str
    cb_side: str
    cb_distance_pct: float
    # Credit spread
    credit_short_strike: float
    credit_25_bucket: str
    credit_25_win: bool
    credit_50_bucket: str
    credit_50_win: bool
    # 1h S/R
    sr_h_fractal_dist_above_pct: float
    sr_h_fractal_dist_below_pct: float
    sr_h_pivot_dist_above_pct: float
    sr_h_pivot_dist_below_pct: float
    # Bar expansion (NEW v3)
    bar_range_5m_t_minus_3_pct: float
    bar_range_5m_t_minus_2_pct: float
    bar_range_5m_t_minus_1_pct: float
    bar_range_5m_signal_pct: float
    bar_range_5m_t_plus_1_pct: float
    bar_range_5m_t_plus_2_pct: float
    avg_range_5m_prior_20_pct: float
    # Void + neighbor box (NEW v3)
    void_above_pct: float
    void_below_pct: float
    box_above_floor: float
    box_above_roof: float
    box_below_floor: float
    box_below_roof: float
    # Wave direction A/B (NEW v3)
    wave_dir_original: str
    wave_dir_corrected: str
    wave_roof_touches: int
    wave_floor_touches: int
    # Maturity (NEW v3)
    pb_maturity: str
    pb_maturity_ratio: float
    pb_duration_bars: int
    # Run + punchback (NEW v3)
    pb_run_pct: float
    pb_punchback: bool


def _pb(v):
    if v is None or v == "":
        return False
    return str(v).lower() in ("true", "1")


def _pf(v, d=0.0):
    try:
        return float(v) if v != "" else d
    except (ValueError, TypeError):
        return d


def _pi(v, d=0):
    try:
        return int(float(v)) if v != "" else d
    except (ValueError, TypeError):
        return d


def load_trades():
    if not TRADES_CSV.exists():
        log.error(f"trades.csv not found at {TRADES_CSV}")
        sys.exit(1)

    log.info(f"Loading {TRADES_CSV}...")
    rows: list[Row] = []

    required_v3 = [
        "bar_range_5m_signal_pct", "wave_dir_original", "wave_dir_corrected",
        "pb_maturity", "box_above_floor",
    ]

    with open(TRADES_CSV) as f:
        rdr = csv.DictReader(f)
        fieldnames = rdr.fieldnames or []
        missing = [c for c in required_v3 if c not in fieldnames]
        if missing:
            log.error(f"trades.csv missing v3 columns: {missing}")
            log.error("Re-run bt_resolution_study_v3.py to generate v3-schema trades.csv")
            sys.exit(1)

        for r in rdr:
            try:
                rows.append(Row(
                    ticker=r["ticker"],
                    resolution_min=_pi(r["resolution_min"]),
                    scoring_system=r["scoring_system"],
                    tier=_pi(r["tier"]),
                    direction=r["direction"],
                    signal_ts=_pi(r["signal_ts"]),
                    entry_ts=_pi(r["entry_ts"]),
                    entry_price=_pf(r["entry_price"]),
                    exit_ts=_pi(r["exit_ts"]),
                    exit_price=_pf(r["exit_price"]),
                    move_signed_pct=_pf(r["move_signed_pct"]),
                    bucket=r["bucket"],
                    win_headline=_pb(r["win_headline"]),
                    regime_trend=r["regime_trend"],
                    ind_ema_diff_pct=_pf(r["ind_ema_diff_pct"]),
                    ind_macd_hist=_pf(r["ind_macd_hist"]),
                    ind_rsi=_pf(r["ind_rsi"], 50.0),
                    ind_wt2=_pf(r["ind_wt2"]),
                    ind_adx=_pf(r["ind_adx"]),
                    pb_state=r.get("pb_state", "no_box"),
                    pb_floor=_pf(r.get("pb_floor", 0)),
                    pb_roof=_pf(r.get("pb_roof", 0)),
                    pb_midpoint=_pf(r.get("pb_midpoint", 0)),
                    pb_wave_label=r.get("pb_wave_label", "none"),
                    cb_side=r.get("cb_side", "n/a"),
                    cb_distance_pct=_pf(r.get("cb_distance_pct", 0)),
                    credit_short_strike=_pf(r.get("credit_short_strike", 0)),
                    credit_25_bucket=r.get("credit_25_bucket", "n/a"),
                    credit_25_win=_pb(r.get("credit_25_win", False)),
                    credit_50_bucket=r.get("credit_50_bucket", "n/a"),
                    credit_50_win=_pb(r.get("credit_50_win", False)),
                    sr_h_fractal_dist_above_pct=_pf(r.get("sr_h_fractal_dist_above_pct", 999), 999.0),
                    sr_h_fractal_dist_below_pct=_pf(r.get("sr_h_fractal_dist_below_pct", 999), 999.0),
                    sr_h_pivot_dist_above_pct=_pf(r.get("sr_h_pivot_dist_above_pct", 999), 999.0),
                    sr_h_pivot_dist_below_pct=_pf(r.get("sr_h_pivot_dist_below_pct", 999), 999.0),
                    bar_range_5m_t_minus_3_pct=_pf(r.get("bar_range_5m_t_minus_3_pct", 0)),
                    bar_range_5m_t_minus_2_pct=_pf(r.get("bar_range_5m_t_minus_2_pct", 0)),
                    bar_range_5m_t_minus_1_pct=_pf(r.get("bar_range_5m_t_minus_1_pct", 0)),
                    bar_range_5m_signal_pct=_pf(r.get("bar_range_5m_signal_pct", 0)),
                    bar_range_5m_t_plus_1_pct=_pf(r.get("bar_range_5m_t_plus_1_pct", 0)),
                    bar_range_5m_t_plus_2_pct=_pf(r.get("bar_range_5m_t_plus_2_pct", 0)),
                    avg_range_5m_prior_20_pct=_pf(r.get("avg_range_5m_prior_20_pct", 0)),
                    void_above_pct=_pf(r.get("void_above_pct", 0)),
                    void_below_pct=_pf(r.get("void_below_pct", 0)),
                    box_above_floor=_pf(r.get("box_above_floor", 0)),
                    box_above_roof=_pf(r.get("box_above_roof", 0)),
                    box_below_floor=_pf(r.get("box_below_floor", 0)),
                    box_below_roof=_pf(r.get("box_below_roof", 0)),
                    wave_dir_original=r.get("wave_dir_original", "none"),
                    wave_dir_corrected=r.get("wave_dir_corrected", "none"),
                    wave_roof_touches=_pi(r.get("wave_roof_touches", 0)),
                    wave_floor_touches=_pi(r.get("wave_floor_touches", 0)),
                    pb_maturity=r.get("pb_maturity", "none"),
                    pb_maturity_ratio=_pf(r.get("pb_maturity_ratio", 0)),
                    pb_duration_bars=_pi(r.get("pb_duration_bars", 0)),
                    pb_run_pct=_pf(r.get("pb_run_pct", 0)),
                    pb_punchback=_pb(r.get("pb_punchback", False)),
                ))
            except (ValueError, KeyError) as e:
                log.debug(f"Skip malformed row: {e}")
                continue

    log.info(f"Loaded {len(rows):,} trades")
    return rows


# ═══════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════

def _stats(subset):
    n = len(subset)
    if n == 0:
        return (0, 0, 0.0, 0.0)
    wins = sum(1 for r in subset if r.bucket == "full_win")
    partials = sum(1 for r in subset if r.bucket == "partial")
    wr = 100 * wins / n
    avg = sum(r.move_signed_pct for r in subset) / n
    return (n, wins, wr, avg)


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


def _quintile_bounds(values):
    if not values:
        return [0, 0, 0, 0]
    sv = sorted(values)
    n = len(sv)
    return [sv[int(n * 0.2)], sv[int(n * 0.4)],
            sv[int(n * 0.6)], sv[int(n * 0.8)]]


def _assign_q(value, bounds):
    if value < bounds[0]: return "Q1"
    if value < bounds[1]: return "Q2"
    if value < bounds[2]: return "Q3"
    if value < bounds[3]: return "Q4"
    return "Q5"


# ═══════════════════════════════════════════════════════════
# ANALYSIS 1: PER-TICKER
# ═══════════════════════════════════════════════════════════

def analyze_per_ticker(rows, out_path):
    log.info("Per-ticker...")
    g = defaultdict(list)
    for r in rows:
        g[(r.ticker, r.scoring_system, r.resolution_min, r.tier, r.direction)].append(r)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "scoring", "resolution_min", "tier", "direction",
                    "n_trades", "full_win", "partial", "full_loss",
                    "wr_pct", "wr_with_partials_pct", "avg_move_pct", "significance"])
        for key, ts in sorted(g.items()):
            ticker, scoring, resolution, tier, direction = key
            n = len(ts)
            if n == 0:
                continue
            fw = sum(1 for t in ts if t.bucket == "full_win")
            part = sum(1 for t in ts if t.bucket == "partial")
            fl = sum(1 for t in ts if t.bucket == "full_loss")
            avg = sum(t.move_signed_pct for t in ts) / n
            wr = 100 * fw / n
            wr_p = 100 * (fw + part) / n
            sig = "ok" if n >= 100 else ("small" if n >= 30 else "tiny")
            w.writerow([ticker, scoring, resolution, tier, direction, n,
                        fw, part, fl, round(wr, 1), round(wr_p, 1),
                        round(avg, 3), sig])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 2: POTTER BOX STATE
# ═══════════════════════════════════════════════════════════

def analyze_potter_box(rows, out_path):
    log.info("Potter Box state...")
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
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 3: CB SIDE
# ═══════════════════════════════════════════════════════════

def analyze_cb_side(rows, out_path):
    log.info("CB side (in_box only)...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "cb_side",
                    "n_trades", "wr_pct", "avg_move_pct",
                    "vs_in_box_baseline_wr_pct", "vs_overall_baseline_wr_pct"])
        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        overall = _baseline_wr(rows, scoring, resolution, tier, direction)
                        ib = [r for r in rows
                              if r.scoring_system == scoring and r.resolution_min == resolution
                              and r.tier == tier and r.direction == direction
                              and r.pb_state == "in_box"]
                        if not ib:
                            continue
                        ib_wr = 100 * sum(1 for r in ib if r.bucket == "full_win") / len(ib)
                        by = defaultdict(list)
                        for r in ib:
                            by[r.cb_side].append(r)
                        for side in ("below_cb", "above_cb", "at_cb"):
                            subset = by.get(side, [])
                            if not subset:
                                continue
                            n, _, wr, avg = _stats(subset)
                            w.writerow([scoring, resolution, tier, direction, side,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1),
                                        round(wr - overall, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 4: WAVE LABEL
# ═══════════════════════════════════════════════════════════

def analyze_wave_label(rows, out_path):
    log.info("Wave label...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "wave_label",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])
        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        ib = [r for r in rows
                              if r.scoring_system == scoring and r.resolution_min == resolution
                              and r.tier == tier and r.direction == direction
                              and r.pb_state == "in_box"]
                        if not ib:
                            continue
                        ib_wr = 100 * sum(1 for r in ib if r.bucket == "full_win") / len(ib)
                        by = defaultdict(list)
                        for r in ib:
                            by[r.pb_wave_label].append(r)
                        for wl in ("established", "weakening", "breakout_probable",
                                   "breakout_imminent", "none"):
                            subset = by.get(wl, [])
                            if not subset:
                                continue
                            n, _, wr, avg = _stats(subset)
                            w.writerow([scoring, resolution, tier, direction, wl,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 5: CREDIT SPREADS
# ═══════════════════════════════════════════════════════════

def analyze_credit_spreads(rows, out_path):
    log.info("Credit spreads...")
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
                        ib = [r for r in rows
                              if r.scoring_system == scoring and r.resolution_min == resolution
                              and r.tier == tier and r.direction == direction
                              and r.pb_state == "in_box" and r.credit_short_strike > 0]
                        n = len(ib)
                        if n < 20:
                            continue
                        dw = sum(1 for r in ib if r.bucket == "full_win")
                        c25 = sum(1 for r in ib if r.credit_25_win)
                        c50 = sum(1 for r in ib if r.credit_50_win)
                        c25_p = sum(1 for r in ib if r.credit_25_bucket == "partial")
                        c50_p = sum(1 for r in ib if r.credit_50_bucket == "partial")
                        debit_wr = 100 * dw / n
                        c25_wr = 100 * c25 / n
                        c50_wr = 100 * c50 / n
                        best_c = max(c25_wr, c50_wr)
                        if debit_wr >= best_c + 3:
                            best = "debit"
                        elif best_c >= debit_wr + 3:
                            best = f"credit_{'25' if c25_wr >= c50_wr else '50'}"
                        else:
                            best = "tie"
                        w.writerow([scoring, resolution, tier, direction, n,
                                    round(debit_wr, 1), round(c25_wr, 1), round(c50_wr, 1),
                                    round(100 * c25_p / n, 1), round(100 * c50_p / n, 1), best])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 6: 1-HOUR S/R
# ═══════════════════════════════════════════════════════════

def analyze_sr_hourly(rows, out_path):
    log.info("1h S/R...")
    has_sr = any(r.sr_h_fractal_dist_above_pct < 999 for r in rows[:1000])
    if not has_sr:
        log.warning("No S/R data; skipping")
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
                        sub = [r for r in rows
                               if r.scoring_system == scoring and r.resolution_min == resolution
                               and r.tier == tier and r.direction == direction]
                        if len(sub) < 100:
                            continue
                        for method, side_attr_above, side_attr_below in [
                            ("fractal", "sr_h_fractal_dist_above_pct", "sr_h_fractal_dist_below_pct"),
                            ("pivot", "sr_h_pivot_dist_above_pct", "sr_h_pivot_dist_below_pct"),
                        ]:
                            for side_name, attr in [("above_spot", side_attr_above),
                                                     ("below_spot", side_attr_below)]:
                                buckets = defaultdict(list)
                                for r in sub:
                                    buckets[_bin(getattr(r, attr))].append(r)
                                for b in ("<0.5%", "0.5-1%", "1-2%", "2-3%", "3%+", "none"):
                                    s = buckets.get(b, [])
                                    if not s:
                                        continue
                                    n, _, wr, avg = _stats(s)
                                    w.writerow([scoring, resolution, tier, direction,
                                                method, side_name, b,
                                                n, round(wr, 1), round(avg, 3),
                                                round(wr - baseline, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 7: RE-FIRE
# ═══════════════════════════════════════════════════════════

def analyze_refire(rows, out_path):
    log.info("Re-fire analysis...")
    wg = defaultdict(list)
    for r in rows:
        dt = datetime.fromtimestamp(r.signal_ts)
        iy, iw, _ = dt.isocalendar()
        wg[(r.ticker, r.scoring_system, r.resolution_min, r.tier, r.direction, iy, iw)].append(r)
    for k in wg:
        wg[k].sort(key=lambda x: x.signal_ts)

    dist = defaultdict(lambda: defaultdict(int))
    first_fire = defaultdict(lambda: defaultdict(list))
    pos_wr = defaultdict(lambda: defaultdict(list))
    for key, group in wg.items():
        _, scoring, resolution, tier, direction, _, _ = key
        if resolution != 15:
            continue
        total = len(group)
        bkt = str(total) if total <= 3 else "4+"
        dist[(scoring, tier, direction)][bkt] += 1
        first_fire[(scoring, tier, direction)][bkt].append(group[0])
        for pos, r in enumerate(group, start=1):
            pbkt = str(pos) if pos <= 3 else "4+"
            pos_wr[(scoring, tier, direction)][pbkt].append(r)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["analysis", "scoring", "tier", "direction", "bucket",
                    "n", "wr_pct", "avg_move_pct"])
        for (s, t, d), b in sorted(dist.items()):
            for bk in ("1", "2", "3", "4+"):
                n = b.get(bk, 0)
                if n == 0:
                    continue
                w.writerow(["fires_in_week_distribution", s, t, d, bk, n, "", ""])
        for (s, t, d), b in sorted(first_fire.items()):
            for bk in ("1", "2", "3", "4+"):
                sub = b.get(bk, [])
                if not sub:
                    continue
                wins = sum(1 for x in sub if x.bucket == "full_win")
                avg = sum(x.move_signed_pct for x in sub) / len(sub)
                w.writerow(["first_fire_given_total_fires", s, t, d, bk,
                            len(sub), round(100 * wins / len(sub), 1), round(avg, 3)])
        for (s, t, d), b in sorted(pos_wr.items()):
            for bk in ("1", "2", "3", "4+"):
                sub = b.get(bk, [])
                if not sub:
                    continue
                wins = sum(1 for x in sub if x.bucket == "full_win")
                avg = sum(x.move_signed_pct for x in sub) / len(sub)
                w.writerow(["fire_position_in_week", s, t, d, bk,
                            len(sub), round(100 * wins / len(sub), 1), round(avg, 3)])
    log.info(f"  → {out_path}")
    return wg


# ═══════════════════════════════════════════════════════════
# ANALYSIS 8: ROLL-UP
# ═══════════════════════════════════════════════════════════

def analyze_rollup(wg, out_path):
    log.info("Roll-up simulation...")
    results = defaultdict(list)
    for key, group in wg.items():
        _, scoring, resolution, tier, direction, _, _ = key
        if resolution != 15 or len(group) < 2:
            continue
        f1 = group[0]; f2 = group[1]
        if f2.entry_ts <= f1.entry_ts:
            continue

        pnl1 = f1.move_signed_pct
        pnl2 = f2.move_signed_pct
        hold_both = pnl1 + pnl2
        hold_both_win = (f1.bucket == "full_win" and f2.bucket == "full_win")

        if f1.entry_price > 0:
            if direction == "bull":
                rollup = (f2.exit_price - f1.entry_price) / f1.entry_price * 100
            else:
                rollup = -(f2.exit_price - f1.entry_price) / f1.entry_price * 100
            rollup_win = rollup >= -1.0
        else:
            rollup = 0.0
            rollup_win = False

        combo = (scoring, tier, direction)
        results[(combo, "hold_both")].append((hold_both, hold_both_win))
        results[(combo, "roll_up")].append((rollup, rollup_win))
        results[(combo, "fire1_only")].append((pnl1, f1.bucket == "full_win"))

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "tier", "direction", "strategy",
                    "n_paired_weeks", "wr_pct", "avg_pnl_pct", "avg_pnl_per_leg_pct"])
        by_combo = defaultdict(dict)
        for (combo, strat), data in results.items():
            by_combo[combo][strat] = data
        for combo in sorted(by_combo.keys()):
            s, t, d = combo
            for strat in ("fire1_only", "hold_both", "roll_up"):
                data = by_combo[combo].get(strat, [])
                if not data:
                    continue
                n = len(data)
                wins = sum(1 for _, wn in data if wn)
                wr = 100 * wins / n
                avg = sum(p for p, _ in data) / n
                per_leg = avg / 2 if strat == "hold_both" else avg
                w.writerow([s, t, d, strat, n, round(wr, 1), round(avg, 3), round(per_leg, 3)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 9: QUINTILES (plain + CB-aligned)
# ═══════════════════════════════════════════════════════════

def analyze_quintiles(rows, out_plain, out_cb):
    log.info("Indicator quintiles...")

    combos = defaultdict(list)
    for r in rows:
        key = (r.scoring_system, r.resolution_min, r.tier, r.direction)
        combos[key].append(r)

    with open(out_plain, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "indicator", "quintile", "lower_bound", "upper_bound",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])
        for (scoring, resolution, tier, direction), subset in sorted(combos.items()):
            if len(subset) < 100:
                continue
            baseline = _baseline_wr(rows, scoring, resolution, tier, direction)
            for col, _ in INDICATORS:
                values = [getattr(r, col) for r in subset]
                bounds = _quintile_bounds(values)
                by_q = defaultdict(list)
                for r in subset:
                    by_q[_assign_q(getattr(r, col), bounds)].append(r)
                qb = {
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
                    n, _, wr, avg = _stats(qset)
                    lo, hi = qb[q]
                    w.writerow([scoring, resolution, tier, direction,
                                col, q, round(lo, 4), round(hi, 4),
                                n, round(wr, 1), round(avg, 3),
                                round(wr - baseline, 1)])

    aligned = [r for r in rows if r.pb_state == "in_box" and (
        (r.direction == "bull" and r.cb_side in ("below_cb", "at_cb")) or
        (r.direction == "bear" and r.cb_side in ("above_cb", "at_cb"))
    )]
    log.info(f"  CB-aligned in_box subset: {len(aligned):,}")

    al_combos = defaultdict(list)
    for r in aligned:
        al_combos[(r.scoring_system, r.resolution_min, r.tier, r.direction)].append(r)

    with open(out_cb, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "indicator", "quintile", "lower_bound", "upper_bound",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_aligned_baseline_wr_pct"])
        for (scoring, resolution, tier, direction), subset in sorted(al_combos.items()):
            if len(subset) < 100:
                continue
            al_wins = sum(1 for r in subset if r.bucket == "full_win")
            al_wr = 100 * al_wins / len(subset)
            for col, _ in INDICATORS:
                values = [getattr(r, col) for r in subset]
                bounds = _quintile_bounds(values)
                by_q = defaultdict(list)
                for r in subset:
                    by_q[_assign_q(getattr(r, col), bounds)].append(r)
                qb = {
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
                    n, _, wr, avg = _stats(qset)
                    lo, hi = qb[q]
                    w.writerow([scoring, resolution, tier, direction,
                                col, q, round(lo, 4), round(hi, 4),
                                n, round(wr, 1), round(avg, 3),
                                round(wr - al_wr, 1)])
    log.info(f"  → {out_plain}, {out_cb}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 10: DIAMOND SETUP
# ═══════════════════════════════════════════════════════════

def analyze_diamond(rows, out_path):
    log.info("Diamond setup...")
    combos = defaultdict(list)
    for r in rows:
        combos[(r.scoring_system, r.resolution_min, r.tier, r.direction)].append(r)

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
            eb = _quintile_bounds(ema_vals)
            mb = _quintile_bounds(macd_vals)
            buckets = {"diamond": [], "ema_extreme": [], "macd_extreme": [], "both_extreme": []}
            for r in subset:
                eq = _assign_q(r.ind_ema_diff_pct, eb)
                mq = _assign_q(r.ind_macd_hist, mb)
                ec = eq in ("Q2", "Q3", "Q4")
                mc = mq in ("Q2", "Q3", "Q4")
                if ec and mc: buckets["diamond"].append(r)
                elif ec: buckets["macd_extreme"].append(r)
                elif mc: buckets["ema_extreme"].append(r)
                else: buckets["both_extreme"].append(r)
            for st in ("diamond", "ema_extreme", "macd_extreme", "both_extreme"):
                b = buckets[st]
                if not b:
                    continue
                n, _, wr, avg = _stats(b)
                w.writerow([scoring, resolution, tier, direction,
                            st, n, round(wr, 1), round(avg, 3),
                            round(wr - baseline, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 11: BAR EXPANSION (NEW v3)
# ═══════════════════════════════════════════════════════════

def analyze_bar_expansion(rows, out_path):
    """For each signal, compute expansion ratio = signal bar range / prior-20 avg.
    Bin into <0.5x, 0.5-1x, 1-1.5x, 1.5-2x, 2-3x, 3x+.

    Also compute the multi-bar momentum pattern: is the expansion building?
    (T-2 < T-1 < T < T+1) = building. (T-2 > T = fading).
    """
    log.info("Bar expansion...")

    def _bin(ratio):
        if ratio <= 0: return "no_data"
        if ratio < 0.5: return "<0.5x"
        if ratio < 1.0: return "0.5-1x"
        if ratio < 1.5: return "1-1.5x"
        if ratio < 2.0: return "1.5-2x"
        if ratio < 3.0: return "2-3x"
        return "3x+"

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "metric", "bucket", "n_trades", "wr_pct", "avg_move_pct",
                    "vs_baseline_wr_pct"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        baseline = _baseline_wr(rows, scoring, resolution, tier, direction)
                        sub = [r for r in rows
                               if r.scoring_system == scoring and r.resolution_min == resolution
                               and r.tier == tier and r.direction == direction
                               and r.avg_range_5m_prior_20_pct > 0]
                        if len(sub) < 100:
                            continue

                        # Signal bar expansion ratio
                        by_sig = defaultdict(list)
                        for r in sub:
                            ratio = r.bar_range_5m_signal_pct / r.avg_range_5m_prior_20_pct if r.avg_range_5m_prior_20_pct > 0 else 0
                            by_sig[_bin(ratio)].append(r)
                        for b in ("<0.5x", "0.5-1x", "1-1.5x", "1.5-2x", "2-3x", "3x+"):
                            s = by_sig.get(b, [])
                            if not s:
                                continue
                            n, _, wr, avg = _stats(s)
                            w.writerow([scoring, resolution, tier, direction,
                                        "signal_bar_expansion_ratio", b,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

                        # T+1 (entry bar) expansion ratio
                        by_p1 = defaultdict(list)
                        for r in sub:
                            ratio = r.bar_range_5m_t_plus_1_pct / r.avg_range_5m_prior_20_pct if r.avg_range_5m_prior_20_pct > 0 else 0
                            by_p1[_bin(ratio)].append(r)
                        for b in ("<0.5x", "0.5-1x", "1-1.5x", "1.5-2x", "2-3x", "3x+"):
                            s = by_p1.get(b, [])
                            if not s:
                                continue
                            n, _, wr, avg = _stats(s)
                            w.writerow([scoring, resolution, tier, direction,
                                        "entry_bar_expansion_ratio", b,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])

                        # Momentum pattern: is expansion building through T-2, T-1, T?
                        building = []
                        fading = []
                        flat = []
                        for r in sub:
                            t_m2 = r.bar_range_5m_t_minus_2_pct
                            t_m1 = r.bar_range_5m_t_minus_1_pct
                            t_sig = r.bar_range_5m_signal_pct
                            if t_m2 == 0 or t_m1 == 0 or t_sig == 0:
                                continue
                            if t_sig > t_m1 * 1.1 and t_m1 > t_m2 * 1.1:
                                building.append(r)
                            elif t_sig < t_m1 * 0.9 and t_m1 < t_m2 * 1.0:
                                fading.append(r)
                            else:
                                flat.append(r)
                        for label, subs in [("building", building), ("fading", fading), ("flat", flat)]:
                            if not subs:
                                continue
                            n, _, wr, avg = _stats(subs)
                            w.writerow([scoring, resolution, tier, direction,
                                        "pre_signal_momentum_pattern", label,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 12: WAVE DIRECTION A/B TEST (NEW v3)
# ═══════════════════════════════════════════════════════════

def analyze_wave_ab(rows, out_path):
    """For in_box trades with wave direction set, test:
      - Trades where wave_dir_original matches signal direction → WR?
      - Trades where wave_dir_corrected matches signal direction → WR?

    Whichever has higher WR is the correct Potter Box wave interpretation.
    """
    log.info("Wave direction A/B...")

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "wave_interpretation", "wave_alignment",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])

        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        ib = [r for r in rows
                              if r.scoring_system == scoring and r.resolution_min == resolution
                              and r.tier == tier and r.direction == direction
                              and r.pb_state == "in_box"]
                        if len(ib) < 100:
                            continue
                        ib_wr = 100 * sum(1 for r in ib if r.bucket == "full_win") / len(ib)

                        sig_dir = "bullish" if direction == "bull" else "bearish"

                        # Original interpretation
                        orig_aligned = [r for r in ib if r.wave_dir_original == sig_dir]
                        orig_opposite = [r for r in ib if r.wave_dir_original != sig_dir and r.wave_dir_original != "none"]
                        for label, subs in [("aligned", orig_aligned), ("opposite", orig_opposite)]:
                            if not subs:
                                continue
                            n, _, wr, avg = _stats(subs)
                            w.writerow([scoring, resolution, tier, direction,
                                        "original", label,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1)])

                        # Corrected interpretation
                        corr_aligned = [r for r in ib if r.wave_dir_corrected == sig_dir]
                        corr_opposite = [r for r in ib if r.wave_dir_corrected != sig_dir and r.wave_dir_corrected != "none"]
                        for label, subs in [("aligned", corr_aligned), ("opposite", corr_opposite)]:
                            if not subs:
                                continue
                            n, _, wr, avg = _stats(subs)
                            w.writerow([scoring, resolution, tier, direction,
                                        "corrected", label,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 13: MATURITY (NEW v3)
# ═══════════════════════════════════════════════════════════

def analyze_maturity(rows, out_path):
    log.info("Box maturity...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "maturity",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])
        for scoring in ("pinescript", "active_scanner"):
            for resolution in (5, 15, 30):
                for tier in (1, 2):
                    for direction in ("bull", "bear"):
                        ib = [r for r in rows
                              if r.scoring_system == scoring and r.resolution_min == resolution
                              and r.tier == tier and r.direction == direction
                              and r.pb_state == "in_box"]
                        if not ib:
                            continue
                        ib_wr = 100 * sum(1 for r in ib if r.bucket == "full_win") / len(ib)
                        by_mat = defaultdict(list)
                        for r in ib:
                            by_mat[r.pb_maturity].append(r)
                        for m in ("early", "mid", "late", "overdue", "none"):
                            sub = by_mat.get(m, [])
                            if not sub:
                                continue
                            n, _, wr, avg = _stats(sub)
                            w.writerow([scoring, resolution, tier, direction, m,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# ANALYSIS 14: SETUP INTERSECTION (NEW v3) — THE BIG ONE
# ═══════════════════════════════════════════════════════════

def analyze_setup_intersection(rows, out_path):
    """Apply stacked filters and measure WR at each level.
    Per-ticker so you can see where "raw 53% ticker" becomes "75% conviction".

    Filter stack (only for active_scanner T2 at 15m — primary setup):
      L0: raw              — just scanner fired
      L1: in_box           — scanner fired while price inside a box
      L2: at_edge          — in_box AND within 2% of box boundary (floor for bulls, roof for bears)
      L3: wave_aligned_corr — L2 AND wave_dir_corrected matches direction
      L4: expansion        — L3 AND entry bar expansion >= 1.5x prior-20 avg
      L5: full_stack       — L4 AND NOT established wave AND NOT (bear above_roof) AND NOT (bull established)
    """
    log.info("Setup intersection (conviction stack)...")

    # Focus on active_scanner T2 15m only — primary live setup
    focus = [r for r in rows
             if r.scoring_system == "active_scanner"
             and r.resolution_min == 15
             and r.tier == 2]

    # Per-ticker x direction buckets
    buckets = defaultdict(lambda: {"L0": [], "L1": [], "L2": [], "L3": [], "L4": [], "L5": []})

    for r in focus:
        key = (r.ticker, r.direction)
        buckets[key]["L0"].append(r)

        if r.pb_state == "in_box":
            buckets[key]["L1"].append(r)

            # L2: within 2% of box boundary (aligned with direction)
            at_edge = False
            if r.direction == "bull" and r.pb_floor > 0 and r.entry_price > 0:
                dist = (r.entry_price - r.pb_floor) / r.entry_price * 100
                if 0 <= dist <= 2.0:
                    at_edge = True
            elif r.direction == "bear" and r.pb_roof > 0 and r.entry_price > 0:
                dist = (r.pb_roof - r.entry_price) / r.entry_price * 100
                if 0 <= dist <= 2.0:
                    at_edge = True

            if at_edge:
                buckets[key]["L2"].append(r)

                # L3: wave corrected matches
                sig_dir = "bullish" if r.direction == "bull" else "bearish"
                if r.wave_dir_corrected == sig_dir:
                    buckets[key]["L3"].append(r)

                    # L4: entry bar expansion >= 1.5x
                    if r.avg_range_5m_prior_20_pct > 0:
                        p1_ratio = r.bar_range_5m_t_plus_1_pct / r.avg_range_5m_prior_20_pct
                        if p1_ratio >= 1.5:
                            buckets[key]["L4"].append(r)

                            # L5: all filters + hard-skips
                            skip = False
                            if r.direction == "bear" and r.pb_state == "above_roof":
                                skip = True
                            if r.pb_wave_label == "established":
                                skip = True
                            if not skip:
                                buckets[key]["L5"].append(r)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "direction", "filter_level", "filter_description",
                    "n_trades", "wr_pct", "avg_move_pct"])

        labels = {
            "L0": "raw (scanner fired only)",
            "L1": "+ in_box",
            "L2": "+ at_edge (within 2% of boundary)",
            "L3": "+ wave_aligned_corrected",
            "L4": "+ entry_bar_expansion >= 1.5x",
            "L5": "+ no hard-skips (full conviction stack)",
        }

        for (ticker, direction), levels in sorted(buckets.items()):
            for lvl in ("L0", "L1", "L2", "L3", "L4", "L5"):
                sub = levels.get(lvl, [])
                if not sub:
                    continue
                n, _, wr, avg = _stats(sub)
                w.writerow([ticker, direction, lvl, labels[lvl],
                            n, round(wr, 1), round(avg, 3)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def write_report(rows, out_path):
    total = len(rows)
    md = f"""# Combined Analysis Report

Trades analyzed: {total:,} signals from bt_resolution_study_v3.py

## Files produced (reference)

**Original overlays** (stable from earlier runs):
- `summary_per_ticker.csv` — WR per (ticker × combo)
- `summary_potter_box.csv` — WR by PB state
- `summary_cb_side.csv` — WR by CB side (in_box only)
- `summary_wave_label.csv` — WR by wave label
- `summary_credit_spreads.csv` — debit vs credit WR
- `summary_sr_hourly.csv` — 1h S/R proximity WR
- `summary_refire.csv` — fires-per-week + position WR
- `summary_rollup.csv` — roll-up strategy comparison
- `summary_quintiles.csv` — indicator quintile WR
- `summary_quintiles_cb.csv` — quintiles on CB-aligned only
- `summary_diamond.csv` — Diamond setup WR

**NEW v3 overlays:**
- `summary_bar_expansion.csv` — signal bar + entry bar expansion ratio WR
- `summary_wave_ab.csv` — original vs corrected wave interpretation
- `summary_maturity.csv` — WR by box maturity
- `summary_setup_intersection.csv` — per-ticker conviction stack lift

## Key questions each file answers

### summary_bar_expansion.csv
For each (scoring, tier, direction), three metrics binned:
- `signal_bar_expansion_ratio` — signal bar range vs prior-20 avg
- `entry_bar_expansion_ratio` — bar AFTER signal (T+1)
- `pre_signal_momentum_pattern` — building / fading / flat

**What to look for:** If `signal_bar 2-3x` or `3x+` buckets have WR well above baseline,
expansion-bar signals are a filter-on candidate. If `fading` pattern loses vs baseline,
add as skip filter.

### summary_wave_ab.csv
For each combo (in_box only), compares:
- `original` wave alignment (more-touched boundary = breakout direction)
- `corrected` wave alignment (more-touched boundary = absorption, OPPOSITE direction breaks)

**What to look for:** Higher WR on the aligned-version of whichever interpretation wins.
If `corrected aligned` has higher WR than `original aligned`, use corrected logic live.

### summary_maturity.csv
Engine says skip early boxes. Backtest tests whether mid/late/overdue actually differ.

**What to look for:** If `late` and `overdue` WR >> `mid` and `early`, enforce a
minimum maturity filter live.

### summary_setup_intersection.csv
The full conviction stack per ticker. Columns:
- L0 raw scanner → L1 in_box → L2 at_edge → L3 wave_aligned → L4 expansion → L5 full_stack

**What to look for:** Per-ticker lift from L0 to L5. Tickers where L5 >> L0 are
"conviction only" — trade only on full stack. Tickers where L5 ≈ L0 offer no
incremental filter value from our stack.

## Your decision criteria

From your earlier message:
> "I'm happy to have them so test for anything that we can find an edge and put in
> the deliverable bot when we are finished testing. I want confirmed cards with edge
> not noise"

Rules for including a filter in the bot:
- Must show >=5 WR points lift over baseline at sample size >=500
- Must be independently verifiable (i.e., lift survives when other filters also applied)
- Must be computable at signal-fire time in live code

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

    # Existing analyses
    analyze_per_ticker(rows, OUT_DIR / "summary_per_ticker.csv")
    analyze_potter_box(rows, OUT_DIR / "summary_potter_box.csv")
    analyze_cb_side(rows, OUT_DIR / "summary_cb_side.csv")
    analyze_wave_label(rows, OUT_DIR / "summary_wave_label.csv")
    analyze_credit_spreads(rows, OUT_DIR / "summary_credit_spreads.csv")
    analyze_sr_hourly(rows, OUT_DIR / "summary_sr_hourly.csv")
    wg = analyze_refire(rows, OUT_DIR / "summary_refire.csv")
    analyze_rollup(wg, OUT_DIR / "summary_rollup.csv")
    analyze_quintiles(rows, OUT_DIR / "summary_quintiles.csv",
                      OUT_DIR / "summary_quintiles_cb.csv")
    analyze_diamond(rows, OUT_DIR / "summary_diamond.csv")

    # NEW v3 analyses
    analyze_bar_expansion(rows, OUT_DIR / "summary_bar_expansion.csv")
    analyze_wave_ab(rows, OUT_DIR / "summary_wave_ab.csv")
    analyze_maturity(rows, OUT_DIR / "summary_maturity.csv")
    analyze_setup_intersection(rows, OUT_DIR / "summary_setup_intersection.csv")

    write_report(rows, OUT_DIR / "report_combined.md")

    log.info(f"DONE. All outputs in {OUT_DIR}:")
    for fn in sorted(OUT_DIR.glob("summary_*.csv")):
        log.info(f"  {fn.name}  ({fn.stat().st_size} bytes)")
    rp = OUT_DIR / "report_combined.md"
    if rp.exists():
        log.info(f"  {rp.name}  ({rp.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
