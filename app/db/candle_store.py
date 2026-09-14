"""Postgres-backed `CandleStore` (the `candles` table).

Bound to a single instrument/granularity -- the MVP trades one asset on
one timeframe (`RiskLimits.max_open_positions=1`, README: "single
asset"), and `MarketDataPipeline.reconcile()`'s `upsert(df)` call carries
no instrument/granularity of its own (see `app/market/pipeline.py`), so
the store needs to already know which pair it owns.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine


class SupabaseCandleStore:
    def __init__(self, engine: Engine, instrument: str, granularity: str) -> None:
        self._engine = engine
        self._instrument = instrument
        self._granularity = granularity

    def upsert(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        rows = [
            {
                "instrument": self._instrument,
                "granularity": self._granularity,
                "ts": ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts,
                "open": float(r["o"]),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
                "volume": int(r["v"]),
            }
            for ts, r in df.iterrows()
        ]
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    insert into candles (instrument, granularity, ts, open, high, low, close, volume)
                    values (:instrument, :granularity, :ts, :open, :high, :low, :close, :volume)
                    on conflict (instrument, granularity, ts) do update set
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        volume = excluded.volume
                    """
                ),
                rows,
            )
        return len(rows)

    def read(self, instrument: str, granularity: str, *, limit: int | None = None) -> pd.DataFrame:
        query = """
            select ts, open, high, low, close, volume
            from candles
            where instrument = :instrument and granularity = :granularity
            order by ts desc
        """
        params: dict[str, object] = {"instrument": instrument, "granularity": granularity}
        if limit is not None:
            query += " limit :limit"
            params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(text(query), params).mappings().all()
        if not rows:
            return pd.DataFrame(columns=["o", "h", "l", "c", "v"]).set_index(
                pd.DatetimeIndex([], name="ts")
            )
        df = pd.DataFrame(
            {
                "ts": [r["ts"] for r in rows],
                "o": [float(r["open"]) for r in rows],
                "h": [float(r["high"]) for r in rows],
                "l": [float(r["low"]) for r in rows],
                "c": [float(r["close"]) for r in rows],
                "v": [int(r["volume"] or 0) for r in rows],
            }
        )
        return df.set_index("ts").sort_index()
