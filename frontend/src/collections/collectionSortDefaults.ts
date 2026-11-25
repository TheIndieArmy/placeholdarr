import type { CollectionSortOption, CollectionSourceBlock } from "../types/api";

const TMDB_RANKED_SUBTYPES = new Set(["trending", "popular", "upcoming", "page"]);

function sourcePreservesFetchOrder(block: CollectionSourceBlock): boolean {
  if (block.type === "catalog") return false;
  if (
    block.type === "tmdb_trending" ||
    block.type === "tmdb_popular" ||
    block.type === "tmdb_upcoming" ||
    block.type === "tmdb_discover" ||
    block.type === "tmdb_list" ||
    block.type === "tmdb_url" ||
    block.type === "tmdb_person" ||
    block.type === "tmdb_company" ||
    block.type === "tmdb_keyword" ||
    block.type === "tmdb_collection" ||
    block.type === "mdblist" ||
    block.type === "trakt_list" ||
    block.type === "trakt_chart" ||
    block.type === "stevenlu" ||
    block.type === "anilist" ||
    block.type === "tautulli" ||
    block.type === "arr_tag"
  ) {
    return true;
  }
  if (block.type === "tmdb") {
    const subtype = String(block.subtype || "trending")
      .trim()
      .toLowerCase();
    return TMDB_RANKED_SUBTYPES.has(subtype);
  }
  if (block.type === "trakt") return true;
  return false;
}

export function defaultSortForSources(sources: CollectionSourceBlock[]): CollectionSortOption {
  if (sources.some((s) => s.type === "catalog")) return "popularity";
  if (sources.length > 0 && sources.every(sourcePreservesFetchOrder)) return "source_order";
  return "popularity";
}

/** True when arrange still matches factory defaults (auto-follow source changes). */
export function isAutoArrangeSort(sort: CollectionSortOption | null | undefined): boolean {
  return sort == null || sort === "popularity" || sort === "source_order";
}