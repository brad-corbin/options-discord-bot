# persistent_state.py
# ═══════════════════════════════════════════════════════════════════
# Redis-Backed Persistent State — Survives Redeploys
#
# Provides durable storage for bot state that would otherwise die
# when Render restarts the container. All critical runtime data
# flows through this module.
#
# Categories:
#   - Flow campaigns (multi-day OI buildup/unwinding tracking)
#   - Active trades (scanner-managed positions)
#   - Swing signal cache (income scanner fib confluence)
#   - Thesis store (EM card / monitor context)
#   - ORB levels (15-min opening range)
#   - Flow alert cooldowns (prevent duplicate alerts)
#   - Intraday volume snapshots (5-min accumulation tracking)
#
# All keys have explicit TTLs to prevent stale data buildup.
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable

log = logging.getLogger(__name__)

# ── TTL Constants ──
TTL_FLOW_CAMPAIGN = 30 * 86400    # 30 days — institutional campaigns last weeks
TTL_ACTIVE_TRADE = 24 * 3600      # 24h — trades don't last longer
TTL_SWING_SIGNAL = 7 * 86400      # 7 days — income confluence window
TTL_THESIS = 24 * 3600            # 24h — rebuilt daily
TTL_ORB = 18 * 3600               # 18h — intraday only
TTL_FLOW_COOLDOWN = 2 * 3600      # 2h — alert dedup
TTL_VOLUME_SNAPSHOT = 20 * 3600   # 20h — intraday volume tracking
TTL_VOLUME_FLAG = 48 * 3600       # 48h — yesterday's flags for morning confirmation
TTL_OI_BASELINE = 50 * 3600       # 50h — survives overnight + missed morning


