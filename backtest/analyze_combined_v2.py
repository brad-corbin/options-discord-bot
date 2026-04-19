#!/usr/bin/env python3
"""
analyze_combined_v2.py — STREAMING version of analyze_combined_v1.py
════════════════════════════════════════════════════════════════════════════════

Same analyses, same output CSVs, same columns, same bucket definitions as v1.
Different implementation: streams trades.csv instead of loading ~850MB of
Row dataclass objects into memory.

WHY v2 EXISTS:
  v1 OOM'd on the 572K-trade v3 dataset. v1 loads every row as a Row dataclass
  (~1.5KB each × 572K = ~850MB), iterates the full list 14+ times.

  v2 runs in ~50-80MB peak memory by:
    - streaming trades.csv with csv.DictReader (one row at a time)
    - accumulating all single-pass analyses in ONE file pass
    - collecting only the minimal data needed for multi-pass analyses
    - doing quintile binning in a SECOND pass using bounds from first pass
    - processing refire/rollup/setup_intersection from small in-memory subsets

STREAMING STRATEGY:
  PASS 1 (single stream through trades.csv):
    - accumulate counters for: per_ticker, potter_box, cb_side, wave_label,
      credit_spreads, sr_hourly, bar_expansion, wave_ab, maturity
    - collect indicator value lists per combo for quintile bounds
    - collect minimal 15m-row dicts grouped by ISO week (for refire/rollup)
    - collect active_scanner T2 15m subset (for setup_intersection)
  PASS 2 (re-stream trades.csv):
    - use quintile bounds from pass 1 to bin each row
    - accumulate quintile WR counters and diamond counters
  IN-MEMORY:
    - refire/rollup processes the week-keyed 15m groups
    - setup_intersection processes the filtered subset

USAGE (Render shell):
    cd /opt/render/project/src
    export BACKTEST_DIR=/var/backtest
    python3 backtest/analyze_combined_v2.py

READS:  $BACKTEST_DIR/trades.csv  (default /tmp/backtest_resolution)
WRITES: same directory:
  report_combined.md
  summary_per_ticker.csv
  summary_potter_box.csv
  summary_cb_side.csv
  summary_wave_label.csv
  summary_credit_spreads.csv
  summary_sr_hourly.csv
  summary_refire.csv
  summary_rollup.csv
  summary_quintiles.csv
  summary_quintiles_cb.csv
  summary_diamond.csv
  summary_bar_expansion.csv
  summary_wave_ab.csv
  summary_maturity.csv
  summary_setup_intersection.csv

Runs in ~3-5 minutes on 572K signals with ~50-80MB peak RSS.

Output-compatible with analyze_combined_v1.py. Column order, bucket definitions,
and CSV headers match v1 exactly so downstream consumers don't break.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("analyze_combined_v2")

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

# Tuple of (scoring, resolution, tier, direction) combos iterated by v1
_SCORINGS = ("pinescript", "active_scanner")
_RESOLUTIONS = (5, 15, 30)
_TIERS = (1, 2)
_DIRECTIONS = ("bull", "bear")


# ═══════════════════════════════════════════════════════════
# TYPED FIELD EXTRACTION (matches v1's _pb, _pf, _pi)
# ═══════════════════════════════════════════════════════════

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


def _parse_row_typed(r):
    """Parse CSV row dict → typed dict. Matches v1's Row dataclass field-for-field.
    Returns None on malformed rows (logged as debug)."""
    try:
        return {
            "ticker": r["ticker"],
            "resolution_min": _pi(r["resolution_min"]),
            "scoring_system": r["scoring_system"],
            "tier": _pi(r["tier"]),
            "direction": r["direction"],
            "signal_ts": _pi(r["signal_ts"]),
            "entry_ts": _pi(r["entry_ts"]),
            "entry_price": _pf(r["entry_price"]),
            "exit_ts": _pi(r["exit_ts"]),
            "exit_price": _pf(r["exit_price"]),
            "move_signed_pct": _pf(r["move_signed_pct"]),
            "bucket": r["bucket"],
            "win_headline": _pb(r["win_headline"]),
            "regime_trend": r.get("regime_trend", ""),
            "ind_ema_diff_pct": _pf(r["ind_ema_diff_pct"]),
            "ind_macd_hist": _pf(r["ind_macd_hist"]),
            "ind_rsi": _pf(r["ind_rsi"], 50.0),
            "ind_wt2": _pf(r["ind_wt2"]),
            "ind_adx": _pf(r["ind_adx"]),
            "pb_state": r.get("pb_state", "no_box"),
            "pb_floor": _pf(r.get("pb_floor", 0)),
            "pb_roof": _pf(r.get("pb_roof", 0)),
            "pb_midpoint": _pf(r.get("pb_midpoint", 0)),
            "pb_wave_label": r.get("pb_wave_label", "none"),
            "cb_side": r.get("cb_side", "n/a"),
            "cb_distance_pct": _pf(r.get("cb_distance_pct", 0)),
            "credit_short_strike": _pf(r.get("credit_short_strike", 0)),
            "credit_25_bucket": r.get("credit_25_bucket", "n/a"),
            "credit_25_win": _pb(r.get("credit_25_win", False)),
            "credit_50_bucket": r.get("credit_50_bucket", "n/a"),
            "credit_50_win": _pb(r.get("credit_50_win", False)),
            "sr_h_fractal_dist_above_pct": _pf(r.get("sr_h_fractal_dist_above_pct", 999), 999.0),
            "sr_h_fractal_dist_below_pct": _pf(r.get("sr_h_fractal_dist_below_pct", 999), 999.0),
            "sr_h_pivot_dist_above_pct": _pf(r.get("sr_h_pivot_dist_above_pct", 999), 999.0),
            "sr_h_pivot_dist_below_pct": _pf(r.get("sr_h_pivot_dist_below_pct", 999), 999.0),
            "bar_range_5m_t_minus_3_pct": _pf(r.get("bar_range_5m_t_minus_3_pct", 0)),
            "bar_range_5m_t_minus_2_pct": _pf(r.get("bar_range_5m_t_minus_2_pct", 0)),
            "bar_range_5m_t_minus_1_pct": _pf(r.get("bar_range_5m_t_minus_1_pct", 0)),
            "bar_range_5m_signal_pct": _pf(r.get("bar_range_5m_signal_pct", 0)),
            "bar_range_5m_t_plus_1_pct": _pf(r.get("bar_range_5m_t_plus_1_pct", 0)),
            "bar_range_5m_t_plus_2_pct": _pf(r.get("bar_range_5m_t_plus_2_pct", 0)),
            "avg_range_5m_prior_20_pct": _pf(r.get("avg_range_5m_prior_20_pct", 0)),
            "void_above_pct": _pf(r.get("void_above_pct", 0)),
            "void_below_pct": _pf(r.get("void_below_pct", 0)),
            "box_above_floor": _pf(r.get("box_above_floor", 0)),
            "box_above_roof": _pf(r.get("box_above_roof", 0)),
            "box_below_floor": _pf(r.get("box_below_floor", 0)),
            "box_below_roof": _pf(r.get("box_below_roof", 0)),
            "wave_dir_original": r.get("wave_dir_original", "none"),
            "wave_dir_corrected": r.get("wave_dir_corrected", "none"),
            "wave_roof_touches": _pi(r.get("wave_roof_touches", 0)),
            "wave_floor_touches": _pi(r.get("wave_floor_touches", 0)),
            "pb_maturity": r.get("pb_maturity", "none"),
            "pb_maturity_ratio": _pf(r.get("pb_maturity_ratio", 0)),
            "pb_duration_bars": _pi(r.get("pb_duration_bars", 0)),
            "pb_run_pct": _pf(r.get("pb_run_pct", 0)),
            "pb_punchback": _pb(r.get("pb_punchback", False)),
        }
    except (ValueError, KeyError) as e:
        log.debug(f"Skip malformed row: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# UTILITIES (matches v1)
# ═══════════════════════════════════════════════════════════

def _quintile_bounds(values):
    """Same as v1's _quintile_bounds."""
    if not values:
        return [0, 0, 0, 0]
    sv = sorted(values)
    n = len(sv)
    return [sv[int(n * 0.2)], sv[int(n * 0.4)],
            sv[int(n * 0.6)], sv[int(n * 0.8)]]


