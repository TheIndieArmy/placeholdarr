import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { fetchJson, getCsrfToken } from "../api/client";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import placeholdarrLogoBlue from "../assets/Placeholdarr_blue.svg";
import placeholdarrLogoYellow from "../assets/Placeholdarr_yellow.svg";
import imdbIcon from "../assets/ratings/imdb.svg";
import tmdbIcon from "../assets/ratings/tmdb.svg";
import rottenTomatoesIcon from "../assets/ratings/rottentomatoes.svg";
import metacriticIcon from "../assets/ratings/metacritic.svg";
import traktIcon from "../assets/ratings/trakt.svg";

export type LabelSize = "small" | "medium" | "large";
export type LabelStyle = "chip" | "banner";
export type BannerEdge = "top" | "bottom" | "left" | "right";

export type PosterLayoutLabel = {
  id: string;
  token: string;
  row: number;
  col: number;
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
const SIZE_MULT: Record<LabelSize, number> = { small: 1, medium: 1.5, large: 2 };
const BANNER_SIZE_MULT: Record<LabelSize, number> = { small: 0.72, medium: 1, large: 1.35 };
const BANNER_THICKNESS_MIN = 0.04;
const BANNER_THICKNESS_MAX = 0.45;
const BANNER_SPAN_MIN_GAP = 0.05;

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
const CUSTOM_TEXT_MAX_LEN = 120;
const CUSTOM_SEP_DEFAULT = "·";
const RATING_ICONS: Record<string, string> = {
  imdb: imdbIcon,
  tmdb: tmdbIcon,
  trakt: traktIcon,
  rottentomatoes: rottenTomatoesIcon,
  metacritic: metacriticIcon,
};

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

function hexToRgba(value: string, alpha: number, fallback = "#000000"): string {
  const solid = solidHex(value, fallback).slice(1);
  const r = parseInt(solid.slice(0, 2), 16);
  const g = parseInt(solid.slice(2, 4), 16);
  const b = parseInt(solid.slice(4, 6), 16);
  const a = Math.max(0, Math.min(1, alpha));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
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

function edgeAnchorCell(edge: BannerEdge, cols: number, rows: number): { row: number; col: number } {
  const midC = Math.max(0, Math.floor(cols / 2));
  const midR = Math.max(0, Math.floor(rows / 2));
  if (edge === "bottom") return { row: rows - 1, col: midC };
  if (edge === "left") return { row: midR, col: 0 };
  if (edge === "right") return { row: midR, col: cols - 1 };
  return { row: 0, col: midC };
}

function inferBannerEdge(row: number, col: number, cols: number, rows: number): BannerEdge {
  const dist: Record<BannerEdge, number> = {
    top: row,
    bottom: rows - 1 - row,
    left: col,
    right: cols - 1 - col,
  };
  return (Object.entries(dist) as Array<[BannerEdge, number]>).sort((a, b) => a[1] - b[1])[0][0];
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
  return {
    ...label,
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

/** Match bake-time clamp: keep a content-sized chip fully on the poster. */
function clampBoxToBoard(
  ax: number,
  ay: number,
  boxW: number,
  boxH: number,
  boardW: number,
  boardH: number,
  margin = 4,
): { cx: number; cy: number } {
  let x0 = ax - boxW / 2;
  let y0 = ay - boxH / 2;
  let x1 = x0 + boxW;
  let y1 = y0 + boxH;
  const maxX = Math.max(margin + 1, boardW - margin);
  const maxY = Math.max(margin + 1, boardH - margin);
  if (boxW > maxX - margin) {
    x0 = margin;
    x1 = maxX;
  } else {
    if (x0 < margin) {
      x1 += margin - x0;
      x0 = margin;
    }
    if (x1 > maxX) {
      const shift = x1 - maxX;
      x0 -= shift;
      x1 -= shift;
    }
  }
  if (boxH > maxY - margin) {
    y0 = margin;
    y1 = maxY;
  } else {
    if (y0 < margin) {
      y1 += margin - y0;
      y0 = margin;
    }
    if (y1 > maxY) {
      const shift = y1 - maxY;
      y0 -= shift;
      y1 -= shift;
    }
  }
  return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 };
}

function estimateHandleSizePx(
  token: string,
  sampleText: string,
  boardW: number,
  mediaKind: MediaKind,
  size: LabelSize = "small",
  arted = false,
): { w: number; h: number } {
  // Mirror bake metrics (1000×1500 / 1920×1080) so edge-clamp shifts match the JPEG chip.
  const posterW = mediaKind === "episode" ? 1920 : 1000;
  const posterH = mediaKind === "episode" ? 1080 : 1500;
  const scale = boardW / posterW;
  const mult = SIZE_MULT[size] || 1;
  if (token === "Badge") {
    const frac = mediaKind === "episode" ? 0.18 : 0.11;
    // Medium matches legacy corner badge size.
    const side = Math.max(24, posterH * frac) * scale * (mult / SIZE_MULT.medium);
    return { w: side, h: side };
  }
  const preferred =
    (mediaKind === "episode"
      ? Math.max(28, Math.min(56, posterH * 0.055))
      : Math.max(26, Math.min(52, posterW * 0.048))) * mult;
  const padY = Math.max(8, preferred * 0.32);
  // Mirror bake: arted ratings hug the logo+score cluster.
  const padX = arted && token === "Rating" ? Math.max(5, preferred * 0.16) : Math.max(14, preferred * 0.55);
  const text = String(sampleText || token);
  // Approx glyph width at bake font size, then scale to the designer board.
  let textW = Math.max(preferred * 0.55, text.length * preferred * 0.5);
  const textH = preferred * 0.72;
  if (arted && token === "Rating") {
    const iconH = Math.max(textH, preferred * 0.78);
    const gap = preferred * 0.1;
    textW += iconH + gap;
  }
  const w = Math.min(boardW * 0.9, (textW + padX * 2) * scale);
  const h = (textH + padY * 2) * scale;
  return { w: Math.max(20, w), h: Math.max(16, h) };
}

/** Snap-cell center, then shift so the live chip stays on-poster. */
function clampedHandlePct(args: {
  row: number;
  col: number;
  cols: number;
  rows: number;
  boardW: number;
  boardH: number;
  token: string;
  sampleText: string;
  mediaKind: MediaKind;
  size?: LabelSize;
  arted?: boolean;
}): { leftPct: number; topPct: number } {
  const { row, col, cols, rows, boardW, boardH, token, sampleText, mediaKind, size, arted } = args;
  if (boardW <= 0 || boardH <= 0) {
    return { leftPct: ((col + 0.5) / cols) * 100, topPct: ((row + 0.5) / rows) * 100 };
  }
  const ax = ((col + 0.5) / cols) * boardW;
  const ay = ((row + 0.5) / rows) * boardH;
  const dims = estimateHandleSizePx(token, sampleText, boardW, mediaKind, size || "small", Boolean(arted));
  const { cx, cy } = clampBoxToBoard(ax, ay, dims.w, dims.h, boardW, boardH);
  return { leftPct: (cx / boardW) * 100, topPct: (cy / boardH) * 100 };
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
  }) => void;
};

export default function PosterLayoutDesigner(props: Props) {
  const [catalog, setCatalog] = useState<PosterLayoutCatalog | null>(null);
  const [draft, setDraft] = useState<Record<string, PosterLayoutProfile> | null>(null);
  const [kind, setKind] = useState<MediaKind>("movie");
  const [availability, setAvailability] = useState<Availability>("placeholder");
  const [variant, setVariant] = useState<Variant>("default");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [placeToken, setPlaceToken] = useState<string>("PLACEHOLDER");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applyScope, setApplyScope] = useState<ApplyScope>("next_full_sync");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewTmdbId, setPreviewTmdbId] = useState(DEFAULT_PREVIEW_ART.movie.tmdbId);
  const [previewBaseUrl, setPreviewBaseUrl] = useState<string | null>(null);
  const [previewArtTitle, setPreviewArtTitle] = useState<string | null>(DEFAULT_PREVIEW_ART.movie.title);
  const [previewArtTokens, setPreviewArtTokens] = useState<Record<string, string> | null>(null);
  const [previewArtBusy, setPreviewArtBusy] = useState(false);
  const [previewArtError, setPreviewArtError] = useState<string | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [dropCell, setDropCell] = useState<{ row: number; col: number } | null>(null);
  const [boardSize, setBoardSize] = useState({ w: 0, h: 0 });

  const boardRef = useRef<HTMLDivElement | null>(null);
  const dragMovedRef = useRef(false);
  const previewBlobRef = useRef<string | null>(null);

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

  useEffect(() => {
    return () => {
      if (previewBlobRef.current) URL.revokeObjectURL(previewBlobRef.current);
    };
  }, []);

  useEffect(() => {
    const el = boardRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const apply = () => {
      const rect = el.getBoundingClientRect();
      setBoardSize({ w: rect.width, h: rect.height });
    };
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, [catalog, kind, variant]);

  const grid = catalog?.grids?.[activeKey] || (kind === "episode" ? { cols: 33, rows: 21 } : { cols: 25, rows: 37 });
  const centerCol = Math.floor((grid.cols - 1) / 2);
  const centerRow = Math.floor((grid.rows - 1) / 2);
  const labels = draft?.[activeKey]?.labels || [];
  const profileGrayscale = Boolean(draft?.[activeKey]?.grayscale);
  const allowed = (catalog?.allowed_tokens?.[activeKey] || []).filter((name) => name !== "Banner");
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
    const first = allowed.includes(placeToken) ? placeToken : allowed[0] || "PLACEHOLDER";
    if (first !== placeToken) setPlaceToken(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, allowed.join("|")]);

  const labelsKey = JSON.stringify(labels);
  useEffect(() => {
    props.onPreviewChange?.({
      profileKey: activeKey,
      labels: JSON.parse(labelsKey) as PosterLayoutLabel[],
      grayscale: profileGrayscale,
      baseUrl: previewBaseUrl,
      sampleTokens: previewArtTokens,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey, labelsKey, profileGrayscale, previewBaseUrl, previewArtTokens]);

  useEffect(() => {
    // Kind change restores the bundled sample id so movie/TV/episode stay appropriate.
    const sample = DEFAULT_PREVIEW_ART[kind];
    setPreviewTmdbId(sample.tmdbId);
    setPreviewBaseUrl(null);
    setPreviewArtTitle(sample.title);
    setPreviewArtTokens(null);
    setPreviewArtError(null);
  }, [kind]);

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const csrf = getCsrfToken();
          const response = await fetch("/api/settings/poster-overlay-preview", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              Accept: "image/jpeg",
              "Content-Type": "application/json",
              ...(csrf ? { "X-CSRF-Token": csrf } : {}),
            },
            body: JSON.stringify({
              kind: previewKind(kind),
              grayscale: profileGrayscale,
              profile_key: activeKey,
              // Board art only; interactive HTML chips are the labels you drag.
              labels: [],
              dim_base: true,
              base_url: previewBaseUrl || undefined,
            }),
          });
          if (!response.ok) {
            throw new Error(`Preview failed (${response.status})`);
          }
          const blob = await response.blob();
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          if (previewBlobRef.current) URL.revokeObjectURL(previewBlobRef.current);
          previewBlobRef.current = url;
          setPreviewUrl(url);
          setPreviewError(null);
        } catch (err) {
          if (cancelled) return;
          setPreviewError(err instanceof Error ? err.message : String(err));
        }
      })();
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [activeKey, kind, profileGrayscale, previewBaseUrl]);

  async function applyPreviewArt(opts: { tmdbId?: string; random?: boolean }) {
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
  }

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

  function labelAt(row: number, col: number): PosterLayoutLabel | undefined {
    // Banners use edge anchors only; they should not block chip snap cells.
    return labels.find(
      (l) => normalizeLabelStyle(l.style, l.token) !== "banner" && l.row === row && l.col === col,
    );
  }

  function cellFromPoint(clientX: number, clientY: number): { row: number; col: number } | null {
    const board = boardRef.current;
    if (!board) return null;
    const rect = board.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    // Allow dragging past the poster edge; snap to the nearest in-bounds cell.
    const x = Math.min(Math.max(clientX - rect.left, 0), rect.width - 0.001);
    const y = Math.min(Math.max(clientY - rect.top, 0), rect.height - 0.001);
    let col = Math.min(grid.cols - 1, Math.max(0, Math.floor((x / rect.width) * grid.cols)));
    let row = Math.min(grid.rows - 1, Math.max(0, Math.floor((y / rect.height) * grid.rows)));
    // Magnetic snap to the exact center cell (odd grids put that cell's center at 50%).
    const nx = x / rect.width;
    const ny = y / rect.height;
    const magnetX = 0.5 / grid.cols; // half a cell in normalized units
    const magnetY = 0.5 / grid.rows;
    if (Math.abs(nx - 0.5) <= magnetX) col = centerCol;
    if (Math.abs(ny - 0.5) <= magnetY) row = centerRow;
    return { row, col };
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
    const anchor = edgeAnchorCell(edge, grid.cols, grid.rows);
    const next: PosterLayoutLabel = {
      id: newLabelId(),
      token: "Banner",
      row: anchor.row,
      col: anchor.col,
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

  function placeOrSelect(row: number, col: number) {
    const existing = labelAt(row, col);
    if (existing) {
      setSelectedId(existing.id);
      return;
    }
    if (!placeToken || !allowed.includes(placeToken)) return;
    // Only one badge per profile: empty-cell click relocates it instead of stacking copies.
    if (placeToken === "Badge") {
      const badge = labels.find((l) => l.token === "Badge");
      if (badge) {
        moveLabel(badge.id, row, col);
        setSelectedId(badge.id);
        return;
      }
    }
    const next: PosterLayoutLabel = {
      id: newLabelId(),
      token: placeToken,
      row,
      col,
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

  function moveLabel(id: string, row: number, col: number) {
    const moving = labels.find((l) => l.id === id);
    if (!moving || isBannerLabel(moving)) return;
    const occupant = labelAt(row, col);
    if (occupant && occupant.id !== id) return;
    updateActiveLabels(labels.map((l) => (l.id === id ? { ...l, row, col } : l)));
  }

  function patchSelected(patch: Partial<PosterLayoutLabel>) {
    if (!selected) return;
    updateActiveLabels(labels.map((l) => (l.id === selected.id ? { ...l, ...patch } : l)));
  }

  function removeSelected() {
    if (!selected) return;
    updateActiveLabels(labels.filter((l) => l.id !== selected.id));
    setSelectedId(null);
  }

  function beginDrag(labelId: string, e: ReactPointerEvent<HTMLElement>) {
    const moving = labels.find((l) => l.id === labelId);
    if (moving && isBannerLabel(moving)) {
      // Banners are not grabbable so chips can be placed on top of them.
      setSelectedId(labelId);
      return;
    }
    const cell = cellFromPoint(e.clientX, e.clientY);
    setSelectedId(labelId);
    setDragId(labelId);
    setDropCell(cell);
    dragMovedRef.current = false;
    boardRef.current?.setPointerCapture(e.pointerId);
  }

  function onBoardPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    // Label handles stopPropagation; this only runs for empty board clicks.
    if (e.target !== e.currentTarget && !(e.target as HTMLElement | null)?.dataset?.boardEmpty) {
      return;
    }
    const cell = cellFromPoint(e.clientX, e.clientY);
    if (!cell) return;
    // Never place when a label already owns this snap cell.
    if (labelAt(cell.row, cell.col)) {
      const hit = labelAt(cell.row, cell.col);
      if (hit) beginDrag(hit.id, e);
      return;
    }
    placeOrSelect(cell.row, cell.col);
  }

  function onLabelPointerDown(labelId: string, e: ReactPointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    beginDrag(labelId, e);
  }

  function onBoardPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    if (!dragId) return;
    const cell = cellFromPoint(e.clientX, e.clientY);
    if (!cell) return;
    dragMovedRef.current = true;
    setDropCell(cell);
  }

  function onBoardPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    if (!dragId) return;
    const cell = dropCell || cellFromPoint(e.clientX, e.clientY);
    if (cell && dragMovedRef.current) {
      moveLabel(dragId, cell.row, cell.col);
    }
    setDragId(null);
    setDropCell(null);
    try {
      boardRef.current?.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
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
        Real-file overlays are planned for later (Arr root folders).
      </p>

      <div className="flex flex-wrap gap-2">
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
                  ? "Coming soon: overlays for real posters in Arr root folders"
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

      <div className={`flex flex-col gap-4 min-w-0 ${props.compact ? "" : "xl:flex-row xl:items-start"}`}>
        {/* Fixed portrait window so Movie/TV/Episode tabs do not jump the section height. */}
        <div className="w-[17rem] max-w-full shrink-0 space-y-3">
          <div className="relative w-full aspect-[2/3] rounded-lg overflow-hidden border border-[#424753]/40 bg-[#0b111b]">
            {/* Stage: full-bleed for posters; letterboxed 16:9 for episode thumbs. */}
            <div
              ref={boardRef}
              className={`touch-none select-none ${
                kind === "episode"
                  ? "absolute left-0 right-0 top-1/2 w-full -translate-y-1/2 aspect-video overflow-hidden border-y border-[#424753]/35"
                  : "absolute inset-0"
              } ${dragId ? "cursor-grabbing" : "cursor-crosshair"}`}
              onPointerDown={onBoardPointerDown}
              onPointerMove={onBoardPointerMove}
              onPointerUp={onBoardPointerUp}
              onPointerCancel={onBoardPointerUp}
            >
            {/* Empty-hit layer so clicks on art/grid still place; labels sit above and capture drags. */}
            <div className="absolute inset-0 z-0" data-board-empty="1" />
            {previewUrl ? (
              <img
                src={previewUrl}
                alt={`${activeKey} layout preview`}
                className="absolute inset-0 z-[1] h-full w-full object-cover pointer-events-none"
                draggable={false}
              />
            ) : (
              <div className="absolute inset-0 z-[1] flex items-center justify-center text-[12px] text-slate-500 px-3 text-center pointer-events-none">
                {previewError || "Loading preview…"}
              </div>
            )}
            {(() => {
              const moving = dragId ? labels.find((l) => l.id === dragId) : selected;
              if (!moving || isBannerLabel(moving)) return null;
              const guideCell =
                dragId && dropCell
                  ? dropCell
                  : { row: moving.row, col: moving.col };
              const glowX = guideCell.col === centerCol;
              const glowY = guideCell.row === centerRow;
              return (
                <>
                  <div
                    aria-hidden
                    className="pointer-events-none absolute inset-y-0 z-[4] w-px -translate-x-1/2"
                    style={{
                      left: "50%",
                      backgroundColor: glowX ? props.accentHex : "rgba(148,163,184,0.35)",
                      boxShadow: glowX
                        ? `0 0 0 1px ${props.accentHex}55, 0 0 10px ${props.accentHex}, 0 0 18px ${props.accentHex}88`
                        : undefined,
                    }}
                  />
                  <div
                    aria-hidden
                    className="pointer-events-none absolute inset-x-0 z-[4] h-px -translate-y-1/2"
                    style={{
                      top: "50%",
                      backgroundColor: glowY ? props.accentHex : "rgba(148,163,184,0.35)",
                      boxShadow: glowY
                        ? `0 0 0 1px ${props.accentHex}55, 0 0 10px ${props.accentHex}, 0 0 18px ${props.accentHex}88`
                        : undefined,
                    }}
                  />
                </>
              );
            })()}
            {labels.map((label) => {
              const meta = tokenMeta.find((t) => t.name === label.token);
              const displayName = meta?.label || label.token;
              const sampleText = String(
                label.token === "Custom"
                  ? renderCustomChipText(
                      normalizeCustomText(label.custom_text, "Custom") || "{Title} {Sep} {Year}",
                      sampleTokens,
                    ) || "Custom text"
                  : label.token === "Rating" && label.rating_source && label.rating_source !== "auto"
                    ? sampleTokens[`Rating:${label.rating_source}`] || sampleTokens.Rating || displayName
                    : sampleTokens[label.token] || displayName || label.token,
              );
              const active = label.id === selectedId;
              const dragging = label.id === dragId;
              const labelStyle = normalizeLabelStyle(label.style, label.token);
              const labelSize = normalizeLabelSize(label.size, label.token);
              const showRing = active || dragging;
              const fillOpacity = label.fill_opacity ?? 0.65;

              if (labelStyle === "banner") {
                const edge = normalizeBannerEdge(label.edge);
                const frac = normalizeBannerThickness(
                  label.thickness,
                  edge,
                  labelSize,
                  kind === "episode",
                );
                const span = normalizeBannerSpan(label.span_start, label.span_end);
                const along = (span.end - span.start) * 100;
                const alongStart = span.start * 100;
                const stripStyle: CSSProperties =
                  edge === "top"
                    ? { left: `${alongStart}%`, top: 0, width: `${along}%`, height: `${frac * 100}%` }
                    : edge === "bottom"
                      ? { left: `${alongStart}%`, bottom: 0, width: `${along}%`, height: `${frac * 100}%` }
                      : edge === "left"
                        ? { left: 0, top: `${alongStart}%`, width: `${frac * 100}%`, height: `${along}%` }
                        : { right: 0, top: `${alongStart}%`, width: `${frac * 100}%`, height: `${along}%` };
                return (
                  <div
                    key={label.id}
                    aria-hidden
                    className="absolute z-[5] pointer-events-none"
                    style={{
                      ...stripStyle,
                      backgroundColor: hexToRgba(label.fill_color, fillOpacity, "#000000"),
                      boxShadow: active
                        ? `inset 0 0 0 1px rgba(0,0,0,0.55), inset 0 0 0 3px ${props.accentHex}`
                        : undefined,
                    }}
                    title={`Banner · ${edge} (select from the list to edit)`}
                  />
                );
              }

              const row = dragging && dropCell ? dropCell.row : label.row;
              const col = dragging && dropCell ? dropCell.col : label.col;
              const blocked = Boolean(
                dragging &&
                  dropCell &&
                  labelAt(dropCell.row, dropCell.col) &&
                  labelAt(dropCell.row, dropCell.col)?.id !== label.id,
              );
              const isBadge = label.token === "Badge";
              const arted = Boolean(label.arted) && label.token === "Rating";
              const ratingSource =
                arted
                  ? label.rating_source && label.rating_source !== "auto"
                    ? label.rating_source
                    : "imdb"
                  : label.rating_source && label.rating_source !== "auto"
                    ? label.rating_source
                    : String(sampleTokens.RatingSource || "imdb");
              const ratingIcon = arted ? RATING_ICONS[ratingSource] || RATING_ICONS.imdb : null;
              const size = estimateHandleSizePx(
                label.token,
                sampleText,
                boardSize.w || 272,
                kind,
                labelSize,
                arted,
              );
              const { leftPct, topPct } = clampedHandlePct({
                row,
                col,
                cols: grid.cols,
                rows: grid.rows,
                boardW: boardSize.w,
                boardH: boardSize.h,
                token: label.token,
                sampleText,
                mediaKind: kind,
                size: labelSize,
                arted,
              });
              const borderOpacity = label.border_opacity ?? 0;
              const stroke = Math.max(2, Math.round(size.h * 0.08));
              const badgeVariant = badgeLogoVariant(label.fill_color);
              const fontPx = Math.max(9, Math.round(size.h * 0.42));
              return (
                <div
                  key={label.id}
                  role="button"
                  tabIndex={-1}
                  aria-label={`Move ${displayName}`}
                  className={`absolute z-10 cursor-grab active:cursor-grabbing ${showRing ? "z-20" : ""}`}
                  style={{
                    left: `${leftPct}%`,
                    top: `${topPct}%`,
                    width: size.w,
                    height: size.h,
                    transform: "translate(-50%, -50%)",
                    opacity: blocked ? 0.45 : 1,
                    boxShadow: showRing
                      ? `0 0 0 1px rgba(0,0,0,0.55), 0 0 0 3px ${props.accentHex}`
                      : undefined,
                  }}
                  title={`${displayName} (drag to move)`}
                  onPointerDown={(e) => onLabelPointerDown(label.id, e)}
                >
                  {isBadge ? (
                    <span
                      className="flex h-full w-full items-center justify-center overflow-hidden"
                      style={{
                        borderRadius: "20%",
                        backgroundColor: badgeVariant === "light" ? "#fbbf24" : "#0b111b",
                      }}
                    >
                      <img
                        src={badgeVariant === "light" ? placeholdarrLogoBlue : placeholdarrLogoYellow}
                        alt=""
                        draggable={false}
                        className="pointer-events-none h-[70%] w-[70%] object-contain"
                      />
                    </span>
                  ) : (
                    <span
                      className={`flex h-full w-full items-center justify-center overflow-hidden rounded-md font-headline whitespace-nowrap ${
                        ratingIcon ? "gap-0.5 px-1" : "gap-1 px-1.5"
                      }`}
                      style={{
                        color: solidHex(label.text_color, "#ffffff"),
                        backgroundColor: hexToRgba(label.fill_color, fillOpacity, "#000000"),
                        border:
                          borderOpacity > 0
                            ? `${stroke}px solid ${hexToRgba(label.border_color, borderOpacity, "#ffffff")}`
                            : "none",
                        fontSize: fontPx,
                        lineHeight: 1,
                      }}
                    >
                      {ratingIcon ? (
                        <span
                          className="inline-flex shrink-0 items-center justify-center overflow-hidden rounded-[20%]"
                          style={{
                            height: Math.max(11, Math.round(size.h * 0.58)),
                            width: Math.max(11, Math.round(size.h * 0.58)),
                            backgroundColor: ratingSource === "imdb" ? "#F5C518" : "#1e2430",
                          }}
                        >
                          <img
                            src={ratingIcon}
                            alt=""
                            draggable={false}
                            className="pointer-events-none object-contain"
                            style={{ height: "78%", width: "78%" }}
                          />
                        </span>
                      ) : null}
                      {sampleText}
                    </span>
                  )}
                </div>
              );
            })}
            </div>
          </div>

          <div className="space-y-1.5 px-0.5">
            <label className="text-[12px] uppercase tracking-wider text-slate-500">Preview art</label>
            <div className="flex items-center gap-2 pl-0.5">
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

          <div className="rounded-lg border border-[#424753]/40 divide-y divide-[#424753]/25 overflow-hidden">
            {labels.length === 0 ? (
              <div className="px-3 py-2 text-[13px] text-slate-500">No labels on this profile yet.</div>
            ) : (
              labels.map((label) => {
                const meta = tokenMeta.find((t) => t.name === label.token);
                const active = label.id === selectedId;
                const banner = isBannerLabel(label);
                return (
                  <button
                    key={label.id}
                    type="button"
                    onClick={() => setSelectedId(label.id)}
                    className={`w-full text-left px-3 py-2 text-[13px] flex items-center justify-between gap-2 ${
                      active ? "bg-white/5 text-slate-100" : "text-slate-300 hover:bg-white/[0.03]"
                    }`}
                  >
                    <span className="truncate">
                      {banner
                        ? "Banner"
                        : label.token === "Custom"
                          ? renderCustomChipText(
                              normalizeCustomText(label.custom_text, "Custom") || "{Title} {Sep} {Year}",
                              sampleTokens,
                            ) || "Custom text"
                          : meta?.label || label.token}
                    </span>
                    <span className="text-[11px] text-slate-500 font-mono shrink-0">
                      {banner
                        ? normalizeBannerEdge(label.edge)
                        : `r${label.row + 1}c${label.col + 1}`}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>

        <div className="min-w-0 flex-1 space-y-3 overflow-hidden">
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
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
            <button
              type="button"
              disabled={BANNER_EDGES.every((e) => Boolean(bannerAtEdge(e)))}
              onClick={() => placeBanner()}
              className="rounded-lg border border-[#424753]/55 px-3 py-1.5 text-[12px] font-headline uppercase tracking-wider text-slate-300 hover:border-[#424753] hover:text-slate-100 disabled:opacity-40"
            >
              Add banner
            </button>
            <label className="text-[12px] uppercase tracking-wider text-slate-500 shrink-0">Add chip</label>
            <select
              value={placeToken}
              onChange={(e) => setPlaceToken(e.target.value)}
              className={`${SELECT_CLASS} sm:max-w-[16rem]`}
            >
              {allowed.map((name) => {
                const meta = tokenMeta.find((t) => t.name === name);
                return (
                  <option key={name} value={name} className="bg-[#0b111b] text-slate-100">
                    {meta?.label || name}
                  </option>
                );
              })}
            </select>
          </div>

          {selected ? (
            <div className="space-y-3 rounded-lg border border-[#424753]/40 p-3 min-w-0 overflow-hidden">
              <div className="flex items-center justify-between gap-2 min-w-0">
                <div className="text-[13px] font-semibold text-slate-200 truncate">
                  {tokenMeta.find((t) => t.name === selected.token)?.label || selected.token}
                </div>
                <button
                  type="button"
                  onClick={removeSelected}
                  className="text-[12px] uppercase tracking-wider text-red-300/90 hover:text-red-200 shrink-0"
                >
                  Remove
                </button>
              </div>
              {isBannerLabel(selected) ? (
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
                            const anchor = edgeAnchorCell(opt, grid.cols, grid.rows);
                            patchSelected({
                              token: "Banner",
                              edge: opt,
                              row: anchor.row,
                              col: anchor.col,
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
              ) : null}
              {isBannerLabel(selected) ? (
                <>
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
                    const edge = normalizeBannerEdge(selected.edge);
                    const sideA = edge === "left" || edge === "right" ? "Top" : "Left";
                    const sideB = edge === "left" || edge === "right" ? "Bottom" : "Right";
                    const startPct = Math.round(span.start * 100);
                    const endPct = Math.round(span.end * 100);
                    const gapPct = Math.round(BANNER_SPAN_MIN_GAP * 100);
                    return (
                      <div className="space-y-2 min-w-0">
                        <div className="text-[12px] text-slate-400">
                          Along edge ({startPct}% – {endPct}%)
                        </div>
                        <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
                          <span>
                            {sideA} ({startPct}%)
                          </span>
                          <input
                            type="range"
                            min={0}
                            max={100 - gapPct}
                            step={1}
                            value={startPct}
                            onChange={(e) => {
                              const nextStart = Math.min(
                                Number(e.target.value) / 100,
                                span.end - BANNER_SPAN_MIN_GAP,
                              );
                              patchSelected({
                                span_start: Math.max(0, nextStart),
                                span_end: span.end,
                              });
                            }}
                            className="w-full accent-[var(--brand-accent)]"
                          />
                        </label>
                        <label className="space-y-1 text-[12px] text-slate-400 min-w-0 block">
                          <span>
                            {sideB} ({endPct}%)
                          </span>
                          <input
                            type="range"
                            min={gapPct}
                            max={100}
                            step={1}
                            value={endPct}
                            onChange={(e) => {
                              const nextEnd = Math.max(
                                Number(e.target.value) / 100,
                                span.start + BANNER_SPAN_MIN_GAP,
                              );
                              patchSelected({
                                span_start: span.start,
                                span_end: Math.min(1, nextEnd),
                              });
                            }}
                            className="w-full accent-[var(--brand-accent)]"
                          />
                        </label>
                      </div>
                    );
                  })()}
                </>
              ) : (
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
              )}
              {selected.token === "Badge" ? (
                <div className="space-y-2">
                  <p className="text-[12px] text-slate-500">Placeholdarr logo. Dark is yellow on navy; light is blue on yellow.</p>
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
              ) : isBannerLabel(selected) ? (
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
              ) : (
                <div className="grid grid-cols-1 gap-3 min-w-0 sm:grid-cols-2">
                  {selected.token === "Custom" ? (
                    <div className="space-y-2 min-w-0 sm:col-span-2">
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
                      <p className="text-[11px] text-slate-500 truncate" title={renderCustomChipText(selected.custom_text || "", sampleTokens)}>
                        Preview:{" "}
                        <span className="text-slate-300">
                          {renderCustomChipText(selected.custom_text || "", sampleTokens) || "(empty)"}
                        </span>
                      </p>
                    </div>
                  ) : null}
                  {(catalog?.arted_tokens || ["Rating"]).includes(selected.token) ? (
                    <div className="space-y-2 sm:col-span-2">
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
                      {selected.arted && selected.token === "Rating" ? (
                        <label className="block space-y-1 text-[12px] text-slate-400 min-w-0">
                          <span>Rating source</span>
                          <select
                            value={
                              selected.rating_source && selected.rating_source !== "auto"
                                ? selected.rating_source
                                : "imdb"
                            }
                            onChange={(e) => patchSelected({ rating_source: e.target.value })}
                            className={SELECT_CLASS}
                          >
                            {(catalog?.rating_sources || ["imdb", "tmdb", "trakt", "rottentomatoes", "metacritic"]).map(
                              (source) => (
                                <option key={source} value={source} className="bg-[#0b111b] text-slate-100">
                                  {source === "rottentomatoes"
                                    ? "Rotten Tomatoes"
                                    : source === "tmdb"
                                      ? "TMDB"
                                      : source === "imdb"
                                        ? "IMDb"
                                        : source.charAt(0).toUpperCase() + source.slice(1)}
                                </option>
                              ),
                            )}
                          </select>
                        </label>
                      ) : null}
                    </div>
                  ) : null}
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
                      // Picking a color with opacity at 0 would look like a no-op; turn the stroke on.
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
                      onChange={(e) => patchSelected({ border_opacity: Number(e.target.value) / 100 })}
                      className="w-full accent-[var(--brand-accent)]"
                    />
                  </label>
                </div>
              )}
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-3 pt-1">
            <button
              type="button"
              disabled={!dirty || busy}
              onClick={() => setApplyOpen(true)}
              className={`px-4 py-2 rounded-lg text-[13px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS} disabled:opacity-40`}
              style={accentFilledStyle(props.accentHex)}
            >
              {busy ? "Saving…" : "Save layouts"}
            </button>
            <button
              type="button"
              disabled={busy || !dirty}
              title="Discard unsaved edits and restore the last saved layout"
              onClick={() => discardUnsavedProfile()}
              className="text-[12px] uppercase tracking-wider text-slate-400 hover:text-slate-200 disabled:opacity-40"
            >
              Discard unsaved
            </button>
            {dirty ? <span className="text-[12px] text-amber-200/90">Unsaved changes</span> : null}
            {saveMsg ? <span className="text-[12px] text-slate-400">{saveMsg}</span> : null}
            {previewError ? <span className="text-[12px] text-red-400">{previewError}</span> : null}
            {saveError ? <span className="text-[12px] text-red-400">{saveError}</span> : null}
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
