import type { CSSProperties } from "react";
import type { LibraryItem } from "../types/api";
import type { ThemeMode } from "../brandTypes";
import { LIBRARY_CARD_SIZE_DEFAULT } from "./cardSettings";
import type { LibraryCardAccent } from "./LibraryGridCard";
import { LibraryCardStatusBar } from "./LibraryCardStatusBar";

export type LibraryListRowProps = {
  item: LibraryItem;
  posterWidthPx: number;
  themeMode: ThemeMode;
  accent: LibraryCardAccent;
  onClick: () => void;
};

export function libraryListThumbPx(posterWidthPx: number): number {
  const scale = posterWidthPx / LIBRARY_CARD_SIZE_DEFAULT;
  return Math.round(Math.min(80, Math.max(44, 52 * scale)));
}

export function LibraryListRow(props: LibraryListRowProps) {
  const isLight = props.themeMode === "light";
  const thumbPx = libraryListThumbPx(props.posterWidthPx);
  const scale = props.posterWidthPx / LIBRARY_CARD_SIZE_DEFAULT;

  const cardStyle = {
    "--library-card-scale": String(scale),
    borderColor: props.accent.hex,
  } as CSSProperties;

  const titleStyle: CSSProperties = {
    fontSize: `clamp(13px, calc(14px * var(--library-card-scale)), 17px)`,
  };
  const yearStyle: CSSProperties = {
    color: props.accent.icon,
    fontSize: `clamp(11px, calc(12px * var(--library-card-scale)), 14px)`,
  };

  return (
    <button
      type="button"
      onClick={props.onClick}
      className={`group flex w-full items-stretch gap-3 rounded-xl border-2 px-3 py-2.5 text-left transition-colors hover:scale-[1.005] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ${
        isLight
          ? "bg-white shadow-sm shadow-slate-900/5 hover:bg-slate-50/80"
          : "bg-[#1e2430] hover:bg-[#252c3a]"
      }`}
      style={cardStyle}
    >
      <div
        className={`relative shrink-0 overflow-hidden rounded-md ring-1 ${
          isLight ? "ring-slate-200/90" : "ring-white/10"
        }`}
        style={{ width: thumbPx, aspectRatio: "2 / 3" }}
      >
        {props.item.poster_url ? (
          <img
            src={props.item.poster_url}
            alt=""
            loading="lazy"
            decoding="async"
            className="absolute inset-0 h-full w-full object-cover"
          />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center bg-slate-800/80 text-slate-500 font-headline text-[9px] uppercase">
            No art
          </div>
        )}
      </div>

      <div className="flex min-w-0 flex-1 flex-col justify-center gap-1.5 py-0.5">
        <div className="min-w-0">
          <div
            className={`font-bold leading-snug line-clamp-2 ${isLight ? "text-slate-900" : "text-white"}`}
            style={titleStyle}
          >
            {props.item.title}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="font-semibold tabular-nums" style={yearStyle}>
              {props.item.year || "—"}
            </span>
            {props.item.instance_label ? (
              <span
                className={`truncate font-headline uppercase tracking-wider text-[10px] ${
                  isLight ? "text-slate-500" : "text-slate-500"
                }`}
              >
                {props.item.instance_label}
              </span>
            ) : null}
          </div>
        </div>
        <LibraryCardStatusBar item={props.item} isLight={isLight} className="max-w-md" />
      </div>

      <span
        className={`material-symbols-outlined shrink-0 self-center opacity-0 transition-opacity group-hover:opacity-60 ${
          isLight ? "text-slate-400" : "text-slate-500"
        }`}
        style={{ fontSize: 20 }}
        aria-hidden
      >
        chevron_right
      </span>
    </button>
  );
}
