"""Test the V2 5D recorder helper. Patch G.4.

Hermetic test for `_build_v25d_alert_payload` extracted from app.py via
AST. Same pattern as test_alert_recorder_lcb_wire.py — app.py cannot be
`import`-ed on Windows (top-level `import fcntl`) and pulls heavy deps
on every platform, so we exec the helper into a clean namespace.
"""
import ast
import os


def _load_v25d_helper():
    """Locate and exec _build_v25d_alert_payload from app.py into a fresh
    namespace, returning the function and the V25D_ENGINE_VERSION constant.
    """
    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    func_node = None
    const_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_v25d_alert_payload":
            func_node = node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "V25D_ENGINE_VERSION":
                    const_node = node

    if func_node is None:
        raise AssertionError("_build_v25d_alert_payload not found at module scope in app.py")
    if const_node is None:
        raise AssertionError("V25D_ENGINE_VERSION constant not found at module scope in app.py")

    ns = {"__name__": "v25d_helper_test_ns"}
    exec(compile(ast.Module(body=[const_node], type_ignores=[]), "<v25d_const>", "exec"), ns)
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<v25d_helper>", "exec"), ns)
    return ns["_build_v25d_alert_payload"], ns["V25D_ENGINE_VERSION"]


def test_build_v25d_payload_canonical_attrs():
    """Helper reads setup_grade and bias (canonical V2SetupResult attr names)
    and populates classification/direction correctly."""
    _build, V25D_ENGINE_VERSION = _load_v25d_helper()

    class FakeV2:
        # Canonical attribute names (post-G.4 fix)
        setup_grade = "A"
        bias = "bull"
        setup_archetype = "BULL_MOMENTUM"
        action = "REVIEW"
        final_action = "REVIEW"
        trade_expression = "5D_STRUCTURE"
        vehicle_status = "APPROVED"
        momentum_burst_score = 8
        momentum_burst_label = "YES"
        historical_proxy_wr = 0.62
        mtf_alignment = "aligned"
        hold_window = "5 trading days"

    payload = _build(
        ticker="SPY", spot=588.30, v2_result=FakeV2(),
        canonical_snapshot={"intent": "front"},
        webhook_data={"foo": "bar"},
    )
    assert payload["engine"] == "v2_5d"
    assert payload["engine_version"] == V25D_ENGINE_VERSION
    assert payload["classification"] == "A"          # from setup_grade
    assert payload["direction"] == "bull"            # from bias
    assert payload["features"]["v2_5d_grade"] == "A"
    assert payload["features"]["setup_archetype"] == "BULL_MOMENTUM"
    assert payload["features"]["action"] == "REVIEW"
    assert payload["features"]["final_action"] == "REVIEW"
    assert payload["features"]["trade_expression"] == "5D_STRUCTURE"
    assert payload["features"]["vehicle_status"] == "APPROVED"
    assert payload["features"]["momentum_burst_score"] == 8
    assert payload["features"]["momentum_burst_label"] == "YES"
    assert payload["features"]["historical_proxy_wr"] == 0.62
    assert payload["features"]["mtf_alignment"] == "aligned"
    assert payload["features"]["hold_window"] == "5 trading days"
    assert payload["parent_alert_id"] is None
    assert payload["telegram_chat"] == "main"
    assert payload["spot_at_fire"] == 588.30
    assert payload["suggested_dte"] is None  # V2 5D is a classifier — no DTE


def test_build_v25d_payload_block_grade():
    """BLOCK grade with bias=bear populates classification and direction."""
    _build, _ = _load_v25d_helper()

    class FakeV2:
        setup_grade = "BLOCK"
        bias = "bear"
        setup_archetype = "WEAK_SETUP"
        action = "NO TRADE"
        final_action = "NO TRADE"
        trade_expression = "BLOCK"
        vehicle_status = "NOT_CHECKED"
        momentum_burst_score = 0
        momentum_burst_label = "NO"
        historical_proxy_wr = 0.0
        mtf_alignment = "unknown"
        hold_window = "5 trading days"

    payload = _build(
        ticker="QQQ", spot=510.0, v2_result=FakeV2(),
        canonical_snapshot={}, webhook_data={},
    )
    assert payload["classification"] == "BLOCK"
    assert payload["direction"] == "bear"


def test_build_v25d_payload_old_attr_names_fallback():
    """Defensive fallback: if v2_result only has the legacy 'grade'/'direction'
    attrs (not 'setup_grade'/'bias'), the helper still returns correct values."""
    _build, _ = _load_v25d_helper()

    class LegacyFakeV2:
        # Deliberately uses old names only
        grade = "GRADE_A"
        direction = "bull"
        # No setup_grade, no bias, no new attrs
        setup_archetype = None
        action = None
        final_action = None
        trade_expression = None
        vehicle_status = None
        momentum_burst_score = None
        momentum_burst_label = None
        historical_proxy_wr = None
        mtf_alignment = None
        hold_window = None

    payload = _build(
        ticker="SPY", spot=582.0, v2_result=LegacyFakeV2(),
        canonical_snapshot={}, webhook_data={},
    )
    # setup_grade is missing → falls back to grade
    assert payload["classification"] == "GRADE_A"
    # bias is missing → falls back to direction
    assert payload["direction"] == "bull"


if __name__ == "__main__":
    tests = [
        test_build_v25d_payload_canonical_attrs,
        test_build_v25d_payload_block_grade,
        test_build_v25d_payload_old_attr_names_fallback,
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
