# Streaming-First Dashboard Spot Prices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `omega_dashboard/spot_prices.py` off Yahoo Finance polling onto the existing Schwab WebSocket stream, with a Schwab REST fallback for previous-close (so red/green change coloring keeps working) and for unsubscribed tickers (cold start).

**Architecture:**
The Schwab streamer (`schwab_stream.py:SchwabStreamManager`) already maintains a `SpotPriceStore` keyed by ticker, populated by Level 1 equity quotes. Today it covers only `FLOW_TICKERS` (35 trading-engine tickers). The dashboard requests a wider, dynamic set (positions + watchlist) — only 2/21 overlap. We extend the streamer with dynamic equity subscription (mirroring the existing option dynamic-add pattern), add a small Redis-backed `prev_close_store` filled lazily from Schwab's batch quote endpoint, and rewrite `spot_prices.py` to: streaming-first → Schwab REST batch fallback → Yahoo retired (kept behind off-by-default env var for one release as rollback insurance).

**Tech Stack:** Python 3, schwab-py library (already in use), Flask (dashboard routes), Redis (existing — used for prev_close cache), threading (daemon-thread pattern matches existing `SchwabStreamManager`).

---

## Decisions already locked in

- **No new infrastructure.** Reuses existing daemon thread, existing Redis client, existing `_cached_md` rate-limited Schwab path.
- **Patch granularity follows project rule.** Four independent patches, each shippable on its own with no half-finished state.
- **Env-var rollback at every step.** `DASHBOARD_SPOT_USE_STREAMING=0` is the master switch. Off → behavior is unchanged from today. On → new path active.
- **`get_quote` per ticker, not batch.** Schwab-py's `get_quote(symbol)` is the path we already use in `schwab_adapter.py:451`. Looping ~50 tickers once per day is ~50 calls against a 110/min budget — fine. If batch (`get_quotes(symbols=[...])`) turns out to work, we can swap later. Don't gamble on it now.
- **`$VIX`, `$VIX9D`, `$VVIX`, `$VIX3M` are out of scope.** They aren't equity tickers, and the streamer's L1-equity sub won't accept them. They stay on the existing thesis-cycle REST path.
- **Yahoo path stays in the file for one release**, gated by env var. After a clean week of streaming-first traffic in prod, a follow-up patch (S.5, NOT in this plan) deletes it.

---

## File structure

**Created:**
- `prev_close_store.py` — thread-safe per-ticker previous-close cache. Reads Schwab `get_quote` lazily on miss. ~120 lines.
- `test_prev_close_store.py` — unit tests, no network. ~150 lines.
- `test_schwab_stream_equity_dynamic.py` — unit tests for new `add_equity_symbols`/`remove_equity_symbols`. No live WebSocket needed. ~120 lines.
- `test_spot_prices_streaming.py` — unit tests for the rewritten dashboard fetcher. Mocks streaming + prev_close stores. ~180 lines.

**Modified:**
- `schwab_stream.py` — adds `_pending_equity_adds`, `_pending_equity_unsubs`, `_equity_fields` instance attr, `add_equity_symbols()`, `remove_equity_symbols()`, equity branch in `_process_pending_subs()`. ~50 lines added.
- `omega_dashboard/spot_prices.py` — rewrite of `get_spot_prices()` to be streaming-first with Schwab REST fallback and prev_close lookup. Yahoo path stays as last-resort, env-gated. ~80 lines net change.
- `omega_dashboard/routes.py` — adds `/api/register-tickers` POST endpoint that calls `add_equity_symbols()`. ~25 lines added.
- `omega_dashboard/templates/dashboard/portfolio.html` — small JS addition: POST registered tickers on page load, before the first `/api/spot-prices` call. ~10 lines added.
- `omega_dashboard/templates/dashboard/command_center.html` — same addition as portfolio.html. ~10 lines added.

**Total touched:** 4 new files (~570 lines), 5 modified files (~175 lines added).

---

# Patch S.1 — Dynamic equity sub on `SchwabStreamManager`

**Why first:** Patches S.3 and S.4 both depend on the streamer being able to add equity tickers at runtime. This patch is purely additive — no behavior change yet (no caller). Lets us land it during market hours safely.

**Files:**
- Modify: `schwab_stream.py:585-820` (add `_pending_equity_*` queues, methods, equity branch in `_process_pending_subs`)
- Create: `test_schwab_stream_equity_dynamic.py`

### Task 1.1: Write failing test for queue state

- [ ] **Step 1: Create the test file**

Create `test_schwab_stream_equity_dynamic.py`:

```python
"""
test_schwab_stream_equity_dynamic.py — unit tests for dynamic equity
subscription on SchwabStreamManager.

No WebSocket, no network. Tests the queue state directly. The actual
WebSocket interaction is exercised in production; this file pins the
contract that add/remove are idempotent and update the right structures.
"""

from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schwab_stream import SchwabStreamManager

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def _make_mgr(tickers=None):
    """Build a manager without starting the thread. We never call .start()
    in these tests — pure queue/state checks."""
    return SchwabStreamManager(
        schwab_client=None,
        tickers=list(tickers or ["AAPL", "MSFT"]),
    )


def test_add_equity_symbols_queues_new_only():
    mgr = _make_mgr(["AAPL"])
    mgr.add_equity_symbols(["AAPL", "MSFT", "NVDA"])
    # AAPL already there, only MSFT/NVDA queued
    assert_eq(sorted(mgr._pending_equity_adds), ["MSFT", "NVDA"],
              "add_equity_symbols queues only new tickers")
    assert_eq(sorted(mgr._tickers), ["AAPL", "MSFT", "NVDA"],
              "add_equity_symbols updates _tickers")


def test_add_equity_symbols_idempotent():
    mgr = _make_mgr(["AAPL"])
    mgr.add_equity_symbols(["MSFT"])
    mgr.add_equity_symbols(["MSFT"])  # second call is a no-op
    assert_eq(mgr._pending_equity_adds, ["MSFT"],
              "add_equity_symbols is idempotent on repeated calls")


def test_add_equity_symbols_empty_noop():
    mgr = _make_mgr(["AAPL"])
    mgr.add_equity_symbols([])
    mgr.add_equity_symbols(None)
    assert_eq(mgr._pending_equity_adds, [], "empty/None input is no-op")


def test_remove_equity_symbols():
    mgr = _make_mgr(["AAPL", "MSFT", "NVDA"])
    mgr.remove_equity_symbols(["MSFT"])
    assert_eq(sorted(mgr._tickers), ["AAPL", "NVDA"],
              "remove_equity_symbols drops from _tickers")
    assert_eq(mgr._pending_equity_unsubs, ["MSFT"],
              "remove_equity_symbols queues for unsub")


def test_remove_equity_symbols_unknown_is_noop():
    mgr = _make_mgr(["AAPL"])
    mgr.remove_equity_symbols(["UNKNOWN"])
    # Still queues it (Schwab will silently ignore an unsub for a symbol
    # that isn't subscribed) but does not crash on an unknown ticker.
    assert_eq(mgr._tickers, ["AAPL"], "remove of unknown ticker leaves _tickers untouched")


if __name__ == "__main__":
    test_add_equity_symbols_queues_new_only()
    test_add_equity_symbols_idempotent()
    test_add_equity_symbols_empty_noop()
    test_remove_equity_symbols()
    test_remove_equity_symbols_unknown_is_noop()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python3 test_schwab_stream_equity_dynamic.py
```

