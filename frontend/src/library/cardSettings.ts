import type { CSSProperties } from "react";

export type LibraryCardVariant = "stacked" | "framed" | "reveal" | "spotlight" | "ticket";

export type LibraryViewMode = "grid" | "list";

export const LIBRARY_CARD_SIZE_MIN = 104;
export const LIBRARY_CARD_SIZE_MAX = 240;
export const LIBRARY_CARD_SIZE_DEFAULT = 144;
/** Fixed Plex-style gutter between library cards (horizontal and vertical). */
export const LIBRARY_GRID_GAP_PX = 8;
/** Inset padding so hover scale (1.02) is not clipped by grid cells. */
export const LIBRARY_GRID_HOVER_PAD_PX = 6;

export function libraryPosterGridStyle(posterWidthPx: number): CSSProperties {
  const w = clampSize(posterWidthPx);
  return {
    display: "grid",
    gridTemplateColumns: `repeat(auto-fill, ${w}px)`,
    gap: `${LIBRARY_GRID_GAP_PX}px`,
    justifyContent: "start",
    alignContent: "start",
    overflow: "visible",
  };
}

/** Wrap each grid card so hover scale is not clipped by grid / content-visibility. */
export const libraryPosterGridItemClassName =
  "relative z-0 min-w-0 overflow-visible box-border hover:z-20";
export function libraryPosterGridItemStyle(): CSSProperties {
  const p = LIBRARY_GRID_HOVER_PAD_PX;
  return { padding: p, margin: -p };
}

export function libraryListStyle(): CSSProperties {
  return {
    display: "flex",
    flexDirection: "column",
    gap: `${LIBRARY_GRID_GAP_PX}px`,
  };
}

const STORAGE_KEY = "placeholdarr:library-card-settings";

export type LibraryCardSettings = {
  variant: LibraryCardVariant;
  posterWidthPx: number;
  viewMode: LibraryViewMode;
};

const VIEW_MODES: LibraryViewMode[] = ["grid", "list"];

function isViewMode(v: string | null): v is LibraryViewMode {
  return v != null && (VIEW_MODES as string[]).includes(v);
}

const VARIANTS: LibraryCardVariant[] = ["stacked", "framed", "reveal", "spotlight", "ticket"];

const REMOVED_VARIANTS = new Set(["compact", "marquee", "banner"]);

function normalizeVariant(v: string | null | undefined): LibraryCardVariant {
  if (v != null && (VARIANTS as string[]).includes(v)) return v as LibraryCardVariant;
  if (v != null && REMOVED_VARIANTS.has(v)) return "ticket";
  return "stacked";
}

function clampSize(n: number): number {
  return Math.min(LIBRARY_CARD_SIZE_MAX, Math.max(LIBRARY_CARD_SIZE_MIN, Math.round(n)));
}

export function readLibraryCardSettings(): LibraryCardSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { variant: "stacked", posterWidthPx: LIBRARY_CARD_SIZE_DEFAULT, viewMode: "grid" };
    }
    const parsed = JSON.parse(raw) as Partial<LibraryCardSettings>;
    const viewMode = parsed.viewMode ?? null;
    return {
      variant: normalizeVariant(parsed.variant ?? null),
      posterWidthPx: clampSize(Number(parsed.posterWidthPx) || LIBRARY_CARD_SIZE_DEFAULT),
      viewMode: isViewMode(viewMode) ? viewMode : "grid",
    };
  } catch {
    return { variant: "stacked", posterWidthPx: LIBRARY_CARD_SIZE_DEFAULT, viewMode: "grid" };
  }
}

export function writeLibraryCardSettings(settings: LibraryCardSettings): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        variant: settings.variant,
        posterWidthPx: clampSize(settings.posterWidthPx),
        viewMode: settings.viewMode,
      }),
    );
  } catch {
    /* private mode */
  }
}

export const LIBRARY_CARD_VARIANT_META: Record<
  LibraryCardVariant,
  { label: string; tagline: string; description: string }
> = {
  stacked: {
    label: "Polaroid",
    tagline: "Instant print",
    description:
      "Thick white frame, drop shadow, and caption under the photo—reads like a physical print on the shelf, not a UI card.",
  },
  framed: {
    label: "Framed",
    tagline: "Inset poster + status bar",
    description: "Poster sits inside the frame with padding; a status bar and metadata sit below the art.",
  },
  reveal: {
    label: "Reveal",
    tagline: "Flip for details",
    description:
      "Poster and title on the front like Compact; hover or focus flips to year, status, instance, episodes, and overview.",
  },
  spotlight: {
    label: "Spotlight",
    tagline: "Poster on stage",
    description:
      "Dark stage card with a large centered poster, soft accent glow, and a compact title strip below—year stays small in the footer.",
  },
  ticket: {
    label: "Stack",
    tagline: "Title · poster · details",
    description:
      "Title, full 2:3 poster, then status bar and metadata—taller card so nothing sits on the art.",
  },
};
