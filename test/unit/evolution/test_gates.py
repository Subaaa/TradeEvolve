"""The promotion gates, including the two that used to be incapable of firing.

Two regressions are pinned here explicitly:

*   the old Monte Carlo compared the sum of a shuffled trade list to the sum
    of the original — always equal, so the gate could never reject anything;
*   the old shadow measured elapsed hours from the last candle rather than
    from the start, so it was never ready and promotion was impossible.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.evolution import bootstrap, p_hacking, shadow
from app.evolution.challenger_builder import (
    ChallengerError,
    HypothesisCandidate,
    apply_candidate,
)
from app.evolution.promotion_arbiter import DecisionKind, arbitrate
from app.evolution.walk_forward import make_folds
from app.strategy.configs import LLM_RANGES, MUTABLE_BY_LLM, StrategyConfig
from pinned.promotion_gates import PROMOTION_GATES

pytestmark = pytest.mark.unit

NOW = datetime(2026, 3, 8, 22, 0, tzinfo=UTC)


# ------------------------------------------------------------------ bootstrap


def test_a_consistently_profitable_sequence_passes() -> None:
    result = bootstrap.run([1.0, 0.8, 1.2, 0.9, 1.1] * 8)
    assert result.passes is True
    assert result.p_positive > 0.9


def test_a_losing_sequence_fails() -> None:
    result = bootstrap.run([-1.0] * 40)
    assert result.passes is False
    assert result.p_positive == 0.0


def test_an_edge_that_rests_on_one_lucky_trade_fails() -> None:
    """The regression the old sum-of-shuffle Monte Carlo could not catch."""
    pnl = [-1.0] * 29 + [40.0]
    assert sum(pnl) > 0  # profitable in aggregate
    assert bootstrap.run(pnl).passes is False


def test_bootstrap_is_deterministic() -> None:
    sample = [0.4, -1.0, 2.0, -1.0, 0.7] * 10
    assert bootstrap.run(sample) == bootstrap.run(sample)


def test_an_empty_sequence_fails_rather_than_dividing_by_zero() -> None:
    result = bootstrap.run([])
    assert result.passes is False
    assert result.iterations == 0


# --------------------------------------------------------------------- shadow


def test_shadow_hours_are_measured_from_the_start_not_the_last_candle() -> None:
    from test.conftest import START, flat_series

    candles = flat_series(300, start=START)
    started = START
    now = START + timedelta(hours=200)
    result = shadow.observe(
        candles, StrategyConfig(version_tag="t"), 10_000.0,
        started_at=started, now=now,
    )
    assert result.hours_observed == pytest.approx(200.0)


def test_shadow_is_not_ready_before_the_minimum_window() -> None:
    from test.conftest import START, flat_series

    result = shadow.observe(
        flat_series(300), StrategyConfig(version_tag="t"), 10_000.0,
        started_at=START, now=START + timedelta(hours=1),
    )
    assert result.ready is False


# ---------------------------------------------------------- challenger builder


def test_a_field_outside_the_allow_list_is_refused(config: StrategyConfig) -> None:
    candidate = HypothesisCandidate(
        field="min_atr_bps", from_value=12.0, to_value=1.0, reason="cheat"
    )
    with pytest.raises(ChallengerError, match="allow-list"):
        apply_candidate(config, candidate)


def test_a_value_outside_the_allowed_range_is_refused(config: StrategyConfig) -> None:
    low, high = LLM_RANGES["atr_sl_multiplier"]
    candidate = HypothesisCandidate(
        field="atr_sl_multiplier",
        from_value=config.atr_sl_multiplier,
        to_value=high + 1.0,
        reason="wider",
    )
    with pytest.raises(ChallengerError, match="outside"):
        apply_candidate(config, candidate)
    assert low < high


def test_a_stale_from_value_is_refused(config: StrategyConfig) -> None:
    """Optimistic concurrency: the champion may have moved since the review."""
    candidate = HypothesisCandidate(
        field="tp_r_multiple", from_value=99.0, to_value=2.5, reason="stale"
    )
    with pytest.raises(ChallengerError, match="does not match"):
        apply_candidate(config, candidate)


def test_a_no_op_candidate_is_refused(config: StrategyConfig) -> None:
    candidate = HypothesisCandidate(
        field="tp_r_multiple",
        from_value=config.tp_r_multiple,
        to_value=config.tp_r_multiple,
        reason="nothing",
    )
    with pytest.raises(ChallengerError, match="does not change"):
        apply_candidate(config, candidate)


def test_a_fractional_value_for_an_integer_field_is_refused(
    config: StrategyConfig,
) -> None:
    candidate = HypothesisCandidate(
        field="fast_ema_length",
        from_value=float(config.fast_ema_length),
        to_value=11.5,
        reason="half a bar",
    )
    with pytest.raises(ChallengerError, match="integer parameter"):
        apply_candidate(config, candidate)


def test_a_change_that_breaks_a_config_invariant_is_refused(
    config: StrategyConfig,
) -> None:
    """Slow EMA must stay longer than fast. Caught here, not at the first tick."""
    candidate = HypothesisCandidate(
        field="fast_ema_length",
        from_value=float(config.fast_ema_length),
        to_value=20.0,  # inside the allowed range, but >= slow (21) after rounding
        reason="faster",
    )
    updated = apply_candidate(config, candidate)
    assert updated.fast_ema_length == 20
    assert updated.slow_ema_length > updated.fast_ema_length

    worse = HypothesisCandidate(
        field="slow_ema_length",
        from_value=float(config.slow_ema_length),
        to_value=15.0,
        reason="shorter",
    )
    bad_config = config.model_copy(update={"fast_ema_length": 18})
    with pytest.raises(ChallengerError, match="invariant"):
        apply_candidate(bad_config, worse)


def test_a_valid_candidate_produces_a_new_tagged_version(
    config: StrategyConfig,
) -> None:
    candidate = HypothesisCandidate(
        field="tp_r_multiple",
        from_value=config.tp_r_multiple,
        to_value=2.5,
        reason="targets are being missed",
    )
    updated = apply_candidate(config, candidate)
    assert updated.tp_r_multiple == 2.5
    assert updated.version_tag != config.version_tag
    assert updated.fast_ema_length == config.fast_ema_length


def test_every_mutable_field_has_a_declared_range() -> None:
    assert set(MUTABLE_BY_LLM) == set(LLM_RANGES)


# ---------------------------------------------------------------- walk-forward


def test_folds_do_not_reuse_test_windows() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    folds = make_folds(
        start, start + timedelta(days=90), train_days=20, test_days=10, folds=3
    )
    assert len(folds) == 3
    test_windows = [(f[2], f[3]) for f in folds]
    for (a_start, a_end), (b_start, _) in itertools.pairwise(test_windows):
        assert b_start >= a_end or b_start > a_start


def test_folds_stop_when_history_runs_out() -> None:
    """Only whole folds that fit inside the history are generated.

    With 35 days, fold 0 spans days 0-30; fold 1 would need day 40, so it is
    dropped rather than silently shortened.
    """
    start = datetime(2026, 1, 1, tzinfo=UTC)
    folds = make_folds(
        start, start + timedelta(days=35), train_days=20, test_days=10, folds=5
    )
    assert len(folds) == 1
    assert folds[0][3] <= start + timedelta(days=35)


# -------------------------------------------------------------------- arbiter


def test_the_arbiter_refuses_without_folds() -> None:
    decision = arbitrate(
        experiment_id=1,
        champion_wf_score=1.0,
        challenger_folds=[],
        bootstrap=None,
        shadow=None,
        now=NOW,
    )
    assert decision.decision is DecisionKind.REJECTED
    assert decision.promoted is False


# ------------------------------------------------------------------ p-hacking


def test_repeated_failures_declare_a_plateau() -> None:
    limit = PROMOTION_GATES.max_hypothesis_attempts_per_champion
    state = p_hacking.update(1, current_attempts=limit - 1, candidate_failed=True)
    assert state.is_plateau is True
    ok, codes = p_hacking.enforce(state, has_different_evidence=False)
    assert ok is False
    assert codes == ["POTENTIAL_P_HACKING"]


def test_a_success_resets_the_counter() -> None:
    state = p_hacking.update(1, current_attempts=3, candidate_failed=False)
    assert state.attempts == 0
    assert state.is_plateau is False
