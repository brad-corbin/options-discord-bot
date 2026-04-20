#!/usr/bin/env python3
"""
bt_credit_confirmation.py — Phase 1 credit spread edge confirmation
════════════════════════════════════════════════════════════════════════════════

Purpose
-------
Prove or disprove (on the full 572K-trade annotated dataset) that credit spreads
placed at Potter Box boundaries replicate the small-sample 83-91% WR finding
from the weekend trial run, filtered by the CONVICTION TAKE gate.

CONVICTION TAKE gate (from handoff, v8.3 locked scorer baseline)
---------------------------------------------------------------
 1. Tier 1 or 2 active_scanner signal (pinescript excluded; all resolutions
    included — 15m is "preferred" but we want the edge across all three).
 2. Ticker in Tier-A/B list (drops COIN, CRM, MRNA, MSTR, SMCI, SOFI).
 3. CB side aligned: bull → below_cb or at_cb, bear → above_cb or at_cb.
 4. Hard skip: pb_state == above_roof AND direction == bear.
 5. Hard skip: pb_wave_label == established AND direction == bull.
 6. Regime-aware: in BEAR regime, bulls only.

Credit gate (additional, applied to produce credit_* columns)
-------------------------------------------------------------
 - pb_state == in_box (we need the range to protect the short)
 - credit_short_strike > 0 (safety)
 - Width: $2.50 on indices/sectors, $5.00 on single stocks
 - Short strike: floor for bulls, roof for bears (already baked into the CSV)

Stratification
--------------
Combo key: tier × bias × resolution × regime × cb_side × wave_label
(n_min = 100 for the "replicates" pass criterion)

Inputs
------
/var/backtest/phase3_v3_1_2026-04-19/trades_35tickers_annotated.csv  (380MB, 572,561 rows)

Override via env:
  BT_CREDIT_INPUT=/path/to/trades.csv
  BT_CREDIT_OUT=/path/to/out_dir

Outputs
-------
<out_dir>/summary_credit_by_combo.csv        — stratified WR + EV per combo
<out_dir>/summary_credit_headline.csv        — top-line headline numbers
<out_dir>/summary_credit_ticker_class.csv    — indices/sectors vs single stocks
<out_dir>/go_nogo_memo.md                    — 1-page go/no-go recommendation

Pass criterion for "the finding replicates"
------------------------------------------
Combos with n ≥ 100 should show:
  - For bull in_box + below_cb/at_cb: credit_50_wr_5d ≥ 75%
  - For bear in_box + above_cb/at_cb: credit_50_wr_5d ≥ 65% (lower bar; bears
    had smaller sample and weaker baseline in the trial run)

If the large-sample number collapses to baseline or below, the small-sample
result was noise and Phase 2 (live shadow logging) is moot.

Notes on avg_credit_received_pct
--------------------------------
The annotated CSV does NOT carry option-chain prices at signal time, so
realized credit cannot be computed exactly. We compute a structural EV proxy
assuming 33% of width as credit received (typical 5 DTE ATM short leg),
documented inline in the output. Replace with realized credit once Phase 2
shadow logging gives us real chain data.

Runtime: ~5-10 min on Render (streaming, no pandas load).
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bt_credit_confirmation")


# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

DEFAULT_INPUT = "/var/backtest/phase3_v3_1_2026-04-19/trades_35tickers_annotated.csv"
DEFAULT_OUT_DIR = "/var/backtest/phase3_v3_1_2026-04-19"

INPUT_CSV = Path(os.environ.get("BT_CREDIT_INPUT", DEFAULT_INPUT))
OUT_DIR = Path(os.environ.get("BT_CREDIT_OUT", DEFAULT_OUT_DIR))

# Tier-A/B universe — the 6 drops come from the handoff. Everything else in
# the 35-ticker ALL_TICKERS set is eligible.
EXCLUDED_TICKERS = {"COIN", "CRM", "MRNA", "MSTR", "SMCI", "SOFI"}

# Width lookup by ticker class. Indices/sectors get $2.50, single stocks $5.00.
INDICES_SECTORS = {
    "SPY", "QQQ", "IWM", "DIA",
    "GLD", "TLT",
    "SOXX", "XLE", "XLF", "XLV",
}

# Minimum sample size before a combo is reported at all. Smaller combos get
# rolled into the "small_n" catch-all so we don't clutter the output with
# noise but still account for every trade.
N_MIN_REPORT = 30
N_MIN_CONFIDENCE = 100

# EV proxy: assume credit captured on entry is this fraction of spread width.
# 33% is typical for a 5 DTE short-leg at-the-money credit spread with normal
# IV. Swap in realized credit from Phase 2 live shadow logging when available.
ASSUMED_CREDIT_FRAC = 0.33


# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def _pb(v) -> bool:
    if v is None or v == "":
        return False
    return str(v).lower() in ("true", "1", "t", "yes")


def _pf(v, d: float = 0.0) -> float:
    try:
        return float(v) if v not in ("", None) else d
    except (ValueError, TypeError):
        return d


def _pi(v, d: int = 0) -> int:
    try:
        return int(float(v)) if v not in ("", None) else d
    except (ValueError, TypeError):
        return d


def _ticker_class(ticker: str) -> str:
    return "index_sector" if ticker in INDICES_SECTORS else "single_stock"


def _passes_conviction_gate(
    scoring_system: str,
    tier: int,
    bias: str,
    ticker: str,
    pb_state: str,
    cb_side: str,
    wave_label: str,
    regime_trend: str,
) -> bool:
    """Return True iff this row would have passed the CONVICTION TAKE gate."""
    # 1. active_scanner + Tier 1/2
    if scoring_system != "active_scanner":
        return False
    if tier not in (1, 2):
        return False
    # 2. Ticker universe
    if ticker in EXCLUDED_TICKERS:
        return False
    # 3. CB side aligned
    if bias == "bull" and cb_side not in ("below_cb", "at_cb"):
        return False
    if bias == "bear" and cb_side not in ("above_cb", "at_cb"):
        return False
    # 4. Hard skip: bear + above_roof
    if bias == "bear" and pb_state == "above_roof":
        return False
    # 5. Hard skip: bull + wave_label established
    if bias == "bull" and wave_label == "established":
        return False
    # 6. BEAR regime → bulls only
    if regime_trend == "BEAR" and bias == "bear":
        return False
    return True


def _credit_gate(pb_state: str, credit_short_strike: float) -> bool:
    """Additional filter on top of conviction gate to qualify for credit eval."""
    return pb_state == "in_box" and credit_short_strike > 0.0


def _combo_id(tier: int, bias: str, resolution: int, regime: str,
              cb_side: str, wave_label: str) -> str:
    return f"T{tier}|{bias}|{resolution}m|{regime}|{cb_side}|{wave_label}"


def _ev_per_trade(bucket: str, direction: str,
                  entry_price: float, exit_price: float,
                  short_strike: float, width: float) -> float:
    """EV in units of 'percent of max risk' assuming ASSUMED_CREDIT_FRAC.

    Max credit = credit_frac * width   (dollars per share)
    Max risk   = (1 - credit_frac) * width
    full_win   → +max_credit / max_risk   (capture all premium)
    full_loss  → -1.0                    (lose full width minus credit)
    partial    → linear interpolation between full_win and full_loss based on
                 where exit landed between short and long strikes.

    Returns ev as a decimal (e.g. +0.49 for full_win, -1.00 for full_loss).
    """
    if width <= 0:
        return 0.0
    credit = ASSUMED_CREDIT_FRAC * width
    max_risk = width - credit
    if max_risk <= 0:
        return 0.0

    if bucket == "full_win":
        return credit / max_risk
    if bucket == "full_loss":
        return -1.0
    if bucket == "partial":
        # Figure out where exit landed between short and long strike.
        if direction == "bull":
            # short = floor, long = floor - width, exit between them
            long_strike = short_strike - width
            if exit_price >= short_strike:
                # Shouldn't hit — would be full_win — but safe fallback
                return credit / max_risk
            if exit_price <= long_strike:
                return -1.0
            # Net P&L = credit - (short_strike - exit_price) per share
            per_share = credit - (short_strike - exit_price)
            return per_share / max_risk
        else:
            long_strike = short_strike + width
            if exit_price <= short_strike:
                return credit / max_risk
            if exit_price >= long_strike:
                return -1.0
            per_share = credit - (exit_price - short_strike)
            return per_share / max_risk
    return 0.0


# ═══════════════════════════════════════════════════════════
# DATA MODEL — minimal slice of the v3.1 schema
# ═══════════════════════════════════════════════════════════

@dataclass
class ConfirmRow:
    ticker: str
    tier: int
    bias: str
    resolution_min: int
    scoring_system: str
    regime_trend: str
    # Outcome buckets
    debit_bucket: str        # full_win / partial / full_loss (=bucket)
    debit_win_5d: bool       # =win_headline (5d exit is the headline)
    # Potter Box state
    pb_state: str
    cb_side: str
    wave_label: str
    # Credit sim from CSV
    credit_short_strike: float
    credit_25_bucket: str
    credit_25_win: bool
    credit_50_bucket: str
    credit_50_win: bool
    # Prices for EV calc
    entry_price: float
    exit_price: float


REQUIRED_COLS = [
    "ticker", "tier", "direction", "resolution_min", "scoring_system",
    "regime_trend",
    "bucket", "win_headline",
    "pb_state", "cb_side", "pb_wave_label",
    "credit_short_strike",
    "credit_25_bucket", "credit_25_win",
    "credit_50_bucket", "credit_50_win",
    "entry_price", "exit_price",
]


def _load_rows(path: Path):
    """Stream the annotated CSV. Yields only rows that PASS the conviction gate.

    Non-qualifying rows are counted but discarded — keeps memory sane on 380MB.
    """
    if not path.exists():
        log.error(f"Input not found: {path}")
        sys.exit(1)

    log.info(f"Streaming {path} ({path.stat().st_size / 1e6:.1f} MB)...")

    total_read = 0
    passed = 0
    filtered_counts: dict = defaultdict(int)

    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        fields = rdr.fieldnames or []
        missing = [c for c in REQUIRED_COLS if c not in fields]
        if missing:
            log.error(f"Annotated CSV missing required columns: {missing}")
            log.error("Re-generate with bt_resolution_study_v3_1.py + annotator pass.")
            sys.exit(1)

        for r in rdr:
            total_read += 1
            if total_read % 50_000 == 0:
                log.info(f"  scanned {total_read:,} rows, {passed:,} through gate")

            ticker = r.get("ticker", "").upper()
            tier = _pi(r.get("tier"))
            bias = r.get("direction", "")
            scoring_system = r.get("scoring_system", "")
            regime_trend = r.get("regime_trend", "UNKNOWN").upper()
            pb_state = r.get("pb_state", "no_box")
            cb_side = r.get("cb_side", "n/a")
            wave_label = r.get("pb_wave_label", "none")

            if not _passes_conviction_gate(
                scoring_system, tier, bias, ticker,
                pb_state, cb_side, wave_label, regime_trend,
            ):
                # Bucket the rejection reason for diagnostics
                if scoring_system != "active_scanner":
                    filtered_counts["not_active_scanner"] += 1
                elif tier not in (1, 2):
                    filtered_counts["wrong_tier"] += 1
                elif ticker in EXCLUDED_TICKERS:
                    filtered_counts["excluded_ticker"] += 1
                elif bias == "bull" and cb_side not in ("below_cb", "at_cb"):
                    filtered_counts["cb_side_bull"] += 1
                elif bias == "bear" and cb_side not in ("above_cb", "at_cb"):
                    filtered_counts["cb_side_bear"] += 1
                elif bias == "bear" and pb_state == "above_roof":
                    filtered_counts["bear_above_roof"] += 1
                elif bias == "bull" and wave_label == "established":
                    filtered_counts["bull_wave_established"] += 1
                elif regime_trend == "BEAR" and bias == "bear":
                    filtered_counts["bear_regime_bears"] += 1
                else:
                    filtered_counts["other"] += 1
                continue

            passed += 1
            yield ConfirmRow(
                ticker=ticker,
                tier=tier,
                bias=bias,
                resolution_min=_pi(r.get("resolution_min")),
                scoring_system=scoring_system,
                regime_trend=regime_trend,
                debit_bucket=r.get("bucket", "n/a"),
                debit_win_5d=_pb(r.get("win_headline")),
                pb_state=pb_state,
                cb_side=cb_side,
                wave_label=wave_label,
                credit_short_strike=_pf(r.get("credit_short_strike")),
                credit_25_bucket=r.get("credit_25_bucket", "n/a"),
                credit_25_win=_pb(r.get("credit_25_win")),
                credit_50_bucket=r.get("credit_50_bucket", "n/a"),
                credit_50_win=_pb(r.get("credit_50_win")),
                entry_price=_pf(r.get("entry_price")),
                exit_price=_pf(r.get("exit_price")),
            )

    log.info(f"Total rows: {total_read:,}")
    log.info(f"Passed conviction gate: {passed:,} ({100.0 * passed / max(1, total_read):.1f}%)")
    log.info("Rejection breakdown:")
    for reason, cnt in sorted(filtered_counts.items(), key=lambda x: -x[1]):
        log.info(f"  {reason:<28} {cnt:>10,}")


# ═══════════════════════════════════════════════════════════
# AGGREGATION
# ═══════════════════════════════════════════════════════════

@dataclass
class ComboStats:
    combo_id: str
    tier: int
    bias: str
    resolution: int
    regime: str
    cb_side: str
    wave_label: str
    ticker_classes: set = field(default_factory=set)
    n_trades: int = 0
    n_debit_win: int = 0
    # Only in_box + credit_short_strike>0 rows contribute to credit counts
    n_credit_eligible: int = 0
    n_credit_25_win: int = 0
    n_credit_25_partial: int = 0
    n_credit_25_loss: int = 0
    n_credit_50_win: int = 0
    n_credit_50_partial: int = 0
    n_credit_50_loss: int = 0
    # EV accumulators (sum per-trade EV, divide by n_credit_eligible at end)
    ev_25_sum: float = 0.0
    ev_50_sum: float = 0.0


def aggregate(rows_iter) -> dict:
    combos: dict[str, ComboStats] = {}

    for row in rows_iter:
        key = _combo_id(
            row.tier, row.bias, row.resolution_min, row.regime_trend,
            row.cb_side, row.wave_label,
        )
        c = combos.get(key)
        if c is None:
            c = ComboStats(
                combo_id=key,
                tier=row.tier, bias=row.bias, resolution=row.resolution_min,
                regime=row.regime_trend, cb_side=row.cb_side,
                wave_label=row.wave_label,
            )
            combos[key] = c

        c.ticker_classes.add(_ticker_class(row.ticker))
        c.n_trades += 1
        if row.debit_win_5d:
            c.n_debit_win += 1

        if _credit_gate(row.pb_state, row.credit_short_strike):
            c.n_credit_eligible += 1
            # $2.50 width outcome
            if row.credit_25_bucket == "full_win":
                c.n_credit_25_win += 1
            elif row.credit_25_bucket == "partial":
                c.n_credit_25_partial += 1
            elif row.credit_25_bucket == "full_loss":
                c.n_credit_25_loss += 1
            # $5.00 width outcome
            if row.credit_50_bucket == "full_win":
                c.n_credit_50_win += 1
            elif row.credit_50_bucket == "partial":
                c.n_credit_50_partial += 1
            elif row.credit_50_bucket == "full_loss":
                c.n_credit_50_loss += 1
            # EV contributions
            c.ev_25_sum += _ev_per_trade(
                row.credit_25_bucket, row.bias,
                row.entry_price, row.exit_price,
                row.credit_short_strike, 2.50,
            )
            c.ev_50_sum += _ev_per_trade(
                row.credit_50_bucket, row.bias,
                row.entry_price, row.exit_price,
                row.credit_short_strike, 5.00,
            )

    return combos


# ═══════════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════════

def _pct(num: int, den: int) -> float:
    return 100.0 * num / den if den > 0 else 0.0


def _best_strategy(debit_wr: float, c25_wr: float, c50_wr: float,
                   n_debit: int, n_credit: int) -> str:
    """Pick better_strategy. Uses 3-point WR guardband; requires both samples
    to have ≥ 30 n before naming a clear winner."""
    if n_debit < 30 or n_credit < 30:
        return "insufficient_n"
    best_c = max(c25_wr, c50_wr)
    if debit_wr >= best_c + 3:
        return "debit"
    if best_c >= debit_wr + 3:
        return f"credit_{'25' if c25_wr >= c50_wr else '50'}"
    return "tie"


def write_summary_combo(combos: dict, path: Path):
    """Main deliverable: summary_credit_by_combo.csv."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "combo_id",
            "tier", "bias", "resolution_min", "regime", "cb_side", "wave_label",
            "ticker_classes",
            "n_trades",
            "debit_wr_5d_pct",
            "n_credit_eligible",
            "credit_25_wr_5d_pct",
            "credit_25_partial_pct",
            "credit_25_full_loss_pct",
            "credit_50_wr_5d_pct",
            "credit_50_partial_pct",
            "credit_50_full_loss_pct",
            "ev_25_avg_pct_of_maxrisk",
            "ev_50_avg_pct_of_maxrisk",
            "better_strategy",
            "confidence",
        ])
        ordered = sorted(combos.values(), key=lambda c: (-c.n_trades, c.combo_id))
        for c in ordered:
            if c.n_trades < N_MIN_REPORT:
                continue

            debit_wr = _pct(c.n_debit_win, c.n_trades)
            c25_wr = _pct(c.n_credit_25_win, c.n_credit_eligible)
            c50_wr = _pct(c.n_credit_50_win, c.n_credit_eligible)
            c25_part = _pct(c.n_credit_25_partial, c.n_credit_eligible)
            c50_part = _pct(c.n_credit_50_partial, c.n_credit_eligible)
            c25_loss = _pct(c.n_credit_25_loss, c.n_credit_eligible)
            c50_loss = _pct(c.n_credit_50_loss, c.n_credit_eligible)
            ev25 = (c.ev_25_sum / c.n_credit_eligible * 100.0) if c.n_credit_eligible else 0.0
            ev50 = (c.ev_50_sum / c.n_credit_eligible * 100.0) if c.n_credit_eligible else 0.0

            conf = "high" if c.n_credit_eligible >= N_MIN_CONFIDENCE else \
                   "medium" if c.n_credit_eligible >= N_MIN_REPORT else "low"

            w.writerow([
                c.combo_id,
                c.tier, c.bias, c.resolution, c.regime, c.cb_side, c.wave_label,
                "+".join(sorted(c.ticker_classes)) or "none",
                c.n_trades,
                f"{debit_wr:.1f}",
                c.n_credit_eligible,
                f"{c25_wr:.1f}",
                f"{c25_part:.1f}",
                f"{c25_loss:.1f}",
                f"{c50_wr:.1f}",
                f"{c50_part:.1f}",
                f"{c50_loss:.1f}",
                f"{ev25:+.1f}",
                f"{ev50:+.1f}",
                _best_strategy(debit_wr, c25_wr, c50_wr,
                               c.n_trades, c.n_credit_eligible),
                conf,
            ])
    log.info(f"  → {path}")


