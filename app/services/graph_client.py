import json
import logging
from typing import Any

import httpx

from app.services.auth_service import auth_service

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.microsoft.com/v1.0/me/todo"
MAX_RETRIES = 3


def _try_parse_truncated_json(text: str) -> dict | None:
    """Try to parse JSON that may have trailing garbage after the valid object."""
    # Find the position of the last '}' which should close the root object
    for end_pos in range(len(text), 0, -1):
        if text[end_pos - 1] == "}":
            try:
                return json.loads(text[:end_pos])
            except json.JSONDecodeError:
                continue
    return None


class MSGraphToDoClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, read=120.0),
                limits=httpx.Limits(max_connections=10),
            )
        return self._client

    async def _headers(self) -> dict[str, str]:
        token = await auth_service.get_access_token()
        if not token:
            raise RuntimeError("Not authenticated. Call POST /api/v1/auth/device-code first.")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _request(
        self, method: str, url: str, json_body: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        client = await self._get_client()
        headers = await self._headers()

        for attempt in range(MAX_RETRIES):
            response = await client.request(method, url, headers=headers, json=json_body, params=params)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                logger.warning("Rate limited, retrying after %ds (attempt %d)", retry_after, attempt + 1)
                import asyncio
                await asyncio.sleep(retry_after)
                continue

            if response.status_code == 410:
                raise DeltaLinkExpiredError("Delta link expired")

            response.raise_for_status()
            if response.status_code == 204:
                return {}
            try:
                return response.json()
            except Exception as e:
                raw = response.content
                logger.warning(
                    "JSON parse failed (len=%d, attempt %d/%d): %s",
                    len(raw), attempt + 1, MAX_RETRIES, e,
                )
                # Graph API sometimes appends extra data after valid JSON.
                # Try to find the last valid closing brace and parse up to it.
                text = raw.decode("utf-8", errors="replace")
                result = _try_parse_truncated_json(text)
                if result is not None:
                    logger.info("Recovered truncated JSON (used %d of %d chars)", len(text), len(raw))
                    return result
                if attempt < MAX_RETRIES - 1:
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                raise

        raise RuntimeError("Max retries exceeded for Graph API request")

    # --- Task Lists ---

    async def get_lists(self) -> list[dict]:
        result = await self._request("GET", f"{BASE_URL}/lists")
        return result.get("value", [])

    async def create_list(self, display_name: str) -> dict:
        return await self._request("POST", f"{BASE_URL}/lists", json_body={"displayName": display_name})

    async def update_list(self, list_ms_id: str, display_name: str) -> dict:
        return await self._request("PATCH", f"{BASE_URL}/lists/{list_ms_id}", json_body={"displayName": display_name})

    async def delete_list(self, list_ms_id: str) -> None:
        await self._request("DELETE", f"{BASE_URL}/lists/{list_ms_id}")

    async def get_lists_delta(self, delta_link: str | None = None) -> dict:
        url = delta_link or f"{BASE_URL}/lists/delta"
        all_values = []
        result = {}
        while url:
            result = await self._request("GET", url)
            all_values.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
        return {
            "value": all_values,
            "delta_link": result.get("@odata.deltaLink"),
        }

    # --- Tasks ---

    async def get_tasks(self, list_ms_id: str) -> list[dict]:
        all_tasks = []
        url = f"{BASE_URL}/lists/{list_ms_id}/tasks"
        while url:
            result = await self._request("GET", url)
            all_tasks.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
        return all_tasks

    async def create_task(self, list_ms_id: str, task_data: dict) -> dict:
        return await self._request("POST", f"{BASE_URL}/lists/{list_ms_id}/tasks", json_body=task_data)

    async def update_task(self, list_ms_id: str, task_ms_id: str, task_data: dict) -> dict:
        return await self._request("PATCH", f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}", json_body=task_data)

    async def delete_task(self, list_ms_id: str, task_ms_id: str) -> None:
        await self._request("DELETE", f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}")

    async def get_tasks_delta(self, list_ms_id: str, delta_link: str | None = None) -> dict:
        url = delta_link or f"{BASE_URL}/lists/{list_ms_id}/tasks/delta"
        all_values = []
        result = {}
        while url:
            result = await self._request("GET", url)
            all_values.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
        return {
            "value": all_values,
            "delta_link": result.get("@odata.deltaLink"),
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class DeltaLinkExpiredError(Exception):
    pass


graph_client = MSGraphToDoClient()
