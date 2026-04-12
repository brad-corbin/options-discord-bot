#!/usr/bin/env python3
"""
Potter Box Walk-Forward Backtest
═════════════════════════════════
Simulates the Potter Box strategy on daily bars, matching the bot's rules:

Entry conditions (all must be true):
  1. Active box exists with maturity >= "mid" (past 50% of avg duration)
  2. Wave label >= "weakening" (3+ touches on one boundary)
  3. Price is inside the box
  4. Direction signal fires (simulated via price action since we lack live flow)
     - Bullish: price bounces off floor (close near floor + next bar up)
     - Bearish: price rejects off roof (close near roof + next bar down)
  5. Void exists in the trade direction (need room to run)

Exit rules:
  - Target: next box boundary through the void (box-to-box)
  - Stop: opposite boundary of current box
  - Max hold: 15 bars (configurable)
  - Trailing: if price hits CB in profit direction, move stop to CB

Trade structure (simplified for stock-price backtest):
  - 4:1 R:R sizing — risk 1 unit per trade, target 4 units
  - P&L calculated from actual price movement as % of entry

Output: trades list + equity curve + stats (JSON for visualization)
"""

import sys, os, json, math, csv
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# Support both local and repo imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))

from potter_box import (
    detect_boxes, detect_voids, detect_supply_demand_zones, detect_cb_gap_signals,
    detect_punchback, classify_maturity, _body_top, _body_bot,
    TOUCH_ZONE_PCT, MIN_BOX_BARS, DEFAULT_DURATION, _get_tier
)


# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

MAX_HOLD_BARS = 15
COOLDOWN_BARS = 3          # no re-entry same direction within N bars
RISK_PER_TRADE_PCT = 2.0   # % of equity risked per trade
INITIAL_EQUITY = 10000.0
TRAIL_TO_CB = True         # move stop to CB once in profit
MIN_VOID_FOR_ENTRY = 2.0   # void must be at least 2% to trade
MIN_WAVE_STAGE = 3         # minimum touches on a boundary (weakening+)
MIN_MATURITY = "mid"       # box must be at least "mid" maturity


# ═══════════════════════════════════════════════════════════
# SIGNAL DETECTION (price-action proxy for flow)
# ═══════════════════════════════════════════════════════════

def detect_floor_bounce(bars, box, bar_idx):
    """
    Bullish signal: price touches floor zone and bounces.
    - Current bar: low touches floor zone AND close > open (bullish candle)
    - OR: previous bar touched floor, current bar opens above previous close
    """
    if bar_idx < 1: return False
    floor = box["floor"]; tz = floor * TOUCH_ZONE_PCT
    curr = bars[bar_idx]; prev = bars[bar_idx - 1]
    
    bb_curr = _body_bot(curr); bb_prev = _body_bot(prev)
    
    # Current bar bounces off floor
    if (abs(bb_curr - floor) <= tz * 2 or curr["l"] < floor * (1 + TOUCH_ZONE_PCT)) and curr["c"] > curr["o"]:
        return True
    # Previous bar touched floor, current bar gaps up
    if (abs(bb_prev - floor) <= tz * 2 or prev["l"] < floor * (1 + TOUCH_ZONE_PCT)) and curr["o"] > prev["c"]:
        return True
    return False


def detect_roof_rejection(bars, box, bar_idx):
    """
    Bearish signal: price touches roof zone and rejects.
    - Current bar: high touches roof zone AND close < open (bearish candle)
    - OR: previous bar touched roof, current bar opens below previous close
    """
    if bar_idx < 1: return False
    roof = box["roof"]; tz = roof * TOUCH_ZONE_PCT
    curr = bars[bar_idx]; prev = bars[bar_idx - 1]
    
    bt_curr = _body_top(curr); bt_prev = _body_top(prev)
    
    if (abs(bt_curr - roof) <= tz * 2 or curr["h"] > roof * (1 - TOUCH_ZONE_PCT)) and curr["c"] < curr["o"]:
        return True
    if (abs(bt_prev - roof) <= tz * 2 or prev["h"] > roof * (1 - TOUCH_ZONE_PCT)) and curr["o"] < prev["c"]:
        return True
    return False


