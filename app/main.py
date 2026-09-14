"""Entry point for the agent process.

Boots:
1. Logging
2. APScheduler
3. The Alpaca MCP subprocess (lazy: only when an order needs to be sent)
4. FastAPI on 127.0.0.1:8080
5. Signal handlers

The MCP subprocess driver is intentionally a small async wrapper; the
agent process never holds the Alpaca credentials.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

import uvicorn

from app.api.server import create_app
from app.config import get_settings
from app.scheduler.cron import start as start_scheduler


logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def main() -> int:
    s = get_settings()
    configure_logging(s.app_log_level)
    logger.info("TradeEvolve starting (env=%s)", s.app_env)

    sched = start_scheduler()
    api = create_app()

    stop_event = threading.Event()

    def _stop(*_: object) -> None:
        logger.warning("shutdown signal received")
        stop_event.set()
        sched.shutdown(wait=False)
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
