import { useCallback, useEffect, useState } from "react";
import type { ThemeMode } from "../brandTypes";
import {
  createCollectionRecipe,
  deleteCollectionRecipe,
  getCollectionPlexSections,
  getCollectionRecipes,
  runCollectionRecipe,
  toggleCollectionRecipe,
  updateCollectionRecipe,
  type RecipeWritePayload,
} from "../api/collections";
import type { CollectionRecipe, LibraryItem, PlexSectionOption } from "../types/api";
import { ToggleSwitch } from "../ToggleSwitch";
import { CollectionEditor } from "./CollectionEditor";
import { getCollectionTheme } from "./collectionTheme";

function formatSchedule(recipe: CollectionRecipe): string {
  const hours = recipe.run_interval_hours;
  if (hours == null) return "App default";
  if (hours === 1) return "Hourly";
  if (hours === 24) return "Daily";
  if (hours === 168) return "Weekly";
  if (hours % 24 === 0) return `Every ${hours / 24}d`;
  return `Every ${hours}h`;
}

function formatLastRun(recipe: CollectionRecipe): string {
  if (!recipe.last_run_at) return "Never run";
  try {
    const date = new Date(recipe.last_run_at);
    return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return recipe.last_run_at;
  }
}

export function CollectionsPanel(props: {
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
  libraryItems: LibraryItem[];
  libraryLoading: boolean;
  onEnsureLibrary: () => void;
  /** Navigate to Settings → Media Integrations (Plex). */
  onOpenPlexSettings?: () => void;
}) {
  const accentHex = props.accent.hex;
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);

  const [recipes, setRecipes] = useState<CollectionRecipe[]>([]);
  const [tmdbConfigured, setTmdbConfigured] = useState(true);
  const [traktConfigured, setTraktConfigured] = useState(true);
  const [sections, setSections] = useState<PlexSectionOption[]>([]);
  const [sectionsError, setSectionsError] = useState<string | null>(null);
  const [sectionsLoaded, setSectionsLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // null = list view; "new" = creating; recipe = editing
  const [editing, setEditing] = useState<CollectionRecipe | "new" | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [runningIds, setRunningIds] = useState<Set<number>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);

  const plexReady = sectionsLoaded && !sectionsError;

  const refresh = useCallback(async () => {
    try {
      const payload = await getCollectionRecipes();
      setRecipes(payload.recipes);
      setTmdbConfigured(payload.tmdb_configured);
      setTraktConfigured(payload.trakt_configured);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Failed to load collections");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    getCollectionPlexSections()
      .then((payload) => {
        setSections(payload.sections);
        setSectionsError(null);
      })
      .catch((err) => {
        setSections([]);
        setSectionsError(err instanceof Error ? err.message : "Plex libraries unavailable");
      })
      .finally(() => {
        setSectionsLoaded(true);
      });
  }, [refresh]);

  // Poll while a manual run is in-flight so last-run summaries land in the list.
  useEffect(() => {
    if (!runningIds.size) return;
    const timer = window.setInterval(() => {
      void refresh().then(() => {
        setRunningIds((prev) => {
          const next = new Set(prev);
          for (const id of prev) {
            const recipe = recipes.find((r) => r.id === id);
            if (recipe?.last_run_summary) next.delete(id);
          }
          return next;
        });
      });
    }, 4000);
    return () => window.clearInterval(timer);
  }, [runningIds, refresh, recipes]);

  const handleSave = async (payload: RecipeWritePayload) => {
    setSaving(true);
    setSaveError(null);
    try {
      if (editing && editing !== "new") {
        await updateCollectionRecipe(editing.id, payload);
      } else {
        await createCollectionRecipe(payload);
      }
      setEditing(null);
      await refresh();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to save collection");
    } finally {
      setSaving(false);
    }
  };

  const handleRun = async (recipe: CollectionRecipe) => {
    if (!plexReady) {
      setActionError("Plex must be configured before a collection can run.");
      return;
    }
    setActionError(null);
    try {
      await runCollectionRecipe(recipe.id);
      setRunningIds((prev) => new Set(prev).add(recipe.id));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to start run");
    }
  };

  const handleToggle = async (recipe: CollectionRecipe, enabled: boolean) => {
    setActionError(null);
    try {
      await toggleCollectionRecipe(recipe.id, enabled);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to update collection");
    }
  };

  const handleDelete = async (recipe: CollectionRecipe) => {
    if (!window.confirm(`Delete collection recipe "${recipe.name}"? The Plex collection itself is not removed.`)) {
      return;
    }
    setActionError(null);
    try {
      await deleteCollectionRecipe(recipe.id);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Failed to delete collection");
    }
  };

  const plexBanner = sectionsError ? (
    <div className="mb-4 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-4 py-3 text-[14px] text-yellow-300">
      <p>
        Collections sync membership into Plex. Enable Plex and add a URL and token under{" "}
        <span className="font-semibold text-yellow-200">Settings → Media Integrations</span>
        {recipes.length ? ". Existing recipes stay listed, but new collections and runs are disabled until Plex is ready." : "."}
      </p>
      {props.onOpenPlexSettings ? (
        <button
          type="button"
          onClick={props.onOpenPlexSettings}
          className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-yellow-500/40 bg-yellow-500/15 px-3 py-1.5 text-[13px] font-headline uppercase tracking-wider text-yellow-100 hover:bg-yellow-500/25 transition-colors"
        >
          <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
            settings
          </span>
          Open Plex settings
        </button>
      ) : null}
      <p className={`mt-2 text-[12px] ${isLight ? "text-amber-800/80" : "text-yellow-400/70"}`}>{sectionsError}</p>
    </div>
  ) : null;

  if (editing !== null) {
    return (
      <div>
        <div className="flex items-center gap-3 mb-6">
          <button
            type="button"
            onClick={() => {
              setEditing(null);
              setSaveError(null);
            }}
            className={`material-symbols-outlined transition-colors ${theme.iconMuted} ${isLight ? "hover:text-slate-900" : "hover:text-white"}`}
            style={{ fontSize: 22 }}
            title="Back to collections"
          >
            arrow_back
          </button>
          <h1 className={`text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
            {editing === "new" ? "New Collection" : `Edit: ${editing.name}`}
          </h1>
        </div>
        {plexBanner}
        <CollectionEditor
          recipe={editing === "new" ? null : editing}
          sections={sections}
          tmdbConfigured={tmdbConfigured}
          traktConfigured={traktConfigured}
          libraryItems={props.libraryItems}
          libraryLoading={props.libraryLoading}
          onEnsureLibrary={props.onEnsureLibrary}
          accent={props.accent}
          themeMode={props.themeMode}
          saving={saving}
          saveError={saveError}
          onSave={(payload) => void handleSave(payload)}
          onCancel={() => {
            setEditing(null);
            setSaveError(null);
          }}
        />
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <div className="w-2 h-2 rounded-full bg-green-500" />
        <span className={`text-[12px] font-headline uppercase tracking-widest ${theme.iconMuted}`}>
          Rule-Based Plex Collections
        </span>
      </div>
      <div className="flex justify-between items-center mb-6">
        <h1 className={`text-[32px] font-black tracking-tight font-headline flex items-center gap-3 ${isLight ? "text-slate-900" : "text-white"}`}>
          Collections
          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold font-headline uppercase bg-orange-600/30 text-orange-300">
            Beta
          </span>
        </h1>
        <button
          type="button"
          disabled={!plexReady}
          title={!plexReady ? "Configure Plex in Settings before creating a collection" : undefined}
          onClick={() => {
            if (!plexReady) return;
            setSaveError(null);
            setEditing("new");
          }}
          className="flex items-center gap-1.5 rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider text-[#0a0e14] disabled:opacity-40 disabled:cursor-not-allowed"
          style={{ backgroundColor: accentHex }}
        >
          <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
            add
          </span>
          New Collection
        </button>
      </div>

      {plexBanner}
      {!tmdbConfigured ? (
        <div className="mb-4 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-4 py-2.5 text-[14px] text-yellow-300">
          No TMDB API key configured. TMDB sources (Trending, Popular, Discover…) are disabled — add a key under
          Settings → Media Integrations to enable them. Catalog-based collections still work.
        </div>
      ) : null}
      {actionError ? (
        <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-[14px] text-red-300">
          {actionError}
        </div>
      ) : null}
      {loadError ? (
        <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-[14px] text-red-300">
          {loadError}
        </div>
      ) : null}

      <div className={`${theme.blockCard} overflow-hidden`}>
        {loading ? (
          <div className={`p-10 text-center text-[16px] ${theme.muted}`}>Loading collections…</div>
        ) : !recipes.length ? (
          <div className="p-12 text-center">
            <span className={`material-symbols-outlined ${theme.iconMuted}`} style={{ fontSize: 44 }}>
              video_library
            </span>
            <p className={`mt-3 text-[16px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>No collections yet.</p>
            <p className={`mt-1 text-[14px] max-w-md mx-auto ${theme.muted}`}>
              Build rule-based Plex collections from TMDB trending, streaming services, or your own catalog metadata —
              they stay in sync automatically.
            </p>
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className={`border-b ${theme.divider}`}>
                {["Collection", "Target Library", "Items", "Schedule", "Last Run", "Enabled", "Actions"].map((h) => (
                  <th
                    key={h}
                    className={`px-5 py-3 text-left text-[12px] font-headline uppercase tracking-widest font-normal ${theme.muted}`}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className={isLight ? "divide-y divide-slate-200/80" : "divide-y divide-[#424753]/15"}>
              {recipes.map((recipe) => {
                const summary = recipe.last_run_summary;
                const running = runningIds.has(recipe.id);
                return (
                  <tr
                    key={recipe.id}
                    className={isLight ? "hover:bg-[#f2f7ff] transition-colors" : "hover:bg-[#1e2430]/40 transition-colors"}
                  >
                    <td className="px-5 py-4">
                      <button
                        type="button"
                        onClick={() => {
                          setSaveError(null);
                          setEditing(recipe);
                        }}
                        className="text-left"
                      >
                        <span
                          className={`block text-[16px] transition-colors ${isLight ? "text-slate-900 hover:text-sky-800" : "text-slate-200 hover:text-white"}`}
                        >
                          {recipe.name}
                        </span>
                        <span className={`block text-[13px] ${theme.muted}`}>→ "{recipe.collection_title}"</span>
                      </button>
                      {recipe.active_window && !recipe.window_active ? (
                        <span
                          className="mt-1 inline-flex items-center gap-1 rounded-full border border-slate-500/40 px-2 py-0.5 text-[11px] font-headline uppercase tracking-wider text-slate-400"
                          title={`Seasonal window ${recipe.active_window.start} → ${recipe.active_window.end}; dormant until it reopens`}
                        >
                          <span className="material-symbols-outlined" style={{ fontSize: 12 }}>
                            bedtime
                          </span>
                          Dormant
                        </span>
                      ) : null}
                    </td>
                    <td className={`px-5 py-4 text-[15px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
                      {(() => {
                        const ids = recipe.plex_section_ids?.length ? recipe.plex_section_ids : [recipe.plex_section_id];
                        const names = ids.map((id) => sections.find((s) => s.id === id)?.title ?? `Section ${id}`);
                        return names.join(", ");
                      })()}
                      <span className={`ml-1.5 text-[12px] uppercase ${isLight ? "text-slate-400" : "text-slate-600"}`}>
                        {recipe.plex_section_type === "movie" ? "Movies" : "TV"}
                      </span>
                    </td>
                    <td className={`px-5 py-4 text-[15px] font-mono ${isLight ? "text-slate-800" : "text-slate-300"}`}>
                      {summary?.synced ? summary.synced.total : "—"}
                    </td>
                    <td className={`px-5 py-4 text-[14px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
                      {formatSchedule(recipe)}
                      {recipe.active_window ? (
                        <span className={`block text-[12px] ${theme.muted}`}>
                          {recipe.active_window.start} → {recipe.active_window.end}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-5 py-4">
                      <span className={`block text-[14px] ${isLight ? "text-slate-600" : "text-slate-400"}`}>
                        {running ? "Running…" : formatLastRun(recipe)}
                      </span>
                      {summary ? (
                        summary.status === "ok" ? (
                          <>
                            <span className="text-[12px] text-green-400/90">
                              +{summary.synced?.added ?? 0} / -{summary.synced?.removed ?? 0}
                            </span>
                            {(summary.missing_from_arr_count ?? 0) > 0 ? (
                              <span
                                className={`block text-[12px] ${theme.muted}`}
                                title={`Source list titles not in ${recipe.plex_section_type === "show" ? "Sonarr" : "Radarr"} (last run). +N are titles that were not in that set last time.`}
                              >
                                {summary.missing_from_arr_count} not in{" "}
                                {recipe.plex_section_type === "show" ? "Sonarr" : "Radarr"}
                                {summary.missing_from_arr_new != null && summary.missing_from_arr_new > 0
                                  ? ` · +${summary.missing_from_arr_new} new`
                                  : ""}
                              </span>
                            ) : null}
                          </>
                        ) : summary.status === "cleared" ? (
                          <span className={`text-[12px] ${theme.muted}`}>Cleared (out of window)</span>
                        ) : (
                          <span className="text-[12px] text-red-400" title={summary.error}>
                            Failed
                          </span>
                        )
                      ) : null}
                    </td>
                    <td className="px-5 py-4">
                      <ToggleSwitch
                        checked={recipe.enabled}
                        onChange={(enabled) => void handleToggle(recipe, enabled)}
                        accentHex={accentHex}
                        size="sm"
                        ariaLabel={recipe.enabled ? "Disable scheduled runs" : "Enable scheduled runs"}
                      />
                    </td>
                    <td className="px-5 py-4">
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          disabled={running || !plexReady}
                          onClick={() => void handleRun(recipe)}
                          className={`material-symbols-outlined rounded-md p-1.5 transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${theme.iconMuted} ${isLight ? "hover:text-slate-900 hover:bg-slate-100" : "hover:text-white hover:bg-[#1e2430]"}`}
                          style={{ fontSize: 18 }}
                          title={!plexReady ? "Configure Plex before running" : "Run now"}
                        >
                          play_arrow
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setSaveError(null);
                            setEditing(recipe);
                          }}
                          className={`material-symbols-outlined rounded-md p-1.5 transition-colors ${theme.iconMuted} ${isLight ? "hover:text-slate-900 hover:bg-slate-100" : "hover:text-white hover:bg-[#1e2430]"}`}
                          style={{ fontSize: 18 }}
                          title="Edit"
                        >
                          edit
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleDelete(recipe)}
                          className={`material-symbols-outlined rounded-md p-1.5 transition-colors ${theme.iconMuted} ${isLight ? "hover:text-red-600 hover:bg-red-50" : "hover:text-red-400 hover:bg-[#1e2430]"}`}
                          style={{ fontSize: 18 }}
                          title="Delete"
                        >
                          delete
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <p className={`mt-4 text-[13px] ${theme.muted}`}>
        Enabled collections run automatically on the collections sync schedule (Settings → Library sync). Collections
        only include titles already present in the target Plex library — for a placeholder library that means anything
        Placeholdarr materialized; rules refine by metadata, not placeholder state.
      </p>

    </div>
  );
}
