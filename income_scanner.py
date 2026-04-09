# income_scanner.py
# ═══════════════════════════════════════════════════════════════════
# Income Trade Scanner — Fully Automated Scorecard v5
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v5.0 — April 8, 2026
# All inputs auto-computed — zero manual parameters:
#   - Event risk: FMP earnings calendar (primary) + yfinance (fallback)
#                 + economic_calendar.py for FOMC/CPI/NFP/GDP
#                 + FMP sector/industry for gap risk classification
#                 + regime event overlay (WAR_CRISIS, MACRO_SHOCK)
#   - Liquidity: MarketData.app option chain bid/ask + OI (via chain_fn)
#   - Credit: MarketData.app chain mid-prices (via chain_fn)
#   - Fib levels: auto from swing-style fib computation on daily OHLCV
#   - DTE: MarketData.app expirations (via expirations_fn)
#   - Strikes: chosen from actual chain when available, increment-based fallback
#
# Data sources:
#   - Option chains + expirations: MarketData.app via chain_fn / expirations_fn
#     (same CachedMarketData used by OI tracker and active scanner)
#   - Daily OHLCV: shared via ohlcv_fn (MarketData daily candles or yfinance)
#   - Earnings calendar: FMP /v3/earning_calendar (primary), yfinance (fallback)
#   - Sector / industry: FMP /v3/profile (primary), yfinance (fallback)
#   - Economic events: economic_calendar.py (Finnhub)
#   - Regime context: market_regime.py get_regime_package()
#
# Scorecard (100 pts max, capped):
#   A. Regime alignment      0–15
#   B. Weekly structure       0–15
#   C. Daily structure        0–15
#   D. Support quality        0–15
#   E. Break-even placement   0–15
#   F. Distance / cushion     0–10
#   G. Technical condition    0–10
#   H. Liquidity              0–5
#   I. Event / gap penalty    0 to −10
#   J. DTE adjustment         −5 to +5
# ═══════════════════════════════════════════════════════════════════

import logging
import math
from datetime import datetime, date, timedelta
from typing import Optional, Callable, List, Dict

log = logging.getLogger(__name__)

try:
    import yfinance as yf
    _YF = True
except ImportError:
    _YF = False
    log.warning("yfinance not available — income scanner will need ohlcv_fn")

# FMP integration — earnings calendar, sector/industry, company profile
try:
    from fundamental_screener import _fmp_get, FMP_TOKEN
    _FMP = bool(FMP_TOKEN)
except ImportError:
    _FMP = False
    _fmp_get = None
    FMP_TOKEN = ""

# Economic calendar — FOMC, CPI, NFP, GDP inside DTE window
try:
    from economic_calendar import get_events_in_window
    _ECON_CAL = True
except ImportError:
    _ECON_CAL = False
    get_events_in_window = None

INCOME_TICKERS = ["AAPL", "NVDA", "MRNA", "PLTR", "MSFT", "AMZN", "GOOGL"]

# Detection parameters
LOOKBACK_DAYS       = 180
TOUCH_TOLERANCE_PCT = 1.5
MIN_TOUCHES         = 2
MIN_TOUCH_SPACING   = 5
MAX_LEVELS          = 6

# Biotech / high-gap-risk sectors
HIGH_GAP_SECTORS = {"Healthcare", "Biotechnology"}
HIGH_GAP_INDUSTRIES = {"Biotechnology", "Drug Manufacturers", "Diagnostics & Research"}

# Standard option width increments by price
def _option_increment(spot):
    if spot > 200: return 2.50
    if spot > 100: return 1.00
    return 0.50


# ═══════════════════════════════════════════════════════════
# DATA LAYER — auto-fetch everything
# ═══════════════════════════════════════════════════════════

def default_ohlcv_fn(ticker, days=250):
    """Default OHLCV fetcher. Returns dict or None."""
    if not _YF:
        return None
    try:
        data = yf.download(ticker, period="1y", interval="1d",
                           progress=False, multi_level_index=False)
        if data is None or len(data) < 30:
            return None
        return {
            "open": data["Open"].tolist(), "high": data["High"].tolist(),
            "low": data["Low"].tolist(), "close": data["Close"].tolist(),
            "volume": data["Volume"].tolist(),
        }
    except Exception as e:
        log.error(f"OHLCV fetch failed for {ticker}: {e}")
        return None


def _fetch_ticker_obj(ticker):
    """Get yfinance Ticker object (for earnings/sector fallback only)."""
    if not _YF:
        return None
    try:
        return yf.Ticker(ticker)
    except Exception:
        return None


def _find_weekly_expiry(ticker, expirations_fn=None, ticker_obj=None):
    """
    Find nearest weekly expiration and compute DTE.
    Uses MarketData expirations_fn (primary) or yfinance (fallback).
    Returns (expiry_string, dte_int) or (None, 5).
    """
    exps = []

    # MarketData path (primary)
    if expirations_fn:
        try:
            exps = expirations_fn(ticker)
        except Exception as e:
            log.debug(f"MarketData expirations failed for {ticker}: {e}")

    # yfinance fallback
    if not exps and ticker_obj:
        try:
            exps = list(ticker_obj.options)
        except Exception:
            pass

    if not exps:
        return None, 5

    today = date.today()
    best_exp = None
    best_dte = 999

    for exp_str in exps:
        try:
            exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if 2 <= dte <= 9 and dte < best_dte:
                best_dte = dte
                best_exp = str(exp_str)[:10]
        except ValueError:
            continue

    if best_exp is None:
        for exp_str in exps[:5]:
            try:
                exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte > 0 and dte < best_dte:
                    best_dte = dte
                    best_exp = str(exp_str)[:10]
            except ValueError:
                continue

    return best_exp, best_dte if best_exp else 5


def _fetch_option_chain(ticker, expiry, chain_fn=None):
    """
    Fetch option chain for an expiration.
    Uses MarketData chain_fn (primary) — returns columnar dict.
    Returns {"strike": [], "bid": [], "ask": [], "side": [], "openInterest": [], "volume": []}
    or empty dict on failure.
    """
    if not chain_fn or not expiry:
        return {}

    try:
        # Fetch puts and calls separately (cheaper on MarketData credits)
        put_data = chain_fn(ticker, expiry, side="put")
        call_data = chain_fn(ticker, expiry, side="call")

        # Merge into unified structure
        result = {"strike": [], "bid": [], "ask": [], "mid": [],
                  "side": [], "openInterest": [], "volume": []}

        for data, side_label in [(put_data, "put"), (call_data, "call")]:
            if not data or data.get("s") != "ok":
                continue
            n = len(data.get("strike", []))
            for i in range(n):
                result["strike"].append(data.get("strike", [None] * n)[i])
                result["bid"].append(data.get("bid", [0] * n)[i] or 0)
                result["ask"].append(data.get("ask", [0] * n)[i] or 0)
                result["mid"].append(data.get("mid", [0] * n)[i] or 0)
                result["side"].append(side_label)
                result["openInterest"].append(data.get("openInterest", [0] * n)[i] or 0)
                result["volume"].append(data.get("volume", [0] * n)[i] or 0)

        return result if result["strike"] else {}

    except Exception as e:
        log.debug(f"Chain fetch failed for {ticker} {expiry}: {e}")
        return {}


