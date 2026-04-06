#!/usr/bin/env python3
"""
active_backtest.py — Active Scanner Backtest (1-5 DTE holds)

Signals come from intraday 5-min bars (EMA/MACD/WaveTrend/RSI/VWAP).
Exits are measured at daily closes: EOD, +1d, +2d, +3d, +5d.

This matches how the scanner is actually used: signal fires intraday,
a 1-5 DTE options spread is entered, held for 1-5 trading days.

Usage:
  python backtest/active_backtest.py --ticker NVDA
  python backtest/active_backtest.py --ticker NVDA --from 2025-07-01 --to 2026-04-01
  python backtest/active_backtest.py --ticker NVDA --days 90
"""

import os, sys, csv, json, time, argparse, requests
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ─── Config (mirrors active_scanner.py exactly) ──────────────────────────────
EMA_FAST = 5; EMA_SLOW = 12
MACD_FAST = 12; MACD_SLOW = 26; MACD_SIGNAL_P = 9
RSI_PERIOD = 14; WT_CHANNEL = 10; WT_AVERAGE = 21
SIGNAL_TIER_1_SCORE = 75; MIN_SIGNAL_SCORE = 50
BARS_LOOKBACK = 80; DEDUP_BARS = 3
MARKET_OPEN_CT = (8, 30); MARKET_CLOSE_CT = (15, 0)
EXIT_DAYS = {"eod": 0, "1d": 1, "2d": 2, "3d": 3, "5d": 5}
TOKEN = os.getenv("MARKETDATA_TOKEN", "").strip()


# ─── MarketData.app ───────────────────────────────────────────────────────────
def _md_get(url, params):
    if not TOKEN:
        sys.exit("ERROR: MARKETDATA_TOKEN not set")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    delay = 15
    for attempt in range(1, 5):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code in (429, 403):
                wait = max(int(r.headers.get("Retry-After", delay)), delay)
                if attempt < 4:
                    print(f"  [{r.status_code}] waiting {wait}s…"); time.sleep(wait)
                    delay = min(delay * 2, 120); continue
                r.raise_for_status()
            r.raise_for_status(); return r.json()
        except requests.exceptions.Timeout:
            if attempt < 4:
                time.sleep(delay); delay = min(delay * 2, 120)
            else:
                sys.exit("ERROR: timeout")
    sys.exit("ERROR: max retries")


def _parse_ts(ts) -> datetime:
    """
    Convert a MarketData.app timestamp to a CT-aware datetime.

    Handles all formats the API returns:
      - int / float        → Unix timestamp
      - numeric string     → float() then Unix timestamp
      - 'YYYY-MM-DD HH:MM:SS ±HH:MM'  → ISO with colon offset (strip colon first)
      - 'YYYY-MM-DDTHH:MM:SS±HH:MM'   → ISO T-format
      - 'YYYY-MM-DD'                   → bare date (treated as noon UTC to avoid date shift)
    """
    CT_OFFSET = timezone(timedelta(hours=-5))

    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CT_OFFSET)

    ts_str = str(ts).strip()

    # Numeric string ('1728403500' or '1728403500.0')
    try:
        return datetime.fromtimestamp(float(ts_str), tz=timezone.utc).astimezone(CT_OFFSET)
    except (ValueError, OSError):
        pass

    # ISO string with colon in UTC offset: '2025-10-08 09:30:00 -04:00'
    # strptime %z handles +HHMM but not +HH:MM — strip the colon
    if len(ts_str) >= 6 and ts_str[-3] == ':' and ts_str[-6] in ('+', '-'):
        ts_str = ts_str[:-3] + ts_str[-2:]

    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CT_OFFSET)
        except ValueError:
            continue

    # Bare date string: '2025-12-19' — use noon UTC to avoid CT date-shift
    try:
        dt = datetime.strptime(ts_str[:10], "%Y-%m-%d")
        dt = dt.replace(hour=12, tzinfo=timezone.utc)
        return dt.astimezone(CT_OFFSET)
    except ValueError:
        pass

    raise ValueError(f"Cannot parse timestamp: {ts!r}")


