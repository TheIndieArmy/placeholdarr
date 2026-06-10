import { useCallback, useEffect, useRef, useState } from "react";
import { getCalendar } from "../api/dashboard";
import { TAB_HIDDEN_POLL_MS, TAB_IDLE_POLL_MS } from "../dashboard/pollIntervals";
import type { CalendarResponse } from "../types/api";

export function useCalendarData(opts: {
  enabled: boolean;
  month: string;
  onMonthResolved?: (month: string) => void;
  onSuccess?: () => void;
  onError?: (message: string) => void;
}) {
  const [calendar, setCalendar] = useState<CalendarResponse | null>(null);
  const [calendarLoading, setCalendarLoading] = useState(false);

  const monthRef = useRef(opts.month);
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);
  const onMonthResolvedRef = useRef(opts.onMonthResolved);

  useEffect(() => {
    monthRef.current = opts.month;
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
    onMonthResolvedRef.current = opts.onMonthResolved;
  }, [opts.month, opts.onError, opts.onMonthResolved, opts.onSuccess]);

  const refreshCalendar = useCallback(async (options?: { showLoading?: boolean; month?: string }) => {
    const showLoading = options?.showLoading ?? false;
    const month = options?.month ?? monthRef.current;
    if (showLoading) {
      setCalendarLoading(true);
    }

    try {
      const payload = await getCalendar(month);
      if (payload.ok) {
        setCalendar(payload);
        if (payload.month) {
          onMonthResolvedRef.current?.(payload.month);
        }
        onSuccessRef.current?.();
      }
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Calendar refresh failed");
      throw err;
    } finally {
      if (showLoading) {
        setCalendarLoading(false);
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
        await refreshCalendar();
      } catch {
        /* onError already surfaced */
      }

      if (!stopped) {
        schedule(TAB_IDLE_POLL_MS);
      }
    };

    void refreshCalendar({ showLoading: true, month: opts.month });

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
  }, [opts.enabled, opts.month, refreshCalendar]);

  return {
    calendar,
    calendarLoading,
    refreshCalendar,
  };
}
