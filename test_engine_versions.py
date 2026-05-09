"""Tests for register_engine_versions. Patch G.9.

Hermetic — uses temp DB. Calls the function via AST-extract since
importing app.py fails on Windows.
"""
import ast
import os
import shutil
import sqlite3
import tempfile


def _setup():
    tmpdir = tempfile.mkdtemp(prefix="recorder_g9_")
    db = os.path.join(tmpdir, "desk.db")
    os.environ["RECORDER_DB_PATH"] = db
    os.environ["RECORDER_ENABLED"] = "true"
    from db_migrate import apply_migrations
    apply_migrations(db)
    return tmpdir, db


def _teardown(tmpdir):
    for k in ("RECORDER_DB_PATH", "RECORDER_ENABLED"):
        os.environ.pop(k, None)
    try:
        import alert_recorder
        with alert_recorder._conn_lock:
            for c in alert_recorder._conn_cache.values():
                try:
                    c.close()
                except Exception:
                    pass
            alert_recorder._conn_cache.clear()
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def _load_register_engine_versions():
    """Load register_engine_versions from app.py via AST-extract.

    The function references LCB_ENGINE_VERSION, V25D_ENGINE_VERSION,
    CREDIT_ENGINE_VERSION constants in app.py and CONVICTION_ENGINE_VERSION
    from oi_flow. Load all four constants and the function into a clean
    namespace plus mock the alert_recorder import.
    """
    with open("app.py", "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in (
                    "LCB_ENGINE_VERSION", "V25D_ENGINE_VERSION",
                    "CREDIT_ENGINE_VERSION",
                ):
                    exec(compile(ast.Module(body=[node], type_ignores=[]),
                                 "<g9>", "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name == "register_engine_versions":
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<g9>", "exec"), ns)
    # Provide the runtime imports the function needs
    import alert_recorder
    import logging
    ns["_alert_recorder"] = alert_recorder
    ns["log"] = logging.getLogger("test_g9")
    # Pull CONVICTION_ENGINE_VERSION from oi_flow
    from oi_flow import CONVICTION_ENGINE_VERSION
    ns["CONVICTION_ENGINE_VERSION"] = CONVICTION_ENGINE_VERSION
    return ns["register_engine_versions"]


def test_register_engine_versions_writes_all_four():
    tmpdir, db = _setup()
    try:
        register = _load_register_engine_versions()
        register()
        register()  # idempotent
        register()
        conn = sqlite3.connect(db)
        rows = dict(conn.execute(
            "SELECT engine, engine_version FROM engine_versions"
        ).fetchall())
        for e in ("long_call_burst", "v2_5d", "credit_v84",
                  "oi_flow_conviction"):
            assert e in rows, f"missing {e}: {rows}"
        conn.close()
    finally:
        _teardown(tmpdir)


def test_register_skips_when_recorder_disabled():
    """Master gate off -> no rows written."""
    tmpdir, db = _setup()
    os.environ["RECORDER_ENABLED"] = "false"
    try:
        register = _load_register_engine_versions()
        register()
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM engine_versions").fetchall()
        assert rows == [(0,)], "master gate off should not write engine versions"
        conn.close()
    finally:
        _teardown(tmpdir)


if __name__ == "__main__":
    tests = [
        test_register_engine_versions_writes_all_four,
        test_register_skips_when_recorder_disabled,
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
