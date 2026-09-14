"""Pydantic input/output schemas for every LLM call site."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.evolution.challenger_builder import HypothesisCandidate
from pinned.news_policy import NewsCategory


# ---- Classify news ---------------------------------------------------------


class ClassifyNewsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    news_id: int
    title: str
    body: str
    source_tags: list[str] = Field(default_factory=list)


class ClassifyNewsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: NewsCategory
    asset_tags: list[Literal["XAU", "USD", "EUR", "JPY", "OTHER"]]
    sentiment: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    is_fact: bool
    summary: str = Field(max_length=500)


# ---- Review trade ----------------------------------------------------------


class ClosedTradeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trade_id: int
    opened_at: datetime
    closed_at: datetime
    side: str
    units: float
    entry: float
    exit: float
    pnl_usd: float
    pnl_r: float
    exit_reason: str


class ReviewTradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    closed_trade: ClosedTradeSummary
    last_20_closed_trades: list[ClosedTradeSummary] = Field(default_factory=list)
    related_news: list[str] = Field(default_factory=list)


class ReviewTradeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=600)
    failure_mode_tags: list[str] = Field(default_factory=list, max_length=10)
    lessons: list[str] = Field(default_factory=list, max_length=5)


# ---- Daily commentary ------------------------------------------------------


class DailyReportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    net_pnl_usd: float
    num_trades: int
    num_rejections: int
    sharpe: float | None = None
    max_drawdown_pct: float | None = None


class DailyCommentaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_report: DailyReportSummary
    top_failures: list[str] = Field(default_factory=list)
    top_news: list[str] = Field(default_factory=list)


class DailyCommentaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=400)
    observations: list[str] = Field(default_factory=list, max_length=5)


# ---- Weekly review ---------------------------------------------------------


class JournalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str
    num_trades: int
    num_rejections: int
    net_pnl_usd: float


class WeeklyReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_7_days: list[JournalSummary]
    last_12_weeks: list[DailyReportSummary]
    leaderboard: list[dict[str, Any]] = Field(default_factory=list)


class WeeklyReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=800)
    failure_patterns: list[str] = Field(default_factory=list, max_length=10)
    candidate: HypothesisCandidate | None = None
    blocked_reason: str | None = None


# ---- Failure pattern reflection -------------------------------------------


class FailurePattern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag: str
    description: str
    example_trade_ids: list[int] = Field(default_factory=list)


class ReflectFailurePatternIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: FailurePattern
    examples: list[ClosedTradeSummary] = Field(default_factory=list)


class ReflectFailurePatternOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_cause_hypotheses: list[str] = Field(default_factory=list, max_length=5)
    suggested_experiment: str = Field(max_length=600)


# ---- Site registry --------------------------------------------------------


LLM_SITES = Literal[
    "classify_news",
    "review_trade",
    "daily_commentary",
    "weekly_review",
    "reflect_failure_pattern",
]


LLM_SITE_BUDGETS: dict[str, tuple[int, int]] = {
    "classify_news": (4000, 500),
    "review_trade": (8000, 800),
    "daily_commentary": (6000, 600),
    "weekly_review": (16000, 1500),
    "reflect_failure_pattern": (8000, 1000),
}
