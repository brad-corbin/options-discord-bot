# portfolio.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Phase 2A — Portfolio Data Layer
#   - Redis-backed holdings + options CRUD
#   - P/L calculation engine
#   - Wheel cycle tracking
#
# v3.2 — Multi-account support:
#   - All functions accept account="brad" kwarg
#   - Storage keys prefixed: {account}:portfolio:holdings, etc.
#   - Day trade P/L logging
#   - Mutual fund / ETF lump balance tracking
#   - One-time migration helper for existing data
#
# Uses the same store_get/store_set pattern from app.py.
# All data is JSON-serialized in Redis (or in-memory fallback).

import json
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# KEYS (v3.2: all prefixed by account)
# ─────────────────────────────────────────────────────────

def _key_holdings(account: str = "brad") -> str:
    return f"{account}:portfolio:holdings"

def _key_options(account: str = "brad") -> str:
    return f"{account}:portfolio:options"

def _key_options_counter(account: str = "brad") -> str:
    return f"{account}:portfolio:options:counter"

def _key_settings(account: str = "brad") -> str:
    return f"{account}:portfolio:settings"

def _key_cash(account: str = "brad") -> str:
    return f"{account}:portfolio:cash"

def _key_mutualfunds(account: str = "brad") -> str:
    return f"{account}:portfolio:mutualfunds"

# Legacy keys (pre-v3.2, no account prefix)
_LEGACY_KEY_HOLDINGS        = "portfolio:holdings"
_LEGACY_KEY_OPTIONS         = "portfolio:options"
_LEGACY_KEY_OPTIONS_COUNTER = "portfolio:options:counter"
_LEGACY_KEY_SETTINGS        = "portfolio:settings"


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

def _next_option_id(account: str = "brad") -> str:
    """Auto-increment option ID: opt_001, opt_002, ..."""
    key = _key_options_counter(account)
    raw = _get(key)
    n = int(raw) if raw else 0
    n += 1
    _set(key, str(n))
    return f"opt_{n:03d}"


# ═══════════════════════════════════════════════════════════
# HOLDINGS CRUD
# ═══════════════════════════════════════════════════════════

def get_all_holdings(account: str = "brad") -> dict:
    """Return full holdings dict. Keys are uppercase tickers."""
    return _load_json(_key_holdings(account), {})

def get_holding(ticker: str, account: str = "brad") -> Optional[dict]:
    h = get_all_holdings(account=account)
    return h.get(ticker.upper())

def add_holding(ticker: str, shares: int, cost_basis: float,
                tag: str = None, notes: str = "",
                account: str = "brad") -> dict:
    """
    Add shares to a holding. If ticker already exists, average the cost basis.
    Returns the updated holding dict.
    """
    ticker = ticker.upper()
    h = get_all_holdings(account=account)

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

    _save_json(_key_holdings(account), h)
    log.info(f"Holdings [{account}]: added {shares}sh {ticker} @{cost_basis}")
    return h[ticker]


def remove_holding(ticker: str, shares: int = None,
                   account: str = "brad") -> dict:
    """
    Remove shares (or all if shares=None) from a holding.
    Returns {"removed": True/False, "remaining": int, "holding": dict or None}
    """
    ticker = ticker.upper()
    h = get_all_holdings(account=account)

    if ticker not in h:
        return {"removed": False, "remaining": 0, "holding": None,
                "error": f"{ticker} not in holdings"}

    existing = h[ticker]

    if shares is None or shares >= existing["shares"]:
        # Full removal
        removed_shares = existing["shares"]
        del h[ticker]
        _save_json(_key_holdings(account), h)
        log.info(f"Holdings [{account}]: removed ALL {removed_shares}sh {ticker}")
        return {"removed": True, "remaining": 0, "holding": None}
    else:
        existing["shares"] -= shares
        h[ticker] = existing
        _save_json(_key_holdings(account), h)
        log.info(f"Holdings [{account}]: removed {shares}sh {ticker}, {existing['shares']} remaining")
        return {"removed": True, "remaining": existing["shares"], "holding": existing}


# ═══════════════════════════════════════════════════════════
# OPTIONS CRUD
# ═══════════════════════════════════════════════════════════

