import { useEffect, useMemo, useState } from "react";
import { getCollectionHubs, type CollectionHubRow } from "../../api/collections";
import { BrandCheckbox } from "../../BrandCheckbox";
import type { ThemeMode } from "../../brandTypes";
import type { CollectionRecipe, PlexSectionOption } from "../../types/api";
import { ToggleSwitch } from "../../ToggleSwitch";
import { isCollectionSetRecipe } from "../CollectionSetEditor";
import { getCollectionTheme } from "../collectionTheme";

const DISCOVER_NAME_PREFIX = "Discover · ";

type RecipeKind = "recipe" | "set" | "discover";

type Placement = { hubs: number; recommended: number; home: number; shared: number };

function recipeKind(recipe: CollectionRecipe): RecipeKind {
  if (recipe.name.startsWith(DISCOVER_NAME_PREFIX)) return "discover";
  return isCollectionSetRecipe(recipe) ? "set" : "recipe";
}

function recipeSectionIds(recipe: CollectionRecipe): number[] {
  return recipe.plex_section_ids?.length ? recipe.plex_section_ids : [recipe.plex_section_id];
}

function setCollectionCount(recipe: CollectionRecipe): number | null {
  return recipe.last_run_summary?.collection_set?.collection_count ?? null;
}

export function RecipesGroupedList(props: {
  themeMode: ThemeMode;
  accentHex: string;
  recipes: CollectionRecipe[];
  sections: PlexSectionOption[];
  plexReady: boolean;
  selectedIds: Set<number>;
  runningIds: Set<number>;
  describeSchedule: (recipe: CollectionRecipe) => string;
  describeLastRun: (recipe: CollectionRecipe) => string;
  onToggleSelected: (id: number) => void;
  onSetSelected: (ids: number[], selected: boolean) => void;
  onEdit: (recipe: CollectionRecipe) => void;
  onRun: (recipe: CollectionRecipe) => void;
  onDelete: (recipe: CollectionRecipe) => void;
  onToggleEnabled: (recipe: CollectionRecipe, enabled: boolean) => void;
  onOpenOrdering?: () => void;
}) {
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);
  const [hubsBySection, setHubsBySection] = useState<Record<number, CollectionHubRow[]>>({});
  const [kindFilter, setKindFilter] = useState<RecipeKind | "all">("all");

  const sectionIdsInUse = useMemo(() => {
    const ids = new Set<number>();
    for (const recipe of props.recipes) for (const id of recipeSectionIds(recipe)) ids.add(id);
    return [...ids].sort((a, b) => a - b);
  }, [props.recipes]);
  const sectionKey = sectionIdsInUse.join(",");

  useEffect(() => {
    if (!props.plexReady || !sectionIdsInUse.length) return;
    let cancelled = false;
    void Promise.all(
      sectionIdsInUse.map((id) =>
        getCollectionHubs(id)
          .then((snap) => [id, snap.hubs ?? []] as const)
          .catch(() => [id, [] as CollectionHubRow[]] as const),
      ),
    ).then((rows) => {
      if (!cancelled) setHubsBySection(Object.fromEntries(rows));
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.plexReady, sectionKey]);

  const placementFor = (recipeId: number, sectionId: number): Placement | null => {
    const hubs = hubsBySection[sectionId];
    if (!hubs) return null;
    const mine = hubs.filter((h) => h.recipe_id === recipeId);
    return {
      hubs: mine.length,
      recommended: mine.filter((h) => h.promoted_to_recommended).length,
      home: mine.filter((h) => h.promoted_to_home).length,
      shared: mine.filter((h) => h.promoted_to_shared).length,
    };
  };

  const kindCounts = useMemo(() => {
    const counts: Record<RecipeKind, number> = { recipe: 0, set: 0, discover: 0 };
    for (const recipe of props.recipes) counts[recipeKind(recipe)] += 1;
    return counts;
  }, [props.recipes]);

  const groups = useMemo(() => {
    const visible = props.recipes.filter((r) => kindFilter === "all" || recipeKind(r) === kindFilter);
    return sectionIdsInUse
      .map((sectionId) => {
        const section = props.sections.find((s) => s.id === sectionId);
        return {
          sectionId,
          title: section?.title ?? `Section ${sectionId}`,
          type: section?.type ?? visible.find((r) => recipeSectionIds(r).includes(sectionId))?.plex_section_type,
          recipes: visible
            .filter((r) => recipeSectionIds(r).includes(sectionId))
            .sort((a, b) => a.name.localeCompare(b.name)),
        };
      })
      .filter((g) => g.recipes.length);
  }, [props.recipes, props.sections, sectionIdsInUse, kindFilter]);

  const chip = (active: boolean) =>
    `rounded-full border px-3 py-1 text-[12px] font-headline uppercase tracking-wider transition-colors ${
      active
        ? isLight
          ? "border-slate-900 bg-slate-900 text-white"
          : "border-slate-200 bg-slate-200 text-slate-900"
        : theme.chipInactive
    }`;

  const kindBadge = (recipe: CollectionRecipe) => {
    const kind = recipeKind(recipe);
    const count = setCollectionCount(recipe);
    const label =
      kind === "discover"
        ? "Discover"
        : kind === "set"
          ? `Set${count != null ? ` (${count})` : ""}`
          : "Recipe";
    const color =
      kind === "discover"
        ? "border-violet-400/40 text-violet-300"
        : kind === "set"
          ? "border-sky-400/40 text-sky-300"
          : isLight
            ? "border-slate-300 text-slate-600"
            : "border-[#424753] text-slate-300";
    return (
      <span
        className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-headline uppercase tracking-wider ${color}`}
      >
        {label}
      </span>
    );
  };

  const placementPills = (recipe: CollectionRecipe, sectionId: number) => {
    const placement = placementFor(recipe.id, sectionId);
    if (!placement) return <span className={`text-[12px] ${theme.muted}`}>…</span>;
    if (!placement.hubs) {
      return <span className={`text-[12px] ${theme.muted}`}>{recipe.last_run_at ? "Not in Plex" : "Not built yet"}</span>;
    }
    const many = placement.hubs > 1;
    const pills: { key: string; label: string; count: number }[] = [
      { key: "rec", label: "Recommended", count: placement.recommended },
      { key: "home", label: "Home", count: placement.home },
      { key: "shared", label: "Shared", count: placement.shared },
    ];
    const anyOn = pills.some((p) => p.count > 0);
    return (
      <button
        type="button"
        onClick={props.onOpenOrdering}
        disabled={!props.onOpenOrdering}
        title="Change where this appears on the Ordering page"
        className="flex flex-wrap items-center gap-1 text-left"
      >
        {anyOn ? (
          pills
            .filter((p) => p.count > 0)
            .map((p) => (
              <span
                key={p.key}
                className="rounded-full px-2 py-0.5 text-[11px] font-medium"
                style={{ backgroundColor: `${props.accentHex}26`, color: props.accentHex }}
              >
                {p.label}
                {many ? ` ${p.count}/${placement.hubs}` : ""}
              </span>
            ))
        ) : (
          <span className={`text-[12px] ${theme.muted}`}>Library tab only</span>
        )}
      </button>
    );
  };

  const statusLine = (recipe: CollectionRecipe) => {
    const summary = recipe.last_run_summary;
    if (!summary) return null;
    if (summary.status === "ok") {
      return (
        <span className="text-[12px] text-green-400/90">
          +{summary.synced?.added ?? 0} / -{summary.synced?.removed ?? 0}
          {(summary.missing_from_arr_count ?? 0) > 0 ? (
            <span className={`ml-1.5 ${theme.muted}`}>· {summary.missing_from_arr_count} not in catalog</span>
          ) : null}
        </span>
      );
    }
    if (summary.status === "cleared") return <span className={`text-[12px] ${theme.muted}`}>Cleared (out of window)</span>;
    if (summary.status === "removed") return <span className={`text-[12px] ${theme.muted}`}>Removed (out of window)</span>;
    if (summary.status === "dormant") return <span className={`text-[12px] ${theme.muted}`}>Kept (out of window)</span>;
    return (
      <span className="text-[12px] text-red-400" title={summary.error}>
        Failed
      </span>
    );
  };

  const iconButton = (variant: "neutral" | "danger") =>
    `inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border transition disabled:cursor-not-allowed disabled:opacity-40 ${
      variant === "danger"
        ? isLight
          ? "border-red-300/50 text-red-600 hover:bg-red-50"
          : "border-red-400/25 text-red-300 hover:bg-red-500/10"
        : isLight
          ? "border-slate-200 text-slate-600 hover:border-slate-300 hover:text-slate-900"
          : "border-white/10 text-slate-300 hover:border-white/25 hover:text-white"
    }`;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={chip(kindFilter === "all")} onClick={() => setKindFilter("all")}>
          All ({props.recipes.length})
        </button>
        <button type="button" className={chip(kindFilter === "recipe")} onClick={() => setKindFilter("recipe")}>
          Recipes ({kindCounts.recipe})
        </button>
        <button type="button" className={chip(kindFilter === "set")} onClick={() => setKindFilter("set")}>
          Sets ({kindCounts.set})
        </button>
        <button type="button" className={chip(kindFilter === "discover")} onClick={() => setKindFilter("discover")}>
          Discover ({kindCounts.discover})
        </button>
      </div>

      {groups.map((group) => {
        const ids = group.recipes.map((r) => r.id);
        const allSelected = ids.every((id) => props.selectedIds.has(id));
        return (
          <section key={group.sectionId} className={`${theme.blockCard} overflow-hidden`}>
            <header className={`flex items-center gap-3 border-b px-4 py-3 ${theme.divider}`}>
              <BrandCheckbox
                checked={allSelected}
                size="sm"
                accentHex={props.accentHex}
                ariaLabel={`Select all in ${group.title}`}
                onChange={() => props.onSetSelected(ids, !allSelected)}
              />
              <span className="material-symbols-outlined" style={{ fontSize: 18, color: props.accentHex }} aria-hidden>
                {group.type === "show" ? "tv" : "movie"}
              </span>
              <span className={`text-[16px] font-headline font-bold ${isLight ? "text-slate-900" : "text-white"}`}>
                {group.title}
              </span>
              <span className={`text-[12px] uppercase tracking-wider ${theme.muted}`}>
                {group.type === "show" ? "TV" : "Movies"} · {group.recipes.length}
              </span>
            </header>
            <div className={`grid grid-cols-[auto_minmax(0,1fr)_minmax(9rem,auto)_minmax(8rem,auto)_auto_auto] items-center gap-x-4 px-4 py-2 text-[11px] font-headline uppercase tracking-widest ${theme.muted}`}>
              <span className="w-4" />
              <span>Collection</span>
              <span>Shows on</span>
              <span>Last run</span>
              <span>On</span>
              <span className="w-[6.5rem]" />
            </div>
            <ul className={isLight ? "divide-y divide-slate-200/80" : "divide-y divide-[#424753]/15"}>
              {group.recipes.map((recipe) => {
                const running = props.runningIds.has(recipe.id);
                const kind = recipeKind(recipe);
                const otherLibraries = recipeSectionIds(recipe).length - 1;
                return (
                  <li
                    key={recipe.id}
                    className={`grid grid-cols-[auto_minmax(0,1fr)_minmax(9rem,auto)_minmax(8rem,auto)_auto_auto] items-center gap-x-4 px-4 py-3 ${
                      isLight ? "hover:bg-[#f2f7ff]" : "hover:bg-[#1e2430]/40"
                    }`}
                  >
                    <BrandCheckbox
                      checked={props.selectedIds.has(recipe.id)}
                      size="sm"
                      accentHex={props.accentHex}
                      ariaLabel={`Select ${recipe.name}`}
                      onChange={() => props.onToggleSelected(recipe.id)}
                    />
                    <button type="button" onClick={() => props.onEdit(recipe)} className="min-w-0 text-left">
                      <span className="flex items-center gap-2">
                        {kindBadge(recipe)}
                        <span
                          className={`truncate text-[15px] ${isLight ? "text-slate-900 hover:text-sky-800" : "text-slate-100 hover:text-white"}`}
                        >
                          {kind === "discover" ? recipe.name.slice(DISCOVER_NAME_PREFIX.length) : recipe.name}
                        </span>
                        {recipe.active_window && !recipe.window_active ? (
                          <span className="shrink-0 rounded-full border border-slate-500/40 px-2 py-0.5 text-[10px] font-headline uppercase tracking-wider text-slate-400">
                            Dormant
                          </span>
                        ) : null}
                      </span>
                      <span className={`mt-0.5 block truncate text-[12px] ${theme.muted}`}>
                        {kind === "set"
                          ? `${recipe.definition.collection_set?.category ?? "category"} · ${recipe.collection_title}`
                          : `"${recipe.collection_title}"`}
                        {" · "}
                        {props.describeSchedule(recipe)}
                        {otherLibraries > 0 ? ` · also in ${otherLibraries} other librar${otherLibraries === 1 ? "y" : "ies"}` : ""}
                      </span>
                    </button>
                    <div>{placementPills(recipe, group.sectionId)}</div>
                    <div className="flex flex-col">
                      <span className={`text-[13px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
                        {props.describeLastRun(recipe)}
                      </span>
                      {statusLine(recipe)}
                    </div>
                    <ToggleSwitch
                      checked={recipe.enabled}
                      onChange={(enabled) => props.onToggleEnabled(recipe, enabled)}
                      accentHex={props.accentHex}
                      size="sm"
                      ariaLabel={recipe.enabled ? "Disable scheduled runs" : "Enable scheduled runs"}
                    />
                    <div className="flex w-[6.5rem] items-center justify-end gap-1">
                      <button
                        type="button"
                        disabled={running || !props.plexReady}
                        onClick={() => props.onRun(recipe)}
                        className={iconButton("neutral")}
                        title={running ? "Running…" : "Run now"}
                        aria-label={running ? "Running" : "Run now"}
                      >
                        <span className={`material-symbols-outlined ${running ? "animate-spin" : ""}`} style={{ fontSize: 18 }}>
                          {running ? "progress_activity" : "play_arrow"}
                        </span>
                      </button>
                      <button
                        type="button"
                        onClick={() => props.onEdit(recipe)}
                        className={iconButton("neutral")}
                        title="Edit"
                        aria-label="Edit"
                      >
                        <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                          edit
                        </span>
                      </button>
                      <button
                        type="button"
                        onClick={() => props.onDelete(recipe)}
                        className={iconButton("danger")}
                        title="Delete"
                        aria-label="Delete"
                      >
                        <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                          delete
                        </span>
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
      {!groups.length ? <p className={`text-[14px] ${theme.muted}`}>Nothing matches this filter.</p> : null}
    </div>
  );
}
