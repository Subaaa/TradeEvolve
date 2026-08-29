"""Property-based and unit tests for ATR and True Range."""
from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from app.market.indicators.atr import atr, atr_percentile, true_range


@given(
    n=st.integers(min_value=30, max_value=200),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    length=st.integers(min_value=2, max_value=20),
)
@settings(max_examples=200, deadline=None)
def test_atr_constant_series_is_zero(n: int, seed: int, length: int) -> None:
    rng = np.random.default_rng(seed)
    base = float(rng.uniform(100, 200))
    h = pd.Series([base + 1.0] * n)
    l = pd.Series([base - 1.0] * n)
    c = pd.Series([base] * n)
    a = atr(h, l, c, length)
    out = a.iloc[length:].to_numpy()
    # Constant range = 2; ATR should be 2.
    np.testing.assert_allclose(out, 2.0, atol=1e-9)


def test_true_range_matches_definition() -> None:
    h = pd.Series([11.0, 12.0, 13.0, 14.0])
    l = pd.Series([9.0, 10.0, 11.0, 12.0])
    c = pd.Series([10.0, 11.0, 12.0, 13.0])
    tr = true_range(h, l, c)
    # bar 0: prev_close NaN, so TR = h - l = 2
    # bar 1: max(1, |12-10|=2, |10-10|=0) = 2
    # bar 2: max(2, |13-11|=2, |11-11|=0) = 2
    # bar 3: max(2, |14-12|=2, |12-12|=0) = 2
    np.testing.assert_allclose(tr.to_numpy(), [2.0, 2.0, 2.0, 2.0], atol=1e-9)


def test_atr_rejects_zero_length() -> None:
    h = pd.Series([1.0, 2.0, 3.0])
    l = pd.Series([0.5, 1.5, 2.5])
    c = pd.Series([0.7, 1.7, 2.7])
    try:
        atr(h, l, c, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for length=0")


def test_atr_percentile_bounds() -> None:
    np.random.seed(0)
    a = pd.Series(np.abs(np.random.normal(size=200)) + 1.0)
    p = atr_percentile(a, window=50, level=0.95)
    valid = p.dropna()
    assert (valid >= 0.0).all() and (valid <= 1.0).all()


def test_atr_percentile_warmup_is_nan() -> None:
    a = pd.Series([1.0] * 100)
    p = atr_percentile(a, window=50, level=0.5)
    assert p.iloc[:49].isna().all()
    assert p.iloc[49:].notna().all()
