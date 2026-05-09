"""Alert tracker daemon — G.7.

# v11.7 (Patch G.7): daemon thread that samples structure marks + underlying
# spot for active alerts on a variable cadence and writes alert_price_track
# rows. Piggybacks on existing OptionQuoteStore + schwab_stream streaming
# spot — zero new Schwab REST calls.

Cadence schedule (from SAMPLING_CADENCE in alert_recorder):
  0-1h:    sample every 60 seconds
  1-4h:    sample every 5 minutes
  4h-1d:   sample every 15 minutes
  1d-7d:   sample every 30 minutes
  7d+:     sample every 60 minutes

Gated by:
  RECORDER_ENABLED=true         (master gate, default off)
  RECORDER_TRACKER_ENABLED=true (tracker-specific gate, default off)

The daemon spawns unconditionally at bot startup; the inner loop checks both
gates each pass and no-ops if either is off. Flipping the env var on and
redeploying starts sampling within one loop interval (~15s).

Structure mark fetching:
  - long_call / long_put: read OptionQuoteStore by OCC symbol built from
    suggested_structure fields (ticker, expiry, strike, side).
    Uses build_occ_symbol from schwab_stream.
  - bull_put / bear_call (credit spreads): read both legs from store,
    compute net mark = short_mid - long_mid.
  - Returns None on cache miss — daemon writes the row with null
    structure_mark rather than making a new HTTP call. The V1 contract
    is: record what the cache has, never trigger new requests.
"""
import logging
import os
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Daemon state
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# Loop interval: sleep between full passes (seconds). Shorter than the
# finest-grained cadence bucket — the per-alert _should_sample check
# decides whether to actually emit a row for each alert.
_LOOP_INTERVAL_SEC = 15

# Maximum elapsed time we track any alert (default 7 days). Configurable
# for testing.
_DEFAULT_HORIZON_SEC = 7 * 24 * 60 * 60

# Per-alert last-sample tracking: alert_id -> elapsed_seconds at last sample.
# Lives in module state; reset on daemon restart. That's acceptable for V1
# (a restart causes one duplicate sample at most).
_last_sample_elapsed: dict = {}
_last_sample_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────
# Gate helpers
# ─────────────────────────────────────────────────────────────

def _master_enabled() -> bool:
    return os.getenv("RECORDER_ENABLED", "false").lower() in ("1", "true", "yes")


def _tracker_enabled() -> bool:
    """Both master gate AND tracker-specific gate must be on."""
    if not _master_enabled():
        return False
    return os.getenv("RECORDER_TRACKER_ENABLED", "false").lower() in ("1", "true", "yes")


# ─────────────────────────────────────────────────────────────
# Cadence / sampling logic
# ─────────────────────────────────────────────────────────────

def _cadence_for(elapsed_seconds: int) -> int:
    """Return the sampling cadence (seconds) for the given elapsed time.

    Matches SAMPLING_CADENCE from alert_recorder.py:
      0-1h:    60s
      1-4h:    5min
      4h-1d:   15min
      1d-7d:   30min
      7d+:     60min
    """
    # Import here to avoid circular dependency at module level
    try:
        from alert_recorder import SAMPLING_CADENCE
        cadence = SAMPLING_CADENCE[-1][1]
        for lower, bucket_cadence in SAMPLING_CADENCE:
            if elapsed_seconds >= lower:
                cadence = bucket_cadence
        return cadence
    except Exception:
        # Fallback: hardcoded table matching the spec
        if elapsed_seconds < 60 * 60:
            return 60
        if elapsed_seconds < 4 * 60 * 60:
            return 5 * 60
        if elapsed_seconds < 24 * 60 * 60:
            return 15 * 60
        if elapsed_seconds < 7 * 24 * 60 * 60:
            return 30 * 60
        return 60 * 60


def _should_sample(
    elapsed_seconds: int,
    last_sample_elapsed: Optional[int],
    horizon_seconds: int = _DEFAULT_HORIZON_SEC,
) -> bool:
    """True if the daemon should emit a track sample for this alert now.

    Rules:
      1. If elapsed > horizon: always False (tracking window expired).
      2. If no prior sample: always True (first sample).
      3. If time since last sample >= cadence for current elapsed: True.
    """
    if elapsed_seconds > horizon_seconds:
        return False
    if last_sample_elapsed is None:
        return True
    cadence = _cadence_for(elapsed_seconds)
    time_since_last = elapsed_seconds - last_sample_elapsed
    return time_since_last >= cadence


def _last_sample_for(alert_id: str) -> Optional[int]:
    """Return the elapsed_seconds at the last sample for this alert, or None."""
    with _last_sample_lock:
        return _last_sample_elapsed.get(alert_id)


def _record_sample_elapsed(alert_id: str, elapsed_seconds: int) -> None:
    """Update the in-memory last-sample tracker."""
    with _last_sample_lock:
        _last_sample_elapsed[alert_id] = elapsed_seconds


