"""Broker-facing types and the `Broker` protocol.

These are the only shapes the rest of the app sees. `app.broker.alpaca`
implements the protocol against Alpaca's paper REST API; `app.broker.fake`
implements it in memory for tests.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class BrokerOrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    """Alpaca order states we care about, collapsed to four outcomes."""

    NEW = "new"                 # accepted, working, unfilled
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def is_working(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class Bar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ts: datetime
    o: float
    h: float
    l: float  # the bar's low; `l` matches the OHLC convention used repo-wide
    c: float
    v: float


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cash: float
    equity: float
    portfolio_value: float
    buying_power: float
    currency: str
    status: str
    crypto_status: str | None = None


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    side: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    bid: float
    ask: float
    ts: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class OrderRequest(BaseModel):
    """What we ask the broker to do. Crypto-safe subset only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    qty: float
    side: OrderSide
    type: BrokerOrderType
    time_in_force: str
    client_order_id: str
    limit_price: float | None = None
    stop_price: float | None = None


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    type: BrokerOrderType
    status: OrderStatus
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    stop_price: float | None = None
    limit_price: float | None = None


class BrokerError(RuntimeError):
    """Any broker-side failure. Carries the HTTP status when there was one."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Broker(Protocol):
    """The surface the executor and data pipeline depend on."""

    def get_account(self) -> Account: ...

    def list_positions(self) -> list[Position]: ...

    def latest_quote(self, instrument: str) -> Quote: ...

    def fetch_bars(
        self,
        instrument: str,
        granularity: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[Bar]: ...

    def submit_order(self, req: OrderRequest) -> Order: ...

    def get_order(self, order_id: str) -> Order: ...

    def list_orders(
        self,
        *,
        status: str = "open",
        after: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]: ...

    def cancel_order(self, order_id: str) -> None: ...

    def close_position(self, instrument: str) -> Order | None: ...