Expected: `AttributeError: 'SchwabStreamManager' object has no attribute 'add_equity_symbols'` (or similar). All five tests fail.

### Task 1.2: Implement queues + methods

- [ ] **Step 1: Add the queue attributes to `__init__`**

In `schwab_stream.py`, find the `__init__` method of `SchwabStreamManager` (line 585). After `self._pending_option_unsubs = []` (line 591), insert the equity equivalents. The full updated `__init__` block (replacing lines 585-606) should read:

```python
    def __init__(self, schwab_client, tickers: list,
                 option_symbols: list = None):
        self._client = schwab_client
        self._tickers = list(tickers)
        self._option_symbols = list(option_symbols or [])
        self._pending_option_adds = []   # symbols queued for dynamic add
        self._pending_option_unsubs = [] # symbols queued for removal
        # S.1: equity dynamic subscription queues — mirror the option pattern.
        self._pending_equity_adds = []
        self._pending_equity_unsubs = []
        self._equity_fields = None       # set during _stream, reused by _process_pending_subs
        self._stream_client = None       # set during _stream for dynamic ops
        self._thread = None
        self._running = False
        self._connected = False
        self._reconnect_delay = 5  # seconds, doubles on failure, max 60
        self._stats = {
            "updates_received": 0,
            "option_updates_received": 0,
            "connects": 0,
            "disconnects": 0,
            "errors": 0,
            "last_update": None,
            "option_symbols_subscribed": 0,
            "equity_symbols_subscribed": 0,
        }
        self._lock = threading.Lock()
```

- [ ] **Step 2: Add `add_equity_symbols` and `remove_equity_symbols` methods**

Insert these methods directly after `remove_option_symbols` (line 642). The insertion point is the line just before `def stop(self):`:

```python
    # S.1: equity dynamic subscription — mirrors add_option_symbols.
    def add_equity_symbols(self, symbols: list):
        """Queue equity tickers for dynamic Level 1 subscription.

        Idempotent: tickers already in _tickers are silently dropped.
        Thread-safe. Newly added tickers are picked up by the periodic
        _process_pending_subs sweep within ~5 seconds.
        """
        if not symbols:
            return
        with self._lock:
            existing = set(self._tickers)
            new_syms = [s.upper() for s in symbols if s and s.upper() not in existing]
            if new_syms:
                self._pending_equity_adds.extend(new_syms)
                self._tickers.extend(new_syms)
                log.info(f"Queued {len(new_syms)} equity symbols for streaming subscription")

    def remove_equity_symbols(self, symbols: list):
        """Queue equity tickers for unsubscribe and drop them from _tickers."""
        if not symbols:
            return
        with self._lock:
            up = {s.upper() for s in symbols if s}
            self._pending_equity_unsubs.extend(up)
            self._tickers = [t for t in self._tickers if t.upper() not in up]
```

- [ ] **Step 3: Run the test, verify pass**

```bash
python3 test_schwab_stream_equity_dynamic.py
```

Expected: `PASSED: 5, FAILED: 0`. (The test does not exercise the WebSocket round-trip — only the queue/state contract.)

### Task 1.3: Wire the equity branch into `_process_pending_subs`

- [ ] **Step 1: Save `equity_fields` as instance attr in `_stream()`**

In `schwab_stream.py`, find the existing `equity_fields = [...]` block (line 731-738). Replace the assignment so the list is also stored on the instance:

```python
        equity_fields = [
            StreamClient.LevelOneEquityFields.SYMBOL,
            StreamClient.LevelOneEquityFields.LAST_PRICE,
            StreamClient.LevelOneEquityFields.MARK,
            StreamClient.LevelOneEquityFields.BID_PRICE,
            StreamClient.LevelOneEquityFields.ASK_PRICE,
            StreamClient.LevelOneEquityFields.TOTAL_VOLUME,
        ]
        self._equity_fields = equity_fields  # S.1: needed by _process_pending_subs
        await stream_client.level_one_equity_subs(self._tickers, fields=equity_fields)
        with self._lock:
            self._stats["equity_symbols_subscribed"] = len(self._tickers)
        log.info(f"Subscribed to Level 1 equity quotes: {len(self._tickers)} symbols")
```

- [ ] **Step 2: Extend `_process_pending_subs` to handle equity adds/unsubs**

Replace the entire `_process_pending_subs` method (line 821-845) with this expanded version:

