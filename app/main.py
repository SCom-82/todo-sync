import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import api_router
from app.config import settings
from app.scheduler import start_scheduler, stop_scheduler
from app.services.graph_client import graph_client

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()
    await graph_client.close()


app = FastAPI(
    title="Todo Sync Service",
    description="Bidirectional sync: Microsoft To Do ↔ PostgreSQL",
    version="0.1.0",
    lifespan=lifespan,
    root_path_in_servers=False,
)


@app.middleware("http")
async def force_https_scheme(request, call_next):
    """Trust X-Forwarded-Proto from Traefik to fix Swagger UI mixed content."""
    if request.headers.get("x-forwarded-proto") == "https":
        request.scope["scheme"] = "https"
    return await call_next(request)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/api/v1/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/v1/readyz")
async def readyz():
    from app.database import engine
    from app.services.auth_service import auth_service
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    auth_ok = await auth_service.is_authenticated()

    status = "ready" if db_ok else "not_ready"
    return {"status": status, "database": db_ok, "authenticated": auth_ok}


from sqlalchemy import text
