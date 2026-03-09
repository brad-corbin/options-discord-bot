# trade_journal.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.5 — Structured Trade Journal
#
# Logs every signal and trade with full context for backtesting.
# Three entry types:
#
#   SIGNAL:  Every TV webhook, whether it produced a trade or not.
#            Captures indicator state, tier, bias, and outcome.
#
#   OPEN:    When a spread is opened. Captures entry context:
#            signal data, confidence, Greeks, regime, IV/RV edge.
#
#   CLOSE:   When a spread is closed. Captures exit context:
#            exit reason, hold duration, P/L, Greeks attribution.
#
# Storage: {account}:journal:entries → list of entry dicts (capped)
#
# Query interface: filter by ticker, date range, tier, side,
# confidence band, win/loss, and compute aggregate stats.

import json
import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# STORE INTERFACE (shared with portfolio.py)
# ─────────────────────────────────────────────────────────

_store_get = None
_store_set = None

def init_store(getter, setter):
    """Call once at startup alongside portfolio.init_store()."""
    global _store_get, _store_set
    _store_get = getter
    _store_set = setter
    log.info("Trade journal store initialized")

def _get(key: str):
    if _store_get is None:
        raise RuntimeError("Journal store not initialized")
    return _store_get(key)

def _set(key: str, value: str, ttl: int = 0):
    if _store_set is None:
        raise RuntimeError("Journal store not initialized")
    _store_set(key, value, ttl)

def _key_journal(account: str = "brad") -> str:
    return f"{account}:journal:entries"

def _load_entries(account: str = "brad") -> list:
    raw = _get(_key_journal(account))
    if raw is None:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

def _save_entries(entries: list, account: str = "brad"):
    from trading_rules import JOURNAL_MAX_ENTRIES
    # Prune oldest if over cap
    if len(entries) > JOURNAL_MAX_ENTRIES:
        entries = entries[-JOURNAL_MAX_ENTRIES:]
    _set(_key_journal(account), json.dumps(entries))

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════
# SIGNAL LOGGING
# ═══════════════════════════════════════════════════════════

def log_signal(
    ticker: str,
    webhook_data: Dict,
    outcome: str,
    confidence: int = None,
    reason: str = None,
    trade_id: str = None,
    account: str = "brad",
):
    """
    Log a TV webhook signal — whether it produced a trade or not.

    outcome: "trade_opened" / "rejected" / "duplicate" / "risk_blocked" / "bear_signal"
    """
    from trading_rules import JOURNAL_LOG_ALL_SIGNALS, JOURNAL_LOG_REJECTED

    if not JOURNAL_LOG_ALL_SIGNALS:
        return
    if outcome == "rejected" and not JOURNAL_LOG_REJECTED:
        return

    entry = {
        "type":        "signal",
        "timestamp":   _now_str(),
        "date":        _today_str(),
        "ticker":      ticker.upper(),
        "bias":        webhook_data.get("bias", "unknown"),
        "tier":        webhook_data.get("tier", "?"),
        "outcome":     outcome,
        "confidence":  confidence,
        "reason":      reason,
        "trade_id":    trade_id,
        # Indicator snapshot
        "wt1":         webhook_data.get("wt1"),
        "wt2":         webhook_data.get("wt2"),
        "rsi_mfi":     webhook_data.get("rsi_mfi"),
        "rsi_mfi_bull": webhook_data.get("rsi_mfi_bull"),
        "above_vwap":  webhook_data.get("above_vwap"),
        "htf_confirmed": webhook_data.get("htf_confirmed"),
        "htf_converging": webhook_data.get("htf_converging"),
        "daily_bull":  webhook_data.get("daily_bull"),
        "macd_hist":   webhook_data.get("macd_hist"),
        "close":       webhook_data.get("close"),
        "timeframe":   webhook_data.get("timeframe"),
    }

    entries = _load_entries(account)
    entries.append(entry)
    _save_entries(entries, account)
    log.info(f"Journal [{account}]: signal {ticker} {outcome}")


# ═══════════════════════════════════════════════════════════
# TRADE OPEN LOGGING
# ═══════════════════════════════════════════════════════════

