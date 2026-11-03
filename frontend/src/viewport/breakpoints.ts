/** Matches Tailwind `md` (see hamburger / sidebar rail). */
export const DESKTOP_MIN_WIDTH_PX = 768;

export const DESKTOP_MEDIA_QUERY = `(min-width: ${DESKTOP_MIN_WIDTH_PX}px)`;

export type ViewportMode = "mobile" | "desktop";

export function viewportModeFromMatchesDesktop(matchesDesktop: boolean): ViewportMode {
  return matchesDesktop ? "desktop" : "mobile";
}
