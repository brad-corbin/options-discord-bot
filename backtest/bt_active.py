#!/usr/bin/env python3
"""
bt_active.py — Active Scanner Backtest v7 (Regime-Aware)
════════════════════════════════════════════════════════════
Matches production active_scanner.py as of 2026-04-13.

NEW vs old active_backtest.py:
  - Regime detection per day (BULL/TRANSITION/BEAR from SPY+QQQ+IWM)
  - Ticker rules filtering (score_min/max, bias restriction, htf requirement)
  - TRANSITION-specific HTF scoring (CONVERGING +12 instead of +10)
  - TRANSITION RSI window shift (50-75 for bull, not 40-65)
  - Flow boost scoring placeholder (future: connect to flow data)
  - Max hold from ticker rules (not fixed 5d)
  - Regime-specific exit horizons measured

Usage:
  python backtest/bt_active.py --ticker NVDA
  python backtest/bt_active.py --ticker NVDA --from 2025-07-01 --to 2026-04-10
  python backtest/bt_active.py --ticker NVDA --days 180
  python backtest/bt_active.py --all   # Run all watchlist tickers
"""

import os, sys, csv, json, argparse
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bt_shared import *

SIGNAL_TIER_1_SCORE = 75
MIN_SIGNAL_SCORE = 55   # v6.0: raised from 50 to 55
DEDUP_BARS = 3

ALL_TICKERS = [
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL",
    "GLD", "SLV", "AMD", "NFLX", "COIN", "AVGO", "PLTR",
]


