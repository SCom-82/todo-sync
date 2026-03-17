"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_lists",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ms_id", sa.String(255), unique=True, nullable=True),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("is_owner", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("is_shared", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("wellknown_list_name", sa.String(50), nullable=True),
        sa.Column("ms_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_status", sa.String(20), server_default="synced"),
    )
    op.create_index("ix_task_lists_ms_id", "task_lists", ["ms_id"])
    op.create_index("ix_task_lists_sync_status", "task_lists", ["sync_status"])

    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ms_id", sa.String(255), unique=True, nullable=True),
        sa.Column("list_id", UUID(as_uuid=True), sa.ForeignKey("task_lists.id"), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("body_content_type", sa.String(10), server_default="text"),
        sa.Column("importance", sa.String(10), server_default="normal"),
        sa.Column("status", sa.String(20), server_default="notStarted"),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("due_timezone", sa.String(50), server_default="UTC"),
        sa.Column("reminder_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_reminder_on", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("completed_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_timezone", sa.String(50), nullable=True),
        sa.Column("recurrence", JSONB(), nullable=True),
        sa.Column("categories", JSONB(), server_default="[]"),
        sa.Column("ms_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ms_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_status", sa.String(20), server_default="synced"),
    )
    op.create_index("ix_tasks_ms_id", "tasks", ["ms_id"])
    op.create_index("ix_tasks_list_id", "tasks", ["list_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_due_date", "tasks", ["due_date"])
    op.create_index("ix_tasks_sync_status", "tasks", ["sync_status"])
    op.create_index("ix_tasks_reminder", "tasks", ["is_reminder_on", "reminder_datetime"])

    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(50), unique=True, nullable=False),
        sa.Column("delta_link", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(20), server_default="success"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_cache", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "sync_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sync_type", sa.String(20), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("items_pulled", sa.Integer(), server_default="0"),
        sa.Column("items_pushed", sa.Integer(), server_default="0"),
        sa.Column("items_deleted", sa.Integer(), server_default="0"),
        sa.Column("errors", sa.Integer(), server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("details", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("sync_log")
    op.drop_table("auth_tokens")
    op.drop_table("sync_state")
    op.drop_table("tasks")
    op.drop_table("task_lists")
