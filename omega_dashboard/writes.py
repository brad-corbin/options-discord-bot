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
# Phase 4.5+ — kyleigh and clay are "partner ledger" accounts. They have
# their own cash ledger but no holdings/options/spreads. They are NOT included
# in the Combined view (their cash isn't Brad's net worth).
ALL_UNDERLYING_ACCOUNTS = ["brad", "mom", "partner", "kyleigh", "clay"]

# Subset that holds real positions (used by Combined and the trading-focused
# parts of the dashboard).
TRADING_ACCOUNTS = ["brad", "mom", "partner"]

# Subset that's ledger-only — partner profit-sharing accounts.
PARTNER_LEDGER_ACCOUNTS = ["kyleigh", "clay"]

# Display labels for the right-pill on Portfolio entry pages.
UNDERLYING_LABELS = {
    "brad":    "Corbin",
    "mom":     "Volkman",
    "partner": "Partner",
    "kyleigh": "Kyleigh",
    "clay":    "Clay",
}

# Map underlying account → which top-chip color theme that account "belongs" to.
UNDERLYING_TO_THEME = {
    "brad":    "mine",
    "mom":     "mom",
    "partner": "partner",
    "kyleigh": "kyleigh",
    "clay":    "clay",
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

def _key_sold_lots(account: str) -> str:
    """Phase 4.5 — historical record of share sells with realized P&L."""
    return f"{account}:portfolio:sold_lots"

def _key_transfers(recipient: str) -> str:
    """Phase 4 — Kyleigh + Clay notional balance ledgers."""
    return f"transfers:{recipient}:ledger"

def _key_subaccount_list() -> str:
    """Phase 4 — user-extensible list of sub-account tags."""
    return "omega:subaccounts"


def _key_partner_host(partner: str) -> str:
    """Phase 4.5 — which trading account a partner's capital currently lives in.
    Stored value is "brad" / "mom" / "partner" / "" (no exclusion)."""
    return f"{partner}:portfolio:host_account"


def _key_wheel_campaigns(account: str) -> str:
    """Phase 4.5 — wheel campaign tracking (additive, never affects cash)."""
    return f"{account}:portfolio:wheel_campaigns"


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
        # Phase 4.5 — be forgiving: accept "$5,256.78" or "5,256.78" or " 100 "
        if isinstance(v, str):
            cleaned = v.strip().replace("$", "").replace(",", "").replace(" ", "")
            if cleaned == "" or cleaned == "-":
                return default
            return float(cleaned)
        return float(v)
    except Exception:
        return default


def _to_int(v, default=None) -> Optional[int]:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            cleaned = v.strip().replace(",", "").replace(" ", "")
            if cleaned == "":
                return default
            return int(float(cleaned))  # Allow "100.0" → 100
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


# ─────────────────────────────────────────────────────────
# Phase 4.5 — Partner host account (which trading account
# the partner's capital currently lives in)
# ─────────────────────────────────────────────────────────

def get_partner_host(partner: str) -> str:
    """Return which trading account this partner's capital sits in.
    Empty string means 'no exclusion configured' — partner balance won't
    be subtracted from any host's capital tracking."""
    if partner not in PARTNER_LEDGER_ACCOUNTS:
        return ""
    saved = _load(_key_partner_host(partner), None)
    if isinstance(saved, str) and saved in TRADING_ACCOUNTS:
        return saved
    return ""


def set_partner_host(partner: str, host: str) -> Dict:
    """Set which trading account a partner's capital lives in.
    Pass empty string to disable exclusion (partner is just a notional ledger)."""
    if partner not in PARTNER_LEDGER_ACCOUNTS:
        return {"ok": False, "error": f"'{partner}' is not a partner account"}
    host = (host or "").strip().lower()
    if host and host not in TRADING_ACCOUNTS:
        return {"ok": False, "error": f"Host must be empty or one of {TRADING_ACCOUNTS}"}
    prev = get_partner_host(partner)
    if _save(_key_partner_host(partner), host):
        _audit("system", "set_partner_host", partner, {"host": prev}, {"host": host})
        return {"ok": True, "partner": partner, "host": host}
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

    # Phase 4.5 — sign normalization based on event type.
    # User may have typed "+$100" for a withdrawal; we coerce to the right sign.
    # Skip normalization for manual_set (intentional sign control) and
    # transfer_out (handled by add_transfer's bi-directional logic).
    if event_type == "withdrawal" and amt > 0:
        amt = -amt
    elif event_type == "deposit" and amt < 0:
        amt = -amt
    elif event_type == "fee" and amt > 0:
        amt = -amt  # Fees are always expenses

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
                date: str = None,
                _from_assignment: bool = False) -> Dict:
    """Add or merge a share lot.

    If ticker already exists, this CREATES a new lot (we don't mutate the
    existing position — phase 4.5 may add cost-basis-merge logic, but for
    now multiple buys at different prices stay distinct).

    _from_assignment: internal flag set by close_option auto-handle. Used
    to suppress the duplicate campaign event (the close_option hook already
    records csp_assigned with shares).
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

    # Phase 4.5 — campaign hook (best-effort)
    try:
        from . import campaigns as _campaigns
        _campaigns.hook_holding_added(
            account, t, sh, cb, sub,
            is_assignment=_from_assignment,
        )
    except Exception as e:
        log.warning(f"campaign hook (add_holding) failed: {e}")

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
                 note: str = None,
                 _from_call_away: bool = False) -> Dict:
    """Reduce or remove a holding; logs a sell cash event.

    If shares == current shares, removes entirely.

    _from_call_away: internal flag set by close_option auto-handle when CC
    is called away. Suppresses duplicate campaign event tracking.
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

    # Phase 4.5 — record the sold lot for history view
    cost_basis = float(old.get("cost_basis") or 0)
    realized_pnl = round((sp - cost_basis) * sh, 2)
    sold_lots = get_sold_lots(account)
    sold_lots.append({
        "id": _gen_id("sl"),
        "ticker": t,
        "shares": sh,
        "sell_price": sp,
        "cost_basis_at_sale": cost_basis,
        "realized_pnl": realized_pnl,
        "date": date_iso,
        "subaccount": sub,
        "note": note or "",
        "logged_at": _now_iso(),
    })
    _save(_key_sold_lots(account), sold_lots)

    _audit(account, "sell_holding", t, old, {"sold_shares": sh, "sell_price": sp, "remaining": result_holding})

    # Phase 4.5 — campaign hook (best-effort)
    try:
        from . import campaigns as _campaigns
        _campaigns.hook_holding_sold(
            account, t, sh, sp, sub,
            is_call_away=_from_call_away,
        )
    except Exception as e:
        log.warning(f"campaign hook (sell_holding) failed: {e}")

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


def get_sold_lots(account: str) -> List[Dict]:
    """Historical record of share sells (closed share lots)."""
    if not _validate_account(account):
        return []
    return _load(_key_sold_lots(account), [])


def get_closed_options(account: str, since_date: str = None,
                        ticker_filter: str = None) -> List[Dict]:
    """Closed/expired/assigned/rolled options for history view.

    since_date: 'YYYY-MM-DD' — only return options with close_date >= this date
    ticker_filter: substring (case-insensitive) match on ticker
    """
    if not _validate_account(account):
        return []
    opts = _load(_key_options(account), [])
    closed_statuses = {"closed", "expired", "assigned", "rolled"}
    out = []
    for o in opts:
        if not isinstance(o, dict) or o.get("status") not in closed_statuses:
            continue
        if since_date:
            cd = o.get("close_date") or o.get("exp") or ""
            if cd < since_date:
                continue
        if ticker_filter:
            t = (o.get("ticker") or "").upper()
            if ticker_filter.upper() not in t:
                continue
        # Compute realized P&L
        try:
            premium = float(o.get("premium") or 0)
            close_premium = float(o.get("close_premium") or 0)
            contracts = int(o.get("contracts") or 1)
            direction = o.get("direction", "sell")
            if direction == "sell":
                pnl = round((premium - close_premium) * contracts * 100, 2)
            else:
                pnl = round((close_premium - premium) * contracts * 100, 2)
        except Exception:
            pnl = 0.0
        out.append({**o, "realized_pnl": pnl})
    # Newest first
    out.sort(key=lambda x: x.get("close_date") or x.get("exp") or "", reverse=True)
    return out


def get_closed_spreads(account: str, since_date: str = None,
                        ticker_filter: str = None) -> List[Dict]:
    """Closed/expired spreads for history view."""
    if not _validate_account(account):
        return []
    spreads = _load(_key_spreads(account), [])
    closed_statuses = {"closed", "expired"}
    out = []
    for s in spreads:
        if not isinstance(s, dict) or s.get("status") not in closed_statuses:
            continue
        if since_date:
            cd = s.get("close_date") or s.get("exp") or ""
            if cd < since_date:
                continue
        if ticker_filter:
            t = (s.get("ticker") or "").upper()
            if ticker_filter.upper() not in t:
                continue
        # Compute realized P&L
        try:
            contracts = int(s.get("contracts") or 1)
            net_open = float(s.get("credit") or 0) - float(s.get("debit") or 0)
            close_value = float(s.get("close_value") or 0)
            # If opened as credit: pnl = (open_credit - close_value) * contracts * 100
            # If opened as debit:  pnl = (close_value - open_debit) * contracts * 100
            if s.get("credit"):
                pnl = round((float(s.get("credit")) - close_value) * contracts * 100, 2)
            else:
                pnl = round((close_value - float(s.get("debit") or 0)) * contracts * 100, 2)
        except Exception:
            pnl = 0.0
        out.append({**s, "realized_pnl": pnl})
    out.sort(key=lambda x: x.get("close_date") or x.get("exp") or "", reverse=True)
    return out


def get_sold_lots_filtered(account: str, since_date: str = None,
                             ticker_filter: str = None) -> List[Dict]:
    """Sold lots history, filtered by date range and ticker."""
    lots = get_sold_lots(account)
    out = []
    for l in lots:
        if not isinstance(l, dict):
            continue
        if since_date:
            d = l.get("date") or ""
            if d < since_date:
                continue
        if ticker_filter:
            t = (l.get("ticker") or "").upper()
            if ticker_filter.upper() not in t:
                continue
        out.append(l)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


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

    # Phase 4.5 — wheel campaign tracking (best-effort, never raises)
    try:
        from . import campaigns as _campaigns
        _campaigns.hook_option_added(account, opt)
    except Exception as e:
        log.warning(f"campaign hook (add_option) failed: {e}")

    return {"ok": True, "option": opt}


