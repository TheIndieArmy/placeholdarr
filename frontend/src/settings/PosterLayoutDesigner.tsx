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

export type LabelSize = "small" | "medium" | "large";
export type LabelStyle = "chip" | "banner";
export type BannerEdge = "top" | "bottom" | "left" | "right";

export type PosterLayoutLabel = {
  id: string;
  token: string;
  /** Horizontal center as a fraction of poster width (0..1). */
  x: number;
  /** Vertical center as a fraction of poster height (0..1). */
  y: number;
  /** Legacy snap cell; kept for migration / banners. */
  row?: number;
  col?: number;
  text_color: string;
  fill_color: string;
  border_color: string;
  fill_opacity: number;
  border_opacity: number;
  size: LabelSize;
  /** Banner strip depth as a fraction of poster height (top/bottom) or width (left/right). */
  thickness: number;
  /** Along-edge coverage start (0..1). Top/bottom: left→right. Left/right: top→bottom. */
  span_start: number;
  /** Along-edge coverage end (0..1). */
  span_end: number;
  style: LabelStyle;
  edge: BannerEdge;
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
  banner_edges?: BannerEdge[];
  arted_tokens?: string[];
  rating_sources?: string[];
  custom_template_tokens?: Record<string, string[]>;
  custom_text_max_length?: number;
};

const LABEL_SIZES: LabelSize[] = ["small", "medium", "large"];
const BANNER_EDGES: BannerEdge[] = ["top", "bottom", "left", "right"];
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
  thickness: 0.12,
  span_start: 0,
  span_end: 1,
  style: "chip",
  edge: "top",
  arted: true,
  rating_source: "imdb",
  custom_text: "{Title} {Sep} {Year}",
};
const BANNER_SIZE_MULT: Record<LabelSize, number> = { small: 0.72, medium: 1, large: 1.35 };
const BANNER_THICKNESS_MIN = 0.04;
const BANNER_THICKNESS_MAX = 0.45;
/** Minimum along-edge width so the strip stays visible; thumbs can nearly meet. */
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

/** One track, two draggable thumbs for banner along-edge coverage. */
function BannerSpanSlider(props: {
  start: number;
  end: number;
  accentHex: string;
  onChange: (start: number, end: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<"start" | "end" | null>(null);
  const spanRef = useRef({ start: props.start, end: props.end });
  const onChangeRef = useRef(props.onChange);
  const span = normalizeBannerSpan(props.start, props.end);
  spanRef.current = span;
  onChangeRef.current = props.onChange;

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
    dragRef.current = which;
    applyDrag(e.clientX);
  };

  return (
    <div
      ref={trackRef}
      className="relative h-7 w-full touch-none select-none"
      onPointerDown={(e) => {
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
            aria-label={which === "start" ? "Banner span start" : "Banner span end"}
            className="absolute top-1/2 z-[2] h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-[#0b111b] shadow-sm"
            style={{ left: `${value * 100}%`, backgroundColor: props.accentHex }}
            onPointerDown={beginThumb(which)}
          />
        );
      })}
    </div>
  );
}
const CUSTOM_TEXT_MAX_LEN = 120;
const CUSTOM_SEP_DEFAULT = "·";

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
  { variant: "default", label: "Default" },
  { variant: "coming_soon", label: "Coming Soon" },
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
  return token === "Badge" || token === "Banner" ? "medium" : "small";
}

function normalizeLabelStyle(raw: unknown, token: string): LabelStyle {
  if (token === "Banner" || String(raw || "").trim().toLowerCase() === "banner") return "banner";
  return "chip";
}

function isBannerLabel(label: Pick<PosterLayoutLabel, "token" | "style">): boolean {
  return normalizeLabelStyle(label.style, label.token) === "banner";
}

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

/** Designer preview / migrate: fraction of board width (side) or height (top/bottom). */
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

function normalizeCustomText(raw: unknown, token: string, maxLen = CUSTOM_TEXT_MAX_LEN): string {
  if (token !== "Custom") return "";
  return String(raw || "")
    .replace(/[\n\r]/g, " ")
    .trim()
    .slice(0, maxLen);
}

/** Fill `{Token}` placeholders from sample/layout values; empty tokens collapse. */
function renderCustomChipText(template: string, tokenMap: Record<string, string>, separator = CUSTOM_SEP_DEFAULT): string {
  const sep = separator || CUSTOM_SEP_DEFAULT;
  const sentinel = "\u001e";
  const filled = String(template || "").replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (_m, name: string) => {
    if (name === "Sep") return sentinel;
    return String(tokenMap[name] ?? "").trim();
  });
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

