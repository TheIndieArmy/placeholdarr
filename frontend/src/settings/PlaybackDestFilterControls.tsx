import { useMemo } from "react";
import { UI_SECTION_FRAME_CLASS } from "../uiSectionFrame";

export type PlaybackDestOptions = {
  movies: string[];
  tv: string[];
};

export const PLAYBACK_DEST_FILTER_KEYS = [
  "PLAYBACK_MONITOR_ONLY_DESTS",
  "PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS",
  "PLAYBACK_SEARCH_FUTURE_DESTS",
] as const;

export type PlaybackDestFilterKey = (typeof PLAYBACK_DEST_FILTER_KEYS)[number];

export function isPlaybackDestFilterKey(key: string): key is PlaybackDestFilterKey {
  return (PLAYBACK_DEST_FILTER_KEYS as readonly string[]).includes(key);
}

export function asDestPathList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const path = String(item || "").trim();
    if (!path) continue;
    const key = path.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(path);
  }
  return out;
}

function normalizePathKey(path: string): string {
  return path.trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

function pathSelected(selected: string[], path: string): boolean {
  const want = normalizePathKey(path);
  return selected.some((p) => normalizePathKey(p) === want);
}

function togglePath(selected: string[], path: string, on: boolean): string[] {
  const want = normalizePathKey(path);
  const without = selected.filter((p) => normalizePathKey(p) !== want);
  if (!on) return without;
  return [...without, path.trim()];
}

function setGroupSelected(selected: string[], groupPaths: string[], on: boolean): string[] {
  const groupKeys = new Set(groupPaths.map(normalizePathKey));
  const without = selected.filter((p) => !groupKeys.has(normalizePathKey(p)));
  if (!on) return without;
  const next = [...without];
  for (const path of groupPaths) {
    if (!pathSelected(next, path)) next.push(path);
  }
  return next;
}

function DestGroup(props: {
  title: string;
  paths: string[];
  selected: string[];
  disabled?: boolean;
  onChange: (next: string[]) => void;
}) {
  const allOn =
    props.paths.length > 0 && props.paths.every((path) => pathSelected(props.selected, path));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
          {props.title}
        </p>
        {props.paths.length > 0 ? (
          <button
            type="button"
            disabled={props.disabled}
            onClick={() => props.onChange(setGroupSelected(props.selected, props.paths, !allOn))}
            className="text-[11px] font-headline uppercase tracking-wider text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline disabled:opacity-40 disabled:no-underline"
          >
            {allOn ? "Clear" : "Select all"}
          </button>
        ) : null}
      </div>
      {props.paths.length === 0 ? (
        <p className="text-[13px] text-slate-500">No {props.title.toLowerCase()} destinations configured yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {props.paths.map((path) => {
            const checked = pathSelected(props.selected, path);
            return (
              <li key={path}>
                <label
                  className={`flex cursor-pointer items-start gap-2 rounded-lg border border-[#424753]/35 bg-[#0b111b]/40 px-3 py-2 ${
                    props.disabled ? "cursor-not-allowed opacity-55" : "hover:border-[#424753]/60"
                  }`}
                >
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={checked}
                    disabled={props.disabled}
                    onChange={(e) => props.onChange(togglePath(props.selected, path, e.target.checked))}
                  />
                  <span className="min-w-0 break-all font-mono text-[13px] text-slate-300" title={path}>
                    {path}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function PlaybackDestFilterField(props: {
  label: string;
  description: string;
  value: unknown;
  options: PlaybackDestOptions;
  disabled?: boolean;
  note?: string;
  onChange: (next: string[]) => void;
}) {
  const selected = asDestPathList(props.value);
  const movies = props.options.movies || [];
  const tv = props.options.tv || [];
  const emptyOptions = movies.length === 0 && tv.length === 0;

  return (
    <div className={`${UI_SECTION_FRAME_CLASS} space-y-3 p-4 ${props.disabled ? "opacity-80" : ""}`}>
      <div>
        <p className="text-[14px] font-semibold text-slate-200">{props.label}</p>
        <p className="mt-1 text-[13px] leading-relaxed text-slate-400">{props.description}</p>
        {props.note ? <p className="mt-1 text-[12px] leading-relaxed text-slate-500">{props.note}</p> : null}
      </div>
      {emptyOptions ? (
        <p className="text-[13px] text-slate-500">
          Set library folders under Paths first. New destinations stay off until you add them here.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <DestGroup
            title="Movies"
            paths={movies}
            selected={selected}
            disabled={props.disabled}
            onChange={props.onChange}
          />
          <DestGroup
            title="TV"
            paths={tv}
            selected={selected}
            disabled={props.disabled}
            onChange={props.onChange}
          />
        </div>
      )}
      {selected.length > 0 ? (
        <p className="text-[12px] text-slate-500">
          Applies to {selected.length} destination{selected.length === 1 ? "" : "s"}. New destinations are off until
          you add them here.
        </p>
      ) : (
        <p className="text-[12px] text-slate-500">Off for all destinations.</p>
      )}
    </div>
  );
}

const FILTER_META: Record<
  PlaybackDestFilterKey,
  { label: string; description: string }
> = {
  PLAYBACK_MONITOR_ONLY_DESTS: {
    label: "Monitor only on playback (no search)",
    description:
      "Pick library destinations where play only marks titles monitored in Radarr/Sonarr, without searching. Leave empty to search on play everywhere (the default).",
  },
  PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS: {
    label: "Search already-monitored titles on playback",
    description:
      "Pick destinations where titles already monitored in Radarr/Sonarr can be searched again on play. Destinations not listed skip that re-search.",
  },
  PLAYBACK_SEARCH_FUTURE_DESTS: {
    label: "Search future titles on playback",
    description:
      "Pick destinations where not-yet-released titles are searched on play. Destinations not listed can still be monitored but are not searched on this play.",
  },
};

export function PlaybackDestFilterControls(props: {
  values: Record<string, unknown>;
  options: PlaybackDestOptions;
  onChange: (key: PlaybackDestFilterKey, next: string[]) => void;
  /** When true, omit the section heading (Settings field already has a section title). */
  hideHeading?: boolean;
}) {
  const monitorOnlySelected = asDestPathList(props.values.PLAYBACK_MONITOR_ONLY_DESTS);
  const monitorOnlyActive = monitorOnlySelected.length > 0;

  const fields = useMemo(() => PLAYBACK_DEST_FILTER_KEYS.map((key) => ({ key, ...FILTER_META[key] })), []);

  return (
    <div className="space-y-5">
      {props.hideHeading ? null : (
        <>
          <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
            Playback monitoring and searching
          </p>
          <p className="text-[15px] leading-relaxed text-slate-300">
            Placeholdarr reacts when a placeholder or real file is played: it builds an Arr target list, then monitors
            or searches accordingly. Choose which library destinations each filter applies to (Movies and TV are listed
            separately).
          </p>
        </>
      )}
      {fields.map((field) => {
        const isSecondary =
          field.key === "PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS" ||
          field.key === "PLAYBACK_SEARCH_FUTURE_DESTS";
        return (
          <PlaybackDestFilterField
            key={field.key}
            label={field.label}
            description={field.description}
            value={props.values[field.key]}
            options={props.options}
            note={
              isSecondary && monitorOnlyActive
                ? "On destinations also listed under Monitor only, play still monitors without searching (monitor only wins)."
                : undefined
            }
            onChange={(next) => props.onChange(field.key, next)}
          />
        );
      })}
    </div>
  );
}

export function playbackDestFilterSummary(values: Record<string, unknown>): string {
  const monitor = asDestPathList(values.PLAYBACK_MONITOR_ONLY_DESTS);
  if (monitor.length > 0) {
    return `Monitor only is on for ${monitor.length} destination${monitor.length === 1 ? "" : "s"}: those plays mark targets monitored without searching.`;
  }
  const searchMonitored = asDestPathList(values.PLAYBACK_SEARCH_ALREADY_MONITORED_DESTS);
  const searchFuture = asDestPathList(values.PLAYBACK_SEARCH_FUTURE_DESTS);
  const parts: string[] = [
    "Placeholdarr monitors targets as needed and searches the ones that still qualify on destinations you enable below.",
  ];
  if (searchMonitored.length === 0 && searchFuture.length === 0) {
    parts.push("Search already-monitored and Search future are off for all destinations.");
  } else if (searchMonitored.length === 0) {
    parts.push(
      `Search already-monitored is off everywhere; Search future is on for ${searchFuture.length} destination${searchFuture.length === 1 ? "" : "s"}.`,
    );
  } else if (searchFuture.length === 0) {
    parts.push(
      `Search already-monitored is on for ${searchMonitored.length} destination${searchMonitored.length === 1 ? "" : "s"}; Search future is off everywhere.`,
    );
  } else {
    parts.push(
      `Search already-monitored (${searchMonitored.length}) and Search future (${searchFuture.length}) are enabled on the destinations you selected.`,
    );
  }
  return parts.join(" ");
}

/** Build Movies/TV dest options from settings values when API options are missing. */
export function playbackDestOptionsFromValues(values: Record<string, unknown>): PlaybackDestOptions {
  const movies: string[] = [];
  const tv: string[] = [];
  const movieDefault = String(values.MOVIE_LIBRARY_FOLDER || "").trim();
  const tvDefault = String(values.TV_LIBRARY_FOLDER || "").trim();
  if (movieDefault) movies.push(movieDefault);
  if (tvDefault) tv.push(tvDefault);
  try {
    const raw = String(values.LIBRARY_DESTINATION_MAP_JSON || "").trim();
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) {
        for (const row of parsed) {
          if (!row || typeof row !== "object") continue;
          const dest = String((row as { dest_folder?: string }).dest_folder || "").trim();
          const arrType = String((row as { arr_type?: string }).arr_type || "")
            .trim()
            .toLowerCase();
          if (!dest) continue;
          if (arrType === "sonarr") {
            if (!tv.some((p) => normalizePathKey(p) === normalizePathKey(dest))) tv.push(dest);
          } else if (arrType === "radarr") {
            if (!movies.some((p) => normalizePathKey(p) === normalizePathKey(dest))) movies.push(dest);
          }
        }
      }
    }
  } catch {
    /* ignore bad map JSON */
  }
  return { movies, tv };
}
