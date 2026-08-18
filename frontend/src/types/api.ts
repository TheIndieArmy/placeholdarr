export type DashboardTab = "activity" | "library" | "collections" | "calendar" | "errors" | "logs" | "settings" | "setup";

export type ActivitySubPage = "placeholders" | "tasks" | "operations";

export type TaskKey = "full_sync" | "lite_sync" | "calendar_only" | "placeholder_refresh" | "collections_sync";

export interface ScheduledTaskRow {
  task_key: TaskKey;
  label: string;
  enabled: boolean;
  interval_hours: number;
  interval_label: string;
  next_run: string | null;
  running: boolean;
  last_run: string | null;
  last_duration_seconds: number | null;
  last_status: string | null;
}

export interface TaskRunRow {
  id: number;
  task_key: TaskKey | string;
  task_label: string;
  trigger: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  sync_duration_seconds?: number | null;
  wall_clock_duration_seconds?: number | null;
  art_backfill_pending?: boolean;
  error_message?: string | null;
  skip_reason?: string | null;
  details?: string | null;
  progress?: ActivityRow["progress"];
}

export interface TaskRunStatusResponse {
  working: boolean;
  run: TaskRunRow | null;
}

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
      started_at?: string | null;
      ended_at?: string | null;
      duration_seconds?: number | null;
      metrics?: Array<{ label: string; value: string | number | null | undefined; tooltip?: string }>;
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
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  imdb_id?: string | null;
  /** Series network (Sonarr). */
  network?: string | null;
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
  /** When this title was first indexed in Placeholdarr (ISO 8601). */
  created_at?: string | null;
  /** Last catalog/metadata update for this row (ISO 8601). */
  updated_at?: string | null;
  stats: LibraryItemStats;
}

export interface LibraryResponse {
  items: LibraryItem[];
  count: number;
  total: number;
  version: number | string;
}

export interface LibraryVersionResponse {
  movies_version: number;
  series_version: number;
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
  /** Log files capture full verbosity (VERBOSE/DEBUG and above); console stays INFO-only. */
  capture_level?: string | null;
  /** Monotonic line id for incremental live streaming; 0 when tail came from the log file. */
  latest_id?: number;
  /** `live` when served from the in-process buffer; `file` on cold start fallback. */
  source?: "live" | "file" | string | null;
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
  /** When set, the field is only interactive if the parent setting is enabled (bool). */
  depends_on?: string;
  /** When set, the field is non-interactive while the parent bool setting is enabled. */
  disabled_when?: string;
  /** Indent under the parent setting in the settings UI. */
  nested?: boolean;
}

export interface SettingsSection {
  name: string;
  fields: SettingsField[];
}

export interface HealthResponse {
  ok: boolean;
  app_version?: string;
  frontend_build?: string;
}

export interface ReadyResponse {
  ok: boolean;
  startup_sync_complete: boolean;
}

export type DashboardEvent =
  | { type: "ping"; ts: number }
  | { type: "startup_sync_complete"; value: boolean }
  | { type: "library_version"; movies_version: number; series_version: number }
  | { type: "task_runs_version"; version: string };

export interface SettingsStatus {
  setup_complete: boolean;
  setup_completed_at?: string | null;
  configured_settings: number;
  available_settings: number;
  /** False while the background startup ARR sync is still running (workers may be gated). */
  startup_sync_complete?: boolean;
}

export interface SettingsPayload {
  status: SettingsStatus;
  sections: SettingsSection[];
  /** Not a settings field — used to display webhook URLs Radarr/Sonarr/etc. need. */
  webhook_api_key?: string | null;
}

export interface SaveSettingsResponse {
  ok: boolean;
  errors?: Record<string, string>;
  saved_keys?: string[];
  restart_required_keys?: string[];
  status?: SettingsStatus;
  nfo_backfill_keys_changed?: string[];
  nfo_backfill?: { ok?: boolean; enqueued?: boolean; scope?: string };
  art_backfill_keys_changed?: string[];
  art_backfill?: { ok?: boolean; enqueued?: boolean; scope?: string; pending?: boolean };
}

export interface IntegrationTestResponse {
  ok: boolean;
  message: string;
}

// ---------------------------------------------------------------------------
// Collections (rule-based Plex collection builder)
// ---------------------------------------------------------------------------

export type CollectionSourceType =
  | "catalog"
  | "tmdb_trending"
  | "tmdb_popular"
  | "tmdb_upcoming"
  | "tmdb_discover"
  | "tmdb_list"
  | "tmdb_person"
  | "tmdb_company"
  | "tmdb_keyword"
  | "tmdb_collection"
  | "mdblist"
  | "trakt_list"
  | "stevenlu"
  | "anilist";

