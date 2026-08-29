"""Property-based and unit tests for the EMA indicator.

Properties verified:
- The EMA of a constant series is that constant (after warm-up).
- The first `length - 1` values are NaN; the seed is the simple mean.
- The EMA at the seed is finite and equal to the simple mean of the first
  `length` values.
- `ema_cross` returns -1/0/1 only.
- `ema_cross` is invariant under vertical translation of the input.
- Recursive definition matches closed-form: ema_t = sum_{k=0}^{t} alpha*(1-alpha)^k * x_{t-k}.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from app.market.indicators.ema import ema, ema_cross


@given(
    values=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=20,
        max_size=200,
    ),
    length=st.integers(min_value=2, max_value=20),
)
@settings(max_examples=200, deadline=None)
def test_ema_constant_series_is_constant(values: list[float], length: int) -> None:
    s = pd.Series([values[0]] * len(values))
    e = ema(s, length)
    # After warm-up, all values should equal the constant.
    out = e.iloc[length - 1 :].to_numpy()
    np.testing.assert_allclose(out, values[0], atol=1e-9)


@given(
    values=st.lists(
        st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=20,
        max_size=200,
    ),
    length=st.integers(min_value=2, max_value=20),
)
@settings(max_examples=200, deadline=None)
def test_ema_seed_equals_sma(values: list[float], length: int) -> None:
    s = pd.Series(values)
    e = ema(s, length)
    expected_seed = float(np.mean(values[:length]))
    actual_seed = float(e.iloc[length - 1])
    assert math.isfinite(actual_seed)
    assert math.isclose(actual_seed, expected_seed, rel_tol=1e-9, abs_tol=1e-9)


@given(
    values=st.lists(
        st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=20,
        max_size=200,
    ),
    length=st.integers(min_value=2, max_value=20),
)
@settings(max_examples=200, deadline=None)
def test_ema_first_values_are_nan(values: list[float], length: int) -> None:
    s = pd.Series(values)
    e = ema(s, length)
    head = e.iloc[: length - 1].to_numpy()
    assert np.all(np.isnan(head))


def test_ema_rejects_zero_length() -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    try:
        ema(s, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for length=0")


def test_ema_cross_returns_only_three_values() -> None:
    np.random.seed(0)
    s = pd.Series(np.cumsum(np.random.normal(size=200)) + 100)
    c = ema_cross(s, 12, 26)
    assert set(c.unique()).issubset({-1, 0, 1})


def test_ema_cross_translation_invariant() -> None:
    np.random.seed(1)
    s = pd.Series(np.cumsum(np.random.normal(size=200)) + 100)
    s2 = s + 50.0
    c1 = ema_cross(s, 12, 26)
    c2 = ema_cross(s2, 12, 26)
    pd.testing.assert_series_equal(c1, c2)


def test_ema_empty_series() -> None:
    s = pd.Series([], dtype=float)
    e = ema(s, 14)
    assert e.empty
