import { KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  addCollectionTitlesToArr,
  arrAddItemKey,
  getCollectionArrAddOptions,
  normalizeArrTagLabel,
} from "../api/collections";
import { ToggleSwitch } from "../ToggleSwitch";
import { useCollectionTheme } from "./collectionTheme";
import type {
  CollectionArrAddItem,
  CollectionArrAddInstanceOptions,
  CollectionMissingFromArrItem,
} from "../types/api";

function seedTags(defaultTag: string): string[] {
  const seed = normalizeArrTagLabel(defaultTag || "placeholdarr");
  return seed ? [seed] : [];
}

function displayTitle(item: CollectionMissingFromArrItem): string {
  return item.year ? `${item.title} (${item.year})` : item.title;
}

type ProgressStatus = "waiting" | "adding" | "ok" | "skipped" | "error";

type ProgressRow = {
  key: string;
  title: string;
  instanceKey: string;
  instanceLabel: string;
  status: ProgressStatus;
  error?: string | null;
};

function StatusMark(props: { status: ProgressStatus; arrLabel: string; error?: string | null }) {
  if (props.status === "ok") {
    return (
      <span className="inline-flex items-center gap-1 text-emerald-400">
        <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
          check_circle
        </span>
        Added
      </span>
    );
  }
  if (props.status === "skipped") {
    return (
      <span className="inline-flex items-center gap-1 text-emerald-400">
        <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
          check_circle
        </span>
        Already in {props.arrLabel}
      </span>
    );
  }
  if (props.status === "error") {
    return (
      <span className="inline-flex max-w-[16rem] items-start gap-1 text-red-400">
        <span className="material-symbols-outlined shrink-0" style={{ fontSize: 18 }}>
          cancel
        </span>
        <span className="text-right leading-snug">{props.error || "Error"}</span>
      </span>
    );
  }
  if (props.status === "adding") {
    return <span className="text-slate-400">Adding…</span>;
  }
  return <span className="text-slate-500">Waiting…</span>;
}

