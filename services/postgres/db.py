from core.logger import logger
from sqlalchemy import create_engine, inspect, event
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError, DisconnectionError
from sqlalchemy.pool import QueuePool
from core.config import settings
import time
import threading

Base = declarative_base()

# Global engine with optimized configuration
_engine = None
_engine_lock = threading.Lock()

def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:  # Double-check locking
                url = (
                    f"postgresql://{settings.DB_USER}:{settings.DB_PASS}"
                    f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
                )
                _engine = create_engine(
                    url, 
                    echo=False, 
                    future=True,
                    # Optimized connection pool settings
                    poolclass=QueuePool,
                    pool_size=10,              # Number of connections to maintain
                    max_overflow=20,           # Additional connections beyond pool_size
                    pool_pre_ping=True,        # Verify connections before use
                    pool_recycle=3600,         # Recycle connections every hour
                    connect_args={
                        "connect_timeout": 10,  # 10 second connection timeout
                        "application_name": "placeholdarr_scheduler"
                    }
                )
                
                # Add connection event listener for PostgreSQL optimization
                @event.listens_for(_engine, "connect")
                def set_postgresql_pragma(dbapi_connection, connection_record):
                    try:
                        with dbapi_connection.cursor() as cursor:
                            # Optimize PostgreSQL settings (only runtime-configurable parameters)
                            cursor.execute("SET statement_timeout = '300s'")     # 5 minute statement timeout
                            cursor.execute("SET lock_timeout = '60s'")           # 1 minute lock timeout
                            cursor.execute("SET idle_in_transaction_session_timeout = '600s'")  # 10 minute idle timeout
                            cursor.execute("SET tcp_keepalives_idle = '600'")     # TCP keepalive (10 minutes)
                            cursor.execute("SET tcp_keepalives_interval = '30'")  # TCP keepalive interval
                            cursor.execute("SET tcp_keepalives_count = '3'")      # TCP keepalive count
                            # Note: Removed wal_buffers and synchronous_commit as they require server restart/config
                    except Exception as e:
                        logger.warning(f"Failed to set PostgreSQL optimizations: {e}")
                
                logger.info(f"Created optimized database engine for: {_engine.url}", extra={'emoji_type': 'database'})
    
    return _engine

def get_session(engine=None):
    """Get a database session with optimized settings"""
    engine = engine or get_engine()
    Session = sessionmaker(
        bind=engine, 
        future=True,
        autoflush=False,        # Don't auto-flush on queries
        expire_on_commit=False  # Keep objects usable after commit
    )
    return Session()

def db_operation_with_retry(operation, max_retries=3, delay=0.1):
    """Execute database operation with retry logic for connection issues"""
    for attempt in range(max_retries):
        try:
            return operation()
        except (SQLAlchemyError, DisconnectionError) as e:
            if attempt == max_retries - 1:
                logger.error(f"DB operation failed after {max_retries} attempts: {e}", extra={'emoji_type': 'error'})
                raise
            
            logger.warning(f"DB operation failed (attempt {attempt + 1}/{max_retries}): {e}", extra={'emoji_type': 'warning'})
            time.sleep(delay * (2 ** attempt))  # Exponential backoff

from contextlib import contextmanager

@contextmanager
def db_session_scope():
    """Provide a transactional scope around database operations with proper cleanup"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database transaction failed, rolled back: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()

@contextmanager 
def db_batch_scope(batch_size=100):
    """Provide a scope for batch operations with periodic commits"""
    session = get_session()
    try:
        operation_count = 0
        
        def maybe_commit():
            nonlocal operation_count
            operation_count += 1
            if operation_count >= batch_size:
                session.commit()
                operation_count = 0
        
        yield session, maybe_commit
        
        # Final commit for remaining operations
        if operation_count > 0:
            session.commit()
            
    except Exception as e:
        session.rollback()
        logger.error(f"Batch database operation failed, rolled back: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()

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
