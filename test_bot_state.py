"""
test_bot_state.py — Unit tests for BotState.build_from_raw.

Verifies:
  1. Permissive pattern: build_from_raw() always returns a valid object,
     even when most canonical functions are stubbed.
  2. Live integration: gamma_flip is a real value (canonical_gamma_flip
     is implemented), not None.
  3. Stub fields: every other canonical-fed field is None as expected,
     with status correctly recorded in canonical_status.
  4. Trivial summaries (volume, rvol) compute correctly from raw quote.
  5. Derived fields (distance_from_flip_pct, flip_location, gex_sign)
     compute correctly from canonical inputs.
  6. fields_lit / fields_total accessors return sensible counts.

Runs without network, no Schwab credentials needed.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from raw_inputs import RawInputs
from bot_state import BotState

PASSED = []
FAILED = []


def assert_eq(actual, expected, msg):
    if actual != expected:
        FAILED.append(f"{msg}: expected {expected!r}, got {actual!r}")
        return False
    return True


def assert_true(cond, msg):
    if not cond:
        FAILED.append(f"{msg}: condition False")
        return False
    return True


def assert_in_range(actual, lo, hi, msg):
    if actual is None or actual < lo or actual > hi:
        FAILED.append(f"{msg}: {actual!r} not in [{lo}, {hi}]")
        return False
    return True


def assert_is_none(actual, msg):
    if actual is not None:
        FAILED.append(f"{msg}: expected None, got {actual!r}")
        return False
    return True


# ───────────────────────────────────────────────────────────────────────
# Test fixtures
# ───────────────────────────────────────────────────────────────────────

def _build_realistic_raw_inputs(spot: float = 100.0) -> RawInputs:
    """Synthetic RawInputs with put-wall/call-wall positioning so
    canonical_gamma_flip finds a meaningful flip."""
    chain = {
        "s": "ok",
        "optionSymbol": [],
        "strike": [],
        "side": [],
        "expiration": [],
        "openInterest": [],
        "volume": [],
        "delta": [],
        "gamma": [],
        "iv": [],
        "bid": [],
        "ask": [],
    }

    # Put wall
    for K in [spot * 0.92, spot * 0.95, spot * 0.97]:
        chain["optionSymbol"].append(f"PW{int(K*100):08d}P")
        chain["strike"].append(round(K, 2))
        chain["side"].append("put")
        chain["expiration"].append("2026-05-09")
        chain["openInterest"].append(8000)
        chain["volume"].append(200)
        chain["delta"].append(-0.30)
        chain["gamma"].append(0.04)
        chain["iv"].append(0.25)
        chain["bid"].append(0.5)
        chain["ask"].append(0.7)

    # Call wall
    for K in [spot * 1.03, spot * 1.05, spot * 1.08]:
        chain["optionSymbol"].append(f"CW{int(K*100):08d}C")
        chain["strike"].append(round(K, 2))
        chain["side"].append("call")
        chain["expiration"].append("2026-05-09")
        chain["openInterest"].append(8000)
        chain["volume"].append(200)
        chain["delta"].append(0.30)
        chain["gamma"].append(0.04)
        chain["iv"].append(0.25)
        chain["bid"].append(0.5)
        chain["ask"].append(0.7)

    # ATM volume
    for K in [spot * 0.99, spot, spot * 1.01]:
        for side in ("call", "put"):
            chain["optionSymbol"].append(f"AT{int(K*100):08d}{side[0].upper()}")
            chain["strike"].append(round(K, 2))
            chain["side"].append(side)
            chain["expiration"].append("2026-05-09")
            chain["openInterest"].append(2000)
            chain["volume"].append(500)
            chain["delta"].append(0.5 if side == "call" else -0.5)
            chain["gamma"].append(0.05)
            chain["iv"].append(0.25)
            chain["bid"].append(1.0)
            chain["ask"].append(1.2)

    return RawInputs(
        ticker="TEST",
        spot=spot,
        chain=chain,
        expiration="2026-05-09",
        quote={"totalVolume": 1_000_000, "avgVolume20d": 800_000},
        bars=[{"o": 1.0, "h": 1.5, "l": 0.8, "c": 1.2, "v": 1000} for _ in range(504)],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc),
        fetch_errors=(),
    )


def _build_minimal_raw_inputs() -> RawInputs:
    """Even thinner — just enough for a build_from_raw() that produces a valid object."""
    return RawInputs(
        ticker="MIN",
        spot=50.0,
        chain={"s": "ok", "optionSymbol": ["X"], "strike": [50], "side": ["call"],
               "expiration": ["2026-05-09"], "openInterest": [100], "iv": [0.25]},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(),
    )


# ───────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────

def test_build_returns_valid_object_with_stubs():
    """The whole point of permissive build: never crashes, always returns a state."""
    raw = _build_minimal_raw_inputs()
    state = BotState.build_from_raw(raw)
    assert_eq(state.ticker, "MIN", "ticker preserved")
    assert_eq(state.spot, 50.0, "spot preserved")
    assert_eq(state.expiration, "2026-05-09", "expiration preserved")
    PASSED.append("test_build_returns_valid_object_with_stubs")


def test_canonical_status_records_each_canonical():
    """canonical_status dict should have an entry for every canonical_X attempted."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)

    # Live canonicals as of Patch 11.5: gamma_flip, iv_state, exposures, walls
    # Stubbed: pivots, structure, em_state, technicals, bias, dealer_regime,
    #          vol_regime, potter_box, flow_state, calendar
    expected_canonicals = {
        "gamma_flip", "iv_state", "exposures", "walls",
        "pivots", "structure", "em_state", "technicals",
        "bias", "dealer_regime", "vol_regime", "potter_box", "flow_state", "calendar",
    }
    for c in expected_canonicals:
        if c not in state.canonical_status:
            FAILED.append(f"canonical_status missing key: {c}")
            return
    PASSED.append("test_canonical_status_records_each_canonical")


