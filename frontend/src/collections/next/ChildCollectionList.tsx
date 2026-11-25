import type { ThemeMode } from "../../brandTypes";
import type { CollectionPreviewResponse } from "../../types/api";
import { getCollectionTheme } from "../collectionTheme";

const SOURCE_LABELS: Record<string, string> = {
  tmdb: "TMDB",
  library: "Library",
  library_fallback: "Library only (not on TMDB)",
  arr_tag: "Arr tag",
};

export function ChildCollectionList(props: {
  themeMode: ThemeMode;
  accentHex: string;
  rows: NonNullable<CollectionPreviewResponse["set_collections"]>;
  selectedTitle: string | null;
  onSelect: (title: string) => void;
  loading?: boolean;
  heading?: string;
}) {
  const theme = getCollectionTheme(props.themeMode === "light");
  const isLight = props.themeMode === "light";
  const belowMinCount = props.rows.filter((r) => r.skipped === "below_min").length;

  return (
    <div className={`${theme.blockCard} overflow-hidden`}>
      <div className={`border-b px-4 py-3 ${theme.divider}`}>
        <div className="flex items-center gap-2">
          <div className={`flex-1 ${theme.blockTitle}`}>{props.heading ?? "Collections in this set"}</div>
          {props.rows.length ? (
            <span className={`text-[12px] font-mono ${theme.muted}`}>
              {props.rows.length - belowMinCount} built
              {belowMinCount ? ` · ${belowMinCount} below min` : ""}
            </span>
          ) : null}
        </div>
        <div className={`text-[12px] ${theme.muted}`}>
          In Plex counts titles present in the library. Selected is the recipe cut. Below min uses In Plex only.
        </div>
      </div>
      {props.loading ? (
        <p className={`p-4 text-[13px] ${theme.muted}`}>Loading…</p>
      ) : !props.rows.length ? (
        <p className={`p-4 text-[13px] ${theme.muted}`}>No collections match yet.</p>
      ) : (
        <ul className={`max-h-64 divide-y overflow-y-auto ${isLight ? "divide-slate-200" : "divide-[#424753]/25"}`}>
          {props.rows.map((row) => {
            const active = props.selectedTitle === row.title;
            const belowMin = row.skipped === "below_min";
            return (
              <li key={row.title}>
                <button
                  type="button"
                  onClick={() => props.onSelect(row.title)}
                  className={`flex w-full flex-col gap-1 px-4 py-2.5 text-left transition ${
                    active
                      ? isLight
                        ? "bg-sky-50"
                        : "bg-[#1e2430]"
                      : isLight
                        ? "hover:bg-slate-50"
                        : "hover:bg-[#1a2028]"
                  } ${belowMin ? "opacity-60" : ""}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className={`truncate text-[14px] font-medium ${isLight ? "text-slate-900" : "text-slate-100"}`}>
                      {row.title}
                    </span>
                    <span className={`shrink-0 text-[11px] ${theme.muted}`}>{SOURCE_LABELS[row.source ?? "library"]}</span>
                  </div>
                  <div className={`flex flex-wrap gap-x-3 text-[11px] font-mono ${theme.muted}`}>
                    <span>In Plex {row.in_target_library ?? row.selected}</span>
                    <span>Selected {row.after_filters ?? "—"}</span>
                    <span>Placeholders {row.held_count ?? 0}</span>
                    <span>Would add {row.would_add ?? 0}</span>
                    {belowMin ? (
                      <span
                        className={isLight ? "text-amber-800" : "text-amber-300"}
                        title={`Needs at least ${row.min_items ?? "?"} titles in Plex to build this collection`}
                      >
                        Below min (needs {row.min_items ?? "?"} in Plex)
                      </span>
                    ) : null}
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}