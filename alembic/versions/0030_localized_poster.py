"""localized_poster columns for preferred poster language

Revision ID: 0030_localized_poster
Revises: 0029_placeholder_policy_source
Create Date: 2026-09-10 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0030_localized_poster"
down_revision = "0029_placeholder_policy_source"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("movie", "series", "season"):
        op.add_column(table, sa.Column("localized_poster", sa.String(), nullable=True))
        op.add_column(table, sa.Column("localized_poster_lang", sa.String(length=8), nullable=True))


def downgrade():
    for table in ("season", "series", "movie"):
        op.drop_column(table, "localized_poster_lang")
        op.drop_column(table, "localized_poster")
