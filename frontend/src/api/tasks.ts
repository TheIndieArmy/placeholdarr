import { fetchJson } from "./client";
import type { ScheduledTaskRow, TaskRunRow, TaskRunStatusResponse } from "../types/api";

export function getTasksScheduled() {
  return fetchJson<{ tasks: ScheduledTaskRow[] }>("/api/tasks/scheduled");
}

export function getTasksHistory(limit = 50) {
  return fetchJson<TaskRunRow[]>(`/api/tasks/history?limit=${limit}`);
}

export function getTasksStatus() {
  return fetchJson<TaskRunStatusResponse>("/api/tasks/status");
}

export function postTaskRun(
  taskKey: "full_sync" | "lite_sync" | "calendar_only" | "placeholder_refresh",
  opts?: { metadata?: boolean; art?: boolean },
) {
  return fetchJson<{ ok: boolean; task_key: string; message: string }>("/api/tasks/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_key: taskKey, ...(opts || {}) }),
  });
}