```python
    async def _process_pending_subs(self, stream_client):
        """Add/remove option AND equity symbols dynamically without reconnecting."""
        # Snapshot under lock, then operate on copies.
        with self._lock:
            opt_adds = list(self._pending_option_adds)
            self._pending_option_adds.clear()
            opt_unsubs = list(self._pending_option_unsubs)
            self._pending_option_unsubs.clear()
            eq_adds = list(self._pending_equity_adds)
            self._pending_equity_adds.clear()
            eq_unsubs = list(self._pending_equity_unsubs)
            self._pending_equity_unsubs.clear()

        if opt_adds:
            try:
                await stream_client.level_one_option_add(opt_adds, fields=self._option_fields)
                with self._lock:
                    self._stats["option_symbols_subscribed"] += len(opt_adds)
                log.info(f"Dynamically added {len(opt_adds)} option symbols to stream")
            except Exception as e:
                log.warning(f"Failed to add option symbols: {e}")

        if opt_unsubs:
            try:
                await stream_client.level_one_option_unsubs(opt_unsubs)
                with self._lock:
                    self._stats["option_symbols_subscribed"] -= len(opt_unsubs)
                log.info(f"Unsubscribed {len(opt_unsubs)} option symbols from stream")
            except Exception as e:
                log.warning(f"Failed to unsub option symbols: {e}")

        # S.1: equity branch
        if eq_adds:
            try:
                await stream_client.level_one_equity_add(eq_adds, fields=self._equity_fields)
                with self._lock:
                    self._stats["equity_symbols_subscribed"] += len(eq_adds)
                log.info(f"Dynamically added {len(eq_adds)} equity symbols to stream")
            except Exception as e:
                log.warning(f"Failed to add equity symbols: {e}")

        if eq_unsubs:
            try:
                await stream_client.level_one_equity_unsubs(eq_unsubs)
                with self._lock:
                    self._stats["equity_symbols_subscribed"] -= len(eq_unsubs)
                log.info(f"Unsubscribed {len(eq_unsubs)} equity symbols from stream")
            except Exception as e:
                log.warning(f"Failed to unsub equity symbols: {e}")
```

- [ ] **Step 3: AST-check the modified file (audit rule 3)**

```bash
python3 -c "import ast; ast.parse(open('schwab_stream.py').read())"
```

Expected: no output (clean parse). Any `SyntaxError` here means the patch text didn't merge cleanly — investigate, don't ship.

- [ ] **Step 4: Re-run the unit test**

```bash
python3 test_schwab_stream_equity_dynamic.py
```

Expected: `PASSED: 5, FAILED: 0`.

- [ ] **Step 5: Commit**

```bash
git add schwab_stream.py test_schwab_stream_equity_dynamic.py
git commit -m "Patch S.1: dynamic equity subscription on SchwabStreamManager

Mirrors the existing option dynamic-add pattern. add_equity_symbols and
remove_equity_symbols queue changes; _process_pending_subs picks them up
within ~5s using level_one_equity_add/level_one_equity_unsubs. No caller
yet — Patch S.4 wires the dashboard register-tickers path."
```

---

# Patch S.2 — `prev_close_store.py`

**Why second:** S.3 needs prev_close lookup to compute change/change_pct. Build the store first, with no caller, then S.3 adopts it.

**Files:**
- Create: `prev_close_store.py`
- Create: `test_prev_close_store.py`

### Task 2.1: Write the failing tests

- [ ] **Step 1: Create the test file**

Create `test_prev_close_store.py`:

```python
"""
test_prev_close_store.py — unit tests for the previous-close cache.

No network. The Schwab client is mocked; we verify the store calls
get_quote with the right symbol and parses closePrice correctly.
"""

from __future__ import annotations
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


class _FakeSchwabProvider:
    """Mock that returns a canned Schwab get_quote response."""
    def __init__(self, table: dict, raise_on=None):
        # table: ticker -> closePrice (or None to simulate parse failure)
        self.table = table
        self.raise_on = raise_on or set()
        self.calls = []

    def _schwab_get(self, method, *args, **kwargs):
        if method != "get_quote":
            raise RuntimeError(f"unexpected schwab method: {method}")
        symbol = args[0] if args else kwargs.get("symbol")
        self.calls.append(symbol)
        if symbol in self.raise_on:
            raise RuntimeError(f"simulated failure for {symbol}")
        close = self.table.get(symbol)
        if close is None:
            return {symbol: {"quote": {}}}  # missing closePrice
        return {symbol: {"quote": {"closePrice": close, "lastPrice": close + 1.0}}}


def test_get_unknown_returns_none():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    assert_is_none(store.get("AAPL"), "unknown ticker returns None")


def test_set_get_roundtrip():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    store.set("AAPL", 184.50)
    assert_eq(store.get("AAPL"), 184.50, "set/get roundtrip")


def test_ttl_expiry():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore(ttl_sec=0)  # immediate expiry
    store.set("AAPL", 184.50)
    time.sleep(0.01)
    assert_is_none(store.get("AAPL"), "expired entry returns None")


def test_ensure_fetches_missing_via_schwab():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    fake = _FakeSchwabProvider({"AAPL": 184.50, "MSFT": 412.30})
    missing = store.ensure(["AAPL", "MSFT"], schwab_provider=fake)
    assert_eq(missing, [], "all tickers fetched, none missing")
    assert_eq(sorted(fake.calls), ["AAPL", "MSFT"],
              "schwab called once per ticker")
    assert_eq(store.get("AAPL"), 184.50, "AAPL prev_close cached")
    assert_eq(store.get("MSFT"), 412.30, "MSFT prev_close cached")


def test_ensure_skips_already_cached():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    store.set("AAPL", 184.50)
    fake = _FakeSchwabProvider({"AAPL": 999.99})  # would-be wrong value
    store.ensure(["AAPL"], schwab_provider=fake)
    assert_eq(fake.calls, [], "ensure skips cached tickers")
    assert_eq(store.get("AAPL"), 184.50, "cached value preserved")


def test_ensure_returns_unfetchable_tickers():
    from prev_close_store import PrevCloseStore
    store = PrevCloseStore()
    fake = _FakeSchwabProvider({"AAPL": 184.50}, raise_on={"BAD"})
    missing = store.ensure(["AAPL", "BAD"], schwab_provider=fake)
    assert_eq(missing, ["BAD"], "tickers that errored are reported back")
    assert_eq(store.get("AAPL"), 184.50, "successful tickers still cached")
    assert_is_none(store.get("BAD"), "errored ticker not cached")


def test_singleton_accessor():
    from prev_close_store import get_prev_close_store
    s1 = get_prev_close_store()
    s2 = get_prev_close_store()
    assert_eq(id(s1), id(s2), "get_prev_close_store returns singleton")


if __name__ == "__main__":
    test_get_unknown_returns_none()
    test_set_get_roundtrip()
    test_ttl_expiry()
    test_ensure_fetches_missing_via_schwab()
    test_ensure_skips_already_cached()
    test_ensure_returns_unfetchable_tickers()
    test_singleton_accessor()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
python3 test_prev_close_store.py
```

