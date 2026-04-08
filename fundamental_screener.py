# fundamental_screener.py
# ═══════════════════════════════════════════════════════════════════
# Fundamental Data Layer + Peter Lynch Stock Classification
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Nightly job that fetches fundamental data for the watchlist and
# scores each ticker using value, growth, and quality metrics.
#
# Data sources:
#   - Financial Modeling Prep (FMP) — free tier: EPS, revenue, PEG,
#     debt ratios, FCF, market cap, sector, industry
#   - Finnhub (existing token) — insider transactions, recommendation
#     trends, institutional ownership
#   - Yahoo Finance — short interest (free)
#
# Lynch stock categories:
#   FAST_GROWER   — EPS growth 20%+, revenue growth 15%+
#   STALWART      — EPS growth 8-20%, large cap, stable
#   SLOW_GROWER   — EPS growth < 8%, dividend payer
#   CYCLICAL      — earnings follow economic cycles
#   TURNAROUND    — negative earnings trending positive
#   ASSET_PLAY    — significant hidden asset value
#
# Zero MarketData API calls. FMP cached 24hrs, Finnhub cached 12hrs.
#
# Usage:
#   from fundamental_screener import get_fundamentals, classify_lynch
#   data = get_fundamentals("NVDA")
#   lynch = classify_lynch(data)
# ═══════════════════════════════════════════════════════════════════

import os
import time
import logging
import threading
import math
from datetime import datetime, timezone
from typing import Dict, Optional, List

import requests

log = logging.getLogger(__name__)

FMP_TOKEN = os.getenv("FMP_TOKEN", "").strip()
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "").strip()

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
CACHE_TTL_FUNDAMENTALS = 86400   # 24 hours
CACHE_TTL_INSIDER = 43200        # 12 hours

FMP_BASE = "https://financialmodelingprep.com/stable"
FINNHUB_BASE = "https://finnhub.io/api/v1"

# v5.0: ETFs don't have EPS/revenue/PEG — skip FMP entirely
# and return lynch_category="ETF" with no confidence penalty.
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "SPX",      # broad index
    "SOXX", "SMH", "XBI", "IBB",              # sector/thematic
    "XLF", "XLE", "XLV", "XLK", "XLI",       # SPDR sectors
    "XLP", "XLU", "XLY", "XLC", "XLRE", "XLB",
    "GLD", "SLV", "TLT", "HYG", "LQD",       # commodities/bonds
    "ITA", "ARKK", "ARKG", "ARKW",            # thematic
    "EEM", "EFA", "VTI", "VOO", "VTV",        # broad market
    "KWEB", "FXI", "BITO", "MSTR",            # other ETFs
}

PRECHAIN_MIN_FUNDAMENTAL_SCORE = 30  # swing trades need >= 30/100


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        value, ts, ttl = entry
        if time.time() - ts > ttl:
            del _cache[key]
            return None
        return value


def _cache_set(key, value, ttl=CACHE_TTL_FUNDAMENTALS):
    with _cache_lock:
        _cache[key] = (value, time.time(), ttl)


# ── FMP Data Fetchers ──

def _fmp_get(endpoint: str, params: dict = None) -> Optional[dict]:
    if not FMP_TOKEN:
        return None
    try:
        params = params or {}
        params["apikey"] = FMP_TOKEN
        resp = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        # v5.0: Sanitize API key from error logs
        err_str = str(e)
        if FMP_TOKEN:
            err_str = err_str.replace(FMP_TOKEN, "***")
        log.warning(f"FMP {endpoint} error: {err_str}")
        return None


def _fetch_fmp_profile(ticker: str) -> Dict:
    """Company profile: sector, industry, market cap, beta, etc."""
    data = _fmp_get("profile", {"symbol": ticker})
    if data:
        p = data[0] if isinstance(data, list) else data
        if isinstance(p, dict):
            return {
                "sector": p.get("sector", ""),
                "industry": p.get("industry", ""),
                "market_cap": p.get("mktCap", 0),
                "beta": p.get("beta", 1.0),
                "dividend_yield": p.get("lastDiv", 0),
                "description": (p.get("description") or "")[:200],
                "exchange": p.get("exchangeShortName", ""),
                "ipo_date": p.get("ipoDate", ""),
                "is_etf": p.get("isEtf", False),
            }
    return {}


