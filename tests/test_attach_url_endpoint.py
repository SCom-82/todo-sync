"""
Endpoint-level tests for POST /tasks/{id}/attachments/url.

QA condition (CONDITIONAL GO): verify that the HTTP endpoint
  (a) really calls Graph's linkedResources (POST mocked at httpx.AsyncClient.request),
  (b) returns LinkedResourceOut fields (display_name, web_url, application_name),
      NOT AttachmentOut fields (reference_url, size_bytes).

Mocks at HTTP boundary (httpx.AsyncClient.request) — consistent with ADR 0001 rule
and existing smoke tests in test_attach_url_smoke.py.

DB is mocked via FastAPI dependency_overrides (get_db → AsyncMock session).
This keeps the test focused on the endpoint routing and response serialisation,
not on DB internals (which are covered by linked_resource_service tests).

Mutation self-check: if someone replaces linked_resource_service.create() with
attachment_service.create_reference() in app/api/attachments.py:
  - test (a): no POST reaches Graph → captured_calls is empty → assertion fails.
  - test (b): response body has 'reference_url'/'size_bytes' but no 'display_name'/'web_url'
              → assertions on LinkedResourceOut fields fail.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

GRAPH_LR_GUID = "bb001122-3344-5566-7788-99aabbccddee"
LIST_MS_ID = "list-ms-ep-001"
TASK_MS_ID = "task-ms-ep-001"
TEST_URL = "https://github.com/org/repo/pull/42"
TEST_NAME = "PR #42"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_task_mock(task_id: uuid.UUID) -> MagicMock:
    """Build a minimal Task mock that the endpoint will accept."""
    from app.models import Task, TaskList
    task = MagicMock(spec=Task)
    task.id = task_id
    task.ms_id = TASK_MS_ID
    task.deleted_at = None
    task.list_id = uuid.uuid4()
    return task


def _make_task_list_mock(list_id: uuid.UUID) -> MagicMock:
    from app.models import TaskList
    tl = MagicMock(spec=TaskList)
    tl.id = list_id
    tl.ms_id = LIST_MS_ID
    return tl


def _make_db_mock(task_id: uuid.UUID) -> AsyncMock:
    """
    Build an AsyncMock DB session.

    The endpoint calls:  task = await db.get(Task, task_id)
    linked_resource_service.create() calls:
      - db.add(lr)
      - await db.flush()
      - await db.execute(select(Task)...) → task (to get ms_id)
      - await db.execute(select(TaskList)...) → task_list (to get list ms_id)
      - db.add(lr) again after updating ms_id/sync_status
      - await db.commit()
      - await db.refresh(lr)

    db.flush() populates server-generated fields (id, created_at, updated_at) on any
    LinkedResource that was added — simulating what a real SQLAlchemy flush does when
    it executes INSERT and populates default/server_default columns.

    db.refresh(obj) is a no-op in AsyncMock unless we give it a side_effect.
    Since flush already populated the fields, refresh just needs to be awaitable.
    """
    from app.models import LinkedResource

    task = _make_task_mock(task_id)
    task_list = _make_task_list_mock(task.list_id)

    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task

    list_result = MagicMock()
    list_result.scalar_one_or_none.return_value = task_list

    added_objects: list = []

    async def _flush_side_effect():
        """Simulate INSERT: populate id/created_at/updated_at on LinkedResource objects."""
        now = datetime.now(timezone.utc)
        for obj in added_objects:
            if isinstance(obj, LinkedResource):
                if obj.id is None:
                    obj.id = uuid.uuid4()
                if obj.created_at is None:
                    obj.created_at = now
                if obj.updated_at is None:
                    obj.updated_at = now

    db = AsyncMock()
    db.get = AsyncMock(return_value=task)
    db.execute = AsyncMock(side_effect=[task_result, list_result])
    db.flush = AsyncMock(side_effect=_flush_side_effect)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()  # no-op: fields already populated by flush

    db.add = MagicMock(side_effect=lambda obj: added_objects.append(obj))
    db._added = added_objects

    return db


def _graph_lr_201(guid: str, web_url: str, display_name: str) -> httpx.Response:
    body = {
        "@odata.type": "#microsoft.graph.linkedResource",
        "id": guid,
        "webUrl": web_url,
        "displayName": display_name,
        "applicationName": "todo-sync",
    }
    return httpx.Response(
        status_code=201,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request(
            "POST",
            f"https://graph.microsoft.com/v1.0/me/todo/lists/{LIST_MS_ID}/tasks/{TASK_MS_ID}/linkedResources",
        ),
    )


# ─────────────────────────────────────────────
# Fixture: TestClient with mocked DB session
# ─────────────────────────────────────────────

@pytest.fixture
def http_client_and_task_id():
    """
    Return (TestClient, task_id) with:
    - DB dependency overridden → AsyncMock session
    - Scheduler lifespan suppressed (no real DB connection needed)
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db

    task_id = uuid.uuid4()
    db_mock = _make_db_mock(task_id)

    @asynccontextmanager
    async def _no_lifespan(app):
        yield

    async def override_get_db():
        yield db_mock

    app.dependency_overrides[get_db] = override_get_db

    # Suppress scheduler/graph_client lifespan to avoid real connections
    with patch("app.main.start_scheduler"), patch("app.main.stop_scheduler"), \
         patch("app.main.graph_client"):
        client = TestClient(app, raise_server_exceptions=True)
        yield client, task_id

    app.dependency_overrides.clear()


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

