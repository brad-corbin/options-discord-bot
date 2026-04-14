#!/usr/bin/env python3
"""
bt_conviction.py — Conviction Flow + Shadow Signal Backtest
═══════════════════════════════════════════════════════════
Since historical flow data isn't available for replay, this backtest
tests the SHADOW signal model (the technical layer that validates
flow signals) and simulates conviction-style setups using:

  1. Shadow signals: intraday technical signals filtered by regime rules
  2. Volume burst detection: unusual volume as a proxy for institutional flow
  3. Directional bias from EMA/MACD/WaveTrend alignment
  4. Potter box location overlay when available

This answers:
  - "When shadow agrees with a directional signal, does WR improve?"
  - "Do high-volume bars predict direction over 1-5 days?"
  - "Does regime filtering improve conviction signal outcomes?"

Usage:
  python backtest/bt_conviction.py --ticker TSLA --days 180
  python backtest/bt_conviction.py --all
"""

import os, sys, csv, json, argparse
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bt_shared import *

VOLUME_BURST_MULT = 2.0  # Volume >= 2x 20-bar avg = "burst"
MIN_SCORE_CONVICTION = 60
SHADOW_MIN_SCORE = 55

CONVICTION_TICKERS = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META",
    "TSLA", "GOOGL", "AMD", "AVGO", "PLTR", "COIN",
]


def detect_shadow_signal(window, daily_closes, regime="BEAR"):
    """Shadow signal = technical signal that may or may not pass regime rules."""
    if len(window) < 20: return None
    closes = [b["c"] for b in window]
    highs = [b["h"] for b in window if b.get("h") is not None]
    lows = [b["l"] for b in window if b.get("l") is not None]
    volumes = [b.get("v", 0) or 0 for b in window]
    spot = closes[-1]; ml = min(len(closes), len(highs), len(lows), len(volumes))

    ema5 = ema(closes, EMA_FAST); ema12 = ema(closes, EMA_SLOW)
    if not ema5 or not ema12: return None
    ema_bull = ema5[-1] > ema12[-1]
    bias = "bull" if ema_bull else "bear"

    # Score (same as active scanner)
    m = macd(closes)
    hlc3 = [(highs[i]+lows[i]+closes[i])/3 for i in range(ml)]
    wt = wavetrend(hlc3)
    rsi_val = rsi(closes)

    score = 0
    ema_dist = ((ema5[-1]-ema12[-1])/ema12[-1])*100 if ema12[-1] > 0 else 0
    if abs(ema_dist) > 0.03: score += 15
    elif abs(ema_dist) > 0.01: score += 8
    if m:
        h = m.get("macd_hist", 0)
        if (bias=="bull" and h>0) or (bias=="bear" and h<0): score += 15
        elif h != 0: score -= 10
    if wt:
        if bias=="bull" and wt.get("wt_oversold"): score += 15
        elif bias=="bear" and wt.get("wt_overbought"): score += 15
        elif bias=="bull" and wt.get("wt_overbought"): score -= 10
        elif bias=="bear" and wt.get("wt_oversold"): score -= 10

    # VWAP
    vwap_val = None
    if ml > 0:
        tpv = sum((highs[i]+lows[i]+closes[i])/3*volumes[i] for i in range(ml) if volumes[i]>0)
        vs = sum(v for v in volumes[:ml] if v>0)
        if vs > 0: vwap_val = tpv / vs
    if vwap_val:
        if (bias=="bull" and spot>vwap_val) or (bias=="bear" and spot<vwap_val): score += 10
        else: score -= 5

    # HTF
    htf_status = "UNKNOWN"
    if daily_closes and len(daily_closes) >= 21:
        de8 = ema(daily_closes, 8); de21 = ema(daily_closes, 21)
        if de8 and de21:
            d_bull = de8[-1] > de21[-1]
            if d_bull == ema_bull: score += 15; htf_status = "CONFIRMED"
            else:
                gn = abs(de8[-1]-de21[-1]); gp = abs(de8[-2]-de21[-2]) if len(de8)>=2 else gn
                if gn < gp*0.98: score += 10; htf_status = "CONVERGING"
                else: score -= 10; htf_status = "OPPOSING"

    # Volume burst
    avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if len(volumes) >= 20 else sum(volumes)/max(1,len(volumes))
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    volume_burst = vol_ratio >= VOLUME_BURST_MULT
    if vol_ratio > 1.5: score += 10

    if rsi_val:
        if (bias=="bull" and 40<rsi_val<65) or (bias=="bear" and 35<rsi_val<60): score += 5

    if score < SHADOW_MIN_SCORE: return None

    return {
        "bias": bias, "score": score, "htf_status": htf_status,
        "volume_burst": volume_burst, "vol_ratio": round(vol_ratio, 2),
        "rsi": rsi_val, "above_vwap": spot > vwap_val if vwap_val else None,
        "close": spot,
    }


