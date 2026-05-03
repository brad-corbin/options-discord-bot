"""Phase 4 — Portfolio writes.

All write operations go through this module so they:
  1. Validate input
  2. Log to the audit tab via durability.audit_write()
  3. Update Redis via portfolio._store_set
  4. Return a result dict

This module is where the dashboard touches the bot's portfolio state.
Read-only data layer (data.py) reads SNAPSHOTS — entry pages read live
Redis through the helpers here.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────

# UI account → underlying portfolio account keys
# Phase 4 expands this from Phase 3's 2-account model.
UI_TO_PORTFOLIO = {
    "mine":     ["brad"],
    "mom":      ["mom"],
    "partner":  ["partner"],   # NEW — Day Trades
    "kyleigh":  [],            # notional only — no positions, only transfers ledger
    "combined": ["brad", "mom", "partner"],
}

# All underlying account keys we know about
ALL_UNDERLYING_ACCOUNTS = ["brad", "mom", "partner"]

# Display labels for the right-pill on Portfolio entry pages.
# Underlying Redis keys stay "brad" / "mom" / "partner" — only the
# UI label changes.
UNDERLYING_LABELS = {
    "brad":    "Corbin",
    "mom":     "Volkman",
    "partner": "Partner",
}

# Map underlying account → which top-chip color theme that account "belongs" to.
# Used by Portfolio template to switch theme when right-pill is clicked.
UNDERLYING_TO_THEME = {
    "brad":    "mine",
    "mom":     "mom",
    "partner": "partner",
}

# Sub-account tag list (UI dropdown seed). User can add more.
DEFAULT_SUBACCOUNTS = [
    "Brokerage",
    "BC Rollover",
    "BC Roth",
    "CC Roth",
    "CC Rollover",
    "Volkman Wheel",
    "Partnership",
]
DEFAULT_SUBACCOUNT = "Brokerage"

# Trade categories for auto-tagging
TRADE_CATEGORIES = [
    "Wheel CSP", "Wheel CC", "Wheel Roll",
    "Day Trade", "Long Call", "Long Put",
    "Spread", "Earnings", "Iron Condor", "Inverse Condor",
]

# Transfer ledger names (Phase 4: Kyleigh + Clay)
TRANSFER_RECIPIENTS = ["kyleigh", "clay"]


# ─────────────────────────────────────────────────────────
# Late-bound portfolio module access
# ─────────────────────────────────────────────────────────

def _portfolio():
    try:
        import portfolio
        return portfolio
    except Exception as e:
        log.warning(f"portfolio unavailable: {e}")
        return None


def _store_get(key: str) -> Optional[str]:
    pf = _portfolio()
    if not pf or not pf._store_get:
        return None
    try:
        return pf._store_get(key)
    except Exception as e:
        log.warning(f"store_get failed for {key}: {e}")
        return None


def _store_set(key: str, value: str) -> bool:
    pf = _portfolio()
    if not pf or not pf._store_set:
        return False
    try:
        pf._store_set(key, value)
        return True
    except Exception as e:
        log.warning(f"store_set failed for {key}: {e}")
        return False


# ─────────────────────────────────────────────────────────
# Redis key helpers
# Some keys live in portfolio.py's namespace, others are new
# Phase 4 extensions.
# ─────────────────────────────────────────────────────────

def _key_holdings(account: str) -> str:
    return f"{account}:portfolio:holdings"

def _key_options(account: str) -> str:
    return f"{account}:portfolio:options"

def _key_spreads(account: str) -> str:
    return f"{account}:portfolio:spreads"

def _key_cash(account: str) -> str:
    return f"{account}:portfolio:cash"

def _key_cash_ledger(account: str) -> str:
    """Phase 4 — append-only ledger of cash events."""
    return f"{account}:portfolio:cash_ledger"

def _key_lumpsum(account: str) -> str:
    """Phase 4 — ETF lump-sum tracking (separate from share holdings)."""
    return f"{account}:portfolio:lumpsum"

def _key_transfers(recipient: str) -> str:
    """Phase 4 — Kyleigh + Clay notional balance ledgers."""
    return f"transfers:{recipient}:ledger"

def _key_subaccount_list() -> str:
    """Phase 4 — user-extensible list of sub-account tags."""
    return "omega:subaccounts"


# ─────────────────────────────────────────────────────────
# Helpers — load/save JSON values
# ─────────────────────────────────────────────────────────

def _load(key: str, default: Any) -> Any:
    raw = _store_get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _save(key: str, value: Any) -> bool:
    return _store_set(key, json.dumps(value, default=str))


# ─────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────

def _validate_ticker(t: str) -> Optional[str]:
    if not t:
        return None
    t = t.strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,10}$", t):
        return None
    return t


def _validate_date(d: str) -> Optional[str]:
    """Accepts YYYY-MM-DD; returns canonical form or None."""
    if not d:
        return None
    d = d.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return None
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return d
    except Exception:
        return None


def _to_float(v, default=None) -> Optional[float]:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _to_int(v, default=None) -> Optional[int]:
    try:
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
        return default


def _validate_account(account: str) -> bool:
    return account in ALL_UNDERLYING_ACCOUNTS


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────
# Audit helper — late-bound to durability
# ─────────────────────────────────────────────────────────

def _audit(account: str, op: str, target: str, old_value=None, new_value=None):
    try:
        from . import durability
        durability.audit_write(account, op, target, old_value, new_value)
    except Exception as e:
        log.warning(f"audit_write failed (non-fatal): {e}")


# ═════════════════════════════════════════════════════════
# SUB-ACCOUNT TAG MANAGEMENT
# ═════════════════════════════════════════════════════════

def get_subaccounts() -> List[str]:
    """Return current sub-account tag list. Seeds from defaults if empty."""
    saved = _load(_key_subaccount_list(), None)
    if saved is None:
        # First-time seed
        _save(_key_subaccount_list(), DEFAULT_SUBACCOUNTS)
        return list(DEFAULT_SUBACCOUNTS)
    return list(saved) if isinstance(saved, list) else list(DEFAULT_SUBACCOUNTS)


def add_subaccount(name: str) -> Dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Name required"}
    if len(name) > 40:
        return {"ok": False, "error": "Name too long"}
    current = get_subaccounts()
    if name in current:
        return {"ok": False, "error": f"'{name}' already exists"}
    current.append(name)
    if _save(_key_subaccount_list(), current):
        _audit("system", "add_subaccount", name, None, {"name": name})
        return {"ok": True, "subaccounts": current}
    return {"ok": False, "error": "Save failed"}


def remove_subaccount(name: str) -> Dict:
    current = get_subaccounts()
    if name not in current:
        return {"ok": False, "error": "Not found"}
    current = [s for s in current if s != name]
    if _save(_key_subaccount_list(), current):
        _audit("system", "remove_subaccount", name, {"name": name}, None)
        return {"ok": True, "subaccounts": current}
    return {"ok": False, "error": "Save failed"}


# ═════════════════════════════════════════════════════════
# CASH LEDGER
# Append-only ledger of cash events: deposit, withdrawal, manual_set,
# option_open, option_close, share_buy, share_sell, transfer_out,
# transfer_in, roll_credit, roll_debit
# ═════════════════════════════════════════════════════════

def get_cash_ledger(account: str) -> List[Dict]:
    if not _validate_account(account):
        return []
    return _load(_key_cash_ledger(account), [])


def calc_cash_balance(account: str) -> float:
    """Sum of all cash ledger events for an account."""
    ledger = get_cash_ledger(account)
    total = 0.0
    for entry in ledger:
        if isinstance(entry, dict):
            total += float(entry.get("amount") or 0)
    return round(total, 2)


def calc_cash_breakdown(account: str) -> Dict[str, float]:
    """Sum cash ledger events grouped by sub-account tag."""
    ledger = get_cash_ledger(account)
    by_subaccount: Dict[str, float] = {}
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        sub = entry.get("subaccount") or DEFAULT_SUBACCOUNT
        amt = float(entry.get("amount") or 0)
        by_subaccount[sub] = by_subaccount.get(sub, 0.0) + amt
    return {k: round(v, 2) for k, v in by_subaccount.items()}


def add_cash_event(account: str, event_type: str, amount: float,
                   subaccount: str = None, date: str = None,
                   note: str = None, ref_id: str = None) -> Dict:
    """Append a cash event to the ledger."""
    if not _validate_account(account):
        return {"ok": False, "error": f"Invalid account '{account}'"}

    amt = _to_float(amount)
    if amt is None:
        return {"ok": False, "error": "Amount required"}

    date_iso = _validate_date(date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sub = (subaccount or "").strip() or DEFAULT_SUBACCOUNT

    entry = {
        "id": _gen_id("cash"),
        "type": event_type,
        "amount": round(amt, 2),
        "subaccount": sub,
        "date": date_iso,
        "note": (note or "").strip(),
        "ref_id": ref_id,
        "created_at": _now_iso(),
    }

    ledger = get_cash_ledger(account)
    ledger.append(entry)
    if not _save(_key_cash_ledger(account), ledger):
        return {"ok": False, "error": "Save failed"}

    # Mirror the running balance into the legacy cash key so existing bot
    # commands that read get_cash_data() see the right number.
    new_balance = calc_cash_balance(account)
    _save(_key_cash(account), {"cash_balance": new_balance, "last_updated": _now_iso()})

    _audit(account, f"cash_{event_type}", entry["id"], None, entry)

    return {"ok": True, "entry": entry, "new_balance": new_balance}


def delete_cash_event(account: str, entry_id: str) -> Dict:
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    ledger = get_cash_ledger(account)
    target = next((e for e in ledger if isinstance(e, dict) and e.get("id") == entry_id), None)
    if not target:
        return {"ok": False, "error": "Entry not found"}
    ledger = [e for e in ledger if not (isinstance(e, dict) and e.get("id") == entry_id)]
    if not _save(_key_cash_ledger(account), ledger):
        return {"ok": False, "error": "Save failed"}

    new_balance = calc_cash_balance(account)
    _save(_key_cash(account), {"cash_balance": new_balance, "last_updated": _now_iso()})
    _audit(account, "cash_delete", entry_id, target, None)
    return {"ok": True, "deleted": target, "new_balance": new_balance}


# ═════════════════════════════════════════════════════════
# HOLDINGS (share positions)
# ═════════════════════════════════════════════════════════

def get_holdings(account: str) -> Dict[str, Dict]:
    if not _validate_account(account):
        return {}
    return _load(_key_holdings(account), {})


def add_holding(account: str, ticker: str, shares: float, cost_basis: float,
                subaccount: str = None, tag: str = None,
                date: str = None) -> Dict:
    """Add or merge a share lot.

    If ticker already exists, this CREATES a new lot (we don't mutate the
    existing position — phase 4.5 may add cost-basis-merge logic, but for
    now multiple buys at different prices stay distinct).
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    t = _validate_ticker(ticker)
    if not t:
        return {"ok": False, "error": "Invalid ticker"}
    sh = _to_float(shares)
    cb = _to_float(cost_basis)
    if sh is None or sh <= 0:
        return {"ok": False, "error": "Shares must be > 0"}
    if cb is None or cb < 0:
        return {"ok": False, "error": "Cost basis required"}

    sub = (subaccount or "").strip() or DEFAULT_SUBACCOUNT
    date_iso = _validate_date(date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    holdings = get_holdings(account)
    existing = holdings.get(t)

    if existing:
        # Add to existing position — average cost basis
        old_shares = float(existing.get("shares") or 0)
        old_cb = float(existing.get("cost_basis") or 0)
        new_shares = old_shares + sh
        new_cb = ((old_shares * old_cb) + (sh * cb)) / new_shares if new_shares else cb
        holdings[t] = {
            "shares": new_shares,
            "cost_basis": round(new_cb, 4),
            "subaccount": existing.get("subaccount", sub),
            "tag": existing.get("tag") or (tag or ""),
            "first_added": existing.get("first_added", date_iso),
            "last_updated": _now_iso(),
        }
        op = "add_to_holding"
    else:
        holdings[t] = {
            "shares": sh,
            "cost_basis": round(cb, 4),
            "subaccount": sub,
            "tag": (tag or "").strip(),
            "first_added": date_iso,
            "last_updated": _now_iso(),
        }
        op = "add_holding"

    if not _save(_key_holdings(account), holdings):
        return {"ok": False, "error": "Save failed"}

    # Cash event: share purchase debits cash
    cash_amount = -(sh * cb)
    add_cash_event(
        account, "share_buy", cash_amount,
        subaccount=sub, date=date_iso,
        note=f"Buy {sh} {t} @ ${cb}",
        ref_id=t,
    )

    _audit(account, op, t, existing, holdings[t])
    return {"ok": True, "ticker": t, "holding": holdings[t]}


def edit_holding(account: str, ticker: str,
                 shares: float = None, cost_basis: float = None,
                 subaccount: str = None, tag: str = None) -> Dict:
    """Edit an existing holding (no cash event — assumes correction not new buy)."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    t = _validate_ticker(ticker)
    if not t:
        return {"ok": False, "error": "Invalid ticker"}
    holdings = get_holdings(account)
    if t not in holdings:
        return {"ok": False, "error": f"{t} not in holdings"}

    old = dict(holdings[t])
    new = dict(old)
    if shares is not None:
        s = _to_float(shares)
        if s is None or s < 0:
            return {"ok": False, "error": "Invalid shares"}
        new["shares"] = s
    if cost_basis is not None:
        cb = _to_float(cost_basis)
        if cb is None or cb < 0:
            return {"ok": False, "error": "Invalid cost basis"}
        new["cost_basis"] = round(cb, 4)
    if subaccount is not None:
        new["subaccount"] = (subaccount or "").strip() or DEFAULT_SUBACCOUNT
    if tag is not None:
        new["tag"] = (tag or "").strip()
    new["last_updated"] = _now_iso()

    holdings[t] = new
    if not _save(_key_holdings(account), holdings):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "edit_holding", t, old, new)
    return {"ok": True, "ticker": t, "holding": new}


def sell_holding(account: str, ticker: str, shares: float,
                 sell_price: float, date: str = None,
                 note: str = None) -> Dict:
    """Reduce or remove a holding; logs a sell cash event.

    If shares == current shares, removes entirely.
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    t = _validate_ticker(ticker)
    if not t:
        return {"ok": False, "error": "Invalid ticker"}

    sh = _to_float(shares)
    sp = _to_float(sell_price)
    if sh is None or sh <= 0:
        return {"ok": False, "error": "Shares must be > 0"}
    if sp is None or sp < 0:
        return {"ok": False, "error": "Sell price required"}

    holdings = get_holdings(account)
    h = holdings.get(t)
    if not h:
        return {"ok": False, "error": f"{t} not in holdings"}

    current_shares = float(h.get("shares") or 0)
    if sh > current_shares + 0.0001:  # tolerance
        return {"ok": False, "error": f"Selling {sh} but only {current_shares} held"}

    date_iso = _validate_date(date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sub = h.get("subaccount") or DEFAULT_SUBACCOUNT

    new_shares = current_shares - sh
    old = dict(h)

    if new_shares <= 0.0001:
        # Remove entirely
        del holdings[t]
        result_holding = None
    else:
        h["shares"] = new_shares
        h["last_updated"] = _now_iso()
        result_holding = h

    if not _save(_key_holdings(account), holdings):
        return {"ok": False, "error": "Save failed"}

    # Cash event: sell proceeds credit cash
    cash_amount = sh * sp
    add_cash_event(
        account, "share_sell", cash_amount,
        subaccount=sub, date=date_iso,
        note=note or f"Sell {sh} {t} @ ${sp}",
        ref_id=t,
    )

    _audit(account, "sell_holding", t, old, {"sold_shares": sh, "sell_price": sp, "remaining": result_holding})
    return {"ok": True, "ticker": t, "remaining": result_holding, "proceeds": cash_amount}
def delete_holding(account: str, ticker: str, also_delete_cash: bool = False) -> Dict:
    """Hard-remove a ticker from holdings. Optionally undo linked cash events
    (the buy event from add_holding)."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    t = _validate_ticker(ticker)
    if not t:
        return {"ok": False, "error": "Invalid ticker"}
    holdings = get_holdings(account)
    if t not in holdings:
        return {"ok": False, "error": "Holding not found"}

    target = dict(holdings[t])
    linked = find_linked_cash_events_for_holding(account, t)
    cash_deleted_count = 0
    if also_delete_cash and linked:
        ids = [e["id"] for e in linked]
        cr = delete_cash_events_bulk(account, ids)
        cash_deleted_count = cr.get("deleted", 0)

    del holdings[t]
    if not _save(_key_holdings(account), holdings):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "delete_holding", t, target, {"linked_cash_deleted": cash_deleted_count})
    return {
        "ok": True,
        "deleted": target,
        "linked_cash_count": len(linked),
        "linked_cash_deleted": cash_deleted_count,
        "new_balance": calc_cash_balance(account),
    }


# ─────────────────────────────────────────────────────────
# Inspect linked artifacts (used by delete confirmation UI)
# ─────────────────────────────────────────────────────────

def inspect_option_links(account: str, opt_id: str) -> Dict:
    """Return what would be affected by deleting this option.

    For rolls, this includes the OPPOSITE side of the roll (if you're deleting
    a 'rolled' option, the new one created from the roll is linked; if you're
    deleting the new-from-roll option, the rolled one it came from is linked).
    """
    if not _validate_account(account):
        return {}
    options = get_options(account)
    target = next((o for o in options if isinstance(o, dict) and o.get("id") == opt_id), None)
    if not target:
        return {}

    linked_cash = find_linked_cash_events(account, opt_id)
    linked_options: List[Dict] = []

    # If this option was rolled, find the new one created from it
    if target.get("status") == "rolled" and target.get("rolled_to"):
        new_opt = next((o for o in options if o.get("id") == target["rolled_to"]), None)
        if new_opt:
            linked_options.append({"role": "rolled_to_this", "option": new_opt})
            # Also pull cash for the new option
            linked_cash += find_linked_cash_events(account, new_opt["id"])

    # If this option was created from a roll, find the original
    if target.get("rolled_from"):
        orig = next((o for o in options if o.get("id") == target["rolled_from"]), None)
        if orig:
            linked_options.append({"role": "rolled_from_this", "option": orig})

    return {
        "target": target,
        "linked_cash": linked_cash,
        "linked_options": linked_options,
    }


def inspect_holding_links(account: str, ticker: str) -> Dict:
    if not _validate_account(account):
        return {}
    t = _validate_ticker(ticker)
    if not t:
        return {}
    holdings = get_holdings(account)
    if t not in holdings:
        return {}
    return {
        "target": holdings[t],
        "ticker": t,
        "linked_cash": find_linked_cash_events_for_holding(account, t),
    }


def inspect_spread_links(account: str, spread_id: str) -> Dict:
    if not _validate_account(account):
        return {}
    spreads = get_spreads(account)
    target = next((s for s in spreads if isinstance(s, dict) and s.get("id") == spread_id), None)
    if not target:
        return {}
    return {
        "target": target,
        "linked_cash": find_linked_cash_events(account, spread_id),
    }




def get_lumpsum(account: str) -> List[Dict]:
    if not _validate_account(account):
        return []
    return _load(_key_lumpsum(account), [])


def add_lumpsum(account: str, label: str, value: float,
                subaccount: str = None, as_of: str = None,
                note: str = None) -> Dict:
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    label = (label or "").strip()
    if not label:
        return {"ok": False, "error": "Label required"}
    val = _to_float(value)
    if val is None or val < 0:
        return {"ok": False, "error": "Value required"}
    date_iso = _validate_date(as_of) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sub = (subaccount or "").strip() or DEFAULT_SUBACCOUNT

    entry = {
        "id": _gen_id("lump"),
        "label": label,
        "value": round(val, 2),
        "subaccount": sub,
        "as_of": date_iso,
        "note": (note or "").strip(),
        "created_at": _now_iso(),
        "last_updated": _now_iso(),
    }
    items = get_lumpsum(account)
    items.append(entry)
    if not _save(_key_lumpsum(account), items):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "add_lumpsum", entry["id"], None, entry)
    return {"ok": True, "entry": entry}


def update_lumpsum(account: str, entry_id: str, value: float = None,
                   as_of: str = None, label: str = None,
                   note: str = None) -> Dict:
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    items = get_lumpsum(account)
    target = next((e for e in items if e.get("id") == entry_id), None)
    if not target:
        return {"ok": False, "error": "Lump-sum entry not found"}

    old = dict(target)
    if value is not None:
        v = _to_float(value)
        if v is None or v < 0:
            return {"ok": False, "error": "Invalid value"}
        target["value"] = round(v, 2)
    if as_of is not None:
        d = _validate_date(as_of)
        if not d:
            return {"ok": False, "error": "Invalid date"}
        target["as_of"] = d
    if label is not None:
        target["label"] = (label or "").strip() or target["label"]
    if note is not None:
        target["note"] = (note or "").strip()
    target["last_updated"] = _now_iso()

    if not _save(_key_lumpsum(account), items):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "update_lumpsum", entry_id, old, target)
    return {"ok": True, "entry": target}


def delete_lumpsum(account: str, entry_id: str) -> Dict:
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    items = get_lumpsum(account)
    target = next((e for e in items if e.get("id") == entry_id), None)
    if not target:
        return {"ok": False, "error": "Not found"}
    items = [e for e in items if e.get("id") != entry_id]
    if not _save(_key_lumpsum(account), items):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "delete_lumpsum", entry_id, target, None)
    return {"ok": True}


# ═════════════════════════════════════════════════════════
# OPTIONS
# Categories: Wheel CSP / Wheel CC / Long Call / Long Put / Earnings /
#             Iron Condor / Inverse Condor
# (Spreads have their own structure, see SPREADS section)
# ═════════════════════════════════════════════════════════

VALID_OPTION_TYPES = {"CSP", "CC", "LONG_CALL", "LONG_PUT"}
VALID_OPTION_DIRECTIONS = {"sell", "buy"}
VALID_OPTION_STATUSES = {"open", "closed", "expired", "assigned", "rolled"}


def get_options(account: str) -> List[Dict]:
    if not _validate_account(account):
        return []
    return _load(_key_options(account), [])


def get_open_options(account: str) -> List[Dict]:
    return [o for o in get_options(account) if isinstance(o, dict) and o.get("status") == "open"]


def add_option(account: str, ticker: str, opt_type: str,
               strike: float, exp: str, premium: float, contracts: int = 1,
               direction: str = None, subaccount: str = None,
               category: str = None, open_date: str = None,
               note: str = None) -> Dict:
    """Add a new option position."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    t = _validate_ticker(ticker)
    if not t:
        return {"ok": False, "error": "Invalid ticker"}

    opt_type = (opt_type or "").strip().upper()
    if opt_type not in VALID_OPTION_TYPES:
        return {"ok": False, "error": f"Type must be one of {sorted(VALID_OPTION_TYPES)}"}

    # Default direction by type
    if not direction:
        direction = "sell" if opt_type in ("CSP", "CC") else "buy"
    direction = direction.strip().lower()
    if direction not in VALID_OPTION_DIRECTIONS:
        return {"ok": False, "error": "Direction must be 'sell' or 'buy'"}

    strike_f = _to_float(strike)
    premium_f = _to_float(premium)
    contracts_i = _to_int(contracts, default=1)
    if strike_f is None or strike_f <= 0:
        return {"ok": False, "error": "Strike required"}
    if premium_f is None or premium_f < 0:
        return {"ok": False, "error": "Premium required"}
    if contracts_i is None or contracts_i <= 0:
        return {"ok": False, "error": "Contracts must be > 0"}

    exp_iso = _validate_date(exp)
    if not exp_iso:
        return {"ok": False, "error": "Expiration date required (YYYY-MM-DD)"}

    open_iso = _validate_date(open_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sub = (subaccount or "").strip() or DEFAULT_SUBACCOUNT

    # Auto-set category if not given
    if not category:
        if opt_type == "CSP":
            category = "Wheel CSP"
        elif opt_type == "CC":
            category = "Wheel CC"
        elif opt_type == "LONG_CALL":
            category = "Long Call"
        elif opt_type == "LONG_PUT":
            category = "Long Put"

    opt = {
        "id": _gen_id("opt"),
        "ticker": t,
        "type": opt_type,
        "strike": strike_f,
        "exp": exp_iso,
        "premium": round(premium_f, 4),
        "contracts": contracts_i,
        "direction": direction,
        "status": "open",
        "subaccount": sub,
        "category": category,
        "tag": "wheel" if opt_type in ("CSP", "CC") else "",
        "open_date": open_iso,
        "note": (note or "").strip(),
        "created_at": _now_iso(),
    }

    options = get_options(account)
    options.append(opt)
    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}

    # Cash event: sell-to-open credits cash, buy-to-open debits
    if direction == "sell":
        cash_amount = premium_f * contracts_i * 100
    else:
        cash_amount = -(premium_f * contracts_i * 100)

    add_cash_event(
        account, "option_open", cash_amount,
        subaccount=sub, date=open_iso,
        note=f"{direction.upper()} {opt_type} {t} {strike_f} {exp_iso} @ {premium_f}",
        ref_id=opt["id"],
    )

    _audit(account, "add_option", opt["id"], None, opt)
    return {"ok": True, "option": opt}


def close_option(account: str, opt_id: str, status: str,
                 close_premium: float = None, close_date: str = None,
                 note: str = None) -> Dict:
    """Close an open option. status: closed | expired | assigned | rolled.

    For 'rolled', use the dedicated roll_option() helper instead — this just
    marks status=rolled if you want to do it manually.
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    status = (status or "").strip().lower()
    if status not in VALID_OPTION_STATUSES or status == "open":
        return {"ok": False, "error": f"Status must be one of: closed, expired, assigned, rolled"}

    options = get_options(account)
    target = next((o for o in options if isinstance(o, dict) and o.get("id") == opt_id), None)
    if not target:
        return {"ok": False, "error": "Option not found"}
    if target.get("status") != "open":
        return {"ok": False, "error": f"Option already {target.get('status')}"}

    old = dict(target)

    cp = _to_float(close_premium, default=0.0)
    if cp is None or cp < 0:
        cp = 0.0

    close_iso = _validate_date(close_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    target["status"] = status
    target["close_premium"] = round(cp, 4)
    target["close_date"] = close_iso
    target["close_note"] = (note or "").strip()
    target["closed_at"] = _now_iso()

    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}

    # Cash event for buy-to-close (only if status='closed' with non-zero cp)
    sub = target.get("subaccount") or DEFAULT_SUBACCOUNT
    contracts = int(target.get("contracts") or 1)
    direction = target.get("direction", "sell")

    if status == "closed" and cp > 0:
        # Sold-to-open being closed: buy-to-close debits cash by cp*contracts*100
        if direction == "sell":
            cash_amount = -(cp * contracts * 100)
        else:
            # Bought-to-open being closed: sell-to-close credits cash
            cash_amount = cp * contracts * 100
        add_cash_event(
            account, "option_close", cash_amount,
            subaccount=sub, date=close_iso,
            note=f"BTC {target.get('type')} {target.get('ticker')} {target.get('strike')} @ {cp}",
            ref_id=opt_id,
        )
    # expired/assigned/rolled — no extra cash event for the close itself
    # (assignment will be handled separately in handle_assignment if user chooses)

    _audit(account, f"option_{status}", opt_id, old, target)
    return {"ok": True, "option": target}


def edit_option(account: str, opt_id: str, **fields) -> Dict:
    """Edit fields on an existing option (correction, not state change)."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    options = get_options(account)
    target = next((o for o in options if isinstance(o, dict) and o.get("id") == opt_id), None)
    if not target:
        return {"ok": False, "error": "Option not found"}
    old = dict(target)
    editable = {"strike", "exp", "premium", "contracts", "subaccount",
                "category", "tag", "open_date", "note"}
    for k, v in fields.items():
        if k not in editable or v is None:
            continue
        if k in ("strike", "premium"):
            f = _to_float(v)
            if f is not None:
                target[k] = round(f, 4)
        elif k == "contracts":
            i = _to_int(v)
            if i is not None and i > 0:
                target[k] = i
        elif k in ("exp", "open_date"):
            d = _validate_date(v)
            if d:
                target[k] = d
        else:
            target[k] = str(v).strip()
    target["last_updated"] = _now_iso()
    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "edit_option", opt_id, old, target)
    return {"ok": True, "option": target}


def find_linked_cash_events(account: str, ref_id: str) -> List[Dict]:
    """Return cash events linked to a position by ref_id."""
    if not _validate_account(account) or not ref_id:
        return []
    ledger = get_cash_ledger(account)
    return [e for e in ledger if isinstance(e, dict) and e.get("ref_id") == ref_id]


def find_linked_cash_events_for_holding(account: str, ticker: str) -> List[Dict]:
    """Return all cash events whose ref_id matches a ticker (for holdings)."""
    if not _validate_account(account) or not ticker:
        return []
    t = ticker.upper()
    ledger = get_cash_ledger(account)
    return [e for e in ledger if isinstance(e, dict) and (e.get("ref_id") or "").upper() == t]


def delete_cash_events_bulk(account: str, event_ids: List[str]) -> Dict:
    """Remove multiple cash events at once. Used when 'fully undo' deletes
    a position with linked cash events."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    if not event_ids:
        return {"ok": True, "deleted": 0}
    ledger = get_cash_ledger(account)
    keep = [e for e in ledger if not (isinstance(e, dict) and e.get("id") in event_ids)]
    deleted_count = len(ledger) - len(keep)
    if deleted_count == 0:
        return {"ok": True, "deleted": 0}
    if not _save(_key_cash_ledger(account), keep):
        return {"ok": False, "error": "Save failed"}
    new_balance = calc_cash_balance(account)
    _save(_key_cash(account), {"cash_balance": new_balance, "last_updated": _now_iso()})
    _audit(account, "cash_bulk_delete", ",".join(event_ids), {"count": deleted_count}, None)
    return {"ok": True, "deleted": deleted_count, "new_balance": new_balance}


def delete_option(account: str, opt_id: str, also_delete_cash: bool = False) -> Dict:
    """Delete an option (rare; for cleanup of bad entries).

    If also_delete_cash=True, also removes any cash ledger events linked to
    this option's id. This is the safe way to fully undo a position.
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    options = get_options(account)
    target = next((o for o in options if isinstance(o, dict) and o.get("id") == opt_id), None)
    if not target:
        return {"ok": False, "error": "Option not found"}

    # Find linked cash events
    linked = find_linked_cash_events(account, opt_id)
    cash_deleted_count = 0
    if also_delete_cash and linked:
        ids = [e["id"] for e in linked]
        cr = delete_cash_events_bulk(account, ids)
        cash_deleted_count = cr.get("deleted", 0)

    options = [o for o in options if not (isinstance(o, dict) and o.get("id") == opt_id)]
    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "delete_option", opt_id, target, {"linked_cash_deleted": cash_deleted_count})
    return {
        "ok": True,
        "deleted": target,
        "linked_cash_count": len(linked),
        "linked_cash_deleted": cash_deleted_count,
        "new_balance": calc_cash_balance(account),
    }


