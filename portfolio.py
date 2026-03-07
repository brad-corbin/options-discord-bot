# portfolio.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Phase 2A — Portfolio Data Layer
#   - Redis-backed holdings + options CRUD
#   - P/L calculation engine
#   - Wheel cycle tracking
#
# Uses the same store_get/store_set pattern from app.py.
# All data is JSON-serialized in Redis (or in-memory fallback).

import json
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# KEYS
# ─────────────────────────────────────────────────────────
KEY_HOLDINGS        = "portfolio:holdings"
KEY_OPTIONS         = "portfolio:options"
KEY_OPTIONS_COUNTER = "portfolio:options:counter"
KEY_SETTINGS        = "portfolio:settings"

def _wheel_key(ticker: str) -> str:
    return f"portfolio:wheel:{ticker.upper()}"


# ─────────────────────────────────────────────────────────
# STORE INTERFACE
# ─────────────────────────────────────────────────────────
# These will be wired to app.py's store_get / store_set at init time.
# This avoids circular imports — app.py calls portfolio.init_store().

_store_get = None
_store_set = None

def init_store(getter, setter):
    """Call once at startup: portfolio.init_store(store_get, store_set)"""
    global _store_get, _store_set
    _store_get = getter
    _store_set = setter
    log.info("Portfolio store initialized")

def _get(key: str):
    if _store_get is None:
        raise RuntimeError("Portfolio store not initialized — call portfolio.init_store()")
    return _store_get(key)

def _set(key: str, value: str, ttl: int = 0):
    if _store_set is None:
        raise RuntimeError("Portfolio store not initialized — call portfolio.init_store()")
    _set_raw(key, value, ttl)

def _set_raw(key: str, value: str, ttl: int = 0):
    _store_set(key, value, ttl)


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def _load_json(key: str, default=None):
    raw = _get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning(f"Bad JSON in {key}, resetting")
        return default

def _save_json(key: str, data, ttl: int = 0):
    _set(key, json.dumps(data), ttl=ttl)

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _next_option_id() -> str:
    """Auto-increment option ID: opt_001, opt_002, ..."""
    raw = _get(KEY_OPTIONS_COUNTER)
    n = int(raw) if raw else 0
    n += 1
    _set(KEY_OPTIONS_COUNTER, str(n))
    return f"opt_{n:03d}"


# ═══════════════════════════════════════════════════════════
# HOLDINGS CRUD
# ═══════════════════════════════════════════════════════════

def get_all_holdings() -> dict:
    """Return full holdings dict. Keys are uppercase tickers."""
    return _load_json(KEY_HOLDINGS, {})

def get_holding(ticker: str) -> Optional[dict]:
    h = get_all_holdings()
    return h.get(ticker.upper())

def add_holding(ticker: str, shares: int, cost_basis: float,
                tag: str = None, notes: str = "") -> dict:
    """
    Add shares to a holding. If ticker already exists, average the cost basis.
    Returns the updated holding dict.
    """
    ticker = ticker.upper()
    h = get_all_holdings()

    if ticker in h:
        existing = h[ticker]
        old_shares = existing["shares"]
        old_cost   = existing["cost_basis"]
        new_shares = old_shares + shares
        # Weighted average cost basis
        new_cost = ((old_cost * old_shares) + (cost_basis * shares)) / new_shares
        existing["shares"]     = new_shares
        existing["cost_basis"] = round(new_cost, 4)
        if tag and tag not in existing.get("tags", []):
            existing.setdefault("tags", []).append(tag)
        if notes:
            existing["notes"] = notes
        h[ticker] = existing
    else:
        h[ticker] = {
            "shares":     shares,
            "cost_basis": round(cost_basis, 4),
            "date_added": _today_str(),
            "tags":       [tag] if tag else [],
            "notes":      notes,
        }

    _save_json(KEY_HOLDINGS, h)
    log.info(f"Holdings: added {shares}sh {ticker} @{cost_basis}")
    return h[ticker]


def remove_holding(ticker: str, shares: int = None) -> dict:
    """
    Remove shares (or all if shares=None) from a holding.
    Returns {"removed": True/False, "remaining": int, "holding": dict or None}
    """
    ticker = ticker.upper()
    h = get_all_holdings()

    if ticker not in h:
        return {"removed": False, "remaining": 0, "holding": None,
                "error": f"{ticker} not in holdings"}

    existing = h[ticker]

    if shares is None or shares >= existing["shares"]:
        # Full removal
        removed_shares = existing["shares"]
        del h[ticker]
        _save_json(KEY_HOLDINGS, h)
        log.info(f"Holdings: removed ALL {removed_shares}sh {ticker}")
        return {"removed": True, "remaining": 0, "holding": None}
    else:
        existing["shares"] -= shares
        h[ticker] = existing
        _save_json(KEY_HOLDINGS, h)
        log.info(f"Holdings: removed {shares}sh {ticker}, {existing['shares']} remaining")
        return {"removed": True, "remaining": existing["shares"], "holding": existing}


