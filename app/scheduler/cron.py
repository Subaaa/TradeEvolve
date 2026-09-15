"""The job table.

The trading tick fires on the bar boundary plus a twenty-second grace, not on
a fixed minute interval. The delay lets the broker finish publishing the bar
that just closed; without it the tick regularly reads a bar short and skips.

`max_instances=1` and `coalesce=True` on every job: a slow tick must never
overlap the next one, and a backlog must collapse rather than replay.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.market.bars import granularity_seconds
from app.scheduler import deps
from app.scheduler.jobs import (
    audit_verify,
    daily_review,
    daily_rollover,
    shadow_update,
    tick_bar_close,
    tick_monitor,
    weekly_evolution,
)

logger = logging.getLogger(__name__)

BAR_CLOSE_GRACE_SECONDS = 20


def _bar_close_minutes(granularity: str) -> str:
    """Cron minute expression for the close of each bar of this granularity."""
    seconds = granularity_seconds(granularity)
    if seconds >= 3600:
        return "0"
    step = max(1, seconds // 60)
    return ",".join(str(m) for m in range(0, 60, step))


def build_scheduler(granularity: str = "M15") -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")

    sched.add_job(
        tick_bar_close.run,
        CronTrigger(
            minute=_bar_close_minutes(granularity),
            second=BAR_CLOSE_GRACE_SECONDS,
            timezone="UTC",
        ),
        id="tick_bar_close",
        name="Bar-close tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    sched.add_job(
        tick_monitor.run,
        IntervalTrigger(seconds=60, timezone="UTC"),
        id="tick_monitor",
        name="Position monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    sched.add_job(
        daily_rollover.run,
        CronTrigger(hour=0, minute=0, second=5, timezone="UTC"),
        id="daily_rollover",
        name="Daily rollover",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    sched.add_job(
        daily_review.run,
        CronTrigger(hour=0, minute=10, timezone="UTC"),
        id="daily_review",
        name="Daily trade review",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    sched.add_job(
        shadow_update.run,
        CronTrigger(hour=0, minute=20, timezone="UTC"),
        id="shadow_update",
        name="Shadow evaluation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    sched.add_job(
        weekly_evolution.run,
        CronTrigger(day_of_week="sun", hour=22, minute=0, timezone="UTC"),
        id="weekly_evolution",
        name="Weekly evolution",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=7200,
    )
    sched.add_job(
        audit_verify.run,
        CronTrigger(minute=5, timezone="UTC"),
        id="audit_verify",
        name="Journal audit",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    return sched


def start(granularity: str | None = None) -> BackgroundScheduler:
    sched = build_scheduler(granularity or deps.settings().granularity)
    sched.start()
    for job in sched.get_jobs():
        logger.info("scheduled %s (next run: %s)", job.name, job.next_run_time)
    return sched
