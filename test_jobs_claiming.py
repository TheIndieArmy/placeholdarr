from services.postgres.db import get_engine, init_db, get_session
from sqlalchemy import text
from services.jobs import insert_job, claim_jobs
from services.postgres.models import Job


def setup_db():
    eng = get_engine()
    with eng.connect() as conn:
        conn = conn.execution_options(isolation_level='AUTOCOMMIT')
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
    init_db(eng)


def test_claiming_once():
    setup_db()
    # Insert 5 jobs
    ids = []
    for i in range(5):
        jid = insert_job('test:noop', {'i': i}, group_id=None)
        ids.append(jid)

    # Claim up to 5 jobs
    claimed1 = claim_jobs(limit=5)
    assert len(claimed1) == 5

    # Attempt to claim again; should get zero because all are CLAIMED
    claimed2 = claim_jobs(limit=5)
    assert len(claimed2) == 0

    # Ensure statuses in DB are CLAIMED
    session = get_session()
    try:
        rows = session.query(Job).all()
        assert all(r.status == 'CLAIMED' for r in rows)
    finally:
        session.close()
