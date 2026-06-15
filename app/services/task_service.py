import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Task, TaskList
from app.schemas import TaskCreate, TaskUpdate, PatternedRecurrence
from app.services.graph_client import graph_client

logger = logging.getLogger(__name__)


def _today() -> date:
    """Get today's date in user's timezone (not UTC)."""
    return datetime.now(ZoneInfo(settings.user_timezone)).date()


# --- F1.1: list resolution helpers ---

async def _resolve_list(db: AsyncSession, data: TaskCreate) -> TaskList:
    """Resolve list from list_id, list_name, or list_ms_id. Raises ValueError on failure."""
    if data.list_id is not None:
        result = await db.execute(
            select(TaskList).where(TaskList.id == data.list_id, TaskList.deleted_at.is_(None))
        )
        task_list = result.scalar_one_or_none()
        if not task_list:
            raise ValueError(f"Task list {data.list_id} not found")
        return task_list

    if data.list_name is not None:
        result = await db.execute(
            select(TaskList).where(
                TaskList.display_name == data.list_name,
                TaskList.deleted_at.is_(None),
            )
        )
        matches = result.scalars().all()
        if not matches:
            raise ValueError(f"Task list '{data.list_name}' not found")
        if len(matches) > 1:
            raise ValueError(
                f"Multiple lists named '{data.list_name}' found ({len(matches)}). Use list_id instead."
            )
        return matches[0]

    if data.list_ms_id is not None:
        result = await db.execute(
            select(TaskList).where(
                TaskList.ms_id == data.list_ms_id,
                TaskList.deleted_at.is_(None),
            )
        )
        task_list = result.scalar_one_or_none()
        if not task_list:
            raise ValueError(f"Task list with ms_id '{data.list_ms_id}' not found")
        return task_list

    raise ValueError("One of list_id, list_name, or list_ms_id must be provided")


def _recurrence_to_graph(rec: PatternedRecurrence) -> dict:
    """Serialize PatternedRecurrence to MS Graph patternedRecurrence payload.

    Emits ONLY populated non-sentinel sub-fields (empirically: 1b contract).
    Graph backfills sentinels in responses (month=0, daysOfWeek=[], endDate="0001-01-01", …)
    — those must NOT be re-sent or Graph returns HTTP 400 on Nullable=False fields.
    """
    pattern: dict = {
        "type": rec.pattern.type,
        "interval": rec.pattern.interval,
    }
    # daysOfWeek: skip empty list (Graph sentinel) and None
    if rec.pattern.daysOfWeek:  # truthy = non-empty list
        pattern["daysOfWeek"] = rec.pattern.daysOfWeek
    # firstDayOfWeek: Graph sets "sunday" as sentinel for non-weekly types; only include if explicitly set
    # and pattern type warrants it (weekly uses firstDayOfWeek meaningfully)
    if rec.pattern.firstDayOfWeek is not None:
        pt = rec.pattern.type
        if pt == "weekly" or rec.pattern.firstDayOfWeek != "sunday":
            # Include if it's a weekly pattern OR if caller set a non-sentinel value
            pattern["firstDayOfWeek"] = rec.pattern.firstDayOfWeek
    # index: Graph sets "first" as sentinel for non-relative types
    if rec.pattern.index is not None:
        pt = rec.pattern.type
        if pt in ("relativeMonthly", "relativeYearly") or rec.pattern.index != "first":
            pattern["index"] = rec.pattern.index
    # dayOfMonth: 0 is Graph sentinel
    if rec.pattern.dayOfMonth is not None and rec.pattern.dayOfMonth != 0:
        pattern["dayOfMonth"] = rec.pattern.dayOfMonth
    # month: 0 is Graph sentinel
    if rec.pattern.month is not None and rec.pattern.month != 0:
        pattern["month"] = rec.pattern.month

    rng: dict = {
        "type": rec.range.type,
        "startDate": rec.range.startDate,
    }
    # endDate: "0001-01-01" is Graph sentinel for noEnd ranges
    if rec.range.endDate is not None and rec.range.endDate != "0001-01-01":
        rng["endDate"] = rec.range.endDate
    # numberOfOccurrences: 0 is Graph sentinel
    if rec.range.numberOfOccurrences is not None and rec.range.numberOfOccurrences != 0:
        rng["numberOfOccurrences"] = rec.range.numberOfOccurrences

    return {"pattern": pattern, "range": rng}


