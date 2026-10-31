import { useCallback, useEffect, useMemo, useState } from "react";
import type { Brand, ThemeMode } from "../brandTypes";
import { getBrandSemanticTokens } from "../brandSemanticTheme";
import { BrandCheckbox } from "../BrandCheckbox";
import { BrandSelect } from "../BrandSelect";
import { getCollectionArrAddOptions, normalizeArrTagLabel } from "../api/collections";
import {
  getWatchlist,
  postWatchlistAdd,
  postWatchlistExclude,
  postWatchlistSync,
  type WatchlistItem,
  type WatchlistSummary,
} from "../api/watchlist";
import { ToggleSwitch } from "../ToggleSwitch";
import type { CollectionArrAddInstanceOptions } from "../types/api";

type StripAccent = { hex: string; text: string; icon: string; hoverHex: string };
type StatusFilter = "all" | "new" | "in_arr" | "added" | "failed" | "excluded";

function statusLabel(status: string): string {
  if (status === "in_arr") return "In Arr";
  if (status === "new") return "New";
  if (status === "added") return "Added";
  if (status === "failed") return "Failed";
  if (status === "excluded") return "Excluded";
  return status;
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  try {
    return new Date(t).toLocaleString();
  } catch {
    return iso;
  }
}

export function WatchlistPanel(props: {
  brand: Brand;
  themeMode: ThemeMode;
  accent: StripAccent;
}) {
  const semantic = getBrandSemanticTokens(props.brand, props.themeMode, props.accent);
  const isLight = props.themeMode === "light";
  const panelBorder = isLight ? semantic.border : "rgba(66,71,83,0.4)";
  const panelBg = isLight ? semantic.surfacePanel : "#171c22";

  const [summary, setSummary] = useState<WatchlistSummary | null>(null);
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusFilter>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const [syncing, setSyncing] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await getWatchlist({ status: status === "all" ? undefined : status, limit: 1000 });
      setSummary(data.summary);
      setItems(data.items || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load watchlist");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((row) => {
      const hay = `${row.title} ${row.year ?? ""} ${row.media_type} ${row.status}`.toLowerCase();
      return hay.includes(q);
    });
  }, [items, query]);

  const selectedIds = useMemo(
    () => filtered.filter((row) => selected[row.id]).map((row) => row.id),
    [filtered, selected],
  );

  const toggleAll = (on: boolean) => {
    const next: Record<number, boolean> = { ...selected };
    for (const row of filtered) next[row.id] = on;
    setSelected(next);
  };

  const onRefresh = async () => {
    setSyncing(true);
    setError(null);
    try {
      await postWatchlistSync();
      // Give the background thread a moment, then poll.
      await new Promise((r) => setTimeout(r, 1200));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refresh failed");
    } finally {
      setSyncing(false);
    }
  };

  const onExclude = async (excluded: boolean) => {
    if (!selectedIds.length) return;
    setBusy(true);
    setError(null);
    try {
      await postWatchlistExclude(selectedIds, excluded);
      setSelected({});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Exclude update failed");
    } finally {
      setBusy(false);
    }
  };

  const counts = summary?.counts;

  return (
    <div>
      <div className="rounded-xl border overflow-hidden mb-6" style={{ borderColor: panelBorder, backgroundColor: panelBg }}>
        <div className="px-4 py-3 border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.3)" }}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-[20px] font-bold font-headline" style={{ color: isLight ? semantic.fg : "#fff" }}>
                Plex Watchlist
              </h2>
              <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
                Owner Universal Watchlist from Plex. Add titles to Arr so Placeholdarr can create placeholders.
              </p>
            </div>
            <button
              type="button"
              disabled={syncing}
              onClick={() => void onRefresh()}
              className="px-3 py-1.5 rounded-lg text-[12px] uppercase tracking-wider disabled:opacity-50"
              style={{ backgroundColor: props.accent.hex, color: semantic.fgOnAccent }}
            >
              {syncing ? "Refreshing…" : "Refresh now"}
            </button>
          </div>
          <div className="mt-3 flex flex-wrap gap-3 text-[13px]" style={{ color: semantic.fgMuted }}>
            <span>
              Sync:{" "}
              <strong style={{ color: semantic.fg }}>{summary?.enabled ? "Enabled" : "Disabled"}</strong>
            </span>
            <span>
              Token:{" "}
              <strong style={{ color: semantic.fg }}>{summary?.has_token ? "Present" : "Missing"}</strong>
            </span>
            <span>
              When new: <strong style={{ color: semantic.fg }}>{summary?.on_new || "manual"}</strong>
            </span>
            <span>Last poll: {formatWhen(summary?.last_poll_at ?? null)}</span>
            {counts ? (
              <span>
                {counts.new} new · {counts.in_arr} in Arr · {counts.added} added · {counts.failed} failed ·{" "}
                {counts.excluded} excluded
              </span>
            ) : null}
          </div>
          {summary?.last_error ? (
            <p className="mt-2 text-[13px] text-red-400">Last error: {summary.last_error}</p>
          ) : null}
        </div>

        <div
          className="px-4 py-3 border-b flex flex-wrap items-center gap-2"
          style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.2)" }}
        >
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search titles"
            className="px-3 py-1.5 rounded-lg border bg-transparent text-[14px] min-w-[12rem]"
            style={{ borderColor: semantic.border, color: semantic.fg }}
          />
          {(
            [
              ["all", "All"],
              ["new", "New"],
              ["in_arr", "In Arr"],
              ["added", "Added"],
              ["failed", "Failed"],
              ["excluded", "Excluded"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setStatus(value)}
              className="px-2.5 py-1 rounded-lg border text-[12px] uppercase tracking-wider"
              style={{
                borderColor: status === value ? props.accent.hex : semantic.border,
                color: status === value ? props.accent.hex : semantic.fgMuted,
              }}
            >
              {label}
            </button>
          ))}
          <div className="ml-auto flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!selectedIds.length || busy}
              onClick={() => setAddOpen(true)}
              className="px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider disabled:opacity-40"
              style={{ borderColor: semantic.border, color: semantic.fg }}
            >
              Add to Arr ({selectedIds.length})
            </button>
            <button
              type="button"
              disabled={!selectedIds.length || busy}
              onClick={() => void onExclude(true)}
              className="px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider disabled:opacity-40"
              style={{ borderColor: semantic.border, color: semantic.fgMuted }}
            >
              Exclude
            </button>
            <button
              type="button"
              disabled={!selectedIds.length || busy}
              onClick={() => void onExclude(false)}
              className="px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider disabled:opacity-40"
              style={{ borderColor: semantic.border, color: semantic.fgMuted }}
            >
              Un-exclude
            </button>
          </div>
        </div>

        {error ? <div className="px-4 py-2 text-[13px] text-red-400">{error}</div> : null}

        <div className="overflow-x-auto">
          <table className="min-w-[720px] w-full">
            <thead>
              <tr className="border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.2)" }}>
                <th className="px-3 py-2 w-10">
                  <BrandCheckbox
                    checked={filtered.length > 0 && filtered.every((r) => selected[r.id])}
                    onChange={(v) => toggleAll(v)}
                    accentHex={props.accent.hex}
                    ariaLabel="Select all"
                  />
                </th>
                {["Title", "Type", "Status", "Last seen", "Error"].map((h) => (
                  <th
                    key={h}
                    className="px-3 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal"
                    style={{ color: semantic.fgSubtle }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={6} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                    Loading…
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                    No watchlist titles yet. Enable sync in Settings → Watching, then Refresh now.
                  </td>
                </tr>
              ) : (
                filtered.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b last:border-0"
                    style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.15)" }}
                  >
                    <td className="px-3 py-3">
                      <BrandCheckbox
                        checked={Boolean(selected[row.id])}
                        onChange={(v) => setSelected((prev) => ({ ...prev, [row.id]: v }))}
                        accentHex={props.accent.hex}
                        ariaLabel={`Select ${row.title}`}
                      />
                    </td>
                    <td className="px-3 py-3 text-[15px] font-medium" style={{ color: semantic.fg }}>
                      {row.title}
                      {row.year ? <span style={{ color: semantic.fgSubtle }}> ({row.year})</span> : null}
                    </td>
                    <td className="px-3 py-3 text-[14px] capitalize" style={{ color: semantic.fgMuted }}>
                      {row.media_type === "show" ? "TV" : row.media_type}
                    </td>
                    <td className="px-3 py-3 text-[14px]" style={{ color: semantic.fgMuted }}>
                      {statusLabel(row.status)}
                    </td>
                    <td className="px-3 py-3 text-[13px] whitespace-nowrap" style={{ color: semantic.fgSubtle }}>
                      {formatWhen(row.last_seen_at)}
                    </td>
                    <td className="px-3 py-3 text-[13px] max-w-[16rem] truncate" style={{ color: "#f87171" }}>
                      {row.last_error || ""}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {addOpen ? (
        <WatchlistAddModal
          itemIds={selectedIds}
          items={items.filter((i) => selectedIds.includes(i.id))}
          defaultSearch={summary?.on_new === "add_and_search"}
          defaultTvScope={summary?.tv_request_scope || "episode"}
          defaultTag={summary?.arr_tags || "placeholdarr-watchlist"}
          seedRadarrTargets={summary?.radarr_targets || []}
          seedSonarrTargets={summary?.sonarr_targets || []}
          accentHex={props.accent.hex}
          fgOnAccent={semantic.fgOnAccent}
          onClose={() => setAddOpen(false)}
          onDone={async () => {
            setAddOpen(false);
            setSelected({});
            await load();
          }}
        />
      ) : null}
    </div>
  );
}

function WatchlistAddModal(props: {
  itemIds: number[];
  items: WatchlistItem[];
  defaultSearch: boolean;
  defaultTvScope: string;
  defaultTag: string;
  seedRadarrTargets: Array<{
    instance_key: string;
    quality_profile_id?: number | null;
    root_folder_path?: string | null;
  }>;
  seedSonarrTargets: Array<{
    instance_key: string;
    quality_profile_id?: number | null;
    root_folder_path?: string | null;
  }>;
  accentHex: string;
  fgOnAccent: string;
  onClose: () => void;
  onDone: () => Promise<void> | void;
}) {
  const hasMovies = props.items.some((i) => i.media_type === "movie");
  const hasShows = props.items.some((i) => i.media_type === "show");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [radarrInstances, setRadarrInstances] = useState<CollectionArrAddInstanceOptions[]>([]);
  const [sonarrInstances, setSonarrInstances] = useState<CollectionArrAddInstanceOptions[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [profiles, setProfiles] = useState<Record<string, number>>({});
  const [roots, setRoots] = useState<Record<string, string>>({});
  const [monitored, setMonitored] = useState(true);
  const [search, setSearch] = useState(props.defaultSearch);
  const [tvScope, setTvScope] = useState(props.defaultTvScope || "episode");
  const [tags, setTags] = useState(() => {
    const seed = normalizeArrTagLabel(props.defaultTag);
    return seed ? [seed] : [];
  });
  const [tagDraft, setTagDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const applySeed = (
      seed: typeof props.seedRadarrTargets,
      instances: CollectionArrAddInstanceOptions[],
      nextKeys: string[],
      nextProfiles: Record<string, number>,
      nextRoots: Record<string, string>,
    ) => {
      const byKey = new Map(instances.map((row) => [row.instance_key, row]));
      // Only seed instances the user already configured. Never assume first profile/root.
      for (const t of seed) {
        const key = String(t.instance_key || "").trim();
        if (!byKey.has(key)) continue;
        nextKeys.push(key);
        const qp = Number(t.quality_profile_id || 0);
        if (qp > 0) nextProfiles[key] = qp;
        const root = String(t.root_folder_path || "").trim();
        if (root) nextRoots[key] = root;
      }
    };

    void (async () => {
      try {
        const [movieData, showData] = await Promise.all([
          hasMovies ? getCollectionArrAddOptions("movie") : Promise.resolve(null),
          hasShows ? getCollectionArrAddOptions("show") : Promise.resolve(null),
        ]);
        if (cancelled) return;
        const radarr = movieData?.instances || [];
        const sonarr = showData?.instances || [];
        setRadarrInstances(radarr);
        setSonarrInstances(sonarr);
        const nextKeys: string[] = [];
        const nextProfiles: Record<string, number> = {};
        const nextRoots: Record<string, string> = {};
        if (hasMovies) applySeed(props.seedRadarrTargets, radarr, nextKeys, nextProfiles, nextRoots);
        if (hasShows) applySeed(props.seedSonarrTargets, sonarr, nextKeys, nextProfiles, nextRoots);
        setSelectedKeys(nextKeys);
        setProfiles(nextProfiles);
        setRoots(nextRoots);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load Arr options");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [hasMovies, hasShows, props.seedRadarrTargets, props.seedSonarrTargets]);

  const canSubmit = useMemo(() => {
    if (submitting || loading) return false;
    const radarrKeySet = new Set(radarrInstances.map((row) => row.instance_key));
    const sonarrKeySet = new Set(sonarrInstances.map((row) => row.instance_key));
    if (hasMovies && !selectedKeys.some((key) => radarrKeySet.has(key))) return false;
    if (hasShows && !selectedKeys.some((key) => sonarrKeySet.has(key))) return false;
    return selectedKeys.every((key) => Boolean(profiles[key]) && Boolean(roots[key]));
  }, [
    selectedKeys,
    profiles,
    roots,
    submitting,
    loading,
    hasMovies,
    hasShows,
    radarrInstances,
    sonarrInstances,
  ]);

  const toggleInstance = (key: string) => {
    setSelectedKeys((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };

  const commitTag = () => {
    const label = normalizeArrTagLabel(tagDraft);
    if (!label) return;
    setTags((prev) => (prev.includes(label) ? prev : [...prev, label]));
    setTagDraft("");
  };

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await postWatchlistAdd({
        item_ids: props.itemIds,
        search,
        monitored,
        tags,
        tv_scope: hasShows ? tvScope : undefined,
        targets: selectedKeys.map((key) => ({
          instance_key: key,
          quality_profile_id: profiles[key] || undefined,
          root_folder_path: roots[key] || undefined,
        })),
      });
      await props.onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Add to Arr failed");
    } finally {
      setSubmitting(false);
    }
  };

  const renderInstanceGroup = (
    label: string,
    instances: CollectionArrAddInstanceOptions[],
  ) => {
    if (!instances.length) {
      return <p className="text-[13px] text-slate-500">No {label} instances configured.</p>;
    }
    return (
      <div className="space-y-2">
        <div className="text-[12px] uppercase tracking-widest text-slate-500">{label}</div>
        {instances.map((row) => {
          const on = selectedKeys.includes(row.instance_key);
          return (
            <div
              key={row.instance_key}
              className="rounded-lg border border-[#424753]/40 bg-[#12161c] px-3 py-2 space-y-2"
            >
              <label className="flex items-center gap-2 text-[14px] text-slate-200">
                <BrandCheckbox
                  checked={on}
                  size="sm"
                  accentHex={props.accentHex}
                  ariaLabel={row.label || row.instance_key}
                  onChange={() => toggleInstance(row.instance_key)}
                />
                <span className="min-w-0 truncate">{row.label || row.instance_key}</span>
              </label>
              {on ? (
                <div className="grid gap-2 pl-6">
                  <label className="block space-y-1">
                    <span className="flex items-center gap-2 flex-wrap">
                      <span className="text-[12px] uppercase tracking-widest text-slate-500">Quality profile</span>
                      {!profiles[row.instance_key] ? (
                        <span
                          className="px-1.5 py-0.5 rounded text-[11px] font-bold font-headline uppercase"
                          style={{ backgroundColor: `${props.accentHex}4d`, color: props.accentHex }}
                        >
                          Required
                        </span>
                      ) : null}
                    </span>
                    <BrandSelect
                      fullWidth
                      value={profiles[row.instance_key] ? String(profiles[row.instance_key]) : ""}
                      options={row.quality_profiles.map((p) => ({
                        value: String(p.id),
                        label: p.name,
                      }))}
                      onChange={(v) =>
                        setProfiles((prev) => ({ ...prev, [row.instance_key]: Number(v) }))
                      }
                      ariaLabel={`${row.label} quality profile`}
                    />
                  </label>
                  <label className="block space-y-1">
                    <span className="flex items-center gap-2 flex-wrap">
                      <span className="text-[12px] uppercase tracking-widest text-slate-500">Root folder</span>
                      {!roots[row.instance_key] ? (
                        <span
                          className="px-1.5 py-0.5 rounded text-[11px] font-bold font-headline uppercase"
                          style={{ backgroundColor: `${props.accentHex}4d`, color: props.accentHex }}
                        >
                          Required
                        </span>
                      ) : null}
                    </span>
                    <BrandSelect
                      fullWidth
                      value={roots[row.instance_key] || ""}
                      options={row.root_folders.map((r) => ({
                        value: r.path,
                        label: r.path,
                      }))}
                      onChange={(v) => setRoots((prev) => ({ ...prev, [row.instance_key]: v }))}
                      ariaLabel={`${row.label} root folder`}
                    />
                  </label>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
      <button type="button" className="absolute inset-0 bg-black/70" aria-label="Close" onClick={props.onClose} />
      <div className="relative z-10 w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-2xl border border-[#424753]/50 bg-[#171c22] shadow-2xl p-5 space-y-4">
        <div>
          <h3 className="text-[18px] font-headline font-bold text-white">Add to Arr</h3>
          <p className="text-[13px] text-slate-400 mt-1">
            {props.itemIds.length} selected. Titles go to every instance you leave on.
            {hasMovies && hasShows ? " Movies use Radarr targets; TV uses Sonarr targets." : null}
          </p>
        </div>
        {loading ? (
          <p className="text-slate-400 text-[14px]">Loading Arr options…</p>
        ) : (
          <>
            {hasMovies ? renderInstanceGroup("Radarr", radarrInstances) : null}
            {hasShows ? renderInstanceGroup("Sonarr", sonarrInstances) : null}
            {hasShows ? (
              <label className="block space-y-1">
                <span className="text-[12px] uppercase tracking-widest text-slate-500">TV request scope</span>
                <BrandSelect
                  value={tvScope}
                  onChange={setTvScope}
                  options={[
                    { value: "episode", label: "First episode + lookahead" },
                    { value: "season", label: "First season" },
                    { value: "series", label: "Entire series" },
                  ]}
                  fullWidth
                />
              </label>
            ) : null}
            <div className="flex flex-wrap gap-6">
              <label className="flex items-center gap-2 text-[14px] text-slate-300">
                <ToggleSwitch checked={monitored} onChange={setMonitored} accentHex={props.accentHex} ariaLabel="Monitored" />
                Monitored
              </label>
              <label className="flex items-center gap-2 text-[14px] text-slate-300">
                <ToggleSwitch checked={search} onChange={setSearch} accentHex={props.accentHex} ariaLabel="Search" />
                Search now
              </label>
            </div>
            <div>
              <div className="text-[12px] uppercase tracking-widest text-slate-500 mb-1">Tags</div>
              <div className="flex flex-wrap gap-1.5 mb-2">
                {tags.map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTags((prev) => prev.filter((x) => x !== t))}
                    className="px-2 py-0.5 rounded-full border border-[#424753]/50 text-[12px] text-slate-300"
                  >
                    {t} ×
                  </button>
                ))}
              </div>
              <div className="flex gap-2">
                <input
                  value={tagDraft}
                  onChange={(e) => setTagDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      commitTag();
                    }
                  }}
                  className="flex-1 px-3 py-1.5 rounded-lg border border-[#424753]/50 bg-transparent text-[14px] text-slate-200"
                  placeholder="Add tag"
                />
                <button
                  type="button"
                  onClick={commitTag}
                  className="px-3 py-1.5 rounded-lg border border-[#424753]/50 text-[13px] text-slate-300"
                >
                  Add
                </button>
              </div>
            </div>
          </>
        )}
        {error ? <p className="text-[13px] text-red-400">{error}</p> : null}
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={props.onClose}
            className="px-3 py-2 rounded-lg border border-[#424753]/50 text-[13px] text-slate-300"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!canSubmit}
            onClick={() => void submit()}
            className="px-3 py-2 rounded-lg text-[13px] font-medium disabled:opacity-50"
            style={{ backgroundColor: props.accentHex, color: props.fgOnAccent }}
          >
            {submitting ? "Adding…" : "Add"}
          </button>
        </div>
      </div>
    </div>
  );
}
