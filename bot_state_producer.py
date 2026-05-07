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
  6. redis.set(f"{KEY_PREFIX}{ticker}:{intent}", json, ex=tier_ttl)  # see KEY_PREFIX module constant
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
KEY_PREFIX = "bot_state:"  # Redis key prefix for envelopes: bot_state:{ticker}:{intent}


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
    """Recursively normalize values so json.dumps doesn't choke:
      - NaN/+inf/-inf floats → None (non-standard JSON literals).
      - datetime / date → ISO 8601 string (BotState carries snapshot
        timestamps; json.dumps raises TypeError on them by default).
    Standard JSON parsers in JS/Go/etc. reject NaN literals, and there
    is no native JSON datetime type, so ISO strings are the safe default.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    # datetime is a subclass of date, so the datetime branch must come
    # first OR a single isinstance check on (datetime, date) handles both.
    from datetime import datetime, date
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
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


# ─────────────────────────────────────────────────────────────────────
# Per-tier work function — one full pass over (ticker × intent) for
# this tier. Per-ticker errors are caught and logged; the pass always
# completes for every (ticker, intent) regardless of individual failures.
#
# Dependencies are injected (canonical_expiration_fn, fetch_raw_inputs_fn,
# build_from_raw_fn) so unit tests can mock them. In production B.4
# binds the real implementations.
# ─────────────────────────────────────────────────────────────────────


def _build_one_state(
    ticker: str,
    intent: str,
    cached_md,
    canonical_expiration_fn,
    fetch_raw_inputs_fn,
    build_from_raw_fn,
) -> Optional[Dict[str, Any]]:
    """Build a single (ticker, intent) state dict + expiration. Returns
    None if no qualifying expiration. Raises on fetch/build failures —
    caller handles per-ticker isolation.
    """
    expiration = canonical_expiration_fn(ticker, intent, data_router=cached_md)
    if not expiration:
        return None
    raw = fetch_raw_inputs_fn(ticker, expiration, data_router=cached_md)
    state = build_from_raw_fn(raw)
    # If state is a dataclass instance, convert to dict for the envelope.
    state_dict = asdict(state) if is_dataclass(state) else dict(state)
    return {"state_dict": state_dict, "expiration": expiration}


def _run_tier_pass(
    tier_name: str,
    intents: List[str],
    ttl_sec: int,
    tickers: List[str],
    cached_md,
    redis_client,
    canonical_expiration_fn,
    fetch_raw_inputs_fn,
    build_from_raw_fn,
) -> None:
    """One full pass over (ticker × intent) for this tier. Always
    completes; per-ticker errors are logged and skipped."""
    if not intents or not tickers:
        return
    for ticker in tickers:
        for intent in intents:
            t_start = time.time()
            try:
                built = _build_one_state(
                    ticker, intent, cached_md,
                    canonical_expiration_fn,
                    fetch_raw_inputs_fn,
                    build_from_raw_fn,
                )
            except Exception as e:
                log.warning(
                    f"[bsp tier={tier_name}] {ticker}/{intent} build failed: {e}"
                )
                continue
            if built is None:
                continue
            envelope = _build_envelope(
                state=built["state_dict"],
                intent=intent,
                expiration=built["expiration"],
            )
            try:
                payload = _serialize_envelope(envelope)
                key = f"{KEY_PREFIX}{ticker}:{intent}"
                redis_client.set(key, payload, ex=ttl_sec)
            except Exception as e:
                log.warning(
                    f"[bsp tier={tier_name}] {ticker}/{intent} write failed: {e}"
                )
                continue
            elapsed_ms = int((time.time() - t_start) * 1000)
            _record_build_timing(redis_client, ticker, intent,
                                 elapsed_ms, built["expiration"])


# ─────────────────────────────────────────────────────────────────────
# Lock-keeper thread — owns the cross-worker lock independently of any
# tier loop. Wakes every (LOCK_TTL_SEC - 30s) and refreshes. If refresh
# fails (another worker took over), signals all tier loops to stop.
#
# Decoupled from tier loops so an empty Tier A doesn't silently allow
# the lock to expire (path b per Brad's design call on Q2).
# ─────────────────────────────────────────────────────────────────────

LOCK_KEY = "bot_state_producer:lock"
LOCK_TTL_SEC = 90  # > Tier A cadence + max build time
LOCK_REFRESH_INTERVAL_SEC = 60  # LOCK_TTL_SEC - 30s safety margin


def _run_lock_keeper(
    redis_client,
    lock_key: str,
    lock_token: str,
    ttl_sec: int,
    refresh_interval_sec: float,
    stop_event,  # threading.Event
) -> None:
    """Forever-loop: refresh `lock_key` every `refresh_interval_sec`.

    If refresh fails (lock owned by someone else now), set `stop_event`
    so other producer threads exit, then return.
    """
    while not stop_event.is_set():
        if stop_event.wait(timeout=refresh_interval_sec):
            return
        ok = _refresh_lock(redis_client, lock_key, lock_token, ttl_sec)
        if not ok:
            log.warning(
                "[bsp lock-keeper] refresh failed; signaling all tier "
                "loops to stop (another worker took over or lock expired)"
            )
            stop_event.set()
            return


# ─────────────────────────────────────────────────────────────────────
# BotStateProducer — owns three tier daemons + one lock-keeper thread.
# Tiers are staggered T+0/T+10/T+20 so the rate limiter sees a smooth
# ramp rather than a synchronized burst at startup.
# ─────────────────────────────────────────────────────────────────────


class BotStateProducer:
    """Daemon-thread producer with a dedicated lock-keeper.
    Construct via `start_producer()`."""

    def __init__(
        self,
        tickers: List[str],
        tier_a_intents: List[str],
        tier_b_intents: List[str],
        tier_c_intents: List[str],
        tier_a_cadence: int,
        tier_b_cadence: int,
        tier_c_cadence: int,
        cached_md,
        redis_client,
    ):
        self._tickers = tickers
        self._tiers = [
            ("A", tier_a_intents, tier_a_cadence, tier_a_cadence * 3, 0),
            ("B", tier_b_intents, tier_b_cadence, tier_b_cadence * 3, 10),
            ("C", tier_c_intents, tier_c_cadence, tier_c_cadence * 3, 20),
        ]
        self._cached_md = cached_md
        self._redis = redis_client
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock_token: Optional[str] = None

    def start(self) -> bool:
        """Acquire the cross-worker lock and spawn lock-keeper + tier
        threads. Returns True on success, False if the lock is held by
        another worker (this instance stays idle).
        """
        self._lock_token = _acquire_lock(self._redis, LOCK_KEY, LOCK_TTL_SEC)
        if self._lock_token is None:
            log.info(
                "bot_state_producer: another worker holds the leader lock; "
                "this instance staying idle"
            )
            return False

        log.info(
            f"bot_state_producer: starting "
            f"{len([t for t in self._tiers if t[1]])} active tiers "
            f"for {len(self._tickers)} tickers"
        )

        # Lock-keeper thread first — must be running before any tier work.
        keeper = threading.Thread(
            target=_run_lock_keeper,
            kwargs={
                "redis_client": self._redis,
                "lock_key": LOCK_KEY,
                "lock_token": self._lock_token,
                "ttl_sec": LOCK_TTL_SEC,
                "refresh_interval_sec": LOCK_REFRESH_INTERVAL_SEC,
                "stop_event": self._stop,
            },
            name="bsp-lock-keeper",
            daemon=True,
        )
        keeper.start()
        self._threads.append(keeper)

        # Tier daemons — only spawn for tiers with at least one intent.
        for tier_name, intents, cadence, ttl, stagger in self._tiers:
            if not intents:
                continue
            t = threading.Thread(
                target=self._tier_loop,
                args=(tier_name, intents, cadence, ttl, stagger),
                name=f"bsp-tier-{tier_name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        return True

    def stop(self) -> None:
        """Signal all threads to exit at the next sleep boundary; release
        the lock. Idempotent — safe to call twice."""
        global _singleton
        self._stop.set()
        if self._lock_token is not None:
            _release_lock(self._redis, LOCK_KEY, self._lock_token)
            self._lock_token = None
        # Clear the diagnostic singleton if this instance was it. A
        # different live instance shouldn't get nulled by an unrelated
        # stopped one, but in practice there's only ever one producer
        # per process.
        if _singleton is self:
            _singleton = None

    def join(self, timeout: Optional[float] = None) -> None:
        for t in self._threads:
            t.join(timeout=timeout)

    # ──────────────────────────────────────────────────────────────────

    def _tier_loop(self, tier_name: str, intents: List[str],
                   cadence_sec: int, ttl_sec: int, stagger_sec: int) -> None:
        """Forever-loop: stagger initial start, then pass + sleep. Outer
        try/except restarts the loop on unexpected crashes (5s backoff)."""
        # Initial stagger.
        if self._stop.wait(timeout=stagger_sec):
            return

        # Lazy-import production deps so unit tests can stub the module
        # before the thread runs the first pass. Wrapped in try/except
        # so a broken import (circular dep, refactor breakage, missing
        # module) surfaces as a visible log line instead of a silent
        # dead thread that would let the lock-keeper refresh forever
        # while no envelopes get written.
        try:
            from canonical_expiration import canonical_expiration
            from raw_inputs import fetch_raw_inputs
            from bot_state import BotState
        except Exception as e:
            log.error(
                f"[bsp tier={tier_name}] failed to import production "
                f"dependencies: {e}; tier thread exiting"
            )
            return

        while not self._stop.is_set():
            t_start = time.time()
            try:
                _run_tier_pass(
                    tier_name=tier_name,
                    intents=intents,
                    ttl_sec=ttl_sec,
                    tickers=self._tickers,
                    cached_md=self._cached_md,
                    redis_client=self._redis,
                    canonical_expiration_fn=canonical_expiration,
                    fetch_raw_inputs_fn=fetch_raw_inputs,
                    build_from_raw_fn=BotState.build_from_raw,
                )
            except Exception as e:
                log.error(f"[bsp tier={tier_name}] outer loop crashed: {e}")
                # 5s backoff before retry — avoid tight crash loops.
                if self._stop.wait(timeout=5.0):
                    return
                continue
            elapsed = time.time() - t_start
            sleep_for = max(0.0, cadence_sec - elapsed)
            log.info(
                f"[bsp tier={tier_name}] pass complete in {elapsed:.1f}s; "
                f"sleeping {sleep_for:.1f}s"
            )
            if self._stop.wait(timeout=sleep_for):
                return


# ─────────────────────────────────────────────────────────────────────
# Public factory — only entry point. Returns None when the producer
# should not run for ANY reason (env flag off, no redis, no tickers,
# lock held by another worker).
# ─────────────────────────────────────────────────────────────────────

_singleton: Optional["BotStateProducer"] = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def start_producer(cached_md, redis_client) -> Optional["BotStateProducer"]:
    """Factory. Returns a started BotStateProducer or None.

    Returns None when:
      - BOT_STATE_PRODUCER_ENABLED is unset/false (env-flag rollback path)
      - redis_client is None (we can't write envelopes)
      - BOT_STATE_PRODUCER_TICKERS is empty (nothing to produce)
      - cross-worker lock is held by another worker (silent — that
        worker IS the producer)
    """
    global _singleton

    if not _env_bool("BOT_STATE_PRODUCER_ENABLED", default=False):
        log.info("bot_state_producer: BOT_STATE_PRODUCER_ENABLED is off; not starting")
        return None
    if redis_client is None:
        log.warning("bot_state_producer: no redis_client; not starting")
        return None

    tickers = _parse_tickers(os.environ.get("BOT_STATE_PRODUCER_TICKERS"))
    if not tickers:
        log.warning("bot_state_producer: BOT_STATE_PRODUCER_TICKERS empty; not starting")
        return None

    p = BotStateProducer(
        tickers=tickers,
        tier_a_intents=_parse_intents(os.environ.get("BOT_STATE_PRODUCER_INTENTS_TIER_A", "front")),
        tier_b_intents=_parse_intents(os.environ.get("BOT_STATE_PRODUCER_INTENTS_TIER_B", "t7")),
        tier_c_intents=_parse_intents(os.environ.get("BOT_STATE_PRODUCER_INTENTS_TIER_C", "")),
        tier_a_cadence=_parse_int_env(os.environ.get("BOT_STATE_PRODUCER_CADENCE_TIER_A"), 60),
        tier_b_cadence=_parse_int_env(os.environ.get("BOT_STATE_PRODUCER_CADENCE_TIER_B"), 180),
        tier_c_cadence=_parse_int_env(os.environ.get("BOT_STATE_PRODUCER_CADENCE_TIER_C"), 600),
        cached_md=cached_md,
        redis_client=redis_client,
    )
    if not p.start():
        # Lock held by another worker — start() returned False. Don't
        # cache a non-running instance in _singleton.
        return None
    _singleton = p
    return p


def get_producer() -> Optional["BotStateProducer"]:
    """Diagnostic accessor for the running producer (or None)."""
    return _singleton
