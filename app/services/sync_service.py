import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import LinkedResource, SyncLog, SyncState, Task, TaskAttachment, TaskList
from app.services.graph_client import DeltaLinkExpiredError, graph_client

logger = logging.getLogger(__name__)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def _parse_date(dt_obj: dict | None):
    """Parse dateTimeTimeZone from Graph API into a date in user's timezone.

    Graph API returns dueDateTime as UTC (e.g. 2026-03-16T20:00:00 UTC = 2026-03-17 00:00 Samara).
    We convert to user timezone before extracting date to avoid -1 day shift.
    """
    if not dt_obj:
        return None, "UTC"
    raw = dt_obj.get("dateTime", "")
    tz = dt_obj.get("timeZone", "UTC")
    if not raw:
        return None, tz
    from zoneinfo import ZoneInfo
    from app.config import settings
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # If naive datetime, assume UTC (Graph API sends UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Convert to user timezone to get the correct date
    user_tz = ZoneInfo(settings.user_timezone)
    dt_local = dt.astimezone(user_tz)
    return dt_local.date(), tz


def _parse_dt_obj(dt_obj: dict | None) -> tuple[datetime | None, str | None]:
    """Parse Graph dateTimeTimeZone into (tz-aware datetime, tz name).

    Returns (None, None) if dt_obj is empty or invalid.
    Unlike _parse_date, preserves full datetime precision for F1.2 write-path parity.
    """
    if not dt_obj or not isinstance(dt_obj, dict):
        return None, None
    raw = dt_obj.get("dateTime") or ""
    tz = dt_obj.get("timeZone")
    if not raw:
        return None, tz
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, tz


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

    # F3.6: increment delta attempt counter before each delta call
    state.delta_syncs_total = (state.delta_syncs_total or 0) + 1
    _delta_full_reset = False
    try:
        delta_result = await graph_client.get_tasks_delta(task_list.ms_id, state.delta_link)
        state.delta_syncs_succeeded = (state.delta_syncs_succeeded or 0) + 1
    except DeltaLinkExpiredError:
        logger.warning("Tasks delta link expired for list %s, doing full pull", task_list.ms_id)
        state.delta_link = None
        state.delta_full_resets_total = (state.delta_full_resets_total or 0) + 1
        _delta_full_reset = True
        delta_result = await graph_client.get_tasks_delta(task_list.ms_id, None)
        state.delta_syncs_succeeded = (state.delta_syncs_succeeded or 0) + 1
    except Exception as e:
        if "400" in str(e) and state.delta_link:
            logger.warning("Bad delta link for list %s, resetting and retrying", task_list.ms_id)
            state.delta_link = None
            state.delta_full_resets_total = (state.delta_full_resets_total or 0) + 1
            _delta_full_reset = True
            delta_result = await graph_client.get_tasks_delta(task_list.ms_id, None)
            state.delta_syncs_succeeded = (state.delta_syncs_succeeded or 0) + 1
        else:
            raise

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

        raw_due = item.get("dueDateTime")
        if raw_due:
            logger.info("RAW dueDateTime for '%s': %s", item.get("title", "?")[:50], raw_due)
        due_date, due_tz = _parse_date(raw_due)
        due_dt, _ = _parse_dt_obj(raw_due)
        start_dt, start_tz = _parse_dt_obj(item.get("startDateTime"))
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
            "due_datetime": due_dt,
            "start_datetime": start_dt,
            "start_timezone": start_tz,
            "reminder_datetime": reminder_dt,
            "is_reminder_on": item.get("isReminderOn", False),
            "completed_datetime": completed_dt,
            "completed_timezone": completed_tz,
            "recurrence": item.get("recurrence"),
            "categories": item.get("categories", []),
            "ms_created_at": _parse_datetime(item.get("createdDateTime")),
            "ms_last_modified": ms_modified,
            # F3.5: hasAttachments from Graph delta response
            "has_attachments": bool(item.get("hasAttachments", False)),
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

    # Graph Delta API не поддерживает $expand=checklistItems, поэтому подтягиваем
    # checklist items отдельным запросом per-task для всех изменённых (не удалённых) задач.
    # Флаг sync_status == "pending_push" защищает локальные несинхронизированные правки.
    await db.flush()
    upserted_ms_ids = [
        item["id"] for item in delta_result["value"]
        if "@removed" not in item and item.get("id")
    ]
    for task_ms_id in upserted_ms_ids:
        try:
            items = await graph_client.get_checklist_items(task_list.ms_id, task_ms_id)
        except Exception as e:
            logger.warning("Failed to fetch checklistItems for task %s: %s", task_ms_id, e)
            continue
        result = await db.execute(select(Task).where(Task.ms_id == task_ms_id))
        task = result.scalar_one_or_none()
        if task and task.sync_status != "pending_push":
            task.checklist_items = items

    # F2.5: Pull linked_resources for changed tasks
    for task_ms_id in upserted_ms_ids:
        try:
            lr_items = await graph_client.list_linked_resources(task_list.ms_id, task_ms_id)
        except Exception as e:
            logger.warning("Failed to fetch linkedResources for task %s: %s", task_ms_id, e)
            continue
        result = await db.execute(select(Task).where(Task.ms_id == task_ms_id))
        task = result.scalar_one_or_none()
        if not task:
            continue
        remote_lr_ids = {lr.get("id") for lr in lr_items if lr.get("id")}
        existing_lrs = await db.execute(
            select(LinkedResource).where(LinkedResource.task_id == task.id)
        )
        existing_by_ms_id = {lr.ms_id: lr for lr in existing_lrs.scalars().all() if lr.ms_id}
        # Upsert
        for lr_data in lr_items:
            ms_id = lr_data.get("id")
            if not ms_id:
                continue
            if ms_id in existing_by_ms_id:
                lr = existing_by_ms_id[ms_id]
                lr.web_url = lr_data.get("webUrl", lr.web_url)
                lr.display_name = lr_data.get("displayName", lr.display_name)
                lr.application_name = lr_data.get("applicationName")
                lr.external_id = lr_data.get("externalId")
                lr.sync_status = "synced"
            else:
                new_lr = LinkedResource(
                    task_id=task.id,
                    ms_id=ms_id,
                    web_url=lr_data.get("webUrl", ""),
                    display_name=lr_data.get("displayName", ""),
                    application_name=lr_data.get("applicationName"),
                    external_id=lr_data.get("externalId"),
                    sync_status="synced",
                )
                db.add(new_lr)
        # Remove deleted
        for ms_id, lr in existing_by_ms_id.items():
            if ms_id not in remote_lr_ids:
                await db.delete(lr)

    # F2.5: Pull attachments for tasks that have hasAttachments=True
    # (Graph does not support $expand=attachments in delta/list queries)
    tasks_with_attachments = [
        item["id"] for item in delta_result["value"]
        if not item.get("@removed") and item.get("hasAttachments") and item.get("id")
    ]
    for task_ms_id in tasks_with_attachments:
        try:
            att_items = await graph_client.list_attachments(task_list.ms_id, task_ms_id)
        except Exception as e:
            logger.warning("Failed to fetch attachments for task %s: %s", task_ms_id, e)
            continue
        result = await db.execute(select(Task).where(Task.ms_id == task_ms_id))
        task = result.scalar_one_or_none()
        if not task:
            continue
        remote_att_ids = {a.get("id") for a in att_items if a.get("id")}
        existing_atts = await db.execute(
            select(TaskAttachment).where(TaskAttachment.task_id == task.id)
        )
        existing_by_ms_id = {a.ms_id: a for a in existing_atts.scalars().all() if a.ms_id}
        for att_data in att_items:
            ms_id = att_data.get("id")
            if not ms_id:
                continue
            if ms_id not in existing_by_ms_id:
                new_att = TaskAttachment(
                    task_id=task.id,
                    ms_id=ms_id,
                    name=att_data.get("name", "attachment"),
                    content_type=att_data.get("contentType"),
                    size_bytes=att_data.get("size"),
                    sync_status="synced",
                )
                db.add(new_att)
        # Remove deleted
        for ms_id, att in existing_by_ms_id.items():
            if ms_id not in remote_att_ids:
                await db.delete(att)

    state.delta_link = delta_result.get("delta_link")
    state.last_sync_at = datetime.now(timezone.utc)
    state.last_sync_status = "success"

    return upserted, deleted


