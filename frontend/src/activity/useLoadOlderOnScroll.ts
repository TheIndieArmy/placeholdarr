import { useEffect, useRef } from "react";

/** Fires ``onLoadOlder`` when the sentinel nears the bottom of ``scrollRoot`` (or the viewport). */
export function useLoadOlderOnScroll(opts: {
  enabled: boolean;
  hasMore: boolean;
  loading: boolean;
  onLoadOlder: () => void;
}) {
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!opts.enabled || !opts.hasMore) return;
    const el = sentinelRef.current;
    if (!el) return;

    const root =
      (typeof document !== "undefined" &&
        (document.querySelector("main.overflow-y-auto") as HTMLElement | null)) ||
      null;

    const observer = new IntersectionObserver(
      (entries) => {
        const hit = entries.some((e) => e.isIntersecting);
        if (hit && opts.hasMore && !opts.loading) {
          opts.onLoadOlder();
        }
      },
      { root, rootMargin: "240px 0px", threshold: 0 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [opts.enabled, opts.hasMore, opts.loading, opts.onLoadOlder]);

  return sentinelRef;
}