def close_option(account: str, opt_id: str, status: str,
                 close_premium: float = None, close_date: str = None,
                 note: str = None, contracts_to_close: int = None,
                 auto_handle_shares: bool = True,
                 actual_fill_price: float = None) -> Dict:
    """Close an open option. status: closed | expired | assigned | rolled.

    contracts_to_close: if provided and < total contracts on the option,
    performs a PARTIAL close. Splits the position: the closed portion gets
    a new id with the close fields, and the remaining contracts stay open
    on the original id.

    For 'rolled', use the dedicated roll_option() helper instead — this just
    marks status=rolled if you want to do it manually.

    Phase 4.5 — auto-handle assignment:
      - When status='assigned' on a sell-side CSP and auto_handle_shares=True:
        automatically creates a share lot at strike (or actual_fill_price if
        provided), which debits cash via the existing add_holding path.
      - When status='assigned' on a sell-side CC and auto_handle_shares=True:
        automatically sells shares (called away) at strike, which credits
        cash via the existing sell_holding path.
      - The auto-handle audit op is `option_assigned_with_shares` (or
        `cc_called_away_with_shares` for CC) so retro-fix can distinguish
        from legacy `option_assigned`.
      - actual_fill_price overrides strike for both basis and cash math (rare).
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

    total_contracts = int(target.get("contracts") or 1)
    n_to_close = _to_int(contracts_to_close)
    if n_to_close is None or n_to_close <= 0 or n_to_close >= total_contracts:
        # Full close — use existing all-or-nothing path
        n_to_close = total_contracts
        is_partial = False
    else:
        is_partial = True

    old = dict(target)
    cp = _to_float(close_premium, default=0.0)
    if cp is None or cp < 0:
        cp = 0.0
    close_iso = _validate_date(close_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Resolve fill price for assignment (strike unless overridden)
    fill_f = _to_float(actual_fill_price)
    if fill_f is None or fill_f <= 0:
        fill_f = float(target.get("strike") or 0)

    # Snapshot fields needed after potential save
    sub = target.get("subaccount") or DEFAULT_SUBACCOUNT
    direction = target.get("direction", "sell")
    ticker = target.get("ticker")
    opt_type = target.get("type")
    strike = float(target.get("strike") or 0)

    # Determine whether to actually run auto-handle
    do_auto_handle = (
        bool(auto_handle_shares)
        and status == "assigned"
        and direction == "sell"
        and opt_type in ("CSP", "CC")
    )

    if is_partial:
        # Create a new "closed-portion" option record with n_to_close contracts
        closed_portion = dict(target)
        closed_portion["id"] = _gen_id("opt")
        closed_portion["contracts"] = n_to_close
        closed_portion["status"] = status
        closed_portion["close_premium"] = round(cp, 4)
        closed_portion["close_date"] = close_iso
        closed_portion["close_note"] = (note or "").strip()
        closed_portion["closed_at"] = _now_iso()
        closed_portion["partial_of"] = opt_id  # Reference to original
        if status == "assigned" and do_auto_handle:
            closed_portion["actual_fill_price"] = fill_f
            closed_portion["auto_handled_shares"] = True
        options.append(closed_portion)

        # Reduce original to remaining contracts
        target["contracts"] = total_contracts - n_to_close
        target["last_updated"] = _now_iso()

        if not _save(_key_options(account), options):
            return {"ok": False, "error": "Save failed"}

        # Cash event for the closed portion (BTC-style close only)
        if status == "closed" and cp > 0:
            if direction == "sell":
                cash_amount = -(cp * n_to_close * 100)
            else:
                cash_amount = cp * n_to_close * 100
            add_cash_event(
                account, "option_close", cash_amount,
                subaccount=sub, date=close_iso,
                note=f"BTC {n_to_close}/{total_contracts} {opt_type} {ticker} {strike} @ {cp}",
                ref_id=closed_portion["id"],
            )

        # Auto-handle for partial assignments
        auto_result = None
        if do_auto_handle:
            auto_result = _execute_auto_handle(
                account, closed_portion, n_to_close, fill_f, close_iso
            )

        audit_payload = {
            "original": old,
            "remaining_contracts": target["contracts"],
            "closed_portion_id": closed_portion["id"],
            "closed_contracts": n_to_close,
            "status": status,
            "close_premium": cp,
        }
        if auto_result:
            audit_payload["auto_handled"] = True
            audit_payload["auto_result"] = auto_result

        _audit(account, f"partial_close_option", opt_id, old, audit_payload)

        # Phase 4.5 — campaign hook for partial assignments
        if status == "assigned" and do_auto_handle and auto_result:
            try:
                from . import campaigns as _campaigns
                _campaigns.hook_option_closed(
                    account, closed_portion, status,
                    close_premium=cp, close_date=close_iso,
                    auto_handled_shares=True,
                    shares_acquired=auto_result.get("shares_acquired", 0),
                    shares_sold=auto_result.get("shares_sold", 0),
                    actual_fill_price=fill_f,
                )
            except Exception as e:
                log.warning(f"campaign hook (partial close) failed: {e}")

        return {
            "ok": True,
            "partial": True,
            "remaining": target,
            "closed_portion": closed_portion,
            "auto_handled": auto_result,
        }

    # Full close path
    target["status"] = status
    target["close_premium"] = round(cp, 4)
    target["close_date"] = close_iso
    target["close_note"] = (note or "").strip()
    target["closed_at"] = _now_iso()
    if status == "assigned" and do_auto_handle:
        target["actual_fill_price"] = fill_f
        target["auto_handled_shares"] = True

    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}

    contracts = int(total_contracts)

    if status == "closed" and cp > 0:
        if direction == "sell":
            cash_amount = -(cp * contracts * 100)
        else:
            cash_amount = cp * contracts * 100
        add_cash_event(
            account, "option_close", cash_amount,
            subaccount=sub, date=close_iso,
            note=f"BTC {opt_type} {ticker} {strike} @ {cp}",
            ref_id=opt_id,
        )

    # Phase 4.5 — auto-handle assignment for full closes
    auto_result = None
    if do_auto_handle:
        auto_result = _execute_auto_handle(
            account, target, contracts, fill_f, close_iso
        )

    # Audit op: distinguish auto-handled from legacy for retro-fix
    if status == "assigned" and do_auto_handle and auto_result:
        if opt_type == "CSP":
            audit_op = "option_assigned_with_shares"
        else:  # CC
            audit_op = "cc_called_away_with_shares"
    else:
        audit_op = f"option_{status}"

    _audit(account, audit_op, opt_id, old, target)

    # Phase 4.5 — campaign hook
    try:
        from . import campaigns as _campaigns
        _campaigns.hook_option_closed(
            account, target, status,
            close_premium=cp, close_date=close_iso,
            auto_handled_shares=do_auto_handle and (auto_result is not None),
            shares_acquired=(auto_result or {}).get("shares_acquired", 0),
            shares_sold=(auto_result or {}).get("shares_sold", 0),
            actual_fill_price=fill_f if do_auto_handle else None,
        )
    except Exception as e:
        log.warning(f"campaign hook (close_option) failed: {e}")

    result = {"ok": True, "option": target}
    if auto_result:
        result["auto_handled"] = auto_result
    return result


def _execute_auto_handle(account: str, opt: Dict, contracts_closed: int,
                           fill_price: float, close_date: str) -> Optional[Dict]:
    """Execute the share creation/removal that goes with an assignment.

    For CSP assigned: buy 100 × N shares at fill_price.
    For CC called away: sell 100 × N shares at fill_price.

    Returns a dict describing what happened, or None if nothing applicable.
    Mistakes here are non-fatal — the option close already succeeded.
    """
    try:
        opt_type = opt.get("type")
        ticker = opt.get("ticker")
        sub = opt.get("subaccount") or DEFAULT_SUBACCOUNT
        shares = 100 * int(contracts_closed)

        if opt_type == "CSP":
            # Buy shares (debits cash via add_holding)
            r = add_holding(
                account, ticker,
                shares=shares, cost_basis=fill_price,
                subaccount=sub, tag="wheel",
                date=close_date,
                _from_assignment=True,
            )
            if r.get("ok"):
                return {
                    "kind": "csp_assignment",
                    "shares_acquired": shares,
                    "fill_price": fill_price,
                    "cost": shares * fill_price,
                    "ticker": ticker,
                    "subaccount": sub,
                }
            log.warning(f"auto_handle add_holding failed: {r.get('error')}")
            return None

        elif opt_type == "CC":
            # Called away: sell shares (credits cash via sell_holding)
            r = sell_holding(
                account, ticker,
                shares=shares, sell_price=fill_price,
                date=close_date,
                note=f"Called away via CC (auto-handled)",
                _from_call_away=True,
            )
            if r.get("ok"):
                return {
                    "kind": "cc_called_away",
                    "shares_sold": shares,
                    "fill_price": fill_price,
                    "proceeds": shares * fill_price,
                    "ticker": ticker,
                    "subaccount": sub,
                }
            log.warning(f"auto_handle sell_holding failed: {r.get('error')}")
            return None

    except Exception as e:
        log.warning(f"_execute_auto_handle failed: {e}")
        return None
    return None


def edit_spread(account: str, spread_id: str, **fields) -> Dict:
    """Edit fields on an existing spread (correction, not state change).

    Editable: long_strike, short_strike, exp, net (price), is_credit,
    contracts, subaccount, open_date, note.

    Note: changing is_credit between credit/debit will recompute the
    credit/debit fields but does NOT auto-adjust linked cash events.
    Use undo on Settings if you need to fully redo.
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}
    spreads = get_spreads(account)
    target = next((s for s in spreads if isinstance(s, dict) and s.get("id") == spread_id), None)
    if not target:
        return {"ok": False, "error": "Spread not found"}
    old = dict(target)

    editable = {"long_strike", "short_strike", "exp", "net", "is_credit",
                "contracts", "subaccount", "tag", "open_date", "note"}

    for k, v in fields.items():
        if k not in editable or v is None or v == "":
            continue
        if k in ("long_strike", "short_strike", "net"):
            f = _to_float(v)
            if f is None:
                continue
            if k == "net":
                # Update credit/debit based on is_credit value (current or just-edited)
                is_credit_now = fields.get("is_credit")
                if is_credit_now is None:
                    is_credit_now = "true" if target.get("credit") else "false"
                if str(is_credit_now).lower() == "true":
                    target["credit"] = round(f, 4)
                    target["debit"] = None
                else:
                    target["debit"] = round(f, 4)
                    target["credit"] = None
            else:
                target[k] = round(f, 4)
        elif k == "is_credit":
            # Already handled inside "net" block — but if net not in fields, flip without changing value
            if "net" not in fields:
                if str(v).lower() == "true":
                    if target.get("debit"):
                        target["credit"] = target["debit"]
                        target["debit"] = None
                else:
                    if target.get("credit"):
                        target["debit"] = target["credit"]
                        target["credit"] = None
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
    if not _save(_key_spreads(account), spreads):
        return {"ok": False, "error": "Save failed"}
    _audit(account, "edit_spread", spread_id, old, target)
    return {"ok": True, "spread": target}


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

    # Phase 4.5 — campaign hook
    try:
        from . import campaigns as _campaigns
        _campaigns.hook_option_rolled(account, target, new_opt,
                                       net_credit=round(net_credit, 2),
                                       roll_date=roll_iso)
    except Exception as e:
        log.warning(f"campaign hook (roll_option) failed: {e}")

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
                 note: str = None, contracts_to_close: int = None) -> Dict:
    """Close a spread. status: closed | expired.

    contracts_to_close: if provided and < total contracts, performs a
    PARTIAL close. Splits position into closed-N + open-(M-N).
    """
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

    total_contracts = int(target.get("contracts") or 1)
    n_to_close = _to_int(contracts_to_close)
    if n_to_close is None or n_to_close <= 0 or n_to_close >= total_contracts:
        n_to_close = total_contracts
        is_partial = False
    else:
        is_partial = True

    old = dict(target)
    cv = _to_float(close_value, default=0.0) or 0.0
    close_iso = _validate_date(close_date) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if is_partial:
        # Split: create closed-portion record, reduce original
        closed_portion = dict(target)
        closed_portion["id"] = _gen_id("spr")
        closed_portion["contracts"] = n_to_close
        closed_portion["status"] = status
        closed_portion["close_value"] = round(cv, 4)
        closed_portion["close_date"] = close_iso
        closed_portion["close_note"] = (note or "").strip()
        closed_portion["closed_at"] = _now_iso()
        closed_portion["partial_of"] = spread_id
        spreads.append(closed_portion)

        target["contracts"] = total_contracts - n_to_close
        target["last_updated"] = _now_iso()

        if not _save(_key_spreads(account), spreads):
            return {"ok": False, "error": "Save failed"}

        sub = target.get("subaccount") or DEFAULT_SUBACCOUNT
        is_credit = bool(target.get("credit"))
        if status == "closed" and cv > 0:
            if is_credit:
                cash_amount = -(cv * n_to_close * 100)
            else:
                cash_amount = cv * n_to_close * 100
            add_cash_event(
                account, "spread_close", cash_amount,
                subaccount=sub, date=close_iso,
                note=f"Close {n_to_close}/{total_contracts} {target.get('type')} {target.get('ticker')}",
                ref_id=closed_portion["id"],
            )

        _audit(account, "partial_close_spread", spread_id, old, {
            "original": old,
            "remaining_contracts": target["contracts"],
            "closed_portion_id": closed_portion["id"],
            "closed_contracts": n_to_close,
            "status": status,
            "close_value": cv,
        })
        return {
            "ok": True,
            "partial": True,
            "remaining": target,
            "closed_portion": closed_portion,
        }

    # Full close
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

    # Phase 4.5 — auto-mirror to partner cash ledger so their command-center
    # view reflects the transaction without manual double-entry.
    if recipient in PARTNER_LEDGER_ACCOUNTS:
        try:
            if amt > 0:
                # Source paying OUT to partner (e.g. Brad pays Kyleigh).
                # On her ledger: a withdrawal (negative) — she's pulling
                # capital + gains out.
                add_cash_event(
                    recipient, "withdrawal", -amt,
                    subaccount=sub, date=date_iso,
                    note=f"Distribution from {account.title()}: {note or ''}".strip(),
                    ref_id=transfer_id,
                )
            else:
                # Source receiving from partner (e.g. Kyleigh sending money in,
                # logged as a NEGATIVE transfer). On her ledger: a deposit (positive)
                # — she's contributing capital.
                add_cash_event(
                    recipient, "deposit", -amt,  # -amt is positive since amt < 0
                    subaccount=sub, date=date_iso,
                    note=f"Contribution to {account.title()}: {note or ''}".strip(),
                    ref_id=transfer_id,
                )
        except Exception as e:
            log.warning(f"partner ledger mirror failed (non-fatal): {e}")

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

