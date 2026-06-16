"""
ADR 0003 §B-4 smoke tests: attach_url → linkedResource push correctness.

Mocks are placed at the HTTP boundary (httpx.AsyncClient.request), NOT at higher-level
service/client methods. This follows the project lesson from S118 / ADR 0001:
mocking too high hides real service-layer bugs.

Scenarios covered:
  B-T1: attach_url happy path → POST to Graph /linkedResources (201, GUID) →
         sync_status=synced, ms_id=GUID.
  B-T2: attach_url + Graph error → sync_status=pending, NOT lying synced.
  B-T3: create_reference (legacy local-only function) does NOT set synced after fix.
         It must remain pending (honest: no Graph push happens).

Run: pytest tests/test_attach_url_smoke.py -v
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models import LinkedResource, Task, TaskList
from app.services.graph_client import graph_client

# ─────────────────────────────────────────────
# Shared constants / helpers
# ─────────────────────────────────────────────

GUID_LR_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"  # Graph-format GUID for linkedResource
LIST_MS_ID = "list-ms-smoke-001"
TASK_MS_ID = "task-ms-smoke-001"
TEST_URL = "https://github.com/org/repo/pull/99"
TEST_NAME = "PR #99"


def _make_httpx_response(body: dict | bytes, status_code: int = 200) -> httpx.Response:
    """Build a minimal httpx.Response."""
    if isinstance(body, dict):
        content = json.dumps(body).encode()
    else:
        content = body
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request(
            "POST",
            f"https://graph.microsoft.com/v1.0/me/todo/lists/{LIST_MS_ID}/tasks/{TASK_MS_ID}/linkedResources",
        ),
    )


def _make_db_with_task(task_ms_id=TASK_MS_ID, list_ms_id=LIST_MS_ID, task_id=None):
    """Build a minimal AsyncMock db session returning one Task + one TaskList.

    db.execute is called in linked_resource_service.create() twice:
      1st call → Task query
      2nd call → TaskList query
    """
    task = MagicMock(spec=Task)
    task.id = task_id or uuid.uuid4()
    task.ms_id = task_ms_id
    task.list_id = uuid.uuid4()

    task_list = MagicMock(spec=TaskList)
    task_list.id = task.list_id
    task_list.ms_id = list_ms_id

    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    added_objects = []
    db.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))
    db._added = added_objects

    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task

    list_result = MagicMock()
    list_result.scalar_one_or_none.return_value = task_list

    db.execute = AsyncMock(side_effect=[task_result, list_result])
    return db, task, task_list


# ─────────────────────────────────────────────
# B-T1: Happy path — Graph 201 with GUID → synced
# ─────────────────────────────────────────────

class TestAttachUrlHappyPath:
    @pytest.mark.asyncio
    async def test_bt1_attach_url_posts_linked_resource_and_sets_synced(self):
        """B-T1: attach_url → linked_resource_service.create() → POST to Graph /linkedResources
        → Graph returns 201 with GUID → sync_status=synced, ms_id=GUID.

        Validates:
        - A real POST is made to Graph (not mocked away at service level)
        - The URL (webUrl) and name (displayName) are forwarded
        - sync_status becomes 'synced' (not 'pending') after successful push
        - ms_id is set to the GUID returned by Graph
        - No fake synced without Graph confirmation (ADR 0003 §B-4 invariant)
        """
        from app.services import linked_resource_service
        from app.schemas import LinkedResourceIn

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)

        # Build the schema that attach_url endpoint creates before calling the service
        data = LinkedResourceIn(
            web_url=TEST_URL,
            display_name=TEST_NAME,
            application_name="todo-sync",
        )

        captured_request = {}

        def capture_httpx_request(method, url, headers=None, json=None, params=None, **kwargs):
            captured_request["method"] = method
            captured_request["url"] = url
            captured_request["json"] = json
            # Return Graph 201 with GUID id
            graph_resp = {
                "@odata.type": "#microsoft.graph.linkedResource",
                "id": GUID_LR_ID,
                "webUrl": TEST_URL,
                "displayName": TEST_NAME,
                "applicationName": "todo-sync",
            }
            return _make_httpx_response(graph_resp, status_code=201)

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)

        with patch("httpx.AsyncClient.request", side_effect=capture_httpx_request):
            with patch(
                "app.services.graph_client.auth_service.get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ):
                result = await linked_resource_service.create(db, task_id, data)

        # 1. A POST was actually made to Graph /linkedResources
        assert captured_request.get("method") == "POST", (
            f"Expected POST to Graph, got method={captured_request.get('method')!r}"
        )
        assert "linkedResources" in captured_request.get("url", ""), (
            f"Expected URL to contain 'linkedResources', got: {captured_request.get('url')!r}"
        )

        # 2. webUrl and displayName were forwarded correctly
        body = captured_request.get("json", {})
        assert body.get("webUrl") == TEST_URL, f"webUrl mismatch: {body.get('webUrl')!r}"
        assert body.get("displayName") == TEST_NAME, f"displayName mismatch: {body.get('displayName')!r}"

        # 3. After Graph confirms with GUID, service must set synced and ms_id
        assert lr_obj is not None, "LinkedResource object was not added to db"
        assert lr_obj.sync_status == "synced", (
            f"Expected sync_status='synced' after Graph 201, got: {lr_obj.sync_status!r}"
        )
        assert lr_obj.ms_id == GUID_LR_ID, (
            f"Expected ms_id=GUID, got: {lr_obj.ms_id!r}"
        )


# ─────────────────────────────────────────────
# B-T2: Graph error → sync_status stays pending, NOT lying synced
# ─────────────────────────────────────────────

class TestAttachUrlGraphError:
    @pytest.mark.asyncio
    async def test_bt2_graph_500_leaves_pending_not_synced(self):
        """B-T2: attach_url path — when Graph returns 500:
        - sync_status MUST be 'pending' (NOT 'synced')
        - ms_id MUST be None
        - Service must NOT lie that the resource is synced

        This is the core invariant of ADR 0003 §B-4:
        'Partial result == success' is explicitly forbidden.
        """
        from app.services import linked_resource_service
        from app.schemas import LinkedResourceIn

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)

        data = LinkedResourceIn(
            web_url=TEST_URL,
            display_name=TEST_NAME,
            application_name="todo-sync",
        )

        def fail_httpx_request(method, url, headers=None, json=None, params=None, **kwargs):
            resp = _make_httpx_response(
                {"error": {"code": "InternalServerError", "message": "something broke"}},
                status_code=500,
            )
            raise httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=resp.request,
                response=resp,
            )

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)
        db.refresh = AsyncMock()

        with patch("httpx.AsyncClient.request", side_effect=fail_httpx_request):
            with patch(
                "app.services.graph_client.auth_service.get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ):
                # Should NOT raise — service swallows the error and leaves pending
                result = await linked_resource_service.create(db, task_id, data)

        assert lr_obj is not None, "LinkedResource object was not added to db"
        assert lr_obj.sync_status == "pending", (
            f"sync_status MUST be 'pending' on Graph error (not 'synced'). "
            f"Got: {lr_obj.sync_status!r}"
        )
        assert lr_obj.ms_id is None, (
            f"ms_id MUST be None when Graph did not return a valid id. "
            f"Got: {lr_obj.ms_id!r}"
        )

    @pytest.mark.asyncio
    async def test_bt2_graph_400_leaves_pending_not_synced(self):
        """B-T2 variant: Graph returns 400 (Bad Request).
        sync_status must NOT become 'synced'.
        """
        from app.services import linked_resource_service
        from app.schemas import LinkedResourceIn

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)

        data = LinkedResourceIn(
            web_url=TEST_URL,
            display_name=TEST_NAME,
        )

        def fail_httpx_request(method, url, headers=None, json=None, params=None, **kwargs):
            resp = _make_httpx_response(
                {"error": {"code": "BadRequest", "message": "invalid body"}},
                status_code=400,
            )
            raise httpx.HTTPStatusError(
                "400 Bad Request",
                request=resp.request,
                response=resp,
            )

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)
        db.refresh = AsyncMock()

        with patch("httpx.AsyncClient.request", side_effect=fail_httpx_request):
            with patch(
                "app.services.graph_client.auth_service.get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ):
                result = await linked_resource_service.create(db, task_id, data)

        assert lr_obj is not None
        assert lr_obj.sync_status == "pending", (
            f"Expected 'pending', got: {lr_obj.sync_status!r}"
        )
        assert lr_obj.ms_id is None


# ─────────────────────────────────────────────
# B-T3: create_reference (legacy) does NOT fake synced (ADR 0003 §B-4 fix guard)
# ─────────────────────────────────────────────

class TestCreateReferenceLegacy:
    @pytest.mark.asyncio
    async def test_bt3_create_reference_does_not_set_synced(self):
        """B-T3: create_reference() (legacy local-only path) must NOT set sync_status='synced'.

        Before ADR 0003 §B-4 fix, create_reference hardcoded sync_status='synced' without
        any Graph push. That was the root cause of the lying synced bug (ticket ee8e4e23).

        After the fix: status stays 'pending' — honest, since nothing reaches Graph.
        This test is a regression guard: if someone re-introduces the fake synced, it fails.
        """
        from app.services import attachment_service

        task = MagicMock()
        task.id = uuid.uuid4()

        att_instance = MagicMock()
        att_instance.sync_status = "pending"
        att_instance.task_id = task.id

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        with patch.object(attachment_service, "TaskAttachment") as MockAtt:
            MockAtt.return_value = att_instance
            with patch.object(
                attachment_service, "_set_task_has_attachments", new_callable=AsyncMock
            ):
                await attachment_service.create_reference(
                    db,
                    task_id=task.id,
                    url="https://example.com/doc",
                    name="Example Doc",
                )

        # The fake synced assignment (att.sync_status = "synced") must be GONE.
        # sync_status remains "pending" as set at object construction.
        assert att_instance.sync_status == "pending", (
            f"create_reference must NOT set sync_status='synced' (no Graph push happens). "
            f"Got: {att_instance.sync_status!r}. "
            "Regression: the fake synced from ADR 0003 §B-4 was re-introduced."
        )

    @pytest.mark.asyncio
    async def test_bt3_create_reference_does_not_call_graph(self):
        """B-T3 variant: create_reference must make ZERO HTTP calls to Graph.

        URL attachments go through the linkedResource path (attach_url endpoint).
        create_reference is local-only storage — it must never touch Graph.
        """
        from app.services import attachment_service

        task = MagicMock()
        task.id = uuid.uuid4()

        att_instance = MagicMock()
        att_instance.sync_status = "pending"
        att_instance.task_id = task.id

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        with patch.object(attachment_service, "TaskAttachment") as MockAtt:
            MockAtt.return_value = att_instance
            with patch.object(
                attachment_service, "_set_task_has_attachments", new_callable=AsyncMock
            ):
                with patch("httpx.AsyncClient.request") as mock_http:
                    await attachment_service.create_reference(
                        db,
                        task_id=task.id,
                        url="https://example.com/doc",
                        name="Example Doc",
                    )

        # No HTTP calls should have been made
        mock_http.assert_not_called()
