import { fetchJson, postJson, postNdjson, putJson } from "./client";
import type {
  CollectionActiveWindow,
  CollectionArrAddItem,
  CollectionArrAddOptionsResponse,
  CollectionBuilderMeta,
  CollectionDefinition,
  CollectionExplainResponse,
  CollectionPinnedItem,
  CollectionPreviewResponse,
  CollectionRecipe,
  CollectionRecipesResponse,
  CollectionTmdbMeta,
  PlexSectionOption,
} from "../types/api";

/** Must match `ARR_ADD_BATCH_CAP` in `routes/collections.py`. */
export const ARR_ADD_BATCH_CAP = 100;

export function normalizeArrTagLabel(label: string): string {
  return label
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export interface RecipeWritePayload {
  name: string;
  enabled: boolean;
  plex_section_id: number;
  plex_section_ids?: number[];
  plex_section_type: "movie" | "show";
  collection_title: string;
  definition: CollectionDefinition;
  run_interval_hours: number | null;
  run_schedule?: Record<string, unknown> | null;
  active_window: CollectionActiveWindow | null;
}

export function getCollectionRecipes(): Promise<CollectionRecipesResponse> {
  return fetchJson<CollectionRecipesResponse>("/api/collections");
}

export function createCollectionRecipe(payload: RecipeWritePayload): Promise<{ ok: boolean; recipe: CollectionRecipe }> {
  return postJson("/api/collections", payload);
}

export async function updateCollectionRecipe(
  id: number,
  payload: RecipeWritePayload,
): Promise<{ ok: boolean; recipe: CollectionRecipe }> {
  return fetchJson(`/api/collections/${id}`, {
    method: "PUT",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function toggleCollectionRecipe(id: number, enabled: boolean): Promise<{ ok: boolean; recipe: CollectionRecipe }> {
  return postJson(`/api/collections/${id}/toggle`, { enabled });
}

export async function deleteCollectionRecipe(
  id: number,
  options?: { deletePlex?: boolean },
): Promise<{ ok: boolean; delete_plex?: boolean; plex_deleted?: number }> {
  const qs = options?.deletePlex ? "?delete_plex=true" : "";
  return fetchJson(`/api/collections/${id}${qs}`, { method: "DELETE" });
}

export function runCollectionRecipe(id: number): Promise<{ ok: boolean; recipe_id: number; message: string }> {
  return postJson(`/api/collections/${id}/run`);
}

export function previewCollectionDefinition(payload: {
  plex_section_id: number;
  plex_section_ids?: number[];
  plex_section_type: "movie" | "show";
  definition: CollectionDefinition;
}): Promise<CollectionPreviewResponse> {
  return postJson("/api/collections/preview", payload);
}

export interface CollectionTitleConflict {
  title: string;
  section_id: number;
  section_title: string;
  rating_key?: string | null;
  item_count: number;
  reason: "unlabeled" | "other_recipe" | "ours" | string;
}

export function checkCollectionTitleConflicts(payload: {
  plex_section_id: number;
  plex_section_ids?: number[];
  plex_section_type: "movie" | "show";
  collection_title: string;
  definition: CollectionDefinition;
  recipe_id?: number | null;
}): Promise<{ conflicts: CollectionTitleConflict[]; titles_checked: string[] }> {
  return postJson("/api/collections/title-conflicts", payload);
}

export function getCollectionPlexSections(opts?: {
  refresh?: boolean;
}): Promise<{ sections: PlexSectionOption[] }> {
  const params = new URLSearchParams();
  if (opts?.refresh) params.set("refresh", "true");
  const qs = params.toString();
  return fetchJson(`/api/collections/plex-sections${qs ? `?${qs}` : ""}`);
}

export function getCollectionTmdbMeta(mediaType: "movie" | "tv", region: string): Promise<CollectionTmdbMeta> {
  const params = new URLSearchParams({ media_type: mediaType, region });
  return fetchJson(`/api/collections/tmdb-meta?${params.toString()}`);
}

export function getCollectionBuilderMeta(mediaType: "movie" | "show"): Promise<CollectionBuilderMeta> {
  const params = new URLSearchParams({ media_type: mediaType });
  return fetchJson(`/api/collections/builder-meta?${params.toString()}`);
}

export function explainCollectionItem(payload: {
  plex_section_id: number;
  plex_section_type: "movie" | "show";
  definition: CollectionDefinition;
  item: CollectionPinnedItem;
}): Promise<CollectionExplainResponse> {
  return postJson("/api/collections/explain", payload);
}

export function getCollectionArrAddOptions(mediaType: "movie" | "show"): Promise<CollectionArrAddOptionsResponse> {
  const params = new URLSearchParams({ media_type: mediaType });
  return fetchJson(`/api/collections/arr-add-options?${params.toString()}`);
}

export type DiscoverPresetShelf = {
  shelf_id: string;
  name?: string;
  description?: string | null;
  plex_section_type?: "movie" | "show";
  collection_title?: string;
  kind?: "recipe" | "collection_set";
  set_category?: string | null;
  selectable_values?: string[] | null;
  multi_value_hint?: string | null;
  default_sort?: string | null;
  default_limit?: number | null;
  release_basis_configurable?: boolean;
  default_release_basis?: string | null;
};

export type DiscoverShelfLayout = { sort: string; limit: number; release_basis?: string };

export type DiscoverGenrePresetKey = "popular" | "latest" | "upcoming" | "all_time_greats";

export type DiscoverPresetPreview = {
  shelves?: DiscoverPresetShelf[];
  recipes: { name?: string; plex_section_type?: string; collection_title?: string }[];
  hub_order?: { movie?: string[]; show?: string[] };
  default_limit?: number;
  tmdb_genres?: { movie?: string[]; show?: string[] };
  tmdb_configured?: boolean;
  genre_presets?: Record<"movie" | "show", Record<DiscoverGenrePresetKey, string[]>>;
  studio_presets?: Record<DiscoverGenrePresetKey, string[]>;
  default_genre_preset?: DiscoverGenrePresetKey;
  picker_options?: { studios?: string[]; networks?: string[] };
  default_studio_values?: string[];
  default_network_values?: string[];
  default_movie_release_basis?: string;
};

export function getDiscoverPresetPreview(): Promise<DiscoverPresetPreview> {
  return fetchJson("/api/collections/presets/discover");
}

export function previewDiscoverShelf(payload: {
  shelf_id: string;
  section_id: number;
  set_values?: Record<string, string[]> | null;
  shelf_layout?: DiscoverShelfLayout | null;
  auto_add?: Record<string, unknown> | null;
}): Promise<CollectionPreviewResponse> {
  return postJson("/api/collections/presets/discover/shelf-preview", payload);
}

export function applyDiscoverPreset(payload: {
  movie_section_id?: number | null;
  show_section_id?: number | null;
  shelf_ids: string[];
  set_values?: Record<string, string[]>;
  apply_row_order?: boolean;
  auto_add?: Record<string, unknown> | { movie?: Record<string, unknown> | null; show?: Record<string, unknown> | null } | null;
  shelf_layout?: Record<string, DiscoverShelfLayout>;
}): Promise<{ ok: boolean; recipe_ids?: { movie: number[]; show: number[] }; hub?: Record<string, unknown> }> {
  return postJson("/api/collections/presets/discover/apply", payload);
}

export function applyCollectionHubOrder(payload: {
  plex_section_id: number;
  recipe_ids?: number[];
}): Promise<{ ok: boolean; moved?: number; error?: string }> {
  return postJson("/api/collections/hub-order/apply", payload);
}

export type CollectionHubRow = {
  identifier: string;
  title: string;
  rating_key?: string | null;
  promoted_to_recommended: boolean;
  promoted_to_home: boolean;
  promoted_to_shared: boolean;
  deletable: boolean;
  placeholdarr_owned?: boolean;
  recipe_id?: number | null;
  recipe_name?: string | null;
};

export type CollectionHubPromoteCandidate = {
  rating_key: string;
  title: string;
  item_count?: number;
  on_hubs?: boolean;
  placeholdarr_owned?: boolean;
  recipe_id?: number | null;
  recipe_name?: string | null;
};

export type CollectionHubsSnapshot = {
  plex_section_id: number;
  hubs: CollectionHubRow[];
  collections?: CollectionHubPromoteCandidate[];
  promote_candidates: CollectionHubPromoteCandidate[];
};

export function getCollectionHubs(plexSectionId: number): Promise<CollectionHubsSnapshot> {
  const params = new URLSearchParams({ plex_section_id: String(plexSectionId) });
  return fetchJson(`/api/collections/hubs?${params.toString()}`);
}

export function applyCollectionHubs(payload: {
  plex_section_id: number;
  ordered_identifiers?: string[] | null;
  visibility?: {
    identifier?: string;
    rating_key?: string;
    library?: boolean | null;
    home?: boolean | null;
    shared?: boolean | null;
  }[];
  promote_rating_keys?: string[];
  demote_identifiers?: string[];
}): Promise<{
  ok: boolean;
  hubs?: CollectionHubRow[];
  promote_candidates?: CollectionHubPromoteCandidate[];
  errors?: string[];
  reorder?: { ok?: boolean; moved?: number; error?: string };
}> {
  return putJson("/api/collections/hubs", payload);
}

export function getDiscoverPrunePreview(): Promise<{ ok: boolean; count: number; grace_days?: number; enabled?: boolean }> {
  return fetchJson("/api/collections/discover/prune-preview");
}

export function arrAddItemKey(
  instanceKey: string,
  item: { title?: string; year?: number | null; tmdb_id?: number | null; tvdb_id?: number | null; imdb_id?: string | null },
): string {
  return `${instanceKey}:${item.tmdb_id ?? ""}:${item.tvdb_id ?? ""}:${item.imdb_id ?? ""}:${item.title ?? ""}:${item.year ?? ""}`;
}

export type ArrAddStreamEvent = {
  type: string;
  item_key?: string;
  title?: string;
  instance_key?: string;
  status?: "adding" | "ok" | "skipped" | "error";
  error?: string | null;
  message?: string;
  ok?: boolean;
  added?: number;
  skipped?: number;
  errors?: number;
  warnings?: string[];
};

export function addCollectionTitlesToArr(
  payload: {
    media_type: "movie" | "show";
    items: CollectionArrAddItem[];
    instance_keys: string[];
    instance_options: Record<string, { quality_profile_id: number; root_folder_path: string }>;
    monitored: boolean;
    search: boolean;
    tags: string[];
  },
  onEvent: (event: ArrAddStreamEvent) => void,
): Promise<void> {
  return postNdjson("/api/collections/arr-add", payload, (raw) => {
    onEvent(raw as ArrAddStreamEvent);
  });
}

export type CollectionSourceValidation = {
  ok: boolean;
  source_type: string;
  kind?: string | null;
  title?: string | null;
  detail?: string | null;
  suggested_title?: string | null;
  error?: string | null;
};

export function validateCollectionSource(payload: {
  source_type: string;
  media_type: "movie" | "show";
  reference: string;
  subtype?: string | null;
}): Promise<CollectionSourceValidation> {
  return postJson("/api/collections/validate-source", payload);
}

export type CollectionExportBundle = {
  format: string;
  version: number;
  exported_at: string;
  app_version: string;
  recipes: Record<string, unknown>[];
};

export function exportCollectionRecipes(ids: number[]): Promise<CollectionExportBundle> {
  return postJson("/api/collections/export", { ids });
}

export type CollectionImportResult = {
  ok: boolean;
  created: CollectionRecipe[];
  errors: { name: string; error: string }[];
  created_count: number;
};

export function importCollectionRecipes(payload: {
  payload: unknown;
  plex_section_ids?: number[] | null;
}): Promise<CollectionImportResult> {
  return postJson("/api/collections/import", payload);
}