def get_all_options(account: str = "brad") -> list:
    return _load_json(_key_options(account), [])

def get_open_options(account: str = "brad") -> list:
    return [o for o in get_all_options(account=account) if o.get("status") == "open"]

def get_options_for_ticker(ticker: str, account: str = "brad") -> list:
    ticker = ticker.upper()
    return [o for o in get_all_options(account=account) if o.get("ticker") == ticker]

def get_option_by_id(opt_id: str, account: str = "brad") -> Optional[dict]:
    for o in get_all_options(account=account):
        if o.get("id") == opt_id:
            return o
    return None


def add_option(ticker: str, opt_type: str, direction: str,
               strike: float, exp: str, premium: float,
               contracts: int = 1, notes: str = "",
               account: str = "brad") -> dict:
    """
    Record a new options position.
    opt_type: "covered_call", "csp", "debit_spread"
    direction: "buy" or "sell"
    Returns the new option dict.
    """
    opts = get_all_options(account=account)
    opt_id = _next_option_id(account=account)

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
    _save_json(_key_options(account), opts)
    log.info(f"Options [{account}]: opened {opt_id} — {direction} {opt_type} {ticker} "
             f"${strike} exp {exp} @${premium} x{contracts}")
    return new_opt


def close_option(opt_id: str, close_premium: float,
                 account: str = "brad") -> dict:
    """
    Close an option by buying/selling it back at close_premium.
    Returns the updated option dict or error dict.
    """
    opts = get_all_options(account=account)
    for o in opts:
        if o["id"] == opt_id:
            if o["status"] != "open":
                return {"error": f"{opt_id} is already {o['status']}"}
            o["status"]        = "closed"
            o["close_date"]    = _today_str()
            o["close_premium"] = round(close_premium, 2)
            _save_json(_key_options(account), opts)
            log.info(f"Options [{account}]: closed {opt_id} @${close_premium}")
            return o
    return {"error": f"{opt_id} not found"}


def expire_option(opt_id: str, account: str = "brad") -> dict:
    """Mark option as expired worthless (full premium kept for sells)."""
    opts = get_all_options(account=account)
    for o in opts:
        if o["id"] == opt_id:
            if o["status"] != "open":
                return {"error": f"{opt_id} is already {o['status']}"}
            o["status"]        = "expired"
            o["close_date"]    = _today_str()
            o["close_premium"] = 0.0
            _save_json(_key_options(account), opts)
            log.info(f"Options [{account}]: expired {opt_id}")
            return o
    return {"error": f"{opt_id} not found"}


def assign_option(opt_id: str, account: str = "brad") -> dict:
    """
    Mark option as assigned.
    - CSP assigned → auto-add shares to holdings at strike price
    - Covered call assigned → auto-remove shares from holdings
    Returns result dict with action taken.
    """
    opts = get_all_options(account=account)
    for o in opts:
        if o["id"] == opt_id:
            if o["status"] != "open":
                return {"error": f"{opt_id} is already {o['status']}"}

            o["status"]        = "assigned"
            o["close_date"]    = _today_str()
            o["close_premium"] = 0.0  # assigned, no buyback
            _save_json(_key_options(account), opts)

            ticker    = o["ticker"]
            contracts = o["contracts"]
            strike    = o["strike"]
            shares    = contracts * 100

            action = None

            if o["type"] == "csp":
                # Assigned on CSP → you buy 100 shares per contract at strike
                add_holding(ticker, shares, strike, tag="wheel",
                            notes=f"Assigned from {opt_id}",
                            account=account)
                action = f"Added {shares}sh {ticker} @${strike}"
                log.info(f"Options [{account}]: {opt_id} CSP assigned → {action}")

            elif o["type"] == "covered_call":
                # Called away → remove shares
                remove_holding(ticker, shares, account=account)
                action = f"Removed {shares}sh {ticker} (called away @${strike})"
                log.info(f"Options [{account}]: {opt_id} CC assigned → {action}")

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


def calc_ticker_options_income(ticker: str, account: str = "brad") -> float:
    """Sum of realized P/L from all closed options on a ticker."""
    ticker = ticker.upper()
    total = 0.0
    for o in get_all_options(account=account):
        if o.get("ticker") == ticker and o.get("status") in ("closed", "expired", "assigned"):
            total += calc_option_pnl(o)
    return round(total, 2)


