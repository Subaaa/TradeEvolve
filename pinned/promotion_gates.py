"""Promotion gates: fixed thresholds for champion/challenger comparison.

A change here is a human-only code change. The PromotionArbiter is the only
component that imports these constants, and it is the only component that
may promote a challenger to champion.

Values are tuned for M15: far more trades per unit of wall-clock time than
the old H1 configuration, so the windows are measured in days, not months,
and the trade-count floors are reachable inside one evolution cycle.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromotionGates:
    # --- Walk-forward ---
    walk_forward_folds: int = 3
    walk_forward_train_days: int = 20
    walk_forward_test_days: int = 10
    # A challenger must be at least as good as the champion out of sample.
    # 0.95 would promote mild regressions; on a strategy this simple the
    # honest floor is 1.0.
    min_relative_perf: float = 1.0

    # --- Trade-count minimums ---
    min_trades_single_window: int = 10
    min_trades_walk_forward: int = 30

    # --- Bootstrap resampling (replaces the old sum-of-shuffle Monte Carlo,
    #     which was mathematically incapable of ever failing a candidate) ---
    bootstrap_iterations: int = 1000
    bootstrap_min_p_positive: float = 0.80
    bootstrap_seed: int = 20260915

    # --- Shadow paper-trading ---
    min_shadow_hours: int = 120                 # 5 days
    min_shadow_trades: int = 10

    # --- Score weights (primary composite score) ---
    # score = expectancy_r * consistency_score / max_drawdown_pct * (1 - complexity_penalty)
    # tie-break = 0.8 * sharpe + 0.2 * profit_factor
    score_sharpe_weight: float = 0.8
    score_profit_factor_weight: float = 0.2

    # --- Anti-p-hacking ---
    max_hypothesis_attempts_per_champion: int = 4

    # --- Anti-circumvention ---
    # The challenger must never get a more favorable RiskEngine decision than
    # the champion on a structurally identical proposal.
    enforce_risk_engine_fairness: bool = True


# Module-level singleton.
PROMOTION_GATES = PromotionGates()
