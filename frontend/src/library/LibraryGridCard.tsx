import type { CSSProperties, ReactNode } from "react";
import type { LibraryItem } from "../types/api";
import type { ThemeMode } from "../brandTypes";
import type { LibraryCardVariant } from "./cardSettings";
import { LIBRARY_CARD_SIZE_DEFAULT } from "./cardSettings";
import { LibraryCardStatusBar, libraryStatusCounts } from "./LibraryCardStatusBar";
import { StackTwoLineTitle, adaptiveTitleSlotHeightPx } from "./StackTwoLineTitle";

export type LibraryCardAccent = { hex: string; icon: string };

export type LibraryGridCardProps = {
  item: LibraryItem;
  variant: LibraryCardVariant;
  posterWidthPx: number;
  themeMode: ThemeMode;
  accent: LibraryCardAccent;
  onClick: () => void;
};

/** Hover scale on the outer button; clip overflow on an inner shell (avoids border clipping). */
function LibraryCardScaleButton(props: {
  onClick: () => void;
  className: string;
  style?: CSSProperties;
  clipClassName?: string;
  children: ReactNode;
}) {
  return (
    <button type="button" onClick={props.onClick} className={props.className} style={props.style}>
      <div
        className={
          props.clipClassName ??
          "flex w-full min-w-0 flex-col overflow-hidden rounded-[10px]"
        }
      >
        {props.children}
      </div>
    </button>
  );
}

function posterImage(item: LibraryItem) {
  if (item.poster_url) {
    return (
      <img
        src={item.poster_url}
        alt=""
        loading="lazy"
        decoding="async"
        className="absolute inset-0 h-full w-full object-cover"
      />
    );
  }
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-slate-800/80 text-slate-500 font-headline text-xs uppercase">
      No art
    </div>
  );
}

function posterShell(
  item: LibraryItem,
  opts: {
    className?: string;
    roundedClass: string;
    children?: ReactNode;
  },
) {
  return (
    <div
      className={`relative w-full overflow-hidden ${opts.roundedClass} ${opts.className ?? ""}`}
      style={{ aspectRatio: "2 / 3" }}
    >
      {posterImage(item)}
      {opts.children}
    </div>
  );
}

/** Poster art only—no overlays (status/title live outside this block). */
function posterArtBlock(item: LibraryItem, className?: string) {
  return (
    <div className={`relative min-h-0 min-w-0 overflow-hidden bg-neutral-900 ${className ?? ""}`}>
      {posterImage(item)}
    </div>
  );
}

function metaBlock(
  item: LibraryItem,
  opts: {
    isLight: boolean;
    yearStyle: CSSProperties;
    titleStyle: CSSProperties;
    className: string;
    titleFirst?: boolean;
  },
) {
  const year = (
    <div className="font-semibold tabular-nums truncate" style={opts.yearStyle}>
      {item.year || "—"}
    </div>
  );
  const title = (
    <div
      className={`font-bold leading-snug line-clamp-2 ${opts.isLight ? "text-slate-900" : "text-white"}`}
      style={opts.titleStyle}
    >
      {item.title}
    </div>
  );
  return (
    <div className={opts.className}>
      {opts.titleFirst ? (
        <>
          {title}
          <div className="mt-0.5">{year}</div>
        </>
      ) : (
        <>
          {year}
          {title}
        </>
      )}
    </div>
  );
}

function revealDetailRows(item: LibraryItem): { label: string; value: string }[] {
  const counts = libraryStatusCounts(item);
  const isSeries = item.type === "series";
  const rows: { label: string; value: string }[] = [
    { label: "Year", value: item.year ? String(item.year) : "—" },
  ];
  if (item.status) rows.push({ label: "Status", value: item.status });
  if (item.determination) rows.push({ label: "Determination", value: item.determination });
  if (item.instance_label) rows.push({ label: "Instance", value: item.instance_label });
  if (isSeries && counts.total > 0) {
    rows.push({
      label: "Episodes",
      value: `${counts.files} real · ${counts.placeholders} ph · ${counts.missing} miss`,
    });
  } else if (!isSeries) {
    const flags = [
      item.has_file ? "file" : null,
      item.has_placeholder ? "placeholder" : null,
      item.has_missing ? "missing" : null,
      item.is_future ? "future" : null,
    ].filter(Boolean);
    if (flags.length) rows.push({ label: "Library", value: flags.join(", ") });
  }
  return rows;
}