def _parse_date_str(ts) -> str:
    """
    Extract just the YYYY-MM-DD trading date from any timestamp format.
    For daily bars: the API returns the date as-is ('2025-12-19') — use it directly.
    For other formats: extract the date portion without timezone conversion so the
    trading date is preserved regardless of the server's local timezone.
    """
    ts_str = str(ts).strip()

    # Bare date already — the daily bars endpoint returns these directly
    if len(ts_str) == 10 and ts_str[4] == '-' and ts_str[7] == '-':
        return ts_str

    # Datetime string — first 10 chars are always the date
    if len(ts_str) > 10 and ts_str[4] == '-' and ts_str[7] == '-':
        return ts_str[:10]

    # Unix timestamp — convert via _parse_ts and take the CT date
    try:
        return _parse_ts(ts).strftime("%Y-%m-%d")
    except Exception:
        raise ValueError(f"Cannot parse date from: {ts!r}")


def _ts_sort_key(ts) -> int:
    """Return a sortable integer from any timestamp format."""
    try:
        return int(_parse_ts(ts).timestamp())
    except Exception:
        return 0


def download_5min(ticker, from_date, to_date):
    url = f"https://api.marketdata.app/v1/stocks/candles/5/{ticker}/"
    params = {"from": from_date, "to": to_date, "dateformat": "timestamp"}
    print(f"  5-min bars {ticker} {from_date}→{to_date}…")
    data = _md_get(url, params)
    if data.get("s") != "ok":
        print(f"  WARNING: status={data.get('s')}"); return []
    bars = []
    for i, ts in enumerate(data.get("t", [])):
        try:
            dt_ct = _parse_ts(ts)
        except ValueError as e:
            print(f"  WARNING: skipping bar with unparseable timestamp {ts!r}: {e}")
            continue
        bars.append({
            "ts":      _ts_sort_key(ts),
            "date":    dt_ct.strftime("%Y-%m-%d"),
            "time_ct": dt_ct.strftime("%H:%M"),
            "o": data["o"][i] if i < len(data.get("o", [])) else None,
            "h": data["h"][i] if i < len(data.get("h", [])) else None,
            "l": data["l"][i] if i < len(data.get("l", [])) else None,
            "c": data["c"][i] if i < len(data.get("c", [])) else None,
            "v": data["v"][i] if i < len(data.get("v", [])) else 0,
        })
    bars = [b for b in bars if b["c"] is not None]
    bars.sort(key=lambda b: b["ts"])
    print(f"  → {len(bars)} bars"); return bars


def download_daily(ticker, from_date, to_date):
    url = f"https://api.marketdata.app/v1/stocks/candles/D/{ticker}/"
    params = {"from": from_date, "to": to_date, "dateformat": "timestamp"}
    print(f"  Daily bars {ticker} {from_date}→{to_date}…")
    try:
        data = _md_get(url, params)
        if data.get("s") != "ok":
            print(f"  WARNING: status={data.get('s')}"); return []
        bars = []
        for i, ts in enumerate(data.get("t", [])):
            try:
                # Use _parse_date_str — preserves the trading date directly
                # without timezone conversion (daily bars return bare dates)
                date_str = _parse_date_str(ts)
            except ValueError as e:
                print(f"  WARNING: skipping daily bar {ts!r}: {e}")
                continue
            bars.append({
                "date": date_str,
                "c": data["c"][i] if i < len(data.get("c", [])) else None,
            })
        bars = [b for b in bars if b["c"] is not None]
        bars.sort(key=lambda b: b["date"])
        print(f"  → {len(bars)} daily bars"); return bars
    except Exception as e:
        print(f"  WARNING: daily bars failed: {e}"); return []


