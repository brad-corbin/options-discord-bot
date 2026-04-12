#!/usr/bin/env python3
"""
Potter Box Walk-Forward Backtest v2
Matches the bot's ACTUAL logic: position inside box for breakout through void to next box.
"""
import sys, os, json, csv
from datetime import datetime
from typing import List, Dict, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))

from potter_box import (
    detect_boxes, detect_voids, classify_maturity, _body_top, _body_bot,
    TOUCH_ZONE_PCT, MIN_BOX_BARS, DEFAULT_DURATION, _get_tier
)

MAX_HOLD_BARS = 15
COOLDOWN_BARS = 5
RISK_PER_TRADE_PCT = 2.0
INITIAL_EQUITY = 10000.0
MIN_VOID_PCT = 2.0
MIN_WAVE_TOUCHES = 3
MAX_R_CAP = 10.0
TRAIL_TO_VOID_EDGE = True

def get_wave_direction(box):
    rt = box.get("roof_touches", 0)
    ft = box.get("floor_touches", 0)
    if rt > ft and rt >= MIN_WAVE_TOUCHES:
        return "bullish"
    elif ft > rt and ft >= MIN_WAVE_TOUCHES:
        return "bearish"
    return None

class Position:
    def __init__(self, ticker, direction, entry_price, stop_price, target_price,
                 entry_bar, entry_date, box, void_target):
        self.ticker = ticker; self.direction = direction
        self.entry_price = entry_price; self.stop_price = stop_price
        self.target_price = target_price; self.entry_bar = entry_bar
        self.entry_date = entry_date; self.box = box; self.void_target = void_target
        self.bars_held = 0; self.mae = 0.0; self.mfe = 0.0
        self.original_stop = stop_price; self.trailed = False
    def risk_amount(self): return abs(self.entry_price - self.stop_price)

