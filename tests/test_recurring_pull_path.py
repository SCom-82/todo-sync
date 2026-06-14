"""
Tests for recurring pull-path fixes (ADR 2026-06-14, ticket b).

Covers:
1. Auto-advance NOT treated as uncomplete:
   - complete_task on recurring sets last_completed_occurrence_date and keeps pending_push
   - pull of same ms_id with notStarted + dueDate shifted FORWARD → accepted as series roll-forward,
     sync_status=synced, intent marker cleared
2. Completed-sibling (branch B) ingested as separate completed task:
   - new ms_id arriving with status=completed in delta → new Task created, not deduped with series
3. Real uncomplete works correctly (notStarted + dueDate NOT shifted forward):
   - pull of same ms_id with notStarted + same/earlier dueDate → accepted as genuine uncomplete,
     intent marker cleared
4. Non-recurring tasks are unaffected by recurring guard logic

Run: pytest tests/test_recurring_pull_path.py -v
"""
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

DAILY_RECURRENCE = {
    "pattern": {"type": "daily", "interval": 1},
    "range": {"type": "noEnd", "startDate": "2026-06-14"},
}


def _make_task_list(ms_id="list-ms-1"):
    tl = MagicMock()
    tl.id = uuid.uuid4()
    tl.ms_id = ms_id
    tl.deleted_at = None
    return tl


def _make_existing_task(
    *,
    ms_id: str = "task-series-ms-id",
    status: str = "completed",
    sync_status: str = "pending_push",
    due_date: date | None = date(2026, 6, 14),
    recurrence: dict | None = None,
    last_completed_occurrence_date: date | None = None,
    local_modified_at: datetime | None = None,
    updated_at: datetime | None = None,
):
    """Create a MagicMock simulating an existing Task row."""
    t = MagicMock()
    t.id = uuid.uuid4()
    t.ms_id = ms_id
    t.list_id = uuid.uuid4()
    t.deleted_at = None
    t.sync_status = sync_status
    t.status = status
    t.due_date = due_date
    t.due_datetime = None
    t.due_timezone = "UTC"
    t.recurrence = recurrence or DAILY_RECURRENCE
    t.last_completed_occurrence_date = last_completed_occurrence_date
    t.local_modified_at = local_modified_at or datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
    t.updated_at = updated_at or datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
    t.title = "Daily standup"
    t.checklist_items = []
    t.completed_datetime = None
    return t


def _graph_task_item(
    *,
    ms_id: str = "task-series-ms-id",
    status: str = "notStarted",
    due_date_str: str | None = None,
    modified_str: str = "2026-06-14T14:00:00Z",
    recurrence: dict | None = None,
):
    """Build a Graph delta item dict for a task."""
    item: dict = {
        "id": ms_id,
        "title": "Daily standup",
        "status": status,
        "importance": "normal",
        "isReminderOn": False,
        "categories": [],
        "lastModifiedDateTime": modified_str,
    }
    if recurrence is not None:
        item["recurrence"] = recurrence
    else:
        item["recurrence"] = DAILY_RECURRENCE
    if due_date_str:
        item["dueDateTime"] = {
            "dateTime": f"{due_date_str}T07:00:00",
            "timeZone": "UTC",
        }
    return item


def _make_sync_state():
    state = MagicMock()
    state.delta_link = "https://graph.microsoft.com/v1.0/delta?token=old"
    state.delta_syncs_total = 0
    state.delta_syncs_succeeded = 0
    state.delta_full_resets_total = 0
    state.last_sync_at = None
    state.last_sync_status = None
    return state