def run_conviction_backtest(ticker, intraday, daily_bars, regime_bars=None):
    """Run conviction/shadow backtest."""
    daily_close_by_date = {b["date"]: b["c"] for b in daily_bars}
    sorted_dates = sorted(daily_close_by_date.keys())

    spy_b = regime_bars.get("SPY", []) if regime_bars else []
    qqq_b = regime_bars.get("QQQ", []) if regime_bars else []
    iwm_b = regime_bars.get("IWM", []) if regime_bars else []
    vix_b = regime_bars.get("VIX", []) if regime_bars else []

    bars_by_date = {}
    for b in intraday:
        bars_by_date.setdefault(b["date"], []).append(b)

    trades = []; last_sig = {}

    for trade_date in sorted(bars_by_date.keys()):
        day_bars = bars_by_date[trade_date]
        dc = [daily_close_by_date[d] for d in sorted_dates if d <= trade_date]

        if spy_b and qqq_b and iwm_b:
            _, _, regime, _ = compute_regime_for_date(spy_b, qqq_b, iwm_b, vix_b, trade_date)
        else:
            regime = "BEAR"

        # Only check a few bars per day (not every bar)
        check_indices = list(range(5, len(day_bars), 6))  # Every 30 min
        for i in check_indices:
            window = day_bars[max(0,i-79):i+1]
            sig = detect_shadow_signal(window, dc[-30:], regime)
            if sig is None: continue

            key = (ticker, sig["bias"])
            if key in last_sig and trade_date == last_sig[key]: continue
            last_sig[key] = trade_date

            # Regime filtering
            valid, reason = is_signal_valid_for_regime(
                ticker, sig["bias"], sig["score"], sig["htf_status"], regime)

            entry_price = sig["close"]
            phase = time_phase(day_bars[i]["time_ct"])
            exits = exit_dates_for(trade_date, sorted_dates)

            t = {
                "ticker": ticker, "date": trade_date, "time_ct": day_bars[i]["time_ct"],
                "phase": phase, "bias": sig["bias"], "score": sig["score"],
                "htf_status": sig["htf_status"], "volume_burst": sig["volume_burst"],
                "vol_ratio": sig["vol_ratio"], "rsi": sig["rsi"],
                "above_vwap": sig["above_vwap"], "entry_price": round(entry_price, 4),
                "regime": regime, "regime_valid": valid,
                "is_conviction": sig["score"] >= MIN_SCORE_CONVICTION and sig["volume_burst"],
                "shadow_agrees": sig["htf_status"] == "CONFIRMED",
            }

            for label, exit_date in exits.items():
                if exit_date and exit_date in daily_close_by_date:
                    exit_p = daily_close_by_date[exit_date]
                    pnl = (exit_p - entry_price) if sig["bias"]=="bull" else (entry_price - exit_p)
                    pnl_pct = pnl / entry_price * 100
                    t[f"pnl_pct_{label}"] = round(pnl_pct, 3)
                    t[f"win_{label}"] = pnl > 0
                else:
                    t[f"pnl_pct_{label}"] = None; t[f"win_{label}"] = None

            trades.append(t)

    return trades


