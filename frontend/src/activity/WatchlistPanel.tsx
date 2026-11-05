import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Brand, ThemeMode } from "../brandTypes";
import { getBrandSemanticTokens } from "../brandSemanticTheme";
import { BrandCheckbox } from "../BrandCheckbox";
import {
  deleteWatchlistUser,
  getWatchlist,
  patchWatchlistUser,
  postWatchlistAdd,
  postWatchlistExclude,
  postWatchlistRosterRefresh,
  postWatchlistSync,
  type WatchlistArrRemap,
  type WatchlistItem,
  type WatchlistItemUser,
  type WatchlistLibrary,
  type WatchlistLibraryInstance,
  type WatchlistSummary,
  type WatchlistUser,
} from "../api/watchlist";
import { BrandSelect } from "../BrandSelect";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";
import { ConfirmModal } from "../ConfirmModal";
import { ToggleSwitch } from "../ToggleSwitch";
import { UI_SECTION_FRAME_CLASS } from "../uiSectionFrame";

const ARR_BEHAVIOR_OPTIONS = [
  { value: "", label: "Default" },
  { value: "add_and_search", label: "Add, monitor, and search" },
  { value: "monitor_only", label: "Add and monitor" },
  { value: "add_only", label: "Add only" },
  { value: "manual", label: "Manual" },
] as const;

type StripAccent = { hex: string; text: string; icon: string; hoverHex: string };
type WatchlistTab = "accepted" | "queue" | "blocklist" | "people";
type AcceptIntensity = "add_and_search" | "monitor_only" | "add_only";
type ItemSortKey = "title" | "synced" | "accepted" | "rejected";

const DEFAULT_SORT_BY_TAB: Record<Exclude<WatchlistTab, "people">, { key: ItemSortKey; dir: "asc" | "desc" }> = {
  queue: { key: "synced", dir: "desc" },
  accepted: { key: "accepted", dir: "desc" },
  blocklist: { key: "rejected", dir: "desc" },
};

// Friend+Home, Friend, then Home (matches fetch order: switches, GraphQL reset, switches).
const FILTER_GROUP_ORDER = ["owner", "friend", "friend_only", "home", "server_only", "unknown"] as const;

function filterGroupKey(u: WatchlistUser): string {
  const r = String(u.relationship || u.kind || "").trim().toLowerCase();
  if (r === "managed") return "home";
  if ((FILTER_GROUP_ORDER as readonly string[]).includes(r)) return r;
  return r || "unknown";
}

function groupUsersForFilter(users: WatchlistUser[]): Array<{ key: string; label: string; users: WatchlistUser[] }> {
  const buckets = new Map<string, WatchlistUser[]>();
  for (const u of users) {
    const key = filterGroupKey(u);
    const list = buckets.get(key);
    if (list) list.push(u);
    else buckets.set(key, [u]);
  }
  const orderedKeys = [
    ...FILTER_GROUP_ORDER.filter((k) => buckets.has(k)),
    ...[...buckets.keys()].filter((k) => !(FILTER_GROUP_ORDER as readonly string[]).includes(k)).sort(),
  ];
  return orderedKeys.map((key) => {
    const groupUsers = buckets.get(key) || [];
    groupUsers.sort((a, b) =>
      userDisplayName(a).localeCompare(userDisplayName(b), undefined, { sensitivity: "base" }),
    );
    const sample = groupUsers[0];
    return {
      key,
      label: relationshipLabel(key, sample?.kind),
      users: groupUsers,
    };
  });
}

function isoTime(iso: string | null | undefined): number {
  if (!iso) return 0;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

function compareItems(a: WatchlistItem, b: WatchlistItem, key: ItemSortKey, dir: "asc" | "desc"): number {
  let cmp = 0;
  if (key === "title") {
    cmp = String(a.title || "").localeCompare(String(b.title || ""), undefined, { sensitivity: "base" });
  } else if (key === "synced") {
    // First time we saw the title (request time), not last re-poll.
    cmp = isoTime(a.first_seen_at) - isoTime(b.first_seen_at);
  } else if (key === "accepted") {
    cmp = isoTime(a.added_at) - isoTime(b.added_at);
  } else if (key === "rejected") {
    cmp = isoTime(a.excluded_at) - isoTime(b.excluded_at);
  }
  if (cmp === 0) {
    cmp = String(a.title || "").localeCompare(String(b.title || ""), undefined, { sensitivity: "base" });
  }
  return dir === "asc" ? cmp : -cmp;
}

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
  if (state === "future") return "Future";
  if (state === "missing") return "Missing";
  return "Not in library";
}

/** Collections-previewer aligned presence colors. */
const PRESENCE_TONE = {
  files: { text: "#34d399", label: "Real" },
  placeholders: { text: "#22d3ee", label: "PH" },
  missing: { text: "#fb7185", label: "Miss" },
  future: { text: "#a78bfa", label: "Future" },
} as const;

type PresenceCounts = { files: number; placeholders: number; missing: number; future: number };

function presenceCountsFromLibrary(lib: WatchlistLibrary | null | undefined): PresenceCounts {
  const stats = lib?.stats;
  if (stats) {
    return {
      files: Number(stats.files ?? 0) || 0,
      placeholders: Number(stats.placeholders ?? 0) || 0,
      missing: Number(stats.missing ?? 0) || 0,
      future: Number(stats.future ?? 0) || 0,
    };
  }
  const state = String(lib?.state || "");
  return {
    files: state === "file" ? 1 : 0,
    placeholders: state === "placeholder" ? 1 : 0,
    missing: state === "missing" ? 1 : 0,
    future: state === "future" ? 1 : 0,
  };
}

