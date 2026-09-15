"""Risk types: Proposal, AccountState, MarketState, Decision.

`AccountState` is the important one. Every discipline gate in the engine is a
pure comparison against a field here, which means the gates can only work if
something actually keeps these fields current. That something is
`app.risk.account_state.build_from_db`, rebuilt from the `trades` table on
every tick — not incremented in memory, where a restart would quietly reset
the daily loss counter to zero.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"


class ExitReason(StrEnum):
    SL = "sl"
    TP = "tp"
    TIME_STOP = "time_stop"
    HARD_TIME_CAP = "hard_time_cap"
    KILL_SWITCH = "kill_switch"
    SL_FAILSAFE = "sl_failsafe"
    WEEKEND = "weekend"
    MANUAL = "manual"
    UNPROTECTED = "unprotected"
    ORPHAN = "orphan"
    RECONCILED = "reconciled"


class ReasonCode(StrEnum):
    """Closed enum of approval/rejection reason codes.

    Adding a value is a human-only code change.
    """

    OK = "OK"

    # --- hard brakes ---
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    RISK_EXCEEDS_DAILY_LIMIT = "RISK_EXCEEDS_DAILY_LIMIT"
    RISK_EXCEEDS_WEEKLY_LIMIT = "RISK_EXCEEDS_WEEKLY_LIMIT"
    RISK_EXCEEDS_MAX_DRAWDOWN = "RISK_EXCEEDS_MAX_DRAWDOWN"

    # --- discipline ---
    MARKET_CLOSED = "MARKET_CLOSED"
    MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY"
    MAX_LOSSES_PER_DAY = "MAX_LOSSES_PER_DAY"
    CONSECUTIVE_LOSS_COOLDOWN = "CONSECUTIVE_LOSS_COOLDOWN"
    STOP_OUT_COOLDOWN = "STOP_OUT_COOLDOWN"
    REENTRY_AFTER_LOSS_COOLDOWN = "REENTRY_AFTER_LOSS_COOLDOWN"
    MIN_BARS_BETWEEN_ENTRIES = "MIN_BARS_BETWEEN_ENTRIES"
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    NO_ADD_TO_POSITION = "NO_ADD_TO_POSITION"
    SHORT_NOT_SUPPORTED = "SHORT_NOT_SUPPORTED"

    # --- data ---
    STALE_DATA = "STALE_DATA"

    # --- trade shape ---
    MANDATORY_STOP_LOSS_MISSING = "MANDATORY_STOP_LOSS_MISSING"
    SL_TOO_TIGHT_FOR_COSTS = "SL_TOO_TIGHT_FOR_COSTS"
    SL_OUT_OF_RANGE = "SL_OUT_OF_RANGE"
    MIN_RR_BELOW_FLOOR = "MIN_RR_BELOW_FLOOR"
    RISK_EXCEEDS_PER_TRADE = "RISK_EXCEEDS_PER_TRADE"
    RISK_EXCEEDS_POSITION_VALUE = "RISK_EXCEEDS_POSITION_VALUE"
    RISK_EXCEEDS_MAX_UNITS = "RISK_EXCEEDS_MAX_UNITS"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"

    # --- execution-quality events (journaled, never a gate) ---
    ENTRY_SLIPPAGE_EXCEEDED = "ENTRY_SLIPPAGE_EXCEEDED"
    UNPROTECTED_POSITION_FLATTENED = "UNPROTECTED_POSITION_FLATTENED"
    STOP_ORDER_REPLACED = "STOP_ORDER_REPLACED"
    ORPHAN_POSITION = "ORPHAN_POSITION"


class Proposal(BaseModel):
    """A trade proposal produced by the StrategyRunner."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    strategy_version_id: int
    instrument: str
    side: Side
    order_type: OrderType
    units: float
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    expected_rr: float = 0.0
    expected_risk_usd: float = 0.0
    bar_ts: datetime
    client_tag: str
    indicators: dict[str, float] = Field(default_factory=dict)

    @field_validator("units")
    @classmethod
    def _units_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("units must be > 0")
        return v


class OrderSpec(BaseModel):
    """The risk-approved order, handed to the PositionManager."""

    model_config = ConfigDict(extra="forbid")

    instrument: str
    side: Side
    order_type: OrderType
    units: float
    entry_price: float
    stop_loss: float
    take_profit: float
    tif: TimeInForce = TimeInForce.IOC
    client_tag: str
    expected_rr: float
    bar_ts: datetime
    strategy_version_id: int
    indicators: dict[str, float] = Field(default_factory=dict)

    @property
    def sl_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)


class AccountState(BaseModel):
    """Snapshot of the paper account and today's trading history."""

    model_config = ConfigDict(extra="forbid")

    equity: float
    equity_high: float
    day_start_equity: float
    week_start_equity: float
    day_pnl: float = 0.0
    week_pnl: float = 0.0
    drawdown_pct: float = 0.0
    open_positions: int = 0

    # --- discipline counters, all rebuilt from the trades table ---
    trades_today: int = 0
    losses_today: int = 0
    consecutive_losses: int = 0
    bars_since_last_entry: int | None = None
    bars_since_last_exit: int | None = None
    last_exit_reason: ExitReason | None = None
    last_exit_was_loss: bool = False
    last_exit_side: Side | None = None
    last_trade_close_ts: datetime | None = None

    is_weekend: bool = False
    is_kill_switch: bool = False
    kill_switch_reason: str = ""


class MarketState(BaseModel):
    """The most recent market state. Pure data."""

    model_config = ConfigDict(extra="forbid")

    instrument: str
    granularity: str
    last_bar_ts: datetime | None = None
    last_close: float | None = None
    indicators: dict[str, float] = Field(default_factory=dict)
    seconds_since_last_bar: int = 0


class Decision(BaseModel):
    """approved / rejected / skipped.

    `rejected` means the proposal was wrong or a hard brake fired;
    `skipped` means the setup was fine but discipline said not now.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected", "skipped"]
    reason_codes: list[ReasonCode]
    order: OrderSpec | None = None
    notes: str = ""

    @property
    def approved(self) -> bool:
        return self.decision == "approved" and self.order is not None
