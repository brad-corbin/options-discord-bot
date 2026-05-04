"""Phase 3 — Snapshot-based read-only data layer.

Only data source: the most recent portfolio snapshot tab in Sheets, which is
captured nightly by Phase 2's durability layer. No reads from bot internals
(no _scanner, no _thesis_engine, no _cached_md). No live API calls. No hangs
possible.

Trade-off: data freshness = "as of last 06:00 UTC snapshot". Worst case 24h old.
For Phase 3 (read-only views) this is fine. Phase 5+ can add live updates.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

log = logging.getLogger(__name__)

# UI-account → underlying portfolio account keys
UI_TO_PORTFOLIO = {
    "mine":     ["brad"],
    "mom":      ["mom"],
    "partner":  ["partner"],   # Phase 4 — Day Trades
    "kyleigh":  ["kyleigh"],   # Phase 4.5 — partner ledger
    "clay":     ["clay"],      # Phase 4.5 — partner ledger
    "combined": ["brad", "mom", "partner"],  # Excludes kyleigh/clay (notional partners)
}


# ─────────────────────────────────────────────────────────
# Snapshot caching (1 minute, in-memory)
# ─────────────────────────────────────────────────────────
# Reading from Sheets is fast (~200ms) but we don't need to do it on every
# page navigation. Cache the parsed snapshot dict for 60 seconds.

_snapshot_cache: Dict[str, Any] = {"data": None, "fetched_at": 0, "tab": None}
_CACHE_TTL = 60  # seconds


def get_latest_snapshot() -> Optional[Dict]:
    """Get the most recent portfolio snapshot, with a 60s cache."""
    now = time.time()
    if _snapshot_cache["data"] and (now - _snapshot_cache["fetched_at"]) < _CACHE_TTL:
        return _snapshot_cache["data"]

    # Late-bind to durability — same module, no circular issue
    try:
        from . import durability
    except Exception as e:
        log.debug(f"durability unavailable: {e}")
        return None

    try:
        snapshots = durability.list_snapshots()
        if not snapshots:
            return None
        latest = snapshots[0]  # already sorted newest first
        snap = durability.read_snapshot(latest["date"])
        if snap:
            _snapshot_cache["data"] = snap
            _snapshot_cache["fetched_at"] = now
            _snapshot_cache["tab"] = latest["tab"]
            return snap
    except Exception as e:
        log.warning(f"Snapshot fetch failed: {e}")
    return None


def get_snapshot_meta() -> Dict:
    """Metadata about the snapshot currently feeding the dashboard."""
    snap = get_latest_snapshot()
    if not snap:
        return {"available": False}
    return {
        "available": True,
        "tab": _snapshot_cache.get("tab"),
        "captured_at": snap.get("captured_at"),
    }


# ─────────────────────────────────────────────────────────
# Account helpers
# ─────────────────────────────────────────────────────────

def underlying_accounts(ui_account: str) -> List[str]:
    return UI_TO_PORTFOLIO.get(ui_account, [])


def portfolio_data_available(ui_account: str) -> bool:
    """Does this UI account have any underlying portfolio data?"""
    return bool(underlying_accounts(ui_account))


# ─────────────────────────────────────────────────────────
# Income calculation from snapshot
# ─────────────────────────────────────────────────────────

def _option_close_month(opt: Dict) -> Optional[str]:
    """Return YYYY-MM for the close event, or None if open/unknown."""
    if not isinstance(opt, dict):
        return None
    status = opt.get("status")
    if status not in ("closed", "expired", "assigned", "rolled"):
        return None
    close_date = opt.get("close_date") or opt.get("exp")
    if not close_date:
        return None
    try:
        s = str(close_date).split("+")[0].split(".")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m")
            except Exception:
                continue
    except Exception:
        return None
    return None


def _option_pnl(opt: Dict) -> float:
    """Compute realized P/L for a closed option. Same formula as portfolio.py."""
    if not isinstance(opt, dict) or opt.get("status") == "open":
        return 0.0
    try:
        premium = float(opt.get("premium") or 0)
        close_premium = float(opt.get("close_premium") or 0)
        contracts = int(opt.get("contracts") or 1)
        direction = opt.get("direction", "sell")
        if direction == "sell":
            return round((premium - close_premium) * contracts * 100, 2)
        return round((close_premium - premium) * contracts * 100, 2)
    except Exception:
        return 0.0


def calc_income_from_snapshot(snap: Dict, ui_account: str) -> Dict:
    """Sum realized option income by month from the snapshot."""
    if not snap:
        return {"available": False}

    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False}

    snapshot_accounts = snap.get("accounts") or {}

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    by_month: Dict[str, float] = {}
    total_year = 0.0
    total_month = 0.0

    for acc in accounts:
        acct_data = snapshot_accounts.get(acc) or {}
        options = acct_data.get("options") or []
        for opt in options:
            month_key = _option_close_month(opt)
            if not month_key:
                continue
            pnl = _option_pnl(opt)
            by_month[month_key] = by_month.get(month_key, 0.0) + pnl
            if month_key.startswith(current_year):
                total_year += pnl
            if month_key == current_month:
                total_month += pnl

    return {
        "available": True,
        "month": round(total_month, 2),
        "year": round(total_year, 2),
        "by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
    }


def calc_goal_pace(income: Dict) -> Dict:
    """Goal = average of completed-month income within current year."""
    if not income.get("available"):
        return {"available": False}
    by_month = income.get("by_month") or {}
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    completed = [
        v for k, v in by_month.items()
        if k.startswith(current_year) and k < current_month
    ]
    if not completed:
        return {
            "available": True,
            "complete": False,
            "reason": "Goal benchmark builds from February onward (needs ≥1 completed month)",
        }

    avg = sum(completed) / len(completed)
    actual = by_month.get(current_month, 0.0)
    pct = (actual / avg * 100.0) if avg > 0 else 0.0

    return {
        "available": True,
        "complete": True,
        "goal": round(avg, 2),
        "actual": round(actual, 2),
        "pct": min(round(pct, 1), 999.9),
        "completed_months": len(completed),
    }


# ─────────────────────────────────────────────────────────
# Open positions from snapshot
# ─────────────────────────────────────────────────────────

def get_open_positions_from_snapshot(snap: Dict, ui_account: str) -> Dict[str, List[Dict]]:
    """Open positions, grouped by type, from snapshot data."""
    accounts = underlying_accounts(ui_account)
    out = {"wheel_options": [], "shares": [], "spreads": []}

    if not snap or not accounts:
        return out

    snapshot_accounts = snap.get("accounts") or {}

    for acc in accounts:
        acct_data = snapshot_accounts.get(acc) or {}

        # Open options only
        for opt in (acct_data.get("options") or []):
            if not isinstance(opt, dict) or opt.get("status") != "open":
                continue
            out["wheel_options"].append({
                "ticker": (opt.get("ticker") or "").upper(),
                "type": (opt.get("type") or "").upper(),
                "strike": opt.get("strike"),
                "exp": opt.get("exp"),
                "premium": opt.get("premium"),
                "contracts": opt.get("contracts", 1),
                "direction": opt.get("direction", "sell"),
                "tag": opt.get("tag"),
                "account": acc,
            })

        # Holdings (always open)
        holdings = acct_data.get("holdings") or {}
        for ticker, h in holdings.items():
            if not isinstance(h, dict):
                continue
            out["shares"].append({
                "ticker": ticker.upper(),
                "shares": h.get("shares"),
                "cost_basis": h.get("cost_basis"),
                "tag": h.get("tag"),
                "account": acc,
            })

        # Open spreads only
        for spr in (acct_data.get("spreads") or []):
            if not isinstance(spr, dict) or spr.get("status") != "open":
                continue
            out["spreads"].append({
                "ticker": (spr.get("ticker") or "").upper(),
                "type": (spr.get("type") or "").upper(),
                "long": spr.get("long_strike"),
                "short": spr.get("short_strike"),
                "exp": spr.get("exp"),
                "debit": spr.get("debit"),
                "credit": spr.get("credit"),
                "contracts": spr.get("contracts", 1),
                "account": acc,
            })

    return out


# ─────────────────────────────────────────────────────────
# Cash from snapshot
# ─────────────────────────────────────────────────────────

def get_cash_from_snapshot(snap: Dict, ui_account: str) -> Dict:
    """Cash balances per underlying account."""
    accounts = underlying_accounts(ui_account)
    if not snap or not accounts:
        return {"available": False, "total": 0, "by_account": {}}

    snapshot_accounts = snap.get("accounts") or {}
    by_account = {}
    total = 0.0

    for acc in accounts:
        cash = (snapshot_accounts.get(acc) or {}).get("cash") or {}
        if not isinstance(cash, dict):
            cash = {}
        balance = float(cash.get("cash_balance") or cash.get("balance") or 0)
        by_account[acc] = balance
        total += balance

    return {
        "available": True,
        "total": round(total, 2),
        "by_account": by_account,
    }


# ─────────────────────────────────────────────────────────
# Aggregator for the Command Center page
# ─────────────────────────────────────────────────────────

def _option_close_month_live(opt: Dict) -> Optional[str]:
    """Same as _option_close_month but for live data — accepts open_date too."""
    if not isinstance(opt, dict):
        return None
    status = opt.get("status")
    if status not in ("closed", "expired", "assigned", "rolled"):
        return None
    close_date = opt.get("close_date") or opt.get("exp")
    if not close_date:
        return None
    try:
        s = str(close_date).split("+")[0].split(".")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m")
            except Exception:
                continue
    except Exception:
        return None
    return None


def _option_pnl_live(opt: Dict) -> float:
    if not isinstance(opt, dict) or opt.get("status") == "open":
        return 0.0
    try:
        premium = float(opt.get("premium") or 0)
        close_premium = float(opt.get("close_premium") or 0)
        contracts = int(opt.get("contracts") or 1)
        direction = opt.get("direction", "sell")
        if direction == "sell":
            return round((premium - close_premium) * contracts * 100, 2)
        return round((close_premium - premium) * contracts * 100, 2)
    except Exception:
        return 0.0


def _close_month_from_date(date_str: Optional[str]) -> Optional[str]:
    """Parse any YYYY-MM-DD-ish date string into 'YYYY-MM'. None if unparseable."""
    if not date_str:
        return None
    try:
        s = str(date_str).split("+")[0].split(".")[0].split("T")[0]
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m")
            except Exception:
                continue
    except Exception:
        return None
    return None


def _spread_close_month(spr: Dict) -> Optional[str]:
    """Month-bucket key for a closed/expired spread."""
    if not isinstance(spr, dict):
        return None
    if spr.get("status") not in ("closed", "expired"):
        return None
    return _close_month_from_date(spr.get("close_date") or spr.get("exp"))


def _spread_pnl(spr: Dict) -> float:
    """Realized P&L on a closed spread.

    Credit spreads: open_credit - close_value (positive when expired worthless)
    Debit spreads:  close_value - open_debit
    """
    if not isinstance(spr, dict) or spr.get("status") not in ("closed", "expired"):
        return 0.0
    try:
        contracts = int(spr.get("contracts") or 1)
        close_val = float(spr.get("close_value") or 0)
        if spr.get("credit") is not None and spr.get("credit") != 0:
            return round((float(spr.get("credit") or 0) - close_val) * contracts * 100, 2)
        else:
            return round((close_val - float(spr.get("debit") or 0)) * contracts * 100, 2)
    except Exception:
        return 0.0


def calc_income_live(ui_account: str) -> Dict:
    """Sum ALL realized income by month from the LIVE cash ledger.

    Phase 4.5+ — rewritten to walk the cash ledger directly instead of
    iterating positions. This matches Brad's accounting rule:

      "Premium = income at the month it's collected.
       BTC without roll = expense in the month closed.
       Rolls net = income/expense in the roll month."

    Cash event types counted:
      - option_open   (credit positive = sell premium income;
                       debit negative = long option purchase expense)
      - option_close  (debit negative = BTC short closing expense;
                       credit positive = STC long closing income)
      - spread_open   (credit positive = credit spread opened;
                       debit negative = debit spread opened)
      - spread_close  (signed cash impact at close)
      - roll_credit   (always positive — net credit from roll)
      - roll_debit    (always negative — net debit from roll)

    Plus share P&L: sum of `sold_lots[].realized_pnl` by sale date.

    NOT counted as income (capital flow, not P&L):
      deposit, withdrawal, share_buy, share_sell, transfer_out,
      transfer_kyleigh, transfer_clay, lumpsum_*
    """
    from . import writes

    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False}

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    by_month: Dict[str, float] = {}
    by_source: Dict[str, float] = {"options": 0.0, "spreads": 0.0, "shares": 0.0, "fees": 0.0, "summary": 0.0}
    total_year = 0.0
    total_month = 0.0

    # Income-bearing cash event types and their bucket
    INCOME_EVENT_BUCKETS = {
        "option_open": "options",
        "option_close": "options",
        "roll_credit": "options",
        "roll_debit": "options",
        "spread_open": "spreads",
        "spread_close": "spreads",
        # Phase 4.5 — fees count as P&L expenses (negative impact)
        "fee": "fees",
        # Phase 4.5 — manual P&L summary entries (e.g., "January 2026 net" for
        # high-frequency trading accounts that aren't worth logging trade-by-trade).
        # Signed: positive = net gain, negative = net loss.
        "pnl": "summary",
    }

    def add(month_key, amount, source):
        nonlocal total_year, total_month
        if not month_key or not amount:
            return
        by_month[month_key] = by_month.get(month_key, 0.0) + amount
        by_source[source] = by_source.get(source, 0.0) + amount
        if month_key.startswith(current_year):
            total_year += amount
        if month_key == current_month:
            total_month += amount

    for acc in accounts:
        # ─── Walk the cash ledger ───
        try:
            ledger = writes.get_cash_ledger(acc)
        except Exception:
            ledger = []

        for ev in ledger:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type", "")
            bucket = INCOME_EVENT_BUCKETS.get(ev_type)
            if not bucket:
                continue  # skip deposit, withdrawal, share_buy/sell, transfer_*
            try:
                amt = float(ev.get("amount") or 0)
            except Exception:
                continue
            mk = _close_month_from_date(ev.get("date"))
            add(mk, amt, bucket)

        # ─── Sold share lots (realized P&L) ───
        try:
            sold_lots = writes.get_sold_lots(acc)
        except Exception:
            sold_lots = []

        for lot in sold_lots:
            if not isinstance(lot, dict):
                continue
            mk = _close_month_from_date(lot.get("date"))
            try:
                pnl = float(lot.get("realized_pnl") or 0)
            except Exception:
                pnl = 0.0
            add(mk, pnl, "shares")

    return {
        "available": True,
        "method": "cash_ledger",
        "month": round(total_month, 2),
        "year": round(total_year, 2),
        "by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
        "by_source": {k: round(v, 2) for k, v in by_source.items()},
    }


# ─────────────────────────────────────────────────────────
# Phase 4.5 — Partner ledger summaries (Kyleigh, Clay)
# ─────────────────────────────────────────────────────────

def calc_partner_summary(ui_account: str) -> Dict:
    """Simple ledger summary for a partner profit-sharing account.

    Per Brad's spec: track initial deposit + weekly income/loss + withdrawals.
    Show whether the partner's total is up or down vs what they put in.

    Event types:
      - deposit (positive)     = capital contribution
      - withdrawal (negative)  = paid out to partner
      - pnl (signed)           = weekly gain/loss attributed to partner
      - transfer_out (signed)  = legacy auto-mirror entries (treated as withdrawals)

    Computed:
      - total_deposits:   sum of positive deposits
      - total_withdrawals: |sum of negative withdrawals + transfer_out|
      - net_pnl:          sum of pnl entries (positive = up, negative = down)
      - current_balance:  total_deposits + net_pnl - total_withdrawals
                          = what's currently in the partner's name
      - direction:        "up" if net_pnl > 0, "down" if net_pnl < 0, "flat" otherwise
    """
    from . import writes

    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False, "is_partner": False}

    total_deposits = 0.0
    total_withdrawals = 0.0
    net_pnl = 0.0
    event_count = 0
    last_event_date = None

    for acc in accounts:
        try:
            ledger = writes.get_cash_ledger(acc)
        except Exception:
            ledger = []
        for ev in ledger:
            if not isinstance(ev, dict):
                continue
            try:
                amt = float(ev.get("amount") or 0)
            except Exception:
                continue
            ev_type = ev.get("type", "")
            event_count += 1
            d = ev.get("date") or ""
            if d and (last_event_date is None or d > last_event_date):
                last_event_date = d

            if ev_type == "deposit":
                if amt >= 0:
                    total_deposits += amt
                else:
                    total_withdrawals += abs(amt)
            elif ev_type in ("withdrawal", "transfer_out"):
                total_withdrawals += abs(amt)
            elif ev_type == "pnl":
                net_pnl += amt
            # All other types ignored (manual_set, etc.)

    current_balance = total_deposits + net_pnl - total_withdrawals
    if net_pnl > 0.001:
        direction = "up"
    elif net_pnl < -0.001:
        direction = "down"
    else:
        direction = "flat"

    return {
        "available": True,
        "is_partner": True,
        "total_deposits": round(total_deposits, 2),
        "total_withdrawals": round(total_withdrawals, 2),
        "net_pnl": round(net_pnl, 2),
        "current_balance": round(current_balance, 2),
        "direction": direction,
        "event_count": event_count,
        "last_event_date": last_event_date,
    }


def is_partner_account(ui_account: str) -> bool:
    """Check if a UI account is a partner-ledger-only account."""
    return ui_account in ("kyleigh", "clay")


# ─────────────────────────────────────────────────────────
# Phase 4.5 — Per-sub-account breakdown
# Replicates Brad's spreadsheet layout: each sub-account shows
# its own cash, holdings value, income YTD/this-month, and ROI.
# ─────────────────────────────────────────────────────────

def calc_subaccount_breakdown(ui_account: str) -> Dict:
    """Return per-sub-account financial breakdown.

    For each sub-account, returns:
      - cash:              current cash balance
      - holdings_at_cost:  shares × cost basis (cost basis tied up)
      - lumpsum:           lump-sum tracked value
      - total_value:       cash + holdings_at_cost + lumpsum
      - capital_reserved:  cash backing open CSPs (strike × contracts × 100)
      - income_ytd:        realized income YTD (option_open + spread_open + roll_credit
                            - roll_debit + option_close + spread_close + fees + pnl-summary)
      - income_month:      same metrics for current month
      - by_source:         {options, spreads, fees, summary, shares}
      - starting_balance:  earliest deposit-side balance (heuristic from cash ledger)
      - roi_ytd:           income_ytd / starting_balance × 100 (if starting > 0)
      - open_options:      count of open CSP/CC in this sub
      - open_spreads:      count of open spreads
      - open_shares:       sum of share lot sizes
    """
    from . import writes
    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False, "by_subaccount": {}}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current_month = today[:7]

    # Initialize buckets (sub-account name → metrics)
    by_sub: Dict[str, Dict] = {}

    def _bucket(sub: str) -> Dict:
        if sub not in by_sub:
            by_sub[sub] = {
                "cash": 0.0,
                "holdings_at_cost": 0.0,
                "lumpsum": 0.0,
                "capital_reserved": 0.0,
                "income_ytd": 0.0,
                "income_month": 0.0,
                "by_source": {"options": 0.0, "spreads": 0.0, "fees": 0.0, "summary": 0.0, "shares": 0.0},
                "starting_balance": 0.0,
                "open_options": 0,
                "open_spreads": 0,
                "open_shares": 0.0,
            }
        return by_sub[sub]

    INCOME_BUCKET = {
        "option_open": "options",
        "option_close": "options",
        "roll_credit": "options",
        "roll_debit": "options",
        "spread_open": "spreads",
        "spread_close": "spreads",
        "fee": "fees",
        "pnl": "summary",
    }

    for acc in accounts:
        # Cash + income from cash ledger
        try:
            ledger = writes.get_cash_ledger(acc)
        except Exception:
            ledger = []

        # Starting balance: sum of all deposits (initial capital). This is the
        # per-sub-account version of the "Account Start Balance" cell in Brad's
        # spreadsheet.
        for ev in ledger:
            if not isinstance(ev, dict):
                continue
            sub = ev.get("subaccount") or DEFAULT_SUBACCOUNT
            b = _bucket(sub)
            ev_type = ev.get("type", "")
            try:
                amt = float(ev.get("amount") or 0)
            except Exception:
                continue
            ev_date = ev.get("date", "")

            # Cash always
            b["cash"] += amt

            # Starting balance = positive deposits
            if ev_type == "deposit" and amt > 0:
                b["starting_balance"] += amt

            # Income classification
            bucket_name = INCOME_BUCKET.get(ev_type)
            if bucket_name:
                month = ev_date[:7] if ev_date else ""
                ev_year = ev_date[:4] if ev_date else ""
                this_year = today[:4]
                if ev_year == this_year:
                    b["income_ytd"] += amt
                    b["by_source"][bucket_name] += amt
                    if month == current_month:
                        b["income_month"] += amt

        # Lumpsum per sub
        try:
            for ls in writes.get_lumpsum(acc):
                if isinstance(ls, dict):
                    sub = ls.get("subaccount") or DEFAULT_SUBACCOUNT
                    _bucket(sub)["lumpsum"] += float(ls.get("value") or 0)
        except Exception:
            pass

        # Holdings per sub (cost basis × shares)
        try:
            for ticker, h in writes.get_holdings(acc).items():
                if isinstance(h, dict):
                    sub = h.get("subaccount") or DEFAULT_SUBACCOUNT
                    b = _bucket(sub)
                    shares = float(h.get("shares") or 0)
                    cost = float(h.get("cost_basis") or 0)
                    b["holdings_at_cost"] += shares * cost
                    b["open_shares"] += shares
        except Exception:
            pass

        # Open options per sub (and reserved capital for CSPs)
        try:
            for opt in writes.get_options(acc):
                if isinstance(opt, dict) and opt.get("status") == "open":
                    sub = opt.get("subaccount") or DEFAULT_SUBACCOUNT
                    b = _bucket(sub)
                    b["open_options"] += 1
                    if opt.get("type") == "CSP" and opt.get("direction", "sell") == "sell":
                        b["capital_reserved"] += (
                            float(opt.get("strike") or 0)
                            * int(opt.get("contracts") or 1)
                            * 100
                        )

            for spr in writes.get_spreads(acc):
                if isinstance(spr, dict) and spr.get("status") == "open":
                    sub = spr.get("subaccount") or DEFAULT_SUBACCOUNT
                    _bucket(sub)["open_spreads"] += 1

            # Sold lots — realized share P&L per sub (YTD)
            for lot in writes.get_sold_lots(acc):
                if not isinstance(lot, dict):
                    continue
                sub = lot.get("subaccount") or DEFAULT_SUBACCOUNT
                b = _bucket(sub)
                sold_date = lot.get("sold_date") or ""
                if sold_date[:4] == today[:4]:
                    pnl = float(lot.get("realized_pnl") or 0)
                    b["income_ytd"] += pnl
                    b["by_source"]["shares"] += pnl
                    if sold_date[:7] == current_month:
                        b["income_month"] += pnl
        except Exception:
            pass

    # Final pass: compute total_value, ROI
    for sub, b in by_sub.items():
        b["total_value"] = round(b["cash"] + b["holdings_at_cost"] + b["lumpsum"], 2)
        b["cash"] = round(b["cash"], 2)
        b["holdings_at_cost"] = round(b["holdings_at_cost"], 2)
        b["lumpsum"] = round(b["lumpsum"], 2)
        b["capital_reserved"] = round(b["capital_reserved"], 2)
        b["income_ytd"] = round(b["income_ytd"], 2)
        b["income_month"] = round(b["income_month"], 2)
        b["starting_balance"] = round(b["starting_balance"], 2)
        b["roi_ytd"] = round(
            (b["income_ytd"] / b["starting_balance"]) * 100.0, 2
        ) if b["starting_balance"] > 0 else 0.0
        b["by_source"] = {k: round(v, 2) for k, v in b["by_source"].items()}

    return {
        "available": True,
        "by_subaccount": by_sub,
    }


def get_open_positions_live(ui_account: str) -> Dict[str, List[Dict]]:
    """Open positions from LIVE Redis."""
    from . import writes
    accounts = underlying_accounts(ui_account)
    out = {"wheel_options": [], "shares": [], "spreads": []}

    for acc in accounts:
        for opt in writes.get_options(acc):
            if not isinstance(opt, dict) or opt.get("status") != "open":
                continue
            out["wheel_options"].append({
                "ticker": (opt.get("ticker") or "").upper(),
                "type": (opt.get("type") or "").upper(),
                "strike": opt.get("strike"),
                "exp": opt.get("exp"),
                "premium": opt.get("premium"),
                "contracts": opt.get("contracts", 1),
                "direction": opt.get("direction", "sell"),
                "tag": opt.get("tag"),
                "subaccount": opt.get("subaccount"),
                "category": opt.get("category"),
                "account": acc,
            })

        holdings = writes.get_holdings(acc)
        for ticker, h in holdings.items():
            if not isinstance(h, dict):
                continue
            out["shares"].append({
                "ticker": ticker.upper(),
                "shares": h.get("shares"),
                "cost_basis": h.get("cost_basis"),
                "tag": h.get("tag"),
                "subaccount": h.get("subaccount"),
                "account": acc,
            })

        for spr in writes.get_spreads(acc):
            if not isinstance(spr, dict) or spr.get("status") != "open":
                continue
            out["spreads"].append({
                "ticker": (spr.get("ticker") or "").upper(),
                "type": (spr.get("type") or "").upper(),
                "long": spr.get("long_strike"),
                "short": spr.get("short_strike"),
                "exp": spr.get("exp"),
                "debit": spr.get("debit"),
                "credit": spr.get("credit"),
                "contracts": spr.get("contracts", 1),
                "subaccount": spr.get("subaccount"),
                "account": acc,
            })

    return out


def get_cash_live(ui_account: str) -> Dict:
    """Cash totals + per-sub-account breakdown + lump-sum total from LIVE Redis."""
    from . import writes
    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False, "total": 0, "by_account": {}, "by_subaccount": {}, "lumpsum_total": 0}

    by_account = {}
    by_subaccount: Dict[str, float] = {}
    lumpsum_items = []
    total = 0.0
    lumpsum_total = 0.0
    total_deposited = 0.0

    for acc in accounts:
        balance = writes.calc_cash_balance(acc)
        by_account[acc] = balance
        total += balance

        # Sub-account breakdown — sum cash events by sub-account tag
        ledger = writes.get_cash_ledger(acc)
        for entry in ledger:
            if not isinstance(entry, dict):
                continue
            sub = entry.get("subaccount") or "Brokerage"
            amt = float(entry.get("amount") or 0)
            by_subaccount[sub] = by_subaccount.get(sub, 0.0) + amt
            # Track total deposits ever (positive deposit-type events) for capital progression
            if entry.get("type") == "deposit" and amt > 0:
                total_deposited += amt

        # Lump-sum holdings
        for ls in writes.get_lumpsum(acc):
            if isinstance(ls, dict):
                v = float(ls.get("value") or 0)
                lumpsum_total += v
                lumpsum_items.append({
                    "label": ls.get("label"),
                    "value": v,
                    "subaccount": ls.get("subaccount"),
                    "as_of": ls.get("as_of"),
                    "account": acc,
                })

    return {
        "available": True,
        "total": round(total, 2),
        "by_account": {k: round(v, 2) for k, v in by_account.items()},
        "by_subaccount": {k: round(v, 2) for k, v in by_subaccount.items()},
        "lumpsum_total": round(lumpsum_total, 2),
        "lumpsum_items": lumpsum_items,
        "total_deposited": round(total_deposited, 2),
    }


def calc_capital_progression(ui_account: str) -> Dict:
    """Total capital tracked = cash + lump-sums + open option premium value held + open shares at cost.
    Compared to total_deposited to show growth.

    Phase 4.5: For non-partner views, partner ledger balances are SUBTRACTED so
    the displayed capital reflects what actually belongs to the account holder.
    Example: mom's brokerage holds $125k total, but $6k of it is Kyleigh's
    capital + accrued profit-share. Mom's "true" capital = $119k.
    """
    from . import writes
    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False}

    cash_total = 0.0
    deposited = 0.0
    lumpsum_total = 0.0
    holdings_at_cost = 0.0
    open_premium_collected = 0.0  # premium currently held against open CSPs/CCs

    for acc in accounts:
        cash_total += writes.calc_cash_balance(acc)
        for entry in writes.get_cash_ledger(acc):
            if isinstance(entry, dict) and entry.get("type") == "deposit" and float(entry.get("amount") or 0) > 0:
                deposited += float(entry.get("amount") or 0)
        for ls in writes.get_lumpsum(acc):
            if isinstance(ls, dict):
                lumpsum_total += float(ls.get("value") or 0)
        for ticker, h in writes.get_holdings(acc).items():
            if isinstance(h, dict):
                holdings_at_cost += float(h.get("shares") or 0) * float(h.get("cost_basis") or 0)
        for opt in writes.get_options(acc):
            if isinstance(opt, dict) and opt.get("status") == "open":
                if opt.get("direction", "sell") == "sell":
                    open_premium_collected += float(opt.get("premium") or 0) * int(opt.get("contracts") or 1) * 100

    # Partner exclusion (Phase 4.5):
    # For each configured partner, only subtract their balance/deposits if their
    # host trading account is one of the underlying accounts in this view.
    # E.g., Kyleigh hosted at mom → excluded from mom & combined views, NOT from
    # brad's view. Partner views themselves skip exclusion entirely.
    partner_balance_total = 0.0
    partner_deposit_total = 0.0
    if not is_partner_account(ui_account):
        for partner_ui in ("kyleigh", "clay"):
            try:
                host = writes.get_partner_host(partner_ui)
                if not host:
                    continue  # no host configured → no exclusion
                if host not in accounts:
                    continue  # partner's capital lives elsewhere → don't subtract here
                ps = calc_partner_summary(partner_ui)
                if ps.get("available"):
                    partner_balance_total += float(ps.get("current_balance") or 0)
                    partner_deposit_total += float(ps.get("total_deposits") or 0)
            except Exception:
                pass

    total_capital_raw = cash_total + lumpsum_total + holdings_at_cost
    total_capital = total_capital_raw - partner_balance_total
    deposited_net = deposited - partner_deposit_total
    growth = total_capital - deposited_net
    growth_pct = (growth / deposited_net * 100.0) if deposited_net > 0 else 0.0

    return {
        "available": True,
        "total_capital": round(total_capital, 2),
        "total_capital_raw": round(total_capital_raw, 2),
        "deposited": round(deposited_net, 2),
        "deposited_raw": round(deposited, 2),
        "growth": round(growth, 2),
        "growth_pct": round(growth_pct, 1),
        "partner_balance_excluded": round(partner_balance_total, 2),
        "partner_deposits_excluded": round(partner_deposit_total, 2),
        "components": {
            "cash": round(cash_total, 2),
            "lumpsum": round(lumpsum_total, 2),
            "holdings_at_cost": round(holdings_at_cost, 2),
            "open_premium": round(open_premium_collected, 2),
        },
    }


def command_center_data(ui_account: str) -> Dict:
    """Live data for Command Center — reads Redis directly via writes layer.

    Snapshot meta is included for freshness display only — not used for data.
    """
    pf_available = portfolio_data_available(ui_account)
    snap_meta = get_snapshot_meta()

    if not pf_available:
        return {
            "ui_account": ui_account,
            "portfolio_available": False,
            "snapshot_meta": snap_meta,
        }

    # Phase 4.5 — partner ledger accounts (Kyleigh, Clay) get a different view
    if is_partner_account(ui_account):
        from . import writes
        summary = calc_partner_summary(ui_account)
        host = writes.get_partner_host(ui_account)

        # Pull the recent ledger for display
        recent_events = []
        for acc in underlying_accounts(ui_account):
            try:
                ledger = writes.get_cash_ledger(acc)
            except Exception:
                ledger = []
            for ev in ledger:
                if isinstance(ev, dict):
                    recent_events.append(ev)
        recent_events.sort(key=lambda e: e.get("date", ""), reverse=True)

        has_any_data = summary.get("event_count", 0) > 0

        return {
            "ui_account": ui_account,
            "portfolio_available": True,
            "snapshot_available": has_any_data,
            "is_partner": True,
            "partner_host": host,
            "host_options": ["", "brad", "mom", "partner"],
            "host_labels": {"": "— None —", "brad": "Corbin", "mom": "Volkman", "partner": "Partnership"},
            "snapshot_meta": snap_meta,
            "partner_summary": summary,
            "recent_events": recent_events[:10],
            "live_mode": True,
        }

    income = calc_income_live(ui_account)
    goal = calc_goal_pace(income)
    positions = get_open_positions_live(ui_account)
    cash = get_cash_live(ui_account)
    capital = calc_capital_progression(ui_account)
    sub_breakdown = calc_subaccount_breakdown(ui_account)

    open_total = (
        len(positions["wheel_options"])
        + len(positions["spreads"])
    )

    # Has any data at all?
    has_any_data = (
        cash.get("total", 0) != 0
        or open_total > 0
        or len(positions["shares"]) > 0
        or cash.get("lumpsum_total", 0) > 0
    )

    return {
        "ui_account": ui_account,
        "portfolio_available": True,
        "snapshot_available": has_any_data,  # name kept for template compat
        "is_partner": False,
        "snapshot_meta": snap_meta,
        "income": income,
        "goal": goal,
        "positions": positions,
        "cash": cash,
        "capital": capital,
        "sub_breakdown": sub_breakdown,
        "open_total": open_total,
        "live_mode": True,  # flag for template if it wants to indicate "live"
    }
