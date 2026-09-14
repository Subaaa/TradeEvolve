"""Pydantic schemas for the Alpaca MCP tool inputs/outputs."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GetInstrumentsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetInstrumentsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruments: list[dict]


class GetCandlesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument: str
    granularity: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D", "W"]
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    count: int | None = Field(default=None, ge=1, le=5000)


class GetCandlesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candles: list[dict]


class GetLatestPriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument: str


class GetLatestPriceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument: str
    bid: float
    ask: float
    time: str


class GetAccountSummaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetAccountSummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: dict


class GetOpenPositionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetOpenPositionsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positions: list[dict]


class PlaceOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument: str
    qty: float
    side: Literal["buy", "sell"]
    client_order_id: str = ""


class PlaceOrderOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    client_order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float | None = None


class CancelOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str


class CancelOrderOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    status: str
