"""
Tests for F1.1-F1.5 schema and service logic (unit, no DB, mocked Graph).

Run: pytest tests/test_f1_schemas.py -v
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas import (
    ChecklistItemCreate,
    ChecklistItemUpdate,
    PatternedRecurrence,
    RecurrencePattern,
    RecurrenceRange,
    TaskCreate,
    TaskUpdate,
)


# ─────────────────────────────────────────────
# F1.1 – List identifier validation
# ─────────────────────────────────────────────

class TestF11ListIdentifier:
    def test_task_create_with_list_id_ok(self):
        t = TaskCreate(list_id=uuid.uuid4(), title="t")
        assert t.list_id is not None
        assert t.list_name is None
        assert t.list_ms_id is None

    def test_task_create_with_list_name_ok(self):
        t = TaskCreate(list_name="dev-coder", title="t")
        assert t.list_name == "dev-coder"
        assert t.list_id is None

    def test_task_create_with_list_ms_id_ok(self):
        t = TaskCreate(list_ms_id="MSID-ABC", title="t")
        assert t.list_ms_id == "MSID-ABC"

    def test_task_create_no_list_raises(self):
        with pytest.raises(Exception):
            TaskCreate(title="t")

    def test_task_create_two_lists_raises(self):
        with pytest.raises(Exception):
            TaskCreate(list_id=uuid.uuid4(), list_name="dev-coder", title="t")

    def test_task_create_three_lists_raises(self):
        with pytest.raises(Exception):
            TaskCreate(list_id=uuid.uuid4(), list_name="dev-coder", list_ms_id="X", title="t")


# ─────────────────────────────────────────────
# F1.2 – DateTime fields
# ─────────────────────────────────────────────

class TestF12DateTime:
    def test_task_create_due_datetime(self):
        dt = datetime(2026, 4, 15, 15, 0, 0, tzinfo=timezone.utc)
        t = TaskCreate(list_id=uuid.uuid4(), title="t", due_datetime=dt, due_timezone="Europe/Moscow")
        assert t.due_datetime == dt
        assert t.due_timezone == "Europe/Moscow"

    def test_task_create_start_datetime(self):
        dt = datetime(2026, 4, 15, 9, 0, 0, tzinfo=timezone.utc)
        t = TaskCreate(list_id=uuid.uuid4(), title="t", start_datetime=dt, start_timezone="UTC")
        assert t.start_datetime == dt
        assert t.start_timezone == "UTC"

    def test_task_response_has_datetime_fields(self):
        from app.schemas import TaskResponse
        import inspect
        fields = TaskResponse.model_fields
        assert "due_datetime" in fields
        assert "start_datetime" in fields
        assert "start_timezone" in fields
        assert "due_timezone" in fields

    def test_task_update_due_datetime(self):
        dt = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        t = TaskUpdate(due_datetime=dt, due_timezone="Asia/Yekaterinburg")
        assert t.due_datetime == dt

    def test_graph_payload_uses_due_datetime(self):
        """_task_to_graph_payload должен использовать due_datetime если он есть."""
        from app.services.task_service import _task_to_graph_payload
        from app.models import Task

        task = MagicMock(spec=Task)
        task.title = "Test"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
        task.due_datetime = datetime(2026, 4, 15, 11, 0, 0, tzinfo=timezone.utc)
        task.due_timezone = "Europe/Moscow"
        task.due_date = None
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert "dueDateTime" in payload
        assert payload["dueDateTime"]["timeZone"] == "Europe/Moscow"
        assert "T" in payload["dueDateTime"]["dateTime"]

    def test_graph_payload_uses_start_datetime(self):
        from app.services.task_service import _task_to_graph_payload
        from app.models import Task

        task = MagicMock(spec=Task)
        task.title = "Test"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
        task.due_datetime = None
        task.due_date = None
        task.due_timezone = "UTC"
        task.start_datetime = datetime(2026, 4, 15, 9, 0, 0, tzinfo=timezone.utc)
        task.start_timezone = "Europe/Moscow"
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert "startDateTime" in payload
        assert payload["startDateTime"]["timeZone"] == "Europe/Moscow"

    def test_graph_payload_fallback_to_due_date(self):
        """Если due_datetime нет — берём due_date (backward compat)."""
        from app.services.task_service import _task_to_graph_payload
        from app.models import Task
        from datetime import date

        task = MagicMock(spec=Task)
        task.title = "Old"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
        task.due_datetime = None
        task.due_date = date(2026, 4, 15)
        task.due_timezone = "UTC"
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert "dueDateTime" in payload
        assert "2026-04-15" in payload["dueDateTime"]["dateTime"]


# ─────────────────────────────────────────────
# F1.3 – Body content type
# ─────────────────────────────────────────────

class TestF13BodyContentType:
    def test_default_is_text(self):
        t = TaskCreate(list_id=uuid.uuid4(), title="t")
        assert t.body_content_type == "text"

    def test_html_accepted(self):
        t = TaskCreate(list_id=uuid.uuid4(), title="t", body="<h1>x</h1>", body_content_type="html")
        assert t.body_content_type == "html"

    def test_invalid_content_type_rejected(self):
        with pytest.raises(Exception):
            TaskCreate(list_id=uuid.uuid4(), title="t", body_content_type="markdown")

    def test_graph_payload_includes_content_type(self):
        from app.services.task_service import _task_to_graph_payload
        from app.models import Task

        task = MagicMock(spec=Task)
        task.title = "HTML Task"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = "<h1>plan</h1>"
        task.body_content_type = "html"
        task.due_datetime = None
        task.due_date = None
        task.due_timezone = "UTC"
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == "<h1>plan</h1>"

    def test_graph_payload_text_content_type(self):
        from app.services.task_service import _task_to_graph_payload
        from app.models import Task

        task = MagicMock(spec=Task)
        task.title = "Text Task"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = "plain text"
        task.body_content_type = "text"
        task.due_datetime = None
        task.due_date = None
        task.due_timezone = "UTC"
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert payload["body"]["contentType"] == "text"

    def test_task_update_body_content_type(self):
        u = TaskUpdate(body="<p>x</p>", body_content_type="html")
        assert u.body_content_type == "html"

    def test_task_response_has_body_content_type(self):
        from app.schemas import TaskResponse
        fields = TaskResponse.model_fields
        assert "body_content_type" in fields


# ─────────────────────────────────────────────
# F1.4 – Recurrence write-path
# ─────────────────────────────────────────────

class TestF14Recurrence:
    def _weekly_rec(self):
        return PatternedRecurrence(
            pattern=RecurrencePattern(type="weekly", interval=1, daysOfWeek=["monday"]),
            range=RecurrenceRange(type="noEnd", startDate="2026-04-15"),
        )

    def _monthly_rec(self):
        return PatternedRecurrence(
            pattern=RecurrencePattern(type="absoluteMonthly", interval=1, dayOfMonth=1),
            range=RecurrenceRange(type="noEnd", startDate="2026-04-15"),
        )

    def test_weekly_recurrence_valid(self):
        rec = self._weekly_rec()
        assert rec.pattern.type == "weekly"
        assert rec.pattern.daysOfWeek == ["monday"]

    def test_monthly_recurrence_valid(self):
        rec = self._monthly_rec()
        assert rec.pattern.type == "absoluteMonthly"
        assert rec.pattern.dayOfMonth == 1

    def test_yearly_recurrence_valid(self):
        rec = PatternedRecurrence(
            pattern=RecurrencePattern(type="absoluteYearly", interval=1, dayOfMonth=1, month=4),
            range=RecurrenceRange(type="noEnd", startDate="2026-04-15"),
        )
        assert rec.pattern.type == "absoluteYearly"

    def test_weekly_without_days_raises(self):
        with pytest.raises(Exception):
            PatternedRecurrence(
                pattern=RecurrencePattern(type="weekly", interval=1),  # no daysOfWeek
                range=RecurrenceRange(type="noEnd", startDate="2026-04-15"),
            )

    def test_absolute_monthly_without_day_raises(self):
        with pytest.raises(Exception):
            PatternedRecurrence(
                pattern=RecurrencePattern(type="absoluteMonthly", interval=1),  # no dayOfMonth
                range=RecurrenceRange(type="noEnd", startDate="2026-04-15"),
            )

    def test_end_date_range_without_end_raises(self):
        with pytest.raises(Exception):
            PatternedRecurrence(
                pattern=RecurrencePattern(type="weekly", interval=1, daysOfWeek=["monday"]),
                range=RecurrenceRange(type="endDate", startDate="2026-04-15"),  # no endDate
            )

    def test_numbered_without_count_raises(self):
        with pytest.raises(Exception):
            PatternedRecurrence(
                pattern=RecurrencePattern(type="weekly", interval=1, daysOfWeek=["monday"]),
                range=RecurrenceRange(type="numbered", startDate="2026-04-15"),  # no numberOfOccurrences
            )

    def test_task_create_accepts_recurrence(self):
        t = TaskCreate(
            list_id=uuid.uuid4(),
            title="weekly standup",
            recurrence=self._weekly_rec(),
        )
        assert t.recurrence is not None
        assert t.recurrence.pattern.type == "weekly"

    def test_task_create_recurrence_none(self):
        t = TaskCreate(list_id=uuid.uuid4(), title="t")
        assert t.recurrence is None

    def test_graph_payload_includes_recurrence(self):
        from app.services.task_service import _task_to_graph_payload
        from app.models import Task

        rec_dict = {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday"]},
            "range": {"type": "noEnd", "startDate": "2026-04-15"},
        }

        task = MagicMock(spec=Task)
        task.title = "Weekly"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
        task.due_datetime = None
        task.due_date = None
        task.due_timezone = "UTC"
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = rec_dict

        payload = _task_to_graph_payload(task)
        assert "recurrence" in payload
        assert payload["recurrence"]["pattern"]["type"] == "weekly"
        assert payload["recurrence"]["range"]["type"] == "noEnd"

    def test_recurrence_serialization_roundtrip(self):
        """PatternedRecurrence → model_dump() → dict должен соответствовать Graph-схеме."""
        rec = self._weekly_rec()
        d = rec.model_dump()
        assert d["pattern"]["type"] == "weekly"
        assert d["pattern"]["daysOfWeek"] == ["monday"]
        assert d["range"]["type"] == "noEnd"
        assert d["range"]["startDate"] == "2026-04-15"


# ─────────────────────────────────────────────
# F1.5 – Checklist schemas
# ─────────────────────────────────────────────

class TestF15ChecklistSchemas:
    def test_checklist_item_create(self):
        c = ChecklistItemCreate(displayName="купить батарейки")
        assert c.displayName == "купить батарейки"
        assert c.isChecked is False

    def test_checklist_item_create_with_checked(self):
        c = ChecklistItemCreate(displayName="done", isChecked=True)
        assert c.isChecked is True

    def test_checklist_item_create_empty_name_raises(self):
        """displayName required — нельзя создать без него."""
        with pytest.raises(Exception):
            ChecklistItemCreate()

    def test_checklist_item_update_partial(self):
        u = ChecklistItemUpdate(isChecked=True)
        assert u.isChecked is True
        assert u.displayName is None

    def test_checklist_item_update_rename_only(self):
        u = ChecklistItemUpdate(displayName="новое название")
        assert u.displayName == "новое название"
        assert u.isChecked is None

    def test_checklist_item_update_empty_is_valid(self):
        """Пустой PATCH валиден — может использоваться как no-op."""
        u = ChecklistItemUpdate()
        assert u.displayName is None
        assert u.isChecked is None

    def test_checklist_item_response_schema(self):
        from app.schemas import ChecklistItemResponse
        r = ChecklistItemResponse(id="abc-123", displayName="task", isChecked=False)
        assert r.id == "abc-123"
        assert r.displayName == "task"


# ─────────────────────────────────────────────
# F1.5 – task_service checklist functions (mocked DB + Graph)
# ─────────────────────────────────────────────

class TestF15ChecklistService:
    def _make_task(self, checklist_items=None):
        task = MagicMock()
        task.id = uuid.uuid4()
        task.ms_id = "graph-task-id"
        task.list_id = uuid.uuid4()
        task.checklist_items = checklist_items or []
        task.sync_status = "synced"
        task.deleted_at = None
        return task

    def _make_task_list(self):
        tl = MagicMock()
        tl.id = uuid.uuid4()
        tl.ms_id = "graph-list-id"
        return tl

    @pytest.mark.asyncio
    async def test_add_checklist_item_happy_path(self):
        """add_checklist_item создаёт item и возвращает его с id из Graph."""
        from app.services import task_service

        task = self._make_task()
        task_list = self._make_task_list()

        db = AsyncMock()

        # get_task вернёт нашу задачу
        with patch.object(task_service, "get_task", return_value=task), \
             patch.object(task_service.graph_client, "create_checklist_item",
                          return_value={"id": "item-ms-id-1", "displayName": "купить батарейки", "isChecked": False}) as mock_graph, \
             patch("app.services.task_service.select") as _select:

            # Мокируем запрос TaskList через db.execute
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = task_list
            db.execute = AsyncMock(return_value=mock_result)

            result = await task_service.add_checklist_item(db, task.id, "купить батарейки", False)

            assert result is not None
            assert result["id"] == "item-ms-id-1"
            assert result["displayName"] == "купить батарейки"
            mock_graph.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_checklist_item_toggle(self):
        """update_checklist_item обновляет isChecked у существующего пункта."""
        from app.services import task_service

        existing_items = [
            {"id": "item-1", "displayName": "task one", "isChecked": False}
        ]
        task = self._make_task(checklist_items=existing_items)
        task_list = self._make_task_list()

        db = AsyncMock()

        with patch.object(task_service, "get_task", return_value=task), \
             patch.object(task_service.graph_client, "update_checklist_item",
                          return_value={}) as mock_graph, \
             patch("app.services.task_service.select") as _select:

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = task_list
            db.execute = AsyncMock(return_value=mock_result)

            result = await task_service.update_checklist_item(
                db, task.id, "item-1", display_name=None, is_checked=True
            )

            assert result is not None
            assert result["isChecked"] is True
            assert result["displayName"] == "task one"
            mock_graph.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_checklist_item_not_found(self):
        """update_checklist_item возвращает None если item_id не существует."""
        from app.services import task_service

        task = self._make_task(checklist_items=[{"id": "item-1", "displayName": "x", "isChecked": False}])
        task_list = self._make_task_list()

        db = AsyncMock()

        with patch.object(task_service, "get_task", return_value=task), \
             patch("app.services.task_service.select") as _select:

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = task_list
            db.execute = AsyncMock(return_value=mock_result)

            result = await task_service.update_checklist_item(
                db, task.id, "nonexistent-id", display_name=None, is_checked=True
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_remove_checklist_item_happy_path(self):
        """remove_checklist_item удаляет пункт из списка и вызывает Graph."""
        from app.services import task_service

        existing_items = [
            {"id": "item-del", "displayName": "удалить меня", "isChecked": False},
            {"id": "item-keep", "displayName": "оставить", "isChecked": False},
        ]
        task = self._make_task(checklist_items=existing_items)
        task_list = self._make_task_list()

        db = AsyncMock()

        with patch.object(task_service, "get_task", return_value=task), \
             patch.object(task_service.graph_client, "delete_checklist_item",
                          return_value=None) as mock_del, \
             patch("app.services.task_service.select") as _select:

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = task_list
            db.execute = AsyncMock(return_value=mock_result)

            deleted = await task_service.remove_checklist_item(db, task.id, "item-del")

            assert deleted is True
            mock_del.assert_called_once_with("graph-list-id", "graph-task-id", "item-del")
            # Проверяем что item удалён из локального списка
            remaining_ids = [it["id"] for it in task.checklist_items]
            assert "item-del" not in remaining_ids
            assert "item-keep" in remaining_ids

    @pytest.mark.asyncio
    async def test_remove_checklist_item_not_found(self):
        """remove_checklist_item возвращает False если item_id не существует."""
        from app.services import task_service

        task = self._make_task(checklist_items=[{"id": "item-1", "displayName": "x", "isChecked": False}])

        db = AsyncMock()

        with patch.object(task_service, "get_task", return_value=task):
            deleted = await task_service.remove_checklist_item(db, task.id, "nonexistent")
            assert deleted is False

    @pytest.mark.asyncio
    async def test_add_checklist_item_graph_failure_saves_locally(self):
        """Если Graph недоступен — item сохраняется локально, sync_status=pending_push."""
        from app.services import task_service

        task = self._make_task()
        task_list = self._make_task_list()

        db = AsyncMock()

        with patch.object(task_service, "get_task", return_value=task), \
             patch.object(task_service.graph_client, "create_checklist_item",
                          side_effect=Exception("Graph unavailable")), \
             patch("app.services.task_service.select") as _select:

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = task_list
            db.execute = AsyncMock(return_value=mock_result)

            result = await task_service.add_checklist_item(db, task.id, "offline item", False)

            # Item должен быть добавлен локально (без id от Graph)
            assert result is not None
            assert result["displayName"] == "offline item"
            # sync_status должен стать pending_push
            assert task.sync_status == "pending_push"
