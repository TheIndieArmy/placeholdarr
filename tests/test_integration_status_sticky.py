"""Integration status sticky / probe-clear behavior (#184)."""

from __future__ import annotations

import services.integration_status as integration_status


def setup_function() -> None:
    with integration_status._lock:
        integration_status._media.clear()
        integration_status._arr.clear()
        integration_status._last_refresh_at = None


def test_probe_success_clears_connectivity_failure() -> None:
    integration_status.record_media_status(
        "plex",
        ok=False,
        message="HTTP 503 from http://plex:32400/identity",
        source="probe",
    )
    assert integration_status.get_integration_status()["media_has_failure"] is True

    integration_status.record_media_status(
        "plex",
        ok=True,
        message="Connected",
        source="probe",
    )
    status = integration_status.get_integration_status()
    assert status["media_has_failure"] is False
    assert status["media"]["plex"]["ok"] is True


def test_auth_failure_stays_sticky_until_test() -> None:
    integration_status.record_arr_status(
        instance_id="radarr:main",
        arr_type="radarr",
        ok=False,
        message="HTTP 401 Unauthorized",
        source="probe",
    )
    assert integration_status.get_integration_status()["arr"]["radarr:main"]["sticky_failure"] is True

    integration_status.record_arr_status(
        instance_id="radarr:main",
        arr_type="radarr",
        ok=True,
        message="Connected",
        source="probe",
    )
    assert integration_status.get_integration_status()["arr"]["radarr:main"]["ok"] is False

    integration_status.record_arr_status(
        instance_id="radarr:main",
        arr_type="radarr",
        ok=True,
        message="Connected",
        source="test",
    )
    assert integration_status.get_integration_status()["arr"]["radarr:main"]["ok"] is True


def test_runtime_success_does_not_paint_green() -> None:
    integration_status.record_media_status(
        "plex",
        ok=False,
        message="ConnectionError while connecting",
        source="runtime",
    )
    integration_status.record_media_status(
        "plex",
        ok=True,
        message="Connected",
        source="runtime",
    )
    assert integration_status.get_integration_status()["media"]["plex"]["ok"] is False
