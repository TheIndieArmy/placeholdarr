"""block placeholder (never pin) on movie and episode

Revision ID: 0027_block_placeholder
Revises: 0026_force_placeholder_pin
Create Date: 2026-09-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0027_block_placeholder"
down_revision = "0026_force_placeholder_pin"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "movie",
        sa.Column("block_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "episode",
        sa.Column("block_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("episode", "block_placeholder")
    op.drop_column("movie", "block_placeholder")
