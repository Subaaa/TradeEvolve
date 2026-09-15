"""LLM call accounting.

The daily token cap is enforced against these rows, not a counter in memory,
so a process restart cannot reset the day's spend.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .sqlite import utcnow_iso


def insert(
    conn: sqlite3.Connection,
    *,
    site: str,
    model: str,
    usage_input: int,
    usage_output: int,
    cache_read: int,
    latency_ms: int,
    ok: bool,
    error: str | None,
    request_id: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO llm_calls(ts, site, model, input_tokens, output_tokens,"
        " cache_read_tokens, latency_ms, ok, error, request_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            utcnow_iso(),
            site,
            model,
            usage_input,
            usage_output,
            cache_read,
            latency_ms,
            1 if ok else 0,
            error,
            request_id,
        ),
    )
    return int(cur.lastrowid or 0)


def tokens_today(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    day = (now or datetime.now(tz=UTC)).astimezone(UTC).date()
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM llm_calls"
        " WHERE substr(ts, 1, 10) = ?",
        (day.isoformat(),),
    ).fetchone()
    return int(row["n"]) if row else 0


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    )
