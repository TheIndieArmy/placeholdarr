import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ARR_WEBHOOK_SERVICES } from "./webhookConfig";
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
import type {
  ActivityRow,
  CalendarDay,
  CalendarResponse,
  DashboardTab,
  ErrorRow,
  LibraryItem,
  MovieDetailResponse,
  SeriesDetailResponse,
  SettingsField,
  SettingsPayload,
  StatsResponse,
} from "./types/api";

const REFRESH_MS = 5000;
const SETTINGS_SECTION_ORDER = ["Media Integrations", "ARR Integrations", "Paths", "Library sync", "Calendar", "Playback", "Advanced"];
const BEHAVIOR_WIZARD_SECTIONS = ["ARR Integrations", "Library sync", "Calendar", "Playback", "Advanced"] as const;
const WIZARD_STEPS = [
  { key: "paths", name: "Paths" },
  { key: "media", name: "Media Servers" },
  { key: "arr", name: "ARR Services" },
  { key: "webhooks", name: "Webhook Setup" },
  { key: "behavior", name: "Behavior" },
] as const;

const PATH_LIBRARY_ROOT_KEY = "LIBRARY_ROOT";
const PATH_PROFILE_KEYS = [] as const;
const PATH_PER_LIBRARY_OVERRIDE_KEYS = [] as const;

const HIDDEN_PLAYBACK_INTERNAL_KEYS = new Set<string>([]);

