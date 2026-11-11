import { useCallback, useEffect, useRef, useState } from "react";
import { getStats } from "../api/dashboard";
import { getTasksHistory, getTasksScheduled, getTasksStatus } from "../api/tasks";
import { TAB_HIDDEN_POLL_MS, TAB_IDLE_POLL_MS, TASKS_ACTIVE_POLL_MS } from "../dashboard/pollIntervals";
import type { ScheduledTaskRow, StatsResponse, TaskRunRow, TaskRunStatusResponse } from "../types/api";

/** Start/finish/idle only. Live seq is polled separately and must not full-refetch. */
function taskLifecycleFingerprint(status: TaskRunStatusResponse): string {
  if (!status.working || !status.run) {
    return "idle";
  }
  const run = status.run;
  return `${run.id}:${run.status}:${run.ended_at ?? ""}`;
}

function taskLiveFingerprint(status: TaskRunStatusResponse): string {
  if (!status.working || !status.run) {
    return "idle";
  }
  const run = status.run;
  const liveSeq = run.live && typeof run.live.seq === "number" ? run.live.seq : 0;
  return `${taskLifecycleFingerprint(status)}:live${liveSeq}`;
}

function mergeWorkingRun(history: TaskRunRow[], run: TaskRunRow): TaskRunRow[] {
  const idx = history.findIndex((row) => row.id === run.id);
  if (idx < 0) {
    return [run, ...history];
  }
  if (history[idx] === run) {
    return history;
  }
  const next = history.slice();
  next[idx] = run;
  return next;
}

export function useActivityTasks(opts: {
  enabled: boolean;
  /** When false, background refreshes skip the dashboard chrome spinner (sidebar live progress). */
  chromeOnRefresh?: boolean;
  onSuccess?: () => void;
  onError?: (message: string) => void;
  onRefreshing?: (refreshing: boolean) => void;
}) {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTaskRow[]>([]);
  const [taskHistory, setTaskHistory] = useState<TaskRunRow[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);

  const lifecycleFingerprintRef = useRef("idle");
  const liveFingerprintRef = useRef("idle");
  const workingRef = useRef(false);
  const chromeOnRefreshRef = useRef(Boolean(opts.chromeOnRefresh));
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);
  const onRefreshingRef = useRef(opts.onRefreshing);

  useEffect(() => {
    chromeOnRefreshRef.current = Boolean(opts.chromeOnRefresh);
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
    onRefreshingRef.current = opts.onRefreshing;
  }, [opts.chromeOnRefresh, opts.onError, opts.onRefreshing, opts.onSuccess]);

  const refreshTasks = useCallback(async (options?: { showLoading?: boolean; showChrome?: boolean }) => {
    const showLoading = options?.showLoading ?? false;
    const showChrome = options?.showChrome ?? false;
    if (showLoading) {
      setTasksLoading(true);
    }
    if (showChrome) {
      onRefreshingRef.current?.(true);
    }

    try {
      const [payload, sched, hist] = await Promise.all([getStats(), getTasksScheduled(), getTasksHistory(30)]);
      setStats(payload);
      setScheduledTasks(sched.tasks || []);
      setTaskHistory(hist || []);
      onSuccessRef.current?.();
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Tasks refresh failed");
      throw err;
    } finally {
      if (showLoading) {
        setTasksLoading(false);
      }
      if (showChrome) {
        onRefreshingRef.current?.(false);
      }
    }
  }, []);

  /**
   * Patch the working run from /api/tasks/status.
   * Used by the active poll and SSE lifecycle handlers.
   */
  const refreshWorkingLive = useCallback(async () => {
    try {
      const status = await getTasksStatus();
      workingRef.current = status.working;
      const lifecycle = taskLifecycleFingerprint(status);
      const live = taskLiveFingerprint(status);
      const lifecycleChanged = lifecycle !== lifecycleFingerprintRef.current;
      const liveChanged = live !== liveFingerprintRef.current;
      lifecycleFingerprintRef.current = lifecycle;
      liveFingerprintRef.current = live;

      if (!status.working || !status.run) {
        if (lifecycleChanged) {
          await refreshTasks({ showChrome: chromeOnRefreshRef.current });
        }
        return;
      }

      if (lifecycleChanged) {
        await refreshTasks({ showChrome: chromeOnRefreshRef.current });
        // History can lag the status payload; re-apply live after the full reload.
        setTaskHistory((prev) => mergeWorkingRun(prev, status.run as TaskRunRow));
        return;
      }

      if (liveChanged) {
        setTaskHistory((prev) => mergeWorkingRun(prev, status.run as TaskRunRow));
      }
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Tasks live refresh failed");
    }
  }, [refreshTasks]);

  useEffect(() => {
    if (!opts.enabled) return;

    let stopped = false;
    let timeoutId = 0;

    const schedule = (delayMs: number) => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        void tick();
      }, delayMs);
    };

    const tick = async () => {
      if (stopped) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) {
        schedule(TAB_HIDDEN_POLL_MS);
        return;
      }

      let nextDelay = TAB_IDLE_POLL_MS;
      try {
        const status = await getTasksStatus();
        if (stopped) return;

        const lifecycle = taskLifecycleFingerprint(status);
        const live = taskLiveFingerprint(status);
        const lifecycleChanged = lifecycle !== lifecycleFingerprintRef.current;
        const liveChanged = live !== liveFingerprintRef.current;
        lifecycleFingerprintRef.current = lifecycle;
        liveFingerprintRef.current = live;
        workingRef.current = status.working;

        if (status.working) {
          if (lifecycleChanged) {
            await refreshTasks({ showChrome: chromeOnRefreshRef.current });
            if (status.run) {
              setTaskHistory((prev) => mergeWorkingRun(prev, status.run as TaskRunRow));
            }
          } else if (liveChanged && status.run) {
            setTaskHistory((prev) => mergeWorkingRun(prev, status.run as TaskRunRow));
          }
          nextDelay = TASKS_ACTIVE_POLL_MS;
        } else {
          await refreshTasks({
            showChrome: lifecycleChanged ? chromeOnRefreshRef.current : false,
          });
          nextDelay = TAB_IDLE_POLL_MS;
        }
      } catch (err) {
        if (!stopped) {
          onErrorRef.current?.(err instanceof Error ? err.message : "Tasks refresh failed");
        }
        nextDelay = TAB_IDLE_POLL_MS;
      } finally {
        if (!stopped) {
          schedule(nextDelay);
        }
      }
    };

    void refreshTasks({
      showLoading: chromeOnRefreshRef.current,
      showChrome: chromeOnRefreshRef.current,
    }).then(async () => {
      if (stopped) return;
      try {
        const status = await getTasksStatus();
        if (stopped) return;
        workingRef.current = status.working;
        lifecycleFingerprintRef.current = taskLifecycleFingerprint(status);
        liveFingerprintRef.current = taskLiveFingerprint(status);
        if (status.working && status.run) {
          setTaskHistory((prev) => mergeWorkingRun(prev, status.run as TaskRunRow));
        }
        schedule(status.working ? TASKS_ACTIVE_POLL_MS : TAB_IDLE_POLL_MS);
      } catch {
        schedule(TAB_IDLE_POLL_MS);
      }
    });

    const onVisibility = () => {
      window.clearTimeout(timeoutId);
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void tick();
      } else {
        schedule(TAB_HIDDEN_POLL_MS);
      }
    };

    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timeoutId);
    };
  }, [opts.enabled, refreshTasks]);

  return {
    stats,
    scheduledTasks,
    taskHistory,
    tasksLoading,
    refreshTasks,
    refreshWorkingLive,
  };
}
