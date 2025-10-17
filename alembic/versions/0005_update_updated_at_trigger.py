"""add trigger to avoid updating updated_at on last_found-only changes

Revision ID: 0005_update_updated_at_trigger
Revises: 0004_add_season_last_found
Create Date: 2025-10-17 00:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0005_update_updated_at_trigger'
down_revision = '0004_add_season_last_found'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # Create a function that updates updated_at only when columns other than
    # the last-found timestamps have changed. We'll compare row values using
    # hstore for convenience; hstore is available in most PG installs but we
    # fallback to a simple approach if not present.
    # This function will be attached to tables that have an updated_at column.

    conn.execute(sa.text("""
    CREATE OR REPLACE FUNCTION touch_updated_at_if_significant_change()
    RETURNS trigger AS $$
    DECLARE
      excluded_cols text[] := ARRAY['updated_at','last_found_in_sonarr','last_found_in_radarr','last_found_in_sonarr'];
      -- We'll compare the OLD and NEW rows after removing the excluded columns
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

    # Attach the trigger to the tables where we want this behavior.
    # Add safe-if-not-exists checks to avoid duplicate triggers when re-running.
    for tbl in ('movie','series','season','episode','subflow','placeholder','job'):
        conn.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_touch_updated_at_{tbl} ON {tbl};"))
        conn.execute(sa.text(f"CREATE TRIGGER trg_touch_updated_at_{tbl} BEFORE UPDATE ON {tbl} FOR EACH ROW EXECUTE PROCEDURE touch_updated_at_if_significant_change();"))


def downgrade():
    conn = op.get_bind()
    for tbl in ('movie','series','season','episode','subflow','placeholder','job'):
        conn.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_touch_updated_at_{tbl} ON {tbl};"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS touch_updated_at_if_significant_change();"))
