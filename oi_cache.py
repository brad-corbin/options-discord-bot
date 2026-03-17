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