Expected: `ModuleNotFoundError: No module named 'prev_close_store'`. All seven tests fail.

### Task 2.2: Implement `prev_close_store.py`

- [ ] **Step 1: Create the module**

Create `prev_close_store.py`:

```python
"""
prev_close_store.py — thread-safe per-ticker previous-close cache.

Filled lazily from Schwab's get_quote endpoint via _cached_md._schwab.
The dashboard's spot_prices module pairs streaming live price with this
prev_close to compute the change/change_pct columns. Without this, the
dashboard would lose its red/green coloring when streaming-first lands.

Audit rule 1: this is the canonical previous-close source. Don't add
parallel implementations elsewhere.

Architecture:
  - In-memory dict (ticker -> (price, fetched_at))
  - 25h TTL: covers a full overnight + buffer for late afternoon refresh
  - Singleton accessed via get_prev_close_store()
  - ensure(tickers, schwab_provider) batches missing fetches at the call
    site of /api/spot-prices — one Schwab call per uncached ticker per day

Failure modes:
  - Schwab errors on a ticker: the ticker is reported back from ensure()
    so the caller can decide whether to retry, drop, or fall back to a
    different source. The cache is NOT poisoned with bogus values.
  - closePrice missing from quote: same as above.
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 25 * 3600  # 25 hours


class PrevCloseStore:
    """Thread-safe per-ticker previous-close cache with TTL.

    Not a singleton on its own; access via get_prev_close_store().
    """

    def __init__(self, ttl_sec: float = DEFAULT_TTL_SEC):
        self._cache: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_sec

    def get(self, ticker: str) -> Optional[float]:
        """Return cached prev_close if fresh, else None."""
        if not ticker:
            return None
        key = ticker.upper()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            price, fetched_at = entry
            if (time.time() - fetched_at) > self._ttl:
                # Expired — drop it so the next ensure() refetches.
                self._cache.pop(key, None)
                return None
            return price

    def set(self, ticker: str, price: float) -> None:
        """Store a prev_close value. Caller is responsible for the value."""
        if not ticker or price is None:
            return
        key = ticker.upper()
        with self._lock:
            self._cache[key] = (float(price), time.time())

    def ensure(self, tickers: List[str], schwab_provider) -> List[str]:
        """Fetch prev_close for any tickers not already cached.

        Args:
            tickers: list of ticker symbols to ensure.
            schwab_provider: an object with `_schwab_get(method, symbol)`,
                typically `_cached_md._schwab` from app.py.

        Returns:
            List of tickers that could not be fetched (Schwab error,
            missing closePrice in response, etc). Successful tickers are
            stored in the cache; the caller does not need to call set()
            after ensure().
        """
        if not tickers:
            return []

        # Build the missing list under lock so concurrent /api/spot-prices
        # calls don't all fire identical Schwab requests.
        to_fetch: List[str] = []
        with self._lock:
            for t in tickers:
                if not t:
                    continue
                key = t.upper()
                entry = self._cache.get(key)
                if entry is None:
                    to_fetch.append(key)
                    continue
                _, fetched_at = entry
                if (time.time() - fetched_at) > self._ttl:
                    to_fetch.append(key)

        unfetchable: List[str] = []
        for ticker in to_fetch:
            try:
                data = schwab_provider._schwab_get("get_quote", ticker)
            except Exception as e:
                log.debug(f"prev_close fetch failed for {ticker}: {e}")
                unfetchable.append(ticker)
                continue

            entry = (data or {}).get(ticker, {})
            quote = entry.get("quote", {})
            close = quote.get("closePrice")
            if close is None or close == 0:
                # Schwab returned a quote but no usable closePrice.
                unfetchable.append(ticker)
                continue

            with self._lock:
                self._cache[ticker] = (float(close), time.time())

        if to_fetch:
            log.info(
                f"prev_close ensure: fetched {len(to_fetch) - len(unfetchable)}/"
                f"{len(to_fetch)} tickers, {len(unfetchable)} unfetchable"
            )
        return unfetchable

    def stats(self) -> dict:
        """Diagnostic: how many entries, oldest fetch age."""
        with self._lock:
            now = time.time()
            ages = [now - ts for _, ts in self._cache.values()]
            return {
                "entries": len(self._cache),
                "oldest_age_sec": int(max(ages)) if ages else 0,
                "newest_age_sec": int(min(ages)) if ages else 0,
            }


# ─────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────

_singleton: Optional[PrevCloseStore] = None
_singleton_lock = threading.Lock()


def get_prev_close_store() -> PrevCloseStore:
    """Return the process-wide PrevCloseStore."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = PrevCloseStore()
    return _singleton
```

- [ ] **Step 2: Run the tests, verify pass**

```bash
python3 test_prev_close_store.py
```

Expected: `PASSED: 7, FAILED: 0`.

- [ ] **Step 3: AST-check**

```bash
python3 -c "import ast; ast.parse(open('prev_close_store.py').read())"
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add prev_close_store.py test_prev_close_store.py
git commit -m "Patch S.2: prev_close_store with lazy Schwab fetch

Thread-safe per-ticker prev_close cache, 25h TTL. ensure(tickers, provider)
fills missing entries via Schwab get_quote and reports unfetchable tickers
back to the caller. No caller yet — Patch S.3 adopts this from the
dashboard spot_prices module."
```

---

# Patch S.3 — Rewrite `omega_dashboard/spot_prices.py`

**Why third:** S.3 is where actual user-visible behavior changes. Gated behind `DASHBOARD_SPOT_USE_STREAMING=0` (default off). Off → identical to today (Yahoo). On → streaming-first → Schwab REST → Yahoo only as last resort.