# ═════════════════════════════════════════════════════════
# PHASE 4.5 — RETRO-FIX WHEEL ASSIGNMENTS
# Scans audit log for legacy `option_assigned` entries (without auto-handled
# shares) and offers to backfill the share lots.
# ═════════════════════════════════════════════════════════

def retrofix_scan_assignments() -> Dict:
    """Scan all accounts for legacy `option_assigned` audit entries that
    were never followed by a matching share lot.

    Returns a dict with per-account candidate lists. Each candidate has the
    info needed to create the missing share lot.
    """
    try:
        from . import durability
        # Pull a generous slice — assignments aren't super frequent
        raw = durability.list_audit_entries(limit=500)
    except Exception:
        raw = []

    # Build per-account list of subsequent share-buy events keyed by ticker
    # (so we can verify whether a matching lot was added shortly after the
    # assignment).
    per_account_assignments = {}  # account → list of candidate dicts
    per_account_share_buys = {}   # account → list of (ticker, date) tuples

    for entry in raw:
        try:
            new_v = json.loads(entry["new_value"]) if entry.get("new_value") else None
        except Exception:
            new_v = None

        op = entry.get("op", "")
        account = entry.get("account", "")
        target = entry.get("target", "")
        ts = entry.get("timestamp", "")

        if op == "option_assigned":
            # Legacy: no auto-handled shares
            if not isinstance(new_v, dict):
                continue
            opt_type = new_v.get("type")
            if opt_type != "CSP":
                continue  # CC assignments need different handling
            direction = new_v.get("direction", "sell")
            if direction != "sell":
                continue
            ticker = new_v.get("ticker")
            strike = new_v.get("strike")
            contracts = new_v.get("contracts") or 1
            sub = new_v.get("subaccount") or DEFAULT_SUBACCOUNT
            close_date = new_v.get("close_date") or ""
            if not ticker or not strike:
                continue
            cand = {
                "audit_timestamp": ts,
                "audit_target": target,  # opt_id
                "account": account,
                "ticker": ticker,
                "strike": float(strike),
                "contracts": int(contracts),
                "subaccount": sub,
                "close_date": close_date,
                "shares_to_add": 100 * int(contracts),
                "cash_impact": 100 * int(contracts) * float(strike),
                "opt_id": target,
            }
            per_account_assignments.setdefault(account, []).append(cand)

        elif op in ("add_holding", "add_to_holding"):
            if not isinstance(new_v, dict):
                continue
            ticker = target
            date = (new_v.get("first_added")
                     or new_v.get("last_updated", "")[:10]
                     or ts[:10])
            per_account_share_buys.setdefault(account, []).append((ticker, date))

    # Filter out candidates that DO appear to have a matching share lot within
    # 7 days of the assignment date.
    from datetime import datetime as _dt, timedelta as _td

    filtered = {}
    for account, cands in per_account_assignments.items():
        share_buys = per_account_share_buys.get(account, [])
        keep = []
        for c in cands:
            # Try to parse close_date
            cd = c.get("close_date") or c.get("audit_timestamp", "")[:10]
            try:
                ass_date = _dt.strptime(cd[:10], "%Y-%m-%d")
            except Exception:
                # If we can't parse, keep the candidate (better safe than sorry)
                keep.append(c)
                continue

            # Look for a share buy of same ticker within 7 days after
            matched = False
            for (t, d) in share_buys:
                if t != c["ticker"]:
                    continue
                try:
                    bd = _dt.strptime(d[:10], "%Y-%m-%d")
                except Exception:
                    continue
                delta = (bd - ass_date).days
                if 0 <= delta <= 7:
                    matched = True
                    break
            if not matched:
                keep.append(c)
        filtered[account] = keep

    total = sum(len(v) for v in filtered.values())
    return {
        "candidates_by_account": filtered,
        "total_candidates": total,
    }


def retrofix_apply(selections: List[Dict]) -> Dict:
    """Apply selected retrofix candidates.

    selections: list of dicts each containing:
      account, ticker, strike, contracts, subaccount, close_date, opt_id

    For each selection: creates the missing share lot via add_holding (which
    auto-debits cash), wires up the campaign, and audits as `retrofix_assignment`
    so it can be individually undone.
    """
    applied = 0
    skipped = 0
    errors = []

    for sel in selections:
        try:
            account = sel.get("account")
            ticker = sel.get("ticker")
            strike = float(sel.get("strike") or 0)
            contracts = int(sel.get("contracts") or 1)
            sub = sel.get("subaccount") or DEFAULT_SUBACCOUNT
            close_date = sel.get("close_date") or ""
            opt_id = sel.get("opt_id") or ""

            if not _validate_account(account) or not ticker or strike <= 0:
                skipped += 1
                errors.append(f"Skipped invalid: {sel}")
                continue

            shares = 100 * contracts

            # Add the share lot (this auto-creates a cash debit via add_holding)
            r = add_holding(
                account, ticker,
                shares=shares, cost_basis=strike,
                subaccount=sub, tag="wheel",
                date=close_date or None,
                _from_assignment=True,  # suppress duplicate campaign event
            )
            if not r.get("ok"):
                skipped += 1
                errors.append(f"add_holding failed for {ticker}: {r.get('error')}")
                continue

            # Wire up the campaign manually (since _from_assignment suppressed the auto path).
            # Mimic what hook_option_closed would have done:
            try:
                from . import campaigns as _campaigns
                holding = _campaigns.get_active_holding_campaign(account, ticker, sub)
                csp_camp = _campaigns.find_csp_only_campaign(account, ticker, sub, opt_id)
                event = {
                    "type": "csp_assigned",
                    "id": opt_id,
                    "contracts": contracts,
                    "strike": strike,
                    "shares_acquired": shares,
                    "auto_handled": True,
                    "retrofixed": True,
                    "date": close_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                }
                if csp_camp and holding:
                    _campaigns.attach_event(account, csp_camp["id"], event)
                    _campaigns.merge_csp_into_holding(account, csp_camp["id"], holding["id"])
                elif csp_camp and not holding:
                    _campaigns.attach_event(account, csp_camp["id"], event)
                    _campaigns.transition_to_holding(account, csp_camp["id"])
                elif holding and not csp_camp:
                    _campaigns.attach_event(account, holding["id"], event)
                else:
                    new_camp = _campaigns.start_campaign(account, ticker, sub, event)
                    if new_camp:
                        _campaigns.transition_to_holding(account, new_camp["id"])
            except Exception as e:
                log.warning(f"retrofix campaign wiring failed for {ticker}: {e}")

            # Audit as a distinct, undoable operation
            _audit(
                account, "retrofix_assignment", opt_id or ticker, None,
                {
                    "ticker": ticker,
                    "strike": strike,
                    "contracts": contracts,
                    "subaccount": sub,
                    "close_date": close_date,
                    "shares_added": shares,
                    "cash_impact": -shares * strike,
                    "opt_id": opt_id,
                },
            )
            applied += 1

        except Exception as e:
            skipped += 1
            errors.append(f"Exception: {e}")

    return {
        "ok": True,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
    }


