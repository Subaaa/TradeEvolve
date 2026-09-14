"""Alpaca MCP server entry point.

The server speaks MCP over stdio (the default) or HTTP. It exposes 7
tools; the two write tools are gated by `ALPACA_MCP_WRITE_ENABLED`, and
`AlpacaClient` independently refuses to even connect unless the base URL
is a paper-trading endpoint (the `LIVE_TRADING_ENABLED` guard for this
broker).

The MCP Python SDK v2 surface is `MCPServer` (formerly `FastMCP`).
We use the decorator API for clarity.
"""
from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer  # v2.x

from .alpaca_client import AlpacaClient
from .config import AlpacaMcpConfig
from .tools.schemas import (
    CancelOrderIn,
    CancelOrderOut,
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
    PlaceOrderIn,
    PlaceOrderOut,
)

logger = logging.getLogger(__name__)


def build_server(client: AlpacaClient, cfg: AlpacaMcpConfig) -> MCPServer:
    server = MCPServer("alpaca-mcp")

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
    def place_order(args: PlaceOrderIn) -> PlaceOrderOut:
        if not cfg.write_enabled:
            raise PermissionError("place_order is disabled (ALPACA_MCP_WRITE_ENABLED=false)")
        result = client.place_order(args.model_dump())
        return PlaceOrderOut(**result)

    @server.tool()
    def cancel_order(args: CancelOrderIn) -> CancelOrderOut:
        if not cfg.write_enabled:
            raise PermissionError("cancel_order is disabled (ALPACA_MCP_WRITE_ENABLED=false)")
        result = client.cancel_order(args.order_id)
        return CancelOrderOut(**result)

    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    cfg = AlpacaMcpConfig.from_env()
    try:
        client = AlpacaClient(cfg)
    except PermissionError as e:
        logger.error("refusing to start: %s", e)
        return 2
    except RuntimeError as e:
        logger.error("Alpaca client init failed: %s", e)
        return 3
    server = build_server(client, cfg)
    logger.info("alpaca-mcp starting symbol=%s write_enabled=%s", cfg.symbol, cfg.write_enabled)
    try:
        server.run(transport="stdio")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
