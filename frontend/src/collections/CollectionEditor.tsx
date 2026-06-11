import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ThemeMode } from "../brandTypes";
import {
  explainCollectionItem,
  getCollectionBuilderMeta,
  getCollectionTmdbMeta,
  previewCollectionDefinition,
  type RecipeWritePayload,
} from "../api/collections";
import {
  CollectionThemeProvider,
  getCollectionTheme,
  useCollectionTheme,
} from "./collectionTheme";
import type {
  CollectionBuilderMeta,
  CollectionDefinition,
  CollectionExplainCheck,
  CollectionExplainResponse,
  CollectionFilterBlock,
  CollectionFilterField,
  CollectionPinnedItem,
  CollectionPreviewResponse,
  CollectionRecipe,
  CollectionSourceBlock,
  CollectionSourceType,
  CollectionTmdbMeta,
  LibraryItem,
  PlexSectionOption,
} from "../types/api";

const SOURCE_META: Record<
  CollectionSourceType,
  { label: string; icon: string; description: string; requires: "tmdb" | "trakt" | null }
> = {
  tmdb_trending: { label: "TMDB Trending", icon: "trending_up", description: "What's trending on TMDB right now", requires: "tmdb" },
  tmdb_popular: { label: "TMDB Popular", icon: "local_fire_department", description: "All-time popular titles on TMDB", requires: "tmdb" },
  tmdb_upcoming: { label: "TMDB Upcoming / On Air", icon: "event_upcoming", description: "Upcoming movies or currently-airing shows", requires: "tmdb" },
  tmdb_discover: { label: "TMDB Discover", icon: "travel_explore", description: "Filter TMDB by genre, year, streaming service", requires: "tmdb" },
  tmdb_list: { label: "TMDB List", icon: "format_list_bulleted", description: "A public TMDB list by ID", requires: "tmdb" },
  mdblist: { label: "MDBList", icon: "playlist_add_check", description: "A public MDBList — paste the list URL", requires: null },
  trakt_list: { label: "Trakt List", icon: "playlist_play", description: "A public Trakt user list — paste the URL or user/slug", requires: "trakt" },
  catalog: { label: "My Catalog", icon: "inventory_2", description: "Everything Placeholdarr tracks for this library type", requires: null },
};

const FILTER_META: Record<CollectionFilterField, { label: string; icon: string }> = {
  genre: { label: "Genre", icon: "theater_comedy" },
  year: { label: "Year", icon: "calendar_today" },
  certification: { label: "Certification", icon: "verified_user" },
  studio_network: { label: "Studio / Network", icon: "apartment" },
  monitored: { label: "Monitored in ARR", icon: "visibility" },
  quality_profile: { label: "Quality Profile", icon: "high_quality" },
  original_language: { label: "Original Language", icon: "language" },
  instance: { label: "ARR Instance", icon: "dns" },
  release_window: { label: "Release Window", icon: "date_range" },
  rating: { label: "Rating", icon: "star" },
};

// Legacy fields (e.g. the removed downloaded-file "quality" filter) may still exist in saved recipes.
function filterMeta(field: string): { label: string; icon: string } {
  return FILTER_META[field as CollectionFilterField] ?? { label: field, icon: "filter_alt" };
}

function defaultSourceBlock(type: CollectionSourceType): CollectionSourceBlock {
  switch (type) {
    case "tmdb_trending":
      return { type, window: "week", limit: 50 };
    case "tmdb_discover":
      return { type, genre_ids: [], provider_ids: [], watch_region: "US", limit: 100 };
    case "tmdb_list":
      return { type, list_id: "", limit: 200 };
    case "mdblist":
    case "trakt_list":
      return { type, list_ref: "", limit: 200 };
    case "catalog":
      return { type };
    default:
      return { type, limit: 100 };
  }
}

function defaultFilterBlock(field: CollectionFilterField): CollectionFilterBlock {
  switch (field) {
    case "genre":
      return { field, op: "includes_any", values: [] };
    case "year":
      return { field, op: "gte", value: 2000 };
    case "certification":
      return { field, op: "in", values: [] };
    case "studio_network":
      return { field, op: "contains", value: "" };
    case "monitored":
      return { field, op: "is", value: true };
    case "quality_profile":
      return { field, op: "in", values: [] };
    case "original_language":
      return { field, op: "in", values: [] };
    case "instance":
      return { field, op: "is", value: "" };
    case "release_window":
      return { field, op: "within_past", value: 365 };
    case "rating":
      return { field, op: "gte", value: 7 };
  }
}

const chipBase =
  "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[12px] font-headline uppercase tracking-wider cursor-pointer transition-colors border";

