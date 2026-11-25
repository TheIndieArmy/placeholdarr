import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import {
  applyCollectionHubs,
  getCollectionHubs,
  getCollectionPlexSections,
  type CollectionHubPromoteCandidate,
  type CollectionHubRow,
} from "../../api/collections";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";
import type { ThemeMode } from "../../brandTypes";
import type { PlexSectionOption } from "../../types/api";
import { getCollectionTheme } from "../collectionTheme";
import { StickyActionBar } from "./StickyActionBar";

type VisKey = "library" | "home" | "shared";

type DraftHub = CollectionHubRow & {
  library: boolean;
  home: boolean;
  shared: boolean;
  removed: boolean;
};

const VIS_PILLS: { key: VisKey; label: string; hint: string }[] = [
  { key: "library", label: "Recommended", hint: "Library Recommended tab" },
  { key: "home", label: "Home", hint: "Your Plex Home" },
  { key: "shared", label: "Shared", hint: "Shared users' Home" },
];

function toDraft(hub: CollectionHubRow): DraftHub {
  return {
    ...hub,
    library: Boolean(hub.promoted_to_recommended),
    home: Boolean(hub.promoted_to_home),
    shared: Boolean(hub.promoted_to_shared),
    removed: false,
  };
}

function reorderToInsertBefore<T>(list: T[], from: number, insertBefore: number): T[] {
  if (from < 0 || from >= list.length) return list;
  let target = Math.max(0, Math.min(insertBefore, list.length));
  const next = [...list];
  const [item] = next.splice(from, 1);
  if (from < target) target -= 1;
  next.splice(target, 0, item);
  return next;
}

