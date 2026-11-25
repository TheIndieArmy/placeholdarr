import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ThemeMode } from "../../brandTypes";
import { BrandCheckbox } from "../../BrandCheckbox";
import type { CollectionPreviewResponse } from "../../types/api";
import { fileStateOutline, PosterTitleFallback } from "../collectionPreviewPosters";
import { getCollectionTheme } from "../collectionTheme";
import type { PlexSectionOption } from "../../types/api";
import { useDockPrefs } from "./dockPrefs";
import { buildFilmstrip, filmstripForLibrary, type FilmstripItem } from "./filmstrip";
import { PreviewLibraryChips } from "./PreviewLibraryChips";

export function FilmstripPoster(props: { item: FilmstripItem; animate: boolean; width: number; isLight: boolean }) {
  const { item } = props;
  const theme = getCollectionTheme(props.isLight);
  return (
    <div
      title={`${item.title}${item.year ? ` (${item.year})` : ""}${item.ghost ? " · Added on next sync" : ""}`}
      className={`relative shrink-0 overflow-hidden rounded-md ${theme.posterFallback} ${
        item.ghost ? "opacity-55 outline outline-2 outline-dashed outline-offset-[-2px] outline-amber-400" : fileStateOutline(item.fileState)
      } ${props.animate ? "dock-poster-in" : ""}`}
      style={{ width: props.width, aspectRatio: "2 / 3" }}
    >
      {item.poster ? (
        <img src={item.poster} alt={item.title} className={`h-full w-full object-cover ${item.ghost ? "grayscale-[60%]" : ""}`} loading="lazy" />
      ) : (
        <PosterTitleFallback title={item.title} className={theme.sampleFallback} />
      )}
      {item.ghost ? (
        <span className="absolute left-0.5 top-0.5 rounded bg-amber-500/90 px-1 text-[8px] font-headline uppercase tracking-wider text-black">
          Sync
        </span>
      ) : null}
    </div>
  );
}

