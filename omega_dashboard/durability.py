"""Phase 2 — Data Durability.

Three responsibilities:
  1. Snapshot full portfolio state to a dated Sheets tab nightly
  2. Audit log: every portfolio write appends to a Sheets tab for forensics
  3. Restore: rebuild Redis state from any historical snapshot

All Sheets I/O reuses the bot's existing auth setup in app.py via late-binding
imports (avoids circular import; app.py loads first, dashboard registers, then
these helpers are reachable at function-call time).
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────

SNAPSHOT_RETENTION_DAYS = int(os.getenv("OMEGA_SNAPSHOT_RETENTION_DAYS", "30"))
SNAPSHOT_TAB_PREFIX = "portfolio_snapshot_"
AUDIT_TAB = "portfolio_writes"

# UI accounts → underlying portfolio account keys.
# The bot currently only has brad + mom. Phase 4 will add new keys when the
# user enters Partnership / Kyleigh balances. Until then those are empty.
UI_TO_PORTFOLIO_ACCOUNTS = {
    "mine":    ["brad"],
    "mom":     ["mom"],
    "partner": ["partner"],   # Phase 4 — Day Trades
    # kyleigh, clay are notional-only, no positions to snapshot
}

# All underlying accounts we know about. Phase 2 snapshots whatever is here.
KNOWN_ACCOUNTS = sorted({
    a for keys in UI_TO_PORTFOLIO_ACCOUNTS.values() for a in keys
})


# ─────────────────────────────────────────────────────────
# Late-bound app.py helpers
# Avoid circular import — app.py loads first, then registers dashboard
# blueprint. By the time these run, both modules are fully loaded.
# ─────────────────────────────────────────────────────────

def _app_helpers() -> Optional[Dict[str, Any]]:
    """Get Sheets helpers from app.py. Returns None if unavailable."""
    try:
        from app import (
            _get_google_access_token,
            _append_google_sheet_values,
            _sheet_headers_exist,
            GOOGLE_SHEET_ID,
        )
        # _create_google_sheet_tab may exist; not all builds have it
        try:
            from app import _create_google_sheet_tab
        except Exception:
            _create_google_sheet_tab = None

        return {
            "token": _get_google_access_token,
            "append": _append_google_sheet_values,
            "headers_exist": _sheet_headers_exist,
            "create_tab": _create_google_sheet_tab,
            "sheet_id": GOOGLE_SHEET_ID,
        }
    except Exception as e:
        log.warning(f"App Sheets helpers unavailable: {e}")
        return None


def _portfolio_module():
    """Get the portfolio module. Returns None if not loaded."""
    try:
        import portfolio
        return portfolio
    except Exception as e:
        log.warning(f"Portfolio module unavailable: {e}")
        return None


# ─────────────────────────────────────────────────────────
# Snapshot — gather state from Redis
# ─────────────────────────────────────────────────────────

def gather_snapshot() -> Dict:
    """Read full portfolio state for all known accounts."""
    pf = _portfolio_module()
    if not pf:
        return {
            "version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "accounts": {},
            "error": "portfolio module not loaded",
        }

    snapshot = {
        "version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "accounts": {},
    }

    for acc in KNOWN_ACCOUNTS:
        acct_data = {"holdings": {}, "options": [], "spreads": [], "cash": {}}
        try:
            acct_data["holdings"] = pf.get_all_holdings(account=acc) or {}
        except Exception as e:
            log.warning(f"snapshot[{acc}] holdings failed: {e}")
        try:
            acct_data["options"] = pf.get_all_options(account=acc) or []
        except Exception as e:
            log.warning(f"snapshot[{acc}] options failed: {e}")
        try:
            acct_data["spreads"] = pf.get_all_spreads(account=acc) or []
        except Exception as e:
            log.warning(f"snapshot[{acc}] spreads failed: {e}")
        try:
            acct_data["cash"] = pf.get_cash_data(account=acc) or {}
        except Exception as e:
            log.warning(f"snapshot[{acc}] cash failed: {e}")

        # Phase 4 additions — read directly from store (these keys aren't
        # exposed via portfolio.py's API yet)
        try:
            from . import writes
            acct_data["cash_ledger"] = writes.get_cash_ledger(acc) or []
        except Exception as e:
            log.warning(f"snapshot[{acc}] cash_ledger failed: {e}")
        try:
            from . import writes
            acct_data["lumpsum"] = writes.get_lumpsum(acc) or []
        except Exception as e:
            log.warning(f"snapshot[{acc}] lumpsum failed: {e}")

        snapshot["accounts"][acc] = acct_data

    # Phase 4 — also snapshot transfer ledgers (recipient-keyed, not account-keyed)
    try:
        from . import writes
        snapshot["transfers"] = {
            "kyleigh": writes.get_transfer_ledger("kyleigh"),
            "clay":    writes.get_transfer_ledger("clay"),
        }
    except Exception as e:
        log.warning(f"snapshot transfers failed: {e}")
        snapshot["transfers"] = {"kyleigh": [], "clay": []}

    return snapshot


def summarize_account(data: Dict) -> Dict:
    """Compact stats for display."""
    if not isinstance(data, dict) or "error" in data:
        return {"error": data.get("error") if isinstance(data, dict) else "bad data"}
    holdings = data.get("holdings") or {}
    options = data.get("options") or []
    spreads = data.get("spreads") or []
    cash = data.get("cash") or {}
    return {
        "holdings": len(holdings),
        "options_open": sum(1 for o in options if isinstance(o, dict) and o.get("status") == "open"),
        "options_total": len(options),
        "spreads_open": sum(1 for s in spreads if isinstance(s, dict) and s.get("status") == "open"),
        "spreads_total": len(spreads),
        "cash": cash.get("balance") if isinstance(cash, dict) else None,
    }


# ─────────────────────────────────────────────────────────
# Snapshot serialization
# ─────────────────────────────────────────────────────────

SNAPSHOT_HEADERS = ["section", "account", "key", "value_json", "captured_at"]


def _snapshot_tab_name(when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{SNAPSHOT_TAB_PREFIX}{when.strftime('%Y-%m-%d')}"


def serialize_snapshot(snapshot: Dict) -> List[List[str]]:
    """Convert snapshot dict into rows for Sheets append."""
    captured_at = snapshot.get("captured_at", "")
    rows: List[List[str]] = []

    rows.append(["__meta__", "", "version", str(snapshot.get("version", 1)), captured_at])
    rows.append(["__meta__", "", "captured_at", captured_at, captured_at])

    for account, data in (snapshot.get("accounts") or {}).items():
        if not isinstance(data, dict):
            continue
        if "error" in data and "holdings" not in data:
            rows.append(["error", account, "msg", str(data.get("error", "")), captured_at])
            continue

        for ticker, info in (data.get("holdings") or {}).items():
            rows.append(["holdings", account, str(ticker), json.dumps(info, default=str), captured_at])
        for opt in (data.get("options") or []):
            opt_id = (opt.get("id") if isinstance(opt, dict) else None) or ""
            rows.append(["options", account, str(opt_id), json.dumps(opt, default=str), captured_at])
        for spr in (data.get("spreads") or []):
            spr_id = (spr.get("id") if isinstance(spr, dict) else None) or ""
            rows.append(["spreads", account, str(spr_id), json.dumps(spr, default=str), captured_at])

        cash = data.get("cash") or {}
        rows.append(["cash", account, "data", json.dumps(cash, default=str), captured_at])

    return rows


def deserialize_snapshot(rows: List[List[str]]) -> Dict:
    """Reconstruct snapshot dict from Sheets rows."""
    # Skip header row if present
    if rows and rows[0] and rows[0][0] == "section":
        rows = rows[1:]

    snapshot: Dict[str, Any] = {"version": 1, "accounts": {}}

    for row in rows:
        # Pad to 5 columns
        padded = (list(row) + [""] * 5)[:5]
        section, account, key, value_json, captured_at = padded

        if section == "__meta__":
            if key == "version":
                try:
                    snapshot["version"] = int(value_json) if value_json else 1
                except Exception:
                    snapshot["version"] = 1
            elif key == "captured_at":
                snapshot["captured_at"] = value_json
            continue

        if section == "error":
            snapshot["accounts"].setdefault(account, {})["error"] = value_json
            continue

        if not account:
            continue

        acct = snapshot["accounts"].setdefault(
            account, {"holdings": {}, "options": [], "spreads": [], "cash": {}}
        )

        try:
            value = json.loads(value_json) if value_json else None
        except Exception:
            value = None

        if section == "holdings" and key:
            if value is not None:
                acct["holdings"][key] = value
        elif section == "options":
            if value is not None:
                acct["options"].append(value)
        elif section == "spreads":
            if value is not None:
                acct["spreads"].append(value)
        elif section == "cash":
            acct["cash"] = value or {}

    return snapshot


# ─────────────────────────────────────────────────────────
# Sheets writers — take_snapshot, list, read
# ─────────────────────────────────────────────────────────

def _ensure_tab_with_headers(tab: str, headers: List[str], helpers: Dict) -> bool:
    """Ensure the tab exists and has headers in row 1."""
    token_fn = helpers.get("token")
    if not token_fn:
        return False
    token = token_fn()
    if not token:
        return False
    try:
        if not helpers["headers_exist"](tab, token):
            helpers["append"](tab, [headers], token)
        return True
    except Exception as e:
        log.warning(f"Tab/header setup failed for {tab}: {e}")
        return False


def take_snapshot() -> Dict:
    """Capture full portfolio state and write to a dated Sheets tab."""
    helpers = _app_helpers()
    if not helpers or not helpers.get("sheet_id"):
        return {"ok": False, "error": "Sheets not configured (GOOGLE_SHEET_ID missing or auth unavailable)"}

    snapshot = gather_snapshot()
    if "error" in snapshot and not snapshot.get("accounts"):
        return {"ok": False, "error": snapshot["error"]}

    tab = _snapshot_tab_name()
    rows = serialize_snapshot(snapshot)

    if not _ensure_tab_with_headers(tab, SNAPSHOT_HEADERS, helpers):
        return {"ok": False, "error": f"Could not create or initialize tab {tab}"}

    token = helpers["token"]()
    if not token:
        return {"ok": False, "error": "No Sheets access token"}

    try:
        success = helpers["append"](tab, rows, token)
    except Exception as e:
        log.exception(f"Snapshot write failed: {e}")
        return {"ok": False, "error": str(e)}

    if success:
        # Best-effort prune of old tabs
        try:
            _prune_old_snapshots(helpers)
        except Exception as e:
            log.warning(f"Prune failed (non-fatal): {e}")

    summary = {
        acc: summarize_account(d)
        for acc, d in (snapshot.get("accounts") or {}).items()
    }
    return {
        "ok": bool(success),
        "tab": tab,
        "rows": len(rows),
        "captured_at": snapshot.get("captured_at"),
        "summary": summary,
    }


def list_snapshots() -> List[Dict]:
    """List existing snapshot tabs sorted newest first."""
    helpers = _app_helpers()
    if not helpers or not helpers.get("sheet_id"):
        return []

    token = helpers["token"]()
    if not token:
        return []

    try:
        import requests
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{helpers['sheet_id']}"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "sheets.properties(title,sheetId)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        result = []
        for s in data.get("sheets", []):
            title = s.get("properties", {}).get("title", "")
            if title.startswith(SNAPSHOT_TAB_PREFIX):
                date_part = title.replace(SNAPSHOT_TAB_PREFIX, "")
                if re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
                    result.append({"tab": title, "date": date_part})
        result.sort(key=lambda x: x["date"], reverse=True)
        return result
    except Exception as e:
        log.warning(f"List snapshots failed: {e}")
        return []


def read_snapshot(date_iso: str) -> Optional[Dict]:
    """Read a snapshot back from a dated Sheets tab."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_iso or ""):
        return None
    helpers = _app_helpers()
    if not helpers or not helpers.get("sheet_id"):
        return None
    token = helpers["token"]()
    if not token:
        return None

    tab = f"{SNAPSHOT_TAB_PREFIX}{date_iso}"

    try:
        import requests
        rng = requests.utils.quote(f"{tab}!A:E", safe="!:")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{helpers['sheet_id']}/values/{rng}"
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=15
        )
        if resp.status_code == 400:
            log.warning(f"Snapshot {date_iso} not found")
            return None
        resp.raise_for_status()
        values = resp.json().get("values", [])
        return deserialize_snapshot(values)
    except Exception as e:
        log.warning(f"Read snapshot {date_iso} failed: {e}")
        return None


