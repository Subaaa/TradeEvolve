"""Risk types: Proposal, AccountState, MarketState, Decision."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pinned.news_policy import NewsPolicy
from pinned.risk_limits import RiskLimits


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTD = "GTD"
    DAY = "DAY"


class ReasonCode(StrEnum):
    """Closed enum of rejection/skipped reason codes.

    Adding a new value is a human-only code change.
    """
    OK = "OK"
    RISK_EXCEEDS_DAILY_LIMIT = "RISK_EXCEEDS_DAILY_LIMIT"
    RISK_EXCEEDS_WEEKLY_LIMIT = "RISK_EXCEEDS_WEEKLY_LIMIT"
    RISK_EXCEEDS_MAX_DRAWDOWN = "RISK_EXCEEDS_MAX_DRAWDOWN"
    RISK_EXCEEDS_PER_TRADE = "RISK_EXCEEDS_PER_TRADE"
    RISK_EXCEEDS_POSITION_VALUE = "RISK_EXCEEDS_POSITION_VALUE"
    RISK_EXCEEDS_MAX_UNITS = "RISK_EXCEEDS_MAX_UNITS"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    MIN_RR_BELOW_FLOOR = "MIN_RR_BELOW_FLOOR"
    MANDATORY_STOP_LOSS_MISSING = "MANDATORY_STOP_LOSS_MISSING"
    SL_OUT_OF_RANGE = "SL_OUT_OF_RANGE"
    STALE_DATA = "STALE_DATA"
    NEWS_COOLDOWN = "NEWS_COOLDOWN"
    NEWS_NO_NEW_POSITIONS = "NEWS_NO_NEW_POSITIONS"
    CONSECUTIVE_LOSS_COOLDOWN = "CONSECUTIVE_LOSS_COOLDOWN"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    MARKET_CLOSED = "MARKET_CLOSED"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"


class Proposal(BaseModel):
    """A trade proposal produced by the StrategyRunner.

    The RiskEngine validates the proposal but does not look inside `metadata`.
    """
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    strategy_version_id: int
    instrument: str
    side: Side
    order_type: OrderType
    units: int                                       # positive integer; sign of side is separate
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    expected_rr: float = 0.0
    expected_risk_usd: float = 0.0
    bar_ts: datetime
    client_tag: str                                  # idempotency tag
    metadata: dict = Field(default_factory=dict)

    @field_validator("units")
    @classmethod
    def _units_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("units must be > 0")
        return v


class OrderSpec(BaseModel):
    """The risk-approved order; passed to the OrderGateway."""
    model_config = ConfigDict(extra="forbid")

    instrument: str
    side: Side
    order_type: OrderType
    units: int
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    tif: TimeInForce = TimeInForce.GTC
    client_tag: str
    expected_rr: float


class AccountState(BaseModel):
    """Snapshot of the paper-trading account at decision time."""
    model_config = ConfigDict(extra="forbid")

    equity: float
    equity_high: float
    day_pnl: float
    week_pnl: float
    drawdown_pct: float                             # (equity_high - equity) / equity_high
    open_positions: int
    last_n_trade_outcomes: list[Literal["win", "loss", "breakeven"]] = Field(default_factory=list)
    consecutive_losses: int = 0
    last_trade_close_ts: datetime | None = None
    is_kill_switch: bool = False


class MarketState(BaseModel):
    """The most recent market state. Pure data; built by `MarketStateBuilder`."""
    model_config = ConfigDict(extra="forbid")

    instrument: str
    granularity: str
    last_bar_ts: datetime | None
    last_close: float | None
    indicators: dict[str, float] = Field(default_factory=dict)
    seconds_since_last_bar: int = 0
    has_high_impact_in_next_30m: bool = False
    has_central_bank_in_next_2h: bool = False
    has_data_release_in_next_2h: bool = False


class Decision(BaseModel):
    """Discriminated union: approved / rejected / skipped."""
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected", "skipped"]
    reason_codes: list[ReasonCode]
    order: OrderSpec | None = None
    notes: str = ""


def evaluate_inputs_signature(  # pragma: no cover - documentation helper
    proposal: Proposal,
    state: AccountState,
    limits: RiskLimits,
    news_policy: NewsPolicy,
    market_state: MarketState,
    now: datetime,
) -> Decision:
    """Implementation lives in `app.risk.engine.evaluate`."""
    raise NotImplementedError