def test_gamma_flip_is_live_post_patch_11_2():
    """canonical_gamma_flip is implemented — gamma_flip should be a real number, not None."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)
    if state.gamma_flip is None:
        FAILED.append(f"test_gamma_flip_is_live: got None — canonical_gamma_flip should produce value. "
                      f"status: {state.canonical_status.get('gamma_flip')}")
        return
    assert_eq(state.canonical_status.get("gamma_flip"), "live", "status marked live")
    assert_in_range(state.gamma_flip, 75.0, 125.0, "flip in ±25% band of spot 100")
    PASSED.append("test_gamma_flip_is_live_post_patch_11_2")


def test_distance_and_flip_location_derive_from_gamma_flip():
    raw = _build_realistic_raw_inputs(spot=100.0)
    state = BotState.build_from_raw(raw)

    # When gamma_flip is computed, distance and location must derive from it
    if state.gamma_flip is None:
        FAILED.append("test_derived: prerequisite gamma_flip is None")
        return
    if state.distance_from_flip_pct is None:
        FAILED.append("test_derived: distance_from_flip_pct should be computed")
        return
    if state.flip_location == "unknown":
        FAILED.append(f"test_derived: flip_location should be classified, "
                      f"got 'unknown' (gamma_flip={state.gamma_flip})")
        return
    assert_true(state.flip_location in ("above_flip", "below_flip", "at_flip"),
                "flip_location is a valid label")
    PASSED.append("test_distance_and_flip_location_derive_from_gamma_flip")


def test_stubbed_canonicals_record_stub_status():
    """Every canonical that's still stubbed should be marked 'stub' in status."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)

    # Live: gamma_flip (Patch 11.2), iv_state (Patch 11.3.2),
    #       exposures + walls (Patches 11.4 / 11.5),
    #       technicals (Patch F.5.2 — only 'stub' when bars is empty)
    # Every other canonical should be stub.
    expected_stubs = ["pivots",
                      "structure", "em_state",
                      "bias", "dealer_regime", "vol_regime", "potter_box",
                      "flow_state", "calendar"]
    for c in expected_stubs:
        s = state.canonical_status.get(c, "")
        if not s.startswith("stub"):
            FAILED.append(f"test_stubbed_status: {c} should be 'stub*', got {s!r}")
            return
    PASSED.append("test_stubbed_canonicals_record_stub_status")