def _fetch_fmp_ratios(ticker: str) -> Dict:
    """Key financial ratios: PEG, P/E, debt/equity, ROE, FCF yield."""
    data = _fmp_get("ratios-ttm", {"symbol": ticker})
    if data:
        r = data[0] if isinstance(data, list) else data
        if isinstance(r, dict):
            return {
                "peg_ratio": r.get("pegRatioTTM"),
                "pe_ratio": r.get("peRatioTTM"),
                "price_to_sales": r.get("priceToSalesRatioTTM"),
                "price_to_book": r.get("priceToBookRatioTTM"),
                "debt_to_equity": r.get("debtEquityRatioTTM"),
                "roe": r.get("returnOnEquityTTM"),
                "roa": r.get("returnOnAssetsTTM"),
                "current_ratio": r.get("currentRatioTTM"),
                "fcf_yield": r.get("freeCashFlowYieldTTM"),
                "dividend_yield_ttm": r.get("dividendYieldTTM"),
            }
    return {}


def _fetch_fmp_growth(ticker: str) -> Dict:
    """Income statement growth: EPS growth, revenue growth."""
    data = _fmp_get("income-statement-growth", {"symbol": ticker, "period": "annual", "limit": 3})
    if isinstance(data, list) and data:
        latest = data[0]
        prev = data[1] if len(data) > 1 else {}
        return {
            "eps_growth_yoy": latest.get("growthEPS"),
            "revenue_growth_yoy": latest.get("growthRevenue"),
            "net_income_growth_yoy": latest.get("growthNetIncome"),
            "eps_growth_prev_year": prev.get("growthEPS"),
            "revenue_growth_prev_year": prev.get("growthRevenue"),
            # Consistency: both years positive growth
            "consistent_growth": (
                (latest.get("growthEPS") or 0) > 0.05 and
                (prev.get("growthEPS") or 0) > 0.05
            ),
        }
    return {}


# ── Finnhub Data Fetchers ──

def _fetch_insider_sentiment(ticker: str) -> Dict:
    """Net insider buying/selling from Finnhub."""
    if not FINNHUB_TOKEN:
        return {}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/insider-sentiment",
            params={"symbol": ticker, "from": "2024-01-01", "token": FINNHUB_TOKEN},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data:
            return {"insider_net_90d": "neutral", "insider_mspr": 0}

        # MSPR = Monthly Share Purchase Ratio (positive = net buying)
        recent = data[-3:] if len(data) >= 3 else data
        avg_mspr = sum(d.get("mspr", 0) for d in recent) / len(recent)

        if avg_mspr > 5:
            sentiment = "strong_buying"
        elif avg_mspr > 0:
            sentiment = "buying"
        elif avg_mspr > -5:
            sentiment = "neutral"
        else:
            sentiment = "selling"

        return {"insider_net_90d": sentiment, "insider_mspr": round(avg_mspr, 2)}
    except Exception as e:
        log.debug(f"Insider sentiment failed for {ticker}: {e}")
        return {"insider_net_90d": "unknown", "insider_mspr": 0}


def _fetch_recommendation_trends(ticker: str) -> Dict:
    """Analyst consensus from Finnhub."""
    if not FINNHUB_TOKEN:
        return {}
    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/stock/recommendation",
            params={"symbol": ticker, "token": FINNHUB_TOKEN},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return {}

        latest = data[0]
        buy = (latest.get("buy", 0) or 0) + (latest.get("strongBuy", 0) or 0)
        sell = (latest.get("sell", 0) or 0) + (latest.get("strongSell", 0) or 0)
        hold = latest.get("hold", 0) or 0
        total = buy + sell + hold

        if total == 0:
            return {"analyst_consensus": "none", "analyst_buy_pct": 0}

        buy_pct = round(buy / total * 100, 1)
        if buy_pct >= 70:
            consensus = "strong_buy"
        elif buy_pct >= 50:
            consensus = "buy"
        elif sell / total > 0.3:
            consensus = "sell"
        else:
            consensus = "hold"

        return {
            "analyst_consensus": consensus,
            "analyst_buy_pct": buy_pct,
            "analyst_total": total,
        }
    except Exception as e:
        log.debug(f"Recommendation trends failed for {ticker}: {e}")
        return {}


# ── Composite Functions ──