async def _run_pull_tasks_for_list(
    existing_task,
    delta_items: list[dict],
    delta_link_out: str = "https://graph.microsoft.com/v1.0/delta?token=new",
):
    """
    Helper: run pull_tasks_for_list with mocked DB and Graph client.
    Returns the existing_task (mutated in place) and added list.
    """
    from app.services import sync_service

    task_list = _make_task_list()
    sync_state = _make_sync_state()

    delta_result = {"value": delta_items, "delta_link": delta_link_out}

    db = AsyncMock()
    db.flush = AsyncMock()
    added = []
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))

    call_count = 0

    async def mock_execute(query):
        nonlocal call_count
        r = MagicMock()
        call_count += 1
        if call_count == 1:
            # _get_or_create_sync_state
            r.scalar_one_or_none.return_value = sync_state
        elif call_count == 2:
            # select(Task).where(Task.ms_id == ms_id)
            r.scalar_one_or_none.return_value = existing_task
        else:
            # checklist, linkedResources, attachments queries → empty
            r.scalar_one_or_none.return_value = None
            scalars_mock = MagicMock()
            scalars_mock.all.return_value = []
            r.scalars.return_value = scalars_mock
        return r

    db.execute = mock_execute

    with patch.object(sync_service.graph_client, "get_tasks_delta", return_value=delta_result), \
         patch.object(sync_service.graph_client, "get_checklist_items", return_value=[]), \
         patch.object(sync_service.graph_client, "list_linked_resources", return_value=[]), \
         patch.object(sync_service.graph_client, "list_attachments", return_value=[]):
        await sync_service.pull_tasks_for_list(db, task_list)

    return existing_task, added


async def _run_pull_new_task(
    delta_items: list[dict],
    delta_link_out: str = "https://graph.microsoft.com/v1.0/delta?token=new",
):
    """
    Helper: run pull_tasks_for_list where existing task does NOT exist (new task scenario).
    Returns (existing=None, added list).
    """
    from app.services import sync_service

    task_list = _make_task_list()
    sync_state = _make_sync_state()

    delta_result = {"value": delta_items, "delta_link": delta_link_out}

    db = AsyncMock()
    db.flush = AsyncMock()
    added = []
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))

    call_count = 0

    async def mock_execute(query):
        nonlocal call_count
        r = MagicMock()
        call_count += 1
        if call_count == 1:
            r.scalar_one_or_none.return_value = sync_state
        elif call_count == 2:
            # No existing task
            r.scalar_one_or_none.return_value = None
        else:
            r.scalar_one_or_none.return_value = None
            scalars_mock = MagicMock()
            scalars_mock.all.return_value = []
            r.scalars.return_value = scalars_mock
        return r

    db.execute = mock_execute

    with patch.object(sync_service.graph_client, "get_tasks_delta", return_value=delta_result), \
         patch.object(sync_service.graph_client, "get_checklist_items", return_value=[]), \
         patch.object(sync_service.graph_client, "list_linked_resources", return_value=[]), \
         patch.object(sync_service.graph_client, "list_attachments", return_value=[]):
        await sync_service.pull_tasks_for_list(db, task_list)

    return None, added


# ─────────────────────────────────────────────
# 1. Auto-advance NOT treated as uncomplete (ADR §2, branch A)
# ─────────────────────────────────────────────

