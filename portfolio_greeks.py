# portfolio_greeks.py
# ═══════════════════════════════════════════════════════════════════
# Portfolio-Level Greeks Aggregator
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Tracks aggregate delta, gamma, vega, theta across all open positions.
# Enforces portfolio-level exposure limits before new trades are approved.
#
# An institution doesn't ask "is this one trade good?" — it asks
# "is this trade good given everything else I'm already holding?"
#
# Three correlated bear put positions = 3x the directional risk
# that any single position card shows.
#
# Usage:
#   from portfolio_greeks import PortfolioGreeks
#   pg = PortfolioGreeks()
#   pg.register_position(...)
#   allowed, reason = pg.check_new_trade(...)
#   summary = pg.get_summary()
# ═══════════════════════════════════════════════════════════════════

import time
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

# Portfolio exposure limits
MAX_TOTAL_DELTA          = 3.0     # max absolute portfolio delta (sum of all positions)
MAX_TOTAL_DELTA_ONE_SIDE = 2.5     # max net long OR net short delta
MAX_TOTAL_VEGA           = 500.0   # max total vega exposure ($)
MAX_TOTAL_RISK_USD       = 5000    # max total premium at risk across all positions
MAX_OPEN_POSITIONS       = 8       # max simultaneous open positions
MAX_PER_SECTOR           = 3       # max positions in one sector
MAX_SINGLE_NAME_PCT      = 0.35    # max % of total risk in one ticker

# Correlation matrix (simplified — sector-based)
# Tickers in the same sector are treated as ~0.80 correlated
# Tickers in different sectors are treated as ~0.30 correlated
SECTOR_CORRELATION       = 0.80
CROSS_SECTOR_CORRELATION = 0.30

# Sector mapping (reuses trading_rules if available)
try:
    from trading_rules import SWING_SECTOR_MAP as _SECTOR_MAP
except ImportError:
    _SECTOR_MAP = {}


# ═══════════════════════════════════════════════════════════
# POSITION DATA
# ═══════════════════════════════════════════════════════════

@dataclass
class Position:
    """One open position with its Greeks."""
    ticker: str
    direction: str          # "LONG" or "SHORT"
    trade_type: str         # "spread", "long_option"
    option_type: str        # "call" or "put"
    contracts: int          # number of contracts
    entry_price: float      # premium paid per contract
    total_risk: float       # max loss in dollars
    # Greeks (per contract, will be multiplied by contracts for portfolio)
    delta: float = 0.0      # directional exposure
    gamma: float = 0.0      # delta sensitivity
    vega: float = 0.0       # vol sensitivity ($ per 1% IV change)
    theta: float = 0.0      # time decay ($ per day)
    # Metadata
    sector: str = "OTHER"
    entry_time: float = 0.0
    trade_id: str = ""
    spot_at_entry: float = 0.0


# ═══════════════════════════════════════════════════════════
# PORTFOLIO AGGREGATOR
# ═══════════════════════════════════════════════════════════

