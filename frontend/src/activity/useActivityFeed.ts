import { useCallback, useEffect, useRef, useState } from "react";
import { getActivityOperations, getPlaceholderActivity } from "../api/dashboard";
import { TAB_HIDDEN_POLL_MS, TAB_IDLE_POLL_MS } from "../dashboard/pollIntervals";
import type { ActivitySubPage } from "../types/api";
import type { PlaceholderActivityRow } from "../types/api";

function digestActivityRows(rows: { id?: string | number; time?: string | null }[]): string {
  if (!rows.length) return "0";
  const first = rows[0];
  return `${rows.length}:${first.id ?? ""}:${first.time ?? ""}`;
}

export function useActivityFeed(opts: {
  subPage: ActivitySubPage;
  enabled: boolean;
  onSuccess?: () => void;
  onError?: (message: string) => void;
}) {
  const [activity, setActivity] = useState<import("../types/api").ActivityRow[]>([]);
  const [placeholderActivity, setPlaceholderActivity] = useState<PlaceholderActivityRow[]>([]);
  const [feedLoading, setFeedLoading] = useState(false);

  const digestRef = useRef("");
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);

  useEffect(() => {
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
  }, [opts.onError, opts.onSuccess]);

  const refreshFeed = useCallback(
    async (options?: { showLoading?: boolean }) => {
      const showLoading = options?.showLoading ?? false;
      if (showLoading) {
        setFeedLoading(true);
      }

      try {
        if (opts.subPage === "operations") {
          const rows = await getActivityOperations(100);
          const digest = digestActivityRows(rows || []);
          if (digest !== digestRef.current) {
            digestRef.current = digest;
            setActivity(rows || []);
          }
        } else if (opts.subPage === "placeholders") {
          const rows = await getPlaceholderActivity(100);
          const digest = digestActivityRows(rows || []);
          if (digest !== digestRef.current) {
            digestRef.current = digest;
            setPlaceholderActivity(rows || []);
          }
        }
        onSuccessRef.current?.();
      } catch (err) {
        onErrorRef.current?.(err instanceof Error ? err.message : "Activity refresh failed");
        throw err;
      } finally {
        if (showLoading) {
          setFeedLoading(false);
        }
      }
    },
    [opts.subPage],
  );

  useEffect(() => {
    if (!opts.enabled || opts.subPage === "tasks") return;

    let stopped = false;
    let timeoutId = 0;

    const schedule = (delayMs: number) => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        void poll();
      }, delayMs);
    };

    const poll = async () => {
      if (stopped) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) {
        schedule(TAB_HIDDEN_POLL_MS);
        return;
      }

      try {
        await refreshFeed();
      } catch {
        /* onError already surfaced */
      }

      if (!stopped) {
        schedule(TAB_IDLE_POLL_MS);
      }
    };

    void refreshFeed({ showLoading: true });

    const onVisibility = () => {
      window.clearTimeout(timeoutId);
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void poll();
      } else {
        schedule(TAB_HIDDEN_POLL_MS);
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    schedule(TAB_IDLE_POLL_MS);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timeoutId);
    };
  }, [opts.enabled, opts.subPage, refreshFeed]);

  return {
    activity,
    placeholderActivity,
    feedLoading,
    refreshFeed,
  };
}