def _fetch_earnings_date(ticker, ticker_obj=None):
    """
    Get next earnings date. FMP primary, yfinance fallback.
    Returns date or None.
    """
    # FMP earnings calendar (preferred — more reliable)
    if _FMP:
        try:
            today_str = date.today().strftime("%Y-%m-%d")
            future_str = (date.today() + timedelta(days=45)).strftime("%Y-%m-%d")
            data = _fmp_get("earnings-calendar", {"from": today_str, "to": future_str})
            if data:
                for item in data:
                    if item.get("symbol") == ticker:
                        try:
                            return datetime.strptime(item["date"], "%Y-%m-%d").date()
                        except (ValueError, KeyError):
                            continue
        except Exception as e:
            log.debug(f"FMP earnings lookup failed for {ticker}: {e}")

    # yfinance fallback
    if ticker_obj:
        try:
            cal = ticker_obj.calendar
            if isinstance(cal, dict):
                for key in ["Earnings Date", "earningsDate", "earnings_date"]:
                    val = cal.get(key)
                    if val is not None:
                        if isinstance(val, list) and val:
                            val = val[0]
                        if hasattr(val, "date"):
                            return val.date()
                        if isinstance(val, str):
                            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        except Exception:
            pass

    return None


def _fetch_sector(ticker, ticker_obj=None):
    """
    Get sector and industry. FMP primary, yfinance fallback.
    Returns (sector, industry) or (None, None).
    """
    # FMP profile (preferred — cached, consistent)
    if _FMP:
        try:
            data = _fmp_get("profile", {"symbol": ticker})
            if data:
                profile = data[0] if isinstance(data, list) else data
                return profile.get("sector"), profile.get("industry")
        except Exception:
            pass

    # yfinance fallback
    if ticker_obj:
        try:
            info = ticker_obj.info
            return info.get("sector"), info.get("industry")
        except Exception:
            pass

    return None, None


# ═══════════════════════════════════════════════════════════
# AUTO-COMPUTATION — replaces all manual inputs
# ═══════════════════════════════════════════════════════════

def auto_event_risk(ticker, dte, regime_package, ticker_obj=None):
    """
    Auto-compute event/gap risk penalty (0–10).
    Sources:
      - FMP earnings calendar (primary) / yfinance calendar (fallback)
      - FMP company profile for sector/industry
      - economic_calendar.py for FOMC/CPI/NFP/GDP inside DTE window
      - Regime event overlay
    """
    risk = 0

    # 1. Earnings inside trade window
    earnings_date = _fetch_earnings_date(ticker, ticker_obj)
    if earnings_date:
        days_to_earnings = (earnings_date - date.today()).days
        if 0 <= days_to_earnings <= dte:
            risk += 8  # earnings inside window = severe
            log.info(f"{ticker}: earnings in {days_to_earnings}d (inside {dte}d DTE)")
        elif 0 <= days_to_earnings <= dte + 2:
            risk += 4  # just after expiry — IV crush risk
        elif 0 <= days_to_earnings <= dte + 5:
            risk += 2  # approaching

    # 2. Sector gap risk (biotech, etc.)
    sector, industry = _fetch_sector(ticker, ticker_obj)
    if sector in HIGH_GAP_SECTORS or industry in HIGH_GAP_INDUSTRIES:
        risk += 2

    # 3. Economic calendar — FOMC, CPI, NFP, GDP inside DTE window
    if _ECON_CAL:
        try:
            econ_events = get_events_in_window(dte_days=dte)
            if econ_events:
                # Count high-impact events
                high_impact = [e for e in econ_events if e.get("impact") == "high"]
                medium_impact = [e for e in econ_events if e.get("impact") == "medium"]
                if high_impact:
                    risk += min(3, len(high_impact))  # up to +3 for FOMC/CPI/NFP
                    events_str = ", ".join(e.get("event", "?")[:20] for e in high_impact[:2])
                    log.info(f"High-impact econ events in {dte}d window: {events_str}")
                if medium_impact:
                    risk += 1
        except Exception as e:
            log.debug(f"Economic calendar check failed: {e}")

    # 4. Regime event overlay
    event = regime_package.get("event_overlay", "NONE")
    if event == "WAR_CRISIS":
        risk += 3
    elif event == "MACRO_SHOCK":
        risk += 2

    return min(10, risk)


def auto_liquidity(chain, short_strike, long_strike, trade_type):
    """
    Auto-compute liquidity score (0–5) from MarketData option chain.
    Chain is columnar: {"strike": [...], "bid": [...], "ask": [...], ...}
    """
    if not chain or not chain.get("strike"):
        return 1  # no chain data = assume weak liquidity, not neutral

    target_side = "put" if trade_type == "bull_put" else "call"
    score = 0

    # Find the short strike in the columnar chain
    short_idx = None
    strikes = chain.get("strike", [])
    sides = chain.get("side", [])
    for i in range(len(strikes)):
        sd = sides[i] if i < len(sides) else ""
        if strikes[i] is not None and abs(strikes[i] - short_strike) < 0.01 and sd == target_side:
            short_idx = i
            break

    if short_idx is None:
        return 2

    bids = chain.get("bid", [])
    asks = chain.get("ask", [])
    ois = chain.get("openInterest", [])
    vols = chain.get("volume", [])

    bid = (bids[short_idx] or 0) if short_idx < len(bids) else 0
    ask = (asks[short_idx] or 0) if short_idx < len(asks) else 0
    oi = int(ois[short_idx] or 0) if short_idx < len(ois) else 0
    vol = int(vols[short_idx] or 0) if short_idx < len(vols) else 0

    if ask > 0 and bid > 0:
        spread_pct = (ask - bid) / ask * 100
        if spread_pct < 10: score += 2
        elif spread_pct < 25: score += 1

    if oi >= 500: score += 2
    elif oi >= 100: score += 1

    if vol >= 50: score += 1

    return min(5, score)


def auto_credit(chain, short_strike, long_strike, trade_type):
    """
    Get actual credit from MarketData option chain mid-prices.
    Validates BOTH legs for stale/wide quotes before accepting.
    Returns (credit, width) or (None, None) if strikes not found or quotes bad.
    """
    if not chain or not chain.get("strike"):
        return None, None

    target_side = "put" if trade_type == "bull_put" else "call"

    strikes = chain.get("strike", [])
    sides = chain.get("side", [])
    mids = chain.get("mid", [])
    bids = chain.get("bid", [])
    asks = chain.get("ask", [])

    def _find_leg(target_strike):
        """Find a leg and validate its quote quality. Returns mid or None."""
        for i in range(len(strikes)):
            if strikes[i] is None:
                continue
            sd = sides[i] if i < len(sides) else ""
            if sd != target_side or abs(strikes[i] - target_strike) >= 0.01:
                continue

            bid = (bids[i] or 0) if i < len(bids) else 0
            ask = (asks[i] or 0) if i < len(asks) else 0
            mid = (mids[i] if i < len(mids) and mids[i] else (bid + ask) / 2)

            # Reject stale: both bid and ask are 0
            if bid == 0 and ask == 0:
                return None
            # Reject wide: spread > 50% of ask
            if ask > 0 and bid > 0 and (ask - bid) / ask > 0.50:
                return None

            return mid
        return None  # strike not found in chain

    short_mid = _find_leg(short_strike)
    long_mid = _find_leg(long_strike)

    if short_mid is None or long_mid is None:
        return None, None

    credit = round(max(0, short_mid - long_mid), 2)
    width = round(abs(short_strike - long_strike), 2)
    return credit, width