def run_backtest(bars, ticker):
    n = len(bars)
    if n < MIN_BOX_BARS + 30:
        return {"error": "Not enough bars", "trades": [], "equity_curve": []}
    trades = []; equity = INITIAL_EQUITY; equity_curve = []
    position = None; last_trade_bar = -999
    tier = _get_tier(ticker); avg_dur = DEFAULT_DURATION.get(tier, 18)

    for i in range(MIN_BOX_BARS + 10, n):
        visible_bars = bars[:i + 1]; bar = bars[i]; bar_date = bar.get("date", str(i))

        if position is not None:
            position.bars_held += 1
            if position.direction == "bullish":
                position.mfe = max(position.mfe, bar["h"] - position.entry_price)
                position.mae = max(position.mae, position.entry_price - bar["l"])
            else:
                position.mfe = max(position.mfe, position.entry_price - bar["l"])
                position.mae = max(position.mae, bar["h"] - position.entry_price)
            if TRAIL_TO_VOID_EDGE and not position.trailed:
                if position.direction == "bullish" and bar["c"] > position.box["roof"]:
                    position.stop_price = position.box["roof"] * (1 - TOUCH_ZONE_PCT)
                    position.trailed = True
                elif position.direction == "bearish" and bar["c"] < position.box["floor"]:
                    position.stop_price = position.box["floor"] * (1 + TOUCH_ZONE_PCT)
                    position.trailed = True
            exit_price = None; exit_reason = None
            if position.direction == "bullish":
                if bar["l"] <= position.stop_price: exit_price = position.stop_price; exit_reason = "stop"
                elif bar["h"] >= position.target_price: exit_price = position.target_price; exit_reason = "target"
            else:
                if bar["h"] >= position.stop_price: exit_price = position.stop_price; exit_reason = "stop"
                elif bar["l"] <= position.target_price: exit_price = position.target_price; exit_reason = "target"
            if exit_reason is None and position.bars_held >= MAX_HOLD_BARS:
                exit_price = bar["c"]; exit_reason = "max_hold"
            if exit_price is not None:
                if position.direction == "bullish":
                    pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100
                else:
                    pnl_pct = (position.entry_price - exit_price) / position.entry_price * 100
                risk_pct = position.risk_amount() / position.entry_price * 100
                pnl_r = pnl_pct / risk_pct if risk_pct > 0.1 else 0
                pnl_r = max(min(pnl_r, MAX_R_CAP), -MAX_R_CAP)
                dollar_pnl = equity * (RISK_PER_TRADE_PCT / 100) * pnl_r
                equity += dollar_pnl
                trades.append({
                    "ticker": ticker, "direction": position.direction,
                    "signal_type": "box_breakout",
                    "wave_direction": "roof_weakening" if position.direction == "bullish" else "floor_weakening",
                    "entry_date": position.entry_date, "exit_date": bar_date,
                    "entry_price": round(position.entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "stop_price": round(position.original_stop, 2),
                    "target_price": round(position.target_price, 2),
                    "exit_reason": exit_reason,
                    "pnl_pct": round(pnl_pct, 2), "pnl_r": round(pnl_r, 2),
                    "dollar_pnl": round(dollar_pnl, 2), "bars_held": position.bars_held,
                    "mae_pct": round(position.mae / position.entry_price * 100, 2),
                    "mfe_pct": round(position.mfe / position.entry_price * 100, 2),
                    "trailed": position.trailed,
                    "box_roof": position.box["roof"], "box_floor": position.box["floor"],
                    "box_touches": f"{position.box.get('roof_touches',0)}R/{position.box.get('floor_touches',0)}F",
                    "wave_label": position.box.get("wave_label", ""),
                    "void_target": position.void_target,
                    "entry_bar_idx": position.entry_bar, "exit_bar_idx": i,
                    "equity_after": round(equity, 2),
                })
                last_trade_bar = i; position = None

        equity_curve.append({"bar_idx": i, "date": bar_date, "close": bar["c"],
                             "equity": round(equity, 2), "in_trade": position is not None})
        if position is not None: continue
        if i - last_trade_bar < COOLDOWN_BARS: continue

        all_boxes = detect_boxes(visible_bars, ticker)
        if not all_boxes: continue
        active = [b for b in all_boxes if b["active"]]
        if not active: continue
        box = active[-1]
        mat = classify_maturity(box, avg_dur)
        if mat["maturity"] == "early": continue
        spot = bar["c"]
        if spot > box["roof"] * (1 + TOUCH_ZONE_PCT) or spot < box["floor"] * (1 - TOUCH_ZONE_PCT):
            continue
        direction = get_wave_direction(box)
        if direction is None: continue
        voids = detect_voids(visible_bars, all_boxes, ticker)
        va = vb = None
        for v in voids:
            if v["position"] == "above" and (va is None or v["height_pct"] > va["height_pct"]): va = v
            if v["position"] == "below" and (vb is None or v["height_pct"] > vb["height_pct"]): vb = v
        if direction == "bullish" and (va is None or va["height_pct"] < MIN_VOID_PCT): continue
        if direction == "bearish" and (vb is None or vb["height_pct"] < MIN_VOID_PCT): continue
        other_boxes = [b for b in all_boxes if b is not box]
        box_above = box_below = None
        for ob in other_boxes:
            if ob["floor"] > box["roof"] * 0.98:
                if box_above is None or ob["floor"] < box_above["floor"]: box_above = ob
            if ob["roof"] < box["floor"] * 1.02:
                if box_below is None or ob["roof"] > box_below["roof"]: box_below = ob
        entry_price = spot
        if direction == "bullish":
            stop_price = box["floor"] * (1 - TOUCH_ZONE_PCT)
            target_price = box_above["floor"] if box_above else (va["high"] if va else box["roof"] * 1.05)
            void_info = f"${va['low']:.2f}-${va['high']:.2f} ({va['height_pct']:.1f}%)" if va else "none"
        else:
            stop_price = box["roof"] * (1 + TOUCH_ZONE_PCT)
            target_price = box_below["roof"] if box_below else (vb["low"] if vb else box["floor"] * 0.95)
            void_info = f"${vb['low']:.2f}-${vb['high']:.2f} ({vb['height_pct']:.1f}%)" if vb else "none"
        if direction == "bullish" and target_price <= entry_price: continue
        if direction == "bearish" and target_price >= entry_price: continue
        risk = abs(entry_price - stop_price)
        if risk / entry_price < 0.002: continue
        position = Position(ticker=ticker, direction=direction, entry_price=entry_price,
                           stop_price=stop_price, target_price=target_price, entry_bar=i,
                           entry_date=bar_date, box=box, void_target=void_info)

    if position is not None:
        bar = bars[-1]
        pnl_pct = ((bar["c"] - position.entry_price) / position.entry_price * 100
                   if position.direction == "bullish"
                   else (position.entry_price - bar["c"]) / position.entry_price * 100)
        risk_pct = position.risk_amount() / position.entry_price * 100
        pnl_r = max(min(pnl_pct / risk_pct if risk_pct > 0.1 else 0, MAX_R_CAP), -MAX_R_CAP)
        dollar_pnl = equity * (RISK_PER_TRADE_PCT / 100) * pnl_r; equity += dollar_pnl
        trades.append({"ticker": ticker, "direction": position.direction, "signal_type": "box_breakout",
            "wave_direction": "roof_weakening" if position.direction == "bullish" else "floor_weakening",
            "entry_date": position.entry_date, "exit_date": bar.get("date",""),
            "entry_price": round(position.entry_price,2), "exit_price": round(bar["c"],2),
            "stop_price": round(position.original_stop,2), "target_price": round(position.target_price,2),
            "exit_reason": "eod_flat", "pnl_pct": round(pnl_pct,2), "pnl_r": round(pnl_r,2),
            "dollar_pnl": round(dollar_pnl,2), "bars_held": position.bars_held,
            "mae_pct": round(position.mae/position.entry_price*100,2),
            "mfe_pct": round(position.mfe/position.entry_price*100,2), "trailed": position.trailed,
            "box_roof": position.box["roof"], "box_floor": position.box["floor"],
            "box_touches": f"{position.box.get('roof_touches',0)}R/{position.box.get('floor_touches',0)}F",
            "wave_label": position.box.get("wave_label",""), "void_target": position.void_target,
            "entry_bar_idx": position.entry_bar, "exit_bar_idx": len(bars)-1, "equity_after": round(equity,2)})

    stats = compute_stats(trades, equity_curve)
    return {"ticker": ticker, "total_bars": n, "trades": trades, "equity_curve": equity_curve,
            "stats": stats,
            "boxes": [{"roof":b["roof"],"floor":b["floor"],"midpoint":b["midpoint"],
                       "start_idx":b["start_idx"],"end_idx":b["end_idx"],
                       "duration_bars":b["duration_bars"],"wave_label":b["wave_label"],
                       "active":b["active"],"broken":b["broken"],
                       "break_direction":b.get("break_direction"),
                       "roof_touches":b["roof_touches"],"floor_touches":b["floor_touches"]}
                      for b in detect_boxes(bars, ticker)],
            "bars": [{"date":b.get("date",""),"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"]} for b in bars]}

