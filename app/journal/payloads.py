"""Typed journal payloads.

The journal used to carry free-form dicts, which made it append-only but not
actually readable: an LLM reviewing it could see that a proposal happened and
nothing about why, or what became of it. These models are the contract for
what every trade must record.

Each model is `extra="forbid"` so a typo becomes a test failure rather than a
field that silently never gets written.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.risk.types import ExitReason, ReasonCode, Side


class AccountSnapshot(BaseModel):
    """The discipline counters as they stood at decision time."""

    model_config = ConfigDict(extra="forbid")

    equity: float
    day_start_equity: float
    day_pnl: float
    week_pnl: float
    drawdown_pct: float
    trades_today: int
    losses_today: int
    consecutive_losses: int
    open_positions: int
    bars_since_last_entry: int | None = None
    bars_since_last_exit: int | None = None
    last_exit_reason: ExitReason | None = None


class TradeDecisionPayload(BaseModel):
    """Every tick that produced a signal, approved or not.

    A rejected decision is as valuable as an accepted one: the weekly review
    reads the reason-code histogram to see which rule is actually binding.
    """

    model_config = ConfigDict(extra="forbid")

    bar_ts: datetime
    decision: str
    reason_codes: list[ReasonCode]
    client_tag: str | None = None
    side: Side | None = None
    units: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    expected_rr: float | None = None
    expected_risk_usd: float | None = None
    indicators: dict[str, float] = Field(default_factory=dict)
    account: AccountSnapshot
    notes: str = ""


class NoSignalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bar_ts: datetime
    reason: str = "no_signal"
    indicators: dict[str, float] = Field(default_factory=dict)


class TradeOpenedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_id: int
    client_tag: str
    bar_ts: datetime
    side: Side
    units: float
    decision_close: float
    entry_fill_price: float
    entry_slippage_bps: float
    stop_loss: float
    take_profit: float
    sl_distance: float
    entry_order_id: str
    sl_order_id: str | None
    protected: bool
    config_hash: str
    indicators: dict[str, float] = Field(default_factory=dict)


class TradeClosedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_id: int
    client_tag: str
    side: Side
    units: float
    opened_at: datetime
    closed_at: datetime
    entry_fill_price: float
    exit_price: float
    exit_reason: ExitReason
    exit_order_id: str | None
    pnl_usd: float
    pnl_r: float
    fees_usd: float
    fees_estimated: bool
    mfe: float
    mae: float
    mfe_r: float
    mae_r: float
    bars_held: int
    stop_loss: float
    take_profit: float
    indicators: dict[str, float] = Field(default_factory=dict)


class RiskEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: ReasonCode
    detail: str
    trade_id: int | None = None
    client_tag: str | None = None
    value: float | None = None
    action: str = ""


class SystemEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class LlmEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str
    model: str
    ok: bool
    input_tokens: int = 0
    output_tokens: int = 0
    summary: str = ""
    error: str | None = None


class ExperimentEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    experiment_id: int | None = None
    champion_id: int | None = None
    challenger_id: int | None = None
    decision: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


def as_payload(model: BaseModel) -> dict[str, Any]:
    """Serialize a payload model for the journal's canonical JSON."""
    return model.model_dump(mode="json")