function NumberInput(props: {
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  placeholder?: string;
  min?: number;
  max?: number;
  width?: number;
}) {
  const theme = useCollectionTheme();
  return (
    <input
      type="number"
      className={theme.field}
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

function MultiChipPicker(props: {
  options: { key: string; label: string }[];
  selected: string[];
  accentHex: string;
  onToggle: (key: string) => void;
  emptyHint: string;
}) {
  const theme = useCollectionTheme();
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? props.options : props.options.slice(0, 14);
  if (!props.options.length) {
    return <span className={theme.muted}>{props.emptyHint}</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {visible.map((opt) => {
        const active = props.selected.includes(opt.key);
        return (
          <button
            key={opt.key}
            type="button"
            onClick={() => props.onToggle(opt.key)}
            className={`${chipBase} ${
              active ? "text-[#0a0e14] border-transparent" : theme.chipInactive
            }`}
            style={active ? { backgroundColor: props.accentHex } : undefined}
          >
            {opt.label}
          </button>
        );
      })}
      {props.options.length > 14 ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className={`${chipBase} ${theme.chipShowMore}`}
        >
          {expanded ? "Show less" : `+${props.options.length - 14} more`}
        </button>
      ) : null}
    </div>
  );
}

function BlockCard(props: {
  icon: string;
  title: string;
  subtitle?: string;
  accentHex: string;
  onRemove?: () => void;
  children?: ReactNode;
  /** Set when the card body contains dropdowns that must escape the card bounds. */
  overflowVisible?: boolean;
}) {
  const theme = useCollectionTheme();
  return (
    <div className={`${theme.blockCard} ${props.overflowVisible ? "" : "overflow-hidden"}`}>
      <div className={`${theme.blockHeader} ${props.overflowVisible ? "rounded-t-[11px]" : ""}`}>
        <span
          className="material-symbols-outlined rounded-md p-1"
          style={{ fontSize: 18, color: props.accentHex, backgroundColor: `${props.accentHex}1f` }}
        >
          {props.icon}
        </span>
        <div className="flex-1 min-w-0">
          <div className={theme.blockTitle}>{props.title}</div>
          {props.subtitle ? <div className={theme.blockSubtitle}>{props.subtitle}</div> : null}
        </div>
        {props.onRemove ? (
          <button
            type="button"
            onClick={props.onRemove}
            className={`material-symbols-outlined transition-colors ${theme.iconAction}`}
            style={{ fontSize: 18 }}
            title="Remove block"
          >
            close
          </button>
        ) : null}
      </div>
      {props.children ? <div className="px-4 py-3">{props.children}</div> : null}
    </div>
  );
}

function PipelineConnector(props: { label: string; accentHex: string }) {
  const theme = useCollectionTheme();
  return (
    <div className="flex flex-col items-center py-1">
      <div className={`w-px h-3 ${theme.connectorLine}`} />
      <span className={theme.connectorPill} style={{ color: props.accentHex }}>
        {props.label}
      </span>
      <div className={`w-px h-3 ${theme.connectorLine}`} />
    </div>
  );
}

function AddBlockMenu(props: {
  label: string;
  options: { key: string; label: string; icon: string; description?: string; disabled?: boolean }[];
  onAdd: (key: string) => void;
}) {
  const theme = useCollectionTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => setOpen((v) => !v)} className={theme.dashedButton}>
        <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
          add
        </span>
        {props.label}
      </button>
      {open ? (
        <div className={theme.dropdown}>
          {props.options.map((opt) => (
            <button
              key={opt.key}
              type="button"
              disabled={opt.disabled}
              onClick={() => {
                props.onAdd(opt.key);
                setOpen(false);
              }}
              className={`${theme.dropdownItem} items-start gap-2.5 px-3.5 py-2.5`}
            >
              <span className={`material-symbols-outlined mt-0.5 ${theme.iconMuted}`} style={{ fontSize: 17 }}>
                {opt.icon}
              </span>
              <span>
                <span className={theme.dropdownItemTitle}>{opt.label}</span>
                {opt.description ? <span className={theme.dropdownItemDescription}>{opt.description}</span> : null}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function pinKey(item: { tmdb_id?: number | null; tvdb_id?: number | null; imdb_id?: string | null }): string {
  return `${item.tmdb_id ?? ""}:${item.tvdb_id ?? ""}:${item.imdb_id ?? ""}`;
}

const EXPLAIN_STAGE_LABELS: Record<string, string> = {
  sources: "Produced by a source",
  catalog: "Matched in catalog",
  filters: "Passes filters",
  pins: "Pins",
  limit: "Survives sort + limit",
  library: "In target Plex library",
};

const FILTER_OP_LABELS: Record<string, string> = {
  includes_any: "includes any of",
  excludes: "excludes all of",
  gte: "at least",
  lte: "at most",
  between: "between",
  in: "is one of",
  not_in: "is none of",
  contains: "contains",
  not_contains: "does not contain",
  is: "is",
  within_past: "released in the past (days)",
  within_next: "releasing in the next (days)",
};

function explainSourceCheckLabel(check: CollectionExplainCheck): string {
  const meta = SOURCE_META[check.type as CollectionSourceType];
  const base = meta?.label ?? check.type ?? "Source";
  return check.list_ref ? `${base} — ${check.list_ref}` : base;
}

function explainRuleCheckLabel(check: CollectionExplainCheck): string {
  const base = filterMeta(String(check.field ?? "")).label;
  const op = FILTER_OP_LABELS[String(check.op ?? "")] ?? String(check.op ?? "");
  let value = "";
  if (check.values?.length) value = check.values.join(", ");
  else if (check.value != null && check.value !== "") value = String(check.value);
  if (check.value_to != null) value = `${value}–${check.value_to}`;
  return [base, op, value].filter(Boolean).join(" ");
}

function ExplainStatusIcon(props: { status: "pass" | "fail" | "skip" }) {
  if (props.status === "pass") {
    return (
      <span className="material-symbols-outlined text-emerald-400" style={{ fontSize: 16 }}>
        check_circle
      </span>
    );
  }
  if (props.status === "fail") {
    return (
      <span className="material-symbols-outlined text-red-400" style={{ fontSize: 16 }}>
        cancel
      </span>
    );
  }
  return (
    <span className="material-symbols-outlined text-slate-600" style={{ fontSize: 16 }}>
      radio_button_unchecked
    </span>
  );
}

function PinPicker(props: {
  items: CollectionPinnedItem[];
  accentHex: string;
  /** In-memory catalog (same cache the top-bar search uses), pre-filtered to the section's media type. */
  catalog: LibraryItem[];
  catalogLoading: boolean;
  placeholder: string;
  onAdd: (item: CollectionPinnedItem) => void;
  onRemove: (index: number) => void;
}) {
  const theme = useCollectionTheme();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    return props.catalog
      .filter((item) => item.title.toLowerCase().includes(needle))
      .sort((left, right) => {
        const leftStarts = left.title.toLowerCase().startsWith(needle) ? 0 : 1;
        const rightStarts = right.title.toLowerCase().startsWith(needle) ? 0 : 1;
        if (leftStarts !== rightStarts) return leftStarts - rightStarts;
        return left.title.localeCompare(right.title);
      })
      .slice(0, 10);
  }, [props.catalog, query]);

  const pinnedKeys = new Set(props.items.map(pinKey));

  return (
    <div className="flex flex-col gap-2.5">
      <div className="relative" ref={ref}>
        <div className="flex items-center gap-2">
          <span className={`material-symbols-outlined ${theme.iconMuted}`} style={{ fontSize: 17 }}>
            search
          </span>
          <input
            className={`${theme.field} flex-1`}
            value={query}
            placeholder={props.placeholder}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
          />
          {props.catalogLoading ? (
            <span className="h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: props.accentHex }} />
          ) : null}
        </div>
        {open && query.trim() && !results.length && !props.catalogLoading ? (
          <div className={theme.dropdownEmpty}>No catalog titles match.</div>
        ) : null}
        {open && results.length ? (
          <div className={`${theme.dropdown} max-h-72 overflow-y-auto`}>
            {results.map((item) => {
              const candidate: CollectionPinnedItem = {
                tmdb_id: item.tmdb_id ?? null,
                tvdb_id: item.tvdb_id ?? null,
                imdb_id: item.imdb_id ?? null,
                title: item.title,
                year: item.year ?? null,
                poster: item.poster_url ?? null,
              };
              const already = pinnedKeys.has(pinKey(candidate));
              return (
                <button
                  key={item.id}
                  type="button"
                  disabled={already}
                  onClick={() => {
                    props.onAdd(candidate);
                    setQuery("");
                    setOpen(false);
                  }}
                  className={`${theme.dropdownItem} items-center gap-2.5 px-3 py-2`}
                >
                  {item.poster_url ? (
                    <img src={item.poster_url} alt="" className={`h-10 w-7 rounded object-cover ${theme.posterFallback}`} />
                  ) : (
                    <span
                      className={`flex h-10 w-7 items-center justify-center rounded material-symbols-outlined ${theme.posterFallback}`}
                      style={{ fontSize: 14 }}
                    >
                      movie
                    </span>
                  )}
                  <span className="flex-1 min-w-0">
                    <span className={theme.dropdownItemTitle}>{item.title}</span>
                    <span className={theme.dropdownItemDescription}>{item.year ?? "—"}</span>
                  </span>
                  {already ? <span className={`text-[11px] uppercase ${theme.muted}`}>pinned</span> : null}
                </button>
              );
            })}
          </div>
        ) : null}
      </div>
      {props.items.length ? (
        <div className="flex flex-wrap gap-2">
          {props.items.map((item, index) => (
            <span key={pinKey(item)} className={theme.pinTag}>
              {item.poster ? (
                <img src={item.poster} alt="" className="h-8 w-[1.35rem] rounded-sm object-cover" />
              ) : null}
              <span className={theme.pinTitle}>
                {item.title}
                {item.year ? <span className={theme.pinYear}> ({item.year})</span> : null}
              </span>
              <button
                type="button"
                onClick={() => props.onRemove(index)}
                className={`material-symbols-outlined transition-colors ${theme.iconAction}`}
                style={{ fontSize: 15 }}
                title="Remove pin"
              >
                close
              </button>
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function CollectionEditor(props: {
  recipe: CollectionRecipe | null;
  sections: PlexSectionOption[];
  tmdbConfigured: boolean;
  traktConfigured: boolean;
  /** Shared in-memory library catalog (same data the top-bar search filters). */
  libraryItems: LibraryItem[];
  libraryLoading: boolean;
  /** Ask the app shell to load the library shelves if they aren't cached yet. */
  onEnsureLibrary: () => void;
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
  saving: boolean;
  saveError: string | null;
  onSave: (payload: RecipeWritePayload) => void;
  onCancel: () => void;
}) {
  const accentHex = props.accent.hex;
  const theme = getCollectionTheme(props.themeMode === "light");

  const [name, setName] = useState(props.recipe?.name ?? "");
  const [enabled, setEnabled] = useState(props.recipe?.enabled ?? true);
  const [sectionId, setSectionId] = useState<number | null>(props.recipe?.plex_section_id ?? null);
  const [collectionTitle, setCollectionTitle] = useState(props.recipe?.collection_title ?? "");
  const [definition, setDefinition] = useState<CollectionDefinition>(
    props.recipe?.definition && Array.isArray(props.recipe.definition.sources)
      ? props.recipe.definition
      : { sources: [defaultSourceBlock(props.tmdbConfigured ? "tmdb_trending" : "catalog")], filters: [], limit: 50, sort: "popularity" },
  );

  const section = useMemo(
    () => props.sections.find((s) => s.id === sectionId) ?? null,
    [props.sections, sectionId],
  );
  const sectionType: "movie" | "show" = section?.type ?? props.recipe?.plex_section_type ?? "movie";
  const mediaType: "movie" | "tv" = sectionType === "movie" ? "movie" : "tv";

  // TMDB metadata (genres / providers / regions) cached per media type + region.
  const [metaCache, setMetaCache] = useState<Record<string, CollectionTmdbMeta>>({});
  const metaCacheRef = useRef(metaCache);
  metaCacheRef.current = metaCache;
  const ensureMeta = useCallback(
    (mt: "movie" | "tv", region: string) => {
      const key = `${mt}:${region}`;
      if (metaCacheRef.current[key]) return;
      getCollectionTmdbMeta(mt, region)
        .then((meta) => setMetaCache((prev) => ({ ...prev, [key]: meta })))
        .catch(() => {
          /* surfaced via preview errors; chips show fallback hint */
        });
    },
    [],
  );
  useEffect(() => {
    if (props.tmdbConfigured) ensureMeta(mediaType, "US");
  }, [props.tmdbConfigured, mediaType, ensureMeta]);
  const baseMeta = metaCache[`${mediaType}:US`] ?? null;

  // Builder metadata (arr instances / quality profiles / catalog languages) cached per section type.
  const [builderMetaCache, setBuilderMetaCache] = useState<Record<string, CollectionBuilderMeta>>({});
  const builderMetaCacheRef = useRef(builderMetaCache);
  builderMetaCacheRef.current = builderMetaCache;
  useEffect(() => {
    if (builderMetaCacheRef.current[sectionType]) return;
    getCollectionBuilderMeta(sectionType)
      .then((meta) => setBuilderMetaCache((prev) => ({ ...prev, [sectionType]: meta })))
      .catch(() => {
        /* dropdowns show loading hints; not fatal */
      });
  }, [sectionType]);
  const builderMeta = builderMetaCache[sectionType] ?? null;

  // ----- definition mutation helpers -----
  const updateSource = (index: number, patch: Partial<CollectionSourceBlock>) => {
    setDefinition((prev) => ({
      ...prev,
      sources: prev.sources.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    }));
  };
  const removeSource = (index: number) => {
    setDefinition((prev) => ({ ...prev, sources: prev.sources.filter((_, i) => i !== index) }));
  };
  const updateFilter = (index: number, patch: Partial<CollectionFilterBlock>) => {
    setDefinition((prev) => ({
      ...prev,
      filters: prev.filters.map((f, i) => (i === index ? { ...f, ...patch } : f)),
    }));
  };
  const removeFilter = (index: number) => {
    setDefinition((prev) => ({ ...prev, filters: prev.filters.filter((_, i) => i !== index) }));
  };
  const addPin = (bucket: "include" | "exclude", item: CollectionPinnedItem) => {
    setDefinition((prev) => {
      const pins = prev.pins ?? {};
      const current = pins[bucket] ?? [];
      if (current.some((p) => pinKey(p) === pinKey(item))) return prev;
      return { ...prev, pins: { ...pins, [bucket]: [...current, item] } };
    });
  };
  const removePin = (bucket: "include" | "exclude", index: number) => {
    setDefinition((prev) => {
      const pins = prev.pins ?? {};
      return { ...prev, pins: { ...pins, [bucket]: (pins[bucket] ?? []).filter((_, i) => i !== index) } };
    });
  };

  const includePins = definition.pins?.include ?? [];
  const excludePins = definition.pins?.exclude ?? [];
  const [pinsExpanded, setPinsExpanded] = useState(includePins.length > 0 || excludePins.length > 0);

  // Warm the shared library cache so the pins typeahead (and top-bar search) filter in memory.
  const ensureLibraryRef = useRef(props.onEnsureLibrary);
  ensureLibraryRef.current = props.onEnsureLibrary;
  useEffect(() => {
    ensureLibraryRef.current();
  }, []);

  const pinCatalog = useMemo(
    () => props.libraryItems.filter((item) => item.type === (sectionType === "movie" ? "movie" : "series")),
    [props.libraryItems, sectionType],
  );

  // ----- live preview (debounced) -----
  const [preview, setPreview] = useState<CollectionPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewSeq = useRef(0);
  const definitionJson = JSON.stringify(definition);
  useEffect(() => {
    if (!sectionId || !definition.sources.length) {
      setPreview(null);
      return;
    }
    const seq = ++previewSeq.current;
    setPreviewLoading(true);
    setPreviewError(null);
    const timer = window.setTimeout(() => {
      previewCollectionDefinition({
        plex_section_id: sectionId,
        plex_section_type: sectionType,
        definition: JSON.parse(definitionJson) as CollectionDefinition,
      })
        .then((result) => {
          if (previewSeq.current !== seq) return;
          setPreview(result);
          setPreviewLoading(false);
        })
        .catch((err) => {
          if (previewSeq.current !== seq) return;
          setPreviewError(err instanceof Error ? err.message : "Preview failed");
          setPreviewLoading(false);
        });
    }, 900);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [definitionJson, sectionId, sectionType]);

  // ----- explain ("why isn't this title in the collection?") -----
  const [explainQuery, setExplainQuery] = useState("");
  const [explainOpen, setExplainOpen] = useState(false);
  const [explainItem, setExplainItem] = useState<CollectionPinnedItem | null>(null);
  const [explainResult, setExplainResult] = useState<CollectionExplainResponse | null>(null);
  const [explainLoading, setExplainLoading] = useState(false);
  const [explainError, setExplainError] = useState<string | null>(null);
  const explainSeq = useRef(0);
  const explainBoxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!explainOpen) return;
    const onDown = (e: MouseEvent) => {
      if (explainBoxRef.current && !explainBoxRef.current.contains(e.target as Node)) setExplainOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [explainOpen]);

  const explainResults = useMemo(() => {
    const needle = explainQuery.trim().toLowerCase();
    if (!needle) return [];
    return pinCatalog
      .filter((item) => item.title.toLowerCase().includes(needle))
      .sort((left, right) => {
        const leftStarts = left.title.toLowerCase().startsWith(needle) ? 0 : 1;
        const rightStarts = right.title.toLowerCase().startsWith(needle) ? 0 : 1;
        if (leftStarts !== rightStarts) return leftStarts - rightStarts;
        return left.title.localeCompare(right.title);
      })
      .slice(0, 8);
  }, [pinCatalog, explainQuery]);

  useEffect(() => {
    if (!sectionId || !explainItem) {
      setExplainResult(null);
      return;
    }
    const seq = ++explainSeq.current;
    setExplainLoading(true);
    setExplainError(null);
    const timer = window.setTimeout(() => {
      explainCollectionItem({
        plex_section_id: sectionId,
        plex_section_type: sectionType,
        definition: JSON.parse(definitionJson) as CollectionDefinition,
        item: explainItem,
      })
        .then((result) => {
          if (explainSeq.current !== seq) return;
          setExplainResult(result);
          setExplainLoading(false);
        })
        .catch((err) => {
          if (explainSeq.current !== seq) return;
          setExplainError(err instanceof Error ? err.message : "Check failed");
          setExplainLoading(false);
        });
    }, 600);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [definitionJson, sectionId, sectionType, explainItem]);

  const canSave =
    name.trim().length > 0 &&
    collectionTitle.trim().length > 0 &&
    sectionId !== null &&
    definition.sources.length > 0;

  // ----- render helpers -----
  function renderSourceConfig(block: CollectionSourceBlock, index: number) {
    if (block.type === "catalog") {
      return (
        <p className="text-[13px] text-slate-500">
          Starts from every {sectionType === "movie" ? "movie" : "series"} Placeholdarr tracks. Combine with filter
          blocks below to narrow it down.
        </p>
      );
    }
    const region = block.watch_region || "US";
    const regionMeta = metaCache[`${mediaType}:${region}`] ?? baseMeta;
    return (
      <div className="flex flex-col gap-3">
        {block.type === "tmdb_trending" ? (
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Window
            <select
              className={theme.selectField}
              value={block.window ?? "week"}
              onChange={(e) => updateSource(index, { window: e.target.value as "day" | "week" })}
            >
              <option value="day">Today</option>
              <option value="week">This week</option>
            </select>
          </label>
        ) : null}

        {block.type === "tmdb_list" ? (
          <label className={`flex items-center gap-2 ${theme.label}`}>
            List ID
            <input
              className={theme.field}
              style={{ width: 160 }}
              value={block.list_id ?? ""}
              placeholder="e.g. 8136"
              onChange={(e) => updateSource(index, { list_id: e.target.value })}
            />
          </label>
        ) : null}

        {block.type === "mdblist" || block.type === "trakt_list" ? (
          <label className={`flex items-center gap-2 ${theme.label}`}>
            List
            <input
              className={`${theme.field} flex-1`}
              style={{ minWidth: 260 }}
              value={block.list_ref ?? ""}
              placeholder={
                block.type === "mdblist"
                  ? "Paste a list URL or user/slug, e.g. linaspurinis/top-watched-movies-of-the-week"
                  : "Paste a list URL or user/slug, e.g. garycrawfordgc/latest-releases"
              }
              onChange={(e) => updateSource(index, { list_ref: e.target.value })}
            />
          </label>
        ) : null}

        {block.type === "tmdb_discover" ? (
          <>
            <div>
              <div className="text-[12px] font-headline uppercase tracking-widest text-slate-500 mb-1.5">Genres</div>
              <MultiChipPicker
                options={(regionMeta?.genres ?? []).map((g) => ({ key: String(g.id), label: g.name }))}
                selected={(block.genre_ids ?? []).map(String)}
                accentHex={accentHex}
                emptyHint={props.tmdbConfigured ? "Loading genres…" : "Configure a TMDB API key in Settings"}
                onToggle={(key) => {
                  const id = Number(key);
                  const current = block.genre_ids ?? [];
                  updateSource(index, {
                    genre_ids: current.includes(id) ? current.filter((g) => g !== id) : [...current, id],
                  });
                }}
              />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className={`flex items-center gap-2 ${theme.label}`}>
                Years
                <NumberInput
                  value={block.year_from}
                  placeholder="From"
                  min={1900}
                  max={2100}
                  onChange={(v) => updateSource(index, { year_from: v })}
                />
                <span className="text-slate-600">–</span>
                <NumberInput
                  value={block.year_to}
                  placeholder="To"
                  min={1900}
                  max={2100}
                  onChange={(v) => updateSource(index, { year_to: v })}
                />
              </label>
              <label className={`flex items-center gap-2 ${theme.label}`}>
                Min rating
                <NumberInput
                  value={block.min_vote_average}
                  placeholder="e.g. 7"
                  min={0}
                  max={10}
                  width={70}
                  onChange={(v) => updateSource(index, { min_vote_average: v })}
                />
              </label>
            </div>
            <div>
              <div className="flex items-center gap-2 mb-1.5">
                <span className="text-[12px] font-headline uppercase tracking-widest text-slate-500">
                  Streaming services
                </span>
                <select
                  className={theme.selectField}
                  value={region}
                  onChange={(e) => {
                    updateSource(index, { watch_region: e.target.value });
                    ensureMeta(mediaType, e.target.value);
                  }}
                >
                  {(regionMeta?.regions?.length ? regionMeta.regions : [{ code: "US", name: "United States" }]).map(
                    (r) => (
                      <option key={r.code} value={r.code}>
                        {r.name}
                      </option>
                    ),
                  )}
                </select>
              </div>
              <MultiChipPicker
                options={(regionMeta?.providers ?? []).slice(0, 60).map((p) => ({ key: String(p.id), label: p.name }))}
                selected={(block.provider_ids ?? []).map(String)}
                accentHex={accentHex}
                emptyHint={props.tmdbConfigured ? "Loading providers…" : "Configure a TMDB API key in Settings"}
                onToggle={(key) => {
                  const id = Number(key);
                  const current = block.provider_ids ?? [];
                  updateSource(index, {
                    provider_ids: current.includes(id) ? current.filter((p) => p !== id) : [...current, id],
                  });
                }}
              />
            </div>
          </>
        ) : null}

        <label className={`flex items-center gap-2 ${theme.label}`}>
          Max candidates
          <NumberInput
            value={block.limit}
            min={1}
            max={200}
            placeholder="100"
            onChange={(v) => updateSource(index, { limit: v ?? undefined })}
          />
        </label>
      </div>
    );
  }

  function renderFilterConfig(block: CollectionFilterBlock, index: number) {
    const opSelect = (options: { value: string; label: string }[]) => (
      <select
        className={theme.selectOp}
        value={block.op ?? options[0].value}
        onChange={(e) => updateFilter(index, { op: e.target.value })}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );

    switch (block.field) {
      case "genre":
        return (
          <div className="flex flex-col gap-2">
            {opSelect([
              { value: "includes_any", label: "includes any of" },
              { value: "excludes", label: "excludes all of" },
            ])}
            <MultiChipPicker
              options={(baseMeta?.genres ?? []).map((g) => ({ key: g.name, label: g.name }))}
              selected={block.values ?? []}
              accentHex={accentHex}
              emptyHint="Genre list loads from TMDB; type names via ARR metadata otherwise"
              onToggle={(key) => {
                const current = block.values ?? [];
                updateFilter(index, {
                  values: current.includes(key) ? current.filter((v) => v !== key) : [...current, key],
                });
              }}
            />
          </div>
        );
      case "year":
        return (
          <div className="flex flex-wrap items-center gap-2">
            {opSelect([
              { value: "gte", label: "is on or after" },
              { value: "lte", label: "is on or before" },
              { value: "between", label: "is between" },
            ])}
            <NumberInput
              value={typeof block.value === "number" ? block.value : null}
              min={1900}
              max={2100}
              onChange={(v) => updateFilter(index, { value: v })}
            />
            {block.op === "between" ? (
              <>
                <span className="text-slate-600">and</span>
                <NumberInput
                  value={block.value_to ?? null}
                  min={1900}
                  max={2100}
                  onChange={(v) => updateFilter(index, { value_to: v })}
                />
              </>
            ) : null}
          </div>
        );
      case "certification":
        return (
          <div className="flex flex-wrap items-center gap-2">
            {opSelect([
              { value: "in", label: "is one of" },
              { value: "not_in", label: "is none of" },
            ])}
            <input
              className={theme.field}
              style={{ width: 220 }}
              placeholder="e.g. PG-13, R (comma separated)"
              value={(block.values ?? []).join(", ")}
              onChange={(e) =>
                updateFilter(index, {
                  values: e.target.value
                    .split(",")
                    .map((v) => v.trim())
                    .filter(Boolean),
                })
              }
            />
          </div>
        );
      case "studio_network":
        return (
          <div className="flex flex-wrap items-center gap-2">
            {opSelect([
              { value: "contains", label: "contains" },
              { value: "not_contains", label: "does not contain" },
            ])}
            <input
              className={theme.field}
              style={{ width: 200 }}
              placeholder="e.g. HBO"
              value={typeof block.value === "string" ? block.value : ""}
              onChange={(e) => updateFilter(index, { value: e.target.value })}
            />
          </div>
        );
      case "monitored":
        return (
          <select
            className={theme.selectField}
            value={block.value === false ? "no" : "yes"}
            onChange={(e) => updateFilter(index, { value: e.target.value === "yes" })}
          >
            <option value="yes">is monitored</option>
            <option value="no">is not monitored</option>
          </select>
        );
      case "quality_profile":
        return (
          <div className="flex flex-col gap-2">
            {opSelect([
              { value: "in", label: "is one of" },
              { value: "not_in", label: "is none of" },
            ])}
            <MultiChipPicker
              options={(builderMeta?.quality_profiles ?? []).map((p) => ({
                key: p.key,
                label: `${p.name} (${p.instance_label})`,
              }))}
              selected={block.values ?? []}
              accentHex={accentHex}
              emptyHint={builderMeta ? "No quality profiles found — check ARR connections" : "Loading profiles…"}
              onToggle={(key) => {
                const current = block.values ?? [];
                updateFilter(index, {
                  values: current.includes(key) ? current.filter((v) => v !== key) : [...current, key],
                });
              }}
            />
          </div>
        );
      case "original_language":
        return (
          <div className="flex flex-col gap-2">
            {opSelect([
              { value: "in", label: "is one of" },
              { value: "not_in", label: "is none of" },
            ])}
            <MultiChipPicker
              options={(builderMeta?.languages ?? []).map((lang) => ({ key: lang, label: lang }))}
              selected={block.values ?? []}
              accentHex={accentHex}
              emptyHint={builderMeta ? "No languages found in your catalog yet" : "Loading languages…"}
              onToggle={(key) => {
                const current = block.values ?? [];
                updateFilter(index, {
                  values: current.includes(key) ? current.filter((v) => v !== key) : [...current, key],
                });
              }}
            />
          </div>
        );
      case "instance":
        return (
          <select
            className={theme.selectField}
            style={{ width: 260 }}
            value={typeof block.value === "string" ? block.value : ""}
            onChange={(e) => updateFilter(index, { value: e.target.value })}
          >
            <option value="">{builderMeta ? "Select an instance…" : "Loading instances…"}</option>
            {(builderMeta?.instances ?? []).map((inst) => (
              <option key={inst.instance_key} value={inst.instance_key}>
                {inst.label} ({inst.instance_key})
              </option>
            ))}
          </select>
        );
      case "release_window":
        return (
          <div className="flex flex-wrap items-center gap-2">
            {opSelect([
              { value: "within_past", label: "released in the past" },
              { value: "within_next", label: "releasing in the next" },
            ])}
            <NumberInput
              value={typeof block.value === "number" ? block.value : null}
              min={1}
              max={3650}
              onChange={(v) => updateFilter(index, { value: v })}
            />
            <span className="text-[13px] text-slate-500">days</span>
          </div>
        );
      case "rating":
        return (
          <div className="flex flex-wrap items-center gap-2">
            {opSelect([
              { value: "gte", label: "is at least" },
              { value: "lte", label: "is at most" },
            ])}
            <NumberInput
              value={typeof block.value === "number" ? block.value : null}
              min={0}
              max={10}
              width={70}
              onChange={(v) => updateFilter(index, { value: v })}
            />
            <span className="text-[13px] text-slate-500">/ 10</span>
          </div>
        );
    }
  }

  const previewStages: { label: string; value: number | null | undefined }[] = [
    { label: "List candidates", value: preview?.tmdb_candidates },
    { label: "Matched in catalog", value: preview?.matched_in_catalog },
    { label: "After filters", value: preview?.after_filters },
    ...(preview?.pinned_out ? [{ label: "Pinned out", value: preview.pinned_out }] : []),
    ...(preview?.pinned_in ? [{ label: "Pinned in", value: preview.pinned_in }] : []),
    { label: "Selected (sort + limit)", value: preview?.selected },
    { label: "In target library", value: preview?.in_target_library },
  ];

  return (
    <CollectionThemeProvider value={theme}>
    <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
      {/* Pipeline column — capped so leftover width goes to the preview rail instead of stretching cards */}
      <div className="flex-1 min-w-0 lg:max-w-3xl flex flex-col">
        {/* Recipe identity + target — picking the library first drives media type, genres, pins, and preview */}
        <div className={`${theme.identityCard} flex flex-wrap items-center gap-4`}>
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Plex library
            <select
              className={`${theme.selectField} min-w-[14rem]`}
              value={sectionId ?? ""}
              onChange={(e) => setSectionId(e.target.value === "" ? null : Number(e.target.value))}
            >
              <option value="">Select a library…</option>
              {props.sections.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title} ({s.type === "movie" ? "Movies" : "TV"}, {s.item_count})
                </option>
              ))}
            </select>
          </label>
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Collection title
            <input
              className={theme.field}
              style={{ width: 220 }}
              value={collectionTitle}
              placeholder="e.g. Trending Now"
              onChange={(e) => setCollectionTitle(e.target.value)}
            />
          </label>
          <label className={`flex items-center gap-2 ${theme.label}`}>
            Recipe name
            <input
              className={theme.field}
              style={{ width: 220 }}
              value={name}
              placeholder="e.g. Trending This Week"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className={`flex items-center gap-2 ${theme.label} cursor-pointer`}>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="accent-current"
              style={{ accentColor: accentHex }}
            />
            Enabled (runs on schedule)
          </label>
        </div>

        {/* Sources */}
        <div className="flex flex-col gap-2.5">
          <div className={theme.sectionLabel}>Sources</div>
          {definition.sources.map((block, index) => (
            <BlockCard
              key={`${block.type}-${index}`}
              icon={SOURCE_META[block.type].icon}
              title={SOURCE_META[block.type].label}
              subtitle={SOURCE_META[block.type].description}
              accentHex={accentHex}
              onRemove={definition.sources.length > 1 ? () => removeSource(index) : undefined}
            >
              {renderSourceConfig(block, index)}
            </BlockCard>
          ))}
          <AddBlockMenu
            label="Add source"
            options={(Object.keys(SOURCE_META) as CollectionSourceType[]).map((type) => {
              const requires = SOURCE_META[type].requires;
              const disabled =
                (requires === "tmdb" && !props.tmdbConfigured) || (requires === "trakt" && !props.traktConfigured);
              return {
                key: type,
                label: SOURCE_META[type].label,
                icon: SOURCE_META[type].icon,
                description:
                  requires === "trakt" && !props.traktConfigured
                    ? "Add a Trakt Client ID in Settings to enable"
                    : SOURCE_META[type].description,
                disabled,
              };
            })}
            onAdd={(key) =>
              setDefinition((prev) => ({
                ...prev,
                sources: [...prev.sources, defaultSourceBlock(key as CollectionSourceType)],
              }))
            }
          />
        </div>

        <PipelineConnector label="then filter" accentHex={accentHex} />

        {/* Filters */}
        <div className="flex flex-col gap-2.5">
          <div className={theme.sectionLabel}>
            Filters <span className="normal-case tracking-normal opacity-80">(all must match)</span>
          </div>
          {definition.filters.length === 0 ? (
            <div className={theme.dashedPanel}>
              No filters — every matched title passes through.
            </div>
          ) : null}
          {definition.filters.map((block, index) => (
            <BlockCard
              key={`${block.field}-${index}`}
              icon={filterMeta(block.field).icon}
              title={filterMeta(block.field).label}
              accentHex={accentHex}
              onRemove={() => removeFilter(index)}
            >
              {renderFilterConfig(block, index)}
            </BlockCard>
          ))}
          <AddBlockMenu
            label="Add filter"
            options={(Object.keys(FILTER_META) as CollectionFilterField[]).map((field) => ({
              key: field,
              label: FILTER_META[field].label,
              icon: FILTER_META[field].icon,
            }))}
            onAdd={(key) =>
              setDefinition((prev) => ({
                ...prev,
                filters: [...prev.filters, defaultFilterBlock(key as CollectionFilterField)],
              }))
            }
          />
        </div>

        <PipelineConnector label="then pin" accentHex={accentHex} />

        {/* Pins */}
        <div className="flex flex-col gap-2.5">
          <div className={theme.sectionLabel}>
            Pins <span className="normal-case tracking-normal opacity-80">(manual overrides)</span>
          </div>
          {!pinsExpanded ? (
            <button type="button" onClick={() => setPinsExpanded(true)} className={`${theme.dashedButton} py-3`}>
              <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
                push_pin
              </span>
              Pin specific titles to always include or exclude
            </button>
          ) : (
            <>
              <BlockCard
                icon="add_circle"
                title="Always include"
                subtitle="Force these titles in, even if no source or filter matched them"
                accentHex={accentHex}
                overflowVisible
              >
                <PinPicker
                  items={includePins}
                  accentHex={accentHex}
                  catalog={pinCatalog}
                  catalogLoading={props.libraryLoading}
                  placeholder={`Search your ${sectionType === "movie" ? "movie" : "series"} catalog…`}
                  onAdd={(item) => addPin("include", item)}
                  onRemove={(index) => removePin("include", index)}
                />
              </BlockCard>
              <BlockCard
                icon="block"
                title="Always exclude"
                subtitle="Keep these titles out, no matter what the sources and filters say"
                accentHex={accentHex}
                overflowVisible
              >
                <PinPicker
                  items={excludePins}
                  accentHex={accentHex}
                  catalog={pinCatalog}
                  catalogLoading={props.libraryLoading}
                  placeholder={`Search your ${sectionType === "movie" ? "movie" : "series"} catalog…`}
                  onAdd={(item) => addPin("exclude", item)}
                  onRemove={(index) => removePin("exclude", index)}
                />
              </BlockCard>
            </>
          )}
        </div>

        <PipelineConnector label="then arrange" accentHex={accentHex} />

        {/* Output */}
        <BlockCard icon="sort" title="Arrange" subtitle="Order and cap the final selection" accentHex={accentHex}>
          <div className="flex flex-wrap items-center gap-4">
            <label className={`flex items-center gap-2 ${theme.label}`}>
              Sort by
              <select
                className={theme.selectField}
                value={definition.sort ?? "popularity"}
                onChange={(e) =>
                  setDefinition((prev) => ({ ...prev, sort: e.target.value as CollectionDefinition["sort"] }))
                }
              >
                <option value="popularity">Popularity / list rank</option>
                <option value="release_date">Release date (newest)</option>
                <option value="title">Title (A–Z)</option>
              </select>
            </label>
            <label className={`flex items-center gap-2 ${theme.label}`}>
              Max items
              <NumberInput
                value={definition.limit ?? null}
                min={1}
                max={500}
                placeholder="50"
                onChange={(v) => setDefinition((prev) => ({ ...prev, limit: v }))}
              />
            </label>
          </div>
        </BlockCard>

        {/* Actions */}
        {props.saveError ? (
          <div className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-[14px] text-red-300">
            {props.saveError}
          </div>
        ) : null}
        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            disabled={!canSave || props.saving}
            onClick={() => {
              if (!sectionId) return;
              props.onSave({
                name: name.trim(),
                enabled,
                plex_section_id: sectionId,
                plex_section_type: sectionType,
                collection_title: collectionTitle.trim(),
                definition,
              });
            }}
            className="rounded-lg px-5 py-2 text-[14px] font-headline uppercase tracking-wider text-[#0a0e14] transition-opacity disabled:opacity-40"
            style={{ backgroundColor: accentHex }}
          >
            {props.saving ? "Saving…" : props.recipe ? "Save changes" : "Create collection"}
          </button>
          <button type="button" onClick={props.onCancel} className={theme.cancelButton}>
            Cancel
          </button>
        </div>
      </div>

      {/* Preview rail — grows with available width */}
      <aside className="w-full min-w-0 lg:flex-1 lg:min-w-[20rem]">
        <div className={theme.previewRail}>
          <div className={theme.previewHeader}>
            <span className="material-symbols-outlined" style={{ fontSize: 18, color: accentHex }}>
              preview
            </span>
            <span className={theme.heading}>Live preview</span>
            {previewLoading ? (
              <span className="ml-auto h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: accentHex }} />
            ) : null}
          </div>
          <div className="px-4 py-3">
            {!sectionId ? (
              <p className={theme.muted}>Pick a target Plex library to see live counts.</p>
            ) : previewError ? (
              <p className="text-[13px] text-red-300">{previewError}</p>
            ) : (
              <>
                <div className="flex flex-col">
                  {previewStages.map((stage, idx) => (
                    <div key={stage.label} className="flex items-center gap-3 py-1.5">
                      <div className="flex flex-col items-center self-stretch">
                        <span
                          className={`h-2 w-2 rounded-full ${stage.value == null ? theme.stageLineInactive : ""}`}
                          style={stage.value != null ? { backgroundColor: accentHex } : undefined}
                        />
                        {idx < previewStages.length - 1 ? <span className={`w-px flex-1 ${theme.stageLine}`} /> : null}
                      </div>
                      <span className={theme.previewStage}>{stage.label}</span>
                      <span className={theme.previewValue}>
                        {stage.value == null ? "—" : stage.value}
                      </span>
                    </div>
                  ))}
                </div>
                {preview?.plex_error ? (
                  <p className="mt-2 text-[12px] text-yellow-400/90">{preview.plex_error}</p>
                ) : null}
                {preview && preview.unresolved != null && preview.unresolved > 0 ? (
                  <p className="mt-2 text-[12px] text-slate-500">
                    {preview.unresolved} matched title{preview.unresolved === 1 ? " is" : "s are"} not present in the
                    target library and will be skipped.
                  </p>
                ) : null}

                {/* Check a title — trace one catalog title through the pipeline */}
                <div className={`mt-3 border-t ${theme.divider} pt-3`}>
                  <div className={`${theme.sectionLabel} mb-2`}>Check a title</div>
                  <div className="relative" ref={explainBoxRef}>
                    <input
                      className={`${theme.field} w-full`}
                      value={explainQuery}
                      placeholder="Search your catalog to see why a title is in or out…"
                      onChange={(e) => {
                        setExplainQuery(e.target.value);
                        setExplainOpen(true);
                      }}
                      onFocus={() => setExplainOpen(true)}
                    />
                    {explainOpen && explainResults.length ? (
                      <div className={`${theme.dropdown} max-h-60 overflow-y-auto`}>
                        {explainResults.map((item) => (
                          <button
                            key={item.id}
                            type="button"
                            onClick={() => {
                              setExplainItem({
                                tmdb_id: item.tmdb_id ?? null,
                                tvdb_id: item.tvdb_id ?? null,
                                imdb_id: item.imdb_id ?? null,
                                title: item.title,
                                year: item.year ?? null,
                                poster: item.poster_url ?? null,
                              });
                              setExplainQuery("");
                              setExplainOpen(false);
                            }}
                            className={`${theme.dropdownItem} items-center gap-2 px-3 py-1.5`}
                          >
                            <span className={`flex-1 min-w-0 truncate text-[13px] ${theme.pinTitle}`}>{item.title}</span>
                            <span className={`text-[12px] ${theme.pinYear}`}>{item.year ?? ""}</span>
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  {explainItem ? (
                    <div className="mt-2">
                      <div className="flex items-center gap-2">
                        {explainItem.poster ? (
                          <img src={explainItem.poster} alt="" className="h-9 w-6 rounded-sm object-cover" />
                        ) : null}
                        <span className={`flex-1 min-w-0 truncate text-[13px] ${theme.pinTitle}`}>
                          {explainItem.title}
                          {explainItem.year ? <span className={theme.pinYear}> ({explainItem.year})</span> : null}
                        </span>
                        {explainLoading ? (
                          <span className="h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: accentHex }} />
                        ) : explainResult ? (
                          <span
                            className={`rounded-full border px-2 py-0.5 text-[11px] font-headline uppercase tracking-wider ${
                              explainResult.in_collection
                                ? "border-emerald-500/40 text-emerald-400"
                                : "border-red-500/40 text-red-400"
                            }`}
                          >
                            {explainResult.in_collection ? "In collection" : "Not included"}
                          </span>
                        ) : null}
                        <button
                          type="button"
                          onClick={() => {
                            setExplainItem(null);
                            setExplainResult(null);
                            setExplainError(null);
                          }}
                          className={`material-symbols-outlined transition-colors ${theme.iconAction}`}
                          style={{ fontSize: 16 }}
                          title="Clear"
                        >
                          close
                        </button>
                      </div>
                      {explainError ? <p className="mt-1.5 text-[12px] text-red-300">{explainError}</p> : null}
                      {explainResult ? (
                        <div className="mt-1.5 flex flex-col gap-1">
                          {explainResult.stages.map((stage) => (
                            <div key={stage.key}>
                              <div className="flex items-center gap-2 py-0.5">
                                <ExplainStatusIcon status={stage.status} />
                                <span
                                  className={`flex-1 text-[13px] ${
                                    stage.status === "skip" ? theme.explainSkip : theme.explainStage
                                  }`}
                                >
                                  {EXPLAIN_STAGE_LABELS[stage.key] ?? stage.key}
                                  {stage.status === "skip" ? " — not reached" : ""}
                                </span>
                              </div>
                              {stage.detail ? (
                                <p className="ml-6 text-[12px] text-slate-500">{stage.detail}</p>
                              ) : null}
                              {stage.checks.length ? (
                                <div className="ml-6 flex flex-col gap-0.5">
                                  {stage.checks.map((check, i) => (
                                    <div key={i} className="flex items-center gap-1.5">
                                      <ExplainStatusIcon status={check.status} />
                                      <span className={`flex-1 min-w-0 truncate text-[12px] ${theme.explainCheck}`}>
                                        {stage.key === "sources"
                                          ? explainSourceCheckLabel(check)
                                          : explainRuleCheckLabel(check)}
                                      </span>
                                    </div>
                                  ))}
                                  {stage.checks.some((c) => c.detail) ? (
                                    <p className="text-[11px] text-slate-600">
                                      {stage.checks.find((c) => c.detail)?.detail}
                                    </p>
                                  ) : null}
                                </div>
                              ) : null}
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>

                {preview?.sample?.length ? (
                  <div className={`mt-3 border-t ${theme.divider} pt-3`}>
                    <div className={`${theme.sectionLabel} mb-2`}>Sample</div>
                    <div className="grid grid-cols-4 sm:grid-cols-5 xl:grid-cols-6 gap-2">
                      {preview.sample.map((item) =>
                        item.poster ? (
                          <img
                            key={item.id}
                            src={item.poster}
                            alt={item.title}
                            title={`${item.title}${item.year ? ` (${item.year})` : ""}`}
                            className={`aspect-[2/3] w-full rounded-md object-cover ${theme.posterFallback}`}
                            loading="lazy"
                          />
                        ) : (
                          <div
                            key={item.id}
                            title={`${item.title}${item.year ? ` (${item.year})` : ""}`}
                            className={`aspect-[2/3] w-full rounded-md p-1 text-[9px] leading-tight overflow-hidden ${theme.sampleFallback}`}
                          >
                            {item.title}
                          </div>
                        ),
                      )}
                    </div>
                  </div>
                ) : null}
              </>
            )}
          </div>
        </div>
      </aside>
    </div>
    </CollectionThemeProvider>
  );
}
