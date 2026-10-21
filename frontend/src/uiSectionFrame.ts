/**
 * Shared surface for option/section groups (onboarding + Settings).
 * Accent-tinted panel fill used for control clusters (Playback filters, Arr routing, Profile groups).
 */
export const UI_SECTION_FRAME_CLASS =
  "rounded-lg border border-[var(--brand-accent-3)] bg-[color:color-mix(in_srgb,var(--brand-surface-panel)_92%,var(--brand-accent-3)_8%)] shadow-lg shadow-black/15";

/**
 * Micro-legend for a framed control group (Density frames, recipe Display subsections, etc.).
 * White so the section does not read as disabled; keep descriptive body copy on muted slate.
 */
export const UI_SECTION_LEGEND_CLASS =
  "text-[12px] font-headline font-semibold uppercase tracking-wider text-white";
