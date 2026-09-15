"""UTC midnight: re-anchor the day, clear the daily kill switch, write the report.

The daily loss limit is measured against the equity recorded here, which is
what makes "2% of today" mean today and not "2% of the high-water mark".
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from app.db import system_state as ss
from app.db import trades as trades_db
from app.journal.payloads import SystemEventPayload
from app.journal.types import JournalKind
from app.reports import daily as daily_report
from app.risk import kill_switch
from app.scheduler import deps

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _rollover(datetime.now(tz=UTC))
    except Exception:
        logger.exception("daily_rollover failed")


def _rollover(now: datetime) -> None:
    conn = deps.db()
    journal = deps.journal()

    yesterday = (now - timedelta(days=1)).date()
    trades = trades_db.closed_on_day(conn, yesterday)
    report = daily_report.compute(day=yesterday, trades=trades, conn=conn)
    daily_report.save(conn, report)

    cleared = kill_switch.clear_if_daily(conn)

    equity = deps.broker().get_account().equity
    ss.ensure_anchors(conn, equity=equity, now=now)
    # ensure_anchors only rolls when the calendar moved; force today's anchor
    # to the equity we just observed so the new day starts from the truth.
    ss.save_day_anchor(conn, ss.DayAnchor(day=now.date(), start_equity=equity))
    if now.weekday() == 0:
        ss.save_week_anchor(
            conn, ss.WeekAnchor(iso_week=ss.iso_week_of(now), start_equity=equity)
        )

    journal.write(
        kind=JournalKind.SYSTEM_EVENT,
        payload=SystemEventPayload(
            event="daily_rollover",
            detail=f"{yesterday.isoformat()}: {report.num_trades} trades, "
            f"{report.net_pnl_usd:+.2f} USD",
            data={
                "kill_switch_cleared": cleared,
                "start_equity": equity,
                "report": json.loads(json.dumps(report.payload, default=str)),
            },
        ),
    )
    logger.info(
        "rolled over into %s with equity %.2f (yesterday: %d trades, %+.2f USD)",
        now.date().isoformat(),
        equity,
        report.num_trades,
        report.net_pnl_usd,
    )
