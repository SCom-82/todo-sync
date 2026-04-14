"""F3.5: tasks.has_attachments column; F3.6: sync metrics columns in sync_state

Revision ID: 006
Revises: 005
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # F3.5: has_attachments flag on tasks
    op.add_column(
        "tasks",
        sa.Column(
            "has_attachments",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # F3.6: delta sync metrics on sync_state
    op.add_column(
        "sync_state",
        sa.Column(
            "delta_syncs_total",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sync_state",
        sa.Column(
            "delta_syncs_succeeded",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sync_state",
        sa.Column(
            "delta_full_resets_total",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sync_state", "delta_full_resets_total")
    op.drop_column("sync_state", "delta_syncs_succeeded")
    op.drop_column("sync_state", "delta_syncs_total")
    op.drop_column("tasks", "has_attachments")
