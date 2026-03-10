# risk_manager.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.5 — Portfolio Risk Manager
# v3.7 — Direction-aware regime sizing for debit spreads
#
# Key insight: debit spreads buy AND sell premium simultaneously.
# IV crush affects both legs equally — net debit is COMPRESSED in
# high VIX, not expanded. So CRISIS/ELEVATED VIX should not block
# bear spreads — it should actually favor them with reduced size.
#
# Regime logic (v3.7):
#   CRISIS  + bear spread → size ×0.5  (allowed, sized down)
#   CRISIS  + bull spread → size ×0.25 (heavily reduced, going against fear)
#   ELEVATED + bear      → size ×0.75
#   ELEVATED + bull      → size ×0.5
#   CHOPPY  (any)        → size ×0.75
#   TRENDING + LOW/NORM  → size ×1.25 (bonus)
#   NORMAL               → size ×1.0

import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# RISK CHECK — MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────

def check_risk_limits(
    ticker: str,
    debit: float,
    contracts: int,
    account: str = "brad",
    regime: Dict = None,
    new_trade_greeks: Dict = None,
    direction: str = "bull",
) -> Dict:
    """
    Run all portfolio-level risk checks before opening a new spread.

    v3.6: Also checks portfolio-level net delta/gamma/vega limits.
    v3.7: Direction-aware regime sizing — bear spreads allowed in CRISIS,
          bull spreads heavily penalized. CRISIS no longer hard-blocks.

    new_trade_greeks: {"net_delta": 0.12, "net_gamma": 0.003, "net_vega": 0.02}
    """
    from trading_rules import (
        ACCOUNT_SIZE,
        DAILY_LOSS_LIMIT_USD, DAILY_LOSS_LIMIT_PCT,
        MAX_GROSS_EXPOSURE_USD, MAX_GROSS_EXPOSURE_PCT,
        MAX_TICKER_EXPOSURE_USD, MAX_TICKER_EXPOSURE_PCT,
        MAX_OPEN_SPREADS, MAX_SAME_SECTOR_SPREADS,
        SECTOR_MAP,
        REGIME_CRISIS_BLOCK,
        REGIME_ELEVATED_SIZE_MULT, REGIME_CHOPPY_SIZE_MULT,
        REGIME_TRENDING_SIZE_MULT,
        MAX_PORTFOLIO_DELTA, MAX_PORTFOLIO_GAMMA, MAX_PORTFOLIO_VEGA,
    )
    from portfolio import get_open_spreads, calc_spread_pnl, get_all_spreads

    regime    = regime or {}
    direction = (direction or "bull").lower()
    warnings  = []
    blocks    = []
    size_mult = 1.0

    new_risk     = debit * contracts * 100
    open_spreads = get_open_spreads(account=account)

    # ── 1. Max open spread count ──
    if len(open_spreads) >= MAX_OPEN_SPREADS:
        blocks.append(
            f"Max open spreads reached ({len(open_spreads)}/{MAX_OPEN_SPREADS})"
        )

    # ── 2. Gross exposure ──
    current_gross = sum(
        s.get("debit", 0) * s.get("contracts", 1) * 100
        for s in open_spreads
    )
    new_gross   = current_gross + new_risk
    gross_limit = min(MAX_GROSS_EXPOSURE_USD, ACCOUNT_SIZE * MAX_GROSS_EXPOSURE_PCT)

    if new_gross > gross_limit:
        blocks.append(
            f"Gross exposure ${new_gross:,.0f} would exceed "
            f"${gross_limit:,.0f} limit (current: ${current_gross:,.0f})"
        )

    # ── 3. Ticker concentration ──
    ticker_upper   = ticker.upper()
    ticker_risk    = sum(
        s.get("debit", 0) * s.get("contracts", 1) * 100
        for s in open_spreads
        if s.get("ticker") == ticker_upper
    )
    new_ticker_risk = ticker_risk + new_risk
    ticker_limit    = min(MAX_TICKER_EXPOSURE_USD, ACCOUNT_SIZE * MAX_TICKER_EXPOSURE_PCT)

    if new_ticker_risk > ticker_limit:
        blocks.append(
            f"{ticker_upper} exposure ${new_ticker_risk:,.0f} would exceed "
            f"${ticker_limit:,.0f} limit (current: ${ticker_risk:,.0f})"
        )

    # ── 4. Daily loss limit ──
    daily_pnl   = _calc_daily_realized_pnl(account)
    daily_limit = min(DAILY_LOSS_LIMIT_USD, ACCOUNT_SIZE * DAILY_LOSS_LIMIT_PCT)

    if daily_pnl < 0 and abs(daily_pnl) >= daily_limit:
        blocks.append(
            f"Daily loss limit hit: ${daily_pnl:,.0f} "
            f"(limit: -${daily_limit:,.0f})"
        )

    # ── 5. Sector concentration (soft warning) ──
    sector       = SECTOR_MAP.get(ticker_upper, "other")
    sector_count = sum(
        1 for s in open_spreads
        if SECTOR_MAP.get(s.get("ticker", ""), "other") == sector
    )
    if sector_count >= MAX_SAME_SECTOR_SPREADS:
        warnings.append(
            f"Sector '{sector}' has {sector_count} open spreads "
            f"(soft limit: {MAX_SAME_SECTOR_SPREADS})"
        )

    # ── 6. Portfolio Greeks limits (v3.6) ──
    port_greeks  = calc_portfolio_greeks(account)
    new_greeks   = new_trade_greeks or {}
    new_nd       = new_greeks.get("net_delta", 0) * contracts * 100
    new_ng       = new_greeks.get("net_gamma", 0) * contracts * 100
    new_nv       = new_greeks.get("net_vega",  0) * contracts * 100

    projected_delta = port_greeks["net_delta"] + new_nd
    projected_gamma = port_greeks["net_gamma"] + new_ng
    projected_vega  = port_greeks["net_vega"]  + new_nv

    if MAX_PORTFOLIO_DELTA > 0 and abs(projected_delta) > MAX_PORTFOLIO_DELTA:
        warnings.append(
            f"Portfolio delta would be {projected_delta:+.0f} "
            f"(limit: ±{MAX_PORTFOLIO_DELTA:.0f})"
        )
    if MAX_PORTFOLIO_GAMMA > 0 and abs(projected_gamma) > MAX_PORTFOLIO_GAMMA:
        warnings.append(
            f"Portfolio gamma would be {projected_gamma:+.1f} "
            f"(limit: ±{MAX_PORTFOLIO_GAMMA:.0f})"
        )
    if MAX_PORTFOLIO_VEGA > 0 and abs(projected_vega) > MAX_PORTFOLIO_VEGA:
        warnings.append(
            f"Portfolio vega would be {projected_vega:+.1f} "
            f"(limit: ±{MAX_PORTFOLIO_VEGA:.0f})"
        )

    # ── 7. Market regime — direction-aware sizing (v3.7) ──
    vix_regime = regime.get("vix_regime", "NORMAL")
    adx_regime = regime.get("adx_regime", "MODERATE")
    vix_val    = regime.get("vix", 0)
    is_bear    = direction == "bear"

    if vix_regime == "CRISIS":
        if is_bear:
            # Bear spreads BENEFIT from high IV environment —
            # premium is compressed, direction is with the market
            size_mult = 0.5
            warnings.append(
                f"CRISIS regime (VIX {vix_val:.1f}) — bear spread allowed at ×0.5 size"
            )
        else:
            # Bull spreads are going against extreme fear — heavily reduce
            size_mult = 0.25
            warnings.append(
                f"CRISIS regime (VIX {vix_val:.1f}) — bull spread against trend, size ×0.25"
            )
        # Hard block only if REGIME_CRISIS_BLOCK is explicitly set AND it's a bull trade
        if REGIME_CRISIS_BLOCK and not is_bear:
            blocks.append(
                f"VIX CRISIS ({vix_val:.1f}) — bull entries blocked in crisis regime"
            )

    elif vix_regime == "ELEVATED":
        if is_bear:
            size_mult = 0.75
            warnings.append(f"Elevated VIX ({vix_val:.1f}) — bear spread at ×0.75")
        else:
            size_mult = REGIME_ELEVATED_SIZE_MULT  # 0.5
            warnings.append(f"Elevated VIX ({vix_val:.1f}) — bull spread at ×{size_mult}")

    elif adx_regime == "CHOPPY":
        size_mult = REGIME_CHOPPY_SIZE_MULT  # 0.75
        warnings.append(f"Choppy market (ADX {regime.get('adx', 0):.0f}) → size ×{size_mult}")

    elif adx_regime == "TRENDING" and vix_regime in ("LOW", "NORMAL"):
        size_mult = REGIME_TRENDING_SIZE_MULT  # 1.25
        # No warning — this is the ideal case

    allowed = len(blocks) == 0

    return {
        "allowed":          allowed,
        "warnings":         warnings,
        "blocks":           blocks,
        "size_multiplier":  size_mult,
        "regime":           regime,
        "current_gross":    current_gross,
        "new_gross":        new_gross,
        "gross_limit":      gross_limit,
        "open_count":       len(open_spreads),
        "ticker_exposure":  ticker_risk,
        "daily_pnl":        daily_pnl,
    }


