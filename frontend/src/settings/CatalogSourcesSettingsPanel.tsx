import { useCallback, useEffect, useMemo, useState } from "react";
import { getCollectionTmdbMeta } from "../api/collections";
import {
  createDiscoverSource,
  deleteDiscoverSource,
  listDiscoverSources,
  patchDiscoverSource,
  runDiscoverPipeline,
  runDiscoverSource,
  type CatalogSource,
} from "../api/discover";
import { ToggleSwitch } from "../ToggleSwitch";

type Props = {
  enabled: boolean;
  accentHex?: string;
};

const SOURCE_TYPES: Array<{ value: string; label: string }> = [
  { value: "tmdb_popular", label: "Popular" },
  { value: "tmdb_trending", label: "Trending" },
  { value: "tmdb_upcoming", label: "Upcoming" },
  { value: "tmdb_discover", label: "Discover (filters)" },
  { value: "tmdb_list", label: "TMDB list" },
];

const SORT_OPTIONS = [
  { value: "popularity.desc", label: "Popularity" },
  { value: "vote_average.desc", label: "Rating" },
  { value: "primary_release_date.desc", label: "Release date" },
  { value: "revenue.desc", label: "Revenue" },
];

type Draft = {
  name: string;
  source_type: string;
  media_type: "movie" | "tv";
  limit: number;
  window: "day" | "week";
  list_id: string;
  genre_ids: number[];
  year_from: string;
  year_to: string;
  min_vote_average: string;
  sort_by: string;
  watch_region: string;
  enabled: boolean;
};

function emptyDraft(): Draft {
  return {
    name: "",
    source_type: "tmdb_popular",
    media_type: "movie",
    limit: 200,
    window: "week",
    list_id: "",
    genre_ids: [],
    year_from: "",
    year_to: "",
    min_vote_average: "",
    sort_by: "popularity.desc",
    watch_region: "US",
    enabled: true,
  };
}

function draftFromSource(src: CatalogSource): Draft {
  const f = src.filters_json || {};
  const media = src.media_type === "tv" || src.media_type === "series" ? "tv" : "movie";
  return {
    name: src.name,
    source_type: src.source_type,
    media_type: media,
    limit: Math.max(1, Math.min(10000, Number(f.limit) || 200)),
    window: f.window === "day" ? "day" : "week",
    list_id: String(f.list_id || f.id || ""),
    genre_ids: Array.isArray(f.genre_ids) ? f.genre_ids.map(Number).filter((n) => !Number.isNaN(n)) : [],
    year_from: f.year_from != null ? String(f.year_from) : "",
    year_to: f.year_to != null ? String(f.year_to) : "",
    min_vote_average: f.min_vote_average != null ? String(f.min_vote_average) : "",
    sort_by: String(f.sort_by || "popularity.desc"),
    watch_region: String(f.watch_region || "US"),
    enabled: Boolean(src.enabled),
  };
}

function filtersFromDraft(d: Draft): Record<string, unknown> {
  const filters: Record<string, unknown> = {
    limit: Math.max(1, Math.min(10000, Number(d.limit) || 200)),
  };
  if (d.source_type === "tmdb_trending") {
    filters.window = d.window;
  }
  if (d.source_type === "tmdb_list") {
    filters.list_id = d.list_id.trim();
  }
  if (d.source_type === "tmdb_discover") {
    if (d.genre_ids.length) filters.genre_ids = d.genre_ids;
    if (d.year_from.trim()) filters.year_from = Number(d.year_from);
    if (d.year_to.trim()) filters.year_to = Number(d.year_to);
    if (d.min_vote_average.trim()) filters.min_vote_average = Number(d.min_vote_average);
    filters.sort_by = d.sort_by;
    filters.watch_region = d.watch_region || "US";
  }
  return filters;
}

