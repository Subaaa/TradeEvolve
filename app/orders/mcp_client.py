"""Stdio client that launches and talks to the broker MCP subprocess.

The subprocess is the only component that calls the broker's API and
holds a live broker session. `pydantic-settings` reads `.env` into this
process's `Settings` object but does *not* export it to the OS
environment, so the parent has to forward the ALPACA_* values explicitly
into the child's environment when launching it -- `uv run --project
services/alpaca_mcp` has no `.env` of its own to pick them up from
otherwise.

Started lazily on first use, closed on shutdown.
"""
from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from app.config import get_settings

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "alpaca_mcp"


def _subprocess_env() -> dict[str, str]:
    settings = get_settings()
    env = dict(os.environ)
    env.update({
        "ALPACA_API_KEY_ID": settings.alpaca_api_key_id,
        "ALPACA_API_SECRET_KEY": settings.alpaca_api_secret_key,
        "ALPACA_BASE_URL": settings.alpaca_base_url,
        "ALPACA_DATA_URL": settings.alpaca_data_url,
        "ALPACA_SYMBOL": settings.alpaca_symbol,
        "ALPACA_MCP_WRITE_ENABLED": "true" if settings.alpaca_mcp_write_enabled else "false",
    })
    return env


class McpClientPool:
    """Owns one MCP subprocess + session, started on first `call_tool`."""

    def __init__(self, service_dir: Path = _SERVICE_DIR) -> None:
        self._service_dir = service_dir
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def _ensure_started(self) -> ClientSession:
        if self._session is not None:
            return self._session
        params = StdioServerParameters(
            command="uv",
            args=["run", "--project", str(self._service_dir), "python", "-m", "alpaca_mcp.server"],
            env=_subprocess_env(),
        )
        stack = AsyncExitStack()
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._stack = stack
        self._session = session
        return session

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = await self._ensure_started()
        # Every server-side tool is `def <name>(args: <Name>In) -> <Name>Out`,
        # so the MCP SDK derives an input schema of {"args": {...}} -- a
        # single field named after the parameter, not the flattened
        # `<Name>In` fields. Match that calling convention here.
        result = await session.call_tool(name, {"args": arguments})
        first_text = result.content[0].text if result.content and isinstance(
            result.content[0], TextContent
        ) else None
        if result.is_error:
            raise RuntimeError(f"MCP tool {name!r} failed: {first_text or 'unknown MCP tool error'}")
        if result.structured_content is not None:
            return dict(result.structured_content)
        if first_text is not None:
            return cast(dict[str, Any], json.loads(first_text))
        return {}

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
