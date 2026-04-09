# oi_cache.py
# ═══════════════════════════════════════════════════════════════════
# OI Change Cache — stores prior OI per contract per expiration,
# computes oi_change (delta) on the next fetch.
#
# Keys are (ticker, expiration) so 0DTE and 21DTE monitor chains
# don't overwrite each other's snapshots.
#
# TTL: 50 hours — survives overnight + a missed morning run.
# For weekend survival, we also keep a "last known" snapshot per
# ticker+expiry that only gets replaced on successful save.
#
# Usage:
#   from oi_cache import OICache
#   oi_cache = OICache(store_get, store_set)
#   enriched = oi_cache.apply_oi_changes_to_chain("SPY", "2026-03-16", chain_data)
#   oi_cache.save_snapshot("SPY", "2026-03-16", chain_data)
# ═══════════════════════════════════════════════════════════════════

import json
import logging
import time
from typing import Dict, Optional, Callable

log = logging.getLogger(__name__)

OI_CACHE_TTL = 50 * 3600  # 50 hours — survives overnight + one missed cycle
OI_SAVE_COOLDOWN = 120     # minimum seconds between saves for same ticker+expiry

# ── Intraday flow detection thresholds ──
OI_FLOW_BUILDUP_PCT = 1.00     # 100%+ increase from morning baseline
OI_FLOW_UNWIND_PCT = 0.50      # 50%+ decrease from morning baseline
OI_FLOW_MIN_CONTRACTS = 500    # minimum contract change to be notable
OI_FLOW_MIN_BASELINE = 1000    # minimum morning OI to avoid noise on tiny positions
OI_FLOW_MAX_DIST_PCT = 0.10    # only alert strikes within 10% of spot
OI_FLOW_ALERT_COOLDOWN = 1800  # 30 minutes between re-alerts for same strike
OI_FLOW_FIRST_COOLDOWN = 300   # 5 minutes after baseline saved before checking (let OI settle)


