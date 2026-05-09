"""Test the conviction recorder helper. Patch G.6.

Hermetic test. The helper lives in oi_flow.py at module scope.
oi_flow.py imports cleanly on Windows (no fcntl/POSIX-only deps), so
direct import is the primary path. AST-extract fallback retained for
robustness.

The cp dict is the play dict produced by
OIFlowDetector.detect_conviction_plays. Real keys (verified against
oi_flow.py:2705-2760):

  ticker, strike, side, trade_side, trade_direction, trade_emoji,
  volume, oi, vol_oi_ratio, burst, dte, route, expiry, spot, mid,
  ask, notional, directional_bias, has_shadow, shadow_agrees,
  is_streaming_sweep, sweep_notional, em_aligned, em_conflict, ...

trade_direction is "bullish" / "bearish" (NOT "long_call"/"long_put"
as initial task brief suggested). The helper maps:
  bullish -> CONVICTION_LONG_CALL / direction=bull
  bearish -> CONVICTION_LONG_PUT  / direction=bear
"""
import ast
import os


def _load_conviction_helper():
    """Try direct import first; fall back to AST-extract."""
    try:
        from oi_flow import (
            _build_conviction_alert_payload,
            CONVICTION_ENGINE_VERSION,
        )
        return _build_conviction_alert_payload, CONVICTION_ENGINE_VERSION
    except Exception:
        oi_path = os.path.join(os.path.dirname(__file__), "oi_flow.py")
        with open(oi_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        ns = {"__name__": "conviction_helper_test_ns"}
        const_node = None
        func_node = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "CONVICTION_ENGINE_VERSION":
                        const_node = node
            elif isinstance(node, ast.FunctionDef) and node.name == "_build_conviction_alert_payload":
                func_node = node
        if const_node is None:
            raise AssertionError("CONVICTION_ENGINE_VERSION not found at module scope in oi_flow.py")
        if func_node is None:
            raise AssertionError("_build_conviction_alert_payload not found at module scope in oi_flow.py")
        exec(compile(ast.Module(body=[const_node], type_ignores=[]), "<g6_const>", "exec"), ns)
        exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<g6_helper>", "exec"), ns)
        return ns["_build_conviction_alert_payload"], ns["CONVICTION_ENGINE_VERSION"]


def test_build_conviction_payload_bullish():
    """trade_direction=bullish -> CONVICTION_LONG_CALL / bull."""
    _build, version = _load_conviction_helper()
    cp = {
        "ticker": "NVDA",
        "trade_direction": "bullish",
        "trade_side": "LONG CALL",
        "side": "call",
        "strike": 1180.0,
        "notional": 150_000,
        "is_streaming_sweep": True,
        "sweep_notional": 200_000,
        "burst": 7000,
        "vol_oi_ratio": 12.5,
        "volume": 45_000,
        "dte": 7,
        "route": "immediate",
        "expiry": "2026-05-16",
        "spot": 1180.0,
        "directional_bias": "STRONG BULLISH",
        "direction_confidence": 0.85,
        "em_aligned": True,
        "em_conflict": False,
        "is_exit_signal": False,
        "is_reactive": False,
        "shadow_agrees": False,
    }
    payload = _build(cp=cp, canonical_snapshot={"intent": "front"},
                     posted_to="conviction")
    assert payload["engine"] == "oi_flow_conviction"
    assert payload["engine_version"] == version
    assert payload["ticker"] == "NVDA"
    assert payload["classification"] == "CONVICTION_LONG_CALL"
    assert payload["direction"] == "bull"
    assert payload["spot_at_fire"] == 1180.0
    assert payload["telegram_chat"] == "conviction"
    assert payload["parent_alert_id"] is None
    assert payload["suggested_dte"] == 7
    # Structure
    s = payload["suggested_structure"]
    assert s["type"] == "long_call"
    assert s["strike"] == 1180.0
    assert s["expiry"] == "2026-05-16"
    assert s["route"] == "immediate"
    # Features
    f = payload["features"]
    assert f["notional"] == 150_000
    assert f["sweep_notional"] == 200_000
    assert f["is_streaming_sweep"] is True
    assert f["burst"] == 7000
    assert f["vol_oi_ratio"] == 12.5
    assert f["volume"] == 45_000
    assert f["dte"] == 7
    assert f["route"] == "immediate"
    assert f["direction_confidence"] == 0.85
    assert f["em_aligned"] is True
    assert f["em_conflict"] is False
    assert f["is_exit_signal"] is False
    assert f["directional_bias"] == "STRONG BULLISH"
    # Raw engine payload is the cp dict itself
    assert payload["raw_engine_payload"] is cp


def test_build_conviction_payload_bearish():
    """trade_direction=bearish -> CONVICTION_LONG_PUT / bear."""
    _build, _ = _load_conviction_helper()
    cp = {
        "ticker": "AAPL",
        "trade_direction": "bearish",
        "trade_side": "LONG PUT",
        "side": "put",
        "strike": 220.0,
        "notional": 80_000,
        "burst": 3000,
        "vol_oi_ratio": 5.0,
        "dte": 14,
        "route": "swing",
        "expiry": "2026-05-23",
        "spot": 222.0,
    }
    payload = _build(cp=cp, canonical_snapshot={}, posted_to="main")
    assert payload["classification"] == "CONVICTION_LONG_PUT"
    assert payload["direction"] == "bear"
    assert payload["suggested_structure"]["type"] == "long_put"
    assert payload["suggested_structure"]["strike"] == 220.0
    assert payload["telegram_chat"] == "main"
    assert payload["features"]["notional"] == 80_000
    assert payload["features"]["is_streaming_sweep"] is False  # missing key -> False


def test_build_conviction_payload_handles_missing_keys():
    """Sparse cp dict — helper handles all .get(...) safely without KeyError."""
    _build, _ = _load_conviction_helper()
    cp = {"ticker": "TSLA", "trade_direction": "bullish"}
    payload = _build(cp=cp, canonical_snapshot={}, posted_to="main")
    assert payload["classification"] == "CONVICTION_LONG_CALL"
    assert payload["direction"] == "bull"
    assert payload["features"]["notional"] is None
    assert payload["features"]["sweep_notional"] is None
    assert payload["features"]["is_streaming_sweep"] is False
    assert payload["features"]["burst"] is None
    assert payload["features"]["vol_oi_ratio"] is None
    assert payload["spot_at_fire"] is None
    assert payload["suggested_dte"] is None
    # Structure type is derived from trade_direction, not from per-leg keys.
    # Strike/expiry/route can be None when the cp dict is sparse.
    assert payload["suggested_structure"]["type"] == "long_call"
    assert payload["suggested_structure"]["strike"] is None
    assert payload["suggested_structure"]["expiry"] is None
    assert payload["suggested_structure"]["route"] is None


def test_build_conviction_payload_unknown_direction():
    """trade_direction missing/unknown -> classification UNKNOWN, direction None."""
    _build, _ = _load_conviction_helper()
    cp = {"ticker": "SPY"}
    payload = _build(cp=cp, canonical_snapshot={}, posted_to="main")
    # No trade_direction at all
    assert payload["classification"] == "CONVICTION_UNKNOWN"
    assert payload["direction"] is None


if __name__ == "__main__":
    tests = [
        test_build_conviction_payload_bullish,
        test_build_conviction_payload_bearish,
        test_build_conviction_payload_handles_missing_keys,
        test_build_conviction_payload_unknown_direction,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
