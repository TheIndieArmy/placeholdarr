import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { fetchJson } from "../api/client";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import {
  DATE_FORMAT_OPTIONS,
  LAYOUT_STAMPED_DATE_TOKENS,
  STATUS_AND_LAYOUT_DATE_TOKENS,
  formatDateToken,
  normalizeDateFormat,
  type DateFormatOption,
} from "../dateFormats";
import { UI_SECTION_FRAME_CLASS } from "../uiSectionFrame";
import {
  buildLayoutExportPayload,
  downloadJsonFile,
  layoutExportFilename,
  parseLayoutImportPayload,
  parseLayoutProfileKey,
  type LayoutSharePayload,
  type LayoutShareScope,
} from "./posterLayoutShare";

export type LabelSize = "small" | "medium" | "large";
export type LabelStyle = "chip" | "banner";
/** @deprecated Legacy edge; migrated to x/y ranges on load. */
export type BannerEdge = "top" | "bottom" | "left" | "right";

export type PosterLayoutLabel = {
  id: string;
  token: string;
  /** Horizontal center as a fraction of poster width (0..1). */
  x: number;
  /** Vertical center as a fraction of poster height (0..1). */
  y: number;
  /** Legacy snap cell; kept for migration. */
  row?: number;
  col?: number;
  text_color: string;
  fill_color: string;
  border_color: string;
  fill_opacity: number;
  border_opacity: number;
  size: LabelSize;
  /** Continuous chip/badge scale (1 = small, 1.5 = medium, 2 = large). */
  size_scale?: number;
  /** Arted Rating logo placement: left / right / above / below / fill. */
  rating_art_layout?: "left" | "right" | "above" | "below" | "fill";
  /** Legacy single depth; prefer y_end - y_start. */
  thickness: number;
  /** Horizontal coverage start/end (0..1). */
  x_start: number;
  x_end: number;
  /** Vertical coverage start/end (0..1). */
  y_start: number;
  y_end: number;
  style: LabelStyle;
  /** Legacy fields kept optional for older drafts in memory. */
  span_start?: number;
  span_end?: number;
  depth_start?: number;
  depth_end?: number;
  orientation?: "horizontal" | "vertical";
  edge?: BannerEdge;
  arted: boolean;
  rating_source: string;
  /** User-entered copy for the Custom chip token. */
  custom_text: string;
};

export type PosterLayoutProfile = { labels: PosterLayoutLabel[]; grayscale: boolean };

export type PosterLayoutCatalog = {
  ok?: boolean;
  version: number;
  profiles: Record<string, PosterLayoutProfile>;
  profile_keys: string[];
  tokens: Array<{ name: string; label: string }>;
  allowed_tokens: Record<string, string[]>;
  sample_tokens?: Record<string, Record<string, string>>;
  grids: Record<string, { cols: number; rows: number }>;
  fingerprint: string;
  sizes?: LabelSize[];
  styles?: LabelStyle[];
  arted_tokens?: string[];
  rating_sources?: string[];
  custom_template_tokens?: Record<string, string[]>;
  custom_text_max_length?: number;
  custom_date_tokens?: string[];
  custom_date_formats?: Array<{ id: string; label: string; example: string }>;
  rating_art_layouts?: Array<"left" | "right" | "above" | "below" | "fill">;
  size_scale_min?: number;
  size_scale_max?: number;
  size_scale_presets?: Record<string, number>;
};

const LABEL_SIZES: LabelSize[] = ["small", "medium", "large"];
const SIZE_SCALE_PRESETS: Record<LabelSize, number> = { small: 1, medium: 1.5, large: 2 };
const SIZE_SCALE_MIN = 0.5;
const SIZE_SCALE_MAX = 2.5;
const RATING_ART_LAYOUTS: Array<{ id: "left" | "right" | "above" | "below" | "fill"; label: string }> = [
  { id: "left", label: "Left" },
  { id: "right", label: "Right" },
  { id: "above", label: "Above" },
  { id: "below", label: "Below" },
  { id: "fill", label: "Fill" },
];
const FILL_RECTANGLE_TOKEN = "FillRectangle";
const FILL_RECTANGLE_LABEL = "Fill rectangle";
/** Invisible Custom chip used so the empty editor matches the tallest selected layout. */
const EDITOR_HEIGHT_PROBE: PosterLayoutLabel = {
  id: "__editor_height__",
  token: "Custom",
  x: 0.5,
  y: 0.5,
  text_color: "#ffffff",
  fill_color: "#000000",
  border_color: "#ffffff",
  fill_opacity: 0.65,
  border_opacity: 0,
  size: "medium",
  size_scale: 1.5,
  rating_art_layout: "left",
  thickness: 0.12,
  x_start: 0,
  x_end: 1,
  y_start: 0,
  y_end: 0.12,
  style: "chip",
  arted: true,
  rating_source: "imdb",
  custom_text: "{Title} {Sep} {Year}",
};
const BANNER_SIZE_MULT: Record<LabelSize, number> = { small: 0.72, medium: 1, large: 1.35 };
const BANNER_THICKNESS_MIN = 0.04;
const BANNER_THICKNESS_MAX = 0.45;
/** Minimum axis width so the fill stays visible; thumbs can nearly meet. */
const BANNER_SPAN_MIN_GAP = 0.01;

function normalizeBannerSpan(start: unknown, end: unknown): { start: number; end: number } {
  const one = (raw: unknown, fallback: number) => {
    const n = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(n)) return fallback;
    return Math.max(0, Math.min(1, n));
  };
  let a = one(start, 0);
  let b = one(end, 1);
  if (b < a) [a, b] = [b, a];
  if (b - a < BANNER_SPAN_MIN_GAP) {
    const mid = (a + b) / 2;
    a = Math.max(0, mid - BANNER_SPAN_MIN_GAP / 2);
    b = Math.min(1, a + BANNER_SPAN_MIN_GAP);
    if (b - a < BANNER_SPAN_MIN_GAP) a = Math.max(0, b - BANNER_SPAN_MIN_GAP);
  }
  return { start: a, end: b };
}

/** One track, two draggable thumbs for an axis range (bake-px UI). */
function BannerSpanSlider(props: {
  start: number;
  end: number;
  /** Bake pixels for this axis. */
  dim: number;
  accentHex: string;
  onChange: (start: number, end: number) => void;
  onInteract?: () => void;
}) {
  const dim = Math.max(1, Math.round(props.dim));
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<"start" | "end" | null>(null);
  const spanRef = useRef({ start: props.start, end: props.end });
  const onChangeRef = useRef(props.onChange);
  const onInteractRef = useRef(props.onInteract);
  const span = normalizeBannerSpan(props.start, props.end);
  spanRef.current = span;
  onChangeRef.current = props.onChange;
  onInteractRef.current = props.onInteract;
  const startPx = Math.round(span.start * dim);
  const endPx = Math.round(span.end * dim);

  const valueFromClientX = (clientX: number) => {
    const el = trackRef.current;
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return 0;
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  };

  const applyDrag = (clientX: number) => {
    const which = dragRef.current;
    if (!which) return;
    const cur = spanRef.current;
    const v = valueFromClientX(clientX);
    if (which === "start") {
      onChangeRef.current(Math.min(v, cur.end - BANNER_SPAN_MIN_GAP), cur.end);
    } else {
      onChangeRef.current(cur.start, Math.max(v, cur.start + BANNER_SPAN_MIN_GAP));
    }
  };

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (!dragRef.current) return;
      applyDrag(e.clientX);
    };
    const onUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, []);

  const beginThumb = (which: "start" | "end") => (e: ReactPointerEvent<HTMLButtonElement>) => {
    e.preventDefault();
    e.stopPropagation();
    onInteractRef.current?.();
    dragRef.current = which;
    applyDrag(e.clientX);
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
        <span>
          Start <span className="font-mono text-slate-300">{startPx}</span> px
        </span>
        <span>
          End <span className="font-mono text-slate-300">{endPx}</span> px
        </span>
      </div>
      <div
        ref={trackRef}
        className="relative h-7 w-full touch-none select-none"
        onPointerDown={(e) => {
          onInteractRef.current?.();
          const v = valueFromClientX(e.clientX);
          const distStart = Math.abs(v - span.start);
          const distEnd = Math.abs(v - span.end);
          dragRef.current = distStart <= distEnd ? "start" : "end";
          applyDrag(e.clientX);
        }}
      >
        <div className="pointer-events-none absolute left-0 right-0 top-1/2 h-1.5 -translate-y-1/2 rounded-full border border-[#424753]/55 bg-[#0b111b]" />
        <div
          className="pointer-events-none absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full"
          style={{
            left: `${span.start * 100}%`,
            width: `${(span.end - span.start) * 100}%`,
            backgroundColor: props.accentHex,
            opacity: 0.75,
          }}
        />
        {(["start", "end"] as const).map((which) => {
          const value = which === "start" ? span.start : span.end;
          return (
            <button
              key={which}
              type="button"
              aria-label={which === "start" ? "Range start" : "Range end"}
              className="absolute top-1/2 z-[2] h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-[#0b111b] shadow-sm"
              style={{ left: `${value * 100}%`, backgroundColor: props.accentHex }}
              onPointerDown={beginThumb(which)}
            />
          );
        })}
      </div>
    </div>
  );
}
const CUSTOM_TEXT_MAX_LEN = 120;
const CUSTOM_SEP_DEFAULT = "·";
const CUSTOM_DATE_TOKENS = new Set([
  "TheatricalDate",
  "DigitalDate",
  "PhysicalDate",
  "AirDate",
  "SeriesPremiereDate",
  "ComingSoonOrNewEp",
]);
const LEGACY_SINGLE_TOKEN_CHIPS = new Set([
  "ComingSoonOrNewEp",
  "PLACEHOLDER",
  "SeriesStatus",
  "Title",
  "Year",
  "Certification",
  "Genres",
  "Runtime",
  "TheatricalDate",
  "DigitalDate",
  "PhysicalDate",
  "AirDate",
  "SeriesPremiereDate",
  "SXXEYY",
]);

type PosterTextTokenMeta = { name: string; label: string; group: string };

const POSTER_TEXT_TOKEN_META: Record<string, Omit<PosterTextTokenMeta, "name">> = {
  PLACEHOLDER: { label: "PLACEHOLDER", group: "Media" },
  ComingSoonOrNewEp: { label: "Smart release / next episode", group: "Media" },
  Title: { label: "Title", group: "Media" },
  Year: { label: "Year", group: "Media" },
  Certification: { label: "Certification", group: "Media" },
  Genres: { label: "Genres", group: "Media" },
  Runtime: { label: "Runtime", group: "Media" },
  Rating: { label: "Rating", group: "Media" },
  Sep: { label: "Separator", group: "Media" },
  TheatricalDate: { label: "Theatrical date", group: "Movie" },
  DigitalDate: { label: "Digital date", group: "Movie" },
  PhysicalDate: { label: "Physical date", group: "Movie" },
  SeriesStatus: { label: "Series status", group: "TV" },
  SeriesPremiereDate: { label: "Series premiere date", group: "TV" },
  AirDate: { label: "Air date", group: "TV" },
  SXXEYY: { label: "Season/Episode (SxxEyy)", group: "TV" },
};
const POSTER_TEXT_TOKEN_GROUP_ORDER = ["Media", "Movie", "TV"];

/** Fresh regex each call (global flag must not share ``lastIndex``). */
function customDateTokenRe(): RegExp {
  return /\{(TheatricalDate|DigitalDate|PhysicalDate|AirDate|SeriesPremiereDate|ComingSoonOrNewEp)(?::([A-Za-z0-9_-]+))?\}/g;
}

function formatStampedComingSoonToken(
  label: string,
  dateIso: string,
  fmt: string | undefined,
): string {
  const text = String(label || "").trim();
  if (!text) return "";
  if (!fmt) return text;
  const iso = String(dateIso || "").trim();
  if (!iso) return text;
  const formatted = formatDateToken(iso, fmt);
  if (!formatted) return text;
  if (text.includes(" - ")) return `${text.split(" - ")[0]} - ${formatted}`;
  return formatted;
}

type MediaKind = "movie" | "tv" | "episode";
type Availability = "placeholder" | "real";
type Variant = "default" | "coming_soon";
type ApplyScope = "now" | "next_full_sync" | "future";

const MEDIA_TABS: Array<{ kind: MediaKind; label: string }> = [
  { kind: "movie", label: "Movie" },
  { kind: "tv", label: "TV" },
  { kind: "episode", label: "Episode" },
];

const AVAILABILITY_TABS: Array<{ availability: Availability; label: string }> = [
  { availability: "placeholder", label: "Placeholder" },
  { availability: "real", label: "Real" },
];

const VARIANT_TABS: Array<{ variant: Variant; label: string }> = [
  { variant: "default", label: "Released" },
  { variant: "coming_soon", label: "Coming Soon" },
];

function mediaKindLabel(kind: MediaKind): string {
  if (kind === "tv") return "TV";
  if (kind === "episode") return "Episode";
  return "Movie";
}

function variantLabel(variant: Variant): string {
  return variant === "coming_soon" ? "Coming Soon" : "Released";
}

/** When each timing profile is used (shown under tabs + hover titles). */
function variantWhenUsed(kind: MediaKind): { released: string; comingSoon: string; note: string } {
  if (kind === "tv") {
    return {
      released:
        "Series that are completed or continuing without a known unaired episode (such as when between seasons) use the Released poster.",
      comingSoon: "Series with a known unaired episode use the Coming Soon poster.",
      note:
        "Series with a known unaired episode use the Coming Soon poster. Series that are completed or continuing without a known unaired episode (such as when between seasons) use the Released poster.",
    };
  }
  if (kind === "episode") {
    return {
      released: "Released when this episode has no upcoming air date.",
      comingSoon: "Coming Soon for episodes with an upcoming air date.",
      note: "Episodes with an upcoming air date use the Coming Soon poster. Otherwise they use Released.",
    };
  }
  return {
    released: "Released when there is no upcoming preferred release date.",
    comingSoon: "Coming Soon for movies with an upcoming preferred release date.",
    note: "Movies with an upcoming preferred release date use the Coming Soon poster. Otherwise they use Released.",
  };
}

