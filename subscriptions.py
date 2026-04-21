# subscriptions.py
# v8.5 Phase 3: Subscription state manager for /daytrade and /conviction modes.
#
# Persists to Redis via persistent_state. Single-user bot — chat_id keys
# the subscription set. See DEPLOY_PHASE3.md for schema and semantics.

import logging
from datetime import datetime
from typing import Optional, Literal, List

log = logging.getLogger(__name__)

SubMode = Literal["daytrade", "conviction"]
Direction = Literal["call", "put"]

SUB_TTL_SEC = 7 * 24 * 3600   # 7 days rolling


def _sub_key(chat_id: str, ticker: str, direction: str) -> str:
    return f"subscriptions:{chat_id}:{ticker.upper()}:{direction.lower()}"


def _prefix(chat_id: str) -> str:
    return f"subscriptions:{chat_id}:"


class SubscriptionManager:
    def __init__(self, state):
        """state must expose _json_get, _json_set, _redis (for scan)."""
        self._state = state

    def add(self, chat_id: str, ticker: str, direction: str,
            mode: SubMode, source: str = "manual",
            source_dte: Optional[int] = None,
            source_expiry: Optional[str] = None,
            source_notional: Optional[float] = None) -> dict:
        """Add or replace a subscription. Returns the stored record."""
        key = _sub_key(chat_id, ticker, direction)
        rec = {
            "mode": mode,
            "created_at": datetime.now().isoformat(),
            "source": source,
            "ticker": ticker.upper(),
            "direction": direction.lower(),
        }
        if source_dte is not None:
            rec["source_dte"] = source_dte
        if source_expiry:
            rec["source_expiry"] = source_expiry
        if source_notional is not None:
            rec["source_notional"] = source_notional

        try:
            self._state._json_set(key, rec, ttl=SUB_TTL_SEC)
        except Exception as e:
            log.warning(f"Subscription add failed ({key}): {e}")
            return rec
        log.info(f"Subscription added: {chat_id} {ticker} {direction} "
                 f"mode={mode} source={source}")
        return rec

    def remove(self, chat_id: str, ticker: str,
               direction: Optional[str] = None) -> int:
        """Remove subscriptions for ticker. If direction=None, removes both
        directions. Returns count removed."""
        removed = 0
        dirs = [direction] if direction else ["call", "put"]
        for d in dirs:
            key = _sub_key(chat_id, ticker, d)
            try:
                if self._state._redis.delete(key):
                    removed += 1
            except Exception as e:
                log.warning(f"Sub remove failed for {key}: {e}")
        log.info(f"Subscription removed: {chat_id} {ticker} "
                 f"dir={direction or 'all'} count={removed}")
        return removed

    def get(self, chat_id: str, ticker: str, direction: str) -> Optional[dict]:
        """Get specific subscription record. None if not subscribed."""
        key = _sub_key(chat_id, ticker, direction)
        try:
            return self._state._json_get(key)
        except Exception:
            return None

    def mode_for(self, chat_id: str, ticker: str,
                 direction: str) -> Optional[SubMode]:
        """Returns 'daytrade', 'conviction', or None."""
        rec = self.get(chat_id, ticker, direction)
        if rec:
            return rec.get("mode")
        return None

    def has_daytrade(self, chat_id: str, ticker: str) -> bool:
        """Any daytrade sub on this ticker (either direction)?"""
        for d in ("call", "put"):
            rec = self.get(chat_id, ticker, d)
            if rec and rec.get("mode") == "daytrade":
                return True
        return False

    def has_any(self, chat_id: str, ticker: str, direction: str) -> bool:
        """Any sub (daytrade or conviction) for (ticker, direction)?"""
        return self.get(chat_id, ticker, direction) is not None

    def list_all(self, chat_id: str) -> List[dict]:
        """List all active subscriptions for chat_id, sorted by created_at."""
        prefix = _prefix(chat_id)
        results = []
        try:
            for key in self._state._redis.scan_iter(match=f"{prefix}*"):
                key_str = key if isinstance(key, str) else key.decode()
                rec = self._state._json_get(key_str)
                if rec:
                    results.append(rec)
        except Exception as e:
            log.warning(f"Sub list failed: {e}")
        results.sort(key=lambda r: r.get("created_at", ""))
        return results


_sub_manager: Optional[SubscriptionManager] = None


def init_subscription_manager(state) -> SubscriptionManager:
    global _sub_manager
    _sub_manager = SubscriptionManager(state)
    log.info("Subscription manager initialized")
    return _sub_manager


def get_subscription_manager() -> Optional[SubscriptionManager]:
    return _sub_manager
