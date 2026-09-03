"""placeholder policy flags on series and season

Revision ID: 0028_series_season_placeholder_policy
Revises: 0027_block_placeholder
Create Date: 2026-09-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0028_series_season_placeholder_policy"
down_revision = "0027_block_placeholder"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("series", "season"):
        op.add_column(
            table,
            sa.Column("force_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.add_column(
            table,
            sa.Column(
                "force_placeholder_despite_sibling",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        op.add_column(
            table,
            sa.Column("block_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade():
    for table in ("season", "series"):
        op.drop_column(table, "block_placeholder")
        op.drop_column(table, "force_placeholder_despite_sibling")
        op.drop_column(table, "force_placeholder")
