import type { CollectionPreviewResponse, CollectionPreviewSampleItem } from "../../types/api";

export type FilmstripItem = {
  key: string;
  title: string;
  year?: number | null;
  poster: string | null;
  fileState?: CollectionPreviewSampleItem["file_state"];
  /** Plex section ids where this catalog title currently resolves. */
  inLibraries?: number[];
  /** Not in your library yet; auto-add sends it on the next sync. */
  ghost: boolean;
};

/** Final row order: library sample, with would-add titles slotted where they land after the next sync. */
export function buildFilmstrip(
  preview: CollectionPreviewResponse | null | undefined,
  includeGhosts: boolean,
): FilmstripItem[] {
  const sample = preview?.sample ?? [];
  const library: FilmstripItem[] = sample.map((item) => ({
    key: `lib:${item.id}`,
    title: item.title,
    year: item.year,
    poster: item.poster ?? null,
    fileState: item.file_state,
    inLibraries: item.in_libraries ?? [],
    ghost: false,
  }));
  if (!includeGhosts) return library;
  const ghosts = (preview?.missing_from_arr_preview ?? [])
    .filter((item) => item.would_auto_add)
    .map((item, order) => ({
      slot: typeof item.landing_index === "number" ? item.landing_index : Number.POSITIVE_INFINITY,
      order,
      item: {
        key: `add:${item.tmdb_id ?? ""}:${item.tvdb_id ?? ""}:${item.imdb_id ?? ""}:${item.title}`,
        title: item.title,
        year: item.year,
        poster: item.poster,
        ghost: true,
      } satisfies FilmstripItem,
    }))
    .sort((a, b) => a.slot - b.slot || a.order - b.order);
  const out: FilmstripItem[] = [];
  let g = 0;
  library.forEach((row, index) => {
    while (g < ghosts.length && ghosts[g].slot <= index) out.push(ghosts[g++].item);
    out.push(row);
  });
  while (g < ghosts.length) out.push(ghosts[g++].item);
  return out;
}

/**
 * Titles as they appear when visiting the collection from one Plex library.
 * Ghosts (would-add) stay visible. When sectionId is null, return the combined strip.
 */
export function filmstripForLibrary(items: FilmstripItem[], sectionId: number | null | undefined): FilmstripItem[] {
  if (sectionId == null) return items;
  return items.filter((item) => item.ghost || (item.inLibraries ?? []).includes(sectionId));
}
