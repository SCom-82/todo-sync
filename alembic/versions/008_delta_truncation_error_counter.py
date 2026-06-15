"""ADR 0003 §C-7: add last_sync_errors counter to sync_state for delta truncation tracking

Revision ID: 008
Revises: 007
Create Date: 2026-06-15

Adds one column to sync_state (additive, no drop):
- last_sync_errors INTEGER NOT NULL DEFAULT 0
    Cumulative count of delta pages that could not be parsed (Graph InternalServerError
    embedded in 200 OK body, see ADR 0003 §C-7-bis). Non-zero means at least one partial
    delta round has occurred and the corrupted task has NOT been repaired yet. Used by
    monitoring to detect silent data loss. Resets to 0 when manually cleared or after
    the data-fix (corrupted linkedResources deletion) resolves the truncation.

Apply on prod: infra-ops ticket only. dev-coder writes the file; infra-ops runs it.
"""

from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_state",
        sa.Column(
            "last_sync_errors",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("sync_state", "last_sync_errors")
