import type { CSSProperties } from "react";

/** Text on solid brand yellow (`--brand-accent`) fills — dark slate in Placeholdarr themes. */
export const FG_ON_ACCENT_TEXT_CLASS = "text-[color:var(--brand-fg-on-accent)]";

export function accentFilledStyle(accentHex: string): CSSProperties {
  return {
    backgroundColor: accentHex,
    color: "var(--brand-fg-on-accent)",
  };
}
