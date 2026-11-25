import { useEffect, useState } from "react";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";
import type { ThemeMode } from "../../brandTypes";
import type { CollectionPreviewResponse, PlexSectionOption } from "../../types/api";
import { CatalogSamplePoster, PosterTitleFallback } from "../collectionPreviewPosters";
import { CollectionThemeProvider, getCollectionTheme } from "../collectionTheme";
import { PreviewLibraryChips } from "./PreviewLibraryChips";

/** Stage counts, In catalog / Missing tabs, file-state outlines, and Sync badges for one preview response. */
export function RecipePreviewResultView(props: {
  themeMode: ThemeMode;
  accentHex: string;
  sections: PlexSectionOption[];
  sectionIds: number[];
  preview: CollectionPreviewResponse | null;
  loading: boolean;
  error: string | null;
  title?: string | null;
  autoAddConfigured: boolean;
  autoAddActive: boolean;
  /** Wider poster grid for the full-preview sheet (scrollable, multiple rows). */
  layout?: "default" | "sheet";
  /** null = combined (All libraries). */
  libraryFilter?: number | null;
  onLibraryFilterChange?: (sectionId: number | null) => void;
}) {
  const theme = getCollectionTheme(props.themeMode === "light");
  const { preview, loading, error, autoAddConfigured, autoAddActive } = props;
  const [pane, setPane] = useState<"catalog" | "missing">("catalog");
  const [localFilter, setLocalFilter] = useState<number | null>(null);
  const libraryFilter = props.libraryFilter !== undefined ? props.libraryFilter : localFilter;
  const setLibraryFilter = props.onLibraryFilterChange ?? setLocalFilter;

  useEffect(() => {
    setPane("catalog");
  }, [props.title]);

  const missingItems =
    autoAddConfigured && Array.isArray(preview?.missing_from_arr_preview)
      ? preview.missing_from_arr_preview
      : (preview?.missing_from_arr ?? []);
  const missingCount = missingItems.length || (preview?.missing_from_arr_count ?? 0);
  const syncCount = missingItems.filter((item) => item.would_auto_add).length;
  const sample = preview?.sample ?? [];
  const targetLibraries = props.sections.filter((s) => props.sectionIds.includes(s.id));
  const showLibraryTicks = targetLibraries.length > 1;
  const stages: { label: string; value: number | null | undefined }[] = [
    { label: "List candidates", value: preview?.tmdb_candidates },
    { label: "Matched in catalog", value: preview?.matched_in_catalog },
    { label: "After filters", value: preview?.after_filters },
    { label: "Selected", value: preview?.selected },
    ...(missingCount ? [{ label: "Missing from catalog", value: missingCount }] : []),
    ...(autoAddActive ? [{ label: "Would add on sync", value: syncCount }] : []),
  ];

  const tabClass = (active: boolean) =>
    `rounded-md border px-2 py-1 text-[11px] font-headline uppercase tracking-wider ${
      active ? FG_ON_ACCENT_TEXT_CLASS : theme.chipInactive
    }`;
  const tabStyle = (active: boolean) =>
    active ? { ...accentFilledStyle(props.accentHex), borderColor: props.accentHex } : undefined;

  const posterGridClass =
    props.layout === "sheet"
      ? "grid grid-cols-4 gap-2 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10"
      : "grid grid-cols-4 gap-2 sm:grid-cols-5";

  return (
    <CollectionThemeProvider value={theme}>
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <span className={`min-w-0 flex-1 truncate ${theme.heading}`}>{props.title ?? "Preview"}</span>
          {loading ? (
            <span className="h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: props.accentHex }} />
          ) : null}
        </div>
        {error ? <p className="text-[13px] text-red-400">{error}</p> : null}
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
          {stages.map((stage) => (
            <div key={stage.label} className="flex flex-col">
              <span className={`text-[11px] uppercase tracking-wider ${theme.muted}`}>{stage.label}</span>
              <span className={theme.previewValue}>{stage.value == null ? "—" : stage.value}</span>
            </div>
          ))}
        </div>
        {preview?.plex_error ? <p className="text-[12px] text-yellow-400/90">{preview.plex_error}</p> : null}

        {missingCount > 0 ? (
          <div className="flex flex-wrap gap-1">
            <button type="button" onClick={() => setPane("catalog")} className={tabClass(pane === "catalog")} style={tabStyle(pane === "catalog")}>
              In catalog ({sample.length})
            </button>
            <button type="button" onClick={() => setPane("missing")} className={tabClass(pane === "missing")} style={tabStyle(pane === "missing")}>
              Missing from catalog ({missingCount})
            </button>
          </div>
        ) : null}

        {pane === "missing" && missingCount > 0 ? (
          <>
            {autoAddActive && syncCount > 0 ? (
              <div className={`flex flex-wrap items-center gap-2 text-[12px] ${theme.muted}`}>
                <span className="inline-block h-2.5 w-2.5 rounded-sm outline outline-2 outline-offset-[-1px] outline-amber-400" />
                {syncCount} title{syncCount === 1 ? "" : "s"} would be added on the next sync
              </div>
            ) : null}
            <div className={posterGridClass}>
              {missingItems.map((item) => {
                const sync = Boolean(item.would_auto_add);
                return (
                  <div
                    key={`${item.tmdb_id ?? ""}:${item.tvdb_id ?? ""}:${item.title}:${item.year ?? ""}`}
                    title={`${item.title}${item.year ? ` (${item.year})` : ""}${sync ? " · Added on next sync" : ""}`}
                    className={`relative aspect-[2/3] w-full overflow-hidden rounded-md ${theme.posterFallback} ${
                      sync ? "outline outline-2 outline-offset-[-2px] outline-amber-400" : "opacity-70"
                    }`}
                  >
                    {item.poster ? (
                      <img src={item.poster} alt={item.title} className="h-full w-full object-cover" loading="lazy" />
                    ) : (
                      <PosterTitleFallback title={item.title} />
                    )}
                    {sync ? (
                      <span className="absolute left-1 top-1 rounded bg-amber-500/90 px-1 text-[8px] font-headline uppercase tracking-wider text-black">
                        Sync
                      </span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </>
        ) : sample.length ? (
          <>
            <div className={`flex flex-wrap items-center gap-2 text-[12px] ${theme.muted}`}>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm outline outline-2 outline-emerald-400" />
                File
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm outline outline-2 outline-dashed outline-cyan-400" />
                Placeholder
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm outline outline-2 outline-dashed outline-violet-400" />
                Both
              </span>
            </div>
            {showLibraryTicks ? (
              <PreviewLibraryChips
                themeMode={props.themeMode}
                accentHex={props.accentHex}
                libraries={targetLibraries}
                value={libraryFilter}
                onChange={setLibraryFilter}
              />
            ) : null}
            <div className={posterGridClass}>
              {sample.map((item) => (
                <CatalogSamplePoster
                  key={item.id}
                  item={item}
                  libraries={targetLibraries}
                  showLibraryTicks={showLibraryTicks}
                  dimmed={
                    libraryFilter != null && !(item.in_libraries ?? []).includes(libraryFilter)
                  }
                />
              ))}
            </div>
          </>
        ) : !loading && preview ? (
          <p className={`text-[13px] ${theme.muted}`}>Nothing in your library matches this collection yet.</p>
        ) : null}
      </div>
    </CollectionThemeProvider>
  );
}