def write_summary_headline(combos: dict, path: Path):
    """Top-line headlines: bull/bear × in_box × aligned (full population,
    merged across resolutions + regimes + wave labels).

    These are the numbers to read first — they're what we compare the
    83-91% small-sample claim against."""
    tot: dict = defaultdict(lambda: {
        "n_trades": 0, "n_debit_win": 0,
        "n_credit_eligible": 0,
        "n_c25_win": 0, "n_c50_win": 0,
        "ev25_sum": 0.0, "ev50_sum": 0.0,
    })
    for c in combos.values():
        # Headline = bias only (combo key is bias)
        k = c.bias
        tot[k]["n_trades"] += c.n_trades
        tot[k]["n_debit_win"] += c.n_debit_win
        tot[k]["n_credit_eligible"] += c.n_credit_eligible
        tot[k]["n_c25_win"] += c.n_credit_25_win
        tot[k]["n_c50_win"] += c.n_credit_50_win
        tot[k]["ev25_sum"] += c.ev_25_sum
        tot[k]["ev50_sum"] += c.ev_50_sum

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "bias",
            "n_trades_passing_conviction",
            "debit_wr_5d_pct",
            "n_credit_eligible",
            "credit_25_wr_5d_pct",
            "credit_50_wr_5d_pct",
            "ev_25_avg_pct_of_maxrisk",
            "ev_50_avg_pct_of_maxrisk",
        ])
        for bias in ("bull", "bear"):
            t = tot.get(bias, {})
            n = t.get("n_trades", 0)
            if n == 0:
                continue
            ne = t.get("n_credit_eligible", 0)
            w.writerow([
                bias, n,
                f"{_pct(t.get('n_debit_win', 0), n):.1f}",
                ne,
                f"{_pct(t.get('n_c25_win', 0), ne):.1f}",
                f"{_pct(t.get('n_c50_win', 0), ne):.1f}",
                f"{(t.get('ev25_sum', 0.0) / ne * 100.0) if ne else 0.0:+.1f}",
                f"{(t.get('ev50_sum', 0.0) / ne * 100.0) if ne else 0.0:+.1f}",
            ])
    log.info(f"  → {path}")