class TestAttachUrlEndpoint:
    """
    HTTP-layer tests for POST /tasks/{id}/attachments/url.

    These tests verify endpoint routing and response serialisation — not DB internals.
    DB is mocked; Graph HTTP is mocked at httpx.AsyncClient.request boundary.
    """

    def test_ep_posts_to_graph_linkedresources(self, http_client_and_task_id):
        """(a) Endpoint sends POST to Graph /linkedResources.

        Mutation guard: replace linked_resource_service.create() with
        create_reference() in app/api/attachments.py → no HTTP call to Graph →
        captured_calls stays empty → this test goes red.
        """
        client, task_id = http_client_and_task_id
        captured_calls = []

        def mock_httpx(method, url, **kwargs):
            captured_calls.append({"method": method, "url": str(url)})
            return _graph_lr_201(GRAPH_LR_GUID, TEST_URL, TEST_NAME)

        with patch("httpx.AsyncClient.request", side_effect=mock_httpx):
            with patch(
                "app.services.graph_client.auth_service.get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ):
                resp = client.post(
                    f"/api/v1/tasks/{task_id}/attachments/url",
                    params={"url": TEST_URL, "name": TEST_NAME},
                )

        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"

        assert len(captured_calls) >= 1, (
            "No HTTP call was made to Graph. "
            "Mutation guard: endpoint is calling create_reference() instead of "
            "linked_resource_service.create(). Restore the correct call."
        )
        lr_call = next(
            (c for c in captured_calls if "linkedResources" in c["url"]),
            None,
        )
        assert lr_call is not None, (
            f"No POST to /linkedResources found. Calls made: {captured_calls}. "
            "Endpoint must route through linked_resource_service.create()."
        )
        assert lr_call["method"] == "POST", (
            f"Expected POST, got {lr_call['method']!r}"
        )

    def test_ep_returns_linkedresourceout_not_attachmentout(self, http_client_and_task_id):
        """(b) Response body has LinkedResourceOut fields, NOT AttachmentOut fields.

        LinkedResourceOut: display_name, web_url, application_name
        AttachmentOut:     reference_url, size_bytes  (no display_name / web_url)

        Mutation guard: replace linked_resource_service.create() with
        create_reference() in app/api/attachments.py →
          - response_model=LinkedResourceOut can't serialize AttachmentOut
          - 'display_name' and 'web_url' absent from body → assertions fail.
        """
        client, task_id = http_client_and_task_id

        with patch("httpx.AsyncClient.request", side_effect=lambda m, u, **kw: _graph_lr_201(GRAPH_LR_GUID, TEST_URL, TEST_NAME)):
            with patch(
                "app.services.graph_client.auth_service.get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ):
                resp = client.post(
                    f"/api/v1/tasks/{task_id}/attachments/url",
                    params={"url": TEST_URL, "name": TEST_NAME},
                )

        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        body = resp.json()

        # LinkedResourceOut fields MUST be present
        for field in ("display_name", "web_url", "application_name"):
            assert field in body, (
                f"'{field}' missing from response — endpoint returned AttachmentOut "
                f"instead of LinkedResourceOut. Body keys: {list(body.keys())}. "
                "Mutation guard: restore linked_resource_service.create() call."
            )

        # AttachmentOut-only fields must NOT appear
        for bad_field in ("reference_url", "size_bytes"):
            assert bad_field not in body, (
                f"'{bad_field}' found in response — endpoint is using create_reference() "
                f"path (AttachmentOut). Body: {body}. Mutation guard triggered."
            )

        # Field values
        assert body["web_url"] == TEST_URL, (
            f"web_url mismatch: expected {TEST_URL!r}, got {body['web_url']!r}"
        )
        assert body["display_name"] == TEST_NAME, (
            f"display_name mismatch: expected {TEST_NAME!r}, got {body['display_name']!r}"
        )
        assert body["application_name"] == "todo-sync", (
            f"application_name mismatch: expected 'todo-sync', got {body['application_name']!r}"
        )

    def test_ep_404_for_nonexistent_task(self, http_client_and_task_id):
        """Endpoint returns 404 when task_id is not found in DB.

        db.get returns None → 404, no Graph call made.
        """
        client, _ = http_client_and_task_id

        # Override: return None for db.get (task does not exist)
        from app.database import get_db

        async def get_db_none():
            db = AsyncMock()
            db.get = AsyncMock(return_value=None)
            yield db

        from app.main import app
        app.dependency_overrides[get_db] = get_db_none

        nonexistent = str(uuid.uuid4())
        with patch("httpx.AsyncClient.request") as mock_http:
            resp = client.post(
                f"/api/v1/tasks/{nonexistent}/attachments/url",
                params={"url": TEST_URL},
            )

        assert resp.status_code == 404, (
            f"Expected 404 for nonexistent task, got {resp.status_code}: {resp.text}"
        )
        mock_http.assert_not_called()
