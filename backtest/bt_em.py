#!/usr/bin/env python3
"""
bt_em.py — Expected Move Model Backtest
════════════════════════════════════════
Tests the EM prediction model by:
  1. Computing daily expected move from ATR + VIX
  2. Predicting direction from daily bias score
  3. Reconciling against actual EOD price
  4. Measuring: 1σ/2σ containment, direction accuracy, condor success, move ratio

This answers: "If we sell premium at EM boundaries, how often do we win?"

Usage:
  python backtest/bt_em.py --ticker SPY
  python backtest/bt_em.py --ticker SPY --ticker QQQ --days 365
"""

import os, sys, csv, json, argparse, math
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bt_shared import *


def compute_expected_move(closes, highs, lows, vix_val=None, period=14):
    """Compute 1-day expected move (1σ) from ATR and optional VIX."""
    if len(closes) < period + 1: return None
    # ATR-based EM
    atr_val = atr(highs, lows, closes, period)
    if atr_val is None: return None

    # If VIX available, blend: EM = (ATR + VIX-implied) / 2
    if vix_val and vix_val > 0:
        spot = closes[-1]
        vix_daily_move = spot * (vix_val / 100) / math.sqrt(252)
        em_1sd = (atr_val + vix_daily_move) / 2
    else:
        em_1sd = atr_val

    return em_1sd


def compute_bias_score(closes):
    """Compute directional bias from EMA/RSI alignment. Returns -3 to +3."""
    if len(closes) < 21: return 0
    score = 0
    ema8 = ema(closes, 8); ema21 = ema(closes, 21)
    if ema8 and ema21:
        if ema8[-1] > ema21[-1]: score += 1
        else: score -= 1
        # Slope
        if len(ema8) >= 2 and ema8[-1] > ema8[-2]: score += 1
        elif len(ema8) >= 2 and ema8[-1] < ema8[-2]: score -= 1

    rsi_val = rsi(closes)
    if rsi_val:
        if rsi_val > 55: score += 1
        elif rsi_val < 45: score -= 1

    return max(-3, min(3, score))


def run_em_backtest(ticker, daily_bars, vix_bars=None, lookback_min=30):
    """Walk-forward EM prediction backtest."""
    if len(daily_bars) < lookback_min + 1:
        print(f"  Not enough bars ({len(daily_bars)})"); return []

    vix_by_date = {b["date"]: b["c"] for b in vix_bars} if vix_bars else {}
    predictions = []

    for i in range(lookback_min, len(daily_bars) - 1):
        today = daily_bars[i]
        tomorrow = daily_bars[i + 1]
        today_date = today["date"]
        spot = today["c"]

        closes = [b["c"] for b in daily_bars[:i+1]]
        highs  = [b["h"] for b in daily_bars[:i+1]]
        lows   = [b["l"] for b in daily_bars[:i+1]]

        vix_val = vix_by_date.get(today_date)
        em_1sd = compute_expected_move(closes, highs, lows, vix_val)
        if em_1sd is None or em_1sd <= 0: continue

        em_2sd = em_1sd * 2
        bull_1sd = spot + em_1sd; bear_1sd = spot - em_1sd
        bull_2sd = spot + em_2sd; bear_2sd = spot - em_2sd

        bias_score = compute_bias_score(closes)
        if bias_score >= 2:    predicted_dir = "bullish"
        elif bias_score <= -2: predicted_dir = "bearish"
        else:                  predicted_dir = "neutral"

        # Reconcile against next day close
        eod = tomorrow["c"]
        actual_move = eod - spot
        actual_dir = "bullish" if actual_move > 0 else "bearish" if actual_move < 0 else "neutral"

        move_abs = abs(actual_move)
        move_ratio = move_abs / em_1sd if em_1sd > 0 else 0
        in_1_sigma = move_abs <= em_1sd
        in_2_sigma = move_abs <= em_2sd

        # Direction correctness
        dir_correct = (predicted_dir == actual_dir) or predicted_dir == "neutral"
        # Buffered: correct if within 0.3 * EM of prediction
        buffer = em_1sd * 0.3
        if predicted_dir == "neutral":
            buffered_correct = move_abs <= buffer
        elif predicted_dir == "bullish":
            buffered_correct = actual_move > -buffer
        else:
            buffered_correct = actual_move < buffer

        # Condor success: price stays within EM boundaries
        # (both high and low of next day within 1σ)
        next_high = tomorrow["h"]; next_low = tomorrow["l"]
        condor_success = next_high <= bull_1sd and next_low >= bear_1sd

        predictions.append({
            "date": today_date, "next_date": tomorrow["date"],
            "ticker": ticker, "spot": round(spot, 2), "eod_price": round(eod, 2),
            "em_1sd": round(em_1sd, 2), "em_2sd": round(em_2sd, 2),
            "bull_1sd": round(bull_1sd, 2), "bear_1sd": round(bear_1sd, 2),
            "bull_2sd": round(bull_2sd, 2), "bear_2sd": round(bear_2sd, 2),
            "vix": round(vix_val, 1) if vix_val else None,
            "bias_score": bias_score, "predicted_dir": predicted_dir, "actual_dir": actual_dir,
            "actual_move": round(actual_move, 3), "move_abs": round(move_abs, 3),
            "move_ratio": round(move_ratio, 4),
            "in_1_sigma": in_1_sigma, "in_2_sigma": in_2_sigma,
            "dir_correct": dir_correct, "buffered_correct": buffered_correct,
            "condor_success": condor_success,
            "next_high": round(next_high, 2), "next_low": round(next_low, 2),
        })

    return predictions


