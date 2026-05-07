"""
raw_inputs.py — The single fetch entry point for BotState.

PURPOSE
-------
Bundles the raw market data BotState needs into one immutable structure,
fetched through the existing canonical IO layer (DataRouter / `_cached_md`).

This file does NOT replace DataRouter. DataRouter is already the unified
Schwab→MarketData fallback layer with caching. This file is a THIN wrapper
that calls DataRouter for the typical fetch bundle (spot, chain, bars,
quote) and returns the result as a dataclass.

WHY A WRAPPER, NOT A REWRITE
----------------------------
The data_inventory_FULL.md report flagged 220 hits / 36 files for
"MarketData fallback" — which sounded like fragmentation, but on inspection
turned out to be 5 main files calling `_cached_md.X` (the canonical
pattern) plus ~21 peripheral files (dashboards, scheduled tasks, backtest
infrastructure) that bypass DataRouter for read-only auxiliary data.

The trading-critical fetch path is already unified. This wrapper just
packages the typical bundle so engines and BotState don't each have to
make 4 separate calls.

The peripheral DataRouter-bypass files (dashboard.py, em_reconciler.py,
vix_term_structure.py, etc.) can be migrated to DataRouter over time as
cleanup, but they're NOT blocking the engine rebuild because they're
not on the decision path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# DESIGN CHOICES (documented for future readers)
# ───────────────────────────────────────────────────────────────────────
#
# 1. EXPLICIT EXPIRATION (Option A from the design discussion).
#    Caller passes the expiration they want. DataRouter caches by
#    (ticker, expiration) so multiple engines needing different
#    expirations in the same cycle cost only one fetch each.
#    Earlier draft considered lazy-load on first access but that adds
#    complexity in error paths for no real efficiency gain.
#
# 2. 504-BAR LOOKBACK ALWAYS.
#    Real usage in the repo ranges from 25 to 504 days. Potter Box uses
#    504; most engines use 65; vol regime uses 252. Fetching 504 always
#    is one Schwab call per ticker per cycle (cached after first), and
#    every engine can slice down to whatever lookback it wants. Trying
#    to be clever with tiered fetches is more complexity for marginal
#    savings.
#
# 3. NO IV SURFACE BUILD HERE.
#    UnifiedIVSurface is built downstream when needed. RawInputs carries
#    an optional placeholder; if the caller has already built one, it
#    gets passed through. Otherwise None and the canonical IV computation
#    handles the fallback.
#
# 4. ALL FETCHES THROUGH DATAROUTER.
#    No direct schwab_adapter or marketdata calls. If a future ticker
#    needs a fetch DataRouter doesn't expose, add the method to
#    DataRouter first, then wrap here. Keeps single canonical IO layer.
# ───────────────────────────────────────────────────────────────────────


# Constants — change here, propagates everywhere.
DEFAULT_BARS_DAYS = 504              # covers longest lookback (Potter Box)
DEFAULT_CHAIN_STRIKE_LIMIT = None    # None = full chain (Patch 7+ standard)


@dataclass(frozen=True)
class RawInputs:
    """
    Raw market data bundle for one ticker at one moment.

    Built by `fetch_raw_inputs(ticker, expiration, ...)`. Frozen — once
    constructed, fields are read-only. Multiple instances may exist for
    the same ticker (e.g. one per expiration the bot wants to reason about).

    Fields:
        ticker:         e.g. "SPY"
        spot:           current price; from streaming if available, else from quote
        chain:          RAW chain response from DataRouter — dict-of-arrays format
                        (MarketData shape, e.g. {"strike": [540, 545, ...], ...}).
                        Kept in raw form because the canonical row-converter
                        `engine_bridge.build_option_rows()` consumes this shape
                        directly. Engines that need row-style dicts can call
                        the helper `chain_rows()` below.
        expiration:     ISO date string the chain is for, e.g. "2026-05-09"
        quote:          full quote dict (volume, avgVolume20d, bid, ask, etc.)
        bars:           list of OHLCV bar dicts, oldest-first, ~504 trading days
        iv_surface:     UnifiedIVSurface if pre-built by caller, else None
        fetched_at_utc: timestamp the fetch completed
        fetch_errors:   list of (operation, exception_str) tuples for any
                        non-fatal failures during the bundle. Empty if clean.
    """
    ticker: str
    spot: float
    chain: dict                       # dict-of-arrays — RAW from DataRouter
    expiration: str
    quote: dict
    bars: list[dict]
    iv_surface: Optional[dict]
    fetched_at_utc: datetime
    fetch_errors: tuple = ()          # tuple of (operation_name, error_str) pairs

    @property
    def is_clean(self) -> bool:
        """True if every fetch in the bundle succeeded."""
        return len(self.fetch_errors) == 0

    @property
    def chain_rows(self) -> list[dict]:
        """
        Helper: chain pivoted to list-of-dicts shape, for engines that
        prefer row-style access (`.get("strike")` per row).

        Most engines should NOT use this — use `build_option_rows(self.chain, spot, dte)`
        from engine_bridge.py instead. That's the canonical converter to OptionRow
        objects, which is what ExposureEngine and friends consume.

        chain_rows() exists only for legacy display code that iterates rows for
        formatting purposes. Computed on access (not cached) — if you call it
        in a hot loop, cache the result yourself.
        """
        return _pivot_chain_to_rows(self.chain)


# ───────────────────────────────────────────────────────────────────────
# THE BUNDLER
# ───────────────────────────────────────────────────────────────────────

def fetch_raw_inputs(
    ticker: str,
    expiration: str,
    *,
    data_router,
    bars_days: int = DEFAULT_BARS_DAYS,
    iv_surface: Optional[dict] = None,
) -> RawInputs:
    """
    Fetch the raw market-data bundle for one ticker+expiration.

    Args:
        ticker:       e.g. "SPY"
        expiration:   ISO date string, e.g. "2026-05-09". Caller's responsibility
                      to determine the right expiration for their engine
                      (front-week for thesis, 30-45 DTE for income, etc.).
        data_router:  DataRouter / `_cached_md` instance. Required.
                      Pass None to raise immediately rather than fail silently.
        bars_days:    OHLCV lookback. Defaults to 504 (covers longest engine
                      lookback, Potter Box). Override only if you have a reason.
        iv_surface:   Optional pre-built UnifiedIVSurface. Pass through if
                      caller already built one for this cycle (saves rebuild).
                      Otherwise None and downstream computation handles fallback.

    Returns:
        RawInputs frozen dataclass. Check `.is_clean` for fetch health.

    Raises:
        ValueError: data_router is None, or ticker/expiration is empty.

    Notes:
        - Each fetch is wrapped in try/except. A single fetch failure does
          NOT abort the bundle — the field gets a safe default and the
          error is recorded in fetch_errors. Engines downstream can decide
          whether to proceed with partial data or skip.
        - DataRouter caches results, so repeat calls for the same
          (ticker, expiration) within the cache TTL window are free.
    """
    if data_router is None:
        raise ValueError("data_router is required (pass _cached_md or equivalent)")
    if not ticker:
        raise ValueError("ticker is required")
    if not expiration:
        raise ValueError("expiration is required (ISO date string)")

    errors: list[tuple[str, str]] = []

    # ─── Spot ─────────────────────────────────────────────────────────
    # DataRouter.get_spot already checks streaming first (sub-second),
    # falls back to Schwab-then-MarketData on miss.
    try:
        spot = float(data_router.get_spot(ticker))
        if spot <= 0:
            errors.append(("get_spot", f"non-positive spot: {spot}"))
            spot = 0.0
    except Exception as e:
        errors.append(("get_spot", str(e)[:200]))
        spot = 0.0

    # ─── Chain ────────────────────────────────────────────────────────
    # DataRouter returns chain in MarketData dict-of-arrays format (after
    # the schwab_adapter's `_schwab_chain_to_md_format()` has already
    # normalized any Schwab-shape responses). We keep it in that shape —
    # do NOT pivot to list-of-dicts here. Reason: the canonical row
    # converter `engine_bridge.build_option_rows()` consumes dict-of-arrays
    # directly. Pivoting would force every consumer to un-pivot, or worse,
    # introduce a parallel converter — exactly the fragmentation the
    # rebuild is trying to remove.
    try:
        chain = data_router.get_chain(
            ticker,
            expiration,
            strike_limit=DEFAULT_CHAIN_STRIKE_LIMIT,
        )
        # Defensive: error envelope from MarketData ({"s": "error", ...})
        # is treated as fetch failure, not silently passed through.
        if isinstance(chain, dict) and chain.get("s") == "error":
            errors.append(("get_chain", f"provider error: {chain.get('errmsg', 'unknown')[:100]}"))
            chain = {}
        elif chain is None:
            chain = {}
    except Exception as e:
        errors.append(("get_chain", str(e)[:200]))
        chain = {}

    # ─── Quote ────────────────────────────────────────────────────────
    try:
        quote = data_router.get_stock_quote(ticker) or {}
    except Exception as e:
        errors.append(("get_stock_quote", str(e)[:200]))
        quote = {}

    # ─── OHLC bars ────────────────────────────────────────────────────
    try:
        bars = data_router.get_ohlc_bars(ticker, days=bars_days) or []
    except Exception as e:
        errors.append(("get_ohlc_bars", str(e)[:200]))
        bars = []

    # ─── Build the immutable bundle ───────────────────────────────────
    return RawInputs(
        ticker=ticker,
        spot=spot,
        chain=chain,
        expiration=expiration,
        quote=quote,
        bars=bars,
        iv_surface=iv_surface,
        fetched_at_utc=datetime.now(timezone.utc),
        fetch_errors=tuple(errors),
    )


# ───────────────────────────────────────────────────────────────────────
# CHAIN ROW PIVOT HELPER (for legacy display code only)
# ───────────────────────────────────────────────────────────────────────

def _pivot_chain_to_rows(chain: dict) -> list[dict]:
    """
    Pivot dict-of-arrays chain to list-of-dicts. ONLY for legacy display code.

    Most consumers should NOT use this. Use `engine_bridge.build_option_rows()`
    instead — that's the canonical converter to OptionRow objects, which is
    what ExposureEngine and the rest of the dealer-Greek pipeline consume.

    This helper exists for code that formats option rows for display and
    iterates row-by-row. Pure function, returns a fresh list each call.
    """
    if not chain or not isinstance(chain, dict):
        return []

    # Standard MarketData chain fields. Add others as needed.
    fields = (
        "strike", "side", "expiration", "underlying", "underlyingPrice",
        "bid", "ask", "mid", "last", "mark", "volume", "openInterest",
        "delta", "gamma", "vega", "theta", "rho", "iv", "intrinsicValue",
        "extrinsicValue", "updated", "dte", "totalVolume", "optionSymbol",
        "oiChange",
    )

    # Find the array length using whichever field is present.
    length = 0
    for f in fields:
        if f in chain and isinstance(chain[f], (list, tuple)):
            length = len(chain[f])
            break

    if length == 0:
        return []

    rows: list[dict] = []
    for i in range(length):
        row: dict = {}
        for f in fields:
            arr = chain.get(f)
            if isinstance(arr, (list, tuple)) and i < len(arr):
                row[f] = arr[i]
        if row:
            rows.append(row)
    return rows


# ───────────────────────────────────────────────────────────────────────
# AST sanity check at module import / direct run
# ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"raw_inputs.py module loaded.")
    print(f"  DEFAULT_BARS_DAYS = {DEFAULT_BARS_DAYS}")
    print(f"  DEFAULT_CHAIN_STRIKE_LIMIT = {DEFAULT_CHAIN_STRIKE_LIMIT}")
    print(f"  RawInputs fields: {[f.name for f in __import__('dataclasses').fields(RawInputs)]}")
    print(f"  fetch_raw_inputs signature: ticker, expiration, *, data_router, bars_days={DEFAULT_BARS_DAYS}, iv_surface=None")
