import { useCallback, useEffect, useRef, useState } from "react";
import { getActiveSearches, type ActiveSearchesResponse } from "../api/dashboard";
import { TAB_HIDDEN_POLL_MS, TASKS_ACTIVE_POLL_MS } from "../dashboard/pollIntervals";

const IDLE_POLL_MS = 15_000;

const EMPTY: ActiveSearchesResponse = {
  active: false,
  items: [],
  details: "No titles being monitored",
  started_at: null,
  updated_at: null,
};

export function useActiveSearches(opts: { enabled: boolean }) {
  const [snapshot, setSnapshot] = useState<ActiveSearchesResponse>(EMPTY);
  const enabledRef = useRef(opts.enabled);

  useEffect(() => {
    enabledRef.current = opts.enabled;
  }, [opts.enabled]);

  const refresh = useCallback(async () => {
    try {
      const next = await getActiveSearches();
      setSnapshot(next);
      return next;
    } catch {
      /* keep last good snapshot */
      return null;
    }
  }, []);

  useEffect(() => {
    if (!opts.enabled) {
      setSnapshot(EMPTY);
      return;
    }

    let stopped = false;
    let timeoutId = 0;

    const schedule = (ms: number) => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        void tick();
      }, ms);
    };

    const tick = async () => {
      if (stopped || !enabledRef.current) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) {
        schedule(TAB_HIDDEN_POLL_MS);
        return;
      }
      const next = await refresh();
      if (stopped) return;
      const active = Boolean(next?.active);
      schedule(active ? TASKS_ACTIVE_POLL_MS : IDLE_POLL_MS);
    };

    void tick();

    const onVisibility = () => {
      window.clearTimeout(timeoutId);
      if (stopped) return;
      void tick();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timeoutId);
    };
  }, [opts.enabled, refresh]);

  return { snapshot, refresh };
}
