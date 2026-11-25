import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ThemeMode } from "../brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import {
  checkCollectionTitleConflicts,
  getCollectionBuilderMeta,
  previewCollectionDefinition,
  type CollectionTitleConflict,
  type RecipeWritePayload,
} from "../api/collections";
import type {
  CollectionActiveWindow,
  CollectionBuilderMeta,
  CollectionDefinition,
  CollectionPreviewResponse,
  CollectionRecipe,
  CollectionAutoAddConfig,
  CollectionSetConfig,
  CollectionSortOption,
  CollectionVisibleOn,
  PlexSectionOption,
} from "../types/api";
import {
  parseTaskSchedule,
  scheduleFromLegacyHours,
  TaskScheduleEditor,
  type TaskScheduleValue,
} from "../settings/TaskScheduleEditor";
import { ToggleSwitch } from "../ToggleSwitch";
import { getCollectionTheme } from "./collectionTheme";
import { CollectionShelfContentsSection } from "./CollectionShelfContentsSection";
import { buildAutoAddPayload, isCollectionAutoAddReady } from "./collectionAutoAddUtils";
import { pickerOptionsForCategory, starterValuesForCategory } from "./collectionSetStarterValues";
import { CollectionValueMultiSelect, mergeUniqueSorted } from "./CollectionValueMultiSelect";
import { collectionArrangeSortOptions } from "./collectionArrangeOptions";
import { minItemsVersusCapsConflict } from "./collectionCapLimits";
import { managedRoleLabel, selectablePlexSections } from "./managedSections";
import { BuilderSection } from "./next/BuilderSection";
import { BuilderShell } from "./next/BuilderShell";
import { ChildCollectionList } from "./next/ChildCollectionList";
import { RecipePreviewResultView } from "./next/RecipePreviewResultView";
import { ChildSwitcher } from "./next/ChildSwitcher";
import { useDockPrefs } from "./next/dockPrefs";
import { buildFilmstrip, filmstripForLibrary } from "./next/filmstrip";
import { PlexRowMock } from "./next/PlexRowMock";
import { PreviewDock } from "./next/PreviewDock";
import { PreviewLibraryChips } from "./next/PreviewLibraryChips";
import { PreviewSheet } from "./next/PreviewSheet";
import { useRecipePreview } from "./next/useRecipePreview";
import { StickyActionBar } from "./next/StickyActionBar";

type SetCategory = CollectionSetConfig["category"];

const CATEGORY_OPTIONS: {
  value: SetCategory;
  label: string;
  description: string;
  defaultPattern: string;
  movieOnly?: boolean;
  showOnly?: boolean;
}[] = [
  {
    value: "genre",
    label: "Genre",
    description: "One collection per genre (TMDB list plus any extras in your catalog).",
    defaultPattern: "Genre · {value}",
  },
  {
    value: "decade",
    label: "Decade",
    description: "One collection per decade from title years (1990s, 2000s, …).",
    defaultPattern: "Decade · {value}",
  },
  {
    value: "content_rating",
    label: "Content rating",
    description: "One collection per certification / age rating (PG-13, TV-MA, …). Not critic scores.",
    defaultPattern: "Rated · {value}",
  },
  {
    value: "studio",
    label: "Studio",
    description:
      "One Plex collection per studio. Pick from the curated list, your catalog, or add a custom name.",
    defaultPattern: "Studio · {value}",
    movieOnly: true,
  },
  {
    value: "network",
    label: "Network",
    description:
      "One Plex collection per network. Pick from the curated list, your catalog, or add a custom name.",
    defaultPattern: "Network · {value}",
    showOnly: true,
  },
  {
    value: "tag",
    label: "Tag",
    description: "One collection per Radarr/Sonarr tag. Use tags as browsing buckets (Kids, Christmas, …).",
    defaultPattern: "Tag · {value}",
  },
  {
    value: "release_timing",
    label: "Release timing",
    description: "Upcoming and recently released collections. Pick which release date to base them on.",
    defaultPattern: "{value}",
  },
];

const RELEASE_TIMING_PRESETS = [
  { id: "upcoming", label: "Upcoming" },
  { id: "this_week", label: "Released this week" },
  { id: "this_month", label: "Released this month" },
  { id: "this_year", label: "Released this year" },
  { id: "this_decade", label: "Released this decade" },
];

const MOVIE_RELEASE_BASES: { value: string; label: string }[] = [
  { value: "theater", label: "Theatrical" },
  { value: "digital", label: "Digital" },
  { value: "physical", label: "Physical" },
];

const SHOW_RELEASE_BASES: { value: string; label: string }[] = [
  { value: "premiered", label: "Series premiere" },
  { value: "latest_episode", label: "Latest episode" },
  { value: "latest_season", label: "Latest season premiere" },
];

function defaultSetConfig(
  category: SetCategory = "genre",
  sectionType: "movie" | "show" = "movie",
): CollectionSetConfig {
  const meta = CATEGORY_OPTIONS.find((d) => d.value === category) ?? CATEGORY_OPTIONS[0];
  return {
    category: meta.value,
    selection_mode: "include",
    values: starterValuesForCategory(meta.value, sectionType),
    title_pattern: meta.defaultPattern,
    sort: "title",
    include_library: true,
    limit: null,
    min_items: 1,
    instance_key: null,
    release_basis: category === "release_timing" ? "theater" : null,
  };
}

function setDefinition(
  config: CollectionSetConfig,
  adoptExisting = false,
  visibleOn: CollectionVisibleOn | null = null,
  autoAdd?: CollectionAutoAddConfig | null,
): CollectionDefinition {
  return {
    mode: "collection_set",
    collection_set: config,
    sources: [{ type: "catalog" }],
    filters: [],
    limit: config.limit ?? null,
    sort: config.sort ?? "title",
    pins: { include: [], exclude: [] },
    ...(adoptExisting ? { adopt_existing: true } : {}),
    visible_on: {
      library: Boolean(visibleOn?.library),
      home: Boolean(visibleOn?.home),
      shared: Boolean(visibleOn?.shared),
    },
    ...(buildAutoAddPayload(autoAdd) ? { auto_add: buildAutoAddPayload(autoAdd) } : {}),
  };
}

function setCategoryOf(config: CollectionSetConfig | null | undefined): SetCategory | null {
  const raw = config?.category || config?.dimension;
  return raw || null;
}

const SOURCE_LABELS: Record<"tmdb" | "library" | "library_fallback" | "arr_tag", string> = {
  tmdb: "TMDB",
  library: "Library",
  library_fallback: "Library only (not on TMDB)",
  arr_tag: "Arr tag",
};

function autoAddScopeHint(category: SetCategory, _values: string[]): string {
  if (category === "tag") {
    return "Tag children always use your Arr tag list. Auto-add sends missing titles matched from that source only.";
  }
  return "Without auto-add, each child is catalog plus your sort. With auto-add on, children use TMDB sources (when configured) like hand-built recipes, so Missing from ARR can include titles not in Plex yet.";
}

