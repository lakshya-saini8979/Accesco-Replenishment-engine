"""
APScheduler configuration for automated background replenishment jobs.
Supports clean re-initialization across multiple event loops (uvicorn / pytest).
"""

from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.core import no_expiry, hce_engine, pull_theory
import logging

logger = logging.getLogger(__name__)
scheduler: Optional[AsyncIOScheduler] = None


def setup_scheduler(sched: AsyncIOScheduler):
    """Configures the daily cron schedules for the replenishment engine."""
    sched.add_job(
        no_expiry.execute_routing,
        "cron",
        hour="8-18/2",
        minute=0,
        id="job_fresh_routing",
        replace_existing=True,
    )
    sched.add_job(
        hce_engine.execute_hce_batch,
        "cron",
        hour=18,
        minute=0,
        id="job_hce_nudge",
        replace_existing=True,
    )
    sched.add_job(
        pull_theory.execute_nightly_pull,
        "cron",
        hour=21,
        minute=15,
        id="job_store_pull",
        replace_existing=True,
    )
    logger.info("APScheduler: Registered 3 automated replenishment cron jobs.")


def start_scheduler():
    global scheduler
    if scheduler is None or not scheduler.running:
        scheduler = AsyncIOScheduler()
        setup_scheduler(scheduler)
        scheduler.start()
        logger.info("APScheduler started successfully.")


def shutdown_scheduler():
    global scheduler
    if scheduler is not None:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("APScheduler stopped.")