def write_summary_ticker_class(combos: dict, rows_indexed: dict, path: Path):
    """Split by ticker class so we know which width to recommend per class.

    rows_indexed is (ticker_class, bias) → accumulator dict.
    """
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker_class",
            "bias",
            "n_trades_passing_conviction",
            "debit_wr_5d_pct",
            "n_credit_eligible",
            "credit_25_wr_5d_pct",
            "credit_50_wr_5d_pct",
            "ev_25_avg_pct_of_maxrisk",
            "ev_50_avg_pct_of_maxrisk",
            "recommended_width",
        ])
        for (tc, bias) in sorted(rows_indexed.keys()):
            d = rows_indexed[(tc, bias)]
            n = d["n_trades"]
            ne = d["n_credit_eligible"]
            if n == 0:
                continue
            c25 = _pct(d["n_c25_win"], ne)
            c50 = _pct(d["n_c50_win"], ne)
            ev25 = (d["ev25_sum"] / ne * 100.0) if ne else 0.0
            ev50 = (d["ev50_sum"] / ne * 100.0) if ne else 0.0
            # Recommend by higher EV (not higher WR — WR alone ignores loss magnitude)
            if ev25 > ev50 + 2:
                rec = "2.50"
            elif ev50 > ev25 + 2:
                rec = "5.00"
            else:
                rec = "tie"
            w.writerow([
                tc, bias, n,
                f"{_pct(d['n_debit_win'], n):.1f}",
                ne,
                f"{c25:.1f}", f"{c50:.1f}",
                f"{ev25:+.1f}", f"{ev50:+.1f}",
                rec,
            ])
    log.info(f"  → {path}")


