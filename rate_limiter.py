# rate_limiter.py
# ═══════════════════════════════════════════════════════════════════
# Global token-bucket rate limiter for external API calls.
#
# Defensive: prevents runaway loops from blowing through broker/data
# vendor rate limits during a burst. Used by schwab_adapter (Schwab
# has a 120 calls/min ceiling — we throttle at 110 to leave headroom).
#
# Design:
#   - Single process-wide bucket. Multiple workers share the same
#     token pool, so total API pressure across threads stays bounded.
#   - If a caller would exceed the rate, it BLOCKS (doesn't drop).
#     All current call sites tolerate a ~1-3s wait; dropping calls
#     would cause silent data holes.
#   - Waits of >5s get a warn-level log so we notice pathological
#     contention.
#
# Usage:
#   from rate_limiter import rate_limited
#
#   # Decorator form
#   @rate_limited(cost=1, label="schwab.chain")
#   def get_chain(...):
#       return requests.get(...)
#
#   # Context-manager form
#   with rate_limited(cost=1, label="schwab.spot"):
#       r = requests.get(...)
#
# Env vars:
#   SCHWAB_RATE_PER_MIN   int, default 110 (Schwab ceiling is 120;
#                         we leave 10/min headroom for streaming
#                         heartbeats and manual /status commands)
#   SCHWAB_RATE_BURST     int, default = SCHWAB_RATE_PER_MIN (allow
#                         full-minute bursts at startup)
#   SCHWAB_RATE_TIMEOUT_S int, default 60 (give up if waiting longer
#                         than this — raises RateLimitTimeout)
# ═══════════════════════════════════════════════════════════════════

import os
import time
import logging
import threading
import functools
from contextlib import contextmanager

log = logging.getLogger(__name__)


class RateLimitTimeout(RuntimeError):
    """Raised when acquire() waits longer than the configured timeout."""


class TokenBucket:
    """Thread-safe token bucket.

    Refills at `rate_per_min / 60` tokens per second up to `capacity`.
    acquire(n) returns True on success, False on timeout. Never drops
    on success — always consumes exactly `n` tokens when it returns.
    """

    def __init__(self, rate_per_min: int = 110, capacity: int = None):
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be > 0")
        self._rate = rate_per_min / 60.0         # tokens per second
        self._capacity = float(capacity or rate_per_min)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self.rate_per_min = rate_per_min         # read-only, for logging

    def _refill_locked(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    def acquire(self, cost: float = 1.0, timeout: float = 60.0) -> bool:
        """Wait until `cost` tokens are available, then consume them.

        Returns True on success, False if timeout elapsed before enough
        tokens became available. Never partial-consumes.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return True
                # How long until we'll have enough?
                deficit = cost - self._tokens
                wait = deficit / self._rate
            # Sleep outside the lock so other callers can make progress.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Cap each sleep at 0.25s so we respond quickly to new refills
            # from parallel callers (avoids sleeping past the moment enough
            # tokens become available).
            time.sleep(min(wait, remaining, 0.25))

    def snapshot(self) -> dict:
        """For /status — current tokens, capacity, rate."""
        with self._lock:
            self._refill_locked()
            return {
                "tokens": round(self._tokens, 2),
                "capacity": self._capacity,
                "rate_per_min": self.rate_per_min,
            }


# ─────────────────────────────────────────────────────────────
# Global Schwab bucket (process-wide singleton)
# ─────────────────────────────────────────────────────────────

_SCHWAB_RATE_PER_MIN = int(os.getenv("SCHWAB_RATE_PER_MIN", "110") or 110)
_SCHWAB_RATE_BURST = int(os.getenv("SCHWAB_RATE_BURST", str(_SCHWAB_RATE_PER_MIN)) or _SCHWAB_RATE_PER_MIN)
_SCHWAB_RATE_TIMEOUT_S = float(os.getenv("SCHWAB_RATE_TIMEOUT_S", "60") or 60)

SCHWAB_BUCKET = TokenBucket(
    rate_per_min=_SCHWAB_RATE_PER_MIN,
    capacity=_SCHWAB_RATE_BURST,
)

log.info(
    f"Schwab rate limiter initialized: {_SCHWAB_RATE_PER_MIN}/min "
    f"(burst={_SCHWAB_RATE_BURST}, timeout={_SCHWAB_RATE_TIMEOUT_S}s)"
)


# ─────────────────────────────────────────────────────────────
# Public API: decorator + context manager
# ─────────────────────────────────────────────────────────────

def rate_limited(cost: float = 1.0, label: str = "api", bucket: TokenBucket = None):
    """Decorator: throttle a function to the global Schwab bucket.

    Usage:
        @rate_limited(cost=1, label="schwab.chain")
        def fetch_chain(ticker): ...
    """
    _bucket = bucket or SCHWAB_BUCKET

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            acquired = _bucket.acquire(cost=cost, timeout=_SCHWAB_RATE_TIMEOUT_S)
            waited = time.monotonic() - t0
            if not acquired:
                raise RateLimitTimeout(
                    f"Rate limiter timeout after {_SCHWAB_RATE_TIMEOUT_S:.0f}s "
                    f"for {label} (bucket {_bucket.rate_per_min}/min)"
                )
            if waited > 5.0:
                log.warning(
                    f"Rate limiter waited {waited:.1f}s for {label} "
                    f"(bucket {_bucket.rate_per_min}/min)"
                )
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@contextmanager
def rate_limit(cost: float = 1.0, label: str = "api", bucket: TokenBucket = None):
    """Context manager form of the same throttle.

    Usage:
        with rate_limit(cost=1, label="schwab.spot"):
            response = requests.get(url)
    """
    _bucket = bucket or SCHWAB_BUCKET
    t0 = time.monotonic()
    acquired = _bucket.acquire(cost=cost, timeout=_SCHWAB_RATE_TIMEOUT_S)
    waited = time.monotonic() - t0
    if not acquired:
        raise RateLimitTimeout(
            f"Rate limiter timeout after {_SCHWAB_RATE_TIMEOUT_S:.0f}s "
            f"for {label} (bucket {_bucket.rate_per_min}/min)"
        )
    if waited > 5.0:
        log.warning(
            f"Rate limiter waited {waited:.1f}s for {label} "
            f"(bucket {_bucket.rate_per_min}/min)"
        )
    yield


def get_schwab_rate_snapshot() -> dict:
    """For /status or diagnostic endpoints."""
    return SCHWAB_BUCKET.snapshot()
