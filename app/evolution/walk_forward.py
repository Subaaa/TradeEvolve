"""Walk-forward validation.

Splits the input window into N folds. Each fold trains on a window of
`train_months` and tests on the next `test_months`. The candidate's
score is the mean of its test scores across folds. The candidate must
be within `MIN_RELATIVE_PERF` of the champion's score.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import pandas as pd

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


def make_folds(
    start: datetime,
    end: datetime,
    *,
    train_months: int,
    test_months: int,
    folds: int,
) -> list[tuple[datetime, datetime, datetime, datetime]]:
    """Generate (train_start, train_end, test_start, test_end) tuples."""
    out: list[tuple[datetime, datetime, datetime, datetime]] = []
    if start.tzinfo is None:
        start = start.replace(tzinfo=__import__("datetime").timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=__import__("datetime").timezone.utc)
    cursor = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    for i in range(folds):
        train_start = cursor
        train_end = cursor + pd.DateOffset(months=train_months)
        test_start = train_end
        test_end = test_start + pd.DateOffset(months=test_months)
        if test_end > end_ts:
            break
        out.append((
            train_start.to_pydatetime(),
            train_end.to_pydatetime(),
            test_start.to_pydatetime(),
            test_end.to_pydatetime(),
        ))
        cursor = test_start
    return out


def run_folds(
    candles: pd.DataFrame,
    config,
    initial_equity: float,
    *,
    train_months: int = 3,
    test_months: int = 6,
    folds: int = 6,
) -> list[FoldResult]:
    """Run a deterministic walk-forward over the candles."""
    if candles.empty:
        return []
    start = candles.index[0].to_pydatetime()
    end = candles.index[-1].to_pydatetime()
    if start.tzinfo is None:
        start = start.replace(tzinfo=__import__("datetime").timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=__import__("datetime").timezone.utc)
    fold_specs = make_folds(start, end, train_months=train_months, test_months=test_months, folds=folds)
    out: list[FoldResult] = []
    for i, (ts, te, vs, ve) in enumerate(fold_specs):
        train_out = run(BacktestInput(
            config=config, candles=candles, start=ts, end=te,
            initial_equity=initial_equity, oos_label="train", session_id=f"wf-train-{i}",
        ))
        test_out = run(BacktestInput(
            config=config, candles=candles, start=vs, end=ve,
            initial_equity=initial_equity, oos_label="validation", session_id=f"wf-test-{i}",
        ))
        out.append(FoldResult(
            fold_index=i,
            train_start=ts, train_end=te,
            test_start=vs, test_end=ve,
            train_metrics=train_out.metrics,
            test_metrics=test_out.metrics,
        ))
    return out


def mean_test_score(results: Sequence[FoldResult]) -> float:
    if not results:
        return 0.0
    return sum(r.test_metrics.primary_score for r in results) / len(results)
