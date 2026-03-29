"""add phase 2 lookup indexes

Revision ID: 0009_add_phase2_lookup_indexes
Revises: 0008_drop_content_placeholder_status
Create Date: 2026-03-27 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0009_add_phase2_lookup_indexes'
down_revision = '0008_drop_content_placeholder_status'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_movie_determination', 'movie', ['determination'], unique=False)
    op.create_index('ix_episode_determination', 'episode', ['determination'], unique=False)
    op.create_index('ix_series_plex_dummy_id', 'series', ['plex_dummy_id'], unique=False)
    op.create_index(
        'ix_placeholder_plex_placeholder_id_missing',
        'placeholder',
        ['plex_placeholder_id'],
        unique=False,
        postgresql_where=sa.text('plex_placeholder_id IS NULL'),
    )


def downgrade():
    op.drop_index('ix_placeholder_plex_placeholder_id_missing', table_name='placeholder')
    op.drop_index('ix_series_plex_dummy_id', table_name='series')
    op.drop_index('ix_episode_determination', table_name='episode')
    op.drop_index('ix_movie_determination', table_name='movie')