function presenceCountsFromInstance(inst: WatchlistLibraryInstance): PresenceCounts {
  const stats = inst.stats;
  if (stats) {
    return {
      files: Number(stats.files ?? 0) || 0,
      placeholders: Number(stats.placeholders ?? 0) || 0,
      missing: Number(stats.missing ?? 0) || 0,
      future: Number(stats.future ?? 0) || 0,
    };
  }
  return {
    files: inst.has_file ? 1 : 0,
    placeholders: inst.has_placeholder ? 1 : 0,
    missing: inst.state === "missing" || inst.has_missing ? 1 : 0,
    future: inst.state === "future" || inst.has_future ? 1 : 0,
  };
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

function remapLabel(remap: WatchlistArrRemap | null | undefined, lib?: WatchlistLibrary | null): string | null {
  if (remap?.remapped && remap.arr_title) {
    const year = remap.arr_year ? ` (${remap.arr_year})` : "";
    return `${remap.arr_title}${year}`;
  }
  if (lib?.arr_title) {
    const year = lib.arr_year ? ` (${lib.arr_year})` : "";
    return `${lib.arr_title}${year}`;
  }
  return null;
}

function formatArrTargetLabel(title: string, year?: number | null): string {
  return year ? `${title} (${year})` : title;
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  try {
    // Compact enough for fixed table columns; full ISO stays on title/tooltip.
    return new Date(t).toLocaleString(undefined, {
      month: "numeric",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function tableMinWidthForTab(tab: Exclude<WatchlistTab, "people">): string {
  // Library column is wider for the Real / PH / Miss / Future breakdown.
  if (tab === "queue") return "70rem";
  if (tab === "blocklist") return "64rem";
  return "68rem";
}

function userDisplayName(u: { title?: string | null; username?: string | null; plex_user_id: string }): string {
  return String(u.title || u.username || u.plex_user_id || "User").trim() || "User";
}

function relationshipLabel(rel: string | null | undefined, kind?: string | null): string {
  const r = String(rel || kind || "").trim().toLowerCase();
  if (r === "owner") return "Owner";
  // friend = friends ∩ (Home and/or this server); friend_only = friends list only.
  if (r === "friend") return "Friend + Home";
  if (r === "friend_only") return "Friend";
  // Share access only; not on the Plex Home quick-switch list.
  if (r === "server_only") return "Server only";
  // On Home list, not a GraphQL friend (adult or managed).
  if (r === "home" || r === "managed") return "Home";
  if (r === "unknown") return "Unknown";
  return r ? r.replace(/_/g, " ") : "—";
}

function homeProfileLabel(profile: string | null | undefined): string | null {
  const p = String(profile || "").trim().toLowerCase();
  if (p === "pin") return "PIN protected";
  if (p === "managed") return "Managed";
  if (p === "unlocked") return "Unlocked";
  return null;
}

const POLICY_OR_HARD_BLOCKS = new Set([
  "needs_friend_or_token",
  "needs_pin_or_token",
  "home_switch_disabled",
  "friends_sync_disabled",
  "friend_only_disabled",
  "not_found",
]);

function userIsNotFound(u: WatchlistUser): boolean {
  return Boolean(u.not_found) || String(u.block_reason || "").trim() === "not_found";
}

function blockReasonCopy(u: WatchlistUser): string | null {
  const code = String(u.block_reason || "").trim();
  if (code === "not_found" || u.not_found) {
    return "Not found on Plex (unfriended or removed from Home)";
  }
  if (code === "needs_friend_or_token") return "Needs friend access or a user token";
  if (code === "needs_pin_or_token") return "Needs Home PIN or a user token";
  if (code === "home_switch_disabled") {
    return "Not syncing: Home switch is off in Settings → Watchlist";
  }
  if (code === "friends_sync_disabled") {
    return "Not syncing: friends sync is off in Settings → Watchlist";
  }
  if (code === "friend_only_disabled") {
    return "Not syncing: friends without server access is off in Settings → Watchlist";
  }
  if (code === "sync_disabled") return "Sync disabled for this person";
  if (u.last_sync_error) return String(u.last_sync_error);
  return null;
}

function userIsBlocked(u: WatchlistUser): boolean {
  const code = String(u.block_reason || "").trim();
  return POLICY_OR_HARD_BLOCKS.has(code);
}

function syncToggleLocked(u: WatchlistUser): boolean {
  return POLICY_OR_HARD_BLOCKS.has(String(u.block_reason || "").trim());
}

/** What the switch should show: only “on” when a fetch will actually run. */
function syncToggleChecked(u: WatchlistUser): boolean {
  if (typeof u.sync_effective === "boolean") return u.sync_effective;
  return Boolean(u.sync_enabled) && !syncToggleLocked(u);
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
  /** True while a watchlist_sync task run is WORKING (from scheduled tasks poll). */
  syncRunning?: boolean;
  /** Kick the shared tasks poll so the sidebar pill and this button update promptly. */
  onRefreshTasks?: () => void;
  /** Navigate to Settings → Media Integrations (Plex). */
  onOpenPlexSettings?: () => void;
  /** Navigate to Settings → Watchlist. */
  onOpenWatchlistSettings?: () => void;
  /**
   * Optional App hint once settings are loaded:
   * - true: Plex enable + connection details look complete
   * - false: ENABLE_PLEX is off (optimistic empty state after disconnect)
   * - undefined: let summary.plex_ready decide (needed after refresh when secrets are blank)
   */
  plexReady?: boolean;
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
  const [sortKey, setSortKey] = useState<ItemSortKey>(DEFAULT_SORT_BY_TAB.queue.key);
  const [sortDir, setSortDir] = useState<"asc" | "desc">(DEFAULT_SORT_BY_TAB.queue.dir);
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  /** Local gap between click and the first poll that reports syncRunning. */
  const [syncStarting, setSyncStarting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rosterBusy, setRosterBusy] = useState(false);
  const [removeConfirmUser, setRemoveConfirmUser] = useState<WatchlistUser | null>(null);
  const [acceptMenuFor, setAcceptMenuFor] = useState<"bulk" | number | null>(null);
  const wasSyncRunningRef = useRef(false);
  const syncBusy = Boolean(props.syncRunning) || syncStarting;

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

  useEffect(() => {
    if (tab === "people") return;
    const defaults = DEFAULT_SORT_BY_TAB[tab];
    setSortKey(defaults.key);
    setSortDir(defaults.dir);
  }, [tab]);

  useEffect(() => {
    if (props.syncRunning) {
      setSyncStarting(false);
      wasSyncRunningRef.current = true;
      return;
    }
    if (wasSyncRunningRef.current) {
      wasSyncRunningRef.current = false;
      setSyncStarting(false);
      void load();
    }
  }, [props.syncRunning, load]);

  // Bridging poll: POST returns before begin_task_run; keep asking until running or timeout.
  useEffect(() => {
    if (!syncStarting || props.syncRunning) return;
    props.onRefreshTasks?.();
    const pollId = window.setInterval(() => props.onRefreshTasks?.(), 1500);
    const failsafeId = window.setTimeout(() => {
      setSyncStarting(false);
      void load();
    }, 15000);
    return () => {
      window.clearInterval(pollId);
      window.clearTimeout(failsafeId);
    };
  }, [syncStarting, props.syncRunning, props.onRefreshTasks, load]);

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

  const filtered = useMemo(() => {
    const rows = searched.filter((row) => tabForItem(row) === tab);
    if (tab === "people") return rows;
    return [...rows].sort((a, b) => compareItems(a, b, sortKey, sortDir));
  }, [searched, tab, sortKey, sortDir]);

  const syncEnabled = summary ? Boolean(summary.enabled) : true;
  const hasWatchlistHistory =
    items.length > 0 || Boolean(summary?.last_poll_at) || (summary?.counts?.total ?? 0) > 0;

  const onSortHeader = (key: ItemSortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDir(key === "title" ? "asc" : "desc");
  };

  const filterGroups = useMemo(() => groupUsersForFilter(users), [users]);

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
    if (syncBusy) return;
    setSyncStarting(true);
    setError(null);
    try {
      await postWatchlistSync();
      props.onRefreshTasks?.();
    } catch (e) {
      setSyncStarting(false);
      const msg = e instanceof Error ? e.message : "Refresh failed";
      if (/already running/i.test(msg)) {
        setSyncStarting(true);
        props.onRefreshTasks?.();
        setError(null);
        return;
      }
      setError(msg);
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
    const remappedRows = items.filter((row) => ids.includes(row.id) && row.arr_remap?.remapped && row.arr_remap.arr_title);
    if (remappedRows.length) {
      const lines = remappedRows.slice(0, 5).map((row) => {
        const arr = formatArrTargetLabel(String(row.arr_remap?.arr_title), row.arr_remap?.arr_year);
        return `${row.title} adds as ${arr}`;
      });
      const extra =
        remappedRows.length > 5 ? `\n…and ${remappedRows.length - 5} more.` : "";
      const ok = window.confirm(
        `Arr maps ${remappedRows.length} watchlisted title${remappedRows.length === 1 ? "" : "s"} to a different series/movie.\n\n${lines.join("\n")}${extra}\n\nAccept the Arr title${remappedRows.length === 1 ? "" : "s"}?`,
      );
      if (!ok) {
        setBusy(false);
        return;
      }
    }
    try {
      const result = await postWatchlistAdd({
        item_ids: ids,
        search,
        monitored,
        tv_scope: summary?.tv_request_scope || undefined,
        confirm_remap: remappedRows.length > 0,
      });
      const pending = result.needs_remap_confirm || [];
      if (pending.length && !(result.added || 0)) {
        const lines = pending.slice(0, 5).map((row) => {
          const arr = row.arr_title
            ? formatArrTargetLabel(String(row.arr_title), row.arr_year)
            : "Arr title";
          return `${row.watchlist_title || "Title"} adds as ${arr}`;
        });
        const extra = pending.length > 5 ? `\n…and ${pending.length - 5} more.` : "";
        const ok = window.confirm(
          `Arr maps ${pending.length} watchlisted title${pending.length === 1 ? "" : "s"} to a different series/movie.\n\n${lines.join("\n")}${extra}\n\nAccept the Arr title${pending.length === 1 ? "" : "s"}?`,
        );
        if (ok) {
          const confirmed = await postWatchlistAdd({
            item_ids: pending.map((p) => p.id),
            search,
            monitored,
            tv_scope: summary?.tv_request_scope || undefined,
            confirm_remap: true,
          });
          if (confirmed.failed && confirmed.failed > 0 && !confirmed.added) {
            setError(
              `Accept failed for ${confirmed.failed} title${confirmed.failed === 1 ? "" : "s"}. Check Watchlist Arr targets (profile and root are required).`,
            );
          }
        }
      } else if (result.failed && result.failed > 0 && !result.added) {
        setError(
          `Accept failed for ${result.failed} title${result.failed === 1 ? "" : "s"}. Check Watchlist Arr targets (profile and root are required).`,
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

  const onRosterRefresh = async () => {
    setRosterBusy(true);
    setError(null);
    try {
      const result = await postWatchlistRosterRefresh();
      setUsers(result.users || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Roster refresh failed");
    } finally {
      setRosterBusy(false);
    }
  };

  const mergePatchedUser = (prev: WatchlistUser, next: WatchlistUser): WatchlistUser => ({
    ...prev,
    ...next,
    title: next.title || prev.title,
    thumb_url: next.thumb_url ?? prev.thumb_url,
  });

  const onToggleUserSync = async (u: WatchlistUser, enabled: boolean) => {
    if (syncToggleLocked(u)) return;
    setUsers((prev) =>
      prev.map((row) => (row.plex_user_id === u.plex_user_id ? { ...row, sync_enabled: enabled } : row)),
    );
    try {
      const result = await patchWatchlistUser(u.plex_user_id, { sync_enabled: enabled });
      setUsers((prev) =>
        prev.map((row) => (row.plex_user_id === u.plex_user_id ? mergePatchedUser(row, result.user) : row)),
      );
    } catch (e) {
      setUsers((prev) =>
        prev.map((row) =>
          row.plex_user_id === u.plex_user_id ? { ...row, sync_enabled: u.sync_enabled } : row,
        ),
      );
      setError(e instanceof Error ? e.message : "Could not update sync toggle");
    }
  };

  const onChangeUserArrBehavior = async (u: WatchlistUser, value: string) => {
    const nextOnNew = value.trim() ? value.trim() : null;
    setUsers((prev) =>
      prev.map((row) => (row.plex_user_id === u.plex_user_id ? { ...row, on_new: nextOnNew } : row)),
    );
    try {
      const result = await patchWatchlistUser(u.plex_user_id, { on_new: nextOnNew });
      setUsers((prev) =>
        prev.map((row) => (row.plex_user_id === u.plex_user_id ? mergePatchedUser(row, result.user) : row)),
      );
    } catch (e) {
      setUsers((prev) =>
        prev.map((row) => (row.plex_user_id === u.plex_user_id ? { ...row, on_new: u.on_new } : row)),
      );
      setError(e instanceof Error ? e.message : "Could not update Arr behavior");
    }
  };

  const soleTitleRemoveCopy = (u: WatchlistUser): string => {
    const sole = typeof u.sole_title_count === "number" ? u.sole_title_count : null;
    if (sole == null) {
      return "Titles where they are the only person with it on their watchlist will be removed from Placeholdarr.";
    }
    if (sole === 0) return "No titles are held only by them on a watchlist.";
    if (sole === 1) {
      return "1 title where they are the only person with it on their watchlist will be removed from Placeholdarr.";
    }
    return `${sole} titles where they are the only person with it on their watchlist will be removed from Placeholdarr.`;
  };

  const onRemoveUser = (u: WatchlistUser) => {
    if (!userIsNotFound(u) || rosterBusy) return;
    setRemoveConfirmUser(u);
  };

  const confirmRemoveUser = async () => {
    const u = removeConfirmUser;
    if (!u || rosterBusy) return;
    setRosterBusy(true);
    setError(null);
    try {
      await deleteWatchlistUser(u.plex_user_id);
      setRemoveConfirmUser(null);
      setUsers((prev) => prev.filter((row) => row.plex_user_id !== u.plex_user_id));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not remove person");
      setRemoveConfirmUser(null);
    } finally {
      setRosterBusy(false);
    }
  };

  const emptyCopy =
    tab === "queue"
      ? "No requests waiting. New watchlist titles appear here for Accept or Reject."
      : tab === "blocklist"
        ? "No rejected titles."
        : "No accepted titles yet.";

  // App false = known disconnected. App true = connected. Otherwise use API plex_ready
  // after load (refresh-safe: secrets are redacted in field values).
  const plexReady =
    props.plexReady === false
      ? false
      : props.plexReady === true
        ? true
        : loading || !summary
          ? null
          : Boolean(summary.plex_ready);
  if (plexReady === false) {
    return (
      <div>
        <div className="flex items-center gap-2 mb-1">
          <div className="w-2 h-2 rounded-full bg-green-500" />
          <span className="text-[12px] font-headline uppercase tracking-widest text-slate-500">
            Plex Watchlist
          </span>
        </div>
        <h1
          className={`mb-6 text-[32px] font-black tracking-tight font-headline ${
            isLight ? "text-slate-900" : "text-white"
          }`}
        >
          Watchlist
        </h1>
        <div className={`${UI_SECTION_FRAME_CLASS} overflow-hidden`}>
          <div className="p-12 text-center">
            <span className="material-symbols-outlined text-slate-500" style={{ fontSize: 44 }}>
              playlist_add_check
            </span>
            <p className={`mt-3 text-[18px] font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
              Watchlist is Plex only
            </p>
            <p className="mt-2 text-[14px] max-w-lg mx-auto leading-relaxed text-slate-500">
              This feature syncs Plex watchlists into a review queue and Arr targets. Connect Plex under Settings →
              Media Integrations to use it.
            </p>
            {props.onOpenPlexSettings ? (
              <button
                type="button"
                onClick={props.onOpenPlexSettings}
                className={`mt-5 inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
                style={accentFilledStyle(props.accent.hex)}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                  settings
                </span>
                Connect Plex
              </button>
            ) : null}
            {error ? <p className="mt-4 text-[12px] max-w-md mx-auto text-slate-500">{error}</p> : null}
          </div>
        </div>
      </div>
    );
  }

  // Plex is connected but Watchlist sync is off and nothing has been synced yet.
  if (plexReady === true && !loading && summary && !syncEnabled && !hasWatchlistHistory) {
    return (
      <div>
        <div className="flex items-center gap-2 mb-1">
          <div className="w-2 h-2 rounded-full bg-green-500" />
          <span className="text-[12px] font-headline uppercase tracking-widest text-slate-500">
            Plex Watchlist
          </span>
        </div>
        <h1
          className={`mb-6 text-[32px] font-black tracking-tight font-headline ${
            isLight ? "text-slate-900" : "text-white"
          }`}
        >
          Watchlist
        </h1>
        <div className={`${UI_SECTION_FRAME_CLASS} overflow-hidden`}>
          <div className="p-12 text-center">
            <span className="material-symbols-outlined text-slate-500" style={{ fontSize: 44 }}>
              sync_disabled
            </span>
            <p className={`mt-3 text-[18px] font-headline ${isLight ? "text-slate-900" : "text-white"}`}>
              Watchlist sync is off
            </p>
            <p className="mt-2 text-[14px] max-w-lg mx-auto leading-relaxed text-slate-500">
              Turn on Watchlist sync under Settings → Watchlist to pull roster lists into the Queue. Accept and Reject
              stay available here after the first sync.
            </p>
            {props.onOpenWatchlistSettings ? (
              <button
                type="button"
                onClick={props.onOpenWatchlistSettings}
                className={`mt-5 inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS}`}
                style={accentFilledStyle(props.accent.hex)}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>
                  settings
                </span>
                Open Watchlist settings
              </button>
            ) : null}
            {error ? <p className="mt-4 text-[12px] max-w-md mx-auto text-slate-500">{error}</p> : null}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border"
        style={{ borderColor: panelBorder, backgroundColor: panelBg }}
      >
        <div className="shrink-0 px-4 py-3 border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.3)" }}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-[20px] font-bold font-headline" style={{ color: isLight ? semantic.fg : "#fff" }}>
                Plex Watchlist
              </h2>
              <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
                Review roster watchlist requests. Accept sends titles to Arr; Reject moves them to the blocklist.
                Global policies stay in Settings → Watchlist.
              </p>
            </div>
            <button
              type="button"
              disabled={syncBusy || !syncEnabled}
              title={!syncEnabled ? "Enable Watchlist sync in Settings to refresh" : undefined}
              onClick={() => void onRefresh()}
              className="px-3 py-1.5 rounded-lg text-[12px] uppercase tracking-wider disabled:opacity-50"
              style={{ backgroundColor: props.accent.hex, color: semantic.fgOnAccent }}
            >
              {syncBusy ? "Refreshing…" : "Refresh now"}
            </button>
          </div>
          <div className="mt-3 flex flex-wrap gap-3 text-[13px]" style={{ color: semantic.fgMuted }}>
            <span>
              Sync:{" "}
              <strong style={{ color: semantic.fg }}>{summary?.enabled ? "Enabled" : "Disabled"}</strong>
            </span>
            <span>
              Default Arr behavior:{" "}
              <strong style={{ color: semantic.fg }}>{summary?.on_new || "manual"}</strong>
            </span>
            <span>Last poll: {formatWhen(summary?.last_poll_at ?? null)}</span>
          </div>
          {!syncEnabled && hasWatchlistHistory ? (
            <div
              className="mt-3 flex flex-wrap items-start gap-3 rounded-lg border px-3 py-2.5"
              style={{
                borderColor: `${props.accent.hex}55`,
                backgroundColor: `${props.accent.hex}14`,
              }}
            >
              <span
                className="material-symbols-outlined mt-0.5 shrink-0"
                style={{ fontSize: 18, color: props.accent.hex }}
              >
                info
              </span>
              <div className="min-w-0 flex-1 text-[13px]" style={{ color: semantic.fgMuted }}>
                <p style={{ color: semantic.fg }}>
                  Watchlist sync is off. Scheduled sync will not run until you re-enable it in Settings → Watchlist.
                </p>
                <p className="mt-1">
                  Titles already here stay available for Accept and Reject. Refresh now stays off while sync is
                  disabled.
                </p>
              </div>
              {props.onOpenWatchlistSettings ? (
                <button
                  type="button"
                  onClick={props.onOpenWatchlistSettings}
                  className="shrink-0 px-2.5 py-1 rounded-lg border text-[11px] uppercase tracking-wider"
                  style={{ borderColor: semantic.border, color: semantic.fg }}
                >
                  Settings
                </button>
              ) : null}
            </div>
          ) : null}
          {summary?.last_error ? (
            <p className="mt-2 text-[13px] text-red-400">Last error: {summary.last_error}</p>
          ) : null}
        </div>

        <div
          className="shrink-0 px-4 pt-3 flex flex-wrap items-end gap-1 border-b"
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
          <button
            type="button"
            role="tab"
            aria-selected={tab === "people"}
            onClick={() => setTab("people")}
            className="ml-auto px-3 py-2 text-[13px] font-headline uppercase tracking-wider border-b-2 -mb-px transition-colors"
            style={{
              borderColor: tab === "people" ? props.accent.hex : "transparent",
              color: tab === "people" ? semantic.fg : semantic.fgMuted,
            }}
          >
            People
            <span
              className="ml-2 inline-flex min-w-[1.25rem] justify-center rounded-full px-1.5 py-0.5 text-[11px] tabular-nums"
              style={{
                backgroundColor: tab === "people" ? `${props.accent.hex}33` : "rgba(66,71,83,0.35)",
                color: tab === "people" ? props.accent.hex : semantic.fgSubtle,
              }}
            >
              {users.length}
            </span>
          </button>
        </div>

        {tab !== "people" && users.length > 0 ? (
          <div className="shrink-0 px-4 pt-3 border-b" style={{ borderColor: headBorder }}>
            <div className="flex items-center justify-between gap-3 mb-2">
              <div className="text-[12px] font-headline uppercase tracking-widest" style={{ color: semantic.fgSubtle }}>
                Filter
                {selectedUserIds.length ? (
                  <span className="ml-2 normal-case tracking-normal text-slate-400">
                    Filtering {selectedUserIds.length} selected
                  </span>
                ) : (
                  <span className="ml-2 normal-case tracking-normal text-slate-500">Tap to filter titles</span>
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
            <div className="flex gap-4 overflow-x-auto pb-3 overscroll-x-contain">
              {filterGroups.map((group, groupIdx) => (
                <div
                  key={group.key}
                  className="flex shrink-0 items-end gap-3"
                  style={
                    groupIdx > 0
                      ? { borderLeft: `1px solid ${headBorder}`, paddingLeft: "0.75rem" }
                      : undefined
                  }
                >
                  <div className="flex flex-col gap-1.5">
                    <div
                      className="px-1 text-[10px] font-headline uppercase tracking-widest"
                      style={{ color: semantic.fgSubtle }}
                    >
                      {group.label}
                    </div>
                    <div className="flex gap-3">
                      {group.users.map((u) => {
                        const on = selectedUserIds.includes(u.plex_user_id);
                        const name = userDisplayName(u);
                        const blocked = userIsBlocked(u);
                        const dimmed = blocked || !syncToggleChecked(u);
                        return (
                          <button
                            key={u.plex_user_id}
                            type="button"
                            onClick={() => toggleUserFilter(u.plex_user_id)}
                            title={`${relationshipLabel(u.relationship, u.kind)}: ${blockReasonCopy(u) || name}`}
                            className="flex w-[4.5rem] shrink-0 flex-col items-center gap-1.5 rounded-xl border px-1.5 py-2 transition-colors"
                            style={{
                              borderColor: on ? props.accent.hex : semantic.border,
                              backgroundColor: on ? `${props.accent.hex}22` : "transparent",
                              opacity: dimmed && !on ? 0.55 : 1,
                            }}
                          >
                            <div
                              className="rounded-full"
                              style={on ? { boxShadow: `0 0 0 2px ${props.accent.hex}` } : undefined}
                            >
                              <UserAvatar thumbUrl={u.thumb_url} title={name} size={44} dimmed={dimmed} />
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
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {tab === "people" ? (
          <>
            <div
              className="shrink-0 px-4 py-3 border-b flex flex-wrap items-center gap-2"
              style={{ borderColor: headBorder }}
            >
              <div className="text-[13px]" style={{ color: semantic.fgMuted }}>
                Per-person sync and Arr behavior. Greyed sync toggles mean Settings → Watchlist (or a missing
                path) blocks fetch. Arr behavior Default uses Settings → Watchlist; any other choice overrides
                only that person. People no longer on Plex friends/Home are marked not found so you can remove
                them.
              </div>
              <button
                type="button"
                disabled={rosterBusy || syncBusy}
                onClick={() => void onRosterRefresh()}
                className="ml-auto px-3 py-1.5 rounded-lg border text-[12px] uppercase tracking-wider disabled:opacity-50"
                style={{ borderColor: semantic.border, color: semantic.fg }}
              >
                {rosterBusy ? "Refreshing…" : "Refresh roster"}
              </button>
            </div>
            {error ? <div className="shrink-0 px-4 py-2 text-[13px] text-red-400">{error}</div> : null}
            <div className="min-h-0 flex-1 overflow-auto">
              <table className="w-full table-fixed">
                <colgroup>
                  <col className="w-14" />
                  <col />
                  <col className="w-[8.5rem]" />
                  <col className="w-[12.5rem]" />
                  <col className="w-[11rem]" />
                  <col className="w-[4.75rem]" />
                </colgroup>
                <thead>
                  <tr className="border-b" style={{ borderColor: headBorder }}>
                    {["", "Person", "Relationship", "Arr behavior", "Last sync", "Sync"].map((h, idx) => (
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
                      <td colSpan={6} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                        Loading…
                      </td>
                    </tr>
                  ) : users.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                        No roster yet. Run Refresh now or Refresh roster after Plex is connected.
                      </td>
                    </tr>
                  ) : (
                    users.map((u) => {
                      const name = userDisplayName(u);
                      const blocked = userIsBlocked(u);
                      const reason = blockReasonCopy(u);
                      const locked = syncToggleLocked(u);
                      const checked = syncToggleChecked(u);
                      const missing = userIsNotFound(u);
                      return (
                        <tr key={`roster-${u.plex_user_id}`} className="border-b last:border-0" style={{ borderColor: rowBorder }}>
                          <td className="px-2 py-3 align-middle">
                            <UserAvatar
                              thumbUrl={u.thumb_url}
                              title={name}
                              size={36}
                              dimmed={blocked || !checked || missing}
                            />
                          </td>
                          <td className="px-2 py-3 align-middle min-w-0">
                            <div className="truncate text-[14px] font-medium" style={{ color: semantic.fg }}>
                              {name}
                            </div>
                            {reason ? (
                              <p
                                className="mt-0.5 text-[12px] leading-snug"
                                style={{ color: blocked || missing ? "#f87171" : semantic.fgSubtle }}
                              >
                                {reason}
                              </p>
                            ) : u.username && u.username !== name ? (
                              <p className="mt-0.5 text-[12px] truncate" style={{ color: semantic.fgSubtle }}>
                                {u.username}
                              </p>
                            ) : null}
                          </td>
                          <td className="px-2 py-3 align-middle">
                            <div className="flex flex-col items-start gap-1">
                              <span
                                className="inline-block rounded-md px-1.5 py-0.5 text-[10px] uppercase tracking-wider"
                                style={{
                                  backgroundColor: `${props.accent.hex}18`,
                                  color: semantic.fgMuted,
                                }}
                              >
                                {relationshipLabel(u.relationship, u.kind)}
                              </span>
                              {u.relationship === "home" && homeProfileLabel(u.home_profile) ? (
                                <span className="text-[11px]" style={{ color: semantic.fgSubtle }}>
                                  {homeProfileLabel(u.home_profile)}
                                </span>
                              ) : u.relationship === "friend" && u.on_plex_home === false ? (
                                <span className="text-[11px]" style={{ color: semantic.fgSubtle }}>
                                  Server share
                                </span>
                              ) : null}
                            </div>
                          </td>
                          <td className="px-2 py-3 align-middle">
                            <BrandSelect
                              fullWidth
                              disabled={rosterBusy || missing}
                              value={u.on_new || ""}
                              options={[...ARR_BEHAVIOR_OPTIONS]}
                              onChange={(value) => void onChangeUserArrBehavior(u, value)}
                              ariaLabel={`Arr behavior for ${name}`}
                            />
                          </td>
                          <td className="px-2 py-3 align-middle text-[13px]" style={{ color: semantic.fgMuted }}>
                            {formatWhen(u.last_synced_at ?? null)}
                          </td>
                          <td className="px-2 py-3 align-middle">
                            {missing ? (
                              <button
                                type="button"
                                disabled={rosterBusy}
                                onClick={() => onRemoveUser(u)}
                                className="px-2 py-1 rounded-md text-[11px] uppercase tracking-wider disabled:opacity-50"
                                style={{
                                  backgroundColor: "rgba(239,68,68,0.18)",
                                  color: "#fca5a5",
                                  border: "1px solid rgba(239,68,68,0.35)",
                                }}
                              >
                                Remove
                              </button>
                            ) : (
                              <ToggleSwitch
                                size="sm"
                                checked={checked}
                                disabled={locked || rosterBusy}
                                accentHex={props.accent.hex}
                                ariaLabel={`Sync watchlist for ${name}`}
                                onChange={(next) => void onToggleUserSync(u, next)}
                              />
                            )}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <>
        <div
          className="shrink-0 px-4 py-3 border-b flex flex-wrap items-center gap-2"
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

        {error ? <div className="shrink-0 px-4 py-2 text-[13px] text-red-400">{error}</div> : null}

        <div className="min-h-0 flex-1 overflow-auto">
          <table
            className="w-full table-fixed"
            style={{ minWidth: tableMinWidthForTab(tab) }}
          >
            <colgroup>
              <col style={{ width: "2.5rem" }} />
              {/* Title absorbs leftover width; other cols stay rem-sized so they do not crush. */}
              <col />
              {tab === "queue" ? (
                <>
                  <col style={{ width: "7.5rem" }} />
                  <col style={{ width: "13.5rem" }} />
                  <col style={{ width: "10rem" }} />
                  <col style={{ width: "11.5rem" }} />
                  <col style={{ width: "11.5rem" }} />
                </>
              ) : tab === "blocklist" ? (
                <>
                  <col style={{ width: "7.5rem" }} />
                  <col style={{ width: "13.5rem" }} />
                  <col style={{ width: "11.5rem" }} />
                  <col style={{ width: "11.5rem" }} />
                  <col style={{ width: "7.5rem" }} />
                </>
              ) : (
                <>
                  <col style={{ width: "7.5rem" }} />
                  <col style={{ width: "13.5rem" }} />
                  <col style={{ width: "10rem" }} />
                  <col style={{ width: "11.5rem" }} />
                  <col style={{ width: "11.5rem" }} />
                </>
              )}
            </colgroup>
            <thead className="sticky top-0 z-[1]" style={{ backgroundColor: panelBg }}>
              <tr className="border-b" style={{ borderColor: headBorder }}>
                <th className="px-2 py-2 w-10 overflow-hidden">
                  <BrandCheckbox
                    checked={filtered.length > 0 && filtered.every((r) => selected[r.id])}
                    onChange={(v) => toggleAll(v)}
                    accentHex={props.accent.hex}
                    ariaLabel="Select all"
                  />
                </th>
                <SortableTh
                  label="Title"
                  sortKey="title"
                  activeKey={sortKey}
                  dir={sortDir}
                  onSort={onSortHeader}
                  color={semantic.fgSubtle}
                  activeColor={props.accent.hex}
                />
                <th
                  className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal overflow-hidden"
                  style={{ color: semantic.fgSubtle }}
                >
                  By
                </th>
                <th
                  className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal overflow-hidden"
                  style={{ color: semantic.fgSubtle }}
                >
                  Library
                </th>
                {tab === "queue" ? (
                  <th
                    className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal overflow-hidden"
                    style={{ color: semantic.fgSubtle }}
                  >
                    Instances
                  </th>
                ) : null}
                {tab === "accepted" ? (
                  <th
                    className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal overflow-hidden"
                    style={{ color: semantic.fgSubtle }}
                  >
                    Status
                  </th>
                ) : null}
                <SortableTh
                  label="Synced"
                  sortKey="synced"
                  activeKey={sortKey}
                  dir={sortDir}
                  onSort={onSortHeader}
                  color={semantic.fgSubtle}
                  activeColor={props.accent.hex}
                />
                {tab === "accepted" ? (
                  <SortableTh
                    label="Accepted"
                    sortKey="accepted"
                    activeKey={sortKey}
                    dir={sortDir}
                    onSort={onSortHeader}
                    color={semantic.fgSubtle}
                    activeColor={props.accent.hex}
                  />
                ) : null}
                {tab === "blocklist" ? (
                  <SortableTh
                    label="Rejected"
                    sortKey="rejected"
                    activeKey={sortKey}
                    dir={sortDir}
                    onSort={onSortHeader}
                    color={semantic.fgSubtle}
                    activeColor={props.accent.hex}
                  />
                ) : null}
                {tab === "queue" || tab === "blocklist" ? (
                  <th
                    className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal overflow-hidden"
                    style={{ color: semantic.fgSubtle }}
                  />
                ) : null}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
                    Loading…
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-[14px]" style={{ color: semantic.fgMuted }}>
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
                  const arrAs = remapLabel(row.arr_remap, lib);
                  const remapped = Boolean(row.arr_remap?.remapped && arrAs);
                  return (
                    <tr key={row.id} className="border-b last:border-0" style={{ borderColor: rowBorder }}>
                      <td className="px-2 py-3 align-middle overflow-hidden">
                        <BrandCheckbox
                          checked={Boolean(selected[row.id])}
                          onChange={(v) => setSelected((prev) => ({ ...prev, [row.id]: v }))}
                          accentHex={props.accent.hex}
                          ariaLabel={`Select ${row.title}`}
                        />
                      </td>
                      <td className="px-2 py-3 align-middle overflow-hidden">
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
                      <td className="px-1 py-2 align-middle overflow-hidden">
                        <WatchlistedByCell
                          users={row.users || []}
                          title={row.title}
                          fg={semantic.fg}
                          fgMuted={semantic.fgMuted}
                          border={panelBorder}
                          panelBg={panelBg}
                        />
                      </td>
                      <td className="px-1 py-2 align-middle overflow-hidden">
                        <LibraryPresenceCell
                          library={lib}
                          title={row.title}
                          fg={semantic.fg}
                          fgMuted={semantic.fgMuted}
                          fgSubtle={semantic.fgSubtle}
                          border={panelBorder}
                          panelBg={panelBg}
                        />
                      </td>

                      {tab === "queue" ? (
                        <td className="px-2 py-3 text-[12px] align-middle overflow-hidden" style={{ color: semantic.fgMuted }}>
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
                          {remapped && arrAs ? (
                            <div
                              className="mt-1.5 text-[11px] truncate"
                              style={{ color: "#fbbf24" }}
                              title={`Arr maps this watchlist title to ${arrAs}`}
                            >
                              As {arrAs}
                            </div>
                          ) : null}
                          {row.last_error ? (
                            <div className="text-[11px] text-red-400 truncate mt-1.5" title={row.last_error}>
                              {row.last_error}
                            </div>
                          ) : null}
                        </td>
                      ) : null}

                      {tab === "accepted" ? (
                        <td className="px-2 py-3 text-[13px] align-middle overflow-hidden" style={{ color: semantic.fgMuted }}>
                          <div className="min-w-0">
                            <div className="truncate" style={{ color: semantic.fg }}>
                              {statusLabel(row.status)}
                              {remapped && arrAs ? ` as ${arrAs}` : ""}
                            </div>
                            {waitingLibrary ? (
                              <div className="text-[11px] mt-1 truncate" style={{ color: semantic.fgSubtle }}>
                                Waiting for library
                              </div>
                            ) : alreadyOn.length ? (
                              <div
                                className="text-[11px] mt-1 truncate"
                                style={{ color: semantic.fgSubtle }}
                                title={alreadyOn.join(", ")}
                              >
                                On {alreadyOn.join(", ")}
                              </div>
                            ) : null}
                          </div>
                        </td>
                      ) : null}

                      <td
                        className="px-2 py-3 text-[12px] align-middle overflow-hidden whitespace-nowrap truncate"
                        style={{ color: semantic.fgSubtle }}
                        title={row.first_seen_at ? formatWhen(row.first_seen_at) : undefined}
                      >
                        {formatWhen(row.first_seen_at)}
                      </td>

                      {tab === "accepted" ? (
                        <td
                          className="px-2 py-3 text-[12px] align-middle overflow-hidden whitespace-nowrap truncate"
                          style={{ color: semantic.fgSubtle }}
                          title={row.added_at ? formatWhen(row.added_at) : undefined}
                        >
                          {formatWhen(row.added_at)}
                        </td>
                      ) : null}

                      {tab === "blocklist" ? (
                        <td
                          className="px-2 py-3 text-[12px] align-middle overflow-hidden whitespace-nowrap truncate"
                          style={{ color: semantic.fgSubtle }}
                          title={row.excluded_at ? formatWhen(row.excluded_at) : undefined}
                        >
                          {formatWhen(row.excluded_at ?? null)}
                        </td>
                      ) : null}

                      {tab === "queue" ? (
                        <td className="px-2 py-3 align-middle overflow-hidden">
                          <div className="flex items-center justify-end gap-1.5 flex-nowrap">
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
                              className="px-2.5 py-1.5 rounded-lg text-[11px] uppercase tracking-wider font-semibold disabled:opacity-40 shrink-0"
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
                      ) : null}

                      {tab === "blocklist" ? (
                        <td className="px-2 py-3 align-middle text-right overflow-hidden">
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
                      ) : null}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
          </>
        )}
      </div>

      {removeConfirmUser ? (
        <ConfirmModal
          title={`Remove ${userDisplayName(removeConfirmUser)}?`}
          message={soleTitleRemoveCopy(removeConfirmUser)}
          confirmLabel={rosterBusy ? "Removing…" : "Remove"}
          cancelLabel="Cancel"
          accentHex={props.accent.hex}
          themeMode={props.themeMode}
          onCancel={() => {
            if (rosterBusy) return;
            setRemoveConfirmUser(null);
          }}
          onConfirm={() => {
            if (rosterBusy) return;
            void confirmRemoveUser();
          }}
        >
          <p className={`text-[14px] leading-relaxed ${isLight ? "text-slate-500" : "text-slate-400"}`}>
            Arr catalog and placeholders are unchanged.
          </p>
        </ConfirmModal>
      ) : null}
    </div>
  );
}

function SortableTh(props: {
  label: string;
  sortKey: ItemSortKey;
  activeKey: ItemSortKey;
  dir: "asc" | "desc";
  onSort: (key: ItemSortKey) => void;
  color: string;
  activeColor: string;
}) {
  const active = props.activeKey === props.sortKey;
  return (
    <th
      className="px-2 py-2 text-left text-[12px] font-headline uppercase tracking-widest font-normal overflow-hidden"
      aria-sort={active ? (props.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        onClick={() => props.onSort(props.sortKey)}
        className="inline-flex items-center gap-1 max-w-full min-w-0 hover:opacity-90"
        style={{ color: active ? props.activeColor : props.color }}
      >
        <span className="truncate">{props.label}</span>
        <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14 }} aria-hidden>
          {active ? (props.dir === "asc" ? "arrow_upward" : "arrow_downward") : "unfold_more"}
        </span>
      </button>
    </th>
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
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);

  const placeMenu = useCallback(() => {
    const btn = rootRef.current;
    const menu = menuRef.current;
    if (!btn || !menu) return;
    const rect = btn.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const gap = 4;
    const pad = 8;
    const spaceBelow = window.innerHeight - rect.bottom - pad;
    const placeAbove = menuRect.height > spaceBelow && rect.top > spaceBelow;
    let top = placeAbove ? rect.top - menuRect.height - gap : rect.bottom + gap;
    let left = rect.right - menuRect.width;
    top = Math.max(pad, Math.min(top, window.innerHeight - menuRect.height - pad));
    left = Math.max(pad, Math.min(left, window.innerWidth - menuRect.width - pad));
    setMenuPos({ top, left });
  }, []);

  useLayoutEffect(() => {
    if (!props.open) {
      setMenuPos(null);
      return;
    }
    placeMenu();
  }, [props.open, placeMenu]);

  useEffect(() => {
    if (!props.open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      props.onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    const onReposition = () => placeMenu();
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onReposition);
    window.addEventListener("scroll", onReposition, true);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    };
  }, [props.open, props.onClose, placeMenu]);

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
      {props.open
        ? createPortal(
            <div
              ref={menuRef}
              role="menu"
              className="fixed z-[90] w-[18rem] rounded-xl border shadow-xl overflow-hidden"
              style={{
                borderColor: props.border,
                backgroundColor: props.panelBg,
                top: menuPos?.top ?? -9999,
                left: menuPos?.left ?? -9999,
                visibility: menuPos ? "visible" : "hidden",
              }}
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
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

function PresenceStatStack(props: {
  counts: PresenceCounts;
  size?: "sm" | "md";
  showZero?: boolean;
}) {
  const size = props.size ?? "sm";
  const showZero = props.showZero ?? true;
  const items = (
    [
      ["files", props.counts.files],
      ["placeholders", props.counts.placeholders],
      ["missing", props.counts.missing],
      ["future", props.counts.future],
    ] as const
  ).filter(([, n]) => showZero || n > 0);

  if (!items.length) {
    return (
      <span className="text-[12px]" style={{ color: "#64748b" }}>
        —
      </span>
    );
  }

  return (
    <div className={`flex items-end ${size === "md" ? "gap-4" : "gap-2.5"}`}>
      {items.map(([key, n]) => {
        const tone = PRESENCE_TONE[key];
        const muted = n <= 0;
        return (
          <div key={key} className="min-w-0 text-center">
            <div
              className={`font-headline font-bold tabular-nums leading-none ${
                size === "md" ? "text-[22px]" : "text-[16px]"
              }`}
              style={{ color: muted ? "#64748b" : tone.text }}
            >
              {n}
            </div>
            <div
              className={`mt-0.5 font-headline uppercase tracking-wider ${
                size === "md" ? "text-[10px]" : "text-[9px]"
              }`}
              style={{ color: muted ? "#64748b" : tone.text }}
            >
              {tone.label}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function LibraryPresenceCell(props: {
  library: WatchlistLibrary | null | undefined;
  title: string;
  fg: string;
  fgMuted: string;
  fgSubtle: string;
  border: string;
  panelBg: string;
}) {
  const [open, setOpen] = useState(false);
  const lib = props.library;
  const counts = presenceCountsFromLibrary(lib);
  const instances = lib?.instances || [];
  const missingTargets = lib?.missing_target_keys || [];
  const clickable = Boolean(lib) && (instances.length > 0 || missingTargets.length > 0 || lib?.state !== "not_in_library");

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const empty = !lib || lib.state === "not_in_library";

  const body = empty ? (
    <div className="text-[12px]" style={{ color: props.fgMuted }}>
      Not in library
    </div>
  ) : (
    <PresenceStatStack counts={counts} size="sm" showZero />
  );

  return (
    <>
      <button
        type="button"
        disabled={!clickable}
        onClick={() => {
          if (clickable) setOpen(true);
        }}
        aria-label={`Library presence for ${props.title}`}
        title={clickable ? "View per-library breakdown" : undefined}
        className={`w-full min-h-[3.25rem] rounded-lg px-1.5 py-1 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[#424753] ${
          clickable ? "hover:bg-white/5 cursor-pointer" : "cursor-default opacity-80"
        }`}
      >
        {body}
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
            aria-label={`Library presence for ${props.title}`}
            className="relative z-10 w-full max-w-md rounded-xl border shadow-xl overflow-hidden"
            style={{ borderColor: props.border, backgroundColor: props.panelBg }}
          >
            <div
              className="flex items-center justify-between gap-3 px-4 py-3 border-b"
              style={{ borderColor: props.border }}
            >
              <div className="min-w-0">
                <div className="text-[12px] font-headline uppercase tracking-widest" style={{ color: props.fgMuted }}>
                  Library presence
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

            <div className="px-4 py-3 border-b" style={{ borderColor: props.border }}>
              <div className="text-[11px] font-headline uppercase tracking-widest mb-2" style={{ color: props.fgSubtle }}>
                Combined
              </div>
              <PresenceStatStack counts={counts} size="md" showZero />
              <p className="mt-2 text-[12px]" style={{ color: props.fgMuted }}>
                {presenceLabel(lib?.state)}
                {lib?.coverage ? ` · Coverage ${lib.coverage}` : ""}
              </p>
            </div>

            <ul className="max-h-[min(60vh,22rem)] overflow-y-auto py-1">
              {instances.map((inst) => {
                const instCounts = presenceCountsFromInstance(inst);
                return (
                  <li
                    key={`${inst.instance_key}-${inst.item_id}`}
                    className="px-4 py-3 border-b last:border-0"
                    style={{ borderColor: props.border }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-[14px] font-medium truncate" style={{ color: props.fg }}>
                          {inst.instance_key}
                        </div>
                        <div className="text-[11px] mt-0.5" style={{ color: props.fgSubtle }}>
                          {inst.is_watchlist_target ? "Watchlist target" : "Other instance"}
                          {" · "}
                          {presenceLabel(inst.state)}
                        </div>
                      </div>
                      <PresenceStatStack counts={instCounts} size="sm" showZero />
                    </div>
                  </li>
                );
              })}
              {missingTargets.map((key) => (
                <li
                  key={`missing-${key}`}
                  className="px-4 py-3 border-b last:border-0"
                  style={{ borderColor: props.border }}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-[14px] font-medium truncate" style={{ color: props.fg }}>
                        {key}
                      </div>
                      <div className="text-[11px] mt-0.5" style={{ color: props.fgSubtle }}>
                        Watchlist target · Not in library
                      </div>
                    </div>
                    <PresenceStatStack
                      counts={{ files: 0, placeholders: 0, missing: 0, future: 0 }}
                      size="sm"
                      showZero
                    />
                  </div>
                </li>
              ))}
              {!instances.length && !missingTargets.length ? (
                <li className="px-4 py-6 text-[13px]" style={{ color: props.fgMuted }}>
                  No Arr instances matched for this title.
                </li>
              ) : null}
            </ul>
          </div>
        </div>
      ) : null}
    </>
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
      <span className="text-[13px] px-1.5" style={{ color: props.fgMuted }}>
        —
      </span>
    );
  }

  const max = 5;
  const shown = props.users.slice(0, max);
  const extra = props.users.length - shown.length;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={`Watchlisted by: ${props.users.map(userDisplayName).join(", ")}`}
        title={props.users.map(userDisplayName).join(", ")}
        className="flex w-full min-h-[3.25rem] items-center gap-1.5 min-w-0 rounded-lg px-1.5 py-1 text-left transition-colors hover:bg-white/5 focus:outline-none focus-visible:ring-2 focus-visible:ring-[#424753]"
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
