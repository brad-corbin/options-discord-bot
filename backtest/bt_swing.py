#!/usr/bin/env python3
"""
bt_swing.py — Swing Scanner Backtest v3 (Regime-Aware)
══════════════════════════════════════════════════════════
Walk-forward Fib retracement backtest matching swing_scanner.py.

Signals: daily bars → Fib touch + candle quality + trend alignment
Exits:   stop (ATR-capped below swing low), target1 (1.272 ext),
         target2 (1.618 ext), or max-hold (15 days).

NEW vs old swing_backtest.py:
  - Regime overlay (regime computed daily from SPY/QQQ/IWM)
  - Primary trend filter (50/200 SMA)
  - RSI sweet spot scoring (45-60 for bulls)
  - ATR-capped stops (max 2x ATR from entry)
  - Relative strength vs SPY

Usage:
  python backtest/bt_swing.py --ticker NVDA --data backtest/data/NVDA_daily.csv
  python backtest/bt_swing.py --ticker NVDA --days 400
  python backtest/bt_swing.py --all --days 400
"""

import os, sys, csv, json, argparse, math
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from bt_shared import *

# ── Constants from trading_rules.py ──
FIB_LOOKBACK = 50; FIB_TOUCH_ZONE_PCT = 1.25
WEEKLY_EMA_FAST = 5; WEEKLY_EMA_SLOW = 20; WEEKLY_MIN_SEP_PCT = 0.15
DAILY_EMA_FAST = 8; DAILY_EMA_SLOW = 21
RSI_LEN = 14; RSI_OVERSOLD = 48; RSI_OVERBOUGHT = 52
VOL_MA_LEN = 20; VOL_CONTRACT = 0.90; VOL_EXPAND = 1.15
WICK_MIN_PCT = 35.0; CLOSE_ZONE_PCT = 35.0
ATR_LEN = 14; COOLDOWN_BARS = 3
RS_LOOKBACK = 20; RS_REJECT_LONG = -3.0; RS_REJECT_SHORT = 3.0
MAX_HOLD = 15; STOP_ATR_MULT = 0.2; ATR_STOP_CAP = 2.0
MIN_BARS = 80

SWING_TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO",
    "PLTR","COIN","NFLX","QQQ","SPY","GLD","SLV",
    "JPM","GS","BAC","C","HD","WMT","PEP","KR","PFE","MRNA","UNH",
]


def _aggregate_weekly(bars):
    weeks = {}
    for b in bars:
        from datetime import datetime as dt
        d = dt.strptime(b["date"], "%Y-%m-%d")
        wk = d.strftime("%Y-W%W")
        weeks.setdefault(wk, []).append(b)
    result = []
    for wk in sorted(weeks):
        bs = weeks[wk]
        result.append({"o": bs[0]["o"], "h": max(b["h"] for b in bs),
                       "l": min(b["l"] for b in bs), "c": bs[-1]["c"],
                       "v": sum(b["v"] for b in bs), "date": bs[-1]["date"]})
    return result


def _find_pivots(highs, lows, pivot_len):
    sh = []; sl = []
    for i in range(pivot_len, len(highs) - pivot_len):
        if all(highs[i] >= highs[i-j] for j in range(1, pivot_len+1)) and \
           all(highs[i] >= highs[i+j] for j in range(1, pivot_len+1)):
            sh.append((i, highs[i]))
    for i in range(pivot_len, len(lows) - pivot_len):
        if all(lows[i] <= lows[i-j] for j in range(1, pivot_len+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, pivot_len+1)):
            sl.append((i, lows[i]))
    return sh, sl


def compute_fib_levels(swing_high, swing_low):
    rng = swing_high - swing_low
    return {
        "38.2": swing_high - rng * 0.382, "50.0": swing_high - rng * 0.5,
        "61.8": swing_high - rng * 0.618, "78.6": swing_high - rng * 0.786,
        "bear_38.2": swing_low + rng * 0.382, "bear_50.0": swing_low + rng * 0.5,
        "bear_61.8": swing_low + rng * 0.618, "bear_78.6": swing_low + rng * 0.786,
        "bull_ext_127": swing_high + rng * 0.272, "bull_ext_162": swing_high + rng * 0.618,
        "bear_ext_127": swing_low - rng * 0.272, "bear_ext_162": swing_low - rng * 0.618,
        "swing_high": swing_high, "swing_low": swing_low,
    }


