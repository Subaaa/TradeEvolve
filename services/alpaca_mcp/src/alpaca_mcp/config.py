"""Configuration for the Alpaca MCP subprocess."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AlpacaMcpConfig:
    api_key_id: str
    api_secret_key: str
    base_url: str            # trading API, e.g. https://paper-api.alpaca.markets
    data_url: str            # market data API, e.g. https://data.alpaca.markets
    symbol: str               # actual Alpaca symbol, e.g. "PAXG/USD"
    write_enabled: bool

    @classmethod
    def from_env(cls) -> AlpacaMcpConfig:
        return cls(
            api_key_id=os.environ.get("ALPACA_API_KEY_ID", ""),
            api_secret_key=os.environ.get("ALPACA_API_SECRET_KEY", ""),
            base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            data_url=os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets"),
            symbol=os.environ.get("ALPACA_SYMBOL", "PAXG/USD"),
            write_enabled=os.environ.get("ALPACA_MCP_WRITE_ENABLED", "false").lower() == "true",
        )
