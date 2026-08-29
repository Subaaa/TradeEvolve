# oanda-mcp

Standalone OANDA v20 MCP server. Holds the OANDA Practice API token and
exposes 9 tools to the agent process over stdio (or HTTP).

The agent process is a child; it has no direct OANDA credentials.

## Tools

- `get_instruments` — list tradable instruments.
- `get_candles` — historical OHLCV candles.
- `get_latest_price` — current bid/ask.
- `get_account_summary` — Practice account summary.
- `get_open_positions` — list of open positions.
- `get_open_trades` — list of open trades.
- `place_order` — submit an order. **Gated by `OANDA_MCP_WRITE_ENABLED`**.
- `cancel_order` — cancel an order. **Gated**.

## Run

```bash
cd services/oanda_mcp
uv sync
OANDA_API_KEY_PAPER=... OANDA_ACCOUNT_ID_PAPER=... \
OANDA_ENVIRONMENT=practice OANDA_MCP_WRITE_ENABLED=true \
uv run python -m oanda_mcp.server
```
