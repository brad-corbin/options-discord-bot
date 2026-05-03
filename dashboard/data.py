"""Phase 3 — Read-only data layer.

Reads from the bot's existing modules (portfolio, thesis_monitor, active_scanner,
api_cache) via late-binding imports. No writes. No new computations beyond
simple aggregations on top of what the bot already computes.

Where data isn't available (e.g. starting balances before Phase 4 backfill),
returns None or an empty list — never fakes a number.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

log = logging.getLogger(__name__)

# UI-account → underlying portfolio account keys
# Same mapping as durability. Phase 4 will refine.
UI_TO_PORTFOLIO = {
    "mine":     ["brad"],
    "mom":      ["mom"],
    "partner":  [],   # added in Phase 4
    "kyleigh":  [],   # added in Phase 4
    "combined": ["brad", "mom"],
}


# ─────────────────────────────────────────────────────────
# Late-bound imports — avoid circular at module load time
# ─────────────────────────────────────────────────────────

def _portfolio():
    try:
        import portfolio
        return portfolio
    except Exception as e:
        log.debug(f"portfolio unavailable: {e}")
        return None


def _app_module():
    try:
        import app as _app
        return _app
    except Exception as e:
        log.debug(f"app unavailable: {e}")
        return None


def _thesis_monitor_instance():
    """Get the live thesis monitor singleton from app.py if running."""
    try:
        from app import _thesis_engine  # type: ignore
        return _thesis_engine
    except Exception:
        try:
            from app import _thesis_monitor_engine  # type: ignore
            return _thesis_monitor_engine
        except Exception:
            return None


def _scanner_instance():
    """Get the active scanner singleton from app.py if running."""
    try:
        from app import _scanner  # type: ignore
        return _scanner
    except Exception:
        return None


def _cached_md():
    """Get the cached market data wrapper from app.py."""
    try:
        from app import _cached_md as cmd  # type: ignore
        return cmd
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# Spot price cache (bounded, short TTL)
# Avoid hammering the API on every page load.
# ─────────────────────────────────────────────────────────

_spot_cache: Dict[str, tuple] = {}  # ticker -> (price, fetched_at)
_SPOT_TTL = 30  # seconds


def get_spot_cached(ticker: str) -> Optional[float]:
    if not ticker:
        return None
    ticker = ticker.upper()
    now = time.time()
    cached = _spot_cache.get(ticker)
    if cached and (now - cached[1]) < _SPOT_TTL:
        return cached[0]

    # Try cached_md first (already-cached spot)
    cmd = _cached_md()
    price = None
    if cmd:
        try:
            price = cmd.get_spot(ticker, as_float_fn=lambda x, d=None: float(x) if x not in (None, "") else d)
        except Exception:
            price = None

    if price is None:
        return None
    _spot_cache[ticker] = (float(price), now)
    return float(price)


def get_spots_for(tickers: List[str]) -> Dict[str, Optional[float]]:
    """Batch-fetch spot prices, using the per-ticker TTL cache."""
    out = {}
    for t in tickers:
        out[t] = get_spot_cached(t)
    return out


# ─────────────────────────────────────────────────────────
# Account → portfolio mapping
# ─────────────────────────────────────────────────────────

def underlying_accounts(ui_account: str) -> List[str]:
    """Get the list of underlying portfolio keys for a UI account."""
    return UI_TO_PORTFOLIO.get(ui_account, [])


def portfolio_data_available(ui_account: str) -> bool:
    return bool(underlying_accounts(ui_account)) and _portfolio() is not None


# ─────────────────────────────────────────────────────────
# Income calculation — month + year
# ─────────────────────────────────────────────────────────

def _option_close_month(opt: Dict) -> Optional[str]:
    """Return YYYY-MM for the close event of an option, or None if open/unknown."""
    if not isinstance(opt, dict):
        return None
    status = opt.get("status")
    if status not in ("closed", "expired", "assigned", "rolled"):
        return None
    close_date = opt.get("close_date") or opt.get("exp")
    if not close_date:
        return None
    try:
        # Try ISO format first
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(close_date.split("+")[0].split(".")[0], fmt)
                return dt.strftime("%Y-%m")
            except Exception:
                continue
    except Exception:
        return None
    return None


def calc_income_breakdown(ui_account: str) -> Dict:
    """Compute monthly + yearly realized option income.

    Income model from the spec: gross premium ledger.
    - sell-to-open contributes +premium when opened (in open month)
    - buy-to-close contributes net (premium - close_premium) when closed
    - expiration / assignment: full premium counts in open month (already counted)

    For Phase 3 simplicity, we count realized P/L on closed contracts in
    the close month. Open contracts are not yet counted toward income —
    Phase 4 will add proper monthly ledger tracking with the explicit
    income events the spec described.
    """
    pf = _portfolio()
    if not pf:
        return {"available": False, "month": 0.0, "year": 0.0, "by_month": {}}

    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    by_month: Dict[str, float] = {}
    total_year = 0.0
    total_month = 0.0

    for acc in underlying_accounts(ui_account):
        try:
            options = pf.get_all_options(account=acc) or []
        except Exception:
            options = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            month_key = _option_close_month(opt)
            if not month_key:
                continue
            try:
                pnl = pf.calc_option_pnl(opt) or 0.0
            except Exception:
                pnl = 0.0
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


def calc_goal_pace(income_breakdown: Dict) -> Dict:
    """Compute monthly goal as average of completed-month ROCs × current month start.

    Phase 3 limitation: we don't yet have starting balances per month, so we
    can't compute a true ROC-based goal. Returns the average completed-month
    income as a placeholder until Phase 4 backfills balances.
    """
    if not income_breakdown.get("available"):
        return {"available": False}

    by_month = income_breakdown.get("by_month") or {}
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    # Completed months in current year
    completed = [
        v for k, v in by_month.items()
        if k.startswith(current_year) and k < current_month
    ]

    if not completed:
        return {
            "available": True,
            "complete": False,
            "reason": "no completed months yet — goal benchmark builds from February onward",
        }

    avg_completed = sum(completed) / len(completed)
    month_actual = by_month.get(current_month, 0.0)
    pct = (month_actual / avg_completed * 100.0) if avg_completed > 0 else 0.0

    return {
        "available": True,
        "complete": True,
        "goal": round(avg_completed, 2),
        "actual": round(month_actual, 2),
        "pct": min(round(pct, 1), 999.9),
        "completed_months": len(completed),
    }


# ─────────────────────────────────────────────────────────
# Open positions
# ─────────────────────────────────────────────────────────

def get_open_positions(ui_account: str) -> Dict[str, List[Dict]]:
    """Open positions grouped by type: wheel, spreads, intraday."""
    pf = _portfolio()
    accounts = underlying_accounts(ui_account)
    if not pf or not accounts:
        return {"wheel": [], "spreads": [], "intraday": [], "shares": []}

    wheel_options: List[Dict] = []
    shares: List[Dict] = []
    spreads: List[Dict] = []
    intraday: List[Dict] = []  # phase 3.5: tied to thesis_monitor active_trades

    for acc in accounts:
        try:
            opts = pf.get_open_options(account=acc) or []
            for o in opts:
                if not isinstance(o, dict):
                    continue
                row = {
                    "id": o.get("id"),
                    "ticker": (o.get("ticker") or "").upper(),
                    "type": (o.get("type") or "").upper(),
                    "strike": o.get("strike"),
                    "exp": o.get("exp"),
                    "premium": o.get("premium"),
                    "contracts": o.get("contracts", 1),
                    "direction": o.get("direction", "sell"),
                    "open_date": o.get("open_date"),
                    "tag": o.get("tag"),
                    "account": acc,
                }
                wheel_options.append(row)
        except Exception as e:
            log.debug(f"options fetch failed for {acc}: {e}")

        try:
            holdings = pf.get_all_holdings(account=acc) or {}
            for ticker, h in holdings.items():
                if not isinstance(h, dict):
                    continue
                shares.append({
                    "ticker": ticker.upper(),
                    "shares": h.get("shares"),
                    "cost_basis": h.get("cost_basis"),
                    "tag": h.get("tag"),
                    "account": acc,
                })
        except Exception as e:
            log.debug(f"holdings fetch failed for {acc}: {e}")

        try:
            spr = pf.get_open_spreads(account=acc) or []
            for s in spr:
                if not isinstance(s, dict):
                    continue
                spreads.append({
                    "id": s.get("id"),
                    "ticker": (s.get("ticker") or "").upper(),
                    "type": (s.get("type") or "").upper(),
                    "long": s.get("long_strike"),
                    "short": s.get("short_strike"),
                    "exp": s.get("exp"),
                    "debit": s.get("debit"),
                    "credit": s.get("credit"),
                    "contracts": s.get("contracts", 1),
                    "open_date": s.get("open_date"),
                    "account": acc,
                })
        except Exception as e:
            log.debug(f"spreads fetch failed for {acc}: {e}")

    # Pull active intraday trades from thesis monitor (in-memory state)
    tm = _thesis_monitor_instance()
    if tm:
        try:
            tickers = tm.get_monitored_tickers() if hasattr(tm, "get_monitored_tickers") else []
            for t in tickers:
                state = tm.get_state(t) if hasattr(tm, "get_state") else None
                if not state or not getattr(state, "active_trades", None):
                    continue
                for at in state.active_trades:
                    status = getattr(at, "status", None)
                    if status not in ("OPEN", "SCALED", "TRAILED"):
                        continue
                    intraday.append({
                        "ticker": getattr(at, "ticker", t),
                        "direction": getattr(at, "direction", ""),
                        "entry_type": getattr(at, "entry_type", ""),
                        "entry_price": getattr(at, "entry_price", None),
                        "stop_level": getattr(at, "stop_level", None),
                        "trade_id": getattr(at, "trade_id", ""),
                        "status": status,
                    })
        except Exception as e:
            log.debug(f"intraday trades fetch failed: {e}")

    return {
        "wheel_options": wheel_options,
        "shares": shares,
        "spreads": spreads,
        "intraday": intraday,
    }


def annotate_with_pnl(positions: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    """Add live-price-based P/L to open positions where possible."""
    tickers = set()
    for row in positions.get("shares", []):
        if row.get("ticker"):
            tickers.add(row["ticker"])
    for row in positions.get("intraday", []):
        if row.get("ticker"):
            tickers.add(row["ticker"])

    spots = get_spots_for(list(tickers))

    for row in positions.get("shares", []):
        spot = spots.get(row["ticker"])
        if spot is None or row.get("cost_basis") is None or row.get("shares") is None:
            row["spot"] = None
            row["pnl_dollars"] = None
            row["pnl_pct"] = None
            continue
        try:
            cb = float(row["cost_basis"])
            sh = float(row["shares"])
            pnl_dollars = round((spot - cb) * sh, 2)
            pnl_pct = round(((spot - cb) / cb * 100.0), 2) if cb > 0 else None
            row["spot"] = round(spot, 2)
            row["pnl_dollars"] = pnl_dollars
            row["pnl_pct"] = pnl_pct
        except Exception:
            row["spot"] = None
            row["pnl_dollars"] = None
            row["pnl_pct"] = None

    for row in positions.get("intraday", []):
        spot = spots.get(row["ticker"])
        row["spot"] = round(spot, 2) if spot else None
        # Direction-aware P/L on the underlying
        try:
            ep = float(row.get("entry_price") or 0)
            if spot and ep:
                if row.get("direction") == "LONG":
                    row["underlying_pnl"] = round(spot - ep, 2)
                elif row.get("direction") == "SHORT":
                    row["underlying_pnl"] = round(ep - spot, 2)
                else:
                    row["underlying_pnl"] = None
            else:
                row["underlying_pnl"] = None
        except Exception:
            row["underlying_pnl"] = None

    return positions


# ─────────────────────────────────────────────────────────
# System status — regime, scanner, API credits, circuit breaker
# ─────────────────────────────────────────────────────────

def get_scanner_status() -> Dict:
    sc = _scanner_instance()
    if not sc:
        return {"available": False}
    try:
        s = sc.status if hasattr(sc, "status") else {}
        return {
            "available": True,
            "running": bool(s.get("running")),
            "watchlist_size": s.get("watchlist_size"),
            "tickers_scanned": s.get("tickers_scanned"),
            "signals_generated": s.get("signals_generated"),
            "tier_a": s.get("tier_a") or [],
            "tier_b": s.get("tier_b") or [],
            "tier_c": s.get("tier_c") or [],
            "active_tickers": s.get("active_tickers") or [],
        }
    except Exception as e:
        log.debug(f"scanner status failed: {e}")
        return {"available": False}


def get_regime() -> Dict:
    """VIX + ADX regime label."""
    sc = _scanner_instance()
    if sc:
        try:
            s = sc.status
            mr = s.get("market_regime") or {}
            if isinstance(mr, dict) and mr:
                return {
                    "available": True,
                    "label": mr.get("label") or mr.get("regime") or "UNKNOWN",
                    "vix": mr.get("vix"),
                    "adx": mr.get("adx"),
                    "vix_regime": mr.get("vix_regime"),
                    "adx_regime": mr.get("adx_regime"),
                }
        except Exception as e:
            log.debug(f"regime read failed: {e}")
    return {"available": False, "label": "UNKNOWN"}


def get_api_credits() -> Dict:
    cmd = _cached_md()
    if not cmd:
        return {"available": False}
    try:
        if hasattr(cmd, "get_api_status"):
            s = cmd.get_api_status() or {}
            return {
                "available": True,
                "credits": s.get("credits", 0),
                "calls": s.get("calls", 0),
                "budget": s.get("budget", 100000),
                "pct_used": s.get("pct_used", 0),
            }
    except Exception as e:
        log.debug(f"api credits read failed: {e}")
    return {"available": False}


# ─────────────────────────────────────────────────────────
# Watch map data — for Trading view
# ─────────────────────────────────────────────────────────

def get_watchmap_for_ticker(ticker: str) -> Optional[Dict]:
    """Build a watchmap card data dict for a single ticker.

    Reads ThesisContext + MonitorState from the live engine, computes above/below
    levels using options_map.build_watch_levels and triggers from
    build_watch_triggers. Returns a stub card with just spot if no thesis exists
    (so user-pulled tickers without thesis data still show up rather than
    silently disappearing).
    """
    if not ticker:
        return None
    ticker = ticker.upper()
    spot = get_spot_cached(ticker)

    tm = _thesis_monitor_instance()
    thesis = None
    if tm and hasattr(tm, "get_thesis"):
        try:
            thesis = tm.get_thesis(ticker)
        except Exception:
            thesis = None

    # Stub card for tickers without thesis data — user pulled but bot hasn't
    # computed levels yet. Show what we can.
    if not thesis:
        if not spot:
            return None
        return {
            "ticker": ticker,
            "spot": round(spot, 2),
            "bias": "NEUTRAL",
            "bias_score": 0,
            "gex_sign": None,
            "regime": None,
            "vix": None,
            "above": [],
            "below": [],
            "triggers": ["No thesis data yet — appears once the next EM card pass computes levels"],
            "active_trade": None,
            "stub": True,
        }

    state = tm.get_state(ticker) if hasattr(tm, "get_state") else None

    # Compose level structures expected by build_watch_levels
    levels = thesis.levels
    structure = {
        "gamma_flip": levels.gamma_flip,
        "call_wall": levels.call_wall,
        "put_wall": levels.put_wall,
        "max_pain": levels.max_pain,
    }
    em = {
        "bull_1sd": levels.em_high,
        "bear_1sd": levels.em_low,
    }

    direction = "bullish" if thesis.bias_score >= 2 else ("bearish" if thesis.bias_score <= -2 else "neutral")
    targets = {"direction": direction}

    above: List[tuple] = []
    below: List[tuple] = []
    triggers: List[str] = []

    try:
        from options_map import build_watch_levels, build_watch_triggers
        if spot:
            wlvl = build_watch_levels(spot, em, structure, targets, bias_score=thesis.bias_score) or {}
            above = wlvl.get("above") or []
            below = wlvl.get("below") or []
            triggers = build_watch_triggers(spot, structure, targets, wlvl, bias_score=thesis.bias_score) or []
    except Exception as e:
        log.debug(f"watchmap level build failed for {ticker}: {e}")

    active_trade = None
    if state and getattr(state, "active_trades", None):
        for at in state.active_trades:
            if getattr(at, "status", None) in ("OPEN", "SCALED", "TRAILED"):
                active_trade = {
                    "direction": getattr(at, "direction", ""),
                    "entry_price": getattr(at, "entry_price", None),
                    "stop_level": getattr(at, "stop_level", None),
                    "trade_id": getattr(at, "trade_id", ""),
                }
                break

    return {
        "ticker": ticker,
        "spot": round(spot, 2) if spot else None,
        "bias": thesis.bias,
        "bias_score": thesis.bias_score,
        "gex_sign": thesis.gex_sign,
        "regime": thesis.regime,
        "vix": thesis.vix,
        "above": [
            {"price": v, "label": l, "dist": round((v - spot), 2) if spot else None}
            for v, l in above[:4]
        ],
        "below": [
            {"price": v, "label": l, "dist": round((spot - v), 2) if spot else None}
            for v, l in below[:4]
        ],
        "triggers": triggers[:3],
        "active_trade": active_trade,
        "stub": False,
    }


def get_watchmap_grid(extra_tickers: List[str] = None) -> List[Dict]:
    """Return watchmap data for: tier_a + active-trade tickers + extras (pulled).

    Phase 3 simple set. Pinning persistence comes in phase 5+.
    """
    extra_tickers = [t.upper() for t in (extra_tickers or [])]
    sc = _scanner_instance()
    tier_a = []
    if sc:
        try:
            tier_a = list(sc.status.get("tier_a") or [])
        except Exception:
            tier_a = []

    # Active-trade tickers
    active_tickers: List[str] = []
    tm = _thesis_monitor_instance()
    if tm and hasattr(tm, "get_monitored_tickers"):
        try:
            for t in tm.get_monitored_tickers():
                state = tm.get_state(t) if hasattr(tm, "get_state") else None
                if state and getattr(state, "active_trades", None):
                    if any(getattr(at, "status", None) in ("OPEN", "SCALED", "TRAILED")
                           for at in state.active_trades):
                        active_tickers.append(t.upper())
        except Exception:
            pass

    # Order: extras first (newest pulled), active trades, then tier_a
    seen = set()
    ordered: List[str] = []
    for t in extra_tickers + active_tickers + tier_a:
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)

    cards = []
    for t in ordered:
        card = get_watchmap_for_ticker(t)
        if card:
            card["pulled"] = (t in extra_tickers)
            cards.append(card)
    return cards


# ─────────────────────────────────────────────────────────
# Holdings sentiment (mirrors the existing /holdings digest)
# ─────────────────────────────────────────────────────────

def get_holdings_sentiment(ui_account: str) -> Dict:
    """Mirror of the existing /holdings Telegram digest.

    Reuses sentiment_report.calc_sentiment_report() if available, falling back
    to a minimal version if not.
    """
    accounts = underlying_accounts(ui_account)
    if not accounts:
        return {"available": False, "reason": "no underlying accounts"}

    pf = _portfolio()
    if not pf:
        return {"available": False, "reason": "portfolio module unavailable"}

    try:
        import sentiment_report
    except Exception:
        sentiment_report = None

    bullish: List[Dict] = []
    neutral: List[Dict] = []
    bearish: List[Dict] = []
    total_unrealized = 0.0
    total_opt_income = 0.0
    open_options_count = 0

    for acc in accounts:
        try:
            holdings = pf.get_all_holdings(account=acc) or {}
        except Exception:
            holdings = {}
        if not holdings:
            continue

        # Get current prices for these tickers
        spots = get_spots_for(list(holdings.keys()))

        for ticker, h in sorted(holdings.items()):
            spot = spots.get(ticker)
            if spot is None or not isinstance(h, dict):
                continue
            try:
                pnl = pf.calc_holding_pnl(ticker, spot, account=acc)
            except Exception:
                pnl = {}
            unrealized = pnl.get("unrealized") or 0.0
            opt_income = pnl.get("opt_income") or 0.0
            total_unrealized += unrealized
            total_opt_income += opt_income

            # Classify if sentiment_report module exists
            sentiment = "neutral"
            ema = vwap = vol = "?"
            if sentiment_report:
                try:
                    sig = sentiment_report.calc_ticker_sentiment(ticker)
                    sentiment = (sig or {}).get("sentiment", "neutral")
                    ema = (sig or {}).get("ema", "?")
                    vwap = (sig or {}).get("vwap", "?")
                    vol = (sig or {}).get("vol", "?")
                except Exception:
                    pass

            entry = {
                "ticker": ticker,
                "spot": round(spot, 2),
                "ema": ema,
                "vwap": vwap,
                "vol": vol,
                "pnl": round(unrealized + opt_income, 2),
                "pnl_pct": pnl.get("return_pct"),
                "tag": h.get("tag"),
                "account": acc,
            }
            if sentiment == "bullish":
                bullish.append(entry)
            elif sentiment == "bearish":
                bearish.append(entry)
            else:
                neutral.append(entry)

        try:
            open_options_count += len(pf.get_open_options(account=acc) or [])
        except Exception:
            pass

    return {
        "available": True,
        "bullish": bullish,
        "neutral": neutral,
        "bearish": bearish,
        "total_unrealized": round(total_unrealized, 2),
        "total_opt_income": round(total_opt_income, 2),
        "portfolio_pnl": round(total_unrealized + total_opt_income, 2),
        "open_options": open_options_count,
        "is_empty": not (bullish or neutral or bearish),
    }


# ─────────────────────────────────────────────────────────
# Recent alerts (today)
# ─────────────────────────────────────────────────────────

def get_recent_alerts(limit: int = 20) -> List[Dict]:
    """Read recent signal alerts. Phase 3 reads from in-memory dedup if available,
    otherwise returns empty (phase 6 Diagnostic will pull the full feed)."""
    sc = _scanner_instance()
    if not sc:
        return []
    try:
        # Best-effort: many scanners track _signal_dedup or a recent log
        if hasattr(sc, "_recent_signals"):
            recent = list(sc._recent_signals)[-limit:][::-1]
            return [
                {
                    "time": s.get("time") or s.get("ts") or "",
                    "ticker": (s.get("ticker") or "").upper(),
                    "type": (s.get("alert_type") or s.get("type") or "").upper(),
                    "msg": s.get("msg") or s.get("summary") or "",
                    "status": s.get("status") or "Filtered",
                }
                for s in recent
            ]
    except Exception:
        pass
    return []


# ─────────────────────────────────────────────────────────
# Aggregate page-data builders
# ─────────────────────────────────────────────────────────

def command_center_data(ui_account: str) -> Dict:
    """Everything needed to render the Command Center page."""
    pf_available = portfolio_data_available(ui_account)

    income = calc_income_breakdown(ui_account) if pf_available else {"available": False}
    goal = calc_goal_pace(income) if pf_available else {"available": False}
    positions = annotate_with_pnl(get_open_positions(ui_account)) if pf_available else {
        "wheel_options": [], "shares": [], "spreads": [], "intraday": []
    }
    sentiment = get_holdings_sentiment(ui_account) if pf_available else {"available": False}
    regime = get_regime()
    api_credits = get_api_credits()
    scanner = get_scanner_status()
    alerts = get_recent_alerts(limit=10)

    # Counts for status strip
    open_wheel = len(positions.get("wheel_options", []))
    open_spreads = len(positions.get("spreads", []))
    open_intraday = len(positions.get("intraday", []))
    open_total = open_wheel + open_spreads + open_intraday

    return {
        "ui_account": ui_account,
        "portfolio_available": pf_available,
        "income": income,
        "goal": goal,
        "positions": positions,
        "open_total": open_total,
        "sentiment": sentiment,
        "regime": regime,
        "api_credits": api_credits,
        "scanner": scanner,
        "alerts": alerts,
        "now_str": datetime.now(timezone.utc).strftime("%d %b · %H:%M:%S UTC"),
    }


def trading_data(ui_account: str, pulled_tickers: List[str] = None) -> Dict:
    """Everything needed to render the Trading view."""
    regime = get_regime()
    api_credits = get_api_credits()
    scanner = get_scanner_status()
    alerts = get_recent_alerts(limit=20)
    watchcards = get_watchmap_grid(extra_tickers=pulled_tickers or [])

    return {
        "ui_account": ui_account,
        "regime": regime,
        "api_credits": api_credits,
        "scanner": scanner,
        "alerts": alerts,
        "watchcards": watchcards,
        "pulled_tickers": pulled_tickers or [],
        "now_str": datetime.now(timezone.utc).strftime("%d %b · %H:%M:%S UTC"),
    }