def _completion_patch_payload(task: Task) -> dict:
    """Build a status-only PATCH payload for completion/uncomplete operations.

    ADR §3 (completion-PATCH status-only, 2026-06-14): Graph returns HTTP 400 when a
    recurring-task PATCH includes recurrence/dueDateTime alongside status. The minimal
    status-only payload {"status":"completed","completedDateTime":{...}} → HTTP 200.
    Applied to ALL tasks (recurring and non-recurring) for uniformity and safety.
    """
    if task.status == "completed" and task.completed_datetime:
        dt = task.completed_datetime
        # Ensure timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return {
            "status": "completed",
            "completedDateTime": {
                "dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "timeZone": "UTC",
            },
        }
    # uncomplete: clear status and completedDateTime
    return {
        "status": "notStarted",
        "completedDateTime": None,
    }


def _task_to_graph_payload(task: Task) -> dict:
    payload: dict = {"title": task.title, "importance": task.importance, "status": task.status}

    # F1.3: body with contentType
    if task.body:
        payload["body"] = {"content": task.body, "contentType": task.body_content_type}

    # F1.2: prefer due_datetime over due_date
    if task.due_datetime:
        tz_str = task.due_timezone or "UTC"
        payload["dueDateTime"] = {
            "dateTime": task.due_datetime.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
            "timeZone": tz_str,
        }
    elif task.due_date:
        payload["dueDateTime"] = {
            "dateTime": datetime.combine(task.due_date, datetime.min.time()).isoformat(),
            "timeZone": task.due_timezone or "UTC",
        }

    # F1.2: startDateTime
    if task.start_datetime:
        tz_str = task.start_timezone or "UTC"
        payload["startDateTime"] = {
            "dateTime": task.start_datetime.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
            "timeZone": tz_str,
        }

    if task.is_reminder_on and task.reminder_datetime:
        payload["isReminderOn"] = True
        payload["reminderDateTime"] = {
            "dateTime": task.reminder_datetime.isoformat(),
            "timeZone": "UTC",
        }

    if task.categories:
        payload["categories"] = task.categories

    # F1.4: recurrence — build clean payload (no null sub-fields → Graph 400).
    # Stored as raw JSONB dict from Graph (may contain sentinel values like month=0,
    # endDate="0001-01-01", daysOfWeek=[] that Graph backfills on pull).
    # We re-serialize using _recurrence_to_graph to emit only populated fields.
    if task.recurrence:
        try:
            from app.schemas import PatternedRecurrence
            rec_obj = PatternedRecurrence.model_validate(task.recurrence)
            payload["recurrence"] = _recurrence_to_graph(rec_obj)
        except Exception:
            # If stored dict does not match schema (e.g. sentinel-heavy pull data),
            # fall back to a minimal clean rebuild from raw dict to strip nulls.
            rec = task.recurrence
            pattern_raw = rec.get("pattern", {}) if isinstance(rec, dict) else {}
            range_raw = rec.get("range", {}) if isinstance(rec, dict) else {}
            pattern_out: dict = {}
            for k in ("type", "interval", "daysOfWeek", "firstDayOfWeek", "index", "dayOfMonth", "month"):
                v = pattern_raw.get(k)
                # Skip None and sentinel values that Graph backfills
                if v is None:
                    continue
                if k == "daysOfWeek" and v == []:
                    continue
                if k in ("month", "dayOfMonth") and v == 0:
                    continue
                if k == "index" and v == "first":
                    # "first" is a sentinel Graph adds; keep only if pattern type is relativeMonthly/relativeYearly
                    pt = pattern_raw.get("type", "")
                    if pt not in ("relativeMonthly", "relativeYearly"):
                        continue
                pattern_out[k] = v
            range_out: dict = {}
            for k in ("type", "startDate"):
                v = range_raw.get(k)
                if v is not None:
                    range_out[k] = v
            end_date = range_raw.get("endDate")
            if end_date and end_date != "0001-01-01":
                range_out["endDate"] = end_date
            num_occ = range_raw.get("numberOfOccurrences")
            if num_occ is not None and num_occ != 0:
                range_out["numberOfOccurrences"] = num_occ
            if pattern_out and range_out:
                payload["recurrence"] = {"pattern": pattern_out, "range": range_out}

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
    list_name: str | None = None,
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

    # F1.1: resolve list_name → list_id
    if list_name is not None and list_id is None:
        lr = await db.execute(
            select(TaskList).where(
                TaskList.display_name == list_name,
                TaskList.deleted_at.is_(None),
            )
        )
        matches = lr.scalars().all()
        if not matches:
            return []
        if len(matches) == 1:
            list_id = matches[0].id
        else:
            # multiple matches — filter by any of them
            list_ids = [m.id for m in matches]
            q = q.where(Task.list_id.in_(list_ids))

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


