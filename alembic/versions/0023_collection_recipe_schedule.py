"""per-recipe schedule override + seasonal active window for collection recipes

Revision ID: 0023_collection_recipe_schedule
Revises: 0022_collection_recipe
Create Date: 2026-06-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0023_collection_recipe_schedule"
down_revision = "0022_collection_recipe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_recipe", sa.Column("run_interval_hours", sa.Integer(), nullable=True))
    op.add_column("collection_recipe", sa.Column("active_window", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("collection_recipe", "active_window")
    op.drop_column("collection_recipe", "run_interval_hours")
