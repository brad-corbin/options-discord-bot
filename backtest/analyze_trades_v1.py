#!/usr/bin/env python3
"""
analyze_trades_v1.py — Deep analysis of bt_resolution_study trades.csv
═══════════════════════════════════════════════════════════════════════

Reads the 572K-signal trades.csv produced by bt_resolution_study.py and
runs three analyses that the existing output CSVs don't cover:

1. PER-TICKER FINDINGS
   Which specific tickers carry or destroy the edge. Output includes:
   - WR per (ticker, scoring, resolution, tier, direction)
   - Best/worst tickers overall
   - Tickers that swing WR significantly between directions (bull-biased vs
     bear-biased) — candidates for long-only or short-only watchlists

2. RE-FIRE ANALYSIS
   When a ticker fires multiple times in the same ISO week, what happens?
   - Count distribution (how often is a week 1, 2, 3, 4+ fires?)
   - WR of the FIRST fire given total fires that week (does knowing more
     fires follow change the value of entering the first one?)
   - WR by fire position within the week (1st vs 2nd vs 3rd vs later)
   - Conditional: WR of fire N given fire N-1 was open

3. ROLL-UP SIMULATION
   For paired re-fires within the same week, compute both:
   - "Hold both": two independent positions, sum PnL at Friday close
   - "Roll up":   close position 1 at fire-2's entry price, open new
                  position sized identically at fire-2's entry, realize
                  combined PnL at Friday close
   Reports WR and avg PnL for each approach to answer: "should I roll
   my Monday entry up when Tuesday fires at a higher price?"

USAGE (Render shell):
    cd /opt/render/project/src
    python backtest/analyze_trades_v1.py

READS:  /tmp/backtest_resolution/trades.csv   (572K+ rows)
WRITES: /tmp/backtest_resolution/
        summary_per_ticker.csv
        summary_refire.csv
        summary_rollup.csv
        report_analysis.md

NO RE-BACKTESTING. Runs in <60 seconds.
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
log = logging.getLogger("analyze_trades")

IN_DIR = Path(os.environ.get("BACKTEST_DIR", "/tmp/backtest_resolution"))
TRADES_CSV = IN_DIR / "trades.csv"
OUT_DIR = IN_DIR


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


def load_trades():
    if not TRADES_CSV.exists():
        log.error(f"trades.csv not found at {TRADES_CSV}")
        sys.exit(1)

    log.info(f"Loading {TRADES_CSV}...")
    rows: list[Row] = []
    with open(TRADES_CSV) as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                rows.append(Row(
                    ticker=r["ticker"],
                    resolution_min=int(float(r["resolution_min"])),
                    scoring_system=r["scoring_system"],
                    tier=int(float(r["tier"])),
                    direction=r["direction"],
                    signal_ts=int(float(r["signal_ts"])),
                    entry_ts=int(float(r["entry_ts"])),
                    entry_price=float(r["entry_price"]),
                    exit_ts=int(float(r["exit_ts"])),
                    exit_price=float(r["exit_price"]),
                    move_signed_pct=float(r["move_signed_pct"]),
                    bucket=r["bucket"],
                    win_headline=r["win_headline"].lower() in ("true", "1"),
                    regime_trend=r["regime_trend"],
                ))
            except (ValueError, KeyError) as e:
                log.debug(f"Skipping malformed row: {e}")
                continue
    log.info(f"Loaded {len(rows):,} trades")
    return rows


# ═══════════════════════════════════════════════════════════
# ANALYSIS 1: PER-TICKER FINDINGS
# ═══════════════════════════════════════════════════════════

def analyze_per_ticker(rows, out_path):
    """Breakdown WR per (ticker, scoring, resolution, tier, direction).

    Output CSV includes a 'significance' marker — samples under 30 get
    flagged as "small_sample" since WR swings wildly at that size.
    """
    g = defaultdict(list)
    for r in rows:
        g[(r.ticker, r.scoring_system, r.resolution_min, r.tier, r.direction)].append(r)

    log.info(f"Per-ticker: {len(g)} unique combos")

    results = []
    for key, ts in g.items():
        ticker, scoring, resolution, tier, direction = key
        n = len(ts)
        if n == 0:
            continue
        wins = sum(1 for t in ts if t.win_headline)
        full_wins = sum(1 for t in ts if t.bucket == "full_win")
        partials = sum(1 for t in ts if t.bucket == "partial")
        full_losses = sum(1 for t in ts if t.bucket == "full_loss")
        avg_move = sum(t.move_signed_pct for t in ts) / n
        wr = 100 * full_wins / n
        wr_with_partials = 100 * (full_wins + partials) / n

        sig = "ok" if n >= 100 else ("small_sample" if n >= 30 else "tiny_sample")

        results.append({
            "ticker": ticker, "scoring": scoring, "resolution_min": resolution,
            "tier": tier, "direction": direction, "n_trades": n,
            "full_win": full_wins, "partial": partials, "full_loss": full_losses,
            "wr_pct": round(wr, 1), "wr_with_partials_pct": round(wr_with_partials, 1),
            "avg_move_pct": round(avg_move, 3), "significance": sig,
        })

    # Sort: by scoring, then ticker, then resolution, then tier, then direction
    results.sort(key=lambda x: (x["scoring"], x["ticker"], x["resolution_min"],
                                x["tier"], x["direction"]))

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        for r in results:
            w.writerow(r)

    # Identify top/bottom per scoring (at 15m resolution, which is most reliable)
    findings = {"best_bulls": [], "worst_bulls": [],
                "best_bears": [], "worst_bears": [],
                "directional_bias": []}

    # Per-ticker aggregate at 15m active_scanner
    for scoring in ("active_scanner", "pinescript"):
        per_ticker_15m = defaultdict(lambda: {"bull": None, "bear": None})
        for r in results:
            if r["scoring"] != scoring or r["resolution_min"] != 15 or r["tier"] != 2:
                continue
            if r["significance"] == "tiny_sample":
                continue
            per_ticker_15m[r["ticker"]][r["direction"]] = r

        # Best/worst bulls — dedup so "best" and "worst" don't overlap
        bull_entries = [(t, d["bull"]) for t, d in per_ticker_15m.items()
                        if d["bull"] is not None]
        bull_entries.sort(key=lambda x: x[1]["wr_pct"], reverse=True)

        n_bull = len(bull_entries)
        top_n = min(5, max(1, n_bull // 2))  # never overlap with bottom

        for t, r in bull_entries[:top_n]:
            findings["best_bulls"].append(
                f"{scoring} T2 15m {t}: {r['wr_pct']}% ({r['n_trades']} trades, avg {r['avg_move_pct']:+.2f}%)"
            )
        for t, r in bull_entries[-top_n:][::-1]:   # reverse so worst is first
            findings["worst_bulls"].append(
                f"{scoring} T2 15m {t}: {r['wr_pct']}% ({r['n_trades']} trades, avg {r['avg_move_pct']:+.2f}%)"
            )

        bear_entries = [(t, d["bear"]) for t, d in per_ticker_15m.items()
                        if d["bear"] is not None]
        bear_entries.sort(key=lambda x: x[1]["wr_pct"], reverse=True)

        n_bear = len(bear_entries)
        top_n_bear = min(5, max(1, n_bear // 2))

        for t, r in bear_entries[:top_n_bear]:
            findings["best_bears"].append(
                f"{scoring} T2 15m {t}: {r['wr_pct']}% ({r['n_trades']} trades, avg {r['avg_move_pct']:+.2f}%)"
            )
        for t, r in bear_entries[-top_n_bear:][::-1]:
            findings["worst_bears"].append(
                f"{scoring} T2 15m {t}: {r['wr_pct']}% ({r['n_trades']} trades, avg {r['avg_move_pct']:+.2f}%)"
            )

        # Directional bias: tickers where bull-WR and bear-WR differ by 15+ points
        for t, d in per_ticker_15m.items():
            if d["bull"] is None or d["bear"] is None:
                continue
            bull_wr = d["bull"]["wr_pct"]
            bear_wr = d["bear"]["wr_pct"]
            diff = bull_wr - bear_wr
            if abs(diff) >= 15:
                side = "bull-biased" if diff > 0 else "bear-biased"
                findings["directional_bias"].append(
                    f"{scoring} {t}: bull {bull_wr}% vs bear {bear_wr}% "
                    f"(Δ {diff:+.1f}) — {side}"
                )

    log.info(f"Per-ticker analysis complete → {out_path}")
    return findings


# ═══════════════════════════════════════════════════════════
# ANALYSIS 2: RE-FIRE ANALYSIS
# ═══════════════════════════════════════════════════════════

def analyze_refires(rows, out_path):
    """Group trades by (ticker, scoring, resolution, tier, direction, ISO week).

    Compute:
      - Distribution of fires-per-week counts
      - WR by total fires that week (grouped on first fire)
      - WR by fire position in week
      - Conditional WR: given fire N-1 was in the same week, does fire N win?
    """
    # Build (key, week) → list of rows, sorted by signal_ts
    week_groups = defaultdict(list)
    for r in rows:
        dt = datetime.fromtimestamp(r.signal_ts)
        iso_y, iso_w, _ = dt.isocalendar()
        key = (r.ticker, r.scoring_system, r.resolution_min, r.tier,
               r.direction, iso_y, iso_w)
        week_groups[key].append(r)

    for key in week_groups:
        week_groups[key].sort(key=lambda x: x.signal_ts)

    log.info(f"Re-fire groups: {len(week_groups):,} ticker-week buckets")

    # For each row, determine (fires_count_in_week, fire_position_in_week)
    # position is 1-indexed: first fire = 1, second = 2, etc.
    fires_info = {}   # row index → (count, position)
    for key, group in week_groups.items():
        total = len(group)
        for pos, r in enumerate(group, start=1):
            fires_info[id(r)] = (total, pos)

    # ─── Distribution: how often does a week have N fires? ───
    # Per (scoring, tier, direction) — all resolutions combined for this aggregate
    fire_count_dist = defaultdict(lambda: defaultdict(int))
    for key, group in week_groups.items():
        _, scoring, resolution, tier, direction, _, _ = key
        if resolution != 15:  # only aggregate at 15m to keep counts comparable
            continue
        fires_count = len(group)
        bucket = str(fires_count) if fires_count <= 3 else "4+"
        fire_count_dist[(scoring, tier, direction)][bucket] += 1

    # ─── WR by total fires that week (taken from FIRST fire) ───
    first_fire_wr = defaultdict(lambda: defaultdict(list))
    for key, group in week_groups.items():
        _, scoring, resolution, tier, direction, _, _ = key
        if resolution != 15:
            continue
        fires = len(group)
        bucket = str(fires) if fires <= 3 else "4+"
        first = group[0]  # first fire of the week
        first_fire_wr[(scoring, tier, direction)][bucket].append(first)

    # ─── WR by fire POSITION in week ───
    position_wr = defaultdict(lambda: defaultdict(list))
    for key, group in week_groups.items():
        _, scoring, resolution, tier, direction, _, _ = key
        if resolution != 15:
            continue
        for pos, r in enumerate(group, start=1):
            bucket = str(pos) if pos <= 3 else "4+"
            position_wr[(scoring, tier, direction)][bucket].append(r)

    # ─── Write CSV ───
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["analysis", "scoring", "tier", "direction", "bucket",
                    "n_trades", "wr_pct", "avg_move_pct"])

        # Fire-count distribution (how many weeks have N fires?)
        for (scoring, tier, direction), bkts in sorted(fire_count_dist.items()):
            for b in ("1", "2", "3", "4+"):
                n = bkts.get(b, 0)
                if n == 0:
                    continue
                w.writerow(["fires_in_week_distribution", scoring, tier, direction,
                            b, n, "", ""])

        # WR of FIRST fire given total fires that week
        for (scoring, tier, direction), bkts in sorted(first_fire_wr.items()):
            for b in ("1", "2", "3", "4+"):
                subset = bkts.get(b, [])
                if not subset:
                    continue
                wins = sum(1 for x in subset if x.bucket == "full_win")
                avg = sum(x.move_signed_pct for x in subset) / len(subset)
                wr = 100 * wins / len(subset)
                w.writerow(["first_fire_given_total_fires", scoring, tier, direction,
                            b, len(subset), round(wr, 1), round(avg, 3)])

        # WR by fire position
        for (scoring, tier, direction), bkts in sorted(position_wr.items()):
            for b in ("1", "2", "3", "4+"):
                subset = bkts.get(b, [])
                if not subset:
                    continue
                wins = sum(1 for x in subset if x.bucket == "full_win")
                avg = sum(x.move_signed_pct for x in subset) / len(subset)
                wr = 100 * wins / len(subset)
                w.writerow(["fire_position_in_week", scoring, tier, direction,
                            b, len(subset), round(wr, 1), round(avg, 3)])

    log.info(f"Re-fire analysis complete → {out_path}")

    # Return week_groups for use in rollup analysis
    return week_groups


# ═══════════════════════════════════════════════════════════
# ANALYSIS 3: ROLL-UP SIMULATION
# ═══════════════════════════════════════════════════════════

def analyze_rollup(week_groups, out_path):
    """For every same-week re-fire pair, simulate two strategies:

    HOLD BOTH:
      Position 1: entered at fire1.entry_price, exits at fire1.exit_price
                  (same as data already records)
      Position 2: entered at fire2.entry_price, exits at fire2.exit_price
                  (also in data)
      Combined PnL = sum of both

    ROLL UP:
      Close position 1 at fire2.entry_price (mid-week, not Friday close)
      Open new position at fire2.entry_price, exits at fire2.exit_price
      Combined PnL = (fire2_entry - fire1_entry) + (fire2_exit - fire2_entry)
                   = fire2_exit - fire1_entry
                   for bulls; inverted for bears

    For bulls: PnL_signed = (price_final - price_entry) / price_entry * 100
    For bears: PnL_signed = -(price_final - price_entry) / price_entry * 100

    We report WR and avg PnL per strategy, broken down by (scoring, tier,
    direction). Only computed at 15m resolution to keep sample sizes
    meaningful and results interpretable.
    """
    log.info("Computing rollup simulation for same-week paired fires...")

    results = defaultdict(list)  # (scoring, tier, direction, strategy) → [pnl_pct]

    for key, group in week_groups.items():
        _, scoring, resolution, tier, direction, _, _ = key
        if resolution != 15:
            continue
        if len(group) < 2:
            continue
        # Only analyze the FIRST PAIR in the week (fire1, fire2).
        # Ignoring 3rd+ fires keeps the analysis interpretable.
        # Users thinking about rolling typically ask about 1→2 decision.
        fire1 = group[0]
        fire2 = group[1]

        # Require fire2.entry_ts > fire1.entry_ts (should always be true, but safety)
        if fire2.entry_ts <= fire1.entry_ts:
            continue

        # HOLD BOTH: independent PnL on each trade
        pnl1 = fire1.move_signed_pct  # already signed by direction
        pnl2 = fire2.move_signed_pct
        hold_both_pnl = pnl1 + pnl2
        # "Win" for hold-both = BOTH positions win
        hold_both_wins = (fire1.bucket == "full_win" and fire2.bucket == "full_win")

        # ROLL UP: close fire1 at fire2.entry_price, open fire2 at fire2.entry_price
        # Combined PnL = move from fire1.entry to fire2.exit, all captured in one position
        if fire1.entry_price > 0:
            if direction == "bull":
                rollup_pnl = (fire2.exit_price - fire1.entry_price) / fire1.entry_price * 100.0
            else:
                rollup_pnl = -(fire2.exit_price - fire1.entry_price) / fire1.entry_price * 100.0
            rollup_wins = rollup_pnl >= -1.0  # full_win threshold (same as grading)
        else:
            rollup_pnl = 0.0
            rollup_wins = False

        key_out = (scoring, tier, direction)
        results[(key_out, "hold_both")].append((hold_both_pnl, hold_both_wins))
        results[(key_out, "roll_up")].append((rollup_pnl, rollup_wins))

        # Also record fire1-alone WR for comparison baseline
        results[(key_out, "fire1_only")].append((pnl1, fire1.bucket == "full_win"))

    # Summarize
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scoring", "tier", "direction", "strategy",
                    "n_paired_weeks", "wr_pct", "avg_pnl_pct",
                    "avg_pnl_per_leg_pct"])

        # Group by (scoring, tier, direction) so strategies appear together
        by_combo = defaultdict(dict)
        for (combo, strategy), data in results.items():
            by_combo[combo][strategy] = data

        for combo in sorted(by_combo.keys()):
            scoring, tier, direction = combo
            for strategy in ("fire1_only", "hold_both", "roll_up"):
                data = by_combo[combo].get(strategy, [])
                if not data:
                    continue
                n = len(data)
                wins = sum(1 for _, win in data if win)
                wr = 100 * wins / n
                avg_pnl = sum(pnl for pnl, _ in data) / n
                # per-leg PnL: hold_both is divided by 2 (two positions), others by 1
                per_leg = avg_pnl / 2 if strategy == "hold_both" else avg_pnl
                w.writerow([scoring, tier, direction, strategy, n,
                            round(wr, 1), round(avg_pnl, 3), round(per_leg, 3)])

    log.info(f"Rollup analysis complete → {out_path}")


# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def write_report(findings, out_path):
    md = """# Trade Analysis Report — Deliverable 1