def get_fundamentals(ticker: str) -> Dict:
    """
    Fetch all fundamental data for a ticker. Cached 24 hours.

    Returns a comprehensive dict with profile, ratios, growth, and
    insider/analyst data, plus a composite fundamental_score 0-100.

    v5.0: ETFs skip FMP entirely (no EPS/revenue/PEG for ETFs).
    Returns lynch_category="ETF" with confidence_boost=0.
    """
    ticker = ticker.strip().upper()
    cache_key = f"fund:{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # v5.0: ETF detection — skip FMP, no Lynch penalty
    if ticker in KNOWN_ETFS:
        result = {
            "ticker": ticker,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "is_etf": True,
            "fundamental_score": 60,  # neutral — no penalty, no boost
            "lynch_category": {
                "category": "ETF",
                "peg_signal": "N/A",
                "growth_quality": "N/A",
                "risk_flag": None,
                "dte_preference": "0-21",
                "confidence_boost": 0,  # no penalty, no boost
            },
        }
        # Still fetch analyst/insider data from Finnhub if available
        try:
            result.update(_fetch_recommendation_trends(ticker))
        except Exception:
            pass
        _cache_set(cache_key, result, CACHE_TTL_FUNDAMENTALS)
        log.info(f"Fundamentals for {ticker}: ETF — skipping FMP, score=60, lynch=ETF")
        return result

    profile = _fetch_fmp_profile(ticker)
    ratios = _fetch_fmp_ratios(ticker)
    growth = _fetch_fmp_growth(ticker)
    insider = _fetch_insider_sentiment(ticker)
    analyst = _fetch_recommendation_trends(ticker)

    result = {
        "ticker": ticker,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **profile, **ratios, **growth, **insider, **analyst,
    }

    # Compute composite fundamental score (0-100)
    result["fundamental_score"] = _compute_score(result)
    result["lynch_category"] = classify_lynch(result)

    _cache_set(cache_key, result, CACHE_TTL_FUNDAMENTALS)
    log.info(f"Fundamentals for {ticker}: score={result['fundamental_score']}/100, "
             f"lynch={result['lynch_category']['category']}, "
             f"peg={result.get('peg_ratio', '?')}")
    return result


def _compute_score(d: Dict) -> int:
    """Composite fundamental score 0-100."""
    score = 50  # baseline

    # PEG ratio (Lynch's golden metric)
    peg = d.get("peg_ratio")
    if peg is not None and peg > 0:
        if peg < 0.8:
            score += 15
        elif peg < 1.0:
            score += 12
        elif peg < 1.5:
            score += 5
        elif peg > 2.5:
            score -= 10
        elif peg > 3.0:
            score -= 15

    # EPS growth
    eps_g = d.get("eps_growth_yoy")
    if eps_g is not None:
        if eps_g > 0.50:
            score += 12
        elif eps_g > 0.25:
            score += 10
        elif eps_g > 0.10:
            score += 5
        elif eps_g < -0.10:
            score -= 10
        elif eps_g < -0.30:
            score -= 20

    # Revenue growth
    rev_g = d.get("revenue_growth_yoy")
    if rev_g is not None:
        if rev_g > 0.25:
            score += 8
        elif rev_g > 0.10:
            score += 4
        elif rev_g < -0.10:
            score -= 8

    # Growth consistency
    if d.get("consistent_growth"):
        score += 5

    # Debt-to-equity
    dte_ratio = d.get("debt_to_equity")
    if dte_ratio is not None:
        if dte_ratio < 0.3:
            score += 5
        elif dte_ratio > 1.5:
            score -= 5
        elif dte_ratio > 3.0:
            score -= 10

    # ROE
    roe = d.get("roe")
    if roe is not None:
        if roe > 0.25:
            score += 5
        elif roe < 0:
            score -= 5

    # FCF yield
    fcf = d.get("fcf_yield")
    if fcf is not None and fcf > 0.04:
        score += 3

    # Insider sentiment
    insider = d.get("insider_net_90d", "")
    if insider == "strong_buying":
        score += 8
    elif insider == "buying":
        score += 4
    elif insider == "selling":
        score -= 5

    # Analyst consensus
    consensus = d.get("analyst_consensus", "")
    if consensus == "strong_buy":
        score += 5
    elif consensus == "sell":
        score -= 5

    return max(0, min(100, score))


