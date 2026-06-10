import { useCallback, useEffect, useRef, useState } from "react";
import { getStats } from "../api/dashboard";
import { getTasksHistory, getTasksScheduled, getTasksStatus } from "../api/tasks";
import { TAB_HIDDEN_POLL_MS, TAB_IDLE_POLL_MS, TASKS_ACTIVE_POLL_MS } from "../dashboard/pollIntervals";
import type { ScheduledTaskRow, StatsResponse, TaskRunRow, TaskRunStatusResponse } from "../types/api";

function taskStatusFingerprint(status: TaskRunStatusResponse): string {
  if (!status.working || !status.run) {
    return "idle";
  }
  const run = status.run;
  return `${run.id}:${run.status}:${run.ended_at ?? ""}:${JSON.stringify(run.progress ?? null)}`;
}

export function useActivityTasks(opts: {
  enabled: boolean;
  onSuccess?: () => void;
  onError?: (message: string) => void;
  onRefreshing?: (refreshing: boolean) => void;
}) {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTaskRow[]>([]);
  const [taskHistory, setTaskHistory] = useState<TaskRunRow[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);

  const statusFingerprintRef = useRef("idle");
  const workingRef = useRef(false);
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);
  const onRefreshingRef = useRef(opts.onRefreshing);

  useEffect(() => {
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
    onRefreshingRef.current = opts.onRefreshing;
  }, [opts.onError, opts.onRefreshing, opts.onSuccess]);

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
      const [payload, sched, hist] = await Promise.all([getStats(), getTasksScheduled(), getTasksHistory(50)]);
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

      try {
        const status = await getTasksStatus();
        if (stopped) return;

        const fingerprint = taskStatusFingerprint(status);
        const statusChanged = fingerprint !== statusFingerprintRef.current;
        statusFingerprintRef.current = fingerprint;
        workingRef.current = status.working;

        if (status.working) {
          if (statusChanged) {
            await refreshTasks({ showChrome: true });
          }
          schedule(TASKS_ACTIVE_POLL_MS);
        } else {
          await refreshTasks();
          schedule(TAB_IDLE_POLL_MS);
        }
      } catch (err) {
        if (!stopped) {
          onErrorRef.current?.(err instanceof Error ? err.message : "Tasks refresh failed");
        }
        schedule(TAB_IDLE_POLL_MS);
      }
    };

    void refreshTasks({ showLoading: true, showChrome: true }).then(() => {
      if (!stopped) {
        schedule(workingRef.current ? TASKS_ACTIVE_POLL_MS : TAB_IDLE_POLL_MS);
      }
    });

    void getTasksStatus()
      .then((status) => {
        if (stopped) return;
        workingRef.current = status.working;
        statusFingerprintRef.current = taskStatusFingerprint(status);
      })
      .catch(() => {
        /* initial full refresh still runs */
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
  };
}
