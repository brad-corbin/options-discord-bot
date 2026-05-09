"""Test the credit recorder helper. Patch G.5.

Hermetic test using AST-extract pattern (importing app.py fails on
Windows due to fcntl). See test_alert_recorder_lcb_wire.py for the
template.

Extracts _build_credit_alert_payload and CREDIT_ENGINE_VERSION from
app.py via AST and exec into a clean namespace — no bot deps needed.
"""
import ast
import os


def _load_credit_helper():
    """Load _build_credit_alert_payload + CREDIT_ENGINE_VERSION from app.py
    via AST-extract."""
    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    func_node = None
    const_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_credit_alert_payload":
            func_node = node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "CREDIT_ENGINE_VERSION":
                    const_node = node

    if func_node is None:
        raise AssertionError("_build_credit_alert_payload not found at module scope in app.py")
    if const_node is None:
        raise AssertionError("CREDIT_ENGINE_VERSION constant not found at module scope in app.py")

    ns = {"__name__": "credit_helper_test_ns"}
    exec(compile(ast.Module(body=[const_node], type_ignores=[]), "<credit_const>", "exec"), ns)
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<credit_helper>", "exec"), ns)
    return ns["_build_credit_alert_payload"], ns["CREDIT_ENGINE_VERSION"]


def test_build_credit_payload_bull_put():
    """Bull direction -> CREDIT_BULL_PUT / bull_put struct type."""
    _build, version = _load_credit_helper()

    payload = _build(
        ticker="SPY", direction="bull", spot=588.30,
        short_strike=585.0, long_strike=580.0, width=5.0,
        expiry="2026-05-08", credit=0.85, dte_days=0,
        v2_result=None,
        canonical_snapshot={"intent": "front"},
        webhook_data={"raw": "thing"},
        v2_5d_parent_alert_id="v2-parent-id",
    )
    assert payload["engine"] == "credit_v84"
    assert payload["engine_version"] == version
    assert payload["classification"] == "CREDIT_BULL_PUT"
    assert payload["direction"] == "bull"
    s = payload["suggested_structure"]
    assert s["type"] == "bull_put"
    assert s["short"] == 585.0
    assert s["long"] == 580.0
    assert s["width"] == 5.0
    assert s["credit"] == 0.85
    assert s["expiry"] == "2026-05-08"
    assert payload["parent_alert_id"] == "v2-parent-id"
    assert payload["spot_at_fire"] == 588.30
    assert payload["suggested_dte"] == 0
    assert payload["telegram_chat"] == "main"
    # Features
    assert payload["features"]["width"] == 5.0
    assert payload["features"]["credit"] == 0.85
    assert payload["features"]["dte_days"] == 0
    # credit_pct: 0.85 / 5.0 = 0.17
    assert abs(payload["features"]["credit_pct"] - 0.17) < 1e-9
    # No v2_result -> grade and regime are None
    assert payload["features"]["v2_5d_grade"] is None
    assert payload["features"]["regime"] is None


def test_build_credit_payload_bear_call():
    """Bear direction -> CREDIT_BEAR_CALL / bear_call struct type."""
    _build, _ = _load_credit_helper()

    payload = _build(
        ticker="QQQ", direction="bear", spot=510.0,
        short_strike=512.0, long_strike=517.0, width=5.0,
        expiry="2026-05-08", credit=0.95, dte_days=0,
        v2_result=None, canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id=None,
    )
    assert payload["classification"] == "CREDIT_BEAR_CALL"
    assert payload["suggested_structure"]["type"] == "bear_call"
    assert payload["parent_alert_id"] is None


def test_build_credit_payload_with_v2_result():
    """V2 5D parent context populates v2_5d_grade and regime features."""
    _build, _ = _load_credit_helper()

    class FakeV2:
        setup_grade = "A"
        bias = "bull"
        regime = "BULL_BASE"

    payload = _build(
        ticker="SPY", direction="bull", spot=590.0,
        short_strike=585.0, long_strike=580.0, width=5.0,
        expiry="2026-05-15", credit=0.85, dte_days=6,
        v2_result=FakeV2(),
        canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id="v2-parent",
    )
    # v2_5d_grade from setup_grade (G.4 fix pattern)
    assert payload["features"]["v2_5d_grade"] == "A"
    assert payload["features"]["regime"] == "BULL_BASE"
    assert payload["parent_alert_id"] == "v2-parent"


def test_build_credit_payload_v2_grade_fallback():
    """Defensive fallback: if v2_result only has legacy 'grade' attr, still reads correctly."""
    _build, _ = _load_credit_helper()

    class LegacyV2:
        grade = "B"   # old attr name, no setup_grade
        regime = None

    payload = _build(
        ticker="SPY", direction="bull", spot=590.0,
        short_strike=585.0, long_strike=580.0, width=5.0,
        expiry="2026-05-15", credit=0.85, dte_days=6,
        v2_result=LegacyV2(),
        canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id=None,
    )
    assert payload["features"]["v2_5d_grade"] == "B"


def test_build_credit_payload_credit_pct_none_when_zero_width():
    """credit_pct is None when width is 0 (avoid division by zero)."""
    _build, _ = _load_credit_helper()

    payload = _build(
        ticker="SPY", direction="bull", spot=590.0,
        short_strike=585.0, long_strike=585.0, width=0,
        expiry="2026-05-15", credit=0.0, dte_days=6,
        v2_result=None, canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id=None,
    )
    assert payload["features"]["credit_pct"] is None


if __name__ == "__main__":
    tests = [
        test_build_credit_payload_bull_put,
        test_build_credit_payload_bear_call,
        test_build_credit_payload_with_v2_result,
        test_build_credit_payload_v2_grade_fallback,
        test_build_credit_payload_credit_pct_none_when_zero_width,
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