def auto_fib_levels(highs, lows, lookback=60):
    """
    Compute fib retracement levels from recent swing high/low.
    Returns list of fib price levels.
    """
    if len(highs) < lookback or len(lows) < lookback:
        return []

    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]

    swing_high = max(recent_highs)
    swing_low = min(recent_lows)

    if swing_high <= swing_low:
        return []

    fib_range = swing_high - swing_low
    levels = [
        round(swing_high - fib_range * 0.236, 2),
        round(swing_high - fib_range * 0.382, 2),
        round(swing_high - fib_range * 0.500, 2),
        round(swing_high - fib_range * 0.618, 2),
        round(swing_high - fib_range * 0.786, 2),
    ]
    return levels


# ═══════════════════════════════════════════════════════════
# LEVEL DETECTION (same as v3)
# ═══════════════════════════════════════════════════════════

def _cluster_levels(prices, current_price, tolerance_pct, min_touches,
                    min_spacing, max_levels, side):
    if len(prices) < 20:
        return []

    n = len(prices)
    tolerance = current_price * (tolerance_pct / 100)

    if side == "support":
        candidates = [(i, prices[i]) for i in range(n) if prices[i] < current_price]
    else:
        candidates = [(i, prices[i]) for i in range(n) if prices[i] > current_price]

    if not candidates:
        return []

    sorted_cands = sorted(candidates, key=lambda x: x[1])
    levels = []
    used = set()

    for idx, price in sorted_cands:
        if idx in used:
            continue
        cluster_idx = []
        cluster_px = []
        for j, p in sorted_cands:
            if j not in used and abs(p - price) <= tolerance:
                cluster_idx.append(j)
                cluster_px.append(p)
                used.add(j)
        if len(cluster_px) < min_touches:
            continue
        cluster_idx.sort()
        distinct = [cluster_idx[0]]
        for ci in cluster_idx[1:]:
            if ci - distinct[-1] >= min_spacing:
                distinct.append(ci)
        if len(distinct) < min_touches:
            continue

        level_price = sum(cluster_px) / len(cluster_px)
        last_age = n - 1 - max(distinct)
        recency_wt = max(0.1, 1.0 - (last_age / 120))

        if side == "support":
            cushion = ((current_price - level_price) / current_price) * 100
        else:
            cushion = ((level_price - current_price) / current_price) * 100

        levels.append({
            "level": round(level_price, 2), "touches": len(distinct),
            "last_touch_days_ago": last_age, "held": True,
            "quality": round(len(distinct) * recency_wt, 2),
            "cushion_pct": round(cushion, 2),
        })

    levels.sort(key=lambda x: x["quality"], reverse=True)
    return levels[:max_levels]


def detect_support_levels(daily_lows, spot):
    return _cluster_levels(daily_lows, spot, TOUCH_TOLERANCE_PCT, MIN_TOUCHES, MIN_TOUCH_SPACING, MAX_LEVELS, "support")

def detect_resistance_levels(daily_highs, spot):
    return _cluster_levels(daily_highs, spot, TOUCH_TOLERANCE_PCT, MIN_TOUCHES, MIN_TOUCH_SPACING, MAX_LEVELS, "resistance")

def find_support_failure_level(supports, primary):
    below = [s for s in supports if s["level"] < primary * 0.98]
    if below:
        below.sort(key=lambda x: x["quality"], reverse=True)
        return below[0]["level"]
    return round(primary * 0.97, 2)

def find_resistance_failure_level(resistances, primary):
    above = [r for r in resistances if r["level"] > primary * 1.02]
    if above:
        above.sort(key=lambda x: x["quality"], reverse=True)
        return above[0]["level"]
    return round(primary * 1.03, 2)

def _strike_below_support(support, spot, chain=None):
    """
    Pick the nearest valid strike below a support level.
    Uses actual listed strikes from chain when available.
    Falls back to increment-based rounding when chain is unavailable.
    """
    level = support["level"]

    # Chain-aware: find the highest listed put strike below the support level
    if chain and chain.get("strike"):
        put_strikes = sorted(set(
            s for s, sd in zip(chain["strike"], chain.get("side", []))
            if s is not None and sd == "put" and s < level
        ), reverse=True)
        if put_strikes:
            return put_strikes[0]  # highest listed strike below support

    # Fallback: increment-based
    inc = _option_increment(spot)
    return round(math.floor((level - 0.01) / inc) * inc, 2)


def _strike_above_resistance(resistance, spot, chain=None):
    """
    Pick the nearest valid strike above a resistance level.
    Uses actual listed strikes from chain when available.
    """
    level = resistance["level"]

    if chain and chain.get("strike"):
        call_strikes = sorted(set(
            s for s, sd in zip(chain["strike"], chain.get("side", []))
            if s is not None and sd == "call" and s > level
        ))
        if call_strikes:
            return call_strikes[0]  # lowest listed strike above resistance

    inc = _option_increment(spot)
    return round(math.ceil((level + 0.01) / inc) * inc, 2)


def _long_strike_from_chain(short_strike, trade_type, spot, chain=None):
    """
    Pick the long leg strike from the chain (one increment away from short).
    Uses actual listed strikes when available.
    """
    target_side = "put" if trade_type == "bull_put" else "call"

    if chain and chain.get("strike"):
        listed = sorted(set(
            s for s, sd in zip(chain["strike"], chain.get("side", []))
            if s is not None and sd == target_side
        ))
        if trade_type == "bull_put":
            # Next listed strike below the short strike
            below = [s for s in listed if s < short_strike]
            if below:
                return below[-1]  # highest strike below short
        else:
            # Next listed strike above the short strike
            above = [s for s in listed if s > short_strike]
            if above:
                return above[0]  # lowest strike above short

    # Fallback: one increment away
    inc = _option_increment(spot)
    if trade_type == "bull_put":
        return round(short_strike - inc, 2)
    else:
        return round(short_strike + inc, 2)


# ═══════════════════════════════════════════════════════════
# TREND / TECHNICAL (same as v3)
# ═══════════════════════════════════════════════════════════

def _ema(data, period):
    if len(data) < period: return None
    mult = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for val in data[period:]:
        ema = (val - ema) * mult + ema
    return ema

def detect_weekly_trend(closes):
    if len(closes) < 105:
        return {"weekly_bull": False, "weekly_bear": False, "trend": "unknown"}
    weekly = closes[::5]
    if len(weekly) < 21:
        return {"weekly_bull": False, "weekly_bear": False, "trend": "unknown"}
    e8 = _ema(weekly, 8); e21 = _ema(weekly, 21)
    if e8 is None or e21 is None:
        return {"weekly_bull": False, "weekly_bear": False, "trend": "unknown"}
    return {"weekly_bull": e8 > e21, "weekly_bear": e8 < e21,
            "trend": "bull" if e8 > e21 else "bear"}

