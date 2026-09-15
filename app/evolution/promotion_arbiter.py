"""PromotionArbiter: applies the fixed gates and returns a decision.

This is the only component allowed to return `PROMOTED`, and it never reads
LLM output. It is a pure function of the phase results; the gates live in
`pinned/promotion_gates.py`, where the LLM cannot reach them.

The holdout gate the MVP used to declare is gone rather than faked: there was
no holdout dataset and no loader, so the gate was passing a hardcoded `True`
through and adding nothing but the appearance of rigour. Walk-forward on
non-overlapping windows plus a bootstrap plus a live shadow is what actually
guards this decision today.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pinned.promotion_gates import PROMOTION_GATES, PromotionGates

from .bootstrap import BootstrapResult
from .shadow import ShadowResult
from .walk_forward import FoldResult, mean_test_score, total_test_trades


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
    walk_forward_passed: bool = False
    bootstrap_passed: bool = False
    shadow_ready: bool = False
    shadow_beat_champion: bool = False
    detail: str = ""

    @property
    def promoted(self) -> bool:
        return self.decision is DecisionKind.PROMOTED


def arbitrate(
    *,
    experiment_id: int,
    champion_wf_score: float,
    challenger_folds: Sequence[FoldResult],
    bootstrap: BootstrapResult | None,
    shadow: ShadowResult | None,
    champion_shadow: ShadowResult | None = None,
    gates: PromotionGates = PROMOTION_GATES,
    now: datetime | None = None,
) -> PromotionDecision:
    """Apply every gate in order. `PROMOTED` requires all of them to pass."""
    moment = now or datetime.now(tz=UTC)

    def decide(
        kind: DecisionKind,
        codes: list[str],
        *,
        score: float = 0.0,
        wf: bool = False,
        boot: bool = False,
        ready: bool = False,
        beat: bool = False,
        detail: str = "",
    ) -> PromotionDecision:
        return PromotionDecision(
            experiment_id=experiment_id,
            decision=kind,
            reason_codes=codes,
            decided_at=moment,
            champion_score=champion_wf_score,
            challenger_score=score,
            walk_forward_passed=wf,
            bootstrap_passed=boot,
            shadow_ready=ready,
            shadow_beat_champion=beat,
            detail=detail,
        )

    if not challenger_folds:
        return decide(DecisionKind.REJECTED, ["NO_FOLDS"], detail="no walk-forward folds ran")

    challenger_score = mean_test_score(challenger_folds)

    # Gate 1: enough out-of-sample trades to say anything at all.
    trades = total_test_trades(challenger_folds)
    if trades < gates.min_trades_walk_forward:
        return decide(
            DecisionKind.DEFERRED,
            ["INSUFFICIENT_TRADES"],
            score=challenger_score,
            detail=f"{trades} out-of-sample trades < {gates.min_trades_walk_forward}",
        )

    # Gate 2: walk-forward relative performance.
    if champion_wf_score > 0:
        relative = challenger_score / champion_wf_score
    else:
        relative = 1.0 if challenger_score > 0 else 0.0
    if relative < gates.min_relative_perf:
        return decide(
            DecisionKind.REJECTED,
            ["WF_BELOW_FLOOR"],
            score=challenger_score,
            detail=f"relative performance {relative:.3f} < {gates.min_relative_perf}",
        )

    # Gate 3: the edge must survive resampling.
    if bootstrap is None or not bootstrap.passes:
        p = bootstrap.p_positive if bootstrap else 0.0
        return decide(
            DecisionKind.REJECTED,
            ["BOOTSTRAP_BELOW_THRESHOLD"],
            score=challenger_score,
            wf=True,
            detail=f"P(profitable) {p:.3f} < {gates.bootstrap_min_p_positive}",
        )

    # Gate 4: enough live shadow observation.
    if shadow is None or not shadow.ready:
        hours = shadow.hours_observed if shadow else 0.0
        count = shadow.trades if shadow else 0
        return decide(
            DecisionKind.DEFERRED,
            ["SHADOW_NOT_READY"],
            score=challenger_score,
            wf=True,
            boot=True,
            detail=(
                f"shadow has {hours:.1f}h / {count} trades; needs "
                f"{gates.min_shadow_hours}h / {gates.min_shadow_trades}"
            ),
        )

    # Gate 5: the shadow must actually have beaten the champion live.
    if champion_shadow is not None and shadow.expectancy_r <= champion_shadow.expectancy_r:
        return decide(
            DecisionKind.REJECTED,
            ["SHADOW_BELOW_CHAMPION"],
            score=challenger_score,
            wf=True,
            boot=True,
            ready=True,
            detail=(
                f"shadow expectancy {shadow.expectancy_r:.3f}R did not beat champion "
                f"{champion_shadow.expectancy_r:.3f}R"
            ),
        )

    return decide(
        DecisionKind.PROMOTED,
        [],
        score=challenger_score,
        wf=True,
        boot=True,
        ready=True,
        beat=True,
        detail="every gate passed",
    )