def log_trade_open(
    spread_id: str,
    ticker: str,
    rec: Dict,
    regime: Dict = None,
    risk_check: Dict = None,
    account: str = "brad",
):
    """
    Log a trade being opened with full entry context.

    rec: the recommendation dict from options_engine_v3.recommend_trade()
    """
    trade = rec.get("trade", {})
    vol_edge = rec.get("vol_edge", {})
    em_data = rec.get("em_data", {})
    regime = regime or {}

    entry = {
        "type":           "open",
        "timestamp":      _now_str(),
        "date":           _today_str(),
        "spread_id":      spread_id,
        "ticker":         ticker.upper(),
        "direction":      rec.get("direction", "bull"),
        "side":           rec.get("side", "call"),
        "long_strike":    trade.get("long"),
        "short_strike":   trade.get("short"),
        "width":          trade.get("width"),
        "debit":          trade.get("debit"),
        "contracts":      rec.get("contracts"),
        "dte":            rec.get("dte"),
        "exp":            rec.get("exp"),
        "spot":           rec.get("spot"),
        # Signal context
        "tier":           rec.get("tier"),
        "confidence":     rec.get("confidence"),
        "conf_reasons":   rec.get("conf_reasons", []),
        # Trade quality
        "ror":            trade.get("ror"),
        "cost_pct":       trade.get("cost_pct"),
        # Greeks at entry
        "entry_delta_long":  trade.get("long_delta"),
        "entry_delta_short": trade.get("short_delta"),
        "entry_net_theta":   trade.get("net_theta"),
        "entry_net_vega":    trade.get("net_vega"),
        # Vol edge
        "iv":             vol_edge.get("iv"),
        "rv":             vol_edge.get("rv"),
        "iv_pct":         vol_edge.get("iv_pct"),
        "rv_pct":         vol_edge.get("rv_pct"),
        "vol_edge_label": vol_edge.get("edge_label"),
        "vol_edge_pct":   vol_edge.get("edge_pct"),
        # Expected move
        "expected_move":  em_data.get("expected_move"),
        # Regime
        "regime_label":   regime.get("label"),
        "vix":            regime.get("vix"),
        "adx":            regime.get("adx"),
        # Risk check
        "gross_exposure_at_open": risk_check.get("new_gross") if risk_check else None,
    }

    entries = _load_entries(account)
    entries.append(entry)
    _save_entries(entries, account)
    log.info(f"Journal [{account}]: trade open {spread_id} {ticker}")


# ═══════════════════════════════════════════════════════════
# TRADE CLOSE LOGGING + GREEKS ATTRIBUTION
# ═══════════════════════════════════════════════════════════

def log_trade_close(
    spread_id: str,
    ticker: str,
    spread: Dict,
    close_price: float,
    exit_reason: str,
    close_spot: float = None,
    close_greeks: Dict = None,
    account: str = "brad",
):
    """
    Log a trade being closed with exit context and P/L attribution.

    exit_reason: "target_30" / "target_35" / "target_50" / "stop" /
                 "manual" / "expired_itm" / "expired_otm" / "exit_warning"

    close_greeks: {
        "net_theta": ..., "net_vega": ..., "iv": ...,
        "long_delta": ..., "short_delta": ...,
    }
    """
    debit = spread.get("debit", 0)
    contracts = spread.get("contracts", 1)
    pnl = round((close_price - debit) * contracts * 100, 2)
    total_risk = debit * contracts * 100
    ror_pct = round(pnl / total_risk * 100, 1) if total_risk > 0 else 0

    # Hold duration
    open_date = spread.get("open_date", "")
    close_date = _today_str()
    hold_days = 0
    try:
        od = datetime.strptime(open_date, "%Y-%m-%d")
        cd = datetime.strptime(close_date, "%Y-%m-%d")
        hold_days = (cd - od).days
    except Exception:
        pass

    # Greeks P/L attribution
    attribution = _compute_greeks_attribution(spread, spread_id, close_spot, close_greeks, hold_days, account)

    entry = {
        "type":          "close",
        "timestamp":     _now_str(),
        "date":          close_date,
        "spread_id":     spread_id,
        "ticker":        ticker.upper(),
        "direction":     spread.get("direction", "bull"),
        "side":          spread.get("side", "call"),
        "debit":         debit,
        "close_price":   close_price,
        "contracts":     contracts,
        "pnl":           pnl,
        "ror_pct":       ror_pct,
        "exit_reason":   exit_reason,
        "hold_days":     hold_days,
        "open_date":     open_date,
        "close_date":    close_date,
        "entry_spot":    spread.get("entry_spot"),
        "close_spot":    close_spot,
        # Attribution
        "attribution":   attribution,
    }

    entries = _load_entries(account)
    entries.append(entry)
    _save_entries(entries, account)
    log.info(f"Journal [{account}]: trade close {spread_id} {ticker} "
             f"P/L={pnl:+,.0f} ({exit_reason})")


