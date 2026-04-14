"""Unit tests for F1.2 pull-path: Graph dateTimeTimeZone → model datetime fields.

Covers `_parse_dt_obj` helper that stores full datetime with timezone for parity
with the F1.2 write-path (task_service._task_to_graph_payload).
"""

from datetime import datetime, timezone

from app.services.sync_service import _parse_dt_obj


class TestParseDtObj:
    def test_none_returns_nones(self):
        assert _parse_dt_obj(None) == (None, None)

    def test_empty_dict_returns_nones(self):
        assert _parse_dt_obj({}) == (None, None)

    def test_non_dict_returns_nones(self):
        assert _parse_dt_obj("2026-04-14T12:00:00") == (None, None)
        assert _parse_dt_obj(123) == (None, None)

    def test_full_utc_datetime(self):
        dt_obj = {"dateTime": "2026-04-14T12:00:00.0000000", "timeZone": "UTC"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt == datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert tz == "UTC"

    def test_graph_samara_timezone_preserved(self):
        dt_obj = {"dateTime": "2026-04-14T08:00:00.0000000", "timeZone": "Europe/Samara"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt is not None
        assert dt.tzinfo is not None
        assert tz == "Europe/Samara"

    def test_z_suffix_treated_as_utc(self):
        dt_obj = {"dateTime": "2026-04-14T12:00:00Z", "timeZone": "UTC"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt == datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)

    def test_naive_datetime_assumed_utc(self):
        dt_obj = {"dateTime": "2026-04-14T12:00:00", "timeZone": "UTC"}
        dt, _tz = _parse_dt_obj(dt_obj)
        assert dt == datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert dt.tzinfo is not None

    def test_missing_datetime_field(self):
        dt_obj = {"timeZone": "Europe/Moscow"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt is None
        assert tz == "Europe/Moscow"

    def test_empty_datetime_string(self):
        dt_obj = {"dateTime": "", "timeZone": "UTC"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt is None
        assert tz == "UTC"

    def test_invalid_datetime_format(self):
        dt_obj = {"dateTime": "not-a-date", "timeZone": "UTC"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt is None
        assert tz == "UTC"

    def test_timezone_missing(self):
        dt_obj = {"dateTime": "2026-04-14T12:00:00.0000000"}
        dt, tz = _parse_dt_obj(dt_obj)
        assert dt == datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert tz is None


class TestPullPathIntegration:
    """Smoke-test the integration point: a synthetic Graph task item must yield
    populated due_datetime, start_datetime, start_timezone fields in task_data.

    We replicate only the relevant lines from pull_tasks_for_list to avoid
    pulling in async DB plumbing.
    """

    def test_graph_item_populates_all_three_fields(self):
        item = {
            "id": "ms-task-1",
            "title": "Example",
            "dueDateTime": {"dateTime": "2026-04-20T15:00:00.0000000", "timeZone": "UTC"},
            "startDateTime": {"dateTime": "2026-04-15T09:30:00.0000000", "timeZone": "Europe/Samara"},
        }

        due_dt, _ = _parse_dt_obj(item.get("dueDateTime"))
        start_dt, start_tz = _parse_dt_obj(item.get("startDateTime"))

        assert due_dt == datetime(2026, 4, 20, 15, 0, 0, tzinfo=timezone.utc)
        assert start_dt is not None
        assert start_tz == "Europe/Samara"

    def test_graph_item_without_start_datetime(self):
        item = {
            "id": "ms-task-2",
            "title": "No start",
            "dueDateTime": {"dateTime": "2026-04-20T15:00:00.0000000", "timeZone": "UTC"},
        }

        due_dt, _ = _parse_dt_obj(item.get("dueDateTime"))
        start_dt, start_tz = _parse_dt_obj(item.get("startDateTime"))

        assert due_dt is not None
        assert start_dt is None
        assert start_tz is None

    def test_graph_item_without_any_datetimes(self):
        item = {"id": "ms-task-3", "title": "Bare task"}

        due_dt, _ = _parse_dt_obj(item.get("dueDateTime"))
        start_dt, start_tz = _parse_dt_obj(item.get("startDateTime"))

        assert due_dt is None
        assert start_dt is None
        assert start_tz is None
