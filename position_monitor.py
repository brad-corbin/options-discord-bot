# position_monitor.py
# ═══════════════════════════════════════════════════════════════════
# Unified Position Monitor — Live Option P&L Tracking for ALL Trades
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Tracks the actual option contract value in real-time for every
# trade type: active scanner, swing, income, conviction, AND shadow.
#
# Uses the existing Schwab streaming infrastructure:
#   build_occ_symbol() → subscribe_specific() → get_live_premium()
#
# Call register_position() from any engine when a trade fires.
# Call poll_all() on each monitoring cycle (~60s) to update prices.
# Call cleanup_expired() nightly to finalize and log results.
#
# Usage:
#   from position_monitor import PositionMonitor
#   pm = PositionMonitor(sheet_fn=_append_google_sheet_row)
#   
#   # When ANY trade fires (swing, income, active, conviction, shadow):
#   pm.register_position(
#       ticker="TSLA", direction="bull", trade_type="swing",
#       occ_symbol="TSLA260417C00250000", entry_mid=5.25,
#       expiry="2026-04-17", strike=250.0, option_type="call",
#       entry_spot=248.50, metadata={"fib": "50.0", "confidence": 78},
#   )
#
#   # On each poll cycle (~60s):
#   alerts = pm.poll_all()
#
#   # Nightly after close:
#   pm.cleanup_expired()
# ═══════════════════════════════════════════════════════════════════

import logging
import time
import json
import threading
from datetime import datetime, timezone, date, timedelta
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Callable

log = logging.getLogger(__name__)

POSITION_STORE_KEY = "position_monitor:positions"


