"""Add roster relationship / fetch_method / block_reason on plex_watchlist_user.

Revision ID: 0037_plex_watchlist_user_roster
Revises: 0036_plex_watchlist_user_kind
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0037_plex_watchlist_user_roster"
down_revision = "0036_plex_watchlist_user_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plex_watchlist_user",
        sa.Column("relationship", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "plex_watchlist_user",
        sa.Column("fetch_method", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "plex_watchlist_user",
        sa.Column("block_reason", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_plex_watchlist_user_relationship",
        "plex_watchlist_user",
        ["relationship"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_plex_watchlist_user_relationship", table_name="plex_watchlist_user")
    op.drop_column("plex_watchlist_user", "block_reason")
    op.drop_column("plex_watchlist_user", "fetch_method")
    op.drop_column("plex_watchlist_user", "relationship")