class TestRecurringAutoAdvanceNotUncomplete:
    """
    Scenario: we completed a recurring task (occurrence 2026-06-14).
    Graph rolled the series forward to 2026-06-15. Pull sees the same ms_id
    in notStarted with dueDate=2026-06-15 (> last_completed_occurrence_date=2026-06-14).
    Expected: accepted as roll-forward, sync_status=synced, intent marker cleared.
    """

    @pytest.mark.asyncio
    async def test_auto_advance_accepted_not_uncomplete(self):
        existing = _make_existing_task(
            ms_id="task-series-ms-id",
            status="completed",
            sync_status="pending_push",
            due_date=date(2026, 6, 14),
            last_completed_occurrence_date=date(2026, 6, 14),
        )

        # Graph returns same ms_id, notStarted, due_date shifted to 2026-06-15
        graph_item = _graph_task_item(
            ms_id="task-series-ms-id",
            status="notStarted",
            due_date_str="2026-06-15",
            modified_str="2026-06-14T15:00:00Z",
        )

        existing_after, added = await _run_pull_tasks_for_list(existing, [graph_item])

        # Must accept new dueDate and notStarted (auto-advance)
        assert existing_after.status == "notStarted"
        assert existing_after.due_date == date(2026, 6, 15)
        # sync_status must become synced (series rolled forward, conflict resolved)
        assert existing_after.sync_status == "synced"
        # Intent marker must be cleared after advance accepted
        assert existing_after.last_completed_occurrence_date is None
        # No new tasks added (this is not a completed-sibling scenario)
        assert len(added) == 0

    @pytest.mark.asyncio
    async def test_non_recurring_uncomplete_not_blocked(self):
        """Non-recurring task with pending_push and incoming notStarted must NOT be
        blocked by recurring guard — the guard only applies when recurrence IS NOT NULL."""
        existing = _make_existing_task(
            ms_id="task-nonrec-ms-id",
            status="completed",
            sync_status="pending_push",
            due_date=date(2026, 6, 14),
            recurrence=None,  # non-recurring
            last_completed_occurrence_date=None,
        )

        graph_item = _graph_task_item(
            ms_id="task-nonrec-ms-id",
            status="notStarted",
            due_date_str="2026-06-14",
            modified_str="2026-06-14T16:00:00Z",
            recurrence=None,
        )
        # Remove recurrence from item too
        graph_item.pop("recurrence", None)

        existing_after, added = await _run_pull_tasks_for_list(existing, [graph_item])

        # Non-recurring: guard should not interfere.
        # Since local_modified_at (12:00) < ms_modified (16:00), the conflict skip
        # condition (local > ms_modified) is False → falls through to setattr.
        # status should be updated to notStarted from Graph.
        assert existing_after.status == "notStarted"


# ─────────────────────────────────────────────
# 2. Completed-sibling (branch B) ingested as separate task (ADR §2b)
# ─────────────────────────────────────────────

class TestCompletedSiblingIngest:
    """
    Scenario: Graph spawns a new ms_id (completed-sibling) when recurring series is completed.
    This new id arrives in delta with status=completed.
    Expected: ingested as a new separate Task row — not deduped with the series.
    """

    @pytest.mark.asyncio
    async def test_completed_sibling_creates_new_task(self):
        """New ms_id with status=completed (Graph sibling B) → new Task created."""
        sibling_ms_id = "task-sibling-NEWID-ms-id"
        graph_item = {
            "id": sibling_ms_id,
            "title": "Daily standup",
            "status": "completed",
            "importance": "normal",
            "isReminderOn": False,
            "categories": [],
            "lastModifiedDateTime": "2026-06-14T15:30:00Z",
            "completedDateTime": {"dateTime": "2026-06-14T14:00:00Z", "timeZone": "UTC"},
            "dueDateTime": {"dateTime": "2026-06-14T07:00:00Z", "timeZone": "UTC"},
            "recurrence": DAILY_RECURRENCE,
        }

        _, added = await _run_pull_new_task([graph_item])

        # A new Task object must have been added to db
        assert len(added) >= 1
        new_task = added[0]
        assert new_task.ms_id == sibling_ms_id
        assert new_task.sync_status == "synced"

    @pytest.mark.asyncio
    async def test_completed_sibling_does_not_dedup_with_series(self):
        """
        Sibling ms_id is different from series ms_id — they must be separate rows.
        The series task stays as-is; sibling creates a new row.
        """
        series_ms_id = "task-series-ms-id"
        sibling_ms_id = "task-sibling-DIFFERENT-ms-id"

        # Series task exists with pending_push
        existing = _make_existing_task(
            ms_id=series_ms_id,
            status="completed",
            sync_status="pending_push",
            due_date=date(2026, 6, 14),
            last_completed_occurrence_date=date(2026, 6, 14),
        )

        # Delta has TWO items: series auto-advance + sibling
        series_item = _graph_task_item(
            ms_id=series_ms_id,
            status="notStarted",
            due_date_str="2026-06-15",
            modified_str="2026-06-14T15:00:00Z",
        )
        sibling_item = {
            "id": sibling_ms_id,
            "title": "Daily standup",
            "status": "completed",
            "importance": "normal",
            "isReminderOn": False,
            "categories": [],
            "lastModifiedDateTime": "2026-06-14T15:00:00Z",
            "completedDateTime": {"dateTime": "2026-06-14T14:00:00Z", "timeZone": "UTC"},
            "dueDateTime": {"dateTime": "2026-06-14T07:00:00Z", "timeZone": "UTC"},
            "recurrence": DAILY_RECURRENCE,
        }

        from app.services import sync_service

        task_list = _make_task_list()
        sync_state = _make_sync_state()

        delta_result = {"value": [series_item, sibling_item], "delta_link": "...new..."}
        db = AsyncMock()
        db.flush = AsyncMock()
        added = []
        db.add = MagicMock(side_effect=lambda obj: added.append(obj))

        call_count = 0

        async def mock_execute(query):
            nonlocal call_count
            r = MagicMock()
            call_count += 1
            if call_count == 1:
                # sync_state
                r.scalar_one_or_none.return_value = sync_state
            elif call_count == 2:
                # first task lookup → series exists
                r.scalar_one_or_none.return_value = existing
            elif call_count == 3:
                # second task lookup → sibling doesn't exist yet
                r.scalar_one_or_none.return_value = None
            else:
                r.scalar_one_or_none.return_value = None
                scalars_mock = MagicMock()
                scalars_mock.all.return_value = []
                r.scalars.return_value = scalars_mock
            return r

        db.execute = mock_execute

        with patch.object(sync_service.graph_client, "get_tasks_delta", return_value=delta_result), \
             patch.object(sync_service.graph_client, "get_checklist_items", return_value=[]), \
             patch.object(sync_service.graph_client, "list_linked_resources", return_value=[]), \
             patch.object(sync_service.graph_client, "list_attachments", return_value=[]):
            await sync_service.pull_tasks_for_list(db, task_list)

        # Series was updated in place (auto-advance)
        assert existing.status == "notStarted"
        assert existing.due_date == date(2026, 6, 15)
        assert existing.sync_status == "synced"

        # Sibling was added as a NEW row — not the same object as existing
        sibling_rows = [t for t in added if getattr(t, "ms_id", None) == sibling_ms_id]
        assert len(sibling_rows) == 1
        assert sibling_rows[0].sync_status == "synced"


