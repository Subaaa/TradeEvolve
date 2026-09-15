"""Shared, lazily-built dependencies for scheduled jobs.

One process, one database connection, one broker client, one session id. The
previous version deliberately refused to cache the broker client and spawned a
fresh subprocess for every tick — 182 process launches in two and a half
hours, about 2.5 seconds of startup latency on every trading decision. There
is no subprocess now, so there is nothing to amortise: the client is a plain
HTTP session held open for the life of the process.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from functools import lru_cache

from app.broker.alpaca import AlpacaClient, build_client
from app.config import Settings, get_settings
from app.db.journal_store import SqliteJournalStore
from app.db.sqlite import open_db
from app.execution.position_manager import PositionManager
from app.journal.writer import JournalWriter

logger = logging.getLogger(__name__)

SESSION_ID: str = str(uuid.uuid4())


@lru_cache(maxsize=1)
def settings() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def db() -> sqlite3.Connection:
    s = settings()
    logger.info("opening database at %s", s.db_path)
    return open_db(s.db_path)


@lru_cache(maxsize=1)
def broker() -> AlpacaClient:
    return build_client(settings())


@lru_cache(maxsize=1)
def journal() -> JournalWriter:
    return JournalWriter(SqliteJournalStore(db()), session_id=SESSION_ID)


@lru_cache(maxsize=1)
def position_manager() -> PositionManager:
    s = settings()
    return PositionManager(
        broker=broker(),
        conn=db(),
        journal=journal(),
        instrument=s.instrument,
        granularity=s.granularity,
    )


def reset() -> None:
    """Drop every cached dependency. Tests and shutdown only."""
    for f in (settings, db, broker, journal, position_manager):
        f.cache_clear()
