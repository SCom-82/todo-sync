"""F2.3: REST endpoints for task_attachments."""
import base64
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Task, TaskAttachment, MAX_ATTACHMENT_BYTES
from app.schemas import AttachmentOut, AttachmentContentOut, LinkedResourceIn, LinkedResourceOut
from app.services import attachment_service, linked_resource_service

router = APIRouter(tags=["attachments"])


@router.post("/tasks/{task_id}/attachments", response_model=AttachmentOut, status_code=201)
async def upload_attachment(
    task_id: uuid.UUID,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file attachment (multipart/form-data). Hard limit: 3 MB."""
    task = await db.get(Task, task_id)
    if not task or task.deleted_at:
        raise HTTPException(status_code=404, detail="Task not found")

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Attachment too large: {len(content)} bytes > {MAX_ATTACHMENT_BYTES} bytes (3 MB limit)",
        )

    attachment_name = name or file.filename or "attachment"
    content_type = file.content_type or "application/octet-stream"

    att = await attachment_service.create_file(
        db,
        task_id=task_id,
        name=attachment_name,
        content_type=content_type,
        content=content,
    )
    return att


@router.post("/tasks/{task_id}/attachments/url", response_model=LinkedResourceOut, status_code=201)
async def attach_url(
    task_id: uuid.UUID,
    url: str,
    name: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Attach a URL by creating a linkedResource (pushes to Microsoft Graph).

    ADR 0003 §B-4 fix: Graph todoTask attachments only support file content (contentBytes).
    URL references must go through linkedResources, which is the correct Graph mechanism
    and produces a visible link in the MS To Do task details view.

    Response shape changed from AttachmentOut to LinkedResourceOut (same HTTP 201 status).
    """
    task = await db.get(Task, task_id)
    if not task or task.deleted_at:
        raise HTTPException(status_code=404, detail="Task not found")

    data = LinkedResourceIn(
        web_url=url,
        display_name=name or url,
        application_name="todo-sync",
    )
    lr = await linked_resource_service.create(db, task_id, data)
    return lr


@router.get("/tasks/{task_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task or task.deleted_at:
        raise HTTPException(status_code=404, detail="Task not found")
    return await attachment_service.list_for_task(db, task_id)


@router.get("/attachments/{att_id}", response_model=AttachmentContentOut)
async def get_attachment(
    att_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get attachment metadata + base64-encoded content (if stored)."""
    att = await db.get(TaskAttachment, att_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    out = AttachmentContentOut.model_validate(att)
    if att.content_bytes:
        out.content_base64 = base64.b64encode(att.content_bytes).decode("ascii")
    return out


@router.delete("/attachments/{att_id}", status_code=204)
async def delete_attachment(
    att_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    deleted = await attachment_service.delete(db, att_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Attachment not found")
