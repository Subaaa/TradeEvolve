"""OANDA v20 data source.

The data source is the only place in the parent process that knows the
OANDA endpoints. In live operation it goes through the OANDA MCP
subprocess (so the token never enters this process). For tests, we
expose a direct `oandapyV20` path under a `MOCK` env var.
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
        """Return OANDA account summary for the configured Practice account."""


class OandaMcpDataSource:
    """Real data source: talks to the OANDA MCP subprocess via the mcp client.

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


class DirectOandaDataSource:
    """Direct `oandapyV20` data source. Used in tests with a mocked client."""

    def __init__(self, oanda_client: object) -> None:
        self._client = oanda_client

    def fetch_candles(
        self,
        instrument: str,
        granularity: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        count: int | None = None,
    ) -> pd.DataFrame:
        import oandapyV20.endpoints.instruments as instruments  # type: ignore

        params: dict = {"granularity": granularity}
        if count is not None:
            params["count"] = count
        if from_ts is not None:
            params["from"] = from_ts.isoformat()
        if to_ts is not None:
            params["to"] = to_ts.isoformat()
        r = instruments.InstrumentsCandles(instrument, params=params)
        self._client.request(r)
        return _candles_to_df(r.response)

    def latest_price(self, instrument: str) -> tuple[float, float, datetime]:
        import oandapyV20.endpoints.pricing as pricing  # type: ignore

        r = pricing.PricingInfo(self._client.account_id, params={"instruments": instrument})
        self._client.request(r)
        p = r.response["prices"][0]
        return float(p["bids"][0]["price"]), float(p["asks"][0]["price"]), datetime.fromisoformat(
            p["time"].replace("Z", "+00:00")
        )

    def account_summary(self) -> dict:
        import oandapyV20.endpoints.accounts as accounts  # type: ignore

        r = accounts.AccountSummary(self._client.account_id)
        self._client.request(r)
        return r.response["account"]


def _candles_to_df(payload: dict) -> pd.DataFrame:
    """Convert an OANDA `/candles` response to a DataFrame."""
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