function revealBackPanel(
  item: LibraryItem,
  opts: { isLight: boolean; accent: LibraryCardAccent; scale: number },
) {
  const labelSize = `clamp(8px, calc(9px * ${opts.scale}), 11px)`;
  const valueSize = `clamp(9px, calc(10px * ${opts.scale}), 12px)`;
  const rows = revealDetailRows(item);

  return (
    <div
      className={`flex h-full min-h-0 flex-col overflow-hidden ${
        opts.isLight ? "bg-slate-50 text-slate-800" : "bg-[#141820] text-slate-200"
      }`}
    >
      <div className="min-h-0 flex-1 overflow-y-auto px-[calc(8px*var(--library-card-scale))] pt-[calc(8px*var(--library-card-scale))] pb-[calc(4px*var(--library-card-scale))]">
        <div className="space-y-[calc(4px*var(--library-card-scale))]">
          {rows.map((row) => (
            <div key={row.label} className="min-w-0">
              <div
                className="font-headline uppercase tracking-wider opacity-70"
                style={{ fontSize: labelSize, color: opts.accent.icon }}
              >
                {row.label}
              </div>
              <div className="truncate font-medium leading-snug" style={{ fontSize: valueSize }} title={row.value}>
                {row.value}
              </div>
            </div>
          ))}
        </div>
        {item.overview ? (
          <p
            className={`mt-[calc(6px*var(--library-card-scale))] line-clamp-4 leading-snug ${
              opts.isLight ? "text-slate-600" : "text-slate-400"
            }`}
            style={{ fontSize: valueSize }}
          >
            {item.overview}
          </p>
        ) : null}
      </div>
    </div>
  );
}

const THEME_NAVY = "#1e2430";
const META_GREY_LIGHT = "#64748b";
const META_GREY_DARK = "#94a3b8";

/** Stack (`ticket`) uses navy chrome in light mode; accent yellow in dark. */
function stackThemeColor(isLight: boolean, accent: LibraryCardAccent): string {
  return isLight ? THEME_NAVY : accent.hex;
}

function seriesNetworkLabel(item: LibraryItem): string | null {
  if (item.type !== "series") return null;
  const text = String(item.network ?? "").trim();
  return text || null;
}

