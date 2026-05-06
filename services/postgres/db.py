from contextlib import contextmanager

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


def _coerce_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read an int setting with safe fallback to ``default`` and a floor of ``minimum``."""
    try:
        v = int(getattr(settings, name, default) or default)
    except Exception:
        v = default
    return max(minimum, v)


def get_engine():
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = (
        f"postgresql://{settings.DB_USER}:{settings.DB_PASS}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    # Phase 2 of the holistic NOTIFY audit: connection pool sizing is now
    # externally tunable via env / settings. Defaults are higher than the
    # legacy hardcoded values (10/20) to give NOTIFY-era spiky concurrency
    # more headroom, while still well under typical Postgres
    # ``max_connections`` (100). Operators sizing for many workers should
    # confirm: pool_size + max_overflow + (notifier LISTEN) + (other apps)
    # < server max_connections.
    pool_size = _coerce_int("DB_POOL_SIZE", 20, minimum=1)
    max_overflow = _coerce_int("DB_POOL_MAX_OVERFLOW", 20, minimum=0)
    pool_timeout = _coerce_int("DB_POOL_TIMEOUT_SECONDS", 10, minimum=1)
    pool_recycle = _coerce_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60)
    _engine = create_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
    )
    logger.info(
        f"DB engine created pool_size={pool_size} max_overflow={max_overflow} "
        f"pool_timeout={pool_timeout}s pool_recycle={pool_recycle}s",
        extra={"emoji_type": "gear"},
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
    try:
        from services.placeholder_activity_history_hooks import register_placeholder_activity_history_hooks

        register_placeholder_activity_history_hooks()
    except Exception as ex:
        logger.warning("Could not register placeholder_activity_history hooks: %s", ex, extra={"emoji_type": "warning"})
    try:
        from services.system_activity_history_hooks import register_system_activity_history_hooks

        register_system_activity_history_hooks()
    except Exception as ex:
        logger.warning("Could not register system_activity_history hooks: %s", ex, extra={"emoji_type": "warning"})
    try:
        from services.dashboard_stats_snapshot_hooks import register_dashboard_stats_snapshot_hooks

        register_dashboard_stats_snapshot_hooks()
    except Exception as ex:
        logger.warning("Could not register dashboard_stats_snapshot hooks: %s", ex, extra={"emoji_type": "warning"})
    return _engine


def get_session(engine=None):
    """Return a SQLAlchemy Session from a module-level session factory.

    Avoid creating new engines/sessionmakers on each call. Callers should
    close() the returned session when finished. New code should prefer
    ``session_scope()`` so close-on-finally is impossible to forget.
    """
    global _engine, _SessionFactory
    if _SessionFactory is None:
        _engine = get_engine()
    return _SessionFactory()


@contextmanager
def session_scope():
    """Yield a Session and guarantee close-on-finally.

    Phase 2 of the holistic NOTIFY audit: this is the preferred shape for
    new code paths. Existing ``get_session()`` call sites can be migrated
    incrementally; the contract is identical except that a stray ``raise``
    inside the ``with`` block always leaves the connection returned to the
    pool. Use ``session.commit()`` explicitly for writes.

    Example:
        with session_scope() as session:
            session.execute(...)
            session.commit()
    """
    session = get_session()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:
            pass


def pool_stats() -> dict:
    """Return a dict snapshot of SQLAlchemy pool counters for diagnostics.

    Returned keys (all ints, missing if the pool does not expose them):
    ``size``, ``checked_in``, ``checked_out``, ``overflow``, ``total``.
    Safe to call from request handlers; does not block.
    """
    try:
        engine = get_engine()
        pool = engine.pool
        out: dict[str, int] = {}
        for attr, label in (
            ("size", "size"),
            ("checkedin", "checked_in"),
            ("checkedout", "checked_out"),
            ("overflow", "overflow"),
        ):
            fn = getattr(pool, attr, None)
            if callable(fn):
                try:
                    out[label] = int(fn())
                except Exception:
                    pass
        try:
            out["total"] = int(out.get("checked_in", 0)) + int(out.get("checked_out", 0))
        except Exception:
            pass
        return out
    except Exception:
        return {}

def init_db(engine=None, convert_ts: bool | None = None):
    """Initialize DB schema and optionally convert naive timestamp columns to timestamptz.

    By default conversion is opt-in. To force conversion at startup set the
    environment variable CONVERT_TS=1 or call init_db(convert_ts=True).
    """
    engine = engine or get_engine()

    # Recover from hard resets that drop the default schema.
    # Without this, create_all/add-only migrations can no-op/fail and leave
    # the app running against a database with no tables.
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS public')
            conn.commit()
    except Exception as ex:
        logger.warning(f"Could not ensure public schema exists: {ex}", extra={'emoji_type': 'warning'})

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

    # Serialize init_db across processes (two containers, or overlapping restarts).
    # Previously we used pg_try_advisory_lock with ~1.25s of retries; migrations often
    # take longer than that while another session still held the lock, which logged a
    # scary WARNING and then proceeded anyway. Use blocking pg_advisory_lock with a
    # bounded lock_timeout so we normally wait quietly until the lock is free.
    lock_key = 987654321
    lock_wait_seconds = max(5, min(_coerce_int("RUNTIME_SCHEMA_LOCK_WAIT_SECONDS", 120, minimum=5), 600))
    got_lock = False
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text(f"SET LOCAL lock_timeout = '{int(lock_wait_seconds)}s'"))
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key})
                conn.commit()
                got_lock = True
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.warning(
                    "Runtime schema advisory lock not acquired within "
                    f"{lock_wait_seconds}s — another instance may be migrating, or the DB "
                    f"session is stuck. Proceeding with schema steps anyway (concurrent DDL "
                    f"risk): {exc}",
                    extra={"emoji_type": "warning"},
                )

            try:
                # Import models so they are registered with Base.metadata
                import services.postgres.models  # noqa: F401

                logger.info(
                    f"Creating tables from SQLAlchemy models: {list(Base.metadata.tables.keys())}",
                    extra={'emoji_type': 'info'},
                )
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

                # Migrate instance_key composite unique constraints (drop legacy single-col,
                # backfill instance_key, create composite indexes).
                try:
                    _migrate_instance_key_constraints(engine)
                except Exception as ex:
                    logger.error(
                        f"Runtime instance_key constraint migration failed: {ex}",
                        extra={'emoji_type': 'error'},
                    )

                # Optionally perform timestamp conversions if configured
                try:
                    if os.getenv('CONVERT_TS') == '1':
                        _convert_timestamp_columns(engine, inspector)
                except Exception as ex:
                    logger.error(f"Runtime timestamp conversion failed: {ex}", extra={'emoji_type': 'error'})
            finally:
                # Session-level advisory locks survive commit; always release if we
                # acquired, so the pooled connection is not returned with a lock held.
                if got_lock:
                    try:
                        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                        conn.commit()
                    except Exception:
                        try:
                            conn.rollback()
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

    # Ensure unique partial index to prevent duplicate active observation trail
    # jobs for the same group_id when producers run concurrently.
    try:
        from sqlalchemy import text
        idx_sql_obs_trail = text(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_job_obs_trail_groupid "
            "ON job(group_id) "
            "WHERE job_type='placeholder_observation_trail' AND status IN ('PENDING','CLAIMED','WORKING')"
        )
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
            conn.execute(idx_sql_obs_trail)
    except Exception as ex:
        logger.debug(f"Could not ensure ux_job_obs_trail_groupid index exists: {ex}", extra={'emoji_type': 'debug'})
    
    # Ensure unique partial index for Plex busy-aware deferred observation trails
    try:
        from sqlalchemy import text
        idx_sql_plex_busy = text(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_job_plex_busy_deferred_groupid "
            "ON job(group_id) "
            "WHERE job_type='plex_busy_deferred_observation_trail' AND status IN ('PENDING','CLAIMED','WORKING')"
        )
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
            conn.execute(idx_sql_plex_busy)
    except Exception as ex:
        logger.debug(f"Could not ensure ux_job_plex_busy_deferred_groupid index exists: {ex}", extra={'emoji_type': 'debug'})

    # Ensure unique partial index for hybrid observation slice coalescing.
    try:
        from sqlalchemy import text
        idx_sql_hybrid_slice = text(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_job_obs_hybrid_slice_groupid "
            "ON job(group_id) "
            "WHERE job_type='placeholder_observation_hybrid_slice' AND status IN ('PENDING','CLAIMED','WORKING')"
        )
        with engine.connect() as conn:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
            conn.execute(idx_sql_hybrid_slice)
    except Exception as ex:
        logger.debug(f"Could not ensure ux_job_obs_hybrid_slice_groupid index exists: {ex}", extra={'emoji_type': 'debug'})

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

    # Ensure NOTIFY triggers are installed on the job table so worker listeners
    # are woken on every Job INSERT/UPDATE-to-PENDING. Idempotent (CREATE OR
    # REPLACE / DROP IF EXISTS + CREATE). Safe to run on every startup.
    try:
        _ensure_job_notify_trigger(engine)
    except Exception as ex:
        logger.error(
            f"Failed to ensure job NOTIFY trigger: {ex}",
            extra={'emoji_type': 'error'},
        )

    try:
        _ensure_placeholder_queue_monitor_index(engine)
    except Exception as ex:
        logger.debug(
            f"Could not ensure placeholder queue_monitor_active partial index: {ex}",
            extra={'emoji_type': 'debug'},
        )


def _ensure_placeholder_queue_monitor_index(engine):
    """Partial index for playback-flagged placeholders tracked by the queue monitor."""
    idx_sql = text(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_placeholder_queue_monitor_active "
        "ON placeholder (queue_monitor_active) WHERE queue_monitor_active = TRUE"
    )
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level='AUTOCOMMIT')
        conn.execute(idx_sql)


def _ensure_job_notify_trigger(engine):
    """Install Postgres triggers that emit NOTIFY 'placeholdarr_jobs' on job changes.

    Statement-level (FOR EACH STATEMENT) so a multi-row INSERT or UPDATE produces
    one NOTIFY rather than N. The listener drains all PENDING work on each wake,
    so one wake-per-statement is enough.

    Channel name is hard-coded to match `services.postgres.notifier.JOBS_CHANNEL`.

    Phase 6 portability: ``EXECUTE FUNCTION`` is the Postgres 11+ syntax.
    Older 9.6 / 10 servers require ``EXECUTE PROCEDURE``. We attempt the
    modern syntax first and fall back if the server rejects it.
    """
    # Detect server major version once and pick syntax accordingly. The
    # check is cheap (one round-trip) and lets us emit a clear log line
    # if the operator is running an unsupported version.
    server_major = 0
    try:
        with engine.connect() as conn:
            ver = conn.execute(text("SHOW server_version_num")).scalar()
            try:
                server_major = int(int(str(ver or '0')) // 10000)
            except Exception:
                server_major = 0
    except Exception:
        server_major = 0

    if 0 < server_major < 11:
        logger.warning(
            f"Postgres {server_major}.x detected — NOTIFY triggers will use legacy "
            "EXECUTE PROCEDURE syntax. Postgres 11+ is recommended (see README).",
            extra={'emoji_type': 'warning'},
        )

    fn_sql = text(
        """
        CREATE OR REPLACE FUNCTION notify_jobs_change() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('placeholdarr_jobs', '');
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    drop_insert_sql = text("DROP TRIGGER IF EXISTS job_notify_insert ON job")
    drop_update_sql = text("DROP TRIGGER IF EXISTS job_notify_update ON job")

    use_legacy = bool(0 < server_major < 11)
    exec_clause = "EXECUTE PROCEDURE" if use_legacy else "EXECUTE FUNCTION"
    create_insert_sql = text(
        f"""
        CREATE TRIGGER job_notify_insert
        AFTER INSERT ON job
        FOR EACH STATEMENT
        {exec_clause} notify_jobs_change()
        """
    )
    create_update_sql = text(
        f"""
        CREATE TRIGGER job_notify_update
        AFTER UPDATE OF status, run_after ON job
        FOR EACH STATEMENT
        {exec_clause} notify_jobs_change()
        """
    )

    def _install(connection):
        connection.execute(fn_sql)
        connection.execute(drop_insert_sql)
        connection.execute(drop_update_sql)
        connection.execute(create_insert_sql)
        connection.execute(create_update_sql)
        connection.commit()

    try:
        with engine.connect() as conn:
            _install(conn)
    except Exception as exc:
        # If we tried the modern syntax against a server that rejects it,
        # retry with the legacy clause as a last-ditch fallback.
        if not use_legacy and 'EXECUTE FUNCTION' in str(exc).upper():
            logger.warning(
                "EXECUTE FUNCTION rejected; retrying with legacy EXECUTE PROCEDURE",
                extra={'emoji_type': 'warning'},
            )
            create_insert_sql = text(
                """
                CREATE TRIGGER job_notify_insert
                AFTER INSERT ON job
                FOR EACH STATEMENT
                EXECUTE PROCEDURE notify_jobs_change()
                """
            )
            create_update_sql = text(
                """
                CREATE TRIGGER job_notify_update
                AFTER UPDATE OF status, run_after ON job
                FOR EACH STATEMENT
                EXECUTE PROCEDURE notify_jobs_change()
                """
            )
            with engine.connect() as conn:
                _install(conn)
        else:
            raise

    logger.info(
        "Installed NOTIFY triggers on job table (channel='placeholdarr_jobs', "
        f"server_major={server_major or 'unknown'})",
        extra={'emoji_type': 'success'},
    )


