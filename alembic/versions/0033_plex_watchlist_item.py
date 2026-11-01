"""Add plex_watchlist_item table for Plex Watchlist sync.

Revision ID: 0033_plex_watchlist_item
Revises: 0032_collection_recipe_run_schedule
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON


revision = "0033_plex_watchlist_item"
down_revision = "0032_collection_recipe_run_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plex_watchlist_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("identity_key", sa.String(length=640), nullable=False),
        sa.Column("plex_guid", sa.String(length=512), nullable=True),
        sa.Column("rating_key", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("tvdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("arr_instance_key", sa.String(length=128), nullable=True),
        sa.Column("extra", JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plex_watchlist_item_status", "plex_watchlist_item", ["status"])
    op.create_index("ix_plex_watchlist_item_media_type", "plex_watchlist_item", ["media_type"])
    op.create_index(
        "ux_plex_watchlist_item_identity_key",
        "plex_watchlist_item",
        ["identity_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_plex_watchlist_item_identity_key", table_name="plex_watchlist_item")
    op.drop_index("ix_plex_watchlist_item_media_type", table_name="plex_watchlist_item")
    op.drop_index("ix_plex_watchlist_item_status", table_name="plex_watchlist_item")
    op.drop_table("plex_watchlist_item")
