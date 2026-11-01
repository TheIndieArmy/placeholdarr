import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Brand, ThemeMode } from "../brandTypes";
import { getBrandSemanticTokens } from "../brandSemanticTheme";
import { BrandCheckbox } from "../BrandCheckbox";
import {
  getWatchlist,
  postWatchlistAdd,
  postWatchlistExclude,
  postWatchlistSync,
  type WatchlistItem,
  type WatchlistItemUser,
  type WatchlistLibrary,
  type WatchlistSummary,
  type WatchlistUser,
} from "../api/watchlist";

type StripAccent = { hex: string; text: string; icon: string; hoverHex: string };
type WatchlistTab = "accepted" | "queue" | "blocklist";
type AcceptIntensity = "add_and_search" | "monitor_only" | "add_only";

const ACCEPT_OPTIONS: Array<{ value: AcceptIntensity; label: string; hint: string }> = [
  {
    value: "add_and_search",
    label: "Add, monitor, and search",
    hint: "Add to Arr, monitor, and start a search",
  },
  {
    value: "monitor_only",
    label: "Add and monitor",
    hint: "Add to Arr and monitor; no search",
  },
  {
    value: "add_only",
    label: "Add only",
    hint: "Add to Arr without monitoring or searching",
  },
];

function statusLabel(status: string): string {
  if (status === "new") return "New";
  if (status === "added") return "Accepted";
  if (status === "failed") return "Failed";
  if (status === "excluded") return "Rejected";
  if (status === "in_arr") return "New";
  return status;
}

function presenceLabel(state: string | undefined | null): string {
  if (state === "file") return "Real files";
  if (state === "placeholder") return "Placeholders";
  if (state === "missing") return "Missing";
  return "Not in library";
}

function tabForItem(row: WatchlistItem): WatchlistTab {
  if (row.excluded || row.status === "excluded") return "blocklist";
  const coverage = String(row.library?.coverage || "none");
  if (row.status === "added" || coverage === "complete") return "accepted";
  if (row.status === "new" || row.status === "failed" || row.status === "in_arr") return "queue";
  // Partial / none with unexpected status still needs action if not complete.
  if (coverage === "none" || coverage === "partial") return "queue";
  return "accepted";
}

