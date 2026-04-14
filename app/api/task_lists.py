import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import ShareListIn, ShareListOut, TaskListCreate, TaskListResponse, TaskListUpdate
from app.services import task_service
from app.services.graph_client import graph_client

router = APIRouter(prefix="/lists", tags=["task_lists"])


@router.get("", response_model=list[TaskListResponse])
async def list_task_lists(db: AsyncSession = Depends(get_db)):
    return await task_service.get_all_lists(db)


@router.get("/resolve", response_model=TaskListResponse)
async def resolve_task_list(
    name: str = Query(..., description="Точное отображаемое имя списка"),
    db: AsyncSession = Depends(get_db),
):
    """Найти список по display_name. 200 — exact match, 404 — не найден, 409 — дубликат."""
    from sqlalchemy import select
    from app.models import TaskList
    result = await db.execute(
        select(TaskList).where(
            TaskList.display_name == name,
            TaskList.deleted_at.is_(None),
        )
    )
    matches = result.scalars().all()
    if not matches:
        raise HTTPException(status_code=404, detail=f"List '{name}' not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"Multiple lists named '{name}' found ({len(matches)}). Use list_id instead.",
        )
    return matches[0]


@router.post("", response_model=TaskListResponse, status_code=201)
async def create_task_list(data: TaskListCreate, db: AsyncSession = Depends(get_db)):
    return await task_service.create_list(db, data.display_name)


@router.get("/{list_id}", response_model=TaskListResponse)
async def get_task_list(list_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    from app.models import TaskList
    result = await db.execute(
        select(TaskList).where(TaskList.id == list_id, TaskList.deleted_at.is_(None))
    )
    task_list = result.scalar_one_or_none()
    if not task_list:
        raise HTTPException(status_code=404, detail="List not found")
    return task_list


@router.patch("/{list_id}", response_model=TaskListResponse)
async def update_task_list(list_id: uuid.UUID, data: TaskListUpdate, db: AsyncSession = Depends(get_db)):
    task_list = await task_service.update_list(db, list_id, data.display_name)
    if not task_list:
        raise HTTPException(status_code=404, detail="List not found")
    return task_list


@router.delete("/{list_id}", status_code=204)
async def delete_task_list(list_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    deleted = await task_service.delete_list(db, list_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="List not found")


@router.post("/{list_id}/share", response_model=ShareListOut, status_code=200)
async def share_task_list(
    list_id: uuid.UUID,
    data: ShareListIn,
    db: AsyncSession = Depends(get_db),
):
    """F3.3: Invite a user to share a To Do list via Microsoft Graph API.

    Requires the list to exist locally and to have a corresponding ms_id.
    Errors: 404 if list not found; 403 if caller is not the list owner; 502 for other Graph errors.
    """
    from sqlalchemy import select
    from app.models import TaskList

    result = await db.execute(
        select(TaskList).where(TaskList.id == list_id, TaskList.deleted_at.is_(None))
    )
    task_list = result.scalar_one_or_none()
    if not task_list:
        raise HTTPException(status_code=404, detail="List not found")

    if not task_list.ms_id:
        raise HTTPException(
            status_code=400,
            detail="List has no Microsoft Graph ID — sync the list first before sharing.",
        )

    try:
        graph_resp = await graph_client.share_list(
            task_list.ms_id,
            email=str(data.email),
            permission=data.permission,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            raise HTTPException(status_code=404, detail="List not found in Microsoft Graph")
        if status == 403:
            raise HTTPException(
                status_code=403, detail="You are not the owner of this list and cannot share it"
            )
        raise HTTPException(
            status_code=502,
            detail=f"Microsoft Graph API error: {status} — {exc.response.text[:500]}",
        )

    return ShareListOut(
        invited_user_email=str(data.email),
        permission=data.permission,
        raw=graph_resp,
    )
