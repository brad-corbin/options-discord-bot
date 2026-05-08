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

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var. Truthy: 1/true/yes/on (case-insensitive).
    Anything else (or unset) → default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    # Walls (Patch 11.5 — wired from canonical_exposures, no separate compute)
    call_wall: Optional[float]
    put_wall: Optional[float]
    gamma_wall: Optional[float]
    # Progress
    fields_lit: int
    fields_total: int
    canonical_status: dict
    chain_clean: bool
    fetch_errors: list
    error: Optional[str] = None    # set if build_from_raw failed entirely
    # Patch C: distinguishes "producer hasn't written this ticker yet"
    # (skeleton card, neutral styling) from "build raised an exception"
    # (red error card). Always False on the legacy inline-build path.
    warming_up: bool = False
    # Patch D.1: list of {intent, expiration, dte_days, dte_tag, call_wall,
    # put_wall, gamma_wall} dicts, one per intent in INTENTS_ORDER. Populated
    # by _research_data_from_redis when consumer reads all 4 intents per
    # ticker. Empty on the legacy inline-build path. Template renders the
    # click-to-expand WALLS disclosure when this list has entries.
    walls_by_intent: list = field(default_factory=list)


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
# wrong chain. Per-ticker canonical_expiration can legitimately yield
# different dates for different tickers or intents, so the (ticker,
# expiration) pair is the right granularity for the cache.
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


# ─────────────────────────────────────────────────────────────────────
# Patch C: Redis consumer helpers
#
# Reads pre-built BotState envelopes from Redis (written by Patch B's
# bot_state_producer). Validates schema versioning; falls through to a
# warming-up snapshot on any failure mode (missing key, JSON error,
# version mismatch). Gated by RESEARCH_USE_REDIS env var (see
# research_data() below).
# ─────────────────────────────────────────────────────────────────────

# Schema version compat — bump only on TRULY breaking changes.
# Additive producer bumps (new fields, new canonicals) leave this alone.
MIN_COMPATIBLE_PRODUCER_VERSION = 1

# Strict — Patch 9 dealer-side convention. Mismatch = warming-up.
EXPECTED_CONVENTION_VERSION = 2

# Producer's Redis key prefix (must match bot_state_producer.KEY_PREFIX).
KEY_PREFIX = "bot_state:"


def _warming_up_snapshot(ticker: str, reason: str = "warming up") -> "TickerSnapshot":
    """Construct a placeholder snapshot for tickers without (or with
    invalid) Redis data. The template renders these as skeleton cards."""
    return TickerSnapshot(
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
        call_wall=None,
        put_wall=None,
        gamma_wall=None,
        fields_lit=0,
        fields_total=0,
        canonical_status={},
        chain_clean=False,
        fetch_errors=[],
        error=reason,
        warming_up=True,
    )


def _validate_envelope_versions(envelope: dict, ticker: str) -> Optional[str]:
    """Returns None if the envelope's versions are acceptable, else an
    error string suitable for log + warming-up display.

    Forward compat: producer_version > our expected is accepted.
    Backward incompat: producer_version < MIN_COMPATIBLE is rejected.
    Convention strict: convention_version != EXPECTED_CONVENTION_VERSION
    is rejected (Patch 9 protection).
    """
    pv = envelope.get("producer_version")
    if pv is None:
        return f"{ticker}: envelope missing producer_version"
    if pv < MIN_COMPATIBLE_PRODUCER_VERSION:
        return (f"{ticker}: producer_version {pv} below "
                f"MIN_COMPATIBLE={MIN_COMPATIBLE_PRODUCER_VERSION}")

    cv = envelope.get("convention_version")
    if cv is None:
        return f"{ticker}: envelope missing convention_version"
    if cv != EXPECTED_CONVENTION_VERSION:
        return (f"{ticker}: convention_version {cv} != expected "
                f"{EXPECTED_CONVENTION_VERSION} (Patch 9 protection)")

    return None


