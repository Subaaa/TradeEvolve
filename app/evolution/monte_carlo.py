"""Monte Carlo: shuffle a backtest trade list and compare to the actual.

The candidate's actual PnL must be in the 80th percentile of the
shuffled distribution. This is the multiple-testing protection.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .backtester import Trade


@dataclass(frozen=True)
class MonteCarloResult:
    iterations: int
    actual_pnl: float
    percentile: float
    threshold: float
    passes: bool


def run(
    trades: list[Trade],
    *,
    iterations: int = 1000,
    threshold_percentile: float = 0.80,
    seed: int = 42,
) -> MonteCarloResult:
    """Shuffle the trade list's PnL and compute the percentile of the actual PnL.

    We use a numpy generator seeded for determinism; this is the only
    randomness in the validation pipeline.
    """
    if not trades:
        return MonteCarloResult(
            iterations=iterations, actual_pnl=0.0, percentile=0.0,
            threshold=threshold_percentile, passes=False,
        )
    actual = sum(t.pnl_usd for t in trades)
    pnls = [t.pnl_usd for t in trades]
    rng = random.Random(seed)
    better = 0
    for _ in range(iterations):
        rng.shuffle(pnls)
        if sum(pnls) >= actual:
            better += 1
    percentile = 1.0 - (better / iterations)
    return MonteCarloResult(
        iterations=iterations,
        actual_pnl=actual,
        percentile=percentile,
        threshold=threshold_percentile,
        passes=percentile >= threshold_percentile,
    )