function SetNumberInput(props: {
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  placeholder?: string;
  min?: number;
  max?: number;
  width?: number;
  fieldClassName: string;
}) {
  return (
    <input
      type="number"
      className={props.fieldClassName}
      style={{ width: props.width ?? 90 }}
      value={props.value ?? ""}
      min={props.min}
      max={props.max}
      placeholder={props.placeholder}
      onChange={(e) => {
        const raw = e.target.value;
        if (raw === "") {
          props.onChange(null);
          return;
        }
        const parsed = Number(raw);
        props.onChange(Number.isFinite(parsed) ? parsed : null);
      }}
    />
  );
}

function isCollectionSetRecipe(recipe: CollectionRecipe | null | undefined): boolean {
  if (!recipe?.definition) return false;
  return (
    recipe.definition.mode === "collection_set" || Boolean(setCategoryOf(recipe.definition.collection_set))
  );
}

export { isCollectionSetRecipe };

export function CollectionSetEditor(props: {
  recipe: CollectionRecipe | null;
  sections: PlexSectionOption[];
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
  saving: boolean;
  saveError: string | null;
  onSave: (payload: RecipeWritePayload) => void;
  onCancel: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  clockFormat?: string | null;
  uiVariant?: "classic" | "next";
  onOpenOrdering?: () => void;
}) {
  const useNextLayout = props.uiVariant === "next";
  const accentHex = props.accent.hex;
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const existing = props.recipe?.definition?.collection_set;

  const [name, setName] = useState(props.recipe?.name ?? "");
  const [sectionIds, setSectionIds] = useState<number[]>(() => {
    const extras = props.recipe?.plex_section_ids?.filter((id) => Number.isFinite(id) && id >= 1) ?? [];
    if (extras.length) return extras;
    return props.recipe?.plex_section_id ? [props.recipe.plex_section_id] : [];
  });
  const [followGlobalSchedule, setFollowGlobalSchedule] = useState(
    () => props.recipe?.run_schedule == null && props.recipe?.run_interval_hours == null,
  );
  const [runSchedule, setRunSchedule] = useState<TaskScheduleValue>(() => {
    if (props.recipe?.run_schedule && typeof props.recipe.run_schedule === "object") {
      return parseTaskSchedule(props.recipe.run_schedule, 24);
    }
    if (props.recipe?.run_interval_hours != null) {
      return scheduleFromLegacyHours(props.recipe.run_interval_hours);
    }
    return parseTaskSchedule({ enabled: true, mode: "daily", time: "03:00" }, 24);
  });
  const [activeWindow, setActiveWindow] = useState<CollectionActiveWindow | null>(
    props.recipe?.active_window ?? null,
  );
  const [adoptExisting, setAdoptExisting] = useState(Boolean(props.recipe?.definition?.adopt_existing));
  const [visibleOn, setVisibleOn] = useState<CollectionVisibleOn>(() => ({
    library: Boolean(props.recipe?.definition?.visible_on?.library),
    home: Boolean(props.recipe?.definition?.visible_on?.home),
    shared: Boolean(props.recipe?.definition?.visible_on?.shared),
  }));
  const [autoAdd, setAutoAdd] = useState<CollectionAutoAddConfig | null | undefined>(
    () => props.recipe?.definition?.auto_add ?? null,
  );
  const [nameCheck, setNameCheck] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ok" }
    | { status: "conflict"; conflicts: CollectionTitleConflict[] }
    | { status: "error"; message: string }
  >({ status: "idle" });
  const [setConfig, setSetConfig] = useState<CollectionSetConfig>(() => {
    const cat = setCategoryOf(existing);
    const type = (props.recipe?.plex_section_type ?? "movie") as "movie" | "show";
    if (!cat) return defaultSetConfig("genre", type);
    const { dimension: _legacyDim, ...rest } = existing as CollectionSetConfig;
    const savedValues = existing?.values ?? [];
    const isNew = !props.recipe;
    const values =
      savedValues.length > 0
        ? savedValues
        : isNew
          ? starterValuesForCategory(cat, type)
          : [];
    return {
      ...defaultSetConfig(cat, type),
      ...rest,
      category: cat,
      selection_mode: "include",
      values,
    };
  });

  const snapshot = useMemo(
    () =>
      JSON.stringify({
        name,
        sectionIds,
        runSchedule: followGlobalSchedule ? null : runSchedule,
        activeWindow,
        adoptExisting,
        visibleOn,
        autoAdd,
        setConfig: {
          category: setConfig.category,
          selection_mode: setConfig.selection_mode,
          values: setConfig.values,
          title_pattern: setConfig.title_pattern,
          sort: setConfig.sort,
          include_library: setConfig.include_library !== false,
          limit: setConfig.limit,
          min_items: setConfig.min_items,
          instance_key: setConfig.instance_key,
          release_basis: setConfig.release_basis,
        },
      }),
    [
      name,
      sectionIds,
      followGlobalSchedule,
      runSchedule,
      activeWindow,
      adoptExisting,
      visibleOn,
      autoAdd,
      setConfig,
    ],
  );
  const initialRef = useRef(snapshot);
  const dirty = snapshot !== initialRef.current;
  useEffect(() => {
    props.onDirtyChange?.(dirty);
  }, [dirty, props.onDirtyChange]);
  useEffect(() => {
    return () => props.onDirtyChange?.(false);
  }, [props.onDirtyChange]);

  useEffect(() => {
    setNameCheck({ status: "idle" });
  }, [sectionIds, setConfig.title_pattern, setConfig.category, setConfig.selection_mode, setConfig.values]);

  const sectionId = sectionIds[0] ?? null;
  const section = useMemo(
    () => props.sections.find((s) => s.id === sectionId) ?? null,
    [props.sections, sectionId],
  );
  const hasLibrary = sectionIds.length > 0;
  const sectionType: "movie" | "show" = section?.type ?? props.recipe?.plex_section_type ?? "movie";

  const minItemsCapsConflict = useMemo(() => {
    if (setConfig.category === "tag") {
      return null;
    }
    return minItemsVersusCapsConflict({
      minItems: setConfig.min_items,
      includeLibrary: setConfig.include_library !== false,
      libraryLimit: setConfig.limit ?? null,
      autoAddEnabled: Boolean(autoAdd?.enabled),
      missingLimit: autoAdd?.missing_limit ?? autoAdd?.max_per_run,
    });
  }, [autoAdd, setConfig.category, setConfig.include_library, setConfig.limit, setConfig.min_items]);

  const runCheckNames = async () => {
    if (!sectionId || sectionIds.length === 0) return;
    const pattern = setConfig.title_pattern.trim() || defaultSetConfig(setConfig.category).title_pattern;
    if (!pattern.includes("{value}")) return;
    setNameCheck({ status: "loading" });
    try {
      const { dimension: _omit, ...rest } = setConfig;
      const config: CollectionSetConfig = {
        ...rest,
        category: setConfig.category,
        title_pattern: pattern,
        values: setConfig.values,
        instance_key: setConfig.category === "tag" ? setConfig.instance_key : null,
        release_basis: setConfig.category === "release_timing" ? setConfig.release_basis : null,
      };
      const result = await checkCollectionTitleConflicts({
        plex_section_id: sectionId,
        plex_section_ids: sectionIds,
        plex_section_type: sectionType,
        collection_title: pattern,
        definition: setDefinition(config, adoptExisting, visibleOn, autoAdd),
        recipe_id: props.recipe?.id ?? null,
      });
      const blocking = (result.conflicts || []).filter((c) => c.reason !== "ours");
      if (blocking.length === 0) {
        setNameCheck({ status: "ok" });
        return;
      }
      setNameCheck({ status: "conflict", conflicts: blocking });
    } catch (err) {
      setNameCheck({
        status: "error",
        message: err instanceof Error ? err.message : "Could not check those names",
      });
    }
  };

  const [builderMeta, setBuilderMeta] = useState<CollectionBuilderMeta | null>(null);
  useEffect(() => {
    getCollectionBuilderMeta(sectionType)
      .then(setBuilderMeta)
      .catch(() => setBuilderMeta(null));
  }, [sectionType]);

  const catalogOptions = useMemo(() => {
    const fallback = pickerOptionsForCategory(setConfig.category, sectionType);
    if (setConfig.category === "genre") {
      const genres = mergeUniqueSorted(builderMeta?.genres, fallback);
      return genres.map((g) => ({ id: g, label: g }));
    }
    if (setConfig.category === "content_rating") {
      const certs = mergeUniqueSorted(builderMeta?.certifications, fallback);
      return certs.map((c) => ({ id: c, label: c }));
    }
    if (setConfig.category === "decade") {
      const decades = mergeUniqueSorted(builderMeta?.decades, fallback);
      return decades.map((d) => ({ id: d, label: d }));
    }
    if (setConfig.category === "release_timing") {
      return RELEASE_TIMING_PRESETS;
    }
    if (setConfig.category === "tag") {
      if (!hasLibrary) return [] as { id: string; label: string }[];
      const key = setConfig.instance_key;
      return (builderMeta?.arr_tags || [])
        .filter((t) => !key || t.instance_key === key)
        .map((t) => ({ id: String(t.tag_id), label: t.label }));
    }
    if (setConfig.category === "studio") {
      const studios = mergeUniqueSorted(builderMeta?.studios, fallback);
      return studios.map((s) => ({ id: s, label: s }));
    }
    if (setConfig.category === "network") {
      const networks = mergeUniqueSorted(builderMeta?.networks, fallback);
      return networks.map((n) => ({ id: n, label: n }));
    }
    return [] as { id: string; label: string }[];
  }, [builderMeta, hasLibrary, sectionType, setConfig.category, setConfig.instance_key]);

  const releaseBasisOptions = sectionType === "show" ? SHOW_RELEASE_BASES : MOVIE_RELEASE_BASES;

  const arrInstances = useMemo(() => {
    if (!hasLibrary) return [];
    const wanted = sectionType === "movie" ? "radarr" : "sonarr";
    return (builderMeta?.instances || []).filter((i) => i.arr_type === wanted);
  }, [builderMeta, hasLibrary, sectionType]);

  useEffect(() => {
    if (setConfig.category !== "tag") return;
    if (!hasLibrary) {
      if (setConfig.instance_key) {
        setSetConfig((prev) => ({ ...prev, instance_key: null, values: [] }));
      }
      return;
    }
    const stillValid = arrInstances.some((i) => i.instance_key === setConfig.instance_key);
    if (stillValid) return;
    setSetConfig((prev) => ({
      ...prev,
      instance_key: arrInstances[0]?.instance_key ?? null,
      values: [],
    }));
  }, [setConfig.category, setConfig.instance_key, arrInstances, hasLibrary]);

  useEffect(() => {
    if (setConfig.category !== "release_timing") return;
    const allowed = new Set(releaseBasisOptions.map((o) => o.value));
    if (!setConfig.release_basis || !allowed.has(setConfig.release_basis)) {
      setSetConfig((prev) => ({
        ...prev,
        release_basis: sectionType === "show" ? "premiered" : "theater",
      }));
    }
  }, [setConfig.category, setConfig.release_basis, sectionType, releaseBasisOptions]);

  const [preview, setPreview] = useState<CollectionPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [selectedChildTitle, setSelectedChildTitle] = useState<string | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [dockPrefs] = useDockPrefs();
  const [focusLibraryId, setFocusLibraryId] = useState<number | null>(null);
  const [catalogLibraryFilter, setCatalogLibraryFilter] = useState<number | null>(null);

  useEffect(() => {
    const rows = preview?.set_collections ?? [];
    if (!rows.length) {
      setSelectedChildTitle(null);
      return;
    }
    if (!selectedChildTitle || !rows.some((r) => r.title === selectedChildTitle)) {
      setSelectedChildTitle(rows[0]?.title ?? null);
    }
  }, [preview?.set_collections, selectedChildTitle]);

  useEffect(() => {
    if (!sectionIds.length) {
      setFocusLibraryId(null);
      return;
    }
    if (focusLibraryId == null || !sectionIds.includes(focusLibraryId)) {
      setFocusLibraryId(sectionIds[0] ?? null);
    }
  }, [sectionIds, focusLibraryId]);

  const selectedChild = useMemo(
    () => preview?.set_collections?.find((r) => r.title === selectedChildTitle) ?? null,
    [preview?.set_collections, selectedChildTitle],
  );
  const targetLibraries = useMemo(
    () => props.sections.filter((s) => sectionIds.includes(s.id)),
    [props.sections, sectionIds],
  );
  // Before any library is picked, show both movie and TV controlled libs (type locks after the first pick).
  const pickerSections = selectablePlexSections(
    props.sections,
    sectionIds.length ? sectionType : null,
    sectionIds,
  );

  const runPreview = useCallback(async () => {
    if (!sectionId) {
      setPreviewError("Select at least one Plex library");
      return;
    }
    if (setConfig.category === "tag" && !setConfig.instance_key) {
      setPreviewError("Pick a Radarr/Sonarr instance for tag sets");
      return;
    }
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const result = await previewCollectionDefinition({
        plex_section_id: sectionId,
        plex_section_ids: sectionIds,
        plex_section_type: sectionType,
        definition: setDefinition(setConfig, adoptExisting, visibleOn, autoAdd),
      });
      setPreview(result);
    } catch (err) {
      setPreview(null);
      setPreviewError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setPreviewLoading(false);
    }
  }, [sectionId, sectionIds, sectionType, setConfig, adoptExisting, visibleOn, autoAdd]);

  useEffect(() => {
    if (!sectionId) return;
    const timer = window.setTimeout(() => {
      void runPreview();
    }, 400);
    return () => window.clearTimeout(timer);
  }, [
    sectionId,
    sectionIds,
    setConfig.category,
    setConfig.selection_mode,
    setConfig.values,
    setConfig.min_items,
    setConfig.instance_key,
    setConfig.release_basis,
    setConfig.include_library,
    setConfig.limit,
    autoAdd,
    runPreview,
  ]);

  const previewLibraryBreakdown = useMemo(() => {
    if (!preview?.libraries?.length) return [];
    return preview.libraries.map((lib) => {
      const section = props.sections.find((s) => s.id === lib.plex_section_id);
      return {
        id: lib.plex_section_id,
        title: section?.title ?? `Section ${lib.plex_section_id}`,
        count: lib.in_target_library ?? 0,
      };
    });
  }, [preview?.libraries, props.sections]);

  const toggleValue = (id: string) => {
    setSetConfig((prev) => {
      const has = prev.values.some((v) => v.toLowerCase() === id.toLowerCase());
      const values = has
        ? prev.values.filter((v) => v.toLowerCase() !== id.toLowerCase())
        : [...prev.values, id];
      return { ...prev, values };
    });
  };

  const canSave =
    name.trim().length > 0 &&
    sectionIds.length > 0 &&
    setConfig.title_pattern.includes("{value}") &&
    setConfig.values.length > 0 &&
    (setConfig.category !== "tag" || Boolean(setConfig.instance_key)) &&
    isCollectionAutoAddReady(autoAdd);

  const catalogValueStrings = useMemo(
    () => catalogOptions.map((o) => o.id),
    [catalogOptions],
  );

  const handleSave = () => {
    if (!canSave || !sectionId) return;
    const pattern = setConfig.title_pattern.trim() || defaultSetConfig(setConfig.category).title_pattern;
    const { dimension: _omitLegacy, ...rest } = setConfig;
    const config: CollectionSetConfig = {
      ...rest,
      category: setConfig.category,
      title_pattern: pattern,
      values: setConfig.values,
      instance_key: setConfig.category === "tag" ? setConfig.instance_key : null,
      release_basis: setConfig.category === "release_timing" ? setConfig.release_basis : null,
    };
    const scheduleEnabled = followGlobalSchedule || Boolean(runSchedule.enabled);
    props.onSave({
      name: name.trim(),
      enabled: scheduleEnabled,
      plex_section_id: sectionId,
      plex_section_ids: sectionIds,
      plex_section_type: sectionType,
      collection_title: pattern,
      definition: setDefinition(config, adoptExisting, visibleOn, autoAdd),
      run_interval_hours: followGlobalSchedule
        ? null
        : runSchedule.mode === "interval"
          ? runSchedule.interval_hours
          : null,
      run_schedule: followGlobalSchedule ? null : runSchedule,
      active_window: activeWindow,
    });
  };

  const categoryMeta = CATEGORY_OPTIONS.find((d) => d.value === setConfig.category) ?? CATEGORY_OPTIONS[0];

  const childAutoAddActive = Boolean(autoAdd?.enabled) && isCollectionAutoAddReady(autoAdd);
  const childPreview = useRecipePreview(selectedChild?.definition ?? null, sectionIds, sectionType);

  const setSheetBody = (
    <div className="grid gap-5 lg:grid-cols-[minmax(16rem,22rem)_minmax(0,1fr)]">
      <div className="flex flex-col gap-3">
      {previewError ? <p className="text-[13px] text-red-400">{previewError}</p> : null}
      <ChildCollectionList
        themeMode={props.themeMode}
        accentHex={accentHex}
        rows={preview?.set_collections ?? []}
        selectedTitle={selectedChildTitle}
        onSelect={setSelectedChildTitle}
        loading={previewLoading}
      />
      {preview?.plex_error ? <p className="text-[13px] text-amber-400">{preview.plex_error}</p> : null}
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        {dockPrefs.plexMock && selectedChild ? (
          <div className="flex flex-col gap-3">
            {targetLibraries.length > 1 ? (
              <PreviewLibraryChips
                themeMode={props.themeMode}
                accentHex={accentHex}
                libraries={targetLibraries}
                value={focusLibraryId}
                onChange={(id) => setFocusLibraryId(id ?? targetLibraries[0]?.id ?? null)}
                requireLibrary
              />
            ) : null}
            <PlexRowMock
              themeMode={props.themeMode}
              accentHex={accentHex}
              sectionId={focusLibraryId || sectionId || null}
              libraryTitle={
                props.sections.find((s) => s.id === (focusLibraryId || sectionId))?.title
              }
              collectionTitle={selectedChild.title}
              recipeId={props.recipe?.id ?? null}
              items={filmstripForLibrary(
                buildFilmstrip(childPreview.preview, dockPrefs.ghosts && childAutoAddActive),
                focusLibraryId || sectionId || null,
              )}
              promotedOnCreate={Boolean(visibleOn?.library)}
            />
          </div>
        ) : null}
        <div className={`${theme.blockCard} p-4`}>
          {selectedChild ? (
            <RecipePreviewResultView
              themeMode={props.themeMode}
              accentHex={accentHex}
              sections={props.sections}
              sectionIds={sectionIds}
              preview={childPreview.preview}
              loading={childPreview.loading}
              error={childPreview.error}
              title={selectedChild.title}
              autoAddConfigured={Boolean(autoAdd?.enabled)}
              autoAddActive={childAutoAddActive}
              layout={dockPrefs.plexMock ? "default" : "sheet"}
              libraryFilter={catalogLibraryFilter}
              onLibraryFilterChange={setCatalogLibraryFilter}
            />
          ) : (
            <p className={`text-[13px] ${theme.muted}`}>
              {sectionIds.length ? "Pick a collection from the list to preview it." : "Pick a Plex library to preview."}
            </p>
          )}
        </div>
      </div>
    </div>
  );

  const librariesBlock = (
          <div className="flex flex-col gap-1.5 min-w-[16rem]">
            <span className={theme.label}>Plex libraries</span>
            <p className={`text-[12px] ${theme.muted}`}>
              Controlled libraries only (placeholder destinations and Arr real-file roots). Visible on shows items from
              that library.
            </p>
            <div className="flex flex-col gap-1 max-h-40 overflow-y-auto pr-1">
              {pickerSections.map((s) => {
                const selected = sectionIds.includes(s.id);
                const typeLocked = sectionType && s.type !== sectionType && sectionIds.length > 0;
                const role = managedRoleLabel(s.managed_role);
                return (
                  <label
                    key={s.id}
                    className={`flex items-center gap-2 text-[13px] ${typeLocked ? "opacity-40 pointer-events-none" : "cursor-pointer"} ${theme.label}`}
                  >
                    <BrandCheckbox
                      checked={selected}
                      disabled={typeLocked}
                      size="sm"
                      accentHex={accentHex}
                      ariaLabel={`${s.title} library`}
                      onChange={(next) => {
                        setSectionIds((prev) => {
                          if (!next) return prev.filter((id) => id !== s.id);
                          if (prev.includes(s.id)) return prev;
                          if (prev.length && s.type !== sectionType) return prev;
                          return [...prev, s.id];
                        });
                      }}
                    />
                    <span>
                      {s.title}{" "}
                      <span className="opacity-70">
                        ({s.type === "movie" ? "Movies" : "TV"}, {s.item_count}
                        {role ? ` · ${role}` : ""}
                        {s.managed === false ? " · not controlled" : ""})
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
  );
  const setNameBlock = (
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Config name
            <input
              className={theme.field}
              style={{ width: 240 }}
              value={name}
              placeholder="e.g. Movie genres"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
  );
  const scheduleBlock = (
        <div className="space-y-3">
          <div className={`text-[12px] font-headline uppercase tracking-wider ${theme.muted}`}>Schedule</div>
          <TaskScheduleEditor
            value={runSchedule}
            allowFollowGlobal
            followGlobal={followGlobalSchedule}
            onFollowGlobalChange={setFollowGlobalSchedule}
            accentHex={accentHex}
            fallbackHours={24}
            clockFormat={props.clockFormat}
            onChange={setRunSchedule}
          />
        </div>
  );
  const seasonalBlock = (
        <div className="flex flex-wrap items-center gap-4">
          <label className={`flex items-center gap-2 ${theme.label} cursor-pointer select-none`}>
            <span>Seasonal window</span>
            <ToggleSwitch
              checked={activeWindow !== null}
              onChange={(on) =>
                setActiveWindow(on ? { start: "12-01", end: "01-05", when_inactive: "keep" } : null)
              }
              accentHex={accentHex}
              ariaLabel="Seasonal active window"
            />
          </label>
          {activeWindow ? (
            <>
              <label className={`flex items-center gap-2 ${theme.label}`}>
                Start
                <input
                  className={theme.field}
                  style={{ width: 100 }}
                  value={activeWindow.start}
                  placeholder="MM-DD"
                  onChange={(e) => setActiveWindow({ ...activeWindow, start: e.target.value })}
                />
              </label>
              <label className={`flex items-center gap-2 ${theme.label}`}>
                End
                <input
                  className={theme.field}
                  style={{ width: 100 }}
                  value={activeWindow.end}
                  placeholder="MM-DD"
                  onChange={(e) => setActiveWindow({ ...activeWindow, end: e.target.value })}
                />
              </label>
              <label className={`flex items-center gap-2 ${theme.label}`}>
                When inactive
                <BrandSelect
                  triggerClassName="min-w-[8rem] shrink-0"
                  value={activeWindow.when_inactive}
                  options={[
                    { value: "keep", label: "Keep collection as-is" },
                    { value: "clear", label: "Empty the collection" },
                    { value: "delete", label: "Delete the collection" },
                  ]}
                  onChange={(value) =>
                    setActiveWindow({
                      ...activeWindow,
                      when_inactive:
                        value === "delete" ? "delete" : value === "clear" ? "clear" : "keep",
                    })
                  }
                  ariaLabel="When inactive"
                />
              </label>
              {activeWindow.when_inactive === "delete" ? (
                <span className={`text-[12px] font-normal normal-case tracking-normal ${theme.muted}`}>
                  Only collections Placeholdarr owns are deleted.
                </span>
              ) : null}
            </>
          ) : null}
        </div>
  );
  const categoryChipsBlock = (
          <div className="flex flex-wrap gap-2">
            {CATEGORY_OPTIONS.filter(
              (opt) =>
                !(opt.movieOnly && sectionType !== "movie") && !(opt.showOnly && sectionType !== "show"),
            ).map((opt) => {
              const active = setConfig.category === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() =>
                    setSetConfig((prev) => ({
                      ...defaultSetConfig(opt.value, sectionType),
                      selection_mode: "include",
                      values: starterValuesForCategory(opt.value, sectionType, catalogValueStrings),
                      instance_key: opt.value === "tag" ? prev.instance_key || arrInstances[0]?.instance_key || null : null,
                      release_basis:
                        opt.value === "release_timing"
                          ? sectionType === "show"
                            ? "premiered"
                            : "theater"
                          : null,
                      managed_by_section: prev.managed_by_section,
                    }))
                  }
                  className={`rounded-lg px-3 py-1.5 text-[13px] border transition-colors ${
                    active ? `${FG_ON_ACCENT_TEXT_CLASS} border-transparent` : theme.chipInactive
                  }`}
                  style={active ? accentFilledStyle(accentHex) : undefined}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
  );
  const tagInstanceBlock = (
          setConfig.category === "tag" ? (
            hasLibrary ? (
              <label className={`flex items-center gap-2 ${theme.label}`}>
                *arr instance
                <BrandSelect
                  triggerClassName="min-w-[8rem] shrink-0"
                  value={setConfig.instance_key ?? ""}
                  options={[
                    { value: "", label: "Select instance…" },
                    ...arrInstances.map((inst) => ({
                      value: inst.instance_key,
                      label: inst.label || inst.instance_key,
                    })),
                  ]}
                  onChange={(value) =>
                    setSetConfig((prev) => ({ ...prev, instance_key: value || null, values: [] }))
                  }
                  ariaLabel="Arr instance"
                />
              </label>
            ) : (
              <p className={`text-[13px] ${theme.muted}`}>
                Select a Plex library first to choose a matching Radarr or Sonarr instance.
              </p>
            )
          ) : null
  );
  const releaseBasisBlock = (
          setConfig.category === "release_timing" ? (
            <label className={`flex items-center gap-2 ${theme.label}`}>
              Based on
              <BrandSelect
                triggerClassName="min-w-[8rem] shrink-0"
                value={setConfig.release_basis ?? (sectionType === "show" ? "premiered" : "theater")}
                options={releaseBasisOptions.map((opt) => ({
                  value: opt.value,
                  label: opt.label,
                }))}
                onChange={(value) => setSetConfig((prev) => ({ ...prev, release_basis: value }))}
                ariaLabel="Based on"
              />
            </label>
          ) : null
  );
  const titlePatternBlock = (
    <>
          <div className="flex flex-col gap-1.5">
            <label className={`flex flex-wrap items-center gap-2 ${theme.label}`}>
              Title pattern
              <input
                className={`${theme.field} flex-1`}
                style={{ minWidth: 220 }}
                value={setConfig.title_pattern}
                onChange={(e) => {
                  setSetConfig((prev) => ({ ...prev, title_pattern: e.target.value }));
                  if (adoptExisting) setAdoptExisting(false);
                }}
              />
              <button
                type="button"
                className={theme.cancelButton}
                disabled={
                  sectionIds.length === 0 ||
                  !setConfig.title_pattern.includes("{value}") ||
                  nameCheck.status === "loading"
                }
                onClick={() => void runCheckNames()}
              >
                {nameCheck.status === "loading" ? "Checking…" : "Check names"}
              </button>
            </label>
            {adoptExisting ? (
              <p className="text-[13px] text-amber-300/90 max-w-xl">
                Will adopt existing Plex collections when names match. That reconnects previous Placeholdarr
                collections, or takes over non-Placeholdarr ones. Items that do not match this set will be removed on
                sync.
              </p>
            ) : null}
            {nameCheck.status === "ok" ? (
              <p className="text-[13px] text-emerald-400">Those names are available in the selected libraries.</p>
            ) : null}
            {nameCheck.status === "error" ? (
              <p className="text-[13px] text-red-400">{nameCheck.message}</p>
            ) : null}
            {nameCheck.status === "conflict" ? (
              <div className={`text-[13px] space-y-2 max-w-xl ${theme.muted}`}>
                {nameCheck.conflicts.some((c) => c.reason === "other_recipe") ? (
                  <p className={isLight ? "text-amber-800" : "text-amber-200"}>
                    One or more generated names are already used by another Placeholdarr recipe. Change the pattern or
                    values, or rename the other recipe&apos;s collection first.
                  </p>
                ) : (
                  <p className={isLight ? "text-amber-800" : "text-amber-200"}>
                    One or more generated names are already used. Change the pattern or values, or adopt when you save
                    to reconnect previous Placeholdarr collections (or take over non-Placeholdarr ones). Items that do
                    not match are removed on sync.
                  </p>
                )}
                <ul className="space-y-1 max-h-36 overflow-y-auto">
                  {nameCheck.conflicts.map((c) => (
                    <li key={`${c.section_id}:${c.title}:${c.rating_key || ""}`}>
                      <span className={isLight ? "text-slate-800 font-semibold" : "text-slate-200 font-semibold"}>
                        {c.title}
                      </span>
                      {" · "}
                      {c.section_title}
                      {c.reason === "other_recipe"
                        ? " (owned by another Placeholdarr recipe)"
                        : ` (${c.item_count} item${c.item_count === 1 ? "" : "s"})`}
                    </li>
                  ))}
                </ul>
                {nameCheck.conflicts.every((c) => c.reason !== "other_recipe") ? (
                  <button
                    type="button"
                    className={`px-3 py-1.5 rounded-md text-[12px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
                    style={accentFilledStyle(accentHex)}
                    onClick={() => {
                      setAdoptExisting(true);
                      setNameCheck({ status: "idle" });
                    }}
                  >
                    Adopt when saving
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
          <p className={`text-[12px] ${theme.muted}`}>
            Use <code className="font-mono">{"{value}"}</code> for the value label. Optional:{" "}
            <code className="font-mono">{"{category}"}</code>.
          </p>
    </>
  );
  const valuesBlock = (
          setConfig.category === "tag" || setConfig.category === "release_timing" ? (
            <div className="flex flex-wrap gap-1.5">
              {catalogOptions.length ? (
                catalogOptions.map((opt) => {
                  const active = setConfig.values.some((v) => v.toLowerCase() === opt.id.toLowerCase());
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => toggleValue(opt.id)}
                      className={`rounded-md px-2.5 py-1 text-[12px] border ${
                        active ? `${FG_ON_ACCENT_TEXT_CLASS} border-transparent` : theme.chipInactive
                      }`}
                      style={active ? accentFilledStyle(accentHex) : undefined}
                    >
                      {opt.label}
                    </button>
                  );
                })
              ) : (
                <span className={`text-[13px] ${theme.muted}`}>
                  No {categoryMeta.label.toLowerCase()} values available yet.
                </span>
              )}
            </div>
          ) : (
            <CollectionValueMultiSelect
              themeMode={props.themeMode}
              accentHex={accentHex}
              options={catalogValueStrings}
              selected={setConfig.values}
              onChange={(next) => setSetConfig((prev) => ({ ...prev, values: next }))}
              searchPlaceholder={`Search ${categoryMeta.label.toLowerCase()}s…`}
              addLabel={`Add ${categoryMeta.label.toLowerCase()}`}
              emptyOptionsMessage="No matches. Type a name and add it if it is not in the list yet."
            />
          )
  );
  const contentsBlock = (
        <CollectionShelfContentsSection
          layout="panel"
          themeMode={props.themeMode}
          accentHex={accentHex}
          mediaType={sectionType}
          arrTargetsReady={hasLibrary}
          includeLibrary={setConfig.include_library !== false}
          libraryLimit={setConfig.limit ?? null}
          onIncludeLibraryChange={(next) => setSetConfig((prev) => ({ ...prev, include_library: next }))}
          onLibraryLimitChange={(next) => setSetConfig((prev) => ({ ...prev, limit: next }))}
          autoAdd={autoAdd}
          onAutoAddChange={setAutoAdd}
          scopeHint={autoAddScopeHint(setConfig.category, setConfig.values)}
          disabledReason={
            setConfig.category === "tag"
              ? "Tag sets use Arr tags only. This option does not apply."
              : undefined
          }
        />
  );
  const arrangeBlock = (
        <div className="flex flex-col gap-5">
          <div>
            <div className="flex flex-wrap items-center gap-4">
              <label className={`flex items-center gap-2 ${theme.label}`}>
                Sort by
                <BrandSelect
                  triggerClassName="min-w-[8rem] shrink-0"
                  value={setConfig.sort ?? "title"}
                  options={collectionArrangeSortOptions(sectionType)}
                  onChange={(value) =>
                    setSetConfig((prev) => ({ ...prev, sort: value as CollectionSortOption }))
                  }
                  ariaLabel="Sort by"
                />
              </label>
              <label className={`flex flex-wrap items-center gap-2 ${theme.label}`}>
                Min items
                <SetNumberInput
                  fieldClassName={theme.field}
                  value={setConfig.min_items ?? 1}
                  min={0}
                  max={500}
                  placeholder="1"
                  onChange={(v) =>
                    setSetConfig((prev) => ({ ...prev, min_items: v == null ? 1 : v }))
                  }
                />
                {minItemsCapsConflict ? (
                  <span
                    className={`text-[12px] ${isLight ? "text-amber-800" : "text-amber-300"}`}
                    title={minItemsCapsConflict.title}
                  >
                    {minItemsCapsConflict.short}
                  </span>
                ) : null}
              </label>
            </div>
            <p className={`mt-2 text-[13px] ${theme.muted}`}>
              Each child collection needs at least this many matching titles after filters and caps. Collections below
              that are not created or updated on that run. If a collection already exists in Plex but later drops below
              the minimum,
              the next sync removes that collection. Use Collections → Ordering to set where collections appear in
              Plex.
            </p>
          </div>
        </div>
  );

  const classicBody = (
    <>
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className={`text-[22px] font-headline uppercase tracking-wider ${theme.heading}`}>
            Collection Set
          </h2>
          <p className={`mt-1 text-[14px] max-w-2xl ${theme.muted}`}>
            Build many Plex collections from one Collection Set. Select a category such as genre or decade,
            then sync the values you want without writing a recipe for each.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className={theme.cancelButton} onClick={props.onCancel}>
            Cancel
          </button>
          <button
            type="button"
            disabled={!canSave || props.saving}
            onClick={handleSave}
            className={`rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS} disabled:opacity-40`}
            style={accentFilledStyle(accentHex)}
          >
            {props.saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {props.saveError ? (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-[14px] text-red-300">
          {props.saveError}
        </div>
      ) : null}

      <div className={`${theme.identityCard} flex flex-col gap-4`}>
        <div className="flex flex-wrap items-start gap-4">
          {librariesBlock}
          {setNameBlock}
        </div>
        {scheduleBlock}
        {seasonalBlock}
      </div>

      <div className={`${theme.blockCard} space-y-3 overflow-visible p-4`}>
        <div className={theme.blockHeader}>
          <span className="material-symbols-outlined mt-0.5 shrink-0" style={{ fontSize: 16, color: accentHex }} aria-hidden>
            category
          </span>
          <div className="min-w-0 flex-1 space-y-1">
            <div className={theme.blockTitle}>Category</div>
            <div className={theme.blockSubtitle}>{categoryMeta.description}</div>
          </div>
        </div>
        <div className="flex flex-col gap-3">
          {categoryChipsBlock}

          {tagInstanceBlock}

          {releaseBasisBlock}

          {titlePatternBlock}
          {valuesBlock}
        </div>
      </div>

      <div className={`${theme.blockCard} space-y-3 overflow-visible p-4`}>
        <div className={theme.blockHeader}>
          <span className="material-symbols-outlined mt-0.5 shrink-0" style={{ fontSize: 16, color: accentHex }} aria-hidden>
            view_agenda
          </span>
          <div className="min-w-0 flex-1 space-y-1">
            <div className={theme.blockTitle}>What&apos;s in each collection</div>
            <div className={theme.blockSubtitle}>Library titles and titles added from TMDB</div>
          </div>
        </div>
        {contentsBlock}
      </div>

      <div className={`${theme.blockCard} space-y-3 overflow-visible p-4`}>
        <div className={theme.blockHeader}>
          <span className="material-symbols-outlined mt-0.5 shrink-0" style={{ fontSize: 16, color: accentHex }} aria-hidden>
            display_settings
          </span>
          <div className="min-w-0 flex-1 space-y-1">
            <div className={theme.blockTitle}>Arrange</div>
            <div className={theme.blockSubtitle}>Sort and minimum size for each child collection</div>
          </div>
        </div>
        {arrangeBlock}
      </div>

      <div className={`${theme.blockCard} space-y-3 overflow-hidden p-4`}>
        <div className={theme.blockHeader}>
          <span className="material-symbols-outlined mt-0.5 shrink-0" style={{ fontSize: 16, color: accentHex }} aria-hidden>
            preview
          </span>
          <div className="min-w-0 flex-1 space-y-1">
            <div className={theme.blockTitle}>Preview</div>
            <div className={theme.blockSubtitle}>
              Titles in each selected Plex library that would join each collection. Row counts are unique across
              those libraries.
            </div>
          </div>
          <button
            type="button"
            className={`${theme.cancelButton} shrink-0`}
            onClick={() => void runPreview()}
            disabled={previewLoading}
          >
            {previewLoading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <div>
          {previewError ? <p className="text-[13px] text-red-400 mb-2">{previewError}</p> : null}
          {preview?.plex_error ? (
            <p className="text-[13px] text-amber-400 mb-2">{preview.plex_error}</p>
          ) : null}
          {preview?.set_collections?.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-[13px]">
                <thead>
                  <tr className={`border-b ${theme.divider}`}>
                    <th className={`py-2 text-left font-normal ${theme.muted}`}>Collection title</th>
                    <th className={`py-2 text-left font-normal ${theme.muted}`}>Value</th>
                    <th className={`py-2 text-left font-normal ${theme.muted}`}>Source</th>
                    <th className={`py-2 text-right font-normal ${theme.muted}`}>In Plex</th>
                    <th className={`py-2 text-right font-normal ${theme.muted}`}>Selected</th>
                    <th className={`py-2 text-right font-normal ${theme.muted}`}>Placeholders</th>
                    <th className={`py-2 text-right font-normal ${theme.muted}`}>Would add</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.set_collections.map((row) => (
                    <tr key={row.title} className={`border-b ${theme.divider}`}>
                      <td className="py-2">{row.title}</td>
                      <td className={`py-2 ${theme.muted}`}>{row.value}</td>
                      <td
                        className={`py-2 ${
                          row.source === "library_fallback"
                            ? isLight
                              ? "text-amber-800"
                              : "text-amber-300"
                            : theme.muted
                        }`}
                        title={
                          row.source === "library_fallback"
                            ? "Not found on TMDB, so this collection only uses titles in your library."
                            : undefined
                        }
                      >
                        {SOURCE_LABELS[row.source ?? "library"]}
                      </td>
                      <td className="py-2 text-right font-mono">{row.in_target_library ?? row.selected}</td>
                      <td className="py-2 text-right font-mono">{row.after_filters ?? "—"}</td>
                      <td className="py-2 text-right font-mono">{row.held_count ?? 0}</td>
                      <td className="py-2 text-right font-mono">{row.would_add ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className={`mt-2 text-[12px] ${theme.muted} space-y-1`}>
                <p>
                  {preview.set_collection_count} collections
                  {previewLibraryBreakdown.length > 1
                    ? ` · ${preview.selected} unique titles across libraries`
                    : ` · ${preview.selected} titles in library`}
                  {typeof preview.would_add_total === "number" && preview.would_add_total > 0
                    ? ` · would add ${preview.would_add_total} across collections on next sync`
                    : null}
                </p>
                {previewLibraryBreakdown.length ? (
                  <p>
                    {previewLibraryBreakdown.map((lib, index) => (
                      <span key={lib.id}>
                        {index > 0 ? " · " : null}
                        {lib.title}: {lib.count}
                      </span>
                    ))}
                  </p>
                ) : null}
              </div>
            </div>
          ) : (
            <p className={`text-[13px] ${theme.muted}`}>
              {previewLoading
                ? "Loading preview…"
                : "No titles in the selected Plex libraries match this set yet."}
            </p>
          )}
        </div>
      </div>
    </>
  );

  const libraryNames = sectionIds
    .map((id) => props.sections.find((s) => s.id === id)?.title)
    .filter(Boolean)
    .join(", ");
  const issues = [
    !sectionIds.length ? "Pick a Plex library" : null,
    !name.trim() ? "Add a set name" : null,
    !setConfig.title_pattern.includes("{value}") ? "Title pattern needs {value}" : null,
    !setConfig.values.length ? `Pick at least one ${categoryMeta.label.toLowerCase()}` : null,
    setConfig.category === "tag" && !setConfig.instance_key ? "Pick a Radarr/Sonarr instance" : null,
    !isCollectionAutoAddReady(autoAdd) ? "Finish Radarr/Sonarr targets" : null,
  ].filter((v): v is string => Boolean(v));
  const builtCount = preview?.set_collection_count;
  const totalCount = preview?.set_collection_count_total ?? builtCount;
  const missingCap = autoAdd?.missing_limit ?? autoAdd?.max_per_run;

  const nextConfig = (
    <>
      {props.saveError ? (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-[14px] text-red-300">
          {props.saveError}
        </div>
      ) : null}
      <BuilderSection
        number={1}
        title="Where"
        themeMode={props.themeMode}
        summary={[libraryNames || "No library", name.trim() ? `"${name.trim()}"` : null].filter(Boolean).join(" · ")}
      >
        <div className="flex flex-col gap-4">
          {librariesBlock}
          {setNameBlock}
        </div>
      </BuilderSection>
      <BuilderSection
        number={2}
        title="What"
        themeMode={props.themeMode}
        metric={totalCount != null ? `${totalCount} collection${totalCount === 1 ? "" : "s"}` : null}
        summary={`${categoryMeta.label}: ${
          setConfig.values.length
            ? `${setConfig.values.slice(0, 4).join(", ")}${setConfig.values.length > 4 ? ` +${setConfig.values.length - 4}` : ""}`
            : "no values"
        }`}
      >
        <div className="flex flex-col gap-4">
          <p className={`text-[13px] ${theme.muted}`}>{categoryMeta.description}</p>
          {categoryChipsBlock}
          {tagInstanceBlock}
          {releaseBasisBlock}
          {valuesBlock}
        </div>
      </BuilderSection>
      <BuilderSection
        number={3}
        title="Naming"
        themeMode={props.themeMode}
        summary={setConfig.title_pattern}
      >
        <div className="flex flex-col gap-3">{titlePatternBlock}</div>
      </BuilderSection>
      <BuilderSection
        number={4}
        title="Contents"
        themeMode={props.themeMode}
        metric={
          preview && sectionIds.length
            ? `${preview.selected ?? 0} titles${
                childAutoAddActive && preview.would_add_total ? ` + ${preview.would_add_total} to add` : ""
              }`
            : null
        }
        summary={[
          setConfig.include_library !== false
            ? `Library titles${setConfig.limit ? ` (max ${setConfig.limit})` : ""}`
            : "No library titles",
          autoAdd?.enabled ? `Titles you don't have${missingCap ? ` (max ${missingCap})` : ""}` : "Library only",
        ].join(" · ")}
      >
        {contentsBlock}
      </BuilderSection>
      <BuilderSection
        number={5}
        title="Arrange"
        themeMode={props.themeMode}
        metric={
          totalCount != null && builtCount != null && totalCount > builtCount
            ? `${totalCount - builtCount} below min`
            : null
        }
        summary={`${
          collectionArrangeSortOptions(sectionType).find((o) => o.value === (setConfig.sort ?? "title"))?.label ?? ""
        } · min ${setConfig.min_items ?? 1} per collection`}
      >
        {arrangeBlock}
      </BuilderSection>
      <BuilderSection
        number={6}
        title="When"
        themeMode={props.themeMode}
        defaultOpen={false}
        summary={[
          followGlobalSchedule ? "Collections sync schedule" : "Custom schedule",
          activeWindow ? `Seasonal ${activeWindow.start} to ${activeWindow.end}` : null,
        ]
          .filter(Boolean)
          .join(" · ")}
      >
        <div className="flex flex-col gap-4">
          {scheduleBlock}
          {seasonalBlock}
        </div>
      </BuilderSection>
      <BuilderSection
        number={7}
        title="Where it appears"
        themeMode={props.themeMode}
        defaultOpen={false}
        summary="Plex Recommended, Home, and Shared placement"
      >
        <div className="flex flex-col gap-2">
          <p className={`text-[13px] ${theme.muted}`}>
            Hub order and Recommended / Home / Shared visibility are managed live in Plex from the Ordering page.
            Set sync never changes them.
          </p>
          {props.onOpenOrdering ? (
            <button type="button" className={`${theme.cancelButton} self-start`} onClick={props.onOpenOrdering}>
              Open Ordering
            </button>
          ) : null}
        </div>
      </BuilderSection>
      {builtCount != null ? (
        <p className={`px-1 text-[12px] ${theme.muted}`}>
          {builtCount} of {totalCount} collection{totalCount === 1 ? "" : "s"} would be built on the next sync.
        </p>
      ) : null}
    </>
  );

  return useNextLayout ? (
    <BuilderShell
      config={nextConfig}
      dock={
        <PreviewDock
          themeMode={props.themeMode}
          accentHex={accentHex}
          preview={childPreview.preview}
          setChildCounts={
            selectedChild
              ? {
                  inPlex:
                    focusLibraryId != null && selectedChild.by_library
                      ? Number(selectedChild.by_library[String(focusLibraryId)] ?? 0)
                      : (selectedChild.in_target_library ?? selectedChild.selected),
                  recipeSelected: selectedChild.after_filters ?? childPreview.preview?.selected ?? 0,
                }
              : null
          }
          libraries={targetLibraries}
          focusLibraryId={focusLibraryId}
          onFocusLibraryChange={setFocusLibraryId}
          loading={previewLoading || childPreview.loading}
          error={previewError || childPreview.error}
          emptyMessage={
            !sectionIds.length
              ? "Pick a Plex library to see the rows this set builds."
              : !previewLoading && !(preview?.set_collections ?? []).length
                ? "No collections match yet. Pick some values."
                : null
          }
          autoAddActive={childAutoAddActive}
          switcher={
            <ChildSwitcher
              themeMode={props.themeMode}
              rows={preview?.set_collections ?? []}
              selectedTitle={selectedChildTitle}
              onSelect={setSelectedChildTitle}
            />
          }
          onExpand={() => setSheetOpen(true)}
          actions={
            <StickyActionBar
              accentHex={accentHex}
              issues={issues}
              primaryLabel={props.recipe ? "Save changes" : "Create set"}
              primaryDisabled={!canSave}
              primaryLoading={props.saving}
              onCancel={props.onCancel}
              onPrimary={handleSave}
            />
          }
        />
      }
      sheet={
        <PreviewSheet
          open={sheetOpen}
          onClose={() => setSheetOpen(false)}
          themeMode={props.themeMode}
          title={`Full preview: ${name.trim() || "Collection set"}`}
        >
          {setSheetBody}
        </PreviewSheet>
      }
    />
  ) : (
    <div className="flex flex-col gap-4 pb-10">{classicBody}</div>
  );
}
