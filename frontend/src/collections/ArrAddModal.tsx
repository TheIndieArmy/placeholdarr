import { useEffect, useMemo, useState } from "react";
import { addCollectionTitlesToArr, getCollectionArrAddOptions } from "../api/collections";
import { ToggleSwitch } from "../ToggleSwitch";
import { useCollectionTheme } from "./collectionTheme";
import type {
  CollectionArrAddItem,
  CollectionArrAddInstanceOptions,
  CollectionArrAddResponse,
  CollectionMissingFromArrItem,
} from "../types/api";

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
  const [tag, setTag] = useState(props.defaultTag || "placeholdarr");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<CollectionArrAddResponse | null>(null);

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

  const toggleInstance = (key: string) => {
    setSelectedKeys((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };

  const submit = async () => {
    if (!canSubmit) return;
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
    try {
      const response = await addCollectionTitlesToArr({
        media_type: props.mediaType,
        items,
        instance_keys: selectedKeys,
        instance_options,
        monitored,
        search,
        tag: tag.trim(),
      });
      setResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Add failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4">
      <div
        role="dialog"
        aria-modal="true"
        className={`w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-xl border p-4 ${theme.blockCard}`}
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

        {!loading && !result ? (
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
            <label className={`flex flex-col gap-1 ${theme.label}`}>
              Tag
              <input className={theme.field} value={tag} onChange={(event) => setTag(event.target.value)} />
            </label>

            <button
              type="button"
              disabled={!canSubmit}
              onClick={() => void submit()}
              className="rounded-lg px-5 py-2 text-[14px] font-headline uppercase tracking-wider text-[#0a0e14] transition-opacity disabled:opacity-40"
              style={{ backgroundColor: props.accentHex }}
            >
              {submitting ? "Adding…" : `Add to ${arrLabel}`}
            </button>
          </div>
        ) : null}

        {result ? (
          <div className="mt-4 flex flex-col gap-2">
            <p className={theme.muted}>{result.message}</p>
            <p className={theme.label}>
              Added {result.added}, skipped {result.skipped}, errors {result.errors}
            </p>
            {result.results.some((row) => row.status === "error") ? (
              <ul className={`max-h-40 overflow-y-auto text-[12px] ${theme.muted}`}>
                {result.results
                  .filter((row) => row.status === "error")
                  .slice(0, 20)
                  .map((row, idx) => (
                    <li key={`${row.title}-${idx}`}>
                      {row.title}: {row.error}
                    </li>
                  ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
