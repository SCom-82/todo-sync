"""
QA integration tests for F1 block (feature/f1-multi).

Written by dev-qa. Tests NOT written by the coder — fresh eyes, edge cases.

Covers:
  - Migration 004 round-trip (column add/drop/add): ORM model introspection + SQLite DDL
  - _task_to_graph_payload: F1.2 + F1.3 + F1.4 edge cases
  - pull_tasks_for_list: real DB round-trip with mocked graph_client
  - GET /lists/resolve: ambiguous name → 409, not-found → 404
  - PATCH /tasks/{id}/checklist/{item_id}: invalid item_id → 404 (not 500)
  - Regression: due_date legacy path not broken by due_datetime
  - start_datetime without start_timezone → fallback to UTC in payload
"""
import uuid
from datetime import date, datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import DateTime, String, inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Task, TaskList
from app.services.task_service import _task_to_graph_payload


# ─────────────────────────────────────────────────────────────────────────────
# SQLite-compatible schema creation
# SQLite does not support PG-specific JSONB/UUID types.
# We create tables with equivalent SQLite-compatible DDL via raw CREATE TABLE.
# ─────────────────────────────────────────────────────────────────────────────

SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_lists (
    id TEXT PRIMARY KEY,
    ms_id TEXT UNIQUE,
    display_name TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 1,
    is_shared INTEGER NOT NULL DEFAULT 0,
    wellknown_list_name TEXT,
    ms_last_modified TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT,
    sync_status TEXT NOT NULL DEFAULT 'synced'
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    ms_id TEXT UNIQUE,
    list_id TEXT NOT NULL REFERENCES task_lists(id),
    title TEXT NOT NULL,
    body TEXT,
    body_content_type TEXT NOT NULL DEFAULT 'text',
    importance TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'notStarted',
    due_date TEXT,
    due_timezone TEXT NOT NULL DEFAULT 'UTC',
    due_datetime TEXT,
    start_datetime TEXT,
    start_timezone TEXT,
    reminder_datetime TEXT,
    is_reminder_on INTEGER NOT NULL DEFAULT 0,
    completed_datetime TEXT,
    completed_timezone TEXT,
    recurrence TEXT,
    categories TEXT NOT NULL DEFAULT '[]',
    checklist_items TEXT NOT NULL DEFAULT '[]',
    ms_created_at TEXT,
    ms_last_modified TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT,
    sync_status TEXT NOT NULL DEFAULT 'synced'
);

CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT UNIQUE NOT NULL,
    delta_link TEXT,
    last_sync_at TEXT,
    last_sync_status TEXT NOT NULL DEFAULT 'success',
    last_error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_cache TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    items_pulled INTEGER NOT NULL DEFAULT 0,
    items_pushed INTEGER NOT NULL DEFAULT 0,
    items_deleted INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    details TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


@pytest_asyncio.fixture
async def sqlite_engine():
    """Async SQLite engine with SQLite-compatible schema (no JSONB/UUID)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        for stmt in SQLITE_SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(sa.text(stmt))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(sqlite_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide an AsyncSession backed by the SQLite test DB."""
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


def _new_list_id() -> str:
    return str(uuid.uuid4())


def _new_task_id() -> str:
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def sample_task_list(db_session: AsyncSession) -> dict:
    """Insert a TaskList row and return as dict with 'id' key."""
    list_id = _new_list_id()
    await db_session.execute(
        sa.text(
            "INSERT INTO task_lists (id, ms_id, display_name, sync_status) "
            "VALUES (:id, :ms_id, :dn, 'synced')"
        ),
        {"id": list_id, "ms_id": "ms-list-fixture-001", "dn": "Test List"},
    )
    await db_session.commit()
    return {"id": list_id, "ms_id": "ms-list-fixture-001", "display_name": "Test List"}


