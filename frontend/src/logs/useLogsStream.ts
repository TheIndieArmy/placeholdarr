import { useCallback, useEffect, useRef, useState } from "react";
import { getLogs, openLogsEventSource } from "../api/dashboard";
import { TAB_HIDDEN_POLL_MS } from "../dashboard/pollIntervals";

export type LogLevelFilter = "all" | "debug" | "info" | "warn" | "error" | "critical";

const SSE_FALLBACK_POLL_MS = 2_000;

export function useLogsStream(opts: {
  enabled: boolean;
  level: LogLevelFilter;
  tailLines: number;
  onSuccess?: () => void;
  onError?: (message: string) => void;
}) {
  const [logs, setLogs] = useState<string[]>([]);
  const [logFile, setLogFile] = useState("");
  const [logCaptureLevel, setLogCaptureLevel] = useState("");
  const [logsLoading, setLogsLoading] = useState(false);

  const levelRef = useRef(opts.level);
  const tailRef = useRef(opts.tailLines);
  const latestIdRef = useRef(0);
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);

  useEffect(() => {
    levelRef.current = opts.level;
    tailRef.current = opts.tailLines;
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
  }, [opts.level, opts.onError, opts.onSuccess, opts.tailLines]);

  const appendLiveLine = useCallback((line: string) => {
    const maxLines = Math.max(1, tailRef.current);
    setLogs((prev) => {
      const next = [...prev, line];
      return next.length > maxLines ? next.slice(-maxLines) : next;
    });
  }, []);

  const refreshLogs = useCallback(async (options?: { showLoading?: boolean }) => {
    const showLoading = options?.showLoading ?? false;
    if (showLoading) {
      setLogsLoading(true);
    }

    try {
      const payload = await getLogs(levelRef.current, tailRef.current);
      setLogs(payload.lines || []);
      setLogFile(payload.file || "");
      setLogCaptureLevel(payload.capture_level ?? "");
      latestIdRef.current = Math.max(0, payload.latest_id ?? 0);
      onSuccessRef.current?.();
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Logs refresh failed");
      throw err;
    } finally {
      if (showLoading) {
        setLogsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!opts.enabled) return;

    let stopped = false;
    let closeStream: (() => void) | null = null;
    let fallbackTimeoutId = 0;

    const clearFallback = () => {
      window.clearTimeout(fallbackTimeoutId);
      fallbackTimeoutId = 0;
    };

    const scheduleFallbackPoll = (delayMs: number) => {
      clearFallback();
      fallbackTimeoutId = window.setTimeout(() => {
        void pollIncremental();
      }, delayMs);
    };

    const pollIncremental = async () => {
      if (stopped) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) {
        scheduleFallbackPoll(TAB_HIDDEN_POLL_MS);
        return;
      }

      try {
        const payload = await getLogs(levelRef.current, tailRef.current, latestIdRef.current);
        const lines = payload.lines || [];
        if (lines.length > 0) {
          setLogs((prev) => {
            const maxLines = Math.max(1, tailRef.current);
            const next = [...prev, ...lines];
            return next.length > maxLines ? next.slice(-maxLines) : next;
          });
        }
        if (typeof payload.latest_id === "number") {
          latestIdRef.current = Math.max(latestIdRef.current, payload.latest_id);
        }
        onSuccessRef.current?.();
      } catch {
        /* onError already surfaced on full refresh */
      }

      if (!stopped) {
        scheduleFallbackPoll(SSE_FALLBACK_POLL_MS);
      }
    };

    const stopStream = () => {
      if (closeStream) {
        closeStream();
        closeStream = null;
      }
    };

    const startStream = () => {
      stopStream();
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") {
        return;
      }

      closeStream = openLogsEventSource(levelRef.current, latestIdRef.current, {
        onLine: (id, line) => {
          latestIdRef.current = Math.max(latestIdRef.current, id);
          appendLiveLine(line);
          onSuccessRef.current?.();
        },
        onError: () => {
          if (!stopped) {
            scheduleFallbackPoll(SSE_FALLBACK_POLL_MS);
          }
        },
      });
    };

    const bootstrap = async () => {
      try {
        await refreshLogs({ showLoading: true });
      } catch {
        scheduleFallbackPoll(SSE_FALLBACK_POLL_MS);
        return;
      }
      if (!stopped) {
        startStream();
      }
    };

    void bootstrap();

    const onVisibility = () => {
      clearFallback();
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void refreshLogs().then(() => {
          if (!stopped) {
            startStream();
          }
        });
      } else {
        stopStream();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clearFallback();
      stopStream();
    };
  }, [appendLiveLine, opts.enabled, opts.level, refreshLogs]);

  return {
    logs,
    logFile,
    logCaptureLevel,
    logsLoading,
    refreshLogs,
  };
}