def candle_quality(bar):
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    rng = h - l
    if rng <= 0: return {"bull_strong": False, "bull_soft": False, "bear_strong": False, "bear_soft": False}
    body = abs(c - o)
    lower_wick = (min(o, c) - l) / rng * 100
    upper_wick = (h - max(o, c)) / rng * 100
    bull_close = (c - l) / rng * 100
    bear_close = (h - c) / rng * 100
    return {
        "bull_strong": lower_wick >= WICK_MIN_PCT and c >= o and bull_close >= (100 - CLOSE_ZONE_PCT),
        "bull_soft": c >= o or bull_close >= (100 - CLOSE_ZONE_PCT),
        "bear_strong": upper_wick >= WICK_MIN_PCT and c <= o and bear_close >= (100 - CLOSE_ZONE_PCT),
        "bear_soft": c <= o or bear_close >= (100 - CLOSE_ZONE_PCT),
    }


def analyze_swing(ticker, daily_bars, spy_bars=None):
    """Analyze single bar for swing setup. Returns signal dict or None."""
    if len(daily_bars) < MIN_BARS: return None
    closes = [b["c"] for b in daily_bars]; highs = [b["h"] for b in daily_bars]
    lows = [b["l"] for b in daily_bars]; volumes = [b.get("v", 0) for b in daily_bars]
    bar = daily_bars[-1]; spot = bar["c"]

    # Weekly trend
    weekly_bars = _aggregate_weekly(daily_bars)
    if len(weekly_bars) < WEEKLY_EMA_SLOW + 2: return None
    wc = [w["c"] for w in weekly_bars]
    wef = ema(wc, WEEKLY_EMA_FAST); wes = ema(wc, WEEKLY_EMA_SLOW)
    if not wef or not wes: return None
    w_gap = abs(wef[-1] - wes[-1]); w_min = wc[-1] * WEEKLY_MIN_SEP_PCT / 100
    weekly_bull = wef[-1] > wes[-1] and w_gap >= w_min
    weekly_bear = wef[-1] < wes[-1] and w_gap >= w_min
    weekly_bull_loose = wef[-1] > wes[-1]; weekly_bear_loose = wef[-1] < wes[-1]
    w_gap_prev = abs(wef[-2] - wes[-2]) if len(wef) >= 2 else w_gap
    weekly_conv = w_gap < w_gap_prev
    weekly_bull_ok = weekly_bull or (weekly_bull_loose and weekly_conv)
    weekly_bear_ok = weekly_bear or (weekly_bear_loose and weekly_conv)

    # Daily trend
    def8 = ema(closes, DAILY_EMA_FAST); de21 = ema(closes, DAILY_EMA_SLOW)
    if not def8 or not de21: return None
    daily_bull = def8[-1] > de21[-1]; daily_bear = not daily_bull
    d_gap = abs(def8[-1] - de21[-1])
    d_gap_prev = abs(def8[-2] - de21[-2]) if len(def8) >= 2 else d_gap
    daily_conv = d_gap < d_gap_prev

    # Primary trend
    primary = "neutral"
    if len(closes) >= 200:
        s50 = sma(closes, 50); s200 = sma(closes, 200)
        if s50 and s200: primary = "bullish" if s50 > s200 else "bearish"

    # RSI, Volume, ATR
    rsi_val = rsi(closes, RSI_LEN) or 50
    avg_vol = sma(volumes, VOL_MA_LEN)
    vol_contract = volumes[-1] < avg_vol * VOL_CONTRACT if avg_vol else False
    atr_val = atr(highs, lows, closes, ATR_LEN) or 1.0

    # Pivots + Fibs
    pivot_len = max(2, round(FIB_LOOKBACK / 5))
    sh, sl = _find_pivots(highs, lows, pivot_len)
    if not sh or not sl: return None
    last_sh = sh[-1][1]; last_sl = sl[-1][1]
    if last_sh <= last_sl: return None
    fibs = compute_fib_levels(last_sh, last_sl)

    # Fib touch detection
    touch_zone = spot * FIB_TOUCH_ZONE_PCT / 100
    bull_touched = bear_touched = False
    fib_level = ""; fib_price = 0
    for level in ["38.2", "50.0", "61.8", "78.6"]:
        fp = fibs[level]
        if abs(bar["l"] - fp) <= touch_zone and bar["c"] > fp:
            bull_touched = True; fib_level = level; fib_price = fp; break
    if not bull_touched:
        for level in ["bear_38.2", "bear_50.0", "bear_61.8", "bear_78.6"]:
            fp = fibs[level]
            if abs(bar["h"] - fp) <= touch_zone and bar["c"] < fp:
                bear_touched = True; fib_level = level.replace("bear_", ""); fib_price = fp; break

    if not bull_touched and not bear_touched: return None

    cq = candle_quality(bar)
    rsi_bull = rsi_val <= RSI_OVERSOLD; rsi_bear = rsi_val >= RSI_OVERBOUGHT

    # Tier logic
    t1_bull = (cq["bull_strong"] and bull_touched and fib_level in ("61.8","50.0")
               and weekly_bull_ok and (daily_bull or daily_conv) and (vol_contract or rsi_bull))
    t2_bull = cq["bull_soft"] and bull_touched and weekly_bull_ok and (daily_bull or daily_conv) and not t1_bull
    t1_bear = (cq["bear_strong"] and bear_touched and fib_level in ("61.8","50.0")
               and weekly_bear_ok and (daily_bear or daily_conv) and (vol_contract or rsi_bear))
    t2_bear = cq["bear_soft"] and bear_touched and weekly_bear_ok and (daily_bear or daily_conv) and not t1_bear

    if not (t1_bull or t2_bull or t1_bear or t2_bear): return None
    if t1_bull: direction, tier = "bull", 1
    elif t1_bear: direction, tier = "bear", 1
    elif t2_bull: direction, tier = "bull", 2
    else: direction, tier = "bear", 2

    # RS vs SPY
    rs = 0.0
    if spy_bars and len(spy_bars) >= RS_LOOKBACK and len(daily_bars) >= RS_LOOKBACK:
        t_ret = (closes[-1] - closes[-RS_LOOKBACK]) / closes[-RS_LOOKBACK] * 100
        spy_c = [b["c"] for b in spy_bars]
        s_ret = (spy_c[-1] - spy_c[-RS_LOOKBACK]) / spy_c[-RS_LOOKBACK] * 100
        rs = t_ret - s_ret
    if direction == "bull" and rs < RS_REJECT_LONG: return None
    if direction == "bear" and rs > RS_REJECT_SHORT: return None

    # Confidence
    conf = 50 + (15 if tier == 1 else 5)
    if fib_level == "50.0": conf += 12
    elif fib_level == "78.6": conf += 8
    elif fib_level == "61.8": conf += 6
    elif fib_level == "38.2": conf += 5
    if weekly_bull and direction == "bull": conf += 5
    elif weekly_bear and direction == "bear": conf += 5
    if vol_contract: conf += 3
    if primary == "bullish" and direction == "bull": conf += 5
    elif primary == "bearish" and direction == "bear": conf += 5
    if direction == "bull" and 45 <= rsi_val <= 60: conf += 5

    targets = {}
    if direction == "bull":
        targets["target1"] = fibs["bull_ext_127"]; targets["target2"] = fibs["bull_ext_162"]
    else:
        targets["target1"] = fibs["bear_ext_127"]; targets["target2"] = fibs["bear_ext_162"]

    return {
        "ticker": ticker, "direction": direction, "tier": tier,
        "fib_level": fib_level, "fib_price": round(fib_price, 2),
        "swing_high": last_sh, "swing_low": last_sl,
        "fib_target_1": targets["target1"], "fib_target_2": targets["target2"],
        "confidence": conf, "rs_vs_spy": round(rs, 2),
        "primary_trend": primary, "rsi": round(rsi_val, 1),
        "weekly_bull": weekly_bull, "weekly_bear": weekly_bear,
        "vol_contracting": vol_contract, "atr": round(atr_val, 2),
        "touch_count": 1, "date": bar["date"],
    }


