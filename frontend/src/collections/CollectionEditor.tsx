import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ThemeMode } from "../brandTypes";
import {
  explainCollectionItem,
  getCollectionBuilderMeta,
  getCollectionTmdbMeta,
  previewCollectionDefinition,
  type RecipeWritePayload,
} from "../api/collections";
import { ArrAddModal } from "./ArrAddModal";
import {
  CollectionThemeProvider,
  getCollectionTheme,
  useCollectionTheme,
} from "./collectionTheme";
import { AndOrToggle, ToggleSwitch } from "./ToggleSwitch";
import type {
  CollectionActiveWindow,
  CollectionBuilderMeta,
  CollectionDefinition,
  CollectionExplainCheck,
  CollectionExplainNode,
  CollectionExplainResponse,
  CollectionFilterBlock,
  CollectionFilterField,
  CollectionFilterGroup,
  CollectionFilterNode,
  CollectionFilters,
  CollectionPinnedItem,
  CollectionMissingFromArrItem,
  CollectionPreviewResponse,
  CollectionRecipe,
  CollectionRatingProvider,
  CollectionSourceBlock,
  CollectionSourceType,
  CollectionTmdbMeta,
  LibraryItem,
  PlexSectionOption,
} from "../types/api";

const MOVIE_RATING_PROVIDERS: {
  value: CollectionRatingProvider;
  label: string;
  max: number;
}[] = [
  { value: "imdb", label: "IMDb", max: 10 },
  { value: "tmdb", label: "TMDB", max: 10 },
  { value: "trakt", label: "Trakt", max: 10 },
  { value: "metacritic", label: "Metacritic", max: 100 },
  { value: "rottenTomatoes", label: "Rotten Tomatoes", max: 100 },
];

const RATING_PROVIDER_LABELS: Record<string, string> = Object.fromEntries(
  MOVIE_RATING_PROVIDERS.map((p) => [p.value, p.label]),
);

function ratingProviderMax(provider: string | null | undefined): number {
  return MOVIE_RATING_PROVIDERS.find((p) => p.value === provider)?.max ?? 10;
}

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