# ═════════════════════════════════════════════════════════
# ROLL — close one option + open a replacement, single net credit/debit
# ═════════════════════════════════════════════════════════

def roll_option(account: str, opt_id: str,
                new_strike: float, new_exp: str, new_premium: float,
                close_premium: float, roll_date: str = None,
                note: str = None) -> Dict:
    """Roll: close existing option, open new one, log NET cash event."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    options = get_options(account)
    target = next((o for o in options if isinstance(o, dict) and o.get("id") == opt_id), None)
    if not target:
        return {"ok": False, "error": "Option not found"}
    if target.get("status") != "open":
        return {"ok": False, "error": f"Option is {target.get('status')}, can only roll open positions"}

    new_strike_f = _to_float(new_strike)
    new_prem_f = _to_float(new_premium)
    close_prem_f = _to_float(close_premium)
    if new_strike_f is None or new_strike_f <= 0:
        return {"ok": False, "error": "New strike required"}
    if new_prem_f is None or new_prem_f < 0:
        return {"ok": False, "error": "New premium required"}
    if close_prem_f is None or close_prem_f < 0:
        return {"ok": False, "error": "Close premium required"}

    new_exp_iso = _validate_date(new_exp)
    if not new_exp_iso:
        return {"ok": False, "error": "New expiration required"}
    roll_iso = _validate_date(roll_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Close the old option
    old = dict(target)
    target["status"] = "rolled"
    target["close_premium"] = round(close_prem_f, 4)
    target["close_date"] = roll_iso
    target["closed_at"] = _now_iso()
    target["rolled_to"] = None  # set below

    # Open a new option mirroring the original's structure
    direction = target.get("direction", "sell")
    contracts = int(target.get("contracts") or 1)
    sub = target.get("subaccount") or DEFAULT_SUBACCOUNT

    new_opt = {
        "id": _gen_id("opt"),
        "ticker": target.get("ticker"),
        "type": target.get("type"),
        "strike": new_strike_f,
        "exp": new_exp_iso,
        "premium": round(new_prem_f, 4),
        "contracts": contracts,
        "direction": direction,
        "status": "open",
        "subaccount": sub,
        "category": "Wheel Roll",
        "tag": target.get("tag", ""),
        "open_date": roll_iso,
        "note": (note or "").strip(),
        "rolled_from": opt_id,
        "created_at": _now_iso(),
    }

    target["rolled_to"] = new_opt["id"]
    options.append(new_opt)
    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}

    # Single net cash event for the roll
    if direction == "sell":
        # Net = new premium received − close premium paid
        net_credit = (new_prem_f - close_prem_f) * contracts * 100
    else:
        net_credit = (close_prem_f - new_prem_f) * contracts * 100

    event_type = "roll_credit" if net_credit >= 0 else "roll_debit"
    add_cash_event(
        account, event_type, net_credit,
        subaccount=sub, date=roll_iso,
        note=f"Roll {target.get('ticker')} → ${new_strike_f} {new_exp_iso}",
        ref_id=new_opt["id"],
    )

    _audit(account, "roll_option", opt_id, old, {"closed": target, "new": new_opt})
    return {"ok": True, "closed": target, "new_option": new_opt, "net_credit": round(net_credit, 2)}


# ═════════════════════════════════════════════════════════
# SPREADS
# Iron Condor and Inverse Condor live here too (4-leg) — represented as
# two-leg spread for now; phase 4.5 may add explicit IC modeling.
# ═════════════════════════════════════════════════════════

VALID_SPREAD_TYPES = {"BULL_PUT", "BEAR_CALL", "BULL_CALL", "BEAR_PUT", "IRON_CONDOR", "INVERSE_CONDOR"}


def get_spreads(account: str) -> List[Dict]:
    if not _validate_account(account):
        return []
    return _load(_key_spreads(account), [])


def add_spread(account: str, ticker: str, spread_type: str,
               long_strike: float, short_strike: float, exp: str,
               net: float, contracts: int = 1, is_credit: bool = True,
               subaccount: str = None, open_date: str = None,
               note: str = None) -> Dict:
    """Add a credit or debit spread."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    t = _validate_ticker(ticker)
    if not t:
        return {"ok": False, "error": "Invalid ticker"}
    spread_type = (spread_type or "").strip().upper()
    if spread_type not in VALID_SPREAD_TYPES:
        return {"ok": False, "error": f"Spread type must be one of {sorted(VALID_SPREAD_TYPES)}"}

    long_f = _to_float(long_strike)
    short_f = _to_float(short_strike)
    net_f = _to_float(net)
    contracts_i = _to_int(contracts, default=1)

    if long_f is None or short_f is None:
        return {"ok": False, "error": "Both strikes required"}
    if net_f is None or net_f < 0:
        return {"ok": False, "error": "Net price required"}
    if contracts_i is None or contracts_i <= 0:
        return {"ok": False, "error": "Contracts must be > 0"}

    exp_iso = _validate_date(exp)
    if not exp_iso:
        return {"ok": False, "error": "Expiration required"}

    open_iso = _validate_date(open_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sub = (subaccount or "").strip() or DEFAULT_SUBACCOUNT

    spread = {
        "id": _gen_id("spr"),
        "ticker": t,
        "type": spread_type,
        "long_strike": long_f,
        "short_strike": short_f,
        "exp": exp_iso,
        "credit": round(net_f, 4) if is_credit else 0,
        "debit": round(net_f, 4) if not is_credit else 0,
        "contracts": contracts_i,
        "direction": "sell" if is_credit else "buy",
        "status": "open",
        "subaccount": sub,
        "category": "Spread" if spread_type not in ("IRON_CONDOR", "INVERSE_CONDOR") else (
            "Iron Condor" if spread_type == "IRON_CONDOR" else "Inverse Condor"
        ),
        "open_date": open_iso,
        "note": (note or "").strip(),
        "created_at": _now_iso(),
    }

    spreads = get_spreads(account)
    spreads.append(spread)
    if not _save(_key_spreads(account), spreads):
        return {"ok": False, "error": "Save failed"}

    cash_amount = (net_f if is_credit else -net_f) * contracts_i * 100
    add_cash_event(
        account, "spread_open", cash_amount,
        subaccount=sub, date=open_iso,
        note=f"{spread_type} {t} {long_f}/{short_f} {exp_iso}",
        ref_id=spread["id"],
    )

    _audit(account, "add_spread", spread["id"], None, spread)
    return {"ok": True, "spread": spread}


def close_spread(account: str, spread_id: str, status: str,
                 close_value: float = None, close_date: str = None,
                 note: str = None) -> Dict:
    """Close a spread. status: closed | expired."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    status = (status or "").strip().lower()
    if status not in ("closed", "expired"):
        return {"ok": False, "error": "Status must be 'closed' or 'expired'"}

    spreads = get_spreads(account)
    target = next((s for s in spreads if isinstance(s, dict) and s.get("id") == spread_id), None)
    if not target:
        return {"ok": False, "error": "Spread not found"}
    if target.get("status") != "open":
        return {"ok": False, "error": f"Spread already {target.get('status')}"}

    old = dict(target)
    cv = _to_float(close_value, default=0.0) or 0.0
    close_iso = _validate_date(close_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    target["status"] = status
    target["close_value"] = round(cv, 4)
    target["close_date"] = close_iso
    target["close_note"] = (note or "").strip()
    target["closed_at"] = _now_iso()

    if not _save(_key_spreads(account), spreads):
        return {"ok": False, "error": "Save failed"}

    sub = target.get("subaccount") or DEFAULT_SUBACCOUNT
    contracts = int(target.get("contracts") or 1)
    is_credit = bool(target.get("credit"))

    if status == "closed" and cv > 0:
        # Buy back to close (credit spread) debits, sell to close (debit) credits
        if is_credit:
            cash_amount = -(cv * contracts * 100)
        else:
            cash_amount = cv * contracts * 100
        add_cash_event(
            account, "spread_close", cash_amount,
            subaccount=sub, date=close_iso,
            note=f"Close {target.get('type')} {target.get('ticker')}",
            ref_id=spread_id,
        )

    _audit(account, f"spread_{status}", spread_id, old, target)
    return {"ok": True, "spread": target}


def delete_spread(account: str, spread_id: str, also_delete_cash: bool = False) -> Dict:
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    spreads = get_spreads(account)
    target = next((s for s in spreads if isinstance(s, dict) and s.get("id") == spread_id), None)
    if not target:
        return {"ok": False, "error": "Spread not found"}

    linked = find_linked_cash_events(account, spread_id)
    cash_deleted_count = 0
    if also_delete_cash and linked:
        ids = [e["id"] for e in linked]
        cr = delete_cash_events_bulk(account, ids)
        cash_deleted_count = cr.get("deleted", 0)

    spreads = [s for s in spreads if not (isinstance(s, dict) and s.get("id") == spread_id)]
    if not _save(_key_spreads(account), spreads):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "delete_spread", spread_id, target, {"linked_cash_deleted": cash_deleted_count})
    return {
        "ok": True,
        "deleted": target,
        "linked_cash_count": len(linked),
        "linked_cash_deleted": cash_deleted_count,
        "new_balance": calc_cash_balance(account),
    }


# ═════════════════════════════════════════════════════════
# TRANSFERS — Kyleigh + Clay
# ═════════════════════════════════════════════════════════

def get_transfer_ledger(recipient: str) -> List[Dict]:
    recipient = (recipient or "").strip().lower()
    if recipient not in TRANSFER_RECIPIENTS:
        return []
    return _load(_key_transfers(recipient), [])


def calc_transfer_balance(recipient: str) -> float:
    ledger = get_transfer_ledger(recipient)
    return round(sum(float(e.get("amount") or 0) for e in ledger if isinstance(e, dict)), 2)


def add_transfer(recipient: str, account: str, amount: float,
                 subaccount: str = None, date: str = None,
                 note: str = None) -> Dict:
    """Transfer from `account` to `recipient`. Positive amount = transfer out
    of account. Negative amount = reverse transfer (recipient sends back)."""
    recipient = (recipient or "").strip().lower()
    if recipient not in TRANSFER_RECIPIENTS:
        return {"ok": False, "error": f"Recipient must be one of {TRANSFER_RECIPIENTS}"}
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid source account"}
    amt = _to_float(amount)
    if amt is None or amt == 0:
        return {"ok": False, "error": "Amount required (positive or negative non-zero)"}

    date_iso = _validate_date(date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sub = (subaccount or "").strip() or DEFAULT_SUBACCOUNT

    transfer_id = _gen_id("xfer")

    # Recipient ledger entry (positive amount = received)
    recipient_entry = {
        "id": transfer_id,
        "amount": round(amt, 2),
        "from_account": account,
        "from_subaccount": sub,
        "date": date_iso,
        "note": (note or "").strip(),
        "created_at": _now_iso(),
    }
    ledger = get_transfer_ledger(recipient)
    ledger.append(recipient_entry)
    if not _save(_key_transfers(recipient), ledger):
        return {"ok": False, "error": "Save to ledger failed"}

    # Source account cash event (negative amount = transferred out)
    add_cash_event(
        account, "transfer_out", -amt,
        subaccount=sub, date=date_iso,
        note=f"Transfer to {recipient.title()}: {note or ''}".strip(),
        ref_id=transfer_id,
    )

    _audit("system", f"transfer_{recipient}", transfer_id, None, {
        "recipient": recipient, "from_account": account, "amount": amt, "date": date_iso,
    })

    return {
        "ok": True,
        "transfer_id": transfer_id,
        "recipient_balance": calc_transfer_balance(recipient),
    }


def delete_transfer(recipient: str, transfer_id: str) -> Dict:
    """Remove a transfer (cleanup only — does not auto-reverse the cash event)."""
    recipient = (recipient or "").strip().lower()
    if recipient not in TRANSFER_RECIPIENTS:
        return {"ok": False, "error": "Invalid recipient"}
    ledger = get_transfer_ledger(recipient)
    target = next((e for e in ledger if isinstance(e, dict) and e.get("id") == transfer_id), None)
    if not target:
        return {"ok": False, "error": "Transfer not found"}
    ledger = [e for e in ledger if not (isinstance(e, dict) and e.get("id") == transfer_id)]
    if not _save(_key_transfers(recipient), ledger):
        return {"ok": False, "error": "Save failed"}
    _audit("system", f"transfer_{recipient}_delete", transfer_id, target, None)
    return {"ok": True, "deleted": target}


# ═════════════════════════════════════════════════════════
# WIPE — Settings sub-tab
# ═════════════════════════════════════════════════════════

def wipe_account(account: str, confirmation: str) -> Dict:
    """Wipe portfolio data for an account.

    Takes a snapshot first (to keep an audit trail), then clears Redis keys.
    Requires confirmation == 'WIPE {account}' to proceed.
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    expected = f"WIPE {account.upper()}"
    if (confirmation or "").strip().upper() != expected:
        return {"ok": False, "error": f"Confirmation must be '{expected}'"}

    # Snapshot first
    try:
        from . import durability
        snap_result = durability.take_snapshot()
    except Exception as e:
        log.warning(f"pre-wipe snapshot failed: {e}")
        snap_result = {"ok": False, "error": str(e)}

    # Clear all phase 4 keys for this account
    keys_to_clear = [
        _key_holdings(account),
        _key_options(account),
        _key_spreads(account),
        _key_cash(account),
        _key_cash_ledger(account),
        _key_lumpsum(account),
    ]

    cleared = []
    for key in keys_to_clear:
        if _save(key, [] if "ledger" in key or "options" in key or "spreads" in key or "lumpsum" in key else {}):
            cleared.append(key)

    _audit(account, "WIPE_ACCOUNT", account, {"snapshot": snap_result.get("tab")}, {"keys_cleared": cleared})

    return {
        "ok": True,
        "snapshot_taken": snap_result.get("ok", False),
        "snapshot_tab": snap_result.get("tab"),
        "keys_cleared": cleared,
    }


def wipe_all(confirmation: str) -> Dict:
    """Wipe ALL accounts. Requires 'WIPE ALL ACCOUNTS' confirmation."""
    if (confirmation or "").strip().upper() != "WIPE ALL ACCOUNTS":
        return {"ok": False, "error": "Confirmation must be 'WIPE ALL ACCOUNTS'"}

    # Snapshot first
    try:
        from . import durability
        snap_result = durability.take_snapshot()
    except Exception as e:
        snap_result = {"ok": False, "error": str(e)}

    results = {}
    for acc in ALL_UNDERLYING_ACCOUNTS:
        # Use upper-case account in confirmation
        results[acc] = wipe_account(acc, f"WIPE {acc.upper()}")

    return {
        "ok": True,
        "snapshot_taken": snap_result.get("ok", False),
        "snapshot_tab": snap_result.get("tab"),
        "results": results,
    }


# ═════════════════════════════════════════════════════════
# SUMMARY for Portfolio page (live, reads Redis directly)
# ═════════════════════════════════════════════════════════

def portfolio_page_data(active_underlying: str = None) -> Dict:
    """Aggregated data for the Portfolio entry page.

    Returns live counts and recent entries for each underlying account so
    the entry forms can show what's already there.
    """
    # Default to brad if not specified
    if not active_underlying or active_underlying not in ALL_UNDERLYING_ACCOUNTS:
        active_underlying = "brad"

    acc = active_underlying

    holdings = get_holdings(acc)
    options = get_options(acc)
    open_options = [o for o in options if o.get("status") == "open"]
    spreads = get_spreads(acc)
    open_spreads = [s for s in spreads if s.get("status") == "open"]
    lumpsum = get_lumpsum(acc)
    cash_balance = calc_cash_balance(acc)
    cash_breakdown = calc_cash_breakdown(acc)
    cash_ledger = get_cash_ledger(acc)

    return {
        "active_underlying": acc,
        "all_accounts": ALL_UNDERLYING_ACCOUNTS,
        "underlying_labels": UNDERLYING_LABELS,
        "underlying_themes": UNDERLYING_TO_THEME,
        "subaccounts": get_subaccounts(),
        "default_subaccount": DEFAULT_SUBACCOUNT,
        "holdings": holdings,
        "holdings_count": len(holdings),
        "options": options,
        "open_options": open_options,
        "open_options_count": len(open_options),
        "spreads": spreads,
        "open_spreads": open_spreads,
        "open_spreads_count": len(open_spreads),
        "lumpsum": lumpsum,
        "lumpsum_count": len(lumpsum),
        "cash_balance": cash_balance,
        "cash_breakdown": cash_breakdown,
        "cash_ledger": cash_ledger[-50:][::-1],  # last 50, newest first
        "kyleigh_balance": calc_transfer_balance("kyleigh"),
        "clay_balance": calc_transfer_balance("clay"),
        "kyleigh_ledger": get_transfer_ledger("kyleigh")[-50:][::-1],
        "clay_ledger": get_transfer_ledger("clay")[-50:][::-1],
    }


# ═════════════════════════════════════════════════════════
# UNDO LAST ACTION
# Reads the audit log and reverses a recent action by id.
# ═════════════════════════════════════════════════════════

# Operations that can be undone, with their reversal logic
UNDOABLE_OPS = {
    # Position adds → delete the created object + linked cash
    "add_holding", "add_to_holding", "add_option", "add_spread",
    "add_lumpsum",
    # Position closes → revert status, reverse close cash
    "option_closed", "option_expired", "option_assigned",
    "spread_closed", "spread_expired",
    # Cash events
    "cash_deposit", "cash_withdrawal", "cash_share_buy", "cash_share_sell",
    "cash_option_open", "cash_option_close", "cash_spread_open", "cash_spread_close",
    "cash_transfer_out", "cash_roll_credit", "cash_roll_debit",
    # Sells / sub-account changes
    "sell_holding",
    "edit_holding", "edit_option", "update_lumpsum",
    # Transfers
    "transfer_kyleigh", "transfer_clay",
    "roll_option",
}


def get_recent_audit_entries(limit: int = 20) -> List[Dict]:
    """Return recent audit entries with parsed payloads, newest first."""
    try:
        from . import durability
        raw = durability.list_audit_entries(limit=limit)
    except Exception as e:
        log.debug(f"audit list unavailable: {e}")
        return []

    parsed = []
    for entry in raw:
        try:
            old_v = json.loads(entry["old_value"]) if entry.get("old_value") else None
        except Exception:
            old_v = None
        try:
            new_v = json.loads(entry["new_value"]) if entry.get("new_value") else None
        except Exception:
            new_v = None

        parsed.append({
            "timestamp": entry.get("timestamp", ""),
            "account": entry.get("account", ""),
            "op": entry.get("op", ""),
            "target": entry.get("target", ""),
            "old_value": old_v,
            "new_value": new_v,
            "undoable": entry.get("op", "") in UNDOABLE_OPS,
        })
    return parsed


def undo_audit_entry(timestamp: str, op: str, account: str, target: str) -> Dict:
    """Reverse a single audit entry by best-effort.

    Strategy is op-specific:
      - add_X: delete the created X (with linked cash)
      - option_closed / option_expired / option_assigned: revert to open
      - cash_*: delete the cash entry
      - sell_holding: re-add the lot back (no cash reversal — sells happen in the past)
      - roll_option: delete new option, revert old option to open

    Returns {"ok": bool, "msg": str, ...}
    """
    if op not in UNDOABLE_OPS:
        return {"ok": False, "error": f"Operation '{op}' is not undoable"}

    # Fetch latest matching entry to ensure we reverse the right one
    entries = get_recent_audit_entries(limit=100)
    match = None
    for e in entries:
        if (e["timestamp"] == timestamp and e["op"] == op
                and e["account"] == account and e["target"] == target):
            match = e
            break
    if not match:
        return {"ok": False, "error": "Audit entry not found (may have been pruned)"}

    new_v = match.get("new_value")
    old_v = match.get("old_value")

    try:
        # ─── Position adds ───
        if op == "add_option" and isinstance(new_v, dict):
            opt_id = new_v.get("id")
            if not opt_id:
                return {"ok": False, "error": "Option ID missing"}
            r = delete_option(account, opt_id, also_delete_cash=True)
            if r.get("ok"):
                return {"ok": True, "msg": f"Reverted: deleted option + {r.get('linked_cash_deleted', 0)} cash event(s)"}
            return r

        if op == "add_spread" and isinstance(new_v, dict):
            spread_id = new_v.get("id")
            r = delete_spread(account, spread_id, also_delete_cash=True)
            if r.get("ok"):
                return {"ok": True, "msg": f"Reverted: deleted spread + {r.get('linked_cash_deleted', 0)} cash event(s)"}
            return r

        if op in ("add_holding", "add_to_holding") and isinstance(new_v, dict):
            ticker = target  # add_holding uses ticker as target
            r = delete_holding(account, ticker, also_delete_cash=True)
            if r.get("ok"):
                return {"ok": True, "msg": f"Reverted: removed {ticker} + {r.get('linked_cash_deleted', 0)} cash event(s)"}
            return r

        if op == "add_lumpsum" and isinstance(new_v, dict):
            entry_id = new_v.get("id")
            r = delete_lumpsum(account, entry_id)
            if r.get("ok"):
                return {"ok": True, "msg": "Reverted: lump-sum entry deleted"}
            return r

        # ─── Option closes (revert status to open) ───
        if op in ("option_closed", "option_expired", "option_assigned"):
            opt_id = target
            options = get_options(account)
            opt = next((o for o in options if o.get("id") == opt_id), None)
            if not opt:
                return {"ok": False, "error": "Option not found"}

            # Revert fields
            opt["status"] = "open"
            for k in ("close_premium", "close_date", "closed_at", "close_note"):
                opt.pop(k, None)
            _save(_key_options(account), options)

            # Delete linked close cash event(s) — only the close, not the open
            linked = find_linked_cash_events(account, opt_id)
            close_cash_ids = [c["id"] for c in linked if c.get("type") == "option_close"]
            if close_cash_ids:
                delete_cash_events_bulk(account, close_cash_ids)
            _audit(account, "UNDO_option_close", opt_id, None, {"reverted_status": "open"})
            return {"ok": True, "msg": f"Reverted: option re-opened + {len(close_cash_ids)} cash event(s) removed"}

        # ─── Spread closes ───
        if op in ("spread_closed", "spread_expired"):
            spread_id = target
            spreads = get_spreads(account)
            spr = next((s for s in spreads if s.get("id") == spread_id), None)
            if not spr:
                return {"ok": False, "error": "Spread not found"}
            spr["status"] = "open"
            for k in ("close_value", "close_date", "closed_at", "close_note"):
                spr.pop(k, None)
            _save(_key_spreads(account), spreads)
            linked = find_linked_cash_events(account, spread_id)
            close_cash_ids = [c["id"] for c in linked if c.get("type") == "spread_close"]
            if close_cash_ids:
                delete_cash_events_bulk(account, close_cash_ids)
            _audit(account, "UNDO_spread_close", spread_id, None, {"reverted_status": "open"})
            return {"ok": True, "msg": f"Reverted: spread re-opened"}

        # ─── Cash events ───
        if op.startswith("cash_") and op != "cash_bulk_delete":
            entry_id = target
            r = delete_cash_event(account, entry_id)
            if r.get("ok"):
                return {"ok": True, "msg": "Reverted: cash event deleted"}
            return r

        # ─── Sell holding ───
        if op == "sell_holding" and isinstance(old_v, dict):
            # Re-add the holding back
            ticker = target
            r = add_holding(
                account, ticker,
                shares=old_v.get("shares"),
                cost_basis=old_v.get("cost_basis"),
                subaccount=old_v.get("subaccount"),
                tag=old_v.get("tag"),
                date=old_v.get("first_added"),
            )
            if r.get("ok"):
                # Remove the cash credit from the sell that we just re-credited via add
                # Actually: add_holding generates a buy cash event. The sell event still
                # exists in the ledger. Net: we need to delete BOTH the original sell
                # cash AND the new buy cash (both wash). Find linked sell events:
                linked = find_linked_cash_events_for_holding(account, ticker)
                # Delete the most recent share_sell + most recent share_buy (the one we just made)
                sell_ids = [c["id"] for c in linked if c.get("type") == "share_sell"][:1]
                buy_ids = [c["id"] for c in linked if c.get("type") == "share_buy"][:1]
                ids_to_remove = sell_ids + buy_ids
                if ids_to_remove:
                    delete_cash_events_bulk(account, ids_to_remove)
                return {"ok": True, "msg": f"Reverted: {ticker} re-added, sell undone"}
            return r

        # ─── Roll option (revert: delete new opt, reopen old opt, remove roll cash) ───
        if op == "roll_option" and isinstance(new_v, dict):
            new_data = new_v.get("new") or new_v
            new_opt_id = new_data.get("id") if isinstance(new_data, dict) else None
            closed_old = new_v.get("closed") or {}
            old_opt_id = closed_old.get("id") if isinstance(closed_old, dict) else target

            # Delete the new option (and its cash event — the roll credit)
            if new_opt_id:
                delete_option(account, new_opt_id, also_delete_cash=True)

            # Re-open the old option
            options = get_options(account)
            old = next((o for o in options if o.get("id") == old_opt_id), None)
            if old:
                old["status"] = "open"
                for k in ("close_premium", "close_date", "closed_at", "rolled_to"):
                    old.pop(k, None)
                _save(_key_options(account), options)
            _audit(account, "UNDO_roll", old_opt_id, None, {"reverted": True})
            return {"ok": True, "msg": "Reverted: roll undone, old option reopened, new option deleted"}

        # ─── Transfers ───
        if op in ("transfer_kyleigh", "transfer_clay") and isinstance(new_v, dict):
            recipient = "kyleigh" if op == "transfer_kyleigh" else "clay"
            transfer_id = target
            from_account = new_v.get("from_account") or account
            # Delete from recipient ledger
            delete_transfer(recipient, transfer_id)
            # Find the linked cash event in the source account and delete it
            linked = find_linked_cash_events(from_account, transfer_id)
            if linked:
                delete_cash_events_bulk(from_account, [c["id"] for c in linked])
            return {"ok": True, "msg": f"Reverted: transfer to {recipient.title()} undone"}

        # ─── Edits — revert to old value ───
        if op == "edit_holding" and isinstance(old_v, dict):
            ticker = target
            holdings = get_holdings(account)
            holdings[ticker] = old_v
            _save(_key_holdings(account), holdings)
            return {"ok": True, "msg": f"Reverted: {ticker} restored to previous values"}

        if op == "edit_option" and isinstance(old_v, dict):
            opt_id = target
            options = get_options(account)
            for i, o in enumerate(options):
                if o.get("id") == opt_id:
                    options[i] = old_v
                    _save(_key_options(account), options)
                    return {"ok": True, "msg": "Reverted: option restored to previous values"}
            return {"ok": False, "error": "Option not found"}

        if op == "update_lumpsum" and isinstance(old_v, dict):
            entry_id = target
            items = get_lumpsum(account)
            for i, e in enumerate(items):
                if e.get("id") == entry_id:
                    items[i] = old_v
                    _save(_key_lumpsum(account), items)
                    return {"ok": True, "msg": "Reverted: lump-sum restored"}

        return {"ok": False, "error": f"Don't know how to undo '{op}'"}

    except Exception as e:
        log.warning(f"undo failed for {op}: {e}")
        return {"ok": False, "error": f"Undo error: {e}"}
