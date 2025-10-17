"""add migration_history table

Revision ID: 0002_migration_history
Revises: 0001_initial
Create Date: 2025-10-17 00:05:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0002_migration_history'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'migration_history',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('applied_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('details', sa.JSON, nullable=True),
    )


def downgrade():
    op.drop_table('migration_history')
