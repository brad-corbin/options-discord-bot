"""Boot-time SQLite migration runner.

# v11.7 (Patch G.1): runs every SQL file in migrations/ in order, tracks
# applied versions in schema_migrations, idempotent across restarts.

Usage:
    from db_migrate import apply_migrations
    apply_migrations("/var/backtest/desk.db")

The runner:
  * Creates the parent directory if missing.
  * Opens the DB in WAL mode (concurrent reads/writes).
  * Reads migrations/NNNN_*.sql in numerical order.
  * Skips files whose version is already in schema_migrations.
  * Wraps each migration in a transaction.
  * Writes a schema_migrations row on success.

Migration files are named `migrations/NNNN_description.sql` where NNNN is
a 4-digit zero-padded version. Migrations always go forward — no down
scripts in V1. Schema changes that break the recorder require a new
migration file and a producer-version bump elsewhere.
"""
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import List, Tuple

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_RE = re.compile(r"^(\d{4})_.*\.sql$")


def _list_migration_files() -> List[Tuple[int, Path]]:
    """Returns sorted [(version, path), ...] for all migrations."""
    out = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if not entry.is_file():
            continue
        m = _VERSION_RE.match(entry.name)
        if not m:
            continue
        out.append((int(m.group(1)), entry))
    return sorted(out, key=lambda x: x[0])


def _ensure_parent_dir(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _open_with_wal(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_migrations(db_path: str) -> None:
    """Apply every pending migration in migrations/ in order. Idempotent."""
    _ensure_parent_dir(db_path)
    conn = _open_with_wal(db_path)
    try:
        _ensure_schema_migrations(conn)
        already = _applied_versions(conn)
        for version, sql_path in _list_migration_files():
            if version in already:
                continue
            sql = sql_path.read_text(encoding="utf-8")
            log.info(f"db_migrate: applying {sql_path.name}")
            try:
                # executescript() auto-commits before running; conn.rollback()
                # below only covers any DML issued after executescript returns.
                # All V1 migrations are pure DDL with IF NOT EXISTS so partial
                # runs are safe.
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, int(time.time() * 1_000_000)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    path = sys.argv[1] if len(sys.argv) > 1 else "/var/backtest/desk.db"
    apply_migrations(path)
    print(f"Migrations applied to {path}")
