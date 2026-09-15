"""Bar helpers: DataFrame conversion and forming-bar removal.

`drop_forming_bar` is the single most important function in the data path.
The broker happily returns the bar that is currently forming, and a strategy
that reads it will see signals that later disappear. Every consumer — the
live loop, the backtest loader, the shadow runner — goes through here.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.broker.types import Bar

GRANULARITY_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D": 86400,
    "W": 604800,
}


def granularity_seconds(granularity: str) -> int:
    try:
        return GRANULARITY_SECONDS[granularity]
    except KeyError:
        raise ValueError(f"unsupported granularity {granularity!r}") from None


def to_dataframe(bars: Sequence[Bar]) -> pd.DataFrame:
    """Convert broker bars into the canonical o/h/l/c/v frame."""
    if not bars:
        idx = pd.DatetimeIndex([], tz="UTC", name="ts")
        return pd.DataFrame(
            {c: pd.Series(dtype="float64") for c in ("o", "h", "l", "c", "v")}, index=idx
        )
    df = pd.DataFrame.from_records(
        [{"ts": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v} for b in bars]
    ).set_index("ts")
    df.index = pd.DatetimeIndex(df.index, name="ts")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df.sort_index()


def drop_forming_bar(
    df: pd.DataFrame, granularity: str, now: datetime | None = None
) -> pd.DataFrame:
    """Return only bars whose period has fully elapsed at `now`.

    A bar stamped `t` covers `[t, t + period)`, so it is closed only once
    `now >= t + period`.
    """
    if df.empty:
        return df
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    period = timedelta(seconds=granularity_seconds(granularity))
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):  # pragma: no cover - defensive
        raise TypeError("candle frame must have a DatetimeIndex")
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
        df = df.copy()
        df.index = idx
    cutoff = pd.Timestamp(moment - period)
    return df[idx <= cutoff]


def expected_last_closed_bar(now: datetime, granularity: str) -> datetime:
    """The timestamp of the most recent bar that should be closed at `now`."""
    period = granularity_seconds(granularity)
    moment = now.astimezone(UTC)
    epoch = int(moment.timestamp())
    start_of_current = epoch - (epoch % period)
    return datetime.fromtimestamp(start_of_current - period, tz=UTC)


def bars_between(
    earlier: datetime, later: datetime, granularity: str
) -> int:
    """Whole bars elapsed between two instants. Never negative."""
    period = granularity_seconds(granularity)
    delta = (later - earlier).total_seconds()
    return max(0, int(delta // period))
