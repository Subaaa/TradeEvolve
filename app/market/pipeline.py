"""Market data pipeline: reconcile the on-disk candle store with OANDA.

The pipeline is the only component that writes to the `candles` table.
It is idempotent: re-running it on the same window produces no change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

import pandas as pd

from .data_source import MarketDataSource


class CandleStore(Protocol):
    def upsert(self, df: pd.DataFrame) -> int:
        """Insert-or-update candles. Returns the number of rows written."""

    def read(self, instrument: str, granularity: str, *, limit: int | None = None) -> pd.DataFrame:
        """Read the most recent candles, ascending."""


class MarketDataPipeline:
    def __init__(self, source: MarketDataSource, store: CandleStore) -> None:
        self._source = source
        self._store = store

    async def reconcile(
        self, instrument: str, granularity: str, *, lookback: timedelta = timedelta(days=7)
    ) -> int:
        """Fetch any new candles since the last stored bar and upsert them.

        Returns the number of rows written.
        """
        existing = self._store.read(instrument, granularity)
        if existing.empty:
            to_ts = datetime.now(tz=timezone.utc)
            from_ts = to_ts - lookback
            df = await self._source.fetch_candles(
                instrument,
                granularity,
                from_ts=from_ts,
                to_ts=to_ts,
            )
            if df.empty:
                return 0
            return self._store.upsert(df)

        last_ts = existing.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize(timezone.utc)
        # We always re-fetch a small overlap window to be safe with bar
        # boundary alignment.
        from_ts = last_ts - timedelta(hours=2)
        df = await self._source.fetch_candles(
            instrument, granularity, from_ts=from_ts
        )
        if df.empty:
            return 0
        new = df[df.index > last_ts]
        if new.empty:
            return 0
        return self._store.upsert(new)
