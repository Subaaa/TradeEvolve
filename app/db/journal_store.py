"""Postgres-backed `JournalStore` (the `journal_entries` table).

`insert()` goes through the `journal_writer`-role engine (INSERT-only,
enforced at the DB level -- see `app/db/grants.sql`). The two read
methods go through the admin engine instead: `journal_writer` has no
broad `SELECT` by design, and `JournalWriter` (app/journal/writer.py)
only calls these on a cache miss (first write of a session / after a
process restart), not on every write.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.journal.types import JournalEntry, JournalKind, JournalState


class SupabaseJournalStore:
    def __init__(self, admin_engine: Engine, journal_engine: Engine) -> None:
        self._admin = admin_engine
        self._journal = journal_engine

    def insert(self, row: JournalEntry) -> int:
        with self._journal.begin() as conn:
            result = conn.execute(
                text(
                    """
                    insert into journal_entries
                        (ts, kind, strategy_version_id, session_id, prev_state,
                         next_state, reason_codes, payload, hash, prev_hash)
                    values
                        (:ts, :kind, :strategy_version_id, :session_id, :prev_state,
                         :next_state, :reason_codes, cast(:payload as jsonb), :hash, :prev_hash)
                    returning id
                    """
                ),
                {
                    "ts": row.ts,
                    "kind": row.kind.value,
                    "strategy_version_id": row.strategy_version_id,
                    "session_id": row.session_id,
                    "prev_state": row.prev_state.value if row.prev_state else None,
                    "next_state": row.next_state.value if row.next_state else None,
                    "reason_codes": row.reason_codes,
                    "payload": json.dumps(row.payload, default=str),
                    "hash": row.hash,
                    "prev_hash": row.prev_hash,
                },
            )
            return result.scalar_one()

    def last_hash_for_session(self, session_id: UUID) -> bytes | None:
        with self._admin.connect() as conn:
            h = conn.execute(
                text(
                    "select hash from journal_entries where session_id = :sid "
                    "order by id desc limit 1"
                ),
                {"sid": session_id},
            ).scalar()
        return bytes(h) if h is not None else None

    def list_for_session(self, session_id: UUID) -> list[JournalEntry]:
        with self._admin.connect() as conn:
            rows = conn.execute(
                text(
                    "select id, ts, kind, strategy_version_id, session_id, prev_state, "
                    "next_state, reason_codes, payload, hash, prev_hash "
                    "from journal_entries where session_id = :sid order by id asc"
                ),
                {"sid": session_id},
            ).mappings().all()
        return [_row_to_entry(r) for r in rows]


def _row_to_entry(r: Any) -> JournalEntry:
    payload = r["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return JournalEntry(
        id=r["id"],
        ts=r["ts"],
        kind=JournalKind(r["kind"]),
        strategy_version_id=r["strategy_version_id"],
        session_id=r["session_id"],
        prev_state=JournalState(r["prev_state"]) if r["prev_state"] else None,
        next_state=JournalState(r["next_state"]) if r["next_state"] else None,
        reason_codes=list(r["reason_codes"] or []),
        payload=payload,
        hash=bytes(r["hash"]) if r["hash"] is not None else None,
        prev_hash=bytes(r["prev_hash"]) if r["prev_hash"] is not None else None,
    )
