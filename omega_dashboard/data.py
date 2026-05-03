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
    "partner":  [],   # added in Phase 4
    "kyleigh":  [],   # added in Phase 4
    "combined": ["brad", "mom"],
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

def command_center_data(ui_account: str) -> Dict:
    """Everything needed to render Command Center for the given UI account."""
    pf_available = portfolio_data_available(ui_account)
    snap_meta = get_snapshot_meta()
    snap = get_latest_snapshot() if pf_available else None

    if not pf_available:
        return {
            "ui_account": ui_account,
            "portfolio_available": False,
            "snapshot_meta": snap_meta,
        }

    if not snap:
        return {
            "ui_account": ui_account,
            "portfolio_available": True,
            "snapshot_available": False,
            "snapshot_meta": snap_meta,
        }

    income = calc_income_from_snapshot(snap, ui_account)
    goal = calc_goal_pace(income)
    positions = get_open_positions_from_snapshot(snap, ui_account)
    cash = get_cash_from_snapshot(snap, ui_account)

    open_total = (
        len(positions["wheel_options"])
        + len(positions["spreads"])
    )

    return {
        "ui_account": ui_account,
        "portfolio_available": True,
        "snapshot_available": True,
        "snapshot_meta": snap_meta,
        "income": income,
        "goal": goal,
        "positions": positions,
        "cash": cash,
        "open_total": open_total,
    }
