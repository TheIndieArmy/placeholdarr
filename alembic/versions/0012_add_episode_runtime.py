"""add episode runtime

Revision ID: 0012_add_episode_runtime
Revises: 0011_add_instance_key_uniqueness
Create Date: 2026-04-12 22:18:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0012_add_episode_runtime'
down_revision = '0011_add_instance_key_uniqueness'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('episode', sa.Column('sonarr_runtime', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('episode', 'sonarr_runtime')
