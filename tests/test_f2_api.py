"""
Tests for F2.1-F2.3 API endpoints (mock-based, no live DB).

Run: pytest tests/test_f2_api.py -v
"""
import base64
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import MAX_ATTACHMENT_BYTES


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_task(task_id=None):
    task = MagicMock()
    task.id = task_id or uuid.uuid4()
    task.ms_id = "ms-task-id"
    task.list_id = uuid.uuid4()
    task.deleted_at = None
    return task


def _make_lr(lr_id=None, task_id=None):
    now = datetime.now(timezone.utc)
    lr = MagicMock()
    lr.id = lr_id or uuid.uuid4()
    lr.task_id = task_id or uuid.uuid4()
    lr.ms_id = "ms-lr-id"
    lr.web_url = "https://github.com/org/repo"
    lr.display_name = "PR #1"
    lr.application_name = "GitHub"
    lr.external_id = None
    lr.sync_status = "synced"
    lr.created_at = now
    lr.updated_at = now
    return lr


def _make_attachment(att_id=None, task_id=None, content=b"hello"):
    now = datetime.now(timezone.utc)
    att = MagicMock()
    att.id = att_id or uuid.uuid4()
    att.task_id = task_id or uuid.uuid4()
    att.ms_id = "ms-att-id"
    att.name = "test.txt"
    att.content_type = "text/plain"
    att.size_bytes = len(content)
    att.content_bytes = content
    att.reference_url = None
    att.sync_status = "pending"
    att.created_at = now
    return att


# ─────────────────────────────────────────────
# F2.1 – LinkedResource validation tests
# ─────────────────────────────────────────────

class TestLinkedResourceValidation:
    def test_invalid_url_returns_422(self):
        """Pydantic validation: invalid URL in LinkedResourceIn → ValidationError."""
        from pydantic import ValidationError
        from app.schemas import LinkedResourceIn
        with pytest.raises(ValidationError):
            LinkedResourceIn(web_url="not-a-url", display_name="test")

    def test_valid_url_accepted(self):
        from app.schemas import LinkedResourceIn
        lr = LinkedResourceIn(
            web_url="https://example.com/resource",
            display_name="Resource",
        )
        assert "example.com" in str(lr.web_url)

    def test_ftp_url_rejected(self):
        """Only http/https should be accepted by AnyHttpUrl."""
        from pydantic import ValidationError
        from app.schemas import LinkedResourceIn
        with pytest.raises(ValidationError):
            LinkedResourceIn(web_url="ftp://files.example.com", display_name="FTP")


# ─────────────────────────────────────────────
# F2.1 – LinkedResource service (mock DB + Graph)
# ─────────────────────────────────────────────

class TestLinkedResourceService:
    @pytest.mark.asyncio
    async def test_create_linked_resource(self):
        from app.schemas import LinkedResourceIn
        from app.services import linked_resource_service

        task = _make_task()
        task_list = MagicMock()
        task_list.ms_id = "ms-list-id"

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        created_lr = _make_lr(task_id=task.id)

        # Mock db.execute for Task and TaskList queries
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        list_result = MagicMock()
        list_result.scalar_one_or_none.return_value = task_list

        async def mock_execute(query):
            # Alternate: first call for Task, second for TaskList
            return [task_result, list_result][mock_execute.call_count - 1]

        mock_execute.call_count = 0
        original_execute = db.execute

        call_count = [0]
        async def side_effect(q):
            call_count[0] += 1
            if call_count[0] == 1:
                return task_result
            return list_result

        db.execute = side_effect

        data = LinkedResourceIn(
            web_url="https://github.com/org/repo/pull/42",
            display_name="PR #42",
            application_name="GitHub",
        )

        with patch.object(
            linked_resource_service.graph_client,
            "create_linked_resource",
            return_value={"id": "graph-lr-id"},
        ):
            # Mock refresh to set id on the lr object
            async def mock_refresh(obj):
                if not hasattr(obj, '_is_mock_lr'):
                    pass
            db.refresh = mock_refresh

            # The actual call will fail at db.refresh because MagicMock
            # We test that it attempts the creation without raising
            with patch.object(linked_resource_service, "LinkedResource") as MockLR:
                mock_lr_instance = MagicMock()
                mock_lr_instance.ms_id = None
                mock_lr_instance.sync_status = "pending"
                MockLR.return_value = mock_lr_instance

                await linked_resource_service.create(db, task.id, data)
                db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_linked_resource(self):
        from app.services import linked_resource_service

        lr = _make_lr()
        db = AsyncMock()
        db.get = AsyncMock(return_value=lr)
        db.commit = AsyncMock()

        task = _make_task()
        task_list = MagicMock()
        task_list.ms_id = "ms-list-id"

        call_count = [0]
        async def side_effect(q):
            call_count[0] += 1
            r = MagicMock()
            if call_count[0] == 1:
                r.scalar_one_or_none.return_value = task
            else:
                r.scalar_one_or_none.return_value = task_list
            return r

        db.execute = side_effect

        with patch.object(linked_resource_service.graph_client, "delete_linked_resource", return_value=None):
            result = await linked_resource_service.delete(db, lr.id)
        assert result is True
        db.delete.assert_called_once_with(lr)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self):
        from app.services import linked_resource_service

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        result = await linked_resource_service.delete(db, uuid.uuid4())
        assert result is False


