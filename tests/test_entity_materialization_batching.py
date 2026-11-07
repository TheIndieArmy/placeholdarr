"""Regression tests for coalesced / chunked entity materialization (#152)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.source_of_truth.entity_materialization_job import (
    ENTITY_MATERIALIZATION_JOB_TYPE,
    enqueue_entity_materialization_job,
    materialization_job_batch_size,
)
from services.source_of_truth.materializer import (
    bulk_materialization_active,
    bulk_materialization_scope,
    materialization_commit_chunk_size,
)


def test_materialization_batch_size_defaults_positive():
    assert materialization_job_batch_size() >= 1
    assert materialization_commit_chunk_size() >= 1


def test_bulk_materialization_scope_toggles_active_flag():
    assert bulk_materialization_active() is False
    with bulk_materialization_scope():
        assert bulk_materialization_active() is True
        with bulk_materialization_scope():
            assert bulk_materialization_active() is True
        assert bulk_materialization_active() is True
    assert bulk_materialization_active() is False


def test_enqueue_coalesces_same_observation_source_into_pending_job():
    session = MagicMock()
    pending = SimpleNamespace(
        id=41,
        payload={
            "movie_ids": [],
            "episode_ids": [1, 2],
            "observation_source": "event_episode_file_deleted",
        },
        status="PENDING",
        updated_at=None,
    )
    query = session.query.return_value
    query.filter.return_value.order_by.return_value.with_for_update.return_value.all.return_value = [
        pending
    ]

    with patch(
        "services.source_of_truth.entity_materialization_job.materialization_job_batch_size",
        return_value=100,
    ):
        job = enqueue_entity_materialization_job(
            session,
            movie_ids=[],
            episode_ids=[2, 3, 4],
            observation_source="event_episode_file_deleted",
            payload_extras={"run_determination_first": True},
        )

    assert job is pending
    assert pending.payload["episode_ids"] == [1, 2, 3, 4]
    assert pending.payload["run_determination_first"] is True
    session.add.assert_called()
    # No new Job() row when fully absorbed.
    assert session.flush.call_count == 0


def test_enqueue_movie_deleted_finalize_does_not_coalesce():
    session = MagicMock()
    # Coalesce query should not be used for exclusive finalize jobs.
    created = []

    def _add(obj):
        created.append(obj)

    session.add.side_effect = _add
    session.flush.side_effect = lambda: setattr(created[-1], "id", 99) if created else None

    with patch(
        "services.source_of_truth.entity_materialization_job.Job"
    ) as JobCls:
        JobCls.side_effect = lambda **kwargs: SimpleNamespace(id=None, **kwargs)
        job = enqueue_entity_materialization_job(
            session,
            movie_ids=[7],
            episode_ids=[],
            observation_source="event_movie_deleted",
            payload_extras={
                "finalize_movie_deleted_activity": True,
                "movie_row_id": 7,
            },
        )

    session.query.assert_not_called()
    assert job.job_type == ENTITY_MATERIALIZATION_JOB_TYPE
    assert job.payload["finalize_movie_deleted_activity"] is True
    assert job.payload["movie_ids"] == [7]
