"""drop content placeholder_status columns

Revision ID: 0008_drop_content_placeholder_status
Revises: 0007_add_observation_trail_attempt
Create Date: 2026-03-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0008_drop_content_placeholder_status'
down_revision = '0007_add_observation_trail_attempt'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('movie', 'placeholder_status')
    op.drop_column('series', 'placeholder_status')
    op.drop_column('season', 'placeholder_status')
    op.drop_column('episode', 'placeholder_status')


def downgrade():
    op.add_column('movie', sa.Column('placeholder_status', sa.String(), nullable=True))
    op.add_column('series', sa.Column('placeholder_status', sa.String(), nullable=True))
    op.add_column('season', sa.Column('placeholder_status', sa.String(), nullable=True))
    op.add_column('episode', sa.Column('placeholder_status', sa.String(), nullable=True))
