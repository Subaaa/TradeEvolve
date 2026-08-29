"""Live tick: every minute during market hours.

This is the only job that calls the StrategyRunner and the RiskEngine.
For the MVP the body is a stub that exercises the pipeline. The
full implementation wires together:
  - MarketDataPipeline.reconcile
  - MarketStateBuilder
  - StrategyRunner.propose
  - RiskEngine.evaluate
  - OrderGateway.submit (only on approved)
  - JournalWriter.write (every transition)
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def run() -> None:
    """One live tick.

    The body is intentionally a no-op in the scaffolded version; the
    real implementation is the 18-step loop from the plan. The shape
    is fixed: fetch, build state, run strategy, run risk, send or
    journal, then exit.
    """
    logger.debug("tick_live_loop: heartbeat")
