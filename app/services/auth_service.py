import asyncio
import logging

import msal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import AuthToken

logger = logging.getLogger(__name__)

SCOPES = ["Tasks.ReadWrite"]


class AuthService:
    def __init__(self) -> None:
        self._cache = msal.SerializableTokenCache()
        self._lock = asyncio.Lock()
        self._app: msal.PublicClientApplication | None = None
        self._loaded = False

    def _get_app(self) -> msal.PublicClientApplication:
        if self._app is None:
            self._app = msal.PublicClientApplication(
                client_id=settings.ms_client_id,
                authority=f"https://login.microsoftonline.com/{settings.ms_tenant_id}",
                token_cache=self._cache,
            )
        return self._app

    async def _load_cache(self) -> None:
        if self._loaded:
            return
        async with async_session() as db:
            result = await db.execute(select(AuthToken).limit(1))
            row = result.scalar_one_or_none()
            if row:
                self._cache.deserialize(row.token_cache)
                logger.info("Token cache loaded from database")
        self._loaded = True

    async def _persist_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        data = self._cache.serialize()
        async with async_session() as db:
            result = await db.execute(select(AuthToken).limit(1))
            row = result.scalar_one_or_none()
            if row:
                row.token_cache = data
            else:
                db.add(AuthToken(token_cache=data))
            await db.commit()
        logger.info("Token cache persisted to database")

    async def get_access_token(self) -> str | None:
        async with self._lock:
            await self._load_cache()
            app = self._get_app()
            accounts = app.get_accounts()
            if not accounts:
                return None
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
            if result and "access_token" in result:
                await self._persist_cache()
                return result["access_token"]
            logger.warning("Token acquisition failed: %s", result.get("error_description", "unknown"))
            return None

    async def is_authenticated(self) -> bool:
        token = await self.get_access_token()
        return token is not None

    async def initiate_device_code_flow(self) -> dict:
        async with self._lock:
            await self._load_cache()
            app = self._get_app()
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(f"Device code flow failed: {flow.get('error_description', 'unknown')}")
            return flow

    async def complete_device_code_flow(self, flow: dict) -> dict:
        app = self._get_app()
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)
        if "access_token" in result:
            async with self._lock:
                await self._persist_cache()
            logger.info("Device code flow completed successfully")
            return {"authenticated": True}
        raise RuntimeError(f"Authentication failed: {result.get('error_description', 'unknown')}")


auth_service = AuthService()