function framedMetaBlock(
  item: LibraryItem,
  opts: {
    isLight: boolean;
    accent: LibraryCardAccent;
    titleStyle: CSSProperties;
  },
) {
  const yearColor = opts.isLight ? "var(--brand-accent-tertiary, #34daff)" : opts.accent.hex;
  const metaGrey = opts.isLight ? META_GREY_LIGHT : META_GREY_DARK;
  const metaSize = `clamp(10px, calc(11px * var(--library-card-scale)), 13px)`;
  const network = seriesNetworkLabel(item);
  const showNetwork = item.type === "series";

  return (
    <div className="min-w-0 text-center">
      <div
        className={`font-bold leading-tight line-clamp-2 min-h-[2.6em] ${opts.isLight ? "text-slate-900" : "text-white"}`}
        style={opts.titleStyle}
      >
        {item.title}
      </div>
      <div
        className="mt-0.5 flex min-h-[1.25em] items-baseline justify-between gap-2"
        style={{ fontSize: metaSize }}
      >
        <span className="shrink-0 font-semibold tabular-nums text-left" style={{ color: yearColor }}>
          {item.year || "—"}
        </span>
        {showNetwork ? (
          <span
            className={`min-w-0 flex-1 truncate text-right font-medium ${network ? "" : "opacity-0"}`}
            style={{ color: metaGrey }}
            title={network ?? undefined}
            aria-hidden={!network}
          >
            {network || "\u00a0"}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/** Title → poster → metadata footer (vertical reading order). */
function stackCard(
  item: LibraryItem,
  opts: {
    accent: LibraryCardAccent;
    isLight: boolean;
    cardStyle: CSSProperties;
    onClick: () => void;
    statusBar: ReactNode;
    scale: number;
  },
) {
  const metaSize = `clamp(9px, calc(10px * var(--library-card-scale)), 12px)`;
  const details: string[] = [];
  if (item.year) details.push(String(item.year));
  details.push(item.type === "series" ? "Series" : "Film");
  if (item.instance_label) details.push(item.instance_label);

  const stackColor = stackThemeColor(opts.isLight, opts.accent);

  return (
    <LibraryCardScaleButton
      onClick={opts.onClick}
      className={`group relative flex w-full flex-col rounded-xl border-2 text-left cursor-pointer transition-transform hover:scale-[1.02] hover:z-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 overflow-visible ${
        opts.isLight ? "bg-white shadow-md shadow-slate-900/8" : "bg-[#1e2430]"
      }`}
      style={{ ...opts.cardStyle, borderColor: stackColor }}
      clipClassName="flex w-full min-w-0 flex-col overflow-hidden rounded-[10px]"
    >
      <div
        className={`shrink-0 min-w-0 overflow-hidden px-[calc(10px*var(--library-card-scale))] pt-[calc(10px*var(--library-card-scale))] pb-[calc(6px*var(--library-card-scale))] ${
          opts.isLight ? "bg-[#1e2430]" : "bg-[#141a24]"
        }`}
      >
        <StackTwoLineTitle title={item.title} color={stackColor} scale={opts.scale} />
      </div>

      <div
        className="relative w-full shrink-0 overflow-hidden bg-neutral-900"
        style={{ aspectRatio: "2 / 3" }}
      >
        {posterImage(item)}
      </div>

      <div
        className={`shrink-0 ${opts.isLight ? "bg-slate-50/95" : "bg-[#141a24]"}`}
      >
        {opts.statusBar}
      </div>

      <div
        className={`shrink-0 border-t px-[calc(10px*var(--library-card-scale))] py-[calc(8px*var(--library-card-scale))] ${
          opts.isLight ? "border-slate-200/90 bg-slate-50" : "border-[#424753]/40 bg-[#141a24]"
        }`}
      >
        <p
          className={`font-headline uppercase tracking-wider leading-snug line-clamp-2 ${
            opts.isLight ? "text-slate-600" : "text-slate-400"
          }`}
          style={{ fontSize: metaSize }}
        >
          {details.join(" · ")}
        </p>
      </div>
    </LibraryCardScaleButton>
  );
}

function revealFrontFace(
  item: LibraryItem,
  opts: {
    isLight: boolean;
    yearStyle: CSSProperties;
    titleStyle: CSSProperties;
    statusBar: ReactNode;
  },
) {
  return (
    <>
      {posterShell(item, {
        className: "w-full shrink-0",
        roundedClass: "rounded-t-[10px]",
        children: <div className="absolute inset-x-0 bottom-0 z-10">{opts.statusBar}</div>,
      })}
      <div className={`shrink-0 ${opts.isLight ? "bg-slate-50/95" : "bg-[#171c22]"}`}>
        <div className="px-[calc(8px*var(--library-card-scale))] py-[calc(6px*var(--library-card-scale))]">
          {metaBlock(item, {
            isLight: opts.isLight,
            yearStyle: opts.yearStyle,
            titleStyle: opts.titleStyle,
            className: "min-w-0",
            titleFirst: true,
          })}
        </div>
      </div>
    </>
  );
}

export function LibraryGridCard(props: LibraryGridCardProps) {
  const accent = props.accent;
  const isLight = props.themeMode === "light";
  const scale = props.posterWidthPx / LIBRARY_CARD_SIZE_DEFAULT;

  const cardStyle = {
    "--library-card-w": `${props.posterWidthPx}px`,
    "--library-card-scale": String(scale),
    width: "100%",
    borderColor: accent.hex,
  } as CSSProperties;

  const yearStyle: CSSProperties = {
    color: isLight ? META_GREY_LIGHT : META_GREY_DARK,
    fontSize: `clamp(10px, calc(11px * var(--library-card-scale)), 14px)`,
  };
  const titleStyle: CSSProperties = {
    fontSize: `clamp(11px, calc(13px * var(--library-card-scale)), 17px)`,
  };

  const shellClass = `group relative flex w-full flex-col text-left cursor-pointer transition-transform hover:scale-[1.02] hover:z-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 rounded-xl overflow-visible ${
    isLight ? "bg-white shadow-md shadow-slate-900/8" : "bg-[#1e2430]"
  }`;

  const statusBar = (flush: boolean) => (
    <LibraryCardStatusBar item={props.item} isLight={isLight} flush={flush} className="w-full" />
  );

  if (props.variant === "reveal") {
    const flipInner =
      "relative w-full transition-[transform] duration-500 ease-out [transform-style:preserve-3d] motion-reduce:transition-none group-hover:[transform:rotateY(180deg)] group-focus-visible:[transform:rotateY(180deg)] motion-reduce:group-hover:[transform:none] motion-reduce:group-focus-visible:[transform:none]";
    const faceBack =
      "absolute inset-0 flex flex-col overflow-hidden [backface-visibility:hidden] [transform:rotateY(180deg)] motion-reduce:[backface-visibility:visible] motion-reduce:opacity-0 motion-reduce:pointer-events-none motion-reduce:group-hover:opacity-100 motion-reduce:group-hover:pointer-events-auto motion-reduce:group-focus-visible:opacity-100";
    const faceFront =
      "flex flex-col [backface-visibility:hidden] motion-reduce:[backface-visibility:visible] motion-reduce:group-hover:opacity-0 motion-reduce:group-focus-visible:opacity-0";

    return (
      <LibraryCardScaleButton
        onClick={props.onClick}
        className={`${shellClass} border-2 [perspective:1000px] motion-reduce:[perspective:none]`}
        style={cardStyle}
        clipClassName="relative w-full min-h-0 overflow-hidden rounded-[10px]"
      >
        <div className={flipInner}>
          <div className={faceFront}>
            {revealFrontFace(props.item, {
              isLight,
              yearStyle,
              titleStyle,
              statusBar: statusBar(true),
            })}
          </div>
          <div className={faceBack}>
            {revealBackPanel(props.item, { isLight, accent, scale })}
            <div className="shrink-0">{statusBar(true)}</div>
          </div>
        </div>
      </LibraryCardScaleButton>
    );
  }

  if (props.variant === "framed") {
    return (
      <LibraryCardScaleButton
        onClick={props.onClick}
        className={`${shellClass} border-2 p-[calc(6px*var(--library-card-scale))]`}
        style={cardStyle}
      >
        {posterShell(props.item, {
          className: "w-full ring-1 ring-inset " + (isLight ? "ring-slate-200/90" : "ring-white/10"),
          roundedClass: "rounded-lg",
        })}
        <div className="mt-[calc(6px*var(--library-card-scale))] space-y-[calc(6px*var(--library-card-scale))] px-[calc(2px*var(--library-card-scale))] pb-[calc(4px*var(--library-card-scale))]">
          {statusBar(false)}
          {framedMetaBlock(props.item, { isLight, accent, titleStyle })}
        </div>
      </LibraryCardScaleButton>
    );
  }

  if (props.variant === "stacked") {
    /* Polaroid: white frame, shadow, caption — no accent border or vertical stripe */
    const captionStyle: CSSProperties = {
      fontSize: `clamp(11px, calc(13px * var(--library-card-scale)), 16px)`,
    };
    const polaroidYearStyle: CSSProperties = {
      fontSize: `clamp(9px, calc(10px * var(--library-card-scale)), 12px)`,
      color: isLight ? "#64748b" : "#94a3b8",
    };
    const polaroidFrameBg = isLight
      ? "var(--brand-chrome-main, #eef3f8)"
      : "color-mix(in srgb, var(--brand-accent-tertiary, #34daff) 14%, var(--brand-surface-elevated, #1e2430))";

    return (
      <button
        type="button"
        onClick={props.onClick}
        className={`group w-full cursor-pointer text-left transition-[transform,box-shadow] duration-300 hover:-rotate-1 hover:shadow-2xl focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 motion-reduce:hover:rotate-0 shadow-[0_8px_24px_rgba(15,23,42,0.14),0_2px_6px_rgba(15,23,42,0.08)] dark:shadow-[0_12px_32px_rgba(0,0,0,0.45)]`}
        style={{
          ...cardStyle,
          padding: `calc(10px * var(--library-card-scale)) calc(10px * var(--library-card-scale)) calc(18px * var(--library-card-scale))`,
          borderColor: "transparent",
          backgroundColor: polaroidFrameBg,
        }}
      >
        <div
          className="overflow-hidden bg-neutral-200/80 dark:bg-neutral-700/50"
          style={{ boxShadow: "inset 0 0 0 1px rgba(0,0,0,0.06)" }}
        >
          {posterShell(props.item, {
            className: "w-full",
            roundedClass: "rounded-none",
            children: <div className="absolute inset-x-0 bottom-0 z-10">{statusBar(true)}</div>,
          })}
        </div>
        <div className="mt-[calc(10px*var(--library-card-scale))] px-[calc(2px*var(--library-card-scale))] text-center min-w-0">
          <div
            className={`font-bold leading-snug line-clamp-3 tracking-tight ${
              isLight ? "text-slate-900" : ""
            }`}
            style={{
              ...captionStyle,
              ...(isLight ? {} : { color: accent.hex }),
            }}
          >
            {props.item.title}
          </div>
          <div className="mt-1 font-mono tabular-nums" style={polaroidYearStyle}>
            {props.item.year || "—"}
          </div>
        </div>
      </button>
    );
  }

  if (props.variant === "spotlight") {
    /* Dark stage: poster hero + fixed caption band (year, adaptive title) for row alignment */
    const titleSlotPx = adaptiveTitleSlotHeightPx(scale);
    const yearRowPx = Math.ceil(14 * scale);
    const captionPadYPx = Math.ceil(8 * scale);
    const captionBandPx = yearRowPx + titleSlotPx + captionPadYPx * 2;
    const titleColor = isLight ? "#0f172a" : "#ffffff";
    const yearColor = isLight ? META_GREY_LIGHT : META_GREY_DARK;

    return (
      <LibraryCardScaleButton
        onClick={props.onClick}
        className={`group relative flex w-full flex-col rounded-2xl text-left cursor-pointer transition-transform hover:scale-[1.02] hover:z-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 overflow-visible ${
          isLight
            ? "bg-[var(--brand-chrome-main,#eef3f8)] text-slate-900 shadow-md shadow-slate-900/8 border border-slate-300/70"
            : "bg-black text-white"
        }`}
        style={{ ...cardStyle, borderColor: "transparent", aspectRatio: "2 / 3" }}
        clipClassName="relative flex w-full min-h-0 flex-col overflow-hidden rounded-[14px]"
      >
        <div
          className={`pointer-events-none absolute left-1/2 top-[18%] h-[50%] w-[85%] -translate-x-1/2 rounded-full blur-3xl ${
            isLight ? "opacity-35" : "opacity-50"
          }`}
          style={{ backgroundColor: accent.hex }}
          aria-hidden
        />

        <div className="relative z-[1] flex min-h-0 flex-1 flex-col px-[calc(8px*var(--library-card-scale))] pt-[calc(8px*var(--library-card-scale))] pb-[calc(4px*var(--library-card-scale))]">
          <div className="flex min-h-0 flex-1 items-center justify-center">
            <div
              className={`relative w-[92%] overflow-hidden rounded-xl ring-1 ${
                isLight
                  ? "shadow-[0_10px_24px_rgba(15,23,42,0.18)] ring-slate-900/10"
                  : "shadow-[0_12px_40px_rgba(0,0,0,0.55)] ring-white/15"
              }`}
              style={{ aspectRatio: "2 / 3", maxHeight: "100%" }}
            >
              {posterImage(props.item)}
              <div className="absolute inset-x-0 bottom-0 z-10">{statusBar(true)}</div>
            </div>
          </div>

          <div
            className="shrink-0 flex min-w-0 flex-col items-center justify-start px-[calc(2px*var(--library-card-scale))]"
            style={{
              height: captionBandPx,
              paddingTop: captionPadYPx,
              paddingBottom: captionPadYPx,
            }}
          >
            <div
              className="shrink-0 w-full text-center font-semibold tabular-nums"
              style={{
                height: yearRowPx,
                lineHeight: `${yearRowPx}px`,
                fontSize: `clamp(9px, calc(10px * var(--library-card-scale)), 12px)`,
                color: yearColor,
              }}
            >
              {props.item.year || "—"}
            </div>
            <StackTwoLineTitle
              title={props.item.title}
              color={titleColor}
              scale={scale}
              uppercase={false}
            />
          </div>
        </div>
      </LibraryCardScaleButton>
    );
  }

  if (props.variant === "ticket") {
    return stackCard(props.item, {
      accent,
      isLight,
      cardStyle,
      onClick: props.onClick,
      statusBar: statusBar(true),
      scale,
    });
  }

  return null;
}