def detect_breakout_entry(bars, box, bar_idx):
    """
    Breakout signal: price closes decisively outside box.
    Returns "bullish" or "bearish" or None.
    """
    if bar_idx < 1: return None
    curr = bars[bar_idx]; prev = bars[bar_idx - 1]
    roof = box["roof"]; floor = box["floor"]
    tz_r = roof * TOUCH_ZONE_PCT; tz_f = floor * TOUCH_ZONE_PCT
    
    # Was inside, now closed above roof
    if prev["c"] <= roof * (1 + TOUCH_ZONE_PCT) and curr["c"] > roof * (1 + TOUCH_ZONE_PCT * 2):
        return "bullish"
    # Was inside, now closed below floor
    if prev["c"] >= floor * (1 - TOUCH_ZONE_PCT) and curr["c"] < floor * (1 - TOUCH_ZONE_PCT * 2):
        return "bearish"
    return None


def detect_punchback_entry(bars, box, bar_idx):
    """
    Punchback signal: was outside box, returned inside.
    Returns "bullish" (broke below, returned) or "bearish" (broke above, returned).
    """
    if bar_idx < 2: return None
    curr = bars[bar_idx]; prev = bars[bar_idx - 1]
    roof = box["roof"]; floor = box["floor"]
    
    inside_now = floor * (1 - TOUCH_ZONE_PCT) <= curr["c"] <= roof * (1 + TOUCH_ZONE_PCT)
    was_below = prev["c"] < floor * (1 - TOUCH_ZONE_PCT)
    was_above = prev["c"] > roof * (1 + TOUCH_ZONE_PCT)
    
    if inside_now and was_below: return "bullish"
    if inside_now and was_above: return "bearish"
    return None


def detect_cb_gap_entry(bars, box, bar_idx):
    """
    CB gap signal: gapped across cost basis line.
    Returns "bullish" (gap above CB → target roof) or "bearish" (gap below CB → target floor).
    """
    if bar_idx < 1: return None
    curr = bars[bar_idx]; prev = bars[bar_idx - 1]
    cb = box["midpoint"]
    
    if prev["c"] < cb and curr["o"] > cb: return "bullish"
    if prev["c"] > cb and curr["o"] < cb: return "bearish"
    return None


# ═══════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════════

class Position:
    def __init__(self, ticker, direction, entry_price, stop_price, target_price,
                 entry_bar, entry_date, signal_type, box, risk_pct=RISK_PER_TRADE_PCT):
        self.ticker = ticker
        self.direction = direction
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.target_price = target_price
        self.entry_bar = entry_bar
        self.entry_date = entry_date
        self.signal_type = signal_type
        self.box = box
        self.risk_pct = risk_pct
        self.bars_held = 0
        self.mae = 0.0  # max adverse excursion
        self.mfe = 0.0  # max favorable excursion
        self.trail_activated = False
        self.original_stop = stop_price
    
    def risk_amount(self):
        return abs(self.entry_price - self.stop_price)
    
    def reward_amount(self):
        return abs(self.target_price - self.entry_price)
    
    def rr_ratio(self):
        r = self.risk_amount()
        return self.reward_amount() / r if r > 0 else 0


# ═══════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════