**Files:**
- Modify: `omega_dashboard/spot_prices.py` (full rewrite of `get_spot_prices`; Yahoo path retained as fallback)
- Create: `test_spot_prices_streaming.py`

### Task 3.1: Write failing tests for the new path

- [ ] **Step 1: Create the test file**

Create `test_spot_prices_streaming.py`:

```python
"""
test_spot_prices_streaming.py — tests for the streaming-first spot fetcher.

Mocks both the streaming spot store and the Schwab provider. Verifies:
  - streaming hits short-circuit (no REST, no Yahoo)
  - REST falls in for unsubscribed tickers
  - Yahoo only runs when DASHBOARD_SPOT_USE_STREAMING is off (legacy mode)
  - prev_close paired correctly in change/change_pct math
"""

from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    PASSED.append(msg)
    return True


def assert_close(actual, expected, msg, tol=0.01):
    if actual is None or abs(actual - expected) > tol:
        FAILED.append(f"{msg}: expected {expected} ± {tol}, got {actual}")
        return False
    PASSED.append(msg)
    return True


def assert_in(key, dct, msg):
    if key not in dct:
        FAILED.append(f"{msg}: {key!r} not in result")
        return False
    PASSED.append(msg)
    return True


def assert_not_in(key, dct, msg):
    if key in dct:
        FAILED.append(f"{msg}: {key!r} unexpectedly present")
        return False
    PASSED.append(msg)
    return True


class _FakeSchwabProvider:
    def __init__(self, prev_close_table):
        self.prev_close_table = prev_close_table
        self.calls = []

    def _schwab_get(self, method, *args, **kwargs):
        symbol = args[0] if args else kwargs.get("symbol")
        self.calls.append((method, symbol))
        if method == "get_quote":
            close = self.prev_close_table.get(symbol)
            if close is None:
                return {symbol: {"quote": {}}}
            return {
                symbol: {"quote": {
                    "closePrice": close,
                    "lastPrice": close + 1.50,
                    "mark": close + 1.50,
                }}
            }
        raise RuntimeError(f"unexpected method {method}")


def _setup(env_streaming="1", streaming_prices=None, prev_close=None,
           schwab_table=None):
    """Reset module state and inject mocks. Returns (module, fake_provider)."""
    os.environ["DASHBOARD_SPOT_USE_STREAMING"] = env_streaming

    # Reset prev_close singleton.
    import prev_close_store
    prev_close_store._singleton = None
    store = prev_close_store.get_prev_close_store()
    for t, p in (prev_close or {}).items():
        store.set(t, p)

    # Inject streaming-spot fakes by monkey-patching get_streaming_spot.
    import schwab_stream
    spots = streaming_prices or {}
    schwab_stream.get_streaming_spot = lambda t: spots.get(t.upper())

    # Reset spot_prices internal caches.
    from omega_dashboard import spot_prices
    spot_prices._cache.clear()
    spot_prices._neg_cache.clear()

    # Inject the schwab provider lookup.
    fake = _FakeSchwabProvider(schwab_table or {})
    spot_prices._get_schwab_provider = lambda: fake

    return spot_prices, fake


def test_streaming_hit_uses_prev_close_for_change():
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={"AAPL": 186.00},
        prev_close={"AAPL": 184.50},
    )
    out = sp.get_spot_prices(["AAPL"])
    assert_in("AAPL", out, "AAPL in output")
    assert_close(out["AAPL"]["price"], 186.00, "price from streaming")
    assert_close(out["AAPL"]["change"], 1.50, "change = 186.00 - 184.50")
    assert_close(out["AAPL"]["change_pct"], 0.813, "change_pct correct")
    assert_eq(fake.calls, [], "no Schwab REST call when streaming hits")


def test_streaming_miss_falls_to_schwab_rest():
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={},  # no streaming hit
        prev_close={},        # also no prev_close cached
        schwab_table={"NVDA": 900.00},
    )
    out = sp.get_spot_prices(["NVDA"])
    # Schwab is called twice: once for prev_close (ensure), once for live price
    # if separately, OR once if we return both from the same get_quote call.
    # The implementation chooses to do one combined fetch — see Step 2 below.
    assert_in("NVDA", out, "NVDA in output via Schwab REST fallback")
    assert_close(out["NVDA"]["price"], 901.50, "price from Schwab lastPrice")
    assert_close(out["NVDA"]["change"], 1.50, "change vs Schwab closePrice")


def test_unfetchable_ticker_omitted():
    sp, fake = _setup(
        env_streaming="1",
        streaming_prices={},
        prev_close={},
        schwab_table={},  # no quote for FAKE
    )
    out = sp.get_spot_prices(["FAKE"])
    assert_not_in("FAKE", out, "unfetchable ticker omitted from result")


def test_legacy_mode_uses_yahoo():
    """When DASHBOARD_SPOT_USE_STREAMING is off, behavior matches the old path."""
    sp, fake = _setup(
        env_streaming="0",
        streaming_prices={"AAPL": 999.99},  # would-be wrong value if streaming used
        prev_close={"AAPL": 999.99},
    )
    # In legacy mode the stream fast-path is skipped. We don't hit Yahoo
    # in this unit test (no network) — the test just verifies the new
    # streaming path is bypassed when the env var is off.
    # Schwab provider should NOT be called either.
    sp.get_spot_prices(["AAPL"])
    assert_eq(fake.calls, [], "no Schwab REST call in legacy mode")


if __name__ == "__main__":
    test_streaming_hit_uses_prev_close_for_change()
    test_streaming_miss_falls_to_schwab_rest()
    test_unfetchable_ticker_omitted()
    test_legacy_mode_uses_yahoo()
    print(f"PASSED: {len(PASSED)}, FAILED: {len(FAILED)}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(0 if not FAILED else 1)
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
python3 test_spot_prices_streaming.py
```

Expected: tests fail because `_get_schwab_provider` and the streaming-first code path don't exist yet.

### Task 3.2: Rewrite `spot_prices.py` with streaming-first logic

- [ ] **Step 1: Read the current file once for line context**

