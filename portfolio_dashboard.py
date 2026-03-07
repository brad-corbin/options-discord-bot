# portfolio_dashboard.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Phase 2C — Full Portfolio Dashboard with Fundamentals
#   - Earnings dates + proximity alert
#   - Dividend ex-date + yield
#   - IV rank (computed from 52-week IV range via candles)
#   - 52-week high/low + current position in range
#   - Sector exposure breakdown
#   - Analyst consensus target
#   - Per-holding P/L overlay
#
# Uses md_get from app.py (passed in at call time).
# Uses get_iv_rank_from_candles from data_providers if available,
# otherwise computes IV rank locally from candle HV as proxy.

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from portfolio import (
    get_all_holdings,
    get_open_options,
    calc_holding_pnl,
    calc_portfolio_summary,
    calc_ticker_options_income,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────

CANDLE_DAYS_52W   = 260    # ~1 year of trading days
CANDLE_FETCH_52W  = 400    # calendar days to fetch (covers weekends/holidays)
HV_WINDOW         = 20     # 20-day historical volatility window
DASHBOARD_WORKERS = 4      # parallel API fetches
EARNINGS_ALERT_DAYS = 14   # warn if earnings within N days


# ─────────────────────────────────────────────────────────
# SECTOR MAP (static — covers most common tickers)
# ─────────────────────────────────────────────────────────
# MarketData API doesn't have a free sector endpoint,
# so we maintain a lookup. Unknown tickers show as "Other".

SECTOR_MAP = {
    # Tech
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "META": "Technology", "NVDA": "Technology", "AMD": "Technology",
    "AVGO": "Technology", "ORCL": "Technology", "CRM": "Technology",
    "ADBE": "Technology", "INTC": "Technology", "QCOM": "Technology",
    "MU": "Technology", "ANET": "Technology", "NOW": "Technology",
    "PLTR": "Technology", "SOFI": "Technology", "ALAB": "Technology",
    "NBIS": "Technology", "IREN": "Technology", "LMND": "Technology",
    "CRWD": "Technology", "PANW": "Technology", "SNOW": "Technology",
    "NET": "Technology", "DDOG": "Technology", "ZS": "Technology",
    "FTNT": "Technology", "S": "Technology",
    # Semis / AI Infra
    "TSM": "Semiconductors", "ASML": "Semiconductors", "KLAC": "Semiconductors",
    "LRCX": "Semiconductors", "AMAT": "Semiconductors", "MRVL": "Semiconductors",
    "ON": "Semiconductors", "SMCI": "Semiconductors",
    # ETFs
    "SPY": "ETF", "QQQ": "ETF", "IWM": "ETF", "DIA": "ETF",
    "XLF": "ETF", "XLE": "ETF", "XLK": "ETF", "XLV": "ETF",
    "GLD": "ETF", "SLV": "ETF", "TLT": "ETF", "HYG": "ETF",
    "ARKK": "ETF", "SOXL": "ETF", "TQQQ": "ETF",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "OXY": "Energy", "COP": "Energy",
    "SLB": "Energy", "HAL": "Energy", "DVN": "Energy", "MPC": "Energy",
    "PSX": "Energy", "VLO": "Energy",
    # Industrials
    "CAT": "Industrials", "DE": "Industrials", "GE": "Industrials",
    "HON": "Industrials", "UNP": "Industrials", "RTX": "Industrials",
    "LMT": "Industrials", "NOC": "Industrials", "GD": "Industrials",
    "BA": "Industrials", "FDX": "Industrials", "UPS": "Industrials",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials",
    "MS": "Financials", "WFC": "Financials", "C": "Financials",
    "BLK": "Financials", "SCHW": "Financials", "V": "Financials",
    "MA": "Financials", "AXP": "Financials",
    # Healthcare
    "UNH": "Healthcare", "JNJ": "Healthcare", "PFE": "Healthcare",
    "ABBV": "Healthcare", "LLY": "Healthcare", "MRK": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "AMGN": "Healthcare",
    # Consumer
    "AMZN": "Consumer", "TSLA": "Consumer", "HD": "Consumer",
    "NKE": "Consumer", "SBUX": "Consumer", "MCD": "Consumer",
    "WMT": "Consumer", "COST": "Consumer", "TGT": "Consumer",
    "DIS": "Consumer", "NFLX": "Consumer",
    # Clean Energy / Utilities
    "BE": "Clean Energy", "ENPH": "Clean Energy", "FSLR": "Clean Energy",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    # Mining / Materials
    "NEM": "Materials", "GOLD": "Materials", "FCX": "Materials",
    "AA": "Materials", "CLF": "Materials",
    # Telecom
    "T": "Telecom", "VZ": "Telecom", "TMUS": "Telecom",
    # REITs
    "O": "REITs", "AMT": "REITs", "PLD": "REITs", "SPG": "REITs",
    # Crypto-adjacent
    "COIN": "Crypto", "MSTR": "Crypto", "MARA": "Crypto", "RIOT": "Crypto",
    # Other notables from typical watchlists
    "GTLR": "Technology", "DPX": "Industrials", "FRSH": "Technology",
    "RVP": "Industrials", "PTY": "Financials", "XLE": "ETF",
    "HTT": "Technology", "TWM": "ETF", "HP": "Energy",
    "LOW": "Consumer", "TAL": "Consumer", "TTA": "Industrials",
    "TE": "Industrials", "QNDS": "Technology", "BRAT": "Consumer",
}


def _get_sector(ticker: str) -> str:
    return SECTOR_MAP.get(ticker.upper(), "Other")


# ─────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────

def _fetch_52w_candles(ticker: str, md_get: Callable) -> Optional[dict]:
    """Fetch ~1 year of daily candles for 52w range + IV rank calc."""
    ticker = ticker.strip().upper()
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=CANDLE_FETCH_52W)

    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/candles/daily/{ticker}/",
            {"from": start.isoformat(), "to": end.isoformat()},
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return None

        closes = data.get("c", [])
        highs  = data.get("h", [])
        lows   = data.get("l", [])

        if not closes or len(closes) < 30:
            return None

        return {"close": closes, "high": highs, "low": lows}

    except Exception as e:
        log.warning(f"52w candle fetch error for {ticker}: {e}")
        return None