# --- Push: PostgreSQL → MS To Do ---

async def _push_checklist_items(task: Task, list_ms_id: str) -> None:
    """Diff local checklist_items vs remote и применить create/update/delete.

    Мутирует task.checklist_items (проставляет id новым элементам).
    Вызывается после успешного create_task/update_task в push_pending.
    """
    if not task.ms_id:
        return

    try:
        remote_items = await graph_client.get_checklist_items(list_ms_id, task.ms_id)
    except Exception:
        logger.exception("Failed to fetch remote checklistItems for task %s", task.id)
        return

    remote_by_id = {it["id"]: it for it in remote_items if it.get("id")}
    local_items = list(task.checklist_items or [])
    local_ids = {it.get("id") for it in local_items if it.get("id")}

    for remote_id in set(remote_by_id) - local_ids:
        try:
            await graph_client.delete_checklist_item(list_ms_id, task.ms_id, remote_id)
        except Exception:
            logger.exception("Failed to delete checklistItem %s", remote_id)

    changed = False
    for item in local_items:
        item_id = item.get("id")
        if not item_id:
            try:
                created = await graph_client.create_checklist_item(
                    list_ms_id,
                    task.ms_id,
                    {
                        "displayName": item.get("displayName", ""),
                        "isChecked": bool(item.get("isChecked", False)),
                    },
                )
                item["id"] = created.get("id")
                if created.get("createdDateTime"):
                    item["createdDateTime"] = created["createdDateTime"]
                changed = True
            except Exception:
                logger.exception("Failed to create checklistItem for task %s", task.id)
            continue

        remote = remote_by_id.get(item_id)
        if not remote:
            continue
        if (
            remote.get("displayName") != item.get("displayName")
            or bool(remote.get("isChecked")) != bool(item.get("isChecked"))
        ):
            try:
                await graph_client.update_checklist_item(
                    list_ms_id,
                    task.ms_id,
                    item_id,
                    {
                        "displayName": item.get("displayName", ""),
                        "isChecked": bool(item.get("isChecked", False)),
                    },
                )
            except Exception:
                logger.exception("Failed to update checklistItem %s", item_id)

    if changed:
        # SQLAlchemy не замечает мутации внутри JSONB — принудительно пересоздаём.
        task.checklist_items = [dict(it) for it in local_items]


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
            await _push_checklist_items(task, task_list.ms_id)
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

    # F2.5: Push pending linked_resources
    lr_result = await db.execute(
        select(LinkedResource).where(LinkedResource.sync_status == "pending")
    )
    for lr in lr_result.scalars().all():
        task_result = await db.execute(select(Task).where(Task.id == lr.task_id))
        task = task_result.scalar_one_or_none()
        if not task or not task.ms_id:
            continue
        list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
        task_list = list_result.scalar_one_or_none()
        if not task_list or not task_list.ms_id:
            continue
        try:
            resp = await graph_client.create_linked_resource(
                task_list.ms_id,
                task.ms_id,
                {
                    "webUrl": lr.web_url,
                    "displayName": lr.display_name,
                    **({"applicationName": lr.application_name} if lr.application_name else {}),
                    **({"externalId": lr.external_id} if lr.external_id else {}),
                },
            )
            lr.ms_id = resp.get("id")
            lr.sync_status = "synced"
        except Exception:
            logger.exception("Failed to push linked_resource %s to Graph", lr.id)
            lr.sync_status = "failed"

    # F2.5: Push pending attachments (file attachments only; reference attachments are local-only)
    att_result = await db.execute(
        select(TaskAttachment).where(
            TaskAttachment.sync_status == "pending",
            TaskAttachment.content_bytes.is_not(None),
        )
    )
    import base64 as _base64
    for att in att_result.scalars().all():
        task_result = await db.execute(select(Task).where(Task.id == att.task_id))
        task = task_result.scalar_one_or_none()
        if not task or not task.ms_id:
            continue
        list_result = await db.execute(select(TaskList).where(TaskList.id == task.list_id))
        task_list = list_result.scalar_one_or_none()
        if not task_list or not task_list.ms_id:
            continue
        try:
            resp = await graph_client.create_attachment(
                task_list.ms_id,
                task.ms_id,
                {
                    "@odata.type": "#microsoft.graph.taskFileAttachment",
                    "name": att.name,
                    "contentType": att.content_type or "application/octet-stream",
                    "contentBytes": _base64.b64encode(att.content_bytes).decode("ascii"),
                    "size": att.size_bytes or len(att.content_bytes),
                },
            )
            att.ms_id = resp.get("id")
            att.sync_status = "synced"
        except Exception:
            logger.exception("Failed to push attachment %s to Graph", att.id)
            att.sync_status = "failed"
            # Continue with next attachment - failed attachment does not block others

    return pushed_lists, pushed_tasks


