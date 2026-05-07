"""
bot_state_producer.py — Patch B daemon.

Three daemon threads run forever, each on its own tier cadence:
  Tier A (default 60s, default intents=[front])
  Tier B (default 180s, default intents=[t7])
  Tier C (default 600s, default intents=[])

For each (ticker, intent) in the tier:
  1. canonical_expiration(ticker, intent) → ISO date or None
  2. fetch_raw_inputs(ticker, expiration, data_router=cached_md) → RawInputs
  3. BotState.build_from_raw(raw) → BotState
  4. _build_envelope(state_dict, intent, expiration) → dict
  5. _serialize_envelope(env) → JSON string (NaN/inf → null)
  6. redis.set(f"bot_state:{ticker}:{intent}", json, ex=tier_ttl)
  7. _record_build_timing(redis, ticker, intent, elapsed_ms, expiration)

Per-ticker errors caught and logged; the loop continues for other tickers.
The whole thing is gated by env var BOT_STATE_PRODUCER_ENABLED. When that
env var is off, start_producer() returns None and the daemon never spawns.

Audit notes:
  - Tier env vars use the empty-string-safe parser (_parse_intents) so an
    unset Tier C cleanly disables that tier instead of crashing.
  - PRODUCER_VERSION=1 ships with the first version. Bump on schema change.
  - CONVENTION_VERSION=2 is Patch 9's dealer-side convention. Hard-coded —
    don't expose as an env var; mismatch is the consumer's job to detect.
"""

from __future__ import annotations
import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Versioning — bump PRODUCER_VERSION on schema change.
# ─────────────────────────────────────────────────────────────────────

PRODUCER_VERSION = 1
CONVENTION_VERSION = 2  # Patch 9 dealer-side convention. Don't change.


# ─────────────────────────────────────────────────────────────────────
# Env var parsing (defensive — handles unset, empty string, garbage)
# ─────────────────────────────────────────────────────────────────────

def _parse_intents(value: Optional[str]) -> List[str]:
    """Parse a comma-separated intent list. Empty/None → empty list.

    Spec v4 implementer note: "".split(",") returns [""] which would
    iterate as a one-element string list. We filter empty strings out
    so an unset Tier C env var disables that tier cleanly.
    """
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_tickers(value: Optional[str]) -> List[str]:
    """Comma-separated tickers, uppercased, deduped, order-preserving."""
    if not value:
        return []
    seen = set()
    out: List[str] = []
    for raw in value.split(","):
        t = raw.strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _parse_int_env(value: Optional[str], default: int) -> int:
    """Read an int from env. Empty/garbage → default."""
    if not value:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────
# Envelope schema
# ─────────────────────────────────────────────────────────────────────

def _build_envelope(state: Dict[str, Any], intent: str, expiration: str) -> Dict[str, Any]:
    """Wrap a BotState dict in the producer envelope. Caller is responsible
    for converting BotState → dict (typically dataclasses.asdict).

    Returns a NEW dict; does not mutate `state`.
    """
    return {
        "producer_version": PRODUCER_VERSION,
        "convention_version": CONVENTION_VERSION,
        "intent": intent,
        "expiration": expiration,
        "state": state,
    }


# ─────────────────────────────────────────────────────────────────────
# JSON serialization with NaN/inf cleanup
# ─────────────────────────────────────────────────────────────────────

def _clean_for_json(obj: Any) -> Any:
    """Recursively convert NaN, +inf, -inf to None so json.dumps doesn't
    emit non-standard JSON (NaN/Infinity literals). Standard JSON parsers
    in JS/Go/etc. reject those; converting to null is the safest default.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_for_json(v) for v in obj]
    return obj


def _serialize_envelope(env: Dict[str, Any]) -> str:
    """Serialize an envelope dict to a JSON string. NaN/inf inside `state`
    are converted to null; all other values pass through json.dumps.
    """
    cleaned = _clean_for_json(env)
    return json.dumps(cleaned, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────────
# Per-build timing telemetry — hard deliverable per spec v4.
#
# Sorted set keyed by UTC date. Member is JSON, score is unix ms so
# multiple builds for the same ticker in the same second don't collide.
# ZRANGEBYSCORE lets the post-deploy analysis slice arbitrary windows.
# ─────────────────────────────────────────────────────────────────────

TIMINGS_KEY_PREFIX = "bot_state_producer:timings:"
TIMINGS_TTL_SEC = 48 * 3600  # 48 hours — covers the 24h analysis window + slack


def _record_build_timing(redis_client, ticker: str, intent: str,
                         elapsed_ms: int, expiration: str) -> None:
    """Append one build-timing record to the daily sorted set.

    No-op on Redis errors — telemetry must NEVER block the producer loop.
    """
    if redis_client is None:
        return
    try:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"{TIMINGS_KEY_PREFIX}{date_str}"
        member = json.dumps({
            "ticker": ticker,
            "intent": intent,
            "elapsed_ms": int(elapsed_ms),
            "expiration": expiration,
        }, separators=(",", ":"))
        score = int(time.time() * 1000)  # millis since epoch
        redis_client.zadd(key, {member: score})
        redis_client.expire(key, TIMINGS_TTL_SEC)
    except Exception as e:
        log.debug(f"telemetry write failed for {ticker}/{intent}: {e}")


# ─────────────────────────────────────────────────────────────────────
# Multi-worker lock — only one Render web worker runs the producer.
#
# Pattern: SET key token NX EX ttl (atomic acquire). Owner refreshes via
# Lua-equivalent CAS. Release is owner-checked via Lua so a stale owner
# can't unlock a lock that's been re-acquired by someone else.
# ─────────────────────────────────────────────────────────────────────

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


def _acquire_lock(redis_client, lock_key: str, ttl_sec: int) -> Optional[str]:
    """Atomically acquire `lock_key` with TTL. Returns an owner token on
    success, None on failure (lock already held). The token is required
    to release/refresh and is opaque to callers."""
    if redis_client is None:
        return None
    import secrets
    token = secrets.token_hex(16)
    try:
        ok = redis_client.set(lock_key, token, nx=True, ex=ttl_sec)
        return token if ok else None
    except Exception as e:
        log.warning(f"lock acquire failed: {e}")
        return None


def _release_lock(redis_client, lock_key: str, token: str) -> bool:
    """Release a lock — only succeeds if `token` matches the current value."""
    if redis_client is None:
        return False
    try:
        result = redis_client.eval(_RELEASE_SCRIPT, 1, lock_key, token)
        return bool(result)
    except Exception as e:
        log.warning(f"lock release failed: {e}")
        return False


def _refresh_lock(redis_client, lock_key: str, token: str, ttl_sec: int) -> bool:
    """Bump TTL on a lock — only if `token` still owns it."""
    if redis_client is None:
        return False
    try:
        result = redis_client.eval(_REFRESH_SCRIPT, 1, lock_key, token, ttl_sec)
        return bool(result)
    except Exception as e:
        log.warning(f"lock refresh failed: {e}")
        return False
