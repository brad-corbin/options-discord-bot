#!/usr/bin/env python3
# extract_fallback_bounds.py
# ═══════════════════════════════════════════════════════════════════
# v8.3 Phase 3e: Extract real quintile boundaries from the 621K backtest
# and emit a Python dict ready to paste into quintile_store.FALLBACK_BOUNDS.
#
# Usage (on Render where /var/backtest/ lives):
#   python3 extract_fallback_bounds.py \
#       --input /var/backtest/summary_quintiles_cb.csv \
#       --output /home/claude/fallback_bounds_generated.py
#
# Options:
#   --input    Path to summary CSV. Defaults to CB-aligned version.
#   --output   Python file to write. Prints to stdout if omitted.
#   --min-n    Minimum n_trades per (combo, indicator, quintile) row to keep.
#              Default 50 — below this the quintile statistics are noisy.
#   --no-cb-only  Use the unfiltered summary_quintiles.csv instead. Default
#                 is summary_quintiles_cb.csv which only includes CB-aligned
#                 signals (the population the scorer will see in production
#                 once G1 gating is active).
#
# Output format matches quintile_store.FALLBACK_BOUNDS shape:
#   {
#     "<scoring>:<resolution>m:T<tier>:<direction>": {
#       "<indicator>": [q20, q40, q60, q80],   # 4 breakpoints
#       ...
#     },
#     ...
#   }
#
# Quintile boundary extraction logic:
#   For each (scoring, resolution, tier, direction, indicator) group,
#   the backtest wrote 5 rows labeled Q1-Q5 with lower_bound / upper_bound.
#   The 4 breakpoints that define bucket assignment are:
#     q20 = Q2.lower_bound  (= Q1.upper_bound)
#     q40 = Q3.lower_bound
#     q60 = Q4.lower_bound
#     q80 = Q5.lower_bound
#   This matches _assign_quintile in quintile_store.py (strict < comparisons).
#
# Indicator column mapping (backtest ind_* → scorer names):
#   ind_ema_diff_pct → ema_diff_pct
#   ind_macd_hist    → macd_hist
#   ind_rsi          → rsi
#   ind_wt2          → wt2
#   ind_adx          → adx
#
# ═══════════════════════════════════════════════════════════════════

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from typing import Dict, List

log = logging.getLogger(__name__)


# Map backtest column names to scorer indicator keys
INDICATOR_MAP = {
    "ind_ema_diff_pct": "ema_diff_pct",
    "ind_macd_hist":    "macd_hist",
    "ind_rsi":          "rsi",
    "ind_wt2":          "wt2",
    "ind_adx":          "adx",
}


def extract_bounds(
    input_path: str,
    min_n: int = 50,
) -> Dict[str, Dict[str, List[float]]]:
    """Read the summary quintiles CSV and return a FALLBACK_BOUNDS-shape dict.

    Groups rows by (scoring, resolution, tier, direction, indicator), then for
    each group extracts the four quintile breakpoints from Q2/Q3/Q4/Q5.lower_bound.
    """
    # Raw rows grouped by combo+indicator for structural extraction
    # key: (scoring, resolution, tier, direction, indicator)
    # value: dict mapping quintile label → {lower, upper, n}
    groups: Dict[tuple, Dict[str, Dict]] = defaultdict(dict)

    rows_read = 0
    rows_kept = 0
    rows_dropped_low_n = 0
    rows_dropped_bad_indicator = 0

    with open(input_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        required_cols = {"scoring", "resolution_min", "tier", "direction",
                         "indicator", "quintile", "lower_bound", "upper_bound",
                         "n_trades"}
        missing = required_cols - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Input CSV missing required columns: {missing}. "
                f"Got: {reader.fieldnames}"
            )

        for row in reader:
            rows_read += 1

            scoring = row["scoring"].strip().lower()
            resolution = row["resolution_min"].strip()
            tier = row["tier"].strip()
            direction = row["direction"].strip().lower()
            indicator_raw = row["indicator"].strip()
            quintile = row["quintile"].strip().upper()

            # Map indicator to scorer key
            indicator = INDICATOR_MAP.get(indicator_raw)
            if indicator is None:
                rows_dropped_bad_indicator += 1
                continue

            try:
                n_trades = int(row["n_trades"])
                lower = float(row["lower_bound"])
                upper = float(row["upper_bound"])
            except (ValueError, TypeError):
                continue

            if n_trades < min_n:
                rows_dropped_low_n += 1
                continue

            if quintile not in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                continue

            key = (scoring, resolution, tier, direction, indicator)
            groups[key][quintile] = {
                "lower": lower,
                "upper": upper,
                "n": n_trades,
            }
            rows_kept += 1

    log.info(f"Read {rows_read} rows, kept {rows_kept}, "
             f"dropped {rows_dropped_low_n} low-n, "
             f"dropped {rows_dropped_bad_indicator} unmapped-indicator")

    # Build FALLBACK_BOUNDS output shape
    bounds: Dict[str, Dict[str, List[float]]] = {}
    incomplete_groups = 0

    for (scoring, resolution, tier, direction, indicator), quintiles in groups.items():
        # Need all 5 quintiles to extract 4 breakpoints
        if not all(q in quintiles for q in ("Q1", "Q2", "Q3", "Q4", "Q5")):
            incomplete_groups += 1
            log.debug(f"Incomplete group {(scoring, resolution, tier, direction, indicator)}: "
                      f"only has {sorted(quintiles.keys())}")
            continue

        # Breakpoints: Q2.lower = q20, Q3.lower = q40, Q4.lower = q60, Q5.lower = q80
        breakpoints = [
            quintiles["Q2"]["lower"],
            quintiles["Q3"]["lower"],
            quintiles["Q4"]["lower"],
            quintiles["Q5"]["lower"],
        ]

        # Sanity: breakpoints must be monotonic non-decreasing
        if not all(breakpoints[i] <= breakpoints[i+1] for i in range(3)):
            log.warning(f"Non-monotonic breakpoints for "
                        f"{(scoring, resolution, tier, direction, indicator)}: {breakpoints}")
            continue

        # Build the combo key in quintile_store format: scoring:Nm:T#:direction
        # Normalize tier: "1" → "T1"
        tier_key = f"T{tier}" if not tier.startswith("T") else tier
        combo_key = f"{scoring}:{resolution}m:{tier_key}:{direction}"

        if combo_key not in bounds:
            bounds[combo_key] = {}
        bounds[combo_key][indicator] = [round(b, 4) for b in breakpoints]

    log.info(f"Built {len(bounds)} combos "
             f"(skipped {incomplete_groups} incomplete groups)")

    return bounds


