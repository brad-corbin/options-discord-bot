# potter_box.py
# ═══════════════════════════════════════════════════════════════════
# Potter Box — Consolidation Range + Void + Supply/Demand Zones
#
# Core Rules (from Potter Box strategy):
#   1. Box boundaries defined by candle BODIES, not wicks
#      (wicks represent failed/rejected price)
#   2. 50% cost basis line = battleground between buyers and sellers
#   3. Wave theory: each touch weakens the boundary — breakout on 3rd-5th hit
#   4. 5% breakout rule: price must move 5% past boundary to confirm
#   5. Gap signals: gap across CB line → travel to opposite boundary
#   6. Gap outside box entirely → dramatic move to next box
#   7. Punch-back: break outside then return within 2-5 bars → reversal
#   8. Supply/demand zones: large candles (institutional prints)
#      "Three bar play" = strong zone
#   9. Box-to-box: price moves between consolidation ranges through voids
#
# Data: yfinance daily OHLCV (free) + MarketData chains (for strikes)
# Persistence: Redis via PersistentState (survives redeploy)
# Schedule: 8:15 AM CT (detect) + 3:05 PM CT (update + catalog)
# ═══════════════════════════════════════════════════════════════════

import logging
import time
import math
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable, Tuple

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

MIN_BOX_BARS = 8
MAX_RANGE_PCT = {"index": 0.06, "mega_cap": 0.10, "large_cap": 0.14, "mid_cap": 0.14}
TOUCH_ZONE_PCT = 0.005
WAVE_LABELS = {2: "established", 3: "weakening", 4: "breakout_probable", 5: "breakout_imminent"}
BREAKOUT_CONFIRM_PCT = 0.05
PUNCHBACK_MAX_BARS = 5
PUNCHBACK_MIN_BARS = 1
LARGE_CANDLE_ATR_MULT = 1.5
THREE_BAR_PLAY_MIN = 3
MIN_VOID_PCT = 0.03
MAX_VOID_BAR_DENSITY = 3
ATR_PERIOD = 14
MATURITY_EARLY_PCT = 0.50
MATURITY_MID_PCT = 0.75
MATURITY_LATE_PCT = 1.00
DEFAULT_DURATION = {"index": 10, "mega_cap": 15, "large_cap": 18, "mid_cap": 20}
DTE_MAP = {"early": None, "mid": 28, "late": 21, "overdue": 14}
IV_PERCENTILE_MAX = 35
TTL_ACTIVE_BOX = 7 * 86400
TTL_HISTORY_BOX = 90 * 86400
TTL_VOID_MAP = 7 * 86400
TTL_DEFAULTS = 30 * 86400
TTL_ZONE = 30 * 86400

