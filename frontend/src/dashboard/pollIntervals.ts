/** Default refresh while a tab is visible and idle (no in-flight task). */
export const TAB_IDLE_POLL_MS = 60_000;

/** Slower refresh when the document is hidden. */
export const TAB_HIDDEN_POLL_MS = 300_000;

/**
 * Default status check while a maintenance task is running (short phases like
 * ARR catalog / filesystem scan finish in a few seconds).
 */
export const TASKS_ACTIVE_POLL_MS = 1_000;

/**
 * Slower status check during long phases (placeholder materialization, calendar).
 * Keeps the Tasks UI responsive without looking frozen.
 */
export const TASKS_ACTIVE_SLOW_POLL_MS = 5_000;

/** Phase keys that use {@link TASKS_ACTIVE_SLOW_POLL_MS}. */
export const TASKS_SLOW_POLL_PHASE_KEYS = new Set([
  "materialization",
  "placeholder_materialization",
  "calendar",
  "calendar_status",
  "calendar_dates",
  "catalog_gc",
]);

/** Log tail while the Logs tab is open. */
export const LOGS_POLL_MS = 15_000;

/** Fallback /api/ready poll while startup sync runs and SSE is disconnected. */
export const STARTUP_READY_FALLBACK_POLL_MS = 15_000;
