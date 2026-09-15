"""Walk-forward validation.

Folds are measured in days, not months: an M15 strategy generates enough
trades in three weeks to say something, and the old month-based windows
needed years of history that this instrument does not have.

Each fold trains on `train_days` and tests on the following `test_days`; the
candidate's score is the mean of its out-of-sample test scores.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.strategy.configs import StrategyConfig
from pinned.promotion_gates import PROMOTION_GATES, PromotionGates

from .backtester import BacktestInput, PerformanceMetrics, run


@dataclass(frozen=True)
class FoldResult:
    fold_index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def make_folds(
    start: datetime,
    end: datetime,
    *,
    train_days: int,
    test_days: int,
    folds: int,
) -> list[tuple[datetime, datetime, datetime, datetime]]:
    """Generate (train_start, train_end, test_start, test_end) tuples.

    Folds roll forward by `test_days`, so test windows never overlap: reusing
    the same out-of-sample data across folds would make the mean test score a
    measure of one lucky window rather than of stability.
    """
    start, end = _utc(start), _utc(end)
    out: list[tuple[datetime, datetime, datetime, datetime]] = []
    cursor = start
    for _ in range(folds):
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        out.append((train_start, train_end, test_start, test_end))
        cursor = cursor + timedelta(days=test_days)
    return out


def run_folds(
    candles: pd.DataFrame,
    config: StrategyConfig,
    initial_equity: float,
    *,
    gates: PromotionGates = PROMOTION_GATES,
    train_days: int | None = None,
    test_days: int | None = None,
    folds: int | None = None,
    label: str = "wf",
) -> list[FoldResult]:
    """Run a deterministic walk-forward over the candles."""
    if candles.empty:
        return []

    idx = candles.index
    start = _utc(pd.Timestamp(idx[0]).to_pydatetime())
    end = _utc(pd.Timestamp(idx[-1]).to_pydatetime())

    specs = make_folds(
        start,
        end,
        train_days=train_days if train_days is not None else gates.walk_forward_train_days,
        test_days=test_days if test_days is not None else gates.walk_forward_test_days,
        folds=folds if folds is not None else gates.walk_forward_folds,
    )

    results: list[FoldResult] = []
    for i, (ts, te, vs, ve) in enumerate(specs):
        train = run(
            BacktestInput(
                config=config,
                candles=candles,
                start=ts,
                end=te,
                initial_equity=initial_equity,
                oos_label="train",
                session_id=f"{label}-train-{i}",
            ),
            gates,
        )
        test = run(
            BacktestInput(
                config=config,
                candles=candles,
                start=vs,
                end=ve,
                initial_equity=initial_equity,
                oos_label="validation",
                session_id=f"{label}-test-{i}",
            ),
            gates,
        )
        results.append(
            FoldResult(
                fold_index=i,
                train_start=ts,
                train_end=te,
                test_start=vs,
                test_end=ve,
                train_metrics=train.metrics,
                test_metrics=test.metrics,
            )
        )
    return results


def mean_test_score(results: Sequence[FoldResult]) -> float:
    if not results:
        return 0.0
    return sum(r.test_metrics.primary_score for r in results) / len(results)


def total_test_trades(results: Sequence[FoldResult]) -> int:
    return sum(r.test_metrics.trade_count for r in results)


def test_pnl_r(results: Sequence[FoldResult]) -> list[float]:
    """Expectancy per fold, used as the bootstrap sample when trades are not
    individually retained."""
    return [r.test_metrics.expectancy_r for r in results]