Analysis of bt_resolution_study trades.csv (572K+ signals).

## 1. Per-ticker findings (15m active_scanner + pinescript, T2)

### Best T2 bulls (highest WR)
"""
    for line in findings["best_bulls"]:
        md += f"- {line}\n"

    md += "\n### Worst T2 bulls (lowest WR)\n"
    for line in findings["worst_bulls"]:
        md += f"- {line}\n"

    md += "\n### Best T2 bears\n"
    for line in findings["best_bears"]:
        md += f"- {line}\n"

    md += "\n### Worst T2 bears\n"
    for line in findings["worst_bears"]:
        md += f"- {line}\n"

    md += "\n### Tickers with strong directional bias (≥15 point WR gap between bull and bear)\n"
    if not findings["directional_bias"]:
        md += "- None found — all tickers are reasonably balanced.\n"
    else:
        for line in findings["directional_bias"]:
            md += f"- {line}\n"

    md += """

## 2. Re-fire analysis

See `summary_refire.csv`. Three tables stacked:
- `fires_in_week_distribution` — how often do weeks have 1, 2, 3, 4+ fires
- `first_fire_given_total_fires` — WR of the week's first fire, broken out
  by how many total fires happened that week. Answers: "if a lot of fires
  are going to come this week, does my first entry win more often?"
