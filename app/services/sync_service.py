import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import SyncLog, SyncState, Task, TaskList
from app.services.graph_client import DeltaLinkExpiredError, graph_client

logger = logging.getLogger(__name__)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def _parse_date(dt_obj: dict | None):
    if not dt_obj:
        return None, "UTC"
    from datetime import date as date_type
    raw = dt_obj.get("dateTime", "")
    tz = dt_obj.get("timeZone", "UTC")
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date(), tz
    return None, tz


async def _get_or_create_sync_state(db: AsyncSession, resource_type: str) -> SyncState:
    result = await db.execute(select(SyncState).where(SyncState.resource_type == resource_type))
    state = result.scalar_one_or_none()
    if not state:
        state = SyncState(resource_type=resource_type)
        db.add(state)
        await db.flush()
    return state


async def _log_sync(
    db: AsyncSession,
    sync_type: str,
    resource_type: str,
    pulled: int = 0,
    pushed: int = 0,
    deleted: int = 0,
    errors: int = 0,
    duration_ms: int = 0,
) -> None:
    db.add(SyncLog(
        sync_type=sync_type,
        resource_type=resource_type,
        items_pulled=pulled,
        items_pushed=pushed,
        items_deleted=deleted,
        errors=errors,
        duration_ms=duration_ms,
    ))


# --- Pull: MS To Do → PostgreSQL ---

async def pull_lists(db: AsyncSession) -> tuple[int, int]:
    """Pull task lists from MS To Do using delta query. Returns (upserted, deleted)."""
    state = await _get_or_create_sync_state(db, "task_lists")
    try:
        delta_result = await graph_client.get_lists_delta(state.delta_link)
    except DeltaLinkExpiredError:
        logger.warning("Lists delta link expired, doing full pull")
        state.delta_link = None
        delta_result = await graph_client.get_lists_delta(None)

    upserted = 0
    deleted = 0

    for item in delta_result["value"]:
        ms_id = item.get("id")
        if "@removed" in item:
            result = await db.execute(select(TaskList).where(TaskList.ms_id == ms_id))
            existing = result.scalar_one_or_none()
            if existing and not existing.deleted_at:
                existing.deleted_at = datetime.now(timezone.utc)
                existing.sync_status = "synced"
                deleted += 1
            continue

        result = await db.execute(select(TaskList).where(TaskList.ms_id == ms_id))
        existing = result.scalar_one_or_none()
        ms_modified = _parse_datetime(item.get("lastModifiedDateTime"))

        if existing:
            if existing.sync_status == "pending_push" and existing.updated_at and ms_modified:
                if existing.updated_at > ms_modified:
                    continue  # local is newer, skip
            existing.display_name = item.get("displayName", existing.display_name)
            existing.is_owner = item.get("isOwner", existing.is_owner)
            existing.is_shared = item.get("isShared", existing.is_shared)
            existing.wellknown_list_name = item.get("wellknownListName")
            existing.ms_last_modified = ms_modified
            existing.sync_status = "synced"
            existing.deleted_at = None
        else:
            new_list = TaskList(
                ms_id=ms_id,
                display_name=item.get("displayName", "Untitled"),
                is_owner=item.get("isOwner", True),
                is_shared=item.get("isShared", False),
                wellknown_list_name=item.get("wellknownListName"),
                ms_last_modified=ms_modified,
                sync_status="synced",
            )
            db.add(new_list)
        upserted += 1

    state.delta_link = delta_result.get("delta_link")
    state.last_sync_at = datetime.now(timezone.utc)
    state.last_sync_status = "success"

    return upserted, deleted


