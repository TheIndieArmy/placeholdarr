"""add season.last_found_in_sonarr

Revision ID: 0004_add_season_last_found
Revises: 0003_add_movie_updated_at
Create Date: 2025-10-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0004_add_season_last_found'
down_revision = '0003_add_movie_updated_at'
branch_labels = None
depends_on = None


def upgrade():
    # Add the column if it doesn't exist yet
    conn = op.get_bind()
    # Use raw SQL to be idempotent across PG versions
    conn.execute(sa.text("ALTER TABLE season ADD COLUMN IF NOT EXISTS last_found_in_sonarr timestamptz NULL"))


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE season DROP COLUMN IF EXISTS last_found_in_sonarr"))
