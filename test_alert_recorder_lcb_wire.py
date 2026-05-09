"""Test the LCB recorder helper. Patch G.3.

The full integration test for _try_post_long_call_burst would require
booting the bot. Hermetic test focuses on the pure helper that converts
local state into kwargs for alert_recorder.record_alert.

Implementation note: app.py cannot be `import`-ed on Windows (it does
`import fcntl` at top-level, POSIX-only) and pulls heavy deps (requests,
flask, gspread, etc.) on every platform. To keep this test hermetic
across dev environments, we extract the helper's source from app.py via
AST and exec it into a clean namespace. This proves:
  1. The helper is syntactically present in app.py.
  2. The helper has no hidden runtime deps on the rest of app.py.
  3. The helper produces the right kwargs.
"""
import ast
import os


def _load_helper():
    """Locate and exec _build_lcb_alert_payload from app.py into a fresh
    namespace, returning the function object.

    Also imports the LCB_ENGINE_VERSION constant if present so the helper
    can reference it.
    """
    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    func_node = None
    const_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_lcb_alert_payload":
            func_node = node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "LCB_ENGINE_VERSION":
                    const_node = node

    if func_node is None:
        raise AssertionError("_build_lcb_alert_payload not found at module scope in app.py")
    if const_node is None:
        raise AssertionError("LCB_ENGINE_VERSION constant not found at module scope in app.py")

    ns = {"__name__": "lcb_helper_test_ns"}
    # Exec the constant assignment first so the helper can reference it.
    exec(compile(ast.Module(body=[const_node], type_ignores=[]), "<lcb_const>", "exec"), ns)
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<lcb_helper>", "exec"), ns)
    return ns["_build_lcb_alert_payload"], ns["LCB_ENGINE_VERSION"]


def test_build_lcb_payload_from_v2_result():
    """Helper produces the right kwargs dict using canonical setup_grade/bias attrs."""
    _build_lcb_alert_payload, _engine_version = _load_helper()

    class FakeV2:
        # Canonical attribute names (post-G.4 fix)
        setup_grade = "A"
        momentum_burst_label = "YES"
        momentum_burst_score = 7
        rsi = 62.0
        adx = 24.1
        macd_hist = 0.05
        volume_ratio = 1.6
        regime = "BULL_BASE"

    payload = _build_lcb_alert_payload(
        ticker="SPY", bias="bull", spot=588.30,
        v2_result=FakeV2(),
        suggested_strike=590.0,
        suggested_expiry="2026-05-15",
        entry_mark=2.85,
        dte_days=6,
        canonical_snapshot={"intent": "front", "expiration": "2026-05-15"},
        webhook_data={"some_field": "value"},
        v2_5d_parent_alert_id=None,
    )
    assert payload["engine"] == "long_call_burst"
    assert payload["ticker"] == "SPY"
    assert payload["classification"] == "BURST_YES"
    assert payload["direction"] == "bull"
    assert payload["suggested_structure"]["type"] == "long_call"
    assert payload["suggested_structure"]["strike"] == 590.0
    assert payload["suggested_structure"]["expiry"] == "2026-05-15"
    assert payload["suggested_structure"]["entry_mark"] == 2.85
    assert payload["features"]["v2_5d_grade"] == "A"   # reads from setup_grade
    assert payload["features"]["momentum_burst_label"] == "YES"
    assert payload["parent_alert_id"] is None
    assert payload["spot_at_fire"] == 588.30
    assert payload["suggested_dte"] == 6
    assert payload["telegram_chat"] == "main"
    assert payload["engine_version"]  # any non-empty string


def test_build_lcb_payload_with_parent():
    _build_lcb_alert_payload, _ = _load_helper()

    class FakeV2:
        setup_grade = "B"
        momentum_burst_label = "YES"
        momentum_burst_score = 5
        rsi = 55.0
        adx = 20.0
        macd_hist = 0.02
        volume_ratio = 1.2
        regime = "BULL_BASE"

    payload = _build_lcb_alert_payload(
        ticker="QQQ", bias="bull", spot=510.0,
        v2_result=FakeV2(),
        suggested_strike=512.0, suggested_expiry="2026-05-22",
        entry_mark=3.10, dte_days=13,
        canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id="parent-123",
    )
    assert payload["parent_alert_id"] == "parent-123"


def test_build_lcb_payload_handles_missing_v2_attributes():
    """If V2SetupResult lacks an attribute, helper returns None for that
    feature (uses getattr with default)."""
    _build_lcb_alert_payload, _ = _load_helper()

    class MinimalV2:
        setup_grade = "A"
        momentum_burst_label = "YES"
        # missing: momentum_burst_score, rsi, adx, macd_hist, volume_ratio, regime

    payload = _build_lcb_alert_payload(
        ticker="SPY", bias="bull", spot=588.30,
        v2_result=MinimalV2(),
        suggested_strike=590.0, suggested_expiry="2026-05-15",
        entry_mark=2.85, dte_days=6,
        canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id=None,
    )
    # Missing attrs should be None in the features dict
    assert payload["features"]["rsi"] is None
    assert payload["features"]["adx"] is None
    assert payload["features"]["regime"] is None


def test_build_lcb_payload_old_attr_names_fallback():
    """Defensive fallback: if v2_result only has the legacy 'grade' attr
    (not 'setup_grade'), the helper still reads v2_5d_grade correctly."""
    _build_lcb_alert_payload, _ = _load_helper()

    class LegacyFakeV2:
        # Deliberately uses old name only (no setup_grade)
        grade = "GRADE_A"
        momentum_burst_label = "YES"
        momentum_burst_score = 6

    payload = _build_lcb_alert_payload(
        ticker="SPY", bias="bull", spot=585.0,
        v2_result=LegacyFakeV2(),
        suggested_strike=587.0, suggested_expiry="2026-05-15",
        entry_mark=2.50, dte_days=6,
        canonical_snapshot={}, webhook_data={},
        v2_5d_parent_alert_id=None,
    )
    # setup_grade is None on LegacyFakeV2 so fallback to grade
    assert payload["features"]["v2_5d_grade"] == "GRADE_A"


if __name__ == "__main__":
    tests = [
        test_build_lcb_payload_from_v2_result,
        test_build_lcb_payload_with_parent,
        test_build_lcb_payload_handles_missing_v2_attributes,
        test_build_lcb_payload_old_attr_names_fallback,
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
