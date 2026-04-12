# potter_box_fix.py
# ═══════════════════════════════════════════════════════════
# FIX: detect_boxes now splits boxes at breakout candles
#
# Problem: Greedy forward scan kept expanding the range until
# it hit 14%, absorbing breakout candles into the box.
# A box at $195-$205 would absorb breakout candles going to $220
# because the cumulative range stayed under 14%.
#
# Fix: After the initial range is seeded (MIN_BOX_BARS),
# detect breakout candles that would significantly expand the range.
# A bar that expands the established range by > 20% is a breakout,
# not part of the consolidation.
#
# Also: A bar whose body is > 2x the average body AND is directional
# (extends one boundary without touching the other) = breakout candle.
# ═══════════════════════════════════════════════════════════

import sys
sys.path.insert(0, "/home/claude")

from potter_box import (
    _body_top, _body_bot, _body_size, _get_tier,
    MAX_RANGE_PCT, MIN_BOX_BARS, TOUCH_ZONE_PCT, WAVE_LABELS,
    BREAKOUT_CONFIRM_PCT, PUNCHBACK_MAX_BARS, PUNCHBACK_MIN_BARS,
    LARGE_CANDLE_ATR_MULT
)

# New constant: after range is seeded, a single bar can expand the range
# by at most this fraction before it's considered a breakout
RANGE_EXPANSION_LIMIT = 0.20  # 20% of current range


def detect_boxes_fixed(bars, ticker):
    """
    Improved box detection that finds ALL historical boxes.
    
    Key fix: after MIN_BOX_BARS establish the range, any bar that:
      1. Expands the range by > 20% of current range, OR
      2. Has body > 2x average body AND extends only one boundary
    ...is a breakout candle. The box ends at the bar BEFORE it.
    
    This prevents breakout candles from being absorbed into boxes,
    allowing the scanner to find the NEXT box after the breakout.
    """
    if not bars or len(bars) < MIN_BOX_BARS + 2:
        return []
    
    max_range = MAX_RANGE_PCT.get(_get_tier(ticker), 0.14)
    n = len(bars)
    boxes = []
    i = 0
    
    while i < n - MIN_BOX_BARS:
        box_start = i
        running_high = _body_top(bars[i])
        running_low = _body_bot(bars[i])
        body_sum = _body_size(bars[i])
        body_count = 1
        
        j = i + 1
        while j < n:
            bt = _body_top(bars[j])
            bb = _body_bot(bars[j])
            body = _body_size(bars[j])
            
            ch = max(running_high, bt)
            cl = min(running_low, bb)
            mid = (ch + cl) / 2
            if mid <= 0:
                break
            
            # Standard range check
            if (ch - cl) / mid > max_range:
                break
            
            # ── BREAKOUT CANDLE DETECTION (after range is seeded) ──
            bars_in_box = j - box_start
            if bars_in_box >= MIN_BOX_BARS:
                current_range = running_high - running_low
                avg_body = body_sum / body_count if body_count > 0 else 1
                
                if current_range > 0:
                    # Check 1: Does this bar expand the range too much?
                    range_expansion = (ch - cl) - current_range
                    expansion_ratio = range_expansion / current_range
                    
                    # Check 2: Is this an outsized directional candle?
                    is_large = body > avg_body * LARGE_CANDLE_ATR_MULT
                    extends_high = bt > running_high  # pushing roof up
                    extends_low = bb < running_low     # pushing floor down
                    is_directional = extends_high != extends_low  # only one side
                    
                    # Breakout if:
                    # - Range expands by > 20% from one bar, OR
                    # - Large directional candle that extends boundary
                    if expansion_ratio > RANGE_EXPANSION_LIMIT:
                        break
                    if is_large and is_directional and (extends_high or extends_low):
                        # Only break if it actually extends the range meaningfully
                        if range_expansion > current_range * 0.05:
                            break
            
            running_high = ch
            running_low = cl
            body_sum += body
            body_count += 1
            j += 1
        
        box_end = j - 1
        box_bars = box_end - box_start + 1
        
        if box_bars >= MIN_BOX_BARS:
            roof = running_high
            floor = running_low
            rz = roof * TOUCH_ZONE_PCT
            fz = floor * TOUCH_ZONE_PCT
            
            # Count touches
            rt = 0
            ft = 0
            for k in range(box_start, box_end + 1):
                bt_k = _body_top(bars[k])
                bb_k = _body_bot(bars[k])
                if abs(bt_k - roof) <= rz or bars[k]["h"] > roof * (1 + TOUCH_ZONE_PCT * 0.5):
                    rt += 1
                if abs(bb_k - floor) <= fz or bars[k]["l"] < floor * (1 - TOUCH_ZONE_PCT * 0.5):
                    ft += 1
            
            if rt >= 2 and ft >= 2:
                midpoint = (roof + floor) / 2
                rp = (roof - floor) / midpoint * 100 if midpoint > 0 else 0
                mt = max(rt, ft)
                wl = WAVE_LABELS.get(min(mt, 5), "breakout_imminent" if mt >= 5 else "established")
                
                # Check if still active (last bar is inside box)
                lb = bars[-1]
                still_active = (_body_bot(lb) >= floor * (1 - TOUCH_ZONE_PCT) and 
                                _body_top(lb) <= roof * (1 + TOUCH_ZONE_PCT))
                
                # Breakout detection (post-box)
                broken = False
                break_dir = None
                confirmed = False
                break_idx = None
                pb = False
                
                if not still_active and box_end < n - 1:
                    for k in range(box_end + 1, n):
                        bc = bars[k]["c"]
                        if bc > roof * (1 + BREAKOUT_CONFIRM_PCT):
                            broken = True; confirmed = True; break_dir = "up"; break_idx = k; break
                        elif bc < floor * (1 - BREAKOUT_CONFIRM_PCT):
                            broken = True; confirmed = True; break_dir = "down"; break_idx = k; break
                        elif bc > roof * (1 + TOUCH_ZONE_PCT) and not broken:
                            broken = True; break_dir = "up"; break_idx = k
                        elif bc < floor * (1 - TOUCH_ZONE_PCT) and not broken:
                            broken = True; break_dir = "down"; break_idx = k
                    
                    # Punchback detection
                    if broken and break_idx and not confirmed:
                        bars_out = 0
                        for k in range(break_idx, min(break_idx + PUNCHBACK_MAX_BARS + 1, n)):
                            bc2 = bars[k]["c"]
                            if floor * (1 - TOUCH_ZONE_PCT) <= bc2 <= roof * (1 + TOUCH_ZONE_PCT):
                                if bars_out >= PUNCHBACK_MIN_BARS:
                                    pb = True
                                break
                            bars_out += 1
                
                # Run distance
                rd = 0
                if confirmed and break_idx:
                    if break_dir == "up":
                        rd = max(b["h"] for b in bars[break_idx:]) - roof
                    else:
                        rd = floor - min(b["l"] for b in bars[break_idx:])
                
                boxes.append({
                    "ticker": ticker.upper(),
                    "roof": round(roof, 2), "floor": round(floor, 2),
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
        else:
            i += 1
    
    return boxes
