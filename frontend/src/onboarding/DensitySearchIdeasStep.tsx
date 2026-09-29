import { useMemo, useState, type ReactNode } from "react";
import { ToggleSwitch } from "../ToggleSwitch";
import { DENSITY_CHOICE_META, type TvDensityChoice } from "./steps";
import {
  TV_PLAY_PROFILES,
  densityStarFor,
  densitySummaryFor,
  matchTvPlayProfile,
  tvPlayProfilePairingLabel,
  type TvDensityKey,
  type TvPlayProfileKey,
  type TvSearchKey,
} from "../settings/tvPlayProfiles";

type DensityKey = TvDensityChoice;
type SearchKey = TvSearchKey;

const cardSelectedClass =
  "border-[var(--brand-accent-3)] bg-[color:color-mix(in_srgb,var(--brand-surface-panel)_88%,var(--brand-accent-3)_12%)] shadow-lg shadow-black/20";
const cardIdleClass =
  "border-[#424753]/40 bg-[#0b111b]/50 hover:border-[#424753]/70 hover:bg-[#0b111b]/80";

function searchOptions(): { key: SearchKey; label: string; summary: string }[] {
  return [
    {
      key: "episode",
      label: "Episode",
      summary: "Action the played entry, then the next episodes within your Lookahead range.",
    },
    {
      key: "season",
      label: "Season",
      summary:
        "Focus on the season, and add the next when you get within your Lookahead range of the end.",
    },
    {
      key: "series",
      label: "Series",
      summary: "Action the whole show from that play. Lookahead range does not apply.",
    },
  ];
}

function episodesPhrase(n: number): string {
  return n === 1 ? "1 episode" : `${n} episodes`;
}

function playEntryCopy(density: DensityKey, search: SearchKey, lookahead: number): string {
  const ahead = episodesPhrase(Math.max(1, Math.floor(lookahead) || 1));
  if (density === "episode") {
    if (search === "episode") {
      return `You play a specific episode (placeholder or real). Placeholdarr actions that episode plus the next ${ahead}.`;
    }
    if (search === "season") {
      return `You play a specific episode (placeholder or real). Targets cover the rest of that season, and add the next season when you get within ${ahead} of the end.`;
    }
    return "You play a specific episode (placeholder or real). Targets cover the whole show from that play.";
  }

  if (density === "season") {
    if (search === "episode") {
      return `You play the season placeholder. Targets start at the first episode in that season that still needs content, then continue through the next ${ahead}. You do not get a full missing-episode list to click.`;
    }
    if (search === "season") {
      return `You play the season placeholder. Targets focus on that season, and add the next season when you get within ${ahead} of the end.`;
    }
    return "You play the season placeholder. Targets can still widen to the whole show from that play.";
  }

  if (search === "episode") {
    return `You play the show placeholder. Targets start from early missing episodes in that show, then continue through the next ${ahead}. There is no per-episode list to pick from.`;
  }
  if (search === "season") {
    return `You play the show placeholder. Targets use season-width from that single show entry, and add the next season when you get within ${ahead} of the end.`;
  }
  return "You play the show placeholder. Targets cover the whole show from that one entry.";
}

function playMonitorSearchCopy(input: {
  monitorOnly: boolean;
  skipMonitored: boolean;
  skipFuture: boolean;
}): string {
  if (input.monitorOnly) {
    return "Monitor only is enabled: unmonitored targets are marked monitored in Sonarr, without searching on this play.";
  }

  const parts: string[] = [
    "Placeholdarr monitors targets as needed and searches the ones that still qualify.",
  ];
  if (input.skipMonitored && input.skipFuture) {
    parts.push(
      "Search already-monitored and Search future titles are disabled, so those targets are monitored if needed but not searched.",
    );
  } else if (input.skipMonitored) {
    parts.push(
      "Search already-monitored is disabled, so titles Arr already had monitored are not searched again.",
    );
  } else if (input.skipFuture) {
    parts.push(
      "Search future titles is disabled, so not-yet-released targets are monitored if needed but not searched.",
    );
  } else {
    parts.push(
      "Search already-monitored and Search future titles are enabled, so those targets in the list can be searched too.",
    );
  }
  return parts.join(" ");
}