# ─────────────────────────────────────────────
# 3. Real uncomplete works correctly (ADR §2)
# ─────────────────────────────────────────────

class TestRealUncomplete:
    """
    Scenario: user removes the completion checkbox on a recurring task in another client.
    Graph sends the same ms_id in notStarted with dueDate NOT shifted forward (same date).
    Expected: accepted as genuine uncomplete, intent marker cleared.
    """

    @pytest.mark.asyncio
    async def test_real_uncomplete_accepted(self):
        existing = _make_existing_task(
            ms_id="task-series-ms-id",
            status="completed",
            sync_status="pending_push",
            due_date=date(2026, 6, 14),
            last_completed_occurrence_date=date(2026, 6, 14),
        )

        # Graph returns same ms_id, notStarted, due_date SAME (not shifted forward)
        graph_item = _graph_task_item(
            ms_id="task-series-ms-id",
            status="notStarted",
            due_date_str="2026-06-14",  # same date — real uncomplete
            modified_str="2026-06-14T16:00:00Z",
        )

        existing_after, added = await _run_pull_tasks_for_list(existing, [graph_item])

        # Must accept notStarted — real uncomplete from another client
        assert existing_after.status == "notStarted"
        # Intent marker must be cleared
        assert existing_after.last_completed_occurrence_date is None
        # No new sibling added
        assert len(added) == 0

    @pytest.mark.asyncio
    async def test_real_uncomplete_earlier_date_accepted(self):
        """Real uncomplete where dueDate is earlier than last_completed_occurrence_date."""
        existing = _make_existing_task(
            ms_id="task-series-ms-id",
            status="completed",
            sync_status="pending_push",
            due_date=date(2026, 6, 14),
            last_completed_occurrence_date=date(2026, 6, 14),
        )

        # Graph returns same ms_id, notStarted, due_date EARLIER (unusual edge case)
        graph_item = _graph_task_item(
            ms_id="task-series-ms-id",
            status="notStarted",
            due_date_str="2026-06-13",  # earlier — definitely not auto-advance
            modified_str="2026-06-14T16:00:00Z",
        )

        existing_after, _ = await _run_pull_tasks_for_list(existing, [graph_item])

        assert existing_after.status == "notStarted"
        assert existing_after.last_completed_occurrence_date is None