# ─────────────────────────────────────────────────────────────
# Market data reads (zero new HTTP calls)
# ─────────────────────────────────────────────────────────────

def _market_state_now() -> str:
    """Return the current market state string, or 'unknown' if unavailable.

    schwab_stream.get_market_state() is not implemented — returns 'unknown'
    as the V1 fallback. A future patch can wire the real market-hours check.
    """
    try:
        from schwab_stream import get_market_state  # type: ignore[attr-defined]
        return get_market_state() or "unknown"
    except (ImportError, AttributeError):
        return "unknown"


def _fetch_underlying_price(ticker: str) -> Optional[float]:
    """Read current spot from the streaming spot cache. No HTTP call."""
    try:
        from schwab_stream import get_streaming_spot
        return get_streaming_spot(ticker)
    except Exception as e:
        log.debug(f"tracker: _fetch_underlying_price({ticker}) failed: {e}")
        return None


def _build_occ_symbol(structure: dict, ticker: str) -> Optional[str]:
    """Build OCC symbol from a suggested_structure dict.

    Expected fields: expiry (YYYY-MM-DD), strike (float), and type
    (long_call/long_put or bull_put/bear_call) or explicit 'side' field.

    Returns None if fields are missing or malformed.
    """
    try:
        from schwab_stream import build_occ_symbol
        expiry = structure.get("expiry", "")
        strike = structure.get("strike")
        # Infer side from structure type if not explicit
        side = structure.get("side")
        if not side:
            stype = (structure.get("type") or "").lower()
            if "call" in stype:
                side = "call"
            elif "put" in stype:
                side = "put"
        if not expiry or strike is None or not side:
            return None
        return build_occ_symbol(ticker, expiry, side, float(strike))
    except Exception as e:
        log.debug(f"tracker: _build_occ_symbol failed: {e}")
        return None


def _fetch_structure_mark(
    structure: dict,
    ticker: str,
) -> Optional[float]:
    """Read current structure mark from OptionQuoteStore. No HTTP call.

    Handles:
      - long_call / long_put: single OCC symbol, returns mid.
      - bull_put / bear_call: two-leg spread. Reads short_occ and long_occ
        from the store; returns short_mid - long_mid. Structure must have
        'short_strike' and 'long_strike' fields plus 'expiry'.
      - On any cache miss: returns None (V1 contract — log but don't block).

    Symbol format: OCC standard (e.g. 'SPY   260515C00590000')
    built via schwab_stream.build_occ_symbol.
    """
    try:
        from schwab_stream import get_option_store
        store = get_option_store()
    except Exception as e:
        log.debug(f"tracker: get_option_store unavailable: {e}")
        return None

    stype = (structure.get("type") or "").lower()

    # Single-leg: long_call or long_put
    if stype in ("long_call", "long_put"):
        occ = _build_occ_symbol(structure, ticker)
        if occ is None:
            return None
        return store.get_live_premium(occ)

    # Credit spreads: bull_put or bear_call
    if stype in ("bull_put", "bear_call"):
        expiry = structure.get("expiry", "")
        short_strike = structure.get("short_strike")
        long_strike = structure.get("long_strike")
        if not expiry or short_strike is None or long_strike is None:
            return None
        try:
            from schwab_stream import build_occ_symbol
            if stype == "bull_put":
                spread_side = "put"
            else:
                spread_side = "call"
            short_occ = build_occ_symbol(ticker, expiry, spread_side, float(short_strike))
            long_occ = build_occ_symbol(ticker, expiry, spread_side, float(long_strike))
        except Exception as e:
            log.debug(f"tracker: spread OCC build failed: {e}")
            return None
        short_mid = store.get_live_premium(short_occ)
        long_mid = store.get_live_premium(long_occ)
        if short_mid is None or long_mid is None:
            return None
        return round(short_mid - long_mid, 4)

    # Unknown structure type — stub returns None
    log.debug(f"tracker: unknown structure type {stype!r}, mark = None")
    return None


# ─────────────────────────────────────────────────────────────
# PnL computation
# ─────────────────────────────────────────────────────────────

