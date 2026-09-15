"""The weekly evolution loop, end to end.

The pieces for this existed before — backtester, walk-forward, arbiter,
allow-list — but nothing joined them: the two scheduled jobs that were meant
to run this were `logger.debug` heartbeats, and the champion was read from a
file that no promotion could ever change.

The loop, once a week:

    stats -> LLM proposes at most one bounded change -> deterministic
    validation -> challenger version -> walk-forward vs the champion ->
    bootstrap -> shadow

and then, once a day, `evaluate_shadow` asks the arbiter whether the shadow
has earned promotion.

Every rejection is journaled with its reason. A hypothesis that never gets
explained is a hypothesis that gets proposed again next week.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from app.db import candles as candles_db
from app.db import experiments as exp_db
from app.db import strategy_versions as sv_db
from app.db.strategy_versions import StrategyVersion, VersionStatus
from app.evolution import bootstrap, p_hacking, shadow, walk_forward
from app.journal.payloads import ExperimentEventPayload
from app.journal.types import JournalKind
from app.journal.writer import JournalWriter
from app.llm.client import LlmClient
from app.llm.provider import LlmError
from app.llm.schemas import WeeklyReviewOut
from app.strategy.configs import StrategyConfig
from pinned.promotion_gates import PROMOTION_GATES, PromotionGates

from .challenger_builder import ChallengerError, HypothesisCandidate, apply_candidate
from .promotion_arbiter import DecisionKind, PromotionDecision
from .stats import build_weekly_stats

logger = logging.getLogger(__name__)

# History used for walk-forward. Three 20/10-day folds need 50 days; 90 gives
# the folds room to roll forward without reusing test windows.
WALK_FORWARD_DAYS = 90


@dataclass(frozen=True)
class EvolutionResult:
    accepted: bool
    detail: str
    hypothesis_id: int | None = None
    challenger_id: int | None = None
    experiment_id: int | None = None
    candidate: HypothesisCandidate | None = None


def run_weekly_evolution(
    conn: sqlite3.Connection,
    llm: LlmClient,
    journal: JournalWriter,
    *,
    instrument: str,
    granularity: str,
    initial_equity: float,
    now: datetime | None = None,
    gates: PromotionGates = PROMOTION_GATES,
    dry_run: bool = False,
) -> EvolutionResult:
    """Propose, validate and stage one challenger. Never promotes."""
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)

    champion = sv_db.load_champion(conn)
    if champion is None:
        return EvolutionResult(False, "no champion is configured")

    if exp_db.active_experiment(conn) is not None:
        return EvolutionResult(
            False, "an experiment is already in progress; not starting another"
        )

    attempts = exp_db.failed_attempts_for_champion(conn, champion.id)
    plateau = p_hacking.PlateauState(
        champion_id=champion.id,
        attempts=attempts,
        is_plateau=attempts >= gates.max_hypothesis_attempts_per_champion,
        next_must_be_different_evidence=attempts
        >= gates.max_hypothesis_attempts_per_champion,
    )

    stats = build_weekly_stats(conn, champion=champion, now=moment, plateau=plateau)

    try:
        review = llm.call(
            site="weekly_review", payload=stats, output_model=WeeklyReviewOut
        )
    except LlmError as exc:
        _journal(journal, "weekly_review_failed", champion, detail=str(exc))
        return EvolutionResult(False, f"weekly review failed: {exc}")

    review_id = exp_db.insert_weekly_review(
        conn,
        period_start=moment - timedelta(days=7),
        period_end=moment,
        payload_in=stats.model_dump(mode="json"),
        payload_out=review.model_dump(mode="json"),
    )

    if review.candidate is None:
        detail = review.blocked_reason or "the review proposed no change"
        _journal(journal, "no_candidate", champion, detail=detail)
        return EvolutionResult(False, detail)

    candidate = HypothesisCandidate(
        field=review.candidate.field,
        from_value=review.candidate.from_value,
        to_value=review.candidate.to_value,
        reason=review.candidate.reason,
        proposed_at=moment,
        weekly_review_id=review_id,
    )

    # The allow-list, the range and the from_value are re-checked here even
    # though the schema constrained the field name. The schema is convenience;
    # this is the guard.
    try:
        challenger_config = apply_candidate(champion.config, candidate)
    except ChallengerError as exc:
        exp_db.insert_hypothesis(
            conn,
            weekly_review_id=review_id,
            champion_id=champion.id,
            field=candidate.field,
            from_value=candidate.from_value,
            to_value=candidate.to_value,
            reason=candidate.reason,
            accepted=False,
            reject_reason=str(exc),
        )
        _journal(journal, "candidate_rejected", champion, detail=str(exc))
        return EvolutionResult(False, f"candidate rejected: {exc}", candidate=candidate)

    if plateau.is_plateau:
        ok, codes = p_hacking.enforce(plateau, has_different_evidence=False)
        if not ok:
            detail = (
                f"{attempts} hypotheses against this champion have already failed; "
                "refusing another variation on the same idea"
            )
            exp_db.insert_hypothesis(
                conn,
                weekly_review_id=review_id,
                champion_id=champion.id,
                field=candidate.field,
                from_value=candidate.from_value,
                to_value=candidate.to_value,
                reason=candidate.reason,
                accepted=False,
                reject_reason=",".join(codes),
            )
            _journal(journal, "plateau_block", champion, detail=detail, codes=codes)
            return EvolutionResult(False, detail, candidate=candidate)

    history = candles_db.read_bars(
        conn,
        instrument,
        granularity,
        start=moment - timedelta(days=WALK_FORWARD_DAYS),
        end=moment,
    )
    phases = _run_phases(
        history,
        champion_config=champion.config,
        challenger_config=challenger_config,
        initial_equity=initial_equity,
        gates=gates,
    )

    if dry_run:
        # Report what would actually happen, not merely that a candidate was
        # produced: a dry run that says "would stage" while the offline gates
        # rejected the challenger is worse than no dry run at all.
        passed = bool(phases["passes_offline"])
        outcome = (
            f"would stage {challenger_config.version_tag} for shadow trading"
            if passed
            else f"would reject {challenger_config.version_tag}"
            f" ({', '.join(phases['reason_codes'])})"
        )
        return EvolutionResult(
            passed,
            f"dry run: {outcome} — {phases['verdict']}",
            candidate=candidate,
        )

    hypothesis_id = exp_db.insert_hypothesis(
        conn,
        weekly_review_id=review_id,
        champion_id=champion.id,
        field=candidate.field,
        from_value=candidate.from_value,
        to_value=candidate.to_value,
        reason=candidate.reason,
        accepted=True,
    )

    if not phases["passes_offline"]:
        offline_codes: list[str] = list(phases["reason_codes"])
        challenger_id = sv_db.insert_version(
            conn,
            challenger_config,
            status=VersionStatus.REJECTED,
            created_by="llm:weekly_review",
            parent_version_id=champion.id,
        )
        experiment_id = exp_db.insert_experiment(
            conn,
            hypothesis_id=hypothesis_id,
            champion_id=champion.id,
            challenger_id=challenger_id,
            phases=phases,
        )
        exp_db.decide(
            conn,
            experiment_id,
            decision=DecisionKind.REJECTED.value,
            reason_codes=list(offline_codes),
        )
        _journal(
            journal,
            "challenger_rejected_offline",
            champion,
            detail=str(phases["verdict"]),
            codes=list(offline_codes),
            challenger_id=challenger_id,
            experiment_id=experiment_id,
        )
        return EvolutionResult(
            False,
            f"challenger failed offline validation: {phases['verdict']}",
            hypothesis_id=hypothesis_id,
            challenger_id=challenger_id,
            experiment_id=experiment_id,
            candidate=candidate,
        )

    challenger_id = sv_db.insert_version(
        conn,
        challenger_config,
        status=VersionStatus.SHADOW,
        created_by="llm:weekly_review",
        parent_version_id=champion.id,
    )
    experiment_id = exp_db.insert_experiment(
        conn,
        hypothesis_id=hypothesis_id,
        champion_id=champion.id,
        challenger_id=challenger_id,
        phases=phases,
        shadow_started_at=moment,
    )
    _journal(
        journal,
        "challenger_staged",
        champion,
        detail=(
            f"{candidate.field}: {candidate.from_value} -> {candidate.to_value}; "
            f"shadow trading begins"
        ),
        challenger_id=challenger_id,
        experiment_id=experiment_id,
    )
    return EvolutionResult(
        True,
        f"challenger {challenger_config.version_tag} staged for shadow trading",
        hypothesis_id=hypothesis_id,
        challenger_id=challenger_id,
        experiment_id=experiment_id,
        candidate=candidate,
    )


def _run_phases(
    history: pd.DataFrame,
    *,
    champion_config: StrategyConfig,
    challenger_config: StrategyConfig,
    initial_equity: float,
    gates: PromotionGates,
) -> dict[str, Any]:
    """Walk-forward both configs and bootstrap the challenger."""
    if history.empty:
        return {
            "verdict": "no candle history available for walk-forward",
            "passes_offline": False,
            "reason_codes": ["NO_HISTORY"],
        }

    champion_folds = walk_forward.run_folds(
        history, champion_config, initial_equity, gates=gates, label="wf-champ"
    )
    challenger_folds = walk_forward.run_folds(
        history, challenger_config, initial_equity, gates=gates, label="wf-chal"
    )

    champion_score = walk_forward.mean_test_score(champion_folds)
    challenger_score = walk_forward.mean_test_score(challenger_folds)
    trades = walk_forward.total_test_trades(challenger_folds)

    boot = bootstrap.run(walk_forward.test_pnl_r(challenger_folds), gates=gates)

    reason_codes: list[str] = []
    if not challenger_folds:
        reason_codes.append("NO_FOLDS")
    if trades < gates.min_trades_walk_forward:
        reason_codes.append("INSUFFICIENT_TRADES")
    if (champion_score > 0 and challenger_score / champion_score < gates.min_relative_perf) or (champion_score <= 0 and challenger_score <= 0):
        reason_codes.append("WF_BELOW_FLOOR")

    passes = not reason_codes
    verdict = (
        f"champion {champion_score:.4f} vs challenger {challenger_score:.4f} "
        f"over {len(challenger_folds)} folds, {trades} out-of-sample trades"
    )
    return {
        "verdict": verdict,
        "passes_offline": passes,
        "reason_codes": reason_codes,
        "champion_wf_score": champion_score,
        "challenger_wf_score": challenger_score,
        "challenger_trades": trades,
        "folds": len(challenger_folds),
        "bootstrap_p_positive": boot.p_positive,
    }


def evaluate_shadow(
    conn: sqlite3.Connection,
    journal: JournalWriter,
    *,
    instrument: str,
    granularity: str,
    initial_equity: float,
    now: datetime | None = None,
    gates: PromotionGates = PROMOTION_GATES,
) -> str:
    """Ask the arbiter whether the staged challenger has earned promotion."""
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    experiment = exp_db.active_experiment(conn)
    if experiment is None or experiment.shadow_started_at is None:
        return "no experiment in shadow"

    champion = sv_db.get(conn, experiment.champion_id)
    challenger = sv_db.get(conn, experiment.challenger_id)
    if champion is None or challenger is None:
        return "experiment references a missing strategy version"

    window = candles_db.read_bars(
        conn,
        instrument,
        granularity,
        start=experiment.shadow_started_at,
        end=moment,
    )
    challenger_shadow = shadow.observe(
        window,
        challenger.config,
        initial_equity,
        started_at=experiment.shadow_started_at,
        now=moment,
        gates=gates,
    )
    champion_shadow = shadow.observe(
        window,
        champion.config,
        initial_equity,
        started_at=experiment.shadow_started_at,
        now=moment,
        gates=gates,
    )

    boot = bootstrap.run(list(challenger_shadow.pnl_r), gates=gates)
    phases = dict(experiment.phases)
    phases["shadow"] = {
        "hours": challenger_shadow.hours_observed,
        "trades": challenger_shadow.trades,
        "expectancy_r": challenger_shadow.expectancy_r,
        "champion_expectancy_r": champion_shadow.expectancy_r,
        "bootstrap_p_positive": boot.p_positive,
    }
    exp_db.update_phases(conn, experiment.id, phases)

    decision = _arbitrate_with_stored_folds(
        experiment_id=experiment.id,
        phases=phases,
        bootstrap_result=boot,
        challenger_shadow=challenger_shadow,
        champion_shadow=champion_shadow,
        gates=gates,
        now=moment,
    )

    if decision.decision is DecisionKind.DEFERRED:
        return f"shadow still running: {decision.detail}"

    exp_db.decide(
        conn,
        experiment.id,
        decision=decision.decision.value,
        reason_codes=decision.reason_codes,
    )

    if decision.promoted:
        sv_db.promote(conn, challenger_id=challenger.id)
        _journal(
            journal,
            "challenger_promoted",
            champion,
            detail=decision.detail,
            challenger_id=challenger.id,
            experiment_id=experiment.id,
            decision=decision.decision.value,
        )
        logger.warning(
            "challenger %s promoted to champion", challenger.config.version_tag
        )
        return f"promoted {challenger.config.version_tag}: {decision.detail}"

    sv_db.set_status(conn, challenger.id, VersionStatus.REJECTED)
    _journal(
        journal,
        "challenger_rejected",
        champion,
        detail=decision.detail,
        codes=decision.reason_codes,
        challenger_id=challenger.id,
        experiment_id=experiment.id,
        decision=decision.decision.value,
    )
    return f"rejected {challenger.config.version_tag}: {decision.detail}"


def _arbitrate_with_stored_folds(
    *,
    experiment_id: int,
    phases: dict[str, Any],
    bootstrap_result: bootstrap.BootstrapResult,
    challenger_shadow: shadow.ShadowResult,
    champion_shadow: shadow.ShadowResult,
    gates: PromotionGates,
    now: datetime,
) -> PromotionDecision:
    """Re-apply the gates using the fold scores recorded at staging time.

    The walk-forward ran a week ago and its `FoldResult` objects are not
    persisted, so the arbiter is given the stored scores directly rather than
    re-running an expensive backtest to reproduce numbers we already have.
    """
    champion_score = float(phases.get("champion_wf_score", 0.0) or 0.0)
    challenger_score = float(phases.get("challenger_wf_score", 0.0) or 0.0)
    trades = int(phases.get("challenger_trades", 0) or 0)

    def decision(
        kind: DecisionKind, codes: list[str], detail: str, **flags: bool
    ) -> PromotionDecision:
        return PromotionDecision(
            experiment_id=experiment_id,
            decision=kind,
            reason_codes=codes,
            decided_at=now,
            champion_score=champion_score,
            challenger_score=challenger_score,
            detail=detail,
            **flags,
        )

    if trades < gates.min_trades_walk_forward:
        return decision(
            DecisionKind.REJECTED,
            ["INSUFFICIENT_TRADES"],
            f"{trades} out-of-sample trades < {gates.min_trades_walk_forward}",
        )

    relative = challenger_score / champion_score if champion_score > 0 else (
        1.0 if challenger_score > 0 else 0.0
    )
    if relative < gates.min_relative_perf:
        return decision(
            DecisionKind.REJECTED,
            ["WF_BELOW_FLOOR"],
            f"relative performance {relative:.3f} < {gates.min_relative_perf}",
        )

    if not challenger_shadow.ready:
        return decision(
            DecisionKind.DEFERRED,
            ["SHADOW_NOT_READY"],
            (
                f"shadow has {challenger_shadow.hours_observed:.1f}h / "
                f"{challenger_shadow.trades} trades; needs {gates.min_shadow_hours}h / "
                f"{gates.min_shadow_trades}"
            ),
            walk_forward_passed=True,
        )

    if not bootstrap_result.passes:
        return decision(
            DecisionKind.REJECTED,
            ["BOOTSTRAP_BELOW_THRESHOLD"],
            (
                f"shadow P(profitable) {bootstrap_result.p_positive:.3f} < "
                f"{gates.bootstrap_min_p_positive}"
            ),
            walk_forward_passed=True,
            shadow_ready=True,
        )

    if challenger_shadow.expectancy_r <= champion_shadow.expectancy_r:
        return decision(
            DecisionKind.REJECTED,
            ["SHADOW_BELOW_CHAMPION"],
            (
                f"shadow expectancy {challenger_shadow.expectancy_r:.3f}R did not beat "
                f"champion {champion_shadow.expectancy_r:.3f}R"
            ),
            walk_forward_passed=True,
            bootstrap_passed=True,
            shadow_ready=True,
        )

    return decision(
        DecisionKind.PROMOTED,
        [],
        "every gate passed",
        walk_forward_passed=True,
        bootstrap_passed=True,
        shadow_ready=True,
        shadow_beat_champion=True,
    )


def _journal(
    journal: JournalWriter,
    event: str,
    champion: StrategyVersion,
    *,
    detail: str,
    codes: list[str] | None = None,
    challenger_id: int | None = None,
    experiment_id: int | None = None,
    decision: str | None = None,
) -> None:
    journal.write(
        kind=JournalKind.EXPERIMENT_EVENT,
        payload=ExperimentEventPayload(
            event=event,
            experiment_id=experiment_id,
            champion_id=champion.id,
            challenger_id=challenger_id,
            decision=decision,
            reason_codes=codes or [],
            detail=detail,
        ),
        strategy_version_id=champion.id,
        reason_codes=codes or [],
    )
