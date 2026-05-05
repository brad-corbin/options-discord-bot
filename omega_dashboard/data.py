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
import os
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple

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
        for key, h in holdings.items():
            if not isinstance(h, dict):
                continue
            ticker = h.get("ticker") or (key.split("@")[0] if "@" in key else key)
            out["shares"].append({
                "ticker": ticker.upper(),
                "shares": h.get("shares"),
                "cost_basis": h.get("cost_basis"),
                "tag": h.get("tag"),
                "account": acc,
                "subaccount": h.get("subaccount"),
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
# Phase 4.5+ — Live quote fetcher (Finnhub) with cache
# ─────────────────────────────────────────────────────────

_QUOTE_CACHE: Dict[str, Tuple[float, Dict]] = {}  # ticker → (timestamp, data)
_QUOTE_TTL_SECONDS = 60.0  # cache for 1 minute (Finnhub free tier rate-limited)


def fetch_live_quote(ticker: str) -> Optional[Dict]:
    """Fetch a live quote from Finnhub. Returns:
        {ticker, price, change, change_pct, prev_close, day_high, day_low}
    Or None on failure. Cached for 60s per ticker.
    """
    if not ticker:
        return None
    t = ticker.upper().strip()
    now = time.time()
    cached = _QUOTE_CACHE.get(t)
    if cached and (now - cached[0]) < _QUOTE_TTL_SECONDS:
        return cached[1]

    token = os.getenv("FINNHUB_TOKEN", "").strip()
    if not token:
        return None

    try:
        import requests
        resp = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": t, "token": token},
            timeout=4,
        )
        if resp.status_code != 200:
            return None
        d = resp.json() or {}
        # Finnhub returns: c=current, d=change, dp=change_pct, h=high, l=low, pc=prev_close
        price = float(d.get("c") or 0)
        if price <= 0:
            return None  # bad symbol or zero quote
        result = {
            "ticker": t,
            "price": round(price, 2),
            "change": round(float(d.get("d") or 0), 2),
            "change_pct": round(float(d.get("dp") or 0), 2),
            "prev_close": round(float(d.get("pc") or 0), 2),
            "day_high": round(float(d.get("h") or 0), 2),
            "day_low": round(float(d.get("l") or 0), 2),
        }
        _QUOTE_CACHE[t] = (now, result)
        return result
    except Exception:
        return None


def get_watchlist_for_account(ui_account: str, max_tickers: int = 12) -> List[Dict]:
    """Build a watchlist from open positions in this account.

    Returns one entry per unique ticker, sorted by capital exposure (largest first):
        [{ticker, has_csp, has_cc, has_shares, has_spread, capital_at_risk,
          contracts, shares, quote: {price, change, change_pct} or None}]
    """
    from . import writes
    accounts = underlying_accounts(ui_account)
    if not accounts:
        return []

    by_ticker: Dict[str, Dict] = {}

    def _b(t: str) -> Dict:
        if t not in by_ticker:
            by_ticker[t] = {
                "ticker": t,
                "has_csp": False,
                "has_cc": False,
                "has_shares": False,
                "has_spread": False,
                "capital_at_risk": 0.0,
                "contracts": 0,
                "shares": 0.0,
                "cost_basis": 0.0,
                "subaccounts": set(),
            }
        return by_ticker[t]

    for acc in accounts:
        try:
            for opt in writes.get_options(acc):
                if not isinstance(opt, dict) or opt.get("status") != "open":
                    continue
                t = (opt.get("ticker") or "").upper()
                if not t:
                    continue
                b = _b(t)
                ot = opt.get("type", "")
                if ot == "CSP":
                    b["has_csp"] = True
                    b["capital_at_risk"] += (
                        float(opt.get("strike") or 0)
                        * int(opt.get("contracts") or 1) * 100
                    )
                elif ot == "CC":
                    b["has_cc"] = True
                b["contracts"] += int(opt.get("contracts") or 1)
                if opt.get("subaccount"):
                    b["subaccounts"].add(opt["subaccount"])

            for key, h in writes.get_holdings(acc).items():
                if not isinstance(h, dict):
                    continue
                shares = float(h.get("shares") or 0)
                if shares <= 0:
                    continue
                # Resolve real ticker — value field, or strip composite suffix from key
                ticker = h.get("ticker") or (key.split("@")[0] if "@" in key else key)
                t = ticker.upper()
                b = _b(t)
                b["has_shares"] = True
                b["shares"] += shares
                b["cost_basis"] = float(h.get("cost_basis") or 0)
                b["capital_at_risk"] += shares * b["cost_basis"]
                if h.get("subaccount"):
                    b["subaccounts"].add(h["subaccount"])

            # Phase 4.5+ — include open spreads
            for spr in writes.get_spreads(acc):
                if not isinstance(spr, dict) or spr.get("status") != "open":
                    continue
                t = (spr.get("ticker") or "").upper()
                if not t:
                    continue
                b = _b(t)
                b["has_spread"] = True
                # Capital risk for a vertical spread = (strike width) * contracts * 100
                # for debit spreads, or max loss for credit spreads.
                try:
                    long_k = float(spr.get("long_strike") or 0)
                    short_k = float(spr.get("short_strike") or 0)
                    width = abs(short_k - long_k)
                    contracts = int(spr.get("contracts") or 1)
                    b["capital_at_risk"] += width * contracts * 100
                    b["contracts"] += contracts
                except Exception:
                    pass
                if spr.get("subaccount"):
                    b["subaccounts"].add(spr["subaccount"])
        except Exception:
            pass

    # Sort by capital_at_risk descending
    rows = list(by_ticker.values())
    rows.sort(key=lambda r: r["capital_at_risk"], reverse=True)
    rows = rows[:max_tickers]

    # Attach live quotes (cached)
    for r in rows:
        r["subaccounts"] = sorted(r["subaccounts"])
        r["quote"] = fetch_live_quote(r["ticker"])

    return rows


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
                "total_deposits": 0.0,
                "total_withdrawals": 0.0,
                "net_capital": 0.0,  # deposits - withdrawals (Brad's "adjusted starting balance")
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

            # Capital tracking (Brad's mental model): deposits add to invested
            # capital, withdrawals subtract from it. ROI uses the running net.
            if ev_type == "deposit":
                if amt > 0:
                    b["total_deposits"] += amt
                else:
                    b["total_withdrawals"] += abs(amt)
            elif ev_type == "withdrawal":
                b["total_withdrawals"] += abs(amt)

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
        b["total_deposits"] = round(b["total_deposits"], 2)
        b["total_withdrawals"] = round(b["total_withdrawals"], 2)
        # Net capital = what's actually invested (deposits − withdrawals).
        # Brad's spreadsheet-equivalent: "adjusted starting balance" reflecting
        # mid-year cash movements. ROI denominator uses this.
        b["net_capital"] = round(b["total_deposits"] - b["total_withdrawals"], 2)
        # Keep `starting_balance` as alias for back-compat with template
        b["starting_balance"] = b["net_capital"]
        b["roi_ytd"] = round(
            (b["income_ytd"] / b["net_capital"]) * 100.0, 2
        ) if b["net_capital"] > 0 else 0.0
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
        for key, h in holdings.items():
            if not isinstance(h, dict):
                continue
            ticker = h.get("ticker") or (key.split("@")[0] if "@" in key else key)
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
    # Phase 4.5+ — combined view aggregates across all accounts; show more tickers
    wl_max = 30 if ui_account == "combined" else 12
    watchlist = get_watchlist_for_account(ui_account, max_tickers=wl_max)

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
        "watchlist": watchlist,
        "open_total": open_total,
        "live_mode": True,  # flag for template if it wants to indicate "live"
    }

