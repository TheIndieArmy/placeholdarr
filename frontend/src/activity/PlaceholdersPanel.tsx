import { Fragment, useMemo, useState } from "react";
import type { Brand, ThemeMode } from "../brandTypes";
import { getBrandSemanticTokens } from "../brandSemanticTheme";
import type { PlaceholderActivityRow } from "../types/api";
import { dayGroupLabel } from "./formatActivityTime";
import { timeAgo } from "./formatActivityTime";
import { useLoadOlderOnScroll } from "./useLoadOlderOnScroll";

type ActionFilter = "all" | "Created" | "Deleted" | "Status";

type StripAccent = { hex: string; text: string; icon: string; hoverHex: string };

export function PlaceholdersPanel(props: {
  rows: PlaceholderActivityRow[];
  brand: Brand;
  themeMode: ThemeMode;
  accent: StripAccent;
  hasMore?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
}) {
  const semantic = getBrandSemanticTokens(props.brand, props.themeMode, props.accent);
  const isLight = props.themeMode === "light";
  const [query, setQuery] = useState("");
  const [action, setAction] = useState<ActionFilter>("all");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const sentinelRef = useLoadOlderOnScroll({
    enabled: Boolean(props.onLoadOlder),
    hasMore: Boolean(props.hasMore),
    loading: Boolean(props.loadingOlder),
    onLoadOlder: props.onLoadOlder ?? (() => {}),
  });

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (props.rows || []).filter((row) => {
      if (action !== "all" && row.action !== action) return false;
      if (!q) return true;
      const hay = `${row.item_title || ""} ${row.series_title || ""} ${row.reason || ""} ${row.path || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [action, props.rows, query]);

  const groups = useMemo(() => {
    const order: string[] = [];
    const map = new Map<string, PlaceholderActivityRow[]>();
    for (const row of filtered) {
      const label = dayGroupLabel(row.time);
      if (!map.has(label)) {
        map.set(label, []);
        order.push(label);
      }
      map.get(label)!.push(row);
    }
    return order.map((label) => ({ label, rows: map.get(label)! }));
  }, [filtered]);

  const created = filtered.filter((r) => r.action === "Created").length;
  const deleted = filtered.filter((r) => r.action === "Deleted").length;

  const filters: ActionFilter[] = ["all", "Created", "Deleted", "Status"];

  return (
    <div>

      <div
        className="rounded-xl overflow-hidden border mb-6"
        style={{
          borderColor: isLight ? semantic.border : "rgba(66,71,83,0.4)",
          backgroundColor: isLight ? semantic.surfacePanel : "#171c22",
        }}
      >
        <div className="px-4 py-3 border-b" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.3)" }}>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[20px] font-bold font-headline" style={{ color: isLight ? semantic.fg : "#fff" }}>
              Placeholder history
            </h2>
          </div>
          <p className="text-[13px] mt-0.5" style={{ color: semantic.fgMuted }}>
            {filtered.length} of {props.rows.length} loaded rows · {created} created · {deleted} deleted
            {props.hasMore ? " · scroll for older" : ""}
          </p>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search title, series, reason…"
              className="flex-1 min-w-0 rounded-lg border px-3 py-1.5 text-[14px] bg-transparent"
              style={{ borderColor: semantic.border, color: semantic.fg }}
            />
            <div className="flex flex-wrap gap-1.5">
              {filters.map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setAction(f)}
                  className="px-2.5 py-1 rounded-full border text-[11px] font-headline uppercase tracking-wider"
                  style={{
                    borderColor: action === f ? semantic.accent : semantic.border,
                    backgroundColor: action === f ? (isLight ? "rgba(15,23,42,0.06)" : "rgba(125,211,252,0.12)") : "transparent",
                    color: action === f ? semantic.fg : semantic.fgMuted,
                  }}
                >
                  {f === "all" ? "All" : f}
                </button>
              ))}
            </div>
          </div>
        </div>

        {!filtered.length ? (
          <div className="p-10 text-center text-[16px]" style={{ color: semantic.fgSubtle }}>
            No placeholder rows match.
          </div>
        ) : (
          <div>
            {groups.map((group) => (
              <div key={group.label}>
                <div
                  className="px-4 py-1.5 text-[11px] font-headline uppercase tracking-widest"
                  style={{ color: semantic.fgSubtle, backgroundColor: isLight ? semantic.surfaceMuted : "#12161c" }}
                >
                  {group.label}
                </div>
                <ul className="divide-y" style={{ borderColor: isLight ? semantic.border : "rgba(66,71,83,0.15)" }}>
                  {group.rows.map((row, idx) => {
                    const children = Array.isArray(row.children) ? row.children : [];
                    const isBatch = children.length > 0;
                    const batchKey = `ph-${row.id}-${idx}`;
                    const open = !!expanded[batchKey];
                    const seriesOnlyTitle =
                      row.group_kind === "series_create_batch" ||
                      row.group_kind === "series_added_create" ||
                      row.group_kind === "series_bulk_delete";
                    const contentDisplay = seriesOnlyTitle
                      ? row.series_title || row.item_title
                      : row.series_title &&
                          row.item_title &&
                          row.series_title.trim().toLowerCase() !== row.item_title.trim().toLowerCase()
                        ? `${row.series_title} • ${row.item_title}`
                        : row.item_title || row.series_title;
                    return (
                      <Fragment key={batchKey}>
                        <li
                          className={`px-4 py-3 ${isBatch ? "cursor-pointer" : ""}`}
                          onClick={
                            isBatch
                              ? () => setExpanded((prev) => ({ ...prev, [batchKey]: !prev[batchKey] }))
                              : undefined
                          }
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="flex items-center gap-2 min-w-0">
                                {isBatch ? (
                                  <span className="material-symbols-outlined text-slate-500" style={{ fontSize: 18 }}>
                                    {open ? "expand_more" : "chevron_right"}
                                  </span>
                                ) : null}
                                <span className="font-medium truncate" style={{ color: semantic.fg }} title={contentDisplay}>
                                  {contentDisplay}
                                </span>
                              </div>
                              <p className="text-[13px] mt-0.5 truncate" style={{ color: semantic.fgMuted }} title={row.reason}>
                                {row.reason}
                              </p>
                            </div>
                            <div className="shrink-0 text-right">
                              <span
                                className="inline-flex px-2 py-0.5 rounded text-[11px] font-headline uppercase tracking-wider"
                                style={{
                                  color: row.action === "Created" ? semantic.success : row.action === "Deleted" ? "#ea580c" : semantic.accentIce,
                                  backgroundColor: isLight ? semantic.surfaceElevated : "rgba(148,163,184,0.12)",
                                }}
                              >
                                {row.action}
                              </span>
                              <div className="text-[12px] mt-1" style={{ color: semantic.fgSubtle }}>
                                {timeAgo(row.time)}
                              </div>
                            </div>
                          </div>
                        </li>
                        {isBatch && open
                          ? children.map((child, cidx) => (
                              <li
                                key={`${batchKey}-c-${cidx}`}
                                className="px-4 py-2 pl-12 text-[13px]"
                                style={{ backgroundColor: isLight ? semantic.surfaceMuted : "#12161c", color: semantic.fgMuted }}
                              >
                                {child.series_title ? `${child.series_title} • ` : ""}
                                {child.item_title}
                                {child.status ? ` · ${child.status}` : ""}
                              </li>
                            ))
                          : null}
                      </Fragment>
                    );
                  })}
                </ul>
              </div>
            ))}
            <div ref={sentinelRef} className="px-4 py-3 text-center text-[12px]" style={{ color: semantic.fgSubtle }}>
              {props.loadingOlder ? "Loading older…" : props.hasMore ? "Scroll for older history" : null}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
