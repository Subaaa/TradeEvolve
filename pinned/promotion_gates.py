"""Promotion gates: fixed weights and thresholds for champion/challenger comparison.

A change here is a human-only code change. The PromotionArbiter is the only
component that imports these constants, and it is the only component that
may write to `experiments` with `final_decision='promoted'`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromotionGates:
    # --- Walk-forward ---
    walk_forward_folds: int = 6
    walk_forward_train_months: int = 3
    walk_forward_test_months: int = 6
    min_relative_perf: float = 0.95            # challenger WF / champion WF

    # --- Holdout ---
    min_holdout_relative_perf: float = 0.95     # 1.0 would be stricter; 0.95 for MVP

    # --- Monte Carlo ---
    monte_carlo_iterations: int = 1000
    monte_carlo_min_percentile: float = 0.80

    # --- Shadow paper-trading ---
    min_shadow_hours: int = 240                 # ~10 trading days
    min_shadow_trades: int = 30

    # --- Trade-count minimums ---
    min_trades_single_window: int = 30
    min_trades_walk_forward: int = 100

    # --- Score weights (primary composite score) ---
    # score = expectancy_r * consistency_score / max_drawdown_pct * (1 - complexity_penalty)
    # tie-break = 0.8 * sharpe + 0.2 * profit_factor
    # All weights in [0, 1].
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
