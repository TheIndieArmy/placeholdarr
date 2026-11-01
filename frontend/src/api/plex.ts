import { fetchJson } from "./client";

export type PlexOauthServer = {
  machine_id: string;
  name: string;
  owned?: boolean;
  product?: string | null;
  platform?: string | null;
  presence?: boolean;
  uri?: string | null;
  connection_count?: number;
};

export type PlexOauthStatus = {
  ok?: boolean;
  connected: boolean;
  auth_method?: string | null;
  username?: string | null;
  server_name?: string | null;
  machine_id?: string | null;
  url?: string | null;
  enabled?: boolean;
};

export function postPlexOauthPin() {
  return fetchJson<{
    ok: boolean;
    id: number;
    code: string;
    client_id: string;
    auth_url: string;
    expires_in?: number;
  }>("/api/plex/oauth/pin", { method: "POST" });
}

export function postPlexOauthPoll(body: { pin_id: number; client_id?: string }) {
  return fetchJson<{
    ok: boolean;
    authorized: boolean;
    auth_token?: string;
    username?: string | null;
    client_id?: string | null;
    servers?: PlexOauthServer[];
  }>("/api/plex/oauth/poll", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function postPlexOauthServers(body?: { auth_token?: string; client_id?: string }) {
  return fetchJson<{ ok: boolean; servers: PlexOauthServer[] }>("/api/plex/oauth/servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

export function postPlexOauthComplete(body: {
  auth_token?: string;
  client_id?: string;
  machine_id: string;
  username?: string | null;
}) {
  return fetchJson<{
    ok: boolean;
    username?: string | null;
    server_name?: string | null;
    machine_id?: string | null;
    url?: string | null;
    watchlist?: { ok: boolean; error?: string; sample_count?: number };
    status?: PlexOauthStatus;
  }>("/api/plex/oauth/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getPlexOauthStatus() {
  return fetchJson<PlexOauthStatus>("/api/plex/oauth/status");
}
