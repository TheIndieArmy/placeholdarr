import type { HealthResponse } from "./types/api";

const RELOAD_GUARD_KEY = "placeholdarr-frontend-reload";

export function loadedFrontendBuildId(): string {
  if (typeof document === "undefined") return "";
  const scripts = document.getElementsByTagName("script");
  for (let i = 0; i < scripts.length; i += 1) {
    const src = scripts[i]?.getAttribute("src") || "";
    const match = src.match(/\/assets\/index-[^/?#]+\.js/);
    if (match) {
      try {
        return new URL(src, window.location.origin).pathname;
      } catch {
        return match[0];
      }
    }
  }
  return "";
}

/** Reload this tab when the running JS bundle is older than dist/index.html. */
export function reloadIfFrontendStale(health: HealthResponse | null | undefined): void {
  const remote = String(health?.frontend_build || "").trim();
  const local = loadedFrontendBuildId();
  if (!remote || !local) return;
  if (remote === local) {
    try {
      sessionStorage.removeItem(RELOAD_GUARD_KEY);
    } catch {
      /* ignore */
    }
    return;
  }
  try {
    if (sessionStorage.getItem(RELOAD_GUARD_KEY) === remote) return;
    sessionStorage.setItem(RELOAD_GUARD_KEY, remote);
  } catch {
    /* still reload */
  }
  window.location.reload();
}