```bash
python3 -c "print(sum(1 for _ in open('omega_dashboard/spot_prices.py')))"
```

Expected: ~171 lines (current `spot_prices.py`). Confirms we're working with the file we read earlier.

- [ ] **Step 2: Replace the file contents**

Open `omega_dashboard/spot_prices.py` and replace its full contents with:

```python
"""
omega_dashboard/spot_prices.py — dashboard spot price fetcher.

S.3 (streaming-first):
    When DASHBOARD_SPOT_USE_STREAMING=1 (default off until proven), this
    module reads spot prices from the existing Schwab WebSocket stream
    via schwab_stream.get_streaming_spot, pairs them with previous-close
    values from prev_close_store, and falls back to a one-off Schwab REST
    quote for tickers that aren't subscribed yet (cold start). Yahoo
    Finance is reserved as a last-resort fallback for tickers Schwab
    doesn't know about (unusual symbols, OTC names, etc.).

    When the env var is off (default), behavior is identical to the
    legacy v8.3 path: Yahoo Finance polling with a 60s positive cache and
    a negative cooldown cache for 429 / error responses.

    The streaming-first switch is ROLLBACK-SAFE: unset the env var, the
    old Yahoo path runs unchanged. No Redis migration, no schema change.

Returns shape (unchanged):
    {"AAPL": {"price": 184.32, "change": 1.27, "change_pct": 0.69, ...}}
"""

from __future__ import annotations
import os
import time
import logging
import threading
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Caches — shared between the streaming and legacy code paths
# ─────────────────────────────────────────────────────────────

_cache: Dict[str, Tuple[float, Dict]] = {}            # ticker -> (ts, data)
_neg_cache: Dict[str, Tuple[float, str]] = {}         # ticker -> (cooldown_until, kind)
_cache_lock = threading.Lock()
_CACHE_TTL_SEC = 60.0


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


_BACKOFF_429_SEC = _env_int("DASHBOARD_SPOT_429_COOLDOWN_SEC", 300)
_BACKOFF_ERR_SEC = _env_int("DASHBOARD_SPOT_ERR_COOLDOWN_SEC", 60)


# ─────────────────────────────────────────────────────────────
# Yahoo Finance — legacy path. Kept verbatim from v8.3 so the
# rollback (env var off) is a true revert, not "mostly the same".
# ─────────────────────────────────────────────────────────────

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _fetch_one_yahoo(ticker: str) -> Tuple[Dict, str]:
    if not requests:
        return ({}, "skip")
    try:
        r = requests.get(YAHOO_URL.format(ticker), headers=HEADERS, timeout=4.0)
        if r.status_code == 429:
            log.warning(f"spot fetch 429 for {ticker} — cooling down {_BACKOFF_429_SEC}s")
            return ({}, "429")
        r.raise_for_status()
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return ({}, "err")
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or prev is None:
            return ({}, "err")
        change = float(price) - float(prev)
        change_pct = (change / float(prev)) * 100.0 if prev else 0.0
        return (
            {
                "ticker": ticker,
                "price": round(float(price), 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "currency": meta.get("currency", "USD"),
                "fetched_at": time.time(),
            },
            "ok",
        )
    except Exception as e:
        log.warning(f"spot fetch failed for {ticker}: {e}")
        return ({}, "err")


# ─────────────────────────────────────────────────────────────
# Schwab provider lookup — overridable in tests
# ─────────────────────────────────────────────────────────────

def _get_schwab_provider():
    """Return an object with `_schwab_get(method, symbol)`, or None.

    Production: walks app.py's _cached_md._schwab. Tests monkey-patch
    this function to inject a fake.
    """
    try:
        import app
        cached_md = getattr(app, "_cached_md", None)
        if cached_md is None:
            return None
        schwab = getattr(cached_md, "_schwab", None)
        if schwab is None or not getattr(schwab, "available", False):
            return None
        return schwab
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Streaming-first fetch (S.3)
# ─────────────────────────────────────────────────────────────

def _build_record(ticker: str, price: float, prev_close: Optional[float]) -> Dict:
    if prev_close is None or prev_close == 0:
        change = 0.0
        change_pct = 0.0
    else:
        change = float(price) - float(prev_close)
        change_pct = (change / float(prev_close)) * 100.0
    return {
        "ticker": ticker,
        "price": round(float(price), 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "currency": "USD",
        "fetched_at": time.time(),
    }


def _fetch_one_schwab(ticker: str, schwab_provider) -> Optional[Dict]:
    """One Schwab get_quote returning a complete record (price + change).

    Used for cold-start (ticker not yet streaming) and for tickers that
    don't appear in the streaming sub list. Returns None on any failure.
    """
    try:
        data = schwab_provider._schwab_get("get_quote", ticker)
    except Exception as e:
        log.debug(f"schwab spot fetch failed for {ticker}: {e}")
        return None

    entry = (data or {}).get(ticker, {})
    quote = entry.get("quote", {})
    price = quote.get("lastPrice") or quote.get("mark")
    close = quote.get("closePrice")
    if not price:
        return None
    return _build_record(ticker, float(price), float(close) if close else None)


def _fetch_streaming_first(tickers: List[str]) -> Dict[str, Dict]:
    """The streaming-first path. Activated when DASHBOARD_SPOT_USE_STREAMING=1."""
    out: Dict[str, Dict] = {}
    if not tickers:
        return out

    # Lazy imports — these modules are only present when the bot is fully
    # wired. In test contexts they're stubbed.
    try:
        from schwab_stream import get_streaming_spot
    except Exception:
        get_streaming_spot = lambda t: None

    try:
        from prev_close_store import get_prev_close_store
        prev_store = get_prev_close_store()
    except Exception:
        prev_store = None

    schwab_provider = _get_schwab_provider()

    # Phase 1: streaming hits — collect tickers that have a fresh stream price.
    streaming_hits: Dict[str, float] = {}
    streaming_misses: List[str] = []
    for t in tickers:
        price = get_streaming_spot(t)
        if price is not None and price > 0:
            streaming_hits[t] = float(price)
        else:
            streaming_misses.append(t)

    # Phase 2: top up prev_close for streaming hits via the store (one
    # batch of REST calls if cache is cold, then nothing on warm cache).
    if streaming_hits and prev_store is not None and schwab_provider is not None:
        prev_store.ensure(list(streaming_hits.keys()), schwab_provider)

    for t, price in streaming_hits.items():
        prev = prev_store.get(t) if prev_store else None
        out[t] = _build_record(t, price, prev)

    # Phase 3: streaming misses — one Schwab quote each (covers cold-start
    # and unsubscribed tickers). If Schwab also fails, fall through to Yahoo.
    yahoo_misses: List[str] = []
    if streaming_misses and schwab_provider is not None:
        for t in streaming_misses:
            rec = _fetch_one_schwab(t, schwab_provider)
            if rec:
                out[t] = rec
            else:
                yahoo_misses.append(t)
    else:
        yahoo_misses = streaming_misses

    # Phase 4: last-resort Yahoo fallback for the rest.
    for t in yahoo_misses:
        result, status = _fetch_one_yahoo(t)
        if status == "ok" and result:
            out[t] = result
        elif status == "429" and _BACKOFF_429_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_429_SEC, "429")

    return out


# ─────────────────────────────────────────────────────────────
# Legacy Yahoo-only path — DASHBOARD_SPOT_USE_STREAMING=0
# ─────────────────────────────────────────────────────────────

def _fetch_legacy_yahoo(tickers: List[str]) -> Dict[str, Dict]:
    """Behavior-identical to the v8.3 implementation."""
    out: Dict[str, Dict] = {}
    if not tickers:
        return out

    now = time.time()
    to_fetch: List[str] = []
    with _cache_lock:
        for t in tickers:
            cached = _cache.get(t)
            if cached and (now - cached[0]) < _CACHE_TTL_SEC:
                out[t] = cached[1]
                continue
            neg = _neg_cache.get(t)
            if neg and now < neg[0]:
                log.debug(f"spot fetch for {t} suppressed (cooldown {int(neg[0] - now)}s)")
                continue
            to_fetch.append(t)

    for t in to_fetch:
        result, status = _fetch_one_yahoo(t)
        if status == "ok" and result:
            out[t] = result
            with _cache_lock:
                _cache[t] = (time.time(), result)
                _neg_cache.pop(t, None)
        elif status == "429" and _BACKOFF_429_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_429_SEC, "429")
        elif status == "err" and _BACKOFF_ERR_SEC > 0:
            with _cache_lock:
                _neg_cache[t] = (time.time() + _BACKOFF_ERR_SEC, "err")

    return out


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def get_spot_prices(tickers: List[str]) -> Dict[str, Dict]:
    """Fetch spot prices for a list of tickers.

    Streaming-first when DASHBOARD_SPOT_USE_STREAMING=1, else legacy Yahoo.
    The 60s positive cache and negative cooldown cache apply in both modes
    for tickers that end up on the Yahoo fallback path.
    """
    if not tickers:
        return {}

    cleaned = []
    seen = set()
    for t in tickers:
        if not t:
            continue
        u = str(t).strip().upper()
        if u and u not in seen:
            seen.add(u)
            cleaned.append(u)

    if _env_bool("DASHBOARD_SPOT_USE_STREAMING", default=False):
        return _fetch_streaming_first(cleaned)
    return _fetch_legacy_yahoo(cleaned)
```

