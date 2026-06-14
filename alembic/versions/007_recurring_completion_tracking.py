"""ADR 2026-06-14: recurring completion tracking — completion-intent marker and local conflict-guard timestamp

Revision ID: 007
Revises: 006
Create Date: 2026-06-14

Adds two nullable columns to tasks (additive, no drop):
- last_completed_occurrence_date DATE NULL
    completion-intent marker for conflict-guard (ADR §2).
    Set to the dueDate of the recurring occurrence we just completed, so pull-path
    can distinguish "Graph auto-advanced series" from "real uncomplete in another client".
    NOT a history mechanism — history is carried by completed-sibling tasks (ADR §2b / C').
- local_modified_at TIMESTAMPTZ NULL
    moment of last local change (set by app on create/update/complete/uncomplete).
    Used as conflict-guard comparator instead of updated_at (COMMIT time, ADR §1).
    Backfill: local_modified_at = updated_at for existing rows (sufficient approximation).

Apply on prod: infra-ops ticket only. dev-coder writes the file; infra-ops runs it.
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # completion-intent marker for recurring conflict-guard (ADR §2, variant C')
    op.add_column(
        "tasks",
        sa.Column("last_completed_occurrence_date", sa.Date(), nullable=True),
    )

    # local_modified_at: app-set timestamp of last local change (replaces updated_at in conflict-guard)
    op.add_column(
        "tasks",
        sa.Column("local_modified_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill: approximate existing rows with their updated_at value.
    # Good enough as a starting point — future local changes will set the real value.
    op.execute(
        "UPDATE tasks SET local_modified_at = updated_at WHERE local_modified_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("tasks", "local_modified_at")
    op.drop_column("tasks", "last_completed_occurrence_date")
