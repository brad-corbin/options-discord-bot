#!/usr/bin/env python3
"""
bt_itqs_backtest.py — ITQS independent-edge backtest (Phase 1.5)
════════════════════════════════════════════════════════════════════════════════

Purpose
-------
Answer the "rebuild vs patch" architecture question by measuring whether the
EXISTING Income Scanner's ITQS (Income Trade Quality Score) has real edge on
its own signal selection, independent of the v8.3 scorer.

If ITQS picks a high-WR subset → Income Scanner has independent edge →
  REBUILD (keep both channels alongside v8.4 credit cards).

If ITQS picks at ~baseline → Income Scanner is running a strategy without
  real edge → PATCH (retire ITQS signal logic, retarget Income Scanner
  infrastructure to v8.3 signals).

How the test works
------------------
Population: every active_scanner Tier 1/2 row in the Phase 1 annotated CSV
(no CONVICTION TAKE filter — we want ITQS's own picks, not v8.3 overlap).

For each signal:
  1. Load the ticker's daily OHLCV up to the signal date.
  2. Detect support/resistance via income_scanner.detect_support_levels /
     detect_resistance_levels (same clustering as live).
  3. Pick short strike below best support (bull_put) or above best resistance
     (bear_call). Long strike = short ± $2.50 / $5.00 (we grade both widths).
  4. Compute ITQS via income_scanner.compute_itqs — the LIVE scorecard, not
     a reimplementation. Gaps: liquidity=3 (constant), event_risk=0 (per
     Brad's "skip earnings" choice), no swing confluence, no live chain.
  5. Grade the credit outcome at the S/R short strike using the row's
     exit_price (5d horizon, same as Phase 1).

Outputs
-------
<out_dir>/summary_itqs_by_grade.csv           — WR & EV by ITQS grade (A+/A/B/C/F)
<out_dir>/summary_itqs_vs_v83.csv             — overlap with v8.3 gate: ITQS agreement
<out_dir>/summary_itqs_by_combo.csv           — ITQS × tier × bias × regime WR
<out_dir>/itqs_decision_memo.md               — rebuild vs patch recommendation

Caveats (baked into memo)
-------------------------
- Liquidity is constant 3 (live varies 0–5 based on chain bid/ask+OI).
- Event risk is 0 (live penalizes earnings/FMP/macro events up to −10).
- No swing scanner confluence (live boosts ITQS when swing scanner
  independently flags the same level).
- Daily bars fetched on demand from MarketData; cached to /tmp.
- Historical chain not available; strikes round via _option_increment
  fallback. Same approximation Phase 1 used.

Runtime: ~15-20 min on Render (first run, ~1 min for daily bar fetch);
rerun with cache: ~10 min.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# Import ITQS logic from the live scanner — not a reimplementation.
# income_scanner.py wraps yfinance/FMP imports in try/except, so this is safe
# even when those deps are unavailable.
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent if _THIS_DIR.name == "backtest" else _THIS_DIR
sys.path.insert(0, str(_REPO_ROOT))
try:
    import income_scanner as isc
except Exception as e:
    print(f"FATAL: cannot import income_scanner from {_REPO_ROOT}: {e}")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bt_itqs_backtest")


# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

DEFAULT_INPUT = "/var/backtest/phase3_v3_1_2026-04-19/trades_35tickers_annotated.csv"
DEFAULT_OUT_DIR = "/var/backtest/phase3_v3_1_2026-04-19"
DEFAULT_CACHE_DIR = "/tmp/itqs_daily_cache"

INPUT_CSV = Path(os.environ.get("BT_ITQS_INPUT", DEFAULT_INPUT))
OUT_DIR = Path(os.environ.get("BT_ITQS_OUT", DEFAULT_OUT_DIR))
CACHE_DIR = Path(os.environ.get("BT_ITQS_DAILY_CACHE", DEFAULT_CACHE_DIR))

MD_TOKEN = os.environ.get("MARKETDATA_TOKEN", "").strip()

# Fetch a conservatively wide daily window. Phase 1 data spans ~2024-2026.
# Lookback buffer of 180d for weekly EMA / support clustering.
FETCH_START = os.environ.get("BT_ITQS_DAILY_START", "2023-06-01")
FETCH_END = os.environ.get("BT_ITQS_DAILY_END", "2026-04-19")

# v1 → v2 regime mapping (V2_TO_V1 from market_regime.py inverted; v1 lossy
# so we pick the most common v2 value for each v1 bucket).
V1_TO_V2 = {
    "BULL": "BULL_BASE",
    "TRANSITION": "CHOP",
    "BEAR": "BEAR_TRANSITION",
    "UNKNOWN": "CHOP",
}

# Tier-A/B universe (same as Phase 1)
EXCLUDED_TICKERS = {"COIN", "CRM", "MRNA", "MSTR", "SMCI", "SOFI"}
ALL_TICKERS = [
    "AAPL", "AMD", "AMZN", "ARM", "AVGO", "BA", "CAT", "COIN", "CRM", "DIA",
    "GLD", "GOOGL", "GS", "IWM", "JPM", "LLY", "META", "MRNA", "MSFT", "MSTR",
    "NFLX", "NVDA", "ORCL", "PLTR", "QQQ", "SMCI", "SOFI", "SOXX", "SPY",
    "TLT", "TSLA", "UNH", "XLE", "XLF", "XLV",
]

# Constants for gaps (per Brad's choices)
LIQUIDITY_SCORE_CONST = 3       # 0-5, live varies by chain bid/ask+OI
EVENT_RISK_CONST = 0            # 0 to 10, skipped per Brad's fast-path choice
DTE_ASSUMED = 5                 # weekly expiry assumption (4-7 range)

# Grading thresholds (match ITQS grade boundaries)
GRADE_THRESHOLDS = [("A+", 90), ("A", 85), ("B", 75), ("C", 65), ("F", 0)]


# ═══════════════════════════════════════════════════════════
# DAILY BAR FETCH + CACHE
# ═══════════════════════════════════════════════════════════

def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker}_{FETCH_START}_{FETCH_END}.csv"


def _fetch_daily_md(ticker: str, from_date: str, to_date: str,
                    retries: int = 3) -> Optional[dict]:
    """Fetch daily bars from MarketData. Returns dict with o/h/l/c/v/t or None."""
    if not MD_TOKEN:
        log.error("MARKETDATA_TOKEN not set; cannot fetch daily bars.")
        return None
    to_exc = (datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api.marketdata.app/v1/stocks/candles/D/{ticker.upper()}/"
    params = {"from": from_date, "to": to_exc, "dateformat": "timestamp"}
    headers = {"Authorization": f"Bearer {MD_TOKEN}"}
    wait = 10
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=60)
            if r.status_code in (429, 403):
                retry_after = int(r.headers.get("Retry-After", wait))
                log.warning(f"MD {ticker} HTTP {r.status_code}: wait {retry_after}s (attempt {attempt}/{retries})")
                time.sleep(retry_after)
                wait = min(wait * 2, 60)
                continue
            if r.status_code not in (200, 203):
                log.warning(f"MD {ticker} HTTP {r.status_code}: {r.text[:120]}")
                return None
            data = r.json()
            if data.get("s") != "ok":
                log.warning(f"MD {ticker} s={data.get('s')}")
                return None
            return data
        except Exception as e:
            log.warning(f"MD {ticker} fetch error: {e}")
            time.sleep(wait)
            wait = min(wait * 2, 60)
    return None


def _write_cache(ticker: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(ticker)
    t = data.get("t", [])
    o = data.get("o", []); h = data.get("h", [])
    l = data.get("l", []); c = data.get("c", []); v = data.get("v", [])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "date", "o", "h", "l", "c", "v"])
        for i in range(len(t)):
            try:
                ts = int(t[i])
                date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                w.writerow([ts, date_str, o[i], h[i], l[i], c[i], v[i]])
            except (IndexError, ValueError, TypeError):
                continue


def _load_cache(ticker: str) -> Optional[list[dict]]:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    bars = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                bars.append({
                    "ts": int(row["ts"]),
                    "date": row["date"],
                    "o": float(row["o"]), "h": float(row["h"]),
                    "l": float(row["l"]), "c": float(row["c"]),
                    "v": float(row["v"]),
                })
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b["ts"])
    return bars or None


def ensure_daily_cache(tickers: list[str]) -> dict[str, list[dict]]:
    """Fetch + cache daily bars for every ticker. Returns {ticker: [bars]}."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[dict]] = {}
    need_fetch = []
    for t in tickers:
        cached = _load_cache(t)
        if cached:
            out[t] = cached
            log.info(f"  [cache] {t}: {len(cached)} daily bars")
        else:
            need_fetch.append(t)

    if not need_fetch:
        return out

    log.info(f"Fetching daily bars for {len(need_fetch)} tickers from MarketData...")
    for i, t in enumerate(need_fetch, 1):
        log.info(f"  ({i}/{len(need_fetch)}) {t}...")
        data = _fetch_daily_md(t, FETCH_START, FETCH_END)
        if not data:
            log.warning(f"  {t}: fetch failed, skipping ticker entirely")
            continue
        _write_cache(t, data)
        out[t] = _load_cache(t) or []
        time.sleep(0.5)   # gentle rate limit; MarketData is fine with 2/s
    return out