export function OrderingNext(props: {
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
  const [sectionId, setSectionId] = useState<number | null>(null);
  const [original, setOriginal] = useState<DraftHub[]>([]);
  const [draft, setDraft] = useState<DraftHub[]>([]);
  const [candidates, setCandidates] = useState<CollectionHubPromoteCandidate[]>([]);
  const [pendingAdd, setPendingAdd] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyOk, setApplyOk] = useState<string | null>(null);
  const [addQuery, setAddQuery] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [insertBefore, setInsertBefore] = useState<number | null>(null);
  const dragFromRef = useRef<number | null>(null);
  const insertBeforeRef = useRef<number | null>(null);
  const addBoxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getCollectionPlexSections()
      .then((payload) => {
        setSections(payload.sections);
        setSectionsError(null);
        if (payload.sections[0]) setSectionId((prev) => prev ?? payload.sections[0].id);
      })
      .catch((err) => {
        setSections([]);
        setSectionsError(err instanceof Error ? err.message : "Plex libraries unavailable");
      })
      .finally(() => setSectionsLoaded(true));
  }, []);

  const loadSnapshot = useCallback((hubs: CollectionHubRow[], promote: CollectionHubPromoteCandidate[]) => {
    const rows = hubs.map(toDraft);
    setOriginal(rows);
    setDraft(rows);
    setCandidates(promote);
    setPendingAdd([]);
  }, []);

  const refresh = useCallback(
    async (sid: number) => {
      setLoading(true);
      setLoadError(null);
      setApplyError(null);
      setApplyOk(null);
      try {
        const snap = await getCollectionHubs(sid);
        loadSnapshot(snap.hubs || [], snap.promote_candidates || []);
      } catch (err) {
        loadSnapshot([], []);
        setLoadError(err instanceof Error ? err.message : "Failed to load hubs from Plex");
      } finally {
        setLoading(false);
      }
    },
    [loadSnapshot],
  );

  useEffect(() => {
    if (sectionId != null) void refresh(sectionId);
  }, [sectionId, refresh]);

  useEffect(() => {
    if (!addOpen) return;
    const onDown = (event: MouseEvent) => {
      if (!addBoxRef.current?.contains(event.target as Node)) setAddOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [addOpen]);

  const pending = useMemo(() => {
    const originalById = new Map(original.map((row) => [row.identifier, row]));
    const keptOriginal = original.filter((o) => draft.some((d) => d.identifier === o.identifier && !d.removed));
    const keptDraft = draft.filter((d) => !d.removed);
    const moved = new Set<string>();
    keptDraft.forEach((row, index) => {
      if (keptOriginal[index]?.identifier !== row.identifier) moved.add(row.identifier);
    });
    const visibility = new Set<string>();
    for (const row of keptDraft) {
      const before = originalById.get(row.identifier);
      if (before && (before.library !== row.library || before.home !== row.home || before.shared !== row.shared)) {
        visibility.add(row.identifier);
      }
    }
    const removed = draft.filter((d) => d.removed).map((d) => d.identifier);
    return { moved, visibility, removed, add: pendingAdd.length };
  }, [original, draft, pendingAdd]);

  const dirty = pending.moved.size > 0 || pending.visibility.size > 0 || pending.removed.length > 0 || pending.add > 0;

  const switchSection = (id: number) => {
    if (id === sectionId) return;
    if (dirty && !window.confirm("Discard pending changes for this library?")) return;
    setSectionId(id);
  };

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
  const onDragStart = (index: number, event: DragEvent) => {
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
  const onDragOverRow = (index: number, event: DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    setInsertSlot(event.clientY < rect.top + rect.height / 2 ? index : index + 1);
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    const from = dragFromRef.current;
    const slot = insertBeforeRef.current;
    finishDrag();
    if (from == null || slot == null || slot === from || slot === from + 1) return;
    setDraft((prev) => reorderToInsertBefore(prev, from, slot));
    setApplyOk(null);
  };
  const move = (index: number, delta: -1 | 1) => {
    const target = index + delta;
    if (target < 0 || target >= draft.length) return;
    setDraft((prev) => reorderToInsertBefore(prev, index, delta > 0 ? target + 1 : target));
    setApplyOk(null);
  };

  const toggleVis = (identifier: string, key: VisKey) => {
    setDraft((prev) => prev.map((row) => (row.identifier === identifier ? { ...row, [key]: !row[key] } : row)));
    setApplyOk(null);
  };
  const setRemoved = (identifier: string, removed: boolean) => {
    setDraft((prev) => prev.map((row) => (row.identifier === identifier ? { ...row, removed } : row)));
    setApplyOk(null);
  };

  const discard = () => {
    setDraft(original);
    setPendingAdd([]);
    setApplyError(null);
  };

  const handleApply = async () => {
    if (sectionId == null) return;
    setApplying(true);
    setApplyError(null);
    setApplyOk(null);
    try {
      const kept = draft.filter((row) => !row.removed);
      const result = await applyCollectionHubs({
        plex_section_id: sectionId,
        ordered_identifiers: kept.map((r) => r.identifier),
        visibility: kept.map((row) => ({
          identifier: row.identifier,
          library: row.library,
          home: row.home,
          shared: row.shared,
        })),
        promote_rating_keys: pendingAdd,
        demote_identifiers: pending.removed,
      });
      if (result.hubs) loadSnapshot(result.hubs, result.promote_candidates ?? candidates);
      else await refresh(sectionId);
      if (result.errors?.length) setApplyError(result.errors.join("; "));
      else if (!result.ok) setApplyError(result.reorder?.error || "Apply failed");
      else setApplyOk("Applied to Plex. Collection sync never changes this order or visibility.");
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  };

  const addMatches = useMemo(() => {
    const q = addQuery.trim().toLowerCase();
    const queued = new Set(pendingAdd);
    return candidates
      .filter((c) => !queued.has(c.rating_key))
      .filter((c) => !q || c.title.toLowerCase().includes(q))
      .slice(0, 30);
  }, [candidates, pendingAdd, addQuery]);

  if (sectionsLoaded && !sections.length) {
    return (
      <div className={`${theme.blockCard} p-12 text-center`}>
        <p className={`text-[18px] font-headline ${isLight ? "text-slate-900" : "text-white"}`}>Plex required</p>
        <p className={`mt-2 text-[14px] ${theme.muted}`}>
          Connect Plex under Settings → Media Integrations. Admin Plex Pass is required for hub management.
        </p>
        {props.onOpenPlexSettings ? (
          <button
            type="button"
            onClick={props.onOpenPlexSettings}
            className={`mt-5 rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
            style={accentFilledStyle(accentHex)}
          >
            Connect Plex
          </button>
        ) : null}
        {sectionsError ? <p className={`mt-4 text-[12px] ${theme.muted}`}>{sectionsError}</p> : null}
      </div>
    );
  }

  const pendingParts = [
    pending.moved.size ? `${pending.moved.size} moved` : null,
    pending.visibility.size ? `${pending.visibility.size} visibility` : null,
    pending.add ? `${pending.add} to add` : null,
    pending.removed.length ? `${pending.removed.length} to remove` : null,
  ].filter(Boolean);

  const tabClass = (active: boolean) =>
    `flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px] transition-colors ${
      active
        ? isLight
          ? "border-slate-900 bg-slate-900 text-white"
          : "border-slate-300 bg-slate-100 text-slate-900"
        : theme.chipInactive
    }`;

  return (
    <div className="w-full min-w-[max(100%,40rem)] pb-24">
      <div className="mb-1 flex items-center gap-2">
        <div className="h-2 w-2 rounded-full bg-green-500" />
        <span className={`text-[12px] font-headline uppercase tracking-widest ${theme.iconMuted}`}>
          Where collections appear in Plex
        </span>
        <span className="rounded-full border border-sky-400/40 bg-sky-500/10 px-2 py-0.5 text-[11px] font-headline uppercase tracking-wider text-sky-300">
        New UX preview
      </span>
      </div>
      <h1 className={`mb-2 text-[32px] font-black tracking-tight font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
        Ordering
      </h1>
      <p className={`mb-5 max-w-2xl text-[14px] leading-relaxed ${theme.muted}`}>
        The live row order and visibility in Plex for every collection on a library, Placeholdarr or not. Changes stay
        pending until you apply them, and collection sync never overwrites them.
      </p>

      <div className="mb-4 flex flex-wrap items-center gap-2" role="tablist" aria-label="Plex library">
        {sections.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={s.id === sectionId}
            onClick={() => switchSection(s.id)}
            className={tabClass(s.id === sectionId)}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 16 }} aria-hidden>
              {s.type === "show" ? "tv" : "movie"}
            </span>
            {s.title}
            {s.id === sectionId && dirty ? (
              <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: accentHex }} title="Pending changes" />
            ) : null}
          </button>
        ))}
        <button
          type="button"
          disabled={sectionId == null || loading}
          onClick={() => sectionId != null && void refresh(sectionId)}
          className={`ml-auto inline-flex items-center gap-1 text-[12px] disabled:opacity-40 ${theme.muted}`}
          title="Reload from Plex (drops pending changes)"
        >
          <span className="material-symbols-outlined" style={{ fontSize: 16 }}>
            refresh
          </span>
          Reload
        </button>
      </div>

      {loadError ? <p className={`mb-3 text-[13px] ${isLight ? "text-amber-800" : "text-amber-200"}`}>{loadError}</p> : null}
      {applyError ? <p className={`mb-3 text-[13px] ${isLight ? "text-red-700" : "text-red-300"}`}>{applyError}</p> : null}
      {applyOk ? <p className={`mb-3 text-[13px] ${isLight ? "text-emerald-700" : "text-emerald-300"}`}>{applyOk}</p> : null}

      <div className={`${theme.blockCard} overflow-visible`}>
        <div className={`flex flex-wrap items-center gap-3 border-b px-4 py-3 ${theme.divider}`}>
          <span className={`text-[12px] font-headline uppercase tracking-widest ${theme.muted}`}>
            {draft.filter((d) => !d.removed).length} rows
          </span>
          <div ref={addBoxRef} className="relative ml-auto w-full max-w-sm">
            <span
              className={`material-symbols-outlined pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 ${theme.iconMuted}`}
              style={{ fontSize: 18 }}
            >
              add
            </span>
            <input
              className={`${theme.field} w-full pl-8`}
              placeholder={candidates.length ? "Add a collection to Plex rows…" : "No collections left to add"}
              disabled={!candidates.length}
              value={addQuery}
              onFocus={() => setAddOpen(true)}
              onChange={(e) => {
                setAddQuery(e.target.value);
                setAddOpen(true);
              }}
            />
            {addOpen && addMatches.length ? (
              <div className={`${theme.dropdown} absolute inset-x-0 top-full z-40 mt-1 max-h-72 overflow-y-auto`}>
                {addMatches.map((c) => (
                  <button
                    key={c.rating_key}
                    type="button"
                    onClick={() => {
                      setPendingAdd((prev) => [...prev, c.rating_key]);
                      setAddQuery("");
                      setAddOpen(false);
                      setApplyOk(null);
                    }}
                    className={`${theme.dropdownItem} items-center gap-2 px-3 py-2`}
                  >
                    <span className="min-w-0 flex-1 truncate text-[13px]">{c.title}</span>
                    {c.placeholdarr_owned ? (
                      <span className="shrink-0 text-[11px]" style={{ color: accentHex }}>
                        {c.recipe_name ?? "Placeholdarr"}
                      </span>
                    ) : null}
                    {typeof c.item_count === "number" ? (
                      <span className={`shrink-0 text-[11px] ${theme.muted}`}>{c.item_count}</span>
                    ) : null}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        {loading ? (
          <p className={`p-6 text-[14px] ${theme.muted}`}>Loading rows from Plex…</p>
        ) : !draft.length && !pendingAdd.length ? (
          <p className={`p-6 text-[14px] ${theme.muted}`}>
            No collection rows on this library yet. Add one above, or create a collection from Recipes.
          </p>
        ) : (
          <ul
            className={isLight ? "divide-y divide-slate-200/80" : "divide-y divide-[#424753]/20"}
            onDragOver={(e) => {
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
            }}
            onDrop={onDrop}
          >
            {draft.map((row, index) => {
              const position = draft.slice(0, index + 1).filter((d) => !d.removed).length;
              const isDragging = dragFrom === index;
              const noop = dragFrom != null && (insertBefore === dragFrom || insertBefore === dragFrom + 1);
              const showTop = dragFrom != null && insertBefore === index && !noop;
              const showBottom = dragFrom != null && index === draft.length - 1 && insertBefore === draft.length && !noop;
              const changed = pending.moved.has(row.identifier) || pending.visibility.has(row.identifier);
              return (
                <li
                  key={row.identifier}
                  onDragOver={(e) => onDragOverRow(index, e)}
                  onDrop={onDrop}
                  onDragEnd={finishDrag}
                  className={`relative flex items-center gap-3 px-3 py-2.5 ${isDragging ? "opacity-40" : ""} ${
                    row.removed ? "opacity-50" : ""
                  }`}
                >
                  {showTop ? (
                    <div className="pointer-events-none absolute left-3 right-3 top-0 h-0.5 -translate-y-1/2 rounded-full" style={{ backgroundColor: accentHex }} />
                  ) : null}
                  {showBottom ? (
                    <div className="pointer-events-none absolute bottom-0 left-3 right-3 h-0.5 translate-y-1/2 rounded-full" style={{ backgroundColor: accentHex }} />
                  ) : null}
                  <span
                    className="w-1 self-stretch rounded-full"
                    style={{ backgroundColor: changed ? accentHex : "transparent" }}
                    title={changed ? "Changed" : undefined}
                  />
                  <button
                    type="button"
                    draggable={!row.removed}
                    disabled={row.removed}
                    aria-label={`Reorder ${row.title || "row"}`}
                    title="Drag to reorder"
                    onDragStart={(e) => onDragStart(index, e)}
                    onDragEnd={finishDrag}
                    className={`shrink-0 cursor-grab rounded px-0.5 py-1 text-slate-400 hover:text-slate-200 active:cursor-grabbing ${
                      isLight ? "hover:bg-slate-100" : "hover:bg-[#1e2430]"
                    }`}
                  >
                    <span className="material-symbols-outlined leading-none" style={{ fontSize: 20 }}>
                      drag_indicator
                    </span>
                  </button>
                  <span className={`w-6 shrink-0 text-right font-mono text-[13px] ${theme.muted}`}>
                    {row.removed ? "" : position}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div
                      className={`truncate text-[15px] font-medium ${isLight ? "text-slate-900" : "text-white"} ${
                        row.removed ? "line-through" : ""
                      }`}
                    >
                      {row.title || row.identifier}
                    </div>
                    <div className={`mt-0.5 text-[12px] ${theme.muted}`}>
                      {row.placeholdarr_owned ? (
                        props.onOpenRecipes ? (
                          <button type="button" className="underline-offset-2 hover:underline" onClick={props.onOpenRecipes}>
                            Placeholdarr{row.recipe_name ? ` · ${row.recipe_name}` : ""}
                          </button>
                        ) : (
                          `Placeholdarr${row.recipe_name ? ` · ${row.recipe_name}` : ""}`
                        )
                      ) : (
                        "Plex collection"
                      )}
                      {row.removed ? " · will be removed from rows" : ""}
                    </div>
                  </div>
                  {!row.removed ? (
                    <>
                      <div className="flex shrink-0 items-center gap-1">
                        {VIS_PILLS.map((pill) => {
                          const on = row[pill.key];
                          return (
                            <button
                              key={pill.key}
                              type="button"
                              aria-pressed={on}
                              title={pill.hint}
                              onClick={() => toggleVis(row.identifier, pill.key)}
                              className={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${
                                on ? "border-transparent" : theme.chipInactive
                              }`}
                              style={on ? { backgroundColor: `${accentHex}26`, color: accentHex } : undefined}
                            >
                              {pill.label}
                            </button>
                          );
                        })}
                      </div>
                      <div className="flex shrink-0 items-center">
                        <button
                          type="button"
                          disabled={index === 0}
                          onClick={() => move(index, -1)}
                          className={`material-symbols-outlined rounded p-1 disabled:opacity-25 ${theme.iconAction}`}
                          style={{ fontSize: 18 }}
                          aria-label="Move up"
                          title="Move up"
                        >
                          arrow_upward
                        </button>
                        <button
                          type="button"
                          disabled={index === draft.length - 1}
                          onClick={() => move(index, 1)}
                          className={`material-symbols-outlined rounded p-1 disabled:opacity-25 ${theme.iconAction}`}
                          style={{ fontSize: 18 }}
                          aria-label="Move down"
                          title="Move down"
                        >
                          arrow_downward
                        </button>
                        <button
                          type="button"
                          disabled={!row.deletable}
                          onClick={() => setRemoved(row.identifier, true)}
                          className={`material-symbols-outlined rounded p-1 text-red-400 disabled:opacity-25 hover:text-red-300`}
                          style={{ fontSize: 18 }}
                          aria-label="Remove from rows"
                          title={row.deletable ? "Remove from Plex rows" : "This row cannot be removed"}
                        >
                          close
                        </button>
                      </div>
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setRemoved(row.identifier, false)}
                      className={`${theme.cancelButton} shrink-0`}
                    >
                      Undo
                    </button>
                  )}
                </li>
              );
            })}
            {pendingAdd.map((rk) => {
              const c = candidates.find((x) => x.rating_key === rk);
              return (
                <li key={`add-${rk}`} className="flex items-center gap-3 px-3 py-2.5">
                  <span className="w-1 self-stretch rounded-full" style={{ backgroundColor: accentHex }} />
                  <span className="material-symbols-outlined text-emerald-400" style={{ fontSize: 20 }}>
                    add_circle
                  </span>
                  <span className="w-6" />
                  <div className="min-w-0 flex-1">
                    <div className={`truncate text-[15px] font-medium ${isLight ? "text-slate-900" : "text-white"}`}>
                      {c?.title ?? rk}
                    </div>
                    <div className={`mt-0.5 text-[12px] ${theme.muted}`}>
                      Will be added to Recommended. Set Home / Shared and order after applying.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setPendingAdd((prev) => prev.filter((x) => x !== rk))}
                    className={`${theme.cancelButton} shrink-0`}
                  >
                    Undo
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {dirty || applying ? (
        <div className="fixed inset-x-0 bottom-0 z-40 border-t border-[#424753]/40 bg-[#0f1419]/95 px-4 py-3 backdrop-blur-sm lg:left-64">
          <div className="mx-auto flex max-w-[90rem] items-center justify-end gap-3">
            <StickyActionBar
              accentHex={accentHex}
              hint={`Pending: ${pendingParts.join(" · ")}`}
              primaryLabel="Apply to Plex"
              primaryBusyLabel="Applying…"
              primaryLoading={applying}
              onPrimary={() => void handleApply()}
              onCancel={discard}
              cancelLabel="Discard"
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}