def classify_lynch(d: Dict) -> Dict:
    """
    Classify stock using Peter Lynch's framework.

    Returns:
        {
            "category": str,            # FAST_GROWER, STALWART, etc.
            "options_strategy": str,     # recommended spread type
            "dte_preference": str,       # DTE range
            "confidence_boost": int,     # points to add to trade confidence
            "peg_signal": str,           # BUY / HOLD / AVOID
            "description": str,
        }
    """
    eps_g = d.get("eps_growth_yoy") or 0
    rev_g = d.get("revenue_growth_yoy") or 0
    peg = d.get("peg_ratio")
    mcap = d.get("market_cap", 0)
    roe = d.get("roe") or 0
    is_etf = d.get("is_etf", False)

    # ETFs don't get classified
    if is_etf:
        return {
            "category": "ETF", "options_strategy": "debit_spread",
            "dte_preference": "0-10", "confidence_boost": 0,
            "peg_signal": "N/A", "description": "ETF — use technical analysis only",
        }

    # Fast Grower: high growth, any size
    if eps_g > 0.20 and rev_g > 0.15:
        cat = "FAST_GROWER"
        strat = "swing_debit_spread"
        dte_pref = "21-45"
        boost = 12
        desc = f"Fast grower (EPS +{eps_g:.0%}, Rev +{rev_g:.0%}). Swing debit spreads on pullbacks."

    # Stalwart: moderate growth, typically large cap
    elif 0.05 < eps_g <= 0.20 and mcap > 10e9:
        cat = "STALWART"
        strat = "pullback_debit_spread"
        dte_pref = "14-30"
        boost = 8
        desc = f"Stalwart (EPS +{eps_g:.0%}). Reliable grower — trade on support retests."

    # Turnaround: negative but improving
    elif eps_g > 0 and d.get("eps_growth_prev_year") and (d.get("eps_growth_prev_year") or 0) < 0:
        cat = "TURNAROUND"
        strat = "longer_dte_debit_spread"
        dte_pref = "30-60"
        boost = 5
        desc = "Turnaround — earnings recovering. Needs longer DTE to develop."

    # Slow Grower: low growth
    elif eps_g <= 0.05 and eps_g >= 0:
        cat = "SLOW_GROWER"
        strat = "avoid_or_sell_premium"
        dte_pref = "N/A"
        boost = -8
        desc = f"Slow grower (EPS +{eps_g:.0%}). Poor options candidate — theta eats gains."

    # Negative growth
    elif eps_g < 0:
        cat = "DECLINING"
        strat = "bear_spread_or_avoid"
        dte_pref = "7-21"
        boost = -12
        desc = f"Declining earnings (EPS {eps_g:.0%}). Bear spreads only, or avoid."

    else:
        cat = "UNCLASSIFIED"
        strat = "debit_spread"
        dte_pref = "3-14"
        boost = 0
        desc = "Insufficient data for Lynch classification."

    # PEG signal
    if peg is not None and peg > 0:
        if peg < 1.0:
            peg_signal = "BUY"
            boost += 5
        elif peg < 1.5:
            peg_signal = "HOLD"
        elif peg < 2.5:
            peg_signal = "EXPENSIVE"
            boost -= 3
        else:
            peg_signal = "AVOID"
            boost -= 8
    else:
        peg_signal = "N/A"

    return {
        "category": cat,
        "options_strategy": strat,
        "dte_preference": dte_pref,
        "confidence_boost": boost,
        "peg_signal": peg_signal,
        "description": desc,
    }


def get_swing_confidence_adjustments(ticker: str) -> Dict:
    """
    Get confidence adjustments for swing trades based on fundamentals.
    For integration with swing_engine.py confidence scoring.
    """
    data = get_fundamentals(ticker)
    lynch = data.get("lynch_category", {})

    return {
        "fundamental_score": data.get("fundamental_score", 50),
        "lynch_category": lynch.get("category", "UNCLASSIFIED"),
        "lynch_boost": lynch.get("confidence_boost", 0),
        "peg_signal": lynch.get("peg_signal", "N/A"),
        "insider_signal": data.get("insider_net_90d", "unknown"),
        "analyst_consensus": data.get("analyst_consensus", "none"),
        "description": lynch.get("description", ""),
    }


def batch_fetch_fundamentals(tickers: List[str]) -> Dict[str, Dict]:
    """
    Fetch fundamentals for multiple tickers. Respects FMP rate limits.
    Designed for nightly batch runs.
    """
    results = {}
    for i, ticker in enumerate(tickers):
        try:
            results[ticker] = get_fundamentals(ticker)
            # FMP free tier: 250 calls/day, so pace ourselves
            if i > 0 and i % 5 == 0:
                time.sleep(1)
        except Exception as e:
            log.warning(f"Batch fundamental fetch failed for {ticker}: {e}")
            results[ticker] = {"ticker": ticker, "fundamental_score": 50, "error": str(e)}
    return results
