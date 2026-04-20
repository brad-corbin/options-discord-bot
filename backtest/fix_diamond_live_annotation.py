#!/usr/bin/env python3
"""fix_diamond_live_annotation.py
═══════════════════════════════════════════════════════════════════
v8.3 Phase 3 post-annotation fix.

Standalone recovery script for the PosixPath bug in the Phase 3 backtest.
Reads diamond_live_bounds.json (already produced), then rewrites trades.csv
with diamond_live populated per-row. No backtest rerun required.

Usage (Render shell):
    cd /opt/render/project/src
    python3 fix_diamond_live_annotation.py

Inputs:
    /tmp/backtest_resolution/trades.csv
    /tmp/backtest_resolution/diamond_live_bounds.json

Output:
    /tmp/backtest_resolution/trades.csv  (rewritten in place)

Runtime: ~1-2 minutes on 572K rows.
═══════════════════════════════════════════════════════════════════
"""
import csv
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fix_diamond")

TRADES_PATH = "/tmp/backtest_resolution/trades.csv"
BOUNDS_PATH = "/tmp/backtest_resolution/diamond_live_bounds.json"
TMP_PATH    = "/tmp/backtest_resolution/trades.csv.tmp"

_MIDDLE_QUINTILES = {"Q2", "Q3", "Q4"}


def _assign_quintile(value, bounds):
    """Q1..Q5 from value given [q20, q40, q60, q80]. Strict < comparisons."""
    if not bounds or len(bounds) < 4:
        return "unknown"
    if value < bounds[0]: return "Q1"
    if value < bounds[1]: return "Q2"
    if value < bounds[2]: return "Q3"
    if value < bounds[3]: return "Q4"
    return "Q5"


def main():
    # Verify inputs exist
    if not os.path.exists(TRADES_PATH):
        log.error(f"trades.csv not found: {TRADES_PATH}")
        return 1
    if not os.path.exists(BOUNDS_PATH):
        log.error(f"bounds JSON not found: {BOUNDS_PATH}")
        return 1

    # Load bounds. JSON structure:
    # { "active_scanner:5m:T1:bull": {"ema_diff_pct": [q20, q40, q60, q80], "macd_hist": [...]}, ... }
    with open(BOUNDS_PATH, "r") as f:
        bounds_raw = json.load(f)

    # Build per-combo bounds dict keyed by (scoring, resolution, tier, direction)
    combo_bounds = {}
    for combo_key, indicator_dict in bounds_raw.items():
        # Parse key: "active_scanner:5m:T1:bull" → ("active_scanner", "5", "1", "bull")
        try:
            parts = combo_key.split(":")
            if len(parts) != 4:
                continue
            scoring = parts[0]
            resolution = parts[1].rstrip("m")
            tier = parts[2].lstrip("T")
            direction = parts[3]
            key = (scoring, resolution, tier, direction)
            ema_b = indicator_dict.get("ema_diff_pct")
            macd_b = indicator_dict.get("macd_hist")
            if ema_b and macd_b:
                combo_bounds[key] = (ema_b, macd_b)
        except Exception as e:
            log.warning(f"skipped combo_key {combo_key}: {e}")

    log.info(f"Loaded bounds for {len(combo_bounds)} combos")

    # Pass 2: rewrite CSV
    diamond_total = 0
    row_total = 0
    no_bounds = 0

    with open(TRADES_PATH, "r", newline="") as fin, \
         open(TMP_PATH, "w", newline="") as fout:
        reader = csv.DictReader(fin)
        fields = reader.fieldnames
        if "diamond_live" not in fields:
            log.error("diamond_live column not in trades.csv header")
            return 1

        writer = csv.DictWriter(fout, fieldnames=fields)
        writer.writeheader()

        for row in reader:
            row_total += 1
            key = (
                row.get("scoring_system", ""),
                row.get("resolution_min", ""),
                row.get("tier", ""),
                row.get("direction", ""),
            )
            bounds = combo_bounds.get(key)
            is_diamond = False
            if bounds:
                ema_b, macd_b = bounds
                try:
                    ema_val = float(row.get("ind_ema_diff_pct", 0))
                    macd_val = float(row.get("ind_macd_hist", 0))
                    ema_q = _assign_quintile(ema_val, ema_b)
                    macd_q = _assign_quintile(macd_val, macd_b)
                    if ema_q in _MIDDLE_QUINTILES and macd_q in _MIDDLE_QUINTILES:
                        is_diamond = True
                        diamond_total += 1
                except (ValueError, TypeError):
                    pass
            else:
                no_bounds += 1

            row["diamond_live"] = "True" if is_diamond else "False"
            writer.writerow(row)

            if row_total % 50000 == 0:
                log.info(f"  ...processed {row_total:,} rows "
                         f"({diamond_total:,} diamonds so far)")

    # Atomic swap
    os.replace(TMP_PATH, TRADES_PATH)

    pct = 100.0 * diamond_total / row_total if row_total else 0.0
    log.info(f"DONE: {diamond_total:,}/{row_total:,} rows marked diamond_live ({pct:.1f}%)")
    if no_bounds:
        log.warning(f"  {no_bounds:,} rows had no matching combo bounds — diamond_live=False")

    return 0


if __name__ == "__main__":
    sys.exit(main())
