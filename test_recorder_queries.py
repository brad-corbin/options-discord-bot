"""Smoke test that all queries in recorder_queries.sql parse and execute
against an empty DB. Patch G.10."""
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path


def _split_queries(sql_text: str):
    """Split on ';' at end of statement (naive but adequate — none of our
    queries contain inline ';' in literals).

    A 'real' statement is one that contains at least one non-comment,
    non-blank line. Pure file-header comment blocks are skipped.
    """
    parts = []
    buf = []
    for line in sql_text.splitlines():
        s = line.strip()
        if s.startswith("--") or not s:
            buf.append(line)
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).strip()
            # Keep only if there is at least one non-comment, non-blank line
            has_sql = any(
                ln.strip() and not ln.strip().startswith("--")
                for ln in buf
            )
            if stmt and has_sql:
                parts.append(stmt)
            buf = []
    return parts


def test_all_queries_parse_and_execute():
    sql_path = Path(__file__).parent / "recorder_queries.sql"
    sql_text = sql_path.read_text(encoding="utf-8")
    statements = _split_queries(sql_text)
    assert len(statements) >= 10, (
        f"expected >= 10 verification queries, got {len(statements)}"
    )

    tmpdir = tempfile.mkdtemp(prefix="recorder_g10_")
    db = os.path.join(tmpdir, "desk.db")
    try:
        from db_migrate import apply_migrations
        apply_migrations(db)
        conn = sqlite3.connect(db)
        for stmt in statements:
            try:
                conn.execute(stmt).fetchall()
            except sqlite3.Error as e:
                raise AssertionError(
                    f"Query failed:\n{stmt[:200]}\n... -> {e}"
                )
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_all_queries_parse_and_execute()
        print("PASS: test_all_queries_parse_and_execute")
        print("\n1/1 passed")
    except Exception as e:
        print(f"FAIL: {e}")