def detect_daily_trend(closes):
    if len(closes) < 55:
        return {"daily_bull": False, "daily_bear": False, "trend": "unknown",
                "reclaiming": False, "breaking_down": False, "above_50sma": False}
    e8 = _ema(closes, 8); e21 = _ema(closes, 21)
    sma50 = sum(closes[-50:]) / 50; spot = closes[-1]
    daily_bull = e8 > e21 if (e8 and e21) else False
    daily_bear = e8 < e21 if (e8 and e21) else False
    reclaiming = False
    if e8 and e21 and len(closes) >= 30:
        pe8 = _ema(closes[:-5], 8); pe21 = _ema(closes[:-5], 21)
        if pe8 and pe21: reclaiming = (pe8 < pe21) and (e8 > e21)
    breaking_down = daily_bear and spot < sma50
    return {"daily_bull": daily_bull, "daily_bear": daily_bear,
            "trend": "bull" if daily_bull else ("bear" if daily_bear else "neutral"),
            "above_50sma": spot > sma50, "reclaiming": reclaiming, "breaking_down": breaking_down}

def compute_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(0, c) for c in changes[-period:]]
    losses = [max(0, -c) for c in changes[-period:]]
    avg_g = sum(gains) / period; avg_l = sum(losses) / period
    if avg_l == 0: return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 1)

def volume_state(volumes, lookback=10):
    if len(volumes) < lookback + 5: return "unknown"
    ma = sum(volumes[-(lookback+5):-5]) / lookback
    recent = sum(volumes[-5:]) / 5
    if recent < ma * 0.80: return "contracting"
    elif recent > ma * 1.30: return "expanding"
    return "normal"

def vwap_state(closes, highs, lows, volumes):
    if len(closes) < 2: return "unknown"
    n = min(20, len(closes))
    hlc3 = [(highs[-i] + lows[-i] + closes[-i]) / 3 for i in range(1, n+1)]
    vols = [volumes[-i] for i in range(1, n+1)]
    tv = sum(vols)
    if tv == 0: return "unknown"
    vw = sum(p*v for p, v in zip(hlc3, vols)) / tv
    return "above" if closes[-1] > vw else "below"


# ═══════════════════════════════════════════════════════════
# HARD BLOCKS
# ═══════════════════════════════════════════════════════════

def check_hard_blocks(trade_type, short_strike, breakeven, support_level,
                      failure_level, regime_package, return_on_risk=None,
                      earnings_in_window=False):
    blocks = []
    level = support_level["level"]

    if trade_type == "bull_put":
        if short_strike >= level:
            blocks.append(f"Short strike ${short_strike:.2f} at/above support ${level:.2f}")
        if breakeven > failure_level:
            blocks.append(f"Break-even ${breakeven:.2f} above failure ${failure_level:.2f}")
    else:
        if short_strike <= level:
            blocks.append(f"Short strike ${short_strike:.2f} at/below resistance ${level:.2f}")
        if breakeven < failure_level:
            blocks.append(f"Break-even ${breakeven:.2f} below resistance failure ${failure_level:.2f}")

    if return_on_risk is not None and return_on_risk < 5.0:
        blocks.append(f"ROC {return_on_risk:.1f}% below minimum 5%")

    if earnings_in_window:
        blocks.append("Earnings inside trade window — binary event risk")

    core = regime_package.get("core_regime", "")
    event = regime_package.get("event_overlay", "NONE")
    if trade_type == "bull_put" and core == "BEAR_CRISIS" and event == "WAR_CRISIS":
        blocks.append("BEAR_CRISIS + WAR_CRISIS — hard block on put sales")

    return blocks


# ═══════════════════════════════════════════════════════════
# ITQS SCORING (same framework as v3)
# ═══════════════════════════════════════════════════════════

def compute_itqs(trade_type, short_strike, breakeven, spot, support_level,
                 failure_level, regime_package, weekly_trend, daily_trend,
                 rsi, vol, vwap, fib_confluence=False, return_on_risk=None,
                 liquidity_score=3, event_risk=0, dte=5, flow_data=None):
    score = 0; breakdown = {}; notes = []
    level = support_level["level"]; touches = support_level.get("touches", 0)

    if trade_type == "bull_put":
        spot_to_strike_pct = ((spot - short_strike) / spot) * 100 if spot > 0 else 0
        be_to_failure_pct = ((failure_level - breakeven) / failure_level) * 100 if failure_level > 0 else 0
        strike_below = short_strike < level
    else:
        spot_to_strike_pct = ((short_strike - spot) / spot) * 100 if spot > 0 else 0
        be_to_failure_pct = ((breakeven - failure_level) / failure_level) * 100 if failure_level > 0 else 0
        strike_below = short_strike > level

    # A. Regime (0-15)
    core = regime_package.get("core_regime", "BEAR_CRISIS")
    if trade_type == "bull_put":
        a = {"BULL_BASE": 15, "BULL_TRANSITION": 12, "CHOP": 8, "BEAR_TRANSITION": 5, "BEAR_CRISIS": 2}.get(core, 5)
    else:
        a = {"BULL_BASE": 2, "BULL_TRANSITION": 5, "CHOP": 8, "BEAR_TRANSITION": 12, "BEAR_CRISIS": 15}.get(core, 5)
    breakdown["A_regime"] = a; score += a

    # B. Weekly (0-15)
    bull_side = trade_type == "bull_put"
    if (bull_side and weekly_trend.get("weekly_bull")) or (not bull_side and weekly_trend.get("weekly_bear")):
        b = 15; notes.append(f"Weekly {'bull' if bull_side else 'bear'} confirmed")
    elif (bull_side and not weekly_trend.get("weekly_bear")) or (not bull_side and not weekly_trend.get("weekly_bull")):
        b = 10
    else:
        b = 3; notes.append("⚠️ Weekly trend opposes trade direction")
    breakdown["B_weekly"] = b; score += b

    # C. Daily (0-15)
    if trade_type == "bull_put":
        if daily_trend.get("reclaiming"): c = 15; notes.append("Daily reclaiming")
        elif daily_trend.get("daily_bull"): c = 13
        elif daily_trend.get("above_50sma") and not daily_trend.get("breaking_down"): c = 10
        elif daily_trend.get("breaking_down"): c = 3; notes.append("⚠️ Daily breakdown")
        elif daily_trend.get("daily_bear"): c = 5
        else: c = 8
    else:
        if daily_trend.get("breaking_down"): c = 15
        elif daily_trend.get("daily_bear"): c = 13
        elif daily_trend.get("daily_bull"): c = 3
        else: c = 8
    breakdown["C_daily"] = c; score += c

    # D. Support quality (0-15)
    if strike_below and touches >= 3: d = 15
    elif strike_below and touches >= 2: d = 12
    elif abs(short_strike - level)/level < 0.01 and touches >= 3: d = 10
    elif strike_below: d = 8
    elif abs(short_strike - level)/level < 0.01: d = 5
    else: d = 2; notes.append("Strike not below support")
    breakdown["D_support"] = d; score += d

    # E. Break-even (0-15)
    be_safe = (breakeven < failure_level) if trade_type == "bull_put" else (breakeven > failure_level)
    if be_safe and be_to_failure_pct > 2: e = 15; notes.append("Break-even well past failure level")
    elif be_safe and be_to_failure_pct > 0.5: e = 12
    elif be_safe: e = 8
    elif abs(be_to_failure_pct) < 0.5: e = 5; notes.append("Break-even near failure")
    else: e = 2; notes.append("⚠️ Break-even past failure zone")
    breakdown["E_breakeven"] = e; score += e

    # F. Cushion (0-10) — from actual trade
    if 4 <= spot_to_strike_pct <= 8: f = 10
    elif 3 <= spot_to_strike_pct < 4: f = 7
    elif 8 < spot_to_strike_pct <= 12: f = 7
    elif 2 <= spot_to_strike_pct < 3: f = 4; notes.append("Cushion tight")
    elif spot_to_strike_pct > 12: f = 4
    elif spot_to_strike_pct < 2: f = 0; notes.append("⚠️ Cushion < 2%")
    else: f = 5
    breakdown["F_cushion"] = f; score += f

    # G. Technical (0-10)
    g = 0
    if rsi is not None:
        if trade_type == "bull_put":
            if 40 <= rsi <= 55: g += 3
            elif 55 < rsi <= 65 or 35 <= rsi < 40: g += 2
            elif rsi < 30: notes.append("⚠️ RSI oversold")
            else: g += 1
        else:
            if 55 <= rsi <= 70: g += 3
            elif rsi > 75: g += 1; notes.append("RSI overbought")
            else: g += 2
    if vol == "contracting": g += 3; notes.append("Volume contracting")
    elif vol == "normal": g += 2
    if (trade_type == "bull_put" and vwap == "above") or (trade_type == "bear_call" and vwap == "below"): g += 2
    else: g += 1
    if fib_confluence: g += 2; notes.append("Fib confluence")
    breakdown["G_technical"] = min(10, g); score += breakdown["G_technical"]

    # H. Liquidity (0-5)
    breakdown["H_liquidity"] = min(5, max(0, liquidity_score)); score += breakdown["H_liquidity"]

    # I. Event penalty (0 to -10)
    breakdown["I_event_penalty"] = -min(10, max(0, event_risk)); score += breakdown["I_event_penalty"]
    if event_risk >= 5: notes.append(f"⚠️ Event risk elevated (−{event_risk})")
    if event_risk >= 8: notes.append("🚫 Earnings inside trade window")

    # J. DTE (−5 to +5)
    if 4 <= dte <= 7: j = 5
    elif dte == 3: j = 2
    elif 8 <= dte <= 14: j = 2
    elif dte <= 2: j = -5; notes.append("⚠️ DTE ≤2 — no room to roll")
    elif dte > 14: j = -2
    else: j = 0
    breakdown["J_dte"] = j; score += j

    # K. Institutional Flow (−10 to +15)
    k = 0; flow_reasons = []; recommended_expiry = None; expiry_note = ""
    if flow_data:
        k = flow_data.get("score_adj", 0)
        flow_reasons = flow_data.get("reasons", [])
        recommended_expiry = flow_data.get("recommended_expiry")
        expiry_note = flow_data.get("expiry_note", "")
        for fr in flow_reasons:
            notes.append(fr)
    breakdown["K_flow"] = max(-10, min(15, k)); score += breakdown["K_flow"]

    score = max(0, min(100, score))
    if score >= 90: grade, label = "A+", "A+ elite income trade"
    elif score >= 85: grade, label = "A", "A strong trade"
    elif score >= 75: grade, label = "B", "B good trade"
    elif score >= 65: grade, label = "C", "C acceptable"
    else: grade, label = "F", "Pass"

    decision = label
    if grade in ("A+", "A", "B"):
        if trade_type == "bull_put" and weekly_trend.get("weekly_bull") and touches >= 2 and spot_to_strike_pct >= 3:
            decision = "Wheel-friendly entry"
        if event_risk >= 5: decision = "Support-qualified, elevated gap risk"
    elif grade == "C":
        if spot_to_strike_pct < 3: decision = "Pass — strike too close"
        elif c <= 5: decision = "Pass — chart quality too weak"
        elif event_risk >= 7: decision = "Pass — event risk too high"

    return {"score": score, "grade": grade, "label": label, "decision": decision,
            "breakdown": breakdown, "notes": notes, "trade_type": trade_type,
            "spot_to_strike_pct": round(spot_to_strike_pct, 1),
            "be_to_failure_pct": round(be_to_failure_pct, 1),
            "recommended_expiry": recommended_expiry,
            "expiry_note": expiry_note}


