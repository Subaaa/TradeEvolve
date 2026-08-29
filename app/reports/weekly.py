"""Weekly report: LLM-assisted reflection on the past 7 days."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from app.llm.client import LlmClient
from app.llm.schemas import (
    DailyReportSummary,
    JournalSummary,
    WeeklyReviewIn,
    WeeklyReviewOut,
)
from app.evolution.challenger_builder import HypothesisCandidate


logger = logging.getLogger(__name__)


class DailyReportStore(Protocol):
    def get(self, day: date) -> DailyReportSummary | None: ...
    def list_between(self, start: date, end: date) -> list[DailyReportSummary]: ...


def build_input(
    *,
    store: DailyReportStore,
    today: date,
    leaderboard: list[dict] | None = None,
) -> WeeklyReviewIn:
    last7 = store.list_between(today - timedelta(days=7), today)
    last12w = store.list_between(today - timedelta(weeks=12), today)
    return WeeklyReviewIn(
        last_7_days=[
            JournalSummary(
                day=r.day,
                num_trades=r.num_trades,
                num_rejections=r.num_rejections,
                net_pnl_usd=r.net_pnl_usd,
            )
            for r in last7
        ],
        last_12_weeks=last12w,
        leaderboard=leaderboard or [],
    )


def run_review(client: LlmClient, inp: WeeklyReviewIn) -> WeeklyReviewOut:
    return client.call_with_default(
        site="weekly_review",
        input=inp,
        output_model=WeeklyReviewOut,
        default=WeeklyReviewOut(
            summary="weekly review skipped (LLM unavailable)",
            failure_patterns=[],
            candidate=None,
            blocked_reason=None,
        ),
    )