# ═════════════════════════════════════════════════════════
# PHASE 4.5 — BACKFILL CAMPAIGN HISTORY FROM AUDIT LOG
# Walks the audit log and reconstructs the full event history for each
# wheel campaign (csp_open, csp_rolled, csp_assigned, cc_open, cc_closed,
# cc_called_away, etc.). Useful after retro-fix to populate the campaign
# cards with accurate premium totals and timelines.
# ═════════════════════════════════════════════════════════

def _walk_roll_chain(opt_id: str, roll_by_new_id: Dict, accumulated: set,
                      roll_by_old_id: Dict = None):
    """Walk through roll chain in BOTH directions, accumulating opt_ids.

    - Backward: given opt_id (new leg), find what it was rolled FROM
    - Forward:  given opt_id (old leg), find what it was rolled TO

    This ensures the chain is complete regardless of which opt_id we start from.
    """
    if not opt_id or opt_id in accumulated:
        return
    accumulated.add(opt_id)

    # Backward: this opt_id was created by a roll → find old_id
    roll_entry = roll_by_new_id.get(opt_id)
    if roll_entry:
        rd = roll_entry.get("_parsed_new") or {}
        if isinstance(rd, dict):
            closed_data = rd.get("closed") or {}
            if isinstance(closed_data, dict):
                old_id = closed_data.get("id")
                if old_id:
                    _walk_roll_chain(old_id, roll_by_new_id, accumulated, roll_by_old_id)

    # Forward: this opt_id was rolled TO something → find new_id
    if roll_by_old_id is not None:
        forward_entry = roll_by_old_id.get(opt_id)
        if forward_entry:
            rd = forward_entry.get("_parsed_new") or {}
            if isinstance(rd, dict):
                new_data = rd.get("new") or {}
                if isinstance(new_data, dict):
                    new_id = new_data.get("id")
                    if new_id:
                        _walk_roll_chain(new_id, roll_by_new_id, accumulated, roll_by_old_id)


