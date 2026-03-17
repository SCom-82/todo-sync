from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import StatsResponse, TaskResponse
from app.services import task_service

router = APIRouter(tags=["stats"])


@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)):
    return await task_service.get_stats(db)


@router.get("/reminders/upcoming", response_model=list[TaskResponse])
async def get_upcoming_reminders(hours: int = Query(24, ge=1, le=168), db: AsyncSession = Depends(get_db)):
    return await task_service.get_upcoming_reminders(db, hours)


@router.get("/reminders/overdue", response_model=list[TaskResponse])
async def get_overdue_tasks(db: AsyncSession = Depends(get_db)):
    return await task_service.get_overdue_tasks(db)
