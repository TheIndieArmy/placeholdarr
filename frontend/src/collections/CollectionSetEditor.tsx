import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ThemeMode } from "../brandTypes";
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
  CollectionSetConfig,
  PlexSectionOption,
} from "../types/api";
import { ToggleSwitch } from "../ToggleSwitch";
import { getCollectionTheme } from "./collectionTheme";

type SetCategory = CollectionSetConfig["category"];

const CATEGORY_OPTIONS: {
  value: SetCategory;
  label: string;
  description: string;
  defaultPattern: string;
  movieOnly?: boolean;
}[] = [
  {
    value: "genre",
    label: "Genre",
    description: "One collection per genre in your catalog (Action, Comedy, …).",
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
    value: "tag",
    label: "Tag",
    description: "One collection per Radarr/Sonarr tag. Use tags as browsing buckets (Kids, Christmas, …).",
    defaultPattern: "Tag · {value}",
  },
  {
    value: "release_timing",
    label: "Release timing",
    description: "Upcoming and recently released shelves. Pick which release date to base them on.",
    defaultPattern: "{value}",
  },
];

const SCHEDULE_PRESETS: { value: string; label: string }[] = [
  { value: "", label: "App default" },
  { value: "1", label: "Hourly" },
  { value: "6", label: "Every 6 hours" },
  { value: "12", label: "Every 12 hours" },
  { value: "24", label: "Daily" },
  { value: "168", label: "Weekly" },
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

function defaultSetConfig(category: SetCategory = "genre"): CollectionSetConfig {
  const meta = CATEGORY_OPTIONS.find((d) => d.value === category) ?? CATEGORY_OPTIONS[0];
  return {
    category: meta.value,
    selection_mode: "all",
    values: [],
    title_pattern: meta.defaultPattern,
    sort: "title",
    limit: null,
    min_items: 1,
    instance_key: null,
    release_basis: category === "release_timing" ? "theater" : null,
  };
}

function setDefinition(config: CollectionSetConfig, adoptExisting = false): CollectionDefinition {
  return {
    mode: "collection_set",
    collection_set: config,
    sources: [{ type: "catalog" }],
    filters: [],
    limit: config.limit ?? null,
    sort: config.sort ?? "title",
    pins: { include: [], exclude: [] },
    ...(adoptExisting ? { adopt_existing: true } : {}),
  };
}

function setCategoryOf(config: CollectionSetConfig | null | undefined): SetCategory | null {
  const raw = config?.category || config?.dimension;
  return raw || null;
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
}) {
  const accentHex = props.accent.hex;
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const existing = props.recipe?.definition?.collection_set;

  const [name, setName] = useState(props.recipe?.name ?? "");
  const [enabled, setEnabled] = useState(props.recipe?.enabled ?? true);
  const [sectionIds, setSectionIds] = useState<number[]>(() => {
    const extras = props.recipe?.plex_section_ids?.filter((id) => Number.isFinite(id) && id >= 1) ?? [];
    if (extras.length) return extras;
    return props.recipe?.plex_section_id ? [props.recipe.plex_section_id] : [];
  });
  const [runIntervalHours, setRunIntervalHours] = useState<number | null>(props.recipe?.run_interval_hours ?? null);
  const [activeWindow, setActiveWindow] = useState<CollectionActiveWindow | null>(
    props.recipe?.active_window ?? null,
  );
  const [adoptExisting, setAdoptExisting] = useState(Boolean(props.recipe?.definition?.adopt_existing));
  const [nameCheck, setNameCheck] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ok" }
    | { status: "conflict"; conflicts: CollectionTitleConflict[] }
    | { status: "error"; message: string }
  >({ status: "idle" });
  const [setConfig, setSetConfig] = useState<CollectionSetConfig>(() => {
    const cat = setCategoryOf(existing);
    if (!cat) return defaultSetConfig("genre");
    const { dimension: _legacyDim, ...rest } = existing as CollectionSetConfig;
    return { ...defaultSetConfig(cat), ...rest, category: cat, values: existing?.values ?? [] };
  });

  const snapshot = useMemo(
    () =>
      JSON.stringify({
        name,
        enabled,
        sectionIds,
        runIntervalHours,
        activeWindow,
        adoptExisting,
        setConfig: {
          category: setConfig.category,
          selection_mode: setConfig.selection_mode,
          values: setConfig.values,
          title_pattern: setConfig.title_pattern,
          sort: setConfig.sort,
          limit: setConfig.limit,
          min_items: setConfig.min_items,
          instance_key: setConfig.instance_key,
          release_basis: setConfig.release_basis,
        },
      }),
    [name, enabled, sectionIds, runIntervalHours, activeWindow, adoptExisting, setConfig],
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
        values: setConfig.selection_mode === "all" ? [] : setConfig.values,
        instance_key: setConfig.category === "tag" ? setConfig.instance_key : null,
        release_basis: setConfig.category === "release_timing" ? setConfig.release_basis : null,
      };
      const result = await checkCollectionTitleConflicts({
        plex_section_id: sectionId,
        plex_section_ids: sectionIds,
        plex_section_type: sectionType,
        collection_title: pattern,
        definition: setDefinition(config, adoptExisting),
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
    if (!hasLibrary) {
      setBuilderMeta(null);
      return;
    }
    getCollectionBuilderMeta(sectionType)
      .then(setBuilderMeta)
      .catch(() => setBuilderMeta(null));
  }, [hasLibrary, sectionType]);

  const catalogOptions = useMemo(() => {
    if (setConfig.category === "genre") {
      return (builderMeta?.genres || []).map((g) => ({ id: g, label: g }));
    }
    if (setConfig.category === "content_rating") {
      return (builderMeta?.certifications || []).map((c) => ({ id: c, label: c }));
    }
    if (setConfig.category === "decade") {
      return (builderMeta?.decades || []).map((d) => ({ id: d, label: d }));
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
    return [] as { id: string; label: string }[];
  }, [builderMeta, hasLibrary, setConfig.category, setConfig.instance_key]);

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
        definition: setDefinition(setConfig),
      });
      setPreview(result);
    } catch (err) {
      setPreview(null);
      setPreviewError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setPreviewLoading(false);
    }
  }, [sectionId, sectionIds, sectionType, setConfig]);

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
    (setConfig.selection_mode !== "include" || setConfig.values.length > 0) &&
    (setConfig.category !== "tag" || Boolean(setConfig.instance_key));

  const handleSave = () => {
    if (!canSave || !sectionId) return;
    const pattern = setConfig.title_pattern.trim() || defaultSetConfig(setConfig.category).title_pattern;
    const { dimension: _omitLegacy, ...rest } = setConfig;
    const config: CollectionSetConfig = {
      ...rest,
      category: setConfig.category,
      title_pattern: pattern,
      values: setConfig.selection_mode === "all" ? [] : setConfig.values,
      instance_key: setConfig.category === "tag" ? setConfig.instance_key : null,
      release_basis: setConfig.category === "release_timing" ? setConfig.release_basis : null,
    };
    props.onSave({
      name: name.trim(),
      enabled,
      plex_section_id: sectionId,
      plex_section_ids: sectionIds,
      plex_section_type: sectionType,
      collection_title: pattern,
      definition: setDefinition(config, adoptExisting),
      run_interval_hours: runIntervalHours,
      active_window: activeWindow,
    });
  };

  const categoryMeta = CATEGORY_OPTIONS.find((d) => d.value === setConfig.category) ?? CATEGORY_OPTIONS[0];

  return (
    <div className="flex flex-col gap-4 pb-10">
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
            className="rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider text-[#0a0e14] disabled:opacity-40"
            style={{ backgroundColor: accentHex }}
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
          <div className="flex flex-col gap-1.5 min-w-[16rem]">
            <span className={theme.label}>Plex libraries</span>
            <div className="flex flex-col gap-1 max-h-40 overflow-y-auto pr-1">
              {props.sections.map((s) => {
                const selected = sectionIds.includes(s.id);
                const typeLocked = sectionType && s.type !== sectionType && sectionIds.length > 0;
                return (
                  <label
                    key={s.id}
                    className={`flex items-center gap-2 text-[13px] ${typeLocked ? "opacity-40 pointer-events-none" : "cursor-pointer"} ${theme.label}`}
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={typeLocked}
                      onChange={() => {
                        setSectionIds((prev) => {
                          if (prev.includes(s.id)) return prev.filter((id) => id !== s.id);
                          if (prev.length && s.type !== sectionType) return prev;
                          return [...prev, s.id];
                        });
                      }}
                    />
                    <span>
                      {s.title}{" "}
                      <span className="opacity-70">
                        ({s.type === "movie" ? "Movies" : "TV"}, {s.item_count})
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
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
          <label className={`flex items-center gap-2 ${theme.label} cursor-pointer select-none`}>
            <span>Enabled (runs on schedule)</span>
            <ToggleSwitch
              checked={enabled}
              onChange={setEnabled}
              accentHex={accentHex}
              ariaLabel="Enable scheduled runs"
            />
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-4">
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Schedule
            <select
              className={theme.selectField}
              value={runIntervalHours == null ? "" : String(runIntervalHours)}
              onChange={(e) => setRunIntervalHours(e.target.value === "" ? null : Number(e.target.value))}
            >
              {SCHEDULE_PRESETS.map((preset) => (
                <option key={preset.value || "default"} value={preset.value}>
                  {preset.label}
                </option>
              ))}
            </select>
          </label>
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
                <select
                  className={theme.selectField}
                  value={activeWindow.when_inactive}
                  onChange={(e) =>
                    setActiveWindow({
                      ...activeWindow,
                      when_inactive:
                        e.target.value === "delete"
                          ? "delete"
                          : e.target.value === "clear"
                            ? "clear"
                            : "keep",
                    })
                  }
                >
                  <option value="keep">Keep collection as-is</option>
                  <option value="clear">Empty the collection</option>
                  <option value="delete">Delete the collection</option>
                </select>
              </label>
              {activeWindow.when_inactive === "delete" ? (
                <span className={`text-[12px] font-normal normal-case tracking-normal ${theme.muted}`}>
                  Only collections Placeholdarr owns are deleted.
                </span>
              ) : null}
            </>
          ) : null}
        </div>
      </div>

      <div className={`${theme.blockCard} overflow-hidden`}>
        <div className={theme.blockHeader}>
          <span
            className="material-symbols-outlined inline-flex h-8 w-8 items-center justify-center rounded-full"
            style={{ fontSize: 18, color: accentHex, backgroundColor: `${accentHex}1f` }}
          >
            category
          </span>
          <div>
            <div className={theme.blockTitle}>Category</div>
            <div className={theme.blockSubtitle}>{categoryMeta.description}</div>
          </div>
        </div>
        <div className="px-4 py-3 flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {CATEGORY_OPTIONS.map((opt) => {
              const active = setConfig.category === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() =>
                    setSetConfig((prev) => ({
                      ...defaultSetConfig(opt.value),
                      selection_mode: prev.selection_mode,
                      values: [],
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
                    active ? "text-[#0a0e14] border-transparent" : theme.chipInactive
                  }`}
                  style={active ? { backgroundColor: accentHex } : undefined}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>

          {setConfig.category === "tag" ? (
            hasLibrary ? (
              <label className={`flex items-center gap-2 ${theme.label}`}>
                *arr instance
                <select
                  className={theme.selectField}
                  value={setConfig.instance_key ?? ""}
                  onChange={(e) =>
                    setSetConfig((prev) => ({ ...prev, instance_key: e.target.value || null, values: [] }))
                  }
                >
                  <option value="">Select instance…</option>
                  {arrInstances.map((inst) => (
                    <option key={inst.instance_key} value={inst.instance_key}>
                      {inst.label || inst.instance_key}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <p className={`text-[13px] ${theme.muted}`}>
                Select a Plex library first to choose a matching Radarr or Sonarr instance.
              </p>
            )
          ) : null}

          {setConfig.category === "release_timing" ? (
            <label className={`flex items-center gap-2 ${theme.label}`}>
              Based on
              <select
                className={theme.selectField}
                value={setConfig.release_basis ?? (sectionType === "show" ? "premiered" : "theater")}
                onChange={(e) => setSetConfig((prev) => ({ ...prev, release_basis: e.target.value }))}
              >
                {releaseBasisOptions.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

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
                    className="px-3 py-1.5 rounded-md text-[12px] font-headline uppercase tracking-wider text-white"
                    style={{ backgroundColor: accentHex }}
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
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Values
            <select
              className={theme.selectField}
              value={setConfig.selection_mode}
              onChange={(e) =>
                setSetConfig((prev) => ({
                  ...prev,
                  selection_mode: e.target.value as CollectionSetConfig["selection_mode"],
                }))
              }
            >
              <option value="all">All values</option>
              <option value="include">Only selected values</option>
              <option value="exclude">All except selected values</option>
            </select>
          </label>
          {setConfig.selection_mode !== "all" ? (
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
                        active ? "text-[#0a0e14] border-transparent" : theme.chipInactive
                      }`}
                      style={active ? { backgroundColor: accentHex } : undefined}
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
            <p className={`text-[13px] ${theme.muted}`}>
              Will create a collection for each of {catalogOptions.length || "—"}{" "}
              {categoryMeta.label.toLowerCase()}
              {catalogOptions.length === 1 ? "" : "s"}.
            </p>
          )}
        </div>
      </div>

      <div className={`${theme.blockCard} overflow-hidden`}>
        <div className={theme.blockHeader}>
          <span
            className="material-symbols-outlined inline-flex h-8 w-8 items-center justify-center rounded-full"
            style={{ fontSize: 18, color: accentHex, backgroundColor: `${accentHex}1f` }}
          >
            preview
          </span>
          <div>
            <div className={theme.blockTitle}>Preview</div>
            <div className={theme.blockSubtitle}>
              Titles in each selected Plex library that would join each collection. Row counts are unique across
              those libraries.
            </div>
          </div>
          <button
            type="button"
            className={`${theme.cancelButton} ml-auto`}
            onClick={() => void runPreview()}
            disabled={previewLoading}
          >
            {previewLoading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <div className="px-4 py-3">
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
                    <th className={`py-2 text-right font-normal ${theme.muted}`}>In libraries</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.set_collections.map((row) => (
                    <tr key={row.title} className={`border-b ${theme.divider}`}>
                      <td className="py-2">{row.title}</td>
                      <td className={`py-2 ${theme.muted}`}>{row.value}</td>
                      <td className="py-2 text-right font-mono">{row.selected}</td>
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
    </div>
  );
}
