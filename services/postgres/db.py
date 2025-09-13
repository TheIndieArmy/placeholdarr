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

    Base.metadata.create_all(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    created_tables = inspector.get_table_names()
    logger.info(f"Tables AFTER create_all(): {created_tables}", extra={'emoji_type': 'info'})
