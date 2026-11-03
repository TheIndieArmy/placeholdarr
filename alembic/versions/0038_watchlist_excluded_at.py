"""Add excluded_at on plex_watchlist_item for Reject timing.

Revision ID: 0038_watchlist_excluded_at
Revises: 0037_plex_watchlist_user_roster
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0038_watchlist_excluded_at"
down_revision = "0037_plex_watchlist_user_roster"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plex_watchlist_item",
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Best-effort backfill: previously Reject only bumped updated_at.
    op.execute(
        """
        UPDATE plex_watchlist_item
        SET excluded_at = updated_at
        WHERE excluded IS TRUE
          AND excluded_at IS NULL
          AND updated_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("plex_watchlist_item", "excluded_at")
