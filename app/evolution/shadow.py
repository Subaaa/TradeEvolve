"""Shadow paper-trading.

The candidate runs against the live candle stream in parallel with the
champion, but its orders are not sent to the broker. The shadow's PnL is
computed from the same candles the live loop sees, and its metrics must
beat the champion's live metrics over the same window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .backtester import BacktestInput, run


@dataclass(frozen=True)
class ShadowResult:
    hours_observed: int
    trades: int
    net_pnl_usd: float
    ready: bool


def observe(
    candles: pd.DataFrame,
    config,
    initial_equity: float,
    *,
    started_at: datetime,
    min_hours: int = 240,
    min_trades: int = 30,
) -> ShadowResult:
    """Run the candidate as a shadow over the live window."""
    if candles.empty:
        return ShadowResult(0, 0, 0.0, False)
    out = run(BacktestInput(
        config=config, candles=candles,
        start=started_at, end=datetime.now(tz=started_at.tzinfo) if started_at.tzinfo else datetime.utcnow(),
        initial_equity=initial_equity, oos_label="validation", session_id="shadow",
    ))
    last = candles.index[-1]
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    hours = int((pd.Timestamp.now(tz="UTC") - last).total_seconds() // 3600)
    ready = hours >= min_hours and out.metrics.trade_count >= min_trades
    return ShadowResult(
        hours_observed=hours,
        trades=out.metrics.trade_count,
        net_pnl_usd=out.metrics.net_return_pct * initial_equity,
        ready=ready,
    )
