"""placeholder_kind for series/season density stubs

Revision ID: 0031_placeholder_kind
Revises: 0030_localized_poster
Create Date: 2026-09-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0031_placeholder_kind"
down_revision = "0030_localized_poster"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "placeholder",
        sa.Column("placeholder_kind", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_placeholder_kind_series",
        "placeholder",
        ["placeholder_kind", "series_id"],
    )


def downgrade():
    op.drop_index("ix_placeholder_kind_series", table_name="placeholder")
    op.drop_column("placeholder", "placeholder_kind")