"""
─────────────────────────────────────────────────────────────────────────────
Trading tab data layer — Day 2 ship (card grid).

Phase 5a Day 2 — replaces the row table with a per-ticker card view.

Strategy: each card needs four sections of data:
  1. Header context (ticker, spot, %day, time, signal pills)
  2. Watch Map levels (from em_log:{date}:{ticker}:silent + gex:{ticker})
  3. OI today ledger (from volume_flags:{date} filtered to ticker)
  4. Flow today ledger (from flow_history:{ticker}:{date})

This module replaces the v1 trading_data() function (15-column row data)
with v2 trading_data() that returns enriched per-ticker card payloads.

Stage 1 upstream patch (oi_flow.py) writes flow_history:* keys. If that
patch isn't deployed, the Flow ledger renders empty — same behavior as a
ticker with no flow events. Defensive readers throughout.

Failure mode: if any reader fails, the offending section renders empty for
that ticker. Other sections still populate. One bad ticker doesn't break
the page. Trading dashboard never crashes the server.
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import time
from datetime import datetime, date as _date_cls
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_TRADING_CACHE: Dict[str, tuple] = {}
_TRADING_CACHE_TTL_SEC = 1.0

# Top N levels above and below spot rendered per card. Matches the
# Watch Map mockup (4 above, 4 below).
_LEVELS_PER_SIDE = 4

# Top N events per ledger. Trading screenshot example shows 4 flow rows;
# more than 8 gets visually noisy and pushes the card too tall.
_LEDGER_MAX_EVENTS = 6


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def trading_data(ui_account: str) -> Dict:
    """Build the trading tab card payload.

    Returns:
        {
            "ui_account": str,
            "cards": [<per-ticker card dict>, ...],   # alphabetical
            "tickers_total": int,
            "tickers_with_data": int,
            "fetched_at_ts": float,
            "fetched_at_ct": str,
            "available": bool,
            "error": Optional[str],
        }

    Each card dict shape:
        {
            "ticker": "PLTR",
            "spot": 146.75,
            "pct_day": 0.34,
            "as_of_time_ct": "16:13",

            "thesis": {"bias": "STRONG BULLISH", "score": 7,
                       "stripe_class": "bullish-strong"},
            "pb": {"floor": 128.50, "roof": 145.20, "location": "in",
                   "label": "PB IN $128–$145"},
            "gex": {"sign": "positive", "label": "GEX +"},
            "as_signal": "T2 BULL" or None,

            "levels": {
                "above": [(152.40, "call wall"), ...],   # nearest first
                "below": [(144.80, "key hold"), ...],    # nearest first
                "available": True,
            },
            "oi_ledger": {
                "events": [
                    {"time": "15:08", "label": "bull buildup · calls",
                     "side": "call", "direction": "buildup", "bias": "bull",
                     "strike_dte_label": "$148 · 7DTE"},
                    ...
                ],
                "available": True,
            },
            "flow_ledger": {
                "events": [
                    {"time": "14:33", "bias": "bull", "side": "call",
                     "strike_label": "bull · $147C", "notional_label": "$221K",
                     "dte_label": "0D", "dte_class": "short", "is_latest": False},
                    {..., "is_latest": True},
                ],
                "available": True,
            },
        }
    """
    cache_key = f"v2:{ui_account}"
    cached = _TRADING_CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached[0]) < _TRADING_CACHE_TTL_SEC:
        return cached[1]

    try:
        import dashboard as bot_dashboard
        from oi_flow import FLOW_TICKERS
    except Exception as e:
        log.warning(f"trading_data: bot module import failed: {e}")
        empty = _empty_response(ui_account, now,
                                "Trading data layer is initializing or unavailable.")
        _TRADING_CACHE[cache_key] = (now, empty)
        return empty

    bot_state = getattr(bot_dashboard, "_persistent_state", None)
    bot_get_spot = getattr(bot_dashboard, "_get_spot_fn", None)

    cards: List[Dict] = []
    tickers_with_data = 0
    today_ct = _ct_today_str()
    today_utc = _utc_today_str()

    for ticker in sorted(FLOW_TICKERS):
        try:
            card = _build_card(ticker, bot_dashboard, bot_state,
                               bot_get_spot, today_ct, today_utc)
        except Exception as e:
            log.debug(f"trading_data: card build failed for {ticker}: {e}")
            card = _empty_card(ticker)

        if _card_has_signal(card):
            tickers_with_data += 1

        cards.append(card)

    cards.sort(key=_card_sort_key)

    result = {
        "ui_account": ui_account,
        "cards": cards,
        "tickers_total": len(cards),
        "tickers_with_data": tickers_with_data,
        "fetched_at_ts": now,
        "fetched_at_ct": _ct_time_str(now),
        "available": True,
        "error": None,
    }
    _TRADING_CACHE[cache_key] = (now, result)
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CARD BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def _build_card(ticker: str, bot_dashboard, bot_state, bot_get_spot,
                today_ct: str, today_utc: str) -> Dict:
    """Build one ticker card from existing bot state. Reads only —
    never writes back to the bot."""
    snap = bot_dashboard.get_ticker_snapshot(ticker)

    spot = snap.get("spot")
    pct_day = snap.get("pct_day")

    # Header pills
    thesis = _build_thesis_pill(snap)
    pb = _build_pb_pill(snap)
    gex = _build_gex_pill(snap)
    as_signal = (snap.get("active_scanner") or "").strip() or None

    # Levels (Watch Map data — em_log + gex + thesis + spot)
    levels = _build_levels(ticker, spot, bot_state, today_utc)

    # OI ledger (today's volume flags filtered to ticker)
    oi_ledger = _build_oi_ledger(ticker, bot_state, today_ct)

    # Flow ledger (flow_history:{ticker}:{date} from Stage 1 patch)
    flow_ledger = _build_flow_ledger(ticker, bot_state, today_ct, today_utc)

    return {
        "ticker": ticker,
        "spot": spot,
        "pct_day": pct_day,
        "as_of_time_ct": snap.get("updated", "") or "",
        "thesis": thesis,
        "pb": pb,
        "gex": gex,
        "as_signal": as_signal,
        "levels": levels,
        "oi_ledger": oi_ledger,
        "flow_ledger": flow_ledger,
    }


def _empty_card(ticker: str) -> Dict:
    """Default card when reader fails — no data, no crash."""
    return {
        "ticker": ticker,
        "spot": None,
        "pct_day": None,
        "as_of_time_ct": "",
        "thesis": {"bias": "", "score": None, "stripe_class": "neutral"},
        "pb": {"floor": None, "roof": None, "location": "none", "label": ""},
        "gex": {"sign": "", "label": ""},
        "as_signal": None,
        "levels": {"above": [], "below": [], "available": False},
        "oi_ledger": {"events": [], "available": False},
        "flow_ledger": {"events": [], "available": False},
    }


# ═════════════════════════════════════════════════════════════════════════════
# HEADER PILLS
# ═════════════════════════════════════════════════════════════════════════════

def _build_thesis_pill(snap: Dict) -> Dict:
    bias = (snap.get("thesis_bias") or "").strip()
    score = snap.get("thesis_score")

    bias_lc = bias.lower()
    if "strong bull" in bias_lc:
        stripe_class = "bullish-strong"
    elif "bull" in bias_lc:
        stripe_class = "bullish" if "slight" not in bias_lc else "bullish-faint"
    elif "strong bear" in bias_lc:
        stripe_class = "bearish-strong"
    elif "bear" in bias_lc:
        stripe_class = "bearish" if "slight" not in bias_lc else "bearish-faint"
    else:
        stripe_class = "neutral"

    return {
        "bias": bias,
        "score": score,
        "stripe_class": stripe_class,
    }


def _build_pb_pill(snap: Dict) -> Dict:
    floor = snap.get("pb_floor")
    roof = snap.get("pb_roof")
    location = (snap.get("pb_location") or "none").strip().lower()

    label_parts = []
    if location and location != "none":
        label_parts.append(f"PB {location.upper()}")
    else:
        label_parts.append("PB NONE")

    if floor and roof:
        try:
            label_parts.append(f"${float(floor):.0f}–${float(roof):.0f}")
        except (TypeError, ValueError):
            pass

    return {
        "floor": floor,
        "roof": roof,
        "location": location,
        "label": " ".join(label_parts),
    }


def _build_gex_pill(snap: Dict) -> Dict:
    sign = (snap.get("gex_sign") or "").strip().lower()
    if sign == "positive":
        label = "GEX +"
    elif sign == "negative":
        label = "GEX −"
    else:
        label = ""
    return {"sign": sign, "label": label}


# ═════════════════════════════════════════════════════════════════════════════
# WATCH MAP LEVELS
# ═════════════════════════════════════════════════════════════════════════════
# Reads em_log:{utc_date}:{ticker}:silent (written 8:30 AM CT by the bot's
# silent thesis daemon) plus gex:{ticker} for the live gamma/walls. Sorts
# all candidate levels above/below spot. The label per level is the most
# meaningful name from a priority order.

# Priority order — when the same price has multiple labels, prefer the more
# specific one. This is the visual hierarchy that matters most when
# scanning the levels list.
_LEVEL_LABEL_PRIORITY = [
    "EM high", "EM low", "call wall", "put wall", "gamma flip",
    "max pain", "next target", "primary", "key hold",
    "pivot", "R1", "S1", "R2", "S2",
    "fib resistance", "fib support",
    "VPOC", "vp resistance", "vp support",
    "local resistance", "local support",
    "pin",
]


def _build_levels(ticker: str, spot: Optional[float], bot_state,
                  today_utc: str) -> Dict:
    if not bot_state or not spot or spot <= 0:
        return {"above": [], "below": [], "available": False}

    em_log = _read_em_log(bot_state, ticker, today_utc)
    gex = _read_gex(bot_state, ticker)

    if not em_log and not gex:
        return {"above": [], "below": [], "available": False}

    # Build the candidate (level, label) list from all available sources.
    # Pre-merge by price: if two sources contribute the same price, the
    # higher-priority label wins.
    candidates: List[Tuple[float, str]] = []

    def add(level, label):
        try:
            v = float(level)
            if v > 0:
                candidates.append((round(v, 2), label))
        except (TypeError, ValueError):
            pass

    # GEX live takes precedence over em_log static for gamma_flip + walls
    if gex:
        add(gex.get("gamma_flip"), "gamma flip")
        add(gex.get("call_wall"), "call wall")
        add(gex.get("put_wall"), "put wall")
        add(gex.get("max_pain"), "max pain")

    if em_log:
        add(em_log.get("bull_1sd"), "EM high")
        add(em_log.get("bear_1sd"), "EM low")
        # gamma_flip/walls — only if gex didn't already provide them
        if not gex or not gex.get("gamma_flip"):
            add(em_log.get("gamma_flip"), "gamma flip")
        if not gex or not gex.get("call_wall"):
            add(em_log.get("call_wall"), "call wall")
        if not gex or not gex.get("put_wall"):
            add(em_log.get("put_wall"), "put wall")
        if not gex or not gex.get("max_pain"):
            add(em_log.get("max_pain"), "max pain")

        add(em_log.get("pin_zone_high"), "pin zone hi")
        add(em_log.get("pin_zone_low"), "pin zone lo")
        add(em_log.get("pivot"), "pivot")
        add(em_log.get("r1"), "R1")
        add(em_log.get("s1"), "S1")
        add(em_log.get("r2"), "R2")
        add(em_log.get("s2"), "S2")
        add(em_log.get("fib_resistance"), "fib resistance")
        add(em_log.get("fib_support"), "fib support")
        add(em_log.get("vp_resistance"), "vp resistance")
        add(em_log.get("vp_support"), "vp support")
        add(em_log.get("vpoc"), "VPOC")
        add(em_log.get("local_resistance_1"), "local resistance")
        add(em_log.get("local_support_1"), "local support")

    # De-dup by price, keep highest-priority label
    by_price: Dict[float, str] = {}
    priority_idx = {label: i for i, label in enumerate(_LEVEL_LABEL_PRIORITY)}
    for price, label in candidates:
        existing = by_price.get(price)
        if existing is None:
            by_price[price] = label
        else:
            old_p = priority_idx.get(existing, 999)
            new_p = priority_idx.get(label, 999)
            if new_p < old_p:
                by_price[price] = label

    # Split above and below spot
    above = sorted(((p, lbl) for p, lbl in by_price.items() if p > spot),
                    key=lambda x: x[0])
    below = sorted(((p, lbl) for p, lbl in by_price.items() if p < spot),
                    key=lambda x: x[0], reverse=True)

    # Trim to N per side. "Nearest first" = lowest price above-spot at top,
    # highest price below-spot at top of the below list.
    above = above[:_LEVELS_PER_SIDE]
    below = below[:_LEVELS_PER_SIDE]

    # Mark the closest level above spot as "next target" if it doesn't
    # already have a more-specific label
    if above:
        first_price, first_label = above[0]
        if first_label not in ("call wall", "put wall", "gamma flip",
                                "EM high", "EM low", "max pain"):
            above[0] = (first_price, "next target")

    return {
        "above": above,   # list of (price, label) tuples, nearest first
        "below": below,
        "available": True,
    }


def _read_em_log(bot_state, ticker: str, today_utc: str) -> Optional[Dict]:
    """em_log lookup — checks both :silent (morning baseline) and :manual
    (force-runs), returning whichever is most recent.

    v9 (Patch 2c): added :manual fallback. Previously only :silent was read,
    which meant force-runs were invisible to the levels panel — a force-run
    would update thesis_monitor:{ticker} and write em_log:{date}:{ticker}:manual
    but the dashboard would keep showing morning's silent baseline. The whole
    purpose of force-running (refresh the data because something changed) was
    being defeated. Now both keys are read; the most recent wins by
    logged_at_utc timestamp comparison. If only one exists, that one is
    returned. Empty defensive returns (None) on any error so the calling
    _build_levels function falls through to its other sources.
    """
    try:
        silent = bot_state._json_get(f"em_log:{today_utc}:{ticker.upper()}:silent")
        manual = bot_state._json_get(f"em_log:{today_utc}:{ticker.upper()}:manual")
        if silent and manual:
            # Both present — take whichever has the newer timestamp
            ts_s = silent.get("logged_at_utc") or silent.get("timestamp") or ""
            ts_m = manual.get("logged_at_utc") or manual.get("timestamp") or ""
            return manual if ts_m > ts_s else silent
        return manual or silent  # one or neither — None falls through
    except Exception as e:
        log.debug(f"em_log read failed for {ticker}: {e}")
        return None


def _read_gex(bot_state, ticker: str) -> Optional[Dict]:
    """GEX data via the persistent_state fallback chain.

    v9 (Patch 2c): routed through bot_state.get_gex_data() which (per Patch
    2b rev1) reads thesis_monitor:{ticker} as Source 1 and falls back to
    gex:{ticker} only if thesis is missing. Previously this read gex:{ticker}
    directly, bypassing the fallback chain entirely — which produced empty
    levels panels whenever the lightweight blob was deleted/expired even if
    a thesis was available. The lightweight gex:{ticker} writer is being
    deprecated by Patch 2a anyway; routing through get_gex_data() decouples
    this reader from that key's lifecycle.
    """
    try:
        result = bot_state.get_gex_data(ticker)
        return result if result else None
    except Exception as e:
        log.debug(f"gex read failed for {ticker}: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# OI LEDGER
# ═════════════════════════════════════════════════════════════════════════════

def _build_oi_ledger(ticker: str, bot_state, today_ct: str) -> Dict:
    if not bot_state:
        return {"events": [], "available": False}

    try:
        flags = bot_state.get_volume_flags(today_ct) or []
    except Exception as e:
        log.debug(f"oi flags read failed for {ticker}: {e}")
        return {"events": [], "available": False}

    t_flags = [f for f in flags if (f.get("ticker") or "").upper() == ticker.upper()]
    if not t_flags:
        return {"events": [], "available": True}

    # Sort by timestamp ascending (oldest first → latest last)
    def _ts_of(f):
        return f.get("timestamp") or f.get("time") or f.get("ts") or ""
    t_flags.sort(key=_ts_of)

    events: List[Dict] = []
    for f in t_flags[-_LEDGER_MAX_EVENTS:]:
        time_str = _fmt_hhmm_ct(_ts_of(f))
        side = (f.get("side") or "").lower()  # call | put
        side_label = "calls" if side == "call" else "puts" if side == "put" else side

        # Direction inference (matches dashboard.py:_get_oi_snapshot logic)
        flow_type = (f.get("flow_type") or f.get("type") or "").lower()
        directional_bias = (f.get("directional_bias") or "").upper()

        if "buildup" in flow_type:
            direction = "buildup"
        elif "unwind" in flow_type:
            direction = "unwind"
        else:
            # Same-day flags don't have flow_type yet; infer from
            # directional_bias + side
            if "BULLISH" in directional_bias:
                direction = "buildup" if side == "call" else "unwind"
            elif "BEARISH" in directional_bias:
                direction = "buildup" if side == "put" else "unwind"
            else:
                direction = ""

        # Bias of the event for color: bull buildup on calls = bullish,
        # bear buildup on puts = bearish, unwinds are neutral
        if direction == "buildup":
            if side == "call":
                bias = "bullish"
            elif side == "put":
                bias = "bearish"
            else:
                bias = "neutral"
        elif direction == "unwind":
            bias = "neutral"  # unwinds always render in neutral color
        else:
            bias = "neutral"

        # Display label: e.g. "bull buildup · calls" or "unwind · calls"
        if direction == "buildup" and bias == "bullish":
            label = f"bull buildup · {side_label}"
        elif direction == "buildup" and bias == "bearish":
            label = f"bear buildup · {side_label}"
        elif direction == "unwind":
            label = f"unwind · {side_label}"
        elif direction:
            label = f"{direction} · {side_label}"
        else:
            label = side_label

        # strike + DTE label
        strike = f.get("strike")
        expiry = f.get("expiry") or ""
        dte = _calc_dte(expiry)
        if strike and expiry:
            try:
                strike_label = f"${float(strike):.0f} · {dte}DTE"
            except (TypeError, ValueError):
                strike_label = f"{strike} · {dte}DTE"
        elif strike:
            try:
                strike_label = f"${float(strike):.0f}"
            except (TypeError, ValueError):
                strike_label = str(strike)
        else:
            strike_label = ""

        events.append({
            "time": time_str,
            "label": label,
            "bias": bias,
            "side": side,
            "direction": direction,
            "strike_dte_label": strike_label,
        })

    if events:
        events[-1]["is_latest"] = True
    for e in events[:-1]:
        e["is_latest"] = False

    return {"events": events, "available": True}


# ═════════════════════════════════════════════════════════════════════════════
# FLOW LEDGER
# ═════════════════════════════════════════════════════════════════════════════

def _build_flow_ledger(ticker: str, bot_state, today_ct: str,
                       today_utc: str) -> Dict:
    if not bot_state:
        return {"events": [], "available": False}

    # Stage 1 patch writes flow_history:{TICKER}:{today}. The "today" used
    # at write time comes from the bot's existing today_str (UTC). We try
    # both UTC and CT just in case the bot's clock semantics differ.
    history = None
    for date_str in (today_utc, today_ct):
        try:
            key = f"flow_history:{ticker.upper()}:{date_str}"
            history = bot_state._json_get(key)
            if history:
                break
        except Exception as e:
            log.debug(f"flow_history read failed for {ticker} {date_str}: {e}")

    if not history:
        return {"events": [], "available": True}

    history = history[-_LEDGER_MAX_EVENTS:]

    events: List[Dict] = []
    for h in history:
        time_str = _fmt_hhmm_ct(h.get("ts"))
        direction = (h.get("direction") or "").lower()  # bullish | bearish
        side = (h.get("side") or "").lower()  # call | put
        bias = "bullish" if direction == "bullish" else "bearish" if direction == "bearish" else "neutral"

        # Strike label: "bull · $147C" or "bear · $382P"
        side_letter = "C" if side == "call" else "P" if side == "put" else "?"
        strike = h.get("strike")
        bias_short = "bull" if bias == "bullish" else "bear" if bias == "bearish" else ""
        if strike is not None:
            try:
                strike_label = f"{bias_short} · ${float(strike):.0f}{side_letter}".strip(" ·")
            except (TypeError, ValueError):
                strike_label = f"{bias_short} · {strike}{side_letter}".strip(" ·")
        else:
            strike_label = bias_short

        # Notional formatting: $221K, $1.4M
        notional = h.get("notional") or 0
        notional_label = _fmt_notional(notional)

        # DTE pill
        # v8.3 (Patch 2): now shows actual expiry date alongside DTE so the
        # trader can see both at a glance. Format: "5/8 · 1D" (date · DTE).
        # Falls back to just "1D" if expiry parse fails but DTE was computable.
        expiry = h.get("expiry") or ""
        dte = _calc_dte(expiry)
        if dte is None:
            dte_label = ""
            dte_class = "unknown"
        else:
            md = _fmt_expiry_md(expiry)
            dte_label = f"{md} · {dte}D" if md else f"{dte}D"
            # Color: 0DTE = warning (hedging/scalping), 7+DTE = success
            # (positioning), 1-6DTE = neutral
            if dte == 0:
                dte_class = "short"
            elif dte >= 7:
                dte_class = "long"
            else:
                dte_class = "mid"

        events.append({
            "time": time_str,
            "bias": bias,
            "side": side,
            "strike_label": strike_label,
            "notional_label": notional_label,
            "notional_value": int(notional or 0),
            "dte_label": dte_label,
            "dte_class": dte_class,
            "flow_level": (h.get("flow_level") or "").lower(),
        })

    if events:
        events[-1]["is_latest"] = True
    for e in events[:-1]:
        e["is_latest"] = False

    return {"events": events, "available": True}


# ═════════════════════════════════════════════════════════════════════════════
# SORTING / DENSITY
# ═════════════════════════════════════════════════════════════════════════════

def _card_has_signal(card: Dict) -> bool:
    if card.get("spot"):
        return True
    if card["thesis"].get("bias"):
        return True
    if card["pb"].get("floor"):
        return True
    if card["oi_ledger"]["events"]:
        return True
    if card["flow_ledger"]["events"]:
        return True
    return False


def _card_sort_key(card: Dict):
    """Sort cards by signal density desc, then alphabetical ticker.

    Density rule:
      - Each flow event: +1
      - Each OI event: +1
      - Extreme flow_level: +2 bonus
      - Multi-day DTE flow event: +1 bonus
      - Conflicting bull/bear in same ledger: -1 penalty (mixed signal noise)

    The (negative density, ticker) tuple sorts highest density first,
    alphabetical for ties.
    """
    score = 0
    flow_events = card["flow_ledger"]["events"]
    oi_events = card["oi_ledger"]["events"]

    score += len(flow_events)
    score += len(oi_events)

    flow_levels = [e.get("flow_level") for e in flow_events]
    score += 2 * sum(1 for fl in flow_levels if fl == "extreme")

    flow_dtes = [e.get("dte_class") for e in flow_events]
    score += sum(1 for d in flow_dtes if d in ("mid", "long"))

    biases = [e.get("bias") for e in flow_events]
    if "bullish" in biases and "bearish" in biases:
        score -= 1

    return (-score, card["ticker"])


# ═════════════════════════════════════════════════════════════════════════════
# FORMATTING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _fmt_notional(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${int(v)}"


def _fmt_hhmm_ct(ts) -> str:
    """Convert ISO timestamp to HH:MM Central Time."""
    if not ts:
        return ""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return str(ts)[11:16] if len(str(ts)) >= 16 else ""
    try:
        # Handle ISO with or without TZ
        s = str(ts)
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Treat naive as UTC (the bot writes datetime.now().isoformat()
            # which is server-local; on Render that's typically UTC)
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M")
    except Exception:
        return ""


def _calc_dte(expiry: str) -> Optional[int]:
    if not expiry:
        return None
    try:
        exp = _date_cls.fromisoformat(str(expiry)[:10])
        today = _ct_today_date()
        return max(0, (exp - today).days)
    except Exception:
        return None


# v8.3 (Patch 2): compact M/D formatter for the DTE pill.
# Same parsing path as _calc_dte so a string that yields a valid DTE will
# also yield a date label. Returns "" on parse failure.
def _fmt_expiry_md(expiry: str) -> str:
    if not expiry:
        return ""
    try:
        exp = _date_cls.fromisoformat(str(expiry)[:10])
        return f"{exp.month}/{exp.day}"
    except Exception:
        return ""


def _ct_today_date() -> _date_cls:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return _date_cls.today()


def _ct_today_str() -> str:
    return _ct_today_date().isoformat()


def _utc_today_str() -> str:
    try:
        from datetime import timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return _date_cls.today().isoformat()


def _ct_time_str(ts: float) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo("America/Chicago")).strftime("%H:%M:%S")
    except Exception:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _empty_response(ui_account: str, now: float, error: str) -> Dict:
    return {
        "ui_account": ui_account,
        "cards": [],
        "tickers_total": 0,
        "tickers_with_data": 0,
        "fetched_at_ts": now,
        "fetched_at_ct": _ct_time_str(now),
        "available": False,
        "error": error,
    }