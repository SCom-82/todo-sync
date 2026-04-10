import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Task, TaskList
from app.schemas import TaskCreate, TaskUpdate


def _today() -> date:
    """Get today's date in user's timezone (not UTC)."""
    return datetime.now(ZoneInfo(settings.user_timezone)).date()
from app.services.graph_client import graph_client

logger = logging.getLogger(__name__)


async def _try_push_task(task: Task, list_ms_id: str | None, action: str) -> None:
    """Best-effort immediate push to MS Graph. Failures are retried by periodic sync."""
    if not list_ms_id:
        return
    try:
        if action == "create":
            data = _task_to_graph_payload(task)
            result = graph_client.create_task(list_ms_id, data)
            if asyncio.iscoroutine(result):
                result = await result
            task.ms_id = result.get("id")
            task.sync_status = "synced"
        elif action == "update" and task.ms_id:
            data = _task_to_graph_payload(task)
            result = graph_client.update_task(list_ms_id, task.ms_id, data)
            if asyncio.iscoroutine(result):
                await result
            task.sync_status = "synced"
        elif action == "delete" and task.ms_id:
            result = graph_client.delete_task(list_ms_id, task.ms_id)
            if asyncio.iscoroutine(result):
                await result
            task.sync_status = "synced"
    except Exception:
        logger.exception("Failed to push task %s to Graph, will retry on next sync", task.id)


def _task_to_graph_payload(task: Task) -> dict:
    payload: dict = {"title": task.title, "importance": task.importance, "status": task.status}
    if task.body:
        payload["body"] = {"content": task.body, "contentType": task.body_content_type}
    if task.due_date:
        payload["dueDateTime"] = {
            "dateTime": datetime.combine(task.due_date, datetime.min.time()).isoformat(),
            "timeZone": task.due_timezone,
        }
    if task.is_reminder_on and task.reminder_datetime:
        payload["isReminderOn"] = True
        payload["reminderDateTime"] = {
            "dateTime": task.reminder_datetime.isoformat(),
            "timeZone": "UTC",
        }
    if task.categories:
        payload["categories"] = task.categories
    return payload


# --- Task Lists ---

async def get_all_lists(db: AsyncSession) -> list[TaskList]:
    result = await db.execute(
        select(TaskList).where(TaskList.deleted_at.is_(None)).order_by(TaskList.display_name)
    )
    return list(result.scalars().all())


async def create_list(db: AsyncSession, display_name: str) -> TaskList:
    task_list = TaskList(display_name=display_name, sync_status="pending_push")
    db.add(task_list)
    await db.flush()
    try:
        result = await graph_client.create_list(display_name)
        task_list.ms_id = result.get("id")
        task_list.sync_status = "synced"
    except Exception:
        logger.exception("Failed to push list to Graph, will retry on next sync")
    await db.commit()
    await db.refresh(task_list)
    return task_list


async def update_list(db: AsyncSession, list_id: uuid.UUID, display_name: str) -> TaskList | None:
    result = await db.execute(
        select(TaskList).where(TaskList.id == list_id, TaskList.deleted_at.is_(None))
    )
    task_list = result.scalar_one_or_none()
    if not task_list:
        return None
    task_list.display_name = display_name
    task_list.sync_status = "pending_push"
    if task_list.ms_id:
        try:
            await graph_client.update_list(task_list.ms_id, display_name)
            task_list.sync_status = "synced"
        except Exception:
            logger.exception("Failed to push list update to Graph")
    await db.commit()
    await db.refresh(task_list)
    return task_list


async def delete_list(db: AsyncSession, list_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(TaskList).where(TaskList.id == list_id, TaskList.deleted_at.is_(None))
    )
    task_list = result.scalar_one_or_none()
    if not task_list:
        return False
    task_list.deleted_at = datetime.now(timezone.utc)
    task_list.sync_status = "pending_push"
    if task_list.ms_id:
        try:
            await graph_client.delete_list(task_list.ms_id)
            task_list.sync_status = "synced"
        except Exception:
            logger.exception("Failed to delete list from Graph")
    await db.commit()
    return True


# --- Tasks ---

