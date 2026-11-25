"""Discover collections: Arr added-at, auto-add ledger.

Revision ID: 0039_discover_collections
Revises: 0038_watchlist_excluded_at
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0039_discover_collections"
down_revision = "0038_watchlist_excluded_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("movie", sa.Column("radarr_added_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("series", sa.Column("sonarr_added_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "collection_auto_add",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("tvdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(32), nullable=True),
        sa.Column("instance_key", sa.String(64), nullable=False),
        sa.Column("arr_id", sa.Integer(), nullable=True),
        sa.Column("recipe_ids", sa.JSON(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_on_list_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
    )
    op.create_index(
        "ix_collection_auto_add_identity",
        "collection_auto_add",
        ["media_type", "tmdb_id", "tvdb_id", "instance_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_auto_add_identity", table_name="collection_auto_add")
    op.drop_table("collection_auto_add")
    op.drop_column("series", "sonarr_added_at")
    op.drop_column("movie", "radarr_added_at")