def _calc_daily_realized_pnl(account: str = "brad") -> float:
    """Sum of realized P/L from spreads closed TODAY."""
    from portfolio import get_all_spreads, calc_spread_pnl

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0

    for s in get_all_spreads(account=account):
        if s.get("close_date") == today and s.get("status") != "open":
            total += calc_spread_pnl(s)

    return round(total, 2)


# ─────────────────────────────────────────────────────────
# MARKET REGIME DETECTION
# ─────────────────────────────────────────────────────────

def classify_regime(
    vix: float = None,
    adx: float = None,
    spy_candles: List[float] = None,
) -> Dict:
    """
    Classify current market regime based on VIX and ADX.

    v3.7: size_mult in classify_regime is the BASE multiplier for
    neutral/unknown direction. Direction-aware adjustments are applied
    in check_risk_limits() where direction is known.
    """
    from trading_rules import (
        REGIME_VIX_LOW, REGIME_VIX_NORMAL, REGIME_VIX_ELEVATED,
        REGIME_ADX_CHOPPY, REGIME_ADX_TRENDING,
        REGIME_ELEVATED_SIZE_MULT, REGIME_CHOPPY_SIZE_MULT,
        REGIME_TRENDING_SIZE_MULT,
    )

    # VIX regime
    vix = vix or 0
    if vix <= 0:
        vix_regime = "UNKNOWN"
    elif vix < REGIME_VIX_LOW:
        vix_regime = "LOW"
    elif vix < REGIME_VIX_NORMAL:
        vix_regime = "NORMAL"
    elif vix < REGIME_VIX_ELEVATED:
        vix_regime = "ELEVATED"
    else:
        vix_regime = "CRISIS"

    # ADX regime
    if adx is None and spy_candles and len(spy_candles) >= 20:
        adx = _compute_adx(spy_candles, period=14)

    adx = adx or 0
    if adx <= 0:
        adx_regime = "UNKNOWN"
    elif adx < REGIME_ADX_CHOPPY:
        adx_regime = "CHOPPY"
    elif adx < REGIME_ADX_TRENDING:
        adx_regime = "MODERATE"
    else:
        adx_regime = "TRENDING"

    # Combined label + base size_mult (direction-neutral)
    if vix_regime == "CRISIS":
        label     = "CRISIS"
        emoji     = "🔴"
        size_mult = 0.5   # base — check_risk_limits adjusts per direction
    elif vix_regime == "ELEVATED" and adx_regime == "CHOPPY":
        label     = "HIGH VOL CHOP"
        emoji     = "🟠"
        size_mult = REGIME_ELEVATED_SIZE_MULT
    elif vix_regime == "ELEVATED":
        label     = "HIGH VOL TREND"
        emoji     = "🟡"
        size_mult = REGIME_CHOPPY_SIZE_MULT
    elif adx_regime == "CHOPPY":
        label     = "LOW VOL CHOP"
        emoji     = "🟡"
        size_mult = REGIME_CHOPPY_SIZE_MULT
    elif adx_regime == "TRENDING" and vix_regime in ("LOW", "NORMAL"):
        label     = "TRENDING"
        emoji     = "🟢"
        size_mult = REGIME_TRENDING_SIZE_MULT
    else:
        label     = "NORMAL"
        emoji     = "⚪"
        size_mult = 1.0

    return {
        "vix":        round(vix, 2),
        "vix_regime": vix_regime,
        "adx":        round(adx, 2) if adx else 0,
        "adx_regime": adx_regime,
        "label":      label,
        "emoji":      emoji,
        "size_mult":  size_mult,
    }