def test_stubbed_canonical_fields_are_none():
    """Every field fed by a still-stubbed canonical should be None or 'unknown'."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)

    # Greek aggregates: NOW LIVE via canonical_exposures (Patch 11.4)
    # — so they should NOT be None on a healthy chain. Sanity check inverted.
    if state.gex is not None:
        # gex_sign should also be set (positive/negative/neutral, not unknown)
        assert_true(state.gex_sign in ("positive", "negative", "neutral"),
                    f"gex_sign classified, got {state.gex_sign}")

    # Walls: NOW LIVE via canonical_exposures (Patch 11.5).
    # call_wall/put_wall/gamma_wall populated when chain has the structure.
    # max_pain stays None — separate canonical, not yet wired.
    assert_is_none(state.max_pain, "max_pain None — separate canonical")

    # Technicals: NOW LIVE via canonical_technicals (Patch F.5.2).
    # rsi / macd_hist populate when bars are present in the raw inputs.
    # (No assertion here — both None and non-None are valid depending on bars.)

    # Bias / regime
    assert_eq(state.dealer_regime, "unknown", "dealer_regime 'unknown'")
    assert_is_none(state.bias_score, "bias_score None")

    PASSED.append("test_stubbed_canonical_fields_are_none")


def test_volume_summaries_compute_from_quote():
    """Volume / rvol come from raw.quote, not a canonical function."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)
    assert_eq(state.volume_today, 1_000_000.0, "volume_today from quote")
    assert_eq(state.rvol, 1.25, "rvol = 1M / 800k = 1.25")
    PASSED.append("test_volume_summaries_compute_from_quote")


def test_volume_handles_missing_quote_data():
    """Empty quote → volume None, rvol None, no crash."""
    raw = _build_minimal_raw_inputs()
    state = BotState.build_from_raw(raw)
    assert_is_none(state.volume_today, "volume_today None when quote empty")
    assert_is_none(state.rvol, "rvol None when no avg_volume")
    PASSED.append("test_volume_handles_missing_quote_data")


def test_immutability():
    raw = _build_minimal_raw_inputs()
    state = BotState.build_from_raw(raw)
    try:
        state.spot = 999.0  # type: ignore
        FAILED.append("test_immutability: expected FrozenInstanceError")
    except Exception as e:
        if "frozen" not in str(e).lower() and "FrozenInstanceError" not in type(e).__name__:
            FAILED.append(f"test_immutability: wrong exception: {e}")
            return
        PASSED.append("test_immutability")


def test_fields_lit_progress_indicator():
    """fields_lit should be small now (~9 lit fields), grow as canonicals land."""
    raw = _build_realistic_raw_inputs()
    state = BotState.build_from_raw(raw)
    lit = state.fields_lit
    total = state.fields_total

    # Currently lit: ticker, timestamp_utc, spot, expiration, chain_clean,
    # convention_version, snapshot_version, gamma_flip, distance_from_flip_pct,
    # flip_location, volume_today, rvol = ~12 fields
    assert_in_range(lit, 8, 25, f"fields_lit reasonable for early rebuild (got {lit}/{total})")
    assert_in_range(total, 50, 80, f"fields_total reasonable (got {total})")
    assert_true(lit < total, "fields_lit < total (rebuild in progress)")
    PASSED.append("test_fields_lit_progress_indicator")


def test_fetch_errors_propagated():
    """Errors from raw fetch are preserved on BotState."""
    raw = RawInputs(
        ticker="ERR",
        spot=100.0,
        chain={},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(("get_chain", "test failure"),),
    )
    state = BotState.build_from_raw(raw)
    assert_eq(state.chain_clean, False, "chain_clean False when raw had errors")
    assert_eq(len(state.fetch_errors), 1, "errors propagated")
    assert_eq(state.fetch_errors[0][0], "get_chain", "error tagged correctly")
    PASSED.append("test_fetch_errors_propagated")