/** Frozen bottom dock: the collection as one Plex row, live counts, and the save or apply actions. */
export function PreviewDock(props: {
  themeMode: ThemeMode;
  accentHex: string;
  preview: CollectionPreviewResponse | null;
  loading: boolean;
  error?: string | null;
  emptyMessage?: string | null;
  autoAddActive: boolean;
  /** Collection set: Plex membership vs recipe selection for the active child. */
  setChildCounts?: { inPlex: number; recipeSelected: number } | null;
  /** Target libraries for this recipe/set (for In-this-library dock filter). */
  libraries?: PlexSectionOption[];
  /** Required for In Plex row: which library's membership to show. */
  focusLibraryId?: number | null;
  onFocusLibraryChange?: (sectionId: number | null) => void;
  switcher?: ReactNode;
  onExpand: () => void;
  actions: ReactNode;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const [prefs, setPrefs] = useDockPrefs();
  const [optionsOpen, setOptionsOpen] = useState(false);
  const optionsRef = useRef<HTMLDivElement>(null);
  const libraries = props.libraries ?? [];
  const focusLibraryId =
    props.focusLibraryId ?? (libraries.length === 1 ? libraries[0].id : libraries[0]?.id ?? null);
  const items = useMemo(() => {
    const all = buildFilmstrip(props.preview, prefs.ghosts && props.autoAddActive);
    return filmstripForLibrary(all, focusLibraryId);
  }, [props.preview, prefs.ghosts, props.autoAddActive, focusLibraryId]);

  const prevKeys = useRef<Set<string> | null>(null);
  const [delta, setDelta] = useState<{ added: number; removed: number; tick: number } | null>(null);
  const signature = items.map((i) => i.key).join("|");
  useEffect(() => {
    const next = new Set(items.map((i) => i.key));
    const prev = prevKeys.current;
    prevKeys.current = next;
    if (!prev) return;
    let added = 0;
    let removed = 0;
    next.forEach((k) => {
      if (!prev.has(k)) added += 1;
    });
    prev.forEach((k) => {
      if (!next.has(k)) removed += 1;
    });
    if (added || removed) setDelta((d) => ({ added, removed, tick: (d?.tick ?? 0) + 1 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  useEffect(() => {
    if (!optionsOpen) return;
    const onDown = (event: MouseEvent) => {
      if (!optionsRef.current?.contains(event.target as Node)) setOptionsOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [optionsOpen]);

  const p = props.preview;
  const ghostCount = items.filter((i) => i.ghost).length;
  const counts = props.setChildCounts;
  const libSummary =
    focusLibraryId != null
      ? (p?.libraries ?? []).find((lib) => lib.plex_section_id === focusLibraryId)
      : null;
  const inThisLibrary =
    libSummary?.in_target_library != null
      ? libSummary.in_target_library
      : items.filter((i) => !i.ghost).length;
  const stats = [
    counts != null
      ? `${counts.inPlex} in this library · ${counts.recipeSelected} selected`
      : focusLibraryId != null
        ? `${inThisLibrary} in this library`
        : p?.selected != null
          ? `${p.selected} in collection`
          : null,
    props.autoAddActive && typeof p?.to_add_count === "number" ? `${p.to_add_count} would add` : null,
    (p?.missing_from_arr_count ?? 0) > 0 ? `${p?.missing_from_arr_count} missing` : null,
  ].filter(Boolean);
  return (
    <div
      className={`fixed inset-x-0 bottom-0 z-40 border-t backdrop-blur-sm lg:left-64 ${
        isLight ? "border-slate-200 bg-white/95" : "border-[#424753]/50 bg-[#0f1419]/95"
      }`}
    >
      <button
        type="button"
        onClick={props.onExpand}
        className={`absolute left-1/2 top-0 flex -translate-x-1/2 -translate-y-1/2 items-center gap-1.5 rounded-full border px-4 py-1.5 text-[12px] font-headline uppercase tracking-wider shadow-lg transition-colors ${
          isLight ? "border-slate-300 bg-white text-slate-800 hover:bg-slate-50" : "border-[#4b5563] bg-[#1a2029] text-slate-100 hover:bg-[#222a35]"
        }`}
      >
        <span className="material-symbols-outlined" style={{ fontSize: 18, color: props.accentHex }}>
          open_in_full
        </span>
        Full preview
      </button>

      <div className="mx-auto flex max-w-[90rem] flex-col gap-2 px-4 pb-3 pt-4">
        <div className="flex flex-wrap items-center gap-3">
          {props.switcher ? <div className="flex min-w-0 items-center gap-1">{props.switcher}</div> : null}
          <div className={`flex min-w-0 items-center gap-2 text-[12px] ${theme.muted}`}>
            {props.loading ? (
              <span className="h-2 w-2 shrink-0 animate-pulse rounded-full" style={{ backgroundColor: props.accentHex }} />
            ) : null}
            <span className="truncate">
              {stats.length ? stats.join(" · ") : props.loading ? "Updating preview…" : ""}
            </span>
            {delta && prefs.animate ? (
              <span key={delta.tick} className="dock-delta-fade shrink-0 font-mono">
                {delta.added ? <span className="text-emerald-400">+{delta.added}</span> : null}
                {delta.added && delta.removed ? " " : null}
                {delta.removed ? <span className="text-red-400">−{delta.removed}</span> : null}
              </span>
            ) : null}
          </div>
          <div className="ml-auto flex items-center gap-3">{props.actions}</div>
          <div ref={optionsRef} className="relative flex items-center">
            <button
              type="button"
              onClick={() => setOptionsOpen((v) => !v)}
              className={`material-symbols-outlined rounded p-1 ${theme.iconAction}`}
              style={{ fontSize: 18 }}
              title="Dock options"
              aria-label="Dock options"
            >
              tune
            </button>
            <button
              type="button"
              onClick={() => setPrefs({ minimized: !prefs.minimized })}
              className={`material-symbols-outlined rounded p-1 ${theme.iconAction}`}
              style={{ fontSize: 18 }}
              title={prefs.minimized ? "Show posters" : "Hide posters"}
              aria-label={prefs.minimized ? "Show posters" : "Hide posters"}
            >
              {prefs.minimized ? "expand_less" : "expand_more"}
            </button>
            {optionsOpen ? (
              <button
                type="button"
                className="fixed inset-0 z-[45] bg-black/35 lg:hidden"
                aria-label="Close dock options"
                onClick={() => setOptionsOpen(false)}
              />
            ) : null}
            {optionsOpen ? (
              <div
                className={`z-[46] rounded-lg border p-3 shadow-lg ${
                  isLight ? "border-slate-200 bg-white" : "border-[#424753]/60 bg-[#171c22]"
                } max-lg:fixed max-lg:inset-x-3 max-lg:bottom-[max(9.5rem,calc(env(safe-area-inset-bottom,0px)+8.5rem))] lg:absolute lg:bottom-full lg:right-0 lg:mb-2 lg:w-64 lg:max-w-[min(16rem,calc(100vw-2rem))]`}
              >
                <div className={`mb-2 text-[11px] font-headline uppercase tracking-widest ${theme.muted}`}>Dock options</div>
                {(
                  [
                    ["ghosts", "Show would-add posters in the row"],
                    ["animate", "Animate changes"],
                    ["plexMock", "Plex row mock in full preview"],
                  ] as const
                ).map(([key, label]) => (
                  <label key={key} className={`flex cursor-pointer items-center gap-2 py-1 text-[13px] ${theme.label}`}>
                    <BrandCheckbox
                      checked={prefs[key]}
                      size="sm"
                      accentHex={props.accentHex}
                      ariaLabel={label}
                      onChange={(v) => setPrefs({ [key]: v })}
                    />
                    {label}
                  </label>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        {libraries.length > 1 && props.onFocusLibraryChange ? (
          <PreviewLibraryChips
            themeMode={props.themeMode}
            accentHex={props.accentHex}
            libraries={libraries}
            value={focusLibraryId}
            onChange={(id) => props.onFocusLibraryChange?.(id ?? libraries[0]?.id ?? null)}
            requireLibrary
          />
        ) : null}

        {!prefs.minimized ? (
          <div className="dock-filmstrip-outer">
            <div className="dock-filmstrip-row">
            {props.emptyMessage ? (
              <p className={`text-[13px] ${theme.muted}`}>{props.emptyMessage}</p>
            ) : props.error ? (
              <p className="text-[13px] text-red-400">{props.error}</p>
            ) : items.length ? (
              <>
                {items.map((item) => (
                  <FilmstripPoster key={item.key} item={item} animate={prefs.animate} width={64} isLight={isLight} />
                ))}
                {ghostCount && prefs.ghosts ? (
                  <span className={`ml-2 shrink-0 text-[11px] ${theme.muted}`}>
                    Dashed: added on next sync
                  </span>
                ) : null}
              </>
            ) : props.loading ? (
              Array.from({ length: 10 }).map((_, i) => (
                <div key={i} className={`shrink-0 animate-pulse rounded-md ${theme.posterFallback}`} style={{ width: 64, aspectRatio: "2 / 3" }} />
              ))
            ) : p ? (
              <p className={`text-[13px] ${theme.muted}`}>Nothing in your library matches yet.</p>
            ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