def _compute_adx(closes: List[float], period: int = 14) -> float:
    """
    Compute ADX from daily closes (close-to-close approximation).
    Returns current ADX value (0-100).
    """
    if len(closes) < period + 2:
        return 0.0

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    plus_dm  = []
    minus_dm = []
    tr_list  = []

    for i in range(1, len(changes)):
        up_move   = changes[i] if changes[i] > 0 else 0
        down_move = abs(changes[i]) if changes[i] < 0 else 0

        if up_move > down_move:
            plus_dm.append(up_move)
            minus_dm.append(0)
        else:
            plus_dm.append(0)
            minus_dm.append(down_move)

        tr = abs(changes[i])
        tr_list.append(max(tr, 0.01))

    if len(tr_list) < period:
        return 0.0

    def wilder_smooth(data, p):
        if len(data) < p:
            return []
        smoothed = [sum(data[:p]) / p]
        for i in range(p, len(data)):
            smoothed.append((smoothed[-1] * (p - 1) + data[i]) / p)
        return smoothed

    smooth_plus  = wilder_smooth(plus_dm,  period)
    smooth_minus = wilder_smooth(minus_dm, period)
    smooth_tr    = wilder_smooth(tr_list,  period)

    if not smooth_plus or not smooth_minus or not smooth_tr:
        return 0.0

    dx_list = []
    n = min(len(smooth_plus), len(smooth_minus), len(smooth_tr))

    for i in range(n):
        atr = smooth_tr[i]
        if atr <= 0:
            continue
        di_plus  = (smooth_plus[i]  / atr) * 100
        di_minus = (smooth_minus[i] / atr) * 100
        di_sum   = di_plus + di_minus

        if di_sum > 0:
            dx = abs(di_plus - di_minus) / di_sum * 100
            dx_list.append(dx)

    if len(dx_list) < period:
        return sum(dx_list) / max(len(dx_list), 1)

    adx_values = wilder_smooth(dx_list, period)
    return round(adx_values[-1], 2) if adx_values else 0.0