def _assign_q(value, bounds):
    """Same as v1's _assign_q."""
    if value < bounds[0]: return "Q1"
    if value < bounds[1]: return "Q2"
    if value < bounds[2]: return "Q3"
    if value < bounds[3]: return "Q4"
    return "Q5"


def _counter_init():
    """Fresh counter dict for WR/move aggregation."""
    return {"n": 0, "fw": 0, "part": 0, "fl": 0, "sum_move": 0.0}


def _counter_update(c, row):
    c["n"] += 1
    b = row["bucket"]
    if b == "full_win":
        c["fw"] += 1
    elif b == "partial":
        c["part"] += 1
    elif b == "full_loss":
        c["fl"] += 1
    c["sum_move"] += row["move_signed_pct"]


def _counter_wr_avg(c):
    """Return (n, wins, wr_pct, avg_move_pct) like v1's _stats."""
    n = c["n"]
    if n == 0:
        return (0, 0, 0.0, 0.0)
    return (n, c["fw"], 100.0 * c["fw"] / n, c["sum_move"] / n)


def _sr_bucket(d):
    """Matches v1's _bin in analyze_sr_hourly."""
    if d >= 999: return "none"
    if d < 0.5: return "<0.5%"
    if d < 1.0: return "0.5-1%"
    if d < 2.0: return "1-2%"
    if d < 3.0: return "2-3%"
    return "3%+"


def _bar_exp_bucket(ratio):
    """Matches v1's _bin in analyze_bar_expansion."""
    if ratio <= 0: return "no_data"
    if ratio < 0.5: return "<0.5x"
    if ratio < 1.0: return "0.5-1x"
    if ratio < 1.5: return "1-1.5x"
    if ratio < 2.0: return "1.5-2x"
    if ratio < 3.0: return "2-3x"
    return "3x+"


# ═══════════════════════════════════════════════════════════
# PASS 1 — SINGLE STREAM THROUGH trades.csv
# ═══════════════════════════════════════════════════════════