def detect_signal(window, daily_closes, regime="BEAR"):
    """Score a signal from 5min bars + daily context. Returns dict or None."""
    if len(window) < 12: return None
    closes = [b["c"] for b in window]
    highs  = [b["h"] for b in window if b.get("h") is not None]
    lows   = [b["l"] for b in window if b.get("l") is not None]
    volumes = [b.get("v", 0) or 0 for b in window]
    spot = closes[-1]
    bc = len(closes)
    dq = "full" if bc >= 40 else "partial" if bc >= 20 else "minimal"

    # ADTV gate
    if len(volumes) >= 10:
        avg10 = sum(volumes[-10:]) / 10
        if avg10 * spot * 5 * 60 < 5_000_000: return None

    # VWAP
    vwap_val = None
    ml = min(len(closes), len(highs), len(lows), len(volumes))
    if ml > 0:
        tpv = sum((highs[i]+lows[i]+closes[i])/3*volumes[i] for i in range(ml) if volumes[i] > 0)
        vs  = sum(v for v in volumes[:ml] if v > 0)
        if vs > 0: vwap_val = tpv / vs

    # EMA
    ema5 = ema(closes, EMA_FAST); ema12 = ema(closes, EMA_SLOW)
    if not ema5 or not ema12: return None
    ema_bull = ema5[-1] > ema12[-1]
    ema_dist = ((ema5[-1]-ema12[-1])/ema12[-1])*100 if ema12[-1] > 0 else 0

    m = macd(closes)
    hlc3 = [(highs[i]+lows[i]+closes[i])/3 for i in range(ml)]
    wt = wavetrend(hlc3)
    rsi_val = rsi(closes, RSI_PERIOD)

    avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    # Daily HTF
    daily_bull_v = htf_conf = htf_conv = False
    htf_status = "UNKNOWN"
    if daily_closes and len(daily_closes) >= 21:
        de8 = ema(daily_closes, 8); de21 = ema(daily_closes, 21)
        if de8 and de21 and len(de8) >= 2:
            daily_bull_v = de8[-1] > de21[-1]
            htf_conf = daily_bull_v == ema_bull
            if htf_conf:
                htf_status = "CONFIRMED"
            else:
                gn = abs(de8[-1]-de21[-1]); gp = abs(de8[-2]-de21[-2])
                if gn < gp * 0.98:
                    htf_conv = True; htf_status = "CONVERGING"
                else:
                    htf_status = "OPPOSING"

    bias = "bull" if ema_bull else "bear"
    score = 0; bd = {}

    # EMA distance
    if abs(ema_dist) > 0.03:   score += 15; bd["ema"] = 15
    elif abs(ema_dist) > 0.01: score += 8;  bd["ema"] = 8
    else:                                    bd["ema"] = 0

    # MACD
    if m:
        h = m.get("macd_hist", 0)
        if bias == "bull" and h > 0:   score += 15; bd["macd_hist"] = 15
        elif bias == "bear" and h < 0: score += 15; bd["macd_hist"] = 15
        elif h != 0:                   score -= 10; bd["macd_hist"] = -10
        else:                                       bd["macd_hist"] = 0
        if m.get("macd_cross_bull") and bias == "bull":   score += 10; bd["macd_cross"] = 10
        elif m.get("macd_cross_bear") and bias == "bear": score += 10; bd["macd_cross"] = 10
        else: bd["macd_cross"] = 0
    else: bd["macd_hist"] = 0; bd["macd_cross"] = 0

    # WaveTrend
    if wt:
        if bias == "bull" and wt.get("wt_oversold"):      score += 15; bd["wt"] = 15
        elif bias == "bear" and wt.get("wt_overbought"):   score += 15; bd["wt"] = 15
        elif bias == "bull" and wt.get("wt_overbought"):   score -= 10; bd["wt"] = -10
        elif bias == "bear" and wt.get("wt_oversold"):     score -= 10; bd["wt"] = -10
        elif bias == "bull" and wt.get("wt_cross_bull"):   score += 10; bd["wt"] = 10
        elif bias == "bear" and wt.get("wt_cross_bear"):   score += 10; bd["wt"] = 10
        else:                                                            bd["wt"] = 0
    else: bd["wt"] = 0

    # VWAP
    if vwap_val:
        if bias == "bull" and spot > vwap_val:   score += 10; bd["vwap"] = 10
        elif bias == "bear" and spot < vwap_val: score += 10; bd["vwap"] = 10
        elif bias == "bull" and spot < vwap_val: score -= 5;  bd["vwap"] = -5
        elif bias == "bear" and spot > vwap_val: score -= 5;  bd["vwap"] = -5
    else: bd["vwap"] = 0

    # HTF — v6.1: TRANSITION-specific CONVERGING scoring
    if htf_conf:
        score += 15; bd["htf"] = 15
    elif htf_conv and regime == "TRANSITION":
        score += 12; bd["htf"] = 12  # CONVERGING is premium TRANSITION signal
    elif daily_bull_v is not False:
        if (bias == "bull" and daily_bull_v) or (bias == "bear" and not daily_bull_v):
            score += 10; bd["htf"] = 10
        else:
            score -= 10; bd["htf"] = -10
    else: bd["htf"] = 0

    # Volume
    if vol_ratio > 1.5:   score += 10; bd["volume"] = 10
    elif vol_ratio > 1.0: score += 5;  bd["volume"] = 5
    else:                              bd["volume"] = 0

    # RSI — v6.1: TRANSITION bull RSI window is 50-75
    if rsi_val:
        if regime == "TRANSITION" and bias == "bull":
            if 50 < rsi_val < 75:    score += 5; bd["rsi"] = 5
            elif rsi_val < 45:       score -= 5; bd["rsi"] = -5
            else:                                 bd["rsi"] = 0
        elif bias == "bull" and 40 < rsi_val < 65:   score += 5; bd["rsi"] = 5
        elif bias == "bear" and 35 < rsi_val < 60:   score += 5; bd["rsi"] = 5
        else:                                                      bd["rsi"] = 0
    else: bd["rsi"] = 0

    # (Flow boost would go here in production — omitted in backtest since
    #  historical flow data isn't available in 5min bar replay)
    bd["flow"] = 0

    if score < MIN_SIGNAL_SCORE: return None

    return {
        "bias": bias, "tier": "1" if score >= SIGNAL_TIER_1_SCORE else "2",
        "score": score, "score_bd": json.dumps(bd), "data_quality": dq,
        "bar_count": bc, "close": spot, "ema_dist_pct": round(ema_dist, 3),
        "macd_hist": m.get("macd_hist", 0), "wt2": wt.get("wt2", 0),
        "rsi": rsi_val, "vwap": vwap_val,
        "above_vwap": spot > vwap_val if vwap_val else None,
        "htf_status": htf_status, "htf_confirmed": htf_conf,
        "htf_converging": htf_conv, "daily_bull": daily_bull_v,
        "volume_ratio": round(vol_ratio, 2),
    }