export function CatalogSourcesSettingsPanel({ enabled, accentHex = "#FBBF24" }: Props) {
  const [sources, setSources] = useState<CatalogSource[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [genres, setGenres] = useState<Array<{ id: number; name: string }>>([]);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await listDiscoverSources();
      setSources(res.sources || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void refresh();
  }, [enabled, refresh]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void getCollectionTmdbMeta(draft.media_type, draft.watch_region || "US")
      .then((meta) => {
        if (!cancelled) setGenres(meta.genres || []);
      })
      .catch(() => {
        if (!cancelled) setGenres([]);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, draft.watch_region, draft.media_type]);

  const editingSource = useMemo(
    () => (typeof editingId === "number" ? sources.find((s) => s.id === editingId) : null),
    [editingId, sources],
  );

  if (!enabled) return null;

  async function saveDraft() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const name = draft.name.trim() || SOURCE_TYPES.find((t) => t.value === draft.source_type)?.label || "Source";
      const filters_json = filtersFromDraft(draft);
      if (draft.source_type === "tmdb_list" && !String(filters_json.list_id || "").trim()) {
        throw new Error("TMDB list sources need a list ID");
      }
      if (editingId === "new") {
        await createDiscoverSource({
          name,
          source_type: draft.source_type,
          media_type: draft.media_type,
          filters_json,
          enabled: draft.enabled,
        });
        setMessage("Source created");
      } else if (typeof editingId === "number") {
        await patchDiscoverSource(editingId, {
          name,
          source_type: draft.source_type,
          filters_json,
          enabled: draft.enabled,
        });
        setMessage("Source saved");
      }
      setEditingId(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="mx-6 mb-5 rounded-xl border border-[#424753]/40 bg-[#1a222d] overflow-hidden">
      <div className="border-b border-[#424753]/30 px-5 py-4 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-[15px] font-headline font-bold uppercase tracking-wide text-white">Catalog sources</h3>
          <p className="ui-field-description mt-1">
            Configure TMDB movie and TV sources (limit up to 10,000; fetches paginate with rate limiting). TV uses
            show-level stubs (one dummy episode). Run a source or use Activity → Tasks → Discover catalog sync for
            placeholders, NFOs, and posters.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setDraft(emptyDraft());
              setEditingId("new");
              setMessage(null);
              setError(null);
            }}
            className="px-3 py-1.5 rounded-lg border border-[#424753]/50 text-[12px] font-headline uppercase tracking-wider text-slate-200 hover:bg-[#252e3a] disabled:opacity-40"
          >
            Add source
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void refresh()}
            className="px-3 py-1.5 rounded-lg border border-[#424753]/50 text-[12px] font-headline uppercase tracking-wider text-slate-200 hover:bg-[#252e3a] disabled:opacity-40"
          >
            Refresh
          </button>
        </div>
      </div>

      <div className="px-5 py-4 space-y-3">
        {error ? <div className="text-red-400 text-[14px]">{error}</div> : null}
        {message ? <div className="text-green-400 text-[14px]">{message}</div> : null}

        {sources.length === 0 ? (
          <p className="text-[14px] text-slate-500">No sources yet. Add Popular, Trending, Upcoming, Discover, or a TMDB list.</p>
        ) : null}

        {sources.map((src) => {
          const limit = Number((src.filters_json || {}).limit) || 200;
          const stats = src.last_run_stats || {};
          return (
            <div
              key={src.id}
              className="rounded-lg border border-[#424753]/30 px-3 py-3 space-y-2"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[14px] text-slate-100 font-medium truncate">{src.name}</div>
                  <div className="text-[12px] text-slate-500">
                    {src.media_type === "tv" ? "TV" : "Movies"} · {src.source_type} · limit {limit}
                    {src.last_run_at ? ` · last run fetched ${String(stats.fetched ?? "—")}` : ""}
                  </div>
                </div>
                <label className={`flex items-center gap-2 select-none shrink-0 ${busy ? "opacity-50" : ""}`}>
                  <ToggleSwitch
                    checked={Boolean(src.enabled)}
                    disabled={busy}
                    accentHex={accentHex}
                    ariaLabel={`Enable ${src.name}`}
                    onChange={(next) => {
                      void (async () => {
                        setBusy(true);
                        setError(null);
                        try {
                          await patchDiscoverSource(src.id, { enabled: next });
                          await refresh();
                        } catch (err) {
                          setError(err instanceof Error ? err.message : String(err));
                          setBusy(false);
                        }
                      })();
                    }}
                  />
                </label>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setDraft(draftFromSource(src));
                    setEditingId(src.id);
                    setMessage(null);
                    setError(null);
                  }}
                  className="px-2.5 py-1 rounded-md border border-[#424753]/50 text-[12px] font-headline uppercase tracking-wider text-slate-300 hover:bg-[#252e3a] disabled:opacity-40"
                >
                  Edit
                </button>
                <button
                  type="button"
                  disabled={busy || !src.enabled}
                  onClick={() => {
                    void (async () => {
                      setBusy(true);
                      setError(null);
                      setMessage(null);
                      try {
                        await runDiscoverSource(src.id);
                        setMessage(`Started run for ${src.name}. Watch Activity → Tasks for progress.`);
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                      } finally {
                        setBusy(false);
                      }
                    })();
                  }}
                  className="px-2.5 py-1 rounded-md border border-[#424753]/50 text-[12px] font-headline uppercase tracking-wider text-slate-300 hover:bg-[#252e3a] disabled:opacity-40"
                >
                  Run now
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    void (async () => {
                      if (!window.confirm(`Delete source “${src.name}”?`)) return;
                      setBusy(true);
                      setError(null);
                      try {
                        await deleteDiscoverSource(src.id);
                        if (editingId === src.id) setEditingId(null);
                        await refresh();
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                        setBusy(false);
                      }
                    })();
                  }}
                  className="px-2.5 py-1 rounded-md border border-red-500/40 text-[12px] font-headline uppercase tracking-wider text-red-300 hover:bg-red-500/10 disabled:opacity-40"
                >
                  Delete
                </button>
              </div>
            </div>
          );
        })}

        {editingId != null ? (
          <div className="rounded-lg border border-[#424753]/50 bg-[#121822] px-4 py-4 space-y-3">
            <div className="text-[13px] font-headline uppercase tracking-wider text-slate-300">
              {editingId === "new" ? "New source" : `Edit ${editingSource?.name || "source"}`}
            </div>
            <label className="block text-[13px] text-slate-400">
              Name
              <input
                className="mt-1 w-full rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                value={draft.name}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                placeholder="e.g. Popular movies"
              />
            </label>
            <div className="flex flex-wrap gap-3">
              <label className="block text-[13px] text-slate-400">
                Media
                <select
                  className="mt-1 block rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                  value={draft.media_type}
                  disabled={editingId !== "new"}
                  onChange={(e) =>
                    setDraft((d) => ({
                      ...d,
                      media_type: e.target.value === "tv" ? "tv" : "movie",
                      genre_ids: [],
                    }))
                  }
                >
                  <option value="movie">Movies</option>
                  <option value="tv">TV (show-level)</option>
                </select>
              </label>
              <label className="block text-[13px] text-slate-400">
                Type
                <select
                  className="mt-1 block rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                  value={draft.source_type}
                  onChange={(e) => setDraft((d) => ({ ...d, source_type: e.target.value }))}
                >
                  {SOURCE_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-[13px] text-slate-400">
                Max titles
                <input
                  type="number"
                  min={1}
                  max={10000}
                  className="mt-1 block w-28 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                  value={draft.limit}
                  onChange={(e) => setDraft((d) => ({ ...d, limit: Number(e.target.value) || 200 }))}
                />
              </label>
              {draft.source_type === "tmdb_trending" ? (
                <label className="block text-[13px] text-slate-400">
                  Window
                  <select
                    className="mt-1 block rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                    value={draft.window}
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, window: e.target.value === "day" ? "day" : "week" }))
                    }
                  >
                    <option value="week">Week</option>
                    <option value="day">Day</option>
                  </select>
                </label>
              ) : null}
            </div>

            {draft.source_type === "tmdb_list" ? (
              <label className="block text-[13px] text-slate-400">
                TMDB list ID
                <input
                  className="mt-1 w-full rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                  value={draft.list_id}
                  onChange={(e) => setDraft((d) => ({ ...d, list_id: e.target.value }))}
                  placeholder="Numeric list id from themoviedb.org"
                />
              </label>
            ) : null}

            {draft.source_type === "tmdb_discover" ? (
              <div className="space-y-3">
                <label className="block text-[13px] text-slate-400">
                  Sort
                  <select
                    className="mt-1 block rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                    value={draft.sort_by}
                    onChange={(e) => setDraft((d) => ({ ...d, sort_by: e.target.value }))}
                  >
                    {SORT_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
                <div>
                  <div className="text-[12px] font-headline uppercase tracking-widest text-slate-500 mb-1.5">Genres</div>
                  <div className="flex flex-wrap gap-1.5">
                    {genres.map((g) => {
                      const on = draft.genre_ids.includes(g.id);
                      return (
                        <button
                          key={g.id}
                          type="button"
                          onClick={() =>
                            setDraft((d) => ({
                              ...d,
                              genre_ids: on
                                ? d.genre_ids.filter((id) => id !== g.id)
                                : [...d.genre_ids, g.id],
                            }))
                          }
                          className={`rounded-full px-2.5 py-1 text-[12px] border ${
                            on
                              ? "border-yellow-500/50 bg-yellow-500/15 text-yellow-100"
                              : "border-[#424753]/40 text-slate-400 hover:border-[#424753]/70"
                          }`}
                        >
                          {g.name}
                        </button>
                      );
                    })}
                    {!genres.length ? <span className="text-[12px] text-slate-500">Loading genres…</span> : null}
                  </div>
                </div>
                <div className="flex flex-wrap gap-3">
                  <label className="block text-[13px] text-slate-400">
                    Year from
                    <input
                      className="mt-1 block w-24 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                      value={draft.year_from}
                      onChange={(e) => setDraft((d) => ({ ...d, year_from: e.target.value }))}
                      placeholder="2000"
                    />
                  </label>
                  <label className="block text-[13px] text-slate-400">
                    Year to
                    <input
                      className="mt-1 block w-24 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                      value={draft.year_to}
                      onChange={(e) => setDraft((d) => ({ ...d, year_to: e.target.value }))}
                      placeholder="2026"
                    />
                  </label>
                  <label className="block text-[13px] text-slate-400">
                    Min rating
                    <input
                      className="mt-1 block w-20 rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[14px] text-slate-200"
                      value={draft.min_vote_average}
                      onChange={(e) => setDraft((d) => ({ ...d, min_vote_average: e.target.value }))}
                      placeholder="7"
                    />
                  </label>
                </div>
              </div>
            ) : null}

            <div className="flex flex-wrap gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => void saveDraft()}
                className="px-3 py-1.5 rounded-lg text-[12px] font-headline uppercase tracking-wider text-black disabled:opacity-40"
                style={{ backgroundColor: accentHex }}
              >
                Save
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setEditingId(null)}
                className="px-3 py-1.5 rounded-lg border border-[#424753]/50 text-[12px] font-headline uppercase tracking-wider text-slate-300 hover:bg-[#252e3a]"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        <button
          type="button"
          disabled={busy || !sources.some((s) => s.enabled)}
          onClick={async () => {
            setBusy(true);
            setError(null);
            setMessage(null);
            try {
              await runDiscoverPipeline(false);
              setMessage("Discover catalog sync started. See Activity → Tasks for status.");
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
            } finally {
              setBusy(false);
            }
          }}
          className="px-3 py-1.5 rounded-lg border border-[#424753]/50 text-[12px] font-headline uppercase tracking-wider text-slate-200 hover:bg-[#252e3a] disabled:opacity-40"
        >
          Run all enabled sources
        </button>
      </div>
    </div>
  );
}
