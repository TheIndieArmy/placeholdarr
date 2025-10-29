from core.logger import logger
from sqlalchemy import create_engine, inspect, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
from core.config import settings
import os

Base = declarative_base()

# Module-level engine and session factory singletons to avoid creating
# a new connection pool on every call (which exhausted DB connections).
_engine = None
_SessionFactory = None


def get_engine():
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = (
        f"postgresql://{settings.DB_USER}:{settings.DB_PASS}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    # Configure a conservative connection pool. These values can be tuned
    # later based on your Postgres max_connections setting. Using a single
    # engine instance prevents many pools from being created and hitting
    # the "too many clients" error.
    _engine = create_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    # Ensure the DB session / display timezone is set to UTC on every new connection.
    # This makes TIMESTAMPTZ values display consistently as UTC and avoids
    # surprises when the Postgres server default_timezone differs from the app.
    def _set_utc_timezone(dbapi_connection, connection_record):
        try:
            # dbapi_connection is a raw DB-API connection (psycopg2 or psycopg)
            cur = dbapi_connection.cursor()
            cur.execute("SET TIME ZONE 'UTC'")
            cur.close()
        except Exception:
            # Non-fatal; if this fails, connections will use server timezone.
            pass

    event.listen(_engine, "connect", _set_utc_timezone)
    # also prepare a Session factory so get_session can use it
    _SessionFactory = sessionmaker(bind=_engine, future=True)
    return _engine


def get_session(engine=None):
    """Return a SQLAlchemy Session from a module-level session factory.

    Avoid creating new engines/sessionmakers on each call. Callers should
    close() the returned session when finished.
    """
    global _engine, _SessionFactory
    if _SessionFactory is None:
        # ensure engine and session factory initialized
        _engine = get_engine()
    return _SessionFactory()

def init_db(engine=None, convert_ts: bool | None = None):
    """Initialize DB schema and optionally convert naive timestamp columns to timestamptz.

    By default conversion is opt-in. To force conversion at startup set the
    environment variable CONVERT_TS=1 or call init_db(convert_ts=True).
    """
    engine = engine or get_engine()

    logger.info(f"Connecting to database at: {engine.url}", extra={'emoji_type': 'info'})

    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    logger.info(f"Existing tables BEFORE create_all(): {existing_tables}", extra={'emoji_type': 'info'})

    # Import models so they are registered with Base.metadata
    import services.postgres.models  # noqa: F401

    logger.info(f"Tables registered in Base.metadata: {list(Base.metadata.tables.keys())}", extra={'emoji_type': 'info'})

    # Runtime schema management: create tables and apply add-only migrations.
    #
    # Historically this project delegated schema changes to Alembic. Per the
    # repository configuration for development/testing we now create any
    # missing tables and add missing columns on startup. This is an
    # add-only migration path intended for local/dev/test usage. If you run
    # multiple instances, we acquire a Postgres advisory lock to avoid race
    # conditions when creating or altering schema concurrently.
    logger.info("Runtime schema management: create tables + apply add-only migrations", extra={'emoji_type': 'info'})

    # Acquire an advisory lock so concurrent processes don't run create/migrate
    # at the same time. The numeric key is arbitrary but should be stable.
    lock_key = 987654321
    got_lock = False
    try:
        with engine.connect() as conn:
            # Try to acquire advisory lock with a few short retries to handle
            # races when multiple processes start at nearly the same time.
            attempts = 0
            while attempts < 5 and not got_lock:
                try:
                    got_lock = bool(conn.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': lock_key}).scalar())
                except Exception:
                    got_lock = False
                if not got_lock:
                    import time
                    time.sleep(0.25)
                    attempts += 1

            if not got_lock:
                logger.warning('Could not acquire advisory lock for runtime schema operations after retries; proceeding anyway (race risk)', extra={'emoji_type': 'warning'})

            # Import models so they are registered with Base.metadata
            import services.postgres.models  # noqa: F401

            logger.info(f"Creating tables from SQLAlchemy models: {list(Base.metadata.tables.keys())}", extra={'emoji_type': 'info'})
            try:
                Base.metadata.create_all(bind=engine)
            except Exception as ex:
                logger.error(f"Failed to create tables via create_all(): {ex}", extra={'emoji_type': 'error'})

            # Refresh inspector and run add-only column migrations
            inspector = inspect(engine)
            try:
                _migrate_columns(engine, inspector)
            except Exception as ex:
                logger.error(f"Runtime add-only column migration failed: {ex}", extra={'emoji_type': 'error'})

            # Optionally perform timestamp conversions if configured
            try:
                if os.getenv('CONVERT_TS') == '1':
                    _convert_timestamp_columns(engine, inspector)
            except Exception as ex:
                logger.error(f"Runtime timestamp conversion failed: {ex}", extra={'emoji_type': 'error'})

            # Release advisory lock if we acquired it
            if got_lock:
                try:
                    conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': lock_key})
                except Exception:
                    pass
    except Exception as ex:
        logger.error(f"Runtime schema management failed: {ex}", extra={'emoji_type': 'error'})

    # Ensure critical partial unique indexes exist (add-only). These indexes
    # are required for atomic ON CONFLICT upserts used by the enqueue logic.
    try:
        from sqlalchemy import text
        idx_sql = text(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_job_enrichment_groupid "
            "ON job(group_id) WHERE job_type='enrichment' AND status IN ('PENDING','CLAIMED','WORKING')"
        )
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
            conn.execute(idx_sql)
    except Exception as ex:
        logger.debug(f"Could not ensure ux_job_enrichment_groupid index exists: {ex}", extra={'emoji_type': 'debug'})

    # Ensure unique partial index to prevent multiple active SubFlows for same (movie_id, action, branch)
    try:
        from sqlalchemy import text
        idx_sql2 = text(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_subflow_movie_action_branch_active "
            "ON subflow(movie_id, action, branch) WHERE movie_id IS NOT NULL AND status IN ('PENDING','IN_QUEUE','CLAIMED')"
        )
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
            conn.execute(idx_sql2)
    except Exception as ex:
        logger.debug(f"Could not ensure ux_subflow_movie_action_branch_active index exists: {ex}", extra={'emoji_type': 'debug'})

    # Ensure indexes to speed Sonarr lookups (sonarrid) for series and episodes
    try:
        from sqlalchemy import text
        idx_series_sonarr = text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_series_sonarrid ON series(sonarrid)"
        )
        idx_episode_sonarr = text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_episode_sonarrid ON episode(sonarrid)"
        )
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
            conn.execute(idx_series_sonarr)
            conn.execute(idx_episode_sonarr)
    except Exception as ex:
        logger.debug(f"Could not ensure sonarrid indexes exist: {ex}", extra={'emoji_type': 'debug'})

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
                # If the model column has timezone=True, create a timestamptz
                # (Postgres: TIMESTAMP WITH TIME ZONE). Otherwise create a
                # plain TIMESTAMP (without timezone).
                try:
                    has_tz = bool(getattr(col.type, 'timezone', False))
                except Exception:
                    has_tz = False

                if has_tz:
                    sql_type = 'TIMESTAMP WITH TIME ZONE'
                else:
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

        # Detect and remove legacy global-unique indexes that conflict with newer model semantics.
        # Historically the code incorrectly created unique indexes on season_number and episode_number
        # across the whole table. That prevents inserting multiple episodes with episode_number=1
        # (for different seasons). We will drop those unique indexes if present.
        try:
            idxs = inspector.get_indexes(tbl)
        except Exception:
            idxs = []

        problematic = [
            # (table, column_name)
            ('season', 'season_number'),
            ('episode', 'episode_number'),
        ]
        for (tname, colname) in problematic:
            if tname != tbl:
                continue
            for idx in idxs:
                # Some inspectors return 'unique' key, some may omit it
                is_unique = idx.get('unique', False)
                cols = idx.get('column_names') or idx.get('column_names', [])
                # Normalize column names list
                if isinstance(cols, list) and cols == [colname] and is_unique:
                    idx_name = idx.get('name')
                    if idx_name:
                        drop_sql = f'DROP INDEX IF EXISTS "{idx_name}"'
                        try:
                            with engine.connect() as conn:
                                logger.info(f"Dropping legacy unique index {idx_name} on {tname}({colname})", extra={'emoji_type': 'info'})
                                conn.execute(text(drop_sql))
                                conn.commit()
                                logger.info(f"Dropped index {idx_name}", extra={'emoji_type': 'success'})
                        except Exception as ex:
                            logger.error(f"Failed to drop index {idx_name}: {ex}", extra={'emoji_type': 'error'})
                    else:
                        logger.debug(f"Found problematic unique index on {tname}.{colname} but no name returned by inspector", extra={'emoji_type': 'debug'})


