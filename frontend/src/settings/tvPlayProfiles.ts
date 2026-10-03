/** Shared TV density + search profiles for onboarding Profile and Settings → Density & Lookahead. */

export type TvDensityKey = "episode" | "season" | "series";
export type TvSearchKey = "episode" | "season" | "series";
export type TvPlayProfileKey = "watch_as_you_go" | "season_at_a_time" | "request_the_show";

const DENSITY_SHORT: Record<TvDensityKey, string> = {
  episode: "Episode",
  season: "Season",
  series: "Series",
};

const SEARCH_SHORT: Record<TvSearchKey, string> = {
  episode: "Episode",
  season: "Season",
  series: "Series",
};

export type TvPlayProfile = {
  key: TvPlayProfileKey;
  label: string;
  star: string;
  summary: string;
  density: TvDensityKey;
  searchMode: TvSearchKey;
};

export const TV_PLAY_PROFILES: readonly TvPlayProfile[] = [
  {
    key: "watch_as_you_go",
    label: "Watch as you go",
    star: "Progressive watching",
    summary:
      "Episode placeholders in your library. Play to monitor and search that episode plus a short Lookahead buffer, instead of the whole show up front.",
    density: "episode",
    searchMode: "episode",
  },
  {
    key: "season_at_a_time",
    label: "Season at a time",
    star: "Balanced approach",
    summary:
      "One placeholder per season that still needs content. Play to monitor and search that season; the next season is included when its first episode enters your Lookahead range.",
    density: "season",
    searchMode: "season",
  },
  {
    key: "request_the_show",
    label: "Request the show",
    star: "Keeps it simple",
    summary:
      "One placeholder per show. Play to monitor and search the whole series. Fewest placeholder files and lightest sync work; less per-episode detail in the player.",
    density: "series",
    searchMode: "series",
  },
] as const;

/** Card subtitle: which density + search this profile sets. */
export function tvPlayProfilePairingLabel(density: TvDensityKey, searchMode: TvSearchKey): string {
  return `${DENSITY_SHORT[density]} density · ${SEARCH_SHORT[searchMode]} search`;
}

export function matchTvPlayProfile(
  density: string,
  searchMode: string,
): TvPlayProfileKey | "custom" {
  const d = String(density || "").trim().toLowerCase();
  const s = String(searchMode || "").trim().toLowerCase();
  const hit = TV_PLAY_PROFILES.find((p) => p.density === d && p.searchMode === s);
  return hit?.key ?? "custom";
}

/** Density card star text; episode "flexible requesting" only pairs with episode search. */
export function densityStarFor(density: TvDensityKey, searchMode: TvSearchKey): string {
  if (density === "episode") {
    return searchMode === "episode"
      ? "Most flexible requesting"
      : "Mostly informational detail";
  }
  if (density === "season") return "Increasing performance";
  return "Greatest simplicity";
}

export function densitySummaryFor(density: TvDensityKey, searchMode: TvSearchKey): string {
  if (density === "episode") {
    if (searchMode === "episode") {
      return "One placeholder per missing episode. Best when you also use Episode search, so each play requests that episode plus Lookahead.";
    }
    return "One placeholder per missing episode. Mostly informational in the player when Search mode is Season or Series; those modes already widen the Arr target list on play.";
  }
  if (density === "season") {
    return "One placeholder per season that still needs content. Fewer files than every episode; less per-episode detail in the library.";
  }
  return "One placeholder per show that still needs content. Fewest files and lightest sync work.";
}