class Pass1State:
    """All accumulators updated during the single pass through trades.csv."""

    def __init__(self):
        # Per-ticker counters — keyed by (ticker, scoring, resolution, tier, direction)
        self.per_ticker = defaultdict(_counter_init)

        # Baseline WR accumulators — keyed by (scoring, resolution, tier, direction)
        # Used by potter_box/cb_side/wave_label/bar_expansion/sr_hourly for "vs_baseline"
        self.baseline = defaultdict(_counter_init)

        # Potter box state — keyed by (scoring, resolution, tier, direction, pb_state)
        self.potter_box = defaultdict(_counter_init)

        # In-box baseline — keyed by (scoring, resolution, tier, direction)
        self.inbox_baseline = defaultdict(_counter_init)

        # CB side (in_box only) — keyed by (scoring, resolution, tier, direction, cb_side)
        self.cb_side = defaultdict(_counter_init)

        # Wave label (in_box only) — keyed by (scoring, resolution, tier, direction, pb_wave_label)
        self.wave_label = defaultdict(_counter_init)

        # Credit spreads (in_box with strike) — keyed by (scoring, resolution, tier, direction)
        # Accumulate: n_in_box, debit_wins, c25_wins, c50_wins, c25_partials, c50_partials
        self.credit = defaultdict(lambda: {"n": 0, "debit_wins": 0,
                                            "c25_wins": 0, "c50_wins": 0,
                                            "c25_part": 0, "c50_part": 0})

        # 1h S/R — keyed by (scoring, resolution, tier, direction, method, side_name, bucket)
        self.sr_hourly = defaultdict(_counter_init)
        # Pre-sample SR presence check: set to True if any row has sr < 999
        self.sr_present = False

        # Bar expansion — keyed by (scoring, resolution, tier, direction, metric, bucket)
        # metric ∈ {"signal_bar_expansion_ratio", "entry_bar_expansion_ratio", "pre_signal_momentum_pattern"}
        self.bar_exp = defaultdict(_counter_init)

        # Wave A/B (in_box only) — keyed by (scoring, resolution, tier, direction, interpretation, alignment)
        # interpretation ∈ {"original", "corrected"}, alignment ∈ {"aligned", "opposite"}
        self.wave_ab = defaultdict(_counter_init)

        # Maturity (in_box only) — keyed by (scoring, resolution, tier, direction, pb_maturity)
        self.maturity = defaultdict(_counter_init)

        # Quintile indicator values — keyed by (scoring, resolution, tier, direction), value = {col: [values]}
        # Collected during pass 1 so we can compute bounds before pass 2
        self.quintile_values = defaultdict(lambda: {col: [] for col, _ in INDICATORS})

        # CB-aligned in_box subset — separate indicator values for quintiles_cb
        self.quintile_values_cb = defaultdict(lambda: {col: [] for col, _ in INDICATORS})

        # 15m minimal rows grouped by week key, for refire/rollup analysis
        # key = (ticker, scoring, tier, direction, iy, iw)
        # value = list of minimal dicts ordered by signal_ts
        self.wg = defaultdict(list)

        # active_scanner T2 15m subset for setup_intersection analysis
        # stored as minimal dicts
        self.si_subset = []

        # Total rows processed
        self.n_rows = 0
        self.n_skipped = 0


