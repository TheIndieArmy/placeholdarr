import sys
import time

import psycopg2
from core.config import settings

# Postgres SQLSTATE: invalid_catalog_name (database does not exist).
_PG_INVALID_CATALOG_NAME = "3D000"

# How long to hold the process after an unrecoverable DB failure so
# restart: unless-stopped does not thrash the host with CREATE / connect storms.
_UNRECOVERABLE_HOLD_SECONDS = 3600


def _exception_text(exc: BaseException) -> str:
    return str(exc or "")


def _pgcode(exc: BaseException) -> str:
    code = getattr(exc, "pgcode", None)
    return str(code or "")


def _is_database_missing(exc: BaseException) -> bool:
    """True only when the target database catalog entry is absent."""
    if _pgcode(exc) == _PG_INVALID_CATALOG_NAME:
        return True
    text = _exception_text(exc).lower()
    # libpq / psycopg2 wording when connecting to a missing DB name.
    return "database" in text and "does not exist" in text


def _is_catalog_corruption(exc: BaseException) -> bool:
    """True when Postgres catalog/files are damaged (not a missing-DB case)."""
    text = _exception_text(exc).lower()
    if 'could not open file "base/' in text or "could not open file 'base/" in text:
        return True
    if "could not open file" in text and "/base/" in text:
        return True
    # Common unclean-shutdown / torn-page signatures.
    if "missing chunk number" in text:
        return True
    if "could not read block" in text and "base/" in text:
        return True
    return False


def _is_already_exists(exc: BaseException) -> bool:
    return "already exists" in _exception_text(exc).lower()


def _connect(dbname: str):
    return psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASS,
        dbname=dbname,
    )


def _hold_unrecoverable(message: str, detail: BaseException | None = None) -> None:
    """Print a clear fatal and sleep so Docker does not restart-storm the host."""
    print(f"❌ FATAL: {message}")
    if detail is not None:
        print(f"❌ Detail: {detail}")
    print(
        "❌ Repair or recreate the Postgres database data, then restart this container. "
        "Holding to avoid a restart storm under restart: unless-stopped."
    )
    while True:
        time.sleep(_UNRECOVERABLE_HOLD_SECONDS)


def _try_create_database() -> str:
    """
    Attempt CREATE DATABASE for first-boot self-heal.

    Returns:
      "created" | "already_exists"
    Raises on other bootstrap/create failures.
    """
    bootstrap = _connect("postgres")
    try:
        bootstrap.autocommit = True
        cur = bootstrap.cursor()
        try:
            cur.execute(f'CREATE DATABASE "{settings.DB_NAME}"')
        finally:
            cur.close()
    except Exception as create_err:
        if _is_already_exists(create_err):
            return "already_exists"
        raise
    finally:
        try:
            bootstrap.close()
        except Exception:
            pass
    return "created"


def _wait_for_retry(attempt: int, max_attempts: int, delay_seconds: int) -> bool:
    """Log readiness wait. Returns True if another attempt should run."""
    if attempt >= max_attempts:
        return False
    print(
        f"⏳ Waiting for database readiness ({attempt}/{max_attempts}) "
        f"host={settings.DB_HOST} port={settings.DB_PORT}"
    )
    time.sleep(delay_seconds)
    return True


def check_db():
    max_attempts = 30
    delay_seconds = 2
    last_db_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            conn = _connect(settings.DB_NAME)
            conn.close()
            if attempt > 1:
                print(f"✅ Database connection OK after retry {attempt}/{max_attempts}")
            else:
                print("✅ Database connection OK")
            return True

        except Exception as e:
            last_db_error = e

            if _is_catalog_corruption(e):
                _hold_unrecoverable(
                    f'Database "{settings.DB_NAME}" catalog/files look corrupt '
                    f"(missing base/ relations). Not auto-creating or retrying.",
                    e,
                )

            if _is_database_missing(e):
                try:
                    create_result = _try_create_database()
                except Exception as create_err:
                    print(f"❌ Failed creating database {settings.DB_NAME}: {create_err}")
                    last_db_error = create_err
                    if not _wait_for_retry(attempt, max_attempts, delay_seconds):
                        break
                    continue

                if create_result == "created":
                    print(f"✅ Created missing database: {settings.DB_NAME}")
                    continue

                # Catalog says it exists, but connect reported missing (or race).
                # One probe; any failure is unrecoverable (no CREATE retry storm).
                print(
                    f"❌ Database \"{settings.DB_NAME}\" already exists but is not "
                    f"connectable. Treating as unrecoverable."
                )
                try:
                    conn = _connect(settings.DB_NAME)
                    conn.close()
                    print("✅ Database connection OK after create race")
                    return True
                except Exception as probe_err:
                    _hold_unrecoverable(
                        f'Database "{settings.DB_NAME}" already exists but connect still fails.',
                        probe_err,
                    )

            if not _wait_for_retry(attempt, max_attempts, delay_seconds):
                break

    print("❌ DB error:", last_db_error)
    sys.exit(1)