def write_em_summary(ticker, preds, out_dir):
    if not preds: print(f"  No predictions for {ticker}"); return
    n = len(preds)
    lines = []; SEP = "=" * 62

    lines.append(f"\n{SEP}")
    lines.append(f"  EM MODEL BACKTEST — {ticker}")
    lines.append(f"  Period: {preds[0]['date']} → {preds[-1]['date']}")
    lines.append(f"  Total predictions: {n}")
    lines.append(f"{SEP}")

    # Core metrics
    sigma1 = sum(1 for p in preds if p["in_1_sigma"]) / n * 100
    sigma2 = sum(1 for p in preds if p["in_2_sigma"]) / n * 100
    dir_acc = sum(1 for p in preds if p["dir_correct"]) / n * 100
    buf_acc = sum(1 for p in preds if p["buffered_correct"]) / n * 100
    condor = sum(1 for p in preds if p["condor_success"]) / n * 100
    avg_ratio = sum(p["move_ratio"] for p in preds) / n
    med_ratio = sorted(p["move_ratio"] for p in preds)[n // 2]

    lines.append(f"\n  1σ containment:    {sigma1:.1f}%")
    lines.append(f"  2σ containment:    {sigma2:.1f}%")
    lines.append(f"  Direction correct: {dir_acc:.1f}%")
    lines.append(f"  Buffered correct:  {buf_acc:.1f}%")
    lines.append(f"  Condor success:    {condor:.1f}%")
    lines.append(f"  Avg move ratio:    {avg_ratio:.3f} (actual/predicted)")
    lines.append(f"  Median move ratio: {med_ratio:.3f}")

    # By bias score
    lines.append(f"\n{SEP}\n  BY BIAS SCORE\n{SEP}")
    for bs in range(-3, 4):
        sub = [p for p in preds if p["bias_score"] == bs]
        if len(sub) >= 5:
            s1 = sum(1 for p in sub if p["in_1_sigma"]) / len(sub) * 100
            dc = sum(1 for p in sub if p["dir_correct"]) / len(sub) * 100
            cs = sum(1 for p in sub if p["condor_success"]) / len(sub) * 100
            lines.append(f"  Bias {bs:+d}: {len(sub):>3} preds  1σ={s1:.0f}%  dir={dc:.0f}%  condor={cs:.0f}%")

    # Premium selling edge
    lines.append(f"\n{SEP}\n  PREMIUM SELLING EDGE\n{SEP}")
    lines.append(f"  If selling iron condors at 1σ boundaries:")
    lines.append(f"    Win rate: {condor:.1f}%")
    lines.append(f"    Avg move is {avg_ratio:.1f}x of predicted EM")
    lines.append(f"    Only {100-sigma1:.1f}% of days breach 1σ")

    # Tighten to 0.75σ
    tight = sum(1 for p in preds if abs(p["actual_move"]) <= p["em_1sd"] * 0.75) / n * 100
    lines.append(f"\n  If selling at 0.75σ (tighter for more premium):")
    lines.append(f"    Containment: {tight:.1f}%")

    # Move distribution
    lines.append(f"\n{SEP}\n  MOVE RATIO DISTRIBUTION\n{SEP}")
    for pct in [10, 25, 50, 75, 90]:
        idx = int(n * pct / 100)
        val = sorted(p["move_ratio"] for p in preds)[min(idx, n-1)]
        lines.append(f"  P{pct}: {val:.3f}")

    lines.append(f"\n{SEP}\n")
    txt = "\n".join(lines)
    p = out_dir / f"em_backtest_{ticker}.txt"
    p.write_text(txt)
    print(f"\n  Summary → {p}")
    print(txt)


def main():
    ap = argparse.ArgumentParser(description="EM Model Backtest")
    ap.add_argument("--ticker", action="append", default=None)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    tickers = args.ticker or ["SPY", "QQQ"]
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=args.days + 30)).isoformat()

    # Download VIX
    vix_bars = download_vix(from_date, to_date)

    for ticker in tickers:
        print(f"\n{'='*62}")
        print(f"  EM BACKTEST — {ticker}")
        print(f"{'='*62}\n")

        daily = download_daily(ticker, from_date, to_date)
        if not daily or len(daily) < 40: print(f"  Insufficient data"); continue

        preds = run_em_backtest(ticker, daily, vix_bars)
        if preds:
            write_csv(out_dir / f"em_preds_{ticker}.csv", preds)
            write_em_summary(ticker, preds, out_dir)


if __name__ == "__main__":
    main()
