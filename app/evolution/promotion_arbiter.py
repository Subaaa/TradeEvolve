"""PromotionArbiter: applies the fixed gates and writes a PromotionDecision.

This module is the **only** place that may write `final_decision = 'promoted'`.
It never reads LLM output. The gates are in `pinned/promotion_gates.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pinned.promotion_gates import PROMOTION_GATES, PromotionGates
from .monte_carlo import MonteCarloResult
from .shadow import ShadowResult
from .walk_forward import FoldResult


class DecisionKind(StrEnum):
    PROMOTED = "promoted"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class PromotionDecision:
    experiment_id: int
    decision: DecisionKind
    reason_codes: list[str]
    decided_at: datetime
    champion_score: float
    challenger_score: float
    holdout_passed: bool
    monte_carlo_passed: bool
    shadow_ready: bool


def arbitrate(
    *,
    experiment_id: int,
    champion_wf_score: float,
    challenger_folds: list[FoldResult],
    challenger_holdout_score: float,
    challenger_holdout_passes: bool,
    monte_carlo: MonteCarloResult,
    shadow: ShadowResult | None,
    gates: PromotionGates = PROMOTION_GATES,
    now: datetime | None = None,
) -> PromotionDecision:
    """Apply the gates and return a PromotionDecision.

    The decision is never `PROMOTED` unless every gate passes.
    """
    now = now or datetime.utcnow()
    if not challenger_folds:
        return PromotionDecision(
            experiment_id=experiment_id,
            decision=DecisionKind.REJECTED,
            reason_codes=["NO_FOLDS"],
            decided_at=now,
            champion_score=champion_wf_score,
            challenger_score=0.0,
            holdout_passed=False,
            monte_carlo_passed=False,
            shadow_ready=False,
        )

    challenger_score = sum(f.test_metrics.primary_score for f in challenger_folds) / len(challenger_folds)

    # Gate 1: walk-forward relative performance
    if champion_wf_score > 0 and challenger_score / champion_wf_score < gates.min_relative_perf:
        return PromotionDecision(
            experiment_id=experiment_id, decision=DecisionKind.REJECTED,
            reason_codes=["WF_BELOW_FLOOR"], decided_at=now,
            champion_score=champion_wf_score, challenger_score=challenger_score,
            holdout_passed=False, monte_carlo_passed=False, shadow_ready=False,
        )

    # Gate 2: minimum sample
    total_trades = sum(f.test_metrics.trade_count for f in challenger_folds)
    if total_trades < gates.min_trades_walk_forward:
        return PromotionDecision(
            experiment_id=experiment_id, decision=DecisionKind.DEFERRED,
            reason_codes=["INSUFFICIENT_TRADES"], decided_at=now,
            champion_score=champion_wf_score, challenger_score=challenger_score,
            holdout_passed=False, monte_carlo_passed=False, shadow_ready=False,
        )

    # Gate 3: holdout
    if not challenger_holdout_passes:
        return PromotionDecision(
            experiment_id=experiment_id, decision=DecisionKind.REJECTED,
            reason_codes=["HOLDOUT_BELOW_FLOOR"], decided_at=now,
            champion_score=champion_wf_score, challenger_score=challenger_score,
            holdout_passed=False, monte_carlo_passed=False, shadow_ready=False,
        )

    # Gate 4: Monte Carlo
    if not monte_carlo.passes:
        return PromotionDecision(
            experiment_id=experiment_id, decision=DecisionKind.REJECTED,
            reason_codes=["MONTE_CARLO_BELOW_THRESHOLD"], decided_at=now,
            champion_score=champion_wf_score, challenger_score=challenger_score,
            holdout_passed=True, monte_carlo_passed=False, shadow_ready=False,
        )

    # Gate 5: shadow
    if shadow is None or not shadow.ready:
        return PromotionDecision(
            experiment_id=experiment_id, decision=DecisionKind.DEFERRED,
            reason_codes=["SHADOW_NOT_READY"], decided_at=now,
            champion_score=champion_wf_score, challenger_score=challenger_score,
            holdout_passed=True, monte_carlo_passed=True,
            shadow_ready=False,
        )

    return PromotionDecision(
        experiment_id=experiment_id, decision=DecisionKind.PROMOTED,
        reason_codes=[], decided_at=now,
        champion_score=champion_wf_score, challenger_score=challenger_score,
        holdout_passed=True, monte_carlo_passed=True, shadow_ready=True,
    )
