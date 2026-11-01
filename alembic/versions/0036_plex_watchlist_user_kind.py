"""Add kind column to plex_watchlist_user (owner/friend/home).

Revision ID: 0036_plex_watchlist_user_kind
Revises: 0035_merge_heads_list_recipe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0036_plex_watchlist_user_kind"
down_revision = "0035_merge_heads_list_recipe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plex_watchlist_user",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="unknown"),
    )
    op.create_index("ix_plex_watchlist_user_kind", "plex_watchlist_user", ["kind"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_plex_watchlist_user_kind", table_name="plex_watchlist_user")
    op.drop_column("plex_watchlist_user", "kind")
