import { fetchJson, postJson } from "./client";
import type {
  ActivityRow,
  CalendarErrorResponse,
  CalendarResponse,
  DetailResponse,
  ErrorRow,
  IntegrationTestResponse,
  LibraryResponse,
  LibraryVersionResponse,
  DashboardEvent,
  HealthResponse,
  LogsResponse,
  PlaceholderActivityRow,
  ReadyResponse,
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

export function getActivityOperations(limit = 100): Promise<ActivityRow[]> {
  return fetchJson<ActivityRow[]>(`/api/activity/operations?limit=${limit}`);
}

export function getPlaceholderActivity(limit = 100): Promise<PlaceholderActivityRow[]> {
  return fetchJson<PlaceholderActivityRow[]>(`/api/activity/placeholders?limit=${limit}`);
}

export type LibraryFetchResult =
  | { notModified: true; version: number | null }
  | { notModified: false; payload: LibraryResponse };

export function getLibraryVersion(): Promise<LibraryVersionResponse> {
  return fetchJson<LibraryVersionResponse>("/api/library/version");
}

export async function getLibrary(
  opts?: {
    summary?: boolean;
    mediaType?: "movie" | "series";
    ifNoneMatch?: number | string | null;
  },
): Promise<LibraryFetchResult> {
  const summary = opts?.summary === true;
  const q = new URLSearchParams();
  if (summary) q.set("summary", "true");
  if (opts?.mediaType === "movie") q.set("media_type", "movie");
  if (opts?.mediaType === "series") q.set("media_type", "series");

  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts?.ifNoneMatch != null && opts.ifNoneMatch !== "") {
    headers["If-None-Match"] = `"${opts.ifNoneMatch}"`;
  }

  let response: Response;
  try {
    response = await fetch(`/api/library?${q.toString()}`, {
      credentials: "same-origin",
      headers,
    });
  } catch (err) {
    if (err instanceof TypeError) {
      throw new Error("Cannot reach the Placeholdarr API (network error). Trying to reconnect…");
    }
    throw err instanceof Error ? err : new Error(String(err));
  }

  if (response.status === 401) {
    throw new Error("authentication required");
  }

  if (response.status === 304) {
    const etag = response.headers.get("ETag")?.replace(/"/g, "") ?? null;
    return {
      notModified: true,
      version: etag != null && /^\d+$/.test(etag) ? Number(etag) : null,
    };
  }

  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try {
      const payload = (await response.json()) as { message?: string; detail?: string };
      message = payload.message || payload.detail || message;
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }

  const payload = (await response.json()) as LibraryResponse;
  return { notModified: false, payload };
}

export function getMovieDetail(movieId: number): Promise<DetailResponse> {
  return fetchJson<DetailResponse>(`/api/detail/movie/${movieId}`);
}

export function getSeriesDetail(seriesId: number): Promise<DetailResponse> {
  return fetchJson<DetailResponse>(`/api/detail/series/${seriesId}`);
}

export type EntityReconcileStartResponse = {
  ok: boolean;
  job_id?: number;
  step_label?: string;
  reused?: boolean;
  message?: string;
};

export type EntityReconcileStatusResponse = {
  ok: boolean;
  status: "working" | "done" | "failed";
  step?: string;
  step_label?: string;
  error_message?: string | null;
  entity_type?: string;
  entity_id?: number;
};

export function refreshMoviePlaceholder(movieId: number): Promise<EntityReconcileStartResponse> {
  return fetchJson<EntityReconcileStartResponse>(`/api/library/movie/${movieId}/refresh-placeholder`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
  });
}

export function refreshSeriesPlaceholder(seriesId: number): Promise<EntityReconcileStartResponse> {
  return fetchJson<EntityReconcileStartResponse>(`/api/library/series/${seriesId}/refresh-placeholder`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
  });
}

export function refreshEpisodePlaceholder(episodeId: number): Promise<EntityReconcileStartResponse> {
  return fetchJson<EntityReconcileStartResponse>(`/api/library/episode/${episodeId}/refresh-placeholder`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
  });
}

export function getEntityReconcileStatus(jobId: number): Promise<EntityReconcileStatusResponse> {
  return fetchJson<EntityReconcileStatusResponse>(`/api/library/reconcile-jobs/${jobId}`);
}

export function getCalendar(month: string): Promise<CalendarResponse | CalendarErrorResponse> {
  return fetchJson<CalendarResponse | CalendarErrorResponse>(`/api/calendar?month=${encodeURIComponent(month)}`);
}

export function getErrors(limit = 100): Promise<ErrorRow[]> {
  return fetchJson<ErrorRow[]>(`/api/errors?limit=${limit}`);
}

export function getLogs(
  level: "all" | "debug" | "info" | "warn" | "error" | "critical",
  tail = 500,
  sinceId?: number,
): Promise<LogsResponse> {
  const params = new URLSearchParams({
    tail: String(tail),
    level,
  });
  if (sinceId != null) {
    params.set("since_id", String(sinceId));
  }
  return fetchJson<LogsResponse>(`/api/logs?${params.toString()}`);
}

export function openLogsEventSource(
  level: "all" | "debug" | "info" | "warn" | "error" | "critical",
  sinceId: number,
  handlers: {
    onLine: (id: number, line: string) => void;
    onError?: () => void;
  },
): () => void {
  const params = new URLSearchParams({
    since_id: String(Math.max(0, sinceId)),
    level,
  });
  const source = new EventSource(`/api/logs/stream?${params.toString()}`);

  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data) as { id?: number; line?: string };
      if (typeof payload.id === "number" && typeof payload.line === "string") {
        handlers.onLine(payload.id, payload.line);
      }
    } catch {
      /* ignore malformed frames */
    }
  };

  source.onerror = () => {
    handlers.onError?.();
    source.close();
  };

  return () => source.close();
}

export function getHealth(): Promise<HealthResponse> {
  return fetchJson<HealthResponse>("/api/health");
}

export function getReady(): Promise<ReadyResponse> {
  return fetchJson<ReadyResponse>("/api/ready");
}

export function openDashboardEventSource(handlers: {
  onOpen?: () => void;
  onEvent: (event: DashboardEvent) => void;
  onError?: () => void;
}): () => void {
  const source = new EventSource("/api/events");

  source.onopen = () => {
    handlers.onOpen?.();
  };

  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data) as DashboardEvent;
      if (payload && typeof payload.type === "string") {
        handlers.onEvent(payload);
      }
    } catch {
      /* ignore malformed frames */
    }
  };

  source.onerror = () => {
    handlers.onError?.();
    source.close();
  };

  return () => source.close();
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
  return postJson<SaveSettingsResponse>("/api/settings/save", body);
}

export async function testIntegrationConnection(input: {
  service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr";
  url: string;
  credential: string;
}): Promise<IntegrationTestResponse> {
  return postJson<IntegrationTestResponse>("/api/integrations/test", input);
}