def calc_holding_pnl(ticker: str, current_price: float,
                     account: str = "brad") -> dict:
    """
    Full P/L for a single holding:
      - Unrealized share P/L
      - Options income (closed only)
      - Combined total + return %
    """
    ticker = ticker.upper()
    holding = get_holding(ticker, account=account)
    if not holding:
        return {"error": f"{ticker} not in holdings"}

    shares     = holding["shares"]
    cost_basis = holding["cost_basis"]

    unrealized = round((current_price - cost_basis) * shares, 2)
    opt_income = calc_ticker_options_income(ticker, account=account)
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


def calc_open_options_pnl(current_mids: dict = None,
                          account: str = "brad") -> float:
    """
    Unrealized P/L on open options.
    current_mids: {"opt_001": 0.45, "opt_002": 1.20, ...}
    If not provided, returns 0 (need live prices to calculate).
    """
    if not current_mids:
        return 0.0

    total = 0.0
    for o in get_open_options(account=account):
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


def calc_portfolio_summary(price_map: dict, account: str = "brad") -> dict:
    """
    Full portfolio summary.
    price_map: {"AAPL": 192.30, "GOOGL": 299.47, ...}
    Returns aggregate P/L data.
    """
    holdings     = get_all_holdings(account=account)
    total_unrealized = 0.0
    total_opt_income = 0.0
    holding_details  = []

    for ticker, h in sorted(holdings.items()):
        price = price_map.get(ticker)
        if price is None:
            continue

        pnl = calc_holding_pnl(ticker, price, account=account)
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
        "num_open_options":  len(get_open_options(account=account)),
    }


# ═══════════════════════════════════════════════════════════
# WHEEL TRACKING
# ═══════════════════════════════════════════════════════════

def get_wheel_history(ticker: str, account: str = "brad") -> list:
    """Get all options positions for a ticker (the full wheel cycle history)."""
    ticker = ticker.upper()
    positions = get_options_for_ticker(ticker, account=account)
    # Sort by open_date ascending
    positions.sort(key=lambda o: o.get("open_date", ""))
    return positions


def calc_wheel_pnl(ticker: str, account: str = "brad") -> dict:
    """
    Total wheel P/L for a ticker = sum of all premiums collected/lost
    across all CSPs and covered calls (closed + expired + assigned).
    """
    ticker = ticker.upper()
    history = get_wheel_history(ticker, account=account)
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


# ═══════════════════════════════════════════════════════════
# CASH BALANCE TRACKING (v3.3)
# ═══════════════════════════════════════════════════════════
#
# Instead of logging individual day trades, we track:
#   - total_deposited: total cash you put INTO the brokerage account
#   - cash_balance:    current cash sitting in the account (update periodically)
#
# The bot computes everything else automatically:
#   - Holdings value   = sum(shares × current price)       ← live from API
#   - Account value    = cash_balance + holdings_value
#   - Total P/L        = account_value - total_deposited
#   - Unrealized P/L   = holdings_value - holdings_cost_basis
#   - Realized P/L     = total P/L - unrealized P/L
#     (this captures day trades, closed options, everything)
#
# Commands:
#   /cash deposit 50000     → set total cash deposited
#   /cash deposit +5000     → add to deposits (transferred more money in)
#   /cash 12345             → update current cash balance
#   /cash                   → show account P/L breakdown
#   /cash history           → show balance snapshots over time

def get_cash_data(account: str = "brad") -> dict:
    """
    Get cash balance data for an account.
    Returns dict with total_deposited, cash_balance, last_updated, history.
    """
    key = _key_cash(account)
    return _load_json(key, {
        "total_deposited": 0.0,
        "cash_balance":    0.0,
        "last_updated":    None,
        "history":         [],
    })


