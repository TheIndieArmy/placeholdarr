import { useCallback, useEffect, useRef, useState } from "react";
import type { ThemeMode } from "../brandTypes";
import {
  checkCollectionTitleConflicts,
  createCollectionRecipe,
  deleteCollectionRecipe,
  exportCollectionRecipes,
  getCollectionPlexSections,
  getCollectionRecipes,
  importCollectionRecipes,
  runCollectionRecipe,
  toggleCollectionRecipe,
  updateCollectionRecipe,
  type CollectionTitleConflict,
  type RecipeWritePayload,
} from "../api/collections";
import type { CollectionRecipe, LibraryItem, PlexSectionOption } from "../types/api";
import { ConfirmModal } from "../ConfirmModal";
import { ToggleSwitch } from "../ToggleSwitch";
import { CollectionEditor } from "./CollectionEditor";
import { CollectionSetEditor, isCollectionSetRecipe } from "./CollectionSetEditor";
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
  /** True while the recipe editor has unsaved edits (for sidebar leave prompts). */
  onDraftDirty?: (dirty: boolean) => void;
}) {
  const accentHex = props.accent.hex;
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);

  const [recipes, setRecipes] = useState<CollectionRecipe[]>([]);
  const [tmdbConfigured, setTmdbConfigured] = useState(true);
  const [traktConfigured, setTraktConfigured] = useState(true);
  const [tautulliConfigured, setTautulliConfigured] = useState(false);
  const [sections, setSections] = useState<PlexSectionOption[]>([]);
  const [sectionsError, setSectionsError] = useState<string | null>(null);
  const [sectionsLoaded, setSectionsLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // null = list view; "new" = creating recipe; "new-set" = creating collection set; recipe = editing
  const [editing, setEditing] = useState<CollectionRecipe | "new" | "new-set" | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [leaveConfirmOpen, setLeaveConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [titleConflict, setTitleConflict] = useState<{
    payload: RecipeWritePayload;
    conflicts: CollectionTitleConflict[];
  } | null>(null);
  const [runningIds, setRunningIds] = useState<Set<number>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [ioBusy, setIoBusy] = useState(false);
  const [ioMessage, setIoMessage] = useState<string | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);

  const plexReady = sectionsLoaded && !sectionsError;

  const handleEditorDirty = useCallback((dirty: boolean) => {
    setEditorDirty(dirty);
  }, []);

  useEffect(() => {
    props.onDraftDirty?.(editing !== null && editorDirty);
  }, [editing, editorDirty, props.onDraftDirty]);

  useEffect(() => {
    return () => props.onDraftDirty?.(false);
  }, [props.onDraftDirty]);

  const discardEditor = () => {
    setLeaveConfirmOpen(false);
    setEditing(null);
    setSaveError(null);
    setEditorDirty(false);
  };

  const leaveEditor = () => {
    if (editorDirty) {
      setLeaveConfirmOpen(true);
      return;
    }
    discardEditor();
  };

  const refresh = useCallback(async () => {
    try {
      const payload = await getCollectionRecipes();
      setRecipes(payload.recipes);
      setTmdbConfigured(payload.tmdb_configured);
      setTraktConfigured(payload.trakt_configured);
      setTautulliConfigured(Boolean(payload.tautulli_configured));
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

  useEffect(() => {
    setSelectedIds((prev) => {
      const valid = new Set(recipes.map((r) => r.id));
      const next = new Set<number>();
      for (const id of prev) {
        if (valid.has(id)) next.add(id);
      }
      return next;
    });
  }, [recipes]);

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

  const persistRecipe = async (payload: RecipeWritePayload) => {
    if (editing && editing !== "new" && editing !== "new-set") {
      await updateCollectionRecipe(editing.id, payload);
    } else {
      await createCollectionRecipe(payload);
    }
    setEditing(null);
    setTitleConflict(null);
    await refresh();
  };

  const handleSave = async (payload: RecipeWritePayload) => {
    setSaving(true);
    setSaveError(null);
    try {
      if (payload.definition.adopt_existing) {
        await persistRecipe(payload);
        return;
      }
      const recipeId = editing && editing !== "new" && editing !== "new-set" ? editing.id : null;
      const result = await checkCollectionTitleConflicts({
        plex_section_id: payload.plex_section_id,
        plex_section_ids: payload.plex_section_ids,
        plex_section_type: payload.plex_section_type,
        collection_title: payload.collection_title,
        definition: payload.definition,
        recipe_id: recipeId,
      });
      const blocking = (result.conflicts || []).filter((c) => c.reason !== "ours");
      if (blocking.length > 0) {
        setTitleConflict({ payload, conflicts: blocking });
        return;
      }
      await persistRecipe(payload);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to save collection");
    } finally {
      setSaving(false);
    }
  };

  const handleAdoptConflict = async () => {
    if (!titleConflict) return;
    setSaving(true);
    setSaveError(null);
    try {
      await persistRecipe({
        ...titleConflict.payload,
        definition: { ...titleConflict.payload.definition, adopt_existing: true },
      });
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

  const toggleSelected = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allSelected = recipes.length > 0 && selectedIds.size === recipes.length;

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds(new Set());
      return;
    }
    setSelectedIds(new Set(recipes.map((r) => r.id)));
  };

  const handleExportSelected = async () => {
    if (!selectedIds.size) {
      setActionError("Select at least one collection to export");
      return;
    }
    setActionError(null);
    setIoMessage(null);
    setIoBusy(true);
    try {
      const bundle = await exportCollectionRecipes([...selectedIds]);
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const stamp = new Date().toISOString().slice(0, 10);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `placeholdarr-collections-${stamp}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setIoMessage(`Exported ${bundle.recipes.length} collection${bundle.recipes.length === 1 ? "" : "s"}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setIoBusy(false);
    }
  };

  const handleImportFile = async (file: File) => {
    setActionError(null);
    setIoMessage(null);
    setIoBusy(true);
    try {
      const text = await file.text();
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new Error("Import file is not valid JSON");
      }
      const sectionIds = sections.map((s) => s.id);
      const result = await importCollectionRecipes({
        payload: parsed,
        plex_section_ids: sectionIds.length ? sectionIds : null,
      });
      await refresh();
      const parts = [`Imported ${result.created_count} collection${result.created_count === 1 ? "" : "s"}`];
      if (result.errors.length) {
        parts.push(`${result.errors.length} failed`);
        setActionError(result.errors.map((e) => `${e.name}: ${e.error}`).join(" · "));
      }
      setIoMessage(parts.join(" · "));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setIoBusy(false);
      if (importInputRef.current) importInputRef.current.value = "";
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
    const setMode =
      editing === "new-set" || (editing !== "new" && isCollectionSetRecipe(editing));
    return (
      <div>
        {leaveConfirmOpen ? (
          <ConfirmModal
            title="Leave without saving?"
            message="You have an unsaved collection recipe. If you leave now, this draft will be lost."
            confirmLabel="Leave"
            cancelLabel="Stay"
            accentHex={accentHex}
            themeMode={props.themeMode}
            onCancel={() => setLeaveConfirmOpen(false)}
            onConfirm={discardEditor}
          />
        ) : null}
        {titleConflict ? (
          <div className="fixed inset-0 z-[85] flex items-center justify-center bg-[#0f1419]/85 backdrop-blur-sm p-6">
            <div
              className={`w-full max-w-lg rounded-2xl border p-6 shadow-2xl space-y-4 ${
                isLight ? "border-slate-200 bg-white" : "border-[#424753]/40 bg-[#171c22]"
              }`}
            >
              <h3 className={`text-[20px] font-headline font-bold ${isLight ? "text-slate-900" : "text-white"}`}>
                {titleConflict.conflicts.some((c) => c.reason === "other_recipe")
                  ? "Name used by another recipe"
                  : "Collection name already in use"}
              </h3>
              {titleConflict.conflicts.some((c) => c.reason === "other_recipe") ? (
                <p className={`text-[15px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-300"}`}>
                  Another Placeholdarr recipe already uses this name in a selected library. Change this title, or rename
                  the other recipe&apos;s collection first.
                </p>
              ) : (
                <>
                  <p className={`text-[15px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-300"}`}>
                    A collection with this name already exists in a selected library. Rename this recipe, or adopt to
                    reconnect it with Placeholdarr&apos;s ownership tracking (or take it over if it was not managed
                    before).
                  </p>
                  <p className={`text-[14px] leading-relaxed ${isLight ? "text-slate-600" : "text-slate-400"}`}>
                    Adopting reconnects a previous Placeholdarr collection, or takes over a non-Placeholdarr collection
                    with this name. Items that do not match this recipe&apos;s sources and filters will be removed on
                    sync.
                  </p>
                </>
              )}
              <ul className={`text-[13px] space-y-1.5 max-h-40 overflow-y-auto ${isLight ? "text-slate-700" : "text-slate-300"}`}>
                {titleConflict.conflicts.map((c) => (
                  <li key={`${c.section_id}:${c.title}:${c.rating_key || ""}`}>
                    <span className="font-semibold">{c.title}</span>
                    {" · "}
                    {c.section_title}
                    {c.reason === "other_recipe"
                      ? " (owned by another Placeholdarr recipe)"
                      : ` (${c.item_count} item${c.item_count === 1 ? "" : "s"} in Plex)`}
                  </li>
                ))}
              </ul>
              <div className="flex flex-wrap justify-end gap-2 pt-2">
                <button
                  type="button"
                  className={`px-4 py-2 rounded-lg text-[13px] font-headline uppercase tracking-wider ${
                    isLight ? "text-slate-700 hover:bg-slate-100" : "text-slate-300 hover:bg-white/5"
                  }`}
                  onClick={() => setTitleConflict(null)}
                  disabled={saving}
                >
                  {titleConflict.conflicts.some((c) => c.reason === "other_recipe") ? "OK" : "Rename"}
                </button>
                {titleConflict.conflicts.every((c) => c.reason !== "other_recipe") ? (
                  <button
                    type="button"
                    className="px-4 py-2 rounded-lg text-[13px] font-headline uppercase tracking-wider text-white disabled:opacity-60"
                    style={{ backgroundColor: accentHex }}
                    onClick={() => void handleAdoptConflict()}
                    disabled={saving}
                  >
                    {saving ? "Saving…" : "Adopt and save"}
                  </button>
                ) : null}
              </div>
            </div>
          </div>
        ) : null}
        <div className="flex items-center gap-3 mb-6">
          <button
            type="button"
            onClick={leaveEditor}
            className={`material-symbols-outlined transition-colors ${theme.iconMuted} ${isLight ? "hover:text-slate-900" : "hover:text-white"}`}
            style={{ fontSize: 22 }}
            title="Back to collections"
          >
            arrow_back
          </button>
          <h1 className={`text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
            {editing === "new"
              ? "New Collection"
              : editing === "new-set"
                ? "New Collection Set"
                : `Edit: ${editing.name}`}
          </h1>
        </div>
        {plexBanner}
        {setMode ? (
          <CollectionSetEditor
            recipe={typeof editing === "string" ? null : editing}
            sections={sections}
            accent={props.accent}
            themeMode={props.themeMode}
            saving={saving}
            saveError={saveError}
            onSave={(payload) => void handleSave(payload)}
            onCancel={leaveEditor}
            onDirtyChange={handleEditorDirty}
          />
        ) : (
          <CollectionEditor
            recipe={editing === "new" ? null : editing}
            sections={sections}
            tmdbConfigured={tmdbConfigured}
            traktConfigured={traktConfigured}
            tautulliConfigured={tautulliConfigured}
            libraryItems={props.libraryItems}
            libraryLoading={props.libraryLoading}
            onEnsureLibrary={props.onEnsureLibrary}
            accent={props.accent}
            themeMode={props.themeMode}
            saving={saving}
            saveError={saveError}
            onSave={(payload) => void handleSave(payload)}
            onCancel={leaveEditor}
            onDirtyChange={handleEditorDirty}
          />
        )}
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
        <div className="flex items-center gap-2">
          <input
            ref={importInputRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void handleImportFile(file);
            }}
          />
          <button
            type="button"
            disabled={ioBusy || !plexReady}
            title={!plexReady ? "Configure Plex before importing" : "Import collections from a JSON file"}
            onClick={() => importInputRef.current?.click()}
            className={`flex items-center gap-1.5 rounded-lg border px-3 py-2 text-[14px] font-headline uppercase tracking-wider disabled:opacity-40 disabled:cursor-not-allowed ${
              isLight
                ? "border-slate-200 text-slate-700 hover:bg-slate-50"
                : "border-[#424753]/60 text-slate-200 hover:bg-[#1e2430]"
            }`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
              upload
            </span>
            Import
          </button>
          <button
            type="button"
            disabled={ioBusy || selectedIds.size === 0}
            title={selectedIds.size === 0 ? "Select collections to export" : "Export selected collections as JSON"}
            onClick={() => void handleExportSelected()}
            className={`flex items-center gap-1.5 rounded-lg border px-3 py-2 text-[14px] font-headline uppercase tracking-wider disabled:opacity-40 disabled:cursor-not-allowed ${
              isLight
                ? "border-slate-200 text-slate-700 hover:bg-slate-50"
                : "border-[#424753]/60 text-slate-200 hover:bg-[#1e2430]"
            }`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
              download
            </span>
            Export{selectedIds.size ? ` (${selectedIds.size})` : ""}
          </button>
          <button
            type="button"
            disabled={!plexReady}
            title={!plexReady ? "Configure Plex in Settings before creating a collection" : undefined}
            onClick={() => {
              if (!plexReady) return;
              setSaveError(null);
              setEditing("new-set");
            }}
            className={`flex items-center gap-1.5 rounded-lg border px-4 py-2 text-[14px] font-headline uppercase tracking-wider disabled:opacity-40 disabled:cursor-not-allowed ${
              isLight
                ? "border-slate-200 text-slate-700 hover:bg-slate-50"
                : "border-[#424753]/60 text-slate-200 hover:bg-[#1e2430]"
            }`}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
              category
            </span>
            Collection Set
          </button>
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
      </div>

      {plexBanner}
      {!tmdbConfigured ? (
        <div className="mb-4 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-4 py-2.5 text-[14px] text-yellow-300">
          No TMDB API key configured. TMDB sources (Trending, Popular, Discover, person pages…) are disabled — add a
          key under Settings → Media Integrations to enable them. Catalog, MDBList, StevenLu, and AniList still work.
        </div>
      ) : null}
      {ioMessage ? (
        <div className="mb-4 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-4 py-2.5 text-[14px] text-emerald-300">
          {ioMessage}
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
              Build rule-based Plex collections from your catalog, TMDB, MDBList, Trakt, StevenLu, or AniList — they
              stay in sync automatically.
            </p>
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className={`border-b ${theme.divider}`}>
                {["", "Collection", "Target Library", "Items", "Schedule", "Last Run", "Enabled", "Actions"].map((h) => (
                  <th
                    key={h || "select"}
                    className={`px-5 py-3 text-left text-[12px] font-headline uppercase tracking-widest font-normal ${theme.muted}`}
                  >
                    {h === "" ? (
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleSelectAll}
                        aria-label="Select all collections"
                      />
                    ) : (
                      h
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className={isLight ? "divide-y divide-slate-200/80" : "divide-y divide-[#424753]/15"}>
              {recipes.map((recipe) => {
                const summary = recipe.last_run_summary;
                const running = runningIds.has(recipe.id);
                const selected = selectedIds.has(recipe.id);
                return (
                  <tr
                    key={recipe.id}
                    className={isLight ? "hover:bg-[#f2f7ff] transition-colors" : "hover:bg-[#1e2430]/40 transition-colors"}
                  >
                    <td className="px-5 py-4">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => toggleSelected(recipe.id)}
                        aria-label={`Select ${recipe.name}`}
                      />
                    </td>
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
                        <span className={`block text-[13px] ${theme.muted}`}>
                          {isCollectionSetRecipe(recipe)
                            ? `→ Set · ${
                                recipe.definition.collection_set?.category ??
                                recipe.definition.collection_set?.dimension ??
                                "category"
                              } (${
                                recipe.last_run_summary?.collection_set?.collection_count ??
                                "auto"
                              } collections)`
                            : `→ "${recipe.collection_title}"`}
                        </span>
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
                      {summary?.mode === "collection_set" && summary.collection_set?.collection_count != null
                        ? `${summary.collection_set.collection_count} cols`
                        : summary?.synced
                          ? summary.synced.total
                          : "—"}
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
