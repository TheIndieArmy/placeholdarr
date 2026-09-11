import { useEffect, useState } from "react";
import { ensureDestFolder, getArrRootFolders } from "../api/dashboard";
import { getCollectionPlexSections } from "../api/collections";
import type { PlexSectionOption } from "../types/api";

export type DestinationMapRow = {
  instance_key: string;
  arr_type: "radarr" | "sonarr";
  arr_root_path: string;
  dest_folder: string;
  plex_section_id?: number | null;
};

type ArrRootInstance = {
  instance_key: string;
  label: string;
  arr_type: "radarr" | "sonarr";
  root_folders: Array<{ id?: number | null; path: string }>;
};

type SelectedRoot = {
  instance_key: string;
  arr_type: "radarr" | "sonarr";
  path: string;
};

/** One UI rule expands to N JSON rows (one per selected Arr root). */
type DestinationRule = {
  id: string;
  dest_folder: string;
  instance_keys: string[];
  selected_roots: SelectedRoot[];
  plex_section_id: number | "";
};

/** Match Media / ARR integration tiles (rounded-2xl accent surface). */
const DESTINATION_CARD_SURFACE_CLASS =
  "rounded-2xl border border-[var(--brand-accent-3)] bg-[color:color-mix(in_srgb,var(--brand-surface-panel)_92%,var(--brand-accent-3)_8%)] shadow-lg shadow-black/15 backdrop-blur-md transition hover:shadow-[0_0_36px_-14px_color-mix(in_srgb,var(--brand-accent-3)_28%,transparent)]";

