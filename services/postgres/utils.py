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
        print("❌ DB error:", e)
        sys.exit(1)