def _compute_greeks_attribution(
    spread: Dict,
    spread_id: str,
    close_spot: float = None,
    close_greeks: Dict = None,
    hold_days: int = 0,
    account: str = "brad",
) -> Dict:
    """
    Estimate how much of the P/L came from delta, theta, and vega.

    This is an approximation — real attribution would need
    continuous mark-to-market. We use:

    Delta P/L ≈ net_delta × (close_spot - entry_spot) × 100
    Theta P/L ≈ net_theta × hold_days × 100
    Vega P/L  ≈ net_vega × (close_iv - entry_iv) × 100
    Residual  = actual P/L - (delta + theta + vega)

    The residual captures gamma, higher-order effects, and
    model error from using entry Greeks for the full period.
    """
    from trading_rules import GREEKS_ATTRIBUTION_ON_CLOSE

    if not GREEKS_ATTRIBUTION_ON_CLOSE:
        return {}

    # Find the open entry for this spread to get entry Greeks
    open_entry = _find_open_entry(spread_id, account)
    if not open_entry:
        return {"note": "No entry Greeks recorded"}

    contracts = spread.get("contracts", 1)
    debit = spread.get("debit", 0)
    close_price = spread.get("close_price") or 0
    actual_pnl = (close_price - debit) * contracts * 100

    entry_spot = open_entry.get("spot", 0)
    entry_theta = open_entry.get("entry_net_theta") or 0
    entry_vega = open_entry.get("entry_net_vega") or 0
    entry_iv = open_entry.get("iv") or 0

    # Net delta at entry (long_delta - short_delta)
    ld = open_entry.get("entry_delta_long") or 0
    sd = open_entry.get("entry_delta_short") or 0
    entry_net_delta = ld - sd

    # Close data
    close_spot = close_spot or entry_spot
    close_iv = (close_greeks or {}).get("iv", entry_iv)

    # Attributions (per contract, then × contracts)
    spot_change = close_spot - entry_spot if entry_spot > 0 else 0
    iv_change = close_iv - entry_iv if entry_iv > 0 else 0

    delta_pnl = round(entry_net_delta * spot_change * contracts * 100, 2)
    theta_pnl = round(entry_theta * hold_days * contracts * 100, 2)
    vega_pnl = round(entry_vega * iv_change * contracts * 100, 2) if iv_change != 0 else 0
    residual = round(actual_pnl - delta_pnl - theta_pnl - vega_pnl, 2)

    return {
        "delta_pnl":    delta_pnl,
        "theta_pnl":    theta_pnl,
        "vega_pnl":     vega_pnl,
        "residual":     residual,
        "actual_pnl":   round(actual_pnl, 2),
        "spot_change":  round(spot_change, 2),
        "iv_change":    round(iv_change, 4) if iv_change else 0,
        "hold_days":    hold_days,
        "entry_delta":  round(entry_net_delta, 4),
        "entry_theta":  round(entry_theta, 4),
        "entry_vega":   round(entry_vega, 4),
    }


def _find_open_entry(spread_id: str, account: str = "brad") -> Optional[Dict]:
    """Find the 'open' journal entry for a spread ID."""
    for entry in _load_entries(account):
        if entry.get("type") == "open" and entry.get("spread_id") == spread_id:
            return entry
    return None


# ═══════════════════════════════════════════════════════════
# QUERY & ANALYTICS
# ═══════════════════════════════════════════════════════════

def query_journal(
    account: str = "brad",
    entry_type: str = None,
    ticker: str = None,
    side: str = None,
    tier: str = None,
    date_from: str = None,
    date_to: str = None,
    outcome: str = None,
    limit: int = 50,
) -> List[Dict]:
    """
    Query journal entries with filters.
    Returns most recent entries first.
    """
    entries = _load_entries(account)

    filtered = []
    for e in reversed(entries):
        if entry_type and e.get("type") != entry_type:
            continue
        if ticker and e.get("ticker") != ticker.upper():
            continue
        if side and e.get("side") != side:
            continue
        if tier and str(e.get("tier")) != str(tier):
            continue
        if outcome and e.get("outcome") != outcome:
            continue

        entry_date = e.get("date", "")
        if date_from and entry_date < date_from:
            continue
        if date_to and entry_date > date_to:
            continue

        filtered.append(e)
        if len(filtered) >= limit:
            break

    return filtered


