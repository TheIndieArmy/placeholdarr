"""TMDB Discover catalog tables (coexist with Arr movie catalog)

Revision ID: 0031_tmdb_discover_tables
Revises: 0030_localized_poster
Create Date: 2026-09-22 00:00:00.000000

Adds Discover-specific tables so the same Postgres database can hold Arr
``movie`` rows and TMDB Discover ``tmdb_movie`` / overlay rows. Catalog mode
(``CATALOG_MODE``) chooses which catalog drives placeholders and sync; it does
not require a separate database.
"""
from alembic import op
import sqlalchemy as sa


revision = "0031_tmdb_discover_tables"
down_revision = "0030_localized_poster"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _has_index(inspector, table: str, name: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return name in {idx["name"] for idx in inspector.get_indexes(table)}


def _has_fk(inspector, table: str, constrained_columns: list[str]) -> bool:
    if not _has_table(inspector, table):
        return False
    want = set(constrained_columns)
    for fk in inspector.get_foreign_keys(table):
        if set(fk.get("constrained_columns") or []) == want:
            return True
    return False


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "catalog_source"):
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
        inspector = sa.inspect(bind)

    if _has_table(inspector, "catalog_source") and not _has_index(
        inspector, "catalog_source", "ix_catalog_source_enabled"
    ):
        op.create_index("ix_catalog_source_enabled", "catalog_source", ["enabled"])

    if not _has_table(inspector, "tmdb_movie"):
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
        inspector = sa.inspect(bind)

    if _has_table(inspector, "tmdb_movie"):
        if not _has_index(inspector, "tmdb_movie", "ix_tmdb_movie_determination"):
            op.create_index("ix_tmdb_movie_determination", "tmdb_movie", ["determination"])
        if not _has_index(inspector, "tmdb_movie", "ix_tmdb_movie_title"):
            op.create_index("ix_tmdb_movie_title", "tmdb_movie", ["title"])

    if not _has_table(inspector, "tmdb_movie_source"):
        op.create_table(
            "tmdb_movie_source",
            sa.Column("tmdb_id", sa.Integer(), sa.ForeignKey("tmdb_movie.tmdb_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("catalog_source.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )
        inspector = sa.inspect(bind)

    if not _has_table(inspector, "arr_movie_overlay"):
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
        )
        inspector = sa.inspect(bind)

    if _has_table(inspector, "arr_movie_overlay"):
        if not _has_index(inspector, "arr_movie_overlay", "ix_arr_movie_overlay_tmdb_id"):
            op.create_index("ix_arr_movie_overlay_tmdb_id", "arr_movie_overlay", ["tmdb_id"])
        if not _has_index(inspector, "arr_movie_overlay", "ux_arr_movie_overlay_tmdb_instance"):
            op.create_index(
                "ux_arr_movie_overlay_tmdb_instance",
                "arr_movie_overlay",
                ["tmdb_id", "instance_id"],
                unique=True,
            )

    if _has_table(inspector, "placeholder") and not _has_column(inspector, "placeholder", "tmdb_movie_id"):
        op.add_column("placeholder", sa.Column("tmdb_movie_id", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)

    if (
        _has_table(inspector, "placeholder")
        and _has_table(inspector, "tmdb_movie")
        and _has_column(inspector, "placeholder", "tmdb_movie_id")
        and not _has_fk(inspector, "placeholder", ["tmdb_movie_id"])
    ):
        op.create_foreign_key(
            "placeholder_tmdb_movie_id_fkey",
            "placeholder",
            "tmdb_movie",
            ["tmdb_movie_id"],
            ["tmdb_id"],
            ondelete="SET NULL",
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_fk(inspector, "placeholder", ["tmdb_movie_id"]):
        op.drop_constraint("placeholder_tmdb_movie_id_fkey", "placeholder", type_="foreignkey")
    if _has_column(inspector, "placeholder", "tmdb_movie_id"):
        op.drop_column("placeholder", "tmdb_movie_id")

    if _has_table(inspector, "arr_movie_overlay"):
        op.drop_table("arr_movie_overlay")
    if _has_table(inspector, "tmdb_movie_source"):
        op.drop_table("tmdb_movie_source")
    if _has_table(inspector, "tmdb_movie"):
        op.drop_table("tmdb_movie")
    if _has_table(inspector, "catalog_source"):
        op.drop_table("catalog_source")