function edgeAnchorXY(edge: BannerEdge): { x: number; y: number } {
  if (edge === "bottom") return { x: 0.5, y: 1 };
  if (edge === "left") return { x: 0, y: 0.5 };
  if (edge === "right") return { x: 1, y: 0.5 };
  return { x: 0.5, y: 0 };
}

/** Preview number-badge anchor: banner strip center (edge x/y would clip off-canvas). */
function markerAnchorXY(label: PosterLayoutLabel, landscape: boolean): { x: number; y: number } {
  if (!isBannerLabel(label)) {
    return { x: clampFrac(label.x, 0.5), y: clampFrac(label.y, 0.5) };
  }
  const edge = normalizeBannerEdge(label.edge);
  const size = normalizeLabelSize(label.size, label.token);
  const thickness = normalizeBannerThickness(label.thickness, edge, size, landscape);
  const span = normalizeBannerSpan(label.span_start, label.span_end);
  const mid = (span.start + span.end) / 2;
  if (edge === "top") return { x: mid, y: thickness / 2 };
  if (edge === "bottom") return { x: mid, y: 1 - thickness / 2 };
  if (edge === "left") return { x: thickness / 2, y: mid };
  return { x: 1 - thickness / 2, y: mid };
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
  onChange: (frac: number) => void;
  /** Fired while the user is adjusting this control (keeps the preview drag handle visible). */
  onInteract?: () => void;
}) {
  const px = fracToBakePx(props.frac, props.dim);
  const midPx = Math.round(props.dim / 2);
  const atCenter = px === midPx;
  const setPx = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const next = Math.max(0, Math.min(props.dim, Math.round(raw)));
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
          <input
            type="number"
            inputMode="numeric"
            min={0}
            max={props.dim}
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
        min={0}
        max={props.dim}
        step={1}
        value={px}
        aria-label={`${props.label} position`}
        onChange={(e) => setPx(Number(e.target.value))}
        className="w-full accent-[var(--brand-accent)]"
      />
    </div>
  );
}

