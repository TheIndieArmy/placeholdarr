/** Shared confirm copy for Stop task (Tasks panel + sidebar pill). */
export const TASK_STOP_CONFIRM_MESSAGE =
  "Are you sure you want to stop this task? This only stops the current run. It does not undo settings. The next full sync or art refresh will still apply those changes.";

/** Tasks with fine-grained cancel exits today (Stop should feel near-instant). */
export const TASK_KEYS_WITH_STOP_SUPPORT = new Set(["placeholder_refresh"]);
