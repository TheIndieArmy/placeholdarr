import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  DESKTOP_MEDIA_QUERY,
  type ViewportMode,
  viewportModeFromMatchesDesktop,
} from "./breakpoints";

export type ViewportModeValue = {
  mode: ViewportMode;
  isMobile: boolean;
  isDesktop: boolean;
};

const ViewportModeContext = createContext<ViewportModeValue | null>(null);

function readViewportMode(): ViewportMode {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "desktop";
  }
  return viewportModeFromMatchesDesktop(window.matchMedia(DESKTOP_MEDIA_QUERY).matches);
}

export function ViewportModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ViewportMode>(() => readViewportMode());

  useEffect(() => {
    const mq = window.matchMedia(DESKTOP_MEDIA_QUERY);
    const sync = () => setMode(viewportModeFromMatchesDesktop(mq.matches));
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const value = useMemo<ViewportModeValue>(
    () => ({
      mode,
      isMobile: mode === "mobile",
      isDesktop: mode === "desktop",
    }),
    [mode],
  );

  return <ViewportModeContext.Provider value={value}>{children}</ViewportModeContext.Provider>;
}

export function useViewportMode(): ViewportModeValue {
  const ctx = useContext(ViewportModeContext);
  if (!ctx) {
    throw new Error("useViewportMode must be used within ViewportModeProvider");
  }
  return ctx;
}