def _convert_timestamp_columns(engine, inspector, source_tz='UTC'):
    """Convert timestamp columns without timezone to timestamptz for model-declared DateTime(timezone=True).

    This function is destructive in the sense that it changes the column type.
    It assumes existing naive/without-timezone values should be interpreted as
    `source_tz` when converting. If your existing values are actually UTC,
    use source_tz='UTC' (the default). If they are local server time, set
    source_tz accordingly before running.
    """
    from sqlalchemy import text

    logger.info("Scanning for timestamp columns to convert to TIMESTAMP WITH TIME ZONE", extra={'emoji_type': 'info'})

    model_tables = list(Base.metadata.tables.keys())

    for tbl in model_tables:
        if tbl not in inspector.get_table_names():
            continue

        try:
            db_cols = inspector.get_columns(tbl)
        except Exception:
            db_cols = []

        db_col_map = {c['name']: c for c in db_cols}

        for col in Base.metadata.tables[tbl].columns:
            # Only consider DateTime columns where the model declares timezone=True
            tname = type(col.type).__name__.lower()
            if not ('datetime' in tname or 'timestamp' in tname):
                continue
            has_tz = bool(getattr(col.type, 'timezone', False))
            if not has_tz:
                continue

            if col.name not in db_col_map:
                # column missing — handled by _migrate_columns
                continue

            db_col = db_col_map[col.name]
            # Inspector's 'type' may describe Postgres type; look for 'timestamp without time zone'
            db_type = str(db_col.get('type') or '').lower()

            if 'timestamp without time zone' in db_type or ('timestamp' in db_type and 'with time zone' not in db_type):
                alter_sql = f'ALTER TABLE "{tbl}" ALTER COLUMN "{col.name}" TYPE TIMESTAMP WITH TIME ZONE USING ("{col.name}" AT TIME ZONE :src)'
                # Skip conversion if we've already recorded it in auto_migrations.
                already_converted = False
                try:
                    with engine.connect() as conn:
                        res = conn.execute(text("SELECT 1 FROM auto_migrations WHERE table_name=:t AND column_name=:c AND source='convert_to_timestamptz' LIMIT 1"), {"t": tbl, "c": col.name}).fetchone()
                        already_converted = bool(res)
                except Exception:
                    # If the audit table/query fails, be conservative and proceed with conversion.
                    already_converted = False

                if already_converted:
                    logger.debug(f"Skipping conversion for {tbl}.{col.name} because auto_migrations records it", extra={'emoji_type': 'debug'})
                    continue

                try:
                    logger.info(f"Converting {tbl}.{col.name} -> TIMESTAMP WITH TIME ZONE (interpreting existing values as {source_tz})", extra={'emoji_type': 'info'})
                    with engine.connect() as conn:
                        conn = conn.execution_options(isolation_level='AUTOCOMMIT')
                        conn.execute(text(alter_sql), {'src': source_tz})
                        # record migration
                        try:
                            conn.execute(text("INSERT INTO auto_migrations (table_name, column_name, source) VALUES (:t, :c, :s)"), {"t": tbl, "c": col.name, "s": 'convert_to_timestamptz'})
                        except Exception:
                            pass
                        conn.commit()
                    logger.info(f"Converted {tbl}.{col.name} to timestamptz", extra={'emoji_type': 'success'})
                except Exception as ex:
                    logger.error(f"Failed to convert {tbl}.{col.name} to timestamptz: {ex}", extra={'emoji_type': 'error'})
