"""Pydantic types for the journal."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class JournalKind(StrEnum):
    TRADE_DECISION = "trade_decision"
    TRADE_OPENED = "trade_opened"
    TRADE_CLOSED = "trade_closed"
    RISK_EVENT = "risk_event"
    SYSTEM_EVENT = "system_event"
    EXPERIMENT_EVENT = "experiment_event"
    LLM_EVENT = "llm_event"


class JournalState(StrEnum):
    DRAFT = "DRAFT"
    NO_SIGNAL = "NO_SIGNAL"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_SKIPPED = "RISK_SKIPPED"
    APPROVED = "APPROVED"
    ORDER_SENT = "ORDER_SENT"
    ORDER_REJECTED = "ORDER_REJECTED"
    OPEN_UNPROTECTED = "OPEN_UNPROTECTED"
    OPEN_PROTECTED = "OPEN_PROTECTED"
    EXIT_SENT = "EXIT_SENT"
    TRADE_CLOSED = "TRADE_CLOSED"
    REVIEWED = "REVIEWED"


class JournalEntry(BaseModel):
    """An append-only journal row."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    ts: datetime
    kind: JournalKind
    strategy_version_id: int | None = None
    session_id: str
    prev_state: JournalState | None = None
    next_state: JournalState | None = None
    reason_codes: list[str] = []
    payload: dict[str, Any]
    hash: bytes | None = None
    prev_hash: bytes | None = None
