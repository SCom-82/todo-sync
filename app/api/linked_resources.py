"""F2.1: REST CRUD for linked_resources."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import LinkedResource, Task
from app.schemas import LinkedResourceIn, LinkedResourceOut, LinkedResourceUpdate
from app.services import linked_resource_service

router = APIRouter(tags=["linked_resources"])


@router.post("/tasks/{task_id}/linked-resources", response_model=LinkedResourceOut, status_code=201)
async def create_linked_resource(
    task_id: uuid.UUID,
    data: LinkedResourceIn,
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task or task.deleted_at:
        raise HTTPException(status_code=404, detail="Task not found")
    lr = await linked_resource_service.create(db, task_id, data)
    return lr


@router.get("/tasks/{task_id}/linked-resources", response_model=list[LinkedResourceOut])
async def list_linked_resources(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task or task.deleted_at:
        raise HTTPException(status_code=404, detail="Task not found")
    return await linked_resource_service.list_for_task(db, task_id)


@router.get("/linked-resources/{lr_id}", response_model=LinkedResourceOut)
async def get_linked_resource(
    lr_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    lr = await db.get(LinkedResource, lr_id)
    if not lr:
        raise HTTPException(status_code=404, detail="LinkedResource not found")
    return lr


@router.patch("/linked-resources/{lr_id}", response_model=LinkedResourceOut)
async def update_linked_resource(
    lr_id: uuid.UUID,
    data: LinkedResourceUpdate,
    db: AsyncSession = Depends(get_db),
):
    lr = await linked_resource_service.update(db, lr_id, data)
    if not lr:
        raise HTTPException(status_code=404, detail="LinkedResource not found")
    return lr


@router.delete("/linked-resources/{lr_id}", status_code=204)
async def delete_linked_resource(
    lr_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    deleted = await linked_resource_service.delete(db, lr_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="LinkedResource not found")
