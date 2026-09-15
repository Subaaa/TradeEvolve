"""SQLite-backed journal store.

Implements `app.journal.writer.JournalStore`. Writes go through the normal
connection; the append-only guarantee is the pair of triggers in the schema,
not a permission on this code path.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.journal.types import JournalEntry, JournalKind, JournalState

from .sqlite import require_iso, to_iso


def _row_to_entry(r: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        id=int(r["id"]),
        ts=require_iso(r["ts"], field="ts"),
        kind=JournalKind(r["kind"]),
        strategy_version_id=(
            int(r["strategy_version_id"]) if r["strategy_version_id"] is not None else None
        ),
        session_id=str(r["session_id"]),
        prev_state=JournalState(r["prev_state"]) if r["prev_state"] else None,
        next_state=JournalState(r["next_state"]) if r["next_state"] else None,
        reason_codes=list(json.loads(r["reason_codes"])),
        payload=dict(json.loads(r["payload"])),
        hash=bytes(r["hash"]) if r["hash"] is not None else None,
        prev_hash=bytes(r["prev_hash"]) if r["prev_hash"] is not None else None,
    )


class SqliteJournalStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, row: JournalEntry) -> int:
        cur = self._conn.execute(
            "INSERT INTO journal_entries"
            "(ts, kind, strategy_version_id, session_id, prev_state, next_state,"
            " reason_codes, payload, hash, prev_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(row.ts),
                row.kind.value,
                row.strategy_version_id,
                row.session_id,
                row.prev_state.value if row.prev_state else None,
                row.next_state.value if row.next_state else None,
                json.dumps(row.reason_codes),
                json.dumps(row.payload, sort_keys=True, separators=(",", ":"), default=str),
                row.hash,
                row.prev_hash,
            ),
        )
        return int(cur.lastrowid or 0)

    def last_hash_for_session(self, session_id: str) -> bytes | None:
        r = self._conn.execute(
            "SELECT hash FROM journal_entries WHERE session_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if r is None or r["hash"] is None:
            return None
        return bytes(r["hash"])

    def list_for_session(self, session_id: str) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT * FROM journal_entries WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_sessions(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM journal_entries ORDER BY session_id"
        ).fetchall()
        return [str(r["session_id"]) for r in rows]

    def list_recent(
        self, *, limit: int = 50, kind: JournalKind | None = None
    ) -> list[JournalEntry]:
        sql = "SELECT * FROM journal_entries"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind.value)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def count_by_reason_since(self, since_iso: str) -> dict[str, int]:
        """Histogram of reason codes on trade decisions since `since_iso`."""
        rows = self._conn.execute(
            "SELECT reason_codes FROM journal_entries"
            " WHERE kind = ? AND ts >= ?",
            (JournalKind.TRADE_DECISION.value, since_iso),
        ).fetchall()
        out: dict[str, int] = {}
        for r in rows:
            for code in json.loads(r["reason_codes"]):
                out[code] = out.get(code, 0) + 1
        return out
