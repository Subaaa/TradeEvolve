"""Append-only journal writer.

The writer uses a separate connection string (`SUPABASE_JOURNAL_DB_URL`)
that authenticates as a `journal_writer` Postgres role. That role has
`INSERT` permission on the journal tables and nothing else. There is
no `UPDATE` or `DELETE` path in code; the grants are enforced at the
DB level.

The writer maintains an in-memory cache of the most recent hash so
each new row can be chained without a SELECT.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from .hash_chain import compute_hash
from .types import JournalEntry, JournalKind, JournalState


class JournalStore(Protocol):
    def insert(self, row: JournalEntry) -> int: ...
    def last_hash_for_session(self, session_id: UUID) -> bytes | None: ...
    def list_for_session(self, session_id: UUID) -> list[JournalEntry]: ...


class JournalWriter:
    def __init__(self, store: JournalStore) -> None:
        self._store = store

    def write(
        self,
        *,
        session_id: UUID,
        kind: JournalKind,
        payload: dict[str, Any],
        strategy_version_id: int | None = None,
        prev_state: JournalState | None = None,
        next_state: JournalState | None = None,
        reason_codes: list[str] | None = None,
        ts: datetime | None = None,
    ) -> JournalEntry:
        """Append a single journal row. Returns the (un-saved-id) entry."""
        ts = ts or datetime.now(tz=timezone.utc)
        prev_hash = self._store.last_hash_for_session(session_id)
        h = compute_hash(
            ts_iso=ts.isoformat(),
            kind=kind.value,
            payload=payload,
            prev_hash=prev_hash,
        )
        entry = JournalEntry(
            id=None,
            ts=ts,
            kind=kind,
            strategy_version_id=strategy_version_id,
            session_id=session_id,
            prev_state=prev_state,
            next_state=next_state,
            reason_codes=reason_codes or [],
            payload=payload,
            hash=h,
            prev_hash=prev_hash,
        )
        new_id = self._store.insert(entry)
        entry = entry.model_copy(update={"id": new_id})
        return entry
