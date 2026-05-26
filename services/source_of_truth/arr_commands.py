"""Radarr/Sonarr command API: Refresh, Rescan, and poll until complete."""

from __future__ import annotations

import time
from typing import Any, Callable

from core.logger import logger
from services.source_of_truth.arr_api import _build_endpoint, _request_json

TERMINAL_COMMAND_STATUSES = frozenset({"completed", "failed", "aborted", "cancelled"})


class ArrCommandError(Exception):
    """ARR command failed or timed out."""


def _command_status(command: dict[str, Any] | None) -> str:
    if not isinstance(command, dict):
        return ""
    return str(command.get("status") or "").strip().lower()


def post_arr_command(
    *,
    base_url: str,
    api_key: str,
    name: str,
    body: dict[str, Any],
    timeout: int = 30,
) -> int:
    """POST /api/v3/command and return the command id."""
    endpoint = _build_endpoint(base_url, "command")
    payload = {"name": str(name), **body}
    result = _request_json(
        "POST",
        endpoint,
        payload=payload,
        api_key=api_key,
        timeout=timeout,
    )
    if not isinstance(result, dict):
        raise ArrCommandError(f"ARR command {name!r} returned no response")
    raw_id = result.get("id")
    try:
        return int(raw_id)
    except (TypeError, ValueError) as exc:
        raise ArrCommandError(f"ARR command {name!r} returned invalid id: {raw_id!r}") from exc


def get_arr_command(
    *,
    base_url: str,
    api_key: str,
    command_id: int,
    timeout: int = 20,
) -> dict[str, Any]:
    endpoint = _build_endpoint(base_url, f"command/{int(command_id)}")
    result = _request_json("GET", endpoint, api_key=api_key, timeout=timeout)
    if not isinstance(result, dict):
        return {}
    return result


def wait_arr_commands(
    *,
    base_url: str,
    api_key: str,
    command_ids: list[int],
    timeout_s: float = 300.0,
    poll_s: float = 2.0,
    on_tick: Callable[[dict[int, str]], None] | None = None,
) -> None:
    """Poll until every command is terminal or timeout elapses."""
    pending = {int(cid) for cid in command_ids if cid is not None}
    if not pending:
        return

    deadline = time.monotonic() + max(1.0, float(timeout_s))
    poll_interval = max(0.5, float(poll_s))

    while pending and time.monotonic() < deadline:
        statuses: dict[int, str] = {}
        finished: list[int] = []
        for cid in sorted(pending):
            cmd = get_arr_command(base_url=base_url, api_key=api_key, command_id=cid)
            status = _command_status(cmd)
            statuses[cid] = status or "unknown"
            if status in TERMINAL_COMMAND_STATUSES:
                finished.append(cid)
                if status == "failed":
                    msg = str(cmd.get("message") or cmd.get("exception") or "command failed")
                    raise ArrCommandError(f"ARR command {cid} failed: {msg}")
                if status in {"aborted", "cancelled"}:
                    raise ArrCommandError(f"ARR command {cid} ended with status={status}")

        if on_tick is not None:
            try:
                on_tick(statuses)
            except Exception as exc:
                logger.debug(f"wait_arr_commands on_tick failed: {exc}", extra={"emoji_type": "debug"})

        for cid in finished:
            pending.discard(cid)

        if pending:
            time.sleep(poll_interval)

    if pending:
        raise ArrCommandError(
            f"Timed out waiting for ARR commands: {sorted(pending)} (timeout_s={timeout_s})"
        )


def trigger_refresh_movie(*, base_url: str, api_key: str, movie_id: int) -> int:
    return post_arr_command(
        base_url=base_url,
        api_key=api_key,
        name="RefreshMovie",
        body={"movieIds": [int(movie_id)]},
    )


def trigger_rescan_movie(*, base_url: str, api_key: str, movie_id: int) -> int:
    return post_arr_command(
        base_url=base_url,
        api_key=api_key,
        name="RescanMovie",
        body={"movieIds": [int(movie_id)]},
    )


def trigger_refresh_series(*, base_url: str, api_key: str, series_id: int) -> int:
    return post_arr_command(
        base_url=base_url,
        api_key=api_key,
        name="RefreshSeries",
        body={"seriesId": int(series_id)},
    )


def trigger_rescan_series(*, base_url: str, api_key: str, series_id: int) -> int:
    return post_arr_command(
        base_url=base_url,
        api_key=api_key,
        name="RescanSeries",
        body={"seriesId": int(series_id)},
    )
