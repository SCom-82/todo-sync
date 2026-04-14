"""
Tests for F2.1-F2.6 schema and model logic (unit, no DB).

Run: pytest tests/test_f2_schemas.py -v
"""
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import MAX_ATTACHMENT_BYTES
from app.schemas import (
    AttachmentContentOut,
    AttachmentOut,
    LinkedResourceIn,
    LinkedResourceOut,
    LinkedResourceUpdate,
)


# ─────────────────────────────────────────────
# F2.1 – LinkedResource schemas
# ─────────────────────────────────────────────

class TestLinkedResourceIn:
    def test_valid_https_url(self):
        lr = LinkedResourceIn(
            web_url="https://github.com/SCom-82/todo-sync/pull/42",
            display_name="PR #42",
        )
        assert str(lr.web_url).startswith("https://")
        assert lr.display_name == "PR #42"

    def test_valid_http_url(self):
        lr = LinkedResourceIn(
            web_url="http://example.com/page",
            display_name="Example",
        )
        assert lr.display_name == "Example"

    def test_invalid_url_raises_422(self):
        with pytest.raises(ValidationError) as exc_info:
            LinkedResourceIn(
                web_url="not-a-url",
                display_name="bad",
            )
        errors = exc_info.value.errors()
        assert any(e["type"] in ("url_parsing", "value_error", "url_scheme") for e in errors)

    def test_missing_display_name_raises(self):
        with pytest.raises(ValidationError):
            LinkedResourceIn(web_url="https://example.com")

    def test_optional_fields_default_none(self):
        lr = LinkedResourceIn(
            web_url="https://example.com",
            display_name="Test",
        )
        assert lr.application_name is None
        assert lr.external_id is None

    def test_application_name_and_external_id(self):
        lr = LinkedResourceIn(
            web_url="https://notion.so/page",
            display_name="Notion page",
            application_name="Notion",
            external_id="page-abc-123",
        )
        assert lr.application_name == "Notion"
        assert lr.external_id == "page-abc-123"

    def test_update_partial(self):
        u = LinkedResourceUpdate(display_name="Renamed")
        assert u.display_name == "Renamed"
        assert u.web_url is None
        assert u.application_name is None

    def test_update_url_invalid_raises(self):
        with pytest.raises(ValidationError):
            LinkedResourceUpdate(web_url="garbage")

    def test_update_url_valid(self):
        u = LinkedResourceUpdate(web_url="https://github.com/new")
        assert u.web_url is not None

    def test_out_schema_from_orm(self):
        """LinkedResourceOut can be constructed from dict (simulating ORM)."""
        now = datetime.now(timezone.utc)
        data = {
            "id": uuid.uuid4(),
            "task_id": uuid.uuid4(),
            "ms_id": "graph-lr-123",
            "web_url": "https://github.com",
            "display_name": "GitHub",
            "application_name": None,
            "external_id": None,
            "sync_status": "synced",
            "created_at": now,
            "updated_at": now,
        }
        out = LinkedResourceOut(**data)
        assert out.sync_status == "synced"
        assert out.web_url == "https://github.com"


# ─────────────────────────────────────────────
# F2.2 – Attachment schemas
# ─────────────────────────────────────────────

class TestAttachmentSchemas:
    def test_attachment_out_fields(self):
        now = datetime.now(timezone.utc)
        att = AttachmentOut(
            id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            name="report.pdf",
            content_type="application/pdf",
            size_bytes=1024 * 1024,
            sync_status="pending",
            created_at=now,
        )
        assert att.name == "report.pdf"
        assert att.size_bytes == 1024 * 1024
        assert att.content_base64 is None if hasattr(att, "content_base64") else True

    def test_attachment_content_out_has_base64(self):
        now = datetime.now(timezone.utc)
        att = AttachmentContentOut(
            id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            name="img.png",
            sync_status="synced",
            created_at=now,
            content_base64="aGVsbG8=",
        )
        assert att.content_base64 == "aGVsbG8="

    def test_max_attachment_bytes_constant(self):
        """Hard limit must be exactly 3 MB."""
        assert MAX_ATTACHMENT_BYTES == 3 * 1024 * 1024

    def test_attachment_reference_url(self):
        now = datetime.now(timezone.utc)
        att = AttachmentOut(
            id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            name="link",
            sync_status="synced",
            created_at=now,
            reference_url="https://drive.google.com/file/123",
        )
        assert att.reference_url == "https://drive.google.com/file/123"
        assert att.content_type is None
        assert att.size_bytes is None
