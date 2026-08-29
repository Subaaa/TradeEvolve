"""Health check: 30-min heartbeat."""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def run() -> None:
    logger.debug("healthcheck: heartbeat")