def run_backtest(bars: List[Dict], ticker: str, 
                 trade_signals: str = "all") -> Dict:
    """
    Walk-forward backtest.
    
    trade_signals: "bounce" | "breakout" | "punchback" | "cb_gap" | "all"
    """
    n = len(bars)
    if n < MIN_BOX_BARS + 20:
        return {"error": "Not enough bars", "trades": [], "equity_curve": []}
    
    trades = []
    equity = INITIAL_EQUITY
    equity_curve = []
    position: Optional[Position] = None
    cooldowns = {"bullish": -999, "bearish": -999}
    tier = _get_tier(ticker)
    avg_dur = DEFAULT_DURATION.get(tier, 18)
    
    # Walk forward, one bar at a time
    for i in range(MIN_BOX_BARS + 2, n):
        visible_bars = bars[:i + 1]
        bar = bars[i]
        bar_date = bar.get("date", str(i))
        
        # ── Manage open position ──
        if position is not None:
            position.bars_held += 1
            
            # Track MAE / MFE
            if position.direction == "bullish":
                excursion_up = bar["h"] - position.entry_price
                excursion_down = position.entry_price - bar["l"]
                position.mfe = max(position.mfe, excursion_up)
                position.mae = max(position.mae, excursion_down)
            else:
                excursion_up = position.entry_price - bar["l"]
                excursion_down = bar["h"] - position.entry_price
                position.mfe = max(position.mfe, excursion_up)
                position.mae = max(position.mae, excursion_down)
            
            # Trail stop to CB if in profit
            if TRAIL_TO_CB and not position.trail_activated:
                cb = position.box["midpoint"]
                if position.direction == "bullish" and bar["c"] > cb and bar["c"] > position.entry_price:
                    position.stop_price = max(position.stop_price, cb)
                    position.trail_activated = True
                elif position.direction == "bearish" and bar["c"] < cb and bar["c"] < position.entry_price:
                    position.stop_price = min(position.stop_price, cb)
                    position.trail_activated = True
            
            # Check exits (stop checked first — conservative)
            exit_price = None; exit_reason = None
            
            if position.direction == "bullish":
                if bar["l"] <= position.stop_price:
                    exit_price = position.stop_price; exit_reason = "stop"
                elif bar["h"] >= position.target_price:
                    exit_price = position.target_price; exit_reason = "target"
            else:
                if bar["h"] >= position.stop_price:
                    exit_price = position.stop_price; exit_reason = "stop"
                elif bar["l"] <= position.target_price:
                    exit_price = position.target_price; exit_reason = "target"
            
            if exit_reason is None and position.bars_held >= MAX_HOLD_BARS:
                exit_price = bar["c"]; exit_reason = "max_hold"
            
            if exit_price is not None:
                # Calculate P&L
                if position.direction == "bullish":
                    pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100
                else:
                    pnl_pct = (position.entry_price - exit_price) / position.entry_price * 100
                
                risk_pct = position.risk_amount() / position.entry_price * 100
                pnl_r = pnl_pct / risk_pct if risk_pct > 0 else 0
                dollar_pnl = equity * (position.risk_pct / 100) * pnl_r
                equity += dollar_pnl
                
                trades.append({
                    "ticker": ticker,
                    "direction": position.direction,
                    "signal_type": position.signal_type,
                    "entry_date": position.entry_date,
                    "exit_date": bar_date,
                    "entry_price": round(position.entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "stop_price": round(position.original_stop, 2),
                    "target_price": round(position.target_price, 2),
                    "exit_reason": exit_reason,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_r": round(pnl_r, 2),
                    "dollar_pnl": round(dollar_pnl, 2),
                    "bars_held": position.bars_held,
                    "mae_pct": round(position.mae / position.entry_price * 100, 2),
                    "mfe_pct": round(position.mfe / position.entry_price * 100, 2),
                    "rr_ratio": round(position.rr_ratio(), 2),
                    "trailed": position.trail_activated,
                    "box_roof": position.box["roof"],
                    "box_floor": position.box["floor"],
                    "wave_label": position.box.get("wave_label", ""),
                    "entry_bar_idx": position.entry_bar,
                    "exit_bar_idx": i,
                    "equity_after": round(equity, 2),
                })
                
                cooldowns[position.direction] = i
                position = None
        
        # Record equity
        equity_curve.append({
            "bar_idx": i,
            "date": bar_date,
            "close": bar["c"],
            "equity": round(equity, 2),
            "in_trade": position is not None,
        })
        
        # ── Look for new entries (only if flat) ──
        if position is not None:
            continue
        
        # Detect boxes from visible bars only (no lookahead)
        all_boxes = detect_boxes(visible_bars, ticker)
        if not all_boxes:
            continue
        
        active = [b for b in all_boxes if b["active"]]
        if not active:
            continue
        
        box = active[-1]
        mat = classify_maturity(box, avg_dur)
        
        # Gate: maturity
        if mat["maturity"] == "early":
            continue
        
        # Gate: wave stage
        mt = box.get("max_touches", 0)
        if mt < MIN_WAVE_STAGE:
            continue
        
        # Detect voids
        voids = detect_voids(visible_bars, all_boxes, ticker)
        va = vb = None
        for v in voids:
            if v["position"] == "above" and (va is None or v["height_pct"] > va["height_pct"]): va = v
            if v["position"] == "below" and (vb is None or v["height_pct"] > vb["height_pct"]): vb = v
        
        # Adjacent boxes for targets
        other_boxes = [b for b in all_boxes if b is not box]
        box_above = box_below = None
        for ob in other_boxes:
            if ob["floor"] > box["roof"] * 0.98:
                if box_above is None or ob["floor"] < box_above["floor"]: box_above = ob
            if ob["roof"] < box["floor"] * 1.02:
                if box_below is None or ob["roof"] > box_below["roof"]: box_below = ob
        
        # ── Check signals ──
        direction = None; signal_type = None
        
        # 1. Floor bounce
        if trade_signals in ("bounce", "all"):
            if detect_floor_bounce(visible_bars, box, i) and va and va["height_pct"] >= MIN_VOID_FOR_ENTRY:
                direction = "bullish"; signal_type = "floor_bounce"
        
        # 2. Roof rejection
        if direction is None and trade_signals in ("bounce", "all"):
            if detect_roof_rejection(visible_bars, box, i) and vb and vb["height_pct"] >= MIN_VOID_FOR_ENTRY:
                direction = "bearish"; signal_type = "roof_rejection"
        
        # 3. Breakout
        if direction is None and trade_signals in ("breakout", "all"):
            bo = detect_breakout_entry(visible_bars, box, i)
            if bo == "bullish" and va and va["height_pct"] >= MIN_VOID_FOR_ENTRY:
                direction = "bullish"; signal_type = "breakout"
            elif bo == "bearish" and vb and vb["height_pct"] >= MIN_VOID_FOR_ENTRY:
                direction = "bearish"; signal_type = "breakout"
        
        # 4. Punchback
        if direction is None and trade_signals in ("punchback", "all"):
            pb = detect_punchback_entry(visible_bars, box, i)
            if pb == "bullish": direction = "bullish"; signal_type = "punchback"
            elif pb == "bearish": direction = "bearish"; signal_type = "punchback"
        
        # 5. CB gap
        if direction is None and trade_signals in ("cb_gap", "all"):
            cg = detect_cb_gap_entry(visible_bars, box, i)
            if cg == "bullish" and va: direction = "bullish"; signal_type = "cb_gap"
            elif cg == "bearish" and vb: direction = "bearish"; signal_type = "cb_gap"
        
        if direction is None:
            continue
        
        # Cooldown check
        if i - cooldowns[direction] < COOLDOWN_BARS:
            continue
        
        # Set stop and target
        entry_price = bar["c"]  # enter at close (next bar open in production)
        
        if direction == "bullish":
            stop_price = box["floor"] * (1 - TOUCH_ZONE_PCT)
            target_price = box_above["floor"] if box_above else (va["high"] if va else box["roof"] * 1.05)
        else:
            stop_price = box["roof"] * (1 + TOUCH_ZONE_PCT)
            target_price = box_below["roof"] if box_below else (vb["low"] if vb else box["floor"] * 0.95)
        
        # Sanity: target must be better than entry
        if direction == "bullish" and target_price <= entry_price: continue
        if direction == "bearish" and target_price >= entry_price: continue
        
        # Open position
        position = Position(
            ticker=ticker, direction=direction,
            entry_price=entry_price, stop_price=stop_price,
            target_price=target_price, entry_bar=i,
            entry_date=bar_date, signal_type=signal_type, box=box
        )
    
    # Close any remaining position at last bar
    if position is not None:
        bar = bars[-1]
        if position.direction == "bullish":
            pnl_pct = (bar["c"] - position.entry_price) / position.entry_price * 100
        else:
            pnl_pct = (position.entry_price - bar["c"]) / position.entry_price * 100
        risk_pct = position.risk_amount() / position.entry_price * 100
        pnl_r = pnl_pct / risk_pct if risk_pct > 0 else 0
        dollar_pnl = equity * (RISK_PER_TRADE_PCT / 100) * pnl_r
        equity += dollar_pnl
        trades.append({
            "ticker": ticker, "direction": position.direction,
            "signal_type": position.signal_type,
            "entry_date": position.entry_date, "exit_date": bar.get("date", ""),
            "entry_price": round(position.entry_price, 2),
            "exit_price": round(bar["c"], 2),
            "stop_price": round(position.original_stop, 2),
            "target_price": round(position.target_price, 2),
            "exit_reason": "eod_flat",
            "pnl_pct": round(pnl_pct, 2), "pnl_r": round(pnl_r, 2),
            "dollar_pnl": round(dollar_pnl, 2),
            "bars_held": position.bars_held,
            "mae_pct": round(position.mae / position.entry_price * 100, 2),
            "mfe_pct": round(position.mfe / position.entry_price * 100, 2),
            "rr_ratio": round(position.rr_ratio(), 2),
            "trailed": position.trail_activated,
            "box_roof": position.box["roof"], "box_floor": position.box["floor"],
            "wave_label": position.box.get("wave_label", ""),
            "entry_bar_idx": position.entry_bar, "exit_bar_idx": len(bars) - 1,
            "equity_after": round(equity, 2),
        })
    
    # ── Compute stats ──
    stats = compute_stats(trades, equity_curve)
    
    return {
        "ticker": ticker,
        "total_bars": n,
        "trades": trades,
        "equity_curve": equity_curve,
        "stats": stats,
        "boxes": [{"roof": b["roof"], "floor": b["floor"], "midpoint": b["midpoint"],
                   "start_idx": b["start_idx"], "end_idx": b["end_idx"],
                   "duration_bars": b["duration_bars"], "wave_label": b["wave_label"],
                   "active": b["active"], "broken": b["broken"],
                   "break_direction": b.get("break_direction"),
                   "roof_touches": b["roof_touches"], "floor_touches": b["floor_touches"]}
                  for b in detect_boxes(bars, ticker)],
        "bars": [{"date": b.get("date",""), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
                 for b in bars],
    }


def compute_stats(trades, equity_curve):
    if not trades:
        return {"total_trades": 0}
    
    winners = [t for t in trades if t["pnl_pct"] > 0]
    losers = [t for t in trades if t["pnl_pct"] <= 0]
    
    total = len(trades)
    win_rate = len(winners) / total * 100 if total > 0 else 0
    
    avg_win = sum(t["pnl_pct"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl_pct"] for t in losers) / len(losers) if losers else 0
    avg_win_r = sum(t["pnl_r"] for t in winners) / len(winners) if winners else 0
    avg_loss_r = sum(t["pnl_r"] for t in losers) / len(losers) if losers else 0
    
    total_pnl = sum(t["dollar_pnl"] for t in trades)
    total_pnl_pct = (equity_curve[-1]["equity"] / INITIAL_EQUITY - 1) * 100 if equity_curve else 0
    
    # Max drawdown
    peak = INITIAL_EQUITY; max_dd = 0; max_dd_pct = 0
    for ec in equity_curve:
        if ec["equity"] > peak: peak = ec["equity"]
        dd = peak - ec["equity"]
        dd_pct = dd / peak * 100 if peak > 0 else 0
        if dd_pct > max_dd_pct: max_dd_pct = dd_pct; max_dd = dd
    
    # Profit factor
    gross_profit = sum(t["dollar_pnl"] for t in winners)
    gross_loss = abs(sum(t["dollar_pnl"] for t in losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    # By signal type
    by_signal = {}
    for t in trades:
        st = t["signal_type"]
        if st not in by_signal:
            by_signal[st] = {"count": 0, "wins": 0, "total_pnl": 0, "total_r": 0}
        by_signal[st]["count"] += 1
        if t["pnl_pct"] > 0: by_signal[st]["wins"] += 1
        by_signal[st]["total_pnl"] += t["dollar_pnl"]
        by_signal[st]["total_r"] += t["pnl_r"]
    
    for st in by_signal:
        c = by_signal[st]["count"]
        by_signal[st]["win_rate"] = round(by_signal[st]["wins"] / c * 100, 1) if c > 0 else 0
        by_signal[st]["avg_r"] = round(by_signal[st]["total_r"] / c, 2) if c > 0 else 0
        by_signal[st]["total_pnl"] = round(by_signal[st]["total_pnl"], 2)
    
    # By exit reason
    by_exit = {}
    for t in trades:
        er = t["exit_reason"]
        if er not in by_exit: by_exit[er] = {"count": 0, "total_pnl": 0}
        by_exit[er]["count"] += 1
        by_exit[er]["total_pnl"] += t["dollar_pnl"]
    for er in by_exit: by_exit[er]["total_pnl"] = round(by_exit[er]["total_pnl"], 2)
    
    return {
        "total_trades": total,
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_win_r": round(avg_win_r, 2),
        "avg_loss_r": round(avg_loss_r, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_pnl_pct, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_bars_held": round(sum(t["bars_held"] for t in trades) / total, 1),
        "by_signal_type": by_signal,
        "by_exit_reason": by_exit,
        "initial_equity": INITIAL_EQUITY,
        "final_equity": round(equity_curve[-1]["equity"], 2) if equity_curve else INITIAL_EQUITY,
    }


# ═══════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════

def load_csv_bars(path: str) -> list:
    """Load bars from CSV (compatible with backtest/download_bars.py format)."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "date": r.get("date", ""),
                "o": float(r.get("o", r.get("open", r.get("Open", 0)))),
                "h": float(r.get("h", r.get("high", r.get("High", 0)))),
                "l": float(r.get("l", r.get("low", r.get("Low", 0)))),
                "c": float(r.get("c", r.get("close", r.get("Close", 0)))),
                "v": int(float(r.get("v", r.get("volume", r.get("Volume", 0))))),
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def load_yfinance_bars(ticker: str, months: int = 18) -> list:
    """Fetch bars via yfinance."""
    import yfinance as yf
    from datetime import timedelta
    end = datetime.now()
    start = end - timedelta(days=months * 30)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), interval="1d", progress=False)
    if df.empty: return []
    if hasattr(df.columns, 'levels'):
        df.columns = df.columns.get_level_values(0)
    bars = []
    for idx, row in df.iterrows():
        bars.append({"date": idx.strftime("%Y-%m-%d"),
                     "o": float(row["Open"]), "h": float(row["High"]),
                     "l": float(row["Low"]), "c": float(row["Close"]),
                     "v": int(row.get("Volume", 0))})
    return bars


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Potter Box Backtest")
    parser.add_argument("--ticker", default="AAPL", help="Ticker symbol")
    parser.add_argument("--data", help="Path to CSV file with daily bars")
    parser.add_argument("--months", type=int, default=18, help="Months of data (yfinance)")
    parser.add_argument("--signals", default="all",
                        choices=["bounce", "breakout", "punchback", "cb_gap", "all"])
    parser.add_argument("--output", default="potter_backtest_results.json")
    args = parser.parse_args()
    
    # Load data
    if args.data:
        bars = load_csv_bars(args.data)
        print(f"Loaded {len(bars)} bars from {args.data}")
    else:
        print(f"Fetching {args.ticker} via yfinance ({args.months} months)...")
        bars = load_yfinance_bars(args.ticker, args.months)
        print(f"Fetched {len(bars)} bars")
    
    if not bars:
        print("No data!"); sys.exit(1)
    
    # Run backtest
    results = run_backtest(bars, args.ticker, trade_signals=args.signals)
    
    # Print summary
    s = results["stats"]
    print(f"\n{'='*50}")
    print(f"  POTTER BOX BACKTEST — {args.ticker}")
    print(f"{'='*50}")
    print(f"  Trades: {s['total_trades']} ({s['winners']}W / {s['losers']}L)")
    print(f"  Win Rate: {s['win_rate']}%")
    print(f"  Avg Win: {s['avg_win_pct']}% ({s['avg_win_r']}R)")
    print(f"  Avg Loss: {s['avg_loss_pct']}% ({s['avg_loss_r']}R)")
    print(f"  Total Return: {s['total_return_pct']}%")
    print(f"  Max Drawdown: {s['max_drawdown_pct']}%")
    print(f"  Profit Factor: {s['profit_factor']}")
    print(f"  Avg Hold: {s['avg_bars_held']} bars")
    print(f"\n  By Signal Type:")
    for st, data in s.get("by_signal_type", {}).items():
        print(f"    {st}: {data['count']} trades, {data['win_rate']}% WR, {data['avg_r']}R avg, ${data['total_pnl']} P&L")
    print(f"\n  By Exit Reason:")
    for er, data in s.get("by_exit_reason", {}).items():
        print(f"    {er}: {data['count']} trades, ${data['total_pnl']} P&L")
    
    # Save results
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {args.output}")