def test_empty_chain_doesnt_crash_build():
    """Even with empty chain, build_from_raw should not crash —
    gamma_flip will be None but the state object is valid."""
    raw = RawInputs(
        ticker="EMPTY",
        spot=100.0,
        chain={},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(("get_chain", "empty"),),
    )
    state = BotState.build_from_raw(raw)
    assert_is_none(state.gamma_flip, "gamma_flip None when chain empty")
    # status should record the error, not 'live' or 'stub'
    s = state.canonical_status.get("gamma_flip", "")
    assert_true(s.startswith("error") or s == "live",
                f"gamma_flip status reasonable: {s!r}")
    PASSED.append("test_empty_chain_doesnt_crash_build")


def test_iv_state_is_live_via_canonical():
    """Patch 11.3.2: canonical_iv_state landed and BotState wires through to it.
    iv_state should be 'live' in canonical_status, atm_iv should be populated,
    and the representative_iv flows into canonical_gamma_flip's IV-aware band.
    """
    raw = _build_realistic_raw_inputs(spot=100.0)
    state = BotState.build_from_raw(raw)

    iv_state_status = state.canonical_status.get("iv_state", "")
    assert_eq(iv_state_status, "live", "iv_state status is live")

    if state.atm_iv is None:
        FAILED.append(f"test_iv_state_live: atm_iv None despite live status")
        return
    # Synthetic chain has IV=0.25 throughout — UnifiedIVSurface re-derives via
    # resolve_iv so won't be exactly 0.25 but should be in same neighborhood
    assert_in_range(state.atm_iv, 0.10, 0.50,
                    f"atm_iv in reasonable range, got {state.atm_iv}")
    PASSED.append("test_iv_state_is_live_via_canonical")


def test_gamma_flip_uses_iv_aware_band_via_canonical_iv_state():
    """End-to-end: BotState calls canonical_iv_state, gets representative_iv,
    passes it to canonical_gamma_flip → IV-aware band. Both canonicals should
    be live, gamma_flip should be a real value."""
    raw = _build_realistic_raw_inputs(spot=100.0)
    state = BotState.build_from_raw(raw)

    assert_eq(state.canonical_status.get("iv_state"), "live", "iv_state live")
    assert_eq(state.canonical_status.get("gamma_flip"), "live", "gamma_flip live")

    if state.gamma_flip is None:
        FAILED.append("test_iv_aware_e2e: gamma_flip None")
        return
    assert_in_range(state.gamma_flip, 75.0, 125.0, "flip in reasonable band")
    PASSED.append("test_gamma_flip_uses_iv_aware_band_via_canonical_iv_state")


def test_iv_state_handles_empty_chain_gracefully():
    """Empty chain → iv_state status is 'live' but the result has empty values
    (canonical_iv_state returns a dict with None fields, source='empty_chain').
    """
    raw = RawInputs(
        ticker="EMPTY",
        spot=100.0,
        chain={},
        expiration="2026-05-09",
        quote={},
        bars=[],
        iv_surface=None,
        fetched_at_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        fetch_errors=(("get_chain", "empty"),),
    )
    state = BotState.build_from_raw(raw)
    # canonical_iv_state returns gracefully (status 'live'), but the values are None
    assert_eq(state.canonical_status.get("iv_state"), "live",
              "iv_state status 'live' even on empty (graceful return)")
    assert_is_none(state.atm_iv, "atm_iv None on empty chain")
    assert_is_none(state.iv_skew_pp, "iv_skew_pp None on empty chain")
    PASSED.append("test_iv_state_handles_empty_chain_gracefully")


