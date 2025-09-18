from core.logger import logger
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from core.config import settings

Base = declarative_base()

def get_engine():
    url = (
        f"postgresql://{settings.DB_USER}:{settings.DB_PASS}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    engine = create_engine(url, echo=False, future=True)
    return engine

def get_session(engine=None):
    engine = engine or get_engine()
    Session = sessionmaker(bind=engine, future=True)
    return Session()

def init_db(engine=None):
    engine = engine or get_engine()

    logger.info(f"Connecting to database at: {engine.url}", extra={'emoji_type': 'info'})

    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    logger.info(f"Existing tables BEFORE create_all(): {existing_tables}", extra={'emoji_type': 'info'})

    # Import models so they are registered with Base.metadata
    import services.postgres.models  # noqa: F401

    logger.info(f"Tables registered in Base.metadata: {list(Base.metadata.tables.keys())}", extra={'emoji_type': 'info'})

    # Create missing tables
    Base.metadata.create_all(bind=engine, checkfirst=True)

    # Add missing columns to existing tables
    _migrate_columns(engine, inspector)

    inspector = inspect(engine)
    created_tables = inspector.get_table_names()
    logger.info(f"Tables AFTER create_all(): {created_tables}", extra={'emoji_type': 'info'})


def _migrate_columns(engine, inspector):
    """Dynamically add missing columns for tables declared in SQLAlchemy models (add-only).

    Behavior:
    - Imports model metadata (already done in init_db) and compares each model table's
      declared columns against the actual DB columns returned by the inspector.
    - For any missing column, issues a safe `ALTER TABLE ADD COLUMN` statement and
      records the addition in the `auto_migrations` audit table.
    - Add-only: this function will not modify or drop existing columns or change types.
    """
    from sqlalchemy import text
    from datetime import datetime

    # Ensure we have up-to-date lists
    model_tables = list(Base.metadata.tables.keys())
    db_tables = inspector.get_table_names()

    logger.info(f"Starting dynamic schema migration: scanning {len(model_tables)} model tables", extra={'emoji_type': 'info'})

    # Create a small audit table to record auto-applied migrations (if not present)
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS auto_migrations (
                    id SERIAL PRIMARY KEY,
                    table_name VARCHAR(255) NOT NULL,
                    column_name VARCHAR(255) NOT NULL,
                    added_at TIMESTAMP NOT NULL DEFAULT now(),
                    source VARCHAR(50) NOT NULL DEFAULT 'startup'
                )
            """))
            conn.commit()
    except Exception as ex:
        logger.warning(f"Could not ensure auto_migrations table exists: {ex}", extra={'emoji_type': 'warning'})

    # Iterate model tables and add any missing columns (add-only)
    for tbl in model_tables:
        if tbl not in db_tables:
            # Table doesn't exist in DB - create_all() should have handled creation
            continue

        try:
            existing_cols = [c['name'] for c in inspector.get_columns(tbl)]
        except Exception:
            existing_cols = []

        for col in Base.metadata.tables[tbl].columns:
            if col.name in existing_cols:
                continue

            # Determine a safe SQL type mapping based on the sqlalchemy type name
            tname = type(col.type).__name__.lower()
            sql_type = 'TEXT'

            if 'boolean' in tname:
                sql_type = 'BOOLEAN'
            elif 'integer' in tname or 'int' in tname:
                sql_type = 'INTEGER'
            elif 'bigint' in tname:
                sql_type = 'BIGINT'
            elif 'numeric' in tname or 'decimal' in tname:
                sql_type = 'NUMERIC'
            elif 'float' in tname or 'real' in tname:
                sql_type = 'REAL'
            elif 'datetime' in tname or 'timestamp' in tname:
                sql_type = 'TIMESTAMP'
            elif 'json' in tname:
                # Prefer JSONB in Postgres
                sql_type = 'JSONB'
            elif 'text' in tname:
                sql_type = 'TEXT'
            elif 'varchar' in tname or 'string' in tname or 'char' in tname:
                length = getattr(col.type, 'length', None)
                sql_type = f"VARCHAR({length})" if length else 'VARCHAR(255)'
            else:
                # Fallback
                sql_type = 'TEXT'

            # Build ALTER statement (add as nullable to be safe)
            alter_sql = f'ALTER TABLE "{tbl}" ADD COLUMN "{col.name}" {sql_type}'

            # Execute and log
            try:
                with engine.connect() as conn:
                    logger.info(f"Adding column {tbl}.{col.name} {sql_type}", extra={'emoji_type': 'info'})
                    conn.execute(text(alter_sql))
                    # record audit
                    try:
                        conn.execute(text("INSERT INTO auto_migrations (table_name, column_name) VALUES (:t, :c)"), {"t": tbl, "c": col.name})
                    except Exception:
                        # Non-fatal - continue
                        pass
                    conn.commit()
                    logger.info(f"Successfully added column {tbl}.{col.name}", extra={'emoji_type': 'success'})
            except Exception as ex:
                logger.error(f"Failed to add column {tbl}.{col.name}: {ex}", extra={'emoji_type': 'error'})