async def get_tasks(
    db: AsyncSession,
    list_id: uuid.UUID | None = None,
    filter: str | None = None,
    status: str | None = None,
    importance: str | None = None,
    overdue: bool = False,
    due_before: date | None = None,
    due_after: date | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    today = _today()
    q = select(Task).where(Task.deleted_at.is_(None))
    if list_id:
        q = q.where(Task.list_id == list_id)
    # Convenience filter shortcuts (exclude completed by default)
    if filter == "today":
        q = q.where(and_(Task.due_date == today, Task.status != "completed"))
    elif filter == "overdue":
        q = q.where(and_(Task.due_date < today, Task.status != "completed"))
    elif filter == "week":
        week_end = today + timedelta(days=7)
        q = q.where(and_(Task.due_date >= today, Task.due_date <= week_end, Task.status != "completed"))
    else:
        # Original individual filters
        if status:
            q = q.where(Task.status == status)
        if overdue:
            q = q.where(and_(Task.due_date < today, Task.status != "completed"))
        if due_before:
            q = q.where(Task.due_date <= due_before)
        if due_after:
            q = q.where(Task.due_date >= due_after)
    if importance:
        q = q.where(Task.importance == importance)
    if search:
        pattern = f"%{search}%"
        q = q.where(or_(Task.title.ilike(pattern), Task.body.ilike(pattern)))
    q = q.order_by(Task.due_date.asc().nulls_last(), Task.created_at.desc())
    q = q.limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_task(db: AsyncSession, task_id: uuid.UUID) -> Task | None:
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def _try_push_checklist_items(task: Task, list_ms_id: str | None) -> None:
    """Best-effort immediate push checklist items в Graph после успешного create/update.

    Lazy-import _push_checklist_items из sync_service, чтобы избежать circular.
    """
    if not list_ms_id or not task.ms_id or not task.checklist_items:
        return
    try:
        from app.services.sync_service import _push_checklist_items
        await _push_checklist_items(task, list_ms_id)
    except Exception:
        logger.exception("Failed to push checklist items for task %s, will retry on next sync", task.id)


async def create_task(db: AsyncSession, data: TaskCreate) -> Task:
    list_result = await db.execute(
        select(TaskList).where(TaskList.id == data.list_id, TaskList.deleted_at.is_(None))
    )
    task_list = list_result.scalar_one_or_none()
    if not task_list:
        raise ValueError(f"Task list {data.list_id} not found")

    task = Task(
        list_id=data.list_id,
        title=data.title,
        body=data.body,
        importance=data.importance,
        due_date=data.due_date,
        reminder_datetime=data.reminder_datetime,
        is_reminder_on=data.is_reminder_on,
        categories=data.categories,
        checklist_items=[it.model_dump() for it in data.checklist_items],
        sync_status="pending_push",
    )
    db.add(task)
    await db.flush()
    await _try_push_task(task, task_list.ms_id, "create")
    await _try_push_checklist_items(task, task_list.ms_id)
    await db.commit()
    await db.refresh(task)
    return task


async def update_task(db: AsyncSession, task_id: uuid.UUID, data: TaskUpdate) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None
    update_fields = data.model_dump(exclude_unset=True)
    checklist_touched = "checklist_items" in update_fields
    for field, value in update_fields.items():
        setattr(task, field, value)
    task.sync_status = "pending_push"

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    list_ms_id = task_list.ms_id if task_list else None
    await _try_push_task(task, list_ms_id, "update")
    if checklist_touched:
        await _try_push_checklist_items(task, list_ms_id)
    await db.commit()
    await db.refresh(task)
    return task


async def complete_task(db: AsyncSession, task_id: uuid.UUID) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None
    task.status = "completed"
    task.completed_datetime = datetime.now(timezone.utc)
    task.sync_status = "pending_push"

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    await _try_push_task(task, task_list.ms_id if task_list else None, "update")
    await db.commit()
    await db.refresh(task)
    return task


async def uncomplete_task(db: AsyncSession, task_id: uuid.UUID) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None
    task.status = "notStarted"
    task.completed_datetime = None
    task.sync_status = "pending_push"

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    await _try_push_task(task, task_list.ms_id if task_list else None, "update")
    await db.commit()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, task_id: uuid.UUID) -> bool:
    task = await get_task(db, task_id)
    if not task:
        return False
    task.deleted_at = datetime.now(timezone.utc)
    task.sync_status = "pending_push"

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    await _try_push_task(task, task_list.ms_id if task_list else None, "delete")
    await db.commit()
    return True


# --- Stats ---

async def get_stats(db: AsyncSession) -> dict:
    today = _today()
    week_end = today + timedelta(days=7)

    result = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(Task.status == "notStarted").label("not_started"),
            func.count().filter(Task.status == "inProgress").label("in_progress"),
            func.count().filter(Task.status == "completed").label("completed"),
            func.count().filter(and_(Task.due_date < today, Task.status != "completed")).label("overdue"),
            func.count().filter(and_(Task.due_date == today, Task.status != "completed")).label("due_today"),
            func.count().filter(and_(Task.due_date >= today, Task.due_date <= week_end, Task.status != "completed")).label("due_this_week"),
        ).where(Task.deleted_at.is_(None))
    )
    row = result.one()

    by_list_result = await db.execute(
        select(
            TaskList.id,
            TaskList.display_name,
            func.count(Task.id).label("count"),
            func.count().filter(Task.status != "completed").label("incomplete"),
        )
        .outerjoin(Task, and_(Task.list_id == TaskList.id, Task.deleted_at.is_(None)))
        .where(TaskList.deleted_at.is_(None))
        .group_by(TaskList.id, TaskList.display_name)
    )

    return {
        "total": row.total,
        "not_started": row.not_started,
        "in_progress": row.in_progress,
        "completed": row.completed,
        "overdue": row.overdue,
        "due_today": row.due_today,
        "due_this_week": row.due_this_week,
        "by_list": [
            {"list_id": str(r.id), "display_name": r.display_name, "count": r.count, "incomplete": r.incomplete}
            for r in by_list_result.all()
        ],
    }


async def get_upcoming_reminders(db: AsyncSession, hours: int = 24) -> list[Task]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)
    result = await db.execute(
        select(Task).where(
            Task.deleted_at.is_(None),
            Task.is_reminder_on.is_(True),
            Task.reminder_datetime >= now,
            Task.reminder_datetime <= cutoff,
            Task.status != "completed",
        ).order_by(Task.reminder_datetime)
    )
    return list(result.scalars().all())


async def get_overdue_tasks(db: AsyncSession) -> list[Task]:
    result = await db.execute(
        select(Task).where(
            Task.deleted_at.is_(None),
            Task.due_date < _today(),
            Task.status != "completed",
        ).order_by(Task.due_date)
    )
    return list(result.scalars().all())
