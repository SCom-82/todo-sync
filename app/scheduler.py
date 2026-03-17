import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.services.auth_service import auth_service

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _scheduled_sync() -> None:
    if not await auth_service.is_authenticated():
        logger.debug("Skipping scheduled sync: not authenticated")
        return
    try:
        from app.services.sync_service import run_sync
        await run_sync(sync_type="delta")
    except Exception:
        logger.exception("Scheduled sync failed")


def start_scheduler() -> None:
    scheduler.add_job(
        _scheduled_sync,
        "interval",
        seconds=settings.sync_interval_seconds,
        id="sync_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started: sync every %ds", settings.sync_interval_seconds)


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
