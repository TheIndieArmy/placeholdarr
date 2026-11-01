"""Placeholder for env DB stamp; keeps alembic graph continuous after 0034.

Revision ID: 0035_merge_heads_list_recipe
Revises: 0034_plex_watchlist_users

This revision exists because some environments already have
``0035_merge_heads_list_recipe`` in ``alembic_version`` without the original
file. Schema changes for Home users / membership live in 0034.
"""

from __future__ import annotations

revision = "0035_merge_heads_list_recipe"
down_revision = "0034_plex_watchlist_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
