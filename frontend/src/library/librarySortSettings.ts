import type { LibrarySortKey } from "./librarySort";

export const LIBRARY_MOVIES_SORT_KEY = "placeholdarr:library-sort:movies";
export const LIBRARY_TV_SORT_KEY = "placeholdarr:library-sort:tv";

export type LibraryShelfSortKey = "movies" | "tv";

const SHARED_SORT_KEYS: LibrarySortKey[] = [
  "title_asc",
  "title_desc",
  "added_desc",
  "added_asc",
  "updated_desc",
];

const MOVIES_SORT_KEYS: LibrarySortKey[] = [
  "year_desc",
  "year_asc",
  "theater_desc",
  "theater_asc",
  "digital_desc",
  "digital_asc",
  "physical_desc",
  "physical_asc",
  ...SHARED_SORT_KEYS,
];

const TV_SORT_KEYS: LibrarySortKey[] = [
  "premiere_desc",
  "premiere_asc",
  "last_aired_desc",
  "last_aired_asc",
  ...SHARED_SORT_KEYS,
];

const ALL_SORT_KEYS: LibrarySortKey[] = [...new Set([...MOVIES_SORT_KEYS, ...TV_SORT_KEYS])];

function sortKeysForShelf(shelf: LibraryShelfSortKey): LibrarySortKey[] {
  return shelf === "tv" ? TV_SORT_KEYS : MOVIES_SORT_KEYS;
}

function migrateLegacySortKey(raw: string | null, shelf: LibraryShelfSortKey): string | null {
  if (raw === "release_desc") return shelf === "tv" ? "premiere_desc" : "year_desc";
  if (raw === "release_asc") return shelf === "tv" ? "premiere_asc" : "year_asc";
  return raw;
}

export function isLibrarySortKey(v: string | null | undefined, shelf?: LibraryShelfSortKey): v is LibrarySortKey {
  if (v == null) return false;
  if (shelf != null) {
    return (sortKeysForShelf(shelf) as string[]).includes(v);
  }
  return (ALL_SORT_KEYS as string[]).includes(v);
}

export function readStoredLibrarySort(storageKey: string, shelf: LibraryShelfSortKey): LibrarySortKey {
  const allowed = sortKeysForShelf(shelf);
  const fallback: LibrarySortKey = "title_asc";
  try {
    const raw = sessionStorage.getItem(storageKey);
    const migrated = migrateLegacySortKey(raw, shelf);
    if (migrated != null && (allowed as string[]).includes(migrated)) {
      return migrated as LibrarySortKey;
    }
    return fallback;
  } catch {
    return fallback;
  }
}

export const LIBRARY_MOVIES_SORT_OPTIONS: { id: LibrarySortKey; label: string }[] = [
  { id: "title_asc", label: "Title (A–Z)" },
  { id: "title_desc", label: "Title (Z–A)" },
  { id: "year_desc", label: "Year (newest)" },
  { id: "year_asc", label: "Year (oldest)" },
  { id: "theater_desc", label: "Theatrical (newest)" },
  { id: "theater_asc", label: "Theatrical (oldest)" },
  { id: "digital_desc", label: "Digital (newest)" },
  { id: "digital_asc", label: "Digital (oldest)" },
  { id: "physical_desc", label: "Physical (newest)" },
  { id: "physical_asc", label: "Physical (oldest)" },
  { id: "added_desc", label: "Added (newest)" },
  { id: "added_asc", label: "Added (oldest)" },
  { id: "updated_desc", label: "Recently updated" },
];

export const LIBRARY_TV_SORT_OPTIONS: { id: LibrarySortKey; label: string }[] = [
  { id: "title_asc", label: "Title (A–Z)" },
  { id: "title_desc", label: "Title (Z–A)" },
  { id: "premiere_desc", label: "Premiere (newest)" },
  { id: "premiere_asc", label: "Premiere (oldest)" },
  { id: "last_aired_desc", label: "Last aired (newest)" },
  { id: "last_aired_asc", label: "Last aired (oldest)" },
  { id: "added_desc", label: "Added (newest)" },
  { id: "added_asc", label: "Added (oldest)" },
  { id: "updated_desc", label: "Recently updated" },
];

export function librarySortOptionsForShelf(shelf: LibraryShelfSortKey): { id: LibrarySortKey; label: string }[] {
  return shelf === "tv" ? LIBRARY_TV_SORT_OPTIONS : LIBRARY_MOVIES_SORT_OPTIONS;
}
