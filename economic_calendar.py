# economic_calendar.py
# ═══════════════════════════════════════════════════════════════════
# Economic Calendar — Macro Event Awareness
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Detects high-impact macro events (FOMC, CPI, NFP, GDP, PCE, PPI,
# jobless claims) within the DTE window. Uses Finnhub economic
# calendar API (free tier).
#
# Zero MarketData API calls. Finnhub cached 6 hours.
#
# Usage:
#   from economic_calendar import get_events_in_window, has_high_impact_today
#   events = get_events_in_window(dte_days=5)
#   if has_high_impact_today():
#       reduce_0dte_sizing()
# ═══════════════════════════════════════════════════════════════════

import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "").strip()
FINNHUB_BASE = "https://finnhub.io/api/v1"

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 21600  # 6 hours — calendar doesn't change intraday

# High-impact event keywords (Finnhub event field matching)
HIGH_IMPACT_KEYWORDS = [
    "FOMC", "Federal Funds Rate", "Interest Rate Decision",
    "CPI", "Consumer Price Index",
    "Non-Farm Payrolls", "NFP", "Nonfarm",
    "GDP", "Gross Domestic Product",
    "PCE", "Personal Consumption",
    "PPI", "Producer Price Index",
    "Unemployment Rate", "Jobless Claims",
    "Retail Sales",
    "ISM Manufacturing", "ISM Services",
    "Treasury", "Auction",
]

MEDIUM_IMPACT_KEYWORDS = [
    "Housing Starts", "Building Permits",
    "Durable Goods", "Industrial Production",
    "Consumer Confidence", "Michigan",
    "Trade Balance", "Import",
    "Beige Book", "Fed Chair",
    "Existing Home Sales", "New Home Sales",
]


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        value, ts = entry
        if time.time() - ts > CACHE_TTL:
            del _cache[key]
            return None
        return value


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.time())


def _classify_impact(event_name: str) -> str:
    """Classify event impact: high / medium / low."""
    name_upper = event_name.upper()
    for kw in HIGH_IMPACT_KEYWORDS:
        if kw.upper() in name_upper:
            return "high"
    for kw in MEDIUM_IMPACT_KEYWORDS:
        if kw.upper() in name_upper:
            return "medium"
    return "low"


def _fetch_economic_calendar(from_date: str, to_date: str) -> List[Dict]:
    """Fetch economic calendar from Finnhub. Returns list of events."""
    if not FINNHUB_TOKEN:
        log.debug("FINNHUB_TOKEN not set — economic calendar disabled")
        return []

    cache_key = f"econ:{from_date}:{to_date}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/calendar/economic",
            params={"from": from_date, "to": to_date, "token": FINNHUB_TOKEN},
            timeout=5,
        )
        # Handle 403 (paid feature) gracefully — don't retry, cache empty for longer
        if resp.status_code == 403:
            log.warning("Economic calendar: Finnhub returned 403 (paid feature). "
                        "Macro event detection disabled. Consider upgrading Finnhub plan.")
            _cache_set(cache_key, [])
            return []
        resp.raise_for_status()
        data = resp.json()

        events = []
        raw_events = data.get("economicCalendar") or []
        for evt in raw_events:
            if evt.get("country", "").upper() != "US":
                continue

            event_name = evt.get("event", "")
            impact = _classify_impact(event_name)
            if impact == "low":
                continue  # skip low-impact events

            events.append({
                "date": evt.get("date", ""),
                "time": evt.get("time", ""),
                "event": event_name,
                "impact": impact,
                "actual": evt.get("actual"),
                "estimate": evt.get("estimate"),
                "prev": evt.get("prev"),
                "unit": evt.get("unit", ""),
            })

        _cache_set(cache_key, events)
        log.info(f"Economic calendar: {len(events)} US events ({from_date} to {to_date})")
        return events

    except Exception as e:
        # Sanitize: never log the token
        err_str = str(e).replace(FINNHUB_TOKEN, "***")
        log.warning(f"Economic calendar fetch failed: {err_str}")
        _cache_set(cache_key, [])
        return []


def get_events_in_window(dte_days: int = 5) -> List[Dict]:
    """Get all medium/high impact US economic events within DTE window."""
    today = datetime.now(timezone.utc).date()
    from_date = today.strftime("%Y-%m-%d")
    to_date = (today + timedelta(days=dte_days)).strftime("%Y-%m-%d")
    return _fetch_economic_calendar(from_date, to_date)


def has_high_impact_today() -> bool:
    """Quick check: any high-impact event today?"""
    events = get_events_in_window(dte_days=0)
    return any(e.get("impact") == "high" for e in events)


def get_confidence_adjustment(dte: int = 0) -> Dict:
    """
    Compute confidence adjustment based on macro events in DTE window.

    Returns:
        {
            "adjustment": int,       # points to add/subtract
            "events": list,          # relevant events
            "has_high_impact": bool,
            "sizing_multiplier": float,  # 1.0 = normal, 0.5 = reduce
            "description": str,
        }
    """
    events = get_events_in_window(dte_days=max(dte, 1))

    if not events:
        return {
            "adjustment": 0, "events": [], "has_high_impact": False,
            "sizing_multiplier": 1.0, "description": "No significant macro events in window",
        }

    high_events = [e for e in events if e["impact"] == "high"]
    medium_events = [e for e in events if e["impact"] == "medium"]

    today = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    high_today = [e for e in high_events if e["date"] == today]

    adjustment = 0
    size_mult = 1.0

    if high_today:
        adjustment = -15
        size_mult = 0.50
        desc = f"⚠️ HIGH-IMPACT EVENT TODAY: {high_today[0]['event']}"
    elif high_events:
        adjustment = -8
        size_mult = 0.75
        desc = f"📅 High-impact event in DTE window: {high_events[0]['event']} ({high_events[0]['date']})"
    elif medium_events:
        adjustment = -3
        size_mult = 0.90
        desc = f"📅 Medium-impact event: {medium_events[0]['event']} ({medium_events[0]['date']})"
    else:
        desc = "No significant macro events"

    return {
        "adjustment": adjustment,
        "events": events,
        "has_high_impact": bool(high_events),
        "high_impact_today": bool(high_today),
        "sizing_multiplier": size_mult,
        "description": desc,
    }


def format_calendar_line(cal: Dict) -> str:
    """One-line summary for trade cards."""
    if not cal or not cal.get("events"):
        return ""
    if cal.get("high_impact_today"):
        return f"🚨 MACRO: {cal['description']}"
    if cal.get("has_high_impact"):
        return f"📅 MACRO: {cal['description']}"
    return ""
