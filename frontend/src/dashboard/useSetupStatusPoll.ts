import { useEffect, useRef } from "react";
import { getSettingsStatus } from "../api/dashboard";
import type { SettingsStatus } from "../types/api";
import {
  clearSetupCompleteInSession,
  markSetupCompleteInSession,
} from "./setupSession";

/** Fetch onboarding/setup status on demand (mount + tab focus). No periodic interval. */
export function useSetupStatusPoll(opts: {
  enabled: boolean;
  onStatus: (status: SettingsStatus) => void;
  onError?: (message: string) => void;
}) {
  const onStatusRef = useRef(opts.onStatus);
  const onErrorRef = useRef(opts.onError);

  useEffect(() => {
    onStatusRef.current = opts.onStatus;
    onErrorRef.current = opts.onError;
  }, [opts.onError, opts.onStatus]);

  useEffect(() => {
    if (!opts.enabled) return;

    let stopped = false;

    const applyStatus = (status: SettingsStatus) => {
      if (status.setup_complete) {
        markSetupCompleteInSession();
      } else {
        clearSetupCompleteInSession();
      }
      onStatusRef.current(status);
    };

    const refresh = async () => {
      if (stopped) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) return;

      try {
        const status = await getSettingsStatus();
        if (!stopped) {
          applyStatus(status);
        }
      } catch (err) {
        if (!stopped) {
          onErrorRef.current?.(err instanceof Error ? err.message : "Unable to load setup status");
        }
      }
    };

    void refresh();

    const onVisibility = () => {
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void refresh();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [opts.enabled]);
}
