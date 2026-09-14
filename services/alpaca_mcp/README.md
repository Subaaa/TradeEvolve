# alpaca-mcp

Standalone Alpaca (paper trading) MCP server. Holds the Alpaca API
key/secret and exposes 7 tools to the agent process over stdio (or HTTP).

The agent process is a child; it has no direct Alpaca credentials.

Pure HTTPS REST (`httpx`) -- no native library, no local terminal. Runs
the same on Windows, Linux, or inside the Docker image. Refuses to
connect unless `ALPACA_BASE_URL` is a paper-trading URL
(`paper-api.alpaca.markets`) -- this is the live-trading guard for this
broker.

Trades `PAXG/USD` (Pax Gold, 1 token = 1 troy oz of physical gold) as
the `XAU_USD` proxy -- Alpaca has no forex/CFD gold market.

## Tools

- `get_instruments` -- list tradable instruments.
- `get_candles` -- historical OHLCV bars.
- `get_latest_price` -- current bid/ask from the order book.
- `get_account_summary` -- paper account summary (cash, equity, buying power).
- `get_open_positions` -- list of open positions.
- `place_order` -- submit a market order. **Gated by `ALPACA_MCP_WRITE_ENABLED`**.
- `cancel_order` -- cancel an order. **Gated**.

## Run

```bash
cd services/alpaca_mcp
uv sync
ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... \
ALPACA_BASE_URL=https://paper-api.alpaca.markets ALPACA_SYMBOL=PAXG/USD \
ALPACA_MCP_WRITE_ENABLED=true \
uv run python -m alpaca_mcp.server
```