function libraryPath(lib: WatchlistLibrary | null | undefined): string | null {
  if (!lib?.item_id) return null;
  const kind = lib.item_type === "series" || lib.item_type === "show" ? "series" : "movie";
  return `/library/${kind}/${lib.item_id}`;
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

function userDisplayName(u: { title?: string | null; username?: string | null; plex_user_id: string }): string {
  return String(u.title || u.username || u.plex_user_id || "User").trim() || "User";
}

function intensityToFlags(intensity: AcceptIntensity): { search: boolean; monitored: boolean } {
  if (intensity === "add_and_search") return { search: true, monitored: true };
  if (intensity === "add_only") return { search: false, monitored: false };
  return { search: false, monitored: true };
}

function UserAvatar(props: {
  thumbUrl?: string | null;
  title: string;
  size?: number;
  dimmed?: boolean;
}) {
  const size = props.size ?? 40;
  const initial = (props.title || "?").trim().charAt(0).toUpperCase() || "?";
  return (
    <div
      className="relative shrink-0 overflow-hidden rounded-full bg-[#1e2430] ring-1 ring-[#424753]/50"
      style={{ width: size, height: size, opacity: props.dimmed ? 0.45 : 1 }}
    >
      {props.thumbUrl ? (
        <img src={props.thumbUrl} alt="" className="h-full w-full object-cover" loading="lazy" />
      ) : (
        <span
          className="flex h-full w-full items-center justify-center text-[13px] font-headline font-semibold text-slate-300"
          aria-hidden
        >
          {initial}
        </span>
      )}
    </div>
  );
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
  const rowBorder = isLight ? semantic.border : "rgba(66,71,83,0.15)";
  const headBorder = isLight ? semantic.border : "rgba(66,71,83,0.2)";

  const [summary, setSummary] = useState<WatchlistSummary | null>(null);
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [users, setUsers] = useState<WatchlistUser[]>([]);
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<WatchlistTab>("queue");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const [syncing, setSyncing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [acceptMenuFor, setAcceptMenuFor] = useState<"bulk" | number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await getWatchlist({
        limit: 1000,
        userIds: selectedUserIds.length ? selectedUserIds : undefined,
      });
      setSummary(data.summary);
      setItems(data.items || []);
      setUsers(data.users || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load watchlist");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [selectedUserIds]);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  useEffect(() => {
    setSelected({});
    setAcceptMenuFor(null);
  }, [tab, selectedUserIds]);

  const toggleUserFilter = (plexUserId: string) => {
    setSelectedUserIds((prev) =>
      prev.includes(plexUserId) ? prev.filter((id) => id !== plexUserId) : [...prev, plexUserId],
    );
  };

  const searched = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((row) => {
      const userHay = (row.users || []).map((u) => `${u.title} ${u.username || ""}`).join(" ");
      const hay = `${row.title} ${row.year ?? ""} ${row.media_type} ${row.status} ${userHay}`.toLowerCase();
      return hay.includes(q);
    });
  }, [items, query]);

  const tabCounts = useMemo(() => {
    let accepted = 0;
    let queue = 0;
    let blocklist = 0;
    for (const row of searched) {
      const t = tabForItem(row);
      if (t === "accepted") accepted += 1;
      else if (t === "queue") queue += 1;
      else blocklist += 1;
    }
    return { accepted, queue, blocklist };
  }, [searched]);

  const filtered = useMemo(
    () => searched.filter((row) => tabForItem(row) === tab),
    [searched, tab],
  );

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
      await new Promise((r) => setTimeout(r, 1200));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refresh failed");
    } finally {
      setSyncing(false);
    }
  };

  const onReject = async (ids: number[]) => {
    if (!ids.length) return;
    setBusy(true);
    setError(null);
    setAcceptMenuFor(null);
    try {
      await postWatchlistExclude(ids, true);
      setSelected({});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Reject failed");
    } finally {
      setBusy(false);
    }
  };

  const onRestore = async (ids: number[]) => {
    if (!ids.length) return;
    setBusy(true);
    setError(null);
    try {
      await postWatchlistExclude(ids, false);
      setSelected({});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Restore failed");
    } finally {
      setBusy(false);
    }
  };

  const onAccept = async (ids: number[], intensity: AcceptIntensity) => {
    if (!ids.length) return;
    setBusy(true);
    setError(null);
    setAcceptMenuFor(null);
    const { search, monitored } = intensityToFlags(intensity);
    try {
      const result = await postWatchlistAdd({
        item_ids: ids,
        search,
        monitored,
        tv_scope: summary?.tv_request_scope || undefined,
      });
      if (result.failed && result.failed > 0 && !result.added) {
        setError(
          `Accept failed for ${result.failed} title${result.failed === 1 ? "" : "s"}. Check Watching Arr targets (profile and root are required).`,
        );
      }
      setSelected({});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Accept failed");
    } finally {
      setBusy(false);
    }
  };

  const emptyCopy =
    tab === "queue"
      ? "No requests waiting. New watchlist titles appear here for Accept or Reject."
      : tab === "blocklist"
        ? "No rejected titles."
        : "No accepted titles yet.";

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
                Review Home watchlist requests. Accept sends titles to Arr; Reject moves them to the blocklist.
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
          </div>
          {summary?.last_error ? (
            <p className="mt-2 text-[13px] text-red-400">Last error: {summary.last_error}</p>
          ) : null}
        </div>

        {users.length > 0 ? (
          <div className="px-4 py-3 border-b" style={{ borderColor: headBorder }}>
            <div className="flex items-center justify-between gap-3 mb-2">
              <div className="text-[12px] font-headline uppercase tracking-widest" style={{ color: semantic.fgSubtle }}>
                Users
                {selectedUserIds.length ? (
                  <span className="ml-2 normal-case tracking-normal text-slate-400">
                    Filtering {selectedUserIds.length} selected
                  </span>
                ) : (
                  <span className="ml-2 normal-case tracking-normal text-slate-500">Tap to filter</span>
                )}
              </div>
              {selectedUserIds.length ? (
                <button
                  type="button"
                  onClick={() => setSelectedUserIds([])}
                  className="text-[12px] uppercase tracking-wider"
                  style={{ color: semantic.fgMuted }}
                >
                  Clear filter
                </button>
              ) : null}
            </div>
            <div className="flex gap-3 overflow-x-auto pb-1 overscroll-x-contain">
              {users.map((u) => {
                const on = selectedUserIds.includes(u.plex_user_id);
                const name = userDisplayName(u);
                const blocked = Boolean(u.is_protected && u.last_sync_error);
                return (
                  <button
                    key={u.plex_user_id}
                    type="button"
                    onClick={() => toggleUserFilter(u.plex_user_id)}
                    title={
                      blocked
                        ? u.last_sync_error || "Protected user (PIN required to sync)"
                        : name
                    }
                    className="flex w-[4.5rem] shrink-0 flex-col items-center gap-1.5 rounded-xl border px-1.5 py-2 transition-colors"
                    style={{
                      borderColor: on ? props.accent.hex : semantic.border,
                      backgroundColor: on ? `${props.accent.hex}22` : "transparent",
                    }}
                  >
                    <div
                      className="rounded-full"
                      style={on ? { boxShadow: `0 0 0 2px ${props.accent.hex}` } : undefined}
                    >
                      <UserAvatar thumbUrl={u.thumb_url} title={name} size={44} dimmed={blocked} />
                    </div>
                    <span
                      className="w-full truncate text-center text-[11px] leading-tight"
                      style={{ color: on ? semantic.fg : semantic.fgMuted }}
                    >
                      {name}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        ) : null}

        <div
          className="px-4 pt-3 flex flex-wrap items-end gap-1 border-b"
          style={{ borderColor: headBorder }}
          role="tablist"
          aria-label="Watchlist sections"
        >
          {(
            [
              ["queue", "Queue", tabCounts.queue],
              ["accepted", "Accepted", tabCounts.accepted],
              ["blocklist", "Blocklist", tabCounts.blocklist],
            ] as const
          ).map(([value, label, count]) => {
            const on = tab === value;
            return (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => setTab(value)}
                className="px-3 py-2 text-[13px] font-headline uppercase tracking-wider border-b-2 -mb-px transition-colors"
                style={{
                  borderColor: on ? props.accent.hex : "transparent",
                  color: on ? semantic.fg : semantic.fgMuted,
                }}
              >
                {label}
                <span
                  className="ml-2 inline-flex min-w-[1.25rem] justify-center rounded-full px-1.5 py-0.5 text-[11px] tabular-nums"
                  style={{
                    backgroundColor: on ? `${props.accent.hex}33` : "rgba(66,71,83,0.35)",
                    color: on ? props.accent.hex : semantic.fgSubtle,
                  }}
                >
                  {count}
                </span>
              </button>
            );
          })}
        </div>

        <div
          className="px-4 py-3 border-b flex flex-wrap items-center gap-2"
          style={{ borderColor: headBorder }}
        >
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search titles"
            className="px-3 py-1.5 rounded-lg border bg-transparent text-[14px] min-w-[12rem]"
            style={{ borderColor: semantic.border, color: semantic.fg }}
          />
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {tab === "queue" ? (
              <>
                <AcceptMenuButton
                  open={acceptMenuFor === "bulk"}
                  disabled={!selectedIds.length || busy}
                  label={`Accept (${selectedIds.length})`}
                  onToggle={() => setAcceptMenuFor((v) => (v === "bulk" ? null : "bulk"))}
                  onPick={(intensity) => void onAccept(selectedIds, intensity)}
                  onClose={() => setAcceptMenuFor(null)}
                  panelBg={panelBg}
                  border={panelBorder}
                  fg={semantic.fg}
                  fgMuted={semantic.fgMuted}
                  bulk
                />
                <button
                  type="button"
                  disabled={!selectedIds.length || busy}
                  onClick={() => void onReject(selectedIds)}
                  className="px-3 py-1.5 rounded-lg text-[12px] uppercase tracking-wider font-medium disabled:opacity-40"
                  style={{
                    backgroundColor: "rgba(239,68,68,0.18)",
                    color: "#fca5a5",
                    border: "1px solid rgba(239,68,68,0.35)",
                  }}
                >
                  Reject ({selectedIds.length})
                </button>
              </>
            ) : null}
            {tab === "blocklist" ? (
              <button
                type="button"
                disabled={!selectedIds.length || busy}
                onClick={() => void onRestore(selectedIds)}
                className="px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider disabled:opacity-40"
                style={{ borderColor: semantic.border, color: semantic.fg }}
              >
                Restore to queue ({selectedIds.length})
              </button>
            ) : null}
          </div>
        </div>

        {error ? <div className="px-4 py-2 text-[13px] text-red-400">{error}</div> : null}

        <div className="overflow-x-auto">
          <table className="w-full table-fixed">
            <colgroup>
              <col className="w-10" />
              {tab === "queue" ? (
                <>
                  <col className="w-[28%]" />
                  <col className="w-[7.5rem]" />
                  <col className="w-[8.5rem]" />
                  <col className="w-[24%]" />
                  <col className="w-[11.5rem]" />
                </>
              ) : tab === "blocklist" ? (
                <>
                  <col className="w-[42%]" />
                  <col className="w-[8rem]" />
                  <col className="w-[10rem]" />
                  <col className="w-[9rem]" />
                  <col className="w-[8.5rem]" />
                </>
              ) : (
                <>
                  {/* Accepted: give Title room; keep Status compact */}
                  <col className="w-[46%]" />
                  <col className="w-[8rem]" />
                  <col className="w-[12rem]" />
                  <col className="w-[14rem]" />
                </>
              )}
            </colgroup>
            <thead>
              <tr className="border-b" style={{ borderColor: headBorder }}>
                <th className="px-2 py-2 w-10">
                  <BrandCheckbox
                    checked={filtered.length > 0 && filtered.every((r) => selected[r.id])}
                    onChange={(v) => toggleAll(v)}
                    accentHex={props.accent.hex}
                    ariaLabel="Select all"
                  />
                </th>
                {(tab === "queue"
                  ? ["Title", "By", "Library", "Instances", ""]
                  : tab === "blocklist"
                    ? ["Title", "By", "Library", "Last seen", ""]
                    : ["Title", "By", "Library", "Status"]
                ).map((h, idx) => (
                  <th
                    key={`${h}-${idx}`}
                    className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal"
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
                  <td colSpan={7} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                    Loading…
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                    {emptyCopy}
                  </td>
                </tr>
              ) : (
                filtered.map((row) => {
                  const lib = row.library;
                  const path = libraryPath(lib);
                  const poster = lib?.poster_url || row.thumb || null;
                  const alreadyOn = lib?.already_on_keys || [];
                  const willAdd = lib?.will_add_keys || [];
                  const waitingLibrary =
                    row.status === "added" && (!lib || lib.state === "not_in_library" || lib.coverage !== "complete");
                  return (
                    <tr key={row.id} className="border-b last:border-0" style={{ borderColor: rowBorder }}>
                      <td className="px-2 py-3 align-middle">
                        <BrandCheckbox
                          checked={Boolean(selected[row.id])}
                          onChange={(v) => setSelected((prev) => ({ ...prev, [row.id]: v }))}
                          accentHex={props.accent.hex}
                          ariaLabel={`Select ${row.title}`}
                        />
                      </td>
                      <td className="px-2 py-3 align-middle">
                        <div className="flex items-center gap-2 min-w-0">
                          <div className="h-12 w-8 shrink-0 overflow-hidden rounded bg-[#1e2430] ring-1 ring-[#424753]/40">
                            {poster ? (
                              <img src={poster} alt="" className="h-full w-full object-cover" loading="lazy" />
                            ) : null}
                          </div>
                          <div className="min-w-0 flex-1 overflow-hidden">
                            <div
                              className="text-[14px] font-medium truncate"
                              style={{ color: semantic.fg }}
                              title={`${row.title}${row.year ? ` (${row.year})` : ""}`}
                            >
                              {row.title}
                              {row.year ? (
                                <span style={{ color: semantic.fgSubtle }}> ({row.year})</span>
                              ) : null}
                            </div>
                            <div className="text-[11px] mt-0.5 truncate" style={{ color: semantic.fgSubtle }}>
                              {row.media_type === "show" ? "TV" : "Movie"}
                              {path ? (
                                <>
                                  {" · "}
                                  <a
                                    href={path}
                                    className="underline-offset-2 hover:underline"
                                    style={{ color: props.accent.hex }}
                                  >
                                    Library
                                  </a>
                                </>
                              ) : null}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="px-2 py-3 align-middle">
                        <WatchlistedByCell
                          users={row.users || []}
                          title={row.title}
                          fg={semantic.fg}
                          fgMuted={semantic.fgMuted}
                          border={panelBorder}
                          panelBg={panelBg}
                        />
                      </td>
                      <td className="px-2 py-3 align-middle">
                        <div className="text-[12px] truncate" style={{ color: semantic.fgMuted }}>
                          {presenceLabel(lib?.state)}
                        </div>
                        {lib?.display_status ? (
                          <div className="text-[11px] mt-0.5 truncate" style={{ color: semantic.fgSubtle }}>
                            {lib.display_status}
                          </div>
                        ) : null}
                        {lib?.stats ? (
                          <div className="text-[11px] mt-0.5 tabular-nums truncate" style={{ color: semantic.fgSubtle }}>
                            {lib.stats.files ?? 0}/{lib.stats.placeholders ?? 0}/{lib.stats.missing ?? 0}
                          </div>
                        ) : null}
                      </td>

                      {tab === "queue" ? (
                        <>
                          <td className="px-2 py-3 text-[12px] align-middle" style={{ color: semantic.fgMuted }}>
                            <div className="min-w-0">
                              <span className="uppercase tracking-wider text-[10px]" style={{ color: semantic.fgSubtle }}>
                                Already on
                              </span>
                              <div className="mt-0.5 truncate" title={alreadyOn.length ? alreadyOn.join(", ") : undefined}>
                                {alreadyOn.length ? alreadyOn.join(", ") : "—"}
                              </div>
                            </div>
                            <div className="mt-1.5 min-w-0">
                              <span className="uppercase tracking-wider text-[10px]" style={{ color: semantic.fgSubtle }}>
                                Will add
                              </span>
                              <div
                                className="mt-0.5 truncate"
                                style={{ color: semantic.fg }}
                                title={willAdd.length ? willAdd.join(", ") : undefined}
                              >
                                {willAdd.length ? willAdd.join(", ") : "—"}
                              </div>
                            </div>
                            {row.last_error ? (
                              <div className="text-[11px] text-red-400 truncate mt-1.5" title={row.last_error}>
                                {row.last_error}
                              </div>
                            ) : null}
                          </td>
                          <td className="px-2 py-3 align-middle">
                            <div className="flex items-center justify-end gap-1.5 flex-wrap">
                              <AcceptMenuButton
                                open={acceptMenuFor === row.id}
                                disabled={busy}
                                label="Accept"
                                onToggle={() => setAcceptMenuFor((v) => (v === row.id ? null : row.id))}
                                onPick={(intensity) => void onAccept([row.id], intensity)}
                                onClose={() => setAcceptMenuFor(null)}
                                panelBg={panelBg}
                                border={panelBorder}
                                fg={semantic.fg}
                                fgMuted={semantic.fgMuted}
                              />
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void onReject([row.id])}
                                className="px-2.5 py-1.5 rounded-lg text-[11px] uppercase tracking-wider font-semibold disabled:opacity-40"
                                style={{
                                  backgroundColor: "rgba(239,68,68,0.2)",
                                  color: "#fecaca",
                                  border: "1px solid rgba(239,68,68,0.45)",
                                }}
                              >
                                Reject
                              </button>
                            </div>
                          </td>
                        </>
                      ) : null}

                      {tab === "accepted" ? (
                        <td className="px-2 py-3 text-[13px] align-middle" style={{ color: semantic.fgMuted }}>
                          <div className="truncate">
                            {waitingLibrary ? "Accepted · waiting for library" : statusLabel(row.status)}
                          </div>
                          {alreadyOn.length ? (
                            <div className="text-[11px] mt-1 truncate" style={{ color: semantic.fgSubtle }} title={alreadyOn.join(", ")}>
                              On {alreadyOn.join(", ")}
                            </div>
                          ) : null}
                        </td>
                      ) : null}

                      {tab === "blocklist" ? (
                        <>
                          <td className="px-2 py-3 text-[12px] align-middle truncate" style={{ color: semantic.fgSubtle }}>
                            {formatWhen(row.last_seen_at)}
                          </td>
                          <td className="px-2 py-3 align-middle text-right">
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => void onRestore([row.id])}
                              className="px-2.5 py-1.5 rounded-lg border text-[11px] uppercase tracking-wider disabled:opacity-40"
                              style={{ borderColor: semantic.border, color: semantic.fgMuted }}
                            >
                              Restore
                            </button>
                          </td>
                        </>
                      ) : null}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function AcceptMenuButton(props: {
  open: boolean;
  disabled?: boolean;
  label: string;
  onToggle: () => void;
  onPick: (intensity: AcceptIntensity) => void;
  onClose: () => void;
  panelBg: string;
  border: string;
  fg: string;
  fgMuted: string;
  bulk?: boolean;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!props.open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) props.onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey);
    };
  }, [props.open, props.onClose]);

  return (
    <div ref={rootRef} className="relative inline-block">
      <button
        type="button"
        disabled={props.disabled}
        onClick={props.onToggle}
        aria-expanded={props.open}
        aria-haspopup="menu"
        className="px-3 py-1.5 rounded-lg text-[12px] uppercase tracking-wider font-semibold disabled:opacity-40"
        style={{
          backgroundColor: "rgba(34,197,94,0.22)",
          color: "#bbf7d0",
          border: "1px solid rgba(34,197,94,0.45)",
        }}
      >
        {props.label}
      </button>
      {props.open ? (
        <div
          role="menu"
          className={`absolute z-30 mt-1 w-[18rem] rounded-xl border shadow-xl overflow-hidden ${
            props.bulk ? "right-0" : "right-0"
          }`}
          style={{ borderColor: props.border, backgroundColor: props.panelBg }}
        >
          {ACCEPT_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              role="menuitem"
              onClick={() => props.onPick(opt.value)}
              className="w-full text-left px-3 py-2.5 hover:bg-white/5 border-b last:border-0"
              style={{ borderColor: props.border }}
            >
              <div className="text-[13px] font-medium" style={{ color: props.fg }}>
                {opt.label}
              </div>
              <div className="text-[12px] mt-0.5" style={{ color: props.fgMuted }}>
                {opt.hint}
              </div>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function WatchlistedByCell(props: {
  users: WatchlistItemUser[];
  title: string;
  fg: string;
  fgMuted: string;
  border: string;
  panelBg: string;
}) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!props.users.length) {
    return (
      <span className="text-[13px]" style={{ color: props.fgMuted }}>
        —
      </span>
    );
  }

  const max = 5;
  const shown = props.users.slice(0, max);
  const extra = props.users.length - shown.length;
  const label = `${props.users.length} watchlisted by`;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={`Watchlisted by: ${props.users.map(userDisplayName).join(", ")}`}
        title={label}
        className="flex items-center gap-1.5 min-w-0 rounded-lg -mx-1 px-1 py-0.5 transition-colors hover:bg-white/5 focus:outline-none focus-visible:ring-2 focus-visible:ring-[#424753]"
      >
        <div className="flex -space-x-2">
          {shown.map((u) => (
            <div key={u.plex_user_id} className="relative">
              <UserAvatar thumbUrl={u.thumb_url} title={userDisplayName(u)} size={28} />
            </div>
          ))}
        </div>
        {extra > 0 ? (
          <span
            className="shrink-0 text-[12px] font-headline font-semibold tabular-nums"
            style={{ color: props.fgMuted }}
          >
            +{extra}
          </span>
        ) : null}
      </button>

      {open ? (
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
          <button
            type="button"
            className="absolute inset-0 bg-black/70"
            aria-label="Close"
            onClick={() => setOpen(false)}
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Watchlisted by for ${props.title}`}
            className="relative z-10 w-full max-w-sm rounded-xl border shadow-xl overflow-hidden"
            style={{ borderColor: props.border, backgroundColor: props.panelBg }}
          >
            <div
              className="flex items-center justify-between gap-3 px-4 py-3 border-b"
              style={{ borderColor: props.border }}
            >
              <div>
                <div className="text-[12px] font-headline uppercase tracking-widest" style={{ color: props.fgMuted }}>
                  Watchlisted by
                </div>
                <div className="text-[14px] font-medium truncate mt-0.5" style={{ color: props.fg }}>
                  {props.title}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="text-[12px] uppercase tracking-wider shrink-0"
                style={{ color: props.fgMuted }}
              >
                Close
              </button>
            </div>
            <ul className="max-h-[min(60vh,22rem)] overflow-y-auto py-2">
              {props.users.map((u) => {
                const name = userDisplayName(u);
                return (
                  <li key={u.plex_user_id} className="flex items-center gap-3 px-4 py-2.5">
                    <UserAvatar thumbUrl={u.thumb_url} title={name} size={36} />
                    <span className="min-w-0 truncate text-[14px]" style={{ color: props.fg }}>
                      {name}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      ) : null}
    </>
  );
}