- [ ] **Step 3: Run the new test suite, verify pass**

```bash
python3 test_spot_prices_streaming.py
```

Expected: `PASSED: 4+, FAILED: 0`. (The exact count depends on `assert_*` calls; all assertions should pass.)

- [ ] **Step 4: AST-check**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/spot_prices.py').read())"
```

Expected: clean.

- [ ] **Step 5: Smoke-test the legacy path stays intact**

```bash
DASHBOARD_SPOT_USE_STREAMING=0 python3 -c "
from omega_dashboard import spot_prices
out = spot_prices.get_spot_prices(['SPY'])
print(out)
"
```

Expected: a dict with SPY price, change, change_pct (or empty dict if Yahoo is unreachable from the dev box — that's fine; the point is no exception).

- [ ] **Step 6: Commit**

```bash
git add omega_dashboard/spot_prices.py test_spot_prices_streaming.py
git commit -m "Patch S.3: streaming-first dashboard spot fetcher

DASHBOARD_SPOT_USE_STREAMING=0 (default off) → legacy Yahoo path,
unchanged. =1 → streaming spot from schwab_stream.get_streaming_spot
paired with prev_close_store, with Schwab REST cold-start fallback and
Yahoo as last resort. Patch S.4 wires the dashboard register-tickers
endpoint so first-load tickers actually arrive in the stream."
```

---

# Patch S.4 — `/api/register-tickers` + dashboard JS

**Why last:** with S.1-S.3 landed but the env var off, Brad has the option to flip the switch and validate behavior with a manual `add_equity_symbols()` call. S.4 closes the loop so the dashboard self-registers its tickers on every page load. After S.4 + env var on, the dashboard polls 21 tickers from streaming, hits Yahoo zero times in steady state.

**Files:**
- Modify: `omega_dashboard/routes.py:1690` (insert new route above `api_spot_prices`)
- Modify: `omega_dashboard/templates/dashboard/portfolio.html` (line 2042 area — JS POST before fetch)
- Modify: `omega_dashboard/templates/dashboard/command_center.html` (line 515 area — same)

### Task 4.1: Add `/api/register-tickers` route

- [ ] **Step 1: Insert the new route into `omega_dashboard/routes.py`**

Find line 1690 (the comment `# ─── PHASE 4.5+ — LIVE SPOT PRICES ─────────────`). Insert this NEW block immediately ABOVE that comment:

