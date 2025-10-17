"""fix updated_at trigger to preserve old updated_at when only excluded cols change

Revision ID: 0006_fix_updated_at_trigger_preserve_old
Revises: 0005_update_updated_at_trigger
Create Date: 2025-10-17 01:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0006_fix_updated_at_trigger_preserve_old'
down_revision = '0005_update_updated_at_trigger'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    conn.execute(sa.text("""
    CREATE OR REPLACE FUNCTION touch_updated_at_if_significant_change()
    RETURNS trigger AS $$
    DECLARE
      excluded_cols text[] := ARRAY['updated_at','last_found_in_sonarr','last_found_in_radarr','last_found_in_sonarr'];
      old_row jsonb := to_jsonb(OLD) - excluded_cols;
      new_row jsonb := to_jsonb(NEW) - excluded_cols;
    BEGIN
      IF old_row IS DISTINCT FROM new_row THEN
        NEW.updated_at = now();
      ELSE
        -- Preserve previous updated_at when only excluded columns (e.g., last_found) changed
        NEW.updated_at = OLD.updated_at;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """))


def downgrade():
    conn = op.get_bind()
    # Restore previous function (the older version simply set updated_at only when distinct)
    conn.execute(sa.text("""
    CREATE OR REPLACE FUNCTION touch_updated_at_if_significant_change()
    RETURNS trigger AS $$
    DECLARE
      excluded_cols text[] := ARRAY['updated_at','last_found_in_sonarr','last_found_in_radarr','last_found_in_sonarr'];
      old_row jsonb := to_jsonb(OLD) - excluded_cols;
      new_row jsonb := to_jsonb(NEW) - excluded_cols;
    BEGIN
      IF old_row IS DISTINCT FROM new_row THEN
        NEW.updated_at = now();
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """))
