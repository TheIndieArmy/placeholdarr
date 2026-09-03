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
