"""Average True Range (ATR).

True Range is the max of:
    high - low
    |high - prev_close|
    |low  - prev_close|

ATR is a Wilder-smoothed moving average of True Range. We use the
canonical "RMA" (Wilder) variant: alpha = 1 / length, so the
first ATR value is the simple mean of the first `length` TRs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> pd.Series:
    """Compute the True Range series."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder-smoothed ATR.

    Args:
        high, low, close: 1-D series, same index.
        length: window for the smoothing.

    Returns:
        A `pd.Series` of ATR values. The first `length` entries are NaN;
        the value at `length` is the mean of the first `length` TRs.
    """
    if length <= 0:
        raise ValueError(f"length must be > 0, got {length}")
    tr = true_range(high, low, close)
    # Wilder's smoothing
    alpha = 1.0 / length
    out = np.full(len(tr), np.nan, dtype=float)
    values = tr.to_numpy()
    # Seed
    seed_idx = length - 1
    if seed_idx < 0 or seed_idx >= len(values):
        return pd.Series(out, index=tr.index)
    seed = np.nanmean(values[:length])
    if np.isnan(seed):
        return pd.Series(out, index=tr.index)
    out[seed_idx] = seed
    prev = seed
    for i in range(length, len(values)):
        v = values[i]
        if np.isnan(v):
            out[i] = prev
        else:
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return pd.Series(out, index=tr.index)


def atr_percentile(atr_series: pd.Series, window: int, level: float) -> pd.Series:
    """Rolling percentile rank of the latest ATR value within `window`.

    Returns a Series with values in [0, 1]. The first `window` entries are NaN.
    Used by the strategy to refuse entries in a volatility spike.
    """
    if not 0.0 <= level <= 1.0:
        raise ValueError(f"level must be in [0, 1], got {level}")
    # We compute the rank: for each bar, the fraction of values in the
    # past `window` that are <= the current value.
    def _rank(x: np.ndarray) -> float:
        if len(x) == 0 or np.isnan(x[-1]):
            return np.nan
        return float((x[:-1] <= x[-1]).sum()) / float(len(x) - 1)
    return atr_series.rolling(window=window, min_periods=window).apply(_rank, raw=True)