def set_total_deposited(amount: float, add: bool = False,
                        account: str = "brad") -> dict:
    """
    Set (or add to) the total amount deposited into the brokerage account.
    This is money you transferred IN — your baseline.

    Args:
        amount: Dollar amount
        add:    If True, adds to existing deposits (e.g. wired in more cash).
                If False, sets the total (e.g. initial setup).
    """
    key = _key_cash(account)
    data = get_cash_data(account=account)

    if add:
        data["total_deposited"] = round(data.get("total_deposited", 0) + amount, 2)
    else:
        data["total_deposited"] = round(amount, 2)

    _save_json(key, data)
    log.info(f"Cash [{account}]: deposits {'added' if add else 'set'} "
             f"→ ${data['total_deposited']:,.2f}")
    return data


def update_cash_balance(balance: float, account: str = "brad") -> dict:
    """
    Update the current cash balance (what your broker shows as cash).
    Records a history snapshot for tracking over time.
    """
    key = _key_cash(account)
    data = get_cash_data(account=account)

    today = _today_str()
    data["cash_balance"] = round(balance, 2)
    data["last_updated"] = today

    # Add snapshot to history
    data.setdefault("history", [])
    data["history"].append({
        "date":    today,
        "cash":    round(balance, 2),
    })

    _save_json(key, data)
    log.info(f"Cash [{account}]: balance updated to ${balance:,.2f}")
    return data


def calc_account_pnl(price_map: dict, account: str = "brad") -> dict:
    """
    Full account P/L breakdown — includes stocks AND mutual funds.

    price_map: {"AAPL": 192.30, ...} — current prices for holdings

    Returns:
        total_deposited:  money you put in (across everything)
        cash_balance:     cash sitting in account
        holdings_cost:    total cost basis of shares
        holdings_value:   total current market value of shares
        fund_cost:        mutual fund cost basis
        fund_value:       mutual fund current value
        account_value:    cash + holdings_value + fund_value
        unrealized_pnl:   (holdings_value - holdings_cost) + (fund_value - fund_cost)
        total_pnl:        account_value - total_deposited
        realized_pnl:     total_pnl - unrealized_pnl
        return_pct:       total P/L as % of deposits
    """
    cash_data = get_cash_data(account=account)
    holdings  = get_all_holdings(account=account)
    fund_data = get_mutual_fund(account=account)

    total_deposited = cash_data.get("total_deposited", 0)
    cash_balance    = cash_data.get("cash_balance", 0)

    # Calculate holdings cost and current value
    holdings_cost  = 0.0
    holdings_value = 0.0

    for ticker, h in holdings.items():
        shares = h.get("shares", 0)
        cost   = h.get("cost_basis", 0)
        holdings_cost += shares * cost

        price = price_map.get(ticker)
        if price is not None:
            holdings_value += shares * price

    # Include mutual funds
    fund_cost  = fund_data.get("cost_basis", 0)
    fund_value = fund_data.get("current_value", 0)

    account_value  = cash_balance + holdings_value + fund_value
    unrealized_pnl = round((holdings_value - holdings_cost) + (fund_value - fund_cost), 2)
    total_pnl      = round(account_value - total_deposited, 2)
    realized_pnl   = round(total_pnl - unrealized_pnl, 2)
    return_pct     = round((total_pnl / total_deposited) * 100, 2) if total_deposited > 0 else 0.0

    return {
        "total_deposited": total_deposited,
        "cash_balance":    cash_balance,
        "holdings_cost":   round(holdings_cost, 2),
        "holdings_value":  round(holdings_value, 2),
        "fund_cost":       round(fund_cost, 2),
        "fund_value":      round(fund_value, 2),
        "account_value":   round(account_value, 2),
        "unrealized_pnl":  unrealized_pnl,
        "realized_pnl":    realized_pnl,
        "total_pnl":       total_pnl,
        "return_pct":      return_pct,
        "last_updated":    cash_data.get("last_updated"),
        "num_snapshots":   len(cash_data.get("history", [])),
    }


# ═══════════════════════════════════════════════════════════
# MUTUAL FUND / ETF LUMP BALANCE TRACKER (v3.2)
# ═══════════════════════════════════════════════════════════
#
# Stores a single lump record per account:
#   - cost_basis:    total amount originally invested
#   - current_value: last-updated market value
#   - last_updated:  date of last value update
#   - history:       list of {date, value} snapshots for P/L over time
#
# Commands:
#   /fund set 50000           → set original cost basis
#   /fund update 54200        → update current market value (adds snapshot)
#   /fund                     → show current P/L
#   /fund history             → show value over time