function arrIncludeConstraintLabels(filters: CollectionFilters | undefined): string[] {
  const labels: string[] = [];
  const walk = (node: CollectionFilterNode | CollectionFilters | undefined) => {
    if (!node) return;
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (typeof node !== "object") return;
    if ("field" in node && node.field) {
      if (node.field === "instance" && String(node.value || "").trim()) {
        labels.push(FILTER_META.instance.label);
      }
      if (node.field === "quality_profile" && node.op !== "not_in" && (node.values?.length ?? 0) > 0) {
        labels.push(FILTER_META.quality_profile.label);
      }
      if (node.field === "monitored" && node.value !== false) {
        labels.push(FILTER_META.monitored.label);
      }
      return;
    }
    if ("children" in node) (node.children || []).forEach(walk);
  };
  walk(filters);
  return [...new Set(labels)];
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

function defaultFilterBlock(field: CollectionFilterField, sectionType: "movie" | "show"): CollectionFilterBlock {
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
      return {
        field,
        op: "within_past",
        value: 365,
        basis: sectionType === "movie" ? "theater" : "premiered",
      };
    case "rating":
      return sectionType === "movie"
        ? { field, op: "gte", value: 7, provider: "imdb", min_votes: null }
        : { field, op: "gte", value: 7, min_votes: null };
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

function isRuleNode(node: CollectionFilterNode): node is CollectionFilterBlock {
  return (node as CollectionFilterBlock).field !== undefined;
}

function isEmptyFilters(filters: CollectionDefinition["filters"] | undefined): boolean {
  if (!filters) return true;
  if (Array.isArray(filters)) return filters.length === 0;
  return (filters.children ?? []).length === 0;
}

/** True when the tree can be shown in simple mode without losing meaning. */
function isLinearFilters(filters: CollectionDefinition["filters"] | undefined): boolean {
  if (isEmptyFilters(filters)) return true;
  if (!filters || Array.isArray(filters)) return true;
  if (filters.op === "and" && (filters.children ?? []).every(isRuleNode)) return true;
  if (filters.op === "or") {
    return (filters.children ?? []).every(
      (child) => !isRuleNode(child) && child.op === "and" && (child.children ?? []).every(isRuleNode),
    );
  }
  return false;
}

/** Normalize an advanced tree back to the shape simple mode expects. */
function toSimpleFilters(filters: CollectionDefinition["filters"] | undefined): CollectionDefinition["filters"] {
  if (isEmptyFilters(filters)) return [];
  if (!filters || Array.isArray(filters)) return filters ?? [];
  if (filters.op === "and" && (filters.children ?? []).every(isRuleNode)) {
    return [...filters.children] as CollectionFilterBlock[];
  }
  return filters;
}

function filtersToGroups(filters: CollectionDefinition["filters"] | undefined): CollectionFilterBlock[][] {
  if (!filters) return [];
  if (Array.isArray(filters)) return filters.length ? [filters] : [];
  if (filters.op === "and" && (filters.children ?? []).every(isRuleNode)) {
    return [filters.children as CollectionFilterBlock[]];
  }
  return (filters.children ?? []).map((group) => (isRuleNode(group) ? [group] : (group.children ?? []).filter(isRuleNode)));
}

function groupsToFilters(groups: CollectionFilterBlock[][]): CollectionDefinition["filters"] {
  if (!groups.length) return [];
  if (groups.length === 1) return groups[0];
  return { op: "or", children: groups.map((group) => ({ op: "and" as const, children: group })) };
}

/** Hoist a sole AND sub-group of rules so advanced mode doesn't show a fake level-2 wrapper. */
function flattenRedundantFilterGroup(group: CollectionFilterGroup): CollectionFilterGroup {
  const children = (group.children ?? []).map((child) =>
    isRuleNode(child) ? child : flattenRedundantFilterGroup(child),
  );
  if (
    children.length === 1 &&
    !isRuleNode(children[0]) &&
    children[0].op === "and" &&
    (children[0].children ?? []).every(isRuleNode)
  ) {
    const op = group.op === "or" ? "and" : group.op;
    return { op, children: [...children[0].children!] };
  }
  return { ...group, children };
}

/** Coerce any filters value to an editable tree root for the Advanced builder. */
function filtersToTree(filters: CollectionDefinition["filters"] | undefined): CollectionFilterGroup {
  if (!filters) return { op: "and", children: [] };
  if (Array.isArray(filters)) return { op: "and", children: [...filters] };
  return flattenRedundantFilterGroup(filters);
}

function toAdvancedFilters(filters: CollectionDefinition["filters"] | undefined): CollectionFilterGroup {
  return filtersToTree(filters);
}

/** Immutably replace the node at `path` (child indices from the root); null deletes it. */
function updateTreeAt(
  root: CollectionFilterGroup,
  path: number[],
  fn: (node: CollectionFilterNode) => CollectionFilterNode | null,
): CollectionFilterGroup {
  if (!path.length) {
    const next = fn(root);
    return next && !isRuleNode(next) ? next : root;
  }
  const [head, ...rest] = path;
  const children = [...(root.children ?? [])];
  const target = children[head];
  if (target === undefined) return root;
  if (!rest.length) {
    const next = fn(target);
    if (next === null) children.splice(head, 1);
    else children[head] = next;
  } else {
    if (isRuleNode(target)) return root;
    children[head] = updateTreeAt(target, rest, fn);
  }
  return { ...root, children };
}

const MAX_FILTER_DEPTH = 3;

const SCHEDULE_PRESETS: { value: string; label: string }[] = [
  { value: "", label: "App default" },
  { value: "1", label: "Every hour" },
  { value: "6", label: "Every 6 hours" },
  { value: "12", label: "Every 12 hours" },
  { value: "24", label: "Daily" },
  { value: "168", label: "Weekly" },
];

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function splitMonthDay(raw: string): { month: number; day: number } {
  const parts = String(raw || "").trim().split("-").map((p) => Number(p));
  // MM-DD or YYYY-MM-DD (last two segments are month/day).
  const m = parts.length >= 3 ? parts[1] : parts[0];
  const d = parts.length >= 3 ? parts[2] : parts[1];
  return {
    month: Math.min(Math.max(Number.isFinite(m) && m > 0 ? m : 1, 1), 12),
    day: Math.min(Math.max(Number.isFinite(d) && d > 0 ? d : 1, 1), 31),
  };
}

function joinMonthDay(month: number, day: number): string {
  const m = Math.min(Math.max(Number.isFinite(month) && month > 0 ? month : 1, 1), 12);
  const d = Math.min(Math.max(Number.isFinite(day) && day > 0 ? day : 1, 1), 31);
  return `${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

function MonthDayPicker(props: { value: string; onChange: (value: string) => void }) {
  const theme = useCollectionTheme();
  const { month, day } = splitMonthDay(props.value);
  return (
    <span className="inline-flex items-center gap-1.5">
      <select
        className={`${theme.selectField} !min-w-[4.5rem]`}
        value={month}
        onChange={(e) => props.onChange(joinMonthDay(Number(e.target.value), day))}
      >
        {MONTH_NAMES.map((name, i) => (
          <option key={name} value={i + 1}>
            {name}
          </option>
        ))}
      </select>
      <NumberInput
        value={day}
        min={1}
        max={31}
        width={58}
        onChange={(v) => props.onChange(joinMonthDay(month, v == null || v < 1 ? 1 : v))}
      />
    </span>
  );
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
  has_released: "has been released",
  not_yet_released: "not yet released",
};

function explainSourceCheckLabel(check: CollectionExplainCheck): string {
  const meta = SOURCE_META[check.type as CollectionSourceType];
  const base = meta?.label ?? check.type ?? "Source";
  return check.list_ref ? `${base} — ${check.list_ref}` : base;
}

const RELEASE_BASIS_LABELS: Record<string, string> = {
  premiered: "series premiere",
  latest_episode: "latest aired episode",
  latest_season: "latest season premiere",
  theater: "theatrical release",
  digital: "digital release",
  physical: "physical release",
};

function explainRuleCheckLabel(check: CollectionExplainCheck): string {
  const base = filterMeta(String(check.field ?? "")).label;
  const op = FILTER_OP_LABELS[String(check.op ?? "")] ?? String(check.op ?? "");
  let value = "";
  if (check.values?.length) value = check.values.join(", ");
  else if (check.value != null && check.value !== "") value = String(check.value);
  if (check.value_to != null) value = `${value}–${check.value_to}`;
  const basis =
    check.field === "release_window" && check.basis
      ? `(by ${RELEASE_BASIS_LABELS[check.basis] ?? check.basis})`
      : "";
  const provider =
    check.field === "rating"
      ? check.provider
        ? `(${RATING_PROVIDER_LABELS[check.provider] ?? check.provider})`
        : "(ARR rating)"
      : "";
  const votes =
    check.field === "rating" && check.min_votes != null && Number(check.min_votes) > 0
      ? `≥ ${check.min_votes} votes`
      : "";
  return [base, provider, op, value, basis, votes].filter(Boolean).join(" ");
}

/** Skip redundant single-child group wrappers so simple recipes read flat. */
function collapseTrivialExplainNode(node: CollectionExplainNode): CollectionExplainNode {
  let current = node;
  while (current.kind === "group" && current.children.length === 1 && current.children[0].kind === "group") {
    current = current.children[0];
  }
  return current;
}

function ExplainTreeNodeView(props: { node: CollectionExplainNode }) {
  const theme = useCollectionTheme();
  if (props.node.kind === "rule") {
    return (
      <div className="flex items-center gap-1.5">
        <ExplainStatusIcon status={props.node.status} />
        <span className={`flex-1 min-w-0 truncate text-[12px] ${theme.explainCheck}`}>
          {explainRuleCheckLabel(props.node)}
        </span>
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-center gap-1.5">
        <ExplainStatusIcon status={props.node.status} />
        <span className={`text-[12px] ${props.node.status === "skip" ? theme.explainSkip : theme.explainCheck}`}>
          {props.node.op === "and" ? "All of:" : "Any of:"}
          {props.node.status === "skip" ? " — not needed" : ""}
        </span>
      </div>
      <div className="ml-5 flex flex-col gap-0.5">
        {props.node.children.map((child, i) => (
          <ExplainTreeNodeView key={i} node={child} />
        ))}
      </div>
    </div>
  );
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
  const [sectionIds, setSectionIds] = useState<number[]>(() => {
    const extras = props.recipe?.plex_section_ids?.filter((id) => Number.isFinite(id) && id >= 1) ?? [];
    if (extras.length) return extras;
    return props.recipe?.plex_section_id ? [props.recipe.plex_section_id] : [];
  });
  const [collectionTitle, setCollectionTitle] = useState(props.recipe?.collection_title ?? "");
  const [runIntervalHours, setRunIntervalHours] = useState<number | null>(props.recipe?.run_interval_hours ?? null);
  const [activeWindow, setActiveWindow] = useState<CollectionActiveWindow | null>(props.recipe?.active_window ?? null);
  const [definition, setDefinition] = useState<CollectionDefinition>(
    props.recipe?.definition && Array.isArray(props.recipe.definition.sources)
      ? props.recipe.definition
      : { sources: [defaultSourceBlock(props.tmdbConfigured ? "tmdb_trending" : "catalog")], filters: [], limit: 50, sort: "popularity" },
  );

  const sectionId = sectionIds[0] ?? null;
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
  // Filters: simple mode is OR-ed groups of AND-ed rules; Advanced mode edits the
  // full and/or tree (depth-capped). Recipes that already nest open in Advanced.
  const [advancedFilters, setAdvancedFilters] = useState<boolean>(
    () => !isLinearFilters(props.recipe?.definition?.filters),
  );
  const filterGroups = useMemo(() => filtersToGroups(definition.filters), [definition.filters]);
  const filterTree = useMemo(() => filtersToTree(definition.filters), [definition.filters]);
  const canUseSimpleFilters = useMemo(() => isLinearFilters(definition.filters), [definition.filters]);
  const handleAdvancedFiltersToggle = (on: boolean) => {
    if (!on && !canUseSimpleFilters) return;
    setAdvancedFilters(on);
    setDefinition((prev) => ({
      ...prev,
      filters: on ? toAdvancedFilters(prev.filters) : toSimpleFilters(prev.filters),
    }));
  };
  const mutateGroups = (mutate: (groups: CollectionFilterBlock[][]) => CollectionFilterBlock[][]) => {
    setDefinition((prev) => ({ ...prev, filters: groupsToFilters(mutate(filtersToGroups(prev.filters))) }));
  };
  const mutateTree = (mutate: (root: CollectionFilterGroup) => CollectionFilterGroup) => {
    setDefinition((prev) => ({ ...prev, filters: mutate(filtersToTree(prev.filters)) }));
  };
  const setGroupOp = (path: number[], op: "and" | "or") => {
    mutateTree((root) =>
      updateTreeAt(root, path, (node) => (isRuleNode(node) ? node : { ...node, op })),
    );
  };
  const addTreeRule = (path: number[], field: CollectionFilterField) => {
    mutateTree((root) =>
      updateTreeAt(root, path, (node) =>
        isRuleNode(node)
          ? node
          : { ...node, children: [...(node.children ?? []), defaultFilterBlock(field, sectionType)] },
      ),
    );
  };
  const addTreeGroup = (path: number[]) => {
    mutateTree((root) =>
      updateTreeAt(root, path, (node) =>
        isRuleNode(node) ? node : { ...node, children: [...(node.children ?? []), { op: "and" as const, children: [] }] },
      ),
    );
  };
  const removeTreeNode = (path: number[]) => {
    mutateTree((root) => updateTreeAt(root, path, () => null));
  };
  const updateTreeRule = (path: number[], patch: Partial<CollectionFilterBlock>) => {
    mutateTree((root) =>
      updateTreeAt(root, path, (node) => (isRuleNode(node) ? { ...node, ...patch } : node)),
    );
  };
  const updateFilter = (groupIndex: number, ruleIndex: number, patch: Partial<CollectionFilterBlock>) => {
    mutateGroups((groups) =>
      groups.map((group, gi) =>
        gi === groupIndex ? group.map((f, ri) => (ri === ruleIndex ? { ...f, ...patch } : f)) : group,
      ),
    );
  };
  const removeFilter = (groupIndex: number, ruleIndex: number) => {
    mutateGroups((groups) =>
      groups
        .map((group, gi) => (gi === groupIndex ? group.filter((_, ri) => ri !== ruleIndex) : group))
        .filter((group) => group.length > 0),
    );
  };
  const addFilter = (groupIndex: number, field: CollectionFilterField) => {
    mutateGroups((groups) => {
      if (!groups.length) return [[defaultFilterBlock(field, sectionType)]];
      return groups.map((group, gi) =>
        gi === groupIndex ? [...group, defaultFilterBlock(field, sectionType)] : group,
      );
    });
  };
  const addGroup = (field: CollectionFilterField) => {
    mutateGroups((groups) => [...groups, [defaultFilterBlock(field, sectionType)]]);
  };
  const removeGroup = (groupIndex: number) => {
    mutateGroups((groups) => groups.filter((_, gi) => gi !== groupIndex));
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
  const [previewPane, setPreviewPane] = useState<"catalog" | "missing">("catalog");
  const [selectedMissing, setSelectedMissing] = useState<Set<string>>(new Set());
  const [arrModalOpen, setArrModalOpen] = useState(false);
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
        plex_section_ids: sectionIds,
        plex_section_type: sectionType,
        definition: JSON.parse(definitionJson) as CollectionDefinition,
      })
        .then((result) => {
          if (previewSeq.current !== seq) return;
          setPreview(result);
          setSelectedMissing(new Set());
          if (!(result.missing_from_arr_count || result.missing_from_arr_prefilter_count || 0)) {
            setPreviewPane("catalog");
          }
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
  }, [definitionJson, sectionId, sectionIds, sectionType]);

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

  function renderFilterConfig(block: CollectionFilterBlock, update: (patch: Partial<CollectionFilterBlock>) => void) {
    const opSelect = (options: { value: string; label: string }[]) => (
      <select
        className={theme.selectOp}
        value={block.op ?? options[0].value}
        onChange={(e) => update({ op: e.target.value })}
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
              options={(builderMeta?.genres ?? []).map((name) => ({ key: name, label: name }))}
              selected={block.values ?? []}
              accentHex={accentHex}
              emptyHint={builderMeta ? "No genres found in your catalog yet" : "Loading genres…"}
              onToggle={(key) => {
                const current = block.values ?? [];
                update({
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
              onChange={(v) => update({ value: v })}
            />
            {block.op === "between" ? (
              <>
                <span className="text-slate-600">and</span>
                <NumberInput
                  value={block.value_to ?? null}
                  min={1900}
                  max={2100}
                  onChange={(v) => update({ value_to: v })}
                />
              </>
            ) : null}
          </div>
        );
      case "certification": {
        const catalogCerts = builderMeta?.certifications ?? [];
        const selectedCerts = block.values ?? [];
        const certByUpper = new Map<string, string>();
        for (const cert of catalogCerts) {
          const key = cert.trim().toUpperCase();
          if (key && !certByUpper.has(key)) certByUpper.set(key, cert.trim());
        }
        // Keep legacy free-text selections visible even if they left the catalog.
        for (const cert of selectedCerts) {
          const key = cert.trim().toUpperCase();
          if (key && !certByUpper.has(key)) certByUpper.set(key, cert.trim());
        }
        const certOptions = Array.from(certByUpper.values()).map((name) => ({ key: name, label: name }));
        const selectedNormalized = selectedCerts
          .map((cert) => certByUpper.get(cert.trim().toUpperCase()) ?? cert.trim())
          .filter(Boolean);
        return (
          <div className="flex flex-col gap-2">
            {opSelect([
              { value: "in", label: "is one of" },
              { value: "not_in", label: "is none of" },
            ])}
            <MultiChipPicker
              options={certOptions}
              selected={selectedNormalized}
              accentHex={accentHex}
              emptyHint={builderMeta ? "No certifications found in your catalog yet" : "Loading certifications…"}
              onToggle={(key) => {
                const current = new Set(selectedNormalized);
                if (current.has(key)) current.delete(key);
                else current.add(key);
                update({ values: Array.from(current) });
              }}
            />
          </div>
        );
      }
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
              onChange={(e) => update({ value: e.target.value })}
            />
          </div>
        );
      case "monitored":
        return (
          <select
            className={theme.selectField}
            value={block.value === false ? "no" : "yes"}
            onChange={(e) => update({ value: e.target.value === "yes" })}
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
                update({
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
                update({
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
            onChange={(e) => update({ value: e.target.value })}
          >
            <option value="">{builderMeta ? "Select an instance…" : "Loading instances…"}</option>
            {(builderMeta?.instances ?? []).map((inst) => (
              <option key={inst.instance_key} value={inst.instance_key}>
                {inst.label} ({inst.instance_key})
              </option>
            ))}
          </select>
        );
      case "release_window": {
        const statusOp = block.op === "has_released" || block.op === "not_yet_released";
        return (
          <div className="flex flex-wrap items-center gap-2">
            {opSelect([
              { value: "has_released", label: "has been released" },
              { value: "not_yet_released", label: "not yet released" },
              { value: "within_past", label: "released in the past" },
              { value: "within_next", label: "releasing in the next" },
            ])}
            {!statusOp ? (
              <>
                <NumberInput
                  value={typeof block.value === "number" ? block.value : null}
                  min={1}
                  max={3650}
                  onChange={(v) => update({ value: v })}
                />
                <span className="text-[13px] text-slate-500">days</span>
              </>
            ) : null}
            <label className={`flex items-center gap-2 ${theme.label}`}>
              based on
              <select
                className={theme.selectField}
                value={block.basis ?? (sectionType === "movie" ? "theater" : "premiered")}
                onChange={(e) =>
                  update({ basis: e.target.value as CollectionFilterBlock["basis"] })
                }
              >
                {sectionType === "movie" ? (
                  <>
                    <option value="theater">Theatrical release</option>
                    <option value="digital">Digital release</option>
                    <option value="physical">Physical release</option>
                  </>
                ) : (
                  <>
                    <option value="premiered">Series premiere</option>
                    <option value="latest_episode">Latest aired episode</option>
                    <option value="latest_season">Latest season premiere</option>
                  </>
                )}
              </select>
            </label>
          </div>
        );
      }
      case "rating": {
        const provider =
          sectionType === "movie"
            ? ((block.provider as CollectionRatingProvider | null | undefined) ?? "imdb")
            : null;
        const scaleMax = sectionType === "movie" ? ratingProviderMax(provider) : 10;
        const updateRating = (patch: Partial<CollectionFilterBlock>) => {
          if (sectionType === "movie" && !block.provider && !patch.provider) {
            update({ provider: "imdb", ...patch });
            return;
          }
          update(patch);
        };
        return (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              {sectionType === "movie" ? (
                <select
                  className={theme.selectField}
                  value={provider ?? "imdb"}
                  onChange={(e) => {
                    const next = e.target.value as CollectionRatingProvider;
                    const prevMax = ratingProviderMax(provider);
                    const nextMax = ratingProviderMax(next);
                    let nextValue = typeof block.value === "number" ? block.value : null;
                    if (nextValue != null && prevMax !== nextMax) {
                      nextValue = Math.round((nextValue / prevMax) * nextMax * 10) / 10;
                    }
                    updateRating({ provider: next, value: nextValue });
                  }}
                >
                  {MOVIE_RATING_PROVIDERS.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              ) : (
                <span
                  className="text-[13px] text-slate-500"
                  title="Sonarr exposes a single score from Skyhook (usually IMDb when mapped)"
                >
                  Sonarr rating
                </span>
              )}
              <select
                className={theme.selectOp}
                value={block.op ?? "gte"}
                onChange={(e) => updateRating({ op: e.target.value })}
              >
                <option value="gte">is at least</option>
                <option value="lte">is at most</option>
              </select>
              <NumberInput
                value={typeof block.value === "number" ? block.value : null}
                min={0}
                max={scaleMax}
                width={70}
                onChange={(v) => updateRating({ value: v })}
              />
              <span className="text-[13px] text-slate-500">/ {scaleMax}</span>
            </div>
            <label className={`flex flex-wrap items-center gap-2 ${theme.label}`}>
              Min votes
              <NumberInput
                value={typeof block.min_votes === "number" ? block.min_votes : null}
                min={0}
                max={10_000_000}
                width={90}
                placeholder="optional"
                onChange={(v) => updateRating({ min_votes: v })}
              />
              <span className="text-[12px] text-slate-500 font-normal normal-case tracking-normal">
                {sectionType === "movie"
                  ? "on the selected source (titles missing that rating fail)"
                  : "from Sonarr’s score (titles with no rating fail)"}
              </span>
            </label>
          </div>
        );
      }
    }
  }

  // Advanced (nested) filter builder — recursive group cards with AND/OR toggles.
  function renderTreeGroup(group: CollectionFilterGroup, path: number[], depth: number): ReactNode {
    const children = group.children ?? [];
    const isRoot = path.length === 0;
    const accentAlpha = depth === 1 ? "66" : depth === 2 ? "44" : "2e";
    return (
      <div
        className={`rounded-xl border p-3 ${theme.divider}`}
        style={{ borderLeft: `3px solid ${accentHex}${accentAlpha}` }}
      >
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <AndOrToggle
            op={group.op}
            onChange={(op) => setGroupOp(path, op)}
            accentHex={accentHex}
            mutedClass={theme.muted}
          />
          <span className={theme.muted}>{isRoot ? "Top level" : `Nested group (level ${depth})`}</span>
          {!isRoot ? (
            <button
              type="button"
              onClick={() => removeTreeNode(path)}
              className={`ml-auto material-symbols-outlined transition-colors ${theme.iconAction}`}
              style={{ fontSize: 17 }}
              title="Remove group (and everything inside it)"
            >
              close
            </button>
          ) : null}
        </div>
        <div className="flex flex-col">
          {children.map((child, index) => (
            <div key={index}>
              {index > 0 ? <PipelineConnector label={group.op.toUpperCase()} accentHex={accentHex} /> : null}
              {isRuleNode(child) ? (
                <BlockCard
                  icon={filterMeta(child.field).icon}
                  title={filterMeta(child.field).label}
                  accentHex={accentHex}
                  onRemove={() => removeTreeNode([...path, index])}
                >
                  {renderFilterConfig(child, (patch) => updateTreeRule([...path, index], patch))}
                </BlockCard>
              ) : (
                renderTreeGroup(child, [...path, index], depth + 1)
              )}
            </div>
          ))}
          {!children.length ? (
            <div className={theme.dashedPanel}>Empty group — add a rule or sub-group below.</div>
          ) : null}
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <AddBlockMenu
            label="Add rule"
            options={(Object.keys(FILTER_META) as CollectionFilterField[]).map((field) => ({
              key: field,
              label: FILTER_META[field].label,
              icon: FILTER_META[field].icon,
            }))}
            onAdd={(key) => addTreeRule(path, key as CollectionFilterField)}
          />
          {depth < MAX_FILTER_DEPTH ? (
            <button type="button" onClick={() => addTreeGroup(path)} className={theme.dashedButton}>
              <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
                account_tree
              </span>
              Add sub-group
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  const missingCount = preview?.missing_from_arr_count ?? 0;
  const missingPrefilter = preview?.missing_from_arr_prefilter_count ?? 0;
  const missingGaps = preview?.missing_from_arr_filter_gaps ?? [];
  const missingItems = preview?.missing_from_arr ?? [];
  const arrIncludeLabels = arrIncludeConstraintLabels(definition.filters);
  const showMissingPane = missingCount > 0 || missingPrefilter > 0 || missingGaps.length > 0;
  const missingKey = (item: CollectionMissingFromArrItem) =>
    `${item.tmdb_id ?? ""}:${item.tvdb_id ?? ""}:${item.imdb_id ?? ""}:${item.title}:${item.year ?? ""}`;
  const previewStages: { label: string; value: number | null | undefined }[] = [
    { label: "List candidates", value: preview?.tmdb_candidates },
    { label: "Matched in catalog", value: preview?.matched_in_catalog },
    ...(showMissingPane ? [{ label: "Missing from ARR", value: missingCount }] : []),
    { label: "After filters", value: preview?.after_filters },
    ...(preview?.pinned_out ? [{ label: "Pinned out", value: preview.pinned_out }] : []),
    ...(preview?.pinned_in ? [{ label: "Pinned in", value: preview.pinned_in }] : []),
    { label: "Selected (sort + limit)", value: preview?.selected },
    {
      label: sectionIds.length > 1 ? "In libraries (combined)" : "In target library",
      value: preview?.in_target_library,
    },
  ];

  return (
    <CollectionThemeProvider value={theme}>
    <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
      {/* Pipeline column — capped so leftover width goes to the preview rail instead of stretching cards */}
      <div className="flex-1 min-w-0 lg:max-w-3xl flex flex-col">
        {/* Recipe identity + target — picking the library first drives media type, genres, pins, and preview */}
        <div className={`${theme.identityCard} flex flex-col gap-3`}>
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex flex-col gap-1.5 min-w-[16rem]">
              <span className={theme.label}>Plex libraries</span>
              <p className={`text-[12px] font-normal normal-case tracking-normal ${theme.muted}`}>
                Same collection name is created in each selected library (same type only).
              </p>
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
                  <option key={preset.value} value={preset.value}>
                    {preset.label}
                  </option>
                ))}
              </select>
            </label>
            <label className={`flex items-center gap-2 ${theme.label} cursor-pointer select-none`}>
              <span>Seasonal window</span>
              <ToggleSwitch
                checked={activeWindow !== null}
                onChange={(checked) =>
                  setActiveWindow(checked ? { start: "12-01", end: "01-06", when_inactive: "keep" } : null)
                }
                accentHex={accentHex}
                ariaLabel="Seasonal window"
              />
            </label>
            {activeWindow ? (
              <>
                <label className={`flex items-center gap-2 ${theme.label}`}>
                  Active from
                  <MonthDayPicker
                    value={activeWindow.start}
                    onChange={(value) => setActiveWindow((prev) => (prev ? { ...prev, start: value } : prev))}
                  />
                </label>
                <label className={`flex items-center gap-2 ${theme.label}`}>
                  to
                  <MonthDayPicker
                    value={activeWindow.end}
                    onChange={(value) => setActiveWindow((prev) => (prev ? { ...prev, end: value } : prev))}
                  />
                </label>
                <label className={`flex items-center gap-2 ${theme.label}`}>
                  When dormant
                  <select
                    className={theme.selectField}
                    value={activeWindow.when_inactive}
                    onChange={(e) =>
                      setActiveWindow((prev) =>
                        prev ? { ...prev, when_inactive: e.target.value as "keep" | "clear" } : prev,
                      )
                    }
                  >
                    <option value="keep">Keep collection as-is</option>
                    <option value="clear">Empty the collection</option>
                  </select>
                </label>
              </>
            ) : null}
          </div>
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

        {/* Filters — simple mode: AND within a group, OR between groups; Advanced: full nesting */}
        <div className="flex flex-col gap-2.5">
          <div className="flex items-center gap-3">
            <div className={theme.sectionLabel}>
              Filters{" "}
              <span className="normal-case tracking-normal opacity-80">
                {advancedFilters
                  ? "(advanced filtering — nest AND/OR groups up to 3 levels)"
                  : filterGroups.length > 1
                    ? "(a title passes if any group matches; all rules within a group must match)"
                    : "(all must match)"}
              </span>
            </div>
            <label
              className={`ml-auto flex items-center gap-2 ${theme.label} select-none ${
                advancedFilters && !canUseSimpleFilters ? "cursor-not-allowed" : "cursor-pointer"
              }`}
              title={
                advancedFilters && !canUseSimpleFilters
                  ? "This recipe uses logic the simple layout can't represent — simplify groups to switch back"
                  : "Switch between simple OR groups and advanced filtering"
              }
            >
              <span className={advancedFilters && !canUseSimpleFilters ? "opacity-60" : undefined}>
                Advanced filtering
              </span>
              <ToggleSwitch
                checked={advancedFilters}
                disabled={advancedFilters && !canUseSimpleFilters}
                onChange={handleAdvancedFiltersToggle}
                accentHex={accentHex}
                ariaLabel="Advanced filtering"
              />
            </label>
          </div>
          {advancedFilters ? (
            renderTreeGroup(filterTree, [], 1)
          ) : (
            <>
          {filterGroups.length === 0 ? (
            <div className={theme.dashedPanel}>
              No filters — every matched title passes through.
            </div>
          ) : null}
          {filterGroups.map((group, groupIndex) => {
            const groupBody = (
              <>
                <div className="flex flex-col gap-2.5">
                  {group.map((block, ruleIndex) => (
                    <BlockCard
                      key={`${block.field}-${ruleIndex}`}
                      icon={filterMeta(block.field).icon}
                      title={filterMeta(block.field).label}
                      accentHex={accentHex}
                      onRemove={() => removeFilter(groupIndex, ruleIndex)}
                    >
                      {renderFilterConfig(block, (patch) => updateFilter(groupIndex, ruleIndex, patch))}
                    </BlockCard>
                  ))}
                </div>
                <div className="mt-2.5">
                  <AddBlockMenu
                    label="Add filter"
                    options={(Object.keys(FILTER_META) as CollectionFilterField[]).map((field) => ({
                      key: field,
                      label: FILTER_META[field].label,
                      icon: FILTER_META[field].icon,
                    }))}
                    onAdd={(key) => addFilter(groupIndex, key as CollectionFilterField)}
                  />
                </div>
              </>
            );
            // A single group renders without group chrome — exactly like the flat layout.
            if (filterGroups.length === 1) {
              return <div key={groupIndex}>{groupBody}</div>;
            }
            return (
              <div key={groupIndex}>
                {groupIndex > 0 ? <PipelineConnector label="OR" accentHex={accentHex} /> : null}
                <div className={`rounded-xl border p-3 ${theme.divider}`}>
                  <div className="mb-2 flex items-center gap-2">
                    <span
                      className="rounded-md px-1.5 py-0.5 text-[11px] font-headline uppercase tracking-wider"
                      style={{ color: accentHex, backgroundColor: `${accentHex}1f` }}
                    >
                      Group {groupIndex + 1}
                    </span>
                    <span className={theme.muted}>all rules must match</span>
                    <button
                      type="button"
                      onClick={() => removeGroup(groupIndex)}
                      className={`ml-auto material-symbols-outlined transition-colors ${theme.iconAction}`}
                      style={{ fontSize: 17 }}
                      title="Remove group"
                    >
                      close
                    </button>
                  </div>
                  {groupBody}
                </div>
              </div>
            );
          })}
          <AddBlockMenu
            label={filterGroups.length ? "Add OR group" : "Add filter"}
            options={(Object.keys(FILTER_META) as CollectionFilterField[]).map((field) => ({
              key: field,
              label: FILTER_META[field].label,
              icon: FILTER_META[field].icon,
            }))}
            onAdd={(key) => addGroup(key as CollectionFilterField)}
          />
            </>
          )}
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
                onChange={(e) => {
                  const next = e.target.value as CollectionDefinition["sort"];
                  setDefinition((prev) => ({
                    ...prev,
                    sort: next,
                    sort_provider:
                      next === "rating"
                        ? sectionType === "movie"
                          ? (prev.sort_provider ?? "imdb")
                          : null
                        : null,
                  }));
                }}
              >
                <option value="popularity">Popularity / list rank</option>
                <option value="release_date">Release date (newest)</option>
                <option value="latest_aired">Newest content first{sectionType === "show" ? " (latest aired episode)" : ""}</option>
                <option value="rating">Rating (highest first)</option>
                <option value="title">Title (A–Z)</option>
              </select>
            </label>
            {definition.sort === "rating" ? (
              sectionType === "movie" ? (
                <label className={`flex items-center gap-2 ${theme.label}`}>
                  Source
                  <select
                    className={theme.selectField}
                    value={definition.sort_provider ?? "imdb"}
                    onChange={(e) =>
                      setDefinition((prev) => ({
                        ...prev,
                        sort_provider: e.target.value as CollectionRatingProvider,
                      }))
                    }
                  >
                    {MOVIE_RATING_PROVIDERS.map((p) => (
                      <option key={p.value} value={p.value}>
                        {p.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <span className="text-[13px] text-slate-500" title="Sonarr exposes a single score from Skyhook (usually IMDb when mapped)">
                  Sonarr rating
                </span>
              )
            ) : null}
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
                plex_section_ids: sectionIds,
                plex_section_type: sectionType,
                collection_title: collectionTitle.trim(),
                definition,
                run_interval_hours: runIntervalHours,
                active_window: activeWindow
                  ? {
                      start: joinMonthDay(
                        splitMonthDay(activeWindow.start).month,
                        splitMonthDay(activeWindow.start).day,
                      ),
                      end: joinMonthDay(
                        splitMonthDay(activeWindow.end).month,
                        splitMonthDay(activeWindow.end).day,
                      ),
                      when_inactive: activeWindow.when_inactive === "clear" ? "clear" : "keep",
                    }
                  : null,
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
                      <span className={theme.previewValue}>{stage.value == null ? "—" : stage.value}</span>
                    </div>
                  ))}
                </div>
                {preview?.plex_error ? (
                  <p className="mt-2 text-[12px] text-yellow-400/90">{preview.plex_error}</p>
                ) : null}
                {preview && preview.unresolved != null && preview.unresolved > 0 ? (
                  <p className="mt-2 text-[12px] text-slate-500">
                    {preview.unresolved} matched title{preview.unresolved === 1 ? "" : "s"} not present in
                    {sectionIds.length > 1 ? " a selected library" : " the target library"} and will be skipped there.
                  </p>
                ) : null}
                {preview?.libraries && preview.libraries.length > 1 ? (
                  <div className="mt-2 flex flex-col gap-0.5">
                    {preview.libraries.map((lib) => {
                      const title = props.sections.find((s) => s.id === lib.plex_section_id)?.title ?? `Section ${lib.plex_section_id}`;
                      return (
                        <p key={lib.plex_section_id} className={`text-[12px] ${theme.muted}`}>
                          {title}: {lib.in_target_library ?? "—"} in library
                          {lib.unresolved ? ` · ${lib.unresolved} missing` : ""}
                          {lib.plex_error ? ` · ${lib.plex_error}` : ""}
                        </p>
                      );
                    })}
                  </div>
                ) : null}

                <div className={`mt-3 border-t pt-3 ${theme.divider}`}>
                  <div className={`${theme.sectionLabel} mb-2`}>Check a title</div>
              <div className="relative" ref={explainBoxRef}>
                <input
                  className={`${theme.field} w-full`}
                  value={explainQuery}
                  placeholder="Search your catalog…"
                  disabled={!sectionId}
                  onChange={(e) => {
                    setExplainQuery(e.target.value);
                    setExplainOpen(true);
                  }}
                  onFocus={() => setExplainOpen(true)}
                />
                {explainOpen && explainResults.length ? (
                  <div className={`${theme.dropdown} max-h-48 overflow-y-auto`}>
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
                <div className="mt-3">
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
                    <div className="mt-2 flex flex-col gap-1">
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
                          {stage.detail ? <p className="ml-6 text-[12px] text-slate-500">{stage.detail}</p> : null}
                          {stage.key === "filters" && stage.tree ? (
                            <div className="ml-6 flex flex-col gap-0.5">
                              <ExplainTreeNodeView node={collapseTrivialExplainNode(stage.tree)} />
                            </div>
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
              ) : (
                <p className={`mt-2 ${theme.muted}`}>
                  Trace a catalog title through sources, filters, pins, and the final cut.
                </p>
              )}
                </div>

                {preview?.sample?.length || showMissingPane ? (
                  <div className={`mt-3 border-t pt-3 ${theme.divider}`}>
                    {showMissingPane ? (
                      <div className="mb-2 flex flex-wrap gap-1">
                        <button
                          type="button"
                          onClick={() => setPreviewPane("catalog")}
                          className={`rounded-md border px-2 py-1 text-[11px] font-headline uppercase tracking-wider ${
                            previewPane === "catalog" ? "text-[#0a0e14]" : theme.chipInactive
                          }`}
                          style={previewPane === "catalog" ? { backgroundColor: accentHex, borderColor: accentHex } : undefined}
                        >
                          In catalog
                        </button>
                        <button
                          type="button"
                          onClick={() => setPreviewPane("missing")}
                          className={`rounded-md border px-2 py-1 text-[11px] font-headline uppercase tracking-wider ${
                            previewPane === "missing" ? "text-[#0a0e14]" : theme.chipInactive
                          }`}
                          style={previewPane === "missing" ? { backgroundColor: accentHex, borderColor: accentHex } : undefined}
                        >
                          Missing from ARR ({missingCount})
                        </button>
                      </div>
                    ) : (
                      <div className={`${theme.sectionLabel} mb-2`}>Sample</div>
                    )}
                    {previewPane === "missing" && showMissingPane ? (
                      <>
                        {missingItems.length ? (
                        <div className="mb-3 flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            className={`rounded-md border px-2.5 py-1 text-[11px] font-headline uppercase tracking-wider ${theme.chipInactive}`}
                            onClick={() => setSelectedMissing(new Set(missingItems.map(missingKey)))}
                          >
                            Select all
                          </button>
                          <button
                            type="button"
                            className={`rounded-md border px-2.5 py-1 text-[11px] font-headline uppercase tracking-wider ${theme.chipInactive}`}
                            onClick={() => setSelectedMissing(new Set())}
                          >
                            Select none
                          </button>
                          <span className={`text-[12px] ${theme.muted}`}>
                            {selectedMissing.size} selected
                          </span>
                          <button
                            type="button"
                            disabled={selectedMissing.size < 1}
                            onClick={() => setArrModalOpen(true)}
                            className="ml-auto rounded-lg px-4 py-1.5 text-[13px] font-headline uppercase tracking-wider text-[#0a0e14] disabled:opacity-40"
                            style={{ backgroundColor: accentHex }}
                          >
                            Add to {sectionType === "movie" ? "Radarr" : "Sonarr"}
                          </button>
                        </div>
                        ) : null}
                        {missingItems.length && missingGaps.length ? (
                          <div className="mb-2 text-[12px] text-yellow-400/90">
                            <p>The following filters cannot be applied because the source list did not include this data:</p>
                            <ul className="mt-1 list-disc pl-4">
                              {missingGaps.map((gap) => (
                                <li key={gap}>{gap}</li>
                              ))}
                            </ul>
                            <p className="mt-1">
                              This list may include extra {sectionType === "movie" ? "titles" : "series"} that would
                              not pass those filters. Add the {sectionType === "movie" ? "titles" : "series"} you want
                              to {sectionType === "movie" ? "Radarr" : "Sonarr"}. After the next Placeholdarr sync, the
                              collection recipe filters them in or out as usual.
                            </p>
                          </div>
                        ) : null}
                        {missingItems.length ? (
                        <div className="grid grid-cols-4 gap-2 sm:grid-cols-5 xl:grid-cols-6">
                          {missingItems.map((item) => {
                            const key = missingKey(item);
                            const selected = selectedMissing.has(key);
                            return (
                              <button
                                type="button"
                                key={key}
                                title={`${item.title}${item.year ? ` (${item.year})` : ""}`}
                                onClick={() => {
                                  setSelectedMissing((prev) => {
                                    const next = new Set(prev);
                                    if (next.has(key)) next.delete(key);
                                    else next.add(key);
                                    return next;
                                  });
                                }}
                                className={`relative aspect-[2/3] w-full overflow-hidden rounded-md ${theme.posterFallback}`}
                                style={selected ? { boxShadow: `0 0 0 2px ${accentHex}` } : undefined}
                              >
                                {item.poster ? (
                                  <img
                                    src={item.poster}
                                    alt={item.title}
                                    className="h-full w-full object-cover"
                                    loading="lazy"
                                  />
                                ) : (
                                  <span className="block p-1 text-[9px] leading-tight">{item.title}</span>
                                )}
                                <span className="absolute left-1 top-1 rounded bg-black/60 px-1 text-[10px] text-white">
                                  {selected ? "✓" : ""}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                        ) : arrIncludeLabels.length ? (
                          <div className={`mb-2 ${theme.muted}`}>
                            <p>
                              The following filters only apply to {sectionType === "movie" ? "titles" : "series"} already
                              in {sectionType === "movie" ? "Radarr" : "Sonarr"}:
                            </p>
                            <ul className="mt-1 list-disc pl-4">
                              {arrIncludeLabels.map((label) => (
                                <li key={label}>{label}</li>
                              ))}
                            </ul>
                            <p className="mt-1">
                              {sectionType === "movie" ? "Titles" : "Series"} that are not in{" "}
                              {sectionType === "movie" ? "Radarr" : "Sonarr"} cannot satisfy them, so none are listed.
                            </p>
                          </div>
                        ) : (
                          <p className={`mb-2 ${theme.muted}`}>
                            No {sectionType === "movie" ? "titles" : "series"} left after the filters we could apply from
                            the list.
                          </p>
                        )}
                      </>
                    ) : preview?.sample?.length ? (
                      <div className="grid grid-cols-4 gap-2 sm:grid-cols-5 xl:grid-cols-6">
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
                    ) : (
                      <p className={theme.muted}>No catalog sample for this recipe.</p>
                    )}
                  </div>
                ) : null}
              </>
            )}

          </div>
        </div>
      </aside>
    </div>
      {arrModalOpen ? (
        <ArrAddModal
          mediaType={sectionType}
          items={missingItems.filter((item) => selectedMissing.has(missingKey(item)))}
          defaultTag={name.trim() || "placeholdarr"}
          accentHex={accentHex}
          onClose={() => setArrModalOpen(false)}
        />
      ) : null}
    </CollectionThemeProvider>
  );
}
