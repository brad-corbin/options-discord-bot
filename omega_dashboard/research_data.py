"""
omega_dashboard/research_data.py — Data layer for the Research page.

PURPOSE
-------
Computes BotState for the ticker universe and packages the results into a
template-ready dict for the Research page (the renamed Diagnostic tab).

DESIGN
------
Permissive: if BotState.build() fails for a ticker (Schwab fetch error,
malformed chain, etc.), the per-ticker entry is marked errored but the
overall page still renders. Other tickers' results are unaffected.

In-memory cache: results are cached per-ticker for 60 seconds. Browser
refreshes within that window pay zero Schwab cost. Cache is per-process —
fine for single-Render-instance deploys; needs Redis if scaled out.

PROGRESS METRICS
----------------
The page header shows what % of fields are lit across the universe.
This visualizes the rebuild's progress: as canonical functions land,
the % goes up.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS = 60

# Default ticker universe for the Research page.
# Mirrors EM_TICKERS or the silent-thesis universe — but caller can override.
DEFAULT_TICKERS = [
    "SPY", "QQQ", "DIA", "IWM",
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
    "TSLA", "AMD", "AVGO", "ORCL", "CRM",
    "JPM", "BA", "CAT", "GS", "UNH",
    "LLY", "MRNA",
    "GLD", "TLT",
    "XLE", "XLF", "XLV", "SOXX",
    "ARM", "COIN", "MSTR", "PLTR", "SOFI", "SMCI", "NFLX",
]


@dataclass
class TickerSnapshot:
    """One row in the Research grid, ready for template rendering."""
    ticker: str
    spot: Optional[float]
    gamma_flip: Optional[float]
    distance_from_flip_pct: Optional[float]
    flip_location: str
    # IV state (Patch 11.3.2 — canonical_iv_state)
    atm_iv: Optional[float]
    iv_skew_pp: Optional[float]
    iv30: Optional[float]
    # Dealer Greek aggregates (Patch 11.4 — canonical_exposures)
    gex: Optional[float]
    dex: Optional[float]
    vanna: Optional[float]
    charm: Optional[float]
    gex_sign: str
    # Progress
    fields_lit: int
    fields_total: int
    canonical_status: dict
    chain_clean: bool
    fetch_errors: list
    error: Optional[str] = None    # set if build_from_raw failed entirely


@dataclass
class ResearchData:
    """Top-level payload for the Research template."""
    fetched_at_utc: datetime
    tickers_total: int
    tickers_with_data: int
    tickers_errored: int
    fields_lit_avg: float                  # avg fields_lit across all tickers
    fields_total: int
    canonical_status_summary: dict          # {canonical_name: count_lit_across_universe}
    snapshots: list                         # list of TickerSnapshot
    available: bool                         # False if data layer unavailable
    error: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────
# Cache
# ───────────────────────────────────────────────────────────────────────

# Keyed by (ticker, expiration) tuple — a request for the same ticker at a
# different expiration must NOT be served from a cached snapshot of the
# wrong chain. This is rare in normal page use (default expiration is
# next-Friday for everyone) but easy to get wrong if added later.
_CACHE: dict = {}    # (ticker, expiration) -> (timestamp, snapshot)


def _cache_get(ticker: str, expiration: str):
    entry = _CACHE.get((ticker, expiration))
    if not entry:
        return None
    ts, snap = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        return None
    return snap


def _cache_put(ticker: str, expiration: str, snap):
    _CACHE[(ticker, expiration)] = (time.time(), snap)


# ───────────────────────────────────────────────────────────────────────
# Per-ticker snapshot
# ───────────────────────────────────────────────────────────────────────

def build_ticker_snapshot(ticker: str, expiration: str, *, data_router) -> TickerSnapshot:
    """Build a single ticker's research snapshot.

    Returns a TickerSnapshot regardless of success — errors are captured
    inside the snapshot, never raised. Caller renders all snapshots
    uniformly.
    """
    cached = _cache_get(ticker, expiration)
    if cached is not None:
        return cached

    try:
        from bot_state import BotState
        state = BotState.build(ticker, expiration, data_router=data_router)
        snap = TickerSnapshot(
            ticker=ticker,
            spot=state.spot,
            gamma_flip=state.gamma_flip,
            distance_from_flip_pct=state.distance_from_flip_pct,
            flip_location=state.flip_location,
            atm_iv=state.atm_iv,
            iv_skew_pp=state.iv_skew_pp,
            iv30=state.iv30,
            gex=state.gex,
            dex=state.dex,
            vanna=state.vanna,
            charm=state.charm,
            gex_sign=state.gex_sign,
            fields_lit=state.fields_lit,
            fields_total=state.fields_total,
            canonical_status=dict(state.canonical_status),
            chain_clean=state.chain_clean,
            fetch_errors=list(state.fetch_errors),
            error=None,
        )
    except Exception as e:
        log.warning(f"build_ticker_snapshot {ticker}: {type(e).__name__}: {e}")
        snap = TickerSnapshot(
            ticker=ticker,
            spot=None,
            gamma_flip=None,
            distance_from_flip_pct=None,
            flip_location="unknown",
            atm_iv=None,
            iv_skew_pp=None,
            iv30=None,
            gex=None,
            dex=None,
            vanna=None,
            charm=None,
            gex_sign="unknown",
            fields_lit=0,
            fields_total=0,
            canonical_status={},
            chain_clean=False,
            fetch_errors=[],
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )

    _cache_put(ticker, expiration, snap)
    return snap


# ───────────────────────────────────────────────────────────────────────
# Universe-level
# ───────────────────────────────────────────────────────────────────────

def research_data(
    tickers: Optional[list] = None,
    expiration: Optional[str] = None,
    *,
    data_router=None,
) -> ResearchData:
    """Build the full Research page payload.

    Args:
        tickers:      list of tickers to include; defaults to DEFAULT_TICKERS
        expiration:   chain expiration to use for all tickers; if None,
                      uses the next-Friday expiration as a safe default
        data_router:  required for live data. If None, returns an empty
                      payload with available=False (page still renders).

    Returns:
        ResearchData ready for the template.
    """
    if tickers is None:
        tickers = list(DEFAULT_TICKERS)
    if expiration is None:
        expiration = _default_expiration()

    if data_router is None:
        return ResearchData(
            fetched_at_utc=datetime.now(timezone.utc),
            tickers_total=len(tickers),
            tickers_with_data=0,
            tickers_errored=0,
            fields_lit_avg=0.0,
            fields_total=0,
            canonical_status_summary={},
            snapshots=[],
            available=False,
            error="data_router not configured (Research page needs DataRouter)",
        )

    snapshots = []
    for t in tickers:
        snap = build_ticker_snapshot(t, expiration, data_router=data_router)
        snapshots.append(snap)

    # Aggregate metrics
    with_data = sum(1 for s in snapshots if s.error is None and s.spot)
    errored = sum(1 for s in snapshots if s.error is not None)
    fields_lit_avg = (
        sum(s.fields_lit for s in snapshots if s.error is None) / max(with_data, 1)
        if with_data > 0 else 0.0
    )
    fields_total = next((s.fields_total for s in snapshots if s.fields_total > 0), 0)

    # canonical_status_summary: count how many tickers have each canonical 'live'
    status_summary: dict = {}
    for s in snapshots:
        for cname, cstatus in s.canonical_status.items():
            if cname not in status_summary:
                status_summary[cname] = {"live": 0, "stub": 0, "error": 0}
            if cstatus == "live":
                status_summary[cname]["live"] += 1
            elif cstatus.startswith("stub"):
                status_summary[cname]["stub"] += 1
            else:
                status_summary[cname]["error"] += 1

    return ResearchData(
        fetched_at_utc=datetime.now(timezone.utc),
        tickers_total=len(tickers),
        tickers_with_data=with_data,
        tickers_errored=errored,
        fields_lit_avg=fields_lit_avg,
        fields_total=fields_total,
        canonical_status_summary=status_summary,
        snapshots=snapshots,
        available=True,
        error=None,
    )


def _default_expiration() -> str:
    """Next Friday's date, ISO format. Reasonable default for the Research page."""
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday == 0:
        days_to_friday = 7   # next Friday, not today
    return (today + timedelta(days=days_to_friday)).isoformat()


# ───────────────────────────────────────────────────────────────────────
# Direct-run sanity
# ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Sanity: with no data_router, returns valid empty payload
    payload = research_data()
    print(f"Research data (no data_router):")
    print(f"  available: {payload.available}")
    print(f"  error: {payload.error}")
    print(f"  tickers_total: {payload.tickers_total}")
    print(f"  default_expiration: {_default_expiration()}")
