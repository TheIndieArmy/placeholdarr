export type DashboardTab = "activity" | "library" | "calendar" | "errors" | "logs" | "settings" | "setup";

export interface StatsResponse {
  movies: {
    total: number;
    placeholders: number;
    downloaded: number;
    future_outside_lookahead: number;
  };
  series: {
    total: number;
  };
  episodes: {
    total: number;
    placeholders: number;
    downloaded: number;
    future_outside_lookahead: number;
  };
  placeholders_on_disk: number;
  jobs: {
    pending: number;
    failed: number;
    done: number;
  };
  last_sync: string | null;
}

export interface ActivityRow {
  id?: string | number;
  type: "job" | "event";
  source?: string | null;
  event_type?: string | null;
  job_type?: string | null;
  display_name?: string | null;
  status?: string | null;
  error?: string | null;
  details?: string | null;
  time?: string | null;
  progress?: {
    running?: boolean;
    sections?: Array<{
      name: string;
      status: "pending" | "working" | "done" | "failed" | "skipped" | string;
      metrics?: Array<{ label: string; value: string | number | null | undefined }>;
    }>;
    log_file?: string;
    /** Batched queue-monitor titles (Radarr/Sonarr download emulation). */
    queue_items?: Array<{
      kind?: string;
      title?: string;
      subtitle?: string;
      instance?: string;
      line?: string;
      arr_percent?: number | null;
    }>;
    /** Grouped event rows (collapsed summary expands to per-event lines). */
    grouped_events?: Array<{
      id?: number | string;
      display_name?: string;
      status?: string;
      source?: string | null;
      details?: string | null;
      error?: string | null;
      time?: string | null;
    }>;
  };
}

export interface PlaceholderActivityRow {
  id: number;
  type: "placeholder";
  action: "Created" | "Deleted" | "Status";
  item_type: "movie" | "episode" | "batch";
  item_title: string;
  series_title?: string | null;
  path: string;
  reason: string;
  status: string;
  time?: string | null;
  /** Server-side batch of calendar-driven status updates; expands in the UI. */
  group_kind?: "calendar_status_sync" | string;
  children?: PlaceholderActivityChildRow[];
}

export interface PlaceholderActivityChildRow {
  id: number;
  type?: "placeholder";
  action?: string;
  item_type?: "movie" | "episode";
  item_title?: string;
  series_title?: string | null;
  path?: string;
  reason?: string;
  status?: string;
  time?: string | null;
}

export type LibraryItemType = "movie" | "series";

export interface LibraryItemStats {
  downloaded?: number;
  placeholders?: number;
  future?: number;
  missing?: number;
  episode_total?: number;
  episode_files?: number;
  episode_placeholders?: number;
  episode_future?: number;
  episode_missing?: number;
}

export interface LibraryItem {
  id: string;
  item_id: number;
  type: LibraryItemType;
  title: string;
  year: number;
  poster_url?: string | null;
  backdrop_url?: string | null;
  is_4k: boolean;
  instance_key?: string | null;
  instance_id?: string | null;
  instance_label?: string | null;
  arr_link?: string | null;
  determination?: string | null;
  status?: string | null;
  has_file: boolean;
  has_placeholder: boolean;
  is_future: boolean;
  has_missing: boolean;
  overview?: string | null;
  stats: LibraryItemStats;
}

export interface LibraryResponse {
  items: LibraryItem[];
  count: number;
}

/** Deep link to an ARR instance (Radarr / Sonarr) from detail views. */
export interface ArrInstanceOpenLink {
  label: string;
  url: string;
  movie_id?: number;
  series_id?: number;
  has_file?: boolean;
  has_placeholder?: boolean;
  /** When false, this ARR instance does not have this title/show; UI shows "-" for the status line. */
  present?: boolean;
  episode_files?: number;
  episode_placeholders?: number;
  /** Episodes tracked for this show on this Sonarr instance (for ``files/total`` on series detail). */
  episode_total?: number;
}

export interface MovieDetailResponse {
  ok: true;
  type: "movie";
  id: number;
  title: string;
  year: number;
  overview?: string | null;
  poster_url?: string | null;
  backdrop_url?: string | null;
  runtime?: number | null;
  certification?: string | null;
  genres?: string[] | null;
  studio?: string | null;
  ratings?: Record<string, unknown> | null;
  collection?: Record<string, unknown> | null;
  is_4k: boolean;
  instance_key?: string | null;
  instance_id?: string | null;
  instance_label?: string | null;
  arr_link?: string | null;
  /** One entry per configured instance that has this title (same TMDB id). */
  arr_instance_links?: ArrInstanceOpenLink[];
  imdbid?: string | null;
  tmdbid?: number | null;
  status?: string | null;
  determination?: string | null;
  has_file: boolean;
  has_placeholder: boolean;
  placeholder_filepath?: string | null;
  radarr_quality?: string | null;
  radarr_monitored?: boolean;
  radarr_release_status?: string | null;
  theater_release_date?: string | null;
  digital_release_date?: string | null;
  physical_release_date?: string | null;
  last_search?: string | null;
  updated_at?: string | null;
  created_at?: string | null;
}