def _compute_pnl(
    structure: dict,
    current_mark: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    """Compute (pnl_abs, pnl_pct) for a structure given current mark.

    Long structures (long_call, long_put):
      pnl_abs = current_mark - entry_mark
      pnl_pct = pnl_abs / entry_mark * 100

    Credit spreads (bull_put, bear_call):
      Entry is a CREDIT received. PnL is positive when spread decays.
      pnl_abs  = credit - current_mark
      risk     = width - credit
      pnl_pct  = pnl_abs / risk * 100

    Returns (None, None) on missing data.
    """
    if current_mark is None:
        return None, None

    stype = (structure.get("type") or "").lower()

    if stype in ("long_call", "long_put"):
        entry = structure.get("entry_mark")
        if entry is None or entry == 0:
            return None, None
        pnl_abs = round(current_mark - entry, 4)
        pnl_pct = round(pnl_abs / entry * 100, 4)
        return pnl_abs, pnl_pct

    if stype in ("bull_put", "bear_call"):
        credit = structure.get("credit")
        width = structure.get("width")
        if credit is None or width is None or credit == 0:
            return None, None
        risk = width - credit
        if risk <= 0:
            return None, None
        pnl_abs = round(credit - current_mark, 4)
        pnl_pct = round(pnl_abs / risk * 100, 4)
        return pnl_abs, pnl_pct

    return None, None


# ─────────────────────────────────────────────────────────────
# Single-pass logic (testable without threads)
# ─────────────────────────────────────────────────────────────

def run_single_pass() -> None:
    """Run one tracking pass: iterate active alerts, sample eligible ones.

    Designed to be callable from tests (with mocked market-data functions)
    and from the daemon loop. Swallows all exceptions per-alert; never
    crashes the caller.
    """
    if not _tracker_enabled():
        return

    try:
        from alert_recorder import list_active_alerts, record_track_sample
        from alert_recorder import TRACKING_HORIZON_BY_ENGINE
    except Exception as e:
        log.warning(f"tracker: failed to import alert_recorder: {e}")
        return

    try:
        alerts = list_active_alerts()
    except Exception as e:
        log.warning(f"tracker: list_active_alerts failed: {e}")
        return

    for alert in alerts:
        alert_id = alert.get("alert_id")
        if not alert_id:
            continue
        try:
            _process_alert(alert, record_track_sample, TRACKING_HORIZON_BY_ENGINE)
        except Exception as e:
            log.warning(f"tracker: unhandled exception on alert {alert_id}: {e}")


def _process_alert(alert: dict, record_track_sample, horizon_by_engine: dict) -> None:
    """Process one alert: decide whether to sample, fetch marks, write row."""
    alert_id = alert["alert_id"]
    elapsed = alert.get("elapsed_seconds", 0)
    engine = alert.get("engine", "")
    ticker = alert.get("ticker", "")
    structure = alert.get("suggested_structure") or {}
    dte = alert.get("suggested_dte")

    # Determine horizon
    horizon = horizon_by_engine.get(engine)
    if horizon is None and dte:
        horizon = int(dte) * 24 * 60 * 60
    if horizon is None:
        horizon = _DEFAULT_HORIZON_SEC

    last_elapsed = _last_sample_for(alert_id)
    if not _should_sample(elapsed, last_elapsed, horizon_seconds=horizon):
        return

    # Fetch market data (no HTTP calls)
    underlying_price: Optional[float] = None
    structure_mark: Optional[float] = None
    market_state: str = "unknown"

    try:
        underlying_price = _fetch_underlying_price(ticker)
    except Exception as e:
        log.debug(f"tracker: underlying price fetch failed for {ticker}: {e}")

    try:
        structure_mark = _fetch_structure_mark(structure, ticker)
    except Exception as e:
        log.debug(f"tracker: structure mark fetch failed for {alert_id}: {e}")

    try:
        market_state = _market_state_now()
    except Exception as e:
        log.debug(f"tracker: market_state_now failed: {e}")

    pnl_abs, pnl_pct = _compute_pnl(structure, structure_mark)

    wrote = record_track_sample(
        alert_id=alert_id,
        elapsed_seconds=elapsed,
        underlying_price=underlying_price,
        structure_mark=structure_mark,
        structure_pnl_pct=pnl_pct,
        structure_pnl_abs=pnl_abs,
        market_state=market_state,
    )

    if wrote:
        _record_sample_elapsed(alert_id, elapsed)
        log.debug(
            f"tracker: sampled {alert_id} [{engine}] {ticker} "
            f"elapsed={elapsed}s underlying={underlying_price} "
            f"mark={structure_mark} pnl_pct={pnl_pct}"
        )


# ─────────────────────────────────────────────────────────────
# Daemon thread
# ─────────────────────────────────────────────────────────────

def _loop() -> None:
    """Inner daemon loop. Runs until stop() is called."""
    log.info("tracker daemon: started")
    while not _stop_event.is_set():
        try:
            run_single_pass()
        except Exception as e:
            # Outer try/except: daemon never crashes
            log.warning(f"tracker daemon: pass failed: {e}")
        _stop_event.wait(timeout=_LOOP_INTERVAL_SEC)
    log.info("tracker daemon: stopped")


def start() -> None:
    """Spawn the daemon thread. Idempotent — second call is a no-op if the
    thread is already alive. The inner loop checks _tracker_enabled() each
    pass and no-ops when the env var is off."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="alert-tracker-daemon", daemon=True)
    _thread.start()
    log.info("tracker daemon: thread spawned")


def stop() -> None:
    """Signal the daemon to stop and wait for it to exit (up to 5s)."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
    log.info("tracker daemon: stop signalled")