def _snapshot_from_envelope(ticker: str, envelope: dict) -> "TickerSnapshot":
    """Convert a validated producer envelope to a TickerSnapshot.

    Caller must have already validated envelope versions — this function
    parses the `state` dict permissively, defaulting missing fields to
    None / "unknown" / 0 / {} / [] as appropriate.
    """
    state = envelope.get("state") or {}
    return TickerSnapshot(
        ticker=ticker,
        spot=state.get("spot"),
        gamma_flip=state.get("gamma_flip"),
        distance_from_flip_pct=state.get("distance_from_flip_pct"),
        flip_location=state.get("flip_location") or "unknown",
        atm_iv=state.get("atm_iv"),
        iv_skew_pp=state.get("iv_skew_pp"),
        iv30=state.get("iv30"),
        gex=state.get("gex"),
        dex=state.get("dex"),
        vanna=state.get("vanna"),
        charm=state.get("charm"),
        gex_sign=state.get("gex_sign") or "unknown",
        call_wall=state.get("call_wall"),
        put_wall=state.get("put_wall"),
        gamma_wall=state.get("gamma_wall"),
        fields_lit=state.get("fields_lit", 0),
        fields_total=state.get("fields_total", 0),
        canonical_status=state.get("canonical_status") or {},
        chain_clean=bool(state.get("chain_clean", False)),
        fetch_errors=state.get("fetch_errors") or [],
        error=None,
        warming_up=False,
    )


# ─────────────────────────────────────────────────────────────────────
# Patch D.1: Multi-DTE drilldown helpers
#
# The producer writes four envelopes per ticker, one per intent:
#   bot_state:{ticker}:front
#   bot_state:{ticker}:t7
#   bot_state:{ticker}:t30
#   bot_state:{ticker}:t60
# The Research page's WALLS section is a click-to-expand disclosure that
# shows all four intents' walls with DTE tags. _load_walls_for_all_intents
# reads all four per ticker and returns a list-of-dicts in INTENTS_ORDER.
# ─────────────────────────────────────────────────────────────────────

# Order matters — the front intent goes in the disclosure's collapsed
# summary; t7/t30/t60 expand below. Must match canonical_expiration's
# intent vocabulary.
INTENTS_ORDER = ("front", "t7", "t30", "t60")


def _compute_dte_days(expiration_iso: Optional[str], today=None) -> Optional[int]:
    """Days-to-expiration as an integer, or None on missing/malformed input.
    `today` is overridable for tests; production calls use UTC today."""
    if not expiration_iso:
        return None
    try:
        if today is None:
            today = datetime.now(timezone.utc).date()
        exp = datetime.fromisoformat(expiration_iso).date() if "T" in expiration_iso \
              else date.fromisoformat(expiration_iso)
        return (exp - today).days
    except Exception:
        return None


def _format_dte_tag(dte_days: Optional[int]) -> str:
    """Format a DTE integer as a display tag.

    Convention: "0DTE"/"1DTE" for the immediate expirations (matches
    options-trader vocabulary), "ND" for everything further out.
    None → "—" so missing-data rows render with a dash.
    """
    if dte_days is None:
        return "—"
    if dte_days <= 1:
        return f"{dte_days}DTE"
    return f"{dte_days}D"