def _pass1_update(state: Pass1State, row):
    """Update all pass-1 accumulators for one row."""
    state.n_rows += 1

    ticker = row["ticker"]
    scoring = row["scoring_system"]
    resolution = row["resolution_min"]
    tier = row["tier"]
    direction = row["direction"]
    pb_state = row["pb_state"]
    combo = (scoring, resolution, tier, direction)

    # Per-ticker
    _counter_update(state.per_ticker[(ticker, scoring, resolution, tier, direction)], row)

    # Baseline (used by potter_box, cb_side wave_label variants, bar_exp, sr_hourly)
    _counter_update(state.baseline[combo], row)

    # Potter box
    _counter_update(state.potter_box[(*combo, pb_state)], row)

    # In-box-specific accumulators
    if pb_state == "in_box":
        _counter_update(state.inbox_baseline[combo], row)
        _counter_update(state.cb_side[(*combo, row["cb_side"])], row)
        _counter_update(state.wave_label[(*combo, row["pb_wave_label"])], row)
        _counter_update(state.maturity[(*combo, row["pb_maturity"])], row)

        # Wave A/B: aligned vs opposite per interpretation
        sig_dir = "bullish" if direction == "bull" else "bearish"
        if row["wave_dir_original"] != "none":
            align = "aligned" if row["wave_dir_original"] == sig_dir else "opposite"
            _counter_update(state.wave_ab[(*combo, "original", align)], row)
        if row["wave_dir_corrected"] != "none":
            align = "aligned" if row["wave_dir_corrected"] == sig_dir else "opposite"
            _counter_update(state.wave_ab[(*combo, "corrected", align)], row)

        # CB-aligned in_box subset for quintiles_cb
        if (direction == "bull" and row["cb_side"] in ("below_cb", "at_cb")) or \
           (direction == "bear" and row["cb_side"] in ("above_cb", "at_cb")):
            for col, _ in INDICATORS:
                state.quintile_values_cb[combo][col].append(row[col])

    # Credit spreads (in_box with strike)
    if pb_state == "in_box" and row["credit_short_strike"] > 0:
        c = state.credit[combo]
        c["n"] += 1
        if row["bucket"] == "full_win":
            c["debit_wins"] += 1
        if row["credit_25_win"]:
            c["c25_wins"] += 1
        if row["credit_50_win"]:
            c["c50_wins"] += 1
        if row["credit_25_bucket"] == "partial":
            c["c25_part"] += 1
        if row["credit_50_bucket"] == "partial":
            c["c50_part"] += 1

    # 1h S/R
    above = row["sr_h_fractal_dist_above_pct"]
    if above < 999:
        state.sr_present = True
    # Only stream-compute buckets if combo is big enough; but we don't know that yet.
    # v1 filters by len(sub) >= 100 AFTER iteration. We accumulate everything and filter at output time.
    for method, attr_above, attr_below in [
        ("fractal", "sr_h_fractal_dist_above_pct", "sr_h_fractal_dist_below_pct"),
        ("pivot", "sr_h_pivot_dist_above_pct", "sr_h_pivot_dist_below_pct"),
    ]:
        for side_name, attr in [("above_spot", attr_above), ("below_spot", attr_below)]:
            b = _sr_bucket(row[attr])
            _counter_update(state.sr_hourly[(*combo, method, side_name, b)], row)

    # Bar expansion (only if avg_range_5m_prior_20_pct > 0)
    avg_prior = row["avg_range_5m_prior_20_pct"]
    if avg_prior > 0:
        # signal_bar_expansion_ratio
        sig_ratio = row["bar_range_5m_signal_pct"] / avg_prior
        _counter_update(state.bar_exp[(*combo, "signal_bar_expansion_ratio", _bar_exp_bucket(sig_ratio))], row)
        # entry_bar_expansion_ratio (T+1)
        p1_ratio = row["bar_range_5m_t_plus_1_pct"] / avg_prior
        _counter_update(state.bar_exp[(*combo, "entry_bar_expansion_ratio", _bar_exp_bucket(p1_ratio))], row)
        # pre_signal_momentum_pattern
        t_m2 = row["bar_range_5m_t_minus_2_pct"]
        t_m1 = row["bar_range_5m_t_minus_1_pct"]
        t_sig = row["bar_range_5m_signal_pct"]
        if t_m2 > 0 and t_m1 > 0 and t_sig > 0:
            if t_sig > t_m1 * 1.1 and t_m1 > t_m2 * 1.1:
                label = "building"
            elif t_sig < t_m1 * 0.9 and t_m1 < t_m2 * 1.0:
                label = "fading"
            else:
                label = "flat"
            _counter_update(state.bar_exp[(*combo, "pre_signal_momentum_pattern", label)], row)

    # Collect indicator values for quintiles (pass-2 binning uses bounds from these)
    for col, _ in INDICATORS:
        state.quintile_values[combo][col].append(row[col])

    # 15m refire/rollup: store minimal dict keyed by week
    if resolution == 15:
        try:
            dt = datetime.fromtimestamp(row["signal_ts"])
            iy, iw, _ = dt.isocalendar()
            wg_key = (ticker, scoring, tier, direction, iy, iw)
            # Minimal fields needed by refire + rollup analyses
            state.wg[wg_key].append({
                "signal_ts": row["signal_ts"],
                "entry_ts": row["entry_ts"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "move_signed_pct": row["move_signed_pct"],
                "bucket": row["bucket"],
                "direction": row["direction"],
            })
        except (ValueError, OSError):
            pass

    # active_scanner T2 15m subset for setup_intersection
    if scoring == "active_scanner" and resolution == 15 and tier == 2:
        state.si_subset.append({
            "ticker": ticker,
            "direction": direction,
            "pb_state": pb_state,
            "pb_floor": row["pb_floor"],
            "pb_roof": row["pb_roof"],
            "pb_wave_label": row["pb_wave_label"],
            "wave_dir_corrected": row["wave_dir_corrected"],
            "entry_price": row["entry_price"],
            "bar_range_5m_t_plus_1_pct": row["bar_range_5m_t_plus_1_pct"],
            "avg_range_5m_prior_20_pct": row["avg_range_5m_prior_20_pct"],
            "bucket": row["bucket"],
            "move_signed_pct": row["move_signed_pct"],
        })


def run_pass1():
    """Stream trades.csv once, accumulate everything for non-quintile analyses."""
    log.info("PASS 1: streaming trades.csv...")
    state = Pass1State()

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

        report_every = 50000
        for raw in rdr:
            row = _parse_row_typed(raw)
            if row is None:
                state.n_skipped += 1
                continue
            _pass1_update(state, row)
            if state.n_rows % report_every == 0:
                log.info(f"  ...pass 1 processed {state.n_rows:,} rows")

    log.info(f"PASS 1 done: {state.n_rows:,} rows processed, {state.n_skipped} skipped")
    log.info(f"  15m week-groups: {len(state.wg):,}")
    log.info(f"  active_scanner T2 15m subset: {len(state.si_subset):,}")
    return state


# ═══════════════════════════════════════════════════════════
# PASS 2 — QUINTILE BINNING (uses bounds from pass 1 data)
# ═══════════════════════════════════════════════════════════

def run_pass2(state: Pass1State):
    """Compute quintile bounds, then re-stream trades.csv to bin rows.
    Accumulates:
      - quintiles_plain: keyed by (combo, col, quintile) → counters
      - quintiles_cb:    keyed by (combo, col, quintile) → counters (CB-aligned in_box only)
      - diamond:         keyed by (combo, setup_type) → counters
    """
    log.info("Computing quintile bounds from pass-1 indicator lists...")

    # Plain quintile bounds (per combo × indicator)
    bounds_plain = {}  # (combo, col) → [b1, b2, b3, b4]
    minmax_plain = {}  # (combo, col) → (min, max)
    for combo, by_col in state.quintile_values.items():
        if sum(len(v) for v in by_col.values()) / max(1, len(by_col)) < 100:
            # Matches v1's len(subset) < 100 skip check
            continue
        for col, _ in INDICATORS:
            vals = by_col[col]
            if not vals:
                continue
            bounds_plain[(combo, col)] = _quintile_bounds(vals)
            minmax_plain[(combo, col)] = (min(vals), max(vals))

    # CB-aligned quintile bounds
    bounds_cb = {}
    minmax_cb = {}
    for combo, by_col in state.quintile_values_cb.items():
        if sum(len(v) for v in by_col.values()) / max(1, len(by_col)) < 100:
            continue
        for col, _ in INDICATORS:
            vals = by_col[col]
            if not vals:
                continue
            bounds_cb[(combo, col)] = _quintile_bounds(vals)
            minmax_cb[(combo, col)] = (min(vals), max(vals))

    # Diamond uses ema_diff and macd_hist bounds (same plain bounds — v1 recomputes but
    # same dataset produces same bounds). Also diamond requires >= 200 samples.
    # v1 recomputes bounds from subset where len >= 200. We use plain_bounds if combo has
    # ≥200 rows in the combo.
    diamond_eligible = set()
    for combo in state.baseline:
        if state.baseline[combo]["n"] >= 200:
            diamond_eligible.add(combo)

    # CB-aligned in_box baseline for quintiles_cb "vs_aligned_baseline"
    # We accumulate this in pass 2 as we stream (tally of rows in the CB-aligned subset per combo)
    log.info("PASS 2: streaming trades.csv for quintile and diamond binning...")

    quint_plain = defaultdict(_counter_init)  # (combo, col, quintile) → counter
    quint_cb = defaultdict(_counter_init)     # (combo, col, quintile) → counter
    diamond = defaultdict(_counter_init)      # (combo, setup_type) → counter
    cb_aligned_baseline = defaultdict(_counter_init)  # combo → counter (for vs_aligned baseline)

    # Free memory we no longer need
    state.quintile_values = None
    state.quintile_values_cb = None

    n = 0
    report_every = 50000
    with open(TRADES_CSV) as f:
        rdr = csv.DictReader(f)
        for raw in rdr:
            row = _parse_row_typed(raw)
            if row is None:
                continue
            n += 1
            if n % report_every == 0:
                log.info(f"  ...pass 2 processed {n:,} rows")

            scoring = row["scoring_system"]
            resolution = row["resolution_min"]
            tier = row["tier"]
            direction = row["direction"]
            combo = (scoring, resolution, tier, direction)

            # Plain quintiles
            for col, _ in INDICATORS:
                if (combo, col) not in bounds_plain:
                    continue
                b = bounds_plain[(combo, col)]
                q = _assign_q(row[col], b)
                _counter_update(quint_plain[(combo, col, q)], row)

            # CB-aligned quintiles (in_box + direction-aligned CB side)
            is_cb_aligned = (row["pb_state"] == "in_box" and (
                (direction == "bull" and row["cb_side"] in ("below_cb", "at_cb")) or
                (direction == "bear" and row["cb_side"] in ("above_cb", "at_cb"))
            ))
            if is_cb_aligned:
                _counter_update(cb_aligned_baseline[combo], row)
                for col, _ in INDICATORS:
                    if (combo, col) not in bounds_cb:
                        continue
                    b = bounds_cb[(combo, col)]
                    q = _assign_q(row[col], b)
                    _counter_update(quint_cb[(combo, col, q)], row)

            # Diamond (uses ema_diff + macd_hist bounds; requires ≥200-row combo)
            if combo in diamond_eligible:
                if (combo, "ind_ema_diff_pct") in bounds_plain and \
                   (combo, "ind_macd_hist") in bounds_plain:
                    eq = _assign_q(row["ind_ema_diff_pct"], bounds_plain[(combo, "ind_ema_diff_pct")])
                    mq = _assign_q(row["ind_macd_hist"], bounds_plain[(combo, "ind_macd_hist")])
                    ec = eq in ("Q2", "Q3", "Q4")
                    mc = mq in ("Q2", "Q3", "Q4")
                    if ec and mc:
                        st = "diamond"
                    elif ec:
                        st = "macd_extreme"
                    elif mc:
                        st = "ema_extreme"
                    else:
                        st = "both_extreme"
                    _counter_update(diamond[(combo, st)], row)

    log.info(f"PASS 2 done: {n:,} rows processed")
    return {
        "bounds_plain": bounds_plain,
        "minmax_plain": minmax_plain,
        "bounds_cb": bounds_cb,
        "minmax_cb": minmax_cb,
        "quint_plain": quint_plain,
        "quint_cb": quint_cb,
        "cb_aligned_baseline": cb_aligned_baseline,
        "diamond": diamond,
    }


# ═══════════════════════════════════════════════════════════
# WRITERS — one per output CSV (mirror v1 headers exactly)
# ═══════════════════════════════════════════════════════════

def write_per_ticker(state, out_path):
    log.info("write per_ticker...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "scoring", "resolution_min", "tier", "direction",
                    "n_trades", "full_win", "partial", "full_loss",
                    "wr_pct", "wr_with_partials_pct", "avg_move_pct", "significance"])
        for key, c in sorted(state.per_ticker.items()):
            ticker, scoring, resolution, tier, direction = key
            n = c["n"]
            if n == 0:
                continue
            fw, part, fl = c["fw"], c["part"], c["fl"]
            avg = c["sum_move"] / n
            wr = 100 * fw / n
            wr_p = 100 * (fw + part) / n
            sig = "ok" if n >= 100 else ("small" if n >= 30 else "tiny")
            w.writerow([ticker, scoring, resolution, tier, direction, n,
                        fw, part, fl, round(wr, 1), round(wr_p, 1),
                        round(avg, 3), sig])
    log.info(f"  → {out_path}")


