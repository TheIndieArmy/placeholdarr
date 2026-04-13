"""add refresh throttle

Revision ID: 0014_add_refresh_throttle
Revises: 0013_remove_observation
Create Date: 2026-04-12 23:18:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0014_add_refresh_throttle'
down_revision = '0013_remove_observation'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'library_refresh_throttle',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('section_id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_library_refresh_throttle_section_id', 'library_refresh_throttle', ['section_id'], unique=True)


def downgrade():
    op.drop_index('ix_library_refresh_throttle_section_id', table_name='library_refresh_throttle')
    op.drop_table('library_refresh_throttle')
