import { fetchJson, postJson } from "./client";
import type {
  CollectionActiveWindow,
  CollectionArrAddItem,
  CollectionArrAddOptionsResponse,
  CollectionArrAddResponse,
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

export interface RecipeWritePayload {
  name: string;
  enabled: boolean;
  plex_section_id: number;
  plex_section_ids?: number[];
  plex_section_type: "movie" | "show";
  collection_title: string;
  definition: CollectionDefinition;
  run_interval_hours: number | null;
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

export async function deleteCollectionRecipe(id: number): Promise<{ ok: boolean }> {
  return fetchJson(`/api/collections/${id}`, { method: "DELETE" });
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

export function getCollectionPlexSections(): Promise<{ sections: PlexSectionOption[] }> {
  return fetchJson("/api/collections/plex-sections");
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

export function addCollectionTitlesToArr(payload: {
  media_type: "movie" | "show";
  items: CollectionArrAddItem[];
  instance_keys: string[];
  instance_options: Record<string, { quality_profile_id: number; root_folder_path: string }>;
  monitored: boolean;
  search: boolean;
  tag: string;
}): Promise<CollectionArrAddResponse> {
  return postJson("/api/collections/arr-add", payload);
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
