import type { PlaceholderPolicy } from "./PinGlyph";

export const POLICY_LABEL: Record<PlaceholderPolicy, string> = {
  auto: "Auto",
  never: "Never",
  pinned: "Pinned",
};

export const POLICY_TOOLTIP: Record<PlaceholderPolicy, string> = {
  auto: "Auto: follow Placeholdarr settings",
  never: "Never create a placeholder. Does not remove real files on disk.",
  pinned: "Pin placeholder creation. Does not apply when a real file is on disk.",
};

export type TvPlaceholderDensity = "episode" | "season" | "series";

/** True when episode pins are disabled in the library UI for this density. */
export function densityLocksEpisodePins(density: string | null | undefined): boolean {
  const d = String(density || "episode").trim().toLowerCase();
  return d === "season" || d === "series";
}

/** True when season pins are disabled in the library UI for this density. */
export function densityLocksSeasonPins(density: string | null | undefined): boolean {
  return String(density || "episode").trim().toLowerCase() === "series";
}

/**
 * Hover copy when a pin is greyed because density does not use that placeholder level.
 * Prefer pinning the coarser placeholder (season / series) instead.
 */
export function densityPolicyUnavailableReason(
  mediaType: "movie" | "episode" | "series" | "season",
  density: string | null | undefined,
): string | null {
  const d = String(density || "episode").trim().toLowerCase();
  if (mediaType === "episode" && d === "season") {
    return (
      "Not available in Season density. Pin the season (or series) if you want to keep or block " +
      "that season or series placeholder."
    );
  }
  if (mediaType === "episode" && d === "series") {
    return (
      "Not available in Series density. Pin the series if you want to keep or block the series placeholder."
    );
  }
  if (mediaType === "season" && d === "series") {
    return (
      "Not available in Series density. Pin the series if you want to keep or block the series placeholder."
    );
  }
  return null;
}

/** @deprecated Use densityPolicyUnavailableReason; kept for any leftover imports. */
export function densityPolicyHint(
  mediaType: "movie" | "episode" | "series" | "season",
  density: string | null | undefined,
): string | null {
  return densityPolicyUnavailableReason(mediaType, density);
}

const CYCLE_ORDER: PlaceholderPolicy[] = ["auto", "never", "pinned"];

export function nextPlaceholderPolicy(current: PlaceholderPolicy): PlaceholderPolicy {
  const index = CYCLE_ORDER.indexOf(current);
  return CYCLE_ORDER[(index + 1) % CYCLE_ORDER.length];
}

export function policyFromFlags(
  forcePlaceholder?: boolean,
  blockPlaceholder?: boolean,
  placeholderPolicy?: PlaceholderPolicy | null,
): PlaceholderPolicy {
  if (placeholderPolicy === "auto" || placeholderPolicy === "never" || placeholderPolicy === "pinned") {
    return placeholderPolicy;
  }
  if (forcePlaceholder) return "pinned";
  if (blockPlaceholder) return "never";
  return "auto";
}