def get_bars_up_to(daily: list[dict], signal_ts: int) -> list[dict]:
    """Return all daily bars with ts < signal_ts (strict less-than to avoid lookahead).

    signal_ts is from intraday 5m bar; daily bar at same date would be same-day.
    We need DAY-BEFORE data to be safe from lookahead.
    """
    cutoff = signal_ts - 86400   # one full day before
    return [b for b in daily if b["ts"] < cutoff]


# ═══════════════════════════════════════════════════════════
# ITQS INVOCATION — one trade at a time
# ═══════════════════════════════════════════════════════════

@dataclass
class ItqsResult:
    score: int
    grade: str
    short_strike: float
    long_strike_25: float
    long_strike_50: float
    level: float
    failure_level: float
    breakeven: float
    credit_25: float
    credit_50: float
    trade_type: str
    had_hard_block: bool


def build_regime_package(regime_trend: str) -> dict:
    core = V1_TO_V2.get(regime_trend.upper(), "CHOP")
    return {
        "core_regime": core,
        "core_score": 0,
        "effective_regime": core,
        "v1_regime": regime_trend,
        "event_overlay": "NONE",
    }


def compute_itqs_for_signal(
    bias: str,
    spot: float,
    bars_before: list[dict],
    regime_trend: str,
    rsi_from_csv: Optional[float],
) -> Optional[ItqsResult]:
    """Run the live ITQS logic on a single historical signal.

    Returns None when there's not enough history, no S/R found, or invalid
    strike placement.
    """
    if len(bars_before) < 110:      # weekly EMA needs 105 bars of closes
        return None

    highs = [b["h"] for b in bars_before]
    lows = [b["l"] for b in bars_before]
    closes = [b["c"] for b in bars_before]
    volumes = [b["v"] for b in bars_before]

    weekly = isc.detect_weekly_trend(closes)
    daily = isc.detect_daily_trend(closes)
    rsi = rsi_from_csv if rsi_from_csv is not None else isc.compute_rsi(closes)
    vol = isc.volume_state(volumes)
    vwap = isc.vwap_state(closes, highs, lows, volumes)
    fibs = isc.auto_fib_levels(highs, lows)

    if bias == "bull":
        trade_type = "bull_put"
        levels = isc.detect_support_levels(lows, spot)
        if not levels:
            return None
        best = levels[0]   # sorted by quality desc
        short_strike = isc._strike_below_support(best, spot, chain=None)
        if short_strike <= 0 or short_strike >= spot:
            return None
        long_25 = round(short_strike - 2.50, 2)
        long_50 = round(short_strike - 5.00, 2)
        # credit estimate: 33% of width (same proxy as Phase 1 EV calc)
        credit_25 = 2.50 * 0.33
        credit_50 = 5.00 * 0.33
        breakeven = short_strike - credit_50   # use $5 for BE calc; conservative
        failure = isc.find_support_failure_level(levels, best["level"])
    else:
        trade_type = "bear_call"
        levels = isc.detect_resistance_levels(highs, spot)
        if not levels:
            return None
        best = levels[0]
        short_strike = isc._strike_above_resistance(best, spot, chain=None)
        if short_strike <= spot:
            return None
        long_25 = round(short_strike + 2.50, 2)
        long_50 = round(short_strike + 5.00, 2)
        credit_25 = 2.50 * 0.33
        credit_50 = 5.00 * 0.33
        breakeven = short_strike + credit_50
        failure = isc.find_resistance_failure_level(levels, best["level"])

    pkg = build_regime_package(regime_trend)

    # Fib confluence (same definition as live)
    fib_match = any(abs(f - short_strike) / spot < 0.015 for f in fibs) if fibs else False

    # Hard blocks (excluding live-only earnings gate since event_risk=0 here)
    blocks = isc.check_hard_blocks(
        trade_type, short_strike, breakeven, best, failure,
        pkg, return_on_risk=(credit_50 / (5.00 - credit_50) * 100),
        earnings_in_window=False,
    )

    itqs = isc.compute_itqs(
        trade_type, short_strike, breakeven, spot, best, failure,
        pkg, weekly, daily, rsi, vol, vwap,
        fib_confluence=fib_match,
        return_on_risk=(credit_50 / (5.00 - credit_50) * 100) if credit_50 < 5.00 else None,
        liquidity_score=LIQUIDITY_SCORE_CONST,
        event_risk=EVENT_RISK_CONST,
        dte=DTE_ASSUMED,
        flow_data=None,
    )

    return ItqsResult(
        score=itqs["score"],
        grade=itqs["grade"],
        short_strike=short_strike,
        long_strike_25=long_25,
        long_strike_50=long_50,
        level=best["level"],
        failure_level=failure,
        breakeven=breakeven,
        credit_25=credit_25,
        credit_50=credit_50,
        trade_type=trade_type,
        had_hard_block=bool(blocks),
    )


