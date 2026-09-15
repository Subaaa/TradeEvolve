"""Append-only journal writer.

Append-only used to be enforced by a dedicated Postgres role with INSERT-only
grants. On SQLite it is enforced by two triggers that abort any UPDATE or
DELETE on `journal_entries`, which is the same guarantee with nothing to
misconfigure at deploy time.

Each row is chained to the previous row of the same session, so tampering
with any row breaks every hash after it. The hourly audit job walks the chain.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from .hash_chain import compute_hash
from .payloads import as_payload
from .types import JournalEntry, JournalKind, JournalState


class JournalStore(Protocol):
    def insert(self, row: JournalEntry) -> int: ...

    def last_hash_for_session(self, session_id: str) -> bytes | None: ...

    def list_for_session(self, session_id: str) -> list[JournalEntry]: ...


class JournalWriter:
    def __init__(self, store: JournalStore, *, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def write(
        self,
        *,
        kind: JournalKind,
        payload: BaseModel | dict[str, Any],
        strategy_version_id: int | None = None,
        prev_state: JournalState | None = None,
        next_state: JournalState | None = None,
        reason_codes: list[str] | None = None,
        ts: datetime | None = None,
    ) -> JournalEntry:
        """Append a single journal row and return it with its assigned id."""
        ts = ts or datetime.now(tz=UTC)
        body = as_payload(payload) if isinstance(payload, BaseModel) else payload
        prev_hash = self._store.last_hash_for_session(self._session_id)
        h = compute_hash(
            ts_iso=ts.isoformat(),
            kind=kind.value,
            payload=body,
            prev_hash=prev_hash,
        )
        entry = JournalEntry(
            id=None,
            ts=ts,
            kind=kind,
            strategy_version_id=strategy_version_id,
            session_id=self._session_id,
            prev_state=prev_state,
            next_state=next_state,
            reason_codes=reason_codes or [],
            payload=body,
            hash=h,
            prev_hash=prev_hash,
        )
        new_id = self._store.insert(entry)
        return entry.model_copy(update={"id": new_id})