TIER_MAP = {
    "index": {"SPY", "QQQ", "IWM", "DIA"},
    "mega_cap": {"AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "GOOGL"},
    "large_cap": {"AMD", "AVGO", "NFLX", "CRM", "BA", "LLY", "UNH", "JPM", "GS", "CAT", "ORCL", "ARM"},
}

def _get_tier(ticker):
    t = ticker.upper()
    for tier, tickers in TIER_MAP.items():
        if t in tickers: return tier
    return "mid_cap"

def _max_range_pct(ticker): return MAX_RANGE_PCT.get(_get_tier(ticker), 0.14)
def _body_top(bar): return max(bar["o"], bar["c"])
def _body_bot(bar): return min(bar["o"], bar["c"])
def _body_size(bar): return abs(bar["c"] - bar["o"])

def _compute_atr(bars, period=ATR_PERIOD):
    if len(bars) < period + 1: return 0
    trs = [bars[0]["h"] - bars[0]["l"]]
    for i in range(1, len(bars)):
        tr = max(bars[i]["h"] - bars[i]["l"],
                 abs(bars[i]["h"] - bars[i-1]["c"]),
                 abs(bars[i]["l"] - bars[i-1]["c"]))
        trs.append(tr)
    return sum(trs[-period:]) / period


# ═══════════════════════════════════════════════════════════
# BOX DETECTION — BODIES DEFINE BOUNDARIES
# ═══════════════════════════════════════════════════════════

def detect_boxes(bars, ticker):
    if not bars or len(bars) < MIN_BOX_BARS + 2: return []
    max_range = _max_range_pct(ticker)
    n = len(bars); boxes = []
    i = 0
    while i < n - MIN_BOX_BARS:
        box_start = i
        running_high = _body_top(bars[i])
        running_low = _body_bot(bars[i])
        j = i + 1
        while j < n:
            ch = max(running_high, _body_top(bars[j]))
            cl = min(running_low, _body_bot(bars[j]))
            mid = (ch + cl) / 2
            if mid <= 0: break
            if (ch - cl) / mid > max_range: break
            running_high = ch; running_low = cl; j += 1
        box_end = j - 1; box_bars = box_end - box_start + 1
        if box_bars >= MIN_BOX_BARS:
            roof = running_high; floor = running_low
            rz = roof * TOUCH_ZONE_PCT; fz = floor * TOUCH_ZONE_PCT
            rt = sum(1 for k in range(box_start, box_end+1) if abs(_body_top(bars[k]) - roof) <= rz)
            ft = sum(1 for k in range(box_start, box_end+1) if abs(_body_bot(bars[k]) - floor) <= fz)
            if rt >= 2 and ft >= 2:
                midpoint = (roof + floor) / 2
                rp = (roof - floor) / midpoint * 100 if midpoint > 0 else 0
                mt = max(rt, ft)
                wl = WAVE_LABELS.get(min(mt, 5), "breakout_imminent" if mt >= 5 else "established")
                lb = bars[-1]; still_active = (_body_bot(lb) >= floor * (1-TOUCH_ZONE_PCT) and _body_top(lb) <= roof * (1+TOUCH_ZONE_PCT))
                broken = False; break_dir = None; confirmed = False; break_idx = None; pb = False
                if not still_active and box_end < n - 1:
                    for k in range(box_end+1, n):
                        bc = bars[k]["c"]
                        if bc > roof * (1 + BREAKOUT_CONFIRM_PCT):
                            broken = True; confirmed = True; break_dir = "up"; break_idx = k; break
                        elif bc < floor * (1 - BREAKOUT_CONFIRM_PCT):
                            broken = True; confirmed = True; break_dir = "down"; break_idx = k; break
                        elif bc > roof * (1 + TOUCH_ZONE_PCT) and not broken:
                            broken = True; break_dir = "up"; break_idx = k
                        elif bc < floor * (1 - TOUCH_ZONE_PCT) and not broken:
                            broken = True; break_dir = "down"; break_idx = k
                    if broken and break_idx and not confirmed:
                        bars_out = 0
                        for k in range(break_idx, min(break_idx + PUNCHBACK_MAX_BARS + 1, n)):
                            bc2 = bars[k]["c"]
                            if floor * (1-TOUCH_ZONE_PCT) <= bc2 <= roof * (1+TOUCH_ZONE_PCT):
                                if bars_out >= PUNCHBACK_MIN_BARS: pb = True
                                break
                            bars_out += 1
                rd = 0
                if confirmed and break_idx:
                    if break_dir == "up": rd = max(b["h"] for b in bars[break_idx:]) - roof
                    else: rd = floor - min(b["l"] for b in bars[break_idx:])
                boxes.append({
                    "ticker": ticker.upper(), "roof": round(roof, 2), "floor": round(floor, 2),
                    "midpoint": round(midpoint, 2), "range_pct": round(rp, 2),
                    "duration_bars": box_bars, "roof_touches": rt, "floor_touches": ft,
                    "wave_label": wl, "max_touches": mt,
                    "start_idx": box_start, "end_idx": box_end,
                    "start_date": str(bars[box_start].get("date", ""))[:10],
                    "end_date": str(bars[box_end].get("date", ""))[:10],
                    "active": still_active, "broken": broken, "break_confirmed": confirmed,
                    "break_direction": break_dir, "punchback": pb,
                    "run_distance": round(rd, 2),
                    "run_pct": round(rd / midpoint * 100, 2) if midpoint > 0 else 0,
                })
            i = box_end + 1
        else: i += 1
    return boxes


# ═══════════════════════════════════════════════════════════
# COST BASIS GAP SIGNAL
# ═══════════════════════════════════════════════════════════

def detect_cb_gap_signals(bars, box):
    if not box.get("active"): return []
    signals = []; mp = box["midpoint"]; rf = box["roof"]; fl = box["floor"]
    si = box["start_idx"]; ei = box["end_idx"]
    for i in range(max(si, 1), min(ei + 1, len(bars) - 1)):
        pc = bars[i-1]["c"]; to = bars[i]["o"]
        if pc < mp and to > mp:
            signals.append({"type": "cb_gap_bullish", "bar_idx": i,
                "date": str(bars[i].get("date",""))[:10], "prev_close": round(pc,2),
                "open": round(to,2), "midpoint": round(mp,2), "target": round(rf,2),
                "note": "Closed below CB, gapped above → target roof"})
        elif pc > mp and to < mp:
            signals.append({"type": "cb_gap_bearish", "bar_idx": i,
                "date": str(bars[i].get("date",""))[:10], "prev_close": round(pc,2),
                "open": round(to,2), "midpoint": round(mp,2), "target": round(fl,2),
                "note": "Closed above CB, gapped below → target floor"})
    return signals


# ═══════════════════════════════════════════════════════════
# GAP OUTSIDE BOX
# ═══════════════════════════════════════════════════════════

def detect_gap_outside(bars, box):
    if not box.get("active") or len(bars) < 2: return None
    rf = box["roof"]; fl = box["floor"]; lb = bars[-1]; pb = bars[-2]
    if pb["c"] <= rf * (1+TOUCH_ZONE_PCT) and lb["o"] > rf * (1+TOUCH_ZONE_PCT*2):
        return {"type": "gap_above_box", "direction": "up",
            "date": str(lb.get("date",""))[:10], "gap_from": round(pb["c"],2),
            "gap_to": round(lb["o"],2),
            "note": "Gapped entirely above box — dramatic move to next box above"}
    if pb["c"] >= fl * (1-TOUCH_ZONE_PCT) and lb["o"] < fl * (1-TOUCH_ZONE_PCT*2):
        return {"type": "gap_below_box", "direction": "down",
            "date": str(lb.get("date",""))[:10], "gap_from": round(pb["c"],2),
            "gap_to": round(lb["o"],2),
            "note": "Gapped entirely below box — dramatic move to next box below"}
    return None


# ═══════════════════════════════════════════════════════════
# PUNCH-BACK DETECTION
# ═══════════════════════════════════════════════════════════

def detect_punchback(bars, box):
    if len(bars) < 3: return None
    rf = box["roof"]; fl = box["floor"]; mp = box["midpoint"]
    lookback = min(7, len(bars)); recent = bars[-lookback:]
    for i in range(len(recent) - 1):
        c = recent[i]["c"]
        inside = fl * (1-TOUCH_ZONE_PCT) <= c <= rf * (1+TOUCH_ZONE_PCT)
        if inside: continue
        broke_above = c > rf * (1+TOUCH_ZONE_PCT)
        bars_out = 1
        for k in range(i+1, min(i+PUNCHBACK_MAX_BARS+1, len(recent))):
            rc = recent[k]["c"]
            ret = fl * (1-TOUCH_ZONE_PCT) <= rc <= rf * (1+TOUCH_ZONE_PCT)
            if ret and bars_out >= PUNCHBACK_MIN_BARS:
                avg_v = sum(b.get("v",0) for b in recent) / max(len(recent),1)
                vol_spike = recent[k].get("v",0) > avg_v * 1.3 if avg_v > 0 else False
                if broke_above:
                    return {"type": "punchback", "direction": "bearish", "bars_outside": bars_out,
                        "target_cb": round(mp,2), "target_opposite": round(fl,2),
                        "volume_spike": vol_spike, "date": str(recent[k].get("date",""))[:10],
                        "note": f"Punch-back: broke above ${rf:.2f}, returned in {bars_out} bars → target CB ${mp:.2f} or floor ${fl:.2f}"}
                else:
                    return {"type": "punchback", "direction": "bullish", "bars_outside": bars_out,
                        "target_cb": round(mp,2), "target_opposite": round(rf,2),
                        "volume_spike": vol_spike, "date": str(recent[k].get("date",""))[:10],
                        "note": f"Punch-back: broke below ${fl:.2f}, returned in {bars_out} bars → target CB ${mp:.2f} or roof ${rf:.2f}"}
            elif not ret: bars_out += 1
            else: break
    return None


# ═══════════════════════════════════════════════════════════
# SUPPLY / DEMAND ZONES — LARGE CANDLES (BODIES ONLY)
# ═══════════════════════════════════════════════════════════

def detect_supply_demand_zones(bars, ticker):
    if len(bars) < ATR_PERIOD + 5: return []
    atr = _compute_atr(bars, ATR_PERIOD)
    if atr <= 0: return []
    zones = []; thresh = atr * LARGE_CANDLE_ATR_MULT; n = len(bars)
    i = ATR_PERIOD
    while i < n:
        body = _body_size(bars[i])
        if body < thresh: i += 1; continue
        bullish = bars[i]["c"] > bars[i]["o"]; zone_bars = [bars[i]]; consec = 1
        for k in range(i+1, min(i+6, n)):
            nb = _body_size(bars[k]); nb_bull = bars[k]["c"] > bars[k]["o"]
            if nb >= thresh and nb_bull == bullish: zone_bars.append(bars[k]); consec += 1
            elif nb >= thresh * 0.7 and nb_bull == bullish: zone_bars.append(bars[k])
            else: break
        zt = max(_body_top(b) for b in zone_bars); zb = min(_body_bot(b) for b in zone_bars)
        if consec >= 3: strength = "STRONGER"
        elif consec >= 2: strength = "STRONG"
        else: strength = "WEAK"
        zones.append({
            "ticker": ticker.upper(), "type": "demand" if bullish else "supply",
            "top": round(zt,2), "bottom": round(zb,2),
            "height_pct": round((zt-zb)/zb*100,2) if zb > 0 else 0,
            "strength": strength, "consecutive_large": consec,
            "bar_count": len(zone_bars), "start_idx": i,
            "date": str(bars[i].get("date",""))[:10],
        })
        i += len(zone_bars)
    return zones


# ═══════════════════════════════════════════════════════════
# VOID DETECTION — BODY DENSITY
# ═══════════════════════════════════════════════════════════

def detect_voids(bars, boxes, ticker):
    if not bars or len(bars) < 20: return []
    spot = bars[-1]["c"]
    if spot <= 0: return []
    bts = [_body_top(b) for b in bars]; bbs = [_body_bot(b) for b in bars]
    pmx = max(bts); pmn = min(bbs); pr = pmx - pmn
    if pr <= 0: return []
    bs = spot * 0.005; nb = min(int(pr / bs) + 1, 500)
    if nb <= 0: return []
    bs = pr / nb; density = [0] * nb
    for bar in bars:
        bt = _body_top(bar); bb = _body_bot(bar)
        for bi in range(nb):
            bl = pmn + bi * bs; bh = bl + bs
            if bt >= bl and bb <= bh: density[bi] += 1
    voids = []; inv = False; vs = 0
    for bi in range(nb):
        if density[bi] <= MAX_VOID_BAR_DENSITY:
            if not inv: inv = True; vs = bi
        else:
            if inv:
                vl = pmn + vs * bs; vh = pmn + bi * bs; vp = (vh - vl) / spot
                if vp >= MIN_VOID_PCT:
                    pos = "above" if vl > spot else "below"
                    adj = None
                    for bx in boxes:
                        if pos == "above" and abs(bx["roof"] - vl) / spot < 0.02: adj = bx; break
                        elif pos == "below" and abs(bx["floor"] - vh) / spot < 0.02: adj = bx; break
                    voids.append({"ticker": ticker.upper(), "low": round(vl,2), "high": round(vh,2),
                        "height": round(vh-vl,2), "height_pct": round(vp*100,2), "position": pos,
                        "adjacent_to_box": adj is not None,
                        "adjacent_box_roof": adj["roof"] if adj else None,
                        "adjacent_box_floor": adj["floor"] if adj else None})
                inv = False
    return voids


# ═══════════════════════════════════════════════════════════
# MATURITY
# ═══════════════════════════════════════════════════════════

def classify_maturity(box, historical_avg):
    d = box.get("duration_bars", 0); a = historical_avg if historical_avg > 0 else 15
    r = d / a if a > 0 else 1.0
    if r < MATURITY_EARLY_PCT: l, n = "early", f"Box young ({d} bars, avg {a:.0f}). Watchlist only."
    elif r < MATURITY_MID_PCT: l, n = "mid", f"Box maturing ({d} bars, avg {a:.0f}). Entry window."
    elif r < MATURITY_LATE_PCT: l, n = "late", f"Box mature ({d} bars, avg {a:.0f}). Breakout probable."
    else: l, n = "overdue", f"Box overdue ({d} bars, avg {a:.0f}). Breakout imminent."
    return {"maturity": l, "maturity_ratio": round(r,2), "duration_bars": d,
            "historical_avg": round(a,1), "suggested_dte": DTE_MAP.get(l), "note": n}


# ═══════════════════════════════════════════════════════════
# STRIKE SELECTION
# ═══════════════════════════════════════════════════════════

def select_strikes(chain_data, spot, direction, box, void_above, void_below, target_dte):
    sym_list = chain_data.get("optionSymbol") or []; n = len(sym_list)
    if n == 0: return None
    def col(name, default=None):
        v = chain_data.get(name, default)
        return v if isinstance(v, list) else [default] * n
    strikes=col("strike"); sides=col("side",""); bids=col("bid",0); asks=col("ask",0)
    mids=col("mid",0); ivs=col("iv",0); deltas=col("delta",0); ois=col("openInterest",0); dtes=col("dte",0)
    contracts = []
    for i in range(n):
        if strikes[i] is None: continue
        contracts.append({"symbol": sym_list[i], "strike": float(strikes[i]),
            "side": str(sides[i] or "").lower(), "bid": float(bids[i] or 0),
            "ask": float(asks[i] or 0), "mid": float(mids[i] or 0),
            "iv": float(ivs[i] or 0), "delta": float(deltas[i] or 0),
            "oi": int(ois[i] or 0), "dte": int(dtes[i] or 0)})
    calls = sorted([c for c in contracts if c["side"]=="call"], key=lambda c: c["strike"])
    puts = sorted([c for c in contracts if c["side"]=="put"], key=lambda c: c["strike"])
    if not calls or not puts: return None
    rf = box["roof"]; fl = box["floor"]

    if direction == "bullish":
        void = void_above
        if not void: return None
        vm = (void["low"] + void["high"]) / 2
        pp = [c for c in calls if rf*0.99 <= c["strike"] <= vm and c["mid"] >= 0.10 and c["ask"] > 0]
        if not pp: pp = [c for c in calls if c["strike"] > spot and c["mid"] >= 0.10 and c["ask"] > 0]
        if not pp: return None
        pp.sort(key=lambda c: abs(abs(c["delta"])-0.30)); primary = pp[0]
        hp = [p for p in puts if p["strike"] <= fl*1.01 and p["mid"] >= 0.05 and p["ask"] > 0]
        if not hp: hp = [p for p in puts if p["strike"] < spot and p["mid"] >= 0.05]
        if not hp: return None
        hp.sort(key=lambda c: abs(abs(c["delta"])-0.20)); hedge = hp[0]
        tp = void["high"]
    else:
        void = void_below
        if not void: return None
        vm = (void["low"] + void["high"]) / 2
        pp = [p for p in puts if vm <= p["strike"] <= fl*1.01 and p["mid"] >= 0.10 and p["ask"] > 0]
        if not pp: pp = [p for p in puts if p["strike"] < spot and p["mid"] >= 0.10]
        if not pp: return None
        pp.sort(key=lambda c: abs(abs(c["delta"])-0.30)); primary = pp[0]
        hp = [c for c in calls if c["strike"] >= rf*0.99 and c["mid"] >= 0.05 and c["ask"] > 0]
        if not hp: hp = [c for c in calls if c["strike"] > spot and c["mid"] >= 0.05]
        if not hp: return None
        hp.sort(key=lambda c: abs(abs(c["delta"])-0.20)); hedge = hp[0]
        tp = void["low"]

    pc = primary["ask"]*4*100; hc = hedge["ask"]*1*100; tc = pc + hc
    if direction == "bullish": intr = max(0, tp - primary["strike"])
    else: intr = max(0, primary["strike"] - tp)
    pat = intr*4*100 - tc; rr = pat / tc if tc > 0 else 0
    def fiv(v): return round(v*100,1) if v < 5 else round(v,1)
    return {
        "direction": direction,
        "structure": f"4:1 {'call' if direction=='bullish' else 'put'}/{'put' if direction=='bullish' else 'call'}",
        "primary": {"side": "call" if direction=="bullish" else "put", "strike": primary["strike"],
            "symbol": primary["symbol"], "qty": 4, "ask": primary["ask"], "bid": primary["bid"],
            "mid": primary["mid"], "iv": fiv(primary["iv"]), "delta": round(primary["delta"],3),
            "oi": primary["oi"], "dte": primary["dte"]},
        "hedge": {"side": "put" if direction=="bullish" else "call", "strike": hedge["strike"],
            "symbol": hedge["symbol"], "qty": 1, "ask": hedge["ask"], "bid": hedge["bid"],
            "mid": hedge["mid"], "iv": fiv(hedge["iv"]), "delta": round(hedge["delta"],3),
            "oi": hedge["oi"], "dte": hedge["dte"]},
        "total_cost": round(tc,2), "max_loss": round(tc,2), "target_price": round(tp,2),
        "profit_at_target": round(pat,2), "reward_risk": round(rr,2),
    }


# ═══════════════════════════════════════════════════════════
# SCANNER CLASS
# ═══════════════════════════════════════════════════════════

class PotterBoxScanner:
    def __init__(self, persistent_state, flow_detector=None, post_fn=None):
        self._state = persistent_state; self._flow = flow_detector; self._post = post_fn

    def _active_key(self, t): return f"potter_box:active:{t.upper()}"
    def _history_key(self, t, d): return f"potter_box:history:{t.upper()}:{d}"
    def _void_key(self, t): return f"potter_box:void:{t.upper()}"
    def _zone_key(self, t): return f"potter_box:zones:{t.upper()}"
    def _defaults_key(self, t): return f"potter_box:avg_duration:{t.upper()}"
    def _save(self, key, data, ttl): self._state._json_set(key, data, ttl)
    def _load(self, key): return self._state._json_get(key)
    def get_active_box(self, t): return self._load(self._active_key(t))
    def get_void_map(self, t): return self._load(self._void_key(t)) or []
    def get_zones(self, t): return self._load(self._zone_key(t)) or []

    def _log_completed_box(self, ticker, box):
        self._save(self._history_key(ticker, date.today().isoformat()), box, TTL_HISTORY_BOX)
        self._update_avg(ticker, box["duration_bars"])

    def _update_avg(self, ticker, new_dur):
        key = self._defaults_key(ticker); ex = self._load(key)
        cnt = (ex.get("count",0) if ex else 0) + 1; tot = (ex.get("total",0) if ex else 0) + new_dur
        self._save(key, {"count": cnt, "total": tot, "avg": round(tot/cnt,1)}, TTL_DEFAULTS)

    def get_avg_duration(self, ticker):
        d = self._load(self._defaults_key(ticker))
        if d and d.get("count",0) >= 2: return d["avg"]
        return DEFAULT_DURATION.get(_get_tier(ticker), 15)

    def scan_ticker(self, ticker, bars, chain_fn=None, spot_fn=None,
                    expirations_fn=None, iv_percentile_fn=None):
        ticker = ticker.upper()
        if not bars or len(bars) < 30: return None
        spot = bars[-1]["c"]
        if spot <= 0: return None

        all_boxes = detect_boxes(bars, ticker)
        if not all_boxes: return None
        active = [b for b in all_boxes if b["active"]]
        for cb in [b for b in all_boxes if b.get("break_confirmed")]:
            try: self._log_completed_box(ticker, cb)
            except: pass

        zones = detect_supply_demand_zones(bars, ticker)
        if zones: self._save(self._zone_key(ticker), zones, TTL_ZONE)
        voids = detect_voids(bars, all_boxes, ticker)
        self._save(self._void_key(ticker), voids, TTL_VOID_MAP)

        if not active: return None
        box = active[-1]; self._save(self._active_key(ticker), box, TTL_ACTIVE_BOX)
        avg_dur = self.get_avg_duration(ticker); mat = classify_maturity(box, avg_dur)
        if mat["maturity"] == "early": return None

        cb_sigs = detect_cb_gap_signals(bars, box)
        gap_out = detect_gap_outside(bars, box)
        pb = detect_punchback(bars, box)

        va = vb = None
        for v in voids:
            if v["position"] == "above" and (va is None or v["height_pct"] > va["height_pct"]): va = v
            elif v["position"] == "below" and (vb is None or v["height_pct"] > vb["height_pct"]): vb = v

        flow_dir = None; flow_ctx = {}
        if self._flow:
            try:
                for c in self._state.get_all_flow_campaigns(ticker):
                    s = c.get("strike", 0)
                    if abs(s - box["roof"]) / spot < 0.03:
                        if c.get("side") == "call" and "buildup" in c.get("flow_type",""): flow_dir = "bullish"; flow_ctx = c; break
                        elif c.get("side") == "put" and "unwinding" in c.get("flow_type",""): flow_dir = "bullish"; flow_ctx = c; break
                    if abs(s - box["floor"]) / spot < 0.03:
                        if c.get("side") == "put" and "buildup" in c.get("flow_type",""): flow_dir = "bearish"; flow_ctx = c; break
                        elif c.get("side") == "call" and "unwinding" in c.get("flow_type",""): flow_dir = "bearish"; flow_ctx = c; break
            except: pass

        iv_pct = None
        if iv_percentile_fn:
            try: iv_pct = iv_percentile_fn(ticker)
            except: pass

        setup = {"ticker": ticker, "box": box, "spot": round(spot,2),
            "void_above": va, "void_below": vb, "maturity": mat,
            "wave_label": box.get("wave_label","established"),
            "flow_direction": flow_dir, "flow_context": flow_ctx, "iv_percentile": iv_pct,
            "cb_gap_signals": cb_sigs[-1:] if cb_sigs else [], "gap_outside": gap_out,
            "punchback": pb,
            "supply_demand_zones": [z for z in zones if abs(z["top"]-spot)/spot < 0.15 or abs(z["bottom"]-spot)/spot < 0.15][:4],
            "scan_time": datetime.now().isoformat()}

        if flow_dir and chain_fn and expirations_fn:
            tdte = mat.get("suggested_dte", 21); vfd = va if flow_dir == "bullish" else vb
            if vfd and tdte:
                try:
                    exps = expirations_fn(ticker) or []; td = date.today(); be = None; bd = 999
                    for exp in exps:
                        try:
                            dte = (datetime.strptime(str(exp)[:10],"%Y-%m-%d").date() - td).days
                            if dte < 7: continue
                            if abs(dte - tdte) < bd: bd = abs(dte - tdte); be = exp
                        except: continue
                    if be:
                        chain = chain_fn(ticker, be)
                        if isinstance(chain, dict) and chain.get("s") == "ok":
                            trade = select_strikes(chain, spot, flow_dir, box, va, vb, tdte)
                            if trade: setup["trade"] = trade; setup["expiry"] = be
                except Exception as e: log.debug(f"Potter strike select failed {ticker}: {e}")
        return setup

    def scan_all(self, tickers, ohlcv_fn, chain_fn=None, spot_fn=None,
                 expirations_fn=None, iv_percentile_fn=None):
        setups = []
        for ticker in tickers:
            try:
                bars = ohlcv_fn(ticker)
                if not bars: continue
                if isinstance(bars, dict) and "close" in bars:
                    bl = []
                    cl = bars.get("close",[]); op = bars.get("open",cl); hi = bars.get("high",cl)
                    lo = bars.get("low",cl); vo = bars.get("volume",[0]*len(cl))
                    for idx in range(len(cl)):
                        bl.append({"date":"","o":op[idx],"h":hi[idx],"l":lo[idx],"c":cl[idx],
                                  "v":vo[idx] if idx < len(vo) else 0})
                    bars = bl
                s = self.scan_ticker(ticker, bars, chain_fn=chain_fn, spot_fn=spot_fn,
                                    expirations_fn=expirations_fn, iv_percentile_fn=iv_percentile_fn)
                if s: setups.append(s)
            except Exception as e: log.debug(f"Potter scan error {ticker}: {e}")
        log.info(f"Potter Box scan: {len(setups)} setups from {len(tickers)} tickers")
        return setups

    def format_alert(self, setup):
        t = setup["ticker"]; box = setup["box"]; mat = setup["maturity"]
        sp = setup["spot"]; va = setup.get("void_above"); vb = setup.get("void_below")
        fd = setup.get("flow_direction"); fc = setup.get("flow_context",{}); trade = setup.get("trade")
        wave = setup.get("wave_label","")
        me = {"mid":"📅","late":"⏰","overdue":"🚨"}.get(mat["maturity"],"📅")
        we = {"weakening":"⚡","breakout_probable":"🔥","breakout_imminent":"💥"}.get(wave,"")
        lines = [f"📦 POTTER BOX — {t}", "━"*28,
            f"Box: ${box['floor']:.2f} (floor) – ${box['roof']:.2f} (roof)",
            f"50% CB: ${box['midpoint']:.2f} (trapped volume battleground)",
            f"Range: {box['range_pct']:.1f}% | Touches: {box['roof_touches']}R / {box['floor_touches']}F",
            f"{me} Duration: {box['duration_bars']} bars (avg {mat['historical_avg']:.0f} — {mat['maturity'].upper()})"]
        if we: lines.append(f"{we} Wave: {wave.replace('_',' ').title()} (touch {box['max_touches']} — boundary eroding)")
        if va: lines.append(f"⬆️ Void Above: ${va['low']:.2f} → ${va['high']:.2f} ({va['height_pct']:.1f}%)")
        if vb: lines.append(f"⬇️ Void Below: ${vb['high']:.2f} → ${vb['low']:.2f} ({vb['height_pct']:.1f}%)")
        for z in setup.get("supply_demand_zones",[])[:2]:
            ze = "🟩" if z["type"]=="demand" else "🟥"
            lines.append(f"{ze} {z['type'].title()} zone: ${z['bottom']:.2f}–${z['top']:.2f} ({z['strength']})")
        if setup.get("cb_gap_signals"): lines.append(f"📊 CB Gap: {setup['cb_gap_signals'][-1]['note']}")
        if setup.get("punchback"): lines.append(f"🥊 Punch-back: {setup['punchback']['note']}")
        if setup.get("gap_outside"): lines.append(f"💨 {setup['gap_outside']['note']}")
        if fd:
            cd = fc.get("consecutive_days",0); co = fc.get("total_oi_change",0)
            lines.append(f"\n🏛️ Flow: {fd.upper()} ({cd}D campaign, {co:+,} OI)")
        ivp = setup.get("iv_percentile")
        if ivp is not None: lines.append(f"📊 IV Pctl: {ivp:.0f}% ({'compressed ✅' if ivp <= IV_PERCENTILE_MAX else 'elevated ⚠️'})")
        if trade:
            p = trade["primary"]; h = trade["hedge"]
            lines += ["", f"{'🟢' if fd=='bullish' else '🔴'} {trade['structure']}",
                f"  📗 {p['qty']}x ${p['strike']:.0f} {p['side'].upper()} @ ${p['ask']:.2f} (δ{p['delta']:.2f}, IV {p['iv']:.0f}%)",
                f"  📕 {h['qty']}x ${h['strike']:.0f} {h['side'].upper()} @ ${h['ask']:.2f} (δ{h['delta']:.2f}, IV {h['iv']:.0f}%)",
                f"  Exp: {setup.get('expiry','?')} ({p['dte']}D)",
                f"  Cost: ${trade['total_cost']:.2f} | Target: ${trade['target_price']:.2f} | R/R: {trade['reward_risk']:.1f}:1"]
        elif fd: lines.append(f"\nDirection: {fd.upper()} — awaiting chain for strikes")
        else: lines.append("\nNo flow bias — watchlist. Waiting for institutional positioning.")
        return "\n".join(lines)

    def format_summary(self, setups):
        if not setups: return ""
        lines = [f"📦 POTTER BOX — {len(setups)} setups", "━"*28]
        for s in setups:
            box = s["box"]; mat = s["maturity"]; fd = s.get("flow_direction"); trade = s.get("trade")
            wave = s.get("wave_label","")
            me = {"mid":"📅","late":"⏰","overdue":"🚨"}.get(mat["maturity"],"📅")
            ft = f"{'🟢' if fd=='bullish' else '🔴'} {fd.upper()}" if fd else "⚪ no bias"
            we = {"weakening":"⚡","breakout_probable":"🔥","breakout_imminent":"💥"}.get(wave,"")
            tt = f" | ${trade['total_cost']:.0f}, {trade['reward_risk']:.1f}:1" if trade else ""
            lines.append(f"  {me}{we} {s['ticker']} ${box['floor']:.0f}–${box['roof']:.0f} ({box['duration_bars']}D) | {ft}{tt}")
        return "\n".join(lines)
