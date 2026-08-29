"""Tests for challenger_builder, walk_forward, monte_carlo, shadow,
promotion_arbiter, p_hacking, and a basic journal hash-chain test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app.evolution.backtester import BacktestInput, Trade, run as run_backtest
from app.evolution.challenger_builder import (
    ChallengerError,
    HypothesisCandidate,
    apply_candidate,
)
from app.evolution.monte_carlo import run as run_monte_carlo
from app.evolution.p_hacking import update as update_p_hacking, enforce as enforce_p_hacking
from app.evolution.promotion_arbiter import DecisionKind, arbitrate
from app.evolution.walk_forward import run_folds
from app.journal.hash_chain import compute_hash, verify_chain
from app.journal.types import JournalEntry, JournalKind, JournalState
from app.risk.types import Side
from app.strategy.configs import StrategyConfig


def test_apply_candidate_accepts_mutable_field() -> None:
    src = Path("pinned/strategies/ema_atr_xauusd_h1_v1.json")
    cfg = StrategyConfig.from_json_file(src)
    cand = HypothesisCandidate(
        field="fast_ema_length",
        from_value=20,
        to_value=18,
        reason="explore",
        proposed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        weekly_review_id=1,
    )
    new = apply_candidate(cfg, cand)
    assert new.fast_ema_length == 18
    assert new.canonical_hash() != cfg.canonical_hash()


def test_apply_candidate_rejects_immutable_field() -> None:
    src = Path("pinned/strategies/ema_atr_xauusd_h1_v1.json")
    cfg = StrategyConfig.from_json_file(src)
    cand = HypothesisCandidate(
        field="mandatory_stop_loss",  # not in the allow-list
        from_value=True,
        to_value=False,
        reason="bypass",
        proposed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        weekly_review_id=1,
    )
    with pytest.raises(ChallengerError):
        apply_candidate(cfg, cand)


def test_apply_candidate_rejects_mismatched_from_value() -> None:
    src = Path("pinned/strategies/ema_atr_xauusd_h1_v1.json")
    cfg = StrategyConfig.from_json_file(src)
    cand = HypothesisCandidate(
        field="fast_ema_length",
        from_value=99,            # not the current value
        to_value=18,
        reason="stale",
        proposed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        weekly_review_id=1,
    )
    with pytest.raises(ChallengerError):
        apply_candidate(cfg, cand)


def test_monte_carlo_empty_trades() -> None:
    from app.evolution.monte_carlo import MonteCarloResult
    r = run_monte_carlo([])
    assert isinstance(r, MonteCarloResult)
    assert r.passes is False


def test_p_hacking_increments_on_failure() -> None:
    s = update_p_hacking(champion_id=1, current_attempts=0, candidate_failed=True)
    assert s.attempts == 1
    assert s.is_plateau is False
    s2 = update_p_hacking(champion_id=1, current_attempts=s.attempts, candidate_failed=True)
    s3 = update_p_hacking(champion_id=1, current_attempts=s2.attempts, candidate_failed=True)
    s4 = update_p_hacking(champion_id=1, current_attempts=s3.attempts, candidate_failed=True)
    assert s4.is_plateau is True
    passes, codes = enforce_p_hacking(s4, has_different_evidence=False)
    assert not passes
    assert "POTENTIAL_P_HACKING" in codes


def test_p_hacking_resets_on_success() -> None:
    s = update_p_hacking(champion_id=1, current_attempts=3, candidate_failed=True)
    s2 = update_p_hacking(champion_id=1, current_attempts=s.attempts, candidate_failed=False)
    assert s2.attempts == 0


def test_hash_chain_detects_tamper() -> None:
    e1 = JournalEntry(
        id=1, ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
        kind=JournalKind.SYSTEM_EVENT, session_id=uuid4(),
        payload={"event": "a"},
        hash=compute_hash(ts_iso="2025-01-01T00:00:00+00:00", kind="system_event", payload={"event": "a"}, prev_hash=None),
    )
    e2 = JournalEntry(
        id=2, ts=datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
        kind=JournalKind.SYSTEM_EVENT, session_id=e1.session_id,
        payload={"event": "b"},
        hash=compute_hash(ts_iso="2025-01-01T00:01:00+00:00", kind="system_event", payload={"event": "b"}, prev_hash=e1.hash),
    )
    ok, bad = verify_chain([e1, e2])
    assert ok and bad is None
    # Tamper e2's payload
    tampered = e2.model_copy(update={"payload": {"event": "X"}})
    ok, bad = verify_chain([e1, tampered])
    assert not ok
    assert bad == "2"


def test_promotion_arbiter_rejects_when_wf_below_floor() -> None:
    from app.evolution.monte_carlo import MonteCarloResult
    from app.evolution.shadow import ShadowResult
    from app.evolution.walk_forward import FoldResult
    from app.evolution.backtester import PerformanceMetrics
    from app.evolution.promotion_arbiter import PromotionDecision

    # Construct a synthetic fold result with a low primary score
    low_metrics = PerformanceMetrics(
        net_return_pct=-0.05, expectancy_r=-0.2, max_drawdown_pct=0.20,
        sharpe=-0.5, profit_factor=0.5, trade_count=10, consistency_score=0.2,
        oos_score=0.5, complexity_penalty=0.01, primary_score=0.1, tie_break_score=-0.5,
    )
    high_metrics = PerformanceMetrics(
        net_return_pct=0.10, expectancy_r=0.5, max_drawdown_pct=0.10,
        sharpe=1.0, profit_factor=1.5, trade_count=30, consistency_score=0.7,
        oos_score=0.8, complexity_penalty=0.01, primary_score=2.0, tie_break_score=1.0,
    )
    fold = FoldResult(
        fold_index=0,
        train_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        train_end=datetime(2024, 3, 1, tzinfo=timezone.utc),
        test_start=datetime(2024, 3, 1, tzinfo=timezone.utc),
        test_end=datetime(2024, 6, 1, tzinfo=timezone.utc),
        train_metrics=high_metrics, test_metrics=low_metrics,
    )
    d = arbitrate(
        experiment_id=1, champion_wf_score=2.0,
        challenger_folds=[fold],
        challenger_holdout_score=0.0, challenger_holdout_passes=True,
        monte_carlo=MonteCarloResult(iterations=1000, actual_pnl=100.0, percentile=0.9, threshold=0.8, passes=True),
        shadow=ShadowResult(hours_observed=300, trades=40, net_pnl_usd=50.0, ready=True),
    )
    assert d.decision in (DecisionKind.REJECTED, DecisionKind.DEFERRED)
    assert "WF_BELOW_FLOOR" in d.reason_codes


def test_walk_forward_runs() -> None:
    cfg = StrategyConfig.from_json_file(Path("pinned/strategies/ema_atr_xauusd_h1_v1.json"))
    rng = np.random.default_rng(0)
    idx = pd.date_range("2020-01-01", periods=4000, freq="h", tz="UTC")
    closes = 2000.0 + np.arange(4000) * 0.01 + rng.normal(scale=2.0, size=4000).cumsum() * 0.1
    candles = pd.DataFrame({
        "o": closes - 0.2, "h": closes + 1, "l": closes - 1, "c": closes, "v": 0,
    }, index=idx)
    folds = run_folds(candles, cfg, initial_equity=10_000.0, train_months=2, test_months=3, folds=3)
    assert len(folds) >= 1
    for f in folds:
        assert f.train_metrics.trade_count >= 0
        assert f.test_metrics.trade_count >= 0