# ─────────────────────────────────────────────────────────
# PORTFOLIO GREEKS (v3.6)
# ─────────────────────────────────────────────────────────

def calc_portfolio_greeks(account: str = "brad") -> Dict:
    """
    Compute aggregate Greeks across all open spreads.
    Uses entry Greeks stored in trade journal as approximation.
    """
    from portfolio import get_open_spreads

    open_spreads = get_open_spreads(account=account)
    totals       = {"net_delta": 0, "net_gamma": 0, "net_vega": 0, "net_theta": 0}
    by_ticker    = {}

    for s in open_spreads:
        ticker    = s.get("ticker", "?")
        contracts = s.get("contracts", 1)
        multiplier = contracts * 100

        nd = s.get("net_delta") or 0
        ng = s.get("net_gamma") or 0
        nv = s.get("net_vega")  or 0
        nt = s.get("net_theta") or 0

        if nd == 0:
            try:
                from trade_journal import _find_open_entry
                journal_entry = _find_open_entry(s.get("id", ""), account)
                if journal_entry:
                    ld = journal_entry.get("entry_delta_long")  or 0
                    sd = journal_entry.get("entry_delta_short") or 0
                    nd = ld - sd
                    nt = journal_entry.get("entry_net_theta") or 0
                    nv = journal_entry.get("entry_net_vega")  or 0
            except Exception:
                pass

        d = nd * multiplier
        g = ng * multiplier
        v = nv * multiplier
        t = nt * multiplier

        totals["net_delta"] += d
        totals["net_gamma"] += g
        totals["net_vega"]  += v
        totals["net_theta"] += t

        if ticker not in by_ticker:
            by_ticker[ticker] = {"delta": 0, "gamma": 0, "vega": 0, "theta": 0}
        by_ticker[ticker]["delta"] += d
        by_ticker[ticker]["gamma"] += g
        by_ticker[ticker]["vega"]  += v
        by_ticker[ticker]["theta"] += t

    return {
        "net_delta": round(totals["net_delta"], 2),
        "net_gamma": round(totals["net_gamma"], 4),
        "net_vega":  round(totals["net_vega"],  2),
        "net_theta": round(totals["net_theta"], 2),
        "by_ticker": {t: {k: round(v, 2) for k, v in d.items()} for t, d in by_ticker.items()},
    }