def run_backtest(ticker, intraday, daily_bars, regime_bars=None):
    """Run active scanner backtest with regime-aware filtering."""
    daily_by_date = {}
    for b in daily_bars:
        daily_by_date.setdefault(b["date"], []).append(b)
    daily_close_by_date = {d: bs[-1]["c"] for d, bs in daily_by_date.items()}
    sorted_dates = sorted(daily_close_by_date.keys())

    # Pre-compute regime for each trading day
    spy_bars = regime_bars.get("SPY", []) if regime_bars else []
    qqq_bars = regime_bars.get("QQQ", []) if regime_bars else []
    iwm_bars = regime_bars.get("IWM", []) if regime_bars else []
    vix_bars = regime_bars.get("VIX", []) if regime_bars else []

    regime_cache = {}
    for d in sorted_dates:
        if spy_bars and qqq_bars and iwm_bars:
            _, core, v1, _ = compute_regime_for_date(spy_bars, qqq_bars, iwm_bars, vix_bars, d)
            regime_cache[d] = v1
        else:
            regime_cache[d] = "BEAR"  # default if no regime data

    # Group intraday bars by date
    bars_by_date = {}
    for b in intraday:
        bars_by_date.setdefault(b["date"], []).append(b)

    trades = []; last_sig = {}
    for trade_date in sorted(bars_by_date.keys()):
        day_bars = bars_by_date[trade_date]
        regime = regime_cache.get(trade_date, "BEAR")

        # Build daily close array up to this date
        dc = [daily_close_by_date[d] for d in sorted_dates if d <= trade_date]

        for i in range(DEDUP_BARS, len(day_bars)):
            window = day_bars[max(0, i-79):i+1]
            sig = detect_signal(window, dc[-30:], regime=regime)
            if sig is None: continue

            # Dedup: same bias within DEDUP_BARS
            key = (ticker, sig["bias"])
            if key in last_sig and i - last_sig[key] < DEDUP_BARS: continue
            last_sig[key] = i

            # ── REGIME-AWARE TICKER RULES FILTER ──
            valid, reason = is_signal_valid_for_regime(
                ticker, sig["bias"], sig["score"], sig["htf_status"], regime
            )
            rule = get_ticker_rule(ticker, regime)
            max_hold_days = rule.get("max_hold", 5)

            phase = time_phase(day_bars[i]["time_ct"])
            entry_price = sig["close"]

            # Compute exits at various horizons
            exits = exit_dates_for(trade_date, sorted_dates)
            t = {
                "ticker": ticker, "entry_date": trade_date,
                "entry_time_ct": day_bars[i]["time_ct"], "phase": phase,
                "bias": sig["bias"], "tier": sig["tier"], "score": sig["score"],
                "htf_status": sig["htf_status"], "htf_confirmed": sig["htf_confirmed"],
                "data_quality": sig["data_quality"], "entry_price": entry_price,
                "ema_dist_pct": sig["ema_dist_pct"], "macd_hist": sig["macd_hist"],
                "wt2": sig["wt2"], "rsi": sig["rsi"],
                "above_vwap": sig["above_vwap"], "volume_ratio": sig["volume_ratio"],
                "score_bd": sig["score_bd"],
                "regime": regime, "regime_valid": valid, "regime_reason": reason,
                "max_hold": max_hold_days,
            }

            # MFE/MAE on entry day
            remaining = day_bars[i:]
            if remaining:
                mfe = max(b["h"] for b in remaining) - entry_price if sig["bias"] == "bull" else entry_price - min(b["l"] for b in remaining)
                mae = entry_price - min(b["l"] for b in remaining) if sig["bias"] == "bull" else max(b["h"] for b in remaining) - entry_price
                t["mfe_eod_pts"] = round(mfe, 3)
                t["mae_eod_pts"] = round(-mae, 3)
            else:
                t["mfe_eod_pts"] = None; t["mae_eod_pts"] = None

            # P&L at each exit horizon
            for label, exit_date in exits.items():
                if exit_date and exit_date in daily_close_by_date:
                    exit_p = daily_close_by_date[exit_date]
                    if sig["bias"] == "bull":
                        pnl = exit_p - entry_price
                    else:
                        pnl = entry_price - exit_p
                    pnl_pct = (pnl / entry_price) * 100
                    t[f"exit_date_{label}"] = exit_date
                    t[f"exit_price_{label}"] = exit_p
                    t[f"pnl_{label}"] = round(pnl, 3)
                    t[f"pnl_pct_{label}"] = round(pnl_pct, 3)
                    t[f"win_{label}"] = pnl > 0
                else:
                    for k in [f"exit_date_{label}", f"exit_price_{label}", f"pnl_{label}", f"pnl_pct_{label}", f"win_{label}"]:
                        t[k] = None

            trades.append(t)

    return trades