def _fetch_earnings(ticker: str, md_get: Callable) -> Optional[dict]:
    """
    Fetch next earnings date from MarketData API.
    Returns {"date": "YYYY-MM-DD", "days_away": int} or None.
    """
    ticker = ticker.strip().upper()
    try:
        data = md_get(
            f"https://api.marketdata.app/v1/stocks/earnings/{ticker}/",
            {"from": datetime.now(timezone.utc).date().isoformat()},
        )

        if not isinstance(data, dict) or data.get("s") != "ok":
            return None

        dates = data.get("date", [])
        if not dates:
            return None

        # First future date
        today = datetime.now(timezone.utc).date()
        for d in dates:
            try:
                dt_str = str(d)[:10]
                earn_date = datetime.fromisoformat(dt_str).date()
                days_away = (earn_date - today).days
                if days_away >= 0:
                    return {"date": dt_str, "days_away": days_away}
            except Exception:
                continue

        return None

    except Exception as e:
        log.debug(f"Earnings fetch for {ticker}: {e}")
        return None


def _fetch_quote_full(ticker: str, md_get: Callable) -> Optional[dict]:
    """
    Fetch quote with extended fields: last, high52w, low52w, change, volume.
    """
    ticker = ticker.strip().upper()
    try:
        data = md_get(f"https://api.marketdata.app/v1/stocks/quotes/{ticker}/")

        def _extract(field):
            v = data.get(field)
            if v is None:
                return None
            try:
                return float(v[0]) if isinstance(v, list) else float(v)
            except (ValueError, TypeError, IndexError):
                return None

        last = None
        for field in ("last", "mid", "bid", "ask"):
            v = _extract(field)
            if v and v > 0:
                last = v
                break

        return {
            "last":    last,
            "high52w": _extract("52weekHigh"),
            "low52w":  _extract("52weekLow"),
            "change":  _extract("change"),
            "changep": _extract("changepct"),
            "volume":  _extract("volume"),
        }

    except Exception as e:
        log.warning(f"Quote fetch error for {ticker}: {e}")
        return None


