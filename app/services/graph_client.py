import json
import logging
from typing import Any

import httpx

from app.services.auth_service import auth_service

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.microsoft.com/v1.0/me/todo"
MAX_RETRIES = 3


def _try_parse_truncated_json(text: str) -> dict | None:
    """Try to parse JSON that may have trailing garbage or be truncated.

    Strategy: use json.raw_decode which stops at the end of the first
    valid JSON object, ignoring trailing garbage.
    """
    decoder = json.JSONDecoder()
    try:
        result, end_idx = decoder.raw_decode(text)
        if isinstance(result, dict):
            logger.info("raw_decode recovered JSON: used %d of %d chars", end_idx, len(text))
            return result
    except json.JSONDecodeError:
        pass
    # Fallback: try to find the closing of the @odata response pattern
    # The response should be {"@odata...": "...", "value": [...]}
    # Find the last "]}" which closes the value array and root object
    idx = text.rfind("]}")
    if idx > 0:
        candidate = text[:idx + 2]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
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
    async def get_tasks_with_expand(self, list_ms_id: str) -> list[dict]:
        """F2.6: Non-delta full pull with $expand=checklistItems,linkedResources.

        Reduces N+1 by including checklistItems and linkedResources inline.
        Attachments are NOT supported via $expand in Graph API.
        """
        all_tasks = []
        params = {"$expand": "checklistItems,linkedResources"}
        url = f"{BASE_URL}/lists/{list_ms_id}/tasks"
        while url:
            result = await self._request("GET", url, params=params if not all_tasks else None)
            all_tasks.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
            params = None
        return all_tasks


    async def create_task(self, list_ms_id: str, task_data: dict) -> dict:
        return await self._request("POST", f"{BASE_URL}/lists/{list_ms_id}/tasks", json_body=task_data)

    async def update_task(self, list_ms_id: str, task_ms_id: str, task_data: dict) -> dict:
        return await self._request("PATCH", f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}", json_body=task_data)

    async def delete_task(self, list_ms_id: str, task_ms_id: str) -> None:
        await self._request("DELETE", f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}")

    # --- Checklist items ---

    async def get_checklist_items(self, list_ms_id: str, task_ms_id: str) -> list[dict]:
        url = f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/checklistItems"
        all_items: list[dict] = []
        while url:
            result = await self._request("GET", url)
            all_items.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
        return all_items

    async def create_checklist_item(self, list_ms_id: str, task_ms_id: str, data: dict) -> dict:
        return await self._request(
            "POST",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/checklistItems",
            json_body=data,
        )

    async def update_checklist_item(
        self, list_ms_id: str, task_ms_id: str, item_id: str, data: dict
    ) -> dict:
        return await self._request(
            "PATCH",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/checklistItems/{item_id}",
            json_body=data,
        )

    async def delete_checklist_item(self, list_ms_id: str, task_ms_id: str, item_id: str) -> None:
        await self._request(
            "DELETE",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/checklistItems/{item_id}",
        )

    async def get_tasks_delta(self, list_ms_id: str, delta_link: str | None = None) -> dict:
        url = delta_link or f"{BASE_URL}/lists/{list_ms_id}/tasks/delta"
        all_values = []
        result = {}
        skipped_pages = 0
        while url:
            try:
                result = await self._request("GET", url)
                all_values.extend(result.get("value", []))
                url = result.get("@odata.nextLink")
            except (json.JSONDecodeError, Exception) as e:
                if "JSONDecodeError" in type(e).__name__ or "JSON" in str(e):
                    logger.warning("Skipping unparseable delta page for list %s: %s", list_ms_id, e)
                    skipped_pages += 1
                    # Can't get nextLink from broken response, stop pagination
                    break
                raise
        if skipped_pages:
            logger.warning("Delta sync for list %s: skipped %d pages, got %d items", list_ms_id, skipped_pages, len(all_values))
        return {
            "value": all_values,
            "delta_link": result.get("@odata.deltaLink") if result else None,
        }


    # --- F2.5: LinkedResources ---

    async def list_linked_resources(self, list_ms_id: str, task_ms_id: str) -> list[dict]:
        url = f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/linkedResources"
        all_items: list[dict] = []
        while url:
            result = await self._request("GET", url)
            all_items.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
        return all_items

    async def create_linked_resource(self, list_ms_id: str, task_ms_id: str, data: dict) -> dict:
        return await self._request(
            "POST",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/linkedResources",
            json_body=data,
        )

    async def update_linked_resource(
        self, list_ms_id: str, task_ms_id: str, lr_ms_id: str, data: dict
    ) -> dict:
        return await self._request(
            "PATCH",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/linkedResources/{lr_ms_id}",
            json_body=data,
        )

    async def delete_linked_resource(self, list_ms_id: str, task_ms_id: str, lr_ms_id: str) -> None:
        await self._request(
            "DELETE",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/linkedResources/{lr_ms_id}",
        )

    # --- F2.5: Attachments ---

    async def list_attachments(self, list_ms_id: str, task_ms_id: str) -> list[dict]:
        """List attachments for a task. Note: $expand not supported for attachments in Graph."""
        url = f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/attachments"
        all_items: list[dict] = []
        while url:
            result = await self._request("GET", url)
            all_items.extend(result.get("value", []))
            url = result.get("@odata.nextLink")
        return all_items

    async def create_attachment(self, list_ms_id: str, task_ms_id: str, data: dict) -> dict:
        """Create a small attachment (<=3 MB). data must include contentBytes as base64 string."""
        return await self._request(
            "POST",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/attachments",
            json_body=data,
        )

    async def delete_attachment(self, list_ms_id: str, task_ms_id: str, att_ms_id: str) -> None:
        await self._request(
            "DELETE",
            f"{BASE_URL}/lists/{list_ms_id}/tasks/{task_ms_id}/attachments/{att_ms_id}",
        )


    # --- F3.3: Share list ---

    async def share_list(self, list_ms_id: str, email: str, permission: str) -> dict:
        """Invite a user to share a To Do list.

        POST /me/todo/lists/{id}/members
        See: https://learn.microsoft.com/en-us/graph/api/todotasklist-post-members

        Args:
            list_ms_id: Microsoft ID of the list.
            email: Email address of the user to invite.
            permission: "read" or "readwrite".

        Returns:
            Invitation info dict from Graph API.

        Raises:
            httpx.HTTPStatusError: with status_code 404 (list not found), 403 (not owner),
                or other Graph errors.
        """
        payload = {
            "displayName": email,
            "sharedWithUserPermission": permission,
        }
        return await self._request(
            "POST",
            f"{BASE_URL}/lists/{list_ms_id}/members",
            json_body=payload,
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class DeltaLinkExpiredError(Exception):
    pass




graph_client = MSGraphToDoClient()
