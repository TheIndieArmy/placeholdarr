import { fetchJson } from "./client";

export type WatchlistStatus = "new" | "in_arr" | "added" | "excluded" | "failed" | "all";

export interface WatchlistCounts {
  new: number;
  in_arr: number;
  added: number;
  excluded: number;
  failed: number;
  total: number;
}

export interface WatchlistArrTarget {
  instance_key: string;
  quality_profile_id?: number | null;
  root_folder_path?: string | null;
}

export interface WatchlistSummary {
  enabled: boolean;
  has_token: boolean;
  on_new: string;
  tv_request_scope: string;
  arr_tags?: string;
  radarr_targets?: WatchlistArrTarget[];
  sonarr_targets?: WatchlistArrTarget[];
  last_poll_at: string | null;
  last_error: string | null;
  counts: WatchlistCounts;
}

export interface WatchlistItem {
  id: number;
  identity_key: string;
  plex_guid: string | null;
  rating_key: string | null;
  title: string;
  year: number | null;
  media_type: "movie" | "show" | string;
  tmdb_id: number | null;
  tvdb_id: number | null;
  imdb_id: string | null;
  status: string;
  excluded: boolean;
  last_error: string | null;
  last_seen_at: string | null;
  first_seen_at: string | null;
  added_at: string | null;
  arr_instance_key: string | null;
}

export function getWatchlist(opts?: { status?: string; limit?: number }) {
  const params = new URLSearchParams();
  if (opts?.status) params.set("status", opts.status);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return fetchJson<{ summary: WatchlistSummary; items: WatchlistItem[] }>(
    `/api/watchlist${qs ? `?${qs}` : ""}`,
  );
}

export function postWatchlistSync() {
  return fetchJson<{ ok: boolean; message: string }>("/api/watchlist/sync", {
    method: "POST",
  });
}

export function postWatchlistAdd(body: {
  item_ids: number[];
  search?: boolean;
  monitored?: boolean;
  tags?: string[];
  tv_scope?: string;
  targets?: WatchlistArrTarget[];
  instance_key?: string;
  quality_profile_id?: number;
  root_folder_path?: string;
}) {
  return fetchJson<{ ok: boolean; skipped?: number; failed?: number; added?: number }>(
    "/api/watchlist/add",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

export function postWatchlistExclude(itemIds: number[], excluded: boolean) {
  return fetchJson<{ ok: boolean; updated: number; excluded: boolean }>("/api/watchlist/exclude", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_ids: itemIds, excluded }),
  });
}