export function ArrAddModal(props: {
  mediaType: "movie" | "show";
  items: CollectionMissingFromArrItem[];
  defaultTag: string;
  accentHex: string;
  onClose: () => void;
}) {
  const theme = useCollectionTheme();
  const arrLabel = props.mediaType === "movie" ? "Radarr" : "Sonarr";
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [instances, setInstances] = useState<CollectionArrAddInstanceOptions[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [profiles, setProfiles] = useState<Record<string, number>>({});
  const [roots, setRoots] = useState<Record<string, string>>({});
  const [monitored, setMonitored] = useState(true);
  const [search, setSearch] = useState(false);
  const [tags, setTags] = useState<string[]>(() => seedTags(props.defaultTag));
  const [tagDraft, setTagDraft] = useState("");
  const tagsRef = useRef(tags);
  const draftRef = useRef(tagDraft);
  tagsRef.current = tags;
  draftRef.current = tagDraft;
  const [submitting, setSubmitting] = useState(false);
  const [progressRows, setProgressRows] = useState<ProgressRow[] | null>(null);
  const [progressSummary, setProgressSummary] = useState<{
    added: number;
    skipped: number;
    errors: number;
    message: string;
    warnings: string[];
    done: boolean;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getCollectionArrAddOptions(props.mediaType)
      .then((data) => {
        if (cancelled) return;
        const list = data.instances || [];
        setInstances(list);
        const keys = list.map((row) => row.instance_key);
        setSelectedKeys(keys);
        const nextProfiles: Record<string, number> = {};
        const nextRoots: Record<string, string> = {};
        for (const row of list) {
          if (row.quality_profiles[0]) nextProfiles[row.instance_key] = row.quality_profiles[0].id;
          if (row.root_folders[0]) nextRoots[row.instance_key] = row.root_folders[0].path;
        }
        setProfiles(nextProfiles);
        setRoots(nextRoots);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load ARR options");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [props.mediaType]);

  const canSubmit = useMemo(() => {
    if (!selectedKeys.length || submitting) return false;
    return selectedKeys.every((key) => profiles[key] && roots[key]);
  }, [selectedKeys, profiles, roots, submitting]);

  const normalizedDraft = normalizeArrTagLabel(tagDraft);
  const draftPreview =
    tagDraft.trim() && normalizedDraft && normalizedDraft !== tagDraft.trim() ? normalizedDraft : null;

  const commitDraft = () => {
    const label = normalizeArrTagLabel(draftRef.current);
    if (!label) {
      setTagDraft("");
      draftRef.current = "";
      return;
    }
    setTags((prev) => {
      const next = prev.some((item) => item.toLowerCase() === label.toLowerCase()) ? prev : [...prev, label];
      tagsRef.current = next;
      return next;
    });
    setTagDraft("");
    draftRef.current = "";
  };

  const onTagKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commitDraft();
      return;
    }
    if (event.key === "Backspace" && !tagDraft) {
      event.preventDefault();
      setTags((prev) => prev.slice(0, -1));
    }
  };

  const toggleInstance = (key: string) => {
    setSelectedKeys((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };

  const submit = async () => {
    if (!canSubmit) return;
    commitDraft();
    setSubmitting(true);
    setError(null);
    const items: CollectionArrAddItem[] = props.items.map((item) => ({
      title: item.title,
      year: item.year,
      tmdb_id: item.tmdb_id,
      tvdb_id: item.tvdb_id,
      imdb_id: item.imdb_id,
    }));
    const instance_options: Record<string, { quality_profile_id: number; root_folder_path: string }> = {};
    for (const key of selectedKeys) {
      instance_options[key] = {
        quality_profile_id: profiles[key],
        root_folder_path: roots[key],
      };
    }
    const extra = normalizeArrTagLabel(draftRef.current);
    const committedTags = [...tagsRef.current];
    if (extra && !committedTags.some((item) => item.toLowerCase() === extra.toLowerCase())) {
      committedTags.push(extra);
    }
    const instanceLabels = new Map(instances.map((row) => [row.instance_key, row.label || row.instance_key]));
    const rows: ProgressRow[] = [];
    for (const key of selectedKeys) {
      for (const item of props.items) {
        rows.push({
          key: arrAddItemKey(key, item),
          title: displayTitle(item),
          instanceKey: key,
          instanceLabel: instanceLabels.get(key) || key,
          status: "waiting",
        });
      }
    }
    setProgressRows(rows);
    setProgressSummary(null);
    const patchRow = (itemKey: string, patch: Partial<ProgressRow>) => {
      setProgressRows((prev) =>
        (prev || []).map((row) => (row.key === itemKey ? { ...row, ...patch } : row)),
      );
    };
    try {
      await addCollectionTitlesToArr(
        {
          media_type: props.mediaType,
          items,
          instance_keys: selectedKeys,
          instance_options,
          monitored,
          search,
          tags: committedTags,
        },
        (event) => {
          if (event.type === "ping") return;
          if (event.type === "warning" && event.message) {
            setProgressSummary((prev) => ({
              added: prev?.added ?? 0,
              skipped: prev?.skipped ?? 0,
              errors: prev?.errors ?? 0,
              message: prev?.message ?? "",
              warnings: [...(prev?.warnings || []), event.message as string],
              done: prev?.done ?? false,
            }));
            return;
          }
          if (event.type === "fatal") {
            setError(event.message || "Add failed");
            setProgressRows((prev) =>
              (prev || []).map((row) =>
                row.status === "waiting" || row.status === "adding"
                  ? { ...row, status: "error", error: event.message || "Add failed" }
                  : row,
              ),
            );
            return;
          }
          if (event.type === "item" && event.item_key) {
            const status = (event.status || "adding") as ProgressStatus;
            patchRow(event.item_key, {
              status,
              ...(event.title ? { title: event.title } : {}),
              error: event.error,
            });
            return;
          }
          if (event.type === "done") {
            setProgressSummary((prev) => ({
              added: event.added ?? 0,
              skipped: event.skipped ?? 0,
              errors: event.errors ?? 0,
              message: event.message || prev?.message || "",
              warnings: event.warnings || prev?.warnings || [],
              done: true,
            }));
          }
        },
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : "Add failed";
      setError(message);
      setProgressRows((prev) =>
        (prev || []).map((row) =>
          row.status === "waiting" || row.status === "adding"
            ? { ...row, status: "error", error: message }
            : row,
        ),
      );
    } finally {
      setSubmitting(false);
      setProgressSummary((prev) => (prev ? { ...prev, done: true } : prev));
    }
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4">
      <div
        role="dialog"
        aria-modal="true"
        className={`w-full max-w-xl max-h-[90vh] overflow-y-auto rounded-xl border p-4 ${theme.blockCard}`}
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className={theme.heading}>Add to {arrLabel}</h2>
            <p className={`mt-1 ${theme.muted}`}>
              {props.items.length} title{props.items.length === 1 ? "" : "s"} — membership in Plex updates after ARR
              webhook and Placeholdarr sync.
            </p>
          </div>
          <button type="button" onClick={props.onClose} className={theme.cancelButton}>
            Close
          </button>
        </div>

        {loading ? <p className={`mt-4 ${theme.muted}`}>Loading instance options…</p> : null}
        {error ? <p className="mt-3 text-[13px] text-red-300">{error}</p> : null}

        {!loading && !progressRows ? (
          <div className="mt-4 flex flex-col gap-4">
            <div>
              <div className={`${theme.sectionLabel} mb-2`}>Instances</div>
              {instances.length ? (
                <div className="flex flex-col gap-2">
                  {instances.map((row) => (
                    <label key={row.instance_key} className={`flex items-center gap-2 ${theme.label}`}>
                      <input
                        type="checkbox"
                        checked={selectedKeys.includes(row.instance_key)}
                        onChange={() => toggleInstance(row.instance_key)}
                      />
                      {row.label}
                    </label>
                  ))}
                </div>
              ) : (
                <p className={theme.muted}>No {arrLabel} instances configured.</p>
              )}
            </div>

            {selectedKeys.map((key) => {
              const row = instances.find((item) => item.instance_key === key);
              if (!row) return null;
              return (
                <div key={key} className="flex flex-col gap-2">
                  <div className={theme.sectionLabel}>{row.label}</div>
                  <label className={`flex flex-col gap-1 ${theme.label}`}>
                    Quality profile
                    <select
                      className={theme.selectField}
                      value={profiles[key] ?? ""}
                      onChange={(event) =>
                        setProfiles((prev) => ({ ...prev, [key]: Number(event.target.value) }))
                      }
                    >
                      <option value="">Select profile</option>
                      {row.quality_profiles.map((profile) => (
                        <option key={profile.id} value={profile.id}>
                          {profile.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className={`flex flex-col gap-1 ${theme.label}`}>
                    Root folder
                    <select
                      className={theme.selectField}
                      value={roots[key] ?? ""}
                      onChange={(event) => setRoots((prev) => ({ ...prev, [key]: event.target.value }))}
                    >
                      <option value="">Select folder</option>
                      {row.root_folders.map((folder) => (
                        <option key={folder.path} value={folder.path}>
                          {folder.path}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              );
            })}

            <label className="flex items-center justify-between gap-3">
              <span className="flex flex-col">
                <span className={theme.label}>{props.mediaType === "movie" ? "Monitor movie" : "Monitor series"}</span>
                <span className={theme.muted}>
                  {props.mediaType === "movie"
                    ? "Radarr will monitor this title."
                    : "Sonarr will monitor the series (all seasons except specials)."}
                </span>
              </span>
              <ToggleSwitch
                checked={monitored}
                onChange={setMonitored}
                accentHex={props.accentHex}
                ariaLabel={props.mediaType === "movie" ? "Monitor movie" : "Monitor series"}
              />
            </label>
            <label className="flex items-center justify-between gap-3">
              <span className="flex flex-col">
                <span className={theme.label}>Search right away</span>
                <span className={theme.muted}>
                  {props.mediaType === "movie"
                    ? "Off by default so Placeholdarr can create placeholders instead of a bulk grab."
                    : "Searches for missing episodes. Off by default so Placeholdarr can create placeholders instead of a bulk grab."}
                </span>
              </span>
              <ToggleSwitch
                checked={search}
                onChange={setSearch}
                accentHex={props.accentHex}
                ariaLabel="Search right away"
              />
            </label>
            <div className={`flex flex-col gap-1 ${theme.label}`}>
              Tags
              <div className={`${theme.field} flex min-h-[2.25rem] flex-wrap items-center gap-1.5`}>
                {tags.map((tag) => (
                  <span
                    key={tag}
                    className="inline-flex items-center gap-1 rounded-md border border-current/20 bg-black/10 px-1.5 py-0.5 text-[12px]"
                  >
                    {tag}
                    <button
                      type="button"
                      className="leading-none opacity-70 hover:opacity-100"
                      aria-label={`Remove tag ${tag}`}
                      onClick={() => setTags((prev) => prev.filter((item) => item !== tag))}
                    >
                      ×
                    </button>
                  </span>
                ))}
                <input
                  className="min-w-[8rem] flex-1 bg-transparent outline-none"
                  value={tagDraft}
                  placeholder={tags.length ? "Add another, then Enter" : "Type a tag and press Enter"}
                  onChange={(event) => setTagDraft(event.target.value)}
                  onKeyDown={onTagKeyDown}
                  onBlur={commitDraft}
                />
              </div>
              {draftPreview ? (
                <span className={theme.muted}>Saved as {draftPreview} (spaces become dashes)</span>
              ) : (
                <span className={theme.muted}>Press Enter to add a tag. Spaces become dashes, like in {arrLabel}.</span>
              )}
            </div>

            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => void submit()}
              className="rounded-lg px-5 py-2 text-[14px] font-headline uppercase tracking-wider text-[#0a0e14] transition-opacity disabled:opacity-40"
              style={{ backgroundColor: props.accentHex }}
            >
              {`Add to ${arrLabel}`}
            </button>
          </div>
        ) : null}

        {progressRows ? (
          <div className="mt-4 flex flex-col gap-3">
            <p className={theme.muted}>
              {progressSummary?.done
                ? progressSummary.message
                : `Adding to ${arrLabel}. Status updates as each title lands in the library.`}
            </p>
            <p className={theme.label}>
              {progressRows.filter((row) => row.status === "ok").length} added
              {" · "}
              {progressRows.filter((row) => row.status === "skipped").length} already in {arrLabel}
              {" · "}
              {progressRows.filter((row) => row.status === "error").length} errors
              {!progressSummary?.done
                ? ` · ${progressRows.filter((row) => row.status === "adding" || row.status === "waiting").length} remaining`
                : ""}
            </p>
            {progressSummary?.warnings.length ? (
              <ul className={`text-[12px] ${theme.muted}`}>
                {progressSummary.warnings.map((warning, idx) => (
                  <li key={`${warning}-${idx}`}>{warning}</li>
                ))}
              </ul>
            ) : null}
            <ul className="max-h-[50vh] divide-y divide-white/10 overflow-y-auto">
              {progressRows.map((row) => (
                <li key={row.key} className="flex items-start justify-between gap-3 py-2 text-[13px]">
                  <span className={theme.label}>
                    {row.title}
                    {selectedKeys.length > 1 ? (
                      <span className={`ml-2 ${theme.muted}`}>{row.instanceLabel}</span>
                    ) : null}
                  </span>
                  <StatusMark status={row.status} arrLabel={arrLabel} error={row.error} />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  );
}
