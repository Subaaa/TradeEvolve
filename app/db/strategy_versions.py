"""Strategy version storage and the champion pointer.

The champion is a database row, not a file. The live loop reads it on every
tick, which is what makes a promotion take effect without a restart and what
makes "which config produced this trade" answerable after the fact.

A partial unique index in the schema guarantees at most one champion row
exists at any time, so a bug in the promotion transaction fails loudly rather
than leaving the bot trading two strategies at once.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.strategy.configs import StrategyConfig

from .sqlite import require_iso, transaction, utcnow_iso


class VersionStatus(StrEnum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    SHADOW = "shadow"
    RETIRED = "retired"
    REJECTED = "rejected"


class StrategyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    version_tag: str
    parent_version_id: int | None
    created_at: datetime
    created_by: str
    config: StrategyConfig
    config_hash: str
    status: VersionStatus


def _row(r: sqlite3.Row) -> StrategyVersion:
    return StrategyVersion(
        id=int(r["id"]),
        version_tag=str(r["version_tag"]),
        parent_version_id=(
            int(r["parent_version_id"]) if r["parent_version_id"] is not None else None
        ),
        created_at=require_iso(r["created_at"], field="created_at"),
        created_by=str(r["created_by"]),
        config=StrategyConfig.from_json_str(str(r["config"])),
        config_hash=str(r["config_hash"]),
        status=VersionStatus(r["status"]),
    )


def insert_version(
    conn: sqlite3.Connection,
    config: StrategyConfig,
    *,
    status: VersionStatus,
    created_by: str,
    parent_version_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO strategy_versions"
        "(version_tag, parent_version_id, created_at, created_by, config, config_hash, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            config.version_tag,
            parent_version_id,
            utcnow_iso(),
            created_by,
            config.to_json_str(),
            config.canonical_hash_hex(),
            status.value,
        ),
    )
    return int(cur.lastrowid or 0)


def get(conn: sqlite3.Connection, version_id: int) -> StrategyVersion | None:
    r = conn.execute(
        "SELECT * FROM strategy_versions WHERE id = ?", (version_id,)
    ).fetchone()
    return _row(r) if r else None


def get_by_tag(conn: sqlite3.Connection, version_tag: str) -> StrategyVersion | None:
    r = conn.execute(
        "SELECT * FROM strategy_versions WHERE version_tag = ?", (version_tag,)
    ).fetchone()
    return _row(r) if r else None


def load_champion(conn: sqlite3.Connection) -> StrategyVersion | None:
    r = conn.execute(
        "SELECT * FROM strategy_versions WHERE status = ? LIMIT 1",
        (VersionStatus.CHAMPION.value,),
    ).fetchone()
    return _row(r) if r else None


def load_shadow(conn: sqlite3.Connection) -> StrategyVersion | None:
    r = conn.execute(
        "SELECT * FROM strategy_versions WHERE status = ? ORDER BY id DESC LIMIT 1",
        (VersionStatus.SHADOW.value,),
    ).fetchone()
    return _row(r) if r else None


def set_status(conn: sqlite3.Connection, version_id: int, status: VersionStatus) -> None:
    conn.execute(
        "UPDATE strategy_versions SET status = ? WHERE id = ?", (status.value, version_id)
    )


def ensure_champion(
    conn: sqlite3.Connection, config: StrategyConfig, *, created_by: str = "human"
) -> StrategyVersion:
    """Return the champion, bootstrapping it from `config` if there is none."""
    existing = load_champion(conn)
    if existing is not None:
        return existing
    by_tag = get_by_tag(conn, config.version_tag)
    if by_tag is not None:
        set_status(conn, by_tag.id, VersionStatus.CHAMPION)
        promoted = get(conn, by_tag.id)
        assert promoted is not None
        return promoted
    new_id = insert_version(
        conn, config, status=VersionStatus.CHAMPION, created_by=created_by
    )
    created = get(conn, new_id)
    assert created is not None
    return created


def promote(conn: sqlite3.Connection, *, challenger_id: int) -> None:
    """Retire the current champion and promote `challenger_id` in one step.

    Both writes happen inside a single transaction: the unique index allows
    exactly one champion, so doing this in two statements without a
    transaction would either fail or leave the system with none.
    """
    with transaction(conn):
        current = load_champion(conn)
        if current is not None:
            conn.execute(
                "UPDATE strategy_versions SET status = ? WHERE id = ?",
                (VersionStatus.RETIRED.value, current.id),
            )
        conn.execute(
            "UPDATE strategy_versions SET status = ?, parent_version_id ="
            " COALESCE(parent_version_id, ?) WHERE id = ?",
            (
                VersionStatus.CHAMPION.value,
                current.id if current else None,
                challenger_id,
            ),
        )


def lineage(conn: sqlite3.Connection, *, limit: int = 50) -> list[StrategyVersion]:
    rows = conn.execute(
        "SELECT * FROM strategy_versions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row(r) for r in rows]
