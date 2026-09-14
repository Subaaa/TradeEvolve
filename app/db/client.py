"""Database client.

Two connection strings:
- `SUPABASE_DB_URL`: service role; for reads and non-journal writes.
- `SUPABASE_JOURNAL_DB_URL`: a custom `journal_writer` role; for
  `INSERT`-only on the journal tables. Used by `JournalWriter`.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def _with_psycopg_driver(url: str) -> str:
    """Force the `psycopg` (v3) driver -- SQLAlchemy's default `postgresql://`
    dialect assumes `psycopg2`, which this project does not depend on.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_admin_engine(url: str) -> Engine:
    return create_engine(
        _with_psycopg_driver(url), pool_pre_ping=True, pool_size=5, max_overflow=10, future=True
    )


def make_journal_engine(url: str) -> Engine:
    return create_engine(
        _with_psycopg_driver(url), pool_pre_ping=True, pool_size=2, max_overflow=5, future=True
    )


@contextmanager
def session(engine: Engine) -> Iterator:
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        yield s