# ═══════════════════════════════════════════════════════════
# OPTIONS CRUD
# ═══════════════════════════════════════════════════════════

def get_all_options() -> list:
    return _load_json(KEY_OPTIONS, [])

def get_open_options() -> list:
    return [o for o in get_all_options() if o.get("status") == "open"]

def get_options_for_ticker(ticker: str) -> list:
    ticker = ticker.upper()
    return [o for o in get_all_options() if o.get("ticker") == ticker]

def get_option_by_id(opt_id: str) -> Optional[dict]:
    for o in get_all_options():
        if o.get("id") == opt_id:
            return o
    return None


def add_option(ticker: str, opt_type: str, direction: str,
               strike: float, exp: str, premium: float,
               contracts: int = 1, notes: str = "") -> dict:
    """
    Record a new options position.
    opt_type: "covered_call", "csp", "debit_spread"
    direction: "buy" or "sell"
    Returns the new option dict.
    """
    opts = get_all_options()
    opt_id = _next_option_id()

    new_opt = {
        "id":            opt_id,
        "ticker":        ticker.upper(),
        "type":          opt_type,
        "direction":     direction.lower(),
        "strike":        round(strike, 2),
        "exp":           exp,
        "contracts":     contracts,
        "premium":       round(premium, 2),
        "open_date":     _today_str(),
        "close_date":    None,
        "close_premium": None,
        "status":        "open",
        "notes":         notes,
    }

    opts.append(new_opt)
    _save_json(KEY_OPTIONS, opts)
    log.info(f"Options: opened {opt_id} — {direction} {opt_type} {ticker} "
             f"${strike} exp {exp} @${premium} x{contracts}")
    return new_opt


def close_option(opt_id: str, close_premium: float) -> dict:
    """
    Close an option by buying/selling it back at close_premium.
    Returns the updated option dict or error dict.
    """
    opts = get_all_options()
    for o in opts:
        if o["id"] == opt_id:
            if o["status"] != "open":
                return {"error": f"{opt_id} is already {o['status']}"}
            o["status"]        = "closed"
            o["close_date"]    = _today_str()
            o["close_premium"] = round(close_premium, 2)
            _save_json(KEY_OPTIONS, opts)
            log.info(f"Options: closed {opt_id} @${close_premium}")
            return o
    return {"error": f"{opt_id} not found"}


def expire_option(opt_id: str) -> dict:
    """Mark option as expired worthless (full premium kept for sells)."""
    opts = get_all_options()
    for o in opts:
        if o["id"] == opt_id:
            if o["status"] != "open":
                return {"error": f"{opt_id} is already {o['status']}"}
            o["status"]        = "expired"
            o["close_date"]    = _today_str()
            o["close_premium"] = 0.0
            _save_json(KEY_OPTIONS, opts)
            log.info(f"Options: expired {opt_id}")
            return o
    return {"error": f"{opt_id} not found"}


def assign_option(opt_id: str) -> dict:
    """
    Mark option as assigned.
    - CSP assigned → auto-add shares to holdings at strike price
    - Covered call assigned → auto-remove shares from holdings
    Returns result dict with action taken.
    """
    opts = get_all_options()
    for o in opts:
        if o["id"] == opt_id:
            if o["status"] != "open":
                return {"error": f"{opt_id} is already {o['status']}"}

            o["status"]        = "assigned"
            o["close_date"]    = _today_str()
            o["close_premium"] = 0.0  # assigned, no buyback
            _save_json(KEY_OPTIONS, opts)

            ticker    = o["ticker"]
            contracts = o["contracts"]
            strike    = o["strike"]
            shares    = contracts * 100

            action = None

            if o["type"] == "csp":
                # Assigned on CSP → you buy 100 shares per contract at strike
                add_holding(ticker, shares, strike, tag="wheel",
                            notes=f"Assigned from {opt_id}")
                action = f"Added {shares}sh {ticker} @${strike}"
                log.info(f"Options: {opt_id} CSP assigned → {action}")

            elif o["type"] == "covered_call":
                # Called away → remove shares
                remove_holding(ticker, shares)
                action = f"Removed {shares}sh {ticker} (called away @${strike})"
                log.info(f"Options: {opt_id} CC assigned → {action}")

            return {"option": o, "action": action}

    return {"error": f"{opt_id} not found"}


# ═══════════════════════════════════════════════════════════
# P/L CALCULATIONS
# ═══════════════════════════════════════════════════════════

