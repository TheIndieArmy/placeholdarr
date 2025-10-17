"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2025-10-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Use SQLAlchemy metadata to create all tables defined in models
    from services.postgres.db import get_engine, Base
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def downgrade():
    from services.postgres.db import get_engine, Base
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
