"""Broker MCP data source.

The data source is the only place in the parent process that talks to
the broker's MCP subprocess (so credentials never enter this process).
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

import pandas as pd


class MarketDataSource(Protocol):
    def fetch_candles(
        self,
        instrument: str,
        granularity: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        count: int | None = None,
    ) -> pd.DataFrame:
        """Fetch historical candles. Columns: ts (DatetimeIndex), o, h, l, c, v.

        Implementations must return ascending-sorted, deduplicated rows.
        """

    def latest_price(self, instrument: str) -> tuple[float, float, datetime]:
        """Return (bid, ask, ts)."""

    def account_summary(self) -> dict:
        """Return the broker's account summary for the configured demo account."""


class McpDataSource:
    """Real data source: talks to the broker MCP subprocess via the mcp client.

    The MCP client is constructed by `app.orders.mcp_client.McpClientPool`;
    we keep the constructor dependency-free to make this module testable.
    """

    def __init__(self, mcp_client: object) -> None:
        self._client = mcp_client

    async def fetch_candles(
        self,
        instrument: str,
        granularity: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        count: int | None = None,
    ) -> pd.DataFrame:
        result = await self._client.call_tool(  # type: ignore[attr-defined]
            "get_candles",
            {
                "instrument": instrument,
                "granularity": granularity,
                "from": from_ts.isoformat() if from_ts else None,
                "to": to_ts.isoformat() if to_ts else None,
                "count": count,
            },
        )
        return _candles_to_df(result)

    async def latest_price(self, instrument: str) -> tuple[float, float, datetime]:
        result = await self._client.call_tool(  # type: ignore[attr-defined]
            "get_latest_price", {"instrument": instrument}
        )
        return float(result["bid"]), float(result["ask"]), datetime.fromisoformat(result["time"])

    async def account_summary(self) -> dict:
        return await self._client.call_tool("get_account_summary", {})  # type: ignore[attr-defined]


def _candles_to_df(payload: dict) -> pd.DataFrame:
    """Convert a `/candles` MCP response to a DataFrame."""
    rows = []
    for c in payload.get("candles", []):
        if not c.get("complete", False) and "mid" not in c:
            continue
        m = c.get("mid") or c.get("bid") or {}
        rows.append({
            "ts": pd.Timestamp(c["time"]),
            "o": float(m["o"]),
            "h": float(m["h"]),
            "l": float(m["l"]),
            "c": float(m["c"]),
            "v": int(c.get("volume", 0)),
        })
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    return df