def _validate_graph_id(resp: dict) -> str | None:
    """Return Graph id from response if it looks like a valid Graph Task base64 id.

    Delegates to graph_id.is_task_graph_id — single source of truth.
    Kept here as private alias so existing callers inside task_service are not changed.
    """
    from app.services.graph_id import is_task_graph_id
    return is_task_graph_id(resp)


async def _try_push_task(
    task: Task, list_ms_id: str | None, action: str, payload_override: dict | None = None
) -> bool:
    """Best-effort immediate push to MS Graph. Returns True on success, False on failure.

    P0 push-verify: non-2xx responses and exceptions keep task in pending_push (not synced).
    Graph response body is validated — synced is only set when a real Graph id is confirmed.
    """
    if not list_ms_id:
        return False
    try:
        if action == "create":
            data = payload_override if payload_override is not None else _task_to_graph_payload(task)
            result = graph_client.create_task(list_ms_id, data)
            if asyncio.iscoroutine(result):
                result = await result
            graph_id = _validate_graph_id(result)
            if not graph_id:
                logger.error(
                    "Push create task %s: Graph response missing valid id (got %r). "
                    "Task stays pending_push.",
                    task.id, result.get("id"),
                )
                return False
            task.ms_id = graph_id
            task.sync_status = "synced"
            return True
        elif action == "update" and task.ms_id:
            data = payload_override if payload_override is not None else _task_to_graph_payload(task)
            result = graph_client.update_task(list_ms_id, task.ms_id, data)
            if asyncio.iscoroutine(result):
                result = await result
            task.sync_status = "synced"
            return True
        elif action == "delete" and task.ms_id:
            result = graph_client.delete_task(list_ms_id, task.ms_id)
            if asyncio.iscoroutine(result):
                await result
            task.sync_status = "synced"
            return True
    except Exception:
        logger.exception(
            "Failed to push task %s (action=%s) to Graph, will retry on next sync",
            task.id, action,
        )
    # On any failure: leave sync_status as pending_push (caller must NOT set synced).
    return False


async def _try_push_checklist_items(task: Task, list_ms_id: str | None) -> None:
    """Best-effort immediate push checklist items в Graph после успешного create/update."""
    if not list_ms_id or not task.ms_id or not task.checklist_items:
        return
    try:
        from app.services.sync_service import _push_checklist_items
        await _push_checklist_items(task, list_ms_id)
    except Exception:
        logger.exception("Failed to push checklist items for task %s, will retry on next sync", task.id)


def _validate_recurrence_has_due(
    recurrence: object | None,
    due_datetime: object | None,
    due_date: object | None,
) -> None:
    """Raise ValueError if recurrence is set but no dueDateTime/due_date provided.

    Graph requires dueDateTime for any recurring task (empirically: HTTP 400,
    'The property dueDateTime is required when creating recurrence in the task entity').
    We enforce this on our side so we never send a guaranteed-to-fail payload.
    """
    if recurrence is not None and due_datetime is None and due_date is None:
        raise ValueError(
            "A recurring task requires dueDateTime (or due_date). "
            "Graph rejects recurring tasks without dueDateTime with HTTP 400."
        )


