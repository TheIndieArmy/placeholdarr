import { fetchJson, postJson, ApiUnauthorizedError } from "./client";
import { reloadIfFrontendStale } from "../frontendBuild";
import type {
  ActivityRow,
  CalendarErrorResponse,
  CalendarResponse,
  DetailResponse,
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
  return getActivityOperationsPage({ limit }).then((p) => p.items);
}

export type ActivityFeedPage<T> = {
  items: T[];
  has_more: boolean;
  next_before_time?: string | null;
  next_before_id?: number | null;
};

export function getActivityOperationsPage(opts?: {
  limit?: number;
  beforeTime?: string | null;
  beforeId?: number | null;
}): Promise<ActivityFeedPage<ActivityRow>> {
  const limit = opts?.limit ?? 100;
  const params = new URLSearchParams({ limit: String(limit) });
  if (opts?.beforeTime) params.set("before_time", opts.beforeTime);
  if (opts?.beforeId != null) params.set("before_id", String(opts.beforeId));
  return fetchJson<ActivityFeedPage<ActivityRow>>(`/api/activity/operations?${params}`);
}

export function getActivityOperations(limit = 100): Promise<ActivityRow[]> {
  return getActivityOperationsPage({ limit }).then((p) => p.items);
}

export function getPlaceholderActivityPage(opts?: {
  limit?: number;
  beforeTime?: string | null;
  beforeId?: number | null;
}): Promise<ActivityFeedPage<PlaceholderActivityRow>> {
  const limit = opts?.limit ?? 100;
  const params = new URLSearchParams({ limit: String(limit) });
  if (opts?.beforeTime) params.set("before_time", opts.beforeTime);
  if (opts?.beforeId != null) params.set("before_id", String(opts.beforeId));
  return fetchJson<ActivityFeedPage<PlaceholderActivityRow>>(`/api/activity/placeholders?${params}`);
}

export function getPlaceholderActivity(limit = 100): Promise<PlaceholderActivityRow[]> {
  return getPlaceholderActivityPage({ limit }).then((p) => p.items);
}

export type ActiveSearchItem = {
  kind?: string;
  title?: string;
  subtitle?: string;
  instance?: string;
  line?: string;
  arr_percent?: number | null;
};

export type ActiveSearchesResponse = {
  active: boolean;
  items: ActiveSearchItem[];
  details: string;
  started_at?: string | null;
  updated_at?: string | null;
};

export function getActiveSearches(): Promise<ActiveSearchesResponse> {
  return fetchJson<ActiveSearchesResponse>("/api/activity/active-searches");
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

export async function getHealth(): Promise<HealthResponse> {
  const health = await fetchJson<HealthResponse>("/api/health");
  reloadIfFrontendStale(health);
  return health;
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
  /** Settings secret key (e.g. PLEX_TOKEN). Used when credential is blank. */
  credential_key?: string;
  /** ARR instance id. Used when credential is blank for radarr/sonarr. */
  instance_id?: string;
}): Promise<IntegrationTestResponse> {
  // The API returns HTTP 400 with `{ ok: false, message }` for failed connection tests
  // and missing fields. Treat that as a normal result so callers can show the message
  // instead of leaving UI stuck on "Testing…".
  try {
    return await postJson<IntegrationTestResponse>("/api/integrations/test", input);
  } catch (err) {
    if (err instanceof ApiUnauthorizedError) {
      throw err;
    }
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, message: message || "Connection test failed" };
  }
}