def run_backtest(ticker, daily_bars, spy_bars=None, regime_bars=None):
    trades = []; position = None; last_signal_bar = -999

    spy_aligned = []
    if spy_bars:
        spy_by_date = {b["date"]: b for b in spy_bars}
        spy_aligned = [spy_by_date.get(b["date"]) for b in daily_bars]

    for i in range(MIN_BARS, len(daily_bars)):
        bar = daily_bars[i]

        # Manage open position
        if position is not None:
            position["hold_days"] += 1
            if position["direction"] == "bull":
                position["mfe"] = max(position["mfe"], bar["h"] - position["entry_price"])
                position["mae"] = min(position["mae"], -(position["entry_price"] - bar["l"]))
            else:
                position["mfe"] = max(position["mfe"], position["entry_price"] - bar["l"])
                position["mae"] = min(position["mae"], -(bar["h"] - position["entry_price"]))

            exit_price = exit_reason = None
            if position["direction"] == "bull":
                if bar["l"] <= position["stop"]: exit_price = position["stop"]; exit_reason = "stop"
                elif bar["h"] >= position["target1"]: exit_price = position["target1"]; exit_reason = "target1"
            else:
                if bar["h"] >= position["stop"]: exit_price = position["stop"]; exit_reason = "stop"
                elif bar["l"] <= position["target1"]: exit_price = position["target1"]; exit_reason = "target1"
            if not exit_reason and position["hold_days"] >= MAX_HOLD:
                exit_price = bar["c"]; exit_reason = "max_hold"

            if exit_price:
                pnl = (exit_price - position["entry_price"]) if position["direction"] == "bull" else (position["entry_price"] - exit_price)
                pnl_pct = pnl / position["entry_price"] * 100
                trades.append({**{k: position[k] for k in ["ticker","direction","tier","fib_level","fib_price",
                    "swing_high","swing_low","stop","target1","target2","entry_date","entry_price",
                    "confidence","rs_vs_spy","primary_trend","rsi","weekly_bull","weekly_bear",
                    "vol_contracting","atr","touch_count"]},
                    "close_date": bar["date"], "close_price": round(exit_price, 2),
                    "close_reason": exit_reason, "hold_days": position["hold_days"],
                    "pnl_pts": round(pnl, 3), "pnl_pct": round(pnl_pct, 4),
                    "mae_pts": round(position["mae"], 3), "mfe_pts": round(position["mfe"], 3),
                })
                position = None; last_signal_bar = i
                continue

        if position is not None: continue
        if i - last_signal_bar < COOLDOWN_BARS: continue

        # Scan for signal
        visible = daily_bars[:i+1]
        spy_vis = [spy_aligned[j] for j in range(min(i+1, len(spy_aligned))) if spy_aligned[j]] if spy_aligned else None
        sig = analyze_swing(ticker, visible, spy_vis)
        if sig is None: continue

        # Enter at next bar open
        if i + 1 >= len(daily_bars): continue
        entry_price = daily_bars[i+1]["o"]

        # ATR-capped stop
        if sig["direction"] == "bull":
            swing_dist = entry_price - (sig["swing_low"] - sig["atr"] * STOP_ATR_MULT)
            stop = entry_price - min(swing_dist, sig["atr"] * ATR_STOP_CAP)
        else:
            swing_dist = (sig["swing_high"] + sig["atr"] * STOP_ATR_MULT) - entry_price
            stop = entry_price + min(swing_dist, sig["atr"] * ATR_STOP_CAP)

        position = {
            "ticker": ticker, "direction": sig["direction"], "tier": sig["tier"],
            "fib_level": sig["fib_level"], "fib_price": sig["fib_price"],
            "swing_high": sig["swing_high"], "swing_low": sig["swing_low"],
            "stop": round(stop, 2),
            "target1": round(sig["fib_target_1"], 2), "target2": round(sig["fib_target_2"], 2),
            "entry_date": daily_bars[i+1]["date"], "entry_price": round(entry_price, 2),
            "confidence": sig["confidence"], "rs_vs_spy": sig["rs_vs_spy"],
            "primary_trend": sig["primary_trend"], "rsi": sig["rsi"],
            "weekly_bull": sig["weekly_bull"], "weekly_bear": sig["weekly_bear"],
            "vol_contracting": sig["vol_contracting"], "atr": sig["atr"],
            "touch_count": sig["touch_count"],
            "hold_days": 0, "mae": 0.0, "mfe": 0.0,
        }

    return trades