# ═══════════════════════════════════════════════════════════
# SCAN ONE TICKER — FULLY AUTOMATED
# ═══════════════════════════════════════════════════════════

def scan_ticker_income(ticker, regime_package, ohlcv_fn=None,
                       chain_fn=None, expirations_fn=None, flow_fn=None):
    """
    Fully automated scan. Zero manual inputs.
    chain_fn(ticker, expiry, side=) → MarketData columnar dict
    expirations_fn(ticker) → list of expiration strings
    """
    fetch = ohlcv_fn or default_ohlcv_fn
    try:
        ohlcv = fetch(ticker)
        if ohlcv is None:
            return []

        closes = ohlcv["close"]; highs = ohlcv["high"]
        lows = ohlcv["low"]; volumes = ohlcv["volume"]
        if len(closes) < 60:
            return []
        spot = closes[-1]

        # Auto-detect everything
        supports = detect_support_levels(lows, spot)
        resistances = detect_resistance_levels(highs, spot)
        weekly = detect_weekly_trend(closes)
        daily = detect_daily_trend(closes)
        rsi = compute_rsi(closes)
        vol = volume_state(volumes)
        vwap_st = vwap_state(closes, highs, lows, volumes)
        fibs = auto_fib_levels(highs, lows)

        # ── Swing scanner confluence ──
        # Check for recent swing signals on this ticker
        swing_signal = None
        swing_fib_price = None
        swing_income_eligible = False
        try:
            from swing_scanner import get_recent_signals
            swing_signal = get_recent_signals(ticker, max_age_days=7)
            if swing_signal:
                swing_fib_price = swing_signal.get("fib_price")
                swing_income_eligible = swing_signal.get("income_eligible", False)
                if swing_fib_price and swing_fib_price not in fibs:
                    fibs.append(swing_fib_price)
                log.info(f"Income scan {ticker}: swing signal found — "
                         f"fib {swing_signal.get('fib_level')}% @ ${swing_fib_price}, "
                         f"income_eligible={swing_income_eligible}, "
                         f"hold={swing_signal.get('hold_class')}")
        except ImportError:
            pass
        except Exception as e:
            log.debug(f"Swing signal lookup failed for {ticker}: {e}")

        # Option chain data (MarketData primary, yfinance fallback for expirations)
        ticker_obj = _fetch_ticker_obj(ticker)
        expiry, dte = _find_weekly_expiry(ticker, expirations_fn=expirations_fn, ticker_obj=ticker_obj)
        chain = _fetch_option_chain(ticker, expiry, chain_fn=chain_fn) if expiry else {}

        # Auto event risk (FMP primary, yfinance fallback)
        event_risk = auto_event_risk(ticker, dte, regime_package, ticker_obj=ticker_obj)

        # Check earnings for hard block
        earnings_date = _fetch_earnings_date(ticker, ticker_obj=ticker_obj)
        earnings_in_window = False
        if earnings_date:
            days_to_earn = (earnings_date - date.today()).days
            earnings_in_window = 0 <= days_to_earn <= dte

        opportunities = []

        # ── Bull put candidates ──
        for sup in supports:
            short_strike = _strike_below_support(sup, spot, chain=chain)
            if short_strike <= 0 or short_strike >= spot:
                continue

            # Long strike from actual chain or increment fallback
            long_strike = _long_strike_from_chain(short_strike, "bull_put", spot, chain=chain)

            # Try to get real credit from chain
            real_credit, real_width = auto_credit(chain, short_strike, long_strike, "bull_put")

            if real_credit is not None and real_width and real_width > 0:
                credit = real_credit
                width = real_width
            else:
                width = abs(short_strike - long_strike)
                credit = width * 0.15  # fallback estimate

            breakeven = short_strike - credit
            roc = (credit / width) * 100 if width > 0 else 0
            cushion_pct = ((spot - short_strike) / spot) * 100
            failure = find_support_failure_level(supports, sup["level"])

            # Auto liquidity
            liq = auto_liquidity(chain, short_strike, long_strike, "bull_put")

            # Fib confluence (includes swing scanner fibs if available)
            fib_match = any(abs(f - short_strike) / spot < 0.015 for f in fibs) if fibs else False

            # Swing signal confluence — boost if swing scanner confirms this support
            swing_confirmed = False
            if swing_signal and swing_signal.get("direction") == "bull" and swing_fib_price:
                if abs(swing_fib_price - sup["level"]) / spot < 0.02:
                    swing_confirmed = True
                    fib_match = True  # swing signal counts as fib confluence

            blocks = check_hard_blocks("bull_put", short_strike, breakeven, sup, failure,
                                       regime_package, return_on_risk=roc,
                                       earnings_in_window=earnings_in_window)

            # Institutional flow scoring
            flow_data = None
            if flow_fn:
                try:
                    flow_data = flow_fn(ticker, short_strike, "bull_put", expiry)
                except Exception:
                    pass

            itqs = compute_itqs(
                "bull_put", short_strike, breakeven, spot, sup, failure,
                regime_package, weekly, daily, rsi, vol, vwap_st,
                fib_confluence=fib_match, return_on_risk=roc,
                liquidity_score=liq, event_risk=event_risk, dte=dte,
                flow_data=flow_data,
            )

            opportunities.append({
                "ticker": ticker, "trade_type": "bull_put",
                "spot": round(spot, 2), "short_strike": short_strike,
                "long_strike": long_strike, "width": width,
                "credit": round(credit, 2), "roc_pct": round(roc, 1),
                "breakeven": round(breakeven, 2), "failure_level": failure,
                "dte": dte, "expiry": expiry,
                "level": sup["level"], "level_type": "support",
                "touches": sup["touches"],
                "last_touch_days_ago": sup["last_touch_days_ago"],
                "cushion_pct": round(cushion_pct, 1), "quality": sup["quality"],
                "weekly_trend": weekly["trend"], "daily_trend": daily["trend"],
                "rsi": rsi, "vol_state": vol, "vwap": vwap_st,
                "fib_confluence": fib_match,
                "swing_confirmed": swing_confirmed,
                "swing_signal": {
                    "fib_level": swing_signal.get("fib_level"),
                    "confidence": swing_signal.get("confidence"),
                    "hold_class": swing_signal.get("hold_class"),
                    "income_eligible": swing_income_eligible,
                } if swing_confirmed else None,
                "liquidity_score": liq, "event_risk": event_risk,
                "chain_available": bool(chain.get("strike")),
                "hard_blocks": blocks, "itqs": itqs,
                "regime": regime_package.get("core_regime", "UNKNOWN"),
            })

        # ── Bear call candidates ──
        for res in resistances:
            short_strike = _strike_above_resistance(res, spot, chain=chain)
            if short_strike <= spot:
                continue

            long_strike = _long_strike_from_chain(short_strike, "bear_call", spot, chain=chain)
            real_credit, real_width = auto_credit(chain, short_strike, long_strike, "bear_call")

            if real_credit is not None and real_width and real_width > 0:
                credit = real_credit; width = real_width
            else:
                width = abs(long_strike - short_strike); credit = width * 0.15

            breakeven = short_strike + credit
            roc = (credit / width) * 100 if width > 0 else 0
            cushion_pct = ((short_strike - spot) / spot) * 100
            failure = find_resistance_failure_level(resistances, res["level"])
            liq = auto_liquidity(chain, short_strike, long_strike, "bear_call")
            fib_match = any(abs(f - short_strike) / spot < 0.015 for f in fibs) if fibs else False

            blocks = check_hard_blocks("bear_call", short_strike, breakeven, res, failure,
                                       regime_package, return_on_risk=roc,
                                       earnings_in_window=earnings_in_window)

            # Institutional flow scoring
            flow_data = None
            if flow_fn:
                try:
                    flow_data = flow_fn(ticker, short_strike, "bear_call", expiry)
                except Exception:
                    pass

            itqs = compute_itqs(
                "bear_call", short_strike, breakeven, spot, res, failure,
                regime_package, weekly, daily, rsi, vol, vwap_st,
                fib_confluence=fib_match, return_on_risk=roc,
                liquidity_score=liq, event_risk=event_risk, dte=dte,
                flow_data=flow_data,
            )

            opportunities.append({
                "ticker": ticker, "trade_type": "bear_call",
                "spot": round(spot, 2), "short_strike": short_strike,
                "long_strike": long_strike, "width": width,
                "credit": round(credit, 2), "roc_pct": round(roc, 1),
                "breakeven": round(breakeven, 2), "failure_level": failure,
                "dte": dte, "expiry": expiry,
                "level": res["level"], "level_type": "resistance",
                "touches": res["touches"],
                "last_touch_days_ago": res["last_touch_days_ago"],
                "cushion_pct": round(cushion_pct, 1), "quality": res["quality"],
                "weekly_trend": weekly["trend"], "daily_trend": daily["trend"],
                "rsi": rsi, "vol_state": vol, "vwap": vwap_st,
                "fib_confluence": fib_match,
                "liquidity_score": liq, "event_risk": event_risk,
                "chain_available": bool(chain.get("strike")),
                "hard_blocks": blocks, "itqs": itqs,
                "regime": regime_package.get("core_regime", "UNKNOWN"),
            })

        opportunities.sort(key=lambda x: x["itqs"]["score"], reverse=True)
        return opportunities

    except Exception as e:
        log.error(f"Income scanner error for {ticker}: {e}", exc_info=True)
        return []