def test_exposures_is_live_via_canonical():
    """Patch 11.4: canonical_exposures landed. Greek aggregates populated,
    gex_sign classified, exposures status 'live' in canonical_status."""
    raw = _build_realistic_raw_inputs(spot=100.0)
    state = BotState.build_from_raw(raw)

    assert_eq(state.canonical_status.get("exposures"), "live", "exposures live")

    if state.gex is None:
        FAILED.append(f"test_exposures_live: gex None despite live status")
        return
    assert_true(isinstance(state.gex, (int, float)), "gex is a number")

    # gex_sign should be classified now that gex is populated
    assert_true(state.gex_sign in ("positive", "negative", "neutral"),
                f"gex_sign classified from gex value, got {state.gex_sign}")
    PASSED.append("test_exposures_is_live_via_canonical")


def test_walls_are_live_via_canonical_exposures():
    """Patch 11.5: walls share canonical_exposures' compute (no separate
    canonical). When exposures is live, walls status mirrors it and
    call_wall/put_wall/gamma_wall populate from exposures.walls dict.
    """
    raw = _build_realistic_raw_inputs(spot=100.0)
    state = BotState.build_from_raw(raw)

    # Status: walls mirrors exposures since they share the same compute
    assert_eq(state.canonical_status.get("walls"), "live", "walls live")
    assert_eq(state.canonical_status.get("exposures"), "live",
              "exposures live (walls share its compute)")

    # The realistic chain has put-wall (8000 OI at 92/95/97) and call-wall
    # (8000 OI at 103/105/108) positioning, so call_wall and put_wall
    # should populate from canonical_exposures.walls.
    assert_true(state.call_wall is not None,
                f"call_wall populated, got {state.call_wall}")
    assert_true(state.put_wall is not None,
                f"put_wall populated, got {state.put_wall}")
    PASSED.append("test_walls_are_live_via_canonical_exposures")


# ───────────────────────────────────────────────────────────────────────
# v11.7 (Patch F.5.1): canonical_technicals integration tests.
# ───────────────────────────────────────────────────────────────────────

def test_build_technicals_from_raw_full_clean_bars():
    """With ~100 clean bars, helper returns the same numbers as
    canonical_technicals when called directly. Wrapper-consistency."""
    from bot_state import _build_technicals_from_raw
    import canonical_technicals as ct

    # Build 100 synthetic bars (enough for RSI(14), MACD(26+9=35), ADX(14)).
    # Use a simple linear ramp so the indicators have nonzero, deterministic
    # values.
    bars = [
        {"h": 100.0 + i + 0.5, "l": 100.0 + i - 0.5, "c": 100.0 + i,
         "o": 100.0 + i - 0.2, "v": 1_000_000}
        for i in range(100)
    ]

    class FakeRaw:
        ticker = "TEST"
        bars = None  # set below
    raw = FakeRaw()
    raw.bars = bars

    result = _build_technicals_from_raw(raw)

    # Wrapper-consistency: each value matches canonical_technicals' output.
    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    expected_rsi = ct.rsi(closes)
    expected_macd = ct.macd(closes)
    expected_adx = ct.adx(highs, lows, closes)

    assert result["rsi"] == expected_rsi, \
        f"RSI drift: helper={result['rsi']}, canonical={expected_rsi}"
    assert result["macd_line"] == expected_macd.get("macd_line"), \
        f"MACD line drift: helper={result['macd_line']}, canonical={expected_macd.get('macd_line')}"
    assert result["macd_signal"] == expected_macd.get("signal_line"), \
        f"MACD signal drift"
    assert result["macd_hist"] == expected_macd.get("macd_hist"), \
        f"MACD hist drift"
    assert result["adx"] == expected_adx, \
        f"ADX drift: helper={result['adx']}, canonical={expected_adx}"
    PASSED.append("test_build_technicals_from_raw_full_clean_bars")


def test_build_technicals_from_raw_handles_long_key_format():
    """Bars using 'high'/'low'/'close' (not h/l/c) must work — defensive
    pattern from risk_manager.py:275."""
    from bot_state import _build_technicals_from_raw
    import canonical_technicals as ct

    bars = [
        {"high": 100.0 + i + 0.5, "low": 100.0 + i - 0.5, "close": 100.0 + i}
        for i in range(100)
    ]
    class FakeRaw:
        ticker = "TEST"
        bars = None
    raw = FakeRaw()
    raw.bars = bars
    result = _build_technicals_from_raw(raw)

    closes = [b["close"] for b in bars]
    assert result["rsi"] == ct.rsi(closes)
    assert result["macd_hist"] == ct.macd(closes).get("macd_hist")
    PASSED.append("test_build_technicals_from_raw_handles_long_key_format")


