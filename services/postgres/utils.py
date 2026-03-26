import psycopg2
import sys
from core.config import settings

def check_db():
    try:
        conn = psycopg2.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASS,
            dbname=settings.DB_NAME,
        )
        conn.close()
        print("✅ Database connection OK")
        return True
    except Exception as e:
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
            return True
        except Exception as create_err:
            print("❌ DB error:", e)
            print("❌ Failed creating database:", create_err)
            sys.exit(1)