type CopyDest = {
  key: string;
  kind: MediaKind;
  variant: Variant;
  label: string;
};

function copyDestinations(kind: MediaKind, availability: Availability, variant: Variant): CopyDest[] {
  const out: CopyDest[] = [];
  for (const media of MEDIA_TABS) {
    for (const tab of VARIANT_TABS) {
      if (media.kind === kind && tab.variant === variant) continue;
      out.push({
        key: profileKey(media.kind, availability, tab.variant),
        kind: media.kind,
        variant: tab.variant,
        label: `${tab.label} (${media.label})`,
      });
    }
  }
  return out;
}

/** Quick-fill templates for Custom chips (recreate the old smart Coming Soon chip). */
type CustomTextPreset = {
  id: string;
  label: string;
  template: string;
  /** Omit to show for every media kind. */
  kinds?: MediaKind[];
};

const CUSTOM_TEXT_PRESETS: CustomTextPreset[] = [
  {
    id: "smart-cs",
    label: "Smart release / next episode",
    template: "{ComingSoonOrNewEp}",
  },
  {
    id: "title-year",
    label: "Title and year",
    template: "{Title} {Sep} {Year}",
  },
  {
    id: "theatrical",
    label: "Theatrical date",
    template: "Theatrical - {TheatricalDate}",
    kinds: ["movie"],
  },
  {
    id: "digital",
    label: "Digital date",
    template: "Digital - {DigitalDate}",
    kinds: ["movie"],
  },
  {
    id: "physical",
    label: "Physical date",
    template: "Physical - {PhysicalDate}",
    kinds: ["movie"],
  },
  {
    id: "airing",
    label: "Airing date",
    template: "Airing - {AirDate}",
    kinds: ["episode"],
  },
  {
    id: "premiere",
    label: "Series premiere",
    template: "Premiere - {SeriesPremiereDate}",
    kinds: ["tv", "episode"],
  },
  {
    id: "status",
    label: "Series status",
    template: "{SeriesStatus}",
    kinds: ["tv", "episode"],
  },
  {
    id: "sxxeyy",
    label: "Title and episode code",
    template: "{Title} {Sep} {SXXEYY}",
    kinds: ["tv", "episode"],
  },
  {
    id: "placeholder",
    label: "PLACEHOLDER",
    template: "{PLACEHOLDER}",
  },
];

/** Bundled overlay-examples samples (see frontend/public/overlay-examples/meta.json). */
const DEFAULT_PREVIEW_ART: Record<MediaKind, { tmdbId: string; title: string }> = {
  movie: { tmdbId: "1226863", title: "The Super Mario Galaxy Movie" },
  tv: { tmdbId: "1396", title: "Breaking Bad" },
  episode: { tmdbId: "1396", title: "Ozymandias" },
};

const FIELD_CLASS =
  "w-full min-w-0 rounded-lg border border-[#424753]/55 bg-[#0b111b] px-2.5 py-1.5 text-[13px] text-slate-100 outline-none focus:border-[color:var(--brand-accent)]";
const SELECT_CLASS = `${FIELD_CLASS} appearance-none`;

function profileKey(kind: MediaKind, availability: Availability, variant: Variant): string {
  return `${kind}.${availability}.${variant}`;
}

/** Match bake: light fill → blue mark; otherwise yellow on navy. */
function badgeLogoVariant(fillColor: string | undefined): "dark" | "light" {
  const h = String(fillColor || "").replace("#", "");
  if (h.length < 6) return "dark";
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  if ([r, g, b].some((n) => Number.isNaN(n))) return "dark";
  return 0.2126 * r + 0.7152 * g + 0.0722 * b >= 160 ? "light" : "dark";
}

function previewKind(kind: MediaKind): string {
  if (kind === "tv") return "series";
  return kind;
}

function newLabelId(): string {
  return Math.random().toString(16).slice(2, 14);
}

function cloneProfiles(profiles: Record<string, PosterLayoutProfile>): Record<string, PosterLayoutProfile> {
  const cloned = JSON.parse(JSON.stringify(profiles)) as Record<string, PosterLayoutProfile>;
  for (const key of Object.keys(cloned)) {
    const labels = cloned[key]?.labels;
    const landscape = key.startsWith("episode.");
    cloned[key] = {
      labels: Array.isArray(labels) ? labels.map((l) => normalizeLabelColors(l, landscape)) : [],
      grayscale: Boolean(cloned[key]?.grayscale),
    };
  }
  return cloned;
}

function hexOr(value: string, fallback: string): string {
  const text = String(value || "").trim();
  if (/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(text)) return text;
  return fallback;
}

function solidHex(value: string, fallback: string): string {
  const text = hexOr(value, fallback);
  return text.length >= 7 ? text.slice(0, 7) : fallback;
}

function normalizeLabelSize(raw: unknown, token: string): LabelSize {
  const text = String(raw || "").trim().toLowerCase();
  if (text === "small" || text === "medium" || text === "large") return text;
  return token === "Badge" || token === FILL_RECTANGLE_TOKEN || token === "Banner" ? "medium" : "small";
}

function normalizeSizeScale(raw: unknown, size: LabelSize): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (Number.isFinite(n)) return Math.max(SIZE_SCALE_MIN, Math.min(SIZE_SCALE_MAX, n));
  return SIZE_SCALE_PRESETS[size] ?? 1;
}

function nearestSizePreset(scale: number, presets: Record<string, number> = SIZE_SCALE_PRESETS): LabelSize {
  let best: LabelSize = "small";
  let bestDist = Infinity;
  for (const key of LABEL_SIZES) {
    const dist = Math.abs((presets[key] ?? SIZE_SCALE_PRESETS[key]) - scale);
    if (dist < bestDist) {
      bestDist = dist;
      best = key;
    }
  }
  return best;
}

function normalizeRatingArtLayout(raw: unknown): "left" | "right" | "above" | "below" | "fill" {
  const key = String(raw || "").trim().toLowerCase();
  if (key === "left" || key === "right" || key === "above" || key === "below" || key === "fill") return key;
  if (key === "beside" || key === "inline" || key === "side") return "left";
  if (key === "top") return "above";
  if (key === "bottom") return "below";
  if (key === "background" || key === "bg") return "fill";
  return "left";
}

function normalizeLabelStyle(raw: unknown, token: string): LabelStyle {
  if (
    token === FILL_RECTANGLE_TOKEN ||
    token === "Banner" ||
    String(raw || "").trim().toLowerCase() === "banner"
  ) {
    return "banner";
  }
  return "chip";
}

function isFillRectangleLabel(label: Pick<PosterLayoutLabel, "token" | "style">): boolean {
  return normalizeLabelStyle(label.style, label.token) === "banner";
}

/** @deprecated alias */
const isBannerLabel = isFillRectangleLabel;

function normalizeBannerEdge(raw: unknown): BannerEdge {
  const text = String(raw || "").trim().toLowerCase();
  if (text === "top" || text === "bottom" || text === "left" || text === "right") return text;
  return "top";
}

function bannerBaseFrac(edge: BannerEdge, landscape: boolean): number {
  const side = edge === "left" || edge === "right";
  if (side) return landscape ? 0.14 : 0.16;
  return landscape ? 0.18 : 0.11;
}

/** Legacy single-depth clamp (edge banners). */
function normalizeBannerThickness(
  raw: unknown,
  edge: BannerEdge,
  size: LabelSize,
  landscape: boolean,
): number {
  if (typeof raw === "number" && !Number.isNaN(raw)) {
    let n = raw;
    if (n > 1) n = n / 100;
    return Math.max(BANNER_THICKNESS_MIN, Math.min(BANNER_THICKNESS_MAX, n));
  }
  const frac = bannerBaseFrac(edge, landscape) * (BANNER_SIZE_MULT[size] || 1);
  return Math.max(BANNER_THICKNESS_MIN, Math.min(BANNER_THICKNESS_MAX, frac));
}

type FillRectGeom = { xStart: number; xEnd: number; yStart: number; yEnd: number };

/** Resolve x/y ranges (migrates legacy orientation/span/depth or edge+thickness). */
function fillRectGeometryFromLabel(
  label: Pick<
    PosterLayoutLabel,
    | "x_start"
    | "x_end"
    | "y_start"
    | "y_end"
    | "orientation"
    | "edge"
    | "thickness"
    | "span_start"
    | "span_end"
    | "depth_start"
    | "depth_end"
    | "size"
    | "token"
  >,
  landscape: boolean,
): FillRectGeom {
  const size = normalizeLabelSize(label.size, label.token);
  const hasXy =
    label.x_start != null || label.x_end != null || label.y_start != null || label.y_end != null;
  if (hasXy) {
    const x = normalizeBannerSpan(label.x_start, label.x_end);
    const y = normalizeBannerSpan(label.y_start, label.y_end);
    return { xStart: x.start, xEnd: x.end, yStart: y.start, yEnd: y.end };
  }

  const span = normalizeBannerSpan(label.span_start, label.span_end);
  const hasDepth = label.depth_start != null || label.depth_end != null;
  const orientRaw = String(label.orientation || "").trim().toLowerCase();
  const hasOrient = orientRaw === "horizontal" || orientRaw === "vertical";
  const hasLegacyEdge = label.edge != null && String(label.edge).trim() !== "";

  if (hasDepth || (hasOrient && !hasLegacyEdge)) {
    const orientation = hasOrient ? orientRaw : "horizontal";
    const depth = hasDepth
      ? normalizeBannerSpan(label.depth_start, label.depth_end)
      : { start: 0, end: normalizeBannerThickness(label.thickness, "top", size, landscape) };
    if (orientation === "vertical") {
      return {
        xStart: depth.start,
        xEnd: depth.end,
        yStart: span.start,
        yEnd: span.end,
      };
    }
    return {
      xStart: span.start,
      xEnd: span.end,
      yStart: depth.start,
      yEnd: depth.end,
    };
  }

  const edge = normalizeBannerEdge(label.edge);
  const thick = normalizeBannerThickness(label.thickness, edge, size, landscape);
  if (edge === "bottom") {
    return {
      xStart: span.start,
      xEnd: span.end,
      yStart: Math.max(0, 1 - thick),
      yEnd: 1,
    };
  }
  if (edge === "left") {
    return { xStart: 0, xEnd: thick, yStart: span.start, yEnd: span.end };
  }
  if (edge === "right") {
    return {
      xStart: Math.max(0, 1 - thick),
      xEnd: 1,
      yStart: span.start,
      yEnd: span.end,
    };
  }
  return { xStart: span.start, xEnd: span.end, yStart: 0, yEnd: thick };
}

function fillRectCenterXY(geom: FillRectGeom): { x: number; y: number } {
  return { x: (geom.xStart + geom.xEnd) / 2, y: (geom.yStart + geom.yEnd) / 2 };
}

/** Translate a fill rect so its center sits at (cx, cy); keep widths fixed. */
function translateFillRectToCenter(geom: FillRectGeom, cx: number, cy: number): FillRectGeom {
  const w = Math.max(BANNER_SPAN_MIN_GAP, geom.xEnd - geom.xStart);
  const h = Math.max(BANNER_SPAN_MIN_GAP, geom.yEnd - geom.yStart);
  let xStart = clampFrac(cx, 0.5) - w / 2;
  let yStart = clampFrac(cy, 0.5) - h / 2;
  xStart = Math.max(0, Math.min(1 - w, xStart));
  yStart = Math.max(0, Math.min(1 - h, yStart));
  return { xStart, xEnd: xStart + w, yStart, yEnd: yStart + h };
}

function normalizeCustomText(raw: unknown, token: string, maxLen = CUSTOM_TEXT_MAX_LEN): string {
  if (token !== "Custom") return "";
  return String(raw || "")
    .replace(/[\n\r]/g, " ")
    .trim()
    .slice(0, maxLen);
}

function templateHasDateToken(template: string): boolean {
  return customDateTokenRe().test(String(template || ""));
}

/** Rewrite date tokens in a preset/template to include the chosen format id. */
function applyDateFormatToTemplate(template: string, formatId: string): string {
  const fmt = normalizeDateFormat(formatId);
  return String(template || "").replace(customDateTokenRe(), (_m, name: string) => `{${name}:${fmt}}`);
}

/** Compare preset templates ignoring which date format was chosen. */
function presetMatchesCustomText(presetTemplate: string, customText: string): boolean {
  const norm = (text: string) =>
    String(text || "")
      .replace(customDateTokenRe(), (_m, name: string) => `{${name}}`)
      .trim();
  return norm(presetTemplate) === norm(customText);
}

/** Fill `{Token}` / `{DateToken:format}` placeholders; empty tokens collapse. */
function renderCustomChipText(template: string, tokenMap: Record<string, string>, separator = CUSTOM_SEP_DEFAULT): string {
  const sep = separator || CUSTOM_SEP_DEFAULT;
  const sentinel = "\u001e";
  const filled = String(template || "").replace(
    /\{([A-Za-z][A-Za-z0-9_]*)(?::([A-Za-z0-9_-]+))?\}/g,
    (_m, name: string, fmt: string | undefined) => {
      if (name === "Sep") return sentinel;
      const raw = String(tokenMap[name] ?? "").trim();
      if (!raw) return "";
      if (LAYOUT_STAMPED_DATE_TOKENS.has(name) || name === "ComingSoonOrNewEp") {
        return formatStampedComingSoonToken(raw, String(tokenMap.ComingSoonOrNewEpDate || ""), fmt);
      }
      if (CUSTOM_DATE_TOKENS.has(name) || STATUS_AND_LAYOUT_DATE_TOKENS.has(name)) {
        return formatDateToken(raw, fmt);
      }
      return raw;
    },
  );
  let rendered = filled;
  if (filled.includes(sentinel)) {
    const parts = filled
      .split(sentinel)
      .map((p) => p.trim())
      .filter(Boolean);
    rendered = parts.join(` ${sep} `);
  }
  return rendered
    .replace(/[ \t]+/g, " ")
    .replace(/\(\s+/g, "(")
    .replace(/\s+\)/g, ")")
    .replace(/\(\s*\)/g, "")
    .replace(/\[\s*\]/g, "")
    .replace(/\s+([,.;:!?])/g, "$1")
    .replace(/[ \t]+/g, " ")
    .trim();
}

