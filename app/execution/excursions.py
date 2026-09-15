"""Maximum favourable / adverse excursion over a trade's lifetime.

MFE and MAE answer the two questions a review actually needs: "was this
nearly a winner before it turned" and "how close did it come to the stop
before it worked". Without them, a review can only see the outcome, which is
the one part of a trade that is mostly noise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.risk.types import Side


@dataclass(frozen=True)
class Excursions:
    mfe: float
    mae: float
    mfe_r: float
    mae_r: float
    bars_held: int


def compute_mfe_mae(
    bars: pd.DataFrame,
    *,
    entry: float,
    side: Side,
    sl_distance: float,
    opened_at: datetime,
    closed_at: datetime,
) -> Excursions:
    """Excursions over the bars the trade was open for, in price and in R.

    Both values are reported as non-negative distances from entry: `mfe` is
    how far the trade went the right way, `mae` how far the wrong way.
    """
    if bars.empty or sl_distance <= 0:
        return Excursions(0.0, 0.0, 0.0, 0.0, 0)

    idx = bars.index
    if not isinstance(idx, pd.DatetimeIndex):  # pragma: no cover - defensive
        return Excursions(0.0, 0.0, 0.0, 0.0, 0)

    start = pd.Timestamp(opened_at)
    end = pd.Timestamp(closed_at)
    if idx.tz is not None:
        start = start.tz_convert(idx.tz) if start.tzinfo else start.tz_localize(idx.tz)
        end = end.tz_convert(idx.tz) if end.tzinfo else end.tz_localize(idx.tz)

    window = bars[(idx >= start) & (idx <= end)]
    if window.empty:
        return Excursions(0.0, 0.0, 0.0, 0.0, 0)

    highest = float(window["h"].max())
    lowest = float(window["l"].min())

    if side is Side.LONG:
        mfe = max(0.0, highest - entry)
        mae = max(0.0, entry - lowest)
    else:
        mfe = max(0.0, entry - lowest)
        mae = max(0.0, highest - entry)

    return Excursions(
        mfe=mfe,
        mae=mae,
        mfe_r=mfe / sl_distance,
        mae_r=mae / sl_distance,
        bars_held=len(window),
    )
