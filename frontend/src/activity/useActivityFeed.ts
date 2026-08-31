import { useCallback, useEffect, useRef, useState } from "react";
import {
  getActivityOperationsPage,
  getPlaceholderActivityPage,
  type ActivityFeedPage,
} from "../api/dashboard";
import { TAB_HIDDEN_POLL_MS, TAB_IDLE_POLL_MS } from "../dashboard/pollIntervals";
import type { ActivitySubPage } from "../types/api";
import type { ActivityRow, PlaceholderActivityRow } from "../types/api";

const PAGE_LIMIT = 50;

function rowKey(row: { id?: string | number; type?: string; time?: string | null; action?: string }): string {
  return `${row.type ?? ""}:${row.id ?? ""}:${row.time ?? ""}:${row.action ?? ""}`;
}

function digestActivityRows(rows: { id?: string | number; time?: string | null }[]): string {
  if (!rows.length) return "0";
  const first = rows[0];
  return `${rows.length}:${first.id ?? ""}:${first.time ?? ""}`;
}

function mergeFreshWithTail<T extends { id?: string | number; type?: string; time?: string | null; action?: string }>(
  fresh: T[],
  previous: T[],
): T[] {
  if (!previous.length) return fresh;
  if (!fresh.length) return previous;
  const freshKeys = new Set(fresh.map(rowKey));
  const oldestFresh = fresh[fresh.length - 1]?.time || "";
  const tail = previous.filter((row) => {
    if (freshKeys.has(rowKey(row))) return false;
    const t = row.time || "";
    return Boolean(oldestFresh) && t < oldestFresh;
  });
  return [...fresh, ...tail];
}

export function useActivityFeed(opts: {
  subPage: ActivitySubPage;
  enabled: boolean;
  onSuccess?: () => void;
  onError?: (message: string) => void;
}) {
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [placeholderActivity, setPlaceholderActivity] = useState<PlaceholderActivityRow[]>([]);
  const [feedLoading, setFeedLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [nextBeforeTime, setNextBeforeTime] = useState<string | null>(null);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);

  const digestRef = useRef("");
  const cursorRef = useRef<{ time: string | null; id: number | null }>({ time: null, id: null });
  const hasMoreRef = useRef(false);
  const loadingOlderRef = useRef(false);
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);

  useEffect(() => {
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
  }, [opts.onError, opts.onSuccess]);

  useEffect(() => {
    cursorRef.current = { time: nextBeforeTime, id: nextBeforeId };
  }, [nextBeforeId, nextBeforeTime]);

  useEffect(() => {
    hasMoreRef.current = hasMore;
  }, [hasMore]);

  const applyPageMeta = useCallback((page: ActivityFeedPage<unknown>, replaceCursor: boolean) => {
    setHasMore(Boolean(page.has_more));
    if (replaceCursor || page.has_more) {
      setNextBeforeTime(page.next_before_time ?? null);
      setNextBeforeId(page.next_before_id ?? null);
    }
  }, []);

  const refreshFeed = useCallback(
    async (options?: { showLoading?: boolean }) => {
      const showLoading = options?.showLoading ?? false;
      if (showLoading) {
        setFeedLoading(true);
      }

      try {
        if (opts.subPage === "operations") {
          const page = await getActivityOperationsPage({ limit: PAGE_LIMIT });
          const digest = digestActivityRows(page.items || []);
          if (digest !== digestRef.current) {
            digestRef.current = digest;
            setActivity((prev) => mergeFreshWithTail(page.items || [], prev));
          }
          applyPageMeta(page, true);
        } else if (opts.subPage === "placeholders") {
          const page = await getPlaceholderActivityPage({ limit: PAGE_LIMIT });
          const digest = digestActivityRows(page.items || []);
          if (digest !== digestRef.current) {
            digestRef.current = digest;
            setPlaceholderActivity((prev) => mergeFreshWithTail(page.items || [], prev));
          }
          applyPageMeta(page, true);
        }
        onSuccessRef.current?.();
      } catch (err) {
        onErrorRef.current?.(err instanceof Error ? err.message : "Activity refresh failed");
        throw err;
      } finally {
        if (showLoading) {
          setFeedLoading(false);
        }
      }
    },
    [applyPageMeta, opts.subPage],
  );

  const loadOlder = useCallback(async () => {
    if (loadingOlderRef.current || !hasMoreRef.current) return;
    const { time, id } = cursorRef.current;
    if (!time && id == null) return;

    loadingOlderRef.current = true;
    setLoadingOlder(true);
    try {
      if (opts.subPage === "operations") {
        const page = await getActivityOperationsPage({
          limit: PAGE_LIMIT,
          beforeTime: time,
          beforeId: id,
        });
        const incoming = page.items || [];
        setActivity((prev) => {
          const seen = new Set(prev.map(rowKey));
          const appended = incoming.filter((r) => !seen.has(rowKey(r)));
          return [...prev, ...appended];
        });
        applyPageMeta(page, true);
      } else if (opts.subPage === "placeholders") {
        const page = await getPlaceholderActivityPage({
          limit: PAGE_LIMIT,
          beforeTime: time,
          beforeId: id,
        });
        const incoming = page.items || [];
        setPlaceholderActivity((prev) => {
          const seen = new Set(prev.map(rowKey));
          const appended = incoming.filter((r) => !seen.has(rowKey(r)));
          return [...prev, ...appended];
        });
        applyPageMeta(page, true);
      }
      onSuccessRef.current?.();
    } catch (err) {
      onErrorRef.current?.(err instanceof Error ? err.message : "Activity load older failed");
    } finally {
      loadingOlderRef.current = false;
      setLoadingOlder(false);
    }
  }, [applyPageMeta, opts.subPage]);

  useEffect(() => {
    if (!opts.enabled || opts.subPage === "tasks") return;

    digestRef.current = "";
    setHasMore(false);
    setNextBeforeTime(null);
    setNextBeforeId(null);
    if (opts.subPage === "operations") setActivity([]);
    if (opts.subPage === "placeholders") setPlaceholderActivity([]);

    let stopped = false;
    let timeoutId = 0;

    const schedule = (delayMs: number) => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        void poll();
      }, delayMs);
    };

    const poll = async () => {
      if (stopped) return;
      const hidden = typeof document !== "undefined" && document.visibilityState !== "visible";
      if (hidden) {
        schedule(TAB_HIDDEN_POLL_MS);
        return;
      }

      try {
        await refreshFeed();
      } catch {
        /* onError already surfaced */
      }

      if (!stopped) {
        schedule(TAB_IDLE_POLL_MS);
      }
    };

    void refreshFeed({ showLoading: true });

    const onVisibility = () => {
      window.clearTimeout(timeoutId);
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void poll();
      } else {
        schedule(TAB_HIDDEN_POLL_MS);
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    schedule(TAB_IDLE_POLL_MS);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timeoutId);
    };
  }, [opts.enabled, opts.subPage, refreshFeed]);

  return {
    activity,
    placeholderActivity,
    feedLoading,
    loadingOlder,
    hasMore,
    loadOlder,
    refreshFeed,
  };
}
