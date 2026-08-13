import { useEffect, useRef, useState } from "react";
import { openDashboardEventSource } from "../api/dashboard";
import type { DashboardEvent, LibraryVersionResponse } from "../types/api";

export function useDashboardEvents(opts: {
  enabled: boolean;
  onConnected?: () => void;
  onDisconnected?: () => void;
  onStartupSyncComplete?: (value: boolean) => void;
  onLibraryVersion?: (versions: LibraryVersionResponse) => void;
  /** Fires only when the task-runs fingerprint actually changes (a run started/finished). */
  onTaskRunsVersion?: (version: string) => void;
}) {
  const [eventsConnected, setEventsConnected] = useState(false);

  const onConnectedRef = useRef(opts.onConnected);
  const onDisconnectedRef = useRef(opts.onDisconnected);
  const onStartupSyncRef = useRef(opts.onStartupSyncComplete);
  const onLibraryVersionRef = useRef(opts.onLibraryVersion);
  const onTaskRunsVersionRef = useRef(opts.onTaskRunsVersion);
  const lastTaskRunsVersionRef = useRef<string | null>(null);

  useEffect(() => {
    onConnectedRef.current = opts.onConnected;
    onDisconnectedRef.current = opts.onDisconnected;
    onStartupSyncRef.current = opts.onStartupSyncComplete;
    onLibraryVersionRef.current = opts.onLibraryVersion;
    onTaskRunsVersionRef.current = opts.onTaskRunsVersion;
  }, [opts.onConnected, opts.onDisconnected, opts.onLibraryVersion, opts.onStartupSyncComplete, opts.onTaskRunsVersion]);

  useEffect(() => {
    if (!opts.enabled) {
      setEventsConnected(false);
      return;
    }

    let stopped = false;
    let closeStream: (() => void) | null = null;
    let reconnectTimeoutId = 0;

    const clearReconnect = () => {
      window.clearTimeout(reconnectTimeoutId);
      reconnectTimeoutId = 0;
    };

    const markDisconnected = () => {
      if (stopped) return;
      setEventsConnected(false);
      onDisconnectedRef.current?.();
    };

    const handleEvent = (event: DashboardEvent) => {
      if (event.type === "startup_sync_complete") {
        onStartupSyncRef.current?.(Boolean(event.value));
        return;
      }
      if (event.type === "library_version") {
        onLibraryVersionRef.current?.({
          movies_version: Number(event.movies_version) || 0,
          series_version: Number(event.series_version) || 0,
        });
        return;
      }
      if (event.type === "task_runs_version") {
        const version = String(event.version ?? "");
        // The SSE loop re-sends all state events whenever any of them changes;
        // dedupe here so consumers only react to real task-run transitions.
        if (lastTaskRunsVersionRef.current === version) return;
        const isFirst = lastTaskRunsVersionRef.current === null;
        lastTaskRunsVersionRef.current = version;
        if (!isFirst) {
          onTaskRunsVersionRef.current?.(version);
        }
      }
    };

    const startStream = () => {
      if (closeStream) {
        closeStream();
        closeStream = null;
      }
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") {
        return;
      }

      closeStream = openDashboardEventSource({
        onOpen: () => {
          if (stopped) return;
          setEventsConnected(true);
          onConnectedRef.current?.();
        },
        onEvent: handleEvent,
        onError: () => {
          markDisconnected();
          if (!stopped) {
            clearReconnect();
            reconnectTimeoutId = window.setTimeout(() => {
              startStream();
            }, 2_000);
          }
        },
      });
    };

    startStream();

    const onVisibility = () => {
      clearReconnect();
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        startStream();
      } else if (closeStream) {
        closeStream();
        closeStream = null;
        markDisconnected();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clearReconnect();
      if (closeStream) {
        closeStream();
      }
      setEventsConnected(false);
    };
  }, [opts.enabled]);

  return { eventsConnected };
}