def write_conviction_summary(ticker, trades, out_dir):
    if not trades: return
    n = len(trades)
    lines = [f"\n{'='*62}", f"  CONVICTION/SHADOW BACKTEST — {ticker}", f"  Signals: {n}", f"{'='*62}"]

    # All signals
    for label in ["eod","1d","3d","5d"]:
        s = compute_win_stats(trades, label)
        if s["n"]: lines.append(f"  {label}: {s['wr']:.1f}%WR  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    # Volume burst vs normal
    burst = [t for t in trades if t["volume_burst"]]
    normal = [t for t in trades if not t["volume_burst"]]
    if len(burst) >= 10:
        lines.append(f"\n  VOLUME BURST ({len(burst)} signals):")
        for h_ in ["1d","3d","5d"]:
            s = compute_win_stats(burst, h_)
            if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}%WR  avg {s['avg_pct']:+.3f}%")
    if len(normal) >= 10:
        lines.append(f"\n  NORMAL VOLUME ({len(normal)} signals):")
        for h_ in ["1d","3d","5d"]:
            s = compute_win_stats(normal, h_)
            if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}%WR  avg {s['avg_pct']:+.3f}%")

    # Shadow agrees vs not
    agrees = [t for t in trades if t["shadow_agrees"]]
    disagrees = [t for t in trades if not t["shadow_agrees"]]
    if len(agrees) >= 10:
        lines.append(f"\n  SHADOW AGREES (HTF CONFIRMED) ({len(agrees)}):")
        for h_ in ["1d","3d","5d"]:
            s = compute_win_stats(agrees, h_)
            if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}%WR  avg {s['avg_pct']:+.3f}%")

    # Conviction (high score + volume burst)
    conv = [t for t in trades if t["is_conviction"]]
    if len(conv) >= 5:
        lines.append(f"\n  CONVICTION (score≥60 + vol burst) ({len(conv)}):")
        for h_ in ["1d","3d","5d"]:
            s = compute_win_stats(conv, h_)
            if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}%WR  avg {s['avg_pct']:+.3f}%")

    # Regime-valid
    valid = [t for t in trades if t["regime_valid"]]
    if len(valid) >= 10:
        lines.append(f"\n  REGIME-VALID ({len(valid)}):")
        for h_ in ["1d","3d","5d"]:
            s = compute_win_stats(valid, h_)
            if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}%WR  avg {s['avg_pct']:+.3f}%")

    txt = "\n".join(lines) + f"\n{'='*62}\n"
    p = out_dir / f"conviction_v1_{ticker}.txt"
    p.write_text(txt); print(txt)


def main():
    ap = argparse.ArgumentParser(description="Conviction/Shadow Backtest")
    ap.add_argument("--ticker", default=None); ap.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=270); ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    tickers = CONVICTION_TICKERS if args.all else ([args.ticker.upper()] if args.ticker else ["TSLA"])
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=args.days)).isoformat()
    daily_from = (date.today() - timedelta(days=args.days + 100)).isoformat()

    regime_bars = {}
    for t in ["SPY","QQQ","IWM"]:
        regime_bars[t] = download_daily(t, daily_from, to_date)
    regime_bars["VIX"] = []

    for ticker in tickers:
        print(f"\n{'='*62}\n  CONVICTION BACKTEST — {ticker}\n{'='*62}")
        intraday = download_5min(ticker, from_date, to_date)
        daily = download_daily(ticker, daily_from, to_date)
        if not intraday or not daily: print("  Insufficient data"); continue
        intraday = [b for b in intraday if is_market_bar(b["time_ct"])]
        trades = run_conviction_backtest(ticker, intraday, daily, regime_bars)
        print(f"  Signals: {len(trades)}")
        if trades:
            write_csv(out_dir / f"conviction_v1_trades_{ticker}.csv", trades)
            write_conviction_summary(ticker, trades, out_dir)


if __name__ == "__main__":
    main()