async def pull_tasks_for_list(db: AsyncSession, task_list: TaskList) -> tuple[int, int]:
    """Pull tasks for a specific list using delta query."""
    if not task_list.ms_id:
        return 0, 0

    resource_type = f"tasks:{task_list.ms_id}"
    state = await _get_or_create_sync_state(db, resource_type)

    try:
        delta_result = await graph_client.get_tasks_delta(task_list.ms_id, state.delta_link)
    except DeltaLinkExpiredError:
        logger.warning("Tasks delta link expired for list %s, doing full pull", task_list.ms_id)
        state.delta_link = None
        delta_result = await graph_client.get_tasks_delta(task_list.ms_id, None)

    upserted = 0
    deleted = 0

    for item in delta_result["value"]:
        ms_id = item.get("id")
        if "@removed" in item:
            result = await db.execute(select(Task).where(Task.ms_id == ms_id))
            existing = result.scalar_one_or_none()
            if existing and not existing.deleted_at:
                existing.deleted_at = datetime.now(timezone.utc)
                existing.sync_status = "synced"
                deleted += 1
            continue

        result = await db.execute(select(Task).where(Task.ms_id == ms_id))
        existing = result.scalar_one_or_none()
        ms_modified = _parse_datetime(item.get("lastModifiedDateTime"))

        due_date, due_tz = _parse_date(item.get("dueDateTime"))
        reminder_dt_raw = item.get("reminderDateTime")
        reminder_dt = None
        if reminder_dt_raw and isinstance(reminder_dt_raw, dict):
            raw = reminder_dt_raw.get("dateTime", "")
            if raw:
                reminder_dt = _parse_datetime(raw)

        body_obj = item.get("body", {})
        completed_obj = item.get("completedDateTime")
        completed_dt = None
        completed_tz = None
        if completed_obj and isinstance(completed_obj, dict):
            raw = completed_obj.get("dateTime", "")
            if raw:
                completed_dt = _parse_datetime(raw)
            completed_tz = completed_obj.get("timeZone")

        task_data = {
            "title": item.get("title", ""),
            "body": body_obj.get("content") if isinstance(body_obj, dict) else None,
            "body_content_type": body_obj.get("contentType", "text") if isinstance(body_obj, dict) else "text",
            "importance": item.get("importance", "normal"),
            "status": item.get("status", "notStarted"),
            "due_date": due_date,
            "due_timezone": due_tz,
            "reminder_datetime": reminder_dt,
            "is_reminder_on": item.get("isReminderOn", False),
            "completed_datetime": completed_dt,
            "completed_timezone": completed_tz,
            "recurrence": item.get("recurrence"),
            "categories": item.get("categories", []),
            "ms_created_at": _parse_datetime(item.get("createdDateTime")),
            "ms_last_modified": ms_modified,
        }

        if existing:
            if existing.sync_status == "pending_push" and existing.updated_at and ms_modified:
                if existing.updated_at > ms_modified:
                    continue
            for key, value in task_data.items():
                setattr(existing, key, value)
            existing.sync_status = "synced"
            existing.deleted_at = None
        else:
            new_task = Task(
                ms_id=ms_id,
                list_id=task_list.id,
                sync_status="synced",
                **task_data,
            )
            db.add(new_task)
        upserted += 1

    state.delta_link = delta_result.get("delta_link")
    state.last_sync_at = datetime.now(timezone.utc)
    state.last_sync_status = "success"

    return upserted, deleted


# --- Push: PostgreSQL → MS To Do ---

