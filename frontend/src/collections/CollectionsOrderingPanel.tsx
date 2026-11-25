import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import type { ThemeMode } from "../brandTypes";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import {
  applyCollectionHubs,
  getCollectionHubs,
  getCollectionPlexSections,
  type CollectionHubPromoteCandidate,
  type CollectionHubRow,
} from "../api/collections";
import type { PlexSectionOption } from "../types/api";
import { getCollectionTheme } from "./collectionTheme";

type DraftHub = CollectionHubRow & {
  /** Local visibility overrides before Apply. */
  library: boolean;
  home: boolean;
  shared: boolean;
};

function toDraft(hub: CollectionHubRow): DraftHub {
  return {
    ...hub,
    library: Boolean(hub.promoted_to_recommended),
    home: Boolean(hub.promoted_to_home),
    shared: Boolean(hub.promoted_to_shared),
  };
}

/** Move `from` so the item lands at visual slot `insertBefore` (0..length) in the pre-drag list. */
function reorderToInsertBefore<T>(list: T[], from: number, insertBefore: number): T[] {
  if (from < 0 || from >= list.length) return list;
  let target = Math.max(0, Math.min(insertBefore, list.length));
  const next = [...list];
  const [item] = next.splice(from, 1);
  if (from < target) target -= 1;
  next.splice(target, 0, item);
  return next;
}

