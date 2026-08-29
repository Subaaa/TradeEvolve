"""Rolling z-score tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from app.market.indicators.rolling_z import rolling_z


@given(
    n=st.integers(min_value=200, max_value=1000),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    window=st.integers(min_value=2, max_value=20),
)
@settings(max_examples=200, deadline=None)
def test_rolling_z_standard_normal(n: int, seed: int, window: int) -> None:
    rng = np.random.default_rng(seed)
    s = pd.Series(rng.normal(size=n))
    z = rolling_z(s, window)
    valid = z.dropna()
    # z of a standard normal has mean ~0 and std ~1 over a long enough series.
    # The bounds are wide enough to handle small-sample effects.
    assert abs(valid.mean()) < 0.3
    assert 0.7 < valid.std() < 1.3


def test_rolling_z_constant_series_is_nan() -> None:
    s = pd.Series([5.0] * 50)
    z = rolling_z(s, 10)
    valid = z.dropna()
    # std = 0 -> NaN (we don't divide by zero)
    assert valid.empty or valid.isna().all()