def write_summary(ticker, trades, out_dir):
    if not trades: print(f"  No signals for {ticker}"); return
    lines = []; SEP = "=" * 62

    # Header
    lines.append(f"\n{SEP}")
    lines.append(f"  ACTIVE SCANNER BACKTEST v7 (REGIME-AWARE) — {ticker}")
    lines.append(f"{SEP}")
    lines.append(f"  Total signals:          {len(trades)}")
    lines.append(f"  Date range:             {trades[0]['entry_date']} → {trades[-1]['entry_date']}")
    lines.append(f"  Unique days:            {len(set(t['entry_date'] for t in trades))}")

    # Regime breakdown
    lines.append(f"\n{SEP}\n  REGIME BREAKDOWN\n{SEP}")
    for reg in ["BULL", "TRANSITION", "BEAR"]:
        sub = [t for t in trades if t["regime"] == reg]
        valid = [t for t in sub if t["regime_valid"]]
        lines.append(f"  {reg}: {len(sub)} signals, {len(valid)} regime-valid ({len(valid)/max(1,len(sub))*100:.0f}%)")

    # Win rates — ALL signals (unfiltered)
    lines.append(f"\n{SEP}\n  ALL SIGNALS (unfiltered)\n{SEP}")
    for label in EXIT_DAYS:
        s = compute_win_stats(trades, label)
        if s["n"]: lines.append(f"  {label:>4}: {s['n']:>4} trades  {s['wr']:>5.1f}% win  {s['avg_pct']:>+7.3f}%  PF {s['pf']}")

    # Win rates — REGIME-VALID only
    valid_trades = [t for t in trades if t["regime_valid"]]
    lines.append(f"\n{SEP}\n  REGIME-VALID SIGNALS ONLY ({len(valid_trades)} trades)\n{SEP}")
    for label in EXIT_DAYS:
        s = compute_win_stats(valid_trades, label)
        if s["n"]: lines.append(f"  {label:>4}: {s['n']:>4} trades  {s['wr']:>5.1f}% win  {s['avg_pct']:>+7.3f}%  PF {s['pf']}")

    # By bias (regime-valid)
    for bias in ["bull", "bear"]:
        sub = [t for t in valid_trades if t["bias"] == bias]
        if sub:
            lines.append(f"\n  {bias.upper()} (regime-valid) — {len(sub)} signals")
            for label in ["eod", "1d", "3d", "5d"]:
                s = compute_win_stats(sub, label)
                if s["n"]: lines.append(f"    {label}: {s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    # By HTF (regime-valid)
    lines.append(f"\n{SEP}\n  BY HTF STATUS (regime-valid)\n{SEP}")
    for status in ["CONFIRMED", "CONVERGING", "OPPOSING", "UNKNOWN"]:
        sub = [t for t in valid_trades if t["htf_status"] == status]
        if sub:
            lines.append(f"\n  {status} — {len(sub)} ({len(sub)/max(1,len(valid_trades))*100:.0f}%)")
            for h_ in ["eod", "1d", "3d"]:
                s = compute_win_stats(sub, h_)
                if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    # By phase
    lines.append(f"\n{SEP}\n  BY PHASE (regime-valid)\n{SEP}")
    for phase in ["MORNING", "MIDDAY", "AFTERNOON"]:
        sub = [t for t in valid_trades if t["phase"] == phase]
        if sub:
            lines.append(f"\n  {phase} — {len(sub)}")
            for h_ in ["eod", "1d", "3d"]:
                s = compute_win_stats(sub, h_)
                if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    # By regime
    lines.append(f"\n{SEP}\n  BY REGIME (regime-valid)\n{SEP}")
    for reg in ["BULL", "TRANSITION", "BEAR"]:
        sub = [t for t in valid_trades if t["regime"] == reg]
        if sub:
            lines.append(f"\n  {reg} — {len(sub)}")
            for h_ in ["eod", "1d", "3d", "5d"]:
                s = compute_win_stats(sub, h_)
                if s["n"]: lines.append(f"    {h_}: {s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    # Rejection reasons
    rejected = [t for t in trades if not t["regime_valid"]]
    if rejected:
        lines.append(f"\n{SEP}\n  REGIME REJECTIONS ({len(rejected)} signals)\n{SEP}")
        reasons = {}
        for t in rejected:
            r = t["regime_reason"]
            reasons[r] = reasons.get(r, 0) + 1
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  {r}: {c}")

    lines.append(f"\n{SEP}\n")
    txt = "\n".join(lines)
    p = out_dir / f"active_v7_{ticker}.txt"
    p.write_text(txt)
    print(f"\n  Summary → {p}")
    print(txt)


def main():
    ap = argparse.ArgumentParser(description="Active Scanner Backtest v7 (Regime-Aware)")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--all", action="store_true", help="Run all watchlist tickers")
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to", dest="to_date", default=None)
    ap.add_argument("--days", type=int, default=270, help="Lookback days (default 270)")
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    tickers = ALL_TICKERS if args.all else ([args.ticker.upper()] if args.ticker else ["SPY"])
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    to_date = args.to_date or today
    from_date = args.from_date or (date.today() - timedelta(days=args.days)).isoformat()
    daily_from = (datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=100)).strftime("%Y-%m-%d")

    # Download regime data (SPY, QQQ, IWM, VIX)
    print(f"\n{'='*62}")
    print(f"  Downloading regime reference data…")
    print(f"{'='*62}\n")
    regime_bars = {}
    for t in ["SPY", "QQQ", "IWM"]:
        regime_bars[t] = download_daily(t, daily_from, to_date)
    regime_bars["VIX"] = download_vix(daily_from, to_date)

    for ticker in tickers:
        print(f"\n{'='*62}")
        print(f"  ACTIVE BACKTEST v7 — {ticker}")
        print(f"  Period: {from_date} → {to_date} | Regime-aware")
        print(f"{'='*62}\n")

        intraday = download_5min(ticker, from_date, to_date)
        if ticker in ("SPY", "QQQ", "IWM"):
            daily = regime_bars.get(ticker, [])
        else:
            daily = download_daily(ticker, daily_from, to_date)

        if not intraday: print(f"  ERROR: no 5-min bars for {ticker}"); continue
        if not daily:    print(f"  ERROR: no daily bars for {ticker}"); continue

        intraday = [b for b in intraday if is_market_bar(b["time_ct"])]
        print(f"  Market-hours bars: {len(intraday)} | Daily: {len(daily)}")

        trades = run_backtest(ticker, intraday, daily, regime_bars)
        print(f"  Signals fired: {len(trades)} | Regime-valid: {sum(1 for t in trades if t['regime_valid'])}")

        if trades:
            write_csv(out_dir / f"active_v7_trades_{ticker}.csv", trades)
            write_summary(ticker, trades, out_dir)


if __name__ == "__main__":
    main()