# ═══════════════════════════════════════════════════════════
# CREDIT OUTCOME GRADING — at S/R strike (not PB floor/roof)
# ═══════════════════════════════════════════════════════════

def grade_credit(bias: str, short: float, long_strike: float, exit_price: float) -> str:
    """full_win / partial / full_loss — same logic as Phase 1."""
    if short <= 0 or long_strike <= 0 or exit_price <= 0:
        return "n/a"
    if bias == "bull":
        if exit_price >= short: return "full_win"
        if exit_price > long_strike: return "partial"
        return "full_loss"
    else:
        if exit_price <= short: return "full_win"
        if exit_price < long_strike: return "partial"
        return "full_loss"


def ev_per_trade(bucket: str, bias: str, exit_price: float,
                 short_strike: float, width: float, credit_frac: float = 0.33) -> float:
    """EV as % of max risk (same formula as Phase 1)."""
    if width <= 0:
        return 0.0
    credit = credit_frac * width
    max_risk = width - credit
    if max_risk <= 0:
        return 0.0
    if bucket == "full_win":
        return credit / max_risk
    if bucket == "full_loss":
        return -1.0
    if bucket == "partial":
        if bias == "bull":
            long_strike = short_strike - width
            per_share = credit - (short_strike - exit_price)
        else:
            long_strike = short_strike + width
            per_share = credit - (exit_price - short_strike)
        return max(-1.0, min(credit / max_risk, per_share / max_risk))
    return 0.0


