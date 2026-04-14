"""F2.1: Service layer for linked_resources."""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LinkedResource
from app.schemas import LinkedResourceIn, LinkedResourceUpdate
from app.services.graph_client import graph_client

logger = logging.getLogger(__name__)


async def create(db: AsyncSession, task_id: uuid.UUID, data: LinkedResourceIn) -> LinkedResource:
    lr = LinkedResource(
        task_id=task_id,
        web_url=str(data.web_url),
        display_name=data.display_name,
        application_name=data.application_name,
        external_id=data.external_id,
        sync_status="pending",
    )
    db.add(lr)
    await db.flush()

    # Best-effort push to Graph
    from app.models import Task, TaskList
    from sqlalchemy import select as sa_select
    result = await db.execute(sa_select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task and task.ms_id:
        list_result = await db.execute(sa_select(TaskList).where(TaskList.id == task.list_id))
        task_list = list_result.scalar_one_or_none()
        if task_list and task_list.ms_id:
            try:
                resp = await graph_client.create_linked_resource(
                    task_list.ms_id,
                    task.ms_id,
                    {
                        "webUrl": str(data.web_url),
                        "displayName": data.display_name,
                        **({"applicationName": data.application_name} if data.application_name else {}),
                        **({"externalId": data.external_id} if data.external_id else {}),
                    },
                )
                lr.ms_id = resp.get("id")
                lr.sync_status = "synced"
            except Exception:
                logger.exception("Failed to push linked_resource to Graph for task %s", task_id)

    await db.commit()
    await db.refresh(lr)
    return lr


async def list_for_task(db: AsyncSession, task_id: uuid.UUID) -> list[LinkedResource]:
    result = await db.execute(
        select(LinkedResource).where(LinkedResource.task_id == task_id)
    )
    return list(result.scalars().all())


async def update(db: AsyncSession, lr_id: uuid.UUID, data: LinkedResourceUpdate) -> LinkedResource | None:
    lr = await db.get(LinkedResource, lr_id)
    if not lr:
        return None

    fields = data.model_dump(exclude_unset=True)
    if "web_url" in fields and fields["web_url"] is not None:
        fields["web_url"] = str(fields["web_url"])

    for k, v in fields.items():
        setattr(lr, k, v)

    lr.updated_at = datetime.now(timezone.utc)

    # Best-effort push to Graph
    if lr.ms_id:
        from app.models import Task, TaskList
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Task).where(Task.id == lr.task_id))
        task = result.scalar_one_or_none()
        if task and task.ms_id:
            list_result = await db.execute(sa_select(TaskList).where(TaskList.id == task.list_id))
            task_list = list_result.scalar_one_or_none()
            if task_list and task_list.ms_id:
                try:
                    patch = {}
                    if "web_url" in fields:
                        patch["webUrl"] = fields["web_url"]
                    if "display_name" in fields:
                        patch["displayName"] = fields["display_name"]
                    if "application_name" in fields:
                        patch["applicationName"] = fields["application_name"]
                    if "external_id" in fields:
                        patch["externalId"] = fields["external_id"]
                    if patch:
                        await graph_client.update_linked_resource(
                            task_list.ms_id, task.ms_id, lr.ms_id, patch
                        )
                except Exception:
                    logger.exception("Failed to push linked_resource update to Graph %s", lr_id)

    await db.commit()
    await db.refresh(lr)
    return lr


async def delete(db: AsyncSession, lr_id: uuid.UUID) -> bool:
    lr = await db.get(LinkedResource, lr_id)
    if not lr:
        return False

    # Best-effort delete from Graph
    if lr.ms_id:
        from app.models import Task, TaskList
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Task).where(Task.id == lr.task_id))
        task = result.scalar_one_or_none()
        if task and task.ms_id:
            list_result = await db.execute(sa_select(TaskList).where(TaskList.id == task.list_id))
            task_list = list_result.scalar_one_or_none()
            if task_list and task_list.ms_id:
                try:
                    await graph_client.delete_linked_resource(task_list.ms_id, task.ms_id, lr.ms_id)
                except Exception:
                    logger.exception("Failed to delete linked_resource from Graph %s", lr_id)

    await db.delete(lr)
    await db.commit()
    return True
