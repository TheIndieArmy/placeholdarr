"""add projected display status on placeholder

Revision ID: 0018_add_placeholder_projected_display_status
Revises: 0017_dashboard_stats_snapshot
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0018_add_placeholder_projected_display_status"
down_revision = "0017_dashboard_stats_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("placeholder", sa.Column("display_status_projected", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("placeholder", "display_status_projected")
