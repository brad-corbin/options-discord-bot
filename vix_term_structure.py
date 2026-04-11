# vix_term_structure.py
# ═══════════════════════════════════════════════════════════════════
# VIX Term Structure — Contango / Backwardation / VVIX
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v7.0: Schwab quote primary, Yahoo Finance fallback. Cache: 120s TTL.
# ═══════════════════════════════════════════════════════════════════

import time
import logging
import threading
from typing import Dict, Optional, Callable

import requests

log = logging.getLogger(__name__)

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 120

_YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

BACKWARDATION_WARN   = 1.05
BACKWARDATION_SEVERE = 1.15
VIX9D_SPIKE_RATIO    = 1.10
VVIX_ELEVATED        = 110.0
VVIX_EXTREME         = 130.0

# v7.0: Schwab quote callback — set by app.py at startup
_schwab_quote_fn: Optional[Callable] = None

# Schwab uses $ prefix for indices
_SCHWAB_SYMBOLS = {
    "VIX": "$VIX",
    "VIX9D": "$VIX9D",
    "VIX3M": "$VIX3M",
    "VVIX": "$VVIX",
}


def set_quote_fn(fn: Callable):
    """Wire Schwab quote function from app.py. Called once at startup.
    fn(symbol) -> float or None
    """
    global _schwab_quote_fn
    _schwab_quote_fn = fn
    log.info("VIX term structure: Schwab quotes wired")


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.time() - ts > CACHE_TTL:
            del _cache[key]
            return None
        return value


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.time())


def _get_index_quote(symbol: str) -> Optional[float]:
    """v7.0: Get index quote — Schwab primary, Yahoo fallback.
    symbol: short name like 'VIX', 'VIX9D', 'VIX3M', 'VVIX'
    """
    # Try Schwab first
    if _schwab_quote_fn:
        schwab_sym = _SCHWAB_SYMBOLS.get(symbol, f"${symbol}")
        try:
            val = _schwab_quote_fn(schwab_sym)
            if val and val > 0:
                return val
        except Exception as e:
            log.debug(f"Schwab quote failed for {schwab_sym}: {e}")

    # Fallback to Yahoo
    return _yahoo_last(f"%5E{symbol}")


def _yahoo_last(symbol: str) -> Optional[float]:
    try:
        resp = requests.get(
            f"{_YAHOO_BASE}/{symbol}",
            params={"interval": "1d", "range": "1d"},
            headers=_YAHOO_HEADERS, timeout=5,
        )
        resp.raise_for_status()
        result = resp.json().get("chart", {}).get("result", [])
        if result:
            meta = result[0].get("meta", {})
            for field in ("regularMarketPrice", "previousClose"):
                v = meta.get(field)
                if v and float(v) > 0:
                    return float(v)
    except Exception as e:
        log.debug(f"Yahoo last failed for {symbol}: {e}")
    return None


def get_vix_term_structure() -> Dict:
    cached = _cache_get("vts")
    if cached is not None:
        return cached

    vix = _get_index_quote("VIX")
    vix9d = _get_index_quote("VIX9D")
    vix3m = _get_index_quote("VIX3M")
    vvix = _get_index_quote("VVIX")

    if not vix or vix <= 0:
        r = {"vix": 0, "vix9d": None, "vix3m": None, "vvix": None,
             "term_structure": "UNKNOWN", "vix_vix3m_ratio": 1.0,
             "vix9d_vix_ratio": None, "vvix_regime": "UNKNOWN",
             "confidence_adjustment": 0, "dte_bias": "NEUTRAL",
             "description": "VIX data unavailable"}
        _cache_set("vts", r)
        return r

    ratio = (vix / vix3m) if vix3m and vix3m > 0 else 1.0

    if ratio >= BACKWARDATION_SEVERE:
        ts, ca, db = "SEVERE_BACKWARDATION", -15, "SHORTER"
        desc = f"Severe backwardation (VIX/VIX3M={ratio:.2f}). Extreme near-term fear."
    elif ratio >= BACKWARDATION_WARN:
        ts, ca, db = "BACKWARDATION", -8, "SHORTER"
        desc = f"Backwardation (VIX/VIX3M={ratio:.2f}). Elevated hedging demand."
    elif ratio >= 0.95:
        ts, ca, db = "FLAT", -3, "NEUTRAL"
        desc = f"Flat term structure (VIX/VIX3M={ratio:.2f}). Transitional."
    else:
        ts, ca, db = "CONTANGO", 3, "NEUTRAL"
        desc = f"Contango (VIX/VIX3M={ratio:.2f}). Normal conditions."

    v9r = (vix9d / vix) if vix9d and vix > 0 else None
    if v9r and v9r > VIX9D_SPIKE_RATIO:
        ca -= 5
        desc += f" Short-term spike (VIX9D/VIX={v9r:.2f})."

    if vvix and vvix >= VVIX_EXTREME:
        vr = "EXTREME"; ca -= 5
        desc += f" VVIX extreme ({vvix:.0f})."
    elif vvix and vvix >= VVIX_ELEVATED:
        vr = "ELEVATED"; ca -= 2
        desc += f" VVIX elevated ({vvix:.0f})."
    else:
        vr = "NORMAL"

    result = {
        "vix": vix, "vix9d": vix9d, "vix3m": vix3m, "vvix": vvix,
        "term_structure": ts, "vix_vix3m_ratio": round(ratio, 3),
        "vix9d_vix_ratio": round(v9r, 3) if v9r else None,
        "vvix_regime": vr, "confidence_adjustment": ca,
        "dte_bias": db, "description": desc,
    }
    _cache_set("vts", result)
    return result


def format_term_structure_line(ts: Dict) -> str:
    if not ts or ts.get("term_structure") == "UNKNOWN":
        return ""
    emojis = {"CONTANGO": "🟢", "FLAT": "🟡", "BACKWARDATION": "🟠", "SEVERE_BACKWARDATION": "🔴"}
    emoji = emojis.get(ts["term_structure"], "⚪")
    parts = [f"{emoji} {ts['term_structure']}", f"VIX {ts['vix']:.1f}",
             f"VIX/3M={ts['vix_vix3m_ratio']:.2f}"]
    if ts.get("vvix"):
        parts.append(f"VVIX {ts['vvix']:.0f}")
    return " | ".join(parts)
