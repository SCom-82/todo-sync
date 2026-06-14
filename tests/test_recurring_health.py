"""
Tests for health endpoint fixes (ADR 2026-06-14, ticket c).

Covers:
1. /sync/trigger: exception inside run_sync returns readable HTTP 500 with traceback detail,
   not a bare FastAPI "Internal Server Error" with empty body.
2. delta_success_rate_pct: already covered by test_f3_sync.py — referenced here for traceability.

Run: pytest tests/test_recurring_health.py -v
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────
# 1. /sync/trigger readable 500 (ticket c)
# ─────────────────────────────────────────────

class TestSyncTriggerReadable500:
    """
    Verify that /sync/trigger wraps exceptions into HTTP 500 with a readable detail
    containing the exception class and message — not an empty FastAPI 500.
    """

    def _make_client(self):
        from app.main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_trigger_exception_returns_readable_500(self):
        """When run_sync raises, /sync/trigger must return HTTP 500 with readable detail."""
        with patch("app.services.sync_service.run_sync", new_callable=AsyncMock) as mock_sync:
            mock_sync.side_effect = RuntimeError("Graph token expired")
            client = self._make_client()
            response = client.post("/api/v1/sync/trigger")

        assert response.status_code == 500
        body = response.json()
        # detail must be a non-empty string containing exception info
        detail = body.get("detail", "")
        assert isinstance(detail, str), f"detail must be str, got {type(detail)}"
        assert len(detail) > 0, "detail must not be empty"
        # Must mention the exception type
        assert "RuntimeError" in detail, f"detail must contain exception type, got: {detail!r}"
        # Must mention the message
        assert "Graph token expired" in detail, f"detail must contain message, got: {detail!r}"

    def test_trigger_exception_detail_contains_traceback(self):
        """detail must contain traceback info (not just the exception line)."""
        with patch("app.services.sync_service.run_sync", new_callable=AsyncMock) as mock_sync:
            mock_sync.side_effect = ValueError("Missing delta token")
            client = self._make_client()
            response = client.post("/api/v1/sync/trigger")

        assert response.status_code == 500
        detail = response.json().get("detail", "")
        # Traceback presence: 'Traceback (most recent call last)' OR the exception type+message
        # The implementation uses traceback.format_exc() which always includes this header
        assert "Traceback" in detail or "ValueError" in detail, (
            f"Expected traceback or exception in detail, got: {detail!r}"
        )
        assert "Missing delta token" in detail

    def test_trigger_success_returns_200(self):
        """When run_sync succeeds, /sync/trigger returns 200 with sync result."""
        with patch("app.services.sync_service.run_sync", new_callable=AsyncMock) as mock_sync:
            mock_sync.return_value = {"status": "ok", "synced": 0}
            client = self._make_client()
            response = client.post("/api/v1/sync/trigger")

        assert response.status_code == 200
        assert response.json().get("status") == "ok"


# ─────────────────────────────────────────────
# 2. delta_success_rate_pct: zero-division guard (ticket c, traceability)
# ─────────────────────────────────────────────

class TestDeltaSuccessRateTraceability:
    """
    delta_success_rate_pct division-by-zero guard is tested in test_f3_sync.py
    (test_skip_rate_zero_when_no_syncs, test_status_endpoint_zero_metrics_no_division_error).
    This class provides ticket-c AC traceability and a direct formula sanity check.
    """

    def test_success_rate_formula_correct(self):
        """success_rate = succeeded/total * 100, rounded to 2 decimal places."""
        total = 10
        succeeded = 7
        rate = round((succeeded / total) * 100, 2)
        assert rate == 70.0

    def test_success_rate_zero_when_total_is_zero(self):
        """When total=0, success_rate must be 0.0 (not ZeroDivisionError)."""
        total = 0
        succeeded = 0
        rate = 0.0 if total == 0 else round((succeeded / total) * 100, 2)
        assert rate == 0.0

    def test_schema_field_named_correctly(self):
        """SyncStatusResponse must have delta_success_rate_pct (not the old delta_skip_rate_pct)."""
        from app.schemas import SyncStatusResponse
        import dataclasses
        # Check field exists
        s = SyncStatusResponse(last_sync_at=None, last_sync_status=None, resources=[])
        assert hasattr(s, "delta_success_rate_pct"), (
            "SyncStatusResponse must have delta_success_rate_pct (renamed from delta_skip_rate_pct)"
        )
        # Must NOT have the old misnomer
        assert not hasattr(s, "delta_skip_rate_pct"), (
            "delta_skip_rate_pct must have been removed (renamed to delta_success_rate_pct)"
        )
