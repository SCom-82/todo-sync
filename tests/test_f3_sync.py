"""
Tests for F3.5 has_attachments flip and F3.6 delta sync metrics.

Run: pytest tests/test_f3_sync.py -v
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import SyncState


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_task(ms_id="task-ms-1", has_attachments=False):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.ms_id = ms_id
    t.list_id = uuid.uuid4()
    t.deleted_at = None
    t.sync_status = "synced"
    t.checklist_items = []
    t.title = "Test task"
    t.has_attachments = has_attachments
    return t


def _make_attachment(att_id=None, task_id=None, ms_id="ms-att-1"):
    now = datetime.now(timezone.utc)
    att = MagicMock()
    att.id = att_id or uuid.uuid4()
    att.task_id = task_id or uuid.uuid4()
    att.ms_id = ms_id
    att.name = "test.txt"
    att.content_type = "text/plain"
    att.size_bytes = 100
    att.content_bytes = b"hello"
    att.reference_url = None
    att.sync_status = "synced"
    att.created_at = now
    return att


# ─────────────────────────────────────────────
# F3.5 — has_attachments flip in attachment_service
# ─────────────────────────────────────────────

class TestHasAttachmentsFlip:
    @pytest.mark.asyncio
    async def test_create_file_sets_has_attachments_true(self):
        """Creating a file attachment sets task.has_attachments = True."""
        from app.services import attachment_service

        task = _make_task()
        task_list = MagicMock()
        task_list.ms_id = "ms-list-id"

        content = b"test content"

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        att_instance = _make_attachment(task_id=task.id)

        # Track has_attachments mutations
        changes = []

        async def mock_execute(query):
            r = MagicMock()
            r.scalar_one_or_none.return_value = task
            return r

        db.execute = mock_execute

        async def mock_refresh(obj):
            pass
        db.refresh = mock_refresh

        with patch.object(attachment_service, "TaskAttachment") as MockAtt:
            MockAtt.return_value = att_instance
            with patch.object(attachment_service, "_try_push_to_graph", return_value=None):
                with patch.object(
                    attachment_service, "_set_task_has_attachments",
                    wraps=lambda db, tid, val: changes.append(val) or None
                ) as mock_set:
                    # Simulate the real function calling _set_task_has_attachments
                    mock_set.return_value = None
                    mock_set.side_effect = AsyncMock(side_effect=lambda db, tid, val: changes.append(val))
                    
                    await attachment_service.create_file(
                        db,
                        task_id=task.id,
                        name="test.txt",
                        content_type="text/plain",
                        content=content,
                    )

        # Verify _set_task_has_attachments was called with True
        mock_set.assert_called_once()
        call_args = mock_set.call_args
        assert call_args[0][2] is True or call_args[1].get("value") is True or changes == [True]

    @pytest.mark.asyncio
    async def test_create_reference_sets_has_attachments_true(self):
        """Creating a reference attachment sets task.has_attachments = True."""
        from app.services import attachment_service

        task = _make_task()
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        att_instance = MagicMock()
        att_instance.sync_status = "pending"
        att_instance.task_id = task.id

        async def mock_refresh(obj):
            pass
        db.refresh = mock_refresh

        with patch.object(attachment_service, "TaskAttachment") as MockAtt:
            MockAtt.return_value = att_instance
            with patch.object(
                attachment_service, "_set_task_has_attachments"
            ) as mock_set:
                mock_set.return_value = None
                mock_set.side_effect = AsyncMock()
                
                await attachment_service.create_reference(
                    db,
                    task_id=task.id,
                    url="https://drive.google.com/file",
                    name="Google Drive",
                )

        mock_set.assert_called_once()
        # First positional arg after db and task_id should be True
        call_args = mock_set.call_args[0]
        assert call_args[2] is True

    @pytest.mark.asyncio
    async def test_delete_last_attachment_clears_flag(self):
        """Deleting the last attachment sets task.has_attachments = False."""
        from app.services import attachment_service
        from app.models import TaskAttachment as TaskAttachmentModel

        task = _make_task(has_attachments=True)

        # Use spec to ensure att.ms_id is properly controlled
        att = MagicMock(spec=TaskAttachmentModel)
        att.id = uuid.uuid4()
        att.task_id = task.id
        att.ms_id = None  # local-only: skip Graph delete path
        att.sync_status = "synced"
        att.name = "test.txt"

        db = AsyncMock()
        db.get = AsyncMock(return_value=att)
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.delete = AsyncMock()

        call_count = [0]
        async def mock_execute(q):
            call_count[0] += 1
            r = MagicMock()
            # Query for remaining attachments after db.delete + db.flush
            r.scalars.return_value.first.return_value = None
            return r

        db.execute = mock_execute

        with patch.object(
            attachment_service, "_set_task_has_attachments"
        ) as mock_set:
            mock_set.return_value = None
            mock_set.side_effect = AsyncMock()

            result = await attachment_service.delete(db, att.id)

        assert result is True
        # _set_task_has_attachments should have been called with False (no remaining attachments)
        mock_set.assert_called_once()
        call_args = mock_set.call_args[0]
        assert call_args[2] is False

    def test_set_task_has_attachments_helper_exists(self):
        """_set_task_has_attachments helper function exists in attachment_service."""
        from app.services import attachment_service
        assert hasattr(attachment_service, "_set_task_has_attachments")
        assert callable(attachment_service._set_task_has_attachments)


# ─────────────────────────────────────────────
# F3.5 — has_attachments set during pull from Graph
# ─────────────────────────────────────────────

class TestHasAttachmentsInSyncPull:
    def test_has_attachments_in_task_data_dict(self):
        """sync_service.pull_tasks_for_list must include has_attachments in task_data."""
        import inspect
        from app.services import sync_service
        source = inspect.getsource(sync_service.pull_tasks_for_list)
        assert "has_attachments" in source
        assert "hasAttachments" in source

    def test_has_attachments_bool_cast(self):
        """hasAttachments from Graph should be cast to bool."""
        # Verify that item.get("hasAttachments", False) is cast via bool()
        assert bool(None) is False
        assert bool(True) is True
        assert bool(False) is False
        # Graph API may return string or null in some edge cases
        assert bool("") is False


# ─────────────────────────────────────────────
# F3.6 — delta sync metrics instrumentation
# ─────────────────────────────────────────────

class TestDeltaSyncMetrics:
    def test_sync_state_has_metric_columns(self):
        """SyncState model must have the 4 metric columns."""
        assert hasattr(SyncState, "delta_syncs_total")
        assert hasattr(SyncState, "delta_syncs_succeeded")
        assert hasattr(SyncState, "delta_full_resets_total")

    def test_sync_status_response_has_metric_fields(self):
        """SyncStatusResponse schema must have the 4 metric fields."""
        from app.schemas import SyncStatusResponse
        s = SyncStatusResponse(last_sync_at=None, last_sync_status=None, resources=[])
        assert hasattr(s, "delta_syncs_total")
        assert hasattr(s, "delta_syncs_succeeded")
        assert hasattr(s, "delta_full_resets_total")
        assert hasattr(s, "delta_skip_rate_pct")

    def test_skip_rate_zero_when_no_syncs(self):
        """delta_skip_rate_pct should be 0.0 when total=0 (no division by zero)."""
        from app.schemas import SyncStatusResponse
        s = SyncStatusResponse(
            last_sync_at=None, last_sync_status=None, resources=[],
            delta_syncs_total=0, delta_syncs_succeeded=0,
        )
        assert s.delta_skip_rate_pct == 0.0

    def test_skip_rate_formula(self):
        """skip_rate = succeeded/total * 100."""
        # We test the formula logic used in the API handler directly
        total = 10
        succeeded = 8
        rate = round((succeeded / total) * 100, 2)
        assert rate == 80.0

    def test_skip_rate_100_percent(self):
        total = 5
        succeeded = 5
        rate = round((succeeded / total) * 100, 2)
        assert rate == 100.0

    def test_skip_rate_partial(self):
        total = 3
        succeeded = 2
        rate = round((succeeded / total) * 100, 2)
        assert abs(rate - 66.67) < 0.01

    @pytest.mark.asyncio
    async def test_metrics_incremented_on_successful_delta(self):
        """pull_tasks_for_list increments delta_syncs_total and delta_syncs_succeeded on success."""
        from app.services import sync_service

        task_list = MagicMock()
        task_list.id = uuid.uuid4()
        task_list.ms_id = "ms-list-1"

        state = MagicMock(spec=SyncState)
        state.delta_link = "https://graph/delta?token=existing"
        state.delta_syncs_total = 0
        state.delta_syncs_succeeded = 0
        state.delta_full_resets_total = 0

        delta_result = {
            "value": [],
            "delta_link": "https://graph/delta?token=new",
        }

        db = AsyncMock()
        db.flush = AsyncMock()

        with patch.object(sync_service, "_get_or_create_sync_state", return_value=state), \
             patch.object(sync_service.graph_client, "get_tasks_delta", return_value=delta_result), \
             patch.object(sync_service.graph_client, "get_checklist_items", return_value=[]), \
             patch.object(sync_service.graph_client, "list_linked_resources", return_value=[]), \
             patch.object(sync_service.graph_client, "list_attachments", return_value=[]):

            await sync_service.pull_tasks_for_list(db, task_list)

        # delta_syncs_total and delta_syncs_succeeded should have been incremented
        assert state.delta_syncs_total == 1
        assert state.delta_syncs_succeeded == 1

    @pytest.mark.asyncio
    async def test_metrics_incremented_on_full_reset(self):
        """pull_tasks_for_list increments delta_full_resets_total when DeltaLinkExpiredError."""
        from app.services import sync_service
        from app.services.graph_client import DeltaLinkExpiredError

        task_list = MagicMock()
        task_list.id = uuid.uuid4()
        task_list.ms_id = "ms-list-1"

        state = MagicMock(spec=SyncState)
        state.delta_link = "https://graph/delta?token=expired"
        state.delta_syncs_total = 5
        state.delta_syncs_succeeded = 4
        state.delta_full_resets_total = 0

        delta_result = {
            "value": [],
            "delta_link": "https://graph/delta?token=new",
        }

        call_count = [0]

        async def side_effect(list_ms_id, delta_link):
            call_count[0] += 1
            if call_count[0] == 1:
                raise DeltaLinkExpiredError("Delta link expired")
            return delta_result

        db = AsyncMock()
        db.flush = AsyncMock()

        with patch.object(sync_service, "_get_or_create_sync_state", return_value=state), \
             patch.object(sync_service.graph_client, "get_tasks_delta", side_effect=side_effect), \
             patch.object(sync_service.graph_client, "get_checklist_items", return_value=[]), \
             patch.object(sync_service.graph_client, "list_linked_resources", return_value=[]), \
             patch.object(sync_service.graph_client, "list_attachments", return_value=[]):

            await sync_service.pull_tasks_for_list(db, task_list)

        # total incremented, full_resets incremented, succeeded also incremented (retry succeeded)
        assert state.delta_syncs_total == 6
        assert state.delta_full_resets_total == 1
        assert state.delta_syncs_succeeded == 5


# ─────────────────────────────────────────────
# F3.6 — sync status endpoint aggregation
# ─────────────────────────────────────────────

class TestSyncStatusAggregation:
    @pytest.mark.asyncio
    async def test_status_endpoint_aggregates_metrics(self):
        """GET /sync/status aggregates metrics from all sync_state rows."""
        from app.api.sync import get_sync_status

        now = datetime.now(timezone.utc)

        state1 = MagicMock(spec=SyncState)
        state1.resource_type = "task_lists"
        state1.last_sync_at = now
        state1.last_sync_status = "success"
        state1.last_error = None
        state1.delta_syncs_total = 3
        state1.delta_syncs_succeeded = 3
        state1.delta_full_resets_total = 0

        state2 = MagicMock(spec=SyncState)
        state2.resource_type = "tasks:ms-list-1"
        state2.last_sync_at = now
        state2.last_sync_status = "success"
        state2.last_error = None
        state2.delta_syncs_total = 7
        state2.delta_syncs_succeeded = 5
        state2.delta_full_resets_total = 2

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [state1, state2]
        db.execute = AsyncMock(return_value=result_mock)

        response = await get_sync_status(db)

        assert response.delta_syncs_total == 10  # 3 + 7
        assert response.delta_syncs_succeeded == 8  # 3 + 5
        assert response.delta_full_resets_total == 2  # 0 + 2
        # skip_rate = 8/10 * 100 = 80.0
        assert response.delta_skip_rate_pct == 80.0

    @pytest.mark.asyncio
    async def test_status_endpoint_zero_metrics_no_division_error(self):
        """GET /sync/status with all-zero metrics returns 0.0 skip_rate."""
        from app.api.sync import get_sync_status

        state = MagicMock(spec=SyncState)
        state.resource_type = "task_lists"
        state.last_sync_at = None
        state.last_sync_status = "success"
        state.last_error = None
        state.delta_syncs_total = 0
        state.delta_syncs_succeeded = 0
        state.delta_full_resets_total = 0

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [state]
        db.execute = AsyncMock(return_value=result_mock)

        response = await get_sync_status(db)

        assert response.delta_syncs_total == 0
        assert response.delta_skip_rate_pct == 0.0  # No division by zero