def backfill_campaign_history(account: Optional[str] = None) -> Dict:
    """Reconstruct full event history for all campaigns from the audit log.

    For each campaign:
      1. Find every opt_id referenced in its events.
      2. Walk backwards through any roll_option audits to find predecessor opt_ids.
      3. Rebuild the events list from the audit log for the entire chain:
         - add_option   → csp_open / cc_open
         - roll_option  → csp_rolled / cc_rolled (with computed net_credit)
         - option_closed / option_expired / option_assigned[_with_shares]
                        → csp_closed / csp_expired / csp_assigned / etc.
      4. Preserve any non-option events (manual_share_add, etc.).
      5. Recompute the rollup.

    Idempotent — running multiple times produces the same result.
    Audited as `backfill_campaign_history` (not undoable; rebuilds derived data).
    """
    from . import campaigns as _campaigns
    from . import durability

    accounts = [account] if account else list(ALL_UNDERLYING_ACCOUNTS)

    # Pull a generous slice of the audit log
    try:
        raw = durability.list_audit_entries(limit=2000)
    except Exception as e:
        log.warning(f"audit log unavailable for backfill: {e}")
        return {"ok": False, "error": "audit log unavailable"}

    # Index audits by op + opt_id for fast lookup
    add_option_by_target: Dict[str, Dict] = {}
    roll_by_new_id: Dict[str, Dict] = {}
    roll_by_old_id: Dict[str, Dict] = {}  # Phase 4.5+ — forward index for chain walking
    close_by_target: Dict[str, Dict] = {}

    for entry in raw:
        op = entry.get("op", "") or ""
        try:
            new_v = json.loads(entry["new_value"]) if entry.get("new_value") else None
        except Exception:
            new_v = None
        # Cache parsed payload on the entry for re-use
        entry["_parsed_new"] = new_v

        if op == "add_option":
            tgt = entry.get("target", "")
            if tgt:
                # Newest entry wins (later edit_option wouldn't, but add_option is one-shot)
                if tgt not in add_option_by_target:
                    add_option_by_target[tgt] = entry
        elif op == "roll_option" and isinstance(new_v, dict):
            new_data = new_v.get("new")
            closed_data = new_v.get("closed")
            if isinstance(new_data, dict) and new_data.get("id"):
                roll_by_new_id[new_data["id"]] = entry
            if isinstance(closed_data, dict) and closed_data.get("id"):
                roll_by_old_id[closed_data["id"]] = entry
        elif op in ("option_closed", "option_expired", "option_assigned",
                    "option_assigned_with_shares", "cc_called_away_with_shares"):
            tgt = entry.get("target", "")
            if tgt and tgt not in close_by_target:
                close_by_target[tgt] = entry

    # Phase 4.5+ — Live option index. When the user has edited a closed
    # option (premium/contracts/close_premium correction), the audit log still
    # has the original add_option values. Prefer current option records so
    # backfill respects user edits.
    live_options_by_id: Dict[str, Dict[str, Dict]] = {}  # account → opt_id → opt
    for acc in accounts:
        try:
            live_options_by_id[acc] = {
                o["id"]: o
                for o in get_options(acc)
                if isinstance(o, dict) and o.get("id")
            }
        except Exception:
            live_options_by_id[acc] = {}

    summary = {
        "ok": True,
        "accounts_modified": 0,
        "campaigns_modified": 0,
        "events_added": 0,
        "premium_recovered": 0.0,
        "details": [],
    }

    for acc in accounts:
        if not _validate_account(acc):
            continue

        camps = _campaigns._load_campaigns(acc)

        # ── Phase 4.5+ — Orphan discovery ──
        # Scan the audit log for option events on this account whose opt_ids
        # don't appear in any existing campaign. Create campaigns for each
        # orphan group (by ticker+subaccount). This recovers wheels that were
        # missed when the campaign system was added mid-flight, OR when CCs
        # were opened against manually-added shares (older code paths).
        existing_opt_ids: set = set()
        for camp in camps:
            for ev in camp.get("events", []) or []:
                if ev.get("id"):
                    existing_opt_ids.add(ev["id"])
                if ev.get("old_id"):
                    existing_opt_ids.add(ev["old_id"])

        orphan_groups: Dict[tuple, List[Dict]] = {}  # (ticker, sub) → [audit entries]
        for entry in raw:
            if entry.get("account") != acc:
                continue
            op = entry.get("op", "") or ""
            if op != "add_option":
                continue
            tgt = entry.get("target", "")
            if not tgt or tgt in existing_opt_ids:
                continue
            od = entry.get("_parsed_new") or {}
            if not isinstance(od, dict):
                continue
            opt_type = od.get("type", "")
            direction = od.get("direction", "sell")
            if opt_type not in ("CSP", "CC") or direction != "sell":
                continue
            ticker = od.get("ticker") or ""
            sub = od.get("subaccount") or ""
            if not ticker:
                continue
            orphan_groups.setdefault((ticker, sub), []).append(entry)

        for (ticker, sub), entries in orphan_groups.items():
            # Sort by open_date / timestamp so the earliest open is the seed
            entries.sort(key=lambda e: (
                (e.get("_parsed_new") or {}).get("open_date") or e.get("timestamp", "")
            ))
            seed_entry = entries[0]
            seed_data = seed_entry.get("_parsed_new") or {}
            seed_opt_type = seed_data.get("type", "")

            # Build initial event from seed
            def _build_open_event(entry, data):
                ot = data.get("type", "")
                ev_type = "csp_open" if ot == "CSP" else "cc_open" if ot == "CC" else None
                if not ev_type:
                    return None
                return {
                    "type": ev_type,
                    "id": entry.get("target"),
                    "premium": data.get("premium"),
                    "contracts": data.get("contracts") or 1,
                    "strike": data.get("strike"),
                    "exp": data.get("exp"),
                    "open_date": data.get("open_date") or entry.get("timestamp", "")[:10],
                    "date": data.get("open_date") or entry.get("timestamp", "")[:10],
                    "backfilled": True,
                }

            seed_event = _build_open_event(seed_entry, seed_data)
            if not seed_event:
                continue

            new_camp = _campaigns.start_campaign(acc, ticker, sub, seed_event)
            if not new_camp:
                continue

            # If seed was a CC, the campaign starts in holding phase (CCs require shares).
            if seed_opt_type == "CC":
                _campaigns.transition_to_holding(acc, new_camp["id"])

            # Add the rest of the orphan options as events on the same campaign
            # (these are independent CSPs/CCs on the same ticker+sub that weren't
            # rolled from the seed; the existing roll-chain walk below handles
            # their close/expire events).
            for entry in entries[1:]:
                data = entry.get("_parsed_new") or {}
                ev = _build_open_event(entry, data)
                if ev:
                    _campaigns.attach_event(acc, new_camp["id"], ev)

            any_modified = True
            summary["campaigns_modified"] += 1
            seed_premium = float(seed_event.get("premium") or 0) * int(seed_event.get("contracts") or 1) * 100
            extra_premium = sum(
                float((e.get("_parsed_new") or {}).get("premium") or 0)
                * int((e.get("_parsed_new") or {}).get("contracts") or 1) * 100
                for e in entries[1:]
            )
            summary["details"].append({
                "account": acc,
                "ticker": ticker,
                "subaccount": sub,
                "events_before": 0,
                "events_after": len(entries),
                "premium_before": 0.0,
                "premium_after": round(seed_premium + extra_premium, 2),
                "discovery": True,
            })

        # Reload campaigns after discovery so subsequent walks include the new ones
        if orphan_groups:
            camps = _campaigns._load_campaigns(acc)

        any_modified = bool(orphan_groups)
        if not camps:
            if any_modified:
                # Edge case: orphans were created but immediately persisted by start_campaign
                summary["accounts_modified"] += 1
            continue

        for camp in camps:
            old_events = camp.get("events", []) or []
            old_premium = (camp.get("rollup") or {}).get("total_premium", 0)

            # Collect opt_ids from current events
            opt_ids_in_events: set = set()
            non_option_events = []
            for ev in old_events:
                # Phase 4.5+ — opt_id can be in id, old_id, or new_id (rolls store
                # both old/new). Treat the event as option-related if any are set.
                if ev.get("id"):
                    opt_ids_in_events.add(ev.get("id"))
                if ev.get("old_id"):
                    opt_ids_in_events.add(ev.get("old_id"))
                if ev.get("new_id"):
                    opt_ids_in_events.add(ev.get("new_id"))
                # Truly id-less events (manual_share_add, shares_seeded, etc.) get preserved
                if not ev.get("id") and not ev.get("old_id") and not ev.get("new_id"):
                    non_option_events.append(ev)

            if not opt_ids_in_events:
                continue  # No options to backfill for

            # Walk back through the roll chain for each
            chain_ids: set = set()
            for oid in opt_ids_in_events:
                _walk_roll_chain(oid, roll_by_new_id, chain_ids, roll_by_old_id)

            # Build the new events list
            new_events: List[Dict] = []

            for oid in chain_ids:
                # Live option (current state) — preferred source for premium/contracts
                live_opt = live_options_by_id.get(acc, {}).get(oid) or {}

                # 1. csp_open / cc_open from add_option audit
                add_entry = add_option_by_target.get(oid)
                if add_entry:
                    od = add_entry.get("_parsed_new") or {}
                    if isinstance(od, dict):
                        opt_type = (live_opt.get("type") or od.get("type", ""))
                        if opt_type == "CSP":
                            new_events.append({
                                "type": "csp_open",
                                "id": oid,
                                "strike": live_opt.get("strike") or od.get("strike"),
                                "exp": live_opt.get("exp") or od.get("exp"),
                                "contracts": live_opt.get("contracts") or od.get("contracts") or 1,
                                "premium": live_opt.get("premium") if live_opt else od.get("premium"),
                                "open_date": (live_opt.get("open_date") or od.get("open_date")
                                              or add_entry.get("timestamp", "")[:10]),
                                "date": (live_opt.get("open_date") or od.get("open_date")
                                         or add_entry.get("timestamp", "")[:10]),
                                "backfilled": True,
                            })
                        elif opt_type == "CC":
                            new_events.append({
                                "type": "cc_open",
                                "id": oid,
                                "strike": live_opt.get("strike") or od.get("strike"),
                                "exp": live_opt.get("exp") or od.get("exp"),
                                "contracts": live_opt.get("contracts") or od.get("contracts") or 1,
                                "premium": live_opt.get("premium") if live_opt else od.get("premium"),
                                "open_date": (live_opt.get("open_date") or od.get("open_date")
                                              or add_entry.get("timestamp", "")[:10]),
                                "date": (live_opt.get("open_date") or od.get("open_date")
                                         or add_entry.get("timestamp", "")[:10]),
                                "backfilled": True,
                            })

                # 2. csp_rolled / cc_rolled from roll_option audit (this opt_id was created by a roll)
                roll_entry = roll_by_new_id.get(oid)
                if roll_entry:
                    rd = roll_entry.get("_parsed_new") or {}
                    if isinstance(rd, dict):
                        closed_data = rd.get("closed") or {}
                        new_data = rd.get("new") or {}
                        if isinstance(closed_data, dict) and isinstance(new_data, dict):
                            opt_type = new_data.get("type") or closed_data.get("type") or "CSP"
                            old_id = closed_data.get("id")
                            close_p = float(closed_data.get("close_premium") or 0)
                            new_p = float(new_data.get("premium") or 0)
                            contracts = int(new_data.get("contracts") or closed_data.get("contracts") or 1)
                            net_credit = (new_p - close_p) * contracts * 100
                            roll_date = (new_data.get("open_date")
                                         or roll_entry.get("timestamp", "")[:10])
                            ev_kind = "csp_rolled" if opt_type == "CSP" else \
                                      "cc_rolled" if opt_type == "CC" else None
                            if ev_kind:
                                new_events.append({
                                    "type": ev_kind,
                                    "id": oid,
                                    "old_id": old_id,
                                    "new_strike": new_data.get("strike"),
                                    "new_exp": new_data.get("exp"),
                                    "new_premium": new_p,
                                    "close_premium": close_p,
                                    "contracts": contracts,
                                    "net_credit": net_credit,
                                    "roll_date": roll_date,
                                    "date": roll_date,
                                    "backfilled": True,
                                })

                # 3. close event from option_closed / expired / assigned audit
                close_entry = close_by_target.get(oid)
                if close_entry:
                    cd = close_entry.get("_parsed_new") or {}
                    co_op = close_entry.get("op", "")
                    if isinstance(cd, dict):
                        opt_type = cd.get("type", "CSP")
                        contracts = int(cd.get("contracts") or 1)
                        cdate = cd.get("close_date") or close_entry.get("timestamp", "")[:10]

                        if co_op in ("option_assigned", "option_assigned_with_shares"):
                            if opt_type == "CSP":
                                new_events.append({
                                    "type": "csp_assigned",
                                    "id": oid,
                                    "contracts": contracts,
                                    "strike": cd.get("strike"),
                                    "shares_acquired": 100 * contracts,
                                    "auto_handled": (co_op == "option_assigned_with_shares"),
                                    "date": cdate,
                                    "backfilled": True,
                                })
                            elif opt_type == "CC":
                                # Legacy CC assignment without auto-handle
                                new_events.append({
                                    "type": "cc_called_away",
                                    "id": oid,
                                    "contracts": contracts,
                                    "strike": cd.get("strike"),
                                    "date": cdate,
                                    "backfilled": True,
                                })
                        elif co_op == "cc_called_away_with_shares" and opt_type == "CC":
                            new_events.append({
                                "type": "cc_called_away",
                                "id": oid,
                                "contracts": contracts,
                                "strike": cd.get("strike"),
                                "shares_sold": 100 * contracts,
                                "auto_handled": True,
                                "date": cdate,
                                "backfilled": True,
                            })
                        elif co_op == "option_closed":
                            ev_kind = "csp_closed" if opt_type == "CSP" else \
                                      "cc_closed" if opt_type == "CC" else None
                            if ev_kind:
                                new_events.append({
                                    "type": ev_kind,
                                    "id": oid,
                                    "contracts": contracts,
                                    "close_premium": cd.get("close_premium") or 0,
                                    "date": cdate,
                                    "backfilled": True,
                                })
                        elif co_op == "option_expired":
                            ev_kind = "csp_expired" if opt_type == "CSP" else \
                                      "cc_expired" if opt_type == "CC" else None
                            if ev_kind:
                                new_events.append({
                                    "type": ev_kind,
                                    "id": oid,
                                    "contracts": contracts,
                                    "close_premium": 0,
                                    "date": cdate,
                                    "backfilled": True,
                                })

            # Combine with non-option events and sort chronologically
            def _ev_sort_key(e):
                d = e.get("date") or e.get("open_date") or e.get("roll_date") or "0000-00-00"
                # Tiebreak: open events before close events on same date
                priority = {"csp_open": 0, "cc_open": 0, "csp_rolled": 1, "cc_rolled": 1,
                            "csp_closed": 2, "cc_closed": 2, "csp_expired": 2, "cc_expired": 2,
                            "csp_assigned": 3, "cc_called_away": 3,
                            "manual_share_add": 0, "share_sold": 4}
                return (d, priority.get(e.get("type", ""), 5))

            all_events = new_events + non_option_events
            all_events.sort(key=_ev_sort_key)

            # Update campaign
            camp["events"] = all_events
            camp["rollup"] = _campaigns._compute_rollup(camp)

            new_count = len(all_events)
            old_count = len(old_events)
            new_premium = camp["rollup"].get("total_premium", 0)

            # Phase 4.5+ — also detect changes in individual event premiums/contracts
            # (not just count/total). This catches the case where an option was
            # edited (e.g., 7.65 → 3.82) and the rebuilt events differ from the
            # stored ones even though total premium happens to be the same.
            events_differ = False
            if old_count == new_count:
                for old_ev, new_ev in zip(old_events, all_events):
                    for k in ("premium", "contracts", "close_premium", "strike", "exp"):
                        if old_ev.get(k) != new_ev.get(k):
                            events_differ = True
                            break
                    if events_differ:
                        break

            if new_count != old_count or new_premium != old_premium or events_differ:
                any_modified = True
                summary["campaigns_modified"] += 1
                summary["events_added"] += max(0, new_count - old_count)
                summary["premium_recovered"] += max(0, new_premium - old_premium)
                summary["details"].append({
                    "account": acc,
                    "ticker": camp.get("ticker"),
                    "subaccount": camp.get("subaccount"),
                    "events_before": old_count,
                    "events_after": new_count,
                    "premium_before": round(old_premium, 2),
                    "premium_after": round(new_premium, 2),
                    "events_changed": events_differ and new_count == old_count,
                })

        if any_modified:
            _campaigns._save_campaigns(acc, camps)
            summary["accounts_modified"] += 1
            _audit(
                acc, "backfill_campaign_history", "all_campaigns", None,
                {"campaigns_modified": summary["campaigns_modified"]},
            )

    # Build per-account summary for clearer flash messages
    summary["per_account"] = {}
    for d in summary["details"]:
        acc = d.get("account") or "?"
        bucket = summary["per_account"].setdefault(acc, {
            "campaigns": 0, "events": 0, "premium": 0.0, "discovered": 0,
        })
        bucket["campaigns"] += 1
        bucket["events"] += max(0, d.get("events_after", 0) - d.get("events_before", 0))
        bucket["premium"] += max(0, d.get("premium_after", 0) - d.get("premium_before", 0))
        if d.get("discovery"):
            bucket["discovered"] += 1

    summary["premium_recovered"] = round(summary["premium_recovered"], 2)
    return summary


# ═════════════════════════════════════════════════════════
# PHASE 4.5 — REPAIR OPTION DATA
# Scans closed/expired/assigned/rolled options for missing fields
# (close_date, premium, contracts) and patches them from the audit log.
# Idempotent. Safe to re-run.
# ═════════════════════════════════════════════════════════

