import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { copyTextToClipboard } from "./copyToClipboard";
import { ARR_WEBHOOK_SERVICES, PLAYBACK_WEBHOOK_SERVICES } from "./webhookConfig";
import {
  getActivity,
  getCalendar,
  getErrors,
  getLibrary,
  getLogs,
  getMovieDetail,
  getPlaceholderActivity,
  getSeriesDetail,
  getSettingsCurrent,
  getSettingsStatus,
  getStats,
  saveSettings,
  testIntegrationConnection,
} from "./api/dashboard";
import embyIcon from "./assets/services/emby.svg";
import jellyfinIcon from "./assets/services/jellyfin.svg";
import plexIcon from "./assets/services/plex.svg";
import radarrIcon from "./assets/services/radarr.svg";
import sonarrIcon from "./assets/services/sonarr.svg";
import placeholdarrLogoBlue from "./assets/Placeholdarr_blue.svg";
import placeholdarrLogoYellow from "./assets/Placeholdarr_yellow.svg";
import type { Brand, ThemeMode } from "./brandTypes";
import { getBrandSemanticTokens, semanticTokensToCssVars, type BrandSemanticTokens } from "./brandSemanticTheme";
import tautulliIcon from "./assets/services/tautulli.svg";
import type {
  ActivityRow,
  ArrInstanceOpenLink,
  CalendarDay,
  CalendarResponse,
  DashboardTab,
  ErrorRow,
  LibraryItem,
  MovieDetailResponse,
  SeriesDetailResponse,
  SeriesSeasonDetail,
  SeriesEpisodeDetail,
  SettingsField,
  SettingsPayload,
  StatsResponse,
} from "./types/api";

const REFRESH_MS_VISIBLE = 5000;
const REFRESH_MS_HIDDEN = 30000;
const LIBRARY_MOVIES_PATH = "/library";
const LIBRARY_TV_PATH = "/library/tv";
const LIBRARY_MOVIES_FILTER_KEY = "placeholdarr:library-shelf-filter:movies";
const LIBRARY_TV_FILTER_KEY = "placeholdarr:library-shelf-filter:tv";
/** Legacy single key (pre split movies / TV pages). */
const LIBRARY_FILTER_STORAGE_KEY_LEGACY = "placeholdarr:library-filter";

export type LibraryShelfFilter = "all" | "placeholders" | "future" | "missing";

function isLibraryShelfFilter(v: string | null): v is LibraryShelfFilter {
  return v === "all" || v === "placeholders" || v === "future" || v === "missing";
}

function readStoredShelfFilter(storageKey: string): LibraryShelfFilter {
  try {
    const v = sessionStorage.getItem(storageKey);
    if (isLibraryShelfFilter(v)) return v;
  } catch {
    /* private / blocked storage */
  }
  return "all";
}

/** One-time read of legacy `placeholdarr:library-filter` into shelf filters; clears legacy key when consumed. */
function readLegacyLibraryFilterMigration(): { movies: LibraryShelfFilter; tv: LibraryShelfFilter } | null {
  try {
    const v = sessionStorage.getItem(LIBRARY_FILTER_STORAGE_KEY_LEGACY);
    if (v == null) return null;
    sessionStorage.removeItem(LIBRARY_FILTER_STORAGE_KEY_LEGACY);
    if (v === "movie") return { movies: "all", tv: "all" };
    if (v === "series") return { movies: "all", tv: "all" };
    if (isLibraryShelfFilter(v)) return { movies: v, tv: v };
  } catch {
    /* ignore */
  }
  return null;
}

function getLibraryListShelf(pathname: string): "movies" | "tv" | null {
  const p = pathname.replace(/\/$/, "") || "/";
  if (p === LIBRARY_TV_PATH) return "tv";
  if (p === LIBRARY_MOVIES_PATH) return "movies";
  return null;
}

function digestLibraryItems(items: LibraryItem[]): string {
  return items
    .map((i) => `${i.id}\t${i.title}\t${i.year}\t${i.type}\t${i.has_file}\t${i.has_placeholder}\t${i.is_future}\t${i.has_missing}\t${i.status ?? ""}\t${i.overview ?? ""}`)
    .join("\n");
}

function formatDashboardDataAge(msAgo: number): string {
  const s = Math.floor(msAgo / 1000);
  if (s < 12) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}
const LOG_TAIL_LINES = 2000;
const STUDIO_THEME_MODE_STORAGE_KEY = "placeholdarr:studio-theme-mode";

function readStoredThemeMode(): ThemeMode {
  try {
    const v = localStorage.getItem(STUDIO_THEME_MODE_STORAGE_KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* private / blocked storage */
  }
  return "dark";
}
const SETTINGS_SECTION_ORDER = [
  "Media Integrations",
  "ARR Integrations",
  "Paths",
  "Library sync",
  "Calendar",
  "Lookahead",
  "Status Updates",
  "Advanced",
];
const SETTINGS_SECTION_ICONS: Record<string, string> = {
  "Media Integrations": "hub",
  "ARR Integrations": "dns",
  "Paths": "folder",
  "Library sync": "sync",
  "Calendar": "calendar_month",
  "Lookahead": "fast_forward",
  "Status Updates": "edit_notifications",
  "Advanced": "tune",
};
const SETTINGS_SECTION_SLUGS: Record<string, string> = {
  "Media Integrations": "media-integrations",
  "ARR Integrations": "arr-integrations",
  "Paths": "paths",
  "Library sync": "library-sync",
  "Calendar": "calendar",
  "Lookahead": "lookahead",
  "Status Updates": "status-updates",
  "Advanced": "advanced",
};
const BEHAVIOR_WIZARD_SECTIONS = [
  "ARR Integrations",
  "Library sync",
  "Calendar",
  "Lookahead",
  "Status Updates",
  "Advanced",
] as const;

/** Shared onboarding section heading (behavior wizard, paths). */
const ONBOARDING_SECTION_TITLE_CLASS =
  "mb-3 pb-2 border-b border-[#424753]/40 text-base font-headline font-bold uppercase tracking-wide text-white";

/**
 * Grouped settings / onboarding section surface: raised slate panel with a brand accent3 rail on all sides.
 * Requires an ancestor that sets CSS vars (e.g. `semanticTokensToCssVars` on `.brand-theme-scope`).
 */
const UI_SECTION_FRAME_CLASS =
  "rounded-lg border border-[var(--brand-accent-3)] bg-[color:color-mix(in_srgb,var(--brand-surface-panel)_92%,var(--brand-accent-3)_8%)] shadow-lg shadow-black/15";

/**
 * Media / ARR service tiles (wizard + settings grids): same accent rail and surface fill as
 * {@link UI_SECTION_FRAME_CLASS}, with the taller rounded-2xl silhouette.
 */
const UI_INTEGRATION_CARD_SURFACE_CLASS =
  "rounded-2xl border border-[var(--brand-accent-3)] bg-[color:color-mix(in_srgb,var(--brand-surface-panel)_92%,var(--brand-accent-3)_8%)] shadow-lg shadow-black/15 backdrop-blur-md transition hover:shadow-[0_0_36px_-14px_color-mix(in_srgb,var(--brand-accent-3)_28%,transparent)]";

/** Wizard stacked section bodies (bottom margin between sections). */
const WIZARD_ONBOARDING_SECTION_SURFACE_CLASS = `mb-4 ${UI_SECTION_FRAME_CLASS} px-4 py-4 sm:px-5`;

const WIZARD_STEPS = [
  { key: "paths", name: "Paths" },
  { key: "media", name: "Media Servers" },
  { key: "arr", name: "ARR Services" },
  { key: "behavior", name: "Behavior" },
] as const;

const TMDB_POSTER_IMG_BASE = "https://image.tmdb.org/t/p/w300";

/** `poster_path` values (TMDB) — onboarding hero only; decorative collage (16 unique slots). */
const WIZARD_HEADER_POSTER_PATHS: readonly string[] = [
  "/qJ2tW6WMUDux911r6m7haRef0WH.jpg", // The Dark Knight
  "/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg", // Fight Club
  "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg", // Inception
  "/49WJfeN0moxb9IPfGn8AIqMGskD.jpg", // The Shawshank Redemption
  "/u3bZgnGQ9T01sWNhyveQz0wH0Hl.jpg", // Breaking Bad (miniseries key art)
  "/9gk7adHYeDvHkCSEqAvQNLV5Uge.jpg", // Django Unchained
  "/aKuFiU82s5ISJpGZp7YkIr3kCUd.jpg", // The Lord of the Rings: The Fellowship of the Ring
  "/q6y0Go1tsGEsmtFryDOJo3dEmqu.jpg", // Pulp Fiction
  "/6oom5QYQ2yQTMJIbnvbkBL9cHo6.jpg", // Interstellar
  "/zSqJ1qFq8NXFfi7JeIYMlzyR0dx.jpg", // The Matrix
  "/vL5LR6WdxWPjLPFRLe133jXWsh5.jpg", // Goodfellas
  "/63N9uy8nd9j7Eog2axPQ8lbr3Wj.jpg", // Star Wars
  "/sF1U4EUQS8YHUYjNl3pMGNIQyr0.jpg", // Blade Runner
  "/hek3koDUyRQk7FIhPXsa6mT2Zc3.jpg", // The Godfather Part II
  "/vZloFAK7NmvMGKE7VkF5UHaz0I.jpg", // Spider-Man: Into the Spider-Verse
  "/7WsyChQLEftFiDOVTGkv3hFpyyt.jpg", // Avengers: Infinity War
];

/** Light / high-key art for the hero center 2×2 when `yellowBlue` — reads better under yellow multiply + slate lockup. */
const ONBOARDING_HERO_LIGHT_CENTER_POSTERS: readonly [string, string, string, string] = [
  "/itAKcobTYGpYT8Phwjd8c9hleTo.jpg", // Frozen (2013)
  "/uDO8zWDhfWwoFdKS4fzkUJt0Rf0.jpg", // La La Land (2016)
  "/pZn87R7gtmMCGGO8KeaAfZDhXLg.jpg", // Soul (2020)
  "/iuFNMS8U5cb6xfzi51Dbkovj7vM.jpg", // Barbie (2023)
];
/** Center cell indices for `grid-cols-4` / `grid-rows-4` (matches `min-[520px]:` breakpoint below). */
const ONBOARDING_HERO_LIGHT_CENTER_SLOTS_NARROW = [5, 6, 9, 10] as const;
/** Center cell indices for `grid-cols-8` / `grid-rows-2`. */
const ONBOARDING_HERO_LIGHT_CENTER_SLOTS_WIDE = [3, 4, 11, 12] as const;

const PATH_LIBRARY_ROOT_KEY = "LIBRARY_ROOT";
const PATH_PROFILE_KEYS = [] as const;
const PATH_PER_LIBRARY_OVERRIDE_KEYS = [] as const;

const HIDDEN_PLAYBACK_INTERNAL_KEYS = new Set<string>([]);

/** Shown in API/config but omitted from dashboard UI (onboarding + settings). */
const SETTINGS_UI_HIDDEN_FIELD_KEYS = new Set<string>(["WORKER_COUNT"]);

const ARR_CONFIGURATION_KEYS = new Set<string>([
  "ARR_INSTANCES_JSON",
]);

const ARR_SEARCH_PLAYBACK_KEYS = new Set<string>([
  "MOVIE_PLACEHOLDER_SEARCH_MODE",
  "TV_PLACEHOLDER_SEARCH_MODE",
]);

const ARR_REAL_FILE_PLAYBACK_KEYS = new Set<string>([
  "MOVIE_PLAYBACK_INSTANCE_MODE",
  "TV_PLAYBACK_INSTANCE_MODE",
  "ENABLE_PLAYBACK_FALLBACK_SEARCH",
  "PLAYBACK_FALLBACK_TIMEOUT_MINUTES",
]);

const ARR_BEHAVIOR_KEYS = new Set<string>([
  ...ARR_SEARCH_PLAYBACK_KEYS,
  ...ARR_REAL_FILE_PLAYBACK_KEYS,
]);

function partitionLibraryPathFields(fields: SettingsField[]) {
  const profileSet = new Set<string>(PATH_PROFILE_KEYS as unknown as string[]);
  const overrideSet = new Set<string>(PATH_PER_LIBRARY_OVERRIDE_KEYS as unknown as string[]);
  const byKey = new Map(fields.map((f) => [f.key, f]));
  const root = byKey.get(PATH_LIBRARY_ROOT_KEY);
  const profiles = PATH_PROFILE_KEYS.map((k) => byKey.get(k)).filter(Boolean) as SettingsField[];
  const overrides = PATH_PER_LIBRARY_OVERRIDE_KEYS.map((k) => byKey.get(k)).filter(Boolean) as SettingsField[];
  const rest = fields.filter((f) => f.key !== PATH_LIBRARY_ROOT_KEY && !profileSet.has(f.key) && !overrideSet.has(f.key));
  return { root, profiles, overrides, rest };
}

const URL_TEST_TARGET: Record<string, { service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr"; credentialKey: string }> = {
  PLEX_URL: { service: "plex", credentialKey: "PLEX_TOKEN" },
  JELLYFIN_URL: { service: "jellyfin", credentialKey: "JELLYFIN_TOKEN" },
  EMBY_URL: { service: "emby", credentialKey: "EMBY_TOKEN" },
};

/** Activity stat cards still route with movie/series distinction; list pages use {@link LibraryShelfFilter}. */
type LibraryFilter = "all" | "movie" | "series" | "placeholders" | "future" | "missing";

type FieldValueMap = Record<string, unknown>;

type CalendarFilters = {
  mediaTypes: Record<string, boolean>;
  releaseTypes: Record<string, boolean>;
};

type BrandAccent = {
  label: string;
  hex: string;
  text: string;
  icon: string;
  hoverHex: string;
};

/** Product name is Placeholdarr; `Brand` is the single official dashboard token set. */
const BRAND: Brand = "placeholdarr";

const BRAND_META: { label: string; tagline: string } = {
  label: "Placeholdarr",
  tagline: "High fidelity simulation — your library as a living spec sheet.",
};

/** Branded first paint for /setup while settings load (Seerr-style splash: motion masks wait). */
function SetupBootShell(props: {
  setupShellClass: string;
  surfaceStyle: CSSProperties;
  brand: Brand;
  accentHex: string;
  appLabel: string;
  errorMessage?: string | null;
}) {
  const err = props.errorMessage?.trim();
  return (
    <div className={`${props.setupShellClass} setup-boot-shell`} style={props.surfaceStyle}>
      <div className="setup-boot-shell__content flex w-full max-w-[min(92vw,22rem)] flex-col items-center gap-7">
        <div className="flex flex-col items-center gap-3 opacity-90">
          <BrandLogo brand={props.brand} accentHex={props.accentHex} className="h-14 w-auto max-w-[11rem] object-contain" />
          <div className="text-center text-[11px] font-headline font-bold uppercase tracking-[0.22em] text-slate-500">
            {props.appLabel}
          </div>
        </div>
        <div className="w-full space-y-2.5" aria-hidden={Boolean(err)}>
          <div className="setup-boot-shell__bar setup-boot-shell__bar--lg" />
          <div className="setup-boot-shell__bar setup-boot-shell__bar--md" />
          <div className="setup-boot-shell__bar setup-boot-shell__bar--sm" />
        </div>
        {err ? (
          <p className="max-w-full text-center text-xs leading-relaxed text-red-400/95" role="alert">
            {err}
          </p>
        ) : (
          <div className="flex items-center gap-2 text-[11px] font-headline uppercase tracking-widest text-slate-500">
            <span className="material-symbols-outlined animate-spin text-slate-500" style={{ fontSize: 16 }}>
              progress_activity
            </span>
            <span className="animate-pulse">Preparing setup…</span>
          </div>
        )}
      </div>
    </div>
  );
}

const BRAND_ACCENTS: Record<`${Brand}-${ThemeMode}`, BrandAccent> = {
  "placeholdarr-light": {
    label: "Placeholdarr",
    /** Primary stat / hero yellow (matches dark top bar band), not ochre. */
    hex: "#FBBF24",
    text: "#0f172a",
    /** Cyan rail / secondary chrome on light panels */
    icon: "#0284C7",
    hoverHex: "#D97706",
  },
  "placeholdarr-dark": {
    label: "Placeholdarr",
    hex: "#FBBF24",
    text: "#E2E8F0",
    icon: "#CBD5E1",
    hoverHex: "#d97706",
  },
};

function getStudioDarkBackdrop(_brand: Brand, _accent: BrandAccent, semantic: BrandSemanticTokens): string {
  return `radial-gradient(1000px 540px at 6% -14%, ${alphaColor(semantic.accent, 0.28)}, transparent 58%), radial-gradient(860px 480px at 96% 108%, ${alphaColor(semantic.accentIce, 0.22)}, transparent 54%), radial-gradient(720px 420px at 44% 96%, ${alphaColor(semantic.accent3, 0.14)}, transparent 56%), linear-gradient(172deg, ${semantic.chromeSidebar} 0%, ${semantic.surfaceMuted} 46%, ${semantic.surfacePanel} 100%)`;
}

function alphaColor(hex: string, alpha: number) {
  const normalized = hex.replace("#", "");
  const expanded = normalized.length === 3
    ? normalized.split("").map((char) => `${char}${char}`).join("")
    : normalized;
  const value = Number.parseInt(expanded, 16);
  const red = (value >> 16) & 255;
  const green = (value >> 8) & 255;
  const blue = value & 255;
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function titleSortKey(title: string | null | undefined): string {
  const raw = String(title || "").trim().toLowerCase();
  // Ignore leading punctuation/quotes and leading articles for alpha sorting.
  return raw
    .replace(/^[^a-z0-9]+/i, "")
    .replace(/^(the|an|a)\s+/i, "")
    .replace(/^[^a-z0-9]+/i, "");
}

function titleSortLetter(title: string | null | undefined): string {
  const key = titleSortKey(title);
  const first = key.charAt(0).toUpperCase();
  return /[A-Z]/.test(first) ? first : "#";
}

function getBrandAccent(brand: Brand, theme: ThemeMode) {
  const key = `${brand}-${theme}` as const;
  return BRAND_ACCENTS[key];
}

function BrandLogo(props: { brand: Brand; accentHex: string; className?: string; variant?: "blue" | "yellow" }) {
  void props.accentHex;
  const src = props.variant === "yellow" ? placeholdarrLogoYellow : placeholdarrLogoBlue;
  return (
    <img
      src={src}
      alt=""
      className={`block object-contain object-left select-none ${props.className ?? ""}`}
      draggable={false}
      aria-hidden
    />
  );
}

function getBrandFocusClass(brand: Brand, theme: ThemeMode) {
  const accent = getBrandAccent(brand, theme);
  const hex = accent.hex.replace("#", "");
  return `focus:border-[#${hex}]/70`;
}

function getBrandSelectionClass(brand: Brand, theme: ThemeMode) {
  const accent = getBrandAccent(brand, theme);
  const hex = accent.hex.replace("#", "");
  return `bg-[#${hex}]/20`;
}

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const contentScrollRef = useRef<HTMLElement | null>(null);

  const brand = BRAND;
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => readStoredThemeMode());

  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [library, setLibrary] = useState<LibraryItem[]>([]);
  const [calendar, setCalendar] = useState<CalendarResponse | null>(null);
  const [errors, setErrors] = useState<ErrorRow[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [logFile, setLogFile] = useState<string>("");
  const [logLevel, setLogLevel] = useState<"all" | "debug" | "info" | "warn" | "error" | "critical">("all");
  const [logFilter, setLogFilter] = useState("");
  const [placeholderActivity, setPlaceholderActivity] = useState<any[]>([]);
  const [activityTab, setActivityTab] = useState<"system" | "placeholders">("system");
  const activityTabRef = useRef<"system" | "placeholders">(activityTab);
  activityTabRef.current = activityTab;

  const [libraryShelfFilters, setLibraryShelfFilters] = useState<{ movies: LibraryShelfFilter; tv: LibraryShelfFilter }>(() => {
    const migrated = readLegacyLibraryFilterMigration();
    if (migrated) return migrated;
    return {
      movies: readStoredShelfFilter(LIBRARY_MOVIES_FILTER_KEY),
      tv: readStoredShelfFilter(LIBRARY_TV_FILTER_KEY),
    };
  });
  const [calendarMonth, setCalendarMonth] = useState(getCurrentMonthToken());
  const [calendarFilters, setCalendarFilters] = useState<CalendarFilters>({
    mediaTypes: { movie: true, episode: true },
    releaseTypes: { inCinemas: true, digitalRelease: true, physicalRelease: true },
  });
  const [calendarSelectedId, setCalendarSelectedId] = useState<string | null>(null);
  const [calendarSpotlightOpen, setCalendarSpotlightOpen] = useState(false);
  const [calendarSpotlight, setCalendarSpotlight] = useState<MovieDetailResponse | SeriesDetailResponse | null>(null);
  const [calendarSpotlightLoading, setCalendarSpotlightLoading] = useState(false);
  const [calendarSpotlightCache, setCalendarSpotlightCache] = useState<Record<string, MovieDetailResponse | SeriesDetailResponse>>({});

  const [titleSearch, setTitleSearch] = useState("");
  const [titleSearchOpen, setTitleSearchOpen] = useState(false);
  const [titleSearchIndex, setTitleSearchIndex] = useState(0);

  const [settingsPayload, setSettingsPayload] = useState<SettingsPayload | null>(null);
  const settingsPayloadRef = useRef<SettingsPayload | null>(null);
  useEffect(() => {
    settingsPayloadRef.current = settingsPayload;
  }, [settingsPayload]);
  const [activeSettingsSection, setActiveSettingsSection] = useState("Media Integrations");
  const [fieldValues, setFieldValues] = useState<FieldValueMap>({});
  const [baselineValues, setBaselineValues] = useState<FieldValueMap>({});
  const [settingsFeedback, setSettingsFeedback] = useState("");
  const [settingsFeedbackKind, setSettingsFeedbackKind] = useState<"" | "success" | "error">("");

  const [setupStatus, setSetupStatus] = useState<{ setup_complete: boolean } | null>(null);
  const setupCompleteRef = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    setupCompleteRef.current = setupStatus?.setup_complete;
  }, [setupStatus]);
  const [onboardingVisible, setOnboardingVisible] = useState(false);
  const [onboardingStepIndex, setOnboardingStepIndex] = useState(0);

  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [dashboardRefreshing, setDashboardRefreshing] = useState(false);
  const [lastDashboardSuccessAt, setLastDashboardSuccessAt] = useState<number | null>(null);
  /** Re-render header “Live · … ago” without waiting for the next poll. */
  const [dashboardAgeTick, setDashboardAgeTick] = useState(0);

  const titleSearchRef = useRef(titleSearch);
  useEffect(() => {
    titleSearchRef.current = titleSearch;
  }, [titleSearch]);

  const libraryDigestRef = useRef<string>("");

  const currentTab = getTabFromPath(location.pathname);
  const settingsSectionNames = useMemo(
    () =>
      settingsPayload
        ? SETTINGS_SECTION_ORDER.filter((name) => settingsPayload.sections.some((s) => s.name === name))
        : [],
    [settingsPayload],
  );
  const firstSettingsSection = settingsSectionNames[0] ?? SETTINGS_SECTION_ORDER[0];
  const firstSettingsPath = `/settings/${SETTINGS_SECTION_SLUGS[firstSettingsSection] ?? "media-integrations"}`;
  /** Until we know setup is complete, prefer `/setup` so `/` does not bounce through `/activity` (which runs heavy Activity fetches before we learn onboarding is incomplete). */
  const defaultLandingPath =
    setupStatus != null && setupStatus.setup_complete ? "/activity" : "/setup";
  const showReconnectPanel = !!errorMessage && /Cannot reach the Placeholdarr API/i.test(errorMessage);
  const brandAccent = getBrandAccent(brand, themeMode);
  const brandSemantic = getBrandSemanticTokens(brand, themeMode, brandAccent);
  const brandMeta = BRAND_META;
  /** Flat shell for setup-route “Loading…” only — avoids multi-layer backdrop paint before the wizard mounts. */
  const setupLoadingShellStyle = useMemo(
    () =>
      ({
        ...(semanticTokensToCssVars(brandSemantic) as CSSProperties),
        backgroundColor: brandSemantic.chromePage,
      }) as CSSProperties,
    [brandSemantic],
  );
  const hasUnsavedChanges = useMemo(
    () => settingsValuesDirty(fieldValues, baselineValues, settingsPayload),
    [fieldValues, baselineValues, settingsPayload],
  );
  const hasUnsavedChangesRef = useRef(false);
  const selectedCalendarItem = useMemo(
    () => findCalendarItem(calendar, calendarSelectedId),
    [calendar, calendarSelectedId],
  );
  const selectedCalendarDetailKey = useMemo(() => {
    if (!selectedCalendarItem) return null;
    if (selectedCalendarItem.media_type === "movie") {
      return `movie:${selectedCalendarItem.item_id}`;
    }
    if (selectedCalendarItem.series_id) {
      return `series:${selectedCalendarItem.series_id}`;
    }
    return `episode:${selectedCalendarItem.item_id}`;
  }, [selectedCalendarItem]);
  const titleSearchResults = useMemo(() => {
    const query = titleSearch.trim().toLowerCase();
    if (!query) return [];

    return [...library]
      .filter((item) => {
        const title = item.title.toLowerCase();
        const overview = String(item.overview || "").toLowerCase();
        return title.includes(query) || overview.includes(query);
      })
      .sort((left, right) => {
        const leftTitle = titleSortKey(left.title);
        const rightTitle = titleSortKey(right.title);
        const leftStarts = leftTitle.startsWith(query) ? 0 : 1;
        const rightStarts = rightTitle.startsWith(query) ? 0 : 1;
        if (leftStarts !== rightStarts) return leftStarts - rightStarts;
        return leftTitle.localeCompare(rightTitle);
      })
      .slice(0, 8);
  }, [library, titleSearch]);

  useEffect(() => {
    hasUnsavedChangesRef.current = hasUnsavedChanges;
  }, [hasUnsavedChanges]);

  useEffect(() => {
    try {
      sessionStorage.setItem(LIBRARY_MOVIES_FILTER_KEY, libraryShelfFilters.movies);
      sessionStorage.setItem(LIBRARY_TV_FILTER_KEY, libraryShelfFilters.tv);
    } catch {
      /* ignore */
    }
  }, [libraryShelfFilters]);

  useEffect(() => {
    if (lastDashboardSuccessAt == null) return;
    const id = window.setInterval(() => setDashboardAgeTick((n) => n + 1), 5000);
    return () => window.clearInterval(id);
  }, [lastDashboardSuccessAt]);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!hasUnsavedChanges) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [hasUnsavedChanges]);

  useEffect(() => {
    let stopped = false;
    let timeoutId = 0;
    let showRefreshChrome = false;

    const scheduleNext = () => {
      window.clearTimeout(timeoutId);
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      const delay = hidden ? REFRESH_MS_HIDDEN : REFRESH_MS_VISIBLE;
      timeoutId = window.setTimeout(() => {
        void runRefresh().then(() => {
          if (!stopped) scheduleNext();
        });
      }, delay);
    };

    async function runRefresh() {
      showRefreshChrome = false;
      try {
        if (currentTab === "setup") {
          // First visit: one full `getSettingsCurrent` (via loadSettings) so we do not block on status
          // and then again in a separate effect — that doubled network latency before the wizard appeared.
          // Later polls: light `getSettingsStatus` only so we never clobber in-progress wizard edits.
          try {
            if (!settingsPayloadRef.current) {
              await loadSettings(stopped);
            } else {
              const status = await getSettingsStatus();
              if (!stopped) {
                setSetupStatus(status);
                setOnboardingVisible(!status.setup_complete);
              }
            }
            if (!stopped) {
              setErrorMessage(null);
            }
          } catch (err) {
            if (!stopped) {
              setErrorMessage(err instanceof Error ? err.message : "Unable to load setup. Check the API and try again.");
            }
          }
          if (!stopped) {
            setLoading(false);
          }
          return;
        }

        if (currentTab === "activity" && setupCompleteRef.current === false) {
          const status = await getSettingsStatus();
          if (!stopped) {
            setSetupStatus(status);
            setOnboardingVisible(!status.setup_complete);
          }
          if (!stopped) {
            setErrorMessage(null);
            setLoading(false);
          }
          return;
        }

        showRefreshChrome = true;
        if (!stopped) setDashboardRefreshing(true);

        if (currentTab === "activity") {
          await loadStats(stopped, setStats);
          if (activityTabRef.current === "system") {
            const rows = await getActivity(100);
            if (!stopped) setActivity(rows || []);
          }
          const placeholderRows = await getPlaceholderActivity(100);
          if (!stopped) setPlaceholderActivity(placeholderRows || []);
        } else if (currentTab === "library") {
          const searchTrim = titleSearchRef.current.trim();
          const useSummary = searchTrim.length === 0;
          const payload = await getLibrary(1000, { summary: useSummary });
          const next = payload.items || [];
          const digest = digestLibraryItems(next);
          if (digest !== libraryDigestRef.current) {
            libraryDigestRef.current = digest;
            if (!stopped) setLibrary(next);
          }
        } else if (currentTab === "calendar") {
          const payload = await getCalendar(calendarMonth);
          if (!stopped && payload.ok) {
            setCalendar(payload);
            setCalendarMonth(payload.month || calendarMonth);
          }
        } else if (currentTab === "errors") {
          const rows = await getErrors(100);
          if (!stopped) setErrors(rows || []);
        } else if (currentTab === "logs") {
          const payload = await getLogs(logLevel, LOG_TAIL_LINES);
          if (!stopped) {
            setLogs(payload.lines || []);
            setLogFile(payload.file || "");
          }
        } else if (currentTab === "settings") {
          // Avoid clobbering in-progress edits from the periodic refresh loop.
          if (!hasUnsavedChangesRef.current) {
            await loadSettings(stopped);
          }
        }

        const status = await getSettingsStatus();
        if (!stopped) {
          setSetupStatus(status);
          setOnboardingVisible(!status.setup_complete);
        }

        if (!stopped) {
          setErrorMessage(null);
          setLoading(false);
          setLastDashboardSuccessAt(Date.now());
        }
      } catch (err) {
        if (!stopped) {
          setErrorMessage(err instanceof Error ? err.message : "Dashboard refresh failed");
          setLoading(false);
        }
      } finally {
        if (showRefreshChrome && !stopped) {
          setDashboardRefreshing(false);
        }
      }
    }

    void runRefresh().then(() => {
      if (!stopped) scheduleNext();
    });

    const onVisibility = () => {
      window.clearTimeout(timeoutId);
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void runRefresh().then(() => {
          if (!stopped) scheduleNext();
        });
      } else {
        scheduleNext();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timeoutId);
    };
  }, [calendarMonth, currentTab, logLevel]);

  /** When switching back to System Activity, refresh the feed (poll may have skipped it on Placeholder History). */
  useEffect(() => {
    if (currentTab !== "activity" || activityTab !== "system") return;
    let cancelled = false;
    void (async () => {
      try {
        const rows = await getActivity(100);
        if (!cancelled) setActivity(rows || []);
      } catch {
        /* ignore — next poll will retry */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activityTab, currentTab]);

  /** When the user searches from the header, load full rows (overview) so overview matches work. */
  useEffect(() => {
    const q = titleSearch.trim();
    if (!q) return;

    let stopped = false;
    const handle = window.setTimeout(() => {
      void (async () => {
        try {
          const payload = await getLibrary(1000, { summary: false });
          if (stopped) return;
          const next = payload.items || [];
          libraryDigestRef.current = digestLibraryItems(next);
          setLibrary(next);
        } catch {
          /* ignore — periodic refresh will retry */
        }
      })();
    }, 400);

    return () => {
      stopped = true;
      window.clearTimeout(handle);
    };
  }, [titleSearch]);

  useEffect(() => {
    if (library.length > 0) return;
    if (location.pathname === "/setup" || location.pathname.startsWith("/setup/")) return;

    let stopped = false;
    getLibrary(1000, { summary: true })
      .then((payload) => {
        if (!stopped) {
          const next = payload.items || [];
          libraryDigestRef.current = digestLibraryItems(next);
          setLibrary(next);
        }
      })
      .catch(() => {
        // Ignore prefetch failures; the tab-specific loader will retry.
      });

    return () => {
      stopped = true;
    };
  }, [library.length, location.pathname]);

  useEffect(() => {
    if (currentTab !== "calendar") return;

    const visibleItems = flattenVisibleCalendarItems(calendar, calendarFilters);
    if (!visibleItems.length) {
      setCalendarSelectedId(null);
      setCalendarSpotlight(null);
      setCalendarSpotlightLoading(false);
      return;
    }

    if (!calendarSelectedId || !visibleItems.some((item) => item.id === calendarSelectedId)) {
      setCalendarSelectedId(visibleItems[0].id);
    }
  }, [calendar, calendarFilters, calendarSelectedId, currentTab]);

  useEffect(() => {
    if (currentTab !== "calendar" || !selectedCalendarItem) {
      setCalendarSpotlightLoading(false);
      return;
    }

    if (!selectedCalendarDetailKey) {
      setCalendarSpotlight(null);
      setCalendarSpotlightLoading(false);
      return;
    }

    const cachedSpotlight = calendarSpotlightCache[selectedCalendarDetailKey];
    if (cachedSpotlight) {
      setCalendarSpotlight(cachedSpotlight);
      setCalendarSpotlightLoading(false);
      return;
    }

    let stopped = false;
    const item = selectedCalendarItem;
    const detailKey = selectedCalendarDetailKey;

    async function loadCalendarSpotlight() {
      setCalendarSpotlight(null);
      setCalendarSpotlightLoading(true);
      try {
        if (item.media_type === "movie") {
          const result = await getMovieDetail(item.item_id);
          if (!stopped && result.ok) {
            setCalendarSpotlightCache((prev) => ({ ...prev, [detailKey]: result }));
            setCalendarSpotlight(result);
          } else if (!stopped) {
            setCalendarSpotlight(null);
          }
          return;
        }

        const seriesId = item.series_id;
        if (!seriesId) {
          if (!stopped) setCalendarSpotlight(null);
          return;
        }

        const result = await getSeriesDetail(seriesId);
        if (!stopped && result.ok) {
          setCalendarSpotlightCache((prev) => ({ ...prev, [detailKey]: result }));
          setCalendarSpotlight(result);
        } else if (!stopped) {
            setCalendarSpotlight(result.ok ? result : null);
          }
      } catch {
        if (!stopped) {
          setCalendarSpotlight(null);
        }
      } finally {
        if (!stopped) {
          setCalendarSpotlightLoading(false);
        }
      }
    }

    loadCalendarSpotlight();
    return () => {
      stopped = true;
    };
  }, [calendarSpotlightCache, currentTab, selectedCalendarDetailKey, selectedCalendarItem]);

  useEffect(() => {
    setTitleSearchOpen(false);
    setTitleSearchIndex(0);
  }, [location.pathname]);

  useEffect(() => {
    if (titleSearchIndex >= titleSearchResults.length) {
      setTitleSearchIndex(0);
    }
  }, [titleSearchIndex, titleSearchResults.length]);

  useEffect(() => {
    if (!onboardingVisible) return;
    setOnboardingStepIndex((i) => Math.min(i, WIZARD_STEPS.length - 1));
  }, [onboardingVisible]);

  useEffect(() => {
    if (setupStatus?.setup_complete !== false) return;
    if (settingsPayload) return;
    // `/setup` bootstrap uses `loadSettings` from the tab refresh loop — avoid a duplicate first fetch.
    if (location.pathname === "/setup" || location.pathname.startsWith("/setup/")) return;

    let stopped = false;
    loadSettings(stopped).catch(() => {
      // Keep the current page visible; periodic refresh will retry loading settings.
    });

    return () => {
      stopped = true;
    };
  }, [settingsPayload, setupStatus?.setup_complete, location.pathname]);

  useEffect(() => {
    if (loading || !setupStatus) return;
    if (setupStatus.setup_complete) return;
    if (location.pathname === "/setup" || location.pathname.startsWith("/setup/")) return;
    navigate("/setup", { replace: true });
  }, [loading, setupStatus, location.pathname, navigate]);

  useEffect(() => {
    if (currentTab !== "settings") return;
    if (!settingsPayload) return;
    if (location.pathname === "/settings" || location.pathname === "/settings/") {
      navigate(firstSettingsPath, { replace: true });
      return;
    }
    const slug = location.pathname.split("/")[2] || "";
    const matched = settingsSectionNames.find((name) => SETTINGS_SECTION_SLUGS[name] === slug);
    if (!matched) {
      navigate(firstSettingsPath, { replace: true });
      return;
    }
    if (matched !== activeSettingsSection) {
      setActiveSettingsSection(matched);
    }
  }, [activeSettingsSection, currentTab, firstSettingsPath, location.pathname, navigate, settingsPayload, settingsSectionNames]);

  const libraryListShelf = getLibraryListShelf(location.pathname);

  const filteredLibrary = useMemo(() => {
    if (!libraryListShelf) return [];
    const shelfFilter = libraryListShelf === "tv" ? libraryShelfFilters.tv : libraryShelfFilters.movies;
    return library
      .filter((item) => {
        if (libraryListShelf === "movies" && item.type !== "movie") return false;
        if (libraryListShelf === "tv" && item.type !== "series") return false;
        if (shelfFilter === "placeholders") return item.has_placeholder;
        if (shelfFilter === "future") return item.is_future;
        if (shelfFilter === "missing") return item.has_missing;
        return true;
      })
      .sort((left, right) => titleSortKey(left.title).localeCompare(titleSortKey(right.title)));
  }, [library, libraryListShelf, libraryShelfFilters]);

  const visibleLogs = useMemo(() => {
    const filter = logFilter.trim().toLowerCase();
    if (!filter) return logs;
    return logs.filter((line) => line.toLowerCase().includes(filter));
  }, [logFilter, logs]);

  const calendarSummary = useMemo(() => {
    if (!calendar) {
      return { movieCount: 0, episodeCount: 0, inWindowCount: 0 };
    }

    let movieCount = 0;
    let episodeCount = 0;
    let inWindowCount = 0;

    calendar.weeks.forEach((week) => {
      week.forEach((day) => {
        day.items.filter((item) => isCalendarItemVisible(item, calendarFilters)).forEach((item) => {
          if (item.media_type === "movie") movieCount += 1;
          else episodeCount += 1;
          if (item.in_lookahead_window) inWindowCount += 1;
        });
      });
    });

    return { movieCount, episodeCount, inWindowCount };
  }, [calendar, calendarFilters]);

  async function loadSettings(stopped: boolean) {
    const payload = await getSettingsCurrent();
    if (stopped) return;

    setSettingsPayload(payload);
    setSetupStatus(payload.status);
    setOnboardingVisible(!payload.status.setup_complete);

    const nextValues: FieldValueMap = {};
    payload.sections.forEach((section) => {
      section.fields.forEach((field) => {
        nextValues[field.key] = field.value;
      });
    });

    setFieldValues(nextValues);
    setBaselineValues(nextValues);

    const sections = SETTINGS_SECTION_ORDER.filter((name) => payload.sections.some((s) => s.name === name));
    if (sections.length > 0 && !sections.includes(activeSettingsSection)) {
      const slug = location.pathname.split("/")[2] || "";
      const matched = sections.find((name) => SETTINGS_SECTION_SLUGS[name] === slug);
      setActiveSettingsSection(matched || sections[0]);
    }
  }

  /** Load `/api/settings/current` as soon as the Settings tab is opened — do not wait for the next 5s poll tick (avoids a blank main pane when settings were never fetched while on Activity). */
  useEffect(() => {
    if (currentTab !== "settings") return;
    if (settingsPayload) return;
    if (hasUnsavedChangesRef.current) return;
    void loadSettings(false).catch(() => {
      /* Errors surface via dashboard refresh / error banner; poll will retry. */
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadSettings omitted: new function identity each render would retrigger while payload is null.
  }, [currentTab, settingsPayload]);

  async function handlePartialSave(result: any, partialValues: Record<string, unknown>) {
    if (!result) return;
    if (!result.ok) {
      const first = Object.entries(result.errors || {})[0];
      setSettingsFeedback(first ? `${first[0]}: ${first[1]}` : "Unable to save settings");
      setSettingsFeedbackKind("error");
      return;
    }
    // Merge partial values into baseline so UI no longer flags them as unsaved
    setBaselineValues((prev) => ({ ...prev, ...partialValues }));
    const payload = await getSettingsCurrent();
    setSettingsPayload(payload);
    setSetupStatus(payload.status);
    const restartKeys = result.restart_required_keys || [];
    setSettingsFeedback(restartKeys.length ? `Saved. Restart recommended for: ${restartKeys.join(", ")}` : "Saved.");
    setSettingsFeedbackKind("success");
  }

  function tryNavigate(path: string) {
    const stayingWithinSettings = currentTab === "settings" && path.startsWith("/settings");
    if (!hasUnsavedChanges || currentTab !== "settings" || stayingWithinSettings) {
      navigate(path);
      return;
    }
    const shouldLeave = window.confirm("You have unsaved settings changes. Leave this section without saving?");
    if (shouldLeave) {
      navigate(path);
    }
  }

  function getActiveScrollTop() {
    const container = contentScrollRef.current;
    if (container) return container.scrollTop;
    return window.scrollY || 0;
  }

  function setActiveScrollTop(top: number) {
    const nextTop = Number.isFinite(top) ? Math.max(0, top) : 0;
    const container = contentScrollRef.current;
    if (container) {
      container.scrollTop = nextTop;
      return;
    }
    window.scrollTo(0, nextTop);
  }

  function openLibraryDetail(item: { type: "movie" | "series"; item_id: number; title?: string }) {
    if (getLibraryListShelf(location.pathname) !== null) {
      const currentScrollTop = getActiveScrollTop();
      sessionStorage.setItem("libraryScrollTop", String(currentScrollTop));
      sessionStorage.setItem("libraryScrollRestorePending", "1");
    }
    navigate(`/library/${item.type}/${item.item_id}`);
    setTitleSearchOpen(false);
  }

  function openCalendarItemDetail(item: CalendarDay["items"][number]) {
    if (item.media_type === "movie") {
      openLibraryDetail({ type: "movie", item_id: item.item_id, title: item.title });
      return;
    }

    const seriesId = item.series_id;
    if (!seriesId) return;
    openLibraryDetail({ type: "series", item_id: seriesId, title: item.title });
  }

  function handleCalendarSelect(itemId: string) {
    setCalendarSelectedId(itemId);
    // Open the inline overlay by default (not the full side pane)
    setCalendarSpotlightOpen(false);
  }

  function selectSearchResult(index: number) {
    const result = titleSearchResults[index];
    if (!result) return;
    openLibraryDetail({ type: result.type, item_id: result.item_id, title: result.title });
  }

  useEffect(() => {
    if (getLibraryListShelf(location.pathname) === null) return;
    if (sessionStorage.getItem("libraryScrollRestorePending") !== "1") return;

    const rawTop = sessionStorage.getItem("libraryScrollTop");
    const savedTop = rawTop == null ? 0 : Number(rawTop);

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        setActiveScrollTop(savedTop);
      });
    });

    sessionStorage.removeItem("libraryScrollRestorePending");
  }, [location.pathname, library.length]);

  useEffect(() => {
    const isDetailRoute = location.pathname.startsWith("/library/") && (location.pathname.includes("/movie/") || location.pathname.includes("/series/"));
    if (!isDetailRoute) return;

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const container = contentScrollRef.current;
        if (container) container.scrollTop = 0;
        window.scrollTo(0, 0);
        document.documentElement.scrollTop = 0;
        document.body.scrollTop = 0;
      });
    });
  }, [location.pathname]);

  function renderTabBody() {
    // Settings has its own loading UI (`SettingsPanel` when payload is null). Do not gate it on the global
    // bootstrap flag — otherwise cold loads on `/settings/...` or slow first refresh can show an empty main area.
    if (loading && currentTab !== "settings") {
      return (
        <div
          className={`min-h-[40vh] flex items-center justify-center text-sm font-headline uppercase tracking-widest ${
            themeMode === "light" ? "text-slate-600" : "text-slate-400"
          }`}
        >
          Loading dashboard data...
        </div>
      );
    }

    if (location.pathname.startsWith("/library/") && (location.pathname.includes("/movie/") || location.pathname.includes("/series/"))) {
      return <DetailRoutePage brand={brand} themeMode={themeMode} scrollContainerRef={contentScrollRef} />;
    }

    const openLibraryWithFilter = (filter: LibraryFilter) => {
      if (filter === "movie") {
        setLibraryShelfFilters((prev) => ({ ...prev, movies: "all" }));
        navigate(LIBRARY_MOVIES_PATH);
        return;
      }
      if (filter === "series") {
        setLibraryShelfFilters((prev) => ({ ...prev, tv: "all" }));
        navigate(LIBRARY_TV_PATH);
        return;
      }
      if (filter === "all" || filter === "placeholders" || filter === "future" || filter === "missing") {
        setLibraryShelfFilters((prev) => ({ ...prev, movies: filter }));
        navigate(LIBRARY_MOVIES_PATH);
      }
    };

    if (currentTab === "activity") return <ActivityPanel rows={activity} placeholderRows={placeholderActivity} activityTab={activityTab} onActivityTabChange={setActivityTab} stats={stats} brand={brand} themeMode={themeMode} onOpenLibraryFilter={openLibraryWithFilter} />;

    if (currentTab === "library") {
      const shelf = libraryListShelf;
      if (shelf === "movies" || shelf === "tv") {
        return (
          <LibraryPanel
            shelfTitle={shelf === "tv" ? "TV Library" : "Movies"}
            items={filteredLibrary}
            activeFilter={shelf === "tv" ? libraryShelfFilters.tv : libraryShelfFilters.movies}
            onFilterChange={(value) => {
              if (shelf === "tv") {
                setLibraryShelfFilters((prev) => ({ ...prev, tv: value }));
              } else {
                setLibraryShelfFilters((prev) => ({ ...prev, movies: value }));
              }
            }}
            onOpenDetail={(item) => openLibraryDetail({ type: item.type, item_id: item.item_id, title: item.title })}
            brand={brand}
            themeMode={themeMode}
          />
        );
      }
      return <Navigate to={LIBRARY_MOVIES_PATH} replace />;
    }

    if (currentTab === "calendar") {
      return (
        <CalendarPanel
          payload={calendar}
          month={calendarMonth}
          summary={calendarSummary}
          filters={calendarFilters}
          selectedItem={selectedCalendarItem}
          spotlight={calendarSpotlight}
          spotlightOpen={calendarSpotlightOpen}
          spotlightLoading={calendarSpotlightLoading}
          brand={brand}
          themeMode={themeMode}
          onMonthChange={setCalendarMonth}
          onSelectItem={handleCalendarSelect}
          onOpenSpotlightDetail={openCalendarItemDetail}
          onToggleSpotlight={setCalendarSpotlightOpen}
          onToggleFilter={(group, key) => {
            setCalendarFilters((prev) => ({
              ...prev,
              [group]: {
                ...prev[group],
                [key]: !prev[group][key],
              },
            }));
          }}
        />
      );
    }

    if (currentTab === "errors") return <ErrorsPanel rows={errors} brand={brand} themeMode={themeMode} />;

    if (currentTab === "logs") {
      return (
        <LogsPanel
          lines={visibleLogs}
          logFile={logFile}
          logLevel={logLevel}
          logFilter={logFilter}
          brand={brand}
          themeMode={themeMode}
          onLevelChange={setLogLevel}
          onFilterChange={setLogFilter}
        />
      );
    }

    if (currentTab === "setup") {
      return <div className="empty">Setup is handled on the /setup route.</div>;
    }

    if (currentTab === "settings") {
      return (
        <SettingsPanel
          payload={settingsPayload}
          activeSection={activeSettingsSection}
          values={fieldValues}
          hasUnsavedChanges={hasUnsavedChanges}
          feedback={settingsFeedback}
          feedbackKind={settingsFeedbackKind}
          brand={brand}
          themeMode={themeMode}
          onValueChange={(key, value) => setFieldValues((prev) => ({ ...prev, [key]: value }))}
          onSave={async () => {
            setSettingsFeedback("Saving...");
            setSettingsFeedbackKind("");
            const result = await saveSettings(buildPersistableSettingsValues(fieldValues, settingsPayload));
            if (!result.ok) {
              const first = Object.entries(result.errors || {})[0];
              setSettingsFeedback(first ? `${first[0]}: ${first[1]}` : "Unable to save settings");
              setSettingsFeedbackKind("error");
              return;
            }
            setBaselineValues(fieldValues);
            const restartKeys = result.restart_required_keys || [];
            setSettingsFeedback(restartKeys.length ? `Saved. Restart recommended for: ${restartKeys.join(", ")}` : "Saved and applied.");
            setSettingsFeedbackKind("success");
            const payload = await getSettingsCurrent();
            setSettingsPayload(payload);
            setSetupStatus(payload.status);
            setOnboardingVisible(!payload.status.setup_complete);
          }}
          onTestConnection={async ({ service, urlKey, credentialKey }) => {
            const url = String(fieldValues[urlKey] || "").trim();
            const credential = String(fieldValues[credentialKey] || "").trim();
            return testIntegrationConnection({ service, url, credential });
          }}
        />
      );
    }

    return <div className="empty">Unknown route.</div>;
  }

  // Body class for global studio chrome (Placeholdarr light/dark)
  useEffect(() => {
    document.body.className = themeMode === "light" ? "theme-studio-light" : "theme-studio-dark";
  }, [themeMode]);

  useEffect(() => {
    try {
      localStorage.setItem(STUDIO_THEME_MODE_STORAGE_KEY, themeMode);
    } catch {
      /* ignore */
    }
  }, [themeMode]);

  const setupRouteActive = location.pathname === "/setup" || location.pathname.startsWith("/setup/");
  if (setupRouteActive) {
    const setupShellClass = `brand-theme-scope theme-${themeMode} layout-${brand}-${themeMode} min-h-screen flex items-center justify-center font-brand-body text-sm font-headline tracking-wide ${themeMode === "light" ? "text-slate-700" : "text-slate-300"}`;
    if (setupStatus?.setup_complete) {
      return <Navigate to="/activity" replace />;
    }
    if (loading || !setupStatus || !settingsPayload) {
      return (
        <SetupBootShell
          setupShellClass={setupShellClass}
          surfaceStyle={setupLoadingShellStyle}
          brand={brand}
          accentHex={brandAccent.hex}
          appLabel={brandMeta.label}
          errorMessage={errorMessage}
        />
      );
    }
    return (
      <OnboardingWizard
        payload={settingsPayload}
        stepIndex={onboardingStepIndex}
        values={fieldValues}
        hasUnsavedChanges={hasUnsavedChanges}
        brand={brand}
        themeMode={themeMode}
        onBack={() => setOnboardingStepIndex((i) => Math.max(0, i - 1))}
        onNext={() => setOnboardingStepIndex((i) => Math.min(WIZARD_STEPS.length - 1, i + 1))}
        onPartialSave={handlePartialSave}
        onChange={(key, value) => setFieldValues((prev) => ({ ...prev, [key]: value }))}
        onTestConnection={async ({ service, urlKey, credentialKey }) => {
          const url = String(fieldValues[urlKey] || "").trim();
          const credential = String(fieldValues[credentialKey] || "").trim();
          return testIntegrationConnection({ service, url, credential });
        }}
        onSave={async () => {
          setSettingsFeedback("Saving...");
          setSettingsFeedbackKind("");
          const result = await saveSettings(buildPersistableSettingsValues(fieldValues, settingsPayload));
          if (!result.ok) {
            const first = Object.entries(result.errors || {})[0];
            setSettingsFeedback(first ? `${first[0]}: ${first[1]}` : "Unable to save settings");
            setSettingsFeedbackKind("error");
            return;
          }
          setBaselineValues(fieldValues);
          setSettingsFeedback("Saved and applied.");
          setSettingsFeedbackKind("success");
          const payload = await getSettingsCurrent();
          setSettingsPayload(payload);
          setSetupStatus(payload.status);
          setOnboardingVisible(!payload.status.setup_complete);
        }}
      />
    );
  }

  // Studio shell (Placeholdarr layout; light vs dark via themeMode only)
  const isActive = (path: string) =>
    location.pathname === path || location.pathname.startsWith(`${path}/`);
  const isStudioGlass = themeMode !== "light";
  const studioLightChrome = {
    page: brandSemantic.chromePage,
    sidebar: brandSemantic.chromeSidebar,
    header: brandSemantic.chromeHeader,
    main: brandSemantic.chromeMain,
    border: brandSemantic.border,
  };
  const brandVarStyle = semanticTokensToCssVars(brandSemantic) as CSSProperties;
  const topBarDivider = isStudioGlass
    ? alphaColor("#0f172a", 0.22)
    : alphaColor("#94a3b8", 0.45);
  /** Matches Studio top-bar search/dropdown strip. */
  const studioTopBarBlue = "#1e2430";
  const studioHeaderBackground = isStudioGlass
    ? `linear-gradient(to right, ${brandSemantic.topBarBand} 0%, ${brandSemantic.topBarBand} 50%, ${studioTopBarBlue} 80%, ${studioTopBarBlue} 100%)`
    : `linear-gradient(to right, ${studioTopBarBlue} 0%, ${studioTopBarBlue} 52%, ${brandSemantic.topBarBand} 84%, ${brandSemantic.topBarBand} 100%)`;

  return (
      <div
        className={`brand-theme-scope theme-${themeMode} layout-${brand}-${themeMode} flex h-screen overflow-hidden font-brand-body ${isStudioGlass ? "text-slate-100" : "text-slate-900"}`}
        style={{
          ...brandVarStyle,
          ...(isStudioGlass ? {
            backgroundImage: getStudioDarkBackdrop(brand, brandAccent, brandSemantic),
          } : { backgroundColor: studioLightChrome.page }),
        }}
      >
        {/* Sidebar */}
        <aside
          className={`hidden md:flex flex-col h-full w-64 z-20 flex-shrink-0 pb-6 pt-0 ${isStudioGlass ? "bg-white/8 backdrop-blur-2xl border-r" : "shadow-[12px_0_24px_rgba(40,42,48,0.10)]"}`}
          style={isStudioGlass ? { borderRightWidth: 1, borderRightStyle: "solid", borderRightColor: brandSemantic.glassBorder } : { backgroundColor: studioLightChrome.sidebar, borderRightWidth: 1, borderRightStyle: "solid", borderRightColor: studioLightChrome.border }}
        >
          <div
            className="flex h-16 w-full shrink-0 items-center justify-center border-b px-3"
            style={{ backgroundColor: isStudioGlass ? brandSemantic.topBarBand : studioTopBarBlue, borderBottomColor: topBarDivider }}
          >
            <BrandLogo
              brand={brand}
              accentHex={brandAccent.hex}
              variant={isStudioGlass ? "blue" : "yellow"}
              className="max-h-[52px] w-full max-w-[13.5rem] object-contain object-center"
            />
          </div>

          {/* Nav */}
          <nav className="flex-1 space-y-1 font-brand-label pt-4">
            {(() => {
              const navActiveClass =
                "flex items-center w-full px-6 py-3 gap-4 font-brand-label text-sm uppercase tracking-widest transition-all duration-200 border-l-4";
              const navInactiveClass =
                "flex items-center w-full px-6 py-3 gap-4 transition-all duration-200 font-brand-label text-sm uppercase tracking-widest group " +
                (isStudioGlass
                  ? "text-slate-400 hover:text-slate-100 hover:bg-[color:var(--brand-nav-hover)]"
                  : "text-slate-600 hover:text-slate-900 hover:bg-[color:var(--brand-nav-hover)]");
              const navActiveStyle = (
                isStudioGlass
                  ? {
                      backgroundColor: alphaColor(brandSemantic.accent, 0.22),
                      color: brandSemantic.fg,
                      borderLeftColor: brandSemantic.accent,
                    }
                  : {
                      backgroundColor: brandSemantic.fg,
                      color: brandSemantic.accent,
                      borderLeftColor: brandSemantic.accent,
                    }
              ) as const;
              const librarySectionActive = isActive("/library");
              const moviesSubActive =
                location.pathname === LIBRARY_MOVIES_PATH ||
                location.pathname === `${LIBRARY_MOVIES_PATH}/` ||
                location.pathname.startsWith("/library/movie/");
              const tvSubActive = location.pathname === LIBRARY_TV_PATH || location.pathname.startsWith("/library/series/");
              const subBase =
                "flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-[11px] font-headline uppercase tracking-wider transition-colors ";
              const subActiveClass = isStudioGlass
                ? "bg-[#1e2430] text-slate-100"
                : "bg-[color:var(--brand-fg)] text-[color:var(--brand-accent)]";
              const subInactiveClass = isStudioGlass
                ? "text-slate-400 hover:bg-[#1e2430]/50 hover:text-slate-200"
                : "text-slate-600 hover:bg-slate-100 hover:text-slate-900";

              return (
                <>
                  {isActive("/activity") ? (
                    <button type="button" onClick={() => tryNavigate("/activity")} className={navActiveClass} style={navActiveStyle}>
                      <span className="material-symbols-outlined">analytics</span>
                      <span>Activity</span>
                    </button>
                  ) : (
                    <button type="button" onClick={() => tryNavigate("/activity")} className={navInactiveClass}>
                      <span className="material-symbols-outlined transition-transform group-hover:translate-x-1">analytics</span>
                      <span>Activity</span>
                    </button>
                  )}

                  {librarySectionActive ? (
                    <button type="button" onClick={() => tryNavigate(LIBRARY_MOVIES_PATH)} className={navActiveClass} style={navActiveStyle}>
                      <span className="material-symbols-outlined">movie_filter</span>
                      <span>Library</span>
                    </button>
                  ) : (
                    <button type="button" onClick={() => tryNavigate(LIBRARY_MOVIES_PATH)} className={navInactiveClass}>
                      <span className="material-symbols-outlined transition-transform group-hover:translate-x-1">movie_filter</span>
                      <span>Library</span>
                    </button>
                  )}
                  {currentTab === "library" ? (
                    <div className="mt-1 space-y-0.5 pl-6 pr-3">
                      <button
                        type="button"
                        onClick={() => tryNavigate(LIBRARY_MOVIES_PATH)}
                        className={subBase + (moviesSubActive ? subActiveClass : subInactiveClass)}
                      >
                        <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                          movie
                        </span>
                        <span className="truncate">Movies</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => tryNavigate(LIBRARY_TV_PATH)}
                        className={subBase + (tvSubActive ? subActiveClass : subInactiveClass)}
                      >
                        <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                          tv_gen
                        </span>
                        <span className="truncate">TV</span>
                      </button>
                    </div>
                  ) : null}

                  {[
                    { icon: "calendar_month", label: "Calendar", path: "/calendar" },
                    { icon: "error", label: "Errors", path: "/errors" },
                    { icon: "terminal", label: "Logs", path: "/logs" },
                    { icon: "settings", label: "Settings", path: "/settings" },
                  ].map(({ icon, label, path }) =>
                    isActive(path) ? (
                      <button key={path} type="button" onClick={() => tryNavigate(path === "/settings" ? firstSettingsPath : path)} className={navActiveClass} style={navActiveStyle}>
                        <span className="material-symbols-outlined">{icon}</span>
                        <span>{label}</span>
                      </button>
                    ) : (
                      <button key={path} type="button" onClick={() => tryNavigate(path === "/settings" ? firstSettingsPath : path)} className={navInactiveClass}>
                        <span className="material-symbols-outlined transition-transform group-hover:translate-x-1">{icon}</span>
                        <span>{label}</span>
                      </button>
                    ),
                  )}
                </>
              );
            })()}
            {currentTab === "settings" && settingsSectionNames.length ? (
              <div className="mt-1 space-y-0.5 pl-6 pr-3">
                {settingsSectionNames.map((name) => {
                  const subPath = `/settings/${SETTINGS_SECTION_SLUGS[name] ?? ""}`;
                  const isSubActive = location.pathname === subPath;
                  return (
                    <button
                      key={name}
                      type="button"
                      onClick={() => tryNavigate(subPath)}
                      className={`flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-[11px] font-headline uppercase tracking-wider transition-colors ${
                        isStudioGlass
                          ? isSubActive
                            ? "bg-[#1e2430] text-slate-100"
                            : "text-slate-400 hover:bg-[#1e2430]/50 hover:text-slate-200"
                          : isSubActive
                            ? "bg-[color:var(--brand-fg)] text-[color:var(--brand-accent)]"
                            : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                      }`}
                    >
                      <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                        {SETTINGS_SECTION_ICONS[name] || "settings"}
                      </span>
                      <span className="truncate">{name}</span>
                    </button>
                  );
                })}
              </div>
            ) : null}
          </nav>

        </aside>

        {/* Main area */}
        <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {/* Topbar */}
          <header
            className="flex justify-between items-center w-full px-6 py-3 h-16 z-10 flex-shrink-0 border-b"
            style={{ backgroundImage: studioHeaderBackground, borderBottomColor: topBarDivider }}
          >
            <div className="flex items-center flex-1 max-w-xl">
              <div
                className="relative w-full max-w-lg"
                onBlur={() => {
                  window.setTimeout(() => setTitleSearchOpen(false), 120);
                }}
              >
                <span className={`material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-lg pointer-events-none ${isStudioGlass ? "text-slate-400" : "text-slate-400"}`}>search</span>
                <input
                  value={titleSearch}
                  onChange={(e) => {
                    setTitleSearch(e.target.value);
                    setTitleSearchIndex(0);
                    setTitleSearchOpen(true);
                  }}
                  onFocus={() => setTitleSearchOpen(true)}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowDown") {
                      event.preventDefault();
                      if (titleSearchResults.length > 0) {
                        setTitleSearchOpen(true);
                        setTitleSearchIndex((prev) => (prev + 1) % titleSearchResults.length);
                      }
                    } else if (event.key === "ArrowUp") {
                      event.preventDefault();
                      if (titleSearchResults.length > 0) {
                        setTitleSearchOpen(true);
                        setTitleSearchIndex((prev) => (prev - 1 + titleSearchResults.length) % titleSearchResults.length);
                      }
                    } else if (event.key === "Enter") {
                      const index = titleSearchResults.length ? titleSearchIndex : 0;
                      if (titleSearchResults[index]) {
                        event.preventDefault();
                        selectSearchResult(index);
                      }
                    } else if (event.key === "Escape") {
                      setTitleSearchOpen(false);
                    }
                  }}
                  className={`w-full rounded-lg py-2 pl-10 pr-4 text-sm border focus:outline-none placeholder-slate-500 ${isStudioGlass ? `border-[#2a3444] bg-[#1e2430] text-slate-100 ${getBrandFocusClass(brand, themeMode)}` : `bg-white text-slate-900 border-[#cddbeb] ${getBrandFocusClass(brand, themeMode)}`}`}
                  placeholder="Search titles, series, and movies..."
                />
                {titleSearchOpen && titleSearch.trim() ? (
                  <div className={`absolute top-[calc(100%+0.5rem)] left-0 right-0 overflow-hidden rounded-xl border shadow-2xl ${isStudioGlass ? "bg-[#0f1726]/90 border-white/15 backdrop-blur-2xl" : "border-[#cddbeb] bg-white"}`}>
                    {titleSearchResults.length ? (
                      <div className="max-h-96 overflow-y-auto p-2">
                        {titleSearchResults.map((item, index) => (
                          <button
                            key={item.id}
                            type="button"
                            onMouseDown={() => selectSearchResult(index)}
                            className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors ${
                              index === titleSearchIndex ? (isStudioGlass ? getBrandSelectionClass(brand, themeMode) : "") : (isStudioGlass ? "hover:bg-[#1e2430]" : "hover:bg-[#f2f7ff]")
                            }`}
                            style={index === titleSearchIndex && !isStudioGlass ? { backgroundColor: alphaColor(brandAccent.hex, 0.15) } : undefined}
                          >
                            <div className={`flex h-11 w-8 flex-none overflow-hidden rounded-md border ${isStudioGlass ? "bg-[#1e2430] border-[#424753]/30" : "bg-[#eef4fb] border-[#d7e2f0]"}`}>
                              {item.poster_url ? (
                                <img src={item.poster_url} alt="" className="h-full w-full object-cover" />
                              ) : (
                                <div className="flex h-full w-full items-center justify-center text-[10px] font-headline uppercase text-slate-500">
                                  {item.type === "movie" ? "MOV" : "SER"}
                                </div>
                              )}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className={`truncate text-sm font-semibold ${isStudioGlass ? "text-white" : "text-slate-900"}`}>{item.title}</div>
                              <div className={`mt-0.5 flex items-center gap-2 text-[11px] ${isStudioGlass ? "text-slate-400" : "text-slate-500"}`}>
                                <span className="font-headline uppercase tracking-wider">{item.type}</span>
                                <span>{item.year || "--"}</span>
                                {item.has_placeholder ? <span className="text-teal-300">Placeholder</span> : null}
                                {item.has_missing ? <span className="text-red-300">Missing</span> : null}
                              </div>
                            </div>
                            <span className="material-symbols-outlined text-slate-500" style={{ fontSize: 16 }}>north_west</span>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <div className="px-4 py-3 text-sm text-slate-500">No matching titles found.</div>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
            <div className="flex min-w-0 flex-1 items-center justify-end gap-3 ml-4">
              <div
                className={`hidden min-w-0 truncate text-right text-[10px] font-headline uppercase tracking-widest sm:block ${isStudioGlass ? "text-slate-500" : "text-slate-500"}`}
                title="Dashboard poll status (slower when the tab is in the background)"
                aria-live="polite"
              >
                {dashboardRefreshing ? (
                  <span className="inline-flex items-center gap-1.5">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full" style={{ backgroundColor: brandAccent.hex }} />
                    Syncing
                  </span>
                ) : lastDashboardSuccessAt != null ? (
                  <span>
                    Live ·{" "}
                    {(() => {
                      void dashboardAgeTick;
                      return formatDashboardDataAge(Date.now() - lastDashboardSuccessAt);
                    })()}
                  </span>
                ) : null}
              </div>
              <button
                type="button"
                onClick={() => setThemeMode((m) => (m === "dark" ? "light" : "dark"))}
                className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border outline-none transition-colors ${isStudioGlass ? `border-[#2a3444] bg-[#1e2430] text-amber-200/95 hover:bg-[#252e3a] ${getBrandFocusClass(brand, themeMode)}` : `border-transparent text-slate-900 hover:brightness-[0.97] ${getBrandFocusClass(brand, themeMode)}`}`}
                style={!isStudioGlass ? { backgroundColor: brandSemantic.topBarBand } : undefined}
                aria-label={themeMode === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                title={themeMode === "dark" ? "Light mode" : "Dark mode"}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 22 }}>
                  {themeMode === "dark" ? "light_mode" : "dark_mode"}
                </span>
              </button>
            </div>
          </header>

          {/* Setup banner */}
          {!setupStatus?.setup_complete && !showReconnectPanel ? (
            <div className="border-b border-l-4 px-6 py-3 text-sm"
              style={{
                backgroundColor: alphaColor(brandSemantic.accent2, isStudioGlass ? 0.14 : 0.1),
                borderColor: alphaColor(brandSemantic.accent, isStudioGlass ? 0.38 : 0.28),
                borderLeftColor: brandSemantic.accent,
                color: isStudioGlass ? brandSemantic.fg : brandSemantic.fgMuted,
              }}
            >
              Onboarding required. Complete the setup wizard to unlock the full dashboard flow.
            </div>
          ) : null}

          {/* Content */}
          <main
            ref={(el) => { contentScrollRef.current = el; }}
            className={`flex-1 overflow-y-auto p-6 ${isStudioGlass ? "bg-transparent" : ""}`}
            style={!isStudioGlass ? { backgroundColor: studioLightChrome.main } : undefined}
          >
            {showReconnectPanel ? (
              <section
                className={`rounded-xl border p-8 md:p-12 ${
                  isStudioGlass
                    ? "border-cyan-500/30 bg-[#0f1520]/80 text-slate-100"
                    : "border-cyan-200 bg-cyan-50 text-slate-900"
                }`}
              >
                <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
                  <div className="relative mb-6 h-16 w-16">
                    <div
                      className={`absolute inset-0 rounded-full border-2 ${
                        isStudioGlass ? "border-cyan-400/30" : "border-cyan-400/40"
                      }`}
                    />
                    <div
                      className={`absolute inset-0 rounded-full border-2 border-t-transparent animate-spin ${
                        isStudioGlass ? "border-cyan-300" : "border-cyan-600"
                      }`}
                    />
                  </div>
                  <h2 className="text-xl font-semibold md:text-2xl">Reconnecting to Placeholdarr</h2>
                  <p className={`mt-3 max-w-2xl text-sm md:text-base ${isStudioGlass ? "text-slate-300" : "text-slate-700"}`}>
                    Live dashboard data is temporarily unavailable. We are automatically retrying the API connection.
                  </p>
                  <div className="mt-6 flex items-center gap-2">
                    <span className={`inline-block h-2.5 w-2.5 rounded-full animate-pulse ${isStudioGlass ? "bg-cyan-300" : "bg-cyan-600"}`} />
                    <span className={`inline-block h-2.5 w-2.5 rounded-full animate-pulse [animation-delay:150ms] ${isStudioGlass ? "bg-cyan-300/80" : "bg-cyan-600/80"}`} />
                    <span className={`inline-block h-2.5 w-2.5 rounded-full animate-pulse [animation-delay:300ms] ${isStudioGlass ? "bg-cyan-300/60" : "bg-cyan-600/60"}`} />
                  </div>
                  <p className={`mt-6 text-xs ${isStudioGlass ? "text-slate-400" : "text-slate-500"}`}>
                    This panel will close automatically once the connection is restored.
                  </p>
                </div>
              </section>
            ) : (
              renderTabBody()
            )}
          </main>

          {errorMessage && !showReconnectPanel ? (
            <div
              className={`mx-6 mb-4 rounded-lg border p-3 text-sm ${
                isStudioGlass
                  ? "border-red-500/40 bg-red-900/30 text-red-300"
                  : "border-red-200 bg-red-50 text-red-800"
              }`}
            >
              {errorMessage}
            </div>
          ) : null}
        </div>

        <Routes>
          <Route path="/" element={<Navigate to={defaultLandingPath} replace />} />
          <Route path="*" element={null} />
        </Routes>
      </div>
  );
}

function StatCard(props: { title: string; value: number | undefined; sub: string; accent?: BrandAccent; themeMode?: ThemeMode; onClick?: () => void }) {
  const accent = props.accent ?? { label: "Placeholdarr", hex: "#FBBF24", text: "#E2E8F0", icon: "#CBD5E1", hoverHex: "#d97706" };
  const isLight = props.themeMode === "light";
  const [hover, setHover] = useState(false);
  const yellow = accent.hex;
  const cyan = accent.icon;
  /** Light mode: one branded slate rail + frame (matches title color), not mixed yellow/cyan borders. */
  const lightFrame = accent.text;
  const baseStyle: React.CSSProperties = isLight
    ? {
        borderLeft: `6px solid ${lightFrame}`,
        borderTop: `2px solid ${lightFrame}`,
        borderBottom: `2px solid ${lightFrame}`,
        borderRight: `1px solid ${lightFrame}`,
        background: undefined,
        paddingLeft: 12,
        transition: "transform 0.18s ease, box-shadow 0.18s ease",
        cursor: props.onClick ? "pointer" : undefined,
      }
    : {
        borderLeft: `6px solid ${yellow}`,
        borderTop: `2px solid ${yellow}`,
        borderBottom: `2px solid ${yellow}`,
        borderRight: `1px solid ${yellow}`,
        background: undefined,
        paddingLeft: 12,
        transition: "transform 0.18s ease, box-shadow 0.18s ease",
        cursor: props.onClick ? "pointer" : undefined,
      };

  const hoverStyle: React.CSSProperties = hover
    ? isLight
      ? {
          transform: "translateY(-6px)",
          boxShadow: `0 14px 36px ${alphaColor(lightFrame, 0.16)}, 0 6px 20px ${alphaColor(lightFrame, 0.1)}`,
        }
      : { transform: "translateY(-6px)", boxShadow: `0 12px 36px ${alphaColor(yellow, 0.22)}` }
    : {};

  return (
    <article
      className="stat-card"
      style={{ ...baseStyle, ...hoverStyle }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={props.onClick}
    >
      <div className="stat-head">
        <div className="stat-value" style={{ color: yellow }}>{props.value ?? "--"}</div>
        <div className="stat-title" style={{ color: accent.text }}>{props.title}</div>
      </div>
      <div className="stat-sub" style={{ color: isLight ? alphaColor(cyan, 0.88) : alphaColor(yellow, 0.8) }}>{props.sub}</div>
    </article>
  );
}

function ActivityPanel(props: {
  rows: ActivityRow[];
  placeholderRows?: any[];
  activityTab?: "system" | "placeholders";
  onActivityTabChange?: (tab: "system" | "placeholders") => void;
  stats: StatsResponse | null;
  brand: Brand;
  themeMode: ThemeMode;
  onOpenLibraryFilter?: (f: LibraryFilter) => void;
}) {
  const s = props.stats;
  const accent = getBrandAccent(props.brand, props.themeMode);
  const semantic = getBrandSemanticTokens(props.brand, props.themeMode, accent);
  const isLight = props.themeMode === "light";
  const tab = props.activityTab || "system";
  const panelShellStyle: React.CSSProperties | undefined = isLight
    ? {
        borderColor: semantic.glassBorder,
        boxShadow: `0 14px 44px ${alphaColor(semantic.accentIce, 0.1)}`,
      }
    : undefined;
  const placeholderRows = props.placeholderRows || [];
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});
  const [placeholderHistoryExpanded, setPlaceholderHistoryExpanded] = useState<Record<string, boolean>>({});

  const rowKey = (row: ActivityRow, idx: number) => `${row.type}-${String((row as any).id ?? row.time ?? idx)}`;

  useEffect(() => {
    setExpandedRows((prev) => {
      const next = { ...prev };
      props.rows.forEach((row, idx) => {
        const jt = String((row as any).job_type || "");
        if (jt !== "full_sync_progress" && jt !== "queue_monitor_batch") {
          return;
        }
        const key = rowKey(row, idx);
        if (next[key] !== undefined) {
          return;
        }
        const status = String(row.status || "").toLowerCase();
        const isFinal = status === "done" || status === "success" || status === "failed";
        next[key] = !isFinal;
      });
      return next;
    });
  }, [props.rows]);

  // Check if there are any failures
  const failedCount = props.rows.filter(r => r.status === "FAILED").length;
  const hasFailures = failedCount > 0;

  // For placeholder tab: count active vs deleted
  const createdCount = placeholderRows.filter(r => r.action === "Created").length;
  const deletedCount = placeholderRows.filter(r => r.action === "Deleted").length;

  return (
    <div>
      {/* Status pill */}
      <div className="flex items-center gap-2 mb-4">
        <div className={`w-2 h-2 rounded-full ${tab === "system" && hasFailures ? "bg-red-500" : "bg-green-500"}`} />
        <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">
          {tab === "system"
            ? hasFailures
              ? `${failedCount} Issue${failedCount === 1 ? "" : "s"}`
              : "System Online"
            : `${createdCount} Created • ${deletedCount} Deleted`}
        </span>
      </div>

      {/* Alert banner if there are system failures */}
      {tab === "system" && hasFailures && (
        <div className="mb-6 p-4 rounded-lg border border-red-600/40 bg-red-900/20">
          <div className="flex items-start gap-3">
            <span className="material-symbols-outlined text-red-400 flex-shrink-0" style={{ fontSize: 20 }}>error</span>
            <div>
              <div className="font-headline text-sm font-bold text-red-300">Recent Failures Detected</div>
              <div className="text-xs text-red-200/80 mt-1">Check the list below or view logs for details.</div>
            </div>
          </div>
        </div>
      )}

      {/* Top stat cards (shown on both system and placeholder tabs) */}
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-6">
        <StatCard
          accent={accent}
          themeMode={props.themeMode}
          title="Movies"
          value={s?.movies.total}
          sub={`Downloaded ${s?.movies.downloaded ?? "--"} • Placeholders ${s?.movies.placeholders ?? "--"}`}
          onClick={() => props.onOpenLibraryFilter?.("movie")}
        />
        <StatCard accent={accent} themeMode={props.themeMode} title="Series" value={s?.series.total} sub="Tracked series" onClick={() => props.onOpenLibraryFilter?.("series")} />
        <StatCard accent={accent} themeMode={props.themeMode} title="Episodes" value={s?.episodes.total} sub={`Downloaded ${s?.episodes.downloaded ?? "--"} • Placeholders ${s?.episodes.placeholders ?? "--"}`} />
        <StatCard accent={accent} themeMode={props.themeMode} title="Placeholders" value={s?.placeholders_on_disk} sub="On disk" onClick={() => props.onOpenLibraryFilter?.("placeholders")} />
        <StatCard accent={accent} themeMode={props.themeMode} title="Jobs" value={s?.jobs.pending} sub={`Done ${s?.jobs.done ?? "--"} • Failed ${s?.jobs.failed ?? "--"}`} />
      </div>

      {/* Tab buttons */}
      <div
        className={`flex gap-2 mb-6 border-b pb-4 ${isLight ? "" : "border-[#424753]/30"}`}
        style={isLight ? { borderBottomColor: semantic.borderSubtle } : undefined}
      >
        <button
          type="button"
          onClick={() => props.onActivityTabChange?.("system")}
          className={`px-4 py-2 rounded-tl-lg rounded-tr-lg text-xs font-headline uppercase tracking-wider transition-colors ${
            tab === "system"
              ? `${isLight ? "text-slate-900" : "text-white"} font-bold border-b-2`
              : isLight
                ? "text-slate-500 hover:text-slate-800"
                : "text-slate-400 hover:text-slate-200"
          }`}
          style={
            tab === "system"
              ? {
                  borderBottomColor: semantic.accent3,
                  backgroundColor: alphaColor(semantic.accent2, isLight ? 0.12 : 0.16),
                }
              : undefined
          }
        >
          System Activity
        </button>
        <button
          type="button"
          onClick={() => props.onActivityTabChange?.("placeholders")}
          className={`px-4 py-2 rounded-tl-lg rounded-tr-lg text-xs font-headline uppercase tracking-wider transition-colors ${
            tab === "placeholders"
              ? `${isLight ? "text-slate-900" : "text-white"} font-bold border-b-2`
              : isLight
                ? "text-slate-500 hover:text-slate-800"
                : "text-slate-400 hover:text-slate-200"
          }`}
          style={
            tab === "placeholders"
              ? {
                  borderBottomColor: semantic.accent3,
                  backgroundColor: alphaColor(semantic.accent2, isLight ? 0.12 : 0.16),
                }
              : undefined
          }
        >
          Placeholder History
        </button>
      </div>

      {/* System Activity table */}
      {tab === "system" && (
        <div
          className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden mb-6"
          style={panelShellStyle}
        >
          <div className="flex justify-between items-start px-4 py-3 border-b border-[#424753]/30">
            <div>
              <h2 className="text-lg font-bold text-white font-headline">Recent Operations</h2>
              <p className="text-[11px] text-slate-400 mt-0.5">{props.rows.length} recent operations</p>
            </div>
          </div>
          {!props.rows.length ? (
            <div className="p-10 text-center text-slate-500 text-sm">No recent activity.</div>
          ) : (
            <>
              <div className="overflow-x-auto">
              <table className="min-w-[480px] w-full table-fixed">
                <colgroup>
                  <col className="w-[88px] sm:w-[104px]" />
                  <col />
                  <col className="w-[100px] sm:w-[112px]" />
                </colgroup>
                <thead>
                  <tr className="border-b border-[#424753]/20">
                    {["When", "Operation", "Status"].map(h => (
                      <th
                        key={h}
                        className={`px-3 py-2 text-left text-[10px] font-headline uppercase tracking-widest font-normal ${isLight ? "text-sky-800" : "text-slate-500"}`}
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#424753]/15">
                  {props.rows.map((row, idx) => {
                    const displayName = (row as any).display_name || row.event_type || row.job_type || "--";
                    const detailLine = (row as any).details || "";
                    const status = String(row.status || "").toLowerCase();
                    const statusColor = status === "done" || status === "success" ? "text-green-400" : status === "failed" ? "text-red-400" : "text-slate-400";
                    const dotColor = status === "done" || status === "success" ? "bg-green-500" : status === "failed" ? "bg-red-500" : "bg-slate-500";
                    const errorMessage = row.error ? ` — ${row.error}` : "";
                    const key = rowKey(row, idx);
                    const jobType = String((row as any).job_type || "");
                    const hasSectionProgress = Array.isArray((row as any).progress?.sections);
                    const queueItems: Array<any> =
                      jobType === "queue_monitor_batch" && Array.isArray((row as any).progress?.queue_items)
                        ? ((row as any).progress.queue_items as Array<any>)
                        : [];
                    const groupedEvents: Array<any> = Array.isArray((row as any).progress?.grouped_events)
                      ? ((row as any).progress.grouped_events as Array<any>)
                      : [];
                    const hasQueueBatchDetails = jobType === "queue_monitor_batch";
                    const hasGroupedEventDetails = groupedEvents.length > 0;
                    const hasExpandable = hasSectionProgress || hasQueueBatchDetails || hasGroupedEventDetails;
                    const isExpanded = !!expandedRows[key];
                    const progressSections = hasSectionProgress ? ((row as any).progress.sections as Array<any>) : [];

                    const toggleProgress = () => {
                      setExpandedRows((prev) => ({ ...prev, [key]: !prev[key] }));
                    };

                    const statusTokenClass = (token: string) => {
                      const normalized = String(token || "").toLowerCase();
                      if (normalized === "done") return "text-green-300 bg-green-700/20 border-green-500/30";
                      if (normalized === "failed") return "text-red-300 bg-red-700/20 border-red-500/30";
                      if (normalized === "working") return "text-sky-300 bg-sky-700/20 border-sky-500/30";
                      if (normalized === "skipped") return "text-amber-300 bg-amber-700/20 border-amber-500/30";
                      return "text-slate-300 bg-slate-700/20 border-slate-500/30";
                    };

                    return (
                      <tr key={key} className="hover:bg-[#1e2430]/40 transition-colors">
                        <td className="px-3 py-2 text-xs text-slate-400 whitespace-nowrap align-top">{timeAgo(row.time || null)}</td>
                        <td className="px-3 py-2 text-xs text-slate-300 min-w-0 align-top">
                          <span className="font-medium line-clamp-2">{displayName}</span>
                          {detailLine && <div className="ui-field-description-compact mt-0.5 line-clamp-2">{detailLine}</div>}
                          {errorMessage && <span className="text-[11px] text-red-300/70">{errorMessage}</span>}
                          {hasExpandable && (
                            <div className="mt-2">
                              <button
                                type="button"
                                onClick={toggleProgress}
                                className="inline-flex items-center gap-1 rounded border border-[#4a5568]/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-slate-300 hover:bg-[#2a3342]"
                              >
                                <span className="material-symbols-outlined" style={{ fontSize: 13 }}>{isExpanded ? "expand_less" : "expand_more"}</span>
                                {isExpanded ? "Hide details" : "Show details"}
                              </button>
                              {isExpanded && hasSectionProgress && (
                                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                                  {progressSections.map((section: any, sidx: number) => {
                                    const metrics = Array.isArray(section?.metrics) ? section.metrics : [];
                                    const sectionStatus = String(section?.status || "pending").toLowerCase();
                                    const showMetrics = sectionStatus === "done" || sectionStatus === "failed" || sectionStatus === "skipped";
                                    return (
                                      <div key={`${key}-section-${sidx}`} className="rounded border border-[#4a5568]/40 bg-[#1b2431] p-2">
                                        <div className="mb-1 flex items-center justify-between">
                                          <span className="text-[10px] font-headline uppercase tracking-wider text-slate-300">{String(section?.name || "Step")}</span>
                                          <span className={`rounded border px-1.5 py-0.5 text-[9px] font-headline uppercase tracking-wider ${statusTokenClass(String(section?.status || "pending"))}`}>
                                            {String(section?.status || "pending")}
                                          </span>
                                        </div>
                                        <div className="space-y-0.5 text-[11px] text-slate-400">
                                          {showMetrics ? (
                                            metrics.map((metric: any, midx: number) => (
                                              <div key={`${key}-section-${sidx}-metric-${midx}`} className="flex justify-between gap-2">
                                                <span>{String(metric?.label || "Metric")}</span>
                                                <span className="text-slate-200">{String(metric?.value ?? "--")}</span>
                                              </div>
                                            ))
                                          ) : (
                                            <div className="text-sky-300/80">
                                              {sectionStatus === "working" ? "Running..." : "Waiting to start..."}
                                            </div>
                                          )}
                                        </div>
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                              {isExpanded && hasQueueBatchDetails && (
                                <div className="mt-2 space-y-2">
                                  {queueItems.length === 0 ? (
                                    <div className="rounded border border-[#4a5568]/40 bg-[#1b2431] px-3 py-2 text-[11px] text-slate-400">
                                      No per-title rows in this batch yet (Arr queue may be empty while search runs).
                                    </div>
                                  ) : (
                                    queueItems.map((it: any, qix: number) => (
                                      <div
                                        key={`${key}-q-${qix}`}
                                        className="rounded border border-[#4a5568]/40 bg-[#1b2431] px-3 py-2 text-[11px]"
                                      >
                                        <div className="flex flex-wrap items-baseline justify-between gap-2">
                                          <span className="font-medium text-slate-200">
                                            {String(it?.title || "—")}
                                            {it?.subtitle ? (
                                              <span className="text-slate-500 font-normal"> · {String(it.subtitle)}</span>
                                            ) : null}
                                          </span>
                                          {it?.instance ? (
                                            <span className="text-[9px] font-headline uppercase tracking-wider text-slate-500">{String(it.instance)}</span>
                                          ) : null}
                                        </div>
                                        <div className="text-slate-400 mt-0.5">{String(it?.line || "—")}</div>
                                      </div>
                                    ))
                                  )}
                                </div>
                              )}
                              {isExpanded && hasGroupedEventDetails && (
                                <div className="mt-2 space-y-2">
                                  {groupedEvents.map((ev: any, gix: number) => {
                                    const evStatus = String(ev?.status || "").toLowerCase();
                                    const evStatusColor =
                                      evStatus === "done" || evStatus === "success"
                                        ? "text-green-400"
                                        : evStatus === "failed"
                                          ? "text-red-400"
                                          : "text-slate-400";
                                    return (
                                      <div
                                        key={`${key}-ge-${gix}`}
                                        className="rounded border border-[#4a5568]/40 bg-[#1b2431] px-3 py-2 text-[11px]"
                                      >
                                        <div className="flex flex-wrap items-center justify-between gap-2">
                                          <span className="font-medium text-slate-200">{String(ev?.display_name || "Event")}</span>
                                          <span className={`text-[10px] font-headline uppercase tracking-wider ${evStatusColor}`}>
                                            {evStatus || "done"}
                                          </span>
                                        </div>
                                        <div className="mt-0.5 text-slate-400">
                                          {ev?.details ? String(ev.details) : "—"}
                                          {ev?.source ? ` • ${String(ev.source)}` : ""}
                                        </div>
                                        {ev?.error ? (
                                          <div className="mt-0.5 text-red-300/70">{String(ev.error)}</div>
                                        ) : null}
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 align-top">
                          <div className="flex items-center gap-1.5 justify-end sm:justify-start">
                            <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotColor}`} />
                            <span className={`text-[10px] font-medium font-headline uppercase tracking-wider ${statusColor}`}>
                              {status === "done" || status === "success" ? "Complete" : status === "failed" ? "Failed" : "Running"}
                            </span>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              </div>
              <div className="px-4 py-2 border-t border-[#424753]/20 text-[10px] text-slate-500 font-headline uppercase tracking-widest">
                Showing {props.rows.length} items
              </div>
            </>
          )}
        </div>
      )}

      {/* Placeholder history table */}
      {tab === "placeholders" && (
        <div
          className={`rounded-xl overflow-hidden mb-6 border ${isLight ? "border-slate-200/90" : "border-[#424753]/40 bg-[#171c22]"}`}
          style={{
            ...(isLight ? { backgroundColor: semantic.surfacePanel } : {}),
            ...panelShellStyle,
          }}
        >
          <div
            className={`flex justify-between items-start px-5 py-4 border-b ${isLight ? "border-slate-200/80" : "border-[#424753]/30"}`}
          >
            <div>
              <h2 className={`text-xl font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}>Placeholder History</h2>
              <p className={`text-xs mt-0.5 ${isLight ? "text-slate-500" : "text-slate-400"}`}>{placeholderRows.length} recent placeholder changes</p>
            </div>
          </div>
          {!placeholderRows.length ? (
            <div className={`p-10 text-center text-sm ${isLight ? "text-slate-500" : "text-slate-500"}`}>No placeholder history yet.</div>
          ) : (
            <>
              <div className="overflow-hidden">
              <table className="w-full table-fixed">
                <colgroup>
                  <col className="w-[78px] sm:w-[96px]" />
                  <col />
                  <col className="w-[78px] sm:w-[92px]" />
                  <col className="w-[96px] sm:w-[132px]" />
                </colgroup>
                <thead>
                  <tr className={`border-b ${isLight ? "border-slate-200/90" : "border-[#424753]/20"}`}>
                    {["When", "Content", "Action", "Reason"].map(h => (
                      <th
                        key={h}
                        className={`px-2 sm:px-3 py-3 text-left text-[10px] font-headline uppercase tracking-widest font-normal ${isLight ? "text-sky-800" : "text-slate-500"}`}
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className={isLight ? "divide-y divide-slate-200/80" : "divide-y divide-[#424753]/15"}>
                  {placeholderRows.map((row, idx) => {
                    const actionColor =
                      row.action === "Created"
                        ? "text-green-400"
                        : row.action === "Deleted"
                          ? "text-orange-400"
                          : "text-sky-300";
                    const actionBg =
                      row.action === "Created"
                        ? "bg-green-500/20"
                        : row.action === "Deleted"
                          ? "bg-orange-500/20"
                          : "bg-sky-500/20";
                    const actionColorLight =
                      row.action === "Created"
                        ? "text-green-800"
                        : row.action === "Deleted"
                          ? "text-orange-900"
                          : "text-sky-900";
                    const actionBgLight =
                      row.action === "Created"
                        ? "bg-green-100"
                        : row.action === "Deleted"
                          ? "bg-orange-100"
                          : "bg-sky-100";
                    const contentDisplay = row.series_title
                      ? `${row.series_title} • ${row.item_title}`
                      : row.item_title;
                    const children = Array.isArray(row.children) ? row.children : [];
                    const isBatch = children.length > 0;
                    const batchKey = `ph-batch-${row.id}-${idx}`;
                    const batchOpen = !!placeholderHistoryExpanded[batchKey];

                    return (
                      <Fragment key={`placeholder-${row.id}-${idx}`}>
                        <tr
                          className={`transition-colors ${isBatch ? "cursor-pointer" : ""} ${isLight ? "hover:bg-slate-50/90" : "hover:bg-[#1e2430]/40"}`}
                          onClick={
                            isBatch
                              ? () =>
                                  setPlaceholderHistoryExpanded((prev) => ({
                                    ...prev,
                                    [batchKey]: !prev[batchKey],
                                  }))
                              : undefined
                          }
                        >
                          <td
                            className={`px-2 sm:px-3 py-4 text-xs sm:text-sm whitespace-nowrap truncate ${isLight ? "text-slate-500" : "text-slate-400"}`}
                            title={timeAgo(row.time || null)}
                          >
                            {timeAgo(row.time || null)}
                          </td>
                          <td className={`px-2 sm:px-3 py-4 text-xs sm:text-sm min-w-0 ${isLight ? "text-slate-800" : "text-slate-300"}`}>
                            <div className="flex items-start gap-1.5 min-w-0">
                              {isBatch ? (
                                <span
                                  className={`material-symbols-outlined flex-none transition-transform mt-0.5 ${isLight ? "text-slate-500" : "text-slate-500"}`}
                                  style={{ fontSize: 18, transform: batchOpen ? "rotate(90deg)" : "rotate(0deg)" }}
                                  aria-hidden
                                >
                                  chevron_right
                                </span>
                              ) : null}
                              <div className="min-w-0 flex-1">
                                <span className="font-medium block truncate" title={contentDisplay}>
                                  {contentDisplay}
                                </span>
                                {row.path ? (
                                  <div
                                    className={`text-[10px] mt-0.5 truncate hidden lg:block ${isLight ? "text-slate-500" : "text-slate-500"}`}
                                    title={row.path}
                                  >
                                    {row.path}
                                  </div>
                                ) : null}
                              </div>
                            </div>
                          </td>
                          <td className="px-2 sm:px-3 py-4">
                            <span
                              className={`inline-flex items-center px-2 py-1 rounded text-[10px] sm:text-xs font-medium font-headline uppercase tracking-wider whitespace-nowrap ${
                                isLight ? `${actionColorLight} ${actionBgLight}` : `${actionColor} ${actionBg}`
                              }`}
                            >
                              {row.action}
                            </span>
                          </td>
                          <td
                            className={`px-2 sm:px-3 py-4 text-xs sm:text-sm truncate ${isLight ? "text-slate-600" : "text-slate-400"}`}
                            title={row.reason || undefined}
                          >
                            {row.reason}
                          </td>
                        </tr>
                        {isBatch && batchOpen ? (
                          <tr
                            key={`${batchKey}-detail`}
                            onClick={(e) => e.stopPropagation()}
                            style={{ backgroundColor: isLight ? semantic.surfaceMuted : "#12161c" }}
                          >
                            <td
                              colSpan={4}
                              className={`p-0 border-t align-top ${isLight ? "border-slate-200/90" : "border-[#424753]/25"}`}
                            >
                              <div className="px-3 sm:px-4 py-3.5 pl-9 sm:pl-11 space-y-4">
                                {children.map((child: any, cidx: number) => {
                                  const childContent = child.series_title
                                    ? `${child.series_title} • ${child.item_title || ""}`
                                    : child.item_title || "";
                                  const statusLabel = String(child.status || "").trim() || "Unknown";
                                  const railColor = isLight ? alphaColor(semantic.accent2, 0.55) : alphaColor(semantic.accentIce, 0.4);
                                  return (
                                    <div
                                      key={`${batchKey}-c-${child.id}-${cidx}`}
                                      className="min-w-0 border-l-2 pl-3.5"
                                      style={{ borderLeftColor: railColor }}
                                      title={child.reason ? String(child.reason) : undefined}
                                    >
                                      <div className="flex items-start justify-between gap-3 min-w-0">
                                        <div
                                          className={`text-xs sm:text-sm font-medium truncate min-w-0 flex-1 ${isLight ? "text-slate-900" : "text-slate-100"}`}
                                          title={childContent}
                                        >
                                          {childContent}
                                        </div>
                                        <div className="flex-none flex flex-col items-end gap-1 text-right shrink-0 max-w-[min(12.5rem,46%)] sm:max-w-[14rem]">
                                          <span
                                            className="text-[9px] font-headline uppercase tracking-wider leading-tight"
                                            style={{ color: semantic.fgMuted }}
                                          >
                                            Status updated to
                                          </span>
                                          <span
                                            className="text-[11px] sm:text-xs font-headline font-semibold uppercase tracking-wide px-2.5 py-1 rounded-md border"
                                            style={{
                                              color: isLight ? semantic.fgOnAccent : semantic.accent,
                                              backgroundColor: isLight
                                                ? alphaColor(semantic.accent, 0.2)
                                                : alphaColor(semantic.accent, 0.14),
                                              borderColor: isLight
                                                ? alphaColor(semantic.accent, 0.42)
                                                : alphaColor(semantic.accent, 0.28),
                                            }}
                                          >
                                            {statusLabel}
                                          </span>
                                        </div>
                                      </div>
                                      {child.path ? (
                                        <div
                                          className={`text-[10px] mt-1.5 truncate font-mono ${isLight ? "text-slate-500" : "text-slate-500"}`}
                                          title={child.path}
                                        >
                                          {child.path}
                                        </div>
                                      ) : null}
                                    </div>
                                  );
                                })}
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
              </div>
              <div
                className={`px-5 py-3 border-t text-[10px] font-headline uppercase tracking-widest ${isLight ? "border-slate-200/90 text-slate-500" : "border-[#424753]/20 text-slate-500"}`}
              >
                Showing {placeholderRows.length} items
              </div>
            </>
          )}
        </div>
      )}

      {/* Storage Progress (system tab only) */}
      {tab === "system" && (
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="mb-4">
            <span className="font-headline text-xs font-bold text-white uppercase tracking-widest">Storage Progress</span>
          </div>
          <div className="space-y-4">
            <div>
              <div className="flex justify-between text-[10px] text-slate-400 mb-1.5 font-headline uppercase tracking-widest">
                <span>Movies on Disk</span><span>{s?.movies.downloaded ?? "--"} / {s?.movies.total ?? "--"}</span>
              </div>
              <div className="h-1.5 bg-[#252e3a] rounded-full">
                <div className="h-full rounded-full" style={{ backgroundColor: accent.hex, width: s ? `${Math.min(100, (s.movies.downloaded / Math.max(s.movies.total, 1)) * 100).toFixed(0)}%` : "0%" }} />
              </div>
            </div>
            <div>
              <div className="flex justify-between text-[10px] text-slate-400 mb-1.5 font-headline uppercase tracking-widest">
                <span>Episodes on Disk</span><span>{s?.episodes.downloaded ?? "--"} / {s?.episodes.total ?? "--"}</span>
              </div>
              <div className="h-1.5 bg-[#252e3a] rounded-full">
                <div className="h-full rounded-full" style={{ backgroundColor: accent.hex, width: s ? `${Math.min(100, (s.episodes.downloaded / Math.max(s.episodes.total, 1)) * 100).toFixed(0)}%` : "0%" }} />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function LibraryPanel(props: {
  shelfTitle: string;
  items: LibraryItem[];
  activeFilter: LibraryShelfFilter;
  onFilterChange: (value: LibraryShelfFilter) => void;
  onOpenDetail: (item: LibraryItem) => void;
  brand: Brand; themeMode: ThemeMode;
}) {
  const accent = getBrandAccent(props.brand, props.themeMode);
  const isLight = props.themeMode === "light";
  const filters: Array<{ id: LibraryShelfFilter; label: string }> = [
    { id: "all", label: "All" },
    { id: "placeholders", label: "Placeholders" },
    { id: "future", label: "Future" },
    { id: "missing", label: "Missing" },
  ];
  const totalMissing = props.items.filter(i => i.has_missing).length;
  const sectionRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const groupedItems = useMemo(() => {
    const groups: Record<string, LibraryItem[]> = {};
    props.items.forEach((item) => {
      const letter = titleSortLetter(item.title);
      if (!groups[letter]) groups[letter] = [];
      groups[letter].push(item);
    });
    const letters = Object.keys(groups).sort((a, b) => {
      if (a === "#") return 1;
      if (b === "#") return -1;
      return a.localeCompare(b);
    });
    return { groups, letters };
  }, [props.items]);

  function statusBadge(item: LibraryItem) {
    if (item.has_missing) {
      return (
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase tracking-wider border ${
            isLight ? "bg-red-50 text-red-800 border-red-200" : "bg-red-600 text-white border-transparent"
          }`}
        >
          Missing
        </span>
      );
    }
    if (item.has_placeholder) {
      return (
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase tracking-wider border ${
            isLight ? "bg-teal-50 text-teal-900 border-teal-200" : "bg-teal-700 text-white border-transparent"
          }`}
        >
          Placeholder
        </span>
      );
    }
    if (item.is_future) {
      return (
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase tracking-wider border ${
            isLight ? "bg-slate-100 text-slate-700 border-slate-200" : "bg-slate-600 text-white border-transparent"
          }`}
        >
          Future
        </span>
      );
    }
    if (item.has_file) {
      return (
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase tracking-wider border ${
            isLight ? "bg-slate-100 text-slate-700 border-slate-200" : "bg-slate-500 text-white border-transparent"
          }`}
        >
          1080p
        </span>
      );
    }
    return null;
  }

  return (
    <div>
      {/* Header + filter tabs */}
      <div className="flex flex-wrap justify-between items-end gap-4 mb-6">
        <div>
          <h2 className={`text-3xl font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>{props.shelfTitle}</h2>
          <p className={`text-sm mt-1 ${isLight ? "text-slate-600" : "text-slate-400"}`}>Showing {props.items.length} items matching your criteria</p>
        </div>
        <div
          className={`flex flex-wrap gap-1 p-1 rounded-lg border ${
            isLight ? "bg-white border-slate-200/90 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          {filters.map(f => (
            <button key={f.id} type="button" onClick={() => props.onFilterChange(f.id)}
              className={`px-4 py-1.5 rounded-md text-xs font-headline uppercase tracking-wider transition-colors ${
                f.id === props.activeFilter
                  ? isLight
                    ? "text-slate-900 font-semibold"
                    : "text-white font-semibold"
                  : isLight
                    ? "text-slate-600 hover:text-slate-900 hover:bg-slate-50"
                    : "text-slate-400 hover:text-slate-200"
                  }`} style={f.id === props.activeFilter ? { backgroundColor: accent.hex } : undefined}>
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {/* Poster grid with alphabet sections */}
      {props.items.length === 0 ? (
        <div className={`text-center py-16 ${isLight ? "text-slate-600" : "text-slate-500"}`}>No library items match the current filter.</div>
      ) : (
        <div className="flex items-start gap-4 mb-8">
          <div className="flex-1 space-y-8">
            {groupedItems.letters.map((letter) => (
              <div
                key={letter}
                ref={(el) => {
                  sectionRefs.current[letter] = el;
                }}
                style={{ contentVisibility: "auto", containIntrinsicSize: "1px 720px" }}
              >
                <div
                  className={`mb-3 text-xs font-headline uppercase tracking-widest border-b pb-2 ${
                    isLight ? "text-slate-700 border-slate-200" : "text-slate-500 border-[#424753]/25"
                  }`}
                >
                  {letter}
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
                  {(groupedItems.groups[letter] || []).map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => props.onOpenDetail(item)}
                      className={`relative isolate rounded-xl overflow-hidden group cursor-pointer text-left transition-transform hover:scale-[1.02] ${
                        isLight ? "bg-white shadow-md shadow-slate-900/8" : "bg-[#1e2430]"
                      }`}
                      style={{
                        aspectRatio: "2/3",
                        border: `2px solid ${accent.hex}`,
                      }}
                    >
                      {item.poster_url ? (
                        <img
                          src={item.poster_url}
                          alt=""
                          className="absolute inset-0 h-full w-full object-cover scale-[1.01]"
                        />
                      ) : null}
                      <div
                        className={`absolute pointer-events-none bottom-0 left-0 right-0 ${isLight ? "h-[48%]" : "h-[34%]"}`}
                        style={{
                          background: isLight
                            ? "linear-gradient(180deg, rgba(238,243,248,0) 0%, rgba(238,243,248,0.42) 38%, rgba(238,243,248,0.88) 68%, rgba(238,243,248,1) 88%, rgba(238,243,248,1) 100%)"
                            : "linear-gradient(180deg, rgba(15,20,25,0) 0%, rgba(15,20,25,0.62) 58%, rgba(15,20,25,0.95) 100%)",
                        }}
                      />
                      <div className="absolute top-2 left-2">{statusBadge(item)}</div>
                      <div className={`absolute bottom-0 left-0 right-0 flex flex-col gap-1 px-3 ${isLight ? "pb-3 pt-12" : "pb-3 pt-8"}`}>
                        <div className="text-xs font-semibold tabular-nums truncate" style={{ color: accent.icon }}>
                          {item.year || "—"}
                        </div>
                        <div className={`font-bold text-sm leading-snug line-clamp-2 ${isLight ? "text-slate-900" : "text-white"}`}>{item.title}</div>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div
            className={`hidden lg:flex sticky top-24 flex-col gap-1 rounded-lg border px-2 py-2 ${
              isLight ? "border-slate-200 bg-white/95 shadow-sm backdrop-blur-sm" : "border-[#424753]/35 bg-[#111722]/90"
            }`}
          >
            {groupedItems.letters.map((letter) => (
              <button
                key={`alpha-${letter}`}
                type="button"
                onClick={() => sectionRefs.current[letter]?.scrollIntoView({ behavior: "smooth", block: "start" })}
                className={`w-6 h-6 rounded text-[10px] font-headline font-bold transition-colors ${
                  isLight
                    ? "text-slate-600 hover:text-slate-900 hover:bg-slate-100"
                    : "text-slate-400 hover:text-white hover:bg-[#293346]"
                }`}
                title={`Jump to ${letter}`}
              >
                {letter}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Footer stat cards */}
      <div className="grid grid-cols-3 gap-4">
        <div
          className={`rounded-xl border p-5 ${
            isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          <div className="flex justify-between items-start">
            <div className={`text-[10px] font-headline uppercase tracking-widest mb-3 ${isLight ? "text-slate-500" : "text-slate-400"}`}>Total Items</div>
            <span className={`material-symbols-outlined ${isLight ? "text-slate-400" : "text-slate-600"}`} style={{ fontSize: 18 }}>storage</span>
          </div>
          <div className={`text-3xl font-black font-headline ${isLight ? "text-slate-900" : "text-white"}`}>{props.items.length}</div>
        </div>
        <div
          className={`rounded-xl border p-5 ${
            isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          <div className="flex justify-between items-start">
            <div className={`text-[10px] font-headline uppercase tracking-widest mb-3 ${isLight ? "text-slate-500" : "text-slate-400"}`}>Missing Assets</div>
            <span className="material-symbols-outlined text-yellow-500" style={{ fontSize: 18 }}>warning</span>
          </div>
          <div className={`text-3xl font-black font-headline ${isLight ? "text-slate-900" : "text-white"}`}>{totalMissing}</div>
          {totalMissing > 0 && (
            <button type="button" onClick={() => props.onFilterChange("missing")}
              className="mt-3 text-xs font-headline uppercase tracking-wider flex items-center gap-1" style={{ color: accent.icon }}>
              View Errors <span className="material-symbols-outlined" style={{ fontSize: 14 }}>arrow_forward</span>
            </button>
          )}
        </div>
        <div
          className={`rounded-xl border p-5 ${
            isLight ? "bg-white border-slate-200 shadow-sm" : "bg-[#171c22] border-[#424753]/40"
          }`}
        >
          <div className="flex justify-between items-start">
            <div className={`text-[10px] font-headline uppercase tracking-widest mb-3 ${isLight ? "text-slate-500" : "text-slate-400"}`}>Sync Status</div>
            <span className="material-symbols-outlined" style={{ fontSize: 18, color: accent.hex }}>sync</span>
          </div>
          <div className="flex items-center gap-2 mb-1">
            <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
            <span className={`font-bold font-headline text-sm ${isLight ? "text-slate-900" : "text-white"}`}>Active</span>
          </div>
          <div className={`text-xs ${isLight ? "text-slate-600" : "text-slate-400"}`}>Library indexed</div>
        </div>
      </div>
    </div>
  );
}

function DetailRoutePage(props: { brand: Brand; themeMode: ThemeMode; scrollContainerRef: React.RefObject<HTMLElement | null> }) {
  const navigate = useNavigate();
  const location = useLocation();
  const accent = getBrandAccent(props.brand, props.themeMode);
  const isLight = props.themeMode === "light";
  const [payload, setPayload] = useState<MovieDetailResponse | SeriesDetailResponse | null>(null);
  const [openSeasons, setOpenSeasons] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const pathParts = location.pathname.split("/");
  const entityType = pathParts[2] || "";
  const itemId = pathParts[3] || "";

  useEffect(() => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const container = props.scrollContainerRef.current;
        if (container) container.scrollTop = 0;
        window.scrollTo(0, 0);
        document.documentElement.scrollTop = 0;
        document.body.scrollTop = 0;
      });
    });
  }, [entityType, itemId, props.scrollContainerRef]);

  useEffect(() => {
    if (loading) return;
    const container = props.scrollContainerRef.current;
    if (container) container.scrollTop = 0;
    window.scrollTo(0, 0);
  }, [loading, props.scrollContainerRef]);

  useEffect(() => {
    let stopped = false;
    async function load() {
      if (!entityType || !itemId) return;
      setLoading(true);
      setError(null);
      try {
        if (entityType === "movie") {
          const result = await getMovieDetail(Number(itemId));
          if (result.ok && !stopped) {
            setPayload(result);
          } else if (!stopped && !result.ok) {
            setError((result as { message?: string }).message || "Movie not found");
          } else if (!stopped) {
            setError("Movie not found");
          }
        } else if (entityType === "series") {
          const result = await getSeriesDetail(Number(itemId));
          if (result.ok && result.type === "series" && !stopped) {
            setPayload(result);
            setOpenSeasons([]);
          } else if (!stopped && !result.ok) {
            setError((result as { message?: string }).message || "Series not found");
          } else if (!stopped) {
            setError("Series not found");
          }
        } else if (!stopped) {
          setError("Unsupported detail type");
        }
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Failed to load detail");
      } finally {
        if (!stopped) setLoading(false);
      }
    }
    load();
    return () => { stopped = true; };
  }, [entityType, itemId]);

  return (
    <div className={`min-h-screen ${isLight ? "bg-[#eef3f8]" : "bg-[#0f1419]"}`}>
      <div className={`px-6 py-4 border-b flex items-center gap-3 ${isLight ? "border-[#d7e2f0]" : "border-[#424753]/30"}`}>
        <button type="button" onClick={() => {
          sessionStorage.setItem("libraryScrollRestorePending", "1");
          navigate(-1);
        }}
          className={`flex items-center gap-1.5 text-xs font-headline uppercase tracking-wider transition-colors ${isLight ? "text-slate-500 hover:text-slate-900" : "text-slate-400 hover:text-white"}`}>
          <span className="material-symbols-outlined" style={{ fontSize: 16 }}>arrow_back</span>
          Library
        </button>
        <span className={isLight ? "text-slate-400" : "text-slate-600"}>/</span>
        <span className={`text-xs font-headline uppercase tracking-wider ${isLight ? "text-slate-700" : "text-slate-300"}`}>
          {loading ? "Loading..." : payload?.title || "Detail"}
        </span>
      </div>
      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="flex items-center gap-3 text-slate-400">
            <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
            <span className="text-sm font-headline uppercase tracking-widest">Loading detail...</span>
          </div>
        </div>
      ) : null}
      {error ? (
        <div
          className={`mx-6 mt-4 rounded-xl border p-4 text-sm ${
            isLight ? "border-red-200 bg-red-50 text-red-800" : "border-red-500/30 bg-red-600/15 text-red-300"
          }`}
        >
          {error}
        </div>
      ) : null}
      {!loading && !error && payload?.type === "movie" ? <MovieDetail payload={payload} brand={props.brand} themeMode={props.themeMode} /> : null}
      {!loading && !error && payload?.type === "series" ? (
        <SeriesDetail
          payload={payload}
          brand={props.brand}
          themeMode={props.themeMode}
          openSeasons={openSeasons}
          onToggleSeason={(seasonId) => setOpenSeasons((prev) => (prev.includes(seasonId) ? prev.filter((id) => id !== seasonId) : [...prev, seasonId]))}
        />
      ) : null}
    </div>
  );
}

function detailArrInstanceLinks(
  payload: { arr_instance_links?: ArrInstanceOpenLink[]; arr_link?: string | null },
  singleFallbackLabel: string,
): { label: string; url: string }[] {
  if (payload.arr_instance_links?.length) {
    return payload.arr_instance_links.map((x) => ({ label: x.label, url: x.url }));
  }
  if (payload.arr_link) return [{ label: singleFallbackLabel, url: payload.arr_link }];
  return [];
}

/** Logo well for movie file-state strip — matches onboarding ARR integration icon boxes (`#1e2430` + ring). */
const MOVIE_FILE_STATE_RADARR_LOGO_WELL: CSSProperties = {
  backgroundColor: "#1e2430",
  border: "2px solid rgba(250, 204, 21, 0.78)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
};

const MOVIE_FILE_STATE_PLACEHOLDARR_LOGO_WELL: CSSProperties = {
  backgroundColor: "#1e2430",
  border: "2px solid rgba(251, 191, 36, 0.78)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
};

/** Sonarr icon well — matches onboarding ``ONBOARDING_ARR_VISUAL.sonarr`` ring. */
const SERIES_FILE_STATE_SONARR_LOGO_WELL: CSSProperties = {
  backgroundColor: "#1e2430",
  border: "2px solid rgba(56, 189, 248, 0.8)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
};

function seriesFileStateSonarrFileTotal(present: boolean, episodeFiles: number): string {
  if (!present) return "-";
  return String(Math.max(0, Math.floor(Number.isFinite(episodeFiles) ? episodeFiles : 0)));
}

function movieFileStateRadarrStatus(row: { present: boolean; has_file_known: boolean; has_file: boolean }): string {
  if (!row.present) return "-";
  if (!row.has_file_known) return "-";
  return row.has_file ? "Yes" : "No";
}

/**
 * Onboarding-style integration tiles: fixed navy icon wells, **Placeholdarr** first (placeholder on disk),
 * then each Radarr instance (name under logo, whole tile links to Radarr). Outer frame follows light/dark.
 */
function MovieFileStateSection(props: {
  links: ArrInstanceOpenLink[] | undefined;
  arrLink?: string | null;
  hasFile: boolean;
  hasPlaceholder: boolean;
  instanceLabel?: string | null;
  isLight: boolean;
  brand: Brand;
  accentHex: string;
  radarrIconSrc: string;
}) {
  const instanceLabel = String(props.instanceLabel || "Radarr").trim() || "Radarr";
  const rawMovieLinks = props.links;
  const linkRows: {
    label: string;
    url: string;
    present: boolean;
    has_file: boolean;
    has_file_known: boolean;
    has_placeholder: boolean;
  }[] = Array.isArray(rawMovieLinks) && rawMovieLinks.length
    ? rawMovieLinks.map((l) => ({
        label: l.label,
        url: l.url,
        present: l.present !== false,
        has_file: l.has_file === true,
        has_file_known: typeof l.has_file === "boolean",
        has_placeholder: Boolean(l.has_placeholder),
      }))
    : rawMovieLinks == null
      ? (() => {
          const u = String(props.arrLink || "").trim();
          if (!u) return [];
          return [
            {
              label: instanceLabel,
              url: u,
              present: true,
              has_file: props.hasFile,
              has_file_known: true,
              has_placeholder: props.hasPlaceholder,
            },
          ];
        })()
      : [];

  const placeholderOnDisk =
    Boolean(props.hasPlaceholder) || linkRows.some((r) => r.has_placeholder);
  const brandLabel = getBrandAccent(props.brand, props.isLight ? "light" : "dark").label;

  return (
    <div
      className={`mb-4 rounded-lg border px-3 py-3 md:px-4 md:py-3 ${
        props.isLight ? "border-[#d7e2f0] bg-white shadow-sm" : "border-[#424753]/40 bg-[#171c22]"
      }`}
    >
      <div className="flex w-full flex-wrap items-stretch justify-center gap-3">
        <div
          className="movie-file-state-dark-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
          role="group"
          aria-label="Placeholder dummy on disk"
        >
          <div
            className="flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl"
            style={MOVIE_FILE_STATE_PLACEHOLDARR_LOGO_WELL}
            aria-hidden
          >
            <BrandLogo
              brand={props.brand}
              accentHex={props.accentHex}
              variant="yellow"
              className="h-10 w-auto max-w-[4.75rem] object-contain object-center"
            />
          </div>
          <div className="movie-file-state-tile-title text-sm font-semibold font-headline leading-tight">{brandLabel}</div>
          <div className="movie-file-state-tile-status text-lg font-bold font-headline tabular-nums leading-none">
            {placeholderOnDisk ? "Yes" : "No"}
          </div>
        </div>
        {linkRows.map((row, idx) => {
          const status = movieFileStateRadarrStatus(row);
          return (
            <a
              key={`${row.url}-${idx}`}
              href={row.url}
              target="_blank"
              rel="noreferrer"
              className="movie-file-state-dark-tile movie-file-state-arr-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
            >
              <div
                className="movie-file-state-arr-well flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl"
                style={MOVIE_FILE_STATE_RADARR_LOGO_WELL}
                aria-hidden
              >
                <img src={props.radarrIconSrc} alt="" decoding="async" className="h-12 w-12 object-contain" aria-hidden />
              </div>
              <div className="movie-file-state-tile-title text-sm font-semibold font-headline leading-tight">{row.label}</div>
              <div className="movie-file-state-tile-status text-lg font-bold font-headline tabular-nums leading-none">{status}</div>
            </a>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Same integration strip as movie detail: **Placeholdarr** first, then each **Sonarr** instance.
 * **Placeholdarr** shows total **placeholder** episode count; each Sonarr tile shows **downloaded episode file**
 * count, or ``-`` when ``present: false``. Uses ``arr_instance_links`` whenever the API sends a non-empty array
 * (do not fall back to ``arr_link`` when the array is empty — that would hide padded multi-instance rows).
 */
function SeriesFileStateSection(props: {
  seasons: SeriesSeasonDetail[];
  links: ArrInstanceOpenLink[] | undefined;
  arrLink?: string | null;
  instanceLabel?: string | null;
  isLight: boolean;
  brand: Brand;
  accentHex: string;
  sonarrIconSrc: string;
}) {
  const instanceLabel = String(props.instanceLabel || "Sonarr").trim() || "Sonarr";
  const rawLinks = props.links;
  const linkRows: { label: string; url: string; present: boolean; episode_files: number }[] = Array.isArray(rawLinks) && rawLinks.length
    ? rawLinks.map((l) => ({
        label: l.label,
        url: l.url,
        present: l.present !== false,
        episode_files: typeof l.episode_files === "number" ? l.episode_files : 0,
      }))
    : rawLinks == null
      ? (() => {
          const u = String(props.arrLink || "").trim();
          if (!u) return [];
          const files = (props.seasons || []).reduce((a, s) => a + Number(s.episode_files || 0), 0);
          return [{ label: instanceLabel, url: u, present: true, episode_files: files }];
        })()
      : [];

  const aggPlaceholders = useMemo(
    () => (props.seasons || []).reduce((a, s) => a + Number(s.episode_placeholders || 0), 0),
    [props.seasons],
  );

  const brandLabel = getBrandAccent(props.brand, props.isLight ? "light" : "dark").label;
  const phTotalStr = String(Math.max(0, Math.floor(aggPlaceholders)));

  return (
    <div
      className={`mb-4 rounded-lg border px-3 py-3 md:px-4 md:py-3 ${
        props.isLight ? "border-[#d7e2f0] bg-white shadow-sm" : "border-[#424753]/40 bg-[#171c22]"
      }`}
    >
      <div className="flex w-full flex-wrap items-stretch justify-center gap-3">
        <div
          className="movie-file-state-dark-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
          role="group"
          aria-label="Episodes with placeholder files"
        >
          <div
            className="flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl"
            style={MOVIE_FILE_STATE_PLACEHOLDARR_LOGO_WELL}
            aria-hidden
          >
            <BrandLogo
              brand={props.brand}
              accentHex={props.accentHex}
              variant="yellow"
              className="h-10 w-auto max-w-[4.75rem] object-contain object-center"
            />
          </div>
          <div className="movie-file-state-tile-title text-sm font-semibold font-headline leading-tight">{brandLabel}</div>
          <div className="movie-file-state-tile-status text-2xl font-black font-headline tabular-nums leading-none">{phTotalStr}</div>
          <div className="movie-file-state-tile-caption mt-0.5 text-[10px] font-headline font-medium uppercase tracking-wider">Episodes</div>
        </div>
        {linkRows.map((row, idx) => {
          const totalStr = seriesFileStateSonarrFileTotal(row.present, row.episode_files);
          return (
            <a
              key={`${row.label}-${row.url}-${idx}`}
              href={row.url}
              target="_blank"
              rel="noreferrer"
              className="movie-file-state-dark-tile movie-file-state-arr-tile flex min-w-[7.5rem] flex-1 flex-col items-center gap-3 px-4 py-5 text-center sm:min-w-[9rem] sm:max-w-[11rem]"
            >
              <div
                className="movie-file-state-arr-well flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl"
                style={SERIES_FILE_STATE_SONARR_LOGO_WELL}
                aria-hidden
              >
                <img src={props.sonarrIconSrc} alt="" decoding="async" className="h-12 w-12 object-contain" aria-hidden />
              </div>
              <div className="movie-file-state-tile-title text-sm font-semibold font-headline leading-tight">{row.label}</div>
              <div className="movie-file-state-tile-status text-2xl font-black font-headline tabular-nums leading-none">{totalStr}</div>
              <div className="movie-file-state-tile-caption mt-0.5 text-[10px] font-headline font-medium uppercase tracking-wider">Episodes</div>
            </a>
          );
        })}
      </div>
    </div>
  );
}

function DetailArrLaunchBar(props: {
  links: { label: string; url: string }[];
  iconSrc: string;
  heading: string;
  isLight: boolean;
}) {
  if (!props.links.length) return null;
  return (
    <div className={`rounded-xl border p-5 md:p-6 ${props.isLight ? "bg-white border-[#d7e2f0] shadow-sm" : "bg-[#171c22] border-[#424753]/40"}`}>
      <h3 className={`text-[11px] font-headline uppercase tracking-widest ${props.isLight ? "text-slate-500" : "text-slate-400"}`}>{props.heading}</h3>
      <div className="mt-4 flex flex-wrap gap-3">
        {props.links.map((lnk) => (
          <a
            key={lnk.url}
            href={lnk.url}
            target="_blank"
            rel="noreferrer"
            className="detail-arr-instance-launch group inline-flex min-w-[12.5rem] flex-1 items-center gap-3 rounded-xl border border-[#424753]/50 px-4 py-3 transition-colors hover:border-[#424753] sm:max-w-sm sm:flex-none"
          >
            <img src={props.iconSrc} alt="" className="h-9 w-9 shrink-0 object-contain" decoding="async" />
            <span className="detail-arr-instance-launch__label min-w-0 flex-1 font-headline text-sm font-semibold leading-tight text-slate-100">{lnk.label}</span>
            <span className="material-symbols-outlined shrink-0 text-slate-500 transition-colors group-hover:text-slate-300" style={{ fontSize: 18 }}>
              open_in_new
            </span>
          </a>
        ))}
      </div>
    </div>
  );
}

function MovieDetail(props: { payload: MovieDetailResponse; brand: Brand; themeMode: ThemeMode }) {
  const payload = props.payload;
  const accent = getBrandAccent(props.brand, props.themeMode);
  const isLight = props.themeMode === "light";
  const heroArtUrl = payload.backdrop_url || payload.poster_url;
  return (
    <div>
      {/* Hero banner */}
      <div className="relative h-[22rem] md:h-[30rem] lg:h-[34rem] overflow-hidden"
        style={heroArtUrl ? { backgroundImage: `linear-gradient(to right, ${isLight ? "rgba(238,243,248,0.90)" : "rgba(8,12,18,0.78)"} 18%, ${isLight ? "rgba(238,243,248,0.52)" : "rgba(8,12,18,0.45)"} 42%, ${isLight ? "rgba(238,243,248,0.10)" : "rgba(8,12,18,0.08)"}), url(${heroArtUrl})`, backgroundSize: "cover", backgroundPosition: "center 35%" } : { backgroundColor: alphaColor(accent.hex, isLight ? 0.14 : 0.2) }}>
        <div
          className="absolute inset-0"
          style={{
            background: isLight
              ? "linear-gradient(180deg, rgba(238,243,248,0) 32%, rgba(238,243,248,0.2) 58%, rgba(238,243,248,0.72) 80%, rgba(238,243,248,0.96) 93%, rgba(238,243,248,1) 100%)"
              : "linear-gradient(180deg, rgba(15,20,25,0) 32%, rgba(15,20,25,0.22) 58%, rgba(15,20,25,0.72) 80%, rgba(15,20,25,0.96) 93%, rgba(15,20,25,1) 100%)",
          }}
        />
      </div>

      <div className="px-6 md:px-10 lg:px-12 -mt-64 md:-mt-80 lg:-mt-96 relative pb-10">
        <div className="flex gap-6 md:gap-10 items-end mb-8 md:mb-10">
          <div className={`flex-none w-40 h-60 md:w-52 md:h-[19.5rem] lg:w-56 lg:h-[21rem] rounded-2xl overflow-hidden border-2 shadow-[0_30px_80px_rgba(0,0,0,0.5)] ${isLight ? "border-[#d7e2f0] bg-white" : "border-[#424753]/40 bg-[#1e2430]"}`}>
            {payload.poster_url ? <img src={payload.poster_url} alt="" className="w-full h-full object-cover" /> : <div className="w-full h-full flex items-center justify-center text-slate-600 font-bold">MOV</div>}
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-end pb-1 md:pb-2">
            {payload.year ? (
              <div className="text-xl font-semibold tabular-nums md:text-2xl" style={{ color: accent.icon }}>
                {payload.year}
              </div>
            ) : null}
            <h1 className={`mt-1 text-4xl font-black font-headline tracking-tight leading-[1.02] md:text-5xl lg:text-6xl ${isLight ? "text-slate-900" : "text-white"}`}>{payload.title}</h1>
          </div>
        </div>

        {payload.overview && <p className={`text-lg leading-relaxed max-w-5xl mb-8 ${isLight ? "text-slate-700" : "text-slate-200"}`}>{payload.overview}</p>}

        <MovieFileStateSection
          links={payload.arr_instance_links}
          arrLink={payload.arr_link}
          hasFile={payload.has_file}
          hasPlaceholder={payload.has_placeholder}
          instanceLabel={payload.instance_label}
          isLight={isLight}
          brand={props.brand}
          accentHex={accent.hex}
          radarrIconSrc={radarrIcon}
        />

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          {[
            { label: "Quality", value: payload.radarr_quality },
            { label: "Theatrical", value: payload.theater_release_date },
            { label: "Digital", value: payload.digital_release_date },
            { label: "Physical", value: payload.physical_release_date },
          ].filter(m => m.value).map(m => (
            <div key={m.label} className={`rounded-xl border p-5 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}>
              <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-1">{m.label}</div>
              <div className={`text-base font-semibold ${isLight ? "text-slate-900" : "text-white"}`}>{m.value}</div>
            </div>
          ))}
        </div>

      </div>
    </div>
  );
}

function SeriesDetail(props: { payload: SeriesDetailResponse; brand: Brand; themeMode: ThemeMode; openSeasons: number[]; onToggleSeason: (seasonId: number) => void }) {
  const payload = props.payload;
  const accent = getBrandAccent(props.brand, props.themeMode);
  const isLight = props.themeMode === "light";
  const heroArtUrl = payload.backdrop_url || payload.poster_url;
  const seasonsDesc = useMemo(
    () => [...(payload.seasons || [])].sort((a, b) => (b.season_number || 0) - (a.season_number || 0)),
    [payload.seasons],
  );
  return (
    <div>
      {/* Hero banner — light mode uses the same soft scrim treatment as movie detail */}
      <div className="relative h-[22rem] md:h-[30rem] lg:h-[34rem] overflow-hidden"
        style={heroArtUrl ? { backgroundImage: `linear-gradient(to right, ${isLight ? "rgba(238,243,248,0.90)" : "rgba(8,12,18,0.78)"} 18%, ${isLight ? "rgba(238,243,248,0.52)" : "rgba(8,12,18,0.45)"} 42%, ${isLight ? "rgba(238,243,248,0.10)" : "rgba(8,12,18,0.08)"}), url(${heroArtUrl})`, backgroundSize: "cover", backgroundPosition: "center 35%" } : { backgroundColor: alphaColor(accent.hex, isLight ? 0.14 : 0.2) }}>
        <div
          className="absolute inset-0"
          style={{
            background: isLight
              ? "linear-gradient(180deg, rgba(238,243,248,0) 32%, rgba(238,243,248,0.2) 58%, rgba(238,243,248,0.72) 80%, rgba(238,243,248,0.96) 93%, rgba(238,243,248,1) 100%)"
              : "linear-gradient(180deg, rgba(15,20,25,0) 32%, rgba(15,20,25,0.22) 58%, rgba(15,20,25,0.72) 80%, rgba(15,20,25,0.96) 93%, rgba(15,20,25,1) 100%)",
          }}
        />
      </div>

      <div className="px-6 md:px-10 lg:px-12 -mt-64 md:-mt-80 lg:-mt-96 relative pb-10">
        <div className="flex gap-6 md:gap-10 items-end mb-8 md:mb-10">
          <div className={`flex-none w-40 h-60 md:w-52 md:h-[19.5rem] lg:w-56 lg:h-[21rem] rounded-2xl overflow-hidden border-2 shadow-[0_30px_80px_rgba(0,0,0,0.5)] ${isLight ? "border-[#d7e2f0] bg-white" : "border-[#424753]/40 bg-[#1e2430]"}`}>
            {payload.poster_url ? <img src={payload.poster_url} alt="" className="w-full h-full object-cover" /> : <div className="w-full h-full flex items-center justify-center text-slate-600 font-bold">TV</div>}
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-end pb-1 md:pb-2">
            {payload.year ? (
              <div className="text-xl font-semibold tabular-nums md:text-2xl" style={{ color: accent.icon }}>
                {payload.year}
              </div>
            ) : null}
            <h1 className={`mt-1 text-4xl font-black font-headline tracking-tight leading-[1.02] md:text-5xl lg:text-6xl ${isLight ? "text-slate-900" : "text-white"}`}>{payload.title}</h1>
          </div>
        </div>

        {payload.overview && <p className={`text-lg leading-relaxed max-w-5xl mb-8 ${isLight ? "text-slate-700" : "text-slate-200"}`}>{payload.overview}</p>}

        <SeriesFileStateSection
          seasons={payload.seasons || []}
          links={payload.arr_instance_links}
          arrLink={payload.arr_link}
          instanceLabel={payload.instance_label}
          isLight={isLight}
          brand={props.brand}
          accentHex={accent.hex}
          sonarrIconSrc={sonarrIcon}
        />

        <div className="grid grid-cols-2 sm:grid-cols-2 gap-4 mb-6">
          {[
            { label: "First Aired", value: payload.first_aired },
            { label: "Network", value: payload.network },
          ]
            .filter((m) => m.value != null && String(m.value).length > 0)
            .map((m) => (
              <div key={m.label} className={`rounded-xl border p-5 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}>
                <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-1">{m.label}</div>
                <div className={`text-base font-semibold tabular-nums ${isLight ? "text-slate-900" : "text-white"}`}>{m.value}</div>
              </div>
            ))}
        </div>

        <div className="mb-4">
          <h3 className="text-xs font-headline uppercase tracking-widest text-slate-500 mb-3">Seasons &amp; Episodes</h3>
        </div>
        <div className="space-y-2">
          {seasonsDesc.map(season => {
            const open = props.openSeasons.includes(season.id);
            return (
              <div key={season.id} className={`border rounded-xl overflow-hidden ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}>
                <button type="button" onClick={() => props.onToggleSeason(season.id)}
                  className={`w-full flex items-center justify-between px-5 py-4 text-left transition-colors ${isLight ? "hover:bg-slate-100" : "hover:bg-[#1e2430]/50"}`}>
                  <div className="flex items-center gap-3">
                    <span className="material-symbols-outlined text-slate-500 transition-transform" style={{ fontSize: 18, transform: open ? "rotate(90deg)" : "rotate(0deg)" }}>chevron_right</span>
                    <span className={`text-sm font-bold font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
                      {season.season_number === 0 ? "Specials" : `Season ${season.season_number}`}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 text-xs font-headline uppercase tracking-wider">
                    <span className="text-slate-500">{season.episode_total} episodes</span>
                    <span className="px-2 py-0.5 rounded bg-teal-600/20 border border-teal-500/30 text-teal-300">Placeholder {season.episode_placeholders}</span>
                    <span className="px-2 py-0.5 rounded bg-green-600/20 border border-green-500/30 text-green-300">Downloaded {season.episode_files}</span>
                  </div>
                </button>
                {open && (
                  <div className={`border-t divide-y ${isLight ? "border-slate-200 divide-slate-200" : "border-[#424753]/30 divide-[#424753]/15"}`}>
                    {season.episodes.map(ep => (
                      <div key={ep.id} className={`flex items-start gap-4 px-5 py-3 transition-colors ${isLight ? "hover:bg-slate-50" : "hover:bg-[#1e2430]/30"}`}>
                        <span className="flex-none w-10 text-xs text-slate-500 font-mono pt-0.5">E{String(ep.episode_number).padStart(2, "0")}</span>
                        <div className="flex-1 min-w-0">
                          <div className={`text-sm font-medium ${isLight ? "text-slate-900" : "text-white"}`}>{ep.title || `Episode ${ep.episode_number}`}</div>
                          <div className="ui-field-description-compact mt-0.5">{ep.air_date || "No air date"}</div>
                        </div>
                        <div className="flex-none">
                          {ep.has_placeholder
                            ? <span className="px-2 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-teal-600/20 border border-teal-500/30 text-teal-300">Placeholder</span>
                            : ep.has_file
                              ? <span className="px-2 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-green-600/20 border border-green-500/30 text-green-300">Downloaded</span>
                              : <span className="px-2 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-red-600/20 border border-red-500/30 text-red-300">Missing</span>
                          }
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function CalendarPanel(props: {
  payload: CalendarResponse | null;
  month: string;
  summary: { movieCount: number; episodeCount: number; inWindowCount: number };
  filters: CalendarFilters;
  selectedItem: CalendarDay["items"][number] | null;
  spotlight: MovieDetailResponse | SeriesDetailResponse | null;
  spotlightOpen: boolean;
  spotlightLoading: boolean;
  brand: Brand; themeMode: ThemeMode;
  onMonthChange: (month: string) => void;
  onSelectItem: (itemId: string) => void;
  onOpenSpotlightDetail: (item: CalendarDay["items"][number]) => void;
  onToggleSpotlight: (open: boolean) => void;
  onToggleFilter: (group: "mediaTypes" | "releaseTypes", key: string) => void;
}) {
  const payload = props.payload;
  const accent = getBrandAccent(props.brand, props.themeMode);

  const calendarGridRef = useRef<HTMLDivElement>(null);
  const overlayContainerRef = useRef<HTMLDivElement>(null);
  const releaseMenuRef = useRef<HTMLDivElement>(null);
  const [overlayOpen, setOverlayOpen] = useState(false);
  const [overlayExpanded, setOverlayExpanded] = useState(false);
  const [overlayAnchor, setOverlayAnchor] = useState({ x: 0, y: 0 });
  const [overlayPosition, setOverlayPosition] = useState({ top: 12, left: 12, width: 680 });
  const [releaseMenuOpen, setReleaseMenuOpen] = useState(false);
  const [calendarSpotlightExpandedEpisodeIds, setCalendarSpotlightExpandedEpisodeIds] = useState<number[]>([]);

  function calculateOverlayPosition(anchorX: number, anchorY: number) {
    const container = overlayContainerRef.current;
    if (!container) {
      return { top: 12, left: 12, width: 680 };
    }
    const rect = container.getBoundingClientRect();
    const maxWidth = Math.max(320, Math.min(760, rect.width - 24));
    const estimatedHeight = Math.min(Math.max(420, rect.height * 0.72), 620);

    let left = anchorX + 18;
    if (left + maxWidth > rect.width - 12) {
      left = anchorX - maxWidth - 18;
    }
    left = Math.max(12, Math.min(left, rect.width - maxWidth - 12));

    let top = anchorY - estimatedHeight * 0.35;
    top = Math.max(12, Math.min(top, rect.height - estimatedHeight - 12));

    return { top, left, width: maxWidth };
  }

  useEffect(() => {
    if (!overlayOpen || !props.selectedItem) {
      return;
    }
    setOverlayPosition(calculateOverlayPosition(overlayAnchor.x, overlayAnchor.y));
    setOverlayExpanded(false);
    // Wait for selected-item render so the card animates from click origin reliably.
    requestAnimationFrame(() => requestAnimationFrame(() => setOverlayExpanded(true)));
  }, [overlayOpen, props.selectedItem?.id, overlayAnchor.x, overlayAnchor.y]);

  useEffect(() => {
    if (!overlayOpen) {
      return;
    }
    const onResize = () => {
      setOverlayPosition(calculateOverlayPosition(overlayAnchor.x, overlayAnchor.y));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [overlayOpen, overlayAnchor.x, overlayAnchor.y]);

  useEffect(() => {
    if (!releaseMenuOpen) {
      return;
    }
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (releaseMenuRef.current && target && !releaseMenuRef.current.contains(target)) {
        setReleaseMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [releaseMenuOpen]);

  const calendarSpotlightEpisodeIds = useMemo(() => {
    if (!props.selectedItem || props.selectedItem.media_type !== "episode") return [] as number[];
    const grouped = props.selectedItem.group_episode_ids;
    if (grouped && grouped.length) return [...grouped];
    return [props.selectedItem.item_id];
  }, [props.selectedItem]);

  useEffect(() => {
    const item = props.selectedItem;
    if (!item || item.media_type !== "episode") {
      setCalendarSpotlightExpandedEpisodeIds([]);
      return;
    }
    const grouped = item.group_episode_ids;
    const ids = grouped && grouped.length ? [...grouped] : [item.item_id];
    setCalendarSpotlightExpandedEpisodeIds(ids.length === 1 ? ids : []);
  }, [props.selectedItem?.id]);

  function handleLocalSelectItem(itemId: string, e: React.MouseEvent) {
    const container = overlayContainerRef.current;
    if (container) {
      const containerRect = container.getBoundingClientRect();
      const targetRect = (e.currentTarget as HTMLElement).getBoundingClientRect();
      const anchorX = targetRect.left - containerRect.left + targetRect.width / 2;
      const anchorY = targetRect.top - containerRect.top + targetRect.height / 2;
      setOverlayAnchor({ x: anchorX, y: anchorY });
      setOverlayPosition(calculateOverlayPosition(anchorX, anchorY));
    }
    setOverlayExpanded(false);
    setOverlayOpen(true);
    props.onSelectItem(itemId);
  }

  function closeOverlay() {
    setOverlayExpanded(false);
    setTimeout(() => setOverlayOpen(false), 320);
  }

  function shiftMonth(delta: number) {
    const [y, m] = (payload?.month || props.month).split("-").map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    props.onMonthChange(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }

  if (!payload) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="flex items-center gap-3 text-slate-400">
          <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
          <span className="text-sm font-headline uppercase tracking-widest">Loading calendar...</span>
        </div>
      </div>
    );
  }

  const spotlightImage = props.spotlight
    ? (props.spotlight.backdrop_url || props.spotlight.poster_url)
    : null;

  const spotlightSeries = props.spotlight && "seasons" in props.spotlight ? (props.spotlight as SeriesDetailResponse) : null;
  const spotlightMovie = props.spotlight && !("seasons" in props.spotlight) ? (props.spotlight as MovieDetailResponse) : null;

  const spotlightDescription = (() => {
    if (props.spotlightLoading) return "Loading metadata...";
    if (!props.selectedItem) return "Select a release on the calendar to inspect it here.";
    if (props.selectedItem.media_type === "episode") {
      const text = spotlightSeries?.overview?.trim();
      return text || "No series synopsis available.";
    }
    const movieText = spotlightMovie?.overview?.trim();
    return movieText || props.selectedItem.reason || "Select a release on the calendar to inspect it here.";
  })();

  const spotlightArrLinks: { label: string; url: string }[] = (() => {
    const sp = props.spotlight;
    if (sp && Array.isArray(sp.arr_instance_links) && sp.arr_instance_links.length) {
      return sp.arr_instance_links.map((x) => ({ label: x.label, url: x.url }));
    }
    const single = sp?.arr_link || props.selectedItem?.arr_link;
    return single ? [{ label: spotlightMovie ? "Radarr" : spotlightSeries ? "Sonarr" : "Open app", url: single }] : [];
  })();
  const spotlightPoster = props.spotlight?.poster_url || null;
  const spotlightMeta = props.selectedItem ? formatCalendarSpotlightMeta(props.selectedItem) : [];
  const heroDateParts = props.selectedItem ? formatCalendarHeroDateParts(props.selectedItem.release_date) : null;
  const calendarSpotlightEpisodeRows =
    props.selectedItem?.media_type === "episode"
      ? collectEpisodesForCalendarDay(spotlightSeries, calendarSpotlightEpisodeIds)
      : [];
  const activeMediaFilters = Object.values(props.filters.mediaTypes).filter(Boolean).length;
  const activeReleaseFilters = Object.values(props.filters.releaseTypes).filter(Boolean).length;
  const totalVisible = props.summary.movieCount + props.summary.episodeCount;

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-2 mb-1">
        <div className="w-2 h-2 rounded-full" style={{ backgroundColor: accent.hex }} />
        <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">Upcoming Releases</span>
      </div>
      <div className="flex justify-between items-center mb-5">
        <h1 className="text-3xl font-black text-white tracking-tight font-headline">Release Calendar</h1>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => shiftMonth(-1)}
            className="w-8 h-8 flex items-center justify-center bg-[#1e2430] border border-[#424753]/40 rounded-lg text-slate-400 hover:text-white hover:border-slate-400 transition-colors">
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>chevron_left</span>
          </button>
          <span className="text-sm font-bold text-white font-headline px-3 min-w-36 text-center">{payload.month_label}</span>
          <button type="button" onClick={() => shiftMonth(1)}
            className="w-8 h-8 flex items-center justify-center bg-[#1e2430] border border-[#424753]/40 rounded-lg text-slate-400 hover:text-white hover:border-slate-400 transition-colors">
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>chevron_right</span>
          </button>
          <button type="button" onClick={() => props.onMonthChange(getCurrentMonthToken())}
            className="ml-1 px-3 py-1.5 bg-[#1e2430] border border-[#424753]/40 rounded-lg text-xs text-slate-400 hover:text-white font-headline uppercase tracking-wider transition-colors">
            Today
          </button>
        </div>
      </div>

      {/* Filter legend */}
      <div className="flex flex-wrap gap-2 mb-5 p-3 bg-[#171c22] border border-[#424753]/40 rounded-xl">
        <span className="text-[10px] font-headline uppercase tracking-widest text-slate-500 self-center mr-2">Filter by</span>
        {payload.legend.media_types.map(item => {
          const active = props.filters.mediaTypes[item.key] !== false;
          const mediaIcon = item.key === "movie" ? "movie" : "tv";
          return (
            <button key={`media-${item.key}`} type="button" onClick={() => props.onToggleFilter("mediaTypes", item.key)}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-headline uppercase tracking-wider border transition-colors ${active ? "" : "bg-[#252e3a] border-[#424753]/40 text-slate-500 hover:text-slate-300"}`}
              style={active ? { backgroundColor: alphaColor(accent.hex, 0.18), borderColor: alphaColor(accent.hex, 0.45), color: accent.text } : undefined}>
              <span className="material-symbols-outlined" style={{ fontSize: 14 }}>{mediaIcon}</span>
              {item.label}
            </button>
          );
        })}
        <div className="relative" ref={releaseMenuRef}>
          <button
            type="button"
            onClick={() => setReleaseMenuOpen((prev) => !prev)}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-headline uppercase tracking-wider border transition-colors ${releaseMenuOpen ? "bg-teal-600/20 border-teal-500/50 text-teal-300" : "bg-[#252e3a] border-[#424753]/40 text-slate-300 hover:text-white"}`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 14 }}>tune</span>
            Movie release types
            <span className="material-symbols-outlined" style={{ fontSize: 14 }}>expand_more</span>
          </button>
          {releaseMenuOpen ? (
            <div className="absolute left-0 mt-2 w-64 rounded-xl border border-[#424753]/40 bg-[#111722] shadow-2xl z-20 p-2">
              {payload.legend.movie_release_types.map((item) => {
                const active = props.filters.releaseTypes[item.key] !== false;
                return (
                  <label
                    key={`rel-menu-${item.key}`}
                    className="flex items-center gap-2 px-2 py-2 rounded-md text-xs text-slate-300 hover:bg-[#1b2433] cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={active}
                      onChange={() => props.onToggleFilter("releaseTypes", item.key)}
                      className="h-3.5 w-3.5 rounded border-[#424753]/60 bg-[#0f141c] text-teal-400 focus:ring-0"
                    />
                    <span className="font-headline uppercase tracking-wider">{item.label}</span>
                  </label>
                );
              })}
            </div>
          ) : null}
        </div>
        <span className="ml-auto text-[10px] font-headline text-slate-500 self-center">
          {props.summary.movieCount} movies · {props.summary.episodeCount} TV episodes
        </span>
      </div>

      <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-[#424753]/40 bg-[#171c22] px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500">Calendar Health</div>
          <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-slate-300">
            <span><span className="text-white font-semibold">{totalVisible}</span> visible releases</span>
            <span><span className="text-white font-semibold">{props.summary.inWindowCount}</span> in lookahead</span>
            <span><span className="text-white font-semibold">{activeMediaFilters}</span> media filters active</span>
            <span><span className="text-white font-semibold">{activeReleaseFilters}</span> release filters active</span>
          </div>
          <div className="ui-field-description mt-1">{payload.lookahead.label}</div>
        </div>
      </div>

      {/* Calendar grid — full width, overlay hovers above it */}
      <div className="relative" ref={overlayContainerRef}>
        <div className="overflow-x-auto rounded-xl border border-[#424753]/40 bg-[#171c22]" ref={calendarGridRef}>
          <div className="min-w-[980px] overflow-hidden rounded-xl">
            <div className="grid grid-cols-7 border-b border-[#424753]/30">
              {payload.weekday_labels.map(w => (
                <div key={w} className="px-3 py-3 text-center text-[10px] font-headline uppercase tracking-widest text-slate-500">{w}</div>
              ))}
            </div>
            {payload.weeks.map((week, idx) => (
              <div key={idx} className="grid grid-cols-7 border-b border-[#424753]/20 last:border-b-0">
                {week.map(day => (
                  <CalendarDayCell
                    key={day.iso_date}
                    day={day}
                    filters={props.filters}
                    brand={props.brand}
                    themeMode={props.themeMode}
                    selectedItemId={props.selectedItem?.id || null}
                    onSelectItem={handleLocalSelectItem}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>

        {/* Animated floating overlay — replaces the old side pane */}
        {overlayOpen && props.selectedItem ? (
          <>
            {/* Backdrop */}
            <div
              className="absolute inset-0 rounded-xl"
              style={{ background: "rgba(5,8,14,0.55)", zIndex: 45, backdropFilter: "blur(2px)" }}
              onClick={closeOverlay}
            />
            {/* Card */}
            <div
              className="absolute pointer-events-auto calendar-spotlight-overlay"
              style={{
                top: overlayPosition.top,
                left: overlayPosition.left,
                width: overlayPosition.width,
                maxWidth: "calc(100% - 24px)",
                zIndex: 50,
                borderRadius: 20,
                overflow: "hidden",
                background: "#0b1017",
                border: `1px solid ${alphaColor(accent.hex, 0.3)}`,
                boxShadow: `0 32px 80px rgba(0,0,0,0.6), 0 0 0 1px ${alphaColor(accent.hex, 0.15)}`,
                transformOrigin: `${Math.max(0, Math.min(100, ((overlayAnchor.x - overlayPosition.left) / Math.max(overlayPosition.width, 1)) * 100))}% ${Math.max(0, Math.min(100, ((overlayAnchor.y - overlayPosition.top) / 560) * 100))}%`,
                transform: overlayExpanded ? "scale(1)" : "scale(0.84)",
                opacity: overlayExpanded ? 1 : 0,
                transition: "transform 0.34s cubic-bezier(0.2,0.9,0.2,1), opacity 0.2s ease",
              }}
            >
              {/* Hero image */}
              <div className="relative h-64 md:h-72 bg-[#0a0e14]">
                {spotlightImage ? (
                  <div
                    className="absolute inset-0 bg-cover bg-center"
                    style={{ backgroundImage: `linear-gradient(180deg, rgba(5,8,14,0.06) 0%, rgba(5,8,14,0.7) 64%, rgba(5,8,14,1) 100%), url(${spotlightImage})` }}
                  />
                ) : (
                  <div className="absolute inset-0" style={{ background: `linear-gradient(135deg, ${alphaColor(accent.hex, 0.18)}, rgba(5,8,14,0.9))` }} />
                )}
                {heroDateParts ? (
                  <div
                    className="absolute top-3 left-3 z-[2] flex w-[3.25rem] flex-col items-center rounded-lg border border-white/25 bg-black/60 px-1.5 py-2 shadow-lg backdrop-blur-sm"
                    aria-hidden
                  >
                    <span className="text-[9px] font-headline font-bold uppercase tracking-[0.12em] text-orange-100/90">
                      {heroDateParts.month}
                    </span>
                    <span className="mt-0.5 text-[1.65rem] font-black leading-none text-white font-headline tabular-nums">
                      {heroDateParts.day}
                    </span>
                  </div>
                ) : null}
                {/* Close button */}
                <button
                  type="button"
                  onClick={closeOverlay}
                  className="absolute top-3 right-3 z-[2] flex h-8 w-8 items-center justify-center rounded-full bg-black/50 text-slate-300 hover:text-white hover:bg-black/70 transition-colors calendar-spotlight-close"
                >
                  <span className="material-symbols-outlined" style={{ fontSize: 18 }}>close</span>
                </button>
                <div className="absolute inset-x-0 bottom-0 z-[1] p-5 md:p-6">
                  <div className="flex items-end gap-4 md:gap-5">
                    <div className="hidden md:block flex-none w-24 h-36 rounded-xl overflow-hidden border border-white/20 shadow-2xl bg-[#151b25]">
                      {spotlightPoster ? (
                        <img src={spotlightPoster} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <div className="h-full w-full flex items-center justify-center text-slate-500 font-headline text-xs uppercase">
                          {props.selectedItem.media_type === "movie" ? "MOV" : "TV"}
                        </div>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <span
                          className={
                            props.selectedItem.media_type === "movie"
                              ? "rounded border border-teal-500/40 bg-teal-600/25 px-2.5 py-1 text-[10px] font-bold font-headline uppercase tracking-wider text-teal-100"
                              : "rounded border border-orange-500/40 bg-orange-600/25 px-2.5 py-1 text-[10px] font-bold font-headline uppercase tracking-wider text-orange-100"
                          }
                        >
                          {props.selectedItem.media_type === "movie" ? "Movie" : "TV"}
                        </span>
                        {props.selectedItem.release_type_label ? (
                          <span className="rounded border border-teal-500/30 bg-teal-600/20 px-2.5 py-1 text-[10px] font-bold font-headline uppercase tracking-wider text-teal-200">
                            {props.selectedItem.release_type_label}
                          </span>
                        ) : null}
                      </div>
                      <h3 className="text-2xl md:text-3xl font-black tracking-tight text-white font-headline leading-tight">
                        {props.selectedItem.title}
                      </h3>
                      {props.selectedItem.media_type === "movie" && props.selectedItem.subtitle ? (
                        <p className="mt-1 text-sm text-slate-300 truncate">{props.selectedItem.subtitle}</p>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>

              {/* Body */}
              <div className="space-y-5 p-5 md:p-6">
                <p className="text-base leading-relaxed text-slate-200">
                  {spotlightDescription}
                </p>

                {props.selectedItem.media_type === "episode" ? (
                  props.spotlightLoading ? (
                    <div className="text-sm text-slate-500">Loading episodes…</div>
                  ) : calendarSpotlightEpisodeRows.length ? (
                    <div className="overflow-hidden rounded-xl border border-[#424753]/30 bg-black/25">
                      {calendarSpotlightEpisodeRows.map((ep, idx) => {
                        const expanded = calendarSpotlightExpandedEpisodeIds.includes(ep.id);
                        const code = `S${String(ep.season_number).padStart(2, "0")}E${String(ep.episode_number).padStart(2, "0")}`;
                        const epTitle = ep.title?.trim() || `Episode ${ep.episode_number}`;
                        return (
                          <div
                            key={ep.id}
                            className={idx > 0 ? "border-t border-[#424753]/25" : undefined}
                          >
                            <button
                              type="button"
                              onClick={() => {
                                setCalendarSpotlightExpandedEpisodeIds((prev) =>
                                  prev.includes(ep.id) ? prev.filter((x) => x !== ep.id) : [...prev, ep.id],
                                );
                              }}
                              className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-white/[0.04]"
                            >
                              <span className="flex-none font-mono text-[11px] font-semibold text-orange-200/95">{code}</span>
                              <span className="min-w-0 flex-1 text-[13px] font-medium leading-snug text-slate-100">{epTitle}</span>
                              <span
                                className={`material-symbols-outlined flex-none text-slate-500 transition-transform ${expanded ? "rotate-180" : ""}`}
                                style={{ fontSize: 20 }}
                              >
                                expand_more
                              </span>
                            </button>
                            {expanded ? (
                              <div className="border-t border-[#424753]/20 bg-black/20 px-3 pb-3 pt-2">
                                {ep.still_url ? (
                                  <div className="mb-3 overflow-hidden rounded-lg border border-[#424753]/35 bg-[#0a0e14]">
                                    <img
                                      src={ep.still_url}
                                      alt=""
                                      className="max-h-48 w-full object-cover object-center"
                                    />
                                  </div>
                                ) : null}
                                <p className="text-sm leading-relaxed text-slate-300">
                                  {ep.overview?.trim() || "No episode synopsis available."}
                                </p>
                              </div>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="text-sm text-slate-500">Episode details not available.</div>
                  )
                ) : null}

                {spotlightMeta.length ? (
                  <div className="grid grid-cols-2 gap-2 rounded-xl border border-[#424753]/30 bg-black/30 p-3 calendar-spotlight-meta">
                    {spotlightMeta.map((bit) => (
                      <div key={bit.label} className="text-xs">
                        <div className="font-headline uppercase tracking-widest text-slate-500 mb-0.5">{bit.label}</div>
                        <div className="text-slate-300">{bit.value}</div>
                      </div>
                    ))}
                  </div>
                ) : null}

                <div className="flex flex-wrap gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => { closeOverlay(); props.onOpenSpotlightDetail(props.selectedItem!); }}
                    className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-xs font-headline uppercase tracking-wider text-white transition-colors"
                    style={{ backgroundColor: accent.hex }}
                  >
                    <span className="material-symbols-outlined" style={{ fontSize: 14 }}>open_in_new</span>
                    Full Detail
                  </button>
                  {spotlightArrLinks.map((lnk) => (
                    <a
                      key={lnk.url}
                      href={lnk.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1.5 rounded-lg border border-[#424753]/40 bg-[#1e2430] px-4 py-2 text-xs font-headline uppercase tracking-wider text-slate-300 transition-colors hover:text-white"
                    >
                      <span className="material-symbols-outlined" style={{ fontSize: 14 }}>north_east</span>
                      {lnk.label}
                    </a>
                  ))}
                </div>
              </div>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

function CalendarDayCell(props: {
  day: CalendarDay;
  filters: CalendarFilters;
  selectedItemId: string | null;
  brand: Brand; themeMode: ThemeMode;
  onSelectItem: (itemId: string, e: React.MouseEvent) => void;
}) {
  const visibleItems = props.day.items.filter(item => isCalendarItemVisible(item, props.filters));
  const { day } = props;
  const accent = getBrandAccent(props.brand, props.themeMode);
  const lookaheadFillOpacity = props.themeMode === "light" ? 0.16 : 0.08;
  const dayCellStyle = day.is_today
    ? { backgroundColor: alphaColor(accent.hex, 0.1) }
    : day.in_lookahead_window
      ? { backgroundColor: alphaColor(accent.hex, lookaheadFillOpacity) }
      : undefined;
  return (
    <div className={`min-h-[124px] p-2.5 border-r border-[#424753]/20 last:border-r-0 transition-colors ${
      !day.is_current_month ? "opacity-35" : ""
    } ${day.is_today ? "" : "hover:bg-[#1e2430]/50"}`} style={dayCellStyle}>
      {/* Day number */}
      <div className="flex items-center justify-between mb-1">
        <span className={`text-xs font-bold font-headline leading-none ${
          day.is_today ? "w-5 h-5 flex items-center justify-center rounded-full text-white text-[10px]" : "text-slate-400"
        }`} style={day.is_today ? { backgroundColor: accent.hex } : undefined}>
          {day.day_number}
        </span>
      </div>
      {/* Items */}
      <div className="space-y-1">
        {visibleItems.map(item => {
          const metaBits = formatCalendarItemMeta(item);
          // Movies: one fixed teal/green accent for every release type (matches prior "digital" look).
          // TV: fixed orange, independent of app theme — same idea as movies.
          const releaseColor = item.media_type === "movie" ? "border-l-teal-400" : "border-l-orange-400";
          const isSelected = props.selectedItemId === item.id;

          return (
            <button
              key={item.id}
              type="button"
              onClick={(e) => props.onSelectItem(item.id, e)}
              className={`w-full rounded-md border border-[#424753]/30 border-l-[3px] ${releaseColor} px-2 py-1.5 text-left transition-colors ${
                isSelected ? "bg-[#2a3344]" : "bg-[#252c38]/80 hover:bg-[#2a3344]"
              }`}
            >
              <div className="flex items-start gap-1.5">
                <span
                  className={`material-symbols-outlined mt-0.5 text-[12px] ${
                    item.media_type === "movie" ? "text-teal-400" : "text-orange-300"
                  }`}
                >
                  {item.media_type === "movie" ? "movie" : "tv"}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[11.5px] font-semibold leading-snug text-white" title={item.title}>{item.title}</div>
                  {item.media_type === "episode" && item.subtitle ? (
                    <div className="mt-0.5 text-[10px] leading-snug text-slate-400">{item.subtitle}</div>
                  ) : null}
                  {metaBits.length ? <div className="mt-0.5 text-[10px] leading-snug text-slate-500">{metaBits.map((bit) => bit.value).join(" • ")}</div> : null}
                </div>
              </div>
            </button>
          );
        })}
        {day.item_count > visibleItems.length ? (
          <div className="px-1 text-[10px] text-slate-500">+{day.item_count - visibleItems.length} hidden by filters</div>
        ) : null}
      </div>
    </div>
  );
}

function ErrorsPanel(props: { rows: ErrorRow[]; brand: Brand; themeMode: ThemeMode }) {
  const severityColor: Record<string, string> = {
    critical:  "bg-red-600 text-white",
    error:     "bg-red-500 text-white",
    io_err:    "bg-orange-600 text-white",
    timeout:   "bg-yellow-600 text-white",
    warning:   "bg-purple-600 text-white",
    warn:      "bg-purple-600 text-white",
  };
  const accent = getBrandAccent(props.brand, props.themeMode);

  return (
    <div>
      {/* Status bar */}
      <div className="flex items-center gap-2 mb-1">
        <div className="w-2 h-2 rounded-full bg-green-500" />
        <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">System Online</span>
      </div>

      {/* Title row */}
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-black text-white tracking-tight font-headline">Diagnostics</h1>
        <div className="flex bg-[#1e2430] rounded-lg border border-[#424753]/40 p-0.5">
          <button type="button" className="px-4 py-1.5 rounded-md bg-[#252e3a] text-white text-xs font-headline uppercase tracking-wider">Errors</button>
          <button type="button" className="px-4 py-1.5 text-slate-400 hover:text-slate-200 text-xs font-headline uppercase tracking-wider transition-colors">Logs</button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex gap-3 mb-4 flex-wrap">
        <div className="flex-1 min-w-48 relative">
          <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" style={{ fontSize: 16 }}>filter_list</span>
          <input className={`w-full bg-[#1e2430] border border-[#424753]/40 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-300 placeholder-slate-500 outline-none ${getBrandFocusClass(props.brand, props.themeMode)}`}
            placeholder="Filter by source or message keyword..." />
        </div>
        <div className="relative">
          <select className="appearance-none bg-[#1e2430] border border-[#424753]/40 rounded-lg px-3 py-2 pr-8 text-sm text-slate-300 outline-none">
            <option>All Severities</option>
            <option>Critical</option>
            <option>Warning</option>
          </select>
          <span className="material-symbols-outlined absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" style={{ fontSize: 16 }}>expand_more</span>
        </div>
        <div className="relative">
          <select className="appearance-none bg-[#1e2430] border border-[#424753]/40 rounded-lg px-3 py-2 pr-8 text-sm text-slate-300 outline-none">
            <option>Last 60 Minutes</option>
            <option>Last 24 Hours</option>
            <option>Last 7 Days</option>
          </select>
          <span className="material-symbols-outlined absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" style={{ fontSize: 16 }}>history</span>
        </div>
      </div>

      {/* Error table */}
      <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden mb-4">
        {!props.rows.length ? (
          <div className="p-10 text-center text-slate-500 text-sm">No errors found.</div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-[#424753]/30">
                {["Timestamp", "Source", "Label", "Message", "Action"].map(h => (
                  <th key={h} className="px-5 py-3 text-left text-[10px] font-headline uppercase tracking-widest text-slate-500 font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-[#424753]/15">
              {props.rows.map((row, idx) => {
                const labelKey = (row.label || "").toLowerCase().replace(/\s+/g, "_");
                const badgeClass = severityColor[labelKey] || "bg-slate-600 text-white";
                return (
                  <tr key={`${row.source}-${idx}`} className="hover:bg-[#1e2430]/40 transition-colors">
                    <td className="px-5 py-4 text-sm text-slate-400 font-mono whitespace-nowrap">{row.time || "--"}</td>
                    <td className="px-5 py-4 text-sm text-slate-300">{row.source}</td>
                    <td className="px-5 py-4">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold font-headline uppercase tracking-wider ${badgeClass}`}>{row.label}</span>
                    </td>
                    <td className="px-5 py-4 text-sm text-slate-400 max-w-xs truncate" title={row.error}>{row.error}</td>
                    <td className="px-5 py-4">
                      <button type="button" className="text-[10px] font-headline uppercase tracking-widest text-slate-400 hover:text-slate-200 transition-colors">Details</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Live log stream */}
      <div className="bg-[#0a0e14] rounded-xl border border-[#424753]/40 p-4 mb-6">
        <div className="flex justify-between items-center mb-3">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-red-500" />
            <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">Live Log Stream</span>
          </div>
          <span className="text-[10px] font-headline uppercase tracking-widest text-slate-500">Status: Monitoring</span>
        </div>
        <div className="font-mono text-xs space-y-1.5 text-slate-400 max-h-32 overflow-y-auto">
          <div><span style={{ color: accent.icon }}>[INFO]</span> System polling active</div>
          <div><span className="text-green-400">[SUCCESS]</span> Handshake established. Protocol V4.</div>
          <div><span style={{ color: accent.icon }}>[INFO]</span> Checking database consistency...</div>
          {props.rows.slice(0, 3).map((row, i) => (
            <div key={i}><span className="text-red-400">[ERROR]</span> {row.source}: {row.error}</div>
          ))}
        </div>
      </div>

      {/* Footer stat cards */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="flex justify-between items-start">
            <div className="text-[10px] font-headline uppercase tracking-widest text-slate-400 mb-3">Errors (24h)</div>
            <span className="material-symbols-outlined text-slate-600" style={{ fontSize: 18 }}>trending_up</span>
          </div>
          <div className="text-3xl font-black text-white font-headline">{props.rows.length}</div>
        </div>
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="flex justify-between items-start">
            <div className="text-[10px] font-headline uppercase tracking-widest text-slate-400 mb-3">Health Score</div>
            <span className="material-symbols-outlined" style={{ fontSize: 18, color: accent.icon }}>verified_user</span>
          </div>
          <div className="text-3xl font-black text-white font-headline">{props.rows.length === 0 ? "100%" : `${Math.max(0, 100 - props.rows.length * 2).toFixed(1)}%`}</div>
        </div>
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="flex justify-between items-start">
            <div className="text-[10px] font-headline uppercase tracking-widest text-slate-400 mb-3">Log Volume</div>
            <span className="material-symbols-outlined text-slate-600" style={{ fontSize: 18 }}>bar_chart</span>
          </div>
          <div className="text-3xl font-black text-white font-headline">{props.rows.length} entries</div>
        </div>
      </div>
    </div>
  );
}

function LogsPanel(props: {
  lines: string[];
  logFile: string;
  logLevel: "all" | "debug" | "info" | "warn" | "error" | "critical";
  logFilter: string;
  brand: Brand;
  themeMode: ThemeMode;
  onLevelChange: (value: "all" | "debug" | "info" | "warn" | "error" | "critical") => void;
  onFilterChange: (value: string) => void;
}) {
  const accent = getBrandAccent(props.brand, props.themeMode);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoFollowRef = useRef(true);

  useLayoutEffect(() => {
    const el = streamRef.current;
    if (!el || !shouldAutoFollowRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [props.lines]);

  const handleStreamScroll = () => {
    const el = streamRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - (el.scrollTop + el.clientHeight);
    // Treat near-bottom as "follow mode" to avoid jitter from fractional pixels.
    shouldAutoFollowRef.current = distanceFromBottom <= 24;
  };

  return (
    <div>
      {/* Title row */}
      <div className="flex items-center gap-2 mb-1">
        <div className="w-2 h-2 rounded-full" style={{ backgroundColor: accent.hex }} />
        <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">Live</span>
      </div>
      <div className="flex justify-between items-center mb-5">
        <h1 className="text-3xl font-black text-white tracking-tight font-headline">System Logs</h1>
        <span className="text-xs text-slate-500 font-mono">{props.logFile}</span>
      </div>

      {/* Filter bar */}
      <div className="flex gap-3 mb-4 flex-wrap">
        <div className="flex-1 min-w-48 relative">
          <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" style={{ fontSize: 16 }}>search</span>
          <input value={props.logFilter} onChange={e => props.onFilterChange(e.target.value)}
            className={`w-full bg-[#1e2430] border border-[#424753]/40 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-300 placeholder-slate-500 outline-none ${getBrandFocusClass(props.brand, props.themeMode)}`}
            placeholder="Search log output..." />
        </div>
        <div className="relative">
          <select value={props.logLevel} onChange={e => props.onLevelChange(e.target.value as "all" | "debug" | "info" | "warn" | "error" | "critical")}
            className="appearance-none bg-[#1e2430] border border-[#424753]/40 rounded-lg px-3 py-2 pr-8 text-sm text-slate-300 outline-none">
            <option value="all">All Levels</option>
            <option value="debug">Debug + Above</option>
            <option value="info">Info + Above</option>
            <option value="warn">Warnings + Above</option>
            <option value="error">Errors + Above</option>
            <option value="critical">Critical Only</option>
          </select>
          <span className="material-symbols-outlined absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" style={{ fontSize: 16 }}>expand_more</span>
        </div>
      </div>

      {/* Log terminal */}
      <div className="bg-[#0a0e14] rounded-xl border border-[#424753]/40 p-5">
        <div className="flex justify-between items-center pb-3 mb-3 border-b border-[#424753]/30">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-slate-500" style={{ fontSize: 18 }}>terminal</span>
            <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">Output Stream</span>
          </div>
          <span className="text-[10px] font-headline uppercase tracking-widest text-slate-500">{props.lines.length} lines</span>
        </div>
        <div
          ref={streamRef}
          onScroll={handleStreamScroll}
          className="font-mono text-xs space-y-1 max-h-[60vh] overflow-y-auto"
        >
          {!props.lines.length && <div className="text-slate-600 p-2">No log lines to display.</div>}
          {props.lines.map((line, idx) => {
            const isError = line.includes("ERROR") || line.includes("CRITICAL");
            const isWarn = line.includes("WARNING") || line.includes("WARN");
            const isInfo = line.includes("INFO");
            const colorClass = isError ? "text-red-400" : isWarn ? "text-yellow-400" : isInfo ? "text-slate-400" : "text-slate-500";
            const prefixClass = isError ? "text-red-500" : isWarn ? "text-yellow-500" : isInfo ? "" : "text-slate-600";
            return (
              <div key={idx} className={`${colorClass} leading-relaxed whitespace-pre-wrap break-all`}>
                <span className={`${prefixClass} select-none`} style={isInfo ? { color: accent.icon } : undefined}>{String(idx + 1).padStart(4, "\u00a0")} </span>
                {line}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

type ArrInstanceDraft = {
  id: string;
  instance_id: string;
  label: string;
  arr_type: "radarr" | "sonarr";
  instance_key: string;
  instance_key_aliases?: string[];
  url: string;
  api_key: string;
  role: "primary" | "secondary" | "additional";
  priority: number;
  is_4k: boolean;
};

const ARR_INSTANCE_LIMIT_PER_TYPE = 2;

function normalizeInstanceKey(input: string) {
  return String(input || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^[_-]+|[_-]+$/g, "");
}

/** True when `instance_id` embeds a UUID, matching `services.app_config._arr_instance_id_has_uuid`. */
function arrInstanceIdEmbedsUuid(instanceId: string): boolean {
  let hyphens = 0;
  for (const ch of String(instanceId || "")) {
    if (ch === "-") hyphens += 1;
  }
  return hyphens >= 4;
}

function slotWebhookRoleFromRowRole(role: string): "primary" | "secondary" {
  const r = String(role || "").trim().toLowerCase();
  if (r === "secondary" || r === "additional") return "secondary";
  return "primary";
}

function stableArrInstanceId(arrType: "radarr" | "sonarr", slotRole: "primary" | "secondary") {
  return `${arrType}_${slotRole}`;
}

function buildArrInstanceWebhookUrls(origin: string, instance_id: string, instance_key: string) {
  const id = String(instance_id || "").trim().toLowerCase();
  const key = normalizeInstanceKey(String(instance_key || ""));
  const byId = id ? `${origin}/webhook?instance_id=${encodeURIComponent(id)}` : "";
  const byKey = `${origin}/webhook?instance=${encodeURIComponent(key)}`;
  const uuidLike = id.length > 0 && arrInstanceIdEmbedsUuid(id);
  const primary = id ? byId : byKey;
  return { primary, byId: id ? byId : "", byKey, uuidLike };
}

function inferDefaultKey(label: string, arrType: "radarr" | "sonarr") {
  const slug = normalizeInstanceKey(label);
  return slug || `${arrType}_instance`;
}

function normalizeInstanceRole(input: unknown, rank: number): "primary" | "secondary" | "additional" {
  const role = String(input || "").trim().toLowerCase();
  if (role === "primary" || role === "secondary" || role === "additional") return role;
  if (rank <= 0) return "primary";
  if (rank === 1) return "secondary";
  return "additional";
}

function deriveIs4kFromRole(role: string) {
  return role !== "primary";
}

function getPlexLibraryIdNote(fieldKey: string) {
  if (fieldKey === "PLEX_MOVIE_SECTION_ID") return "Use the Plex library ID for the placeholder movie library that points at your derived `movies` path.";
  if (fieldKey === "PLEX_TV_SECTION_ID") return "Use the Plex library ID for the placeholder TV library that points at your derived `tv` path.";
  return null;
}

function parseArrInstancesFromValues(values: FieldValueMap): ArrInstanceDraft[] {
  const raw = String(values.ARR_INSTANCES_JSON || "").trim();
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        const rankByType: Record<"radarr" | "sonarr", number> = { radarr: 0, sonarr: 0 };
        const items = parsed
          .filter((item) => item && typeof item === "object")
          .map((item, index) => {
            const obj = item as Record<string, unknown>;
            const arrType = String(obj.arr_type || obj.type || "").toLowerCase() === "sonarr" ? "sonarr" : "radarr";
            const label = String(obj.label || obj.instance_key || obj.key || obj.name || `${arrType} ${index + 1}`);
            const rank = rankByType[arrType];
            rankByType[arrType] = rank + 1;
            const role = normalizeInstanceRole(obj.role, rank);
            const instanceKey = normalizeInstanceKey(String(obj.instance_key || obj.key || obj.name || inferDefaultKey(label, arrType)));
            const slotRole = slotWebhookRoleFromRowRole(role);
            const rawInstanceId = String(obj.instance_id || obj.id || "").trim().toLowerCase();
            const instanceId = rawInstanceId || stableArrInstanceId(arrType, slotRole);
            const aliasRaw = Array.isArray(obj.instance_key_aliases) ? obj.instance_key_aliases : [];
            const instance_key_aliases = aliasRaw
              .map((a) => normalizeInstanceKey(String(a)))
              .filter((a) => a && a !== instanceKey);
            return {
              id: instanceId || `json-${arrType}-${index}`,
              instance_id: instanceId || `json-${arrType}-${index}`,
              label,
              arr_type: arrType,
              instance_key: instanceKey,
              instance_key_aliases,
              url: String(obj.url || ""),
              api_key: String(obj.api_key || obj.apikey || ""),
              role,
              priority: Number.isFinite(Number(obj.priority)) ? Number(obj.priority) : rank,
              is_4k: deriveIs4kFromRole(role),
            } satisfies ArrInstanceDraft;
          });
        if (items.length) return items;
      }
    } catch {
      return [];
    }
  }
  return [];
}

function serializeArrInstances(instances: ArrInstanceDraft[]) {
  const rankByType: Record<"radarr" | "sonarr", number> = { radarr: 0, sonarr: 0 };
  const clean = instances
    .map((row) => {
      const label = String(row.label || "").trim();
      const existingKey = normalizeInstanceKey(String(row.instance_key || ""));
      const inferredKey = existingKey || normalizeInstanceKey(inferDefaultKey(label, row.arr_type));
      const rank = rankByType[row.arr_type];
      rankByType[row.arr_type] = rank + 1;
      const role = normalizeInstanceRole(row.role, rank);
      const slotRole = slotWebhookRoleFromRowRole(role);
      const instanceId =
        String(row.instance_id || "").trim().toLowerCase() || stableArrInstanceId(row.arr_type, slotRole);
      const aliasList = (row.instance_key_aliases || [])
        .map((a) => normalizeInstanceKey(String(a)))
        .filter((a) => a && a !== inferredKey);
      const out: Record<string, unknown> = {
        instance_id: instanceId,
        arr_type: row.arr_type,
        instance_key: inferredKey,
        label,
        url: String(row.url || "").trim(),
        api_key: String(row.api_key || "").trim(),
        role,
        priority: rank,
        is_4k: deriveIs4kFromRole(role),
      };
      if (aliasList.length) {
        out.instance_key_aliases = aliasList;
      }
      return out;
    })
    .filter((row) => row.instance_key && row.url && row.api_key);
  return JSON.stringify(clean);
}

/** Normalize URL for duplicate detection across Radarr/Sonarr instances (host, port, path). */
function normalizeArrInstanceUrlForDedupe(url: string): string {
  const t = url.trim();
  if (!t) return "";
  const href = t.includes("://") ? t : `http://${t}`;
  try {
    const u = new URL(href);
    const host = u.hostname.toLowerCase();
    const port = u.port || (u.protocol === "https:" ? "443" : u.protocol === "http:" ? "80" : "");
    const path = u.pathname.replace(/\/+$/, "") || "";
    return `${host}:${port}${path}`;
  } catch {
    return t.toLowerCase().replace(/\/+$/, "");
  }
}

/** If candidateUrl matches another row's URL, return that row (excluding self by draft id). */
function findDuplicateArrInstanceUrl(
  instances: ArrInstanceDraft[],
  selfId: string,
  candidateUrl: string,
): ArrInstanceDraft | null {
  const n = normalizeArrInstanceUrlForDedupe(candidateUrl);
  if (!n) return null;
  for (const row of instances) {
    if (row.id === selfId) continue;
    const other = normalizeArrInstanceUrlForDedupe(String(row.url || ""));
    if (!other) continue;
    if (other === n) return row;
  }
  return null;
}

/** Primary row has URL + API key in persisted settings (e.g. after refresh or step save). */
function arrPrimaryPersistedWithCredentials(values: FieldValueMap, arrType: "radarr" | "sonarr"): boolean {
  const instances = parseArrInstancesFromValues(values);
  const first = instances.find((row) => row.arr_type === arrType);
  if (!first) return false;
  return String(first.url || "").trim().length > 0 && String(first.api_key || "").trim().length > 0;
}

const PLACEHOLDER_MODE_VALUES = new Set(["primary", "secondary", "both"]);
const PLAYBACK_MODE_VALUES = new Set(["match", "primary", "secondary", "both"]);

function buildPersistableSettingsValues(values: FieldValueMap, payload: SettingsPayload | null) {
  const allowedKeys = new Set<string>(
    payload ? payload.sections.flatMap((section) => section.fields.map((field) => field.key)) : Object.keys(values),
  );
  const cleaned: FieldValueMap = {};

  Object.entries(values).forEach(([key, value]) => {
    if (allowedKeys.has(key) && !key.startsWith("WIZARD_")) {
      cleaned[key] = value;
    }
  });

  const instances = parseArrInstancesFromValues(cleaned);
  const hasRadarrSecondary = instances.filter((item) => item.arr_type === "radarr").length > 1;
  const hasSonarrSecondary = instances.filter((item) => item.arr_type === "sonarr").length > 1;

  const moviePlaceholderMode = String(cleaned.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "primary").trim().toLowerCase();
  const tvPlaceholderMode = String(cleaned.TV_PLACEHOLDER_SEARCH_MODE ?? "primary").trim().toLowerCase();
  const moviePlaybackMode = String(cleaned.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match").trim().toLowerCase();
  const tvPlaybackMode = String(cleaned.TV_PLAYBACK_INSTANCE_MODE ?? "match").trim().toLowerCase();

  cleaned.MOVIE_PLACEHOLDER_SEARCH_MODE = hasRadarrSecondary
    ? (PLACEHOLDER_MODE_VALUES.has(moviePlaceholderMode) ? moviePlaceholderMode : "primary")
    : "primary";
  cleaned.TV_PLACEHOLDER_SEARCH_MODE = hasSonarrSecondary
    ? (PLACEHOLDER_MODE_VALUES.has(tvPlaceholderMode) ? tvPlaceholderMode : "primary")
    : "primary";
  cleaned.MOVIE_PLAYBACK_INSTANCE_MODE = hasRadarrSecondary
    ? (PLAYBACK_MODE_VALUES.has(moviePlaybackMode) ? moviePlaybackMode : "match")
    : "match";
  cleaned.TV_PLAYBACK_INSTANCE_MODE = hasSonarrSecondary
    ? (PLAYBACK_MODE_VALUES.has(tvPlaybackMode) ? tvPlaybackMode : "match")
    : "match";

  if (!hasRadarrSecondary && !hasSonarrSecondary) {
    cleaned.ENABLE_PLAYBACK_FALLBACK_SEARCH = false;
  }

  if ("PLACEHOLDER_STATUS_PROJECTION_MODE" in cleaned) {
    const pm = String(cleaned.PLACEHOLDER_STATUS_PROJECTION_MODE ?? "summary").trim().toLowerCase();
    if (pm === "off" || !["summary", "title", "both"].includes(pm)) {
      cleaned.PLACEHOLDER_STATUS_PROJECTION_MODE = "summary";
    }
  }

  return cleaned;
}

function ArrInstancesEditor(props: {
  values: FieldValueMap;
  onValueChange: (key: string, value: unknown) => void;
  accent: BrandAccent;
  onPrimaryTestStatusChange?: (arrType: "radarr" | "sonarr", ok: boolean) => void;
  onSecondaryTestStatusChange?: (arrType: "radarr" | "sonarr", ok: boolean) => void;
  /** "slots" = Overseerr-style dashed placeholders + slide-over editor (onboarding). */
  layout?: "cards" | "slots";
}) {
  const layout = props.layout ?? "cards";
  const [instances, setInstances] = useState<ArrInstanceDraft[]>(() => parseArrInstancesFromValues(props.values));
  const [testState, setTestState] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [instanceKeyConflict, setInstanceKeyConflict] = useState<string | null>(null);
  const [primaryCache, setPrimaryCache] = useState<{ radarr: ArrInstanceDraft | null; sonarr: ArrInstanceDraft | null }>(() => {
    const parsed = parseArrInstancesFromValues(props.values);
    return {
      radarr: parsed.filter((item) => item.arr_type === "radarr")[0] || null,
      sonarr: parsed.filter((item) => item.arr_type === "sonarr")[0] || null,
    };
  });
  const [secondaryCache, setSecondaryCache] = useState<{ radarr: ArrInstanceDraft | null; sonarr: ArrInstanceDraft | null }>(() => {
    const parsed = parseArrInstancesFromValues(props.values);
    return {
      radarr: parsed.filter((item) => item.arr_type === "radarr")[1] || null,
      sonarr: parsed.filter((item) => item.arr_type === "sonarr")[1] || null,
    };
  });
  const [primaryEnabled, setPrimaryEnabled] = useState<{ radarr: boolean; sonarr: boolean }>(() => {
    const parsed = parseArrInstancesFromValues(props.values);
    return {
      radarr: parsed.filter((item) => item.arr_type === "radarr").length > 0,
      sonarr: parsed.filter((item) => item.arr_type === "sonarr").length > 0,
    };
  });
  const [secondaryEnabled, setSecondaryEnabled] = useState<{ radarr: boolean; sonarr: boolean }>(() => {
    const parsed = parseArrInstancesFromValues(props.values);
    return {
      radarr: parsed.filter((item) => item.arr_type === "radarr").length > 1,
      sonarr: parsed.filter((item) => item.arr_type === "sonarr").length > 1,
    };
  });
  const [primaryConnectionOk, setPrimaryConnectionOk] = useState<{ radarr: boolean; sonarr: boolean }>({
    radarr: false,
    sonarr: false,
  });
  const primaryGateOk = useMemo(
    () => ({
      radarr: primaryConnectionOk.radarr || arrPrimaryPersistedWithCredentials(props.values, "radarr"),
      sonarr: primaryConnectionOk.sonarr || arrPrimaryPersistedWithCredentials(props.values, "sonarr"),
    }),
    [primaryConnectionOk.radarr, primaryConnectionOk.sonarr, props.values],
  );
  const [slotPanel, setSlotPanel] = useState<{ arrType: "radarr" | "sonarr"; slotIndex: 0 | 1; isNew?: boolean } | null>(null);
  const [disconnectDialog, setDisconnectDialog] = useState<{
    arrType: "radarr" | "sonarr";
    slotIndex: 0 | 1;
    label: string;
  } | null>(null);
  const [webhookSetupDialog, setWebhookSetupDialog] = useState<{
    arrType: "radarr" | "sonarr";
    instance_key: string;
    instance_id: string;
    label: string;
  } | null>(null);
  const slotPanelSnapshotRef = useRef<string | null>(null);
  const slotPanelCaptureKeyRef = useRef<string>("");
  const [slotFooterTestBusy, setSlotFooterTestBusy] = useState(false);
  const [slotPanelTestPassed, setSlotPanelTestPassed] = useState(false);

  useEffect(() => {
    props.onValueChange("WIZARD_RADARR_SECONDARY_ENABLED", secondaryEnabled.radarr);
    props.onValueChange("WIZARD_SONARR_SECONDARY_ENABLED", secondaryEnabled.sonarr);
  }, [props, secondaryEnabled]);


  function update(next: ArrInstanceDraft[]) {
    const radarr = next.filter((item) => item.arr_type === "radarr").slice(0, ARR_INSTANCE_LIMIT_PER_TYPE);
    const sonarr = next.filter((item) => item.arr_type === "sonarr").slice(0, ARR_INSTANCE_LIMIT_PER_TYPE);
    const trimmed = [...radarr, ...sonarr];

    const keysOf = (rows: ArrInstanceDraft[]) =>
      rows.map((r) => normalizeInstanceKey(String(r.instance_key || inferDefaultKey(r.label, r.arr_type))));
    const radKeys = keysOf(radarr);
    const sonKeys = keysOf(sonarr);
    if (radarr.length > 1 && new Set(radKeys).size !== radKeys.length) {
      setInstanceKeyConflict("Two Radarr rows cannot share the same instance key (derived from the name/key). Give each instance a distinct name.");
      return;
    }
    if (sonarr.length > 1 && new Set(sonKeys).size !== sonKeys.length) {
      setInstanceKeyConflict("Two Sonarr rows cannot share the same instance key (derived from the name/key). Give each instance a distinct name.");
      return;
    }
    setInstanceKeyConflict(null);

    setInstances(trimmed);
    props.onValueChange("ARR_INSTANCES_JSON", serializeArrInstances(trimmed));
  }

  function defaultSlot(arrType: "radarr" | "sonarr", slotIndex: 0 | 1): ArrInstanceDraft {
    const label = arrType === "radarr"
      ? (slotIndex === 0 ? "Radarr Primary" : "Radarr Secondary")
      : (slotIndex === 0 ? "Sonarr Primary" : "Sonarr Secondary");
    const instanceKey = inferDefaultKey(label, arrType);
    const role = slotIndex === 0 ? "primary" : "secondary";
    return {
      id: `slot-${arrType}-${slotIndex}`,
      instance_id: stableArrInstanceId(arrType, role),
      label,
      arr_type: arrType,
      instance_key: instanceKey,
      url: "",
      api_key: "",
      role,
      priority: slotIndex,
      is_4k: deriveIs4kFromRole(role),
    };
  }

  function getTypeRows(arrType: "radarr" | "sonarr"): ArrInstanceDraft[] {
    return instances.filter((item) => item.arr_type === arrType).slice(0, ARR_INSTANCE_LIMIT_PER_TYPE);
  }

  function upsertSlot(arrType: "radarr" | "sonarr", slotIndex: 0 | 1, patch: Partial<ArrInstanceDraft>) {
    const typeRows = getTypeRows(arrType);
    const otherRows = instances.filter((item) => item.arr_type !== arrType);
    while (typeRows.length <= slotIndex) {
      typeRows.push(defaultSlot(arrType, typeRows.length === 0 ? 0 : 1));
    }
    const target = typeRows[slotIndex] || defaultSlot(arrType, slotIndex);
    const merged = { ...target, ...patch };
    typeRows[slotIndex] = merged;
    if (Object.prototype.hasOwnProperty.call(patch, "url") || Object.prototype.hasOwnProperty.call(patch, "api_key")) {
      setTestState((prev) => {
        const next = { ...prev };
        delete next[merged.id];
        return next;
      });
    }
    if (slotIndex === 0 && (Object.prototype.hasOwnProperty.call(patch, "url") || Object.prototype.hasOwnProperty.call(patch, "api_key"))) {
      setPrimaryConnectionOk((prev) => ({ ...prev, [arrType]: false }));
      props.onPrimaryTestStatusChange?.(arrType, false);
      props.onSecondaryTestStatusChange?.(arrType, false);
    }
    if (slotIndex === 1 && (Object.prototype.hasOwnProperty.call(patch, "url") || Object.prototype.hasOwnProperty.call(patch, "api_key"))) {
      props.onSecondaryTestStatusChange?.(arrType, false);
    }
    update([...otherRows, ...typeRows]);
  }

  function setPrimary(arrType: "radarr" | "sonarr", enabled: boolean) {
    const typeRows = getTypeRows(arrType);
    const otherRows = instances.filter((item) => item.arr_type !== arrType);
    if (!enabled) {
      setPrimaryCache((prev) => ({ ...prev, [arrType]: typeRows[0] || prev[arrType] }));
      setSecondaryCache((prev) => ({ ...prev, [arrType]: typeRows[1] || prev[arrType] }));
      setPrimaryEnabled((prev) => ({ ...prev, [arrType]: false }));
      setSecondaryEnabled((prev) => ({ ...prev, [arrType]: false }));
      setPrimaryConnectionOk((prev) => ({ ...prev, [arrType]: false }));
      props.onPrimaryTestStatusChange?.(arrType, false);
      props.onSecondaryTestStatusChange?.(arrType, false);
      update(otherRows);
      return;
    }

    const primary = typeRows[0] ?? primaryCache[arrType] ?? defaultSlot(arrType, 0);
    setPrimaryEnabled((prev) => ({ ...prev, [arrType]: true }));
    update([...otherRows, { ...primary }]);
  }

  function setSecondary(arrType: "radarr" | "sonarr", enabled: boolean) {
    if (enabled && (!primaryEnabled[arrType] || !primaryGateOk[arrType])) return;
    const typeRows = getTypeRows(arrType);
    const otherRows = instances.filter((item) => item.arr_type !== arrType);
    if (!enabled) {
      setSecondaryCache((prev) => ({ ...prev, [arrType]: typeRows[1] || prev[arrType] }));
      setSecondaryEnabled((prev) => ({ ...prev, [arrType]: false }));
      props.onSecondaryTestStatusChange?.(arrType, false);
      update([...otherRows, ...typeRows.slice(0, 1)]);
      return;
    }
    // Ensure primary exists before adding secondary
    const primary = typeRows[0] ?? defaultSlot(arrType, 0);
    const secondary = typeRows[1] ?? secondaryCache[arrType] ?? defaultSlot(arrType, 1);
    setSecondaryEnabled((prev) => ({ ...prev, [arrType]: true }));
    update([...otherRows, primary, { ...secondary }]);
  }

  async function runTest(item: ArrInstanceDraft, arrType: "radarr" | "sonarr", slotIndex: 0 | 1) {
    setTestState((prev) => ({ ...prev, [item.id]: { ok: true, message: "Testing..." } }));
    const result = await testIntegrationConnection({
      service: item.arr_type,
      url: String(item.url || ""),
      credential: String(item.api_key || ""),
    });
    setTestState((prev) => ({ ...prev, [item.id]: result }));
    if (slotIndex === 0 && primaryEnabled[arrType]) {
      setPrimaryConnectionOk((prev) => ({ ...prev, [arrType]: Boolean(result.ok) }));
      props.onPrimaryTestStatusChange?.(arrType, Boolean(result.ok));
      if (!result.ok) props.onSecondaryTestStatusChange?.(arrType, false);
    }
    if (slotIndex === 1 && secondaryEnabled[arrType]) {
      props.onSecondaryTestStatusChange?.(arrType, Boolean(result.ok));
    }
    return result;
  }

  const byType = {
    radarr: getTypeRows("radarr"),
    sonarr: getTypeRows("sonarr"),
  };

  function slotFor(arrType: "radarr" | "sonarr", slotIndex: 0 | 1): ArrInstanceDraft {
    return byType[arrType][slotIndex] || defaultSlot(arrType, slotIndex);
  }

  function openSlotPanel(next: { arrType: "radarr" | "sonarr"; slotIndex: 0 | 1; isNew?: boolean }) {
    setSlotPanelTestPassed(false);
    setSlotFooterTestBusy(false);
    setSlotPanel({ ...next, isNew: Boolean(next.isNew) });
  }

  function instanceWebhookUrls(instance_id: string, instance_key: string) {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    return buildArrInstanceWebhookUrls(origin, instance_id, instance_key);
  }

  function onSlotPanelSaveClick() {
    if (!slotPanel) return;
    const snap = slotFor(slotPanel.arrType, slotPanel.slotIndex);
    const credsOk = Boolean(String(snap.url || "").trim() && String(snap.api_key || "").trim());
    const rawSnap = slotPanelSnapshotRef.current;
    let prevNorm = "";
    if (rawSnap != null) {
      try {
        const parsed = JSON.parse(rawSnap) as ArrInstanceDraft[];
        if (Array.isArray(parsed)) {
          const typeRows = parsed.filter((item) => item.arr_type === slotPanel.arrType).slice(0, ARR_INSTANCE_LIMIT_PER_TYPE);
          const prevUrl = String(typeRows[slotPanel.slotIndex]?.url || "").trim();
          prevNorm = normalizeArrInstanceUrlForDedupe(prevUrl);
        }
      } catch {
        prevNorm = "";
      }
    } else if (!slotPanel.isNew) {
      prevNorm = normalizeArrInstanceUrlForDedupe(String(snap.url || "").trim());
    }
    const currNorm = normalizeArrInstanceUrlForDedupe(String(snap.url || "").trim());
    const urlChanged = prevNorm !== currNorm;
    handleSlotPanelSave();
    if (credsOk && urlChanged) {
      setWebhookSetupDialog({
        arrType: snap.arr_type,
        instance_key: normalizeInstanceKey(String(snap.instance_key || "")),
        instance_id: String(snap.instance_id || ""),
        label: String(snap.label || ""),
      });
    }
  }

  function confirmDisconnectInstance() {
    if (!disconnectDialog) return;
    const { arrType, slotIndex } = disconnectDialog;
    if (slotIndex === 0) {
      setPrimary(arrType, false);
    } else {
      setSecondary(arrType, false);
    }
    setSlotPanel((p) => {
      if (p?.arrType === arrType && p.slotIndex === slotIndex) {
        slotPanelSnapshotRef.current = null;
        slotPanelCaptureKeyRef.current = "";
        setSlotFooterTestBusy(false);
        return null;
      }
      return p;
    });
    setDisconnectDialog(null);
  }

  function restoreSlotPanelSnapshot() {
    const raw = slotPanelSnapshotRef.current;
    slotPanelSnapshotRef.current = null;
    if (raw == null) return;
    try {
      const parsed = JSON.parse(raw) as ArrInstanceDraft[];
      if (!Array.isArray(parsed)) return;
      setInstanceKeyConflict(null);
      setInstances(parsed);
      props.onValueChange("ARR_INSTANCES_JSON", serializeArrInstances(parsed));
    } catch {
      // ignore corrupt snapshot
    }
  }

  function handleSlotPanelCancel() {
    const panel = slotPanel;
    slotPanelCaptureKeyRef.current = "";
    setSlotFooterTestBusy(false);
    setSlotPanel(null);
    // "Connect …" enables primary/secondary and opens the editor with `isNew`. Closing without Save should
    // return to the dashed Connect card, not a half-added slot shell. Do not run `restoreSlotPanelSnapshot`
    // here: the captured JSON can still include the empty primary row, which would fight `setPrimary(false)`.
    if (panel?.isNew) {
      slotPanelSnapshotRef.current = null;
      if (panel.slotIndex === 0) {
        setPrimary(panel.arrType, false);
      } else {
        setSecondary(panel.arrType, false);
      }
      return;
    }
    restoreSlotPanelSnapshot();
  }

  function handleSlotPanelSave() {
    slotPanelSnapshotRef.current = null;
    slotPanelCaptureKeyRef.current = "";
    setSlotFooterTestBusy(false);
    setSlotPanel(null);
  }

  function card(
    item: ArrInstanceDraft,
    arrType: "radarr" | "sonarr",
    slotIndex: 0 | 1,
    required: boolean,
    opts?: {
      showToggle?: boolean;
      enabled?: boolean;
      onToggle?: (enabled: boolean) => void;
      toggleDisabled?: boolean;
      toggleHint?: string;
      statusHint?: string;
    },
  ) {
    const status = testState[item.id];
    const isEnabled = opts?.enabled ?? true;
    const isDisabled = !isEnabled;
    const dupPeer = findDuplicateArrInstanceUrl(instances, item.id, String(item.url || ""));
    return (
      <div
        key={item.id}
        className={`rounded-xl border border-[var(--brand-accent-3)] bg-[color:color-mix(in_srgb,var(--brand-surface-panel)_92%,var(--brand-accent-3)_8%)] shadow-md shadow-black/15 overflow-hidden ${isDisabled ? "opacity-60" : ""}`}
      >
        <div className="p-4 space-y-3">
          <div className="flex items-center justify-between gap-2">
            <div className="flex-1">
              <input
                className="bg-transparent text-sm font-semibold text-white font-headline outline-none w-full"
                value={item.label}
                onChange={(e) => {
                  const value = e.target.value;
                  upsertSlot(arrType, slotIndex, { label: value });
                }}
                placeholder="Instance name (e.g. Sonarr, Sonarr 4K)"
                disabled={isDisabled}
              />
              <div className="text-[11px] text-slate-400 mt-1">
                {item.arr_type.toUpperCase()} {required ? "• Primary" : "• Secondary"}
              </div>
              {opts?.statusHint ? <div className="ui-field-description-compact mt-1">{opts.statusHint}</div> : null}
            </div>
            {opts?.showToggle ? (
              <label className={`flex items-center gap-2 select-none shrink-0 ${opts.toggleDisabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}>
                <span className="text-[11px] text-slate-300">Enabled</span>
                <div
                  className={`w-9 h-5 rounded-full transition-colors flex items-center px-0.5 ${isEnabled ? "" : "bg-[#252e3a]"}`}
                  style={isEnabled ? { backgroundColor: props.accent.hex } : undefined}
                  onClick={() => {
                    if (opts.toggleDisabled) return;
                    opts.onToggle?.(!isEnabled);
                  }}
                >
                  <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${isEnabled ? "translate-x-4" : "translate-x-0"}`} />
                </div>
              </label>
            ) : null}
          </div>
          {opts?.toggleHint && opts.toggleDisabled ? <div className="ui-field-description-compact">{opts.toggleHint}</div> : null}
          <div>
            <label className="block text-[11px] font-semibold text-slate-400 mb-1">URL &amp; port</label>
            <input
              className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-xs text-slate-200"
              value={item.url}
              onChange={(e) => upsertSlot(arrType, slotIndex, { url: e.target.value })}
              placeholder={arrType === "sonarr" ? "https://host:8989" : "https://host:7878"}
              disabled={isDisabled}
            />
            {dupPeer && !isDisabled ? (
              <p className="mt-1.5 text-xs text-red-400">
                Same address as &quot;{dupPeer.label}&quot; ({dupPeer.arr_type}). Use a distinct URL for each instance.
              </p>
            ) : null}
          </div>
          <input
            className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-xs text-slate-200"
            value={item.api_key}
            onChange={(e) => upsertSlot(arrType, slotIndex, { api_key: e.target.value })}
            placeholder="API key"
            type="password"
            disabled={isDisabled}
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void runTest(item, arrType, slotIndex)}
              className="px-3 py-1.5 rounded-md text-xs bg-[#252e3a] border border-[#424753]/40 text-slate-300"
              disabled={isDisabled || Boolean(dupPeer)}
            >
              Test
            </button>
          </div>
          <div className="min-h-[2.25rem] text-xs" aria-live="polite">
            {status && status.message !== "Testing..." ? (
              <div className={`mt-1.5 ${status.ok ? "text-green-400" : "text-red-400"}`}>{status.message}</div>
            ) : status && status.message === "Testing..." ? (
              <div className="mt-1.5 flex items-center gap-1.5 text-slate-400">
                <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 14 }}>progress_activity</span>
                <span>Testing…</span>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    );
  }

  const slotPanelDupPeer = useMemo(() => {
    if (!slotPanel) return null;
    const it = slotFor(slotPanel.arrType, slotPanel.slotIndex);
    return findDuplicateArrInstanceUrl(instances, it.id, String(it.url || ""));
  }, [instances, slotPanel]);

  const slotPanelItemForEffect = slotPanel ? slotFor(slotPanel.arrType, slotPanel.slotIndex) : null;
  const slotPanelConnectionKey = slotPanelItemForEffect
    ? `${slotPanelItemForEffect.id}:${String(slotPanelItemForEffect.url)}:${String(slotPanelItemForEffect.api_key)}`
    : "";

  useEffect(() => {
    if (!slotPanel) return;
    setSlotPanelTestPassed(false);
  }, [slotPanel?.arrType, slotPanel?.slotIndex, slotPanelConnectionKey]);

  useEffect(() => {
    if (!slotPanel) {
      setSlotFooterTestBusy(false);
      setSlotPanelTestPassed(false);
      slotPanelCaptureKeyRef.current = "";
    }
  }, [slotPanel]);

  useLayoutEffect(() => {
    if (!slotPanel) {
      return;
    }
    const key = `${slotPanel.arrType}-${slotPanel.slotIndex}`;
    if (slotPanelCaptureKeyRef.current === key) return;
    slotPanelCaptureKeyRef.current = key;
    slotPanelSnapshotRef.current = JSON.stringify(instances);
    // Intentionally omit `instances` from deps: capture baseline only when the panel opens or switches slots.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slotPanel]);

  return (
    <div className="space-y-5">
      {instanceKeyConflict ? (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">{instanceKeyConflict}</div>
      ) : null}
      {disconnectDialog ? (
        <div className="fixed inset-0 z-[65] flex items-center justify-center bg-[#0f1419]/80 backdrop-blur-sm p-6">
          <div className="w-full max-w-md rounded-2xl border border-[#424753]/40 bg-[#171c22] p-6 shadow-2xl space-y-4">
            <h3 className="text-lg font-headline font-bold text-white">Disconnect this instance?</h3>
            <p className="text-sm text-slate-300 leading-relaxed">
              This removes &quot;{disconnectDialog.label}&quot; from Placeholdarr. When you save settings, movies or
              shows that were tracked only on this instance will be marked removed and their placeholders cleaned up.
              If another configured {disconnectDialog.arrType === "radarr" ? "Radarr" : "Sonarr"} instance still lists
              the same title (same TMDB or TVDB id), shared on-disk placeholders are left in place.
            </p>
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                className="px-4 py-2 rounded-lg text-xs font-headline uppercase tracking-wider border border-[#424753]/50 text-slate-300 hover:bg-[#252e3a]"
                onClick={() => setDisconnectDialog(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="px-4 py-2 rounded-lg text-xs font-headline uppercase tracking-wider bg-red-600 text-white hover:bg-red-500 border border-red-500/80"
                onClick={confirmDisconnectInstance}
              >
                Disconnect
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {webhookSetupDialog ? (() => {
        const wh = webhookSetupDialog;
        const service = ARR_WEBHOOK_SERVICES.services.find((s) => s.id === wh.arrType);
        const urls = instanceWebhookUrls(wh.instance_id, wh.instance_key);
        const webhookUrl = urls.primary;
        const serviceLabel = service?.name || (wh.arrType === "radarr" ? "Radarr" : "Sonarr");
        return (
          <div className="fixed inset-0 z-[80] flex items-center justify-center bg-[#0f1419]/85 backdrop-blur-sm p-6">
            <div className="w-full max-w-lg max-h-[min(90vh,720px)] overflow-y-auto rounded-2xl border border-[#424753]/40 bg-[#171c22] p-6 shadow-2xl space-y-4">
              <h3 className="text-lg font-headline font-bold text-white">Configure webhooks in {serviceLabel}</h3>
              <p className="text-sm text-slate-300">
                {serviceLabel} must call Placeholdarr using this URL so imports, deletes, and searches stay in sync
                {wh.label ? ` (${wh.label})` : ""}.
              </p>
              <ol className="ui-field-description space-y-2 list-decimal list-inside text-sm text-slate-300">
                <li>Go to Settings → Webhooks → Add (+)</li>
                <li>
                  <span className="text-slate-200">Webhook URL</span>
                  <div className="mt-1 flex items-start gap-2 pl-0">
                    <span className="min-w-0 flex-1 break-all font-mono text-[12px] leading-snug text-slate-300">{webhookUrl}</span>
                    <WebhookStepCopyButton text={webhookUrl} ariaLabel={`Copy ${serviceLabel} webhook URL`} className="mt-0.5 shrink-0" />
                  </div>
                </li>
                <li>Enable these events (required):</li>
              </ol>
              <div className="ml-4 space-y-1">
                {(service?.triggers || [])
                  .filter((t) => t.required)
                  .map((trigger) => (
                    <div key={`wh-${wh.instance_id}-${trigger.event}`} className="text-xs text-slate-300">
                      {trigger.displayName}
                    </div>
                  ))}
              </div>
              <div className="flex justify-end pt-2">
                <button
                  type="button"
                  className="px-5 py-2 rounded-lg text-xs font-headline uppercase tracking-wider text-white"
                  style={{ backgroundColor: props.accent.hex }}
                  onClick={() => setWebhookSetupDialog(null)}
                >
                  Done
                </button>
              </div>
            </div>
          </div>
        );
      })() : null}
      {layout === "slots" ? (
        <div className="space-y-5">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:items-stretch sm:gap-5">
          {(["radarr", "sonarr"] as const).map((arrType) => {
            const primaryItem = slotFor(arrType, 0);
            const secondaryItem = slotFor(arrType, 1);
            const secondaryAddLocked = !primaryEnabled[arrType] || !primaryGateOk[arrType];
            const av = ONBOARDING_ARR_VISUAL[arrType];
            const serviceName = arrType === "radarr" ? "Radarr" : "Sonarr";
            return (
              <div key={arrType} className={`min-w-0 ${UI_INTEGRATION_CARD_SURFACE_CLASS} p-5`}>
                <div className="mb-5 flex flex-col items-center gap-3 text-center">
                  <div
                    className="flex h-20 w-20 shrink-0 items-center justify-center rounded-2xl"
                    style={av.well}
                    aria-hidden
                  >
                    <img src={av.iconSrc} alt="" decoding="async" className="h-12 w-12 object-contain" aria-hidden />
                  </div>
                  <h3 className="text-lg font-bold tracking-tight text-white font-headline">{serviceName}</h3>
                </div>
                <div className="flex min-h-0 flex-col gap-3">
                  <div className="flex min-h-0 flex-col">
                    <div className="mb-2 text-[10px] font-headline uppercase tracking-[0.14em] text-slate-500">Slot 1 · Primary</div>
                    {!primaryEnabled[arrType] ? (
                      <button
                        type="button"
                        onClick={() => {
                          setPrimary(arrType, true);
                          openSlotPanel({ arrType, slotIndex: 0, isNew: true });
                        }}
                        className="flex min-h-[132px] flex-1 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-white/15 bg-black/25 px-3 py-4 text-center text-slate-300 transition hover:border-white/30 hover:bg-white/[0.04]"
                      >
                        <span className="material-symbols-outlined" style={{ fontSize: 28 }}>add</span>
                        <span className="text-sm font-headline tracking-wide">Connect {serviceName}</span>
                      </button>
                    ) : (
                      <div className="flex min-h-[132px] flex-1 flex-col justify-between rounded-xl border border-white/[0.08] bg-[#0a0f18]/95 px-4 py-3">
                        <div className="min-w-0">
                          <div className="text-sm font-semibold text-white font-headline truncate">{primaryItem.label}</div>
                          <div className="truncate font-mono text-[11px] text-slate-500">{String(primaryItem.url || "").trim() || "—"}</div>
                          {testState[primaryItem.id] && !testState[primaryItem.id].ok ? (
                            <div className="mt-1 text-xs text-red-400">{testState[primaryItem.id].message}</div>
                          ) : null}
                        </div>
                        <div className="mt-3 flex flex-wrap items-center gap-2">
                          <div className="flex min-w-0 flex-1 flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => openSlotPanel({ arrType, slotIndex: 0, isNew: false })}
                              className="rounded-lg border border-white/15 bg-white/[0.05] px-3 py-1.5 text-[11px] font-headline font-semibold uppercase tracking-wider text-slate-200 transition hover:border-white/25 hover:bg-white/[0.09]"
                            >
                              Configure
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setWebhookSetupDialog({
                                  arrType,
                                  instance_key: normalizeInstanceKey(String(primaryItem.instance_key || "")),
                                  instance_id: String(primaryItem.instance_id || ""),
                                  label: String(primaryItem.label || ""),
                                });
                              }}
                              className="rounded-lg border border-white/10 bg-transparent px-3 py-1.5 text-[11px] font-headline font-semibold uppercase tracking-wider text-slate-400 transition hover:border-white/20 hover:text-slate-200"
                            >
                              Webhook URL
                            </button>
                          </div>
                          <button
                            type="button"
                            onClick={() =>
                              setDisconnectDialog({
                                arrType,
                                slotIndex: 0,
                                label: String(primaryItem.label || arrType),
                              })
                            }
                            className="ml-auto shrink-0 rounded-lg px-3 py-1.5 text-[11px] font-medium text-red-400 transition hover:text-red-300"
                          >
                            Remove
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                  <div className="flex min-h-0 flex-col">
                    <div className="mb-2 text-[10px] font-headline uppercase tracking-[0.14em] text-slate-500">Slot 2 · Secondary</div>
                    {!secondaryEnabled[arrType] ? (
                      <div
                        className={`flex min-h-[132px] flex-1 flex-col items-center justify-center rounded-xl border border-dashed px-3 py-4 text-center transition-colors ${
                          secondaryAddLocked
                            ? "border-white/[0.07] bg-black/20 text-slate-600"
                            : "border-white/15 bg-black/25 text-slate-300 hover:border-white/30 hover:bg-white/[0.04]"
                        }`}
                      >
                        {secondaryAddLocked ? (
                          <>
                            <span className="material-symbols-outlined opacity-35" style={{ fontSize: 24 }}>
                              lock
                            </span>
                            <p className="ui-field-description-compact mt-2 text-center">
                              {!primaryEnabled[arrType]
                                ? "Connect primary first."
                                : "Pass a primary connection test to unlock this slot."}
                            </p>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              setSecondary(arrType, true);
                              openSlotPanel({ arrType, slotIndex: 1, isNew: true });
                            }}
                            className="flex w-full flex-col items-center justify-center gap-2 py-2"
                          >
                            <span className="material-symbols-outlined" style={{ fontSize: 26 }}>add</span>
                            <span className="text-sm font-headline tracking-wide">Secondary instance</span>
                          </button>
                        )}
                      </div>
                    ) : (
                      <div className="flex min-h-[132px] flex-1 flex-col justify-between rounded-xl border border-white/[0.08] bg-[#0a0f18]/95 px-4 py-3">
                        <div className="min-w-0">
                          <div className="text-sm font-semibold text-white font-headline truncate">{secondaryItem.label}</div>
                          <div className="truncate font-mono text-[11px] text-slate-500">{String(secondaryItem.url || "").trim() || "—"}</div>
                          {testState[secondaryItem.id] && !testState[secondaryItem.id].ok ? (
                            <div className="mt-1 text-xs text-red-400">{testState[secondaryItem.id].message}</div>
                          ) : null}
                        </div>
                        <div className="mt-3 flex flex-wrap items-center gap-2">
                          <div className="flex min-w-0 flex-1 flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => openSlotPanel({ arrType, slotIndex: 1, isNew: false })}
                              className="rounded-lg border border-white/15 bg-white/[0.05] px-3 py-1.5 text-[11px] font-headline font-semibold uppercase tracking-wider text-slate-200 transition hover:border-white/25 hover:bg-white/[0.09]"
                            >
                              Configure
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setWebhookSetupDialog({
                                  arrType,
                                  instance_key: normalizeInstanceKey(String(secondaryItem.instance_key || "")),
                                  instance_id: String(secondaryItem.instance_id || ""),
                                  label: String(secondaryItem.label || ""),
                                });
                              }}
                              className="rounded-lg border border-white/10 bg-transparent px-3 py-1.5 text-[11px] font-headline font-semibold uppercase tracking-wider text-slate-400 transition hover:border-white/20 hover:text-slate-200"
                            >
                              Webhook URL
                            </button>
                          </div>
                          <button
                            type="button"
                            onClick={() =>
                              setDisconnectDialog({
                                arrType,
                                slotIndex: 1,
                                label: String(secondaryItem.label || arrType),
                              })
                            }
                            className="ml-auto shrink-0 rounded-lg px-3 py-1.5 text-[11px] font-medium text-red-400 transition hover:text-red-300"
                          >
                            Remove
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          </div>
          {slotPanel ? (() => {
            const { arrType, slotIndex, isNew } = slotPanel;
            const item = slotFor(arrType, slotIndex);
            const status = testState[item.id];
            const roleLabel = slotIndex === 0 ? "Primary" : "Secondary";
            const serviceLabel = arrType === "radarr" ? "Radarr" : "Sonarr";
            const slotCommitLabel = isNew ? `Add ${serviceLabel}` : `Save ${serviceLabel}`;
            const detailsComplete =
              String(item.url || "").trim().length > 0 && String(item.api_key || "").trim().length > 0;
            const dupBlocks = Boolean(slotPanelDupPeer);
            const testDisabled = !detailsComplete || dupBlocks || slotFooterTestBusy;
            const saveDisabled = !slotPanelTestPassed || dupBlocks || slotFooterTestBusy;
            return (
              <div className="fixed inset-0 z-[70] flex items-center justify-center overflow-y-auto p-4 sm:p-6">
                <button
                  type="button"
                  aria-label="Close panel"
                  className="absolute inset-0 bg-black/70 backdrop-blur-sm"
                  onClick={handleSlotPanelCancel}
                />
                <div
                  role="dialog"
                  aria-modal="true"
                  aria-label={`${serviceLabel} ${roleLabel} server`}
                  className="relative z-10 my-auto flex w-full max-w-lg max-h-[min(90vh,720px)] flex-col overflow-hidden rounded-2xl border border-[#424753]/50 bg-[#171c22] shadow-2xl"
                >
                  <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-[#424753]/40 shrink-0">
                    <div>
                      <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500">ARR server</div>
                      <h2 className="text-lg font-headline font-bold text-white mt-0.5" style={{ color: props.accent.text }}>
                        {serviceLabel} · {roleLabel}
                      </h2>
                    </div>
                    <button
                      type="button"
                      onClick={handleSlotPanelCancel}
                      className="p-2 rounded-lg text-slate-400 hover:text-white hover:bg-[#252e3a]/80"
                      aria-label="Close"
                    >
                      <span className="material-symbols-outlined" style={{ fontSize: 22 }}>close</span>
                    </button>
                  </div>
                  <div className="flex-1 min-h-0 overflow-y-auto p-5 space-y-4">
                    <div>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">Server name</label>
                      <input
                        className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none focus:ring-2 focus:ring-offset-0 focus:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]"
                        value={item.label}
                        onChange={(e) => upsertSlot(arrType, slotIndex, { label: e.target.value })}
                        placeholder="Instance name (e.g. Radarr 4K)"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">URL &amp; port</label>
                      <input
                        className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none focus:ring-2 focus:ring-offset-0 focus:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]"
                        value={item.url}
                        onChange={(e) => upsertSlot(arrType, slotIndex, { url: e.target.value })}
                        placeholder={arrType === "sonarr" ? "https://host:8989" : "https://host:7878"}
                      />
                      {slotPanelDupPeer ? (
                        <p className="mt-2 text-xs text-red-400">
                          Same address as &quot;{slotPanelDupPeer.label}&quot; ({slotPanelDupPeer.arr_type}). Each instance must use a distinct URL and port.
                        </p>
                      ) : null}
                    </div>
                    <div>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">API key</label>
                      <input
                        className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none focus:ring-2 focus:ring-offset-0 focus:ring-[color:color-mix(in_srgb,var(--brand-accent-tertiary)_42%,transparent)]"
                        value={item.api_key}
                        onChange={(e) => upsertSlot(arrType, slotIndex, { api_key: e.target.value })}
                        placeholder="API key"
                        type="password"
                      />
                    </div>
                  </div>
                  <div className="shrink-0 border-t border-[#424753]/40 bg-[#141a24]">
                    <div
                      className="flex min-h-[2.75rem] items-center gap-2 px-4 pt-3 text-xs text-red-400"
                      aria-live="polite"
                    >
                      {status && !slotFooterTestBusy && status.message !== "Testing..." && !status.ok ? (
                        <>
                          <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>error</span>
                          <span className="line-clamp-2 leading-snug">{status.message}</span>
                        </>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap items-stretch justify-between gap-2 px-4 pb-4 pt-1">
                    <button
                      type="button"
                      onClick={handleSlotPanelCancel}
                      className="min-w-[5.5rem] flex-1 sm:flex-none px-4 py-2.5 rounded-lg text-xs font-headline uppercase tracking-wider border border-[#424753]/55 text-slate-300 hover:bg-[#252e3a]/80 hover:border-[#424753]/80 transition-colors"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      disabled={testDisabled}
                      title={
                        status?.ok && !slotFooterTestBusy && status.message !== "Testing..."
                          ? status.message
                          : "Run connection test"
                      }
                      onClick={() => {
                        void (async () => {
                          setSlotFooterTestBusy(true);
                          const r = await runTest(item, arrType, slotIndex);
                          setSlotPanelTestPassed(Boolean(r?.ok));
                          setSlotFooterTestBusy(false);
                        })();
                      }}
                      className={`flex h-11 w-[9rem] shrink-0 basis-[9rem] items-center justify-center gap-1.5 rounded-lg px-2 text-xs font-headline uppercase tracking-wider font-semibold border transition-colors duration-200 ease-out disabled:cursor-not-allowed disabled:opacity-40 ${
                        status?.ok && !slotFooterTestBusy && status.message !== "Testing..."
                          ? "border-emerald-500/70 bg-emerald-600/15 text-emerald-300 hover:border-emerald-400/90 hover:bg-emerald-600/25"
                          : "border-amber-400/80 bg-amber-500 text-slate-900 hover:bg-amber-400 disabled:hover:bg-amber-500"
                      }`}
                    >
                      {slotFooterTestBusy ? (
                        <>
                          <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 18 }}>
                            progress_activity
                          </span>
                          <span>Testing…</span>
                        </>
                      ) : status?.ok && status.message !== "Testing..." ? (
                        <span className="material-symbols-outlined shrink-0" style={{ fontSize: 22 }}>
                          check_circle
                        </span>
                      ) : (
                        <>
                          <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>wifi</span>
                          <span>Test</span>
                        </>
                      )}
                    </button>
                    <button
                      type="button"
                      disabled={saveDisabled}
                      onClick={onSlotPanelSaveClick}
                      aria-label={slotCommitLabel}
                      className="btn-brand-tertiary min-w-[6.5rem] flex-1 sm:flex-none px-4 py-2.5 rounded-lg text-xs font-headline uppercase tracking-wider font-semibold border transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {slotCommitLabel}
                    </button>
                  </div>
                  </div>
                </div>
              </div>
            );
          })() : null}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-5">
        {(["radarr", "sonarr"] as const).map((arrType) => (
          <div key={arrType} className="min-w-0">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-semibold text-white font-headline">{arrType === "radarr" ? "Radarr Settings" : "Sonarr Settings"}</h3>

            </div>
            <div className="flex flex-col gap-3">
              {card(slotFor(arrType, 0), arrType, 0, true, {
                showToggle: true,
                enabled: primaryEnabled[arrType],
                onToggle: (enabled) => setPrimary(arrType, enabled),
                statusHint: primaryConnectionOk[arrType]
                  ? "Connection test confirmed."
                  : primaryGateOk[arrType]
                    ? "Primary is saved with URL and API key. Run Test again if you change either."
                    : "Run a successful primary connection test to unlock the secondary instance toggle.",
              })}
              {card(slotFor(arrType, 1), arrType, 1, false, {
                showToggle: true,
                enabled: secondaryEnabled[arrType],
                onToggle: (enabled) => setSecondary(arrType, enabled),
                toggleDisabled: !primaryEnabled[arrType] || !primaryGateOk[arrType],
                toggleHint: !primaryEnabled[arrType]
                  ? "Enable and configure the primary instance first."
                  : (!primaryGateOk[arrType] ? "Run a successful primary connection test first." : undefined),
              })}
            </div>

          </div>
        ))}
        </div>
      )}
    </div>
  );
}

function LibraryPathsForm(props: {
  fields: SettingsField[];
  values: FieldValueMap;
  brand: Brand;
  themeMode: ThemeMode;
  accent: BrandAccent;
  layout: "settings" | "wizard";
  onValueChange: (key: string, value: unknown) => void;
  runTest?: (field: SettingsField) => void;
  testResults?: Record<string, { ok: boolean; message: string }>;
}) {
  const { root, profiles, overrides, rest } = useMemo(() => partitionLibraryPathFields(props.fields), [props.fields]);
  const [overridesOpen, setOverridesOpen] = useState(() => {
    if (props.layout === "wizard") return false;
    return overrides.some((f) => String(props.values[f.key] ?? "").trim() !== "");
  });
  const focus = getBrandFocusClass(props.brand, props.themeMode);

  function renderBool(field: SettingsField, compact?: boolean) {
    const v = Boolean(props.values[field.key]);
    return (
      <label className="flex items-center gap-3 cursor-pointer select-none w-fit">
        <div
          className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${v ? "" : "bg-[#252e3a]"}`}
          style={v ? { backgroundColor: props.accent.hex } : undefined}
          onClick={() => props.onValueChange(field.key, !v)}
        >
          <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${v ? "translate-x-5" : "translate-x-0"}`} />
        </div>
        <span className={compact ? "text-xs text-slate-400" : "text-sm text-slate-300"}>{v ? "Enabled" : "Disabled"}</span>
      </label>
    );
  }

  function renderTextInput(field: SettingsField, opts?: { compact?: boolean; muted?: boolean }) {
    const border = opts?.muted ? "border-[#424753]/25" : "border-[#424753]/40";
    const ph = field.secret && field.has_saved_value ? "Saved value retained unless overwritten" : `Enter ${field.label.toLowerCase()}...`;
    return (
      <input
        className={`w-full bg-[#0f1419] border ${border} rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${focus} ${opts?.compact ? "text-xs py-1.5" : ""}`}
        type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
        value={String(props.values[field.key] ?? "")}
        placeholder={ph}
        onChange={(e) => props.onValueChange(field.key, e.target.value)}
      />
    );
  }

  function renderChoice(field: SettingsField, opts?: { compact?: boolean; muted?: boolean }) {
    const border = opts?.muted ? "border-[#424753]/25" : "border-[#424753]/40";
    return (
      <select
        className={`w-full bg-[#0f1419] border ${border} rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${focus} ${opts?.compact ? "text-xs py-1.5" : ""}`}
        value={String(props.values[field.key] ?? field.options?.[0]?.value ?? "")}
        onChange={(e) => props.onValueChange(field.key, e.target.value)}
      >
        {(field.options || []).map((opt) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    );
  }

  function renderSettingsRestField(field: SettingsField) {
    const value = props.values[field.key];
    const test = props.testResults?.[field.key];
    const testTarget = URL_TEST_TARGET[field.key];
    return (
      <div key={field.key} className="px-6 py-5">
        <div className="flex items-start gap-3 mb-2">
          <div className="flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-semibold text-white font-headline">{field.label}</span>
              {field.required && (
                <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase" style={{ backgroundColor: alphaColor(props.accent.hex, 0.3), color: props.accent.text }}>Required</span>
              )}
              {field.secret && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-[#252e3a] text-slate-400">Secret</span>}
              {field.restart_required && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-orange-600/30 text-orange-300">Restart Required</span>}
            </div>
            {field.description && <p className="ui-field-description mt-1">{field.description}</p>}
            {getPlexLibraryIdNote(field.key) ? <p className="ui-field-description mt-1">{getPlexLibraryIdNote(field.key)}</p> : null}
          </div>
        </div>
        {field.type === "bool" ? (
          renderBool(field)
        ) : field.type === "choice" && field.options?.length ? (
          <select
            className={`w-full max-w-xl bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${focus}`}
            value={String(value ?? field.options[0]?.value ?? "")}
            onChange={(e) => props.onValueChange(field.key, e.target.value)}
          >
            {field.options.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        ) : (
          <div className="flex gap-2">
            <input
              className={`flex-1 bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${focus}`}
              type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
              value={String(value ?? "")}
              placeholder={field.secret && field.has_saved_value ? "Saved value retained unless overwritten" : `Enter ${field.label.toLowerCase()}...`}
              onChange={(e) => props.onValueChange(field.key, e.target.value)}
            />
            {testTarget && props.runTest && (
              <button type="button" onClick={() => props.runTest!(field)}
                className="flex items-center gap-1.5 px-3 py-2 bg-[#252e3a] hover:bg-[#30353b] border border-[#424753]/40 rounded-lg text-xs text-slate-300 font-headline uppercase tracking-wider transition-colors whitespace-nowrap">
                <span className="material-symbols-outlined" style={{ fontSize: 14 }}>wifi</span>
                Test
              </button>
            )}
          </div>
        )}
        {testTarget ? (
          <div
            className={`mt-2 flex min-h-[2.25rem] items-start gap-1.5 text-xs ${
              test && test.message !== "Testing..." ? (test.ok ? "text-green-400" : "text-red-400") : "text-slate-400"
            }`}
            aria-live="polite"
          >
            {test && test.message === "Testing..." ? (
              <>
                <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 14 }}>progress_activity</span>
                <span>Testing…</span>
              </>
            ) : test ? (
              <>
                <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
                <span className="leading-snug">{test.message}</span>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  const rootCardClass = "rounded-xl border px-4 py-4";
  const rootCardStyle =
    props.layout === "wizard"
      ? undefined
      : {
          borderColor: alphaColor(props.accent.hex, 0.4),
          backgroundColor: alphaColor(props.accent.hex, 0.07),
        };

  const rootBlock = root ? (
    <div
      className={props.layout === "wizard" ? undefined : rootCardClass}
      style={props.layout === "wizard" ? undefined : rootCardStyle}
    >
      <label className="block text-sm font-semibold text-white font-headline mb-1">{root.label}</label>
      {root.description && <p className="ui-field-description mb-3 leading-relaxed">{root.description}</p>}
      {root.type === "bool" ? renderBool(root) : renderTextInput(root)}
    </div>
  ) : null;

  const profileBlock = profiles.length ? (
    <div className={`${UI_SECTION_FRAME_CLASS} px-4 py-4`}>
      <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-2">Folder Profiles</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {profiles.map((field) => (
          <div key={field.key} className="rounded-lg border border-[#424753]/35 bg-[#0b111b] px-3 py-2">
            <div className="text-xs text-slate-300 font-medium mb-2">{field.label}</div>
            {field.key === "ENABLE_STANDARD_PROFILE" ? (
              <span className="text-xs text-emerald-300">Always enabled</span>
            ) : renderBool(field, true)}
            {field.description ? <p className="ui-field-description-compact mt-2">{field.description}</p> : null}
          </div>
        ))}
      </div>
    </div>
  ) : null;

  const overridesBlock = overrides.length ? (
    <div className={props.layout === "settings" ? `${UI_SECTION_FRAME_CLASS} px-4 py-3` : `${UI_SECTION_FRAME_CLASS} px-3 py-3`}>
      <button
        type="button"
        onClick={() => setOverridesOpen((o) => !o)}
        className="flex w-full items-start justify-between gap-3 text-left rounded-md outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-[#171c22] focus-visible:ring-emerald-500/40"
      >
        <div className="min-w-0">
          <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-0.5">Optional overrides</div>
          <div className="text-sm font-medium text-slate-400 font-headline">Custom folder per library</div>
          <p className="ui-field-description mt-1">
            Use when a library path is not under Library Root (or you use no root and set each path manually).
          </p>
        </div>
        <span
          className="material-symbols-outlined shrink-0 text-slate-500 transition-transform duration-200"
          style={{ fontSize: 22, transform: overridesOpen ? "rotate(180deg)" : undefined }}
        >
          expand_more
        </span>
      </button>
      {overridesOpen && (
        <div className="mt-4 ml-1 space-y-4 pl-4 border-l-2 border-[#424753]/60">
          {overrides.map((field) => (
            <div key={field.key}>
              <label className="block text-xs font-medium text-slate-400 font-headline mb-1">{field.label}</label>
              {field.description && <p className="ui-field-description-compact mb-2">{field.description}</p>}
              {field.type === "bool" ? renderBool(field, true) : field.type === "choice" ? renderChoice(field, { compact: true, muted: true }) : renderTextInput(field, { compact: true, muted: true })}
            </div>
          ))}
        </div>
      )}
    </div>
  ) : null;

  if (props.layout === "wizard") {
    return (
      <div className="space-y-6">
        <h2 className={ONBOARDING_SECTION_TITLE_CLASS}>Library paths</h2>
        <div className={WIZARD_ONBOARDING_SECTION_SURFACE_CLASS}>
          <div className="space-y-5">
            {rootBlock}
            {profileBlock}
            {overridesBlock}
            {rest.map((field) => (
              <div key={field.key}>
                <label className="block text-sm font-semibold text-white font-headline mb-1">{field.label}</label>
                {field.description && <p className="ui-field-description mb-2 leading-relaxed">{field.description}</p>}
                {field.type === "bool" ? renderBool(field) : field.type === "choice" ? renderChoice(field) : renderTextInput(field)}
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      {root ? (
        <div className="px-6 py-5">{rootBlock}</div>
      ) : null}
      {profiles.length ? <div className="px-6 py-5">{profileBlock}</div> : null}
      {overrides.length ? <div className="px-6 py-5">{overridesBlock}</div> : null}
      {rest.map((field) => renderSettingsRestField(field))}
    </>
  );
}

type LookaheadIntroVariant = "settings" | "onboarding";

function LookaheadSectionIntro(props: { variant: LookaheadIntroVariant; embedded?: boolean }) {
  const wrapClass =
    props.variant === "settings"
      ? "px-6 py-5 border-b border-[#424753]/20"
      : props.embedded
        ? "space-y-3"
        : WIZARD_ONBOARDING_SECTION_SURFACE_CLASS;
  return (
    <div className={wrapClass}>
      <p className="ui-field-description text-slate-300 leading-relaxed">
        Lookahead keeps your storage usage low by only monitoring and searching for episodes in Sonarr as you progress
        watching a series.
      </p>
      <p className="ui-field-description mt-3 text-slate-300 leading-relaxed">
        When an episode plays, Placeholdarr identifies what was watched, checks Sonarr for what&apos;s already on disk,
        monitors the appropriate content, and triggers a search when needed.
      </p>
      <p className="ui-field-description mt-3 text-slate-400 leading-relaxed">
        <span className="font-medium text-slate-200">Search Mode</span> determines the scope of what gets monitored and
        searched each time you play something:
      </p>
      <ul className="mt-3 list-disc space-y-2 pl-5 text-xs text-slate-400 leading-relaxed">
        <li>
          <span className="font-medium text-slate-200">Series</span>
          {" — "}The entire series is monitored and searched. This mirrors default Sonarr behavior.
        </li>
        <li>
          <span className="font-medium text-slate-200">Season</span>
          {" — "}Only the played episode&apos;s season is monitored and searched. When you finish a season, Placeholdarr
          automatically monitors and searches the next one.
        </li>
        <li>
          <span className="font-medium text-slate-200">Episode</span>
          {" — "}Only the played episode and the next few episodes, up to your configured{" "}
          <span className="font-medium text-slate-200">Lookahead Range</span>, are monitored and searched.
        </li>
      </ul>
      <p className="ui-field-description mt-3 text-slate-400 leading-relaxed">
        Regardless of which mode you choose, Placeholdarr will monitor the full series once you&apos;re approaching the
        end of available content, ensuring Sonarr can pick up new episodes as they air.
      </p>
      <p className="ui-field-description mt-3 text-slate-400 leading-relaxed">
        <span className="font-medium text-slate-200">Lookahead Range</span> sets how many episodes ahead of the one you
        just watched Placeholdarr will monitor and search in Episode mode.
      </p>
    </div>
  );
}

type StatusUpdatesIntroVariant = "settings" | "onboarding";

function StatusUpdatesSectionIntro(props: { variant: StatusUpdatesIntroVariant; embedded?: boolean }) {
  const wrapClass =
    props.variant === "settings"
      ? "px-6 py-5 border-b border-[#424753]/20"
      : props.embedded
        ? "space-y-3"
        : WIZARD_ONBOARDING_SECTION_SURFACE_CLASS;
  return (
    <div className={wrapClass}>
      <p className="ui-field-description text-slate-300 leading-relaxed">
        Use these settings to adjust how Placeholdarr updates content so users can track request progress right from
        media players. Plex typically updates automatically while Emby and Jellyfin typically require page refreshes.
      </p>
    </div>
  );
}

/** Status Updates choice: copy lives here; `PLACEHOLDER_STATUS_UPDATES.description` in app_config is intentionally empty. */
function PlaceholderStatusUpdatesDescription(props: { spacing: "settings" | "wizard" }) {
  const top = props.spacing === "settings" ? "mt-1" : "mb-2";
  return (
    <ul className={`list-disc space-y-2 pl-5 text-xs text-slate-400 leading-relaxed ${top}`}>
      <li>
        <span className="font-medium text-slate-200">All</span>
        {" — "}When a placeholder plays, show search and download progress in the player.
      </li>
      <li>
        <span className="font-medium text-slate-200">Request only</span>
        {" — "}Keep player text simple; show &quot;Request&quot; so placeholders are easy to spot.
      </li>
      <li>
        <span className="font-medium text-slate-200">Off</span>
        {" — "}Do not show placeholder status in media players.
      </li>
    </ul>
  );
}

/** Calendar toggle: copy lives here; `ENABLE_COMING_SOON_COUNTDOWN.description` in app_config is intentionally empty. */
function ComingSoonCountdownDescription(props: { spacing: "settings" | "wizard" }) {
  const top = props.spacing === "settings" ? "mt-1" : "mb-2";
  return (
    <ul className={`list-disc space-y-2 pl-5 text-xs text-slate-400 leading-relaxed ${top}`}>
      <li>
        <span className="font-medium text-slate-200">Enabled</span>
        {" — "}Placeholders show a countdown until release (for example, &quot;Airing in 12 days&quot;).
      </li>
      <li>
        <span className="font-medium text-slate-200">Disabled</span>
        {" — "}Future placeholders always show &quot;Coming soon&quot;.
      </li>
    </ul>
  );
}

/** Keep sentences in sync with `STARTUP_SYNC_MODE` description in `services/app_config.py`. */
function StartupSyncModeDescription(props: { spacing: "settings" | "wizard" }) {
  const top = props.spacing === "settings" ? "mt-1" : "mb-2";
  return (
    <div className={`${top} space-y-3`}>
      <p className="ui-field-description text-slate-300 leading-relaxed">
        Controls how Placeholdarr refreshes from Radarr and Sonarr during startup.
      </p>
      <ul className="list-disc space-y-2 pl-5 text-xs text-slate-400 leading-relaxed">
        <li>
          <span className="font-medium text-slate-200">Full sync</span>
          {" "}
          will scan arrs services and Placeholdarr root folder before proceeding to add/delete placeholder files as needed.
        </li>
        <li>
          <span className="font-medium text-slate-200">Lite sync</span>
          {" "}
          compares each configured Radarr/Sonarr catalog to the database, syncs only what changed, then runs scoped placeholder
          work (no full library filesystem scan on startup).
        </li>
        <li>
          <span className="font-medium text-slate-200">Auto</span>
          {" "}
          will run full at startup when needed (for example, after adding a new arr instance), and a lite sync at other times.
        </li>
      </ul>
      <p className="ui-field-description text-slate-400 leading-relaxed">
        Placeholdarr operations are relatively quick. However, media player libraries still need to scan and update, which can take some time for large library changes.
      </p>
      <p className="ui-field-description ui-field-description-accent3 leading-relaxed">
        A full sync will automatically start in the background at the completion of this setup.
      </p>
    </div>
  );
}

function SettingsPanel(props: {
  payload: SettingsPayload | null;
  activeSection: string;
  values: FieldValueMap;
  hasUnsavedChanges: boolean;
  feedback: string;
  feedbackKind: "" | "success" | "error";
  brand: Brand;
  themeMode: ThemeMode;
  onValueChange: (key: string, value: unknown) => void;
  onSave: () => Promise<void>;
  onTestConnection: (input: { service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr"; urlKey: string; credentialKey: string }) => Promise<{ ok: boolean; message: string }>;
}) {
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [arrSecondaryTestStatus, setArrSecondaryTestStatus] = useState<{ radarr: boolean; sonarr: boolean }>({ radarr: false, sonarr: false });
  const [mediaPanel, setMediaPanel] = useState<null | (typeof ONBOARDING_MEDIA_CARDS)[number]["id"]>(null);
  const [playbackWebhookDialog, setPlaybackWebhookDialog] = useState<{
    serviceId: "tautulli" | "jellyfin" | "emby";
    instanceParam: string;
  } | null>(null);
  const accent = getBrandAccent(props.brand, props.themeMode);

  const arrInstances = parseArrInstancesFromValues(props.values);
  const hasRadarrSecondaryConfigured = arrInstances.filter((item) => item.arr_type === "radarr").length > 1;
  const hasSonarrSecondaryConfigured = arrInstances.filter((item) => item.arr_type === "sonarr").length > 1;
  const canUseRadarrSecondaryBehavior = hasRadarrSecondaryConfigured && arrSecondaryTestStatus.radarr;
  const canUseSonarrSecondaryBehavior = hasSonarrSecondaryConfigured && arrSecondaryTestStatus.sonarr;
  const unlockedSettingsSearchBehavior = [
    canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "both") : null,
    canUseSonarrSecondaryBehavior ? String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "both") : null,
    canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match") : null,
    canUseSonarrSecondaryBehavior ? String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match") : null,
  ].filter((value): value is string => Boolean(value));
  const fallbackUnnecessaryBecauseAllBoth = unlockedSettingsSearchBehavior.length > 0 && unlockedSettingsSearchBehavior.every((value) => value === "both");

  useEffect(() => {
    if (!props.payload) return;
    if (fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH)) {
      props.onValueChange("ENABLE_PLAYBACK_FALLBACK_SEARCH", false);
    }
  }, [fallbackUnnecessaryBecauseAllBoth, props.payload, props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH, props.onValueChange]);

  if (!props.payload) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="flex items-center gap-3 text-slate-400">
          <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
          <span className="text-sm font-headline uppercase tracking-widest">Loading settings...</span>
        </div>
      </div>
    );
  }

  const payload = props.payload;
  if (!payload.sections?.length) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 min-h-[40vh] px-6 text-center">
        <p className={`text-sm font-headline uppercase tracking-widest ${props.themeMode === "light" ? "text-slate-600" : "text-slate-400"}`}>
          No settings sections were returned from the API.
        </p>
        <p className={`text-xs max-w-md ${props.themeMode === "light" ? "text-slate-500" : "text-slate-500"}`}>
          Refresh the page or check server logs. If the problem persists, verify `/api/settings/current` returns a non-empty `sections` list.
        </p>
      </div>
    );
  }
  const active = payload.sections.find((s) => s.name === props.activeSection) || payload.sections[0];
  const canUseAnySecondaryBehavior = canUseRadarrSecondaryBehavior || canUseSonarrSecondaryBehavior;
  const allSettingsFieldsByKey = useMemo(() => {
    const m = new Map<string, SettingsField>();
    for (const section of payload.sections) {
      for (const f of section.fields) m.set(f.key, f);
    }
    return m;
  }, [payload.sections]);

  async function runTest(field: SettingsField) {
    const target = URL_TEST_TARGET[field.key];
    if (!target) return;
    setTestResults((prev) => ({ ...prev, [field.key]: { ok: true, message: "Testing..." } }));
    const result = await props.onTestConnection({
      service: target.service,
      urlKey: field.key,
      credentialKey: target.credentialKey,
    });
    setTestResults((prev) => ({ ...prev, [field.key]: result }));
  }

  function renderStandardField(field: SettingsField) {
    if (HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key) || SETTINGS_UI_HIDDEN_FIELD_KEYS.has(field.key)) return null;
    const value = props.values[field.key];
    const test = testResults[field.key];
    const testTarget = URL_TEST_TARGET[field.key];
    const statusUpdatesOff = String(props.values.PLACEHOLDER_STATUS_UPDATES ?? "").toUpperCase() === "OFF";
    const projectionFieldLocked = field.key === "PLACEHOLDER_STATUS_PROJECTION_MODE" && statusUpdatesOff;
    const tvPlayMode = String(props.values.TV_PLAY_MODE ?? "episode").trim().toLowerCase();
    const lookaheadRangeLocked = field.key === "EPISODES_LOOKAHEAD" && tvPlayMode !== "episode";
    const rowMuted = projectionFieldLocked || lookaheadRangeLocked;

    return (
      <div key={field.key} className={`px-6 py-5 ${rowMuted ? "opacity-50" : ""}`}>
        <div className="flex items-start gap-3 mb-2">
          <div className="flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-semibold text-white font-headline">{field.label}</span>
              {field.required && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase" style={{ backgroundColor: alphaColor(accent.hex, 0.3), color: accent.text }}>Required</span>}
              {field.secret && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-[#252e3a] text-slate-400">Secret</span>}
              {field.restart_required && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-orange-600/30 text-orange-300">Restart Required</span>}
            </div>
            {!(lookaheadRangeLocked && field.key === "EPISODES_LOOKAHEAD") &&
              (field.key === "STARTUP_SYNC_MODE" ? (
                <StartupSyncModeDescription spacing="settings" />
              ) : field.key === "PLACEHOLDER_STATUS_UPDATES" ? (
                <PlaceholderStatusUpdatesDescription spacing="settings" />
              ) : field.key === "ENABLE_COMING_SOON_COUNTDOWN" ? (
                <ComingSoonCountdownDescription spacing="settings" />
              ) : field.description ? (
                <p className="ui-field-description mt-1">{field.description}</p>
              ) : null)}
            {field.key === "FULL_SYNC_INTERVAL_HOURS" ? (
              <p className="ui-field-description ui-field-description-accent3 mt-2 leading-relaxed">
                If you have Startup ARR sync mode set to OFF, then a scheduled sync is recommended.
              </p>
            ) : null}
          </div>
        </div>

        {field.type === "bool" ? (
          <label className="flex items-center gap-3 cursor-pointer select-none w-fit">
            <div className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${Boolean(value) ? "" : "bg-[#252e3a]"}`}
              style={Boolean(value) ? { backgroundColor: accent.hex } : undefined}
              onClick={() => props.onValueChange(field.key, !Boolean(value))}>
              <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${Boolean(value) ? "translate-x-5" : "translate-x-0"}`} />
            </div>
            <span className="text-sm text-slate-300">{Boolean(value) ? "Enabled" : "Disabled"}</span>
          </label>
        ) : field.type === "choice" && field.options?.length ? (
          <select
            className={`w-full max-w-xl bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${projectionFieldLocked ? "cursor-not-allowed" : ""}`}
            disabled={projectionFieldLocked}
            value={(() => {
              const raw = String(value ?? field.options[0]?.value ?? "");
              if (field.key === "PLACEHOLDER_STATUS_PROJECTION_MODE" && raw.toLowerCase() === "off") return "summary";
              return raw;
            })()}
            onChange={(e) => props.onValueChange(field.key, e.target.value)}
          >
            {field.options.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        ) : (
          <div className="flex gap-2">
            <input
              className={`flex-1 bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${lookaheadRangeLocked ? "cursor-not-allowed" : ""}`}
              type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
              disabled={lookaheadRangeLocked}
              value={String(value ?? "")}
              placeholder={field.secret && field.has_saved_value ? "Saved value retained unless overwritten" : `Enter ${field.label.toLowerCase()}...`}
              onChange={e => props.onValueChange(field.key, e.target.value)}
            />
            {testTarget && (
              <button type="button" onClick={() => runTest(field)}
                className="flex items-center gap-1.5 px-3 py-2 bg-[#252e3a] hover:bg-[#30353b] border border-[#424753]/40 rounded-lg text-xs text-slate-300 font-headline uppercase tracking-wider transition-colors whitespace-nowrap">
                <span className="material-symbols-outlined" style={{ fontSize: 14 }}>wifi</span>
                Test
              </button>
            )}
          </div>
        )}

        {testTarget ? (
          <div
            className={`mt-2 flex min-h-[2.25rem] items-start gap-1.5 text-xs ${
              test && test.message !== "Testing..." ? (test.ok ? "text-green-400" : "text-red-400") : "text-slate-400"
            }`}
            aria-live="polite"
          >
            {test && test.message === "Testing..." ? (
              <>
                <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 14 }}>progress_activity</span>
                <span>Testing…</span>
              </>
            ) : test ? (
              <>
                <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
                <span className="leading-snug">{test.message}</span>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  function renderOnboardingStyleSectionRows(fields: SettingsField[], opts?: { intro?: ReactNode }) {
    return (
      <div className="px-6 py-5">
        <div className={`${UI_SECTION_FRAME_CLASS} overflow-hidden divide-y divide-[#424753]/20`}>
          {opts?.intro ? <div className="px-6 py-5">{opts.intro}</div> : null}
          {fields.map((field) => renderStandardField(field))}
        </div>
      </div>
    );
  }

  return (
    <>
    <div>
      {/* Page title row */}
      <div className="flex justify-between items-center mb-6">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <div className="w-2 h-2 rounded-full" style={{ backgroundColor: accent.hex }} />
            <span className="text-[10px] font-headline uppercase tracking-widest text-slate-400">Configuration</span>
          </div>
          <h1 className="text-3xl font-black text-white tracking-tight font-headline">Settings</h1>
        </div>
        <div className="flex items-center gap-3">
          {props.hasUnsavedChanges && (
            <span className="flex items-center gap-1.5 text-xs text-yellow-400 font-headline uppercase tracking-wider">
              <div className="w-1.5 h-1.5 rounded-full bg-yellow-400" />
              Unsaved changes
            </span>
          )}
          {props.feedback && (
            <span className={`text-xs font-headline uppercase tracking-wider ${props.feedbackKind === "success" ? "text-green-400" : "text-red-400"}`}>
              {props.feedback}
            </span>
          )}
          <button type="button" onClick={() => props.onSave()}
            className="flex items-center gap-2 px-5 py-2 text-white text-xs font-headline uppercase tracking-wider rounded-lg transition-colors"
            style={{ backgroundColor: accent.hex }}>
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>save</span>
            Save Settings
          </button>
        </div>
      </div>

      <div className="w-full min-w-0">
        {/* Active section fields */}
        <div className="w-full min-w-0">
          <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden">
            <div className="px-6 py-4 border-b border-[#424753]/30">
              <h2 className="text-base font-bold text-white font-headline">{active.name}</h2>
            </div>
            <div className="divide-y divide-[#424753]/20">
              {active.name === "Paths" ? (
                <LibraryPathsForm
                  fields={active.fields}
                  values={props.values}
                  brand={props.brand}
                  themeMode={props.themeMode}
                  accent={accent}
                  layout="settings"
                  onValueChange={props.onValueChange}
                  runTest={runTest}
                  testResults={testResults}
                />
              ) : active.name === "Media Integrations" ? (
                (() => {
                  const fieldByKey = new Map(active.fields.map((f) => [f.key, f]));
                  return (
                    <div className="space-y-5 px-6 py-5">
                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                        {ONBOARDING_MEDIA_CARDS.map((card) => {
                          const enabled = Boolean(props.values[card.enabledKey]);
                          const availableFields = card.keys.map((key) => fieldByKey.get(key)).filter(Boolean) as SettingsField[];
                          const urlField = availableFields.find((f) => URL_TEST_TARGET[f.key]);
                          const urlTest = urlField ? testResults[urlField.key] : undefined;
                          const vis = ONBOARDING_MEDIA_VISUAL[card.id];
                          const address = urlField ? String(props.values[urlField.key] ?? "").trim() : "";
                          const movieLib = card.id === "plex" ? String(props.values.PLEX_MOVIE_SECTION_ID ?? "").trim() : "";
                          const tvLib = card.id === "plex" ? String(props.values.PLEX_TV_SECTION_ID ?? "").trim() : "";
                          const mediaDetailsComplete = mediaCardConnectionDetailsComplete(card, props.values, allSettingsFieldsByKey);
                          return (
                            <div key={card.id} className={`group relative flex min-h-[250px] flex-col ${UI_INTEGRATION_CARD_SURFACE_CLASS} p-6 duration-200`}>
                              <div className="flex h-[5.25rem] w-full shrink-0 items-center justify-center" aria-hidden>
                                {card.id === "plex" ? (
                                  <div className="flex max-w-full items-center justify-center gap-2 px-2 sm:px-3">
                                    <div className={`${MEDIA_PLEX_PAIR_WELL_FRAME} h-16 w-fit shrink-0 ${MEDIA_PLEX_PAIR_LOGO_INSET}`} style={vis.well}>
                                      <img src={plexIcon} alt="" decoding="async" className="h-8 w-auto max-h-8 shrink-0 object-contain" aria-hidden />
                                    </div>
                                    <span className="select-none text-xl font-extralight leading-none text-white/85" aria-hidden>×</span>
                                    <div className={`${MEDIA_PLEX_PAIR_WELL_FRAME} h-16 w-16 shrink-0`} style={vis.well}>
                                      <img src={tautulliIcon} alt="" decoding="async" className="h-8 w-8 shrink-0 object-contain" aria-hidden />
                                    </div>
                                  </div>
                                ) : (
                                  <div className="flex h-[5.25rem] w-[5.25rem] items-center justify-center rounded-2xl" style={vis.well}>
                                    <img src={vis.iconSrc} alt="" decoding="async" className="h-10 w-10 object-contain" aria-hidden />
                                  </div>
                                )}
                              </div>
                              <h4 className="mt-5 w-full text-center text-lg font-bold tracking-tight text-white font-headline">{card.title}</h4>
                              {!enabled ? (
                                <button
                                  type="button"
                                  onClick={() => {
                                    props.onValueChange(card.enabledKey, true);
                                    setMediaPanel(card.id);
                                  }}
                                  className="mt-6 w-full rounded-xl border border-white/20 bg-white/[0.04] py-2.5 text-sm font-semibold tracking-wide text-white/95 transition hover:border-white/35 hover:bg-white/[0.09]"
                                >
                                  Connect
                                </button>
                              ) : (
                                <div className="mt-5 flex min-h-0 flex-1 flex-col text-left">
                                  {card.id === "plex" && "note" in card && card.note ? <p className="ui-field-description-compact mb-2">{card.note}</p> : null}
                                  <dl className="space-y-1.5 rounded-xl border border-white/[0.06] bg-black/20 p-3 text-[11px] leading-snug">
                                    <div className="flex min-w-0 gap-2">
                                      <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Address</dt>
                                      <dd className="truncate font-mono text-slate-200" title={address || undefined}>{address || "—"}</dd>
                                    </div>
                                    {card.id === "plex" ? (
                                      <>
                                        <div className="flex min-w-0 gap-2">
                                          <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Movie lib.</dt>
                                          <dd className="truncate font-mono text-slate-200" title={movieLib || undefined}>{movieLib || "—"}</dd>
                                        </div>
                                        <div className="flex min-w-0 gap-2">
                                          <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">TV lib.</dt>
                                          <dd className="truncate font-mono text-slate-200" title={tvLib || undefined}>{tvLib || "—"}</dd>
                                        </div>
                                      </>
                                    ) : null}
                                  </dl>
                                  <div className="mt-2 flex min-h-[2.5rem] flex-col justify-center text-xs">
                                    {urlTest && !urlTest.ok ? <p className="text-red-400">{urlTest.message}</p> : !mediaDetailsComplete ? <p className="ui-field-description">Add URL and credentials in Configure.</p> : null}
                                  </div>
                                  <div className="mt-auto flex flex-col gap-2 pt-4">
                                    <button
                                      type="button"
                                      onClick={() => setMediaPanel(card.id)}
                                      className="w-full rounded-xl border border-white/20 bg-white/[0.04] py-2.5 text-xs font-headline font-semibold uppercase tracking-wider text-slate-100 transition hover:border-white/30 hover:bg-white/[0.08]"
                                    >
                                      Configure
                                    </button>
                                    {mediaCardPlaybackWebhookConfig(card.id) ? (
                                      <button
                                        type="button"
                                        onClick={() => {
                                          const cfg = mediaCardPlaybackWebhookConfig(card.id);
                                          if (!cfg) return;
                                          const instanceParam =
                                            String(props.values[cfg.instanceKeyField] ?? "").trim() || cfg.defaultKey;
                                          setPlaybackWebhookDialog({ serviceId: cfg.serviceId, instanceParam });
                                        }}
                                        className="w-full rounded-xl border border-white/10 bg-transparent py-2.5 text-xs font-headline font-semibold uppercase tracking-wider text-slate-400 transition hover:border-white/20 hover:text-slate-200"
                                      >
                                        Webhook URL
                                      </button>
                                    ) : null}
                                    <button
                                      type="button"
                                      onClick={() => {
                                        props.onValueChange(card.enabledKey, false);
                                        setMediaPanel((p) => (p === card.id ? null : p));
                                      }}
                                      className="text-center text-[11px] font-medium text-slate-500 underline-offset-2 transition hover:text-red-300 hover:underline"
                                    >
                                      Remove connection
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      {mediaPanel ? (() => {
                        const card = ONBOARDING_MEDIA_CARDS.find((c) => c.id === mediaPanel);
                        if (!card) return null;
                        const panelFields = card.keys.map((key) => fieldByKey.get(key)).filter(Boolean) as SettingsField[];
                        return (
                          <div className={`${UI_SECTION_FRAME_CLASS} overflow-hidden divide-y divide-[#424753]/20`}>
                            <div className="px-6 py-4 border-b border-[#424753]/30">
                              <h3 className="text-base font-bold text-white font-headline">{card.title} Configuration</h3>
                            </div>
                            {panelFields.map((field) => renderStandardField(field))}
                          </div>
                        );
                      })() : null}
                      {(() => {
                        const remaining = active.fields.filter((field) => !ONBOARDING_MEDIA_CARDS.some((card) => [card.enabledKey, ...card.keys].includes(field.key)));
                        return remaining.length ? renderOnboardingStyleSectionRows(remaining) : null;
                      })()}
                    </div>
                  );
                })()
              ) : active.name === "ARR Integrations" ? (
                <>
                  {active.fields
                    .filter(
                      (field) =>
                        !ARR_CONFIGURATION_KEYS.has(field.key) &&
                        !ARR_BEHAVIOR_KEYS.has(field.key) &&
                        !HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key),
                    )
                    .map((field) => renderStandardField(field))}
                  <div className="px-6 py-5">
                    <div className="mb-3">
                      <h3 className="text-base font-bold text-white font-headline">ARR Instances</h3>
                      <p className="ui-field-description mt-1">Configure up to 2 Radarr and 2 Sonarr instances. These entries power webhook labels and instance-aware routing.</p>
                    </div>
                    <ArrInstancesEditor
                      layout="slots"
                      values={props.values}
                      onValueChange={props.onValueChange}
                      accent={accent}
                      onSecondaryTestStatusChange={(arrType, ok) => {
                        setArrSecondaryTestStatus((prev) => ({ ...prev, [arrType]: ok }));
                      }}
                    />
                  </div>

                  {/* Placeholder search mode dropdowns */}
                  <div className="px-6 pb-5">
                    <div className={`${UI_SECTION_FRAME_CLASS} p-4 space-y-4`}>
                      <div className="text-center">
                        <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Placeholder Search Behavior</h3>
                        <p className="ui-field-description mx-auto max-w-2xl">When a placeholder plays, Placeholdarr triggers a search in the corresponding ARR app. Choose which instance to search.</p>
                      </div>
                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-5">
                        <div className="min-w-0">
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                          <select
                            className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                            value={canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "both") : "na"}
                            onChange={(e) => props.onValueChange("MOVIE_PLACEHOLDER_SEARCH_MODE", e.target.value)}
                            disabled={!canUseRadarrSecondaryBehavior}
                          >
                            {canUseRadarrSecondaryBehavior ? (
                              <>
                                <option value="primary">Primary instance</option>
                                <option value="secondary">Secondary instance</option>
                                <option value="both">Both instances</option>
                              </>
                            ) : (
                              <option value="na">Not applicable, no second instance set up.</option>
                            )}
                          </select>
                          <p className="ui-field-description-compact mt-1.5">
                            {canUseRadarrSecondaryBehavior
                              ? (({ primary: "Searches your primary (standard) Radarr instance.", secondary: "Searches your secondary (4K) Radarr instance.", both: "Searches both Radarr instances — ensures full coverage." } as Record<string, string>)[String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "both")] ?? "Searches both Radarr instances.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                        <div className="min-w-0">
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                          <select
                            className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                            value={canUseSonarrSecondaryBehavior ? String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "both") : "na"}
                            onChange={(e) => props.onValueChange("TV_PLACEHOLDER_SEARCH_MODE", e.target.value)}
                            disabled={!canUseSonarrSecondaryBehavior}
                          >
                            {canUseSonarrSecondaryBehavior ? (
                              <>
                                <option value="primary">Primary instance</option>
                                <option value="secondary">Secondary instance</option>
                                <option value="both">Both instances</option>
                              </>
                            ) : (
                              <option value="na">Not applicable, no second instance set up.</option>
                            )}
                          </select>
                          <p className="ui-field-description-compact mt-1.5">
                            {canUseSonarrSecondaryBehavior
                              ? (({ primary: "Searches your primary (standard) Sonarr instance.", secondary: "Searches your secondary (4K) Sonarr instance.", both: "Searches both Sonarr instances — ensures full coverage." } as Record<string, string>)[String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "both")] ?? "Searches both Sonarr instances.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                      </div>
                    </div>
                    <div className={`mt-3 ${UI_SECTION_FRAME_CLASS} p-4 space-y-4`}>
                      <div className="text-center">
                        <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Real-File Search Behavior</h3>
                        <p className="ui-field-description mx-auto max-w-2xl">When an actual media file is played, choose how Placeholdarr routes the playback-triggered ARR search.</p>
                      </div>
                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-5">
                        <div className="min-w-0">
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                          <select
                            className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                            value={canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match") : "na"}
                            onChange={(e) => props.onValueChange("MOVIE_PLAYBACK_INSTANCE_MODE", e.target.value)}
                            disabled={!canUseRadarrSecondaryBehavior}
                          >
                            {canUseRadarrSecondaryBehavior ? (
                              <>
                                <option value="match">Match by library path</option>
                                <option value="primary">Primary instance</option>
                                <option value="secondary">Secondary instance</option>
                                <option value="both">Both instances</option>
                              </>
                            ) : (
                              <option value="na">Not applicable, no second instance set up.</option>
                            )}
                          </select>
                          <p className="ui-field-description-compact mt-1.5">
                            {canUseRadarrSecondaryBehavior
                              ? (({ match: "Uses the movie file path to determine which Radarr instance should be searched.", primary: "Always searches your primary (standard) Radarr instance.", secondary: "Always searches your secondary (4K) Radarr instance.", both: "Searches both Radarr instances." } as Record<string, string>)[String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match")] ?? "Uses the movie file path to determine which Radarr instance should be searched.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                        <div className="min-w-0">
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                          <select
                            className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                            value={canUseSonarrSecondaryBehavior ? String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match") : "na"}
                            onChange={(e) => props.onValueChange("TV_PLAYBACK_INSTANCE_MODE", e.target.value)}
                            disabled={!canUseSonarrSecondaryBehavior}
                          >
                            {canUseSonarrSecondaryBehavior ? (
                              <>
                                <option value="match">Match by library path</option>
                                <option value="primary">Primary instance</option>
                                <option value="secondary">Secondary instance</option>
                                <option value="both">Both instances</option>
                              </>
                            ) : (
                              <option value="na">Not applicable, no second instance set up.</option>
                            )}
                          </select>
                          <p className="ui-field-description-compact mt-1.5">
                            {canUseSonarrSecondaryBehavior
                              ? (({ match: "Uses the TV file path to determine which Sonarr instance should be searched.", primary: "Always searches your primary (standard) Sonarr instance.", secondary: "Always searches your secondary (4K) Sonarr instance.", both: "Searches both Sonarr instances." } as Record<string, string>)[String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match")] ?? "Uses the TV file path to determine which Sonarr instance should be searched.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                      </div>
                      <div className="border-t border-[#424753]/20 pt-4">
                        <div className="mx-auto flex max-w-lg flex-col items-center gap-4 text-center">
                          <div>
                            <div className="text-xs font-semibold text-slate-300">Fallback search</div>
                            <div className="ui-field-description mt-1">
                              {fallbackUnnecessaryBecauseAllBoth ? (
                                "Fallback is not needed because every unlocked search behavior already searches both instances."
                              ) : canUseAnySecondaryBehavior ? (
                                <div className="mx-auto w-full max-w-md text-left">
                                  <p>When enabled, the non-selected instance is searched automatically if:</p>
                                  <ul className="mt-2 list-disc space-y-1.5 pl-4">
                                    <li>The selected instance doesn&apos;t have the content added (immediate fallback search), or</li>
                                    <li>
                                      The content isn&apos;t imported before the fallback timeout (e.g. content not found, indexer/download errors, etc.)
                                    </li>
                                  </ul>
                                </div>
                              ) : (
                                "Not applicable, no second instance set up."
                              )}
                            </div>
                          </div>
                          <label className="flex cursor-pointer select-none items-center justify-center gap-3">
                            <div
                              className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth ? (Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? "" : "bg-[#252e3a]") : "bg-[#1a1f27] opacity-60 cursor-not-allowed"}`}
                              style={canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? { backgroundColor: accent.hex } : undefined}
                              onClick={() => canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && props.onValueChange("ENABLE_PLAYBACK_FALLBACK_SEARCH", !Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH))}
                            >
                              <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? "translate-x-5" : "translate-x-0"}`} />
                            </div>
                            <span className="text-sm text-slate-300">
                              {fallbackUnnecessaryBecauseAllBoth
                                ? "Not needed"
                                : canUseAnySecondaryBehavior
                                ? (Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? "Enabled" : "Disabled")
                                : "Not applicable, no second instance set up."}
                            </span>
                          </label>
                          <div className="text-center">
                            <label className="block text-xs font-semibold text-slate-300 mb-1.5">Fallback timeout (minutes)</label>
                            {canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? (
                              <input
                                className={`mx-auto mt-0.5 block w-[4.25rem] bg-[#0b111b] border border-[#424753]/40 rounded-lg px-2 py-2 text-center text-sm tabular-nums tracking-tight text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)}`}
                                type="text"
                                inputMode="numeric"
                                maxLength={3}
                                autoComplete="off"
                                value={(() => {
                                  const raw = props.values.PLAYBACK_FALLBACK_TIMEOUT_MINUTES;
                                  const digits = String(raw ?? "").replace(/\D/g, "").slice(0, 3);
                                  if (digits.length > 0) return digits;
                                  return raw === undefined || raw === null || String(raw).trim() === "" ? "30" : "";
                                })()}
                                onChange={(e) => {
                                  const d = e.target.value.replace(/\D/g, "").slice(0, 3);
                                  props.onValueChange("PLAYBACK_FALLBACK_TIMEOUT_MINUTES", d);
                                }}
                              />
                            ) : fallbackUnnecessaryBecauseAllBoth ? (
                              <input
                                className="mx-auto mt-0.5 block w-full max-w-md bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                                type="text"
                                value="Not needed because all unlocked behaviors already search both instances."
                                disabled
                              />
                            ) : canUseAnySecondaryBehavior ? (
                              <input
                                className="mx-auto mt-0.5 block w-full max-w-md bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                                type="text"
                                value="Enable fallback search."
                                disabled
                              />
                            ) : (
                              <input
                                className="mx-auto mt-0.5 block w-full max-w-md bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                                type="text"
                                value="Not applicable, no second instance set up."
                                disabled
                              />
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </>
              ) : active.name === "Lookahead" ? (
                renderOnboardingStyleSectionRows(active.fields, {
                  intro: <LookaheadSectionIntro variant="onboarding" embedded />,
                })
              ) : active.name === "Status Updates" ? (
                renderOnboardingStyleSectionRows(active.fields, {
                  intro: <StatusUpdatesSectionIntro variant="onboarding" embedded />,
                })
              ) : active.name === "Library sync" ? (
                renderOnboardingStyleSectionRows(active.fields)
              ) : (
                renderOnboardingStyleSectionRows(active.fields)
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
    {playbackWebhookDialog ? (
      <PlaybackWebhookSetupModal
        dialog={playbackWebhookDialog}
        onClose={() => setPlaybackWebhookDialog(null)}
        accent={accent}
      />
    ) : null}
    </>
  );
}

const ONBOARDING_MEDIA_CARDS = [
  {
    id: "plex" as const,
    title: "Plex",
    enabledKey: "ENABLE_PLEX",
    note: "Tautulli is required for Plex playback webhooks and playback-aware routing.",
    keys: ["PLEX_URL", "PLEX_TOKEN", "PLEX_MOVIE_SECTION_ID", "PLEX_TV_SECTION_ID", "TAUTULLI_INSTANCE_KEY"],
  },
  {
    id: "jellyfin" as const,
    title: "Jellyfin",
    enabledKey: "ENABLE_JELLYFIN",
    keys: ["JELLYFIN_URL", "JELLYFIN_TOKEN", "JELLYFIN_INSTANCE_KEY"],
  },
  {
    id: "emby" as const,
    title: "Emby",
    enabledKey: "ENABLE_EMBY",
    keys: ["EMBY_URL", "EMBY_TOKEN", "EMBY_INSTANCE_KEY"],
  },
];

/** Logo tile fill — solid studio navy (sidebar brand strip / dark chrome wells). */
const INTEGRATION_LOGO_WELL_BG = "#1e2430";

const ONBOARDING_MEDIA_VISUAL: Record<
  (typeof ONBOARDING_MEDIA_CARDS)[number]["id"],
  { iconSrc: string; well: CSSProperties }
> = {
  /** Icon wells: solid navy + service-colored ring (light and dark). */
  plex: {
    iconSrc: plexIcon,
    well: {
      backgroundColor: INTEGRATION_LOGO_WELL_BG,
      border: "2px solid rgba(251, 191, 36, 0.75)",
      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
    },
  },
  jellyfin: {
    iconSrc: jellyfinIcon,
    well: {
      backgroundColor: INTEGRATION_LOGO_WELL_BG,
      border: "2px solid rgba(34, 211, 238, 0.8)",
      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
    },
  },
  emby: {
    iconSrc: embyIcon,
    well: {
      backgroundColor: INTEGRATION_LOGO_WELL_BG,
      border: "2px solid rgba(74, 222, 128, 0.78)",
      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
    },
  },
};

const ONBOARDING_ARR_VISUAL: Record<"radarr" | "sonarr", { iconSrc: string; well: CSSProperties }> = {
  radarr: {
    iconSrc: radarrIcon,
    well: {
      backgroundColor: INTEGRATION_LOGO_WELL_BG,
      border: "2px solid rgba(250, 204, 21, 0.78)",
      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
    },
  },
  sonarr: {
    iconSrc: sonarrIcon,
    well: {
      backgroundColor: INTEGRATION_LOGO_WELL_BG,
      border: "2px solid rgba(56, 189, 248, 0.8)",
      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
    },
  },
};

const TAUTULLI_WEBHOOK_PAYLOAD_TEMPLATE = `{
    "event": "playback.start",
    "media": {
        "type": "{media_type}",
        "title": "{title}",
        "show_name": "{show_name}",
        "episode_name": "{episode_name}",
        "season_num": "{season_num}",
        "episode_num": "{episode_num}",
        "year": "{year}",
        "ids": {
            "plex": "{rating_key}",
            "tmdb": "{themoviedb_id}",
            "tvdb": "{thetvdb_id}",
            "imdb": "{imdb_id}"
        },
        "file_info": {
            "path": "{file}"
        }
    }
}`;

const JELLYFIN_WEBHOOK_PAYLOAD_TEMPLATE = `{
  "event": "playback.start",
  "ItemId": "{{ItemId}}",
  "UserId": "{{UserId}}",
  "Name": "{{Name}}",
  "ItemType": "{{ItemType}}",
  "SeriesName": "{{SeriesName}}",
  "SeasonNumber": "{{SeasonNumber}}",
  "EpisodeNumber": "{{EpisodeNumber}}",
  "Provider_tmdb": "{{Provider_tmdb}}",
  "Provider_tvdb": "{{Provider_tvdb}}",
  "Provider_imdb": "{{Provider_imdb}}",
  "Year": "{{Year}}",
  "NotificationType": "{{NotificationType}}"
}`;

type PlaybackWebhookServiceId = "tautulli" | "jellyfin" | "emby";

function mediaCardPlaybackWebhookConfig(cardId: (typeof ONBOARDING_MEDIA_CARDS)[number]["id"]): {
  serviceId: PlaybackWebhookServiceId;
  instanceKeyField: string;
  defaultKey: string;
} | null {
  if (cardId === "plex") return { serviceId: "tautulli", instanceKeyField: "TAUTULLI_INSTANCE_KEY", defaultKey: "tautulli" };
  if (cardId === "jellyfin") return { serviceId: "jellyfin", instanceKeyField: "JELLYFIN_INSTANCE_KEY", defaultKey: "jellyfin" };
  if (cardId === "emby") return { serviceId: "emby", instanceKeyField: "EMBY_INSTANCE_KEY", defaultKey: "emby" };
  return null;
}

/** Plex × Tautulli on the media step card: smaller wells + `h-8` marks so the row fits the panel with side padding. */
const MEDIA_PLEX_PAIR_WELL_FRAME = "flex items-center justify-center rounded-2xl";
const MEDIA_PLEX_PAIR_LOGO_INSET = "p-[calc((4rem-2rem)/2)]";

function mediaCardConnectionKeys(card: (typeof ONBOARDING_MEDIA_CARDS)[number]): { urlKey: string; credentialKey: string } | null {
  const urlKey = card.keys.find((k) => Boolean(URL_TEST_TARGET[k]));
  if (!urlKey) return null;
  const target = URL_TEST_TARGET[urlKey];
  if (!target) return null;
  return { urlKey, credentialKey: target.credentialKey };
}

function WebhookStepCopyButton(props: { text: string; ariaLabel: string; variant?: "inline" | "header"; className?: string }) {
  const variant = props.variant ?? "inline";
  const iconSize = variant === "header" ? 16 : 14;
  const [copyHint, setCopyHint] = useState<"idle" | "ok" | "err">("idle");
  const title =
    copyHint === "ok" ? "Copied" : copyHint === "err" ? "Copy failed — select URL text or use HTTPS" : "Copy to clipboard";
  return (
    <button
      type="button"
      aria-label={props.ariaLabel}
      title={title}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        void (async () => {
          const ok = await copyTextToClipboard(props.text);
          setCopyHint(ok ? "ok" : "err");
          window.setTimeout(() => setCopyHint("idle"), 2200);
        })();
      }}
      className={`inline-flex shrink-0 items-center justify-center rounded border border-[#424753]/50 bg-[#252e3a]/80 text-slate-300 transition hover:border-[#424753]/80 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/20 ${
        variant === "header" ? "h-7 w-7" : "h-6 w-6"
      } ${props.className ?? ""}`}
    >
      <span className="material-symbols-outlined" style={{ fontSize: iconSize }} aria-hidden>
        content_copy
      </span>
    </button>
  );
}

function PlaybackWebhookSetupModal(props: {
  dialog: { serviceId: PlaybackWebhookServiceId; instanceParam: string };
  onClose: () => void;
  accent: { hex: string };
}) {
  const pb = props.dialog;
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const webhookUrl = `${origin}/webhook?instance=${encodeURIComponent(pb.instanceParam)}`;
  const svcMeta = PLAYBACK_WEBHOOK_SERVICES.services.find((s) => s.id === pb.serviceId);
  const name = svcMeta?.name ?? pb.serviceId;
  return (
    <div className="fixed inset-0 z-[85] flex items-center justify-center bg-[#0f1419]/85 backdrop-blur-sm p-6">
      <div className="w-full max-w-lg max-h-[min(90vh,720px)] overflow-y-auto rounded-2xl border border-[#424753]/40 bg-[#171c22] p-6 shadow-2xl space-y-4">
        <h3 className="text-lg font-headline font-bold text-white">Configure webhooks in {name}</h3>
        <p className="text-sm text-slate-300">
          {pb.serviceId === "tautulli"
            ? `${name} must notify Placeholdarr at this URL so Plex playback is tracked.`
            : `${name} can send playback events to Placeholdarr using this URL.`}
        </p>
        <ol className="ui-field-description space-y-2 list-decimal list-inside text-sm text-slate-300">
          {pb.serviceId === "tautulli" ? (
            <>
              <li>Open Tautulli and go to Settings → Notification Agents.</li>
              <li>Create a new Webhook notification agent.</li>
              <li>Set Trigger to Playback Start and Payload Format to JSON.</li>
              <li>
                <span className="text-slate-200">Webhook URL</span>
                <div className="mt-1 flex items-start gap-2 pl-0">
                  <span className="min-w-0 flex-1 break-all font-mono text-[12px] leading-snug text-slate-300">{webhookUrl}</span>
                  <WebhookStepCopyButton text={webhookUrl} ariaLabel={`Copy ${name} webhook URL`} className="mt-0.5 shrink-0" />
                </div>
              </li>
              <li>Paste the JSON payload template below, then save.</li>
            </>
          ) : pb.serviceId === "jellyfin" ? (
            <>
              <li>In Jellyfin, install the Webhook plugin (Dashboard → Plugins → Catalog) if needed.</li>
              <li>Go to Dashboard → Plugins → Webhook and click Add Webhook.</li>
              <li>Set Events to include Playback Start and Content Type to application/json.</li>
              <li>
                <span className="text-slate-200">Webhook URL</span>
                <div className="mt-1 flex items-start gap-2 pl-0">
                  <span className="min-w-0 flex-1 break-all font-mono text-[12px] leading-snug text-slate-300">{webhookUrl}</span>
                  <WebhookStepCopyButton text={webhookUrl} ariaLabel={`Copy ${name} webhook URL`} className="mt-0.5 shrink-0" />
                </div>
              </li>
              <li>Paste the JSON payload template below, then save the webhook.</li>
            </>
          ) : (
            <>
              <li>In Emby, go to Settings → Notifications.</li>
              <li>Add or edit a webhook notification.</li>
              <li>
                <span className="text-slate-200">Webhook URL</span>
                <div className="mt-1 flex items-start gap-2 pl-0">
                  <span className="min-w-0 flex-1 break-all font-mono text-[12px] leading-snug text-slate-300">{webhookUrl}</span>
                  <WebhookStepCopyButton text={webhookUrl} ariaLabel={`Copy ${name} webhook URL`} className="mt-0.5 shrink-0" />
                </div>
              </li>
              <li>Enable the playback events you want Placeholdarr to process, then save.</li>
            </>
          )}
        </ol>
        {svcMeta && svcMeta.triggers.length ? (
          <div>
            <div className="text-xs font-semibold text-slate-400 mb-1.5">Suggested events</div>
            <div className="ml-1 space-y-1">
              {svcMeta.triggers.map((t) => (
                <div key={t.event} className="text-xs text-slate-300">
                  {t.displayName}
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-slate-500">Playback webhooks are optional; enable the events you care about.</p>
          </div>
        ) : null}
        {pb.serviceId === "tautulli" || pb.serviceId === "jellyfin" ? (
          <div>
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <div className="text-[10px] font-headline uppercase tracking-wider text-slate-500">JSON payload template</div>
              <WebhookStepCopyButton
                text={pb.serviceId === "tautulli" ? TAUTULLI_WEBHOOK_PAYLOAD_TEMPLATE : JELLYFIN_WEBHOOK_PAYLOAD_TEMPLATE}
                ariaLabel={`Copy ${name} JSON payload template`}
                variant="header"
              />
            </div>
            <pre className="overflow-x-auto rounded border border-[#424753]/40 bg-[#0a0d11] p-3 text-[11px] font-mono leading-relaxed text-slate-300">
              <code>{pb.serviceId === "tautulli" ? TAUTULLI_WEBHOOK_PAYLOAD_TEMPLATE : JELLYFIN_WEBHOOK_PAYLOAD_TEMPLATE}</code>
            </pre>
          </div>
        ) : null}
        <div className="flex justify-end pt-2">
          <button
            type="button"
            className="px-5 py-2 rounded-lg text-xs font-headline uppercase tracking-wider text-white"
            style={{ backgroundColor: props.accent.hex }}
            onClick={props.onClose}
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

function mediaCardConnectionDetailsComplete(
  card: (typeof ONBOARDING_MEDIA_CARDS)[number],
  values: FieldValueMap,
  fieldsByKey?: Map<string, SettingsField>,
): boolean {
  const keys = mediaCardConnectionKeys(card);
  if (!keys) return false;
  if (String(values[keys.urlKey] ?? "").trim().length === 0) return false;
  const cred = String(values[keys.credentialKey] ?? "").trim();
  if (cred.length > 0) return true;
  const credField = fieldsByKey?.get(keys.credentialKey);
  return Boolean(credField?.secret && credField.has_saved_value);
}

/**
 * **Spectral Data** palette (marketing guide) — recorded for hero + future UI:
 * - **SLATE** (primary dark) `#0F172A` → token `surfacePanel`
 * - **CYBER YELLOW** (accent) `#FBBF24` → token `accent`
 * - **SURFACE** (void base) `#0B1326` → token `chromePage`
 * - **Spectral cyan** (marketing) → `PLACEHOLDARR_SPECTRAL_CYAN_HEX` in `brandSemanticTheme.ts` (`#22D3EE`; UI ice is `accentIce` `#7DD3FC`)
 *
 * Variants:
 * - `blueWhite` — horizontal multiply: **CYBER YELLOW** (`accent`) at L/R edges → **void** (`chromePage`) center; lockup **CYBER YELLOW** (`accent`).
 * - `yellowBlue` — yellow multiply (`accent`), lockup **SLATE** (`surfacePanel`) — original logo / wordmark blue.
 */
const ONBOARDING_HERO_BANNER_VARIANT: "blueWhite" | "yellowBlue" = "blueWhite";

/** Raster logo → slate `surfacePanel` (`#0F172A`). Outline: thin accent ring on wrapper (yellowBlue). */
const ONBOARDING_HERO_LOGO_FILTER_SLATE =
  "object-contain [filter:brightness(0)_saturate(100%)_invert(10%)_sepia(22%)_saturate(4200%)_hue-rotate(191deg)_brightness(0.96)_contrast(1.04)]";

function OnboardingWizardHeroBanner(props: { footerBlendHex: string }) {
  const simAccent = getBrandAccent("placeholdarr", "dark");
  const sim = getBrandSemanticTokens("placeholdarr", "dark", simAccent);
  const variant = ONBOARDING_HERO_BANNER_VARIANT;
  const isYellow = variant === "yellowBlue";

  const [wideHeroGrid, setWideHeroGrid] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia("(min-width: 520px)").matches : false,
  );
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 520px)");
    const onChange = () => setWideHeroGrid(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const heroPosterSrc = (index: number) => {
    const base = WIZARD_HEADER_POSTER_PATHS[index];
    if (!isYellow) return base;
    const slots = wideHeroGrid ? ONBOARDING_HERO_LIGHT_CENTER_SLOTS_WIDE : ONBOARDING_HERO_LIGHT_CENTER_SLOTS_NARROW;
    const slotIndex = slots.findIndex((cell) => cell === index);
    if (slotIndex === -1) return base;
    return ONBOARDING_HERO_LIGHT_CENTER_POSTERS[slotIndex];
  };

  const posterImgClass = isYellow
    ? "h-full w-full object-cover grayscale contrast-[1.05] brightness-[1.08]"
    : "h-full w-full object-cover grayscale contrast-[1.06] brightness-[1.04]";
  const posterGridGapColor = sim.chromePage;
  const slateLogoBrandYellowOutlineStyle: CSSProperties | undefined = isYellow
    ? {
        filter: [
          `drop-shadow(1.5px 0 0 ${sim.accent})`,
          `drop-shadow(-1.5px 0 0 ${sim.accent})`,
          `drop-shadow(0 1.5px 0 ${sim.accent})`,
          `drop-shadow(0 -1.5px 0 ${sim.accent})`,
        ].join(' '),
      }
    : undefined;

  return (
    <div
      className="relative mb-5 flex min-h-[220px] shrink-0 flex-col overflow-x-hidden overflow-y-visible pb-4 pt-3 sm:mb-6 sm:min-h-[240px] sm:pb-5 sm:pt-4"
      style={{ width: "100vw", marginLeft: "calc(50% - 50vw)" }}
    >
      <div className="absolute inset-0" aria-hidden>
        <div
          className="absolute inset-0 grid grid-cols-4 grid-rows-4 min-[520px]:grid-cols-8 min-[520px]:grid-rows-2 gap-px"
          style={{ backgroundColor: posterGridGapColor }}
        >
          {WIZARD_HEADER_POSTER_PATHS.map((_path, i) => (
            <div key={`${i}-${heroPosterSrc(i)}`} className="relative min-h-0 min-w-0 overflow-hidden">
              <img
                src={`${TMDB_POSTER_IMG_BASE}${heroPosterSrc(i)}`}
                alt=""
                className={posterImgClass}
                loading="lazy"
                decoding="async"
              />
            </div>
          ))}
        </div>
        {isYellow ? (
          <div
            className="pointer-events-none absolute inset-0 mix-blend-multiply opacity-[0.58]"
            style={{ backgroundColor: sim.accent }}
            aria-hidden
          />
        ) : (
          <div
            className="pointer-events-none absolute inset-0 mix-blend-multiply opacity-[0.76]"
            style={{
              background: `linear-gradient(90deg,
                ${alphaColor(sim.accent, 1)} 0%,
                ${alphaColor(sim.accent, 0.95)} 25%,
                ${alphaColor(sim.chromePage, 1)} 30%,
                ${alphaColor(sim.chromePage, 1)} 70%,
                ${alphaColor(sim.accent, 0.95)} 75%,
                ${alphaColor(sim.accent, 1)} 100%)`,
            }}
            aria-hidden
          />
        )}
      </div>
      {isYellow ? (
        <>
          <div
            className="pointer-events-none absolute inset-0"
            style={{
              background: `linear-gradient(to bottom, ${alphaColor(simAccent.hoverHex, 0.42)}, ${alphaColor(sim.accent, 0.14)}, ${alphaColor(sim.chromePage, 0.68)})`,
            }}
            aria-hidden
          />
          <div
            className="pointer-events-none absolute inset-0"
            style={{
              background: `radial-gradient(ellipse 58% 70% at 50% 38%, transparent 0%, transparent 20%, ${alphaColor(simAccent.hoverHex, 0.32)} 52%, ${alphaColor(sim.chromePage, 0.78)} 100%)`,
            }}
            aria-hidden
          />
        </>
      ) : (
        <>
          <div
            className="pointer-events-none absolute inset-0"
            style={{
              background: `linear-gradient(to bottom, ${alphaColor(sim.chromePage, 0.22)}, ${alphaColor(sim.chromePage, 0.06)}, ${alphaColor(sim.chromePage, 0.26)})`,
            }}
            aria-hidden
          />
          <div
            className="pointer-events-none absolute inset-0"
            style={{
              background: `radial-gradient(ellipse 72% 88% at 50% 44%, transparent 0%, transparent 48%, ${alphaColor(sim.chromePage, 0.1)} 78%, ${alphaColor(sim.chromePage, 0.28)} 100%)`,
            }}
            aria-hidden
          />
        </>
      )}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 bottom-0 z-[9] h-[5.25rem] sm:h-28"
        style={{
          background: `linear-gradient(to top, ${alphaColor(props.footerBlendHex, 0.97)} 0%, ${alphaColor(props.footerBlendHex, 0.65)} 28%, ${alphaColor(props.footerBlendHex, 0.22)} 62%, transparent 100%)`,
        }}
      />
      <div className="relative z-10 mx-auto flex w-full flex-col items-center justify-center overflow-visible px-4 py-10 sm:py-12">
        <div className="relative z-10 flex flex-col items-center justify-center gap-2.5 px-4 text-center sm:gap-3.5">
          <div className="inline-block" style={slateLogoBrandYellowOutlineStyle}>
            {isYellow ? (
              <BrandLogo
                brand="placeholdarr"
                accentHex={simAccent.hex}
                className={`h-16 w-auto max-w-[13rem] shrink-0 object-contain object-center sm:h-[5rem] sm:max-w-[15.5rem] ${ONBOARDING_HERO_LOGO_FILTER_SLATE}`}
              />
            ) : (
              <img
                src={placeholdarrLogoYellow}
                alt=""
                aria-hidden
                draggable={false}
                className="block h-16 w-auto max-w-[13rem] shrink-0 object-contain object-center select-none sm:h-[5rem] sm:max-w-[15.5rem]"
              />
            )}
          </div>
          <span
            className={`font-headline text-2xl font-black tracking-tight sm:text-3xl [paint-order:stroke_fill] ${
              isYellow
                ? "drop-shadow-[0_2px_6px_rgba(0,0,0,0.35)]"
                : "[-webkit-text-stroke:1.25px_rgba(15,23,42,0.55)] sm:[-webkit-text-stroke:1.5px_rgba(15,23,42,0.58)] drop-shadow-[0_1px_3px_rgba(0,0,0,0.45)]"
            }`}
            style={{
              color: isYellow ? sim.surfacePanel : sim.accent,
              ...(isYellow
                ? { WebkitTextStroke: `1.75px ${sim.accent}`, paintOrder: 'stroke fill' as const }
                : {}),
            }}
          >
            Placeholdarr
          </span>
        </div>
      </div>
    </div>
  );
}

function OnboardingWizard(props: {
  payload: SettingsPayload;
  stepIndex: number;
  values: FieldValueMap;
  hasUnsavedChanges: boolean;
  brand: Brand;
  themeMode: ThemeMode;
  onBack: () => void;
  onNext: () => void;
  onChange: (key: string, value: unknown) => void;
  onSave: () => Promise<void>;
  onTestConnection: (input: { service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr"; urlKey: string; credentialKey: string }) => Promise<{ ok: boolean; message: string }>;
  onPartialSave?: (result: any, partialValues: Record<string, unknown>) => Promise<void> | void;
}) {
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [arrPrimaryTestStatus, setArrPrimaryTestStatus] = useState<{ radarr: boolean; sonarr: boolean }>({ radarr: false, sonarr: false });
  const [arrSecondaryTestStatus, setArrSecondaryTestStatus] = useState<{ radarr: boolean; sonarr: boolean }>({ radarr: false, sonarr: false });
  const stepContentRef = useRef<HTMLDivElement | null>(null);
  const step = WIZARD_STEPS[props.stepIndex];
  /** Setup runs before theme toggle is exposed — keep wizard chrome and tokens on dark. */
  const wizardUiTheme: ThemeMode = "dark";
  const accent = getBrandAccent(props.brand, wizardUiTheme);
  const wizardSemantic = getBrandSemanticTokens(props.brand, wizardUiTheme, accent);
  const wizardShellStyle = {
    ...(semanticTokensToCssVars(wizardSemantic) as CSSProperties),
    backgroundImage: getStudioDarkBackdrop(props.brand, accent, wizardSemantic),
  } as CSSProperties;
  const arrInstances = parseArrInstancesFromValues(props.values);
  const hasRadarrSecondary = arrInstances.filter((item) => item.arr_type === "radarr").length > 1;
  const hasSonarrSecondary = arrInstances.filter((item) => item.arr_type === "sonarr").length > 1;
  const uiHasRadarrSecondary = hasRadarrSecondary || Boolean(props.values.WIZARD_RADARR_SECONDARY_ENABLED);
  const uiHasSonarrSecondary = hasSonarrSecondary || Boolean(props.values.WIZARD_SONARR_SECONDARY_ENABLED);
  const canUseRadarrSecondaryBehavior = uiHasRadarrSecondary && arrSecondaryTestStatus.radarr;
  const canUseSonarrSecondaryBehavior = uiHasSonarrSecondary && arrSecondaryTestStatus.sonarr;
  const canUseAnySecondaryBehavior = canUseRadarrSecondaryBehavior || canUseSonarrSecondaryBehavior;
  const hasLibraryRoot = String(props.values.LIBRARY_ROOT ?? "").trim().length > 0;
  const allSettingsFieldsByKey = useMemo(() => {
    const m = new Map<string, SettingsField>();
    for (const section of props.payload.sections) {
      for (const f of section.fields) {
        m.set(f.key, f);
      }
    }
    return m;
  }, [props.payload]);
  const hasConfirmedMediaConnection = ONBOARDING_MEDIA_CARDS.some((card) => {
    if (!Boolean(props.values[card.enabledKey])) return false;
    const conn = mediaCardConnectionKeys(card);
    if (!conn) return false;
    return Boolean(testResults[conn.urlKey]?.ok) || mediaCardConnectionDetailsComplete(card, props.values, allSettingsFieldsByKey);
  });
  const hasConfirmedArrConnection =
    arrPrimaryTestStatus.radarr ||
    arrPrimaryTestStatus.sonarr ||
    arrPrimaryPersistedWithCredentials(props.values, "radarr") ||
    arrPrimaryPersistedWithCredentials(props.values, "sonarr");
  const keys = fieldsForWizardStep(step.key, props.payload.sections);
  const fields = props.payload.sections.flatMap((section) => section.fields).filter((f) => keys.includes(f.key));
  const [stepSaving, setStepSaving] = useState(false);
  const [stepError, setStepError] = useState<string | null>(null);
  const [mediaPanel, setMediaPanel] = useState<null | (typeof ONBOARDING_MEDIA_CARDS)[number]["id"]>(null);
  const [mediaPanelTestPassed, setMediaPanelTestPassed] = useState(false);
  const [mediaFooterTestBusy, setMediaFooterTestBusy] = useState(false);
  const [playbackWebhookDialog, setPlaybackWebhookDialog] = useState<{
    serviceId: PlaybackWebhookServiceId;
    instanceParam: string;
  } | null>(null);
  const mediaPanelOpenedViaAddRef = useRef(false);
  const mediaPanelSnapshotRef = useRef<Record<string, unknown>>({});

  const hasUnlockedSearchBehavior = [
    canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "primary") : null,
    canUseSonarrSecondaryBehavior ? String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "primary") : null,
    canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match") : null,
    canUseSonarrSecondaryBehavior ? String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match") : null,
  ].filter((value): value is string => Boolean(value));
  const fallbackUnnecessaryBecauseAllBoth = hasUnlockedSearchBehavior.length > 0 && hasUnlockedSearchBehavior.every((value) => value === "both");
  const canProceed = (() => {
    if (step.key === "paths") return hasLibraryRoot;
    if (step.key === "media") return hasConfirmedMediaConnection;
    if (step.key === "arr") return hasConfirmedArrConnection;
    return true;
  })();

  useEffect(() => {
    if (fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH)) {
      props.onChange("ENABLE_PLAYBACK_FALLBACK_SEARCH", false);
    }
  }, [fallbackUnnecessaryBecauseAllBoth, props, props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH]);

  useEffect(() => {
    if (step.key !== "media") {
      setMediaPanel(null);
      setMediaPanelTestPassed(false);
    }
  }, [step.key]);

  useEffect(() => {
    const container = stepContentRef.current;
    if (container) {
      container.scrollTop = 0;
    }
    window.scrollTo(0, 0);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, [props.stepIndex]);

  async function runTest(field: SettingsField): Promise<{ ok: boolean; message: string } | null> {
    const target = URL_TEST_TARGET[field.key];
    if (!target) return null;
    setTestResults((prev) => ({ ...prev, [field.key]: { ok: true, message: "Testing..." } }));
    const result = await props.onTestConnection({
      service: target.service,
      urlKey: field.key,
      credentialKey: target.credentialKey,
    });
    setTestResults((prev) => ({ ...prev, [field.key]: result }));
    return result;
  }

  function handleMediaPanelFieldChange(key: string, value: unknown) {
    setMediaPanelTestPassed(false);
    if (mediaPanel) {
      const c = ONBOARDING_MEDIA_CARDS.find((x) => x.id === mediaPanel);
      const conn = c ? mediaCardConnectionKeys(c) : null;
      if (conn && (key === conn.urlKey || key === conn.credentialKey)) {
        setTestResults((prev) => {
          const next = { ...prev };
          delete next[conn.urlKey];
          return next;
        });
      }
    }
    props.onChange(key, value);
  }

  function handleMediaPanelCancel() {
    if (!mediaPanel) {
      setMediaPanel(null);
      return;
    }
    const card = ONBOARDING_MEDIA_CARDS.find((c) => c.id === mediaPanel);
    if (!card) {
      setMediaPanel(null);
      return;
    }
    if (mediaPanelOpenedViaAddRef.current) {
      props.onChange(card.enabledKey, false);
      for (const k of card.keys) {
        props.onChange(k, "");
      }
    } else {
      const snap = mediaPanelSnapshotRef.current;
      for (const [k, v] of Object.entries(snap)) {
        props.onChange(k, v);
      }
    }
    setMediaPanelTestPassed(false);
    setMediaPanel(null);
  }

  function buildStepPartialValues(stepKeys: string[]) {
    const partial: Record<string, unknown> = {};
    for (const key of stepKeys) partial[key] = props.values[key];

    if (step.key === "arr") {
      partial.MOVIE_PLACEHOLDER_SEARCH_MODE = canUseRadarrSecondaryBehavior
        ? String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "primary")
        : "primary";
      partial.TV_PLACEHOLDER_SEARCH_MODE = canUseSonarrSecondaryBehavior
        ? String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "primary")
        : "primary";
      partial.MOVIE_PLAYBACK_INSTANCE_MODE = canUseRadarrSecondaryBehavior
        ? String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match")
        : "match";
      partial.TV_PLAYBACK_INSTANCE_MODE = canUseSonarrSecondaryBehavior
        ? String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match")
        : "match";
      partial.ENABLE_PLAYBACK_FALLBACK_SEARCH = canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth
        ? Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH)
        : false;
      partial.PLAYBACK_FALLBACK_TIMEOUT_MINUTES = String(props.values.PLAYBACK_FALLBACK_TIMEOUT_MINUTES ?? "").trim() || 30;
    }

    return partial;
  }

  function wizardFieldRow(field: SettingsField) {
    if (HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key) || SETTINGS_UI_HIDDEN_FIELD_KEYS.has(field.key)) return null;
    const test = testResults[field.key];
    const testTarget = URL_TEST_TARGET[field.key];
    const focus = getBrandFocusClass(props.brand, wizardUiTheme);
    const statusUpdatesOff = String(props.values.PLACEHOLDER_STATUS_UPDATES ?? "").toUpperCase() === "OFF";
    const projectionFieldLocked = field.key === "PLACEHOLDER_STATUS_PROJECTION_MODE" && statusUpdatesOff;
    const tvPlayMode = String(props.values.TV_PLAY_MODE ?? "episode").trim().toLowerCase();
    const lookaheadRangeLocked = field.key === "EPISODES_LOOKAHEAD" && tvPlayMode !== "episode";
    const rowMuted = projectionFieldLocked || lookaheadRangeLocked;
    return (
      <div key={field.key} className={rowMuted ? "opacity-50" : undefined}>
        <label className="block text-sm font-semibold text-white font-headline mb-1">{field.label}</label>
        {!(lookaheadRangeLocked && field.key === "EPISODES_LOOKAHEAD") &&
          (field.key === "STARTUP_SYNC_MODE" ? (
            <StartupSyncModeDescription spacing="wizard" />
          ) : field.key === "PLACEHOLDER_STATUS_UPDATES" ? (
            <PlaceholderStatusUpdatesDescription spacing="wizard" />
          ) : field.key === "ENABLE_COMING_SOON_COUNTDOWN" ? (
            <ComingSoonCountdownDescription spacing="wizard" />
          ) : field.description ? (
            <p className="ui-field-description mb-2 leading-relaxed">{field.description}</p>
          ) : null)}
        {field.key === "FULL_SYNC_INTERVAL_HOURS" ? (
          <p className="ui-field-description ui-field-description-accent3 mb-2 mt-1 leading-relaxed">
            If you have Startup ARR sync mode set to OFF, then a scheduled sync is recommended.
          </p>
        ) : null}
        {field.type === "bool" ? (
          <label className="flex items-center gap-3 cursor-pointer select-none w-fit">
            <div className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${Boolean(props.values[field.key]) ? "" : "bg-[#252e3a]"}`}
              style={Boolean(props.values[field.key]) ? { backgroundColor: accent.hex } : undefined}
              onClick={() => props.onChange(field.key, !Boolean(props.values[field.key]))}>
              <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${Boolean(props.values[field.key]) ? "translate-x-5" : "translate-x-0"}`} />
            </div>
            <span className="text-sm text-slate-300">{Boolean(props.values[field.key]) ? "Enabled" : "Disabled"}</span>
          </label>
        ) : field.type === "choice" && field.options?.length ? (
          <select
            className={`w-full bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${focus} ${projectionFieldLocked ? "cursor-not-allowed" : ""}`}
            disabled={projectionFieldLocked}
            value={(() => {
              const raw = String(props.values[field.key] ?? field.options[0]?.value ?? "");
              if (field.key === "PLACEHOLDER_STATUS_PROJECTION_MODE" && raw.toLowerCase() === "off") return "summary";
              return raw;
            })()}
            onChange={(e) => props.onChange(field.key, e.target.value)}
          >
            {field.options.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        ) : (
          <div className="flex gap-2">
            <input
              className={`flex-1 min-w-0 bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${focus} ${lookaheadRangeLocked ? "cursor-not-allowed" : ""}`}
              type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
              disabled={lookaheadRangeLocked}
              value={String(props.values[field.key] ?? "")}
              placeholder={field.secret && field.has_saved_value ? "Saved value retained unless overwritten" : `Enter ${field.label.toLowerCase()}...`}
              onChange={(e) => props.onChange(field.key, e.target.value)}
            />
            {testTarget && (
              <button type="button" onClick={() => runTest(field)}
                className="flex items-center gap-1.5 px-3 py-2 bg-[#252e3a] hover:bg-[#30353b] border border-[#424753]/40 rounded-lg text-xs text-slate-300 font-headline uppercase tracking-wider transition-colors whitespace-nowrap shrink-0">
                <span className="material-symbols-outlined" style={{ fontSize: 14 }}>wifi</span>
                Test
              </button>
            )}
          </div>
        )}
        {testTarget ? (
          <div
            className={`mt-2 flex min-h-[2.25rem] items-start gap-1.5 text-xs ${
              test && test.message !== "Testing..." ? (test.ok ? "text-green-400" : "text-red-400") : "text-slate-400"
            }`}
            aria-live="polite"
          >
            {test && test.message === "Testing..." ? (
              <>
                <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 14 }}>progress_activity</span>
                <span>Testing…</span>
              </>
            ) : test ? (
              <>
                <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
                <span className="leading-snug">{test.message}</span>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <>
      <div
        className={`brand-theme-scope theme-dark layout-${props.brand}-dark min-h-screen flex flex-col relative overflow-x-hidden font-brand-body text-slate-200`}
        style={wizardShellStyle}
      >
        <div className="relative z-10 flex flex-1 min-h-0 w-full flex-col items-center pb-5 sm:pb-8">
          <OnboardingWizardHeroBanner footerBlendHex={wizardSemantic.chromePage} />
          <div className="flex w-full max-w-3xl flex-1 min-h-0 flex-col px-3">
          <div className="flex flex-1 min-h-0 flex-col rounded-2xl border border-white/10 bg-[#121722]/93 backdrop-blur-md shadow-2xl overflow-hidden max-h-[min(900px,calc(100vh-7rem))]">
        <div className="px-6 sm:px-8 py-4 sm:py-5 border-b border-[#424753]/30 shrink-0">
          <div className="flex items-center gap-0">
            {WIZARD_STEPS.map((s, i) => {
              const done = i < props.stepIndex;
              const active = i === props.stepIndex;
              return (
                <div key={s.key} className="flex items-center flex-1 last:flex-none">
                  <div className={`flex flex-col items-center gap-1 min-w-max ${active ? "" : done ? "text-green-400" : "text-slate-600"}`} style={active ? { color: accent.icon } : undefined}>
                    <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold font-headline border-2 transition-colors ${active ? "text-white" : done ? "bg-green-600/20 border-green-500 text-green-400" : "bg-[#252e3a] border-[#424753]/40 text-slate-600"}`}
                      style={active ? { backgroundColor: accent.hex, borderColor: accent.hex } : undefined}>
                      {done ? <span className="material-symbols-outlined" style={{ fontSize: 14 }}>check</span> : i + 1}
                    </div>
                    <span className="text-[10px] font-headline uppercase tracking-wider">{s.name}</span>
                  </div>
                  {i < WIZARD_STEPS.length - 1 && (
                    <div className={`flex-1 h-0.5 mx-3 mb-4 rounded-full transition-colors ${done ? "bg-green-500" : "bg-[#252e3a]"}`} />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Fields */}
        <div ref={stepContentRef} className="px-6 sm:px-8 py-6 overflow-y-auto flex-1 min-h-0">
          {step.key === "paths" ? (
            <LibraryPathsForm
              fields={fields}
              values={props.values}
              brand={props.brand}
              themeMode={wizardUiTheme}
              accent={accent}
              layout="wizard"
              onValueChange={props.onChange}
            />
          ) : step.key === "media" ? (() => {
            const fieldByKey = new Map(fields.map((f) => [f.key, f]));

            return (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                  {ONBOARDING_MEDIA_CARDS.map((card) => {
                    const enabled = Boolean(props.values[card.enabledKey]);
                    const availableFields = card.keys
                      .map((key) => fieldByKey.get(key))
                      .filter(Boolean) as SettingsField[];
                    const urlField = availableFields.find((f) => URL_TEST_TARGET[f.key]);
                    const urlTest = urlField ? testResults[urlField.key] : undefined;
                    const vis = ONBOARDING_MEDIA_VISUAL[card.id];
                    const address = urlField ? String(props.values[urlField.key] ?? "").trim() : "";
                    const movieLib = card.id === "plex" ? String(props.values.PLEX_MOVIE_SECTION_ID ?? "").trim() : "";
                    const tvLib = card.id === "plex" ? String(props.values.PLEX_TV_SECTION_ID ?? "").trim() : "";
                    const mediaDetailsComplete = mediaCardConnectionDetailsComplete(card, props.values, allSettingsFieldsByKey);

                    return (
                      <div
                        key={card.id}
                        className={`group relative flex min-h-[260px] flex-col ${UI_INTEGRATION_CARD_SURFACE_CLASS} p-6 duration-200`}
                      >
                        <div className="flex h-[5.25rem] w-full shrink-0 items-center justify-center" aria-hidden>
                          {card.id === "plex" ? (
                            <div className="flex max-w-full items-center justify-center gap-2 px-2 sm:px-3">
                              <div
                                className={`${MEDIA_PLEX_PAIR_WELL_FRAME} h-16 w-fit shrink-0 ${MEDIA_PLEX_PAIR_LOGO_INSET}`}
                                style={vis.well}
                              >
                                <img src={plexIcon} alt="" decoding="async" className="h-8 w-auto max-h-8 shrink-0 object-contain" aria-hidden />
                              </div>
                              <span className="select-none text-xl font-extralight leading-none text-white/85" aria-hidden>
                                ×
                              </span>
                              <div
                                className={`${MEDIA_PLEX_PAIR_WELL_FRAME} h-16 w-16 shrink-0`}
                                style={vis.well}
                              >
                                <img src={tautulliIcon} alt="" decoding="async" className="h-8 w-8 shrink-0 object-contain" aria-hidden />
                              </div>
                            </div>
                          ) : (
                            <div
                              className="flex h-[5.25rem] w-[5.25rem] items-center justify-center rounded-2xl"
                              style={vis.well}
                            >
                              <img src={vis.iconSrc} alt="" decoding="async" className="h-10 w-10 object-contain" aria-hidden />
                            </div>
                          )}
                        </div>
                        <h4 className="mt-5 w-full text-center text-lg font-bold tracking-tight text-white font-headline">{card.title}</h4>

                        {!enabled ? (
                          <button
                            type="button"
                            aria-label={`Connect ${card.title}`}
                            onClick={() => {
                              mediaPanelOpenedViaAddRef.current = true;
                              const snap: Record<string, unknown> = {};
                              for (const k of card.keys) {
                                snap[k] = props.values[k];
                              }
                              mediaPanelSnapshotRef.current = snap;
                              props.onChange(card.enabledKey, true);
                              setMediaPanelTestPassed(false);
                              setMediaPanel(card.id);
                            }}
                            className="mt-6 w-full rounded-xl border border-white/20 bg-white/[0.04] py-2.5 text-sm font-semibold tracking-wide text-white/95 transition hover:border-white/35 hover:bg-white/[0.09] active:scale-[0.99]"
                          >
                            Connect
                          </button>
                        ) : (
                          <div className="mt-5 flex min-h-0 flex-1 flex-col text-left">
                            {card.id === "plex" && "note" in card && card.note ? (
                              <p className="ui-field-description-compact mb-2">{card.note}</p>
                            ) : null}
                            <dl className="space-y-1.5 rounded-xl border border-white/[0.06] bg-black/20 p-3 text-[11px] leading-snug">
                              <div className="flex min-w-0 gap-2">
                                <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Address</dt>
                                <dd className="truncate font-mono text-slate-200" title={address || undefined}>
                                  {address || "—"}
                                </dd>
                              </div>
                              {card.id === "plex" ? (
                                <>
                                  <div className="flex min-w-0 gap-2">
                                    <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Movie lib.</dt>
                                    <dd className="truncate font-mono text-slate-200" title={movieLib || undefined}>
                                      {movieLib || "—"}
                                    </dd>
                                  </div>
                                  <div className="flex min-w-0 gap-2">
                                    <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">TV lib.</dt>
                                    <dd className="truncate font-mono text-slate-200" title={tvLib || undefined}>
                                      {tvLib || "—"}
                                    </dd>
                                  </div>
                                </>
                              ) : null}
                            </dl>
                            <div className="mt-2 flex min-h-[2.5rem] flex-col justify-center text-xs">
                              {urlTest && !urlTest.ok ? (
                                <p className="text-red-400">{urlTest.message}</p>
                              ) : !mediaDetailsComplete ? (
                                <p className="ui-field-description">Add URL and credentials in Configure.</p>
                              ) : null}
                            </div>
                            <div className="mt-auto flex flex-col gap-2 pt-4">
                              <button
                                type="button"
                                onClick={() => {
                                  mediaPanelOpenedViaAddRef.current = false;
                                  const snap: Record<string, unknown> = {};
                                  for (const k of card.keys) {
                                    snap[k] = props.values[k];
                                  }
                                  mediaPanelSnapshotRef.current = snap;
                                  const uk = mediaCardConnectionKeys(card)?.urlKey;
                                  const ready = mediaCardConnectionDetailsComplete(card, props.values, allSettingsFieldsByKey);
                                  setMediaPanelTestPassed(Boolean(ready && uk && testResults[uk]?.ok));
                                  setMediaPanel(card.id);
                                }}
                                className="w-full rounded-xl border border-white/20 bg-white/[0.04] py-2.5 text-xs font-headline font-semibold uppercase tracking-wider text-slate-100 transition hover:border-white/30 hover:bg-white/[0.08]"
                              >
                                Configure
                              </button>
                              {mediaCardPlaybackWebhookConfig(card.id) ? (
                                <button
                                  type="button"
                                  onClick={() => {
                                    const cfg = mediaCardPlaybackWebhookConfig(card.id);
                                    if (!cfg) return;
                                    const instanceParam =
                                      String(props.values[cfg.instanceKeyField] ?? "").trim() || cfg.defaultKey;
                                    setPlaybackWebhookDialog({ serviceId: cfg.serviceId, instanceParam });
                                  }}
                                  className="w-full rounded-xl border border-white/10 bg-transparent py-2.5 text-xs font-headline font-semibold uppercase tracking-wider text-slate-400 transition hover:border-white/20 hover:text-slate-200"
                                >
                                  Webhook URL
                                </button>
                              ) : null}
                              <button
                                type="button"
                                onClick={() => {
                                  props.onChange(card.enabledKey, false);
                                  setMediaPanel((p) => (p === card.id ? null : p));
                                }}
                                className="text-center text-[11px] font-medium text-slate-500 underline-offset-2 transition hover:text-red-300 hover:underline"
                              >
                                Remove connection
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
              </div>
            );
          })() : step.key === "arr" ? (
            <div className="space-y-6">
              <ArrInstancesEditor
                layout="slots"
                values={props.values}
                onValueChange={props.onChange}
                accent={accent}
                onPrimaryTestStatusChange={(arrType, ok) => {
                  setArrPrimaryTestStatus((prev) => ({ ...prev, [arrType]: ok }));
                }}
                onSecondaryTestStatusChange={(arrType, ok) => {
                  setArrSecondaryTestStatus((prev) => ({ ...prev, [arrType]: ok }));
                }}
              />
              <div className={`${UI_SECTION_FRAME_CLASS} p-4 space-y-4`}>
                <div className="text-center">
                  <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Placeholder Search Behavior</h3>
                  <p className="mx-auto max-w-2xl text-xs text-slate-400">Choose which ARR instance to search when a placeholder is played.</p>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-5">
                  <div className="min-w-0">
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                    <select
                      className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, wizardUiTheme)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                      value={canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "primary") : "na"}
                      onChange={(e) => props.onChange("MOVIE_PLACEHOLDER_SEARCH_MODE", e.target.value)}
                      disabled={!canUseRadarrSecondaryBehavior}
                    >
                      {canUseRadarrSecondaryBehavior ? (
                        <>
                          <option value="primary">Primary instance</option>
                          <option value="secondary">Secondary instance</option>
                          <option value="both">Both instances</option>
                        </>
                      ) : (
                        <option value="na">Not applicable, no second instance set up.</option>
                      )}
                    </select>
                  </div>
                  <div className="min-w-0">
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                    <select
                      className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, wizardUiTheme)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                      value={canUseSonarrSecondaryBehavior ? String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "primary") : "na"}
                      onChange={(e) => props.onChange("TV_PLACEHOLDER_SEARCH_MODE", e.target.value)}
                      disabled={!canUseSonarrSecondaryBehavior}
                    >
                      {canUseSonarrSecondaryBehavior ? (
                        <>
                          <option value="primary">Primary instance</option>
                          <option value="secondary">Secondary instance</option>
                          <option value="both">Both instances</option>
                        </>
                      ) : (
                        <option value="na">Not applicable, no second instance set up.</option>
                      )}
                    </select>
                  </div>
                </div>
              </div>
              <div className={`${UI_SECTION_FRAME_CLASS} p-4 space-y-4`}>
                <div className="text-center">
                  <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Real-File Search Behavior</h3>
                  <p className="mx-auto max-w-2xl text-xs text-slate-400">When a real media file is played, choose how Placeholdarr routes ARR searches.</p>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-5">
                  <div className="min-w-0">
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                    <select
                      className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, wizardUiTheme)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                      value={canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match") : "na"}
                      onChange={(e) => props.onChange("MOVIE_PLAYBACK_INSTANCE_MODE", e.target.value)}
                      disabled={!canUseRadarrSecondaryBehavior}
                    >
                      {canUseRadarrSecondaryBehavior ? (
                        <>
                          <option value="match">Match by library path</option>
                          <option value="primary">Primary instance</option>
                          <option value="secondary">Secondary instance</option>
                          <option value="both">Both instances</option>
                        </>
                      ) : (
                        <option value="na">Not applicable, no second instance set up.</option>
                      )}
                    </select>
                  </div>
                  <div className="min-w-0">
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                    <select
                      className={`w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, wizardUiTheme)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
                      value={canUseSonarrSecondaryBehavior ? String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match") : "na"}
                      onChange={(e) => props.onChange("TV_PLAYBACK_INSTANCE_MODE", e.target.value)}
                      disabled={!canUseSonarrSecondaryBehavior}
                    >
                      {canUseSonarrSecondaryBehavior ? (
                        <>
                          <option value="match">Match by library path</option>
                          <option value="primary">Primary instance</option>
                          <option value="secondary">Secondary instance</option>
                          <option value="both">Both instances</option>
                        </>
                      ) : (
                        <option value="na">Not applicable, no second instance set up.</option>
                      )}
                    </select>
                  </div>
                </div>
                <div className="border-t border-[#424753]/20 pt-4">
                  <div className="mx-auto flex max-w-lg flex-col items-center gap-4 text-center">
                    <div>
                      <div className="text-xs font-semibold text-slate-300">Fallback search</div>
                      <div className="ui-field-description mt-1">
                        {fallbackUnnecessaryBecauseAllBoth ? (
                          "Fallback is not needed because every unlocked search behavior already searches both instances."
                        ) : canUseAnySecondaryBehavior ? (
                          <div className="mx-auto w-full max-w-md text-left">
                            <p>When enabled, the non-selected source is searched automatically if:</p>
                            <ul className="mt-2 list-disc space-y-1.5 pl-4">
                              <li>The selected source doesn&apos;t have the content added (immediate fallback search), or</li>
                              <li>
                                The content isn&apos;t imported before the fallback timeout (e.g. content not found, indexer/download errors, etc.)
                              </li>
                            </ul>
                          </div>
                        ) : (
                          "Not applicable, no second instance set up."
                        )}
                      </div>
                    </div>
                    <label className="flex cursor-pointer select-none items-center justify-center gap-3">
                      <div
                        className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth ? (Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? "" : "bg-[#252e3a]") : "bg-[#1a1f27] opacity-60 cursor-not-allowed"}`}
                        style={canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? { backgroundColor: accent.hex } : undefined}
                        onClick={() => canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && props.onChange("ENABLE_PLAYBACK_FALLBACK_SEARCH", !Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH))}
                      >
                        <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? "translate-x-5" : "translate-x-0"}`} />
                      </div>
                      <span className="text-sm text-slate-300">
                        {fallbackUnnecessaryBecauseAllBoth
                          ? "Not needed"
                          : canUseAnySecondaryBehavior
                          ? (Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? "Enabled" : "Disabled")
                          : "Not applicable, no second instance set up."}
                      </span>
                    </label>
                    <div className="text-center">
                      <label className="block text-xs font-semibold text-slate-300 mb-1.5">Fallback timeout (minutes)</label>
                      {canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? (
                        <input
                          className={`mx-auto mt-0.5 block w-[4.25rem] bg-[#0b111b] border border-[#424753]/40 rounded-lg px-2 py-2 text-center text-sm tabular-nums tracking-tight text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, wizardUiTheme)}`}
                          type="text"
                          inputMode="numeric"
                          maxLength={3}
                          autoComplete="off"
                          value={(() => {
                            const raw = props.values.PLAYBACK_FALLBACK_TIMEOUT_MINUTES;
                            const digits = String(raw ?? "").replace(/\D/g, "").slice(0, 3);
                            if (digits.length > 0) return digits;
                            return raw === undefined || raw === null ? "30" : "";
                          })()}
                          onChange={(e) => {
                            const d = e.target.value.replace(/\D/g, "").slice(0, 3);
                            props.onChange("PLAYBACK_FALLBACK_TIMEOUT_MINUTES", d);
                          }}
                        />
                      ) : fallbackUnnecessaryBecauseAllBoth ? (
                        <input
                          className="mx-auto mt-0.5 block w-full max-w-md bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                          type="text"
                          value="Not needed because all unlocked behaviors already search both instances."
                          disabled
                        />
                      ) : canUseAnySecondaryBehavior ? (
                        <input
                          className="mx-auto mt-0.5 block w-full max-w-md bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                          type="text"
                          value="Enable fallback search."
                          disabled
                        />
                      ) : (
                        <input
                          className="mx-auto mt-0.5 block w-full max-w-md bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                          type="text"
                          value="Not applicable, no second instance set up."
                          disabled
                        />
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          ) : !fields.length ? (
            <div className="text-center text-slate-500 text-sm py-8">No fields for this step.</div>
          ) : step.key === "behavior" ? (
            <div className="space-y-6">
              {BEHAVIOR_WIZARD_SECTIONS.map((sectionName) => {
                const secFields = fields.filter((f) => f.section === sectionName);
                if (!secFields.length) return null;
                const fieldsBlock = <div className="space-y-5">{secFields.map((field) => wizardFieldRow(field))}</div>;
                const surfaceClass = WIZARD_ONBOARDING_SECTION_SURFACE_CLASS;
                return (
                  <div key={sectionName}>
                    <h2 className={ONBOARDING_SECTION_TITLE_CLASS}>{sectionName}</h2>
                    {sectionName === "Lookahead" ? (
                      <div className={surfaceClass}>
                        <LookaheadSectionIntro variant="onboarding" embedded />
                        <div className="mt-4 border-t border-[#424753]/25 pt-4">{fieldsBlock}</div>
                      </div>
                    ) : sectionName === "Status Updates" ? (
                      <div className={surfaceClass}>
                        <StatusUpdatesSectionIntro variant="onboarding" embedded />
                        <div className="mt-4 border-t border-[#424753]/25 pt-4">{fieldsBlock}</div>
                      </div>
                    ) : (
                      <div className={surfaceClass}>{fieldsBlock}</div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="space-y-5">{fields.map((field) => wizardFieldRow(field))}</div>
          )}
        </div>

        {/* Actions */}
        <div className="px-8 py-5 border-t border-[#424753]/30 flex items-center justify-between gap-4">
          <button
            type="button"
            disabled={props.stepIndex <= 0}
            onClick={() => props.onBack()}
            className="px-4 py-2 rounded-lg text-xs font-headline uppercase tracking-wider border border-[#424753]/50 text-slate-300 hover:bg-[#252e3a] hover:border-[#424753]/80 transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent"
          >
            Back
          </button>
          <div className="flex items-center gap-3">
            {props.hasUnsavedChanges && <span className="text-xs text-yellow-400 font-headline uppercase tracking-wider">Unsaved changes</span>}
            {props.stepIndex < WIZARD_STEPS.length - 1 ? (
              <button
                type="button"
                disabled={!canProceed || stepSaving}
                onClick={async () => {
                  setStepError(null);

                  const stepKeys = keys || [];
                  if (!stepKeys.length) {
                    props.onNext();
                    return;
                  }

                  // Build partial payload for this step and save it
                  const partial = buildStepPartialValues(stepKeys);
                  try {
                    setStepSaving(true);
                    const result = await saveSettings(partial, true, {
                      source: "onboarding",
                      stepKey: step.key,
                      stepName: step.name,
                    });
                    if (!result.ok) {
                      const first = Object.entries(result.errors || {})[0];
                      setStepError(first ? `${first[0]}: ${first[1]}` : "Unable to save step settings");
                      setStepSaving(false);
                      return;
                    }
                    // Inform parent to merge baseline and refresh payload
                    await props.onPartialSave?.(result, partial);
                    setStepSaving(false);
                    props.onNext();
                  } catch (err) {
                    setStepSaving(false);
                    setStepError(err instanceof Error ? err.message : String(err));
                  }
                }}
                className="flex items-center gap-2 px-5 py-2 text-white rounded-lg text-xs font-headline uppercase tracking-wider transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                style={{ backgroundColor: accent.hex }}
                >
                {keys.length ? (stepSaving ? "Saving..." : "Save & Continue") : "Continue"}
                <span className="material-symbols-outlined" style={{ fontSize: 14 }}>arrow_forward</span>
              </button>
            ) : (
              <button type="button" onClick={() => props.onSave()}
                className="flex items-center gap-2 px-5 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg text-xs font-headline uppercase tracking-wider transition-colors">
                <span className="material-symbols-outlined" style={{ fontSize: 14 }}>check_circle</span>
                Save &amp; Finish
              </button>
            )}
          </div>
        </div>
        {stepError ? (
          <div className="px-8 pb-4 text-sm text-red-400">{stepError}</div>
        ) : null}
          </div>
        </div>
      </div>
      </div>
      {step.key === "media" && mediaPanel ? (() => {
        const card = ONBOARDING_MEDIA_CARDS.find((c) => c.id === mediaPanel);
        if (!card) return null;
        const fieldByKey = new Map(fields.map((f) => [f.key, f]));
        const focus = getBrandFocusClass(props.brand, wizardUiTheme);
        const availableFields = card.keys
          .map((key) => fieldByKey.get(key))
          .filter(Boolean) as SettingsField[];
        if (!Boolean(props.values[card.enabledKey])) {
          return null;
        }
        const urlFieldForFooter = availableFields.find((f) => URL_TEST_TARGET[f.key]);
        const detailsComplete = mediaCardConnectionDetailsComplete(card, props.values, allSettingsFieldsByKey);
        const addMediaLabel = `Add ${card.title}`;
        const urlConnTest = urlFieldForFooter ? testResults[urlFieldForFooter.key] : undefined;
        const urlTestFailed =
          Boolean(urlConnTest && !mediaFooterTestBusy && urlConnTest.message !== "Testing..." && !urlConnTest.ok);
        const urlTestSucceeded =
          Boolean(urlConnTest && !mediaFooterTestBusy && urlConnTest.message !== "Testing..." && urlConnTest.ok);
        return (
          <div
            className={`fixed inset-0 z-[70] flex items-center justify-center overflow-y-auto p-4 sm:p-6 brand-theme-scope theme-dark layout-${props.brand}-dark`}
            style={semanticTokensToCssVars(wizardSemantic) as CSSProperties}
          >
            <button
              type="button"
              aria-label="Close panel"
              className="absolute inset-0 bg-black/70 backdrop-blur-sm"
              onClick={handleMediaPanelCancel}
            />
            <div
              role="dialog"
              aria-modal="true"
              aria-label={`${card.title} server`}
              className="relative z-10 my-auto flex w-full max-w-lg max-h-[min(90vh,720px)] flex-col overflow-hidden rounded-2xl border border-[#424753]/50 bg-[#171c22] shadow-2xl"
            >
              <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-[#424753]/40 shrink-0">
                <div>
                  <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500">Media server</div>
                  <h2 className="text-lg font-headline font-bold text-white mt-0.5" style={{ color: accent.text }}>
                    {card.title}
                  </h2>
                </div>
                <button
                  type="button"
                  onClick={handleMediaPanelCancel}
                  className="p-2 rounded-lg text-slate-400 hover:text-white hover:bg-[#252e3a]/80"
                  aria-label="Close"
                >
                  <span className="material-symbols-outlined" style={{ fontSize: 22 }}>close</span>
                </button>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto p-5 space-y-4">
                {availableFields.map((field) => {
                  if (HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key)) return null;
                  const value = props.values[field.key];
                  return (
                    <div key={field.key}>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">{field.label}</label>
                      {getPlexLibraryIdNote(field.key) ? <p className="text-[11px] text-slate-500 mb-1.5">{getPlexLibraryIdNote(field.key)}</p> : null}
                      {field.type === "bool" ? (
                        <label className="flex items-center gap-3 cursor-pointer select-none w-fit">
                          <div
                            className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${Boolean(value) ? "" : "bg-[#252e3a]"}`}
                            style={Boolean(value) ? { backgroundColor: accent.hex } : undefined}
                            onClick={() => handleMediaPanelFieldChange(field.key, !Boolean(value))}
                          >
                            <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${Boolean(value) ? "translate-x-5" : "translate-x-0"}`} />
                          </div>
                          <span className="text-xs text-slate-300">{Boolean(value) ? "Enabled" : "Disabled"}</span>
                        </label>
                      ) : (
                        <div className="flex gap-2">
                          <input
                            className={`flex-1 min-w-0 bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${focus}`}
                            type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
                            value={String(value ?? "")}
                            placeholder={field.secret && field.has_saved_value ? "Saved value retained unless overwritten" : `Enter ${field.label.toLowerCase()}...`}
                            onChange={(e) => handleMediaPanelFieldChange(field.key, e.target.value)}
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="shrink-0 border-t border-[#424753]/40 bg-[#141a24]">
                <div
                  className="flex min-h-[2.75rem] items-center gap-2 px-4 pt-3 text-xs text-red-400"
                  aria-live="polite"
                >
                  {urlTestFailed && urlConnTest ? (
                    <>
                      <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>error</span>
                      <span className="line-clamp-2 leading-snug">{urlConnTest.message}</span>
                    </>
                  ) : null}
                </div>
                <div className="flex flex-wrap items-stretch justify-between gap-2 px-4 pb-4 pt-1">
                <button
                  type="button"
                  onClick={handleMediaPanelCancel}
                  className="min-w-[5.5rem] flex-1 sm:flex-none px-4 py-2.5 rounded-lg text-xs font-headline uppercase tracking-wider border border-[#424753]/55 text-slate-300 hover:bg-[#252e3a]/80 hover:border-[#424753]/80 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={!detailsComplete || !urlFieldForFooter || mediaFooterTestBusy}
                  title={
                    urlTestSucceeded && urlConnTest
                      ? urlConnTest.message
                      : "Run connection test"
                  }
                  onClick={() => {
                    if (!urlFieldForFooter) return;
                    void (async () => {
                      setMediaFooterTestBusy(true);
                      const result = await runTest(urlFieldForFooter);
                      setMediaFooterTestBusy(false);
                      setMediaPanelTestPassed(Boolean(result?.ok));
                    })();
                  }}
                  className={`flex h-11 w-[9rem] shrink-0 basis-[9rem] items-center justify-center gap-1.5 rounded-lg px-2 text-xs font-headline uppercase tracking-wider font-semibold border transition-colors duration-200 ease-out disabled:cursor-not-allowed disabled:opacity-40 ${
                    urlTestSucceeded
                      ? "border-emerald-500/70 bg-emerald-600/15 text-emerald-300 hover:border-emerald-400/90 hover:bg-emerald-600/25"
                      : "border-amber-400/80 bg-amber-500 text-slate-900 hover:bg-amber-400 disabled:hover:bg-amber-500"
                  }`}
                >
                  {mediaFooterTestBusy ? (
                    <>
                      <span className="material-symbols-outlined shrink-0 animate-spin" style={{ fontSize: 18 }}>
                        progress_activity
                      </span>
                      <span>Testing…</span>
                    </>
                  ) : urlTestSucceeded ? (
                    <span className="material-symbols-outlined shrink-0" style={{ fontSize: 22 }}>
                      check_circle
                    </span>
                  ) : (
                    <>
                      <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>wifi</span>
                      <span>Test</span>
                    </>
                  )}
                </button>
                <button
                  type="button"
                  disabled={!mediaPanelTestPassed || mediaFooterTestBusy}
                  onClick={() => {
                    const panelId = mediaPanel;
                    const card = panelId ? ONBOARDING_MEDIA_CARDS.find((c) => c.id === panelId) : undefined;
                    const conn = card ? mediaCardConnectionKeys(card) : null;
                    const cfg = panelId ? mediaCardPlaybackWebhookConfig(panelId) : null;
                    const instanceParam = cfg
                      ? String(props.values[cfg.instanceKeyField] ?? "").trim() || cfg.defaultKey
                      : "";
                    const prevUrl = conn ? String(mediaPanelSnapshotRef.current[conn.urlKey] ?? "").trim() : "";
                    const newUrl = conn ? String(props.values[conn.urlKey] ?? "").trim() : "";
                    const urlChanged =
                      normalizeArrInstanceUrlForDedupe(prevUrl) !== normalizeArrInstanceUrlForDedupe(newUrl);
                    setMediaPanel(null);
                    setMediaPanelTestPassed(false);
                    if (cfg && urlChanged) {
                      setPlaybackWebhookDialog({ serviceId: cfg.serviceId, instanceParam });
                    }
                  }}
                  aria-label={addMediaLabel}
                  className="btn-brand-tertiary min-w-[6.5rem] flex-1 sm:flex-none px-4 py-2.5 rounded-lg text-xs font-headline uppercase tracking-wider font-semibold border transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {addMediaLabel}
                </button>
                </div>
              </div>
            </div>
          </div>
        );
      })() : null}
      {playbackWebhookDialog ? (
        <PlaybackWebhookSetupModal
          dialog={playbackWebhookDialog}
          onClose={() => setPlaybackWebhookDialog(null)}
          accent={accent}
        />
      ) : null}
    </>
  );
}
function getTabFromPath(pathname: string): DashboardTab {
  if (pathname === "/setup" || pathname.startsWith("/setup/")) return "setup";
  if (pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/calendar")) return "calendar";
  if (pathname.startsWith("/errors")) return "errors";
  if (pathname.startsWith("/logs")) return "logs";
  if (pathname.startsWith("/settings")) return "settings";
  return "activity";
}

function getCurrentMonthToken() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function flattenVisibleCalendarItems(payload: CalendarResponse | null, filters: CalendarFilters) {
  if (!payload) return [] as CalendarDay["items"];

  return payload.weeks.flatMap((week) =>
    week.flatMap((day) => day.items.filter((item) => isCalendarItemVisible(item, filters))),
  );
}

function findCalendarItem(payload: CalendarResponse | null, itemId: string | null) {
  if (!payload || !itemId) return null;

  for (const week of payload.weeks) {
    for (const day of week) {
      const item = day.items.find((entry) => entry.id === itemId);
      if (item) return item;
    }
  }

  return null;
}

function isCalendarItemVisible(item: CalendarDay["items"][number], filters: CalendarFilters) {
  const mediaType = item.media_type || "episode";
  if (!filters.mediaTypes[mediaType]) return false;
  if (mediaType === "movie") {
    const releaseType = item.release_type || "inCinemas";
    return !!filters.releaseTypes[releaseType];
  }
  return true;
}

/** Compare only keys the server persists — ignores client-only wizard keys (e.g. WIZARD_*). */
function settingsValuesDirty(fieldValues: FieldValueMap, baselineValues: FieldValueMap, payload: SettingsPayload | null): boolean {
  if (!payload) {
    return !deepEqualValues(fieldValues, baselineValues);
  }
  const keys = [...new Set(payload.sections.flatMap((section) => section.fields.map((f) => f.key)))];
  const pick = (src: FieldValueMap): FieldValueMap => {
    const out: FieldValueMap = {};
    for (const k of keys) {
      out[k] = src[k];
    }
    return out;
  };
  return !deepEqualValues(pick(fieldValues), pick(baselineValues));
}

function deepEqualValues(a: FieldValueMap, b: FieldValueMap) {
  const aKeys = Object.keys(a).sort();
  const bKeys = Object.keys(b).sort();
  if (aKeys.length !== bKeys.length) return false;
  for (let i = 0; i < aKeys.length; i += 1) {
    if (aKeys[i] !== bKeys[i]) return false;
    const av = normalizeComparable(a[aKeys[i]]);
    const bv = normalizeComparable(b[bKeys[i]]);
    if (av !== bv) return false;
  }
  return true;
}

function normalizeComparable(value: unknown) {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function timeAgo(iso: string | null) {
  if (!iso) return "--";
  const d = new Date(iso);
  const now = new Date();
  const s = Math.floor((now.getTime() - d.getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function formatCalendarHeroDateParts(iso: string): { month: string; day: string } | null {
  if (!iso) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const [y, m, d] = iso.split("-").map(Number);
    const date = new Date(y, m - 1, d);
    return {
      month: date.toLocaleDateString(undefined, { month: "short" }).toUpperCase(),
      day: String(d),
    };
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return {
    month: date.toLocaleDateString(undefined, { month: "short" }).toUpperCase(),
    day: String(date.getDate()),
  };
}

/** Spotlight card: date lives on hero; omit status / reason / redundant counts. */
function formatCalendarSpotlightMeta(item: CalendarDay["items"][number]): Array<{ label: string; value: string }> {
  const bits: Array<{ label: string; value: string }> = [];
  if (item.media_type === "movie" && item.release_type_label) {
    bits.push({
      label: "Release",
      value: item.release_type_label,
    });
  }
  if (typeof item.days_until === "number") {
    const relative =
      item.days_until === 0 ? "Today" : item.days_until === 1 ? "1 day" : `${item.days_until} days`;
    bits.push({ label: "Countdown", value: relative });
  }
  return bits;
}

type CalendarSpotlightEpisodeRow = SeriesEpisodeDetail & { season_number: number };

function collectEpisodesForCalendarDay(
  series: SeriesDetailResponse | null,
  episodeIds: number[],
): CalendarSpotlightEpisodeRow[] {
  if (!series || !episodeIds.length) return [];
  const want = new Set(episodeIds);
  const out: CalendarSpotlightEpisodeRow[] = [];
  for (const season of series.seasons) {
    for (const ep of season.episodes) {
      if (want.has(ep.id)) {
        out.push({ ...ep, season_number: season.season_number });
      }
    }
  }
  out.sort(
    (a, b) => a.season_number - b.season_number || a.episode_number - b.episode_number,
  );
  return out;
}

function formatCalendarItemMeta(item: CalendarDay["items"][number]) {
  const bits: Array<{ label: string; value: string }> = [];
  // Keep day-cell cards intentionally lean: movie cards show only release type.
  if (item.media_type === "movie" && item.release_type_label) {
    bits.push({ label: "Release", value: item.release_type_label });
  }
  return bits;
}

async function loadStats(stopped: boolean, setStats: (s: StatsResponse) => void) {
  const payload = await getStats();
  if (!stopped) setStats(payload);
}

function fieldsForWizardStep(stepKey: (typeof WIZARD_STEPS)[number]["key"], sections: { name: string; fields: SettingsField[] }[]) {
  const map: Record<string, string[]> = {};
  sections.forEach((section) => {
    map[section.name] = section.fields.map((f) => f.key);
  });

  const integrations = new Set(map.Integrations || []);
  const mediaIntegrations = new Set([...(map["Media Integrations"] || []), ...[...integrations].filter((k) => k.startsWith("PLEX") || k.startsWith("JELLYFIN") || k.startsWith("EMBY") || k === "ENABLE_PLEX" || k === "ENABLE_JELLYFIN" || k === "ENABLE_EMBY")]);
  const arrIntegrations = new Set([
    ...(map["ARR Integrations"] || []),
    ...[...integrations].filter(
      (k) =>
        (k.startsWith("RADARR") || k.startsWith("SONARR") || k === "ARR_INSTANCES_JSON") &&
        !HIDDEN_PLAYBACK_INTERNAL_KEYS.has(k),
    ),
  ]);
  const paths = map.Paths || [];
  const librarySync = map["Library sync"] || [];
  const calendar = map.Calendar || [];
  const lookahead = map.Lookahead || [];
  const statusUpdates = map["Status Updates"] || [];
  const advanced = map.Advanced || [];
  const arrBehaviorFromArrIntegrations = [...arrIntegrations].filter((k) => ARR_BEHAVIOR_KEYS.has(k));
  const arrBehaviorFromLookahead = lookahead.filter((k) => ARR_BEHAVIOR_KEYS.has(k));
  const arrBehavior = arrBehaviorFromArrIntegrations.length ? arrBehaviorFromArrIntegrations : arrBehaviorFromLookahead;
  const lookaheadNonArr = lookahead.filter((k) => !ARR_BEHAVIOR_KEYS.has(k));

  if (stepKey === "paths") return [...paths];
  if (stepKey === "arr") {
    return [
      ...[...arrIntegrations].filter((k) => k.startsWith("RADARR") || k.startsWith("SONARR") || k === "ARR_INSTANCES_JSON"),
      ...arrBehavior,
    ];
  }
  if (stepKey === "media") {
    return [...mediaIntegrations];
  }
  return [
    ...librarySync,
    ...calendar,
    ...lookaheadNonArr,
    ...statusUpdates,
    ...advanced.filter((k) => !SETTINGS_UI_HIDDEN_FIELD_KEYS.has(k)),
  ];
}