export interface CollectionSourceBlock {
  type: CollectionSourceType;
  /** tmdb_trending */
  window?: "day" | "week";
  /** tmdb_discover */
  genre_ids?: number[];
  year_from?: number | null;
  year_to?: number | null;
  provider_ids?: number[];
  watch_region?: string;
  min_vote_average?: number | null;
  /** tmdb_list / tmdb_person / tmdb_company / tmdb_keyword / tmdb_collection — id or TMDB URL */
  list_id?: string;
  tmdb_ref?: string;
  /** mdblist / trakt_list / anilist / stevenlu — pasted URL or user/slug */
  list_ref?: string;
  /** per-source candidate cap */
  limit?: number;
}

export type CollectionFilterField =
  | "genre"
  | "year"
  | "certification"
  | "studio_network"
  | "monitored"
  | "quality_profile"
  | "original_language"
  | "instance"
  | "release_window"
  | "rating";

export type CollectionReleaseWindowBasis =
  | "premiered"
  | "latest_episode"
  | "latest_season"
  | "theater"
  | "digital"
  | "physical";

/** Year filter: premiere/release year vs TV first–last air overlap. */
export type CollectionYearBasis = "premiered" | "aired_during";

/** Radarr nested rating providers for movie rating filters. TV uses Sonarr's flat score. */
export type CollectionRatingProvider =
  | "imdb"
  | "tmdb"
  | "trakt"
  | "metacritic"
  | "rottenTomatoes";

export interface CollectionFilterBlock {
  field: CollectionFilterField;
  op?: string;
  value?: string | number | boolean | null;
  value_to?: number | null;
  values?: string[];
  /** release_window: TV air-date mode or movie release type. year: premiere vs was-airing-during. */
  basis?: CollectionReleaseWindowBasis | CollectionYearBasis | null;
  /** rating only (movies): which Radarr ratings.* key to use. */
  provider?: CollectionRatingProvider | null;
  /** rating only: require at least this many votes on the chosen score. */
  min_votes?: number | null;
}

export type CollectionSortOption = "popularity" | "release_date" | "latest_aired" | "title" | "rating";

/** Boolean filter tree: groups carry and/or and may nest rules or sub-groups (depth ≤ 3). */
export interface CollectionFilterGroup {
  op: "and" | "or";
  children: CollectionFilterNode[];
}

export type CollectionFilterNode = CollectionFilterBlock | CollectionFilterGroup;

/** Legacy recipes store a flat rule list (implicit single AND group). */
export type CollectionFilters = CollectionFilterBlock[] | CollectionFilterGroup;

export interface CollectionPinnedItem {
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  imdb_id?: string | null;
  title: string;
  year?: number | null;
  poster?: string | null;
}

export interface CollectionPins {
  include?: CollectionPinnedItem[];
  exclude?: CollectionPinnedItem[];
}

export interface CollectionDefinition {
  sources: CollectionSourceBlock[];
  filters: CollectionFilters;
  limit?: number | null;
  sort?: CollectionSortOption | null;
  /** When sort is rating (movies): which Radarr ratings.* key to order by. */
  sort_provider?: CollectionRatingProvider | null;
  pins?: CollectionPins;
}

export interface CollectionRunSummary {
  status: "ok" | "error" | "cleared" | "skipped" | "dormant";
  error?: string;
  reason?: string;
  window_cleared?: boolean;
  pinned_in?: number;
  pinned_out?: number;
  tmdb_candidates?: number | null;
  matched_in_catalog?: number;
  after_filters?: number;
  selected?: number;
  in_target_library?: number | null;
  unresolved?: number | null;
  /** Source titles not in ARR (last run). Pre-filter; not Plex unresolved. */
  missing_from_arr_count?: number;
  /** Titles missing now that were not in the previous run's missing set. Null = no baseline yet. */
  missing_from_arr_new?: number | null;
  missing_from_arr_keys?: string[];
  synced?: { added: number; removed: number; total: number; created: boolean };
  libraries?: CollectionLibrarySyncSummary[];
}

export interface CollectionLibrarySyncSummary {
  plex_section_id: number;
  in_target_library?: number | null;
  unresolved?: number | null;
  plex_error?: string | null;
  synced?: { added: number; removed: number; total: number; created: boolean };
}

export interface CollectionActiveWindow {
  /** MM-DD, annually recurring; wrap-around (start > end) spans the new year. */
  start: string;
  end: string;
  when_inactive: "keep" | "clear";
}