def restore_from_snapshot(date_iso: str) -> Dict:
    """Overwrite Redis portfolio data from a snapshot."""
    snapshot = read_snapshot(date_iso)
    if not snapshot:
        return {"ok": False, "error": f"Snapshot {date_iso} not found"}

    pf = _portfolio_module()
    if not pf:
        return {"ok": False, "error": "Portfolio module not available"}

    # Sanity: portfolio.py exposes _store_set / _key_holdings / etc. We use those
    # to write raw state. The functions are populated when app.py calls
    # portfolio.init_store(...).
    if pf._store_set is None:
        return {"ok": False, "error": "Portfolio store not initialized (app not fully started)"}

    restored = {}
    accounts_data = snapshot.get("accounts") or {}

    for account, data in accounts_data.items():
        if not isinstance(data, dict) or "error" in data:
            restored[account] = {"error": (data or {}).get("error", "skipped")}
            continue
        try:
            # Holdings (dict) — entire keyspace
            pf._store_set(pf._key_holdings(account), json.dumps(data.get("holdings") or {}))
            # Options (list)
            pf._store_set(pf._key_options(account), json.dumps(data.get("options") or []))
            # Spreads (list)
            pf._store_set(pf._key_spreads(account), json.dumps(data.get("spreads") or []))
            # Cash (dict)
            pf._store_set(pf._key_cash(account), json.dumps(data.get("cash") or {}))

            # Phase 4 ledgers — store via writes module key helpers
            try:
                from . import writes
                pf._store_set(writes._key_cash_ledger(account), json.dumps(data.get("cash_ledger") or []))
                pf._store_set(writes._key_lumpsum(account), json.dumps(data.get("lumpsum") or []))
            except Exception as e:
                log.warning(f"Phase 4 ledger restore for {account}: {e}")

            restored[account] = summarize_account(data)
        except Exception as e:
            log.exception(f"Restore failed for account {account}: {e}")
            restored[account] = {"error": str(e)}

    # Phase 4 — restore transfer ledgers (recipient-keyed)
    try:
        from . import writes
        transfers = snapshot.get("transfers") or {}
        if transfers.get("kyleigh") is not None:
            pf._store_set(writes._key_transfers("kyleigh"), json.dumps(transfers.get("kyleigh") or []))
        if transfers.get("clay") is not None:
            pf._store_set(writes._key_transfers("clay"), json.dumps(transfers.get("clay") or []))
    except Exception as e:
        log.warning(f"Restore transfers failed: {e}")

    # Audit-log the restore itself
    audit_write(
        "system",
        "restore",
        f"snapshot_{date_iso}",
        old_value=None,
        new_value={
            "snapshot_date": date_iso,
            "accounts_restored": list(accounts_data.keys()),
            "captured_at": snapshot.get("captured_at"),
        },
    )

    return {
        "ok": True,
        "snapshot_date": date_iso,
        "captured_at": snapshot.get("captured_at"),
        "restored": restored,
    }


