"""Add placeholder FK and episode.season_id lookup indexes

Revision ID: 0032_placeholder_episode_lookup_indexes
Revises: 0031_tmdb_discover_tables
Create Date: 2026-09-22 00:00:00.000000

Library and series-stats queries join placeholder/episode by foreign keys.
Without these indexes Postgres falls back to sequential scans (painful once
the buffer cache is cold after heavy Discover materialize I/O).
"""
from alembic import op
import sqlalchemy as sa


revision = "0032_placeholder_episode_lookup_indexes"
down_revision = "0031_tmdb_discover_tables"
branch_labels = None
depends_on = None


def _has_index(inspector, table: str, name: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return name in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    indexes = (
        ("placeholder", "ix_placeholder_movie_id", ["movie_id"]),
        ("placeholder", "ix_placeholder_episode_id", ["episode_id"]),
        ("placeholder", "ix_placeholder_series_id", ["series_id"]),
        ("placeholder", "ix_placeholder_season_id", ["season_id"]),
        ("placeholder", "ix_placeholder_tmdb_movie_id", ["tmdb_movie_id"]),
        ("placeholder", "ix_placeholder_path", ["path"]),
        ("episode", "ix_episode_season_id", ["season_id"]),
        ("episode", "ix_episode_status", ["status"]),
        ("movie", "ix_movie_status", ["status"]),
        ("series", "ix_series_status", ["status"]),
    )
    for table, name, cols in indexes:
        if table not in inspector.get_table_names():
            continue
        if _has_index(inspector, table, name):
            continue
        op.create_index(name, table, cols)
        inspector = sa.inspect(bind)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, name in (
        ("series", "ix_series_status"),
        ("movie", "ix_movie_status"),
        ("episode", "ix_episode_status"),
        ("episode", "ix_episode_season_id"),
        ("placeholder", "ix_placeholder_path"),
        ("placeholder", "ix_placeholder_tmdb_movie_id"),
        ("placeholder", "ix_placeholder_season_id"),
        ("placeholder", "ix_placeholder_series_id"),
        ("placeholder", "ix_placeholder_episode_id"),
        ("placeholder", "ix_placeholder_movie_id"),
    ):
        if _has_index(inspector, table, name):
            op.drop_index(name, table_name=table)
