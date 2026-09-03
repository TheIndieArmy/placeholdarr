"""force placeholder pin on movie and episode

Revision ID: 0026_force_placeholder_pin
Revises: 0025_collection_recipe_plex_keys
Create Date: 2026-09-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0026_force_placeholder_pin"
down_revision = "0025_collection_recipe_plex_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "movie",
        sa.Column("force_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "movie",
        sa.Column(
            "force_placeholder_despite_sibling",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "episode",
        sa.Column("force_placeholder", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "episode",
        sa.Column(
            "force_placeholder_despite_sibling",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column("episode", "force_placeholder_despite_sibling")
    op.drop_column("episode", "force_placeholder")
    op.drop_column("movie", "force_placeholder_despite_sibling")
    op.drop_column("movie", "force_placeholder")