/** Lift legacy ``#RRGGBBAA`` into RGB + opacity (alpha 00 meant border off). */
function normalizeLabelColors(label: PosterLayoutLabel, landscape = false): PosterLayoutLabel {
  const border = hexOr(label.border_color, "#000000");
  const size = normalizeLabelSize(label.size, label.token);
  const style = normalizeLabelStyle(label.style, label.token);
  const edge = style === "banner" ? normalizeBannerEdge(label.edge) : "top";
  const thickness =
    style === "banner"
      ? normalizeBannerThickness(label.thickness, edge, size, landscape)
      : bannerBaseFrac("top", landscape);
  const span =
    style === "banner"
      ? normalizeBannerSpan(label.span_start, label.span_end)
      : { start: 0, end: 1 };
  const arted = Boolean(label.arted) && label.token === "Rating" && style !== "banner";
  let ratingSource = String(label.rating_source || "auto").trim().toLowerCase() || "auto";
  if (arted && (ratingSource === "auto" || !ratingSource)) ratingSource = "imdb";
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
  const xy =
    style === "banner"
      ? edgeAnchorXY(edge)
      : normalizeLabelXY(label, cols, rows);
  const cell = cellFromXY(xy.x, xy.y, cols, rows);
  return {
    ...label,
    x: xy.x,
    y: xy.y,
    row: cell.row,
    col: cell.col,
    border_color: border.length === 9 ? border.slice(0, 7) : solidHex(label.border_color, "#000000"),
    border_opacity: Math.max(0, Math.min(1, borderOpacity)),
    fill_opacity: typeof label.fill_opacity === "number" ? label.fill_opacity : 0.65,
    size,
    thickness,
    span_start: span.start,
    span_end: span.end,
    style,
    edge,
    arted,
    rating_source: ratingSource,
    custom_text: normalizeCustomText(label.custom_text, label.token),
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
      <div className="space-y-2">
        <div className="text-[12px] text-slate-400">Size</div>
        <div className="flex flex-wrap gap-2">
          {(props.catalog?.sizes?.length ? props.catalog.sizes : LABEL_SIZES).map((opt) => (
            <button
              key={opt}
              type="button"
              tabIndex={-1}
              className="rounded-lg border border-[#424753]/55 px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider text-slate-400"
            >
              {opt}
            </button>
          ))}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
        <BakePxAxisControl label="X" frac={label.x} dim={bake.w} onChange={noop} />
        <BakePxAxisControl label="Y" frac={label.y} dim={bake.h} onChange={noop} />
      </div>
      <div className="space-y-2 min-w-0">
        <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
          <span>Custom text</span>
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
    /** Center guides for the selected chip (null for banners / no selection). */
    guide?: { x: number; y: number } | null;
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
  /** Register a handler so the sticky preview can drag-update chip x/y. */
  onRegisterPlacementDrag?: (handler: ((x: number, y: number) => void) | null) => void;
};

function labelElementName(
  label: PosterLayoutLabel,
  tokenMeta: Array<{ name: string; label: string }>,
): string {
  if (isBannerLabel(label)) return "Banner";
  return tokenMeta.find((t) => t.name === label.token)?.label || label.token;
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
  const [placeToken, setPlaceToken] = useState<string>("PLACEHOLDER");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applyScope, setApplyScope] = useState<ApplyScope>("next_full_sync");
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
    const chips = tokens.filter((name) => name !== "Banner");
    return ["Banner", ...chips];
  }, [activeKey, catalog?.allowed_tokens]);
  const tokenMeta = catalog?.tokens || [];
  const sampleTokens = useMemo(() => {
    const catalogSampleTokens = catalog?.sample_tokens?.[activeKey] || {};
    if (!previewArtTokens || Object.keys(previewArtTokens).length === 0) return catalogSampleTokens;
    return { ...catalogSampleTokens, ...previewArtTokens, Sep: catalogSampleTokens.Sep || "·" };
  }, [activeKey, catalog?.sample_tokens, previewArtTokens]);

  const dirty = useMemo(() => {
    if (!catalog || !draft) return false;
    return JSON.stringify(draft) !== JSON.stringify(catalog.profiles);
  }, [catalog, draft]);

  useEffect(() => {
    const first = placeOptions.includes(placeToken) ? placeToken : placeOptions[0] || "PLACEHOLDER";
    if (first !== placeToken) setPlaceToken(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, placeOptions.join("|")]);

  const labelsKey = JSON.stringify(labels);
  const selectedForGuide = labels.find((l) => l.id === selectedId) || null;
  const guide =
    selectedForGuide && !isBannerLabel(selectedForGuide)
      ? { x: clampFrac(selectedForGuide.x, 0.5), y: clampFrac(selectedForGuide.y, 0.5) }
      : null;
  const guideKey = guide ? `${guide.x}:${guide.y}` : "";
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
        if (!sel || isBannerLabel(sel)) return prev;
        const nx = clampFrac(x, 0.5);
        const ny = clampFrac(y, 0.5);
        const cols = kind === "episode" ? 33 : 25;
        const rows = kind === "episode" ? 21 : 37;
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

  function bannerAtEdge(edge: BannerEdge): PosterLayoutLabel | undefined {
    return labels.find((l) => isBannerLabel(l) && normalizeBannerEdge(l.edge) === edge);
  }

  function placeBanner(preferredEdge?: BannerEdge | null) {
    const edge =
      preferredEdge ||
      BANNER_EDGES.find((e) => !bannerAtEdge(e)) ||
      null;
    if (!edge) return;
    const existingBanner = bannerAtEdge(edge);
    if (existingBanner) {
      setSelectedId(existingBanner.id);
      return;
    }
    const anchor = edgeAnchorXY(edge);
    const next: PosterLayoutLabel = {
      id: newLabelId(),
      token: "Banner",
      x: anchor.x,
      y: anchor.y,
      text_color: "#ffffff",
      fill_color: "#000000",
      border_color: "#ffffff",
      border_opacity: 0,
      fill_opacity: 0.65,
      size: "medium",
      thickness: bannerBaseFrac(edge, kind === "episode"),
      span_start: 0,
      span_end: 1,
      style: "banner",
      edge,
      arted: false,
      rating_source: "auto",
      custom_text: "",
    };
    updateActiveLabels([...labels, next]);
    setSelectedId(next.id);
  }

  function placeChip() {
    if (!placeToken || placeToken === "Banner" || !placeOptions.includes(placeToken)) return;
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
      thickness: bannerBaseFrac("top", kind === "episode"),
      span_start: 0,
      span_end: 1,
      style: "chip",
      edge: "top",
      arted: false,
      rating_source: "auto",
      custom_text: placeToken === "Custom" ? "{Title} {Sep} {Year}" : "",
    };
    updateActiveLabels([...labels, next]);
    setSelectedId(next.id);
  }

  function placeSelectedToken() {
    if (placeToken === "Banner") {
      placeBanner();
      return;
    }
    placeChip();
  }

  const canPlaceSelected =
    placeToken === "Banner"
      ? BANNER_EDGES.some((e) => !bannerAtEdge(e))
      : Boolean(placeToken && placeOptions.includes(placeToken));

  function patchSelected(patch: Partial<PosterLayoutLabel>) {
    if (!selected) return;
    updateActiveLabels(
      labels.map((l) => {
        if (l.id !== selected.id) return l;
        const next = { ...l, ...patch };
        if (patch.x != null || patch.y != null) {
          const cols = kind === "episode" ? 33 : 25;
          const rows = kind === "episode" ? 21 : 37;
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

  if (loadError) {
    return <div className="text-[13px] text-red-400">{loadError}</div>;
  }
  if (!catalog || !draft) {
    return <div className="text-[13px] text-slate-500">Loading layout designer…</div>;
  }

  return (
    <div className="space-y-4 min-w-0">
      <p className="ui-field-description leading-relaxed">
        Design posters as you please. Movie, TV, and Episode each have Default and Coming Soon layouts for placeholders.
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
              onClick={() => {
                setVariant(tab.variant);
                setSelectedId(null);
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
        <div className="ml-auto flex flex-wrap items-center justify-end gap-3">
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
        </div>
      </div>

      <div className={`flex flex-col gap-4 min-w-0 ${props.compact ? "" : "xl:flex-row-reverse xl:items-start"}`}>
        <div className="w-full max-w-md shrink-0 space-y-3 xl:w-[17rem]">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-[#424753]/40 px-3 py-2.5">
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-slate-200">Grayscale</div>
              <p className="text-[12px] text-slate-500 leading-snug">
                Applies only to this profile (
                {kind === "tv" ? "TV" : kind === "episode" ? "Episode" : "Movie"}{" "}
                {availability === "real" ? "Real" : "Placeholder"}{" "}
                {variant === "coming_soon" ? "Coming Soon" : "Default"}
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
                {placeOptions.map((name) => {
                  const meta = tokenMeta.find((t) => t.name === name);
                  return (
                    <option key={name} value={name} className="bg-[#0b111b] text-slate-100">
                      {meta?.label || name}
                    </option>
                  );
                })}
              </select>
              <button
                type="button"
                title={placeToken === "Banner" ? "Add banner" : `Add ${placeToken}`}
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
                    {tokenMeta.find((t) => t.name === selected.token)?.label || selected.token}
                  </div>
                  {isBannerLabel(selected) ? (
                    <>
                      <div className="space-y-2">
                        <div className="text-[12px] text-slate-400">Edge</div>
                        <div className="flex flex-wrap gap-2">
                          {(catalog?.banner_edges?.length ? catalog.banner_edges : BANNER_EDGES).map((opt) => {
                            const active = normalizeBannerEdge(selected.edge) === opt;
                            const taken = Boolean(bannerAtEdge(opt) && bannerAtEdge(opt)?.id !== selected.id);
                            return (
                              <button
                                key={opt}
                                type="button"
                                disabled={taken}
                                onClick={() => {
                                  const anchor = edgeAnchorXY(opt);
                                  patchSelected({
                                    token: "Banner",
                                    edge: opt,
                                    x: anchor.x,
                                    y: anchor.y,
                                    style: "banner",
                                  });
                                }}
                                className={`rounded-lg border px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider disabled:opacity-35 ${
                                  active
                                    ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                                    : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                                }`}
                              >
                                {opt}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                      <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
                        <span>
                          Depth (
                          {Math.round(
                            normalizeBannerThickness(
                              selected.thickness,
                              normalizeBannerEdge(selected.edge),
                              normalizeLabelSize(selected.size, selected.token),
                              kind === "episode",
                            ) * 100,
                          )}
                          %)
                        </span>
                        <input
                          type="range"
                          min={Math.round(BANNER_THICKNESS_MIN * 100)}
                          max={Math.round(BANNER_THICKNESS_MAX * 100)}
                          step={1}
                          value={Math.round(
                            normalizeBannerThickness(
                              selected.thickness,
                              normalizeBannerEdge(selected.edge),
                              normalizeLabelSize(selected.size, selected.token),
                              kind === "episode",
                            ) * 100,
                          )}
                          onChange={(e) =>
                            patchSelected({
                              thickness: Math.max(
                                BANNER_THICKNESS_MIN,
                                Math.min(BANNER_THICKNESS_MAX, Number(e.target.value) / 100),
                              ),
                            })
                          }
                          className="w-full accent-[var(--brand-accent)]"
                        />
                      </label>
                      {(() => {
                        const span = normalizeBannerSpan(selected.span_start, selected.span_end);
                        const startPct = Math.round(span.start * 100);
                        const endPct = Math.round(span.end * 100);
                        return (
                          <div className="space-y-1 min-w-0">
                            <div className="text-[12px] text-slate-400">
                              Along edge ({startPct}% – {endPct}%)
                            </div>
                            <BannerSpanSlider
                              start={span.start}
                              end={span.end}
                              accentHex={props.accentHex}
                              onChange={(start, end) => patchSelected({ span_start: start, span_end: end })}
                            />
                          </div>
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
                      <div className="space-y-2">
                        <div className="text-[12px] text-slate-400">Size</div>
                        <div className="flex flex-wrap gap-2">
                          {(catalog?.sizes?.length ? catalog.sizes : LABEL_SIZES).map((opt) => {
                            const active = normalizeLabelSize(selected.size, selected.token) === opt;
                            return (
                              <button
                                key={opt}
                                type="button"
                                onClick={() => patchSelected({ size: opt })}
                                className={`rounded-lg border px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider ${
                                  active
                                    ? "border-[var(--brand-accent)] text-slate-100 bg-[color:color-mix(in_srgb,var(--brand-accent)_18%,transparent)]"
                                    : "border-[#424753]/55 text-slate-400 hover:border-[#424753] hover:text-slate-200"
                                }`}
                              >
                                {opt}
                              </button>
                            );
                          })}
                        </div>
                      </div>
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
                            <span>Custom text</span>
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
                              className={`${FIELD_CLASS} font-mono text-[12px]`}
                            />
                          </label>
                          <p className="text-[11px] text-slate-500 leading-snug">
                            Use static poster tokens like <span className="font-mono">{"{Title}"}</span> and{" "}
                            <span className="font-mono">{"{Sep}"}</span>. Empty values collapse automatically.
                          </p>
                          <div className="flex flex-wrap gap-1.5">
                            {(
                              catalog?.custom_template_tokens?.[activeKey] || [
                                "Title",
                                "Year",
                                "ComingSoonOrNewEp",
                                "Rating",
                                "Runtime",
                                "Sep",
                              ]
                            ).map((name) => (
                              <button
                                key={name}
                                type="button"
                                title={`Insert {${name}}`}
                                onClick={() => {
                                  const maxLen = catalog?.custom_text_max_length || CUSTOM_TEXT_MAX_LEN;
                                  const insert = `{${name}}`;
                                  const current = String(selected.custom_text || "");
                                  const next = `${current}${current && !current.endsWith(" ") ? " " : ""}${insert}`.slice(
                                    0,
                                    maxLen,
                                  );
                                  patchSelected({ custom_text: next });
                                }}
                                className="rounded-md border border-[#424753]/55 px-1.5 py-0.5 font-mono text-[11px] text-slate-400 hover:border-[#424753] hover:text-slate-200"
                              >
                                {`{${name}}`}
                              </button>
                            ))}
                          </div>
                          <p
                            className="text-[11px] text-slate-500 truncate"
                            title={renderCustomChipText(selected.custom_text || "", sampleTokens)}
                          >
                            Preview:{" "}
                            <span className="text-slate-300">
                              {renderCustomChipText(selected.custom_text || "", sampleTokens) || "(empty)"}
                            </span>
                          </p>
                        </div>
                      ) : null}
                      {(catalog?.arted_tokens || ["Rating"]).includes(selected.token) ? (
                        <div className="space-y-2">
                          <div className="text-[12px] text-slate-400">Chip look</div>
                          <div className="flex flex-wrap gap-2">
                            {([
                              { id: false, label: "Text" },
                              { id: true, label: "Arted" },
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
                                      patchSelected({ arted: true, rating_source: source });
                                      return;
                                    }
                                    patchSelected({ arted: false });
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
              On Plex, the unwatched count in the top right can cover badge and label text. Prefer bottom or left
              placement for important labels.
            </p>
          </div>
        </div>
      </div>

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
