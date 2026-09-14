"""Runtime configuration loaded from environment.

Read once at process start. After that, code imports the typed `Settings`
singleton via `get_settings()`.

We split the configuration into two parts: the secrets the LLM may *not*
see (DB URLs, the Alpaca API secret — although it is only read by the
MCP subprocess, never by the parent), and the operational settings
that may be surfaced in admin endpoints.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pinned.simulation_constants import LIVE_TRADING_ENABLED as _LIVE_GUARD


class Settings(BaseSettings):
    """Typed runtime config.

    Anything the LLM client should be able to see is here; anything it
    must not see is loaded only by the components that need it
    (the LLM client is constructed with only `minimax_api_key`).
    """

    model_config = SettingsConfigDict(
        env_file=os.environ.get("NITO_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    minimax_api_key: str = Field(default="dev-placeholder", min_length=1)
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M3"
    llm_daily_soft_token_cap: int = 100_000
    llm_daily_hard_token_cap: int = 200_000

    # --- Supabase (admin) ---
    supabase_url: str = "https://dev.supabase.co"
    supabase_service_role_key: str = "dev-placeholder"
    supabase_db_url: str = "postgresql://dev:dev@localhost:5432/dev"
    supabase_journal_db_url: str = "postgresql://journal_writer:dev@localhost:5432/dev"

    # --- Alpaca (read by MCP subprocess only) ---
    alpaca_api_key_id: str = ""
    alpaca_api_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_symbol: str = "PAXG/USD"
    alpaca_mcp_write_enabled: bool = True

    # --- App ---
    app_env: str = "dev"
    app_log_level: str = "INFO"
    app_timezone: str = "UTC"
    app_http_host: str = "127.0.0.1"
    app_http_port: int = 8080

    # --- Strategy & risk ---
    initial_paper_equity_usd: float = 10_000.0
    instrument: str = "XAU_USD"
    granularity: str = "H1"

    # --- Alerts ---
    alert_webhook_url: str = ""

    # --- Project layout ---
    project_root: Path = Path(__file__).resolve().parents[1]

    @property
    def live_trading_enabled(self) -> bool:
        # Always false in the MVP. The Alpaca MCP subprocess also enforces
        # this independently by refusing to connect to a non-paper base URL.
        return _LIVE_GUARD

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def fixtures_dir(self) -> Path:
        return self.data_dir / "fixtures"

    @property
    def candles_dir(self) -> Path:
        return self.data_dir / "candles"

    @property
    def holdout_dir(self) -> Path:
        return self.data_dir / "holdout"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Tests that need different settings should call `get_settings.cache_clear()`
    and re-instantiate.
    """
    return Settings()
