"""Entry point for the agent process.

Boot order matters. Reconciliation runs **before** the scheduler starts, so a
restart that finds an open position adopts and re-protects it (or flattens an
orphan) before any new tick can decide to open a second one.

One process: the scheduler, the read-only API, one SQLite file and one HTTP
session to Alpaca paper. No subprocess, no message broker, no worker pool.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

import uvicorn

from app.api.server import create_app
from app.config import get_settings
from app.db import strategy_versions as sv_db
from app.journal.payloads import SystemEventPayload
from app.journal.types import JournalKind
from app.scheduler import deps
from app.scheduler.cron import start as start_scheduler
from app.strategy.configs import StrategyConfig

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def main() -> int:
    s = get_settings()
    configure_logging(s.app_log_level)
    logger.info(
        "TradeEvolve starting (env=%s, instrument=%s, granularity=%s, live=%s)",
        s.app_env,
        s.instrument,
        s.granularity,
        s.live_trading_enabled,
    )

    conn = deps.db()
    champion = sv_db.ensure_champion(
        conn, StrategyConfig.from_json_file(s.resolved_champion_config())
    )
    logger.info("champion: %s (id %d)", champion.version_tag, champion.id)

    journal = deps.journal()
    journal.write(
        kind=JournalKind.SYSTEM_EVENT,
        payload=SystemEventPayload(
            event="process_start",
            detail=f"session {deps.SESSION_ID}",
            data={"instrument": s.instrument, "granularity": s.granularity},
        ),
        strategy_version_id=champion.id,
    )

    report = deps.position_manager().reconcile_on_boot()
    if not report.clean:
        logger.warning(
            "boot reconciliation: adopted=%s closed=%s orphans=%d stray_orders=%d",
            report.adopted,
            report.closed_from_broker,
            report.orphans_flattened,
            report.stray_orders_cancelled,
        )

    sched = start_scheduler(s.granularity)
    api = create_app()
    stop_event = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        logger.warning("shutdown signal %s received", signum)
        stop_event.set()
        try:
            sched.shutdown(wait=False)
        finally:
            journal.write(
                kind=JournalKind.SYSTEM_EVENT,
                payload=SystemEventPayload(
                    event="process_stop", detail=f"signal {signum}"
                ),
            )
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    uvicorn.run(
        api,
        host=s.app_http_host,
        port=s.app_http_port,
        log_level=s.app_log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
