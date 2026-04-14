import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import (
    ChecklistItemCreate,
    ChecklistItemResponse,
    ChecklistItemUpdate,
    TaskCreate,
    TaskResponse,
    TaskUpdate,
)
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    list_id: uuid.UUID | None = None,
    list_name: str | None = None,   # F1.1: resolve by name
    filter: str | None = None,
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
        list_name=list_name,
        filter=filter,
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


# --- F1.5: Checklist point-edit endpoints ---

@router.get("/{task_id}/checklist", response_model=list[ChecklistItemResponse])
async def list_checklist_items(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Получить все пункты чек-листа задачи."""
    task = await task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    items = task.checklist_items or []
    # Return only items that have an id (synced with Graph)
    return [
        ChecklistItemResponse(
            id=it.get("id", ""),
            displayName=it.get("displayName", ""),
            isChecked=bool(it.get("isChecked", False)),
        )
        for it in items
        if it.get("id")
    ]


@router.post("/{task_id}/checklist", response_model=ChecklistItemResponse, status_code=201)
async def add_checklist_item(
    task_id: uuid.UUID,
    data: ChecklistItemCreate,
    db: AsyncSession = Depends(get_db),
):
    """Добавить один пункт чек-листа."""
    task = await task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    result = await task_service.add_checklist_item(
        db, task_id, data.displayName, data.isChecked
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return ChecklistItemResponse(
        id=result.get("id", ""),
        displayName=result.get("displayName", ""),
        isChecked=bool(result.get("isChecked", False)),
    )


@router.patch("/{task_id}/checklist/{item_id}", response_model=ChecklistItemResponse)
async def update_checklist_item(
    task_id: uuid.UUID,
    item_id: str,
    data: ChecklistItemUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Обновить один пункт чек-листа (toggle/rename)."""
    result = await task_service.update_checklist_item(
        db, task_id, item_id,
        display_name=data.displayName,
        is_checked=data.isChecked,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Task or checklist item not found")
    return ChecklistItemResponse(
        id=result.get("id", ""),
        displayName=result.get("displayName", ""),
        isChecked=bool(result.get("isChecked", False)),
    )


@router.delete("/{task_id}/checklist/{item_id}", status_code=204)
async def remove_checklist_item(
    task_id: uuid.UUID,
    item_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Удалить один пункт чек-листа."""
    deleted = await task_service.remove_checklist_item(db, task_id, item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task or checklist item not found")
