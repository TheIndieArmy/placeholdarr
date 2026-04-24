"""placeholder activity history table

Revision ID: 0015_placeholder_activity_history
Revises: 0014_add_refresh_throttle
Create Date: 2026-04-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0015_placeholder_activity_history"
down_revision = "0014_add_refresh_throttle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "placeholder_activity_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("item_type", sa.String(length=16), nullable=False),
        sa.Column("placeholder_id", sa.Integer(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("episode_id", sa.Integer(), nullable=True),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("instance_key", sa.String(length=128), nullable=True),
        sa.Column("instance_id", sa.String(length=128), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=True),
        sa.Column("path", sa.String(), nullable=False, server_default=""),
        sa.Column("item_title", sa.String(), nullable=False, server_default=""),
        sa.Column("series_title", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("status_label", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("event_log_id", sa.Integer(), nullable=True),
        sa.Column("extra_snapshot", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_log_id"], ["event_log.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["movie_id"], ["movie.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["placeholder_id"], ["placeholder.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["season_id"], ["season.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_placeholder_activity_history_instance_occurred",
        "placeholder_activity_history",
        ["instance_key", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_placeholder_activity_history_occurred_at",
        "placeholder_activity_history",
        ["occurred_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_placeholder_activity_history_occurred_at", table_name="placeholder_activity_history")
    op.drop_index("ix_placeholder_activity_history_instance_occurred", table_name="placeholder_activity_history")
    op.drop_table("placeholder_activity_history")
