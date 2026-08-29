"""Market data backfill: 5-min reconcile."""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def run() -> None:
    logger.debug("market_data_backfill: heartbeat")
