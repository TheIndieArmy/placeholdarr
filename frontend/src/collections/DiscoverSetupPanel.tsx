import { useCallback, useEffect, useMemo, useState } from "react";
import type { ThemeMode } from "../brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import {
  applyDiscoverPreset,
  getCollectionBuilderMeta,
  getCollectionPlexSections,
  getCollectionTmdbMeta,
  getDiscoverPresetPreview,
  type DiscoverPresetShelf,
  type DiscoverShelfLayout,
} from "../api/collections";
import type { CollectionBuilderMeta, PlexSectionOption } from "../types/api";
import { CollectionValueMultiSelect, mergeUniqueSorted } from "./CollectionValueMultiSelect";
import {
  MOVIE_SHELF_ORDER,
  SHOW_SHELF_ORDER,
  STARTER_PRESET_CHOICES,
  sortShelvesByOrder,
  valuesFromStarterPresets,
  type DiscoverStarterPresetKey,
} from "./discoverStarterSets";
import { getCollectionTheme } from "./collectionTheme";
import { CollectionAutoAddSection } from "./CollectionAutoAddSection";
import {
  buildAutoAddPayload,
  isCollectionAutoAddReady,
} from "./collectionAutoAddUtils";
import type { CollectionAutoAddConfig } from "../types/api";
import { collectionArrangeSortOptions } from "./collectionArrangeOptions";
import { BuilderSection } from "./next/BuilderSection";
import { BuilderShell } from "./next/BuilderShell";
import { StickyActionBar } from "./next/StickyActionBar";
import { ChildCollectionList } from "./next/ChildCollectionList";
import { RecipePreviewResultView } from "./next/RecipePreviewResultView";
import { ChildSwitcher } from "./next/ChildSwitcher";
import { managedPlexSections, managedRoleLabel, selectablePlexSections } from "./managedSections";
import { useDockPrefs } from "./next/dockPrefs";
import { buildFilmstrip, filmstripForLibrary } from "./next/filmstrip";
import { PlexRowMock } from "./next/PlexRowMock";
import { PreviewDock } from "./next/PreviewDock";
import { PreviewSheet } from "./next/PreviewSheet";
import { useDiscoverShelfPreview, useRecipePreview } from "./next/useRecipePreview";

const LAYOUT_CLASS = "w-full min-w-[max(100%,52rem)] max-w-4xl mx-auto pb-16";

function arrangeOptionsForShelf(shelf: DiscoverPresetShelf): { value: string; label: string }[] {
  return collectionArrangeSortOptions(shelf.plex_section_type === "show" ? "show" : "movie");
}

/** Prefer a controlled placeholder library, then any managed library of that type. */
function defaultPlaceholderSectionId(sections: PlexSectionOption[], type: "movie" | "show"): number | "" {
  const typed = managedPlexSections(sections).filter((s) => s.type === type);
  if (!typed.length) {
    const fallback = sections.filter((s) => s.type === type);
    return fallback[0]?.id ?? "";
  }
  const placeholderRole = typed.find((s) => s.managed_role === "placeholder" || s.managed_role === "both");
  if (placeholderRole) return placeholderRole.id;
  const byName = typed.find((s) => /placeholder/i.test(s.title));
  return byName?.id ?? typed[0].id;
}

function shelfLabel(shelf: DiscoverPresetShelf): string {
  return shelf.name || shelf.collection_title || shelf.shelf_id;
}

function shelfSubtext(shelf: DiscoverPresetShelf): string {
  const fromPreset = String(shelf.description || "").trim();
  if (fromPreset) return fromPreset;
  const isSet = shelf.kind === "collection_set";
  if (isSet && shelf.set_category === "genre") {
    return "Collection set: one Plex collection per genre (use starter sets or the list below).";
  }
  return "One TMDB-backed Plex collection synced on a schedule.";
}

const DISCOVER_MOVIE_RELEASE_BASES: { value: string; label: string }[] = [
  { value: "theater", label: "Theatrical" },
  { value: "digital", label: "Digital" },
  { value: "physical", label: "Physical" },
];

const DISCOVER_SHOW_RELEASE_BASES: { value: string; label: string }[] = [
  { value: "premiered", label: "Series premiere" },
  { value: "latest_episode", label: "Latest episode" },
  { value: "latest_season", label: "Latest season premiere" },
];

function releaseBasisOptionsForShelf(shelf: DiscoverPresetShelf): { value: string; label: string }[] {
  return shelf.plex_section_type === "show" ? DISCOVER_SHOW_RELEASE_BASES : DISCOVER_MOVIE_RELEASE_BASES;
}

function defaultReleaseBasisForShelf(
  shelf: DiscoverPresetShelf,
  previewMovieDefault: string | undefined,
): string {
  const fromShelf = String(shelf.default_release_basis || "").trim();
  if (fromShelf) return fromShelf;
  if (shelf.plex_section_type === "movie") {
    return previewMovieDefault || "theater";
  }
  return "premiered";
}

