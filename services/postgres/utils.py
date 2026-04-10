import psycopg2
import sys
import time
from core.config import settings

def check_db():
    max_attempts = 30
    delay_seconds = 2
    last_db_error = None
    last_create_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            conn = psycopg2.connect(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USER,
                password=settings.DB_PASS,
                dbname=settings.DB_NAME,
            )
            conn.close()
            if attempt > 1:
                print(f"✅ Database connection OK after retry {attempt}/{max_attempts}")
            else:
                print("✅ Database connection OK")
            return True

        except Exception as e:
            last_db_error = e

            # If the target DB does not exist yet, create it from the default
            # postgres maintenance database so first boot is self-healing.
            try:
                bootstrap = psycopg2.connect(
                    host=settings.DB_HOST,
                    port=settings.DB_PORT,
                    user=settings.DB_USER,
                    password=settings.DB_PASS,
                    dbname='postgres',
                )
                bootstrap.autocommit = True
                cur = bootstrap.cursor()
                cur.execute(f'CREATE DATABASE "{settings.DB_NAME}"')
                cur.close()
                bootstrap.close()
                print(f"✅ Created missing database: {settings.DB_NAME}")
            except Exception as create_err:
                if 'already exists' not in str(create_err).lower():
                    last_create_error = create_err

            if attempt < max_attempts:
                print(
                    f"⏳ Waiting for database readiness ({attempt}/{max_attempts}) "
                    f"host={settings.DB_HOST} port={settings.DB_PORT}"
                )
                time.sleep(delay_seconds)

    print("❌ DB error:", last_db_error)
    if last_create_error:
        print("❌ Failed creating database:", last_create_error)
    sys.exit(1)