# ═══════════════════════════════════════════════════════════
# CONVICTION TAKE GATE — same as Phase 1 (for overlap comparison)
# ═══════════════════════════════════════════════════════════

def passes_v83_gate(scoring_system, tier, bias, ticker, pb_state, cb_side,
                    wave_label, regime_trend) -> bool:
    if scoring_system != "active_scanner":
        return False
    if tier not in (1, 2):
        return False
    if ticker in EXCLUDED_TICKERS:
        return False
    if bias == "bull" and cb_side not in ("below_cb", "at_cb"):
        return False
    if bias == "bear" and cb_side not in ("above_cb", "at_cb"):
        return False
    if bias == "bear" and pb_state == "above_roof":
        return False
    if bias == "bull" and wave_label == "established":
        return False
    if regime_trend == "BEAR" and bias == "bear":
        return False
    return True


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

REQUIRED_COLS = [
    "ticker", "tier", "direction", "resolution_min", "scoring_system",
    "regime_trend", "signal_ts",
    "entry_price", "exit_price",
    "pb_state", "cb_side", "pb_wave_label",
    "ind_rsi",
]


@dataclass
class Agg:
    n: int = 0
    n_win_25: int = 0; n_part_25: int = 0; n_loss_25: int = 0
    n_win_50: int = 0; n_part_50: int = 0; n_loss_50: int = 0
    ev25_sum: float = 0.0; ev50_sum: float = 0.0
    n_hard_block: int = 0
    n_in_v83_overlap: int = 0
    n_win_50_in_overlap: int = 0
    n_win_50_outside_overlap: int = 0
    # For stratified roll-ups
    by_grade: dict = field(default_factory=lambda: defaultdict(lambda: Agg() if False else None))


