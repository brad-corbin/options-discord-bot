#!/usr/bin/env python3
"""run_phase4_scorer_validation.py
═══════════════════════════════════════════════════════════════════
v8.3 Phase 4 — Scorer revalidation against Phase 3 trades.csv

For every row in trades.csv, reconstruct a scanner_event + context_snapshot
equivalent to what the live scorer would see, then call score_signal().
Compare WR for (score >= threshold) vs unfiltered baseline per segment.

Memory-safe: streams row-by-row, never loads full CSV.

Usage (Render shell):
    cd /opt/render/project/src
    python3 run_phase4_scorer_validation.py

Inputs:
    /tmp/backtest_resolution/trades.csv  (572K rows, 35 tickers, annotated)

Outputs:
    /tmp/backtest_resolution/phase4_scorer_validation.csv
    /tmp/backtest_resolution/phase4_summary.txt
═══════════════════════════════════════════════════════════════════
"""
import csv
import logging
import os
import sys
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phase4")

TRADES_PATH = "/tmp/backtest_resolution/trades.csv"
VALIDATION_PATH = "/tmp/backtest_resolution/phase4_scorer_validation.csv"
SUMMARY_PATH = "/tmp/backtest_resolution/phase4_summary.txt"

POST_THRESHOLD = 70  # score >= this → "would post"

# Make sure we're importing from the repo
BOT_REPO_PATH = "/opt/render/project/src"
if BOT_REPO_PATH not in sys.path:
    sys.path.insert(0, BOT_REPO_PATH)


def _bool(s):
    return str(s).strip().lower() in ("true", "1", "yes")


def _float(s, default=0.0):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _int(s, default=0):
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


def build_scanner_event(row):
    """Build the scanner_event dict (what active_scanner's _enqueue_signal passes).

    Shape matches what score_signal expects to read (ticker, bias).
    """
    return {
        "ticker": row.get("ticker", ""),
        "bias": row.get("direction", ""),
        "webhook_data": {
            "source": row.get("scoring_system", ""),
            "timeframe": row.get("resolution_min", "5"),
            "tier": str(row.get("tier", "")),
            "score": _int(row.get("score", 0)),
            "ema_dist_pct": _float(row.get("ind_ema_diff_pct", 0)),
            "macd_hist": _float(row.get("ind_macd_hist", 0)),
            "rsi_mfi": _float(row.get("ind_rsi", 50)),
            "wt2": _float(row.get("ind_wt2", 0)),
            "adx": _float(row.get("ind_adx", 0)),
            "above_vwap": _bool(row.get("above_vwap", "False")),
            "htf_status": row.get("htf_status", "UNKNOWN"),
        },
    }


def build_context_snapshot(row):
    """Build the context_snapshot dict (what _build_context_snapshot produces).

    All fields from Phase 2e context builder, pulled from the row.
    """
    ctx = {
        "ticker": row.get("ticker", ""),
        "direction": row.get("direction", ""),
        "scoring_source": row.get("scoring_system", ""),
        "timeframe": f"{row.get('resolution_min', '5')}m",
        "tier": f"T{row.get('tier', '2')}",
        # Potter Box fields
        "pb_state": row.get("pb_state", "no_box"),
        "wave_label": row.get("pb_wave_label", ""),
        "wave_dir_original": row.get("wave_dir_original", ""),
        "maturity": row.get("pb_maturity", ""),
        # cb_side only populated when in_box (matches live _build_context_snapshot)
        "cb_side": row.get("cb_side", "n/a") if row.get("pb_state") == "in_box" else "",
        # at_edge — from Phase 3b column
        "at_edge": _bool(row.get("at_edge", "False")),
        # SR fields — not in Phase 3 CSV directly, use defaults
        "fractal_resistance_above_spot_pct": _float(row.get("sr_h_fractal_dist_above_pct", 999)),
        "fractal_support_below_spot_pct": _float(row.get("sr_h_fractal_dist_below_pct", 999)),
        "pivot_resistance_above_spot_pct": _float(row.get("sr_h_pivot_dist_above_pct", 999)),
        "pivot_support_below_spot_pct": _float(row.get("sr_h_pivot_dist_below_pct", 999)),
        # diamond — native from Phase 3c
        "diamond": _bool(row.get("diamond_live", "False")),
        # quintile fields — not in CSV per-row; scorer rules that require
        # them will get "unknown" and skip gracefully
        "ema_diff_quintile": "unknown",
        "macd_hist_quintile": "unknown",
        "rsi_quintile": "unknown",
        "wt2_quintile": "unknown",
        "adx_quintile": "unknown",
        # recent_flow — B12 off in v8.3
        "recent_flow": None,
    }
    return ctx