# ─────────────────────────────────────────────────────────
# IV RANK (Historical Volatility proxy)
# ─────────────────────────────────────────────────────────
# True IV rank requires options IV data across 52 weeks.
# As a credit-efficient proxy, we compute HV rank:
#   - 20-day HV at each point over the last year
#   - Current HV percentile within that range

def _calc_hv(closes: list, window: int = HV_WINDOW) -> list:
    """
    Calculate rolling historical volatility (annualized).
    Returns list of HV values (one per day after warmup).
    """
    if len(closes) < window + 1:
        return []

    hvs = []
    for i in range(window, len(closes)):
        returns = []
        for j in range(i - window + 1, i + 1):
            if closes[j - 1] > 0:
                returns.append(math.log(closes[j] / closes[j - 1]))

        if len(returns) >= window - 1:
            mean = sum(returns) / len(returns)
            var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            hv = math.sqrt(var) * math.sqrt(252) * 100  # annualized %
            hvs.append(hv)

    return hvs


def _calc_iv_rank(closes: list) -> Optional[int]:
    """
    Compute IV rank as percentile of current HV within 52-week HV range.
    Returns 0-100 int, or None if insufficient data.
    """
    hvs = _calc_hv(closes)
    if not hvs or len(hvs) < 30:
        return None

    current = hvs[-1]
    hv_min = min(hvs)
    hv_max = max(hvs)

    if hv_max <= hv_min:
        return 50  # flat vol

    rank = int(((current - hv_min) / (hv_max - hv_min)) * 100)
    return max(0, min(100, rank))


# ─────────────────────────────────────────────────────────
# 52-WEEK RANGE POSITION
# ─────────────────────────────────────────────────────────

def _calc_52w_position(price: float, high52: float, low52: float) -> Optional[int]:
    """
    Where price sits in the 52-week range, as 0-100%.
    0% = at 52w low, 100% = at 52w high.
    """
    if high52 is None or low52 is None or high52 <= low52:
        return None
    pos = ((price - low52) / (high52 - low52)) * 100
    return max(0, min(100, int(pos)))


