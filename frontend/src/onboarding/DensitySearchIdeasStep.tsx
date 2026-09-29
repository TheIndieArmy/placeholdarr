import { useMemo, useState, type ReactNode } from "react";
import { DENSITY_CHOICE_META, type TvDensityChoice } from "./steps";
import { UI_SECTION_FRAME_CLASS } from "../uiSectionFrame";
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

/** Selected option on a framed section: brand accent (gold) so it stays distinct from accent-3 panel fill. */
const cardSelectedClass =
  "border-[var(--brand-accent)] bg-[#0b111b]/90 ring-1 ring-[var(--brand-accent)]/45 shadow-lg shadow-black/25";
const cardIdleClass =
  "border-[#424753]/50 bg-[#0b111b]/45 hover:border-[#424753]/75 hover:bg-[#0b111b]/65";

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

function OptionCardGrid<K extends string>(props: {
  legend: string;
  options: { key: K; label: string; summary: string; star?: string; detail?: string }[];
  selected: K | null;
  onSelect: (key: K) => void;
  columns?: "3" | "1";
  /** Intro copy inside the framed group (above the cards). */
  intro?: ReactNode;
}) {
  const gridClass =
    props.columns === "1" ? "grid grid-cols-1 gap-3" : "grid grid-cols-1 items-stretch gap-3 sm:grid-cols-3";
  return (
    <div className={`${UI_SECTION_FRAME_CLASS} space-y-3 p-4`}>
      <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
        {props.legend}
      </p>
      {props.intro ? <div className="text-[14px] leading-relaxed text-slate-300">{props.intro}</div> : null}
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

/** Compact profile picker for Settings → Lookahead (and reusable elsewhere). */
export function TvPlayProfilePicker(props: {
  density: string;
  searchMode: string;
  onApplyProfile: (density: TvDensityKey, searchMode: TvSearchKey) => void;
  compact?: boolean;
}) {
  const matched = matchTvPlayProfile(props.density, props.searchMode);
  return (
    <div className={`${UI_SECTION_FRAME_CLASS} space-y-3 p-4`}>
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
  onDensityChange: (value: DensityKey) => void;
  onSearchModeChange: (value: SearchKey) => void;
  onLookaheadChange: (value: number) => void;
  onRetireWhenChange: (value: TvDensityRetireWhen) => void;
  guide?: ReactNode;
}) {
  const matched = matchTvPlayProfile(props.density, props.searchMode);
  const [customizeOpen, setCustomizeOpen] = useState(matched === "custom");
  const lookaheadLocked = props.searchMode === "series";
  const retireWhenLocked = props.density === "episode";
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
          className={`group ${UI_SECTION_FRAME_CLASS} overflow-hidden`}
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

            <div
              className={`space-y-2 rounded-xl border px-4 py-4 ${
                lookaheadLocked
                  ? "border-[#424753]/30 bg-[#0b111b]/40 opacity-60"
                  : "border-[#424753]/50 bg-[#0b111b]/45"
              }`}
            >
              <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
                Lookahead range
              </p>
              <p className="text-[14px] leading-relaxed text-slate-300">
                For Episode and Season search modes, Lookahead range sets how far ahead Placeholdarr will try to action
                episodes in Arr. Series search already covers the whole show, so Lookahead is unused there.
              </p>
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
        </details>

        <div
          className={`${UI_SECTION_FRAME_CLASS} space-y-2 p-4 ${
            retireWhenLocked ? "opacity-60" : ""
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

      </div>

      <div className="shrink-0 -mx-6 border-t border-[#424753]/40 bg-[#121722] px-6 py-4 sm:-mx-8 sm:px-8">
        <p className="text-[12px] font-headline font-semibold uppercase tracking-wider text-slate-400">
          What to expect with your current settings
        </p>
        <p className="mt-2 text-[14px] leading-relaxed text-slate-200">
          {entryLine} Monitor and search filters for each library destination come next on Playback.
        </p>
      </div>
    </div>
  );
}
