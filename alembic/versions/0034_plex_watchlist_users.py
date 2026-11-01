"""Plex Home users + watchlist membership for multi-user watchlist.

Revision ID: 0034_plex_watchlist_users
Revises: 0033_plex_watchlist_item
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON


revision = "0034_plex_watchlist_users"
down_revision = "0033_plex_watchlist_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plex_watchlist_user",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plex_user_id", sa.String(length=64), nullable=False),
        sa.Column("plex_uuid", sa.String(length=64), nullable=True),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("thumb_url", sa.String(length=1024), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_restricted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_protected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Future per-user Arr / on_new overrides (null = account defaults).
        sa.Column("settings_override", JSON(), nullable=True),
        sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.String(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plex_user_id", name="uq_plex_watchlist_user_plex_user_id"),
    )
    op.create_index("ix_plex_watchlist_user_title", "plex_watchlist_user", ["title"])

    op.create_table(
        "plex_watchlist_membership",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("watchlist_item_id", sa.Integer(), nullable=False),
        sa.Column("plex_user_id", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"],
            ["plex_watchlist_item.id"],
            name="fk_plex_watchlist_membership_item",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "watchlist_item_id",
            "plex_user_id",
            name="uq_plex_watchlist_membership_item_user",
        ),
    )
    op.create_index(
        "ix_plex_watchlist_membership_plex_user_id",
        "plex_watchlist_membership",
        ["plex_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_plex_watchlist_membership_plex_user_id", table_name="plex_watchlist_membership")
    op.drop_table("plex_watchlist_membership")
    op.drop_index("ix_plex_watchlist_user_title", table_name="plex_watchlist_user")
    op.drop_table("plex_watchlist_user")
