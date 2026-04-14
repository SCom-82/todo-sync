from fastapi import APIRouter

from app.api import auth, stats, sync, task_lists, tasks, linked_resources, attachments

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(task_lists.router)
api_router.include_router(tasks.router)
api_router.include_router(linked_resources.router)
api_router.include_router(attachments.router)
api_router.include_router(stats.router)
api_router.include_router(sync.router)
