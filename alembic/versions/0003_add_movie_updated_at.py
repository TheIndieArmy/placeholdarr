"""add movie.updated_at column

Revision ID: 0003_add_movie_updated_at
Revises: 0002_migration_history
Create Date: 2025-10-17 06:40:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0003_add_movie_updated_at'
down_revision = '0002_migration_history'
branch_labels = None
depends_on = None


def upgrade():
    # Add updated_at column to movie table (timestamptz)
    op.add_column('movie', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True))


def downgrade():
    op.drop_column('movie', 'updated_at')