@dataclass
class MonitoredPosition:
    """A position being tracked for live option P&L."""
    # Identity
    pos_id: str = ""                     # unique ID
    ticker: str = ""
    direction: str = ""                  # "bull" or "bear"
    trade_type: str = ""                 # "active", "swing", "income", "conviction", "shadow"
    occ_symbol: str = ""                 # OCC option symbol for streaming

    # Option contract details
    option_type: str = ""                # "call" or "put"
    strike: float = 0.0
    expiry: str = ""                     # "YYYY-MM-DD"

    # Entry
    entry_mid: float = 0.0               # option mid price at entry
    entry_spot: float = 0.0              # underlying price at entry
    entry_time: str = ""                 # ISO timestamp
    entry_date: str = ""                 # "YYYY-MM-DD"

    # Live tracking (updated every poll)
    current_mid: float = 0.0             # latest option mid
    current_spot: float = 0.0            # latest underlying price
    last_update: str = ""                # ISO timestamp of last update

    # Peak tracking
    peak_mid: float = 0.0                # highest option mid seen
    peak_date: str = ""                  # date of peak
    peak_spot: float = 0.0              # underlying price at peak
    peak_pnl_pct: float = 0.0           # (peak_mid - entry_mid) / entry_mid * 100
    peak_pnl_dollars: float = 0.0       # peak_mid - entry_mid per contract

    # Trough tracking (for credit spreads / income)
    trough_mid: float = 999999.0         # lowest option mid (for puts that gain by going down)

    # Final
    final_mid: float = 0.0               # last mid before expiry
    final_pnl_pct: float = 0.0          # P&L if held to expiry
    final_pnl_dollars: float = 0.0
    is_expired: bool = False
    is_closed: bool = False              # manually marked closed
    close_reason: str = ""

    # Result
    win_at_peak: bool = False            # was it ever profitable?
    win_at_close: bool = False           # profitable at expiry/close?

    # Metadata (flexible per engine)
    metadata: dict = field(default_factory=dict)

    # v7.2: Silent Thesis Layer fields — structural alignment data at entry
    thesis_direction: str = ""           # thesis bias at entry (BULLISH/BEARISH/NEUTRAL)
    thesis_aligned: str = ""             # YES/NO/NEUTRAL — flow aligned with thesis?
    thesis_gex_sign: str = ""            # GEX sign from morning thesis
    thesis_gamma_flip_vs_spot: str = ""  # "above" or "below" — spot vs gamma flip
    thesis_put_wall: float = 0.0         # put wall level from thesis
    thesis_call_wall: float = 0.0        # call wall level from thesis

    # Snapshot history (optional — last N mid prices for charting)
    mid_snapshots: list = field(default_factory=list)  # [{"ts": "...", "mid": 1.23, "spot": 200.5}]
    MAX_SNAPSHOTS: int = 100

    def update_mid(self, mid: float, spot: float = 0.0):
        """Called on every poll. Updates current, peak, and snapshots."""
        if mid <= 0:
            return

        now = datetime.now(timezone.utc)
        self.current_mid = mid
        self.current_spot = spot
        self.last_update = now.isoformat()

        # Peak tracking
        if mid > self.peak_mid:
            self.peak_mid = mid
            self.peak_date = now.strftime("%Y-%m-%d")
            self.peak_spot = spot
            if self.entry_mid > 0:
                self.peak_pnl_pct = round((mid - self.entry_mid) / self.entry_mid * 100, 2)
                self.peak_pnl_dollars = round(mid - self.entry_mid, 2)

        # Trough (for income/credit spreads where you want lowest)
        if mid < self.trough_mid:
            self.trough_mid = mid

        self.win_at_peak = self.peak_mid > self.entry_mid if self.entry_mid > 0 else False

        # Snapshot (throttled to avoid bloat)
        snap = {"ts": now.isoformat()[:19], "mid": round(mid, 2)}
        if spot > 0:
            snap["spot"] = round(spot, 2)
        self.mid_snapshots.append(snap)
        if len(self.mid_snapshots) > self.MAX_SNAPSHOTS:
            # Keep every Nth to compress
            self.mid_snapshots = self.mid_snapshots[::2]

    def finalize(self):
        """Called when position expires or is closed."""
        self.final_mid = self.current_mid
        if self.entry_mid > 0:
            self.final_pnl_pct = round(
                (self.final_mid - self.entry_mid) / self.entry_mid * 100, 2)
            self.final_pnl_dollars = round(self.final_mid - self.entry_mid, 2)
        self.win_at_close = self.final_mid > self.entry_mid if self.entry_mid > 0 else False

    def to_sheet_row(self) -> dict:
        """Flatten to a dict suitable for Google Sheets logging."""
        return {
            "pos_id": self.pos_id,
            "ticker": self.ticker,
            "direction": self.direction,
            "trade_type": self.trade_type,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "occ_symbol": self.occ_symbol,
            "entry_date": self.entry_date,
            "entry_mid": self.entry_mid,
            "entry_spot": self.entry_spot,
            "peak_mid": self.peak_mid,
            "peak_date": self.peak_date,
            "peak_pnl_pct": self.peak_pnl_pct,
            "peak_pnl_dollars": self.peak_pnl_dollars,
            "final_mid": self.final_mid,
            "final_pnl_pct": self.final_pnl_pct,
            "final_pnl_dollars": self.final_pnl_dollars,
            "win_at_peak": self.win_at_peak,
            "win_at_close": self.win_at_close,
            "close_reason": self.close_reason,
            "is_expired": self.is_expired,
            "num_snapshots": len(self.mid_snapshots),
            # v7.2: Silent Thesis Layer — structural alignment at entry
            "thesis_direction": self.thesis_direction,
            "thesis_aligned": self.thesis_aligned,
            "thesis_gex_sign": self.thesis_gex_sign,
            "thesis_gamma_flip_vs_spot": self.thesis_gamma_flip_vs_spot,
            "thesis_put_wall": self.thesis_put_wall,
            "thesis_call_wall": self.thesis_call_wall,
        }


