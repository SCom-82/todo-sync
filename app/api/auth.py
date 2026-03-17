import asyncio
import logging

from fastapi import APIRouter, HTTPException

from app.schemas import AuthStatusResponse, DeviceCodeResponse
from app.services.auth_service import auth_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

_pending_flow: dict | None = None


@router.post("/device-code", response_model=DeviceCodeResponse)
async def initiate_device_code():
    global _pending_flow
    try:
        flow = await auth_service.initiate_device_code_flow()
        _pending_flow = flow
        # Start background polling for completion
        asyncio.create_task(_poll_device_code(flow))
        return DeviceCodeResponse(
            user_code=flow["user_code"],
            verification_uri=flow["verification_uri"],
            expires_in=flow.get("expires_in", 900),
            message=flow.get("message", f"Go to {flow['verification_uri']} and enter code {flow['user_code']}"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _poll_device_code(flow: dict) -> None:
    try:
        await auth_service.complete_device_code_flow(flow)
        logger.info("Device code flow completed - user authenticated")
    except Exception:
        logger.exception("Device code flow failed")


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status():
    authenticated = await auth_service.is_authenticated()
    return AuthStatusResponse(authenticated=authenticated)