def _migrate_instance_key_constraints(engine):
    """Drop legacy single-column unique constraints on tmdbid/tvdbid, backfill instance_key,
    and create the composite unique indexes (tmdbid, instance_key) / (tvdbid, instance_key).

    Safe to call repeatedly — all operations are idempotent.
    """
    from sqlalchemy import text
    
    # Derive default instance keys from configured instances using role/priority.
    def get_default_key(arr_type: str, role: str) -> str:
        item = settings.resolve_arr_instance(arr_type, role=role) or settings.resolve_arr_instance(arr_type)
        if item:
            return str(item.get('instance_key', '')).strip().lower()
        return f"{arr_type}_{role}"

    radarr_primary_key = get_default_key('radarr', 'primary')
    sonarr_primary_key = get_default_key('sonarr', 'primary')

    steps = [
        # (description, SQL)
        # 1. Backfill instance_key for movie rows where it is NULL
        (
            "Backfill movie.instance_key from defaults",
            f"""
            UPDATE movie
               SET instance_key = '{radarr_primary_key}'
             WHERE instance_key IS NULL OR instance_key = ''
            """,
        ),
        # 2. Backfill instance_key for series rows where it is NULL
        (
            "Backfill series.instance_key from defaults",
            f"""
            UPDATE series
               SET instance_key = '{sonarr_primary_key}'
             WHERE instance_key IS NULL OR instance_key = ''
            """,
        ),
        # 3. Drop old single-column unique constraint on movie.tmdbid (if present)
        (
            "Drop legacy movie_tmdbid_key constraint",
            "ALTER TABLE movie DROP CONSTRAINT IF EXISTS movie_tmdbid_key",
        ),
        # 4. Drop old single-column unique constraint on series.tvdbid (if present)
        (
            "Drop legacy series_tvdbid_key constraint",
            "ALTER TABLE series DROP CONSTRAINT IF EXISTS series_tvdbid_key",
        ),
        # 5. Also drop any unique index (not constraint) on just movie.tmdbid
        (
            "Drop legacy ux_movie_tmdbid index (single-col)",
            "DROP INDEX IF EXISTS ux_movie_tmdbid",
        ),
        # 6. Also drop any unique index (not constraint) on just series.tvdbid
        (
            "Drop legacy ux_series_tvdbid index (single-col)",
            "DROP INDEX IF EXISTS ux_series_tvdbid",
        ),
    ]

    for desc, sql in steps:
        try:
            with engine.connect() as conn:
                conn.execute(text(sql.strip()))
                conn.commit()
                logger.debug(f"instance_key migration: {desc} — OK", extra={'emoji_type': 'debug'})
        except Exception as ex:
            logger.warning(f"instance_key migration step '{desc}' failed (may be harmless): {ex}", extra={'emoji_type': 'warning'})

    # Create composite unique indexes via CONCURRENTLY (must be outside a transaction)
    composite_indexes = [
        (
            "ux_movie_tmdbid_instance_key",
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_movie_tmdbid_instance_key "
            "ON movie(tmdbid, instance_key)",
        ),
        (
            "ux_series_tvdbid_instance_key",
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_series_tvdbid_instance_key "
            "ON series(tvdbid, instance_key)",
        ),
    ]
    for idx_name, idx_sql in composite_indexes:
        try:
            with engine.connect() as conn:
                conn = conn.execution_options(isolation_level='AUTOCOMMIT')
                conn.execute(text(idx_sql))
                logger.info(f"Ensured composite unique index: {idx_name}", extra={'emoji_type': 'success'})
        except Exception as ex:
            logger.warning(f"Could not ensure composite index {idx_name}: {ex}", extra={'emoji_type': 'warning'})


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
