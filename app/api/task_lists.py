import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import TaskListCreate, TaskListResponse, TaskListUpdate
from app.services import task_service

router = APIRouter(prefix="/lists", tags=["task_lists"])


@router.get("", response_model=list[TaskListResponse])
async def list_task_lists(db: AsyncSession = Depends(get_db)):
    return await task_service.get_all_lists(db)


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
