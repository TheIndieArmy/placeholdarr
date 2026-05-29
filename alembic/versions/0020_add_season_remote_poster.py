"""add season remote_poster for Sonarr season artwork

Revision ID: 0020_add_season_remote_poster
Revises: 0019_scheduled_task_run
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_add_season_remote_poster"
down_revision = "0019_scheduled_task_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("season", sa.Column("remote_poster", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("season", "remote_poster")
