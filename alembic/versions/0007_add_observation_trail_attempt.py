"""add observation trail attempt table

Revision ID: 0007_add_observation_trail_attempt
Revises: 0006_fix_updated_at_trigger_preserve_old
Create Date: 2026-03-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0007_add_observation_trail_attempt'
down_revision = '0006_fix_updated_at_trigger_preserve_old'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'observation_trail_attempt',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('trail_job_id', sa.Integer(), sa.ForeignKey('job.id'), nullable=False),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('attempt_number', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('placeholders_before', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('placeholders_after', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('observed_plex', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('observed_jellyfin', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('observed_emby', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('observe_failed', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('elapsed_ms', sa.Integer(), nullable=True),
        sa.Column('resolution_reason', sa.String(), nullable=True),
        sa.Column('unresolved_placeholder_ids', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('now()')),
    )
    op.create_index(
        'ix_observation_trail_attempt_job_created',
        'observation_trail_attempt',
        ['trail_job_id', 'created_at'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_observation_trail_attempt_job_created', table_name='observation_trail_attempt')
    op.drop_table('observation_trail_attempt')
