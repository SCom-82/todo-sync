"""
F2.1 contract tests: linked_resource push correctness.

Mocks are placed at the HTTP boundary (graph_client._request), NOT at higher-level
methods like create_linked_resource. This follows the lesson from S118 / recurring-fix:
mocking too high hides real service-layer bugs.

All tests call real service functions:
- linked_resource_service.create()
- sync_service._push_pending_linked_resources() (via sync_service._push_pending)

Run: pytest tests/test_f2_1_linked_resource_push.py -v
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models import LinkedResource, Task, TaskList
from app.services.graph_client import graph_client


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

GUID_ID = "f9cddce2-dce2-f9cd-e2dc-cdf9e2dccdf9"  # real Graph linkedResource id format
LIST_MS_ID = "list-ms-001"
TASK_MS_ID = "task-ms-001"


def _make_db_with_task(task_ms_id=TASK_MS_ID, list_ms_id=LIST_MS_ID, task_id=None, list_id=None):
    """Build a minimal AsyncMock db session returning one Task + one TaskList."""
    task = MagicMock(spec=Task)
    task.id = task_id or uuid.uuid4()
    task.ms_id = task_ms_id
    task.list_id = list_id or uuid.uuid4()

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

    # db.execute returns different results depending on call order:
    # 1st call → Task query, 2nd call → TaskList query
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task

    list_result = MagicMock()
    list_result.scalar_one_or_none.return_value = task_list

    db.execute = AsyncMock(side_effect=[task_result, list_result])
    return db, task, task_list


def _make_lr_schema():
    from app.schemas import LinkedResourceIn
    return LinkedResourceIn(
        web_url="https://github.com/org/repo/pull/42",
        display_name="PR #42",
        application_name="GitHub",
    )


# ─────────────────────────────────────────────
# T1 — happy path: GUID id is accepted (not rejected)
# ─────────────────────────────────────────────

class TestLinkedResourceCreateHappyPath:
    @pytest.mark.asyncio
    async def test_t1_guid_id_accepted_sets_synced(self):
        """T1: Graph returns 201 with GUID-format id. lr.ms_id = GUID, sync_status = synced.

        GUID-shaped ids are the documented format for linkedResources (ADR §2).
        They must NOT be rejected by is_task_graph_id's UUID-filter.
        """
        from app.services import linked_resource_service

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)
        data = _make_lr_schema()

        async def mock_request(method, url, json_body=None, params=None):
            assert method == "POST"
            assert "linkedResources" in url
            return {
                "@odata.type": "#microsoft.graph.linkedResource",
                "id": GUID_ID,
                "webUrl": json_body.get("webUrl"),
                "displayName": json_body.get("displayName"),
            }

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)

        with patch.object(graph_client, "_request", side_effect=mock_request):
            # Patch db.refresh to set lr_obj fields as service expects after commit
            async def fake_refresh(obj):
                pass  # lr_obj already has all attributes set by service
            db.refresh = AsyncMock(side_effect=fake_refresh)

            result = await linked_resource_service.create(db, task_id, data)

        # The service sets attributes on the lr object directly before commit
        assert lr_obj is not None, "LinkedResource was not added to db"
        assert lr_obj.ms_id == GUID_ID, f"Expected GUID ms_id, got: {lr_obj.ms_id!r}"
        assert lr_obj.sync_status == "synced", f"Expected synced, got: {lr_obj.sync_status!r}"


# ─────────────────────────────────────────────
# T2 — Graph 400 → pending, ms_id=None
# ─────────────────────────────────────────────

class TestLinkedResourceCreate400:
    @pytest.mark.asyncio
    async def test_t2_graph_400_leaves_pending(self):
        """T2: Graph returns 400. lr.sync_status stays pending, ms_id is None."""
        from app.services import linked_resource_service

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)
        data = _make_lr_schema()

        mock_response = MagicMock()
        mock_response.status_code = 400

        async def mock_request(method, url, json_body=None, params=None):
            raise httpx.HTTPStatusError("400 Bad Request", request=MagicMock(), response=mock_response)

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)
        db.refresh = AsyncMock()

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await linked_resource_service.create(db, task_id, data)

        assert lr_obj is not None
        assert lr_obj.sync_status == "pending", f"Expected pending, got: {lr_obj.sync_status!r}"
        assert lr_obj.ms_id is None, f"Expected ms_id=None, got: {lr_obj.ms_id!r}"


# ─────────────────────────────────────────────
# T3 — Graph 500 → pending, ms_id=None
# ─────────────────────────────────────────────

class TestLinkedResourceCreate500:
    @pytest.mark.asyncio
    async def test_t3_graph_500_leaves_pending(self):
        """T3: Graph returns 500. lr.sync_status stays pending, ms_id is None."""
        from app.services import linked_resource_service

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)
        data = _make_lr_schema()

        mock_response = MagicMock()
        mock_response.status_code = 500

        async def mock_request(method, url, json_body=None, params=None):
            raise httpx.HTTPStatusError("500 Internal Server Error", request=MagicMock(), response=mock_response)

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)
        db.refresh = AsyncMock()

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await linked_resource_service.create(db, task_id, data)

        assert lr_obj is not None
        assert lr_obj.sync_status == "pending"
        assert lr_obj.ms_id is None


# ─────────────────────────────────────────────
# T4 — 201 with empty body → pending (key test)
# ─────────────────────────────────────────────

class TestLinkedResourceCreate201NoId:
    @pytest.mark.asyncio
    async def test_t4_201_without_id_leaves_pending(self):
        """T4 (key): Graph returns 201 but body has no 'id'. Must NOT set synced.

        This is the RC-A1 regression: the old code did synced unconditionally.
        """
        from app.services import linked_resource_service

        task_id = uuid.uuid4()
        db, task, _ = _make_db_with_task(task_id=task_id)
        data = _make_lr_schema()

        async def mock_request(method, url, json_body=None, params=None):
            return {}  # 201 OK but empty body — no "id"

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)
        db.refresh = AsyncMock()

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await linked_resource_service.create(db, task_id, data)

        assert lr_obj is not None
        assert lr_obj.sync_status == "pending", (
            f"REGRESSION: synced was set without a real id. Got: {lr_obj.sync_status!r}"
        )
        assert lr_obj.ms_id is None, f"Expected ms_id=None, got: {lr_obj.ms_id!r}"


# ─────────────────────────────────────────────
# T5 — task.ms_id is None → push skipped
# ─────────────────────────────────────────────

class TestLinkedResourceCreateTaskNotSynced:
    @pytest.mark.asyncio
    async def test_t5_task_without_ms_id_skips_push(self):
        """T5: If parent task has ms_id=None (not yet synced to Graph), push is skipped silently.

        lr stays pending. _request is never called. No exception raised.
        """
        from app.services import linked_resource_service

        task_id = uuid.uuid4()
        # task.ms_id = None simulates task not yet pushed to Graph
        db, task, _ = _make_db_with_task(task_ms_id=None, task_id=task_id)
        data = _make_lr_schema()

        lr_obj = None

        def capture_add(obj):
            nonlocal lr_obj
            if isinstance(obj, LinkedResource):
                lr_obj = obj

        db.add = MagicMock(side_effect=capture_add)
        db.refresh = AsyncMock()

        request_called = False

        async def mock_request(*args, **kwargs):
            nonlocal request_called
            request_called = True
            return {"id": GUID_ID}

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await linked_resource_service.create(db, task_id, data)

        assert lr_obj is not None
        assert lr_obj.sync_status == "pending", f"Expected pending, got: {lr_obj.sync_status!r}"
        assert lr_obj.ms_id is None
        assert not request_called, "Graph _request was called despite task.ms_id being None"


# ─────────────────────────────────────────────
# T6 — push-loop happy path
# ─────────────────────────────────────────────

class TestPushLoopHappyPath:
    @pytest.mark.asyncio
    async def test_t6_push_loop_happy_sets_synced(self):
        """T6: _push_pending_linked_resources happy path — pending lr becomes synced with GUID."""
        from app.services import sync_service

        lr_id = uuid.uuid4()
        task_id = uuid.uuid4()

        lr = MagicMock(spec=LinkedResource)
        lr.id = lr_id
        lr.task_id = task_id
        lr.ms_id = None
        lr.web_url = "https://github.com/pr/42"
        lr.display_name = "PR #42"
        lr.application_name = "GitHub"
        lr.external_id = None
        lr.sync_status = "pending"

        task = MagicMock(spec=Task)
        task.id = task_id
        task.ms_id = TASK_MS_ID
        task.list_id = uuid.uuid4()

        task_list = MagicMock(spec=TaskList)
        task_list.id = task.list_id
        task_list.ms_id = LIST_MS_ID

        db = AsyncMock()
        db.commit = AsyncMock()

        # _push_pending executes: select(LinkedResource pending), select(Task), select(TaskList)
        # Plus select(Task deleted_at), select(Task pending_push), etc — many queries.
        # We build side_effect list for just the lr-related portion.
        # Easier: patch the whole _push_pending_linked_resources sub-section.
        # Actually we need to call the real push-loop. Let's mock db.execute carefully.

        # We'll directly test the linked-resource sub-loop by mocking execute to return
        # a sequence: [lr_query, task_query, list_query] for the lr push section.
        lr_scalars = MagicMock()
        lr_scalars.scalars.return_value.all.return_value = [lr]

        task_scalar = MagicMock()
        task_scalar.scalar_one_or_none.return_value = task

        list_scalar = MagicMock()
        list_scalar.scalar_one_or_none.return_value = task_list

        # _push_pending calls many db.execute — we only care about the lr ones.
        # Provide enough results for the whole function:
        # 1: pushed_lists query (task_lists where sync_status pending)
        # 2: pushed_tasks create query
        # 3: pushed_tasks update query
        # 4: pushed_tasks delete query (deleted_at not None)
        # 5: lr pending query
        # 6: task query for lr
        # 7: list query for lr
        # 8: att pending query
        empty_scalars = MagicMock()
        empty_scalars.scalars.return_value.all.return_value = []

        db.execute = AsyncMock(side_effect=[
            empty_scalars,   # task_lists pending_push
            empty_scalars,   # tasks pending_push create
            empty_scalars,   # tasks pending_push update
            empty_scalars,   # tasks deleted pending delete
            lr_scalars,      # linked_resources pending
            task_scalar,     # task for lr
            list_scalar,     # task_list for lr
            empty_scalars,   # attachments pending
        ])

        async def mock_request(method, url, json_body=None, params=None):
            assert method == "POST"
            assert "linkedResources" in url
            return {
                "id": GUID_ID,
                "webUrl": json_body.get("webUrl"),
            }

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await sync_service.push_pending(db)

        assert lr.ms_id == GUID_ID, f"Expected GUID, got: {lr.ms_id!r}"
        assert lr.sync_status == "synced", f"Expected synced, got: {lr.sync_status!r}"


# ─────────────────────────────────────────────
# T7 — push-loop Graph 500 → pending (not failed)
# ─────────────────────────────────────────────

class TestPushLoopRetryOnError:
    @pytest.mark.asyncio
    async def test_t7_push_loop_500_stays_pending_not_failed(self):
        """T7: push-loop gets Graph 500. lr.sync_status must be pending (not failed).

        RC-B fix: terminal 'failed' made resource unrecoverable because push-loop
        only picks up 'pending'. Now 500 → pending so next sync retries.
        """
        from app.services import sync_service

        lr_id = uuid.uuid4()
        task_id = uuid.uuid4()

        lr = MagicMock(spec=LinkedResource)
        lr.id = lr_id
        lr.task_id = task_id
        lr.ms_id = None
        lr.web_url = "https://github.com/pr/42"
        lr.display_name = "PR #42"
        lr.application_name = None
        lr.external_id = None
        lr.sync_status = "pending"

        task = MagicMock(spec=Task)
        task.id = task_id
        task.ms_id = TASK_MS_ID
        task.list_id = uuid.uuid4()

        task_list = MagicMock(spec=TaskList)
        task_list.id = task.list_id
        task_list.ms_id = LIST_MS_ID

        db = AsyncMock()
        db.commit = AsyncMock()

        lr_scalars = MagicMock()
        lr_scalars.scalars.return_value.all.return_value = [lr]

        task_scalar = MagicMock()
        task_scalar.scalar_one_or_none.return_value = task

        list_scalar = MagicMock()
        list_scalar.scalar_one_or_none.return_value = task_list

        empty_scalars = MagicMock()
        empty_scalars.scalars.return_value.all.return_value = []

        db.execute = AsyncMock(side_effect=[
            empty_scalars,
            empty_scalars,
            empty_scalars,
            empty_scalars,
            lr_scalars,
            task_scalar,
            list_scalar,
            empty_scalars,
        ])

        mock_response = MagicMock()
        mock_response.status_code = 500

        async def mock_request(method, url, json_body=None, params=None):
            raise httpx.HTTPStatusError("500 Internal Server Error", request=MagicMock(), response=mock_response)

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await sync_service.push_pending(db)

        assert lr.sync_status == "pending", (
            f"REGRESSION (RC-B): expected pending after 500, got: {lr.sync_status!r}. "
            f"'failed' makes resource permanently stuck in push-loop."
        )
        assert lr.ms_id is None


# ─────────────────────────────────────────────
# T8 — regression: validator behaviour difference
# ─────────────────────────────────────────────

class TestValidatorRegression:
    def test_t8_is_present_id_accepts_guid(self):
        """T8a: is_present_id returns the GUID — not None. GUID is valid for linkedResources."""
        from app.services.graph_id import is_present_id
        result = is_present_id({"id": GUID_ID})
        assert result == GUID_ID, (
            f"is_present_id rejected a valid GUID. Got: {result!r}. "
            f"This would permanently block linked_resource sync."
        )

    def test_t8_is_task_graph_id_rejects_guid(self):
        """T8b: is_task_graph_id returns None for GUID — correct, Task ids are never GUIDs."""
        from app.services.graph_id import is_task_graph_id
        result = is_task_graph_id({"id": GUID_ID})
        assert result is None, (
            f"is_task_graph_id should reject GUID-shaped id for tasks. Got: {result!r}."
        )

    def test_t8_is_task_graph_id_accepts_base64(self):
        """T8c: is_task_graph_id accepts long opaque base64 Task ids."""
        from app.services.graph_id import is_task_graph_id
        base64_id = "AQMkADAwATNiZmYAZC1lZWI4LWI5MjktMDACLTAwCgBGAAADJ8Q"
        result = is_task_graph_id({"id": base64_id})
        assert result == base64_id

    def test_t8_is_present_id_rejects_empty_string(self):
        """T8d: is_present_id returns None for empty/missing id."""
        from app.services.graph_id import is_present_id
        assert is_present_id({}) is None
        assert is_present_id({"id": ""}) is None
        assert is_present_id({"id": None}) is None

    def test_t8_is_present_id_accepts_base64(self):
        """T8e: is_present_id also accepts base64 ids (attachments)."""
        from app.services.graph_id import is_present_id
        base64_id = "AQMkADAwATNiZmYAZC1lZWI4LWI5MjktMDACLTAwCgBGAAADJ8Q"
        result = is_present_id({"id": base64_id})
        assert result == base64_id
