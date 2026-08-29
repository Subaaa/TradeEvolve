"""Thin wrapper over `oandapyV20`.

The MCP server is the only component that knows the OANDA token.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import oandapyV20  # type: ignore
import oandapyV20.endpoints.accounts as accounts  # type: ignore
import oandapyV20.endpoints.instruments as instruments  # type: ignore
import oandapyV20.endpoints.orders as orders  # type: ignore
import oandapyV20.endpoints.pricing as pricing  # type: ignore
import oandapyV20.endpoints.trades as trades  # type: ignore

from .config import OandaMcpConfig


class OandaClient:
    def __init__(self, cfg: OandaMcpConfig) -> None:
        if not cfg.api_key or not cfg.account_id:
            raise RuntimeError("OANDA credentials not set in MCP subprocess env")
        self._cfg = cfg
        self._client = oandapyV20.API(
            environment=cfg.environment,
            access_token=cfg.api_key,
        )
        self._account_id = cfg.account_id

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def environment(self) -> str:
        return self._cfg.environment

    def list_instruments(self) -> list[dict[str, Any]]:
        r = accounts.AccountInstruments(self._account_id)
        self._client.request(r)
        out = []
        for ins in r.response.get("instruments", []):
            out.append({
                "name": ins["name"],
                "type": ins["type"],
                "displayName": ins.get("displayName", ins["name"]),
                "pipLocation": ins.get("pipLocation", -4),
            })
        return out

    def fetch_candles(
        self,
        instrument: str,
        granularity: str,
        *,
        from_: datetime | None = None,
        to: datetime | None = None,
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"granularity": granularity, "price": "M"}
        if count is not None:
            params["count"] = count
        if from_ is not None:
            params["from"] = from_.isoformat()
        if to is not None:
            params["to"] = to.isoformat()
        r = instruments.InstrumentsCandles(instrument, params=params)
        self._client.request(r)
        return r.response.get("candles", [])

    def latest_price(self, instrument: str) -> dict[str, Any]:
        r = pricing.PricingInfo(self._account_id, params={"instruments": instrument})
        self._client.request(r)
        prices = r.response.get("prices", [])
        if not prices:
            raise RuntimeError(f"no price returned for {instrument}")
        p = prices[0]
        return {
            "instrument": instrument,
            "bid": float(p["bids"][0]["price"]),
            "ask": float(p["asks"][0]["price"]),
            "time": p["time"],
        }

    def account_summary(self) -> dict[str, Any]:
        r = accounts.AccountSummary(self._account_id)
        self._client.request(r)
        return r.response.get("account", {})

    def open_positions(self) -> list[dict[str, Any]]:
        r = trades.OpenPositions(self._account_id)
        self._client.request(r)
        return r.response.get("positions", [])

    def open_trades(self) -> list[dict[str, Any]]:
        r = trades.OpenTrades(self._account_id)
        self._client.request(r)
        return r.response.get("trades", [])

    def place_order(self, order_body: dict[str, Any]) -> dict[str, Any]:
        if not self._cfg.write_enabled:
            raise PermissionError("place_order is disabled in this MCP subprocess")
        if self._cfg.environment == "live":
            raise PermissionError("live trading is disabled; LIVE_TRADING_ENABLED is false")
        r = orders.Orders(self._account_id, data=order_body)
        self._client.request(r)
        return r.response

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self._cfg.write_enabled:
            raise PermissionError("cancel_order is disabled in this MCP subprocess")
        r = orders.OrderCancel(self._account_id, order_id)
        self._client.request(r)
        return r.response