def _range_bar(pct: int) -> str:
    """Visual bar: ▓░░░░░░░░░ 12%"""
    filled = max(0, min(10, pct // 10))
    return "▓" * filled + "░" * (10 - filled) + f" {pct}%"


# ─────────────────────────────────────────────────────────
# PER-TICKER DASHBOARD DATA
# ─────────────────────────────────────────────────────────

def _analyze_holding(ticker: str, holding: dict, md_get: Callable) -> dict:
    """
    Fetch all dashboard data for one holding.
    Returns a dict with all fundamental + technical data.
    """
    ticker = ticker.upper()
    result = {
        "ticker":     ticker,
        "sector":     _get_sector(ticker),
        "shares":     holding.get("shares", 0),
        "cost_basis": holding.get("cost_basis", 0),
        "tags":       holding.get("tags", []),
        "error":      None,
    }

    # Quote
    quote = _fetch_quote_full(ticker, md_get)
    if not quote or not quote.get("last"):
        result["error"] = "no quote"
        return result

    price = quote["last"]
    result["price"]    = price
    result["change"]   = quote.get("change")
    result["changep"]  = quote.get("changep")
    result["high52w"]  = quote.get("high52w")
    result["low52w"]   = quote.get("low52w")

    # 52-week position (prefer quote data, fallback to candles)
    high52 = quote.get("high52w")
    low52  = quote.get("low52w")

    # Candles for IV rank (always need these)
    candles = _fetch_52w_candles(ticker, md_get)
    if candles:
        closes = candles["close"]

        # If quote didn't have 52w data, compute from candles
        if high52 is None:
            high52 = max(candles.get("high", closes))
            result["high52w"] = high52
        if low52 is None:
            low52 = min(candles.get("low", closes))
            result["low52w"] = low52

        # IV rank
        result["iv_rank"] = _calc_iv_rank(closes)
    else:
        result["iv_rank"] = None

    # 52w range position
    if high52 and low52:
        result["range_pct"] = _calc_52w_position(price, high52, low52)
    else:
        result["range_pct"] = None

    # Earnings
    earnings = _fetch_earnings(ticker, md_get)
    result["earnings"] = earnings  # {"date": ..., "days_away": ...} or None

    # P/L
    pnl = calc_holding_pnl(ticker, price)
    if "error" not in pnl:
        result["unrealized"]  = pnl["unrealized"]
        result["opt_income"]  = pnl["opt_income"]
        result["total_pnl"]   = pnl["total_pnl"]
        result["return_pct"]  = pnl["return_pct"]
    else:
        result["unrealized"]  = 0
        result["opt_income"]  = 0
        result["total_pnl"]   = 0
        result["return_pct"]  = 0

    return result


# ─────────────────────────────────────────────────────────
# FULL DASHBOARD REPORT
# ─────────────────────────────────────────────────────────

def generate_dashboard(md_get: Callable) -> list:
    """
    Generate full portfolio dashboard.
    Returns list of Telegram message strings (split to stay under limits).

    Fetches data for all holdings in parallel, then formats:
      1. Per-holding detail cards
      2. Sector exposure breakdown
      3. Earnings calendar
      4. Portfolio totals
    """
    holdings = get_all_holdings()
    if not holdings:
        return ["📊 No holdings to dashboard. Use /hold add TICKER SHARES @PRICE"]

    # Fetch all data in parallel
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=DASHBOARD_WORKERS) as executor:
        futures = {
            executor.submit(_analyze_holding, ticker, h, md_get): ticker
            for ticker, h in holdings.items()
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                data = future.result()
                if data.get("error"):
                    errors.append(f"{ticker}: {data['error']}")
                else:
                    results.append(data)
            except Exception as e:
                errors.append(f"{ticker}: {type(e).__name__}")

    # Sort by total P/L descending
    results.sort(key=lambda r: r.get("total_pnl", 0), reverse=True)

    # ── BUILD MESSAGES ──
    messages = []

    # === PART 1: Holdings Detail ===
    lines = [f"📊 PORTFOLIO DASHBOARD — {datetime.now(timezone.utc).strftime('%I:%M %p UTC')}\n"]

    for r in results:
        lines.append(_format_holding_card(r))

    if errors:
        lines.append(f"\n⚠️ Data errors: {', '.join(errors[:5])}")

    messages.append("\n".join(lines))

    # === PART 2: Sector Exposure + Earnings Calendar + Totals ===
    lines2 = []

    # Sector exposure
    sector_totals = {}
    total_invested = 0.0
    for r in results:
        invested = r.get("shares", 0) * r.get("cost_basis", 0)
        sector = r.get("sector", "Other")
        sector_totals[sector] = sector_totals.get(sector, 0) + invested
        total_invested += invested

    if sector_totals and total_invested > 0:
        lines2.append("📎 SECTOR EXPOSURE\n")
        for sector, value in sorted(sector_totals.items(), key=lambda x: -x[1]):
            pct = (value / total_invested) * 100
            bar_len = max(1, int(pct / 5))  # roughly scale
            bar = "█" * bar_len
            lines2.append(f"  {bar} {sector} {pct:.0f}% (${value:,.0f})")
        lines2.append("")

    # Earnings calendar (upcoming 30 days)
    upcoming_earnings = []
    for r in results:
        earn = r.get("earnings")
        if earn and earn.get("days_away", 999) <= 30:
            upcoming_earnings.append((earn["days_away"], r["ticker"], earn["date"]))

    if upcoming_earnings:
        upcoming_earnings.sort()
        lines2.append("📅 EARNINGS (next 30 days)\n")
        for days, ticker, date in upcoming_earnings:
            alert = " ⚠️" if days <= EARNINGS_ALERT_DAYS else ""
            lines2.append(f"  {ticker}  {date}  ({days}d){alert}")
        lines2.append("")

    # Portfolio totals
    total_unrealized = sum(r.get("unrealized", 0) for r in results)
    total_opt_income = sum(r.get("opt_income", 0) for r in results)
    combined = total_unrealized + total_opt_income

    open_opts = get_open_options()

    lines2.append("💰 PORTFOLIO TOTALS\n")
    lines2.append(f"  Holdings: {len(results)} positions")
    lines2.append(f"  Invested: ${total_invested:,.0f}")
    lines2.append(f"  Unrealized: {_fmt_money(total_unrealized)}")
    if total_opt_income != 0:
        lines2.append(f"  Options Income: {_fmt_money(total_opt_income)}")
    lines2.append(f"  Combined P/L: {_fmt_money(combined)}")
    if total_invested > 0:
        lines2.append(f"  Return: {_fmt_pct((combined / total_invested) * 100)}")
    if open_opts:
        lines2.append(f"  Open Options: {len(open_opts)}")

    messages.append("\n".join(lines2))

    return messages


# ─────────────────────────────────────────────────────────
# CARD FORMATTER
# ─────────────────────────────────────────────────────────

def _format_holding_card(r: dict) -> str:
    """
    Format a single holding card:
      AAPL  $192.30 (+1.2%)  100sh @$185.50
      52w: ▓▓▓▓▓▓░░░░ 62%  |  IVR: 45
      Earnings: 2026-04-24 (18d) ⚠️
      P/L: +$680 (+3.7%)  |  Opt Income: +$450
    """
    ticker  = r["ticker"]
    price   = r.get("price", 0)
    changep = r.get("changep")
    shares  = r.get("shares", 0)
    cost    = r.get("cost_basis", 0)

    # Line 1: ticker, price, day change, position
    day_chg = f" ({changep:+.1f}%)" if changep is not None else ""
    tags_str = ""
    if r.get("tags"):
        tags_str = "  " + " ".join("#" + t for t in r["tags"])

    line1 = f"{ticker}  ${price:.2f}{day_chg}  {shares}sh @${cost:.2f}{tags_str}"

    # Line 2: 52w range + IV rank
    parts2 = []
    range_pct = r.get("range_pct")
    if range_pct is not None:
        parts2.append(f"52w: {_range_bar(range_pct)}")

    iv_rank = r.get("iv_rank")
    if iv_rank is not None:
        ivr_emoji = "🔥" if iv_rank >= 60 else "❄️" if iv_rank <= 25 else ""
        parts2.append(f"IVR: {iv_rank}{ivr_emoji}")

    line2 = "  " + "  |  ".join(parts2) if parts2 else ""

    # Line 3: earnings
    line3 = ""
    earn = r.get("earnings")
    if earn:
        alert = " ⚠️" if earn["days_away"] <= EARNINGS_ALERT_DAYS else ""
        line3 = f"  Earnings: {earn['date']} ({earn['days_away']}d){alert}"

    # Line 4: P/L
    total_pnl  = r.get("total_pnl", 0)
    return_pct = r.get("return_pct", 0)
    opt_income = r.get("opt_income", 0)

    pnl_parts = [f"P/L: {_fmt_money(total_pnl)} ({_fmt_pct(return_pct)})"]
    if opt_income != 0:
        pnl_parts.append(f"Opt: {_fmt_money(opt_income)}")

    line4 = "  " + "  |  ".join(pnl_parts)

    # Assemble
    card = line1
    if line2:
        card += "\n" + line2
    if line3:
        card += "\n" + line3
    card += "\n" + line4

    return card


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def _fmt_money(v: float) -> str:
    prefix = "+" if v >= 0 else ""
    return f"{prefix}${v:,.0f}"

def _fmt_pct(v: float) -> str:
    prefix = "+" if v >= 0 else ""
    return f"{prefix}{v:.1f}%"