def _pf(v, d=0.0):
    try:
        return float(v) if v not in ("", None) else d
    except (ValueError, TypeError):
        return d


def _pi(v, d=0):
    try:
        return int(float(v)) if v not in ("", None) else d
    except (ValueError, TypeError):
        return d


def _pb(v):
    return str(v).lower() in ("true", "1", "t", "yes") if v not in ("", None) else False


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Input: {INPUT_CSV}")
    log.info(f"Output: {OUT_DIR}")
    log.info(f"Cache:  {CACHE_DIR}")

    if not INPUT_CSV.exists():
        log.error(f"Input not found: {INPUT_CSV}")
        sys.exit(1)

    # 1. Fetch daily bars for all tickers (cached)
    log.info("")
    log.info("Step 1: ensure daily bar cache...")
    daily_bars = ensure_daily_cache(ALL_TICKERS)
    log.info(f"Daily bars loaded for {len(daily_bars)}/{len(ALL_TICKERS)} tickers")

    # 2. Stream Phase 1 CSV and compute ITQS per row
    log.info("")
    log.info("Step 2: stream Phase 1 CSV and compute ITQS per row...")

    # Aggregation buckets
    by_grade: dict[str, dict] = defaultdict(lambda: {
        "n": 0,
        "n_win_25": 0, "n_part_25": 0, "n_loss_25": 0,
        "n_win_50": 0, "n_part_50": 0, "n_loss_50": 0,
        "ev25_sum": 0.0, "ev50_sum": 0.0,
        "n_hard_block": 0,
        "n_in_v83_overlap": 0,
        "n_win_50_in_overlap": 0,
        "n_win_50_outside_overlap": 0,
    })
    by_combo: dict[tuple, dict] = defaultdict(lambda: {
        "n": 0, "n_win_50": 0, "ev50_sum": 0.0,
    })
    skip_reasons: dict[str, int] = defaultdict(int)

    total_rows = 0
    active_scanner_rows = 0
    itqs_computed = 0
    no_daily = 0

    with open(INPUT_CSV) as f:
        rdr = csv.DictReader(f)
        fields = rdr.fieldnames or []
        missing = [c for c in REQUIRED_COLS if c not in fields]
        if missing:
            log.error(f"CSV missing columns: {missing}")
            sys.exit(1)

        for r in rdr:
            total_rows += 1
            if total_rows % 50_000 == 0:
                log.info(f"  scanned {total_rows:,} | active_scanner {active_scanner_rows:,} | itqs {itqs_computed:,}")

            if r.get("scoring_system") != "active_scanner":
                skip_reasons["not_active_scanner"] += 1
                continue
            tier = _pi(r.get("tier"))
            if tier not in (1, 2):
                skip_reasons["wrong_tier"] += 1
                continue

            ticker = r.get("ticker", "").upper()
            if ticker not in daily_bars:
                skip_reasons["no_daily_bars"] += 1
                no_daily += 1
                continue

            active_scanner_rows += 1

            bias = r.get("direction", "")
            if bias not in ("bull", "bear"):
                skip_reasons["bad_direction"] += 1
                continue

            signal_ts = _pi(r.get("signal_ts"))
            spot = _pf(r.get("entry_price"))
            exit_price = _pf(r.get("exit_price"))
            regime_trend = r.get("regime_trend", "UNKNOWN").upper()
            rsi = _pf(r.get("ind_rsi"), 50.0)

            if spot <= 0 or exit_price <= 0 or signal_ts <= 0:
                skip_reasons["bad_prices"] += 1
                continue

            bars_before = get_bars_up_to(daily_bars[ticker], signal_ts)
            itqs = compute_itqs_for_signal(
                bias, spot, bars_before, regime_trend, rsi_from_csv=rsi,
            )
            if itqs is None:
                skip_reasons["itqs_none"] += 1
                continue

            itqs_computed += 1

            # Grade credit at S/R strike
            bucket_25 = grade_credit(bias, itqs.short_strike, itqs.long_strike_25, exit_price)
            bucket_50 = grade_credit(bias, itqs.short_strike, itqs.long_strike_50, exit_price)
            ev25 = ev_per_trade(bucket_25, bias, exit_price, itqs.short_strike, 2.50)
            ev50 = ev_per_trade(bucket_50, bias, exit_price, itqs.short_strike, 5.00)

            # Overlap with v8.3 CONVICTION TAKE gate?
            in_v83 = passes_v83_gate(
                r.get("scoring_system"), tier, bias, ticker,
                r.get("pb_state", "no_box"), r.get("cb_side", "n/a"),
                r.get("pb_wave_label", "none"), regime_trend,
            )

            # Accumulate by grade
            g = by_grade[itqs.grade]
            g["n"] += 1
            if bucket_25 == "full_win": g["n_win_25"] += 1
            elif bucket_25 == "partial": g["n_part_25"] += 1
            elif bucket_25 == "full_loss": g["n_loss_25"] += 1
            if bucket_50 == "full_win": g["n_win_50"] += 1
            elif bucket_50 == "partial": g["n_part_50"] += 1
            elif bucket_50 == "full_loss": g["n_loss_50"] += 1
            g["ev25_sum"] += ev25
            g["ev50_sum"] += ev50
            if itqs.had_hard_block:
                g["n_hard_block"] += 1
            if in_v83:
                g["n_in_v83_overlap"] += 1
                if bucket_50 == "full_win":
                    g["n_win_50_in_overlap"] += 1
            else:
                if bucket_50 == "full_win":
                    g["n_win_50_outside_overlap"] += 1

            # By combo: (grade, tier, bias, regime)
            ck = (itqs.grade, tier, bias, regime_trend)
            bc = by_combo[ck]
            bc["n"] += 1
            if bucket_50 == "full_win":
                bc["n_win_50"] += 1
            bc["ev50_sum"] += ev50

    # 3. Write outputs
    log.info("")
    log.info(f"Rows scanned:           {total_rows:,}")
    log.info(f"active_scanner T1/T2:   {active_scanner_rows:,}")
    log.info(f"ITQS computed:          {itqs_computed:,}")
    log.info(f"Dropped (no daily):     {no_daily:,}")
    log.info("Skip reasons:")
    for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        log.info(f"  {k:<28} {v:>10,}")

    log.info("")
    log.info("Step 3: write outputs...")

    write_summary_by_grade(by_grade, OUT_DIR / "summary_itqs_by_grade.csv")
    write_summary_vs_v83(by_grade, OUT_DIR / "summary_itqs_vs_v83.csv")
    write_summary_by_combo(by_combo, OUT_DIR / "summary_itqs_by_combo.csv")
    write_decision_memo(by_grade, by_combo, OUT_DIR / "itqs_decision_memo.md",
                        itqs_computed)

    log.info("")
    log.info("DONE. Read itqs_decision_memo.md first.")