def write_go_nogo_memo(combos: dict, path: Path):
    """1-page memo: did the edge replicate? At what WR by combo? Recommended gate."""
    high_n = [c for c in combos.values() if c.n_credit_eligible >= N_MIN_CONFIDENCE]
    med_n = [c for c in combos.values()
             if N_MIN_REPORT <= c.n_credit_eligible < N_MIN_CONFIDENCE]

    # Pass thresholds from the handoff
    bull_target_wr = 75.0
    bear_target_wr = 65.0

    bull_pass = [c for c in high_n if c.bias == "bull"
                 and _pct(c.n_credit_50_win, c.n_credit_eligible) >= bull_target_wr]
    bear_pass = [c for c in high_n if c.bias == "bear"
                 and _pct(c.n_credit_50_win, c.n_credit_eligible) >= bear_target_wr]
    bull_high = [c for c in high_n if c.bias == "bull"]
    bear_high = [c for c in high_n if c.bias == "bear"]

    def _combo_summary(c: ComboStats) -> str:
        c50 = _pct(c.n_credit_50_win, c.n_credit_eligible)
        c25 = _pct(c.n_credit_25_win, c.n_credit_eligible)
        dwr = _pct(c.n_debit_win, c.n_trades)
        ev50 = (c.ev_50_sum / c.n_credit_eligible * 100.0) if c.n_credit_eligible else 0.0
        return (f"- `{c.combo_id}` — n={c.n_credit_eligible}, "
                f"debit_wr={dwr:.1f}%, c25_wr={c25:.1f}%, c50_wr={c50:.1f}%, "
                f"ev50={ev50:+.1f}%")

    lines = [
        "# Credit Spread Confirmation — Go/No-Go Memo",
        "",
        f"Source: `{INPUT_CSV}`",
        f"Output dir: `{OUT_DIR}`",
        "",
        "## Headline",
        "",
        f"- Combos passing `n ≥ {N_MIN_CONFIDENCE}` confidence floor: **{len(high_n)}**",
        f"- Bull combos at high-n meeting target `credit_50_wr ≥ {bull_target_wr:.0f}%`: "
        f"**{len(bull_pass)} / {len(bull_high)}**",
        f"- Bear combos at high-n meeting target `credit_50_wr ≥ {bear_target_wr:.0f}%`: "
        f"**{len(bear_pass)} / {len(bear_high)}**",
        "",
        "## Recommendation",
        "",
    ]

    # Decision logic
    if len(bull_pass) >= max(1, len(bull_high) // 2) and \
       len(bear_pass) >= max(1, len(bear_high) // 2):
        lines += [
            "**GO** — large-sample credit edge replicates. Proceed to Phase 2 "
            "(2-week shadow logging on v8.3 live trade cards) before deploying "
            "credit cards to Telegram.",
            "",
            "Deployment gate for v8.4:",
            "- Apply CONVICTION TAKE gate identically to live",
            "- Post dual card (debit + credit) only when credit gate ALSO holds",
            "- Width by ticker class per `summary_credit_ticker_class.csv`",
            "- Route credit cards through Income Scanner tracking",
        ]
    elif (len(bull_pass) + len(bear_pass)) == 0:
        lines += [
            "**NO-GO** — large-sample credit WR collapsed. The small-sample "
            "trial finding was noise. Do NOT retarget the Income Scanner to "
            "v8.3 signals. Debit-only stays the shipping path.",
        ]
    else:
        lines += [
            "**PARTIAL / GO ON BULL-ONLY** — bull credit edge present, bear "
            "credit edge did not replicate at the target WR. Consider shipping "
            "bull_put credit cards only; keep bear_call out of scope until a "
            "better-filtered population is identified.",
        ]

    lines += [
        "",
        "## Top 10 combos by n_credit_eligible (high-confidence slice)",
        "",
    ]
    top10 = sorted(high_n, key=lambda c: -c.n_credit_eligible)[:10]
    if not top10:
        lines.append("_(no combos met the high-confidence threshold)_")
    else:
        for c in top10:
            lines.append(_combo_summary(c))

    lines += [
        "",
        "## Notes on EV proxy",
        "",
        f"- EV is computed with an assumed credit of {ASSUMED_CREDIT_FRAC * 100:.0f}% of "
        "spread width — a typical 5-DTE ATM short leg. Replace with realized "
        "credit from Phase 2 shadow logging before promoting any combo to live.",
        "- EV is expressed as a percent of **max risk** (not max credit), so "
        "comparable to the risk-adjusted return metric used elsewhere in the bot.",
        "",
        "## What the script did NOT check (out of scope for Phase 1)",
        "",
        "- Early exit at 50% max profit (backtest holds to day-5 exit).",
        "- Assignment risk on short ITM legs near expiration.",
        "- Survivorship bias from boxes that collapsed mid-hold.",
        "- Real chain pricing / real fills.",
        "",
        "These become relevant in Phase 2 (live shadow logging) — not here.",
    ]

    path.write_text("\n".join(lines))
    log.info(f"  → {path}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Input:  {INPUT_CSV}")
    log.info(f"Output: {OUT_DIR}")
    log.info("")
    log.info("CONVICTION TAKE gate:")
    log.info("  active_scanner + tier 1/2, Tier-A/B tickers, CB aligned,")
    log.info("  no bear+above_roof, no bull+wave_established, BEAR regime bulls only")
    log.info("")

    # Pass 1: stream through the CSV, aggregate into combo keys AND ticker-class buckets
    ticker_class_index: dict = defaultdict(lambda: {
        "n_trades": 0, "n_debit_win": 0,
        "n_credit_eligible": 0,
        "n_c25_win": 0, "n_c50_win": 0,
        "ev25_sum": 0.0, "ev50_sum": 0.0,
    })

    # Wrap loader so we can tee into the ticker-class index AND the combo agg
    def _loader():
        for row in _load_rows(INPUT_CSV):
            # tee into ticker-class bucket
            tc = _ticker_class(row.ticker)
            d = ticker_class_index[(tc, row.bias)]
            d["n_trades"] += 1
            if row.debit_win_5d:
                d["n_debit_win"] += 1
            if _credit_gate(row.pb_state, row.credit_short_strike):
                d["n_credit_eligible"] += 1
                if row.credit_25_bucket == "full_win":
                    d["n_c25_win"] += 1
                if row.credit_50_bucket == "full_win":
                    d["n_c50_win"] += 1
                d["ev25_sum"] += _ev_per_trade(
                    row.credit_25_bucket, row.bias,
                    row.entry_price, row.exit_price,
                    row.credit_short_strike, 2.50,
                )
                d["ev50_sum"] += _ev_per_trade(
                    row.credit_50_bucket, row.bias,
                    row.entry_price, row.exit_price,
                    row.credit_short_strike, 5.00,
                )
            yield row

    combos = aggregate(_loader())
    log.info(f"Combos built: {len(combos)}")

    # Write outputs
    write_summary_combo(combos, OUT_DIR / "summary_credit_by_combo.csv")
    write_summary_headline(combos, OUT_DIR / "summary_credit_headline.csv")
    write_summary_ticker_class(combos, ticker_class_index,
                               OUT_DIR / "summary_credit_ticker_class.csv")
    write_go_nogo_memo(combos, OUT_DIR / "go_nogo_memo.md")

    log.info("")
    log.info("DONE. Read go_nogo_memo.md first, then summary_credit_by_combo.csv.")


if __name__ == "__main__":
    main()