def _prune_old_snapshots(helpers: Dict):
    """Delete snapshot tabs older than retention. Best-effort."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)).date()
    snapshots = list_snapshots()

    to_delete_dates = []
    for snap in snapshots:
        try:
            snap_date = datetime.strptime(snap["date"], "%Y-%m-%d").date()
            if snap_date < cutoff:
                to_delete_dates.append(snap)
        except Exception:
            continue

    if not to_delete_dates:
        return

    token = helpers["token"]()
    if not token:
        return

    try:
        import requests
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{helpers['sheet_id']}"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "sheets.properties(title,sheetId)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        title_to_id = {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in data.get("sheets", [])
        }

        delete_requests = []
        for snap in to_delete_dates:
            sid = title_to_id.get(snap["tab"])
            if sid is not None:
                delete_requests.append({"deleteSheet": {"sheetId": sid}})

        if delete_requests:
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{helpers['sheet_id']}:batchUpdate"
            requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"requests": delete_requests},
                timeout=15,
            ).raise_for_status()
            log.info(f"Pruned {len(delete_requests)} old snapshot tabs")
    except Exception as e:
        log.warning(f"Prune sheet delete failed: {e}")


# ─────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────

AUDIT_HEADERS = ["timestamp", "account", "operation", "target", "old_value", "new_value"]


def audit_write(account: str, op: str, target: str,
                old_value: Any, new_value: Any) -> bool:
    """Append a write event to the audit log tab.

    Called by phase 4's portfolio write functions before each mutation.
    Phase 2 also uses this to log restores.
    """
    helpers = _app_helpers()
    if not helpers or not helpers.get("sheet_id"):
        return False

    row = [
        datetime.now(timezone.utc).isoformat(),
        account or "",
        op or "",
        target or "",
        json.dumps(old_value, default=str) if old_value is not None else "",
        json.dumps(new_value, default=str) if new_value is not None else "",
    ]

    try:
        if not _ensure_tab_with_headers(AUDIT_TAB, AUDIT_HEADERS, helpers):
            return False
        token = helpers["token"]()
        if not token:
            return False
        return bool(helpers["append"](AUDIT_TAB, [row], token))
    except Exception as e:
        log.warning(f"Audit write failed: {e}")
        return False


def list_audit_entries(limit: int = 50) -> List[Dict]:
    """Read recent audit log entries (newest first)."""
    helpers = _app_helpers()
    if not helpers or not helpers.get("sheet_id"):
        return []
    token = helpers["token"]()
    if not token:
        return []

    try:
        import requests
        rng = requests.utils.quote(f"{AUDIT_TAB}!A:F", safe="!:")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{helpers['sheet_id']}/values/{rng}"
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=15
        )
        if resp.status_code == 400:
            return []
        resp.raise_for_status()
        values = resp.json().get("values", [])

        # Skip header
        if values and values[0] and values[0][0] == "timestamp":
            values = values[1:]

        entries = []
        for row in values[-limit:][::-1]:
            padded = (list(row) + [""] * 6)[:6]
            timestamp, account, op, target, old_value, new_value = padded
            entries.append({
                "timestamp": timestamp,
                "account": account,
                "op": op,
                "target": target,
                "old_value": old_value,
                "new_value": new_value,
            })
        return entries
    except Exception as e:
        log.warning(f"Audit list failed: {e}")
        return []


# ─────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────

def get_status() -> Dict:
    """Status summary for the restore page header."""
    helpers = _app_helpers()
    pf = _portfolio_module()
    snapshots = list_snapshots() if helpers else []

    return {
        "sheets_available": bool(helpers and helpers.get("sheet_id")),
        "portfolio_available": pf is not None,
        "store_initialized": bool(pf and pf._store_set is not None) if pf else False,
        "retention_days": SNAPSHOT_RETENTION_DAYS,
        "snapshot_count": len(snapshots),
        "latest_snapshot": snapshots[0] if snapshots else None,
        "known_accounts": KNOWN_ACCOUNTS,
    }
