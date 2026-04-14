"""Add due_datetime, start_datetime, start_timezone fields to tasks (F1.2)

Revision ID: 004
Revises: 003
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("due_datetime", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("start_datetime", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("start_timezone", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "start_timezone")
    op.drop_column("tasks", "start_datetime")
    op.drop_column("tasks", "due_datetime")