# ═══════════════════════════════════════════════════════════
# MANUAL TRADE SCORING — also fully automated context
# ═══════════════════════════════════════════════════════════

def score_trade(ticker, trade_type, short_strike, width, credit, regime_package,
                ohlcv_fn=None, chain_fn=None, expirations_fn=None, expiry=None,
                flow_fn=None):
    """
    Score a specific trade. All context auto-computed.
    Pass expiry="2026-04-11" to score against a specific expiration.
    If omitted, auto-selects nearest weekly.
    Example: score_trade("MRNA", "bull_put", 47.0, 2.0, 0.45, regime_pkg)
    Example: score_trade("MRNA", "bull_put", 47.0, 2.0, 0.45, regime_pkg, expiry="2026-04-11")
    """
    fetch = ohlcv_fn or default_ohlcv_fn
    try:
        ohlcv = fetch(ticker)
        if ohlcv is None:
            return {"error": f"No data for {ticker}"}

        closes = ohlcv["close"]; highs = ohlcv["high"]
        lows = ohlcv["low"]; volumes = ohlcv["volume"]
        if len(closes) < 60:
            return {"error": "Insufficient data"}
        spot = closes[-1]

        # Expiry: use explicit if provided, otherwise auto-detect
        ticker_obj = _fetch_ticker_obj(ticker)
        if expiry:
            dte = max(0, (datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date() - date.today()).days)
        else:
            expiry, dte = _find_weekly_expiry(ticker, expirations_fn=expirations_fn, ticker_obj=ticker_obj)

        chain = _fetch_option_chain(ticker, expiry, chain_fn=chain_fn) if expiry else {}
        event_risk = auto_event_risk(ticker, dte, regime_package, ticker_obj=ticker_obj)
        earnings_date = _fetch_earnings_date(ticker, ticker_obj=ticker_obj)
        earnings_in_window = earnings_date and 0 <= (earnings_date - date.today()).days <= dte

        # Auto fib + levels
        fibs = auto_fib_levels(highs, lows)

        if trade_type == "bull_put":
            breakeven = short_strike - credit
            long_strike = short_strike - width
            levels = detect_support_levels(lows, spot)
            best = min(levels, key=lambda s: abs(s["level"] - short_strike)) if levels else None
            if best is None:
                best = {"level": short_strike, "touches": 0, "last_touch_days_ago": 999,
                        "cushion_pct": ((spot-short_strike)/spot)*100, "quality": 0, "held": True}
            failure = find_support_failure_level(levels, best["level"])
        else:
            breakeven = short_strike + credit
            long_strike = short_strike + width
            levels = detect_resistance_levels(highs, spot)
            best = min(levels, key=lambda r: abs(r["level"] - short_strike)) if levels else None
            if best is None:
                best = {"level": short_strike, "touches": 0, "last_touch_days_ago": 999,
                        "cushion_pct": ((short_strike-spot)/spot)*100, "quality": 0, "held": True}
            failure = find_resistance_failure_level(levels, best["level"])

        weekly = detect_weekly_trend(closes)
        daily = detect_daily_trend(closes)
        rsi = compute_rsi(closes)
        vol = volume_state(volumes)
        vwap_st = vwap_state(closes, highs, lows, volumes)
        roc = (credit / width) * 100 if width > 0 else 0

        liq = auto_liquidity(chain, short_strike, long_strike, trade_type)
        fib_match = any(abs(f - short_strike) / spot < 0.015 for f in fibs) if fibs else False

        # Fetch institutional flow data if available
        flow_data = None
        if flow_fn:
            try:
                flow_data = flow_fn(ticker, short_strike, trade_type, expiry)
            except Exception:
                pass

        blocks = check_hard_blocks(trade_type, short_strike, breakeven, best, failure,
                                   regime_package, return_on_risk=roc,
                                   earnings_in_window=bool(earnings_in_window))

        itqs = compute_itqs(
            trade_type, short_strike, breakeven, spot, best, failure,
            regime_package, weekly, daily, rsi, vol, vwap_st,
            fib_confluence=fib_match, return_on_risk=roc,
            liquidity_score=liq, event_risk=event_risk, dte=dte,
            flow_data=flow_data,
        )

        return {
            "ticker": ticker, "trade_type": trade_type,
            "spot": round(spot, 2), "short_strike": short_strike,
            "long_strike": long_strike, "width": width, "credit": credit,
            "breakeven": round(breakeven, 2), "roc_pct": round(roc, 1), "dte": dte,
            "expiry": expiry,
            "support_level": best["level"], "support_touches": best["touches"],
            "failure_level": failure,
            "regime": regime_package.get("core_regime", "UNKNOWN"),
            "weekly_trend": weekly["trend"], "daily_trend": daily["trend"],
            "rsi": rsi, "vol_state": vol, "vwap": vwap_st,
            "fib_confluence": fib_match,
            "liquidity_score": liq, "event_risk": event_risk,
            "chain_available": bool(chain.get("strike")),
            "hard_blocks": blocks, "itqs": itqs,
        }

    except Exception as e:
        log.error(f"Trade scoring error: {e}", exc_info=True)
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
# FORMATTING (updated for auto-computed fields)
# ═══════════════════════════════════════════════════════════

