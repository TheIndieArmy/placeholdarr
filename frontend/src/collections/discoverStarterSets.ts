import { mergeUniqueSorted } from "./CollectionValueMultiSelect";

export type DiscoverStarterPresetKey = "popular" | "latest" | "upcoming" | "all_time_greats";

export const STARTER_PRESET_CHOICES: { key: DiscoverStarterPresetKey; label: string }[] = [
  { key: "popular", label: "Popular" },
  { key: "latest", label: "Latest" },
  { key: "upcoming", label: "Upcoming" },
  { key: "all_time_greats", label: "All-time greats" },
];

export const MOVIE_SHELF_ORDER = [
  "movie:trending",
  "movie:popular-movies",
  "movie:recently-released",
  "movie:upcoming-movies",
  "movie:genres",
] as const;

export const SHOW_SHELF_ORDER = [
  "show:trending",
  "show:popular-series",
  "show:recently-aired",
  "show:upcoming-series",
  "show:genres",
] as const;

export function resolvePresetNames(preset: string[], options: string[]): string[] {
  if (!preset.length || !options.length) return [];
  const byLower = new Map(options.map((o) => [o.toLowerCase(), o]));
  const out: string[] = [];
  const seen = new Set<string>();
  for (const name of preset) {
    const resolved = byLower.get(name.toLowerCase());
    if (!resolved) continue;
    const key = resolved.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(resolved);
  }
  return out;
}

export function valuesFromStarterPresets(
  active: Set<DiscoverStarterPresetKey>,
  presets: Partial<Record<DiscoverStarterPresetKey, string[]>> | null,
  options: string[],
  manualExtras: string[],
): string[] {
  const fromPresets: string[] = [];
  if (presets) {
    for (const key of active) {
      fromPresets.push(...resolvePresetNames(presets[key] || [], options));
    }
  }
  return mergeUniqueSorted(fromPresets, manualExtras);
}

export function manualExtrasFromSelection(
  selected: string[],
  active: Set<DiscoverStarterPresetKey>,
  presets: Partial<Record<DiscoverStarterPresetKey, string[]>> | null,
  options: string[],
): string[] {
  const presetUnion = new Set(
    valuesFromStarterPresets(active, presets, options, []).map((s) => s.toLowerCase()),
  );
  return selected.filter((s) => !presetUnion.has(s.toLowerCase()));
}

export function sortShelvesByOrder<T extends { shelf_id: string }>(list: T[], order: readonly string[]): T[] {
  const rank = new Map(order.map((id, i) => [id, i]));
  return [...list].sort((a, b) => (rank.get(a.shelf_id) ?? 99) - (rank.get(b.shelf_id) ?? 99));
}