"""Collection auto-add per-collection keys.

Revision ID: 0040_collection_auto_add_keys
Revises: 0039_discover_collections
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0040_collection_auto_add_keys"
down_revision = "0039_discover_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_auto_add", sa.Column("collection_keys", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("collection_auto_add", "collection_keys")