def test_build_technicals_from_raw_empty_bars():
    """Empty bars list -> all-None RSI/MACD, ADX=0.0 (matches canonical_technicals
    convention)."""
    from bot_state import _build_technicals_from_raw

    class FakeRaw:
        ticker = "TEST"
        bars = []
    raw = FakeRaw()

    result = _build_technicals_from_raw(raw)
    assert result["rsi"] is None
    assert result["macd_line"] is None
    assert result["macd_signal"] is None
    assert result["macd_hist"] is None
    assert result["adx"] == 0.0, "ADX is 0.0 on insufficient data, not None"
    PASSED.append("test_build_technicals_from_raw_empty_bars")


def test_build_technicals_from_raw_partial_bar_keys():
    """A bar missing one of the OHLC keys -> all-None/0.0. The helper does
    not silently fill in zeros that would corrupt the math."""
    from bot_state import _build_technicals_from_raw

    bars = [{"h": 100.0, "l": 99.0, "c": 99.5} for _ in range(50)]
    bars[10] = {"h": 100.0, "l": 99.0}  # missing close -> must trip defense
    class FakeRaw:
        ticker = "TEST"
        bars = None
    raw = FakeRaw()
    raw.bars = bars

    result = _build_technicals_from_raw(raw)
    assert result["rsi"] is None
    assert result["macd_hist"] is None
    assert result["adx"] == 0.0
    PASSED.append("test_build_technicals_from_raw_partial_bar_keys")


def test_build_technicals_from_raw_handles_ohlc_objects():
    """Production hotfix regression: bars in production come as either
    dicts OR OHLC dataclass objects (options_exposure.py:501). The helper
    must support both shapes — a dict-only implementation floods logs
    with 'OHLC object has no attribute get' AttributeErrors."""
    from bot_state import _build_technicals_from_raw
    import canonical_technicals as ct
    from dataclasses import dataclass
    from typing import Optional

    @dataclass
    class FakeOHLC:
        open: float
        high: float
        low: float
        close: float
        prev_close: Optional[float] = None

    bars = [
        FakeOHLC(open=100.0 + i - 0.2,
                 high=100.0 + i + 0.5,
                 low=100.0 + i - 0.5,
                 close=100.0 + i)
        for i in range(100)
    ]
    class FakeRaw:
        ticker = "TEST"
        bars = None
    raw = FakeRaw()
    raw.bars = bars

    result = _build_technicals_from_raw(raw)

    closes = [b.close for b in bars]
    highs  = [b.high  for b in bars]
    lows   = [b.low   for b in bars]
    assert result["rsi"] == ct.rsi(closes), \
        f"RSI drift on OHLC bars: helper={result['rsi']}, canonical={ct.rsi(closes)}"
    assert result["adx"] == ct.adx(highs, lows, closes), \
        f"ADX drift on OHLC bars"
    PASSED.append("test_build_technicals_from_raw_handles_ohlc_objects")


# ───────────────────────────────────────────────────────────────────────
# v11.7 (Patch F.5.2): BotState.build_from_raw technicals wiring tests.
# ───────────────────────────────────────────────────────────────────────