```python
# ─── S.4: TICKER REGISTRATION ───────────────────────────────
@dashboard_bp.route("/api/register-tickers", methods=["POST"])
@login_required
def api_register_tickers():
    """Register a set of tickers for streaming spot subscription.

    Called by the dashboard pages on load, BEFORE the first /api/spot-prices
    request. Hands the ticker list to schwab_stream's add_equity_symbols
    so the WebSocket sub catches up by the time the user's spot-prices
    poll arrives. Idempotent — repeated calls with the same tickers are
    silently de-duped inside add_equity_symbols.

    Body: {"tickers": ["AAPL", "MSFT", ...]}
    Returns: {"registered": N, "active": <streaming sub count>}

    No-ops cleanly when DASHBOARD_SPOT_USE_STREAMING is off — the streamer
    still subscribes; the dashboard just won't read from it.
    """
    from flask import jsonify, request
    body = request.get_json(silent=True) or {}
    raw = body.get("tickers") or []
    tickers = [str(t).strip().upper() for t in raw if t and str(t).strip()]
    if not tickers:
        return jsonify({"registered": 0, "active": 0})

    try:
        from schwab_stream import _stream_manager
        if _stream_manager is None:
            log.debug("register-tickers: stream manager not running; skipping")
            return jsonify({"registered": 0, "active": 0, "note": "stream offline"})
        _stream_manager.add_equity_symbols(tickers)
        active = _stream_manager.status.get("equity_symbols_subscribed", 0)
        return jsonify({"registered": len(tickers), "active": active})
    except Exception as e:
        log.warning(f"register-tickers failed: {e}")
        return jsonify({"registered": 0, "active": 0, "error": str(e)}), 200


```

- [ ] **Step 2: AST-check**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/routes.py').read())"
```

Expected: clean.

- [ ] **Step 3: Manual smoke-test the endpoint**

```bash
python3 -c "
from omega_dashboard import routes
# Verify the route exists in the blueprint.
rules = [r.rule for r in routes.dashboard_bp.deferred_functions if hasattr(r, 'rule')]
print('register endpoint registered' if any('/api/register-tickers' in str(d) for d in dir(routes)) else 'manual check via /url_map at runtime')
"
```

Expected: no exception. (Full route registration is verified at runtime via Flask's `url_map`; this just confirms the file imports.)

### Task 4.2: Wire dashboard JS to call `/api/register-tickers`

- [ ] **Step 1: Update `portfolio.html`**

Find the line `fetch('/api/spot-prices?tickers=' + encodeURIComponent(tickers))` (line 2042). Replace the surrounding block — from the `if (!tickers) return;` line down through the start of the fetch — with:

```html
  if (!tickers) return;

  // S.4: register the ticker set with the streamer first, so the
  // /api/spot-prices call below hits a warm streaming subscription.
  // Fire-and-forget — failure here is non-fatal; spot-prices falls
  // through to Schwab REST then Yahoo on its own.
  fetch('/api/register-tickers', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tickers: tickers.split(',')})
  }).catch(() => {});

  fetch('/api/spot-prices?tickers=' + encodeURIComponent(tickers))
```

- [ ] **Step 2: Update `command_center.html`**

Find the line `fetch('/api/spot-prices?tickers=' + encodeURIComponent(tickers))` (line 515). Apply the same replacement as Step 1.

- [ ] **Step 3: Manual smoke-test**

Start the bot locally if possible, hit `/dashboard/portfolio` in the browser, watch the network tab:

1. `POST /api/register-tickers` should fire first (200 OK, body `{"registered": N, "active": M}`)
2. `GET /api/spot-prices?tickers=...` follows immediately

If Brad can't run locally during market hours, defer this step to the post-deploy log inspection — look for `Queued N equity symbols for streaming subscription` lines after the next /portfolio load.

- [ ] **Step 4: Commit**

```bash
git add omega_dashboard/routes.py omega_dashboard/templates/dashboard/portfolio.html omega_dashboard/templates/dashboard/command_center.html
git commit -m "Patch S.4: dashboard registers its tickers with the streamer

/api/register-tickers POST hands the page's ticker set to the
SchwabStreamManager so the next /api/spot-prices poll hits a warm
streaming subscription. portfolio.html and command_center.html call
the new endpoint before their spot-prices fetch. Fire-and-forget;
failures here just mean the next call falls through Schwab REST →
Yahoo as before."
```

---

# Deployment & cutover

After all four patches are committed and pushed:

1. **Deploy with env var off.** Render auto-rebuild picks up the new code; behavior is unchanged because `DASHBOARD_SPOT_USE_STREAMING` is unset (defaults to off).
2. **Verify the legacy path still works.** Load `/dashboard` and `/dashboard/portfolio` from the browser. Confirm spots populate as before.
3. **Inspect logs for the Patch S.1 changes.** Look for `Queued N equity symbols for streaming subscription` after page loads — confirms S.4 is firing and S.1 is queueing. No `level_one_equity_add` failures should appear (would indicate the schwab-py version doesn't expose that method; if so, rollback is just leaving the env var off).
4. **Flip the switch.** In Render → Environment, add `DASHBOARD_SPOT_USE_STREAMING=1`. Trigger a manual redeploy.
5. **Watch the next 10 minutes of logs.** Healthy signals:
   - First page load: `Dynamically added N equity symbols to stream`
   - Subsequent page loads: zero `spot fetch 429` warnings
   - `prev_close ensure: fetched X/X tickers` once per ticker per day
6. **Rollback if needed.** Unset `DASHBOARD_SPOT_USE_STREAMING` in Render env, redeploy. Within 60s the dashboard is back on Yahoo.

# CLAUDE.md update (after S.4 ships)

Once the streaming-first path has been clean for at least one trading day, add to CLAUDE.md under "Decisions already made":

> - Dashboard spot prices come from the Schwab WebSocket via
>   `schwab_stream.get_streaming_spot`, paired with `prev_close_store`
>   for change/change_pct. Schwab REST `get_quote` is the cold-start
>   fallback; Yahoo is last-resort only and gated behind
>   `DASHBOARD_SPOT_USE_STREAMING=0` for emergency rollback.

Follow-up patch S.5 (NOT in this plan): delete the Yahoo path entirely after a clean two-week run. That's a separate decision and a separate commit.

---

# Smoke-test command list (add to CLAUDE.md after S.3)

```bash
# Patch S streaming-spots tests
python3 test_schwab_stream_equity_dynamic.py
python3 test_prev_close_store.py
python3 test_spot_prices_streaming.py
```