def get_mutual_fund(account: str = "brad") -> dict:
    """
    Get the mutual fund / ETF lump balance for an account.
    Returns dict with cost_basis, current_value, last_updated, history.
    """
    key = _key_mutualfunds(account)
    return _load_json(key, {
        "cost_basis":    0.0,
        "current_value": 0.0,
        "last_updated":  None,
        "history":       [],
    })


def set_mutual_fund_basis(cost_basis: float, account: str = "brad") -> dict:
    """
    Set (or reset) the original cost basis for mutual funds.
    This is the total amount invested across all mutual funds / ETFs.
    """
    key = _key_mutualfunds(account)
    fund = get_mutual_fund(account=account)
    fund["cost_basis"] = round(cost_basis, 2)
    if not fund.get("last_updated"):
        fund["last_updated"] = _today_str()
    _save_json(key, fund)
    log.info(f"MutualFund [{account}]: set cost basis to ${cost_basis:,.2f}")
    return fund


def update_mutual_fund_value(current_value: float,
                             account: str = "brad") -> dict:
    """
    Update the current market value and record a history snapshot.
    Call this periodically (weekly, monthly) to track P/L over time.
    """
    key = _key_mutualfunds(account)
    fund = get_mutual_fund(account=account)

    today = _today_str()
    fund["current_value"] = round(current_value, 2)
    fund["last_updated"]  = today

    # Add snapshot to history
    fund.setdefault("history", [])
    fund["history"].append({
        "date":  today,
        "value": round(current_value, 2),
    })

    _save_json(key, fund)
    log.info(f"MutualFund [{account}]: updated value to ${current_value:,.2f}")
    return fund


def calc_mutual_fund_pnl(account: str = "brad") -> dict:
    """
    Calculate P/L for the mutual fund lump balance.
    Returns dict with cost_basis, current_value, pnl, return_pct.
    """
    fund = get_mutual_fund(account=account)
    cost    = fund.get("cost_basis", 0)
    current = fund.get("current_value", 0)
    pnl     = round(current - cost, 2)
    pct     = round((pnl / cost) * 100, 2) if cost > 0 else 0.0

    return {
        "cost_basis":    cost,
        "current_value": current,
        "pnl":           pnl,
        "return_pct":    pct,
        "last_updated":  fund.get("last_updated"),
        "num_snapshots": len(fund.get("history", [])),
    }


# ═══════════════════════════════════════════════════════════
# DATA MIGRATION (one-time, v3.2)
# ═══════════════════════════════════════════════════════════

def migrate_to_multi_account() -> int:
    """
    ONE-TIME MIGRATION: Copy all existing (unprefixed) portfolio data
    to brad:-prefixed keys so the multi-account system picks them up.

    Run this once from a Python shell or temporary /migrate command:
        import portfolio
        portfolio.init_store(store_get, store_set)
        portfolio.migrate_to_multi_account()

    Safe to run multiple times — it only copies, never deletes originals.
    """
    migrated = 0

    # Holdings
    raw = _get(_LEGACY_KEY_HOLDINGS)
    if raw:
        _set(_key_holdings("brad"), raw)
        log.info(f"Migrated {_LEGACY_KEY_HOLDINGS} → {_key_holdings('brad')}")
        migrated += 1

    # Options list
    raw = _get(_LEGACY_KEY_OPTIONS)
    if raw:
        _set(_key_options("brad"), raw)
        log.info(f"Migrated {_LEGACY_KEY_OPTIONS} → {_key_options('brad')}")
        migrated += 1

    # Options counter
    raw = _get(_LEGACY_KEY_OPTIONS_COUNTER)
    if raw:
        _set(_key_options_counter("brad"), raw)
        log.info(f"Migrated {_LEGACY_KEY_OPTIONS_COUNTER} → {_key_options_counter('brad')}")
        migrated += 1

    # Settings
    raw = _get(_LEGACY_KEY_SETTINGS)
    if raw:
        _set(_key_settings("brad"), raw)
        log.info(f"Migrated {_LEGACY_KEY_SETTINGS} → {_key_settings('brad')}")
        migrated += 1

    log.info(f"Migration complete: {migrated} keys copied to brad: prefix")
    return migrated
