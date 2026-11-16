/** Full Tasks history refresh while a tab is visible and idle (no in-flight task). */
export const TAB_IDLE_POLL_MS = 60_000;

/** Slower refresh when the document is hidden. */
export const TAB_HIDDEN_POLL_MS = 300_000;

/**
 * Cheap ``/api/tasks/status`` probe while idle so a Run started on an open page
 * switches onto the 1s live cadence without waiting for the 60s history refresh
 * (or a full browser reload).
 */
export const TASKS_IDLE_STATUS_POLL_MS = 5_000;

/**
 * Status check while a maintenance task is running.
 * Live ticks only patch ``run.live`` (no full history refetch), so 1s is cheap
 * and keeps catalog discovery / mat bars moving without a manual refresh.
 */
export const TASKS_ACTIVE_POLL_MS = 1_000;

/** Log tail while the Logs tab is open. */
export const LOGS_POLL_MS = 15_000;

/** Fallback /api/ready poll while startup sync runs and SSE is disconnected. */
export const STARTUP_READY_FALLBACK_POLL_MS = 15_000;