async def create_task(db: AsyncSession, data: TaskCreate) -> Task:
    # F1.1: resolve list by name or ms_id
    task_list = await _resolve_list(db, data)

    # Validate: recurring task must have dueDateTime
    _validate_recurrence_has_due(data.recurrence, data.due_datetime, data.due_date)

    # F1.2: compute due_date from due_datetime if needed
    resolved_due_date = data.due_date
    if data.due_datetime and not resolved_due_date:
        tz_str = data.due_timezone or settings.user_timezone
        try:
            local_dt = data.due_datetime.astimezone(ZoneInfo(tz_str))
            resolved_due_date = local_dt.date()
        except Exception:
            resolved_due_date = data.due_datetime.date()

    now_utc = datetime.now(timezone.utc)
    task = Task(
        list_id=task_list.id,
        title=data.title,
        body=data.body,
        body_content_type=data.body_content_type,  # F1.3
        importance=data.importance,
        due_date=resolved_due_date,
        due_timezone=data.due_timezone or settings.user_timezone,
        due_datetime=data.due_datetime,            # F1.2
        start_datetime=data.start_datetime,        # F1.2
        start_timezone=data.start_timezone,        # F1.2
        reminder_datetime=data.reminder_datetime,
        is_reminder_on=data.is_reminder_on,
        categories=data.categories,
        checklist_items=[it.model_dump() for it in data.checklist_items],
        recurrence=data.recurrence.model_dump() if data.recurrence else None,  # F1.4
        sync_status="pending_push",
        local_modified_at=now_utc,  # ADR §1: track local change time for conflict-guard
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

    # F1.2: if due_datetime is being set, recompute due_date
    if "due_datetime" in update_fields and update_fields["due_datetime"] is not None:
        due_dt = update_fields["due_datetime"]
        tz_str = update_fields.get("due_timezone") or task.due_timezone or settings.user_timezone
        try:
            local_dt = due_dt.astimezone(ZoneInfo(tz_str))
            update_fields.setdefault("due_date", local_dt.date())
        except Exception:
            update_fields.setdefault("due_date", due_dt.date())

    # F1.4: serialize recurrence Pydantic model to dict for JSONB
    if "recurrence" in update_fields and update_fields["recurrence"] is not None:
        rec_obj = update_fields["recurrence"]
        if isinstance(rec_obj, dict):
            pass  # already dict (shouldn't happen, but safe)
        else:
            update_fields["recurrence"] = rec_obj.model_dump()

    for field, value in update_fields.items():
        setattr(task, field, value)
    task.sync_status = "pending_push"
    task.local_modified_at = datetime.now(timezone.utc)  # ADR §1: conflict-guard comparator

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    list_ms_id = task_list.ms_id if task_list else None
    # ADR §3 (secondary): for recurring tasks, only include recurrence in the update PATCH
    # if it was actually changed — otherwise Graph may 400 on a full-payload recurring PATCH.
    update_payload = _task_to_graph_payload(task)
    if task.recurrence and "recurrence" not in update_fields:
        update_payload.pop("recurrence", None)
    await _try_push_task(task, list_ms_id, "update", payload_override=update_payload)
    if checklist_touched:
        await _try_push_checklist_items(task, list_ms_id)
    await db.commit()
    await db.refresh(task)
    return task


async def complete_task(db: AsyncSession, task_id: uuid.UUID) -> Task | None:
    task = await get_task(db, task_id)
    if not task:
        return None
    now_utc = datetime.now(timezone.utc)
    task.status = "completed"
    task.completed_datetime = now_utc
    task.sync_status = "pending_push"
    task.local_modified_at = now_utc  # ADR §1: conflict-guard comparator

    # ADR §2: set completion-intent marker for conflict-guard.
    # Records which dueDate occurrence we just completed. On next pull, if Graph sends
    # the same ms_id in notStarted with dueDate shifted FORWARD — that's auto-advance, not uncomplete.
    if task.recurrence and task.due_date:
        task.last_completed_occurrence_date = task.due_date
        logger.debug(
            "complete_task %s: recurring — setting last_completed_occurrence_date=%s",
            task.id, task.due_date,
        )

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()

    # ADR §3: completion-PATCH must be status-only (recurring full-payload PATCH → Graph 400).
    push_succeeded = await _try_push_task(
        task, task_list.ms_id if task_list else None, "update",
        payload_override=_completion_patch_payload(task),
    )

    # ADR §3: for recurring tasks, do NOT mark synced after immediate push even if it succeeded.
    # Graph auto-advances the series (A: same id flips to notStarted with new dueDateTime) and
    # spawns a sibling (B: new id with completed status). The next pull must handle this
    # gracefully — conflict-guard needs the task in pending_push (not synced) to trigger.
    if push_succeeded and task.recurrence:
        task.sync_status = "pending_push"
        logger.debug(
            "complete_task %s: recurring — keeping pending_push after push (ADR §3, conflict-guard)",
            task.id,
        )

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
    task.local_modified_at = datetime.now(timezone.utc)  # ADR §1
    # Clear intent marker on local uncomplete
    task.last_completed_occurrence_date = None

    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    # ADR §3: uncomplete-PATCH must also be status-only (same Graph-400 risk on recurring).
    await _try_push_task(
        task, task_list.ms_id if task_list else None, "update",
        payload_override=_completion_patch_payload(task),
    )
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


# --- Checklist point-edit (F1.5) ---

async def add_checklist_item(
    db: AsyncSession,
    task_id: uuid.UUID,
    display_name: str,
    is_checked: bool = False,
) -> dict | None:
    """Добавить один пункт чек-листа. Возвращает созданный item-dict или None если задача не найдена."""
    task = await get_task(db, task_id)
    if not task:
        return None

    new_item: dict = {"displayName": display_name, "isChecked": is_checked}

    # Try push to Graph immediately
    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    list_ms_id = task_list.ms_id if task_list else None

    if list_ms_id and task.ms_id:
        try:
            created = await graph_client.create_checklist_item(
                list_ms_id, task.ms_id,
                {"displayName": display_name, "isChecked": is_checked},
            )
            new_item["id"] = created.get("id")
            if created.get("createdDateTime"):
                new_item["createdDateTime"] = created["createdDateTime"]
        except Exception:
            logger.exception("Failed to push new checklist item to Graph for task %s", task_id)
            task.sync_status = "pending_push"

    # Update local JSONB
    items = list(task.checklist_items or [])
    items.append(new_item)
    task.checklist_items = items
    await db.commit()
    await db.refresh(task)
    return new_item


async def update_checklist_item(
    db: AsyncSession,
    task_id: uuid.UUID,
    item_id: str,
    display_name: str | None,
    is_checked: bool | None,
) -> dict | None:
    """Обновить один пункт чек-листа по его ms_id. Возвращает обновлённый item или None."""
    task = await get_task(db, task_id)
    if not task:
        return None

    items = list(task.checklist_items or [])
    target = None
    for item in items:
        if item.get("id") == item_id:
            target = item
            break
    if target is None:
        return None

    # Apply changes locally
    if display_name is not None:
        target["displayName"] = display_name
    if is_checked is not None:
        target["isChecked"] = is_checked

    # Try push to Graph
    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    list_ms_id = task_list.ms_id if task_list else None

    if list_ms_id and task.ms_id:
        try:
            patch_data: dict = {}
            if display_name is not None:
                patch_data["displayName"] = display_name
            if is_checked is not None:
                patch_data["isChecked"] = is_checked
            await graph_client.update_checklist_item(list_ms_id, task.ms_id, item_id, patch_data)
        except Exception:
            logger.exception("Failed to push checklist item update to Graph for task %s item %s", task_id, item_id)
            task.sync_status = "pending_push"

    task.checklist_items = [dict(it) for it in items]
    await db.commit()
    await db.refresh(task)
    return target


async def remove_checklist_item(
    db: AsyncSession,
    task_id: uuid.UUID,
    item_id: str,
) -> bool:
    """Удалить один пункт чек-листа по ms_id. Возвращает True если удалён."""
    task = await get_task(db, task_id)
    if not task:
        return False

    items = list(task.checklist_items or [])
    original_len = len(items)
    items = [it for it in items if it.get("id") != item_id]
    if len(items) == original_len:
        return False  # item not found

    # Try push to Graph
    list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    list_ms_id = task_list.ms_id if task_list else None

    if list_ms_id and task.ms_id:
        try:
            await graph_client.delete_checklist_item(list_ms_id, task.ms_id, item_id)
        except Exception:
            logger.exception("Failed to delete checklist item from Graph for task %s item %s", task_id, item_id)
            task.sync_status = "pending_push"

    task.checklist_items = [dict(it) for it in items]
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
