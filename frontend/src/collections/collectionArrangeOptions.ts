import type { CollectionSortOption } from "../types/api";

export function collectionArrangeSortOptions(
  sectionType: "movie" | "show",
): { value: CollectionSortOption; label: string }[] {
  return [
    { value: "source_order", label: "Source order (as fetched)" },
    { value: "popularity", label: "Popularity score" },
    { value: "release_date", label: "Release date (newest)" },
    {
      value: "latest_aired",
      label:
        sectionType === "show"
          ? "Newest content first (latest aired episode)"
          : "Newest content first",
    },
    { value: "rating", label: "Rating (highest first)" },
    { value: "title", label: "Title (A–Z)" },
    { value: "random", label: "Random (changes each run)" },
    { value: "recent", label: "Recently added to Arr" },
  ];
}