def compute_stats(trades, equity_curve):
    if not trades:
        return {"total_trades":0,"initial_equity":INITIAL_EQUITY,"final_equity":INITIAL_EQUITY}
    winners=[t for t in trades if t["pnl_pct"]>0]; losers=[t for t in trades if t["pnl_pct"]<=0]
    total=len(trades); win_rate=len(winners)/total*100
    avg_win=sum(t["pnl_pct"] for t in winners)/len(winners) if winners else 0
    avg_loss=sum(t["pnl_pct"] for t in losers)/len(losers) if losers else 0
    avg_win_r=sum(t["pnl_r"] for t in winners)/len(winners) if winners else 0
    avg_loss_r=sum(t["pnl_r"] for t in losers)/len(losers) if losers else 0
    total_pnl_pct=(equity_curve[-1]["equity"]/INITIAL_EQUITY-1)*100 if equity_curve else 0
    peak=INITIAL_EQUITY; max_dd_pct=0
    for ec in equity_curve:
        if ec["equity"]>peak: peak=ec["equity"]
        dd_pct=(peak-ec["equity"])/peak*100
        if dd_pct>max_dd_pct: max_dd_pct=dd_pct
    gp=sum(t["dollar_pnl"] for t in winners); gl=abs(sum(t["dollar_pnl"] for t in losers))
    pf=gp/gl if gl>0 else float("inf")
    by_dir={}
    for t in trades:
        d=t["direction"]
        if d not in by_dir: by_dir[d]={"count":0,"wins":0,"total_pnl":0,"total_r":0}
        by_dir[d]["count"]+=1
        if t["pnl_pct"]>0: by_dir[d]["wins"]+=1
        by_dir[d]["total_pnl"]+=t["dollar_pnl"]; by_dir[d]["total_r"]+=t["pnl_r"]
    for d in by_dir:
        c=by_dir[d]["count"]; by_dir[d]["win_rate"]=round(by_dir[d]["wins"]/c*100,1)
        by_dir[d]["avg_r"]=round(by_dir[d]["total_r"]/c,2); by_dir[d]["total_pnl"]=round(by_dir[d]["total_pnl"],2)
    by_exit={}
    for t in trades:
        er=t["exit_reason"]
        if er not in by_exit: by_exit[er]={"count":0,"total_pnl":0}
        by_exit[er]["count"]+=1; by_exit[er]["total_pnl"]+=t["dollar_pnl"]
    for er in by_exit: by_exit[er]["total_pnl"]=round(by_exit[er]["total_pnl"],2)
    by_wave={}
    for t in trades:
        wl=t.get("wave_label","")
        if wl not in by_wave: by_wave[wl]={"count":0,"wins":0,"total_r":0}
        by_wave[wl]["count"]+=1
        if t["pnl_pct"]>0: by_wave[wl]["wins"]+=1
        by_wave[wl]["total_r"]+=t["pnl_r"]
    for wl in by_wave:
        c=by_wave[wl]["count"]; by_wave[wl]["win_rate"]=round(by_wave[wl]["wins"]/c*100,1)
        by_wave[wl]["avg_r"]=round(by_wave[wl]["total_r"]/c,2)
    return {"total_trades":total,"winners":len(winners),"losers":len(losers),
            "win_rate":round(win_rate,1),"avg_win_pct":round(avg_win,2),"avg_loss_pct":round(avg_loss,2),
            "avg_win_r":round(avg_win_r,2),"avg_loss_r":round(avg_loss_r,2),
            "total_pnl":round(sum(t["dollar_pnl"] for t in trades),2),
            "total_return_pct":round(total_pnl_pct,2),"max_drawdown_pct":round(max_dd_pct,2),
            "profit_factor":round(pf,2),
            "avg_bars_held":round(sum(t["bars_held"] for t in trades)/total,1),
            "by_direction":by_dir,"by_exit_reason":by_exit,"by_wave_label":by_wave,
            "initial_equity":INITIAL_EQUITY,
            "final_equity":round(equity_curve[-1]["equity"],2) if equity_curve else INITIAL_EQUITY}