function OptionCardGrid<K extends string>(props: {
  legend: string;
  options: { key: K; label: string; summary: string; star?: string; detail?: string }[];
  selected: K | null;
  onSelect: (key: K) => void;
  columns?: "3" | "1";
}) {
  const gridClass =
    props.columns === "1" ? "grid grid-cols-1 gap-3" : "grid grid-cols-1 items-stretch gap-3 sm:grid-cols-3";
  return (
    <div className="space-y-2">
      <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
        {props.legend}
      </p>
      <div className={gridClass}>
        {props.options.map((opt) => {
          const selected = props.selected === opt.key;
          return (
            <button
              key={opt.key}
              type="button"
              onClick={() => props.onSelect(opt.key)}
              className={`flex h-full flex-col rounded-xl border px-4 py-4 text-left transition ${selected ? cardSelectedClass : cardIdleClass}`}
            >
              <div className="text-[16px] font-headline font-semibold text-white">{opt.label}</div>
              {opt.star ? (
                <p className="mt-1.5 min-h-[2.5rem] text-[12px] font-semibold leading-snug text-[var(--brand-accent)]">
                  * {opt.star}
                </p>
              ) : null}
              <p className="mt-2 text-[13px] leading-snug text-slate-400">{opt.summary}</p>
              {opt.detail ? (
                <p className="mt-auto pt-3 text-[11px] leading-snug text-slate-500">{opt.detail}</p>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ToggleRow(props: {
  label: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div
      className={`flex items-start gap-3 rounded-xl border border-[#424753]/40 bg-[#0b111b]/50 px-4 py-3 ${
        props.disabled ? "opacity-55" : ""
      }`}
    >
      <ToggleSwitch
        checked={props.checked}
        onChange={props.onChange}
        accentHex="var(--brand-accent)"
        disabled={props.disabled}
        ariaLabel={props.label}
        className="mt-0.5"
      />
      <div className="min-w-0">
        <p className="text-[14px] font-semibold text-slate-200">{props.label}</p>
        <p className="mt-1 text-[13px] leading-relaxed text-slate-400">{props.description}</p>
      </div>
    </div>
  );
}

/** Compact profile picker for Settings → Lookahead (and reusable elsewhere). */
export function TvPlayProfilePicker(props: {
  density: string;
  searchMode: string;
  onApplyProfile: (density: TvDensityKey, searchMode: TvSearchKey) => void;
  compact?: boolean;
}) {
  const matched = matchTvPlayProfile(props.density, props.searchMode);
  return (
    <div className="space-y-2">
      <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
        Starting profile
      </p>
      <p className="ui-field-description leading-relaxed">
        Pick a common pairing of library density and Search mode. Each card shows which settings it applies. You can
        still change density and search separately below.
      </p>
      <div className={`grid gap-3 ${props.compact ? "grid-cols-1" : "grid-cols-1 sm:grid-cols-3"}`}>
        {TV_PLAY_PROFILES.map((profile) => {
          const selected = matched === profile.key;
          return (
            <button
              key={profile.key}
              type="button"
              onClick={() => props.onApplyProfile(profile.density, profile.searchMode)}
              className={`flex h-full flex-col rounded-xl border px-4 py-4 text-left transition ${selected ? cardSelectedClass : cardIdleClass}`}
            >
              <div className="text-[15px] font-headline font-semibold text-white">{profile.label}</div>
              <p className="mt-1.5 text-[12px] font-semibold leading-snug text-[var(--brand-accent)]">
                * {profile.star}
              </p>
              <p className="mt-2 text-[13px] leading-snug text-slate-400">{profile.summary}</p>
              <p className="mt-auto pt-3 text-[11px] leading-snug text-slate-500">
                {tvPlayProfilePairingLabel(profile.density, profile.searchMode)}
              </p>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export type TvDensityRetireWhen =
  | "when_no_episode_needs_placeholder"
  | "when_any_episode_has_file";

const RETIRE_WHEN_OPTIONS: { value: TvDensityRetireWhen; label: string }[] = [
  {
    value: "when_no_episode_needs_placeholder",
    label: "No episode still needs a placeholder (recommended)",
  },
  {
    value: "when_any_episode_has_file",
    label: "Any episode in the show (or season) has a real file",
  },
];

export function DensitySearchIdeasStep(props: {
  density: DensityKey;
  searchMode: SearchKey;
  lookahead: number;
  retireWhen: TvDensityRetireWhen;
  monitorOnly: boolean;
  skipMonitoredSearch: boolean;
  skipFutureSearch: boolean;
  onDensityChange: (value: DensityKey) => void;
  onSearchModeChange: (value: SearchKey) => void;
  onLookaheadChange: (value: number) => void;
  onRetireWhenChange: (value: TvDensityRetireWhen) => void;
  onMonitorOnlyChange: (value: boolean) => void;
  onSkipMonitoredSearchChange: (value: boolean) => void;
  onSkipFutureSearchChange: (value: boolean) => void;
  guide?: ReactNode;
}) {
  const matched = matchTvPlayProfile(props.density, props.searchMode);
  const [customizeOpen, setCustomizeOpen] = useState(matched === "custom");
  const lookaheadLocked = props.searchMode === "series";
  const retireWhenLocked = props.density === "episode";
  const searchFiltersLocked = props.monitorOnly;
  const lookahead = Math.max(1, Math.floor(Number(props.lookahead) || 1));
  const retireWhen: TvDensityRetireWhen =
    props.retireWhen === "when_any_episode_has_file"
      ? "when_any_episode_has_file"
      : "when_no_episode_needs_placeholder";
  const searchOpts = useMemo(() => searchOptions(), []);
  const densityOptions = useMemo(
    () =>
      (["episode", "season", "series"] as const).map((key) => ({
        key,
        label: DENSITY_CHOICE_META[key].label,
        summary: densitySummaryFor(key, props.searchMode),
        star: densityStarFor(key, props.searchMode),
      })),
    [props.searchMode],
  );
  const entryLine = playEntryCopy(props.density, props.searchMode, lookahead);
  const actionLine = playMonitorSearchCopy({
    monitorOnly: props.monitorOnly,
    skipMonitored: props.skipMonitoredSearch,
    skipFuture: props.skipFutureSearch,
  });

  function applyProfile(key: TvPlayProfileKey) {
    const profile = TV_PLAY_PROFILES.find((p) => p.key === key);
    if (!profile) return;
    props.onDensityChange(profile.density);
    props.onSearchModeChange(profile.searchMode);
    setCustomizeOpen(false);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto pb-4" data-step-scroll>
        {props.guide}
        <div className="space-y-3 text-[15px] leading-relaxed text-slate-300">
          <p>Open Customize profile for full details and to choose your own mix.</p>
        </div>

        <OptionCardGrid
          legend="Starting profile"
          options={TV_PLAY_PROFILES.map((p) => ({
            key: p.key,
            label: p.label,
            detail: tvPlayProfilePairingLabel(p.density, p.searchMode),
            summary: p.summary,
            star: p.star,
          }))}
          selected={matched === "custom" ? null : matched}
          onSelect={applyProfile}
        />

        <details
          className="group rounded-lg border border-[#424753]/40 bg-[#0b111b]/40"
          open={customizeOpen || matched === "custom"}
          onToggle={(e) => setCustomizeOpen((e.target as HTMLDetailsElement).open)}
        >
          <summary className="flex cursor-pointer list-none select-none items-center gap-2 px-4 py-3 text-[13px] font-headline uppercase tracking-wider text-slate-300">
            <span
              className="material-symbols-outlined text-slate-500 transition-transform group-open:rotate-90"
              style={{ fontSize: 18 }}
            >
              chevron_right
            </span>
            Customize profile
          </summary>
          <div className="space-y-6 border-t border-[#424753]/30 px-4 pb-4 pt-4">
            <div className="space-y-3 text-[14px] leading-relaxed text-slate-300">
              <p>
                Starting profiles pair TV placeholder density with Search mode. Density is how many placeholder files
                appear in the player (lower density is lighter on sync and disk). Search mode is how wide Sonarr&apos;s
                target list is when you play something. Episode density shows the most library detail; with Season or
                Series search that detail is mostly informational, because play already widens the Arr target list.
              </p>
            </div>

            <OptionCardGrid
              legend="TV placeholder density"
              options={densityOptions}
              selected={props.density}
              onSelect={props.onDensityChange}
            />

            <div className="space-y-3 text-[14px] leading-relaxed text-slate-300">
              <p>
                Change either half of the profile below. Search mode is what drives flexible vs broad requesting on
                play.
              </p>
            </div>

            <OptionCardGrid
              legend="Search mode"
              options={searchOpts}
              selected={props.searchMode}
              onSelect={props.onSearchModeChange}
            />

            <div className="space-y-2">
              <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
                Lookahead range
              </p>
              <p className="text-[14px] leading-relaxed text-slate-300">
                For Episode and Season search modes, Lookahead range sets how far ahead Placeholdarr will try to action
                episodes in Arr. Series search already covers the whole show, so Lookahead is unused there.
              </p>
              <div
                className={`rounded-xl border px-4 py-4 ${
                  lookaheadLocked
                    ? "border-[#424753]/30 bg-[#0b111b]/30 opacity-60"
                    : "border-[#424753]/40 bg-[#0b111b]/50"
                }`}
              >
                <label className="block text-[14px] font-semibold text-slate-200">
                  Episodes ahead
                  <input
                    type="number"
                    min={1}
                    max={99}
                    disabled={lookaheadLocked}
                    className="mt-2 block w-24 rounded-lg border border-[#424753]/40 bg-[#0b111b] px-3 py-2 text-[15px] text-slate-200 outline-none disabled:cursor-not-allowed"
                    value={lookahead}
                    onChange={(e) => {
                      const raw = e.target.value.replace(/\D/g, "");
                      const n = Math.max(1, Math.min(99, Number(raw || "1")));
                      props.onLookaheadChange(n);
                    }}
                  />
                </label>
                <p className="mt-2 text-[13px] leading-relaxed text-slate-400">
                  {lookaheadLocked
                    ? "Not used while Search mode is Series (the whole show is already included)."
                    : "In Episode search, this is how many episodes ahead to look for. In Season search, this is how many episodes from the end of the season before Placeholdarr starts actioning the next season."}
                </p>
              </div>
            </div>
          </div>
        </details>

        <div
          className={`space-y-2 rounded-xl border px-4 py-4 ${
            retireWhenLocked
              ? "border-[#424753]/30 bg-[#0b111b]/30 opacity-60"
              : "border-[#424753]/40 bg-[#0b111b]/50"
          }`}
        >
          <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
            Remove season/series placeholder when
          </p>
          <p className="text-[14px] leading-relaxed text-slate-300">
            Only applies when TV placeholder density is Season or Series. Episode density still removes each episode
            placeholder when that episode no longer needs one.
          </p>
          <label className="block text-[14px] font-semibold text-slate-200">
            Retire when
            <select
              disabled={retireWhenLocked}
              className="mt-2 block w-full max-w-xl rounded-lg border border-[#424753]/40 bg-[#0b111b] px-3 py-2 text-[15px] text-slate-200 outline-none disabled:cursor-not-allowed"
              value={retireWhen}
              onChange={(e) => props.onRetireWhenChange(e.target.value as TvDensityRetireWhen)}
            >
              {RETIRE_WHEN_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          {retireWhenLocked ? (
            <p className="text-[13px] leading-relaxed text-slate-400">
              Not used while TV placeholder density is Every episode.
            </p>
          ) : null}
        </div>

        <div className="space-y-2">
          <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
            Playback monitoring and searching
          </p>
          <p className="text-[15px] leading-relaxed text-slate-300">
            Placeholdarr reacts when a placeholder or real file is played: it builds an Arr target list, then monitors or
            searches accordingly. TV uses your selected profile; movies use the played title. These modifiers apply to
            movies and TV.
          </p>
          <div className="space-y-2">
            <ToggleRow
              label="Monitor only on playback (no search)"
              description="When disabled, searching on play stays on (the default); the filters below only change which targets in the list are searched. When enabled, only mark unmonitored titles monitored in Radarr/Sonarr, without searching."
              checked={props.monitorOnly}
              onChange={props.onMonitorOnlyChange}
            />
            <ToggleRow
              label="Search already-monitored titles on playback"
              description="When disabled, titles Arr already had monitored are skipped for search (assuming Arr already searched and is tracking them). Unmonitored targets are still monitored, then searched. Useful if your indexers have stricter limits. When enabled, already-monitored targets can be searched again on play too."
              checked={!props.skipMonitoredSearch}
              disabled={searchFiltersLocked}
              onChange={(value) => props.onSkipMonitoredSearchChange(!value)}
            />
            <ToggleRow
              label="Search future titles on playback"
              description="When disabled, not-yet-released titles can still be monitored, but are not searched on this play (TV uses episode air dates; movies use your preferred Calendar release date). When enabled, future titles in the target list are searched like any other."
              checked={!props.skipFutureSearch}
              disabled={searchFiltersLocked}
              onChange={(value) => props.onSkipFutureSearchChange(!value)}
            />
          </div>
        </div>
      </div>

      <div className="shrink-0 -mx-6 border-t border-[#424753]/40 bg-[#121722] px-6 py-4 sm:-mx-8 sm:px-8">
        <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
          What to expect with your current settings
        </p>
        <p className="mt-2 text-[14px] leading-relaxed text-slate-200">
          {entryLine} {actionLine}
        </p>
      </div>
    </div>
  );
}
