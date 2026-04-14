#!/usr/bin/env python3
"""
bt_income.py — Income Scanner Backtest v1
═════════════════════════════════════════
Tests the income (credit spread) scanner by:
  1. Detecting support/resistance levels from daily bars
  2. Scoring via ITQS (regime, weekly, daily, support quality, cushion, RSI)
  3. Simulating bull put / bear call spreads
  4. Checking if short strike was breached within DTE window

This answers: "Do our support/resistance levels hold for premium selling?"

Usage:
  python backtest/bt_income.py --ticker AAPL --days 365
  python backtest/bt_income.py --all
"""

import os, sys, csv, json, argparse, math
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bt_shared import *

DTE_WINDOW = 7  # Weekly income trades: 5-7 DTE
CUSHION_MIN_PCT = 2.0
LEVEL_LOOKBACK = 60
TOUCH_TOLERANCE_PCT = 1.0
MIN_TOUCHES = 2
SCAN_INTERVAL = 5  # Check every 5 bars (weekly)

INCOME_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
    "SPY", "QQQ", "IWM", "AMD", "AVGO", "PLTR",
]


def detect_support_levels(lows, spot, lookback=LEVEL_LOOKBACK, tolerance_pct=TOUCH_TOLERANCE_PCT, min_touches=MIN_TOUCHES):
    """Cluster daily lows into support levels."""
    if len(lows) < lookback: return []
    recent_lows = lows[-lookback:]
    tolerance = spot * tolerance_pct / 100
    levels = []
    used = set()
    for i, lo in enumerate(recent_lows):
        if i in used or lo >= spot: continue
        cluster = [lo]; indices = [i]
        for j, lo2 in enumerate(recent_lows):
            if j != i and j not in used and abs(lo2 - lo) <= tolerance:
                cluster.append(lo2); indices.append(j)
        if len(cluster) >= min_touches:
            avg = sum(cluster) / len(cluster)
            for idx in indices: used.add(idx)
            cushion = (spot - avg) / spot * 100
            levels.append({
                "level": round(avg, 2), "touches": len(cluster),
                "cushion_pct": round(cushion, 1), "quality": "strong" if len(cluster) >= 3 else "moderate",
                "last_touch_days_ago": lookback - max(indices),
            })
    return sorted(levels, key=lambda x: -x["cushion_pct"])


def detect_resistance_levels(highs, spot, lookback=LEVEL_LOOKBACK, tolerance_pct=TOUCH_TOLERANCE_PCT, min_touches=MIN_TOUCHES):
    """Cluster daily highs into resistance levels."""
    if len(highs) < lookback: return []
    recent_highs = highs[-lookback:]
    tolerance = spot * tolerance_pct / 100
    levels = []
    used = set()
    for i, hi in enumerate(recent_highs):
        if i in used or hi <= spot: continue
        cluster = [hi]; indices = [i]
        for j, hi2 in enumerate(recent_highs):
            if j != i and j not in used and abs(hi2 - hi) <= tolerance:
                cluster.append(hi2); indices.append(j)
        if len(cluster) >= min_touches:
            avg = sum(cluster) / len(cluster)
            for idx in indices: used.add(idx)
            cushion = (avg - spot) / spot * 100
            levels.append({
                "level": round(avg, 2), "touches": len(cluster),
                "cushion_pct": round(cushion, 1), "quality": "strong" if len(cluster) >= 3 else "moderate",
                "last_touch_days_ago": lookback - max(indices),
            })
    return sorted(levels, key=lambda x: -x["cushion_pct"])


def score_itqs_simple(trade_type, cushion_pct, touches, rsi_val, weekly_bull, daily_bull, regime):
    """Simplified ITQS scoring (no option chain needed)."""
    score = 0

    # A. Regime (0-15)
    if trade_type == "bull_put":
        a = {"BULL": 15, "TRANSITION": 8, "BEAR": 3}.get(regime, 5)
    else:
        a = {"BULL": 3, "TRANSITION": 8, "BEAR": 15}.get(regime, 5)
    score += a

    # B. Weekly (0-15)
    if (trade_type == "bull_put" and weekly_bull) or (trade_type == "bear_call" and not weekly_bull):
        score += 15
    else:
        score += 5

    # C. Daily (0-15)
    if (trade_type == "bull_put" and daily_bull) or (trade_type == "bear_call" and not daily_bull):
        score += 13
    else:
        score += 5

    # D. Support quality (0-15)
    if touches >= 3: score += 15
    elif touches >= 2: score += 10
    else: score += 5

    # F. Cushion (0-10)
    if 4 <= cushion_pct <= 8: score += 10
    elif 3 <= cushion_pct < 4: score += 7
    elif 2 <= cushion_pct < 3: score += 4
    elif cushion_pct > 8: score += 6
    else: score += 0

    # G. RSI (0-5)
    if rsi_val:
        if trade_type == "bull_put" and 40 <= rsi_val <= 55: score += 5
        elif trade_type == "bear_call" and 55 <= rsi_val <= 70: score += 5
        else: score += 2

    return min(100, max(0, score))


