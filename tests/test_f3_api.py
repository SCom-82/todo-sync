"""
Tests for F3.3 API endpoint — POST /lists/{list_id}/share.

Run: pytest tests/test_f3_api.py -v
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_task_list(list_id=None, ms_id="ms-list-123", is_owner=True):
    tl = MagicMock()
    tl.id = list_id or uuid.uuid4()
    tl.ms_id = ms_id
    tl.display_name = "My List"
    tl.is_owner = is_owner
    tl.is_shared = False
    tl.deleted_at = None
    tl.wellknown_list_name = None
    tl.sync_status = "synced"
    tl.created_at = datetime.now(timezone.utc)
    tl.updated_at = datetime.now(timezone.utc)
    return tl


# ─────────────────────────────────────────────
# F3.3 — share endpoint validation
# ─────────────────────────────────────────────

class TestShareListValidation:
    def test_invalid_email_rejected(self):
        """ShareListIn with invalid email → pydantic ValidationError."""
        from pydantic import ValidationError
        from app.schemas import ShareListIn
        with pytest.raises(ValidationError):
            ShareListIn(email="not-email", permission="readwrite")

    def test_invalid_permission_rejected(self):
        from pydantic import ValidationError
        from app.schemas import ShareListIn
        with pytest.raises(ValidationError):
            ShareListIn(email="valid@example.com", permission="owner")

    def test_valid_share_request(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="alice@example.com", permission="readwrite")
        assert str(s.email) == "alice@example.com"
        assert s.permission == "readwrite"

    def test_read_permission_valid(self):
        from app.schemas import ShareListIn
        s = ShareListIn(email="bob@test.com", permission="read")
        assert s.permission == "read"


# ─────────────────────────────────────────────
# F3.3 — graph_client.share_list method
# ─────────────────────────────────────────────

class TestGraphClientShareList:
    def test_share_list_method_exists(self):
        from app.services.graph_client import graph_client
        assert hasattr(graph_client, "share_list")
        assert callable(graph_client.share_list)

    @pytest.mark.asyncio
    async def test_share_list_calls_correct_endpoint(self):
        from app.services.graph_client import graph_client

        captured = {}

        async def mock_request(method, url, json_body=None, params=None):
            captured["method"] = method
            captured["url"] = url
            captured["json_body"] = json_body
            return {"id": "member-id-123", "displayName": "alice@example.com"}

        with patch.object(graph_client, "_request", side_effect=mock_request):
            result = await graph_client.share_list("ms-list-id", "alice@example.com", "readwrite")

        assert captured["method"] == "POST"
        assert "ms-list-id/members" in captured["url"]
        assert captured["json_body"]["displayName"] == "alice@example.com"
        assert captured["json_body"]["sharedWithUserPermission"] == "readwrite"
        assert result["id"] == "member-id-123"

    @pytest.mark.asyncio
    async def test_share_list_read_permission(self):
        from app.services.graph_client import graph_client

        captured = {}

        async def mock_request(method, url, json_body=None, params=None):
            captured["json_body"] = json_body
            return {"id": "member-id-456"}

        with patch.object(graph_client, "_request", side_effect=mock_request):
            await graph_client.share_list("ms-list-id", "bob@example.com", "read")

        assert captured["json_body"]["sharedWithUserPermission"] == "read"

    @pytest.mark.asyncio
    async def test_share_list_propagates_http_error(self):
        """graph_client.share_list should propagate HTTPStatusError to caller."""
        from app.services.graph_client import graph_client

        mock_response = MagicMock()
        mock_response.status_code = 403

        async def mock_request(method, url, json_body=None, params=None):
            raise httpx.HTTPStatusError(
                "403 Forbidden", request=MagicMock(), response=mock_response
            )

        with patch.object(graph_client, "_request", side_effect=mock_request):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await graph_client.share_list("ms-list-id", "user@example.com", "readwrite")
            assert exc_info.value.response.status_code == 403


# ─────────────────────────────────────────────
# F3.3 — service layer error mapping
# ─────────────────────────────────────────────

class TestShareEndpointErrorHandling:
    @pytest.mark.asyncio
    async def test_404_when_list_not_found_in_db(self):
        """If list not in local DB → 404."""
        from app.api.task_lists import share_task_list
        from app.schemas import ShareListIn
        from fastapi import HTTPException

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result_mock)

        data = ShareListIn(email="user@example.com", permission="readwrite")

        with pytest.raises(HTTPException) as exc_info:
            await share_task_list(uuid.uuid4(), data, db)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_400_when_list_has_no_ms_id(self):
        """If list has no ms_id (never synced) → 400."""
        from app.api.task_lists import share_task_list
        from app.schemas import ShareListIn
        from fastapi import HTTPException

        tl = _make_task_list(ms_id=None)
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = tl
        db.execute = AsyncMock(return_value=result_mock)

        data = ShareListIn(email="user@example.com", permission="readwrite")

        with pytest.raises(HTTPException) as exc_info:
            await share_task_list(tl.id, data, db)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_403_from_graph_becomes_403(self):
        """Graph 403 → our API 403."""
        from app.api.task_lists import share_task_list
        from app.schemas import ShareListIn
        from app.services.graph_client import graph_client
        from fastapi import HTTPException

        tl = _make_task_list()
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = tl
        db.execute = AsyncMock(return_value=result_mock)

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        async def raise_403(*args, **kwargs):
            raise httpx.HTTPStatusError("403", request=MagicMock(), response=mock_response)

        data = ShareListIn(email="user@example.com", permission="readwrite")

        with patch.object(graph_client, "share_list", side_effect=raise_403):
            with pytest.raises(HTTPException) as exc_info:
                await share_task_list(tl.id, data, db)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_404_from_graph_becomes_404(self):
        """Graph 404 → our API 404."""
        from app.api.task_lists import share_task_list
        from app.schemas import ShareListIn
        from app.services.graph_client import graph_client
        from fastapi import HTTPException

        tl = _make_task_list()
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = tl
        db.execute = AsyncMock(return_value=result_mock)

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        async def raise_404(*args, **kwargs):
            raise httpx.HTTPStatusError("404", request=MagicMock(), response=mock_response)

        data = ShareListIn(email="user@example.com", permission="readwrite")

        with patch.object(graph_client, "share_list", side_effect=raise_404):
            with pytest.raises(HTTPException) as exc_info:
                await share_task_list(tl.id, data, db)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_502_for_other_graph_errors(self):
        """Graph 500 → our API 502."""
        from app.api.task_lists import share_task_list
        from app.schemas import ShareListIn
        from app.services.graph_client import graph_client
        from fastapi import HTTPException

        tl = _make_task_list()
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = tl
        db.execute = AsyncMock(return_value=result_mock)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        async def raise_500(*args, **kwargs):
            raise httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)

        data = ShareListIn(email="user@example.com", permission="readwrite")

        with patch.object(graph_client, "share_list", side_effect=raise_500):
            with pytest.raises(HTTPException) as exc_info:
                await share_task_list(tl.id, data, db)
            assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_success_returns_share_out(self):
        """Successful share returns ShareListOut with invited email."""
        from app.api.task_lists import share_task_list
        from app.schemas import ShareListIn
        from app.services.graph_client import graph_client

        tl = _make_task_list()
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = tl
        db.execute = AsyncMock(return_value=result_mock)

        graph_response = {"id": "member-id", "displayName": "user@example.com"}

        data = ShareListIn(email="user@example.com", permission="readwrite")

        with patch.object(graph_client, "share_list", return_value=graph_response):
            result = await share_task_list(tl.id, data, db)

        assert result.invited_user_email == "user@example.com"
        assert result.permission == "readwrite"
        assert result.raw == graph_response