# ─────────────────────────────────────────────
# 4. complete_task sets intent marker and keeps pending_push (ADR §2 + §3)
# ─────────────────────────────────────────────

class TestCompleteTaskSetsIntentMarker:
    """
    complete_task for a recurring task must:
    - set last_completed_occurrence_date = task.due_date
    - keep sync_status = pending_push even after successful push (ADR §3)
    """

    @pytest.mark.asyncio
    async def test_complete_recurring_sets_intent_marker(self):
        from app.services import task_service

        task_id = uuid.uuid4()
        task = MagicMock()
        task.id = task_id
        task.ms_id = "task-series-ms-id"
        task.list_id = uuid.uuid4()
        task.status = "notStarted"
        task.sync_status = "synced"
        task.recurrence = DAILY_RECURRENCE
        task.due_date = date(2026, 6, 14)
        task.due_datetime = None
        task.due_timezone = "UTC"
        task.completed_datetime = None
        task.local_modified_at = None
        task.last_completed_occurrence_date = None
        task.title = "Daily standup"

        task_list = MagicMock()
        task_list.ms_id = "list-ms-id"
        task_list.id = task.list_id

        db = AsyncMock()

        call_count = 0

        async def mock_execute(query):
            nonlocal call_count
            r = MagicMock()
            call_count += 1
            if call_count == 1:
                r.scalar_one_or_none.return_value = task
            else:
                r.scalar_one_or_none.return_value = task_list
            return r

        db.execute = mock_execute
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        with patch.object(task_service, "_try_push_task", return_value=True) as mock_push:
            result = await task_service.complete_task(db, task_id)

        # Intent marker set to the occurrence dueDate
        assert task.last_completed_occurrence_date == date(2026, 6, 14)
        # Must stay pending_push after push (ADR §3)
        assert task.sync_status == "pending_push"
        # local_modified_at must be set
        assert task.local_modified_at is not None

    @pytest.mark.asyncio
    async def test_complete_non_recurring_becomes_synced_after_push(self):
        """Non-recurring tasks: after successful push they should become synced (unchanged behavior)."""
        from app.services import task_service

        task_id = uuid.uuid4()
        task = MagicMock()
        task.id = task_id
        task.ms_id = "task-nonrec-ms-id"
        task.list_id = uuid.uuid4()
        task.status = "notStarted"
        task.sync_status = "synced"
        task.recurrence = None  # non-recurring
        task.due_date = date(2026, 6, 14)
        task.due_datetime = None
        task.due_timezone = "UTC"
        task.completed_datetime = None
        task.local_modified_at = None
        task.last_completed_occurrence_date = None
        task.title = "One-off task"

        task_list = MagicMock()
        task_list.ms_id = "list-ms-id"
        task_list.id = task.list_id

        db = AsyncMock()

        call_count = 0

        async def mock_execute(query):
            nonlocal call_count
            r = MagicMock()
            call_count += 1
            if call_count == 1:
                r.scalar_one_or_none.return_value = task
            else:
                r.scalar_one_or_none.return_value = task_list
            return r

        db.execute = mock_execute
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        # Simulate _try_push_task: for non-recurring, it will set task.sync_status = "synced"
        # (this is done inside _try_push_task for successful push)
        async def fake_push(task_obj, list_ms_id, action):
            if action == "update":
                task_obj.sync_status = "synced"
            return True

        with patch.object(task_service, "_try_push_task", side_effect=fake_push):
            result = await task_service.complete_task(db, task_id)

        # Non-recurring: after push (which set synced), the §3 guard must NOT reset to pending_push
        assert task.sync_status == "synced"
        # Intent marker must NOT be set for non-recurring
        assert task.last_completed_occurrence_date is None
