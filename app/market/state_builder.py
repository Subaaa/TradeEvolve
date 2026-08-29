"""Build a MarketState from the most recent candles, indicators, and news flags.

Pure function. No I/O, no LLM.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.risk.types import MarketState
from pinned.news_policy import NewsPolicy


def build(
    *,
    instrument: str,
    granularity: str,
    candles: pd.DataFrame,
    indicators: dict[str, float] | None = None,
    news_policy: NewsPolicy = NewsPolicy.NORMAL,
    news_flags: dict[str, bool] | None = None,
    now: datetime | None = None,
) -> MarketState:
    """Build a MarketState from current data."""
    if candles.empty:
        return MarketState(
            instrument=instrument,
            granularity=granularity,
            last_bar_ts=None,
            last_close=None,
            indicators=indicators or {},
            seconds_since_last_bar=10**9,
            has_high_impact_in_next_30m=False,
            has_central_bank_in_next_2h=False,
            has_data_release_in_next_2h=False,
        )

    now = now or datetime.now(tz=timezone.utc)
    last_bar_ts = candles.index[-1]
    if isinstance(last_bar_ts, pd.Timestamp):
        last_bar_dt = last_bar_ts.to_pydatetime()
    else:
        last_bar_dt = last_bar_ts  # type: ignore[assignment]
    if last_bar_dt.tzinfo is None:
        last_bar_dt = last_bar_dt.replace(tzinfo=timezone.utc)

    # Granularity -> bar length in seconds
    bar_seconds = {
        "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H4": 14400, "D": 86400,
    }.get(granularity, 3600)

    # Last bar's *completion* is the last_bar_ts + bar length.
    last_completion = last_bar_dt + pd.Timedelta(seconds=bar_seconds)
    seconds_since = max(0, int((now - last_completion).total_seconds()))

    news_flags = news_flags or {}
    return MarketState(
        instrument=instrument,
        granularity=granularity,
        last_bar_ts=last_bar_dt,
        last_close=float(candles["c"].iloc[-1]),
        indicators=indicators or {},
        seconds_since_last_bar=seconds_since,
        has_high_impact_in_next_30m=bool(news_flags.get("has_high_impact_in_next_30m", False)),
        has_central_bank_in_next_2h=bool(news_flags.get("has_central_bank_in_next_2h", False)),
        has_data_release_in_next_2h=bool(news_flags.get("has_data_release_in_next_2h", False)),
    )


def indicator_snapshot(candles: pd.DataFrame, **indicator_specs: Any) -> dict[str, float]:
    """Compute the latest value of each named indicator.

    Convenience for callers that need a flat dict of {name: value}.
    """
    out: dict[str, float] = {}
    if candles.empty:
        return out
    for name, value in indicator_specs.items():
        if value is None:
            continue
        if hasattr(value, "iloc"):
            v = value.iloc[-1]
        else:
            v = value
        if pd.isna(v):
            continue
        out[name] = float(v)
    return out