class PortfolioGreeks:
    """
    Tracks aggregate Greeks across all open positions.
    Thread-safe for concurrent access from scanner + thesis monitor.
    """

    def __init__(self):
        self._positions: Dict[str, Position] = {}  # trade_id → Position
        self._lock = threading.Lock()

    def _get_sector(self, ticker: str) -> str:
        """Map ticker to sector."""
        t = ticker.upper()
        for sector, tickers in _SECTOR_MAP.items():
            if t in tickers:
                return sector
        return "OTHER"

    # ── Position Management ──

    def register_position(
        self,
        trade_id: str,
        ticker: str,
        direction: str,
        trade_type: str = "spread",
        option_type: str = "put",
        contracts: int = 1,
        entry_price: float = 0.0,
        total_risk: float = 0.0,
        delta: float = 0.0,
        gamma: float = 0.0,
        vega: float = 0.0,
        theta: float = 0.0,
        spot: float = 0.0,
    ):
        """Register a new open position."""
        with self._lock:
            # Normalize delta sign: SHORT positions have negative delta contribution
            # (even if the option delta is positive, a short position means
            #  you benefit from price going DOWN)
            if direction == "SHORT" and delta > 0:
                delta = -delta
            elif direction == "LONG" and delta < 0:
                delta = -delta  # long puts have negative delta, keep it

            pos = Position(
                ticker=ticker.upper(),
                direction=direction,
                trade_type=trade_type,
                option_type=option_type,
                contracts=contracts,
                entry_price=entry_price,
                total_risk=total_risk,
                delta=delta,
                gamma=gamma,
                vega=vega,
                theta=theta,
                sector=self._get_sector(ticker),
                entry_time=time.time(),
                trade_id=trade_id,
                spot_at_entry=spot,
            )

            self._positions[trade_id] = pos
            log.info(f"Portfolio: registered {ticker} {direction} "
                     f"(delta={delta:.3f}x{contracts}, "
                     f"vega=${vega:.2f}, risk=${total_risk:.0f}, "
                     f"sector={pos.sector})")

    def close_position(self, trade_id: str, reason: str = ""):
        """Remove a closed position."""
        with self._lock:
            pos = self._positions.pop(trade_id, None)
            if pos:
                log.info(f"Portfolio: closed {pos.ticker} {pos.direction} "
                         f"({reason})")

    def update_greeks(self, trade_id: str, delta: float = None,
                      gamma: float = None, vega: float = None, theta: float = None):
        """Update Greeks for a position (e.g., from live option quotes)."""
        with self._lock:
            pos = self._positions.get(trade_id)
            if pos:
                if delta is not None:
                    pos.delta = delta
                if gamma is not None:
                    pos.gamma = gamma
                if vega is not None:
                    pos.vega = vega
                if theta is not None:
                    pos.theta = theta

    # ── Aggregate Calculations ──

    def _aggregate(self) -> dict:
        """Compute portfolio-level aggregates. Caller must hold lock."""
        if not self._positions:
            return {
                "total_delta": 0.0, "total_gamma": 0.0,
                "total_vega": 0.0, "total_theta": 0.0,
                "total_risk": 0.0, "position_count": 0,
                "long_delta": 0.0, "short_delta": 0.0,
                "sector_counts": {}, "ticker_risk": {},
            }

        total_delta = 0.0
        total_gamma = 0.0
        total_vega = 0.0
        total_theta = 0.0
        total_risk = 0.0
        long_delta = 0.0
        short_delta = 0.0
        sector_counts: Dict[str, int] = {}
        ticker_risk: Dict[str, float] = {}

        for pos in self._positions.values():
            pos_delta = pos.delta * pos.contracts
            pos_gamma = pos.gamma * pos.contracts
            pos_vega = pos.vega * pos.contracts
            pos_theta = pos.theta * pos.contracts

            total_delta += pos_delta
            total_gamma += pos_gamma
            total_vega += pos_vega
            total_theta += pos_theta
            total_risk += pos.total_risk

            if pos_delta > 0:
                long_delta += pos_delta
            else:
                short_delta += pos_delta

            sector_counts[pos.sector] = sector_counts.get(pos.sector, 0) + 1
            ticker_risk[pos.ticker] = ticker_risk.get(pos.ticker, 0) + pos.total_risk

        return {
            "total_delta": round(total_delta, 3),
            "total_gamma": round(total_gamma, 4),
            "total_vega": round(total_vega, 2),
            "total_theta": round(total_theta, 2),
            "total_risk": round(total_risk, 2),
            "position_count": len(self._positions),
            "long_delta": round(long_delta, 3),
            "short_delta": round(short_delta, 3),
            "sector_counts": sector_counts,
            "ticker_risk": ticker_risk,
        }

    # ── Pre-Trade Check ──

    def check_new_trade(
        self,
        ticker: str,
        direction: str,
        delta: float,
        contracts: int = 1,
        total_risk: float = 0.0,
        vega: float = 0.0,
    ) -> Tuple[bool, str]:
        """
        Check if a new trade is allowed given portfolio exposure.

        Returns (allowed: bool, reason: str).
        If not allowed, reason explains which limit was hit.
        """
        with self._lock:
            agg = self._aggregate()

            # Position count limit
            if agg["position_count"] >= MAX_OPEN_POSITIONS:
                return False, (f"Max positions reached ({agg['position_count']}/{MAX_OPEN_POSITIONS})")

            # Sector concentration
            sector = self._get_sector(ticker)
            sector_count = agg["sector_counts"].get(sector, 0)
            if sector_count >= MAX_PER_SECTOR:
                return False, (f"Sector {sector} at max ({sector_count}/{MAX_PER_SECTOR})")

            # Total risk limit
            new_total_risk = agg["total_risk"] + total_risk
            if new_total_risk > MAX_TOTAL_RISK_USD:
                return False, (f"Total risk ${new_total_risk:.0f} > ${MAX_TOTAL_RISK_USD} limit")

            # Single-name concentration
            ticker_risk = agg["ticker_risk"].get(ticker.upper(), 0) + total_risk
            if new_total_risk > 0 and ticker_risk / new_total_risk > MAX_SINGLE_NAME_PCT:
                return False, (f"{ticker} concentration {ticker_risk/new_total_risk:.0%} > "
                              f"{MAX_SINGLE_NAME_PCT:.0%} limit")

            # Delta exposure
            new_pos_delta = delta * contracts
            if direction == "SHORT" and new_pos_delta > 0:
                new_pos_delta = -new_pos_delta

            new_total_delta = agg["total_delta"] + new_pos_delta

            if abs(new_total_delta) > MAX_TOTAL_DELTA:
                return False, (f"Total delta {new_total_delta:+.2f} would exceed "
                              f"±{MAX_TOTAL_DELTA} limit")

            # One-sided delta (prevents stacking all short or all long)
            if new_pos_delta > 0:
                new_long = agg["long_delta"] + new_pos_delta
                if new_long > MAX_TOTAL_DELTA_ONE_SIDE:
                    return False, (f"Long delta {new_long:.2f} would exceed "
                                  f"{MAX_TOTAL_DELTA_ONE_SIDE} limit")
            else:
                new_short = agg["short_delta"] + new_pos_delta
                if abs(new_short) > MAX_TOTAL_DELTA_ONE_SIDE:
                    return False, (f"Short delta {new_short:.2f} would exceed "
                                  f"-{MAX_TOTAL_DELTA_ONE_SIDE} limit")

            # Vega exposure
            new_total_vega = agg["total_vega"] + (vega * contracts)
            if abs(new_total_vega) > MAX_TOTAL_VEGA:
                return False, (f"Total vega ${new_total_vega:.0f} would exceed "
                              f"${MAX_TOTAL_VEGA} limit")

            # Correlation warning (not a hard block, just a note)
            warnings = []
            for pos in self._positions.values():
                if pos.sector == sector and pos.direction == direction:
                    warnings.append(f"Correlated with {pos.ticker} {pos.direction} "
                                   f"(same sector {sector})")

            reason = "Approved"
            if warnings:
                reason += " ⚠️ " + "; ".join(warnings[:2])

            return True, reason

    # ── Summary ──

    def get_summary(self) -> dict:
        """Portfolio summary for Telegram display."""
        with self._lock:
            agg = self._aggregate()
            positions = []
            for pos in self._positions.values():
                positions.append({
                    "ticker": pos.ticker,
                    "direction": pos.direction,
                    "type": pos.trade_type,
                    "delta": round(pos.delta * pos.contracts, 3),
                    "contracts": pos.contracts,
                    "risk": pos.total_risk,
                    "sector": pos.sector,
                    "age_hours": round((time.time() - pos.entry_time) / 3600, 1),
                })

            return {
                **agg,
                "positions": positions,
            }

    def format_summary(self) -> str:
        """Formatted portfolio summary for Telegram."""
        s = self.get_summary()

        if s["position_count"] == 0:
            return "📊 Portfolio: No open positions"

        lines = [
            "📊 ── PORTFOLIO GREEKS ──",
            "",
            f"Positions: {s['position_count']}/{MAX_OPEN_POSITIONS}",
            f"Total Delta: {s['total_delta']:+.3f} "
            f"(long {s['long_delta']:+.3f} / short {s['short_delta']:+.3f})",
            f"Total Gamma: {s['total_gamma']:+.4f}",
            f"Total Vega:  ${s['total_vega']:+.2f}",
            f"Total Theta: ${s['total_theta']:.2f}/day",
            f"Total Risk:  ${s['total_risk']:.0f} / ${MAX_TOTAL_RISK_USD}",
            "",
        ]

        # Sector breakdown
        if s["sector_counts"]:
            sector_str = " | ".join(f"{k}: {v}" for k, v in
                                     sorted(s["sector_counts"].items()))
            lines.append(f"Sectors: {sector_str}")

        # Individual positions
        lines.append("")
        for p in s["positions"]:
            dir_icon = "🟢" if p["direction"] == "LONG" else "🔴"
            lines.append(f"  {dir_icon} {p['ticker']} {p['direction']} "
                        f"δ={p['delta']:+.3f} "
                        f"${p['risk']:.0f} risk "
                        f"({p['age_hours']:.0f}h)")

        # Exposure assessment
        lines.append("")
        abs_delta = abs(s["total_delta"])
        if abs_delta > MAX_TOTAL_DELTA * 0.8:
            lines.append(f"⚠️ DELTA EXPOSURE HIGH — {abs_delta:.2f}/{MAX_TOTAL_DELTA}")
        elif abs_delta > MAX_TOTAL_DELTA * 0.5:
            lines.append(f"🟡 Delta moderate — room for "
                        f"{MAX_TOTAL_DELTA - abs_delta:.1f} more")
        else:
            lines.append(f"✅ Delta comfortable — {abs_delta:.2f}/{MAX_TOTAL_DELTA}")

        return "\n".join(lines)

    @property
    def position_count(self) -> int:
        with self._lock:
            return len(self._positions)

    @property
    def total_delta(self) -> float:
        with self._lock:
            return sum(p.delta * p.contracts for p in self._positions.values())