def format_income_alert(opp):
    itqs = opp["itqs"]; bd = itqs["breakdown"]
    grade = itqs["grade"]; score = itqs["score"]
    div = "━" * 30
    ge = {"A+": "🟢", "A": "🟢", "B": "🟡", "C": "🟠", "F": "🔴"}.get(grade, "⚪")
    te = "🐂" if opp["trade_type"] == "bull_put" else "🐻"

    lines = [f"{ge} INCOME SCAN — {opp['ticker']}", f"Grade: {grade} (ITQS: {score}) | {itqs['decision']}", div]

    blocks = opp.get("hard_blocks", [])
    if blocks:
        lines.append("🚫 HARD BLOCKS:"); [lines.append(f"  ✗ {b}") for b in blocks]; lines.append(div)

    ll = "Support" if opp["trade_type"] == "bull_put" else "Resistance"
    chain_tag = "📡 live chain" if opp.get("chain_available") else "📊 estimated"
    lines += [
        f"{ll}: ${opp['level']:.2f} ({opp['touches']}T, last {opp['last_touch_days_ago']}d)",
        f"Strike: ${opp['short_strike']:.2f}/{opp.get('long_strike', opp['short_strike'] - opp.get('width',1)):.2f} | Failure: ${opp.get('failure_level',0):.2f}",
        f"Spot: ${opp['spot']:.2f} | Cushion: {opp['cushion_pct']:.1f}%",
        f"Credit: ${opp.get('credit',0):.2f} on ${opp.get('width',0):.2f} wide ({opp.get('roc_pct',0):.1f}% ROC) [{chain_tag}]",
        f"Expiry: {opp.get('expiry','weekly')} ({opp.get('dte',5)}d)",
    ]

    # Swing scanner confluence
    if opp.get("swing_confirmed") and opp.get("swing_signal"):
        ss = opp["swing_signal"]
        lines.append(f"🧭 SWING CONFIRMED: fib {ss['fib_level']}% | conf {ss['confidence']} | "
                     f"{ss['hold_class'].replace('_', ' ')} {'💰' if ss['income_eligible'] else ''}")

    lines += [
        f"", f"📊 SCORECARD",
        f"  A. Regime:     {bd.get('A_regime',0):>3}/15  ({opp.get('regime','')})",
        f"  B. Weekly:     {bd.get('B_weekly',0):>3}/15  ({opp.get('weekly_trend','')})",
        f"  C. Daily:      {bd.get('C_daily',0):>3}/15  ({opp.get('daily_trend','')})",
        f"  D. Support:    {bd.get('D_support',0):>3}/15  ({opp['touches']}T)",
        f"  E. Break-even: {bd.get('E_breakeven',0):>3}/15",
        f"  F. Cushion:    {bd.get('F_cushion',0):>3}/10  ({itqs.get('spot_to_strike_pct','?')}%)",
        f"  G. Technical:  {bd.get('G_technical',0):>3}/10  (RSI {opp.get('rsi','?')} | {opp.get('vol_state','')})",
        f"  H. Liquidity:  {bd.get('H_liquidity',0):>3}/5   {'(chain)' if opp.get('chain_available') else '(est)'}",
        f"  I. Event:      {bd.get('I_event_penalty',0):>3}   (auto: {opp.get('event_risk',0)})",
        f"  J. DTE:        {bd.get('J_dte',0):>+3}   ({opp.get('dte',5)}d)",
        f"  {'─'*24}",
        f"  TOTAL:         {score:>3}    Grade {grade}",
    ]
    if itqs["notes"]: lines.append(""); [lines.append(f"  {n}") for n in itqs["notes"]]
    lines.append(div)
    return "\n".join(lines)


