"""Pydantic types for the journal."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JournalKind(StrEnum):
    TRADE_DECISION = "trade_decision"
    RISK_EVENT = "risk_event"
    NEWS_EVENT = "news_event"
    SYSTEM_EVENT = "system_event"
    EXPERIMENT_EVENT = "experiment_event"


class JournalState(StrEnum):
    DRAFT = "DRAFT"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_SKIPPED = "RISK_SKIPPED"
    APPROVED = "APPROVED"
    ORDER_SENT = "ORDER_SENT"
    ORDER_ACK = "ORDER_ACK"
    ORDER_REJECTED = "ORDER_REJECTED"
    TRADE_CLOSED = "TRADE_CLOSED"
    REVIEWED = "REVIEWED"
    NO_SIGNAL = "NO_SIGNAL"
    REVIEW_DEFERRED = "REVIEW_DEFERRED"


class JournalEntry(BaseModel):
    """An append-only journal row."""
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    ts: datetime
    kind: JournalKind
    strategy_version_id: int | None = None
    session_id: UUID
    prev_state: JournalState | None = None
    next_state: JournalState | None = None
    reason_codes: list[str] = []
    payload: dict[str, Any]
    hash: bytes | None = None
    prev_hash: bytes | None = None