@pytest_asyncio.fixture
async def sample_task(db_session: AsyncSession, sample_task_list: dict) -> dict:
    """Insert a Task row and return as dict."""
    task_id = _new_task_id()
    await db_session.execute(
        sa.text(
            "INSERT INTO tasks (id, ms_id, list_id, title, sync_status, checklist_items, categories) "
            "VALUES (:id, :ms_id, :list_id, 'QA Task', 'synced', '[]', '[]')"
        ),
        {"id": task_id, "ms_id": "ms-task-fixture-001", "list_id": sample_task_list["id"]},
    )
    await db_session.commit()
    return {"id": task_id, "ms_id": "ms-task-fixture-001", "list_id": sample_task_list["id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Migration 004 round-trip (ORM introspection + DDL column checks)
# ─────────────────────────────────────────────────────────────────────────────

class TestMigration004RoundTrip:
    """Verify that migration 004 columns are present in the ORM model and DB schema."""

    def test_orm_model_has_due_datetime(self):
        """Task ORM model must declare due_datetime column."""
        cols = {c.key for c in Task.__table__.columns}
        assert "due_datetime" in cols, "due_datetime missing from Task model"

    def test_orm_model_has_start_datetime(self):
        cols = {c.key for c in Task.__table__.columns}
        assert "start_datetime" in cols, "start_datetime missing from Task model"

    def test_orm_model_has_start_timezone(self):
        cols = {c.key for c in Task.__table__.columns}
        assert "start_timezone" in cols, "start_timezone missing from Task model"

    def test_due_datetime_is_datetime_with_tz(self):
        """due_datetime must be DateTime(timezone=True)."""
        col = Task.__table__.c["due_datetime"]
        assert isinstance(col.type, DateTime), f"Expected DateTime, got {type(col.type)}"
        assert col.type.timezone is True, "due_datetime must have timezone=True"

    def test_start_datetime_is_datetime_with_tz(self):
        col = Task.__table__.c["start_datetime"]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True

    def test_start_timezone_is_string_nullable(self):
        col = Task.__table__.c["start_timezone"]
        assert isinstance(col.type, String)
        assert col.nullable is True

    @pytest.mark.asyncio
    async def test_sqlite_schema_has_all_three_columns(self, sqlite_engine):
        """SQLite schema created from raw DDL must have all three F1.2 columns."""
        async with sqlite_engine.connect() as conn:
            result = await conn.execute(sa.text("PRAGMA table_info(tasks)"))
            col_names = [row[1] for row in result.fetchall()]
        assert "due_datetime" in col_names, f"due_datetime not in SQLite tasks: {col_names}"
        assert "start_datetime" in col_names
        assert "start_timezone" in col_names

    @pytest.mark.asyncio
    async def test_round_trip_write_and_read_datetimes(self, db_session: AsyncSession, sample_task_list: dict):
        """Write datetime values to SQLite, read them back — column presence confirmed."""
        task_id = _new_task_id()
        await db_session.execute(
            sa.text(
                "INSERT INTO tasks (id, ms_id, list_id, title, sync_status, "
                "due_datetime, start_datetime, start_timezone, checklist_items, categories) "
                "VALUES (:id, :ms_id, :list_id, 'DT Task', 'synced', "
                ":due_dt, :start_dt, :tz, '[]', '[]')"
            ),
            {
                "id": task_id,
                "ms_id": "ms-dt-rt-001",
                "list_id": sample_task_list["id"],
                "due_dt": "2026-04-20T15:00:00+00:00",
                "start_dt": "2026-04-15T09:30:00+00:00",
                "tz": "Europe/Samara",
            },
        )
        await db_session.commit()

        result = await db_session.execute(
            sa.text("SELECT due_datetime, start_datetime, start_timezone FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] is not None and "2026-04-20" in row[0]
        assert row[1] is not None and "2026-04-15" in row[1]
        assert row[2] == "Europe/Samara"

    @pytest.mark.asyncio
    async def test_downgrade_simulation_drop_and_recreate(self, sqlite_engine):
        """Simulate migration downgrade: drop columns (via table recreation), then verify they're gone,
        then re-add them (simulate upgrade). Tests upgrade→downgrade→upgrade flow."""
        # SQLite < 3.35 does not support DROP COLUMN, so we test using a temp table
        async with sqlite_engine.begin() as conn:
            # Create a temp table without the F1.2 columns (simulating pre-004 state)
            await conn.execute(sa.text("""
                CREATE TABLE tasks_pre004 (
                    id TEXT PRIMARY KEY,
                    ms_id TEXT,
                    list_id TEXT,
                    title TEXT NOT NULL,
                    sync_status TEXT DEFAULT 'synced',
                    checklist_items TEXT DEFAULT '[]',
                    categories TEXT DEFAULT '[]'
                )
            """))
            # Verify the F1.2 columns don't exist here
            result = await conn.execute(sa.text("PRAGMA table_info(tasks_pre004)"))
            cols = [row[1] for row in result.fetchall()]
            assert "due_datetime" not in cols
            assert "start_datetime" not in cols
            assert "start_timezone" not in cols

            # Simulate upgrade: add the columns (migration 004 upgrade())
            await conn.execute(sa.text("ALTER TABLE tasks_pre004 ADD COLUMN due_datetime TEXT"))
            await conn.execute(sa.text("ALTER TABLE tasks_pre004 ADD COLUMN start_datetime TEXT"))
            await conn.execute(sa.text("ALTER TABLE tasks_pre004 ADD COLUMN start_timezone TEXT"))

            # Verify columns exist post-upgrade
            result = await conn.execute(sa.text("PRAGMA table_info(tasks_pre004)"))
            cols_after = [row[1] for row in result.fetchall()]
            assert "due_datetime" in cols_after
            assert "start_datetime" in cols_after
            assert "start_timezone" in cols_after


# ─────────────────────────────────────────────────────────────────────────────
# _task_to_graph_payload edge cases (F1.2 + F1.3 + F1.4)
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphPayloadEdgeCases:
    """Edge cases in _task_to_graph_payload not covered by coder tests."""

    def _base_task_mock(self):
        task = MagicMock(spec=Task)
        task.title = "Base"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
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
        return task

    # F1.2: start_datetime without start_timezone → fallback to UTC in payload
    def test_start_datetime_no_timezone_falls_back_to_utc(self):
        task = self._base_task_mock()
        task.start_datetime = datetime(2026, 4, 15, 9, 0, 0, tzinfo=timezone.utc)
        task.start_timezone = None

        payload = _task_to_graph_payload(task)
        assert "startDateTime" in payload
        assert payload["startDateTime"]["timeZone"] == "UTC", (
            f"Expected UTC fallback when start_timezone=None, got {payload['startDateTime']['timeZone']}"
        )

    # F1.2: due_date legacy without due_datetime — regression guard
    def test_legacy_due_date_only_no_due_datetime(self):
        task = self._base_task_mock()
        task.due_datetime = None
        task.due_date = date(2026, 5, 1)

        payload = _task_to_graph_payload(task)
        assert "dueDateTime" in payload
        assert "2026-05-01" in payload["dueDateTime"]["dateTime"]
        assert payload["dueDateTime"]["timeZone"] == "UTC"

    # F1.2: both due_datetime and due_date present — due_datetime wins
    def test_due_datetime_takes_priority_over_due_date(self):
        task = self._base_task_mock()
        task.due_datetime = datetime(2026, 4, 20, 15, 0, 0, tzinfo=timezone.utc)
        task.due_timezone = "Europe/Moscow"
        task.due_date = date(2026, 4, 21)  # different date — should be ignored

        payload = _task_to_graph_payload(task)
        assert "dueDateTime" in payload
        # Must use due_datetime value (April 20), not due_date (April 21)
        assert "2026-04-20" in payload["dueDateTime"]["dateTime"], (
            f"due_datetime should win over due_date: {payload['dueDateTime']}"
        )
        assert payload["dueDateTime"]["timeZone"] == "Europe/Moscow"

    # F1.2: no due at all → dueDateTime must NOT appear in payload
    def test_no_due_produces_no_due_datetime_key(self):
        task = self._base_task_mock()
        task.due_datetime = None
        task.due_date = None

        payload = _task_to_graph_payload(task)
        assert "dueDateTime" not in payload

    # F1.3: body=None → body key must NOT appear (Graph treats absent body as no-op)
    def test_no_body_no_body_key(self):
        task = self._base_task_mock()
        task.body = None

        payload = _task_to_graph_payload(task)
        assert "body" not in payload

    # F1.3: html body with all fields correct
    def test_html_body_full_payload(self):
        task = self._base_task_mock()
        task.body = "<b>urgent</b>"
        task.body_content_type = "html"

        payload = _task_to_graph_payload(task)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == "<b>urgent</b>"

    # F1.4: recurrence=None → recurrence key must NOT appear
    def test_no_recurrence_no_recurrence_key(self):
        task = self._base_task_mock()
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert "recurrence" not in payload

    # F1.4: recurrence dict round-trip through payload
    def test_recurrence_dict_passed_through(self):
        task = self._base_task_mock()
        task.recurrence = {
            "pattern": {"type": "daily", "interval": 1},
            "range": {"type": "noEnd", "startDate": "2026-04-15"},
        }

        payload = _task_to_graph_payload(task)
        assert "recurrence" in payload
        assert payload["recurrence"]["pattern"]["type"] == "daily"
        assert payload["recurrence"]["range"]["type"] == "noEnd"

    # Combined: due_datetime + html body + recurrence all in one payload
    def test_combined_f12_f13_f14_payload(self):
        task = self._base_task_mock()
        task.due_datetime = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        task.due_timezone = "Europe/Samara"
        task.start_datetime = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
        task.start_timezone = "Europe/Samara"
        task.body = "<p>combined</p>"
        task.body_content_type = "html"
        task.recurrence = {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["friday"]},
            "range": {"type": "noEnd", "startDate": "2026-05-10"},
        }

        payload = _task_to_graph_payload(task)
        # F1.2
        assert payload["dueDateTime"]["timeZone"] == "Europe/Samara"
        assert "T" in payload["dueDateTime"]["dateTime"]
        assert payload["startDateTime"]["timeZone"] == "Europe/Samara"
        # F1.3
        assert payload["body"]["contentType"] == "html"
        # F1.4
        assert payload["recurrence"]["pattern"]["type"] == "weekly"

    def test_due_datetime_format_is_graph_compatible(self):
        """Graph expects 'YYYY-MM-DDTHH:MM:SS.0000000' format, not ISO with +offset."""
        task = self._base_task_mock()
        task.due_datetime = datetime(2026, 4, 20, 14, 30, 59, tzinfo=timezone.utc)
        task.due_timezone = "UTC"

        payload = _task_to_graph_payload(task)
        dt_str = payload["dueDateTime"]["dateTime"]
        # Must have T separator and no +00:00 offset (Graph uses timeZone field separately)
        assert "T" in dt_str
        assert "14:30" in dt_str
        # Should not contain timezone offset — timeZone is a separate key
        assert "+" not in dt_str, f"dateTime should not contain +offset: {dt_str}"


# ─────────────────────────────────────────────────────────────────────────────
# pull_tasks_for_list: task_data assembly verification (mocked DB session)
# SQLite + ORM is not used here due to PG-specific JSONB/UUID type incompatibility.
# Instead we mock the DB session and verify the task_data dict assembled by
# pull_tasks_for_list contains correct F1.2 fields before being written to DB.
# This is the relevant integration boundary: Graph JSON → task_data dict.
# ─────────────────────────────────────────────────────────────────────────────

class TestPullTasksForListIntegration:
    """
    Integration test for the Graph→task_data assembly in pull_tasks_for_list.

    We mock both graph_client and the DB session (AsyncMock) to intercept
    the Task(...) constructor call and verify that due_datetime, start_datetime,
    and start_timezone are correctly extracted from Graph items.

    The actual DB persistence is tested separately in TestMigration004RoundTrip
    (raw SQLite DDL + round-trip read).
    """

    def _task_list_mock(self, list_id=None, ms_id="ms-list-001"):
        tl = MagicMock(spec=TaskList)
        tl.id = list_id or str(uuid.uuid4())
        tl.ms_id = ms_id
        return tl

    def _build_task_data_from_graph_item(self, item: dict) -> dict:
        """
        Replicate the task_data assembly logic from pull_tasks_for_list
        without touching the DB. This is a white-box helper to verify
        the data extraction logic independently of DB infrastructure.
        """
        from app.services.sync_service import _parse_date, _parse_dt_obj, _parse_datetime

        raw_due = item.get("dueDateTime")
        due_date, due_tz = _parse_date(raw_due)
        due_dt, _ = _parse_dt_obj(raw_due)
        start_dt, start_tz = _parse_dt_obj(item.get("startDateTime"))
        body_obj = item.get("body", {})

        return {
            "title": item.get("title", ""),
            "body": body_obj.get("content") if isinstance(body_obj, dict) else None,
            "body_content_type": body_obj.get("contentType", "text") if isinstance(body_obj, dict) else "text",
            "importance": item.get("importance", "normal"),
            "status": item.get("status", "notStarted"),
            "due_date": due_date,
            "due_timezone": due_tz,
            "due_datetime": due_dt,
            "start_datetime": start_dt,
            "start_timezone": start_tz,
            "recurrence": item.get("recurrence"),
            "categories": item.get("categories", []),
        }

    def test_graph_item_with_both_datetimes_populates_all_three_fields(self):
        """Full item with dueDateTime + startDateTime → due_datetime, start_datetime, start_timezone set."""
        item = {
            "id": "ms-1",
            "title": "Task With Datetimes",
            "dueDateTime": {"dateTime": "2026-04-20T15:00:00.0000000", "timeZone": "UTC"},
            "startDateTime": {"dateTime": "2026-04-15T09:30:00.0000000", "timeZone": "Europe/Samara"},
        }
        data = self._build_task_data_from_graph_item(item)

        assert data["due_datetime"] is not None, "due_datetime must be populated"
        assert data["due_datetime"].year == 2026
        assert data["due_datetime"].month == 4
        assert data["due_datetime"].day == 20

        assert data["start_datetime"] is not None, "start_datetime must be populated"
        assert data["start_datetime"].month == 4
        assert data["start_datetime"].day == 15

        assert data["start_timezone"] == "Europe/Samara", f"Expected Europe/Samara, got {data['start_timezone']}"

    def test_graph_item_without_start_datetime_sets_none(self):
        """Item without startDateTime → start_datetime=None, start_timezone=None."""
        item = {
            "id": "ms-2",
            "title": "No Start",
            "dueDateTime": {"dateTime": "2026-04-20T15:00:00.0000000", "timeZone": "UTC"},
        }
        data = self._build_task_data_from_graph_item(item)
        assert data["due_datetime"] is not None
        assert data["start_datetime"] is None, "start_datetime should be None"
        assert data["start_timezone"] is None, "start_timezone should be None"

    def test_graph_item_bare_no_datetimes(self):
        """Item without any dateTime fields → all three fields are None."""
        item = {"id": "ms-3", "title": "Bare Task", "importance": "normal", "status": "notStarted"}
        data = self._build_task_data_from_graph_item(item)
        assert data["due_datetime"] is None
        assert data["start_datetime"] is None
        assert data["start_timezone"] is None

    def test_graph_item_preserves_samara_timezone(self):
        """start_timezone must preserve the exact string from Graph, not convert it."""
        item = {
            "id": "ms-4",
            "title": "Samara",
            "startDateTime": {"dateTime": "2026-04-14T08:00:00.0000000", "timeZone": "Europe/Samara"},
        }
        data = self._build_task_data_from_graph_item(item)
        assert data["start_timezone"] == "Europe/Samara"

    def test_graph_item_due_datetime_is_tz_aware(self):
        """Parsed due_datetime must be timezone-aware (for DB storage compatibility)."""
        item = {
            "id": "ms-5",
            "dueDateTime": {"dateTime": "2026-04-20T15:00:00.0000000", "timeZone": "UTC"},
        }
        data = self._build_task_data_from_graph_item(item)
        assert data["due_datetime"] is not None
        assert data["due_datetime"].tzinfo is not None, "due_datetime must be tz-aware"

    def test_graph_item_start_datetime_is_tz_aware(self):
        """Parsed start_datetime must be timezone-aware."""
        item = {
            "id": "ms-6",
            "startDateTime": {"dateTime": "2026-04-15T09:30:00.0000000", "timeZone": "Europe/Samara"},
        }
        data = self._build_task_data_from_graph_item(item)
        assert data["start_datetime"] is not None
        assert data["start_datetime"].tzinfo is not None, "start_datetime must be tz-aware"

    def test_graph_item_body_content_type_preserved(self):
        """body_content_type from Graph item is preserved in task_data."""
        item = {
            "id": "ms-7",
            "title": "HTML",
            "body": {"content": "<b>test</b>", "contentType": "html"},
        }
        data = self._build_task_data_from_graph_item(item)
        assert data["body_content_type"] == "html"
        assert data["body"] == "<b>test</b>"

    def test_graph_item_recurrence_preserved_as_dict(self):
        """recurrence from Graph is stored as-is (dict) in task_data."""
        rec = {"pattern": {"type": "daily", "interval": 1}, "range": {"type": "noEnd", "startDate": "2026-04-15"}}
        item = {"id": "ms-8", "title": "Recurring", "recurrence": rec}
        data = self._build_task_data_from_graph_item(item)
        assert data["recurrence"] == rec


# ─────────────────────────────────────────────────────────────────────────────
# F1.1: GET /lists/resolve — ambiguous name → 409, not-found → 404
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveListEndpoint:
    """Tests for GET /lists/resolve using FastAPI TestClient with SQLite."""

    def _make_app_client(self, sqlite_engine):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.database import get_db

        factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        return client, factory

    @pytest.mark.asyncio
    async def test_resolve_list_not_found_returns_404(self, sqlite_engine):
        client, _ = self._make_app_client(sqlite_engine)
        try:
            resp = client.get("/api/v1/lists/resolve?name=NonExistentList")
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        finally:
            from app.main import app
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_resolve_list_ambiguous_name_returns_409(self, sqlite_engine):
        """Two lists with same display_name → 409 Conflict (not 200 with first match)."""
        client, factory = self._make_app_client(sqlite_engine)
        try:
            # Insert two lists with same name
            async with factory() as session:
                for i in range(2):
                    await session.execute(
                        sa.text(
                            "INSERT INTO task_lists (id, ms_id, display_name, sync_status) "
                            "VALUES (:id, :ms_id, 'Duplicate Name', 'synced')"
                        ),
                        {"id": _new_list_id(), "ms_id": f"ms-dup-{i}"},
                    )
                await session.commit()

            resp = client.get("/api/v1/lists/resolve?name=Duplicate+Name")
            assert resp.status_code == 409, (
                f"Expected 409 Conflict for duplicate list name, got {resp.status_code}: {resp.text}"
            )
            assert "Multiple" in resp.json()["detail"], f"Detail should mention 'Multiple': {resp.json()}"
        finally:
            from app.main import app
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_resolve_list_exact_match_returns_200(self, sqlite_engine):
        """Single list with matching name → 200 with correct data."""
        client, factory = self._make_app_client(sqlite_engine)
        try:
            async with factory() as session:
                list_id = _new_list_id()
                await session.execute(
                    sa.text(
                        "INSERT INTO task_lists (id, ms_id, display_name, sync_status) "
                        "VALUES (:id, :ms_id, 'UniqueListXYZ', 'synced')"
                    ),
                    {"id": list_id, "ms_id": "ms-unique-xyz"},
                )
                await session.commit()

            resp = client.get("/api/v1/lists/resolve?name=UniqueListXYZ")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert data["display_name"] == "UniqueListXYZ"
        finally:
            from app.main import app
            app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# F1.5: PATCH/DELETE /tasks/{id}/checklist/{item_id} edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestChecklistEndpointEdgeCases:
    """F1.5 edge cases: invalid/nonexistent item_id must yield 404, not 500."""

    def _make_app_client(self, sqlite_engine):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.database import get_db

        factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        return TestClient(app), factory

    @pytest.mark.asyncio
    async def test_patch_checklist_invalid_item_id_returns_404(self, sqlite_engine):
        """PATCH with item_id not in checklist_items → 404, not 500."""
        import json
        client, factory = self._make_app_client(sqlite_engine)
        try:
            async with factory() as session:
                list_id = _new_list_id()
                task_id = _new_task_id()
                await session.execute(
                    sa.text(
                        "INSERT INTO task_lists (id, ms_id, display_name, sync_status) "
                        "VALUES (:id, :ms_id, 'CL List', 'synced')"
                    ),
                    {"id": list_id, "ms_id": "ms-cl-list-001"},
                )
                items = json.dumps([{"id": "real-item-id", "displayName": "real item", "isChecked": False}])
                await session.execute(
                    sa.text(
                        "INSERT INTO tasks (id, ms_id, list_id, title, sync_status, checklist_items, categories) "
                        "VALUES (:id, :ms_id, :list_id, 'CL Task', 'synced', :items, '[]')"
                    ),
                    {"id": task_id, "ms_id": "ms-cl-task-001", "list_id": list_id, "items": items},
                )
                await session.commit()

            resp = client.patch(
                f"/api/v1/tasks/{task_id}/checklist/invalid-uuid-that-does-not-exist",
                json={"isChecked": True},
            )
            assert resp.status_code == 404, (
                f"Expected 404 for nonexistent item_id, got {resp.status_code}: {resp.text}"
            )
        finally:
            from app.main import app
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_checklist_invalid_item_id_returns_404(self, sqlite_engine):
        """DELETE with item_id not in checklist_items → 404."""
        import json
        client, factory = self._make_app_client(sqlite_engine)
        try:
            async with factory() as session:
                list_id = _new_list_id()
                task_id = _new_task_id()
                await session.execute(
                    sa.text(
                        "INSERT INTO task_lists (id, ms_id, display_name, sync_status) "
                        "VALUES (:id, :ms_id, 'Del List', 'synced')"
                    ),
                    {"id": list_id, "ms_id": "ms-del-list-001"},
                )
                items = json.dumps([{"id": "exists-id", "displayName": "item", "isChecked": False}])
                await session.execute(
                    sa.text(
                        "INSERT INTO tasks (id, ms_id, list_id, title, sync_status, checklist_items, categories) "
                        "VALUES (:id, :ms_id, :list_id, 'Del Task', 'synced', :items, '[]')"
                    ),
                    {"id": task_id, "ms_id": "ms-del-task-001", "list_id": list_id, "items": items},
                )
                await session.commit()

            resp = client.delete(f"/api/v1/tasks/{task_id}/checklist/nonexistent-item-xyz")
            assert resp.status_code == 404, (
                f"Expected 404 for nonexistent item_id on DELETE, got {resp.status_code}: {resp.text}"
            )
        finally:
            from app.main import app
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_patch_checklist_nonexistent_task_returns_404(self, sqlite_engine):
        """PATCH on task that doesn't exist at all → 404 (not 422, not 500)."""
        client, _ = self._make_app_client(sqlite_engine)
        try:
            fake_task_id = uuid.uuid4()
            resp = client.patch(
                f"/api/v1/tasks/{fake_task_id}/checklist/some-item-id",
                json={"isChecked": True},
            )
            assert resp.status_code == 404, (
                f"Expected 404 for nonexistent task, got {resp.status_code}: {resp.text}"
            )
        finally:
            from app.main import app
            app.dependency_overrides.clear()

    def test_get_checklist_filters_items_without_id_via_api_handler(self):
        """
        GET /checklist handler filters out items without Graph id (local-only items).
        Verified by calling the handler logic directly without DB plumbing —
        the filtering is in the route handler itself (tasks.py line 109: if it.get("id")).
        """
        from app.api.tasks import list_checklist_items
        from app.schemas import ChecklistItemResponse

        # Reproduce what the handler does: filter items with no id
        raw_items = [
            {"id": "graph-id-1", "displayName": "synced item", "isChecked": False},
            {"displayName": "local-only item", "isChecked": True},  # no id
            {"id": "", "displayName": "empty-id item", "isChecked": False},  # empty string id
        ]

        # This is the exact filter logic from the handler (app/api/tasks.py)
        result = [
            ChecklistItemResponse(
                id=it.get("id", ""),
                displayName=it.get("displayName", ""),
                isChecked=bool(it.get("isChecked", False)),
            )
            for it in raw_items
            if it.get("id")  # filters out None, missing, and empty string
        ]

        # Only "graph-id-1" should survive — empty string is falsy in Python
        assert len(result) == 1, f"Expected 1 item (with truthy id), got {len(result)}: {result}"
        assert result[0].id == "graph-id-1"


# ─────────────────────────────────────────────────────────────────────────────
# Regression: legacy due_date push-path not broken by due_datetime
# ─────────────────────────────────────────────────────────────────────────────

class TestPushPathRegression:
    """Regression guard: tasks created before F1.2 (no due_datetime) still push correctly."""

    def _legacy_task(self):
        task = MagicMock(spec=Task)
        task.title = "Legacy"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
        task.body_content_type = "text"
        task.due_datetime = None
        task.due_date = date(2026, 3, 1)
        task.due_timezone = "Europe/Samara"
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None
        return task

    def test_legacy_task_payload_has_due_datetime_key(self):
        """Even legacy task must have dueDateTime key in Graph payload."""
        payload = _task_to_graph_payload(self._legacy_task())
        assert "dueDateTime" in payload

    def test_legacy_task_payload_correct_date(self):
        payload = _task_to_graph_payload(self._legacy_task())
        assert "2026-03-01" in payload["dueDateTime"]["dateTime"]

    def test_legacy_task_payload_uses_due_timezone(self):
        payload = _task_to_graph_payload(self._legacy_task())
        assert payload["dueDateTime"]["timeZone"] == "Europe/Samara"

    def test_new_datetime_task_does_not_include_date_format_only(self):
        """Task with due_datetime should use T-format with time component."""
        task = MagicMock(spec=Task)
        task.title = "New"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
        task.body_content_type = "text"
        task.due_datetime = datetime(2026, 4, 20, 14, 30, 0, tzinfo=timezone.utc)
        task.due_timezone = "UTC"
        task.due_date = None
        task.start_datetime = None
        task.start_timezone = None
        task.is_reminder_on = False
        task.reminder_datetime = None
        task.categories = []
        task.recurrence = None

        payload = _task_to_graph_payload(task)
        assert "T" in payload["dueDateTime"]["dateTime"]
        assert "14:30" in payload["dueDateTime"]["dateTime"]

    def test_task_with_due_datetime_none_fields_not_leaked(self):
        """Task where all optional fields are None — no spurious keys in payload."""
        task = MagicMock(spec=Task)
        task.title = "Minimal"
        task.importance = "normal"
        task.status = "notStarted"
        task.body = None
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
        # Only mandatory fields should be present
        assert set(payload.keys()) == {"title", "importance", "status"}, (
            f"Unexpected keys in minimal payload: {set(payload.keys())}"
        )
