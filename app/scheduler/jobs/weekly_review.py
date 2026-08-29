"""Weekly review: Sunday 22:00 UTC, LLM call. May produce a hypothesis."""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def run() -> None:
    logger.debug("weekly_review: heartbeat")
