"""Daily report: 00:05 UTC."""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def run() -> None:
    logger.debug("daily_report: heartbeat")
