import { useEffect, useState } from "react";
import { getCollectionHubs, type CollectionHubRow } from "../../api/collections";
import type { ThemeMode } from "../../brandTypes";
import { getCollectionTheme } from "../collectionTheme";
import type { FilmstripItem } from "./filmstrip";
import { FilmstripPoster } from "./PreviewDock";

const NEIGHBORS = 2;

/** Mock of the library's Recommended tab: this collection's row between its real Plex neighbours. */
export function PlexRowMock(props: {
  themeMode: ThemeMode;
  accentHex: string;
  sectionId: number | null;
  libraryTitle?: string;
  collectionTitle: string;
  recipeId?: number | null;
  items: FilmstripItem[];
  promotedOnCreate?: boolean;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const [hubs, setHubs] = useState<CollectionHubRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!props.sectionId) return;
    let cancelled = false;
    setHubs(null);
    setError(null);
    getCollectionHubs(props.sectionId)
      .then((snap) => {
        if (!cancelled) setHubs(snap.hubs ?? []);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not read Plex rows");
      });
    return () => {
      cancelled = true;
    };
  }, [props.sectionId]);

  if (!props.sectionId) return null;

  const title = props.collectionTitle.trim();
  const index = (hubs ?? []).findIndex(
    (h) => h.title === title && (props.recipeId == null || h.recipe_id == null || h.recipe_id === props.recipeId),
  );
  const onRecommended = index >= 0 && Boolean(hubs?.[index]?.promoted_to_recommended);
  const list = hubs ?? [];
  const above = index >= 0 ? list.slice(Math.max(0, index - NEIGHBORS), index) : list.slice(-NEIGHBORS);
  const below = index >= 0 ? list.slice(index + 1, index + 1 + NEIGHBORS) : [];

  const neighbor = (hub: CollectionHubRow) => (
    <div key={hub.identifier} className="flex flex-col gap-1.5 opacity-50">
      <span className={`text-[13px] font-semibold ${isLight ? "text-slate-700" : "text-slate-300"}`}>{hub.title}</span>
      <div className="flex gap-2 overflow-hidden">
        {Array.from({ length: 12 }).map((_, i) => (
          <div key={i} className={`shrink-0 rounded-md ${theme.posterFallback}`} style={{ width: 72, aspectRatio: "2 / 3" }} />
        ))}
      </div>
    </div>
  );

  const note =
    index >= 0
      ? onRecommended
        ? `Row ${index + 1} of ${list.length} on Recommended. Change it on the Ordering page.`
        : "This collection exists but is not on Recommended. Promote it on the Ordering page."
      : props.promotedOnCreate
        ? "Not created yet. It joins the end of Recommended when created; move it on the Ordering page."
        : "Not created yet. It will show in the library's Collections tab; promote it on the Ordering page.";

  return (
    <div className={`${theme.blockCard} flex flex-col gap-4 p-4`}>
      <div className="flex items-center gap-2">
        <span className="material-symbols-outlined" style={{ fontSize: 18, color: props.accentHex }}>
          live_tv
        </span>
        <span className={theme.blockTitle}>In Plex: {props.libraryTitle ?? "Library"} · Recommended</span>
      </div>
      {error ? <p className="text-[13px] text-amber-400">{error}</p> : null}
      {hubs == null && !error ? <p className={`text-[13px] ${theme.muted}`}>Reading rows from Plex…</p> : null}
      {hubs != null ? (
        <div className={`flex flex-col gap-4 rounded-lg p-4 ${isLight ? "bg-slate-900/90" : "bg-black/40"}`}>
          {above.map(neighbor)}
          <div
            className="flex flex-col gap-1.5 rounded-lg p-2"
            style={{ boxShadow: `0 0 0 2px ${props.accentHex}`, opacity: index >= 0 && !onRecommended ? 0.8 : 1 }}
          >
            <span className="text-[13px] font-semibold text-white">{title || "Untitled collection"}</span>
            <div className="flex gap-2 overflow-x-auto pb-1">
              {props.items.length ? (
                props.items.map((item) => (
                  <FilmstripPoster key={item.key} item={item} animate={false} width={72} isLight={false} />
                ))
              ) : (
                <span className="text-[12px] text-slate-400">No titles yet.</span>
              )}
            </div>
          </div>
          {below.map(neighbor)}
        </div>
      ) : null}
      <p className={`text-[12px] ${theme.muted}`}>{note}</p>
    </div>
  );
}
