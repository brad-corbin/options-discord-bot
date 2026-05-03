"""Phase 4.5 — Wheel campaign tracking.

A "wheel campaign" is a logical grouping of related option/share events for
a single ticker + sub-account. Campaigns are a READ-ONLY ROLLUP CONCEPT —
they NEVER affect cash math, holdings math, or option state. If campaign
data gets corrupted, deleting the Redis key restores the system to working
order; the underlying cash + holdings + options state stays correct.

CORE RULE (from Brad):
  Each open CSP starts its own campaign until assignment. After assignment,
  campaigns merge into the existing share holding for that ticker + sub-account.

States:
  active_csp_phase    - CSP open, no shares yet
  active_holding      - Shares owned (whether or not CCs are open)
  closed              - Shares hit zero AND no open puts

Campaigns scope per ticker + sub-account. Multiple sub-accounts → independent
campaigns even for the same ticker.

Per ticker + sub-account, there is AT MOST ONE active_holding campaign at a
time. Multiple open CSPs that haven't been assigned can each be their own
active_csp_phase campaign.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Storage helpers — late-bound through writes.py
# ─────────────────────────────────────────────────────────

def _store_get(key: str) -> Optional[str]:
    from . import writes
    return writes._store_get(key)


def _store_set(key: str, value: str) -> bool:
    from . import writes
    return writes._store_set(key, value)


def _key_campaigns(account: str) -> str:
    return f"{account}:portfolio:wheel_campaigns"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _gen_campaign_id(ticker: str, subaccount: str, opened_at: str) -> str:
    sub_slug = (subaccount or "default").lower().replace(" ", "_")
    return f"wheel_{ticker}_{sub_slug}_{opened_at}"


# ─────────────────────────────────────────────────────────
# Load / Save
# ─────────────────────────────────────────────────────────

def _load_campaigns(account: str) -> List[Dict]:
    raw = _store_get(_key_campaigns(account))
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_campaigns(account: str, campaigns: List[Dict]) -> bool:
    return _store_set(_key_campaigns(account), json.dumps(campaigns, default=str))


# ─────────────────────────────────────────────────────────
# Public read API
# ─────────────────────────────────────────────────────────

def get_campaigns(account: str) -> List[Dict]:
    """Return all campaigns for an account (active + closed)."""
    return _load_campaigns(account)


def get_active_campaigns(account: str) -> List[Dict]:
    return [c for c in _load_campaigns(account) if c.get("status") != "closed"]


def get_closed_campaigns(account: str) -> List[Dict]:
    return [c for c in _load_campaigns(account) if c.get("status") == "closed"]


def get_active_holding_campaign(account: str, ticker: str,
                                  subaccount: str) -> Optional[Dict]:
    """Return the active SHARE-HOLDING-phase campaign for ticker+sub, if any.
    There should be at most one of these per ticker+sub at any time.
    """
    for c in _load_campaigns(account):
        if (c.get("status") == "active_holding"
                and c.get("ticker") == ticker
                and c.get("subaccount") == subaccount):
            return c
    return None


def find_csp_only_campaign(account: str, ticker: str, subaccount: str,
                            opt_id: str) -> Optional[Dict]:
    """Find the CSP-phase campaign that contains a specific open CSP.

    Returns the campaign whose events list contains a csp_open with id=opt_id
    AND has not yet transitioned to active_holding.
    """
    for c in _load_campaigns(account):
        if c.get("status") != "active_csp_phase":
            continue
        if c.get("ticker") != ticker or c.get("subaccount") != subaccount:
            continue
        for ev in c.get("events", []):
            if ev.get("type") == "csp_open" and ev.get("id") == opt_id:
                return c
    return None


def find_campaign_by_id(account: str, campaign_id: str) -> Optional[Dict]:
    for c in _load_campaigns(account):
        if c.get("id") == campaign_id:
            return c
    return None


# ─────────────────────────────────────────────────────────
# Mutations — single-pass, save once at end
# ─────────────────────────────────────────────────────────

def _persist(account: str, mutator) -> Optional[Dict]:
    """Load → mutate → save, returning the affected campaign if any.

    `mutator` is a callable taking the campaign list, mutating it, and
    returning the affected campaign dict (or None).
    """
    campaigns = _load_campaigns(account)
    try:
        affected = mutator(campaigns)
    except Exception as e:
        log.warning(f"campaign mutator failed: {e}")
        return None
    if not _save_campaigns(account, campaigns):
        log.warning("save_campaigns failed (non-fatal)")
        return None
    return affected


def start_campaign(account: str, ticker: str, subaccount: str,
                   initial_event: Dict) -> Optional[Dict]:
    """Create a new campaign in active_csp_phase with one initial event."""
    today = _today_iso()
    cid = _gen_campaign_id(ticker, subaccount, today)

    def _mutate(campaigns):
        # Avoid id collision if same ticker+sub started today multiple times
        existing_ids = {c.get("id") for c in campaigns}
        unique_id = cid
        suffix = 1
        while unique_id in existing_ids:
            suffix += 1
            unique_id = f"{cid}_{suffix}"

        new = {
            "id": unique_id,
            "ticker": ticker,
            "subaccount": subaccount,
            "account": account,
            "status": "active_csp_phase",
            "opened_at": today,
            "closed_at": None,
            "events": [initial_event],
            "rollup": {},
        }
        new["rollup"] = _compute_rollup(new)
        campaigns.append(new)
        return new

    return _persist(account, _mutate)


def attach_event(account: str, campaign_id: str, event: Dict) -> Optional[Dict]:
    def _mutate(campaigns):
        for c in campaigns:
            if c.get("id") == campaign_id:
                c.setdefault("events", []).append(event)
                c["rollup"] = _compute_rollup(c)
                return c
        return None
    return _persist(account, _mutate)


def transition_to_holding(account: str, campaign_id: str) -> Optional[Dict]:
    """Mark a CSP-phase campaign as active_holding (after assignment)."""
    def _mutate(campaigns):
        for c in campaigns:
            if c.get("id") == campaign_id:
                c["status"] = "active_holding"
                return c
        return None
    return _persist(account, _mutate)


def merge_csp_into_holding(account: str, csp_campaign_id: str,
                            holding_campaign_id: str) -> Optional[Dict]:
    """Merge a CSP-phase campaign INTO an existing active_holding campaign.

    All events from the CSP campaign get appended to the holding campaign.
    The CSP campaign is removed.
    """
    def _mutate(campaigns):
        csp = next((c for c in campaigns if c.get("id") == csp_campaign_id), None)
        holding = next((c for c in campaigns if c.get("id") == holding_campaign_id), None)
        if not csp or not holding:
            return None
        holding.setdefault("events", []).extend(csp.get("events", []))
        # Mark merged source for traceability
        holding.setdefault("merged_in", []).append({
            "from_id": csp_campaign_id,
            "merged_at": _now_iso(),
            "from_opened_at": csp.get("opened_at"),
        })
        # Remove the CSP campaign
        campaigns[:] = [c for c in campaigns if c.get("id") != csp_campaign_id]
        holding["rollup"] = _compute_rollup(holding)
        return holding
    return _persist(account, _mutate)


def close_campaign(account: str, campaign_id: str) -> Optional[Dict]:
    """Mark a campaign closed."""
    def _mutate(campaigns):
        for c in campaigns:
            if c.get("id") == campaign_id:
                c["status"] = "closed"
                c["closed_at"] = _today_iso()
                c["rollup"] = _compute_rollup(c)
                return c
        return None
    return _persist(account, _mutate)


def remove_event_from_campaign(account: str, campaign_id: str,
                                  event_filter: Dict) -> Optional[Dict]:
    """Remove the most recent event matching `event_filter` (key/value subset).

    Used when undoing actions. Recomputes rollup. Does NOT delete a campaign
    that becomes empty — that's intentional, the underlying state may still
    show evidence of past activity.
    """
    def _mutate(campaigns):
        for c in campaigns:
            if c.get("id") != campaign_id:
                continue
            events = c.get("events", [])
            # Find LAST matching event
            for i in range(len(events) - 1, -1, -1):
                ev = events[i]
                if all(ev.get(k) == v for k, v in event_filter.items()):
                    events.pop(i)
                    c["rollup"] = _compute_rollup(c)
                    return c
            return None
        return None
    return _persist(account, _mutate)


def remove_campaign(account: str, campaign_id: str) -> bool:
    """Hard-delete a campaign by id. Used for undo of campaign starts."""
    def _mutate(campaigns):
        before = len(campaigns)
        campaigns[:] = [c for c in campaigns if c.get("id") != campaign_id]
        return {"removed": before - len(campaigns)}
    r = _persist(account, _mutate)
    return bool(r and r.get("removed", 0) > 0)


# ─────────────────────────────────────────────────────────
# Rollup calculation
# ─────────────────────────────────────────────────────────

def _compute_rollup(campaign: Dict) -> Dict:
    """Recompute totals from the events list.

    Notes on accounting (per Brad's clarified approach):
      - Premiums are income at collection. Sum is total_premium.
      - Share P&L = (sell strike − cost basis) × shares, recognized at sell.
      - Effective basis = avg cost basis − total_premium / current_shares
        (display-only hint).
    """
    events = campaign.get("events", [])

    total_premium = 0.0
    total_share_pnl = 0.0
    shares_held = 0
    cost_basis_total = 0.0  # sum of (shares * basis) for current holdings (weighted avg)
    csp_open_count = 0
    csp_closed_count = 0
    cc_open_count = 0
    cc_closed_count = 0

    for ev in events:
        t = ev.get("type")

        # ─── CSP events ───
        if t == "csp_open":
            premium = float(ev.get("premium") or 0)
            contracts = int(ev.get("contracts") or 1)
            total_premium += premium * contracts * 100
            csp_open_count += 1
        elif t in ("csp_closed", "csp_expired"):
            close_premium = float(ev.get("close_premium") or 0)
            contracts = int(ev.get("contracts") or 1)
            total_premium -= close_premium * contracts * 100
            csp_closed_count += 1
        elif t == "csp_assigned":
            csp_closed_count += 1
            # Acquired shares add to holdings at strike
            shares_acquired = int(ev.get("shares_acquired") or 0)
            strike = float(ev.get("strike") or 0)
            new_total = shares_held + shares_acquired
            if new_total > 0:
                cost_basis_total = cost_basis_total + (shares_acquired * strike)
                shares_held = new_total
        elif t == "csp_rolled":
            # Roll: net premium = new − old, contracts same
            net_credit = float(ev.get("net_credit") or 0)
            total_premium += net_credit
            # Roll keeps the campaign open — don't increment closed_count
            # (rolled CSPs replace the previous one)

        # ─── CC events ───
        elif t == "cc_open":
            premium = float(ev.get("premium") or 0)
            contracts = int(ev.get("contracts") or 1)
            total_premium += premium * contracts * 100
            cc_open_count += 1
        elif t in ("cc_closed", "cc_expired"):
            close_premium = float(ev.get("close_premium") or 0)
            contracts = int(ev.get("contracts") or 1)
            total_premium -= close_premium * contracts * 100
            cc_closed_count += 1
        elif t == "cc_called_away":
            cc_closed_count += 1
            shares_sold = int(ev.get("shares_sold") or 0)
            sell_price = float(ev.get("sell_price") or 0)
            # Compute weighted basis at the time of the sell
            avg_basis = (cost_basis_total / shares_held) if shares_held > 0 else 0
            pnl = (sell_price - avg_basis) * shares_sold
            total_share_pnl += pnl
            # Reduce holdings by the sold shares (proportional cost basis reduction)
            shares_held = max(0, shares_held - shares_sold)
            if shares_held == 0:
                cost_basis_total = 0
            else:
                cost_basis_total = avg_basis * shares_held
        elif t == "cc_rolled":
            net_credit = float(ev.get("net_credit") or 0)
            total_premium += net_credit

        # ─── Manual share events ───
        elif t == "manual_share_add":
            sh = float(ev.get("shares") or 0)
            cb = float(ev.get("cost_basis") or 0)
            cost_basis_total += sh * cb
            shares_held += int(sh) if sh == int(sh) else int(sh)  # keep int when whole
            shares_held = int(shares_held) if shares_held == int(shares_held) else shares_held
            # Recompute (avoid floating drift)
            shares_held = int(round(shares_held))
        elif t == "manual_share_sell":
            sh = float(ev.get("shares") or 0)
            sp = float(ev.get("sell_price") or 0)
            avg_basis = (cost_basis_total / shares_held) if shares_held > 0 else 0
            total_share_pnl += (sp - avg_basis) * sh
            shares_held = max(0, shares_held - int(round(sh)))
            if shares_held == 0:
                cost_basis_total = 0
            else:
                cost_basis_total = avg_basis * shares_held

    weighted_basis = (cost_basis_total / shares_held) if shares_held > 0 else 0.0
    effective_basis = (weighted_basis - (total_premium / shares_held)) if shares_held > 0 else 0.0

    # Duration
    opened = campaign.get("opened_at")
    closed = campaign.get("closed_at") or _today_iso()
    duration = 0
    try:
        d_open = datetime.strptime(opened, "%Y-%m-%d")
        d_close = datetime.strptime(closed, "%Y-%m-%d")
        duration = (d_close - d_open).days
    except Exception:
        pass

    return {
        "total_premium": round(total_premium, 2),
        "total_share_pnl": round(total_share_pnl, 2),
        "total_pnl": round(total_premium + total_share_pnl, 2),
        "shares_held": int(shares_held),
        "weighted_cost_basis": round(weighted_basis, 4),
        "effective_basis": round(effective_basis, 4),
        "duration_days": duration,
        "csp_open_count": csp_open_count,
        "csp_closed_count": csp_closed_count,
        "cc_open_count": cc_open_count,
        "cc_closed_count": cc_closed_count,
    }


def recompute_all_rollups(account: str) -> int:
    """Recompute rollups for every campaign in the account. Returns count."""
    def _mutate(campaigns):
        for c in campaigns:
            c["rollup"] = _compute_rollup(c)
        return {"recomputed": len(campaigns)}
    r = _persist(account, _mutate)
    return (r or {}).get("recomputed", 0)


# ─────────────────────────────────────────────────────────
# High-level event hooks — called from writes.py
# These are best-effort: any exception here MUST NOT break the underlying
# write operation.
# ─────────────────────────────────────────────────────────

def hook_option_added(account: str, opt: Dict) -> None:
    """Called after add_option succeeds."""
    try:
        ticker = opt.get("ticker")
        opt_type = opt.get("type")
        sub = opt.get("subaccount") or ""
        direction = opt.get("direction", "sell")

        # Only track wheel options (CSP/CC, sell-side)
        if opt_type not in ("CSP", "CC") or direction != "sell":
            return

        if opt_type == "CSP":
            # Find existing active_holding campaign for ticker+sub
            holding = get_active_holding_campaign(account, ticker, sub)
            event = {
                "type": "csp_open",
                "id": opt.get("id"),
                "premium": opt.get("premium"),
                "contracts": opt.get("contracts", 1),
                "strike": opt.get("strike"),
                "exp": opt.get("exp"),
                "open_date": opt.get("open_date"),
            }
            if holding:
                # Attach to the existing holding-phase campaign
                attach_event(account, holding["id"], event)
            else:
                # Start a new CSP-phase campaign
                start_campaign(account, ticker, sub, event)

        elif opt_type == "CC":
            # CCs always attach to the active holding campaign
            holding = get_active_holding_campaign(account, ticker, sub)
            if not holding:
                # CC without shares is unusual — skip campaign tracking
                # (could be a naked call, not a wheel)
                return
            event = {
                "type": "cc_open",
                "id": opt.get("id"),
                "premium": opt.get("premium"),
                "contracts": opt.get("contracts", 1),
                "strike": opt.get("strike"),
                "exp": opt.get("exp"),
                "open_date": opt.get("open_date"),
            }
            attach_event(account, holding["id"], event)
    except Exception as e:
        log.warning(f"hook_option_added failed (non-fatal): {e}")


def hook_option_closed(account: str, opt: Dict, status: str,
                        close_premium: float, close_date: str,
                        auto_handled_shares: bool = False,
                        shares_acquired: int = 0,
                        shares_sold: int = 0,
                        actual_fill_price: float = None) -> None:
    """Called after close_option succeeds.

    For assignments with auto-handled shares, this is responsible for either:
      - Transitioning a CSP-phase campaign to active_holding (no existing
        holding campaign for ticker+sub)
      - Merging a CSP-phase campaign into an existing active_holding campaign
    """
    try:
        ticker = opt.get("ticker")
        opt_type = opt.get("type")
        sub = opt.get("subaccount") or ""
        opt_id = opt.get("id")
        contracts = int(opt.get("contracts") or 1)
        strike = float(opt.get("strike") or 0)
        direction = opt.get("direction", "sell")

        if opt_type not in ("CSP", "CC") or direction != "sell":
            return

        if opt_type == "CSP":
            # Find which campaign holds this CSP
            csp_campaign = find_csp_only_campaign(account, ticker, sub, opt_id)
            holding = get_active_holding_campaign(account, ticker, sub)
            # The CSP could also have been attached to a holding campaign
            # if it was opened while shares were already held.
            home_campaign = csp_campaign or holding

            if status == "expired":
                if home_campaign:
                    attach_event(account, home_campaign["id"], {
                        "type": "csp_expired",
                        "id": opt_id,
                        "contracts": contracts,
                        "strike": strike,
                        "close_premium": close_premium,
                        "date": close_date,
                    })
                    _maybe_close_campaign(account, home_campaign["id"])
            elif status == "closed":
                if home_campaign:
                    attach_event(account, home_campaign["id"], {
                        "type": "csp_closed",
                        "id": opt_id,
                        "contracts": contracts,
                        "strike": strike,
                        "close_premium": close_premium,
                        "date": close_date,
                    })
                    _maybe_close_campaign(account, home_campaign["id"])
            elif status == "assigned":
                if not auto_handled_shares:
                    # Legacy behavior — just record the event, don't change shares
                    if home_campaign:
                        attach_event(account, home_campaign["id"], {
                            "type": "csp_assigned",
                            "id": opt_id,
                            "contracts": contracts,
                            "strike": strike,
                            "shares_acquired": 0,  # legacy: no auto-shares
                            "actual_fill_price": actual_fill_price,
                            "auto_handled": False,
                            "date": close_date,
                        })
                    return

                # Auto-handled: shares were created. Wire up the campaign:
                event = {
                    "type": "csp_assigned",
                    "id": opt_id,
                    "contracts": contracts,
                    "strike": strike,
                    "shares_acquired": shares_acquired,
                    "actual_fill_price": actual_fill_price,
                    "auto_handled": True,
                    "date": close_date,
                }

                if csp_campaign and holding:
                    # CSP was its own campaign, but a holding campaign exists.
                    # Merge: CSP gets assigned event, then merges into holding.
                    attach_event(account, csp_campaign["id"], event)
                    merge_csp_into_holding(account, csp_campaign["id"], holding["id"])
                elif csp_campaign and not holding:
                    # CSP was its own campaign, transitions to holding.
                    attach_event(account, csp_campaign["id"], event)
                    transition_to_holding(account, csp_campaign["id"])
                elif holding and not csp_campaign:
                    # CSP was attached to existing holding campaign all along.
                    attach_event(account, holding["id"], event)
                else:
                    # No campaign anywhere — start a fresh one in holding state.
                    new_camp = start_campaign(account, ticker, sub, event)
                    if new_camp:
                        transition_to_holding(account, new_camp["id"])

        elif opt_type == "CC":
            holding = get_active_holding_campaign(account, ticker, sub)
            if not holding:
                return  # untracked CC

            if status == "expired":
                attach_event(account, holding["id"], {
                    "type": "cc_expired",
                    "id": opt_id,
                    "contracts": contracts,
                    "strike": strike,
                    "close_premium": close_premium,
                    "date": close_date,
                })
            elif status == "closed":
                attach_event(account, holding["id"], {
                    "type": "cc_closed",
                    "id": opt_id,
                    "contracts": contracts,
                    "strike": strike,
                    "close_premium": close_premium,
                    "date": close_date,
                })
            elif status == "assigned":
                if not auto_handled_shares:
                    attach_event(account, holding["id"], {
                        "type": "cc_called_away",
                        "id": opt_id,
                        "contracts": contracts,
                        "strike": strike,
                        "shares_sold": 0,
                        "auto_handled": False,
                        "date": close_date,
                    })
                    return

                attach_event(account, holding["id"], {
                    "type": "cc_called_away",
                    "id": opt_id,
                    "contracts": contracts,
                    "strike": strike,
                    "shares_sold": shares_sold,
                    "sell_price": actual_fill_price if actual_fill_price else strike,
                    "auto_handled": True,
                    "date": close_date,
                })
                _maybe_close_campaign(account, holding["id"])
    except Exception as e:
        log.warning(f"hook_option_closed failed (non-fatal): {e}")


def hook_holding_added(account: str, ticker: str, shares: float,
                        cost_basis: float, subaccount: str,
                        is_assignment: bool = False) -> None:
    """Called after add_holding succeeds.

    is_assignment=True: this came from auto-handle on a CSP. Don't double-add
    the event — the close_option hook already records csp_assigned with shares.
    is_assignment=False: regular manual share add. Attach to active campaign
    if one exists, otherwise do nothing (manual share holdings might not be
    wheel positions).
    """
    if is_assignment:
        return
    try:
        holding = get_active_holding_campaign(account, ticker, subaccount)
        if not holding:
            return
        attach_event(account, holding["id"], {
            "type": "manual_share_add",
            "ticker": ticker,
            "shares": shares,
            "cost_basis": cost_basis,
            "date": _today_iso(),
        })
    except Exception as e:
        log.warning(f"hook_holding_added failed (non-fatal): {e}")


def hook_holding_sold(account: str, ticker: str, shares: float,
                       sell_price: float, subaccount: str,
                       is_call_away: bool = False) -> None:
    """Called after sell_holding succeeds.

    is_call_away=True: this came from auto-handle on CC. Already recorded
    by close_option hook; skip.
    """
    if is_call_away:
        return
    try:
        holding = get_active_holding_campaign(account, ticker, subaccount)
        if not holding:
            return
        attach_event(account, holding["id"], {
            "type": "manual_share_sell",
            "ticker": ticker,
            "shares": shares,
            "sell_price": sell_price,
            "date": _today_iso(),
        })
        _maybe_close_campaign(account, holding["id"])
    except Exception as e:
        log.warning(f"hook_holding_sold failed (non-fatal): {e}")


def hook_option_rolled(account: str, old_opt: Dict, new_opt: Dict,
                        net_credit: float, roll_date: str) -> None:
    """Called after roll_option succeeds. Both old + new attach to same
    campaign as a single roll event."""
    try:
        ticker = old_opt.get("ticker")
        opt_type = old_opt.get("type")
        sub = old_opt.get("subaccount") or ""
        old_id = old_opt.get("id")
        new_id = new_opt.get("id")

        # Find the campaign hosting the old option
        home_campaign = None
        if opt_type == "CSP":
            home_campaign = find_csp_only_campaign(account, ticker, sub, old_id)
            if not home_campaign:
                home_campaign = get_active_holding_campaign(account, ticker, sub)
        elif opt_type == "CC":
            home_campaign = get_active_holding_campaign(account, ticker, sub)

        if not home_campaign:
            return

        ev_type = "csp_rolled" if opt_type == "CSP" else "cc_rolled"
        attach_event(account, home_campaign["id"], {
            "type": ev_type,
            "old_id": old_id,
            "new_id": new_id,
            "net_credit": net_credit,
            "new_strike": new_opt.get("strike"),
            "new_exp": new_opt.get("exp"),
            "date": roll_date,
        })
    except Exception as e:
        log.warning(f"hook_option_rolled failed (non-fatal): {e}")


# ─────────────────────────────────────────────────────────
# Campaign close logic
# ─────────────────────────────────────────────────────────

def _maybe_close_campaign(account: str, campaign_id: str) -> None:
    """Close the campaign if shares=0 AND no open puts/calls remain.

    Open puts/calls = events with csp_open or cc_open with NO matching
    closed/expired/assigned/rolled event for the same id.
    """
    try:
        camp = find_campaign_by_id(account, campaign_id)
        if not camp:
            return
        if camp.get("status") == "closed":
            return

        rollup = camp.get("rollup", {}) or _compute_rollup(camp)
        shares_held = rollup.get("shares_held", 0)
        if shares_held > 0:
            return

        # Build map of opens vs closes
        events = camp.get("events", [])
        opens = set()
        closes = set()
        for ev in events:
            t = ev.get("type")
            if t in ("csp_open", "cc_open"):
                opens.add(ev.get("id"))
            elif t in ("csp_closed", "csp_expired", "csp_assigned",
                        "cc_closed", "cc_expired", "cc_called_away"):
                closes.add(ev.get("id"))
            elif t in ("csp_rolled",):
                # Old id is closed by the roll
                closes.add(ev.get("old_id"))
            elif t == "cc_rolled":
                closes.add(ev.get("old_id"))

        outstanding = opens - closes
        if outstanding:
            return  # still have open positions

        close_campaign(account, campaign_id)
    except Exception as e:
        log.warning(f"_maybe_close_campaign failed: {e}")


# ─────────────────────────────────────────────────────────
# Wipe handler
# ─────────────────────────────────────────────────────────

def wipe_campaigns(account: str) -> bool:
    """Clear campaign data for an account (called from wipe_account)."""
    return _store_set(_key_campaigns(account), json.dumps([]))