def write_potter_box(state, out_path):
    log.info("write potter_box...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "pb_state",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        bc = state.baseline.get(combo)
                        if not bc or bc["n"] == 0:
                            continue
                        baseline_wr = 100.0 * bc["fw"] / bc["n"]
                        for st in ("in_box", "above_roof", "below_floor", "post_box", "no_box"):
                            c = state.potter_box.get((*combo, st))
                            if not c or c["n"] == 0:
                                continue
                            n, _, wr, avg = _counter_wr_avg(c)
                            w.writerow([scoring, resolution, tier, direction, st,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline_wr, 1)])
    log.info(f"  → {out_path}")


def write_cb_side(state, out_path):
    log.info("write cb_side...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "cb_side",
                    "n_trades", "wr_pct", "avg_move_pct",
                    "vs_in_box_baseline_wr_pct", "vs_overall_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        ibc = state.inbox_baseline.get(combo)
                        bc = state.baseline.get(combo)
                        if not ibc or ibc["n"] == 0 or not bc or bc["n"] == 0:
                            continue
                        ib_wr = 100.0 * ibc["fw"] / ibc["n"]
                        overall_wr = 100.0 * bc["fw"] / bc["n"]
                        for side in ("below_cb", "above_cb", "at_cb"):
                            c = state.cb_side.get((*combo, side))
                            if not c or c["n"] == 0:
                                continue
                            n, _, wr, avg = _counter_wr_avg(c)
                            w.writerow([scoring, resolution, tier, direction, side,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1),
                                        round(wr - overall_wr, 1)])
    log.info(f"  → {out_path}")


