"""Smoke test: enqueue a simple enrichment job and run the worker once.

Usage: run with the project's venv python to exercise the new enrichment job path.
"""
from services.enricher import enqueue_enrichment_job
from services.jobs import work_once
from core.logger import logger


def main():
    payload = {'series': {'tvdbId': 999999, 'id': None}}
    job_id = enqueue_enrichment_job(payload, is_4k=False)
    if not job_id:
        logger.error('Failed to enqueue enrichment job')
        return
    logger.info(f'Enqueued enrichment job {job_id}, running worker once...')
    work_once()


if __name__ == '__main__':
    main()
