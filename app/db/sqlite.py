"""SQLite connection and migrations.

One file, WAL mode, foreign keys on. This replaces the two Postgres roles and
two connection pools the project used to carry: the append-only guarantee the
`journal_writer` role provided is now a pair of triggers, which is both
simpler and impossible to misconfigure at deploy time.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def to_iso(dt: datetime) -> str:
    """Serialize a datetime as a tz-aware UTC ISO string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def require_iso(text: str | None, *, field: str) -> datetime:
    dt = from_iso(text)
    if dt is None:
        raise ValueError(f"expected a timestamp in column {field!r}")
    return dt


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the settings the rest of the app assumes."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        detect_types=0,
        isolation_level=None,  # autocommit; explicit transactions via `transaction()`
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply every migration file in order. Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {
        str(r["name"]) for r in conn.execute("SELECT name FROM schema_migrations")
    }
    for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in applied:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (sql_file.name, utcnow_iso()),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_db(path: Path | str) -> sqlite3.Connection:
    """`connect` plus `migrate`, the normal entry point."""
    conn = connect(path)
    migrate(conn)
    return conn


def backup_to(conn: sqlite3.Connection, dest: Path) -> Path:
    """Consistent online backup of the live database."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest
