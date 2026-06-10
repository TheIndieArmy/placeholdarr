/** Default refresh while a tab is visible and idle (no in-flight task). */
export const TAB_IDLE_POLL_MS = 60_000;

/** Slower refresh when the document is hidden. */
export const TAB_HIDDEN_POLL_MS = 300_000;

/** Faster status checks while a maintenance task is running. */
export const TASKS_ACTIVE_POLL_MS = 5_000;

/** Log tail while the Logs tab is open. */
export const LOGS_POLL_MS = 15_000;

/** Fallback /api/ready poll while startup sync runs and SSE is disconnected. */
export const STARTUP_READY_FALLBACK_POLL_MS = 15_000;