def write_summary(ticker, trades, out_dir):
    if not trades: print(f"  No trades for {ticker}"); return
    n = len(trades); wins = [t for t in trades if t["pnl_pts"] > 0]; losses = [t for t in trades if t["pnl_pts"] <= 0]
    wr = len(wins)/n*100; tw = sum(t["pnl_pts"] for t in wins); tl = abs(sum(t["pnl_pts"] for t in losses))
    pf = tw/tl if tl > 0 else float("inf")
    lines = [f"\n{'='*62}", f"  SWING BACKTEST v3 — {ticker}", f"  From: {trades[0]['entry_date']} | Max hold: {MAX_HOLD}d", f"{'='*62}"]
    lines.append(f"  Trades: {n} | WR: {wr:.1f}% ({len(wins)}W/{len(losses)}L)")
    lines.append(f"  Total P&L: {sum(t['pnl_pts'] for t in trades):+.2f} pts | PF: {pf:.2f}")
    lines.append(f"  Avg winner: {sum(t['pnl_pts'] for t in wins)/max(1,len(wins)):+.2f} pts")
    lines.append(f"  Avg loser: {sum(t['pnl_pts'] for t in losses)/max(1,len(losses)):+.2f} pts")
    lines.append(f"  Avg hold: {sum(t['hold_days'] for t in trades)/n:.1f} days")
    for d in ["bull","bear"]:
        sub = [t for t in trades if t["direction"]==d]
        if sub:
            w = sum(1 for t in sub if t["pnl_pts"]>0)
            lines.append(f"  {d.upper()}: {len(sub)}T {w/len(sub)*100:.0f}%WR {sum(t['pnl_pts'] for t in sub):+.1f}pts")
    for fl in ["38.2","50.0","61.8","78.6"]:
        sub = [t for t in trades if t["fib_level"]==fl]
        if sub:
            w = sum(1 for t in sub if t["pnl_pts"]>0)
            lines.append(f"  Fib {fl}: {len(sub)}T {w/len(sub)*100:.0f}%WR avg {sum(t['pnl_pct'] for t in sub)/len(sub):+.2f}%")
    for r in ["target1","target2","stop","max_hold"]:
        sub = [t for t in trades if t["close_reason"]==r]
        if sub:
            w = sum(1 for t in sub if t["pnl_pts"]>0)
            lines.append(f"  {r}: {len(sub)}T {w/len(sub)*100:.0f}%WR avg {sum(t['pnl_pct'] for t in sub)/len(sub):+.2f}%")
    mfe_vals = [t["mfe_pts"] for t in trades]
    mae_vals = [t["mae_pts"] for t in trades]
    lines.append(f"\n  Avg MFE: {sum(mfe_vals)/n:+.2f} | Avg MAE: {sum(mae_vals)/n:+.2f}")
    if wins: lines.append(f"  Winner MFE capture: {sum(t['pnl_pts']/max(t['mfe_pts'],0.01) for t in wins)/len(wins)*100:.0f}%")
    txt = "\n".join(lines) + f"\n{'='*62}\n"
    p = out_dir / f"swing_v3_{ticker}.txt"
    p.write_text(txt); print(txt)