export interface SeriesEpisodeDetail {
  id: number;
  episode_number: number;
  title: string;
  air_date?: string | null;
  overview?: string | null;
  still_url?: string | null;
  has_file: boolean;
  has_placeholder: boolean;
  determination?: string | null;
  status?: string | null;
  sonarr_quality?: string | null;
  sonarr_monitored?: boolean;
  placeholder_filepath?: string | null;
}

export interface SeriesSeasonDetail {
  id: number;
  season_number: number;
  title?: string | null;
  overview?: string | null;
  has_files: boolean;
  episode_total: number;
  episode_files: number;
  episode_placeholders: number;
  episodes: SeriesEpisodeDetail[];
}

export interface SeriesDetailResponse {
  ok: true;
  type: "series";
  id: number;
  title: string;
  year: number;
  overview?: string | null;
  poster_url?: string | null;
  backdrop_url?: string | null;
  runtime?: number | null;
  certification?: string | null;
  genres?: string[] | null;
  network?: string | null;
  ratings?: Record<string, unknown> | null;
  is_4k: boolean;
  instance_key?: string | null;
  instance_id?: string | null;
  instance_label?: string | null;
  arr_link?: string | null;
  /** One entry per configured instance that has this show (same TVDB id). */
  arr_instance_links?: ArrInstanceOpenLink[];
  imdbid?: string | null;
  tvdbid?: number | null;
  status?: string | null;
  sonarr_status?: string | null;
  sonarr_monitored?: boolean;
  first_aired?: string | null;
  updated_at?: string | null;
  created_at?: string | null;
  seasons: SeriesSeasonDetail[];
}

export interface DetailErrorResponse {
  ok: false;
  message: string;
}

export type DetailResponse = MovieDetailResponse | SeriesDetailResponse | DetailErrorResponse;

export interface ErrorRow {
  source: string;
  label: string;
  error: string;
  time?: string | null;
}

export interface LogsResponse {
  lines: string[];
  file?: string | null;
}

export interface CalendarLegendItem {
  key: string;
  label: string;
  icon?: string;
}

export interface CalendarItem {
  id: string;
  item_id: number;
  series_id?: number;
  season_number?: number | null;
  episode_number?: number | null;
  /** Same calendar day + series; spotlight uses first `item_id`. */
  group_episode_ids?: number[];
  group_episode_count?: number;
  media_type: "movie" | "episode";
  title: string;
  subtitle?: string;
  release_date: string;
  in_lookahead_window: boolean;
  days_until?: number | null;
  status?: string | null;
  reason?: string | null;
  has_file: boolean;
  has_placeholder: boolean;
  is_4k: boolean;
  instance_key?: string | null;
  arr_link?: string | null;
  release_type?: "inCinemas" | "digitalRelease" | "physicalRelease";
  release_type_label?: string;
  release_type_preferred?: boolean;
}

export interface CalendarDay {
  iso_date: string;
  day_number: number;
  is_current_month: boolean;
  is_today: boolean;
  in_lookahead_window: boolean;
  item_count: number;
  items: CalendarItem[];
}

export interface CalendarResponse {
  ok: true;
  month: string;
  month_label: string;
  today_month: string;
  previous_month: string;
  next_month: string;
  weekday_labels: string[];
  lookahead: {
    days: number;
    mode: string;
    start_date: string;
    end_date?: string | null;
    label: string;
  };
  legend: {
    movie_release_types: CalendarLegendItem[];
    media_types: CalendarLegendItem[];
  };
  summary: {
    movie_count: number;
    episode_count: number;
    total_count: number;
    in_window_count: number;
  };
  weeks: CalendarDay[][];
}

export interface CalendarErrorResponse {
  ok: false;
  message: string;
}

export interface SettingsFieldOption {
  value: string;
  label: string;
}

export interface SettingsField {
  key: string;
  section: string;
  label: string;
  description: string;
  type: "bool" | "int" | "url" | "path" | "string" | "choice";
  required: boolean;
  secret: boolean;
  restart_required: boolean;
  value: unknown;
  saved_value?: unknown;
  has_saved_value?: boolean;
  options?: SettingsFieldOption[];
}

export interface SettingsSection {
  name: string;
  fields: SettingsField[];
}

export interface SettingsStatus {
  setup_complete: boolean;
  setup_completed_at?: string | null;
  configured_settings: number;
  available_settings: number;
}

export interface SettingsPayload {
  status: SettingsStatus;
  sections: SettingsSection[];
}

export interface SaveSettingsResponse {
  ok: boolean;
  errors?: Record<string, string>;
  saved_keys?: string[];
  restart_required_keys?: string[];
  status?: SettingsStatus;
}

export interface IntegrationTestResponse {
  ok: boolean;
  message: string;
}
