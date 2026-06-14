"""
Contract tests for completion/uncomplete PATCH payload (ADR 2026-06-14 §3, ticket 5f1666ce).

WHY THIS FILE EXISTS (ADR §5 — "mock passed, real Graph 400"):
The pre-existing completion test mocked `_try_push_task` *wholesale*, so the outbound
PATCH payload it builds was never inspected — the prod bug (full-payload PATCH with
recurrence → Graph HTTP 400 on recurring completion) slipped through 229 green tests.
These tests mock one level deeper — at the `graph_client.update_task` HTTP boundary —
and assert the payload that ACTUALLY reaches Graph is status-only.

Empirical basis: ticket 4c3bfed0 (direct Graph REST) — minimal {"status":"completed"}
PATCH → 200; full payload with recurrence/dueDateTime → 400.

Run: pytest tests/test_completion_patch_contract.py -v
"""
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_task(*, status="notStarted", recurrence=None, due_datetime=None,
               due_date=None, completed_datetime=None, ms_id="task-graph-id-AQMK..."):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.ms_id = ms_id
    t.list_id = uuid.uuid4()
    t.title = "Weekly ritual"
    t.body = None
    t.body_content_type = "text"
    t.importance = "normal"
    t.status = status
    t.due_date = due_date
    t.due_datetime = due_datetime
    t.due_timezone = "UTC"
    t.start_datetime = None
    t.start_timezone = None
    t.reminder_datetime = None
    t.is_reminder_on = False
    t.categories = []
    t.recurrence = recurrence
    t.completed_datetime = completed_datetime
    return t


_RECUR = {
    "pattern": {"type": "daily", "interval": 1},
    "range": {"type": "noEnd", "startDate": "2026-06-14"},
}
_FORBIDDEN = ("recurrence", "dueDateTime", "title", "importance", "body", "startDateTime")


# ── 1. Payload builder: completion is status-only ──────────────────────────

class TestCompletionPayloadBuilder:
    def test_completed_payload_is_status_only(self):
        from app.services.task_service import _completion_patch_payload
        task = _make_task(
            status="completed",
            recurrence=_RECUR,
            due_datetime=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
            completed_datetime=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
        )
        payload = _completion_patch_payload(task)
        assert payload["status"] == "completed"
        assert "completedDateTime" in payload
        for k in _FORBIDDEN:
            assert k not in payload, f"completion payload must not contain {k!r}, got {payload!r}"

    def test_uncomplete_payload_is_status_only(self):
        from app.services.task_service import _completion_patch_payload
        task = _make_task(status="notStarted", recurrence=_RECUR,
                          due_datetime=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc))
        payload = _completion_patch_payload(task)
        assert payload["status"] == "notStarted"
        for k in _FORBIDDEN:
            assert k not in payload, f"uncomplete payload must not contain {k!r}, got {payload!r}"

    def test_naive_completed_datetime_made_aware(self):
        from app.services.task_service import _completion_patch_payload
        task = _make_task(status="completed",
                          completed_datetime=datetime(2026, 6, 14, 12, 0))  # naive
        payload = _completion_patch_payload(task)
        assert payload["completedDateTime"]["timeZone"] == "UTC"


# ── 2. Regression guard: full payload (old behavior) WOULD carry recurrence ──

class TestFullPayloadCarriesRecurrence:
    """Documents the bug class: the generic payload (what completion used to send) includes
    recurrence — exactly what Graph rejects on a recurring completion PATCH."""

    def test_full_payload_includes_recurrence(self):
        from app.services.task_service import _task_to_graph_payload
        task = _make_task(status="completed", recurrence=_RECUR,
                          due_datetime=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc))
        full = _task_to_graph_payload(task)
        assert "recurrence" in full and "dueDateTime" in full  # <- would 400 on completion


# ── 3. Boundary contract: payload reaching graph_client.update_task is status-only ──

class TestGraphBoundaryReceivesStatusOnly:
    @pytest.mark.asyncio
    async def test_recurring_completion_patch_excludes_recurrence_at_boundary(self):
        from app.services import task_service
        from app.services.task_service import _try_push_task, _completion_patch_payload

        task = _make_task(
            status="completed", recurrence=_RECUR,
            due_datetime=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
            completed_datetime=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
        )
        captured = {}

        def _fake_update(list_ms_id, ms_id, data):
            captured["data"] = data
            return {"id": task.ms_id, "status": "notStarted"}  # Graph 200, auto-advanced

        with patch.object(task_service.graph_client, "update_task", side_effect=_fake_update):
            ok = await _try_push_task(
                task, "list-ms-id", "update",
                payload_override=_completion_patch_payload(task),
            )

        assert ok is True
        data = captured["data"]
        assert data["status"] == "completed"
        for k in _FORBIDDEN:
            assert k not in data, f"PATCH reaching Graph must not contain {k!r} (→ HTTP 400), got {data!r}"


# ── 4. End-to-end wiring: complete_task() routes through status-only payload ──

class TestCompleteTaskWiringEndToEnd:
    """Guards the wiring regression that originally slipped: complete_task must send the
    status-only payload (not _task_to_graph_payload) to the Graph boundary."""

    @pytest.mark.asyncio
    async def test_complete_task_sends_status_only_to_graph(self):
        from app.services import task_service

        task = _make_task(
            status="notStarted", recurrence=_RECUR, due_date=date(2026, 6, 16),
            due_datetime=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
        )
        task_list = MagicMock()
        task_list.ms_id = "list-ms-id"
        res_task = MagicMock()
        res_task.scalar_one_or_none.return_value = task
        res_list = MagicMock()
        res_list.scalar_one_or_none.return_value = task_list

        db = MagicMock()
        db.execute = AsyncMock(side_effect=[res_task, res_list])
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        captured = {}

        def _fake_update(list_ms_id, ms_id, data):
            captured["data"] = data
            return {"id": task.ms_id, "status": "notStarted"}

        with patch.object(task_service.graph_client, "update_task", side_effect=_fake_update):
            await task_service.complete_task(db, task.id)

        data = captured["data"]
        assert data["status"] == "completed"
        for k in _FORBIDDEN:
            assert k not in data, f"complete_task sent {k!r} to Graph (→ HTTP 400 on recurring), got {data!r}"