# ─────────────────────────────────────────────────────────
# RISK DASHBOARD (for /risk command)
# ─────────────────────────────────────────────────────────

def get_risk_dashboard(account: str = "brad", regime: Dict = None) -> Dict:
    from trading_rules import (
        ACCOUNT_SIZE,
        MAX_GROSS_EXPOSURE_USD, MAX_GROSS_EXPOSURE_PCT,
        MAX_TICKER_EXPOSURE_USD, MAX_TICKER_EXPOSURE_PCT,
        DAILY_LOSS_LIMIT_USD, DAILY_LOSS_LIMIT_PCT,
        MAX_OPEN_SPREADS, MAX_SAME_SECTOR_SPREADS,
        SECTOR_MAP,
        MAX_PORTFOLIO_DELTA, MAX_PORTFOLIO_GAMMA, MAX_PORTFOLIO_VEGA,
    )
    from portfolio import get_open_spreads

    regime       = regime or {}
    open_spreads = get_open_spreads(account=account)

    gross       = sum(
        s.get("debit", 0) * s.get("contracts", 1) * 100
        for s in open_spreads
    )
    gross_limit = min(MAX_GROSS_EXPOSURE_USD, ACCOUNT_SIZE * MAX_GROSS_EXPOSURE_PCT)
    gross_pct   = round(gross / gross_limit * 100, 1) if gross_limit > 0 else 0

    ticker_risk = {}
    for s in open_spreads:
        t = s.get("ticker", "?")
        r = s.get("debit", 0) * s.get("contracts", 1) * 100
        ticker_risk[t] = ticker_risk.get(t, 0) + r

    ticker_limit  = min(MAX_TICKER_EXPOSURE_USD, ACCOUNT_SIZE * MAX_TICKER_EXPOSURE_PCT)

    sector_counts = {}
    for s in open_spreads:
        t      = s.get("ticker", "?")
        sector = SECTOR_MAP.get(t, "other")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    daily_pnl   = _calc_daily_realized_pnl(account)
    daily_limit = min(DAILY_LOSS_LIMIT_USD, ACCOUNT_SIZE * DAILY_LOSS_LIMIT_PCT)

    port_greeks = calc_portfolio_greeks(account)

    return {
        "open_count":       len(open_spreads),
        "max_open":         MAX_OPEN_SPREADS,
        "gross_exposure":   round(gross, 2),
        "gross_limit":      round(gross_limit, 2),
        "gross_pct":        gross_pct,
        "ticker_risk":      ticker_risk,
        "ticker_limit":     round(ticker_limit, 2),
        "sector_counts":    sector_counts,
        "sector_limit":     MAX_SAME_SECTOR_SPREADS,
        "daily_pnl":        daily_pnl,
        "daily_limit":      round(daily_limit, 2),
        "regime":           regime,
        "portfolio_greeks": port_greeks,
        "greek_limits": {
            "delta": MAX_PORTFOLIO_DELTA,
            "gamma": MAX_PORTFOLIO_GAMMA,
            "vega":  MAX_PORTFOLIO_VEGA,
        },
    }
