"""multi-library targets for collection recipes

Revision ID: 0024_collection_recipe_section_ids
Revises: 0023_collection_recipe_schedule
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0024_collection_recipe_section_ids"
down_revision = "0023_collection_recipe_schedule"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_recipe", sa.Column("plex_section_ids", sa.JSON(), nullable=True))
    op.execute(
        """
        UPDATE collection_recipe
        SET plex_section_ids = jsonb_build_array(plex_section_id)
        WHERE plex_section_ids IS NULL
        """
    )


def downgrade():
    op.drop_column("collection_recipe", "plex_section_ids")
