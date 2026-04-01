"""add rich arr metadata fields

Revision ID: 0010_add_rich_arr_metadata_fields
Revises: 0009_add_phase2_lookup_indexes
Create Date: 2026-03-30 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0010_add_rich_arr_metadata_fields'
down_revision = '0009_add_phase2_lookup_indexes'
branch_labels = None
depends_on = None


def upgrade():
    # movie
    op.add_column('movie', sa.Column('remote_fanart', sa.String(), nullable=True))
    op.add_column('movie', sa.Column('radarr_runtime', sa.Integer(), nullable=True))
    op.add_column('movie', sa.Column('radarr_certification', sa.String(), nullable=True))
    op.add_column('movie', sa.Column('radarr_genres', sa.JSON(), nullable=True))
    op.add_column('movie', sa.Column('radarr_studio', sa.String(), nullable=True))
    op.add_column('movie', sa.Column('radarr_ratings', sa.JSON(), nullable=True))
    op.add_column('movie', sa.Column('radarr_collection', sa.JSON(), nullable=True))
    op.add_column('movie', sa.Column('radarr_actors', sa.JSON(), nullable=True))
    op.add_column('movie', sa.Column('radarr_directors', sa.JSON(), nullable=True))
    op.add_column('movie', sa.Column('radarr_credits', sa.JSON(), nullable=True))
    op.add_column('movie', sa.Column('radarr_trailer', sa.String(), nullable=True))
    op.add_column('movie', sa.Column('radarr_premiered', sa.Date(), nullable=True))
    op.add_column('movie', sa.Column('radarr_payload_raw', sa.JSON(), nullable=True))

    # series
    op.add_column('series', sa.Column('remote_fanart', sa.String(), nullable=True))
    op.add_column('series', sa.Column('remote_banner', sa.String(), nullable=True))
    op.add_column('series', sa.Column('sonarr_runtime', sa.Integer(), nullable=True))
    op.add_column('series', sa.Column('sonarr_certification', sa.String(), nullable=True))
    op.add_column('series', sa.Column('sonarr_genres', sa.JSON(), nullable=True))
    op.add_column('series', sa.Column('sonarr_network', sa.String(), nullable=True))
    op.add_column('series', sa.Column('sonarr_ratings', sa.JSON(), nullable=True))
    op.add_column('series', sa.Column('sonarr_tmdbid', sa.Integer(), nullable=True))
    op.add_column('series', sa.Column('sonarr_tvmazeid', sa.Integer(), nullable=True))
    op.add_column('series', sa.Column('sonarr_first_aired', sa.Date(), nullable=True))
    op.add_column('series', sa.Column('sonarr_actors', sa.JSON(), nullable=True))
    op.add_column('series', sa.Column('sonarr_payload_raw', sa.JSON(), nullable=True))

    # episode
    op.add_column('episode', sa.Column('sonarr_episode_tvdbid', sa.Integer(), nullable=True))
    op.add_column('episode', sa.Column('sonarr_episode_still', sa.String(), nullable=True))
    op.add_column('episode', sa.Column('sonarr_episode_directors', sa.JSON(), nullable=True))
    op.add_column('episode', sa.Column('sonarr_episode_credits', sa.JSON(), nullable=True))
    op.add_column('episode', sa.Column('sonarr_payload_raw', sa.JSON(), nullable=True))
    op.add_column('episode', sa.Column('sonarr_episodefile_payload_raw', sa.JSON(), nullable=True))


def downgrade():
    # episode
    op.drop_column('episode', 'sonarr_episodefile_payload_raw')
    op.drop_column('episode', 'sonarr_payload_raw')
    op.drop_column('episode', 'sonarr_episode_credits')
    op.drop_column('episode', 'sonarr_episode_directors')
    op.drop_column('episode', 'sonarr_episode_still')
    op.drop_column('episode', 'sonarr_episode_tvdbid')

    # series
    op.drop_column('series', 'sonarr_payload_raw')
    op.drop_column('series', 'sonarr_actors')
    op.drop_column('series', 'sonarr_first_aired')
    op.drop_column('series', 'sonarr_tvmazeid')
    op.drop_column('series', 'sonarr_tmdbid')
    op.drop_column('series', 'sonarr_ratings')
    op.drop_column('series', 'sonarr_network')
    op.drop_column('series', 'sonarr_genres')
    op.drop_column('series', 'sonarr_certification')
    op.drop_column('series', 'sonarr_runtime')
    op.drop_column('series', 'remote_banner')
    op.drop_column('series', 'remote_fanart')

    # movie
    op.drop_column('movie', 'radarr_payload_raw')
    op.drop_column('movie', 'radarr_premiered')
    op.drop_column('movie', 'radarr_trailer')
    op.drop_column('movie', 'radarr_credits')
    op.drop_column('movie', 'radarr_directors')
    op.drop_column('movie', 'radarr_actors')
    op.drop_column('movie', 'radarr_collection')
    op.drop_column('movie', 'radarr_ratings')
    op.drop_column('movie', 'radarr_studio')
    op.drop_column('movie', 'radarr_genres')
    op.drop_column('movie', 'radarr_certification')
    op.drop_column('movie', 'radarr_runtime')
    op.drop_column('movie', 'remote_fanart')
