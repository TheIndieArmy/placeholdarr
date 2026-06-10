import { useEffect, useRef } from "react";
import { getReady } from "../api/dashboard";
import { STARTUP_READY_FALLBACK_POLL_MS } from "./pollIntervals";

/** Fallback until SSE is connected: poll /api/ready until startup sync completes. */
export function useStartupReadyPoll(opts: {
  enabled: boolean;
  onStartupSyncComplete: (value: boolean) => void;
}) {
  const onCompleteRef = useRef(opts.onStartupSyncComplete);

  useEffect(() => {
    onCompleteRef.current = opts.onStartupSyncComplete;
  }, [opts.onStartupSyncComplete]);

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
        schedule(STARTUP_READY_FALLBACK_POLL_MS);
        return;
      }

      try {
        const ready = await getReady();
        if (stopped) return;
        onCompleteRef.current(Boolean(ready.startup_sync_complete));
        if (!ready.startup_sync_complete && !stopped) {
          schedule(STARTUP_READY_FALLBACK_POLL_MS);
        }
      } catch {
        if (!stopped) {
          schedule(STARTUP_READY_FALLBACK_POLL_MS);
        }
      }
    };

    void poll();

    const onVisibility = () => {
      window.clearTimeout(timeoutId);
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void poll();
      } else {
        schedule(STARTUP_READY_FALLBACK_POLL_MS);
      }
    };

    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timeoutId);
    };
  }, [opts.enabled]);
}
