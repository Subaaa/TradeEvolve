# Risk

This document enumerates the risk-management rules and how the system
enforces them. The plan (`plan.md` Section 11) is the source of truth;
this is a quick reference.

## The deterministic core

Three components are LLM-unreachable:

- `app/risk/engine.py` — `evaluate(proposal, state, limits, news_policy, market_state, now) -> Decision`. Pure function. No network, no I/O, no LLM.
- `app/evolution/promotion_arbiter.py` — `arbitrate(...) -> PromotionDecision`. Pure function. Never reads LLM output.
- `pinned/risk_limits.py`, `pinned/news_policy.py`, `pinned/promotion_gates.py`, `pinned/simulation_constants.py` — frozen constants.

A `ruff` lint check and a build-time test (`test_pinned_invariant.py`)
fail the build if any non-`pinned/` module imports a non-allow-listed
symbol from `pinned/`.

## Limits (defaults; configurable by human only)

| Limit | Default |
|-------|---------|
| `MAX_RISK_PER_TRADE_PCT` | 0.5% of equity |
| `MAX_DAILY_LOSS_PCT` | 3% of equity |
| `MAX_WEEKLY_LOSS_PCT` | 6% of equity (kill switch) |
| `MAX_DRAWDOWN_PCT` | 15% from equity high (kill switch) |
| `MAX_OPEN_POSITIONS` | 1 |
| `MIN_RR_RATIO` | 1.5 |
| `MANDATORY_STOP_LOSS` | true |
| `MAX_SL_DISTANCE_ATR_MULTIPLE` | 3.0 |
| `COOLDOWN_AFTER_CONSECUTIVE_LOSSES` | 3 trades, 24h |
| `COOLDOWN_AROUND_NEWS` | 30 min before/after high-impact |
| `STALE_DATA_MAX_AGE_SEC` | 300 (5 min) |
| `MIN_BAR_VOLUME_FOR_ENTRY` | 0 (XAU/USD volume is unreliable) |

The risk engine checks the cheapest and most-critical conditions
first, so a shape failure cannot bypass a risk limit.

## Promotion gates (in `pinned/promotion_gates.py`)

| Gate | Default |
|------|---------|
| Walk-forward folds | 6 |
| Walk-forward train/test months | 3 / 6 |
| `MIN_RELATIVE_PERF` | 0.95 |
| `MIN_HOLDOUT_RELATIVE_PERF` | 0.95 |
| Monte Carlo iterations | 1,000 |
| `MONTE_CARLO_MIN_PERCENTILE` | 0.80 |
| `MIN_SHADOW_HOURS` | 240 |
| `MIN_SHADOW_TRADES` | 30 |
| `MIN_TRADES_SINGLE_WINDOW` | 30 |
| `MIN_TRADES_WALK_FORWARD` | 100 |
| `MAX_HYPOTHESIS_ATTEMPTS_PER_CHAMPION` | 4 |
| `ENFORCE_RISK_ENGINE_FAIRNESS` | true |

## Kill switch

A row in `system_state.kill_switch = {"engaged": true, "reason": "..."}`.
The live loop checks it every tick. When engaged, the next proposal is
rejected with `KILL_SWITCH_ENGAGED`. The kill switch is engaged by:

- A breach of `MAX_WEEKLY_LOSS_PCT` or `MAX_DRAWDOWN_PCT`.
- A human operator via the `app.ops.kill_switch` module or the
  `system_state` table directly.

A human must clear the kill switch after a manual review.

## Anti-circumvention

The `PromotionArbiter` checks that the challenger's proposed trade,
when fed to the same `RiskEngine` with the same `RiskLimits`, does
**not** get a more favorable decision than the champion's. A candidate
that bypasses a limit is rejected with `RISK_CIRCUMVENTION`.

## Anti-p-hacking

After 4 failed challengers from a given champion, the system declares
a "plateau". The next hypothesis is auto-rejected unless it cites
*different evidence* (e.g., a different market or regime). The LLM is
notified of the plateau in the weekly review prompt.

## What the LLM may not propose

- Any change to constants in `pinned/`.
- Any change to `LIVE_TRADING_ENABLED`.
- Any change to the holdout dataset hash.
- Any deletion of audit log rows.
- More than one mutation per week.
- A trade that would be approved by the engine when the champion's
  would be rejected.

All of these are enforced by either schema validation (Pydantic),
import-graph checks (build-time test), or the PromotionArbiter.