async def push_pending(db: AsyncSession) -> tuple[int, int]:
    """Push locally changed items to MS To Do. Returns (pushed_lists, pushed_tasks)."""
    pushed_lists = 0
    pushed_tasks = 0

    # Push lists
    result = await db.execute(
        select(TaskList).where(TaskList.sync_status == "pending_push", TaskList.deleted_at.is_(None))
    )
    for task_list in result.scalars().all():
        try:
            if task_list.ms_id:
                await graph_client.update_list(task_list.ms_id, task_list.display_name)
            else:
                resp = await graph_client.create_list(task_list.display_name)
                task_list.ms_id = resp.get("id")
            task_list.sync_status = "synced"
            pushed_lists += 1
        except Exception:
            logger.exception("Failed to push list %s", task_list.id)

    # Push deleted lists
    result = await db.execute(
        select(TaskList).where(TaskList.sync_status == "pending_push", TaskList.deleted_at.is_not(None))
    )
    for task_list in result.scalars().all():
        if task_list.ms_id:
            try:
                await graph_client.delete_list(task_list.ms_id)
                task_list.sync_status = "synced"
                pushed_lists += 1
            except Exception:
                logger.exception("Failed to delete list %s from Graph", task_list.id)

    # Push tasks
    result = await db.execute(
        select(Task).where(Task.sync_status == "pending_push", Task.deleted_at.is_(None))
    )
    for task in result.scalars().all():
        list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
        task_list = list_result.scalar_one_or_none()
        if not task_list or not task_list.ms_id:
            continue
        try:
            from app.services.task_service import _task_to_graph_payload
            payload = _task_to_graph_payload(task)
            if task.ms_id:
                await graph_client.update_task(task_list.ms_id, task.ms_id, payload)
            else:
                resp = await graph_client.create_task(task_list.ms_id, payload)
                task.ms_id = resp.get("id")
            task.sync_status = "synced"
            pushed_tasks += 1
        except Exception:
            logger.exception("Failed to push task %s", task.id)

    # Push deleted tasks
    result = await db.execute(
        select(Task).where(Task.sync_status == "pending_push", Task.deleted_at.is_not(None))
    )
    for task in result.scalars().all():
        if task.ms_id:
            list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
            task_list = list_result.scalar_one_or_none()
            if task_list and task_list.ms_id:
                try:
                    await graph_client.delete_task(task_list.ms_id, task.ms_id)
                    task.sync_status = "synced"
                    pushed_tasks += 1
                except Exception:
                    logger.exception("Failed to delete task %s from Graph", task.id)

    return pushed_lists, pushed_tasks


# --- Main sync orchestrator ---

async def run_sync(sync_type: str = "delta") -> dict:
    """Run a full sync cycle: pull then push."""
    start = time.monotonic()
    total_pulled = 0
    total_pushed = 0
    total_deleted = 0
    total_errors = 0

    async with async_session() as db:
        try:
            # Pull lists
            lists_upserted, lists_deleted = await pull_lists(db)
            total_pulled += lists_upserted
            total_deleted += lists_deleted
            await db.commit()

            # Pull tasks for each list (commit after each list)
            result = await db.execute(
                select(TaskList).where(TaskList.deleted_at.is_(None), TaskList.ms_id.is_not(None))
            )
            task_lists = list(result.scalars().all())
            for task_list in task_lists:
                try:
                    tasks_upserted, tasks_deleted = await pull_tasks_for_list(db, task_list)
                    total_pulled += tasks_upserted
                    total_deleted += tasks_deleted
                    await db.commit()
                except Exception:
                    logger.exception("Failed to pull tasks for list %s", task_list.ms_id)
                    await db.rollback()
                    total_errors += 1

            # Push pending changes
            pushed_lists, pushed_tasks = await push_pending(db)
            total_pushed += pushed_lists + pushed_tasks
            await db.commit()

            duration_ms = int((time.monotonic() - start) * 1000)
            # Log the sync
            async with async_session() as log_db:
                await _log_sync(
                    log_db, sync_type, "all",
                    pulled=total_pulled, pushed=total_pushed,
                    deleted=total_deleted, errors=total_errors,
                    duration_ms=duration_ms,
                )
                await log_db.commit()

            logger.info(
                "Sync completed: pulled=%d pushed=%d deleted=%d errors=%d duration=%dms",
                total_pulled, total_pushed, total_deleted, total_errors, duration_ms,
            )

            return {
                "pulled": total_pulled,
                "pushed": total_pushed,
                "deleted": total_deleted,
                "errors": total_errors,
                "duration_ms": duration_ms,
            }

        except Exception:
            logger.exception("Sync failed")
            await db.rollback()
            duration_ms = int((time.monotonic() - start) * 1000)
            async with async_session() as log_db:
                await _log_sync(log_db, sync_type, "all", errors=1, duration_ms=duration_ms)
                await log_db.commit()
            raise
