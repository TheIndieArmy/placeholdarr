from services.source_of_truth.placeholder_art_reconciler import (
    PLACEHOLDER_ART_REFRESH_JOB_TYPE,
    _art_refresh_release_handler_session,
    process_placeholder_art_refresh_job,
)


def test_art_refresh_job_type_constant():
    assert PLACEHOLDER_ART_REFRESH_JOB_TYPE == "placeholder_art_refresh"


def test_process_art_job_skips_empty_ids():
    class _Job:
        id = 1
        payload = {"placeholder_ids": []}

    class _Session:
        def query(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return []

        def get(self, _id):
            return None

    out = process_placeholder_art_refresh_job(_Session(), _Job())
    assert out.get("ok") is False
    assert out.get("reason") == "no_placeholder_ids"


def test_art_refresh_release_handler_session_closes():
    calls = []

    class _Session:
        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    _art_refresh_release_handler_session(_Session())
    assert calls == ["commit", "close"]