def calc_option_pnl(opt: dict) -> float:
    """
    Calculate realized P/L for a CLOSED/EXPIRED/ASSIGNED option.
    Returns dollar P/L (positive = profit).
    """
    if opt.get("status") == "open":
        return 0.0

    premium       = opt.get("premium", 0)
    close_premium = opt.get("close_premium", 0) or 0
    contracts     = opt.get("contracts", 1)
    direction     = opt.get("direction", "sell")

    if direction == "sell":
        # Sold for premium, bought back for close_premium
        pnl = (premium - close_premium) * contracts * 100
    else:
        # Bought for premium, sold for close_premium
        pnl = (close_premium - premium) * contracts * 100

    return round(pnl, 2)


def calc_ticker_options_income(ticker: str) -> float:
    """Sum of realized P/L from all closed options on a ticker."""
    ticker = ticker.upper()
    total = 0.0
    for o in get_all_options():
        if o.get("ticker") == ticker and o.get("status") in ("closed", "expired", "assigned"):
            total += calc_option_pnl(o)
    return round(total, 2)


def calc_holding_pnl(ticker: str, current_price: float) -> dict:
    """
    Full P/L for a single holding:
      - Unrealized share P/L
      - Options income (closed only)
      - Combined total + return %
    """
    ticker = ticker.upper()
    holding = get_holding(ticker)
    if not holding:
        return {"error": f"{ticker} not in holdings"}

    shares     = holding["shares"]
    cost_basis = holding["cost_basis"]

    unrealized = round((current_price - cost_basis) * shares, 2)
    opt_income = calc_ticker_options_income(ticker)
    total_pnl  = round(unrealized + opt_income, 2)
    invested   = cost_basis * shares
    return_pct = round((total_pnl / invested) * 100, 2) if invested > 0 else 0.0

    return {
        "ticker":       ticker,
        "shares":       shares,
        "cost_basis":   cost_basis,
        "current":      current_price,
        "unrealized":   unrealized,
        "opt_income":   opt_income,
        "total_pnl":    total_pnl,
        "return_pct":   return_pct,
    }


def calc_open_options_pnl(current_mids: dict = None) -> float:
    """
    Unrealized P/L on open options.
    current_mids: {"opt_001": 0.45, "opt_002": 1.20, ...}
    If not provided, returns 0 (need live prices to calculate).
    """
    if not current_mids:
        return 0.0

    total = 0.0
    for o in get_open_options():
        mid = current_mids.get(o["id"])
        if mid is None:
            continue

        premium   = o.get("premium", 0)
        contracts = o.get("contracts", 1)

        if o.get("direction") == "sell":
            pnl = (premium - mid) * contracts * 100
        else:
            pnl = (mid - premium) * contracts * 100

        total += pnl

    return round(total, 2)


def calc_portfolio_summary(price_map: dict) -> dict:
    """
    Full portfolio summary.
    price_map: {"AAPL": 192.30, "GOOGL": 299.47, ...}
    Returns aggregate P/L data.
    """
    holdings     = get_all_holdings()
    total_unrealized = 0.0
    total_opt_income = 0.0
    holding_details  = []

    for ticker, h in sorted(holdings.items()):
        price = price_map.get(ticker)
        if price is None:
            continue

        pnl = calc_holding_pnl(ticker, price)
        if "error" in pnl:
            continue

        total_unrealized += pnl["unrealized"]
        total_opt_income += pnl["opt_income"]
        holding_details.append(pnl)

    combined = round(total_unrealized + total_opt_income, 2)

    return {
        "holdings":         holding_details,
        "total_unrealized":  round(total_unrealized, 2),
        "total_opt_income":  round(total_opt_income, 2),
        "combined_pnl":      combined,
        "num_holdings":      len(holdings),
        "num_open_options":  len(get_open_options()),
    }


# ═══════════════════════════════════════════════════════════
# WHEEL TRACKING
# ═══════════════════════════════════════════════════════════

def get_wheel_history(ticker: str) -> list:
    """Get all options positions for a ticker (the full wheel cycle history)."""
    ticker = ticker.upper()
    positions = get_options_for_ticker(ticker)
    # Sort by open_date ascending
    positions.sort(key=lambda o: o.get("open_date", ""))
    return positions


def calc_wheel_pnl(ticker: str) -> dict:
    """
    Total wheel P/L for a ticker = sum of all premiums collected/lost
    across all CSPs and covered calls (closed + expired + assigned).
    """
    ticker = ticker.upper()
    history = get_wheel_history(ticker)
    total_premium = 0.0
    rounds = 0

    for o in history:
        if o.get("status") in ("closed", "expired", "assigned"):
            total_premium += calc_option_pnl(o)
            rounds += 1

    open_positions = [o for o in history if o.get("status") == "open"]

    return {
        "ticker":         ticker,
        "total_premium":  round(total_premium, 2),
        "closed_rounds":  rounds,
        "open_positions": len(open_positions),
        "history":        history,
    }