const ARR_CONFIGURATION_KEYS = new Set<string>([
  "RADARR_URL",
  "RADARR_API_KEY",
  "RADARR_4K_URL",
  "RADARR_4K_API_KEY",
  "SONARR_URL",
  "SONARR_API_KEY",
  "SONARR_4K_URL",
  "SONARR_4K_API_KEY",
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

const WIZARD_STEP_GUIDANCE: Record<(typeof WIZARD_STEPS)[number]["key"], { title: string; detail: string }> = {
  paths: {
    title: "Start with Library Root",
    detail:
      "Set one base path. Placeholdarr will create and use `movies` and `tv` folders under that path for placeholders.",
  },
  media: {
    title: "Media server targets",
    detail:
      "Enable at least one media server you actually run. You can use Plex, Jellyfin, Emby, or a combination. Disabled services can stay blank.",
  },
  arr: {
    title: "ARR connections",
    detail:
      "Add up to 2 Radarr and 2 Sonarr instances, give each a label, and test connectivity.",
  },
  webhooks: {
    title: "Connect webhooks",
    detail:
      "Use the exact webhook URLs below for each ARR/media service. Instance keys are derived from your current labels.",
  },
  behavior: {
    title: "Sync, calendar, and playback",
    detail:
      "Tune refresh cadence, lookahead windows, and playback behavior. Keep dummy files under /config and choose which dummy path Coming Soon placeholders use.",
  },
};

const URL_TEST_TARGET: Record<string, { service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr"; credentialKey: string }> = {
  PLEX_URL: { service: "plex", credentialKey: "PLEX_TOKEN" },
  JELLYFIN_URL: { service: "jellyfin", credentialKey: "JELLYFIN_TOKEN" },
  EMBY_URL: { service: "emby", credentialKey: "EMBY_TOKEN" },
  RADARR_URL: { service: "radarr", credentialKey: "RADARR_API_KEY" },
  RADARR_4K_URL: { service: "radarr", credentialKey: "RADARR_4K_API_KEY" },
  SONARR_URL: { service: "sonarr", credentialKey: "SONARR_API_KEY" },
  SONARR_4K_URL: { service: "sonarr", credentialKey: "SONARR_4K_API_KEY" },
};

type LibraryFilter = "all" | "movie" | "series" | "placeholders" | "future" | "missing";

type FieldValueMap = Record<string, unknown>;

type CalendarFilters = {
  mediaTypes: Record<string, boolean>;
  releaseTypes: Record<string, boolean>;
};

type Brand = "placeholdarr" | "placeholdarr-neon" | "spectarr" | "phantarr" | "mirarr" | "elfhosted";
type ThemeMode = "light" | "dark";

type BrandAccent = {
  label: string;
  hex: string;
  text: string;
  icon: string;
  hoverHex: string;
};

const BRAND_OPTIONS: Array<{ value: Brand; label: string }> = [
  { value: "placeholdarr", label: "Placeholdarr" },
  { value: "placeholdarr-neon", label: "Placeholdarr Neon" },
  { value: "spectarr", label: "Spectarr" },
  { value: "phantarr", label: "Phantarr" },
  { value: "mirarr", label: "Mirarr" },
  { value: "elfhosted", label: "ElfHosted" },
];

const THEME_MODE_OPTIONS: Array<{ value: ThemeMode; label: string }> = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
];

const THEME_OPTIONS: Array<{ value: "standard" | "glassmorphism" | "minimalist-dark" | "studio-dark"; label: string }> = [
  { value: "standard", label: "Standard" },
  { value: "glassmorphism", label: "Glass" },
  { value: "minimalist-dark", label: "Minimalist" },
  { value: "studio-dark", label: "Studio" },
];

const BRAND_META: Record<Brand, { label: string; tagline: string }> = {
  placeholdarr: { label: "Placeholdarr", tagline: "Your library, complete - even before it's downloaded." },
  "placeholdarr-neon": { label: "Placeholdarr Neon", tagline: "Your library, complete - with a neon control room edge." },
  spectarr: { label: "Spectarr", tagline: "A specter in your library - present, but not yet real." },
  phantarr: { label: "Phantarr", tagline: "Phantom media, on demand. Real when you need it." },
  mirarr: { label: "Mirarr", tagline: "Your library, reflected in full - even what's not there yet." },
  elfhosted: { label: "Placeholdarr", tagline: "Your library, complete. No docker, no suffering." },
};

const BRAND_ACCENTS: Record<`${Brand}-${ThemeMode}`, BrandAccent> = {
  // Placeholdarr - Ghost Steel
  "placeholdarr-light": {
    label: "Placeholdarr",
    hex: "#7B9FD4",
    text: "#D8EAF7",
    icon: "#A8C3E5",
    hoverHex: "#4D7BB0",
  },
  "placeholdarr-dark": {
    label: "Placeholdarr",
    hex: "#7B9FD4",
    text: "#e4eeff",
    icon: "#cfe0ff",
    hoverHex: "#6889bb",
  },
  "placeholdarr-neon-light": {
    label: "Placeholdarr Neon",
    hex: "#b33771",
    text: "#ffe4f1",
    icon: "#ffc6e0",
    hoverHex: "#9b2f62",
  },
  "placeholdarr-neon-dark": {
    label: "Placeholdarr Neon",
    hex: "#b33771",
    text: "#ffd7e8",
    icon: "#ffb6d5",
    hoverHex: "#9b2f62",
  },
  // Spectarr - Slate Lavender
  "spectarr-light": {
    label: "Spectarr",
    hex: "#9B9BB4",
    text: "#EDEDF5",
    icon: "#BEBDD4",
    hoverHex: "#6E6E88",
  },
  "spectarr-dark": {
    label: "Spectarr",
    hex: "#9B9BB4",
    text: "#e6e6f0",
    icon: "#d4d4e8",
    hoverHex: "#7d7d96",
  },
  // Phantarr - Ghost Teal
  "phantarr-light": {
    label: "Phantarr",
    hex: "#5DAFB2",
    text: "#CBE9EA",
    icon: "#8FCBCD",
    hoverHex: "#3A8E91",
  },
  "phantarr-dark": {
    label: "Phantarr",
    hex: "#5DAFB2",
    text: "#daf7f7",
    icon: "#bdeff0",
    hoverHex: "#4a999d",
  },
  // Mirarr - Amber
  "mirarr-light": {
    label: "Mirarr",
    hex: "#C4893D",
    text: "#F2DFC0",
    icon: "#D4A96D",
    hoverHex: "#9A6A2A",
  },
  "mirarr-dark": {
    label: "Mirarr",
    hex: "#C4893D",
    text: "#f0ddb0",
    icon: "#e8c890",
    hoverHex: "#a87a2f",
  },
  // ElfHosted - Forest Green
  "elfhosted-light": {
    label: "ElfHosted",
    hex: "#3d5a22",
    text: "#e8f5d6",
    icon: "#50762e",
    hoverHex: "#2c4418",
  },
  "elfhosted-dark": {
    label: "ElfHosted",
    hex: "#50762e",
    text: "#c8e6a0",
    icon: "#6a9c3e",
    hoverHex: "#3d5a22",
  },
};

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

function BrandLogo(props: { brand: Brand; accentHex: string; className?: string }) {
  const stroke = "#ffffff";
  if (props.brand === "placeholdarr" || props.brand === "placeholdarr-neon" || props.brand === "elfhosted") {
    return (
      <svg className={props.className} viewBox="0 0 72 72" xmlns="http://www.w3.org/2000/svg" aria-hidden>
        <rect width="72" height="72" rx="16" fill={props.accentHex} />
        <polyline points="28,18 19,18 19,54 28,54" stroke={stroke} strokeWidth="5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        <polyline points="44,18 53,18 53,54 44,54" stroke={stroke} strokeWidth="5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (props.brand === "spectarr") {
    return (
      <svg className={props.className} viewBox="0 0 72 72" xmlns="http://www.w3.org/2000/svg" aria-hidden>
        <rect width="72" height="72" rx="16" fill={props.accentHex} />
        <path d="M36 15 A21 21 0 0 0 36 57 Z" fill="#fff" />
        <path d="M36 15 A21 21 0 0 1 36 57" stroke="#fff" strokeWidth="4" fill="none" strokeDasharray="4 5" strokeLinecap="round" />
        <circle cx="36" cy="36" r="4.5" fill={props.accentHex} />
        <circle cx="36" cy="36" r="2.5" fill="#fff" />
      </svg>
    );
  }
  if (props.brand === "phantarr") {
    return (
      <svg className={props.className} viewBox="0 0 72 72" xmlns="http://www.w3.org/2000/svg" aria-hidden>
        <rect width="72" height="72" rx="16" fill={props.accentHex} />
        <polygon points="23,17 55,36 23,55" stroke="#fff" strokeWidth="4.5" fill="none" strokeLinejoin="round" strokeLinecap="round" />
        <line x1="23" y1="17" x2="55" y2="36" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" />
        <line x1="23" y1="55" x2="55" y2="36" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" />
        <line x1="23" y1="17" x2="23" y2="55" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" strokeDasharray="5 5" />
      </svg>
    );
  }
  return (
    <svg className={props.className} viewBox="0 0 72 72" xmlns="http://www.w3.org/2000/svg" aria-hidden>
      <rect width="72" height="72" rx="16" fill={props.accentHex} />
      <polygon points="36,13 57,36 36,36 15,36" fill="#fff" />
      <polyline points="15,36 36,59 57,36" stroke="#fff" strokeWidth="4" fill="none" strokeDasharray="5 4.5" strokeLinecap="round" strokeLinejoin="round" />
      <line x1="13" y1="36" x2="59" y2="36" stroke={props.accentHex} strokeWidth="1.5" />
    </svg>
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

  const [theme, setTheme] = useState<"standard" | "glassmorphism" | "minimalist-dark" | "studio-dark">("studio-dark");
  const [brand, setBrand] = useState<Brand>("placeholdarr");
  const [themeMode, setThemeMode] = useState<ThemeMode>("dark");

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

  const [libraryFilter, setLibraryFilter] = useState<LibraryFilter>("all");
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
  const [activeSettingsSection, setActiveSettingsSection] = useState("Media Integrations");
  const [fieldValues, setFieldValues] = useState<FieldValueMap>({});
  const [baselineValues, setBaselineValues] = useState<FieldValueMap>({});
  const [settingsFeedback, setSettingsFeedback] = useState("");
  const [settingsFeedbackKind, setSettingsFeedbackKind] = useState<"" | "success" | "error">("");

  const [setupStatus, setSetupStatus] = useState<{ setup_complete: boolean } | null>(null);
  const [onboardingVisible, setOnboardingVisible] = useState(false);
  const [onboardingStepIndex, setOnboardingStepIndex] = useState(0);

  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const currentTab = getTabFromPath(location.pathname);
  const defaultLandingPath = setupStatus?.setup_complete === false ? "/settings" : "/activity";
  const brandAccent = getBrandAccent(brand, themeMode);
  const brandMeta = BRAND_META[brand];
  const hasUnsavedChanges = useMemo(() => !deepEqualValues(fieldValues, baselineValues), [fieldValues, baselineValues]);
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

    async function refresh() {
      try {
        await loadStats(stopped, setStats);

        if (currentTab === "activity") {
          const rows = await getActivity(100);
          if (!stopped) setActivity(rows || []);
          const placeholderRows = await getPlaceholderActivity(100);
          if (!stopped) setPlaceholderActivity(placeholderRows || []);
        } else if (currentTab === "library") {
          const payload = await getLibrary(400);
          if (!stopped) setLibrary(payload.items || []);
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
          const payload = await getLogs(logLevel, 500);
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
        }
      } catch (err) {
        if (!stopped) {
          setErrorMessage(err instanceof Error ? err.message : "Dashboard refresh failed");
          setLoading(false);
        }
      }
    }

    refresh();
    const timer = window.setInterval(refresh, REFRESH_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [calendarMonth, currentTab, logLevel]);

  useEffect(() => {
    if (library.length > 0) return;

    let stopped = false;
    getLibrary(400)
      .then((payload) => {
        if (!stopped) {
          setLibrary(payload.items || []);
        }
      })
      .catch(() => {
        // Ignore prefetch failures; the tab-specific loader will retry.
      });

    return () => {
      stopped = true;
    };
  }, [library.length]);

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
    if (location.pathname === "/" || location.pathname.startsWith("/activity")) {
      navigate("/settings", { replace: true });
    }
  }, [location.pathname, navigate, setupStatus]);

  const filteredLibrary = useMemo(() => {
    return library
      .filter((item) => {
      if (libraryFilter === "movie") return item.type === "movie";
      if (libraryFilter === "series") return item.type === "series";
      if (libraryFilter === "placeholders") return item.has_placeholder;
      if (libraryFilter === "future") return item.is_future;
      if (libraryFilter === "missing") return item.has_missing;
      return true;
      })
      .sort((left, right) => titleSortKey(left.title).localeCompare(titleSortKey(right.title)));
  }, [library, libraryFilter]);

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
      setActiveSettingsSection(sections[0]);
    }
  }

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
    if (!hasUnsavedChanges || currentTab !== "settings") {
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
    if (currentTab === "library") {
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
    if (currentTab !== "library") return;
    if (sessionStorage.getItem("libraryScrollRestorePending") !== "1") return;

    const rawTop = sessionStorage.getItem("libraryScrollTop");
    const savedTop = rawTop == null ? 0 : Number(rawTop);

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        setActiveScrollTop(savedTop);
      });
    });

    sessionStorage.removeItem("libraryScrollRestorePending");
  }, [currentTab, location.pathname, library.length]);

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
    if (loading) return <div className="empty">Loading dashboard data...</div>;

    if (location.pathname.startsWith("/library/") && (location.pathname.includes("/movie/") || location.pathname.includes("/series/"))) {
      return <DetailRoutePage brand={brand} themeMode={themeMode} scrollContainerRef={contentScrollRef} />;
    }

    const openLibraryWithFilter = (filter: LibraryFilter) => {
      setLibraryFilter(filter);
      navigate("/library");
    };

    if (currentTab === "activity") return <ActivityPanel rows={activity} placeholderRows={placeholderActivity} activityTab={activityTab} onActivityTabChange={setActivityTab} stats={stats} brand={brand} themeMode={themeMode} onOpenLibraryFilter={openLibraryWithFilter} />;

    if (currentTab === "library") {
      return (
        <LibraryPanel
          items={filteredLibrary}
          activeFilter={libraryFilter}
          onFilterChange={setLibraryFilter}
          onOpenDetail={(item) => openLibraryDetail({ type: item.type, item_id: item.item_id, title: item.title })}
          stats={stats}
          brand={brand}
          themeMode={themeMode}
        />
      );
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
          onSectionChange={setActiveSettingsSection}
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

  // Dynamically set body class based on theme mode
  useEffect(() => {
    document.body.className = themeMode === "light" ? "theme-studio-light" : "theme-glassmorphism";
  }, [themeMode]);

  // Studio Shell — Tailwind JSX matching the design mockups exactly
  if (theme === "studio-dark") {
    const isActive = (path: string) =>
      location.pathname === path || location.pathname.startsWith(`${path}/`);
    const isStudioGlass = themeMode !== "light";

    return (
      <div
        className={`theme-${themeMode} flex h-screen overflow-hidden font-body ${isStudioGlass ? "text-slate-100" : "bg-[#e3e3e5] text-slate-900"}`}
        style={isStudioGlass ? {
          backgroundImage: `radial-gradient(1200px 600px at 10% -20%, ${alphaColor(brandAccent.hex, 0.22)}, transparent 60%), radial-gradient(900px 500px at 85% 120%, ${alphaColor(brandAccent.hex, 0.12)}, transparent 55%), linear-gradient(160deg, #0b1320, #0a101a 55%, #0b1528)`,
        } : undefined}
      >
        {/* Sidebar */}
        <aside className={`hidden md:flex flex-col h-full py-6 w-64 z-20 flex-shrink-0 ${isStudioGlass ? "bg-white/8 backdrop-blur-2xl border-r border-white/10 shadow-[20px_0_45px_rgba(8,14,30,0.45)]" : "bg-[#e6e6e8] border-r border-[#b8bdc4] shadow-[12px_0_24px_rgba(40,42,48,0.10)]"}`}>
          {/* Brand */}
          <div className="px-6 mb-10">
            <div className="flex items-center gap-3">
              <BrandLogo brand={brand} accentHex={brandAccent.hex} className="w-10 h-10 rounded-lg shadow-lg flex-shrink-0" />
              <div>
                <h1 className={`text-lg font-black font-headline leading-none ${isStudioGlass ? "text-white" : "text-slate-900"}`}>{brandMeta.label}</h1>
                <p className={`text-[10px] tracking-wide font-headline mt-1 ${isStudioGlass ? "text-slate-400" : "text-slate-500"}`}>{brandMeta.tagline}</p>
              </div>
            </div>
          </div>

          {/* Nav */}
          <nav className="flex-1 space-y-1">
            {[
              { icon: "analytics", label: "Activity", path: "/activity" },
              { icon: "movie_filter", label: "Library", path: "/library" },
              { icon: "calendar_month", label: "Calendar", path: "/calendar" },
              { icon: "error", label: "Errors", path: "/errors" },
              { icon: "terminal", label: "Logs", path: "/logs" },
              { icon: "settings", label: "Settings", path: "/settings" },
            ].map(({ icon, label, path }) =>
              isActive(path) ? (
                <button key={path} type="button" onClick={() => tryNavigate(path)}
                  className={`flex items-center w-full px-6 py-3 gap-4 font-headline text-sm uppercase tracking-widest transition-all duration-200 border-l-4 ${isStudioGlass ? "" : "text-slate-900"}`}
                  style={{ backgroundColor: alphaColor(brandAccent.hex, isStudioGlass ? 0.2 : 0.14), color: isStudioGlass ? brandAccent.text : "#0f172a", borderLeftColor: brandAccent.hex }}>
                  <span className="material-symbols-outlined">{icon}</span>
                  <span>{label}</span>
                </button>
              ) : (
                <button key={path} type="button" onClick={() => tryNavigate(path)}
                  className={`flex items-center w-full px-6 py-3 gap-4 transition-all duration-200 font-headline text-sm uppercase tracking-widest group ${isStudioGlass ? "text-slate-400 hover:text-slate-100 hover:bg-white/5" : "text-slate-600 hover:text-slate-900 hover:bg-[#e4edf8]"}`}>
                  <span className="material-symbols-outlined transition-transform group-hover:translate-x-1">{icon}</span>
                  <span>{label}</span>
                </button>
              )
            )}
          </nav>

          {/* Footer */}
          <div className="px-6 mt-auto pt-6 border-t border-[#424753]/20">
            <button type="button"
              className="w-full text-white font-headline text-xs font-bold uppercase tracking-widest py-3 rounded-lg shadow-lg active:scale-95 transition-transform flex items-center justify-center gap-2"
              style={{ backgroundColor: brandAccent.hex }}>
              <span className="material-symbols-outlined text-sm">sync</span>
              Sync Library
            </button>
            <div className="mt-6 space-y-2">
              <a href="#" className={`flex items-center gap-3 text-xs font-headline tracking-widest uppercase transition-colors ${isStudioGlass ? "text-slate-400 hover:text-slate-100" : "text-slate-500 hover:text-slate-900"}`}>
                <span className="material-symbols-outlined text-sm">help</span>
                <span>Support</span>
              </a>
              <a href="#" className={`flex items-center gap-3 text-xs font-headline tracking-widest uppercase transition-colors ${isStudioGlass ? "text-slate-400 hover:text-slate-100" : "text-slate-500 hover:text-slate-900"}`}>
                <span className="material-symbols-outlined text-sm">description</span>
                <span>Docs</span>
              </a>
            </div>
          </div>
        </aside>

        {/* Main area */}
        <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {/* Topbar */}
          <header className={`flex justify-between items-center w-full px-6 py-3 h-16 z-10 flex-shrink-0 border-b ${isStudioGlass ? "bg-white/6 backdrop-blur-xl" : "bg-[#ececee] border-[#b8bdc4]"}`}
            style={{ borderColor: isStudioGlass ? alphaColor(brandAccent.hex, 0.2) : "#b8bdc4" }}>
            <div className="flex items-center flex-1 max-w-xl">
              <div
                className="relative w-full max-w-lg"
                onBlur={() => {
                  window.setTimeout(() => setTitleSearchOpen(false), 120);
                }}
              >
                <span className={`material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-lg pointer-events-none ${isStudioGlass ? "text-slate-500" : "text-slate-400"}`}>search</span>
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
                  className={`w-full rounded-lg py-2 pl-10 pr-4 text-sm border focus:outline-none placeholder-slate-500 ${isStudioGlass ? `bg-white/10 text-slate-100 border-white/20 ${getBrandFocusClass(brand, themeMode)}` : `bg-white text-slate-900 border-[#cddbeb] ${getBrandFocusClass(brand, themeMode)}`}`}
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
            <div className="flex items-center gap-3 ml-4">
              <select
                value={theme}
                onChange={(e) => setTheme(e.target.value as any)}
                className={`rounded-lg border px-3 py-2 text-xs outline-none ${isStudioGlass ? `border-[#2a3444] bg-[#1e2430] text-slate-300 ${getBrandFocusClass(brand, themeMode)}` : `border-[#cddbeb] bg-white text-slate-700 ${getBrandFocusClass(brand, themeMode)}`}`}
                style={{ minWidth: "120px" }}
              >
                {THEME_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>

              <select
                value={brand}
                onChange={(e) => setBrand(e.target.value as Brand)}
                className={`rounded-lg border px-3 py-2 text-xs outline-none ${isStudioGlass ? `border-[#2a3444] bg-[#1e2430] text-slate-300 ${getBrandFocusClass(brand, themeMode)}` : `border-[#cddbeb] bg-white text-slate-700 ${getBrandFocusClass(brand, themeMode)}`}`}
                style={{ minWidth: "150px" }}
              >
                {BRAND_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>

              {/* Theme mode selector moved to top-right (replaces db/connectivity/profile icons) */}
              <select
                value={themeMode}
                onChange={(e) => setThemeMode(e.target.value as ThemeMode)}
                className={`rounded-lg border px-3 py-2 pr-8 text-xs outline-none ${isStudioGlass ? `border-[#2a3444] bg-[#1e2430] text-slate-300 ${getBrandFocusClass(brand, themeMode)}` : `border-[#cddbeb] bg-white text-slate-700 ${getBrandFocusClass(brand, themeMode)}`}`}
                style={{ minWidth: "118px" }}
              >
                {THEME_MODE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
          </header>

          {/* Setup banner */}
          {!setupStatus?.setup_complete ? (
            <div className="border-b border-l-4 px-6 py-3 text-sm"
              style={{ backgroundColor: alphaColor(brandAccent.hex, isStudioGlass ? 0.15 : 0.12), borderColor: alphaColor(brandAccent.hex, isStudioGlass ? 0.35 : 0.3), borderLeftColor: brandAccent.hex, color: isStudioGlass ? brandAccent.text : "#374151" }}>
              Onboarding required. Complete the setup wizard to unlock the full dashboard flow.
            </div>
          ) : null}

          {/* Content */}
          <main ref={(el) => { contentScrollRef.current = el; }} className={`flex-1 overflow-y-auto p-6 ${isStudioGlass ? "bg-transparent" : "bg-[#eef3f8]"}`}>
            {renderTabBody()}
          </main>

          {errorMessage ? (
            <div className="mx-6 mb-4 p-3 rounded-lg bg-red-900/30 border border-red-500/40 text-red-300 text-sm">{errorMessage}</div>
          ) : null}
        </div>

        {/* Onboarding wizard */}
        {onboardingVisible && settingsPayload ? (
          <OnboardingWizard
            payload={settingsPayload}
            stepIndex={onboardingStepIndex}
            values={fieldValues}
            hasUnsavedChanges={hasUnsavedChanges}
            brand={brand}
            themeMode={themeMode}
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
        ) : null}

        <Routes>
          <Route path="/" element={<Navigate to={defaultLandingPath} replace />} />
          <Route path="*" element={null} />
        </Routes>
      </div>
    );
  }

  // Main UI block with injected wrapper classes
  return (
    <div className={`app-shell theme-${theme} layout-${brand}-${themeMode} theme-${themeMode}`}>
      <header className="topbar">
        <div className="topbar-content">
          <h1>{brandMeta.label} Dashboard</h1>
          <p>
            Auto-refresh {REFRESH_MS / 1000}s · Last sync: {stats?.last_sync ? timeAgo(stats.last_sync) : "never"}
          </p>
        </div>
            <div className="topbar-actions" style={{ display: 'flex', alignItems: 'center', gap: '15px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <select
                value={brand}
                onChange={(e) => setBrand(e.target.value as Brand)}
                className="input layout-selector"
                style={{ minWidth: "150px" }}
              >
                {BRAND_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              <select
                value={themeMode}
                onChange={(e) => setThemeMode(e.target.value as ThemeMode)}
                className="input"
                style={{ minWidth: "118px", paddingRight: "1.9rem" }}
              >
                {THEME_MODE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              <div className="meta-pill">Dashboard route: /</div>
            </div>
      </header>

      {!setupStatus?.setup_complete ? (
        <div className="setup-banner">Onboarding required. Complete the setup wizard to unlock the full dashboard flow.</div>
      ) : null}

      <section className="stats-grid">
        <StatCard accent={brandAccent} title="Movies" value={stats?.movies.total} sub={`Downloaded ${stats?.movies.downloaded ?? "--"} • Placeholders ${stats?.movies.placeholders ?? "--"}`} />
        <StatCard accent={brandAccent} title="Series" value={stats?.series.total} sub="Tracked series" />
        <StatCard accent={brandAccent} title="Episodes" value={stats?.episodes.total} sub={`Downloaded ${stats?.episodes.downloaded ?? "--"} • Placeholders ${stats?.episodes.placeholders ?? "--"}`} />
        <StatCard accent={brandAccent} title="Placeholders" value={stats?.placeholders_on_disk} sub="On disk" />
        <StatCard accent={brandAccent} title="Jobs" value={stats?.jobs.pending} sub={`Done ${stats?.jobs.done ?? "--"} • Failed ${stats?.jobs.failed ?? "--"}`} />
      </section>

      <nav className="tabs" aria-label="Dashboard sections">
        <TabLink path="/activity" currentPath={location.pathname} label="Activity" onNavigate={tryNavigate} />
        <TabLink path="/library" currentPath={location.pathname} label="Library" onNavigate={tryNavigate} />
        <TabLink path="/calendar" currentPath={location.pathname} label="Calendar" onNavigate={tryNavigate} />
        <TabLink path="/errors" currentPath={location.pathname} label={`Errors${errors.length ? ` (${errors.length})` : ""}`} onNavigate={tryNavigate} />
        <TabLink path="/logs" currentPath={location.pathname} label="Logs" onNavigate={tryNavigate} />
        <TabLink path="/settings" currentPath={location.pathname} label="Settings" onNavigate={tryNavigate} />
      </nav>

      <main ref={(el) => { contentScrollRef.current = el; }} className="panel">{renderTabBody()}</main>
      {errorMessage ? <div className="error-box" style={{ marginTop: 12 }}>{errorMessage}</div> : null}

      {onboardingVisible && settingsPayload ? (
        <OnboardingWizard
          payload={settingsPayload}
          stepIndex={onboardingStepIndex}
          values={fieldValues}
          hasUnsavedChanges={hasUnsavedChanges}
          brand={brand}
          themeMode={themeMode}
          onNext={() => setOnboardingStepIndex((i) => Math.min(WIZARD_STEPS.length - 1, i + 1))}
            onChange={(key, value) => setFieldValues((prev) => ({ ...prev, [key]: value }))}
            onPartialSave={handlePartialSave}
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
      ) : null}

      <Routes>
        <Route path="/" element={<Navigate to={defaultLandingPath} replace />} />
        <Route path="*" element={null} />
      </Routes>


    </div>
  );
}

function TabLink(props: {
  path: string;
  currentPath: string;
  label: string;
  onNavigate: (path: string) => void;
}) {
  const active = props.currentPath === props.path || props.currentPath.startsWith(`${props.path}/`);
  return (
    <button
      type="button"
      className={`tab ${active ? "active" : ""}`}
      onClick={() => props.onNavigate(props.path)}
    >
      {props.label}
    </button>
  );
}

function StatCard(props: { title: string; value: number | undefined; sub: string; accent?: BrandAccent; onClick?: () => void }) {
  const accent = props.accent ?? { label: "", hex: "#7B9FD4", text: "#fff", icon: "#cfe0ff", hoverHex: "#6889bb" };
  const [hover, setHover] = useState(false);
  const baseStyle: React.CSSProperties = {
    borderLeft: `6px solid ${accent.hex}`,
    borderTop: `2px solid ${accent.hex}`,
    borderBottom: `2px solid ${accent.hex}`,
    borderRight: `1px solid ${accent.hex}`,
    background: undefined,
    paddingLeft: 12,
    transition: "transform 0.18s ease, box-shadow 0.18s ease",
    cursor: props.onClick ? "pointer" : undefined,
  };

  const hoverStyle: React.CSSProperties = hover
    ? { transform: "translateY(-6px)", boxShadow: `0 12px 36px ${alphaColor(accent.hex, 0.22)}` }
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
        <div className="stat-title" style={{ color: accent.text }}>{props.title}</div>
        <div className="stat-value" style={{ color: accent.hex }}>{props.value ?? "--"}</div>
      </div>
      <div className="stat-sub" style={{ color: alphaColor(accent.hex, 0.8) }}>{props.sub}</div>
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
  const tab = props.activityTab || "system";
  const placeholderRows = props.placeholderRows || [];

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
          title="Movies"
          value={s?.movies.total}
          sub={`Downloaded ${s?.movies.downloaded ?? "--"} • Placeholders ${s?.movies.placeholders ?? "--"}`}
          onClick={() => props.onOpenLibraryFilter?.("movie")}
        />
        <StatCard accent={accent} title="Series" value={s?.series.total} sub="Tracked series" onClick={() => props.onOpenLibraryFilter?.("series")} />
        <StatCard accent={accent} title="Episodes" value={s?.episodes.total} sub={`Downloaded ${s?.episodes.downloaded ?? "--"} • Placeholders ${s?.episodes.placeholders ?? "--"}`} />
        <StatCard accent={accent} title="Placeholders" value={s?.placeholders_on_disk} sub="On disk" onClick={() => props.onOpenLibraryFilter?.("placeholders")} />
        <StatCard accent={accent} title="Jobs" value={s?.jobs.pending} sub={`Done ${s?.jobs.done ?? "--"} • Failed ${s?.jobs.failed ?? "--"}`} />
      </div>

      {/* Tab buttons */}
      <div className="flex gap-2 mb-6 border-b border-[#424753]/30 pb-4">
        <button
          type="button"
          onClick={() => props.onActivityTabChange?.("system")}
          className={`px-4 py-2 rounded-tl-lg rounded-tr-lg text-xs font-headline uppercase tracking-wider transition-colors ${
            tab === "system"
              ? "text-white font-bold border-b-2"
              : "text-slate-400 hover:text-slate-200"
          }`}
          style={tab === "system" ? { borderBottomColor: accent.hex, backgroundColor: accent.hex + "15" } : {}}
        >
          System Activity
        </button>
        <button
          type="button"
          onClick={() => props.onActivityTabChange?.("placeholders")}
          className={`px-4 py-2 rounded-tl-lg rounded-tr-lg text-xs font-headline uppercase tracking-wider transition-colors ${
            tab === "placeholders"
              ? "text-white font-bold border-b-2"
              : "text-slate-400 hover:text-slate-200"
          }`}
          style={tab === "placeholders" ? { borderBottomColor: accent.hex, backgroundColor: accent.hex + "15" } : {}}
        >
          Placeholder History
        </button>
      </div>

      {/* System Activity table */}
      {tab === "system" && (
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden mb-6">
          <div className="flex justify-between items-start px-5 py-4 border-b border-[#424753]/30">
            <div>
              <h2 className="text-xl font-bold text-white font-headline">Recent Operations</h2>
              <p className="text-xs text-slate-400 mt-0.5">{props.rows.length} recent operations</p>
            </div>
          </div>
          {!props.rows.length ? (
            <div className="p-10 text-center text-slate-500 text-sm">No recent activity.</div>
          ) : (
            <>
              <div className="overflow-x-auto">
              <table className="min-w-[520px] w-full">
                <thead>
                  <tr className="border-b border-[#424753]/20">
                    {["When", "Operation", "Status"].map(h => (
                      <th key={h} className="px-5 py-3 text-left text-[10px] font-headline uppercase tracking-widest text-slate-500 font-normal">{h}</th>
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

                    return (
                      <tr key={`${row.type}-${row.time || idx}`} className="hover:bg-[#1e2430]/40 transition-colors">
                        <td className="px-5 py-4 text-sm text-slate-400 whitespace-nowrap">{timeAgo(row.time || null)}</td>
                        <td className="px-5 py-4 text-sm text-slate-300">
                          <span className="font-medium">{displayName}</span>
                          {detailLine && <div className="text-xs text-slate-500 mt-0.5">{detailLine}</div>}
                          {errorMessage && <span className="text-xs text-red-300/70">{errorMessage}</span>}
                        </td>
                        <td className="px-5 py-4">
                          <div className="flex items-center gap-2">
                            <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotColor}`} />
                            <span className={`text-xs font-medium font-headline uppercase tracking-wider ${statusColor}`}>
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
              <div className="px-5 py-3 border-t border-[#424753]/20 text-[10px] text-slate-500 font-headline uppercase tracking-widest">
                Showing {props.rows.length} items
              </div>
            </>
          )}
        </div>
      )}

      {/* Placeholder history table */}
      {tab === "placeholders" && (
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden mb-6">
          <div className="flex justify-between items-start px-5 py-4 border-b border-[#424753]/30">
            <div>
              <h2 className="text-xl font-bold text-white font-headline">Placeholder History</h2>
              <p className="text-xs text-slate-400 mt-0.5">{placeholderRows.length} recent placeholder changes</p>
            </div>
          </div>
          {!placeholderRows.length ? (
            <div className="p-10 text-center text-slate-500 text-sm">No placeholder history yet.</div>
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
                  <tr className="border-b border-[#424753]/20">
                    {["When", "Content", "Action", "Reason"].map(h => (
                      <th key={h} className="px-2 sm:px-3 py-3 text-left text-[10px] font-headline uppercase tracking-widest text-slate-500 font-normal">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#424753]/15">
                  {placeholderRows.map((row, idx) => {
                    const actionColor = row.action === "Created" ? "text-green-400" : "text-orange-400";
                    const actionBg = row.action === "Created" ? "bg-green-500/20" : "bg-orange-500/20";
                    const contentDisplay = row.series_title
                      ? `${row.series_title} • ${row.item_title}`
                      : row.item_title;

                    return (
                      <tr key={`placeholder-${row.id}-${idx}`} className="hover:bg-[#1e2430]/40 transition-colors">
                        <td className="px-2 sm:px-3 py-4 text-xs sm:text-sm text-slate-400 whitespace-nowrap truncate" title={timeAgo(row.time || null)}>{timeAgo(row.time || null)}</td>
                        <td className="px-2 sm:px-3 py-4 text-xs sm:text-sm text-slate-300 min-w-0">
                          <span className="font-medium block truncate" title={contentDisplay}>{contentDisplay}</span>
                          <div className="text-[10px] text-slate-500 mt-0.5 truncate hidden lg:block" title={row.path || undefined}>{row.path}</div>
                        </td>
                        <td className="px-2 sm:px-3 py-4">
                          <span className={`inline-flex items-center px-2 py-1 rounded text-[10px] sm:text-xs font-medium font-headline uppercase tracking-wider whitespace-nowrap ${actionColor} ${actionBg}`}>
                            {row.action}
                          </span>
                        </td>
                        <td className="px-2 sm:px-3 py-4 text-xs sm:text-sm text-slate-400 truncate" title={row.reason || undefined}>{row.reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              </div>
              <div className="px-5 py-3 border-t border-[#424753]/20 text-[10px] text-slate-500 font-headline uppercase tracking-widest">
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
  items: LibraryItem[];
  activeFilter: LibraryFilter;
  onFilterChange: (value: LibraryFilter) => void;
  onOpenDetail: (item: LibraryItem) => void;
  stats: StatsResponse | null;
  brand: Brand; themeMode: ThemeMode;
}) {
  const accent = getBrandAccent(props.brand, props.themeMode);
  const filters: Array<{ id: LibraryFilter; label: string }> = [
    { id: "all", label: "All" },
    { id: "movie", label: "Movies" },
    { id: "series", label: "Series" },
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
    if (item.has_missing) return <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-red-600 text-white font-headline uppercase tracking-wider">Missing</span>;
    if (item.is_4k) return <span className="px-1.5 py-0.5 rounded text-[10px] font-bold text-white font-headline uppercase tracking-wider" style={{ backgroundColor: accent.hex }}>4K Ultra HD</span>;
    if (item.has_placeholder) return <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-teal-700 text-white font-headline uppercase tracking-wider">Placeholder</span>;
    if (item.is_future) return <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-slate-600 text-white font-headline uppercase tracking-wider">Future</span>;
    if (item.has_file) return <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-slate-500 text-white font-headline uppercase tracking-wider">1080p</span>;
    return null;
  }

  return (
    <div>
      {/* Header + filter tabs */}
      <div className="flex flex-wrap justify-between items-end gap-4 mb-6">
        <div>
          <h2 className="text-3xl font-black text-white tracking-tight font-headline">Library Explorer</h2>
          <p className="text-sm text-slate-400 mt-1">Showing {props.items.length} items matching your criteria</p>
        </div>
        <div className="flex flex-wrap gap-1 bg-[#171c22] p-1 rounded-lg border border-[#424753]/40">
          {filters.map(f => (
            <button key={f.id} type="button" onClick={() => props.onFilterChange(f.id)}
              className={`px-4 py-1.5 rounded-md text-xs font-headline uppercase tracking-wider transition-colors ${
                f.id === props.activeFilter
                    ? "text-white font-semibold"
                  : "text-slate-400 hover:text-slate-200"
                  }`} style={f.id === props.activeFilter ? { backgroundColor: accent.hex } : undefined}>
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {/* Poster grid with alphabet sections */}
      {props.items.length === 0 ? (
        <div className="text-center text-slate-500 py-16">No library items match the current filter.</div>
      ) : (
        <div className="flex items-start gap-4 mb-8">
          <div className="flex-1 space-y-8">
            {groupedItems.letters.map((letter) => (
              <div key={letter} ref={(el) => { sectionRefs.current[letter] = el; }}>
                <div className="mb-3 text-xs font-headline uppercase tracking-widest text-slate-500 border-b border-[#424753]/25 pb-2">{letter}</div>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
                  {(groupedItems.groups[letter] || []).map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => props.onOpenDetail(item)}
                      className="relative isolate rounded-xl overflow-hidden bg-[#1e2430] group cursor-pointer text-left transition-transform hover:scale-[1.02]"
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
                        className="absolute pointer-events-none bottom-0 left-0 right-0 h-[34%]"
                        style={{
                          background:
                            "linear-gradient(180deg, rgba(15,20,25,0) 0%, rgba(15,20,25,0.62) 58%, rgba(15,20,25,0.95) 100%)",
                        }}
                      />
                      <div className="absolute top-2 left-2">{statusBadge(item)}</div>
                      <div className="absolute bottom-0 left-0 right-0 p-3">
                        <div className="font-bold text-white text-sm leading-tight truncate">{item.title}</div>
                        <div className="mt-0.5 flex items-center justify-between gap-2">
                          <div className="text-[11px] text-slate-400">{item.year || "--"}</div>
                          <div className="px-1.5 py-0.5 rounded bg-black/45 border border-white/20 text-[9px] text-slate-200 font-headline uppercase tracking-wider">{item.type}</div>
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div className="hidden lg:flex sticky top-24 flex-col gap-1 rounded-lg border border-[#424753]/35 bg-[#111722]/90 px-2 py-2">
            {groupedItems.letters.map((letter) => (
              <button
                key={`alpha-${letter}`}
                type="button"
                onClick={() => sectionRefs.current[letter]?.scrollIntoView({ behavior: "smooth", block: "start" })}
                className="w-6 h-6 rounded text-[10px] font-headline font-bold text-slate-400 hover:text-white hover:bg-[#293346] transition-colors"
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
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="flex justify-between items-start">
            <div className="text-[10px] font-headline uppercase tracking-widest text-slate-400 mb-3">Total Items</div>
            <span className="material-symbols-outlined text-slate-600" style={{ fontSize: 18 }}>storage</span>
          </div>
          <div className="text-3xl font-black text-white font-headline">{props.items.length}</div>
        </div>
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="flex justify-between items-start">
            <div className="text-[10px] font-headline uppercase tracking-widest text-slate-400 mb-3">Missing Assets</div>
            <span className="material-symbols-outlined text-yellow-500" style={{ fontSize: 18 }}>warning</span>
          </div>
          <div className="text-3xl font-black text-white font-headline">{totalMissing}</div>
          {totalMissing > 0 && (
            <button type="button" onClick={() => props.onFilterChange("missing")}
              className="mt-3 text-xs font-headline uppercase tracking-wider flex items-center gap-1" style={{ color: accent.icon }}>
              View Errors <span className="material-symbols-outlined" style={{ fontSize: 14 }}>arrow_forward</span>
            </button>
          )}
        </div>
        <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 p-5">
          <div className="flex justify-between items-start">
            <div className="text-[10px] font-headline uppercase tracking-widest text-slate-400 mb-3">Sync Status</div>
            <span className="material-symbols-outlined" style={{ fontSize: 18, color: accent.hex }}>sync</span>
          </div>
          <div className="flex items-center gap-2 mb-1">
            <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
            <span className="text-white font-bold font-headline text-sm">Active</span>
          </div>
          <div className="text-xs text-slate-400">Library indexed</div>
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
      {error ? <div className="mx-6 mt-4 p-4 bg-red-600/15 border border-red-500/30 rounded-xl text-sm text-red-300">{error}</div> : null}
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

function MovieDetail(props: { payload: MovieDetailResponse; brand: Brand; themeMode: ThemeMode }) {
  const p = props.payload;
  const accent = getBrandAccent(props.brand, props.themeMode);
  const isLight = props.themeMode === "light";
  const heroArtUrl = p.backdrop_url || p.poster_url;
  return (
    <div>
      {/* Hero banner */}
      <div className="relative h-[22rem] md:h-[30rem] lg:h-[34rem] overflow-hidden"
        style={heroArtUrl ? { backgroundImage: `linear-gradient(to right, ${isLight ? "rgba(238,243,248,0.90)" : "rgba(8,12,18,0.78)"} 18%, ${isLight ? "rgba(238,243,248,0.52)" : "rgba(8,12,18,0.45)"} 42%, ${isLight ? "rgba(238,243,248,0.10)" : "rgba(8,12,18,0.08)"}), url(${heroArtUrl})`, backgroundSize: "cover", backgroundPosition: "center 35%" } : { backgroundColor: alphaColor(accent.hex, isLight ? 0.14 : 0.2) }}>
        <div
          className="absolute inset-0"
          style={{
            background: isLight
              ? "linear-gradient(180deg, rgba(238,243,248,0) 38%, rgba(238,243,248,0.28) 70%, rgba(238,243,248,0.64) 100%)"
              : "linear-gradient(180deg, rgba(15,20,25,0) 38%, rgba(15,20,25,0.20) 70%, rgba(15,20,25,0.58) 100%)",
          }}
        />
      </div>

      <div className="px-6 md:px-10 lg:px-12 -mt-64 md:-mt-80 lg:-mt-96 relative pb-10">
        <div className="flex gap-6 md:gap-10 items-end mb-8">
          <div className={`flex-none w-40 h-60 md:w-52 md:h-[19.5rem] lg:w-56 lg:h-[21rem] rounded-2xl overflow-hidden border-2 shadow-[0_30px_80px_rgba(0,0,0,0.5)] ${isLight ? "border-[#d7e2f0] bg-white" : "border-[#424753]/40 bg-[#1e2430]"}`}>
            {p.poster_url ? <img src={p.poster_url} alt="" className="w-full h-full object-cover" /> : <div className="w-full h-full flex items-center justify-center text-slate-600 font-bold">MOV</div>}
          </div>
          <div className="flex-1 pb-1 md:pb-2">
            <h1 className={`text-4xl md:text-6xl lg:text-7xl font-black font-headline tracking-tight leading-[0.95] ${isLight ? "text-slate-900" : "text-white"}`}>{p.title}</h1>
            <div className="flex items-center gap-3 mt-4 flex-wrap">
              {p.year && <span className={`text-lg ${isLight ? "text-slate-700" : "text-slate-200"}`}>{p.year}</span>}
              <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase" style={{ backgroundColor: alphaColor(accent.hex, 0.2), border: `1px solid ${alphaColor(accent.hex, 0.35)}`, color: accent.text }}>Movie</span>
              {p.is_4k && <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase text-white" style={{ backgroundColor: accent.hex }}>4K</span>}
              {p.has_placeholder && <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase bg-teal-600/30 border border-teal-500/30 text-teal-300">Placeholder</span>}
              {p.has_file
                ? <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase bg-green-600/20 border border-green-500/30 text-green-300">Downloaded</span>
                : <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase bg-red-600/20 border border-red-500/30 text-red-300">Missing</span>}
            </div>
            {p.arr_link && (
              <div className="mt-4">
                <a href={p.arr_link} target="_blank" rel="noreferrer"
                  className="inline-flex items-center gap-1.5 px-4 py-2.5 bg-[#252e3a] border border-[#424753]/40 rounded-lg text-xs text-slate-300 hover:text-white font-headline uppercase tracking-wider transition-colors">
                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>open_in_new</span>
                  Open in Radarr
                </a>
              </div>
            )}
          </div>
        </div>

        {p.overview && <p className={`text-lg leading-relaxed max-w-5xl mb-8 ${isLight ? "text-slate-700" : "text-slate-200"}`}>{p.overview}</p>}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          {[
            { label: "Status", value: p.status },
            { label: "Determination", value: p.determination },
            { label: "Quality", value: p.radarr_quality },
            { label: "Theatrical", value: p.theater_release_date },
            { label: "Digital", value: p.digital_release_date },
            { label: "Physical", value: p.physical_release_date },
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
  const p = props.payload;
  const accent = getBrandAccent(props.brand, props.themeMode);
  const isLight = props.themeMode === "light";
  const heroArtUrl = p.backdrop_url || p.poster_url;
  const seasonsDesc = useMemo(
    () => [...(p.seasons || [])].sort((a, b) => (b.season_number || 0) - (a.season_number || 0)),
    [p.seasons],
  );
  return (
    <div>
      {/* Hero banner */}
      <div className="relative h-[22rem] md:h-[30rem] lg:h-[34rem] bg-gradient-to-r from-[#0a0e14] to-[#1a2233] overflow-hidden"
        style={heroArtUrl ? { backgroundImage: `linear-gradient(to right, rgba(8,12,18,0.78) 18%, rgba(8,12,18,0.45) 42%, rgba(8,12,18,0.08)), url(${heroArtUrl})`, backgroundSize: "cover", backgroundPosition: "center 35%" } : {}}>
        <div
          className="absolute inset-0"
          style={{ background: "linear-gradient(180deg, rgba(15,20,25,0) 38%, rgba(15,20,25,0.20) 70%, rgba(15,20,25,0.58) 100%)" }}
        />
      </div>

      <div className="px-6 md:px-10 lg:px-12 -mt-64 md:-mt-80 lg:-mt-96 relative pb-10">
        <div className="flex gap-6 md:gap-10 items-end mb-8">
          <div className="flex-none w-40 h-60 md:w-52 md:h-[19.5rem] lg:w-56 lg:h-[21rem] rounded-2xl overflow-hidden border-2 border-[#424753]/40 bg-[#1e2430] shadow-[0_30px_80px_rgba(0,0,0,0.5)]">
            {p.poster_url ? <img src={p.poster_url} alt="" className="w-full h-full object-cover" /> : <div className="w-full h-full flex items-center justify-center text-slate-600 font-bold">TV</div>}
          </div>
          <div className="flex-1 pb-1 md:pb-2">
            <h1 className="text-4xl md:text-6xl lg:text-7xl font-black text-white font-headline tracking-tight leading-[0.95]">{p.title}</h1>
            <div className="flex items-center gap-3 mt-4 flex-wrap">
              {p.year && <span className="text-lg text-slate-200">{p.year}</span>}
              {p.network && <span className="text-lg text-slate-300">{p.network}</span>}
              <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase bg-orange-600/20 border border-orange-500/30 text-orange-300">Series</span>
              {p.is_4k && <span className="px-2.5 py-1 rounded text-xs font-bold font-headline uppercase text-white" style={{ backgroundColor: accent.hex }}>4K</span>}
              {p.sonarr_monitored != null && (
                <span className={`px-2.5 py-1 rounded text-xs font-bold font-headline uppercase ${p.sonarr_monitored ? "" : "bg-slate-600/30 border border-slate-500/30 text-slate-400"}`}
                  style={p.sonarr_monitored ? { backgroundColor: alphaColor(accent.hex, 0.2), border: `1px solid ${alphaColor(accent.hex, 0.35)}`, color: accent.text } : undefined}>
                  {p.sonarr_monitored ? "Monitored" : "Unmonitored"}
                </span>
              )}
            </div>
            {p.arr_link && (
              <div className="mt-4">
                <a href={p.arr_link} target="_blank" rel="noreferrer"
                  className="inline-flex items-center gap-1.5 px-4 py-2.5 bg-[#252e3a] border border-[#424753]/40 rounded-lg text-xs text-slate-300 hover:text-white font-headline uppercase tracking-wider transition-colors">
                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>open_in_new</span>
                  Open in Sonarr
                </a>
              </div>
            )}
          </div>
        </div>

        {p.overview && <p className="text-lg text-slate-200 leading-relaxed max-w-5xl mb-8">{p.overview}</p>}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
          {[
            { label: "Status", value: p.status },
            { label: "Sonarr Status", value: p.sonarr_status },
            { label: "First Aired", value: p.first_aired },
            { label: "Network", value: p.network },
          ].filter(m => m.value).map(m => (
            <div key={m.label} className={`rounded-xl border p-5 ${isLight ? "bg-white border-[#d7e2f0]" : "bg-[#171c22] border-[#424753]/40"}`}>
              <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-1">{m.label}</div>
              <div className={`text-base font-semibold ${isLight ? "text-slate-900" : "text-white"}`}>{m.value}</div>
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
                  className="w-full flex items-center justify-between px-5 py-4 text-left hover:bg-[#1e2430]/50 transition-colors">
                  <div className="flex items-center gap-3">
                    <span className="material-symbols-outlined text-slate-500 transition-transform" style={{ fontSize: 18, transform: open ? "rotate(90deg)" : "rotate(0deg)" }}>chevron_right</span>
                    <span className="text-sm font-bold text-white font-headline">
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
                  <div className="border-t border-[#424753]/30 divide-y divide-[#424753]/15">
                    {season.episodes.map(ep => (
                      <div key={ep.id} className="flex items-start gap-4 px-5 py-3 hover:bg-[#1e2430]/30 transition-colors">
                        <span className="flex-none w-10 text-xs text-slate-500 font-mono pt-0.5">E{String(ep.episode_number).padStart(2, "0")}</span>
                        <div className="flex-1 min-w-0">
                          <div className="text-sm text-white font-medium">{ep.title || `Episode ${ep.episode_number}`}</div>
                          <div className="text-xs text-slate-500 mt-0.5">{ep.air_date || "No air date"}</div>
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

  // For episode cards, find the specific episode overview inside the series detail
  const episodeOverview = (() => {
    if (!props.selectedItem || props.selectedItem.media_type !== "episode") return null;
    if (!props.spotlight || !('seasons' in props.spotlight)) return null;
    for (const season of (props.spotlight as SeriesDetailResponse).seasons) {
      const ep = season.episodes.find(e => e.id === props.selectedItem!.item_id);
      if (ep?.overview) return ep.overview;
    }
    return null;
  })();

  const spotlightOverview = episodeOverview || props.spotlight?.overview || props.selectedItem?.reason || "Select a release on the calendar to inspect it here.";
  const spotlightArrLink = props.spotlight?.arr_link || props.selectedItem?.arr_link;
  const spotlightPoster = props.spotlight?.poster_url || null;
  const spotlightMeta = props.selectedItem ? formatCalendarItemMeta(props.selectedItem) : [];
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
          <div className="mt-1 text-xs text-slate-500">{payload.lookahead.label}</div>
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
                {/* Close button */}
                <button
                  type="button"
                  onClick={closeOverlay}
                  className="absolute top-3 right-3 flex h-8 w-8 items-center justify-center rounded-full bg-black/50 text-slate-300 hover:text-white hover:bg-black/70 transition-colors calendar-spotlight-close"
                >
                  <span className="material-symbols-outlined" style={{ fontSize: 18 }}>close</span>
                </button>
                <div className="absolute inset-x-0 bottom-0 p-5 md:p-6">
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
                          className="rounded px-2.5 py-1 text-[10px] font-bold font-headline uppercase tracking-wider"
                          style={{ backgroundColor: alphaColor(accent.hex, 0.28), borderColor: alphaColor(accent.hex, 0.45), border: `1px solid ${alphaColor(accent.hex, 0.45)}`, color: accent.text }}
                        >
                          {props.selectedItem.media_type === "movie" ? "Movie" : "Episode"}
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
                      {props.selectedItem.subtitle ? (
                        <p className="mt-1 text-sm text-slate-300 truncate">{props.selectedItem.subtitle}</p>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>

              {/* Body */}
              <div className="space-y-5 p-5 md:p-6">
                <p className="text-base leading-relaxed text-slate-200">
                  {props.spotlightLoading ? "Loading metadata..." : spotlightOverview}
                </p>

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
                  {spotlightArrLink ? (
                    <a
                      href={spotlightArrLink}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1.5 rounded-lg border border-[#424753]/40 bg-[#1e2430] px-4 py-2 text-xs font-headline uppercase tracking-wider text-slate-300 transition-colors hover:text-white"
                    >
                      <span className="material-symbols-outlined" style={{ fontSize: 14 }}>north_east</span>
                      Open ARR
                    </a>
                  ) : null}
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
  return (
    <div className={`min-h-[150px] p-3 border-r border-[#424753]/20 last:border-r-0 transition-colors ${
      !day.is_current_month ? "opacity-35" : ""
    } ${day.is_today ? "" : "hover:bg-[#1e2430]/50"}`} style={day.is_today ? { backgroundColor: alphaColor(accent.hex, 0.1) } : undefined}>
      {/* Day number */}
      <div className="flex items-center justify-between mb-1">
        <span className={`text-xs font-bold font-headline leading-none ${
          day.is_today ? "w-5 h-5 flex items-center justify-center rounded-full text-white text-[10px]" : "text-slate-400"
        }`} style={day.is_today ? { backgroundColor: accent.hex } : undefined}>
          {day.day_number}
        </span>
        {day.in_lookahead_window && !day.is_today && (
          <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: alphaColor(accent.hex, 0.5) }} />
        )}
      </div>
      {/* Items */}
      <div className="space-y-1.5">
        {visibleItems.map(item => {
          const metaBits = formatCalendarItemMeta(item).slice(0, 2);
          const releaseColor = item.media_type === "movie"
            ? item.release_type === "digitalRelease"
              ? "border-l-teal-400"
              : item.release_type === "physicalRelease"
                ? "border-l-fuchsia-400"
                : ""
            : "border-l-orange-400";
          const isSelected = props.selectedItemId === item.id;

          return (
            <button
              key={item.id}
              type="button"
              onClick={(e) => props.onSelectItem(item.id, e)}
              className={`w-full rounded-md border border-[#424753]/30 border-l-[3px] ${releaseColor} px-2.5 py-2 text-left transition-colors ${
                isSelected ? "bg-[#2a3344]" : "bg-[#252c38]/80 hover:bg-[#2a3344]"
              }`} style={item.media_type === "movie" && item.release_type !== "digitalRelease" && item.release_type !== "physicalRelease" ? { borderLeftColor: accent.hex } : undefined}
            >
              <div className="flex items-start gap-1.5">
                <span className={`material-symbols-outlined mt-0.5 text-[12px] ${item.media_type === "movie" ? "" : "text-orange-300"}`} style={item.media_type === "movie" ? { color: accent.icon } : undefined}>
                  {item.media_type === "movie" ? "movie" : "tv"}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[12px] font-semibold leading-snug text-white" title={item.title}>{item.title}</div>
                  {item.subtitle ? <div className="mt-0.5 text-[10px] leading-snug text-slate-400">{item.subtitle}</div> : null}
                  {metaBits.length ? <div className="mt-1 text-[10px] leading-snug text-slate-500">{metaBits.map((bit) => bit.value).join(" • ")}</div> : null}
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
        <div className="font-mono text-xs space-y-1 max-h-[60vh] overflow-y-auto">
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
  label: string;
  arr_type: "radarr" | "sonarr";
  instance_key: string;
  url: string;
  api_key: string;
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

function inferDefaultKey(label: string, arrType: "radarr" | "sonarr") {
  const slug = normalizeInstanceKey(label);
  return slug || `${arrType}_instance`;
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
        const items = parsed
          .filter((item) => item && typeof item === "object")
          .map((item, index) => {
            const obj = item as Record<string, unknown>;
            const arrType = String(obj.arr_type || obj.type || "").toLowerCase() === "sonarr" ? "sonarr" : "radarr";
            const label = String(obj.label || obj.instance_key || obj.key || obj.name || `${arrType} ${index + 1}`);
            return {
              id: `json-${arrType}-${index}`,
              label,
              arr_type: arrType,
              instance_key: normalizeInstanceKey(String(obj.instance_key || obj.key || obj.name || inferDefaultKey(label, arrType))),
              url: String(obj.url || ""),
              api_key: String(obj.api_key || obj.apikey || ""),
              is_4k: Boolean(obj.is_4k),
            } satisfies ArrInstanceDraft;
          });
        if (items.length) return items;
      }
    } catch {
      // Fall through to legacy pairs.
    }
  }

  const legacy: ArrInstanceDraft[] = [];
  const radarrUrl = String(values.RADARR_URL || "").trim();
  const radarrKey = String(values.RADARR_API_KEY || "").trim();
  const radarr4kUrl = String(values.RADARR_4K_URL || "").trim();
  const radarr4kKey = String(values.RADARR_4K_API_KEY || "").trim();
  const sonarrUrl = String(values.SONARR_URL || "").trim();
  const sonarrKey = String(values.SONARR_API_KEY || "").trim();
  const sonarr4kUrl = String(values.SONARR_4K_URL || "").trim();
  const sonarr4kKey = String(values.SONARR_4K_API_KEY || "").trim();

  if (radarrUrl || radarrKey) {
    legacy.push({
      id: "legacy-radarr-std",
      label: "Radarr Primary",
      arr_type: "radarr",
      instance_key: normalizeInstanceKey(String(values.RADARR_STD_INSTANCE_KEY || "radarr_std")),
      url: radarrUrl,
      api_key: radarrKey,
      is_4k: false,
    });
  }
  if (radarr4kUrl || radarr4kKey) {
    legacy.push({
      id: "legacy-radarr-4k",
      label: "Radarr 4K",
      arr_type: "radarr",
      instance_key: normalizeInstanceKey(String(values.RADARR_4K_INSTANCE_KEY || "radarr_4k")),
      url: radarr4kUrl,
      api_key: radarr4kKey,
      is_4k: true,
    });
  }
  if (sonarrUrl || sonarrKey) {
    legacy.push({
      id: "legacy-sonarr-std",
      label: "Sonarr Primary",
      arr_type: "sonarr",
      instance_key: normalizeInstanceKey(String(values.SONARR_STD_INSTANCE_KEY || "sonarr_std")),
      url: sonarrUrl,
      api_key: sonarrKey,
      is_4k: false,
    });
  }
  if (sonarr4kUrl || sonarr4kKey) {
    legacy.push({
      id: "legacy-sonarr-4k",
      label: "Sonarr 4K",
      arr_type: "sonarr",
      instance_key: normalizeInstanceKey(String(values.SONARR_4K_INSTANCE_KEY || "sonarr_4k")),
      url: sonarr4kUrl,
      api_key: sonarr4kKey,
      is_4k: true,
    });
  }
  return legacy;
}

function serializeArrInstances(instances: ArrInstanceDraft[]) {
  const clean = instances
    .map((row) => {
      const label = String(row.label || "").trim();
      const inferredKey = normalizeInstanceKey(inferDefaultKey(label, row.arr_type));
      const inferredIs4k = /(^|\s)4k(\s|$)/i.test(label);
      return {
        arr_type: row.arr_type,
        instance_key: inferredKey,
        label,
        url: String(row.url || "").trim(),
        api_key: String(row.api_key || "").trim(),
        is_4k: Boolean(row.is_4k || inferredIs4k),
      };
    })
    .filter((row) => row.instance_key && row.url && row.api_key);
  return JSON.stringify(clean);
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

  return cleaned;
}

function ArrInstancesEditor(props: {
  values: FieldValueMap;
  onValueChange: (key: string, value: unknown) => void;
  accent: BrandAccent;
  onPrimaryTestStatusChange?: (arrType: "radarr" | "sonarr", ok: boolean) => void;
  onSecondaryTestStatusChange?: (arrType: "radarr" | "sonarr", ok: boolean) => void;
}) {
  const [instances, setInstances] = useState<ArrInstanceDraft[]>(() => parseArrInstancesFromValues(props.values));
  const [testState, setTestState] = useState<Record<string, { ok: boolean; message: string }>>({});
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

  useEffect(() => {
    props.onValueChange("WIZARD_RADARR_SECONDARY_ENABLED", secondaryEnabled.radarr);
    props.onValueChange("WIZARD_SONARR_SECONDARY_ENABLED", secondaryEnabled.sonarr);
  }, [props, secondaryEnabled]);


  function update(next: ArrInstanceDraft[]) {
    const radarr = next.filter((item) => item.arr_type === "radarr").slice(0, ARR_INSTANCE_LIMIT_PER_TYPE);
    const sonarr = next.filter((item) => item.arr_type === "sonarr").slice(0, ARR_INSTANCE_LIMIT_PER_TYPE);
    const trimmed = [...radarr, ...sonarr];
    setInstances(trimmed);
    props.onValueChange("ARR_INSTANCES_JSON", serializeArrInstances(trimmed));
  }

  function defaultSlot(arrType: "radarr" | "sonarr", slotIndex: 0 | 1): ArrInstanceDraft {
    const label = arrType === "radarr"
      ? (slotIndex === 0 ? "Radarr Primary" : "Radarr Secondary")
      : (slotIndex === 0 ? "Sonarr Primary" : "Sonarr Secondary");
    return {
      id: `slot-${arrType}-${slotIndex}`,
      label,
      arr_type: arrType,
      instance_key: inferDefaultKey(label, arrType),
      url: "",
      api_key: "",
      is_4k: slotIndex === 1,
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
    typeRows[slotIndex] = { ...target, ...patch };
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
    if (enabled && (!primaryEnabled[arrType] || !primaryConnectionOk[arrType])) return;
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
  }

  const byType = {
    radarr: getTypeRows("radarr"),
    sonarr: getTypeRows("sonarr"),
  };

  function slotFor(arrType: "radarr" | "sonarr", slotIndex: 0 | 1): ArrInstanceDraft {
    return byType[arrType][slotIndex] || defaultSlot(arrType, slotIndex);
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
    return (
      <div key={item.id} className={`rounded-xl border border-[#424753]/40 bg-[#0f1419] overflow-hidden ${isDisabled ? "opacity-60" : ""}`}>
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
              {opts?.statusHint ? <div className="text-[11px] text-slate-500 mt-1">{opts.statusHint}</div> : null}
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
          {opts?.toggleHint && opts.toggleDisabled ? <div className="text-[11px] text-slate-500">{opts.toggleHint}</div> : null}
          <input
            className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-xs text-slate-200"
            value={item.url}
            onChange={(e) => upsertSlot(arrType, slotIndex, { url: e.target.value })}
            placeholder="http://arr-host:port"
            disabled={isDisabled}
          />
          <input
            className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-xs text-slate-200"
            value={item.api_key}
            onChange={(e) => upsertSlot(arrType, slotIndex, { api_key: e.target.value })}
            placeholder="API key"
            type="password"
            disabled={isDisabled}
          />
          <div className="text-[11px] text-slate-400">Instance key (derived): <span className="text-slate-200">{normalizeInstanceKey(inferDefaultKey(String(item.label || ""), item.arr_type))}</span></div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => runTest(item, arrType, slotIndex)}
              className="px-3 py-1.5 rounded-md text-xs bg-[#252e3a] border border-[#424753]/40 text-slate-300"
              disabled={isDisabled}
            >
              Test
            </button>
          </div>
          {status ? (
            <div className={`text-xs ${status.ok ? "text-green-400" : "text-red-400"}`}>{status.message}</div>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {(["radarr", "sonarr"] as const).map((arrType) => (
        <div key={arrType}>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-white font-headline">{arrType === "radarr" ? "Radarr Settings" : "Sonarr Settings"}</h3>

          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {card(slotFor(arrType, 0), arrType, 0, true, {
              showToggle: true,
              enabled: primaryEnabled[arrType],
              onToggle: (enabled) => setPrimary(arrType, enabled),
              statusHint: primaryConnectionOk[arrType]
                ? "Connection test confirmed."
                : "Run a successful primary connection test to unlock the secondary instance toggle.",
            })}
            {card(slotFor(arrType, 1), arrType, 1, false, {
              showToggle: true,
              enabled: secondaryEnabled[arrType],
              onToggle: (enabled) => setSecondary(arrType, enabled),
              toggleDisabled: !primaryEnabled[arrType] || !primaryConnectionOk[arrType],
              toggleHint: !primaryEnabled[arrType]
                ? "Enable and configure the primary instance first."
                : (!primaryConnectionOk[arrType] ? "Run a successful primary connection test first." : undefined),
            })}
          </div>

        </div>
      ))}
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
            {field.description && <p className="text-xs text-slate-500 mt-1">{field.description}</p>}
            {getPlexLibraryIdNote(field.key) ? <p className="text-xs text-slate-500 mt-1">{getPlexLibraryIdNote(field.key)}</p> : null}
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
        {test && (
          <div className={`mt-2 flex items-center gap-1.5 text-xs ${test.ok ? "text-green-400" : "text-red-400"}`}>
            <span className="material-symbols-outlined" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
            {test.message}
          </div>
        )}
      </div>
    );
  }

  const rootBlock = root ? (
    <div
      className="rounded-xl border px-4 py-4"
      style={{
        borderColor: alphaColor(props.accent.hex, 0.4),
        backgroundColor: alphaColor(props.accent.hex, 0.07),
      }}
    >
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <span className="text-sm font-semibold text-white font-headline">{root.label}</span>
        <span className="px-2 py-0.5 rounded text-[9px] font-bold font-headline uppercase tracking-wide bg-emerald-500/15 text-emerald-300/95 border border-emerald-500/25">
          Suggested setup
        </span>
      </div>
      <p className="text-xs text-slate-400 mb-3 leading-relaxed">
        Placeholdarr creates placeholder folders under this root using a simple structure:
        <span className="block mt-1">`movies` and `tv`</span>
      </p>
      {root.description && <p className="text-[11px] text-slate-500 mb-3 leading-relaxed">{root.description}</p>}
      {root.type === "bool" ? renderBool(root) : renderTextInput(root)}
    </div>
  ) : null;

  const profileBlock = profiles.length ? (
    <div className="rounded-xl border border-[#424753]/40 bg-[#0f1419]/55 px-4 py-4">
      <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-2">Folder Profiles</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {profiles.map((field) => (
          <div key={field.key} className="rounded-lg border border-[#424753]/35 bg-[#0b111b] px-3 py-2">
            <div className="text-xs text-slate-300 font-medium mb-2">{field.label}</div>
            {field.key === "ENABLE_STANDARD_PROFILE" ? (
              <span className="text-xs text-emerald-300">Always enabled</span>
            ) : renderBool(field, true)}
            {field.description ? <p className="text-[11px] text-slate-600 mt-2">{field.description}</p> : null}
          </div>
        ))}
      </div>
    </div>
  ) : null;

  const overridesBlock = overrides.length ? (
    <div className={props.layout === "settings" ? "rounded-lg border border-[#424753]/30 bg-[#0f1419]/50 px-4 py-3" : "rounded-lg border border-[#424753]/30 bg-[#0f1419]/50 px-3 py-3"}>
      <button
        type="button"
        onClick={() => setOverridesOpen((o) => !o)}
        className="flex w-full items-start justify-between gap-3 text-left rounded-md outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-[#171c22] focus-visible:ring-emerald-500/40"
      >
        <div className="min-w-0">
          <div className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-0.5">Optional overrides</div>
          <div className="text-sm font-medium text-slate-400 font-headline">Custom folder per library</div>
          <p className="text-[11px] text-slate-600 mt-1 leading-relaxed">
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
              {field.description && <p className="text-[11px] text-slate-600 mb-2 leading-relaxed">{field.description}</p>}
              {field.type === "bool" ? renderBool(field, true) : field.type === "choice" ? renderChoice(field, { compact: true, muted: true }) : renderTextInput(field, { compact: true, muted: true })}
            </div>
          ))}
        </div>
      )}
    </div>
  ) : null;

  if (props.layout === "wizard") {
    return (
      <div className="space-y-5">
        {rootBlock}
        {profileBlock}
        {overridesBlock}
        {rest.map((field) => (
          <div key={field.key}>
            <label className="block text-sm font-semibold text-white font-headline mb-1">{field.label}</label>
            {field.description && <p className="text-xs text-slate-500 mb-2">{field.description}</p>}
            {field.type === "bool" ? renderBool(field) : field.type === "choice" ? renderChoice(field) : renderTextInput(field)}
          </div>
        ))}
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

function SettingsPanel(props: {
  payload: SettingsPayload | null;
  activeSection: string;
  values: FieldValueMap;
  hasUnsavedChanges: boolean;
  feedback: string;
  feedbackKind: "" | "success" | "error";
  brand: Brand;
  themeMode: ThemeMode;
  onSectionChange: (name: string) => void;
  onValueChange: (key: string, value: unknown) => void;
  onSave: () => Promise<void>;
  onTestConnection: (input: { service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr"; urlKey: string; credentialKey: string }) => Promise<{ ok: boolean; message: string }>;
}) {
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [arrSecondaryTestStatus, setArrSecondaryTestStatus] = useState<{ radarr: boolean; sonarr: boolean }>({ radarr: false, sonarr: false });
  const accent = getBrandAccent(props.brand, props.themeMode);

  if (!props.payload) return (
    <div className="flex items-center justify-center h-64">
      <div className="flex items-center gap-3 text-slate-400">
        <div className="w-2 h-2 rounded-full animate-pulse" style={{ backgroundColor: accent.hex }} />
        <span className="text-sm font-headline uppercase tracking-widest">Loading settings...</span>
      </div>
    </div>
  );

  const sectionNames = SETTINGS_SECTION_ORDER.filter((name) => props.payload!.sections.some((s) => s.name === name));
  const active = props.payload.sections.find((s) => s.name === props.activeSection) || props.payload.sections[0];
  const arrInstances = parseArrInstancesFromValues(props.values);
  const hasRadarrSecondaryConfigured = arrInstances.filter((item) => item.arr_type === "radarr").length > 1;
  const hasSonarrSecondaryConfigured = arrInstances.filter((item) => item.arr_type === "sonarr").length > 1;
  const canUseRadarrSecondaryBehavior = hasRadarrSecondaryConfigured && arrSecondaryTestStatus.radarr;
  const canUseSonarrSecondaryBehavior = hasSonarrSecondaryConfigured && arrSecondaryTestStatus.sonarr;
  const canUseAnySecondaryBehavior = canUseRadarrSecondaryBehavior || canUseSonarrSecondaryBehavior;
  const unlockedSettingsSearchBehavior = [
    canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "both") : null,
    canUseSonarrSecondaryBehavior ? String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "both") : null,
    canUseRadarrSecondaryBehavior ? String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match") : null,
    canUseSonarrSecondaryBehavior ? String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match") : null,
  ].filter((value): value is string => Boolean(value));
  const fallbackUnnecessaryBecauseAllBoth = unlockedSettingsSearchBehavior.length > 0 && unlockedSettingsSearchBehavior.every((value) => value === "both");

  useEffect(() => {
    if (fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH)) {
      props.onValueChange("ENABLE_PLAYBACK_FALLBACK_SEARCH", false);
    }
  }, [fallbackUnnecessaryBecauseAllBoth, props, props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH]);

  const SECTION_ICONS: Record<string, string> = {
    "Media Integrations": "hub",
    "ARR Integrations": "dns",
    "Paths":        "folder",
    "Library sync": "sync",
    "Calendar":     "calendar_month",
    "Playback":     "play_circle",
    "Advanced":     "tune",
  };

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

  const failedConnectionTests = Object.entries(testResults).filter(([, result]) => result && !result.ok);

  function renderStandardField(field: SettingsField) {
    if (HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key)) return null;
    const value = props.values[field.key];
    const test = testResults[field.key];
    const testTarget = URL_TEST_TARGET[field.key];

    return (
      <div key={field.key} className="px-6 py-5">
        <div className="flex items-start gap-3 mb-2">
          <div className="flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-semibold text-white font-headline">{field.label}</span>
              {field.required && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase" style={{ backgroundColor: alphaColor(accent.hex, 0.3), color: accent.text }}>Required</span>}
              {field.secret && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-[#252e3a] text-slate-400">Secret</span>}
              {field.restart_required && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-orange-600/30 text-orange-300">Restart Required</span>}
            </div>
            {field.description && <p className="text-xs text-slate-500 mt-1">{field.description}</p>}
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
            className={`w-full max-w-xl bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)}`}
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
              className={`flex-1 bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)}`}
              type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
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

        {test && (
          <div className={`mt-2 flex items-center gap-1.5 text-xs ${test.ok ? "text-green-400" : "text-red-400"}`}>
            <span className="material-symbols-outlined" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
            {test.message}
          </div>
        )}
      </div>
    );
  }

  return (
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

      <div className="flex gap-5">
        {/* Section sidebar */}
        <div className="flex-none w-52">
          <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden">
            {sectionNames.map(name => (
              <button key={name} type="button" onClick={() => props.onSectionChange(name)}
                className={`w-full flex items-center gap-3 px-4 py-3 text-sm text-left border-b border-[#424753]/20 last:border-b-0 transition-colors ${name === active.name ? "text-white border-l-2" : "text-slate-400 hover:text-slate-200 hover:bg-[#1e2430]"}`}
                style={name === active.name ? { backgroundColor: alphaColor(accent.hex, 0.15), borderLeftColor: accent.hex } : undefined}>
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>{SECTION_ICONS[name] || "settings"}</span>
                {name}
              </button>
            ))}
          </div>
        </div>

        {/* Active section fields */}
        <div className="flex-1 min-w-0">
          <div className="bg-[#171c22] rounded-xl border border-[#424753]/40 overflow-hidden">
            <div className="px-6 py-4 border-b border-[#424753]/30">
              <h2 className="text-base font-bold text-white font-headline">{active.name}</h2>
            </div>
            {failedConnectionTests.length ? (
              <div className="mx-6 mt-5 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3">
                <div className="text-[11px] font-headline uppercase tracking-widest text-red-300">Connection Warning</div>
                <p className="mt-1 text-xs text-red-200/90">One or more configured services could not be reached. Review the failed test results below and correct the URL or API key.</p>
              </div>
            ) : null}
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
                <>
                  {active.fields.map((field) => renderStandardField(field))}
                </>
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
                      <p className="text-xs text-slate-500 mt-1">Configure up to 2 Radarr and 2 Sonarr instances. These entries power webhook labels and instance-aware routing.</p>
                    </div>
                    <ArrInstancesEditor
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
                    <div className="rounded-lg border border-[#424753]/30 bg-[#0f1419] p-4 space-y-4">
                      <div>
                        <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Placeholder Search Behavior</h3>
                        <p className="text-xs text-slate-400">When a placeholder plays, Placeholdarr triggers a search in the corresponding ARR app. Choose which instance to search.</p>
                      </div>
                      <div className="space-y-4">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                          <select
                            className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                          <p className="text-[11px] text-slate-500 mt-1.5">
                            {canUseRadarrSecondaryBehavior
                              ? (({ primary: "Searches your primary (standard) Radarr instance.", secondary: "Searches your secondary (4K) Radarr instance.", both: "Searches both Radarr instances — ensures full coverage." } as Record<string, string>)[String(props.values.MOVIE_PLACEHOLDER_SEARCH_MODE ?? "both")] ?? "Searches both Radarr instances.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                        <div className="border-t border-[#424753]/20" />
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                          <select
                            className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                          <p className="text-[11px] text-slate-500 mt-1.5">
                            {canUseSonarrSecondaryBehavior
                              ? (({ primary: "Searches your primary (standard) Sonarr instance.", secondary: "Searches your secondary (4K) Sonarr instance.", both: "Searches both Sonarr instances — ensures full coverage." } as Record<string, string>)[String(props.values.TV_PLACEHOLDER_SEARCH_MODE ?? "both")] ?? "Searches both Sonarr instances.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                      </div>
                    </div>
                    <div className="mt-3 rounded-lg border border-[#424753]/30 bg-[#0f1419] p-4 space-y-4">
                      <div>
                        <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Real-File Search Behavior</h3>
                        <p className="text-xs text-slate-400">When an actual media file is played, choose how Placeholdarr routes the playback-triggered ARR search.</p>
                      </div>
                      <div className="space-y-4">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                          <select
                            className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                          <p className="text-[11px] text-slate-500 mt-1.5">
                            {canUseRadarrSecondaryBehavior
                              ? (({ match: "Uses the movie file path to determine which Radarr instance should be searched.", primary: "Always searches your primary (standard) Radarr instance.", secondary: "Always searches your secondary (4K) Radarr instance.", both: "Searches both Radarr instances." } as Record<string, string>)[String(props.values.MOVIE_PLAYBACK_INSTANCE_MODE ?? "match")] ?? "Uses the movie file path to determine which Radarr instance should be searched.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                        <div className="border-t border-[#424753]/20" />
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                          <select
                            className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                          <p className="text-[11px] text-slate-500 mt-1.5">
                            {canUseSonarrSecondaryBehavior
                              ? (({ match: "Uses the TV file path to determine which Sonarr instance should be searched.", primary: "Always searches your primary (standard) Sonarr instance.", secondary: "Always searches your secondary (4K) Sonarr instance.", both: "Searches both Sonarr instances." } as Record<string, string>)[String(props.values.TV_PLAYBACK_INSTANCE_MODE ?? "match")] ?? "Uses the TV file path to determine which Sonarr instance should be searched.")
                              : "Not applicable, no second instance set up."}
                          </p>
                        </div>
                        <div className="border-t border-[#424753]/20" />
                        <div className="flex items-center justify-between gap-3">
                          <div>
                            <div className="text-xs font-semibold text-slate-300">Fallback search</div>
                            <div className="text-xs text-slate-500">
                              {fallbackUnnecessaryBecauseAllBoth
                                ? "Fallback is not needed because every unlocked search behavior already searches both instances."
                                : canUseAnySecondaryBehavior
                                ? "If the selected ARR instance has the item but the search does not resolve, Placeholdarr waits the timeout and then searches the other enabled instance. If the item is not in ARR at all (missing/deleted rows), fallback is immediate."
                                : "Not applicable, no second instance set up."}
                            </div>
                          </div>
                          <label className="flex items-center gap-3 cursor-pointer select-none w-fit shrink-0">
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
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1.5">Fallback timeout (minutes)</label>
                          {canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? (
                            <input
                              className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)}`}
                              type="number"
                              min={1}
                              value={String(props.values.PLAYBACK_FALLBACK_TIMEOUT_MINUTES ?? "")}
                              onChange={(e) => props.onValueChange("PLAYBACK_FALLBACK_TIMEOUT_MINUTES", e.target.value)}
                            />
                          ) : fallbackUnnecessaryBecauseAllBoth ? (
                            <input
                              className="w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                              type="text"
                              value="Not needed because all unlocked behaviors already search both instances."
                              disabled
                            />
                          ) : canUseAnySecondaryBehavior ? (
                            <input
                              className="w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                              type="text"
                              value="Enable fallback search to set timeout."
                              disabled
                            />
                          ) : (
                            <input
                              className="w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                              type="text"
                              value="Not applicable, no second instance set up."
                              disabled
                            />
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </>
              ) : active.name === "Playback" ? (
                <>{active.fields.map((field) => renderStandardField(field))}</>
              ) : (
                active.fields.map((field) => renderStandardField(field))
              )}
            </div>
          </div>
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
  onNext: () => void;
  onChange: (key: string, value: unknown) => void;
  onSave: () => Promise<void>;
  onTestConnection: (input: { service: "plex" | "jellyfin" | "emby" | "radarr" | "sonarr"; urlKey: string; credentialKey: string }) => Promise<{ ok: boolean; message: string }>;
  onPartialSave?: (result: any, partialValues: Record<string, unknown>) => Promise<void> | void;
}) {
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [arrPrimaryTestStatus, setArrPrimaryTestStatus] = useState<{ radarr: boolean; sonarr: boolean }>({ radarr: false, sonarr: false });
  const [arrSecondaryTestStatus, setArrSecondaryTestStatus] = useState<{ radarr: boolean; sonarr: boolean }>({ radarr: false, sonarr: false });
  const step = WIZARD_STEPS[props.stepIndex];
  const guidance = WIZARD_STEP_GUIDANCE[step.key];
  const accent = getBrandAccent(props.brand, props.themeMode);
  const arrInstances = parseArrInstancesFromValues(props.values);
  const hasArrInstances = arrInstances.length > 0;
  const hasRadarrSecondary = arrInstances.filter((item) => item.arr_type === "radarr").length > 1;
  const hasSonarrSecondary = arrInstances.filter((item) => item.arr_type === "sonarr").length > 1;
  const uiHasRadarrSecondary = hasRadarrSecondary || Boolean(props.values.WIZARD_RADARR_SECONDARY_ENABLED);
  const uiHasSonarrSecondary = hasSonarrSecondary || Boolean(props.values.WIZARD_SONARR_SECONDARY_ENABLED);
  const canUseRadarrSecondaryBehavior = uiHasRadarrSecondary && arrSecondaryTestStatus.radarr;
  const canUseSonarrSecondaryBehavior = uiHasSonarrSecondary && arrSecondaryTestStatus.sonarr;
  const canUseAnySecondaryBehavior = canUseRadarrSecondaryBehavior || canUseSonarrSecondaryBehavior;
  const hasLibraryRoot = String(props.values.LIBRARY_ROOT ?? "").trim().length > 0;
  const enabledMediaServices = [
    { enabled: Boolean(props.values.ENABLE_PLEX), urlKey: "PLEX_URL" },
    { enabled: Boolean(props.values.ENABLE_JELLYFIN), urlKey: "JELLYFIN_URL" },
    { enabled: Boolean(props.values.ENABLE_EMBY), urlKey: "EMBY_URL" },
  ];
  const hasConfirmedMediaConnection = enabledMediaServices.some((item) => item.enabled && Boolean(testResults[item.urlKey]?.ok));
  const hasConfirmedArrConnection = arrPrimaryTestStatus.radarr || arrPrimaryTestStatus.sonarr;
  const keys = fieldsForWizardStep(step.key, props.payload.sections);
  const fields = props.payload.sections.flatMap((section) => section.fields).filter((f) => keys.includes(f.key));
  const [stepSaving, setStepSaving] = useState(false);
  const [stepError, setStepError] = useState<string | null>(null);

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
    if (step.key === "webhooks") return hasArrInstances;
    return true;
  })();

  useEffect(() => {
    if (fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH)) {
      props.onChange("ENABLE_PLAYBACK_FALLBACK_SEARCH", false);
    }
  }, [fallbackUnnecessaryBecauseAllBoth, props, props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH]);

  useEffect(() => {
    setTestResults({});
  }, [props.stepIndex]);

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
    if (HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key)) return null;
    const test = testResults[field.key];
    const testTarget = URL_TEST_TARGET[field.key];
    const focus = getBrandFocusClass(props.brand, props.themeMode);
    return (
      <div key={field.key}>
        <label className="block text-sm font-semibold text-white font-headline mb-1">{field.label}</label>
        {field.description && <p className="text-xs text-slate-500 mb-2">{field.description}</p>}
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
            className={`w-full bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${focus}`}
            value={String(props.values[field.key] ?? field.options[0]?.value ?? "")}
            onChange={(e) => props.onChange(field.key, e.target.value)}
          >
            {field.options.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        ) : (
          <div className="flex gap-2">
            <input
              className={`flex-1 min-w-0 bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 outline-none transition-colors ${focus}`}
              type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
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
        {test && (
          <div className={`mt-2 flex items-center gap-1.5 text-xs ${test.ok ? "text-green-400" : "text-red-400"}`}>
            <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
            <span>{test.message}</span>
          </div>
        )}
      </div>
    );
  }

  const failedConnectionTests = Object.entries(testResults).filter(([, result]) => result && !result.ok);

  return (
    <div className="fixed inset-0 z-50 bg-[#0f1419]/80 backdrop-blur-sm flex items-center justify-center p-6">
      <div className="w-full max-w-2xl bg-[#171c22] border border-[#424753]/40 rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="px-8 pt-8 pb-6 border-b border-[#424753]/30">
          <div className="text-[10px] font-headline uppercase tracking-widest mb-1" style={{ color: accent.icon }}>Initial Configuration</div>
          <h2 className="text-2xl font-black text-white font-headline tracking-tight">Integration Setup Wizard</h2>
          <p className="text-sm text-slate-400 mt-1">Complete setup to unlock full dashboard access.</p>
        </div>

        {/* Stepper */}
        <div className="px-8 py-5 border-b border-[#424753]/30">
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
        <div className="px-8 py-6 overflow-y-auto max-h-[50vh]">
          {step.key !== "media" && step.key !== "webhooks" ? (
            <div className="mb-5 rounded-xl border border-[#424753]/40 bg-[#0f1419] px-4 py-3">
              <div className="text-[11px] font-headline uppercase tracking-widest" style={{ color: accent.icon }}>{guidance.title}</div>
              <p className="mt-1 text-xs text-slate-400">{guidance.detail}</p>
            </div>
          ) : null}
          {failedConnectionTests.length ? (
            <div className="mb-5 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3">
              <div className="text-[11px] font-headline uppercase tracking-widest text-red-300">Connection Warning</div>
              <p className="mt-1 text-xs text-red-200/90">One or more configured services could not be reached. Review the failed test results in this step and correct the URL or API key.</p>
            </div>
          ) : null}
          {step.key === "paths" ? (
            <LibraryPathsForm
              fields={fields}
              values={props.values}
              brand={props.brand}
              themeMode={props.themeMode}
              accent={accent}
              layout="wizard"
              onValueChange={props.onChange}
            />
          ) : step.key === "media" ? (() => {
            const focus = getBrandFocusClass(props.brand, props.themeMode);
            const mediaCards = [
              {
                id: "plex",
                title: "Plex",
                enabledKey: "ENABLE_PLEX",
                note: "Tautulli is required for Plex playback webhooks and playback-aware routing.",
                keys: ["PLEX_URL", "PLEX_TOKEN", "PLEX_MOVIE_SECTION_ID", "PLEX_TV_SECTION_ID", "TAUTULLI_INSTANCE_KEY"],
              },
              {
                id: "jellyfin",
                title: "Jellyfin",
                enabledKey: "ENABLE_JELLYFIN",
                note: "Optional playback webhook support can be configured in the Webhook step.",
                keys: ["JELLYFIN_URL", "JELLYFIN_TOKEN", "JELLYFIN_INSTANCE_KEY"],
              },
              {
                id: "emby",
                title: "Emby",
                enabledKey: "ENABLE_EMBY",
                note: "Optional playback webhook support can be configured in the Webhook step.",
                keys: ["EMBY_URL", "EMBY_TOKEN", "EMBY_INSTANCE_KEY"],
              },
            ] as const;
            const fieldByKey = new Map(fields.map((f) => [f.key, f]));

            return (
              <div className="space-y-4">
                {mediaCards.map((card) => {
                  const enabled = Boolean(props.values[card.enabledKey]);
                  const availableFields = card.keys
                    .map((key) => fieldByKey.get(key))
                    .filter(Boolean) as SettingsField[];

                  return (
                    <div key={card.id} className={`rounded-xl border border-[#424753]/40 bg-[#0f1419] p-4 transition-opacity ${enabled ? "" : "opacity-55"}`}>
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <h3 className="text-sm font-semibold text-white font-headline">{card.title}</h3>
                          <p className="mt-1 text-xs text-slate-400">{card.note}</p>
                        </div>
                        <label className="flex items-center gap-3 cursor-pointer select-none w-fit shrink-0">
                          <div
                            className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${enabled ? "" : "bg-[#252e3a]"}`}
                            style={enabled ? { backgroundColor: accent.hex } : undefined}
                            onClick={() => props.onChange(card.enabledKey, !enabled)}
                          >
                            <div className={`w-4 h-4 rounded-full bg-white shadow transition-transform ${enabled ? "translate-x-5" : "translate-x-0"}`} />
                          </div>
                          <span className="text-sm text-slate-300">{enabled ? "Enabled" : "Disabled"}</span>
                        </label>
                      </div>
                      <div className="mt-4 space-y-3">
                        {availableFields.map((field) => {
                          if (field.key === card.enabledKey) return null;
                          if (HIDDEN_PLAYBACK_INTERNAL_KEYS.has(field.key)) return null;
                          const value = props.values[field.key];
                          const test = testResults[field.key];
                          const testTarget = URL_TEST_TARGET[field.key];
                          return (
                            <div key={field.key}>
                              <label className="block text-xs font-semibold text-slate-300 mb-1">{field.label}</label>
                              {getPlexLibraryIdNote(field.key) ? <p className="text-[11px] text-slate-500 mb-1.5">{getPlexLibraryIdNote(field.key)}</p> : null}
                              {field.type === "bool" ? (
                                <label className="flex items-center gap-3 cursor-pointer select-none w-fit">
                                  <div
                                    className={`w-10 h-5 rounded-full transition-colors flex items-center px-0.5 ${Boolean(value) ? "" : "bg-[#252e3a]"}`}
                                    style={Boolean(value) ? { backgroundColor: accent.hex } : undefined}
                                    onClick={() => enabled && props.onChange(field.key, !Boolean(value))}
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
                                    onChange={(e) => props.onChange(field.key, e.target.value)}
                                    disabled={!enabled}
                                  />
                                  {testTarget ? (
                                    <button
                                      type="button"
                                      onClick={() => runTest(field)}
                                      disabled={!enabled}
                                      className="flex items-center gap-1.5 px-3 py-2 bg-[#252e3a] hover:bg-[#30353b] border border-[#424753]/40 rounded-lg text-xs text-slate-300 font-headline uppercase tracking-wider transition-colors whitespace-nowrap shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
                                    >
                                      <span className="material-symbols-outlined" style={{ fontSize: 14 }}>wifi</span>
                                      Test
                                    </button>
                                  ) : null}
                                </div>
                              )}
                              {test ? (
                                <div className={`mt-2 flex items-center gap-1.5 text-xs ${test.ok ? "text-green-400" : "text-red-400"}`}>
                                  <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }}>{test.ok ? "check_circle" : "error"}</span>
                                  <span>{test.message}</span>
                                </div>
                              ) : null}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })() : step.key === "arr" ? (
            <div className="space-y-4">
              <ArrInstancesEditor
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
              <div className="rounded-lg border border-[#424753]/30 bg-[#0f1419] p-4 space-y-4">
                <div>
                  <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Placeholder Search Behavior</h3>
                  <p className="text-xs text-slate-400">Choose which ARR instance to search when a placeholder is played.</p>
                </div>
                <div className="space-y-4">
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                    <select
                      className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                  <div className="border-t border-[#424753]/20" />
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                    <select
                      className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
              <div className="rounded-lg border border-[#424753]/30 bg-[#0f1419] p-4 space-y-4">
                <div>
                  <h3 className="text-xs font-semibold text-white font-headline uppercase tracking-wider mb-1">Real-File Search Behavior</h3>
                  <p className="text-xs text-slate-400">When a real media file is played, choose how Placeholdarr routes ARR searches.</p>
                </div>
                <div className="space-y-4">
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">Movies (Radarr)</label>
                    <select
                      className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseRadarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                  <div className="border-t border-[#424753]/20" />
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">TV Shows (Sonarr)</label>
                    <select
                      className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)} ${canUseSonarrSecondaryBehavior ? "" : "opacity-60 cursor-not-allowed"}`}
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
                  <div className="border-t border-[#424753]/20" />
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-xs font-semibold text-slate-300">Fallback search</div>
                      <div className="text-xs text-slate-500">
                        {fallbackUnnecessaryBecauseAllBoth
                          ? "Fallback is not needed because every unlocked search behavior already searches both instances."
                          : canUseAnySecondaryBehavior
                          ? "If the selected ARR instance has the item but the search does not resolve, Placeholdarr waits the timeout and then searches the other enabled instance. If the item is not in ARR at all (missing/deleted rows), fallback is immediate."
                          : "Not applicable, no second instance set up."}
                      </div>
                    </div>
                    <label className="flex items-center gap-3 cursor-pointer select-none w-fit shrink-0">
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
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 mb-1.5">Fallback timeout (minutes)</label>
                    {canUseAnySecondaryBehavior && !fallbackUnnecessaryBecauseAllBoth && Boolean(props.values.ENABLE_PLAYBACK_FALLBACK_SEARCH) ? (
                      <input
                        className={`w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-200 outline-none transition-colors ${getBrandFocusClass(props.brand, props.themeMode)}`}
                        type="number"
                        min={1}
                        value={String(props.values.PLAYBACK_FALLBACK_TIMEOUT_MINUTES ?? 30)}
                        onChange={(e) => props.onChange("PLAYBACK_FALLBACK_TIMEOUT_MINUTES", e.target.value)}
                      />
                    ) : fallbackUnnecessaryBecauseAllBoth ? (
                      <input
                        className="w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                        type="text"
                        value="Not needed because all unlocked behaviors already search both instances."
                        disabled
                      />
                    ) : canUseAnySecondaryBehavior ? (
                      <input
                        className="w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                        type="text"
                        value="Enable fallback search to set timeout."
                        disabled
                      />
                    ) : (
                      <input
                        className="w-full max-w-xs bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2 text-sm text-slate-500 opacity-60 cursor-not-allowed"
                        type="text"
                        value="Not applicable, no second instance set up."
                        disabled
                      />
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : step.key === "webhooks" ? (() => {
            const instances = arrInstances;
            const webhookBaseUrl = window.location.origin;
            const hasArrInstances = instances.length > 0;
            const tautulliPayloadTemplate = `{
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
            const jellyfinPayloadTemplate = `{
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

            return (
              <div className="space-y-6">
                {!hasArrInstances && (
                  <div className="rounded-xl border border-yellow-500/30 bg-yellow-500/10 px-4 py-3">
                    <div className="flex items-start gap-2">
                      <span className="material-symbols-outlined shrink-0 text-yellow-500" style={{ fontSize: 18 }}>warning</span>
                      <div>
                        <h3 className="text-sm font-semibold text-yellow-200">No ARR instances configured</h3>
                        <p className="mt-1 text-xs text-yellow-300/80">Configure at least one Radarr or Sonarr instance in ARR Services, then continue to enable webhook setup.</p>
                      </div>
                    </div>
                  </div>
                )}

                {hasArrInstances && (
                  <>
                    {/* Trigger Setup Instructions */}
                    <div>
                      <h3 className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-3 pb-2 border-b border-[#424753]/30">Setup Steps for Each Service</h3>
                      <div className="space-y-4">
                        {instances.map((instance) => {
                          const service = ARR_WEBHOOK_SERVICES.services.find((s) => s.id === instance.arr_type);
                          if (!service) return null;
                          const liveInstanceKey = normalizeInstanceKey(inferDefaultKey(String(instance.label || ""), instance.arr_type));
                          const webhookUrl = `${webhookBaseUrl}/webhook?instance=${encodeURIComponent(liveInstanceKey)}`;
                          return (
                            <div key={`steps-${instance.id}`} className="rounded-lg border border-[#424753]/40 bg-[#0f1419] p-4">
                              <div className="font-semibold text-sm text-white mb-2 flex items-center gap-2">
                                <span style={{ color: service.color }}>●</span>
                                {service.name} {instance.label ? `- ${instance.label}` : ""}
                              </div>
                              <ol className="text-xs text-slate-400 space-y-1.5 list-decimal list-inside">
                                <li>Go to Settings → Webhooks → Create New Webhook</li>
                                <li>Set URL to <span className="font-mono text-slate-300">{webhookUrl}</span></li>
                                <li>Select required events:</li>
                              </ol>
                              <div className="mt-2 ml-4 space-y-1">
                                {service.triggers.filter((t) => t.required).map((trigger) => (
                                  <div key={`${instance.id}-${trigger.event}`} className="flex items-center gap-2 text-xs text-slate-300">
                                    <span className="material-symbols-outlined" style={{ fontSize: 14 }}>check_box_outline_blank</span>
                                    <span>{trigger.displayName}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    {/* Playback Webhooks (if applicable) */}
                    {(Boolean(props.values.ENABLE_PLEX) || Boolean(props.values.ENABLE_JELLYFIN) || Boolean(props.values.ENABLE_EMBY)) && (
                      <div>
                        <h3 className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-3 pb-2 border-b border-[#424753]/30">Playback Source Webhooks</h3>
                        <div className="space-y-3">
                          {Boolean(props.values.ENABLE_PLEX) && (
                            <div className="rounded-lg border border-[#424753]/40 bg-[#0f1419] p-4">
                              <div className="flex items-start justify-between gap-3">
                                <div className="flex-1">
                                  <div className="text-xs font-semibold text-slate-300">Tautulli</div>
                                  <div className="mt-1 text-[11px] text-slate-500">Required for Plex playback signals</div>
                                  <div className="mt-2 text-[11px] text-slate-400 font-mono break-all bg-[#0a0d11] rounded px-2 py-1.5">{`${webhookBaseUrl}/webhook?instance=${encodeURIComponent(String(props.values.TAUTULLI_INSTANCE_KEY || "tautulli"))}`}</div>
                                </div>
                                <button
                                  type="button"
                                  onClick={() => {
                                    navigator.clipboard.writeText(`${webhookBaseUrl}/webhook?instance=${encodeURIComponent(String(props.values.TAUTULLI_INSTANCE_KEY || "tautulli"))}`);
                                    alert("Tautulli webhook URL copied to clipboard!");
                                  }}
                                  className="mt-0.5 shrink-0 px-3 py-1.5 bg-[#252e3a] border border-[#424753]/40 hover:border-[#424753]/80 text-slate-300 hover:text-white rounded text-[10px] font-headline uppercase tracking-wider transition-colors"
                                >
                                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>content_copy</span>
                                </button>
                              </div>
                              <ol className="mt-3 text-[11px] text-slate-400 list-decimal list-inside space-y-1.5">
                                <li>Open Tautulli and go to Settings → Notification Agents.</li>
                                <li>Create a new Webhook notification agent.</li>
                                <li>Set Trigger to Playback Start and Payload Format to JSON.</li>
                                <li>Paste the payload template below and save.</li>
                              </ol>
                              <div className="mt-3">
                                <div className="text-[10px] font-headline uppercase tracking-wider text-slate-500">JSON Payload Template</div>
                                <pre className="mt-1.5 overflow-x-auto rounded bg-[#0a0d11] border border-[#424753]/40 p-3 text-[11px] leading-relaxed text-slate-300 font-mono">
                                  <code>{tautulliPayloadTemplate}</code>
                                </pre>
                              </div>
                            </div>
                          )}
                          {Boolean(props.values.ENABLE_JELLYFIN) && (
                            <div className="rounded-lg border border-[#424753]/40 bg-[#0f1419] p-4">
                              <div className="flex items-start justify-between gap-3">
                                <div className="flex-1">
                                  <div className="text-xs font-semibold text-slate-300">Jellyfin</div>
                                  <div className="mt-1 text-[11px] text-slate-500">Use the Webhook plugin with a custom JSON payload template.</div>
                                  <div className="mt-2 text-[11px] text-slate-400 font-mono break-all bg-[#0a0d11] rounded px-2 py-1.5">{`${webhookBaseUrl}/webhook?instance=${encodeURIComponent(String(props.values.JELLYFIN_INSTANCE_KEY || "jellyfin"))}`}</div>
                                </div>
                                <button
                                  type="button"
                                  onClick={() => {
                                    navigator.clipboard.writeText(`${webhookBaseUrl}/webhook?instance=${encodeURIComponent(String(props.values.JELLYFIN_INSTANCE_KEY || "jellyfin"))}`);
                                    alert("Jellyfin webhook URL copied to clipboard!");
                                  }}
                                  className="mt-0.5 shrink-0 px-3 py-1.5 bg-[#252e3a] border border-[#424753]/40 hover:border-[#424753]/80 text-slate-300 hover:text-white rounded text-[10px] font-headline uppercase tracking-wider transition-colors"
                                >
                                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>content_copy</span>
                                </button>
                              </div>
                              <ol className="mt-3 text-[11px] text-slate-400 list-decimal list-inside space-y-1.5">
                                <li>In Jellyfin, install the Webhook plugin (Dashboard → Plugins → Catalog) if needed.</li>
                                <li>Go to Dashboard → Plugins → Webhook and click Add Webhook.</li>
                                <li>Set Events to include Playback Start and Content Type to application/json.</li>
                                <li>Paste the payload template below, then save the webhook.</li>
                              </ol>
                              <div className="mt-3">
                                <div className="text-[10px] font-headline uppercase tracking-wider text-slate-500">JSON Payload Template</div>
                                <pre className="mt-1.5 overflow-x-auto rounded bg-[#0a0d11] border border-[#424753]/40 p-3 text-[11px] leading-relaxed text-slate-300 font-mono">
                                  <code>{jellyfinPayloadTemplate}</code>
                                </pre>
                              </div>
                            </div>
                          )}
                          {Boolean(props.values.ENABLE_EMBY) && (
                            <div className="rounded-lg border border-[#424753]/40 bg-[#0f1419] p-4">
                              <div className="flex items-start justify-between gap-3">
                                <div className="flex-1">
                                  <div className="text-xs font-semibold text-slate-300">Emby</div>
                                  <div className="mt-1 text-[11px] text-slate-500">Set a playback-start webhook so Placeholdarr receives media start events.</div>
                                  <div className="mt-2 text-[11px] text-slate-400 font-mono break-all bg-[#0a0d11] rounded px-2 py-1.5">{`${webhookBaseUrl}/webhook?instance=${encodeURIComponent(String(props.values.EMBY_INSTANCE_KEY || "emby"))}`}</div>
                                </div>
                                <button
                                  type="button"
                                  onClick={() => {
                                    navigator.clipboard.writeText(`${webhookBaseUrl}/webhook?instance=${encodeURIComponent(String(props.values.EMBY_INSTANCE_KEY || "emby"))}`);
                                    alert("Emby webhook URL copied to clipboard!");
                                  }}
                                  className="mt-0.5 shrink-0 px-3 py-1.5 bg-[#252e3a] border border-[#424753]/40 hover:border-[#424753]/80 text-slate-300 hover:text-white rounded text-[10px] font-headline uppercase tracking-wider transition-colors"
                                >
                                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>content_copy</span>
                                </button>
                              </div>
                              <ol className="mt-3 text-[11px] text-slate-400 list-decimal list-inside space-y-1.5">
                                <li>In Emby, go to Settings → Notifications.</li>
                                <li>Add or edit a webhook notification and set the URL above.</li>
                                <li>Enable the playback-start event you want Placeholdarr to process.</li>
                                <li>Save the webhook.</li>
                              </ol>
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
            );
          })() : !fields.length ? (
            <div className="text-center text-slate-500 text-sm py-8">No fields for this step.</div>
          ) : step.key === "behavior" ? (
            <div className="space-y-6">
              {BEHAVIOR_WIZARD_SECTIONS.map((sectionName) => {
                const secFields = fields.filter((f) => f.section === sectionName);
                if (!secFields.length) return null;
                return (
                  <div key={sectionName}>
                    <h3 className="text-[10px] font-headline uppercase tracking-widest text-slate-500 mb-3 pb-2 border-b border-[#424753]/30">{sectionName}</h3>
                    <div className="space-y-5">{secFields.map((field) => wizardFieldRow(field))}</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="space-y-5">{fields.map((field) => wizardFieldRow(field))}</div>
          )}
        </div>

        {/* Actions */}
        <div className="px-8 py-5 border-t border-[#424753]/30 flex items-center justify-end">
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
                {stepSaving ? "Saving..." : "Save & Continue"}
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
  );
}
function getTabFromPath(pathname: string): DashboardTab {
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

function formatCalendarItemMeta(item: CalendarDay["items"][number]) {
  const bits: Array<{ label: string; value: string }> = [
    { label: "Date", value: formatShortDate(item.release_date) },
  ];

  if (item.release_type_label) {
    bits.push({ label: "Release", value: item.release_type_label + (item.release_type_preferred ? "" : " fallback") });
  }
  if (typeof item.days_until === "number") {
    const relative = item.days_until === 0
      ? "Today"
      : item.days_until === 1
        ? "1 day"
        : `${item.days_until} days`;
    bits.push({ label: "Countdown", value: relative });
  }
  if (item.reason) {
    bits.push({ label: "Reason", value: item.reason });
  }
  if (item.status) {
    bits.push({ label: "Status", value: item.status });
  }

  return bits;
}

function formatShortDate(iso: string) {
  // Handle date-only strings (YYYY-MM-DD) as local dates to avoid timezone shifts
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const [y, m, d] = iso.split("-").map(Number);
    const date = new Date(y, m - 1, d);
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;

  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
  const playback = map.Playback || [];
  const advanced = map.Advanced || [];
  const arrBehaviorFromArrIntegrations = [...arrIntegrations].filter((k) => ARR_BEHAVIOR_KEYS.has(k));
  const arrBehaviorFromPlayback = playback.filter((k) => ARR_BEHAVIOR_KEYS.has(k));
  const arrBehavior = arrBehaviorFromArrIntegrations.length ? arrBehaviorFromArrIntegrations : arrBehaviorFromPlayback;
  const playbackNonArrSearch = playback.filter((k) => !ARR_BEHAVIOR_KEYS.has(k));

  if (stepKey === "paths") return [...paths];
  if (stepKey === "webhooks") return [];
  if (stepKey === "arr") {
    return [
      ...[...arrIntegrations].filter((k) => k.startsWith("RADARR") || k.startsWith("SONARR") || k === "ARR_INSTANCES_JSON"),
      ...arrBehavior,
    ];
  }
  if (stepKey === "media") {
    return [...mediaIntegrations];
  }
  return [...librarySync, ...calendar, ...playbackNonArrSearch, ...advanced];
}