def run_income_backtest(ticker, daily_bars, regime_bars=None):
    """Walk-forward income scanner backtest."""
    if len(daily_bars) < LEVEL_LOOKBACK + DTE_WINDOW + 10: return []
    trades = []

    # Regime per date
    spy_b = regime_bars.get("SPY", []) if regime_bars else []
    qqq_b = regime_bars.get("QQQ", []) if regime_bars else []
    iwm_b = regime_bars.get("IWM", []) if regime_bars else []
    vix_b = regime_bars.get("VIX", []) if regime_bars else []

    for i in range(LEVEL_LOOKBACK, len(daily_bars) - DTE_WINDOW, SCAN_INTERVAL):
        bar = daily_bars[i]; spot = bar["c"]; scan_date = bar["date"]
        closes = [b["c"] for b in daily_bars[:i+1]]
        highs = [b["h"] for b in daily_bars[:i+1]]
        lows = [b["l"] for b in daily_bars[:i+1]]

        # Regime
        if spy_b and qqq_b and iwm_b:
            _, _, regime, _ = compute_regime_for_date(spy_b, qqq_b, iwm_b, vix_b, scan_date)
        else:
            regime = "BEAR"

        # Trends
        ema8 = ema(closes, 8); ema21 = ema(closes, 21)
        daily_bull = ema8[-1] > ema21[-1] if ema8 and ema21 else True
        w_bars = _aggregate_weekly_simple(daily_bars[:i+1])
        wc = [w["c"] for w in w_bars]
        we5 = ema(wc, 5); we20 = ema(wc, 20)
        weekly_bull = we5[-1] > we20[-1] if we5 and we20 else True
        rsi_val = rsi(closes)

        # Support levels → bull put candidates
        supports = detect_support_levels(lows, spot)
        for sup in supports:
            if sup["cushion_pct"] < CUSHION_MIN_PCT: continue
            short_strike = sup["level"]
            itqs = score_itqs_simple("bull_put", sup["cushion_pct"], sup["touches"],
                                     rsi_val, weekly_bull, daily_bull, regime)

            # Check outcome: was short strike breached within DTE window?
            future_bars = daily_bars[i+1:i+1+DTE_WINDOW]
            if not future_bars: continue
            breached = any(b["l"] <= short_strike for b in future_bars)
            eod_price = future_bars[-1]["c"]
            eod_above = eod_price > short_strike

            trades.append({
                "ticker": ticker, "scan_date": scan_date, "trade_type": "bull_put",
                "spot": round(spot, 2), "short_strike": round(short_strike, 2),
                "cushion_pct": sup["cushion_pct"], "touches": sup["touches"],
                "quality": sup["quality"], "itqs_score": itqs,
                "grade": "A" if itqs >= 85 else "B" if itqs >= 75 else "C" if itqs >= 65 else "F",
                "regime": regime, "weekly_bull": weekly_bull, "daily_bull": daily_bull,
                "rsi": round(rsi_val, 1) if rsi_val else None,
                "strike_breached": breached, "eod_above_strike": eod_above,
                "win": not breached, "eod_price": round(eod_price, 2),
                "max_adverse": round(min(b["l"] for b in future_bars) - short_strike, 2),
            })

        # Resistance levels → bear call candidates
        resistances = detect_resistance_levels(highs, spot)
        for res in resistances:
            if res["cushion_pct"] < CUSHION_MIN_PCT: continue
            short_strike = res["level"]
            itqs = score_itqs_simple("bear_call", res["cushion_pct"], res["touches"],
                                     rsi_val, weekly_bull, daily_bull, regime)

            future_bars = daily_bars[i+1:i+1+DTE_WINDOW]
            if not future_bars: continue
            breached = any(b["h"] >= short_strike for b in future_bars)
            eod_price = future_bars[-1]["c"]
            eod_below = eod_price < short_strike

            trades.append({
                "ticker": ticker, "scan_date": scan_date, "trade_type": "bear_call",
                "spot": round(spot, 2), "short_strike": round(short_strike, 2),
                "cushion_pct": res["cushion_pct"], "touches": res["touches"],
                "quality": res["quality"], "itqs_score": itqs,
                "grade": "A" if itqs >= 85 else "B" if itqs >= 75 else "C" if itqs >= 65 else "F",
                "regime": regime, "weekly_bull": weekly_bull, "daily_bull": daily_bull,
                "rsi": round(rsi_val, 1) if rsi_val else None,
                "strike_breached": breached, "eod_above_strike": eod_price > short_strike,
                "win": not breached, "eod_price": round(eod_price, 2),
                "max_adverse": round(short_strike - max(b["h"] for b in future_bars), 2),
            })

    return trades


