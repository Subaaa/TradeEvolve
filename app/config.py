"""Runtime configuration loaded from environment.

Read once at process start; after that, code imports the typed `Settings`
singleton via `get_settings()`.

The process is a single Python process. It holds the Alpaca paper credentials
(there is no subprocess to hide them from — the previous MCP indirection
forwarded the same secrets from this process anyway) and the Anthropic API
key. The LLM client is constructed with only the Anthropic key: it never
receives broker credentials or a database handle.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pinned.simulation_constants import LIVE_TRADING_ENABLED as _LIVE_GUARD

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Typed runtime config."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("NITO_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"
    llm_timeout_sec: float = 120.0
    llm_max_retries: int = 3
    llm_daily_hard_token_cap: int = 400_000

    # --- Alpaca (paper only) ---
    alpaca_api_key_id: str = ""
    alpaca_api_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_symbol: str = "PAXG/USD"
    # Writes (order submit / cancel) are off by default. The live loop needs
    # this on; a read-only inspection run does not.
    alpaca_write_enabled: bool = False

    # --- App ---
    app_env: str = "dev"
    app_log_level: str = "INFO"
    app_http_host: str = "127.0.0.1"
    app_http_port: int = 8080

    # --- Strategy ---
    initial_paper_equity_usd: float = 10_000.0
    instrument: str = "XAU_USD"
    granularity: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D"] = "M15"
    champion_bootstrap_config: str = "pinned/strategies/pullback_paxg_m15_v1.json"

    # --- Storage ---
    db_path: Path = Field(default=_PROJECT_ROOT / "data" / "tradeevolve.sqlite")

    # --- Project layout ---
    project_root: Path = _PROJECT_ROOT

    @property
    def live_trading_enabled(self) -> bool:
        """Always false in the MVP.

        `app.broker.alpaca.AlpacaClient` independently refuses to connect to
        anything that is not a paper-trading base URL.
        """
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
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def resolved_champion_config(self) -> Path:
        p = Path(self.champion_bootstrap_config)
        return p if p.is_absolute() else self.project_root / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Tests that need different settings should call `get_settings.cache_clear()`.
    """
    return Settings()