def format_scorecard(result):
    """Format score_trade() result as full scorecard."""
    if "error" in result: return f"❌ {result['error']}"
    itqs = result["itqs"]; bd = itqs["breakdown"]; div = "━" * 34

    if result["trade_type"] == "bull_put":
        strikes = f"{result['short_strike']}/{result['long_strike']}"
        ttype = "Bull Put Spread"
    else:
        strikes = f"{result['short_strike']}/{result['long_strike']}"
        ttype = "Bear Call Spread"

    chain_tag = "chain context" if result.get("chain_available") else "no chain"
    lines = [
        f"📋 INCOME TRADE SCORECARD", div,
        f"Underlying:   {result['ticker']}",
        f"Trade Type:   {ttype}",
        f"Strikes:      {strikes}",
        f"Credit:       ${result['credit']:.2f}  (your entry)",
        f"Width:        ${result['width']:.2f}",
        f"Break-even:   ${result['breakeven']:.2f}",
        f"ROC on Risk:  {result['roc_pct']:.1f}%",
        f"DTE:          {result['dte']}  (exp: {result.get('expiry','?')})",
        f"Chain:        {chain_tag}", div,
    ]
    blocks = result.get("hard_blocks", [])
    if blocks:
        lines.append("🚫 HARD BLOCKS:"); [lines.append(f"  ✗ {b}") for b in blocks]; lines.append(div)

    lines += [
        f"A. Regime:     {bd.get('A_regime',0):>3}/15  ({result['regime']})",
        f"B. Weekly:     {bd.get('B_weekly',0):>3}/15  ({result['weekly_trend']})",
        f"C. Daily:      {bd.get('C_daily',0):>3}/15  ({result['daily_trend']})",
        f"D. Support:    {bd.get('D_support',0):>3}/15  (${result['support_level']:.2f}, {result['support_touches']}T)",
        f"E. Break-even: {bd.get('E_breakeven',0):>3}/15  (${result['breakeven']:.2f} vs fail ${result['failure_level']:.2f})",
        f"F. Cushion:    {bd.get('F_cushion',0):>3}/10  ({itqs['spot_to_strike_pct']}% spot→strike)",
        f"G. Technical:  {bd.get('G_technical',0):>3}/10  (RSI {result['rsi']} | {result['vol_state']} | VWAP {result['vwap']})",
        f"H. Liquidity:  {bd.get('H_liquidity',0):>3}/5   (auto: {'chain' if result.get('chain_available') else 'est'})",
        f"I. Event:      {bd.get('I_event_penalty',0):>3}   (auto: {result.get('event_risk',0)})",
        f"J. DTE:        {bd.get('J_dte',0):>+3}   ({result['dte']}d)",
        f"K. Flow:       {bd.get('K_flow',0):>+3}   (institutional)",
        f"{'─'*28}",
        f"TOTAL:         {itqs['score']:>3}/100",
        f"GRADE:         {itqs['grade']}",
        f"DECISION:      {itqs['decision']}", "",
    ]
    if itqs.get("recommended_expiry") and itqs.get("expiry_note"):
        lines.append(f"🧭 {itqs['expiry_note']}")
        lines.append("")
    if itqs["notes"]: [lines.append(f"  {n}") for n in itqs["notes"]]
    lines.append(div)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# FULL SCAN
# ═══════════════════════════════════════════════════════════

def run_income_scan(regime_package, ohlcv_fn=None, tickers=None, notify_fn=None,
                    chain_fn=None, expirations_fn=None, flow_fn=None):
    """Fully automated scan across all income tickers."""
    tickers = tickers or INCOME_TICKERS
    all_opps = []

    for ticker in tickers:
        log.info(f"Income scan: {ticker}")
        opps = scan_ticker_income(ticker, regime_package, ohlcv_fn=ohlcv_fn,
                                  chain_fn=chain_fn, expirations_fn=expirations_fn,
                                  flow_fn=flow_fn)
        all_opps.extend(opps)
        if notify_fn:
            for opp in opps:
                if opp["itqs"]["grade"] in ("A+", "A") and not opp.get("hard_blocks"):
                    try: notify_fn(format_income_alert(opp))
                    except Exception as e: log.error(f"Alert failed: {e}")

    all_opps.sort(key=lambda x: x["itqs"]["score"], reverse=True)

    if notify_fn:
        passing = [o for o in all_opps if o["itqs"]["grade"] in ("A+","A","B","C") and not o.get("hard_blocks")]
        core = regime_package.get("core_regime", "?")

        if passing:
            lines = [f"📊 INCOME SCAN | {core}", "━" * 28]
            for opp in passing[:10]:
                g = opp["itqs"]["grade"]; s = opp["itqs"]["score"]
                emoji = {"A+":"🟢","A":"🟢","B":"🟡","C":"🟠"}.get(g, "⚪")
                typ = "PUT" if opp["trade_type"] == "bull_put" else "CALL"
                ct = "📡" if opp.get("chain_available") else "📊"
                lines.append(f"{emoji} {opp['ticker']} {typ} {opp['short_strike']}/{opp.get('long_strike','')} "
                           f"${opp.get('credit',0):.2f} ({opp.get('roc_pct',0):.0f}% ROC) "
                           f"{g}={s} {ct}")
            try: notify_fn("\n".join(lines))
            except Exception: pass
        else:
            # Nothing passed — explain why
            blocked = [o for o in all_opps if o.get("hard_blocks")]
            scored_well_but_blocked = [o for o in blocked
                                       if o["itqs"]["grade"] in ("A+", "A", "B", "C")]
            low_scoring = [o for o in all_opps if not o.get("hard_blocks")
                          and o["itqs"]["grade"] == "F"]
            top3 = all_opps[:3] if all_opps else []

            lines = [
                f"📊 INCOME SCAN | {core}",
                "━" * 28,
                f"Scanned {len(tickers)} tickers — {len(all_opps)} opportunities evaluated",
            ]

            if scored_well_but_blocked:
                lines.append(f"🚫 {len(scored_well_but_blocked)} scored B or better but HARD-BLOCKED")
            if low_scoring:
                lines.append(f"❌ {len(low_scoring)} scored below C (65/100)")
            if blocked and not scored_well_but_blocked:
                lines.append(f"🚫 {len(blocked)} hard-blocked")

            if top3:
                lines.append("")
                lines.append("Top scores:")
                for opp in top3:
                    s = opp["itqs"]["score"]; g = opp["itqs"]["grade"]
                    typ = "PUT" if opp["trade_type"] == "bull_put" else "CALL"
                    block_reasons = opp.get("hard_blocks", [])
                    if block_reasons:
                        # Show first block reason, truncated
                        reason = block_reasons[0][:50]
                        lines.append(f"  {opp['ticker']} {typ} @${opp['short_strike']:.2f} — "
                                   f"{g}={s} 🚫 {reason}")
                    else:
                        lines.append(f"  {opp['ticker']} {typ} @${opp['short_strike']:.2f} — "
                                   f"{g}={s} ({opp['itqs']['decision']})")

            lines.append("")
            if core in ("BEAR_CRISIS", "BEAR_TRANSITION"):
                lines.append(f"Regime {core} — bull put scores suppressed, call spreads blocked on structure")
            else:
                lines.append(f"Regime {core} — no qualifying opportunities this scan")
            try: notify_fn("\n".join(lines))
            except Exception: pass

    log.info(f"Income scan: {len(all_opps)} opps across {len(tickers)} tickers")
    return all_opps
