"""Widen sync_state.resource_type to VARCHAR(500)

Revision ID: 002
Revises: 001
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("sync_state", "resource_type", type_=sa.String(500), existing_type=sa.String(50))


def downgrade() -> None:
    op.alter_column("sync_state", "resource_type", type_=sa.String(50), existing_type=sa.String(500))
