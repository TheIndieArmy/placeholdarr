"""dashboard stats snapshot table

Revision ID: 0017_dashboard_stats_snapshot
Revises: 0016_system_activity_history
Create Date: 2026-04-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0017_dashboard_stats_snapshot"
down_revision = "0016_system_activity_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dashboard_stats_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("movies_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("movies_placeholders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("movies_downloaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("movies_future_outside_lookahead", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("series_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("episodes_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("episodes_placeholders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("episodes_downloaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("episodes_future_outside_lookahead", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("placeholders_on_disk", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_pending", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sync", sa.DateTime(timezone=True), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("dashboard_stats_snapshot")