def main():
    if not os.path.exists(TRADES_PATH):
        log.error(f"trades.csv not found: {TRADES_PATH}")
        return 1

    # Import scorer — use the v8.3 (Phase 2f+2g) version, NOT the live scorer.
    # Phase 4 measures what lift the NEW scorer produces. Live scorer stays untouched.
    try:
        from conviction_scorer_v83 import score_signal
    except ImportError as e:
        log.error(f"conviction_scorer_v83 import failed: {e}")
        log.error("Expected file: /opt/render/project/src/conviction_scorer_v83.py")
        log.error("Copy the Phase 2f+2g conviction_scorer.py to that path.")
        return 1

    log.info(f"Loading {TRADES_PATH} (streaming)...")

    # Counters per segment
    # key: (scoring, resolution, tier, direction)
    # value: {n_total, n_filter_passed, n_score_ge_thresh, wins_*, partials_*}
    stats = defaultdict(lambda: {
        "total": 0,
        "total_wins": 0,
        "filter_passed": 0,
        "filter_passed_wins": 0,
        "score_ge_thresh": 0,
        "score_ge_thresh_wins": 0,
        "filter_and_score_ge_thresh": 0,
        "filter_and_score_ge_thresh_wins": 0,
        "discarded_hard_gate": 0,
    })

    # Global
    total = 0
    scorer_errors = 0
    row_count = 0

    with open(TRADES_PATH, "r", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            row_count += 1
            if row_count % 50000 == 0:
                log.info(f"  ...processed {row_count:,} rows")

            scoring = row.get("scoring_system", "")
            resolution = row.get("resolution_min", "")
            tier = row.get("tier", "")
            direction = row.get("direction", "")
            key = (scoring, resolution, tier, direction)
            s = stats[key]

            filter_passed = _bool(row.get("scanner_filters_passed", "False"))
            win = _bool(row.get("win_headline", "False"))

            s["total"] += 1
            if win:
                s["total_wins"] += 1
            if filter_passed:
                s["filter_passed"] += 1
                if win:
                    s["filter_passed_wins"] += 1

            # Score the signal
            try:
                event = build_scanner_event(row)
                ctx = build_context_snapshot(row)
                result = score_signal(event, ctx)

                if result.decision == "discard":
                    s["discarded_hard_gate"] += 1

                if result.score >= POST_THRESHOLD:
                    s["score_ge_thresh"] += 1
                    if win:
                        s["score_ge_thresh_wins"] += 1
                    if filter_passed:
                        s["filter_and_score_ge_thresh"] += 1
                        if win:
                            s["filter_and_score_ge_thresh_wins"] += 1

            except Exception as e:
                scorer_errors += 1
                if scorer_errors <= 5:
                    log.warning(f"scorer error on row {row_count}: {e}")

    log.info(f"Processed {row_count:,} rows ({scorer_errors} scorer errors)")

    # Write validation CSV
    log.info(f"Writing {VALIDATION_PATH}...")
    with open(VALIDATION_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "scoring_system", "resolution_min", "tier", "direction",
            "n_total", "wr_baseline_pct",
            "n_filter_passed", "wr_filter_passed_pct",
            "n_score_ge_70", "wr_score_ge_70_pct",
            "n_filter_and_score_ge_70", "wr_filter_and_score_ge_70_pct",
            "lift_vs_baseline_pct", "n_discarded_hard_gate",
        ])
        for key in sorted(stats.keys()):
            scoring, resolution, tier, direction = key
            s = stats[key]
            wr_baseline = 100.0 * s["total_wins"] / s["total"] if s["total"] else 0.0
            wr_filter = 100.0 * s["filter_passed_wins"] / s["filter_passed"] if s["filter_passed"] else 0.0
            wr_score = 100.0 * s["score_ge_thresh_wins"] / s["score_ge_thresh"] if s["score_ge_thresh"] else 0.0
            wr_both = 100.0 * s["filter_and_score_ge_thresh_wins"] / s["filter_and_score_ge_thresh"] if s["filter_and_score_ge_thresh"] else 0.0
            lift = wr_both - wr_baseline
            w.writerow([
                scoring, resolution, tier, direction,
                s["total"], f"{wr_baseline:.1f}",
                s["filter_passed"], f"{wr_filter:.1f}",
                s["score_ge_thresh"], f"{wr_score:.1f}",
                s["filter_and_score_ge_thresh"], f"{wr_both:.1f}",
                f"{lift:+.1f}", s["discarded_hard_gate"],
            ])

    # Summary headline
    total_rows = sum(s["total"] for s in stats.values())
    total_wins = sum(s["total_wins"] for s in stats.values())
    total_filter = sum(s["filter_passed"] for s in stats.values())
    total_filter_wins = sum(s["filter_passed_wins"] for s in stats.values())
    total_both = sum(s["filter_and_score_ge_thresh"] for s in stats.values())
    total_both_wins = sum(s["filter_and_score_ge_thresh_wins"] for s in stats.values())

    wr_baseline = 100.0 * total_wins / total_rows if total_rows else 0.0
    wr_filter = 100.0 * total_filter_wins / total_filter if total_filter else 0.0
    wr_both = 100.0 * total_both_wins / total_both if total_both else 0.0
    lift = wr_both - wr_baseline

    summary_lines = [
        "=" * 60,
        "PHASE 4 SCORER VALIDATION — HEADLINE",
        "=" * 60,
        f"Total rows processed:              {total_rows:>10,}",
        f"Scorer errors:                     {scorer_errors:>10,}",
        "",
        f"Baseline WR (all trades):          {wr_baseline:>9.2f}%   n={total_rows:,}",
        f"Filtered WR (scanner filters only): {wr_filter:>9.2f}%   n={total_filter:,}",
        f"Filtered + score>=70 WR:           {wr_both:>9.2f}%   n={total_both:,}",
        "",
        f"LIFT vs baseline (headline):       {lift:+.2f} WR",
        f"  Target: +8 WR or better for deploy",
        "",
        f"Detail: {VALIDATION_PATH}",
        "=" * 60,
    ]

    summary = "\n".join(summary_lines)
    with open(SUMMARY_PATH, "w") as f:
        f.write(summary + "\n")

    print("\n" + summary)
    log.info(f"Summary → {SUMMARY_PATH}")
    log.info(f"Detail → {VALIDATION_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
