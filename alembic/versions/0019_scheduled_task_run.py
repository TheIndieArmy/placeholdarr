"""scheduled task run history for Tasks UI

Revision ID: 0019_scheduled_task_run
Revises: 0018_add_placeholder_projected_display_status
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_scheduled_task_run"
down_revision = "0018_add_placeholder_projected_display_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_task_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("skip_reason", sa.String(length=256), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_scheduled_task_run_started_at",
        "scheduled_task_run",
        ["started_at"],
    )
    op.create_index(
        "ix_scheduled_task_run_task_key_started",
        "scheduled_task_run",
        ["task_key", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_task_run_task_key_started", table_name="scheduled_task_run")
    op.drop_index("ix_scheduled_task_run_started_at", table_name="scheduled_task_run")
    op.drop_table("scheduled_task_run")
