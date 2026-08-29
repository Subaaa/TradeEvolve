"""Rolling z-score (mean / std over a window)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_z(series: pd.Series, window: int) -> pd.Series:
    """z = (x - rolling_mean) / rolling_std.

    Returns NaN where std == 0 to avoid divide-by-zero. The first
    `window - 1` entries are NaN.
    """
    if window <= 0:
        raise ValueError(f"window must be > 0, got {window}")
    r = series.rolling(window=window, min_periods=window)
    mean = r.mean()
    std = r.std(ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (series - mean) / std
    return z.replace([np.inf, -np.inf], np.nan)