def repair_option_data(account: Optional[str] = None) -> Dict:
    """Walk every option and patch missing fields from the audit log.

    For each option with status closed/expired/assigned/rolled, ensure:
      - close_date is set (fall back to: close_at, exp, audit timestamp)
      - premium is set (fall back to: add_option audit's new_value.premium)
      - contracts is set (fall back to: add_option audit, default 1)

    Idempotent — running multiple times yields the same result.
    Audited as `repair_option_data` per fix.
    """
    from . import durability

    accounts = [account] if account else list(ALL_UNDERLYING_ACCOUNTS)

    try:
        raw = durability.list_audit_entries(limit=2000)
    except Exception as e:
        log.warning(f"audit log unavailable: {e}")
        return {"ok": False, "error": "audit log unavailable"}

    # Index add_option audits by opt_id (target)
    add_option_by_target: Dict[str, Dict] = {}
    close_by_target: Dict[str, Dict] = {}
    for entry in raw:
        op = entry.get("op", "") or ""
        try:
            new_v = json.loads(entry["new_value"]) if entry.get("new_value") else None
        except Exception:
            new_v = None
        entry["_parsed_new"] = new_v

        if op == "add_option":
            tgt = entry.get("target", "")
            if tgt and tgt not in add_option_by_target:
                add_option_by_target[tgt] = entry
        elif op in ("option_closed", "option_expired", "option_assigned",
                    "option_assigned_with_shares", "cc_called_away_with_shares"):
            tgt = entry.get("target", "")
            if tgt and tgt not in close_by_target:
                close_by_target[tgt] = entry

    summary = {
        "ok": True,
        "options_repaired": 0,
        "fields_filled": 0,
        "details": [],
    }

    for acc in accounts:
        if not _validate_account(acc):
            continue

        options = get_options(acc)
        if not options:
            continue

        any_changed = False

        for opt in options:
            if not isinstance(opt, dict):
                continue
            status = opt.get("status", "open")
            # Only repair closed-state options
            if status not in ("closed", "expired", "assigned", "rolled"):
                continue

            opt_id = opt.get("id", "")
            patches = []

            # 1. close_date
            # Priority: exp (assignment/expiry usually = close date) →
            #           audit payload's close_date →
            #           closed_at field (truncated to date) →
            #           audit timestamp (truncated to date)
            if not opt.get("close_date"):
                fallback = opt.get("exp") or ""
                if not fallback:
                    ce = close_by_target.get(opt_id)
                    if ce:
                        cd = ce.get("_parsed_new")
                        if isinstance(cd, dict) and cd.get("close_date"):
                            fallback = cd["close_date"]
                if not fallback:
                    fallback = (opt.get("closed_at") or "")[:10]
                if not fallback:
                    ce = close_by_target.get(opt_id)
                    if ce:
                        fallback = ce.get("timestamp", "")[:10]
                if fallback:
                    opt["close_date"] = fallback
                    patches.append(f"close_date={fallback}")

            # 2. premium
            if opt.get("premium") in (None, 0, 0.0, ""):
                ae = add_option_by_target.get(opt_id)
                if ae:
                    od = ae.get("_parsed_new") or {}
                    if isinstance(od, dict) and od.get("premium") not in (None, ""):
                        try:
                            opt["premium"] = float(od["premium"])
                            patches.append(f"premium={opt['premium']}")
                        except Exception:
                            pass

            # 3. contracts
            if opt.get("contracts") in (None, 0, ""):
                ae = add_option_by_target.get(opt_id)
                if ae:
                    od = ae.get("_parsed_new") or {}
                    if isinstance(od, dict) and od.get("contracts") not in (None, ""):
                        try:
                            opt["contracts"] = int(od["contracts"])
                            patches.append(f"contracts={opt['contracts']}")
                        except Exception:
                            pass

            # 4. close_premium (default 0 for assigned/expired, leave for closed)
            if opt.get("close_premium") is None:
                if status in ("expired", "assigned"):
                    opt["close_premium"] = 0
                    patches.append("close_premium=0")

            # 5. direction (default sell for CSP/CC)
            if not opt.get("direction"):
                opt_type = opt.get("type", "")
                if opt_type in ("CSP", "CC"):
                    opt["direction"] = "sell"
                    patches.append("direction=sell")
                elif opt_type in ("LONG_PUT", "LONG_CALL"):
                    opt["direction"] = "buy"
                    patches.append("direction=buy")

            if patches:
                any_changed = True
                summary["options_repaired"] += 1
                summary["fields_filled"] += len(patches)
                summary["details"].append({
                    "account": acc,
                    "opt_id": opt_id,
                    "ticker": opt.get("ticker"),
                    "status": status,
                    "patches": patches,
                })
                _audit(acc, "repair_option_data", opt_id, None,
                       {"patches": patches, "ticker": opt.get("ticker")})

        if any_changed:
            _save(_key_options(acc), options)

    return summary


# ═════════════════════════════════════════════════════════
# PHASE 4.5 — EDIT CLOSED POSITION META
# Allows editing sub-account / note on closed options, spreads, and
# sold share lots without touching cash math. Pure metadata changes.
# ═════════════════════════════════════════════════════════

def edit_closed_option_meta(account: str, opt_id: str,
                              subaccount: Optional[str] = None,
                              note: Optional[str] = None,
                              premium: Optional[float] = None,
                              contracts: Optional[int] = None,
                              close_premium: Optional[float] = None) -> Dict:
    """Update fields on a closed option.

    Phase 4.5+: now also supports correcting the open premium, contracts, and
    close premium. When those change, the linked cash events are adjusted so
    cash totals stay correct.
    """
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}

    options = get_options(account)
    opt = next((o for o in options if isinstance(o, dict) and o.get("id") == opt_id), None)
    if not opt:
        return {"ok": False, "error": "Option not found"}
    if opt.get("status") == "open":
        return {"ok": False, "error": "Use the regular edit form for open options"}

    old = {
        "subaccount": opt.get("subaccount"),
        "note": opt.get("note"),
        "premium": opt.get("premium"),
        "contracts": opt.get("contracts"),
        "close_premium": opt.get("close_premium"),
    }
    if subaccount is not None:
        opt["subaccount"] = subaccount.strip() or DEFAULT_SUBACCOUNT
    if note is not None:
        opt["note"] = note.strip()

    # Phase 4.5 — premium / contracts / close_premium edits with cash propagation
    new_premium = _to_float(premium) if premium is not None else None
    new_contracts = _to_int(contracts) if contracts is not None else None
    new_close_premium = _to_float(close_premium) if close_premium is not None else None

    if new_premium is not None and new_premium >= 0:
        opt["premium"] = round(new_premium, 4)
    if new_contracts is not None and new_contracts > 0:
        opt["contracts"] = new_contracts
    if new_close_premium is not None and new_close_premium >= 0:
        opt["close_premium"] = round(new_close_premium, 4)

    if not _save(_key_options(account), options):
        return {"ok": False, "error": "Save failed"}

    # Now reconcile cash events linked to this option
    ledger = get_cash_ledger(account)
    cash_changed = False
    direction = opt.get("direction", "sell")
    final_contracts = int(opt.get("contracts") or 1)
    final_premium = float(opt.get("premium") or 0)
    final_close = float(opt.get("close_premium") or 0)

    # Sell-to-open credits cash; buy-to-open debits.
    sign_open = 1.0 if direction == "sell" else -1.0
    sign_close = -1.0 if direction == "sell" else 1.0  # closing sell = pay back, closing buy = receive

    target_open_amount = round(sign_open * final_premium * final_contracts * 100, 2)
    target_close_amount = round(sign_close * final_close * final_contracts * 100, 2)

    for ev in ledger:
        if not isinstance(ev, dict) or ev.get("ref_id") != opt_id:
            continue
        ev_type = ev.get("type", "")
        if ev_type == "option_open":
            if subaccount is not None and ev.get("subaccount") != opt["subaccount"]:
                ev["subaccount"] = opt["subaccount"]
                cash_changed = True
            if abs(float(ev.get("amount") or 0) - target_open_amount) > 0.005:
                ev["amount"] = target_open_amount
                ev["note"] = (ev.get("note") or "") + " [edited]"
                cash_changed = True
        elif ev_type == "option_close":
            if subaccount is not None and ev.get("subaccount") != opt["subaccount"]:
                ev["subaccount"] = opt["subaccount"]
                cash_changed = True
            if abs(float(ev.get("amount") or 0) - target_close_amount) > 0.005:
                ev["amount"] = target_close_amount
                ev["note"] = (ev.get("note") or "") + " [edited]"
                cash_changed = True

    if cash_changed:
        _save(_key_cash_ledger(account), ledger)

    # Phase 4.5+ — also update any campaign event that mirrors this option
    # so the Active Wheels display reflects the corrected values.
    try:
        from . import campaigns as _campaigns
        camps = _campaigns._load_campaigns(account)
        camp_changed = False
        for camp in camps:
            for ev in camp.get("events", []) or []:
                if ev.get("id") != opt_id and ev.get("old_id") != opt_id:
                    continue
                if new_premium is not None and "premium" in ev:
                    ev["premium"] = round(new_premium, 4)
                    camp_changed = True
                if new_contracts is not None and "contracts" in ev:
                    ev["contracts"] = new_contracts
                    camp_changed = True
                if new_close_premium is not None and "close_premium" in ev:
                    ev["close_premium"] = round(new_close_premium, 4)
                    camp_changed = True
            if camp_changed:
                camp["rollup"] = _campaigns._compute_rollup(camp)
        if camp_changed:
            _campaigns._save_campaigns(account, camps)
    except Exception as e:
        log.warning(f"campaign mirror update failed (non-fatal): {e}")

    new_meta = {
        "subaccount": opt.get("subaccount"),
        "note": opt.get("note"),
        "premium": opt.get("premium"),
        "contracts": opt.get("contracts"),
        "close_premium": opt.get("close_premium"),
    }
    _audit(account, "edit_closed_option_meta", opt_id, old, new_meta)
    return {"ok": True, "option": opt, "cash_adjusted": cash_changed}


