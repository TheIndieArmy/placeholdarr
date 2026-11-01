import { fetchJson } from "./client";

export type WatchlistStatus = "new" | "added" | "excluded" | "failed" | "all";

export interface WatchlistCounts {
  new: number;
  added: number;
  excluded: number;
  failed: number;
  total: number;
  in_arr?: number;
}

export interface WatchlistCoverageCounts {
  none: number;
  partial: number;
  complete: number;
}

export interface WatchlistArrTarget {
  instance_key: string;
  quality_profile_id?: number | null;
  root_folder_path?: string | null;
}

export interface WatchlistLibraryInstance {
  instance_key: string;
  item_id: number;
  item_type: "movie" | "series" | string;
  is_watchlist_target: boolean;
  has_file: boolean;
  has_placeholder: boolean;
  has_missing?: boolean;
  monitored?: boolean;
  state?: string;
  stats?: { files?: number; placeholders?: number; missing?: number; future?: number; total?: number };
}

export interface WatchlistLibrary {
  state: "not_in_library" | "missing" | "placeholder" | "file" | string;
  coverage: "none" | "partial" | "complete" | string;
  target_keys: string[];
  matched_target_keys: string[];
  missing_target_keys: string[];
  other_instance_keys: string[];
  already_on_keys: string[];
  will_add_keys: string[];
  instances: WatchlistLibraryInstance[];
  item_id: number | null;
  item_type: "movie" | "series" | string;
  display_status?: string | null;
  poster_url?: string | null;
  stats?: { files?: number; placeholders?: number; missing?: number } | null;
}

export interface WatchlistUser {
  plex_user_id: string;
  plex_uuid?: string | null;
  username?: string | null;
  title: string;
  thumb_url?: string | null;
  /** owner | friend | home | unknown (fetch channel) */
  kind?: string | null;
  /** owner | friend | friend_only | server_only | home | unknown */
  relationship?: string | null;
  /** discover | graphql | home_switch | none */
  fetch_method?: string | null;
  /** needs_friend_or_token | needs_pin_or_token | home_switch_disabled | friends_sync_disabled | friend_only_disabled | sync_disabled */
  block_reason?: string | null;
  /** True when this identity appears on the Plex Home quick-switch list. */
  on_plex_home?: boolean;
  /** pin | managed | unlocked when on Home (mainly for relationship=home). */
  home_profile?: string | null;
  is_admin?: boolean;
  is_restricted?: boolean;
  is_protected?: boolean;
  is_guest?: boolean;
  /** Stored preference (may be on while Settings policy blocks fetch). */
  sync_enabled?: boolean;
  /** True only when this person will actually be fetched on the next sync. */
  sync_effective?: boolean;
  /** Per-user Arr behavior override; null/undefined means Settings default. */
  on_new?: string | null;
  settings_override?: Record<string, unknown> | null;
  last_synced_at?: string | null;
  last_sync_error?: string | null;
}

export interface WatchlistItemUser {
  plex_user_id: string;
  title: string;
  username?: string | null;
  thumb_url?: string | null;
  is_admin?: boolean;
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
  user_count?: number;
  counts: WatchlistCounts;
  coverage_counts?: WatchlistCoverageCounts;
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
  thumb?: string | null;
  users?: WatchlistItemUser[];
  library?: WatchlistLibrary | null;
}

export function getWatchlist(opts?: { status?: string; limit?: number; userIds?: string[] }) {
  const params = new URLSearchParams();
  if (opts?.status) params.set("status", opts.status);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.userIds?.length) params.set("user_ids", opts.userIds.join(","));
  const qs = params.toString();
  return fetchJson<{ summary: WatchlistSummary; items: WatchlistItem[]; users: WatchlistUser[] }>(
    `/api/watchlist${qs ? `?${qs}` : ""}`,
  );
}

export function getWatchlistUsers() {
  return fetchJson<{ users: WatchlistUser[] }>("/api/watchlist/users");
}

export function patchWatchlistUser(
  plexUserId: string,
  body: { sync_enabled?: boolean; on_new?: string | null },
) {
  return fetchJson<{ ok: boolean; user: WatchlistUser }>(
    `/api/watchlist/users/${encodeURIComponent(plexUserId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

export function postWatchlistRosterRefresh() {
  return fetchJson<{ ok: boolean; users: WatchlistUser[]; created?: number; updated?: number }>(
    "/api/watchlist/roster/refresh",
    { method: "POST" },
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