def emit_python_source(bounds: Dict[str, Dict[str, List[float]]],
                       input_path: str,
                       min_n: int) -> str:
    """Render bounds dict as a Python source file ready to paste into quintile_store.py."""
    header = f'''# ══════════════════════════════════════════════════════════════════
# FALLBACK_BOUNDS — generated by extract_fallback_bounds.py
# Source: {input_path}
# Filter: n_trades >= {min_n} per (combo, indicator, quintile)
# Combos: {len(bounds)}
# Generated: see Phase 3e extraction log for date
# Replace the placeholder FALLBACK_BOUNDS in quintile_store.py with this dict.
# ══════════════════════════════════════════════════════════════════

FALLBACK_BOUNDS = '''

    # Use json.dumps with sort_keys for deterministic output
    body = json.dumps(bounds, indent=4, sort_keys=True)
    # JSON uses double quotes which is valid Python dict literal syntax

    return header + body + "\n"


def main():
    p = argparse.ArgumentParser(description="Extract FALLBACK_BOUNDS from backtest CSV")
    p.add_argument("--input", default="/var/backtest/summary_quintiles_cb.csv",
                   help="Path to summary quintiles CSV")
    p.add_argument("--output", default=None,
                   help="Python source file to write (prints to stdout if omitted)")
    p.add_argument("--min-n", type=int, default=50,
                   help="Minimum n_trades per row to include (default 50)")
    p.add_argument("--no-cb-only", action="store_true",
                   help="Use summary_quintiles.csv instead of _cb.csv")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    input_path = args.input
    if args.no_cb_only and "summary_quintiles_cb.csv" in input_path:
        input_path = input_path.replace("summary_quintiles_cb.csv", "summary_quintiles.csv")
        log.info(f"Using non-CB filtered source: {input_path}")

    try:
        bounds = extract_bounds(input_path, min_n=args.min_n)
    except FileNotFoundError:
        log.error(f"Input CSV not found: {input_path}")
        log.error("On Render, the backtest outputs are at /var/backtest/")
        return 2
    except Exception as e:
        log.error(f"Extraction failed: {e}")
        return 1

    if not bounds:
        log.error("No combos extracted — check input file")
        return 1

    output = emit_python_source(bounds, input_path, args.min_n)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        log.info(f"Wrote {len(bounds)} combos to {args.output}")

        # Quick sanity report
        total_indicators = sum(len(v) for v in bounds.values())
        log.info(f"Total indicator-level entries: {total_indicators}")
        # Show one sample combo so a reviewer can eyeball the output
        sample_key = sorted(bounds.keys())[0]
        log.info(f"Sample: {sample_key} → {bounds[sample_key]}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
