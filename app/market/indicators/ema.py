"""Exponential moving average.

Standard recursive definition:
    ema_t = alpha * x_t + (1 - alpha) * ema_{t-1}
where alpha = 2 / (length + 1).

The first `length` values are seeded with the simple mean of the
first `length` values; this matches the convention used by
TA-Lib, vectorbt, and most charting libraries.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Compute the exponential moving average of `series` with span `length`.

    Args:
        series: A 1-D price series indexed by datetime.
        length: EMA span (e.g. 20, 50, 200).

    Returns:
        A `pd.Series` of the same index as `series`. The first `length - 1`
        entries are NaN; the value at `length - 1` is the SMA seed.
    """
    if length <= 0:
        raise ValueError(f"length must be > 0, got {length}")
    if series.empty:
        return series.copy()

    s = pd.to_numeric(series, errors="coerce").astype(float)
    alpha = 2.0 / (length + 1.0)

    # SMA seed
    seed = s.iloc[:length].mean()
    out = np.full(len(s), np.nan, dtype=float)
    if np.isnan(seed):
        return pd.Series(out, index=s.index)

    out[length - 1] = seed
    prev = seed
    values = s.to_numpy()
    for i in range(length, len(values)):
        v = values[i]
        if np.isnan(v):
            # Carry forward previous EMA on missing data
            out[i] = prev
        else:
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev

    return pd.Series(out, index=s.index)


def ema_cross(series: pd.Series, fast: int, slow: int) -> pd.Series:
    """Return a Series with values in {-1, 0, 1}:
    - 1 when `fast` crosses above `slow` on this bar
    - -1 when `fast` crosses below `slow` on this bar
    - 0 otherwise (or if either is NaN)
    """
    e_fast = ema(series, fast)
    e_slow = ema(series, slow)
    out = pd.Series(0, index=series.index, dtype=int)
    # prev_*_above is the previous bar's (fast - slow) > 0
    diff = e_fast - e_slow
    prev_diff = diff.shift(1)
    cross_up = (prev_diff <= 0) & (diff > 0)
    cross_down = (prev_diff >= 0) & (diff < 0)
    out = out.mask(cross_up, 1).mask(cross_down, -1)
    return out
