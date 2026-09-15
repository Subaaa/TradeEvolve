"""Weekly reviews, hypotheses and experiments.

These three tables are the record of how the strategy got to where it is: what
the model saw, what it proposed, what the gates said, and what was actually
promoted. Without them a champion's parameters are just numbers nobody can
account for.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .sqlite import from_iso, require_iso, to_iso, utcnow_iso


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    weekly_review_id: int | None
    proposed_at: datetime
    champion_id: int
    field: str
    from_value: float
    to_value: float
    reason: str | None
    accepted: bool
    reject_reason: str | None


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    hypothesis_id: int | None
    champion_id: int
    challenger_id: int
    created_at: datetime
    shadow_started_at: datetime | None
    phases: dict[str, Any] = Field(default_factory=dict)
    final_decision: str | None
    reason_codes: list[str] = Field(default_factory=list)
    decided_at: datetime | None


# ------------------------------------------------------------ weekly reviews


def insert_weekly_review(
    conn: sqlite3.Connection,
    *,
    period_start: datetime,
    period_end: datetime,
    payload_in: dict[str, Any],
    payload_out: dict[str, Any],
) -> int:
    cur = conn.execute(
        "INSERT INTO weekly_reviews(period_start, period_end, input, output)"
        " VALUES (?, ?, ?, ?)",
        (
            to_iso(period_start),
            to_iso(period_end),
            json.dumps(payload_in, default=str),
            json.dumps(payload_out, default=str),
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------- hypotheses


def insert_hypothesis(
    conn: sqlite3.Connection,
    *,
    weekly_review_id: int | None,
    champion_id: int,
    field: str,
    from_value: float,
    to_value: float,
    reason: str,
    accepted: bool,
    reject_reason: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO hypotheses(weekly_review_id, proposed_at, champion_id, field,"
        " from_value, to_value, reason, accepted, reject_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            weekly_review_id,
            utcnow_iso(),
            champion_id,
            field,
            json.dumps(from_value),
            json.dumps(to_value),
            reason,
            1 if accepted else 0,
            reject_reason,
        ),
    )
    return int(cur.lastrowid or 0)


def _hypothesis(r: sqlite3.Row) -> Hypothesis:
    return Hypothesis(
        id=int(r["id"]),
        weekly_review_id=(
            int(r["weekly_review_id"]) if r["weekly_review_id"] is not None else None
        ),
        proposed_at=require_iso(r["proposed_at"], field="proposed_at"),
        champion_id=int(r["champion_id"]),
        field=str(r["field"]),
        from_value=float(json.loads(r["from_value"])),
        to_value=float(json.loads(r["to_value"])),
        reason=r["reason"],
        accepted=bool(r["accepted"]),
        reject_reason=r["reject_reason"],
    )


def hypotheses_for_champion(
    conn: sqlite3.Connection, champion_id: int, *, limit: int = 20
) -> list[Hypothesis]:
    rows = conn.execute(
        "SELECT * FROM hypotheses WHERE champion_id = ? ORDER BY id DESC LIMIT ?",
        (champion_id, limit),
    ).fetchall()
    return [_hypothesis(r) for r in rows]


def failed_attempts_for_champion(conn: sqlite3.Connection, champion_id: int) -> int:
    """Hypotheses against this champion that did not end in a promotion."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM hypotheses h"
        " LEFT JOIN experiments e ON e.hypothesis_id = h.id"
        " WHERE h.champion_id = ?"
        "   AND (h.accepted = 0 OR e.final_decision IN ('rejected', 'deferred'))",
        (champion_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------- experiments


def insert_experiment(
    conn: sqlite3.Connection,
    *,
    hypothesis_id: int | None,
    champion_id: int,
    challenger_id: int,
    phases: dict[str, Any],
    shadow_started_at: datetime | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO experiments(hypothesis_id, champion_id, challenger_id, created_at,"
        " shadow_started_at, phases) VALUES (?,?,?,?,?,?)",
        (
            hypothesis_id,
            champion_id,
            challenger_id,
            utcnow_iso(),
            to_iso(shadow_started_at) if shadow_started_at else None,
            json.dumps(phases, default=str),
        ),
    )
    return int(cur.lastrowid or 0)


def _experiment(r: sqlite3.Row) -> Experiment:
    return Experiment(
        id=int(r["id"]),
        hypothesis_id=int(r["hypothesis_id"]) if r["hypothesis_id"] is not None else None,
        champion_id=int(r["champion_id"]),
        challenger_id=int(r["challenger_id"]),
        created_at=require_iso(r["created_at"], field="created_at"),
        shadow_started_at=from_iso(r["shadow_started_at"]),
        phases=dict(json.loads(r["phases"] or "{}")),
        final_decision=r["final_decision"],
        reason_codes=list(json.loads(r["reason_codes"] or "[]")),
        decided_at=from_iso(r["decided_at"]),
    )


def active_experiment(conn: sqlite3.Connection) -> Experiment | None:
    r = conn.execute(
        "SELECT * FROM experiments WHERE final_decision IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _experiment(r) if r else None


def get_experiment(conn: sqlite3.Connection, experiment_id: int) -> Experiment | None:
    r = conn.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    return _experiment(r) if r else None


def update_phases(
    conn: sqlite3.Connection, experiment_id: int, phases: dict[str, Any]
) -> None:
    conn.execute(
        "UPDATE experiments SET phases = ? WHERE id = ?",
        (json.dumps(phases, default=str), experiment_id),
    )


def decide(
    conn: sqlite3.Connection,
    experiment_id: int,
    *,
    decision: str,
    reason_codes: list[str],
) -> None:
    conn.execute(
        "UPDATE experiments SET final_decision = ?, reason_codes = ?, decided_at = ?"
        " WHERE id = ?",
        (decision, json.dumps(reason_codes), utcnow_iso(), experiment_id),
    )


def list_recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[Experiment]:
    rows = conn.execute(
        "SELECT * FROM experiments ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_experiment(r) for r in rows]
