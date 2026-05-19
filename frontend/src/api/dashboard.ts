import { fetchJson } from "./client";
import type {
  ActivityRow,
  CalendarErrorResponse,
  CalendarResponse,
  DetailResponse,
  ErrorRow,
  IntegrationTestResponse,
  LibraryResponse,
  LogsResponse,
  PlaceholderActivityRow,
  SaveSettingsResponse,
  SettingsPayload,
  SettingsStatus,
  StatsResponse,
} from "../types/api";

export function getStats(): Promise<StatsResponse> {
  return fetchJson<StatsResponse>("/api/stats");
}

export function getActivity(limit = 100): Promise<ActivityRow[]> {
  return fetchJson<ActivityRow[]>(`/api/activity?limit=${limit}`);
}

export function getPlaceholderActivity(limit = 100): Promise<PlaceholderActivityRow[]> {
  return fetchJson<PlaceholderActivityRow[]>(`/api/activity/placeholders?limit=${limit}`);
}

export function getLibrary(limit = 1000, opts?: { summary?: boolean }): Promise<LibraryResponse> {
  const summary = opts?.summary === true;
  const q = new URLSearchParams({ limit: String(limit) });
  if (summary) q.set("summary", "true");
  return fetchJson<LibraryResponse>(`/api/library?${q.toString()}`);
}

export function getMovieDetail(movieId: number): Promise<DetailResponse> {
  return fetchJson<DetailResponse>(`/api/detail/movie/${movieId}`);
}

export function getSeriesDetail(seriesId: number): Promise<DetailResponse> {
  return fetchJson<DetailResponse>(`/api/detail/series/${seriesId}`);
}

export function getCalendar(month: string): Promise<CalendarResponse | CalendarErrorResponse> {
  return fetchJson<CalendarResponse | CalendarErrorResponse>(`/api/calendar?month=${encodeURIComponent(month)}`);
}

export function getErrors(limit = 100): Promise<ErrorRow[]> {
  return fetchJson<ErrorRow[]>(`/api/errors?limit=${limit}`);
}

export function getLogs(level: "all" | "debug" | "info" | "warn" | "error" | "critical", tail = 500): Promise<LogsResponse> {
  return fetchJson<LogsResponse>(`/api/logs?tail=${tail}&level=${level}`);
}

export function getSettingsCurrent(): Promise<SettingsPayload> {
  return fetchJson<SettingsPayload>("/api/settings/current");
}

export function getSettingsStatus(): Promise<SettingsStatus> {
  return fetchJson<SettingsStatus>("/api/settings/status");
}

export type NfoBackfillApplyScope = "now" | "next_full_sync" | "future";

export async function saveSettings(
  values: Record<string, unknown>,
  partial = false,
  context?: { source?: string; stepKey?: string; stepName?: string },
  applyScope?: NfoBackfillApplyScope,
): Promise<SaveSettingsResponse> {
  const body: Record<string, unknown> = { values, partial, context };
  if (applyScope) {
    body.apply_scope = applyScope;
  }
  const response = await fetch("/api/settings/save", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  return (await response.json()) as SaveSettingsResponse;
}

export async function testIntegrationConnection(input: {
  service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr";
  url: string;
  credential: string;
}): Promise<IntegrationTestResponse> {
  const response = await fetch("/api/integrations/test", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(input),
  });
  return (await response.json()) as IntegrationTestResponse;
}
