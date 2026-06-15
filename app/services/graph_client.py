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


def _extract_prefix_items(text: str) -> list:
    """Extract the valid prefix of the 'value' array from a truncated Graph delta response.

    Graph delta pages have the shape: {"value": [{...}, {...}, ...], "@odata.nextLink": "..."}
    When Graph truncates the body mid-serialization (server-side serialization bug), the
    'value' array is left open. This function extracts all fully-serialized items before
    the truncation point.

    Returns a (possibly empty) list of dicts — the items that successfully serialized.
    Does NOT return None on failure; returns [] instead, so callers can ingest the prefix
    and still mark the round as partial.
    """
    # Locate the start of the "value" array
    value_key = '"value"'
    key_pos = text.find(value_key)
    if key_pos == -1:
        return []
    # Find the opening '[' of the array
    array_start = text.find("[", key_pos + len(value_key))
    if array_start == -1:
        return []

    items = []
    pos = array_start + 1
    decoder = json.JSONDecoder()

    # Skip whitespace and commas, then try to decode individual JSON objects
    while pos < len(text):
        # Skip whitespace and commas between items
        while pos < len(text) and text[pos] in " \t\n\r,":
            pos += 1
        if pos >= len(text):
            break
        # End of array (clean close)
        if text[pos] == "]":
            break
        # Try to parse next object
        try:
            obj, end_idx = decoder.raw_decode(text, pos)
            if isinstance(obj, dict):
                items.append(obj)
            pos = end_idx
        except json.JSONDecodeError:
            # Can't parse further — truncation point reached
            break

    return items


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
        """Fetch all delta pages for a task list.

        Returns a dict with keys:
          - "value": list of task dicts ingested so far
          - "delta_link": finalised delta token (None if round is partial/failed)
          - "truncated": True if at least one page failed to parse (partial round)
          - "truncated_pages": count of pages that could not be parsed
          - "rescued_items": count of items extracted from truncated pages via prefix-parse

        Defensive invariant (ADR 0003 §C-7):
          - If any page fails JSON parsing, the round is marked as partial (truncated=True).
          - delta_link is set to None so callers do NOT advance the delta cursor.
          - Items successfully serialized before the truncation point are rescued via
            _extract_prefix_items and included in "value".
          - prev_next_link (the nextLink from the last successful page) is stored so
            diagnostics can identify where the round broke.
          - "Partial result == success" is explicitly forbidden: callers MUST check
            "truncated" and set last_sync_status accordingly.
        """
        url = delta_link or f"{BASE_URL}/lists/{list_ms_id}/tasks/delta"
        all_values: list = []
        result: dict = {}
        truncated_pages = 0
        rescued_items = 0
        # Stores the nextLink from the last successfully parsed page.
        # On truncation, this is the URL we would need to resume from (hard-stop case).
        # TODO: resumption from prev_next_link is not yet implemented; stored for diagnostics only.
        prev_next_link: str | None = None

        while url:
            try:
                result = await self._request("GET", url)
                all_values.extend(result.get("value", []))
                next_link = result.get("@odata.nextLink")
                if next_link:
                    prev_next_link = url  # the page we just successfully fetched
                url = next_link
            except Exception as e:
                is_json_error = (
                    isinstance(e, json.JSONDecodeError)
                    or "JSONDecodeError" in type(e).__name__
                    or "JSON" in str(e)
                )
                if not is_json_error:
                    raise

                # --- Truncated page handling (ADR 0003 §C-7 revised) ---
                # Graph emitted an InternalServerError inside a 200 OK body for a task
                # with a corrupted linkedResources navigation property, then closed the
                # HTTP stream without completing the JSON. We cannot get the nextLink from
                # this body (Fact 2: hard-stop confirmed by battle-test §C-7-bis).

                # Attempt partial-parse: salvage items that Graph serialized before the error.
                raw_text = getattr(e, "doc", None)
                if raw_text is None:
                    # JSONDecodeError stores the partial doc in .doc; fall back to re-reading
                    # the response body from the exception args if available.
                    try:
                        raw_text = e.args[0] if e.args else ""
                    except Exception:
                        raw_text = ""

                # _request already decoded bytes→str for us; re-decode if we got bytes
                if isinstance(raw_text, (bytes, bytearray)):
                    raw_text = raw_text.decode("utf-8", errors="replace")

                rescued = _extract_prefix_items(raw_text or "")
                if rescued:
                    logger.error(
                        "Delta page truncated for list %s — rescued %d prefix items from broken page "
                        "(Graph InternalServerError in body, ADR 0003 §C-7). "
                        "prev_next_link=%s",
                        list_ms_id, len(rescued), prev_next_link,
                    )
                    all_values.extend(rescued)
                    rescued_items += len(rescued)
                else:
                    logger.error(
                        "Delta page truncated for list %s — could not rescue any items from broken page "
                        "(Graph InternalServerError in body, ADR 0003 §C-7). "
                        "prev_next_link=%s",
                        list_ms_id, prev_next_link,
                    )

                truncated_pages += 1
                # Hard-stop: cannot continue pagination, nextLink is past the truncation point.
                # delta_link will be None (not advanced) — caller must NOT mark round as success.
                result = {}  # no delta_link available from this broken response
                break

        return {
            "value": all_values,
            "delta_link": result.get("@odata.deltaLink") if result and not truncated_pages else None,
            "truncated": truncated_pages > 0,
            "truncated_pages": truncated_pages,
            "rescued_items": rescued_items,
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
