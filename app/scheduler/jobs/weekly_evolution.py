"""Sunday 22:00 UTC: one bounded hypothesis, validated offline, staged to shadow."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.evolution.pipeline import run_weekly_evolution
from app.llm.client import build_client
from app.scheduler import deps

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _evolve(datetime.now(tz=UTC))
    except Exception:
        logger.exception("weekly_evolution failed")


def _evolve(now: datetime) -> None:
    s = deps.settings()
    conn = deps.db()
    result = run_weekly_evolution(
        conn,
        build_client(s, conn),
        deps.journal(),
        instrument=s.instrument,
        granularity=s.granularity,
        initial_equity=s.initial_paper_equity_usd,
        now=now,
    )
    logger.info("weekly evolution: %s", result.detail)
