"""series episode stats columns, library catalog version, lookup indexes

Revision ID: 0021_series_episode_stats
Revises: 0020_add_season_remote_poster
Create Date: 2026-05-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0021_series_episode_stats"
down_revision = "0020_add_season_remote_poster"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("series", sa.Column("episode_total", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("series", sa.Column("episode_files", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("series", sa.Column("episode_placeholders", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("series", sa.Column("episode_future", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("series", sa.Column("episode_missing", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("series", sa.Column("stats_computed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "library_catalog_version",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("movies_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("series_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO library_catalog_version (id, movies_version, series_version) VALUES (1, 0, 0)"
    )

    op.create_index(
        "ix_episode_season_id_active",
        "episode",
        ["season_id"],
        unique=False,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index(
        "ix_season_series_id_active",
        "season",
        ["series_id"],
        unique=False,
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index(
        "ix_movie_library_list",
        "movie",
        ["is_deleted", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_series_library_list",
        "series",
        ["is_deleted", "updated_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_series_library_list", table_name="series")
    op.drop_index("ix_movie_library_list", table_name="movie")
    op.drop_index("ix_season_series_id_active", table_name="season")
    op.drop_index("ix_episode_season_id_active", table_name="episode")
    op.drop_table("library_catalog_version")
    op.drop_column("series", "stats_computed_at")
    op.drop_column("series", "episode_missing")
    op.drop_column("series", "episode_future")
    op.drop_column("series", "episode_placeholders")
    op.drop_column("series", "episode_files")
    op.drop_column("series", "episode_total")