def _load_walls_for_all_intents(ticker: str, redis_client) -> list:
    """Read all four intents' envelopes for a ticker. Returns a list of
    dicts in INTENTS_ORDER. Each dict has keys:
      - intent: str (e.g. "front")
      - expiration: ISO date string or None
      - dte_days: int or None
      - dte_tag: pre-formatted display string ("1DTE"/"8D"/"—")
      - call_wall, put_wall, gamma_wall: float or None

    Missing/malformed envelopes contribute an entry with all None values
    so the consumer always returns 4 entries (template renders "—" for
    missing rows). No exception ever propagates to the caller.

    `dte_tag` is computed here (not in the template) so the Jinja
    template stays simple — `{{ w.dte_tag }}` instead of inline
    string concatenation.
    """
    out = []
    for intent in INTENTS_ORDER:
        entry = {
            "intent": intent,
            "expiration": None,
            "dte_days": None,
            "dte_tag": "—",
            "call_wall": None,
            "put_wall": None,
            "gamma_wall": None,
        }
        if redis_client is None:
            out.append(entry)
            continue

        key = f"{KEY_PREFIX}{ticker}:{intent}"
        try:
            raw = redis_client.get(key)
        except Exception as e:
            log.debug(f"walls reader: redis GET {key} failed: {e}")
            out.append(entry)
            continue

        if raw is None:
            out.append(entry)
            continue

        try:
            envelope = json.loads(raw)
        except Exception as e:
            log.debug(f"walls reader: malformed envelope for {ticker}/{intent}: {e}")
            out.append(entry)
            continue
        if not isinstance(envelope, dict):
            log.debug(f"walls reader: envelope for {ticker}/{intent} is not a dict")
            out.append(entry)
            continue
        version_err = _validate_envelope_versions(envelope, ticker)
        if version_err is not None:
            log.debug(f"walls reader: {version_err}")
            out.append(entry)
            continue

        state = envelope.get("state") or {}
        entry["expiration"] = envelope.get("expiration")
        entry["dte_days"] = _compute_dte_days(entry["expiration"])
        entry["dte_tag"] = _format_dte_tag(entry["dte_days"])
        entry["call_wall"] = state.get("call_wall")
        entry["put_wall"] = state.get("put_wall")
        entry["gamma_wall"] = state.get("gamma_wall")
        out.append(entry)

    return out


def _load_snapshot_from_redis(
    ticker: str,
    intent: str,
    *,
    redis_client,
) -> "TickerSnapshot":
    """GET bot_state:{ticker}:{intent}, decode + validate, return a
    TickerSnapshot. Any failure mode (no Redis, missing key, malformed
    JSON, version mismatch) returns a warming-up snapshot with the
    failure reason set as snap.error.
    """
    if redis_client is None:
        return _warming_up_snapshot(ticker, reason="redis unavailable")

    key = f"{KEY_PREFIX}{ticker}:{intent}"
    try:
        raw = redis_client.get(key)
    except Exception as e:
        log.warning(f"research_data: redis GET {key} failed: {e}")
        return _warming_up_snapshot(ticker, reason="redis error")

    if raw is None:
        return _warming_up_snapshot(ticker, reason="missing key")

    try:
        envelope = json.loads(raw)
    except Exception as e:
        log.warning(f"research_data: malformed envelope for {ticker}: {e}")
        return _warming_up_snapshot(ticker, reason="malformed envelope")

    # Defensive: a JSON-valid but non-dict payload (e.g., "null", "42",
    # "[1,2,3]") would crash _validate_envelope_versions on .get(). The
    # consumer must never propagate exceptions; route to warming-up.
    if not isinstance(envelope, dict):
        log.warning(
            f"research_data: envelope for {ticker} is not a dict "
            f"(got {type(envelope).__name__})"
        )
        return _warming_up_snapshot(ticker, reason="envelope not a dict")

    err = _validate_envelope_versions(envelope, ticker)
    if err is not None:
        log.warning(f"research_data: {err}")
        return _warming_up_snapshot(ticker, reason=err)

    try:
        return _snapshot_from_envelope(ticker, envelope)
    except Exception as e:
        log.warning(f"research_data: snapshot construction failed for {ticker}: {e}")
        return _warming_up_snapshot(ticker, reason=f"parse error: {e}")


def _research_data_from_redis(
    tickers: list,
    intent: str,
    *,
    redis_client,
) -> "ResearchData":
    """Build the full Research page payload by reading each ticker's
    envelope from Redis. Tickers without populated keys render as
    warming-up. Returns ResearchData ready for the template."""
    if redis_client is None:
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
            error="redis client not available",
        )

    snapshots = []
    for t in tickers:
        snap = _load_snapshot_from_redis(t, intent, redis_client=redis_client)
        # Patch D.2: attach all-intents walls. For warming-up snapshots
        # leave walls_by_intent empty — the synthetic placeholder
        # shouldn't pretend to have data the producer never wrote.
        if not snap.warming_up:
            snap.walls_by_intent = _load_walls_for_all_intents(
                t, redis_client=redis_client,
            )
        snapshots.append(snap)

    # Aggregate metrics — count "with_data" as not-warming-up, not-errored.
    with_data = sum(1 for s in snapshots if not s.warming_up and s.error is None)
    # Note: on the Redis-consumer path errored is structurally always 0
    # (any failure sets warming_up=True). The metric carries non-zero
    # values from the legacy build_ticker_snapshot inline-build path.
    errored = sum(1 for s in snapshots if s.error is not None and not s.warming_up)
    fields_lit_avg = (
        sum(s.fields_lit for s in snapshots if not s.warming_up)
        / max(with_data, 1)
    )
    fields_total = next((s.fields_total for s in snapshots if s.fields_total > 0), 0)

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