def edit_closed_spread_meta(account: str, spr_id: str,
                              subaccount: Optional[str] = None,
                              note: Optional[str] = None) -> Dict:
    """Update sub-account or note on a closed spread (does not touch cash math)."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}

    spreads = get_spreads(account)
    spr = next((s for s in spreads if isinstance(s, dict) and s.get("id") == spr_id), None)
    if not spr:
        return {"ok": False, "error": "Spread not found"}
    if spr.get("status") == "open":
        return {"ok": False, "error": "Use the regular edit form for open spreads"}

    old = {"subaccount": spr.get("subaccount"), "note": spr.get("note")}
    if subaccount is not None:
        spr["subaccount"] = subaccount.strip() or DEFAULT_SUBACCOUNT
    if note is not None:
        spr["note"] = note.strip()

    if not _save(_key_spreads(account), spreads):
        return {"ok": False, "error": "Save failed"}

    # Update linked cash events' sub-account too
    if subaccount is not None:
        ledger = get_cash_ledger(account)
        changed = False
        for ev in ledger:
            if isinstance(ev, dict) and ev.get("ref_id") == spr_id:
                if ev.get("subaccount") != spr["subaccount"]:
                    ev["subaccount"] = spr["subaccount"]
                    changed = True
        if changed:
            _save(_key_cash_ledger(account), ledger)

    _audit(account, "edit_closed_spread_meta", spr_id, old, {
        "subaccount": spr.get("subaccount"),
        "note": spr.get("note"),
    })
    return {"ok": True, "spread": spr}


def edit_sold_lot_meta(account: str, lot_id: str,
                         subaccount: Optional[str] = None,
                         note: Optional[str] = None) -> Dict:
    """Update sub-account or note on a sold share lot (does not touch P&L)."""
    if not _validate_account(account):
        return {"ok": False, "error": "Invalid account"}

    lots = _load(_key_sold_lots(account), [])
    lot = next((l for l in lots if isinstance(l, dict) and l.get("id") == lot_id), None)
    if not lot:
        return {"ok": False, "error": "Sold lot not found"}

    old = {"subaccount": lot.get("subaccount"), "note": lot.get("note")}
    if subaccount is not None:
        lot["subaccount"] = subaccount.strip() or DEFAULT_SUBACCOUNT
    if note is not None:
        lot["note"] = note.strip()

    if not _save(_key_sold_lots(account), lots):
        return {"ok": False, "error": "Save failed"}

    _audit(account, "edit_sold_lot_meta", lot_id, old, {
        "subaccount": lot.get("subaccount"),
        "note": lot.get("note"),
    })
    return {"ok": True, "lot": lot}


# ═════════════════════════════════════════════════════════
# WIPE
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
        _key_sold_lots(account),
        _key_wheel_campaigns(account),
    ]

    cleared = []
    for key in keys_to_clear:
        empty_val = "[]" if any(s in key for s in ("ledger", "options", "spreads", "lumpsum", "sold_lots", "campaigns")) else "{}"
        if _store_set(key, empty_val):
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

def portfolio_page_data(active_underlying: str = None,
                          show_closed: bool = False,
                          since_date: str = None,
                          ticker_filter: str = None) -> Dict:
    """Aggregated data for the Portfolio entry page.

    Returns live counts and recent entries for each underlying account so
    the entry forms can show what's already there.

    show_closed: if True, also include closed_options/closed_spreads/sold_lots
    since_date: 'YYYY-MM-DD' to filter closed history (only those closed after)
    ticker_filter: substring match on ticker for closed history
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

    out = {
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
        "show_closed": show_closed,
        "since_date_filter": since_date or "",
        "ticker_filter": ticker_filter or "",
    }

    # Closed history — only fetch when toggle is on (saves a Redis read)
    if show_closed:
        out["closed_options"] = get_closed_options(acc, since_date=since_date, ticker_filter=ticker_filter)
        out["closed_spreads"] = get_closed_spreads(acc, since_date=since_date, ticker_filter=ticker_filter)
        out["sold_lots"] = get_sold_lots_filtered(acc, since_date=since_date, ticker_filter=ticker_filter)
    else:
        out["closed_options"] = []
        out["closed_spreads"] = []
        out["sold_lots"] = []

    # Phase 4.5 — wheel campaigns
    try:
        from . import campaigns as _campaigns
        out["active_campaigns"] = _campaigns.get_active_campaigns(acc)
        if show_closed:
            out["closed_campaigns"] = _campaigns.get_closed_campaigns(acc)
        else:
            out["closed_campaigns"] = []
    except Exception as e:
        log.warning(f"campaigns load failed (non-fatal): {e}")
        out["active_campaigns"] = []
        out["closed_campaigns"] = []

    return out


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
    # Phase 4.5 — auto-handled assignments (CSP buy & CC called away)
    "option_assigned_with_shares", "cc_called_away_with_shares",
    "partial_close_option",
    # Cash events
    "cash_deposit", "cash_withdrawal", "cash_share_buy", "cash_share_sell",
    "cash_option_open", "cash_option_close", "cash_spread_open", "cash_spread_close",
    "cash_transfer_out", "cash_roll_credit", "cash_roll_debit",
    # Sells / sub-account changes
    "sell_holding",
    "edit_holding", "edit_option", "edit_spread", "update_lumpsum",
    # Transfers
    "transfer_kyleigh", "transfer_clay",
    "roll_option",
    # Phase 4.5 — retro-fix
    "retrofix_assignment",
}


def _enrich_audit_target(entry: Dict) -> Dict:
    """Phase 4.5 — Build a human-readable target description from audit entry.

    Returns dict with:
      headline: short string (e.g. "LMND CSP $80")
      detail:   small muted line (e.g. "exp 2026-06-20 · ×2 · Volkman Wheel · opened 2026-04-15")
      kind:     one of: option, holding, cash, spread, lumpsum, transfer, system, other

    Falls back to the raw target if data is sparse.
    """
    op = entry.get("op", "") or ""
    target = entry.get("target", "") or ""
    nv = entry.get("new_value")
    ov = entry.get("old_value")

    # Use new_value when available, fall back to old_value
    payload = nv if isinstance(nv, dict) else (ov if isinstance(ov, dict) else None)

    # Helper to dollar-format
    def _money(v):
        try:
            f = float(v)
            return f"${int(f)}" if f == int(f) else f"${f:.2f}"
        except Exception:
            return f"${v}"

    # ─── Options ───
    if op in ("add_option", "edit_option", "option_closed", "option_expired",
              "option_assigned", "option_assigned_with_shares",
              "cc_called_away_with_shares",
              "partial_close_option"):
        # For partial_close_option, the original is in payload.original
        opt = payload
        if op == "partial_close_option" and isinstance(payload, dict):
            opt = payload.get("original") or payload

        if isinstance(opt, dict):
            ticker = opt.get("ticker", "?")
            opt_type = opt.get("type", "?")
            strike = opt.get("strike", "?")
            exp = opt.get("exp", "")
            contracts = opt.get("contracts") or 1
            sub = opt.get("subaccount", "")
            opened = opt.get("open_date", "")

            headline = f"{ticker} {opt_type} {_money(strike)}"
            details = []
            if exp:
                details.append(f"exp {exp}")
            try:
                ic = int(contracts)
                if ic > 0:
                    details.append(f"×{ic}")
            except Exception:
                pass
            if sub:
                details.append(sub)
            if opened:
                details.append(f"opened {opened}")
            # For closes, show what happened
            if op in ("option_closed", "option_assigned", "option_expired",
                      "option_assigned_with_shares", "cc_called_away_with_shares"):
                cp = opt.get("close_premium")
                cd = opt.get("close_date")
                if cp is not None:
                    details.append(f"closed @ ${cp}")
                if cd:
                    details.append(f"on {cd}")

            return {
                "headline": headline,
                "detail": " · ".join(details),
                "kind": "option",
            }

    # ─── Holdings ───
    if op in ("add_holding", "add_to_holding", "edit_holding",
              "delete_holding", "sell_holding"):
        ticker = target
        if isinstance(payload, dict):
            shares = payload.get("shares") or payload.get("sold_shares")
            cb = payload.get("cost_basis") or payload.get("sell_price")
            sub = payload.get("subaccount", "")
            details = []
            if shares is not None:
                try:
                    sf = float(shares)
                    details.append(f"{int(sf)} sh" if sf == int(sf) else f"{sf} sh")
                except Exception:
                    details.append(f"{shares} sh")
            if cb is not None:
                details.append(f"@ {_money(cb)}")
            if sub:
                details.append(sub)
            verb_map = {
                "add_holding": "Buy",
                "add_to_holding": "Buy more",
                "edit_holding": "Edit",
                "delete_holding": "Delete",
                "sell_holding": "Sell",
            }
            verb = verb_map.get(op, op)
            return {
                "headline": f"{verb} {ticker}",
                "detail": " · ".join(details),
                "kind": "holding",
            }

    # ─── Cash events ───
    if op.startswith("cash_"):
        if isinstance(payload, dict):
            event_type = payload.get("type", "")
            amount = payload.get("amount", 0)
            sub = payload.get("subaccount", "")
            note = payload.get("note", "")
            date = payload.get("date", "")
            try:
                amt = float(amount)
                amt_str = f"+{_money(abs(amt))}" if amt >= 0 else f"-{_money(abs(amt))}"
            except Exception:
                amt_str = str(amount)
            details = []
            if sub:
                details.append(sub)
            if date:
                details.append(date)
            if note:
                details.append(note[:60])
            return {
                "headline": f"{event_type or 'cash'} {amt_str}",
                "detail": " · ".join(details),
                "kind": "cash",
            }

    # ─── Spreads ───
    if op in ("add_spread", "edit_spread", "spread_closed", "spread_expired",
              "delete_spread"):
        if isinstance(payload, dict):
            spr = payload
            if "original" in spr and isinstance(spr["original"], dict):
                spr = spr["original"]
            ticker = spr.get("ticker", "?")
            stype = spr.get("type", "spread")
            strikes = spr.get("strikes") or [spr.get("short_strike"), spr.get("long_strike")]
            strikes = [s for s in strikes if s is not None]
            details = []
            exp = spr.get("exp", "")
            contracts = spr.get("contracts") or 1
            sub = spr.get("subaccount", "")
            if exp:
                details.append(f"exp {exp}")
            try:
                ic = int(contracts)
                if ic > 0:
                    details.append(f"×{ic}")
            except Exception:
                pass
            if sub:
                details.append(sub)
            strike_str = ""
            if len(strikes) >= 2:
                strike_str = f" {_money(strikes[0])}/{_money(strikes[1])}"
            elif len(strikes) == 1:
                strike_str = f" {_money(strikes[0])}"
            return {
                "headline": f"{ticker} {stype}{strike_str}",
                "detail": " · ".join(details),
                "kind": "spread",
            }

    # ─── Roll ───
    if op == "roll_option":
        if isinstance(payload, dict):
            new_data = payload.get("new") if isinstance(payload.get("new"), dict) else None
            closed_data = payload.get("closed") if isinstance(payload.get("closed"), dict) else None
            ticker = (new_data or closed_data or {}).get("ticker", "?")
            opt_type = (new_data or closed_data or {}).get("type", "")
            old_strike = (closed_data or {}).get("strike")
            new_strike = (new_data or {}).get("strike")
            old_exp = (closed_data or {}).get("exp")
            new_exp = (new_data or {}).get("exp")
            sub = (new_data or {}).get("subaccount", "")
            details = []
            if old_exp and new_exp:
                details.append(f"{old_exp} → {new_exp}")
            elif new_exp:
                details.append(f"new exp {new_exp}")
            if sub:
                details.append(sub)
            strike_str = ""
            if old_strike and new_strike:
                strike_str = f" {_money(old_strike)} → {_money(new_strike)}"
            elif new_strike:
                strike_str = f" → {_money(new_strike)}"
            return {
                "headline": f"Roll {ticker} {opt_type}{strike_str}",
                "detail": " · ".join(details),
                "kind": "option",
            }

    # ─── Lumpsum ───
    if op in ("add_lumpsum", "update_lumpsum", "delete_lumpsum"):
        if isinstance(payload, dict):
            label = payload.get("label", "lumpsum")
            value = payload.get("value", 0)
            details = [f"value {_money(value)}"]
            sub = payload.get("subaccount", "")
            if sub:
                details.append(sub)
            return {
                "headline": f"Lumpsum: {label}",
                "detail": " · ".join(details),
                "kind": "lumpsum",
            }

    # ─── Transfers ───
    if op in ("transfer_kyleigh", "transfer_clay"):
        if isinstance(payload, dict):
            recipient = "Kyleigh" if op.endswith("kyleigh") else "Clay"
            amount = payload.get("amount", 0)
            from_acct = payload.get("from_account", "")
            details = []
            if from_acct:
                details.append(f"from {from_acct}")
            return {
                "headline": f"Transfer to {recipient} {_money(amount)}",
                "detail": " · ".join(details),
                "kind": "transfer",
            }

    # ─── Retro-fix (Phase 4.5) ───
    if op == "retrofix_assignment":
        if isinstance(payload, dict):
            ticker = payload.get("ticker", "?")
            strike = payload.get("strike", "?")
            shares = payload.get("shares_added", 0)
            sub = payload.get("subaccount", "")
            details = [f"+{int(shares)} sh @ {_money(strike)}"]
            if sub:
                details.append(sub)
            return {
                "headline": f"Retro-fix: {ticker} CSP {_money(strike)}",
                "detail": " · ".join(details),
                "kind": "option",
            }

    # ─── Sub-account / system ───
    if op in ("add_subaccount", "remove_subaccount"):
        return {
            "headline": f"{op}: {target}",
            "detail": "",
            "kind": "system",
        }

    if op == "WIPE_ACCOUNT":
        return {
            "headline": f"WIPED account {target}",
            "detail": "snapshot taken first",
            "kind": "system",
        }

    # Default fallback
    return {
        "headline": target[:50] if target else op,
        "detail": "",
        "kind": "other",
    }


