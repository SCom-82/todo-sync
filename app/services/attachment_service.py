"""F2.2/F2.3: Service layer for task_attachments."""
import base64
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TaskAttachment
from app.services.graph_client import graph_client

logger = logging.getLogger(__name__)



async def _set_task_has_attachments(db, task_id: uuid.UUID, value: bool) -> None:
    """F3.5: Update Task.has_attachments flag."""
    from app.models import Task
    from sqlalchemy import select as sa_select
    result = await db.execute(sa_select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task:
        task.has_attachments = value


async def create_file(
    db: AsyncSession,
    task_id: uuid.UUID,
    name: str,
    content_type: str,
    content: bytes,
) -> TaskAttachment:
    att = TaskAttachment(
        task_id=task_id,
        name=name,
        content_type=content_type,
        size_bytes=len(content),
        content_bytes=content,
        sync_status="pending",
    )
    db.add(att)
    await db.flush()

    # F3.5: mark task as having attachments
    await _set_task_has_attachments(db, task_id, True)

    # Best-effort push to Graph
    await _try_push_to_graph(db, att, task_id, content)

    await db.commit()
    await db.refresh(att)
    return att


async def create_reference(
    db: AsyncSession,
    task_id: uuid.UUID,
    url: str,
    name: str,
) -> TaskAttachment:
    att = TaskAttachment(
        task_id=task_id,
        name=name,
        reference_url=url,
        sync_status="pending",
    )
    db.add(att)
    await db.flush()

    # F3.5: mark task as having attachments
    await _set_task_has_attachments(db, task_id, True)

    # Reference attachments are stored locally only (Graph supports file attachments via upload session)
    # For now mark as synced since we don't push reference attachments to Graph
    att.sync_status = "synced"

    await db.commit()
    await db.refresh(att)
    return att


async def _try_push_to_graph(
    db: AsyncSession,
    att: TaskAttachment,
    task_id: uuid.UUID,
    content: bytes,
) -> None:
    from app.models import Task, TaskList
    from sqlalchemy import select as sa_select

    result = await db.execute(sa_select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task or not task.ms_id:
        return

    list_result = await db.execute(sa_select(TaskList).where(TaskList.id == task.list_id))
    task_list = list_result.scalar_one_or_none()
    if not task_list or not task_list.ms_id:
        return

    try:
        resp = await graph_client.create_attachment(
            task_list.ms_id,
            task.ms_id,
            {
                "@odata.type": "#microsoft.graph.taskFileAttachment",
                "name": att.name,
                "contentType": att.content_type or "application/octet-stream",
                "contentBytes": base64.b64encode(content).decode("ascii"),
                "size": len(content),
            },
        )
        att.ms_id = resp.get("id")
        att.sync_status = "synced"
    except Exception:
        logger.exception("Failed to push attachment to Graph for task %s", task_id)


async def list_for_task(db: AsyncSession, task_id: uuid.UUID) -> list[TaskAttachment]:
    result = await db.execute(
        select(TaskAttachment).where(TaskAttachment.task_id == task_id)
    )
    return list(result.scalars().all())


async def delete(db: AsyncSession, att_id: uuid.UUID) -> bool:
    att = await db.get(TaskAttachment, att_id)
    if not att:
        return False

    # Best-effort delete from Graph if synced
    if att.ms_id and att.sync_status == "synced":
        from app.models import Task, TaskList
        from sqlalchemy import select as sa_select

        result = await db.execute(sa_select(Task).where(Task.id == att.task_id))
        task = result.scalar_one_or_none()
        if task and task.ms_id:
            list_result = await db.execute(sa_select(TaskList).where(TaskList.id == task.list_id))
            task_list = list_result.scalar_one_or_none()
            if task_list and task_list.ms_id:
                try:
                    await graph_client.delete_attachment(task_list.ms_id, task.ms_id, att.ms_id)
                except Exception:
                    logger.exception("Failed to delete attachment from Graph %s", att_id)

    task_id_for_check = att.task_id
    await db.delete(att)
    await db.flush()

    # F3.5: recheck has_attachments after deletion
    remaining = await db.execute(
        select(TaskAttachment).where(TaskAttachment.task_id == task_id_for_check)
    )
    if not remaining.scalars().first():
        await _set_task_has_attachments(db, task_id_for_check, False)

    await db.commit()
    return True
