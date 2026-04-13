"""remove observation
Revision ID: 0013_remove_observation
Revises: 0012_add_episode_runtime
Create Date: 2026-04-12 23:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0013_remove_observation'
down_revision = '0012_add_episode_runtime'
branch_labels = None
depends_on = None

def upgrade():
    # Remove Plex-specific observation columns from placeholder table
    op.drop_column('placeholder', 'plex_placeholder_id')
    op.drop_column('placeholder', 'plex_id_observed_at')
    
    # Remove observation-related tables
    op.drop_table('observation_trail_attempt')
    op.drop_table('observation_flight')

def downgrade():
    # Restore Plex-specific observation columns
    op.add_column('placeholder', sa.Column('plex_placeholder_id', sa.String(), nullable=True))
    op.add_column('placeholder', sa.Column('plex_id_observed_at', sa.DateTime(timezone=True), nullable=True))
    
    # Restore observation_trail_attempt table
    op.create_table('observation_trail_attempt',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('trail_job_id', sa.Integer(), sa.ForeignKey('job.id'), nullable=False),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('attempt_number', sa.Integer(), nullable=False, default=1),
        sa.Column('placeholders_before', sa.Integer(), nullable=False, default=0),
        sa.Column('placeholders_after', sa.Integer(), nullable=False, default=0),
        sa.Column('resolved_delta', sa.Integer(), nullable=False, default=0),
        sa.Column('plex_busy_count', sa.Integer(), nullable=False, default=0),
        sa.Column('strict_keys_only', sa.Boolean(), default=True),
        sa.Column('elapsed_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'))
    )

    # Restore observation_flight table
    op.create_table('observation_flight',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('status', sa.String(), default='ACTIVE'),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True)
    )
