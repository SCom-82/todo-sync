"""
Tests for F3 schema validation.

F3.3: ShareListIn — EmailStr + permission enum
F3.5: TaskResponse — body_preview truncation logic, has_attachments field

Run: pytest tests/test_f3_schemas.py -v
"""
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_task_response(**kwargs):
    from app.schemas import TaskResponse
    defaults = dict(
        id=uuid.uuid4(),
        ms_id=None,
        list_id=uuid.uuid4(),
        title="Test task",
        body=None,
        importance="normal",
        status="notStarted",
        is_reminder_on=False,
        has_attachments=False,
        sync_status="synced",
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(kwargs)
    return TaskResponse(**defaults)


# ─────────────────────────────────────────────
# F3.3 — ShareListIn validation
# ─────────────────────────────────────────────

class TestShareListIn:
    def test_valid_email_readwrite(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="user@example.com", permission="readwrite")
        assert str(s.email) == "user@example.com"
        assert s.permission == "readwrite"

    def test_valid_email_read(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="alice@company.org", permission="read")
        assert s.permission == "read"

    def test_default_permission_is_readwrite(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="bob@example.com")
        assert s.permission == "readwrite"

    def test_invalid_email_raises(self):
        from app.schemas import ShareListIn
        with pytest.raises(ValidationError):
            ShareListIn(email="not-an-email", permission="readwrite")

    def test_missing_email_raises(self):
        from app.schemas import ShareListIn
        with pytest.raises(ValidationError):
            ShareListIn(permission="read")

    def test_invalid_permission_raises(self):
        from app.schemas import ShareListIn
        with pytest.raises(ValidationError):
            ShareListIn(email="user@example.com", permission="write")

    def test_invalid_permission_admin_raises(self):
        from app.schemas import ShareListIn
        with pytest.raises(ValidationError):
            ShareListIn(email="user@example.com", permission="admin")

    def test_email_with_plus(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="user+tag@example.com", permission="readwrite")
        assert "example.com" in str(s.email)

    def test_subdomain_email(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="admin@sub.domain.com", permission="read")
        assert s.permission == "read"


# ─────────────────────────────────────────────
# F3.5 — body_preview truncation
# ─────────────────────────────────────────────

class TestBodyPreview:
    def test_short_body_returns_full(self):
        t = _make_task_response(body="Hello world")
        assert t.body_preview == "Hello world"

    def test_exactly_256_chars_no_truncation(self):
        body = "x" * 256
        t = _make_task_response(body=body)
        assert t.body_preview == body
        assert len(t.body_preview) == 256

    def test_257_chars_truncated_to_256(self):
        body = "x" * 257
        t = _make_task_response(body=body)
        assert len(t.body_preview) <= 256

    def test_long_body_word_boundary_truncation(self):
        # 30 repetitions of "word1 word2 " = 360 chars
        body = "word1 word2 " * 30
        t = _make_task_response(body=body)
        assert len(t.body_preview) <= 256
        # Should end at a word boundary (no trailing space in this case)
        # The last word should be complete
        assert not t.body_preview.endswith(" ")

    def test_long_body_no_spaces_truncated_at_256(self):
        body = "a" * 300
        t = _make_task_response(body=body)
        assert len(t.body_preview) == 256

    def test_none_body_gives_none_preview(self):
        t = _make_task_response(body=None)
        assert t.body_preview is None

    def test_empty_body_gives_empty_preview(self):
        t = _make_task_response(body="")
        # Empty string is falsy, so body_preview stays None
        assert t.body_preview is None

    def test_explicit_body_preview_not_overwritten(self):
        """If body_preview is provided explicitly, validator should not overwrite it."""
        t = _make_task_response(body="Long body " * 50, body_preview="custom preview")
        # Validator runs mode="after" and only sets if body_preview is None
        assert t.body_preview == "custom preview"

    def test_unicode_body_truncation(self):
        body = "привет мир " * 30  # Russian words, ~330 chars
        t = _make_task_response(body=body)
        assert len(t.body_preview) <= 256


# ─────────────────────────────────────────────
# F3.5 — has_attachments field
# ─────────────────────────────────────────────

class TestHasAttachments:
    def test_has_attachments_default_false(self):
        t = _make_task_response()
        assert t.has_attachments is False

    def test_has_attachments_true(self):
        t = _make_task_response(has_attachments=True)
        assert t.has_attachments is True

    def test_has_attachments_from_attributes(self):
        """TaskResponse.from_attributes should pick up has_attachments from ORM model."""
        from app.schemas import TaskResponse
        from unittest.mock import MagicMock
        mock_task = MagicMock()
        mock_task.id = uuid.uuid4()
        mock_task.ms_id = None
        mock_task.list_id = uuid.uuid4()
        mock_task.title = "Test"
        mock_task.body = None
        mock_task.body_content_type = "text"
        mock_task.importance = "normal"
        mock_task.status = "notStarted"
        mock_task.due_date = None
        mock_task.due_datetime = None
        mock_task.due_timezone = None
        mock_task.start_datetime = None
        mock_task.start_timezone = None
        mock_task.reminder_datetime = None
        mock_task.is_reminder_on = False
        mock_task.completed_datetime = None
        mock_task.recurrence = None
        mock_task.categories = []
        mock_task.checklist_items = []
        mock_task.has_attachments = True
        mock_task.body_preview = None
        mock_task.sync_status = "synced"
        mock_task.created_at = _now()
        mock_task.updated_at = _now()

        t = TaskResponse.model_validate(mock_task)
        assert t.has_attachments is True


# ─────────────────────────────────────────────
# F3.6 — SyncStatusResponse metrics fields
# ─────────────────────────────────────────────

class TestSyncStatusMetrics:
    def test_default_metrics_are_zero(self):
        from app.schemas import SyncStatusResponse
        s = SyncStatusResponse(last_sync_at=None, last_sync_status=None, resources=[])
        assert s.delta_syncs_total == 0
        assert s.delta_syncs_succeeded == 0
        assert s.delta_full_resets_total == 0
        assert s.delta_skip_rate_pct == 0.0

    def test_metrics_set_correctly(self):
        from app.schemas import SyncStatusResponse
        s = SyncStatusResponse(
            last_sync_at=None, last_sync_status=None, resources=[],
            delta_syncs_total=10, delta_syncs_succeeded=8,
            delta_full_resets_total=2, delta_skip_rate_pct=80.0,
        )
        assert s.delta_syncs_total == 10
        assert s.delta_syncs_succeeded == 8
        assert s.delta_full_resets_total == 2
        assert s.delta_skip_rate_pct == 80.0


# ─────────────────────────────────────────────
# Regression: F1/F2 schemas still work
# ─────────────────────────────────────────────

class TestF1F2Regression:
    def test_task_response_imports(self):
        from app.schemas import (
            TaskCreate, TaskUpdate, TaskResponse,
            ChecklistItemCreate, ChecklistItemUpdate,
            PatternedRecurrence, RecurrencePattern, RecurrenceRange,
        )
        t = TaskCreate(list_id=uuid.uuid4(), title="regression")
        assert t.title == "regression"

    def test_linked_resource_schema(self):
        from app.schemas import LinkedResourceIn
        lr = LinkedResourceIn(web_url="https://example.com", display_name="test")
        assert "example.com" in str(lr.web_url)

    def test_attachment_out_schema(self):
        from app.schemas import AttachmentOut
        att = AttachmentOut(
            id=uuid.uuid4(), task_id=uuid.uuid4(), ms_id=None,
            name="file.txt", content_type="text/plain",
            size_bytes=100, reference_url=None, sync_status="synced",
            created_at=_now(),
        )
        assert att.name == "file.txt"