# ───────────────────────────────────────────────────────────────────────
# Per-ticker snapshot
# ───────────────────────────────────────────────────────────────────────

def build_ticker_snapshot(ticker: str, intent: str = "front", *, data_router) -> TickerSnapshot:
    """Build a single ticker's research snapshot.

    Resolves the chain expiration per-ticker via canonical_expiration, then
    builds BotState. Defaults to intent='front' (first non-0-DTE chain) — the
    Research page's standard view. Returns a TickerSnapshot regardless of
    success; errors are captured inside the snapshot, never raised.
    """
    from canonical_expiration import canonical_expiration
    expiration = canonical_expiration(ticker, intent, data_router=data_router)
    if expiration is None:
        # No qualifying chain (e.g. t60 on a ticker with only short-dated chains).
        return TickerSnapshot(
            ticker=ticker,
            spot=None, gamma_flip=None, distance_from_flip_pct=None,
            flip_location="unknown",
            atm_iv=None, iv_skew_pp=None, iv30=None,
            gex=None, dex=None, vanna=None, charm=None,
            gex_sign="unknown",
            call_wall=None, put_wall=None, gamma_wall=None,
            fields_lit=0, fields_total=0,
            canonical_status={}, chain_clean=False, fetch_errors=[],
            error=f"no chain for intent={intent}",
        )

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
            call_wall=state.call_wall,
            put_wall=state.put_wall,
            gamma_wall=state.gamma_wall,
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
            call_wall=None,
            put_wall=None,
            gamma_wall=None,
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
    intent: str = "front",
    *,
    data_router=None,
    redis_client=None,
) -> ResearchData:
    """Build the full Research page payload.

    When RESEARCH_USE_REDIS=1, reads pre-built envelopes from Redis
    (Patch C consumer path). Otherwise builds inline via DataRouter +
    canonical_expiration + BotState.build (legacy v11 path).

    Args:
        tickers:      list of tickers to include; defaults to DEFAULT_TICKERS
        intent:       canonical_expiration intent for chain selection. Default
                      'front' = first non-0-DTE expiration per ticker. Other
                      valid values: 't7', 't30', 't60'.
        data_router:  required for legacy path. Ignored when RESEARCH_USE_REDIS=1.
        redis_client: required for consumer path. Ignored when env var is off.

    Returns:
        ResearchData ready for the template.
    """
    if tickers is None:
        tickers = list(DEFAULT_TICKERS)

    # Patch C: env-var-gated dispatch. Default off → legacy inline path
    # runs unchanged. Set to 1/true to activate the Redis consumer.
    if _env_bool("RESEARCH_USE_REDIS", default=False):
        return _research_data_from_redis(
            tickers=tickers,
            intent=intent,
            redis_client=redis_client,
        )

    # Legacy inline-build path (unchanged from v11.6 / Patch A).
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
        snap = build_ticker_snapshot(t, intent, data_router=data_router)
        snapshots.append(snap)

    with_data = sum(1 for s in snapshots if s.error is None and s.spot)
    errored = sum(1 for s in snapshots if s.error is not None)
    fields_lit_avg = (
        sum(s.fields_lit for s in snapshots if s.error is None) / max(with_data, 1)
        if with_data > 0 else 0.0
    )
    fields_total = next((s.fields_total for s in snapshots if s.fields_total > 0), 0)

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
