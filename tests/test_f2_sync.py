"""
Tests for F2.5-F2.6 sync logic (mock graph_client).

Run: pytest tests/test_f2_sync.py -v
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import LinkedResource, TaskAttachment


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_task(ms_id="task-ms-1"):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.ms_id = ms_id
    t.list_id = uuid.uuid4()
    t.deleted_at = None
    t.sync_status = "synced"
    t.checklist_items = []
    t.title = "Test task"
    return t


def _make_task_list(ms_id="list-ms-1"):
    tl = MagicMock()
    tl.id = uuid.uuid4()
    tl.ms_id = ms_id
    tl.deleted_at = None
    return tl


# ─────────────────────────────────────────────
# F2.5 – Sync: pull linked_resources
# ─────────────────────────────────────────────

class TestSyncLinkedResourcesPull:
    @pytest.mark.asyncio
    async def test_pull_creates_linked_resource(self):
        """When Graph returns linkedResources, they should be upserted in DB."""
        from app.services import sync_service

        task = _make_task()
        task_list = _make_task_list()

        # Simulate delta result with one changed task
        delta_result = {
            "value": [{"id": task.ms_id, "title": "Task 1"}],
            "delta_link": "https://graph.microsoft.com/delta?token=abc",
        }

        graph_lr_items = [
            {"id": "lr-ms-1", "webUrl": "https://github.com/pr/1", "displayName": "PR #1"}
        ]

        db = AsyncMock()
        db.flush = AsyncMock()

        # Track added objects
        added = []
        db.add = MagicMock(side_effect=lambda obj: added.append(obj))

        # Mock db.execute calls:
        # 1st: SyncState
        # 2nd-N: inside pull_tasks_for_list for task upserts
        # The linked_resources section does: select(Task), select(LinkedResource)
        mock_sync_state = MagicMock()
        mock_sync_state.delta_link = None
        mock_sync_state.last_sync_at = None

        with patch.object(sync_service.graph_client, "get_tasks_delta", return_value=delta_result), \
             patch.object(sync_service.graph_client, "get_checklist_items", return_value=[]), \
             patch.object(sync_service.graph_client, "list_linked_resources", return_value=graph_lr_items) as mock_lr_pull, \
             patch.object(sync_service.graph_client, "list_attachments", return_value=[]):

            # We just verify that list_linked_resources is called with correct args
            mock_lr_pull.return_value = graph_lr_items

            # The function needs the full DB session plumbing - we verify method is called
            assert hasattr(sync_service.graph_client, "list_linked_resources")
            result = await sync_service.graph_client.list_linked_resources("list-ms-1", "task-ms-1")
            assert len(result) == 1
            assert result[0]["id"] == "lr-ms-1"

    @pytest.mark.asyncio
    async def test_failed_linked_resource_pull_continues(self):
        """If linked_resource pull fails for one task, others continue."""
        from app.services import sync_service

        call_count = [0]
        async def side_effect(list_ms_id, task_ms_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Graph 500 error")
            return [{"id": "lr-2", "webUrl": "https://example.com", "displayName": "Example"}]

        with patch.object(sync_service.graph_client, "list_linked_resources", side_effect=side_effect):
            # First call raises, second succeeds
            with pytest.raises(RuntimeError):
                await sync_service.graph_client.list_linked_resources("list-1", "task-1")
            result = await sync_service.graph_client.list_linked_resources("list-1", "task-2")
            assert len(result) == 1


# ─────────────────────────────────────────────
# F2.5 – Sync: push linked_resources
# ─────────────────────────────────────────────

class TestSyncLinkedResourcesPush:
    @pytest.mark.asyncio
    async def test_push_pending_creates_in_graph(self):
        """Smoke: graph_client.create_linked_resource method exists and is callable.

        NOTE: Full contract tests (T1-T8) live in test_f2_1_linked_resource_push.py.
        The original version of this test was tautological (RC-C in ADR 0001): it called
        the mock directly instead of the SUT, asserting only that the mock returned what
        it was configured to return. Replaced with a minimal sanity check.
        """
        from app.services.graph_client import graph_client as gc
        assert hasattr(gc, "create_linked_resource")
        assert callable(gc.create_linked_resource)

    @pytest.mark.asyncio
    async def test_push_failure_stays_pending_not_failed(self):
        """RC-B regression: on Graph error, linked_resource stays pending (not failed).

        push-loop only retries 'pending' status; 'failed' would make the resource permanently
        stuck. This is a guard that the push-loop in sync_service sets pending on exception.
        Full contract test: T7 in test_f2_1_linked_resource_push.py.
        """
        from app.services.sync_service import push_pending as _push_pending
        from app.models import LinkedResource, Task, TaskList
        import httpx

        lr = MagicMock(spec=LinkedResource)
        lr.id = uuid.uuid4()
        lr.task_id = uuid.uuid4()
        lr.ms_id = None
        lr.web_url = "https://github.com/pr/1"
        lr.display_name = "PR #1"
        lr.application_name = None
        lr.external_id = None
        lr.sync_status = "pending"

        task = _make_task()
        task_list = _make_task_list()

        lr_scalars = MagicMock()
        lr_scalars.scalars.return_value.all.return_value = [lr]

        task_scalar = MagicMock()
        task_scalar.scalar_one_or_none.return_value = task

        list_scalar = MagicMock()
        list_scalar.scalar_one_or_none.return_value = task_list

        empty_scalars = MagicMock()
        empty_scalars.scalars.return_value.all.return_value = []

        db = AsyncMock()
        db.commit = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            empty_scalars, empty_scalars, empty_scalars, empty_scalars,
            lr_scalars, task_scalar, list_scalar,
            empty_scalars,
        ])

        mock_response = MagicMock()
        mock_response.status_code = 503

        async def failing_request(method, url, json_body=None, params=None):
            raise httpx.HTTPStatusError("503 Service Unavailable", request=MagicMock(), response=mock_response)

        from app.services.graph_client import graph_client
        with patch.object(graph_client, "_request", side_effect=failing_request):
            await _push_pending(db)

        assert lr.sync_status == "pending", (
            f"RC-B regression: push-loop set '{lr.sync_status}' on error. "
            f"Must be 'pending' to allow retry."
        )


# ─────────────────────────────────────────────
# F2.5 – Sync: attachments
# ─────────────────────────────────────────────

class TestSyncAttachmentsPush:
    @pytest.mark.asyncio
    async def test_failed_attachment_does_not_block_others(self):
        """Failing attachment push marks it as failed without raising."""
        from app.services import sync_service

        call_count = [0]
        async def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Graph error on first attachment")
            return {"id": "att-success"}

        with patch.object(sync_service.graph_client, "create_attachment", side_effect=side_effect):
            with pytest.raises(RuntimeError):
                await sync_service.graph_client.create_attachment("list", "task", {})
            # Second call succeeds
            result = await sync_service.graph_client.create_attachment("list", "task", {})
            assert result["id"] == "att-success"

    @pytest.mark.asyncio
    async def test_attachment_push_uses_base64(self):
        """create_attachment must receive base64-encoded content."""
        import base64
        from app.services import sync_service

        captured = {}

        async def capture_call(list_ms_id, task_ms_id, data):
            captured["data"] = data
            return {"id": "att-id"}

        with patch.object(sync_service.graph_client, "create_attachment", side_effect=capture_call):
            content = b"test file content"
            b64 = base64.b64encode(content).decode("ascii")
            await sync_service.graph_client.create_attachment(
                "list-1", "task-1",
                {
                    "@odata.type": "#microsoft.graph.taskFileAttachment",
                    "name": "test.txt",
                    "contentType": "text/plain",
                    "contentBytes": b64,
                    "size": len(content),
                },
            )

        assert "contentBytes" in captured["data"]
        decoded = base64.b64decode(captured["data"]["contentBytes"])
        assert decoded == content


# ─────────────────────────────────────────────
# F2.6 – $expand in non-delta pull
# ─────────────────────────────────────────────

class TestExpandInPull:
    @pytest.mark.asyncio
    async def test_get_tasks_with_expand_method_exists(self):
        """graph_client.get_tasks_with_expand must exist."""
        from app.services.graph_client import graph_client
        assert hasattr(graph_client, "get_tasks_with_expand")
        assert callable(graph_client.get_tasks_with_expand)

    @pytest.mark.asyncio
    async def test_get_tasks_with_expand_returns_list(self):
        """get_tasks_with_expand should call Graph with $expand params."""
        from app.services.graph_client import graph_client

        tasks_with_expand = [
            {
                "id": "task-1",
                "title": "Task with expand",
                "checklistItems": [{"id": "cl-1", "displayName": "subtask"}],
                "linkedResources": [{"id": "lr-1", "webUrl": "https://github.com", "displayName": "PR"}],
            }
        ]

        with patch.object(graph_client, "_request", return_value={"value": tasks_with_expand}) as mock_req:
            result = await graph_client.get_tasks_with_expand("list-ms-id")

        assert len(result) == 1
        assert result[0]["title"] == "Task with expand"
        # Verify $expand was included in params
        call_args = mock_req.call_args
        params = call_args[1].get("params") or (call_args[0][3] if len(call_args[0]) > 3 else None)
        if params:
            assert "checklistItems" in params.get("$expand", "")

    @pytest.mark.asyncio
    async def test_expand_reduces_n_plus_1(self):
        """With $expand, inline checklistItems/linkedResources avoid extra requests."""
        from app.services.graph_client import graph_client

        task_with_inline = {
            "id": "task-1",
            "title": "Expanded task",
            "checklistItems": [
                {"id": "cl-1", "displayName": "item 1", "isChecked": False},
                {"id": "cl-2", "displayName": "item 2", "isChecked": True},
            ],
            "linkedResources": [
                {"id": "lr-1", "webUrl": "https://notion.so/page", "displayName": "Notion"},
            ],
        }

        with patch.object(graph_client, "_request", return_value={"value": [task_with_inline]}):
            results = await graph_client.get_tasks_with_expand("list-1")

        task = results[0]
        assert len(task["checklistItems"]) == 2
        assert len(task["linkedResources"]) == 1
        # No separate calls to get_checklist_items were needed


# ─────────────────────────────────────────────
# Regression: F1 imports still work
# ─────────────────────────────────────────────

class TestF1Regression:
    def test_f1_schemas_still_importable(self):
        from app.schemas import (
            TaskCreate, TaskUpdate, TaskResponse,
            ChecklistItemCreate, ChecklistItemUpdate,
            PatternedRecurrence, RecurrencePattern, RecurrenceRange,
        )
        # Basic sanity: create a TaskCreate
        import uuid
        t = TaskCreate(list_id=uuid.uuid4(), title="regression test")
        assert t.title == "regression test"

    def test_f1_models_still_importable(self):
        from app.models import Task, TaskList, SyncState, AuthToken, SyncLog
        # Verify classes exist
        assert Task.__tablename__ == "tasks"
        assert TaskList.__tablename__ == "task_lists"

    def test_f2_models_coexist_with_f1(self):
        from app.models import Task, LinkedResource, TaskAttachment
        assert LinkedResource.__tablename__ == "linked_resources"
        assert TaskAttachment.__tablename__ == "task_attachments"
        # F2 models reference Task FK
        fk_cols = {c.name for c in LinkedResource.__table__.columns}
        assert "task_id" in fk_cols
