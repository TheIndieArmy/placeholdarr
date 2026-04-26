"""system activity history table

Revision ID: 0016_system_activity_history
Revises: 0015_placeholder_activity_history
Create Date: 2026-04-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0016_system_activity_history"
down_revision = "0015_placeholder_activity_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_activity_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_activity_history_occurred_at", "system_activity_history", ["occurred_at"], unique=False)


def downgrade():
    op.drop_index("ix_system_activity_history_occurred_at", table_name="system_activity_history")
    op.drop_table("system_activity_history")
