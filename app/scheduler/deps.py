"""Lazily-constructed, process-lifetime shared dependencies for scheduler jobs.

Jobs are plain no-argument `run()` functions APScheduler calls directly
(`app/scheduler/cron.py`), so dependencies can't be constructor-injected;
the (synchronous, connection-pooled) DB engines and stores are built once
here instead, mirroring the `get_settings()` cached-singleton pattern in
`app/config.py`.

`McpClientPool` is deliberately *not* cached here: each job wraps its
async body in its own `asyncio.run(...)` call (a fresh event loop every
time), and the pool's stdio streams/tasks are bound to whichever loop
created them -- reusing one across separate `asyncio.run()` calls hangs
`asyncio.run()`'s own shutdown waiting on tasks tied to an abandoned
loop. Construct a fresh `McpClientPool()` per job invocation and
`await pool.close()` it before returning (see `market_data_backfill.py`
/ `tick_live_loop.py`), matching the pattern already used in
`app/cli.py`'s `broker-ping` command.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.candle_store import SupabaseCandleStore
from app.db.client import make_admin_engine, make_journal_engine
from app.db.journal_store import SupabaseJournalStore
from app.journal.writer import JournalWriter


@lru_cache(maxsize=1)
def get_admin_engine() -> Engine:
    return make_admin_engine(get_settings().supabase_db_url)


@lru_cache(maxsize=1)
def get_journal_engine() -> Engine:
    return make_journal_engine(get_settings().supabase_journal_db_url)


@lru_cache(maxsize=1)
def get_candle_store() -> SupabaseCandleStore:
    s = get_settings()
    return SupabaseCandleStore(get_admin_engine(), s.instrument, s.granularity)


@lru_cache(maxsize=1)
def get_journal_writer() -> JournalWriter:
    store = SupabaseJournalStore(get_admin_engine(), get_journal_engine())
    return JournalWriter(store)