def load_csv_bars(path):
    rows=[]
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({"date":r.get("date",""),
                "o":float(r.get("o",r.get("open",r.get("Open",0)))),
                "h":float(r.get("h",r.get("high",r.get("High",0)))),
                "l":float(r.get("l",r.get("low",r.get("Low",0)))),
                "c":float(r.get("c",r.get("close",r.get("Close",0)))),
                "v":int(float(r.get("v",r.get("volume",r.get("Volume",0)))))})
    rows.sort(key=lambda r: r["date"]); return rows

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Potter Box Backtest v2")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--data", required=True, help="Path to daily CSV")
    parser.add_argument("--output", default="backtest/results/potter_backtest_results.json")
    args = parser.parse_args()
    bars = load_csv_bars(args.data); print(f"Loaded {len(bars)} bars from {args.data}")
    results = run_backtest(bars, args.ticker); s = results["stats"]
    print(f"\n{'='*50}")
    print(f"  POTTER BOX BACKTEST v2 — {args.ticker}")
    print(f"{'='*50}")
    print(f"  Strategy: Position in box -> breakout through void -> next box")
    print(f"  Direction: Wave theory (most-touched boundary = weakening)")
    print(f"  Trades: {s['total_trades']} ({s['winners']}W / {s['losers']}L)")
    print(f"  Win Rate: {s['win_rate']}%")
    print(f"  Avg Win: {s['avg_win_pct']}% ({s['avg_win_r']}R)")
    print(f"  Avg Loss: {s['avg_loss_pct']}% ({s['avg_loss_r']}R)")
    print(f"  Total Return: {s['total_return_pct']}%")
    print(f"  Max Drawdown: {s['max_drawdown_pct']}%")
    print(f"  Profit Factor: {s['profit_factor']}")
    print(f"  Avg Hold: {s['avg_bars_held']} bars")
    print(f"\n  By Direction:")
    for d, data in s.get("by_direction",{}).items():
        print(f"    {d}: {data['count']}T, {data['win_rate']}%WR, {data['avg_r']}R avg, ${data['total_pnl']:,.2f}")
    print(f"\n  By Exit:")
    for er, data in s.get("by_exit_reason",{}).items():
        print(f"    {er}: {data['count']}T, ${data['total_pnl']:,.2f}")
    print(f"\n  By Wave Stage:")
    for wl, data in s.get("by_wave_label",{}).items():
        print(f"    {wl}: {data['count']}T, {data['win_rate']}%WR, {data['avg_r']}R avg")
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f: json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {args.output}")
