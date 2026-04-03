"""add instance key uniqueness for movie and series

Revision ID: 0011_add_instance_key_uniqueness
Revises: 0010_add_rich_arr_metadata_fields
Create Date: 2026-04-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0011_add_instance_key_uniqueness'
down_revision = '0010_add_rich_arr_metadata_fields'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    movie_columns = {col['name'] for col in inspector.get_columns('movie')}
    series_columns = {col['name'] for col in inspector.get_columns('series')}

    if 'instance_key' not in movie_columns:
        op.add_column('movie', sa.Column('instance_key', sa.String(), nullable=True))
    if 'instance_key' not in series_columns:
        op.add_column('series', sa.Column('instance_key', sa.String(), nullable=True))

    op.execute(
        """
        UPDATE movie
        SET instance_key = CASE
            WHEN COALESCE(is_4k, FALSE) THEN 'radarr_4k'
            ELSE 'radarr_std'
        END
        WHERE instance_key IS NULL
        """
    )
    op.execute(
        """
        UPDATE series
        SET instance_key = CASE
            WHEN COALESCE(is_4k, FALSE) THEN 'sonarr_4k'
            ELSE 'sonarr_std'
        END
        WHERE instance_key IS NULL
        """
    )

    op.alter_column('movie', 'instance_key', nullable=False)
    op.alter_column('series', 'instance_key', nullable=False)

    op.execute("ALTER TABLE movie DROP CONSTRAINT IF EXISTS movie_tmdbid_key")
    op.execute("ALTER TABLE series DROP CONSTRAINT IF EXISTS series_tvdbid_key")

    movie_indexes = {idx['name'] for idx in inspector.get_indexes('movie')}
    series_indexes = {idx['name'] for idx in inspector.get_indexes('series')}

    if 'ux_movie_tmdbid_instance_key' not in movie_indexes:
        op.create_index('ux_movie_tmdbid_instance_key', 'movie', ['tmdbid', 'instance_key'], unique=True)
    if 'ux_series_tvdbid_instance_key' not in series_indexes:
        op.create_index('ux_series_tvdbid_instance_key', 'series', ['tvdbid', 'instance_key'], unique=True)


def downgrade():
    op.drop_index('ux_series_tvdbid_instance_key', table_name='series')
    op.drop_index('ux_movie_tmdbid_instance_key', table_name='movie')

    op.create_unique_constraint('movie_tmdbid_key', 'movie', ['tmdbid'])
    op.create_unique_constraint('series_tvdbid_key', 'series', ['tvdbid'])

    op.drop_column('series', 'instance_key')
    op.drop_column('movie', 'instance_key')
