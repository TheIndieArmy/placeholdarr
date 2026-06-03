import type { LibrarySortKey } from "./librarySort";

export const LIBRARY_MOVIES_SORT_KEY = "placeholdarr:library-sort:movies";
export const LIBRARY_TV_SORT_KEY = "placeholdarr:library-sort:tv";

const SORT_KEYS: LibrarySortKey[] = [
  "title_asc",
  "title_desc",
  "year_desc",
  "year_asc",
  "added_desc",
  "added_asc",
  "updated_desc",
];

export function isLibrarySortKey(v: string | null | undefined): v is LibrarySortKey {
  return v != null && (SORT_KEYS as string[]).includes(v);
}

export function readStoredLibrarySort(storageKey: string): LibrarySortKey {
  try {
    const raw = sessionStorage.getItem(storageKey);
    return isLibrarySortKey(raw) ? raw : "title_asc";
  } catch {
    return "title_asc";
  }
}

export const LIBRARY_SORT_OPTIONS: { id: LibrarySortKey; label: string }[] = [
  { id: "title_asc", label: "Title (A–Z)" },
  { id: "title_desc", label: "Title (Z–A)" },
  { id: "year_desc", label: "Year (newest)" },
  { id: "year_asc", label: "Year (oldest)" },
  { id: "added_desc", label: "Added (newest)" },
  { id: "added_asc", label: "Added (oldest)" },
  { id: "updated_desc", label: "Recently updated" },
];