export function DiscoverSetupPanel(props: {
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
  onDone: () => void;
  onOpenPlexSettings?: () => void;
  onOpenOrdering?: () => void;
  uiVariant?: "classic" | "next";
}) {
  const useNextLayout = props.uiVariant === "next";
  const [previewShelfId, setPreviewShelfId] = useState<string | null>(null);
  const accentHex = props.accent.hex;
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);

  const [sections, setSections] = useState<PlexSectionOption[]>([]);
  const [sectionsError, setSectionsError] = useState<string | null>(null);
  const [sectionsLoaded, setSectionsLoaded] = useState(false);
  const [shelves, setShelves] = useState<DiscoverPresetShelf[]>([]);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [movieSectionId, setMovieSectionId] = useState<number | "">("");
  const [showSectionId, setShowSectionId] = useState<number | "">("");
  const [selectedShelfIds, setSelectedShelfIds] = useState<Set<string>>(new Set());
  const [activeMovieGenrePresets, setActiveMovieGenrePresets] = useState<Set<DiscoverStarterPresetKey>>(
    () => new Set(["popular"]),
  );
  const [activeShowGenrePresets, setActiveShowGenrePresets] = useState<Set<DiscoverStarterPresetKey>>(
    () => new Set(["popular"]),
  );
  const [movieGenreOverride, setMovieGenreOverride] = useState<string[] | null>(null);
  const [showGenreOverride, setShowGenreOverride] = useState<string[] | null>(null);
  const [movieBuilderMeta, setMovieBuilderMeta] = useState<CollectionBuilderMeta | null>(null);
  const [showBuilderMeta, setShowBuilderMeta] = useState<CollectionBuilderMeta | null>(null);
  const [previewTmdbMovieGenres, setPreviewTmdbMovieGenres] = useState<string[]>([]);
  const [previewTmdbShowGenres, setPreviewTmdbShowGenres] = useState<string[]>([]);
  const [genrePresetsMovie, setGenrePresetsMovie] = useState<Partial<Record<DiscoverStarterPresetKey, string[]>> | null>(
    null,
  );
  const [genrePresetsShow, setGenrePresetsShow] = useState<Partial<Record<DiscoverStarterPresetKey, string[]>> | null>(
    null,
  );
  const [applyRowOrder, setApplyRowOrder] = useState(true);
  const [defaultCollectionLimit, setDefaultCollectionLimit] = useState(20);
  const [shelfLayout, setShelfLayout] = useState<Record<string, DiscoverShelfLayout>>({});
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [autoAddMovie, setAutoAddMovie] = useState<CollectionAutoAddConfig | null>(null);
  const [autoAddShow, setAutoAddShow] = useState<CollectionAutoAddConfig | null>(null);

  const plexReady = sectionsLoaded && !sectionsError && sections.length > 0;
  const focusedShelf =
    shelves.find((s) => s.shelf_id === previewShelfId) ??
    shelves.find((s) => selectedShelfIds.has(s.shelf_id)) ??
    shelves[0] ??
    null;

  const movieShelves = useMemo(
    () => sortShelvesByOrder(
      shelves.filter((s) => s.plex_section_type === "movie"),
      MOVIE_SHELF_ORDER,
    ),
    [shelves],
  );
  const showShelves = useMemo(
    () => sortShelvesByOrder(
      shelves.filter((s) => s.plex_section_type === "show"),
      SHOW_SHELF_ORDER,
    ),
    [shelves],
  );
  const movieRowShelves = useMemo(() => movieShelves.filter((s) => s.kind === "recipe"), [movieShelves]);
  const movieSetShelves = useMemo(() => movieShelves.filter((s) => s.kind === "collection_set"), [movieShelves]);
  const showRowShelves = useMemo(() => showShelves.filter((s) => s.kind === "recipe"), [showShelves]);
  const showSetShelves = useMemo(() => showShelves.filter((s) => s.kind === "collection_set"), [showShelves]);

  const movieGenresShelf = useMemo(
    () => movieShelves.find((s) => s.set_category === "genre"),
    [movieShelves],
  );
  const showGenresShelf = useMemo(
    () => showShelves.find((s) => s.set_category === "genre"),
    [showShelves],
  );

  const movieGenresSelected = movieGenresShelf ? selectedShelfIds.has(movieGenresShelf.shelf_id) : false;
  const showGenresSelected = showGenresShelf ? selectedShelfIds.has(showGenresShelf.shelf_id) : false;

  const movieGenreOptions = useMemo(
    () => mergeUniqueSorted(previewTmdbMovieGenres, movieBuilderMeta?.genres),
    [previewTmdbMovieGenres, movieBuilderMeta?.genres],
  );
  const showGenreOptions = useMemo(
    () => mergeUniqueSorted(previewTmdbShowGenres, showBuilderMeta?.genres),
    [previewTmdbShowGenres, showBuilderMeta?.genres],
  );

  const movieGenreValues = useMemo(() => {
    if (movieGenreOverride) return movieGenreOverride;
    return valuesFromStarterPresets(activeMovieGenrePresets, genrePresetsMovie, movieGenreOptions, []);
  }, [movieGenreOverride, activeMovieGenrePresets, genrePresetsMovie, movieGenreOptions]);
  const showGenreValues = useMemo(() => {
    if (showGenreOverride) return showGenreOverride;
    return valuesFromStarterPresets(activeShowGenrePresets, genrePresetsShow, showGenreOptions, []);
  }, [showGenreOverride, activeShowGenrePresets, genrePresetsShow, showGenreOptions]);
  const toggleStarterPreset = useCallback(
    (
      key: DiscoverStarterPresetKey,
      active: Set<DiscoverStarterPresetKey>,
      setActive: (next: Set<DiscoverStarterPresetKey>) => void,
      clearOverride: () => void,
    ) => {
      const next = new Set(active);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      setActive(next);
      clearOverride();
    },
    [],
  );

  const movieShelfSelected = useMemo(
    () => movieShelves.some((s) => selectedShelfIds.has(s.shelf_id)),
    [movieShelves, selectedShelfIds],
  );
  const showShelfSelected = useMemo(
    () => showShelves.some((s) => selectedShelfIds.has(s.shelf_id)),
    [showShelves, selectedShelfIds],
  );

  useEffect(() => {
    getCollectionPlexSections()
      .then((payload) => {
        setSections(payload.sections);
        setSectionsError(null);
      })
      .catch((err) => {
        setSections([]);
        setSectionsError(err instanceof Error ? err.message : "Plex libraries unavailable");
      })
      .finally(() => setSectionsLoaded(true));

    void getDiscoverPresetPreview()
      .then((preview) => {
        const list = preview.shelves?.length ? preview.shelves : [];
        setShelves(list);
        const layout: Record<string, DiscoverShelfLayout> = {};
        const capDefault = preview.default_limit ?? 20;
        const movieBasisDefault = preview.default_movie_release_basis;
        for (const s of list) {
          const row: DiscoverShelfLayout = {
            sort: String(s.default_sort || "popularity"),
            limit: Number(s.default_limit ?? capDefault),
          };
          if (s.release_basis_configurable) {
            row.release_basis = defaultReleaseBasisForShelf(s, movieBasisDefault);
          }
          layout[s.shelf_id] = row;
        }
        setShelfLayout(layout);
        setPreviewError(null);
        const allIds = new Set(list.map((s) => s.shelf_id));
        setSelectedShelfIds(allIds);
        setPreviewTmdbMovieGenres(preview.tmdb_genres?.movie || []);
        setPreviewTmdbShowGenres(preview.tmdb_genres?.show || []);
        setGenrePresetsMovie(preview.genre_presets?.movie || null);
        setGenrePresetsShow(preview.genre_presets?.show || null);
        if (preview.default_limit) setDefaultCollectionLimit(preview.default_limit);
      })
      .catch((err) => {
        setPreviewError(err instanceof Error ? err.message : "Failed to load Discover preset");
      });
  }, []);

  useEffect(() => {
    if (!plexReady) return;
    void Promise.all([
      getCollectionBuilderMeta("movie"),
      getCollectionBuilderMeta("show"),
      getCollectionTmdbMeta("movie", "US").catch(() => null),
      getCollectionTmdbMeta("tv", "US").catch(() => null),
    ]).then(([movieMeta, showMeta, tmdbMovie, tmdbTv]) => {
      setMovieBuilderMeta(movieMeta);
      setShowBuilderMeta(showMeta);
      if (tmdbMovie?.genres?.length) {
        const names = tmdbMovie.genres.map((g) => g.name).filter(Boolean);
        setPreviewTmdbMovieGenres((prev) => mergeUniqueSorted(prev, names));
      }
      if (tmdbTv?.genres?.length) {
        const names = tmdbTv.genres.map((g) => g.name).filter(Boolean);
        setPreviewTmdbShowGenres((prev) => mergeUniqueSorted(prev, names));
      }
    });
  }, [plexReady]);

  useEffect(() => {
    if (movieSectionId === "" && sections.length) {
      setMovieSectionId(defaultPlaceholderSectionId(sections, "movie"));
    }
    if (showSectionId === "" && sections.length) {
      setShowSectionId(defaultPlaceholderSectionId(sections, "show"));
    }
  }, [sections, movieSectionId, showSectionId]);

  const toggleShelf = useCallback((shelfId: string) => {
    setSelectedShelfIds((prev) => {
      const next = new Set(prev);
      if (next.has(shelfId)) next.delete(shelfId);
      else next.add(shelfId);
      return next;
    });
  }, []);

  const toggleAllForType = useCallback((type: "movie" | "show", on: boolean) => {
    const ids = shelves.filter((s) => s.plex_section_type === type).map((s) => s.shelf_id);
    setSelectedShelfIds((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }, [shelves]);

  const reviewMovieCount = movieShelves.filter((s) => selectedShelfIds.has(s.shelf_id)).length;
  const reviewShowCount = showShelves.filter((s) => selectedShelfIds.has(s.shelf_id)).length;

  const movieAutoAddOk = !movieShelfSelected || isCollectionAutoAddReady(autoAddMovie);
  const showAutoAddOk = !showShelfSelected || isCollectionAutoAddReady(autoAddShow);

  const canApply =
    plexReady &&
    selectedShelfIds.size > 0 &&
    (!movieShelfSelected || movieSectionId !== "") &&
    (!showShelfSelected || showSectionId !== "") &&
    (!movieGenresSelected || movieGenreValues.length > 0) &&
    (!showGenresSelected || showGenreValues.length > 0) &&
    movieAutoAddOk &&
    showAutoAddOk;

  const focusedType: "movie" | "show" = focusedShelf?.plex_section_type === "show" ? "show" : "movie";
  const focusedIsSet = focusedShelf?.kind === "collection_set";
  const focusedAutoAdd = focusedType === "show" ? autoAddShow : autoAddMovie;
  const focusedAutoAddReady = Boolean(focusedAutoAdd?.enabled) && isCollectionAutoAddReady(focusedAutoAdd);
  const focusedSectionId: number | "" = !focusedShelf ? "" : focusedType === "show" ? showSectionId : movieSectionId;
  const focusedSectionIds = focusedSectionId === "" ? [] : [Number(focusedSectionId)];
  const focusedSetValues =
    focusedShelf?.set_category === "genre" ? (focusedType === "show" ? showGenreValues : movieGenreValues) : undefined;
  const shelfPreview = useDiscoverShelfPreview({
    shelfId: useNextLayout && focusedShelf ? focusedShelf.shelf_id : null,
    isSet: focusedIsSet,
    sectionType: focusedType,
    sectionId: focusedSectionId,
    layout: focusedShelf ? shelfLayout[focusedShelf.shelf_id] : undefined,
    setValues: focusedSetValues,
    autoAdd: focusedAutoAdd,
    autoAddReady: focusedAutoAddReady,
  });
  const [discoverChildTitle, setDiscoverChildTitle] = useState<string | null>(null);
  const discoverChildRows = useMemo(
    () => (focusedIsSet ? (shelfPreview.preview?.set_collections ?? []) : []),
    [focusedIsSet, shelfPreview.preview],
  );
  useEffect(() => {
    if (!discoverChildRows.length) return;
    if (!discoverChildTitle || !discoverChildRows.some((r) => r.title === discoverChildTitle)) {
      setDiscoverChildTitle(discoverChildRows[0]?.title ?? null);
    }
  }, [discoverChildRows, discoverChildTitle]);
  const selectedDiscoverChild = discoverChildRows.find((r) => r.title === discoverChildTitle) ?? null;
  const discoverChildPreview = useRecipePreview(
    focusedIsSet ? (selectedDiscoverChild?.definition ?? null) : null,
    focusedSectionIds,
    focusedType,
  );
  const dockState = focusedIsSet ? discoverChildPreview : shelfPreview;
  const [sheetOpen, setSheetOpen] = useState(false);
  const [dockPrefs] = useDockPrefs();

  const handleApply = async () => {
    if (!canApply) return;
    setApplying(true);
    setApplyError(null);
    const set_values: Record<string, string[]> = {};
    if (movieGenresShelf && movieGenresSelected) {
      set_values[movieGenresShelf.shelf_id] = movieGenreValues;
    }
    if (showGenresShelf && showGenresSelected) {
      set_values[showGenresShelf.shelf_id] = showGenreValues;
    }
    try {
      const auto_add: Record<string, unknown> = {};
      const moviePayload = buildAutoAddPayload(autoAddMovie);
      const showPayload = buildAutoAddPayload(autoAddShow);
      if (moviePayload) auto_add.movie = moviePayload;
      if (showPayload) auto_add.show = showPayload;

      await applyDiscoverPreset({
        movie_section_id: movieShelfSelected ? Number(movieSectionId) : null,
        show_section_id: showShelfSelected ? Number(showSectionId) : null,
        shelf_ids: [...selectedShelfIds],
        set_values: Object.keys(set_values).length ? set_values : undefined,
        apply_row_order: applyRowOrder,
        auto_add: Object.keys(auto_add).length ? auto_add : undefined,
        shelf_layout: Object.fromEntries(
          [...selectedShelfIds].map((id) => {
            const row = shelfLayout[id];
            const shelf = shelves.find((s) => s.shelf_id === id);
            const entry: DiscoverShelfLayout = {
              sort: row?.sort || "popularity",
              limit: row?.limit ?? defaultCollectionLimit,
            };
            if (shelf?.release_basis_configurable && row?.release_basis) {
              entry.release_basis = row.release_basis;
            }
            return [id, entry];
          }),
        ),
      });
      props.onDone();
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : "Discover preset apply failed");
    } finally {
      setApplying(false);
    }
  };

  if (!sectionsLoaded) {
    return (
      <div className={LAYOUT_CLASS}>
        <p className={theme.muted}>Loading Discover setup…</p>
      </div>
    );
  }

  if (!plexReady) {
    return (
      <div className={LAYOUT_CLASS}>
        <h1 className={`text-[28px] font-black font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
          Discover setup
        </h1>
        <p className={`mt-3 text-[14px] ${theme.muted}`}>
          Connect Plex before creating Discover collections.
        </p>
        {sectionsError ? <p className={`mt-2 text-[13px] text-amber-600`}>{sectionsError}</p> : null}
        {props.onOpenPlexSettings ? (
          <button
            type="button"
            className={`mt-4 rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
            style={accentFilledStyle(accentHex)}
            onClick={() => props.onOpenPlexSettings?.()}
          >
            Connect Plex
          </button>
        ) : null}
      </div>
    );
  }

  const selectClass = `rounded-lg border px-2 py-1.5 text-[14px] w-full max-w-md ${
    isLight ? "border-slate-200 bg-white" : "border-[#424753] bg-[#12161f]"
  }`;

  function renderShelfControls(shelf: DiscoverPresetShelf, className: string) {
    const releaseBasisConfigurable = Boolean(shelf.release_basis_configurable);
    const releaseBasisOptions = releaseBasisOptionsForShelf(shelf);
    return (
            <div className={className}>
              <label className={`flex flex-col gap-0.5 text-[11px] ${theme.label}`}>
                Arrange by
                <BrandSelect
                  triggerClassName="min-w-[10rem]"
                  value={shelfLayout[shelf.shelf_id]?.sort ?? "popularity"}
                  options={arrangeOptionsForShelf(shelf)}
                  onChange={(value) =>
                    setShelfLayout((prev) => ({
                      ...prev,
                      [shelf.shelf_id]: {
                        ...prev[shelf.shelf_id],
                        sort: value,
                        limit: prev[shelf.shelf_id]?.limit ?? defaultCollectionLimit,
                      },
                    }))
                  }
                  ariaLabel={`Arrange by for ${shelfLabel(shelf)}`}
                />
              </label>
              {releaseBasisConfigurable ? (
                <label className={`flex flex-col gap-0.5 text-[11px] ${theme.label}`}>
                  Release date
                  <BrandSelect
                    triggerClassName="min-w-[9rem]"
                    value={
                      shelfLayout[shelf.shelf_id]?.release_basis ??
                      defaultReleaseBasisForShelf(shelf, undefined)
                    }
                    options={releaseBasisOptions}
                    onChange={(value) =>
                      setShelfLayout((prev) => ({
                        ...prev,
                        [shelf.shelf_id]: {
                          ...prev[shelf.shelf_id],
                          sort: prev[shelf.shelf_id]?.sort ?? String(shelf.default_sort || "popularity"),
                          limit: prev[shelf.shelf_id]?.limit ?? defaultCollectionLimit,
                          release_basis: value,
                        },
                      }))
                    }
                    ariaLabel={`Release date type for ${shelfLabel(shelf)}`}
                  />
                </label>
              ) : null}
              <label className={`flex flex-col gap-0.5 text-[11px] ${theme.label}`}>
                Items
                <input
                  type="number"
                  min={1}
                  max={500}
                  className={`${selectClass} max-w-[4.5rem] text-[13px] py-1`}
                  value={shelfLayout[shelf.shelf_id]?.limit ?? defaultCollectionLimit}
                  onChange={(e) => {
                    const next = Math.max(1, Math.min(500, Number(e.target.value) || 1));
                    setShelfLayout((prev) => ({
                      ...prev,
                      [shelf.shelf_id]: {
                        ...prev[shelf.shelf_id],
                        sort: prev[shelf.shelf_id]?.sort ?? String(shelf.default_sort || "popularity"),
                        limit: next,
                      },
                    }));
                  }}
                />
              </label>
            </div>
    );
  }

  function renderShelfRow(shelf: DiscoverPresetShelf) {
    const checked = selectedShelfIds.has(shelf.shelf_id);
    const titleClass = `text-[14px] font-semibold shrink-0 ${isLight ? "text-slate-900" : "text-slate-100"}`;
    return (
      <div
        key={shelf.shelf_id}
        className={`rounded-lg border p-2.5 ${isLight ? "border-slate-200 bg-white" : "border-[#424753]/60 bg-[#12161f]/40"}`}
      >
        <div className="flex flex-wrap items-center gap-x-2 gap-y-2">
          <BrandCheckbox
            checked={checked}
            size="sm"
            accentHex={accentHex}
            themeMode={props.themeMode}
            align="center"
            ariaLabel={`Include ${shelfLabel(shelf)}`}
            onChange={() => toggleShelf(shelf.shelf_id)}
          />
          <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className={titleClass}>{shelfLabel(shelf)}</span>
            <span className={`text-[12px] ${theme.muted} min-w-0`}>{shelfSubtext(shelf)}</span>
          </div>
          {checked
            ? renderShelfControls(
                shelf,
                "flex w-full flex-wrap items-end gap-2 pl-7 sm:pl-0 sm:ml-auto sm:w-auto lg:flex-1 lg:justify-end",
              )
            : null}
        </div>
        {checked ? <div className="mt-2">{renderShelfExtras(shelf)}</div> : null}
      </div>
    );
  }

  function shelfSummary(shelf: DiscoverPresetShelf): string {
    const row = shelfLayout[shelf.shelf_id];
    const sortLabel =
      arrangeOptionsForShelf(shelf).find((o) => o.value === (row?.sort ?? shelf.default_sort))?.label ?? "";
    const parts = [sortLabel, `${row?.limit ?? defaultCollectionLimit} items`];
    if (shelf.set_category === "genre") {
      const count = shelf.plex_section_type === "show" ? showGenreValues.length : movieGenreValues.length;
      parts.unshift(`${count} genre${count === 1 ? "" : "s"}`);
    }
    return parts.filter(Boolean).join(" · ");
  }

  function renderNextRow(shelf: DiscoverPresetShelf) {
    const checked = selectedShelfIds.has(shelf.shelf_id);
    const focused = focusedShelf?.shelf_id === shelf.shelf_id;
    return (
      <div
        key={shelf.shelf_id}
        className={`flex flex-col rounded-lg border px-2.5 py-2 transition-colors ${
          focused
            ? isLight
              ? "border-sky-300 bg-sky-50"
              : "border-[#4b5a70] bg-[#1e2430]"
            : isLight
              ? "border-slate-200 bg-white hover:bg-slate-50"
              : "border-[#424753]/50 bg-[#12161f]/40 hover:bg-[#1a2028]"
        }`}
      >
        <div className="flex items-center gap-2">
        <BrandCheckbox
          checked={checked}
          size="sm"
          accentHex={accentHex}
          themeMode={props.themeMode}
          align="center"
          ariaLabel={`Include ${shelfLabel(shelf)}`}
          onChange={() => toggleShelf(shelf.shelf_id)}
        />
        <button
          type="button"
          onClick={() => setPreviewShelfId(shelf.shelf_id)}
          className={`flex min-w-0 flex-1 items-baseline gap-2 text-left ${checked ? "" : "opacity-60"}`}
        >
          <span className={`truncate text-[14px] font-semibold ${isLight ? "text-slate-900" : "text-slate-100"}`}>
            {shelfLabel(shelf)}
          </span>
          <span className={`ml-auto shrink-0 text-[12px] ${theme.muted}`}>{checked ? shelfSummary(shelf) : "Not included"}</span>
        </button>
        <span
          className={`material-symbols-outlined shrink-0 ${focused ? "" : theme.iconMuted}`}
          style={{ fontSize: 18, color: focused ? accentHex : undefined }}
          aria-hidden
        >
          {focused && checked ? "expand_less" : "expand_more"}
        </span>
        </div>
        {focused && checked ? (
          <div className="flex flex-col gap-3 px-1 pb-1 pt-3">
            <p className={`text-[12px] ${theme.muted}`}>{shelfSubtext(shelf)}</p>
            {renderShelfControls(shelf, "flex flex-wrap items-end gap-3")}
            {renderShelfExtras(shelf)}
          </div>
        ) : null}
      </div>
    );
  }

  function renderValuePicker(
    options: string[],
    selected: string[],
    onChange: (next: string[]) => void,
    hint: string,
    searchPlaceholder: string,
    addLabel: string,
    emptyOptionsMessage?: string,
  ) {
    return (
      <CollectionValueMultiSelect
        themeMode={props.themeMode}
        accentHex={accentHex}
        options={options}
        selected={selected}
        onChange={onChange}
        hint={hint}
        searchPlaceholder={searchPlaceholder}
        addLabel={addLabel}
        emptyOptionsMessage={emptyOptionsMessage}
      />
    );
  }

  function renderStarterSetToggles(
    active: Set<DiscoverStarterPresetKey>,
    onToggle: (key: DiscoverStarterPresetKey) => void,
  ) {
    return (
      <div className="pl-7 flex flex-col gap-2 border-t border-dashed border-[#424753]/30 pt-2">
        <span className={`text-[12px] ${theme.muted}`}>Starter sets (toggle any combination):</span>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {STARTER_PRESET_CHOICES.map(({ key, label }) => (
            <label key={key} className={`flex items-center gap-2 text-[13px] cursor-pointer ${theme.label}`}>
              <BrandCheckbox
                checked={active.has(key)}
                size="sm"
                accentHex={accentHex}
                themeMode={props.themeMode}
                ariaLabel={`${label} genres`}
                onChange={() => onToggle(key)}
              />
              {label}
            </label>
          ))}
        </div>
        <span className={`text-[11px] ${theme.muted}`}>
          Starters pick which genre names appear below. Apply creates one Plex collection per name.
        </span>
      </div>
    );
  }

  function renderShelfExtras(shelf: DiscoverPresetShelf) {
    if (!selectedShelfIds.has(shelf.shelf_id)) return null;
    if (shelf.set_category === "genre" && shelf.plex_section_type === "movie" && movieGenresShelf) {
      return (
        <>
          {renderStarterSetToggles(
            activeMovieGenrePresets,
            (key) =>
              toggleStarterPreset(key, activeMovieGenrePresets, setActiveMovieGenrePresets, () => setMovieGenreOverride(null)),
          )}
          {renderValuePicker(
            movieGenreOptions,
            movieGenreValues,
            (next) => setMovieGenreOverride(next),
            "One Plex collection per selected genre. Options use TMDB's official movie genre list plus any genres in your catalog.",
            "Search genres…",
            "Add genre",
            previewTmdbMovieGenres.length
              ? "No genres match your search."
              : "Configure a TMDB API key in Settings for the full genre list from TMDB.",
          )}
        </>
      );
    }
    if (shelf.set_category === "genre" && shelf.plex_section_type === "show" && showGenresShelf) {
      return (
        <>
          {renderStarterSetToggles(
            activeShowGenrePresets,
            (key) =>
              toggleStarterPreset(key, activeShowGenrePresets, setActiveShowGenrePresets, () => setShowGenreOverride(null)),
          )}
          {renderValuePicker(
            showGenreOptions,
            showGenreValues,
            (next) => setShowGenreOverride(next),
            "One Plex collection per selected genre. Options use TMDB's official TV genre list plus any genres in your catalog.",
            "Search genres…",
            "Add genre",
            previewTmdbShowGenres.length
              ? "No genres match your search."
              : "Configure a TMDB API key in Settings for the full genre list from TMDB.",
          )}
        </>
      );
    }
    return null;
  }

  const discoverBody = (
    <div className={LAYOUT_CLASS}>
      <PresetBadge theme={theme} />
      <div className="mb-2 flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className={`text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
            Discover setup
          </h1>
          <p className={`mt-1 text-[14px] max-w-2xl ${theme.muted}`}>
            Discover collections showcase trending, popular, upcoming, and genre rows on Plex, like a streaming app home.
            Pick recipes, tune arrange-by and limits inline, then apply once. For studio or network collections, use a normal
            Collection set recipe instead.
          </p>
        </div>
        <button
          type="button"
          onClick={() => props.onDone()}
          className={`shrink-0 rounded-lg border px-3 py-2 text-[13px] font-headline uppercase tracking-wider ${
            isLight ? "border-slate-200 text-slate-700 hover:bg-slate-50" : "border-[#424753]/60 text-slate-200 hover:bg-[#1e2430]"
          }`}
        >
          Back to recipes
        </button>
      </div>

      {previewError ? (
        <p className={`mb-4 text-[13px] text-amber-600`}>{previewError}</p>
      ) : null}

      <section className={`mb-6 rounded-xl border p-5 ${theme.blockCard}`}>
        <h2 className={`text-[13px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>1 · Libraries</h2>
        <p className={`mt-1 text-[13px] ${theme.muted}`}>
          Choose the movie and TV Plex libraries where these collections should live. Placeholder libraries are selected
          when we find them in your server. Skip a side by unchecking all collections in that section.
        </p>
        <div className="mt-4 grid gap-4 sm:grid-cols-2 max-w-2xl">
          <label className={`flex flex-col gap-1 text-[13px] ${theme.label}`}>
            Movie library
            <select
              className={selectClass}
              disabled={!movieShelfSelected}
              value={movieSectionId}
              onChange={(e) => setMovieSectionId(e.target.value ? Number(e.target.value) : "")}
            >
              {sections
                .filter((s) => s.type === "movie")
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title}
                  </option>
                ))}
            </select>
          </label>
          <label className={`flex flex-col gap-1 text-[13px] ${theme.label}`}>
            TV library
            <select
              className={selectClass}
              disabled={!showShelfSelected}
              value={showSectionId}
              onChange={(e) => setShowSectionId(e.target.value ? Number(e.target.value) : "")}
            >
              {sections
                .filter((s) => s.type === "show")
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title}
                  </option>
                ))}
            </select>
          </label>
        </div>
      </section>

      <section className={`mb-6 rounded-xl border p-5 ${theme.blockCard}`}>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <h2 className={`text-[13px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>2 · Movie collections</h2>
          <div className="flex gap-2">
            <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => toggleAllForType("movie", true)}>
              Select all
            </button>
            <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => toggleAllForType("movie", false)}>
              Select none
            </button>
          </div>
        </div>
        <LibrarySkipHint theme={theme} type="movie" selected={movieShelfSelected} />
        <p className={`mt-3 text-[12px] ${theme.muted}`}>
          <span className="font-semibold text-inherit">TMDB rows</span> are single collections from TMDB.{" "}
          <span className="font-semibold text-inherit">Collection sets</span> add one Plex collection per genre you pick.
          Controls sit on the same line as each checked recipe.
        </p>
        <p className={`mt-2 text-[12px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>TMDB rows</p>
        <div className="mt-2 flex flex-col gap-2">
          {movieRowShelves.map((shelf) => renderShelfRow(shelf))}
        </div>
        <p className={`mt-4 text-[12px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>Collection sets</p>
        <div className="mt-2 flex flex-col gap-2">
          {movieSetShelves.map((shelf) => renderShelfRow(shelf))}
        </div>
      </section>

      <section className={`mb-6 rounded-xl border p-5 ${theme.blockCard}`}>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <h2 className={`text-[13px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>3 · TV collections</h2>
          <div className="flex gap-2">
            <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => toggleAllForType("show", true)}>
              Select all
            </button>
            <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => toggleAllForType("show", false)}>
              Select none
            </button>
          </div>
        </div>
        <LibrarySkipHint theme={theme} type="show" selected={showShelfSelected} />
        <p className={`mt-3 text-[12px] ${theme.muted}`}>
          Same split as movies: TMDB rows vs a per-genre collection set.
        </p>
        <p className={`mt-2 text-[12px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>TMDB rows</p>
        <div className="mt-2 flex flex-col gap-2">
          {showRowShelves.map((shelf) => renderShelfRow(shelf))}
        </div>
        <p className={`mt-4 text-[12px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>Collection sets</p>
        <div className="mt-2 flex flex-col gap-2">
          {showSetShelves.map((shelf) => renderShelfRow(shelf))}
        </div>
      </section>

      <section className={`mb-6 rounded-xl border p-5 ${theme.blockCard}`}>
        <h2 className={`text-[13px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>4 · Auto-add</h2>
        <p className={`mt-1 text-[13px] ${theme.muted}`}>
          Optional: on each sync, add Missing from ARR titles to Radarr or Sonarr for the recipes you create below.
        </p>
        {movieShelfSelected ? (
          <div className="mt-4">
            <CollectionAutoAddSection
              layout="panel"
              themeMode={props.themeMode}
              accentHex={accentHex}
              mediaType="movie"
              autoAdd={autoAddMovie}
              onChange={(next) => setAutoAddMovie(next ?? null)}
              scopeHint="Applies to every movie Discover recipe and collection set created in this run."
            />
          </div>
        ) : null}
        {showShelfSelected ? (
          <div className={movieShelfSelected ? "mt-4 pt-4 border-t border-[#424753]/30" : "mt-4"}>
            <CollectionAutoAddSection
              layout="panel"
              themeMode={props.themeMode}
              accentHex={accentHex}
              mediaType="show"
              autoAdd={autoAddShow}
              onChange={(next) => setAutoAddShow(next ?? null)}
              scopeHint="Applies to every TV Discover recipe and collection set created in this run."
            />
          </div>
        ) : null}
        {!movieShelfSelected && !showShelfSelected ? (
          <p className={`mt-3 text-[13px] ${theme.muted}`}>Select movie or TV collections above to configure auto-add.</p>
        ) : null}
      </section>

      <section className={`mb-6 rounded-xl border p-5 ${theme.blockCard}`}>
        <h2 className={`text-[13px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>5 · Options</h2>
        <p className={`mt-1 text-[13px] ${theme.muted}`}>
          Arrange by and item limits are set on each recipe above (for example release date on Upcoming, random on Genres).
        </p>
        <label className={`mt-3 flex items-start gap-2.5 text-[14px] ${theme.label}`}>
          <BrandCheckbox
            checked={applyRowOrder}
            size="sm"
            accentHex={accentHex}
            themeMode={props.themeMode}
            align="start"
            ariaLabel="Apply preset hub order in Plex"
            onChange={setApplyRowOrder}
          />
          <span>
            Apply preset Recommended row order after create
            <span className={`mt-0.5 block text-[12px] ${theme.muted}`}>
              Reorders only the Discover collections you create. Change order anytime under Collections → Ordering
              {props.onOpenOrdering ? (
                <>
                  {" "}
                  (
                  <button type="button" className="underline" onClick={() => props.onOpenOrdering?.()}>
                    open Ordering
                  </button>
                  ).
                </>
              ) : (
                "."
              )}
            </span>
          </span>
        </label>
      </section>

      <section className={`mb-8 rounded-xl border p-5 ${theme.blockCard}`}>
        <h2 className={`text-[13px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>6 · Review</h2>
        <ul className={`mt-3 text-[14px] space-y-1 ${isLight ? "text-slate-800" : "text-slate-200"}`}>
          <li>
            {reviewMovieCount} movie collection recipe{reviewMovieCount === 1 ? "" : "s"}
            {movieShelfSelected && movieSectionId !== ""
              ? ` → ${sections.find((s) => s.id === movieSectionId)?.title || "library"}`
              : " (skipped)"}
          </li>
          <li>
            {reviewShowCount} TV collection recipe{reviewShowCount === 1 ? "" : "s"}
            {showShelfSelected && showSectionId !== ""
              ? ` → ${sections.find((s) => s.id === showSectionId)?.title || "library"}`
              : " (skipped)"}
          </li>
          {movieGenresSelected ? (
            <li>
              Movie genres: {movieGenreValues.length} collection{movieGenreValues.length === 1 ? "" : "s"}
            </li>
          ) : null}
          {showGenresSelected ? (
            <li>
              TV genres: {showGenreValues.length} collection{showGenreValues.length === 1 ? "" : "s"}
            </li>
          ) : null}
        </ul>
        {applyError ? <p className={`mt-3 text-[13px] text-amber-600`}>{applyError}</p> : null}
        <button
          type="button"
          disabled={!canApply || applying}
          className={`mt-4 rounded-lg px-5 py-2.5 text-[14px] font-headline uppercase tracking-wider disabled:opacity-40 ${FG_ON_ACCENT_TEXT_CLASS}`}
          style={accentFilledStyle(accentHex)}
          onClick={() => void handleApply()}
        >
          {applying ? "Creating collections…" : "Apply Discover setup"}
        </button>
      </section>
    </div>
  );

  if (useNextLayout) {
    const movieLibraryTitle = sections.find((s) => s.id === movieSectionId)?.title;
    const showLibraryTitle = sections.find((s) => s.id === showSectionId)?.title;
    const issues = [
      selectedShelfIds.size === 0 ? "Pick at least one row" : null,
      movieShelfSelected && movieSectionId === "" ? "Pick a movie library" : null,
      showShelfSelected && showSectionId === "" ? "Pick a TV library" : null,
      movieGenresSelected && !movieGenreValues.length ? "Pick movie genres" : null,
      showGenresSelected && !showGenreValues.length ? "Pick TV genres" : null,
      !movieAutoAddOk ? "Finish Radarr targets" : null,
      !showAutoAddOk ? "Finish Sonarr targets" : null,
    ].filter((v): v is string => Boolean(v));
    const readyLabel = [
      reviewMovieCount ? `${reviewMovieCount} movie row${reviewMovieCount === 1 ? "" : "s"}` : null,
      reviewShowCount ? `${reviewShowCount} TV row${reviewShowCount === 1 ? "" : "s"}` : null,
      movieGenresSelected ? `${movieGenreValues.length} movie genre collections` : null,
      showGenresSelected ? `${showGenreValues.length} TV genre collections` : null,
    ]
      .filter(Boolean)
      .join(" · ");

    const renderTypeSection = (
      number: number,
      type: "movie" | "show",
      rowShelves: DiscoverPresetShelf[],
      setShelves: DiscoverPresetShelf[],
      count: number,
      total: number,
    ) => (
      <BuilderSection
        number={number}
        title={type === "movie" ? "Movie collections" : "TV collections"}
        themeMode={props.themeMode}
        summary={count ? `${count} of ${total} selected` : "Skipped"}
      >
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <span className={`flex-1 text-[12px] ${theme.muted}`}>
              Tick rows to create them. Click a row to tune it; the dock below previews it.
            </span>
            <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => toggleAllForType(type, true)}>
              All
            </button>
            <button type="button" className={`text-[12px] underline ${theme.muted}`} onClick={() => toggleAllForType(type, false)}>
              None
            </button>
          </div>
          <p className={`mt-1 text-[11px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>TMDB rows</p>
          {rowShelves.map((shelf) => renderNextRow(shelf))}
          {setShelves.length ? (
            <>
              <p className={`mt-2 text-[11px] font-headline uppercase tracking-widest ${theme.sectionLabel}`}>
                Collection sets
              </p>
              {setShelves.map((shelf) => renderNextRow(shelf))}
            </>
          ) : null}
        </div>
      </BuilderSection>
    );

    const nextConfig = (
      <>
        <div className="mb-1 flex flex-wrap items-start justify-between gap-3">
          <div>
            <PresetBadge theme={theme} />
            <h1 className={`text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
              Discover setup
            </h1>
            <p className={`mt-1 max-w-2xl text-[14px] ${theme.muted}`}>
              Streaming-style rows for Plex: trending, popular, upcoming, and one collection per genre. For studio or
              network collections, create a Collection set instead.
            </p>
          </div>
          <span className="rounded-full border border-sky-400/40 bg-sky-500/10 px-2 py-0.5 text-[11px] font-headline uppercase tracking-wider text-sky-300">
            New UX preview
          </span>
        </div>
        {previewError ? <p className="text-[13px] text-amber-600">{previewError}</p> : null}
        {applyError ? <p className="text-[13px] text-amber-600">{applyError}</p> : null}

        <BuilderSection
          number={1}
          title="Where"
          themeMode={props.themeMode}
          summary={[
            movieShelfSelected ? `Movies: ${movieLibraryTitle ?? "none"}` : null,
            showShelfSelected ? `TV: ${showLibraryTitle ?? "none"}` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        >
          <p className={`mb-3 text-[13px] ${theme.muted}`}>
            Controlled libraries only (placeholder destinations and Arr real-file roots). A side with no ticked rows is
            skipped. Visible on only shows items from the library you pick.
          </p>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className={`flex flex-col gap-1 text-[13px] ${theme.label}`}>
              Movie library
              <BrandSelect
                value={movieSectionId === "" ? "" : String(movieSectionId)}
                disabled={!movieShelfSelected}
                options={selectablePlexSections(sections, "movie", movieSectionId === "" ? [] : [movieSectionId]).map(
                  (s) => ({
                    value: String(s.id),
                    label: `${s.title}${managedRoleLabel(s.managed_role) ? ` (${managedRoleLabel(s.managed_role)})` : ""}`,
                  }),
                )}
                onChange={(value) => setMovieSectionId(value ? Number(value) : "")}
                ariaLabel="Movie library"
              />
            </label>
            <label className={`flex flex-col gap-1 text-[13px] ${theme.label}`}>
              TV library
              <BrandSelect
                value={showSectionId === "" ? "" : String(showSectionId)}
                disabled={!showShelfSelected}
                options={selectablePlexSections(sections, "show", showSectionId === "" ? [] : [showSectionId]).map(
                  (s) => ({
                    value: String(s.id),
                    label: `${s.title}${managedRoleLabel(s.managed_role) ? ` (${managedRoleLabel(s.managed_role)})` : ""}`,
                  }),
                )}
                onChange={(value) => setShowSectionId(value ? Number(value) : "")}
                ariaLabel="TV library"
              />
            </label>
          </div>
        </BuilderSection>

        {renderTypeSection(2, "movie", movieRowShelves, movieSetShelves, reviewMovieCount, movieShelves.length)}
        {renderTypeSection(3, "show", showRowShelves, showSetShelves, reviewShowCount, showShelves.length)}

        <BuilderSection
          number={4}
          title="Titles you don't have"
          themeMode={props.themeMode}
          defaultOpen={false}
          summary={[
            movieShelfSelected ? `Movies: ${autoAddMovie?.enabled ? "add to Radarr" : "off"}` : null,
            showShelfSelected ? `TV: ${autoAddShow?.enabled ? "add to Sonarr" : "off"}` : null,
          ]
            .filter(Boolean)
            .join(" · ") || "Off"}
        >
          <div className="flex flex-col gap-4">
            <p className={`text-[13px] ${theme.muted}`}>
              Optional: on each sync, send titles these rows find but you do not have to Radarr or Sonarr.
            </p>
            {movieShelfSelected ? (
              <CollectionAutoAddSection
                layout="panel"
                themeMode={props.themeMode}
                accentHex={accentHex}
                mediaType="movie"
                autoAdd={autoAddMovie}
                onChange={(next) => setAutoAddMovie(next ?? null)}
                scopeHint="Applies to every movie row and collection set created in this run."
              />
            ) : null}
            {showShelfSelected ? (
              <CollectionAutoAddSection
                layout="panel"
                themeMode={props.themeMode}
                accentHex={accentHex}
                mediaType="show"
                autoAdd={autoAddShow}
                onChange={(next) => setAutoAddShow(next ?? null)}
                scopeHint="Applies to every TV row and collection set created in this run."
              />
            ) : null}
            {!movieShelfSelected && !showShelfSelected ? (
              <p className={`text-[13px] ${theme.muted}`}>Tick movie or TV rows first.</p>
            ) : null}
          </div>
        </BuilderSection>

        <BuilderSection
          number={5}
          title="Where it appears"
          themeMode={props.themeMode}
          defaultOpen={false}
          summary={applyRowOrder ? "Preset Recommended row order" : "Leave Plex row order alone"}
        >
          <label className={`flex items-start gap-2.5 text-[14px] ${theme.label}`}>
            <BrandCheckbox
              checked={applyRowOrder}
              size="sm"
              accentHex={accentHex}
              themeMode={props.themeMode}
              align="start"
              ariaLabel="Apply preset hub order in Plex"
              onChange={setApplyRowOrder}
            />
            <span>
              Apply the preset Recommended row order after create
              <span className={`mt-0.5 block text-[12px] ${theme.muted}`}>
                Only reorders the Discover collections you create. Change order any time on the Ordering page.
              </span>
            </span>
          </label>
          {props.onOpenOrdering ? (
            <button type="button" className={`${theme.cancelButton} mt-3`} onClick={() => props.onOpenOrdering?.()}>
              Open Ordering
            </button>
          ) : null}
        </BuilderSection>
      </>
    );

    const rowLabel = (shelf: DiscoverPresetShelf) =>
      `${shelf.plex_section_type === "show" ? "TV" : "Movies"}: ${shelfLabel(shelf)}`;
    const focusedTitle = focusedIsSet
      ? (selectedDiscoverChild?.title ?? "")
      : (focusedShelf?.collection_title ?? (focusedShelf ? shelfLabel(focusedShelf) : ""));
    const dockSwitcher = (
      <>
        <BrandSelect
          triggerClassName="min-w-[12rem] max-w-[18rem]"
          value={focusedShelf?.shelf_id ?? ""}
          options={shelves.map((s) => ({
            value: s.shelf_id,
            label: `${rowLabel(s)}${selectedShelfIds.has(s.shelf_id) ? "" : " (not included)"}`,
          }))}
          onChange={(value) => setPreviewShelfId(value)}
          ariaLabel="Row to preview"
        />
        {focusedIsSet ? (
          <ChildSwitcher
            themeMode={props.themeMode}
            rows={discoverChildRows}
            selectedTitle={discoverChildTitle}
            onSelect={setDiscoverChildTitle}
          />
        ) : null}
      </>
    );

    return (
      <BuilderShell
        config={nextConfig}
        dock={
          <PreviewDock
            themeMode={props.themeMode}
            accentHex={accentHex}
            preview={dockState.preview}
            loading={shelfPreview.loading || dockState.loading}
            error={shelfPreview.error || dockState.error}
            emptyMessage={
              !focusedShelf
                ? "Pick a row to preview it."
                : focusedSectionId === ""
                  ? `Pick a ${focusedType === "movie" ? "movie" : "TV"} library to preview.`
                  : null
            }
            autoAddActive={focusedAutoAddReady}
            libraries={
              focusedSectionId === ""
                ? []
                : sections.filter((s) => s.id === Number(focusedSectionId))
            }
            focusLibraryId={focusedSectionId === "" ? null : Number(focusedSectionId)}
            switcher={dockSwitcher}
            onExpand={() => setSheetOpen(true)}
            actions={
              <StickyActionBar
                accentHex={accentHex}
                issues={issues}
                readyLabel={readyLabel ? `Creates ${readyLabel}` : "Ready"}
                primaryLabel="Apply Discover setup"
                primaryBusyLabel="Creating collections…"
                primaryDisabled={!canApply}
                primaryLoading={applying}
                onPrimary={() => void handleApply()}
                onCancel={() => props.onDone()}
                cancelLabel="Back to recipes"
              />
            }
          />
        }
        sheet={
          <PreviewSheet
            open={sheetOpen}
            onClose={() => setSheetOpen(false)}
            themeMode={props.themeMode}
            title={`Full preview: ${focusedShelf ? rowLabel(focusedShelf) : "Discover"}`}
            headerExtra={<div className="flex items-center gap-1">{dockSwitcher}</div>}
          >
            <div className={focusedIsSet ? "grid gap-5 lg:grid-cols-[minmax(16rem,22rem)_minmax(0,1fr)]" : "flex flex-col gap-5"}>
              {focusedIsSet ? (
                <ChildCollectionList
                  themeMode={props.themeMode}
                  accentHex={accentHex}
                  rows={discoverChildRows}
                  selectedTitle={discoverChildTitle}
                  onSelect={setDiscoverChildTitle}
                  loading={shelfPreview.loading && !discoverChildRows.length}
                  heading="Collections this set builds"
                />
              ) : null}
              <div className="flex min-w-0 flex-col gap-5">
                {dockPrefs.plexMock && focusedShelf && focusedSectionId !== "" ? (
                  <PlexRowMock
                    themeMode={props.themeMode}
                    accentHex={accentHex}
                    sectionId={Number(focusedSectionId)}
                    libraryTitle={sections.find((s) => s.id === focusedSectionId)?.title}
                    collectionTitle={focusedTitle}
                    items={filmstripForLibrary(
                      buildFilmstrip(dockState.preview, dockPrefs.ghosts && focusedAutoAddReady),
                      Number(focusedSectionId),
                    )}
                    promotedOnCreate={applyRowOrder}
                  />
                ) : null}
                <div className={`${theme.blockCard} p-4`}>
                  <RecipePreviewResultView
                    themeMode={props.themeMode}
                    accentHex={accentHex}
                    sections={sections}
                    sectionIds={focusedSectionIds}
                    preview={dockState.preview}
                    loading={dockState.loading}
                    error={dockState.error}
                    title={focusedTitle}
                    autoAddConfigured={Boolean(focusedAutoAdd?.enabled)}
                    autoAddActive={focusedAutoAddReady}
                    layout={dockPrefs.plexMock ? "default" : "sheet"}
                  />
                </div>
              </div>
            </div>
          </PreviewSheet>
        }
      />
    );
  }

  return discoverBody;
}

function PresetBadge({ theme }: { theme: ReturnType<typeof getCollectionTheme> }) {
  return (
    <div className="mb-1 flex items-center gap-2">
      <div className="w-2 h-2 rounded-full bg-green-500" />
      <span className={`text-[12px] font-headline uppercase tracking-widest ${theme.iconMuted}`}>Discover preset</span>
    </div>
  );
}

function LibrarySkipHint({
  theme,
  type,
  selected,
}: {
  theme: ReturnType<typeof getCollectionTheme>;
  type: "movie" | "show";
  selected: boolean;
}) {
  if (selected) return null;
  return (
    <p className={`mt-2 text-[12px] ${theme.muted}`}>
      No {type === "movie" ? "movie" : "TV"} collections selected. This side will be skipped.
    </p>
  );
}