class PositionMonitor:
    """Unified live option P&L tracker for all trade types."""

    # Google Sheets tab names per trade type
    SHEET_TABS = {
        "active":     "position_tracking_active",
        "swing":      "position_tracking_swing",
        "income":     "position_tracking_income",
        "conviction": "position_tracking_conviction",
        "shadow":     "position_tracking_shadow",
    }

    def __init__(self, persistent_state=None, sheet_fn=None, spot_fn=None):
        """
        Args:
            persistent_state: PersistentState for persistence across restarts
            sheet_fn: function(tab, values_list, token) to append to Google Sheets
                      or function(filename, fieldnames, row) — we handle both
            spot_fn: function(ticker) -> float for underlying price
        """
        self._state = persistent_state
        self._sheet_fn = sheet_fn
        self._spot_fn = spot_fn
        self._lock = threading.Lock()
        self._positions: Dict[str, MonitoredPosition] = {}
        self._subscribed_symbols: set = set()

        # Load from persistence
        self._load()

    def _load(self):
        """Load positions from persistent state."""
        if not self._state:
            return
        try:
            data = self._state._json_get(POSITION_STORE_KEY)
            if data:
                for pos_data in data:
                    pos = MonitoredPosition(**{k: v for k, v in pos_data.items()
                                               if k in MonitoredPosition.__dataclass_fields__})
                    self._positions[pos.pos_id] = pos
                log.info(f"PositionMonitor loaded {len(self._positions)} positions")
        except Exception as e:
            log.warning(f"PositionMonitor load failed: {e}")

    def _save(self):
        """Persist positions."""
        if not self._state:
            return
        try:
            data = [asdict(p) for p in self._positions.values()]
            self._state._json_set(POSITION_STORE_KEY, data, ttl=86400 * 30)
        except Exception as e:
            log.debug(f"PositionMonitor save failed: {e}")

    def register_position(
        self,
        ticker: str,
        direction: str,
        trade_type: str,
        occ_symbol: str,
        entry_mid: float,
        expiry: str,
        strike: float,
        option_type: str,
        entry_spot: float = 0.0,
        metadata: dict = None,
    ) -> str:
        """Register a new position for live tracking.
        
        Call this from ANY engine when a trade fires or a shadow signal is logged.
        Returns the position ID.
        """
        import uuid
        now = datetime.now(timezone.utc)
        pos_id = f"{trade_type[:3]}_{ticker}_{str(uuid.uuid4())[:6]}"

        pos = MonitoredPosition(
            pos_id=pos_id,
            ticker=ticker.upper(),
            direction=direction,
            trade_type=trade_type,
            occ_symbol=occ_symbol,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            entry_mid=entry_mid,
            entry_spot=entry_spot,
            entry_time=now.isoformat(),
            entry_date=now.strftime("%Y-%m-%d"),
            current_mid=entry_mid,
            peak_mid=entry_mid,
            peak_date=now.strftime("%Y-%m-%d"),
            peak_spot=entry_spot,
            metadata=metadata or {},
        )

        with self._lock:
            self._positions[pos_id] = pos

        # Subscribe to streaming
        self._subscribe(occ_symbol)

        # v7.2: Populate thesis alignment fields at entry
        self._populate_thesis_fields(pos)

        self._save()
        self._log_open_to_sheets(pos)  # v7.1: log to sheets immediately on open
        log.info(f"PositionMonitor: registered {trade_type} {ticker} {direction} "
                 f"OCC={occ_symbol} entry=${entry_mid:.2f} exp={expiry}")

        return pos_id

    def _subscribe(self, occ_symbol: str):
        """Subscribe to Schwab streaming for this option."""
        if occ_symbol in self._subscribed_symbols:
            return
        try:
            from schwab_stream import get_option_symbol_manager
            osm = get_option_symbol_manager()
            if osm:
                osm.subscribe_specific([occ_symbol])
                self._subscribed_symbols.add(occ_symbol)
                log.info(f"PositionMonitor: subscribed {occ_symbol}")
        except Exception as e:
            log.debug(f"PositionMonitor subscribe failed for {occ_symbol}: {e}")

    def _populate_thesis_fields(self, pos: MonitoredPosition):
        """v7.2: Populate thesis alignment fields at position entry time.

        Looks up the morning thesis for this ticker and records structural
        alignment data. This is OBSERVATIONAL — it does not gate or block.
        The data enables filtering in Google Sheets to measure whether
        thesis alignment improves win rate.
        """
        try:
            from thesis_monitor import get_engine
            thesis = get_engine().get_thesis(pos.ticker)
            if not thesis:
                return

            pos.thesis_direction = thesis.bias or ""
            pos.thesis_gex_sign = thesis.gex_sign or ""

            # Determine alignment
            thesis_bullish = thesis.bias in ("BULLISH", "LEAN_BULLISH") or thesis.bias_score > 2
            thesis_bearish = thesis.bias in ("BEARISH", "LEAN_BEARISH") or thesis.bias_score < -2
            flow_bullish = (pos.direction or "").lower() in ("bull", "bullish", "long")

            if (flow_bullish and thesis_bullish) or (not flow_bullish and thesis_bearish):
                pos.thesis_aligned = "YES"
            elif (flow_bullish and thesis_bearish) or (not flow_bullish and thesis_bullish):
                pos.thesis_aligned = "NO"
            else:
                pos.thesis_aligned = "NEUTRAL"

            # Gamma flip vs spot
            lvl = thesis.levels
            if lvl.gamma_flip and pos.entry_spot > 0:
                pos.thesis_gamma_flip_vs_spot = (
                    "above" if pos.entry_spot > lvl.gamma_flip else "below")

            if lvl.put_wall:
                pos.thesis_put_wall = round(lvl.put_wall, 2)
            if lvl.call_wall:
                pos.thesis_call_wall = round(lvl.call_wall, 2)

            log.debug(f"Thesis fields populated: {pos.ticker} aligned={pos.thesis_aligned} "
                      f"dir={pos.thesis_direction} gex={pos.thesis_gex_sign}")

        except Exception as e:
            log.debug(f"Thesis field population failed for {pos.ticker}: {e}")

    def _get_thesis_proximity(self, ticker: str, spot: float) -> str:
        """v7.2: Check if spot is near any thesis levels for exit alert context.

        Returns a string like '📍 price approaching put wall at $358.00' or ''.
        """
        try:
            from thesis_monitor import get_engine
            thesis = get_engine().get_thesis(ticker)
            if not thesis or spot <= 0:
                return ""

            lvl = thesis.levels
            near = []
            for name, val in [("put wall", lvl.put_wall), ("call wall", lvl.call_wall),
                              ("γ-flip", lvl.gamma_flip), ("EM low", lvl.em_low),
                              ("EM high", lvl.em_high)]:
                if val and val > 0:
                    dist_pct = abs(spot - val) / spot * 100
                    if dist_pct < 0.5:  # within 0.5% of spot
                        direction = ("at" if dist_pct < 0.1 else
                                     "approaching" if spot < val else "testing")
                        near.append(f"📍 price {direction} {name} at ${val:.2f}")
            return "\n".join(near) if near else ""

        except Exception as e:
            log.debug(f"Thesis proximity check failed for {ticker}: {e}")
            return ""

    def poll_all(self) -> List[dict]:
        """Poll live prices for all open positions.
        Call on every monitoring cycle (~60s).
        Returns list of alert dicts (for swing trail alerts etc).
        """
        alerts = []

        try:
            from schwab_stream import get_live_premium
        except ImportError:
            return alerts

        with self._lock:
            positions = list(self._positions.values())

        for pos in positions:
            if pos.is_expired or pos.is_closed:
                continue

            # Check if expired
            if pos.expiry:
                try:
                    exp_date = datetime.strptime(pos.expiry, "%Y-%m-%d").date()
                    if date.today() > exp_date:
                        pos.is_expired = True
                        pos.finalize()
                        pos.close_reason = "expired"
                        self._log_to_sheets(pos)
                        log.info(f"PositionMonitor: {pos.pos_id} expired. "
                                 f"Peak: ${pos.peak_mid:.2f} ({pos.peak_pnl_pct:+.1f}%) "
                                 f"Final: ${pos.final_mid:.2f} ({pos.final_pnl_pct:+.1f}%)")
                        continue
                except ValueError:
                    pass

            # Get live premium
            mid = None
            try:
                mid = get_live_premium(pos.occ_symbol)
            except Exception:
                pass

            if not mid or mid <= 0:
                continue

            # Get underlying spot
            spot = 0.0
            if self._spot_fn:
                try:
                    spot = self._spot_fn(pos.ticker) or 0.0
                except Exception:
                    pass

            old_peak = pos.peak_mid
            pos.update_mid(mid, spot)

            # Log new peaks for non-shadow trades
            if mid > old_peak and pos.trade_type != "shadow":
                if pos.entry_mid > 0:
                    pnl_pct = (mid - pos.entry_mid) / pos.entry_mid * 100
                    if pnl_pct >= 20:  # only alert on meaningful peaks (20%+)
                        # v7.2: Thesis level proximity for exit context
                        _prox = self._get_thesis_proximity(pos.ticker, spot) if spot > 0 else ""
                        _prox_line = f"\n{_prox}" if _prox else ""
                        alerts.append({
                            "msg": (
                                f"📈 OPTION PEAK — {pos.ticker} {pos.trade_type} {pos.direction}\n\n"
                                f"Option: {pos.occ_symbol}\n"
                                f"Entry: ${pos.entry_mid:.2f} → Peak: ${mid:.2f} ({pnl_pct:+.1f}%)\n"
                                f"Spot: ${spot:.2f}\n"
                                f"Consider taking profit."
                                f"{_prox_line}"
                            ),
                            "type": "position_peak",
                            "priority": 3,
                            "alert_key": f"pos_peak_{pos.pos_id}_{int(pnl_pct//20)}",
                        })

        self._save()
        return alerts

    def close_position(self, pos_id: str, reason: str = "manual"):
        """Mark a position as closed (e.g., user took profit)."""
        with self._lock:
            pos = self._positions.get(pos_id)
        if not pos:
            return
        pos.is_closed = True
        pos.close_reason = reason
        pos.finalize()
        self._log_to_sheets(pos)
        self._save()
        log.info(f"PositionMonitor: {pos_id} closed ({reason}). "
                 f"Peak: ${pos.peak_mid:.2f} ({pos.peak_pnl_pct:+.1f}%) "
                 f"Final: ${pos.final_mid:.2f} ({pos.final_pnl_pct:+.1f}%)")

    def cleanup_expired(self):
        """Finalize all expired positions. Call nightly after close."""
        with self._lock:
            for pos in self._positions.values():
                if pos.is_expired or pos.is_closed:
                    continue
                if pos.expiry:
                    try:
                        exp_date = datetime.strptime(pos.expiry, "%Y-%m-%d").date()
                        if date.today() > exp_date:
                            pos.is_expired = True
                            pos.finalize()
                            pos.close_reason = "expired"
                            self._log_to_sheets(pos)
                    except ValueError:
                        pass
        self._save()

    def _log_to_sheets(self, pos: MonitoredPosition):
        """Log finalized position to Google Sheets."""
        if not self._sheet_fn:
            return

        tab = self.SHEET_TABS.get(pos.trade_type, "position_tracking_other")
        row = pos.to_sheet_row()
        row["log_type"] = "CLOSE"
        # Add metadata fields
        for k, v in (pos.metadata or {}).items():
            row[f"meta_{k}"] = v

        try:
            fieldnames = list(row.keys())
            self._sheet_fn(tab, fieldnames, row)
            log.info(f"PositionMonitor sheets CLOSE logged: {pos.pos_id}")
        except Exception as e:
            log.warning(f"PositionMonitor sheets CLOSE log failed for {pos.pos_id} {pos.ticker}: {e}")

    def _log_open_to_sheets(self, pos: MonitoredPosition):
        """Log position OPEN to Google Sheets immediately on registration."""
        if not self._sheet_fn:
            return

        tab = self.SHEET_TABS.get(pos.trade_type, "position_tracking_other")
        row = {
            "log_type": "OPEN",
            "pos_id": pos.pos_id,
            "ticker": pos.ticker,
            "direction": pos.direction,
            "trade_type": pos.trade_type,
            "option_type": pos.option_type,
            "strike": pos.strike,
            "expiry": pos.expiry,
            "occ_symbol": pos.occ_symbol,
            "entry_date": pos.entry_date,
            "entry_time": pos.entry_time,
            "entry_mid": pos.entry_mid,
            "entry_spot": pos.entry_spot,
            # v7.2: Silent Thesis Layer — structural alignment at entry
            "thesis_direction": pos.thesis_direction,
            "thesis_aligned": pos.thesis_aligned,
            "thesis_gex_sign": pos.thesis_gex_sign,
            "thesis_gamma_flip_vs_spot": pos.thesis_gamma_flip_vs_spot,
            "thesis_put_wall": pos.thesis_put_wall,
            "thesis_call_wall": pos.thesis_call_wall,
        }
        for k, v in (pos.metadata or {}).items():
            row[f"meta_{k}"] = v

        try:
            fieldnames = list(row.keys())
            self._sheet_fn(tab, fieldnames, row)
            log.info(f"PositionMonitor sheets OPEN logged: {pos.pos_id} {pos.ticker} {pos.trade_type}")
        except Exception as e:
            log.warning(f"PositionMonitor sheets OPEN log failed for {pos.pos_id} {pos.ticker}: {e}")

    def get_open_positions(self, trade_type: str = None) -> List[MonitoredPosition]:
        """Get all open (non-expired, non-closed) positions."""
        with self._lock:
            positions = [p for p in self._positions.values()
                         if not p.is_expired and not p.is_closed]
        if trade_type:
            positions = [p for p in positions if p.trade_type == trade_type]
        return positions

    def get_position(self, pos_id: str) -> Optional[MonitoredPosition]:
        """Get a specific position by ID."""
        return self._positions.get(pos_id)

    def format_status_report(self, trade_type: str = None) -> str:
        """Generate a status report of open positions."""
        positions = self.get_open_positions(trade_type)
        if not positions:
            return f"📊 No open {trade_type or 'monitored'} positions."

        lines = [
            f"📊 OPEN POSITIONS — {trade_type.upper() if trade_type else 'ALL'}",
            "═" * 40,
        ]

        for pos in sorted(positions, key=lambda p: p.peak_pnl_pct, reverse=True):
            pnl_now = 0
            if pos.entry_mid > 0 and pos.current_mid > 0:
                pnl_now = (pos.current_mid - pos.entry_mid) / pos.entry_mid * 100
            emoji = "🟢" if pnl_now > 0 else "🔴"
            lines.append(
                f"{emoji} {pos.ticker} {pos.trade_type} {pos.direction} | "
                f"Entry: ${pos.entry_mid:.2f} → Now: ${pos.current_mid:.2f} ({pnl_now:+.1f}%) | "
                f"Peak: ${pos.peak_mid:.2f} ({pos.peak_pnl_pct:+.1f}%) | "
                f"Exp: {pos.expiry}"
            )

        return "\n".join(lines)

    def format_shadow_scorecard(self) -> str:
        """Generate scorecard specifically for shadow positions."""
        with self._lock:
            shadows = [p for p in self._positions.values()
                       if p.trade_type == "shadow" and (p.is_expired or p.is_closed)]

        if not shadows:
            return "📊 Shadow Scorecard: no expired shadow positions yet."

        total = len(shadows)
        win_at_peak = sum(1 for p in shadows if p.win_at_peak)
        win_at_close = sum(1 for p in shadows if p.win_at_close)
        avg_peak_pnl = sum(p.peak_pnl_pct for p in shadows) / total
        avg_final_pnl = sum(p.final_pnl_pct for p in shadows) / total

        lines = [
            "📊 SHADOW POSITION SCORECARD",
            "═" * 40,
            f"Total shadow positions tracked: {total}",
            "",
            f"Win at PEAK (option was profitable at some point):",
            f"  {win_at_peak}/{total} = {win_at_peak/total*100:.0f}%",
            f"  Avg peak P&L: {avg_peak_pnl:+.1f}%",
            "",
            f"Win at CLOSE (held to expiry, still profitable):",
            f"  {win_at_close}/{total} = {win_at_close/total*100:.0f}%",
            f"  Avg final P&L: {avg_final_pnl:+.1f}%",
            "",
            f"Money left on table (peak vs close):",
            f"  Avg: {avg_peak_pnl - avg_final_pnl:+.1f}% per position",
        ]

        # By filter category (from metadata)
        categories = {}
        for p in shadows:
            cat = p.metadata.get("filter_category", "unknown")
            if cat not in categories:
                categories[cat] = {"n": 0, "peak_wins": 0, "close_wins": 0,
                                   "peak_pnl": 0, "final_pnl": 0}
            categories[cat]["n"] += 1
            if p.win_at_peak:
                categories[cat]["peak_wins"] += 1
            if p.win_at_close:
                categories[cat]["close_wins"] += 1
            categories[cat]["peak_pnl"] += p.peak_pnl_pct
            categories[cat]["final_pnl"] += p.final_pnl_pct

        if categories:
            lines.append("")
            lines.append("By filter category:")
            for cat, data in sorted(categories.items(), key=lambda x: -x[1]["n"]):
                n = data["n"]
                peak_wr = data["peak_wins"] / n * 100
                close_wr = data["close_wins"] / n * 100
                avg_pk = data["peak_pnl"] / n
                emoji = "✅" if peak_wr < 50 else "⚠️" if peak_wr < 60 else "🔴"
                lines.append(
                    f"  {emoji} {cat}: {n} pos | "
                    f"Peak WR: {peak_wr:.0f}% ({avg_pk:+.1f}%) | "
                    f"Close WR: {close_wr:.0f}%"
                )
                if peak_wr >= 55 and n >= 20:
                    lines.append(
                        f"     ⚠️ Filter may be blocking winners — review"
                    )

        return "\n".join(lines)
