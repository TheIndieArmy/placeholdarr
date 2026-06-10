import { useEffect, useRef } from "react";
import { getHealth } from "../api/dashboard";

const HEALTH_BACKOFF_MS = [2_000, 5_000, 15_000, 30_000] as const;

export function useApiHealthCheck(opts: {
  /** When false (SSE connected), polling is idle except a rare safety probe on tab focus. */
  enabled: boolean;
  onHealthy?: () => void;
  onUnhealthy?: (message: string) => void;
}) {
  const onHealthyRef = useRef(opts.onHealthy);
  const onUnhealthyRef = useRef(opts.onUnhealthy);

  useEffect(() => {
    onHealthyRef.current = opts.onHealthy;
    onUnhealthyRef.current = opts.onUnhealthy;
  }, [opts.onHealthy, opts.onUnhealthy]);

  useEffect(() => {
    if (!opts.enabled) return;

    let stopped = false;
    let timeoutId = 0;
    let attempt = 0;

    const clearTimer = () => {
      window.clearTimeout(timeoutId);
      timeoutId = 0;
    };

    const schedule = (delayMs: number) => {
      clearTimer();
      timeoutId = window.setTimeout(() => {
        void probe();
      }, delayMs);
    };

    const probe = async () => {
      if (stopped) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) {
        schedule(HEALTH_BACKOFF_MS[HEALTH_BACKOFF_MS.length - 1]);
        return;
      }

      try {
        await getHealth();
        if (stopped) return;
        attempt = 0;
        onHealthyRef.current?.();
      } catch (err) {
        if (!stopped) {
          onUnhealthyRef.current?.(
            err instanceof Error ? err.message : "Cannot reach the Placeholdarr API (network error). Trying to reconnect…",
          );
        }
        const delay = HEALTH_BACKOFF_MS[Math.min(attempt, HEALTH_BACKOFF_MS.length - 1)];
        attempt += 1;
        if (!stopped) {
          schedule(delay);
        }
        return;
      }

      if (!stopped) {
        schedule(HEALTH_BACKOFF_MS[HEALTH_BACKOFF_MS.length - 1]);
      }
    };

    void probe();

    const onVisibility = () => {
      clearTimer();
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void probe();
      } else {
        schedule(HEALTH_BACKOFF_MS[HEALTH_BACKOFF_MS.length - 1]);
      }
    };

    const onOnline = () => {
      clearTimer();
      if (!stopped) {
        void probe();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onOnline);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onOnline);
      clearTimer();
    };
  }, [opts.enabled]);
}