# ─── Indicators (exact copy from active_scanner.py) ──────────────────────────
def _ema(values, period):
    if len(values) < period: return []
    ema = [sum(values[:period]) / period]
    mult = 2.0 / (period + 1)
    for v in values[period:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema

def _rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains = [max(0, closes[i]-closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i-1]-closes[i]) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100.0 if al == 0 else 100.0 - (100.0 / (1 + ag / al))

def _macd(closes):
    if len(closes) < MACD_SLOW + MACD_SIGNAL_P: return {}
    ef = _ema(closes, MACD_FAST); es = _ema(closes, MACD_SLOW)
    offset = MACD_SLOW - MACD_FAST
    ml = [ef[i+offset] - es[i] for i in range(len(es))]
    if len(ml) < MACD_SIGNAL_P: return {}
    sig = _ema(ml, MACD_SIGNAL_P)
    return {
        "macd_hist": ml[-1] - sig[-1] if sig else 0,
        "macd_cross_bull": (len(ml)>=2 and len(sig)>=2 and ml[-2]<sig[-2] and ml[-1]>sig[-1]) if sig else False,
        "macd_cross_bear": (len(ml)>=2 and len(sig)>=2 and ml[-2]>sig[-2] and ml[-1]<sig[-1]) if sig else False,
    }

def _wavetrend(hlc3):
    if len(hlc3) < WT_AVERAGE + WT_CHANNEL + 4: return {}
    esa = _ema(hlc3, WT_CHANNEL)
    if not esa: return {}
    offset = len(hlc3) - len(esa)
    d_series = [abs(hlc3[i+offset] - esa[i]) for i in range(len(esa))]
    de = _ema(d_series, WT_CHANNEL)
    if not de: return {}
    o2 = len(d_series) - len(de)
    ci = [(hlc3[i+offset+o2] - esa[i+o2]) / (0.015 * de[i]) if de[i] != 0 else 0
          for i in range(len(de))]
    wt1 = _ema(ci, WT_AVERAGE)
    if not wt1 or len(wt1) < 4: return {}
    wt2 = _ema(wt1, 4)
    if not wt2: return {}
    return {
        "wt2": wt2[-1],
        "wt_oversold": wt2[-1] < -30, "wt_overbought": wt2[-1] > 60,
        "wt_cross_bull": (len(wt1)>=2 and len(wt2)>=2 and wt1[-2]<wt2[-2] and wt1[-1]>wt2[-1]),
        "wt_cross_bear": (len(wt1)>=2 and len(wt2)>=2 and wt1[-2]>wt2[-2] and wt1[-1]<wt2[-1]),
    }


# ─── Signal detection ─────────────────────────────────────────────────────────
def detect_signal(window, daily_closes):
    if len(window) < 12: return None
    closes  = [b["c"] for b in window]
    highs   = [b["h"] for b in window if b.get("h") is not None]
    lows    = [b["l"] for b in window if b.get("l") is not None]
    volumes = [b.get("v", 0) or 0 for b in window]
    spot    = closes[-1]
    bc      = len(closes)
    dq      = "full" if bc>=40 else "partial" if bc>=20 else "minimal"

    # ADTV gate
    if len(volumes) >= 10:
        avg10 = sum(volumes[-10:]) / 10
        if avg10 * spot * 5 * 60 < 5_000_000: return None

    # VWAP
    vwap = None
    ml = min(len(closes), len(highs), len(lows), len(volumes))
    if ml > 0:
        tpv = sum((highs[i]+lows[i]+closes[i])/3*volumes[i] for i in range(ml) if volumes[i]>0)
        vs  = sum(v for v in volumes[:ml] if v > 0)
        if vs > 0: vwap = tpv / vs

    # EMA
    ema5 = _ema(closes, EMA_FAST); ema12 = _ema(closes, EMA_SLOW)
    if not ema5 or not ema12: return None
    ema_bull = ema5[-1] > ema12[-1]
    ema_dist = ((ema5[-1]-ema12[-1])/ema12[-1])*100 if ema12[-1] > 0 else 0

    macd = _macd(closes)
    hlc3 = [(highs[i]+lows[i]+closes[i])/3 for i in range(ml)]
    wt   = _wavetrend(hlc3)
    rsi  = _rsi(closes, RSI_PERIOD)

    avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    # Daily HTF
    daily_bull = htf_conf = htf_conv = False
    htf_status = "UNKNOWN"
    if daily_closes and len(daily_closes) >= 21:
        de8 = _ema(daily_closes, 8); de21 = _ema(daily_closes, 21)
        if de8 and de21 and len(de8) >= 2:
            daily_bull = de8[-1] > de21[-1]
            htf_conf   = daily_bull == ema_bull
            if htf_conf:
                htf_status = "CONFIRMED"
            else:
                gn = abs(de8[-1]-de21[-1]); gp = abs(de8[-2]-de21[-2])
                if gn < gp * 0.98:
                    htf_conv = True; htf_status = "CONVERGING"
                else:
                    htf_status = "OPPOSING"

    bias  = "bull" if ema_bull else "bear"
    score = 0; bd = {}

    if abs(ema_dist) > 0.03:   score += 15; bd["ema"] = 15
    elif abs(ema_dist) > 0.01: score += 8;  bd["ema"] = 8
    else:                                   bd["ema"] = 0

    if macd:
        h = macd.get("macd_hist", 0)
        if bias=="bull" and h>0:   score+=15; bd["macd_hist"]=15
        elif bias=="bear" and h<0: score+=15; bd["macd_hist"]=15
        elif h!=0:                 score-=10; bd["macd_hist"]=-10
        else:                                 bd["macd_hist"]=0
        if macd.get("macd_cross_bull") and bias=="bull": score+=10; bd["macd_cross"]=10
        elif macd.get("macd_cross_bear") and bias=="bear": score+=10; bd["macd_cross"]=10
        else: bd["macd_cross"]=0
    else:
        bd["macd_hist"]=0; bd["macd_cross"]=0

    if wt:
        if bias=="bull" and wt.get("wt_oversold"):    score+=15; bd["wt"]=15
        elif bias=="bear" and wt.get("wt_overbought"):score+=15; bd["wt"]=15
        elif bias=="bull" and wt.get("wt_overbought"):score-=10; bd["wt"]=-10
        elif bias=="bear" and wt.get("wt_oversold"):  score-=10; bd["wt"]=-10
        elif bias=="bull" and wt.get("wt_cross_bull"):score+=10; bd["wt"]=10
        elif bias=="bear" and wt.get("wt_cross_bear"):score+=10; bd["wt"]=10
        else:                                                     bd["wt"]=0
    else: bd["wt"]=0

    if vwap:
        if bias=="bull" and spot>vwap:   score+=10; bd["vwap"]=10
        elif bias=="bear" and spot<vwap: score+=10; bd["vwap"]=10
        elif bias=="bull" and spot<vwap: score-=5;  bd["vwap"]=-5
        elif bias=="bear" and spot>vwap: score-=5;  bd["vwap"]=-5
    else: bd["vwap"]=0

    if htf_conf: score+=15; bd["htf"]=15
    elif daily_bull is not False:
        if (bias=="bull" and daily_bull) or (bias=="bear" and not daily_bull):
            score+=10; bd["htf"]=10
        else: score-=10; bd["htf"]=-10
    else: bd["htf"]=0

    if vol_ratio>1.5:   score+=10; bd["volume"]=10
    elif vol_ratio>1.0: score+=5;  bd["volume"]=5
    else:                          bd["volume"]=0

    if rsi:
        if bias=="bull" and 40<rsi<65:   score+=5; bd["rsi"]=5
        elif bias=="bear" and 35<rsi<60: score+=5; bd["rsi"]=5
        else:                                       bd["rsi"]=0
    else: bd["rsi"]=0

    if score < MIN_SIGNAL_SCORE: return None

    return {
        "bias": bias, "tier": "1" if score>=SIGNAL_TIER_1_SCORE else "2",
        "score": score, "score_breakdown": bd, "data_quality": dq,
        "bar_count": bc, "close": spot, "ema_dist_pct": round(ema_dist, 3),
        "macd_hist": macd.get("macd_hist", 0),
        "macd_cross_bull": macd.get("macd_cross_bull", False),
        "macd_cross_bear": macd.get("macd_cross_bear", False),
        "wt2": wt.get("wt2", 0),
        "wt_cross_bull": wt.get("wt_cross_bull", False),
        "wt_cross_bear": wt.get("wt_cross_bear", False),
        "rsi": rsi, "vwap": vwap,
        "above_vwap": spot>vwap if vwap else None,
        "htf_status": htf_status, "htf_confirmed": htf_conf,
        "htf_converging": htf_conv, "daily_bull": daily_bull,
        "volume_ratio": round(vol_ratio, 2),
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────
def is_market_bar(t):
    try:
        h, m = int(t[:2]), int(t[3:5]); tm = h*60+m
        return MARKET_OPEN_CT[0]*60+MARKET_OPEN_CT[1] <= tm < MARKET_CLOSE_CT[0]*60+MARKET_CLOSE_CT[1]
    except: return True

def time_phase(t):
    try:
        h, m = int(t[:2]), int(t[3:5]); tm = h*60+m
        o = MARKET_OPEN_CT[0]*60+MARKET_OPEN_CT[1]
        return "MORNING" if tm < o+90 else "MIDDAY" if tm < o+210 else "AFTERNOON"
    except: return "UNKNOWN"

def exit_dates_for(ref_date, sorted_dates):
    """Return {label: date} for each exit horizon."""
    try: idx = sorted_dates.index(ref_date)
    except ValueError:
        idx = next((j for j, d in enumerate(sorted_dates) if d >= ref_date), None)
        if idx is None: return {k: None for k in EXIT_DAYS}
    return {label: sorted_dates[idx+n] if idx+n < len(sorted_dates) else None
            for label, n in EXIT_DAYS.items()}


# ─── Walk-forward backtest ────────────────────────────────────────────────────
def run_backtest(ticker, intraday_bars, daily_bars):
    """
    Walk every 5-min bar, detect signals, evaluate at daily closes for 1-5d holds.
    Entry = close of bar where signal fires.
    Exits = daily close at EOD, +1d, +2d, +3d, +5d.
    """
    trades = []
    n = len(intraday_bars)

    daily_close_map = {b["date"]: b["c"] for b in daily_bars}
    daily_dates_all = [b["date"] for b in daily_bars]
    daily_closes_all = [b["c"] for b in daily_bars]
    sorted_td = sorted(set(daily_dates_all))

    # HTF closes available at each date: all closes strictly before that date
    htf_map = {}
    for i, d in enumerate(daily_dates_all):
        htf_map[d] = daily_closes_all[:i]

    dedup = {}  # bias → last bar index
    print(f"  Walking {n} 5-min bars…")

    for i in range(BARS_LOOKBACK, n):
        bar = intraday_bars[i]
        if not is_market_bar(bar["time_ct"]): continue

        window    = intraday_bars[max(0, i-BARS_LOOKBACK+1): i+1]
        bar_date  = bar["date"]
        daily_htf = htf_map.get(bar_date, [])

        sig = detect_signal(window, daily_htf)
        if not sig: continue

        if i - dedup.get(sig["bias"], -999) < DEDUP_BARS: continue
        dedup[sig["bias"]] = i

        entry = bar["c"]
        xdates = exit_dates_for(bar_date, sorted_td)

        # Intraday MFE/MAE on entry day
        mfe = mae = 0.0
        for b in intraday_bars[i:]:
            if b["date"] != bar_date or not is_market_bar(b["time_ct"]): break
            if b["h"] and b["l"]:
                if sig["bias"] == "bull":
                    mfe = max(mfe, b["h"] - entry); mae = min(mae, b["l"] - entry)
                else:
                    mfe = max(mfe, entry - b["l"]); mae = min(mae, entry - b["h"])

        trade = {
            "ticker": ticker, "entry_date": bar_date,
            "entry_time_ct": bar["time_ct"], "phase": time_phase(bar["time_ct"]),
            "bias": sig["bias"], "tier": sig["tier"], "score": sig["score"],
            "htf_status": sig["htf_status"], "htf_confirmed": sig["htf_confirmed"],
            "data_quality": sig["data_quality"], "entry_price": round(entry, 4),
            "ema_dist_pct": sig["ema_dist_pct"],
            "macd_hist": round(sig["macd_hist"], 4),
            "macd_cross": sig["macd_cross_bull"] or sig["macd_cross_bear"],
            "wt2": round(sig["wt2"], 2),
            "wt_cross": sig["wt_cross_bull"] or sig["wt_cross_bear"],
            "rsi": round(sig["rsi"], 1) if sig["rsi"] else None,
            "above_vwap": sig["above_vwap"],
            "volume_ratio": sig["volume_ratio"],
            "mfe_eod_pts": round(mfe, 4), "mae_eod_pts": round(mae, 4),
            "score_bd": json.dumps(sig["score_breakdown"]),
        }

        for label in EXIT_DAYS:
            xd = xdates.get(label)
            xc = daily_close_map.get(xd) if xd else None
            if xc is not None:
                pnl = (xc - entry) if sig["bias"]=="bull" else (entry - xc)
                pnl_pct = pnl / entry * 100
                win = pnl > 0
            else:
                pnl = pnl_pct = win = None
            trade[f"exit_date_{label}"]  = xd
            trade[f"exit_price_{label}"] = round(xc, 4) if xc else None
            trade[f"pnl_{label}"]        = round(pnl, 4) if pnl is not None else None
            trade[f"pnl_pct_{label}"]    = round(pnl_pct, 4) if pnl_pct is not None else None
            trade[f"win_{label}"]        = win

        trades.append(trade)

    return trades


# ─── Summary ──────────────────────────────────────────────────────────────────
def _stats(trades, col):
    valid = [t for t in trades if t.get(f"win_{col}") is not None]
    if not valid: return {"n": 0, "wr": 0.0, "avg_pct": 0.0, "pf": 0.0}
    wins = [t for t in valid if t[f"win_{col}"]]
    gw = sum(t[f"pnl_pct_{col}"] for t in wins)
    gl = sum(t[f"pnl_pct_{col}"] for t in valid if not t[f"win_{col}"])
    pf = abs(gw/gl) if gl < 0 else float("inf")
    return {"n": len(valid), "wr": len(wins)/len(valid)*100,
            "avg_pct": sum(t[f"pnl_pct_{col}"] for t in valid)/len(valid),
            "pf": round(pf, 2)}


def write_summary(ticker, trades, out_dir):
    if not trades: print(f"  No signals for {ticker}"); return
    lines = []; SEP = "="*62
    def h(s): lines.append(f"\n{SEP}\n  {s}\n{SEP}")
    def r(l, v): lines.append(f"  {l:<30} {v}")

    h(f"ACTIVE SCANNER BACKTEST (1-5 DTE) — {ticker}")
    r("Total signals:", len(trades))
    r("Date range:", f"{trades[0]['entry_date']} → {trades[-1]['entry_date']}")
    r("Unique trading days:", len(set(t["entry_date"] for t in trades)))
    r("Avg signals / day:", f"{len(trades)/max(1,len(set(t['entry_date'] for t in trades))):.1f}")

    h("WIN RATE BY EXIT HORIZON")
    r("Horizon", "n      Win%    Avg P&L%    PF")
    lines.append("  " + "-"*46)
    for label in EXIT_DAYS:
        s = _stats(trades, label)
        if s["n"]:
            r(f"{label}", f"{s['n']:>4}   {s['wr']:>5.1f}%   {s['avg_pct']:>+7.3f}%   {s['pf']}")

    h("BY TIER")
    for tier in ["1", "2"]:
        sub = [t for t in trades if t["tier"]==tier]
        if not sub: continue
        lines.append(f"\n  T{tier} — {len(sub)} signals")
        for label in EXIT_DAYS:
            s = _stats(sub, label)
            if s["n"]: r(f"  {label}", f"{s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    h("BY HTF STATUS  ← most diagnostic cut")
    for status in ["CONFIRMED","CONVERGING","OPPOSING","UNKNOWN"]:
        sub = [t for t in trades if t["htf_status"]==status]
        if not sub: continue
        lines.append(f"\n  {status} — {len(sub)} signals ({len(sub)/len(trades)*100:.0f}%)")
        for label in ["eod","1d","2d","3d"]:
            s = _stats(sub, label)
            if s["n"]: r(f"  {label}", f"{s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    h("BY SCORE BUCKET")
    for lo, hi, label in [(50,59,"50-59 marginal T2"),(60,74,"60-74 solid T2"),(75,999,"75+ T1")]:
        sub = [t for t in trades if lo<=t["score"]<=hi]
        if not sub: continue
        lines.append(f"\n  {label} — {len(sub)} signals")
        for h_ in ["1d","3d"]:
            s = _stats(sub, h_)
            if s["n"]: r(f"  {h_}", f"{s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    h("BY TIME OF DAY")
    for phase in ["MORNING","MIDDAY","AFTERNOON"]:
        sub = [t for t in trades if t["phase"]==phase]
        if not sub: continue
        lines.append(f"\n  {phase} — {len(sub)} signals")
        for h_ in ["eod","1d","3d"]:
            s = _stats(sub, h_)
            if s["n"]: r(f"  {h_}", f"{s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    h("BY DIRECTION")
    for bias in ["bull","bear"]:
        sub = [t for t in trades if t["bias"]==bias]
        if not sub: continue
        lines.append(f"\n  {bias.upper()} — {len(sub)} signals")
        for h_ in ["eod","1d","3d"]:
            s = _stats(sub, h_)
            if s["n"]: r(f"  {h_}", f"{s['wr']:.1f}% win  avg {s['avg_pct']:+.3f}%  PF {s['pf']}")

    h("INTRADAY MFE/MAE ON ENTRY DAY  (stop sizing context)")
    mfe_v = [t["mfe_eod_pts"] for t in trades if t["mfe_eod_pts"] is not None]
    mae_v = [abs(t["mae_eod_pts"]) for t in trades if t["mae_eod_pts"] is not None]
    if mfe_v:
        r("Avg MFE entry day:", f"${sum(mfe_v)/len(mfe_v):.3f}")
        r("Avg MAE entry day:", f"-${sum(mae_v)/len(mae_v):.3f}")
        r("MFE/MAE ratio:", f"{sum(mfe_v)/max(sum(mae_v),0.001):.2f}x")

    h("SCORE COMPONENTS")
    for comp in ["ema","macd_hist","macd_cross","wt","vwap","htf","volume","rsi"]:
        pos = sum(1 for t in trades if json.loads(t["score_bd"]).get(comp,0)>0)
        neg = sum(1 for t in trades if json.loads(t["score_bd"]).get(comp,0)<0)
        r(f"{comp}:", f"+{pos} ({pos/len(trades)*100:.0f}%)  -{neg} ({neg/len(trades)*100:.0f}%)")

    lines.append(f"\n{SEP}\n")
    txt = "\n".join(lines)
    p = out_dir / f"active_summary_{ticker}.txt"
    p.write_text(txt)
    print(f"\n  Summary → {p}"); print(txt)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--from", dest="from_date", default=None)
    ap.add_argument("--to",   dest="to_date",   default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    ticker = args.ticker.upper()
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    if args.days:
        to_date = today; from_date = (date.today() - timedelta(days=args.days)).isoformat()
    else:
        from_date = args.from_date or (date.today() - timedelta(days=90)).isoformat()
        to_date   = args.to_date   or today

    daily_from = (datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")

    print(f"\n{'='*62}")
    print(f"  ACTIVE BACKTEST (1-5 DTE holds) — {ticker}")
    print(f"  Signals: {from_date} → {to_date}")
    print(f"  Exits:   EOD · +1d · +2d · +3d · +5d (daily closes)")
    print(f"{'='*62}\n")

    intraday = download_5min(ticker, from_date, to_date)
    daily    = download_daily(ticker, daily_from, to_date)

    if not intraday: print("ERROR: no 5-min bars"); sys.exit(1)
    if not daily:    print("ERROR: no daily bars");  sys.exit(1)

    intraday = [b for b in intraday if is_market_bar(b["time_ct"])]
    print(f"  Market-hours bars: {len(intraday)}  |  Daily bars: {len(daily)}")

    trades = run_backtest(ticker, intraday, daily)
    print(f"\n  Signals fired: {len(trades)}")

    if not trades: print("No signals. Try longer range."); sys.exit(0)

    csv_path = out_dir / f"active_trades_{ticker}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader(); w.writerows(trades)
    print(f"  Trades CSV → {csv_path}")
    write_summary(ticker, trades, out_dir)


if __name__ == "__main__":
    main()