def _aggregate_weekly_simple(bars):
    from datetime import datetime as dt
    weeks = {}
    for b in bars:
        try:
            d = dt.strptime(b["date"], "%Y-%m-%d")
            wk = d.strftime("%Y-W%W")
            weeks.setdefault(wk, []).append(b)
        except: pass
    return [{"c": bs[-1]["c"]} for wk, bs in sorted(weeks.items())]


def write_income_summary(ticker, trades, out_dir):
    if not trades: print(f"  No income trades for {ticker}"); return
    n = len(trades)
    lines = [f"\n{'='*62}", f"  INCOME SCANNER BACKTEST — {ticker}", f"  Trades: {n}", f"{'='*62}"]

    wins = sum(1 for t in trades if t["win"]); wr = wins/n*100
    lines.append(f"  Overall WR: {wr:.1f}% ({wins}/{n})")

    for tt in ["bull_put", "bear_call"]:
        sub = [t for t in trades if t["trade_type"] == tt]
        if sub:
            w = sum(1 for t in sub if t["win"])
            lines.append(f"\n  {tt.upper()}: {len(sub)}T  WR {w/len(sub)*100:.1f}%")

    for grade in ["A", "B", "C", "F"]:
        sub = [t for t in trades if t["grade"] == grade]
        if len(sub) >= 5:
            w = sum(1 for t in sub if t["win"])
            lines.append(f"  Grade {grade}: {len(sub)}T  WR {w/len(sub)*100:.1f}%")

    for lo, hi, lbl in [(2,4,"2-4%"),(4,6,"4-6%"),(6,8,"6-8%"),(8,99,"8%+")]:
        sub = [t for t in trades if lo <= t["cushion_pct"] < hi]
        if len(sub) >= 5:
            w = sum(1 for t in sub if t["win"])
            lines.append(f"  Cushion {lbl}: {len(sub)}T  WR {w/len(sub)*100:.1f}%")

    for regime in ["BULL", "TRANSITION", "BEAR"]:
        sub = [t for t in trades if t["regime"] == regime]
        if len(sub) >= 5:
            w = sum(1 for t in sub if t["win"])
            lines.append(f"  Regime {regime}: {len(sub)}T  WR {w/len(sub)*100:.1f}%")

    txt = "\n".join(lines) + f"\n{'='*62}\n"
    p = out_dir / f"income_v1_{ticker}.txt"
    p.write_text(txt); print(txt)


def main():
    ap = argparse.ArgumentParser(description="Income Scanner Backtest")
    ap.add_argument("--ticker", default=None); ap.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=365); ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    tickers = INCOME_TICKERS if args.all else ([args.ticker.upper()] if args.ticker else ["SPY"])
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=args.days + 30)).isoformat()

    regime_bars = {}
    for t in ["SPY","QQQ","IWM"]:
        regime_bars[t] = download_daily(t, from_date, to_date)

    for ticker in tickers:
        print(f"\n{'='*62}\n  INCOME BACKTEST — {ticker}\n{'='*62}")
        daily = download_daily(ticker, from_date, to_date)
        if not daily or len(daily) < 80: print("  Insufficient data"); continue
        trades = run_income_backtest(ticker, daily, regime_bars)
        print(f"  Trades: {len(trades)}")
        if trades:
            write_csv(out_dir / f"income_v1_trades_{ticker}.csv", trades)
            write_income_summary(ticker, trades, out_dir)


if __name__ == "__main__":
    main()
