"""APScheduler wiring.

This module is intentionally small: register the jobs, start the
scheduler. The job functions live in `app.scheduler.jobs.*`.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.scheduler.jobs import (
    audit_verify,
    challenger_pipeline,
    daily_commentary,
    daily_report,
    healthcheck,
    market_data_backfill,
    news_fetch,
    tick_live_loop,
    weekly_review,
)


logger = logging.getLogger(__name__)


def build_scheduler() -> BackgroundScheduler:
    """Build and return a configured BackgroundScheduler (not yet started)."""
    sched = BackgroundScheduler(timezone="UTC")
    # Market hours: every minute during open, but the job itself checks.
    sched.add_job(
        tick_live_loop.run,
        IntervalTrigger(minutes=1, timezone="UTC"),
        id="tick_live_loop", name="Live tick", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=30,
    )
    sched.add_job(
        market_data_backfill.run,
        IntervalTrigger(minutes=5, timezone="UTC"),
        id="market_data_backfill", name="Market data backfill", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    sched.add_job(
        news_fetch.run,
        IntervalTrigger(minutes=15, timezone="UTC"),
        id="news_fetch", name="News fetch", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    sched.add_job(
        daily_report.run,
        CronTrigger(hour=0, minute=5, timezone="UTC"),
        id="daily_report", name="Daily report", replace_existing=True,
        max_instances=1, misfire_grace_time=600,
    )
    sched.add_job(
        daily_commentary.run,
        CronTrigger(hour=0, minute=10, timezone="UTC"),
        id="daily_commentary", name="Daily commentary", replace_existing=True,
        max_instances=1, misfire_grace_time=600,
    )
    sched.add_job(
        weekly_review.run,
        CronTrigger(day_of_week="sun", hour=22, minute=0, timezone="UTC"),
        id="weekly_review", name="Weekly review", replace_existing=True,
        max_instances=1, misfire_grace_time=3600,
    )
    sched.add_job(
        challenger_pipeline.run,
        CronTrigger(day_of_week="sun", hour=22, minute=5, timezone="UTC"),
        id="challenger_pipeline", name="Challenger pipeline", replace_existing=True,
        max_instances=1, misfire_grace_time=3600,
    )
    sched.add_job(
        audit_verify.run,
        CronTrigger(minute=0, timezone="UTC"),
        id="audit_verify", name="Audit verify", replace_existing=True,
        max_instances=1, misfire_grace_time=300,
    )
    sched.add_job(
        healthcheck.run,
        IntervalTrigger(minutes=30, timezone="UTC"),
        id="healthcheck", name="Health check", replace_existing=True,
        max_instances=1, misfire_grace_time=60,
    )
    return sched


def start() -> BackgroundScheduler:
    s = get_settings()
    sched = build_scheduler()
    sched.start()
    logger.info("scheduler started (env=%s)", s.app_env)
    return sched