class PersistentState:
    """
    Redis-backed state that survives container restarts.

    Usage:
        state = PersistentState(store_get, store_set, store_scan)
        state.save_flow_campaign(campaign_data)
        campaign = state.get_flow_campaign("SPY", 680.0, "call", "2026-04-25")
    """

    def __init__(self, store_get_fn: Callable, store_set_fn: Callable,
                 store_scan_fn: Callable = None):
        """
        store_get_fn: function(key) → str or None
        store_set_fn: function(key, value_str, ttl=int)
        store_scan_fn: function(pattern) → list of keys (optional, for listing)
        """
        self._get = store_get_fn
        self._set = store_set_fn
        self._scan = store_scan_fn

    def _json_get(self, key: str) -> Optional[dict]:
        try:
            raw = self._get(key)
            if raw is not None:
                return json.loads(raw)
        except Exception as e:
            log.debug(f"PersistentState get failed for {key}: {e}")
        return None

    def _json_set(self, key: str, data: dict, ttl: int):
        try:
            self._set(key, json.dumps(data, default=str), ttl=ttl)
            return True
        except Exception as e:
            log.warning(f"PersistentState set failed for {key}: {e}")
            return False

    # ═══════════════════════════════════════════════════════
    # FLOW CAMPAIGNS — multi-day institutional positioning
    # ═══════════════════════════════════════════════════════

    def _flow_campaign_key(self, ticker: str, strike: float, side: str,
                           expiry: str) -> str:
        return f"flow_campaign:{ticker.upper()}:{strike}:{side}:{expiry}"

    def save_flow_campaign(self, campaign: dict) -> bool:
        key = self._flow_campaign_key(
            campaign["ticker"], campaign["strike"],
            campaign["side"], campaign["expiry"],
        )
        return self._json_set(key, campaign, TTL_FLOW_CAMPAIGN)

    def get_flow_campaign(self, ticker: str, strike: float, side: str,
                          expiry: str) -> Optional[dict]:
        key = self._flow_campaign_key(ticker, strike, side, expiry)
        return self._json_get(key)

    def get_all_flow_campaigns(self, ticker: str = None) -> List[dict]:
        """Get all active flow campaigns, optionally filtered by ticker."""
        if not self._scan:
            return []
        try:
            pattern = f"flow_campaign:{ticker.upper()}:*" if ticker else "flow_campaign:*"
            keys = self._scan(pattern)
            campaigns = []
            for key in keys:
                data = self._json_get(key)
                if data:
                    campaigns.append(data)
            return campaigns
        except Exception as e:
            log.debug(f"Flow campaign scan failed: {e}")
            return []

    def update_flow_campaign(self, ticker: str, strike: float, side: str,
                             expiry: str, day_entry: dict,
                             flow_type: str) -> dict:
        """
        Update or create a flow campaign with a new daily entry.
        Returns the updated campaign dict.
        """
        existing = self.get_flow_campaign(ticker, strike, side, expiry)

        if existing is None:
            campaign = {
                "ticker": ticker.upper(),
                "strike": strike,
                "side": side,
                "expiry": expiry,
                "consecutive_days": 1,
                "total_oi_change": day_entry.get("oi_change", 0),
                "daily_history": [day_entry],
                "first_spotted": day_entry.get("date", date.today().isoformat()),
                "last_confirmed": day_entry.get("date", date.today().isoformat()),
                "flow_type": flow_type,
                "peak_vol_oi_ratio": day_entry.get("vol_oi_ratio", 0),
                "price_at_start": day_entry.get("spot", 0),
                "price_at_last": day_entry.get("spot", 0),
            }
        else:
            history = existing.get("daily_history", [])
            # Don't duplicate same date
            today_str = day_entry.get("date", date.today().isoformat())
            if any(d.get("date") == today_str for d in history):
                return existing

            history.append(day_entry)
            # Keep last 30 days
            history = history[-30:]

            # Check consecutiveness
            last_date = existing.get("last_confirmed", "")
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
                today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
                gap = (today_dt - last_dt).days
                if gap <= 2:  # allow weekends
                    consecutive = existing.get("consecutive_days", 0) + 1
                else:
                    consecutive = 1  # gap too big, reset
            except (ValueError, TypeError):
                consecutive = existing.get("consecutive_days", 0) + 1

            # Check if flow type flipped
            if flow_type != existing.get("flow_type"):
                # Direction reversed — reset campaign
                consecutive = 1
                history = [day_entry]

            campaign = {
                "ticker": ticker.upper(),
                "strike": strike,
                "side": side,
                "expiry": expiry,
                "consecutive_days": consecutive,
                "total_oi_change": sum(d.get("oi_change", 0) for d in history),
                "daily_history": history,
                "first_spotted": existing.get("first_spotted", today_str),
                "last_confirmed": today_str,
                "flow_type": flow_type,
                "peak_vol_oi_ratio": max(
                    existing.get("peak_vol_oi_ratio", 0),
                    day_entry.get("vol_oi_ratio", 0),
                ),
                "price_at_start": existing.get("price_at_start", day_entry.get("spot", 0)),
                "price_at_last": day_entry.get("spot", 0),
            }

        self.save_flow_campaign(campaign)
        return campaign

    # ═══════════════════════════════════════════════════════
    # VOLUME FLAGS — intraday volume spikes for next-morning confirmation
    # ═══════════════════════════════════════════════════════

    def _volume_flag_key(self, date_str: str) -> str:
        return f"vol_flags:{date_str}"

    def save_volume_flags(self, date_str: str, flags: list) -> bool:
        """Save today's significant volume strikes for tomorrow's OI confirmation."""
        return self._json_set(self._volume_flag_key(date_str), flags, TTL_VOLUME_FLAG)

    def get_volume_flags(self, date_str: str) -> list:
        """Load a day's volume flags for OI confirmation."""
        return self._json_get(self._volume_flag_key(date_str)) or []

    def append_volume_flag(self, date_str: str, flag: dict):
        """Add a volume flag to today's list."""
        existing = self.get_volume_flags(date_str)
        # Dedup by ticker+strike+side
        dedup_key = f"{flag.get('ticker')}:{flag.get('strike')}:{flag.get('side')}"
        existing = [f for f in existing
                    if f"{f.get('ticker')}:{f.get('strike')}:{f.get('side')}" != dedup_key]
        existing.append(flag)
        self.save_volume_flags(date_str, existing)

    # ═══════════════════════════════════════════════════════
    # INTRADAY VOLUME SNAPSHOTS — track accumulation between pulls
    # ═══════════════════════════════════════════════════════

    def _vol_snapshot_key(self, ticker: str, expiry: str, date_str: str) -> str:
        return f"vol_snap:{ticker.upper()}:{expiry}:{date_str}"

    def save_volume_snapshot(self, ticker: str, expiry: str,
                            snapshot: Dict[str, int]) -> bool:
        """Save current volume at each strike. Used to detect bursts."""
        today = date.today().isoformat()
        key = self._vol_snapshot_key(ticker, expiry, today)
        return self._json_set(key, snapshot, TTL_VOLUME_SNAPSHOT)

    def get_volume_snapshot(self, ticker: str, expiry: str) -> Optional[dict]:
        today = date.today().isoformat()
        key = self._vol_snapshot_key(ticker, expiry, today)
        return self._json_get(key)

    # ═══════════════════════════════════════════════════════
    # OI BASELINES — morning snapshot for day-over-day confirmation
    # ═══════════════════════════════════════════════════════

    def _oi_baseline_key(self, ticker: str, expiry: str, date_str: str) -> str:
        return f"oi_baseline:{ticker.upper()}:{expiry}:{date_str}"

    def save_oi_baseline(self, ticker: str, expiry: str,
                         oi_data: Dict[str, int]) -> bool:
        """Save morning OI baseline. Returns False if already exists today."""
        today = date.today().isoformat()
        key = self._oi_baseline_key(ticker, expiry, today)
        existing = self._get(key)
        if existing is not None:
            return False  # already saved today
        return self._json_set(key, oi_data, TTL_OI_BASELINE)

    def get_oi_baseline(self, ticker: str, expiry: str,
                        date_str: str = None) -> Optional[dict]:
        if date_str is None:
            date_str = date.today().isoformat()
        key = self._oi_baseline_key(ticker, expiry, date_str)
        return self._json_get(key)

    def get_yesterday_oi_baseline(self, ticker: str, expiry: str) -> Optional[dict]:
        """Get yesterday's OI baseline for morning confirmation."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        baseline = self.get_oi_baseline(ticker, expiry, yesterday)
        if baseline:
            return baseline
        # Try Friday if today is Monday
        if date.today().weekday() == 0:
            friday = (date.today() - timedelta(days=3)).isoformat()
            return self.get_oi_baseline(ticker, expiry, friday)
        return None

    # ═══════════════════════════════════════════════════════
    # ACTIVE TRADES — scanner-managed positions
    # ═══════════════════════════════════════════════════════

    def _active_trade_key(self, trade_id: str) -> str:
        return f"active_trade:{trade_id}"

    def save_active_trade(self, trade_id: str, trade_data: dict) -> bool:
        return self._json_set(self._active_trade_key(trade_id), trade_data,
                              TTL_ACTIVE_TRADE)

    def get_active_trade(self, trade_id: str) -> Optional[dict]:
        return self._json_get(self._active_trade_key(trade_id))

    def remove_active_trade(self, trade_id: str):
        try:
            self._set(self._active_trade_key(trade_id), "", ttl=1)
        except Exception:
            pass

    def get_all_active_trades(self) -> List[dict]:
        if not self._scan:
            return []
        try:
            keys = self._scan("active_trade:*")
            return [self._json_get(k) for k in keys
                    if self._json_get(k) is not None]
        except Exception:
            return []

    # ═══════════════════════════════════════════════════════
    # SWING SIGNAL CACHE — persisted for income confluence
    # ═══════════════════════════════════════════════════════

    def _swing_signal_key(self, ticker: str) -> str:
        return f"swing_signal:{ticker.upper()}"

    def save_swing_signal(self, ticker: str, signal: dict) -> bool:
        return self._json_set(self._swing_signal_key(ticker), signal,
                              TTL_SWING_SIGNAL)

    def get_swing_signal(self, ticker: str) -> Optional[dict]:
        return self._json_get(self._swing_signal_key(ticker))

    def get_all_swing_signals(self) -> Dict[str, dict]:
        if not self._scan:
            return {}
        try:
            keys = self._scan("swing_signal:*")
            result = {}
            for key in keys:
                data = self._json_get(key)
                if data and data.get("ticker"):
                    result[data["ticker"]] = data
            return result
        except Exception:
            return {}

    # ═══════════════════════════════════════════════════════
    # THESIS STORE — EM card / monitor context
    # ═══════════════════════════════════════════════════════

    def _thesis_key(self, ticker: str) -> str:
        return f"thesis:{ticker.upper()}"

    def save_thesis(self, ticker: str, thesis: dict) -> bool:
        return self._json_set(self._thesis_key(ticker), thesis, TTL_THESIS)

    def get_thesis(self, ticker: str) -> Optional[dict]:
        return self._json_get(self._thesis_key(ticker))

    # ═══════════════════════════════════════════════════════
    # ORB LEVELS — 15-min opening range
    # ═══════════════════════════════════════════════════════

    def _orb_key(self, ticker: str, date_str: str) -> str:
        return f"orb:{ticker.upper()}:{date_str}"

    def save_orb(self, ticker: str, high: float, low: float) -> bool:
        today = date.today().isoformat()
        data = {"high": high, "low": low, "date": today}
        return self._json_set(self._orb_key(ticker, today), data, TTL_ORB)

    def get_orb(self, ticker: str) -> Optional[dict]:
        today = date.today().isoformat()
        return self._json_get(self._orb_key(ticker, today))

    # ═══════════════════════════════════════════════════════
    # FLOW ALERT COOLDOWNS — prevent duplicate alerts
    # ═══════════════════════════════════════════════════════

    def _cooldown_key(self, alert_key: str) -> str:
        return f"flow_cd:{alert_key}"

    def check_and_set_cooldown(self, alert_key: str,
                                cooldown_seconds: int = 1800) -> bool:
        """
        Returns True if alert should fire (not in cooldown).
        Sets cooldown if firing.
        """
        key = self._cooldown_key(alert_key)
        existing = self._get(key)
        if existing is not None:
            return False  # in cooldown
        self._set(key, "1", ttl=cooldown_seconds)
        return True

    # ═══════════════════════════════════════════════════════
    # STALK ALERTS — persistent watchlist from confirmed flow
    # ═══════════════════════════════════════════════════════

    def _stalk_key(self, ticker: str) -> str:
        return f"stalk:{ticker.upper()}"

    def save_stalk_alert(self, ticker: str, stalk: dict) -> bool:
        return self._json_set(self._stalk_key(ticker), stalk,
                              ttl=24 * 3600)  # 24h — today only

    def get_stalk_alert(self, ticker: str) -> Optional[dict]:
        return self._json_get(self._stalk_key(ticker))

    def get_all_stalk_alerts(self) -> List[dict]:
        if not self._scan:
            return []
        try:
            keys = self._scan("stalk:*")
            return [self._json_get(k) for k in keys
                    if self._json_get(k) is not None]
        except Exception:
            return []

    # ═══════════════════════════════════════════════════════
    # SHADOW SIGNAL STORAGE (intraday, 4hr TTL)
    # ═══════════════════════════════════════════════════════

    def save_shadow_signal(self, ticker: str, signal_data: dict):
        """Store a shadow signal so flow conviction can find convergence."""
        self._json_set(f"shadow:{ticker.upper()}", signal_data, ttl=14400)

    def get_shadow_signal(self, ticker: str) -> dict:
        """Get stored shadow signal for convergence checking."""
        return self._json_get(f"shadow:{ticker.upper()}")

    # ═══════════════════════════════════════════════════════
    # FLOW DIRECTION CACHE (intraday, 2hr TTL)
    # Updated on every significant+ flow detection so any
    # subsystem can query "what is the latest flow bias?"
    # ═══════════════════════════════════════════════════════

    def save_flow_direction(self, ticker: str, data: dict):
        """
        Store latest significant+ flow direction for a ticker.
        data: {direction, vol_oi, volume, flow_level, side, strike, timestamp}
        """
        self._json_set(f"flow_dir:{ticker.upper()}", data, ttl=7200)

    def get_flow_direction(self, ticker: str) -> dict:
        """Get latest flow direction. Returns None if no recent significant+ flow."""
        return self._json_get(f"flow_dir:{ticker.upper()}")

    # ── Intraday Re-hit Counter ──

    def increment_flow_rehit(self, ticker: str, strike: float, side: str) -> int:
        """
        Increment and return the intraday hit count for a specific strike.
        Resets daily via TTL (8hr). Returns the NEW count (1 = first hit, 2+ = re-hit).
        """
        key = f"flow_rehit:{ticker.upper()}:{strike:.0f}:{side}"
        try:
            if self._redis:
                val = self._redis.incr(key)
                self._redis.expire(key, 28800)  # 8hr TTL
                return int(val)
        except Exception:
            pass
        return 1

    def get_flow_rehit_count(self, ticker: str, strike: float, side: str) -> int:
        """Get current hit count for a strike (0 if never hit)."""
        key = f"flow_rehit:{ticker.upper()}:{strike:.0f}:{side}"
        try:
            if self._redis:
                val = self._redis.get(key)
                return int(val) if val else 0
        except Exception:
            pass
        return 0

    # ── GEX Level Access ──

    def get_gamma_flip_level(self, ticker: str) -> float:
        """
        Get gamma flip level for a ticker.
        Sources (in priority order):
          1. Thesis monitor (full institutional snapshot — SPY/QQQ)
          2. Lightweight GEX from flow detector (all tickers with chain data)
        """
        # Source 1: Full thesis
        try:
            thesis = self.get_thesis(ticker)
            if thesis:
                levels = thesis.get("levels", {})
                gf = levels.get("gamma_flip")
                if gf and gf > 0:
                    return float(gf)
        except Exception:
            pass
        # Source 2: Lightweight GEX from flow sweep
        try:
            gex = self._json_get(f"gex:{ticker.upper()}")
            if gex and gex.get("gamma_flip", 0) > 0:
                return float(gex["gamma_flip"])
        except Exception:
            pass
        return 0.0

    def get_gex_sign(self, ticker: str) -> str:
        """
        Get GEX sign (positive/negative) for a ticker.
        Sources: thesis monitor → lightweight GEX from flow detector.
        """
        # Source 1: Full thesis
        try:
            thesis = self.get_thesis(ticker)
            if thesis and thesis.get("gex_sign"):
                return thesis.get("gex_sign", "")
        except Exception:
            pass
        # Source 2: Lightweight GEX
        try:
            gex = self._json_get(f"gex:{ticker.upper()}")
            if gex and gex.get("gex_sign"):
                return gex["gex_sign"]
        except Exception:
            pass
        return ""

    def get_gex_data(self, ticker: str) -> dict:
        """Get full GEX data including call/put walls and max pain."""
        try:
            gex = self._json_get(f"gex:{ticker.upper()}")
            if gex:
                return gex
        except Exception:
            pass
        return {}

    def get_flow_conviction_boost(self, ticker: str, direction: str) -> float:
        """
        Calculate EntryValidator flow boost for a ticker/direction.
        Returns 0-14 based on flow alignment and magnitude.

        Used by thesis_monitor before calling EntryValidator.validate().
        """
        fd = self.get_flow_direction(ticker)
        if not fd:
            return 0.0
        flow_dir = (fd.get("direction", "") or "").lower()
        want_dir = direction.lower()

        # Check alignment
        aligned = (
            ("bull" in flow_dir and want_dir == "long") or
            ("bear" in flow_dir and want_dir == "short")
        )
        if not aligned:
            return 0.0

        vol_oi = fd.get("vol_oi", 0)
        flow_level = fd.get("flow_level", "")

        # Scale boost by magnitude
        if vol_oi >= 10:     # conviction-level
            return 14.0       # pushes score 2 → 4
        elif vol_oi >= 5:    # very strong
            return 10.0       # overrides critical-pair rejection
        elif vol_oi >= 2:    # extreme
            return 7.0        # pushes score 2 → 3
        elif flow_level == "significant":
            return 4.0        # meaningful but not decisive
        return 0.0

    def save_conviction_boost(self, ticker: str, boost: float, direction: str):
        """Store a flow conviction boost for EntryValidator (30 min TTL)."""
        self._json_set(f"conviction_boost:{ticker.upper()}", {
            "boost": boost, "direction": direction,
            "timestamp": datetime.now().isoformat(),
        }, ttl=1800)

    def get_conviction_boost(self, ticker: str, direction: str) -> float:
        """Get active conviction boost for a ticker+direction."""
        data = self._json_get(f"conviction_boost:{ticker.upper()}")
        if not data:
            return 0.0
        # Only apply if direction matches
        stored_dir = (data.get("direction", "") or "").lower()
        if direction.lower() in ("long", "bull", "bullish") and "bull" in stored_dir:
            return data.get("boost", 0.0)
        if direction.lower() in ("short", "bear", "bearish") and "bear" in stored_dir:
            return data.get("boost", 0.0)
        return 0.0

    # ═══════════════════════════════════════════════════════
    # POTTER BOX BREAK EVENTS — enriched break/reclaim data
    # ═══════════════════════════════════════════════════════

    _BREAK_TTL = 4 * 3600  # 4 hours — intraday shelf life

    def _break_event_key(self, ticker: str) -> str:
        return f"potter_break:{ticker.upper()}"

    def save_break_event(self, ticker: str, event: dict):
        """Persist an enriched break event (conviction, exposure, trade).

        Keyed by ticker — latest break overwrites previous.
        4-hour TTL: break events are intraday-actionable only.
        """
        self._json_set(self._break_event_key(ticker), event, self._BREAK_TTL)

    def get_break_event(self, ticker: str) -> Optional[dict]:
        """Retrieve the most recent break event for a ticker."""
        return self._json_get(self._break_event_key(ticker))

    def get_all_break_events(self) -> List[dict]:
        """Retrieve all active break events (scan potter_break:* keys)."""
        if not self._scan:
            return []
        try:
            keys = self._scan("potter_break:*")
            events = []
            for key in keys:
                ev = self._json_get(key)
                if ev:
                    events.append(ev)
            return events
        except Exception:
            return []

    # ═══════════════════════════════════════════════════════
    # DIAGNOSTICS
    # ═══════════════════════════════════════════════════════

    @property
    def status(self) -> dict:
        """Overview of persistent state for debugging."""
        result = {"redis_backed": True}
        if self._scan:
            try:
                result["flow_campaigns"] = len(self._scan("flow_campaign:*"))
                result["active_trades"] = len(self._scan("active_trade:*"))
                result["swing_signals"] = len(self._scan("swing_signal:*"))
                result["stalk_alerts"] = len(self._scan("stalk:*"))
                result["volume_flags"] = len(self._scan("vol_flags:*"))
                result["oi_baselines"] = len(self._scan("oi_baseline:*"))
                result["break_events"] = len(self._scan("potter_break:*"))
            except Exception:
                result["scan_error"] = True
        return result
