from fastapi import APIRouter

from app.api import auth, stats, sync, task_lists, tasks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(task_lists.router)
api_router.include_router(tasks.router)
api_router.include_router(stats.router)
api_router.include_router(sync.router)