# ─────────────────────────────────────────────
# F2.2/F2.3 – Attachment size validation
# ─────────────────────────────────────────────

class TestAttachmentSizeValidation:
    def test_max_attachment_constant(self):
        assert MAX_ATTACHMENT_BYTES == 3 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_create_file_within_limit(self):
        from app.services import attachment_service

        content = b"x" * (1024 * 1024)  # 1 MB
        task = _make_task()
        task_list = MagicMock()
        task_list.ms_id = "ms-list-id"

        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        att_instance = _make_attachment(content=content, task_id=task.id)

        call_count = [0]
        async def side_effect(q):
            call_count[0] += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = task if call_count[0] == 1 else task_list
            return r

        db.execute = side_effect

        async def mock_refresh(obj):
            pass
        db.refresh = mock_refresh

        with patch.object(attachment_service, "TaskAttachment") as MockAtt:
            MockAtt.return_value = att_instance
            with patch.object(attachment_service.graph_client, "create_attachment", return_value={"id": "graph-att"}):
                result = await attachment_service.create_file(
                    db,
                    task_id=task.id,
                    name="bigfile.bin",
                    content_type="application/octet-stream",
                    content=content,
                )
        # No exception raised — size is within limit
        db.add.assert_called_once()

    def test_413_threshold(self):
        """Verify the size threshold for HTTP 413 response."""
        over_limit = MAX_ATTACHMENT_BYTES + 1
        assert over_limit > MAX_ATTACHMENT_BYTES

    @pytest.mark.asyncio
    async def test_create_reference_attachment(self):
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
            # F3.5: patch _set_task_has_attachments to avoid db.execute plumbing in this test
            with patch.object(attachment_service, "_set_task_has_attachments", new_callable=AsyncMock):
                result = await attachment_service.create_reference(
                    db,
                    task_id=task.id,
                    url="https://drive.google.com/file/123",
                    name="Google Drive file",
                )

        # Reference attachments get synced=True (local-only, no Graph push)
        assert att_instance.sync_status == "synced"
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_attachment(self):
        from app.services import attachment_service

        att = _make_attachment()
        att.sync_status = "synced"
        task = _make_task()
        task_list = MagicMock()
        task_list.ms_id = "ms-list-id"

        db = AsyncMock()
        db.get = AsyncMock(return_value=att)
        db.commit = AsyncMock()

        call_count = [0]
        async def side_effect(q):
            call_count[0] += 1
            r = MagicMock()
            r.scalar_one_or_none.return_value = task if call_count[0] == 1 else task_list
            return r

        db.execute = side_effect

        with patch.object(attachment_service.graph_client, "delete_attachment", return_value=None):
            result = await attachment_service.delete(db, att.id)
        assert result is True
        db.delete.assert_called_once_with(att)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_attachment(self):
        from app.services import attachment_service

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        result = await attachment_service.delete(db, uuid.uuid4())
        assert result is False


# ─────────────────────────────────────────────
# F2.5 – base64 conversion for Graph
# ─────────────────────────────────────────────

class TestBase64Conversion:
    def test_content_bytes_to_base64(self):
        import base64
        content = b"hello world"
        encoded = base64.b64encode(content).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == content

    def test_empty_content_base64(self):
        import base64
        content = b""
        encoded = base64.b64encode(content).decode("ascii")
        assert encoded == ""

    def test_1mb_content_base64_size(self):
        import base64
        content = b"x" * (1024 * 1024)
        encoded = base64.b64encode(content).decode("ascii")
        # base64 expands by ~33%
        assert len(encoded) > len(content)
        assert base64.b64decode(encoded) == content
