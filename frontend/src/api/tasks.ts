import { fetchJson } from "./client";
import type { ScheduledTaskRow, TaskRunRow, TaskRunStatusResponse } from "../types/api";

export function getTasksScheduled() {
  return fetchJson<{ tasks: ScheduledTaskRow[] }>("/api/tasks/scheduled");
}

/** Last N runs per task type (default 30). High-cadence jobs cannot starve others. */
export function getTasksHistory(perTask = 30) {
  return fetchJson<TaskRunRow[]>(`/api/tasks/history?per_task=${perTask}`);
}

export function getTasksStatus() {
  return fetchJson<TaskRunStatusResponse>("/api/tasks/status");
}

export function postTaskRun(
  taskKey:
    | "full_sync"
    | "lite_sync"
    | "calendar_sync"
    | "calendar_only"
    | "placeholder_refresh"
    | "collections_sync"
    | "watchlist_sync",
  opts?: { metadata?: boolean; art?: boolean },
) {
  return fetchJson<{ ok: boolean; task_key: string; message: string }>("/api/tasks/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_key: taskKey, ...(opts || {}) }),
  });
}

export function postTaskAbandon(opts?: { runId?: number; reason?: string }) {
  return fetchJson<{ ok: boolean; abandoned: number[]; reason: string }>("/api/tasks/abandon", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      run_id: opts?.runId ?? null,
      reason: opts?.reason ?? "stopped_by_user",
    }),
  });
}
