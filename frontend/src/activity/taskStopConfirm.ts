/** Shared confirm copy for Stop task (Tasks panel + sidebar pill). */
export const TASK_STOP_CONFIRM_MESSAGE =
  "Are you sure you want to stop this task? This only stops the current run. It does not undo settings. The next full sync or art refresh will still apply those changes.";

/** Tasks with cooperative cancel checkpoints (Stop stops at the next safe boundary). */
export const TASK_KEYS_WITH_STOP_SUPPORT = new Set(["placeholder_refresh", "full_sync", "lite_sync"]);