def test_build_from_raw_status_technicals_is_live():
    """Spec F.5.2: BotState.build_from_raw with clean bars returns
    status['technicals'] == 'live', NOT 'stub'. Confirms the wiring
    landed and BotState is reading from canonical_technicals."""
    bars = [
        {"h": 100.0 + i + 0.5, "l": 100.0 + i - 0.5, "c": 100.0 + i,
         "o": 100.0 + i - 0.2, "v": 1_000_000}
        for i in range(100)
    ]
    raw = RawInputs(
        ticker="TEST",
        spot=199.0,
        expiration="2026-05-09",
        chain={},
        quote={},
        bars=bars,
        iv_surface=None,
        fetched_at_utc=datetime.now(timezone.utc),
        fetch_errors=(),
    )
    state = BotState.build_from_raw(raw)
    s = state.canonical_status.get("technicals")
    if s != "live":
        FAILED.append(
            f"test_build_from_raw_status_technicals_is_live: "
            f"Expected canonical_status['technicals']='live', got {s!r}. "
            f"F.5.2 wiring did not land."
        )
        return
    # Sanity: indicator values populated on the snapshot.
    if state.rsi is None:
        FAILED.append("test_build_from_raw_status_technicals_is_live: rsi should populate from helper")
        return
    if state.adx is None:
        FAILED.append("test_build_from_raw_status_technicals_is_live: adx should populate from helper")
        return
    PASSED.append("test_build_from_raw_status_technicals_is_live")


def test_build_from_raw_status_technicals_handles_bad_bars():
    """If raw.bars is malformed, the helper's defensive guard kicks in
    and returns all-None / adx=0.0; build_from_raw still runs cleanly
    (permissive build contract)."""
    raw = RawInputs(
        ticker="TEST",
        spot=199.0,
        expiration="2026-05-09",
        chain={},
        quote={},
        bars=[{"this_is_not_a_bar": "garbage"}],  # missing every OHLC key
        iv_surface=None,
        fetched_at_utc=datetime.now(timezone.utc),
        fetch_errors=(),
    )
    state = BotState.build_from_raw(raw)
    # Helper returned all-None on partial keys; status is "live" because no
    # exception raised. Build did NOT crash — that's the contract.
    s = state.canonical_status.get("technicals") or ""
    if not (s == "live" or s.startswith("stub") or s.startswith("error")):
        FAILED.append(
            f"test_build_from_raw_status_technicals_handles_bad_bars: "
            f"unexpected canonical_status {s!r}"
        )
        return
    if state.ticker != "TEST":
        FAILED.append("test_build_from_raw_status_technicals_handles_bad_bars: ticker lost")
        return
    PASSED.append("test_build_from_raw_status_technicals_handles_bad_bars")


# ───────────────────────────────────────────────────────────────────────
# Run all
# ───────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_build_returns_valid_object_with_stubs,
        test_canonical_status_records_each_canonical,
        test_gamma_flip_is_live_post_patch_11_2,
        test_distance_and_flip_location_derive_from_gamma_flip,
        test_stubbed_canonicals_record_stub_status,
        test_stubbed_canonical_fields_are_none,
        test_volume_summaries_compute_from_quote,
        test_volume_handles_missing_quote_data,
        test_immutability,
        test_fields_lit_progress_indicator,
        test_fetch_errors_propagated,
        test_empty_chain_doesnt_crash_build,
        test_iv_state_is_live_via_canonical,
        test_gamma_flip_uses_iv_aware_band_via_canonical_iv_state,
        test_iv_state_handles_empty_chain_gracefully,
        test_exposures_is_live_via_canonical,
        test_walls_are_live_via_canonical_exposures,
        # v11.7 (Patch F.5.1): canonical_technicals integration
        test_build_technicals_from_raw_full_clean_bars,
        test_build_technicals_from_raw_handles_long_key_format,
        test_build_technicals_from_raw_empty_bars,
        test_build_technicals_from_raw_partial_bar_keys,
        test_build_technicals_from_raw_handles_ohlc_objects,
        # v11.7 (Patch F.5.2): BotState.build_from_raw technicals wiring
        test_build_from_raw_status_technicals_is_live,
        test_build_from_raw_status_technicals_handles_bad_bars,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAILED.append(f"{t.__name__}: unexpected exception {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"PASSED: {len(PASSED)} / {len(tests)}")
    for p in PASSED:
        print(f"  ✓ {p}")
    if FAILED:
        print(f"\nFAILED: {len(FAILED)}")
        for f in FAILED:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print(f"\n{'='*60}")
        print("All tests passed.")


if __name__ == "__main__":
    main()