- `fire_position_in_week` — WR by position (1st, 2nd, 3rd, 4+) in week.
  Answers: "is the first fire the best or do later fires win more?"

## 3. Roll-up simulation

See `summary_rollup.csv`. For every same-week paired fire at 15m:
- `fire1_only` — WR of just taking the first fire (baseline)
- `hold_both` — WR when both positions are held to Friday close
  (win = BOTH legs win; avg PnL is combined across both legs)
- `roll_up` — Close fire1 at fire2's entry price, open a new single
  position at fire2's entry, exit at fire2's Friday close. Answers
  your specific scenario: "If I entered Monday at 120 and it fires
  again Tuesday at 122, should I roll up to follow the price?"

**Interpretation guide:**
- If `roll_up` WR > `fire1_only` WR: rolling up helps capture the stronger
  momentum by re-entering at a better price with the same risk spread.
- If `hold_both` WR is high but avg PnL per leg is low: most fires are
  small wins, two small wins is better than one but diminishing returns.
- If `roll_up` avg_pnl > `hold_both` avg_pnl: rolling compounds the move,
  holding both dilutes it.

— Not financial advice —
"""
    Path(out_path).write_text(md)
    log.info(f"Report written → {out_path}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    rows = load_trades()
    if not rows:
        log.error("No rows loaded; aborting")
        sys.exit(1)

    findings = analyze_per_ticker(rows, OUT_DIR / "summary_per_ticker.csv")
    week_groups = analyze_refires(rows, OUT_DIR / "summary_refire.csv")
    analyze_rollup(week_groups, OUT_DIR / "summary_rollup.csv")
    write_report(findings, OUT_DIR / "report_analysis.md")

    log.info(f"DONE. Outputs in {OUT_DIR}:")
    for fn in ("report_analysis.md", "summary_per_ticker.csv",
               "summary_refire.csv", "summary_rollup.csv"):
        p = OUT_DIR / fn
        if p.exists():
            log.info(f"  {fn}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
