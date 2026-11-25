import type { PlexSectionOption } from "../types/api";

/** Sections this Placeholdarr instance controls (placeholder dests + Arr-root real libs). */
export function managedPlexSections(sections: PlexSectionOption[]): PlexSectionOption[] {
  const managed = sections.filter((s) => s.managed === true);
  // If the API omitted annotations or none matched, keep the full list so pickers stay usable.
  return managed.length ? managed : sections;
}

/** Managed sections for a media type, plus any currently selected ids (grandfather unmanaged). */
export function selectablePlexSections(
  sections: PlexSectionOption[],
  type: "movie" | "show" | null | undefined,
  selectedIds: number[] = [],
): PlexSectionOption[] {
  const selected = new Set(selectedIds.filter((id) => Number.isFinite(id) && id >= 1));
  const base = managedPlexSections(sections).filter((s) => !type || s.type === type);
  const extra = sections.filter(
    (s) => selected.has(s.id) && (!type || s.type === type) && !base.some((b) => b.id === s.id),
  );
  return [...base, ...extra];
}

export function managedRoleLabel(role: PlexSectionOption["managed_role"]): string | null {
  if (role === "placeholder") return "Placeholders";
  if (role === "real") return "Real files";
  if (role === "both") return "Placeholders + real files";
  return null;
}
