"""Market data backfill: 5-min reconcile against the broker."""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.market.data_source import McpDataSource
from app.market.pipeline import MarketDataPipeline
from app.orders.mcp_client import McpClientPool
from app.scheduler.deps import get_candle_store

logger = logging.getLogger(__name__)


async def _reconcile() -> int:
    settings = get_settings()
    pool = McpClientPool()
    try:
        source = McpDataSource(pool)
        pipeline = MarketDataPipeline(source, get_candle_store())
        return await pipeline.reconcile(settings.instrument, settings.granularity)
    finally:
        await pool.close()


def run() -> None:
    try:
        written = asyncio.run(_reconcile())
        logger.info("market_data_backfill: wrote %d candle rows", written)
    except Exception:
        logger.exception("market_data_backfill: reconcile failed")
