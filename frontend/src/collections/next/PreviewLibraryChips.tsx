import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../../brandAccentUi";
import type { ThemeMode } from "../../brandTypes";
import type { PlexSectionOption } from "../../types/api";
import { getCollectionTheme } from "../collectionTheme";

/** All libraries + per-library chips (classic preview). null = combined view. */
export function PreviewLibraryChips(props: {
  themeMode: ThemeMode;
  accentHex: string;
  libraries: PlexSectionOption[];
  value: number | null;
  onChange: (sectionId: number | null) => void;
  /** When true, require a concrete library (no All). Used by In Plex mock. */
  requireLibrary?: boolean;
  allLabel?: string;
}) {
  const theme = getCollectionTheme(props.themeMode === "light");
  if (props.libraries.length < 2 && !props.requireLibrary) return null;

  const chipClass = (active: boolean) =>
    `rounded-md border px-2 py-1 text-[11px] font-headline uppercase tracking-wider ${
      active ? FG_ON_ACCENT_TEXT_CLASS : theme.chipInactive
    }`;
  const chipStyle = (active: boolean) =>
    active ? { ...accentFilledStyle(props.accentHex), borderColor: props.accentHex } : undefined;

  return (
    <div className="flex flex-wrap gap-1">
      {!props.requireLibrary ? (
        <button
          type="button"
          onClick={() => props.onChange(null)}
          className={chipClass(props.value == null)}
          style={chipStyle(props.value == null)}
        >
          {props.allLabel ?? "All libraries"}
        </button>
      ) : null}
      {props.libraries.map((lib) => (
        <button
          key={lib.id}
          type="button"
          onClick={() => props.onChange(lib.id)}
          className={chipClass(props.value === lib.id)}
          style={chipStyle(props.value === lib.id)}
        >
          {lib.title}
        </button>
      ))}
    </div>
  );
}
