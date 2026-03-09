# risk_manager.py
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# v3.5 — Portfolio Risk Manager
#
# Enforces portfolio-level risk limits:
#   - Daily loss limit (auto-pause)
#   - Gross exposure cap
#   - Ticker concentration cap
#   - Max open spread count
#   - Sector concentration (soft warning)
#
# Market regime detection:
#   - VIX-based volatility regime
#   - ADX-based trend/chop classification
#   - Regime affects position sizing and confidence
#
# All functions are stateless — they read from portfolio.py
# and return pass/fail with reasons. The engine calls these
# before opening any new position.

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
) -> Dict:
    """
    Run all portfolio-level risk checks before opening a new spread.

    Returns:
        {
            "allowed": True/False,
            "warnings": [...],       # soft warnings (sector concentration, etc.)
            "blocks": [...],         # hard blocks (daily loss, gross exposure, etc.)
            "size_multiplier": 1.0,  # regime-adjusted sizing
            "regime": {...},         # current market regime data
        }
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
    )
    from portfolio import get_open_spreads, calc_spread_pnl, get_all_spreads

    regime = regime or {}
    warnings = []
    blocks = []
    size_mult = 1.0

    new_risk = debit * contracts * 100
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
    new_gross = current_gross + new_risk
    gross_limit = min(MAX_GROSS_EXPOSURE_USD, ACCOUNT_SIZE * MAX_GROSS_EXPOSURE_PCT)

    if new_gross > gross_limit:
        blocks.append(
            f"Gross exposure ${new_gross:,.0f} would exceed "
            f"${gross_limit:,.0f} limit (current: ${current_gross:,.0f})"
        )

    # ── 3. Ticker concentration ──
    ticker_upper = ticker.upper()
    ticker_risk = sum(
        s.get("debit", 0) * s.get("contracts", 1) * 100
        for s in open_spreads
        if s.get("ticker") == ticker_upper
    )
    new_ticker_risk = ticker_risk + new_risk
    ticker_limit = min(MAX_TICKER_EXPOSURE_USD, ACCOUNT_SIZE * MAX_TICKER_EXPOSURE_PCT)

    if new_ticker_risk > ticker_limit:
        blocks.append(
            f"{ticker_upper} exposure ${new_ticker_risk:,.0f} would exceed "
            f"${ticker_limit:,.0f} limit (current: ${ticker_risk:,.0f})"
        )

    # ── 4. Daily loss limit ──
    daily_pnl = _calc_daily_realized_pnl(account)
    daily_limit = min(DAILY_LOSS_LIMIT_USD, ACCOUNT_SIZE * DAILY_LOSS_LIMIT_PCT)

    if daily_pnl < 0 and abs(daily_pnl) >= daily_limit:
        blocks.append(
            f"Daily loss limit hit: ${daily_pnl:,.0f} "
            f"(limit: -${daily_limit:,.0f})"
        )

    # ── 5. Sector concentration (soft warning) ──
    sector = SECTOR_MAP.get(ticker_upper, "other")
    sector_count = sum(
        1 for s in open_spreads
        if SECTOR_MAP.get(s.get("ticker", ""), "other") == sector
    )
    if sector_count >= MAX_SAME_SECTOR_SPREADS:
        warnings.append(
            f"Sector '{sector}' has {sector_count} open spreads "
            f"(soft limit: {MAX_SAME_SECTOR_SPREADS})"
        )

    # ── 6. Market regime adjustments ──
    vix_regime = regime.get("vix_regime", "NORMAL")
    adx_regime = regime.get("adx_regime", "MODERATE")

    if vix_regime == "CRISIS" and REGIME_CRISIS_BLOCK:
        blocks.append(
            f"VIX CRISIS ({regime.get('vix', 0):.1f}) — "
            f"all new entries blocked"
        )

    # Size multiplier from regime
    if vix_regime == "ELEVATED" and adx_regime == "CHOPPY":
        size_mult = REGIME_ELEVATED_SIZE_MULT
        warnings.append(f"Regime: elevated VIX + choppy → size ×{size_mult}")
    elif adx_regime == "CHOPPY":
        size_mult = REGIME_CHOPPY_SIZE_MULT
        warnings.append(f"Regime: choppy market → size ×{size_mult}")
    elif adx_regime == "TRENDING" and vix_regime in ("LOW", "NORMAL"):
        size_mult = REGIME_TRENDING_SIZE_MULT
        # No warning needed — this is the good case

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
    """
    Sum of realized P/L from spreads closed TODAY.
    """
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

    Args:
        vix:          Current VIX spot level
        adx:          14-period ADX on SPY daily
        spy_candles:  Recent SPY daily closes (for computing ADX if not provided)

    Returns dict with:
        vix:          float — current VIX
        vix_regime:   "LOW" / "NORMAL" / "ELEVATED" / "CRISIS"
        adx:          float — current ADX
        adx_regime:   "CHOPPY" / "MODERATE" / "TRENDING"
        label:        human-readable regime label
        emoji:        visual indicator
        size_mult:    recommended position size multiplier
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

    # Combined label
    if vix_regime == "CRISIS":
        label = "CRISIS"
        emoji = "🔴"
        size_mult = 0.0  # blocked
    elif vix_regime == "ELEVATED" and adx_regime == "CHOPPY":
        label = "HIGH VOL CHOP"
        emoji = "🟠"
        size_mult = REGIME_ELEVATED_SIZE_MULT
    elif vix_regime == "ELEVATED":
        label = "HIGH VOL TREND"
        emoji = "🟡"
        size_mult = REGIME_CHOPPY_SIZE_MULT
    elif adx_regime == "CHOPPY":
        label = "LOW VOL CHOP"
        emoji = "🟡"
        size_mult = REGIME_CHOPPY_SIZE_MULT
    elif adx_regime == "TRENDING" and vix_regime in ("LOW", "NORMAL"):
        label = "TRENDING"
        emoji = "🟢"
        size_mult = REGIME_TRENDING_SIZE_MULT
    else:
        label = "NORMAL"
        emoji = "⚪"
        size_mult = 1.0

    return {
        "vix":         round(vix, 2),
        "vix_regime":  vix_regime,
        "adx":         round(adx, 2) if adx else 0,
        "adx_regime":  adx_regime,
        "label":       label,
        "emoji":       emoji,
        "size_mult":   size_mult,
    }


def _compute_adx(closes: List[float], period: int = 14) -> float:
    """
    Compute ADX (Average Directional Index) from daily closes.

    Simplified computation using close-to-close directional movement.
    A proper ADX uses high/low/close but this gives a reasonable
    approximation when only closes are available.

    Returns the current ADX value (0-100).
    """
    if len(closes) < period + 2:
        return 0.0

    # Compute absolute daily changes as proxy for directional movement
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Positive and negative directional movement
    plus_dm = []
    minus_dm = []
    tr_list = []

    for i in range(1, len(changes)):
        up_move = changes[i] if changes[i] > 0 else 0
        down_move = abs(changes[i]) if changes[i] < 0 else 0

        if up_move > down_move:
            plus_dm.append(up_move)
            minus_dm.append(0)
        else:
            plus_dm.append(0)
            minus_dm.append(down_move)

        # True range approximation (using close-to-close)
        tr = abs(changes[i])
        tr_list.append(max(tr, 0.01))  # avoid division by zero

    if len(tr_list) < period:
        return 0.0

    # Smoothed averages (Wilder's smoothing)
    def wilder_smooth(data, p):
        if len(data) < p:
            return []
        smoothed = [sum(data[:p]) / p]
        for i in range(p, len(data)):
            smoothed.append((smoothed[-1] * (p - 1) + data[i]) / p)
        return smoothed

    smooth_plus = wilder_smooth(plus_dm, period)
    smooth_minus = wilder_smooth(minus_dm, period)
    smooth_tr = wilder_smooth(tr_list, period)

    if not smooth_plus or not smooth_minus or not smooth_tr:
        return 0.0

    # DI+ and DI-
    dx_list = []
    n = min(len(smooth_plus), len(smooth_minus), len(smooth_tr))

    for i in range(n):
        atr = smooth_tr[i]
        if atr <= 0:
            continue
        di_plus = (smooth_plus[i] / atr) * 100
        di_minus = (smooth_minus[i] / atr) * 100
        di_sum = di_plus + di_minus

        if di_sum > 0:
            dx = abs(di_plus - di_minus) / di_sum * 100
            dx_list.append(dx)

    if len(dx_list) < period:
        return sum(dx_list) / max(len(dx_list), 1)

    # ADX = smoothed average of DX
    adx_values = wilder_smooth(dx_list, period)
    return round(adx_values[-1], 2) if adx_values else 0.0


# ─────────────────────────────────────────────────────────
# RISK DASHBOARD (for /risk command)
# ─────────────────────────────────────────────────────────

def get_risk_dashboard(account: str = "brad", regime: Dict = None) -> Dict:
    """
    Full risk status for display via /risk command.
    """
    from trading_rules import (
        ACCOUNT_SIZE,
        MAX_GROSS_EXPOSURE_USD, MAX_GROSS_EXPOSURE_PCT,
        MAX_TICKER_EXPOSURE_USD, MAX_TICKER_EXPOSURE_PCT,
        DAILY_LOSS_LIMIT_USD, DAILY_LOSS_LIMIT_PCT,
        MAX_OPEN_SPREADS, MAX_SAME_SECTOR_SPREADS,
        SECTOR_MAP,
    )
    from portfolio import get_open_spreads

    regime = regime or {}
    open_spreads = get_open_spreads(account=account)

    # Gross exposure
    gross = sum(
        s.get("debit", 0) * s.get("contracts", 1) * 100
        for s in open_spreads
    )
    gross_limit = min(MAX_GROSS_EXPOSURE_USD, ACCOUNT_SIZE * MAX_GROSS_EXPOSURE_PCT)
    gross_pct = round(gross / gross_limit * 100, 1) if gross_limit > 0 else 0

    # Ticker exposure breakdown
    ticker_risk = {}
    for s in open_spreads:
        t = s.get("ticker", "?")
        r = s.get("debit", 0) * s.get("contracts", 1) * 100
        ticker_risk[t] = ticker_risk.get(t, 0) + r

    ticker_limit = min(MAX_TICKER_EXPOSURE_USD, ACCOUNT_SIZE * MAX_TICKER_EXPOSURE_PCT)

    # Sector breakdown
    sector_counts = {}
    for s in open_spreads:
        t = s.get("ticker", "?")
        sector = SECTOR_MAP.get(t, "other")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    # Daily P/L
    daily_pnl = _calc_daily_realized_pnl(account)
    daily_limit = min(DAILY_LOSS_LIMIT_USD, ACCOUNT_SIZE * DAILY_LOSS_LIMIT_PCT)

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
    }
