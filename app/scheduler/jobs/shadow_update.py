"""00:20 UTC: ask the arbiter whether the shadow challenger has earned promotion."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.evolution.pipeline import evaluate_shadow
from app.scheduler import deps

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _update(datetime.now(tz=UTC))
    except Exception:
        logger.exception("shadow_update failed")


def _update(now: datetime) -> None:
    s = deps.settings()
    outcome = evaluate_shadow(
        deps.db(),
        deps.journal(),
        instrument=s.instrument,
        granularity=s.granularity,
        initial_equity=s.initial_paper_equity_usd,
        now=now,
    )
    logger.info("shadow update: %s", outcome)
