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
        return (b.year || 0) - (a.year || 0) || titleCompare(a, b, "asc");
      case "year_asc":
        return (a.year || 0) - (b.year || 0) || titleCompare(a, b, "asc");
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
