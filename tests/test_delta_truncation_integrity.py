"""
ADR 0003 §C-7 smoke tests: delta truncation integrity.

These tests mock at the HTTP boundary (httpx.AsyncClient.request), NOT at higher-level
graph_client methods. This ensures the full request→parse→rescue→return pipeline is
exercised and no truncation-hiding at the method level is possible.

Covered scenarios:
  C-SMOKE-1: Truncated body → delta_link NOT advanced, errors≥1, partial status, prefix rescued
  C-SMOKE-2: Clean round → delta_link advanced, success status, no errors
  C-SMOKE-3: _extract_prefix_items correctly extracts valid prefix from truncated body
  C-SMOKE-4: _extract_prefix_items returns [] on empty / totally broken body

Run: pytest tests/test_delta_truncation_integrity.py -v
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.graph_client import MSGraphToDoClient, _extract_prefix_items


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

LIST_MS_ID = "list-ms-test-001"
DELTA_TOKEN = "https://graph.microsoft.com/v1.0/me/todo/lists/.../tasks/delta?$deltatoken=FINAL"


def _make_response(body: bytes, status_code: int = 200) -> httpx.Response:
    """Build a minimal httpx.Response with the given body."""
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/todo/lists/x/tasks/delta"),
    )


def _task_json(n: int) -> dict:
    return {
        "id": f"task-ms-{n:04d}",
        "title": f"Task {n}",
        "status": "notStarted",
        "importance": "normal",
        "createdDateTime": "2024-01-01T00:00:00Z",
        "lastModifiedDateTime": "2024-01-01T00:00:00Z",
    }


def _full_delta_body(tasks: list[dict], next_link: str | None = None, delta_link: str | None = None) -> bytes:
    """Build a complete, valid delta-page body."""
    obj: dict = {"value": tasks}
    if next_link:
        obj["@odata.nextLink"] = next_link
    if delta_link:
        obj["@odata.deltaLink"] = delta_link
    return json.dumps(obj).encode()


def _truncated_delta_body(tasks_prefix: list[dict]) -> bytes:
    """Build a body that starts like a delta page but is truncated mid-value-array.

    Simulates the Graph InternalServerError pattern: some tasks serialize correctly,
    then the body is cut off. No nextLink/deltaLink in the truncated portion.
    """
    # Serialize the valid prefix as a partial JSON body (no closing brackets)
    value_str = json.dumps(tasks_prefix)
    # Remove the closing ']' to simulate truncation mid-array
    truncated_value = value_str[:-1]  # drops the final ']'
    # Build body: {"value": [task1, task2, ...  (TRUNCATED — no ], no nextLink, no })
    body = f'{{"value": {truncated_value}'.encode()
    return body


# ─────────────────────────────────────────────
# C-SMOKE-1: Truncated body does NOT advance delta_link
# ─────────────────────────────────────────────

class TestDeltaTruncationIntegrity:
    @pytest.mark.asyncio
    async def test_truncated_page_does_not_advance_delta_link(self):
        """C-SMOKE-1 (ADR 0003 §C-7):

        When a delta page returns a truncated JSON body (Graph InternalServerError pattern):
        - delta_link in result MUST be None (not advanced)
        - truncated MUST be True
        - truncated_pages MUST be >= 1
        - rescued_items MUST match the number of tasks in the valid prefix
        - Items in the prefix MUST be present in result["value"]
        """
        # Build a truncated body with 3 valid prefix tasks (then cut off)
        prefix_tasks = [_task_json(i) for i in range(1, 4)]
        truncated_body = _truncated_delta_body(prefix_tasks)

        client = MSGraphToDoClient()

        with patch("httpx.AsyncClient.request") as mock_req:
            mock_resp = _make_response(truncated_body)
            mock_req.return_value = mock_resp

            # Patch auth so we don't need a real token
            with patch("app.services.graph_client.auth_service.get_access_token", new_callable=AsyncMock) as mock_auth:
                mock_auth.return_value = "fake-token"
                result = await client.get_tasks_delta(LIST_MS_ID, delta_link=None)

        # Core assertion: delta cursor NOT advanced
        assert result["delta_link"] is None, (
            f"delta_link must be None on truncated round, got {result['delta_link']!r}"
        )
        # Round marked as partial
        assert result["truncated"] is True, "truncated flag must be True"
        assert result["truncated_pages"] >= 1, "truncated_pages must be >= 1"

        # Prefix items rescued
        assert result["rescued_items"] == len(prefix_tasks), (
            f"Expected {len(prefix_tasks)} rescued items, got {result['rescued_items']}"
        )
        rescued_ids = {t["id"] for t in result["value"]}
        for t in prefix_tasks:
            assert t["id"] in rescued_ids, (
                f"Prefix task {t['id']} should be in value, got {rescued_ids!r}"
            )

    @pytest.mark.asyncio
    async def test_clean_round_advances_delta_link(self):
        """C-SMOKE-2: Clean single-page round advances delta_link and reports success."""
        tasks = [_task_json(i) for i in range(1, 6)]
        body = _full_delta_body(tasks, delta_link=DELTA_TOKEN)

        client = MSGraphToDoClient()

        with patch("httpx.AsyncClient.request") as mock_req:
            mock_req.return_value = _make_response(body)
            with patch("app.services.graph_client.auth_service.get_access_token", new_callable=AsyncMock) as mock_auth:
                mock_auth.return_value = "fake-token"
                result = await client.get_tasks_delta(LIST_MS_ID)

        assert result["delta_link"] == DELTA_TOKEN, (
            f"Clean round must advance delta_link to the delta token, got {result['delta_link']!r}"
        )
        assert result["truncated"] is False
        assert result["truncated_pages"] == 0
        assert result["rescued_items"] == 0
        assert len(result["value"]) == len(tasks)

    @pytest.mark.asyncio
    async def test_clean_multi_page_round_advances_delta_link(self):
        """C-SMOKE-2b: Multi-page round: first page has nextLink, second has deltaLink."""
        tasks_p1 = [_task_json(i) for i in range(1, 4)]
        tasks_p2 = [_task_json(i) for i in range(4, 7)]

        NEXT_LINK = "https://graph.microsoft.com/v1.0/me/todo/lists/.../tasks/delta?$skiptoken=SKIP"
        body_p1 = _full_delta_body(tasks_p1, next_link=NEXT_LINK)
        body_p2 = _full_delta_body(tasks_p2, delta_link=DELTA_TOKEN)

        client = MSGraphToDoClient()

        call_count = 0

        async def mock_request_side_effect(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(body_p1)
            return _make_response(body_p2)

        with patch("httpx.AsyncClient.request", side_effect=mock_request_side_effect):
            with patch("app.services.graph_client.auth_service.get_access_token", new_callable=AsyncMock) as mock_auth:
                mock_auth.return_value = "fake-token"
                result = await client.get_tasks_delta(LIST_MS_ID)

        assert result["delta_link"] == DELTA_TOKEN
        assert result["truncated"] is False
        assert len(result["value"]) == len(tasks_p1) + len(tasks_p2)


# ─────────────────────────────────────────────
# C-SMOKE-3/4: _extract_prefix_items unit tests
# ─────────────────────────────────────────────

class TestExtractPrefixItems:
    def test_extracts_valid_prefix_from_truncated_body(self):
        """C-SMOKE-3: _extract_prefix_items returns items before truncation point."""
        tasks = [_task_json(i) for i in range(1, 4)]
        truncated = _truncated_delta_body(tasks).decode()
        result = _extract_prefix_items(truncated)
        assert len(result) == len(tasks), f"Expected {len(tasks)} items, got {len(result)}"
        for i, task in enumerate(tasks):
            assert result[i]["id"] == task["id"]

    def test_returns_empty_on_totally_broken_body(self):
        """C-SMOKE-4: _extract_prefix_items returns [] on completely unparseable body."""
        assert _extract_prefix_items("") == []
        assert _extract_prefix_items("not json at all {{{{") == []
        assert _extract_prefix_items('{"no_value": true}') == []

    def test_returns_empty_on_no_value_key(self):
        """_extract_prefix_items returns [] when 'value' key is absent."""
        body = json.dumps({"@odata.context": "...", "other": []})
        assert _extract_prefix_items(body) == []

    def test_returns_all_items_on_complete_body(self):
        """_extract_prefix_items also works on a fully valid body (no-op safe)."""
        tasks = [_task_json(i) for i in range(1, 6)]
        full = json.dumps({"value": tasks, "@odata.deltaLink": DELTA_TOKEN})
        result = _extract_prefix_items(full)
        assert len(result) == len(tasks)
