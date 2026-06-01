import { useCallback, useEffect, useRef, useState } from "react";
import { getLibrary, getLibraryVersion } from "../api/dashboard";
import type { LibraryItem } from "../types/api";

/** Version-only poll while the library list is open (no full-catalog 5s poll). */
export const LIBRARY_VERSION_POLL_MS = 60_000;

export type LibraryShelfKey = "movies" | "tv";

export type LibraryShelfCache = {
  items: LibraryItem[];
  total: number;
  version: number | null;
  digest: string;
  loadedAt: number;
};

type LibraryVersions = { movies_version: number; series_version: number };

const SHELF_MEDIA: Record<LibraryShelfKey, "movie" | "series"> = {
  movies: "movie",
  tv: "series",
};

export function digestLibraryItems(items: LibraryItem[]): string {
  return items
    .map(
      (i) =>
        `${i.id}\t${i.title}\t${i.year}\t${i.type}\t${i.has_file}\t${i.has_placeholder}\t${i.is_future}\t${i.has_missing}\t${i.status ?? ""}\t${i.poster_url ?? ""}\t${i.overview ?? ""}`,
    )
    .join("\n");
}

export function useLibraryShelves(opts: {
  /** Active list shelf (`movies` / `tv`), or null on detail routes. */
  listShelf: LibraryShelfKey | null;
  titleSearch: string;
  /** Library list route is mounted and should load/cache shelves. */
  enabled: boolean;
  onSuccess?: () => void;
  onError?: (message: string) => void;
}) {
  const [libraryCache, setLibraryCache] = useState<Partial<Record<LibraryShelfKey, LibraryShelfCache>>>({});
  const [libraryLoading, setLibraryLoading] = useState(false);

  const libraryCacheRef = useRef(libraryCache);
  const libraryDigestRef = useRef<Partial<Record<LibraryShelfKey, string>>>({});
  const versionsRef = useRef<LibraryVersions | null>(null);
  const onSuccessRef = useRef(opts.onSuccess);
  const onErrorRef = useRef(opts.onError);

  useEffect(() => {
    libraryCacheRef.current = libraryCache;
  }, [libraryCache]);

  useEffect(() => {
    onSuccessRef.current = opts.onSuccess;
    onErrorRef.current = opts.onError;
  }, [opts.onSuccess, opts.onError]);

  const useSummary = opts.titleSearch.trim().length === 0;

  const applyShelfCache = useCallback((shelfKey: LibraryShelfKey, entry: LibraryShelfCache) => {
    libraryDigestRef.current[shelfKey] = entry.digest;
    setLibraryCache((prev) => ({ ...prev, [shelfKey]: entry }));
  }, []);

  const refreshShelf = useCallback(
    async (
      shelfKey: LibraryShelfKey,
      versions: LibraryVersions,
      options: {
        summary: boolean;
        force?: boolean;
        stopped: boolean;
      },
    ): Promise<void> => {
      const { summary, force = false, stopped } = options;
      const targetVersion = shelfKey === "movies" ? versions.movies_version : versions.series_version;
      const cached = libraryCacheRef.current[shelfKey];

      if (!force && cached?.version === targetVersion && cached.items.length > 0) {
        return;
      }

      const result = await getLibrary({
        summary,
        mediaType: SHELF_MEDIA[shelfKey],
        ifNoneMatch: force ? undefined : (cached?.version ?? undefined),
      });

      if (stopped) return;

      if (result.notModified) {
        if (cached) {
          applyShelfCache(shelfKey, {
            ...cached,
            version: targetVersion,
            loadedAt: Date.now(),
          });
        }
        return;
      }

      const next = result.payload.items || [];
      const digest = digestLibraryItems(next);
      if (digest === libraryDigestRef.current[shelfKey] && cached?.version === targetVersion) {
        return;
      }

      applyShelfCache(shelfKey, {
        items: next,
        total: result.payload.total ?? next.length,
        version: typeof result.payload.version === "number" ? result.payload.version : targetVersion,
        digest,
        loadedAt: Date.now(),
      });
    },
    [applyShelfCache],
  );

  const syncShelvesFromVersions = useCallback(
    async (options: {
      shelves: LibraryShelfKey[];
      summary: boolean;
      force?: boolean;
      stopped: boolean;
    }) => {
      const versions = await getLibraryVersion();
      if (options.stopped) return;
      versionsRef.current = versions;

      for (const shelfKey of options.shelves) {
        await refreshShelf(shelfKey, versions, {
          summary: options.summary,
          force: options.force,
          stopped: options.stopped,
        });
      }

      if (!options.stopped) {
        onSuccessRef.current?.();
      }
    },
    [refreshShelf],
  );

  const invalidateLibraryShelves = useCallback(() => {
    setLibraryCache((prev) => {
      const next: Partial<Record<LibraryShelfKey, LibraryShelfCache>> = { ...prev };
      for (const key of ["movies", "tv"] as LibraryShelfKey[]) {
        const shelf = next[key];
        if (shelf) {
          next[key] = { ...shelf, version: null };
        }
      }
      return next;
    });
  }, []);

  const refreshLibraryShelves = useCallback(
    async (options?: { shelves?: LibraryShelfKey[]; force?: boolean }) => {
      const shelves = options?.shelves ?? (["movies", "tv"] as LibraryShelfKey[]);
      try {
        await syncShelvesFromVersions({
          shelves,
          summary: useSummary,
          force: options?.force,
          stopped: false,
        });
      } catch (err) {
        onErrorRef.current?.(err instanceof Error ? err.message : "Library refresh failed");
      }
    },
    [syncShelvesFromVersions, useSummary],
  );

  /** Load active shelf on navigation; prefetch the other shelf once for title search. */
  useEffect(() => {
    if (!opts.enabled || !opts.listShelf) return;

    const activeShelf = opts.listShelf;
    const otherShelf: LibraryShelfKey = activeShelf === "movies" ? "tv" : "movies";
    let stopped = false;

    void (async () => {
      const cached = libraryCacheRef.current[activeShelf];
      if (!cached?.items.length) {
        setLibraryLoading(true);
      }

      try {
        const versions = await getLibraryVersion();
        if (stopped) return;
        versionsRef.current = versions;

        await refreshShelf(activeShelf, versions, { summary: useSummary, stopped });
        if (stopped) return;

        const otherCached = libraryCacheRef.current[otherShelf];
        if (!otherCached?.items.length) {
          void refreshShelf(otherShelf, versions, { summary: useSummary, stopped }).then(() => {
            if (!stopped) onSuccessRef.current?.();
          });
        }

        onSuccessRef.current?.();
      } catch (err) {
        if (!stopped) {
          onErrorRef.current?.(err instanceof Error ? err.message : "Library load failed");
        }
      } finally {
        if (!stopped) setLibraryLoading(false);
      }
    })();

    return () => {
      stopped = true;
    };
  }, [opts.enabled, opts.listShelf, useSummary, refreshShelf]);

  /** Tab focus + periodic version check — refetch catalog body only when version counters change. */
  useEffect(() => {
    if (!opts.enabled) return;

    let stopped = false;

    const runVersionCheck = () => {
      if (stopped || document.visibilityState !== "visible") return;
      const shelves = (["movies", "tv"] as LibraryShelfKey[]).filter(
        (key) => (libraryCacheRef.current[key]?.items.length ?? 0) > 0,
      );
      if (!shelves.length && opts.listShelf) {
        shelves.push(opts.listShelf);
      }
      if (!shelves.length) return;

      void syncShelvesFromVersions({
        shelves,
        summary: useSummary,
        stopped,
      }).catch((err) => {
        if (!stopped) {
          onErrorRef.current?.(err instanceof Error ? err.message : "Library version check failed");
        }
      });
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        runVersionCheck();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    const pollId = window.setInterval(runVersionCheck, LIBRARY_VERSION_POLL_MS);

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearInterval(pollId);
    };
  }, [opts.enabled, opts.listShelf, useSummary, syncShelvesFromVersions]);

  return {
    libraryCache,
    libraryLoading,
    invalidateLibraryShelves,
    refreshLibraryShelves,
  };
}
