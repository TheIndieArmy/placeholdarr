"""collection recipe table for rule-based Plex collections

Revision ID: 0022_collection_recipe
Revises: 0021_series_episode_stats
Create Date: 2026-06-10 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0022_collection_recipe"
down_revision = "0021_series_episode_stats"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collection_recipe",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("plex_section_id", sa.Integer(), nullable=False),
        sa.Column("plex_section_type", sa.String(length=16), nullable=False),
        sa.Column("collection_title", sa.String(length=200), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collection_recipe_enabled", "collection_recipe", ["enabled"], unique=False)


def downgrade():
    op.drop_index("ix_collection_recipe_enabled", table_name="collection_recipe")
    op.drop_table("collection_recipe")
