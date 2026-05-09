"""Tests for db_migrate.apply_migrations.

Patch G.1 — boot-time migration runner. No network, no Schwab, no Redis.
Uses a temp directory for the SQLite DB so tests are fully hermetic.
"""
import os
import sqlite3
import tempfile
import shutil
from pathlib import Path


def _fresh_db_path():
    """Returns (tmpdir, db_path); caller is responsible for shutil.rmtree."""
    tmpdir = tempfile.mkdtemp(prefix="recorder_test_")
    return tmpdir, os.path.join(tmpdir, "test.db")


def test_apply_migrations_creates_all_tables():
    """Spec G.1: applying migrations on a fresh DB creates every V1 table."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
        for required in ("alerts", "alert_features", "alert_price_track",
                         "alert_outcomes", "engine_versions",
                         "schema_migrations"):
            assert required in names, f"missing table {required}: got {names}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_migrations_records_version():
    """Spec G.1: schema_migrations row is written after a migration applies."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert rows == [(1,)], f"expected [(1,)], got {rows}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_migrations_is_idempotent():
    """Spec G.1: re-applying the same migrations is a no-op (no errors,
    no duplicate rows)."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        apply_migrations(db)  # second call must not raise
        apply_migrations(db)  # third call must not raise
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        assert rows == [(1,)], f"idempotency broken: {rows}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_migrations_creates_parent_directory():
    """If /var/backtest/ doesn't exist (dev / first deploy), apply_migrations
    creates it. Production already has /var/backtest as a Render disk."""
    from db_migrate import apply_migrations
    tmpdir = tempfile.mkdtemp(prefix="recorder_test_")
    try:
        nested = os.path.join(tmpdir, "deep", "nested", "dir")
        db = os.path.join(nested, "test.db")
        apply_migrations(db)
        assert os.path.exists(db), "DB file not created"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_wal_mode_enabled():
    """WAL mode is required for concurrent reads (dashboard) + writes (recorder).
    Apply_migrations must enable it."""
    from db_migrate import apply_migrations
    tmpdir, db = _fresh_db_path()
    try:
        apply_migrations(db)
        conn = sqlite3.connect(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"expected WAL, got {mode}"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    tests = [
        test_apply_migrations_creates_all_tables,
        test_apply_migrations_records_version,
        test_apply_migrations_is_idempotent,
        test_apply_migrations_creates_parent_directory,
        test_wal_mode_enabled,
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
