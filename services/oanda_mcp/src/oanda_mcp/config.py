"""Configuration for the OANDA MCP subprocess."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OandaMcpConfig:
    api_key: str
    account_id: str
    environment: str            # 'practice' or 'live'
    write_enabled: bool

    @classmethod
    def from_env(cls) -> "OandaMcpConfig":
        env = os.environ.get("OANDA_ENVIRONMENT", "practice")
        if env == "live":
            api_key = os.environ.get("OANDA_API_KEY_LIVE", "")
            account_id = os.environ.get("OANDA_ACCOUNT_ID_LIVE", "")
        else:
            api_key = os.environ.get("OANDA_API_KEY_PAPER", "")
            account_id = os.environ.get("OANDA_ACCOUNT_ID_PAPER", "")
        write = os.environ.get("OANDA_MCP_WRITE_ENABLED", "false").lower() == "true"
        return cls(
            api_key=api_key,
            account_id=account_id,
            environment=env,
            write_enabled=write,
        )