def _pct(num: int, den: int) -> float:
    return 100.0 * num / den if den > 0 else 0.0


def write_summary_by_grade(by_grade: dict, path: Path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "itqs_grade", "n_trades",
            "hard_block_pct",
            "credit_25_wr_5d_pct", "credit_25_partial_pct", "credit_25_loss_pct",
            "credit_50_wr_5d_pct", "credit_50_partial_pct", "credit_50_loss_pct",
            "ev_25_avg_pct_of_maxrisk", "ev_50_avg_pct_of_maxrisk",
        ])
        for grade, _ in GRADE_THRESHOLDS:
            g = by_grade.get(grade)
            if not g or g["n"] == 0:
                continue
            n = g["n"]
            w.writerow([
                grade, n,
                f"{_pct(g['n_hard_block'], n):.1f}",
                f"{_pct(g['n_win_25'], n):.1f}", f"{_pct(g['n_part_25'], n):.1f}", f"{_pct(g['n_loss_25'], n):.1f}",
                f"{_pct(g['n_win_50'], n):.1f}", f"{_pct(g['n_part_50'], n):.1f}", f"{_pct(g['n_loss_50'], n):.1f}",
                f"{g['ev25_sum'] / n * 100:+.1f}",
                f"{g['ev50_sum'] / n * 100:+.1f}",
            ])
    log.info(f"  → {path}")