def calc_journal_stats(account: str = "brad", ticker: str = None) -> Dict:
    """
    Aggregate stats from the journal for backtesting analysis.

    Returns:
        signal_count, trade_count, win_rate, avg_pnl, avg_hold_days,
        by_tier, by_confidence_band, by_dte, by_vol_edge,
        attribution_totals (delta/theta/vega breakdown)
    """
    entries = _load_entries(account)

    signals = [e for e in entries if e.get("type") == "signal"]
    closes = [e for e in entries if e.get("type") == "close"]

    if ticker:
        ticker = ticker.upper()
        signals = [s for s in signals if s.get("ticker") == ticker]
        closes = [c for c in closes if c.get("ticker") == ticker]

    # Win/loss
    wins = [c for c in closes if c.get("pnl", 0) > 0]
    losses = [c for c in closes if c.get("pnl", 0) <= 0]

    total_pnl = sum(c.get("pnl", 0) for c in closes)
    avg_pnl = round(total_pnl / max(len(closes), 1), 2)
    avg_hold = round(sum(c.get("hold_days", 0) for c in closes) / max(len(closes), 1), 1)

    avg_win = round(sum(c.get("pnl", 0) for c in wins) / max(len(wins), 1), 2)
    avg_loss = round(sum(c.get("pnl", 0) for c in losses) / max(len(losses), 1), 2)

    # By tier
    by_tier = {}
    for c in closes:
        # Find matching open entry for tier
        open_e = _find_open_entry(c.get("spread_id", ""), account)
        t = open_e.get("tier", "?") if open_e else "?"
        if t not in by_tier:
            by_tier[t] = {"count": 0, "pnl": 0, "wins": 0}
        by_tier[t]["count"] += 1
        by_tier[t]["pnl"] += c.get("pnl", 0)
        if c.get("pnl", 0) > 0:
            by_tier[t]["wins"] += 1

    # By confidence band (40-55, 55-70, 70-85, 85-100)
    by_conf = {"40-55": {"count": 0, "pnl": 0, "wins": 0},
               "55-70": {"count": 0, "pnl": 0, "wins": 0},
               "70-85": {"count": 0, "pnl": 0, "wins": 0},
               "85+":   {"count": 0, "pnl": 0, "wins": 0}}

    for c in closes:
        open_e = _find_open_entry(c.get("spread_id", ""), account)
        conf = open_e.get("confidence", 0) if open_e else 0
        if conf >= 85:
            band = "85+"
        elif conf >= 70:
            band = "70-85"
        elif conf >= 55:
            band = "55-70"
        else:
            band = "40-55"
        by_conf[band]["count"] += 1
        by_conf[band]["pnl"] += c.get("pnl", 0)
        if c.get("pnl", 0) > 0:
            by_conf[band]["wins"] += 1

    # By vol edge
    by_edge = {"BUYER": {"count": 0, "pnl": 0, "wins": 0},
               "SELLER": {"count": 0, "pnl": 0, "wins": 0},
               "NEUTRAL": {"count": 0, "pnl": 0, "wins": 0}}

    for c in closes:
        open_e = _find_open_entry(c.get("spread_id", ""), account)
        edge = open_e.get("vol_edge_label", "NEUTRAL") if open_e else "NEUTRAL"
        if edge not in by_edge:
            edge = "NEUTRAL"
        by_edge[edge]["count"] += 1
        by_edge[edge]["pnl"] += c.get("pnl", 0)
        if c.get("pnl", 0) > 0:
            by_edge[edge]["wins"] += 1

    # Attribution totals
    total_delta_pnl = 0
    total_theta_pnl = 0
    total_vega_pnl = 0
    total_residual = 0
    attr_count = 0

    for c in closes:
        attr = c.get("attribution", {})
        if attr and "delta_pnl" in attr:
            total_delta_pnl += attr.get("delta_pnl", 0)
            total_theta_pnl += attr.get("theta_pnl", 0)
            total_vega_pnl += attr.get("vega_pnl", 0)
            total_residual += attr.get("residual", 0)
            attr_count += 1

    # Signal conversion rate
    trade_signals = [s for s in signals if s.get("outcome") == "trade_opened"]
    rejected_signals = [s for s in signals if s.get("outcome") == "rejected"]

    return {
        "signal_count":     len(signals),
        "trade_signals":    len(trade_signals),
        "rejected_signals": len(rejected_signals),
        "conversion_rate":  round(len(trade_signals) / max(len(signals), 1) * 100, 1),
        "trade_count":      len(closes),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(len(wins) / max(len(closes), 1) * 100, 1),
        "total_pnl":        round(total_pnl, 2),
        "avg_pnl":          avg_pnl,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "avg_hold_days":    avg_hold,
        "by_tier":          by_tier,
        "by_confidence":    by_conf,
        "by_vol_edge":      by_edge,
        "attribution": {
            "count":        attr_count,
            "delta_pnl":    round(total_delta_pnl, 2),
            "theta_pnl":    round(total_theta_pnl, 2),
            "vega_pnl":     round(total_vega_pnl, 2),
            "residual":     round(total_residual, 2),
        },
    }
