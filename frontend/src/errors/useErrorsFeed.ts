import { useCallback, useEffect, useRef, useState } from "react";
import { getErrors } from "../api/dashboard";
import { TAB_HIDDEN_POLL_MS, TAB_IDLE_POLL_MS } from "../dashboard/pollIntervals";
import type { ErrorRow } from "../types/api";

function digestErrors(rows: ErrorRow[]): string {
  if (!rows.length) return "0";
  const first = rows[0];
  return `${rows.length}:${first.time ?? ""}:${first.label}`;
}

export function useErrorsFeed(opts: {
  enabled: boolean;
  onSuccess?: () => void;
  onError?: (message: string) => void;
}) {
  const [errors, setErrors] = useState<ErrorRow[]>([]);
  const [errorsLoading, setErrorsLoading] = useState(false);
  const digestRef = useRef("");
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);

  useEffect(() => {
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
  }, [opts.onError, opts.onSuccess]);

  const refreshErrors = useCallback(async (options?: { showLoading?: boolean }) => {
    const showLoading = options?.showLoading ?? false;
    if (showLoading) {
      setErrorsLoading(true);
    }

    try {
      const rows = await getErrors(100);
      const digest = digestErrors(rows || []);
      if (digest !== digestRef.current) {
        digestRef.current = digest;
        setErrors(rows || []);
      }
      onSuccessRef.current?.();
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Errors refresh failed");
      throw err;
    } finally {
      if (showLoading) {
        setErrorsLoading(false);
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
        await refreshErrors();
      } catch {
        /* onError already surfaced */
      }

      if (!stopped) {
        schedule(TAB_IDLE_POLL_MS);
      }
    };

    void refreshErrors({ showLoading: true });

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
  }, [opts.enabled, refreshErrors]);

  return {
    errors,
    errorsLoading,
    refreshErrors,
  };
}
