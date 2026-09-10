"""placeholder_policy_source on movie and series

Revision ID: 0029_placeholder_policy_source
Revises: 0028_series_season_placeholder_policy
Create Date: 2026-09-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0029_placeholder_policy_source"
down_revision = "0028_series_season_placeholder_policy"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("movie", "series"):
        op.add_column(
            table,
            sa.Column("placeholder_policy_source", sa.String(length=16), nullable=True),
        )


def downgrade():
    for table in ("series", "movie"):
        op.drop_column(table, "placeholder_policy_source")