# --- Main sync orchestrator ---

async def run_sync(sync_type: str = "delta") -> dict:
    """Run a full sync cycle: pull then push."""
    start = time.monotonic()
    total_pulled = 0
    total_pushed = 0
    total_deleted = 0
    total_errors = 0

    try:
        # Pull lists (own session)
        async with async_session() as db:
            lists_upserted, lists_deleted = await pull_lists(db)
            total_pulled += lists_upserted
            total_deleted += lists_deleted
            await db.commit()

        # Get list of (id, ms_id) for iteration
        async with async_session() as db:
            result = await db.execute(
                select(TaskList.id, TaskList.ms_id).where(
                    TaskList.deleted_at.is_(None), TaskList.ms_id.is_not(None)
                )
            )
            list_refs = [(row.id, row.ms_id) for row in result.all()]

        # Pull tasks for each list (separate session per list)
        for list_id, list_ms_id in list_refs:
            try:
                async with async_session() as db:
                    result = await db.execute(select(TaskList).where(TaskList.id == list_id))
                    task_list = result.scalar_one()
                    tasks_upserted, tasks_deleted = await pull_tasks_for_list(db, task_list)
                    total_pulled += tasks_upserted
                    total_deleted += tasks_deleted
                    await db.commit()
            except Exception:
                logger.exception("Failed to pull tasks for list %s", list_ms_id)
                total_errors += 1

        # Push pending changes (own session)
        async with async_session() as db:
            pushed_lists, pushed_tasks = await push_pending(db)
            total_pushed += pushed_lists + pushed_tasks
            await db.commit()

        duration_ms = int((time.monotonic() - start) * 1000)
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
        duration_ms = int((time.monotonic() - start) * 1000)
        async with async_session() as log_db:
            await _log_sync(log_db, sync_type, "all", errors=1, duration_ms=duration_ms)
            await log_db.commit()
        raise