function newRuleId(): string {
  return `rule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Match ``services.library_destinations._normalize_path`` for uniqueness checks. */
function normalizeDestPath(path: string): string {
  let text = String(path || "")
    .trim()
    .replace(/\\/g, "/");
  while (text.includes("//")) {
    text = text.replace(/\/\//g, "/");
  }
  if (text.length > 1 && text.endsWith("/")) {
    text = text.replace(/\/+$/, "");
  }
  return text;
}

function rootKey(instance_key: string, path: string): string {
  return `${String(instance_key || "")
    .trim()
    .toLowerCase()}::${normalizeDestPath(path).toLowerCase()}`;
}

function destinationLabel(rules: DestinationRule[], ruleId: string): string {
  const index = rules.findIndex((r) => r.id === ruleId);
  return index >= 0 ? `Destination ${index + 1}` : "another destination";
}

/** Other destination that already claims this instance+root (excluding ``exceptRuleId``). */
function findRootClaimedBy(
  rules: DestinationRule[],
  instanceKey: string,
  path: string,
  exceptRuleId?: string
): DestinationRule | null {
  const want = rootKey(instanceKey, path);
  for (const rule of rules) {
    if (exceptRuleId && rule.id === exceptRuleId) continue;
    if (rule.selected_roots.some((r) => rootKey(r.instance_key, r.path) === want)) {
      return rule;
    }
  }
  return null;
}

function parseMapJson(raw: string): DestinationMapRow[] {
  const text = String(raw || "").trim();
  if (!text) return [];
  try {
    const payload = JSON.parse(text);
    if (!Array.isArray(payload)) return [];
    const out: DestinationMapRow[] = [];
    for (const item of payload) {
      if (!item || typeof item !== "object") continue;
      const arr_type = String((item as DestinationMapRow).arr_type || "").toLowerCase();
      if (arr_type !== "radarr" && arr_type !== "sonarr") continue;
      const instance_key = String((item as DestinationMapRow).instance_key || "").trim().toLowerCase();
      const arr_root_path = String((item as DestinationMapRow).arr_root_path || "").trim();
      const dest_folder = String((item as DestinationMapRow).dest_folder || "").trim();
      if (!instance_key || !arr_root_path || !dest_folder) continue;
      let plex_section_id: number | null | undefined;
      const plexRaw = (item as DestinationMapRow).plex_section_id;
      if (plexRaw !== undefined && plexRaw !== null && String(plexRaw).trim() !== "") {
        const n = Number(plexRaw);
        plex_section_id = Number.isFinite(n) ? n : null;
      }
      out.push({ instance_key, arr_type, arr_root_path, dest_folder, plex_section_id });
    }
    return out;
  } catch {
    return [];
  }
}

function rowsToRules(rows: DestinationMapRow[]): DestinationRule[] {
  const groups = new Map<string, DestinationRule>();
  for (const row of rows) {
    const plexKey = row.plex_section_id == null ? "" : String(row.plex_section_id);
    const key = `${row.dest_folder}::${row.arr_type}::${plexKey}`;
    const existing = groups.get(key);
    const selected: SelectedRoot = {
      instance_key: row.instance_key,
      arr_type: row.arr_type,
      path: row.arr_root_path,
    };
    if (existing) {
      if (!existing.instance_keys.includes(row.instance_key)) {
        existing.instance_keys.push(row.instance_key);
      }
      const rk = rootKey(row.instance_key, row.arr_root_path);
      if (!existing.selected_roots.some((r) => rootKey(r.instance_key, r.path) === rk)) {
        existing.selected_roots.push(selected);
      }
      continue;
    }
    groups.set(key, {
      id: newRuleId(),
      dest_folder: row.dest_folder,
      instance_keys: [row.instance_key],
      selected_roots: [selected],
      plex_section_id: row.plex_section_id == null ? "" : row.plex_section_id,
    });
  }
  return Array.from(groups.values());
}

function rulesToJson(rules: DestinationRule[]): string {
  const rows: DestinationMapRow[] = [];
  for (const rule of rules) {
    const dest = rule.dest_folder.trim();
    if (!dest || !rule.selected_roots.length) continue;
    for (const root of rule.selected_roots) {
      const arr_root_path = String(root.path || "").trim();
      const instance_key = String(root.instance_key || "").trim().toLowerCase();
      if (!arr_root_path || !instance_key) continue;
      const row: DestinationMapRow = {
        instance_key,
        arr_type: root.arr_type,
        arr_root_path,
        dest_folder: dest,
      };
      if (rule.plex_section_id !== "" && Number.isFinite(Number(rule.plex_section_id))) {
        row.plex_section_id = Number(rule.plex_section_id);
      }
      rows.push(row);
    }
  }
  if (!rows.length) return "";
  return JSON.stringify(rows);
}

function emptyRule(libraryRoot?: string): DestinationRule {
  const root = String(libraryRoot || "").trim().replace(/\/+$/, "");
  return {
    id: newRuleId(),
    dest_folder: root ? `${root}/` : "",
    instance_keys: [],
    selected_roots: [],
    plex_section_id: "",
  };
}

function ruleArrType(
  rule: DestinationRule,
  allInstances: ArrRootInstance[]
): "radarr" | "sonarr" | null {
  for (const key of rule.instance_keys) {
    const inst = allInstances.find((i) => i.instance_key === key);
    if (inst) return inst.arr_type;
  }
  for (const root of rule.selected_roots) {
    if (root.arr_type === "radarr" || root.arr_type === "sonarr") return root.arr_type;
  }
  return null;
}

function coerceRuleToSingleArrType(rule: DestinationRule, allInstances: ArrRootInstance[]): DestinationRule {
  const locked = ruleArrType(rule, allInstances);
  if (!locked) return rule;
  const allowed = new Set(
    allInstances.filter((i) => i.arr_type === locked).map((i) => i.instance_key)
  );
  const instance_keys = rule.instance_keys.filter((k) => allowed.has(k));
  const selected_roots = rule.selected_roots.filter(
    (r) => r.arr_type === locked || allowed.has(r.instance_key)
  );
  if (
    instance_keys.length === rule.instance_keys.length &&
    selected_roots.length === rule.selected_roots.length
  ) {
    return rule;
  }
  return { ...rule, instance_keys, selected_roots };
}

function ruleIsComplete(rule: DestinationRule): boolean {
  return Boolean(rule.dest_folder.trim() && rule.selected_roots.length > 0);
}

function cloneRule(rule: DestinationRule): DestinationRule {
  return {
    ...rule,
    instance_keys: [...rule.instance_keys],
    selected_roots: rule.selected_roots.map((r) => ({ ...r })),
  };
}

export function DestinationMapEditor(props: {
  value: string;
  onChange: (json: string) => void;
  focusClass: string;
  layout?: "settings" | "wizard";
  /** When false, Plex library pickers are disabled (Jellyfin/Emby refresh by path). */
  plexActive?: boolean;
  /** Prefills new destination paths from Settings → Library Root. */
  libraryRoot?: string;
}) {
  const [instances, setInstances] = useState<ArrRootInstance[]>([]);
  const [sections, setSections] = useState<PlexSectionOption[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [rules, setRules] = useState<DestinationRule[]>(() => rowsToRules(parseMapJson(props.value)));
  const [draft, setDraft] = useState<DestinationRule | null>(null);
  const [draftIsNew, setDraftIsNew] = useState(false);
  const [saveHint, setSaveHint] = useState<string | null>(null);
  const [folderBusy, setFolderBusy] = useState(false);
  const [folderStatus, setFolderStatus] = useState<null | {
    kind: "created" | "exists" | "error";
    message: string;
  }>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const plexPromise = props.plexActive
      ? getCollectionPlexSections().catch(() => ({ sections: [] as PlexSectionOption[] }))
      : Promise.resolve({ sections: [] as PlexSectionOption[] });
    Promise.all([getArrRootFolders(), plexPromise])
      .then(([arrPayload, plexPayload]) => {
        if (cancelled) return;
        const nextInstances = (arrPayload.instances || []).map((item) => ({
          instance_key: String(item.instance_key || "").trim().toLowerCase(),
          label: String(item.label || item.instance_key || ""),
          arr_type: (item.arr_type === "sonarr" ? "sonarr" : "radarr") as "radarr" | "sonarr",
          root_folders: Array.isArray(item.root_folders) ? item.root_folders : [],
        }));
        setInstances(nextInstances);
        setSections(props.plexActive ? plexPayload.sections || [] : []);
        setLoadError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Failed to load Arr root folders");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [props.plexActive]);

  useEffect(() => {
    if (!instances.length || !rules.length) return;
    const next = rules.map((rule) => coerceRuleToSingleArrType(rule, instances));
    const changed = next.some((rule, i) => rule !== rules[i]);
    if (changed) commitRules(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- coerce when instance catalog arrives
  }, [instances]);

  useEffect(() => {
    const next = rowsToRules(parseMapJson(props.value));
    const current = rulesToJson(rules);
    const incoming = rulesToJson(next);
    if (current !== incoming) {
      setRules(next);
      setDraft(null);
      setDraftIsNew(false);
      setSaveHint(null);
      setFolderStatus(null);
      setFolderBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to external value changes
  }, [props.value]);

  function commitRules(nextRules: DestinationRule[]) {
    setRules(nextRules);
    props.onChange(rulesToJson(nextRules));
  }

  function patchDraft(patch: Partial<DestinationRule>) {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));
    setSaveHint(null);
    if (patch.dest_folder !== undefined) {
      setFolderStatus(null);
    }
  }

  function toggleInstance(instance: ArrRootInstance) {
    if (!draft) return;
    const has = draft.instance_keys.includes(instance.instance_key);
    if (has) {
      patchDraft({
        instance_keys: draft.instance_keys.filter((k) => k !== instance.instance_key),
        selected_roots: draft.selected_roots.filter((r) => r.instance_key !== instance.instance_key),
      });
      return;
    }
    const lockedType = ruleArrType(draft, instances);
    if (lockedType && lockedType !== instance.arr_type) return;
    const sameTypeKeys = new Set(
      instances.filter((i) => i.arr_type === instance.arr_type).map((i) => i.instance_key)
    );
    patchDraft({
      instance_keys: [...draft.instance_keys.filter((k) => sameTypeKeys.has(k)), instance.instance_key],
      selected_roots: draft.selected_roots.filter((r) => sameTypeKeys.has(r.instance_key)),
    });
  }

  function toggleRoot(instance: ArrRootInstance, path: string) {
    if (!draft) return;
    const rk = rootKey(instance.instance_key, path);
    const has = draft.selected_roots.some((r) => rootKey(r.instance_key, r.path) === rk);
    if (!has) {
      const claimed = findRootClaimedBy(rules, instance.instance_key, path, draft.id);
      if (claimed) {
        setSaveHint(
          `That Arr root is already mapped on ${destinationLabel(rules, claimed.id)}. Pick a different root or edit that destination first.`
        );
        return;
      }
    }
    const selected_roots = has
      ? draft.selected_roots.filter((r) => rootKey(r.instance_key, r.path) !== rk)
      : [
          ...draft.selected_roots,
          { instance_key: instance.instance_key, arr_type: instance.arr_type, path },
        ];
    const instance_keys = draft.instance_keys.includes(instance.instance_key)
      ? draft.instance_keys
      : [...draft.instance_keys, instance.instance_key];
    patchDraft({ selected_roots, instance_keys });
  }

  function openAddModal() {
    if (draft) return;
    setDraft(emptyRule(props.libraryRoot));
    setDraftIsNew(true);
    setSaveHint(null);
    setFolderStatus(null);
  }

  function openEditModal(rule: DestinationRule) {
    if (draft) return;
    setDraft(cloneRule(rule));
    setDraftIsNew(false);
    setSaveHint(null);
    setFolderStatus(null);
  }

  function closeModal() {
    setDraft(null);
    setDraftIsNew(false);
    setSaveHint(null);
    setFolderStatus(null);
    setFolderBusy(false);
  }

  async function createPlaceholderFolder() {
    if (!draft) return;
    const path = draft.dest_folder.trim();
    if (!path) {
      setFolderStatus({ kind: "error", message: "Enter a placeholder path first." });
      return;
    }
    setFolderBusy(true);
    setFolderStatus(null);
    try {
      const result = await ensureDestFolder(path);
      if (!result.ok) {
        setFolderStatus({ kind: "error", message: result.message || "Could not create folder" });
        return;
      }
      if (result.existed) {
        setFolderStatus({ kind: "exists", message: "Path already exists" });
        return;
      }
      if (result.created) {
        setFolderStatus({ kind: "created", message: "Folder created" });
        return;
      }
      setFolderStatus({ kind: "created", message: result.message || "Folder ready" });
    } catch (err) {
      setFolderStatus({
        kind: "error",
        message: err instanceof Error ? err.message : "Could not create folder",
      });
    } finally {
      setFolderBusy(false);
    }
  }

  function saveDraft() {
    if (!draft) return;
    const nextDraft = coerceRuleToSingleArrType(draft, instances);
    if (!ruleIsComplete(nextDraft)) {
      setSaveHint("Add a placeholder path and at least one Arr root folder before saving.");
      return;
    }
    for (const root of nextDraft.selected_roots) {
      const claimed = findRootClaimedBy(rules, root.instance_key, root.path, nextDraft.id);
      if (claimed) {
        const inst = instances.find((i) => i.instance_key === root.instance_key);
        const label = inst?.label || root.instance_key;
        setSaveHint(
          `${label} root ${normalizeDestPath(root.path) || root.path} is already on ${destinationLabel(rules, claimed.id)}. Each Arr root can only map to one Placeholdarr folder.`
        );
        return;
      }
    }
    if (draftIsNew) {
      commitRules([...rules, nextDraft]);
    } else {
      commitRules(rules.map((rule) => (rule.id === nextDraft.id ? nextDraft : rule)));
    }
    closeModal();
  }

  function removeRule(id: string) {
    if (draft?.id === id) closeModal();
    commitRules(rules.filter((rule) => rule.id !== id));
  }

  const plexActive = Boolean(props.plexActive);
  const plexSectionsAvailable = plexActive && sections.length > 0;
  const lockedType = draft ? ruleArrType(draft, instances) : null;
  const sectionType = lockedType === "sonarr" ? "show" : lockedType === "radarr" ? "movie" : null;
  const sectionOptions = sectionType ? sections.filter((s) => s.type === sectionType) : [];
  const selectedInstances = draft
    ? instances.filter((i) => draft.instance_keys.includes(i.instance_key))
    : [];
  const canAdd = Boolean(instances.length) && !draft;
  const pathExample = (() => {
    const root = String(props.libraryRoot || "").trim().replace(/\/+$/, "");
    return root ? `${root}/animated-movies` : "/data/placeholders/animated-movies";
  })();

  function plexLabelFor(rule: DestinationRule): string {
    const locked = ruleArrType(rule, instances);
    if (rule.plex_section_id === "" || rule.plex_section_id == null) {
      if (locked === "radarr") return "Default Movies library";
      if (locked === "sonarr") return "Default TV library";
      return "Default Plex library";
    }
    const match = sections.find((s) => s.id === Number(rule.plex_section_id));
    return match ? `${match.title} (#${match.id})` : `Plex #${rule.plex_section_id}`;
  }

  function instanceLabelsFor(rule: DestinationRule): string {
    const labels = rule.instance_keys.map((key) => {
      const inst = instances.find((i) => i.instance_key === key);
      return inst?.label || key;
    });
    return labels.length ? labels.join(", ") : "—";
  }

  const modal = draft ? (
    <div className="fixed inset-0 z-[70] flex items-center justify-center overflow-y-auto p-4 sm:p-6">
      <button
        type="button"
        aria-label="Close panel"
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={closeModal}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={draftIsNew ? "Add library destination" : "Configure library destination"}
        className="relative z-10 my-auto flex w-full max-w-lg max-h-[min(90vh,720px)] flex-col overflow-hidden rounded-2xl border border-[#424753]/50 bg-[#171c22] shadow-2xl"
      >
        <div className="flex items-start justify-between gap-3 border-b border-[#424753]/40 px-5 py-4 shrink-0">
          <div>
            <div className="text-[12px] font-headline uppercase tracking-widest text-slate-500">
              Library destination
            </div>
            <h2 className="mt-0.5 text-[20px] font-headline font-bold text-white">
              {draftIsNew ? "Add destination" : "Configure destination"}
              {lockedType ? (
                <span className="ml-2 text-[14px] font-medium text-slate-400">
                  · {lockedType === "radarr" ? "Movies" : "TV"}
                </span>
              ) : null}
            </h2>
          </div>
          <button
            type="button"
            onClick={closeModal}
            className="rounded-lg p-2 text-slate-400 hover:bg-[#252e3a]/80 hover:text-white"
            aria-label="Close"
          >
            <span className="material-symbols-outlined" style={{ fontSize: 22 }}>
              close
            </span>
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          <div>
            <label className="mb-1 block text-[14px] font-semibold text-slate-300">Placeholder path</label>
            <div
              className={`mb-1 flex min-h-[1.35rem] items-center justify-end gap-1.5 text-[13px] ${
                !folderStatus
                  ? "invisible"
                  : folderStatus.kind === "error"
                    ? "text-red-400"
                    : folderStatus.kind === "exists"
                      ? "text-slate-300"
                      : "text-emerald-300"
              }`}
              aria-live="polite"
              aria-hidden={!folderStatus}
            >
              {folderStatus ? (
                <>
                  <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16 }}>
                    {folderStatus.kind === "error"
                      ? "error"
                      : folderStatus.kind === "exists"
                        ? "folder"
                        : "check_circle"}
                  </span>
                  <span className="leading-snug">{folderStatus.message}</span>
                </>
              ) : (
                <span>Folder created</span>
              )}
            </div>
            <div className="flex gap-2">
              <input
                className={`min-w-0 flex-1 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[16px] text-slate-200 outline-none ${props.focusClass}`}
                value={draft.dest_folder}
                placeholder={pathExample}
                onChange={(e) => patchDraft({ dest_folder: e.target.value })}
              />
              <button
                type="button"
                disabled={folderBusy || !draft.dest_folder.trim()}
                onClick={() => void createPlaceholderFolder()}
                className="shrink-0 rounded-lg border border-[#424753]/55 bg-[#0f1419] px-3 py-2 text-[13px] font-headline uppercase tracking-wider text-slate-200 transition hover:bg-[#151b24] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {folderBusy ? "Creating…" : "Create folder"}
              </button>
            </div>
            <p className="ui-field-description-compact mt-1">
              Folder where Placeholdarr writes placeholders for the roots selected below. Create it here so media
              players can map a library to it.
            </p>
          </div>

          <div>
            <div className="mb-2 text-[14px] font-semibold text-slate-300">Arr instances</div>
            {!instances.length ? (
              <p className="ui-field-description-compact">No Arr instances available.</p>
            ) : (
              <>
                <p className="ui-field-description-compact mb-2">
                  Pick Radarr or Sonarr instances. A destination cannot mix Movies and TV.
                </p>
                <div className="space-y-2">
                  {instances.map((item) => {
                    const checked = draft.instance_keys.includes(item.instance_key);
                    const lockedOut = Boolean(lockedType && lockedType !== item.arr_type);
                    return (
                      <label
                        key={item.instance_key}
                        className={`flex items-start gap-3 rounded-lg border border-[#424753]/30 bg-[#0f1419] px-3 py-2 ${
                          lockedOut ? "cursor-not-allowed opacity-45" : "cursor-pointer"
                        }`}
                      >
                        <input
                          type="checkbox"
                          className="mt-1"
                          checked={checked}
                          disabled={lockedOut}
                          onChange={() => toggleInstance(item)}
                        />
                        <span className="min-w-0 text-[14px] text-slate-200">
                          {item.label} <span className="text-slate-500">({item.arr_type})</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              </>
            )}
          </div>

          <div>
            <div className="mb-2 text-[14px] font-semibold text-slate-300">Arr root folders</div>
            {!draft.instance_keys.length ? (
              <p className="ui-field-description-compact">Select at least one Arr instance first.</p>
            ) : (
              <div className="space-y-4">
                <p className="ui-field-description-compact">
                  Each Arr root can only map to one Placeholdarr folder. The same instance can appear on multiple
                  destinations when each uses different roots.
                </p>
                {selectedInstances.map((inst) => {
                  const roots = inst.root_folders || [];
                  return (
                    <div key={inst.instance_key} className="space-y-2">
                      <div className="text-[12px] font-headline uppercase tracking-widest text-slate-500">
                        {inst.label}
                      </div>
                      {!roots.length ? (
                        <p className="ui-field-description-compact">
                          No root folders returned. Check the Arr connection, then reopen Settings.
                        </p>
                      ) : (
                        roots.map((folder) => {
                          const checked = draft.selected_roots.some(
                            (r) =>
                              rootKey(r.instance_key, r.path) ===
                              rootKey(inst.instance_key, folder.path)
                          );
                          const claimedBy = checked
                            ? null
                            : findRootClaimedBy(rules, inst.instance_key, folder.path, draft.id);
                          const taken = Boolean(claimedBy);
                          return (
                            <label
                              key={folder.path}
                              className={`flex items-start gap-3 rounded-lg border border-[#424753]/30 bg-[#0f1419] px-3 py-2 ${
                                taken ? "cursor-not-allowed opacity-45" : "cursor-pointer"
                              }`}
                            >
                              <input
                                type="checkbox"
                                className="mt-1"
                                checked={checked}
                                disabled={taken}
                                onChange={() => toggleRoot(inst, folder.path)}
                              />
                              <span className="min-w-0">
                                <span className="block break-all font-mono text-[13px] text-slate-200">
                                  {folder.path}
                                </span>
                                {taken && claimedBy ? (
                                  <span className="mt-0.5 block text-[12px] text-slate-500">
                                    Already on {destinationLabel(rules, claimedBy.id)}
                                  </span>
                                ) : null}
                              </span>
                            </label>
                          );
                        })
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {plexActive ? (
            <div className={plexSectionsAvailable ? undefined : "opacity-70"}>
              <label className="mb-1 block text-[14px] font-semibold text-slate-300">
                Plex library (optional override)
              </label>
              {!sectionType ? (
                <p className="ui-field-description-compact">
                  Select Arr instances first to choose a Movies or TV Plex library override.
                </p>
              ) : !sections.length ? (
                <p className="ui-field-description-compact">
                  Could not load Plex libraries yet. Leave blank to use the default Movies/TV section under Library
                  Root, or reconnect Plex and reopen Settings.
                </p>
              ) : (
                <select
                  className={`w-full rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[16px] text-slate-200 outline-none ${props.focusClass}`}
                  value={draft.plex_section_id === "" ? "" : String(draft.plex_section_id)}
                  onChange={(e) =>
                    patchDraft({
                      plex_section_id: e.target.value ? Number(e.target.value) : "",
                    })
                  }
                >
                  <option value="">Use default {sectionType === "movie" ? "Movies" : "TV"} library</option>
                  {sectionOptions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.title} (#{s.id})
                    </option>
                  ))}
                </select>
              )}
            </div>
          ) : (
            <p className="ui-field-description-compact">
              Plex library overrides are hidden while Plex is off. Jellyfin and Emby refresh by folder path and do not
              need library IDs.
            </p>
          )}
        </div>

        <div className="shrink-0 border-t border-[#424753]/40 bg-[#141a24]">
          <div className="flex min-h-[2.75rem] items-center gap-2 px-4 pt-3 text-[14px] text-yellow-300/90" aria-live="polite">
            {saveHint ? <span className="leading-snug">{saveHint}</span> : null}
          </div>
          <div className="flex flex-wrap items-stretch justify-between gap-2 px-4 pb-4 pt-1">
            <button
              type="button"
              onClick={closeModal}
              className="min-w-[5.5rem] flex-1 rounded-lg border border-[#424753]/55 px-4 py-2.5 text-[14px] font-headline uppercase tracking-wider text-slate-300 transition-colors hover:border-[#424753]/80 hover:bg-[#252e3a]/80 sm:flex-none"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={saveDraft}
              className="btn-brand-tertiary min-w-[6.5rem] flex-1 rounded-lg border px-4 py-2.5 text-[14px] font-headline font-semibold uppercase tracking-wider sm:flex-none"
            >
              {draftIsNew ? "Add destination" : "Save destination"}
            </button>
          </div>
        </div>
      </div>
    </div>
  ) : null;

  const body = (
    <div className="space-y-4">
      <p className="ui-field-description leading-relaxed">
        Map Arr root folders to Placeholdarr folders (and optional Plex libraries). Anything left unmapped keeps using
        Library Root above (<span className="font-mono text-slate-400">movies</span> /{" "}
        <span className="font-mono text-slate-400">tv</span>). After Save Settings, choose Apply now or Next full sync so
        existing placeholders move.
      </p>

      {loading ? <p className="ui-field-description">Loading Arr root folders…</p> : null}
      {loadError ? <p className="text-[14px] text-yellow-300/90">{loadError}</p> : null}

      {!loading && !instances.length ? (
        <p className="ui-field-description">
          Configure at least one Radarr or Sonarr instance under ARR Integrations to map root folders.
        </p>
      ) : null}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {rules.map((rule, index) => {
          const locked = ruleArrType(rule, instances);
          return (
            <div key={rule.id} className={`flex min-h-[220px] flex-col ${DESTINATION_CARD_SURFACE_CLASS} p-5`}>
              <div className="flex h-14 w-full shrink-0 items-center justify-center" aria-hidden>
                <span className="material-symbols-outlined text-slate-400" style={{ fontSize: 36 }}>
                  {locked === "sonarr" ? "tv" : "movie"}
                </span>
              </div>
              <h4 className="mt-2 w-full text-center text-[18px] font-bold tracking-tight text-white font-headline">
                Destination {index + 1}
                {locked ? (
                  <span className="block text-[13px] font-medium text-slate-400">
                    {locked === "radarr" ? "Movies" : "TV"}
                  </span>
                ) : null}
              </h4>
              <div className="mt-3 min-w-0 flex-1">
                <div className="truncate text-center font-mono text-[13px] text-slate-300" title={rule.dest_folder}>
                  {rule.dest_folder}
                </div>
                <dl className="mt-3 space-y-1.5 rounded-xl border border-white/[0.06] bg-black/20 p-3 text-[13px] leading-snug">
                  <div className="flex min-w-0 gap-2">
                    <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Instances</dt>
                    <dd className="min-w-0 truncate text-slate-200" title={instanceLabelsFor(rule)}>
                      {instanceLabelsFor(rule)}
                    </dd>
                  </div>
                  <div className="flex min-w-0 gap-2">
                    <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Roots</dt>
                    <dd className="min-w-0 text-slate-200">
                      {rule.selected_roots.length === 1 ? (
                        <span className="block truncate font-mono" title={rule.selected_roots[0].path}>
                          {rule.selected_roots[0].path}
                        </span>
                      ) : (
                        <span>{rule.selected_roots.length} folders</span>
                      )}
                    </dd>
                  </div>
                  {plexActive ? (
                    <div className="flex min-w-0 gap-2">
                      <dt className="w-[4.75rem] shrink-0 font-medium text-slate-500">Plex</dt>
                      <dd className="min-w-0 truncate text-slate-200" title={plexLabelFor(rule)}>
                        {plexLabelFor(rule)}
                      </dd>
                    </div>
                  ) : null}
                </dl>
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={Boolean(draft)}
                  onClick={() => openEditModal(rule)}
                  className="rounded-lg border border-white/15 bg-white/[0.05] px-3 py-1.5 text-[13px] font-headline font-semibold uppercase tracking-wider text-slate-200 transition hover:border-white/25 hover:bg-white/[0.09] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Configure
                </button>
                <button
                  type="button"
                  onClick={() => removeRule(rule.id)}
                  className="ml-auto shrink-0 rounded-lg px-3 py-1.5 text-[13px] font-medium text-red-400 transition hover:text-red-300"
                >
                  Remove
                </button>
              </div>
            </div>
          );
        })}

        <button
          type="button"
          disabled={!canAdd}
          onClick={openAddModal}
          className={`flex min-h-[220px] flex-col items-center justify-center gap-2 rounded-2xl border border-dashed px-4 py-6 text-center transition ${
            canAdd
              ? "border-white/15 bg-black/25 text-slate-300 hover:border-white/30 hover:bg-white/[0.04]"
              : "cursor-not-allowed border-white/[0.07] bg-black/20 text-slate-600"
          }`}
        >
          <span className="material-symbols-outlined" style={{ fontSize: 28 }}>
            add
          </span>
          <span className="text-[16px] font-headline tracking-wide">Add destination</span>
          {!instances.length ? (
            <span className="ui-field-description-compact max-w-[16rem]">Connect Arr first</span>
          ) : null}
        </button>
      </div>

      {modal}
    </div>
  );

  if (props.layout === "wizard") {
    return body;
  }

  return <div className="ui-section-frame-inner">{body}</div>;
}

export function destinationMapHasRows(raw: string): boolean {
  return parseMapJson(raw).length > 0;
}