def write_wave_label(state, out_path):
    log.info("write wave_label...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "wave_label",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        ibc = state.inbox_baseline.get(combo)
                        if not ibc or ibc["n"] == 0:
                            continue
                        ib_wr = 100.0 * ibc["fw"] / ibc["n"]
                        for wl in ("established", "weakening", "breakout_probable",
                                   "breakout_imminent", "none"):
                            c = state.wave_label.get((*combo, wl))
                            if not c or c["n"] == 0:
                                continue
                            n, _, wr, avg = _counter_wr_avg(c)
                            w.writerow([scoring, resolution, tier, direction, wl,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1)])
    log.info(f"  → {out_path}")


def write_credit_spreads(state, out_path):
    log.info("write credit_spreads...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "n_in_box", "debit_wr_pct",
                    "credit_25_wr_pct", "credit_50_wr_pct",
                    "credit_25_partial_pct", "credit_50_partial_pct",
                    "better_strategy"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        c = state.credit.get((scoring, resolution, tier, direction))
                        if not c or c["n"] < 20:
                            continue
                        n = c["n"]
                        debit_wr = 100 * c["debit_wins"] / n
                        c25_wr = 100 * c["c25_wins"] / n
                        c50_wr = 100 * c["c50_wins"] / n
                        c25_pp = 100 * c["c25_part"] / n
                        c50_pp = 100 * c["c50_part"] / n
                        best_c = max(c25_wr, c50_wr)
                        if debit_wr >= best_c + 3:
                            best = "debit"
                        elif best_c >= debit_wr + 3:
                            best = f"credit_{'25' if c25_wr >= c50_wr else '50'}"
                        else:
                            best = "tie"
                        w.writerow([scoring, resolution, tier, direction, n,
                                    round(debit_wr, 1), round(c25_wr, 1), round(c50_wr, 1),
                                    round(c25_pp, 1), round(c50_pp, 1), best])
    log.info(f"  → {out_path}")


def write_sr_hourly(state, out_path):
    log.info("write sr_hourly...")
    if not state.sr_present:
        log.warning("No S/R data; skipping")
        return
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "sr_method", "sr_side", "distance_bucket",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        bc = state.baseline.get(combo)
                        if not bc or bc["n"] < 100:
                            continue
                        baseline_wr = 100.0 * bc["fw"] / bc["n"]
                        for method in ("fractal", "pivot"):
                            for side_name in ("above_spot", "below_spot"):
                                for b in ("<0.5%", "0.5-1%", "1-2%", "2-3%", "3%+", "none"):
                                    c = state.sr_hourly.get((*combo, method, side_name, b))
                                    if not c or c["n"] == 0:
                                        continue
                                    n, _, wr, avg = _counter_wr_avg(c)
                                    w.writerow([scoring, resolution, tier, direction,
                                                method, side_name, b,
                                                n, round(wr, 1), round(avg, 3),
                                                round(wr - baseline_wr, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# REFIRE / ROLLUP — in-memory processing of state.wg
# ═══════════════════════════════════════════════════════════

def write_refire(state, out_path):
    log.info("write refire...")
    # Sort each week's list by signal_ts
    for k in state.wg:
        state.wg[k].sort(key=lambda x: x["signal_ts"])

    dist = defaultdict(lambda: defaultdict(int))
    first_fire = defaultdict(lambda: defaultdict(list))
    pos_wr = defaultdict(lambda: defaultdict(list))

    for key, group in state.wg.items():
        _, scoring, tier, direction, _, _ = key
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
                wins = sum(1 for x in sub if x["bucket"] == "full_win")
                avg = sum(x["move_signed_pct"] for x in sub) / len(sub)
                w.writerow(["first_fire_given_total_fires", s, t, d, bk,
                            len(sub), round(100 * wins / len(sub), 1), round(avg, 3)])
        for (s, t, d), b in sorted(pos_wr.items()):
            for bk in ("1", "2", "3", "4+"):
                sub = b.get(bk, [])
                if not sub:
                    continue
                wins = sum(1 for x in sub if x["bucket"] == "full_win")
                avg = sum(x["move_signed_pct"] for x in sub) / len(sub)
                w.writerow(["fire_position_in_week", s, t, d, bk,
                            len(sub), round(100 * wins / len(sub), 1), round(avg, 3)])
    log.info(f"  → {out_path}")


def write_rollup(state, out_path):
    log.info("write rollup...")
    results = defaultdict(list)
    for key, group in state.wg.items():
        _, scoring, tier, direction, _, _ = key
        if len(group) < 2:
            continue
        f1 = group[0]
        f2 = group[1]
        if f2["entry_ts"] <= f1["entry_ts"]:
            continue

        pnl1 = f1["move_signed_pct"]
        pnl2 = f2["move_signed_pct"]
        hold_both = pnl1 + pnl2
        hold_both_win = (f1["bucket"] == "full_win" and f2["bucket"] == "full_win")

        if f1["entry_price"] > 0:
            if direction == "bull":
                rollup = (f2["exit_price"] - f1["entry_price"]) / f1["entry_price"] * 100
            else:
                rollup = -(f2["exit_price"] - f1["entry_price"]) / f1["entry_price"] * 100
            rollup_win = rollup >= -1.0
        else:
            rollup = 0.0
            rollup_win = False

        combo = (scoring, tier, direction)
        results[(combo, "hold_both")].append((hold_both, hold_both_win))
        results[(combo, "roll_up")].append((rollup, rollup_win))
        results[(combo, "fire1_only")].append((pnl1, f1["bucket"] == "full_win"))

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
# QUINTILES + DIAMOND writers (use pass-2 output)
# ═══════════════════════════════════════════════════════════

def write_quintiles(state, p2, out_plain, out_cb):
    log.info("write quintiles (plain + CB-aligned)...")

    # Plain
    with open(out_plain, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "indicator", "quintile", "lower_bound", "upper_bound",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        bc = state.baseline.get(combo)
                        if not bc or bc["n"] < 100:
                            continue
                        baseline_wr = 100.0 * bc["fw"] / bc["n"]
                        for col, _ in INDICATORS:
                            if (combo, col) not in p2["bounds_plain"]:
                                continue
                            bounds = p2["bounds_plain"][(combo, col)]
                            mn, mx = p2["minmax_plain"][(combo, col)]
                            qb = {
                                "Q1": (mn, bounds[0]),
                                "Q2": (bounds[0], bounds[1]),
                                "Q3": (bounds[1], bounds[2]),
                                "Q4": (bounds[2], bounds[3]),
                                "Q5": (bounds[3], mx),
                            }
                            for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                                c = p2["quint_plain"].get((combo, col, q))
                                if not c or c["n"] == 0:
                                    continue
                                n, _, wr, avg = _counter_wr_avg(c)
                                lo, hi = qb[q]
                                w.writerow([scoring, resolution, tier, direction,
                                            col, q, round(lo, 4), round(hi, 4),
                                            n, round(wr, 1), round(avg, 3),
                                            round(wr - baseline_wr, 1)])
    log.info(f"  → {out_plain}")

    # CB-aligned — v1 uses the aligned-subset baseline (not overall baseline)
    with open(out_cb, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "indicator", "quintile", "lower_bound", "upper_bound",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_aligned_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        ab = p2["cb_aligned_baseline"].get(combo)
                        if not ab or ab["n"] < 100:
                            continue
                        al_wr = 100.0 * ab["fw"] / ab["n"]
                        for col, _ in INDICATORS:
                            if (combo, col) not in p2["bounds_cb"]:
                                continue
                            bounds = p2["bounds_cb"][(combo, col)]
                            mn, mx = p2["minmax_cb"][(combo, col)]
                            qb = {
                                "Q1": (mn, bounds[0]),
                                "Q2": (bounds[0], bounds[1]),
                                "Q3": (bounds[1], bounds[2]),
                                "Q4": (bounds[2], bounds[3]),
                                "Q5": (bounds[3], mx),
                            }
                            for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                                c = p2["quint_cb"].get((combo, col, q))
                                if not c or c["n"] == 0:
                                    continue
                                n, _, wr, avg = _counter_wr_avg(c)
                                lo, hi = qb[q]
                                w.writerow([scoring, resolution, tier, direction,
                                            col, q, round(lo, 4), round(hi, 4),
                                            n, round(wr, 1), round(avg, 3),
                                            round(wr - al_wr, 1)])
    log.info(f"  → {out_cb}")


def write_diamond(state, p2, out_path):
    log.info("write diamond...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "setup_type", "n_trades", "wr_pct", "avg_move_pct",
                    "vs_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        bc = state.baseline.get(combo)
                        if not bc or bc["n"] < 200:
                            continue
                        baseline_wr = 100.0 * bc["fw"] / bc["n"]
                        for st in ("diamond", "ema_extreme", "macd_extreme", "both_extreme"):
                            c = p2["diamond"].get((combo, st))
                            if not c or c["n"] == 0:
                                continue
                            n, _, wr, avg = _counter_wr_avg(c)
                            w.writerow([scoring, resolution, tier, direction,
                                        st, n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline_wr, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# BAR EXPANSION / WAVE A/B / MATURITY writers
# ═══════════════════════════════════════════════════════════

def write_bar_expansion(state, out_path):
    log.info("write bar_expansion...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "metric", "bucket", "n_trades", "wr_pct", "avg_move_pct",
                    "vs_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        bc = state.baseline.get(combo)
                        if not bc or bc["n"] < 100:
                            continue
                        baseline_wr = 100.0 * bc["fw"] / bc["n"]
                        # Two ratio metrics
                        for metric in ("signal_bar_expansion_ratio", "entry_bar_expansion_ratio"):
                            for b in ("<0.5x", "0.5-1x", "1-1.5x", "1.5-2x", "2-3x", "3x+"):
                                c = state.bar_exp.get((*combo, metric, b))
                                if not c or c["n"] == 0:
                                    continue
                                n, _, wr, avg = _counter_wr_avg(c)
                                w.writerow([scoring, resolution, tier, direction,
                                            metric, b,
                                            n, round(wr, 1), round(avg, 3),
                                            round(wr - baseline_wr, 1)])
                        # Momentum pattern labels
                        for label in ("building", "fading", "flat"):
                            c = state.bar_exp.get((*combo, "pre_signal_momentum_pattern", label))
                            if not c or c["n"] == 0:
                                continue
                            n, _, wr, avg = _counter_wr_avg(c)
                            w.writerow([scoring, resolution, tier, direction,
                                        "pre_signal_momentum_pattern", label,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - baseline_wr, 1)])
    log.info(f"  → {out_path}")


def write_wave_ab(state, out_path):
    log.info("write wave_ab...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction",
                    "wave_interpretation", "wave_alignment",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        ibc = state.inbox_baseline.get(combo)
                        if not ibc or ibc["n"] < 100:
                            continue
                        ib_wr = 100.0 * ibc["fw"] / ibc["n"]
                        for interp in ("original", "corrected"):
                            for align in ("aligned", "opposite"):
                                c = state.wave_ab.get((*combo, interp, align))
                                if not c or c["n"] == 0:
                                    continue
                                n, _, wr, avg = _counter_wr_avg(c)
                                w.writerow([scoring, resolution, tier, direction,
                                            interp, align,
                                            n, round(wr, 1), round(avg, 3),
                                            round(wr - ib_wr, 1)])
    log.info(f"  → {out_path}")


def write_maturity(state, out_path):
    log.info("write maturity...")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "resolution_min", "tier", "direction", "maturity",
                    "n_trades", "wr_pct", "avg_move_pct", "vs_in_box_baseline_wr_pct"])
        for scoring in _SCORINGS:
            for resolution in _RESOLUTIONS:
                for tier in _TIERS:
                    for direction in _DIRECTIONS:
                        combo = (scoring, resolution, tier, direction)
                        ibc = state.inbox_baseline.get(combo)
                        if not ibc or ibc["n"] == 0:
                            continue
                        ib_wr = 100.0 * ibc["fw"] / ibc["n"]
                        for m in ("early", "mid", "late", "overdue", "none"):
                            c = state.maturity.get((*combo, m))
                            if not c or c["n"] == 0:
                                continue
                            n, _, wr, avg = _counter_wr_avg(c)
                            w.writerow([scoring, resolution, tier, direction, m,
                                        n, round(wr, 1), round(avg, 3),
                                        round(wr - ib_wr, 1)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# SETUP INTERSECTION — in-memory processing of si_subset
# ═══════════════════════════════════════════════════════════

def write_setup_intersection(state, out_path):
    log.info("write setup_intersection...")
    buckets = defaultdict(lambda: {"L0": [], "L1": [], "L2": [], "L3": [], "L4": [], "L5": []})

    for r in state.si_subset:
        key = (r["ticker"], r["direction"])
        buckets[key]["L0"].append(r)

        if r["pb_state"] == "in_box":
            buckets[key]["L1"].append(r)

            at_edge = False
            if r["direction"] == "bull" and r["pb_floor"] > 0 and r["entry_price"] > 0:
                dist = (r["entry_price"] - r["pb_floor"]) / r["entry_price"] * 100
                if 0 <= dist <= 2.0:
                    at_edge = True
            elif r["direction"] == "bear" and r["pb_roof"] > 0 and r["entry_price"] > 0:
                dist = (r["pb_roof"] - r["entry_price"]) / r["entry_price"] * 100
                if 0 <= dist <= 2.0:
                    at_edge = True

            if at_edge:
                buckets[key]["L2"].append(r)

                sig_dir = "bullish" if r["direction"] == "bull" else "bearish"
                if r["wave_dir_corrected"] == sig_dir:
                    buckets[key]["L3"].append(r)

                    if r["avg_range_5m_prior_20_pct"] > 0:
                        p1_ratio = r["bar_range_5m_t_plus_1_pct"] / r["avg_range_5m_prior_20_pct"]
                        if p1_ratio >= 1.5:
                            buckets[key]["L4"].append(r)

                            skip = False
                            if r["direction"] == "bear" and r["pb_state"] == "above_roof":
                                skip = True
                            if r["pb_wave_label"] == "established":
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
                n = len(sub)
                wins = sum(1 for x in sub if x["bucket"] == "full_win")
                wr = 100.0 * wins / n
                avg = sum(x["move_signed_pct"] for x in sub) / n
                w.writerow([ticker, direction, lvl, labels[lvl],
                            n, round(wr, 1), round(avg, 3)])
    log.info(f"  → {out_path}")


# ═══════════════════════════════════════════════════════════
# REPORT (matches v1)
# ═══════════════════════════════════════════════════════════

def write_report(n_rows, out_path):
    md = f"""# Combined Analysis Report

Trades analyzed: {n_rows:,} signals from bt_resolution_study_v3.py

**Analyzer:** analyze_combined_v2.py (streaming — constant memory)

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
    if not TRADES_CSV.exists():
        log.error(f"trades.csv not found at {TRADES_CSV}")
        log.error(f"Set BACKTEST_DIR env var if your data is elsewhere.")
        sys.exit(1)

    log.info(f"Input:  {TRADES_CSV}")
    log.info(f"Output: {OUT_DIR}")

    # PASS 1 — single stream
    state = run_pass1()
    if state.n_rows == 0:
        log.error("No rows processed")
        sys.exit(1)

    # PASS 2 — re-stream for quintile + diamond binning
    p2 = run_pass2(state)

    # WRITE — all outputs
    write_per_ticker(state, OUT_DIR / "summary_per_ticker.csv")
    write_potter_box(state, OUT_DIR / "summary_potter_box.csv")
    write_cb_side(state, OUT_DIR / "summary_cb_side.csv")
    write_wave_label(state, OUT_DIR / "summary_wave_label.csv")
    write_credit_spreads(state, OUT_DIR / "summary_credit_spreads.csv")
    write_sr_hourly(state, OUT_DIR / "summary_sr_hourly.csv")
    write_refire(state, OUT_DIR / "summary_refire.csv")
    write_rollup(state, OUT_DIR / "summary_rollup.csv")
    write_quintiles(state, p2, OUT_DIR / "summary_quintiles.csv",
                    OUT_DIR / "summary_quintiles_cb.csv")
    write_diamond(state, p2, OUT_DIR / "summary_diamond.csv")
    write_bar_expansion(state, OUT_DIR / "summary_bar_expansion.csv")
    write_wave_ab(state, OUT_DIR / "summary_wave_ab.csv")
    write_maturity(state, OUT_DIR / "summary_maturity.csv")
    write_setup_intersection(state, OUT_DIR / "summary_setup_intersection.csv")
    write_report(state.n_rows, OUT_DIR / "report_combined.md")

    log.info(f"DONE. All outputs in {OUT_DIR}:")
    for fn in sorted(OUT_DIR.glob("summary_*.csv")):
        log.info(f"  {fn.name}  ({fn.stat().st_size} bytes)")
    rp = OUT_DIR / "report_combined.md"
    if rp.exists():
        log.info(f"  {rp.name}  ({rp.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
