import type { LibraryItem } from "../types/api";

export function titleSortKey(title: string | null | undefined): string {
  const raw = String(title || "").trim().toLowerCase();
  return raw
    .replace(/^[^a-z0-9]+/i, "")
    .replace(/^(the|an|a)\s+/i, "")
    .replace(/^[^a-z0-9]+/i, "");
}

export function titleSortLetter(title: string | null | undefined): string {
  const key = titleSortKey(title);
  const first = key.charAt(0).toUpperCase();
  return /[A-Z]/.test(first) ? first : "#";
}

export type LibrarySortKey =
  | "title_asc"
  | "title_desc"
  | "year_desc"
  | "year_asc"
  | "theater_desc"
  | "theater_asc"
  | "digital_desc"
  | "digital_asc"
  | "physical_desc"
  | "physical_asc"
  | "premiere_desc"
  | "premiere_asc"
  | "last_aired_desc"
  | "last_aired_asc"
  | "added_desc"
  | "added_asc"
  | "updated_desc";

export function sortUsesAlphaSections(key: LibrarySortKey): boolean {
  return key === "title_asc" || key === "title_desc";
}

function sortTimestamp(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

function titleCompare(a: LibraryItem, b: LibraryItem, direction: "asc" | "desc"): number {
  const cmp = titleSortKey(a.title).localeCompare(titleSortKey(b.title));
  return direction === "asc" ? cmp : -cmp;
}

/** Radarr/Sonarr-style year ordering: year then title A–Z within the same year. */
function compareYear(a: LibraryItem, b: LibraryItem, direction: "asc" | "desc"): number {
  const ya = a.year || 0;
  const yb = b.year || 0;
  if (ya !== yb) {
    return direction === "desc" ? yb - ya : ya - yb;
  }
  return titleCompare(a, b, "asc");
}

/** Release-type sorts use only that date column; undated rows sort after dated rows. */
function compareReleaseDate(
  a: LibraryItem,
  b: LibraryItem,
  pick: (item: LibraryItem) => string | null | undefined,
  direction: "asc" | "desc",
): number {
  const ta = sortTimestamp(pick(a));
  const tb = sortTimestamp(pick(b));
  const desc = direction === "desc";

  if (ta === null && tb === null) {
    return titleCompare(a, b, "asc");
  }
  if (ta === null) return 1;
  if (tb === null) return -1;
  if (ta !== tb) {
    return desc ? tb - ta : ta - tb;
  }
  return titleCompare(a, b, "asc");
}

function comparePremiereOrAired(
  a: LibraryItem,
  b: LibraryItem,
  pick: (item: LibraryItem) => string | null | undefined,
  direction: "asc" | "desc",
): number {
  const ta = sortTimestamp(pick(a)) ?? (a.year > 0 ? Date.UTC(a.year, 0, 1) : null);
  const tb = sortTimestamp(pick(b)) ?? (b.year > 0 ? Date.UTC(b.year, 0, 1) : null);
  const desc = direction === "desc";

  if (ta === null && tb === null) {
    return titleCompare(a, b, "asc");
  }
  if (ta === null) return 1;
  if (tb === null) return -1;
  if (ta !== tb) {
    return desc ? tb - ta : ta - tb;
  }
  return titleCompare(a, b, "asc");
}

/** Full-resolution insert time; missing timestamps sort after dated rows. */
function compareCatalogTimestamp(
  a: LibraryItem,
  b: LibraryItem,
  field: "created_at" | "updated_at",
  direction: "asc" | "desc",
): number {
  const ta = sortTimestamp(a[field]);
  const tb = sortTimestamp(b[field]);
  const desc = direction === "desc";

  if (ta === null && tb === null) {
    return desc ? b.item_id - a.item_id : a.item_id - b.item_id;
  }
  if (ta === null) return 1;
  if (tb === null) return -1;
  if (ta !== tb) {
    return desc ? tb - ta : ta - tb;
  }
  return desc ? b.item_id - a.item_id : a.item_id - b.item_id;
}

/** Stable library ordering for the active shelf sort control. */
export function sortLibraryItems(items: LibraryItem[], key: LibrarySortKey): LibraryItem[] {
  const out = [...items];
  out.sort((a, b) => {
    switch (key) {
      case "title_asc":
        return titleCompare(a, b, "asc");
      case "title_desc":
        return titleCompare(a, b, "desc");
      case "year_desc":
        return compareYear(a, b, "desc");
      case "year_asc":
        return compareYear(a, b, "asc");
      case "theater_desc":
        return compareReleaseDate(a, b, (item) => item.theater_release_date, "desc");
      case "theater_asc":
        return compareReleaseDate(a, b, (item) => item.theater_release_date, "asc");
      case "digital_desc":
        return compareReleaseDate(a, b, (item) => item.digital_release_date, "desc");
      case "digital_asc":
        return compareReleaseDate(a, b, (item) => item.digital_release_date, "asc");
      case "physical_desc":
        return compareReleaseDate(a, b, (item) => item.physical_release_date, "desc");
      case "physical_asc":
        return compareReleaseDate(a, b, (item) => item.physical_release_date, "asc");
      case "premiere_desc":
        return comparePremiereOrAired(a, b, (item) => item.premiere_date, "desc");
      case "premiere_asc":
        return comparePremiereOrAired(a, b, (item) => item.premiere_date, "asc");
      case "last_aired_desc":
        return comparePremiereOrAired(a, b, (item) => item.last_aired_date, "desc");
      case "last_aired_asc":
        return comparePremiereOrAired(a, b, (item) => item.last_aired_date, "asc");
      case "added_desc":
        return compareCatalogTimestamp(a, b, "created_at", "desc");
      case "added_asc":
        return compareCatalogTimestamp(a, b, "created_at", "asc");
      case "updated_desc":
        return compareCatalogTimestamp(a, b, "updated_at", "desc");
      default:
        return titleCompare(a, b, "asc");
    }
  });
  return out;
}

export function groupLibraryItemsByLetter(items: LibraryItem[]): { groups: Record<string, LibraryItem[]>; letters: string[] } {
  const groups: Record<string, LibraryItem[]> = {};
  items.forEach((item) => {
    const letter = titleSortLetter(item.title);
    if (!groups[letter]) groups[letter] = [];
    groups[letter].push(item);
  });
  const letters = Object.keys(groups).sort((a, b) => {
    if (a === "#") return 1;
    if (b === "#") return -1;
    return a.localeCompare(b);
  });
  return { groups, letters };
}