def write_summary_vs_v83(by_grade: dict, path: Path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "itqs_grade", "n_trades",
            "n_in_v83_overlap", "overlap_pct",
            "credit_50_wr_in_overlap_pct",
            "credit_50_wr_outside_overlap_pct",
            "delta_wr_pct",
        ])
        for grade, _ in GRADE_THRESHOLDS:
            g = by_grade.get(grade)
            if not g or g["n"] == 0:
                continue
            n = g["n"]
            n_ov = g["n_in_v83_overlap"]
            n_out = n - n_ov
            wr_in = _pct(g["n_win_50_in_overlap"], n_ov)
            wr_out = _pct(g["n_win_50_outside_overlap"], n_out)
            delta = wr_in - wr_out if n_ov > 0 and n_out > 0 else 0.0
            w.writerow([
                grade, n, n_ov,
                f"{_pct(n_ov, n):.1f}",
                f"{wr_in:.1f}", f"{wr_out:.1f}",
                f"{delta:+.1f}",
            ])
    log.info(f"  → {path}")


def write_summary_by_combo(by_combo: dict, path: Path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "itqs_grade", "tier", "bias", "regime", "n_trades",
            "credit_50_wr_5d_pct", "ev_50_avg_pct_of_maxrisk",
        ])
        ordered = sorted(by_combo.items(), key=lambda x: (-x[1]["n"], x[0]))
        for (grade, tier, bias, regime), d in ordered:
            if d["n"] < 30:
                continue
            w.writerow([
                grade, tier, bias, regime, d["n"],
                f"{_pct(d['n_win_50'], d['n']):.1f}",
                f"{d['ev50_sum'] / d['n'] * 100:+.1f}",
            ])
    log.info(f"  → {path}")


