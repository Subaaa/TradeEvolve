"""Shadow paper-trading.

The candidate runs against the same candles the live loop sees, but its
orders are never sent. Its metrics must beat the champion's over the same
window before anything is promoted.

The old implementation computed `hours_observed` as the time since the *last
candle*, which on live data is always a few minutes. `ready` was therefore
never true and the shadow gate returned `SHADOW_NOT_READY` forever, silently
making promotion impossible. It is now measured from `started_at`, which is
what the word means.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from app.strategy.configs import StrategyConfig
from pinned.promotion_gates import PROMOTION_GATES, PromotionGates

from .backtester import BacktestInput, run


@dataclass(frozen=True)
class ShadowResult:
    hours_observed: float
    trades: int
    net_pnl_usd: float
    expectancy_r: float
    primary_score: float
    ready: bool
    pnl_r: tuple[float, ...] = ()


def observe(
    candles: pd.DataFrame,
    config: StrategyConfig,
    initial_equity: float,
    *,
    started_at: datetime,
    now: datetime | None = None,
    gates: PromotionGates = PROMOTION_GATES,
) -> ShadowResult:
    """Replay the candidate over the shadow window."""
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    moment = now or datetime.now(tz=UTC)
    hours = max(0.0, (moment - started).total_seconds() / 3600.0)

    if candles.empty:
        return ShadowResult(hours, 0, 0.0, 0.0, 0.0, False)

    out = run(
        BacktestInput(
            config=config,
            candles=candles,
            start=started,
            end=moment,
            initial_equity=initial_equity,
            oos_label="validation",
            session_id="shadow",
        ),
        gates,
    )

    ready = (
        hours >= gates.min_shadow_hours
        and out.metrics.trade_count >= gates.min_shadow_trades
    )
    return ShadowResult(
        hours_observed=hours,
        trades=out.metrics.trade_count,
        net_pnl_usd=out.metrics.net_return_pct * initial_equity,
        expectancy_r=out.metrics.expectancy_r,
        primary_score=out.metrics.primary_score,
        ready=ready,
        pnl_r=tuple(t.pnl_r for t in out.trades),
    )
