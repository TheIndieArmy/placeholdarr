import { fetchJson, postJson } from "./client";

export type CatalogSource = {
  id: number;
  name: string;
  source_type: string;
  media_type: string;
  filters_json: Record<string, unknown>;
  enabled: boolean;
  run_interval_hours: number | null;
  last_run_at: string | null;
  last_run_stats: Record<string, unknown>;
};

export async function fetchDiscoverStatus() {
  return fetchJson<{
    catalog_mode_discover: boolean;
    tmdb_configured: boolean;
    movie_count: number;
    source_count: number;
    needs_placeholder: number;
  }>("/api/discover/status");
}

export async function verifyDiscoverTmdb() {
  return postJson<{ ok: boolean }>("/api/discover/verify-tmdb");
}

export async function listDiscoverSources() {
  return fetchJson<{ sources: CatalogSource[] }>("/api/discover/sources");
}

export async function createDiscoverSource(body: {
  name: string;
  source_type: string;
  media_type?: string;
  filters_json?: Record<string, unknown>;
  enabled?: boolean;
  run_interval_hours?: number | null;
}) {
  return postJson<{ source: CatalogSource }>("/api/discover/sources", body);
}

export async function patchDiscoverSource(
  sourceId: number,
  body: {
    name?: string;
    source_type?: string;
    filters_json?: Record<string, unknown>;
    enabled?: boolean;
    run_interval_hours?: number | null;
  },
) {
  return fetchJson<{ source: CatalogSource }>(`/api/discover/sources/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteDiscoverSource(sourceId: number) {
  return fetchJson<{ ok: boolean }>(`/api/discover/sources/${sourceId}`, { method: "DELETE" });
}

export async function runDiscoverPipeline(ensureDefault = true) {
  return postJson<{ ok?: boolean; started?: boolean; message?: string }>(
    `/api/discover/run?ensure_default=${ensureDefault ? "true" : "false"}`,
  );
}

export async function runDiscoverSource(sourceId: number) {
  return postJson<{ ok?: boolean; started?: boolean; message?: string; source_id?: number }>(
    `/api/discover/sources/${sourceId}/run`,
  );
}
