"""OrderGateway: sends approved orders to OANDA via the MCP subprocess.

The gateway is the only component that calls OANDA write tools. The
risk engine has already approved the order by the time it reaches here.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from app.journal.types import JournalEntry
from app.journal.writer import JournalWriter
from app.risk.types import OrderSpec


logger = logging.getLogger(__name__)


class McpClientLike(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class OrderGateway:
    def __init__(self, mcp_client: McpClientLike, journal: JournalWriter) -> None:
        self._mcp = mcp_client
        self._journal = journal

    async def submit(self, order: OrderSpec) -> dict[str, Any]:
        """Submit an order via the MCP server. Returns the OANDA OrderFill."""
        args: dict[str, Any] = {
            "instrument": order.instrument,
            "units": order.units if order.side.value == "long" else -order.units,
            "type": order.order_type.value,
            "timeInForce": order.tif.value,
            "clientExtensions": {"tag": order.client_tag},
        }
        if order.entry_price:
            args["price"] = order.entry_price
        if order.stop_loss is not None:
            args["stopLossOnFill"] = {"price": str(order.stop_loss), "timeInForce": "GTC"}
        if order.take_profit is not None:
            args["takeProfitOnFill"] = {"price": str(order.take_profit), "timeInForce": "GTC"}
        return await self._mcp.call_tool("place_order", args)

    async def cancel(self, order_id: str) -> dict[str, Any]:
        return await self._mcp.call_tool("cancel_order", {"orderId": order_id})
