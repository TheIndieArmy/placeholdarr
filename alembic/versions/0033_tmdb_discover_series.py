"""TMDB Discover show-level series tables

Revision ID: 0033_tmdb_discover_series
Revises: 0032_placeholder_episode_lookup_indexes
Create Date: 2026-09-23 00:00:00.000000

Adds Discover TV catalog (``tmdb_series``), source membership, Sonarr overlay,
and ``placeholder.tmdb_series_id`` for show-level stubs (one dummy episode).
"""
from alembic import op
import sqlalchemy as sa


revision = "0033_tmdb_discover_series"
down_revision = "0032_placeholder_episode_lookup_indexes"
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

    if not _has_table(inspector, "tmdb_series"):
        op.create_table(
            "tmdb_series",
            sa.Column("tmdb_id", sa.Integer(), primary_key=True),
            sa.Column("tvdb_id", sa.Integer(), nullable=True),
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
            sa.Column("first_air_date", sa.String(), nullable=True),
            sa.Column("has_placeholder", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("placeholder_folder", sa.String(), nullable=True),
            sa.Column("placeholder_filepath", sa.String(), nullable=True),
            sa.Column("determination", sa.String(), nullable=True),
            sa.Column("determination_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )
        inspector = sa.inspect(bind)

    if _has_table(inspector, "tmdb_series"):
        if not _has_index(inspector, "tmdb_series", "ix_tmdb_series_determination"):
            op.create_index("ix_tmdb_series_determination", "tmdb_series", ["determination"])
        if not _has_index(inspector, "tmdb_series", "ix_tmdb_series_title"):
            op.create_index("ix_tmdb_series_title", "tmdb_series", ["title"])
        if not _has_index(inspector, "tmdb_series", "ix_tmdb_series_tvdb_id"):
            op.create_index("ix_tmdb_series_tvdb_id", "tmdb_series", ["tvdb_id"])

    if not _has_table(inspector, "tmdb_series_source"):
        op.create_table(
            "tmdb_series_source",
            sa.Column(
                "tmdb_id",
                sa.Integer(),
                sa.ForeignKey("tmdb_series.tmdb_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "source_id",
                sa.Integer(),
                sa.ForeignKey("catalog_source.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )
        inspector = sa.inspect(bind)

    if not _has_table(inspector, "arr_series_overlay"):
        op.create_table(
            "arr_series_overlay",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tmdb_id",
                sa.Integer(),
                sa.ForeignKey("tmdb_series.tmdb_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("instance_key", sa.String(), nullable=False),
            sa.Column("sonarr_id", sa.Integer(), nullable=True),
            sa.Column("monitored", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("has_file", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("sonarr_filepath", sa.String(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )
        inspector = sa.inspect(bind)

    if _has_table(inspector, "arr_series_overlay"):
        if not _has_index(inspector, "arr_series_overlay", "ix_arr_series_overlay_tmdb_id"):
            op.create_index("ix_arr_series_overlay_tmdb_id", "arr_series_overlay", ["tmdb_id"])
        if not _has_index(inspector, "arr_series_overlay", "ux_arr_series_overlay_tmdb_instance"):
            op.create_index(
                "ux_arr_series_overlay_tmdb_instance",
                "arr_series_overlay",
                ["tmdb_id", "instance_id"],
                unique=True,
            )

    if _has_table(inspector, "placeholder") and not _has_column(inspector, "placeholder", "tmdb_series_id"):
        op.add_column("placeholder", sa.Column("tmdb_series_id", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)

    if (
        _has_table(inspector, "placeholder")
        and _has_table(inspector, "tmdb_series")
        and _has_column(inspector, "placeholder", "tmdb_series_id")
        and not _has_fk(inspector, "placeholder", ["tmdb_series_id"])
    ):
        op.create_foreign_key(
            "placeholder_tmdb_series_id_fkey",
            "placeholder",
            "tmdb_series",
            ["tmdb_series_id"],
            ["tmdb_id"],
            ondelete="SET NULL",
        )
        inspector = sa.inspect(bind)

    if (
        _has_table(inspector, "placeholder")
        and _has_column(inspector, "placeholder", "tmdb_series_id")
        and not _has_index(inspector, "placeholder", "ix_placeholder_tmdb_series_id")
    ):
        op.create_index("ix_placeholder_tmdb_series_id", "placeholder", ["tmdb_series_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "placeholder", "ix_placeholder_tmdb_series_id"):
        op.drop_index("ix_placeholder_tmdb_series_id", table_name="placeholder")
    if _has_fk(inspector, "placeholder", ["tmdb_series_id"]):
        op.drop_constraint("placeholder_tmdb_series_id_fkey", "placeholder", type_="foreignkey")
    if _has_column(inspector, "placeholder", "tmdb_series_id"):
        op.drop_column("placeholder", "tmdb_series_id")

    if _has_table(inspector, "arr_series_overlay"):
        op.drop_table("arr_series_overlay")
    if _has_table(inspector, "tmdb_series_source"):
        op.drop_table("tmdb_series_source")
    if _has_table(inspector, "tmdb_series"):
        op.drop_table("tmdb_series")
