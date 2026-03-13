# oi_cache.py
# ═══════════════════════════════════════════════════════════════════
# OI Change Cache — stores yesterday's OI per contract, computes
# oi_change (delta) on the next fetch.
#
# Uses the same store_get/store_set interface as app.py's Redis/mem store.
# Keyed by ticker → {(strike, side, expiry): oi}
# TTL: 25 hours (survives overnight, auto-expires if stale).
#
# Usage:
#   from oi_cache import OICache
#   oi_cache = OICache(store_get, store_set)
#   oi_changes = oi_cache.compute_oi_changes(ticker, current_chain_data)
#   # oi_changes = {(strike, side, expiry): delta_oi} or {}
#   oi_cache.save_snapshot(ticker, current_chain_data)
# ═══════════════════════════════════════════════════════════════════

import json
import logging
from typing import Dict, Optional, Callable, Tuple

log = logging.getLogger(__name__)

OI_CACHE_TTL = 25 * 3600  # 25 hours — survives overnight, stale after


class OICache:
    """
    Caches OI per contract. On the next fetch, diffs current OI against
    cached OI to produce oi_change per contract.

    store_get / store_set must match app.py's Redis/mem store interface:
        store_get(key) → str or None
        store_set(key, value_str, ttl=int)
    """

    def __init__(self, store_get_fn: Callable, store_set_fn: Callable):
        self._get = store_get_fn
        self._set = store_set_fn

    def _key(self, ticker: str) -> str:
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
            # Key: "strike|side" e.g. "575.0|call"
            key = f"{float(strike)}|{side}"
            result[key] = oi

        return result

    def get_prior_snapshot(self, ticker: str) -> Optional[Dict[str, int]]:
        """Load the cached OI snapshot from store."""
        try:
            raw = self._get(self._key(ticker))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as e:
            log.warning(f"OI cache load failed for {ticker}: {e}")
            return None

    def save_snapshot(self, ticker: str, chain_data: dict):
        """Save current OI as the reference snapshot for next comparison."""
        try:
            current = self._parse_chain_oi(chain_data)
            if not current:
                return
            self._set(self._key(ticker), json.dumps(current), ttl=OI_CACHE_TTL)
            log.debug(f"OI snapshot saved for {ticker}: {len(current)} contracts")
        except Exception as e:
            log.warning(f"OI cache save failed for {ticker}: {e}")

    def compute_oi_changes(self, ticker: str, chain_data: dict) -> Dict[str, int]:
        """
        Compare current chain OI against cached prior snapshot.
        Returns {contract_key: oi_change} where oi_change = current - prior.
        Positive = new positions opened, negative = positions closed.

        If no prior snapshot exists, returns empty dict (first run).
        """
        prior = self.get_prior_snapshot(ticker)
        if prior is None:
            return {}

        current = self._parse_chain_oi(chain_data)
        changes = {}
        for key, cur_oi in current.items():
            prev_oi = prior.get(key, 0)
            delta = cur_oi - prev_oi
            if delta != 0:
                changes[key] = delta

        return changes

    def apply_oi_changes_to_chain(self, ticker: str, chain_data: dict) -> dict:
        """
        Compute OI changes and inject them into the chain data as an
        'oiChange' array parallel to the existing arrays.
        Returns the enriched chain_data dict (mutates in place for efficiency).
        """
        changes = self.compute_oi_changes(ticker, chain_data)
        if not changes:
            return chain_data

        sym_list = chain_data.get("optionSymbol") or []
        n = len(sym_list)

        def col(name, default=None):
            v = chain_data.get(name, default)
            return v if isinstance(v, list) else [default] * n

        strikes = col("strike", None)
        sides   = col("side", "")

        oi_change_list = []
        for i in range(n):
            strike = strikes[i]
            side   = str(sides[i] or "").lower()
            if strike is None:
                oi_change_list.append(None)
                continue
            key = f"{float(strike)}|{side}"
            oi_change_list.append(changes.get(key))

        chain_data["oiChange"] = oi_change_list
        return chain_data
