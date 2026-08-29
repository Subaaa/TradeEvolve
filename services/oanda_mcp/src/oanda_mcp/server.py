"""OANDA MCP server entry point.

The server speaks MCP over stdio (the default) or HTTP. It exposes 9
tools; the two write tools are gated by `OANDA_MCP_WRITE_ENABLED` and
the `LIVE_TRADING_ENABLED` constant in the parent process.

The MCP Python SDK v2 surface is `MCPServer` (formerly `FastMCP`).
We use the decorator API for clarity.
"""
from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer  # v2.x
from pydantic import ValidationError

from .config import OandaMcpConfig
from .oanda_client import OandaClient
from .tools.get_instruments import (
    GetAccountSummaryIn,
    GetAccountSummaryOut,
    GetCandlesIn,
    GetCandlesOut,
    GetInstrumentsIn,
    GetInstrumentsOut,
    GetLatestPriceIn,
    GetLatestPriceOut,
    GetOpenPositionsIn,
    GetOpenPositionsOut,
    GetOpenTradesIn,
    GetOpenTradesOut,
    PlaceOrderIn,
    PlaceOrderOut,
    CancelOrderIn,
    CancelOrderOut,
)


logger = logging.getLogger(__name__)


def build_server(client: OandaClient, cfg: OandaMcpConfig) -> MCPServer:
    server = MCPServer("oanda-mcp")

    @server.tool()
    def get_instruments(args: GetInstrumentsIn) -> GetInstrumentsOut:
        return GetInstrumentsOut(instruments=client.list_instruments())

    @server.tool()
    def get_candles(args: GetCandlesIn) -> GetCandlesOut:
        candles = client.fetch_candles(
            args.instrument,
            args.granularity,
            from_=args.from_,
            to=args.to,
            count=args.count,
        )
        return GetCandlesOut(candles=candles)

    @server.tool()
    def get_latest_price(args: GetLatestPriceIn) -> GetLatestPriceOut:
        d = client.latest_price(args.instrument)
        return GetLatestPriceOut(**d)

    @server.tool()
    def get_account_summary(args: GetAccountSummaryIn) -> GetAccountSummaryOut:
        return GetAccountSummaryOut(account=client.account_summary())

    @server.tool()
    def get_open_positions(args: GetOpenPositionsIn) -> GetOpenPositionsOut:
        return GetOpenPositionsOut(positions=client.open_positions())

    @server.tool()
    def get_open_trades(args: GetOpenTradesIn) -> GetOpenTradesOut:
        return GetOpenTradesOut(trades=client.open_trades())

    @server.tool()
    def place_order(args: PlaceOrderIn) -> PlaceOrderOut:
        if not cfg.write_enabled:
            raise PermissionError("place_order is disabled (OANDA_MCP_WRITE_ENABLED=false)")
        if cfg.environment == "live":
            raise PermissionError("live trading is disabled (LIVE_TRADING_ENABLED=false)")
        body = args.model_dump(exclude_none=True, by_alias=True)
        result = client.place_order(body)
        return PlaceOrderOut(
            orderFillTransaction=result.get("orderFillTransaction"),
            orderCancelTransaction=result.get("orderCancelTransaction"),
            relatedTransactionIDs=result.get("relatedTransactionIDs", []),
        )

    @server.tool()
    def cancel_order(args: CancelOrderIn) -> CancelOrderOut:
        if not cfg.write_enabled:
            raise PermissionError("cancel_order is disabled (OANDA_MCP_WRITE_ENABLED=false)")
        result = client.cancel_order(args.orderId)
        return CancelOrderOut(orderCancelTransaction=result.get("orderCancelTransaction"))

    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    cfg = OandaMcpConfig.from_env()
    if cfg.environment == "live":
        logger.error("refusing to start: OANDA_ENVIRONMENT=live (LIVE_TRADING_ENABLED=false)")
        return 2
    try:
        client = OandaClient(cfg)
    except RuntimeError as e:
        logger.error("OANDA client init failed: %s", e)
        return 3
    server = build_server(client, cfg)
    logger.info(
        "oanda-mcp starting env=%s write_enabled=%s",
        cfg.environment, cfg.write_enabled,
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