def get_recent_audit_entries(limit: int = 20) -> List[Dict]:
    """Return recent audit entries with parsed payloads + enrichment, newest first."""
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

        item = {
            "timestamp": entry.get("timestamp", ""),
            "account": entry.get("account", ""),
            "op": entry.get("op", ""),
            "target": entry.get("target", ""),
            "old_value": old_v,
            "new_value": new_v,
            "undoable": entry.get("op", "") in UNDOABLE_OPS,
        }
        # Phase 4.5 — enriched display
        item["enriched"] = _enrich_audit_target(item)
        # Pretty-printed JSON for the click-to-expand panel
        try:
            item["full_json"] = json.dumps({
                "op": item["op"],
                "account": item["account"],
                "target": item["target"],
                "old_value": old_v,
                "new_value": new_v,
            }, indent=2, default=str)
        except Exception:
            item["full_json"] = ""
        parsed.append(item)
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

        # ─── Phase 4.5: auto-handled assignments — revert option AND remove the share lot/sell ───
        if op == "option_assigned_with_shares":
            opt_id = target
            options = get_options(account)
            opt = next((o for o in options if o.get("id") == opt_id), None)
            if not opt:
                return {"ok": False, "error": "Option not found"}

            ticker = opt.get("ticker")
            sub = opt.get("subaccount") or DEFAULT_SUBACCOUNT
            contracts = int(opt.get("contracts") or 1)
            fill = float(opt.get("actual_fill_price") or opt.get("strike") or 0)
            shares = 100 * contracts

            # 1. Reverse the auto-created share lot
            holdings = get_holdings(account)
            h = holdings.get(ticker)
            if h:
                # Remove the shares (this is best-effort — multiple buys may have averaged)
                sell_holding(account, ticker, shares=shares, sell_price=fill)
                # The sell_holding records a cash credit. We need to cancel it
                # since the original auto-handle's debit is also being cancelled below.
                # Use delete_cash_events to wash both.
                linked = find_linked_cash_events_for_holding(account, ticker)
                most_recent_buy = next((c["id"] for c in linked if c.get("type") == "share_buy"), None)
                most_recent_sell = next((c["id"] for c in linked if c.get("type") == "share_sell"), None)
                wash_ids = [i for i in (most_recent_buy, most_recent_sell) if i]
                if wash_ids:
                    delete_cash_events_bulk(account, wash_ids)

            # 2. Revert the option to open
            opt["status"] = "open"
            for k in ("close_premium", "close_date", "closed_at", "close_note",
                      "actual_fill_price", "auto_handled_shares"):
                opt.pop(k, None)
            _save(_key_options(account), options)

            # 3. Best-effort campaign cleanup
            try:
                from . import campaigns as _campaigns
                # Find the campaign that contains this assignment event
                for camp in _campaigns.get_campaigns(account):
                    for ev in camp.get("events", []):
                        if ev.get("id") == opt_id and ev.get("type") == "csp_assigned":
                            _campaigns.remove_event_from_campaign(
                                account, camp["id"],
                                {"id": opt_id, "type": "csp_assigned"}
                            )
                            # If status was active_holding, we may need to revert
                            # Best-effort only — full recovery is hard
                            break
            except Exception as e:
                log.warning(f"undo campaign cleanup failed: {e}")

            _audit(account, "UNDO_option_assigned_with_shares", opt_id, None,
                   {"reverted_status": "open", "shares_reversed": shares})
            return {"ok": True, "msg": f"Reverted: option re-opened, {shares} shares + cash reversed"}

        if op == "cc_called_away_with_shares":
            opt_id = target
            options = get_options(account)
            opt = next((o for o in options if o.get("id") == opt_id), None)
            if not opt:
                return {"ok": False, "error": "Option not found"}

            ticker = opt.get("ticker")
            sub = opt.get("subaccount") or DEFAULT_SUBACCOUNT
            contracts = int(opt.get("contracts") or 1)
            fill = float(opt.get("actual_fill_price") or opt.get("strike") or 0)
            shares = 100 * contracts

            # 1. Re-add the shares that were called away
            #    Use fill as cost basis since that's what the callaway sold at
            #    (this is approximate — original basis is harder to recover)
            r = add_holding(account, ticker, shares=shares, cost_basis=fill,
                            subaccount=sub, tag="wheel")
            # The add_holding records a buy debit; we need to wash both that
            # and the original sell credit.
            linked = find_linked_cash_events_for_holding(account, ticker)
            most_recent_buy = next((c["id"] for c in linked if c.get("type") == "share_buy"), None)
            most_recent_sell = next((c["id"] for c in linked if c.get("type") == "share_sell"), None)
            wash_ids = [i for i in (most_recent_buy, most_recent_sell) if i]
            if wash_ids:
                delete_cash_events_bulk(account, wash_ids)

            # 2. Revert the option to open
            opt["status"] = "open"
            for k in ("close_premium", "close_date", "closed_at", "close_note",
                      "actual_fill_price", "auto_handled_shares"):
                opt.pop(k, None)
            _save(_key_options(account), options)

            # 3. Campaign cleanup (best-effort)
            try:
                from . import campaigns as _campaigns
                for camp in _campaigns.get_campaigns(account):
                    for ev in camp.get("events", []):
                        if ev.get("id") == opt_id and ev.get("type") == "cc_called_away":
                            _campaigns.remove_event_from_campaign(
                                account, camp["id"],
                                {"id": opt_id, "type": "cc_called_away"}
                            )
                            # If campaign was closed, reopen
                            if camp.get("status") == "closed":
                                camps = _campaigns._load_campaigns(account)
                                for c in camps:
                                    if c.get("id") == camp["id"]:
                                        c["status"] = "active_holding"
                                        c["closed_at"] = None
                                _campaigns._save_campaigns(account, camps)
                            break
            except Exception as e:
                log.warning(f"undo campaign cleanup failed: {e}")

            _audit(account, "UNDO_cc_called_away_with_shares", opt_id, None,
                   {"reverted_status": "open", "shares_restored": shares})
            return {"ok": True, "msg": f"Reverted: option re-opened, {shares} shares restored, cash reversed"}

        # ─── Phase 4.5: partial_close_option — undo the closed-portion option ───
        if op == "partial_close_option" and isinstance(new_v, dict):
            closed_portion_id = new_v.get("closed_portion_id")
            original_total_contracts = (new_v.get("original") or {}).get("contracts")
            opt_id = target  # original option

            # 1. Restore original option's contract count and remove the closed-portion record
            options = get_options(account)
            original = next((o for o in options if o.get("id") == opt_id), None)
            if not original:
                return {"ok": False, "error": "Original option not found"}
            try:
                original["contracts"] = int(original_total_contracts or original.get("contracts", 1))
            except Exception:
                pass
            options = [o for o in options if o.get("id") != closed_portion_id]
            _save(_key_options(account), options)

            # 2. Reverse linked cash for the closed portion
            if closed_portion_id:
                linked = find_linked_cash_events(account, closed_portion_id)
                if linked:
                    delete_cash_events_bulk(account, [c["id"] for c in linked])

            # 3. If auto-handled, also reverse the share creation/removal
            if new_v.get("auto_handled") and isinstance(new_v.get("auto_result"), dict):
                ar = new_v["auto_result"]
                ticker = ar.get("ticker")
                sub = ar.get("subaccount") or DEFAULT_SUBACCOUNT
                if ar.get("kind") == "csp_assignment":
                    n = int(ar.get("shares_acquired") or 0)
                    fp = float(ar.get("fill_price") or 0)
                    if ticker and n > 0:
                        sell_holding(account, ticker, shares=n, sell_price=fp)
                        linked = find_linked_cash_events_for_holding(account, ticker)
                        wash = [c["id"] for c in linked if c.get("type") in ("share_buy", "share_sell")][:2]
                        if wash:
                            delete_cash_events_bulk(account, wash)
                elif ar.get("kind") == "cc_called_away":
                    n = int(ar.get("shares_sold") or 0)
                    fp = float(ar.get("fill_price") or 0)
                    if ticker and n > 0:
                        add_holding(account, ticker, shares=n, cost_basis=fp,
                                    subaccount=sub, tag="wheel")
                        linked = find_linked_cash_events_for_holding(account, ticker)
                        wash = [c["id"] for c in linked if c.get("type") in ("share_buy", "share_sell")][:2]
                        if wash:
                            delete_cash_events_bulk(account, wash)

            _audit(account, "UNDO_partial_close", opt_id, None, {"closed_portion_id": closed_portion_id})
            return {"ok": True, "msg": "Reverted: partial close undone, contracts restored"}

        # ─── Phase 4.5: retrofix_assignment — undo the share lot creation ───
        if op == "retrofix_assignment" and isinstance(new_v, dict):
            ticker = new_v.get("ticker")
            shares = int(new_v.get("shares_added") or 0)
            fill = float(new_v.get("strike") or 0)
            if ticker and shares > 0:
                sell_holding(account, ticker, shares=shares, sell_price=fill)
                linked = find_linked_cash_events_for_holding(account, ticker)
                wash = [c["id"] for c in linked if c.get("type") in ("share_buy", "share_sell")][:2]
                if wash:
                    delete_cash_events_bulk(account, wash)
            _audit(account, "UNDO_retrofix", target, None, {"ticker": ticker, "shares_reversed": shares})
            return {"ok": True, "msg": f"Reverted: retro-fix undone, {shares} {ticker} shares reversed"}

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

        if op == "edit_spread" and isinstance(old_v, dict):
            spread_id = target
            spreads = get_spreads(account)
            for i, s in enumerate(spreads):
                if s.get("id") == spread_id:
                    spreads[i] = old_v
                    _save(_key_spreads(account), spreads)
                    return {"ok": True, "msg": "Reverted: spread restored to previous values"}
            return {"ok": False, "error": "Spread not found"}

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
