"""TMDB Discover catalog tables (movies-first)

Revision ID: 0031_tmdb_discover
Revises: 0030_localized_poster
Create Date: 2026-09-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0031_tmdb_discover"
down_revision = "0030_localized_poster"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_source",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False, server_default="movie"),
        sa.Column("filters_json", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("run_interval_hours", sa.Integer(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_stats", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_catalog_source_enabled", "catalog_source", ["enabled"])

    op.create_table(
        "tmdb_movie",
        sa.Column("tmdb_id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("overview", sa.String(), nullable=True),
        sa.Column("poster_path", sa.String(), nullable=True),
        sa.Column("remote_poster", sa.String(), nullable=True),
        sa.Column("popularity", sa.Float(), nullable=True),
        sa.Column("vote_average", sa.Float(), nullable=True),
        sa.Column("vote_count", sa.Integer(), nullable=True),
        sa.Column("genre_ids", sa.JSON(), nullable=True),
        sa.Column("original_language", sa.String(), nullable=True),
        sa.Column("release_date", sa.String(), nullable=True),
        sa.Column("has_placeholder", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("placeholder_folder", sa.String(), nullable=True),
        sa.Column("placeholder_filepath", sa.String(), nullable=True),
        sa.Column("determination", sa.String(), nullable=True),
        sa.Column("determination_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_tmdb_movie_determination", "tmdb_movie", ["determination"])
    op.create_index("ix_tmdb_movie_title", "tmdb_movie", ["title"])

    op.create_table(
        "tmdb_movie_source",
        sa.Column("tmdb_id", sa.Integer(), sa.ForeignKey("tmdb_movie.tmdb_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("catalog_source.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "arr_movie_overlay",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tmdb_id", sa.Integer(), sa.ForeignKey("tmdb_movie.tmdb_id", ondelete="CASCADE"), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("instance_key", sa.String(), nullable=False),
        sa.Column("radarr_id", sa.Integer(), nullable=True),
        sa.Column("monitored", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("has_file", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("radarr_filepath", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tmdb_id", "instance_id", name="ux_arr_movie_overlay_tmdb_instance"),
    )
    op.create_index("ix_arr_movie_overlay_tmdb_id", "arr_movie_overlay", ["tmdb_id"])

    op.add_column(
        "placeholder",
        sa.Column("tmdb_movie_id", sa.Integer(), sa.ForeignKey("tmdb_movie.tmdb_id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_placeholder_tmdb_movie_id", "placeholder", ["tmdb_movie_id"])


def downgrade():
    op.drop_index("ix_placeholder_tmdb_movie_id", table_name="placeholder")
    op.drop_column("placeholder", "tmdb_movie_id")
    op.drop_index("ix_arr_movie_overlay_tmdb_id", table_name="arr_movie_overlay")
    op.drop_table("arr_movie_overlay")
    op.drop_table("tmdb_movie_source")
    op.drop_index("ix_tmdb_movie_title", table_name="tmdb_movie")
    op.drop_index("ix_tmdb_movie_determination", table_name="tmdb_movie")
    op.drop_table("tmdb_movie")
    op.drop_index("ix_catalog_source_enabled", table_name="catalog_source")
    op.drop_table("catalog_source")
