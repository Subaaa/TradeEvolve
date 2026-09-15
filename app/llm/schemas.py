"""Input and output schemas for every LLM call site.

Two rules make these safe to hand to a model:

*   Every output model is `extra="forbid"` and contains no `Any`, no bare
    `dict`, and no unconstrained string where an enum will do. The schema is
    the contract, and a loose schema is how an LLM's suggestion becomes an
    unreviewed config change.
*   The only field that can reach the strategy is a `HypothesisCandidate`,
    whose `field` is constrained to the mutable allow-list. Even so,
    `challenger_builder` re-checks the name, the range and the current value
    before anything is applied. The schema is a convenience, not the guard.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MutableField = Literal[
    "fast_ema_length",
    "slow_ema_length",
    "atr_length",
    "pullback_lookback",
    "atr_sl_multiplier",
    "tp_r_multiple",
    "time_stop_bars",
]

FailureModeTag = Literal[
    "stopped_at_the_low",
    "target_too_far",
    "target_too_close",
    "entry_too_late",
    "chop_no_trend",
    "stop_too_tight",
    "stop_too_wide",
    "held_too_long",
    "exited_too_early",
    "news_driven_move",
    "cost_dominated",
    "clean_win",
    "clean_loss",
    "unclear",
]

Site = Literal["review_trade", "weekly_review"]

# (max input tokens, max output tokens) per site. The input figure is
# advisory; the output figure is enforced by the API.
LLM_SITE_BUDGETS: dict[Site, tuple[int, int]] = {
    "review_trade": (24_000, 4_000),
    "weekly_review": (48_000, 6_000),
}

SITE_EFFORT: dict[Site, Literal["low", "medium", "high"]] = {
    "review_trade": "medium",
    "weekly_review": "high",
}


# ---------------------------------------------------------------- review_trade


class ClosedTradeSummary(BaseModel):
    """One closed trade, as the reviewer sees it."""

    model_config = ConfigDict(extra="forbid")

    trade_id: int
    opened_at: str
    closed_at: str
    side: str
    units: float
    entry: float
    stop_loss: float
    take_profit: float
    exit_price: float
    exit_reason: str
    pnl_usd: float
    pnl_r: float
    fees_usd: float
    mfe_r: float
    mae_r: float
    bars_held: int
    indicators: dict[str, float] = Field(default_factory=dict)


class ReviewTradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    strategy_version_tag: str
    strategy_params: dict[str, float]
    trades: list[ClosedTradeSummary]
    recent_context: list[ClosedTradeSummary] = Field(default_factory=list)
    day_pnl_usd: float = 0.0
    rejection_counts: dict[str, int] = Field(default_factory=dict)


class TradeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_id: int
    summary: str
    failure_mode_tags: list[FailureModeTag] = Field(default_factory=list)
    followed_rules: bool = True


class ReviewTradeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[TradeReview]
    day_summary: str


# --------------------------------------------------------------- weekly_review


class DayStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    trades: int
    wins: int
    losses: int
    net_pnl_usd: float
    expectancy_r: float


class ParamRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    min: float
    max: float


class PriorHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    from_value: float
    to_value: float
    outcome: str
    reason: str


class WeeklyReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: str
    period_end: str
    strategy_version_tag: str
    params: dict[str, ParamRange]
    total_trades: int
    net_pnl_usd: float
    expectancy_r: float
    win_rate: float
    profit_factor: float
    avg_mfe_r: float
    avg_mae_r: float
    exit_reason_counts: dict[str, int]
    rejection_counts: dict[str, int]
    by_day: list[DayStats]
    win_rate_by_hour: dict[str, float]
    prior_hypotheses: list[PriorHypothesis] = Field(default_factory=list)
    plateau: bool = False
    trade_reviews: list[str] = Field(default_factory=list)


class HypothesisOut(BaseModel):
    """The one bounded change the review is allowed to propose."""

    model_config = ConfigDict(extra="forbid")

    field: MutableField
    from_value: float
    to_value: float
    reason: str


class WeeklyReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    failure_patterns: list[str] = Field(default_factory=list)
    candidate: HypothesisOut | None = None
    blocked_reason: str | None = None
