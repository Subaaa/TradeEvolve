"""Position monitor: runs every minute regardless of the bar clock.

Exits must not wait for a bar to close. A stop that filled at :03 should be
recorded at :04, not at :15, and a position that lost its protective order
needs replacing within one cycle, not within one bar.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.db import candles as candles_db
from app.risk import kill_switch
from app.scheduler import deps

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _monitor(datetime.now(tz=UTC))
    except Exception:
        logger.exception("tick_monitor failed")


def _monitor(now: datetime) -> None:
    conn = deps.db()
    s = deps.settings()
    manager = deps.position_manager()

    from app.db import trades as trades_db

    if not trades_db.open_trades(conn):
        return

    bid: float | None
    try:
        bid = deps.broker().latest_quote(s.instrument).bid
    except Exception as exc:
        logger.warning("could not fetch a quote: %s", exc)
        bid = None

    bars = candles_db.read_bars(conn, s.instrument, s.granularity, limit=400)

    closed = manager.monitor(
        now=now,
        latest_bid=bid,
        closed_bars=bars,
        kill_switch=kill_switch.is_engaged(conn),
    )
    for trade in closed:
        logger.info(
            "monitor closed trade %d (%s) for %.2f USD",
            trade.id,
            trade.exit_reason.value if trade.exit_reason else "unknown",
            trade.pnl_usd or 0.0,
        )
