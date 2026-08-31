"""store plex collection ratingKeys for ownership-safe sync

Revision ID: 0025_collection_recipe_plex_keys
Revises: 0024_collection_recipe_section_ids
Create Date: 2026-08-24 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0025_collection_recipe_plex_keys"
down_revision = "0024_collection_recipe_section_ids"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_recipe", sa.Column("plex_collection_keys", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("collection_recipe", "plex_collection_keys")
