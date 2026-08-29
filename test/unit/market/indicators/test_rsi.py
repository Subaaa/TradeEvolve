"""RSI tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from app.market.indicators.rsi import rsi


@given(
    n=st.integers(min_value=30, max_value=200),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    length=st.integers(min_value=2, max_value=20),
)
@settings(max_examples=200, deadline=None)
def test_rsi_bounds(n: int, seed: int, length: int) -> None:
    rng = np.random.default_rng(seed)
    s = pd.Series(np.cumsum(rng.normal(size=n)) + 100)
    r = rsi(s, length)
    valid = r.dropna()
    assert (valid >= 0.0).all() and (valid <= 100.0).all()


def test_rsi_monotonic_up_is_near_100() -> None:
    s = pd.Series(np.arange(1.0, 200.0))  # strictly increasing
    r = rsi(s, 14)
    valid = r.dropna()
    # All gains, no losses -> RSI = 100
    np.testing.assert_allclose(valid.to_numpy(), 100.0, atol=1e-9)


def test_rsi_monotonic_down_is_near_0() -> None:
    s = pd.Series(np.arange(200.0, 0.0, -1.0))  # strictly decreasing
    r = rsi(s, 14)
    valid = r.dropna()
    np.testing.assert_allclose(valid.to_numpy(), 0.0, atol=1e-9)
