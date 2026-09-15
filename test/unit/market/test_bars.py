"""The forming-bar guard.

A repainting signal is the failure this project is most exposed to, so these
are property tests rather than examples: no input should ever let a bar whose
period has not elapsed reach the strategy.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.market.bars import (
    bars_between,
    drop_forming_bar,
    expected_last_closed_bar,
    granularity_seconds,
    to_dataframe,
)
from test.conftest import M15, START, make_bar

pytestmark = pytest.mark.unit


def _series(n: int) -> pd.DataFrame:
    return to_dataframe(
        [make_bar(START + i * M15, 100.0, 101.0, 99.0, 100.5) for i in range(n)]
    )


def test_to_dataframe_is_utc_and_sorted() -> None:
    df = to_dataframe(
        [
            make_bar(START + 2 * M15, 1, 2, 0.5, 1.5),
            make_bar(START, 1, 2, 0.5, 1.5),
            make_bar(START + M15, 1, 2, 0.5, 1.5),
        ]
    )
    assert list(df.index) == sorted(df.index)
    assert df.index.tz is not None


def test_to_dataframe_preserves_fractional_volume() -> None:
    # PAXG bar volumes are fractional; the old int() cast zeroed every one.
    df = to_dataframe([make_bar(START, 1, 2, 0.5, 1.5, v=0.044733)])
    assert df["v"].iloc[0] == pytest.approx(0.044733)


def test_drop_forming_bar_removes_the_open_bar() -> None:
    df = _series(5)
    last = df.index[-1].to_pydatetime()
    # One second before the final bar's period elapses, it is still forming.
    closed = drop_forming_bar(df, "M15", last + M15 - timedelta(seconds=1))
    assert len(closed) == 4
    assert closed.index[-1].to_pydatetime() == last - M15


def test_drop_forming_bar_keeps_a_just_closed_bar() -> None:
    df = _series(5)
    last = df.index[-1].to_pydatetime()
    closed = drop_forming_bar(df, "M15", last + M15)
    assert len(closed) == 5


@settings(max_examples=100, deadline=None)
@given(
    count=st.integers(min_value=1, max_value=60),
    offset_seconds=st.integers(min_value=-3600, max_value=3600),
    granularity=st.sampled_from(["M1", "M5", "M15", "M30", "H1"]),
)
def test_no_forming_bar_ever_survives(
    count: int, offset_seconds: int, granularity: str
) -> None:
    period = timedelta(seconds=granularity_seconds(granularity))
    bars = [
        make_bar(START + i * period, 100.0, 101.0, 99.0, 100.5) for i in range(count)
    ]
    now = START + count * period + timedelta(seconds=offset_seconds)
    closed = drop_forming_bar(to_dataframe(bars), granularity, now)
    for ts in closed.index:
        assert ts.to_pydatetime() + period <= now


def test_expected_last_closed_bar_is_one_period_back() -> None:
    now = datetime(2026, 3, 2, 10, 7, 30, tzinfo=UTC)
    assert expected_last_closed_bar(now, "M15") == datetime(
        2026, 3, 2, 9, 45, tzinfo=UTC
    )


def test_bars_between_is_never_negative() -> None:
    later = START
    earlier = START + 5 * M15
    assert bars_between(earlier, later, "M15") == 0
    assert bars_between(later, earlier, "M15") == 5


def test_granularity_seconds_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported granularity"):
        granularity_seconds("M7")
