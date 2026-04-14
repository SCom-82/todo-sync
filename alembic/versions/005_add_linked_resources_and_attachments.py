"""Add linked_resources and task_attachments tables (F2.1, F2.2)

Revision ID: 005
Revises: 004
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # F2.1: linked_resources
    op.create_table(
        "linked_resources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ms_id", sa.String(255), nullable=True),
        sa.Column("web_url", sa.String(2048), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("application_name", sa.String(255), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column(
            "sync_status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_linked_resources_task_id", "linked_resources", ["task_id"])
    op.create_index("ix_linked_resources_ms_id", "linked_resources", ["ms_id"])
    op.create_index("ix_linked_resources_sync_status", "linked_resources", ["sync_status"])

    # F2.2: task_attachments
    op.create_table(
        "task_attachments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ms_id", sa.String(255), nullable=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("content_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("reference_url", sa.String(2048), nullable=True),
        sa.Column(
            "sync_status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_task_attachments_task_id", "task_attachments", ["task_id"])
    op.create_index("ix_task_attachments_ms_id", "task_attachments", ["ms_id"])
    op.create_index("ix_task_attachments_sync_status", "task_attachments", ["sync_status"])


def downgrade() -> None:
    op.drop_table("task_attachments")
    op.drop_table("linked_resources")
