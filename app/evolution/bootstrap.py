"""Bootstrap resampling of a trade sequence.

This replaces the old `monte_carlo.py`, which shuffled the trade list and
compared the sum of the shuffled PnLs to the sum of the original. Addition is
commutative, so those two numbers were always equal: `better == iterations`,
the percentile was always 0.0, and the gate could never pass. The promotion
pipeline had a gate that was mathematically incapable of ever letting a
challenger through.

What we actually want to know is whether the edge survives resampling: draw
`n` trades with replacement, many times, and ask how often the resampled
sequence is profitable. A strategy whose result depends on two lucky trades
fails this; a strategy with a real if modest edge passes.
"""
from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from pinned.promotion_gates import PROMOTION_GATES, PromotionGates


@dataclass(frozen=True)
class BootstrapResult:
    iterations: int
    p_positive: float
    p05_net_r: float
    median_net_r: float
    passes: bool


def run(
    pnl_r: Sequence[float],
    *,
    iterations: int | None = None,
    seed: int | None = None,
    gates: PromotionGates = PROMOTION_GATES,
) -> BootstrapResult:
    """Resample `pnl_r` with replacement and summarise the distribution.

    Args:
        pnl_r: per-trade results in R multiples.
        iterations: resamples to draw; defaults to the pinned gate value.
        seed: RNG seed; defaults to the pinned seed so runs are reproducible.
    """
    n = len(pnl_r)
    if n == 0:
        return BootstrapResult(0, 0.0, 0.0, 0.0, False)

    iters = iterations if iterations is not None else gates.bootstrap_iterations
    rng = random.Random(gates.bootstrap_seed if seed is None else seed)

    totals: list[float] = []
    values = list(pnl_r)
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        totals.append(total)

    totals.sort()
    positive = sum(1 for t in totals if t > 0.0)
    p_positive = positive / iters
    p05_index = max(0, int(0.05 * iters) - 1)
    median_index = iters // 2

    return BootstrapResult(
        iterations=iters,
        p_positive=p_positive,
        p05_net_r=totals[p05_index],
        median_net_r=totals[median_index],
        passes=p_positive >= gates.bootstrap_min_p_positive,
    )
