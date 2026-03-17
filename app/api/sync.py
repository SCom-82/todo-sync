from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import SyncLog, SyncState
from app.schemas import SyncLogEntry, SyncStatusResponse
from app.services import sync_service

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/trigger")
async def trigger_sync():
    result = await sync_service.run_sync(sync_type="manual")
    return result


@router.post("/reset")
async def reset_sync(db: AsyncSession = Depends(get_db)):
    """Reset all delta links to force a full resync."""
    from sqlalchemy import update
    await db.execute(update(SyncState).values(delta_link=None))
    await db.commit()
    return {"message": "Delta links reset. Next sync will do a full pull."}


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SyncState).order_by(SyncState.last_sync_at.desc()))
    states = result.scalars().all()
    last_sync_at = None
    last_status = None
    resources = []
    for s in states:
        resources.append({
            "resource_type": s.resource_type,
            "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
            "last_sync_status": s.last_sync_status,
            "last_error": s.last_error,
        })
        if s.last_sync_at and (last_sync_at is None or s.last_sync_at > last_sync_at):
            last_sync_at = s.last_sync_at
            last_status = s.last_sync_status
    return SyncStatusResponse(
        last_sync_at=last_sync_at,
        last_sync_status=last_status,
        resources=resources,
    )


@router.get("/log", response_model=list[SyncLogEntry])
async def get_sync_log(limit: int = Query(20, le=100), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SyncLog).order_by(SyncLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