class OICache:
    """
    Caches OI per contract per expiration. On the next fetch, diffs
    current OI against cached OI to produce oi_change per contract.

    store_get / store_set must match app.py's Redis/mem store interface:
        store_get(key) → str or None
        store_set(key, value_str, ttl=int)
    """

    def __init__(self, store_get_fn: Callable, store_set_fn: Callable):
        self._get = store_get_fn
        self._set = store_set_fn
        # Cooldown guard: tracks last save time per (ticker, expiry) to prevent
        # double-saves within the same processing cycle when multiple code paths
        # (e.g. _run_v4_prefilter and _get_0dte_iv) call save_snapshot in quick
        # succession. Key: (ticker, expiry) → monotonic timestamp of last save.
        self._last_save: Dict[tuple, float] = {}

    def _key(self, ticker: str, expiration: str) -> str:
        """Key includes expiration so different DTEs don't collide."""
        return f"oi_snap:{ticker.upper()}:{expiration}"

    # ── Legacy key for backward compat on first deploy ──
    def _legacy_key(self, ticker: str) -> str:
        return f"oi_snap:{ticker.upper()}"

    def _parse_chain_oi(self, chain_data: dict) -> Dict[str, int]:
        """Extract {contract_key: oi} from MarketData chain response."""
        sym_list = chain_data.get("optionSymbol") or []
        n = len(sym_list)
        if n == 0:
            return {}

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides   = col("side", "")
        oi_list = col("openInterest", 0)

        result = {}
        for i in range(n):
            strike = strikes[i]
            side   = str(sides[i] or "").lower()
            oi     = int(oi_list[i] or 0)
            if strike is None or side not in ("call", "put"):
                continue
            key = f"{float(strike)}|{side}"
            result[key] = oi

        return result

    def get_prior_snapshot(self, ticker: str, expiration: str) -> Optional[Dict[str, int]]:
        """
        Load the cached OI snapshot from store.
        Tries new expiration-aware key first, falls back to legacy key.
        """
        try:
            raw = self._get(self._key(ticker, expiration))
            if raw is not None:
                return json.loads(raw)
            # Fall back to legacy key (one-time migration path)
            raw = self._get(self._legacy_key(ticker))
            if raw is not None:
                log.info(f"OI cache: migrating legacy key for {ticker} → {ticker}:{expiration}")
                return json.loads(raw)
            return None
        except Exception as e:
            log.warning(f"OI cache load failed for {ticker}:{expiration}: {e}")
            return None

    def save_snapshot(self, ticker: str, expiration: str, chain_data: dict):
        """Save current OI as the reference snapshot for next comparison.

        Enforces a per-(ticker, expiry) cooldown of OI_SAVE_COOLDOWN seconds to
        prevent multiple code paths (prefilter + EM card) from double-saving within
        the same processing cycle, which would overwrite a fresh snapshot with an
        identical or stale one.
        """
        guard_key = (ticker.upper(), expiration)
        now = time.monotonic()
        last = self._last_save.get(guard_key, 0.0)
        if now - last < OI_SAVE_COOLDOWN:
            log.debug(
                f"OI snapshot save skipped for {ticker}:{expiration} — "
                f"cooldown ({now - last:.0f}s < {OI_SAVE_COOLDOWN}s)"
            )
            return

        try:
            current = self._parse_chain_oi(chain_data)
            if not current:
                return
            self._set(
                self._key(ticker, expiration),
                json.dumps(current),
                ttl=OI_CACHE_TTL,
            )
            self._last_save[guard_key] = now
            log.info(
                f"OI snapshot saved for {ticker}:{expiration} — "
                f"{len(current)} contracts, TTL={OI_CACHE_TTL // 3600}h"
            )
        except Exception as e:
            log.warning(f"OI cache save failed for {ticker}:{expiration}: {e}")

    def compute_oi_changes(self, ticker: str, expiration: str, chain_data: dict) -> Dict[str, int]:
        """
        Compare current chain OI against cached prior snapshot.
        Returns {contract_key: oi_change} where oi_change = current - prior.
        Positive = new positions opened, negative = positions closed.

        If no prior snapshot exists, returns empty dict (first run).
        """
        prior = self.get_prior_snapshot(ticker, expiration)
        if prior is None:
            log.info(f"OI cache: no prior snapshot for {ticker}:{expiration} — first run, caching now")
            return {}

        current = self._parse_chain_oi(chain_data)
        if not current:
            return {}

        changes = {}
        matched = 0
        for key, cur_oi in current.items():
            prev_oi = prior.get(key, 0)
            delta = cur_oi - prev_oi
            if key in prior:
                matched += 1
            if delta != 0:
                changes[key] = delta

        log.info(
            f"OI cache: {ticker}:{expiration} — "
            f"{matched}/{len(current)} strikes matched prior, "
            f"{len(changes)} changed"
        )
        return changes

    def apply_oi_changes_to_chain(self, ticker: str, expiration: str, chain_data: dict) -> dict:
        """
        Compute OI changes and inject them into the chain data as an
        'oiChange' array parallel to the existing arrays.
        Returns the enriched chain_data dict (mutates in place for efficiency).
        """
        changes = self.compute_oi_changes(ticker, expiration, chain_data)
        if not changes:
            # Still inject an oiChange array of Nones so downstream knows
            # the field exists but had no data (vs field missing entirely)
            sym_list = chain_data.get("optionSymbol") or []
            chain_data["oiChange"] = [None] * len(sym_list)
            return chain_data

        sym_list = chain_data.get("optionSymbol") or []
        n = len(sym_list)

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides   = col("side", "")

        oi_change_list = []
        populated = 0
        for i in range(n):
            strike = strikes[i]
            side   = str(sides[i] or "").lower()
            if strike is None:
                oi_change_list.append(None)
                continue
            key = f"{float(strike)}|{side}"
            val = changes.get(key)
            oi_change_list.append(val)
            if val is not None:
                populated += 1

        chain_data["oiChange"] = oi_change_list
        log.info(f"OI cache: injected {populated} oiChange values into {ticker}:{expiration} chain")
        return chain_data

    # ═══════════════════════════════════════════════════════════
    # INTRADAY FLOW DETECTION
    # Compare current OI against morning baseline (not rolling snapshot).
    # Detects institutional buildup (100%+) and unwinding (50%+).
    # Alerts once on discovery, re-alerts every 30 min if trend continues.
    # ═══════════════════════════════════════════════════════════

    def _baseline_key(self, ticker: str, expiration: str, date_str: str) -> str:
        return f"oi_baseline:{ticker.upper()}:{expiration}:{date_str}"

    def save_morning_baseline(self, ticker: str, expiration: str, chain_data: dict) -> bool:
        """
        Save morning OI baseline. Called once per day per ticker/exp.
        Returns True if baseline was saved (first call), False if already exists.
        """
        from datetime import date as _date
        today = _date.today().isoformat()
        key = self._baseline_key(ticker, expiration, today)

        # Check if baseline already exists for today
        existing = self._get(key)
        if existing is not None:
            return False  # already saved today

        current = self._parse_chain_oi(chain_data)
        if not current:
            return False

        try:
            self._set(key, json.dumps(current), ttl=20 * 3600)  # 20h TTL — expires overnight
            log.info(f"OI morning baseline saved: {ticker}:{expiration} — {len(current)} contracts")
            return True
        except Exception as e:
            log.warning(f"OI baseline save failed for {ticker}:{expiration}: {e}")
            return False

    def _get_morning_baseline(self, ticker: str, expiration: str) -> Optional[Dict[str, int]]:
        """Load today's morning baseline from store."""
        from datetime import date as _date
        today = _date.today().isoformat()
        key = self._baseline_key(ticker, expiration, today)
        try:
            raw = self._get(key)
            if raw is not None:
                return json.loads(raw)
            return None
        except Exception:
            return None

    def check_intraday_flow(self, ticker: str, expiration: str,
                            chain_data: dict, spot: float) -> list:
        """
        Compare current OI against morning baseline.
        Returns list of flow alert dicts for significant buildup/unwinding.

        Each alert dict:
          {ticker, strike, side, morning_oi, current_oi, change, change_pct,
           flow_type ('buildup'|'unwinding'), dist_from_spot, directional_bias}
        """
        baseline = self._get_morning_baseline(ticker, expiration)
        if baseline is None:
            return []

        current = self._parse_chain_oi(chain_data)
        if not current:
            return []

        now = time.monotonic()

        # Initialize alert cooldown tracker if needed
        if not hasattr(self, '_flow_alert_times'):
            self._flow_alert_times = {}

        alerts = []
        for key, cur_oi in current.items():
            base_oi = baseline.get(key, 0)
            if base_oi < OI_FLOW_MIN_BASELINE:
                continue

            # Parse strike and side
            try:
                strike_str, side = key.split("|")
                strike = float(strike_str)
            except (ValueError, IndexError):
                continue

            # Distance filter — skip far OTM strikes
            if spot > 0:
                dist_pct = abs(strike - spot) / spot
                if dist_pct > OI_FLOW_MAX_DIST_PCT:
                    continue

            change = cur_oi - base_oi
            abs_change = abs(change)
            if abs_change < OI_FLOW_MIN_CONTRACTS:
                continue

            change_pct = change / base_oi if base_oi > 0 else 0

            flow_type = None
            if change_pct >= OI_FLOW_BUILDUP_PCT:
                flow_type = "buildup"
            elif change_pct <= -OI_FLOW_UNWIND_PCT:
                flow_type = "unwinding"

            if not flow_type:
                continue

            # Cooldown check — alert once on discovery, then every 30 min
            cooldown_key = f"{ticker}:{strike}:{side}:{flow_type}"
            last_alert = self._flow_alert_times.get(cooldown_key, 0)
            if now - last_alert < OI_FLOW_ALERT_COOLDOWN:
                continue

            self._flow_alert_times[cooldown_key] = now

            # Directional context
            if side == "call":
                if flow_type == "buildup":
                    directional = "BULLISH" if strike > spot else "BULLISH (ITM accumulation)"
                else:
                    directional = "BEARISH unwind" if strike > spot else "profit-taking"
            else:  # put
                if flow_type == "buildup":
                    directional = "BEARISH" if strike < spot else "BEARISH (ITM accumulation)"
                else:
                    directional = "BULLISH unwind" if strike < spot else "profit-taking"

            alerts.append({
                "ticker": ticker,
                "expiration": expiration,
                "strike": strike,
                "side": side,
                "morning_oi": base_oi,
                "current_oi": cur_oi,
                "change": change,
                "change_pct": round(change_pct * 100, 1),
                "flow_type": flow_type,
                "dist_from_spot": round((strike - spot) / spot * 100, 2),
                "spot": spot,
                "directional_bias": directional,
            })

        # Sort by absolute change descending — biggest flow first
        alerts.sort(key=lambda a: abs(a["change"]), reverse=True)

        # Cap at top 5 per ticker per check to avoid alert storms
        return alerts[:5]

    def format_flow_alert(self, alert: dict) -> str:
        """Format a single intraday flow alert for Telegram."""
        t = alert
        if t["flow_type"] == "buildup":
            emoji = "🔥" if abs(t["change_pct"]) >= 200 else "📈"
            header = f"{emoji} OI BUILDUP — {t['ticker']}"
        else:
            emoji = "🧊" if abs(t["change_pct"]) >= 75 else "📉"
            header = f"{emoji} OI UNWINDING — {t['ticker']}"

        side_emoji = "📗" if t["side"] == "call" else "📕"
        dist_dir = "above" if t["dist_from_spot"] > 0 else "below"

        lines = [
            header,
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            f"{side_emoji} ${t['strike']:.0f} {t['side'].upper()} ({abs(t['dist_from_spot']):.1f}% {dist_dir} spot ${t['spot']:.2f})",
            f"Morning: {t['morning_oi']:,} → Now: {t['current_oi']:,} ({t['change']:+,})",
            f"Change: {t['change_pct']:+.0f}%",
            f"Exp: {t['expiration']}",
            f"Signal: {t['directional_bias']}",
        ]
        return "\n".join(lines)

    def format_flow_summary(self, alerts: list) -> str:
        """Format multiple flow alerts into a single summary message."""
        if not alerts:
            return ""

        buildups = [a for a in alerts if a["flow_type"] == "buildup"]
        unwinds = [a for a in alerts if a["flow_type"] == "unwinding"]

        lines = ["📊 INTRADAY OI FLOW DETECTED", "━" * 28]

        if buildups:
            lines.append("")
            lines.append("🔥 BUILDUP (new positions opening):")
            for a in buildups[:3]:
                side_emoji = "📗" if a["side"] == "call" else "📕"
                lines.append(
                    f"  {side_emoji} {a['ticker']} ${a['strike']:.0f} {a['side'].upper()} "
                    f"— {a['change']:+,} ({a['change_pct']:+.0f}%) → {a['directional_bias']}"
                )

        if unwinds:
            lines.append("")
            lines.append("🧊 UNWINDING (positions closing):")
            for a in unwinds[:3]:
                side_emoji = "📗" if a["side"] == "call" else "📕"
                lines.append(
                    f"  {side_emoji} {a['ticker']} ${a['strike']:.0f} {a['side'].upper()} "
                    f"— {a['change']:+,} ({a['change_pct']:+.0f}%) → {a['directional_bias']}"
                )

        return "\n".join(lines)

    def cleanup_flow_cooldowns(self):
        """Purge stale cooldown entries. Call once per hour."""
        if not hasattr(self, '_flow_alert_times'):
            return
        now = time.monotonic()
        stale = [k for k, v in self._flow_alert_times.items()
                 if now - v > OI_FLOW_ALERT_COOLDOWN * 4]
        for k in stale:
            del self._flow_alert_times[k]