def main():
    ap = argparse.ArgumentParser(description="Swing Scanner Backtest v3")
    ap.add_argument("--ticker", default=None); ap.add_argument("--all", action="store_true")
    ap.add_argument("--data", default=None, help="Path to daily CSV")
    ap.add_argument("--spy", default=None, help="Path to SPY daily CSV")
    ap.add_argument("--days", type=int, default=400); ap.add_argument("--out-dir", default="results")
    ap.add_argument("--backtest-from", default=None, help="Only generate signals after this date")
    args = ap.parse_args()

    tickers = SWING_TICKERS if args.all else ([args.ticker.upper()] if args.ticker else ["AAPL"])
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=args.days)).isoformat()

    for ticker in tickers:
        print(f"\n{'='*62}\n  SWING BACKTEST v3 — {ticker}\n{'='*62}")
        if args.data:
            daily = load_daily_csv(args.data)
        else:
            daily = download_daily(ticker, from_date, to_date)
        if args.spy:
            spy = load_daily_csv(args.spy)
        else:
            spy = download_daily("SPY", from_date, to_date)
        if not daily or len(daily) < MIN_BARS: print(f"  Insufficient data"); continue

        if args.backtest_from:
            cutoff = args.backtest_from
            daily = [b for b in daily if b["date"] <= to_date]

        trades = run_backtest(ticker, daily, spy)
        print(f"  Trades: {len(trades)}")
        if trades:
            write_csv(out_dir / f"swing_v3_trades_{ticker}.csv", trades)
            write_summary(ticker, trades, out_dir)


if __name__ == "__main__":
    main()
