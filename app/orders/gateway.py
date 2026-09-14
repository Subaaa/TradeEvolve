"""OrderGateway: sends approved orders to the broker via the MCP subprocess.

The gateway is the only component that calls broker write tools. The
risk engine has already approved the order by the time it reaches here.

The app's risk model sizes positions in ounces of gold (`OrderSpec.units`,
OANDA's original convention). Alpaca trades `PAXG/USD` (Pax Gold, 1
token = 1 troy oz of gold), so `units` maps directly to Alpaca's order
`qty` -- no lot/contract-size conversion is needed here.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from app.risk.types import OrderSpec

logger = logging.getLogger(__name__)


class McpClientLike(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class OrderGateway:
    def __init__(self, mcp_client: McpClientLike) -> None:
        self._mcp = mcp_client

    async def submit(self, order: OrderSpec) -> dict[str, Any]:
        """Submit an order via the MCP server. Returns the broker's fill result."""
        args: dict[str, Any] = {
            "instrument": order.instrument,
            "qty": order.units,
            "side": "buy" if order.side.value == "long" else "sell",
            "client_order_id": order.client_tag,
        }
        return await self._mcp.call_tool("place_order", args)

    async def cancel(self, order_id: str) -> dict[str, Any]:
        return await self._mcp.call_tool("cancel_order", {"order_id": order_id})
