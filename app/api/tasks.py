import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import TaskCreate, TaskResponse, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    list_id: uuid.UUID | None = None,
    status: str | None = None,
    importance: str | None = None,
    overdue: bool = False,
    due_before: date | None = None,
    due_after: date | None = None,
    search: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    return await task_service.get_tasks(
        db,
        list_id=list_id,
        status=status,
        importance=importance,
        overdue=overdue,
        due_before=due_before,
        due_after=due_after,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(data: TaskCreate, db: AsyncSession = Depends(get_db)):
    try:
        return await task_service.create_task(db, data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(task_id: uuid.UUID, data: TaskUpdate, db: AsyncSession = Depends(get_db)):
    task = await task_service.update_task(db, task_id, data)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    deleted = await task_service.delete_task(db, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")


@router.post("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await task_service.complete_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/{task_id}/uncomplete", response_model=TaskResponse)
async def uncomplete_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await task_service.uncomplete_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