def write_decision_memo(by_grade: dict, by_combo: dict, path: Path,
                        total_computed: int):
    """Rebuild vs patch recommendation based on ITQS edge."""
    # Key numbers
    a_plus_a = {"n": 0, "win_50": 0, "ev50_sum": 0.0}
    b_plus = {"n": 0, "win_50": 0, "ev50_sum": 0.0}
    f_grade = {"n": 0, "win_50": 0, "ev50_sum": 0.0}
    for grade, _ in GRADE_THRESHOLDS:
        g = by_grade.get(grade, {})
        if grade in ("A+", "A"):
            a_plus_a["n"] += g.get("n", 0)
            a_plus_a["win_50"] += g.get("n_win_50", 0)
            a_plus_a["ev50_sum"] += g.get("ev50_sum", 0.0)
        if grade in ("A+", "A", "B"):
            b_plus["n"] += g.get("n", 0)
            b_plus["win_50"] += g.get("n_win_50", 0)
            b_plus["ev50_sum"] += g.get("ev50_sum", 0.0)
        if grade == "F":
            f_grade["n"] = g.get("n", 0)
            f_grade["win_50"] = g.get("n_win_50", 0)
            f_grade["ev50_sum"] = g.get("ev50_sum", 0.0)

    def _pct2(num, den):
        return 100.0 * num / den if den > 0 else 0.0

    wr_a = _pct2(a_plus_a["win_50"], a_plus_a["n"])
    wr_b = _pct2(b_plus["win_50"], b_plus["n"])
    wr_f = _pct2(f_grade["win_50"], f_grade["n"])
    ev_a = (a_plus_a["ev50_sum"] / a_plus_a["n"] * 100) if a_plus_a["n"] else 0.0
    ev_b = (b_plus["ev50_sum"] / b_plus["n"] * 100) if b_plus["n"] else 0.0

    lines = [
        "# ITQS Backtest — Rebuild vs Patch Decision Memo",
        "",
        f"Source: `{INPUT_CSV}`",
        f"ITQS computed on: **{total_computed:,}** active_scanner Tier 1/2 signals",
        "",
        "## Headline",
        "",
        f"| ITQS grade | n | credit_50 WR | EV ($5) |",
        "|---|---:|---:|---:|",
        f"| A+ / A | {a_plus_a['n']:,} | {wr_a:.1f}% | {ev_a:+.1f}% |",
        f"| A+ / A / B | {b_plus['n']:,} | {wr_b:.1f}% | {ev_b:+.1f}% |",
        f"| F (rejected) | {f_grade['n']:,} | {wr_f:.1f}% | {f_grade['ev50_sum'] / max(f_grade['n'],1) * 100:+.1f}% |",
        "",
        "## Recommendation",
        "",
    ]

    # Decision rule
    if wr_a >= 75.0 and wr_a - wr_f >= 8.0:
        lines += [
            "**REBUILD** — ITQS A+/A picks hit ≥75% credit WR AND meaningfully outperform "
            f"F-grade picks (delta {wr_a - wr_f:+.1f} WR pts). The Income Scanner has "
            "independent edge.",
            "",
            "Proceed with:",
            "1. New `credit_card_builder.py` + v8.3 scorer POST hook for v8.4 credit cards",
            "2. Leave `income_scanner.py` in place — it's running a real strategy",
            "3. Dashboard tracks both channels separately; decide later which to prioritize",
            "4. Env var `V84_CREDIT_DUAL_POST` defaults off",
        ]
    elif wr_b >= 70.0 and wr_b - wr_f >= 5.0:
        lines += [
            "**REBUILD (weaker case)** — ITQS B+ picks hit ≥70% credit WR with modest "
            f"delta vs F-grade ({wr_b - wr_f:+.1f} WR pts). Edge exists but is smaller "
            "than v8.3's. Keep both systems; prioritize v8.4 development.",
            "",
            "Proceed with:",
            "1. New `credit_card_builder.py` + v8.3 scorer POST hook",
            "2. Leave `income_scanner.py` in place for now",
            "3. After v8.4 has 4+ weeks live data, re-evaluate whether to deprecate Income Scanner",
        ]
    elif wr_a < 65.0 or (wr_a - wr_f) < 3.0:
        lines += [
            f"**PATCH** — ITQS A+/A WR is {wr_a:.1f}% with delta vs F-grade of only "
            f"{wr_a - wr_f:+.1f}pts. ITQS is not identifying a meaningfully winning "
            "subset on its own signal selection.",
            "",
            "Proceed with:",
            "1. Retarget `income_scanner.py` scan_ticker_income to consume v8.3 scorer signals",
            "2. Replace S/R strike selection with PB floor/roof (from v8.3 context)",
            "3. Replace spot-based width with ticker-class width ($2.50 / $5.00)",
            "4. Remove earnings-downgrade fallback (app.py:5834); income becomes first-class",
        ]
    else:
        lines += [
            f"**MUDDY** — ITQS A+/A WR is {wr_a:.1f}%, delta vs F = {wr_a - wr_f:+.1f}pts. "
            "Not a clean rebuild or patch verdict. Brad's call.",
            "",
            "If you lean rebuild: safest — coexist and observe.",
            "If you lean patch: retire the fuzzy signal picker, keep the infrastructure.",
        ]

    lines += [
        "",
        "## Caveats (read before acting)",
        "",
        "- Liquidity component (H) held constant at 3/5. Live varies 0-5 based on real "
        "chain bid/ask + OI — may pull some A-grades down to B in practice.",
        "- Event risk component (I) set to 0. Live penalizes earnings-window and "
        "macro-event days up to −10. Some A/B grades here would be F on earnings days.",
        "- No swing scanner confluence boost. Live boosts ITQS when swing scanner "
        "independently flags the same S/R level; unavailable historically.",
        "- Strikes use `_option_increment` fallback (no historical chain). Same "
        "approximation Phase 1 used.",
        "- Credit grading uses structural 33%-of-width proxy — real premium varies.",
        "",
        "## Next steps",
        "",
        "1. Confirm verdict with Brad",
        "2. Based on verdict → Phase 3 Rebuild OR Phase 3 Patch design doc",
        "3. Either path: deploy env-gated behind `V84_CREDIT_DUAL_POST` default off",
    ]

    path.write_text("\n".join(lines))
    log.info(f"  → {path}")


if __name__ == "__main__":
    main()