type DateFormatPickerState = { mode: "preset"; preset: CustomTextPreset };

function PosterTextTokensModal(props: {
  open: boolean;
  value: string;
  maxLength: number;
  tokenNames: string[];
  dateTokenNames: Set<string>;
  dateFormatOptions: DateFormatOption[];
  sampleTokens: Record<string, string>;
  accentHex: string;
  onClose: () => void;
  onCommit: (next: string) => void;
}) {
  const [draft, setDraft] = useState(props.value);
  const [pendingDateToken, setPendingDateToken] = useState<string | null>(null);
  const editorRef = useRef<HTMLInputElement | null>(null);
  const cursorRef = useRef<number>(props.value.length);

  useEffect(() => {
    if (!props.open) return;
    setDraft(props.value);
    cursorRef.current = props.value.length;
    setPendingDateToken(null);
  }, [props.open, props.value]);

  if (!props.open) return null;

  const grouped = new Map<string, PosterTextTokenMeta[]>();
  for (const name of props.tokenNames) {
    const meta = POSTER_TEXT_TOKEN_META[name] || { label: name, group: "Media" };
    const entry: PosterTextTokenMeta = { name, label: meta.label, group: meta.group };
    const arr = grouped.get(entry.group) ?? [];
    arr.push(entry);
    grouped.set(entry.group, arr);
  }
  const orderedGroups = [
    ...POSTER_TEXT_TOKEN_GROUP_ORDER.filter((g) => grouped.has(g)),
    ...Array.from(grouped.keys()).filter((g) => !POSTER_TEXT_TOKEN_GROUP_ORDER.includes(g)),
  ];

  const preview = renderCustomChipText(draft, props.sampleTokens);

  function insert(placeholder: string) {
    const maxLen = props.maxLength;
    const cursor = Math.min(cursorRef.current, draft.length);
    const next = `${draft.slice(0, cursor)}${placeholder}${draft.slice(cursor)}`.slice(0, maxLen);
    setDraft(next);
    cursorRef.current = Math.min(cursor + placeholder.length, next.length);
    requestAnimationFrame(() => {
      const el = editorRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(cursorRef.current, cursorRef.current);
      }
    });
  }

  function insertToken(name: string) {
    if (props.dateTokenNames.has(name)) {
      setPendingDateToken(name);
      return;
    }
    insert(`{${name}}`);
  }

  function applyDateFormat(formatId: string) {
    if (!pendingDateToken) return;
    const fmt = normalizeDateFormat(formatId);
    insert(`{${pendingDateToken}:${fmt}}`);
    setPendingDateToken(null);
  }

  function commitAndClose() {
    props.onCommit(draft.replace(/[\n\r]/g, "").slice(0, props.maxLength));
    props.onClose();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) commitAndClose();
      }}
    >
      <div className={`w-full max-w-3xl ${UI_SECTION_FRAME_CLASS} bg-[#171c22] max-h-[85vh] flex flex-col overflow-hidden`}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#424753]/30">
          <div>
            <h3 className="text-[18px] font-bold text-white font-headline">Text tokens</h3>
            <p className="ui-field-description mt-1">
              Build the Text chip with poster tokens. Empty values collapse automatically. Date tokens ask for a
              format.
            </p>
          </div>
          <button type="button" onClick={commitAndClose} className="text-slate-400 hover:text-slate-100">
            <span className="material-symbols-outlined" style={{ fontSize: 22 }}>
              close
            </span>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {orderedGroups.map((groupName) => {
            const tokens = grouped.get(groupName) ?? [];
            return (
              <div key={groupName}>
                <h4 className="text-[12px] font-headline uppercase tracking-widest text-slate-400 mb-2">
                  {groupName}
                </h4>
                <div className="flex flex-wrap gap-2">
                  {tokens.map((token) => {
                    const sample = String(props.sampleTokens[token.name] ?? "").trim();
                    const isDate = props.dateTokenNames.has(token.name);
                    return (
                      <button
                        key={token.name}
                        type="button"
                        title={isDate ? `Insert {${token.name}:format}` : `Insert {${token.name}}`}
                        onClick={() => insertToken(token.name)}
                        className="flex flex-col items-start gap-0.5 rounded-lg border border-[#424753]/60 bg-[#1e2430] px-3 py-1.5 text-left text-slate-100 transition-colors hover:bg-[#252e3a]"
                      >
                        <span className="font-mono text-[13px]">{`{${token.name}}`}</span>
                        <span className="text-[11px] text-slate-500">
                          {token.label}
                          {sample ? ` · ${sample}` : ""}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        <div className="px-5 py-4 border-t border-[#424753]/30 space-y-2">
          <label className="block text-[12px] font-headline uppercase tracking-wider text-slate-400 mb-1">
            Template
          </label>
          <input
            ref={editorRef}
            className="w-full bg-[#0f1419] border border-[#424753]/40 rounded-lg px-3 py-2 text-[15px] text-slate-200 outline-none font-mono"
            value={draft}
            placeholder="{Title} {Sep} {Year}"
            maxLength={props.maxLength}
            onChange={(e) => {
              setDraft(e.target.value.replace(/[\n\r]/g, ""));
              cursorRef.current = e.target.selectionStart ?? e.target.value.length;
            }}
            onKeyUp={(e) => {
              cursorRef.current = e.currentTarget.selectionStart ?? e.currentTarget.value.length;
            }}
            onClick={(e) => {
              cursorRef.current = e.currentTarget.selectionStart ?? e.currentTarget.value.length;
            }}
          />
          <div className="flex items-center gap-2 text-[13px]">
            <span className="text-slate-500 font-headline uppercase tracking-wider">Preview:</span>
            <span className="text-slate-200 font-mono truncate">{preview || "(empty)"}</span>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={props.onClose}
              className="text-[14px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={commitAndClose}
              className={`px-4 py-2 ${FG_ON_ACCENT_TEXT_CLASS} text-[14px] font-headline uppercase tracking-wider rounded-lg`}
              style={accentFilledStyle(props.accentHex)}
            >
              Done
            </button>
          </div>
        </div>
      </div>

      {pendingDateToken ? (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) setPendingDateToken(null);
          }}
        >
          <div className="w-full max-w-md rounded-2xl border border-[#424753]/40 bg-[#171c22] overflow-hidden">
            <div className="px-5 py-4 border-b border-[#424753]/30">
              <h3 className="text-[18px] font-bold text-white font-headline">Date format</h3>
              <p className="ui-field-description mt-1">
                Choose a format for{" "}
                <span className="font-mono text-slate-300">{`{${pendingDateToken}}`}</span>. It will be inserted as{" "}
                <span className="font-mono text-slate-300">{`{${pendingDateToken}:format}`}</span>.
              </p>
            </div>
            <div className="px-5 py-4 space-y-2">
              {props.dateFormatOptions.map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => applyDateFormat(opt.id)}
                  className="flex w-full items-center justify-between gap-3 rounded-lg border border-[#424753]/55 px-3 py-2.5 text-left hover:border-[#424753] hover:bg-white/[0.03]"
                >
                  <span className="text-[13px] font-headline uppercase tracking-wider text-slate-200">
                    {opt.label}
                  </span>
                  <span className="font-mono text-[12px] text-slate-400">{opt.example}</span>
                </button>
              ))}
            </div>
            <div className="px-5 py-3 border-t border-[#424753]/30 flex justify-end">
              <button
                type="button"
                onClick={() => setPendingDateToken(null)}
                className="text-[14px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function clampFrac(raw: unknown, fallback = 0.5): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(1, n));
}

/** Prefer ``x``/``y``; migrate legacy snap-cell centers when missing. */
function normalizeLabelXY(
  label: Pick<PosterLayoutLabel, "x" | "y" | "row" | "col">,
  cols = 25,
  rows = 37,
): { x: number; y: number } {
  const hasX = label.x != null && Number.isFinite(Number(label.x));
  const hasY = label.y != null && Number.isFinite(Number(label.y));
  if (hasX || hasY) {
    return { x: clampFrac(label.x, 0.5), y: clampFrac(label.y, 0.5) };
  }
  const c = Math.max(1, cols);
  const r = Math.max(1, rows);
  const col = Math.max(0, Math.min(c - 1, Number(label.col) || 0));
  const row = Math.max(0, Math.min(r - 1, Number(label.row) || 0));
  return { x: (col + 0.5) / c, y: (row + 0.5) / r };
}

/** Preview number-badge anchor: banner strip center. */
function markerAnchorXY(label: PosterLayoutLabel, landscape: boolean): { x: number; y: number } {
  if (!isBannerLabel(label)) {
    return { x: clampFrac(label.x, 0.5), y: clampFrac(label.y, 0.5) };
  }
  return fillRectCenterXY(fillRectGeometryFromLabel(label, landscape));
}

/** Legacy snap cell nearest to a fraction (kept on labels for older bake fallbacks). */
function cellFromXY(x: number, y: number, cols: number, rows: number): { row: number; col: number } {
  const c = Math.max(1, cols);
  const r = Math.max(1, rows);
  return {
    col: Math.max(0, Math.min(c - 1, Math.round(x * c - 0.5))),
    row: Math.max(0, Math.min(r - 1, Math.round(y * r - 0.5))),
  };
}

/** Bake canvas size used by Pillow overlays (designer px controls map to this). */
function bakeSizeForKind(kind: MediaKind): { w: number; h: number } {
  return kind === "episode" ? { w: 1920, h: 1080 } : { w: 1000, h: 1500 };
}

function fracToBakePx(frac: number, dim: number): number {
  return Math.round(clampFrac(frac, 0.5) * Math.max(1, dim));
}

function bakePxToFrac(px: number, dim: number): number {
  const d = Math.max(1, dim);
  return clampFrac(px / d, 0);
}

/** Slider + typed bake-pixel field for one axis (stores fraction via onChange). */
function BakePxAxisControl(props: {
  label: string;
  frac: number;
  dim: number;
  /** Inclusive bake-px floor (default 0). */
  minFrac?: number;
  /** Inclusive bake-px ceiling (default 1). */
  maxFrac?: number;
  /** Show snap-to-center (chip placement); off for banner depth. */
  showCenter?: boolean;
  onChange: (frac: number) => void;
  /** Fired while the user is adjusting this control (keeps the preview drag handle visible). */
  onInteract?: () => void;
}) {
  const minPx = Math.round(Math.max(0, props.minFrac ?? 0) * props.dim);
  const maxPx = Math.round(Math.min(1, props.maxFrac ?? 1) * props.dim);
  const px = Math.max(minPx, Math.min(maxPx, fracToBakePx(props.frac, props.dim)));
  const midPx = Math.round(props.dim / 2);
  const atCenter = px === midPx;
  const showCenter = props.showCenter !== false;
  const setPx = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const next = Math.max(minPx, Math.min(maxPx, Math.round(raw)));
    props.onInteract?.();
    props.onChange(bakePxToFrac(next, props.dim));
  };
  return (
    <div
      className="space-y-1 text-[12px] text-slate-400 min-w-0"
      onPointerDown={() => props.onInteract?.()}
      onFocusCapture={() => props.onInteract?.()}
    >
      <div className="flex items-center gap-2">
        <span className="shrink-0">{props.label}</span>
        <div className="ml-auto flex items-center gap-1.5 shrink-0">
          {showCenter ? (
            <button
              type="button"
              title={`Snap ${props.label} to center (${midPx} px)`}
              aria-label={`Center ${props.label}`}
              onClick={() => setPx(midPx)}
              className={`rounded-md border px-1.5 py-0.5 text-[10px] font-headline uppercase tracking-wider ${
                atCenter
                  ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                  : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
              }`}
            >
              Center
            </button>
          ) : null}
          <input
            type="number"
            inputMode="numeric"
            min={minPx}
            max={maxPx}
            step={1}
            value={px}
            aria-label={`${props.label} pixels`}
            onChange={(e) => setPx(Number(e.target.value))}
            className="w-[4.25rem] shrink-0 rounded-lg border border-[#424753]/55 bg-[#0b111b] px-2 py-1 font-mono text-[12px] tabular-nums text-slate-100 outline-none focus:border-[color:var(--brand-accent)]"
          />
        </div>
      </div>
      <input
        type="range"
        min={minPx}
        max={maxPx}
        step={1}
        value={px}
        aria-label={`${props.label} position`}
        onChange={(e) => setPx(Number(e.target.value))}
        className="w-full accent-[var(--brand-accent)]"
      />
    </div>
  );
}

/** Continuous chip/badge size with S/M/L presets (same row pattern as Center on X/Y). */
function SizeScaleControl(props: {
  scale: number;
  min?: number;
  max?: number;
  presets?: Record<string, number>;
  onChange: (scale: number, size: LabelSize) => void;
}) {
  const min = props.min ?? SIZE_SCALE_MIN;
  const max = props.max ?? SIZE_SCALE_MAX;
  const presets: Record<string, number> = { ...SIZE_SCALE_PRESETS, ...(props.presets || {}) };
  const scale = Math.max(min, Math.min(max, props.scale));
  const setScale = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const next = Math.max(min, Math.min(max, Math.round(raw * 100) / 100));
    props.onChange(next, nearestSizePreset(next, presets));
  };
  const applyPreset = (key: LabelSize) => {
    const next = Number(presets[key] ?? SIZE_SCALE_PRESETS[key]);
    if (!Number.isFinite(next)) return;
    props.onChange(Math.max(min, Math.min(max, next)), key);
  };
  return (
    <div className="space-y-1 text-[12px] text-slate-400 min-w-0">
      <div className="flex items-center gap-2">
        <span className="shrink-0">Size</span>
        <div className="ml-auto flex items-center gap-1.5 shrink-0">
          {LABEL_SIZES.map((key) => {
            const presetVal = Number(presets[key] ?? SIZE_SCALE_PRESETS[key]);
            const active = Math.abs(scale - presetVal) < 0.001;
            return (
              <button
                key={key}
                type="button"
                title={`${key} (${presetVal})`}
                aria-label={`Size ${key}`}
                onClick={() => applyPreset(key)}
                className={`rounded-md border px-1.5 py-0.5 text-[10px] font-headline uppercase tracking-wider ${
                  active
                    ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                    : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                }`}
              >
                {key === "small" ? "S" : key === "medium" ? "M" : "L"}
              </button>
            );
          })}
          <input
            type="number"
            inputMode="decimal"
            min={min}
            max={max}
            step={0.05}
            value={Number(scale.toFixed(2))}
            aria-label="Size scale"
            onChange={(e) => setScale(Number(e.target.value))}
            className="w-[4.25rem] shrink-0 rounded-lg border border-[#424753]/55 bg-[#0b111b] px-2 py-1 font-mono text-[12px] tabular-nums text-slate-100 outline-none focus:border-[color:var(--brand-accent)]"
          />
        </div>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={0.05}
        value={scale}
        aria-label="Size scale"
        onChange={(e) => setScale(Number(e.target.value))}
        className="w-full accent-[var(--brand-accent)]"
      />
    </div>
  );
}

/** Lift legacy ``#RRGGBBAA`` into RGB + opacity (alpha 00 meant border off). */
function normalizeLabelColors(label: PosterLayoutLabel, landscape = false): PosterLayoutLabel {
  const border = hexOr(label.border_color, "#000000");
  const size = normalizeLabelSize(label.size, label.token);
  const sizeScale = normalizeSizeScale(label.size_scale, size);
  const style = normalizeLabelStyle(label.style, label.token);
  const arted = Boolean(label.arted) && label.token === "Rating" && style !== "banner";
  let ratingSource = String(label.rating_source || "auto").trim().toLowerCase() || "auto";
  if (arted && (ratingSource === "auto" || !ratingSource)) ratingSource = "imdb";
  const ratingArtLayout = arted ? normalizeRatingArtLayout(label.rating_art_layout) : "left";
  const borderOpacity =
    style === "banner"
      ? 0
      : border.length === 9
        ? typeof label.border_opacity === "number" && !Number.isNaN(label.border_opacity)
          ? label.border_opacity
          : parseInt(border.slice(7, 9), 16) / 255
        : typeof label.border_opacity === "number" && !Number.isNaN(label.border_opacity)
          ? Math.max(0, Math.min(1, label.border_opacity))
          : 0;
  const cols = landscape ? 33 : 25;
  const rows = landscape ? 21 : 37;
  let xStart = 0;
  let xEnd = 1;
  let yStart = 0;
  let yEnd = bannerBaseFrac("top", landscape);
  let thickness = yEnd;
  let xy: { x: number; y: number };
  let token = label.token === "Banner" ? FILL_RECTANGLE_TOKEN : label.token;
  let customTextRaw = label.custom_text;
  // Former single-value placeables → Text with `{Token}`.
  if (LEGACY_SINGLE_TOKEN_CHIPS.has(token)) {
    if (!String(customTextRaw || "").trim()) customTextRaw = `{${token}}`;
    token = "Custom";
  }
  if (style === "banner") {
    token = FILL_RECTANGLE_TOKEN;
    const geom = fillRectGeometryFromLabel(label, landscape);
    xStart = geom.xStart;
    xEnd = geom.xEnd;
    yStart = geom.yStart;
    yEnd = geom.yEnd;
    thickness = Math.max(BANNER_SPAN_MIN_GAP, yEnd - yStart);
    xy = fillRectCenterXY(geom);
  } else {
    xy = normalizeLabelXY(label, cols, rows);
  }
  const cell = cellFromXY(xy.x, xy.y, cols, rows);
  return {
    ...label,
    token,
    x: xy.x,
    y: xy.y,
    row: cell.row,
    col: cell.col,
    border_color: border.length === 9 ? border.slice(0, 7) : solidHex(label.border_color, "#000000"),
    border_opacity: Math.max(0, Math.min(1, borderOpacity)),
    fill_opacity: typeof label.fill_opacity === "number" ? label.fill_opacity : 0.65,
    size: label.size_scale != null ? nearestSizePreset(sizeScale) : size,
    size_scale: sizeScale,
    thickness,
    x_start: xStart,
    x_end: xEnd,
    y_start: yStart,
    y_end: yEnd,
    style,
    arted,
    rating_source: ratingSource,
    rating_art_layout: ratingArtLayout,
    custom_text: normalizeCustomText(customTextRaw, token),
  };
}

export type LayoutPreviewMarker = {
  id: string;
  /** 1-based list index. */
  n: number;
  x: number;
  y: number;
};

/** Invisible tallest editor body (Custom chip). Locks panel height without inserting mid-form gaps. */
function EditorHeightLock(props: {
  kind: MediaKind;
  catalog: PosterLayoutCatalog | null;
  activeKey: string;
  sampleTokens: Record<string, string>;
}) {
  const bake = bakeSizeForKind(props.kind);
  const label = EDITOR_HEIGHT_PROBE;
  const noop = () => {};
  const tokenNames =
    props.catalog?.custom_template_tokens?.[props.activeKey] || [
      "Title",
      "Year",
      "ComingSoonOrNewEp",
      "Rating",
      "Runtime",
      "Sep",
    ];
  return (
    <div className="space-y-3">
      <div className="text-[13px] font-semibold text-slate-200 truncate">Custom</div>
      <SizeScaleControl
        scale={normalizeSizeScale(label.size_scale, label.size)}
        min={props.catalog?.size_scale_min}
        max={props.catalog?.size_scale_max}
        presets={props.catalog?.size_scale_presets}
        onChange={() => {}}
      />
      <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
        <BakePxAxisControl label="X" frac={label.x} dim={bake.w} onChange={noop} />
        <BakePxAxisControl label="Y" frac={label.y} dim={bake.h} onChange={noop} />
      </div>
      <div className="space-y-2 min-w-0">
        <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
          <span>Text</span>
          <input
            type="text"
            tabIndex={-1}
            readOnly
            value={label.custom_text}
            className={`${FIELD_CLASS} font-mono text-[12px]`}
          />
        </label>
        <p className="text-[11px] text-slate-500 leading-snug">
          Use static poster tokens like <span className="font-mono">{"{Title}"}</span> and{" "}
          <span className="font-mono">{"{Sep}"}</span>. Empty values collapse automatically.
        </p>
        <div className="flex flex-wrap gap-1.5">
          {tokenNames.map((name) => (
            <button
              key={name}
              type="button"
              tabIndex={-1}
              className="rounded-md border border-[#424753]/55 px-1.5 py-0.5 font-mono text-[11px] text-slate-400"
            >
              {`{${name}}`}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-slate-500 truncate">
          Preview:{" "}
          <span className="text-slate-300">
            {renderCustomChipText(label.custom_text, props.sampleTokens) || "(empty)"}
          </span>
        </p>
      </div>
      <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
        <ColorField label="Text" value={label.text_color} onChange={noop} />
        <ColorField label="Fill" value={label.fill_color} onChange={noop} />
        <ColorField label="Border" value={label.border_color} onChange={noop} />
        <label className="space-y-1 text-[12px] text-slate-400 min-w-0">
          <span>Fill opacity (65%)</span>
          <input type="range" tabIndex={-1} disabled value={65} className="w-full accent-[var(--brand-accent)]" />
        </label>
        <label className="space-y-1 text-[12px] text-slate-400 min-w-0 sm:col-span-2">
          <span>Border opacity (0%)</span>
          <input type="range" tabIndex={-1} disabled value={0} className="w-full accent-[var(--brand-accent)]" />
        </label>
      </div>
    </div>
  );
}

type Props = {
  accentHex: string;
  compact?: boolean;
  /** Notify parent so the sticky preview can follow the active draft. */
  onPreviewChange?: (state: {
    profileKey: string;
    labels: PosterLayoutLabel[];
    grayscale: boolean;
    baseUrl?: string | null;
    sampleTokens?: Record<string, string> | null;
    /**
     * Sticky-preview movement guides for the selected label.
     * Chips: center crosshairs. Banners: depth + span outline.
     */
    guide?:
      | { kind: "chip"; x: number; y: number }
      | {
          kind: "banner";
          xStart: number;
          xEnd: number;
          yStart: number;
          yEnd: number;
        }
      | null;
    /** Number badges overlaid on the sticky preview while the asset list is hovered. */
    markers?: LayoutPreviewMarker[];
    listHovering?: boolean;
    hoveredId?: string | null;
    /** Selected label id; preview bake draws it last so it sits on top. */
    selectedId?: string | null;
    /** True while X/Y bake-px controls are being adjusted. */
    placementControlsHot?: boolean;
  }) => void;
  /** Mount Preview art controls under the sticky bake preview. */
  onPreviewArtControlsChange?: (node: ReactNode | null) => void;
  /** Mount Unsaved / Discard / Save on the Layout designer legend row. */
  onHeaderActionsChange?: (node: ReactNode | null) => void;
  /** Register a handler so the sticky preview can drag-update chip x/y. */
  onRegisterPlacementDrag?: (handler: ((x: number, y: number) => void) | null) => void;
};

function tokenDisplayLabel(
  token: string,
  tokenMeta: Array<{ name: string; label: string }> = [],
): string {
  if (token === FILL_RECTANGLE_TOKEN || token === "Banner") return FILL_RECTANGLE_LABEL;
  if (token === "Custom") return tokenMeta.find((t) => t.name === token)?.label || "Text";
  return tokenMeta.find((t) => t.name === token)?.label || token;
}

function labelElementName(
  label: PosterLayoutLabel,
  tokenMeta: Array<{ name: string; label: string }>,
): string {
  if (isFillRectangleLabel(label)) return FILL_RECTANGLE_LABEL;
  return tokenDisplayLabel(label.token, tokenMeta);
}

export default function PosterLayoutDesigner(props: Props) {
  const [catalog, setCatalog] = useState<PosterLayoutCatalog | null>(null);
  const [draft, setDraft] = useState<Record<string, PosterLayoutProfile> | null>(null);
  const [kind, setKind] = useState<MediaKind>("movie");
  const [availability, setAvailability] = useState<Availability>("placeholder");
  const [variant, setVariant] = useState<Variant>("default");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [listHovering, setListHovering] = useState(false);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [placementControlsHot, setPlacementControlsHot] = useState(false);
  const placementHotTimerRef = useRef<number | null>(null);
  const bumpPlacementControlsHot = useCallback(() => {
    setPlacementControlsHot(true);
    if (placementHotTimerRef.current != null) window.clearTimeout(placementHotTimerRef.current);
    // Keep "hot" briefly after each tweak so the preview handle does not flicker between slider ticks.
    placementHotTimerRef.current = window.setTimeout(() => {
      placementHotTimerRef.current = null;
      setPlacementControlsHot(false);
    }, 400);
  }, []);
  useEffect(() => {
    return () => {
      if (placementHotTimerRef.current != null) window.clearTimeout(placementHotTimerRef.current);
    };
  }, []);
  const [placeToken, setPlaceToken] = useState<string>("Custom");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applyScope, setApplyScope] = useState<ApplyScope>("next_full_sync");
  const [copyMenuOpen, setCopyMenuOpen] = useState(false);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  /** Single destination, or all other profiles for this availability. */
  const [copyConfirmTarget, setCopyConfirmTarget] = useState<CopyDest | "all" | null>(null);
  const [importConfirmPayload, setImportConfirmPayload] = useState<LayoutSharePayload | null>(null);
  const [dateFormatPicker, setDateFormatPicker] = useState<DateFormatPickerState | null>(null);
  const [textTokensOpen, setTextTokensOpen] = useState(false);
  const copyMenuRef = useRef<HTMLDivElement | null>(null);
  const exportMenuRef = useRef<HTMLDivElement | null>(null);
  const importFileRef = useRef<HTMLInputElement | null>(null);
  const [previewTmdbId, setPreviewTmdbId] = useState(DEFAULT_PREVIEW_ART.movie.tmdbId);
  const [previewBaseUrl, setPreviewBaseUrl] = useState<string | null>(null);
  const [previewArtTitle, setPreviewArtTitle] = useState<string | null>(DEFAULT_PREVIEW_ART.movie.title);
  const [previewArtTokens, setPreviewArtTokens] = useState<Record<string, string> | null>(null);
  const [previewArtBusy, setPreviewArtBusy] = useState(false);
  const [previewArtError, setPreviewArtError] = useState<string | null>(null);

  const activeKey = profileKey(kind, availability, variant);

  const reload = useCallback(async () => {
    const data = await fetchJson<PosterLayoutCatalog>("/api/settings/poster-layouts");
    setCatalog(data);
    setDraft(cloneProfiles(data.profiles || {}));
    setLoadError(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    reload().catch((err) => {
      if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
    });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const labels = draft?.[activeKey]?.labels || [];
  const profileGrayscale = Boolean(draft?.[activeKey]?.grayscale);
  const placeOptions = useMemo(() => {
    const tokens = catalog?.allowed_tokens?.[activeKey] || [];
    const chips = tokens.filter((name) => name !== FILL_RECTANGLE_TOKEN && name !== "Banner");
    return [FILL_RECTANGLE_TOKEN, ...chips];
  }, [activeKey, catalog?.allowed_tokens]);
  const tokenMeta = catalog?.tokens || [];
  const sampleTokens = useMemo(() => {
    const catalogSampleTokens = catalog?.sample_tokens?.[activeKey] || {};
    if (!previewArtTokens || Object.keys(previewArtTokens).length === 0) return catalogSampleTokens;
    return { ...catalogSampleTokens, ...previewArtTokens, Sep: catalogSampleTokens.Sep || "·" };
  }, [activeKey, catalog?.sample_tokens, previewArtTokens]);

  const whenUsed = variantWhenUsed(kind);
  const copyDests = useMemo(
    () => copyDestinations(kind, availability, variant),
    [kind, availability, variant],
  );

  useEffect(() => {
    if (!copyMenuOpen) return;
    const onDoc = (event: MouseEvent) => {
      const root = copyMenuRef.current;
      if (root && event.target instanceof Node && !root.contains(event.target)) {
        setCopyMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [copyMenuOpen]);

  useEffect(() => {
    if (!exportMenuOpen) return;
    const onDoc = (event: MouseEvent) => {
      const root = exportMenuRef.current;
      if (root && event.target instanceof Node && !root.contains(event.target)) {
        setExportMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [exportMenuOpen]);

  const dirty = useMemo(() => {
    if (!catalog || !draft) return false;
    return JSON.stringify(draft) !== JSON.stringify(catalog.profiles);
  }, [catalog, draft]);

  useEffect(() => {
    const first = placeOptions.includes(placeToken) ? placeToken : placeOptions[0] || "Custom";
    if (first !== placeToken) setPlaceToken(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, placeOptions.join("|")]);

  const labelsKey = JSON.stringify(labels);
  const selectedForGuide = labels.find((l) => l.id === selectedId) || null;
  const guide = (() => {
    if (!selectedForGuide) return null;
    if (isFillRectangleLabel(selectedForGuide)) {
      const geom = fillRectGeometryFromLabel(selectedForGuide, kind === "episode");
      return {
        kind: "banner" as const,
        xStart: geom.xStart,
        xEnd: geom.xEnd,
        yStart: geom.yStart,
        yEnd: geom.yEnd,
      };
    }
    return {
      kind: "chip" as const,
      x: clampFrac(selectedForGuide.x, 0.5),
      y: clampFrac(selectedForGuide.y, 0.5),
    };
  })();
  const guideKey = guide
    ? guide.kind === "banner"
      ? `b:${guide.xStart}:${guide.xEnd}:${guide.yStart}:${guide.yEnd}`
      : `c:${guide.x}:${guide.y}`
    : "";
  const markers: LayoutPreviewMarker[] = labels.map((label, index) => {
    const anchor = markerAnchorXY(label, kind === "episode");
    return {
      id: label.id,
      n: index + 1,
      x: anchor.x,
      y: anchor.y,
    };
  });
  const markersKey = markers.map((m) => `${m.id}:${m.n}:${m.x}:${m.y}`).join("|");
  useEffect(() => {
    props.onPreviewChange?.({
      profileKey: activeKey,
      labels: JSON.parse(labelsKey) as PosterLayoutLabel[],
      grayscale: profileGrayscale,
      baseUrl: previewBaseUrl,
      sampleTokens: previewArtTokens,
      guide,
      markers,
      listHovering,
      hoveredId,
      selectedId,
      placementControlsHot,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeKey,
    labelsKey,
    profileGrayscale,
    previewBaseUrl,
    previewArtTokens,
    guideKey,
    selectedId,
    markersKey,
    listHovering,
    hoveredId,
    placementControlsHot,
  ]);

  useEffect(() => {
    if (!props.onRegisterPlacementDrag) return;
    props.onRegisterPlacementDrag((x, y) => {
      setDraft((prev) => {
        if (!prev || !selectedId) return prev;
        const current = prev[activeKey] || { labels: [], grayscale: false };
        const list = current.labels || [];
        const sel = list.find((l) => l.id === selectedId);
        if (!sel) return prev;
        const cols = kind === "episode" ? 33 : 25;
        const rows = kind === "episode" ? 21 : 37;
        const landscape = kind === "episode";
        if (isFillRectangleLabel(sel)) {
          const geom = fillRectGeometryFromLabel(sel, landscape);
          const moved = translateFillRectToCenter(geom, x, y);
          const center = fillRectCenterXY(moved);
          const cell = cellFromXY(center.x, center.y, cols, rows);
          return {
            ...prev,
            [activeKey]: {
              ...current,
              labels: list.map((l) =>
                l.id === selectedId
                  ? {
                      ...l,
                      token: FILL_RECTANGLE_TOKEN,
                      x_start: moved.xStart,
                      x_end: moved.xEnd,
                      y_start: moved.yStart,
                      y_end: moved.yEnd,
                      thickness: Math.max(BANNER_SPAN_MIN_GAP, moved.yEnd - moved.yStart),
                      x: center.x,
                      y: center.y,
                      row: cell.row,
                      col: cell.col,
                    }
                  : l,
              ),
            },
          };
        }
        const nx = clampFrac(x, 0.5);
        const ny = clampFrac(y, 0.5);
        const cell = cellFromXY(nx, ny, cols, rows);
        return {
          ...prev,
          [activeKey]: {
            ...current,
            labels: list.map((l) =>
              l.id === selectedId ? { ...l, x: nx, y: ny, row: cell.row, col: cell.col } : l,
            ),
          },
        };
      });
    });
    return () => props.onRegisterPlacementDrag?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, kind, selectedId, props.onRegisterPlacementDrag]);

  useEffect(() => {
    // Kind change restores the bundled sample id so movie/TV/episode stay appropriate.
    const sample = DEFAULT_PREVIEW_ART[kind];
    setPreviewTmdbId(sample.tmdbId);
    setPreviewBaseUrl(null);
    setPreviewArtTitle(sample.title);
    setPreviewArtTokens(null);
    setPreviewArtError(null);
  }, [kind]);

  const applyPreviewArt = useCallback(async (opts: { tmdbId?: string; random?: boolean }) => {
    setPreviewArtBusy(true);
    setPreviewArtError(null);
    try {
      const params = new URLSearchParams({ kind: previewKind(kind) });
      if (opts.random) params.set("random", "true");
      else if (opts.tmdbId) params.set("tmdb_id", opts.tmdbId);
      const data = await fetchJson<{
        ok?: boolean;
        message?: string;
        tmdb_id?: number | null;
        poster_url?: string;
        title?: string;
        tokens?: Record<string, string>;
      }>(`/api/settings/poster-layouts/preview-art?${params.toString()}`);
      if (!data.ok || !data.poster_url) {
        throw new Error(data.message || "Could not load poster art");
      }
      setPreviewBaseUrl(data.poster_url);
      setPreviewArtTitle(data.title || data.tokens?.Title || null);
      const nextTokens: Record<string, string> = {};
      if (data.tokens && typeof data.tokens === "object") {
        for (const [key, value] of Object.entries(data.tokens)) {
          const text = String(value || "").trim();
          if (key && text) nextTokens[key] = text;
        }
      }
      if (data.title && !nextTokens.Title) nextTokens.Title = data.title;
      setPreviewArtTokens(Object.keys(nextTokens).length ? nextTokens : null);
      setPreviewTmdbId(data.tmdb_id != null ? String(data.tmdb_id) : opts.tmdbId || "");
    } catch (err) {
      setPreviewArtError(err instanceof Error ? err.message : String(err));
    } finally {
      setPreviewArtBusy(false);
    }
  }, [kind]);

  const previewArtControls = useMemo(
    () => (
      <div className="space-y-1.5">
        <label className="text-[12px] uppercase tracking-wider text-slate-500">Preview art</label>
        <div className="flex items-center gap-2">
          <input
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            value={previewTmdbId}
            onChange={(e) => setPreviewTmdbId(e.target.value.replace(/[^\d]/g, ""))}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                const id = previewTmdbId.trim();
                if (id) void applyPreviewArt({ tmdbId: id });
              }
            }}
            placeholder={kind === "episode" ? "TV TMDB ID" : "TMDB ID"}
            className={`${FIELD_CLASS} flex-1`}
            disabled={previewArtBusy}
          />
          <button
            type="button"
            title="Load TMDB art"
            disabled={previewArtBusy || !previewTmdbId.trim()}
            onClick={() => void applyPreviewArt({ tmdbId: previewTmdbId.trim() })}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-[#424753]/55 text-slate-300 hover:border-[#424753] hover:text-slate-100 disabled:opacity-40"
          >
            <span className="material-symbols-outlined leading-none" style={{ fontSize: 22 }}>
              search
            </span>
          </button>
          <button
            type="button"
            title={
              kind === "episode"
                ? "Random episode thumb from library"
                : kind === "tv"
                  ? "Random TV poster from library"
                  : "Random movie poster from library"
            }
            disabled={previewArtBusy}
            onClick={() => void applyPreviewArt({ random: true })}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-[#424753]/55 text-slate-300 hover:border-[#424753] hover:text-slate-100 disabled:opacity-40"
          >
            <span className="material-symbols-outlined leading-none" style={{ fontSize: 22 }}>
              casino
            </span>
          </button>
        </div>
        {previewArtTitle ? (
          <p className="text-[11px] text-slate-500 truncate" title={previewArtTitle}>
            {previewArtTitle}
          </p>
        ) : null}
        {previewArtError ? <p className="text-[11px] text-red-400">{previewArtError}</p> : null}
      </div>
    ),
    [
      applyPreviewArt,
      kind,
      previewArtBusy,
      previewArtError,
      previewArtTitle,
      previewTmdbId,
    ],
  );

  useLayoutEffect(() => {
    props.onPreviewArtControlsChange?.(previewArtControls);
    return () => props.onPreviewArtControlsChange?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewArtControls]);

  const headerActions = useMemo(
    () => (
      <>
        {dirty ? <span className="text-[12px] text-amber-200/90">Unsaved changes</span> : null}
        {saveMsg ? <span className="text-[12px] text-slate-400">{saveMsg}</span> : null}
        {saveError ? <span className="text-[12px] text-red-400">{saveError}</span> : null}
        <button
          type="button"
          disabled={busy || !dirty}
          title="Discard unsaved edits and restore the last saved layout"
          onClick={() => discardUnsavedProfile()}
          className="text-[12px] uppercase tracking-wider text-slate-400 hover:text-slate-200 disabled:opacity-40"
        >
          Discard unsaved
        </button>
        <button
          type="button"
          disabled={!dirty || busy}
          onClick={() => setApplyOpen(true)}
          className={`px-4 py-2 rounded-lg text-[13px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS} disabled:opacity-40`}
          style={accentFilledStyle(props.accentHex)}
        >
          {busy ? "Saving…" : "Save layouts"}
        </button>
      </>
    ),
    // discardUnsavedProfile closes over catalog; dirty/busy/messages drive the UI.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busy, dirty, props.accentHex, saveError, saveMsg, catalog],
  );

  useLayoutEffect(() => {
    props.onHeaderActionsChange?.(headerActions);
    return () => props.onHeaderActionsChange?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [headerActions]);

  const selected = labels.find((l) => l.id === selectedId) || null;

  function updateActiveLabels(next: PosterLayoutLabel[]) {
    setDraft((prev) => {
      if (!prev) return prev;
      const current = prev[activeKey] || { labels: [], grayscale: false };
      return {
        ...prev,
        [activeKey]: { ...current, labels: next },
      };
    });
  }

  function setProfileGrayscale(next: boolean) {
    setDraft((prev) => {
      if (!prev) return prev;
      const current = prev[activeKey] || { labels: [], grayscale: false };
      return {
        ...prev,
        [activeKey]: { ...current, grayscale: next },
      };
    });
  }

  function placeBanner() {
    const yEnd = bannerBaseFrac("top", kind === "episode");
    const next: PosterLayoutLabel = {
      id: newLabelId(),
      token: FILL_RECTANGLE_TOKEN,
      x: 0.5,
      y: yEnd / 2,
      text_color: "#ffffff",
      fill_color: "#000000",
      border_color: "#ffffff",
      border_opacity: 0,
      fill_opacity: 0.65,
      size: "medium",
      size_scale: SIZE_SCALE_PRESETS.medium,
      rating_art_layout: "left",
      thickness: yEnd,
      x_start: 0,
      x_end: 1,
      y_start: 0,
      y_end: yEnd,
      style: "banner",
      arted: false,
      rating_source: "auto",
      custom_text: "",
    };
    updateActiveLabels([...labels, next]);
    setSelectedId(next.id);
  }

  function placeChip() {
    if (
      !placeToken ||
      placeToken === FILL_RECTANGLE_TOKEN ||
      placeToken === "Banner" ||
      !placeOptions.includes(placeToken)
    ) {
      return;
    }
    if (placeToken === "Badge") {
      const badge = labels.find((l) => l.token === "Badge");
      if (badge) {
        updateActiveLabels(
          labels.map((l) => (l.id === badge.id ? { ...l, x: 0.5, y: 0.5 } : l)),
        );
        setSelectedId(badge.id);
        return;
      }
    }
    const chipDepth = bannerBaseFrac("top", kind === "episode");
    const next: PosterLayoutLabel = {
      id: newLabelId(),
      token: placeToken,
      x: 0.5,
      y: 0.5,
      text_color: "#ffffff",
      fill_color: placeToken === "Badge" ? "#0b111b" : "#000000",
      border_color: "#ffffff",
      border_opacity: 0,
      fill_opacity: placeToken === "Badge" ? 1 : 0.65,
      size: placeToken === "Badge" ? "medium" : "small",
      size_scale: placeToken === "Badge" ? SIZE_SCALE_PRESETS.medium : SIZE_SCALE_PRESETS.small,
      rating_art_layout: "left",
      thickness: chipDepth,
      x_start: 0,
      x_end: 1,
      y_start: 0,
      y_end: chipDepth,
      style: "chip",
      arted: placeToken === "Rating",
      rating_source: placeToken === "Rating" ? "imdb" : "auto",
      custom_text: placeToken === "Custom" ? "{Title} {Sep} {Year}" : "",
    };
    updateActiveLabels([...labels, next]);
    setSelectedId(next.id);
  }

  function placeSelectedToken() {
    if (placeToken === FILL_RECTANGLE_TOKEN || placeToken === "Banner") {
      placeBanner();
      return;
    }
    placeChip();
  }

  const canPlaceSelected = Boolean(placeToken && placeOptions.includes(placeToken));

  function patchSelected(patch: Partial<PosterLayoutLabel>) {
    if (!selected) return;
    updateActiveLabels(
      labels.map((l) => {
        if (l.id !== selected.id) return l;
        const next = { ...l, ...patch };
        const cols = kind === "episode" ? 33 : 25;
        const rows = kind === "episode" ? 21 : 37;
        const fillGeomTouched =
          isFillRectangleLabel(next) &&
          (patch.x_start != null ||
            patch.x_end != null ||
            patch.y_start != null ||
            patch.y_end != null);
        if (fillGeomTouched) {
          next.token = FILL_RECTANGLE_TOKEN;
          const geom = fillRectGeometryFromLabel(next, kind === "episode");
          next.x_start = geom.xStart;
          next.x_end = geom.xEnd;
          next.y_start = geom.yStart;
          next.y_end = geom.yEnd;
          next.thickness = Math.max(BANNER_SPAN_MIN_GAP, geom.yEnd - geom.yStart);
          const center = fillRectCenterXY(geom);
          next.x = center.x;
          next.y = center.y;
          const cell = cellFromXY(center.x, center.y, cols, rows);
          next.row = cell.row;
          next.col = cell.col;
        } else if (patch.x != null || patch.y != null) {
          const cell = cellFromXY(clampFrac(next.x, 0.5), clampFrac(next.y, 0.5), cols, rows);
          next.row = cell.row;
          next.col = cell.col;
        }
        return next;
      }),
    );
  }

  function removeLabel(id: string) {
    updateActiveLabels(labels.filter((l) => l.id !== id));
    if (selectedId === id) setSelectedId(null);
  }

  async function persist(scope: ApplyScope) {
    if (!draft) return;
    setBusy(true);
    setSaveError(null);
    setSaveMsg(null);
    try {
      const data = await fetchJson<PosterLayoutCatalog & { changed?: boolean }>(
        "/api/settings/poster-layouts",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            version: catalog?.version,
            profiles: draft,
            apply_scope: scope,
          }),
        },
      );
      setCatalog(data);
      setDraft(cloneProfiles(data.profiles || {}));
      setApplyOpen(false);
      if (data.changed === false) setSaveMsg("No layout changes to apply.");
      else if (scope === "now") setSaveMsg("Saved. Art refresh queued for the library.");
      else if (scope === "next_full_sync") setSaveMsg("Saved. Art refresh scheduled for the next full sync.");
      else setSaveMsg("Saved. Existing art is unchanged until the next refresh.");
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function discardUnsavedProfile() {
    if (!catalog) return;
    setDraft(cloneProfiles(catalog.profiles || {}));
    setSelectedId(null);
    setSaveError(null);
    setSaveMsg("Discarded unsaved edits. Layout restored to the last saved version.");
  }

  function exportLayouts(scope: LayoutShareScope) {
    if (!draft) return;
    const payload = buildLayoutExportPayload({
      scope,
      draft,
      activeKey,
      layoutDocVersion: catalog?.version,
    });
    downloadJsonFile(layoutExportFilename(scope, activeKey), payload);
    setExportMenuOpen(false);
    setSaveError(null);
  }

  function applyImportPayload(payload: LayoutSharePayload) {
    if (!draft) return;
    const updates: Record<string, PosterLayoutProfile> = {};
    let importedCount = 0;
    let focusKey: string | null = null;

    for (const [key, source] of Object.entries(payload.profiles)) {
      const parsed = parseLayoutProfileKey(key);
      if (!parsed) continue;
      const { profile } = buildCopiedProfile(
        {
          labels: (source.labels || []) as PosterLayoutLabel[],
          grayscale: Boolean(source.grayscale),
        },
        key,
        parsed.kind,
      );
      updates[key] = profile;
      importedCount += 1;
      if (!focusKey) focusKey = key;
    }

    if (payload.scope === "single" && payload.profile_key && updates[payload.profile_key]) {
      focusKey = payload.profile_key;
    }

    if (importedCount === 0) {
      setSaveError("Import contained no usable layout profiles.");
      setImportConfirmPayload(null);
      return;
    }

    setDraft((prev) => (prev ? { ...prev, ...updates } : prev));
    if (focusKey) {
      const parsed = parseLayoutProfileKey(focusKey);
      if (parsed) {
        setKind(parsed.kind);
        setAvailability(parsed.availability);
        setVariant(parsed.variant);
      }
    }
    setSelectedId(null);
    setImportConfirmPayload(null);
    setExportMenuOpen(false);
    setCopyMenuOpen(false);
    setSaveError(null);
    setSaveMsg(null);
  }

  function requestImportPayload(payload: LayoutSharePayload) {
    if (!draft) return;
    const needsConfirm = Object.keys(payload.profiles).some(
      (key) => (draft[key]?.labels || []).length > 0,
    );
    if (needsConfirm) {
      setImportConfirmPayload(payload);
      return;
    }
    applyImportPayload(payload);
  }

  async function onImportFileSelected(file: File | null) {
    if (!file) return;
    setSaveError(null);
    try {
      const text = await file.text();
      let parsedJson: unknown;
      try {
        parsedJson = JSON.parse(text);
      } catch {
        throw new Error("Could not parse JSON layout file.");
      }
      const payload = parseLayoutImportPayload(parsedJson);
      requestImportPayload(payload);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      if (importFileRef.current) importFileRef.current.value = "";
    }
  }

  function buildCopiedProfile(
    source: PosterLayoutProfile,
    destKey: string,
    destKind: MediaKind,
  ): { profile: PosterLayoutProfile; dropped: number } {
    const allowed = new Set(catalog?.allowed_tokens?.[destKey] || []);
    const destLandscape = destKind === "episode";
    const sourceLabels = source.labels || [];
    const clonedLabels = sourceLabels
      .filter((label) => {
        const token = label.token === "Banner" ? FILL_RECTANGLE_TOKEN : label.token;
        if (allowed.size === 0) return true;
        return allowed.has(token);
      })
      .map((label) =>
        normalizeLabelColors(
          {
            ...JSON.parse(JSON.stringify(label)),
            id: newLabelId(),
          } as PosterLayoutLabel,
          destLandscape,
        ),
      );
    return {
      profile: {
        labels: clonedLabels,
        grayscale: Boolean(source.grayscale),
      },
      dropped: sourceLabels.length - clonedLabels.length,
    };
  }

  function applyCopyToDest(dest: CopyDest) {
    if (!draft) return;
    const source = draft[activeKey] || { labels: [], grayscale: false };
    const { profile, dropped } = buildCopiedProfile(source, dest.key, dest.kind);
    setDraft((prev) => {
      if (!prev) return prev;
      return { ...prev, [dest.key]: profile };
    });
    setKind(dest.kind);
    setVariant(dest.variant);
    setSelectedId(null);
    setCopyMenuOpen(false);
    setCopyConfirmTarget(null);
    setSaveError(null);
    setSaveMsg(
      dropped > 0
        ? `Copied layout to ${mediaKindLabel(dest.kind)} · ${variantLabel(dest.variant)} (${dropped} incompatible label${dropped === 1 ? "" : "s"} dropped). Save to keep.`
        : `Copied layout to ${mediaKindLabel(dest.kind)} · ${variantLabel(dest.variant)}. Save to keep.`,
    );
  }

  function applyCopyToAll() {
    if (!draft) return;
    const source = draft[activeKey] || { labels: [], grayscale: false };
    const updates: Record<string, PosterLayoutProfile> = {};
    let totalDropped = 0;
    for (const dest of copyDests) {
      const { profile, dropped } = buildCopiedProfile(source, dest.key, dest.kind);
      updates[dest.key] = profile;
      totalDropped += dropped;
    }
    setDraft((prev) => (prev ? { ...prev, ...updates } : prev));
    setSelectedId(null);
    setCopyMenuOpen(false);
    setCopyConfirmTarget(null);
    setSaveError(null);
    setSaveMsg(
      totalDropped > 0
        ? `Copied layout to all other profiles (${totalDropped} incompatible label${totalDropped === 1 ? "" : "s"} dropped across destinations). Save to keep.`
        : "Copied layout to all other profiles. Save to keep.",
    );
  }

  function requestCopyToDest(dest: CopyDest) {
    const destLabels = draft?.[dest.key]?.labels || [];
    if (destLabels.length > 0) {
      setCopyMenuOpen(false);
      setCopyConfirmTarget(dest);
      return;
    }
    applyCopyToDest(dest);
  }

  function requestCopyToAll() {
    const anyOccupied = copyDests.some((dest) => (draft?.[dest.key]?.labels || []).length > 0);
    if (anyOccupied) {
      setCopyMenuOpen(false);
      setCopyConfirmTarget("all");
      return;
    }
    applyCopyToAll();
  }

  function confirmCopyOverwrite() {
    if (copyConfirmTarget === "all") {
      applyCopyToAll();
      return;
    }
    if (copyConfirmTarget) applyCopyToDest(copyConfirmTarget);
  }

  function applyDateFormatChoice(formatId: string) {
    if (!dateFormatPicker || !selected) return;
    const maxLen = catalog?.custom_text_max_length || CUSTOM_TEXT_MAX_LEN;
    const fmt = normalizeDateFormat(formatId);
    const next = applyDateFormatToTemplate(dateFormatPicker.preset.template, fmt).slice(0, maxLen);
    patchSelected({ custom_text: next });
    setDateFormatPicker(null);
  }

  const dateFormatOptions: DateFormatOption[] = catalog?.custom_date_formats?.length
    ? catalog.custom_date_formats
    : DATE_FORMAT_OPTIONS;
  const dateTokenNames = catalog?.custom_date_tokens?.length
    ? new Set(catalog.custom_date_tokens)
    : CUSTOM_DATE_TOKENS;

  if (loadError) {
    return <div className="text-[13px] text-red-400">{loadError}</div>;
  }
  if (!catalog || !draft) {
    return <div className="text-[13px] text-slate-500">Loading layout designer…</div>;
  }

  return (
    <div className="space-y-4 min-w-0 overflow-x-clip">
      <p className="ui-field-description leading-relaxed">
        Design placeholder posters for Movie, TV, and Episode. Each has Released and Coming Soon layouts.
      </p>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {MEDIA_TABS.map((tab) => (
            <button
              key={tab.kind}
              type="button"
              onClick={() => {
                setKind(tab.kind);
                setSelectedId(null);
                setCopyMenuOpen(false);
              }}
              className={`px-3 py-1.5 rounded-lg text-[13px] font-headline uppercase tracking-wider border ${
                kind === tab.kind ? "" : "border-[#424753]/40 text-slate-400 hover:text-slate-200"
              }`}
              style={
                kind === tab.kind
                  ? { borderColor: props.accentHex, color: props.accentHex, backgroundColor: `${props.accentHex}14` }
                  : undefined
              }
            >
              {tab.label}
            </button>
          ))}
          <span className="self-center text-slate-600 px-1">·</span>
          {AVAILABILITY_TABS.map((tab) => {
            const realSoon = tab.availability === "real";
            const active = availability === tab.availability;
            return (
              <button
                key={tab.availability}
                type="button"
                disabled={realSoon}
                title={
                  realSoon
                    ? "Coming soon: overlays for real posters"
                    : undefined
                }
                onClick={() => {
                  if (realSoon) return;
                  setAvailability(tab.availability);
                  setSelectedId(null);
                  setCopyMenuOpen(false);
                }}
                className={`px-3 py-1.5 rounded-lg text-[13px] font-headline uppercase tracking-wider border ${
                  realSoon
                    ? "cursor-not-allowed border-[#424753]/25 text-slate-600 opacity-55"
                    : active
                      ? ""
                      : "border-[#424753]/40 text-slate-400 hover:text-slate-200"
                }`}
                style={
                  !realSoon && active
                    ? { borderColor: props.accentHex, color: props.accentHex, backgroundColor: `${props.accentHex}14` }
                    : undefined
                }
              >
                {tab.label}
              </button>
            );
          })}
          <span className="self-center text-slate-600 px-1">·</span>
          {VARIANT_TABS.map((tab) => (
            <button
              key={tab.variant}
              type="button"
              title={tab.variant === "coming_soon" ? whenUsed.comingSoon : whenUsed.released}
              onClick={() => {
                setVariant(tab.variant);
                setSelectedId(null);
                setCopyMenuOpen(false);
              }}
              className={`px-3 py-1.5 rounded-lg text-[13px] font-headline uppercase tracking-wider border ${
                variant === tab.variant ? "" : "border-[#424753]/40 text-slate-400 hover:text-slate-200"
              }`}
              style={
                variant === tab.variant
                  ? { borderColor: props.accentHex, color: props.accentHex, backgroundColor: `${props.accentHex}14` }
                  : undefined
              }
            >
              {tab.label}
            </button>
          ))}
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <div className="relative" ref={exportMenuRef}>
            <button
              type="button"
              disabled={busy || !draft}
              title="Download layout JSON to share or back up"
              onClick={() => {
                setCopyMenuOpen(false);
                setExportMenuOpen((open) => !open);
              }}
              className="px-3 py-1.5 rounded-lg text-[13px] font-headline uppercase tracking-wider border border-[#424753]/40 text-slate-400 hover:text-slate-200 disabled:opacity-40"
            >
              Export
            </button>
            {exportMenuOpen ? (
              <div className="absolute right-0 top-full z-30 mt-1 min-w-[12rem] rounded-lg border border-[#424753]/40 bg-[#171c22] py-1 shadow-lg shadow-black/30">
                <button
                  type="button"
                  onClick={() => exportLayouts("single")}
                  className="block w-full px-3 py-1.5 text-left text-[13px] text-slate-300 hover:bg-white/5 hover:text-slate-100"
                >
                  Current layout
                </button>
                <button
                  type="button"
                  onClick={() => exportLayouts("all")}
                  className="block w-full px-3 py-1.5 text-left text-[13px] text-slate-300 hover:bg-white/5 hover:text-slate-100"
                >
                  All layouts
                </button>
              </div>
            ) : null}
          </div>
          <button
            type="button"
            disabled={busy || !draft}
            title="Import a Placeholdarr layout JSON file"
            onClick={() => {
              setCopyMenuOpen(false);
              setExportMenuOpen(false);
              importFileRef.current?.click();
            }}
            className="px-3 py-1.5 rounded-lg text-[13px] font-headline uppercase tracking-wider border border-[#424753]/40 text-slate-400 hover:text-slate-200 disabled:opacity-40"
          >
            Import
          </button>
          <input
            ref={importFileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => void onImportFileSelected(e.target.files?.[0] || null)}
          />
          <div className="relative" ref={copyMenuRef}>
            <button
              type="button"
              disabled={busy || availability !== "placeholder"}
              title="Copy this layout's labels and grayscale to another timing or media profile"
              onClick={() => {
                setExportMenuOpen(false);
                setCopyMenuOpen((open) => !open);
              }}
              className="px-3 py-1.5 rounded-lg text-[13px] font-headline uppercase tracking-wider border border-[#424753]/40 text-slate-400 hover:text-slate-200 disabled:opacity-40"
            >
              Copy layout to
            </button>
            {copyMenuOpen ? (
              <div className="absolute right-0 top-full z-30 mt-1 min-w-[14rem] rounded-lg border border-[#424753]/40 bg-[#171c22] py-1 shadow-lg shadow-black/30">
                <button
                  type="button"
                  onClick={() => requestCopyToAll()}
                  className="block w-full px-3 py-1.5 text-left text-[13px] text-slate-200 hover:bg-white/5 hover:text-slate-100"
                >
                  All
                </button>
                <div className="my-1 border-t border-[#424753]/30" />
                {copyDests.map((dest) => (
                  <button
                    key={dest.key}
                    type="button"
                    onClick={() => requestCopyToDest(dest)}
                    className="block w-full px-3 py-1.5 text-left text-[13px] text-slate-300 hover:bg-white/5 hover:text-slate-100"
                  >
                    {dest.label}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <p className="text-center text-[13px] leading-snug text-amber-200/90 -mt-1">{whenUsed.note}</p>

      <div className={`flex flex-col gap-4 min-w-0 ${props.compact ? "" : "xl:flex-row-reverse xl:items-start"}`}>
        <div className="w-full max-w-md shrink-0 space-y-3 xl:w-[17rem]">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-[#424753]/40 px-3 py-2.5">
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-slate-200">Grayscale</div>
              <p className="text-[12px] text-slate-500 leading-snug">
                Applies only to this profile (
                {mediaKindLabel(kind)}{" "}
                {availability === "real" ? "Real" : "Placeholder"}{" "}
                {variantLabel(variant)}
                ).
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={profileGrayscale}
              onClick={() => setProfileGrayscale(!profileGrayscale)}
              className={`relative h-7 w-12 shrink-0 rounded-full border transition-colors ${
                profileGrayscale
                  ? "border-[var(--brand-accent)] bg-[color:color-mix(in_srgb,var(--brand-accent)_35%,transparent)]"
                  : "border-[#424753]/55 bg-[#0b111b]"
              }`}
            >
              <span
                className={`absolute top-0.5 h-5 w-5 rounded-full bg-slate-100 transition-transform ${
                  profileGrayscale ? "left-6" : "left-0.5"
                }`}
              />
            </button>
          </div>
          <div
            className="rounded-lg border border-[#424753]/40 divide-y divide-[#424753]/25 overflow-hidden"
            onMouseEnter={() => setListHovering(true)}
            onMouseLeave={() => {
              setListHovering(false);
              setHoveredId(null);
            }}
          >
            {labels.length === 0 ? (
              <div className="px-3 py-2 text-[13px] text-slate-500">No labels on this profile yet.</div>
            ) : (
              labels.map((label, index) => {
                const active = label.id === selectedId;
                const hovered = label.id === hoveredId;
                const n = index + 1;
                const name = labelElementName(label, tokenMeta);
                return (
                  <div
                    key={label.id}
                    onMouseEnter={() => setHoveredId(label.id)}
                    className={`flex items-center gap-1 px-2 py-1.5 ${
                      active || hovered ? "bg-white/5 text-slate-100" : "text-slate-300 hover:bg-white/[0.03]"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => setSelectedId(label.id)}
                      className="min-w-0 flex-1 text-left text-[13px] flex items-center gap-2 truncate px-1 py-0.5"
                    >
                      <span
                        className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md font-mono text-[11px] ${
                          hovered
                            ? "bg-[color:color-mix(in_srgb,var(--brand-accent)_35%,transparent)] text-slate-100"
                            : "bg-[#0b111b] text-slate-500"
                        }`}
                      >
                        {n}
                      </span>
                      <span className="truncate">{name}</span>
                    </button>
                    <button
                      type="button"
                      title={`Remove ${name}`}
                      aria-label={`Remove ${name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        removeLabel(label.id);
                      }}
                      className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-red-400/20 bg-transparent text-red-400 transition hover:border-red-400/35 hover:bg-red-500/10 hover:text-red-300"
                    >
                      <span className="material-symbols-outlined leading-none" style={{ fontSize: 18 }}>
                        delete
                      </span>
                    </button>
                  </div>
                );
              })
            )}
            <div className="flex items-center gap-2 px-2 py-2 bg-[#0b111b]/40">
              <select
                value={placeToken}
                onChange={(e) => setPlaceToken(e.target.value)}
                aria-label="Add label type"
                className={`${SELECT_CLASS} flex-1 min-w-0`}
              >
                {placeOptions.map((name) => (
                  <option key={name} value={name} className="bg-[#0b111b] text-slate-100">
                    {tokenDisplayLabel(name, tokenMeta)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                title={`Add ${tokenDisplayLabel(placeToken, tokenMeta)}`}
                disabled={!canPlaceSelected}
                onClick={() => placeSelectedToken()}
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-[#424753]/55 text-slate-300 hover:border-[#424753] hover:text-slate-100 disabled:opacity-40"
              >
                <span className="material-symbols-outlined leading-none" style={{ fontSize: 22 }}>
                  add
                </span>
              </button>
            </div>
          </div>
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-3 overflow-hidden">
          {/* Height locked to tallest (Custom) editor; visible controls stay top-aligned. */}
          <div
            className={`grid min-w-0 flex-1 overflow-hidden rounded-lg border ${
              selected ? "border-[#424753]/40" : "border-dashed border-[#424753]/40"
            }`}
          >
            <div
              className="col-start-1 row-start-1 invisible pointer-events-none min-w-0 p-3"
              aria-hidden
              inert
            >
              <EditorHeightLock
                kind={kind}
                catalog={catalog}
                activeKey={activeKey}
                sampleTokens={sampleTokens}
              />
            </div>
            <div className="col-start-1 row-start-1 flex min-h-0 min-w-0 flex-col p-3">
              {selected ? (
                <div className="min-w-0 space-y-3">
                  <div className="text-[13px] font-semibold text-slate-200 truncate">
                    {labelElementName(selected, tokenMeta)}
                  </div>
                  {isFillRectangleLabel(selected) ? (
                    <>
                      {(() => {
                        const geom = fillRectGeometryFromLabel(selected, kind === "episode");
                        const bake = bakeSizeForKind(kind);
                        return (
                          <>
                            <div className="space-y-1 min-w-0">
                              <div className="text-[12px] text-slate-400">Horizontal</div>
                              <BannerSpanSlider
                                start={geom.xStart}
                                end={geom.xEnd}
                                dim={bake.w}
                                accentHex={props.accentHex}
                                onInteract={bumpPlacementControlsHot}
                                onChange={(start, end) =>
                                  patchSelected({ x_start: start, x_end: end })
                                }
                              />
                            </div>
                            <div className="space-y-1 min-w-0">
                              <div className="text-[12px] text-slate-400">Vertical</div>
                              <BannerSpanSlider
                                start={geom.yStart}
                                end={geom.yEnd}
                                dim={bake.h}
                                accentHex={props.accentHex}
                                onInteract={bumpPlacementControlsHot}
                                onChange={(start, end) =>
                                  patchSelected({ y_start: start, y_end: end })
                                }
                              />
                            </div>
                          </>
                        );
                      })()}
                      <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
                        <ColorField
                          label="Fill"
                          value={selected.fill_color}
                          onChange={(v) => patchSelected({ fill_color: solidHex(v, "#000000") })}
                        />
                        <label className="space-y-1 text-[12px] text-slate-400 min-w-0">
                          <span>Fill opacity ({Math.round((selected.fill_opacity ?? 0.65) * 100)}%)</span>
                          <input
                            type="range"
                            min={0}
                            max={100}
                            value={Math.round((selected.fill_opacity ?? 0.65) * 100)}
                            onChange={(e) => patchSelected({ fill_opacity: Number(e.target.value) / 100 })}
                            className="w-full accent-[var(--brand-accent)]"
                          />
                        </label>
                      </div>
                    </>
                  ) : (
                    <>
                      <SizeScaleControl
                        scale={normalizeSizeScale(selected.size_scale, normalizeLabelSize(selected.size, selected.token))}
                        min={catalog?.size_scale_min}
                        max={catalog?.size_scale_max}
                        presets={catalog?.size_scale_presets}
                        onChange={(size_scale, size) => patchSelected({ size_scale, size })}
                      />
                      {(() => {
                        const bake = bakeSizeForKind(kind);
                        return (
                          <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
                            <BakePxAxisControl
                              label="X"
                              frac={selected.x}
                              dim={bake.w}
                              onChange={(x) => patchSelected({ x })}
                              onInteract={bumpPlacementControlsHot}
                            />
                            <BakePxAxisControl
                              label="Y"
                              frac={selected.y}
                              dim={bake.h}
                              onChange={(y) => patchSelected({ y })}
                              onInteract={bumpPlacementControlsHot}
                            />
                          </div>
                        );
                      })()}
                      {selected.token === "Badge" ? (
                        <div className="space-y-2">
                          <p className="text-[12px] text-slate-500">
                            Placeholdarr logo. Dark is yellow on navy; light is blue on yellow.
                          </p>
                          <div className="flex flex-wrap gap-2">
                            {([
                              { id: "dark" as const, label: "Dark", fill: "#0b111b" },
                              { id: "light" as const, label: "Light", fill: "#fbbf24" },
                            ]).map((opt) => {
                              const active = badgeLogoVariant(selected.fill_color) === opt.id;
                              return (
                                <button
                                  key={opt.id}
                                  type="button"
                                  onClick={() => patchSelected({ fill_color: opt.fill, fill_opacity: 1 })}
                                  className={`rounded-lg border px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider ${
                                    active
                                      ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                                      : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                                  }`}
                                >
                                  {opt.label}
                                </button>
                              );
                            })}
                          </div>
                        </div>
                      ) : null}
                      {selected.token === "Custom" ? (
                        <div className="space-y-2 min-w-0">
                          <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
                            <span>Text</span>
                            <div className="flex gap-2 min-w-0">
                              <input
                                type="text"
                                value={selected.custom_text || ""}
                                maxLength={catalog?.custom_text_max_length || CUSTOM_TEXT_MAX_LEN}
                                placeholder="{Title} {Sep} {Year}"
                                onChange={(e) =>
                                  patchSelected({
                                    custom_text: e.target.value
                                      .replace(/[\n\r]/g, "")
                                      .slice(0, catalog?.custom_text_max_length || CUSTOM_TEXT_MAX_LEN),
                                  })
                                }
                                className={`${FIELD_CLASS} font-mono text-[12px] min-w-0 flex-1`}
                              />
                              <button
                                type="button"
                                onClick={() => setTextTokensOpen(true)}
                                className="shrink-0 px-3 py-2 bg-[#252e3a] hover:bg-[#30353b] border border-[#424753]/40 rounded-lg text-[14px] text-slate-300 font-mono transition-colors"
                                title="Insert a token"
                              >
                                {`{ }`}
                              </button>
                            </div>
                          </label>
                          <p
                            className="text-[11px] text-slate-500 truncate"
                            title={renderCustomChipText(selected.custom_text || "", sampleTokens)}
                          >
                            Preview:{" "}
                            <span className="text-slate-300">
                              {renderCustomChipText(selected.custom_text || "", sampleTokens) || "(empty)"}
                            </span>
                          </p>
                          <div className="space-y-1.5">
                            <div className="text-[12px] text-slate-400">Presets</div>
                            <div className="flex flex-wrap gap-1.5">
                              {CUSTOM_TEXT_PRESETS.filter(
                                (preset) => !preset.kinds || preset.kinds.includes(kind),
                              ).map((preset) => {
                                const active = presetMatchesCustomText(
                                  preset.template,
                                  String(selected.custom_text || ""),
                                );
                                return (
                                  <button
                                    key={preset.id}
                                    type="button"
                                    title={preset.template}
                                    onClick={() => {
                                      if (templateHasDateToken(preset.template)) {
                                        setDateFormatPicker({ mode: "preset", preset });
                                        return;
                                      }
                                      patchSelected({
                                        custom_text: preset.template.slice(
                                          0,
                                          catalog?.custom_text_max_length || CUSTOM_TEXT_MAX_LEN,
                                        ),
                                      });
                                    }}
                                    className={`rounded-md border px-1.5 py-0.5 text-[11px] ${
                                      active
                                        ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                                        : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                                    }`}
                                  >
                                    {preset.label}
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      ) : null}
                      {(catalog?.arted_tokens || ["Rating"]).includes(selected.token) ? (
                        <div className="space-y-2">
                          <div className="text-[12px] text-slate-400">Chip look</div>
                          <div className="flex flex-wrap gap-2">
                            {([
                              { id: true, label: "Arted" },
                              { id: false, label: "Text" },
                            ] as const).map((opt) => {
                              const active = Boolean(selected.arted) === opt.id;
                              return (
                                <button
                                  key={String(opt.id)}
                                  type="button"
                                  onClick={() => {
                                    if (opt.id) {
                                      const source =
                                        selected.rating_source && selected.rating_source !== "auto"
                                          ? selected.rating_source
                                          : "imdb";
                                      patchSelected({
                                        arted: true,
                                        rating_source: source,
                                        rating_art_layout: normalizeRatingArtLayout(
                                          selected.rating_art_layout,
                                        ),
                                      });
                                      return;
                                    }
                                    patchSelected({ arted: false, rating_art_layout: "left" });
                                  }}
                                  className={`rounded-lg border px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider ${
                                    active
                                      ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                                      : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                                  }`}
                                >
                                  {opt.label}
                                </button>
                              );
                            })}
                          </div>
                          <label
                            className={`block space-y-1 text-[12px] min-w-0 ${
                              selected.arted
                                ? "text-slate-400"
                                : "pointer-events-none text-slate-600 opacity-40"
                            }`}
                            aria-hidden={!selected.arted}
                          >
                            <span>Rating source</span>
                            <select
                              value={
                                selected.rating_source && selected.rating_source !== "auto"
                                  ? selected.rating_source
                                  : "imdb"
                              }
                              disabled={!selected.arted}
                              tabIndex={selected.arted ? undefined : -1}
                              onChange={(e) => patchSelected({ rating_source: e.target.value })}
                              className={SELECT_CLASS}
                            >
                              {(
                                catalog?.rating_sources || [
                                  "imdb",
                                  "tmdb",
                                  "trakt",
                                  "rottentomatoes",
                                  "metacritic",
                                ]
                              ).map((source) => (
                                <option key={source} value={source} className="bg-[#0b111b] text-slate-100">
                                  {source === "rottentomatoes"
                                    ? "Rotten Tomatoes"
                                    : source === "tmdb"
                                      ? "TMDB"
                                      : source === "imdb"
                                        ? "IMDb"
                                        : source.charAt(0).toUpperCase() + source.slice(1)}
                                </option>
                              ))}
                            </select>
                          </label>
                          <div
                            className={`space-y-2 ${
                              selected.arted
                                ? ""
                                : "pointer-events-none opacity-40"
                            }`}
                            aria-hidden={!selected.arted}
                          >
                            <div className="text-[12px] text-slate-400">Logo placement</div>
                            <div className="flex flex-wrap gap-2">
                              {(
                                (catalog?.rating_art_layouts || RATING_ART_LAYOUTS.map((o) => o.id)).map(
                                  (id) => RATING_ART_LAYOUTS.find((o) => o.id === id) || { id, label: id },
                                )
                              ).map((opt) => {
                                const active =
                                  normalizeRatingArtLayout(selected.rating_art_layout) === opt.id;
                                return (
                                  <button
                                    key={opt.id}
                                    type="button"
                                    disabled={!selected.arted}
                                    tabIndex={selected.arted ? undefined : -1}
                                    onClick={() => patchSelected({ rating_art_layout: opt.id })}
                                    className={`rounded-lg border px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider ${
                                      active
                                        ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                                        : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                                    }`}
                                  >
                                    {opt.label}
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      ) : null}
                      {selected.token !== "Badge" ? (
                        <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
                          <ColorField
                            label="Text"
                            value={selected.text_color}
                            onChange={(v) => patchSelected({ text_color: solidHex(v, "#ffffff") })}
                          />
                          <ColorField
                            label="Fill"
                            value={selected.fill_color}
                            onChange={(v) => patchSelected({ fill_color: solidHex(v, "#000000") })}
                          />
                          <ColorField
                            label="Border"
                            value={selected.border_color}
                            onChange={(v) => {
                              const next = solidHex(v, "#ffffff");
                              const opacity = (selected.border_opacity ?? 0) > 0 ? selected.border_opacity : 1;
                              patchSelected({ border_color: next, border_opacity: opacity });
                            }}
                          />
                          <label className="space-y-1 text-[12px] text-slate-400 min-w-0">
                            <span>
                              {selected.arted &&
                              normalizeRatingArtLayout(selected.rating_art_layout) === "fill"
                                ? "Logo opacity"
                                : "Fill opacity"}{" "}
                              ({Math.round((selected.fill_opacity ?? 0.65) * 100)}%)
                            </span>
                            <input
                              type="range"
                              min={0}
                              max={100}
                              value={Math.round((selected.fill_opacity ?? 0.65) * 100)}
                              onChange={(e) => patchSelected({ fill_opacity: Number(e.target.value) / 100 })}
                              className="w-full accent-[var(--brand-accent)]"
                            />
                          </label>
                          <label className="space-y-1 text-[12px] text-slate-400 min-w-0 sm:col-span-2">
                            <span>Border opacity ({Math.round((selected.border_opacity ?? 0) * 100)}%)</span>
                            <input
                              type="range"
                              min={0}
                              max={100}
                              value={Math.round((selected.border_opacity ?? 0) * 100)}
                              onChange={(e) =>
                                patchSelected({ border_opacity: Number(e.target.value) / 100 })
                              }
                              className="w-full accent-[var(--brand-accent)]"
                            />
                          </label>
                        </div>
                      ) : null}
                    </>
                  )}
                </div>
              ) : (
                <div className="flex flex-1 items-center justify-center px-6 py-10 text-center">
                  <p className="max-w-sm text-[13px] leading-relaxed text-slate-500">
                    Select an element from the list, or add one with the dropdown and + button.
                  </p>
                </div>
              )}
            </div>
          </div>
          <div className="shrink-0 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-[12px] leading-snug text-yellow-100/95">
            <p className="mb-0.5 font-headline text-[10px] font-semibold uppercase tracking-wider text-yellow-200/90">
              Plex tip
            </p>
            <p>
              On Plex, the unwatched count in the top right can cover badge and label text. Consider avoiding this
              area.
            </p>
          </div>
        </div>
      </div>

      {copyConfirmTarget ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) setCopyConfirmTarget(null);
          }}
        >
          <div className="w-full max-w-md rounded-2xl border border-[#424753]/40 bg-[#171c22] overflow-hidden">
            <div className="px-5 py-4 border-b border-[#424753]/30">
              <h3 className="text-[18px] font-bold text-white font-headline">Overwrite layout?</h3>
              <p className="ui-field-description mt-1">
                {copyConfirmTarget === "all" ? (
                  <>
                    One or more destination profiles already have labels. Copying will replace them with this
                    profile&apos;s layout (incompatible chips for that media are dropped, such as &quot;next
                    airing&quot;). Save layouts afterward to keep the change.
                  </>
                ) : (
                  <>
                    {mediaKindLabel(copyConfirmTarget.kind)} · {variantLabel(copyConfirmTarget.variant)} already has
                    labels. Copying will replace them with this profile&apos;s layout (incompatible chips for that
                    media are dropped, such as &quot;next airing&quot;). Save layouts afterward to keep the change.
                  </>
                )}
              </p>
            </div>
            <div className="px-5 py-3 border-t border-[#424753]/30 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setCopyConfirmTarget(null)}
                className="text-[14px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => confirmCopyOverwrite()}
                className={`px-4 py-2 rounded-lg text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
                style={accentFilledStyle(props.accentHex)}
              >
                Overwrite
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {importConfirmPayload ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) setImportConfirmPayload(null);
          }}
        >
          <div className="w-full max-w-md rounded-2xl border border-[#424753]/40 bg-[#171c22] overflow-hidden">
            <div className="px-5 py-4 border-b border-[#424753]/30">
              <h3 className="text-[18px] font-bold text-white font-headline">Overwrite layout?</h3>
              <p className="ui-field-description mt-1">
                {importConfirmPayload.scope === "single" && importConfirmPayload.profile_key ? (
                  <>
                    Importing will replace the existing labels on{" "}
                    {(() => {
                      const parsed = parseLayoutProfileKey(importConfirmPayload.profile_key);
                      if (!parsed) return importConfirmPayload.profile_key;
                      return `${mediaKindLabel(parsed.kind)} · ${variantLabel(parsed.variant)}`;
                    })()}
                    . Incompatible chips for that media are dropped, such as &quot;next airing&quot;. Save layouts
                    afterward to keep the change.
                  </>
                ) : (
                  <>
                    One or more profiles in this file already have labels in your draft. Importing will replace those
                    profiles (other profiles stay unchanged). Incompatible chips for each media are dropped, such as
                    &quot;next airing&quot;. Save layouts afterward to keep the change.
                  </>
                )}
              </p>
            </div>
            <div className="px-5 py-3 border-t border-[#424753]/30 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setImportConfirmPayload(null)}
                className="text-[14px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => applyImportPayload(importConfirmPayload)}
                className={`px-4 py-2 rounded-lg text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
                style={accentFilledStyle(props.accentHex)}
              >
                Overwrite
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {selected?.token === "Custom" ? (
        <PosterTextTokensModal
          open={textTokensOpen}
          value={selected.custom_text || ""}
          maxLength={catalog?.custom_text_max_length || CUSTOM_TEXT_MAX_LEN}
          tokenNames={
            catalog?.custom_template_tokens?.[activeKey] || [
              "Title",
              "Year",
              "ComingSoonOrNewEp",
              "Rating",
              "Runtime",
              "Sep",
            ]
          }
          dateTokenNames={dateTokenNames}
          dateFormatOptions={dateFormatOptions}
          sampleTokens={sampleTokens}
          accentHex={props.accentHex}
          onClose={() => setTextTokensOpen(false)}
          onCommit={(next) => patchSelected({ custom_text: next })}
        />
      ) : null}

      {dateFormatPicker ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) setDateFormatPicker(null);
          }}
        >
          <div className="w-full max-w-md rounded-2xl border border-[#424753]/40 bg-[#171c22] overflow-hidden">
            <div className="px-5 py-4 border-b border-[#424753]/30">
              <h3 className="text-[18px] font-bold text-white font-headline">Date format</h3>
              <p className="ui-field-description mt-1">
                Choose how dates appear in the &quot;{dateFormatPicker.preset.label}&quot; preset. The token is
                written with your choice (for example{" "}
                <span className="font-mono text-slate-300">{"{TheatricalDate:short}"}</span>).
              </p>
            </div>
            <div className="px-5 py-4 space-y-2">
              {dateFormatOptions.map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => applyDateFormatChoice(opt.id)}
                  className="flex w-full items-center justify-between gap-3 rounded-lg border border-[#424753]/55 px-3 py-2.5 text-left hover:border-[#424753] hover:bg-white/[0.03]"
                >
                  <span className="text-[13px] font-headline uppercase tracking-wider text-slate-200">
                    {opt.label}
                  </span>
                  <span className="font-mono text-[12px] text-slate-400">{opt.example}</span>
                </button>
              ))}
            </div>
            <div className="px-5 py-3 border-t border-[#424753]/30 flex justify-end">
              <button
                type="button"
                onClick={() => setDateFormatPicker(null)}
                className="text-[14px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {applyOpen ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
          onClick={(e) => {
            if (e.target === e.currentTarget && !busy) setApplyOpen(false);
          }}
        >
          <div className="w-full max-w-xl rounded-2xl border border-[#424753]/40 bg-[#171c22] overflow-hidden">
            <div className="px-5 py-4 border-b border-[#424753]/30">
              <h3 className="text-[18px] font-bold text-white font-headline">Apply poster layouts</h3>
              <p className="ui-field-description mt-1">
                Saving layouts refreshes placeholder art across the library. Choose when to apply.
              </p>
            </div>
            <div className="px-5 py-4 space-y-3">
              {(
                [
                  {
                    value: "now" as const,
                    title: "Apply now",
                    body: "Queue a full-library art refresh immediately.",
                  },
                  {
                    value: "next_full_sync" as const,
                    title: "Next full sync (recommended)",
                    body: "Apply on the next scheduled or manual full sync.",
                  },
                ] as const
              ).map((opt) => {
                const selectedOpt = applyScope === opt.value;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setApplyScope(opt.value)}
                    className={`w-full text-left px-4 py-3 rounded-lg border transition-colors ${
                      selectedOpt ? "" : "border-[#424753]/40 hover:border-[#5b6473]/60"
                    }`}
                    style={
                      selectedOpt
                        ? { borderColor: props.accentHex, backgroundColor: `${props.accentHex}14` }
                        : undefined
                    }
                  >
                    <div className="text-[15px] font-semibold text-slate-100">{opt.title}</div>
                    <p className="text-[13px] text-slate-400 mt-1">{opt.body}</p>
                  </button>
                );
              })}
            </div>
            <div className="px-5 py-3 border-t border-[#424753]/30 flex justify-end gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={() => setApplyOpen(false)}
                className="text-[14px] font-headline uppercase tracking-wider text-slate-400 hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void persist(applyScope)}
                className={`px-4 py-2 rounded-lg text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS} disabled:opacity-50`}
                style={accentFilledStyle(props.accentHex)}
              >
                {busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ColorField(props: { label: string; value: string; onChange: (v: string) => void }) {
  const solid = solidHex(props.value, "#ffffff");
  return (
    <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
      <span>{props.label}</span>
      <div className="flex items-center gap-2 min-w-0">
        <input
          type="color"
          value={solid}
          onChange={(e) => props.onChange(e.target.value)}
          className="h-8 w-9 shrink-0 rounded border border-[#424753]/55 bg-[#0b111b] p-0.5"
        />
        <input
          type="text"
          value={solid}
          onChange={(e) => props.onChange(solidHex(e.target.value, solid))}
          spellCheck={false}
          className={`${FIELD_CLASS} font-mono text-[12px]`}
        />
      </div>
    </label>
  );
}