export function CollectionsOrderingPanel(props: {
  accent: { hex: string; icon: string };
  themeMode: ThemeMode;
  onOpenPlexSettings?: () => void;
  onOpenRecipes?: () => void;
}) {
  const accentHex = props.accent.hex;
  const isLight = props.themeMode === "light";
  const theme = getCollectionTheme(isLight);

  const [sections, setSections] = useState<PlexSectionOption[]>([]);
  const [sectionsError, setSectionsError] = useState<string | null>(null);
  const [sectionsLoaded, setSectionsLoaded] = useState(false);
  const [sectionId, setSectionId] = useState<number | "">("");
  const [draft, setDraft] = useState<DraftHub[]>([]);
  const [promoteCandidates, setPromoteCandidates] = useState<CollectionHubPromoteCandidate[]>([]);
  const [pendingPromote, setPendingPromote] = useState<string[]>([]);
  const [pendingDemote, setPendingDemote] = useState<string[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyOk, setApplyOk] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [addKey, setAddKey] = useState("");
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  /** Insertion slot in the current list: 0 = before first, length = after last. */
  const [insertBefore, setInsertBefore] = useState<number | null>(null);
  const dragFromRef = useRef<number | null>(null);
  const insertBeforeRef = useRef<number | null>(null);

  const plexReady = sectionsLoaded && sections.length > 0;

  useEffect(() => {
    getCollectionPlexSections()
      .then((payload) => {
        setSections(payload.sections);
        setSectionsError(null);
        const first = payload.sections[0];
        if (first) setSectionId((prev) => (prev === "" ? first.id : prev));
      })
      .catch((err) => {
        setSections([]);
        setSectionsError(err instanceof Error ? err.message : "Plex libraries unavailable");
      })
      .finally(() => setSectionsLoaded(true));
  }, []);

  const refresh = useCallback(async (sid: number) => {
    setLoading(true);
    setLoadError(null);
    setApplyError(null);
    setApplyOk(null);
    try {
      const snap = await getCollectionHubs(sid);
      setDraft((snap.hubs || []).map(toDraft));
      setPromoteCandidates(snap.promote_candidates || []);
      setPendingPromote([]);
      setPendingDemote([]);
      setDirty(false);
      setAddKey("");
    } catch (err) {
      setDraft([]);
      setPromoteCandidates([]);
      setLoadError(err instanceof Error ? err.message : "Failed to load hubs from Plex");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (sectionId === "") return;
    const sid = Number(sectionId);
    if (!Number.isFinite(sid) || sid < 1) return;
    void refresh(sid);
  }, [sectionId, refresh]);

  const availablePromote = useMemo(() => {
    const pending = new Set(pendingPromote);
    return promoteCandidates.filter((c) => !pending.has(c.rating_key));
  }, [promoteCandidates, pendingPromote]);

  const finishDrag = () => {
    dragFromRef.current = null;
    insertBeforeRef.current = null;
    setDragFrom(null);
    setInsertBefore(null);
  };

  const setInsertSlot = (slot: number) => {
    insertBeforeRef.current = slot;
    setInsertBefore((prev) => (prev === slot ? prev : slot));
  };

  const onHubDragStart = (index: number, event: DragEvent) => {
    dragFromRef.current = index;
    setDragFrom(index);
    setInsertSlot(index);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(index));
    const row = (event.currentTarget as HTMLElement).closest("li");
    if (row instanceof HTMLElement) {
      try {
        event.dataTransfer.setDragImage(row, 28, Math.min(24, row.offsetHeight / 2));
      } catch {
        /* browser may reject custom drag images */
      }
    }
  };

  const onHubDragOverRow = (index: number, event: DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const before = event.clientY < rect.top + rect.height / 2;
    setInsertSlot(before ? index : index + 1);
  };

  const onHubDrop = (event: DragEvent) => {
    event.preventDefault();
    const from = dragFromRef.current;
    const slot = insertBeforeRef.current;
    finishDrag();
    if (from == null || slot == null) return;
    if (slot === from || slot === from + 1) return;
    setDraft((prev) => reorderToInsertBefore(prev, from, slot));
    setDirty(true);
    setApplyOk(null);
  };

  const setVis = (identifier: string, key: "library" | "home" | "shared", value: boolean) => {
    setDraft((prev) =>
      prev.map((row) => (row.identifier === identifier ? { ...row, [key]: value } : row)),
    );
    setDirty(true);
    setApplyOk(null);
  };

  const queuePromote = (ratingKey: string) => {
    if (!ratingKey) return;
    setPendingPromote((prev) => (prev.includes(ratingKey) ? prev : [...prev, ratingKey]));
    setDirty(true);
    setApplyOk(null);
    setAddKey("");
  };

  const queueDemote = (identifier: string) => {
    setPendingDemote((prev) => (prev.includes(identifier) ? prev : [...prev, identifier]));
    setDraft((prev) => prev.filter((r) => r.identifier !== identifier));
    setDirty(true);
    setApplyOk(null);
  };

  const handleApply = async () => {
    if (sectionId === "") return;
    setApplying(true);
    setApplyError(null);
    setApplyOk(null);
    try {
      const visibility = draft.map((row) => ({
        identifier: row.identifier,
        library: row.library,
        home: row.home,
        shared: row.shared,
      }));
      const result = await applyCollectionHubs({
        plex_section_id: Number(sectionId),
        ordered_identifiers: draft.map((r) => r.identifier),
        visibility,
        promote_rating_keys: pendingPromote,
        demote_identifiers: pendingDemote,
      });
      if (result.hubs) setDraft(result.hubs.map(toDraft));
      if (result.promote_candidates) setPromoteCandidates(result.promote_candidates);
      setPendingPromote([]);
      setPendingDemote([]);
      setDirty(false);
      if (result.errors?.length) {
        setApplyError(result.errors.join("; "));
      } else if (!result.ok) {
        setApplyError(result.reorder?.error || "Apply failed");
      } else {
        setApplyOk("Applied to Plex. Collections sync will not change this order or visibility.");
      }
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  };

  if (sectionsLoaded && !plexReady) {
    return (
      <div>
        <h1 className={`mb-2 text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
          Ordering
        </h1>
        <p className={`mb-6 text-[14px] ${theme.muted}`}>
          Reorder Plex Recommended collections and set Visible On for any collection in a library.
        </p>
        <div className={`${theme.blockCard} overflow-hidden p-12 text-center`}>
          <p className={`text-[18px] font-headline ${isLight ? "text-slate-900" : "text-white"}`}>Plex required</p>
          <p className={`mt-2 text-[14px] ${theme.muted}`}>
            Connect Plex under Settings → Media Integrations. Admin Plex Pass is required for hub management.
          </p>
          {props.onOpenPlexSettings ? (
            <button
              type="button"
              onClick={props.onOpenPlexSettings}
              className={`mt-5 inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
              style={accentFilledStyle(accentHex)}
            >
              Connect Plex
            </button>
          ) : null}
          {sectionsError ? <p className={`mt-4 text-[12px] ${theme.muted}`}>{sectionsError}</p> : null}
        </div>
      </div>
    );
  }

  return (
    <div className="w-full min-w-[max(100%,40rem)]">
      <div className="mb-1 flex items-center gap-2">
        <div className="w-2 h-2 rounded-full bg-green-500" />
        <span className={`text-[12px] font-headline uppercase tracking-widest ${theme.iconMuted}`}>
          Plex Recommended hubs
        </span>
      </div>
      <h1 className={`mb-2 text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
        Ordering
      </h1>
      <p className={`mb-6 max-w-2xl text-[14px] leading-relaxed ${theme.muted}`}>
        Live order and visibility from Plex for any library collection (Placeholdarr or not). Drag rows with the
        grabber, then Apply. Recipe sync never overwrites this. Create-time Visible On still lives on Recipes.
      </p>

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className={`flex flex-col gap-1 text-[13px] ${theme.label}`}>
          Library
          <BrandSelect
            triggerClassName="min-w-[14rem]"
            value={sectionId === "" ? "" : String(sectionId)}
            options={sections.map((s) => ({
              value: String(s.id),
              label: `${s.title} (${s.type === "show" ? "TV" : "Movie"})`,
            }))}
            onChange={(value) => setSectionId(value ? Number(value) : "")}
            ariaLabel="Plex library"
          />
        </label>
        <button
          type="button"
          disabled={sectionId === "" || loading}
          onClick={() => sectionId !== "" && void refresh(Number(sectionId))}
          className={`rounded-lg border px-3 py-2 text-[13px] font-headline uppercase tracking-wider disabled:opacity-40 ${
            isLight ? "border-slate-200 text-slate-700" : "border-[#424753]/60 text-slate-200"
          }`}
        >
          Refresh from Plex
        </button>
        <button
          type="button"
          disabled={!dirty || applying || sectionId === ""}
          onClick={() => void handleApply()}
          className={`rounded-lg px-4 py-2 text-[13px] font-headline uppercase tracking-wider disabled:opacity-40 ${FG_ON_ACCENT_TEXT_CLASS}`}
          style={accentFilledStyle(accentHex)}
        >
          {applying ? "Applying…" : "Apply to Plex"}
        </button>
        {props.onOpenRecipes ? (
          <button
            type="button"
            onClick={props.onOpenRecipes}
            className={`rounded-lg border px-3 py-2 text-[13px] ${
              isLight ? "border-slate-200 text-slate-600" : "border-[#424753]/60 text-slate-300"
            }`}
          >
            Recipes
          </button>
        ) : null}
      </div>

      {loadError ? (
        <p className={`mb-4 text-[13px] ${isLight ? "text-amber-800" : "text-amber-200"}`}>{loadError}</p>
      ) : null}
      {applyError ? (
        <p className={`mb-4 text-[13px] ${isLight ? "text-red-700" : "text-red-300"}`}>{applyError}</p>
      ) : null}
      {applyOk ? (
        <p className={`mb-4 text-[13px] ${isLight ? "text-emerald-700" : "text-emerald-300"}`}>{applyOk}</p>
      ) : null}

      <div className={`${theme.blockCard} relative z-20 mb-4 space-y-3 p-4`}>
        <div className={theme.blockTitle}>Add collection to hubs</div>
        <p className={`text-[13px] ${theme.muted}`}>
          Promotes onto Library Recommended (Home / Shared stay off until you toggle them). Requires Plex Pass.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <BrandSelect
            triggerClassName="min-w-[16rem]"
            value={addKey}
            options={[
              { value: "", label: availablePromote.length ? "Choose a collection…" : "No collections left to add" },
              ...availablePromote.map((c) => ({
                value: c.rating_key,
                label: `${c.title}${c.placeholdarr_owned ? " · Placeholdarr" : ""}`,
              })),
            ]}
            onChange={setAddKey}
            ariaLabel="Collection to promote"
          />
          <button
            type="button"
            disabled={!addKey}
            onClick={() => queuePromote(addKey)}
            className={`rounded-lg border px-3 py-2 text-[13px] disabled:opacity-40 ${
              isLight ? "border-slate-200" : "border-[#424753]"
            }`}
          >
            Queue add
          </button>
        </div>
        {pendingPromote.length ? (
          <ul className={`text-[13px] ${theme.muted} list-disc pl-5`}>
            {pendingPromote.map((rk) => {
              const row = promoteCandidates.find((c) => c.rating_key === rk);
              return <li key={rk}>Will promote: {row?.title || rk}</li>;
            })}
          </ul>
        ) : null}
      </div>

      <div className={`${theme.blockCard} overflow-hidden`}>
        {loading ? (
          <p className={`p-6 text-[14px] ${theme.muted}`}>Loading hubs…</p>
        ) : draft.length === 0 ? (
          <p className={`p-6 text-[14px] ${theme.muted}`}>
            No managed hubs on this library yet. Add a collection above, or create one from Recipes with Visible On.
          </p>
        ) : (
          <ul
            className="divide-y divide-[#424753]/25"
            onDragOver={(e) => {
              // Allow dropping in the list gutters / after the last row.
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
            }}
            onDrop={onHubDrop}
          >
            {draft.map((row, index) => {
              const isDragging = dragFrom === index;
              const slotIsNoop =
                dragFrom != null && (insertBefore === dragFrom || insertBefore === dragFrom + 1);
              const showTopLine = dragFrom != null && insertBefore === index && !slotIsNoop;
              const showBottomLine =
                dragFrom != null &&
                index === draft.length - 1 &&
                insertBefore === draft.length &&
                !slotIsNoop;
              return (
                <li
                  key={row.identifier}
                  onDragOver={(e) => onHubDragOverRow(index, e)}
                  onDrop={onHubDrop}
                  onDragEnd={finishDrag}
                  className={[
                    "relative flex flex-col gap-3 px-3 py-3 sm:flex-row sm:items-center sm:justify-between",
                    isDragging ? "opacity-40" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {showTopLine ? (
                    <div
                      className="pointer-events-none absolute left-3 right-3 top-0 z-10 h-0.5 -translate-y-1/2 rounded-full"
                      style={{ backgroundColor: accentHex }}
                      aria-hidden
                    />
                  ) : null}
                  {showBottomLine ? (
                    <div
                      className="pointer-events-none absolute bottom-0 left-3 right-3 z-10 h-0.5 translate-y-1/2 rounded-full"
                      style={{ backgroundColor: accentHex }}
                      aria-hidden
                    />
                  ) : null}
                  <div className="flex min-w-0 flex-1 items-start gap-2">
                    <button
                      type="button"
                      draggable
                      aria-label={`Reorder ${row.title || "hub"}`}
                      title="Drag to reorder"
                      onDragStart={(e) => onHubDragStart(index, e)}
                      onDragEnd={finishDrag}
                      className={`mt-0.5 shrink-0 cursor-grab touch-none rounded px-0.5 py-1 text-slate-400 hover:text-slate-200 active:cursor-grabbing ${
                        isLight ? "hover:bg-slate-100" : "hover:bg-[#1e2430]"
                      }`}
                    >
                      <span className="material-symbols-outlined leading-none" style={{ fontSize: 20 }} aria-hidden>
                        drag_indicator
                      </span>
                    </button>
                    <div className="min-w-0 flex-1">
                      <div className={`truncate text-[15px] font-medium ${isLight ? "text-slate-900" : "text-white"}`}>
                        {row.title || row.identifier}
                      </div>
                      <div className={`mt-0.5 text-[12px] ${theme.muted}`}>
                        {row.placeholdarr_owned
                          ? row.recipe_name
                            ? `Placeholdarr · ${row.recipe_name}`
                            : "Placeholdarr"
                          : "Plex collection"}
                        {pendingDemote.includes(row.identifier) ? " · queued remove" : ""}
                      </div>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-3 pl-8 sm:pl-0">
                    <label className={`flex items-center gap-1.5 text-[12px] ${theme.label}`}>
                      <BrandCheckbox
                        checked={row.library}
                        size="sm"
                        accentHex={accentHex}
                        themeMode={props.themeMode}
                        ariaLabel="Library Recommended"
                        onChange={(v) => setVis(row.identifier, "library", v)}
                      />
                      Recommended
                    </label>
                    <label className={`flex items-center gap-1.5 text-[12px] ${theme.label}`}>
                      <BrandCheckbox
                        checked={row.home}
                        size="sm"
                        accentHex={accentHex}
                        themeMode={props.themeMode}
                        ariaLabel="Home"
                        onChange={(v) => setVis(row.identifier, "home", v)}
                      />
                      Home
                    </label>
                    <label className={`flex items-center gap-1.5 text-[12px] ${theme.label}`}>
                      <BrandCheckbox
                        checked={row.shared}
                        size="sm"
                        accentHex={accentHex}
                        themeMode={props.themeMode}
                        ariaLabel="Shared Home"
                        onChange={(v) => setVis(row.identifier, "shared", v)}
                      />
                      Shared
                    </label>
                    <button
                      type="button"
                      disabled={!row.deletable}
                      title={row.deletable ? "Remove from hubs" : "This hub cannot be removed"}
                      aria-label="Remove from hubs"
                      onClick={() => queueDemote(row.identifier)}
                      className={`inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border transition disabled:opacity-30 ${
                        isLight
                          ? "border-red-300/50 bg-transparent text-red-600 hover:border-red-400 hover:bg-red-50 hover:text-red-700"
                          : "border-red-400/20 bg-transparent text-red-400 hover:border-red-400/35 hover:bg-red-500/10 hover:text-red-300"
                      }`}
                    >
                      <span className="material-symbols-outlined leading-none" style={{ fontSize: 20 }} aria-hidden>
                        delete
                      </span>
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
