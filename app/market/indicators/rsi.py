"""Relative Strength Index (Wilder)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI on `close`.

    Returns a Series with values in [0, 100]. The first `length` entries are NaN.
    """
    if length <= 0:
        raise ValueError(f"length must be > 0, got {length}")
    diff = close.diff()
    up = diff.clip(lower=0.0)
    down = (-diff).clip(lower=0.0)
    # Wilder smoothing
    alpha = 1.0 / length
    avg_up = np.full(len(close), np.nan, dtype=float)
    avg_down = np.full(len(close), np.nan, dtype=float)
    up_vals = up.to_numpy()
    down_vals = down.to_numpy()
    # Seed with simple mean over first `length` deltas
    if length < len(close):
        seed_up = np.nanmean(up_vals[1 : length + 1])
        seed_down = np.nanmean(down_vals[1 : length + 1])
        if not (np.isnan(seed_up) or np.isnan(seed_down)):
            avg_up[length] = seed_up
            avg_down[length] = seed_down
            prev_up, prev_down = seed_up, seed_down
            for i in range(length + 1, len(close)):
                u, d = up_vals[i], down_vals[i]
                if np.isnan(u) or np.isnan(d):
                    avg_up[i] = prev_up
                    avg_down[i] = prev_down
                else:
                    prev_up = alpha * u + (1 - alpha) * prev_up
                    prev_down = alpha * d + (1 - alpha) * prev_down
                    avg_up[i] = prev_up
                    avg_down[i] = prev_down
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_down == 0, np.inf, avg_up / avg_down)
    rsi_v = 100.0 - (100.0 / (1.0 + rs))
    rsi_v = np.where(np.isinf(rs), 100.0, rsi_v)
    return pd.Series(rsi_v, index=close.index)
