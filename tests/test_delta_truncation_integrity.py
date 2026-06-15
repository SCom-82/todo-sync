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
  C-SMOKE-5: Multi-page truncation (prod scenario КС-Финансы/Семья):
             page 1 valid with nextLink, page 2 truncated → sync_service.pull_tasks_for_list
             yields partial status, delta_link not advanced, errors≥1, prefix rescued.
             Mutation guard: removing result={} or break in truncation branch breaks this test.

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

        # Retry exhaustion: _request must have tried exactly MAX_RETRIES=3 times
        # (all attempts return the same truncated body, so 3 HTTP calls are made
        #  before raising — confirming retry logic runs fully before truncation-rescue).
        assert mock_req.call_count == 3, (
            f"Expected exactly 3 retries (MAX_RETRIES) on truncated body, got {mock_req.call_count}"
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


# ─────────────────────────────────────────────
# C-SMOKE-5: Multi-page truncation — exact prod scenario (ADR 0003 §C-7-bis)
#
# Mocks at HTTP boundary (httpx.AsyncClient.request).
# Calls real sync_service.pull_tasks_for_list (the SUT).
# Validates all four invariants required by ADR 0003 §C-7:
#   1. Tasks from page 1 are ingested (db.add called for each page-1 task)
#   2. Prefix tasks from truncated page 2 are rescued (rescued_items > 0 path exercised)
#   3. state.delta_link NOT advanced (remains as set before the call)
#   4. state.last_sync_status == "partial"
#   5. state.last_sync_errors incremented (>= 1)
#
# Mutation guard target:
#   - Remove `result = {}` (line ~339 graph_client.py) → delta_link would be set from
#     broken result dict → state.delta_link gets advanced → assertion (3) fails.
#   - Remove `break` in truncation branch → pagination loop would continue on a consumed
#     URL (no nextLink from broken page) → infinite loop or wrong result structure →
#     assertion (4) or (5) fails.
# ─────────────────────────────────────────────

NEXT_LINK_P1 = "https://graph.microsoft.com/v1.0/me/todo/lists/.../tasks/delta?$skiptoken=PAGE2"
PREV_DELTA_LINK = "https://graph.microsoft.com/v1.0/me/todo/lists/.../tasks/delta?$deltatoken=PREV"


class TestMultiPageTruncationProdScenario:
    """C-SMOKE-5: Multi-page abort — the exact КС-Финансы/Семья production pattern.

    Page 1: valid delta response with @odata.nextLink (3 tasks, no deltaLink yet).
    Page 2: truncated body (2-task prefix, then cut off — Graph InternalServerError pattern).

    pull_tasks_for_list is called on the real sync_service, with:
      - httpx.AsyncClient.request mocked at the HTTP boundary
      - _get_or_create_sync_state mocked to return a controllable state object
      - db mocked as AsyncMock (scalar_one_or_none → None so tasks are treated as new)
      - graph_client sub-calls (checklist, linked_resources, attachments) mocked to [] / {}
    """

    def _make_state(self, existing_delta_link: str | None = PREV_DELTA_LINK):
        """Build a plain namespace that behaves like SyncState for attribute reads/writes."""
        state = MagicMock()
        state.delta_link = existing_delta_link
        state.last_sync_status = "success"
        state.last_sync_errors = 0
        state.last_sync_at = None
        state.delta_syncs_total = 0
        state.delta_syncs_succeeded = 0
        state.delta_full_resets_total = 0
        return state

    def _make_db(self):
        """AsyncMock DB session: execute returns a result where scalar_one_or_none → None.

        None means 'task does not exist locally' so pull_tasks_for_list will create a new
        Task via db.add() for each ingested item — allowing us to verify ingestion by
        counting db.add calls.
        """
        db = AsyncMock()
        db.flush = AsyncMock()
        db.add = MagicMock()   # sync call in the real code (not awaited)
        db.delete = AsyncMock()

        # db.execute returns an object whose .scalar_one_or_none() returns None (new task)
        # and whose .scalars().all() returns [] (no existing attachments/linked_resources)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.mark.asyncio
    async def test_multi_page_truncation_on_page2(self):
        """C-SMOKE-5 (ADR 0003 §C-7-bis): page-1 valid + page-2 truncated.

        Verifies that pull_tasks_for_list:
          - ingests page-1 tasks (db.add called N times for page-1 items)
          - rescues page-2 prefix tasks (also ingested via db.add)
          - does NOT advance state.delta_link (remains PREV_DELTA_LINK)
          - sets state.last_sync_status = "partial"
          - increments state.last_sync_errors to >= 1

        Mutation guard: removing `result = {}` or `break` in graph_client.py truncation
        branch will break this test (delta_link would be advanced or status would be wrong).
        """
        from app.services import sync_service

        # Page 1: 3 tasks, valid, has nextLink (not yet final page)
        page1_tasks = [_task_json(i) for i in range(1, 4)]
        body_p1 = _full_delta_body(page1_tasks, next_link=NEXT_LINK_P1)

        # Page 2: 2-task prefix then truncation (no deltaLink visible)
        page2_prefix_tasks = [_task_json(i) for i in range(4, 6)]
        body_p2_truncated = _truncated_delta_body(page2_prefix_tasks)

        # task_list mock
        task_list = MagicMock()
        task_list.id = "local-list-uuid-001"
        task_list.ms_id = LIST_MS_ID

        state = self._make_state(existing_delta_link=PREV_DELTA_LINK)
        db = self._make_db()

        # HTTP call sequence: page 1 (1 call) → page 2 (3 retries with same truncated body)
        # Total expected = 4 calls. If `break` is mutated away, the loop continues forever
        # making repeated calls to page-2 URL. We cap at MAX_CALLS to make the test FAIL
        # (not hang) on that mutation.
        MAX_CALLS = 10
        http_call_count = 0

        async def http_side_effect(method, url, **kwargs):
            nonlocal http_call_count
            http_call_count += 1
            if http_call_count > MAX_CALLS:
                raise RuntimeError(
                    f"HTTP mock exceeded {MAX_CALLS} calls — likely infinite loop due to "
                    "missing `break` in truncation branch (mutation guard)"
                )
            if http_call_count == 1:
                return _make_response(body_p1)
            # Calls 2, 3, 4 (= 3 retries of page 2) all return the same truncated body
            return _make_response(body_p2_truncated)

        with patch("httpx.AsyncClient.request", side_effect=http_side_effect) as mock_req:
            with patch("app.services.graph_client.auth_service.get_access_token", new_callable=AsyncMock) as mock_auth:
                mock_auth.return_value = "fake-token"
                with patch.object(sync_service, "_get_or_create_sync_state", new_callable=AsyncMock) as mock_get_state:
                    mock_get_state.return_value = state
                    # Sub-calls from pull_tasks_for_list after delta fetch:
                    # get_checklist_items, list_linked_resources, list_attachments
                    with patch.object(sync_service.graph_client, "get_checklist_items", new_callable=AsyncMock) as mock_cl, \
                         patch.object(sync_service.graph_client, "list_linked_resources", new_callable=AsyncMock) as mock_lr, \
                         patch.object(sync_service.graph_client, "list_attachments", new_callable=AsyncMock) as mock_att:
                        mock_cl.return_value = []
                        mock_lr.return_value = []
                        mock_att.return_value = []

                        upserted, deleted = await sync_service.pull_tasks_for_list(db, task_list)

        # ── 1. Tasks from page 1 are ingested ──
        # All page-1 and rescued page-2 prefix tasks should be new (existing=None),
        # so pull_tasks_for_list calls db.add() once per ingested task.
        total_ingested = len(page1_tasks) + len(page2_prefix_tasks)
        assert db.add.call_count == total_ingested, (
            f"Expected db.add called {total_ingested} times (page1={len(page1_tasks)} + "
            f"rescued={len(page2_prefix_tasks)}), got {db.add.call_count}"
        )
        assert upserted == total_ingested, (
            f"upserted counter must equal {total_ingested}, got {upserted}"
        )

        # ── 2. Prefix tasks from page 2 are rescued ──
        # Covered by db.add.call_count check above (prefix tasks are in the ingested set).
        # Additionally verify that the rescue path was exercised via HTTP call count:
        # 1 (page 1) + 3 (page 2 retries, MAX_RETRIES=3) = 4 total HTTP calls
        assert mock_req.call_count == 4, (
            f"Expected 4 HTTP calls (1 page-1 + 3 retries page-2), got {mock_req.call_count}"
        )

        # ── 3. state.delta_link NOT advanced ──
        # Must remain as it was before the call (PREV_DELTA_LINK), not updated to any new token.
        assert state.delta_link == PREV_DELTA_LINK, (
            f"delta_link must NOT be advanced on truncated round; "
            f"expected {PREV_DELTA_LINK!r}, got {state.delta_link!r}"
        )

        # ── 4. state.last_sync_status == "partial" ──
        assert state.last_sync_status == "partial", (
            f"last_sync_status must be 'partial' after truncated round, got {state.last_sync_status!r}"
        )

        # ── 5. state.last_sync_errors incremented ──
        assert state.last_sync_errors >= 1, (
            f"last_sync_errors must be >= 1 after truncated round, got {state.last_sync_errors}"
        )