export interface CollectionRecipe {
  id: number;
  name: string;
  enabled: boolean;
  plex_section_id: number;
  /** Extra + primary Plex section ids (same type). First id matches plex_section_id. */
  plex_section_ids?: number[];
  plex_section_type: "movie" | "show";
  collection_title: string;
  definition: CollectionDefinition;
  /** Null = follow the global COLLECTIONS_SYNC_INTERVAL_HOURS cadence. */
  run_interval_hours: number | null;
  active_window: CollectionActiveWindow | null;
  /** Whether today falls inside the active window (always true without one). */
  window_active: boolean;
  last_run_at: string | null;
  last_run_summary: CollectionRunSummary | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CollectionRecipesResponse {
  recipes: CollectionRecipe[];
  tmdb_configured: boolean;
  trakt_configured: boolean;
}

export interface PlexSectionOption {
  id: number;
  title: string;
  type: "movie" | "show";
  item_count: number;
}

export interface CollectionTmdbMeta {
  configured: boolean;
  genres: { id: number; name: string }[];
  providers: { id: number; name: string; priority?: number | null }[];
  regions: { code: string; name: string }[];
}

export interface CollectionBuilderMeta {
  instances: { instance_key: string; label: string; arr_type: string }[];
  /** key is "{instance_key}:{profile_id}" — matches engine filter values. */
  quality_profiles: { key: string; name: string; instance_key: string; instance_label: string }[];
  languages: string[];
  /** Distinct Radarr/Sonarr genre names present in the catalog (matches filter evaluation). */
  genres: string[];
  /** Distinct Radarr/Sonarr certifications present in the catalog (matches filter evaluation). */
  certifications: string[];
}

export type CollectionExplainStatus = "pass" | "fail" | "skip";

export interface CollectionExplainCheck {
  status: CollectionExplainStatus;
  detail?: string | null;
  /** Source checks */
  type?: string;
  list_ref?: string | null;
  /** Filter rule checks */
  field?: string;
  op?: string | null;
  value?: string | number | boolean | null;
  value_to?: number | null;
  values?: string[] | null;
  basis?: CollectionReleaseWindowBasis | CollectionYearBasis | null;
  provider?: CollectionRatingProvider | null;
  min_votes?: number | null;
}

export interface CollectionExplainRuleNode extends CollectionExplainCheck {
  kind: "rule";
}

export interface CollectionExplainGroupNode {
  kind: "group";
  op: "and" | "or";
  status: CollectionExplainStatus;
  children: CollectionExplainNode[];
}

export type CollectionExplainNode = CollectionExplainRuleNode | CollectionExplainGroupNode;

export interface CollectionExplainStage {
  key: "sources" | "catalog" | "filters" | "pins" | "limit" | "library";
  status: CollectionExplainStatus;
  detail: string | null;
  checks: CollectionExplainCheck[];
  /** Filters stage only: verdict tree mirroring the filter structure. */
  tree?: CollectionExplainGroupNode | null;
}

export interface CollectionExplainResponse {
  in_collection: boolean;
  stages: CollectionExplainStage[];
}

export interface CollectionPreviewSampleItem {
  id: number;
  title: string;
  year: number | null;
  tmdb_id: number | null;
  tvdb_id: number | null;
  poster: string | null;
  has_file?: boolean;
  has_placeholder?: boolean;
  file_state?: "file" | "placeholder" | "mixed" | "none";
  in_libraries?: number[];
}

export interface CollectionMissingFromArrItem {
  title: string;
  year: number | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  imdb_id?: string | null;
  poster: string | null;
}

export interface CollectionPreviewResponse {
  tmdb_candidates: number | null;
  matched_in_catalog: number;
  after_filters: number;
  pinned_in: number;
  pinned_out: number;
  selected: number;
  in_target_library: number | null;
  unresolved: number | null;
  plex_error: string | null;
  libraries?: CollectionLibrarySyncSummary[];
  sample: CollectionPreviewSampleItem[];
  missing_from_arr?: CollectionMissingFromArrItem[];
  missing_from_arr_count?: number;
  missing_from_arr_prefilter_count?: number;
  missing_from_arr_filter_gaps?: string[];
}

export interface CollectionArrAddInstanceOptions {
  instance_key: string;
  label: string;
  arr_type: string;
  quality_profiles: { id: number; name: string }[];
  root_folders: { id?: number | null; path: string }[];
}

export interface CollectionArrAddOptionsResponse {
  instances: CollectionArrAddInstanceOptions[];
}

export interface CollectionArrAddItem {
  title: string;
  year?: number | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  imdb_id?: string | null;
}

export interface CollectionArrAddResult {
  title: string;
  instance_key: string;
  status: "ok" | "skipped" | "error";
  error?: string | null;
}

export interface CollectionArrAddResponse {
  ok: boolean;
  added: number;
  skipped: number;
  errors: number;
  results: CollectionArrAddResult[];
  message: string;
}
